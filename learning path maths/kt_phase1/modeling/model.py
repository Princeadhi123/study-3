"""Shared Transformer-based KT model for all variants.

Variant is controlled purely by which features are switched on:
  A "skill_only"          use_item=False, use_content=False
  B "skill_item"          use_item=True,  use_content=False
  C "skill_item_content"  use_item=True,  use_content=True
  D "skill_item_content_option"  use_item=True, use_content=True, use_option=True

Architecture (SAKT/DKT-style, causal self-attention):
  interaction_i = embed(skill_{i-1}, item_{i-1}, correct_{i-1}, rt_{i-1},
                         attempt_{i-1}, content_{i-1}, option_{i-1})
                         (i=1 uses a learned start token instead of position 0)
  h_1..h_T = CausalTransformerEncoder(interaction_1..interaction_T)
  query_i  = embed(skill_i, item_i, content_i)          -- no correctness or
                                                            option, both would
                                                            leak the label!
  logit_i  = MLP(concat(h_i, query_i))
  P(correct_i) = sigmoid(logit_i)

`h_i` only depends on interactions strictly before step i, so this is a valid
next-step-correctness predictor with no label leakage.

Variant D (`use_content=True, use_option=True`) adds, as part of the
PREVIOUS step's interaction only (never the current query -- see
dataset.py's `selected_text_idx`), the frozen multilingual embedding of the
ACTUAL TEXT the student selected/answered (e.g. "-24"), looked up in the
exact same `content_vectors` table as the current question's content.
Deliberately NOT a plain `nn.Embedding(option_position)`: a bare
option-index embedding would force "option 1" to mean the same thing on
every question it appears on, when in reality option 1 is "4" on one
question and "20" on another -- semantically unrelated. Embedding the text
itself means the model sees what was actually picked, not an arbitrary
offered-order position. This only carries real information for the ~42% of
rows that come from a multiple-choice/single-answer exercise type
(MATH_DRILLER/VILLE_QUIZ/VOICE_DRILLER/CROSSWORD_PUZZLE, see
build_v2_raw_clean.py); every other row's selected_text is the constant
"[NO_OPTIONS]" sentinel (itself just another embedded string). It is a
building block toward a misconception-aware variant: once
kt_phase1/reports_v2/distractor_catalog.csv is tagged with misconception_ids,
the same slot can carry a `misconception_embed(prev_misconception_id)`
lookup instead of (or concatenated with) this text embedding.
"""
import torch
import torch.nn as nn


