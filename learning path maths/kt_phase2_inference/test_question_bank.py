"""Tests for question_bank.build_bank over a synthetic gzipped source."""
import csv
import gzip
import json
import random
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from question_bank import build_bank

SKILLS = ["s1", "s2", "s3", "s4"]
QUESTION_KEYS = {"question_id", "skill_id", "item_id", "exercise_id",
                 "text", "options", "answer_index", "content_text"}


def options_payload(correct_at=1):
    options = [{"value": "distractor", "correct": False},
               {"value": "the answer", "correct": True}]
    if correct_at == 0:
        options.reverse()
    return json.dumps(options)


def make_row(skill, template, variant=0, **overrides):
    row = {
        "skill_id": skill,
        "skill_name": f"Skill {skill}",
        "item_id": f"item-{skill}-{template}",
        "item_instance_id": f"inst-{skill}-{template}-v{variant}",
        "exercise_id": f"ex-{skill}-{template}",
        "exercise_family": "mcq",
        "correctness_available": "1",
        "has_options": "True",
        "text": f"Which option fits template {template} of {skill}? (v{variant})",
        "options_json": options_payload(),
        "correct_option_index": "1",
        "correct_option_value": "the answer",
        "student_id": f"stu-{variant}",
        "selected_option": "distractor",
    }
    row.update(overrides)
    return row


def make_rows(templates_per_skill=12, skills=SKILLS, duplicate_templates=()):
    rows = []
    for skill in skills:
        for template in range(templates_per_skill):
            rows.append(make_row(skill, template))
            if template in duplicate_templates:
                rows.append(make_row(skill, template, variant=1))
    return rows


def write_source(path, rows, drop_columns=()):
    columns = [c for c in rows[0] if c not in drop_columns]
    with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_catalog(path, skills=SKILLS, extra_rows=()):
    with open(path, "w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["skill_id", "skill_name", "vocab_idx"])
        writer.writeheader()
        for index, skill in enumerate(skills):
            writer.writerow({"skill_id": skill,
                             "skill_name": f"Skill {skill}",
                             "vocab_idx": str(index)})
        writer.writerows(extra_rows)


class BuildBankTests(unittest.TestCase):
    def build(self, rows, skills=None, catalog_skills=SKILLS,
              drop_columns=()):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source, catalog = tmp / "source.csv.gz", tmp / "catalog.csv"
            write_source(source, rows, drop_columns)
            write_catalog(catalog, catalog_skills)
            return build_bank(source, catalog, skills)

    def test_builds_bank(self):
        bank = self.build(make_rows())
        self.assertEqual(bank["protocol"], "offline_mcq_demo_only")
        self.assertEqual(len(bank["questions"]), 40)
        self.assertEqual(list(bank["skill_names"]), SKILLS)
        self.assertEqual(
            bank["eligible_templates_by_skill"], {s: 12 for s in SKILLS})

    def test_one_question_per_exercise_template(self):
        bank = self.build(make_rows(duplicate_templates={1, 4, 9}))
        pairs = Counter(
            (q["skill_id"], q["exercise_id"]) for q in bank["questions"])
        self.assertEqual(len(bank["questions"]), 40)
        self.assertTrue(all(count == 1 for count in pairs.values()))

    def test_round_robin_skill_counts(self):
        bank = self.build(make_rows())
        first, questions = bank["questions"][:20], bank["questions"]
        self.assertEqual(Counter(q["skill_id"] for q in first),
                         {s: 5 for s in SKILLS})
        self.assertEqual(Counter(q["skill_id"] for q in questions),
                         {s: 10 for s in SKILLS})
        self.assertEqual([q["skill_id"] for q in first],
                         [SKILLS[i % 4] for i in range(20)])

    def test_unique_question_ids_and_content(self):
        bank = self.build(make_rows())
        self.assertEqual(
            len({q["question_id"] for q in bank["questions"]}), 40)
        self.assertEqual(
            len({q["content_text"] for q in bank["questions"]}), 40)

    def test_bank_omits_student_ids_and_observed_answers(self):
        bank = self.build(make_rows())
        blob = json.dumps(bank)
        for question in bank["questions"]:
            self.assertEqual(set(question), QUESTION_KEYS)
        self.assertNotIn("student_id", blob)
        self.assertNotIn("selected_option", blob)

    def test_row_order_independence(self):
        rows = make_rows(duplicate_templates={0, 3, 7})
        shuffled = rows[:]
        random.Random(7).shuffle(shuffled)
        self.assertEqual(self.build(rows), self.build(shuffled))

    def test_skips_invalid_option_key_rows(self):
        bad_option_variants = [
            {"options_json": "not json"},
            {"options_json": json.dumps(
                [{"value": "only one", "correct": True}])},
            {"options_json": json.dumps(
                [{"value": "a", "correct": True},
                 {"value": "b", "correct": True}]),
             "correct_option_index": "0", "correct_option_value": "a"},
            {"options_json": json.dumps(
                [{"value": "a", "correct": True},
                 {"value": "a", "correct": False}]),
             "correct_option_index": "0", "correct_option_value": "a"},
            {"correct_option_index": "0"},
            {"correct_option_value": "something else"},
        ]
        for overrides in bad_option_variants:
            rows = make_rows(10)
            rows.append(make_row("s1", 99, **overrides))
            with self.subTest(overrides=overrides):
                bank = self.build(rows)
                self.assertEqual(
                    bank["eligible_templates_by_skill"]["s1"], 10)
                self.assertNotIn(
                    "ex-s1-99",
                    {q["exercise_id"] for q in bank["questions"]})

    def test_skips_unavailable_or_placeholder_rows(self):
        rows = make_rows(10)
        rows += [
            make_row("s1", 90, correctness_available="0"),
            make_row("s1", 91, text="Exercise: untitled placeholder"),
            make_row("s1", 92, text="short"),
            make_row("s1", 93, exercise_family="open"),
            make_row("s1", 94, has_options="False"),
            make_row("s1", 95, item_instance_id=""),
        ]
        bank = self.build(rows)
        self.assertEqual(bank["eligible_templates_by_skill"]["s1"], 10)

    def test_insufficient_templates_rejected(self):
        rows = [r for r in make_rows(10)
                if r["exercise_id"] != "ex-s4-9"]
        with self.assertRaises(ValueError):
            self.build(rows)

    def test_auto_selects_top_four_by_template_count(self):
        rows = make_rows(12) + [make_row("s5", t) for t in range(11)]
        bank = self.build(rows, catalog_skills=SKILLS + ["s5"])
        self.assertEqual(list(bank["skill_names"]), SKILLS)

    def test_explicit_skills_order(self):
        order = ["s3", "s1", "s4", "s2"]
        bank = self.build(make_rows(), skills=order)
        self.assertEqual(list(bank["skill_names"]), order)
        self.assertEqual(
            [q["skill_id"] for q in bank["questions"][:20]],
            [order[i % 4] for i in range(20)])

    def test_explicit_skills_rejected(self):
        rows = make_rows()
        for bad in (["s1", "s2", "s3"], ["s1", "s1", "s2", "s3"],
                    ["s1", "s2", "s3", "unknown"], ["s1", "s2", "s3", "s4", "s5"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.build(rows, skills=bad)

    def test_missing_required_column_rejected(self):
        with self.assertRaises(ValueError):
            self.build(make_rows(), drop_columns={"options_json"})


if __name__ == "__main__":
    unittest.main()
