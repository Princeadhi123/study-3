"""Tests for the local synthetic demo service (offline, fake adapters).

No network, credentials, or real model files: providers are injected
test doubles and diagnostics use the FakeKT adapter from the
existing adapter tests.
"""
import json
import threading
import time
import unittest
from pathlib import Path
import tempfile

import phase3_paths  # noqa: F401 -- installs the Phase 2 import path
from demo_service import ConflictError, DemoService
from live_diagnostics import LiveDiagnostics
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_kt_adapter import FakeKT


def fake_diagnostics(bank):
    return LiveDiagnostics(
        model_loader=lambda: FakeKT(bank))


class BrokenDiagnostics:
    def warmup(self):
        return {"status": "unavailable"}

    @property
    def state(self):
        return {"status": "unavailable", "mode": "test",
                "scope_warning": ""}

    def evaluate(self, bank, taxonomy, rows):
        raise RuntimeError("should be sanitized")


class FakeGenerator:
    def __init__(self):
        self.execution = None

    def generate(self, payload):
        self.execution = {"status": "fake_completed"}
        return {"candidate_id":
                payload["selected_candidate"]["candidate_id"],
                "opening": "The draft is ready for review."}


class BlockingSelector:
    """Selector that parks on an Event until the test releases it."""

    def __init__(self, gate):
        self.gate = gate
        self.execution = None
        self.calls = 0

    def select(self, payload):
        self.calls += 1
        self.gate.wait(timeout=20)
        self.execution = {"status": "fake_completed"}
        return {"candidate_id":
                payload["candidates"][0]["candidate_id"]}


class BudgetExhaustedSelector:
    execution = None

    def select(self, payload):
        raise SystemExit("new-call budget exhausted")


