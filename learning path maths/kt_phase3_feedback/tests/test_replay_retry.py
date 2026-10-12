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
        with self.assertRaises(SystemExit):
            with cache.retry_failed_scope(True):
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
        with self.assertRaises(RuntimeError):
            with self.cache.retry_failed_scope(True):
                raise RuntimeError("boom")
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
        with self.assertRaises(ValueError):
            with self.cache.retry_failed_scope(True):
                _obtain(self.cache, calls)
        self.assertEqual(calls, [1])
        self.assertEqual(self.cache.new_calls, 1)


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
        reply = {"choices": [{"finish_reason": "stop",
                              "message": {"role": "assistant",
                                          "content": json.dumps(
                                              {"candidate_id": candidate_id,
                                               "sections": dict(
                                                   VALID_SECTIONS)})}}],
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
        self.cache = CaptureCache(self.root / "captures", None)
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

    def test_retry_flag_validation(self):
        with self.assertRaises(ValueError):
            self.service.replays.start(
                self.request(retry_failed_provider_calls="yes"))
        with self.assertRaises(ValueError):
            self.service.replays.start(
                self.request(retry_failed_provider_calls=True,
                             unknown_field=True))
        with self.assertRaises(ValueError):
            self.service.replays.start(
                self.request(provider_mode="rules",
                             allow_provider_calls=False,
                             retry_failed_provider_calls=True))
        with self.assertRaises(ValueError):
            self.service.replays.start(
                self.request(allow_provider_calls=False,
                             retry_failed_provider_calls=True))
        self.assertEqual(self.service.replays.list()["runs"], [])

    def test_legacy_five_field_request_still_works(self):
        run = self.service.replays.start(self.request())
        record = self.service.replays.get(run["id"])
        self.assertFalse(record.get("retry_failed_provider_calls", False))

    def test_retry_flag_persisted_and_idempotent(self):
        request = self.request(retry_failed_provider_calls=True)
        run = self.service.replays.start(request)
        self.assertTrue(run["retry_failed_provider_calls"])
        again = self.service.replays.start(request)
        self.assertEqual(again["id"], run["id"])
        self.assertEqual(len(self.service.replays.list()["runs"]), 1)
        with self.assertRaises(ResearchConflict):
            self.service.replays.start(
                dict(request, retry_failed_provider_calls=False))
        self.assertEqual(len(self.service.replays.list()["runs"]), 1)

    def test_failed_call_retried_on_next_flagged_run(self):
        self.opener.failures = 1
        request_one = self.request(retry_failed_provider_calls=True)
        first = self.wait(
            self.service.replays.start(request_one)["id"])
        self.assertEqual(first["cases"][0]["provider_status"], "fallback")
        self.assertEqual(len(self.opener.calls), 1)
        captures = self.root / "captures"
        base_files = [p for p in captures.glob("aitta_full_*.json")
                      if "_attempt_" not in p.name]
        self.assertEqual(len(base_files), 1)
        base_bytes = base_files[0].read_bytes()
        base_record = json.loads(base_bytes)
        self.assertFalse(base_record["ok"])
        self.assertNotIn("retry_failed_provider_calls",
                         json.dumps(base_record["request"]))
        first_run_bytes = (self.root / "runtime" / "replay_runs" /
                           f"{first['id']}.json").read_bytes()
        request_two = self.request(retry_failed_provider_calls=True)
        second = self.wait(
            self.service.replays.start(request_two)["id"])
        self.assertEqual(second["cases"][0]["provider_status"], "ready")
        self.assertEqual(len(self.opener.calls), 2)
        attempt_files = sorted(
            captures.glob("aitta_full_*_attempt_*.json"))
        self.assertEqual(len(attempt_files), 1)
        attempt = json.loads(attempt_files[0].read_text())
        self.assertTrue(attempt["ok"])
        self.assertTrue(attempt["retry_of_failed_capture"])
        self.assertEqual(attempt["attempt_number"], 2)
        self.assertEqual(attempt["previous_capture_file"],
                         base_files[0].name)
        self.assertEqual(base_files[0].read_bytes(), base_bytes)
        self.assertEqual(
            (self.root / "runtime" / "replay_runs" /
             f"{first['id']}.json").read_bytes(), first_run_bytes)
        execution = second["cases"][0]
        self.assertEqual(execution["status"], "complete")
        request_three = self.request(retry_failed_provider_calls=True)
        third = self.wait(
            self.service.replays.start(request_three)["id"])
        self.assertEqual(third["cases"][0]["provider_status"], "ready")
        self.assertEqual(len(self.opener.calls), 2)

    def test_unflagged_run_reuses_failure(self):
        self.opener.failures = 1
        first = self.wait(
            self.service.replays.start(self.request())["id"])
        self.assertEqual(first["cases"][0]["provider_status"], "fallback")
        self.assertEqual(len(self.opener.calls), 1)
        second = self.wait(
            self.service.replays.start(self.request())["id"])
        self.assertEqual(second["cases"][0]["provider_status"], "fallback")
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(
            list((self.root / "captures").glob("*_attempt_*.json")), [])

    def test_legacy_single_worker_uses_retry_scope(self):
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

        first = wait(legacy.replays.start(
            self.request(retry_failed_provider_calls=True))["id"])
        self.assertEqual(first["cases"][0]["provider_status"], "fallback")
        self.assertEqual(len(self.opener.calls), 1)
        second = wait(legacy.replays.start(
            self.request(retry_failed_provider_calls=True))["id"])
        self.assertEqual(second["cases"][0]["provider_status"], "ready")
        self.assertEqual(len(self.opener.calls), 2)
        attempts = list((self.root / "captures").glob(
            "aitta_full_*_attempt_*.json"))
        self.assertEqual(len(attempts), 1)

    def test_rules_replay_never_retries(self):
        self.opener.failures = 1
        first = self.wait(
            self.service.replays.start(self.request())["id"])
        self.assertEqual(len(self.opener.calls), 1)
        self.cache.new_calls = 0
        rules = self.wait(self.service.replays.start(
            self.request(provider_mode="rules",
                         allow_provider_calls=False))["id"])
        self.assertEqual(rules["status"], "complete")
        self.assertEqual(len(self.opener.calls), 1)


if __name__ == "__main__":
    unittest.main()
