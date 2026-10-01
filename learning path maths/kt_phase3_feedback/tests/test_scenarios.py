"""Fixed-instrument synthetic scenario and research-report regression checks."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

        trace = {"p_correct_before_each_answer": [.6] * 40,
                 "coverage": {"unknown_item_ids": [self.bank["questions"][0]["item_id"]]}}
        with tempfile.TemporaryDirectory() as tmp, patch(
                "scenario_runner.trace_responses", return_value=trace):
            result = run_scenario(self.bank, {"profile": "all_correct"},
                                  SessionStore(Path(tmp)), device="cpu",
                                  gate=Gate(), graph=make_taxonomy(self.bank))
        self.assertEqual("cold", result["conformal"]["items"][0]["regime"])
        self.assertEqual("cold", result["conformal"]["checkpoints"]["midpoint"]["sA"]["regime"])
        self.assertEqual("cold", result["conformal"]["checkpoints"]["end"]["sA"]["regime"])
        self.assertEqual("warm", result["conformal"]["checkpoints"]["end"]["sB"]["regime"])
        self.assertEqual(10, result["conformal"]["checkpoints"]["end"]["sA"]["n_items"])
        self.assertEqual("approximate_k5_not_calibrated_k10",
                         result["conformal"]["midpoint_status"])
        self.assertEqual("approved_bank_topics_subtopics_only", result["graph"]["scope"])
        for recommendation in result["graph"]["checkpoints"]["end"]["recommendations"].values():
            self.assertEqual("NONE", recommendation["recommendation_type"])
        self.assertNotIn("routing", result["graph"])
        self.assertNotIn("graph", json.dumps(result["observed"]))
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

    def test_saved_conformal_graph_and_teacher_stay_private_without_kt_run(self):
        from api import student_submission_result
        from compare_scenarios import pipeline_tables
        from evaluate_scenarios import attach_saved_diagnostics
        bank, taxonomy, original = self.fixture()
        original["scenarios"][0]["kt_coverage"] = {
            "unknown_item_ids": [bank["questions"][0]["item_id"]]}
        before = copy.deepcopy(original)

        class Gate:
            def __init__(self, k):
                self.k = k

            def item_decision(self, probability, regime):
                return _RoutingGate({}).item_decision(probability, regime)

            def checkpoint(self, probabilities, sid, regime):
                decision = {"n_items": len(probabilities), "regime": regime,
                            "status": "CONFIDENT_STRUGGLE" if sid == "sA" else "UNCERTAIN_BEHAVIOR",
                            "lower": .1, "upper": .9, "point_estimate": .5,
                            "mastery_threshold": .8}
                return type("Result", (), {"to_dict": lambda self: decision})()

        graph = taxonomy
        with patch("evaluate_scenarios.trace_responses", side_effect=AssertionError("must not run KT")):
            updated = attach_saved_diagnostics(original, bank, Gate(10), Gate(5), graph)
        self.assertEqual(before, original)
        case = updated["scenarios"][0]
        for key in ("responses", "observed", "selector_context", "kt_pre_answer_probability"):
            self.assertEqual(before["scenarios"][0][key], case[key])
        self.assertEqual(40, len(case["conformal"]["items"]))
        for checkpoint, k in (("midpoint", 5), ("end", 10)):
            checkpoints = case["conformal"]["checkpoints"][checkpoint]
            self.assertEqual([k] * 4, [r["n_items"] for r in checkpoints.values()])
            self.assertEqual("cold", checkpoints["sA"]["regime"])
            self.assertEqual("warm", checkpoints["sB"]["regime"])
        rec = case["graph"]["checkpoints"]["end"]["recommendations"]["sA"]
        self.assertEqual("NONE", rec["recommendation_type"])
        self.assertNotIn("routing", case["graph"])
        self.assertNotIn("graph_routing", case["teacher_end"]["research_diagnostics"])
        self.assertNotIn("teacher_midpoint", case)
        checkpoints, routing, teachers, counts = pipeline_tables(updated)
        self.assertEqual((8, 4, 1), (len(checkpoints), len(routing), len(teachers)))
        self.assertEqual(40, teachers[0]["total"]["correct"])
        public = student_submission_result({"session_id": "test", "accepted": True,
                                           "position": 40, "checkpoint": "end",
                                           "feed": {"student": case["observed"]["end"],
                                                    "teacher": case["teacher_end"]}})
        self.assertEqual(case["observed"]["end"], public["feedback"])
        for private in ("conformal", "graph", "research_diagnostics", "teacher", "question_id"):
            self.assertNotIn(private, json.dumps(public))
        with self.assertRaises(ValueError):
            attach_saved_diagnostics(original, bank, Gate(5), Gate(5), graph)
        broken = copy.deepcopy(original)
        broken["scenarios"][0]["kt_pre_answer_probability"][0] = float("nan")
        with self.assertRaises(ValueError):
            attach_saved_diagnostics(broken, bank, Gate(10), Gate(5), graph)
        broken = copy.deepcopy(original)
        broken["scenarios"][0]["kt_coverage"] = {}
        with self.assertRaises(ValueError):
            attach_saved_diagnostics(broken, bank, Gate(10), Gate(5), graph)

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


class AssessmentGraphTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)

    def run_private(self, profile="all_correct", gate=None):
        trace = {"p_correct_before_each_answer": [.6] * 40,
                 "coverage": {"unknown_item_ids": []}}
        with tempfile.TemporaryDirectory() as tmp, patch(
                "scenario_runner.trace_responses", return_value=trace):
            return run_scenario(self.bank, {"profile": profile}, SessionStore(Path(tmp)),
                                device="cpu" if gate else None, gate=gate, graph=self.taxonomy)

    def test_graph_only_uses_bank_taxonomy_without_model(self):
        result = self.run_private()
        self.assertEqual("not_requested", result["kt"]["status"])
        self.assertEqual("approved_bank_topics_subtopics_only", result["graph"]["scope"])
        self.assertNotIn("routing", result["graph"])
        self.assertNotIn("prerequisites", result["graph"])
        for checkpoint, n in (("midpoint", 20), ("end", 40)):
            graph = result["graph"]["checkpoints"][checkpoint]
            self.assertEqual(n, sum(t["out_of"] for t in graph["topics"]))
            self.assertTrue(all(r["recommendation_type"] == "NONE"
                                for r in graph["recommendations"].values()))

    def test_practice_candidates_follow_observed_errors_not_gate(self):
        low = self.run_private("all_incorrect", _RoutingGate({s: "CONFIDENT_STRUGGLE"
                                                               for s in self.bank["skill_names"]}))
        high = self.run_private("all_incorrect", _RoutingGate({s: "MASTERY_SAFE"
                                                                for s in self.bank["skill_names"]}))
        self.assertEqual(low["graph"], high["graph"])
        self.assertEqual(low["observed"], high["observed"])
        for recommendation in low["graph"]["checkpoints"]["end"]["recommendations"].values():
            self.assertEqual("ASSESSMENT_SUBTOPIC_PRACTICE", recommendation["recommendation_type"])
            self.assertEqual(10, sum(s["incorrect"] for s in recommendation["subtopics"]))
        self.assertNotIn("ASSESSMENT_SUBTOPIC_PRACTICE", json.dumps(low["observed"]))

    def test_global_graph_objects_and_stale_taxonomy_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                run_scenario(self.bank, {"profile": "all_correct"}, SessionStore(Path(tmp)), graph=object())
            stale = copy.deepcopy(self.taxonomy)
            stale["bank_fingerprint"] = "different"
            with self.assertRaises(ValueError):
                run_scenario(self.bank, {"profile": "all_correct"}, SessionStore(Path(tmp)), graph=stale)

    def test_same_skill_score_distinguishes_different_subtopic_errors(self):
        from feedback_service import assessment_feedback_graph
        from tests.helpers import responses
        taxonomy = copy.deepcopy(self.taxonomy)
        ids = taxonomy["topics"][0]["subtopics"][0]["question_ids"]
        taxonomy["topics"][0]["subtopics"] = [
            {"id": "first", "name": "First subtopic", "question_ids": ids[:5]},
            {"id": "second", "name": "Second subtopic", "question_ids": ids[5:]}]
        graphs = []
        for target in (ids[0], ids[5]):
            rows = responses(self.bank)
            for row, q in zip(rows, self.bank["questions"]):
                if row["question_id"] == target:
                    row["selected_index"] = (q["answer_index"] + 1) % len(q["options"])
            graphs.append(assessment_feedback_graph(self.bank, taxonomy, rows))
        self.assertEqual(9, graphs[0]["topics"][0]["correct"])
        self.assertEqual(9, graphs[1]["topics"][0]["correct"])
        self.assertEqual("first", graphs[0]["recommendations"]["sA"]["subtopics"][0]["subtopic_id"])
        self.assertEqual("second", graphs[1]["recommendations"]["sA"]["subtopics"][0]["subtopic_id"])
        for private in ("question_id", "answer_index", "selected_index", "CONFIDENT_STRUGGLE"):
            self.assertNotIn(private, json.dumps(graphs))

    def test_graph_refuses_incomplete_and_out_of_order_checkpoints(self):
        from feedback_service import assessment_feedback_graph
        from tests.helpers import responses
        for count in (0, 19, 21, 39):
            with self.subTest(count=count), self.assertRaises(ValueError):
                assessment_feedback_graph(self.bank, self.taxonomy, responses(self.bank, count=count))
        rows = responses(self.bank, count=20)
        rows[0]["question_id"] = rows[1]["question_id"]
        with self.assertRaises(ValueError):
            assessment_feedback_graph(self.bank, self.taxonomy, rows)
        stale = copy.deepcopy(self.taxonomy)
        stale["bank_fingerprint"] = "old"
        with self.assertRaises(ValueError):
            assessment_feedback_graph(self.bank, stale, responses(self.bank))

    def test_legacy_global_graph_report_is_refused(self):
        result = self.run_private()
        result["graph"] = {"status": "exploratory_only", "scope": "global_skill_graph_not_question_graph"}
        with self.assertRaises(ValueError):
            compile_report([result])

    def test_saved_rescope_preserves_conformal_and_strips_global_provenance(self):
        from evaluate_scenarios import rescope_saved_diagnostics
        bank, taxonomy, original = SavedEvaluationReplayTests().fixture()
        original["scenarios"][0]["conformal"] = {"checkpoints": {"end": {"frozen": "diagnostic"}}}
        original["scenarios"][0]["graph"] = {"scope": "global_skill_graph_not_question_graph"}
        original["diagnostic_provenance"] = {"graph_nodes": {}, "graph_edges": {}, "end_calibration": {"sha256": "kept"}}
        before = copy.deepcopy(original)
        result = rescope_saved_diagnostics(original, bank, taxonomy)
        self.assertEqual(before, original)
        for key in ("observed", "responses", "selector_context", "kt_pre_answer_probability", "conformal"):
            self.assertEqual(before["scenarios"][0][key], result["scenarios"][0][key])
        self.assertNotIn("graph_nodes", result["diagnostic_provenance"])
        self.assertNotIn("graph_edges", result["diagnostic_provenance"])
        self.assertEqual("kept", result["diagnostic_provenance"]["end_calibration"]["sha256"])
        self.assertNotIn("graph_routing", result["scenarios"][0]["teacher_end"]["research_diagnostics"])


if __name__ == "__main__":
    unittest.main()
