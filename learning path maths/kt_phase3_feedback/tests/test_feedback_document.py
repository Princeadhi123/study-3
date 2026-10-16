"""Single-document teacher edits for end feedback.

`document_text` is an alternative save body mutually exclusive with
the legacy sections/content payload. The saved document is the exact
teacher wording; structured sections/cards stay as the retained
original and scores are never editable. All fixtures are offline.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import phase3_paths  # noqa: F401
from demo_service import ConflictError, DemoService
from full_feedback import FullFeedbackAittaGenerator
from aitta_generator import AittaGenerator
from tests.test_assessment_worked_review import (
    grounded_rows, make_grounded_bank, make_grounded_taxonomy)
from tests.test_demo_service import fake_diagnostics, wait_for
from tests.test_full_feedback import API_KEY, BASE_URL, MODEL
from tests.test_llm_worked_reviews import WorkedReviewOpener


class SectionsStub:
    supports_full_feedback = True
    execution = None

    def generate(self, payload):
        return {"candidate_id": payload["selected_candidate"]
                ["candidate_id"],
                "sections": {
                    "assessment_summary": "Authored summary.",
                    "observed_strengths": "Authored strengths.",
                    "review_focus": "Authored focus.",
                    "next_steps": "Authored steps."}}


DOC = ("Edited summary line one.\n\n"
       "- Checked step: 10% means one tenth.\n\n"
       "  Indented unicode note — caf\u00e9 \u00d7 2.")


def _service(root, variant):
    bank = make_grounded_bank()
    taxonomy = make_grounded_taxonomy(bank)
    if variant == "worked_first":
        generator = FullFeedbackAittaGenerator(
            AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                           opener=WorkedReviewOpener()))
    else:
        generator = SectionsStub()
    return bank, DemoService(
        root=root / "data", bank=bank, taxonomy=taxonomy,
        diagnostics=fake_diagnostics(bank), provider_mode="hosted",
        review_mode=("legacy_single" if variant == "single"
                     else "aligned_two_topic"),
        generator=generator)


def _ready(service, bank, variant):
    kwargs = ({"feedback_variant": variant} if variant != "single"
              else {})
    created = service.create_session(**kwargs)
    sid, token = created["session_id"], created["student_token"]
    for row in grounded_rows(bank, wrong_positions=(0, 5)):
        service.submit_response(sid, token, row)
    assert wait_for(
        lambda: service._load_meta(sid)["provider_job"]["status"]
        in ("ready", "fallback")), "provider job did not finish"
    delivery = service.teacher_session(sid)["feedback_delivery"]
    assert delivery["review_mode"] == (
        "legacy_single" if variant == "single"
        else "aligned_two_topic"), delivery["review_mode"]
    assert delivery["feedback_variant"] == (
        "question_first" if variant == "single" else variant), (
        delivery["feedback_variant"])
    return sid, token


class FeedbackDocumentTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _session(self, variant="single"):
        bank, service = _service(Path(self.tmp.name) / variant, variant)
        self.addCleanup(service.close, wait=True)
        sid, token = _ready(service, bank, variant)
        delivery = service.teacher_session(sid)["feedback_delivery"]
        return service, bank, sid, token, delivery

    def test_document_save_release_exact_and_scores_locked(self):
        for variant in ("single", "question_first", "worked_first"):
            with self.subTest(variant=variant):
                service, bank, sid, token, delivery = self._session(
                    variant)
                base_total = copy.deepcopy(
                    delivery["preview"]["total"])
                base_skills = copy.deepcopy(
                    delivery["preview"]["skills"])
                saved = service.save_feedback_edit(sid, {
                    "message_sha256": delivery["preview_sha256"],
                    "document_text": DOC,
                    "reviewer_label": "doc-tester"})
                self.assertTrue(saved["saved"])
                detail = service.teacher_session(sid)
                preview = detail["feedback_delivery"]["preview"]
                # Exact wording preserved verbatim.
                self.assertEqual(preview["document_text"], DOC)
                self.assertTrue(preview["document_edited"])
                self.assertEqual(preview["text"], DOC)
                # Scores/source data untouched.
                self.assertEqual(preview["total"], base_total)
                self.assertEqual(preview["skills"], base_skills)
                self.assertEqual(detail["feedback_delivery"]
                                 ["preview_source"], "teacher_edited")
                # Not student-visible before approval.
                snap = json.dumps(service.snapshot(sid, token))
                self.assertNotIn("document_text", snap)
                self.assertNotIn("Indented unicode note", snap)
                # Release uses the saved document hash exactly.
                released = service.release_feedback(sid, {
                    "message_sha256": saved["preview_sha256"],
                    "reviewer_label": "doc-tester"})
                self.assertTrue(released["released"])
                out = service._load_meta(
                    sid)["feedback_release"]["released_feedback"]
                self.assertEqual(out["document_text"], DOC)
                snap = service.snapshot(sid, token)
                self.assertEqual(
                    snap["feedback"]["end"]["document_text"], DOC)

    def test_document_validation(self):
        service, bank, sid, token, delivery = self._session()
        digest = delivery["preview_sha256"]
        sections = {s["kind"]: s["text"]
                    for s in delivery["preview"]["sections"]}
        cases = [
            {"message_sha256": digest, "document_text": DOC,
             "sections": sections},
            {"message_sha256": digest, "document_text": DOC,
             "content": {}},
            {"message_sha256": digest, "document_text": "   "},
            {"message_sha256": digest, "document_text": "x" * 32769},
            {"message_sha256": digest, "document_text": 42},
            {"message_sha256": digest, "document_text": "bad\x01"},
            {"message_sha256": digest, "document_text": DOC,
             "unknown_key": 1},
            {"message_sha256": digest},
        ]
        for body in cases:
            with self.subTest(body=sorted(body)):
                with self.assertRaises(ValueError):
                    service.save_feedback_edit(sid, body)
        # Stale hash -> 409 conflict.
        with self.assertRaises(ConflictError):
            service.save_feedback_edit(sid, {
                "message_sha256": "0" * 64, "document_text": DOC})

    def test_legacy_edit_clears_document_and_source_retained(self):
        service, bank, sid, token, delivery = self._session()
        first = service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "document_text": DOC})
        detail = service.teacher_session(sid)
        preview = detail["feedback_delivery"]["preview"]
        self.assertIn("document_text", preview)
        # A legacy structured save on the document revision drops the
        # document override explicitly.
        sections = {s["kind"]: s["text"]
                    for s in preview["sections"]}
        sections["assessment_summary"] = "Structured edit again."
        second = service.save_feedback_edit(sid, {
            "message_sha256": first["preview_sha256"],
            "sections": sections})
        self.assertTrue(second["saved"])
        preview = (service.teacher_session(sid)["feedback_delivery"]
                   ["preview"])
        self.assertNotIn("document_text", preview)
        self.assertEqual(preview["sections"][0]["text"],
                         "Structured edit again.")
        # The first pre-edit source preview is retained for audit.
        meta = service._load_meta(sid)
        src = meta["feedback_edit"]["source_preview"]
        self.assertNotIn("document_text", src)
        self.assertNotEqual(src["sections"][0]["text"],
                            "Structured edit again.")

    def test_document_max_length_boundary(self):
        service, bank, sid, token, delivery = self._session()
        doc = "x" * 32768
        saved = service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "document_text": doc})
        self.assertTrue(saved["saved"])
        preview = (service.teacher_session(sid)["feedback_delivery"]
                   ["preview"])
        self.assertEqual(preview["document_text"], doc)

    def test_incomplete_session_rejects_document(self):
        bank, service = _service(Path(self.tmp.name) / "inc",
                                 "single")
        self.addCleanup(service.close, wait=True)
        sid = service.create_session()["session_id"]
        with self.assertRaises(ValueError):
            service.save_feedback_edit(sid, {
                "message_sha256": "0" * 64, "document_text": DOC})

    def test_resave_keeps_first_source_and_release_pinning(self):
        service, bank, sid, token, delivery = self._session()
        first = service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "document_text": DOC})
        service.release_feedback(sid, {
            "message_sha256": first["preview_sha256"],
            "reviewer_label": "doc-tester"})
        # A second document save does not rewrite the released copy
        # and keeps the original source preview as audit baseline.
        second = service.save_feedback_edit(sid, {
            "message_sha256": first["preview_sha256"],
            "document_text": DOC + "\n\nSecond revision."})
        meta = service._load_meta(sid)
        src = meta["feedback_edit"]["source_preview"]
        self.assertNotIn("document_text", src)
        self.assertEqual(src["sections"][0]["text"],
                         delivery["preview"]["sections"][0]["text"])
        released = meta["feedback_release"]["released_feedback"]
        self.assertEqual(released["document_text"], DOC)
        snap = service.snapshot(sid, token)
        self.assertEqual(
            snap["feedback"]["end"]["document_text"], DOC)
        # Re-approval updates the student document exactly.
        service.release_feedback(sid, {
            "message_sha256": second["preview_sha256"],
            "reviewer_label": "doc-tester"})
        snap = service.snapshot(sid, token)
        self.assertEqual(snap["feedback"]["end"]["document_text"],
                         DOC + "\n\nSecond revision.")

    def test_document_blocks_validated_and_released(self):
        service, bank, sid, token, delivery = self._session()
        digest = delivery["preview_sha256"]
        blocks = [
            {"kind": "heading", "text": "Assessment summary"},
            {"kind": "paragraph", "text": "Line caf\u00e9 \u00d7 2."},
            {"kind": "paragraph", "text": ""},
            {"kind": "unordered_list",
             "items": ["first", "second"]},
            {"kind": "ordered_list",
             "items": ["step one", "step two"]}]
        doc = ("Assessment summary\n\nLine caf\u00e9 \u00d7 2.\n\n\n\n"
               "- first\n- second\n\n1. step one\n2. step two")
        saved = service.save_feedback_edit(sid, {
            "message_sha256": digest,
            "document_text": doc,
            "document_blocks": blocks})
        preview = (service.teacher_session(sid)["feedback_delivery"]
                   ["preview"])
        self.assertEqual(preview["document_blocks"], blocks)
        self.assertEqual(preview["document_text"], doc)
        service.release_feedback(sid, {
            "message_sha256": saved["preview_sha256"],
            "reviewer_label": "doc-tester"})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["document_blocks"], blocks)
        self.assertEqual(released["document_text"], doc)
        # (formatting/hash coverage lives in
        # test_blocks_formatting_changes_hash_and_plain_clears)

    def test_document_blocks_rejected(self):
        service, bank, sid, token, delivery = self._session()
        digest = delivery["preview_sha256"]
        doc = "Para one.\n\n- item a\n- item b"
        good = [{"kind": "paragraph", "text": "Para one."},
                {"kind": "unordered_list", "items": ["a", "b"]}]
        cases = [
            # blocks without document_text
            {"message_sha256": digest, "document_blocks": good},
            # null / empty list
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": None},
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": []},
            # non-dict / null blocks and non-string kinds
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": ["not a block"]},
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": [None]},
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": [{"kind": []}]},
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": [{"kind": {"a": 1}}]},
            # unknown kind / extra + html-ish fields
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": [{"kind": "image", "src": "x"}]},
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": [{"kind": "paragraph", "text": "x",
                                  "onclick": "alert(1)"}]},
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": [{"kind": "paragraph"}]},
            # malformed items
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": [{"kind": "unordered_list",
                                  "items": []}]},
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": [{"kind": "unordered_list",
                                  "items": ["ok", "  "]}]},
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": [{"kind": "unordered_list",
                                  "items": ["i"] * 101}]},
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": [{"kind": "ordered_list",
                                  "items": [{"nested": 1}]}]},
            # too many blocks
            {"message_sha256": digest, "document_text": "x",
             "document_blocks": [{"kind": "paragraph", "text": "x"}
                                 ] * 501},
            # serialized text mismatch
            {"message_sha256": digest, "document_text": doc,
             "document_blocks": good},
            {"message_sha256": digest, "document_text": doc,
             "document_blocks": [{"kind": "paragraph",
                                  "text": "different"}]},
        ]
        for body in cases:
            with self.subTest(body=str(body)[:80]):
                with self.assertRaises(ValueError):
                    service.save_feedback_edit(sid, body)

    def test_blocks_formatting_changes_hash_and_plain_clears(self):
        service, bank, sid, token, delivery = self._session()
        doc = "Summary heading\n\nParagraph text."
        first = service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "document_text": doc,
            "document_blocks": [
                {"kind": "heading", "text": "Summary heading"},
                {"kind": "paragraph", "text": "Paragraph text."}]})
        second = service.save_feedback_edit(sid, {
            "message_sha256": first["preview_sha256"],
            "document_text": doc,
            "document_blocks": [
                {"kind": "paragraph", "text": "Summary heading"},
                {"kind": "paragraph", "text": "Paragraph text."}]})
        self.assertNotEqual(first["preview_sha256"],
                            second["preview_sha256"])
        # A plain document_text save (old client) drops the blocks.
        third = service.save_feedback_edit(sid, {
            "message_sha256": second["preview_sha256"],
            "document_text": "Plain text only."})
        preview = (service.teacher_session(sid)["feedback_delivery"]
                   ["preview"])
        self.assertEqual(preview["document_text"], "Plain text only.")
        self.assertNotIn("document_blocks", preview)
        # A legacy structured save clears blocks and document.
        sections = {s["kind"]: s["text"]
                    for s in preview["sections"]}
        service.save_feedback_edit(sid, {
            "message_sha256": third["preview_sha256"],
            "sections": sections})
        preview = (service.teacher_session(sid)["feedback_delivery"]
                   ["preview"])
        self.assertNotIn("document_text", preview)
        self.assertNotIn("document_blocks", preview)

    def test_deterministic_preview_excludes_document(self):
        service, bank, sid, token, delivery = self._session()
        service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "document_text": DOC})
        det = service._deterministic_preview(service._load_meta(sid))
        self.assertIsNotNone(det)
        self.assertNotIn("document_text", det)
        self.assertNotIn("document_edited", det)


if __name__ == "__main__":
    unittest.main()
