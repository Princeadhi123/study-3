"""Tests for the scoped worked-example generation adapter (mocked)."""
import copy
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

import worked_example_generation
from aitta_generator import (
    AittaGenerationError, AittaGenerator, DEFAULT_MODEL)
from replay_provider_scenarios import CaptureCache
from research_content_descriptions import MATHEMATICAL_TASKS
from tests.test_jev_selector import FakeOpener, FakeResponse
from worked_example_generation import (
    CachedWorkedExampleGenerator, WorkedExampleAittaGenerator,
    build_input, validate_reply)

FAKE_KEY = "fake-aitta-token-xyz"
BASE_URL = "https://aitta.example/v1"


def make_card(**overrides):
    card = {"candidate_id": "pct_slot", "subtopic_id": "percentage_amount",
            "subtopic_name": "Percentage of an amount",
            "question_id": "q-pct-8", "status": "ready",
            "prompt": "What is 100% of 75?",
            "options": ["75", "100"],
            "selected_answer": "100", "correct_answer": "75",
            "checked_steps": ["Compute 100% of 75.",
                              "The answer is 75."],
            "checked_reasoning_question":
                "Why does 100% keep the value unchanged?"}
    card.update(overrides)
    return card


def ok_body(card, inner=None, model="openai/gpt-oss-120b",
            finish="stop", usage=None):
    if inner is None:
        inner = json.dumps({
            "question_id": card["question_id"],
            "steps": ["First multiply by the percentage.",
                      "The result stays 75."],
            "reasoning_question": "Why is 100% of a number the number?"})
    if usage is None:
        usage = {"prompt_tokens": 40, "completion_tokens": 20,
                 "total_tokens": 60}
    return json.dumps({"model": model,
                       "choices": [{"finish_reason": finish,
                                    "message": {"role": "assistant",
                                                "content": inner}}],
                       "usage": usage}).encode("utf-8")


def native(opener=None, model=DEFAULT_MODEL):
    inner = AittaGenerator(FAKE_KEY, BASE_URL, timeout=12.5,
                           model=model,
                           opener=(opener if opener is not None
                                   else FakeOpener(ok_body(make_card()))))
    return WorkedExampleAittaGenerator(inner)


