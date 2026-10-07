"""Research scoring separation and frozen smoke preparation contracts."""
import copy
import unittest

from evidence_feedback import build_evidence, build_research_evidence
from mcq_test import score_checkpoint, student_questions, validate_bank_structure
from shadow_practice import ShadowPracticeRecommender
from shadow_smoke import BAND, PATTERNS, cases, taxonomy
from tests.helpers import make_bank, make_taxonomy, responses


def research_bank():
    bank = make_bank()
    bank["protocol"] = "offline_historical_evaluation_bank"
    bank["review_status"] = "independently_math_checked; educator_approval_pending"
    return bank


class ResearchScoringTests(unittest.TestCase):
    def test_research_scoring_does_not_approve_or_serve_bank(self):
        bank = research_bank()
        tax = make_taxonomy(bank)
        before = copy.deepcopy(bank)
        evidence = build_research_evidence(bank, tax, responses(bank))
        self.assertEqual(evidence["total"], {"correct": 40, "incorrect": 0, "out_of": 40})
        self.assertEqual(bank, before)
        for call in (lambda: score_checkpoint(bank, responses(bank)),
                     lambda: student_questions(bank, 1),
                     lambda: build_evidence(bank, tax, responses(bank))):
            with self.assertRaises(ValueError):
                call()

    def test_demo_and_research_aggregate_counts_agree_for_same_answers(self):
        demo = make_bank()
        research = research_bank()
        rows = responses(demo, correct=False)
        a = build_evidence(demo, make_taxonomy(demo), rows)
        b = build_research_evidence(research, make_taxonomy(research), rows)
        for key in ("total", "halves", "skills", "subtopics"):
            self.assertEqual(a[key], b[key])

    def test_structure_validator_does_not_bypass_serving_guard(self):
        bank = research_bank()
        self.assertEqual(len(validate_bank_structure(bank)), 40)
        bank["questions"][1]["question_id"] = bank["questions"][0]["question_id"]
        with self.assertRaises(ValueError):
            validate_bank_structure(bank)

    def test_research_evidence_rejects_approval_and_response_errors(self):
        bank = make_bank()
        with self.assertRaisesRegex(ValueError, "research bank"):
            build_research_evidence(bank, make_taxonomy(bank), responses(bank))
        bank = research_bank()
        for rows in (responses(bank, count=20), [{"question_id": "bad", "selected_index": 0}] * 40):
            with self.assertRaises(ValueError):
                build_research_evidence(bank, make_taxonomy(bank), rows)

    def test_research_recommender_explicit_and_private(self):
        bank = research_bank()
        rec = ShadowPracticeRecommender(
            {"schema": "phase3_shadow_practice_pool_v1",
             "scope": "research_only_not_learner_approved", "questions": []},
            BAND, lambda *args: self.fail("empty pool should not predict"))
        result = rec.recommend_research(bank, make_taxonomy(bank), responses(bank))
        self.assertEqual(result["status"], "abstained")
        self.assertFalse(result["used_for_feedback"])
        with self.assertRaises(ValueError):
            rec.recommend(bank, make_taxonomy(bank), responses(bank))


class SmokePreparationTests(unittest.TestCase):
    def test_five_patterns_match_frozen_order_and_topic_mapping(self):
        bank = research_bank()
        old = "sD"
        fraction = "skill_e8d49a51337a"
        bank["skill_names"][fraction] = bank["skill_names"].pop(old)
        for q in bank["questions"]:
            if q["skill_id"] == old:
                q["skill_id"] = fraction
        tax = taxonomy(bank)
        members = cases(bank)
        self.assertEqual(tuple(c["name"] for c in members), PATTERNS)
        totals = {}
        for case in members:
            evidence = build_research_evidence(bank, tax, case["responses"])
            totals[case["name"]] = evidence["total"]["correct"]
        self.assertEqual(totals["all_correct"], 40)
        self.assertEqual(totals["all_incorrect"], 0)
        self.assertEqual(totals["only_fractions_weak"], 30)
        self.assertEqual(totals["global_alternating"], 20)
        self.assertEqual(cases(bank), members)


if __name__ == "__main__":
    unittest.main()
