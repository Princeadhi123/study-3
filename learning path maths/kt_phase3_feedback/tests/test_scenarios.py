"""Fixed-instrument synthetic scenario and research-report regression checks."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scenario_runner
from scenario_report import compile_report, write_report
from scenario_runner import generate_responses, run_scenario
from session_store import SessionStore
from tests.helpers import make_bank, make_taxonomy


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

    def test_subtopic_research_report_has_observed_counts_without_conformal(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_scenario(self.bank, {"profile": "all_correct", "seed": 11},
                                  SessionStore(Path(tmp) / "sessions"),
                                  taxonomy=make_taxonomy(self.bank))
            report = compile_report([result])
            self.assertEqual(8, len(report["subtopics"]))
            self.assertEqual({"midpoint": 20, "end": 40}, {
                label: sum(row["n_items"] for row in report["subtopics"]
                           if row["checkpoint"] == label)
                for label in ("midpoint", "end")})
            self.assertEqual(40, sum(row["observed_correct"]
                                     for row in report["subtopics"]
                                     if row["checkpoint"] == "end"))
            self.assertNotIn("conformal", json.dumps(report["subtopics"]))
            out = Path(tmp) / "report"
            write_report(report, out)
            self.assertEqual(9, len((out / "subtopic_summary.csv").read_text(
                encoding="utf-8").splitlines()))

    def test_kt_conformal_and_graph_are_diagnostic_only(self):
        class Decision:
            prediction_set = (0, 1)
            status = type("Status", (), {"value": "UNCERTAIN_BEHAVIOR"})()

        class Gate:
            k = 10

            def item_decision(self, probability, regime):
                return Decision()

            def checkpoint(self, probabilities, sid, regime):
                status = ("CONFIDENT_STRUGGLE" if sid == "sB"
                          and len(probabilities) == 10 else "UNCERTAIN_BEHAVIOR")
                return type("Result", (), {"to_dict": lambda self: {
                    "status": status, "lower": .1,
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
        routing = result["graph"]["routing"]
        self.assertEqual("exploratory_only_researcher_only", routing["status"])
        self.assertEqual("end", routing["checkpoint"])
        flagged = routing["recommendations"]["sB"]
        self.assertEqual("CONFIDENT_STRUGGLE", flagged["checkpoint_status"])
        self.assertEqual("DIRECT_SKILL_REVIEW", flagged["recommendation_type"])
        self.assertIsNone(flagged["prerequisite_skill_id"])
        self.assertIsNone(flagged["prerequisite_skill_name"])
        self.assertEqual("No grounded prerequisite candidate; review Hinta "
                         "directly.", flagged["message"])
        for sid in ("sA", "sC", "sD"):
            self.assertEqual("NONE", routing["recommendations"][sid]
                             ["recommendation_type"])
        self.assertNotIn("routing", json.dumps(result["observed"]))
        self.assertEqual(40, result["observed"]["end"]["teacher"]["total"]["correct"])
        report = compile_report([result])
        self.assertEqual(.6, report["trace"][0]["kt_probability"])
        self.assertEqual(.4, report["summary"][4]["mean_absolute_gap"])
        self.assertEqual("approximate_k5_not_calibrated_k10",
                         report["summary"][0]["conformal_calibration_status"])

    def test_paired_historical_gates_use_matching_block_sizes(self):
        class Gate:
            k = 10

            def item_decision(self, probability, regime):
                return type("Item", (), {"prediction_set": (0, 1),
                                         "status": type("Status", (), {"value": "UNCERTAIN_BEHAVIOR"})()})()

            def checkpoint(self, probabilities, sid, regime):
                return type("Result", (), {"to_dict": lambda self: {
                    "n_items": len(probabilities), "regime": regime,
                    "status": "END_GATE"}})()

        class MidpointGate(Gate):
            k = 5

            def checkpoint(self, probabilities, sid, regime):
                return type("Result", (), {"to_dict": lambda self: {
                    "n_items": len(probabilities), "regime": regime,
                    "status": "MIDPOINT_GATE"}})()

        trace = {"p_correct_before_each_answer": [.6] * 40,
                 "coverage": {"unknown_item_ids": []}}
        with tempfile.TemporaryDirectory() as tmp, patch(
                "scenario_runner.trace_responses", return_value=trace):
            result = run_scenario(self.bank, {"profile": "all_correct"},
                                  SessionStore(Path(tmp)), device="cpu",
                                  gate=Gate(), midpoint_gate=MidpointGate())
            with self.assertRaisesRegex(ValueError, "paired k=5"):
                run_scenario(self.bank, {"profile": "all_correct"},
                             SessionStore(Path(tmp)), device="cpu",
                             gate=Gate(), midpoint_gate=Gate())
        self.assertEqual(5, result["conformal"]["midpoint_calibrated_k"])
        self.assertEqual("historical_k5_not_validated_for_fixed_bank",
                         result["conformal"]["midpoint_status"])
        self.assertEqual("MIDPOINT_GATE",
                         result["conformal"]["checkpoints"]["midpoint"]["sA"]["status"])
        self.assertEqual("END_GATE",
                         result["conformal"]["checkpoints"]["end"]["sA"]["status"])
        self.assertEqual("researcher_only_fixed_40_question_bank",
                         compile_report([result])["scope"])
        self.assertNotIn("conformal", json.dumps(result["observed"]["end"]["student"]))

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


class SavedEvaluationReplayTests(unittest.TestCase):
    def fixture(self):
        bank = make_bank()
        taxonomy = make_taxonomy(bank)
        with tempfile.TemporaryDirectory() as tmp:
            result = run_scenario(bank, {"profile": "all_correct"},
                                  SessionStore(Path(tmp)), taxonomy=taxonomy)
        case = {"name": "all_correct", "group": "deterministic", "seed": 42,
                "responses": result["responses"],
                "observed": {c: result["observed"][c]["student"] for c in ("midpoint", "end")},
                "selector_context": {"midpoint": {}, "end": {}},
                "kt_pre_answer_probability": [.5] * 40, "kt_coverage": {}}
        case["observed"]["end"]["message"] = "previous feedback"
        return bank, taxonomy, {"schema": "phase3_fixed40_comparison_v1",
                                "bank_sha256": result["bank_sha256"], "scenarios": [case]}

    def test_replay_preserves_saved_model_data_and_original(self):
        from evaluate_scenarios import replay_feedback
        bank, taxonomy, original = self.fixture()
        before = copy.deepcopy(original)
        with patch("evaluate_scenarios.trace_responses", side_effect=AssertionError("must not run KT")):
            updated = replay_feedback(original, bank, taxonomy)
        self.assertEqual(before, original)
        case = updated["scenarios"][0]
        self.assertEqual("previous feedback", case["observed_before"]["end"]["message"])
        self.assertIn("You answered every question correctly", case["observed"]["end"]["message"])
        self.assertEqual(before["scenarios"][0]["responses"], case["responses"])
        self.assertEqual([.5] * 40, case["kt_pre_answer_probability"])
        self.assertNotIn("total_correct", case["selector_context"]["midpoint"]["observed_performance"])
        original["bank_sha256"] = "different"
        with self.assertRaises(ValueError):
            replay_feedback(original, bank, taxonomy)

    def test_comparison_reports_duplicates_and_refuses_invalid_predictions(self):
        from compare_scenarios import summarize
        bank, taxonomy, original = self.fixture()
        original["scenarios"] = [copy.deepcopy(original["scenarios"][0]) for _ in range(54)]
        for i, case in enumerate(original["scenarios"]):
            case["name"] = f"case_{i}"
        for case, name in zip(original["scenarios"][-2:], ("wrong_option_1", "wrong_option_2")):
            case.update(name=name, group="distractor")
        summary, feedback, subtopics, skills, diagnostic = summarize(original, bank)
        self.assertEqual((54, 108, 1), (len(summary), len(feedback),
                                        diagnostic["unique_response_sequences"]))
        self.assertEqual("case_0", summary[1]["duplicate_response_sequence_of"])
        for invalid in (float("nan"), -0.1, 1.1, True):
            broken = copy.deepcopy(original)
            broken["scenarios"][0]["kt_pre_answer_probability"][0] = invalid
            with self.subTest(probability=invalid), self.assertRaises(ValueError):
                summarize(broken, bank)
        broken = copy.deepcopy(original)
        broken["scenarios"][0]["responses"][0]["selected_index"] = True
        with self.assertRaises(ValueError):
            summarize(broken, bank)


class _RoutingGate:
    def __init__(self, end_statuses, midpoint_statuses=None, k=10):
        self.k = k
        self._end = dict(end_statuses)
        self._midpoint = dict(midpoint_statuses or {})

    def item_decision(self, probability, regime):
        return type("Item", (), {"prediction_set": (0, 1),
                                 "status": type("Status", (), {"value": "UNCERTAIN_BEHAVIOR"})()})()

    def checkpoint(self, probabilities, sid, regime):
        statuses = self._end if len(probabilities) == 10 else self._midpoint
        status = statuses.get(sid, "UNCERTAIN_BEHAVIOR")
        return type("Result", (), {"to_dict": lambda self: {
            "n_items": len(probabilities), "regime": regime,
            "status": status}})()


class _RoutingGraph:
    def __init__(self, candidates):
        self._candidates = candidates

    def foundational_for(self, sid, allow_weak_evidence):
        return self._candidates.get(sid, [])


GROUNDED_S_B = {"skill_id": "sB", "skill_name": "Peruslaskut",
                "relation": "prerequisite", "evidence": "grounded",
                "depth": 1, "score": .9, "edge_weight": .9}


class PrivateRoutingTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_bank()
        self.bank["skill_names"]["sA"] = "Murtoluvut"
        self.bank["skill_names"]["sB"] = "Hinta"

    def run_private(self, gate=None, graph=None):
        trace = {"p_correct_before_each_answer": [.6] * 40,
                 "coverage": {"unknown_item_ids": []}}
        with tempfile.TemporaryDirectory() as tmp, patch(
                "scenario_runner.trace_responses", return_value=trace):
            kwargs = {"device": "cpu"} if gate is not None else {}
            return run_scenario(self.bank, {"profile": "all_correct"},
                                SessionStore(Path(tmp)),
                                gate=gate, graph=graph, **kwargs)

    def test_k10_end_statuses_map_to_private_recommendations(self):
        gate = _RoutingGate({"sA": "CONFIDENT_STRUGGLE",
                             "sB": "MASTERY_SAFE",
                             "sC": "UNCERTAIN_BEHAVIOR"})
        graph = _RoutingGraph({"sA": [dict(GROUNDED_S_B)]})
        result = self.run_private(gate, graph)
        routing = result["graph"]["routing"]
        self.assertEqual("exploratory_only_researcher_only", routing["status"])
        self.assertEqual("end", routing["checkpoint"])
        recs = routing["recommendations"]
        self.assertEqual(list(self.bank["skill_names"]), list(recs))
        flagged = recs["sA"]
        self.assertEqual("sA", flagged["target_skill_id"])
        self.assertEqual("Murtoluvut", flagged["target_skill_name"])
        self.assertEqual("CONFIDENT_STRUGGLE", flagged["checkpoint_status"])
        self.assertEqual("PREREQUISITE_REVIEW", flagged["recommendation_type"])
        self.assertEqual("sB", flagged["prerequisite_skill_id"])
        self.assertEqual("Peruslaskut", flagged["prerequisite_skill_name"])
        self.assertEqual("Exploratory prerequisite review candidate: "
                         "Peruslaskut (for Murtoluvut).", flagged["message"])
        for sid, status in (("sB", "MASTERY_SAFE"),
                            ("sC", "UNCERTAIN_BEHAVIOR"),
                            ("sD", "UNCERTAIN_BEHAVIOR")):
            rec = recs[sid]
            self.assertEqual(status, rec["checkpoint_status"])
            self.assertEqual("NONE", rec["recommendation_type"])
            self.assertIsNone(rec["prerequisite_skill_id"])
            self.assertIsNone(rec["prerequisite_skill_name"])
            self.assertIsNone(rec["message"])

    def test_ranked_selection_picks_first_grounded_candidate(self):
        candidates = [
            {"skill_id": "sX", "skill_name": "Weak edge",
             "relation": "prerequisite", "evidence": "weak", "depth": 1},
            dict(GROUNDED_S_B),
            {"skill_id": "sC", "skill_name": "Remote edge",
             "relation": "prerequisite", "evidence": "grounded", "depth": 2},
        ]
        result = self.run_private(_RoutingGate({"sA": "CONFIDENT_STRUGGLE"}),
                          _RoutingGraph({"sA": candidates}))
        rec = result["graph"]["routing"]["recommendations"]["sA"]
        self.assertEqual("PREREQUISITE_REVIEW", rec["recommendation_type"])
        self.assertEqual("sB", rec["prerequisite_skill_id"])

    def test_ineligible_candidates_fall_back_to_direct_review(self):
        ineligible = [
            [],
            [{"skill_id": "sB"}],
            [{**GROUNDED_S_B, "evidence": "weak"}],
            [{**GROUNDED_S_B, "relation": "curriculum_order"}],
            [{**GROUNDED_S_B, "skill_id": "sA"}],
            [{**GROUNDED_S_B, "skill_id": "  "}],
            [{**GROUNDED_S_B, "skill_name": ""}],
            [{**GROUNDED_S_B, "skill_name": None}],
            [{**GROUNDED_S_B, "depth": 3}],
            ["not-a-dict"],
        ]
        for candidates in ineligible:
            with self.subTest(candidates=candidates):
                result = self.run_private(_RoutingGate({"sA": "CONFIDENT_STRUGGLE"}),
                                  _RoutingGraph({"sA": candidates}))
                rec = result["graph"]["routing"]["recommendations"]["sA"]
                self.assertEqual("CONFIDENT_STRUGGLE", rec["checkpoint_status"])
                self.assertEqual("DIRECT_SKILL_REVIEW",
                                 rec["recommendation_type"])
                self.assertIsNone(rec["prerequisite_skill_id"])
                self.assertIsNone(rec["prerequisite_skill_name"])
                self.assertEqual("No grounded prerequisite candidate; review "
                                 "Murtoluvut directly.", rec["message"])

    def test_midpoint_struggle_alone_never_routes(self):
        gate = _RoutingGate({"sA": "MASTERY_SAFE"},
                            midpoint_statuses={"sA": "CONFIDENT_STRUGGLE"})
        result = self.run_private(gate, _RoutingGraph({"sA": [dict(GROUNDED_S_B)]}))
        self.assertEqual("CONFIDENT_STRUGGLE", result["conformal"]
                         ["checkpoints"]["midpoint"]["sA"]["status"])
        rec = result["graph"]["routing"]["recommendations"]["sA"]
        self.assertEqual("MASTERY_SAFE", rec["checkpoint_status"])
        self.assertEqual("NONE", rec["recommendation_type"])
        self.assertIsNone(rec["message"])

    def test_non_k10_gate_fails_closed(self):
        result = self.run_private(_RoutingGate({"sA": "CONFIDENT_STRUGGLE"}, k=5),
                          _RoutingGraph({"sA": [dict(GROUNDED_S_B)]}))
        self.assertEqual({"status": "k10_required"},
                         result["graph"]["routing"])
        self.assertNotIn("recommendations", result["graph"]["routing"])
        self.assertNotIn("PREREQUISITE_REVIEW", json.dumps(result["observed"]))

    def test_wrong_sized_end_checkpoint_fails_closed(self):
        end = {sid: {"n_items": 5, "status": "CONFIDENT_STRUGGLE"}
               for sid in self.bank["skill_names"]}
        end["sB"]["n_items"] = 10
        routing = scenario_runner._prerequisite_routing(
            self.bank, _RoutingGate({}), end, {"sA": [dict(GROUNDED_S_B)]})
        self.assertEqual({"status": "k10_required"}, routing)

    def test_routing_requires_both_gate_and_graph(self):
        graph_only = self.run_private(graph=_RoutingGraph({"sA": [dict(GROUNDED_S_B)]}))
        self.assertEqual("exploratory_only", graph_only["graph"]["status"])
        self.assertIn("sA", graph_only["graph"]["prerequisites"])
        self.assertNotIn("routing", graph_only["graph"])
        gate_only = self.run_private(gate=_RoutingGate({"sA": "CONFIDENT_STRUGGLE"}))
        self.assertEqual({"status": "not_requested"}, gate_only["graph"])

    def test_observed_student_feed_is_identical_across_routing_outcomes(self):
        graph = _RoutingGraph({"sA": [dict(GROUNDED_S_B)]})
        routed = self.run_private(_RoutingGate({"sA": "CONFIDENT_STRUGGLE"}), graph)
        direct = self.run_private(_RoutingGate({"sA": "CONFIDENT_STRUGGLE"}),
                          _RoutingGraph({}))
        quiet = self.run_private(_RoutingGate({"sA": "MASTERY_SAFE"}), graph)
        self.assertEqual("PREREQUISITE_REVIEW", routed["graph"]["routing"]
                         ["recommendations"]["sA"]["recommendation_type"])
        self.assertEqual("DIRECT_SKILL_REVIEW", direct["graph"]["routing"]
                         ["recommendations"]["sA"]["recommendation_type"])
        self.assertEqual("NONE", quiet["graph"]["routing"]
                         ["recommendations"]["sA"]["recommendation_type"])
        self.assertEqual(routed["observed"], direct["observed"])
        self.assertEqual(routed["observed"], quiet["observed"])
        student = routed["observed"]["end"]["student"]
        self.assertIn("message", student)
        self.assertEqual(quiet["observed"]["end"]["student"]["message"],
                         student["message"])
        blob = json.dumps(routed["observed"])
        for term in ("Peruslaskut", "PREREQUISITE_REVIEW",
                     "CONFIDENT_STRUGGLE", "exploratory_only"):
            self.assertNotIn(term, blob)


if __name__ == "__main__":
    unittest.main()
