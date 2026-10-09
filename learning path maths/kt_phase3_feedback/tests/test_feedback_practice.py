import copy
import json
import unittest
from unittest import mock

import phase3_paths  # noqa: F401
import feedback_practice
from evidence_feedback import build_evidence, canonical_digest
from feedback_practice import (
    PRACTICE_STATUS_MESSAGES, empty_context, project_practice_context,
    resolve_practice, validate_practice_context)
from session_store import bank_fingerprint
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_shadow_recommendation_service import make_pool


def make_graph(pool):
    return {"exercises": [
        {"question_id": q["question_id"], "source_id": "practice_pool_v2",
         "role": "practice", "skill_id": q["skill_id"],
         "concept_id": f"concept_{q['skill_id']}",
         "concept_label": f"Concept {q['skill_id']}",
         "text": q["text"], "options": list(q["options"]),
         "mathematical_task": f"Task for {q['skill_id']}"}
        for q in pool["questions"]]}


def selected_result(bank, rows, pool, qid="pool_q1", band=(0.4, 0.8),
                    p_correct=0.6):
    questions = {q["question_id"]: q for q in pool["questions"]}
    q = questions[qid]
    return {
        "schema": "phase3_shadow_practice_recommendation_v1",
        "status": "selected", "reason": None, "mode": "shadow_only",
        "bank_sha256": bank_fingerprint(bank),
        "response_sha256": canonical_digest(rows),
        "pool_sha256": canonical_digest(pool),
        "answer_count": 40,
        "target_band": list(band),
        "selected_question_id": qid,
        "baseline_question_id": qid,
        "candidate_predictions": [
            {"question_id": qid, "skill_id": q["skill_id"],
             "item_id": q["item_id"], "regime": "warm",
             "p_correct": p_correct}],
        "in_band_question_ids": [qid],
        "outside_band_question_ids": [],
        "used_for_student_advice": False, "used_for_feedback": False}


class PracticeCase(unittest.TestCase):
    def setUp(self):
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.pool = make_pool(("sA", "sB"))
        self.graph = make_graph(self.pool)
        patch_pool = mock.patch.object(
            feedback_practice, "load_current_practice_pool",
            return_value=copy.deepcopy(self.pool))
        patch_graph = mock.patch.object(
            feedback_practice, "teacher_content_context",
            return_value=copy.deepcopy(self.graph))
        self.addCleanup(patch_pool.stop)
        self.addCleanup(patch_graph.stop)
        patch_pool.start()
        patch_graph.start()

    def evidence(self, wrong=("sA",)):
        rows = responses(self.bank)
        for row in rows:
            q = next(q for q in self.bank["questions"]
                     if q["question_id"] == row["question_id"])
            if q["skill_id"] in wrong:
                row["selected_index"] = (
                    q["answer_index"] + 1) % len(q["options"])
        return build_evidence(self.bank, self.taxonomy, rows), rows

    def context(self, qid="pool_q1"):
        return {"schema": feedback_practice.PRACTICE_CONTEXT_SCHEMA,
                "status": "selected", "selected_question_id": qid,
                "pool_sha256": canonical_digest(self.pool)}


class EmptyContextTests(PracticeCase):
    def test_terminal_empty_statuses(self):
        for status in ("abstained", "unavailable", "disabled"):
            ctx = empty_context(status)
            self.assertEqual(ctx["status"], status)
            self.assertIsNone(ctx["selected_question_id"])
            self.assertIsNone(ctx["pool_sha256"])

    def test_selected_empty_rejected(self):
        with self.assertRaises(ValueError):
            empty_context("selected")
        with self.assertRaises(ValueError):
            empty_context("pending")


