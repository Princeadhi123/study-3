PRACTICE_CONTEXT_SCHEMA = "phase3_feedback_practice_context_v1"
FEEDBACK_BUNDLE_VERSION = "phase3_feedback_practice_bundle_v1"
INTEGRATED_TEMPLATE_VERSION = "phase3_student_feedback_template_v2"
PRACTICE_CONTEXT_KEYS = {
    "schema", "status", "selected_question_id", "pool_sha256"}
PRACTICE_TERMINAL_STATUSES = {
    "selected", "abstained", "unavailable", "disabled"}
PRACTICE_STATUS_MESSAGES = {
    "pending": "Practice selection is still running. The combined draft will be ready when it finishes.",
    "selected": "This practice activity accompanies the feedback.",
    "abstained": "No eligible practice activity was selected. No outside-band activity has been substituted.",
    "unavailable": "Practice selection is unavailable. No replacement activity has been invented.",
    "disabled": "KT practice selection is not enabled for this session.",
}
PRACTICE_SELECTION_NOTICE = (
    "Synthetic demo activity only. This selection does not establish mastery "
    "or a validated optimal learning difficulty.")
PRACTICE_NEXT_STEP_SELECTED = (
    "A suggested practice activity accompanies this feedback: {concept_name} "
    "({skill_name}). Use it as a follow-up, not as a judgement of mastery.")
PRACTICE_NEXT_STEP_ABSTAINED = (
    "No practice activity was selected for this assessment. Ask your teacher "
    "to choose a suitable follow-up if needed.")
PRACTICE_NEXT_STEP_UNAVAILABLE = (
    "No practice activity is attached because practice selection is "
    "unavailable. Ask your teacher to choose a suitable follow-up.")


import copy
import math

from evidence_feedback import canonical_digest
from research_runtime import (
    load_current_practice_pool, teacher_content_context)
from session_store import bank_fingerprint
from shadow_practice import eligibility, validate_target_band


def empty_context(status):
    if status not in PRACTICE_TERMINAL_STATUSES - {"selected"}:
        raise ValueError("invalid empty practice context status")
    return {"schema": PRACTICE_CONTEXT_SCHEMA, "status": status,
            "selected_question_id": None, "pool_sha256": None}


def _graph_row(question):
    rows = [e for e in teacher_content_context("demo")["exercises"]
            if e["source_id"] == "practice_pool_v2"
            and e["role"] == "practice"
            and e["question_id"] == question["question_id"]]
    if (len(rows) != 1 or rows[0]["skill_id"] != question["skill_id"]
            or rows[0]["text"] != question["text"]
            or rows[0]["options"] != question["options"]):
        raise ValueError("practice graph row does not match the pool")
    return rows[0]


def validate_practice_context(context, evidence):
    if (not isinstance(context, dict)
            or set(context) != PRACTICE_CONTEXT_KEYS
            or context["schema"] != PRACTICE_CONTEXT_SCHEMA
            or context["status"] not in PRACTICE_TERMINAL_STATUSES):
        raise ValueError("invalid practice context")
    status = context["status"]
    if status != "selected":
        if (context["selected_question_id"] is not None
                or context["pool_sha256"] is not None):
            raise ValueError("terminal context must not carry selection")
        return copy.deepcopy(context)
    qid = context["selected_question_id"]
    if not isinstance(qid, str) or not qid.strip():
        raise ValueError("selected practice context requires a question id")
    pool = load_current_practice_pool()
    if context["pool_sha256"] != canonical_digest(pool):
        raise ValueError("practice context pool hash mismatch")
    matches = [q for q in pool["questions"] if q["question_id"] == qid]
    if len(matches) != 1:
        raise ValueError("selected practice question is not in the pool")
    question = matches[0]
    skills = {s["skill_id"]: s for s in evidence["skills"]}
    skill = skills.get(question["skill_id"])
    if skill is None or not skill["incorrect"]:
        raise ValueError("selected practice skill lacks observed errors")
    _graph_row(question)
    return copy.deepcopy(context)


