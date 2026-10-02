"""Tests for the hosted Aitta generator adapter (fully mocked)."""
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

import aitta_generator
import synthetic_feedback_demo
from aitta_generator import (
    AittaGenerationError, AittaGenerator, DEFAULT_ENV_PATH,
    DEFAULT_MODEL, ENV_BASE_URL, ENV_KEY, ENV_MODEL)
from jev_selector import (
    JevSelector, SELECTION_PROMPT_VERSION, _NoRedirectHandler)
from synthetic_feedback import GENERATION_INSTRUCTIONS, PROMPT_VERSION
from synthetic_feedback import run_synthetic_feedback
from synthetic_feedback_demo import END_SKILLS, MIDPOINT_SKILLS
from tests.test_jev_selector import FakeOpener, FakeResponse
from tests.test_synthetic_feedback import make_input, valid_opening

FAKE_KEY = "fake-aitta-token-xyz"
BASE_URL = "https://aitta.example/v1"
ENDPOINT_URL = BASE_URL + "/chat/completions"


def ok_body(opening="Thank you for completing this assessment.",
            candidate="observed_summary", model="openai/gpt-oss-120b",
            finish="stop", usage=None, choices=None):
    inner = json.dumps({"candidate_id": candidate, "opening": opening})
    if usage is None:
        usage = {"prompt_tokens": 40, "completion_tokens": 9,
                 "total_tokens": 49, "provider_extra": "ignored"}
    if choices is None:
        choices = [{"finish_reason": finish,
                    "message": {"role": "assistant",
                                "content": inner}}]
    return json.dumps({"model": model, "choices": choices,
                       "usage": usage,
                       "extra_provider_field": "ignored"}).encode("utf-8")


def generation_payload(audience="student", checkpoint="end",
                       candidate_id=None):
    """Capture the exact generation payload the runner would send."""
    rows = MIDPOINT_SKILLS if checkpoint == "midpoint" else END_SKILLS
    evidence = {"schema": "phase3_synthetic_feedback_input_v1",
                "data_origin": "synthetic", "audience": audience,
                "checkpoint": checkpoint,
                "skills": copy.deepcopy(rows)}
    captured = {}

    class Selector:
        def select(self, payload):
            cid = (candidate_id
                   or payload["candidates"][0]["candidate_id"])
            return {"candidate_id": cid}

    class Recorder:
        def generate(self, payload):
            captured["payload"] = payload
            return {"candidate_id": payload["selected_candidate"]
                    ["candidate_id"], "opening": "Ok."}

    run_synthetic_feedback(evidence, Selector(), Recorder())
    return captured["payload"]


