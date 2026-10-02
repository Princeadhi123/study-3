"""Tests for the observed-evidence hosted provider adapters (mocked).

Fully offline: fake openers only, no network. These verify the wire
contracts and sanitized failure behaviour, not educational quality.
"""
import json
import unittest
import urllib.error

import evidence_feedback
from evidence_feedback import build_evidence, run_feedback
from evidence_feedback_policy import (
    GENERATION_SCHEMA, REVIEW_STATUS, SELECTION_INSTRUCTIONS,
    SELECTION_PROMPT_VERSION)
from evidence_providers import (
    EvidenceAittaGenerator, EvidenceJevSelector)
from aitta_generator import AittaGenerationError, AittaGenerator
from jev_selector import ENDPOINT, MODEL, QUESTION_ID, JevSelectionError
from synthetic_feedback import GENERATION_INSTRUCTIONS, PROMPT_VERSION
from tests.helpers import make_bank, make_taxonomy
from tests.test_aitta_generator import (
    BASE_URL, FAKE_KEY as AITTA_KEY, ok_body as aitta_ok_body)
from tests.test_evidence_feedback import submissions
from tests.test_jev_selector import (
    FakeOpener, ok_body as jev_ok_body)

JEV_KEY = "fake-evidence-jev-token"


def end_evidence(wrong=(0, 1)):
    bank = make_bank()
    return build_evidence(bank, make_taxonomy(bank),
                          submissions(bank, set(wrong)))


def mid_evidence(wrong=()):
    bank = make_bank()
    return build_evidence(bank, make_taxonomy(bank),
                          submissions(bank, set(wrong), count=20))


def selection(audience="student", checkpoint="end", wrong=(0, 1)):
    evidence = (end_evidence(wrong) if checkpoint == "end"
                else mid_evidence())
    return evidence_feedback.selection_payload(
        evidence, audience, checkpoint)


def jev(opener=None, timeout=12.5):
    return EvidenceJevSelector(
        JEV_KEY, timeout=timeout,
        opener=opener if opener is not None
        else FakeOpener(jev_ok_body(choice="review_sub_sA")))


def opening_payload(audience="student", checkpoint="end",
                    candidate_id="review_sub_sA",
                    strategy="focused_review"):
    return {"schema": GENERATION_SCHEMA, "audience": audience,
            "checkpoint": checkpoint,
            "selected_candidate": {
                "candidate_id": candidate_id, "strategy": strategy,
                "review_status": REVIEW_STATUS}}


def evidence_aitta(opener=None):
    inner = AittaGenerator(
        AITTA_KEY, BASE_URL,
        opener=opener if opener is not None
        else FakeOpener(aitta_ok_body(candidate="observed_summary")))
    return EvidenceAittaGenerator(inner)


class SelectorRequestTests(unittest.TestCase):
    def test_posts_exact_observed_evidence_contract(self):
        opener = FakeOpener(jev_ok_body(choice="review_sub_sB"))
        payload = selection("student", "end")
        result = jev(opener).select(payload)
        self.assertEqual(result, {"candidate_id": "review_sub_sB"})
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"),
                         f"Bearer {JEV_KEY}")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["state"], {
            "audience": "student", "checkpoint": "end",
            "evidence": payload["evidence"],
            "candidates": payload["candidates"]})
        self.assertNotIn("schema", body["state"])
        expected_criteria = {
            c["candidate_id"]: c["action"]
            for c in payload["candidates"]}
        self.assertEqual(body["questions"], {QUESTION_ID: {
            "type": "choice",
            "instructions": SELECTION_INSTRUCTIONS,
            "criteria": expected_criteria}})
        self.assertEqual(set(expected_criteria),
                         {"review_sub_sA", "review_sub_sB"})

    def test_wire_carries_no_raw_or_diagnostic_fields(self):
        opener = FakeOpener(jev_ok_body(choice="review_sub_sA"))
        jev(opener).select(selection("teacher", "end"))
        blob = opener.requests[0].data.decode("utf-8")
        for marker in ("question_id", "answer_index", "options",
                       "selected_index", "item_id", "exercise_id",
                       "session", "kt_", "ability", "theta",
                       "response", "distractor"):
            self.assertNotIn(marker, blob)
        self.assertNotIn(JEV_KEY, blob)

    def test_single_candidates_resolve_without_http(self):
        for audience, checkpoint, expected in (
                ("student", "midpoint", "neutral"),
                ("student", "end", "optional_consolidation"),
                ("teacher", "end", "optional_consolidation")):
            with self.subTest(case=(audience, checkpoint)):
                opener = FakeOpener(jev_ok_body(choice="neutral"))
                selector = jev(opener)
                payload = (selection(audience, checkpoint, wrong=())
                           if checkpoint == "end"
                           else selection(audience, checkpoint))
                result = selector.select(payload)
                self.assertEqual(result, {"candidate_id": expected})
                self.assertEqual(opener.requests, [])
                self.assertEqual(selector.last_metadata["status"],
                                 "not_called_single_candidate")
                self.assertEqual(selector.last_metadata
                                 ["prompt_version"],
                                 SELECTION_PROMPT_VERSION)

    def test_metadata_reports_prompt_version_and_no_confidence(self):
        opener = FakeOpener(jev_ok_body(choice="review_sub_sA"))
        selector = jev(opener)
        selector.select(selection())
        metadata = selector.last_metadata
        self.assertEqual(metadata["status"], "completed")
        self.assertEqual(metadata["prompt_version"],
                         SELECTION_PROMPT_VERSION)
        for marker in ("confidence", "probabilities", "rationale"):
            self.assertNotIn(marker, json.dumps(metadata))


