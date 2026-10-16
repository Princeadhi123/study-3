"""Provider-authored worked-card steps for worked_first feedback.

The same hosted worked-first call returns four narrative sections plus
per-card teaching steps and a reasoning question. Validation is shape
only - never semantic verification; deterministic checked cards remain
the fallback, provider wording requires educator review, and no extra
hosted call is made. All fixtures are offline fakes - no network.
"""
import copy
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import phase3_paths  # noqa: F401
from aitta_generator import AittaGenerationError, AittaGenerator
from assessment_worked_review import build_assessment_review_examples
from demo_api import make_server
from demo_service import DemoService
from evidence_feedback import build_evidence
from evidence_feedback_policy import build_review_bundles
import feedback_practice
from feedback_practice import resolve_practice
from full_feedback import (
    CachedFullFeedbackGenerator, FullFeedbackAittaGenerator,
    build_multi_full_input, validate_worked_reviews)
from replay_provider_scenarios import CaptureCache
from worked_first_context import (
    PROMPT_VERSION as WF_PROMPT, SCHEMA as WF_SCHEMA, WF_MAX_TOKENS,
    build_worked_first_context)
from tests.test_assessment_worked_review import (
    grounded_rows, make_grounded_bank, make_grounded_taxonomy, positions)
from tests.test_demo_service import fake_diagnostics, wait_for
from tests.test_full_feedback import (
    API_KEY, BASE_URL, MODEL, RecordingOpener, VALID_SECTIONS,
    wire_reply)

AUTHORED = [
    (["10% means one tenth of the whole amount.",
      "One tenth of 50 is 5."],
     "Why does dividing 50 into ten equal parts give 10%?"),
    (["Three copies of x plus two copies of x make five copies of x.",
      "The result is 5x."],
     "Why does the variable stay x when the coefficients are added?")]
GENERIC = (["Follow the checked method shown for this question."],
           "Why does the checked method work for this question?")


