"""Tests for the hosted Jev selector adapter (fully mocked, no network)."""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import jev_selector
import synthetic_feedback_demo
from jev_selector import (
    CRITERIA, DEFAULT_ENV_PATH, ENDPOINT, ENV_KEY, MODEL, QUESTION_ID,
    SELECTION_INSTRUCTIONS, SELECTION_PROMPT_VERSION, JevSelectionError,
    JevSelector, _NoRedirectHandler)
from synthetic_feedback import run_synthetic_feedback
from synthetic_feedback_demo import END_SKILLS, MIDPOINT_SKILLS
from tests.test_synthetic_feedback import (
    RecordingGenerator, make_input, valid_opening)

FAKE_KEY = "fake-token-xyz"


def ok_body(choice="observed_summary", model="jev-2026-10-02",
            usage=None):
    if usage is None:
        usage = {"input_tokens": 120, "output_tokens": 8}
    return json.dumps({
        "model": model,
        "answers": {QUESTION_ID: {
            "type": "choice", "choice": choice,
            "probabilities": {choice: 0.7, "other": 0.3},
            "confidence": 0.7}},
        "usage": usage,
        "extra_provider_field": {"ignored": True},
    }).encode("utf-8")


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self, limit=-1):
        self.read_limit = limit
        return self.body

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, body=b"{}", error=None):
        self.body = body
        self.error = error
        self.requests = []
        self.timeouts = []
        self.responses = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        response = FakeResponse(self.body)
        self.responses.append(response)
        return response


def selection_payload(audience="student", checkpoint="end"):
    """Capture the exact selection payload the runner would send."""
    rows = MIDPOINT_SKILLS if checkpoint == "midpoint" else END_SKILLS
    evidence = {"schema": "phase3_synthetic_feedback_input_v1",
                "data_origin": "synthetic", "audience": audience,
                "checkpoint": checkpoint,
                "skills": copy.deepcopy(rows)}
    captured = {}

    class Recorder:
        def select(self, payload):
            captured["payload"] = payload
            return {"candidate_id":
                    payload["candidates"][0]["candidate_id"]}

    run_synthetic_feedback(evidence, Recorder())
    return captured["payload"]


def jev(opener=None, timeout=12.5):
    return JevSelector(FAKE_KEY, timeout=timeout,
                       opener=opener if opener is not None
                       else FakeOpener(ok_body()))


class RequestShapeTests(unittest.TestCase):
    def test_posts_exact_contract(self):
        opener = FakeOpener(ok_body())
        result = jev(opener).select(selection_payload("student", "end"))
        self.assertEqual(result, {"candidate_id": "observed_summary"})
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"),
                         f"Bearer {FAKE_KEY}")
        self.assertEqual(request.get_header("Content-type"),
                         "application/json")
        self.assertEqual(opener.timeouts[0], 12.5)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(set(body["state"]),
                         {"audience", "checkpoint", "evidence",
                          "candidates"})
        payload = selection_payload("teacher", "end")
        self.assertEqual(body["state"]["audience"], "student")
        self.assertEqual(body["state"]["checkpoint"], "end")
        self.assertEqual(body["state"]["evidence"],
                         payload["evidence"])
        self.assertEqual(body["state"]["candidates"],
                         payload["candidates"])
        self.assertEqual(body["questions"], {QUESTION_ID: {
            "type": "choice",
            "instructions": SELECTION_INSTRUCTIONS,
            "criteria": {"observed_summary": CRITERIA["observed_summary"],
                         "neutral": CRITERIA["neutral"]}}})
        self.assertNotIn(SELECTION_PROMPT_VERSION,
                         json.dumps(body))

    def test_request_carries_no_private_or_extra_fields(self):
        opener = FakeOpener(ok_body())
        jev(opener).select(selection_payload("teacher", "end"))
        blob = json.dumps(
            json.loads(opener.requests[0].data.decode("utf-8")))
        for marker in ("answer_index", "question_id", "session_id",
                       "selected_index", "prompt_version",
                       "data_origin", "schema"):
            self.assertNotIn(marker, blob)

    def test_success_neutral_choice(self):
        opener = FakeOpener(ok_body(choice="neutral"))
        result = jev(opener).select(selection_payload("student", "end"))
        self.assertEqual(result, {"candidate_id": "neutral"})
        self.assertNotIn("probabilities", json.dumps(result))

    def test_midpoint_resolves_without_request(self):
        opener = FakeOpener(ok_body())
        selector = jev(opener)
        result = selector.select(
            selection_payload("student", "midpoint"))
        self.assertEqual(result, {"candidate_id": "neutral"})
        self.assertEqual(opener.requests, [])
        self.assertEqual(selector.last_metadata, {
            "status": "not_called_single_candidate",
            "model_version": None, "usage": None})

    def test_probabilities_and_confidence_are_ignored(self):
        opener = FakeOpener(ok_body())
        selector = jev(opener)
        selector.select(selection_payload("student", "end"))
        self.assertNotIn("probabilities",
                         json.dumps(selector.last_metadata))
        self.assertNotIn("confidence",
                         json.dumps(selector.last_metadata))