class SelectorValidationTests(unittest.TestCase):
    def assert_rejected_without_request(self, payload):
        opener = FakeOpener(jev_ok_body(choice="review_sub_sA"))
        selector = jev(opener)
        with self.assertRaises(ValueError):
            selector.select(payload)
        self.assertEqual(opener.requests, [])
        self.assertEqual(selector.last_metadata["status"],
                         "not_called")

    def test_rejects_bad_envelope(self):
        good = selection()
        missing = {k: v for k, v in good.items() if k != "evidence"}
        for bad in (None, "payload", [], {}, missing,
                    {**good, "session_id": "s1"},
                    {**good, "schema": "wrong"},
                    {**good, "audience": "admin"},
                    {**good, "checkpoint": "start"}):
            with self.subTest(bad=bad):
                self.assert_rejected_without_request(bad)

    def test_rejects_invalid_candidates_and_evidence(self):
        payload = selection()
        payload["candidates"] = payload["candidates"][::-1]
        self.assert_rejected_without_request(payload)
        payload = selection()
        payload["candidates"][0]["action"] = "Do something else."
        self.assert_rejected_without_request(payload)
        payload = selection()
        payload["evidence"]["total"]["correct"] = 0
        self.assert_rejected_without_request(payload)
        payload = selection()
        payload["evidence"]["skills"][0]["correct"] = True
        self.assert_rejected_without_request(payload)
        payload = selection("student", "midpoint")
        payload["evidence"] = {"total": {"correct": 0}}
        self.assert_rejected_without_request(payload)
        payload = selection("teacher", "end")
        payload["checkpoint"] = "midpoint"
        self.assert_rejected_without_request(payload)


class SelectorFailureTests(unittest.TestCase):
    def test_transport_failures_sanitize(self):
        for error in (urllib.error.HTTPError(
                          ENDPOINT, 401, f"denied {JEV_KEY}", {}, None),
                      urllib.error.URLError("refused"),
                      TimeoutError(f"slow {JEV_KEY}"),
                      RuntimeError(f"boom {JEV_KEY}")):
            with self.subTest(error=type(error).__name__):
                selector = jev(FakeOpener(error=error))
                with self.assertRaises(JevSelectionError) as ctx:
                    selector.select(selection())
                self.assertEqual(str(ctx.exception),
                                 "Jev request failed")
                self.assertNotIn(JEV_KEY, repr(ctx.exception))
                metadata = selector.last_metadata
                self.assertEqual(metadata["status"], "failed")
                self.assertIsNone(metadata["model_version"])
                self.assertIsNone(metadata["usage"])
                self.assertEqual(metadata["prompt_version"],
                                 SELECTION_PROMPT_VERSION)

    def test_bad_responses_fail_sanitized(self):
        bodies = {
            "bad_json": b"not json",
            "unknown_choice": jev_ok_body(choice="neutral"),
            "missing_usage": json.dumps({
                "model": "m", "answers": {QUESTION_ID: {
                    "type": "choice", "choice": "review_sub_sA"
                }}}).encode(),
            "key_in_model": jev_ok_body(choice="review_sub_sA",
                                        model=JEV_KEY),
        }
        for name, body in bodies.items():
            with self.subTest(name=name):
                selector = jev(FakeOpener(body))
                with self.assertRaises(JevSelectionError):
                    selector.select(selection())
                self.assertEqual(selector.last_metadata["status"],
                                 "failed")

    def test_metadata_has_no_stale_success_after_failure(self):
        selector = jev(FakeOpener(jev_ok_body(choice="review_sub_sA")))
        selector.select(selection())
        self.assertEqual(selector.last_metadata["status"], "completed")
        selector._opener = FakeOpener(error=RuntimeError("x"))
        with self.assertRaises(JevSelectionError):
            selector.select(selection())
        self.assertEqual(selector.last_metadata, {
            "status": "failed", "model_version": None, "usage": None,
            "prompt_version": SELECTION_PROMPT_VERSION})


