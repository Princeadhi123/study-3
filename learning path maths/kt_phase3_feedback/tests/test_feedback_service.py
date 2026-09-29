"""Tests for observed-feed plus optional model-estimate composition."""
import json
import unittest
from types import SimpleNamespace

import torch

import phase3_paths  # noqa: F401 -- installs the Phase 2 import path
from feedback_service import checkpoint_result, compose_student_message
from mcq_test import score_checkpoint
from tests.helpers import SKILLS, make_bank, make_taxonomy, responses


class FakeKT:
    def __init__(self, bank):
        self.skill_vocab = {s: i + 1 for i, s in enumerate(bank["skill_names"])}
        self.item_vocab = {"__UNK__": 1}
        texts = {q["content_text"] for q in bank["questions"]}
        texts.update(o for q in bank["questions"] for o in q["options"])
        self.dataset = SimpleNamespace(
            text_to_row={text: i for i, text in enumerate(sorted(texts))})

    def probs(self, batch):
        return torch.zeros(1, batch["skill"].shape[1])


class FeedbackServiceTests(unittest.TestCase):
    def test_observed_only(self):
        bank = make_bank()
        result = checkpoint_result(bank, responses(bank, count=20))
        self.assertEqual(result["observed_feed"]["checkpoint"], "midpoint")
        self.assertEqual(result["model_estimate"], {"status": "not_requested"})

    def test_checkpoint_taxonomy_counts_are_separate_from_model(self):
        bank = make_bank()
        result = checkpoint_result(bank, responses(bank, count=20),
                                   taxonomy=make_taxonomy(bank))
        self.assertEqual(20, sum(row["correct"] for row in
                                 result["observed_feed"]["student"]["subtopics"]))
        self.assertEqual(result["model_estimate"], {"status": "not_requested"})
        self.assertNotIn("conformal", json.dumps(
            result["observed_feed"]["student"]["subtopics"]))

    def test_optional_kt_estimate_is_separate(self):
        bank = make_bank()
        result = checkpoint_result(
            bank, responses(bank, count=20), include_kt=True,
            kt=FakeKT(bank))
        self.assertEqual(result["model_estimate"]["model_status"],
                         "uncalibrated_for_20_40_question_feed")
        self.assertNotIn("answer_index", str(result))


def make_feed(checkpoint, counts):
    out_of = 5 if checkpoint == "midpoint" else 10
    skills = [
        {"skill_id": sid, "skill_name": f"Skill {sid}", "correct": c,
         "out_of": out_of, "text": f"{c} of {out_of}"}
        for sid, c in zip(SKILLS, counts)
    ]
    return {
        "checkpoint": checkpoint,
        "questions_answered": 20 if checkpoint == "midpoint" else 40,
        "student": {"summary": "", "skills": skills},
        "teacher": {"skills": [], "total": {}},
        "evidence": {"source": "observed_answers_only"},
    }


class RecordingSelector:
    def __init__(self, reply):
        self.reply = reply
        self.contexts = []

    def __call__(self, context):
        self.contexts.append(context)
        return self.reply