class ContextOpener(FakeOpener):
    """Fake opener that echoes the requested selected_candidate id."""

    def open(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        context = json.loads(body["messages"][1]["content"])
        self.requests.append(request)
        self.timeouts.append(timeout)
        return FakeResponse(ok_body(
            candidate=context["selected_candidate"]["candidate_id"]))


def aitta(opener=None, base_url=BASE_URL, timeout=12.5):
    return AittaGenerator(FAKE_KEY, base_url, timeout=timeout,
                          opener=opener
                          if opener is not None
                          else FakeOpener(ok_body()))


class RequestShapeTests(unittest.TestCase):
    def test_posts_exact_contract(self):
        opener = FakeOpener(ok_body())
        result = aitta(opener).generate(
            generation_payload("student", "end"))
        self.assertEqual(result, {
            "candidate_id": "observed_summary",
            "opening": "Thank you for completing this assessment."})
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(request.full_url, ENDPOINT_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"),
                         f"Bearer {FAKE_KEY}")
        self.assertEqual(request.get_header("Content-type"),
                         "application/json")
        self.assertEqual(opener.timeouts[0], 12.5)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(set(body), {"model", "messages", "max_tokens",
                                     "reasoning_effort",
                                     "response_format"})
        self.assertEqual(body["model"], DEFAULT_MODEL)
        self.assertEqual(body["max_tokens"], 1024)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["response_format"],
                         {"type": "json_object"})
        self.assertEqual(len(body["messages"]), 2)
        self.assertEqual(body["messages"][0],
                         {"role": "system",
                          "content": GENERATION_INSTRUCTIONS})
        user = body["messages"][1]
        self.assertEqual(user["role"], "user")
        self.assertEqual(json.loads(user["content"]), {
            "audience": "student", "checkpoint": "end",
            "selected_candidate": {
                "candidate_id": "observed_summary",
                "strategy": "describe_observed_counts",
                "review_status": "draft_pending_educator_review"}})

    def test_wire_sends_no_evidence_or_private_fields(self):
        opener = FakeOpener(ok_body())
        aitta(opener).generate(generation_payload("teacher", "end"))
        blob = opener.requests[0].data.decode("utf-8")
        for marker in ("skill_a", "skill_b", "Arithmetic", "Prices",
                       "Fractions", "Percentages", "correct", "out_of",
                       "permitted_evidence", "evidence", "total",
                       "prompt_version", "data_origin",
                       "answer_index", "question_id"):
            self.assertNotIn(marker, blob)
        self.assertNotIn(FAKE_KEY, blob)

    def test_all_cases_and_candidate_choices(self):
        cases = [("student", "midpoint", "neutral"),
                 ("student", "end", "observed_summary"),
                 ("student", "end", "neutral"),
                 ("teacher", "end", "observed_summary")]
        for audience, checkpoint, candidate in cases:
            with self.subTest(case=(audience, checkpoint, candidate)):
                opener = FakeOpener(ok_body(candidate=candidate))
                result = aitta(opener).generate(
                    generation_payload(audience, checkpoint,
                                       candidate))
                self.assertEqual(result["candidate_id"], candidate)
                body = json.loads(
                    opener.requests[0].data.decode("utf-8"))
                context = json.loads(body["messages"][1]["content"])
                self.assertEqual(context["audience"], audience)
                self.assertEqual(context["checkpoint"], checkpoint)
                self.assertEqual(context["selected_candidate"]
                                 ["candidate_id"], candidate)
                self.assertNotIn("permitted_evidence",
                                 json.dumps(context))

    def test_base_url_variants(self):
        for base, endpoint in (
                ("https://aitta.example", "https://aitta.example/chat/completions"),
                ("https://aitta.example/", "https://aitta.example/chat/completions"),
                ("https://aitta.example/v1", "https://aitta.example/v1/chat/completions"),
                ("https://aitta.example/v1/", "https://aitta.example/v1/chat/completions"),
                ("https://aitta.example/api/aitta/v1", "https://aitta.example/api/aitta/v1/chat/completions"),
                ("https://aitta.example/api/aitta/v1/", "https://aitta.example/api/aitta/v1/chat/completions"),
                ("https://aitta.example:8443/v1", "https://aitta.example:8443/v1/chat/completions")):
            with self.subTest(base=base):
                self.assertEqual(
                    AittaGenerator(FAKE_KEY, base)._endpoint, endpoint)

    def test_no_redirects_by_default(self):
        handler = _NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(
            None, None, 302, "Found", {}, "https://evil.example/"))
        default = AittaGenerator(FAKE_KEY, BASE_URL)
        self.assertTrue(any(isinstance(h, _NoRedirectHandler)
                            for h in default._opener.handlers))


