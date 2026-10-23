import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

import phase3_paths  # noqa: F401
from aitta_generator import AittaGenerationError, AittaGenerator
from evidence_feedback import (
    build_evidence, canonical_digest, run_feedback, run_multi_feedback)
from evidence_feedback_policy import (
    SCOPE, build_candidates, build_review_bundles)
from full_feedback import (
    CachedFullFeedbackGenerator, FULL_FEEDBACK_INSTRUCTIONS,
    FULL_FEEDBACK_MAX_TOKENS, FULL_FEEDBACK_PROMPT_VERSION,
    FULL_FEEDBACK_SECTION_KEYS, FULL_FEEDBACK_SECTION_MAX_CHARS,
    FULL_FEEDBACK_SECTION_TITLES, FullFeedbackAittaGenerator,
    MULTI_FEEDBACK_GUARDRAILS_VERSION,
    PROVIDER_FULL_NARRATIVE_GUARDRAILS_VERSION,
    _provider_full_sections,
    MULTI_FULL_FEEDBACK_INSTRUCTIONS,
    MULTI_FULL_FEEDBACK_PROMPT_VERSION, MULTI_FULL_FEEDBACK_SCHEMA,
    PLAIN_LANGUAGE_INSTRUCTIONS,
    UNGROUNDED_WORKED_REVIEW_OUTPUT_INSTRUCTIONS,
    build_full_input, build_multi_full_input,
    full_message_sections,
    full_reply_rejection_code, FULL_FEEDBACK_REJECTION_CODES,
    guarded_multi_message, structured_sections,
    validate_full_input, validate_full_reply)
