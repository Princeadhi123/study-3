import hashlib
import json
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from demo_service import DemoService
from evidence_feedback import build_evidence, canonical_digest, run_feedback
from research_workspace import DEFAULT_REPORT, ResearchConflict
from scenario_replays import DEFAULT_SOURCE
import phase3_paths
from session_store import bank_fingerprint
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_demo_service import fake_diagnostics


def replay_fixture(root, bank, taxonomy, count=3):
    cases, saved = [], []
    for index in range(count):
        rows = responses(bank, correct=index % 2 == 0, count=40)
        name = ("all_correct", "all_incorrect", "stable_strong_s11")[index % 3]
        if index >= 3:
            name = f"fixture_{index}"
        packages = [{"audience": audience, "checkpoint": checkpoint,
                     "review": run_feedback(build_evidence(bank, taxonomy, rows[:limit]), audience, checkpoint)}
                    for audience, checkpoint, limit in
                    (("student", "midpoint", 20), ("student", "end", 40), ("teacher", "end", 40))]
        cases.append({"name": name, "group": "deterministic", "packages": packages,
                      "assessment_graph": {}, "research_diagnostics": {}})
        saved.append({"name": name, "responses": rows})
    source = {"schema": "phase3_fixed40_comparison_v1",
              "scope": "private_synthetic_fixed_40_cold_start_not_student_validation",
              "bank_sha256": bank_fingerprint(bank), "scenarios": saved}
    source_path = root / "source.json"
    raw = json.dumps(source).encode()
    source_path.write_bytes(raw)
    report = {"schema": "phase3_evidence_feedback_report_v1",
              "pipeline_schema": "phase3_integrated_synthetic_pipeline_v1",
              "status": "draft_not_for_learner_delivery", "scenarios": cases,
              "source": {"bank_sha256": bank_fingerprint(bank),
                         "taxonomy_sha256": canonical_digest(taxonomy),
                         "sha256": hashlib.sha256(raw).hexdigest()},
              "policy": {"version": "fixture"}}
    report_path = root / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return source_path, report_path, source


class ScenarioReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.source_path, self.report_path, self.source = replay_fixture(
            self.root, self.bank, self.taxonomy)
        self.service = DemoService(root=self.root / "runtime", bank=self.bank,
                                   taxonomy=self.taxonomy, diagnostics=fake_diagnostics(self.bank),
                                   replay_report=self.report_path, replay_source=self.source_path)
        self.addCleanup(self.service.close, wait=True)
        self.library = self.service.research.library()

    def request(self, count=1, mode="rules"):
        return {"request_id": secrets.token_hex(16), "report_sha256": self.library["report_sha256"],
                "scenario_ids": [s["id"] for s in self.library["scenarios"][:count]],
                "provider_mode": mode, "allow_provider_calls": mode == "hosted"}

    def wait(self, run_id, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            view = self.service.replays.get(run_id)
            if view["status"] not in ("queued", "running", "cancelling"):
                return view
            time.sleep(.03)
        self.fail("replay did not finish")

    def test_selected_case_reuses_exact_answers_and_preserves_source(self):
        before = self.report_path.read_bytes(), self.source_path.read_bytes()
        request = self.request()
        run = self.service.replays.start(request)
        finished = self.wait(run["id"])
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["completed"], 1)
        result = finished["cases"][0]
        detail = self.service.teacher_session(result["session_id"])
        self.assertEqual(detail["answered_count"], 40)
        self.assertEqual([r["selected_index"] for r in detail["question_responses"]],
                         [r["selected_index"] for r in self.source["scenarios"][0]["responses"]])
        self.assertEqual(detail["checkpoints"]["end"]["diagnostics"]["mode"], "fresh_frozen_model_inference")
        self.assertEqual(result["changes"]["observed_correct_delta"], 0)
        self.assertEqual(before, (self.report_path.read_bytes(), self.source_path.read_bytes()))
        self.assertIn("code_sha256", finished)
        self.assertNotIn("student_token", json.dumps(finished))
        self.assertNotIn("selected_index", json.dumps(finished))
        self.assertTrue(detail["review_hashes"])
        self.assertEqual(self.service.list_sessions(), [])
        again = self.service.replays.start(request)
        self.assertEqual(again["id"], run["id"])
        self.assertEqual(len(self.service.replays.list()["runs"]), 1)

    def test_all_cases_have_complete_results_and_replay_history(self):
        run = self.service.replays.start(self.request(3))
        final = self.wait(run["id"])
        self.assertEqual(final["completed"], 3)
        self.assertEqual(len({c["session_id"] for c in final["cases"]}), 3)
        self.assertEqual(final["failed"], 0)
        self.assertTrue(all(c["status"] == "complete" for c in final["cases"]))
        for row in final["cases"]:
            detail = self.service.teacher_session(row["session_id"])
            self.assertEqual(detail["replay"]["run_id"], final["id"])
            self.assertEqual(detail["provider_job"]["provider_mode"], "rules")

    @unittest.skipUnless(DEFAULT_REPORT.is_file() and DEFAULT_SOURCE.is_file() and phase3_paths.APPROVED_BANK.is_file(),
                         "private retained report/source/bank unavailable")
    def test_all_54_retained_cases_with_fake_diagnostics_and_no_providers(self):
        self.service.close(wait=True)
        bank = json.loads(phase3_paths.APPROVED_BANK.read_text(encoding="utf-8"))
        taxonomy = json.loads(phase3_paths.ASSESSMENT_TAXONOMY.read_text(encoding="utf-8"))
        self.service = DemoService(root=self.root / "real_source_test", bank=bank, taxonomy=taxonomy,
                                   diagnostics=fake_diagnostics(bank))
        self.addCleanup(self.service.close, wait=True)
        self.library = self.service.research.library()
        names = [row["display_name"] for row in self.library["scenarios"]]
        self.assertEqual(len(names), 54)
        self.assertEqual(len(set(names)), 54)
        self.assertTrue(all("skill_" not in n and "_s11" not in n for n in names))
        run = self.service.replays.start(self.request(54))
        final = self.wait(run["id"], timeout=120)
        self.assertEqual(final["completed"], 54)
        self.assertEqual(final["failed"], 0)
        self.assertTrue(all(row["changes"]["observed_correct_delta"] == 0 for row in final["cases"]))
        self.assertTrue(all(len(self.service.teacher_session(row["session_id"])["question_responses"]) == 40
                            for row in final["cases"]))

    def test_source_binding_and_invalid_request_fail_before_admission(self):
        for body in (dict(self.request(), scenario_ids=["f" * 32]),
                     dict(self.request(), scenario_ids=[]),
                     dict(self.request(), scenario_ids=[self.library["scenarios"][0]["id"]] * 2),
                     dict(self.request(), allow_provider_calls="false")):
            with self.assertRaises(ValueError):
                self.service.replays.start(body)
        with self.assertRaises(ResearchConflict):
            self.service.replays.start(dict(self.request(), report_sha256="0" * 64))
        self.source_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.service.replays.start(self.request())
        self.assertEqual(self.service.replays.list()["runs"], [])

    def test_hosted_requires_configuration_and_explicit_consent(self):
        with self.assertRaises(ValueError):
            self.service.replays.start(self.request(mode="hosted"))
        self.service.provider_mode = "hosted"
        with self.assertRaises(ValueError):
            self.service.replays.start(dict(self.request(mode="hosted"), allow_provider_calls=False))
        with patch.object(self.service, "_audience_result", wraps=self.service._audience_result) as call:
            final = self.wait(self.service.replays.start(self.request())["id"])
        self.assertEqual(final["status"], "complete")
        self.assertTrue(all(c.args[2] == "rules" for c in call.call_args_list))

    def test_code_changes_require_server_restart(self):
        with patch.object(self.service.replays, "current_code", return_value={"changed": "digest"}):
            with self.assertRaises(ResearchConflict):
                self.service.replays.start(self.request())

    def test_cancel_stops_admitting_more_cases_and_busy_start_is_rejected(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        evaluate = self.service.diagnostics.evaluate

        def slow(*args):
            entered.set()
            release.wait(10)
            return evaluate(*args)

        self.addCleanup(release.set)
        with patch.object(self.service.diagnostics, "evaluate", side_effect=slow):
            run = self.service.replays.start(self.request(3))
            self.assertTrue(entered.wait(5))
            with self.assertRaises(ResearchConflict):
                self.service.replays.start(self.request())
            self.service.replays.cancel(run["id"])
            release.set()
            final = self.wait(run["id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(sum(c["status"] == "cancelled" for c in final["cases"]), 2)

    def test_original_case_detail_is_read_only_and_has_all_answers(self):
        case_id = self.library["scenarios"][0]["id"]
        view = self.service.scenario_detail(case_id)["detail"]
        self.assertTrue(view["read_only"])
        self.assertEqual(len(view["question_responses"]), 40)
        self.assertEqual(view["review_hashes"], {})
        self.assertEqual(view["checkpoints"]["end"]["student_review"]["message"],
                         self.service.research.scenario(case_id)["packages"][1]["review"]["message"])
        self.assertFalse(self.service.list_sessions())

    def test_saved_replay_remains_readable_after_policy_changes(self):
        final = self.wait(self.service.replays.start(self.request())["id"])
        case = final["cases"][0]
        before = self.service.replays.result(final["id"], case["scenario_id"])
        contract = before["review_contract"]
        self.service.add_review(case["session_id"], {
            "audience": "student", "message_sha256": before["review_hashes"]["student"],
            "judgments": {f["id"]: f["choices"][0] for f in contract["fields"]},
            "note": "Preserve this old-version review"})
        with patch("demo_service.POLICY_VERSION", "future-policy"):
            after = self.service.replays.result(final["id"], case["scenario_id"])
        self.assertTrue(after["read_only"])
        self.assertEqual(after["question_responses"], before["question_responses"])
        self.assertEqual(after["reviews"][0]["note"], "Preserve this old-version review")
        self.assertFalse(after["review_hashes"])
        self.assertEqual(after["replay_changes"], case["changes"])

    def test_restart_marks_unfinished_runs_interrupted_without_resubmitting(self):
        self.service.close(wait=True)
        directory = self.root / "runtime" / "replay_runs"
        directory.mkdir(exist_ok=True)
        identifier = secrets.token_hex(16)
        (directory / f"{identifier}.json").write_text(json.dumps({
            "id": identifier, "status": "running", "created_at": "fixture",
            "cases": [{"status": "running"}, {"status": "queued"}]}), encoding="utf-8")
        from scenario_replays import ScenarioReplays
        manager = ScenarioReplays(self.service, self.source_path)
        self.addCleanup(manager.close, wait=True)
        self.assertEqual(manager.get(identifier)["status"], "interrupted")
        self.assertTrue(all(c["status"] == "interrupted" for c in manager.get(identifier)["cases"]))
        self.assertEqual(self.service.list_sessions(), [])


if __name__ == "__main__":
    unittest.main()
