"""Opt-in private practice selection; an experimental heuristic, not ZPD/mastery."""
import copy
import math
from fractions import Fraction

from evidence_feedback import build_evidence, build_research_evidence, canonical_digest


POOL_SCHEMA = "phase3_shadow_practice_pool_v1"
POOL_SCOPE = "research_only_not_learner_approved"
POLICY_VERSION = "observed_error_topic_then_explicit_probability_band_v1"
QUESTION_KEYS = {"question_id", "skill_id", "item_id", "text", "options", "content_text"}


def normalized_prompt(text):
    return " ".join(text.split()).casefold()


def validate_target_band(band):
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        raise ValueError("explicit target band must contain lower and upper")
    for value in band:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("target band values must be finite numbers")
    lower, upper = band
    if not 0 <= lower < upper <= 1:
        raise ValueError("target band must satisfy 0 <= lower < upper <= 1")
    return float(lower), float(upper)


def validate_practice_pool(pool):
    if (not isinstance(pool, dict) or set(pool) != {"schema", "scope", "questions"}
            or pool["schema"] != POOL_SCHEMA or pool["scope"] != POOL_SCOPE
            or not isinstance(pool["questions"], list)):
        raise ValueError("explicit private shadow practice pool required")
    seen_ids, seen_prompts = set(), set()
    for q in pool["questions"]:
        if (not isinstance(q, dict) or not QUESTION_KEYS <= set(q)
                or set(q) - QUESTION_KEYS - {"answer_index"}):
            raise ValueError("invalid practice question fields")
        for key in ("question_id", "skill_id", "item_id", "text", "content_text"):
            if not isinstance(q[key], str) or not q[key].strip():
                raise ValueError("practice question identifiers/text must be nonempty")
        options = q["options"]
        if (not isinstance(options, list) or len(options) < 2
                or any(not isinstance(o, str) or not o.strip() for o in options)
                or len(set(options)) != len(options)):
            raise ValueError("practice options must be distinct nonempty strings")
        if q["content_text"] != q["text"] + " [OPTIONS] " + " | ".join(options):
            raise ValueError("practice content must match ordered options")
        if "answer_index" in q and (type(q["answer_index"]) is not int
                                    or q["answer_index"] not in range(len(options))):
            raise ValueError("invalid private practice answer index")
        prompt = normalized_prompt(q["text"])
        if q["question_id"] in seen_ids or prompt in seen_prompts:
            raise ValueError("duplicate practice question or normalized prompt")
        seen_ids.add(q["question_id"])
        seen_prompts.add(prompt)
    return copy.deepcopy(pool)


def eligibility(bank, evidence, pool):
    skills = {s["skill_id"]: s for s in evidence["skills"]}
    answered_ids = {q["question_id"] for q in bank["questions"]}
    answered_prompts = {normalized_prompt(q["text"]) for q in bank["questions"]}
    candidates, exclusions = [], []
    for q in pool["questions"]:
        skill = skills.get(q["skill_id"])
        if q["question_id"] in answered_ids or normalized_prompt(q["text"]) in answered_prompts:
            reason = "already_assessed_question_or_prompt"
        elif skill is None:
            reason = "topic_not_assessed"
        elif not skill["incorrect"]:
            reason = "no_observed_errors_in_topic"
        else:
            candidates.append(q)
            continue
        exclusions.append({"question_id": q["question_id"], "reason": reason})
    return candidates, exclusions, skills


def select_predictions(candidates, prediction_result, skills, band):
    predictions = prediction_result["predictions"]
    if not isinstance(predictions, list) or len(predictions) != len(candidates):
        raise ValueError("future predictions must match eligible candidates")
    by_id = {}
    questions = {q["question_id"]: q for q in candidates}
    for row in predictions:
        if not isinstance(row, dict) or set(row) != {"question_id", "skill_id", "item_id", "regime", "p_correct"}:
            raise ValueError("invalid future prediction fields")
        q = questions.get(row["question_id"])
        if q is None or row["question_id"] in by_id:
            raise ValueError("unknown or duplicate future prediction")
        if row["skill_id"] != q["skill_id"] or row["item_id"] != q["item_id"] or row["regime"] not in ("warm", "cold"):
            raise ValueError("future prediction identity mismatch")
        p = row["p_correct"]
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError("invalid future probability")
        by_id[row["question_id"]] = row

    def priority(q):
        skill = skills[q["skill_id"]]
        return (-skill["incorrect"], -Fraction(skill["incorrect"], skill["out_of"]), q["question_id"])

    baseline = min(candidates, key=priority)["question_id"] if candidates else None
    lower, upper = band
    midpoint = (lower + upper) / 2
    qualified = [q for q in candidates if lower <= by_id[q["question_id"]]["p_correct"] <= upper]
    selected = min(qualified, key=lambda q: (
        priority(q)[:2], abs(by_id[q["question_id"]]["p_correct"] - midpoint), q["question_id"]
    )) if qualified else None
    return {
        "selected_question_id": selected["question_id"] if selected else None,
        "baseline_question_id": baseline,
        "candidate_predictions": [copy.deepcopy(by_id[q["question_id"]]) for q in candidates],
        "in_band_question_ids": [q["question_id"] for q in qualified],
        "outside_band_question_ids": [q["question_id"] for q in candidates if q not in qualified],
    }


