PRACTICE_CONTEXT_SCHEMA = "phase3_feedback_practice_context_v1"
FEEDBACK_BUNDLE_VERSION = "phase3_feedback_practice_bundle_v1"
INTEGRATED_TEMPLATE_VERSION = "phase3_student_feedback_template_v3"
MULTI_INTEGRATED_TEMPLATE_VERSION = "phase3_student_feedback_template_v4"
PRACTICE_ALIGNED_INSTRUCTION = (
    "Choose an option and explain your reasoning.")
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
    "If your teacher approves, a separate practice activity is proposed on "
    "{concept_name}. Review and practice are separate suggestions; the "
    "practice may cover different content.")
PRACTICE_RELATIONSHIP_MESSAGES = {
    "same_content":
        "This practice activity covers the same assessed content as the "
        "review focus. Review and practice are separate activities.",
    "different_content":
        "This is separate practice on {concept_name}, another area where "
        "this assessment recorded incorrect answers. It does not practise "
        "the review focus, {subtopic_name}.",
    "same_topic_different_content":
        "This practice activity is in the same broad topic as the review "
        "focus, but covers {concept_name} rather than {subtopic_name}.",
    "no_review_focus":
        "This practice activity is separate from the review suggestion.",
}
REVIEW_EXAMPLE_NOTICE = (
    "Discuss this example with your teacher. It is not the selected "
    "practice question.")
REVIEW_EXAMPLES = {
    "percentage_amount": {
        "prompt": "Find 30% of 90.",
        "explanation": "The whole is 90. Write 30% as 30/100 = 0.30, then "
                       "multiply: 0.30 × 90 = 27."},
    "divisibility": {
        "prompt": "Is 18 divisible by 6?",
        "explanation": "18 ÷ 6 = 3 with no remainder, so 6 is a divisor of "
                       "18. Check: 6 × 3 = 18."},
    "prime_recognition": {
        "prompt": "Which of 13 and 15 is prime?",
        "explanation": "13 has only the positive divisors 1 and 13, so it "
                       "is prime. 15 = 3 × 5, so 15 is not prime."},
    "combine_like_terms": {
        "prompt": "Simplify 7a − 2a.",
        "explanation": "Both terms have the variable part a. Subtract the "
                       "coefficients and keep a: (7 − 2)a = 5a."},
    "retain_unlike_terms": {
        "prompt": "Simplify 2a + 5b.",
        "explanation": "The variable parts a and b differ, so these are "
                       "unlike terms. They cannot be combined into one "
                       "term; the expression remains 2a + 5b."},
    "fraction_product": {
        "prompt": "Calculate 2/3 × 3/5.",
        "explanation": "Multiply the numerators and denominators: "
                       "(2 × 3)/(3 × 5) = 6/15 = 2/5."},
    "fraction_of_quantity": {
        "prompt": "Find 3/4 of 12.",
        "explanation": "The whole is 12. Multiply by the fraction: "
                       "(3/4) × 12 = 36/4 = 9."},
    "fraction_times_integer": {
        "prompt": "Calculate 2/5 × 3.",
        "explanation": "Write 3 as 3/1, then multiply: "
                       "(2 × 3)/(5 × 1) = 6/5."},
    "arithmetic_order_of_operations": {
        "prompt": "Calculate 3 + 2 × (8 − 5).",
        "explanation": "Evaluate the brackets first: 8 − 5 = 3. Multiply "
                       "next: 2 × 3 = 6. Then add: 3 + 6 = 9."},
    "arithmetic_division_by_ten": {
        "prompt": "Calculate 430 ÷ 10.",
        "explanation": "Dividing by 10 makes each digit's place value one "
                       "tenth as large: 430 ÷ 10 = 43. Check: 43 × 10 = 430."},
    "price_subtraction": {
        "prompt": "Find the difference between EUR 7.50 and EUR 2.20.",
        "explanation": "Align the decimal places and subtract: "
                       "EUR 7.50 − EUR 2.20 = EUR 5.30. Check: "
                       "EUR 5.30 + EUR 2.20 = EUR 7.50."},
    "price_unit_price": {
        "prompt": "Three kilograms cost EUR 12. What is the price per "
                  "kilogram?",
        "explanation": "Divide the total price by the quantity: "
                       "EUR 12 ÷ 3 kg = EUR 4 per kilogram. Check: "
                       "3 kg × EUR 4 per kilogram = EUR 12."},
    "price_proportional_cost": {
        "prompt": "Four notebooks cost EUR 12 at a fixed unit price. What "
                  "do six cost?",
        "explanation": "One notebook costs EUR 12 ÷ 4 = EUR 3. Six "
                       "notebooks cost 6 × EUR 3 = EUR 18."},
    "fractions_compare": {
        "prompt": "Which is larger: 2/3 or 3/5?",
        "explanation": "Use a common denominator: 2/3 = 10/15 and "
                       "3/5 = 9/15. Since 10/15 > 9/15, 2/3 is larger."},
    "fractions_multiply": {
        "prompt": "Calculate 2/3 × 3/5.",
        "explanation": "Multiply the numerators and denominators: "
                       "(2 × 3)/(3 × 5) = 6/15 = 2/5."},
    "fractions_divide_whole": {
        "prompt": "Calculate (3/4) ÷ 2.",
        "explanation": "Multiply by the reciprocal of 2: (3/4) × (1/2) = "
                       "3/8. Check: (3/8) × 2 = 3/4."},
    "fractions_multiply_whole": {
        "prompt": "Calculate 2/5 × 3.",
        "explanation": "Write 3 as 3/1, then multiply: "
                       "(2 × 3)/(5 × 1) = 6/5."},
    "fractions_add_same_denominator": {
        "prompt": "Calculate 2/7 + 3/7.",
        "explanation": "Keep the common denominator and add the "
                       "numerators: (2 + 3)/7 = 5/7."},
    "fractions_add_different_denominators": {
        "prompt": "Calculate 1/3 + 1/4.",
        "explanation": "Use denominator 12: 1/3 = 4/12 and 1/4 = 3/12. "
                       "Then add: 4/12 + 3/12 = 7/12."},
    "percent_of_number": {
        "prompt": "Find 30% of 90.",
        "explanation": "The whole is 90. Write 30% as 30/100 = 0.30, then "
                       "multiply: 0.30 × 90 = 27."},
    "percent_find_whole": {
        "prompt": "30% of an amount is 27. Find the whole amount.",
        "explanation": "Write 30% as 0.30 and divide the part by it: "
                       "27 ÷ 0.30 = 90. Check: 0.30 × 90 = 27."},
    "percent_as_multiplier": {
        "prompt": "Write 35% as a decimal multiplier.",
        "explanation": "Divide by 100: 35/100 = 0.35. Multiplying an "
                       "amount by 0.35 gives 35% of that amount."},
    "percent_find_rate": {
        "prompt": "What percentage of 50 is 15?",
        "explanation": "The part is 15 and the whole is 50. Divide and "
                       "multiply by 100: (15/50) × 100 = 30, so the "
                       "percentage is 30%."},
}
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