from replay_provider_scenarios import CaptureCache
import feedback_practice
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_feedback_practice import make_graph, make_pool
from unittest import mock

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
               usage=None, finish_reason="stop", content=None,
               worked_reviews=None):
    if sections is None:
        sections = dict(VALID_SECTIONS)
    if content is None:
        reply = {"candidate_id": candidate_id, "sections": sections}
        if worked_reviews is not None:
            reply["worked_reviews"] = worked_reviews
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
                           "feedback_plan", "baseline_sections",
                           "practice_context"})
        self.assertEqual(context["practice_context"],
                         {"status": "disabled", "selection": None})
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
        generator = self._generator(opener)
        with self.assertRaises(AittaGenerationError):
            generator.generate(self.full_input)

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

    def test_empty_overlength_and_markup_rejected(self):
        for key, value in (("assessment_summary", "   "),
                           ("observed_strengths", "x" * 2001),
                           ("next_steps", "Use <b>this</b> tip.")):
            sections = dict(VALID_SECTIONS)
            sections[key] = value
            self._reject(wire_reply(sections=sections,
                                    candidate_id=self.selected_id))

    def test_internal_process_language_is_not_a_validation_failure(self):
        # Internal application terminology is prompted against via the
        # shared plain-language instructions, not a validation gate:
        # flagged sections pass validation unchanged and generation
        # completes without a rejected draft.
        for key in FULL_FEEDBACK_SECTION_KEYS:
            sections = dict(VALID_SECTIONS)
            sections[key] = "This section follows the plan."
            opener = RecordingOpener(
                wire_reply(sections=sections,
                           candidate_id=self.selected_id))
            generator = self._generator(opener)
            reply = generator.generate(self.full_input)
            self.assertEqual(reply["sections"][key], sections[key])
            self.assertEqual(generator.last_metadata["status"],
                             "completed")
            self.assertIsNone(generator.last_rejected_draft)
        for phrase in ("the feedback plan", "a priority group",
                       "the selected candidate", "the selection policy",
                       "routing details", "the system prompt",
                       "Jev routing", "the Aitta system",
                       "according to this plan", "based on our plan",
                       "guided by its plan", "The review plan highlights",
                       "the review plans", "a REVIEW PLAN",
                       "the practice context", "practice contexts",
                       "PRACTICE CONTEXTS"):
            sections = dict(VALID_SECTIONS)
            sections["next_steps"] = f"A note about {phrase}."
            opener = RecordingOpener(
                wire_reply(sections=sections,
                           candidate_id=self.selected_id))
            reply = self._generator(opener).generate(self.full_input)
            self.assertEqual(reply["sections"]["next_steps"],
                             sections["next_steps"])
        for accepted in ("Plan your calculation before answering.",
                         "Review these topics with your teacher.",
                         "Try the selected practice activities below."):
            sections = dict(VALID_SECTIONS)
            sections["next_steps"] = accepted
            opener = RecordingOpener(
                wire_reply(sections=sections,
                           candidate_id=self.selected_id))
            reply = self._generator(opener).generate(self.full_input)
            self.assertEqual(
                reply["sections"]["next_steps"], accepted)

    def test_prompt_version_v8_request_and_isolation(self):
        self.assertEqual(FULL_FEEDBACK_PROMPT_VERSION,
                         "aitta_feedback_with_practice_v8")
        for stale in ("aitta_feedback_with_practice_v2",
                      "aitta_feedback_with_practice_v3",
                      "aitta_feedback_with_practice_v4",
                      "aitta_feedback_with_practice_v5",
                      "aitta_feedback_with_practice_v6",
                      "aitta_feedback_with_practice_v7"):
            with self.subTest(stale=stale):
                payload = copy.deepcopy(self.full_input)
                payload["prompt_version"] = stale
                with self.assertRaises(ValueError):
                    validate_full_input(payload)
        body = self._generator(
            RecordingOpener()).request(self.full_input)
        context = json.loads(body["messages"][1]["content"])
        self.assertEqual(context["prompt_version"],
                         FULL_FEEDBACK_PROMPT_VERSION)
        system = body["messages"][0]["content"]
        self.assertEqual(system, FULL_FEEDBACK_INSTRUCTIONS)
        self.assertIn(PLAIN_LANGUAGE_INSTRUCTIONS, system)
        self.assertIn("Digits and number words are both allowed, "
                      "including 20 and twenty.", system)
        self.assertIn("Every numerical claim must match the supplied "
                      "observed evidence", system)
        for banned in ("numeric digits", "numerical quantities in words",
                       "spell out"):
            self.assertNotIn(banned, system)

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
            generator = self._generator(opener)
            with self.assertRaises(ValueError):
                generator.generate(payload)
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
            "shape_guardrails_not_semantic_verification")

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

    def test_validate_full_reply_accepts_numeric_formats(self):
        for text in ("20 out of 40 assessed answers were correct.",
                     "Twenty out of forty answers were recorded.",
                     "Result ٣ shown for twenty-one items.",
                     "Continue one step at a time.",
                     "Half of the questions were correct."):
            for key in FULL_FEEDBACK_SECTION_KEYS:
                with self.subTest(text=text, key=key):
                    sections = dict(VALID_SECTIONS)
                    sections[key] = text
                    reply = {"candidate_id": self.selected_id,
                             "sections": sections}
                    self.assertIsNotNone(
                        validate_full_reply(reply, self.selected_id))
                    self.assertIsNone(
                        full_reply_rejection_code(
                            reply, self.selected_id))

    def test_validate_full_reply_scope_only_and_scope_strip(self):
        for key in FULL_FEEDBACK_SECTION_KEYS:
            sections = dict(VALID_SECTIONS)
            sections[key] = SCOPE
            with self.subTest(key=key):
                self.assertIsNone(validate_full_reply(
                    {"candidate_id": self.selected_id,
                     "sections": sections}, self.selected_id))
        sections = dict(VALID_SECTIONS)
        sections["observed_strengths"] = (
            SCOPE + "\n\n" + sections["observed_strengths"])
        ok = validate_full_reply(
            {"candidate_id": self.selected_id,
             "sections": sections}, self.selected_id)
        self.assertIsNotNone(ok)
        self.assertEqual(ok["observed_strengths"],
                         VALID_SECTIONS["observed_strengths"])

    def test_full_message_sections_dedup_scope_variants(self):
        from full_feedback import _without_scope
        stretched = SCOPE.replace(" ", "  ")
        self.assertEqual(_without_scope(stretched), "")
        rendered = full_message_sections({
            "assessment_summary": "Completed.",
            "observed_strengths": "Correct work shown.",
            "review_focus": ("Topic A.\n\n" + SCOPE + "\n\n"
                             + stretched + "\n\n" + SCOPE.lower()),
            "next_steps": "Work with your teacher. " + SCOPE})
        texts = {s["kind"]: s["text"] for s in rendered}
        self.assertTrue(texts["review_focus"].endswith(SCOPE))
        self.assertIn("Topic A.", texts["review_focus"])
        self.assertNotIn(SCOPE, texts["next_steps"])
        self.assertIn("Work with your teacher.", texts["next_steps"])
        self.assertEqual(
            "\n\n".join(s["text"] for s in rendered).count(SCOPE), 1)
        again = full_message_sections(texts)
        self.assertEqual({s["kind"]: s["text"] for s in again}, texts)

    def _practice_ctx(self, pool, qid="pool_q1"):
        return {"schema": feedback_practice.PRACTICE_CONTEXT_SCHEMA,
                "status": "selected", "selected_question_id": qid,
                "pool_sha256": canonical_digest(pool)}

    def _patched_pool(self):
        pool = make_pool(("sA", "sB"))
        patches = [
            mock.patch.object(feedback_practice,
                              "load_current_practice_pool",
                              return_value=copy.deepcopy(pool)),
            mock.patch.object(feedback_practice,
                              "teacher_content_context",
                              return_value=make_graph(pool))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return pool

    def test_wire_practice_descriptor_only(self):
        pool = self._patched_pool()
        full_input = build_full_input(
            self.evidence, "student", self.selected,
            self._practice_ctx(pool))
        opener = RecordingOpener(
            wire_reply(candidate_id=self.selected_id))
        self._generator(opener).generate(full_input)
        body = opener.calls[0]["body"]
        context = json.loads(body["messages"][1]["content"])
        practice = context["practice_context"]
        self.assertEqual(set(practice), {"status", "selection"})
        self.assertEqual(practice["status"], "selected")
        self.assertEqual(
            set(practice["selection"]),
            {"skill_name", "concept_name", "mathematical_task"})
        self.assertEqual(practice["selection"]["concept_name"],
                         "Concept sA")
        self.assertEqual(practice["selection"]["mathematical_task"],
                         "Task for sA")
        raw = json.dumps(practice)
        for forbidden in ("pool_q", "Pool practice prompt",
                          "p_correct", "answer_index", "options",
                          "item_id", "sha256"):
            self.assertNotIn(forbidden, raw)

    def test_jev_selection_payload_has_no_practice(self):
        seen = []

        selected_id = self.selected_id

        class Spy:
            def select(self, payload):
                seen.append(copy.deepcopy(payload))
                return {"candidate_id": selected_id}

        class Quiet:
            supports_full_feedback = True

            def generate(self, payload):
                raise RuntimeError("offline")

        pool = self._patched_pool()
        review = run_feedback(
            self.evidence, "student", "end", selector=Spy(),
            generator=Quiet(),
            practice_context=self._practice_ctx(pool))
        self.assertNotIn("practice_context", seen[0])
        self.assertNotIn("practice", json.dumps(seen[0]))
        self.assertEqual(review["selected_candidate_id"],
                         self.selected_id)

    def test_cache_key_differs_by_practice_context(self):
        pool = self._patched_pool()
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), 4)
            opener = RecordingOpener(
                wire_reply(candidate_id=self.selected_id))
            native = self._generator(opener)
            cached = CachedFullFeedbackGenerator(
                native, cache, MODEL, "endpointhash" * 8)
            cached.generate(self.full_input)
            other = build_full_input(
                self.evidence, "student", self.selected,
                self._practice_ctx(pool))
            cached.generate(other)
            self.assertEqual(len(opener.calls), 2)
            self.assertEqual(
                len(list(Path(tmp).glob("aitta_full_*.json"))), 2)

    def test_fallback_next_steps_include_practice_labels(self):
        pool = self._patched_pool()
        ctx = self._practice_ctx(pool)
        review = run_feedback(
            self.evidence, "student", "end",
            practice_context=ctx)
        next_steps = next(s for s in review["message"]["sections"]
                          if s["kind"] == "next_steps")
        expected = feedback_practice.PRACTICE_NEXT_STEP_SELECTED.format(
            concept_name="Concept sA", skill_name="Skill sA")
        self.assertIn(expected, next_steps["text"])
        card = feedback_practice.resolve_practice(ctx,
                                                  self.evidence)["card"]
        self.assertEqual(card["question"]["text"],
                         "Pool practice prompt 1?")
        self.assertEqual(card["question"]["options"],
                         ["one", "two", "three"])

    def test_practice_context_requires_end_checkpoint(self):
        pool = self._patched_pool()
        ctx = self._practice_ctx(pool)
        with self.assertRaises(ValueError):
            run_feedback(self.evidence, "student", "midpoint",
                         practice_context=ctx)

    def _reject_code(self, reply, code):
        opener = RecordingOpener(reply)
        generator = self._generator(opener)
        with self.assertRaises(AittaGenerationError) as caught:
            generator.generate(self.full_input)
        self.assertEqual(str(caught.exception), "Aitta request failed")
        meta = generator.last_metadata
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["rejection_code"], code)
        self.assertEqual(meta["transport"]["stage"], "body_received")
        self.assertEqual(
            meta["failure_stage"],
            "response_contract_or_full_feedback_validation")
        return generator

    def test_rejection_codes_envelope_and_choices(self):
        sid = self.selected_id
        for data, code in (
                (None, "invalid_choices"),
                ({}, "invalid_choices"),
                (dict(wire_reply(candidate_id=sid), choices=[]),
                 "invalid_choices"),
                (dict(wire_reply(candidate_id=sid),
                      choices=[{"finish_reason": "stop"}, {}]),
                 "invalid_choices"),
                (dict(wire_reply(candidate_id=sid), choices=[5]),
                 "invalid_choice"),
                (wire_reply(candidate_id=sid, finish_reason="length"),
                 "completion_truncated"),
                (wire_reply(candidate_id=sid, finish_reason="stop_words"),
                 "completion_not_stopped"),
                (wire_reply(candidate_id=sid, finish_reason=None),
                 "completion_not_stopped"),
                (wire_reply(candidate_id=sid, content="{not json"),
                 "invalid_content_json")):
            with self.subTest(code=code):
                self._reject_code(data, code)
        for mutate in (
                lambda d: d["choices"][0].pop("message"),
                lambda d: d["choices"][0].update(message=None),
                lambda d: d["choices"][0].update(message={"content": 42}),
                lambda d: d["choices"][0]["message"].pop("content")):
            data = wire_reply(candidate_id=sid)
            mutate(data)
            with self.subTest(mutate=mutate):
                self._reject_code(data, "invalid_message_content")
        bad_reply = json.dumps({"candidate_id": sid})
        self._reject_code(
            wire_reply(content=bad_reply), "invalid_reply_fields")
        extra_reply = json.dumps(
            {"candidate_id": sid, "sections": dict(VALID_SECTIONS),
             "confidence": "high"})
        self._reject_code(
            wire_reply(content=extra_reply), "invalid_reply_fields")
        self._reject_code(
            wire_reply(candidate_id="someone_else"),
            "candidate_id_mismatch")
        missing = dict(VALID_SECTIONS)
        missing.pop("next_steps")
        self._reject_code(
            wire_reply(sections=missing, candidate_id=sid),
            "invalid_section_fields")

    def test_rejection_codes_per_section(self):
        room = FULL_FEEDBACK_SECTION_MAX_CHARS
        bad_values = (
            (None, "invalid_section_type"),
            (42, "invalid_section_type"),
            ("   ", "empty_section"),
            ("x" * (room + 1), "section_too_long"),
            ("Invalid control\x01character.", "control_character"),
            ("Use <b>this</b> tip.", "markup"))
        for key in FULL_FEEDBACK_SECTION_KEYS:
            for value, reason in bad_values:
                sections = dict(VALID_SECTIONS)
                sections[key] = value
                with self.subTest(key=key, reason=reason):
                    self._reject_code(
                        wire_reply(sections=sections,
                                   candidate_id=self.selected_id),
                        f"{reason}_in_{key}")

    def test_review_focus_scope_overflow_code(self):
        room = FULL_FEEDBACK_SECTION_MAX_CHARS - len("\n\n" + SCOPE)
        sections = dict(VALID_SECTIONS)
        sections["review_focus"] = "x" * room
        opener = RecordingOpener(
            wire_reply(sections=sections, candidate_id=self.selected_id))
        reply = self._generator(opener).generate(self.full_input)
        self.assertEqual(reply["candidate_id"], self.selected_id)
        sections["review_focus"] = "x" * (room + 1)
        self._reject_code(
            wire_reply(sections=sections, candidate_id=self.selected_id),
            "review_focus_scope_too_long")

    def test_secret_rejection_codes(self):
        sid = self.selected_id
        self._reject_code(
            wire_reply(candidate_id=sid, model=f"leak-{API_KEY}"),
            "secret_in_model_metadata")
        for bad_model in ("", "bad model", 5, "m" * 121):
            with self.subTest(model=bad_model):
                self._reject_code(
                    wire_reply(candidate_id=sid, model=bad_model),
                    "invalid_model_metadata")
        key = "syntheticfakesecretkeyvalue"
        sections = dict(VALID_SECTIONS)
        sections["observed_strengths"] = f"Please note {key} for review."
        opener = RecordingOpener(
            wire_reply(sections=sections, candidate_id=sid))
        inner = AittaGenerator(key, BASE_URL, model=MODEL, opener=opener)
        generator = FullFeedbackAittaGenerator(inner)
        with self.assertRaises(AittaGenerationError):
            generator.generate(self.full_input)
        self.assertEqual(generator.last_metadata["rejection_code"],
                         "secret_in_sections")

    def test_usage_rejection_codes(self):
        sid = self.selected_id
        for mutate in (
                lambda d: d.pop("usage"),
                lambda d: d.update(usage={}),
                lambda d: d.update(usage={"prompt_tokens": True,
                                          "completion_tokens": 20,
                                          "total_tokens": 30}),
                lambda d: d.update(usage={"prompt_tokens": -1,
                                          "completion_tokens": 20,
                                          "total_tokens": 30}),
                lambda d: d.update(usage={"prompt_tokens": 10,
                                          "completion_tokens": 20})):
            data = wire_reply(candidate_id=sid)
            mutate(data)
            with self.subTest(mutate=mutate):
                self._reject_code(data, "invalid_usage_metadata")
        data = wire_reply(candidate_id=sid)
        data["usage"] = {"prompt_tokens": 10, "completion_tokens": 20,
                         "total_tokens": 99}
        self._reject_code(data, "usage_total_mismatch")

    def test_decode_and_transport_failure_codes(self):
        generator = self._generator(BytesOpener(b"\xff\xfe{not utf8"))
        with self.assertRaises(AittaGenerationError) as caught:
            generator.generate(self.full_input)
        self.assertEqual(str(caught.exception), "Aitta request failed")
        meta = generator.last_metadata
        self.assertEqual(meta["rejection_code"],
                         "response_decode_or_size")
        self.assertEqual(
            meta["failure_stage"],
            "response_contract_or_full_feedback_validation")
        generator = self._generator(
            RecordingOpener(error=TimeoutError("dial timed out")))
        with self.assertRaises(AittaGenerationError) as caught:
            generator.generate(self.full_input)
        self.assertEqual(str(caught.exception), "Aitta request failed")
        meta = generator.last_metadata
        self.assertEqual(meta["rejection_code"], "transport_failure")
        self.assertEqual(meta["failure_stage"], "open_failed")
        self.assertNotIn("dial timed out", json.dumps(meta))

    def test_rejection_metadata_never_leaks_sentinels(self):
        sentinel = "SENTINELSECRETVALUE"
        cases = [
            RecordingOpener(wire_reply(
                candidate_id=self.selected_id,
                content=json.dumps({"candidate_id": self.selected_id,
                                    "sections": dict(VALID_SECTIONS),
                                    sentinel: sentinel}))),
            RecordingOpener(wire_reply(
                candidate_id=self.selected_id,
                content=f"{{broken {sentinel}")),
            RecordingOpener(error=RuntimeError(
                f"provider said {sentinel}"))]
        fixed = {"status", "model_version", "usage", "prompt_version",
                 "role", "transport", "failure_stage", "rejection_code"}
        for opener in cases:
            with self.subTest(opener=opener):
                generator = self._generator(opener)
                with self.assertRaises(AittaGenerationError) as caught:
                    generator.generate(self.full_input)
                self.assertEqual(str(caught.exception),
                                 "Aitta request failed")
                self.assertNotIn(sentinel, str(caught.exception))
                meta = generator.last_metadata
                self.assertEqual(set(meta), fixed)
                self.assertIn(meta["rejection_code"],
                              FULL_FEEDBACK_REJECTION_CODES)
                self.assertNotIn(sentinel, json.dumps(meta))

    def test_success_after_failure_clears_rejection_code(self):
        opener = RecordingOpener(
            wire_reply(candidate_id="someone_else"))
        generator = self._generator(opener)
        with self.assertRaises(AittaGenerationError):
            generator.generate(self.full_input)
        self.assertEqual(generator.last_metadata["rejection_code"],
                         "candidate_id_mismatch")
        opener.payload = wire_reply(candidate_id=self.selected_id)
        reply = generator.generate(self.full_input)
        self.assertEqual(reply["candidate_id"], self.selected_id)
        meta = generator.last_metadata
        self.assertEqual(meta["status"], "completed")
        self.assertNotIn("rejection_code", meta)

    def test_rejection_code_parity_with_validator(self):
        sid = self.selected_id
        replies = [
            None, [], "x", {}, {"candidate_id": sid},
            {"candidate_id": "other",
             "sections": dict(VALID_SECTIONS)},
            {"candidate_id": sid, "sections": None},
            {"candidate_id": sid,
             "sections": dict(VALID_SECTIONS, extra="x")},
            {"candidate_id": sid, "sections": dict(VALID_SECTIONS)},
        ]
        for key in FULL_FEEDBACK_SECTION_KEYS:
            for bad in (None, 5, "   ", "x" * 2001, "bad\x01char",
                        "value \u0663", "<b>x</b>", "One thing.",
                        "follows the plan.", "plain valid prose."):
                sections = dict(VALID_SECTIONS)
                sections[key] = bad
                replies.append({"candidate_id": sid,
                                "sections": sections})
        room = FULL_FEEDBACK_SECTION_MAX_CHARS - len("\n\n" + SCOPE)
        for length in (room, room + 1):
            sections = dict(VALID_SECTIONS)
            sections["review_focus"] = "x" * length
            replies.append({"candidate_id": sid,
                            "sections": sections})
        for reply in replies:
            with self.subTest(reply=reply):
                self.assertEqual(
                    full_reply_rejection_code(reply, sid) is None,
                    validate_full_reply(reply, sid) is not None)

    def test_failed_capture_retains_rejection_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), 4)
            bad = dict(VALID_SECTIONS)
            bad["next_steps"] = "Take <b>one</b> quick step."
            opener = RecordingOpener(
                wire_reply(sections=bad,
                           candidate_id=self.selected_id))
            native = self._generator(opener)
            cached = CachedFullFeedbackGenerator(
                native, cache, MODEL, "endpointhash" * 8)
            with self.assertRaises(RuntimeError):
                cached.generate(self.full_input)
            self.assertFalse(cached.execution["reused"])
            path = next(Path(tmp).glob("aitta_full_*.json"))
            record = json.loads(path.read_text())
            self.assertIsNone(record["reply"])
            self.assertEqual(record["metadata"]["rejection_code"],
                             "markup_in_next_steps")
            self.assertEqual(
                cached.execution["metadata"]["rejection_code"],
                "markup_in_next_steps")
            with self.assertRaises(RuntimeError):
                cached.generate(copy.deepcopy(self.full_input))
            self.assertTrue(cached.execution["reused"])
            self.assertEqual(
                cached.execution["metadata"]["rejection_code"],
                "markup_in_next_steps")
            self.assertEqual(len(opener.calls), 1)
            self.assertEqual(
                len(list(Path(tmp).glob("aitta_full_*.json"))), 1)
            record["metadata"].pop("rejection_code")
            path.write_text(json.dumps(record))
            with self.assertRaises(RuntimeError):
                cached.generate(copy.deepcopy(self.full_input))
            self.assertTrue(cached.execution["reused"])
            self.assertNotIn("rejection_code",
                             cached.execution["metadata"])

    def test_thread_local_rejection_codes(self):
        generator = self._generator(RecordingOpener(
            wire_reply(candidate_id=self.selected_id)))
        bad_choices = dict(wire_reply(candidate_id=self.selected_id),
                           choices=[])
        truncated = wire_reply(candidate_id=self.selected_id,
                               finish_reason="length")
        barrier = threading.Barrier(2)
        codes = {}

        def worker(name, data):
            barrier.wait(timeout=10)
            with self.assertRaises(AittaGenerationError):
                generator._parse(data, self.selected_id)
            codes[name] = generator.last_metadata["rejection_code"]

        threads = [threading.Thread(
            target=worker, args=(name, data))
            for name, data in (("a", bad_choices), ("b", truncated))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(codes["a"], "invalid_choices")
        self.assertEqual(codes["b"], "completion_truncated")


class BytesResponse(FakeResponse):
    def __init__(self, raw):
        self._payload = raw
        self.closed = False


class BytesOpener:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append(request)
        return BytesResponse(self.raw)


class MultiFullFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.evidence = build_evidence(
            self.bank, self.taxonomy,
            responses(self.bank, correct=False, count=40))
        self.bundle = build_review_bundles(self.evidence)[0]
        self.slots = [feedback_practice.empty_context("abstained")
                      for _ in self.bundle["review_candidates"]]
        self.full_input = build_multi_full_input(
            self.evidence, "student", self.bundle, self.slots)

    def _generator(self, opener):
        inner = AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                               opener=opener)
        return FullFeedbackAittaGenerator(inner)

    def test_build_multi_input_schema_and_fields(self):
        payload = self.full_input
        self.assertEqual(payload["schema"], MULTI_FULL_FEEDBACK_SCHEMA)
        self.assertEqual(payload["audience"], "student")
        self.assertEqual(payload["checkpoint"], "end")
        self.assertEqual(payload["prompt_version"],
                         MULTI_FULL_FEEDBACK_PROMPT_VERSION)
        self.assertEqual(payload["selected_candidate"], self.bundle)
        self.assertEqual(
            len(payload["feedback_plans"]),
            len(self.bundle["review_candidates"]))
        self.assertEqual(
            len(payload["practice_contexts"]),
            len(self.bundle["review_candidates"]))
        self.assertLessEqual(
            len(payload["review_examples"]),
            len(self.bundle["review_candidates"]))
        self.assertEqual(
            {s["kind"] for s in payload["baseline_sections"]},
            set(FULL_FEEDBACK_SECTION_KEYS))
        self.assertIn(
            SCOPE, next(s["text"] for s in payload["baseline_sections"]
                        if s["kind"] == "review_focus"))

    def test_validate_multi_accepts_and_rebuild_deterministic(self):
        normalized = validate_full_input(copy.deepcopy(self.full_input))
        self.assertEqual(normalized, self.full_input)

    def test_rebuilt_validation_preserves_first_error(self):
        payload = copy.deepcopy(self.full_input)
        payload["feedback_plans"] = []
        payload["baseline_sections"] = []
        with self.assertRaisesRegex(ValueError, "^feedback_plans must match the selected bundle$"):
            validate_full_input(payload)

    def test_validate_multi_rejects_tampered_fields(self):
        for mutate in (
                lambda p: p["feedback_plans"].pop(),
                lambda p: p["practice_contexts"].pop(),
                lambda p: p["practice_contexts"][0].update(
                    status="selected"),
                lambda p: p.update(extra="x"),
                lambda p: p["evidence"].update(answers=[]),
                lambda p: p["selected_candidate"].update(
                    candidate_id="review_bundle_forged"),
                lambda p: p["review_examples"].append({})):
            payload = copy.deepcopy(self.full_input)
            mutate(payload)
            with self.assertRaises(ValueError):
                validate_full_input(payload)

    def test_wire_safe_no_private_ids_or_probabilities(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=self.bundle["candidate_id"]))
        self._generator(opener).generate(copy.deepcopy(self.full_input))
        body = opener.calls[0]["body"]
        context = json.loads(body["messages"][1]["content"])
        self.assertEqual(context["schema"], MULTI_FULL_FEEDBACK_SCHEMA)
        self.assertEqual(context["prompt_version"],
                         MULTI_FULL_FEEDBACK_PROMPT_VERSION)
        for ctx in context["practice_contexts"]:
            self.assertLessEqual(
                set(ctx), {"status", "topic", "selection"})
        raw = json.dumps(context)
        for forbidden in ("pool_q", "Pool practice prompt",
                          "p_correct", '"answer_index"',
                          '"selected_index"', '"assessment_context"',
                          '"responses"', "item_id", "sha256"):
            self.assertNotIn(forbidden, raw)

    def test_v12_prompt_wording_contract(self):
        self.assertEqual(
            self.full_input["prompt_version"],
            "aitta_assessment_grounded_two_topic_feedback_v12")
        opener = RecordingOpener(
            wire_reply(candidate_id=self.bundle["candidate_id"]))
        reply = self._generator(opener).generate(self.full_input)
        self.assertEqual(set(reply["sections"]),
                         set(FULL_FEEDBACK_SECTION_KEYS))
        body = opener.calls[0]["body"]
        system = body["messages"][0]["content"]
        # Ungrounded multi requests append the legacy two-field output
        # contract; grounded requests use the worked-review contract.
        self.assertEqual(system,
                         MULTI_FULL_FEEDBACK_INSTRUCTIONS
                         + UNGROUNDED_WORKED_REVIEW_OUTPUT_INSTRUCTIONS)
        self.assertIn(PLAIN_LANGUAGE_INSTRUCTIONS, system)
        for needle in (
                "write 'On this assessment, ...'",
                "separate topic names clearly",
                "Give this teacher-review instruction once",
                "Call these 'separate practice activities', not "
                "'similar practice tasks'",
                "shown on each practice card",
                "The application owns the final observed-strengths "
                "list",
                "Digits and number words are both allowed, including "
                "20 and twenty."):
            self.assertIn(needle, system)
        for banned in ("numeric digits", "spell out"):
            self.assertNotIn(banned, system)
        user = json.loads(body["messages"][1]["content"])
        self.assertEqual(
            user["prompt_version"],
            "aitta_assessment_grounded_two_topic_feedback_v12")

    def test_stale_prompt_versions_rejected_before_request(self):
        for stale in (
                "aitta_assessment_grounded_two_topic_feedback_v3",
                "aitta_assessment_grounded_two_topic_feedback_v4",
                "aitta_assessment_grounded_two_topic_feedback_v5",
                "aitta_assessment_grounded_two_topic_feedback_v6",
                "aitta_assessment_grounded_two_topic_feedback_v7",
                "aitta_assessment_grounded_two_topic_feedback_v8",
                "aitta_assessment_grounded_two_topic_feedback_v9",
                "aitta_assessment_grounded_two_topic_feedback_v10",
                "aitta_assessment_grounded_two_topic_feedback_v11"):
            with self.subTest(stale=stale):
                payload = copy.deepcopy(self.full_input)
                payload["prompt_version"] = stale
                opener = RecordingOpener(
                    wire_reply(
                        candidate_id=self.bundle["candidate_id"]))
                generator = self._generator(opener)
                with self.assertRaises(ValueError):
                    generator.generate(payload)
                self.assertEqual(opener.calls, [])
                self.assertEqual(generator.last_metadata["status"],
                                 "not_called")

    def test_run_multi_feedback_records_generated_sections(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=self.bundle["candidate_id"]))
        review = run_multi_feedback(
            self.evidence,
            generator=self._generator(opener),
            practice_contexts=self.slots)
        self.assertEqual(len(opener.calls), 1)
        trace = review["trace"]
        self.assertEqual(trace["phrasing_source"],
                         "injected_generator_full_sections")
        self.assertEqual(trace["guardrails_version"],
                         MULTI_FEEDBACK_GUARDRAILS_VERSION)
        self.assertEqual(
            trace["section_sources"],
            {"assessment_summary": "provider",
             "observed_strengths": "application",
             "review_focus": "application",
             "next_steps": "application"})
        texts = {s["kind"]: s["text"]
                 for s in review["message"]["sections"]}
        self.assertTrue(texts["assessment_summary"].startswith(
            VALID_SECTIONS["assessment_summary"]))
        self.assertNotEqual(texts["observed_strengths"],
                            VALID_SECTIONS["observed_strengths"])
        self.assertTrue(texts["observed_strengths"].startswith(
            "No correct answers were recorded"))
        self.assertEqual(
            review["message"]["text"].count(SCOPE), 1)
        self.assertIsNone(trace["fallback_reason"])
        self.assertEqual(
            trace["generation_prompt_version"],
            MULTI_FULL_FEEDBACK_PROMPT_VERSION)

    def test_all_correct_empty_contexts_and_generation(self):
        evidence = build_evidence(
            self.bank, self.taxonomy, responses(self.bank))
        bundle = build_review_bundles(evidence)[0]
        self.assertTrue(all(c["focus"] is None
                            for c in bundle["review_candidates"]))
        payload = build_multi_full_input(
            evidence, "student", bundle, [])
        normalized = validate_full_input(copy.deepcopy(payload))
        self.assertEqual(normalized["practice_contexts"], [])
        opener = RecordingOpener(
            wire_reply(candidate_id=bundle["candidate_id"]))
        review = run_multi_feedback(
            evidence,
            generator=self._generator(opener),
            practice_contexts=[])
        self.assertEqual(len(opener.calls), 1)
        self.assertIsNone(review["trace"]["fallback_reason"])
        self.assertEqual(
            review["trace"]["guardrails_version"],
            MULTI_FEEDBACK_GUARDRAILS_VERSION)
        self.assertEqual(
            review["trace"]["section_sources"]["assessment_summary"],
            "provider")
        texts = {s["kind"]: s["text"]
                 for s in review["message"]["sections"]}
        self.assertTrue(texts["assessment_summary"].startswith(
            VALID_SECTIONS["assessment_summary"]))
        self.assertIn("no error-based review priority",
                      texts["review_focus"])

    def test_generator_failure_falls_back_to_baseline(self):
        opener = RecordingOpener(error=RuntimeError("down"))
        review = run_multi_feedback(
            self.evidence,
            generator=self._generator(opener),
            practice_contexts=self.slots)
        self.assertEqual(review["message"],
                         review["template_baseline"])
        self.assertEqual(review["trace"]["fallback_reason"],
                         "generator_error")

    def _selected_ctx(self, pool, qid):
        return {"schema": feedback_practice.PRACTICE_CONTEXT_SCHEMA,
                "status": "selected", "selected_question_id": qid,
                "pool_sha256": canonical_digest(pool)}

    def _patch_pool(self, pool):
        import research_runtime
        for patch in (
                mock.patch.object(
                    feedback_practice, "load_current_practice_pool",
                    return_value=copy.deepcopy(pool)),
                mock.patch.object(
                    research_runtime, "load_current_practice_pool",
                    return_value=copy.deepcopy(pool)),
                mock.patch.object(
                    feedback_practice, "teacher_content_context",
                    return_value=make_graph(pool))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_guarded_message_replaces_adversarial_factual_sections(self):
        from evidence_feedback import validate_evidence
        safe = validate_evidence(self.evidence)
        originals = self.bundle["review_candidates"]
        generated = [
            {"kind": "assessment_summary",
             "text": "You have completed this assessment.\n\n" + SCOPE},
            {"kind": "observed_strengths",
             "text": "Mastery of fractions percentages shown"},
            {"kind": "review_focus",
             "text": "Review 3 of 10 incorrect answers now"},
            {"kind": "next_steps",
             "text": "Try similar practice tasks on the same "
                     "concepts."}]
        pristine = copy.deepcopy(originals)
        message = guarded_multi_message(
            safe, originals, practice_contexts=self.slots,
            generated_sections=generated)
        self.assertEqual(originals, pristine)
        self.assertEqual(message["guardrails_version"],
                         MULTI_FEEDBACK_GUARDRAILS_VERSION)
        self.assertEqual(
            message["section_sources"],
            {"assessment_summary": "provider",
             "observed_strengths": "application",
             "review_focus": "application",
             "next_steps": "application"})
        texts = {s["kind"]: s["text"] for s in message["sections"]}
        self.assertEqual(texts["assessment_summary"],
                         "You have completed this assessment.")
        self.assertNotIn("Mastery of fractions",
                         texts["observed_strengths"])
        self.assertIn("No correct answers were recorded",
                      texts["observed_strengths"])
        self.assertNotIn("3 of 10", texts["review_focus"])
        self.assertIn("Possible review topics", texts["review_focus"])
        self.assertIn("not a uniquely weakest area",
                      texts["review_focus"])
        self.assertNotIn("similar practice tasks", texts["next_steps"])
        self.assertNotIn("same concepts", texts["next_steps"])
        self.assertNotIn("separate practice", texts["next_steps"])
        self.assertEqual(
            message["text"].count(SCOPE), 1)

    def test_provider_full_narrative_accepts_only_valid_bundle(self):
        from evidence_feedback import validate_evidence
        safe = validate_evidence(self.evidence)
        originals = self.bundle["review_candidates"]

        def narrative(**overrides):
            texts = {key: f"Draft {key} narrative for educator review."
                     for key in FULL_FEEDBACK_SECTION_KEYS}
            texts.update(overrides)
            return [{"kind": key, "title": title, "text": texts[key]}
                    for key, title in zip(FULL_FEEDBACK_SECTION_KEYS,
                                          FULL_FEEDBACK_SECTION_TITLES)]

        valid = narrative()
        checked = _provider_full_sections(valid)
        self.assertEqual(
            set(checked), set(FULL_FEEDBACK_SECTION_KEYS))
        message = guarded_multi_message(
            safe, originals, practice_contexts=self.slots,
            generated_sections=valid, provider_full_narrative=True)
        self.assertEqual(message["guardrails_version"],
                         PROVIDER_FULL_NARRATIVE_GUARDRAILS_VERSION)
        self.assertTrue(all(
            source == "provider"
            for source in message["section_sources"].values()))
        texts = {s["kind"]: s["text"] for s in message["sections"]}
        self.assertEqual(message["text"].count(SCOPE), 1)
        self.assertTrue(texts["review_focus"].endswith(SCOPE))
        self.assertNotIn("separate practice", texts["next_steps"])

        partial = valid[:-1]
        out_of_order = list(reversed(valid))
        duplicated = copy.deepcopy(valid)
        duplicated[1] = dict(duplicated[0])
        wrong_kind = narrative()
        wrong_kind[0]["kind"] = "forged"
        bad_text = narrative(
            review_focus="Review <b>incorrect</b> answers now")
        missing_title = narrative()
        del missing_title[2]["title"]
        # A provider-supplied scope notice is stripped and the canonical
        # application scope is appended exactly once instead.
        scoped = narrative(
            observed_strengths="Strengths.\n\n" + SCOPE)
        scoped_message = guarded_multi_message(
            safe, originals, practice_contexts=self.slots,
            generated_sections=scoped, provider_full_narrative=True)
        self.assertEqual(scoped_message["guardrails_version"],
                         PROVIDER_FULL_NARRATIVE_GUARDRAILS_VERSION)
        self.assertEqual(scoped_message["text"].count(SCOPE), 1)
        for bad in (None, "text", [], partial, out_of_order, duplicated,
                    wrong_kind, bad_text, missing_title):
            self.assertIsNone(_provider_full_sections(bad), repr(bad))
            fallback = guarded_multi_message(
                safe, originals, practice_contexts=self.slots,
                generated_sections=bad, provider_full_narrative=True)
            self.assertEqual(fallback["guardrails_version"],
                             MULTI_FEEDBACK_GUARDRAILS_VERSION, repr(bad))
            self.assertTrue(all(
                source == "application"
                for source in fallback["section_sources"].values()),
                repr(bad))

    def test_guarded_grounded_unavailable_examples(self):
        from evidence_feedback import validate_evidence
        from assessment_worked_review import (
            build_assessment_review_examples)
        rows = responses(self.bank, correct=False, count=40)
        safe = validate_evidence(self.evidence)
        originals = self.bundle["review_candidates"]
        context = {"bank": self.bank, "taxonomy": self.taxonomy,
                   "responses": rows}
        examples = build_assessment_review_examples(
            safe, originals, context)
        self.assertEqual([e["status"] for e in examples],
                         ["unavailable", "unavailable"])
        message = guarded_multi_message(
            safe, originals, review_examples=examples,
            practice_contexts=self.slots)
        texts = {s["kind"]: s["text"] for s in message["sections"]}
        self.assertEqual(
            texts["next_steps"].count(
                "a checked worked solution is not available"), 2)
        self.assertNotIn("checked worked steps",
                         texts["next_steps"])
        self.assertIn("No correct answers were recorded",
                      texts["observed_strengths"])
        self.assertEqual(
            message["section_sources"]["observed_strengths"],
            "application")

    def test_guarded_practice_follow_up_card_wording(self):
        from evidence_feedback import validate_evidence
        pool = make_pool(("sA", "sB"))
        self._patch_pool(pool)
        safe = validate_evidence(self.evidence)
        originals = self.bundle["review_candidates"]
        one = guarded_multi_message(
            safe, originals,
            practice_contexts=[
                self._selected_ctx(pool, "pool_q1"),
                feedback_practice.empty_context("abstained")])
        texts = {s["kind"]: s["text"] for s in one["sections"]}
        self.assertIn(
            "If your teacher approves, try the separate practice "
            "activity shown below. Its card explains whether it "
            "covers the review content or related content.",
            texts["next_steps"])
        self.assertNotIn("same concepts", texts["next_steps"])
        both = guarded_multi_message(
            safe, originals,
            practice_contexts=[
                self._selected_ctx(pool, "pool_q1"),
                self._selected_ctx(pool, "pool_q2")])
        joined = next(s["text"] for s in both["sections"]
                      if s["kind"] == "next_steps")
        self.assertIn("separate practice activities shown below. "
                      "Each card explains", joined)
        self.assertEqual(joined.count("If your teacher approves"), 1)
        abstained = guarded_multi_message(
            safe, originals, practice_contexts=self.slots)
        self.assertNotIn(
            "practice activit",
            next(s["text"] for s in abstained["sections"]
                 if s["kind"] == "next_steps"))
        invalid_first = [self._selected_ctx(pool, "pool_q1")]
        with self.assertRaises(ValueError):
            guarded_multi_message(
                safe, originals,
                practice_contexts=invalid_first)
        invalid_second = [
            self._selected_ctx(pool, "pool_q2"),
            feedback_practice.empty_context("abstained")]
        with self.assertRaises(ValueError):
            guarded_multi_message(
                safe, originals,
                practice_contexts=invalid_second)

    def test_cached_generator_enforces_guardrails_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), None)
            opener = RecordingOpener(
                wire_reply(candidate_id=self.bundle["candidate_id"]))
            native = self._generator(opener)
            cached = CachedFullFeedbackGenerator(
                native, cache, MODEL, "endpointhash" * 8)
            first = run_multi_feedback(
                self.evidence, generator=cached,
                practice_contexts=self.slots)
            self.assertEqual(len(opener.calls), 1)
            self.assertFalse(cached.execution["reused"])
            second = run_multi_feedback(
                self.evidence, generator=cached,
                practice_contexts=self.slots)
            self.assertEqual(len(opener.calls), 1)
            self.assertTrue(cached.execution["reused"])
            for review in (first, second):
                trace = review["trace"]
                self.assertEqual(
                    trace["phrasing_source"],
                    "injected_generator_full_sections")
                self.assertEqual(trace["guardrails_version"],
                                 MULTI_FEEDBACK_GUARDRAILS_VERSION)
                self.assertEqual(
                    trace["section_sources"],
                    {"assessment_summary": "provider",
                     "observed_strengths": "application",
                     "review_focus": "application",
                     "next_steps": "application"})
                self.assertEqual(
                    review["message"]["text"].count(SCOPE), 1)

    def test_spelled_quantity_reply_accepted_guarded(self):
        good = dict(VALID_SECTIONS)
        good["observed_strengths"] = (
            "Ten of ten assessed answers were incorrect.")
        opener = RecordingOpener(
            wire_reply(sections=good,
                       candidate_id=self.bundle["candidate_id"]))
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), None)
            cached = CachedFullFeedbackGenerator(
                self._generator(opener), cache, MODEL,
                "endpointhash" * 8)
            review = run_multi_feedback(
                self.evidence, generator=cached,
                practice_contexts=self.slots)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(review["trace"]["phrasing_source"],
                         "injected_generator_full_sections")
        self.assertIsNone(review["trace"]["fallback_reason"])
        self.assertEqual(review["trace"]["guardrails_version"],
                         MULTI_FEEDBACK_GUARDRAILS_VERSION)
        self.assertEqual(
            review["trace"]["section_sources"],
            {"assessment_summary": "provider",
             "observed_strengths": "application",
             "review_focus": "application",
             "next_steps": "application"})
        # Spelled-quantity wording in an application-owned section is
        # accepted but never reaches the displayed message.
        self.assertNotIn(
            "Ten of ten", review["message"]["text"])