class PayloadValidationTests(unittest.TestCase):
    def assert_rejected_without_request(self, payload):
        opener = FakeOpener(ok_body())
        generator = aitta(opener)
        with self.assertRaises(ValueError):
            generator.generate(payload)
        self.assertEqual(opener.requests, [])
        self.assertEqual(generator.last_metadata["status"],
                         "not_called")

    def test_rejects_bad_envelope(self):
        good = generation_payload()
        missing = {k: v for k, v in good.items() if k != "evidence"}
        extra = dict(good, note="x")
        for bad in (None, "payload", [], {}, missing, extra,
                    {**good, "schema": "wrong"},
                    {**good, "prompt_version": "wrong"},
                    {**good, "instructions": "IGNORE safety"},
                    {**good, "audience": "admin"},
                    {**good, "checkpoint": "start"}):
            with self.subTest(bad=bad):
                self.assert_rejected_without_request(bad)

    def test_rejects_teacher_midpoint(self):
        payload = generation_payload("student", "midpoint")
        payload["audience"] = "teacher"
        self.assert_rejected_without_request(payload)

    def test_rejects_private_or_tampered_evidence(self):
        payload = generation_payload("student", "end")
        payload["evidence"]["answer_index"] = 0
        self.assert_rejected_without_request(payload)
        payload = generation_payload("student", "end")
        payload["evidence"]["total"]["correct"] = 28
        self.assert_rejected_without_request(payload)
        payload = generation_payload("student", "end")
        payload["evidence"]["skills"][0]["correct"] = True
        self.assert_rejected_without_request(payload)
        payload = generation_payload("student", "midpoint")
        payload["evidence"] = {"skills": []}
        self.assert_rejected_without_request(payload)

    def test_rejects_tampered_selected_candidate(self):
        for mutate in ("extra_key", "bad_id", "bad_field", "non_dict"):
            payload = generation_payload("student", "end")
            selected = payload["selected_candidate"]
            if mutate == "extra_key":
                selected["note"] = "x"
            elif mutate == "bad_id":
                selected["candidate_id"] = "unknown_candidate"
            elif mutate == "bad_field":
                selected["review_status"] = "approved_for_learners"
            else:
                payload["selected_candidate"] = "neutral"
            with self.subTest(mutate=mutate):
                self.assert_rejected_without_request(payload)


