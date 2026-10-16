"""Teacher-only midpoint Aitta draft tests.

The student midpoint check-in stays deterministic; the provider draft is
a teacher-comparison artifact only. The wire carries first-half
broad-topic aggregates only - no questions, answers, identifiers,
hashes, baselines, or second-half information. All fixtures are offline
fakes; no network.
"""
import copy
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from pathlib import Path

import phase3_paths  # noqa: F401
from aitta_generator import AittaGenerationError, AittaGenerator
from demo_api import make_server
from demo_service import DemoService
from evidence_feedback import (
    MIDPOINT_MESSAGE_VERSION as STUDENT_MESSAGE_VERSION,
    build_evidence, compose_midpoint_message)
from midpoint_feedback import (
    CANDIDATE_ID, INSTRUCTIONS, MESSAGE_VERSION, MIDPOINT_MAX_TOKENS,
    PROMPT_VERSION, SCHEMA, CachedMidpointGenerator,
    MidpointAittaGenerator, build_midpoint_input,
    compose_midpoint_draft, validate_midpoint_input)
from replay_provider_scenarios import CaptureCache
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_demo_service import fake_diagnostics, wait_for
from tests.test_full_feedback import (
    API_KEY, BASE_URL, MODEL, RecordingOpener, wire_reply)

PIN = "135790"
SENTINEL = "MIDPOINT-PROVIDER-SENTINEL"

AUTHORED = {
    "assessment_summary":
        "You have reached the halfway point; in this first set "
        "eighteen of twenty answers were correct.",
    "observed_strengths":
        "Correct answers were recorded across the topics in this "
        "first set.",
    "review_focus":
        "Two incorrect answers were recorded in the first set.",
    "next_steps":
        "Take a short break if you like, then continue with the "
        "remaining twenty questions."}


def midpoint_rows(bank, wrong_positions=()):
    rows = []
    for index, q in enumerate(bank["questions"][:20]):
        selected = q["answer_index"]
        if index in wrong_positions:
            selected = (selected + 1) % len(q["options"])
        rows.append({"question_id": q["question_id"],
                     "selected_index": selected})
    return rows


class _EndStub:
    """End-of-assessment generator stub; midpoint tests never reach it."""
    thread_safe = True

    def generate(self, payload):
        raise RuntimeError("end adapter not under test")


class MidpointInputTests(unittest.TestCase):
    """Input contract and wire projection."""

    def setUp(self):
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.evidence = build_evidence(
            self.bank, self.taxonomy,
            midpoint_rows(self.bank, wrong_positions=(0, 5)))
        self.end_evidence = build_evidence(
            self.bank, self.taxonomy,
            responses(self.bank, count=40))
        self.input = build_midpoint_input(self.evidence)

    def test_build_input_shape_and_checkpoint(self):
        self.assertEqual(self.input["schema"], SCHEMA)
        self.assertEqual(self.input["prompt_version"], PROMPT_VERSION)
        self.assertEqual(self.input["checkpoint"], "midpoint")
        self.assertEqual(self.input["evidence"]["total"]["out_of"], 20)

    def test_end_evidence_rejected(self):
        with self.assertRaises(ValueError):
            build_midpoint_input(self.end_evidence)
        payload = copy.deepcopy(self.input)
        payload["evidence"] = self.end_evidence
        with self.assertRaises(ValueError):
            validate_midpoint_input(payload)

    def test_validate_rejects_malformed_and_tampered(self):
        for payload in (None, {}, {**self.input, "extra": 1},
                        {k: v for k, v in self.input.items()
                         if k != "evidence"},
                        dict(self.input, prompt_version="stale_v0"),
                        dict(self.input, schema="other_schema")):
            with self.assertRaises(ValueError):
                validate_midpoint_input(payload)

    def test_wire_projection_aggregates_only(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=AUTHORED))
        inner = AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                               opener=opener)
        generator = MidpointAittaGenerator(inner)
        reply = generator.generate(self.input)
        self.assertEqual(reply,
                         {"candidate_id": CANDIDATE_ID,
                          "sections": dict(AUTHORED)})
        self.assertEqual(len(opener.calls), 1)
        body = opener.calls[0]["body"]
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["max_tokens"], MIDPOINT_MAX_TOKENS)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][0]["content"], INSTRUCTIONS)
        wire = json.loads(body["messages"][1]["content"])
        self.assertEqual(set(wire), {
            "schema", "prompt_version", "checkpoint", "answered_count",
            "assessment_length", "remaining_count", "total", "skills"})
        self.assertEqual(wire["answered_count"], 20)
        self.assertEqual(wire["assessment_length"], 40)
        self.assertEqual(wire["remaining_count"], 20)
        self.assertEqual(wire["total"],
                         {"correct": 18, "incorrect": 2, "out_of": 20})
        for skill in wire["skills"]:
            self.assertEqual(set(skill), {"skill_name", "correct",
                                          "incorrect", "out_of"})
        # Only the user-message context is scanned for leaks; the
        # verbatim system prompt deliberately names some of these words.
        raw = body["messages"][1]["content"]
        for forbidden in ("question_id", "answer_index",
                          "selected_index", "item_id", "exercise_id",
                          "sha256", "subtopic", "halves", "baseline",
                          "feedback_plan", "practice", "review_example",
                          "diagnostic", "kt_", API_KEY):
            self.assertNotIn(forbidden, raw)

    def test_invalid_input_denied_before_request(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=AUTHORED))
        generator = MidpointAittaGenerator(
            AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                           opener=opener))
        with self.assertRaises(ValueError):
            generator.generate({"schema": SCHEMA, "bad": True})
        self.assertEqual(opener.calls, [])