class GeneratorWrapperTests(unittest.TestCase):
    def test_wire_sends_only_narrow_legacy_context(self):
        opener = FakeOpener(aitta_ok_body(candidate="observed_summary"))
        result = evidence_aitta(opener).generate(opening_payload())
        self.assertEqual(result, {
            "candidate_id": "review_sub_sA",
            "opening": "Thank you for completing this assessment."})
        body = json.loads(opener.requests[0].data.decode("utf-8"))
        self.assertEqual(body["messages"][0], {
            "role": "system", "content": GENERATION_INSTRUCTIONS})
        context = json.loads(body["messages"][1]["content"])
        self.assertEqual(context, {
            "audience": "student", "checkpoint": "end",
            "selected_candidate": {
                "candidate_id": "observed_summary",
                "strategy": "describe_observed_counts",
                "review_status": "draft_pending_educator_review"}})
        blob = opener.requests[0].data.decode("utf-8")
        for marker in ("review_sub_sA", "sub_sA", "skill_a", "Fractions",
                       "out_of", "question_id", "evidence",
                       "bank_sha256"):
            self.assertNotIn(marker, blob)

    def test_returns_original_v2_candidate_id(self):
        for audience, checkpoint, cid, strategy, legacy_id in (
                ("student", "end", "review_sub_sA", "focused_review",
                 "observed_summary"),
                ("student", "end", "review_sub_sB", "supported_review",
                 "observed_summary"),
                ("teacher", "end", "optional_consolidation",
                 "optional_consolidation", "observed_summary"),
                ("student", "midpoint", "neutral",
                 "neutral_encouragement", "neutral")):
            with self.subTest(candidate=cid):
                opener = FakeOpener(aitta_ok_body(candidate=legacy_id))
                result = evidence_aitta(opener).generate(
                    opening_payload(audience, checkpoint, cid,
                                    strategy))
                self.assertEqual(result["candidate_id"], cid)
                context = json.loads(json.loads(
                    opener.requests[0].data.decode("utf-8"))
                    ["messages"][1]["content"])
                self.assertEqual(context["selected_candidate"]
                                 ["candidate_id"], legacy_id)

    def test_wraps_only_aitta_generators(self):
        with self.assertRaises(TypeError):
            EvidenceAittaGenerator(object())
        with self.assertRaises(TypeError):
            EvidenceAittaGenerator(None)

    def test_metadata_carries_role_and_inner_state(self):
        opener = FakeOpener(aitta_ok_body(candidate="observed_summary"))
        wrapper = evidence_aitta(opener)
        wrapper.generate(opening_payload())
        metadata = wrapper.last_metadata
        self.assertEqual(metadata["status"], "completed")
        self.assertEqual(metadata["role"],
                         "opening_only_no_performance_evidence")
        self.assertEqual(metadata["model_version"],
                         "openai/gpt-oss-120b")
        self.assertEqual(metadata["prompt_version"], PROMPT_VERSION)

    def test_metadata_never_reports_stale_state(self):
        # A rejected payload must not inherit an earlier completed call.
        opener = FakeOpener(aitta_ok_body(candidate="observed_summary"))
        wrapper = evidence_aitta(opener)
        wrapper.generate(opening_payload())
        self.assertEqual(wrapper.last_metadata["status"], "completed")
        with self.assertRaises(ValueError):
            wrapper.generate({"schema": "wrong"})
        metadata = wrapper.last_metadata
        self.assertEqual(metadata["status"], "not_called")
        self.assertIsNone(metadata["model_version"])
        self.assertIsNone(metadata["usage"])
        self.assertEqual(metadata["role"],
                         "opening_only_no_performance_evidence")

    def test_metadata_clears_after_inner_failure(self):
        # A success followed by a transport failure must show failed.
        wrapper = evidence_aitta()
        wrapper.generate(opening_payload())
        self.assertEqual(wrapper.last_metadata["status"], "completed")
        wrapper._generator._opener = FakeOpener(
            error=RuntimeError("x"))
        with self.assertRaises(AittaGenerationError):
            wrapper.generate(opening_payload())
        metadata = wrapper.last_metadata
        self.assertEqual(metadata["status"], "failed")
        self.assertIsNone(metadata["model_version"])
        self.assertIsNone(metadata["usage"])
        self.assertEqual(metadata["role"],
                         "opening_only_no_performance_evidence")

    def test_failures_sanitize_and_reset_metadata(self):
        for error in (RuntimeError(f"leak {AITTA_KEY}"),
                      TimeoutError("slow")):
            with self.subTest(error=type(error).__name__):
                wrapper = evidence_aitta(FakeOpener(error=error))
                with self.assertRaises(AittaGenerationError) as ctx:
                    wrapper.generate(opening_payload())
                self.assertEqual(str(ctx.exception),
                                 "Aitta request failed")
                self.assertNotIn(AITTA_KEY, repr(ctx.exception))
                metadata = wrapper.last_metadata
                self.assertEqual(metadata["status"], "failed")
                self.assertIsNone(metadata["model_version"])
                self.assertEqual(
                    metadata["role"],
                    "opening_only_no_performance_evidence")

    def test_runner_roundtrip_through_wrapper(self):
        opener = FakeOpener(aitta_ok_body(candidate="observed_summary"))
        review = run_feedback(
            end_evidence({0}), "student", "end",
            generator=evidence_aitta(opener))
        self.assertEqual(review["trace"]["phrasing_source"],
                         "injected_generator_opening_only")
        self.assertIsNone(review["trace"]["fallback_reason"])
        self.assertIn("Thank you for completing this assessment.",
                      review["message"]["text"])
        self.assertEqual(len(opener.requests), 1)


