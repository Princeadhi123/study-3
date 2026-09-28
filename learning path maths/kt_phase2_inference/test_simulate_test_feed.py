"""Tests for simulate_test_feed.simulate over a synthetic bank fixture."""
import json
import unittest
from collections import Counter

from simulate_test_feed import simulate

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
        "selection": "synthetic fixture",
        "skill_names": {s: f"Skill {s}" for s in SKILLS},
        "eligible_templates_by_skill": {s: 10 for s in SKILLS},
        "questions": questions,
    }


class ScriptedSimulationTests(unittest.TestCase):
    def test_scripted_half_and_full_counts(self):
        demo = simulate(make_bank())
        self.assertEqual(demo["protocol"], "offline_scripted_demo")
        midpoint, end = demo["midpoint"], demo["end"]
        self.assertEqual(midpoint["checkpoint"], "midpoint")
        self.assertEqual(midpoint["questions_answered"], 20)
        self.assertEqual(end["checkpoint"], "end")
        self.assertEqual(end["questions_answered"], 40)
        for entry in midpoint["student"]["skills"]:
            self.assertEqual((entry["correct"], entry["out_of"]), (3, 5))
        for entry in end["student"]["skills"]:
            self.assertEqual((entry["correct"], entry["out_of"]), (5, 10))
        self.assertEqual(
            end["teacher"]["total"], {"correct": 20, "out_of": 40})

    def test_no_answer_key_in_feed_output(self):
        demo = simulate(make_bank())
        for feed in (demo["midpoint"], demo["end"]):
            blob = json.dumps(feed)
            for leaked in ("answer_index", "options", "item_id",
                           "exercise_id", "content_text", "_selected_text"):
                self.assertNotIn(leaked, blob)
        self.assertEqual(len(demo["_selected_text"]), 40)
        self.assertIn(
            demo["_selected_text"][0],
            {"alpha", "beta", "gamma"},
        )

    def test_stable_interleaving_and_determinism(self):
        bank = make_bank()
        first_skills = [q["skill_id"] for q in bank["questions"][:4]]
        self.assertEqual(len(set(first_skills)), 4)
        demo1, demo2 = simulate(bank), simulate(bank)
        self.assertEqual(demo1, demo2)
        self.assertEqual(
            [s["skill_id"] for s in demo1["midpoint"]["student"]["skills"]],
            SKILLS)
        self.assertEqual(
            Counter(q["skill_id"] for q in bank["questions"][:20]),
            {s: 5 for s in SKILLS})

    def test_supplied_responses_all_correct(self):
        bank = make_bank()
        responses = {
            q["question_id"]: q["answer_index"]
            for q in bank["questions"]
        }
        demo = simulate(bank, responses)
        self.assertEqual(demo["protocol"], "offline_supplied_responses")
        for entry in demo["midpoint"]["student"]["skills"]:
            self.assertEqual((entry["correct"], entry["out_of"]), (5, 5))
        for entry in demo["end"]["student"]["skills"]:
            self.assertEqual((entry["correct"], entry["out_of"]), (10, 10))

    def test_illustrative_kt_batch_and_missing_embedding(self):
        import torch
        from types import SimpleNamespace
        from unittest.mock import patch
        from simulate_test_feed import illustrative_kt_trace

        questions = [{**q, "correct": i % 2 == 0}
                     for i, q in enumerate(make_bank()["questions"])]
        selected = [q["options"][q["answer_index"]] for q in questions]
        texts = {text: i for i, text in enumerate(
            dict.fromkeys([q["content_text"] for q in questions] + selected))}

        class FakeKT:
            dataset = SimpleNamespace(text_to_row=texts)
            skill_vocab = {s: i + 2 for i, s in enumerate(SKILLS)}
            item_vocab = {q["item_id"]: i + 2 for i, q in enumerate(questions)}

            def probs(self, batch):
                assert tuple(batch["skill"].shape) == (1, 40)
                assert batch["correct"][0].tolist() == [float(i % 2 == 0)
                                                         for i in range(40)]
                assert batch["selected_text_idx"][0].tolist() == [texts[s] for s in selected]
                return torch.full((1, 40), 0.5)

        with patch("frozen_model.load_frozen_model", return_value=FakeKT()):
            trace = illustrative_kt_trace(questions, selected, "cpu")
            self.assertEqual(trace["p_correct_before_each_scripted_answer"], [0.5] * 40)
            self.assertTrue(trace["not_calibrated_or_validated_for_the_20_40_feed"])
            with self.assertRaisesRegex(ValueError, "embedding table"):
                illustrative_kt_trace(questions, ["missing", *selected[1:]], "cpu")


class MalformedInputTests(unittest.TestCase):
    def test_rejects_malformed_responses(self):
        bank = make_bank()
        qids = [q["question_id"] for q in bank["questions"]]
        good = {qid: 0 for qid in qids}
        cases = [
            {qid: 0 for qid in qids[:-1]},
            {**good, "bogus-id": 0},
            {**good, qids[0]: 3},
            {**good, qids[0]: -1},
            {**good, qids[0]: True},
            {**good, qids[0]: "0"},
        ]
        for responses in cases:
            with self.subTest(responses=responses):
                with self.assertRaises(ValueError):
                    simulate(bank, responses)
        with self.assertRaises(ValueError):
            simulate(bank, ["not", "a", "dict"])

    def test_rejects_malformed_bank(self):
        with self.assertRaises(ValueError):
            simulate({**make_bank(), "protocol": "other"})
        with self.assertRaises(ValueError):
            simulate({**make_bank(),
                      "questions": make_bank()["questions"][:39]})
        duplicate = make_bank()
        duplicate["questions"][5]["question_id"] = (
            duplicate["questions"][0]["question_id"])
        with self.assertRaises(ValueError):
            simulate(duplicate)
        bad_index = make_bank()
        bad_index["questions"][0]["answer_index"] = True
        with self.assertRaises(ValueError):
            simulate(bad_index)
        out_of_range = make_bank()
        out_of_range["questions"][0]["answer_index"] = 3
        with self.assertRaises(ValueError):
            simulate(out_of_range)
        short_options = make_bank()
        short_options["questions"][0]["options"] = ["only"]
        with self.assertRaises(ValueError):
            simulate(short_options)
        unknown_skill = make_bank()
        unknown_skill["questions"][0]["skill_id"] = "not_a_skill"
        with self.assertRaises(ValueError):
            simulate(unknown_skill)


if __name__ == "__main__":
    unittest.main()
