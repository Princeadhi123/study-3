"""Synthetic software acceptance checks; no predictive or learning evaluation."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import phase3_paths
import paths
from active_payload import active_payload
from demo_service import DemoService
from evidence_feedback import canonical_digest
from feedback_service import validate_assessment_taxonomy
from future_kt import predict_future_candidates
from live_diagnostics import LiveDiagnostics
from mcq_service import MCQSessionService
from research_runtime import (
    AUGMENTED_EMBEDDINGS, RESEARCH_STATUS, load_current_practice_pool,
    load_research_bank, require_augmented_embeddings, validate_current_practice_pool)
from session_store import SessionStore
from shadow_practice import ShadowPracticeRecommender
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_demo_service import wait_for
from tests.test_kt_adapter import FakeKT


@unittest.skipUnless(
    (phase3_paths.ARTIFACTS / "bounded_content_graph_v3_20261007/manifest.json").exists(),
    "private v3 content capture not installed")
class ResearchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.banks = {mode: load_research_bank(mode)[0] for mode in ("warm", "cold")}
        self.demo = make_bank()
        self.pool = load_current_practice_pool()
        source = copy.deepcopy(self.demo)
        for bank in self.banks.values():
            source["skill_names"].update(bank["skill_names"])
            source["questions"].extend(bank["questions"])
        source["questions"].extend(self.pool["questions"])
        self.kt = FakeKT(source)
        cold_ids = {q["item_id"] for q in self.banks["cold"]["questions"]}
        self.kt.item_vocab = {key: value for key, value in self.kt.item_vocab.items()
                              if key not in cold_ids and not key.startswith("synthetic_")}
        self.kt.config = {"variant": "skill_item_content_option", "max_seq_len": 50, "seed": 42}

    def service(self, **kwargs):
        service = DemoService(
            root=Path(self.tmp.name), bank=self.demo, taxonomy=make_taxonomy(self.demo),
            diagnostics=LiveDiagnostics(model_loader=lambda: self.kt), **kwargs)
        self.addCleanup(service.close, wait=True)
        return service

    def assert_public(self, snapshot):
        blob = json.dumps(snapshot)
        for marker in ("question_id", "item_id", "exercise_id", "content_text",
                       "answer_index", "bank_sha256", "source_path", "concept_id",
                       "possible_error", "support_link", "p_correct", "conformal"):
            self.assertNotIn(marker, blob)
        for bank in self.banks.values():
            for question in bank["questions"]:
                self.assertNotIn(question["question_id"], blob)
                self.assertNotIn(question["item_id"], blob)

    def finish_mixed(self, service, created, bank, start=0):
        sid, token = created["session_id"], created["student_token"]
        snap = service.snapshot(sid, token)
        for index in range(start, 40):
            self.assert_public(snap)
            question = bank["questions"][index]
            chosen = (question["answer_index"] if index % 2 == 0
                      else (question["answer_index"] + 1) % len(question["options"]))
            snap = service.submit_student_response(sid, token, {
                "question_token": snap["current_question"]["question_token"],
                "selected_index": chosen})
        self.assert_public(snap)
        self.assertEqual(snap["status"], "complete")
        self.assertEqual(snap["answered_count"], 40)
        self.assertEqual(snap["feedback"]["end"]["total"], {"correct": 20, "out_of": 40})
        self.assertTrue(wait_for(lambda: service._load_meta(sid)["provider_job"]["status"]
                                 in ("ready", "fallback")))
        self.assertTrue(wait_for(lambda: service._load_meta(sid)["checkpoints"]["end"]
                                 ["diagnostics_job"]["status"] == "ready"))
        return service.teacher_session(sid)

    def test_interleaved_bank_sessions_resume_and_complete_with_private_context(self):
        service = self.service()
        created = {mode: service.create_session(bank_mode=mode)
                   for mode in ("demo", "warm", "cold")}
        for mode in ("warm", "cold"):
            session = created[mode]
            self.assert_public(session["snapshot"])
            service.submit_student_response(session["session_id"], session["student_token"], {
                "question_token": session["snapshot"]["current_question"]["question_token"],
                "selected_index": self.banks[mode]["questions"][0]["answer_index"]})
        service.close(wait=True)
        service = self.service()
        views = {}
        for mode in ("warm", "cold"):
            views[mode] = self.finish_mixed(service, created[mode], self.banks[mode], start=1)
            view = views[mode]
            _, taxonomy, provenance = load_research_bank(mode)
            self.assertEqual(view["bank_mode"], mode)
            self.assertEqual(view["provenance"]["source_sha256"], provenance["source_sha256"])
            self.assertEqual(view["provenance"]["taxonomy_sha256"], canonical_digest(taxonomy))
            self.assertEqual(view["provenance"]["review_status"], RESEARCH_STATUS)
            self.assertFalse(view["provenance"]["educator_approved"])
            self.assertEqual(view["provider_job"]["status"], "ready")
            graph = view["checkpoints"]["end"]["graph"]
            self.assertIn("pending_educator_review", graph["scope"])
            context = view["content_context"]
            self.assertEqual(context["graph_schema"], "phase3_bounded_content_graph_v3")
            self.assertEqual(context["pool_source"], "practice_pool_v2_private.json")
            self.assertEqual(sum(e["role"] == "practice" for e in context["exercises"]), 24)
            self.assertEqual(sum(e["role"] == "assessment" for e in context["exercises"]), 40)
            self.assertFalse(context["approves_learner_delivery"])
            self.assertNotIn("conformal", json.dumps(view["checkpoints"]))
        self.assertNotEqual(views["warm"]["provenance"]["bank_sha256"],
                            views["cold"]["provenance"]["bank_sha256"])
        self.assertNotEqual(views["warm"]["provenance"]["taxonomy_sha256"],
                            views["cold"]["provenance"]["taxonomy_sha256"])
        cold_kt = views["cold"]["checkpoints"]["end"]["diagnostics"]["kt"]
        self.assertEqual(cold_kt["coverage"]["unknown_item_count"],
                         len({q["item_id"] for q in self.banks["cold"]["questions"]}))
        self.assertEqual(len(cold_kt["items"]), 40)
        self.assertEqual({item["regime"] for item in cold_kt["items"]}, {"cold"})
        self.assertTrue((self.kt.batch["item"] == 1).all())
        demo = created["demo"]
        self.assertEqual(service.snapshot(demo["session_id"], demo["student_token"])["answered_count"], 0)
        self.assertEqual({s["bank_mode"] for s in service.list_sessions()}, {"demo", "warm", "cold"})

    def test_research_taxonomies_are_exact_bank_bindings_not_approval(self):
        for mode in self.banks:
            bank, taxonomy, _ = load_research_bank(mode)
            ids = [qid for topic in taxonomy["topics"] for sub in topic["subtopics"]
                   for qid in sub["question_ids"]]
            self.assertEqual(len(ids), 40)
            self.assertEqual(set(ids), {q["question_id"] for q in bank["questions"]})
            validate_assessment_taxonomy(bank, taxonomy)
            with self.assertRaises(ValueError):
                MCQSessionService(bank, SessionStore(Path(self.tmp.name)), taxonomy=taxonomy)
            for mutation in ("missing", "duplicate"):
                bad = copy.deepcopy(taxonomy)
                qids = bad["topics"][0]["subtopics"][0]["question_ids"]
                qids.pop() if mutation == "missing" else qids.append(qids[0])
                with self.assertRaises(ValueError):
                    validate_assessment_taxonomy(bank, bad)
            other = "cold" if mode == "warm" else "warm"
            with self.assertRaises(ValueError):
                validate_assessment_taxonomy(self.banks[other], taxonomy)
            self.assertEqual(bank["review_status"], RESEARCH_STATUS)

    def test_fraction_simulation_uses_research_topic_without_mutating_source(self):
        service = self.service()
        original = copy.deepcopy(self.banks["warm"])
        created = service.simulate("weak_fractions_only", 1, bank_mode="warm")
        self.assertEqual(created["snapshot"]["feedback"]["end"]["total"],
                         {"correct": 30, "out_of": 40})
        self.assertEqual(service._bank_context("warm")["bank"], original)

    def test_opaque_tokens_enforce_session_order_and_retry(self):
        service = self.service()
        a = service.create_session(bank_mode="warm")
        b = service.create_session(bank_mode="warm")
        sid, token = a["session_id"], a["student_token"]
        row = {"question_token": a["snapshot"]["current_question"]["question_token"], "selected_index": 0}
        with self.assertRaises(ValueError):
            service.submit_student_response(b["session_id"], b["student_token"], row)
        with self.assertRaises(ValueError):
            service.submit_student_response(sid, token, dict(
                row, question_token=service._question_token(service._load_meta(sid), 3)))
        first = service.submit_student_response(sid, token, row)
        self.assertEqual(first, service.submit_student_response(sid, token, row))
        with self.assertRaises(ValueError):
            service.submit_student_response(sid, token, dict(row, selected_index=1))
        for bad in (dict(row, selected_index=True), dict(row, question_token="é")):
            with self.assertRaises(ValueError):
                service.submit_student_response(sid, token, bad)

    def test_tampered_session_binding_rejected(self):
        service = self.service()
        created = service.create_session(bank_mode="cold")
        sid, token = created["session_id"], created["student_token"]
        meta = service._load_meta(sid)
        for key, value in (("bank_mode", "warm"), ("bank_sha256", "0"*64),
                           ("taxonomy_sha256", "0"*64),
                           ("bank_provenance", {})):
            changed = copy.deepcopy(meta)
            changed[key] = value
            service._save_meta(changed)
            with self.assertRaises(ValueError):
                service.snapshot(sid, token)
        service._save_meta(meta)
        record = service.store.load(sid)
        record["bank_mode"] = "warm"
        service.store.save(record)
        with self.assertRaises(ValueError):
            service.snapshot(sid, token)

    def test_current_pool_guard_and_synthetic_unknown_encoding(self):
        old = json.loads((phase3_paths.ARTIFACTS / "shadow_smoke_20261006" /
                          "practice_pool_private.json").read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            validate_current_practice_pool(old)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "KT_PHASE2_TEXT_EMBEDDINGS"):
                self.service(practice_pool=self.pool, practice_target_band=[0.5, 1.0])
            with self.assertRaisesRegex(ValueError, "KT_PHASE2_TEXT_EMBEDDINGS"):
                ShadowPracticeRecommender(self.pool, [0.5, 1.0], lambda *args: None)
        with patch.dict(os.environ, {"KT_PHASE2_TEXT_EMBEDDINGS": str(paths.TEXT_EMBEDDINGS)}):
            if paths.TEXT_EMBEDDINGS != AUGMENTED_EMBEDDINGS:
                with self.assertRaises(ValueError):
                    require_augmented_embeddings()
        with patch.dict(os.environ, {"KT_PHASE2_TEXT_EMBEDDINGS": str(AUGMENTED_EMBEDDINGS)}), \
                patch.object(paths, "TEXT_EMBEDDINGS", AUGMENTED_EMBEDDINGS):
            require_augmented_embeddings()
            synthetic = [q for q in self.pool["questions"]
                         if q["item_id"].startswith("synthetic_divisibility_")]
            result = predict_future_candidates(
                self.banks["warm"], responses(self.banks["warm"]), synthetic, self.kt)
            self.assertEqual(len(result["predictions"]), 4)
            self.assertEqual({r["regime"] for r in result["predictions"]}, {"cold"})
            self.assertEqual(int(self.kt.batch["item"][0, -1]), 1)
            self.kt.item_vocab[synthetic[0]["item_id"]] = 999
            with self.assertRaisesRegex(ValueError, "UNK"):
                predict_future_candidates(self.banks["warm"],
                    responses(self.banks["warm"]), synthetic, self.kt)

    def test_current_pool_live_recommendation_is_teacher_only(self):
        with patch.dict(os.environ, {"KT_PHASE2_TEXT_EMBEDDINGS": str(AUGMENTED_EMBEDDINGS)}), \
                patch.object(paths, "TEXT_EMBEDDINGS", AUGMENTED_EMBEDDINGS):
            service = self.service(practice_pool=self.pool, practice_target_band=[0.5, 1.0])
            created = service.create_session(bank_mode="warm")
            self.finish_mixed(service, created, self.banks["warm"])
            sid = created["session_id"]
            self.assertTrue(wait_for(lambda: service._load_meta(sid)["checkpoints"]["end"]
                                     ["recommendation_job"]["status"] != "pending"))
            result = service.teacher_session(sid)["checkpoints"]["end"]["recommendation"]
            self.assertEqual(result["status"], "selected")
            self.assertEqual(result["pool_sha256"], canonical_digest(self.pool))
            self.assertIn(result["selected_question_id"], {q["question_id"] for q in self.pool["questions"]})
            self.assertFalse(result["used_for_student_advice"])
            self.assertFalse(result["used_for_feedback"])
            self.assertNotIn("conformal", json.dumps(result))

    def test_active_projection_does_not_mutate_archived_output(self):
        saved = {"checkpoints": [{"conformal": {"skills": [1]}, "kt": {"items": []}}],
                 "diagnostic_provenance": {"midpoint_calibration": "private", "checkpoint": "model"}}
        original = copy.deepcopy(saved)
        projected = active_payload(saved)
        self.assertNotIn("conformal", json.dumps(projected))
        self.assertNotIn("calibration", json.dumps(projected))
        self.assertEqual(saved, original)
        js = (phase3_paths.PHASE3_ROOT / "web_demo/app.js").read_text(encoding="utf-8")
        self.assertNotIn("conformal", js.lower())
        self.assertNotRegex(js, r"__p\d+__q[a-f0-9]+")


if __name__ == "__main__":
    unittest.main()