def resolve_practice(context, evidence, candidate=None, aligned=False):
    status = context["status"]
    card = {"status": status, "message": PRACTICE_STATUS_MESSAGES[status],
            "notice": PRACTICE_SELECTION_NOTICE, "question": None}
    focus = (candidate or {}).get("focus") or {}
    if aligned and focus.get("subtopic_name"):
        card["topic_label"] = focus["subtopic_name"]
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
    if aligned:
        card["instruction"] = PRACTICE_ALIGNED_INSTRUCTION
    if candidate is not None:
        focus = candidate.get("focus")
        if focus is None:
            card["relationship"] = {
                "kind": "no_review_focus",
                "message":
                    PRACTICE_RELATIONSHIP_MESSAGES["no_review_focus"]}
        elif (focus.get("skill_id") == question["skill_id"]
                and focus.get("subtopic_id") == row["concept_id"]):
            card["relationship"] = {
                "kind": "same_content",
                "message": PRACTICE_RELATIONSHIP_MESSAGES["same_content"]}
        elif aligned and focus.get("skill_id") == question["skill_id"]:
            card["relationship"] = {
                "kind": "same_topic_different_content",
                "message": PRACTICE_RELATIONSHIP_MESSAGES[
                    "same_topic_different_content"].format(
                        concept_name=row["concept_label"],
                        subtopic_name=focus.get("subtopic_name"))}
        else:
            card["relationship"] = {
                "kind": "different_content",
                "message": PRACTICE_RELATIONSHIP_MESSAGES[
                    "different_content"].format(
                        concept_name=row["concept_label"],
                        subtopic_name=focus.get("subtopic_name"))}
    return {"card": card,
            "selection": {"skill_name": skill["skill_name"],
                          "concept_name": row["concept_label"],
                          "mathematical_task": row["mathematical_task"]}}


def review_example(candidate):
    focus = (candidate or {}).get("focus") or {}
    record = REVIEW_EXAMPLES.get(focus.get("subtopic_id"))
    if record is None:
        return None
    return {"title": "Worked review example",
            "prompt": copy.deepcopy(record["prompt"]),
            "explanation": copy.deepcopy(record["explanation"]),
            "notice": REVIEW_EXAMPLE_NOTICE}


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
