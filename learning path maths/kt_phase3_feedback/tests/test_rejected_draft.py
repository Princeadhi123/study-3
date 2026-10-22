"""Teacher-only retention of rejected Aitta draft wording.

Rejected drafts are kept out of last_metadata, student snapshots, and
released feedback; they surface only inside provider execution records
for educator review. All fixtures are offline fakes - no network.
"""
import json
import os
import secrets
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import phase3_paths  # noqa: F401
from aitta_generator import AittaGenerationError, AittaGenerator
from demo_api import make_server
from demo_service import DemoService
from evidence_feedback import build_evidence
from evidence_feedback_policy import SCOPE, build_candidates
from feedback_practice import resolve_practice
from full_feedback import (
    CachedFullFeedbackGenerator, FullFeedbackAittaGenerator,
    _REJECTED_DRAFT_MAX_CHARS, build_full_input)
from replay_provider_scenarios import CaptureCache
from worked_first_context import PROMPT_VERSION as WF_PROMPT
from tests.browser_runtime import (
    cleanup_browser_directory, read_devtools_port)
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_assessment_worked_review import (
    grounded_rows, make_grounded_bank, make_grounded_taxonomy,
    positions)
from tests.test_demo_service import fake_diagnostics, wait_for
from tests.test_full_feedback import (
    API_KEY, BASE_URL, MODEL, RecordingOpener, VALID_SECTIONS,
    wire_reply)


class RejectedDraftGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.evidence = build_evidence(
            self.bank, self.taxonomy,
            responses(self.bank, correct=False, count=40))
        self.candidates = build_candidates(self.evidence, "end")
        self.selected = self.candidates[1]
        self.full_input = build_full_input(
            self.evidence, "student", self.selected)
        self.selected_id = self.selected["candidate_id"]

    def _generator(self, opener):
        inner = AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                               opener=opener)
        return FullFeedbackAittaGenerator(inner)

    def _reject(self, generator, payload=None):
        with self.assertRaises(AittaGenerationError):
            generator.generate(payload or self.full_input)

    def test_rejected_sections_draft_retained_verbatim(self):
        sections = dict(
            VALID_SECTIONS,
            assessment_summary="You completed <b>the</b> assessment.")
        content = json.dumps(
            {"candidate_id": self.selected_id, "sections": sections})
        opener = RecordingOpener(wire_reply(content=content))
        generator = self._generator(opener)
        self._reject(generator)
        self.assertEqual(generator.last_rejected_draft,
                         {"content": content, "truncated": False})
        self.assertEqual(
            generator.last_metadata["rejection_code"],
            "markup_in_assessment_summary")
        # Raw wording never leaks into the metadata contract.
        self.assertNotIn("<b>the</b>", json.dumps(
            generator.last_metadata))

    def test_malformed_json_and_truncated_content_retained(self):
        opener = RecordingOpener(wire_reply(content="{not json"))
        generator = self._generator(opener)
        self._reject(generator)
        self.assertEqual(generator.last_rejected_draft,
                         {"content": "{not json", "truncated": False})
        opener.payload = wire_reply(
            candidate_id=self.selected_id, finish_reason="length")
        self._reject(generator)
        draft = generator.last_rejected_draft
        self.assertTrue(draft["truncated"])
        self.assertIn("candidate_id", draft["content"])
        self.assertEqual(generator.last_metadata["rejection_code"],
                         "completion_truncated")

    def test_secrets_redacted_and_draft_capped(self):
        endpoint = AittaGenerator(
            API_KEY, BASE_URL, model=MODEL,
            opener=RecordingOpener())._endpoint
        content = f"leak {API_KEY} via {endpoint} " + "x" * 40000
        generator = self._generator(
            RecordingOpener(wire_reply(content=content)))
        self._reject(generator)
        draft = generator.last_rejected_draft
        self.assertEqual(len(draft["content"]),
                         _REJECTED_DRAFT_MAX_CHARS)
        self.assertTrue(draft["truncated"])
        self.assertNotIn(API_KEY, draft["content"])
        self.assertNotIn(endpoint, draft["content"])
        self.assertIn("[REDACTED]", draft["content"])
        self.assertNotIn(content[:80], json.dumps(
            generator.last_metadata))

    def test_no_stale_draft_after_success_or_transport_failure(self):
        opener = RecordingOpener(wire_reply(content="{not json"))
        generator = self._generator(opener)
        self._reject(generator)
        self.assertIsNotNone(generator.last_rejected_draft)
        opener.payload = wire_reply(candidate_id=self.selected_id)
        generator.generate(self.full_input)
        self.assertIsNone(generator.last_rejected_draft)
        opener.payload = wire_reply(content="{not json")
        self._reject(generator)
        self.assertIsNotNone(generator.last_rejected_draft)
        opener.payload = None
        opener.error = urllib.error.URLError("offline")
        self._reject(generator)
        self.assertIsNone(generator.last_rejected_draft)
        self.assertEqual(generator.last_metadata["rejection_code"],
                         "transport_failure")

    def test_input_validation_failure_resets_draft(self):
        opener = RecordingOpener(wire_reply(content="{not json"))
        generator = self._generator(opener)
        self._reject(generator)
        self.assertIsNotNone(generator.last_rejected_draft)
        with self.assertRaises(ValueError):
            generator.generate({"schema": "bogus"})
        self.assertIsNone(generator.last_rejected_draft)

    def test_thread_isolation_and_read_only_copy(self):
        opener = RecordingOpener(wire_reply(content="{not json"))
        generator = self._generator(opener)
        self._reject(generator)
        seen = []
        thread = threading.Thread(
            target=lambda: seen.append(generator.last_rejected_draft))
        thread.start()
        thread.join(timeout=10)
        self.assertEqual(seen, [None])
        draft = generator.last_rejected_draft
        draft["content"] = "mutated"
        draft["truncated"] = True
        self.assertEqual(generator.last_rejected_draft,
                         {"content": "{not json", "truncated": False})


class RejectedDraftCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = CaptureCache(Path(self.tmp.name), None)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.evidence = build_evidence(
            self.bank, self.taxonomy,
            responses(self.bank, correct=False, count=40))
        self.candidates = build_candidates(self.evidence, "end")
        self.selected = self.candidates[1]
        self.full_input = build_full_input(
            self.evidence, "student", self.selected)
        self.selected_id = self.selected["candidate_id"]

    def _adapter(self, opener):
        return FullFeedbackAittaGenerator(
            AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                           opener=opener))

    def test_failed_capture_stores_draft_and_reuses(self):
        sections = dict(
            VALID_SECTIONS,
            assessment_summary="You completed <b>the</b> assessment.")
        content = json.dumps(
            {"candidate_id": self.selected_id, "sections": sections})
        opener = RecordingOpener(wire_reply(content=content))
        adapter = self._adapter(opener)
        request = {"endpoint_sha256": "h", "body": {"k": 1}}
        reply, execution = self.cache.obtain(
            "aitta_full", request,
            lambda: adapter.generate(self.full_input), adapter)
        self.assertIsNone(reply)
        self.assertEqual(execution["rejected_draft"],
                         {"content": content, "truncated": False})
        records = list(Path(self.tmp.name).glob("aitta_full_*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertIsNone(record["reply"])
        self.assertEqual(record["rejected_draft"]["content"], content)
        # Reuse: no second call, draft still surfaced, retry stays opt-in.
        reply, execution = self.cache.obtain(
            "aitta_full", request,
            lambda: adapter.generate(self.full_input), adapter)
        self.assertIsNone(reply)
        self.assertTrue(execution["reused"])
        self.assertEqual(execution["rejected_draft"]["content"], content)
        self.assertEqual(len(opener.calls), 1)

    def test_legacy_failed_record_without_draft_reused(self):
        request = {"endpoint_sha256": "h", "body": {"k": 2}}
        base = self.cache.path("aitta_full", request)
        record = {"provider": "aitta_full", "request": request,
                  "ok": False, "reply": None,
                  "metadata": {"status": "failed"},
                  "original_call_latency_ms": 1.0,
                  "origin": "hosted_replay"}
        base.write_text(json.dumps(record), encoding="utf-8")
        opener = RecordingOpener(wire_reply(content="x"))
        adapter = self._adapter(opener)
        reply, execution = self.cache.obtain(
            "aitta_full", request,
            lambda: adapter.generate(self.full_input), adapter)
        self.assertIsNone(reply)
        self.assertTrue(execution["reused"])
        self.assertNotIn("rejected_draft", execution)
        self.assertEqual(len(opener.calls), 0)

    def test_cached_generator_exposes_draft_without_stale_state(self):
        opener = RecordingOpener(wire_reply(content="{not json"))
        native = self._adapter(opener)
        generator = CachedFullFeedbackGenerator(
            native, self.cache, MODEL, "hash")
        with self.assertRaises(RuntimeError):
            generator.generate(self.full_input)
        self.assertEqual(generator.last_rejected_draft,
                         {"content": "{not json", "truncated": False})
        # Another thread must not see this thread's failed execution.
        seen = []
        thread = threading.Thread(
            target=lambda: seen.append(generator.last_rejected_draft))
        thread.start()
        thread.join(timeout=10)
        self.assertEqual(seen, [None])
        other = build_full_input(
            self.evidence, "student", self.candidates[0])
        opener.payload = wire_reply(
            candidate_id=self.candidates[0]["candidate_id"])
        generator.generate(other)
        self.assertIsNone(generator.last_rejected_draft)
        self.assertNotIn("rejected_draft", generator.execution)

    def test_cached_generator_resets_draft_before_any_call(self):
        opener = RecordingOpener(wire_reply(content="{not json"))
        native = self._adapter(opener)
        cache_root = Path(self.tmp.name) / "b"
        cache_root.mkdir()
        cache = CaptureCache(cache_root, 1)
        generator = CachedFullFeedbackGenerator(
            native, cache, MODEL, "hash")
        with self.assertRaises(RuntimeError):
            generator.generate(self.full_input)
        self.assertIsNotNone(generator.last_rejected_draft)
        # Invalid input fails before cache.obtain; no stale draft.
        with self.assertRaises(ValueError):
            generator.generate({"schema": "bogus"})
        self.assertIsNone(generator.last_rejected_draft)
        # Reproduce the failure, then exhaust the budget on a new
        # request: SystemExit propagates with no stale draft.
        with self.assertRaises(RuntimeError):
            generator.generate(self.full_input)
        self.assertIsNotNone(generator.last_rejected_draft)
        other = build_full_input(
            self.evidence, "student", self.candidates[0])
        with self.assertRaises(SystemExit):
            generator.generate(other)
        self.assertIsNone(generator.last_rejected_draft)


class RejectedDraftServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)

    def _rejected_session(self):
        self.opener = RecordingOpener(
            wire_reply(content="{not valid <script>alert(1)</script>"))
        generator = FullFeedbackAittaGenerator(
            AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                           opener=self.opener))
        self.service = DemoService(
            root=Path(self.tmp.name), bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            review_mode="legacy_single", provider_mode="hosted",
            generator=generator)
        self.addCleanup(self.service.close, wait=True)
        created = self.service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            self.service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]
            ["status"] in ("ready", "fallback")),
            "provider job did not finish")
        return sid, token

    def test_teacher_detail_has_draft_student_surfaces_do_not(self):
        sid, token = self._rejected_session()
        job = self.service._load_meta(sid)["provider_job"]
        self.assertEqual(job["status"], "fallback")
        gen_exec = job["executions"]["student"]["generator"]
        draft = gen_exec["rejected_draft"]
        self.assertEqual(
            draft["content"], "{not valid <script>alert(1)</script>")
        detail = self.service.teacher_session(sid)
        self.assertEqual(
            detail["provider_job"]["executions"]["student"]
            ["generator"]["rejected_draft"], draft)
        # Current preview remains the deterministic baseline.
        self.assertEqual(detail["feedback_delivery"]["preview_source"],
                         "deterministic")
        # Student snapshots never carry executions or rejected wording.
        snapshot = self.service.snapshot(sid, token)
        self.assertEqual(set(snapshot["provider_job"]),
                         {"status", "provider_mode"})
        self.assertNotIn("rejected_draft", json.dumps(snapshot))
        self.assertNotIn("<script>", json.dumps(snapshot))
        # Released feedback stays clean before and after release.
        released = self.service.release_feedback(sid, {
            "message_sha256":
                detail["feedback_delivery"]["preview_sha256"]})
        self.assertTrue(released["released"])
        snapshot = self.service.snapshot(sid, token)
        self.assertNotIn("rejected_draft", json.dumps(snapshot))
        self.assertNotIn("<script>", json.dumps(snapshot))
        self.assertEqual(set(snapshot["provider_job"]),
                         {"status", "provider_mode"})

    def test_rejection_code_without_retained_wording(self):
        self.opener = RecordingOpener(wire_reply(content=123))
        generator = FullFeedbackAittaGenerator(
            AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                           opener=self.opener))
        self.service = DemoService(
            root=Path(self.tmp.name), bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            review_mode="legacy_single", provider_mode="hosted",
            generator=generator)
        self.addCleanup(self.service.close, wait=True)
        created = self.service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            self.service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]
            ["status"] in ("ready", "fallback")))
        gen_exec = (self.service.teacher_session(sid)["provider_job"]
                    ["executions"]["student"]["generator"])
        self.assertNotIn("rejected_draft", gen_exec)
        self.assertEqual(gen_exec["rejection_code"],
                         "invalid_message_content")
        self.assertNotIn("rejected_draft", json.dumps(
            self.service.snapshot(sid, token)))

    def test_teacher_routes_require_unlock(self):
        sid, _ = self._rejected_session()
        pin = f"{secrets.randbelow(1000000):06d}"
        server = make_server(self.service, pin)
        thread = threading.Thread(
            target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        for path in (f"/api/teacher/sessions/{sid}",
                     f"/api/teacher/sessions/{sid}/export"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + path, timeout=10)
            self.assertEqual(ctx.exception.code, 403)
        # An unlocked teacher sees the draft in detail and export.
        request = urllib.request.Request(
            f"{base}/api/teacher/unlock",
            data=json.dumps({"pin": pin}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            cookie = response.headers["Set-Cookie"].split(";")[0]
        for path in (f"/api/teacher/sessions/{sid}",
                     f"/api/teacher/sessions/{sid}/export"):
            request = urllib.request.Request(
                base + path, headers={"Cookie": cookie})
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read())
            draft = (payload["provider_job"]["executions"]["student"]
                     ["generator"]["rejected_draft"])
            self.assertEqual(
                draft["content"],
                "{not valid <script>alert(1)</script>")


class ContextOpener(RecordingOpener):
    """Answers with a wire reply whose candidate_id is read from the
    request's user-message context (multi/worked_first payloads select
    their id at call time)."""

    def __init__(self, sections):
        super().__init__()
        self.sections = sections

    def open(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        context = json.loads(body["messages"][1]["content"])
        worked_reviews = None
        if context.get("schema") \
                == "phase3_worked_first_feedback_generation_v1":
            worked_reviews = [
                {"question_id": example["question_id"],
                 "steps": ["Follow the checked method shown for "
                           "this question."],
                 "reasoning_question":
                     "Why does the checked method work here?"}
                for example in context.get("review_examples") or []
                if example.get("status") == "ready"]
        self.payload = wire_reply(
            candidate_id=context["selected_candidate"]["candidate_id"],
            sections=self.sections, worked_reviews=worked_reviews)
        return super().open(request, timeout)


class WorkedFirstRejectedDraftTests(unittest.TestCase):
    """The real worked_first multi path must retain the rejected draft
    for teachers while every student surface stays clean."""

    SENTINEL = "You completed <b>one</b> step toward review."

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_grounded_bank()
        self.taxonomy = make_grounded_taxonomy(self.bank)
        wrong = sorted({positions(self.bank, "pct", 0)[0],
                        positions(self.bank, "alg", 0)[0]})
        self.rows = grounded_rows(self.bank, wrong)
        self.sections = dict(
            VALID_SECTIONS, assessment_summary=self.SENTINEL)

    def _complete(self, generator, root, cache=None):
        service = DemoService(
            root=root, bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            provider_mode="hosted", generator=generator)
        if cache is not None:
            service.cache = cache
        self.addCleanup(service.close, wait=True)
        created = service.create_session(
            feedback_variant="worked_first")
        sid, token = created["session_id"], created["student_token"]
        for row in self.rows:
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]
            ["status"] in ("ready", "fallback")),
            "provider job did not finish")
        return service, sid, token

    def test_worked_first_rejected_draft_native_and_cached(self):
        for mode in ("native", "cached"):
            with self.subTest(mode=mode):
                opener = ContextOpener(self.sections)
                native = FullFeedbackAittaGenerator(
                    AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                                   opener=opener))
                if mode == "native":
                    generator = native
                else:
                    cache_root = Path(self.tmp.name) / "captures"
                    cache_root.mkdir(exist_ok=True)
                    cache = CaptureCache(cache_root, None)
                    generator = CachedFullFeedbackGenerator(
                        native, cache, MODEL, "hash")
                service, sid, token = self._complete(
                    generator, Path(self.tmp.name) / mode,
                    cache=getattr(generator, "cache", None))
                self.assertEqual(len(opener.calls), 1)
                request = opener.calls[0]["body"]
                self.assertIn(
                    WF_PROMPT,
                    request["messages"][1]["content"])
                job = service._load_meta(sid)["provider_job"]
                self.assertEqual(job["status"], "fallback")
                gen_exec = job["executions"]["student"]["generator"]
                code = (gen_exec.get("rejection_code")
                        or (gen_exec.get("metadata") or {})
                        .get("rejection_code"))
                self.assertEqual(
                    code, "markup_in_assessment_summary")
                draft = gen_exec["rejected_draft"]
                self.assertIn(self.SENTINEL, draft["content"])
                self.assertFalse(draft["truncated"])
                detail = service.teacher_session(sid)
                self.assertEqual(
                    detail["feedback_delivery"]["preview_source"],
                    "deterministic")
                snapshot = service.snapshot(sid, token)
                self.assertNotIn(self.SENTINEL, json.dumps(snapshot))
                self.assertNotIn("rejected_draft", json.dumps(snapshot))
                released = service.release_feedback(sid, {
                    "message_sha256": detail["feedback_delivery"]
                    ["preview_sha256"]})
                self.assertTrue(released["released"])
                snapshot = service.snapshot(sid, token)
                self.assertNotIn(self.SENTINEL, json.dumps(snapshot))

    def test_worked_first_numeric_sections_native_and_cached(self):
        numeric = {
            "assessment_summary":
                "You finished the assessment; 38 of 40 answers were "
                "correct.",
            "observed_strengths":
                "You answered thirty-eight questions correctly on "
                "this assessment.",
            "review_focus":
                "Two assessed topics are possible starting points "
                "for review.",
            "next_steps":
                "If your teacher approves, work through one checked "
                "example at a time with your teacher."}
        for mode in ("native", "cached"):
            with self.subTest(mode=mode):
                opener = ContextOpener(numeric)
                native = FullFeedbackAittaGenerator(
                    AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                                   opener=opener))
                cache = None
                generator = native
                if mode == "cached":
                    cache_root = Path(self.tmp.name) / "num_captures"
                    cache_root.mkdir(exist_ok=True)
                    cache = CaptureCache(cache_root, None)
                    generator = CachedFullFeedbackGenerator(
                        native, cache, MODEL, "hash")
                service, sid, token = self._complete(
                    generator, Path(self.tmp.name) / f"num_{mode}",
                    cache=cache)
                self.assertEqual(len(opener.calls), 1)
                job = service._load_meta(sid)["provider_job"]
                self.assertEqual(job["status"], "ready")
                gen_exec = job["executions"]["student"]["generator"]
                self.assertNotIn("rejected_draft", gen_exec)
                self.assertIsNone(generator.last_rejected_draft)
                end = service._load_meta(sid)["checkpoints"]["end"]
                review = end["student_review"]
                self.assertEqual(
                    review["trace"]["guardrails_version"],
                    "provider_full_narrative_shape_checked_v2")
                sources = review["message"]["section_sources"]
                self.assertTrue(all(
                    source == "provider" for source in sources.values()))
                sections = review["message"]["sections"]
                self.assertEqual(
                    [s["kind"] for s in sections],
                    ["assessment_summary", "observed_strengths",
                     "review_focus", "next_steps"])
                self.assertEqual(
                    [s["title"] for s in sections],
                    ["Assessment summary", "Observed strengths",
                     "Review focus", "Next steps"])
                texts = {s["kind"]: s["text"] for s in sections}
                for kind, text in numeric.items():
                    if kind == "review_focus":
                        self.assertTrue(texts[kind].startswith(text))
                        self.assertTrue(texts[kind].endswith(SCOPE))
                    else:
                        self.assertEqual(texts[kind], text)
                # Scores, checked examples and practice stay
                # application-owned and unchanged.
                detail = service.teacher_session(sid)
                preview = detail["feedback_delivery"]["preview"]
                self.assertEqual(preview["total"],
                                 {"correct": 38, "out_of": 40})
                self.assertEqual(preview["review_examples"],
                                 review.get("review_examples") or [])
                by_id = {c["candidate_id"]: c for c in
                         review.get("selected_candidates") or []}
                expected_cards = [
                    resolve_practice(
                        slot["context"], end["evidence"],
                        candidate=by_id.get(slot.get("candidate_id")),
                        aligned=True)["card"]
                    for slot in end.get("practice_contexts") or []]
                self.assertEqual(
                    preview.get("practice_activities") or [],
                    expected_cards)
                # No automatic release: teacher must approve first.
                pre = service.snapshot(sid, token)
                self.assertIsNone(pre["feedback"]["end"])
                self.assertEqual(pre["feedback_delivery"]["status"],
                                 "awaiting_teacher_review")
                released = service.release_feedback(sid, {
                    "message_sha256": detail["feedback_delivery"]
                    ["preview_sha256"]})
                self.assertTrue(released["released"])
                out = service._load_meta(
                    sid)["feedback_release"]["released_feedback"]
                self.assertEqual(out["total"], preview["total"])
                self.assertEqual(out["review_examples"],
                                 preview["review_examples"])
                self.assertEqual(out["practice_activities"],
                                 preview["practice_activities"])
                snapshot = service.snapshot(sid, token)
                self.assertNotIn("rejected_draft", json.dumps(snapshot))
                if mode == "cached":
                    # An identical request reuses the capture - no
                    # second provider call.
                    again = service.create_session(
                        feedback_variant="worked_first")
                    sid2, token2 = (again["session_id"],
                                    again["student_token"])
                    for row in self.rows:
                        service.submit_response(sid2, token2, row)
                    self.assertTrue(wait_for(
                        lambda: service._load_meta(sid2)
                        ["provider_job"]["status"]
                        in ("ready", "fallback")))
                    self.assertEqual(len(opener.calls), 1)
                    gen_exec2 = (service._load_meta(sid2)
                                 ["provider_job"]["executions"]
                                 ["student"]["generator"])
                    self.assertTrue(gen_exec2["reused"])


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1",
                     "opt-in installed Edge + websocket-client browser test")
