import copy
import json
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import phase3_paths  # noqa: F401
from aitta_generator import AittaGenerator
from demo_service import DemoService
from full_feedback import (
    CachedFullFeedbackGenerator, FullFeedbackAittaGenerator)
from replay_provider_scenarios import CaptureCache
from research_workspace import ResearchConflict
from tests.helpers import make_bank, make_taxonomy
from tests.test_demo_service import fake_diagnostics
from tests.test_scenario_replays import replay_fixture
from tests.test_assessment_worked_review import VALID_SECTIONS


MODEL = "openai/gpt-oss-120b"


class Metadata:
    def __init__(self):
        self.last_metadata = {"status": "completed"}


def _obtain(cache, calls, reply="ok", failures=()):
    def callback():
        calls.append(len(calls) + 1)
        if len(calls) in failures:
            raise TimeoutError("simulated timeout")
        return reply
    return cache.obtain("aitta_test", {"req": 1}, callback, Metadata())


class CaptureRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = CaptureCache(self.root, None)

    def _base(self):
        return next(self.root.glob("aitta_test_*.json"))

    def test_failed_capture_reused_by_default(self):
        calls = []
        reply, first = _obtain(self.cache, calls, failures=(1,))
        self.assertIsNone(reply)
        self.assertFalse(first["reused"])
        reply, second = _obtain(self.cache, calls)
        self.assertIsNone(reply)
        self.assertTrue(second["reused"])
        self.assertEqual(calls, [1])
        self.assertEqual(self.cache.new_calls, 1)

    def test_retry_scope_retries_failed_once(self):
        calls = []
        _obtain(self.cache, calls, failures=(1,))
        base = self._base()
        base_bytes = base.read_bytes()
        with self.cache.retry_failed_scope(True):
            reply, second = _obtain(self.cache, calls)
        self.assertEqual(reply, "ok")
        self.assertFalse(second["reused"])
        self.assertEqual(second["attempt_number"], 2)
        self.assertTrue(second["retry_of_failed_capture"])
        self.assertEqual(second["previous_capture_file"], base.name)
        attempt = self.root / second["capture_file"]
        self.assertTrue(attempt.name.endswith("_attempt_000000002.json"))
        self.assertEqual(base.read_bytes(), base_bytes)
        record = json.loads(attempt.read_text(encoding="utf-8"))
        self.assertTrue(record["ok"])
        self.assertEqual(record["attempt_number"], 2)
        self.assertTrue(record["retry_of_failed_capture"])
        self.assertEqual(record["previous_capture_file"], base.name)
        self.assertEqual(calls, [1, 2])
        reply, third = _obtain(self.cache, calls)
        self.assertEqual(reply, "ok")
        self.assertTrue(third["reused"])
        self.assertEqual(calls, [1, 2])
        fresh = CaptureCache(self.root, None)
        reply, fourth = _obtain(fresh, [])
        self.assertEqual(reply, "ok")
        self.assertTrue(fourth["reused"])
        self.assertEqual(fourth["capture_file"], attempt.name)

    def test_repeated_retry_failure_appends_attempts(self):
        calls = []
        _obtain(self.cache, calls, failures=(1,))
        with self.cache.retry_failed_scope(True):
            reply, second = _obtain(self.cache, calls, failures=(2,))
        self.assertIsNone(reply)
        self.assertEqual(calls, [1, 2])
        self.assertFalse(second["reused"])
        second_file = self.root / second["capture_file"]
        self.assertFalse(json.loads(second_file.read_text())["ok"])
        with self.cache.retry_failed_scope(True):
            reply, third = _obtain(self.cache, calls, failures=(3,))
        self.assertIsNone(reply)
        self.assertEqual(third["attempt_number"], 3)
        self.assertEqual(third["previous_capture_file"], second_file.name)
        self.assertEqual(calls, [1, 2, 3])
        reply, fourth = _obtain(self.cache, calls)
        self.assertIsNone(reply)
        self.assertTrue(fourth["reused"])
        self.assertEqual(calls, [1, 2, 3])

    def test_budget_blocks_retry_before_callback(self):
        cache = CaptureCache(self.root, 1)
        calls = []
        _obtain(cache, calls, failures=(1,))
        with cache.retry_failed_scope(True):
            with self.assertRaises(SystemExit):
                _obtain(cache, calls)
        self.assertEqual(calls, [1])
        self.assertEqual(list(self.root.glob("*_attempt_*.json")), [])

    def test_successful_capture_never_retried(self):
        calls = []
        _obtain(self.cache, calls)
        with self.cache.retry_failed_scope(True):
            reply, second = _obtain(self.cache, calls)
        self.assertEqual(reply, "ok")
        self.assertTrue(second["reused"])
        self.assertEqual(calls, [1])

    def test_scope_requires_bool_and_restores(self):
        with self.assertRaises(ValueError):
            with self.cache.retry_failed_scope("yes"):
                pass
        self.assertFalse(self.cache._retry_failed.get())
        with self.cache.retry_failed_scope(True):
            self.assertTrue(self.cache._retry_failed.get())
            with self.cache.retry_failed_scope(False):
                self.assertFalse(self.cache._retry_failed.get())
            self.assertTrue(self.cache._retry_failed.get())
        self.assertFalse(self.cache._retry_failed.get())
        boom = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            with self.cache.retry_failed_scope(True):
                raise boom
        self.assertFalse(self.cache._retry_failed.get())

    def test_scope_is_per_thread_and_lock_dedups(self):
        calls = []
        _obtain(self.cache, calls, failures=(1,))
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(enabled):
            barrier.wait()
            with self.cache.retry_failed_scope(enabled):
                outcomes.append(_obtain(self.cache, calls))

        threads = [threading.Thread(target=worker, args=(flag,))
                   for flag in (True, False)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(sorted(o[1]["reused"] for o in outcomes),
                         [False, True])

    def test_tampered_attempt_record_rejected(self):
        calls = []
        _obtain(self.cache, calls, failures=(1,))
        with self.cache.retry_failed_scope(True):
            _, second = _obtain(self.cache, calls, failures=(2,))
        attempt = self.root / second["capture_file"]
        record = json.loads(attempt.read_text())
        record["request"] = {"req": 999}
        attempt.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ValueError):
            _obtain(self.cache, calls)
        self.assertEqual(calls, [1, 2])

    def test_corrupt_attempt_json_rejected(self):
        calls = []
        _obtain(self.cache, calls, failures=(1,))
        with self.cache.retry_failed_scope(True):
            _, second = _obtain(self.cache, calls, failures=(2,))
        attempt = self.root / second["capture_file"]
        attempt.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            _obtain(self.cache, calls)

    def test_tampered_provider_rejected(self):
        calls = []
        _obtain(self.cache, calls, failures=(1,))
        base = self._base()
        record = json.loads(base.read_text())
        record["provider"] = "jev"
        base.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ValueError):
            _obtain(self.cache, calls)
        self.assertEqual(calls, [1])
        self.assertEqual(self.cache.new_calls, 1)

    def test_base_directory_rejected_before_call(self):
        self.cache.path("aitta_test", {"req": 1}).mkdir()
        calls = []
        with self.assertRaises(ValueError):
            _obtain(self.cache, calls)
        self.assertEqual(calls, [])
        self.assertEqual(self.cache.new_calls, 0)

    def test_attempt_directory_rejected_before_call(self):
        calls = []
        _obtain(self.cache, calls, failures=(1,))
        self.cache._attempt_path(self._base(), 2).mkdir()
        with self.assertRaises(ValueError):
            _obtain(self.cache, calls)
        with self.cache.retry_failed_scope(True):
            with self.assertRaises(ValueError):
                _obtain(self.cache, calls)
        self.assertEqual(calls, [1])
        self.assertEqual(self.cache.new_calls, 1)