def project_practice_context(result, bank, rows, evidence, recommender):
    status = (result or {}).get("status")
    if status == "disabled":
        return empty_context("disabled"), None
    if status in ("abstained", "unavailable"):
        return empty_context(status), None
    if status != "selected":
        return empty_context("unavailable"), "practice_context_validation_failed"
    try:
        if (result.get("schema")
                != "phase3_shadow_practice_recommendation_v1"):
            raise ValueError
        if result["bank_sha256"] != bank_fingerprint(bank):
            raise ValueError
        if result["response_sha256"] != canonical_digest(rows):
            raise ValueError
        pool = load_current_practice_pool()
        if result["pool_sha256"] != canonical_digest(pool):
            raise ValueError
        count = result["answer_count"]
        if (not isinstance(count, int) or isinstance(count, bool)
                or count != 40):
            raise ValueError
        selected = result["selected_question_id"]
        raw_predictions = result["candidate_predictions"]
        if not isinstance(raw_predictions, list):
            raise ValueError
        predictions = {}
        for row in raw_predictions:
            if not isinstance(row, dict) or row.get("question_id") in predictions:
                raise ValueError
            predictions[row["question_id"]] = row
        questions = pool["questions"]
        if not isinstance(questions, list):
            raise ValueError
        matches = [q for q in questions if q["question_id"] == selected]
        if len(matches) != 1:
            raise ValueError
        question = matches[0]
        candidates, _, _ = eligibility(bank, evidence, pool)
        if all(q["question_id"] != selected for q in candidates):
            raise ValueError
        prediction = predictions.get(selected)
        if prediction is None:
            raise ValueError
        if (prediction["skill_id"] != question["skill_id"]
                or prediction["item_id"] != question["item_id"]
                or prediction["regime"] not in ("warm", "cold")):
            raise ValueError
        p = prediction["p_correct"]
        if (isinstance(p, bool) or not isinstance(p, (int, float))
                or not math.isfinite(p) or not 0 <= p <= 1):
            raise ValueError
        band = validate_target_band(result["target_band"])
        if not band[0] <= p <= band[1]:
            raise ValueError
        if (recommender is not None and hasattr(recommender, "band")
                and tuple(result["target_band"]) != tuple(recommender.band)):
            raise ValueError
        context = {"schema": PRACTICE_CONTEXT_SCHEMA,
                   "status": "selected",
                   "selected_question_id": selected,
                   "pool_sha256": canonical_digest(pool)}
        validate_practice_context(context, evidence)
        return context, None
    except Exception:
        return (empty_context("unavailable"),
                "practice_context_validation_failed")


def resolve_practice(context, evidence):
    status = context["status"]
    card = {"status": status, "message": PRACTICE_STATUS_MESSAGES[status],
            "notice": PRACTICE_SELECTION_NOTICE, "question": None}
    if status != "selected":
        return {"card": card, "selection": None}
    validate_practice_context(context, evidence)
    pool = load_current_practice_pool()
    question = next(q for q in pool["questions"]
                    if q["question_id"] == context["selected_question_id"])
    skill = next(s for s in evidence["skills"]
                 if s["skill_id"] == question["skill_id"])
    row = _graph_row(question)
    card["question"] = {"skill_name": skill["skill_name"],
                        "concept_name": row["concept_label"],
                        "text": question["text"],
                        "options": copy.deepcopy(question["options"])}
    return {"card": card,
            "selection": {"skill_name": skill["skill_name"],
                          "concept_name": row["concept_label"],
                          "mathematical_task": row["mathematical_task"]}}


def contextual_message(evidence, audience, candidate, context,
                       opening=None):
    from evidence_feedback_policy import (
        build_candidates, build_feedback_plan, render_message)
    from full_feedback import structured_sections
    message = render_message(evidence, audience, "end", candidate,
                             opening)
    sections = structured_sections({
        "message": message,
        "selected_candidate_id": candidate["candidate_id"],
        "candidates": build_candidates(evidence, "end"),
        "feedback_plan": build_feedback_plan(candidate)})
    status = context["status"]
    extra = None
    if status == "selected":
        resolved = resolve_practice(context, evidence)
        selection = resolved["selection"]
        extra = PRACTICE_NEXT_STEP_SELECTED.format(
            concept_name=selection["concept_name"],
            skill_name=selection["skill_name"])
    elif status == "abstained":
        extra = PRACTICE_NEXT_STEP_ABSTAINED
    elif status == "unavailable":
        extra = PRACTICE_NEXT_STEP_UNAVAILABLE
    if extra:
        last = next(s for s in sections if s["kind"] == "next_steps")
        last["text"] = (
            f"{last['text']}\n\n{extra}" if last["text"] else extra)
    return {"sections": sections,
            "text": "\n\n".join(
                s["text"] for s in sections if s["text"])}
