import copy
import json
import tempfile
import unittest
from pathlib import Path

import phase3_paths  # noqa: F401
from aitta_generator import AittaGenerationError, AittaGenerator
from evidence_feedback import build_evidence, canonical_digest, run_feedback
from evidence_feedback_policy import SCOPE, build_candidates
from full_feedback import (
    CachedFullFeedbackGenerator, FULL_FEEDBACK_INSTRUCTIONS,
    FULL_FEEDBACK_MAX_TOKENS, FULL_FEEDBACK_PROMPT_VERSION,
    FULL_FEEDBACK_SECTION_KEYS, FULL_FEEDBACK_SECTION_MAX_CHARS,
    FULL_FEEDBACK_SECTION_TITLES, FullFeedbackAittaGenerator,
    build_full_input, full_message_sections, validate_full_input,
    validate_full_reply)
from replay_provider_scenarios import CaptureCache
from tests.helpers import make_bank, make_taxonomy, responses

API_KEY = "test-aitta-key-0123456789abcdef"
BASE_URL = "https://aitta.example.test"
MODEL = "openai/gpt-oss-120b"

VALID_SECTIONS = {
    "assessment_summary":
        "You have completed the assessment.",
    "observed_strengths":
        "Your observed answers provide a starting point for review.",
    "review_focus":
        "Review the selected assessed content with support.",
    "next_steps":
        "Work through the supplied example with your teacher."}


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, limit=-1):
        if limit is not None and limit >= 0:
            return self._payload[:limit]
        return self._payload

    def close(self):
        self.closed = True


class RecordingOpener:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append({
            "body": json.loads(request.data.decode("utf-8")),
            "headers": dict(request.header_items())})
        if self.error is not None:
            raise self.error
        return FakeResponse(self.payload)


def wire_reply(sections=None, candidate_id=None, model=MODEL,
               usage=None, finish_reason="stop", content=None):
    if sections is None:
        sections = dict(VALID_SECTIONS)
    if content is None:
        reply = {"candidate_id": candidate_id, "sections": sections}
        content = json.dumps(reply)
    return {
        "choices": [{"finish_reason": finish_reason,
                     "message": {"role": "assistant",
                                 "content": content}}],
        "model": model,
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 20,
                           "total_tokens": 30}}


