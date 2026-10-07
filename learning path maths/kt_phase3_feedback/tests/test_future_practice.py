"""Query encoding and explicit heuristic checks; not a learner evaluation."""
import copy
import json
import unittest

import torch

from evidence_feedback import build_evidence, run_feedback
from future_kt import predict_future_candidates
from shadow_practice import ShadowPracticeRecommender, validate_practice_pool, validate_target_band
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_kt_adapter import FakeKT


def question(name="future_a", skill="sA"):
    text = f"Unanswered practice {name}?"
    return {
        "question_id": name, "skill_id": skill, "item_id": name,
        "text": text, "options": ["alpha", "beta", "gamma"],
        "content_text": text + " [OPTIONS] alpha | beta | gamma",
        "answer_index": 0,
    }


def pool(questions):
    return {"schema": "phase3_shadow_practice_pool_v1",
            "scope": "research_only_not_learner_approved", "questions": questions}


class RecordingKT(FakeKT):
    def __init__(self, bank, candidates):
        source = copy.deepcopy(bank)
        source["questions"].extend(candidates)
        super().__init__(source)
        self.config = {"variant": "skill_item_content_option", "max_seq_len": 400, "seed": 42}
        self.batches = []
        self.output = None

    def probs(self, batch):
        self.batches.append({k: v.clone() for k, v in batch.items()})
        if self.output is not None:
            return self.output
        p = 0.4 + 0.05 * (int(batch["item"][0, -1]) % 4)
        return torch.full((1, batch["skill"].shape[1]), p)


class FutureQueryTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_bank()
        self.candidates = [question("future_a"), question("future_b")]
        self.kt = RecordingKT(self.bank, self.candidates)
        self.rows = responses(self.bank)

    def predict(self, candidates=None):
        return predict_future_candidates(self.bank, self.rows,
                                         self.candidates if candidates is None else candidates, self.kt)

    def test_each_candidate_has_same_forty_answer_prefix(self):
        result = self.predict()
        self.assertEqual(len(result["predictions"]), 2)
        for key in self.kt.batches[0]:
            self.assertTrue(torch.equal(self.kt.batches[0][key][:, :40],
                                        self.kt.batches[1][key][:, :40]), key)
        self.assertEqual(tuple(self.kt.batches[0]["skill"].shape), (1, 41))
        self.assertTrue(torch.equal(self.kt.batches[0]["correct"][0, :40], torch.ones(40)))
        self.assertEqual(result["provenance"]["query_position"], 40)
        self.assertIn("not_applied", result["provenance"]["conformal"])

    def test_unanswered_slots_are_fixed_and_independent_of_key(self):
        before = self.predict()
        batch_before = self.kt.batches[-1]
        changed = copy.deepcopy(self.candidates)
        for q in changed:
            q["answer_index"] = 2
            q["selected_index"] = 2
            q["correct"] = True
        after = self.predict(changed)
        self.assertEqual(before, after)
        for key in batch_before:
            self.assertTrue(torch.equal(batch_before[key], self.kt.batches[-1][key]), key)
        for key in ("correct", "selected_text_idx", "rt", "rt_mask", "time_bin_ids"):
            self.assertEqual(float(batch_before[key][0, -1]), 0)
        self.assertEqual(float(batch_before["attempt"][0, -1]), 1)

    def test_candidate_order_does_not_change_predictions(self):
        before = {r["question_id"]: r["p_correct"] for r in self.predict()["predictions"]}
        after = {r["question_id"]: r["p_correct"] for r in self.predict(self.candidates[::-1])["predictions"]}
        self.assertEqual(before, after)

    def test_last_observed_answer_is_retained(self):
        self.rows[-1]["selected_index"] = (self.bank["questions"][-1]["answer_index"] + 1) % 3
        self.predict()
        self.assertEqual(float(self.kt.batches[-1]["correct"][0, 39]), 0)
        selected = self.bank["questions"][-1]["options"][self.rows[-1]["selected_index"]]
        self.assertEqual(int(self.kt.batches[-1]["selected_text_idx"][0, 39]),
                         self.kt.dataset.text_to_row[selected])

    def test_unknown_item_is_cold_not_missing_skill_fallback(self):
        del self.kt.item_vocab["future_a"]
        result = self.predict()
        self.assertEqual(result["predictions"][0]["regime"], "cold")
        self.assertEqual(int(self.kt.batches[0]["item"][0, -1]), 1)
        self.candidates[0]["skill_id"] = "unrecognized"
        with self.assertRaisesRegex(ValueError, "skill/content"):
            self.predict()

    def test_missing_content_or_history_option_is_rejected(self):
        del self.kt.dataset.text_to_row[self.candidates[0]["content_text"]]
        with self.assertRaisesRegex(ValueError, "skill/content"):
            self.predict()
        self.kt = RecordingKT(self.bank, self.candidates)
        del self.kt.dataset.text_to_row["alpha"]
        with self.assertRaisesRegex(ValueError, "history"):
            self.predict()

    def test_incomplete_history_and_wrong_model_length_are_rejected(self):
        self.rows = self.rows[:20]
        with self.assertRaisesRegex(ValueError, "40 completed"):
            self.predict()
        self.rows = responses(self.bank)
        self.kt.config["max_seq_len"] = 40
        with self.assertRaisesRegex(ValueError, "sequence length"):
            self.predict()
        self.kt.config["max_seq_len"] = 400
        self.kt.config["variant"] = "skill_only"
        with self.assertRaisesRegex(ValueError, "variant D"):
            self.predict()

    def test_bad_shape_and_nonfinite_probability_are_rejected(self):
        self.kt.output = torch.ones((1, 40))
        with self.assertRaisesRegex(ValueError, "shape"):
            self.predict()
        self.kt.output = torch.full((1, 41), float("nan"))
        with self.assertRaisesRegex(ValueError, "probability"):
            self.predict()


class ShadowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.rows = responses(self.bank, correct=False)
        self.candidates = [question("future_a"), question("future_b")]
        self.calls = []
        self.probabilities = {"future_a": 0.5, "future_b": 0.6}

    def predictor(self, bank, rows, candidates):
        self.calls.append(copy.deepcopy(candidates))
        return {"predictions": [
            {"question_id": q["question_id"], "skill_id": q["skill_id"],
             "item_id": q["item_id"], "regime": "warm",
             "p_correct": self.probabilities[q["question_id"]]} for q in candidates]}

    def recommend(self, candidates=None, band=(0.4, 0.8)):
        recommender = ShadowPracticeRecommender(
            pool(self.candidates if candidates is None else candidates), band, self.predictor)
        return recommender.recommend(self.bank, self.taxonomy, self.rows)

    def test_observed_error_baseline_and_probability_choice_are_distinct(self):
        result = self.recommend()
        self.assertEqual(result["baseline_question_id"], "future_a")
        self.assertEqual(result["selected_question_id"], "future_b")
        self.assertFalse(result["used_for_feedback"])
        self.assertFalse(result["used_for_student_advice"])
        self.assertEqual(result["conformal"], "not_applied")
        for field in ("answer_index", "selected_index", "options"):
            self.assertNotIn(field, json.dumps(result))

    def test_inclusive_band_and_stable_tie(self):
        self.probabilities = {"future_a": 0.25, "future_b": 0.75}
        result = self.recommend(band=(0.25, 0.75))
        self.assertEqual(result["in_band_question_ids"], ["future_a", "future_b"])
        self.assertEqual(result["selected_question_id"], "future_a")

    def test_no_in_band_choice_abstains_without_forcing_baseline(self):
        self.probabilities = {"future_a": 0.95, "future_b": 0.1}
        result = self.recommend()
        self.assertEqual(result["status"], "abstained")
        self.assertIsNone(result["selected_question_id"])
        self.assertEqual(result["baseline_question_id"], "future_a")

    def test_all_correct_and_empty_pool_need_no_predictor(self):
        self.rows = responses(self.bank)
        self.assertEqual(self.recommend()["status"], "abstained")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.recommend(candidates=[])["status"], "abstained")

    def test_repeated_assessment_ids_and_prompts_are_excluded(self):
        original = copy.deepcopy(self.bank["questions"][0])
        original = {k: original[k] for k in self.candidates[0]}
        alias = copy.deepcopy(original)
        alias["question_id"] = "new_id"
        alias["text"] = "  " + alias["text"].upper() + "  "
        alias["content_text"] = alias["text"] + " [OPTIONS] " + " | ".join(alias["options"])
        for candidate in (original, alias):
            result = self.recommend(candidates=[candidate])
            self.assertEqual(result["status"], "abstained")
            self.assertEqual(result["excluded_candidates"][0]["reason"],
                             "already_assessed_question_or_prompt")
        self.assertEqual(self.calls, [])

    def test_unassessed_and_no_error_topics_excluded(self):
        for q in self.bank["questions"]:
            if q["skill_id"] == "sB":
                self.rows[self.bank["questions"].index(q)]["selected_index"] = q["answer_index"]
        result = self.recommend(candidates=[question("future_b", "sB"), question("outside", "sX")])
        self.assertEqual(result["status"], "abstained")
        self.assertEqual({e["reason"] for e in result["excluded_candidates"]},
                         {"topic_not_assessed", "no_observed_errors_in_topic"})
        self.assertEqual(self.calls, [])

    def test_model_failure_and_bad_probabilities_are_unavailable(self):
        for bad in (float("nan"), True, -0.1, 1.1):
            self.probabilities["future_a"] = bad
            result = self.recommend()
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["selected_question_id"])

    def test_recommendation_does_not_mutate_feedback_inputs(self):
        evidence = build_evidence(self.bank, self.taxonomy, self.rows)
        before = run_feedback(evidence, "student", "end")
        originals = copy.deepcopy((self.bank, self.taxonomy, self.rows, self.candidates))
        self.recommend()
        self.assertEqual(originals, (self.bank, self.taxonomy, self.rows, self.candidates))
        self.assertEqual(before, run_feedback(evidence, "student", "end"))

    def test_invalid_band_and_pool_rejected(self):
        for band in (None, (0.5, 0.5), (0.8, 0.4), (True, 1), (0.4, float("nan")), (-0.1, 0.8)):
            with self.assertRaises(ValueError):
                validate_target_band(band)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_practice_pool(pool([question(), question()]))
        invalid = question()
        invalid["p_correct"] = 0.6
        with self.assertRaisesRegex(ValueError, "fields"):
            validate_practice_pool(pool([invalid]))


if __name__ == "__main__":
    unittest.main()
