"""Tests for mcq_test.student_questions and mcq_test.score_checkpoint."""
import copy
import json
import unittest
from collections import Counter

from mcq_test import score_checkpoint, student_questions

SKILLS = ["sA", "sB", "sC", "sD"]
PER_HALF = 5


def make_bank():
    questions = []
    for half in (0, 1):
        for slot in range(PER_HALF):
            for skill in SKILLS:
                index = half * PER_HALF + slot
                questions.append(
                    {
                        "question_id": f"{skill}__q{index}",
                        "skill_id": skill,
                        "item_id": f"item-{skill}-{index}",
                        "exercise_id": f"ex-{skill}-{index}",
                        "text": f"Prompt {skill} {index}?",
                        "options": ["alpha", "beta", "gamma"],
                        "answer_index": index % 3,
                        "content_text": (
                            f"Prompt {skill} {index}? "
                            "[OPTIONS] alpha | beta | gamma"
                        ),
                    }
                )
    return {
        "protocol": "offline_mcq_demo_only",
        "review_status": "approved",
        "selection": "synthetic fixture",
        "skill_names": {s: f"Skill {s}" for s in SKILLS},
        "eligible_templates_by_skill": {s: 10 for s in SKILLS},
        "questions": questions,
    }


def correct_responses(bank, count=40):
    return [
        {"question_id": q["question_id"], "selected_index": q["answer_index"]}
        for q in bank["questions"][:count]
    ]


def incorrect_responses(bank, count=40):
    return [
        {
            "question_id": q["question_id"],
            "selected_index": (q["answer_index"] + 1) % len(q["options"]),
        }
        for q in bank["questions"][:count]
    ]


class StudentQuestionTests(unittest.TestCase):
    def test_half_one_and_two_payloads(self):
        bank = make_bank()
        for half, expected_ids in (
            (1, [q["question_id"] for q in bank["questions"][:20]]),
            (2, [q["question_id"] for q in bank["questions"][20:]]),
        ):
            served = student_questions(bank, half)
            self.assertEqual([q["question_id"] for q in served], expected_ids)
            self.assertEqual(len(served), 20)
            for question in served:
                self.assertEqual(
                    set(question),
                    {"question_id", "skill_id", "text", "options"})
                self.assertEqual(
                    Counter(q["skill_id"] for q in served),
                    {s: 5 for s in SKILLS})

    def test_no_key_or_private_fields_in_payload(self):
        blob = json.dumps(student_questions(make_bank(), 1))
        for leaked in ("answer_index", "content_text", "item_id",
                       "exercise_id", "correct"):
            self.assertNotIn(leaked, blob)

    def test_options_copied_not_aliased(self):
        bank = make_bank()
        served = student_questions(bank, 1)
        served[0]["options"][0] = "mutated"
        served[0]["text"] = "mutated"
        self.assertEqual(bank["questions"][0]["options"][0], "alpha")
        self.assertNotEqual(bank["questions"][0]["text"], "mutated")

    def test_rejects_bad_half(self):
        bank = make_bank()
        for half in (0, 3, True, False, "1", 1.0, None):
            with self.subTest(half=half):
                with self.assertRaises(ValueError):
                    student_questions(bank, half)


class ScoreCheckpointTests(unittest.TestCase):
    def test_repeated_exercise_id_with_different_text_accepted(self):
        bank = make_bank()
        bank["questions"][20]["exercise_id"] = (
            bank["questions"][0]["exercise_id"])
        bank["questions"][39]["exercise_id"] = (
            bank["questions"][3]["exercise_id"])
        self.assertEqual(len(student_questions(bank, 1)), 20)
        self.assertEqual(len(student_questions(bank, 2)), 20)
        feed = score_checkpoint(bank, correct_responses(bank, 40))
        self.assertEqual(feed["teacher"]["total"],
                         {"correct": 40, "out_of": 40})

    def test_midpoint_and_end_all_correct(self):
        bank = make_bank()
        mid = score_checkpoint(bank, correct_responses(bank, 20))
        self.assertEqual(mid["checkpoint"], "midpoint")
        self.assertEqual(mid["questions_answered"], 20)
        for entry in mid["student"]["skills"]:
            self.assertEqual((entry["correct"], entry["out_of"]), (5, 5))
        end = score_checkpoint(bank, correct_responses(bank, 40))
        self.assertEqual(end["checkpoint"], "end")
        self.assertEqual(end["questions_answered"], 40)
        for entry in end["student"]["skills"]:
            self.assertEqual((entry["correct"], entry["out_of"]), (10, 10))
        self.assertEqual(
            end["teacher"]["total"], {"correct": 40, "out_of": 40})

    def test_midpoint_and_end_all_incorrect(self):
        bank = make_bank()
        mid = score_checkpoint(bank, incorrect_responses(bank, 20))
        for entry in mid["student"]["skills"]:
            self.assertEqual((entry["correct"], entry["out_of"]), (0, 5))
        end = score_checkpoint(bank, incorrect_responses(bank, 40))
        for entry in end["student"]["skills"]:
            self.assertEqual((entry["correct"], entry["out_of"]), (0, 10))
        self.assertEqual(
            end["teacher"]["total"], {"correct": 0, "out_of": 40})

    def test_mixed_choices_match_private_key(self):
        bank = make_bank()
        responses = []
        expected = 0
        for index, question in enumerate(bank["questions"]):
            hit = index % 3 == 0
            expected += hit
            selected = (question["answer_index"] if hit
                        else (question["answer_index"] + 1)
                        % len(question["options"]))
            responses.append({"question_id": question["question_id"],
                              "selected_index": selected})
        feed = score_checkpoint(bank, responses)
        self.assertEqual(feed["teacher"]["total"],
                         {"correct": expected, "out_of": 40})
        totals = {s["skill_id"]: s["total"]["correct"]
                  for s in feed["teacher"]["skills"]}
        self.assertEqual(sum(totals.values()), expected)
        for skill_id in SKILLS:
            self.assertIn(totals[skill_id], range(11))

    def test_output_is_only_the_feed(self):
        feed = score_checkpoint(make_bank(), correct_responses(make_bank(), 40))
        self.assertEqual(
            set(feed),
            {"checkpoint", "questions_answered", "student", "teacher",
             "evidence", "suggestions"})
        blob = json.dumps(feed)
        for leaked in ("answer_index", "options", "selected_index",
                       "selected_text", "content_text", "item_id",
                       "exercise_id"):
            self.assertNotIn(leaked, blob)

    def test_does_not_mutate_bank(self):
        bank = make_bank()
        snapshot = copy.deepcopy(bank)
        score_checkpoint(bank, correct_responses(bank, 40))
        student_questions(bank, 1)
        self.assertEqual(bank, snapshot)


