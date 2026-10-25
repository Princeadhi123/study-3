import copy
import email.utils
import json
import secrets
import ssl
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import phase3_paths  # noqa: F401
import feedback_practice
from aitta_generator import AittaGenerator
from demo_service import DemoService
from evidence_feedback import build_evidence
from evidence_feedback_policy import (
    build_candidates, build_review_bundles)
from full_feedback import (
    CachedFullFeedbackGenerator, FullFeedbackAittaGenerator,
    build_full_input, build_multi_full_input)
from midpoint_feedback import (
    CANDIDATE_ID, CachedMidpointGenerator, MidpointAittaGenerator,
    build_midpoint_input)
from replay_provider_scenarios import (
    AUTOMATIC_RETRY_MAX, CaptureCache)
from transport_diagnostics import _retry_after_seconds
from worked_first_context import (
    SCHEMA as WF_SCHEMA, build_worked_first_context)
from tests.helpers import make_bank, make_taxonomy
from tests.test_assessment_worked_review import (
    grounded_rows, make_grounded_bank, make_grounded_taxonomy,
    positions)
from tests.test_demo_service import fake_diagnostics
from tests.test_full_feedback import (
    API_KEY, BASE_URL, MODEL, FakeResponse, RecordingOpener,
    VALID_SECTIONS, wire_reply)
from tests.test_llm_worked_reviews import (
    AUTHORED as WF_AUTHORED, GENERIC as WF_GENERIC,
    WorkedReviewOpener)
from tests.test_midpoint_feedback import AUTHORED, midpoint_rows

PROVIDER = "aitta_midpoint"


