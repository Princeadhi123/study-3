"""Offline contracts for the audit-driven prompt safeguards."""
import copy
import unittest

from feedback_language import (
    ASSESSMENT_FACT_INSTRUCTIONS, END_REPORTING_INSTRUCTIONS,
    MATH_NOTATION_INSTRUCTIONS, MIDPOINT_REPORTING_INSTRUCTIONS,
    PLAIN_LANGUAGE_INSTRUCTIONS, PRACTICE_STATUS_INSTRUCTIONS,
    provider_math_context)
from full_feedback import (
    FULL_FEEDBACK_INSTRUCTIONS, FULL_FEEDBACK_PROMPT_VERSION,
    GROUNDED_WORKED_REVIEW_OUTPUT_INSTRUCTIONS,
    MULTI_FULL_FEEDBACK_INSTRUCTIONS, MULTI_FULL_FEEDBACK_PROMPT_VERSION)
from midpoint_feedback import INSTRUCTIONS as MIDPOINT_INSTRUCTIONS
from midpoint_feedback import PROMPT_VERSION as MIDPOINT_PROMPT_VERSION
from worked_example_generation import INSTRUCTIONS as SCOPED_INSTRUCTIONS
from worked_example_generation import PROMPT_VERSION as SCOPED_PROMPT_VERSION
from worked_first_context import INSTRUCTIONS as WORKED_FIRST_INSTRUCTIONS
from worked_first_context import PROMPT_VERSION as WORKED_FIRST_PROMPT_VERSION


class FeedbackPromptGuardrailTests(unittest.TestCase):
    def test_new_versions_isolate_previous_requests(self):
        self.assertEqual(FULL_FEEDBACK_PROMPT_VERSION, "aitta_feedback_with_practice_v8")
        self.assertEqual(MULTI_FULL_FEEDBACK_PROMPT_VERSION, "aitta_assessment_grounded_two_topic_feedback_v13")
        self.assertEqual(WORKED_FIRST_PROMPT_VERSION, "aitta_worked_first_evidence_author_v11")
        self.assertEqual(MIDPOINT_PROMPT_VERSION, "aitta_midpoint_check_in_v6")
        self.assertEqual(SCOPED_PROMPT_VERSION, "aitta_selected_worked_example_v6")

    def test_math_fidelity_reaches_every_generation_path(self):
        for instructions in (FULL_FEEDBACK_INSTRUCTIONS,
                             MULTI_FULL_FEEDBACK_INSTRUCTIONS,
                             WORKED_FIRST_INSTRUCTIONS,
                             SCOPED_INSTRUCTIONS):
            with self.subTest(instructions=instructions[:40]):
                self.assertIn(MATH_NOTATION_INSTRUCTIONS, instructions)
        self.assertNotIn(MATH_NOTATION_INSTRUCTIONS, MIDPOINT_INSTRUCTIONS)
        for rule in ("U+2044", "Never invent digits", "3/6 is proper",
                     "7/2 = 3 1/2, never 3/2",
                     "Compare equivalent values rather than spellings",
                     "or invent a matching option",
                     "1/2 * 2/3 = 2/6 = 1/3",
                     "3/6 * 10/1 = 30/6 = 5",
                     "only 100% equals 1",
                     "Do not say a particular step caused the error",
                     "not a seventh confirmation step",
                     "Do not use LaTeX commands or math delimiters"):
            self.assertIn(rule, MATH_NOTATION_INSTRUCTIONS)

    def test_count_reconciliation_reaches_all_narratives(self):
        self.assertIn(ASSESSMENT_FACT_INSTRUCTIONS, PLAIN_LANGUAGE_INSTRUCTIONS)
        for instructions in (FULL_FEEDBACK_INSTRUCTIONS,
                             MULTI_FULL_FEEDBACK_INSTRUCTIONS,
                             WORKED_FIRST_INSTRUCTIONS, MIDPOINT_INSTRUCTIONS):
            self.assertIn(ASSESSMENT_FACT_INSTRUCTIONS, instructions)
        for rule in ("correct, incorrect and out_of counts together",
                     "Child counts are already included in the parent",
                     "'X of Y questions correctly'",
                     "not 10 divisor successes plus one prime success",
                     "must not recap individual question examples or positions",
                     "More incorrect than correct means incorrect > correct",
                     "Incorrect completed answers are not unanswered",
                     "Do not derive performance percentages"):
            self.assertIn(rule, ASSESSMENT_FACT_INSTRUCTIONS)
        self.assertNotIn(ASSESSMENT_FACT_INSTRUCTIONS, SCOPED_INSTRUCTIONS)

    def test_practice_rules_are_limited_to_end_feedback(self):
        for instructions in (FULL_FEEDBACK_INSTRUCTIONS,
                             MULTI_FULL_FEEDBACK_INSTRUCTIONS,
                             WORKED_FIRST_INSTRUCTIONS):
            self.assertIn(PRACTICE_STATUS_INSTRUCTIONS, instructions)
        self.assertNotIn(PRACTICE_STATUS_INSTRUCTIONS, MIDPOINT_INSTRUCTIONS)
        self.assertNotIn(PRACTICE_STATUS_INSTRUCTIONS, SCOPED_INSTRUCTIONS)
        for rule in ("use singular for one", "No eligible activity is different",
                     "Same broad topic does not mean identical content",
                     "Do not promise one for every review topic",
                     "Zero selected slots",
                     "One selected slot",
                     "Multiple selected slots",
                     "No separate practice activity was selected.",
                     "retaining unlike terms is not combining their coefficients"):
            self.assertIn(rule, PRACTICE_STATUS_INSTRUCTIONS)

    def test_section_contracts_are_scoped_and_final(self):
        for instructions in (FULL_FEEDBACK_INSTRUCTIONS,
                             MULTI_FULL_FEEDBACK_INSTRUCTIONS,
                             WORKED_FIRST_INSTRUCTIONS):
            self.assertTrue(instructions.endswith(END_REPORTING_INSTRUCTIONS))
            self.assertNotIn(MIDPOINT_REPORTING_INSTRUCTIONS, instructions)
        self.assertTrue(MIDPOINT_INSTRUCTIONS.endswith(MIDPOINT_REPORTING_INSTRUCTIONS))
        self.assertNotIn(END_REPORTING_INSTRUCTIONS, MIDPOINT_INSTRUCTIONS)
        self.assertNotIn(END_REPORTING_INSTRUCTIONS, SCOPED_INSTRUCTIONS)
        self.assertNotIn(MIDPOINT_REPORTING_INSTRUCTIONS, SCOPED_INSTRUCTIONS)
        for rule in ("at most two broad topics", "Do not enumerate child counts",
                     "do not tell the learner to create new questions"):
            self.assertIn(rule.lower(), END_REPORTING_INSTRUCTIONS.lower())
        for rule in ("Do not say 'in each area' when any topic has zero correct",
                     "unless every other topic has zero incorrect",
                     "no section contradicts another",
                     "Never swap a fraction-topic success into percentages"):
            self.assertIn(rule, MIDPOINT_REPORTING_INSTRUCTIONS)

    def test_response_shape_and_step_limits_are_rechecked(self):
        for instructions in (GROUNDED_WORKED_REVIEW_OUTPUT_INSTRUCTIONS,
                             WORKED_FIRST_INSTRUCTIONS, SCOPED_INSTRUCTIONS):
            self.assertIn("instead of adding a seventh confirmation step", instructions)
        self.assertIn("never an array", WORKED_FIRST_INSTRUCTIONS)
        self.assertIn("never an array", MIDPOINT_INSTRUCTIONS)
        self.assertIn("unique maximum of 5, not a tie", MIDPOINT_INSTRUCTIONS)
        self.assertIn("not a judgement of ability", MIDPOINT_INSTRUCTIONS)
        self.assertIn("next_steps must only encourage continuing", MIDPOINT_INSTRUCTIONS)