class MultiBaselineSectionsRegressionTests(unittest.TestCase):
    def _cand(self, sub_id, sub_name, incorrect, out_of,
              pattern="recurring_errors", tied=None,
              strategy="supported_review", action=None, focus=True):
        candidate = {"candidate_id": f"review_{sub_id}",
                     "focus": None,
                     "planning": {"error_pattern": pattern,
                                  "tied_candidate_ids": tied or []},
                     "strategy": strategy,
                     "action": action or f"Action for {sub_name}."}
        if focus:
            candidate["focus"] = {
                "subtopic_id": sub_id, "subtopic_name": sub_name,
                "incorrect": incorrect, "out_of": out_of}
        return candidate

    def _texts(self, sections):
        return {s["kind"]: s["text"] for s in sections}

    def test_all_correct_optional_candidate(self):
        from full_feedback import multi_baseline_sections
        evidence = {"skills": [{"skill_name": "Murtoluvut", "correct": 40},
                               {"skill_name": "Prosenttilaskenta",
                                "correct": 10}]}
        texts = self._texts(multi_baseline_sections(
            evidence, [self._cand("x", "x", 0, 0, focus=False,
                                  action="Celebrate.")]))
        self.assertEqual(
            texts["observed_strengths"],
            "On this assessment, you answered questions correctly in "
            "fractions, and in percentages.")
        self.assertEqual(
            texts["review_focus"],
            "There is no error-based review priority on this assessment."
            "\n\n" + SCOPE)
        self.assertEqual(texts["next_steps"], "Celebrate.")

    def test_two_error_focus_supported_and_other(self):
        from full_feedback import multi_baseline_sections
        evidence = {"skills": [{"skill_name": "Murtoluvut", "correct": 5},
                               {"skill_name": "Prosenttilaskenta",
                                "correct": 3}]}
        texts = self._texts(multi_baseline_sections(
            evidence,
            [self._cand("percentage_amount", "Percentage of an amount",
                        3, 10),
             self._cand("divisibility", "Divisibility", 2, 8,
                        strategy="worked_example")]))
        self.assertEqual(
            texts["review_focus"],
            "Possible review topics:\n\n"
            "Percentage of an amount.\n\nDivisibility.\n\n" + SCOPE)
        self.assertEqual(
            texts["next_steps"],
            "With your teacher, work through the example for Percentage "
            "of an amount below and explain the method.\n\n"
            "Review the example for Divisibility below, explain each "
            "step, and ask your teacher to check your reasoning.")

    def test_isolated_limited_tied_and_unknown_action(self):
        from full_feedback import multi_baseline_sections
        evidence = {"skills": [{"skill_name": "Murtoluvut", "correct": 5},
                               {"skill_name": "Prosenttilaskenta",
                                "correct": 3}]}
        texts = self._texts(multi_baseline_sections(
            evidence,
            [self._cand("percentage_amount", "Percentage of an amount",
                        1, 5, pattern="isolated_incorrect_answer",
                        tied=["a", "b"]),
             self._cand("unknown_sub", "Unknown topic", 1, 1,
                        tied=["c", "d"], action="Custom action.")]))
        self.assertEqual(
            texts["review_focus"],
            "Possible review topics:\n\n"
            "Percentage of an amount. This is an isolated incorrect "
            "answer, not evidence of a consistent difficulty. Other "
            "options share this display rank; this is not a uniquely "
            "weakest area.\n\nUnknown topic. Only one question assessed "
            "this content. Other options share this display rank; this "
            "is not a uniquely weakest area.\n\n" + SCOPE)
        self.assertEqual(
            texts["next_steps"],
            "With your teacher, work through the example for Percentage "
            "of an amount below and explain the method.\n\n"
            "Custom action.")

    def test_no_correct_answers(self):
        from full_feedback import multi_baseline_sections
        evidence = {"skills": [{"skill_name": "Murtoluvut", "correct": 0},
                               {"skill_name": "Prosenttilaskenta",
                                "correct": 0}]}
        texts = self._texts(multi_baseline_sections(
            evidence,
            [self._cand("prime_recognition", "Prime recognition", 4, 10,
                        strategy="unsupported_review")]))
        self.assertEqual(
            texts["observed_strengths"],
            "No correct answers were recorded on this assessment. "
            "Work through the review examples with support.")
        self.assertEqual(
            texts["next_steps"],
            "Review the example for Prime recognition below, explain "
            "each step, and ask your teacher to check your reasoning.")
        self.assertEqual(texts["review_focus"].count(SCOPE), 1)

    def test_shared_grounded_instruction_once_same_support(self):
        from full_feedback import multi_baseline_sections
        evidence = {"skills": [{"skill_name": "Murtoluvut",
                                "correct": 5}]}
        for strategy in ("supported_review", "focused_review"):
            with self.subTest(strategy=strategy):
                texts = self._texts(multi_baseline_sections(
                    evidence,
                    [self._cand("a", "Topic A", 2, 5,
                                strategy=strategy),
                     self._cand("b", "Topic B", 3, 6,
                                strategy=strategy)],
                    review_examples=[
                        {"candidate_id": "review_a",
                         "status": "ready"},
                        {"candidate_id": "review_b",
                         "status": "ready"}]))
                opening = ("With your teacher, revisit the assessment "
                           "answers shown below."
                           if strategy == "supported_review" else
                           "Revisit the assessment answers shown below.")
                self.assertEqual(
                    texts["next_steps"],
                    opening + " Compare your selected answers with the "
                    "checked worked steps, explain your reasoning, and "
                    "discuss each reasoning question with your teacher.")
                self.assertEqual(
                    texts["next_steps"].count("checked worked steps"), 1)

    def test_mixed_ready_unavailable_no_checked_steps_claim(self):
        from full_feedback import multi_baseline_sections
        evidence = {"skills": [{"skill_name": "Murtoluvut",
                                "correct": 5}]}
        texts = self._texts(multi_baseline_sections(
            evidence,
            [self._cand("a", "Topic A", 2, 5,
                        strategy="focused_review"),
             self._cand("b", "Topic B", 3, 6,
                        strategy="focused_review")],
            review_examples=[
                {"candidate_id": "review_a", "status": "ready"},
                {"candidate_id": "review_b", "status": "unavailable"}]))
        self.assertEqual(
            texts["next_steps"],
            "Revisit your assessment answer on Topic A below, compare "
            "it with the checked worked steps, and discuss the "
            "reasoning question with your teacher.\n\n"
            "Ask your teacher to work through your assessment question "
            "on Topic B with you; a checked worked solution is not "
            "available for it, and no replacement example has been "
            "invented.")
        self.assertEqual(
            texts["next_steps"].count("checked worked steps"), 1)
        self.assertIn("not available", texts["next_steps"])

    def test_mixed_support_keeps_individual_grounded_steps(self):
        from full_feedback import multi_baseline_sections
        evidence = {"skills": [{"skill_name": "Murtoluvut",
                                "correct": 5}]}
        texts = self._texts(multi_baseline_sections(
            evidence,
            [self._cand("a", "Topic A", 2, 5,
                        strategy="supported_review"),
             self._cand("b", "Topic B", 3, 6,
                        strategy="focused_review")],
            review_examples=[
                {"candidate_id": "review_a", "status": "ready"},
                {"candidate_id": "review_b", "status": "ready"}]))
        self.assertEqual(
            texts["next_steps"],
            "With your teacher, revisit your assessment answer on "
            "Topic A below, compare it with the checked worked steps, "
            "and discuss the reasoning question.\n\n"
            "Revisit your assessment answer on Topic B below, compare "
            "it with the checked worked steps, and discuss the "
            "reasoning question with your teacher.")
        self.assertEqual(
            texts["next_steps"].count("checked worked steps"), 2)

    def test_no_grounded_context_keeps_candidate_actions(self):
        from full_feedback import multi_baseline_sections
        evidence = {"skills": [{"skill_name": "Murtoluvut",
                                "correct": 5}]}
        texts = self._texts(multi_baseline_sections(
            evidence,
            [self._cand("a", "Topic A", 2, 5, action="Custom one."),
             self._cand("b", "Topic B", 3, 6, action="Custom two.")],
            review_examples=[]))
        self.assertEqual(texts["next_steps"],
                         "Custom one.\n\nCustom two.")