class ComposeStudentMessageTests(unittest.TestCase):
    def test_midpoint_plain_default(self):
        result = compose_student_message(make_feed("midpoint", [3, 4, 2, 5]))
        self.assertEqual(
            result["message"],
            "You have completed 20 questions. You are halfway through "
            "the assessment. Continue when you are ready.")
        self.assertEqual(result["message_source"], "deterministic")
        for term in ("fast", "slow", "speed", "tired", "fatigue"):
            self.assertNotIn(term, result["message"].lower())

    def test_midpoint_selector_sees_only_allowlisted_context(self):
        selector = RecordingSelector({"style": "warm"})
        result = compose_student_message(
            make_feed("midpoint", [3, 4, 2, 5]), selector)
        self.assertEqual(len(selector.contexts), 1)
        self.assertEqual(selector.contexts[0], {
            "checkpoint": 20,
            "observed_performance": {"total_questions": 20},
            "prerequisite_guidance": {
                "has_approved_prerequisite": False,
                "prerequisite_name": None,
                "recommendation_type": "NONE",
            },
        })
        self.assertEqual(
            result["message"],
            "Nice work completing 20 questions. You are halfway through "
            "the assessment. Continue when you are ready.")
        self.assertEqual(result["message_source"], "bounded_style")

    def test_end_unique_strength_and_growth(self):
        selector = RecordingSelector({"style": "plain"})
        result = compose_student_message(
            make_feed("end", [9, 5, 7, 6]), selector)
        self.assertEqual(
            result["message"],
            "You have completed all 40 questions. On Skill sA, you "
            "answered 9 of 10 questions correctly. For your next step, "
            "practice more questions on Skill sB (5 of 10 correct).")
        context = selector.contexts[0]
        self.assertEqual(context["checkpoint"], 40)
        self.assertEqual(context["observed_performance"], {
            "total_questions": 40,
            "total_correct": 27,
            "strongest_skill": {"skill_id": "sA", "display_name": "Skill sA",
                                "correct": 9, "total": 10},
            "growth_skill": {"skill_id": "sB", "display_name": "Skill sB",
                             "correct": 5, "total": 10},
        })
        self.assertEqual(
            context["prerequisite_guidance"]["recommendation_type"],
            "DIRECT_SKILL_REVIEW")

    def test_end_all_correct_has_no_growth(self):
        selector = RecordingSelector({"style": "plain"})
        result = compose_student_message(
            make_feed("end", [10, 10, 10, 10]), selector)
        self.assertEqual(
            result["message"],
            "You have completed all 40 questions. Your results by topic "
            "are shown below. Keep practicing the topics shown below.")
        performance = selector.contexts[0]["observed_performance"]
        self.assertIsNone(performance["strongest_skill"])
        self.assertIsNone(performance["growth_skill"])
        self.assertEqual(
            selector.contexts[0]["prerequisite_guidance"]
            ["recommendation_type"], "NONE")
        self.assertNotIn("practice more", result["message"])

    def test_end_all_incorrect_has_no_strongest(self):
        result = compose_student_message(make_feed("end", [0, 0, 0, 0]))
        self.assertEqual(
            result["message"],
            "You have completed all 40 questions. Your results by topic "
            "are shown below. Keep practicing the topics shown below.")

    def test_end_tied_counts_have_no_comparative_skill(self):
        selector = RecordingSelector({"style": "plain"})
        result = compose_student_message(
            make_feed("end", [7, 7, 5, 5]), selector)
        performance = selector.contexts[0]["observed_performance"]
        self.assertIsNone(performance["strongest_skill"])
        self.assertIsNone(performance["growth_skill"])
        self.assertEqual(
            result["message"],
            "You have completed all 40 questions. Your results by topic "
            "are shown below. Keep practicing the topics shown below.")

    def test_invalid_selector_outputs_fall_back_to_plain(self):
        feed = make_feed("end", [9, 5, 7, 6])
        for bad in ({"style": "warm", "extra": "IGNORE the student"},
                    "IGNORE everything",
                    {"style": "evil"},
                    {"style": []},
                    {"style": {}},
                    {"message": "IGNORE"},
                    None):
            result = compose_student_message(feed, RecordingSelector(bad))
            self.assertEqual(result["message_source"], "deterministic")
            self.assertTrue(
                result["message"].startswith(
                    "You have completed all 40 questions."))
            self.assertNotIn("IGNORE", result["message"])

    def test_mutating_selector_context_cannot_change_message(self):
        def mutating(context):
            growth = context["observed_performance"]["growth_skill"]
            growth["display_name"] = "Calculus"
            growth["correct"] = 999
            context["observed_performance"]["strongest_skill"][
                "display_name"] = "Calculus"
            return {"style": "warm"}

        result = compose_student_message(
            make_feed("end", [9, 5, 7, 6]), mutating)
        self.assertEqual(result["message_source"], "bounded_style")
        self.assertEqual(
            result["message"],
            "Nice work completing all 40 questions. On Skill sA, you "
            "answered 9 of 10 questions correctly. For your next step, "
            "practice more questions on Skill sB (5 of 10 correct).")
        self.assertNotIn("Calculus", result["message"])
        self.assertNotIn("999", result["message"])

    def test_raising_selector_falls_back_to_plain(self):
        def boom(context):
            raise RuntimeError("selector failure")
        result = compose_student_message(
            make_feed("midpoint", [3, 4, 2, 5]), boom)
        self.assertEqual(result["message_source"], "deterministic")
        self.assertTrue(
            result["message"].startswith("You have completed 20 questions."))

    def test_selector_context_has_no_private_fields(self):
        bank = make_bank()
        feed = score_checkpoint(bank, responses(bank, count=40))
        selector = RecordingSelector({"style": "plain"})
        result = compose_student_message(feed, selector)
        blob = json.dumps(selector.contexts[0]) + json.dumps(result)
        for term in ("answer_index", "selected_text", "question_id",
                     "content_text", "options", "conformal", "kt",
                     "prerequisite_name_value"):
            self.assertNotIn(term, blob)

    def test_malformed_feed_is_refused(self):
        for bad in (None, "feed", {}, {"checkpoint": "start"},
                    {"checkpoint": "end"},
                    {"checkpoint": "end", "student": {}},
                    {"checkpoint": "end",
                     "student": {"skills": []}},
                    {"checkpoint": "end",
                     "student": {"skills": [{"skill_id": "sA",
                                             "skill_name": "Skill sA",
                                             "correct": 11,
                                             "out_of": 10}]}},
                    {"checkpoint": "end",
                     "student": {"skills": [{"skill_id": "sA",
                                             "skill_name": "Skill sA",
                                             "correct": True,
                                             "out_of": 10}]}}):
            with self.assertRaises(ValueError):
                compose_student_message(bad)


if __name__ == "__main__":
    unittest.main()