class WorkedReviewOpener(RecordingOpener):
    """Wire reply whose candidate_id and ready question_ids are read
    from the request's user-message context; authored step text is
    indexed per ready example."""

    def __init__(self, sections=None, per_card=AUTHORED,
                 reviews_override=None):
        super().__init__()
        self.sections = sections
        self.per_card = per_card
        self.reviews_override = reviews_override

    def open(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        context = json.loads(body["messages"][1]["content"])
        ready = [e for e in context.get("review_examples") or []
                 if e.get("status") == "ready"]
        if self.reviews_override is not None:
            reviews = self.reviews_override(ready)
        else:
            reviews = []
            for index, example in enumerate(ready):
                steps, reasoning = (self.per_card[index]
                                    if index < len(self.per_card)
                                    else GENERIC)
                reviews.append({
                    "question_id": example["question_id"],
                    "steps": list(steps),
                    "reasoning_question": reasoning})
        self.payload = wire_reply(
            candidate_id=context["selected_candidate"]["candidate_id"],
            sections=self.sections, worked_reviews=reviews)
        return super().open(request, timeout)


class WorkedReviewValidationTests(unittest.TestCase):
    """Direct shape-validation bounds for validate_worked_reviews."""

    def setUp(self):
        self.examples = [
            {"question_id": "q_one", "status": "ready"},
            {"question_id": "q_two", "status": "ready"},
            {"question_id": "q_down", "status": "unavailable"}]
        self.valid = [
            {"question_id": "q_one",
             "steps": ["50% means half of the whole amount."],
             "reasoning_question":
                 "Why does splitting the whole in half give 50%?"},
            {"question_id": "q_two",
             "steps": ["Five copies of v plus two copies of v make "
                       "seven copies of v.", "The result is 7v."],
             "reasoning_question":
                 "Why does the variable stay v when the coefficients "
                 "are added?"}]

    def test_valid_normalized_deep_copy(self):
        out = validate_worked_reviews(self.valid, self.examples)
        self.assertEqual(out, self.valid)
        self.assertIsNot(out, self.valid)
        # Order is immaterial.
        self.assertEqual(
            validate_worked_reviews(list(reversed(self.valid)),
                                    self.examples),
            [self.valid[1], self.valid[0]])

    def test_no_ready_examples_requires_empty_list(self):
        none_ready = [{"question_id": "q_down", "status": "unavailable"}]
        self.assertEqual(validate_worked_reviews([], none_ready), [])
        self.assertIsNone(validate_worked_reviews(
            self.valid[:1], none_ready))

    def test_structural_rejections(self):
        qid = self.examples[0]["question_id"]
        cases = []
        cases.append("not a list")
        cases.append([{"question_id": qid, "steps": ["ok"],
                       "reasoning_question": "Why?"}])  # missing q_two
        cases.append(self.valid + [dict(self.valid[0])])  # duplicate
        unknown = [dict(self.valid[0]),
                   dict(self.valid[1], question_id="q_ghost")]
        cases.append(unknown)
        cases.append([dict(self.valid[0], question_id="q_down"),
                      self.valid[1]])  # unavailable card id
        cases.append([dict(self.valid[0], extra="x"),
                      self.valid[1]])
        cases.append([dict(self.valid[0], steps=[]), self.valid[1]])
        cases.append([dict(self.valid[0], steps=["x"] * 7),
                      self.valid[1]])
        cases.append([dict(self.valid[0], steps="not a list"),
                      self.valid[1]])
        cases.append([dict(self.valid[0], steps=["x" * 1001]),
                      self.valid[1]])
        cases.append([dict(self.valid[0], steps=[""]),
                      self.valid[1]])
        cases.append([dict(self.valid[0], steps=["<b>x</b>"]),
                      self.valid[1]])
        cases.append([dict(self.valid[0], steps=["bad\x01char"]),
                      self.valid[1]])
        cases.append([dict(self.valid[0],
                           reasoning_question=""), self.valid[1]])
        cases.append([dict(self.valid[0],
                           reasoning_question="r" * 601),
                      self.valid[1]])
        cases.append([dict(self.valid[0],
                           reasoning_question="<i>why</i>"),
                      self.valid[1]])
        cases.append([{k: v for k, v in self.valid[0].items()
                       if k != "reasoning_question"}, self.valid[1]])
        # Unhashable/non-string question_id must not raise TypeError.
        cases.append([dict(self.valid[0], question_id=["q_one"]),
                      self.valid[1]])
        cases.append([dict(self.valid[0],
                           question_id={"id": "q_one"}),
                      self.valid[1]])
        cases.append([dict(self.valid[0], question_id=7),
                      self.valid[1]])
        # Same expected length but a duplicated question_id exercises
        # the seen check, not just the count.
        cases.append([self.valid[0], dict(self.valid[0])])
        cases.append([dict(self.valid[0], steps=["ok", None]),
                      self.valid[1]])
        cases.append([dict(self.valid[0], steps=[42]),
                      self.valid[1]])
        cases.append([dict(self.valid[0], reasoning_question=True),
                      self.valid[1]])
        for case in cases:
            with self.subTest(case=repr(case)[:80]):
                self.assertIsNone(
                    validate_worked_reviews(case, self.examples))


class WorkedReviewWireTests(unittest.TestCase):
    """Native and cached worked-first generation end to end."""

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
        # build_multi_full_input expects one slot per focus candidate.
        contexts = [feedback_practice.empty_context("abstained")
                    for candidate in self.bundle["review_candidates"]
                    if candidate["focus"] is not None]
        self.wf_context = build_worked_first_context(
            self.evidence, self.assessment_context)
        self.full_input = build_multi_full_input(
            self.evidence, "student", self.bundle, contexts,
            assessment_context=self.assessment_context,
            worked_first_context=self.wf_context)
        self.ready_ids = [e["question_id"] for e in
                          self.full_input["review_examples"]
                          if e["status"] == "ready"]

    def _generator(self, opener):
        inner = AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                               opener=opener)
        return FullFeedbackAittaGenerator(inner)

    def test_native_generate_sections_and_worked_reviews(self):
        opener = WorkedReviewOpener()
        generator = self._generator(opener)
        reply = generator.generate(copy.deepcopy(self.full_input))
        self.assertEqual(set(reply),
                         {"candidate_id", "sections", "worked_reviews"})
        self.assertEqual([w["question_id"] for w in
                          reply["worked_reviews"]], self.ready_ids)
        self.assertTrue(self.ready_ids)
        self.assertEqual(
            reply["worked_reviews"][0]["steps"], list(AUTHORED[0][0]))
        self.assertEqual(reply["worked_reviews"][0]
                         ["reasoning_question"], AUTHORED[0][1])
        body = opener.calls[0]["body"]
        self.assertEqual(body["max_tokens"], WF_MAX_TOKENS)
        self.assertEqual(body["max_tokens"], 4096)
        context = json.loads(body["messages"][1]["content"])
        self.assertEqual(context["schema"], WF_SCHEMA)
        self.assertEqual(context["prompt_version"], WF_PROMPT)
        self.assertEqual(WF_PROMPT,
                         "aitta_worked_first_evidence_author_v3")
        meta = generator.last_metadata
        self.assertEqual(meta["status"], "completed")
        self.assertEqual(meta["prompt_version"], WF_PROMPT)
        self.assertIsNone(generator.last_rejected_draft)

    def test_cached_round_trip_and_reuse(self):
        opener = WorkedReviewOpener()
        native = self._generator(opener)
        cache = CaptureCache(Path(self.tmp.name), None)
        cached = CachedFullFeedbackGenerator(
            native, cache, MODEL, "hash")
        first = cached.generate(copy.deepcopy(self.full_input))
        self.assertIn("worked_reviews", first)
        second = cached.generate(copy.deepcopy(self.full_input))
        self.assertEqual(len(opener.calls), 1)
        self.assertTrue(cached.execution["reused"])
        self.assertEqual(first, second)

    def test_invalid_worked_reviews_rejected_with_draft(self):
        bad = [dict(question_id=self.ready_ids[0], steps=["ok"],
                    reasoning_question="Why?")]
        opener = WorkedReviewOpener(reviews_override=lambda ready: bad)
        generator = self._generator(opener)
        payload = copy.deepcopy(self.full_input)
        with self.assertRaises(AittaGenerationError):
            generator.generate(payload)
        self.assertEqual(generator.last_metadata["rejection_code"],
                         "invalid_worked_reviews")
        draft = generator.last_rejected_draft
        self.assertIsNotNone(draft)
        self.assertIn("worked_reviews", draft["content"])

    def test_secret_in_worked_reviews_rejected(self):
        opener = WorkedReviewOpener(reviews_override=lambda ready: [
            {"question_id": e["question_id"],
             "steps": [f"Key leak {API_KEY}"],
             "reasoning_question": "Why?"}
            for e in ready])
        generator = self._generator(opener)
        payload = copy.deepcopy(self.full_input)
        with self.assertRaises(AittaGenerationError):
            generator.generate(payload)
        # The demo key contains "aitta", so an embedded key also trips
        # the internal-process check during shape validation; either way
        # the reply is rejected and the secret never leaks.
        self.assertIn(generator.last_metadata["rejection_code"],
                      ("invalid_worked_reviews",
                       "secret_in_worked_reviews"))
        self.assertNotIn(API_KEY,
                         json.dumps(generator.last_metadata))
        self.assertNotIn(
            API_KEY, generator.last_rejected_draft["content"])

    def test_secret_in_worked_reviews_dedicated_code(self):
        # A configured key with no banned internal words exercises the
        # dedicated secret_in_worked_reviews rejection branch.
        secret = "synthetic-secret-9876543210"
        opener = WorkedReviewOpener(reviews_override=lambda ready: [
            {"question_id": e["question_id"],
             "steps": [f"Leaked key {secret}"],
             "reasoning_question": "Why?"}
            for e in ready])
        inner = AittaGenerator(secret, BASE_URL, model=MODEL,
                               opener=opener)
        generator = FullFeedbackAittaGenerator(inner)
        payload = copy.deepcopy(self.full_input)
        with self.assertRaises(AittaGenerationError) as caught:
            generator.generate(payload)
        self.assertEqual(str(caught.exception),
                         "Aitta request failed")
        self.assertEqual(generator.last_metadata["rejection_code"],
                         "secret_in_worked_reviews")
        self.assertNotIn(secret,
                         json.dumps(generator.last_metadata))
        self.assertNotIn(
            secret, generator.last_rejected_draft["content"])

    def test_service_overlay_and_release(self):
        for mode in ("native", "cached"):
            with self.subTest(mode=mode):
                opener = WorkedReviewOpener()
                native = self._generator(opener)
                generator = native
                cache = None
                if mode == "cached":
                    cache_root = Path(self.tmp.name) / "ov_captures"
                    cache_root.mkdir(exist_ok=True)
                    cache = CaptureCache(cache_root, None)
                    generator = CachedFullFeedbackGenerator(
                        native, cache, MODEL, "hash")
                service = DemoService(
                    root=Path(self.tmp.name) / f"svc_{mode}",
                    bank=self.bank, taxonomy=self.taxonomy,
                    diagnostics=fake_diagnostics(self.bank),
                    provider_mode="hosted", generator=generator)
                if cache is not None:
                    service.cache = cache
                self.addCleanup(service.close, wait=True)
                created = service.create_session(
                    feedback_variant="worked_first")
                sid, token = (created["session_id"],
                              created["student_token"])
                for row in self.rows:
                    service.submit_response(sid, token, row)
                self.assertTrue(wait_for(
                    lambda: service._load_meta(sid)["provider_job"]
                    ["status"] in ("ready", "fallback")))
                self.assertEqual(len(opener.calls), 1)
                meta = service._load_meta(sid)
                self.assertEqual(meta["provider_job"]["status"],
                                 "ready")
                end = meta["checkpoints"]["end"]
                review = end["student_review"]
                trace = review["trace"]
                self.assertEqual(
                    trace["worked_review_sources"],
                    {qid: "provider" for qid in self.ready_ids})
                # The deterministic checked steps are preserved under
                # checked_steps/checked_reasoning_question while the
                # displayed steps/reasoning/explanation come from the
                # provider reply.
                det = {c["question_id"]: c for c in
                       build_assessment_review_examples(
                           self.evidence, review["selected_candidates"],
                           self.assessment_context)}
                by_qid = {c["question_id"]: c
                          for c in review["review_examples"]}
                for index, qid in enumerate(self.ready_ids):
                    card = by_qid[qid]
                    steps, reasoning = (AUTHORED[index]
                                        if index < len(AUTHORED)
                                        else GENERIC)
                    self.assertEqual(card["steps"], list(steps))
                    self.assertEqual(card["reasoning_question"],
                                     reasoning)
                    self.assertEqual(card["explanation"],
                                     "\n".join(steps))
                    self.assertEqual(card["explanation_source"],
                                     "provider")
                    self.assertEqual(
                        card["explanation_validation"],
                        "shape_only_requires_educator_review")
                    self.assertEqual(card["checked_steps"],
                                     det[qid]["steps"])
                    self.assertEqual(
                        card["checked_reasoning_question"],
                        det[qid]["reasoning_question"])
                    self.assertNotEqual(card["steps"],
                                        det[qid]["steps"])
                    # IDs, question, options, answers, status and
                    # solution version are untouched.
                    self.assertEqual(card["question_id"], qid)
                    self.assertEqual(card["status"], "ready")
                    self.assertEqual(card["prompt"], det[qid]["prompt"])
                    self.assertEqual(card["options"],
                                     det[qid]["options"])
                    self.assertEqual(card["selected_answer"],
                                     det[qid]["selected_answer"])
                    self.assertEqual(card["correct_answer"],
                                     det[qid]["correct_answer"])
                    self.assertEqual(card["solution_version"],
                                     det[qid]["solution_version"])
                detail = service.teacher_session(sid)
                preview = detail["feedback_delivery"]["preview"]
                self.assertEqual(preview["total"],
                                 {"correct": 38, "out_of": 40})
                # Practice stays application-owned: preview cards equal
                # the deterministic resolution of each saved context.
                by_cand = {c["candidate_id"]: c for c in
                           review.get("selected_candidates") or []}
                expected_cards = [
                    resolve_practice(
                        slot["context"], end["evidence"],
                        candidate=by_cand.get(slot.get("candidate_id")),
                        aligned=True)["card"]
                    for slot in end.get("practice_contexts") or []]
                self.assertEqual(
                    preview.get("practice_activities") or [],
                    expected_cards)
                # Nothing released before the teacher approves.
                pre = service.snapshot(sid, token)
                self.assertIsNone(pre["feedback"]["end"])
                self.assertEqual(pre["feedback_delivery"]["status"],
                                 "awaiting_teacher_review")
                released = service.release_feedback(sid, {
                    "message_sha256": detail["feedback_delivery"]
                    ["preview_sha256"]})
                self.assertTrue(released["released"])
                out = service._load_meta(
                    sid)["feedback_release"]["released_feedback"]
                self.assertEqual(out["review_examples"],
                                 preview["review_examples"])
                snapshot = service.snapshot(sid, token)
                self.assertNotIn("rejected_draft",
                                 json.dumps(snapshot))

    def test_teacher_edit_then_release_keeps_approved_wording(self):
        opener = WorkedReviewOpener()
        service = DemoService(
            root=Path(self.tmp.name) / "svc_edit",
            bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            provider_mode="hosted",
            generator=self._generator(opener))
        self.addCleanup(service.close, wait=True)
        created = service.create_session(feedback_variant="worked_first")
        sid, token = (created["session_id"], created["student_token"])
        for row in self.rows:
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        detail = service.teacher_session(sid)
        preview = detail["feedback_delivery"]["preview"]
        # The teacher edits the first ready card's explanation wording.
        teacher_steps = ["Teacher-approved wording for this method."]
        teacher_reason = "Explain the checked method in your own words."
        patches = []
        edited_qid = self.ready_ids[0]
        for card in preview["review_examples"]:
            if card["question_id"] == edited_qid:
                patches.append({"steps": list(teacher_steps),
                                "reasoning_question": teacher_reason})
            else:
                patches.append({})
        sections = {s["kind"]: s["text"]
                    for s in preview["sections"]}
        saved = service.save_feedback_edit(sid, {
            "message_sha256": detail["feedback_delivery"]
            ["preview_sha256"],
            "sections": sections,
            "content": {"review_examples": patches}})
        self.assertTrue(saved["saved"])
        released = service.release_feedback(sid, {
            "message_sha256": saved["preview_sha256"]})
        self.assertTrue(released["released"])
        out = service._load_meta(
            sid)["feedback_release"]["released_feedback"]
        card = next(c for c in out["review_examples"]
                    if c["question_id"] == edited_qid)
        self.assertEqual(card["steps"], teacher_steps)
        self.assertEqual(card["reasoning_question"], teacher_reason)
        self.assertTrue(card["teacher_edited"])
        # original_content preserves the provider wording for audit.
        self.assertEqual(card["original_content"]["steps"],
                         list(AUTHORED[0][0]))
        snapshot = service.snapshot(sid, token)
        released_card = next(
            c for c in snapshot["feedback"]["end"]["review_examples"]
            if c["question_id"] == edited_qid)
        self.assertEqual(released_card["steps"], teacher_steps)
        self.assertNotIn("rejected_draft", json.dumps(snapshot))

    def test_question_first_gets_no_overlay(self):
        class Stub:
            supports_full_feedback = True
            execution = None

            def generate(self, payload):
                return {"candidate_id":
                        payload["selected_candidate"]["candidate_id"],
                        "sections": dict(VALID_SECTIONS)}

        service = DemoService(
            root=Path(self.tmp.name) / "svc_qf",
            bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            provider_mode="hosted", generator=Stub())
        self.addCleanup(service.close, wait=True)
        created = service.create_session(
            feedback_variant="question_first")
        sid, token = (created["session_id"], created["student_token"])
        for row in self.rows:
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        review = service._load_meta(
            sid)["checkpoints"]["end"]["student_review"]
        self.assertNotIn("worked_review_sources", review["trace"])
        for card in review.get("review_examples") or []:
            self.assertNotIn("explanation_source", card)
            self.assertNotIn("checked_steps", card)

    def test_legacy_two_field_reply_keeps_deterministic_cards(self):
        # A stub generator that returns the legacy two-field reply in
        # worked_first mode keeps the deterministic checked cards.
        class Stub:
            supports_full_feedback = True
            execution = None

            def generate(self, payload):
                return {"candidate_id":
                        payload["selected_candidate"]["candidate_id"],
                        "sections": dict(VALID_SECTIONS)}

        service = DemoService(
            root=Path(self.tmp.name) / "svc_legacy",
            bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            provider_mode="hosted", generator=Stub())
        self.addCleanup(service.close, wait=True)
        created = service.create_session(feedback_variant="worked_first")
        sid, token = (created["session_id"], created["student_token"])
        for row in self.rows:
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        review = service._load_meta(
            sid)["checkpoints"]["end"]["student_review"]
        sources = review["trace"].get("worked_review_sources") or {}
        self.assertTrue(all(source == "application"
                            for source in sources.values()))
        for card in review["review_examples"]:
            self.assertNotEqual(card.get("explanation_source"),
                                "provider")
            self.assertNotIn("checked_steps", card)

    def test_malformed_wire_falls_back_and_keeps_draft(self):
        opener = WorkedReviewOpener(
            reviews_override=lambda ready: [
                {"question_id": "unknown_qid", "steps": ["x"],
                 "reasoning_question": "Why?"}])
        generator = self._generator(opener)
        service = DemoService(
            root=Path(self.tmp.name) / "svc_bad",
            bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            provider_mode="hosted", generator=generator)
        self.addCleanup(service.close, wait=True)
        created = service.create_session(feedback_variant="worked_first")
        sid, token = (created["session_id"], created["student_token"])
        for row in self.rows:
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        meta = service._load_meta(sid)
        self.assertEqual(meta["provider_job"]["status"], "fallback")
        gen_exec = meta["provider_job"]["executions"]["student"][
            "generator"]
        self.assertIsNotNone(gen_exec.get("rejected_draft"))
        review = meta["checkpoints"]["end"]["student_review"]
        for card in review["review_examples"]:
            self.assertNotIn("checked_steps", card)
        snapshot = service.snapshot(sid, token)
        self.assertNotIn("rejected_draft", json.dumps(snapshot))

    def test_deterministic_preview_comparison_teacher_only(self):
        class SectionsStub:
            supports_full_feedback = True
            execution = None

            def generate(self, payload):
                return {"candidate_id":
                        payload["selected_candidate"]["candidate_id"],
                        "sections": dict(VALID_SECTIONS)}

        cases = [
            ("worked_first", "aligned_two_topic", "worked_first", True),
            ("question_first", "aligned_two_topic", "question_first",
             False),
            ("legacy_single", "legacy_single", "question_first", False)]
        for name, review_mode, variant, real_opener in cases:
            with self.subTest(variant=name):
                if real_opener:
                    generator = FullFeedbackAittaGenerator(
                        AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                                       opener=WorkedReviewOpener()))
                else:
                    generator = SectionsStub()
                service = DemoService(
                    root=Path(self.tmp.name) / f"svc_dp_{name}",
                    bank=self.bank, taxonomy=self.taxonomy,
                    diagnostics=fake_diagnostics(self.bank),
                    provider_mode="hosted", generator=generator,
                    review_mode=review_mode)
                self.addCleanup(service.close, wait=True)
                created = service.create_session(
                    feedback_variant=variant)
                sid, token = (created["session_id"],
                              created["student_token"])
                for row in self.rows:
                    service.submit_response(sid, token, row)
                self.assertTrue(wait_for(
                    lambda: service._load_meta(sid)["provider_job"]
                    ["status"] in ("ready", "fallback")))
                meta_before = copy.deepcopy(service._load_meta(sid))
                self.assertEqual(meta_before["provider_job"]["status"],
                                 "ready")
                detail = service.teacher_session(sid)
                delivery = detail["feedback_delivery"]
                det = delivery["deterministic_preview"]
                self.assertIsNotNone(det)
                # It equals _final_preview over the stored baseline
                # exactly as the helper specifies.
                meta = service._load_meta(sid)
                copy_meta = copy.deepcopy(meta)
                copy_meta["feedback_edit"] = None
                copy_meta["checkpoints"]["end"]["student_review"] = (
                    copy_meta["checkpoints"]["end"]
                    ["baseline_student"])
                copy_meta["provider_job"]["status"] = "fallback"
                self.assertEqual(
                    det, service._final_preview(copy_meta))
                # The read never mutates persisted meta or hashes.
                self.assertEqual(service._load_meta(sid), meta_before)
                main = delivery["preview"]
                self.assertNotEqual(det["sections"],
                                    main["sections"])
                self.assertNotIn(
                    AUTHORED[0][0][0], json.dumps(det))
                if name == "worked_first":
                    # Checked baseline cards keep the deterministic
                    # steps and the correct answer, never the provider
                    # explanation.
                    for card in det.get("review_examples") or []:
                        self.assertNotEqual(
                            card.get("explanation_source"), "provider")
                        self.assertIsNotNone(
                            card.get("correct_answer"))
                    authored = {step for steps, _q in AUTHORED
                                for step in steps}
                    for card in det.get("review_examples") or []:
                        self.assertFalse(
                            authored & set(card.get("steps") or []))
                # The student snapshot never exposes the comparison.
                snap = service.snapshot(sid, token)
                self.assertNotIn("deterministic_preview",
                                 json.dumps(snap))
                # Teacher edits change only the main preview.
                section_keys = {"assessment_summary",
                                "observed_strengths",
                                "review_focus", "next_steps"}
                sections = {s["kind"]: s["text"]
                            for s in main["sections"]
                            if s["kind"] in section_keys}
                if set(sections) == section_keys:
                    sections["assessment_summary"] = (
                        "Teacher-edited summary wording.")
                    saved = service.save_feedback_edit(sid, {
                        "message_sha256": delivery["preview_sha256"],
                        "sections": sections})
                    self.assertTrue(saved["saved"])
                    edited = service.teacher_session(sid)
                    edited_delivery = edited["feedback_delivery"]
                    self.assertEqual(edited_delivery["preview_source"],
                                     "teacher_edited")
                    self.assertEqual(
                        edited_delivery["deterministic_preview"], det)
                    self.assertNotEqual(
                        edited_delivery["preview"]["sections"],
                        main["sections"])
                    self.assertEqual(service._load_meta(sid)
                                     ["checkpoints"]["end"]
                                     ["student_review"],
                                     meta_before["checkpoints"]["end"]
                                     ["student_review"])


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1",
                     "opt-in installed Edge + websocket-client browser test")