class BuildInputTests(unittest.TestCase):
    def test_scoped_input_shape(self):
        built = build_input(make_card())
        self.assertEqual(set(built), {
            "schema", "prompt_version", "question_id", "prompt",
            "options", "selected_answer", "correct_answer",
            "subtopic_id", "mathematical_task"})
        self.assertEqual(built["schema"],
                         worked_example_generation.SCHEMA)
        self.assertEqual(built["prompt_version"],
                         worked_example_generation.PROMPT_VERSION)
        self.assertEqual(built["question_id"], "q-pct-8")
        self.assertEqual(built["subtopic_id"], "percentage_amount")
        self.assertEqual(built["mathematical_task"],
                         MATHEMATICAL_TASKS["percentage_amount"])
        # No narrative sections or other cards leak into the input.
        self.assertNotIn("sections", built)
        self.assertNotIn("review_examples", built)

    def test_math_notation_instructions_in_all_prompts(self):
        from feedback_language import MATH_NOTATION_INSTRUCTIONS
        from full_feedback import (
            FULL_FEEDBACK_INSTRUCTIONS, MULTI_FULL_FEEDBACK_INSTRUCTIONS)
        from worked_first_context import INSTRUCTIONS as WF_INSTRUCTIONS
        from midpoint_feedback import INSTRUCTIONS as MIDPOINT_INSTRUCTIONS
        for instructions in (FULL_FEEDBACK_INSTRUCTIONS,
                             MULTI_FULL_FEEDBACK_INSTRUCTIONS,
                             WF_INSTRUCTIONS,
                             worked_example_generation.INSTRUCTIONS):
            self.assertIn(MATH_NOTATION_INSTRUCTIONS, instructions)
        self.assertNotIn(MATH_NOTATION_INSTRUCTIONS, MIDPOINT_INSTRUCTIONS)

    def test_fraction_records_not_mutated(self):
        card = make_card(prompt="3⁄6 · 10⁄1",
                         options=["30⁄6", "6⁄24"],
                         selected_answer="6⁄24",
                         correct_answer="30⁄6")
        built = build_input(card)
        self.assertEqual(built["prompt"], "3⁄6 · 10⁄1")
        self.assertEqual(built["options"], ["30⁄6", "6⁄24"])
        self.assertEqual(built["selected_answer"], "6⁄24")
        self.assertEqual(built["correct_answer"], "30⁄6")

    def test_request_normalizes_fraction_notation_only_on_the_wire(self):
        card = make_card(subtopic_id="fraction_product",
                         prompt="3\u20446 * 10\u20441",
                         options=["30\u20446", "6\u204424"],
                         selected_answer="6\u204424",
                         correct_answer="30\u20446")
        before = copy.deepcopy(card)
        normalized = build_input(card)
        body = native().request(card)
        wire = json.loads(body["messages"][1]["content"])
        self.assertEqual(wire["prompt"], "3/6 * 10/1")
        self.assertEqual(wire["options"], ["30/6", "6/24"])
        self.assertEqual(wire["selected_answer"], "6/24")
        self.assertEqual(wire["correct_answer"], "30/6")
        self.assertEqual(wire["question_id"], card["question_id"])
        self.assertEqual(card, before)
        self.assertEqual(build_input(card), normalized)

    def test_no_solution_fields_reach_request(self):
        card = make_card(checked_steps=["DISTINCT CHECKED STEP"],
                         checked_reasoning_question="DISTINCT CHECKED WHY",
                         steps=["DISTINCT APP STEP"],
                         explanation="DISTINCT APP EXPLANATION",
                         reasoning_question="DISTINCT APP WHY")
        for field in ("checked_steps", "checked_reasoning_question",
                      "steps", "explanation", "reasoning_question"):
            self.assertIn(field, card)
        built = build_input(card)
        wire = json.dumps(built)
        for field in ("checked_steps", "checked_reasoning_question",
                      "steps", "explanation", "reasoning_question"):
            self.assertNotIn(field, built)
        self.assertNotIn("DISTINCT", wire)

    def test_mathematical_task_mapping(self):
        self.assertEqual(
            build_input(make_card())["mathematical_task"],
            MATHEMATICAL_TASKS["percentage_amount"])
        self.assertEqual(
            build_input(make_card(subtopic_id="combine_like_terms"))
            ["mathematical_task"],
            MATHEMATICAL_TASKS["combine_like_terms"])
        self.assertIsNone(
            build_input(make_card(subtopic_id="unmapped_subtopic"))
            ["mathematical_task"])

    def test_normalized_input_requires_exact_keyset(self):
        built = build_input(make_card())
        self.assertEqual(
            worked_example_generation._normalized_input(built), built)
        for bad in (dict(built, sections={}),
                    dict(built, review_examples=[]),
                    dict(built, checked_steps=["x"]),
                    {k: v for k, v in built.items()
                     if k != "mathematical_task"},
                    dict(built, mathematical_task="tampered task"),
                    dict(built, subtopic_id="  "),
                    dict(built, prompt="  ")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                worked_example_generation._normalized_input(bad)
        # None task roundtrips for unmapped subtopics.
        unmapped = build_input(make_card(subtopic_id="unmapped_subtopic"))
        self.assertIsNone(unmapped["mathematical_task"])
        self.assertEqual(
            worked_example_generation._normalized_input(unmapped),
            unmapped)

    def test_stale_schema_or_prompt_version_rejected(self):
        built = build_input(make_card())
        for bad in (dict(built, schema="phase3_selected_worked_example_v1"),
                    dict(built,
                         prompt_version="aitta_selected_worked_example_v1"),
                    dict(built,
                         prompt_version="aitta_selected_worked_example_v4")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                worked_example_generation._normalized_input(bad)

    def test_unsupported_card_rejected_without_call(self):
        for card in (None, {}, make_card(status="pending"),
                     make_card(subtopic_id=""), make_card(subtopic_id=None),
                     make_card(prompt=""), make_card(options=[])):
            with self.assertRaises(ValueError, msg=repr(card)):
                build_input(card)


class RequestAndReplyTests(unittest.TestCase):
    def test_request_uses_configured_model_and_timeout(self):
        card = make_card()
        opener = FakeOpener(ok_body(card))
        result = native(opener, model="custom/model").generate(card)
        self.assertEqual(result["question_id"], "q-pct-8")
        request = opener.requests[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "custom/model")
        self.assertEqual(body["max_tokens"],
                         worked_example_generation.MAX_TOKENS)
        self.assertEqual(
            body["messages"][0]["content"],
            worked_example_generation.INSTRUCTIONS)
        context = json.loads(body["messages"][1]["content"])
        self.assertEqual(context["question_id"], "q-pct-8")
        self.assertEqual(context["subtopic_id"], "percentage_amount")
        self.assertEqual(context["mathematical_task"],
                         MATHEMATICAL_TASKS["percentage_amount"])
        for field in ("checked_steps", "checked_reasoning_question",
                      "steps", "explanation", "reasoning_question"):
            self.assertNotIn(field, context)
        self.assertEqual(opener.timeouts[0], 12.5)

    def test_ready_card_without_solution_generates(self):
        card = make_card()
        for field in ("checked_steps", "checked_reasoning_question"):
            del card[field]
        result = native().generate(card)
        self.assertEqual(result["question_id"], "q-pct-8")

    def test_unavailable_card_generates(self):
        card = make_card(status="unavailable", steps=[],
                         explanation="",
                         reasoning_question="", solution_version=None)
        result = native().generate(card)
        self.assertEqual(result["question_id"], "q-pct-8")

    def test_reply_only_carries_card_overlay(self):
        overlay = native().generate(make_card())
        self.assertEqual(set(overlay), {"question_id", "steps",
                                       "reasoning_question"})
        self.assertEqual(native().generate(make_card())["steps"][0],
                         "First multiply by the percentage.")

    def assert_rejected(self, inner, code=None):
        card = make_card()
        generator = native(FakeOpener(ok_body(card, inner=inner)))
        with self.assertRaises(AittaGenerationError):
            generator.generate(card)
        if code is not None:
            self.assertEqual(generator._last_metadata["status"],
                             "failed")
            self.assertEqual(
                generator._last_metadata["rejection_code"], code)

    def test_wrong_question_id_rejected(self):
        self.assert_rejected(json.dumps({
            "question_id": "other", "steps": ["A."],
            "reasoning_question": "Why?"}), "invalid_worked_reviews")

    def test_extra_narrative_keys_rejected(self):
        self.assert_rejected(json.dumps({
            "question_id": "q-pct-8", "steps": ["A."],
            "reasoning_question": "Why?",
            "sections": {"assessment_summary": "x"}}),
            "invalid_worked_reviews")

    def test_markup_rejected(self):
        self.assert_rejected(json.dumps({
            "question_id": "q-pct-8",
            "steps": ["<b>bold</b> step"],
            "reasoning_question": "Why?"}), "invalid_worked_reviews")

    def test_secret_in_steps_rejected(self):
        self.assert_rejected(json.dumps({
            "question_id": "q-pct-8",
            "steps": [f"token {FAKE_KEY}"],
            "reasoning_question": "Why?"}),
            "secret_in_worked_reviews")

    def test_truncated_completion_rejected(self):
        card = make_card()
        opener = FakeOpener(ok_body(card, finish="length"))
        generator = native(opener)
        with self.assertRaises(AittaGenerationError):
            generator.generate(card)
        self.assertEqual(generator._last_metadata["rejection_code"],
                         "completion_truncated")

    def test_transport_failure_metadata(self):
        card = make_card()
        error = urllib.error.URLError("timed out")
        generator = native(FakeOpener(error=error))
        with self.assertRaises(AittaGenerationError):
            generator.generate(card)
        self.assertEqual(generator._last_metadata["status"], "failed")
        self.assertEqual(generator._last_metadata["role"],
                         "selected_worked_example")


class ValidateReplyTests(unittest.TestCase):
    def test_valid_overlay(self):
        reply = {"question_id": "q-pct-8", "steps": ["A.", "B."],
                 "reasoning_question": "Why?"}
        self.assertEqual(validate_reply(reply, make_card()), reply)

    def test_bad_replies(self):
        card = make_card()
        for reply in (None, "x", {"question_id": "other",
                                  "steps": ["A."],
                                  "reasoning_question": "Why?"},
                      {"question_id": "q-pct-8", "steps": ["A."]}):
            self.assertIsNone(validate_reply(reply, card),
                              msg=repr(reply))


class FlakyOpener:
    """Fails once with a retryable timeout, then serves a body."""

    def __init__(self, body):
        self.body = body
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("timed out")
        return FakeResponse(self.body)


class CachedGeneratorTests(unittest.TestCase):
    def _generator(self, cache, opener):
        return CachedWorkedExampleGenerator(
            native(opener), cache, DEFAULT_MODEL, "e" * 64)

    def test_retry_then_success(self):
        card = make_card()
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), 4,
                                 automatic_retries=True,
                                 reuse_captures=False)
            opener = FlakyOpener(ok_body(card))
            generator = self._generator(cache, opener)
            with cache.session_scope("s" * 32):
                overlay = generator.generate(card)
            self.assertEqual(overlay["question_id"], "q-pct-8")
            self.assertEqual(opener.calls, 2)
            execution = generator.execution
            self.assertEqual(
                execution["automatic_retry"]["stop_reason"], "success")

    def test_cached_reply_revalidated(self):
        card = make_card()
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), 4,
                                 automatic_retries=True,
                                 reuse_captures=False)
            bad = json.dumps({"question_id": "other",
                              "steps": ["A."],
                              "reasoning_question": "Why?"})
            opener = FakeOpener(ok_body(card, inner=bad))
            generator = self._generator(cache, opener)
            with cache.session_scope("s" * 32):
                with self.assertRaises(RuntimeError):
                    generator.generate(card)

    def test_same_card_reuses_capture(self):
        card = make_card()
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), 4)
            opener = FakeOpener(ok_body(card))
            generator = self._generator(cache, opener)
            with cache.session_scope("s" * 32):
                first = generator.generate(card)
                second = generator.generate(card)
            self.assertEqual(first, second)
            self.assertEqual(len(opener.requests), 1)
            self.assertTrue(generator.execution["reused"])

    def test_changed_task_context_bypasses_capture(self):
        card = make_card()
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), 4)
            opener = FakeOpener(ok_body(card))
            generator = self._generator(cache, opener)
            with cache.session_scope("s" * 32):
                generator.generate(card)
                generator.generate(
                    make_card(subtopic_id="combine_like_terms"))
            self.assertEqual(len(opener.requests), 2)


if __name__ == "__main__":
    unittest.main()