ALIGNED_PRACTICE_POLICY_VERSION = "one_in_band_question_per_selected_skill_v1"


def select_aligned_predictions(candidates, prediction_result, skills, band,
                               review_skill_ids):
    if (not isinstance(review_skill_ids, (list, tuple))
            or len(review_skill_ids) > 2
            or len(set(review_skill_ids)) != len(review_skill_ids)
            or any(sid not in skills or not skills[sid]["incorrect"]
                   for sid in review_skill_ids)):
        raise ValueError("practice requires up to two distinct observed-error skills")
    if any(q["skill_id"] not in review_skill_ids for q in candidates):
        raise ValueError("practice candidates must follow selected review skills")
    checked = select_predictions(candidates, prediction_result, skills, band)
    predictions = {p["question_id"]: p
                   for p in checked["candidate_predictions"]}
    selected_ids, slots = [], []
    for sid in review_skill_ids:
        group = [q for q in candidates if q["skill_id"] == sid]
        result = select_predictions(
            group, {"predictions": [predictions[q["question_id"]]
                                    for q in group]}, skills, band)
        qid = result["selected_question_id"]
        if qid is not None:
            selected_ids.append(qid)
        slots.append({
            "skill_id": sid,
            "status": "selected" if qid is not None else "abstained",
            "reason": (None if qid is not None else
                       "no_candidate_in_explicit_target_band" if group else
                       "no_eligible_unattempted_error_topic_candidates"),
            "selected_question_id": qid,
            "baseline_question_id": result["baseline_question_id"]})
    return {
        "selected_question_ids": selected_ids,
        "selected_question_id": selected_ids[0] if selected_ids else None,
        "baseline_question_id": checked["baseline_question_id"],
        "topic_selections": slots,
        "candidate_predictions": checked["candidate_predictions"],
        "in_band_question_ids": checked["in_band_question_ids"],
        "outside_band_question_ids": checked["outside_band_question_ids"]}


