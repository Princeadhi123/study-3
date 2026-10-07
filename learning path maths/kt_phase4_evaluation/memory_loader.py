"""Zero-copy CPU embedding storage with the existing strict frozen loader.

Only storage ownership changes. Dataset encoding, vector values, trained
architecture and strict checkpoint checks remain the existing implementations.
"""
from pathlib import Path

import numpy as np
import torch

from phase4_common import paths
import frozen_model


class SharedEmbeddingDataset(frozen_model.KTSequenceDataset):
    def __init__(self, sequences_path, max_seq_len=400, text_embeddings_path=None):
        # Reuse the trained dataset's record loading and every encoding method.
        super().__init__(sequences_path, max_seq_len=max_seq_len,
                         text_embeddings_path=None)
        if text_embeddings_path is None:
            raise ValueError("Shared loader requires the frozen embedding table")
        with np.load(Path(text_embeddings_path), allow_pickle=True) as data:
            texts, vectors = data["texts"], data["vectors"]
        if vectors.dtype != np.float32 or vectors.ndim != 2:
            raise ValueError("Expected the original float32 embedding matrix")
        if texts.ndim != 1 or len(texts) != len(vectors):
            raise ValueError("Embedding texts/vector rows disagree")
        if not vectors.flags.c_contiguous:
            raise ValueError("Expected the original C-contiguous vectors")
        self.text_vectors = torch.from_numpy(vectors)
        self.text_to_row = {text: index for index, text in enumerate(texts)}
        self.content_dim = vectors.shape[1]
        if self.text_vectors.data_ptr() != vectors.__array_interface__["data"][0]:
            raise RuntimeError("Embedding storage unexpectedly copied")


def load_memory_efficient_model(sequences):
    # The swap is process-local and bounded to this single-threaded construction;
    # no Phase 1/2 files or running demo instance are modified.
    original = frozen_model.KTSequenceDataset
    frozen_model.KTSequenceDataset = SharedEmbeddingDataset
    try:
        return frozen_model.load_frozen_model(sequences=sequences, device="cpu")
    finally:
        frozen_model.KTSequenceDataset = original