class WorkedReviewBrowserTests(unittest.TestCase):
    """One focused render check: provider steps/reasoning display with
    provenance in the teacher preview; practice cards untouched."""

    def setUp(self):
        from tests.test_research_browser import Browser
        edge = Path(os.environ.get(
            "EDGE_BINARY",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"))
        if not edge.is_file():
            self.skipTest("Microsoft Edge is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bank = make_grounded_bank()
        self.taxonomy = make_grounded_taxonomy(self.bank)
        wrong = sorted({positions(self.bank, "pct", 0)[0],
                        positions(self.bank, "alg", 0)[0]})
        self.rows = grounded_rows(self.bank, wrong)
        self.opener = WorkedReviewOpener()
        generator = FullFeedbackAittaGenerator(
            AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                           opener=self.opener))
        self.service = DemoService(
            root=self.root / "data", bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            provider_mode="hosted", generator=generator)
        self.addCleanup(self.service.close, wait=True)
        self.pin = f"{secrets.randbelow(1000000):06d}"
        self.server = make_server(self.service, self.pin)
        thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        profile = self.root / "edge"
        self.process = subprocess.Popen([
            str(edge), "--remote-debugging-port=0",
            f"--user-data-dir={profile}", "--headless=new",
            "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--window-size=1440,1100",
            "about:blank"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_browser)
        port_file = profile / "DevToolsActivePort"
        for _ in range(100):
            if port_file.exists():
                break
            time.sleep(.1)
        port = port_file.read_text().splitlines()[0]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json", timeout=10) as response:
            pages = json.load(response)
        self.browser = Browser(next(
            p["webSocketDebuggerUrl"] for p in pages
            if p["type"] == "page"))
        self.browser.viewport(1440, 1100)

    def stop_browser(self):
        if hasattr(self, "browser"):
            try:
                self.browser.command("Browser.close")
            except Exception:
                pass
            self.browser.socket.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)

    def test_provider_worked_review_rendered_with_provenance(self):
        created = self.service.create_session(
            feedback_variant="worked_first")
        sid, token = (created["session_id"], created["student_token"])
        for row in self.rows:
            self.service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]
            ["status"] == "ready"),
            "provider job did not reach ready")
        b = self.browser
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)}; "
             "document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('.session-row')")
        b.js("document.querySelector('.session-row').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('[data-tab=feedback]').click()")
        b.wait("document.querySelector('#tab-feedback .review-example')")
        detail = "document.querySelector('#tab-feedback')"
        text = b.js(f"{detail}.textContent")
        self.assertIn(
            "10% means one tenth of the whole amount.", text)
        self.assertIn(
            "Why does dividing 50 into ten equal parts give 10%?",
            text)
        self.assertIn(
            "AI-drafted explanation based on checked mathematics; "
            "teacher review required.", text)
        # The deterministic checked steps stay available on the card
        # data (not rendered as the explanation) and IDs are intact.
        self.assertNotIn("checked_steps", text)
        if os.environ.get("BROWSER_SCREENSHOTS"):
            box = "#tab-feedback .review-example"
            b.js(
                f"let el_ = document.querySelector('{box}');"
                "let n = el_; while (n) { if (n.tagName === 'DETAILS')"
                " n.open = true; n = n.parentElement; }"
                "el_.scrollIntoView({block:'center'});")
            b.wait("document.querySelector('" + box +
                   "').getBoundingClientRect().top > 0")
            b.screenshot("llm_worked_review_desktop.png")
        # The deterministic comparison sits in a collapsed disclosure
        # after the main preview and is not shown by default.
        self.assertFalse(b.js(
            "document.querySelector('.end-deterministic-comparison')"
            " === null"))
        self.assertFalse(b.js(
            "document.querySelector('.end-deterministic-comparison')"
            ".open"))
        meta = self.service._load_meta(sid)
        det = self.service._deterministic_preview(meta)
        det_marker = next(
            step for card in det.get("review_examples") or []
            for step in card.get("steps") or [])
        self.assertNotIn("Preview hash", b.js(
            "document.querySelector('#tab-feedback .release-card')"
            ".textContent"))
        hash_before = (self.service.teacher_session(sid)
                       ["feedback_delivery"]["preview_sha256"])
        b.js("document.querySelector('.end-deterministic-comparison')"
             ".open = true")
        comparison = b.js(
            "document.querySelector('.end-deterministic-comparison')"
            ".textContent")
        self.assertIn(det_marker, comparison)
        self.assertIn("Deterministic template version", comparison)
        self.assertIn(det["template_version"], comparison)
        self.assertIn("not the preview being approved", comparison)
        self.assertNotIn(AUTHORED[0][0][0], comparison)
        # The removed "Original question wording" disclosures are gone
        # from the main preview and the expanded comparison; English
        # prompts/options render directly on the cards instead.
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-feedback "
            "details[data-disclosure-key^=\"worked-review-original:\"], "
            "#tab-feedback details[data-disclosure-key^="
            "\"practice-original\"]').length"), 0)
        self.assertFalse(b.js(
            "document.querySelector('#tab-feedback').textContent"
            ".includes('Original question wording')"))
        preview_text = b.js(
            "document.querySelector('#tab-feedback .feedback-preview')"
            ".textContent")
        for host_text in (preview_text, comparison):
            self.assertIn("Simplify: 3x + 2x", host_text)
            self.assertNotIn("Sievennä", host_text)
        # Opening the comparison alone changed neither the preview hash
        # nor the approval payload.
        hash_after = (self.service.teacher_session(sid)
                      ["feedback_delivery"]["preview_sha256"])
        self.assertEqual(hash_before, hash_after)
        # One canonical wording-source row: the fixture generator is not
        # capture-wrapped, so confirm Aitta via offline execution
        # metadata on the stored provider job.
        with self.service.lock:
            meta2 = self.service._load_meta(sid)
            execs = meta2["provider_job"].setdefault("executions", {})
            execs.setdefault("student", {})["generator"] = {
                "capture_file": "aitta_full_test_attempt_1.json",
                "reused": False,
                "original_call_latency_ms": 17731.54,
                "automatic_retry": {"attempts": 2, "max_attempts": 3,
                                    "stop_reason": "success"},
                "metadata": {"status": "completed"}}
            self.service._save_meta(meta2)
        release_card = ("document.querySelector('#tab-feedback "
                        ".release-card')")
        b.wait(f"{release_card}.textContent.includes('Aitta-generated')")
        card_text = b.js(f"{release_card}.textContent")
        self.assertFalse(b.js(
            f"{release_card}.querySelector('.end-wording-source')"
            " === null"))
        self.assertIn("Worked explanations", card_text)
        # header strip only — generation details sit inside the card
        strip_text = b.js(
            "document.querySelector('#tab-feedback .release-card "
            ".release-status').textContent")
        self.assertNotIn("Selection provenance", strip_text)
        self.assertNotIn("Aitta provenance", strip_text)
        # Pure helper coverage: deterministic, unconfirmed provider,
        # teacher-edited variants and partial section authoring.
        cases = b.js(
            "(() => ({"
            "det: endFeedbackSource("
            "{trace:{phrasing_source:'deterministic'}},"
            "{preview_source:'deterministic'},{}),"
            "edited: endFeedbackSource("
            "{trace:{phrasing_source:'injected_generator_full_sections'}},"
            "{preview_source:'teacher_edited'},"
            "{capture_file:'aitta_full_x.json',"
            "metadata:{status:'completed'}}),"
            "unknown: endFeedbackSource({},"
            "{preview_source:'teacher_edited'},{}),"
            "unconf: endFeedbackSource("
            "{trace:{phrasing_source:'injected_generator_full_sections'}},"
            "{},{}),"
            "partial: endFeedbackSource("
            "{trace:{phrasing_source:'injected_generator_full_sections',"
            "section_sources:{assessment_summary:'provider',"
            "next_steps:'rules'}}},{},{}),"
            "openingOnly: endFeedbackSource("
            "{trace:{phrasing_source:'injected_generator_opening_only'}},"
            "{},{}),"
            "editedPartial: endFeedbackSource("
            "{trace:{phrasing_source:'injected_generator_opening_only'}},"
            "{preview_source:'teacher_edited'},"
            "{capture_file:'aitta_full_x.json',"
            "metadata:{status:'completed'}})}))()")
        self.assertEqual(cases["det"]["label"], "Deterministic")
        self.assertEqual(cases["edited"]["label"],
                         "Aitta-generated, then teacher-edited")
        self.assertEqual(cases["unknown"]["label"],
                         "Teacher-edited (original source unavailable)")
        self.assertEqual(cases["unconf"]["label"], "AI-generated")
        self.assertTrue(cases["partial"]["partial"])
        self.assertTrue(cases["openingOnly"]["partial"])
        self.assertEqual(cases["editedPartial"]["label"],
                         "Aitta-generated, then teacher-edited")
        self.assertTrue(cases["editedPartial"]["partial"])
        self.assertFalse(cases["det"]["partial"])
        # A saved teacher edit flips the canonical label via polling;
        # hashes and release semantics are unchanged.
        detail = self.service.teacher_session(sid)
        preview = detail["feedback_delivery"]["preview"]
        sections = {s["kind"]: s["text"] for s in preview["sections"]}
        sections["assessment_summary"] = "Teacher-reviewed wording."
        saved = self.service.save_feedback_edit(sid, {
            "message_sha256": detail["feedback_delivery"]
            ["preview_sha256"],
            "sections": sections})
        self.assertTrue(saved["saved"])
        b.wait(f"{release_card}.textContent.includes("
               "'Aitta-generated, then teacher-edited')")
        card_text = b.js(f"{release_card}.textContent")
        self.assertIn("Last edited", card_text)
        self.assertEqual(b.js(
            f"{release_card}.textContent.split('teacher-edited')"
            ".length - 1"), 1)
        # Research keeps review evidence and generation metadata but
        # drops the old comparison columns and raw dumps.
        research = ("document.querySelector('#tab-feedback "
                    "details[data-disclosure-key=feedback-research]')")
        research_text = b.js(f"{research}.textContent")
        self.assertIn("Review evidence", research_text)
        self.assertIn("Review topics chosen by", research_text)
        self.assertIn("Application rules", research_text)
        self.assertIn("Topic selection is separate from feedback "
                      "wording", research_text)
        self.assertIn("Generation details", research_text)
        self.assertIn("Aitta provenance", research_text)
        self.assertIn("17731.54 ms", research_text)
        self.assertIn("2 of 3", research_text)
        self.assertNotIn("Shared feedback draft", research_text)
        self.assertNotIn("All candidates", research_text)
        self.assertNotIn("Provider executions", research_text)
        self.assertIsNone(b.js(
            f"{research}.querySelector('.compare')"))
        self.assertIsNone(b.js(
            f"{research}.querySelector('.decision-table')"))
        # Generation details live inside the release card directly
        # after the deterministic comparison and before the editing
        # and approval controls — one instance, collapsed by default.
        compare = ("document.querySelector('#tab-feedback "
                   ".end-deterministic-comparison')")
        self.assertTrue(b.js(
            f"{research}.closest('.release-card') === {release_card}"))
        self.assertTrue(b.js(
            f"{compare}.nextElementSibling === {research}"))
        self.assertTrue(b.js(
            f"{research}.compareDocumentPosition("
            "document.querySelector('#tab-feedback "
            "details[data-disclosure-key=feedback-editing]'))"
            " & Node.DOCUMENT_POSITION_FOLLOWING"))
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-feedback "
            "details[data-disclosure-key=feedback-research]')"
            ".length"), 1)
        self.assertFalse(b.js(f"{research}.open"))
        # Both disclosures stay open across a real poll redraw —
        # bump a harmless meta field so the poll payload differs.
        b.js(f"{research}.open = true")
        with self.service.lock:
            meta3 = self.service._load_meta(sid)
            meta3["label"] = meta3.get("label", "") + " (redraw)"
            self.service._save_meta(meta3)
        b.js("window.__detT0 = Date.now()")
        b.wait("Date.now() - window.__detT0 > 3000")
        self.assertTrue(b.js(
            "document.querySelector('.end-deterministic-comparison')"
            ".open"))
        self.assertTrue(b.js(f"{research}.open"))
        self.assertTrue(b.js(
            f"{compare}.nextElementSibling === {research}"))
        # Release and confirm the shared student renderer also has no
        # original-wording disclosures and shows the English question.
        detail = self.service.teacher_session(sid)
        released = self.service.release_feedback(sid, {
            "message_sha256": detail["feedback_delivery"]
            ["preview_sha256"], "reviewer_label": "browser-test"})
        self.assertTrue(released["released"])
        b.command("Page.navigate", {
            "url": f"{self.url}/?session={sid}#token={token}"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.wait("document.body.textContent.includes('Worked review')",
               timeout=30)
        self.assertFalse(b.js(
            "document.body.textContent.includes("
            "'Original question wording')"))
        self.assertEqual(b.js(
            "document.querySelectorAll('details[data-disclosure-key^="
            "\"worked-review-original:\"], details[data-disclosure-key^="
            "\"practice-original\"]').length"), 0)
        student_text = b.js("document.body.textContent")
        self.assertIn("Simplify: 3x + 2x", student_text)
        self.assertNotIn("Sievennä", student_text)
        self.assertEqual(b.errors, [])

    def test_single_document_editor_roundtrip(self):
        created = self.service.create_session(
            feedback_variant="worked_first")
        sid, token = (created["session_id"], created["student_token"])
        for row in self.rows:
            self.service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]
            ["status"] == "ready"),
            "provider job did not reach ready")
        b = self.browser
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)}; "
             "document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('.session-row')")
        b.js("document.querySelector('.session-row').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('[data-tab=feedback]').click()")
        b.wait("document.querySelector('#tab-feedback .feedback-preview')")
        # Default editor: one document textarea; advanced editor stays
        # collapsed.
        b.js("document.querySelector("
             "'#tab-feedback details[data-disclosure-key"
             "=\"feedback-editing\"]').open = true")
        doc = ("document.querySelector('#tab-feedback "
               ".edit-document')")
        b.wait(f"Boolean({doc})")
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-feedback "
            ".edit-document').length"), 1)
        self.assertFalse(b.js(
            "document.querySelector('#tab-feedback "
            "details[data-disclosure-key=feedback-editing-advanced]')"
            ".open"))
        # The document is one contenteditable body rendered as real
        # blocks: headings, paragraphs, lists — the displayed English
        # wording with no scores or research internals inside.
        self.assertTrue(b.js(f"{doc}.isContentEditable"))
        self.assertTrue(b.js(f"Boolean({doc}.querySelector('h3'))"))
        self.assertTrue(b.js(f"Boolean({doc}.querySelector('p'))"))
        self.assertTrue(
            b.js(f"Boolean({doc}.querySelector('ul,ol'))"))
        self.assertFalse(b.js(f"Boolean({doc}.querySelector("
            "'.assessment-results,.score'))"))
        doc_text = b.js(f"{doc}.textContent")
        for needle in ("10% means one tenth of the whole amount.",
                       "Explain your reasoning:",
                       "Simplify: 3x + 2x"):
            self.assertIn(needle, doc_text)
        for absent in ("preview_sha256", "capture_file",
                       "correct out of"):
            self.assertNotIn(absent, doc_text)
        # Template identity is shown separately from authorship and
        # presentation; the worked-first preview carries its template
        # version into research generation details.
        card_text = b.js(
            "document.querySelector('#tab-feedback .release-card')"
            ".textContent")
        self.assertIn("Base feedback template", card_text)
        self.assertIn("Worked-first", card_text)
        self.assertIn("Structured feedback cards", card_text)
        gen_text = b.js(
            "document.querySelector('#tab-feedback "
            ".end-generation-details').textContent")
        self.assertIn("Template version", gen_text)
        self.assertIn(
            "phase3_student_feedback_worked_first_full_v1", gen_text)
        # Pure helper: stored template version wins over the recorded
        # variant; unknown versions and missing data are truthful.
        cases = json.loads(b.js(
            "JSON.stringify(["
            "feedbackTemplateLabel({template_version:"
            "'phase3_student_feedback_template_v9'},'worked_first'),"
            "feedbackTemplateLabel({template_version:'tpl_unknown_0'},"
            "'worked_first'),"
            "feedbackTemplateLabel({},'worked_first'),"
            "feedbackTemplateLabel(null,'question_first'),"
            "feedbackTemplateLabel({},null)])"))
        self.assertEqual(cases, [
            "Question-first",
            "Recorded template (unrecognized version)",
            "Worked-first",
            "Question-first",
            "Historical / unspecified"])
        # DOM -> blocks keeps BR line breaks, nested block boundaries
        # and inline text, while dropping ignored/control nodes.
        fixture = b.js(
            "(()=>{const host=document.createElement('div');"
            "const p=document.createElement('p');"
            "p.appendChild(document.createTextNode('Line one'));"
            "p.appendChild(document.createElement('br'));"
            "p.appendChild(document.createTextNode('line two'));"
            "host.appendChild(p);"
            "const wrap=document.createElement('section');"
            "const inner=document.createElement('div');"
            "inner.textContent='Inner block';"
            "const btn=document.createElement('button');"
            "btn.textContent='ignore me';"
            "inner.appendChild(btn);"
            "wrap.appendChild(inner);"
            "const span=document.createElement('span');"
            "span.textContent='span tail';"
            "wrap.appendChild(span);"
            "host.appendChild(wrap);"
            "return JSON.stringify(documentBlocksFromDom(host))})()")
        parsed = json.loads(fixture)
        self.assertEqual(parsed[0],
                         {"kind": "paragraph",
                          "text": "Line one\nline two"})
        self.assertEqual(parsed[1],
                         {"kind": "paragraph", "text": "Inner block"})
        self.assertEqual(parsed[2],
                         {"kind": "paragraph", "text": "span tail"})
        # Plain stored text also parses singleton and blank-separated
        # bullets into real list blocks.
        parsed_text = json.loads(b.js(
            "JSON.stringify(parseDocumentTextBlocks({sections: []}, "
            f"{json.dumps('Intro.\n\n- one\n\n- two\n\n1. a\n\n2. b')}))"))
        self.assertEqual(parsed_text[1],
                         {"kind": "unordered_list",
                          "items": ["one", "two"]})
        self.assertEqual(parsed_text[2],
                         {"kind": "ordered_list",
                          "items": ["a", "b"]})
        # Edit a paragraph in place; the edit marks the draft unsaved.
        b.js(f"const para=[...{doc}.querySelectorAll('p,li,h3')]"
             ".find((n) => n.textContent.includes("
             "'10% means one tenth')); "
             "para.textContent='A teacher-edited tenth is "
             "one tenth of the whole amount.'; "
             f"{doc}.dispatchEvent(new Event('input'))")
        approve = ("document.querySelector('#tab-feedback "
                   ".release-card button.primary')")
        self.assertTrue(b.js(f"{approve}.disabled"))
        # Realistic typing at the caret plus Enter splits the block.
        b.js(f"{doc}.focus(); "
             f"const t=[...{doc}.childNodes]; "
             "const r=document.createRange(); "
             f"r.selectNodeContents({doc}.querySelector('p')); "
             "r.collapse(false); const s=window.getSelection(); "
             "s.removeAllRanges(); s.addRange(r)")
        b.command("Input.insertText", {"text": " Typed inline."})
        for ev_type in ("rawKeyDown", "char", "keyUp"):
            params = {"type": ev_type, "key": "Enter",
                      "code": "Enter", "windowsVirtualKeyCode": 13,
                      "nativeVirtualKeyCode": 13}
            if ev_type == "char":
                params["text"] = "\r"
                params["unmodifiedText"] = "\r"
            b.command("Input.dispatchKeyEvent", params)
        b.command("Input.insertText", {"text": "New paragraph line."})
        # Diagnostic: record the real contenteditable DOM after Enter
        # (artifact only; never a data source).
        Path(os.environ.get(
            "BROWSER_SCREENSHOTS",
            tempfile.gettempdir())
            ).mkdir(parents=True, exist_ok=True)
        (Path(os.environ.get("BROWSER_SCREENSHOTS",
                             tempfile.gettempdir()))
         / "end_document_editor_dom.html").write_text(
            b.js(f"{doc}.outerHTML"), encoding="utf-8")
        # HTML pasted by a teacher lands as literal text, never markup.
        b.js("const dt=new DataTransfer(); "
             "dt.setData('text/html','<img src=y onerror=alert(2)>'); "
             "dt.setData('text/plain','<img src=y onerror=alert(2)>'); "
             f"{doc}.dispatchEvent(new ClipboardEvent('paste', "
             "{clipboardData: dt, bubbles: true, cancelable: true}))")
        self.assertFalse(
            b.js(f"Boolean({doc}.querySelector('img'))"))
        self.assertIn("<img src=y", b.js(f"{doc}.textContent"))
        # Select backwards across two blocks, then force a real poll
        # redraw by changing the session label server-side; the
        # buffer and the exact anchor/focus offsets must survive.
        b.js(f"const s=window.getSelection(); "
             f"const parts=[...{doc}.children]; "
             "const textAt=(el,i)=>{const w=document.createTreeWalker("
             "el,NodeFilter.SHOW_TEXT);let n=w.nextNode();"
             "while(i--)n=n.nextNode();return n};"
             "s.setBaseAndExtent(textAt(parts[2],0),"
             "textAt(parts[2],0).textContent.length,"
             "textAt(parts[0],0),1)")
        grab = (f"(()=>{{const s=window.getSelection();"
                f"const off=(n,o)=>{{const r=document.createRange();"
                f"r.setStart({doc},0);r.setEnd(n,o);"
                "return r.toString().length};"
                "return JSON.stringify({anchor:off(s.anchorNode,"
                "s.anchorOffset),focus:off(s.focusNode,"
                "s.focusOffset)})})()")
        before = b.js(grab)
        b.js(f"window.__oldDoc = {doc}")
        meta = self.service._load_meta(sid)
        meta["label"] = meta["label"] + " (edited)"
        self.service._save_meta(meta)
        # The label change makes the next poll payload differ, so the
        # pane actually redraws and the old editor node disconnects.
        b.wait("!window.__oldDoc.isConnected", timeout=15)
        time.sleep(0.3)
        self.assertEqual(b.js(grab), before)
        doc_text = b.js(f"{doc}.textContent")
        self.assertIn("Typed inline.", doc_text)
        self.assertIn("New paragraph line.", doc_text)
        # Enter must have produced a real block boundary, not merged
        # text in the same paragraph.
        live_blocks = json.loads(b.js(
            f"JSON.stringify(documentBlocksFromDom({doc}))"))
        live_paras = [blk["text"] for blk in live_blocks
                      if blk["kind"] == "paragraph"]
        self.assertTrue(any(t.startswith("New paragraph line.")
                            for t in live_paras))
        self.assertTrue(any(t.endswith("Typed inline.")
                            for t in live_paras))
        # A caret on a fresh empty paragraph is an element boundary;
        # it must restore to that empty block (not the previous word's
        # end) after a forced poll redraw.
        b.js(f"{{ {doc}.focus(); const sel2=window.getSelection(); "
             f"const r=document.createRange(); "
             f"r.selectNodeContents({doc}); r.collapse(false); "
             f"sel2.removeAllRanges(); sel2.addRange(r) }}")
        for ev_type in ("rawKeyDown", "char", "keyUp"):
            params = {"type": ev_type, "key": "Enter",
                      "code": "Enter", "windowsVirtualKeyCode": 13,
                      "nativeVirtualKeyCode": 13}
            if ev_type == "char":
                params["text"] = "\r"
                params["unmodifiedText"] = "\r"
            b.command("Input.dispatchKeyEvent", params)
        empty_anchor = b.js(
            "window.getSelection().anchorNode.nodeType === 1 ? "
            "'element' : 'text'")
        b.js(f"window.__oldDoc = {doc}")
        meta = self.service._load_meta(sid)
        meta["label"] = meta["label"] + "!"
        self.service._save_meta(meta)
        b.wait("!window.__oldDoc.isConnected", timeout=15)
        time.sleep(0.3)
        anchor_kind = b.js(
            "window.getSelection().anchorNode.nodeType === 1 ? "
            "'element' : 'text'")
        self.assertEqual(anchor_kind, empty_anchor)
        self.assertFalse(b.js(
            "window.getSelection().anchorNode.textContent.trim()"))
        # Opening the advanced editor only inspects; it must not switch
        # the save mode or lock the document.
        adv = ("document.querySelector('#tab-feedback "
               "details[data-disclosure-key="
               "\"feedback-editing-advanced\"]')")
        b.js(f"{adv}.open = true")
        self.assertTrue(b.js(f"{doc}.isContentEditable"))
        self.assertIn("single document", b.js(
            "document.querySelector('#tab-feedback "
            ".feedback-editor-mode').textContent"))
        # Typing in a structured field explicitly selects structured
        # mode; 'Use single editor' returns without losing the buffer.
        sec = ("document.querySelector('#tab-feedback "
               "textarea.edit-section')")
        b.js(f"{sec}.value='Structured wording.'; "
             f"{sec}.dispatchEvent(new Event('input'))")
        self.assertFalse(b.js(f"{doc}.isContentEditable"))
        self.assertIn("advanced structured fields", b.js(
            "document.querySelector('#tab-feedback "
            ".feedback-editor-mode').textContent"))
        use_doc = ("[...document.querySelectorAll('#tab-feedback "
                   "button')].find((n) => n.textContent === "
                   "'Use single editor')")
        b.js(f"{use_doc}.click()")
        self.assertTrue(b.js(f"{doc}.isContentEditable"))
        self.assertIn("Typed inline.", b.js(f"{doc}.textContent"))
        if os.environ.get("BROWSER_SCREENSHOTS"):
            b.js("window.getSelection().removeAllRanges(); "
                 f"{doc}.scrollIntoView({{block: 'center'}})")
            b.screenshot("end_document_editor_desktop.png")
        # Save the document draft.
        save = ("[...document.querySelectorAll('#tab-feedback "
                ".release-card button')].find((n) => n.textContent === "
                "'Save edited draft')")
        b.js(f"{save}.click()")
        b.wait(f"{approve} && !{approve}.disabled", timeout=30)
        # Preview renders the saved blocks formatted once; old
        # structured cards are not duplicated underneath.
        preview_text = b.js(
            "document.querySelector('#tab-feedback .feedback-preview')"
            ".textContent")
        self.assertIn("A teacher-edited tenth is", preview_text)
        self.assertIn("<img src=y onerror=alert(2)>", preview_text)
        self.assertIn("Typed inline.", preview_text)
        self.assertIn("New paragraph line.", preview_text)
        # The Enter split produced two distinct paragraph nodes.
        paras = ("[...document.querySelectorAll('#tab-feedback "
                 ".feedback-preview .feedback-document p')]")
        self.assertTrue(b.js(
            f"Boolean({paras}.find((n) => n.textContent.startsWith("
            "'New paragraph line.')))"))
        self.assertTrue(b.js(
            f"Boolean({paras}.find((n) => n.textContent.endsWith("
            "'Typed inline.')))"))
        self.assertFalse(b.js(
            "document.querySelector('#tab-feedback .feedback-preview "
            "img, #tab-feedback .feedback-preview script')"))
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-feedback .feedback-preview "
            ".feedback-document').length"), 1)
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-feedback .feedback-preview "
            ".feedback-section').length"), 0)
        self.assertTrue(b.js(
            "Boolean(document.querySelector('#tab-feedback "
            ".feedback-preview .feedback-document h3'))"))
        self.assertTrue(b.js(
            "Boolean(document.querySelector('#tab-feedback "
            ".feedback-preview .feedback-document ul, #tab-feedback "
            ".feedback-preview .feedback-document ol'))"))
        self.assertIn("Explain your reasoning:", preview_text)
        card_after = b.js(
            "document.querySelector('#tab-feedback .release-card')"
            ".textContent")
        self.assertIn("Wording source", card_after)
        self.assertIn("teacher-edited", card_after)
        # Editing changes wording source and presentation, not the
        # underlying base template.
        self.assertIn("Worked-first", card_after)
        self.assertIn("Teacher-edited formatted document", card_after)
        # Approve and confirm the student sees the exact document with
        # fixed scores.
        b.js(f"{approve}.click()")
        b.wait("document.querySelector('#tab-feedback .release-card')"
               ".textContent.includes('Already released')", timeout=30)
        b.command("Page.navigate", {
            "url": f"{self.url}/?session={sid}#token={token}"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.wait("document.body.textContent.includes("
               "'A teacher-edited tenth is')", timeout=30)
        student_text = b.js("document.body.textContent")
        self.assertIn("A teacher-edited tenth is one tenth of the "
                      "whole amount.", student_text)
        self.assertIn("<img src=y onerror=alert(2)>", student_text)
        self.assertIn("New paragraph line.", student_text)
        self.assertFalse(b.js("Boolean(document.querySelector('img'))"))
        # Released student view keeps the formatted block structure.
        self.assertTrue(b.js(
            "Boolean(document.querySelector('#end-summary "
            ".feedback-document h3'))"))
        self.assertTrue(b.js(
            "Boolean(document.querySelector('#end-summary "
            ".feedback-document ul, #end-summary "
            ".feedback-document ol'))"))
        self.assertTrue(b.js(
            "document.body.textContent.includes('of 40')"))
        # Mobile width keeps the open editor inside the viewport.
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete'")
        b.js("if (document.querySelector('#pin-input')) {"
             f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)};"
             "document.querySelector('#unlock-form').requestSubmit() }")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('.session-row')")
        b.js("document.querySelector('.session-row').click()")
        b.js("document.querySelector('[data-tab=feedback]').click()")
        b.wait("document.querySelector('#tab-feedback .edit-document')")
        b.js("document.querySelector('#tab-feedback "
             "details[data-disclosure-key=\"feedback-editing\"]')"
             ".open = true")
        b.viewport(390, 844)
        overflow = b.js(
            "document.documentElement.scrollWidth - "
            "document.documentElement.clientWidth")
        self.assertLessEqual(overflow, 0)
        if os.environ.get("BROWSER_SCREENSHOTS"):
            b.screenshot("end_document_editor_mobile.png")
        b.viewport(1440, 1100)
        self.assertEqual(b.errors, [])


if __name__ == "__main__":
    unittest.main()
