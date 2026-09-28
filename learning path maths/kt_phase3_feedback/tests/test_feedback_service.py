"""Tests for observed-feed plus optional model-estimate composition."""
import unittest
from types import SimpleNamespace

import torch

import phase3_paths  # noqa: F401 -- installs the Phase 2 import path
from feedback_service import checkpoint_result
from tests.helpers import make_bank, responses


class FakeKT:
    def __init__(self, bank):
        self.skill_vocab = {s: i + 1 for i, s in enumerate(bank["skill_names"])}
        self.item_vocab = {"__UNK__": 1}
        texts = {q["content_text"] for q in bank["questions"]}
        texts.update(o for q in bank["questions"] for o in q["options"])
        self.dataset = SimpleNamespace(
            text_to_row={text: i for i, text in enumerate(sorted(texts))})

    def probs(self, batch):
        return torch.zeros(1, batch["skill"].shape[1])


class FeedbackServiceTests(unittest.TestCase):
    def test_observed_only(self):
        bank = make_bank()
        result = checkpoint_result(bank, responses(bank, count=20))
        self.assertEqual(result["observed_feed"]["checkpoint"], "midpoint")
        self.assertEqual(result["model_estimate"], {"status": "not_requested"})

    def test_optional_kt_estimate_is_separate(self):
        bank = make_bank()
        result = checkpoint_result(
            bank, responses(bank, count=20), include_kt=True,
            kt=FakeKT(bank))
        self.assertEqual(result["model_estimate"]["model_status"],
                         "uncalibrated_for_20_40_question_feed")
        self.assertNotIn("answer_index", str(result))


if __name__ == "__main__":
    unittest.main()