class MidpointGeneratorTests(unittest.TestCase):
    """Sanitized response handling, metadata, and retained drafts."""

    def setUp(self):
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.evidence = build_evidence(
            self.bank, self.taxonomy,
            midpoint_rows(self.bank, wrong_positions=(0, 5)))
        self.input = build_midpoint_input(self.evidence)

    def _generator(self, opener):
        return MidpointAittaGenerator(
            AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                           opener=opener))

    def test_metadata_role_and_version(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=AUTHORED))
        generator = self._generator(opener)
        generator.generate(self.input)
        meta = generator.last_metadata
        self.assertEqual(meta["status"], "completed")
        self.assertEqual(meta["role"], "midpoint_check_in")
        self.assertEqual(meta["prompt_version"], PROMPT_VERSION)
        self.assertIn("transport", meta)

    def test_rejected_section_retains_draft(self):
        bad = dict(AUTHORED, review_focus="see <b>this</b>")
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=bad))
        generator = self._generator(opener)
        with self.assertRaises(AittaGenerationError):
            generator.generate(self.input)
        meta = generator.last_metadata
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["rejection_code"],
                         "markup_in_review_focus")
        draft = generator.last_rejected_draft
        self.assertIn("<b>this</b>", draft["content"])
        self.assertNotIn(API_KEY, draft["content"])

    def test_secret_in_sections_rejected(self):
        bad = dict(AUTHORED, observed_strengths=f"key is {API_KEY}")
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=bad))
        generator = self._generator(opener)
        with self.assertRaises(AittaGenerationError):
            generator.generate(self.input)
        self.assertNotIn(API_KEY, json.dumps(generator.last_metadata))

    def test_transport_failure_sanitized(self):
        opener = RecordingOpener(error=RuntimeError("boom" + API_KEY))
        generator = self._generator(opener)
        with self.assertRaises(AittaGenerationError):
            generator.generate(self.input)
        meta = generator.last_metadata
        self.assertEqual(meta["rejection_code"], "transport_failure")
        self.assertNotIn(API_KEY, json.dumps(meta))
        self.assertIsNone(generator.last_rejected_draft)


class MidpointDraftComposeTests(unittest.TestCase):
    """compose_midpoint_draft preserves all application-owned data."""

    def setUp(self):
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.evidence = build_evidence(
            self.bank, self.taxonomy,
            midpoint_rows(self.bank, wrong_positions=(0, 5)))

    def test_paragraphs_replaced_counts_preserved(self):
        base = compose_midpoint_message(self.evidence)
        draft = compose_midpoint_draft(
            self.evidence,
            {"candidate_id": CANDIDATE_ID, "sections": dict(AUTHORED)})
        self.assertEqual(draft["total"], base["total"])
        self.assertEqual(draft["skills"], base["skills"])
        self.assertEqual([s["kind"] for s in draft["sections"]],
                         [s["kind"] for s in base["sections"]])
        self.assertEqual([s["title"] for s in draft["sections"]],
                         [s["title"] for s in base["sections"]])
        self.assertEqual(
            [s["text"] for s in draft["sections"]],
            [AUTHORED[k] for k in (
                "assessment_summary", "observed_strengths",
                "review_focus", "next_steps")])
        self.assertEqual(draft["text"],
                         "\n\n".join(AUTHORED.values()))
        # The saved student baseline is not mutated.
        self.assertNotEqual(base["sections"][0]["text"],
                            AUTHORED["assessment_summary"])

    def test_invalid_reply_rejected(self):
        wrong_id = {"candidate_id": "other", "sections": dict(AUTHORED)}
        with self.assertRaises(ValueError):
            compose_midpoint_draft(self.evidence, wrong_id)
        end_evidence = build_evidence(
            self.bank, self.taxonomy,
            responses(self.bank, count=40))
        reply = {"candidate_id": CANDIDATE_ID,
                 "sections": dict(AUTHORED)}
        with self.assertRaises(ValueError):
            compose_midpoint_draft(end_evidence, reply)


class MidpointCacheTests(unittest.TestCase):
    """Capture/replay for the midpoint namespace."""

    def setUp(self):
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.evidence = build_evidence(
            self.bank, self.taxonomy,
            midpoint_rows(self.bank, wrong_positions=(0, 5)))
        self.input = build_midpoint_input(self.evidence)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _cached(self, opener, budget=None):
        native = MidpointAittaGenerator(
            AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                           opener=opener))
        cache = CaptureCache(Path(self.tmp.name), budget)
        return CachedMidpointGenerator(native, cache, MODEL, "hash")

    def test_capture_reuse_single_call(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=AUTHORED))
        cached = self._cached(opener)
        first = cached.generate(copy.deepcopy(self.input))
        second = cached.generate(copy.deepcopy(self.input))
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(first, second)
        self.assertTrue(cached.execution["reused"])
        self.assertEqual(
            cached.execution["metadata"]["prompt_version"],
            PROMPT_VERSION)

    def test_failed_capture_not_retried(self):
        bad = dict(AUTHORED, review_focus="<script>x</script>")
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=bad))
        cached = self._cached(opener)
        first = copy.deepcopy(self.input)
        with self.assertRaises(RuntimeError):
            cached.generate(first)
        second = copy.deepcopy(self.input)
        with self.assertRaises(RuntimeError):
            cached.generate(second)
        self.assertEqual(len(opener.calls), 1)
        self.assertIsNotNone(cached.last_rejected_draft)

    def test_budget_exhaustion_and_thread_local_execution(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=AUTHORED))
        cached = self._cached(opener, budget=0)
        payload = copy.deepcopy(self.input)
        with self.assertRaises(SystemExit):
            cached.generate(payload)
        seen = []
        worker = threading.Thread(
            target=lambda: seen.append(cached.execution))
        worker.start()
        worker.join(timeout=10)
        # No cross-thread fallback: another thread sees None.
        self.assertEqual(seen, [None])


