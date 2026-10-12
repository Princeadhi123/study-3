import copy
import json
import tempfile
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
    MULTI_FULL_FEEDBACK_PROMPT_VERSION, MULTI_FULL_FEEDBACK_SCHEMA,
    build_full_input, build_multi_full_input, full_message_sections,
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

    def test_internal_process_language_rejected(self):
        for key in FULL_FEEDBACK_SECTION_KEYS:
            sections = dict(VALID_SECTIONS)
            sections[key] = "This section follows the plan."
            self._reject(wire_reply(sections=sections,
                                    candidate_id=self.selected_id))
        for phrase in ("the feedback plan", "a priority group",
                       "the selected candidate", "the selection policy",
                       "routing details", "the system prompt",
                       "Jev routing", "the Aitta system",
                       "according to this plan", "based on our plan",
                       "guided by its plan"):
            sections = dict(VALID_SECTIONS)
            sections["next_steps"] = f"A note about {phrase}."
            self._reject(wire_reply(sections=sections,
                                    candidate_id=self.selected_id))
        sections = dict(VALID_SECTIONS)
        sections["next_steps"] = (
            "Plan your calculation before answering.")
        opener = RecordingOpener(
            wire_reply(sections=sections,
                       candidate_id=self.selected_id))
        result = self._generator(opener).generate(self.full_input)
        self.assertEqual(result["sections"]["next_steps"],
                         sections["next_steps"])

    def test_prompt_version_v3_request_and_isolation(self):
        self.assertEqual(FULL_FEEDBACK_PROMPT_VERSION,
                         "aitta_feedback_with_practice_v3")
        payload = copy.deepcopy(self.full_input)
        payload["prompt_version"] = "aitta_feedback_with_practice_v2"
        with self.assertRaises(ValueError):
            validate_full_input(payload)
        body = self._generator(
            RecordingOpener()).request(self.full_input)
        context = json.loads(body["messages"][1]["content"])
        self.assertEqual(context["prompt_version"],
                         FULL_FEEDBACK_PROMPT_VERSION)

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
        with self.assertRaises(ValueError):
            run_feedback(self.evidence, "student", "midpoint",
                         practice_context=self._practice_ctx(pool))


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
            with self.assertRaises(ValueError):
                mutate(payload)
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
                          "p_correct", "answer_index", '"options"',
                          "item_id", "sha256"):
            self.assertNotIn(forbidden, raw)

    def test_run_multi_feedback_records_generated_sections(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=self.bundle["candidate_id"]))
        review = run_multi_feedback(
            self.evidence,
            generator=self._generator(opener),
            practice_contexts=self.slots)
        self.assertEqual(len(opener.calls), 1)
        for section in review["message"]["sections"]:
            self.assertTrue(
                section["text"].startswith(
                    VALID_SECTIONS[section["kind"]]))
        self.assertIsNone(review["trace"]["fallback_reason"])
        self.assertEqual(
            review["trace"]["generation_prompt_version"],
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
        for section in review["message"]["sections"]:
            self.assertTrue(
                section["text"].startswith(
                    VALID_SECTIONS[section["kind"]]))

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


class MultiBaselineSectionsRegressionTests(unittest.TestCase):
    def _cand(self, sub_id, sub_name, incorrect, out_of,
              pattern="recurring_errors", tied=None,
              strategy="supported_review", action=None, focus=True):
        candidate = {"focus": None,
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
            "Fractions, Percentages.")
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
            "A possible review starting point is Percentage of an "
            "amount: 3 of 10 assessed answers were incorrect.\n\n"
            "A possible review starting point is Divisibility: 2 of 8 "
            "assessed answers were incorrect.\n\n" + SCOPE)
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
            "A possible review starting point is Percentage of an "
            "amount: 1 of 5 assessed answers were incorrect. This is an "
            "isolated incorrect answer, not evidence of a consistent "
            "difficulty. Other options share this display rank; this is "
            "not a uniquely weakest area.\n\n"
            "A possible review starting point is Unknown topic: 1 of 1 "
            "assessed answers were incorrect. Only one question assessed "
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


if __name__ == "__main__":
    unittest.main()