class ProviderMathContextTests(unittest.TestCase):
    def test_nested_math_fields_are_ascii_without_mutating_source(self):
        record = {
            "question_id": "id\u2044unchanged", "prompt": "3\u20446 * 10\u20441",
            "text": "1\u20442 * 2\u20443",
            "question": "2\u20443 * 1\u20442",
            "options": ["30\u20446", "6\u204424"],
            "selected_answer": "6\u204424", "correct_answer": "30\u20446",
            "label": "label\u2044unchanged", "p_correct": .6,
            "correct": False, "out_of": 10,
        }
        source = {"review_examples": [record],
                  "worked_first_context": {"assessment_records": [copy.deepcopy(record)]}}
        before = copy.deepcopy(source)
        result = provider_math_context(source)
        for actual in (result["review_examples"][0],
                       result["worked_first_context"]["assessment_records"][0]):
            self.assertEqual(actual["prompt"], "3/6 * 10/1")
            self.assertEqual(actual["text"], "1/2 * 2/3")
            self.assertEqual(actual["question"], "2/3 * 1/2")
            self.assertEqual(actual["options"], ["30/6", "6/24"])
            self.assertEqual(actual["selected_answer"], "6/24")
            self.assertEqual(actual["correct_answer"], "30/6")
            for key in ("question_id", "label", "p_correct", "correct", "out_of"):
                self.assertEqual(actual[key], record[key])
        result["review_examples"][0]["options"].append("new")
        self.assertEqual(source, before)
        self.assertEqual(len(result["worked_first_context"]["assessment_records"][0]["options"]), 2)

    def test_no_fraction_or_mixed_number_is_reinterpreted(self):
        for text in ("10/1", "10/41", "1 1/2", "-2/3", "2/3 * 1/2"):
            with self.subTest(text=text):
                self.assertEqual(provider_math_context({"prompt": text})["prompt"], text)