class MidpointServiceTests(unittest.TestCase):
    """DemoService phrasing job lifecycle (all offline fakes)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.rows20 = midpoint_rows(self.bank, wrong_positions=(0, 5))
        self.evidence = build_evidence(
            self.bank, self.taxonomy, self.rows20)

    def _midpoint(self, opener, budget=None):
        native = MidpointAittaGenerator(
            AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                           opener=opener))
        if budget is None:
            return native
        return CachedMidpointGenerator(
            native, CaptureCache(Path(self.tmp.name) / "caps", budget),
            MODEL, "hash")

    def _service_with(self, adapter, **kwargs):
        service = DemoService(
            root=Path(self.tmp.name) / f"svc_{secrets.token_hex(4)}",
            bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            provider_mode="hosted", generator=_EndStub(),
            midpoint_generator=adapter, **kwargs)
        self.addCleanup(service.close, wait=True)
        return service

    def _service(self, opener=None, budget=None, **kwargs):
        midpoint = (self._midpoint(opener or RecordingOpener(
                        wire_reply(candidate_id=CANDIDATE_ID,
                                   sections=AUTHORED)),
                        budget)
                    if kwargs.pop("midpoint", True) else None)
        return self._service_with(midpoint, **kwargs)

    def _submit_midpoint(self, service):
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in self.rows20:
            service.submit_response(sid, token, row)
        return sid, token

    def test_draft_ready_becomes_student_wording(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID,
                       sections=dict(AUTHORED,
                                     assessment_summary=SENTINEL)))
        service = self._service(opener)
        sid, token = self._submit_midpoint(service)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["midpoint"]
            .get("phrasing_job", {}).get("status") == "ready"))
        block = service._load_meta(sid)["checkpoints"]["midpoint"]
        draft = block["provider_draft"]
        self.assertEqual(block["provider_draft_version"],
                         MESSAGE_VERSION)
        self.assertEqual(block["phrasing_job"]["prompt_version"],
                         PROMPT_VERSION)
        self.assertEqual(
            draft["sections"][0]["text"], SENTINEL)
        base = compose_midpoint_message(self.evidence)
        self.assertEqual(draft["total"], base["total"])
        self.assertEqual(draft["skills"], base["skills"])
        # The immutable deterministic baseline is retained untouched.
        self.assertEqual(block["student_message"]["sections"],
                         base["sections"])
        self.assertEqual(block["student_message_version"],
                         STUDENT_MESSAGE_VERSION)
        # The student snapshot now shows the Aitta wording, projected
        # to the same section/text/total/skills shape only.
        snap = service.snapshot(sid, token)
        student_mid = snap["feedback"]["midpoint"]
        self.assertEqual(student_mid["sections"], draft["sections"])
        self.assertEqual(student_mid["total"], draft["total"])
        self.assertIn(SENTINEL, json.dumps(snap))
        self.assertNotIn("provider_draft", json.dumps(snap))
        self.assertNotIn("phrasing_job", json.dumps(snap))
        self.assertNotIn("rejected_draft", json.dumps(snap))
        # One call only.
        self.assertEqual(len(opener.calls), 1)
        wire = json.loads(opener.calls[0]["body"]
                          ["messages"][1]["content"])
        self.assertEqual(wire["total"]["out_of"], 20)
        # Teacher detail exposes the draft and job record.
        detail = service.teacher_session(sid)
        teacher_mid = detail["checkpoints"]["midpoint"]
        self.assertEqual(teacher_mid["provider_draft"], draft)
        self.assertIn("execution",
                      teacher_mid["phrasing_job"])

    def test_historical_ready_session_shows_aitta_without_calls(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID,
                       sections=dict(AUTHORED,
                                     assessment_summary=SENTINEL)))
        service = self._service(opener)
        sid, token = self._submit_midpoint(service)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["midpoint"]
            .get("phrasing_job", {}).get("status") == "ready"))
        self.assertEqual(len(opener.calls), 1)
        # A restarted service selects the stored ready draft at read
        # time: no provider call, no migration.
        fresh_opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=AUTHORED))
        restarted = DemoService(
            root=Path(service.root), bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            provider_mode="hosted", generator=_EndStub(),
            midpoint_generator=self._midpoint(fresh_opener))
        self.addCleanup(restarted.close, wait=True)
        snap = restarted.snapshot(sid, token)
        self.assertIn(SENTINEL, json.dumps(
            snap["feedback"]["midpoint"]))
        self.assertEqual(fresh_opener.calls, [])

    def test_ready_without_draft_shows_deterministic(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID,
                       sections=dict(AUTHORED,
                                     assessment_summary=SENTINEL)))
        service = self._service(opener)
        sid, token = self._submit_midpoint(service)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["midpoint"]
            .get("phrasing_job", {}).get("status") == "ready"))
        # A ready job without a stored draft keeps deterministic wording.
        with service.lock:
            meta = service._load_meta(sid)
            meta["checkpoints"]["midpoint"]["provider_draft"] = None
            service._save_meta(meta)
        base = compose_midpoint_message(self.evidence)
        snap = service.snapshot(sid, token)
        self.assertEqual(snap["feedback"]["midpoint"]["sections"],
                         base["sections"])
        self.assertNotIn(SENTINEL, json.dumps(snap))

    def test_wire_sent_before_second_half(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=AUTHORED))
        service = self._service(opener)
        sid, token = self._submit_midpoint(service)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["midpoint"]
            .get("phrasing_job", {}).get("status") == "ready"))
        # Finish the assessment; the stored draft still reflects
        # first-20 aggregates only.
        for row in responses(self.bank, count=40)[20:]:
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        draft = service._load_meta(sid)["checkpoints"]["midpoint"][
            "provider_draft"]
        self.assertEqual(draft["total"]["out_of"], 20)
        self.assertEqual(len(opener.calls), 1)

    def test_rules_session_never_calls(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=AUTHORED))
        service = self._service(opener)
        created = service.create_session(provider_mode="rules")
        sid, token = created["session_id"], created["student_token"]
        for row in self.rows20:
            service.submit_response(sid, token, row)
        block = service._load_meta(sid)["checkpoints"]["midpoint"]
        self.assertEqual(block["phrasing_job"]["status"],
                         "not_requested")
        self.assertIsNone(block["provider_draft"])
        self.assertEqual(opener.calls, [])

    def test_no_midpoint_adapter_skips(self):
        service = self._service(midpoint=False)
        sid, token = self._submit_midpoint(service)
        block = service._load_meta(sid)["checkpoints"]["midpoint"]
        self.assertEqual(block["phrasing_job"]["status"],
                         "not_requested")
        self.assertIsNone(block["provider_draft"])

    def test_provider_error_fallback_student_unchanged(self):
        opener = RecordingOpener(error=RuntimeError("boom"))
        service = self._service(opener)
        sid, token = self._submit_midpoint(service)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["midpoint"]
            .get("phrasing_job", {}).get("status") == "fallback"))
        block = service._load_meta(sid)["checkpoints"]["midpoint"]
        self.assertEqual(block["phrasing_job"]["reason"],
                         "provider_error")
        self.assertIsNone(block["provider_draft"])
        base = compose_midpoint_message(self.evidence)
        snap = service.snapshot(sid, token)
        self.assertEqual(snap["feedback"]["midpoint"]["sections"],
                         base["sections"])
        self.assertNotIn("boom", json.dumps(snap))
        self.assertNotIn("boom", json.dumps(block["phrasing_job"]))

    def test_budget_exhaustion_fallback(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=AUTHORED))
        service = self._service(opener, budget=0)
        sid, token = self._submit_midpoint(service)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["midpoint"]
            .get("phrasing_job", {}).get("status") == "fallback"))
        block = service._load_meta(sid)["checkpoints"]["midpoint"]
        self.assertEqual(block["phrasing_job"]["reason"],
                         "demo_call_budget_exhausted")
        self.assertIsNone(block["provider_draft"])
        self.assertEqual(service._pending_provider, 0)

    def test_queue_capacity_no_negative_counts(self):
        service = self._service(max_pending_jobs=0)
        sid, token = self._submit_midpoint(service)
        block = service._load_meta(sid)["checkpoints"]["midpoint"]
        self.assertEqual(block["phrasing_job"]["status"], "unavailable")
        self.assertEqual(block["phrasing_job"]["reason"],
                         "demo_provider_queue_capacity")
        self.assertEqual(service._pending_provider, 0)

    def test_rejected_reply_draft_recorded_teacher_only(self):
        bad = dict(AUTHORED, next_steps="<script>alert(1)</script>")
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=bad))
        service = self._service(opener)
        sid, token = self._submit_midpoint(service)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["midpoint"]
            .get("phrasing_job", {}).get("status") == "fallback"))
        job = service._load_meta(sid)["checkpoints"]["midpoint"][
            "phrasing_job"]
        self.assertIn("<script>",
                      job["execution"]["rejected_draft"]["content"])
        snap = service.snapshot(sid, token)
        self.assertNotIn("<script>", json.dumps(snap))

    def test_submission_not_blocked_by_slow_call(self):
        gate = threading.Event()

        class GatedOpener(RecordingOpener):
            def open(self, request, timeout=None):
                gate.wait(timeout=10)
                return super().open(request, timeout)

        opener = GatedOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=AUTHORED))
        service = self._service(opener)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in self.rows20:
            snap = service.submit_response(sid, token, row)
        # The 20th answer returns the deterministic check-in while the
        # provider call is still gated.
        self.assertEqual(snap["answered_count"], 20)
        base = compose_midpoint_message(self.evidence)
        self.assertEqual(snap["feedback"]["midpoint"]["sections"],
                         base["sections"])
        gate.set()
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["midpoint"]
            .get("phrasing_job", {}).get("status") == "ready"))

    def test_restart_marks_pending_fallback(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=AUTHORED))
        service = self._service(opener)
        sid, _ = self._submit_midpoint(service)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["midpoint"]
            .get("phrasing_job", {}).get("status") == "ready"))
        # Simulate an interrupted pending job on disk.
        meta = service._load_meta(sid)
        block = meta["checkpoints"]["midpoint"]
        block["phrasing_job"] = {"status": "pending"}
        service._save_meta(meta)
        restarted = DemoService(
            root=Path(service.root), bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            provider_mode="hosted", generator=_EndStub(),
            midpoint_generator=self._midpoint(opener))
        self.addCleanup(restarted.close, wait=True)
        block = restarted._load_meta(sid)["checkpoints"]["midpoint"]
        self.assertEqual(block["phrasing_job"]["status"], "fallback")
        self.assertEqual(block["phrasing_job"]["reason"],
                         "interrupted_by_restart")
        self.assertIsNotNone(block["student_message"])

    def test_failed_capture_reused_until_explicit_retry(self):
        cache_dir = Path(self.tmp.name) / "retry_caps"
        cache_dir.mkdir()
        cache = CaptureCache(cache_dir, 10)
        bad = dict(AUTHORED, review_focus="<b>bad</b>")
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=bad))
        native = MidpointAittaGenerator(
            AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                           opener=opener))
        service = self._service_with(
            CachedMidpointGenerator(native, cache, MODEL, "hash"))
        sid, _ = self._submit_midpoint(service)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["midpoint"]
            .get("phrasing_job", {}).get("status") == "fallback"))
        captures = list(cache_dir.glob("aitta_midpoint_*.json"))
        self.assertEqual(len(captures), 1)
        self.assertFalse(json.loads(
            captures[0].read_text(encoding="utf-8"))["ok"])
        # A second session with identical first-20 aggregates reuses the
        # failed capture: no implicit retry, no new call.
        created = service.create_session(provider_mode="rules")
        sid2, token2 = (created["session_id"], created["student_token"])
        for row in self.rows20:
            service.submit_response(sid2, token2, row)
        service._run_midpoint_phrasing(
            sid2, copy.deepcopy(self.evidence), retry_failed=False)
        self.assertEqual(len(opener.calls), 1)
        job2 = service._load_meta(sid2)["checkpoints"]["midpoint"][
            "phrasing_job"]
        self.assertEqual(job2["status"], "fallback")
        self.assertTrue(job2["execution"]["reused"])
        # The explicit replay retry flag allows exactly one fresh call;
        # the original capture file is preserved.
        opener.payload = wire_reply(candidate_id=CANDIDATE_ID,
                                    sections=AUTHORED)
        service._run_midpoint_phrasing(
            sid2, copy.deepcopy(self.evidence), retry_failed=True)
        self.assertEqual(len(opener.calls), 2)
        job3 = service._load_meta(sid2)["checkpoints"]["midpoint"][
            "phrasing_job"]
        self.assertEqual(job3["status"], "ready")
        self.assertEqual(job3["execution"]["attempt_number"], 2)
        self.assertEqual(job3["execution"]["previous_capture_file"],
                         captures[0].name)
        self.assertTrue(captures[0].exists())
        draft = service._load_meta(sid2)["checkpoints"]["midpoint"][
            "provider_draft"]
        self.assertEqual(draft["sections"][0]["text"],
                         AUTHORED["assessment_summary"])

    def test_default_hosted_factory_builds_both_adapters(self):
        mid_opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=AUTHORED))
        inner = AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                               opener=mid_opener)
        with mock.patch.object(
                AittaGenerator, "from_env", return_value=inner) as env:
            service = DemoService(
                root=Path(self.tmp.name) / f"svc_{secrets.token_hex(4)}",
                bank=self.bank, taxonomy=self.taxonomy,
                diagnostics=fake_diagnostics(self.bank),
                provider_mode="hosted")
        self.addCleanup(service.close, wait=True)
        env.assert_called_once_with()
        midpoint = service.midpoint_generator
        end = service.generator
        self.assertIsInstance(midpoint, CachedMidpointGenerator)
        self.assertIsInstance(midpoint.native, MidpointAittaGenerator)
        # One credentials read, two independent wrappers/observers.
        self.assertIsNot(midpoint.native._generator,
                         end.native._generator)
        self.assertIsNot(midpoint.native._observer,
                         end.native._observer)
        self.assertIs(midpoint.cache, service.cache)
        self.assertIs(end.cache, service.cache)
        sid, token = self._submit_midpoint(service)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["midpoint"]
            .get("phrasing_job", {}).get("status") == "ready"))
        self.assertEqual(len(mid_opener.calls), 1)
        block = service._load_meta(sid)["checkpoints"]["midpoint"]
        self.assertEqual(block["provider_draft"]["sections"][0]["text"],
                         AUTHORED["assessment_summary"])
        base = compose_midpoint_message(self.evidence)
        # Ready now selects the Aitta draft for the student snapshot.
        self.assertEqual(service.snapshot(sid, token)
                         ["feedback"]["midpoint"]["sections"],
                         block["provider_draft"]["sections"])
        self.assertEqual(block["student_message"]["sections"],
                         base["sections"])

    def test_frozen_first20_wire_while_gated_until_40(self):
        gate = threading.Event()

        class GatedOpener(RecordingOpener):
            def open(self, request, timeout=None):
                gate.wait(timeout=30)
                return super().open(request, timeout)

        opener = GatedOpener(
            wire_reply(candidate_id=CANDIDATE_ID,
                       sections=dict(AUTHORED,
                                     assessment_summary=SENTINEL)))
        service = self._service(opener)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in self.rows20:
            snap = service.submit_response(sid, token, row)
        # Answer 20 returns the deterministic check-in while the
        # provider call is still gated.
        self.assertEqual(snap["answered_count"], 20)
        base = compose_midpoint_message(self.evidence)
        self.assertEqual(snap["feedback"]["midpoint"]["sections"],
                         base["sections"])
        # Finish the assessment while the midpoint call is still gated;
        # the frozen first-20 evidence is what reaches the wire.
        for row in responses(self.bank, count=40)[20:]:
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        end_job = copy.deepcopy(
            service._load_meta(sid)["provider_job"])
        gate.set()
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["checkpoints"]["midpoint"]
            .get("phrasing_job", {}).get("status") == "ready"))
        wire = json.loads(opener.calls[0]["body"]
                          ["messages"][1]["content"])
        self.assertEqual(wire["answered_count"], 20)
        self.assertEqual(wire["total"],
                         {"correct": 18, "incorrect": 2, "out_of": 20})
        self.assertEqual(len(opener.calls), 1)
        block = service._load_meta(sid)["checkpoints"]["midpoint"]
        self.assertEqual(block["provider_draft"]["total"],
                         {"correct": 18, "out_of": 20})
        # The late midpoint publication leaves the end job and the saved
        # student message untouched.
        self.assertEqual(service._load_meta(sid)["provider_job"],
                         end_job)
        self.assertEqual(block["student_message"]["sections"],
                         base["sections"])
        self.assertEqual(block["student_message_version"],
                         STUDENT_MESSAGE_VERSION)
        # Release the deterministic end fallback; the late midpoint
        # draft is now the student-facing midpoint wording while end
        # data and release gating are unchanged.
        detail = service.teacher_session(sid)
        digest = detail["feedback_delivery"]["preview_sha256"]
        self.assertIsNotNone(digest)
        service.release_feedback(
            sid, {"message_sha256": digest, "reviewer_label": "t"})
        snap = service.snapshot(sid, token)
        self.assertEqual(snap["feedback"]["midpoint"]["sections"],
                         block["provider_draft"]["sections"])
        self.assertNotIn("provider_draft", json.dumps(snap))
        self.assertNotIn("phrasing_job", json.dumps(snap))
        self.assertNotIn("rejected_draft", json.dumps(snap))
        self.assertNotIn(SENTINEL, json.dumps(
            snap["feedback"].get("end")))


class MidpointApiTests(unittest.TestCase):
    """HTTP boundary: generation details require teacher unlock; the
    student route shows the ready draft wording only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        opener = RecordingOpener(
            wire_reply(candidate_id=CANDIDATE_ID,
                       sections=dict(AUTHORED,
                                     assessment_summary=SENTINEL)))
        self.service = DemoService(
            root=Path(self.tmp.name) / "svc",
            bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            provider_mode="hosted", generator=_EndStub(),
            midpoint_generator=MidpointAittaGenerator(
                AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                               opener=opener)))
        self.addCleanup(self.service.close, wait=True)
        self.server = make_server(self.service, PIN)
        thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _request(self, method, path, body=None, cookie=None,
                 token=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.url + path, data=data, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if cookie:
            request.add_header("Cookie", cookie)
        if token:
            request.add_header("X-Demo-Session-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read()), \
                    dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), dict(exc.headers)

    def test_teacher_detail_includes_draft_unauthorized_forbidden(self):
        status, created, _ = self._request(
            "POST", "/api/sessions", {"synthetic": True})
        self.assertEqual(status, 201)
        sid, token = created["session_id"], created["student_token"]
        for row in midpoint_rows(self.bank):
            status, snap, _ = self._request(
                "POST", f"/api/sessions/{sid}/responses",
                {"question_token": self._token_for(sid, token),
                 "selected_index": row["selected_index"]}, token=token)
            self.assertEqual(status, 200)
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["checkpoints"]
            ["midpoint"].get("phrasing_job", {}).get("status")
            == "ready"))
        status, _, _ = self._request("GET", f"/api/teacher/sessions/{sid}")
        self.assertEqual(status, 403)
        status, _, meta = self._request(
            "POST", "/api/teacher/unlock", {"pin": PIN})
        self.assertEqual(status, 200)
        cookie = meta.get("Set-Cookie", "").split(";")[0]
        status, detail, _ = self._request(
            "GET", f"/api/teacher/sessions/{sid}", cookie=cookie)
        self.assertEqual(status, 200)
        mid = detail["checkpoints"]["midpoint"]
        self.assertEqual(mid["provider_draft"]["sections"][0]["text"],
                         SENTINEL)
        self.assertIn("execution", mid["phrasing_job"])
        # The student route shows the ready Aitta wording but never the
        # job internals, execution record, or rejected wording.
        status, snap, _ = self._request(
            "GET", f"/api/sessions/{sid}", token=token)
        self.assertEqual(status, 200)
        self.assertIn(SENTINEL, json.dumps(
            snap["feedback"]["midpoint"]))
        self.assertNotIn("provider_draft", json.dumps(snap))
        self.assertNotIn("phrasing_job", json.dumps(snap))
        self.assertNotIn("rejected_draft", json.dumps(snap))

    def _token_for(self, sid, token):
        snap = self.service.snapshot(sid, token)
        return snap["current_question"]["question_token"]


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1",
                     "opt-in installed Edge + websocket-client browser test")