def wait_for(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TeacherFailSelector:
    """Student audience selects a non-baseline candidate; teacher fails."""

    def __init__(self):
        self.execution = None

    def select(self, payload):
        if payload["audience"] == "teacher":
            raise RuntimeError("teacher provider down")
        self.execution = {"status": "fake_completed"}
        return {"candidate_id": payload["candidates"][1]["candidate_id"]}


class BudgetAtGeneratorSelector:
    """Selector completes; the generator then exhausts the call budget."""

    def __init__(self):
        self.execution = None

    def select(self, payload):
        self.execution = {"status": "fake_completed"}
        return {"candidate_id":
                payload["candidates"][1]["candidate_id"]}


class BudgetAtGeneratorGenerator:
    def __init__(self):
        self.execution = None

    def generate(self, payload):
        raise SystemExit("new-call budget exhausted")


class DemoServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.service = self._service()

    def tearDown(self):
        self.service.close()

    def _service(self, **kwargs):
        kwargs.setdefault("diagnostics", fake_diagnostics(self.bank))
        service = DemoService(root=Path(self.tmp.name),
                              bank=self.bank, taxonomy=self.taxonomy,
                              **kwargs)
        self.addCleanup(service.close, wait=True)
        return service

    def create(self):
        return self.service.create_session()

    def submit_n(self, session_id, token, count):
        rows = responses(self.bank, count=count)
        last = None
        for row in rows:
            last = self.service.submit_response(session_id, token, row)
        return last

    def complete(self, session_id, token):
        return self.submit_n(session_id, token, 40)

    def job_status(self, session_id):
        return self.service._load_meta(session_id)["provider_job"]

    def wait_job(self, session_id):
        self.assertTrue(wait_for(
            lambda: self.job_status(session_id)["status"]
            in ("ready", "fallback")),
            "provider job did not finish")

    # ---------- submission & snapshot contract ----------

    def test_blocked_provider_keeps_submission_responsive(self):
        gate = threading.Event()
        selector = BlockingSelector(gate)
        service = self._service(
            provider_mode="hosted",
            selector=selector,
            generator=FakeGenerator())
        self.addCleanup(service.close)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        start = time.monotonic()
        snapshot = None
        for row in responses(self.bank, correct=False, count=40):
            snapshot = service.submit_response(sid, token, row)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 10)
        self.assertEqual(snapshot["answered_count"], 40)
        self.assertTrue(wait_for(lambda: selector.calls >= 1),
                        "provider worker never reached the selector")
        self.assertEqual(
            service._load_meta(sid)["provider_job"]["status"],
            "pending")
        self.assertIsNone(snapshot["feedback"]["end"])
        self.assertEqual(snapshot["feedback_delivery"]["status"],
                         "awaiting_teacher_review")
        gate.set()
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            == "ready"))

    def test_duplicate_identical_response_is_idempotent(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        row = responses(self.bank, count=1)[0]
        first = self.service.submit_response(sid, token, row)
        second = self.service.submit_response(sid, token, row)
        self.assertEqual(first["answered_count"], 1)
        self.assertEqual(second["answered_count"], 1)
        self.assertEqual(first, second)
        changed = dict(row, selected_index=(row["selected_index"] + 1) % 3)
        with self.assertRaises(ValueError):
            self.service.submit_response(sid, token, changed)

    def test_concurrent_reads_do_not_lose_ordered_updates(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        errors = []
        done = threading.Event()

        def reader():
            while not done.is_set():
                try:
                    self.service.snapshot(sid, token)
                except Exception as exc:
                    errors.append(exc)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        self.complete(sid, token)
        done.set()
        thread.join(timeout=5)
        self.assertEqual(errors, [])
        snapshot = self.service.snapshot(sid, token)
        self.assertEqual(snapshot["answered_count"], 40)
        self.assertEqual(snapshot["status"], "complete")

    def test_midpoint_snapshot_has_no_counts_or_diagnostics(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        snapshot = self.submit_n(sid, token, 20)
        self.assertEqual(snapshot["answered_count"], 20)
        midpoint = snapshot["feedback"]["midpoint"]
        self.assertIsNotNone(midpoint)
        self.assertEqual(set(midpoint), {"sections", "text"})
        blob = json.dumps(snapshot)
        for marker in ("p_correct", "conformal", "answer_index",
                       "selected_index", "bank_sha256", "candidates",
                       "subtopic", "out_of", "kt"):
            self.assertNotIn(marker, blob)
        self.assertIsNone(snapshot["feedback"]["end"])
        self.assertEqual(snapshot["provider_job"]["status"],
                         "not_requested")

    def test_cross_session_token_rejected(self):
        first = self.create()
        second = self.create()
        with self.assertRaises(PermissionError):
            self.service.snapshot(second["session_id"],
                                  first["student_token"])
        row = responses(self.bank, count=1)[0]
        with self.assertRaises(PermissionError):
            self.service.submit_response(
                second["session_id"], first["student_token"], row)

    # ---------- provider job behaviour ----------

    def test_rules_mode_job_resolves_ready(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        self.complete(sid, token)
        self.wait_job(sid)
        meta = self.service._load_meta(sid)
        self.assertEqual(meta["provider_job"]["status"], "ready")
        self.assertEqual(meta["provider_job"]["provider_mode"], "rules")
        end = meta["checkpoints"]["end"]
        self.assertIsNotNone(end["student_review"])
        self.assertIsNotNone(end["teacher_review"])
        self.assertIsNone(
            end["student_review"]["trace"]["fallback_reason"])

    def test_budget_exhaustion_falls_back_not_pending(self):
        service = self._service(
            provider_mode="hosted",
            selector=BudgetExhaustedSelector(),
            generator=FakeGenerator())
        self.addCleanup(service.close)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            == "fallback"))
        meta = service._load_meta(sid)
        review = meta["checkpoints"]["end"]["student_review"]
        self.assertEqual(review["trace"]["fallback_reason"],
                         "demo_call_budget_exhausted")
        executions = meta["provider_job"]["executions"]
        self.assertEqual(executions["student"]["selector"]["status"],
                         "budget_exhausted")

    def test_budget_after_selector_keeps_completed_record(self):
        service = self._service(
            provider_mode="hosted",
            selector=BudgetAtGeneratorSelector(),
            generator=BudgetAtGeneratorGenerator())
        self.addCleanup(service.close)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            == "fallback"))
        job = service._load_meta(sid)["provider_job"]
        student = job["executions"]["student"]
        self.assertEqual(student["selector"]["status"],
                         "fake_completed")
        self.assertEqual(student["generator"]["status"],
                         "budget_exhausted")
        review = service._load_meta(sid)["checkpoints"]["end"][
            "student_review"]
        self.assertEqual(review["trace"]["fallback_reason"],
                         "demo_call_budget_exhausted")

    def test_single_candidate_emits_local_status(self):
        service = self._service(
            provider_mode="hosted",
            selector=BlockingSelector(threading.Event()),
            generator=FakeGenerator())
        self.addCleanup(service.close)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, count=40):  # all correct: 1 candidate
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        job = service._load_meta(sid)["provider_job"]
        self.assertEqual(
            job["executions"]["student"]["selector"]["status"],
            "local_single_candidate")
        self.assertEqual(
            job["executions"]["teacher"]["selector"]["status"],
            "local_single_candidate")

    def test_partial_fallback_keeps_student_review_public(self):
        service = self._service(
            provider_mode="hosted",
            selector=TeacherFailSelector(),
            generator=FakeGenerator())
        self.addCleanup(service.close)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            == "fallback"))
        meta = service._load_meta(sid)
        end = meta["checkpoints"]["end"]
        student_review = end["student_review"]
        self.assertIsNone(
            student_review["trace"]["fallback_reason"])
        self.assertNotEqual(
            student_review["selected_candidate_id"],
            student_review["baseline_candidate_id"])
        self.assertEqual(end["teacher_review"]["trace"]
                         ["fallback_reason"], "selector_error")
        job = meta["provider_job"]
        self.assertEqual(job["executions"]["teacher"]["generator"]
                         ["status"], "skipped_selector_failure")
        snapshot = service.snapshot(sid, token)
        self.assertEqual(snapshot["provider_job"]["status"],
                         "fallback")
        self.assertIsNone(snapshot["feedback"]["end"])
        self.assertEqual(snapshot["feedback_delivery"]["status"],
                         "awaiting_teacher_review")
        view = service.teacher_session(sid)
        delivery = view["feedback_delivery"]
        for section in student_review["message"]["sections"]:
            self.assertIn(section["text"],
                          delivery["preview"]["text"])
        service.release_feedback(sid, {
            "message_sha256": delivery["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["text"], delivery["preview"]["text"])
        self.assertFalse(released["requires_educator_review"])

    def test_diagnostics_receive_admission_snapshot_rows(self):
        release = threading.Event()
        seen = []

        def model_loader():
            self.assertTrue(release.wait(timeout=30))
            return FakeKT(self.bank)

        diagnostics = LiveDiagnostics(
            model_loader=model_loader)
        real_evaluate = diagnostics.evaluate

        def spy(bank, taxonomy, rows):
            seen.append(len(rows))
            return real_evaluate(bank, taxonomy, rows)

        diagnostics.evaluate = spy
        service = self._service(diagnostics=diagnostics)
        self.addCleanup(service.close)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            service.submit_response(sid, token, row)
        release.set()
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["end"]
            ["diagnostics_job"]["status"] == "ready", timeout=30))
        self.assertEqual(seen, [20, 40])
        meta = service._load_meta(sid)
        midpoint = meta["checkpoints"]["midpoint"]["diagnostics"]
        end = meta["checkpoints"]["end"]["diagnostics"]
        self.assertEqual(midpoint["checkpoint"], "midpoint")
        self.assertEqual(midpoint["answer_count"], 20)
        self.assertNotIn("conformal", midpoint)
        self.assertEqual(len(midpoint["kt"]["items"]), 20)
        self.assertEqual(end["checkpoint"], "end")
        self.assertEqual(end["answer_count"], 40)
        self.assertNotIn("conformal", end)
        self.assertEqual(len(end["kt"]["items"]), 40)

    def test_graph_stored_at_checkpoints_without_model(self):
        service = self._service(diagnostics=BrokenDiagnostics())
        self.addCleanup(service.close)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        self.complete_with(service, sid, token)
        meta = service._load_meta(sid)
        midpoint = meta["checkpoints"]["midpoint"]
        self.assertIsNotNone(midpoint["graph"])
        self.assertEqual(midpoint["graph"]["checkpoint"], "midpoint")
        self.assertEqual(sum(t["out_of"] for t in
                             midpoint["graph"]["topics"]), 20)
        end = meta["checkpoints"]["end"]
        self.assertEqual(sum(t["out_of"] for t in
                             end["graph"]["topics"]), 40)
        self.assertTrue(wait_for(lambda: service._load_meta(sid)
                                 ["checkpoints"]["end"]
                                 ["diagnostics_job"]["status"]
                                 == "unavailable"))

    def test_queue_capacity_rejections(self):
        gate = threading.Event()
        selector = BlockingSelector(gate)
        service = self._service(
            provider_mode="hosted",
            selector=selector,
            generator=FakeGenerator(),
            diagnostics=BrokenDiagnostics(),
            max_pending_jobs=1)
        self.addCleanup(service.close)
        # Occupy the single provider slot (all-incorrect evidence has
        # multiple candidates, so the selector really blocks).
        first = service.create_session()
        for row in responses(self.bank, correct=False, count=40):
            service.submit_response(
                first["session_id"], first["student_token"], row)
        self.assertTrue(wait_for(lambda: selector.calls >= 1))
        # Second session exceeds provider admission capacity.
        second = service.create_session()
        sid2, token2 = second["session_id"], second["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            service.submit_response(sid2, token2, row)
        meta2 = service._load_meta(sid2)
        self.assertEqual(meta2["provider_job"]["status"], "fallback")
        self.assertEqual(meta2["provider_job"]["reason"],
                         "demo_provider_queue_capacity")
        end = meta2["checkpoints"]["end"]
        self.assertEqual(end["student_review"]["trace"]
                         ["fallback_reason"],
                         "demo_provider_queue_capacity")
        self.assertIsNotNone(end["baseline_student"])
        self.assertIsNotNone(end["graph"])
        gate.set()
        self.assertTrue(wait_for(
            lambda: service._load_meta(first["session_id"])
            ["provider_job"]["status"] in ("ready", "fallback")))

    def test_diagnostics_queue_capacity(self):
        gate = threading.Event()

        class BlockingDiag:
            @property
            def state(self):
                return {"status": "ready", "mode": "t",
                        "scope_warning": ""}

            def warmup(self):
                return {"status": "ready"}

            def evaluate(self, bank, taxonomy, rows):
                gate.wait(timeout=30)
                return {"status": "unavailable", "reason": "test"}

        service = self._service(diagnostics=BlockingDiag(),
                                max_pending_jobs=1)
        self.addCleanup(service.close)
        first = service.create_session()
        self.submit_n_with(service, first["session_id"],
                           first["student_token"], 20)
        self.assertTrue(wait_for(
            lambda: service._load_meta(first["session_id"])
            ["checkpoints"]["midpoint"]["diagnostics_job"]["status"]
            == "pending"))
        time.sleep(0.3)  # let the worker enter evaluate and block
        second = service.create_session()
        sid2, token2 = second["session_id"], second["student_token"]
        for row in responses(self.bank, count=20):
            service.submit_response(sid2, token2, row)
        job2 = service._load_meta(sid2)["checkpoints"]["midpoint"][
            "diagnostics_job"]
        self.assertEqual(job2["status"], "unavailable")
        self.assertEqual(job2["reason"],
                         "demo_diagnostics_queue_capacity")
        gate.set()

    def submit_n_with(self, service, sid, token, count):
        for row in responses(self.bank, count=count):
            service.submit_response(sid, token, row)

    def test_submission_shape_validated_before_duplicate(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        row = responses(self.bank, count=1)[0]
        self.service.submit_response(sid, token, row)
        for bad in (
                dict(row, extra=1),
                {k: v for k, v in row.items()
                 if k != "selected_index"},
                dict(row, selected_index=True),
                dict(row, selected_index="0"),
                dict(row, question_id=None),
                "not a dict",
                None):
            with self.assertRaises(ValueError):
                self.service.submit_response(sid, token, bad)
        # Identical retry still resolves to the current snapshot.
        snapshot = self.service.submit_response(sid, token, row)
        self.assertEqual(snapshot["answered_count"], 1)

    def test_incompatible_meta_skipped_not_rewritten(self):
        created = self.create()
        sid = created["session_id"]
        path = Path(self.tmp.name) / "metadata" / f"{sid}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema"] = "wrong_schema"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.service._load_meta(sid)
        self.assertNotIn(sid, [row["session_id"]
                               for row in self.service.list_sessions()])
        self.service.close()
        restarted = self._service()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))
                         ["schema"], "wrong_schema")
        with self.assertRaises(ValueError):
            restarted.teacher_session(sid)
        restarted.close()
        self.service = restarted

    def test_review_judgment_requires_string_choice(self):
        sid = self.finish_ready()
        view = self.service.teacher_session(sid)
        digest = view["review_hashes"]["student"]
        bad = self.judgments()
        bad["scope"] = ["no_concern"]
        with self.assertRaises(ValueError):
            self.service.add_review(sid, {
                "audience": "student", "message_sha256": digest,
                "judgments": bad, "note": ""})

    def complete_with(self, service, sid, token):
        for row in responses(self.bank, count=40):
            service.submit_response(sid, token, row)

    def test_diagnostics_failure_is_unavailable_sanitized(self):
        service = self._service(diagnostics=BrokenDiagnostics())
        self.addCleanup(service.close)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        self.complete_with(service, sid, token)
        self.assertTrue(wait_for(lambda: service._load_meta(sid)
                                 ["checkpoints"]["end"]
                                 ["diagnostics_job"]["status"]
                                 == "unavailable"))
        meta = service._load_meta(sid)
        diag = meta["checkpoints"]["end"]["diagnostics"]
        self.assertEqual(diag["status"], "unavailable")
        self.assertNotIn("should be sanitized", json.dumps(diag))
        self.assertNotIn("p_correct_before_each_answer",
                         json.dumps(diag))

    def test_diagnostics_ready_and_private_only(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        self.complete(sid, token)
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["checkpoints"]["end"]
            ["diagnostics_job"]["status"] == "ready"))
        view = self.service.teacher_session(sid)
        diag = view["checkpoints"]["end"]["diagnostics"]
        self.assertEqual(diag["status"], "ready")
        self.assertEqual(
            len(diag["kt"]["p_correct_before_each_answer"]), 40)
        self.assertFalse(diag["used_for_student_advice"])
        public = self.service.snapshot(sid, token)
        blob = json.dumps(public)
        for marker in ("p_correct", "conformal", "kt", "diagnostics",
                       "answer_index", "subtopic"):
            self.assertNotIn(marker, blob)

    # ---------- restart ----------

    def test_restart_preserves_ready_results_and_reviews(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        self.complete(sid, token)
        self.wait_job(sid)
        view = self.service.teacher_session(sid)
        digest = view["review_hashes"]["student"]
        judgments = {field["id"]: field["choices"][0] for field in
                     view["review_contract"]["fields"]}
        self.service.add_review(sid, {
            "audience": "student", "message_sha256": digest,
            "judgments": judgments, "note": "checked"})
        self.service.close(wait=True)
        restarted = self._service()
        view2 = restarted.teacher_session(sid)
        self.assertEqual(len(view2["reviews"]), 1)
        self.assertEqual(view2["provider_job"]["status"], "ready")
        self.assertIsNotNone(
            view2["checkpoints"]["end"]["student_review"])
        restarted.close()
        self.service = restarted

    def test_restart_marks_pending_jobs_interrupted(self):
        gate = threading.Event()
        first = self._service(
            provider_mode="hosted",
            selector=BlockingSelector(gate),
            generator=FakeGenerator())
        created = first.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            first.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: first._load_meta(sid)["provider_job"]["status"]
            == "pending"))
        first.close(wait=False)
        restarted = self._service()
        meta = restarted._load_meta(sid)
        self.assertEqual(meta["provider_job"]["status"], "fallback")
        self.assertTrue(meta["provider_job"]["interrupted"])
        end = meta["checkpoints"]["end"]
        self.assertEqual(end["student_review"]["trace"]
                         ["fallback_reason"], "interrupted_by_restart")
        self.assertIsNotNone(end["baseline_student"])
        restarted.close()
        gate.set()
        self.service = restarted

    # ---------- reviews ----------

    def finish_ready(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        self.complete(sid, token)
        self.wait_job(sid)
        return sid

    def judgments(self):
        return {"evidence_support": "supported",
                "method_content": "appropriate",
                "audience_tone": "appropriate",
                "taxonomy": "appropriate",
                "scope": "no_concern"}

    def test_review_binding_and_ledger(self):
        sid = self.finish_ready()
        view = self.service.teacher_session(sid)
        digest = view["review_hashes"]["student"]
        note = "Focus wording <b>fine</b> here."
        result = self.service.add_review(sid, {
            "audience": "student", "message_sha256": digest,
            "judgments": self.judgments(), "note": note,
            "reviewer_label": "reviewer-1"})
        self.assertTrue(result["recorded"])
        entry = result["review"]
        self.assertEqual(entry["note"], note)
        self.assertFalse(entry["scored"])
        self.assertFalse(entry["approves_learner_delivery"])
        view = self.service.teacher_session(sid)
        self.assertEqual(view["reviews"][0]["note"], note)
        end = view["checkpoints"]["end"]
        self.assertTrue(end["student_review"]["requires_human_review"])
        self.assertFalse(end["student_review"]["trace"]
                         ["provider_advantage_demonstrated"])

    def test_stale_review_hash_rejected_with_current(self):
        sid = self.finish_ready()
        view = self.service.teacher_session(sid)
        current = view["review_hashes"]["teacher"]
        with self.assertRaises(ConflictError) as ctx:
            self.service.add_review(sid, {
                "audience": "teacher",
                "message_sha256": "0" * 64,
                "judgments": self.judgments(), "note": ""})
        self.assertEqual(ctx.exception.current, current)

    def test_review_requires_completion_and_valid_judgments(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        self.submit_n(sid, token, 20)
        with self.assertRaises(ValueError):
            self.service.add_review(sid, {
                "audience": "student", "message_sha256": "a" * 64,
                "judgments": self.judgments(), "note": ""})
        sid = self.finish_ready()
        view = self.service.teacher_session(sid)
        digest = view["review_hashes"]["student"]
        bad = self.judgments()
        bad["scope"] = "brilliant"
        with self.assertRaises(ValueError):
            self.service.add_review(sid, {
                "audience": "student", "message_sha256": digest,
                "judgments": bad, "note": ""})
        missing = self.judgments()
        del missing["taxonomy"]
        with self.assertRaises(ValueError):
            self.service.add_review(sid, {
                "audience": "student", "message_sha256": digest,
                "judgments": missing, "note": ""})

    def _preview_sha(self, sid):
        return self.service.teacher_session(sid)[
            "feedback_delivery"]["preview_sha256"]

    def test_incomplete_session_has_no_preview_or_release(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        snapshot = self.submit_n(sid, token, 20)
        self.assertIsNone(snapshot["feedback"]["end"])
        self.assertEqual(snapshot["feedback_delivery"]["status"],
                         "not_ready")
        view = self.service.teacher_session(sid)
        self.assertIsNone(view["feedback_delivery"]["preview"])
        self.assertIsNone(view["feedback_delivery"]["preview_sha256"])
        with self.assertRaises(ValueError):
            self.service.release_feedback(
                sid, {"message_sha256": "a" * 64})

    def test_completed_end_hidden_until_release(self):
        created = self.create()
        sid2, token2 = created["session_id"], created["student_token"]
        self.complete(sid2, token2)
        self.wait_job(sid2)
        snapshot = self.service.snapshot(sid2, token2)
        self.assertIsNone(snapshot["feedback"]["end"])
        self.assertEqual(snapshot["feedback_delivery"]["status"],
                         "awaiting_teacher_review")
        blob = json.dumps(snapshot)
        for marker in ("preview_sha256", "released_feedback",
                       "reviewer_label", "p_correct", "answer_index"):
            self.assertNotIn(marker, blob)
        view = self.service.teacher_session(sid2)
        preview = view["feedback_delivery"]["preview"]
        self.assertEqual(
            preview["template_version"],
            "phase3_student_feedback_template_v1")
        self.assertTrue(preview["requires_educator_review"])
        result = self.service.release_feedback(sid2, {
            "message_sha256": view["feedback_delivery"]["preview_sha256"],
            "reviewer_label": "teacher-a"})
        self.assertTrue(result["released"])
        snapshot = self.service.snapshot(sid2, token2)
        end = snapshot["feedback"]["end"]
        self.assertEqual(end["template_version"],
                         "phase3_student_feedback_template_v1")
        self.assertFalse(end["requires_educator_review"])
        self.assertEqual(snapshot["feedback_delivery"]["status"],
                         "released")
        meta = self.service._load_meta(sid2)
        self.assertEqual(meta["feedback_release"]["reviewer_label"],
                         "teacher-a")
        after = self.service.teacher_session(sid2)["feedback_delivery"]
        self.assertEqual(after["preview_sha256"],
                         after["released_preview_sha256"])
        self.assertNotIn("reviewer_label",
                         json.dumps(snapshot["feedback_delivery"]))

    def test_review_note_alone_never_releases(self):
        created = self.create()
        sid2, token2 = created["session_id"], created["student_token"]
        self.complete(sid2, token2)
        self.wait_job(sid2)
        view = self.service.teacher_session(sid2)
        self.service.add_review(sid2, {
            "audience": "student",
            "message_sha256": view["review_hashes"]["student"],
            "judgments": self.judgments(), "note": "checked"})
        snapshot = self.service.snapshot(sid2, token2)
        self.assertIsNone(snapshot["feedback"]["end"])
        self.assertEqual(snapshot["feedback_delivery"]["status"],
                         "awaiting_teacher_review")

    def test_template_four_fixed_headings_and_wording(self):
        cases = {
            "all_correct": responses(self.bank, count=40),
            "all_incorrect": responses(self.bank, correct=False,
                                       count=40)}
        tied = responses(self.bank, count=40)
        for index in (0, 10):
            row = tied[index]
            q = self.bank["questions"][index]
            tied[index] = {"question_id": row["question_id"],
                           "selected_index":
                               (q["answer_index"] + 1) % 3}
        cases["tied"] = tied
        titles = ["Assessment summary", "Observed strengths",
                  "Review focus", "Next steps"]
        for name, rows in cases.items():
            created = self.create()
            sid, token = (created["session_id"],
                          created["student_token"])
            last = None
            for row in rows:
                last = self.service.submit_response(sid, token, row)
            self.wait_job(sid)
            preview = self.service.teacher_session(sid)[
                "feedback_delivery"]["preview"]
            self.assertEqual([s["title"] for s in preview["sections"]],
                             titles, name)
            self.assertTrue(all(s["text"] for s in
                                preview["sections"]), name)
            meta = self.service._load_meta(sid)
            review = (meta["checkpoints"]["end"]["student_review"]
                      or meta["checkpoints"]["end"]["baseline_student"])
            for section in review["message"]["sections"]:
                self.assertIn(section["text"], preview["text"], name)
            self.assertIsNone(last["feedback"]["end"])

    def test_assessment_total_absent_until_complete(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        self.assertIsNone(created["snapshot"]["assessment_total"])
        mid = self.submit_n(sid, token, 20)
        self.assertIsNone(mid["assessment_total"])
        self.assertIsNone(
            self.service.snapshot(sid, token)["assessment_total"])

    def test_assessment_total_counts_observed_answers(self):
        alternating = responses(self.bank, count=40)
        for index in range(1, 40, 2):
            question = self.bank["questions"][index]
            alternating[index] = {
                "question_id": question["question_id"],
                "selected_index": (question["answer_index"] + 1) % 3}
        for rows, expected in (
                (responses(self.bank, count=40), 40),
                (responses(self.bank, correct=False, count=40), 0),
                (alternating, 20)):
            created = self.create()
            sid, token = (created["session_id"],
                          created["student_token"])
            last = None
            for row in rows:
                last = self.service.submit_response(sid, token, row)
            self.assertEqual(last["assessment_total"],
                             {"correct": expected, "out_of": 40})
            self.assertIsNone(last["feedback"]["end"])
            snap = self.service.snapshot(sid, token)
            self.assertEqual(snap["assessment_total"],
                             {"correct": expected, "out_of": 40})
            blob = json.dumps(snap)
            for marker in ("skills", "preview", "reviewer"):
                self.assertNotIn(marker, blob)

    def test_assessment_total_immediate_while_provider_pending(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        selector = BlockingSelector(gate)
        service = self._service(
            provider_mode="hosted",
            selector=selector,
            generator=FakeGenerator())
        self.addCleanup(service.close)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        last = None
        for row in responses(self.bank, correct=False, count=40):
            last = service.submit_response(sid, token, row)
        self.assertEqual(last["assessment_total"],
                         {"correct": 0, "out_of": 40})
        self.assertIsNone(last["feedback"]["end"])
        self.assertTrue(wait_for(lambda: selector.calls >= 1))
        self.assertEqual(
            service._load_meta(sid)["provider_job"]["status"],
            "pending")
        view = service.teacher_session(sid)
        service.release_feedback(sid, {
            "message_sha256":
                view["feedback_delivery"]["preview_sha256"]})
        snap = service.snapshot(sid, token)
        self.assertEqual(snap["assessment_total"],
                         {"correct": 0, "out_of": 40})
        self.assertIsNotNone(snap["feedback"]["end"])

    def test_release_rejects_extra_or_stale_fields(self):
        sid = self.finish_ready()
        sha = self._preview_sha(sid)
        with self.assertRaises(ValueError):
            self.service.release_feedback(sid, {
                "message_sha256": sha, "text": "client supplied"})
        with self.assertRaises(ValueError):
            self.service.release_feedback(sid, {})
        with self.assertRaises(ValueError):
            self.service.release_feedback(sid, {
                "message_sha256": "zz" + "0" * 62})
        with self.assertRaises(ConflictError) as ctx:
            self.service.release_feedback(sid, {
                "message_sha256": "0" * 64})
        self.assertEqual(ctx.exception.current, sha)

    def test_repeated_release_preserves_timestamp(self):
        sid = self.finish_ready()
        sha = self._preview_sha(sid)
        first = self.service.release_feedback(
            sid, {"message_sha256": sha})
        second = self.service.release_feedback(
            sid, {"message_sha256": sha})
        self.assertEqual(first["released_at"], second["released_at"])

    def test_baseline_release_survives_later_provider_result(self):
        gate = threading.Event()
        selector = BlockingSelector(gate)
        service = self._service(
            provider_mode="hosted", selector=selector,
            generator=FakeGenerator())
        self.addCleanup(service.close)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(lambda: selector.calls >= 1))
        view = service.teacher_session(sid)
        baseline_sha = view["feedback_delivery"]["preview_sha256"]
        service.release_feedback(sid, {"message_sha256": baseline_sha})
        baseline_release = service._load_meta(sid)["feedback_release"]
        gate.set()
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            == "ready"))
        snapshot = service.snapshot(sid, token)
        self.assertEqual(snapshot["feedback"]["end"],
                         baseline_release["released_feedback"])
        view = service.teacher_session(sid)
        new_sha = view["feedback_delivery"]["preview_sha256"]
        self.assertNotEqual(new_sha, baseline_sha)
        again = service.release_feedback(
            sid, {"message_sha256": new_sha})
        self.assertTrue(again["released"])
        updated = service._load_meta(sid)["feedback_release"]
        self.assertEqual(updated["preview_sha256"], new_sha)
        self.assertEqual(updated["released_feedback"]["text"],
                         view["feedback_delivery"]["preview"]["text"])

    def test_release_persists_across_restart(self):
        created = self.create()
        sid2, token2 = created["session_id"], created["student_token"]
        self.complete(sid2, token2)
        self.wait_job(sid2)
        sha = self._preview_sha(sid2)
        self.service.release_feedback(sid2, {"message_sha256": sha})
        released = self.service._load_meta(sid2)["feedback_release"]
        self.service.close(wait=True)
        restarted = self._service()
        snapshot = restarted.snapshot(sid2, token2)
        self.assertEqual(snapshot["feedback"]["end"],
                         released["released_feedback"])
        self.assertEqual(snapshot["feedback_delivery"]["status"],
                         "released")
        self.assertEqual(restarted.teacher_session(sid2)
                         ["feedback_delivery"]["released_at"],
                         released["released_at"])
        restarted.close()
        self.service = restarted

    def test_old_metadata_without_release_stays_unreleased(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        self.complete(sid, token)
        self.wait_job(sid)
        path = Path(self.tmp.name) / "metadata" / f"{sid}.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        del meta["feedback_release"]
        path.write_text(json.dumps(meta), encoding="utf-8")
        snapshot = self.service.snapshot(sid, token)
        self.assertIsNone(snapshot["feedback"]["end"])
        self.assertEqual(snapshot["feedback_delivery"]["status"],
                         "awaiting_teacher_review")

    def test_queue_capacity_fallback_preview_can_be_released(self):
        gate = threading.Event()
        selector = BlockingSelector(gate)
        service = self._service(
            provider_mode="hosted", selector=selector,
            generator=FakeGenerator(), max_pending_jobs=1)
        self.addCleanup(service.close)
        first = service.create_session()
        for row in responses(self.bank, correct=False, count=40):
            service.submit_response(
                first["session_id"], first["student_token"], row)
        self.assertTrue(wait_for(lambda: selector.calls >= 1))
        second = service.create_session()
        sid2, token2 = second["session_id"], second["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            service.submit_response(sid2, token2, row)
        view = service.teacher_session(sid2)
        sha = view["feedback_delivery"]["preview_sha256"]
        service.release_feedback(sid2, {"message_sha256": sha})
        snapshot = service.snapshot(sid2, token2)
        self.assertEqual(snapshot["feedback_delivery"]["status"],
                         "released")
        self.assertIsNotNone(snapshot["feedback"]["end"])
        gate.set()

    # ---------- simulation ----------

    def test_simulation_creates_fresh_session(self):
        result = self.service.simulate("alternating", 7)
        sid = result["session_id"]
        self.assertTrue(
            (Path(self.tmp.name) / "sessions" / f"{sid}.json").exists())
        view = self.service.teacher_session(sid)
        self.assertEqual(view["answered_count"], 40)
        self.assertEqual(view["status"], "complete")
        self.assertEqual(len(view["question_responses"]), 40)
        self.assertEqual(view["simulated"]["profile"], "alternating")
        self.assertIn("not a diagnosis",
                      view["simulated"]["label"])
        self.assertIn(f"?session={sid}#token=",
                      result["student_url"])
        with self.assertRaises(ValueError):
            self.service.simulate("not_a_profile", 1)
        with self.assertRaises(ValueError):
            self.service.simulate("all_correct", "x")

    def test_fractions_profile_requires_named_skill(self):
        # weak_fractions_only targets the single skill named
        # "Murtoluvut"; the generic fixture bank has none.
        with self.assertRaises(ValueError):
            self.service.simulate("weak_fractions_only", 7)

    def _completed(self, correct=True, count=40):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=correct, count=count):
            self.service.submit_response(sid, token, row)
        return sid, token

    def _edit_sections(self, marker="Teacher rephrased narrative."):
        return {
            "assessment_summary":
                f"Assessment summary. {marker}",
            "observed_strengths":
                f"Observed strengths. {marker}",
            "review_focus": f"Review focus. {marker}",
            "next_steps": f"Next steps. {marker}"}

    def test_edit_requires_completed_session(self):
        created = self.create()
        sid = created["session_id"]
        self.submit_n(sid, created["student_token"], 20)
        with self.assertRaises(ValueError):
            self.service.save_feedback_edit(sid, {
                "message_sha256": "0" * 64,
                "sections": self._edit_sections()})

    def test_edit_stale_hash_and_extra_fields_rejected(self):
        sid, _ = self._completed()
        view = self.service.teacher_session(sid)
        sha = view["feedback_delivery"]["preview_sha256"]
        with self.assertRaises(ConflictError) as ctx:
            self.service.save_feedback_edit(sid, {
                "message_sha256": "f" * 64,
                "sections": self._edit_sections()})
        self.assertEqual(ctx.exception.current, sha)
        for extra in ({"total": {"correct": 1, "out_of": 40}},
                      {"skills": []},
                      {"template_version": "x"},
                      {"feedback": {"end": "x"}}):
            with self.assertRaises(ValueError):
                self.service.save_feedback_edit(sid, {
                    "message_sha256": sha,
                    "sections": self._edit_sections(), **extra})
        bad_sections = self._edit_sections()
        bad_sections["next_steps"] = 123
        with self.assertRaises(ValueError):
            self.service.save_feedback_edit(sid, {
                "message_sha256": sha, "sections": bad_sections})
        bad_sections = self._edit_sections()
        bad_sections["extra"] = "x"
        with self.assertRaises(ValueError):
            self.service.save_feedback_edit(sid, {
                "message_sha256": sha, "sections": bad_sections})

    def test_edit_saved_private_total_unchanged_no_release(self):
        sid, token = self._completed()
        view = self.service.teacher_session(sid)
        delivery = view["feedback_delivery"]
        original = delivery["preview"]
        res = self.service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "sections": self._edit_sections(),
            "reviewer_label": "ms-test"})
        self.assertTrue(res["saved"])
        self.assertNotEqual(res["preview_sha256"],
                            delivery["preview_sha256"])
        view = self.service.teacher_session(sid)
        delivery = view["feedback_delivery"]
        self.assertEqual(delivery["preview_sha256"],
                         res["preview_sha256"])
        self.assertEqual(delivery["preview_source"], "teacher_edited")
        self.assertEqual(delivery["edit_reviewer_label"], "ms-test")
        self.assertEqual(delivery["preview"]["total"],
                         original["total"])
        self.assertEqual(delivery["preview"]["skills"],
                         original["skills"])
        self.assertTrue(
            delivery["preview"]["requires_educator_review"])
        self.assertEqual(delivery["preview"]["sections"][0]["text"],
                         "Assessment summary. Teacher rephrased "
                         "narrative.")
        snap = self.service.snapshot(sid, token)
        self.assertIsNone(snap["feedback"]["end"])
        self.assertEqual(snap["feedback_delivery"]["status"],
                         "awaiting_teacher_review")
        self.assertNotIn("preview_source",
                         snap["feedback_delivery"])

    def test_release_after_edit_uses_edited_preview(self):
        sid, token = self._completed()
        view = self.service.teacher_session(sid)
        self.service.save_feedback_edit(sid, {
            "message_sha256":
                view["feedback_delivery"]["preview_sha256"],
            "sections": self._edit_sections()})
        view = self.service.teacher_session(sid)
        delivery = view["feedback_delivery"]
        self.service.release_feedback(sid, {
            "message_sha256": delivery["preview_sha256"]})
        snap = self.service.snapshot(sid, token)
        end = snap["feedback"]["end"]
        self.assertEqual(end["sections"],
                         delivery["preview"]["sections"])
        self.assertFalse(end["requires_educator_review"])
        self.assertIn("Teacher rephrased narrative.", end["text"])

    def test_edit_after_release_keeps_student_snapshot(self):
        sid, token = self._completed()
        view = self.service.teacher_session(sid)
        sha = view["feedback_delivery"]["preview_sha256"]
        self.service.release_feedback(
            sid, {"message_sha256": sha})
        released_text = self.service.snapshot(
            sid, token)["feedback"]["end"]["text"]
        self.service.save_feedback_edit(sid, {
            "message_sha256": sha,
            "sections": self._edit_sections("Second draft.")})
        snap = self.service.snapshot(sid, token)
        self.assertEqual(snap["feedback"]["end"]["text"],
                         released_text)
        self.assertEqual(snap["feedback_delivery"]["status"],
                         "released")
        view = self.service.teacher_session(sid)
        self.assertNotEqual(
            view["feedback_delivery"]["preview_sha256"], sha)

    def test_edit_persists_across_restart(self):
        sid, token = self._completed()
        view = self.service.teacher_session(sid)
        res = self.service.save_feedback_edit(sid, {
            "message_sha256":
                view["feedback_delivery"]["preview_sha256"],
            "sections": self._edit_sections(),
            "reviewer_label": "ms-test"})
        restarted = self._service()
        self.addCleanup(restarted.close)
        view = restarted.teacher_session(sid)
        delivery = view["feedback_delivery"]
        self.assertEqual(delivery["preview_sha256"],
                         res["preview_sha256"])
        self.assertEqual(delivery["preview_source"], "teacher_edited")
        restarted.release_feedback(
            sid, {"message_sha256": res["preview_sha256"]})
        snap = restarted.snapshot(sid, token)
        self.assertIn("Teacher rephrased narrative.",
                      snap["feedback"]["end"]["text"])

    def test_pending_provider_edit_survives_provider_result(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        selector = BlockingSelector(gate)
        service = self._service(provider_mode="hosted",
                                selector=selector,
                                generator=FakeGenerator())
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(lambda: selector.calls >= 1))
        self.assertEqual(service._load_meta(sid)["provider_job"]
                         ["status"], "pending")
        view = service.teacher_session(sid)
        res = service.save_feedback_edit(sid, {
            "message_sha256":
                view["feedback_delivery"]["preview_sha256"],
            "sections": self._edit_sections(),
            "reviewer_label": "ms-test"})
        gate.set()
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            != "pending"))
        view = service.teacher_session(sid)
        delivery = view["feedback_delivery"]
        self.assertEqual(delivery["preview_sha256"],
                         res["preview_sha256"])
        self.assertEqual(delivery["preview_source"], "teacher_edited")
        self.assertIn("Teacher rephrased narrative.",
                      delivery["preview"]["text"])
        snap = service.snapshot(sid, token)
        self.assertIsNone(snap["feedback"]["end"])


if __name__ == "__main__":
    unittest.main()
