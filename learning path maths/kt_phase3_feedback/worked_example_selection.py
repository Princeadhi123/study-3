"""Experimental historical KT ranking, private to educator review."""
import copy
import math

POLICY = "checked_lowest_pre_answer_kt_v1"
CONTEXT_KEY = "worked_example_selection"

def freeze_selection(bank, responses, diagnostics):
    from evidence_feedback import canonical_digest
    result = diagnostics if isinstance(diagnostics, dict) else {}
    kt = result.get("kt") if isinstance(result.get("kt"), dict) else {}
    coverage = kt.get("coverage") if isinstance(kt.get("coverage"), dict) else {}
    probabilities = kt.get("p_correct_before_each_answer")
    valid = (
        result.get("status") == "ready"
        and result.get("checkpoint") == "end"
        and result.get("answer_count") == len(responses) == 40
        and kt.get("kind") == "frozen_variant_d_response_trace"
        and coverage.get("question_count") == 40
        and all(coverage.get(k) == 0 for k in (
            "missing_skill_count", "missing_content_count", "missing_option_count"))
        and isinstance(probabilities, list)
        and len(probabilities) == 40
        and all(type(p) in (int, float) and math.isfinite(p) and 0 <= p <= 1
                for p in probabilities))
    return {
        "policy": POLICY,
        "assessment_sha256": canonical_digest({"bank": bank, "responses": responses}),
        "probabilities": copy.deepcopy(probabilities) if valid else None,
        "score_source": "historical_pre_answer_trace" if valid else "unavailable_or_incompatible_trace",
        "overrides": {}}

def validate_selection(selection, bank, responses):
    from evidence_feedback import canonical_digest
    if (not isinstance(selection, dict)
            or set(selection) != {"policy", "assessment_sha256", "probabilities", "score_source", "overrides"}
            or selection["policy"] != POLICY
            or selection["assessment_sha256"] != canonical_digest({"bank": bank, "responses": responses})
            or not isinstance(selection["overrides"], dict)
            or any(not isinstance(k, str) or not isinstance(v, str)
                   for k, v in selection["overrides"].items())):
        raise ValueError("incompatible worked-example selection snapshot")
    probabilities = selection["probabilities"]
    if probabilities is None:
        if selection["score_source"] != "unavailable_or_incompatible_trace":
            raise ValueError("incompatible worked-example score source")
    elif (selection["score_source"] != "historical_pre_answer_trace"
          or not isinstance(probabilities, list)
          or len(probabilities) != len(responses) or len(responses) != 40
          or any(type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1
                 for p in probabilities)):
        raise ValueError("invalid worked-example probabilities")

def _incorrect_entries(bank, responses, focus, qids, probabilities):
    import worked_review_math
    from assessment_worked_review import UNSOLVED_ERRORS

    entries = []
    for index, (question, row) in enumerate(zip(bank["questions"], responses)):
        if (question["question_id"] not in qids
                or question["skill_id"] != focus["skill_id"]
                or row["selected_index"] == question["answer_index"]):
            continue
        try:
            solved = worked_review_math.solution(question, focus["subtopic_id"])
        except UNSOLVED_ERRORS:
            solved = None
        entries.append({"question": question, "row": row, "solution": solved,
                        "position": index + 1,
                        "p_correct_before_answer": probabilities[index] if probabilities is not None else None})
    return entries


def _selection_reason(use_kt, checked):
    if use_kt:
        return (
            "Recommended because this answer was incorrect and had the lowest "
            "pre-answer KT probability among candidates with a checked solution "
            "in this subtopic.")
    if checked:
        return (
            "KT scores are unavailable or incompatible. Recommended the earliest "
            "incorrect question with a checked solution in this subtopic.")
    return (
        "No checked worked solution is available in this subtopic. Retained the "
        "earliest incorrect question for teacher review.")


def select_question(bank, responses, candidate, qids, selection):
    validate_selection(selection, bank, responses)
    focus = candidate["focus"]
    probabilities = selection["probabilities"]
    entries = _incorrect_entries(bank, responses, focus, qids, probabilities)
    if not entries:
        raise ValueError("no incorrect assessment question matches selected focus")
    eligible = [entry for entry in entries if entry["solution"] is not None] or entries
    # When no checked solution exists, retain a question for explicit teacher review.
    checked = any(entry["solution"] is not None for entry in entries)
    use_kt = checked and probabilities is not None
    recommended = min(eligible, key=lambda e: (
        e["p_correct_before_answer"] if use_kt else 0,
        e["position"], e["question"]["question_id"]))
    override = selection["overrides"].get(candidate["candidate_id"])
    selected = recommended
    source = "kt_recommendation" if use_kt else "earliest_fallback"
    if override is not None:
        selected = next((entry for entry in entries if entry["question"]["question_id"] == override), None)
        if selected is None:
            raise ValueError("worked-example override must be an incorrect question in the selected subtopic")
        source = "teacher_override"
    reason = _selection_reason(use_kt, checked)
    provenance = {
        "policy": POLICY, "score_source": selection["score_source"],
        "source": source,
        "reason": reason, "recommended_question_id": recommended["question"]["question_id"],
        "selected_question_id": selected["question"]["question_id"],
        "p_correct_before_answer": selected["p_correct_before_answer"],
        "candidates": [{"question_id": entry["question"]["question_id"],
                        "prompt": entry["question"]["text"], "position": entry["position"],
                        "selected_answer": entry["question"]["options"][entry["row"]["selected_index"]],
                        "correct_answer": entry["question"]["options"][entry["question"]["answer_index"]],
                        "checked_solution_available": entry["solution"] is not None,
                        "p_correct_before_answer": entry["p_correct_before_answer"]}
                       for entry in entries]}
    return selected["question"], selected["row"], selected["solution"], provenance