class RejectedDraftBrowserTests(unittest.TestCase):
    def setUp(self):
        from tests.test_research_browser import Browser
        edge = Path(os.environ.get(
            "EDGE_BINARY",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"))
        if not edge.is_file():
            self.skipTest("Microsoft Edge is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_browser_directory, self.tmp)
        self.root = Path(self.tmp.name)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.opener = RecordingOpener(wire_reply(
            content="<script>alert(1)</script><img src=x>"))
        generator = FullFeedbackAittaGenerator(
            AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                           opener=self.opener))
        self.service = DemoService(
            root=self.root / "data", bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            review_mode="legacy_single", provider_mode="hosted",
            generator=generator)
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
            "--disable-gpu", "--no-first-run", "--disable-extensions",
            "--disable-component-update",
            "--disable-background-networking",
            "--no-default-browser-check", "--window-size=1440,1100",
            "about:blank"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_browser)
        port = read_devtools_port(profile)
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

    def test_rejected_draft_disclosure_escaped_and_student_clean(self):
        created = self.service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            self.service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]
            ["status"] in ("ready", "fallback")),
            "provider job did not finish")
        b = self.browser
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
        b.wait("document.querySelector("
               "'#tab-feedback details.rejected-draft')")
        box = "document.querySelector(" \
              "'#tab-feedback details.rejected-draft')"
        self.assertTrue(b.js(
            f"{box}.textContent.includes("
            "'Rejected Aitta draft (unvalidated)')"))
        self.assertTrue(b.js(
            f"{box}.textContent.includes('invalid_content_json')"))
        # Unsafe markup is literal text inside <pre>, never parsed.
        pre = "document.querySelector(" \
              "'#tab-feedback details.rejected-draft pre')"
        self.assertTrue(b.js(
            f"{pre}.textContent.includes('<script>alert(1)</script>')"))
        self.assertTrue(b.js(
            f"{pre}.innerHTML.includes('&lt;script&gt;')"))
        self.assertFalse(b.js(f"Boolean({box}.querySelector('script'))"))
        self.assertFalse(b.js(f"Boolean({box}.querySelector('img'))"))
        # Not part of the Current preview or any student surface.
        self.assertFalse(b.js(
            "document.querySelector('#tab-overview')"
            " && document.querySelector('#tab-overview').textContent"
            ".includes('<script>alert(1)')"))
        if os.environ.get("BROWSER_SCREENSHOTS"):
            b.js(
                "(() => { const d = document.querySelector("
                "'#tab-feedback details.rejected-draft');"
                " let n = d; while (n) {"
                " if (n.tagName === 'DETAILS') n.open = true;"
                " n = n.parentElement; }"
                " d.scrollIntoView({block: 'center'}); })()")
            b.wait(
                "document.querySelector("
                "'#tab-feedback details.rejected-draft')"
                ".getBoundingClientRect().top > 0", timeout=5)
            b.screenshot("rejected_draft_disclosure.png")
        # The student released-snapshot bridge was removed entirely:
        # teacher detail payloads can no longer be projected onto the
        # student page or into localStorage, and nothing was written.
        self.assertEqual(
            b.js("typeof buildStudentSnapshotFromDetail"), "undefined")
        self.assertEqual(
            b.js("typeof saveReleasedStudentSnapshot"), "undefined")
        self.assertIsNone(b.js(
            "localStorage.getItem('demoReleasedSnapshot')"))
        self.assertEqual(b.errors, [])

    def test_rejection_reason_without_retained_wording(self):
        self.opener.payload = wire_reply(content=123)
        created = self.service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, correct=False, count=40):
            self.service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]
            ["status"] in ("ready", "fallback")),
            "provider job did not finish")
        b = self.browser
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
        b.wait("document.querySelector("
               "'#tab-feedback details.rejected-draft')")
        box = "document.querySelector(" \
              "'#tab-feedback details.rejected-draft')"
        self.assertTrue(b.js(
            f"{box}.textContent.includes('invalid_message_content')"))
        self.assertTrue(b.js(
            f"{box}.textContent.includes("
            "'Rejected wording is unavailable in this capture.')"))
        self.assertFalse(b.js(f"Boolean({box}.querySelector('pre'))"))
        self.assertEqual(b.errors, [])


if __name__ == "__main__":
    unittest.main()