class ApprovalAndProtocolTests(unittest.TestCase):
    def test_rejects_unreviewed_or_missing_review_status(self):
        for status in ("unreviewed; may depend on missing images",
                       "unreviewed", "pending", "", None):
            bank = {**make_bank(), "review_status": status}
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    student_questions(bank, 1)
                with self.assertRaises(ValueError):
                    score_checkpoint(bank, correct_responses(bank, 20))
        bank = make_bank()
        del bank["review_status"]
        with self.assertRaises(ValueError):
            student_questions(bank, 1)

    def test_rejects_non_mcq_protocol(self):
        for protocol in ("other", "live_bank", "", None):
            bank = {**make_bank(), "protocol": protocol}
            with self.subTest(protocol=protocol):
                with self.assertRaises(ValueError):
                    student_questions(bank, 1)
                with self.assertRaises(ValueError):
                    score_checkpoint(bank, correct_responses(bank, 20))


class MalformedBankTests(unittest.TestCase):
    def test_rejects_structural_defects(self):
        defects = []
        short = make_bank()
        short["questions"] = short["questions"][:39]
        defects.append(short)
        dup = make_bank()
        dup["questions"][5]["question_id"] = dup["questions"][0]["question_id"]
        defects.append(dup)
        dup_prompt = make_bank()
        dup_prompt["questions"][20]["text"] = (
            "  " + dup_prompt["questions"][0]["text"].upper() + " ")
        defects.append(dup_prompt)
        dup_prompt_opts = make_bank()
        dup_prompt_opts["questions"][21]["text"] = (
            dup_prompt_opts["questions"][1]["text"])
        dup_prompt_opts["questions"][21]["options"] = ["x", "y", "z"]
        dup_prompt_opts["questions"][21]["answer_index"] = 1
        defects.append(dup_prompt_opts)
        blank_text = make_bank()
        blank_text["questions"][0]["text"] = "   "
        defects.append(blank_text)
        one_option = make_bank()
        one_option["questions"][0]["options"] = ["only"]
        defects.append(one_option)
        dup_option = make_bank()
        dup_option["questions"][0]["options"] = ["same", "same", "third"]
        defects.append(dup_option)
        bool_index = make_bank()
        bool_index["questions"][0]["answer_index"] = True
        defects.append(bool_index)
        range_index = make_bank()
        range_index["questions"][0]["answer_index"] = 3
        defects.append(range_index)
        blank_name = make_bank()
        blank_name["skill_names"]["sA"] = " "
        defects.append(blank_name)
        missing_name = make_bank()
        missing_name["questions"][0]["skill_id"] = "no_such_skill"
        defects.append(missing_name)
        unbalanced = make_bank()
        unbalanced["questions"][0]["skill_id"] = "sB"
        defects.append(unbalanced)
        mismatched = make_bank()
        mismatched["questions"][20]["skill_id"] = "sE"
        mismatched["skill_names"]["sE"] = "Skill sE"
        defects.append(mismatched)
        for bank in defects:
            with self.subTest(bank=bank["questions"][0]["question_id"]):
                with self.assertRaises(ValueError):
                    student_questions(bank, 1)
        with self.assertRaises(ValueError):
            student_questions(["not", "a", "dict"], 1)


class MalformedResponseTests(unittest.TestCase):
    def test_rejects_bad_response_prefixes(self):
        bank = make_bank()
        good = correct_responses(bank, 20)
        cases = [
            good[:19],
            correct_responses(bank, 40) + [
                {"question_id": "extra", "selected_index": 0}],
            "not a list",
            [{**good[0], "extra": 1}, *good[1:]],
            [{k: v for k, v in good[0].items() if k != "selected_index"},
             *good[1:]],
            [{**good[0], "question_id": "bogus"}, *good[1:]],
            [good[1], good[0], *good[2:]],
            [{**good[0], "selected_index": True}, *good[1:]],
            [{**good[0], "selected_index": "0"}, *good[1:]],
            [{**good[0], "selected_index": 3}, *good[1:]],
            [{**good[0], "selected_index": -1}, *good[1:]],
            ["not a dict", *good[1:]],
        ]
        for responses in cases:
            with self.subTest(responses=str(responses)[:80]):
                with self.assertRaises(ValueError):
                    score_checkpoint(bank, responses)


if __name__ == "__main__":
    unittest.main()
