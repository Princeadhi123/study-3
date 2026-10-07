"""Shared vectors and inherited encoding equal the original copied dataset."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch

from memory_loader import SharedEmbeddingDataset, frozen_model, load_memory_efficient_model


class MemoryLoaderTests(unittest.TestCase):
    def test_all_encoded_fields_and_vectors_match_original_loader(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sequences, embeddings = root / "windows.jsonl.gz", root / "table.npz"
            event = {"skill": 2, "item": 3, "correct": 1, "rt": 2.0,
                     "attempt": 1, "content_text": "question", "selected_text": "answer",
                     "time_bin": 4, "split": "test_warm"}
            with gzip.open(sequences, "wt", encoding="utf-8") as stream:
                stream.write(json.dumps({"student_id": "fake", "events": [event]}) + "\n")
            vectors = np.arange(12, dtype=np.float32).reshape(3, 4)
            np.savez(embeddings, texts=np.array(["pad", "question", "answer"], dtype=object),
                     vectors=vectors)
            original = frozen_model.KTSequenceDataset(str(sequences), 4, str(embeddings))
            shared = SharedEmbeddingDataset(str(sequences), 4, str(embeddings))
            torch.testing.assert_close(original.text_vectors, shared.text_vectors,
                                       atol=0, rtol=0)
            self.assertEqual(original.text_to_row, shared.text_to_row)
            for key, value in original[0].items():
                torch.testing.assert_close(value, shared[0][key], atol=0, rtol=0)
            self.assertEqual(shared.text_vectors.data_ptr(),
                             shared.text_vectors.numpy().__array_interface__["data"][0])

    def test_strict_loader_alias_restored_even_on_failure(self):
        original = frozen_model.KTSequenceDataset
        with patch.object(frozen_model, "load_frozen_model", side_effect=RuntimeError("fake")):
            with self.assertRaises(RuntimeError):
                load_memory_efficient_model(Path("unused"))
        self.assertIs(frozen_model.KTSequenceDataset, original)


if __name__ == "__main__":
    unittest.main()
