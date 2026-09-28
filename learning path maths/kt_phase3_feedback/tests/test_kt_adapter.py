"""Tests for conversion from submitted MCQ rows to variant-D batches."""
import unittest
from types import SimpleNamespace

import torch

import phase3_paths  # noqa: F401 -- installs the Phase 2 import path
from kt_adapter import coverage_report, response_events, trace_responses
from tests.helpers import make_bank, responses


class FakeKT:
    def __init__(self, bank):
        self.skill_vocab = {skill: index + 1
                            for index, skill in enumerate(bank["skill_names"])}
        self.item_vocab = {"__UNK__": 1}
        self.item_vocab.update({q["item_id"]: index + 2
                                for index, q in enumerate(bank["questions"])})
        texts = {q["content_text"] for q in bank["questions"]}
        texts.update(option for q in bank["questions"] for option in q["options"])
        self.dataset = SimpleNamespace(
            text_to_row={text: index for index, text in enumerate(sorted(texts))})
        self.batch = None

    def probs(self, batch):
        self.batch = batch
        return torch.linspace(0.1, 0.9, batch["skill"].shape[1]).unsqueeze(0)


class KTAdapterTests(unittest.TestCase):
    def test_trace_twenty_responses(self):
        bank = make_bank()
        kt = FakeKT(bank)
        trace = trace_responses(bank, responses(bank, count=20), kt=kt)
        self.assertEqual(len(trace["p_correct_before_each_answer"]), 20)
        self.assertEqual(kt.batch["skill"].shape, (1, 20))
        self.assertTrue(torch.equal(kt.batch["rt_mask"],
                                    torch.zeros(1, 20)))
        self.assertEqual(trace["coverage"]["missing_option_values"], [])

    def test_missing_option_embedding_is_rejected(self):
        bank = make_bank()
        kt = FakeKT(bank)
        bank["questions"][0]["options"][0] = "never embedded"
        coverage = coverage_report(bank, kt)
        self.assertEqual(coverage["missing_option_values"], ["never embedded"])
        with self.assertRaisesRegex(ValueError, "not KT-text-compatible"):
            trace_responses(bank, responses(bank, count=20), kt=kt)

    def test_unknown_item_maps_to_unk(self):
        bank = make_bank()
        kt = FakeKT(bank)
        bank["questions"][0]["item_id"] = "new-cold-item"
        trace_responses(bank, responses(bank, count=20), kt=kt)
        self.assertEqual(int(kt.batch["item"][0, 0]), 1)

    def test_events_derive_selected_text_and_correctness(self):
        bank = make_bank()
        events = response_events(bank, responses(bank, count=20))
        first = events[0]
        self.assertEqual(first["selected_text"], "alpha")
        self.assertTrue(first["correct"])
        self.assertEqual(len(events), 20)


if __name__ == "__main__":
    unittest.main()
