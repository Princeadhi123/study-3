"""Tests for test_feed.build_test_feed."""
import json
import unittest

from test_feed import build_test_feed

SKILLS = ["geom", "algebra", "fractions", "prob"]
NAMES = {
    "geom": "Geometry",
    "algebra": "Algebra",
    "fractions": "Fractions",
    "prob": "Probability",
}
PER_SKILL = 5


def make_half(correct_map, skills=SKILLS, id_prefix="h1"):
    rows = []
    for skill_id in skills:
        n_correct = correct_map.get(skill_id, 3)
        for j in range(PER_SKILL):
            rows.append(
                {
                    "question_id": f"{id_prefix}-{skill_id}-{j}",
                    "skill_id": skill_id,
                    "correct": j < n_correct,
                }
            )
    return rows


def make_midpoint(correct_map=None):
    return make_half(correct_map or {})


def make_end(first_map=None, second_map=None):
    return make_half(first_map or {}) + make_half(
        second_map or {}, id_prefix="h2"
    )


class BalancedFeedTests(unittest.TestCase):
    def test_balanced_midpoint(self):
        answers = make_midpoint({"geom": 5, "algebra": 2})
        feed = build_test_feed(answers, NAMES)
        self.assertEqual(feed["checkpoint"], "midpoint")
        self.assertEqual(feed["questions_answered"], 20)
        self.assertEqual(feed["suggestions"], [])
        self.assertEqual(
            feed["evidence"]["source"], "observed_answers_only"
        )
        self.assertEqual(feed["evidence"]["model_status"], "not_evaluated")
        student_skills = feed["student"]["skills"]
        self.assertEqual(
            [s["skill_id"] for s in student_skills], SKILLS
        )
        self.assertEqual(student_skills[0]["skill_name"], "Geometry")
        self.assertEqual(student_skills[0]["correct"], 5)
        self.assertEqual(student_skills[0]["out_of"], 5)
        self.assertEqual(student_skills[1]["correct"], 2)
        self.assertTrue(student_skills[0]["text"])
        teacher_skills = feed["teacher"]["skills"]
        self.assertEqual(
            [s["skill_id"] for s in teacher_skills], SKILLS
        )
        self.assertEqual(
            teacher_skills[0]["first_half"],
            {"correct": 5, "out_of": 5},
        )
        self.assertEqual(
            teacher_skills[0]["total"], {"correct": 5, "out_of": 5}
        )
        self.assertEqual(
            feed["teacher"]["total"], {"correct": 13, "out_of": 20}
        )

    def test_balanced_end_different_half_scores(self):
        answers = make_end(
            {"geom": 5, "algebra": 4, "fractions": 4, "prob": 4},
            {"geom": 1, "algebra": 2, "fractions": 3, "prob": 4},
        )
        feed = build_test_feed(answers, NAMES)
        self.assertEqual(feed["checkpoint"], "end")
        self.assertEqual(feed["questions_answered"], 40)
        teacher_skills = feed["teacher"]["skills"]
        self.assertEqual(
            teacher_skills[0]["first_half"],
            {"correct": 5, "out_of": 5},
        )
        self.assertEqual(
            teacher_skills[0]["second_half"],
            {"correct": 1, "out_of": 5},
        )
        self.assertEqual(
            teacher_skills[0]["total"], {"correct": 6, "out_of": 10}
        )
        self.assertEqual(
            teacher_skills[1]["total"], {"correct": 6, "out_of": 10}
        )
        self.assertEqual(
            feed["teacher"]["total"], {"correct": 27, "out_of": 40}
        )
        student_skills = feed["student"]["skills"]
        self.assertEqual(student_skills[0]["correct"], 6)
        self.assertEqual(student_skills[0]["out_of"], 10)

    def test_interleaved_balanced_order(self):
        correct_map = {"geom": 5, "algebra": 4, "fractions": 2, "prob": 3}
        seen = {skill_id: 0 for skill_id in SKILLS}
        answers = []
        for j in range(PER_SKILL):
            for skill_id in SKILLS:
                seen[skill_id] += 1
                answers.append(
                    {
                        "question_id": f"q{j}-{skill_id}",
                        "skill_id": skill_id,
                        "correct": seen[skill_id] <= correct_map[skill_id],
                    }
                )
        feed = build_test_feed(answers, NAMES)
        self.assertEqual(feed["checkpoint"], "midpoint")
        self.assertEqual(
            [s["skill_id"] for s in feed["student"]["skills"]], SKILLS
        )
        for entry in feed["student"]["skills"]:
            self.assertEqual(
                entry["correct"], correct_map[entry["skill_id"]]
            )
            self.assertEqual(entry["out_of"], 5)

    def test_json_serializable(self):
        for answers in (make_midpoint(), make_end()):
            feed = build_test_feed(answers, NAMES)
            self.assertEqual(json.loads(json.dumps(feed)), feed)

    def test_midpoint_carries_no_second_half_data(self):
        feed = build_test_feed(make_midpoint(), NAMES)
        for entry in feed["teacher"]["skills"]:
            self.assertNotIn("second_half", entry)
        self.assertNotIn("second_half", json.dumps(feed))
        self.assertNotIn("second half", feed["student"]["summary"])

    def test_midpoint_teacher_total_reflects_first_half_only(self):
        feed = build_test_feed(
            make_midpoint({"geom": 4, "algebra": 3, "fractions": 2, "prob": 1}),
            NAMES,
        )
        self.assertEqual(
            feed["teacher"]["total"], {"correct": 10, "out_of": 20}
        )