class SequenceOpener:
    """Serves queued outcomes in order; exceptions are raised."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append(request)
        outcome = (self.outcomes.pop(0) if self.outcomes
                   else TimeoutError("no outcome queued"))
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResponse(outcome)


def _http(status, retry_after=None, message="upstream failed"):
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://provider.invalid/v1", status, message, headers, None)


def _midpoint_reply():
    return wire_reply(candidate_id=CANDIDATE_ID,
                      sections=dict(AUTHORED))


class SequenceWorkedOpener(WorkedReviewOpener):
    """Raises queued errors first, then serves dynamic wire replies.

    worked_reviews is only attached when the request's wire context
    carries review_examples, matching the per-variant contract.
    """

    def __init__(self, failures, **kwargs):
        super().__init__(**kwargs)
        self.failures = list(failures)

    def open(self, request, timeout=None):
        if self.failures:
            self.calls.append({
                "body": json.loads(request.data.decode("utf-8")),
                "headers": dict(request.header_items())})
            raise self.failures.pop(0)
        body = json.loads(request.data.decode("utf-8"))
        context = json.loads(body["messages"][1]["content"])
        reviews = None
        if context.get("review_examples") is not None:
            ready = [e for e in context["review_examples"]
                     if e.get("status") == "ready"]
            reviews = []
            for index, example in enumerate(ready):
                steps, reasoning = (self.per_card[index]
                                    if index < len(self.per_card)
                                    else WF_GENERIC)
                reviews.append({
                    "question_id": example["question_id"],
                    "steps": list(steps),
                    "reasoning_question": reasoning,
                    "socratic_prompt":
                        f"What is question {example['question_id']} "
                        "asking you to find?",
                    "progressive_hints": [
                        f"Hint one for {example['question_id']}.",
                        "Hint two for the same question."]})
        self.payload = wire_reply(
            candidate_id=context["selected_candidate"]["candidate_id"],
            sections=self.sections, worked_reviews=reviews)
        return RecordingOpener.open(self, request, timeout)


class AutomaticRetryTests(unittest.TestCase):
    """Bounded transient retries inside CaptureCache.obtain."""

    def setUp(self):
        bank = make_bank()
        taxonomy = make_taxonomy(bank)
        evidence = build_evidence(
            bank, taxonomy, midpoint_rows(bank, wrong_positions=(0, 5)))
        self.input = build_midpoint_input(evidence)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.waits = []
        uniform = mock.patch(
            "replay_provider_scenarios.random.uniform",
            return_value=0.0)
        uniform.start()
        self.addCleanup(uniform.stop)

    def _native(self, opener):
        return MidpointAittaGenerator(
            AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                           opener=opener))

    def _cache(self, budget=None, automatic=True):
        cache = CaptureCache(self.root, budget,
                             automatic_retries=automatic)
        cache._stop_retries.wait = mock.Mock(
            side_effect=lambda delay=None:
            self.waits.append(delay) or False)
        return cache

    def _obtain(self, cache, native, provider=PROVIDER):
        payload = copy.deepcopy(self.input)
        return cache.obtain(
            provider, {"req": "fixed"},
            lambda: native.generate(payload), native)

    def _captures(self):
        return sorted(self.root.glob(f"{PROVIDER}_*.json"))

    def _failed_record(self):
        for path in self._captures():
            record = json.loads(path.read_text(encoding="utf-8"))
            if not record["ok"]:
                return record
        return None

    def test_timeout_then_success_two_calls(self):
        opener = SequenceOpener([TimeoutError("net"), _midpoint_reply()])
        reply, execution = self._obtain(
            self._cache(), self._native(opener))
        self.assertIsNotNone(reply)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(len(self._captures()), 2)
        self.assertEqual(execution["automatic_retry"], {
            "attempts": 2, "max_attempts": AUTOMATIC_RETRY_MAX,
            "stop_reason": "success"})
        self.assertEqual(len(self.waits), 1)
        self.assertGreaterEqual(self.waits[0], 2.0)
        self.assertLessEqual(self.waits[0], 2.5)

    def test_transient_chain_three_attempts(self):
        opener = SequenceOpener(
            [_http(503), TimeoutError("net"), _midpoint_reply()])
        reply, execution = self._obtain(
            self._cache(), self._native(opener))
        self.assertIsNotNone(reply)
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(execution["automatic_retry"]["attempts"], 3)
        self.assertEqual(execution["automatic_retry"]["stop_reason"],
                         "success")
        self.assertGreaterEqual(self.waits[1], 4.0)

    def test_all_timeouts_exhaust_then_failed_reuse(self):
        opener = SequenceOpener(
            [TimeoutError("n")] * 3)
        cache = self._cache()
        reply, execution = self._obtain(cache, self._native(opener))
        self.assertIsNone(reply)
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(execution["automatic_retry"]["stop_reason"],
                         "attempts_exhausted")
        self.assertEqual(len(self._captures()), 3)
        reply, second = self._obtain(cache, self._native(opener))
        self.assertIsNone(reply)
        self.assertTrue(second["reused"])
        self.assertNotIn("automatic_retry", second)
        self.assertEqual(len(opener.calls), 3)

    def test_retry_after_seconds_honored(self):
        opener = SequenceOpener(
            [_http(429, retry_after="5"), _midpoint_reply()])
        reply, execution = self._obtain(
            self._cache(), self._native(opener))
        self.assertIsNotNone(reply)
        self.assertEqual(self.waits, [5.0])
        record = self._failed_record()
        self.assertIsNotNone(record)
        self.assertEqual(
            record["metadata"]["retry_after_seconds"], 5.0)
        transport = record["metadata"]["transport"]
        self.assertEqual(transport["failure_kind"], "http_error")
        self.assertEqual(transport["http_status"], 429)

    def test_retry_after_http_date_honored(self):
        moment = email.utils.formatdate(time.time() + 7, usegmt=True)
        opener = SequenceOpener(
            [_http(503, retry_after=moment), _midpoint_reply()])
        reply, execution = self._obtain(
            self._cache(), self._native(opener))
        self.assertIsNotNone(reply)
        self.assertEqual(len(self.waits), 1)
        self.assertGreater(self.waits[0], 4.0)
        self.assertLessEqual(self.waits[0], 8.0)

    def test_retry_after_over_limit_stops(self):
        opener = SequenceOpener(
            [_http(429, retry_after="120"), _midpoint_reply()])
        reply, execution = self._obtain(
            self._cache(), self._native(opener))
        self.assertIsNone(reply)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.waits, [])
        self.assertEqual(execution["automatic_retry"]["stop_reason"],
                         "retry_after_too_long")

    def test_non_retryable_http_statuses(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                root = self.root / f"s{status}"
                root.mkdir()
                cache = CaptureCache(root, None,
                                     automatic_retries=True)
                cache._stop_retries.wait = mock.Mock(return_value=False)
                opener = SequenceOpener(
                    [_http(status), _midpoint_reply()])
                native = self._native(opener)
                payload = copy.deepcopy(self.input)
                reply, execution = cache.obtain(
                    PROVIDER, {"req": "fixed"},
                    lambda: native.generate(payload), native)
                self.assertIsNone(reply)
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(
                    execution["automatic_retry"]["stop_reason"],
                    "non_retryable")

    def test_contract_rejection_retried_then_success(self):
        malformed = {"unexpected": "shape"}
        opener = SequenceOpener([malformed, _midpoint_reply()])
        reply, execution = self._obtain(
            self._cache(), self._native(opener))
        self.assertIsNotNone(reply)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(len(self._captures()), 2)
        self.assertEqual(execution["automatic_retry"]["attempts"], 2)
        self.assertEqual(execution["automatic_retry"]["stop_reason"],
                         "success")
        self.assertEqual(len(self.waits), 1)
        record = self._failed_record()
        self.assertIsNotNone(record)
        self.assertEqual(
            record["metadata"]["transport"]["stage"],
            "body_received")
        self.assertEqual(
            record["metadata"]["failure_stage"],
            "response_contract_or_full_feedback_validation")

    def test_contract_rejections_exhaust_then_failed(self):
        malformed = {"unexpected": "shape"}
        opener = SequenceOpener(
            [dict(malformed) for _ in range(3)])
        reply, execution = self._obtain(
            self._cache(), self._native(opener))
        self.assertIsNone(reply)
        self.assertEqual(len(opener.calls), 3)
        captures = self._captures()
        self.assertEqual(len(captures), 3)
        self.assertTrue(all(
            not json.loads(path.read_text(encoding="utf-8"))["ok"]
            for path in captures))
        self.assertEqual(execution["automatic_retry"]["attempts"], 3)
        self.assertEqual(execution["automatic_retry"]["stop_reason"],
                         "attempts_exhausted")

    def test_contract_rejection_automatic_disabled_not_retried(self):
        malformed = {"unexpected": "shape"}
        opener = SequenceOpener([malformed, _midpoint_reply()])
        cache = self._cache(automatic=False)
        payload = copy.deepcopy(self.input)
        native = self._native(opener)
        reply, execution = cache.obtain(
            PROVIDER, {"req": "fixed"},
            lambda: native.generate(payload), native)
        self.assertIsNone(reply)
        self.assertEqual(len(opener.calls), 1)
        self.assertNotIn("automatic_retry", execution)
        self.assertEqual(self.waits, [])

    def test_ssl_certificate_failure_not_retried(self):
        error = urllib.error.URLError(
            ssl.SSLCertVerificationError(1, "certificate verify failed"))
        opener = SequenceOpener([error, _midpoint_reply()])
        reply, execution = self._obtain(
            self._cache(), self._native(opener))
        self.assertIsNone(reply)
        self.assertEqual(len(opener.calls), 1)
        record = self._failed_record()
        self.assertEqual(
            record["metadata"]["transport"]["failure_kind"],
            "tls_certificate_error")
        self.assertEqual(execution["automatic_retry"]["attempts"], 1)
        self.assertEqual(execution["automatic_retry"]["stop_reason"],
                         "non_retryable")
        self.assertEqual(self.waits, [])
        self.assertEqual(len(self._captures()), 1)

    def test_ssl_handshake_failure_not_retried(self):
        cases = {
            "wrapped": urllib.error.URLError(
                ssl.SSLError(1, "handshake failure")),
            "direct": ssl.SSLError(1, "handshake failure")}
        for label, error in cases.items():
            with self.subTest(case=label):
                root = self.root / f"tls_{label}"
                root.mkdir()
                cache = CaptureCache(root, None,
                                     automatic_retries=True)
                cache._stop_retries.wait = mock.Mock(
                    return_value=False)
                wait = cache._stop_retries.wait
                opener = SequenceOpener([error, _midpoint_reply()])
                native = self._native(opener)
                payload = copy.deepcopy(self.input)
                reply, execution = cache.obtain(
                    PROVIDER, {"req": "fixed"},
                    lambda: native.generate(payload), native)
                self.assertIsNone(reply)
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(
                    execution["automatic_retry"]["attempts"], 1)
                self.assertEqual(
                    execution["automatic_retry"]["stop_reason"],
                    "non_retryable")
                captures = sorted(
                    root.glob(f"{PROVIDER}_*.json"))
                self.assertEqual(len(captures), 1)
                record = json.loads(
                    captures[0].read_text(encoding="utf-8"))
                self.assertFalse(record["ok"])
                self.assertEqual(
                    record["metadata"]["transport"]["failure_kind"],
                    "tls_error")
                wait.assert_not_called()

    def test_budget_exhausted_mid_chain(self):
        opener = SequenceOpener([TimeoutError("n"), _midpoint_reply()])
        reply, execution = self._obtain(
            self._cache(budget=1), self._native(opener))
        self.assertIsNone(reply)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(execution["automatic_retry"]["stop_reason"],
                         "budget_exhausted")

    def test_shutdown_during_backoff(self):
        opener = SequenceOpener([TimeoutError("n"), _midpoint_reply()])
        cache = self._cache()
        cache._stop_retries.wait = mock.Mock(return_value=True)
        reply, execution = self._obtain(cache, self._native(opener))
        self.assertIsNone(reply)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(execution["automatic_retry"]["stop_reason"],
                         "shutdown")

    def test_reused_captures_never_auto_retry(self):
        opener = SequenceOpener([_midpoint_reply()])
        cache = self._cache()
        native = self._native(opener)
        self._obtain(cache, native)
        reply, second = self._obtain(cache, native)
        self.assertIsNotNone(reply)
        self.assertTrue(second["reused"])
        self.assertNotIn("automatic_retry", second)
        self.assertEqual(len(opener.calls), 1)

    def test_explicit_retry_chain_then_reuse(self):
        opener = SequenceOpener(
            [_http(400), TimeoutError("n"), _midpoint_reply()])
        cache = self._cache()
        native = self._native(opener)
        reply, first = self._obtain(cache, native)
        self.assertIsNone(reply)
        self.assertEqual(len(opener.calls), 1)
        with cache.retry_failed_scope(True):
            reply, second = self._obtain(cache, native)
        self.assertIsNotNone(reply)
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(second["automatic_retry"]["attempts"], 2)
        reply, third = self._obtain(cache, native)
        self.assertIsNotNone(reply)
        self.assertTrue(third["reused"])
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(len(self._captures()), 3)

    def test_concurrent_same_key_single_chain(self):
        opener = SequenceOpener([TimeoutError("n"), _midpoint_reply()])
        cache = self._cache()
        native = self._native(opener)
        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait(timeout=10)
            results.append(self._obtain(cache, native))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(
            sorted(execution["reused"]
                   for _reply, execution in results),
            [False, True])

    def test_ineligible_namespace_never_auto_retries(self):
        opener = SequenceOpener([TimeoutError("n"), _midpoint_reply()])
        reply, execution = self._obtain(
            self._cache(), self._native(opener),
            provider="aitta_test")
        self.assertIsNone(reply)
        self.assertEqual(len(opener.calls), 1)
        self.assertNotIn("automatic_retry", execution)
        self.assertEqual(self.waits, [])

    def test_disabled_cache_never_auto_retries(self):
        opener = SequenceOpener([TimeoutError("n"), _midpoint_reply()])
        reply, execution = self._obtain(
            self._cache(automatic=False), self._native(opener))
        self.assertIsNone(reply)
        self.assertEqual(len(opener.calls), 1)
        self.assertNotIn("automatic_retry", execution)

    def test_metadata_never_leaks_raw_errors_or_headers(self):
        error = urllib.error.HTTPError(
            "https://provider.invalid/v1", 503,
            "Authorization bearer sk-secret-123 leaked",
            {"X-Upstream-Secret": "sk-secret-123",
             "Retry-After": "1"}, None)
        opener = SequenceOpener([error, _midpoint_reply()])
        reply, _execution = self._obtain(
            self._cache(), self._native(opener))
        self.assertIsNotNone(reply)
        blob = json.dumps(
            [json.loads(path.read_text(encoding="utf-8"))
             for path in self._captures()])
        self.assertNotIn("sk-secret-123", blob)
        self.assertNotIn("Authorization", blob)
        self.assertNotIn("X-Upstream-Secret", blob)


class SessionCancellationTests(AutomaticRetryTests):
    """Per-session cancellation inside session_scope: a failed replay
    case stops future attempts for its session only."""

    def _real_cache(self, **kwargs):
        kwargs.setdefault("automatic_retries", True)
        return CaptureCache(self.root, None, **kwargs)

    def _records(self):
        return sorted(
            (json.loads(path.read_text(encoding="utf-8"))
             for path in self._captures()),
            key=lambda record: record.get("chain_attempt", 0))

    def test_scoped_retry_success_correlated_and_isolated(self):
        # a: timeout then success under one scope; context is attached
        # to both attempts, does not leak, and a healthy session with
        # the same payload still makes its own call.
        opener = SequenceOpener([TimeoutError("net"), _midpoint_reply()])
        cache = self._real_cache(reuse_captures=False)
        native = self._native(opener)
        with cache.session_scope("sess-a", replay_id="run-1",
                                 cancel_event=threading.Event()):
            reply, execution = self._obtain(cache, native)
        self.assertIsNotNone(reply)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(execution["automatic_retry"], {
            "attempts": 2, "max_attempts": AUTOMATIC_RETRY_MAX,
            "stop_reason": "success"})
        self.assertEqual(execution["chain_attempt"], 2)
        self.assertEqual(execution["call_context"],
                         {"session_id": "sess-a", "replay_id": "run-1"})
        records = self._records()
        self.assertEqual([r["chain_attempt"] for r in records], [1, 2])
        self.assertEqual(len({r["call_number"] for r in records}), 2)
        self.assertTrue(all(
            r["call_context"] == {"session_id": "sess-a",
                                  "replay_id": "run-1"}
            for r in records))
        # No context leak after the manager exits or into other threads.
        self.assertIsNone(cache._call_context.get())
        leaked = []
        thread = threading.Thread(
            target=lambda: leaked.append(cache._call_context.get()))
        thread.start()
        thread.join(timeout=10)
        self.assertEqual(leaked, [None])
        # Healthy session B, same payload, fresh calls: executes its own
        # callback under its own context — never deduplicated across
        # sessions.
        opener_b = SequenceOpener([_midpoint_reply()])
        native_b = self._native(opener_b)
        with cache.session_scope("sess-b", replay_id="run-1",
                                 cancel_event=threading.Event()):
            reply_b, execution_b = self._obtain(cache, native_b)
        self.assertIsNotNone(reply_b)
        self.assertEqual(len(opener_b.calls), 1)
        self.assertEqual(execution_b["call_context"],
                         {"session_id": "sess-b", "replay_id": "run-1"})
        self.assertEqual(execution_b["automatic_retry"]["attempts"], 1)

    def test_scoped_pre_cancelled_never_calls(self):
        # b: session already cancelled before obtain: zero callbacks,
        # zero budget, no capture files.
        opener = SequenceOpener([_midpoint_reply()])
        cache = CaptureCache(self.root, 5, automatic_retries=True)
        event = threading.Event()
        event.set()
        with cache.session_scope("sess-a", replay_id="run-1",
                                 cancel_event=event):
            reply, execution = self._obtain(cache, self._native(opener))
        self.assertIsNone(reply)
        self.assertEqual(len(opener.calls), 0)
        self.assertEqual(cache.new_calls, 0)
        self.assertEqual(self._captures(), [])
        self.assertEqual(execution["status"], "cancelled")
        self.assertEqual(execution["cancellation_reason"],
                         "replay_case_failed")
        self.assertEqual(execution["automatic_retry"], {
            "attempts": 0, "max_attempts": AUTOMATIC_RETRY_MAX,
            "stop_reason": "session_cancelled"})

    def test_scoped_cancel_on_first_failure_stops_chain(self):
        # c: first attempt fails retryable while the session cancels;
        # no retry runs, the failure capture is retained.
        event = threading.Event()

        class CancellingOpener:
            def __init__(self):
                self.calls = []

            def open(self, request, timeout=None):
                self.calls.append(request)
                event.set()
                raise TimeoutError("net")

        opener = CancellingOpener()
        cache = self._real_cache()
        with cache.session_scope("sess-a", cancel_event=event):
            reply, execution = self._obtain(cache, self._native(opener))
        self.assertIsNone(reply)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(len(self._captures()), 1)
        self.assertEqual(execution["status"], "cancelled")
        self.assertEqual(execution["cancellation_reason"],
                         "replay_case_failed")
        self.assertEqual(execution["automatic_retry"]["attempts"], 1)
        self.assertEqual(execution["automatic_retry"]["stop_reason"],
                         "session_cancelled")
        self.assertFalse(self._records()[0]["ok"])

    def test_scoped_cancel_during_backoff_joins_promptly(self):
        # d: cancelled while waiting out a long backoff; the worker
        # wakes within the 50ms slice and exits.
        opener = SequenceOpener([TimeoutError("n"), _midpoint_reply()])
        cache = self._real_cache(reuse_captures=False)
        cache._retry_delay = lambda attempts, execution: 10.0
        event = threading.Event()
        results = {}

        def worker():
            with cache.session_scope("sess-a", replay_id="run-1",
                                     cancel_event=event):
                results["a"] = self._obtain(cache, self._native(opener))

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self._captures():
            time.sleep(0.02)
        self.assertTrue(self._captures())
        event.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        reply, execution = results["a"]
        self.assertIsNone(reply)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(execution["automatic_retry"]["stop_reason"],
                         "session_cancelled")
        # A healthy parallel scope is unaffected by the cancelled one.
        healthy_opener = SequenceOpener([_midpoint_reply()])
        with cache.session_scope("sess-b",
                                 cancel_event=threading.Event()):
            reply_b, _ = self._obtain(cache, self._native(healthy_opener))
        self.assertIsNotNone(reply_b)
        self.assertEqual(len(healthy_opener.calls), 1)

    def test_scoped_waiter_cancelled_behind_same_key_lock(self):
        # e: B queued behind A on the same request key is cancelled
        # while waiting; when A releases, B never runs a callback and
        # never borrows A's execution.
        entered = threading.Event()
        release = threading.Event()

        class BlockingOpener:
            def __init__(self):
                self.calls = []

            def open(self, request, timeout=None):
                self.calls.append(request)
                entered.set()
                release.wait(timeout=30)
                return FakeResponse(_midpoint_reply())

        opener = BlockingOpener()
        native = self._native(opener)
        cache = CaptureCache(self.root, 5, automatic_retries=True)
        event_a, event_b = threading.Event(), threading.Event()
        results = {}

        def worker(tag, sid, event):
            with cache.session_scope(sid, replay_id="run-1",
                                     cancel_event=event):
                results[tag] = self._obtain(cache, native)

        thread_a = threading.Thread(
            target=worker, args=("a", "sess-a", event_a))
        thread_a.start()
        self.assertTrue(entered.wait(timeout=10))
        thread_b = threading.Thread(
            target=worker, args=("b", "sess-b", event_b))
        thread_b.start()
        time.sleep(0.1)
        event_b.set()
        release.set()
        thread_a.join(timeout=30)
        thread_b.join(timeout=30)
        self.assertFalse(thread_a.is_alive() or thread_b.is_alive())
        reply_a, execution_a = results["a"]
        reply_b, execution_b = results["b"]
        self.assertIsNotNone(reply_a)
        self.assertIsNone(reply_b)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(cache.new_calls, 1)
        self.assertEqual(execution_b["status"], "cancelled")
        self.assertEqual(execution_b["automatic_retry"]["attempts"], 0)
        self.assertNotIn("call_number", execution_b)
        self.assertEqual(execution_b["call_context"]["session_id"],
                         "sess-b")
        self.assertNotEqual(execution_a.get("call_number"),
                            execution_b.get("call_number"))


class RetryAfterParsingTests(unittest.TestCase):
    """Malformed external Retry-After values never break telemetry."""

    def test_invalid_and_out_of_range_values(self):
        for value in ("garbage", "-5", "nan", "inf", "1e999",
                      "Mon, 99 Jan 30000 99:99:99 GMT"):
            with self.subTest(value=value):
                exc = _http(503, retry_after=value)
                self.assertIsNone(_retry_after_seconds(exc))
        self.assertIsNone(_retry_after_seconds(TimeoutError("x")))
        self.assertIsNone(_retry_after_seconds(_http(503)))


class EndVariantRetryTests(unittest.TestCase):
    """CachedFullFeedbackGenerator retry covers every end variant."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_grounded_bank()
        self.taxonomy = make_grounded_taxonomy(self.bank)
        wrong = sorted({positions(self.bank, "pct", 0)[0],
                        positions(self.bank, "alg", 0)[0]})
        self.rows = grounded_rows(self.bank, wrong)
        self.evidence = build_evidence(
            self.bank, self.taxonomy, self.rows)
        self.assessment_context = {
            "bank": self.bank, "taxonomy": self.taxonomy,
            "responses": self.rows}
        self.bundle = build_review_bundles(self.evidence)[0]
        contexts = [feedback_practice.empty_context("abstained")
                    for candidate in self.bundle["review_candidates"]
                    if candidate["focus"] is not None]
        self.multi_input = build_multi_full_input(
            self.evidence, "student", self.bundle, contexts,
            assessment_context=self.assessment_context)
        self.worked_input = build_multi_full_input(
            self.evidence, "student", self.bundle, contexts,
            assessment_context=self.assessment_context,
            worked_first_context=build_worked_first_context(
                self.evidence, self.assessment_context))
        selected = build_candidates(self.evidence, "end")[1]
        self.single_input = build_full_input(
            self.evidence, "student", selected)
        uniform = mock.patch(
            "replay_provider_scenarios.random.uniform",
            return_value=0.0)
        uniform.start()
        self.addCleanup(uniform.stop)

    def _cached(self, opener, name):
        root = Path(self.tmp.name) / name
        root.mkdir()
        cache = CaptureCache(root, None, automatic_retries=True)
        cache._stop_retries.wait = mock.Mock(return_value=False)
        native = FullFeedbackAittaGenerator(
            AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                           opener=opener))
        return (CachedFullFeedbackGenerator(native, cache, MODEL,
                                            "hash"),
                root)

    def test_each_end_variant_retries_transient_timeout(self):
        cases = {
            "single": self.single_input,
            "aligned_two_topic": self.multi_input,
            "worked_first": self.worked_input}
        for name, payload in cases.items():
            with self.subTest(variant=name):
                opener = SequenceWorkedOpener([TimeoutError("net")])
                cached, root = self._cached(opener, name)
                reply = cached.generate(copy.deepcopy(payload))
                self.assertEqual(len(opener.calls), 2)
                self.assertEqual(cached.cache.new_calls, 2)
                retry = cached.execution["automatic_retry"]
                self.assertEqual(retry["stop_reason"], "success")
                self.assertEqual(retry["attempts"], 2)
                self.assertEqual(
                    len(list(root.glob("aitta_full_*.json"))), 2)
                self.assertEqual(reply["sections"],
                                 dict(VALID_SECTIONS))
                if name == "worked_first":
                    ready = [e["question_id"] for e in
                             payload["review_examples"]
                             if e["status"] == "ready"]
                    reviews = reply["worked_reviews"]
                    self.assertEqual(
                        [w["question_id"] for w in reviews], ready)
                    self.assertEqual(reviews[0]["steps"],
                                     list(WF_AUTHORED[0][0]))


