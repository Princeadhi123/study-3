"""Tests for the feedback_variant workflow and deterministic-only hosted wiring."""
import json
import secrets
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import phase3_paths  # noqa: F401
from aitta_generator import AittaGenerator
from demo_api import make_server
from demo_service import DemoService
from full_feedback import CachedFullFeedbackGenerator
from research_workspace import ResearchConflict
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_assessment_worked_review import (
    grounded_rows, make_grounded_bank, make_grounded_taxonomy, positions)
from tests.test_demo_service import fake_diagnostics, wait_for
from tests.test_scenario_replays import replay_fixture


WORKED_FIRST_VERSION = "worked_first_full_feedback_v1"
WORKED_FIRST_TEMPLATE = "phase3_student_feedback_worked_first_full_v1"
FULL_NARRATIVE_GUARDRAILS = "provider_full_narrative_shape_checked_v2"
STANDARD_TITLES = {
    "assessment_summary": "Assessment summary",
    "observed_strengths": "Observed strengths",
    "review_focus": "Review focus",
    "next_steps": "Next steps"}
WORKED_SECTIONS = {
    "assessment_summary": "This assessment is now complete; your teacher will read it with you.",
    "observed_strengths": "Your correct answers show work you can build on together.",
    "review_focus": "A possible review starting point is the assessed topic your teacher selects.",
    "next_steps": "If your teacher approves, revisit the checked answers and explain each step."}


class FullStubGenerator:
    supports_full_feedback = True
    thread_safe = True

    def __init__(self, sections=None, error=None):
        self.sections = dict(sections or WORKED_SECTIONS)
        self.error = error
        self.execution = None
        self.calls = 0
        self.payloads = []

    def generate(self, payload):
        self.calls += 1
        self.payloads.append(payload)
        self.execution = {"status": "fake_completed"}
        if self.error is not None:
            raise self.error
        reply = {"candidate_id":
                 payload["selected_candidate"]["candidate_id"],
                 "sections": dict(self.sections)}
        # Grounded replies (worked-first schema or a non-null
        # assessment context) must carry one worked review per
        # projected provider example, even when the local solver
        # marked a card unavailable.
        from full_feedback import worked_review_examples_for_reply
        expected = worked_review_examples_for_reply(payload)
        if expected is not None:
            reply["worked_reviews"] = [
                {"question_id": e["question_id"],
                 "steps": ["Fixture authored step."],
                 "reasoning_question":
                     "Why does the fixture step work?"}
                for e in expected]
        return reply


class HostedFactoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)

    def test_hosted_factory_initializes_only_aitta(self):
        inner = AittaGenerator("testkey-testkey", "https://aitta.invalid",
                               model="test-model", timeout=5.0)
        with mock.patch.object(AittaGenerator, "from_env",
                               return_value=inner) as from_env:
            service = DemoService(
                root=Path(self.tmp.name), bank=self.bank,
                taxonomy=self.taxonomy,
                diagnostics=fake_diagnostics(self.bank),
                provider_mode="hosted", call_budget=3,
                jev_env_file="does/not/exist.env")
        self.addCleanup(service.close, wait=True)
        from_env.assert_called_once_with()
        self.assertIsNone(service.selector)
        self.assertIsInstance(service.generator,
                              CachedFullFeedbackGenerator)
        self.assertIs(service.generator.native._generator, inner)
        self.assertEqual(inner._timeout, 120.0)
        self.assertEqual(service.cache.budget, 3)
        self.assertTrue((Path(self.tmp.name) / "provider_captures").is_dir())


class FeedbackVariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)

    def _service(self, root=None, **kwargs):
        bank = kwargs.pop("bank", self.bank)
        kwargs.setdefault("diagnostics", fake_diagnostics(bank))
        service = DemoService(
            root=root or Path(self.tmp.name) / "runtime",
            bank=bank,
            taxonomy=kwargs.pop("taxonomy", self.taxonomy), **kwargs)
        self.addCleanup(service.close, wait=True)
        return service

    def _rows_wrong(self, skills=("sA", "sB", "sC")):
        rows = []
        for q in self.bank["questions"]:
            index = q["answer_index"]
            if q["skill_id"] in skills:
                index = (index + 1) % len(q["options"])
            rows.append({"question_id": q["question_id"],
                         "selected_index": index})
        return rows

    def _complete(self, service, rows=None, **kwargs):
        created = service.create_session(**kwargs)
        sid, token = created["session_id"], created["student_token"]
        for row in (rows if rows is not None else self._rows_wrong()):
            service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        return sid, token

    def test_variant_validation_before_side_effects(self):
        service = self._service()
        meta_dir = service.meta_dir
        for bad in ("bogus", ["question_first"], True, 3, None):
            with self.assertRaises(ValueError):
                service.create_session(feedback_variant=bad)
        self.assertEqual(list(meta_dir.glob("*.json")), [])
        legacy = self._service(root=Path(self.tmp.name) / "legacy",
                               review_mode="legacy_single")
        with self.assertRaises(ValueError):
            legacy.create_session(feedback_variant="worked_first")
        self.assertEqual(list(legacy.meta_dir.glob("*.json")), [])
        legacy.create_session(feedback_variant="question_first")
        legacy.create_session()

    def test_worked_first_full_narrative_end_to_end(self):
        generator = FullStubGenerator()
        service = self._service(provider_mode="hosted",
                                generator=generator)
        sid, token = self._complete(
            service, feedback_variant="worked_first")
        self.assertEqual(generator.calls, 1)
        meta = service._load_meta(sid)
        self.assertEqual(meta["feedback_variant"], "worked_first")
        self.assertEqual(meta["review_presentation_version"],
                         WORKED_FIRST_VERSION)
        review = meta["checkpoints"]["end"]["student_review"]
        trace = review["trace"]
        self.assertEqual(trace["guardrails_version"],
                         FULL_NARRATIVE_GUARDRAILS)
        self.assertEqual(
            trace["validation"],
            "provider_full_narrative_shape_checks_only_requires_"
            "educator_review")
        self.assertEqual(trace["selection_source"], "rules")
        self.assertTrue(all(
            source == "provider"
            for source in review["message"]["section_sources"].values()))
        sections = {s["kind"]: s for s in review["message"]["sections"]}
        self.assertEqual(set(sections), set(STANDARD_TITLES))
        for kind, section in sections.items():
            self.assertEqual(section["title"], STANDARD_TITLES[kind])
        from evidence_feedback_policy import SCOPE
        self.assertTrue(sections["review_focus"]["text"].endswith(SCOPE))
        self.assertEqual(
            sections["review_focus"]["text"].count("draft, not a diagnosis"),
            1)
        for kind, text in WORKED_SECTIONS.items():
            needle = text
            if kind == "review_focus":
                self.assertTrue(
                    sections[kind]["text"].startswith(needle))
            else:
                self.assertEqual(sections[kind]["text"], needle)
        self.assertNotIn("separate practice activity",
                         sections["next_steps"]["text"])

        detail = service.teacher_session(sid)
        self.assertEqual(detail["feedback_variant"], "worked_first")
        self.assertEqual(detail["provenance"]["feedback_variant"],
                         "worked_first")
        preview = detail["feedback_delivery"]["preview"]
        self.assertEqual(preview["template_version"], WORKED_FIRST_TEMPLATE)
        self.assertEqual(preview["review_presentation_version"],
                         WORKED_FIRST_VERSION)
        self.assertEqual(preview["feedback_variant"], "worked_first")
        self.assertEqual(
            {s["kind"]: s["title"] for s in preview["sections"]},
            STANDARD_TITLES)
        self.assertEqual(preview["total"], {"correct": 10, "out_of": 40})
        # Question-first scaffolds are absent; checked cards intact.
        for example in preview["review_examples"]:
            self.assertNotEqual(example.get("presentation_version"),
                                "question_first_worked_review_v1")
            self.assertIsNone(example.get("socratic_prompt"))
            self.assertIn("correct_answer", example)
        released = service.release_feedback(
            sid, {"message_sha256":
                  detail["feedback_delivery"]["preview_sha256"]})
        self.assertTrue(released["released"])
        snap = service.snapshot(sid, token)
        self.assertEqual(
            snap["feedback"]["end"]["sections"], preview["sections"])

        # Same answers through question-first keep the summary-only shape.
        qgen = FullStubGenerator()
        qservice = self._service(root=Path(self.tmp.name) / "qf",
                                 provider_mode="hosted",
                                 generator=qgen)
        qsid, _ = self._complete(qservice)
        qmeta = qservice._load_meta(qsid)
        self.assertEqual(qmeta["feedback_variant"], "question_first")
        qreview = qmeta["checkpoints"]["end"]["student_review"]
        self.assertEqual(qreview["trace"]["guardrails_version"],
                         "application_owned_multi_feedback_v1")
        self.assertEqual(
            qreview["message"]["section_sources"],
            {"assessment_summary": "provider",
             "observed_strengths": "application",
             "review_focus": "application",
             "next_steps": "application"})
        self.assertEqual(
            qreview["selected_candidate_id"],
            review["selected_candidate_id"])
        self.assertEqual(
            qreview["sanitized_evidence"]["total"],
            review["sanitized_evidence"]["total"])
        qdetail = qservice.teacher_session(qsid)
        qpreview = qdetail["feedback_delivery"]["preview"]
        self.assertEqual(qpreview["template_version"],
                         "phase3_student_feedback_template_v9")
        self.assertEqual(qpreview["review_presentation_version"],
                         "question_first_worked_review_v1")

    def test_worked_first_invalid_reply_falls_back(self):
        raw = "<script>alert(1)</script>"
        generator = FullStubGenerator(
            sections={"assessment_summary": raw,
                      "observed_strengths": "ok",
                      "review_focus": "ok",
                      "next_steps": "ok"})
        service = self._service(provider_mode="hosted",
                                generator=generator)
        sid, _ = self._complete(
            service, feedback_variant="worked_first")
        review = service._load_meta(
            sid)["checkpoints"]["end"]["student_review"]
        self.assertEqual(review["trace"]["fallback_reason"],
                         "invalid_generation")
        self.assertEqual(review["trace"]["guardrails_version"],
                         "application_owned_multi_feedback_v1")
        self.assertTrue(all(
            source == "application"
            for source in review["message"]["section_sources"].values()))
        self.assertNotIn(raw, json.dumps(review["message"]))

    def test_config_execution_label_tracks_selector(self):
        service = self._service()
        self.assertEqual(
            service.teacher_config()["review_selection_execution"],
            "deterministic_rules")
        seam = self._service(root=Path(self.tmp.name) / "seam",
                             selector=object())
        self.assertEqual(
            seam.teacher_config()["review_selection_execution"],
            "injected_selector_test_seam")

    def test_historical_session_provenance_and_preview(self):
        service = self._service()
        sid, token = self._complete(service)
        detail_now = service.teacher_session(sid)
        self.assertEqual(
            detail_now["provenance"]["review_selection_execution"],
            "rules")
        # Simulate a pre-variant record: no feedback_variant key and a
        # recorded injected-selector selection.
        meta = service._load_meta(sid)
        del meta["feedback_variant"]
        meta["checkpoints"]["end"]["student_review"]["trace"][
            "selection_source"] = "injected_selector"
        service._save_meta(meta)
        detail = service.teacher_session(sid)
        self.assertIsNone(detail["feedback_variant"])
        self.assertEqual(
            detail["provenance"]["review_selection_execution"],
            "injected_selector")
        preview = detail["feedback_delivery"]["preview"]
        self.assertNotIn("feedback_variant", preview)
        self.assertEqual(preview["review_presentation_version"],
                         "question_first_worked_review_v1")
        # Saved edit + release still work on the historical session.
        edited = service.save_feedback_edit(sid, {
            "message_sha256":
                detail["feedback_delivery"]["preview_sha256"],
            "sections": {s["kind"]: s["text"]
                         for s in preview["sections"]
                         if s["kind"] in STANDARD_TITLES},
            "reviewer_label": "historical check"})
        self.assertTrue(edited["saved"])
        released = service.release_feedback(sid, {
            "message_sha256":
                edited["preview_sha256"]})
        self.assertTrue(released["released"])

    def test_grounded_cards_match_across_variants(self):
        bank = make_grounded_bank()
        taxonomy = make_grounded_taxonomy(bank)
        wrong = sorted({positions(bank, "pct", 0)[0],
                        positions(bank, "alg", 0)[0]})
        rows = grounded_rows(bank, wrong)
        results = {}
        for variant in ("worked_first", "question_first"):
            generator = FullStubGenerator()
            service = self._service(
                root=Path(self.tmp.name) / variant, bank=bank,
                taxonomy=taxonomy,
                diagnostics=fake_diagnostics(bank),
                provider_mode="hosted", generator=generator)
            sid, _ = self._complete(
                service, rows=rows, feedback_variant=variant)
            detail = service.teacher_session(sid)
            preview = detail["feedback_delivery"]["preview"]
            cards = preview["review_examples"]
            self.assertTrue(cards)
            self.assertTrue(
                all(c["status"] == "ready" for c in cards))
            self.assertTrue(all(c["steps"] for c in cards))
            review = detail["checkpoints"]["end"]["student_review"]
            results[variant] = {
                "cards": cards, "preview": preview, "review": review,
                "evidence_total": review["sanitized_evidence"]["total"],
                "selected": review["selected_candidate_ids"]}
        self.assertEqual(results["worked_first"]["evidence_total"],
                         results["question_first"]["evidence_total"])
        self.assertEqual(results["worked_first"]["selected"],
                         results["question_first"]["selected"])
        self.assertEqual(
            [c["steps"] for c in results["worked_first"]["cards"]],
            [c["steps"] for c in results["question_first"]["cards"]])
        self.assertEqual(
            [c["correct_answer"] for c in results["worked_first"]["cards"]],
            [c["correct_answer"] for c in
             results["question_first"]["cards"]])
        w_sections = {s["kind"]: s["text"] for s in
                      results["worked_first"]["preview"]["sections"]}
        for kind, text in WORKED_SECTIONS.items():
            if kind == "review_focus":
                self.assertTrue(w_sections[kind].startswith(text))
            else:
                self.assertEqual(w_sections[kind], text)

    def test_api_export_includes_variant_and_narrative(self):
        service = self._service(provider_mode="hosted",
                                generator=FullStubGenerator())
        sid, _ = self._complete(
            service, feedback_variant="worked_first")
        server = make_server(service, "246810")
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        request = urllib.request.Request(
            f"{url}/api/teacher/unlock",
            data=json.dumps({"pin": "246810"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            cookie = response.headers["Set-Cookie"].split(";")[0]
        request = urllib.request.Request(
            f"{url}/api/teacher/sessions/{sid}/export",
            headers={"Cookie": cookie})
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
            self.assertIn("demo_", response.headers[
                "Content-Disposition"])
        self.assertEqual(payload["feedback_variant"], "worked_first")
        texts = {s["kind"]: s["text"] for s in
                 payload["feedback_delivery"]["preview"]["sections"]}
        for kind, text in WORKED_SECTIONS.items():
            if kind == "review_focus":
                self.assertTrue(texts[kind].startswith(text))
            else:
                self.assertEqual(texts[kind], text)

    def test_worked_first_context_authoring_and_teacher_only_scope(self):
        # The supplied narrative is intentionally a qualitative claim the
        # shape guardrails cannot fact-check; the educator-review warning
        # must remain — format acceptance is not hallucination detection.
        unsupported = {
            "assessment_summary":
                "This assessment is complete; the written work invites "
                "a calm conversation with your teacher.",
            "observed_strengths":
                "Your careful effort throughout this assessment is "
                "evident to your teacher.",
            "review_focus":
                "A gentle place to begin review is the assessed topic "
                "your teacher selects.",
            "next_steps":
                "If your teacher approves, revisit the checked answers "
                "and explain each step aloud."}
        generator = FullStubGenerator(sections=unsupported)
        service = self._service(provider_mode="hosted",
                                generator=generator)
        sid, token = self._complete(
            service, feedback_variant="worked_first")
        self.assertEqual(generator.calls, 1)
        wf = generator.payloads[0]["worked_first_context"]
        self.assertEqual(
            wf["version"],
            "worked_first_assessment_research_context_v2")
        self.assertEqual(len(wf["assessment_records"]), 40)
        self.assertEqual(
            wf["model_context"]["checkpoints"]["end"]["status"],
            "available")
        self.assertEqual(
            wf["concept_graph"]["relation"], "is_part_of_not_prerequisite")
        self.assertEqual(
            generator.payloads[0]["prompt_version"],
            "aitta_worked_first_evidence_author_v10")
        self.assertEqual(
            generator.payloads[0]["schema"],
            "phase3_worked_first_feedback_generation_v1")

        meta = service._load_meta(sid)
        review = meta["checkpoints"]["end"]["student_review"]
        self.assertEqual(
            review["trace"]["generation_context_version"],
            "worked_first_assessment_research_context_v2")
        self.assertEqual(
            review["provider_context"]["version"], wf["version"])
        self.assertEqual(
            review["trace"]["validation"],
            "provider_full_narrative_shape_checks_only_requires_"
            "educator_review")
        # Deterministic baseline stays an independent local reference.
        baseline_text = "\n".join(
            s["text"] for s in review["template_baseline"]["sections"])
        provider_text = "\n".join(unsupported.values())
        self.assertNotEqual(baseline_text, provider_text)

        detail = service.teacher_session(sid)
        preview = detail["feedback_delivery"]["preview"]
        texts = {s["kind"]: s["text"] for s in preview["sections"]}
        for kind, text in unsupported.items():
            self.assertTrue(texts[kind].startswith(text))
        service.release_feedback(
            sid, {"message_sha256":
                  detail["feedback_delivery"]["preview_sha256"]})
        snap = service.snapshot(sid, token)
        snap_text = json.dumps(snap)
        self.assertNotIn("provider_context", snap_text)
        self.assertNotIn("assessment_records", snap_text)
        self.assertIn(unsupported["observed_strengths"],
                      json.dumps(snap["feedback"]["end"]))
        self.assertTrue(wait_for(
            lambda: service._jobs_complete(sid)))


class ReplayVariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.source_path, self.report_path, self.source = replay_fixture(
            self.root, self.bank, self.taxonomy)
        self.service = DemoService(
            root=self.root / "runtime", bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            replay_report=self.report_path,
            replay_source=self.source_path)
        self.addCleanup(self.service.close, wait=True)
        self.library = self.service.research.library()

    def request(self, count=1, **extra):
        body = {"request_id": secrets.token_hex(16),
                "report_sha256": self.library["report_sha256"],
                "scenario_ids": [s["id"] for s in
                                 self.library["scenarios"][:count]],
                "provider_mode": "rules",
                "allow_provider_calls": False}
        body.update(extra)
        return body

    def wait(self, run_id, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            view = self.service.replays.get(run_id)
            if view["status"] not in ("queued", "running", "cancelling"):
                return view
            time.sleep(.03)
        self.fail("replay did not finish")

    def test_replay_variant_validation(self):
        for bad in ({"feedback_variant": "bogus"},
                    {"feedback_variant": ["worked_first"]},
                    {"feedback_variant": True},
                    {"feedback_variant": 3}):
            body = self.request(**bad)
            with self.assertRaises(ValueError):
                self.service.replays.start(body)
        legacy = DemoService(
            root=self.root / "legacy", bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            replay_report=self.report_path,
            replay_source=self.source_path,
            review_mode="legacy_single")
        self.addCleanup(legacy.close, wait=True)
        legacy.research.library()
        worked_first_request = self.request(feedback_variant="worked_first")
        with self.assertRaises(ValueError):
            legacy.replays.start(worked_first_request)

    def test_both_variants_saved_independently(self):
        before = (self.report_path.read_bytes(),
                  self.source_path.read_bytes())
        body_q = self.request(feedback_variant="question_first")
        run_q = self.wait(self.service.replays.start(body_q)["id"])
        self.assertEqual(run_q["feedback_variant"], "question_first")
        body_w = self.request(feedback_variant="worked_first")
        run_w = self.wait(self.service.replays.start(body_w)["id"])
        self.assertEqual(run_w["feedback_variant"], "worked_first")
        case_q = run_q["cases"][0]
        case_w = run_w["cases"][0]
        self.assertNotEqual(case_q["session_id"], case_w["session_id"])
        detail_q = self.service.teacher_session(case_q["session_id"])
        detail_w = self.service.teacher_session(case_w["session_id"])
        self.assertEqual(detail_q["feedback_variant"], "question_first")
        self.assertEqual(detail_w["feedback_variant"], "worked_first")
        self.assertEqual(detail_w["replay"]["feedback_variant"],
                         "worked_first")
        self.assertEqual(
            [r["selected_index"] for r in detail_q["question_responses"]],
            [r["selected_index"] for r in detail_w["question_responses"]])
        self.assertEqual(
            detail_q["checkpoints"]["end"]["student_review"]
            ["selected_candidate_id"],
            detail_w["checkpoints"]["end"]["student_review"]
            ["selected_candidate_id"])
        # Idempotent same body; conflict same id different variant.
        self.assertEqual(
            self.service.replays.start(body_q)["id"], body_q["request_id"])
        conflict = dict(body_w, request_id=body_q["request_id"])
        with self.assertRaises(ResearchConflict):
            self.service.replays.start(conflict)
        # Source files unchanged.
        self.assertEqual(before,
                         (self.report_path.read_bytes(),
                          self.source_path.read_bytes()))

    def test_missing_variant_defaults_and_history_view(self):
        run = self.wait(self.service.replays.start(self.request())["id"])
        self.assertEqual(run["feedback_variant"], "question_first")
        detail = self.service.teacher_session(
            run["cases"][0]["session_id"])
        self.assertEqual(detail["feedback_variant"], "question_first")


if __name__ == "__main__":
    unittest.main()
