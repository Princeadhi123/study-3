"""Application wiring tests for the optional shadow practice branch.

Ordinary service/CLI integration coverage only - fakes replace the model
and recommender; no model weights, hosted calls, or research evaluation.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import phase3_paths  # noqa: F401 -- installs the Phase 2 import path
import demo_api
from demo_service import DemoService
from shadow_practice import ShadowPracticeRecommender
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_demo_service import fake_diagnostics, wait_for


def make_pool(skills=("sA", "sB")):
    questions = []
    for index, skill in enumerate(skills, 1):
        text = f"Pool practice prompt {index}?"
        options = ["one", "two", "three"]
        questions.append({
            "question_id": f"pool_q{index}",
            "skill_id": skill,
            "item_id": f"pool-item-{index}",
            "text": text,
            "options": options,
            "content_text": text + " [OPTIONS] " + " | ".join(options),
        })
    return {"schema": "phase3_shadow_practice_pool_v1",
            "scope": "research_only_not_learner_approved",
            "questions": questions}


def empty_pool():
    return {"schema": "phase3_shadow_practice_pool_v1",
            "scope": "research_only_not_learner_approved",
            "questions": []}


def wrong_in(bank, skills):
    rows = []
    for q in bank["questions"]:
        if q["skill_id"] in skills:
            rows.append({"question_id": q["question_id"],
                         "selected_index": (q["answer_index"] + 1)
                         % len(q["options"])})
        else:
            rows.append({"question_id": q["question_id"],
                         "selected_index": q["answer_index"]})
    return rows


class FakeRecommender:
    def __init__(self, results=None, error=None):
        self.calls = []
        self.results = list(results or [])
        self.error = error

    def recommend(self, bank, taxonomy, rows):
        self.calls.append({"bank": bank, "taxonomy": taxonomy,
                           "rows": copy.deepcopy(rows)})
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.results.pop(0))


class RecordingSelector:
    """Hosted-shape selector double: records payloads, no HTTP."""

    def __init__(self):
        self.execution = None
        self.payloads = []

    def select(self, payload):
        self.payloads.append(copy.deepcopy(payload))
        self.execution = {"status": "fake_completed"}
        return {"candidate_id": payload["candidates"][0]["candidate_id"]}


class RecordingGenerator:
    def __init__(self):
        self.execution = None
        self.payloads = []

    def generate(self, payload):
        self.payloads.append(copy.deepcopy(payload))
        self.execution = {"status": "fake_completed"}
        return {"candidate_id":
                payload["selected_candidate"]["candidate_id"],
                "opening": "The draft is ready for review."}


SELECTED_RESULT = {
    "schema": "phase3_shadow_practice_recommendation_v1",
    "status": "selected", "reason": None, "mode": "shadow_only",
    "selected_question_id": "pool_q1", "baseline_question_id": "pool_q1",
    "used_for_student_advice": False, "used_for_feedback": False}

ABSTAINED_RESULT = {
    "schema": "phase3_shadow_practice_recommendation_v1",
    "status": "abstained",
    "reason": "no_candidate_in_explicit_target_band",
    "mode": "shadow_only",
    "selected_question_id": None, "baseline_question_id": "pool_q1",
    "used_for_student_advice": False, "used_for_feedback": False}

FORBIDDEN = ("recommendation", "p_correct", "pool_q")


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)

    def _service(self, root=None, **kwargs):
        kwargs.setdefault("diagnostics", fake_diagnostics(self.bank))
        service = DemoService(root=Path(root or self.tmp.name),
                              bank=self.bank, taxonomy=self.taxonomy,
                              **kwargs)
        self.addCleanup(service.close, wait=True)
        return service

    def _complete(self, service, rows=None):
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in (rows if rows is not None else responses(self.bank)):
            service.submit_response(sid, token, row)
        return sid, token

    def _end(self, service, sid):
        return service.teacher_session(sid)["checkpoints"]["end"]

    def _wait_done(self, service, sid):
        self.assertTrue(wait_for(
            lambda: self._end(service, sid)
            .get("recommendation_job", {}).get("status") != "pending"))

    def _wait_provider(self, service, sid):
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))


class RecommendationWiringTests(ServiceCase):
    def test_default_disabled_no_recommender_invocation(self):
        service = self._service()
        sid, _ = self._complete(service)
        end = self._end(service, sid)
        self.assertIsNone(service.recommender)
        self.assertEqual(end["recommendation_job"]["status"], "disabled")
        self.assertEqual(end["recommendation"]["status"], "disabled")
        self.assertEqual(end["recommendation"]["used_for_student_advice"],
                         False)

    def test_not_called_at_midpoint_called_once_at_end(self):
        fake = FakeRecommender(results=[SELECTED_RESULT])
        service = self._service(recommender=fake)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in responses(self.bank, count=20):
            service.submit_response(sid, token, row)
        self.assertEqual(len(fake.calls), 0)
        for row in responses(self.bank)[20:]:
            service.submit_response(sid, token, row)
        self._wait_done(service, sid)
        self.assertEqual(len(fake.calls), 1)

    def test_recommendation_private_teacher_only(self):
        service = self._service(recommender=FakeRecommender(
            results=[SELECTED_RESULT]))
        sid, token = self._complete(service)
        self._wait_done(service, sid)
        snapshot = service.snapshot(sid, token)
        self.assertNotIn("recommendation", json.dumps(snapshot))
        self.assertNotIn("recommendation",
                         json.dumps(service._load_meta(sid)["provider_job"]))
        recommendation = self._end(service, sid)["recommendation"]
        self.assertEqual(recommendation["status"], "selected")
        self.assertEqual(recommendation["selected_question_id"], "pool_q1")

    def test_provider_payloads_carry_no_recommendation(self):
        selector, generator = RecordingSelector(), RecordingGenerator()
        service = self._service(
            provider_mode="hosted", selector=selector,
            generator=generator,
            recommender=FakeRecommender(results=[SELECTED_RESULT]))
        sid, _ = self._complete(service, rows=wrong_in(self.bank, {"sA", "sB"}))
        self._wait_done(service, sid)
        self._wait_provider(service, sid)
        self.assertTrue(selector.payloads)
        self.assertTrue(generator.payloads)
        wire = json.dumps({"selector": selector.payloads,
                           "generator": generator.payloads})
        for needle in FORBIDDEN:
            self.assertNotIn(needle, wire)

    def test_completed_selected_and_abstained_persist(self):
        fake = FakeRecommender(
            results=[SELECTED_RESULT, ABSTAINED_RESULT])
        service = self._service(recommender=fake)
        first, _ = self._complete(service)
        self._wait_done(service, first)
        second, _ = self._complete(service)
        self._wait_done(service, second)
        for sid, status in ((first, "selected"), (second, "abstained")):
            end = self._end(service, sid)
            self.assertEqual(end["recommendation_job"]["status"], status)
            self.assertEqual(end["recommendation"]["status"], status)
            self.assertEqual(len(fake.calls[0]["rows"]), 40)
            persisted = service._load_meta(sid)["checkpoints"]["end"]
            self.assertEqual(persisted["recommendation"]["status"], status)

    def test_feedback_identical_across_recommendation_outcomes(self):
        fake = FakeRecommender(results=[
            SELECTED_RESULT, ABSTAINED_RESULT], error=None)
        service = self._service(recommender=fake)
        texts = []
        for _ in range(2):
            sid, _ = self._complete(service)
            self._wait_done(service, sid)
            texts.append(self._end(service, sid)
                         ["baseline_student"]["message"]["text"])
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        failing = self._service(root=Path(other.name),
                                recommender=FakeRecommender(
                                    error=RuntimeError("predictor blew up")))
        sid, token = self._complete(failing)
        self._wait_done(failing, sid)
        end = self._end(failing, sid)
        texts.append(end["baseline_student"]["message"]["text"])
        self.assertEqual(end["recommendation"]["status"], "unavailable")
        self.assertEqual(end["recommendation"]["reason"],
                         "future_recommendation_failed")
        self.assertEqual(end["recommendation"]["used_for_feedback"], False)
        self.assertIsNotNone(end["baseline_teacher"])
        self.assertEqual(len(set(texts)), 1)
        self.assertIsNotNone(failing.snapshot(sid, token)["feedback"]["end"])

    def test_forced_privacy_flags_on_injected_result(self):
        leaky = copy.deepcopy(SELECTED_RESULT)
        leaky["used_for_student_advice"] = True
        leaky["used_for_feedback"] = True
        service = self._service(recommender=FakeRecommender(results=[leaky]))
        sid, _ = self._complete(service)
        self._wait_done(service, sid)
        recommendation = self._end(service, sid)["recommendation"]
        self.assertEqual(recommendation["used_for_student_advice"], False)
        self.assertEqual(recommendation["used_for_feedback"], False)

    def test_shared_queue_capacity_skips_recommendation(self):
        fake = FakeRecommender(results=[SELECTED_RESULT])
        service = self._service(recommender=fake, max_pending_jobs=1)
        sid, _ = self._complete(service)
        end = self._end(service, sid)
        self.assertEqual(end["recommendation"]["status"], "unavailable")
        self.assertEqual(end["recommendation"]["reason"],
                         "demo_diagnostics_queue_capacity")
        self.assertEqual(len(fake.calls), 0)

    def test_restart_pending_recommendation_not_retried(self):
        fake = FakeRecommender(results=[SELECTED_RESULT])
        service = self._service(recommender=fake)
        sid, _ = self._complete(service)
        self._wait_done(service, sid)
        meta = service._load_meta(sid)
        end = meta["checkpoints"]["end"]
        end["recommendation_job"] = {"status": "pending"}
        end["recommendation"] = {"status": "pending"}
        service._save_meta(meta)
        service.close()
        restarted = self._service(recommender=fake)
        recovered = restarted._load_meta(sid)["checkpoints"]["end"]
        self.assertEqual(recovered["recommendation_job"]["status"],
                         "unavailable")
        self.assertEqual(recovered["recommendation_job"]["reason"],
                         "interrupted_by_restart")
        self.assertEqual(recovered["recommendation"]["reason"],
                         "interrupted_by_restart")


class FutureKTPoolPathTests(ServiceCase):
    def test_active_service_rejects_arbitrary_pool(self):
        with self.assertRaisesRegex(ValueError, "24-question"):
            self._service(practice_pool=make_pool(), practice_target_band=[0.5, 1.0])

    def test_injected_recommender_cannot_bypass_current_pool_binding(self):
        recommender = ShadowPracticeRecommender(
            empty_pool(), [0.5, 0.9], lambda *args: None)
        with self.assertRaisesRegex(ValueError, "24-question"):
            self._service(recommender=recommender)

    def test_constructor_rejects_unpaired_and_mixed_arguments(self):
        pool = make_pool()
        with self.assertRaises(ValueError):
            self._service(practice_pool=pool)
        with self.assertRaises(ValueError):
            self._service(practice_target_band=[0.5, 0.9])
        with self.assertRaises(ValueError):
            self._service(practice_pool=pool,
                          practice_target_band=[0.5, 0.9],
                          recommender=FakeRecommender())
        with self.assertRaises(ValueError):
            self._service(practice_pool={"schema": "wrong"},
                          practice_target_band=[0.5, 0.9])
        with self.assertRaises(ValueError):
            self._service(practice_pool=pool,
                          practice_target_band=[0.9, 0.5])

    def test_teacher_config_exposes_enabled_shadow_only(self):
        service = self._service()
        self.assertEqual(service.teacher_config()["shadow_practice"],
                         {"enabled": False, "mode": "shadow_only"})
        shadowed = self._service(recommender=FakeRecommender())
        self.assertEqual(shadowed.teacher_config()["shadow_practice"],
                         {"enabled": True, "mode": "shadow_only"})


class CliPairingTests(unittest.TestCase):
    def test_unpaired_shadow_flags_rejected_before_startup(self):
        for argv in (["--shadow-practice-pool", "pool.json"],
                     ["--shadow-target-band", "0.5", "0.9"]):
            with self.assertRaises(SystemExit):
                demo_api.main(argv)


if __name__ == "__main__":
    unittest.main()