class PayloadValidationTests(unittest.TestCase):
    def assert_rejected_without_request(self, payload):
        opener = FakeOpener(ok_body())
        selector = jev(opener)
        with self.assertRaises(ValueError):
            selector.select(payload)
        self.assertEqual(opener.requests, [])
        self.assertEqual(selector.last_metadata["status"],
                         "not_called")

    def test_rejects_bad_envelope(self):
        for bad in (None, "payload", [], {},
                    {**selection_payload(), "session_id": "s1"},
                    {k: v for k, v in selection_payload().items()
                     if k != "candidates"},
                    {**selection_payload(), "schema": "wrong"},
                    {**selection_payload(), "audience": "admin"},
                    {**selection_payload(), "checkpoint": "start"}):
            with self.subTest(bad=bad):
                self.assert_rejected_without_request(bad)

    def test_rejects_teacher_midpoint(self):
        payload = selection_payload("student", "midpoint")
        payload["audience"] = "teacher"
        self.assert_rejected_without_request(payload)

    def test_rejects_midpoint_with_evidence_or_candidates(self):
        payload = selection_payload("student", "midpoint")
        payload["evidence"] = {"skills": []}
        self.assert_rejected_without_request(payload)
        payload = selection_payload("student", "midpoint")
        payload["candidates"].append({"candidate_id": "extra",
                                      "strategy": "x",
                                      "review_status": "x",
                                      "permitted_evidence": []})
        self.assert_rejected_without_request(payload)
        payload = selection_payload("student", "midpoint")
        payload["candidates"][0]["strategy"] = "tampered"
        self.assert_rejected_without_request(payload)

    def test_rejects_private_fields_in_end_evidence(self):
        payload = selection_payload("student", "end")
        payload["evidence"]["answer_index"] = 0
        self.assert_rejected_without_request(payload)
        payload = selection_payload("student", "end")
        payload["evidence"]["skills"][0]["question_id"] = "q1"
        self.assert_rejected_without_request(payload)
        payload = selection_payload("student", "end")
        payload["evidence"]["skills"][0]["correct"] = True
        self.assert_rejected_without_request(payload)

    def test_rejects_tampered_total(self):
        payload = selection_payload("student", "end")
        payload["evidence"]["total"]["correct"] = 28
        self.assert_rejected_without_request(payload)
        payload = selection_payload("student", "end")
        payload["evidence"]["total"]["note"] = "extra"
        self.assert_rejected_without_request(payload)

    def test_rejects_bool_total_that_would_equal_int(self):
        # True == 1 in Python; the strict type check must still refuse.
        payload = selection_payload("student", "end")
        for index, row in enumerate(
                payload["evidence"]["skills"]):
            row["correct"] = 1 if index == 0 else 0
        payload["evidence"]["total"] = {"correct": True, "out_of": 40}
        self.assert_rejected_without_request(payload)

    def test_rejects_tampered_end_candidates(self):
        payload = selection_payload("student", "end")
        payload["candidates"][0]["permitted_evidence"] = ["total"]
        self.assert_rejected_without_request(payload)
        payload = selection_payload("student", "end")
        payload["candidates"] = payload["candidates"][::-1]
        self.assert_rejected_without_request(payload)