class KTTransformer(nn.Module):
    def __init__(self, n_skills, n_items, use_item=True, use_content=False,
                 use_option=False,
                 content_dim=0, d_model=128, n_heads=4, n_layers=2,
                 dim_feedforward=256, dropout=0.2, max_seq_len=400):
        super().__init__()
        self.use_item = use_item
        self.use_content = use_content and content_dim > 0
        # use_option reuses the SAME frozen content_vectors table (the
        # previous selection's text is embedded exactly like a question's
        # content, see module docstring), so it needs the same content_dim.
        self.use_option = use_option and content_dim > 0

        self.skill_embed = nn.Embedding(n_skills, d_model, padding_idx=0)
        self.correct_embed = nn.Embedding(3, d_model)  # 0=incorrect, 1=correct, 2=start-token placeholder
        if use_item:
            self.item_embed = nn.Embedding(n_items, d_model, padding_idx=0)
        if self.use_content:
            self.content_proj = nn.Linear(content_dim, d_model)
        if self.use_option:
            # Separate learnable projection from content_proj: same frozen
            # input vectors, but "this is what was previously selected" is a
            # different role than "this is the current question", so they
            # don't have to share weights.
            self.option_proj = nn.Linear(content_dim, d_model)

        self.rt_proj = nn.Linear(2, d_model)  # [log1p(response_time) or 0, has_response_time flag]
        self.attempt_proj = nn.Linear(1, d_model)
        self.start_token = nn.Parameter(torch.zeros(1, 1, d_model))
        self.pos_embed = nn.Embedding(max_seq_len, d_model)

        interaction_input_dim = d_model  # sum of feature embeddings, projected below
        self.interaction_proj = nn.Linear(d_model, d_model)

        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=n_heads, dim_feedforward=dim_feedforward,
            dropout=dropout, batch_first=True, activation="gelu",
        )
        self.encoder = nn.TransformerEncoder(encoder_layer, num_layers=n_layers)

        query_dim = d_model * (2 if use_item else 1) + (d_model if self.use_content else 0)
        self.output_head = nn.Sequential(
            nn.Linear(d_model + query_dim, dim_feedforward),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(dim_feedforward, 1),
        )

    def _content_vec(self, content_idx, content_vectors, proj):
        if content_vectors is None:
            return None
        vecs = content_vectors[content_idx]  # (B, T, content_dim), frozen lookup
        return proj(vecs)

    def forward(self, batch, content_vectors=None):
        skill, item = batch["skill"], batch["item"]
        correct, rt, rt_mask, attempt = batch["correct"], batch["rt"], batch["rt_mask"], batch["attempt"]
        content_idx, attn_mask = batch["content_idx"], batch["attn_mask"]
        B, T = skill.shape
        device = skill.device

        # ---- previous-step interaction features, shifted right by one ---
        prev_skill = torch.zeros_like(skill)
        prev_skill[:, 1:] = skill[:, :-1]
        prev_correct = torch.full_like(skill, 2)  # 2 = start-token placeholder for position 0
        prev_correct[:, 1:] = correct[:, :-1].long()
        prev_rt = torch.zeros_like(rt)
        prev_rt[:, 1:] = rt[:, :-1]
        prev_rt_mask = torch.zeros_like(rt_mask)
        prev_rt_mask[:, 1:] = rt_mask[:, :-1]
        prev_attempt = torch.ones_like(attempt)
        prev_attempt[:, 1:] = attempt[:, :-1]

        interaction = self.skill_embed(prev_skill) + self.correct_embed(prev_correct)
        interaction = interaction + self.rt_proj(torch.stack([prev_rt, prev_rt_mask], dim=-1))
        interaction = interaction + self.attempt_proj(prev_attempt.unsqueeze(-1))
        if self.use_item:
            prev_item = torch.zeros_like(item)
            prev_item[:, 1:] = item[:, :-1]
            interaction = interaction + self.item_embed(prev_item)
        if self.use_content:
            prev_content_idx = torch.zeros_like(content_idx)
            prev_content_idx[:, 1:] = content_idx[:, :-1]
            prev_content = self._content_vec(prev_content_idx, content_vectors, self.content_proj)
            interaction = interaction + prev_content
        if self.use_option:
            # The PREVIOUS step's selected-answer TEXT, embedded via the same
            # frozen content_vectors table as question content (see
            # dataset.py's selected_text_idx / module docstring) -- never the
            # current step's selection, which would leak the label.
            selected_text_idx = batch["selected_text_idx"]
            prev_selected_idx = torch.zeros_like(selected_text_idx)
            prev_selected_idx[:, 1:] = selected_text_idx[:, :-1]
            prev_option = self._content_vec(prev_selected_idx, content_vectors, self.option_proj)
            interaction = interaction + prev_option

        interaction[:, 0, :] = self.start_token.expand(B, -1, -1).squeeze(1)
        interaction = self.interaction_proj(interaction)
        positions = torch.arange(T, device=device).unsqueeze(0).expand(B, -1)
        interaction = interaction + self.pos_embed(positions)

        # Boolean masks throughout (True = "not allowed to attend"), matching
        # src_key_padding_mask's convention, to avoid mixing float/bool masks.
        causal_mask = torch.triu(torch.ones(T, T, dtype=torch.bool, device=device), diagonal=1)
        padding_mask = attn_mask == 0  # True where padded, for src_key_padding_mask
        hidden = self.encoder(interaction, mask=causal_mask, src_key_padding_mask=padding_mask)

        # ---- query features for the step being predicted (no correctness!) ---
        query_parts = [self.skill_embed(skill)]
        if self.use_item:
            query_parts.append(self.item_embed(item))
        if self.use_content:
            query_parts.append(self._content_vec(content_idx, content_vectors, self.content_proj))
        query = torch.cat(query_parts, dim=-1)

        logits = self.output_head(torch.cat([hidden, query], dim=-1)).squeeze(-1)
        return logits


def build_model(variant, n_skills, n_items, content_dim=0, **kwargs):
    if variant == "skill_only":
        return KTTransformer(n_skills, n_items, use_item=False, use_content=False, **kwargs)
    if variant == "skill_item":
        return KTTransformer(n_skills, n_items, use_item=True, use_content=False, **kwargs)
    if variant == "skill_item_content":
        return KTTransformer(n_skills, n_items, use_item=True, use_content=True,
                              content_dim=content_dim, **kwargs)
    if variant == "skill_item_content_option":
        return KTTransformer(n_skills, n_items, use_item=True, use_content=True,
                              use_option=True, content_dim=content_dim, **kwargs)
    raise ValueError(f"Unknown variant: {variant}")
