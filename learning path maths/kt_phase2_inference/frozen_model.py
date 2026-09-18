"""L1 loader -- load the FROZEN Phase-1 variant-D model for forward passes only.

The architecture is imported from `kt_phase1/modeling/model.py` rather than
re-declared here, so what runs at inference is byte-identical to what was
trained. Nothing in this module trains, fine-tunes, or writes to Phase 1.

## Why this is strict instead of forgiving

`KTSequenceDataset` sets `content_dim = 0` when the text-embedding table is
absent (`dataset.py:44-51`). For a content-aware variant that is not a
graceful degradation -- it builds a structurally *different* model with no
`content_proj` / `option_proj` at all. Depending on load flags that either
raises a confusing key error or, if anyone ever relaxes `strict=`, produces a
model that silently ignores every question's content and still emits
plausible-looking probabilities. A calibration or knowledge-graph artifact
derived from that would be quietly, unrecoverably wrong.

So this module hard-fails on:

- a missing embeddings table (`text_embeddings_v2.npz`),
- an embeddings table whose dimensionality disagrees with the checkpoint's
  `content_proj.weight` (i.e. the wrong table was rsynced back),
- a run config whose variant is not `skill_item_content_option`,
- any state-dict key mismatch (`strict=True`, always).
"""
import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import torch

import paths

# Import the trained architecture itself, never a copy of it.
if str(paths.PHASE1_MODELING) not in sys.path:
    sys.path.insert(0, str(paths.PHASE1_MODELING))

from dataset import KTSequenceDataset, load_vocab, SPLIT_CODE  # noqa: E402
from model import build_model  # noqa: E402

VARIANT = "skill_item_content_option"

RSYNC_HINT = (
    "Variant D indexes a frozen sentence-embedding table for both the current\n"
    "  question's content and the previous step's selected answer. Copy it back\n"
    "  from LUMI, e.g.:\n"
    "    rsync -av lumi:/projappl/project_462001308/math_kt/embeddings/text_embeddings_v2.npz \\\n"
    "        kt_phase1/modeling/prepared_v2/"
)

# Fields `prepare_sequences_v2.py` emits that variant D actually consumes.
# `dataset.py` treats all three as optional for backwards compatibility with
# v1 sequences (`ev.get(...)` with a 0 / fallback default), which is correct
# for v1 but catastrophic here -- see check_sequence_schema.
REQUIRED_EVENT_FIELDS = ("content_text", "selected_text", "time_bin")

SEQUENCES_HINT = (
    "The sequences file must be the one variant D was TRAINED on, recorded in\n"
    "  runs/skill_item_content_option/config.json. Copy it back from LUMI:\n"
    "    rsync -av lumi:/scratch/project_462001308/math_kt/prepared_v2_w400_o0p25_g30_c8/"
    "{sequences.jsonl.gz,vocab.json,split_report.json} \\\n"
    "        kt_phase1/modeling/prepared_v2/"
)


def check_sequence_schema(dataset: KTSequenceDataset, sample_records: int = 50) -> None:
    """Refuse to run variant D on sequences that predate its input fields.

    `dataset.py` deliberately tolerates missing `content_text` /
    `selected_text` / `time_bin`, falling back to plain `text`, index 0 and
    bin 0 respectively, so that v1 sequences still load. For variant D that
    tolerance is a trap rather than a convenience:

    - `selected_text` missing => every previous-selection index becomes 0,
      so `option_proj` receives one constant vector for every interaction.
      D's entire distinguishing input is replaced by a constant, silently.
    - `content_text` missing => content is looked up from `text` (question
      only). The offered options vanish from the query, and most lookups
      miss the table and land on row 0 -- a real embedding of an unrelated
      question, not an "unknown" slot.
    - `time_bin` missing => `time_embed` sees bin 0 ("<60s") everywhere.

    None of this raises. The model produces confident, well-formed, entirely
    meaningless probabilities. Any calibration or graph derived from them
    would be wrong in a way that is invisible downstream, which is exactly
    the failure this whole module is built to prevent.
    """
    seen = set()
    for record in dataset.records[:sample_records]:
        for ev in record["events"]:
            seen.update(ev.keys())
        if seen.issuperset(REQUIRED_EVENT_FIELDS):
            return
    missing = [f for f in REQUIRED_EVENT_FIELDS if f not in seen]
    if missing:
        raise RuntimeError(
            f"Sequences are missing field(s) {missing} that variant D consumes.\n"
            f"  This file was built by an older prepare_sequences_v2.py and is NOT the "
            f"dataset the checkpoint was trained on.\n  {SEQUENCES_HINT}"
        )


class FrozenKT:
    """The deployed model: frozen weights, eval mode, no gradients, ever."""

    def __init__(self, model, content_vectors, dataset, skill_vocab, item_vocab,
                 config, checkpoint_path, device):
        self.model = model
        self.content_vectors = content_vectors
        self.dataset = dataset
        self.skill_vocab = skill_vocab
        self.item_vocab = item_vocab
        self.config = config
        self.checkpoint_path = checkpoint_path
        self.device = device
        # Reverse maps are what the knowledge graph and the orchestrator need
        # to talk about skills/items by their real identifiers.
        self.idx_to_skill = {v: k for k, v in skill_vocab.items()}
        self.idx_to_item = {v: k for k, v in item_vocab.items()}

    @torch.no_grad()
    def logits(self, batch: dict) -> torch.Tensor:
        batch = {k: v.to(self.device) for k, v in batch.items()}
        return self.model(batch, content_vectors=self.content_vectors)

    @torch.no_grad()
    def probs(self, batch: dict) -> torch.Tensor:
        return torch.sigmoid(self.logits(batch))

    def describe(self) -> str:
        n_params = sum(p.numel() for p in self.model.parameters())
        return (f"variant={VARIANT} checkpoint={self.checkpoint_path.name} "
                f"d_model={self.config['d_model']} n_layers={self.config['n_layers']} "
                f"content_dim={self.content_vectors.shape[1]} "
                f"params={n_params:,} device={self.device}")


