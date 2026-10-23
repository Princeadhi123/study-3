import contextlib
import hashlib
import io
import json
import secrets
import tempfile
import threading
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
from tests.test_demo_service import fake_diagnostics, wait_for


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
        self.assertEqual([row["audience"] for row in result["changes"]["feedback"]],
                         ["student"])
        self.assertIsNone(detail["checkpoints"]["end"]["teacher_review"])
        self.assertIsNone(detail["checkpoints"]["end"]["baseline_teacher"])
        original = self.service.research.scenario(result["scenario_id"])
        self.assertEqual(
            [p["audience"] for p in original["packages"]
             if p["checkpoint"] == "end"], ["student", "teacher"])
        self.assertEqual(before, (self.report_path.read_bytes(), self.source_path.read_bytes()))
        self.assertIn("code_sha256", finished)
        self.assertNotIn("student_token", json.dumps(finished))
        self.assertNotIn("selected_index", json.dumps(finished))
        self.assertTrue(detail["review_hashes"])
        self.assertEqual(self.service.list_sessions(), [])
        again = self.service.replays.start(request)
        self.assertEqual(again["id"], run["id"])
        self.assertEqual(len(self.service.replays.list()["runs"]), 1)

    def test_transient_session_save_conflict_does_not_abort_replay(self):
        # A single WinError 32 on the session-file replace is retried
        # inside the store; the replay still records all 40 answers
        # exactly once and completes normally.
        real_replace = Path.replace
        injected = {"count": 0}
        store_root = self.service.store.root

        def flaky(source, target):
            if (source.parent == store_root
                    and source.suffix == ".tmp"):
                doc = json.loads(source.read_text("utf-8"))
                if (len(doc.get("responses") or []) == 28
                        and not injected["count"]):
                    injected["count"] += 1
                    err = PermissionError("sharing violation")
                    err.winerror = 32
                    raise err
            return real_replace(source, target)

        with patch.object(Path, "replace", autospec=True,
                          side_effect=flaky), \
                patch("session_store.sleep"):
            run = self.service.replays.start(self.request())
            finished = self.wait(run["id"])
        self.assertEqual(injected["count"], 1)
        self.assertEqual(finished["status"], "complete")
        case = finished["cases"][0]
        self.assertEqual(case["status"], "complete")
        detail = self.service.teacher_session(case["session_id"])
        expected = self.source["scenarios"][0]["responses"]
        self.assertEqual(detail["answered_count"], 40)
        self.assertEqual(len(detail["question_responses"]), 40)
        self.assertEqual(
            [r["selected_index"] for r in detail["question_responses"]],
            [r["selected_index"] for r in expected])
        by_id = {q["question_id"]: q for q in self.bank["questions"]}
        self.assertEqual(
            detail["observed_total"]["correct"],
            sum(1 for r in expected
                if r["selected_index"]
                == by_id[r["question_id"]]["answer_index"]))
        self.assertIsNone(detail["background_work_cancelled"])
        stored = self.service.store.load(case["session_id"])
        self.assertEqual(len(stored["responses"]), 40)

    def test_current_code_includes_feedback_modules(self):
        names = set(self.service.replays.current_code())
        self.assertIn("full_feedback.py", names)
        self.assertIn("feedback_practice.py", names)
        self.assertIn("midpoint_feedback.py", names)
        self.assertIn("worked_example_selection.py", names)

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
        bad_sha = dict(self.request(), report_sha256="0" * 64)
        with self.assertRaises(ResearchConflict):
            self.service.replays.start(bad_sha)
        self.source_path.write_text("{}", encoding="utf-8")
        empty_source = self.request()
        with self.assertRaises(ValueError):
            self.service.replays.start(empty_source)
        self.assertEqual(self.service.replays.list()["runs"], [])

    def test_hosted_requires_configuration_and_explicit_consent(self):
        unconfigured = self.request(mode="hosted")
        with self.assertRaises(ValueError):
            self.service.replays.start(unconfigured)
        self.service.provider_mode = "hosted"
        no_consent = dict(self.request(mode="hosted"),
                          allow_provider_calls=False)
        with self.assertRaises(ValueError):
            self.service.replays.start(no_consent)
        with patch.object(self.service, "_audience_result", wraps=self.service._audience_result) as call:
            final = self.wait(self.service.replays.start(self.request())["id"])
        self.assertEqual(final["status"], "complete")
        self.assertTrue(all(c.args[2] == "rules" for c in call.call_args_list))

    def test_code_changes_require_server_restart(self):
        with patch.object(self.service.replays, "current_code", return_value={"changed": "digest"}):
            body = self.request()
            with self.assertRaises(ResearchConflict):
                self.service.replays.start(body)

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
            run = self.service.replays.start(dict(self.request(3), max_parallel=1))
            self.assertTrue(entered.wait(5))
            busy_request = self.request()
            with self.assertRaises(ResearchConflict):
                self.service.replays.start(busy_request)
            self.service.replays.cancel(run["id"])
            release.set()
            final = self.wait(run["id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(sum(c["status"] == "cancelled" for c in final["cases"]), 2)

    def test_parallel_execution_runs_6_to_8_cases_concurrently(self):
        import threading
        self.service.close(wait=True)
        fix_dir = self.root / "parallel_fix"
        fix_dir.mkdir(exist_ok=True)
        source_path, report_path, _ = replay_fixture(
            fix_dir, self.bank, self.taxonomy, count=10)
        self.service = DemoService(
            root=self.root / "parallel_runtime", bank=self.bank,
            taxonomy=self.taxonomy, diagnostics=fake_diagnostics(self.bank),
            replay_report=report_path, replay_source=source_path)
        self.addCleanup(self.service.close, wait=True)
        library = self.service.research.library()
        for invalid in (0, 9, "6", True, False):
            body = {
                "request_id": secrets.token_hex(16),
                "report_sha256": library["report_sha256"],
                "scenario_ids": [s["id"] for s in library["scenarios"]],
                "provider_mode": "rules",
                "allow_provider_calls": False,
                "max_parallel": invalid}
            with self.assertRaises(ValueError):
                self.service.replays.start(body)
        barrier = threading.Barrier(6, timeout=10)
        release = threading.Event()
        self.addCleanup(release.set)
        real_jobs_complete = self.service._jobs_complete
        arrived = set()
        lock = threading.Lock()

        def synchronized_jobs_complete(session_id):
            with lock:
                first_visit = session_id not in arrived and len(arrived) < 6
                if first_visit:
                    arrived.add(session_id)
            if first_visit:
                barrier.wait()
                release.wait(10)
            return real_jobs_complete(session_id)

        with patch.object(self.service, "_jobs_complete", side_effect=synchronized_jobs_complete):
            run = self.service.replays.start({
                "request_id": secrets.token_hex(16),
                "report_sha256": library["report_sha256"],
                "scenario_ids": [s["id"] for s in library["scenarios"]],
                "provider_mode": "rules",
                "allow_provider_calls": False,
                "max_parallel": 6})
            self.assertTrue(wait_for(lambda: len(arrived) == 6))
            mid = self.service.replays.get(run["id"])
            self.assertEqual(mid["max_parallel"], 6)
            self.assertEqual(sum(c["status"] == "running" for c in mid["cases"]), 6)
            self.assertEqual(sum(c["status"] == "queued" for c in mid["cases"]), 4)
            release.set()
            final = self.wait(run["id"])
        self.assertEqual(final["status"], "complete")
        self.assertEqual(final["completed"], 10)
        self.assertEqual(final["failed"], 0)

    def test_replay_waits_for_practice_selection_before_completion(self):
        import copy
        import threading
        import feedback_practice
        from tests.test_demo_service import BoundRecommender
        from tests.test_feedback_practice import make_graph, make_pool
        pool = make_pool(("sA", "sB"))
        gate = threading.Event()
        self.addCleanup(gate.set)
        recommender = BoundRecommender(self.bank, pool, gate=gate)
        self.service.close(wait=True)
        self.service = DemoService(
            root=self.root / "runtime", bank=self.bank,
            taxonomy=self.taxonomy, diagnostics=fake_diagnostics(self.bank),
            replay_report=self.report_path, replay_source=self.source_path,
            review_mode="legacy_single")
        self.addCleanup(self.service.close, wait=True)
        self.service.recommender = recommender
        self.library = self.service.research.library()
        scenario_id = self.library["scenarios"][1]["id"]
        with patch.object(
                feedback_practice, "load_current_practice_pool",
                return_value=copy.deepcopy(pool)), \
             patch.object(
                feedback_practice, "teacher_content_context",
                return_value=make_graph(pool)):
            run = self.service.replays.start(
                dict(self.request(), scenario_ids=[scenario_id]))
            self.assertTrue(wait_for(lambda: recommender.calls >= 1))
            time.sleep(0.3)
            view = self.service.replays.get(run["id"])
            self.assertEqual(view["cases"][0]["status"], "running")
            session_id = view["cases"][0]["session_id"]
            detail = self.service.teacher_session(session_id)
            self.assertEqual(detail["provider_job"]["status"],
                             "waiting_for_practice")
            gate.set()
            final = self.wait(run["id"])
            self.assertEqual(final["status"], "complete")
            case = final["cases"][0]
            self.assertEqual(case["status"], "complete")
            self.assertEqual(case["provider_status"], "ready")
            detail = self.service.teacher_session(session_id)
            end = detail["checkpoints"]["end"]
            self.assertEqual(end["practice_context"]["status"], "selected")
            self.assertEqual(
                end["practice_context"]["selected_question_id"],
                "pool_q1")
            self.assertIsNotNone(
                detail["feedback_delivery"]["preview"]["practice"]
                ["question"])
        self.assertIsNotNone(end["student_review"])
        self.assertIsNone(end["teacher_review"])
        self.assertIsNone(end["baseline_teacher"])
        self.assertEqual(recommender.calls, 1)

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


class _BlockingOpener:
    """Hosted opener stub: blocks inside the request until released,
    then raises/returns the queued outcome. Exercises real transport
    metadata without any network."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def open(self, request, timeout=None):
        self.calls.append(request)
        self.entered.set()
        self.release.wait(timeout=60)
        outcome = self.outcome
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ReplaySessionCancellationTests(unittest.TestCase):
    """A failed replay case cancels future provider work for its
    session: no orphan midpoint retries, no late publication, while
    healthy sessions stay fully independent."""

    def setUp(self):
        from aitta_generator import AittaGenerator
        from midpoint_feedback import (
            CachedMidpointGenerator, MidpointAittaGenerator)
        from replay_provider_scenarios import CaptureCache
        from tests.test_full_feedback import (
            API_KEY, BASE_URL, MODEL, FakeResponse)
        from tests.test_auto_retry import SequenceOpener, _midpoint_reply
        self._fakes = {
            "AittaGenerator": AittaGenerator,
            "CachedMidpointGenerator": CachedMidpointGenerator,
            "MidpointAittaGenerator": MidpointAittaGenerator,
            "SequenceOpener": SequenceOpener,
            "midpoint_reply": _midpoint_reply,
            "api": (API_KEY, BASE_URL, MODEL),
            "response": FakeResponse}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.source_path, self.report_path, self.source = replay_fixture(
            self.root, self.bank, self.taxonomy)
        capture_root = self.root / "captures"
        capture_root.mkdir()
        self.cache = CaptureCache(capture_root, None,
                                  automatic_retries=True,
                                  reuse_captures=False)

    def _midpoint(self, opener):
        api_key, base_url, model = self._fakes["api"]
        native = self._fakes["MidpointAittaGenerator"](
            self._fakes["AittaGenerator"](api_key, base_url, model=model,
                                         opener=opener))
        return self._fakes["CachedMidpointGenerator"](
            native, self.cache, model, "endpoint-fixture")

    def _service(self, opener=None, **kwargs):
        from tests.test_demo_service import MultiGenerator
        kwargs.setdefault("provider_mode", "hosted")
        kwargs.setdefault("generator", MultiGenerator())
        kwargs.setdefault("midpoint_generator",
                          self._midpoint(opener)
                          if opener is not None else None)
        service = DemoService(
            root=self.root / f"runtime-{secrets.token_hex(4)}",
            bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            replay_report=self.report_path,
            replay_source=self.source_path, **kwargs)
        service.cache = self.cache
        self.addCleanup(service.close, wait=True)
        return service

    def _request(self, service, mode="hosted"):
        library = service.research.library()
        return {"request_id": secrets.token_hex(16),
                "report_sha256": library["report_sha256"],
                "scenario_ids": [s["id"] for s in library["scenarios"][:1]],
                "provider_mode": mode,
                "allow_provider_calls": mode == "hosted"}

    def _wait(self, service, run_id, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            view = service.replays.get(run_id)
            if view["status"] not in ("queued", "running", "cancelling"):
                return view
            time.sleep(.03)
        self.fail("replay did not finish")

    def _midpoint_records(self):
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in (self.root / "captures").glob(
                    "aitta_midpoint_*.json")]

    def test_case_failure_cancels_midpoint_retry_and_late_result(self):
        # f+h: 21 answers accepted, midpoint call in-flight, case fails
        # on the 22nd submission; the in-flight call finishes but its
        # late result is discarded and no retry ever runs.
        opener = _BlockingOpener(TimeoutError("net"))
        service = self._service(opener)
        # LIFO: gates must release before service.close(wait=True).
        self.addCleanup(opener.release.set)
        original = service.submit_response
        calls = {"count": 0}

        def wrapped(session_id, token, submission):
            calls["count"] += 1
            if calls["count"] == 22:
                self.assertTrue(opener.entered.wait(timeout=30))
                raise RuntimeError("Authorization sk-secret-do-not-log")
            return original(session_id, token, submission)

        console = io.StringIO()
        with patch.object(service, "submit_response",
                          side_effect=wrapped):
            with contextlib.redirect_stdout(console):
                run = service.replays.start(self._request(service))
                finished = self._wait(service, run["id"])
        self.assertEqual(finished["status"], "complete_with_errors")
        case = finished["cases"][0]
        self.assertEqual(case["status"], "failed")
        self.assertEqual(case["reason"], "replay_case_failed")
        details = case["failure_details"]
        self.assertEqual(details["exception_type"], "RuntimeError")
        self.assertEqual(details["stage"], "submit_response")
        self.assertEqual(details["submitted_count"], 21)
        self.assertNotIn("sk-secret-do-not-log", json.dumps(finished))
        self.assertNotIn("sk-secret-do-not-log", console.getvalue())
        self.assertIn("stage=submit_response", console.getvalue())
        sid = case["session_id"]
        meta = service._load_meta(sid)
        self.assertEqual(meta["background_work_cancelled"]["reason"],
                         "replay_case_failed")
        block = meta["checkpoints"]["midpoint"]
        self.assertEqual(block["phrasing_job"]["status"], "cancelled")
        self.assertEqual(block["phrasing_job"]["reason"],
                         "replay_case_failed")
        # In-flight call cannot be stopped; releasing it keeps the
        # capture but publishes nothing and never retries.
        opener.release.set()
        self.assertTrue(wait_for(
            lambda: service._pending_provider == 0, timeout=30))
        self.assertEqual(len(opener.calls), 1)
        meta = service._load_meta(sid)
        block = meta["checkpoints"]["midpoint"]
        self.assertIsNone(block["provider_draft"])
        self.assertEqual(block["phrasing_job"]["status"], "cancelled")
        records = [r for r in self._midpoint_records()
                   if (r.get("call_context") or {}).get("session_id")
                   == sid]
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["ok"])
        self.assertIn(f"session={sid}", console.getvalue())
        # Idempotent cancellation marker; direct sessions unaffected.
        marker = meta["background_work_cancelled"]
        service.cancel_replay_session(sid)
        self.assertEqual(
            service._load_meta(sid)["background_work_cancelled"], marker)
        # The teacher view surfaces the marker, and the replay result
        # carries sanitized execution state while the underlying
        # recorded answers and in-progress status stay intact.
        teacher = service.teacher_session(sid)
        self.assertEqual(teacher["background_work_cancelled"], marker)
        result = service.replays.result(run["id"], case["scenario_id"])
        self.assertEqual(result["replay_execution"]["status"], "failed")
        self.assertEqual(result["replay_execution"]["reason"],
                         "replay_case_failed")
        self.assertEqual(result["replay_execution"]["failure_details"]
                         ["submitted_count"], 21)
        self.assertEqual(result["status"], "in_progress")
        self.assertEqual(result["answered_count"], 21)
        self.assertNotIn("sk-secret-do-not-log", json.dumps(result))
        healthy = _BlockingOpener(
            self._fakes["response"](self._fakes["midpoint_reply"]()))
        healthy.release.set()
        service.midpoint_generator = self._midpoint(healthy)
        with contextlib.redirect_stdout(io.StringIO()):
            second = self._wait(
                service,
                service.replays.start(self._request(service))["id"])
        self.assertEqual(second["status"], "complete")
        fresh = second["cases"][0]["session_id"]
        self.assertNotEqual(fresh, sid)
        fresh_meta = service._load_meta(fresh)
        self.assertNotIn("background_work_cancelled", fresh_meta)
        self.assertEqual(fresh_meta["checkpoints"]["midpoint"]
                         ["phrasing_job"]["status"], "ready")
        self.assertIsNotNone(fresh_meta["checkpoints"]["midpoint"]
                             ["provider_draft"])
        self.assertEqual(len(healthy.calls), 1)
        self.assertIsNone(service.teacher_session(fresh)
                          ["background_work_cancelled"])
        fresh_result = service.replays.result(
            second["id"], second["cases"][0]["scenario_id"])
        self.assertEqual(fresh_result["replay_execution"]["status"],
                         "complete")

    def test_late_midpoint_success_is_captured_but_discarded(self):
        # d: the in-flight call succeeds after the case already failed;
        # its capture is retained for audit but nothing is published.
        opener = _BlockingOpener(
            self._fakes["response"](self._fakes["midpoint_reply"]()))
        service = self._service(opener)
        self.addCleanup(opener.release.set)
        original = service.submit_response
        calls = {"count": 0}

        def wrapped(session_id, token, submission):
            calls["count"] += 1
            if calls["count"] == 22:
                self.assertTrue(opener.entered.wait(timeout=30))
                raise RuntimeError("case failure")
            return original(session_id, token, submission)

        console = io.StringIO()
        with patch.object(service, "submit_response",
                          side_effect=wrapped):
            with contextlib.redirect_stdout(console):
                run = service.replays.start(self._request(service))
                finished = self._wait(service, run["id"])
                sid = finished["cases"][0]["session_id"]
                opener.release.set()
                self.assertTrue(wait_for(
                    lambda: service._pending_provider == 0, timeout=30))
        self.assertEqual(len(opener.calls), 1)
        meta = service._load_meta(sid)
        block = meta["checkpoints"]["midpoint"]
        self.assertEqual(block["phrasing_job"]["status"], "cancelled")
        self.assertIsNone(block["provider_draft"])
        records = [r for r in self._midpoint_records()
                   if (r.get("call_context") or {}).get("session_id")
                   == sid]
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["ok"])
        self.assertIn("session cancelled; response ignored",
                      console.getvalue())

    def test_cancel_survives_meta_load_failure(self):
        # a: Event.set happens before metadata I/O; a failing
        # _load_meta cannot leave retries running, and the cleanup
        # error is recorded without details.
        service = self._service()
        created = service.create_session(provider_mode="hosted")
        sid, token = created["session_id"], created["student_token"]
        service.submit_response(
            sid, token, responses(self.bank, count=1)[0])
        event = service._provider_session_event(sid)
        with patch.object(service, "_load_meta",
                          side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                service.cancel_replay_session(sid)
        self.assertTrue(event.is_set())
        self.assertTrue(
            service._provider_session_stops[sid].is_set())
        # The same protection reaches the replay record when the
        # cleanup path itself fails: only the exception type is kept.
        original = service.submit_response
        calls = {"count": 0}

        def wrapped(session_id, token, submission):
            calls["count"] += 1
            if calls["count"] == 3:
                patcher = patch.object(
                    service, "_load_meta",
                    side_effect=PermissionError("denied"))
                patcher.start()
                self.addCleanup(patcher.stop)
                raise RuntimeError("case failure")
            return original(session_id, token, submission)

        with patch.object(service, "submit_response",
                          side_effect=wrapped):
            run = service.replays.start(self._request(service))
            finished = self._wait(service, run["id"])
        case = finished["cases"][0]
        self.assertEqual(case["status"], "failed")
        self.assertEqual(
            case["failure_details"]["cleanup_error_type"],
            "PermissionError")
        self.assertTrue(
            service._provider_session_stops[
                case["session_id"]].is_set())

    def test_recommendation_stage_cancel_still_suppresses_publish(self):
        # b: cancellation lands while the worker waits on the
        # recommendation result; the finished recommendation is never
        # published and the cancelled provider_job is not rewritten.
        rec_entered = threading.Event()
        rec_release = threading.Event()

        class _StubRecommender:
            def recommend_for_reviews(self, bank, taxonomy, rows,
                                      skill_ids, research=False,
                                      review_subtopic_ids=None):
                rec_entered.set()
                rec_release.wait(timeout=60)
                return {"status": "abstained", "mode": "shadow_only",
                        "reason": "test_gate",
                        "used_for_student_advice": False,
                        "used_for_feedback": False,
                        "topic_selections": []}

        from tests.test_demo_service import MultiGenerator
        generator = MultiGenerator()
        service = self._service(
            self._fakes["SequenceOpener"](
                [self._fakes["midpoint_reply"]()]),
            generator=generator, recommender=_StubRecommender())
        self.addCleanup(rec_release.set)
        created = service.create_session(provider_mode="hosted")
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            service.submit_response(sid, token, row)
        self.assertTrue(rec_entered.wait(timeout=30))
        service.cancel_replay_session(sid)
        rec_release.set()
        self.assertTrue(wait_for(
            lambda: service._pending_provider == 0, timeout=30))
        self.assertEqual(service._pending_diag, 0)
        self.assertEqual(generator.calls, 0)
        meta = service._load_meta(sid)
        self.assertEqual(meta["provider_job"]["status"], "fallback")
        self.assertEqual(meta["provider_job"]["reason"],
                         "replay_case_failed")
        self.assertEqual(meta["checkpoints"]["end"]["provider_job"]
                         ["reason"], "replay_case_failed")

    def test_lock_queued_end_worker_never_runs_generator_after_cancel(
            self):
        # b: the end worker is queued behind the provider call lock;
        # once released it sees the cancellation before invoking the
        # injected generator.
        from tests.test_demo_service import MultiGenerator
        generator = MultiGenerator()
        service = self._service(None, generator=generator)
        with service._provider_call_lock:
            created = service.create_session(provider_mode="hosted")
            sid, token = (created["session_id"],
                          created["student_token"])
            for row in responses(self.bank, correct=False, count=40):
                service.submit_response(sid, token, row)
            # The worker published "pending" right before entering the
            # adapter scope it is now blocked on.
            self.assertTrue(wait_for(
                lambda: (service._load_meta(sid)["provider_job"]
                         ["status"] == "pending"), timeout=30))
            service.cancel_replay_session(sid)
        self.assertTrue(wait_for(
            lambda: service._pending_provider == 0, timeout=30))
        self.assertEqual(generator.calls, 0)
        meta = service._load_meta(sid)
        self.assertEqual(meta["provider_job"]["status"], "fallback")
        self.assertEqual(meta["provider_job"]["reason"],
                         "replay_case_failed")

    def test_queued_midpoint_never_runs_after_case_failure(self):
        # g: provider worker occupied; the admitted midpoint sits in the
        # queue, the case fails, and the queued call never executes.
        opener = _BlockingOpener(
            self._fakes["response"](self._fakes["midpoint_reply"]()))
        service = self._service(opener, provider_workers=1)
        gate = threading.Event()
        # LIFO: gates must release before service.close(wait=True).
        self.addCleanup(gate.set)
        self.addCleanup(opener.release.set)
        occupied = threading.Event()

        def blocker():
            occupied.set()
            gate.wait(timeout=60)

        service.provider_pool.submit(blocker)
        self.assertTrue(occupied.wait(timeout=10))
        original = service.submit_response
        calls = {"count": 0}

        def wrapped(session_id, token, submission):
            calls["count"] += 1
            if calls["count"] == 22:
                raise RuntimeError("case failure")
            return original(session_id, token, submission)

        with patch.object(service, "submit_response",
                          side_effect=wrapped):
            run = service.replays.start(self._request(service))
            finished = self._wait(service, run["id"])
        self.assertEqual(finished["cases"][0]["status"], "failed")
        gate.set()
        self.assertTrue(wait_for(
            lambda: service._pending_provider == 0, timeout=30))
        self.assertEqual(len(opener.calls), 0)
        meta = service._load_meta(finished["cases"][0]["session_id"])
        self.assertEqual(meta["checkpoints"]["midpoint"]
                         ["phrasing_job"]["status"], "cancelled")
        # A healthy session still schedules and runs its midpoint.
        from tests.test_midpoint_feedback import midpoint_rows
        opener.release.set()
        created = service.create_session(provider_mode="hosted")
        sid, token = created["session_id"], created["student_token"]
        for row in midpoint_rows(self.bank, wrong_positions=(0, 5)):
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(lambda: len(opener.calls) == 1,
                                 timeout=30))
        self.assertTrue(wait_for(
            lambda: (service._load_meta(sid)["checkpoints"]["midpoint"]
                     ["phrasing_job"]["status"] == "ready"),
            timeout=30))

    def test_end_provider_job_late_publication_discarded(self):
        # i: the end review generator is mid-call when the session is
        # cancelled; it finishes but never publishes, and the pending
        # counter still drains exactly once.
        entered = threading.Event()
        release = threading.Event()

        from tests.test_demo_service import MultiGenerator

        class GatedGenerator(MultiGenerator):
            def generate(self, payload):
                entered.set()
                release.wait(timeout=60)
                return super().generate(payload)

        generator = GatedGenerator()
        service = self._service(
            self._fakes["SequenceOpener"](
                [self._fakes["midpoint_reply"]()]),
            generator=generator)
        # LIFO: gates must release before service.close(wait=True).
        self.addCleanup(release.set)
        created = service.create_session(provider_mode="hosted")
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=True, count=40):
            service.submit_response(sid, token, row)
        self.assertTrue(entered.wait(timeout=30))
        service.cancel_replay_session(sid)
        release.set()
        self.assertTrue(wait_for(
            lambda: service._pending_provider == 0, timeout=30))
        meta = service._load_meta(sid)
        self.assertEqual(meta["provider_job"]["status"], "fallback")
        self.assertEqual(meta["provider_job"]["reason"],
                         "replay_case_failed")
        self.assertEqual(meta["checkpoints"]["end"]["provider_job"]
                         ["reason"], "replay_case_failed")
        self.assertEqual(generator.calls, 1)

    def test_waiting_diagnostics_worker_exits_on_cancel(self):
        # i: a worked-first end job parked in the diagnostics wait exits
        # when its session is cancelled, without any hosted call.
        from tests.test_demo_service import MultiGenerator
        generator = MultiGenerator()
        diag_gate = threading.Event()
        real = fake_diagnostics(self.bank)

        class SlowDiagnostics:
            def __init__(self, inner):
                self.inner = inner
            def __getattr__(self, name):
                return getattr(self.inner, name)
            def evaluate(self, bank, taxonomy, rows):
                diag_gate.wait(timeout=60)
                return self.inner.evaluate(bank, taxonomy, rows)

        service = DemoService(
            root=self.root / f"runtime-{secrets.token_hex(4)}",
            bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=SlowDiagnostics(real),
            replay_report=self.report_path,
            replay_source=self.source_path,
            provider_mode="hosted", generator=generator,
            midpoint_generator=None)
        service.cache = self.cache
        self.addCleanup(service.close, wait=True)
        # LIFO: gates must release before service.close(wait=True).
        self.addCleanup(diag_gate.set)
        created = service.create_session(provider_mode="hosted",
                                         feedback_variant="worked_first")
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=True, count=40):
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: (service._load_meta(sid)["provider_job"]["status"]
                     == "waiting_for_diagnostics"), timeout=30))
        service.cancel_replay_session(sid)
        self.assertTrue(wait_for(
            lambda: service._pending_provider == 0, timeout=30))
        self.assertEqual(generator.calls, 0)
        meta = service._load_meta(sid)
        self.assertEqual(meta["provider_job"]["status"], "fallback")
        self.assertEqual(meta["provider_job"]["reason"],
                         "replay_case_failed")


if __name__ == "__main__":
    unittest.main()