class FailureAndMetadataTests(unittest.TestCase):
    def test_failures_sanitize_and_fall_back(self):
        cases = {
            "http_error": urllib.error.HTTPError(
                ENDPOINT, 401, f"denied {FAKE_KEY}", {}, None),
            "url_error": urllib.error.URLError("connection refused"),
            "timeout": TimeoutError(f"slow {FAKE_KEY}"),
            "os_error": OSError("network unreachable"),
            "arbitrary": RuntimeError(f"boom {FAKE_KEY}"),
        }
        for name, error in cases.items():
            with self.subTest(name=name):
                opener = FakeOpener(error=error)
                selector = jev(opener)
                with self.assertRaises(JevSelectionError) as ctx:
                    selector.select(selection_payload())
                self.assertEqual(str(ctx.exception),
                                 "Jev request failed")
                self.assertNotIn(FAKE_KEY, repr(ctx.exception))
                self.assertIsNone(ctx.exception.__cause__)
                self.assertEqual(selector.last_metadata, {
                    "status": "failed", "model_version": None,
                    "usage": None})

    def test_bad_bodies_fail(self):
        cases = {
            "bad_utf8": b"\xff\xfe\xfa\xfb",
            "bad_json": b"this is not json",
            "overlarge": b"x" * (1024 * 1024 + 5),
            "not_dict": b"[1, 2]",
            "missing_answers": b"{}",
            "wrong_type": ok_body().replace(
                b'"type": "choice"', b'"type": "text"'),
            "unknown_choice": ok_body(choice="unlisted_candidate"),
            "non_str_choice": ok_body().replace(
                b'"choice": "observed_summary"', b'"choice": 5'),
            "missing_model": ok_body().replace(
                b'"model": "jev-2026-10-02",', b""),
            "empty_model": ok_body(model=""),
            "long_model": ok_body(model="m" * 101),
            "missing_usage": json.dumps({
                "model": "jev-2026-10-02",
                "answers": {QUESTION_ID: {"type": "choice",
                                          "choice": "neutral"}}
            }).encode("utf-8"),
            "bool_usage": ok_body(usage={"input_tokens": True,
                                         "output_tokens": 0}),
            "negative_usage": ok_body(usage={"input_tokens": -1,
                                             "output_tokens": 0}),
            "extra_usage": ok_body(usage={"input_tokens": 1,
                                          "output_tokens": 0,
                                          "cost": 1}),
            "float_usage": ok_body(usage={"input_tokens": 1.5,
                                          "output_tokens": 0}),
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                opener = FakeOpener(body)
                selector = jev(opener)
                with self.assertRaises(JevSelectionError):
                    selector.select(selection_payload())
                self.assertEqual(selector.last_metadata["status"],
                                 "failed")

    def test_runner_fallback_skips_generator(self):
        generator = RecordingGenerator(valid_opening)
        result = run_synthetic_feedback(
            make_input("student", "end"),
            jev(FakeOpener(error=RuntimeError(f"leak {FAKE_KEY}"))),
            generator)
        self.assertEqual(result["trace"]["fallback_reason"],
                         "selector_error")
        self.assertEqual(result["trace"]["phrasing_source"],
                         "deterministic")
        self.assertEqual(generator.payloads, [])
        self.assertNotIn(FAKE_KEY, json.dumps(result))
        self.assertEqual(result["message"]["opening"],
                         "You have completed this assessment.")

    def test_metadata_lifecycle_and_reset(self):
        opener = FakeOpener(ok_body())
        selector = jev(opener)
        self.assertEqual(selector.last_metadata, {
            "status": "not_called", "model_version": None,
            "usage": None})
        selector.select(selection_payload("student", "end"))
        self.assertEqual(selector.last_metadata, {
            "status": "completed", "model_version": "jev-2026-10-02",
            "usage": {"input_tokens": 120, "output_tokens": 8}})
        selector._opener = FakeOpener(error=RuntimeError("x"))
        with self.assertRaises(JevSelectionError):
            selector.select(selection_payload())
        self.assertEqual(selector.last_metadata, {
            "status": "failed", "model_version": None, "usage": None})
        selector.select(selection_payload("student", "midpoint"))
        self.assertEqual(selector.last_metadata["status"],
                         "not_called_single_candidate")

    def test_no_redirects_by_default(self):
        handler = _NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(
            None, None, 302, "Found", {}, "https://evil.example/"))
        default = JevSelector(FAKE_KEY)
        self.assertTrue(any(isinstance(h, _NoRedirectHandler)
                            for h in default._opener.handlers))


