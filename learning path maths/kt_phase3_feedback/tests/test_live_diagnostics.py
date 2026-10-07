"""Tests for fresh-session frozen diagnostics (offline fakes only).

LiveDiagnostics is exercised with the existing FakeKT adapter double;
no real model files, credentials, or network are
used. These check software plumbing, not diagnostic validity.
"""
import copy
import unittest
from unittest.mock import patch

import phase3_paths  # noqa: F401 -- installs the Phase 2 import path
from live_diagnostics import SCOPE_WARNING, LiveDiagnostics
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_kt_adapter import FakeKT


def loader(bank, calls=None):
    def model_loader():
        if calls is not None:
            calls.append(1)
        return FakeKT(bank)

    return model_loader


def broken_loader():
    raise FileNotFoundError("no frozen model")


class LiveDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)

    def service(self, model_loader=None):
        if model_loader is None:
            model_loader = loader(self.bank)
        return LiveDiagnostics(model_loader=model_loader)

    def test_midpoint_twenty_probabilities_without_calibration(self):
        diagnostics = self.service()
        result = diagnostics.evaluate(
            self.bank, self.taxonomy, responses(self.bank, count=20))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["checkpoint"], "midpoint")
        self.assertEqual(result["answer_count"], 20)
        probabilities = result["kt"]["p_correct_before_each_answer"]
        self.assertEqual(len(probabilities), 20)
        self.assertNotIn("conformal", result)
        self.assertEqual(len(result["kt"]["items"]), 20)
        self.assertFalse(result["used_for_student_advice"])

    def test_end_forty_probabilities_without_calibration(self):
        diagnostics = self.service()
        result = diagnostics.evaluate(
            self.bank, self.taxonomy, responses(self.bank, count=40))
        self.assertEqual(result["checkpoint"], "end")
        self.assertEqual(
            len(result["kt"]["p_correct_before_each_answer"]), 40)
        self.assertNotIn("conformal", result)
        self.assertEqual(len(result["kt"]["items"]), 40)

    def test_unknown_item_marks_cold_regime(self):
        vocab_source = copy.deepcopy(self.bank)
        self.bank["questions"][0]["item_id"] = "never-trained-item"
        taxonomy = make_taxonomy(self.bank)
        # Vocabulary built from the pre-mutation bank, so the new
        # item_id is genuinely unknown to the fake model.
        diagnostics = LiveDiagnostics(
            model_loader=lambda: FakeKT(vocab_source))
        result = diagnostics.evaluate(
            self.bank, taxonomy, responses(self.bank, count=20))
        items = result["kt"]["items"]
        self.assertEqual(items[0]["regime"], "cold")
        self.assertTrue(all(row["regime"] == "warm"
                            for row in items[1:]))
        self.assertEqual(result["kt"]["coverage"]["unknown_item_count"], 1)

    def test_missing_model_is_unavailable_not_fabricated(self):
        diagnostics = self.service(model_loader=broken_loader)
        result = diagnostics.evaluate(
            self.bank, self.taxonomy, responses(self.bank, count=20))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["kt"]["status"], "unavailable")
        self.assertNotIn("conformal", result)
        self.assertNotIn("p_correct_before_each_answer",
                         result["kt"])
        self.assertEqual(diagnostics.state["status"], "unavailable")

    def test_model_loaded_once_across_evaluations(self):
        calls = []
        diagnostics = LiveDiagnostics(model_loader=loader(self.bank, calls=calls))
        diagnostics.evaluate(self.bank, self.taxonomy,
                             responses(self.bank, count=20))
        diagnostics.evaluate(self.bank, self.taxonomy,
                             responses(self.bank, count=40))
        self.assertEqual(len(calls), 1)
        self.assertEqual(diagnostics.state["status"], "ready")

    def test_injected_model_never_loads_calibration_modules(self):
        diagnostics = self.service()
        with patch("live_diagnostics.importlib.import_module",
                   side_effect=AssertionError("no extra model/calibration imports")):
            self.assertEqual(diagnostics.evaluate(
                self.bank, self.taxonomy, responses(self.bank, count=20))["status"], "ready")

    def test_graph_matches_observed_responses(self):
        diagnostics = self.service()
        rows = responses(self.bank, correct=True, count=20)
        result = diagnostics.evaluate(
            self.bank, self.taxonomy, rows)
        graph = result["graph"]
        self.assertEqual(graph["checkpoint"], "midpoint")
        self.assertEqual(sum(t["correct"] for t in graph["topics"]), 20)
        self.assertEqual(sum(t["out_of"] for t in graph["topics"]), 20)
        self.assertEqual(
            graph["relation"], "is_part_of_not_prerequisite")
        self.assertIn("Research diagnostics only",
                      result["scope_warning"])

    def test_state_is_nonblocking_property(self):
        diagnostics = LiveDiagnostics(
            model_loader=lambda: FakeKT(self.bank))
        state = diagnostics.state
        self.assertEqual(state["status"], "not_loaded")
        self.assertEqual(state["scope_warning"], SCOPE_WARNING)


if __name__ == "__main__":
    unittest.main()