def _flaky(cache, calls, outcomes):
    """Hosted-style obtain with per-outcome transport metadata."""
    adapter = Metadata()

    def callback():
        calls.append(len(calls) + 1)
        outcome = (outcomes[len(calls) - 1]
                   if len(calls) <= len(outcomes) else "ok")
        if isinstance(outcome, BaseException):
            adapter.last_metadata = {
                "status": "failed",
                "transport": {"stage": "open_failed",
                              "failure_kind": "timeout"}}
            raise outcome
        adapter.last_metadata = {"status": "completed"}
        return "ok"
    return cache.obtain("aitta_full", {"req": 1}, callback, adapter)


class FreshCaptureTests(unittest.TestCase):
    """reuse_captures=False: hosted demo calls never reuse captures."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = CaptureCache(self.root, None, reuse_captures=False)

    def _seed(self, record):
        base = self.cache.path("aitta_test", {"req": 1})
        if isinstance(record, str):
            base.write_text(record, encoding="utf-8")
        else:
            base.write_text(json.dumps(record), encoding="utf-8")
        return base

    def _valid_record(self, ok=True, reply="old"):
        return {"provider": "aitta_test", "request": {"req": 1},
                "ok": ok, "reply": reply, "metadata": {},
                "original_call_latency_ms": 1, "origin": "hosted_replay"}

    def _retry_cache(self, budget=None):
        cache = CaptureCache(self.root, budget, automatic_retries=True,
                             reuse_captures=False)
        cache._stop_retries.wait = mock.Mock(return_value=False)
        return cache

    def test_identical_payload_calls_fresh_each_time(self):
        calls = []
        _, first = _obtain(self.cache, calls)
        _, second = _obtain(self.cache, calls)
        self.assertEqual(calls, [1, 2])
        self.assertFalse(first["reused"])
        self.assertFalse(second["reused"])
        self.assertNotEqual(first["capture_file"], second["capture_file"])
        self.assertEqual(self.cache.new_calls, 2)
        self.assertEqual(
            len(list(self.root.glob("aitta_test_*.json"))), 2)

    def test_saved_successful_capture_ignored_and_preserved(self):
        base = self._seed(self._valid_record(ok=True))
        before = base.read_bytes()
        calls = []
        reply, execution = _obtain(self.cache, calls)
        self.assertEqual(reply, "ok")
        self.assertFalse(execution["reused"])
        self.assertNotEqual(execution["capture_file"], base.name)
        self.assertEqual(calls, [1])
        self.assertEqual(base.read_bytes(), before)

    def test_saved_failed_capture_ignored_and_preserved(self):
        base = self._seed(self._valid_record(ok=False, reply=None))
        before = base.read_bytes()
        calls = []
        reply, execution = _obtain(self.cache, calls)
        self.assertEqual(reply, "ok")
        self.assertFalse(execution["reused"])
        self.assertEqual(calls, [1])
        self.assertEqual(base.read_bytes(), before)

    def test_malformed_saved_capture_not_read(self):
        base = self._seed("{not json")
        calls = []
        reply, execution = _obtain(self.cache, calls)
        self.assertEqual(reply, "ok")
        self.assertFalse(execution["reused"])
        self.assertEqual(calls, [1])
        self.assertEqual(base.read_text(encoding="utf-8"), "{not json")

    def test_new_cache_instance_same_root_still_fresh(self):
        calls = []
        _obtain(self.cache, calls)
        fresh = CaptureCache(self.root, None, reuse_captures=False)
        _obtain(fresh, calls)
        self.assertEqual(calls, [1, 2])

    def test_budget_exhausted_blocks_call_without_capture_reuse(self):
        self._seed(self._valid_record(ok=True))
        cache = CaptureCache(self.root, 0, reuse_captures=False)
        calls = []
        with self.assertRaises(SystemExit):
            _obtain(cache, calls)
        self.assertEqual(calls, [])

    def test_automatic_retry_timeout_then_success(self):
        cache = self._retry_cache()
        calls = []
        reply, execution = _flaky(cache, calls,
                                  [TimeoutError("n"), "ok"])
        self.assertEqual(reply, "ok")
        self.assertEqual(calls, [1, 2])
        self.assertEqual(execution["automatic_retry"], {
            "attempts": 2, "max_attempts": 3, "stop_reason": "success"})
        captures = sorted(self.root.glob("aitta_full_*.json"))
        self.assertEqual(len(captures), 2)
        for path in captures:
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("retry_of_failed_capture", record)
            self.assertNotIn("previous_capture_file", record)

    def test_automatic_retry_exhausts_at_three_calls(self):
        cache = self._retry_cache()
        calls = []
        reply, execution = _flaky(
            cache, calls, [TimeoutError("n")] * 3)
        self.assertIsNone(reply)
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(execution["automatic_retry"]["stop_reason"],
                         "attempts_exhausted")

    def test_nonretryable_failure_single_call(self):
        cache = self._retry_cache()
        adapter = Metadata()
        adapter.last_metadata = {
            "status": "failed",
            "transport": {"stage": "body_received",
                          "failure_kind": "http_error",
                          "http_status": 400}}

        calls = []

        def counted():
            calls.append(1)
            raise ValueError("contract rejection")
        reply, execution = cache.obtain(
            "aitta_full", {"req": 1}, counted, adapter)
        self.assertIsNone(reply)
        self.assertEqual(calls, [1])
        self.assertEqual(execution["automatic_retry"]["stop_reason"],
                         "non_retryable")

    def test_concurrent_identical_calls_both_fresh(self):
        calls = []
        barrier = threading.Barrier(2)
        outcomes = []

        def worker():
            barrier.wait(timeout=10)
            outcomes.append(_obtain(self.cache, calls))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [execution["reused"] for _reply, execution in outcomes],
            [False, False])
        files = {execution["capture_file"]
                 for _reply, execution in outcomes}
        self.assertEqual(len(files), 2)


APP_JS = Path(__file__).resolve().parent.parent / "web_demo" / "app.js"
HARNESS = """
const vm = require("vm");
const fs = require("fs");
const source = fs.readFileSync(process.argv[2], "utf8");
const wanted = new Set(["FAILURE_KIND_LABELS", "FAILURE_STAGE_LABELS",
  "hostedConfirmed", "captureProvenanceKind", "selectionProvenance",
  "aittaProvenance"]);
