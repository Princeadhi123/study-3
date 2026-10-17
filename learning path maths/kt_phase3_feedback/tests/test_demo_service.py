"""Tests for the local synthetic demo service (offline, fake adapters).

No network, credentials, or real model files: providers are injected
test doubles and diagnostics use the FakeKT adapter from the
existing adapter tests.
"""
import copy
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
        self.audiences = []

    def select(self, payload):
        self.audiences.append(payload["audience"])
        if payload["audience"] == "teacher":
            raise RuntimeError("teacher provider down")
        self.execution = {"status": "fake_completed"}
        return {"candidate_id": payload["candidates"][1]["candidate_id"]}


class RecordingSelector:
    def __init__(self):
        self.execution = None
        self.audiences = []

    def select(self, payload):
        self.audiences.append(payload["audience"])
        self.execution = {"status": "fake_completed"}
        return {"candidate_id":
                payload["candidates"][1]["candidate_id"]}


class RecordingGenerator(FakeGenerator):
    def __init__(self):
        super().__init__()
        self.audiences = []

    def generate(self, payload):
        self.audiences.append(payload["audience"])
        return super().generate(payload)


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
        kwargs.setdefault("review_mode", "legacy_single")
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

    def test_midpoint_snapshot_limited_counts_no_diagnostics(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        snapshot = self.submit_n(sid, token, 20)
        self.assertEqual(snapshot["answered_count"], 20)
        midpoint = snapshot["feedback"]["midpoint"]
        self.assertIsNotNone(midpoint)
        self.assertEqual(set(midpoint),
                         {"sections", "text", "total", "skills"})
        self.assertEqual(midpoint["total"],
                         {"correct": 20, "out_of": 20})
        self.assertEqual(len(midpoint["skills"]), 4)
        self.assertEqual(
            sum(row["correct"] for row in midpoint["skills"]), 20)
        for row in midpoint["skills"]:
            self.assertEqual(set(row),
                             {"skill_name", "correct", "out_of"})
            self.assertEqual(row["out_of"], 5)
        blob = json.dumps(snapshot)
        for marker in ("p_correct", "conformal", "answer_index",
                       "selected_index", "bank_sha256", "candidates",
                       "subtopic", "kt"):
            self.assertNotIn(marker, blob)
        self.assertIsNone(snapshot["feedback"]["end"])
        self.assertEqual(snapshot["provider_job"]["status"],
                         "not_requested")

    def test_midpoint_absent_before_twenty_and_sections_at_twenty(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        snapshot = self.submit_n(sid, token, 19)
        self.assertIsNone(snapshot["feedback"]["midpoint"])
        snapshot = self.service.submit_response(
            sid, token, responses(self.bank, count=20)[19])
        midpoint = snapshot["feedback"]["midpoint"]
        self.assertIsNotNone(midpoint)
        self.assertEqual(set(midpoint),
                         {"sections", "text", "total", "skills"})
        self.assertEqual(
            [(s["kind"], s["title"]) for s in midpoint["sections"]],
            [("encouragement", "Halfway there"),
             ("strengths", "What is going well"),
             ("focus", "Your answers so far"),
             ("next_step", "Before you continue")])
        for section in midpoint["sections"]:
            self.assertEqual(set(section), {"kind", "title", "text"})
        meta = self.service._load_meta(sid)
        block = meta["checkpoints"]["midpoint"]
        self.assertEqual(block["student_message_version"],
                         "phase3_student_midpoint_v5")
        self.assertEqual(block["student_message"], midpoint)
        self.assertIsNotNone(block["baseline_student"])
        self.assertEqual(snapshot["provider_job"]["status"],
                         "not_requested")

    def test_saved_midpoint_survives_second_half_and_restart(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        midpoint = self.submit_n(sid, token, 20)["feedback"]["midpoint"]
        for row in responses(self.bank, correct=False, count=40)[20:]:
            self.service.submit_response(sid, token, row)
        snapshot = self.service.snapshot(sid, token)
        self.assertEqual(snapshot["feedback"]["midpoint"], midpoint)
        self.service.close()
        restarted = self._service()
        snapshot = restarted.snapshot(sid, token)
        self.assertEqual(snapshot["feedback"]["midpoint"], midpoint)
        restarted.close()
        self.service = restarted

    def test_old_saved_midpoint_falls_back_to_baseline(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        self.submit_n(sid, token, 20)
        path = Path(self.tmp.name) / "metadata" / f"{sid}.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        block = meta["checkpoints"]["midpoint"]
        block.pop("student_message")
        block.pop("student_message_version")
        path.write_text(json.dumps(meta), encoding="utf-8")
        self.service.close()
        restarted = self._service()
        snapshot = restarted.snapshot(sid, token)
        midpoint = snapshot["feedback"]["midpoint"]
        self.assertIsNotNone(midpoint)
        self.assertEqual(set(midpoint), {"sections", "text"})
        for section in midpoint["sections"]:
            self.assertEqual(set(section), {"kind", "text"})
        self.assertEqual(midpoint["sections"],
                         block["baseline_student"]["message"]["sections"])
        restarted.close()
        self.service = restarted

    def test_teacher_session_shows_exact_saved_midpoint_message(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        snapshot = self.submit_n(sid, token, 20)
        public_mid = snapshot["feedback"]["midpoint"]
        view = self.service.teacher_session(sid)
        block = view["checkpoints"]["midpoint"]
        self.assertEqual(block["student_message"], public_mid)
        self.assertEqual(block["student_message_version"],
                         "phase3_student_midpoint_v5")
        block["student_message"]["sections"][0]["text"] = "mutated"
        snapshot = self.service.snapshot(sid, token)
        self.assertEqual(snapshot["feedback"]["midpoint"], public_mid)
        for row in responses(self.bank, correct=False, count=40)[20:]:
            self.service.submit_response(sid, token, row)
        view = self.service.teacher_session(sid)
        self.assertEqual(
            view["checkpoints"]["midpoint"]["student_message"],
            public_mid)
        snapshot = self.service.snapshot(sid, token)
        self.assertEqual(snapshot["feedback"]["midpoint"], public_mid)
        self.assertIsNone(snapshot["feedback"]["end"])
        self.assertEqual(snapshot["feedback_delivery"]["status"],
                         "awaiting_teacher_review")

    def test_v1_saved_midpoint_returns_only_sections_and_text(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        self.submit_n(sid, token, 20)
        path = Path(self.tmp.name) / "metadata" / f"{sid}.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        block = meta["checkpoints"]["midpoint"]
        message = block["student_message"]
        message.pop("total")
        message.pop("skills")
        block["student_message_version"] = "phase3_student_midpoint_v1"
        path.write_text(json.dumps(meta), encoding="utf-8")
        snapshot = self.service.snapshot(sid, token)
        midpoint = snapshot["feedback"]["midpoint"]
        self.assertEqual(set(midpoint), {"sections", "text"})
        self.assertNotIn("total", midpoint)
        self.assertNotIn("skills", midpoint)

    def test_midpoint_projection_is_first_half_and_deep_copied(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        rows = responses(self.bank, count=20)
        for index in (2, 3, 6, 7, 10, 11, 14, 15, 18, 19):
            question = self.bank["questions"][index]
            rows[index] = {"question_id": question["question_id"],
                           "selected_index":
                               (question["answer_index"] + 1) % 3}
        snapshot = None
        for row in rows:
            snapshot = self.service.submit_response(sid, token, row)
        midpoint = snapshot["feedback"]["midpoint"]
        self.assertEqual(midpoint["total"],
                         {"correct": 10, "out_of": 20})
        self.assertEqual([r["correct"] for r in midpoint["skills"]],
                         [5, 5, 0, 0])
        midpoint["total"]["correct"] = 99
        midpoint["skills"][0]["correct"] = 99
        for row in responses(self.bank, correct=False, count=40)[20:]:
            snapshot = self.service.submit_response(sid, token, row)
        midpoint = snapshot["feedback"]["midpoint"]
        self.assertEqual(midpoint["total"],
                         {"correct": 10, "out_of": 20})
        self.assertEqual([r["correct"] for r in midpoint["skills"]],
                         [5, 5, 0, 0])

    def test_end_stays_hidden_after_midpoint_message_until_release(self):
        created = self.create()
        sid, token = created["session_id"], created["student_token"]
        self.complete(sid, token)
        self.wait_job(sid)
        snapshot = self.service.snapshot(sid, token)
        self.assertIsNotNone(snapshot["feedback"]["midpoint"])
        self.assertIsNone(snapshot["feedback"]["end"])
        self.assertEqual(snapshot["feedback_delivery"]["status"],
                         "awaiting_teacher_review")
        view = self.service.teacher_session(sid)
        self.service.release_feedback(sid, {
            "message_sha256": view["feedback_delivery"]
                                  ["preview_sha256"]})
        snapshot = self.service.snapshot(sid, token)
        self.assertIsNotNone(snapshot["feedback"]["end"])

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
        self.assertIsNone(end["teacher_review"])
        self.assertIsNone(end["baseline_teacher"])
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

    def test_teacher_config_unlimited_limit_serializes_null(self):
        service = self._service(
            provider_mode="hosted", call_budget=None,
            selector=BlockingSelector(threading.Event()),
            generator=FakeGenerator())
        self.addCleanup(service.close)
        config = service.teacher_config()
        self.assertIsNone(config["call_budget"]["limit"])
        self.assertEqual(config["call_budget"]["used"], 0)
        self.assertIn('"limit": null', json.dumps(config))

    def test_shared_student_draft_is_the_only_generated_end(self):
        selector = RecordingSelector()
        generator = RecordingGenerator()
        service = self._service(
            provider_mode="hosted",
            selector=selector, generator=generator)
        self.addCleanup(service.close)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            == "ready"))
        self.assertEqual(selector.audiences, ["student"])
        self.assertEqual(generator.audiences, ["student"])
        meta = service._load_meta(sid)
        end = meta["checkpoints"]["end"]
        self.assertIsNotNone(end["student_review"])
        self.assertIsNone(end["teacher_review"])
        self.assertIsNone(end["baseline_teacher"])
        self.assertEqual(set(meta["provider_job"]["executions"]),
                         {"student"})
        view = service.teacher_session(sid)
        self.assertEqual(set(view["review_hashes"]), {"student"})
        self.assertEqual(view["feedback_delivery"]["status"],
                         "awaiting_teacher_review")
        self.assertIsNone(
            service.snapshot(sid, token)["feedback"]["end"])

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
        self.assertEqual(set(job["executions"]), {"student"})
        self.assertEqual(
            job["executions"]["student"]["selector"]["status"],
            "local_single_candidate")

    def test_teacher_branch_never_runs_and_job_stays_ready(self):
        selector = TeacherFailSelector()
        service = self._service(
            provider_mode="hosted",
            selector=selector,
            generator=FakeGenerator())
        self.addCleanup(service.close)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            == "ready"))
        self.assertEqual(selector.audiences, ["student"])
        meta = service._load_meta(sid)
        end = meta["checkpoints"]["end"]
        student_review = end["student_review"]
        self.assertIsNone(
            student_review["trace"]["fallback_reason"])
        self.assertNotEqual(
            student_review["selected_candidate_id"],
            student_review["baseline_candidate_id"])
        self.assertIsNone(end["teacher_review"])
        job = meta["provider_job"]
        self.assertEqual(set(job["executions"]), {"student"})
        snapshot = service.snapshot(sid, token)
        self.assertEqual(snapshot["provider_job"]["status"],
                         "ready")
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
        current = view["review_hashes"]["student"]
        with self.assertRaises(ConflictError) as ctx:
            self.service.add_review(sid, {
                "audience": "student",
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
            "phase3_student_feedback_template_v3")
        self.assertTrue(preview["requires_educator_review"])
        result = self.service.release_feedback(sid2, {
            "message_sha256": view["feedback_delivery"]["preview_sha256"],
            "reviewer_label": "teacher-a"})
        self.assertTrue(result["released"])
        snapshot = self.service.snapshot(sid2, token2)
        end = snapshot["feedback"]["end"]
        self.assertEqual(end["template_version"],
                         "phase3_student_feedback_template_v3")
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
            for marker in ("skill_id", "preview", "reviewer"):
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


class BoundRecommender:
    def __init__(self, bank, pool, qid="pool_q1", p_correct=0.6,
                 gate=None):
        self.bank = bank
        self._pool = pool
        self.qid = qid
        self.p_correct = p_correct
        self.gate = gate
        self.calls = 0

    def recommend(self, bank, taxonomy, rows):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(timeout=20)
        from tests.test_feedback_practice import selected_result
        return selected_result(self.bank, rows, self._pool, self.qid,
                               p_correct=self.p_correct)


class StaticRecommender:
    def __init__(self, result=None, error=None, gate=None):
        self.result = result
        self.error = error
        self.gate = gate
        self.calls = 0

    def recommend(self, bank, taxonomy, rows):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(timeout=20)
        if self.error is not None:
            raise self.error
        return dict(self.result)


class IntegratedPracticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        import feedback_practice
        from tests.test_feedback_practice import make_graph, make_pool
        self.pool = make_pool(("sA", "sB"))
        from unittest import mock
        for patch in (
                mock.patch.object(
                    feedback_practice, "load_current_practice_pool",
                    return_value=copy.deepcopy(self.pool)),
                mock.patch.object(
                    feedback_practice, "teacher_content_context",
                    return_value=make_graph(self.pool))):
            patch.start()
            self.addCleanup(patch.stop)

    def _service(self, **kwargs):
        kwargs.setdefault("diagnostics", fake_diagnostics(self.bank))
        kwargs.setdefault("review_mode", "legacy_single")
        service = DemoService(root=Path(self.tmp.name),
                              bank=self.bank, taxonomy=self.taxonomy,
                              **kwargs)
        self.addCleanup(service.close, wait=True)
        return service

    def _rows_wrong(self, skills=("sA",)):
        rows = []
        for q in self.bank["questions"]:
            index = q["answer_index"]
            if q["skill_id"] in skills:
                index = (index + 1) % len(q["options"])
            rows.append({"question_id": q["question_id"],
                         "selected_index": index})
        return rows

    def _complete(self, service, rows=None):
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in (rows if rows is not None else self._rows_wrong()):
            service.submit_response(sid, token, row)
        return sid, token

    def _end(self, service, sid):
        return service.teacher_session(sid)["checkpoints"]["end"]

    def _wait_terminal(self, service, sid):
        self.assertTrue(wait_for(
            lambda: self._end(service, sid)
            ["recommendation_job"]["status"] != "pending"))

    def _delivery(self, service, sid):
        return service.teacher_session(sid)["feedback_delivery"]

    def test_selected_practice_released_with_feedback(self):
        service = self._service(recommender=BoundRecommender(
            self.bank, self.pool))
        sid, token = self._complete(service)
        self._wait_terminal(service, sid)
        end = self._end(service, sid)
        self.assertEqual(end["practice_context"]["status"], "selected")
        self.assertEqual(end["practice_context"]
                         ["selected_question_id"], "pool_q1")
        public = service.snapshot(sid, token)
        blob = json.dumps(public)
        for needle in ("pool_q", "Pool practice prompt", "p_correct",
                       "answer_index", "practice_context"):
            self.assertNotIn(needle, blob)
        self.assertIsNone(public["feedback"]["end"])
        self.assertEqual(public["assessment_total"],
                         {"correct": 30, "out_of": 40})
        delivery = self._delivery(service, sid)
        preview = delivery["preview"]
        self.assertEqual(preview["template_version"],
                         "phase3_student_feedback_template_v3")
        card = preview["practice"]
        self.assertEqual(card["status"], "selected")
        self.assertEqual(set(card["question"]),
                         {"skill_name", "concept_name", "text",
                          "options"})
        service.release_feedback(sid, {
            "message_sha256": delivery["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["practice"], card)
        blob = json.dumps(released)
        for needle in ("pool_q", "p_correct", "answer_index",
                       "pool_sha256", "item_id"):
            self.assertNotIn(needle, blob)
        meta = service._load_meta(sid)
        self.assertEqual(
            meta["feedback_release"]["preview_sha256"],
            __import__("evidence_feedback").canonical_digest(preview))

    def test_example_relationship_edit_and_release_preserved(self):
        from unittest import mock
        import feedback_practice
        selector = RecordingSelector()
        generator = RecordingGenerator()
        service = self._service(
            provider_mode="hosted", selector=selector,
            generator=generator,
            recommender=BoundRecommender(self.bank, self.pool))
        sid, token = self._complete(service)
        self._wait_terminal(service, sid)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            == "ready"))
        self.assertEqual(set(selector.audiences), {"student"}
                         if selector.audiences else set())
        self.assertLessEqual(len(selector.audiences), 1)
        self.assertEqual(generator.audiences, ["student"])
        end = self._end(service, sid)
        review = end["student_review"]
        selected = next(
            c for c in review["candidates"]
            if c["candidate_id"] == review["selected_candidate_id"])
        sub_id = selected["focus"]["subtopic_id"]
        self.assertEqual(selected["focus"]["skill_id"], "sA")
        first = {"prompt": "Fixed first prompt?",
                 "explanation": "Fixed first explanation."}
        other = {"prompt": "Fixed other prompt?",
                 "explanation": "Fixed other explanation."}
        with mock.patch.dict(feedback_practice.REVIEW_EXAMPLES,
                             {sub_id: first}):
            delivery = self._delivery(service, sid)
            first_sha = delivery["preview_sha256"]
        with mock.patch.dict(feedback_practice.REVIEW_EXAMPLES,
                             {sub_id: other}):
            other_sha = self._delivery(service, sid)["preview_sha256"]
        self.assertNotEqual(first_sha, other_sha)
        with mock.patch.dict(feedback_practice.REVIEW_EXAMPLES,
                             {sub_id: first}):
            delivery = self._delivery(service, sid)
            preview = delivery["preview"]
            self.assertEqual(preview["template_version"],
                             "phase3_student_feedback_template_v3")
            example = preview["review_example"]
            self.assertEqual(example["title"], "Worked review example")
            self.assertEqual(example["prompt"], first["prompt"])
            self.assertEqual(example["explanation"],
                             first["explanation"])
            self.assertIn("Discuss this example", example["notice"])
            self.assertEqual(
                set(example),
                {"title", "prompt", "explanation", "notice"})
            card = preview["practice"]
            self.assertEqual(card["status"], "selected")
            self.assertEqual(card["relationship"]["kind"],
                             "different_content")
            self.assertIn("Concept sA",
                          card["relationship"]["message"])
            self.assertIn("Subtopic sA",
                          card["relationship"]["message"])
            res = service.save_feedback_edit(sid, {
                "message_sha256": delivery["preview_sha256"],
                "sections": {
                    key: f"Edited {key}."
                    for key in ("assessment_summary",
                                "observed_strengths", "review_focus",
                                "next_steps")},
                "reviewer_label": "fx"})
            released = service.release_feedback(
                sid, {"message_sha256": res["preview_sha256"]})
        self.assertTrue(released["released"])
        snap = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(snap["review_example"], example)
        self.assertEqual(snap["practice"], card)
        blob = json.dumps(snap)
        for needle in ("pool_q", "p_correct", "answer_index",
                       "item_id", "pool_sha256", "sub_"):
            self.assertNotIn(needle, blob)
        stored = service.teacher_session(sid)["feedback_delivery"]
        self.assertEqual(stored["preview"]["review_example"], example)

    def test_edit_content_patches_singleton_example_and_practice(self):
        from unittest import mock
        import feedback_practice
        service = self._service(recommender=BoundRecommender(
            self.bank, self.pool))
        sid, token = self._complete(service)
        self._wait_terminal(service, sid)
        end = self._end(service, sid)
        review = end["student_review"]
        selected = next(
            c for c in review["candidates"]
            if c["candidate_id"] == review["selected_candidate_id"])
        sub_id = selected["focus"]["subtopic_id"]
        record = {"prompt": "Original prompt?",
                  "explanation": "Original explanation."}
        with mock.patch.dict(feedback_practice.REVIEW_EXAMPLES,
                             {sub_id: record}):
            delivery = self._delivery(service, sid)
            preview = delivery["preview"]
            original_example = copy.deepcopy(preview["review_example"])
            original_practice = copy.deepcopy(preview["practice"])
            options = preview["practice"]["question"]["options"]
            content = {
                "review_examples": [{
                    "title": "Teacher card title",
                    "prompt": "Teacher <b>markup</b> prompt?"}],
                "practice_activities": [{
                    "message": "Teacher practice message.",
                    "notice": "Teacher practice notice.",
                    "relationship": {"message": "Teacher link note."},
                    "question": {
                        "text": "Teacher question text?",
                        "options": [f"Option {i}"
                                    for i in range(len(options))]}}]}
            res = service.save_feedback_edit(sid, {
                "message_sha256": delivery["preview_sha256"],
                "sections": {key: f"Edited {key}."
                             for key in ("assessment_summary",
                                         "observed_strengths",
                                         "review_focus",
                                         "next_steps")},
                "content": content})
        preview = self._delivery(service, sid)["preview"]
        example = preview["review_example"]
        self.assertEqual(example["title"], "Teacher card title")
        self.assertEqual(example["prompt"],
                         "Teacher <b>markup</b> prompt?")
        self.assertEqual(example["explanation"],
                         "Original explanation.")
        self.assertTrue(example["teacher_edited"])
        self.assertEqual(example["original_content"],
                         original_example)
        self.assertNotIn("teacher_edited",
                         example["original_content"])
        card = preview["practice"]
        self.assertEqual(card["message"], "Teacher practice message.")
        self.assertEqual(card["notice"], "Teacher practice notice.")
        with self.assertRaises(ValueError):
            service.save_feedback_edit(sid, {
                "message_sha256": res["preview_sha256"],
                "sections": {key: f"Edited {key}."
                             for key in ("assessment_summary",
                                         "observed_strengths",
                                         "review_focus",
                                         "next_steps")},
                "content": {"practice_activities":
                            [{"instruction": "Not present."}]}})
        self.assertEqual(card["relationship"]["message"],
                         "Teacher link note.")
        self.assertEqual(card["relationship"]["kind"],
                         original_practice["relationship"]["kind"])
        self.assertEqual(card["question"]["text"],
                         "Teacher question text?")
        self.assertEqual(card["question"]["options"],
                         [f"Option {i}" for i in range(len(options))])
        self.assertEqual(card["question"]["skill_name"],
                         original_practice["question"]["skill_name"])
        self.assertTrue(card["teacher_edited"])
        self.assertEqual(card["original_content"], original_practice)
        with self.assertRaises(ValueError):
            service.save_feedback_edit(sid, {
                "message_sha256": res["preview_sha256"],
                "sections": {key: f"Edited {key}."
                             for key in ("assessment_summary",
                                         "observed_strengths",
                                         "review_focus",
                                         "next_steps")},
                "content": {"review_examples": [{"steps": ["x"]}]}})
        service.release_feedback(
            sid, {"message_sha256": res["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["review_example"]["title"],
                         "Teacher card title")
        self.assertEqual(released["practice"]["question"]["text"],
                         "Teacher question text?")

    def test_released_same_content_has_no_awaiting_claim(self):
        from unittest import mock
        import feedback_practice
        from tests.test_feedback_practice import make_graph
        graph = make_graph(self.pool)
        for row in graph["exercises"]:
            if row["question_id"] == "pool_q1":
                row["concept_id"] = "sub_sA"
                row["concept_label"] = "Subtopic sA"
        with mock.patch.object(feedback_practice,
                               "teacher_content_context",
                               return_value=copy.deepcopy(graph)):
            service = self._service(recommender=BoundRecommender(
                self.bank, self.pool))
            sid, token = self._complete(service)
            self._wait_terminal(service, sid)
            delivery = self._delivery(service, sid)
            card = delivery["preview"]["practice"]
            self.assertEqual(card["relationship"]["kind"], "same_content")
            self.assertEqual(
                card["relationship"]["message"],
                "This practice activity covers the same assessed content "
                "as the review focus. Review and practice are separate "
                "activities.")
            res = service.release_feedback(sid, {
                "message_sha256": delivery["preview_sha256"]})
            self.assertTrue(res["released"])
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["practice"], card)
        self.assertNotIn("awaiting", json.dumps(released))

    def test_nonselected_states_fabricate_no_question_or_example(self):
        for status in ("abstained", "unavailable"):
            service = self._service(recommender=StaticRecommender(
                result={"status": status}))
            sid, _ = self._complete(service)
            self._wait_terminal(service, sid)
            preview = self._delivery(service, sid)["preview"]
            self.assertIsNone(preview["practice"]["question"])
            self.assertNotIn("relationship", preview["practice"])
            self.assertNotIn("review_example", preview)
        service = self._service()
        sid, _ = self._complete(service)
        self._wait_terminal(service, sid)
        preview = self._delivery(service, sid)["preview"]
        self.assertIsNone(preview["practice"]["question"])
        self.assertNotIn("relationship", preview["practice"])
        self.assertNotIn("review_example", preview)

    def test_abstained_unavailable_disabled_contexts(self):
        service = self._service(recommender=StaticRecommender(
            result={"status": "abstained"}))
        sid, _ = self._complete(service)
        self._wait_terminal(service, sid)
        ctx = self._end(service, sid)["practice_context"]
        self.assertEqual(ctx["status"], "abstained")
        self.assertIsNone(ctx["selected_question_id"])
        self.assertIsNone(self._delivery(service, sid)
                          ["preview"]["practice"]["question"])

        service = self._service(recommender=StaticRecommender(
            error=RuntimeError("missing model")))
        sid, _ = self._complete(service)
        self._wait_terminal(service, sid)
        self.assertEqual(self._end(service, sid)
                         ["practice_context"]["status"], "unavailable")
        self.assertIsNotNone(self._delivery(service, sid)["preview"])

        service = self._service()
        sid, _ = self._complete(service)
        self._wait_terminal(service, sid)
        self.assertEqual(self._end(service, sid)
                         ["practice_context"]["status"], "disabled")

    def test_shallow_legacy_result_becomes_unavailable(self):
        service = self._service(recommender=StaticRecommender(
            result={"status": "selected",
                    "selected_question_id": "pool_q1"}))
        sid, _ = self._complete(service)
        self._wait_terminal(service, sid)
        end = self._end(service, sid)
        self.assertEqual(end["recommendation"]["status"], "selected")
        self.assertEqual(end["practice_context"]["status"], "unavailable")
        card = self._delivery(service, sid)["preview"]["practice"]
        self.assertIsNone(card["question"])
        self.assertEqual(end["practice_context_reason"],
                         "practice_context_validation_failed")

    def test_pending_recommendation_gates_preview_and_jobs(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        calls = {"generate": 0, "contexts": [], "candidates": []}
        sections = {"assessment_summary": "Summary.",
                    "observed_strengths": "Strengths.",
                    "review_focus": "Focus.",
                    "next_steps": "Steps."}

        class FocusSelector:
            def __init__(self):
                self.execution = None
                self.calls = 0

            def select(self, payload):
                self.calls += 1
                focus = next(c for c in payload["candidates"]
                             if c["focus"]
                             and c["focus"]["skill_id"] == "sB")
                self.execution = {"status": "fake_completed"}
                return {"candidate_id": focus["candidate_id"]}

        class CountingFull:
            supports_full_feedback = True

            def generate(self, payload):
                calls["generate"] += 1
                calls["contexts"].append(payload["practice_context"])
                calls["candidates"].append(
                    payload["selected_candidate"]["candidate_id"])
                return {"candidate_id":
                        payload["selected_candidate"]["candidate_id"],
                        "sections": dict(sections)}

        selector = FocusSelector()
        recommender = BoundRecommender(self.bank, self.pool, gate=gate)
        service = self._service(recommender=recommender,
                                provider_mode="hosted",
                                selector=selector,
                                generator=CountingFull())
        sid, token = self._complete(
            service, rows=self._rows_wrong(("sA", "sB")))
        self.assertTrue(wait_for(lambda: recommender.calls >= 1))
        meta = service._load_meta(sid)
        self.assertEqual(meta["provider_job"]["status"],
                         "waiting_for_practice")
        public = service.snapshot(sid, token)
        self.assertEqual(public["assessment_total"],
                         {"correct": 20, "out_of": 40})
        self.assertIsNone(public["feedback"]["end"])
        delivery = self._delivery(service, sid)
        self.assertIsNone(delivery["preview"])
        self.assertEqual(delivery["practice_status"], "pending")
        self.assertEqual(calls["generate"], 0)
        self.assertEqual(selector.calls, 0)
        with self.assertRaises(ConflictError):
            service.release_feedback(sid, {"message_sha256": "0" * 64})
        with self.assertRaises(ConflictError):
            service.save_feedback_edit(sid, {
                "message_sha256": "0" * 64,
                "sections": sections})
        service.snapshot(sid, token)
        self._delivery(service, sid)
        self.assertEqual(recommender.calls, 1)
        gate.set()
        self._wait_terminal(service, sid)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        self.assertEqual(recommender.calls, 1)
        self.assertEqual(selector.calls, 1)
        self.assertEqual(calls["generate"], 1)
        for ctx in calls["contexts"]:
            self.assertEqual(ctx["status"], "selected")
            self.assertEqual(ctx["selected_question_id"], "pool_q1")
        for candidate_id in calls["candidates"]:
            self.assertEqual(candidate_id, "review_sub_sB")
        view = self._delivery(service, sid)
        self.assertEqual(view["preview"]["practice"]["question"]
                         ["skill_name"], "Skill sA")
        self.assertEqual(self._end(service, sid)["student_review"]
                         ["selected_candidate_id"], "review_sub_sB")
        service.snapshot(sid, token)
        self._delivery(service, sid)
        self.assertEqual(recommender.calls, 1)
        self.assertEqual(calls["generate"], 1)
        from full_feedback import FULL_FEEDBACK_PROMPT_VERSION
        self.assertEqual(
            service._load_meta(sid)["provider_job"]
            ["prompt_versions"]["generation"],
            FULL_FEEDBACK_PROMPT_VERSION)

    def test_close_during_blocked_recommendation_saves_result(self):
        gate = threading.Event()
        recommender = BoundRecommender(self.bank, self.pool, gate=gate)
        service = self._service(recommender=recommender)
        sid, _ = self._complete(service)
        self.assertTrue(wait_for(lambda: recommender.calls >= 1))
        service.close(wait=False)
        gate.set()
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["end"]
            ["recommendation_job"]["status"] != "pending"))
        meta = service._load_meta(sid)
        end = meta["checkpoints"]["end"]
        self.assertEqual(end["recommendation"]["status"], "selected")
        self.assertEqual(end["practice_context"]["status"], "selected")
        self.assertEqual(end["practice_context"]
                         ["selected_question_id"], "pool_q1")
        self.assertEqual(meta["provider_job"]["status"], "fallback")
        self.assertIsNotNone(end["student_review"])
        restarted = self._service()
        self.assertIsNotNone(
            restarted.teacher_session(sid)
            ["feedback_delivery"]["preview"])

    def test_restart_pending_recommendation_unavailable(self):
        recommender = BoundRecommender(self.bank, self.pool)
        service = self._service(recommender=recommender)
        sid, _ = self._complete(service)
        self._wait_terminal(service, sid)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        meta = service._load_meta(sid)
        end = meta["checkpoints"]["end"]
        end["recommendation_job"] = {"status": "pending"}
        end["recommendation"] = None
        end["practice_context"] = None
        meta["provider_job"] = {"status": "waiting_for_practice",
                                "provider_mode": "rules"}
        service._save_meta(meta)
        service.close(wait=True)
        restarted = self._service(recommender=BoundRecommender(
            self.bank, self.pool))
        end = restarted._load_meta(sid)["checkpoints"]["end"]
        self.assertEqual(end["recommendation"]["status"], "unavailable")
        self.assertEqual(end["practice_context"]["status"], "unavailable")
        self.assertEqual(
            restarted._load_meta(sid)["provider_job"]["status"],
            "fallback")
        self.assertIsNotNone(
            restarted.teacher_session(sid)
            ["feedback_delivery"]["preview"])

    def test_edit_preserves_practice(self):
        service = self._service(recommender=BoundRecommender(
            self.bank, self.pool))
        sid, token = self._complete(service)
        self._wait_terminal(service, sid)
        delivery = self._delivery(service, sid)
        card = delivery["preview"]["practice"]
        res = service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "sections": {"assessment_summary": "Edited summary.",
                         "observed_strengths": "Edited strengths.",
                         "review_focus": "Edited focus.",
                         "next_steps": "Edited steps."},
            "reviewer_label": "ms-test"})
        delivery = self._delivery(service, sid)
        self.assertEqual(delivery["preview"]["practice"], card)
        self.assertEqual(delivery["preview_sha256"],
                         res["preview_sha256"])
        service.release_feedback(sid, {
            "message_sha256": delivery["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["practice"], card)
        self.assertIn("Edited summary.", released["text"])

    def test_provider_worker_failure_falls_back_without_practice(self):
        from unittest import mock
        from feedback_practice import PRACTICE_NEXT_STEP_UNAVAILABLE
        service = self._service(recommender=BoundRecommender(
            self.bank, self.pool))
        original_baseline = service._baseline_review

        def failing_baseline(evidence, audience, reason, ctx=None):
            if ctx is not None and ctx.get("status") == "selected":
                raise ValueError("injected fallback failure")
            return original_baseline(
                evidence, audience, reason, ctx)

        with mock.patch.object(
                service, "_audience_result",
                side_effect=RuntimeError("injected provider failure")), \
                mock.patch.object(
                    service, "_baseline_review",
                    side_effect=failing_baseline):
            sid, token = self._complete(service)
            self._wait_terminal(service, sid)
            self.assertTrue(wait_for(
                lambda: service._load_meta(sid)
                ["provider_job"]["status"] in ("ready", "fallback")))
        meta = service._load_meta(sid)
        end = meta["checkpoints"]["end"]
        self.assertEqual(meta["provider_job"]["status"], "fallback")
        self.assertEqual(service._pending_provider, 0)
        self.assertEqual(end["practice_context"]["status"],
                         "unavailable")
        self.assertEqual(end["practice_context_reason"],
                         "provider_fallback_context_unavailable")
        delivery = self._delivery(service, sid)
        preview = delivery["preview"]
        self.assertIsNone(preview["practice"]["question"])
        next_steps = next(s["text"] for s in preview["sections"]
                          if s["kind"] == "next_steps")
        self.assertIn(PRACTICE_NEXT_STEP_UNAVAILABLE, next_steps)
        for needle in ("Pool practice prompt", "pool_q1"):
            self.assertNotIn(needle, json.dumps(preview))
        service.release_feedback(sid, {
            "message_sha256": delivery["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertIsNone(released["practice"]["question"])
        self.assertEqual(released["total"],
                         {"correct": 30, "out_of": 40})

    def test_old_session_without_bundle_stays_v1(self):
        service = self._service()
        sid, token = self._complete(service)
        self.assertTrue(wait_for(
            lambda: self._end(service, sid)
            ["diagnostics_job"]["status"] != "pending"))
        meta = service._load_meta(sid)
        meta.pop("feedback_bundle_version")
        service._save_meta(meta)
        delivery = self._delivery(service, sid)
        self.assertEqual(delivery["preview"]["template_version"],
                         "phase3_student_feedback_template_v1")
        self.assertNotIn("practice", delivery["preview"])
        service.release_feedback(sid, {
            "message_sha256": delivery["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertNotIn("practice", released)


MULTI_SECTIONS = {
    "assessment_summary": "You have completed this assessment.",
    "observed_strengths":
        "Your observed answers provide a starting point for review.",
    "review_focus":
        "A possible review starting point is listed below.",
    "next_steps": "Work through each example with your teacher."}


class MultiSelector:
    def __init__(self, events=None, index=0, error=None, prefer=None):
        self.events = events
        self.index = index
        self.error = error
        self.prefer = prefer
        self.execution = None
        self.calls = 0

    def select(self, payload):
        self.calls += 1
        if self.events is not None:
            self.events.append("select")
        if self.error is not None:
            raise self.error
        self.execution = {"status": "fake_completed"}
        candidates = payload["candidates"]
        if self.prefer is not None:
            for bundle in candidates:
                skills = {c["focus"]["skill_id"]
                          for c in bundle["review_candidates"]
                          if c.get("focus")}
                if skills == set(self.prefer):
                    return {"candidate_id": bundle["candidate_id"]}
        return {"candidate_id": candidates[self.index]["candidate_id"]}


class MultiGenerator:
    supports_full_feedback = True

    def __init__(self, events=None):
        self.events = events
        self.execution = None
        self.calls = 0
        self.audiences = []

    def generate(self, payload):
        self.calls += 1
        if self.events is not None:
            self.events.append("generate")
        self.audiences.append(payload["audience"])
        self.execution = {"status": "fake_completed"}
        return {"candidate_id":
                payload["selected_candidate"]["candidate_id"],
                "sections": dict(MULTI_SECTIONS)}


class RecordingPredictor:
    def __init__(self, probabilities=None, events=None):
        self.probabilities = probabilities or {}
        self.events = events
        self.calls = []

    def __call__(self, bank, rows, candidates):
        self.calls.append(copy.deepcopy(candidates))
        if self.events is not None:
            self.events.append("predict")
        return {"predictions": [
            {"question_id": q["question_id"], "skill_id": q["skill_id"],
             "item_id": q["item_id"], "regime": "warm",
             "p_correct": self.probabilities.get(q["question_id"], 0.6)}
            for q in candidates]}


class FakeAlignedRecommender:
    def __init__(self, pool, predictor, band=(0.4, 0.8)):
        from shadow_practice import ShadowPracticeRecommender
        self._inner = ShadowPracticeRecommender(pool, band, predictor)
        self.band = tuple(band)
        self.calls = []
        self.subtopic_calls = []

    def recommend_for_reviews(self, bank, taxonomy, rows,
                              review_skill_ids, research=False,
                              review_subtopic_ids=None):
        self.calls.append(list(review_skill_ids))
        self.subtopic_calls.append(copy.deepcopy(review_subtopic_ids))
        return self._inner.recommend_for_reviews(
            bank, taxonomy, rows, review_skill_ids, research=research,
            review_subtopic_ids=review_subtopic_ids)


class MultiModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        import feedback_practice
        import research_runtime
        from tests.test_feedback_practice import make_graph, make_pool
        self.pool = make_pool(("sA", "sB"))
        from unittest import mock
        for patch in (
                mock.patch.object(
                    feedback_practice, "load_current_practice_pool",
                    return_value=copy.deepcopy(self.pool)),
                mock.patch.object(
                    research_runtime, "load_current_practice_pool",
                    return_value=copy.deepcopy(self.pool)),
                mock.patch.object(
                    feedback_practice, "teacher_content_context",
                    return_value=make_graph(self.pool))):
            patch.start()
            self.addCleanup(patch.stop)

    def _service(self, **kwargs):
        kwargs.setdefault("diagnostics", fake_diagnostics(self.bank))
        kwargs.setdefault("provider_mode", "hosted")
        service = DemoService(root=Path(self.tmp.name),
                              bank=self.bank, taxonomy=self.taxonomy,
                              **kwargs)
        self.addCleanup(service.close, wait=True)
        return service

    def _rows_wrong(self, skills=("sA", "sB")):
        rows = []
        for q in self.bank["questions"]:
            index = q["answer_index"]
            if q["skill_id"] in skills:
                index = (index + 1) % len(q["options"])
            rows.append({"question_id": q["question_id"],
                         "selected_index": index})
        return rows

    def _complete(self, service, rows):
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in rows:
            service.submit_response(sid, token, row)
        return sid, token

    def _end(self, service, sid):
        return service.teacher_session(sid)["checkpoints"]["end"]

    def _wait_done(self, service, sid):
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)
            ["provider_job"]["status"] in ("ready", "fallback")))

    def _delivery(self, service, sid):
        return service.teacher_session(sid)["feedback_delivery"]

    def _aligned(self, probabilities=None, events=None):
        predictor = RecordingPredictor(probabilities, events)
        recommender = FakeAlignedRecommender(self.pool, predictor)
        return recommender, predictor

    def test_two_topics_two_questions_ordered_once(self):
        events = []
        selector = MultiSelector(events, prefer=("sA", "sB"))
        generator = MultiGenerator(events)
        recommender, predictor = self._aligned(
            {"pool_q1": 0.6, "pool_q2": 0.55}, events)
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        sid, token = self._complete(
            service, self._rows_wrong(("sA", "sB", "sC")))
        self._wait_done(service, sid)
        self.assertEqual(events, ["select", "predict", "generate"])
        self.assertEqual(selector.calls, 1)
        self.assertEqual(len(predictor.calls), 1)
        self.assertEqual(generator.calls, 1)
        self.assertEqual(generator.audiences, ["student"])
        self.assertEqual(
            {q["skill_id"] for q in predictor.calls[0]}, {"sA", "sB"})
        self.assertEqual(len(recommender.calls), 1)
        self.assertEqual(set(recommender.calls[0]), {"sA", "sB"})
        end = self._end(service, sid)
        review = end["student_review"]
        self.assertEqual(
            len(review["selected_candidates"]), 2)
        self.assertEqual(
            {c["focus"]["skill_id"]
             for c in review["selected_candidates"]}, {"sA", "sB"})
        self.assertEqual(
            sorted(end["recommendation"]["selected_question_ids"]),
            ["pool_q1", "pool_q2"])
        contexts = end["practice_contexts"]
        self.assertEqual(len(contexts), 2)
        self.assertTrue(
            all(c["context"]["status"] == "selected"
                for c in contexts))
        delivery = self._delivery(service, sid)
        preview = delivery["preview"]
        self.assertEqual(preview["template_version"],
                         "phase3_student_feedback_template_v9")
        activities = preview["practice_activities"]
        self.assertEqual(len(activities), 2)
        self.assertEqual(
            {a["question"]["skill_name"] for a in activities},
            {"Skill sA", "Skill sB"})
        for activity in activities:
            self.assertEqual(activity["status"], "selected")
            self.assertEqual(activity["relationship"]["kind"],
                             "same_topic_different_content")
            self.assertTrue(
                activity["topic_label"].startswith("Subtopic"))
        public = service.snapshot(sid, token)
        blob = json.dumps(public)
        for needle in ("pool_q", "Pool practice prompt", "p_correct",
                       "answer_index", "practice_context",
                       "topic_selections", "review_bundle_"):
            self.assertNotIn(needle, blob)
        self.assertIsNone(public["feedback"]["end"])
        service.release_feedback(sid, {
            "message_sha256": delivery["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["practice_activities"], activities)
        self.assertEqual(released["sections"], preview["sections"])
        self.assertEqual(released["text"], preview["text"])

    def test_concise_preview_raw_review_and_edit_survival(self):
        selector = MultiSelector(prefer=("sA", "sB"))
        generator = MultiGenerator()
        recommender, _ = self._aligned(
            {"pool_q1": 0.6, "pool_q2": 0.55})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        sid, token = self._complete(
            service, self._rows_wrong(("sA", "sB", "sC")))
        self._wait_done(service, sid)
        review = self._end(service, sid)["student_review"]
        raw_texts = {s["kind"]: s["text"]
                     for s in review["message"]["sections"]}
        self.assertIn("draft, not a diagnosis",
                      raw_texts["review_focus"])
        delivery = self._delivery(service, sid)
        preview = delivery["preview"]
        self.assertEqual(preview["template_version"],
                         "phase3_student_feedback_template_v9")
        self.assertEqual(
            [s["title"] for s in preview["sections"]],
            ["Assessment complete", "What went well",
             "Answers to review", "What to do next"])
        ptexts = {s["kind"]: s["text"] for s in preview["sections"]}
        self.assertTrue(
            ptexts["review_focus"].startswith("Review these topics:"))
        for needle in ("draft, not a diagnosis",
                       "If your teacher approves"):
            self.assertNotIn(needle, preview["text"])
        self.assertIsNone(
            service.snapshot(sid, token)["feedback"]["end"])
        result = service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "sections": {key: f"Teacher wording for {key}."
                         for key in ("assessment_summary",
                                     "observed_strengths",
                                     "review_focus", "next_steps")}})
        edited = self._delivery(service, sid)["preview"]
        self.assertEqual(
            {s["kind"]: s["text"] for s in edited["sections"]},
            {"assessment_summary": "Teacher wording for "
                                   "assessment_summary.",
             "observed_strengths": "Teacher wording for "
                                   "observed_strengths.",
             "review_focus": "Teacher wording for review_focus.",
             "next_steps": "Teacher wording for next_steps."})
        service.release_feedback(sid, {
            "message_sha256": result["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["sections"], edited["sections"])
        self.assertEqual(released["text"], edited["text"])

    def test_malicious_provider_sections_replaced_in_preview(self):
        class MaliciousGenerator(MultiGenerator):
            def generate(self, payload):
                super().generate(payload)
                sections = dict(MULTI_SECTIONS)
                sections["observed_strengths"] = (
                    "The student shows mastery of every topic.")
                sections["review_focus"] = (
                    "The weakest areas need remediation.")
                sections["next_steps"] = (
                    "Try similar practice tasks on the same concepts.")
                return {"candidate_id":
                        payload["selected_candidate"]["candidate_id"],
                        "sections": sections}

        selector = MultiSelector(prefer=("sA", "sB"))
        generator = MaliciousGenerator()
        recommender, _ = self._aligned(
            {"pool_q1": 0.6, "pool_q2": 0.55})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        sid, token = self._complete(
            service, self._rows_wrong(("sA", "sB", "sC")))
        self._wait_done(service, sid)
        review = self._end(service, sid)["student_review"]
        self.assertEqual(
            review["trace"]["section_sources"],
            {"assessment_summary": "provider",
             "observed_strengths": "application",
             "review_focus": "application",
             "next_steps": "application"})
        texts = {s["kind"]: s["text"]
                 for s in review["message"]["sections"]}
        self.assertNotIn("mastery", texts["observed_strengths"])
        self.assertNotIn("weakest areas need remediation",
                         texts["review_focus"])
        self.assertNotIn("similar practice tasks", texts["next_steps"])
        self.assertNotIn("same concepts", texts["next_steps"])
        self.assertIn("separate practice activities shown below",
                      texts["next_steps"])
        delivery = self._delivery(service, sid)
        preview = delivery["preview"]
        self.assertEqual(preview["template_version"],
                         "phase3_student_feedback_template_v9")
        ptexts = {s["kind"]: s["text"] for s in preview["sections"]}
        self.assertEqual(ptexts["assessment_summary"],
                         texts["assessment_summary"])
        self.assertEqual(ptexts["observed_strengths"],
                         texts["observed_strengths"])
        for kind in ("review_focus", "next_steps"):
            self.assertNotIn("draft, not a diagnosis", ptexts[kind])
            self.assertNotIn("If your teacher approves", ptexts[kind])
        self.assertEqual(ptexts["next_steps"],
                         "Review the topics listed above and explain "
                         "how you approached those questions. For any "
                         "question without a checked worked solution, "
                         "ask your teacher for help. Then try the "
                         "practice activities below.")
        activities = preview["practice_activities"]
        self.assertEqual(
            {a["relationship"]["kind"] for a in activities},
            {"same_topic_different_content"})
        self.assertTrue(all(a["relationship"]["message"]
                            for a in activities))
        self.assertEqual(
            preview["total"]["correct"],
            sum(1 for q in self.bank["questions"]
                if q["skill_id"] not in ("sA", "sB", "sC")))
        service.release_feedback(sid, {
            "message_sha256": delivery["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["sections"], preview["sections"])
        self.assertEqual(released["text"], preview["text"])

    def test_one_qualifying_one_abstained(self):
        selector = MultiSelector()
        generator = MultiGenerator()
        recommender, _ = self._aligned(
            {"pool_q1": 0.6, "pool_q2": 0.95})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        sid, _ = self._complete(service, self._rows_wrong())
        self._wait_done(service, sid)
        end = self._end(service, sid)
        self.assertEqual(end["recommendation"]["status"], "selected")
        slots = {s["skill_id"]: s for s in
                 end["recommendation"]["topic_selections"]}
        self.assertEqual(slots["sA"]["status"], "selected")
        self.assertEqual(slots["sA"]["selected_question_id"], "pool_q1")
        self.assertEqual(slots["sB"]["status"], "abstained")
        statuses = {c["skill_id"]: c["context"]["status"]
                    for c in end["practice_contexts"]}
        self.assertEqual(statuses, {"sA": "selected", "sB": "abstained"})
        activities = self._delivery(service, sid)["preview"][
            "practice_activities"]
        self.assertEqual(len(activities), 2)
        by_label = {a["topic_label"]: a for a in activities}
        self.assertEqual(
            {a["status"] for a in activities},
            {"selected", "abstained"})
        selected = next(a for a in activities
                        if a["status"] == "selected")
        self.assertEqual(selected["question"]["text"],
                         "Pool practice prompt 1?")
        abstained = next(a for a in activities
                         if a["status"] == "abstained")
        self.assertIsNone(abstained["question"])

    def test_all_correct_no_focus_no_predictor(self):
        selector = MultiSelector()
        generator = MultiGenerator()
        recommender, predictor = self._aligned({"pool_q1": 0.6})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        rows = responses(self.bank)
        sid, _ = self._complete(service, rows)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)
            ["provider_job"]["status"] == "ready"))
        end = self._end(service, sid)
        self.assertEqual(selector.calls, 0)
        self.assertEqual(generator.calls, 1)
        self.assertEqual(predictor.calls, [])
        self.assertEqual(recommender.calls, [])
        self.assertEqual(end["recommendation"]["status"], "abstained")
        self.assertEqual(end["practice_contexts"], [])
        review = end["student_review"]
        self.assertIsNone(review["trace"]["fallback_reason"])
        self.assertTrue(all(
            (c.get("focus") or {}).get("incorrect") in (None, 0)
            or c.get("focus") is None
            for c in review["selected_candidates"]))
        focus = next(
            s["text"] for s in
            review["template_baseline"]["sections"]
            if s["kind"] == "review_focus")
        self.assertIn("no error-based review priority", focus)
        delivery = self._delivery(service, sid)
        preview = delivery["preview"]
        self.assertEqual(preview["practice"]["status"], "abstained")
        self.assertIsNone(preview["practice"]["question"])
        self.assertEqual(preview["review_examples"], [])

    def test_selector_failure_explicit_fallback_keeps_alignment(self):
        selector = MultiSelector(error=RuntimeError("down"))
        generator = MultiGenerator()
        recommender, predictor = self._aligned({"pool_q1": 0.6})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        sid, _ = self._complete(
            service, self._rows_wrong(("sA", "sB", "sC")))
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)
            ["provider_job"]["status"] == "fallback"))
        end = self._end(service, sid)
        self.assertEqual(
            end["student_review"]["trace"]["fallback_reason"],
            "selector_error")
        self.assertEqual(generator.calls, 0)
        self.assertEqual(len(predictor.calls), 1)
        self.assertIsNotNone(
            self._delivery(service, sid)["preview"])

    def test_teacher_edit_release_and_stale_hash(self):
        selector = MultiSelector()
        generator = MultiGenerator()
        recommender, _ = self._aligned(
            {"pool_q1": 0.6, "pool_q2": 0.55})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        sid, token = self._complete(service, self._rows_wrong())
        self._wait_done(service, sid)
        delivery = self._delivery(service, sid)
        sha = delivery["preview_sha256"]
        with self.assertRaises(ConflictError):
            service.save_feedback_edit(sid, {
                "message_sha256": "f" * 64,
                "sections": dict(MULTI_SECTIONS)})
        with self.assertRaises(ConflictError):
            service.release_feedback(
                sid, {"message_sha256": "f" * 64})
        edited = dict(MULTI_SECTIONS)
        edited["next_steps"] = "Review each example with your teacher."
        service.save_feedback_edit(sid, {
            "message_sha256": sha, "sections": edited})
        delivery = self._delivery(service, sid)
        preview = delivery["preview"]
        self.assertEqual(delivery["preview_source"], "teacher_edited")
        texts = {s["kind"]: s["text"] for s in preview["sections"]}
        self.assertEqual(texts["next_steps"], edited["next_steps"])
        self.assertEqual(len(preview["practice_activities"]), 2)
        service.release_feedback(sid, {
            "message_sha256": delivery["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["sections"], preview["sections"])
        self.assertEqual(released["practice_activities"],
                         preview["practice_activities"])

    def test_aligned_session_marks_question_first_preview(self):
        selector = MultiSelector(prefer=("sA", "sB"))
        generator = MultiGenerator()
        recommender, _ = self._aligned(
            {"pool_q1": 0.6, "pool_q2": 0.55})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        sid, _ = self._complete(service, self._rows_wrong())
        self._wait_done(service, sid)
        meta = service._load_meta(sid)
        self.assertEqual(meta["review_presentation_version"],
                         "question_first_worked_review_v1")
        preview = self._delivery(service, sid)["preview"]
        self.assertEqual(preview["template_version"],
                         "phase3_student_feedback_template_v9")
        self.assertEqual(preview["review_presentation_version"],
                         "question_first_worked_review_v1")
        cards = preview["review_examples"]
        self.assertTrue(cards)
        for card in cards:
            self.assertEqual(card["presentation_version"],
                             "question_first_worked_review_v1")
            self.assertEqual(card["scaffold_source"],
                             "application_authored_method_prompt_v1")
            self.assertTrue(card["socratic_prompt"])
            self.assertEqual(card["progressive_hints"], [])
            self.assertEqual(card["option_contrast"], "")
            self.assertIsNone(card["option_contrast_source"])

    def test_missing_presentation_marker_recreates_v8_preview(self):
        selector = MultiSelector(prefer=("sA", "sB"))
        generator = MultiGenerator()
        recommender, _ = self._aligned(
            {"pool_q1": 0.6, "pool_q2": 0.55})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        sid, token = self._complete(service, self._rows_wrong())
        self._wait_done(service, sid)
        meta = service._load_meta(sid)
        meta.pop("review_presentation_version")
        service._save_meta(meta)
        preview = self._delivery(service, sid)["preview"]
        self.assertEqual(preview["template_version"],
                         "phase3_student_feedback_template_v8")
        self.assertNotIn("review_presentation_version", preview)
        for card in preview["review_examples"]:
            for field in ("presentation_version", "scaffold_source",
                          "socratic_prompt", "option_contrast",
                          "option_contrast_source",
                          "progressive_hints"):
                self.assertNotIn(field, card)
        delivery = self._delivery(service, sid)
        service.release_feedback(sid, {
            "message_sha256": delivery["preview_sha256"]})
        service.close(wait=True)
        reopened = DemoService(
            root=Path(self.tmp.name), bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank))
        self.addCleanup(reopened.close, wait=True)
        released = reopened.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["template_version"],
                         "phase3_student_feedback_template_v8")
        for card in released["review_examples"]:
            self.assertNotIn("socratic_prompt", card)

    def test_question_first_card_edits_release_and_rejections(self):
        selector = MultiSelector(prefer=("sA", "sB"))
        generator = MultiGenerator()
        recommender, _ = self._aligned(
            {"pool_q1": 0.6, "pool_q2": 0.55})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        sid, token = self._complete(service, self._rows_wrong())
        self._wait_done(service, sid)
        delivery = self._delivery(service, sid)
        originals = copy.deepcopy(delivery["preview"]
                                  ["review_examples"])
        patch = [{"socratic_prompt": "Teacher prompt.",
                  "option_contrast": "Teacher observation.",
                  "progressive_hints": []},
                 {"option_contrast": ""}]
        res = service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "sections": dict(MULTI_SECTIONS),
            "content": {"review_examples": patch}})
        cards = self._delivery(service, sid)["preview"][
            "review_examples"]
        self.assertEqual(cards[0]["socratic_prompt"],
                         "Teacher prompt.")
        self.assertEqual(cards[0]["option_contrast"],
                         "Teacher observation.")
        self.assertTrue(cards[0]["teacher_edited"])
        self.assertEqual(cards[0]["original_content"],
                         originals[0])
        self.assertEqual(cards[1]["option_contrast"], "")
        self.assertNotIn("teacher_edited", cards[1])
        service.release_feedback(sid, {
            "message_sha256": res["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["review_examples"], cards)
        for bad in ({"progressive_hints": ["x"]},
                    {"progressive_hints": "x"},
                    {"progressive_hints": [""]},
                    {"progressive_hints": [5]},
                    {"socratic_prompt": ""},
                    {"socratic_prompt": 5},
                    {"presentation_version": "x"},
                    {"scaffold_source": "x"},
                    {"option_contrast_source": "x"}):
            with self.assertRaises(ValueError):
                service.save_feedback_edit(sid, {
                    "message_sha256": res["preview_sha256"],
                    "sections": dict(MULTI_SECTIONS),
                    "content": {"review_examples": [bad, {}]}})

    def test_teacher_content_edit_selected_practice(self):
        selector = MultiSelector(prefer=("sA", "sB"))
        generator = MultiGenerator()
        recommender, _ = self._aligned(
            {"pool_q1": 0.6, "pool_q2": 0.55})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        sid, token = self._complete(
            service, self._rows_wrong(("sA", "sB", "sC")))
        self._wait_done(service, sid)
        delivery = self._delivery(service, sid)
        activities = delivery["preview"]["practice_activities"]
        self.assertEqual(len(activities), 2)
        originals = copy.deepcopy(activities)
        for activity in originals:
            self.assertEqual(activity["status"], "selected")
            self.assertTrue(activity["instruction"])
            self.assertTrue(activity["question"]["options"])
            self.assertTrue(activity["relationship"]["message"])
        patches = []
        for activity in originals:
            count = len(activity["question"]["options"])
            patches.append({
                "topic_label": f"Edited topic {activity['topic_label']}",
                "message": "Edited practice message.",
                "instruction": "Edited instruction.",
                "relationship": {"message": "Edited relationship."},
                "question": {
                    "skill_name": "Edited skill",
                    "concept_name": "Edited concept",
                    "text": "Edited practice question?",
                    "options": [f"Opt {i}" for i in range(count)]}})
        res = service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "sections": dict(MULTI_SECTIONS),
            "content": {"practice_activities": patches}})
        preview = self._delivery(service, sid)["preview"]
        edited = preview["practice_activities"]
        for activity, original, patch in zip(
                edited, originals, patches):
            self.assertTrue(activity["teacher_edited"])
            self.assertEqual(activity["original_content"], original)
            self.assertEqual(activity["status"], "selected")
            self.assertEqual(activity["relationship"]["kind"],
                             original["relationship"]["kind"])
            for key, value in patch.items():
                if key == "question":
                    self.assertEqual(activity["question"], value)
                elif key == "relationship":
                    self.assertEqual(activity["relationship"]["message"],
                                     value["message"])
                else:
                    self.assertEqual(activity[key], value)
        self.assertEqual(preview["practice"], edited[0])
        with self.assertRaises(ValueError):
            service.save_feedback_edit(sid, {
                "message_sha256": res["preview_sha256"],
                "sections": dict(MULTI_SECTIONS),
                "content": {"practice_activities": [
                    {"question": {"options": ["only"]}}, {}]}})
        with self.assertRaises(ValueError):
            service.save_feedback_edit(sid, {
                "message_sha256": res["preview_sha256"],
                "sections": dict(MULTI_SECTIONS),
                "content": {"practice_activities": [
                    {"relationship": {"kind": "same_content"}}, {}]}})
        service.release_feedback(
            sid, {"message_sha256": res["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["practice_activities"], edited)

    def test_aligned_stage_failure_terminal_fallback(self):
        selector = MultiSelector(prefer=("sA", "sB"))
        generator = MultiGenerator()
        recommender, predictor = self._aligned({"pool_q1": 0.6})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        real_submit = service.diag_pool.submit

        def flaky(fn, *args, **kwargs):
            if fn == service._aligned_recommendation:
                raise RuntimeError("queue down")
            return real_submit(fn, *args, **kwargs)

        service.diag_pool.submit = flaky
        sid, token = self._complete(
            service, self._rows_wrong(("sA", "sB", "sC")))
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)
            ["provider_job"]["status"] == "fallback"))
        end = self._end(service, sid)
        self.assertEqual(end["recommendation_job"]["status"],
                         "unavailable")
        self.assertEqual(end["recommendation_job"]["reason"],
                         "provider_error")
        statuses = [c["context"]["status"]
                    for c in end["practice_contexts"]]
        self.assertEqual(statuses, ["unavailable", "unavailable"])
        executions = end["provider_job"]["executions"]["student"]
        self.assertEqual(executions["selector"]["status"],
                         "fake_completed")
        self.assertEqual(executions["generator"]["status"],
                         "skipped_after_worker_error")
        self.assertEqual(service._pending_diag, 0)
        self.assertEqual(service._pending_provider, 0)
        public = service.snapshot(sid, token)
        self.assertIsNone(public["feedback"]["end"])
        self.assertNotIn("pool_q", json.dumps(public))

    def test_queue_capacity_marks_slots_unavailable(self):
        generator = MultiGenerator()
        recommender, _ = self._aligned({"pool_q1": 0.6})
        service = self._service(generator=generator,
                                recommender=recommender)
        rows = self._rows_wrong()
        created = service.create_session()
        sid, _ = created["session_id"], created["student_token"]
        for row in rows[:-1]:
            service.submit_response(sid, created["student_token"], row)
        service._pending_diag = service.max_pending_jobs
        service.submit_response(sid, created["student_token"], rows[-1])
        self._wait_done(service, sid)
        end = self._end(service, sid)
        self.assertEqual(end["recommendation_job"]["status"],
                         "unavailable")
        self.assertEqual(end["recommendation_job"]["reason"],
                         "demo_diagnostics_queue_capacity")
        statuses = [c["context"]["status"]
                    for c in end["practice_contexts"]]
        self.assertEqual(statuses, ["unavailable", "unavailable"])

    @staticmethod
    def _subtopic_pool_and_graph():
        def q(qid, skill):
            text = f"Pool practice {qid}?"
            return {"question_id": qid, "skill_id": skill,
                    "item_id": f"item-{qid}", "text": text,
                    "options": ["one", "two", "three"],
                    "content_text":
                        text + " [OPTIONS] one | two | three"}
        questions = [q("pool_exact", "sA"), q("pool_broad", "sA"),
                     q("pool_sb", "sB")]
        pool = {"schema": "phase3_shadow_practice_pool_v1",
                "scope": "research_only_not_learner_approved",
                "questions": questions}
        concepts = {"pool_exact": "sub_sA", "pool_broad": "concept_sA",
                    "pool_sb": "concept_sB"}
        graph = {"exercises": [
            {"question_id": x["question_id"],
             "source_id": "practice_pool_v2", "role": "practice",
             "skill_id": x["skill_id"],
             "concept_id": concepts[x["question_id"]],
             "concept_label": f"Concept {x['question_id']}",
             "text": x["text"], "options": list(x["options"]),
             "mathematical_task": f"Task {x['question_id']}"}
            for x in questions]}
        return pool, graph

    def test_exact_subtopic_chosen_and_relationship_same_content(self):
        import feedback_practice
        import research_runtime
        from unittest import mock
        pool, graph = self._subtopic_pool_and_graph()
        selector = MultiSelector(prefer=("sA", "sB"))
        generator = MultiGenerator()
        predictor = RecordingPredictor(
            {"pool_exact": 0.5, "pool_broad": 0.6, "pool_sb": 0.55})
        recommender = FakeAlignedRecommender(pool, predictor)
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        with mock.patch.object(
                feedback_practice, "load_current_practice_pool",
                return_value=copy.deepcopy(pool)), \
             mock.patch.object(
                research_runtime, "load_current_practice_pool",
                return_value=copy.deepcopy(pool)), \
             mock.patch.object(
                feedback_practice, "teacher_content_context",
                return_value=copy.deepcopy(graph)):
            sid, _ = self._complete(
                service, self._rows_wrong(("sA", "sB", "sC")))
            self._wait_done(service, sid)
            end = self._end(service, sid)
            activities = self._delivery(service, sid)["preview"][
                "practice_activities"]
        self.assertEqual(
            recommender.subtopic_calls,
            [{"sA": "sub_sA", "sB": "sub_sB"}])
        self.assertEqual(
            sorted(end["recommendation"]["selected_question_ids"]),
            ["pool_exact", "pool_sb"])
        self.assertEqual(end["recommendation"]["review_subtopic_ids"],
                         {"sA": "sub_sA", "sB": "sub_sB"})
        self.assertEqual(len(activities), 2)
        by_label = {a["topic_label"]: a for a in activities}
        self.assertEqual(by_label["Subtopic sA"]["relationship"]["kind"],
                         "same_content")
        self.assertEqual(by_label["Subtopic sA"]["question"]["text"],
                         "Pool practice pool_exact?")
        self.assertEqual(by_label["Subtopic sB"]["relationship"]["kind"],
                         "same_topic_different_content")
        for activity in activities:
            self.assertEqual(activity["status"], "selected")

    def test_no_exact_inband_keeps_same_topic_warning(self):
        selector = MultiSelector(prefer=("sA", "sB"))
        generator = MultiGenerator()
        recommender, _ = self._aligned(
            {"pool_q1": 0.6, "pool_q2": 0.55})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        sid, _ = self._complete(
            service, self._rows_wrong(("sA", "sB", "sC")))
        self._wait_done(service, sid)
        self.assertEqual(
            recommender.subtopic_calls,
            [{"sA": "sub_sA", "sB": "sub_sB"}])
        activities = self._delivery(service, sid)["preview"][
            "practice_activities"]
        self.assertEqual(
            {a["relationship"]["kind"] for a in activities},
            {"same_topic_different_content"})
        for activity in activities:
            self.assertTrue(
                activity["relationship"]["message"].startswith(
                    "This is related practice on "))
            self.assertIn(", not ", activity["relationship"]["message"])

    def test_invalid_graph_mapping_unavailable_no_replacement(self):
        import feedback_practice
        from unittest import mock
        selector = MultiSelector(prefer=("sA", "sB"))
        generator = MultiGenerator()
        recommender, predictor = self._aligned(
            {"pool_q1": 0.6, "pool_q2": 0.55})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        with mock.patch.object(
                feedback_practice, "teacher_content_context",
                return_value={"exercises": []}):
            sid, _ = self._complete(
                service, self._rows_wrong(("sA", "sB", "sC")))
            self._wait_done(service, sid)
        end = self._end(service, sid)
        self.assertEqual(end["recommendation"]["status"], "unavailable")
        self.assertEqual(predictor.calls, [])
        statuses = [c["context"]["status"]
                    for c in end["practice_contexts"]]
        self.assertEqual(statuses, ["unavailable", "unavailable"])
        activities = self._delivery(service, sid)["preview"][
            "practice_activities"]
        self.assertEqual(
            {a["status"] for a in activities}, {"unavailable"})
        self.assertTrue(
            all(a["question"] is None for a in activities))

    def test_closing_fallback_contexts_per_mode(self):
        generator = MultiGenerator()
        recommender, _ = self._aligned({"pool_q1": 0.6})
        service = self._service(generator=generator,
                                recommender=recommender)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        rows = self._rows_wrong()
        for row in rows[:-1]:
            service.submit_response(sid, token, row)
        service._closing = True
        service.submit_response(sid, token, rows[-1])
        end = self._end(service, sid)
        self.assertEqual(end["provider_job"]["status"], "fallback")
        statuses = [c["context"]["status"]
                    for c in end["practice_contexts"]]
        self.assertEqual(statuses, ["unavailable", "unavailable"])

        root2 = Path(self.tmp.name) / "no_rec"
        service2 = DemoService(root=root2, bank=self.bank,
                               taxonomy=self.taxonomy,
                               diagnostics=fake_diagnostics(self.bank),
                               provider_mode="hosted",
                               generator=MultiGenerator())
        self.addCleanup(service2.close, wait=True)
        created = service2.create_session()
        sid2, token2 = created["session_id"], created["student_token"]
        for row in rows[:-1]:
            service2.submit_response(sid2, token2, row)
        service2._closing = True
        service2.submit_response(sid2, token2, rows[-1])
        end2 = self._end(service2, sid2)
        self.assertEqual(end2["provider_job"]["status"], "fallback")
        statuses2 = [c["context"]["status"]
                     for c in end2["practice_contexts"]]
        self.assertEqual(statuses2, ["disabled", "disabled"])

    def test_interrupted_practice_recovery_unavailable(self):
        generator = MultiGenerator()
        recommender, _ = self._aligned({"pool_q1": 0.6})
        service = self._service(generator=generator,
                                recommender=recommender)
        sid, token = self._complete(service, self._rows_wrong())
        self._wait_done(service, sid)
        meta = service._load_meta(sid)
        end = meta["checkpoints"]["end"]
        for key in ("student_review", "recommendation",
                    "recommendation_job", "practice_contexts",
                    "practice_context"):
            end.pop(key, None)
        meta["provider_job"].update(
            {"status": "pending", "executions": {"student": {
                "selector": {"status": "completed"}}}})
        service._save_meta(meta)
        service.close(wait=True)
        reopened = DemoService(
            root=Path(self.tmp.name), bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            provider_mode="hosted", generator=MultiGenerator(),
            recommender=recommender)
        self.addCleanup(reopened.close, wait=True)
        meta = reopened._load_meta(sid)
        end = meta["checkpoints"]["end"]
        self.assertEqual(meta["provider_job"]["status"], "fallback")
        statuses = [c["context"]["status"]
                    for c in end["practice_contexts"]]
        self.assertEqual(statuses, ["unavailable", "unavailable"])
        self.assertIsNotNone(end.get("student_review"))
        public = reopened.snapshot(sid, token)
        self.assertIsNone(public["feedback"]["end"])

    def test_teacher_config_reflects_review_mode(self):
        from evidence_feedback_policy import (
            MULTI_REVIEW_POLICY_VERSION, MULTI_SELECTION_INSTRUCTIONS,
            POLICY_VERSION, SELECTION_INSTRUCTIONS)
        from full_feedback import (
            FULL_FEEDBACK_INSTRUCTIONS, FULL_FEEDBACK_PROMPT_VERSION,
            MULTI_FULL_FEEDBACK_INSTRUCTIONS,
            MULTI_FULL_FEEDBACK_PROMPT_VERSION)
        multi = self._service(provider_mode="rules")
        config = multi.teacher_config()
        self.assertEqual(config["review_mode"], "aligned_two_topic")
        self.assertEqual(config["selection_instructions"],
                         MULTI_SELECTION_INSTRUCTIONS)
        self.assertEqual(config["generation_instructions"],
                         MULTI_FULL_FEEDBACK_INSTRUCTIONS)
        self.assertEqual(config["generation_prompt_version"],
                         MULTI_FULL_FEEDBACK_PROMPT_VERSION)
        self.assertEqual(config["review_selection_policy_version"],
                         MULTI_REVIEW_POLICY_VERSION)
        self.assertEqual(config["policy_version"], POLICY_VERSION)
        self.assertEqual(config["review_topic_limit"], 2)
        self.assertEqual(config["practice_question_limit"], 2)
        self.assertEqual(config["practice_alignment"],
                         "selected_review_skills")
        legacy = DemoService(
            root=Path(self.tmp.name) / "legacy", bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            review_mode="legacy_single")
        self.addCleanup(legacy.close, wait=True)
        config = legacy.teacher_config()
        self.assertEqual(config["review_mode"], "legacy_single")
        self.assertEqual(config["selection_instructions"],
                         SELECTION_INSTRUCTIONS)
        self.assertEqual(config["generation_instructions"],
                         FULL_FEEDBACK_INSTRUCTIONS)
        self.assertEqual(config["generation_prompt_version"],
                         FULL_FEEDBACK_PROMPT_VERSION)
        self.assertEqual(config["review_selection_policy_version"],
                         POLICY_VERSION)
        self.assertEqual(config["review_topic_limit"], 1)
        self.assertEqual(config["practice_question_limit"], 1)
        self.assertIsNone(config["practice_alignment"])

    def test_restart_preserves_released_package_and_old_meta(self):
        selector = MultiSelector()
        generator = MultiGenerator()
        recommender, _ = self._aligned(
            {"pool_q1": 0.6, "pool_q2": 0.55})
        service = self._service(selector=selector, generator=generator,
                                recommender=recommender)
        sid, token = self._complete(service, self._rows_wrong())
        self._wait_done(service, sid)
        delivery = self._delivery(service, sid)
        service.release_feedback(sid, {
            "message_sha256": delivery["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        meta = service._load_meta(sid)
        self.assertEqual(
            meta["feedback_selection_version"],
            "observed_distinct_skill_review_v1")
        meta.pop("feedback_selection_version")
        service._save_meta(meta)
        service.close(wait=True)
        reopened = DemoService(root=Path(self.tmp.name),
                               bank=self.bank, taxonomy=self.taxonomy,
                               diagnostics=fake_diagnostics(self.bank))
        self.addCleanup(reopened.close, wait=True)
        meta = reopened._load_meta(sid)
        self.assertNotIn("feedback_selection_version", meta)
        self.assertEqual(
            reopened.snapshot(sid, token)["feedback"]["end"], released)


class GatedDiagnostics:
    """FakeKT-backed diagnostics whose 40-answer evaluation parks on a
    gate so tests can hold the end diagnostics job in 'pending'."""

    def __init__(self, bank, gate, end_result=None):
        self.inner = LiveDiagnostics(model_loader=lambda: FakeKT(bank))
        self.gate = gate
        self.end_result = end_result

    def warmup(self):
        return self.inner.warmup()

    @property
    def state(self):
        return self.inner.state

    def evaluate(self, bank, taxonomy, rows):
        if len(rows) == 40:
            self.gate.wait(timeout=60)
            if self.end_result is not None:
                return self.end_result
        return self.inner.evaluate(bank, taxonomy, rows)


class ProviderDiagnosticsRaceTests(unittest.TestCase):
    """Deterministic reproduction of the stale-meta race: a diagnostics
    result persisted between the provider job's waiting_for_practice save
    and its practice-admission save must not be reverted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        import feedback_practice
        import research_runtime
        from tests.test_feedback_practice import make_graph, make_pool
        self.pool = make_pool(("sA", "sB"))
        from unittest import mock
        for patch in (
                mock.patch.object(
                    feedback_practice, "load_current_practice_pool",
                    return_value=copy.deepcopy(self.pool)),
                mock.patch.object(
                    research_runtime, "load_current_practice_pool",
                    return_value=copy.deepcopy(self.pool)),
                mock.patch.object(
                    feedback_practice, "teacher_content_context",
                    return_value=make_graph(self.pool))):
            patch.start()
            self.addCleanup(patch.stop)

    def _rows_wrong(self, skills=("sA", "sB")):
        rows = []
        for q in self.bank["questions"]:
            index = q["answer_index"]
            if q["skill_id"] in skills:
                index = (index + 1) % len(q["options"])
            rows.append({"question_id": q["question_id"],
                         "selected_index": index})
        return rows

    def _service(self, gate, end_result=None):
        predictor = RecordingPredictor({"pool_q1": 0.6, "pool_q2": 0.55})
        service = DemoService(
            root=Path(self.tmp.name),
            bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=GatedDiagnostics(self.bank, gate, end_result),
            recommender=FakeAlignedRecommender(self.pool, predictor))
        self.addCleanup(service.close, wait=True)
        return service

    def _interleave_diagnostics(self, service, sid, job, result):
        original_save = service._save_meta
        fired = []

        def wrapped(meta):
            original_save(meta)
            if (not fired
                    and meta.get("provider_job", {}).get("status")
                    == "waiting_for_practice"
                    and "multi_selection"
                    in meta["checkpoints"]["end"]):
                fired.append(True)
                latest = service._load_meta(sid)
                end = latest["checkpoints"]["end"]
                end["diagnostics_job"] = dict(job)
                end["diagnostics"] = copy.deepcopy(result)
                original_save(latest)

        service._save_meta = wrapped
        return fired

    def _create(self, service):
        created = service.create_session()
        return created["session_id"], created["student_token"]

    def _submit(self, service, sid, token):
        for row in self._rows_wrong():
            service.submit_response(sid, token, row)

    def _wait_provider_done(self, service, sid):
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)
            ["provider_job"]["status"] in ("ready", "fallback")))

    def test_interleaved_ready_diagnostics_survive_admission(self):
        gate = threading.Event()
        service = self._service(gate)
        sid, token = self._create(service)
        marker = {"status": "ready", "checkpoint": "end",
                  "answer_count": 40, "marker": "interleaved_ready"}
        fired = self._interleave_diagnostics(
            service, sid, {"status": "ready",
                           "finished_at": "2026-10-15T00:00:00Z"},
            marker)
        self._submit(service, sid, token)
        self._wait_provider_done(service, sid)
        self.assertTrue(fired)
        end = service._load_meta(sid)["checkpoints"]["end"]
        self.assertEqual(end["diagnostics_job"]["status"], "ready")
        self.assertEqual(end["diagnostics"], marker)
        self.assertTrue(wait_for(lambda: service._jobs_complete(sid)))
        gate.set()
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["end"]
            ["diagnostics"].get("kt") is not None))
        self.assertTrue(service._jobs_complete(sid))

    def test_interleaved_unavailable_diagnostics_survive_admission(self):
        gate = threading.Event()
        service = self._service(gate)
        sid, token = self._create(service)
        marker = {"status": "unavailable", "reason": "model_unavailable",
                  "checkpoint": "end", "answer_count": 40,
                  "marker": "interleaved_unavailable"}
        fired = self._interleave_diagnostics(
            service, sid, {"status": "unavailable",
                           "reason": "model_unavailable",
                           "finished_at": "2026-10-15T00:00:00Z"},
            marker)
        self._submit(service, sid, token)
        self._wait_provider_done(service, sid)
        self.assertTrue(fired)
        end = service._load_meta(sid)["checkpoints"]["end"]
        self.assertEqual(end["diagnostics_job"]["status"], "unavailable")
        self.assertEqual(end["diagnostics"], marker)
        self.assertTrue(wait_for(lambda: service._jobs_complete(sid)))
        gate.set()

    def test_pending_end_diagnostics_block_jobs_complete(self):
        gate = threading.Event()
        service = self._service(gate)
        sid, token = self._create(service)
        self._submit(service, sid, token)
        self._wait_provider_done(service, sid)
        self.assertFalse(service._jobs_complete(sid))
        self.assertEqual(service._load_meta(sid)["checkpoints"]["end"]
                         ["diagnostics_job"]["status"], "pending")
        gate.set()
        self.assertTrue(wait_for(lambda: service._jobs_complete(sid)))
        self.assertEqual(service._load_meta(sid)["checkpoints"]["end"]
                         ["diagnostics_job"]["status"], "ready")


class RecordingFullGenerator(MultiGenerator):
    def __init__(self, events=None):
        super().__init__(events)
        self.payloads = []

    def generate(self, payload):
        self.payloads.append(copy.deepcopy(payload))
        return super().generate(payload)


class WorkedFirstDiagnosticsWaitTests(unittest.TestCase):
    """Hosted worked-first provider calls wait for terminal diagnostics
    so the authored context carries real or explicitly unavailable
    projections; question-first never waits and never sees it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        import feedback_practice
        import research_runtime
        from tests.test_feedback_practice import make_graph, make_pool
        self.pool = make_pool(("sA", "sB"))
        from unittest import mock
        for patch in (
                mock.patch.object(
                    feedback_practice, "load_current_practice_pool",
                    return_value=copy.deepcopy(self.pool)),
                mock.patch.object(
                    research_runtime, "load_current_practice_pool",
                    return_value=copy.deepcopy(self.pool)),
                mock.patch.object(
                    feedback_practice, "teacher_content_context",
                    return_value=make_graph(self.pool))):
            patch.start()
            self.addCleanup(patch.stop)

    def _rows_wrong(self, skills=("sA", "sB")):
        rows = []
        for q in self.bank["questions"]:
            index = q["answer_index"]
            if q["skill_id"] in skills:
                index = (index + 1) % len(q["options"])
            rows.append({"question_id": q["question_id"],
                         "selected_index": index})
        return rows

    def _service(self, root, gate, generator, end_result=None):
        service = DemoService(
            root=root, bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=GatedDiagnostics(self.bank, gate, end_result),
            provider_mode="hosted", generator=generator)
        self.addCleanup(service.close, wait=True)
        self.addCleanup(gate.set)
        return service

    def _complete(self, service, variant):
        created = service.create_session(feedback_variant=variant)
        sid, token = created["session_id"], created["student_token"]
        for row in self._rows_wrong():
            service.submit_response(sid, token, row)
        return sid

    def _provider_status(self, service, sid):
        try:
            return service._load_meta(sid)["provider_job"]["status"]
        except FileNotFoundError:
            return None

    def test_worked_first_waits_then_sends_terminal_projections(self):
        gate = threading.Event()
        generator = RecordingFullGenerator()
        service = self._service(Path(self.tmp.name), gate, generator)
        sid = self._complete(service, "worked_first")
        self.assertTrue(wait_for(
            lambda: self._provider_status(service, sid)
            == "waiting_for_diagnostics"))
        self.assertEqual(generator.calls, 0)
        self.assertFalse(service._jobs_complete(sid))
        self.assertEqual(
            service._load_meta(sid)["checkpoints"]["end"]
            ["provider_job"]["status"], "waiting_for_diagnostics")

        gate.set()
        self.assertTrue(wait_for(
            lambda: self._provider_status(service, sid)
            in ("ready", "fallback")))
        self.assertEqual(generator.calls, 1)
        wf = generator.payloads[0]["worked_first_context"]
        diag = LiveDiagnostics(
            model_loader=lambda: FakeKT(self.bank))
        rows = self._rows_wrong()
        expected_mid = diag.evaluate(
            self.bank, self.taxonomy, rows[:20])
        expected_end = diag.evaluate(self.bank, self.taxonomy, rows)
        self.assertEqual(
            wf["model_context"]["checkpoints"]["midpoint"],
            {"status": "available", "answer_count": 20,
             "pre_answer_probabilities":
                 expected_mid["kt"]["p_correct_before_each_answer"]})
        self.assertEqual(
            wf["model_context"]["checkpoints"]["end"],
            {"status": "available", "answer_count": 40,
             "pre_answer_probabilities":
                 expected_end["kt"]["p_correct_before_each_answer"]})
        self.assertEqual(len(wf["assessment_records"]), 40)
        meta = service._load_meta(sid)
        review = meta["checkpoints"]["end"]["student_review"]
        self.assertEqual(
            review["trace"]["generation_context_version"],
            "worked_first_assessment_research_context_v2")
        self.assertEqual(
            review["provider_context"]["version"],
            "worked_first_assessment_research_context_v2")
        self.assertEqual(
            meta["feedback_context_version"],
            "worked_first_assessment_research_context_v2")
        self.assertEqual(
            meta["provider_job"]["prompt_versions"]["generation"],
            "aitta_worked_first_evidence_author_v6")
        preview = service.teacher_session(sid)["feedback_delivery"][
            "preview"]
        self.assertNotIn("provider_context", json.dumps(preview))
        self.assertTrue(wait_for(lambda: service._jobs_complete(sid)))

    def test_worked_first_unavailable_diagnostics_still_invoke(self):
        gate = threading.Event()
        generator = RecordingFullGenerator()
        service = self._service(
            Path(self.tmp.name), gate, generator,
            end_result={"status": "unavailable",
                        "reason": "model_unavailable",
                        "checkpoint": "end", "answer_count": 40})
        sid = self._complete(service, "worked_first")
        self.assertTrue(wait_for(
            lambda: self._provider_status(service, sid)
            == "waiting_for_diagnostics"))
        self.assertEqual(generator.calls, 0)
        gate.set()
        self.assertTrue(wait_for(
            lambda: self._provider_status(service, sid)
            in ("ready", "fallback")))
        self.assertEqual(generator.calls, 1)
        wf = generator.payloads[0]["worked_first_context"]
        self.assertEqual(
            wf["model_context"]["checkpoints"]["end"],
            {"status": "unavailable"})

    def test_question_first_never_waits_or_sends_context(self):
        from full_feedback import MULTI_FULL_FEEDBACK_PROMPT_VERSION
        gate = threading.Event()
        generator = RecordingFullGenerator()
        service = self._service(Path(self.tmp.name), gate, generator)
        sid = self._complete(service, "question_first")
        self.assertTrue(wait_for(
            lambda: self._provider_status(service, sid)
            in ("ready", "fallback")))
        self.assertEqual(generator.calls, 1)
        self.assertNotIn("worked_first_context",
                         generator.payloads[0])
        meta = service._load_meta(sid)
        self.assertNotIn("feedback_context_version", meta)
        self.assertEqual(
            meta["provider_job"]["prompt_versions"]["generation"],
            MULTI_FULL_FEEDBACK_PROMPT_VERSION)
        review = meta["checkpoints"]["end"]["student_review"]
        self.assertNotIn("generation_context_version", review["trace"])
        self.assertNotIn("provider_context", review)
        self.assertFalse(service._jobs_complete(sid))
        gate.set()
        self.assertTrue(wait_for(lambda: service._jobs_complete(sid)))

    def test_close_during_wait_then_restart_publishes_fallback(self):
        gate = threading.Event()
        generator = RecordingFullGenerator()
        root = Path(self.tmp.name) / "svc"
        service = self._service(root, gate, generator)
        sid = self._complete(service, "worked_first")
        self.assertTrue(wait_for(
            lambda: self._provider_status(service, sid)
            == "waiting_for_diagnostics"))
        # close(wait=False) sets _closing so the diagnostics wait exits
        # without blocking on the gated end diagnostics.
        service.close(wait=False)
        self.assertTrue(wait_for(
            lambda: service._pending_provider == 0))
        self.assertEqual(generator.calls, 0)
        gate.set()
        service.close(wait=True)
        reopened = DemoService(
            root=root, bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            provider_mode="hosted",
            generator=RecordingFullGenerator())
        self.addCleanup(reopened.close, wait=True)
        meta = reopened._load_meta(sid)
        self.assertEqual(meta["provider_job"]["status"], "fallback")
        self.assertTrue(meta["provider_job"]["interrupted"])
        self.assertIsNotNone(
            meta["checkpoints"]["end"]["student_review"])


if __name__ == "__main__":
    unittest.main()
