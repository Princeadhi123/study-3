"""PyTorch Dataset for the KT sequences produced by prepare_sequences.py.

Each student becomes one fixed-length, causally-ordered sequence (most recent
`max_seq_len` interactions). At position i, the model is given features of
interaction i-1 (skill/item/correctness/response-time/attempt of the PREVIOUS
step) plus the "query" (skill/item/content of the CURRENT step) and must
predict the current step's correctness. This is the standard DKT/SAKT-style
formulation and lets a single sequence carry train/val/test positions
together (with per-position split labels used to mask the loss/metrics).
"""
import gzip
import json
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import Dataset

SPLIT_CODE = {
    "train": 0, "val": 1, "test_warm": 2, "test_cold_item": 3, "skip_cold_in_train": 4,
    # Overlapping lead-in events from prepare_sequences.py's window chunking for
    # students with more than max_seq_len interactions: real history the model
    # can attend to, but excluded from loss/eval (same treatment as
    # skip_cold_in_train -- present, but never a train or eval target).
    "context": 5,
}
PAD_SPLIT = -1


class KTSequenceDataset(Dataset):
    def __init__(self, sequences_path, max_seq_len=400, text_embeddings_path=None):
        self.max_seq_len = max_seq_len
        self.records = []
        with gzip.open(sequences_path, "rt", encoding="utf-8") as f:
            for line in f:
                record = json.loads(line)
                if record["events"]:
                    self.records.append(record)

        self.text_to_row = {}
        self.text_vectors = None
        if text_embeddings_path and Path(text_embeddings_path).exists():
            data = np.load(text_embeddings_path, allow_pickle=True)
            texts, vectors = data["texts"], data["vectors"]
            self.text_vectors = torch.tensor(vectors, dtype=torch.float32)
            self.text_to_row = {t: i for i, t in enumerate(texts)}
            self.content_dim = vectors.shape[1]
        else:
            self.content_dim = 0

    def __len__(self):
        return len(self.records)

    def _content_index(self, text):
        if self.text_vectors is None:
            return 0
        return self.text_to_row.get(text, 0)

    def __getitem__(self, idx):
        # Right-padding is required, not just a style choice: with a causal
        # mask, a query can only attend to keys at or before its own position.
        # If padding were on the LEFT, position 0 could be a pad token whose
        # only allowed key (itself) is also masked out by the padding mask,
        # giving a fully-masked attention row -> softmax NaN -> that NaN then
        # poisons later positions in the next encoder layer via a 0-weight *
        # NaN-value product. Right-padding guarantees every real position's
        # causal window contains only real (unmasked) earlier positions.
        #
        # Each record is already <= max_seq_len events long (prepare_sequences.py
        # splits long students into multiple overlapping windows rather than
        # producing over-length records), so this slice is normally a no-op.
        # It's kept as a defensive fallback -- if this `max_seq_len` doesn't
        # match the one used to build `sequences_path`, this silently
        # truncates instead of erroring, so always pass matching values to
        # prepare_sequences.py and train.py.
        events = self.records[idx]["events"][-self.max_seq_len:]
        n = len(events)

        skill = np.zeros(self.max_seq_len, dtype=np.int64)
        item = np.zeros(self.max_seq_len, dtype=np.int64)
        correct = np.zeros(self.max_seq_len, dtype=np.float32)
        rt = np.zeros(self.max_seq_len, dtype=np.float32)
        rt_mask = np.zeros(self.max_seq_len, dtype=np.float32)
        attempt = np.ones(self.max_seq_len, dtype=np.float32)
        content_idx = np.zeros(self.max_seq_len, dtype=np.int64)
        split = np.full(self.max_seq_len, PAD_SPLIT, dtype=np.int64)
        attn_mask = np.zeros(self.max_seq_len, dtype=np.float32)

        for i, ev in enumerate(events):
            pos = i
            skill[pos] = ev["skill"]
            item[pos] = ev["item"]
            correct[pos] = float(ev["correct"])
            if ev["rt"] is not None and ev["rt"] >= 0:
                # log1p compresses the long tail of response times into a stable range.
                rt[pos] = np.log1p(ev["rt"])
                rt_mask[pos] = 1.0
            attempt[pos] = float(ev.get("attempt") or 1.0)
            content_idx[pos] = self._content_index(ev.get("text"))
            split[pos] = SPLIT_CODE.get(ev["split"], PAD_SPLIT)
            attn_mask[pos] = 1.0

        return {
            "skill": torch.from_numpy(skill),
            "item": torch.from_numpy(item),
            "correct": torch.from_numpy(correct),
            "rt": torch.from_numpy(rt),
            "rt_mask": torch.from_numpy(rt_mask),
            "attempt": torch.from_numpy(attempt),
            "content_idx": torch.from_numpy(content_idx),
            "split": torch.from_numpy(split),
            "attn_mask": torch.from_numpy(attn_mask),
        }


def load_vocab(vocab_path):
    with open(vocab_path, encoding="utf-8") as f:
        data = json.load(f)
    return data["skill_vocab"], data["item_vocab"]