def _content_dim_from_checkpoint(state: dict) -> int:
    """The content dimensionality the checkpoint was actually trained with.

    `content_proj` is `nn.Linear(content_dim, d_model)`, so its weight is
    (d_model, content_dim). Reading it back is the only way to detect that a
    *different* embedding table (e.g. LaBSE's 768-d instead of the
    MiniLM 384-d one) was rsynced into place.
    """
    w = state.get("content_proj.weight")
    if w is None:
        raise RuntimeError(
            f"Checkpoint has no `content_proj.weight` -- this is not a content-aware "
            f"variant. Expected a {VARIANT} checkpoint."
        )
    return int(w.shape[1])


def load_frozen_model(checkpoint: Optional[Path] = None,
                      sequences: Optional[Path] = None,
                      device: str = "cpu",
                      max_seq_len: Optional[int] = None) -> FrozenKT:
    checkpoint = Path(checkpoint) if checkpoint else paths.CHECKPOINT
    sequences = Path(sequences) if sequences else paths.SEQUENCES

    paths.require(checkpoint, "Train/rsync Phase 1's runs/skill_item_content_option/ first.")
    paths.require(paths.RUN_CONFIG)
    paths.require(paths.VOCAB)
    paths.require(sequences)
    paths.require(paths.TEXT_EMBEDDINGS, RSYNC_HINT)

    config = json.loads(paths.RUN_CONFIG.read_text(encoding="utf-8"))
    if config.get("variant") != VARIANT:
        raise RuntimeError(
            f"{paths.RUN_CONFIG} describes variant {config.get('variant')!r}, not {VARIANT!r}. "
            f"Phase 2 deploys variant D only."
        )
    if max_seq_len is None:
        max_seq_len = config["max_seq_len"]
    elif max_seq_len != config["max_seq_len"]:
        # The window length is baked into pos_embed's size; a mismatch would
        # either fail to load or silently truncate history.
        raise RuntimeError(
            f"max_seq_len={max_seq_len} disagrees with the trained window "
            f"({config['max_seq_len']}). They are the same window -- see Phase 1's "
            f"README, 'Adjusting scale'."
        )

    skill_vocab, item_vocab = load_vocab(str(paths.VOCAB))

    dataset = KTSequenceDataset(str(sequences), max_seq_len=max_seq_len,
                                text_embeddings_path=str(paths.TEXT_EMBEDDINGS))
    if dataset.content_dim == 0:
        raise RuntimeError(
            f"{paths.TEXT_EMBEDDINGS} exists but yielded content_dim=0.\n  {RSYNC_HINT}"
        )
    check_sequence_schema(dataset)

    state = torch.load(checkpoint, map_location="cpu")
    ckpt_dim = _content_dim_from_checkpoint(state)
    if ckpt_dim != dataset.content_dim:
        raise RuntimeError(
            f"Embedding table dimensionality mismatch: {paths.TEXT_EMBEDDINGS.name} is "
            f"{dataset.content_dim}-d but the checkpoint was trained on {ckpt_dim}-d vectors. "
            f"The wrong table was copied back -- variant D was trained with "
            f"{config.get('text_embeddings')}."
        )

    model = build_model(
        VARIANT, n_skills=len(skill_vocab), n_items=len(item_vocab),
        content_dim=dataset.content_dim,
        d_model=config["d_model"], n_heads=config["n_heads"],
        n_layers=config["n_layers"], dropout=config["dropout"],
        max_seq_len=max_seq_len,
        use_time_embeddings=config.get("use_time_embeddings", True),
    )
    model.load_state_dict(state, strict=True)  # never relax this
    model.eval()
    for p in model.parameters():
        p.requires_grad_(False)
    model.to(device)

    content_vectors = dataset.text_vectors.to(device)
    return FrozenKT(model, content_vectors, dataset, skill_vocab, item_vocab,
                    config, checkpoint, device)


def embedding_coverage(dataset: KTSequenceDataset, max_records: int = 2000) -> dict:
    """Fraction of events whose text actually resolves in the embedding table.

    `dataset._content_index` returns 0 on a miss, and row 0 is a real vector
    (the lexicographically first text), not a reserved "unknown" slot -- so a
    mismatched table degrades into confidently wrong content features with no
    error anywhere. A near-1.0 hit rate is the check that the table copied
    back is the one variant D was trained on.
    """
    hits = misses = sel_hits = sel_misses = 0
    for record in dataset.records[:max_records]:
        for ev in record["events"]:
            text = ev.get("content_text") or ev.get("text")
            if text in dataset.text_to_row:
                hits += 1
            else:
                misses += 1
            sel = ev.get("selected_text")
            if sel is None:
                continue
            if sel in dataset.text_to_row:
                sel_hits += 1
            else:
                sel_misses += 1
    total = hits + misses
    sel_total = sel_hits + sel_misses
    return {
        "records_sampled": min(max_records, len(dataset.records)),
        "content_hit_rate": hits / total if total else 0.0,
        "content_misses": misses,
        "selected_hit_rate": sel_hits / sel_total if sel_total else 0.0,
        "selected_misses": sel_misses,
    }


if __name__ == "__main__":
    kt = load_frozen_model()
    print(kt.describe())
    print(json.dumps(embedding_coverage(kt.dataset), indent=2))