const picked = source.split(
  /\\n(?=(?:async\\s+)?(?:function|const|let|var)\\s)/).filter((chunk) => {
  const m = chunk.match(
    /^(?:async\\s+)?(?:function|const|let|var)\\s+([A-Za-z_$][\\w$]*)/);
  return m && wanted.has(m[1]);
});
if (picked.length !== wanted.size) {
  throw new Error("app.js provenance helpers not found");
}
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(picked.join("\\n"), sandbox);
const exprs = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
process.stdout.write(JSON.stringify(exprs.map(
  (expr) => vm.runInContext("(" + expr + ")", sandbox))));
"""


class ProvenanceLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("installed Node required")
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.harness = Path(tmp.name) / "harness.js"
        cls.harness.write_text(HARNESS, encoding="utf-8")
        cls.exprs = Path(tmp.name) / "exprs.json"

    def js(self, *expressions):
        self.exprs.write_text(json.dumps(expressions), encoding="utf-8")
        result = subprocess.run(
            [self.node, str(self.harness), str(APP_JS), str(self.exprs)],
            capture_output=True, text=True, encoding="utf-8",
            check=True, timeout=30)
        return json.loads(result.stdout)

    def aitta(self, review, execution="undefined"):
        return self.js(
            f"aittaProvenance({json.dumps(review)}, {execution})")[0]

    def test_deterministic_review_with_absent_execution(self):
        review = {"trace": {"phrasing_source": "deterministic"}}
        self.assertEqual(self.aitta(review), "Deterministic feedback")
        self.assertEqual(self.aitta(review, "null"), "Deterministic feedback")
        self.assertEqual(self.aitta(review, "{}"), "Deterministic feedback")

    def test_injected_review_without_provenance(self):
        review = {"trace": {
            "phrasing_source": "injected_generator_full_sections"}}
        self.assertEqual(
            self.aitta(review),
            "Injected full-feedback generator - hosted provenance "
            "unavailable")

    def test_retry_and_cached_kinds(self):
        review = {"trace": {
            "phrasing_source": "injected_generator_full_sections"}}
        completed = {"status": "completed"}
        fresh_retry = self.aitta(review, json.dumps({
            "capture_file": "aitta_full_x.json", "reused": False,
            "retry_of_failed_capture": True, "metadata": completed}))
        self.assertEqual(
            fresh_retry, "Aitta - fresh hosted retry (full structured feedback)")
        cached_retry = self.aitta(review, json.dumps({
            "capture_file": "aitta_full_x_attempt_000000002.json",
            "reused": True, "retry_of_failed_capture": True,
            "attempt_number": 2, "metadata": completed}))
        self.assertEqual(
            cached_retry,
            "Aitta - cached hosted response (full structured feedback)")
        fresh = self.aitta(review, json.dumps({
            "capture_file": "aitta_full_x.json", "reused": False,
            "metadata": completed}))
        self.assertEqual(
            fresh, "Aitta - fresh hosted response (full structured feedback)")

    def test_failure_labels_use_closed_transport_fields(self):
        review = {"trace": {
            "phrasing_source": "injected_generator_full_sections"}}
        cached_failure = self.aitta(review, json.dumps({
            "capture_file": "aitta_full_x.json", "reused": True,
            "metadata": {"status": "failed",
                         "transport": {"failure_kind": "timeout"},
                         "failure_stage": "open_failed"}}))
        self.assertEqual(
            cached_failure,
            "Aitta - cached failure (timeout, open_failed) - "
            "deterministic feedback used")
        retry_failure = self.aitta(review, json.dumps({
            "capture_file": "aitta_full_x_attempt_000000002.json",
            "reused": False, "retry_of_failed_capture": True,
            "metadata": {"status": "failed",
                         "transport": {"failure_kind": "https://evil.example"},
                         "failure_stage": "body_read_failed"}}))
        self.assertEqual(
            retry_failure,
            "Aitta - fresh hosted retry failed (body_read_failed) - "
            "deterministic feedback used")
        fresh_failure = self.aitta(review, json.dumps({
            "capture_file": "aitta_full_x.json", "reused": False,
            "metadata": {"status": "failed", "transport": {},
                         "failure_stage": "not a real stage"}}))
        self.assertEqual(
            fresh_failure,
            "Aitta - fresh hosted call failed - deterministic feedback used")
        self.assertNotEqual(cached_failure, retry_failure)

    def test_jev_completed_retry_label(self):
        review = {"trace": {"selection_source": "injected_selector"}}
        out = self.js(
            f"selectionProvenance({json.dumps(review)}, {{}}, "
            + json.dumps({"capture_file": "jev_x.json", "reused": False,
                          "retry_of_failed_capture": True,
                          "metadata": {"status": "completed"}})
            + ")")[0]
        self.assertEqual(out, "Jev - fresh hosted retry")


class SuccessOpener:
    def __init__(self, failures=0):
        self.calls = []
        self.failures = failures

    def open(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        context = json.loads(body["messages"][1]["content"])
        self.calls.append(body)
        if len(self.calls) <= self.failures:
            raise TimeoutError("simulated aitta timeout")
        candidate_id = context["selected_candidate"]["candidate_id"]
        content = {"candidate_id": candidate_id,
                   "sections": dict(VALID_SECTIONS)}
        if context.get("review_examples") is not None:
            content["worked_reviews"] = [
                {"question_id": e["question_id"],
                 "steps": ["Fixture checked step."],
                 "reasoning_question":
                     "Why does the fixture step work?",
                 "socratic_prompt":
                     f"What is question {e['question_id']} asking?",
                 "progressive_hints": [
                     f"Hint one for {e['question_id']}.",
                     "Hint two for the same question."]}
                for e in context["review_examples"]
                if e.get("status") == "ready"]
        reply = {"choices": [{"finish_reason": "stop",
                              "message": {"role": "assistant",
                                          "content": json.dumps(
                                              content)}}],
                 "model": MODEL,
                 "usage": {"prompt_tokens": 10, "completion_tokens": 20,
                           "total_tokens": 30}}
        class _Response:
            def __init__(self, data):
                self._data = json.dumps(data).encode("utf-8")

            def read(self, limit=-1):
                return self._data

            def close(self):
                pass
        return _Response(reply)


class ReplayRetryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.source_path, self.report_path, self.source = replay_fixture(
            self.root, self.bank, self.taxonomy)
        self.opener = SuccessOpener()
        (self.root / "captures").mkdir()
        self.cache = CaptureCache(self.root / "captures", None,
                                  reuse_captures=False)
        native = FullFeedbackAittaGenerator(
            AittaGenerator("fake-key-not-real-000000000000",
                           "https://aitta.example.test", model=MODEL,
                           opener=self.opener))
        self.generator = CachedFullFeedbackGenerator(
            native, self.cache, MODEL, "endpointhash" * 8)
        self.service = DemoService(
            root=self.root / "runtime", bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            replay_report=self.report_path,
            replay_source=self.source_path,
            provider_mode="hosted", generator=self.generator)
        self.addCleanup(self.service.close, wait=True)
        self.service.cache = self.cache
        self.library = self.service.research.library()

    def request(self, **extra):
        body = {"request_id": secrets.token_hex(16),
                "report_sha256": self.library["report_sha256"],
                "scenario_ids": [self.library["scenarios"][1]["id"]],
                "provider_mode": "hosted", "allow_provider_calls": True}
        body.update(extra)
        return body

    def wait(self, run_id, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            view = self.service.replays.get(run_id)
            if view["status"] not in ("queued", "running", "cancelling"):
                return view
            time.sleep(.03)
        self.fail("replay did not finish")

    def test_obsolete_retry_flag_rejected_like_any_unknown_key(self):
        for value in (True, False, "yes"):
            with self.subTest(value=value):
                body = self.request(retry_failed_provider_calls=value)
                with self.assertRaises(ValueError):
                    self.service.replays.start(body)
        body = self.request(unknown_field=True)
        with self.assertRaises(ValueError):
            self.service.replays.start(body)
        self.assertEqual(self.service.replays.list()["runs"], [])

    def test_run_records_fresh_provider_execution(self):
        run = self.service.replays.start(self.request())
        record = self.service.replays.get(run["id"])
        self.assertNotIn("retry_failed_provider_calls", record)
        self.assertEqual(record["provider_execution"],
                         "fresh_hosted_calls")

    def test_request_id_stays_idempotent(self):
        request = self.request()
        run = self.service.replays.start(request)
        again = self.service.replays.start(request)
        self.assertEqual(again["id"], run["id"])
        self.assertEqual(len(self.service.replays.list()["runs"]), 1)
        with self.assertRaises(ResearchConflict):
            self.service.replays.start(
                dict(request, max_parallel=2))
        self.assertEqual(len(self.service.replays.list()["runs"]), 1)
        finished = self.wait(run["id"])
        self.assertEqual(finished["status"], "complete")
        # Resubmitting the same request id after completion makes no
        # new hosted call.
        self.service.replays.start(request)
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(len(self.service.replays.list()["runs"]), 1)

    def test_each_new_request_makes_a_fresh_hosted_call(self):
        self.opener.failures = 1
        first = self.wait(
            self.service.replays.start(self.request())["id"])
        self.assertEqual(first["cases"][0]["provider_status"], "fallback")
        self.assertEqual(len(self.opener.calls), 1)
        captures = self.root / "captures"
        fresh_files = sorted(captures.glob("aitta_full_*_call_*.json"))
        self.assertEqual(len(fresh_files), 1)
        self.assertFalse(json.loads(
            fresh_files[0].read_text(encoding="utf-8"))["ok"])
        first_run_bytes = (self.root / "runtime" / "replay_runs" /
                           f"{first['id']}.json").read_bytes()
        # A new request id does not reuse the saved failure: it calls
        # again, and the earlier capture stays untouched.
        second = self.wait(
            self.service.replays.start(self.request())["id"])
        self.assertEqual(second["cases"][0]["provider_status"], "ready")
        self.assertEqual(len(self.opener.calls), 2)
        # A third new request does not reuse the saved success either.
        third = self.wait(
            self.service.replays.start(self.request())["id"])
        self.assertEqual(third["cases"][0]["provider_status"], "ready")
        self.assertEqual(len(self.opener.calls), 3)
        self.assertEqual(
            len(list(captures.glob("aitta_full_*_call_*.json"))), 3)
        self.assertEqual(
            list(captures.glob("*_attempt_*.json")), [])
        self.assertEqual(
            (self.root / "runtime" / "replay_runs" /
             f"{first['id']}.json").read_bytes(), first_run_bytes)
        for run in (second, third):
            session_id = run["cases"][0]["session_id"]
            detail = self.service.teacher_session(session_id)
            execution = (detail["provider_job"]["executions"]
                         ["student"]["generator"])
            self.assertFalse(execution["reused"])
            self.assertNotIn("retry_of_failed_capture", execution)

    def test_call_budget_still_enforced(self):
        first = self.wait(
            self.service.replays.start(self.request())["id"])
        self.assertEqual(first["cases"][0]["provider_status"], "ready")
        self.assertEqual(len(self.opener.calls), 1)
        self.cache.budget = 1
        second = self.wait(
            self.service.replays.start(self.request())["id"])
        # No fallback to the stored capture when the budget is spent.
        self.assertEqual(second["cases"][0]["provider_status"],
                         "fallback")
        self.assertEqual(len(self.opener.calls), 1)

    def test_legacy_single_worker_also_calls_fresh(self):
        self.opener.failures = 1
        legacy = DemoService(
            root=self.root / "legacy", bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            replay_report=self.report_path, replay_source=self.source_path,
            provider_mode="hosted", generator=self.generator,
            review_mode="legacy_single")
        self.addCleanup(legacy.close, wait=True)
        legacy.cache = self.cache

        def wait(run_id, timeout=20):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                view = legacy.replays.get(run_id)
                if view["status"] not in ("queued", "running", "cancelling"):
                    return view
                time.sleep(.03)
            self.fail("legacy replay did not finish")

        first = wait(legacy.replays.start(self.request())["id"])
        self.assertEqual(first["cases"][0]["provider_status"], "fallback")
        self.assertEqual(len(self.opener.calls), 1)
        second = wait(legacy.replays.start(self.request())["id"])
        self.assertEqual(second["cases"][0]["provider_status"], "ready")
        self.assertEqual(len(self.opener.calls), 2)

    def test_rules_replay_makes_no_hosted_call(self):
        self.opener.failures = 1
        first = self.wait(
            self.service.replays.start(self.request())["id"])
        self.assertEqual(len(self.opener.calls), 1)
        rules = self.wait(self.service.replays.start(
            self.request(provider_mode="rules",
                         allow_provider_calls=False))["id"])
        self.assertEqual(rules["status"], "complete")
        self.assertEqual(rules["provider_execution"], "rules_only")
        self.assertEqual(len(self.opener.calls), 1)


if __name__ == "__main__":
    unittest.main()