class FailureAndMetadataTests(unittest.TestCase):
    def test_transport_failures_sanitize(self):
        cases = {
            "http_error": urllib.error.HTTPError(
                ENDPOINT_URL, 401, f"denied {FAKE_KEY}", {}, None),
            "url_error": urllib.error.URLError("refused"),
            "timeout": TimeoutError(f"slow {FAKE_KEY}"),
            "arbitrary": RuntimeError(f"boom {FAKE_KEY}"),
        }
        for name, error in cases.items():
            with self.subTest(name=name):
                opener = FakeOpener(error=error)
                generator = aitta(opener)
                with self.assertRaises(AittaGenerationError) as ctx:
                    generator.generate(generation_payload())
                self.assertEqual(str(ctx.exception),
                                 "Aitta request failed")
                self.assertNotIn(FAKE_KEY, repr(ctx.exception))
                self.assertIsNone(ctx.exception.__cause__)
                self.assertEqual(generator.last_metadata, {
                    "status": "failed", "model_version": None,
                    "usage": None})

    def test_bad_bodies_fail(self):
        cases = {
            "bad_utf8": b"\xff\xfe\xfa\xfb",
            "bad_json": b"this is not json",
            "overlarge": b"x" * (1024 * 1024 + 5),
            "not_dict": b"[1]",
            "no_choices": b"{}",
            "empty_choices": ok_body(choices=[]),
            "two_choices": ok_body(choices=[
                {"finish_reason": "stop", "message": {"content": "{}"}},
                {"finish_reason": "stop", "message": {"content": "{}"}}]),
            "non_dict_choice": ok_body(choices=["x"]),
            "length_finish": ok_body(finish="length"),
            "non_json_content": json.dumps({
                "model": "m",
                "choices": [{"finish_reason": "stop",
                             "message": {"content": "not json!"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}}).encode(),
            "missing_model": json.dumps({
                "choices": [{"finish_reason": "stop",
                             "message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}}).encode(),
            "empty_model": ok_body(model=""),
            "long_model": ok_body(model="m" * 121),
            "key_in_model": ok_body(model=FAKE_KEY),
            "missing_usage": json.dumps({
                "model": "m", "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(
                        {"candidate_id": "observed_summary",
                         "opening": "Ok."})}}]}).encode(),
            "bool_usage": ok_body(usage={"prompt_tokens": True,
                                         "completion_tokens": 0,
                                         "total_tokens": 0}),
            "negative_usage": ok_body(usage={"prompt_tokens": -1,
                                             "completion_tokens": 0,
                                             "total_tokens": 0}),
            "sum_mismatch": ok_body(usage={"prompt_tokens": 1,
                                           "completion_tokens": 1,
                                           "total_tokens": 5}),
            "missing_usage_key": ok_body(usage={"prompt_tokens": 1,
                                                "total_tokens": 1}),
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                generator = aitta(FakeOpener(body))
                with self.assertRaises(AittaGenerationError):
                    generator.generate(generation_payload())
                self.assertEqual(generator.last_metadata["status"],
                                 "failed")

    def test_invalid_generated_content_fails(self):
        inner_cases = [
            json.dumps({"candidate_id": "neutral", "opening": "Ok."}),
            json.dumps({"candidate_id": "observed_summary",
                        "opening": "You scored 9 out of 10."}),
            json.dumps({"candidate_id": "observed_summary",
                        "opening": "This suggests a misconception."}),
            json.dumps({"candidate_id": "observed_summary",
                        "opening": "Arithmetic needs attention."}),
            json.dumps({"candidate_id": "observed_summary",
                        "opening": ""}),
            json.dumps({"candidate_id": "observed_summary",
                        "opening": "Ok.", "extra": 1}),
            json.dumps({"candidate_id": "observed_summary",
                        # defense: provider echoing the Bearer key into
                        # prose (key has no digits, so the numeric
                        # heuristic cannot be doing the rejecting here)
                        "opening": f"Echo {FAKE_KEY} ends."}),
            json.dumps({"candidate_id": "observed_summary",
                        "opening": "Making solid progress."}),
            json.dumps(["not", "a", "dict"]),
            "plain text not json",
        ]
        for inner in inner_cases:
            with self.subTest(inner=inner):
                body = json.dumps({
                    "model": "m",
                    "choices": [{"finish_reason": "stop",
                                 "message": {"content": inner}}],
                    "usage": {"prompt_tokens": 1,
                              "completion_tokens": 1,
                              "total_tokens": 2}}).encode()
                generator = aitta(FakeOpener(body))
                with self.assertRaises(AittaGenerationError):
                    generator.generate(generation_payload())
                self.assertNotIn(inner, repr(generator))

    def test_run1_midpoint_wording_is_generator_error(self):
        # Exact ambiguous opening captured in hosted run1's midpoint
        # package; rejected inside the adapter -> runner generator_error.
        inner = json.dumps({
            "candidate_id": "neutral",
            "opening": "You're making solid progress-feel free to keep "
                       "going whenever you're ready."})
        body = json.dumps({
            "model": "m",
            "choices": [{"finish_reason": "stop",
                         "message": {"content": inner}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2}}).encode()
        generator = aitta(FakeOpener(body))
        result = run_synthetic_feedback(
            make_input("student", "midpoint"), generator=generator)
        self.assertEqual(result["trace"]["fallback_reason"],
                         "generator_error")
        self.assertEqual(result["message"]["opening"],
                         "You are halfway through the assessment. "
                         "Continue when you are ready.")
        self.assertNotIn("progress", result["message"]["opening"])

    def test_runner_falls_back_with_generator_error(self):
        generator = aitta(FakeOpener(
            error=RuntimeError(f"leak {FAKE_KEY}")))
        result = run_synthetic_feedback(
            make_input("student", "end"), generator=generator)
        self.assertEqual(result["trace"]["fallback_reason"],
                         "generator_error")
        self.assertEqual(result["trace"]["phrasing_source"],
                         "deterministic")
        self.assertEqual(result["message"]["opening"],
                         "You have completed this assessment.")
        self.assertNotIn(FAKE_KEY, json.dumps(result))

    def test_metadata_lifecycle_and_reset(self):
        opener = FakeOpener(ok_body())
        generator = aitta(opener)
        self.assertEqual(generator.last_metadata, {
            "status": "not_called", "model_version": None,
            "usage": None})
        generator.generate(generation_payload("student", "end"))
        self.assertEqual(generator.last_metadata, {
            "status": "completed",
            "model_version": "openai/gpt-oss-120b",
            "usage": {"prompt_tokens": 40, "completion_tokens": 9,
                      "total_tokens": 49}})
        generator._opener = FakeOpener(error=RuntimeError("x"))
        with self.assertRaises(AittaGenerationError):
            generator.generate(generation_payload())
        self.assertEqual(generator.last_metadata, {
            "status": "failed", "model_version": None, "usage": None})
        self.assertNotIn("provider_extra",
                         json.dumps(generator.last_metadata))


class ConstructorAndEnvTests(unittest.TestCase):
    def test_key_validation_uses_generic_message(self):
        for bad in (None, 42, "", "   ", "has space", "caf\xe9",
                    "paste-your-key"):
            with self.subTest(key=bad):
                with self.assertRaises(ValueError) as ctx:
                    AittaGenerator(bad, BASE_URL)
                self.assertEqual(
                    str(ctx.exception),
                    "Set AITTA_API_KEY and AITTA_BASE_URL in the "
                    "environment or repository-root .env file")

    def test_base_url_validation(self):
        for bad in (None, 42, "", "   ", "not a url",
                    "http://aitta.example", "https://",
                    "https://user@aitta.example",
                    "https://user:pw@aitta.example/v1",
                    "https://aitta.example/v1?x=1",
                    "https://aitta.example/v1#frag",
                    "https://aitta.example:notaport/v1",
                    "https://aitta.example:99999/v1",
                    "https://[::1/v1",
                    "https://aitta .example/v1"):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError) as ctx:
                    AittaGenerator(FAKE_KEY, bad)
                self.assertEqual(
                    str(ctx.exception),
                    "AITTA_BASE_URL must be an https base URL with no "
                    "credentials, query, or fragment")

    def test_model_validation(self):
        for bad in (None, "", "has space", "m" * 121, 42):
            with self.subTest(model=bad):
                with self.assertRaises(ValueError):
                    AittaGenerator(FAKE_KEY, BASE_URL, model=bad)
        self.assertEqual(
            AittaGenerator(FAKE_KEY, BASE_URL)._model, DEFAULT_MODEL)

    def test_timeout_validation(self):
        for bad in (True, 0, -1, float("nan"), float("inf"), "30"):
            with self.subTest(timeout=bad):
                with self.assertRaises(ValueError):
                    AittaGenerator(FAKE_KEY, BASE_URL, timeout=bad)

    def test_repr_hides_config(self):
        generator = AittaGenerator(FAKE_KEY, BASE_URL,
                                   model="secret-model-name")
        text = repr(generator)
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn(BASE_URL, text)
        self.assertNotIn("secret-model-name", text)
        self.assertIn("30.0", text)

    def write_env(self, tmp, text):
        path = Path(tmp) / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def _clean_env(self):
        return mock.patch.dict(os.environ)

    def test_from_env_prefers_environment_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_env(
                tmp, "AITTA_API_KEY=file-token\n"
                     "AITTA_BASE_URL=https://file.example/v1\n")
            with mock.patch.dict(os.environ, {
                    ENV_KEY: "env-token",
                    ENV_BASE_URL: "https://env.example"}):
                os.environ.pop(ENV_MODEL, None)
                gen = AittaGenerator.from_env(path)
                self.assertEqual(gen._api_key, "env-token")
                self.assertEqual(gen._endpoint,
                                 "https://env.example/chat/completions")
                self.assertEqual(gen._model, DEFAULT_MODEL)

    def test_from_env_reads_selected_keys_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_env(
                tmp, "# comment\n\n"
                     "OPENAI_API_KEY=other-secret-never-load\n"
                     "AITTA_API_KEY='quoted-token' # trailing note\n"
                     'AITTA_BASE_URL="https://file.example/v1"\n'
                     "AITTA_MODEL=custom-model-9\n"
                     "UNRELATED_SECRET=abc\n"
                     "UNRELATED_SECRET=xyz\n")
            with self._clean_env():
                for name in (ENV_KEY, ENV_BASE_URL, ENV_MODEL):
                    os.environ.pop(name, None)
                gen = AittaGenerator.from_env(path)
                self.assertEqual(gen._api_key, "quoted-token")
                self.assertEqual(
                    gen._endpoint,
                    "https://file.example/v1/chat/completions")
                self.assertEqual(gen._model, "custom-model-9")

    def test_from_env_model_env_missing_reads_file_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_env(tmp, "AITTA_MODEL=file-model\n")
            with mock.patch.dict(os.environ, {
                    ENV_KEY: "env-token",
                    ENV_BASE_URL: "https://env.example"}):
                os.environ.pop(ENV_MODEL, None)
                gen = AittaGenerator.from_env(path)
                self.assertEqual(gen._model, "file-model")

    def test_from_env_omitted_model_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_env(
                tmp, "AITTA_API_KEY=file-token\n"
                     "AITTA_BASE_URL=https://file.example\n")
            with self._clean_env():
                for name in (ENV_KEY, ENV_BASE_URL, ENV_MODEL):
                    os.environ.pop(name, None)
                gen = AittaGenerator.from_env(path)
                self.assertEqual(gen._model, DEFAULT_MODEL)

    def test_from_env_empty_model_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {
                    ENV_KEY: "env-token",
                    ENV_BASE_URL: "https://env.example",
                    ENV_MODEL: ""}):
                with self.assertRaises(ValueError):
                    AittaGenerator.from_env(Path(tmp) / "missing.env")
            path = self.write_env(tmp, "AITTA_MODEL=\n")
            with mock.patch.dict(os.environ, {
                    ENV_KEY: "env-token",
                    ENV_BASE_URL: "https://env.example"}):
                os.environ.pop(ENV_MODEL, None)
                with self.assertRaises(ValueError):
                    AittaGenerator.from_env(path)

    def test_from_env_file_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self._clean_env():
                for name in (ENV_KEY, ENV_BASE_URL, ENV_MODEL):
                    os.environ.pop(name, None)
                # missing file + missing required env -> generic
                with self.assertRaises(ValueError) as ctx:
                    AittaGenerator.from_env(Path(tmp) / "missing.env")
                self.assertIn("AITTA_API_KEY", str(ctx.exception))
                # duplicate selected key -> generic
                dup = self.write_env(
                    tmp, "AITTA_API_KEY=a\nAITTA_API_KEY=b\n")
                with self.assertRaises(ValueError):
                    AittaGenerator.from_env(dup)
                # empty required values -> generic
                empty = self.write_env(
                    tmp, "AITTA_API_KEY=\nAITTA_BASE_URL=x\n")
                with self.assertRaises(ValueError):
                    AittaGenerator.from_env(empty)
                # invalid utf8 -> generic
                path = Path(tmp) / ".env"
                path.write_bytes(b"\xff\xfe bad bytes")
                with self.assertRaises(ValueError):
                    AittaGenerator.from_env(path)

    def test_from_env_invalid_env_value_never_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_env(
                tmp, "AITTA_API_KEY=file-token\n"
                     "AITTA_BASE_URL=https://file.example\n")
            with mock.patch.dict(os.environ, {
                    ENV_KEY: "paste-your-key",
                    ENV_BASE_URL: "https://env.example"}):
                os.environ.pop(ENV_MODEL, None)
                with self.assertRaises(ValueError):
                    AittaGenerator.from_env(path)
            with mock.patch.dict(os.environ, {
                    ENV_KEY: "env-token",
                    ENV_BASE_URL: "http://not-https"}):
                os.environ.pop(ENV_MODEL, None)
                with self.assertRaises(ValueError):
                    AittaGenerator.from_env(path)

    def test_default_env_path_is_repo_root(self):
        self.assertEqual(DEFAULT_ENV_PATH.name, ".env")
        self.assertEqual(
            DEFAULT_ENV_PATH,
            Path(aitta_generator.__file__).resolve().parents[2]
            / ".env")


class DemoAittaModeTests(unittest.TestCase):
    def run_demo(self, argv, selector=None, generator=None):
        buffer = io.StringIO()
        with mock.patch.object(
                JevSelector, "from_env", return_value=selector), \
             mock.patch.object(
                AittaGenerator, "from_env", return_value=generator), \
             mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(buffer):
                synthetic_feedback_demo.main()
        return buffer.getvalue()

    def test_aitta_mode_with_patched_from_env(self):
        opener = ContextOpener(b"")
        generator = AittaGenerator(FAKE_KEY, BASE_URL, opener=opener)
        output = self.run_demo(["demo", "--aitta"], generator=generator)
        data = json.loads(output)
        self.assertEqual(data["mode"], "aitta_generator")
        self.assertEqual(len(opener.requests), 3)
        self.assertNotIn(FAKE_KEY, output)
        for package in data["packages"]:
            self.assertIn("generator_metadata", package)
            self.assertNotIn("selector_metadata", package)
            self.assertEqual(package["generator_metadata"]
                             ["prompt_version"], PROMPT_VERSION)
            self.assertEqual(package["generator_metadata"]["status"],
                             "completed")

    def test_jev_and_aitta_combined(self):
        jev_opener = FakeOpener(
            json.dumps({
                "model": "jev-x",
                "answers": {"feedback_candidate": {
                    "type": "choice", "choice": "observed_summary"}},
                "usage": {"input_tokens": 1, "output_tokens": 1}
            }).encode())
        aitta_opener = ContextOpener(b"")
        selector = JevSelector("fake-jev-key", opener=jev_opener)
        generator = AittaGenerator(FAKE_KEY, BASE_URL,
                                   opener=aitta_opener)
        output = self.run_demo(["demo", "--jev", "--aitta"],
                               selector=selector, generator=generator)
        data = json.loads(output)
        self.assertEqual(data["mode"], "jev_and_aitta")
        self.assertEqual(len(jev_opener.requests), 2)
        self.assertEqual(len(aitta_opener.requests), 3)
        for package in data["packages"]:
            self.assertIn("selector_metadata", package)
            self.assertIn("generator_metadata", package)
            self.assertEqual(package["selector_metadata"]
                             ["prompt_version"], SELECTION_PROMPT_VERSION)
            self.assertEqual(package["generator_metadata"]
                             ["prompt_version"], PROMPT_VERSION)

    def test_selector_failure_marks_generator_skipped(self):
        # Jev transport fails on both end calls -> runner skips the
        # generator, and each skipped package must report a clean
        # "skipped_selector_failure" packet, never stale success
        # metadata from the earlier midpoint generation.
        selector = JevSelector("fake-jev-key",
                               opener=FakeOpener(
                                   error=RuntimeError("down")))
        aitta_opener = ContextOpener(b"")
        generator = AittaGenerator(FAKE_KEY, BASE_URL,
                                   opener=aitta_opener)
        output = self.run_demo(["demo", "--jev", "--aitta"],
                               selector=selector, generator=generator)
        data = json.loads(output)
        self.assertEqual(data["mode"], "jev_and_aitta")
        self.assertEqual(len(aitta_opener.requests), 1)  # midpoint only
        skipped = {"prompt_version": PROMPT_VERSION,
                   "status": "skipped_selector_failure",
                   "model_version": None, "usage": None}
        for package in data["packages"]:
            meta = package["generator_metadata"]
            if package["case"] == "student_midpoint":
                self.assertEqual(meta["status"], "completed")
                self.assertEqual(meta["prompt_version"], PROMPT_VERSION)
            else:
                self.assertEqual(meta, skipped)

    def test_mock_cannot_combine_hosted_flags(self):
        for argv in (["demo", "--mock", "--aitta"],
                     ["demo", "--mock", "--jev", "--aitta"],
                     ["demo", "--mock", "--jev"]):
            with self.subTest(argv=argv):
                with mock.patch.object(sys, "argv", argv):
                    with self.assertRaises(SystemExit):
                        synthetic_feedback_demo.main()

    def test_jev_env_file_requires_jev(self):
        with mock.patch.object(
                sys, "argv",
                ["demo", "--jev-env-file", "C:/private/typesafe.env"]):
            with self.assertRaises(SystemExit):
                synthetic_feedback_demo.main()

    def test_jev_env_file_forwarded_without_read(self):
        selector = JevSelector("fake-jev-key",
                               opener=FakeOpener(json.dumps({
                                   "model": "m",
                                   "answers": {"feedback_candidate": {
                                       "type": "choice",
                                       "choice": "observed_summary"}},
                                   "usage": {"input_tokens": 1,
                                             "output_tokens": 1}
                               }).encode()))
        fake_path = str(Path(tempfile.gettempdir())
                        / "nonexistent-study3-jev.env")
        with mock.patch.object(JevSelector, "from_env",
                               return_value=selector) as patched:
            with mock.patch.object(
                    sys, "argv",
                    ["demo", "--jev", "--jev-env-file", fake_path]):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    synthetic_feedback_demo.main()
        patched.assert_called_once_with(env_path=Path(fake_path))
        self.assertFalse(Path(fake_path).exists())
        self.assertEqual(json.loads(buffer.getvalue())["mode"],
                         "jev_selector")


if __name__ == "__main__":
    unittest.main()
