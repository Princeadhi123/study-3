"""Tests for the worked-first provider evidence projection (lead module)."""
import copy
import math
import unittest

import phase3_paths  # noqa: F401 -- installs the Phase 2 import path
from evidence_feedback import build_evidence
from live_diagnostics import LiveDiagnostics
from worked_first_context import (
    CONTEXT_VERSION, KT_BOUNDARY, build_worked_first_context,
    validate_worked_first_context)
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_kt_adapter import FakeKT


def _context_for(bank, taxonomy, rows):
    return {"bank": bank, "taxonomy": taxonomy, "responses": rows}


def _diag_block(status="ready", result=None, reason=None):
    job = {"status": status}
    if reason:
        job["reason"] = reason
    return {"diagnostics_job": job, "diagnostics": result}


def _ready_result(checkpoint, count, probabilities):
    return {"status": "ready", "checkpoint": checkpoint,
            "answer_count": count,
            "kt": {"p_correct_before_each_answer": probabilities}}


class WorkedFirstContextTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.wrong = {2, 17, 33}
        self.rows = []
        for index, q in enumerate(self.bank["questions"]):
            selected = q["answer_index"]
            if index in self.wrong:
                selected = (selected + 1) % len(q["options"])
            self.rows.append({"question_id": q["question_id"],
                              "selected_index": selected})
        self.evidence = build_evidence(
            self.bank, self.taxonomy, self.rows)
        self.assessment_context = _context_for(
            self.bank, self.taxonomy, self.rows)
        diag = LiveDiagnostics(model_loader=lambda: FakeKT(self.bank))
        self.midpoint = diag.evaluate(
            self.bank, self.taxonomy, self.rows[:20])
        self.end = diag.evaluate(self.bank, self.taxonomy, self.rows)
        self.blocks = {
            "midpoint": _diag_block(result=self.midpoint),
            "end": _diag_block(result=self.end)}

    def test_records_cover_all_40_in_order_with_correct_flags(self):
        context = build_worked_first_context(
            self.evidence, self.assessment_context, self.blocks)
        self.assertEqual(context["version"], CONTEXT_VERSION)
        records = context["assessment_records"]
        self.assertEqual(len(records), 40)
        leaf = {qid: (leaf["id"], leaf["name"])
                for topic in self.taxonomy["topics"]
                for leaf in topic["subtopics"]
                for qid in leaf["question_ids"]}
        for index, (record, q, row) in enumerate(
                zip(records, self.bank["questions"], self.rows), 1):
            leaf_id, leaf_name = leaf[q["question_id"]]
            self.assertEqual(record, {
                "position": index,
                "skill_id": q["skill_id"],
                "subtopic_id": leaf_id,
                "subtopic_name": leaf_name,
                "question": q["text"],
                "options": q["options"],
                "selected_answer": q["options"][row["selected_index"]],
                "correct_answer": q["options"][q["answer_index"]],
                "correct": row["selected_index"] == q["answer_index"]})
        self.assertEqual(
            {i for i, r in enumerate(records) if not r["correct"]},
            self.wrong)
        self.assertEqual(context["concept_graph"]["relation"],
                         "is_part_of_not_prerequisite")
        self.assertEqual(
            [t["skill_id"] for t in
             context["concept_graph"]["topics"]],
            [t["skill_id"] for t in self.taxonomy["topics"]])

    def test_projection_excludes_unlisted_fields_and_sentinels(self):
        # Extra assessment-context fields (session ids, credentials,
        # capture paths) are rejected outright by the exact-keys contract.
        for key in ("session_id", "credential_env", "capture_file"):
            context = dict(self.assessment_context)
            context[key] = "C:\\private\\SECRET"
            with self.assertRaises(ValueError):
                build_worked_first_context(
                    self.evidence, context, self.blocks)
        ctx = build_worked_first_context(
            self.evidence, self.assessment_context, self.blocks)
        allowed = {"position", "skill_id", "subtopic_id",
                   "subtopic_name", "question", "options",
                   "selected_answer", "correct_answer", "correct"}
        self.assertTrue(all(set(r) == allowed
                            for r in ctx["assessment_records"]))
        import json
        raw = json.dumps(ctx)
        for forbidden in ("SECRET-SESSION", "AITTA_API_KEY",
                          "C:\\private", "answer_index",
                          "selected_index", "item_id", "content_text"):
            self.assertNotIn(forbidden, raw)

    def test_model_context_exact_known_vectors(self):
        ctx = build_worked_first_context(
            self.evidence, self.assessment_context, self.blocks)
        model = ctx["model_context"]
        self.assertEqual(model["interpretation"], KT_BOUNDARY)
        for checkpoint, result, count in (
                ("midpoint", self.midpoint, 20),
                ("end", self.end, 40)):
            block = model["checkpoints"][checkpoint]
            self.assertEqual(block["status"], "available")
            self.assertEqual(block["answer_count"], count)
            self.assertEqual(
                block["pre_answer_probabilities"],
                result["kt"]["p_correct_before_each_answer"])
            self.assertEqual(len(block["pre_answer_probabilities"]), count)

    def test_unavailable_and_missing_diagnostics_become_explicit(self):
        blocks = {
            "midpoint": _diag_block("unavailable",
                                    reason="interrupted_by_restart",
                                    result={"status": "unavailable"}),
            "end": _diag_block("not_requested")}
        ctx = build_worked_first_context(
            self.evidence, self.assessment_context, blocks)
        self.assertEqual(ctx["model_context"]["checkpoints"], {
            "midpoint": {"status": "unavailable"},
            "end": {"status": "unavailable"}})
        none_ctx = build_worked_first_context(
            self.evidence, self.assessment_context)
        self.assertEqual(
            none_ctx["model_context"]["checkpoints"]["end"],
            {"status": "unavailable"})

    def test_pending_diagnostics_rejected(self):
        blocks = copy.deepcopy(self.blocks)
        blocks["end"]["diagnostics_job"]["status"] = "pending"
        with self.assertRaises(ValueError):
            build_worked_first_context(
                self.evidence, self.assessment_context, blocks)

    def test_rejects_mismatched_or_tampered_inputs(self):
        ctx = build_worked_first_context(
            self.evidence, self.assessment_context, self.blocks)
        tampered = copy.deepcopy(ctx)
        tampered["assessment_records"][0]["correct"] = False
        with self.assertRaises(ValueError):
            validate_worked_first_context(
                tampered, self.evidence, self.assessment_context)
        tampered = copy.deepcopy(ctx)
        tampered["concept_graph"]["topics"][0]["subtopics"].pop()
        with self.assertRaises(ValueError):
            validate_worked_first_context(
                tampered, self.evidence, self.assessment_context)
        for key in ("assessment_records", "concept_graph",
                    "model_context", "version"):
            tampered = copy.deepcopy(ctx)
            tampered.pop(key)
            with self.assertRaises(ValueError):
                validate_worked_first_context(
                    tampered, self.evidence, self.assessment_context)
        tampered = copy.deepcopy(ctx)
        tampered["extra"] = {}
        with self.assertRaises(ValueError):
            validate_worked_first_context(
                tampered, self.evidence, self.assessment_context)
        with self.assertRaises(ValueError):
            build_worked_first_context(
                self.evidence,
                _context_for(self.bank, self.taxonomy,
                             responses(self.bank, correct=True)),
                self.blocks)

    def test_invalid_model_projection_rejected(self):
        base = build_worked_first_context(
            self.evidence, self.assessment_context, self.blocks)
        probs = base["model_context"]["checkpoints"]["end"][
            "pre_answer_probabilities"]
        for bad in (float("nan"), float("inf"), -0.1, 1.1, True):
            tampered = copy.deepcopy(base)
            tampered["model_context"]["checkpoints"]["end"][
                "pre_answer_probabilities"] = probs[:39] + [bad]
            with self.assertRaises(ValueError):
                validate_worked_first_context(
                    tampered, self.evidence, self.assessment_context)
        tampered = copy.deepcopy(base)
        tampered["model_context"]["checkpoints"]["end"][
            "pre_answer_probabilities"] = probs[:39]
        with self.assertRaises(ValueError):
            validate_worked_first_context(
                tampered, self.evidence, self.assessment_context)
        tampered = copy.deepcopy(base)
        tampered["model_context"]["checkpoints"]["end"][
            "answer_count"] = 20
        with self.assertRaises(ValueError):
            validate_worked_first_context(
                tampered, self.evidence, self.assessment_context)
        tampered = copy.deepcopy(base)
        tampered["model_context"]["checkpoints"][
            "midpoint"]["checkpoint"] = "end"
        with self.assertRaises(ValueError):
            validate_worked_first_context(
                tampered, self.evidence, self.assessment_context)
        self.assertFalse(math.isnan(probs[0]))


if __name__ == "__main__":
    unittest.main()