class ValidationTests(unittest.TestCase):
    def test_rejects_wrong_lengths(self):
        for length in (19, 21, 39, 41):
            rows = make_end()
            answers = (rows + rows)[0:length] if length > 40 else rows[:length]
            self.assertEqual(len(answers), length)
            with self.subTest(length=length):
                with self.assertRaises(ValueError):
                    build_test_feed(answers, NAMES)

    def test_rejects_unbalanced_first_half(self):
        answers = make_midpoint()
        answers[0]["skill_id"] = "algebra"
        with self.assertRaises(ValueError):
            build_test_feed(answers, NAMES)

    def test_rejects_unbalanced_second_half(self):
        answers = make_end()
        answers[20]["skill_id"] = "algebra"
        with self.assertRaises(ValueError):
            build_test_feed(answers, NAMES)

    def test_rejects_fifth_skill_in_first_half(self):
        answers = make_midpoint()
        answers[0]["skill_id"] = "extra_skill"
        with self.assertRaises(ValueError):
            build_test_feed(answers, {**NAMES, "extra_skill": "Extra"})

    def test_rejects_changed_second_half_skills(self):
        answers = make_half({}) + make_half(
            {}, skills=["geom", "algebra", "fractions", "trig"],
            id_prefix="h2",
        )
        with self.assertRaises(ValueError):
            build_test_feed(answers, NAMES)

    def test_rejects_duplicate_question_ids_across_halves(self):
        answers = make_end()
        answers[25]["question_id"] = answers[0]["question_id"]
        with self.assertRaises(ValueError):
            build_test_feed(answers, NAMES)

    def test_rejects_blank_or_whitespace_ids(self):
        for field in ("question_id", "skill_id"):
            for bad in ("", "   ", "\t "):
                answers = make_midpoint()
                answers[4][field] = bad
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValueError):
                        build_test_feed(answers, NAMES)

    def test_rejects_nonbool_correct(self):
        for bad in (1, 0, "true", None):
            answers = make_midpoint()
            answers[7]["correct"] = bad
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    build_test_feed(answers, NAMES)

    def test_rejects_missing_or_empty_skill_name(self):
        answers = make_midpoint()
        with self.assertRaises(ValueError):
            build_test_feed(
                answers, {k: v for k, v in NAMES.items() if k != "prob"}
            )
        with self.assertRaises(ValueError):
            build_test_feed(answers, {**NAMES, "prob": ""})

    def test_rejects_nonlist_answers(self):
        with self.assertRaises(ValueError):
            build_test_feed("not a list", NAMES)

    def test_rejects_row_missing_required_keys(self):
        answers = make_midpoint()
        del answers[3]["skill_id"]
        with self.assertRaises(ValueError):
            build_test_feed(answers, NAMES)


if __name__ == "__main__":
    unittest.main()
