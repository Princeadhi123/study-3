"""Fixed-instrument synthetic scenario and research-report regression checks."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scenario_report import compile_report, write_report
from scenario_runner import generate_responses, run_scenario
from session_store import SessionStore
from tests.helpers import make_bank


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_bank()
        self.bank["skill_names"]["sA"] = "Murtoluvut"
        self.bank["skill_names"]["sB"] = "Hinta"

    def rows(self, profile, seed=42):
        return generate_responses(self.bank, {"profile": profile, "seed": seed})

    def test_fixed_boundary_profiles(self):
        expected = {
            "all_correct": lambda r: True,
            "all_incorrect": lambda r: False,
            "alternating": lambda r: r["skill_attempt"] % 2 == 1,
            "first_half_correct_second_half_wrong": lambda r: r["position"] <= 20,
            "weak_fractions_only": lambda r: r["skill_id"] != "sA",
            "weak_price_only": lambda r: r["skill_id"] != "sB",
        }
        for name, policy in expected.items():
            with self.subTest(name=name):
                rows = self.rows(name)
                self.assertEqual(40, len(rows))
                self.assertTrue(all(r["correct"] == policy(r) for r in rows))
                self.assertEqual([q["question_id"] for q in self.bank["questions"]],
                                 [r["question_id"] for r in rows])
                self.assertEqual(10, len([r for r in rows if r["skill_id"] == "sA"]))

    def test_seeded_probabilities_and_uniform_wrong_options(self):
        self.assertEqual(self.rows("learning", 42), self.rows("learning", 42))
        self.assertNotEqual([r["correct"] for r in self.rows("learning", 42)],
                            [r["correct"] for r in self.rows("learning", 43)])
        learning = [r for r in self.rows("learning") if r["skill_id"] == "sA"]
        self.assertAlmostEqual(.4, learning[0]["true_probability"])
        self.assertAlmostEqual(.75, learning[-1]["true_probability"])
        fatigue = [r for r in self.rows("fatigue") if r["skill_id"] == "sA"]
        self.assertAlmostEqual(.75, fatigue[0]["true_probability"])
        self.assertAlmostEqual(.45, fatigue[-1]["true_probability"])
        self.assertTrue(all(abs(r["true_probability"] - 1 / 3) < 1e-6
                            for r in self.rows("guessing")))
        wrong = self.rows("all_incorrect")
        self.assertTrue(all(r["selected_index"] != self.bank["questions"][i]["answer_index"]
                            for i, r in enumerate(wrong)))
        self.assertEqual({1, 2}, {(r["selected_index"] -
                                  self.bank["questions"][i]["answer_index"]) % 3
                                 for i, r in enumerate(wrong)})

    def test_custom_probability_validation(self):
        spec = {"name": "weak_fractions_seed_42", "seed": 42,
                "skill_probabilities": {"sA": .2}, "default_probability": .75,
                "distractor_policy": "uniform_wrong"}
        rows = generate_responses(self.bank, spec)
        self.assertEqual(.2, rows[0]["true_probability"])
        self.assertEqual(.75, rows[1]["true_probability"])
        for change in ({"seed": True}, {"skill_probabilities": {"missing": .2}},
                       {"default_probability": 1.2}, {"distractor_policy": "next"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                generate_responses(self.bank, {**spec, **change})

    def test_service_report_determinism_and_private_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions")
            spec = {"profile": "first_half_correct_second_half_wrong", "seed": 42}
            result = run_scenario(self.bank, spec, store)
            self.assertEqual(20, result["observed"]["midpoint"]["teacher"]["total"]["correct"])
            self.assertEqual(20, result["observed"]["end"]["teacher"]["total"]["correct"])
            self.assertEqual("not_requested", result["conformal"]["status"])
            self.assertEqual("not_requested", result["graph"]["status"])
            self.assertEqual(result, run_scenario(self.bank, spec, store))
            report = compile_report([result])
            self.assertEqual(40, len(report["trace"]))
            self.assertEqual(8, len(report["summary"]))
            self.assertEqual(5, report["summary"][0]["n_items"])
            self.assertIsNone(report["summary"][0]["mean_kt_probability"])
            out = Path(tmp) / "research_report"
            write_report(report, out)
            self.assertEqual(report, json.loads((out / "scenario_report.json").read_text(
                encoding="utf-8")))
            with self.assertRaises(FileExistsError):
                write_report(report, out)

    def test_kt_conformal_and_graph_are_diagnostic_only(self):
        class Decision:
            prediction_set = (0, 1)
            status = type("Status", (), {"value": "UNCERTAIN_BEHAVIOR"})()

        class Gate:
            k = 10

            def item_decision(self, probability, regime):
                return Decision()

            def checkpoint(self, probabilities, sid, regime):
                return type("Result", (), {"to_dict": lambda self: {
                    "status": "UNCERTAIN_BEHAVIOR", "lower": .1,
                    "upper": .9, "n_items": len(probabilities), "regime": regime}})()

        class Graph:
            def foundational_for(self, sid, allow_weak_evidence):
                return [{"skill_id": "prerequisite"}] if sid == "sB" else []

        trace = {"p_correct_before_each_answer": [.6] * 40,
                 "coverage": {"unknown_item_ids": [self.bank["questions"][0]["item_id"]]}}
        with tempfile.TemporaryDirectory() as tmp, patch(
                "scenario_runner.trace_responses", return_value=trace):
            result = run_scenario(self.bank, {"profile": "all_correct"},
                                  SessionStore(Path(tmp)), device="cpu",
                                  gate=Gate(), graph=Graph())
        self.assertEqual("cold", result["conformal"]["items"][0]["regime"])
        self.assertEqual("cold", result["conformal"]["checkpoints"]["midpoint"]["sA"]["regime"])
        self.assertEqual("cold", result["conformal"]["checkpoints"]["end"]["sA"]["regime"])
        self.assertEqual("warm", result["conformal"]["checkpoints"]["end"]["sB"]["regime"])
        self.assertEqual(10, result["conformal"]["checkpoints"]["end"]["sA"]["n_items"])
        self.assertEqual("approximate_k5_not_calibrated_k10",
                         result["conformal"]["midpoint_status"])
        self.assertEqual([{"skill_id": "prerequisite"}],
                         result["graph"]["prerequisites"]["sB"])
        self.assertEqual(40, result["observed"]["end"]["teacher"]["total"]["correct"])
        report = compile_report([result])
        self.assertEqual(.6, report["trace"][0]["kt_probability"])
        self.assertEqual(.4, report["summary"][4]["mean_absolute_gap"])
        self.assertEqual("approximate_k5_not_calibrated_k10",
                         report["summary"][0]["conformal_calibration_status"])

    def test_reject_mixed_banks_and_duplicate_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            result = run_scenario(self.bank, {"profile": "all_correct"}, store)
            with self.assertRaises(ValueError):
                compile_report([result, result])
            other = {**result, "bank_sha256": "different",
                     "scenario": {**result["scenario"], "seed": 43}}
            with self.assertRaises(ValueError):
                compile_report([result, other])


if __name__ == "__main__":
    unittest.main()