class MidpointBrowserTests(unittest.TestCase):
    """Focused render: the ready Aitta draft becomes the student-facing
    midpoint wording; the teacher card shows one main message with a
    collapsed deterministic comparison and generation details."""

    def setUp(self):
        from tests.test_research_browser import Browser
        edge = Path(os.environ.get(
            "EDGE_BINARY",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"))
        if not edge.is_file():
            self.skipTest("Microsoft Edge is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.gate = threading.Event()

        class GatedOpener(RecordingOpener):
            def open(inner, request, timeout=None):
                self.gate.wait(timeout=30)
                return super().open(request, timeout)

        self.opener = GatedOpener(
            wire_reply(candidate_id=CANDIDATE_ID, sections=AUTHORED))
        self.service = DemoService(
            root=self.root / "data", bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            provider_mode="hosted", generator=_EndStub(),
            midpoint_generator=MidpointAittaGenerator(
                AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                               opener=self.opener)))
        self.addCleanup(self.service.close, wait=True)
        self.pin = f"{secrets.randbelow(1000000):06d}"
        self.server = make_server(self.service, self.pin)
        thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        profile = self.root / "edge"
        self.process = subprocess.Popen([
            str(edge), "--remote-debugging-port=0",
            f"--user-data-dir={profile}", "--headless=new",
            "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--window-size=1440,1100",
            "about:blank"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_browser)
        port_file = profile / "DevToolsActivePort"
        for _ in range(100):
            if port_file.exists():
                break
            time.sleep(.1)
        port = port_file.read_text().splitlines()[0]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json", timeout=10) as response:
            pages = json.load(response)
        self.browser = Browser(next(
            p["webSocketDebuggerUrl"] for p in pages
            if p["type"] == "page"))
        self.browser.viewport(1440, 1100)

    def stop_browser(self):
        if hasattr(self, "browser"):
            try:
                self.browser.command("Browser.close")
            except Exception:
                pass
            self.browser.socket.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)

    def test_teacher_midpoint_draft_disclosure(self):
        created = self.service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in midpoint_rows(self.bank, wrong_positions=(0, 5)):
            self.service.submit_response(sid, token, row)
        evidence = build_evidence(
            self.bank, self.taxonomy,
            midpoint_rows(self.bank, wrong_positions=(0, 5)))
        base = compose_midpoint_message(evidence)
        deterministic_text = base["sections"][0]["text"]
        b = self.browser
        # While the provider call is still gated the pause screen shows
        # the deterministic check-in.
        b.command("Page.navigate",
                  {"url": f"{self.url}/?session={sid}#token={token}"})
        b.wait("!document.querySelector('#pause-panel').hidden")
        student_text = b.js(
            "document.querySelector('#pause-panel').textContent")
        self.assertIn(deterministic_text, student_text)
        for expected in AUTHORED.values():
            self.assertNotIn(expected, student_text)
        # Let the provider call through: the paused page's existing poll
        # refreshes the check-in to the Aitta wording without a reload
        # or resuming, and Continue stays available.
        self.gate.set()
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["checkpoints"]
            ["midpoint"].get("phrasing_job", {}).get("status")
            == "ready"))
        b.wait("document.querySelector('#pause-panel').textContent"
               f".includes({json.dumps(AUTHORED['assessment_summary'])})")
        student_text = b.js(
            "document.querySelector('#pause-panel').textContent")
        self.assertNotIn(deterministic_text, student_text)
        self.assertFalse(b.js(
            "document.querySelector('#continue-button') === null"))
        # Focus must stay on Continue across further poll cycles; the
        # refresh must not steal it back to the pause heading.
        b.js("document.querySelector('#continue-button').focus();"
             "window.__focusT0 = Date.now()")
        self.assertEqual(b.js("document.activeElement.id"),
                         "continue-button")
        b.wait("Date.now() - window.__focusT0 > 3000")
        self.assertEqual(b.js("document.activeElement.id"),
                         "continue-button")
        # localStorage carries only the session credentials; no job
        # internals, execution record, or rejected wording.
        stored = b.js("JSON.stringify(localStorage)")
        self.assertNotIn("provider_draft", stored)
        self.assertNotIn("phrasing_job", stored)
        self.assertNotIn("rejected_draft", stored)
        self.assertNotIn("execution", stored)
        # Seed the persisted deterministic baseline with embedded Finnish
        # topic labels so the collapsed comparison exercises the same
        # translation path as the main message.
        seeded = {
            "strengths": ("Vahvuutena n\xe4kyy Jaollisuus, "
                          "tekij\xe4t, alkuluvut ja "
                          "Murtolukujen kerto- ja jakolasku."),
            "focus": ("Harjoittele Prosenttilaskuja sek\xe4 "
                      "Samanmuotoisten termien "
                      "yhdist\xe4minen.")}
        with self.service.lock:
            meta = self.service._load_meta(sid)
            for section in (meta["checkpoints"]["midpoint"]
                            ["student_message"]["sections"]):
                if section["kind"] in seeded:
                    section["text"] = seeded[section["kind"]]
            # Seed realistic sanitized execution metadata on the ready
            # phrasing job (the fixture adapter is not capture-wrapped).
            block = meta["checkpoints"]["midpoint"]
            block["phrasing_job"]["prompt_version"] = PROMPT_VERSION
            block["phrasing_job"].setdefault(
                "execution", {}).update({
                    "capture_file": "aitta_midpoint_test.json",
                    "reused": False,
                    "original_call_latency_ms": 8098.209,
                    "automatic_retry": {"attempts": 1,
                                        "max_attempts": 3,
                                        "stop_reason": "success"},
                    "metadata": {
                        "status": "completed",
                        "model_version": MODEL,
                        "prompt_version":
                            "aitta_midpoint_recorded_fixture_v0",
                        "usage": {"total_tokens": 512}}})
            self.service._save_meta(meta)
        finnish_labels = ["Jaollisuus, tekij\xe4t, alkuluvut",
                          "Murtolukujen kerto- ja jakolasku",
                          "Prosenttilaskuja",
                          "Samanmuotoisten termien yhdist\xe4minen"]
        english_names = ["Divisibility, factors and primes",
                         "Fraction multiplication and division",
                         "Percentages", "Combining like terms"]
        # The shared translator maps the seeded sentences to English.
        for seeded_fi, names in (
                (seeded["strengths"], english_names[:2]),
                (seeded["focus"], english_names[2:])):
            translated = b.js(
                f"englishMidpointText({json.dumps(seeded_fi)})")
            for name in names:
                self.assertIn(name, translated)
            for label in finnish_labels:
                self.assertNotIn(label, translated)
        # Teacher view: the authored wording is the single main message.
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)}; "
             "document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('.session-row')")
        b.js("document.querySelector('.session-row').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('[data-tab=feedback]').click()")
        b.wait("document.querySelector('#tab-feedback "
               ".midpoint-feedback-card')")
        text = b.js("document.querySelector('#tab-feedback').textContent")
        for expected in AUTHORED.values():
            self.assertIn(expected, text)
        self.assertIn("Aitta", text)
        self.assertIn("Your first 20 answers", text)
        self.assertIn("Midpoint generation details", text)
        # The provider prose is not duplicated inside the generation
        # details, and the deterministic baseline sits behind its own
        # collapsed comparison disclosure.
        gen = b.js("document.querySelector('.midpoint-provider-draft')"
                   ".textContent")
        for expected in AUTHORED.values():
            self.assertNotIn(expected, gen)
        self.assertIn("not fact-verified", gen)
        # Generation details show human-readable rows only.
        for needle in ("Generation status", "Ready",
                       "New provider call", MODEL,
                       "8.10 seconds", "1 of 3", "512"):
            self.assertIn(needle, gen)
        for absent in ("Draft version",
                       "Execution record", "aitta_midpoint_test.json"):
            self.assertNotIn(absent, gen)
        # Recorded versions sit directly in the generation details;
        # the deterministic reference version stays inside its own
        # comparison dropdown instead.
        self.assertIn("Template version", gen)
        self.assertIn(MESSAGE_VERSION, gen)
        self.assertIn("Prompt version", gen)
        # The recorded execution prompt wins over the job field.
        self.assertIn("aitta_midpoint_recorded_fixture_v0", gen)
        self.assertNotIn(PROMPT_VERSION, gen)
        self.assertNotIn("Deterministic reference version", gen)
        self.assertFalse(b.js(
            "document.querySelector('.midpoint-version-details')"))
        self.assertFalse(b.js(
            "document.querySelector("
            "'.midpoint-deterministic-comparison') === null"))
        self.assertFalse(b.js(
            "document.querySelector("
            "'.midpoint-deterministic-comparison').open"))
        b.js("document.querySelector("
             "'.midpoint-deterministic-comparison').open = true")
        comparison = b.js(
            "document.querySelector("
            "'.midpoint-deterministic-comparison').textContent")
        self.assertIn("Deterministic template version", comparison)
        self.assertIn(STUDENT_MESSAGE_VERSION, comparison)
        self.assertIn(deterministic_text, comparison)
        self.assertIn("Deterministic midpoint - comparison", comparison)
        for name in english_names:
            self.assertIn(name, comparison)
        for label in finnish_labels:
            self.assertNotIn(label, comparison)
        if os.environ.get("BROWSER_SCREENSHOTS"):
            b.js("document.querySelector('.midpoint-feedback-card')"
                 ".scrollIntoView({block:'center'})")
            b.screenshot("midpoint_draft_desktop.png")
        # The card fits a 390px viewport without horizontal overflow.
        b.viewport(390, 844)
        b.js("document.querySelector('.midpoint-feedback-card')"
             ".scrollIntoView({block:'center'})")
        overflow = b.js(
            "document.documentElement.scrollWidth - "
            "document.documentElement.clientWidth")
        self.assertLessEqual(overflow, 0)
        if os.environ.get("BROWSER_SCREENSHOTS"):
            b.screenshot("midpoint_draft_mobile.png")
        b.viewport(1440, 1100)
        # Pending job wording: the deterministic main stays and the
        # comparison disclosure is absent.
        with self.service.lock:
            meta = self.service._load_meta(sid)
            block = meta["checkpoints"]["midpoint"]
            saved_draft = block.get("provider_draft")
            saved_draft_version = block.get("provider_draft_version")
            saved_job = dict(block.get("phrasing_job") or {})
            queued = saved_job.get("queued_at")
            block["provider_draft"] = None
            block["provider_draft_version"] = None
            block["phrasing_job"] = {"status": "pending",
                                     "queued_at": queued}
            self.service._save_meta(meta)
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.js("if (document.querySelector('#pin-input')) {"
             f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)};"
             "document.querySelector('#unlock-form').requestSubmit() }")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('.session-row')")
        b.js("document.querySelector('.session-row').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('[data-tab=feedback]').click()")
        b.wait("document.querySelector('.midpoint-provider-draft')")
        card_text = b.js(
            "document.querySelector('.midpoint-feedback-card')"
            ".textContent")
        self.assertIn(deterministic_text, card_text)
        self.assertIn("Deterministic fallback", card_text)
        self.assertIsNone(b.js(
            "document.querySelector("
            "'.midpoint-deterministic-comparison')"))
        b.js("document.querySelector('.midpoint-provider-draft')"
             ".open = true")
        pending_text = b.js(
            "document.querySelector('.midpoint-provider-draft')"
            ".textContent")
        self.assertIn("queued or in progress", pending_text)
        self.assertNotIn("No Aitta draft was produced", pending_text)
        # Pending jobs show only the recorded baseline template
        # version; no prompt version is invented without a saved record.
        self.assertIn("Template version", pending_text)
        self.assertIn(STUDENT_MESSAGE_VERSION, pending_text)
        self.assertNotIn("Prompt version", pending_text)
        # A reused ready capture reports 'Reused saved response' without
        # implying a fresh provider call.
        with self.service.lock:
            meta = self.service._load_meta(sid)
            block = meta["checkpoints"]["midpoint"]
            block["provider_draft"] = saved_draft
            block["provider_draft_version"] = saved_draft_version
            reused_job = dict(saved_job)
            reused_job["status"] = "ready"
            reused_job.setdefault("execution", {})["reused"] = True
            block["phrasing_job"] = reused_job
            self.service._save_meta(meta)
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.js("if (document.querySelector('#pin-input')) {"
             f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)};"
             "document.querySelector('#unlock-form').requestSubmit() }")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('.session-row')")
        b.js("document.querySelector('.session-row').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('[data-tab=feedback]').click()")
        b.wait("document.querySelector('#tab-feedback "
               ".midpoint-provider-draft')")
        b.wait("document.querySelector('.midpoint-provider-draft')"
               ".textContent.includes('Reused saved response')")
        # A recorded Aitta draft without a draft version shows no
        # Template version rather than borrowing the baseline's.
        with self.service.lock:
            meta = self.service._load_meta(sid)
            meta["checkpoints"]["midpoint"]["provider_draft_version"] \
                = None
            self.service._save_meta(meta)
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete'")
        b.js("if (document.querySelector('#pin-input')) {"
             f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)};"
             "document.querySelector('#unlock-form').requestSubmit() }")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('.session-row')")
        b.js("document.querySelector('.session-row').click()")
        b.js("document.querySelector('[data-tab=feedback]').click()")
        b.wait("document.querySelector('#tab-feedback "
               ".midpoint-provider-draft')")
        no_version = b.js(
            "document.querySelector('.midpoint-provider-draft')"
            ".textContent")
        self.assertNotIn("Template version", no_version)
        self.assertIn("aitta_midpoint_recorded_fixture_v0",
                      no_version)
        with self.service.lock:
            meta = self.service._load_meta(sid)
            meta["checkpoints"]["midpoint"]["provider_draft_version"] \
                = saved_draft_version
            self.service._save_meta(meta)
        # A transport failure_kind wins over a 'transport_failure'
        # rejection_code — it is not a validation rejection.
        with self.service.lock:
            meta = self.service._load_meta(sid)
            meta["checkpoints"]["midpoint"]["phrasing_job"] = {
                "status": "fallback", "reason": "provider_error",
                "execution": {"metadata": {
                    "rejection_code": "transport_failure",
                    "transport": {"stage": "body_read_failed",
                                  "failure_kind": "network_error",
                                  "http_status": None}}}}
            self.service._save_meta(meta)
        b.wait("document.querySelector('.midpoint-provider-draft')"
               ".textContent.includes('network error')", timeout=30)
        self.assertNotIn("Response rejected by validation",
                         b.js("document.querySelector("
                              "'.midpoint-provider-draft').textContent"))
        # Timed-out open failure explains itself to the teacher; a
        # retained rejected draft stays in its own collapsed details.
        rejected_markup = "<img src=x onerror=alert(1)>"
        with self.service.lock:
            meta = self.service._load_meta(sid)
            meta["checkpoints"]["midpoint"]["phrasing_job"] = {
                "status": "fallback", "reason": "provider_error",
                "execution": {
                    "reused": False,
                    "automatic_retry": {"attempts": 3,
                                        "max_attempts": 3,
                                        "stop_reason":
                                            "attempts_exhausted"},
                    "rejected_draft": {"content": rejected_markup},
                    "metadata": {"transport": {
                        "stage": "open_failed", "failure_kind":
                            "timeout", "http_status": None}}}}
            self.service._save_meta(meta)
        # Re-selecting the same row is a no-op, so reload the page to
        # refetch the detail payload.
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.js("if (document.querySelector('#pin-input')) {"
             f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)};"
             "document.querySelector('#unlock-form').requestSubmit() }")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('.session-row')")
        b.js("document.querySelector('.session-row').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('[data-tab=feedback]').click()")
        b.wait("document.querySelector('#tab-feedback "
               ".midpoint-provider-draft')")
        b.js("document.querySelector('.midpoint-provider-draft')"
             ".open = true")
        fail_text = b.js(
            "document.querySelector('.midpoint-provider-draft')"
            ".textContent")
        self.assertIn("timed out", fail_text)
        self.assertIn("3 of 3", fail_text)
        self.assertIn("Attempts exhausted", fail_text)
        self.assertNotIn("queued or in progress", fail_text)
        # Rejected wording sits in its own collapsed details as plain
        # text — no markup is interpreted.
        rej = ("document.querySelector('#tab-feedback "
               "details[data-disclosure-key=midpoint-rejected-wording]')")
        self.assertTrue(b.js(f"Boolean({rej})"))
        self.assertFalse(b.js(f"{rej}.open"))
        self.assertIn("Rejected provider wording (unvalidated)",
                      b.js(f"{rej}.textContent"))
        self.assertEqual(b.js(
            f"{rej}.querySelector('pre').textContent"),
            rejected_markup)
        self.assertFalse(b.js(
            f"Boolean({rej}.querySelector('img'))"))
        # Rejected wording and job internals never reach the student
        # snapshot payload.
        snap = json.dumps(self.service.snapshot(sid, token))
        self.assertNotIn("rejected_draft", snap)
        self.assertNotIn(rejected_markup, snap)
        self.assertEqual(b.errors, [])


if __name__ == "__main__":
    unittest.main()
