"""Tests for fresh-session frozen diagnostics (offline fakes only).

LiveDiagnostics is exercised with the existing FakeKT adapter double and
a small gate double; no real model files, credentials, or network are
used. These check software plumbing, not diagnostic validity.
"""
import copy
import unittest
from types import SimpleNamespace

import phase3_paths  # noqa: F401 -- installs the Phase 2 import path
from live_diagnostics import SCOPE_WARNING, LiveDiagnostics
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_kt_adapter import FakeKT


class FakeGate:
    """Minimal stand-in with the ConformalGate call surface."""

    def __init__(self, k):
        self.k = k
        self.checkpoints = []
        self.items = []

    def checkpoint(self, probabilities, skill_id, regime="warm"):
        self.checkpoints.append(
            {"skill_id": skill_id, "probabilities": list(probabilities),
             "regime": regime})
        gate = self
        count = len(probabilities)
        mean = sum(probabilities) / count

        class Result:
            def to_dict(self):
                return {"skill_id": skill_id, "n_items": count,
                        "point_estimate": mean, "lower": 0.0,
                        "upper": 1.0, "regime": regime,
                        "status": "mock_gate_label"}

        return Result()

    def item_decision(self, probability, regime):
        self.items.append({"probability": probability, "regime": regime})
        return SimpleNamespace(
            prediction_set=["correct"] if probability >= 0.5
            else ["incorrect"],
            status=SimpleNamespace(value="mock_item_label"))


def loaders(bank, gate_k=(5, 10), calls=None):
    def model_loader():
        if calls is not None:
            calls.append(1)
        return FakeKT(bank)

    def gate_loader():
        return {"midpoint": FakeGate(gate_k[0]), "end": FakeGate(gate_k[1])}

    return model_loader, gate_loader


def broken_loader():
    raise FileNotFoundError("no frozen model")


class LiveDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)

    def service(self, model_loader=None, gate_loader=None):
        if model_loader is None:
            model_loader, gate_loader = loaders(self.bank)
        return LiveDiagnostics(model_loader=model_loader,
                               gate_loader=gate_loader)

    def test_midpoint_twenty_probabilities_and_k5_rows(self):
        diagnostics = self.service()
        result = diagnostics.evaluate(
            self.bank, self.taxonomy, responses(self.bank, count=20))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["checkpoint"], "midpoint")
        self.assertEqual(result["answer_count"], 20)
        probabilities = result["kt"]["p_correct_before_each_answer"]
        self.assertEqual(len(probabilities), 20)
        skills = result["conformal"]["skills"]
        self.assertEqual(len(skills), 4)
        for sid, row in skills.items():
            self.assertEqual(row["n_items"], 5)
            indexes = [i for i, q in
                       enumerate(self.bank["questions"][:20])
                       if q["skill_id"] == sid]
            self.assertAlmostEqual(
                row["point_estimate"],
                sum(probabilities[i] for i in indexes) / 5)
        self.assertEqual(result["conformal"]["calibrated_k"], 5)
        self.assertFalse(result["used_for_student_advice"])

    def test_end_forty_probabilities_and_k10_rows(self):
        diagnostics = self.service()
        result = diagnostics.evaluate(
            self.bank, self.taxonomy, responses(self.bank, count=40))
        self.assertEqual(result["checkpoint"], "end")
        self.assertEqual(
            len(result["kt"]["p_correct_before_each_answer"]), 40)
        self.assertEqual(result["conformal"]["calibrated_k"], 10)
        for row in result["conformal"]["skills"].values():
            self.assertEqual(row["n_items"], 10)
        self.assertEqual(len(result["conformal"]["items"]), 40)

    def test_unknown_item_marks_cold_regime(self):
        vocab_source = copy.deepcopy(self.bank)
        self.bank["questions"][0]["item_id"] = "never-trained-item"
        taxonomy = make_taxonomy(self.bank)
        # Vocabulary built from the pre-mutation bank, so the new
        # item_id is genuinely unknown to the fake model.
        diagnostics = LiveDiagnostics(
            model_loader=lambda: FakeKT(vocab_source),
            gate_loader=lambda: {"midpoint": FakeGate(5),
                                 "end": FakeGate(10)})
        result = diagnostics.evaluate(
            self.bank, taxonomy, responses(self.bank, count=20))
        items = result["conformal"]["items"]
        self.assertEqual(items[0]["regime"], "cold")
        self.assertTrue(all(row["regime"] == "warm"
                            for row in items[1:]))
        cold_skills = [sid for sid, row in
                       result["conformal"]["skills"].items()
                       if row["regime"] == "cold"]
        self.assertEqual(cold_skills, ["sA"])

    def test_missing_model_is_unavailable_not_fabricated(self):
        diagnostics = self.service(model_loader=broken_loader,
                                   gate_loader=lambda: None)
        result = diagnostics.evaluate(
            self.bank, self.taxonomy, responses(self.bank, count=20))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["kt"]["status"], "unavailable")
        self.assertEqual(result["conformal"]["status"], "unavailable")
        self.assertNotIn("p_correct_before_each_answer",
                         result["kt"])
        self.assertEqual(diagnostics.state["status"], "unavailable")

    def test_model_loaded_once_across_evaluations(self):
        calls = []
        model_loader, gate_loader = loaders(self.bank, calls=calls)
        diagnostics = LiveDiagnostics(model_loader=model_loader,
                                      gate_loader=gate_loader)
        diagnostics.evaluate(self.bank, self.taxonomy,
                             responses(self.bank, count=20))
        diagnostics.evaluate(self.bank, self.taxonomy,
                             responses(self.bank, count=40))
        self.assertEqual(len(calls), 1)
        self.assertEqual(diagnostics.state["status"], "ready")

    def test_wrong_gate_calibration_rejected(self):
        model_loader, gate_loader = loaders(self.bank, gate_k=(5, 5))
        diagnostics = LiveDiagnostics(model_loader=model_loader,
                                      gate_loader=gate_loader)
        result = diagnostics.evaluate(
            self.bank, self.taxonomy, responses(self.bank, count=20))
        self.assertEqual(result["status"], "unavailable")

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
            model_loader=lambda: FakeKT(self.bank),
            gate_loader=lambda: {"midpoint": FakeGate(5),
                                 "end": FakeGate(10)})
        state = diagnostics.state
        self.assertEqual(state["status"], "not_loaded")
        self.assertEqual(state["scope_warning"], SCOPE_WARNING)


if __name__ == "__main__":
    unittest.main()