class WorkedFirstWireTests(unittest.TestCase):
    def setUp(self):
        from worked_first_context import build_worked_first_context
        self._build_context = build_worked_first_context
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.rows = responses(self.bank, correct=False, count=40)
        self.evidence = build_evidence(
            self.bank, self.taxonomy, self.rows)
        self.assessment_context = {
            "bank": self.bank, "taxonomy": self.taxonomy,
            "responses": self.rows}
        self.bundle = build_review_bundles(self.evidence)[0]
        self.slots = [feedback_practice.empty_context("abstained")
                      for _ in self.bundle["review_candidates"]]
        self.wf_context = build_worked_first_context(
            self.evidence, self.assessment_context)
        self.full_input = build_multi_full_input(
            self.evidence, "student", self.bundle, self.slots,
            assessment_context=self.assessment_context,
            worked_first_context=self.wf_context)

    def _generator(self, opener):
        inner = AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                               opener=opener)
        return FullFeedbackAittaGenerator(inner)

    def _wf_reply(self):
        from full_feedback import worked_review_examples_for_reply
        expected = worked_review_examples_for_reply(
            self.full_input) or []
        return wire_reply(
            candidate_id=self.bundle["candidate_id"],
            worked_reviews=[
                {"question_id": e["question_id"],
                 "steps": ["Follow the checked method shown for "
                           "this question."],
                 "reasoning_question":
                     "Why does the checked method work here?"}
                for e in expected])

    def test_worked_first_payload_schema_and_keys(self):
        from worked_first_context import (
            PROMPT_VERSION as WF_PROMPT, SCHEMA as WF_SCHEMA)
        payload = self.full_input
        self.assertEqual(payload["schema"], WF_SCHEMA)
        self.assertEqual(payload["prompt_version"], WF_PROMPT)
        self.assertIn("worked_first_context", payload)
        self.assertIn("baseline_sections", payload)
        normalized = validate_full_input(copy.deepcopy(payload))
        self.assertEqual(normalized, payload)

    def test_worked_first_validation_rejects_shape_errors(self):
        from worked_first_context import SCHEMA as WF_SCHEMA
        wf = copy.deepcopy(self.full_input)
        wf.pop("worked_first_context")
        with self.assertRaises(ValueError):
            validate_full_input(wf)
        v5 = build_multi_full_input(
            self.evidence, "student", self.bundle, self.slots,
            assessment_context=self.assessment_context)
        v5["worked_first_context"] = self.wf_context
        with self.assertRaises(ValueError):
            validate_full_input(v5)
        tampered = copy.deepcopy(self.full_input)
        tampered["prompt_version"] = MULTI_FULL_FEEDBACK_PROMPT_VERSION
        with self.assertRaises(ValueError):
            validate_full_input(tampered)
        tampered = copy.deepcopy(self.full_input)
        tampered["schema"] = MULTI_FULL_FEEDBACK_SCHEMA
        with self.assertRaises(ValueError):
            validate_full_input(tampered)
        tampered = copy.deepcopy(self.full_input)
        tampered["worked_first_context"]["assessment_records"][0][
            "correct"] = True
        with self.assertRaises(ValueError):
            validate_full_input(tampered)

    def test_worked_first_wire_omits_baseline_and_raw_context(self):
        from worked_first_context import (
            INSTRUCTIONS as WF_INSTRUCTIONS, PROMPT_VERSION as WF_PROMPT,
            SCHEMA as WF_SCHEMA)
        opener = RecordingOpener(self._wf_reply())
        self._generator(opener).generate(copy.deepcopy(self.full_input))
        body = opener.calls[0]["body"]
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][0]["content"], WF_INSTRUCTIONS)
        self.assertIn(PLAIN_LANGUAGE_INSTRUCTIONS,
                      body["messages"][0]["content"])
        context = json.loads(body["messages"][1]["content"])
        self.assertEqual(context["schema"], WF_SCHEMA)
        self.assertEqual(context["prompt_version"], WF_PROMPT)
        self.assertNotIn("baseline_sections", context)
        self.assertNotIn("assessment_context", context)
        self.assertEqual(
            len(context["worked_first_context"]["assessment_records"]),
            40)
        raw = json.dumps(context)
        for forbidden in ('"baseline_sections"', '"assessment_context"',
                          '"answer_index"', '"selected_index"',
                          "session_id", "bank_sha256", "taxonomy_sha256",
                          '"p_correct"', API_KEY):
            self.assertNotIn(forbidden, raw)

    def test_worked_first_rejects_invalid_context_before_opener(self):
        bad_contexts = []
        none_payload = copy.deepcopy(self.full_input)
        none_payload["worked_first_context"] = None
        bad_contexts.append(none_payload)
        list_payload = copy.deepcopy(self.full_input)
        list_payload["worked_first_context"] = []
        bad_contexts.append(list_payload)
        empty_payload = copy.deepcopy(self.full_input)
        empty_payload["worked_first_context"] = {}
        bad_contexts.append(empty_payload)
        bad_version = copy.deepcopy(self.full_input)
        bad_version["worked_first_context"] = copy.deepcopy(
            self.wf_context)
        bad_version["worked_first_context"]["version"] = "tampered_v0"
        bad_contexts.append(bad_version)
        bad_graph = copy.deepcopy(self.full_input)
        bad_graph["worked_first_context"] = copy.deepcopy(
            self.wf_context)
        bad_graph["worked_first_context"]["concept_graph"][
            "relation"] = "prerequisite"
        bad_contexts.append(bad_graph)
        for payload in bad_contexts:
            with self.subTest(context=payload["worked_first_context"]
                              if not isinstance(
                                  payload["worked_first_context"], dict)
                              else payload["worked_first_context"].get(
                                  "version", "graph_tamper")):
                opener = RecordingOpener(
                    wire_reply(
                        candidate_id=self.bundle["candidate_id"]))
                generator = self._generator(opener)
                request_payload = copy.deepcopy(payload)
                generation_payload = copy.deepcopy(payload)
                with self.assertRaises(ValueError):
                    generator.request(request_payload)
                with self.assertRaises(ValueError):
                    generator.generate(generation_payload)
                self.assertEqual(opener.calls, [])

    def test_worked_first_metadata_and_cache_key_distinct(self):
        from worked_first_context import PROMPT_VERSION as WF_PROMPT
        opener = RecordingOpener(self._wf_reply())
        generator = self._generator(opener)
        generator.generate(copy.deepcopy(self.full_input))
        self.assertEqual(
            generator.last_metadata["prompt_version"], WF_PROMPT)
        v5_input = build_multi_full_input(
            self.evidence, "student", self.bundle, self.slots,
            assessment_context=self.assessment_context)
        v5_body = self._generator(RecordingOpener()).request(v5_input)
        wf_body = generator.request(self.full_input)
        self.assertNotEqual(json.dumps(v5_body), json.dumps(wf_body))
        self.assertNotEqual(
            v5_body["messages"][0]["content"],
            wf_body["messages"][0]["content"])