class ConstructorAndEnvTests(unittest.TestCase):
    def test_key_validation(self):
        for bad in (None, 42, "", "   ", "has space", "tab\tkey",
                    "new\nline", "caf\xe9", "paste-your-key-here",
                    "Paste-Your-Token"):
            with self.subTest(key=bad):
                with self.assertRaises(ValueError) as ctx:
                    JevSelector(bad)
                if isinstance(bad, str) and bad:
                    self.assertNotIn(bad, str(ctx.exception))

    def test_timeout_validation(self):
        for bad in (True, 0, -1, 0.0, float("nan"), float("inf"),
                    "30", None):
            with self.subTest(timeout=bad):
                with self.assertRaises(ValueError):
                    JevSelector(FAKE_KEY, timeout=bad)

    def test_repr_and_metadata_hide_key(self):
        selector = JevSelector(FAKE_KEY)
        self.assertNotIn(FAKE_KEY, repr(selector))
        self.assertNotIn(FAKE_KEY, json.dumps(selector.last_metadata))

    def write_env(self, tmp, text):
        path = Path(tmp) / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_from_env_file_and_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_env(
                tmp, "# comment\n\nTYPESAFE_API_KEY=fake-file-token\n")
            with mock.patch.dict(os.environ):
                os.environ.pop(ENV_KEY, None)
                selector = JevSelector.from_env(path)
                self.assertEqual(selector._api_key, "fake-file-token")
                self.assertEqual(selector._timeout, 30.0)
            with mock.patch.dict(os.environ, {ENV_KEY: "env-token"}):
                selector = JevSelector.from_env(path)
                self.assertEqual(selector._api_key, "env-token")
            path.write_bytes(
                b'\xef\xbb\xbfTYPESAFE_API_KEY="quoted-token"\n')
            with mock.patch.dict(os.environ):
                os.environ.pop(ENV_KEY, None)
                selector = JevSelector.from_env(path)
                self.assertEqual(selector._api_key, "quoted-token")
            self.write_env(tmp, "TYPESAFE_API_KEY='single'\n")
            with mock.patch.dict(os.environ):
                os.environ.pop(ENV_KEY, None)
                selector = JevSelector.from_env(path)
                self.assertEqual(selector._api_key, "single")

    def test_from_env_invalid_utf8_file_is_generic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_bytes(b"\xff\xfe not utf8 TYPESAFE_API_KEY=\xff")
            with mock.patch.dict(os.environ):
                os.environ.pop(ENV_KEY, None)
                with self.assertRaises(ValueError) as ctx:
                    JevSelector.from_env(path)
                self.assertEqual(
                    str(ctx.exception),
                    "Set TYPESAFE_API_KEY in the environment or the "
                    "local Phase 3 .env file")
                self.assertNotIn("ff", repr(ctx.exception))

    def test_from_env_rejects_bad_files_generically(self):
        cases = ("", "# only comments\n", "OTHER_KEY=x\n",
                 "TYPESAFE_API_KEY=a\nTYPESAFE_API_KEY=b\n",
                 "TYPESAFE_API_KEY=\n", "no-equals-line\n",
                 "export TYPESAFE_API_KEY=x\n",
                 "TYPESAFE_API_KEY=x\nOTHER=y\n")
        with tempfile.TemporaryDirectory() as tmp:
            for text in cases:
                path = self.write_env(tmp, text)
                with self.subTest(text=text):
                    with mock.patch.dict(os.environ):
                        os.environ.pop(ENV_KEY, None)
                        with self.assertRaises(ValueError) as ctx:
                            JevSelector.from_env(path)
                        self.assertEqual(
                            str(ctx.exception),
                            "Set TYPESAFE_API_KEY in the environment "
                            "or the local Phase 3 .env file")
            with mock.patch.dict(os.environ):
                os.environ.pop(ENV_KEY, None)
                with self.assertRaises(ValueError) as ctx:
                    JevSelector.from_env(Path(tmp) / "missing.env")
                self.assertIn("TYPESAFE_API_KEY", str(ctx.exception))

    def test_env_var_invalid_value_raises_not_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_env(tmp, "TYPESAFE_API_KEY=file-token\n")
            with mock.patch.dict(os.environ,
                                 {ENV_KEY: "paste-your-key"}):
                with self.assertRaises(ValueError):
                    JevSelector.from_env(path)

    def test_default_env_path_is_module_local(self):
        self.assertEqual(DEFAULT_ENV_PATH.name, ".env")
        self.assertEqual(DEFAULT_ENV_PATH.parent,
                         Path(jev_selector.__file__).resolve().parent)


class DemoJevModeTests(unittest.TestCase):
    def test_jev_mode_with_patched_from_env(self):
        opener = FakeOpener(ok_body())
        selector = JevSelector(FAKE_KEY, opener=opener)
        with mock.patch.object(JevSelector, "from_env",
                               return_value=selector):
            argv = ["synthetic_feedback_demo.py", "--jev"]
            with mock.patch.object(sys, "argv", argv):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    synthetic_feedback_demo.main()
        output = buffer.getvalue()
        data = json.loads(output)
        self.assertEqual(data["mode"], "jev_selector")
        self.assertEqual(len(data["packages"]), 3)
        self.assertEqual(len(opener.requests), 2)
        self.assertNotIn(FAKE_KEY, output)
        for package in data["packages"]:
            metadata = package["selector_metadata"]
            self.assertEqual(metadata["prompt_version"],
                             "jev_selection_v1")
        statuses = {p["case"]: p["selector_metadata"]["status"]
                    for p in data["packages"]}
        self.assertEqual(statuses["student_midpoint"],
                         "not_called_single_candidate")
        self.assertEqual(statuses["student_end"], "completed")
        self.assertEqual(statuses["teacher_end"], "completed")

    def test_jev_and_mock_are_mutually_exclusive(self):
        argv = ["synthetic_feedback_demo.py", "--mock", "--jev"]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit):
                synthetic_feedback_demo.main()

    def test_jev_missing_key_is_clean_error(self):
        with mock.patch.object(JevSelector, "from_env",
                               side_effect=ValueError("nope")):
            argv = ["synthetic_feedback_demo.py", "--jev"]
            with mock.patch.object(sys, "argv", argv):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit):
                        synthetic_feedback_demo.main()
        self.assertIn("TYPESAFE_API_KEY", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
