import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from research_workspace import ResearchWorkspace, ResearchConflict


def make_report(count=4):
    evidence = {
        "data_origin": "synthetic",
        "total": {"correct": 20, "incorrect": 20, "out_of": 40},
        "halves": [{"correct": 10, "incorrect": 10, "out_of": 20}] * 2,
        "skills": [{"skill_name": name, "skill_id": name.lower(),
                    "correct": 5, "incorrect": 5, "out_of": 10}
                   for name in ("Arithmetic", "Prices", "Fractions", "Percentages")],
        "subtopics": []}
    scenarios = []
    for index in range(count):
        packages = []
        for audience in ("student", "teacher"):
            packages.append({
                "audience": audience, "checkpoint": "end",
                "review": {
                    "sanitized_evidence": copy.deepcopy(evidence),
                    "baseline_candidate_id": "base",
                    "selected_candidate_id": "selected" if index % 2 else "base",
                    "template_baseline": {"text": "First draft."},
                    "message": {"text": "Second draft."},
                    "trace": {"fallback_reason": None},
                    "candidates": []},
                "selector_execution": {"capture_file": "jev_test.json",
                                       "metadata": {"status": "completed"},
                                       "reused": bool(index % 2)},
                "generator_execution": {"status": "offline_template_no_provider"}})
        scenarios.append({"name": f"case_{index}", "group": "deterministic",
                          "packages": packages, "assessment_graph": {},
                          "research_diagnostics": {}})
    return {"schema": "phase3_evidence_feedback_report_v1",
            "pipeline_schema": "phase3_integrated_synthetic_pipeline_v1",
            "status": "draft_not_for_learner_delivery",
            "source": {"bank_sha256": "bank", "taxonomy_sha256": "taxonomy"},
            "policy": {"version": "test_policy"}, "scenarios": scenarios}


def judgment(task, preference="A"):
    return {"task_id": task["task_id"], "preference": preference,
            "support_a": "supported", "support_b": "unsure",
            "confidence": "low", "note": "Synthetic fixture only."}


class ResearchWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.report = make_report()
        self.report_path = self.root / "report.json"
        self.report_path.write_text(json.dumps(self.report), encoding="utf-8")
        self.workspace = ResearchWorkspace(self.root, self.report_path)

    def start(self, audience="student"):
        return self.workspace.create_comparison({
            "reviewer_label": "reviewer-01", "audience": audience,
            "prior_exposure": False})

    def test_library_counts_are_derived_not_trusted(self):
        self.report["summary"] = {"scenario_labels": 9000}
        self.report_path.write_text(json.dumps(self.report), encoding="utf-8")
        view = self.workspace.library()
        self.assertEqual(view["summary"]["scenarios"], 4)
        self.assertEqual(view["summary"]["end_packages"], 8)
        self.assertEqual(view["summary"]["selection_differences"], 4)
        self.assertEqual(view["summary"]["selection_matches"], 4)
        self.assertEqual(view["summary"]["selector_execution"]["cached_hosted"], 4)
        case = self.workspace.scenario(view["scenarios"][0]["id"])
        self.assertEqual(case["name"], "case_0")
        self.assertEqual(len(case["packages"]), 2)
        self.assertFalse(view["educational_effectiveness_tested"])

    def test_missing_and_invalid_reports_fail_closed(self):
        missing = ResearchWorkspace(self.root, self.root / "missing.json")
        self.assertEqual(missing.library()["status"], "unavailable")
        with self.assertRaises(ValueError):
            missing.create_comparison({"reviewer_label": "x", "audience": "student",
                                       "prior_exposure": False})
        self.report_path.write_text("{}", encoding="utf-8")
        self.assertEqual(self.workspace.library()["status"], "unavailable")

    def test_malformed_report_shapes_are_unavailable(self):
        invalid = [None, [], {"scenarios": None}]
        for field in ("source", "policy"):
            report = make_report()
            report.pop(field)
            invalid.append(report)
        for field in ("group", "packages"):
            report = make_report()
            report["scenarios"][0].pop(field)
            invalid.append(report)
        for field in ("baseline_candidate_id", "trace"):
            report = make_report()
            report["scenarios"][0]["packages"][0]["review"].pop(field)
            invalid.append(report)
        report = make_report()
        report["scenarios"][0]["packages"][0]["selector_execution"] = None
        invalid.append(report)
        for report in invalid:
            self.report_path.write_text(json.dumps(report), encoding="utf-8")
            workspace = ResearchWorkspace(self.root, self.report_path)
            self.assertEqual(workspace.library()["status"], "unavailable")

    def test_duplicate_cases_and_packages_are_rejected(self):
        for change in ("cases", "packages"):
            report = make_report()
            if change == "cases":
                report["scenarios"].append(copy.deepcopy(report["scenarios"][0]))
            else:
                case = report["scenarios"][0]
                case["packages"].append(copy.deepcopy(case["packages"][0]))
            self.report_path.write_text(json.dumps(report), encoding="utf-8")
            workspace = ResearchWorkspace(self.root, self.report_path)
            self.assertEqual(workspace.library()["status"], "unavailable")

    def test_inconsistent_aggregate_evidence_is_rejected(self):
        for group in ("skills", "halves"):
            report = make_report()
            evidence = report["scenarios"][0]["packages"][0]["review"]["sanitized_evidence"]
            evidence[group][0]["correct"] -= 1
            evidence[group][0]["incorrect"] += 1
            self.report_path.write_text(json.dumps(report), encoding="utf-8")
            workspace = ResearchWorkspace(self.root, self.report_path)
            self.assertEqual(workspace.library()["status"], "unavailable")

    def test_blind_payload_excludes_sources_and_case_identity(self):
        view = self.start()
        self.assertEqual(view["total"], 4)
        self.assertEqual(view["completed"], 0)
        task = view["task"]
        self.assertEqual(set(task["drafts"]), {"A", "B"})
        blob = json.dumps(view)
        for marker in ("case_", "candidate", "selector", "generator", "baseline",
                       "report_sha256", "mapping", "research_diagnostics"):
            self.assertNotIn(marker, blob)
        self.assertEqual(task["evidence"]["total"]["out_of"], 40)
        with self.assertRaises(ResearchConflict):
            self.workspace.export_comparison(view["id"])

    def test_resume_idempotency_and_immutable_judgment(self):
        start = self.start()
        body = judgment(start["task"])
        saved = self.workspace.add_judgment(start["id"], body)
        again = self.workspace.add_judgment(start["id"], body)
        self.assertEqual(saved, again)
        self.assertEqual(saved["completed"], 1)
        with self.assertRaises(ResearchConflict):
            self.workspace.add_judgment(start["id"], dict(body, preference="B"))
        restarted = ResearchWorkspace(self.root, self.report_path)
        self.assertEqual(restarted.comparison(start["id"]), saved)
        self.assertEqual(restarted.comparisons()["comparisons"][0]["completed"], 1)

    def test_concurrent_retry_records_once(self):
        start = self.start()
        body = judgment(start["task"])
        with ThreadPoolExecutor(max_workers=4) as pool:
            views = list(pool.map(lambda _: self.workspace.add_judgment(
                start["id"], body), range(4)))
        self.assertTrue(all(v["completed"] == 1 for v in views))

    def test_completion_reveals_mapping_and_reproducible_bindings(self):
        view = self.start()
        for _ in range(view["total"]):
            view = self.workspace.add_judgment(view["id"], judgment(view["task"]))
        self.assertEqual(view["status"], "complete")
        self.assertIsNone(view["task"])
        export = self.workspace.export_comparison(view["id"])
        self.assertEqual(len(export["tasks"]), 4)
        self.assertEqual(len(export["report_sha256"]), 64)
        self.assertFalse(export["approves_learner_delivery"])
        self.assertFalse(export["educational_effectiveness_tested"])
        self.assertEqual(sum(view["results"]["preferences"].values()), 4)
        self.assertEqual(sum(t["mapping"]["A"] == "baseline"
                             for t in export["tasks"]), 2)
        for task in export["tasks"]:
            self.assertEqual(set(task["message_sha256"]), {"A", "B"})
            self.assertIn(task["judgment"]["resolved_preference"],
                          ("baseline", "selected"))
            self.assertEqual(task["messages"]["A"], task["messages"]["A"].strip())

    def test_changed_report_refuses_to_mix_revisions(self):
        start = self.start()
        self.report_path.write_text(json.dumps(make_report(3)), encoding="utf-8")
        restarted = ResearchWorkspace(self.root, self.report_path)
        with self.assertRaises(ResearchConflict):
            restarted.add_judgment(start["id"], judgment(start["task"]))
        self.assertEqual(restarted.comparison(start["id"])["status"], "source_changed")

    def test_payload_validation_and_path_traversal(self):
        start = self.start()
        for bad in (dict(judgment(start["task"]), preference=True),
                    dict(judgment(start["task"]), support_a="great"),
                    dict(judgment(start["task"]), note="x" * 2001),
                    dict(judgment(start["task"]), extra="x")):
            with self.assertRaises(ValueError):
                self.workspace.add_judgment(start["id"], bad)
        with self.assertRaises(ResearchConflict):
            self.workspace.add_judgment(start["id"],
                                        dict(judgment(start["task"]), task_id="0" * 32))
        for bad in ("../report", "", True):
            with self.assertRaises(ValueError):
                self.workspace.comparison(bad)
        with self.assertRaises(ValueError):
            self.workspace.create_comparison({"reviewer_label": "x", "audience": "student",
                                               "prior_exposure": "false"})

    def test_identical_drafts_are_retained_as_controls(self):
        for case in self.report["scenarios"]:
            for package in case["packages"]:
                package["review"]["message"] = {"text": "First draft."}
        self.report_path.write_text(json.dumps(self.report), encoding="utf-8")
        view = self.start()
        while view["task"]:
            view = self.workspace.add_judgment(view["id"], judgment(view["task"], "tie"))
        self.assertEqual(view["results"]["identical_pairs"], 4)
        self.assertEqual(view["results"]["preferences"]["tie"], 4)


if __name__ == "__main__":
    unittest.main()