class ValidateContextTests(PracticeCase):
    def test_valid_selected(self):
        evidence, _ = self.evidence()
        ctx = validate_practice_context(self.context(), evidence)
        self.assertEqual(ctx["status"], "selected")

    def test_shape_and_field_rejection(self):
        evidence, _ = self.evidence()
        for bad in (None, "x", {"schema": "x"},
                    {**self.context(), "extra": 1},
                    {**self.context(), "status": "pending"},
                    {**self.context(), "selected_question_id": ""},
                    {**self.context(), "status": "abstained"}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_practice_context(bad, evidence)

    def test_pool_hash_mismatch(self):
        evidence, _ = self.evidence()
        ctx = self.context()
        ctx["pool_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "pool hash"):
            validate_practice_context(ctx, evidence)

    def test_question_not_in_pool(self):
        evidence, _ = self.evidence()
        with self.assertRaisesRegex(ValueError, "not in the pool"):
            validate_practice_context(self.context("pool_q9"), evidence)

    def test_skill_mismatch_and_no_errors(self):
        evidence, _ = self.evidence(wrong=("sB",))
        with self.assertRaisesRegex(ValueError, "observed errors"):
            validate_practice_context(self.context("pool_q1"), evidence)
        ctx = validate_practice_context(
            self.context("pool_q2"), evidence)
        self.assertEqual(ctx["selected_question_id"], "pool_q2")

    def test_all_correct_rejects_selected(self):
        evidence, _ = self.evidence(wrong=())
        with self.assertRaisesRegex(ValueError, "observed errors"):
            validate_practice_context(self.context(), evidence)

    def test_graph_mismatch(self):
        evidence, _ = self.evidence()
        broken = copy.deepcopy(self.graph)
        broken["exercises"] = [e for e in broken["exercises"]
                               if e["question_id"] != "pool_q1"]
        with mock.patch.object(feedback_practice,
                               "teacher_content_context",
                               return_value=broken):
            with self.assertRaisesRegex(ValueError, "graph"):
                validate_practice_context(self.context(), evidence)


class ProjectContextTests(PracticeCase):
    def test_selected_projects(self):
        evidence, rows = self.evidence()
        result = selected_result(self.bank, rows, self.pool)
        ctx, reason = project_practice_context(
            result, self.bank, rows, evidence, None)
        self.assertIsNone(reason)
        self.assertEqual(ctx, self.context())

    def test_terminal_empty_statuses_project(self):
        evidence, rows = self.evidence()
        for status in ("disabled", "abstained", "unavailable"):
            ctx, reason = project_practice_context(
                {"status": status}, self.bank, rows, evidence, None)
            self.assertIsNone(reason)
            self.assertEqual(ctx, empty_context(status))

    def test_missing_result_unavailable(self):
        evidence, rows = self.evidence()
        ctx, reason = project_practice_context(
            None, self.bank, rows, evidence, None)
        self.assertEqual(ctx["status"], "unavailable")
        self.assertEqual(reason, "practice_context_validation_failed")

    def test_binding_mismatches_unavailable_no_substitute(self):
        evidence, rows = self.evidence()
        good = selected_result(self.bank, rows, self.pool)
        cases = []
        for key, value in (
                ("bank_sha256", "0" * 64),
                ("response_sha256", "0" * 64),
                ("pool_sha256", "0" * 64),
                ("answer_count", 20),
                ("target_band", [0.7, 0.9]),
                ("selected_question_id", "pool_q9")):
            bad = copy.deepcopy(good)
            bad[key] = value
            cases.append(bad)
        bad = copy.deepcopy(good)
        bad["candidate_predictions"][0]["p_correct"] = 0.95
        cases.append(bad)
        bad = copy.deepcopy(good)
        bad["candidate_predictions"][0]["skill_id"] = "sB"
        cases.append(bad)
        bad = copy.deepcopy(good)
        bad["candidate_predictions"][0]["p_correct"] = True
        cases.append(bad)
        bad = copy.deepcopy(good)
        bad["candidate_predictions"][0]["regime"] = "synthetic"
        cases.append(bad)
        bad = copy.deepcopy(good)
        bad["schema"] = "phase3_unrelated_v1"
        cases.append(bad)
        bad = copy.deepcopy(good)
        bad["candidate_predictions"] = (
            bad["candidate_predictions"] + bad["candidate_predictions"])
        cases.append(bad)
        bad = copy.deepcopy(good)
        bad["candidate_predictions"] = {"pool_q1": {}}
        cases.append(bad)
        bad = copy.deepcopy(good)
        bad["answer_count"] = 40.0
        cases.append(bad)
        for result in cases:
            ctx, reason = project_practice_context(
                result, self.bank, rows, evidence, None)
            self.assertEqual(ctx["status"], "unavailable")
            self.assertIsNone(ctx["selected_question_id"])
            self.assertEqual(reason,
                             "practice_context_validation_failed")

    def test_already_assessed_question_rejected(self):
        evidence, rows = self.evidence()
        pool = copy.deepcopy(self.pool)
        bank_q = self.bank["questions"][0]
        pool["questions"][0]["question_id"] = bank_q["question_id"]
        pool["questions"][0]["text"] = "Completely different prompt?"
        result = selected_result(self.bank, rows, pool,
                                 qid=bank_q["question_id"])
        with mock.patch.object(feedback_practice,
                               "load_current_practice_pool",
                               return_value=pool):
            ctx, reason = project_practice_context(
                result, self.bank, rows, evidence, None)
        self.assertEqual(ctx["status"], "unavailable")
        self.assertEqual(reason,
                         "practice_context_validation_failed")

    def test_already_assessed_prompt_rejected(self):
        evidence, rows = self.evidence()
        pool = copy.deepcopy(self.pool)
        bank_q = self.bank["questions"][0]
        pool["questions"][0]["text"] = bank_q["text"]
        pool["questions"][0]["content_text"] = (
            bank_q["text"] + " [OPTIONS] " +
            " | ".join(pool["questions"][0]["options"]))
        result = selected_result(self.bank, rows, pool)
        with mock.patch.object(feedback_practice,
                               "load_current_practice_pool",
                               return_value=pool):
            ctx, reason = project_practice_context(
                result, self.bank, rows, evidence, None)
        self.assertEqual(ctx["status"], "unavailable")
        self.assertEqual(reason,
                         "practice_context_validation_failed")

    def test_band_attribute_enforced(self):
        evidence, rows = self.evidence()
        result = selected_result(self.bank, rows, self.pool)

        class Rec:
            band = (0.5, 0.9)

        ctx, reason = project_practice_context(
            result, self.bank, rows, evidence, Rec())
        self.assertEqual(ctx["status"], "unavailable")
        self.assertIsNotNone(reason)


class ResolvePracticeTests(PracticeCase):
    def test_selected_card_and_wire(self):
        evidence, _ = self.evidence()
        resolved = resolve_practice(self.context(), evidence)
        card = resolved["card"]
        self.assertEqual(set(card), {"status", "message", "notice",
                                     "question"})
        self.assertEqual(card["status"], "selected")
        self.assertEqual(card["message"],
                         PRACTICE_STATUS_MESSAGES["selected"])
        q = card["question"]
        self.assertEqual(set(q), {"skill_name", "concept_name", "text",
                                  "options"})
        self.assertEqual(q["text"], "Pool practice prompt 1?")
        self.assertEqual(q["options"], ["one", "two", "three"])
        self.assertEqual(q["concept_name"], "Concept sA")
        self.assertEqual(resolved["selection"], {
            "skill_name": q["skill_name"],
            "concept_name": "Concept sA",
            "mathematical_task": "Task for sA"})

    def test_private_fields_never_leak(self):
        evidence, _ = self.evidence()
        pool = copy.deepcopy(self.pool)
        for q in pool["questions"]:
            q["answer_index"] = 0
            q["private_note"] = "secret"
        ctx = self.context()
        ctx["pool_sha256"] = canonical_digest(pool)
        with mock.patch.object(feedback_practice,
                               "load_current_practice_pool",
                               return_value=pool):
            card = resolve_practice(ctx, evidence)["card"]
        blob = str(card)
        for needle in ("answer_index", "p_correct", "pool_q",
                       "private_note", "secret", "sha256", "item_id"):
            self.assertNotIn(needle, blob)
        self.assertIn("does not establish mastery",
                      card["notice"])

    def test_nonselected_statuses_empty_question(self):
        evidence, _ = self.evidence()
        for status in ("abstained", "unavailable", "disabled"):
            card = resolve_practice(empty_context(status), evidence)["card"]
            self.assertIsNone(card["question"])
            self.assertEqual(card["message"],
                             PRACTICE_STATUS_MESSAGES[status])


class RealAssetsBindingTests(unittest.TestCase):
    def test_current_pool_and_warm_bank_bind(self):
        from evidence_feedback import build_research_evidence
        from research_runtime import (
            load_current_practice_pool, load_research_bank,
            teacher_content_context)
        from shadow_practice import eligibility
        bank, taxonomy, _ = load_research_bank("warm")
        pool = load_current_practice_pool()
        rows = [{"question_id": q["question_id"],
                 "selected_index": (q["answer_index"] + 1)
                 % len(q["options"])}
                for q in bank["questions"]]
        evidence = build_research_evidence(bank, taxonomy, rows)
        candidates, exclusions, _ = eligibility(bank, evidence, pool)
        self.assertTrue(candidates)
        question = candidates[0]
        result = {
            "schema": "phase3_shadow_practice_recommendation_v1",
            "status": "selected", "reason": None, "mode": "shadow_only",
            "bank_sha256": bank_fingerprint(bank),
            "response_sha256": canonical_digest(rows),
            "pool_sha256": canonical_digest(pool),
            "answer_count": 40,
            "target_band": [0.4, 0.8],
            "selected_question_id": question["question_id"],
            "baseline_question_id": question["question_id"],
            "candidate_predictions": [
                {"question_id": question["question_id"],
                 "skill_id": question["skill_id"],
                 "item_id": question["item_id"], "regime": "warm",
                 "p_correct": 0.6}],
            "in_band_question_ids": [question["question_id"]],
            "outside_band_question_ids": [],
            "used_for_student_advice": False,
            "used_for_feedback": False}
        ctx, reason = project_practice_context(
            result, bank, rows, evidence, None)
        self.assertIsNone(reason)
        self.assertEqual(ctx["status"], "selected")
        self.assertEqual(ctx["selected_question_id"],
                         question["question_id"])
        resolved = resolve_practice(ctx, evidence)
        card = resolved["card"]["question"]
        self.assertEqual(card["text"], question["text"])
        self.assertEqual(card["options"], question["options"])
        graph_rows = [e for e in teacher_content_context("warm")
                      ["exercises"]
                      if e["question_id"] == question["question_id"]]
        self.assertEqual(len(graph_rows), 1)
        self.assertEqual(card["concept_name"],
                         graph_rows[0]["concept_label"])
        self.assertEqual(resolved["selection"]["mathematical_task"],
                         graph_rows[0]["mathematical_task"])
        from evidence_feedback_policy import build_candidates
        from full_feedback import (
            FullFeedbackAittaGenerator, build_full_input)
        from aitta_generator import AittaGenerator
        from tests.test_full_feedback import (
            RecordingOpener, wire_reply)
        candidate = build_candidates(evidence, "end")[0]
        full_input = build_full_input(evidence, "teacher", candidate,
                                      ctx)
        opener = RecordingOpener(
            wire_reply(candidate_id=candidate["candidate_id"]))
        inner = AittaGenerator("test-aitta-key-0123456789abcdef",
                               "https://aitta.example.test",
                               model="openai/gpt-oss-120b",
                               opener=opener)
        request = FullFeedbackAittaGenerator(inner).request(full_input)
        context = json.loads(request["messages"][1]["content"])
        self.assertEqual(
            set(context["practice_context"]), {"status", "selection"})
        wire = json.dumps(context["practice_context"])
        for needle in (question["question_id"], question["text"],
                       "p_correct", "answer_index", "item_id",
                       "options"):
            self.assertNotIn(needle, wire)
        self.assertEqual(opener.calls, [])


if __name__ == "__main__":
    unittest.main()