class FullFeedbackTests(unittest.TestCase):
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

    def test_wire_sanitized_and_prompt_verbatim(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=self.selected_id))
        reply = self._generator(opener).generate(self.full_input)
        self.assertEqual(reply["candidate_id"], self.selected_id)
        self.assertEqual(len(opener.calls), 1)
        body = opener.calls[0]["body"]
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["max_tokens"], FULL_FEEDBACK_MAX_TOKENS)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][0]["content"],
                         FULL_FEEDBACK_INSTRUCTIONS)
        context = json.loads(body["messages"][1]["content"])
        self.assertEqual(
            set(context), {"schema", "prompt_version", "audience",
                           "checkpoint", "evidence", "selected_candidate",
                           "feedback_plan", "baseline_sections"})
        self.assertEqual(context["prompt_version"],
                         FULL_FEEDBACK_PROMPT_VERSION)
        self.assertEqual(set(context["evidence"]),
                         {"total", "skills", "subtopics"})
        self.assertEqual(context["evidence"]["total"]["out_of"], 40)
        for row in (context["evidence"]["skills"]
                    + context["evidence"]["subtopics"]):
            self.assertNotIn("halves", row)
        raw = json.dumps(body)
        for forbidden in ("answer_index", "selected_index",
                          "session_id", "bank_sha256", "taxonomy_sha256",
                          "data_origin", API_KEY):
            self.assertNotIn(forbidden, raw)
        evidence = json.dumps(context["evidence"])
        self.assertNotIn("halves", evidence)
        self.assertNotIn(API_KEY, body["messages"][1]["content"])

    def test_valid_reply_sections_and_scope(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=self.selected_id))
        generator = self._generator(opener)
        reply = generator.generate(self.full_input)
        self.assertEqual(set(reply["sections"]),
                         set(FULL_FEEDBACK_SECTION_KEYS))
        self.assertEqual(reply["sections"]["review_focus"],
                         VALID_SECTIONS["review_focus"])
        rendered = full_message_sections(reply["sections"])
        self.assertTrue(rendered[2]["text"].endswith(SCOPE))
        meta = generator.last_metadata
        self.assertEqual(meta["status"], "completed")
        self.assertEqual(meta["prompt_version"],
                         FULL_FEEDBACK_PROMPT_VERSION)
        self.assertEqual(meta["role"], "full_structured_feedback")
        self.assertIn("transport", meta)

    def test_reply_scope_appended_exactly_once_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), 4)
            opener = RecordingOpener(
                wire_reply(candidate_id=self.selected_id))
            native = self._generator(opener)
            cached = CachedFullFeedbackGenerator(
                native, cache, MODEL, "endpointhash" * 8)
            first = run_feedback(
                self.evidence, "student", "end",
                selector=self._selector(), generator=cached)
            self.assertFalse(cached.execution["reused"])
            second = run_feedback(
                self.evidence, "student", "end",
                selector=self._selector(), generator=cached)
            self.assertTrue(cached.execution["reused"])
            self.assertEqual(len(opener.calls), 1)
            self.assertEqual(first["message"], second["message"])
            self.assertEqual(first["message"]["text"].count(SCOPE), 1)

    def test_scope_notice_length_boundary(self):
        room = (FULL_FEEDBACK_SECTION_MAX_CHARS
                - len("\n\n" + SCOPE))
        sections = dict(VALID_SECTIONS)
        sections["review_focus"] = "x" * room
        reply = {"candidate_id": self.selected_id,
                 "sections": sections}
        self.assertIsNotNone(
            validate_full_reply(reply, self.selected_id))
        sections["review_focus"] = "x" * (room + 1)
        reply["sections"] = sections
        self.assertIsNone(
            validate_full_reply(reply, self.selected_id))

    def test_cached_reply_wrong_candidate_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), 4)
            opener = RecordingOpener(
                wire_reply(candidate_id=self.selected_id))
            native = self._generator(opener)
            cached = CachedFullFeedbackGenerator(
                native, cache, MODEL, "endpointhash" * 8)
            cached.generate(self.full_input)
            path = next(Path(tmp).glob("aitta_full_*.json"))
            record = json.loads(path.read_text())
            record["reply"]["candidate_id"] = "tampered"
            path.write_text(json.dumps(record))
            with self.assertRaises(ValueError):
                cached.generate(self.full_input)
            self.assertTrue(cached.execution["reused"])

    def _reject(self, reply):
        opener = RecordingOpener(reply)
        with self.assertRaises(AittaGenerationError):
            self._generator(opener).generate(self.full_input)

    def test_wrong_candidate_id_rejected(self):
        self._reject(wire_reply(candidate_id="someone_else"))

    def test_missing_and_extra_section_keys_rejected(self):
        bad = dict(VALID_SECTIONS)
        del bad["next_steps"]
        self._reject(wire_reply(sections=bad,
                                candidate_id=self.selected_id))
        extra = dict(VALID_SECTIONS, confidence="high")
        self._reject(wire_reply(sections=extra,
                                candidate_id=self.selected_id))

    def test_empty_overlength_numeric_and_markup_rejected(self):
        for key, value in (("assessment_summary", "   "),
                           ("observed_strengths", "x" * 2001),
                           ("review_focus", "You scored 20 correct."),
                           ("next_steps", "Use <b>this</b> tip.")):
            sections = dict(VALID_SECTIONS)
            sections[key] = value
            self._reject(wire_reply(sections=sections,
                                    candidate_id=self.selected_id))

    def test_truncated_finish_reason_and_json_rejected(self):
        self._reject(wire_reply(candidate_id=self.selected_id,
                                finish_reason="length"))
        self._reject(wire_reply(content="{not json"))

    def test_model_and_usage_validation(self):
        self._reject(wire_reply(candidate_id=self.selected_id,
                                model=f"leak-{API_KEY}"))
        self._reject(wire_reply(candidate_id=self.selected_id,
                                usage={"prompt_tokens": 5,
                                       "completion_tokens": 4,
                                       "total_tokens": 999}))

    def test_tampered_input_rejected_before_network(self):
        for mutate in (
                lambda p: p["feedback_plan"].update(
                    {"action": "invented"}),
                lambda p: p["baseline_sections"].pop(),
                lambda p: p.update({"session_id": "abc"}),
                lambda p: p["evidence"].update({"answers": []})):
            payload = copy.deepcopy(self.full_input)
            mutate(payload)
            opener = RecordingOpener(
                wire_reply(candidate_id=self.selected_id))
            with self.assertRaises(ValueError):
                self._generator(opener).generate(payload)
            self.assertEqual(opener.calls, [])

    def test_cache_keys_differ_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), 4)
            opener = RecordingOpener(
                wire_reply(candidate_id=self.selected_id))
            native = self._generator(opener)
            cached = CachedFullFeedbackGenerator(
                native, cache, MODEL, "endpointhash" * 8)
            first = cached.generate(self.full_input)
            self.assertFalse(cached.execution["reused"])
            second = cached.generate(copy.deepcopy(self.full_input))
            self.assertTrue(cached.execution["reused"])
            self.assertEqual(first, second)
            self.assertEqual(len(opener.calls), 1)
            self.assertTrue(list(Path(tmp).glob("aitta_full_*.json")))
            other_evidence = build_evidence(
                self.bank, self.taxonomy,
                responses(self.bank, correct=True, count=40))
            other_selected = build_candidates(other_evidence, "end")[0]
            other_input = build_full_input(
                other_evidence, "student", other_selected)
            other_body = native.request(other_input)
            self.assertNotEqual(
                canonical_digest(other_body),
                canonical_digest(
                    json.loads(next(Path(tmp).glob(
                        "aitta_full_*.json")).read_text())["request"]["body"]))

    def _selector(self):
        selected_id = self.selected_id

        class Pick:
            def select(self, payload):
                return {"candidate_id": selected_id}

        return Pick()

    def test_run_feedback_full_injected_mode(self):
        class FullFake:
            supports_full_feedback = True

            def generate(self, payload):
                return {"candidate_id":
                        payload["selected_candidate"]["candidate_id"],
                        "sections": dict(VALID_SECTIONS)}

        review = run_feedback(
            self.evidence, "student", "end",
            selector=self._selector(), generator=FullFake())
        self.assertEqual(review["selected_candidate_id"], self.selected_id)
        kinds = [s["kind"] for s in review["message"]["sections"]]
        titles = [s["title"] for s in review["message"]["sections"]]
        self.assertEqual(kinds, list(FULL_FEEDBACK_SECTION_KEYS))
        self.assertEqual(titles, list(FULL_FEEDBACK_SECTION_TITLES))
        self.assertTrue(review["message"]["sections"][2]["text"].endswith(
            SCOPE))
        self.assertEqual(review["trace"]["phrasing_source"],
                         "injected_generator_full_sections")
        self.assertEqual(review["trace"]["generation_prompt_version"],
                         FULL_FEEDBACK_PROMPT_VERSION)
        self.assertEqual(
            review["trace"]["validation"],
            "shape_and_numeric_guardrails_not_semantic_verification")

    def test_run_feedback_invalid_full_falls_back_keeping_selection(self):
        class BadFull:
            supports_full_feedback = True

            def generate(self, payload):
                payload["feedback_plan"]["action"] = "mutated"
                return {"candidate_id":
                        payload["selected_candidate"]["candidate_id"],
                        "sections": dict(VALID_SECTIONS)}

        review = run_feedback(
            self.evidence, "student", "end",
            selector=self._selector(), generator=BadFull())
        self.assertEqual(review["selected_candidate_id"], self.selected_id)
        self.assertEqual(review["trace"]["fallback_reason"],
                         "invalid_generation")
        self.assertEqual(review["trace"]["phrasing_source"],
                         "deterministic")
        self.assertEqual(
            review["trace"]["generation_prompt_version"],
            FULL_FEEDBACK_PROMPT_VERSION)

    def test_full_generator_system_exit_keeps_selection(self):
        class ExhaustedFull:
            supports_full_feedback = True

            def generate(self, payload):
                raise SystemExit("new-call budget exhausted")

        review = run_feedback(
            self.evidence, "student", "end",
            selector=self._selector(), generator=ExhaustedFull())
        self.assertEqual(review["selected_candidate_id"], self.selected_id)
        self.assertEqual(review["trace"]["fallback_reason"],
                         "generator_error")
        self.assertEqual(
            review["trace"]["generation_prompt_version"],
            FULL_FEEDBACK_PROMPT_VERSION)
        self.assertEqual(review["trace"]["phrasing_source"],
                         "deterministic")

    def test_validate_full_reply_guardrails(self):
        self.assertIsNone(validate_full_reply(None, self.selected_id))
        self.assertIsNone(validate_full_reply(
            {"candidate_id": self.selected_id,
             "sections": {"a": "x"}}, self.selected_id))
        ok = validate_full_reply(
            {"candidate_id": self.selected_id,
             "sections": dict(VALID_SECTIONS)}, self.selected_id)
        self.assertIsNotNone(ok)
        self.assertEqual(ok["assessment_summary"],
                         VALID_SECTIONS["assessment_summary"])

    def test_validate_full_input_accepts_built_payload(self):
        normalized = validate_full_input(copy.deepcopy(self.full_input))
        self.assertEqual(normalized["selected_candidate"], self.selected)


if __name__ == "__main__":
    unittest.main()