class GeneratorPayloadValidationTests(unittest.TestCase):
    def assert_rejected_without_request(self, payload):
        opener = FakeOpener(aitta_ok_body(candidate="observed_summary"))
        wrapper = evidence_aitta(opener)
        with self.assertRaises(ValueError):
            wrapper.generate(payload)
        self.assertEqual(opener.requests, [])

    def test_rejects_bad_envelope(self):
        good = opening_payload()
        missing = {k: v for k, v in good.items()
                   if k != "selected_candidate"}
        for bad in (None, "payload", [], {}, missing,
                    {**good, "evidence": {}},
                    {**good, "schema": "phase3_synthetic_generation_v1"},
                    {**good, "audience": "admin"},
                    {**good, "checkpoint": "start"}):
            with self.subTest(bad=bad):
                self.assert_rejected_without_request(bad)

    def test_rejects_teacher_midpoint(self):
        self.assert_rejected_without_request(
            opening_payload("teacher", "midpoint", "neutral",
                            "neutral_encouragement"))

    def test_rejects_candidate_strategy_mismatch(self):
        cases = [
            opening_payload("student", "end", "neutral",
                            "neutral_encouragement"),
            opening_payload("student", "midpoint",
                            "optional_consolidation",
                            "optional_consolidation"),
            opening_payload("student", "midpoint", "review_sub_sA",
                            "focused_review"),
            opening_payload("student", "end", "review_sub_sA",
                            "neutral_encouragement"),
            opening_payload("student", "end", "review_sub_sA",
                            "optional_consolidation"),
            opening_payload("student", "end",
                            "optional_consolidation", "focused_review"),
            opening_payload("student", "end", "unknown_thing",
                            "focused_review"),
            opening_payload("student", "end", "review_",
                            "focused_review"),
            opening_payload("student", "end", "review_sub sA!",
                            "focused_review"),
        ]
        for payload in cases:
            with self.subTest(selected=payload["selected_candidate"]):
                self.assert_rejected_without_request(payload)

    def test_rejects_bad_selected_candidate_fields(self):
        payload = opening_payload()
        payload["selected_candidate"]["review_status"] = "approved"
        self.assert_rejected_without_request(payload)
        payload = opening_payload()
        payload["selected_candidate"]["strategy"] = "invented"
        self.assert_rejected_without_request(payload)
        payload = opening_payload()
        payload["selected_candidate"]["focus"] = {"x": 1}
        self.assert_rejected_without_request(payload)
        payload = opening_payload()
        payload["selected_candidate"] = "review_sub_sA"
        self.assert_rejected_without_request(payload)
        payload = opening_payload()
        payload["selected_candidate"]["candidate_id"] = 42
        self.assert_rejected_without_request(payload)

    def test_rejects_non_string_strategy(self):
        for bad in (["focused_review"], {"s": 1}, 7, None, True):
            payload = opening_payload()
            payload["selected_candidate"]["strategy"] = bad
            with self.subTest(strategy=bad):
                self.assert_rejected_without_request(payload)

    def test_full_length_review_suffix_accepted(self):
        candidate_id = "review_" + "s" * 80
        opener = FakeOpener(aitta_ok_body(candidate="observed_summary"))
        result = evidence_aitta(opener).generate(
            opening_payload("student", "end", candidate_id,
                            "focused_review"))
        self.assertEqual(result["candidate_id"], candidate_id)
        self.assertEqual(len(opener.requests), 1)

    def test_rejects_oversized_review_suffix(self):
        self.assert_rejected_without_request(
            opening_payload("student", "end", "review_" + "s" * 81,
                            "focused_review"))


if __name__ == "__main__":
    unittest.main()
