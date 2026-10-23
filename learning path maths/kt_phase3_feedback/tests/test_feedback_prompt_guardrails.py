"""Offline contracts for the audit-driven prompt safeguards."""
import unittest

from feedback_language import (
    ASSESSMENT_FACT_INSTRUCTIONS, MATH_NOTATION_INSTRUCTIONS,
    PLAIN_LANGUAGE_INSTRUCTIONS, PRACTICE_STATUS_INSTRUCTIONS)
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
        self.assertEqual(FULL_FEEDBACK_PROMPT_VERSION, "aitta_feedback_with_practice_v7")
        self.assertEqual(MULTI_FULL_FEEDBACK_PROMPT_VERSION, "aitta_assessment_grounded_two_topic_feedback_v11")
        self.assertEqual(WORKED_FIRST_PROMPT_VERSION, "aitta_worked_first_evidence_author_v9")
        self.assertEqual(MIDPOINT_PROMPT_VERSION, "aitta_midpoint_check_in_v5")
        self.assertEqual(SCOPED_PROMPT_VERSION, "aitta_selected_worked_example_v4")

    def test_math_fidelity_reaches_every_generation_path(self):
        for instructions in (FULL_FEEDBACK_INSTRUCTIONS,
                             MULTI_FULL_FEEDBACK_INSTRUCTIONS,
                             WORKED_FIRST_INSTRUCTIONS,
                             MIDPOINT_INSTRUCTIONS, SCOPED_INSTRUCTIONS):
            with self.subTest(instructions=instructions[:40]):
                self.assertIn(MATH_NOTATION_INSTRUCTIONS, instructions)
        for rule in ("U+2044", "Never invent digits", "3/6 is proper",
                     "7/2 = 3 1/2, never 3/2",
                     "compare equivalent values rather than spellings",
                     "never invent a matching option",
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
                     "correct equals out_of and incorrect is zero",
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
                     "do not promise one for every review topic"):
            self.assertIn(rule, PRACTICE_STATUS_INSTRUCTIONS)

    def test_response_shape_and_step_limits_are_rechecked(self):
        for instructions in (GROUNDED_WORKED_REVIEW_OUTPUT_INSTRUCTIONS,
                             WORKED_FIRST_INSTRUCTIONS, SCOPED_INSTRUCTIONS):
            self.assertIn("instead of adding a seventh confirmation step", instructions)
        self.assertIn("never an array", WORKED_FIRST_INSTRUCTIONS)
        self.assertIn("never an array", MIDPOINT_INSTRUCTIONS)
        self.assertIn("unique maximum of 5, not a tie", MIDPOINT_INSTRUCTIONS)
        self.assertIn("not a judgement of ability", MIDPOINT_INSTRUCTIONS)
        self.assertIn("next_steps must only encourage continuing", MIDPOINT_INSTRUCTIONS)