class StructuredSectionsRefactorTests(unittest.TestCase):
    def test_canonical_sections_keep_their_existing_text(self):
        sections = [{"kind": kind, "title": title, "text": kind}
                    for kind, title in zip(FULL_FEEDBACK_SECTION_KEYS,
                                           FULL_FEEDBACK_SECTION_TITLES)]
        review = {"message": {"sections": sections},
                  "feedback_plan": {"action": "Do not replace canonical text."}}
        result = structured_sections(review)
        self.assertEqual(result, sections)
        self.assertIsNot(result, sections)

    def test_existing_next_action_takes_precedence_over_fallback(self):
        review = {"message": {"sections": [{"kind": "next_action", "text": "Existing."}]},
                  "feedback_plan": {"action": "Fallback."}}
        self.assertEqual(structured_sections(review)[-1]["text"], "Existing.")

    def test_plan_action_precedes_selected_candidate(self):
        review = {"feedback_plan": {"action": "Plan action."},
                  "selected_candidate_id": "selected",
                  "candidates": [{"candidate_id": "selected", "action": "Candidate action."}]}
        self.assertEqual(structured_sections(review)[-1]["text"], "Plan action.")

    def test_candidate_fallback_stops_at_first_matching_candidate(self):
        for action in ("Selected action.", None):
            with self.subTest(action=action):
                review = {
                    "selected_candidate_id": "selected",
                    "candidates": [{"candidate_id": "other", "action": "Ignore."},
                                   {"candidate_id": "selected", "action": action},
                                   {"candidate_id": "selected", "action": "Do not use."}]}
                self.assertEqual(structured_sections(review)[-1]["text"], action or "")

    def test_missing_action_leaves_next_steps_empty(self):
        self.assertEqual(structured_sections({})[-1]["text"], "")


if __name__ == "__main__":
    unittest.main()
