import copy

import worked_review_math

ASSESSMENT_WORKED_REVIEW_VERSION = "assessment_grounded_worked_review_v1"
GROUNDED_TEMPLATE_VERSION = "phase3_student_feedback_template_v6"
SELECTION_POLICY = "earliest_incorrect_in_selected_subtopic_v1"
CONTEXT_KEYS = {"bank", "taxonomy", "responses"}
CARD_TITLE = "Worked review of your assessment answer"
READY_NOTICE = (
    "This revisits an answer from this assessment. A selected option "
    "does not establish why it was chosen. Explain the steps to your "
    "teacher before separate practice.")
UNAVAILABLE_NOTICE = (
    "A checked worked solution is unavailable. Ask your teacher to "
    "review this assessment question; no replacement example has been "
    "invented.")
UNAVAILABLE_REASONING = (
    "Explain your approach to this question to your teacher, then work "
    "through it together.")
UNSOLVED_ERRORS = (ValueError, SyntaxError, ZeroDivisionError,
                   OverflowError)


def _rebuild_evidence(context):
    if (not isinstance(context, dict) or set(context) != CONTEXT_KEYS
            or not isinstance(context["bank"], dict)
            or not isinstance(context["taxonomy"], dict)
            or not isinstance(context["responses"], list)):
        raise ValueError(
            "assessment_context must contain exactly bank, taxonomy, "
            "responses")
    bank, taxonomy, responses = (context["bank"], context["taxonomy"],
                                 context["responses"])
    if len(responses) != 40:
        raise ValueError(
            "worked review requires the end checkpoint's 40 answers")
    from evidence_feedback import (
        build_evidence, build_research_evidence)
    builder = (build_research_evidence
               if bank.get("protocol")
               == "offline_historical_evaluation_bank"
               else build_evidence)
    return builder(bank, taxonomy, responses)


def _taxonomy_leaf(taxonomy, focus):
    for topic in taxonomy.get("topics") or []:
        if topic.get("skill_id") != focus["skill_id"]:
            continue
        for subtopic in topic.get("subtopics") or []:
            if subtopic.get("id") == focus["subtopic_id"]:
                return subtopic
    raise ValueError("no taxonomy leaf matches the selected focus")


def build_assessment_review_examples(evidence, selected_candidates,
                                     assessment_context=None):
    if assessment_context is None:
        return []
    rebuilt = _rebuild_evidence(assessment_context)
    from evidence_feedback import validate_evidence
    from evidence_feedback_policy import build_candidates
    safe = validate_evidence(evidence)
    if rebuilt != safe:
        raise ValueError(
            "assessment context does not reproduce the supplied evidence")
    if safe["checkpoint"] != "end":
        raise ValueError("worked review requires the end checkpoint")
    permitted = build_candidates(safe, "end")
    candidates = []
    for candidate in selected_candidates:
        matched = [c for c in permitted if c == candidate]
        if not matched:
            raise ValueError(
                "candidate must exactly equal a permitted selection "
                "candidate")
        candidates.append(matched[0])
    bank = assessment_context["bank"]
    taxonomy = assessment_context["taxonomy"]
    responses = assessment_context["responses"]
    examples = []
    for candidate in candidates:
        focus = candidate["focus"]
        if focus is None:
            continue
        leaf = _taxonomy_leaf(taxonomy, focus)
        qids = set(leaf["question_ids"])
        found = None
        for question, row in zip(bank["questions"], responses):
            if (question["question_id"] in qids
                    and question["skill_id"] == focus["skill_id"]
                    and row["selected_index"] != question["answer_index"]):
                found = (question, row)
                break
        if found is None:
            raise ValueError(
                "no incorrectly answered assessment question matches "
                "the selected focus")
        question, row = found
        options = question["options"]
        card = {
            "source": "incorrect_assessment_response",
            "version": ASSESSMENT_WORKED_REVIEW_VERSION,
            "selection_policy": SELECTION_POLICY,
            "title": CARD_TITLE,
            "candidate_id": candidate["candidate_id"],
            "question_id": question["question_id"],
            "skill_id": focus["skill_id"],
            "subtopic_id": focus["subtopic_id"],
            "for_subtopic_name": focus["subtopic_name"],
            "prompt": question["text"],
            "options": copy.deepcopy(options),
            "selected_answer": options[row["selected_index"]],
            "correct_answer": options[question["answer_index"]]}
        try:
            solved = worked_review_math.solution(
                question, focus["subtopic_id"])
        except UNSOLVED_ERRORS:
            card.update({
                "status": "unavailable", "steps": [], "explanation": "",
                "reasoning_question": UNAVAILABLE_REASONING,
                "solution_version": None,
                "notice": UNAVAILABLE_NOTICE})
        else:
            card.update({
                "status": "ready",
                "steps": solved["steps"],
                "explanation": "\n".join(solved["steps"]),
                "reasoning_question": solved["reasoning_question"],
                "solution_version": solved["solution_version"],
                "notice": READY_NOTICE})
        examples.append(card)
    return examples