class HostedFactoryRetryTests(unittest.TestCase):
    """The hosted demo service opts its shared cache in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)

    def test_hosted_cache_opts_in_and_close_stops_retries(self):
        opener = SequenceOpener([_midpoint_reply()])
        inner = AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                               opener=opener)
        with mock.patch.object(
                AittaGenerator, "from_env", return_value=inner):
            service = DemoService(
                root=Path(self.tmp.name) / f"svc_{secrets.token_hex(4)}",
                bank=self.bank, taxonomy=self.taxonomy,
                diagnostics=fake_diagnostics(self.bank),
                provider_mode="hosted")
        self.addCleanup(service.close, wait=True)
        self.assertTrue(service.cache._automatic_retries)
        self.assertFalse(service.cache._reuse_captures)
        midpoint = service.midpoint_generator
        end = service.generator
        self.assertIsInstance(midpoint, CachedMidpointGenerator)
        self.assertIsInstance(end, CachedFullFeedbackGenerator)
        self.assertIsInstance(end.native, FullFeedbackAittaGenerator)
        self.assertIs(midpoint.cache, service.cache)
        self.assertIs(end.cache, service.cache)
        self.assertFalse(service.cache._stop_retries.is_set())
        service.close(wait=True)
        self.assertTrue(service.cache._stop_retries.is_set())

    def test_hosted_generators_call_fresh_on_identical_payloads(self):
        mid_evidence = build_evidence(
            self.bank, self.taxonomy,
            midpoint_rows(self.bank, wrong_positions=(0, 5)))
        end_evidence = build_evidence(
            self.bank, self.taxonomy,
            grounded_rows(self.bank, wrong_positions=(0, 5)))
        mid_input = build_midpoint_input(mid_evidence)
        end_input = build_full_input(
            end_evidence, "student",
            build_candidates(end_evidence, "end")[1])
        end_reply = wire_reply(
            candidate_id=end_input["selected_candidate"][
                "candidate_id"],
            sections=dict(VALID_SECTIONS))
        opener = SequenceOpener([_midpoint_reply(), _midpoint_reply(),
                               end_reply, end_reply])
        inner = AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                               opener=opener)
        with mock.patch.object(
                AittaGenerator, "from_env", return_value=inner):
            service = DemoService(
                root=Path(self.tmp.name) / f"svc_{secrets.token_hex(4)}",
                bank=self.bank, taxonomy=self.taxonomy,
                diagnostics=fake_diagnostics(self.bank),
                provider_mode="hosted")
        self.addCleanup(service.close, wait=True)
        self.assertFalse(service.cache._reuse_captures)
        midpoint = service.midpoint_generator
        midpoint.generate(copy.deepcopy(mid_input))
        first = copy.deepcopy(midpoint.execution)
        midpoint.generate(copy.deepcopy(mid_input))
        second = copy.deepcopy(midpoint.execution)
        self.assertFalse(first["reused"])
        self.assertFalse(second["reused"])
        self.assertNotEqual(first["capture_file"],
                            second["capture_file"])
        end = service.generator
        end.generate(copy.deepcopy(end_input))
        third = copy.deepcopy(end.execution)
        end.generate(copy.deepcopy(end_input))
        fourth = copy.deepcopy(end.execution)
        self.assertFalse(third["reused"])
        self.assertFalse(fourth["reused"])
        self.assertNotEqual(third["capture_file"],
                            fourth["capture_file"])
        self.assertEqual(len(opener.calls), 4)
        self.assertEqual(service.cache.new_calls, 4)


if __name__ == "__main__":
    unittest.main()