class ShadowPracticeRecommender:
    def __init__(self, pool, target_band, predictor):
        self.pool = validate_practice_pool(pool)
        if any(q["item_id"].startswith("synthetic_divisibility_")
               for q in self.pool["questions"]):
            # A v2 run must opt into its embeddings even if topic eligibility
            # later excludes the four synthetic questions from prediction.
            from research_runtime import (
                require_augmented_embeddings, validate_current_practice_pool)
            validate_current_practice_pool(self.pool)
            require_augmented_embeddings()
        self.band = validate_target_band(target_band)
        self.predictor = predictor
        self.pool_sha256 = canonical_digest(self.pool)

    def recommend(self, bank, taxonomy, responses):
        if len(responses) != 40:
            raise ValueError("recommendations require completed assessment")
        evidence = build_evidence(bank, taxonomy, responses)
        return self._recommend(bank, responses, evidence)

    def recommend_research(self, bank, taxonomy, responses):
        """Offline synthetic research only; leaves bank approval metadata untouched."""
        evidence = build_research_evidence(bank, taxonomy, responses)
        return self._recommend(bank, responses, evidence)

    def recommend_for_reviews(self, bank, taxonomy, responses,
                              review_skill_ids, research=False):
        if len(responses) != 40:
            raise ValueError("recommendations require completed assessment")
        evidence = (build_research_evidence if research
                    else build_evidence)(bank, taxonomy, responses)
        return self._aligned_recommend(bank, responses, evidence,
                                       review_skill_ids)

    def _aligned_recommend(self, bank, responses, evidence,
                           review_skill_ids):
        candidates, exclusions, skills = eligibility(
            bank, evidence, self.pool)
        if (not isinstance(review_skill_ids, (list, tuple))
                or len(review_skill_ids) > 2
                or len(set(review_skill_ids)) != len(review_skill_ids)
                or any(sid not in skills or not skills[sid]["incorrect"]
                       for sid in review_skill_ids)):
            raise ValueError(
                "practice requires up to two distinct observed-error skills")
        review_skill_ids = list(review_skill_ids)
        aligned = [q for q in candidates
                   if q["skill_id"] in review_skill_ids]
        exclusions = exclusions + [
            {"question_id": q["question_id"],
             "reason": "review_topic_not_selected"}
            for q in candidates if q["skill_id"] not in review_skill_ids]
        base = {
            "schema": "phase3_shadow_practice_recommendation_v1",
            "policy_version": ALIGNED_PRACTICE_POLICY_VERSION,
            "mode": "shadow_only", "review_skill_ids": review_skill_ids,
            "used_for_student_advice": False, "used_for_feedback": False,
            "answer_count": 40, "pool_sha256": self.pool_sha256,
            "bank_sha256": evidence["bank_sha256"],
            "response_sha256": canonical_digest(responses),
            "target_band": list(self.band),
            "excluded_candidates": exclusions,
            "selected_question_id": None,
            "selected_question_ids": [],
            "baseline_question_id": None,
            "topic_selections": [],
            "conformal": "not_applied",
            "claim_boundary": "experimental_probability_band_not_validated_ZPD_mastery_or_learning_gain",
        }
        if not review_skill_ids:
            return {**base, "status": "abstained",
                    "reason": "no_selected_review_skills",
                    "candidate_predictions": [],
                    "in_band_question_ids": [],
                    "outside_band_question_ids": []}
        if not aligned:
            return {**base, "status": "abstained",
                    "reason": "no_eligible_unattempted_error_topic_candidates",
                    "topic_selections": [
                        {"skill_id": sid, "status": "abstained",
                         "reason": "no_eligible_unattempted_error_topic_candidates",
                         "selected_question_id": None,
                         "baseline_question_id": None}
                        for sid in review_skill_ids],
                    "candidate_predictions": [],
                    "in_band_question_ids": [],
                    "outside_band_question_ids": []}
        try:
            predicted = self.predictor(
                copy.deepcopy(bank), copy.deepcopy(responses),
                copy.deepcopy(aligned))
            selection = select_aligned_predictions(
                aligned, predicted, skills, self.band, review_skill_ids)
        except Exception:
            return {**base, "status": "unavailable",
                    "reason": "future_prediction_failed",
                    "topic_selections": [
                        {"skill_id": sid, "status": "unavailable",
                         "reason": "future_prediction_failed",
                         "selected_question_id": None,
                         "baseline_question_id": None}
                        for sid in review_skill_ids],
                    "candidate_predictions": [],
                    "in_band_question_ids": [],
                    "outside_band_question_ids": []}
        status = ("selected" if selection["selected_question_ids"]
                  else "abstained")
        return {
            **base, **selection,
            "status": status,
            "reason": (None if status == "selected"
                       else "no_candidate_in_explicit_target_band"),
            "kt_provenance": copy.deepcopy(predicted.get("provenance", {})),
        }

    def _recommend(self, bank, responses, evidence):
        candidates, exclusions, skills = eligibility(bank, evidence, self.pool)
        base = {
            "schema": "phase3_shadow_practice_recommendation_v1",
            "policy_version": POLICY_VERSION, "mode": "shadow_only",
            "used_for_student_advice": False, "used_for_feedback": False,
            "answer_count": 40, "pool_sha256": self.pool_sha256,
            "bank_sha256": evidence["bank_sha256"],
            "response_sha256": canonical_digest(responses),
            "target_band": list(self.band), "excluded_candidates": exclusions,
            "selected_question_id": None, "baseline_question_id": None,
            "conformal": "not_applied",
            "claim_boundary": "experimental_probability_band_not_validated_ZPD_mastery_or_learning_gain",
        }
        if not candidates:
            return {**base, "status": "abstained", "reason": "no_eligible_unattempted_error_topic_candidates",
                    "candidate_predictions": [], "in_band_question_ids": [], "outside_band_question_ids": []}
        try:
            predicted = self.predictor(copy.deepcopy(bank), copy.deepcopy(responses), copy.deepcopy(candidates))
            selection = select_predictions(candidates, predicted, skills, self.band)
        except Exception:
            return {**base, "status": "unavailable", "reason": "future_prediction_failed"}
        return {
            **base, **selection,
            "status": "selected" if selection["selected_question_id"] else "abstained",
            "reason": None if selection["selected_question_id"] else "no_candidate_in_explicit_target_band",
            "kt_provenance": copy.deepcopy(predicted.get("provenance", {})),
        }
