import copy

import worked_example_selection
import worked_review_math

ASSESSMENT_WORKED_REVIEW_VERSION = "assessment_grounded_worked_review_v1"
GROUNDED_TEMPLATE_VERSION = "phase3_student_feedback_template_v7"
CONCISE_TEMPLATE_VERSION = "phase3_student_feedback_template_v8"
SELECTION_POLICY = "earliest_incorrect_in_selected_subtopic_v1"
CONTEXT_KEYS = {"bank", "taxonomy", "responses"}
SELECTION_CONTEXT_KEY = worked_example_selection.CONTEXT_KEY
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
CONCISE_TITLES = {
    "assessment_summary": "Assessment complete",
    "observed_strengths": "What went well",
    "review_focus": "Answers to review",
    "next_steps": "What to do next"}
CONCISE_SCOPE = (
    "These results describe this assessment, not overall mastery.")
CONCISE_READY_NOTICE = (
    "Compare your answer with the steps, then explain your reasoning.")
CONCISE_PRACTICE_NOTICE = (
    "Synthetic demo practice. This activity does not establish mastery "
    "or an optimal learning difficulty.")


def _rebuild_evidence(context):
    if (not isinstance(context, dict)
            or set(context) not in (
                CONTEXT_KEYS, CONTEXT_KEYS | {SELECTION_CONTEXT_KEY})
            or not isinstance(context["bank"], dict)
            or not isinstance(context["taxonomy"], dict)
            or not isinstance(context["responses"], list)):
        raise ValueError(
            "assessment_context must contain exactly bank, taxonomy, "
            "responses plus an optional worked example selection")
    bank, taxonomy, responses = (context["bank"], context["taxonomy"],
                                 context["responses"])
    if SELECTION_CONTEXT_KEY in context:
        from worked_example_selection import validate_selection
        validate_selection(
            context[SELECTION_CONTEXT_KEY], bank, responses)
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
    snapshot = assessment_context.get(SELECTION_CONTEXT_KEY)
    examples = []
    for candidate in candidates:
        focus = candidate["focus"]
        if focus is None:
            continue
        leaf = _taxonomy_leaf(taxonomy, focus)
        qids = set(leaf["question_ids"])
        provenance = None
        if snapshot is not None:
            question, row, solved, provenance = (
                worked_example_selection.select_question(
                    bank, responses, candidate, qids, snapshot))
        else:
            found = None
            for question, row in zip(bank["questions"], responses):
                if (question["question_id"] in qids
                        and question["skill_id"] == focus["skill_id"]
                        and row["selected_index"]
                        != question["answer_index"]):
                    found = (question, row)
                    break
            if found is None:
                raise ValueError(
                    "no incorrectly answered assessment question "
                    "matches the selected focus")
            question, row = found
            solved = "unresolved"
        options = question["options"]
        card = {
            "source": "incorrect_assessment_response",
            "version": ASSESSMENT_WORKED_REVIEW_VERSION,
            "selection_policy": (worked_example_selection.POLICY
                                 if snapshot is not None
                                 else SELECTION_POLICY),
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
        if provenance is not None:
            card["selection_provenance"] = provenance
        if solved == "unresolved":
            try:
                solved = worked_review_math.solution(
                    question, focus["subtopic_id"])
            except UNSOLVED_ERRORS:
                solved = None
        if solved is None:
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


def _concise_focus_paragraph(candidate):
    focus = candidate.get("focus") or {}
    planning = candidate.get("planning") or {}
    paragraph = f"{focus['subtopic_name']}."
    if planning.get("error_pattern") == "isolated_incorrect_answer":
        paragraph += (" One answer was incorrect here; this alone "
                      "does not show a repeated difficulty.")
    if focus.get("out_of") == 1:
        paragraph += " Only one question covered this area."
    if len(planning.get("tied_candidate_ids") or []) > 1:
        paragraph += " Other areas had the same review priority."
    return paragraph


def concise_student_preview(preview, selected_candidates):
    from feedback_practice import PRACTICE_SELECTION_NOTICE
    result = copy.deepcopy(preview)
    result["template_version"] = CONCISE_TEMPLATE_VERSION
    sections = result.get("sections") or []
    by_kind = {}
    for section in sections:
        kind = section.get("kind")
        if kind in CONCISE_TITLES:
            section["title"] = CONCISE_TITLES[kind]
            by_kind[kind] = section
    candidates = list(selected_candidates or [])
    error_candidates = [
        c for c in candidates if (c.get("focus") or {}).get(
            "subtopic_name")]
    review_focus = by_kind.get("review_focus")
    if review_focus is not None:
        if error_candidates:
            paragraphs = "\n\n".join(
                _concise_focus_paragraph(c) for c in error_candidates)
            review_focus["text"] = ("Review these topics:\n\n"
                                    + paragraphs + "\n\n"
                                    + CONCISE_SCOPE)
        else:
            total = result.get("total") or {}
            if (total.get("correct") is not None
                    and total["correct"] == total.get("out_of")):
                review_focus["text"] = (
                    "You answered all questions correctly on this "
                    "assessment.")
    examples = result.get("review_examples") or []
    ready = any(e.get("status") == "ready" for e in examples)
    unavailable = any(e.get("status") == "unavailable"
                      for e in examples)
    if ready:
        steps = ("Review your answers below. Compare them with the "
                 "worked steps and explain your reasoning.")
    elif error_candidates:
        steps = ("Review the topics listed above and explain how you "
                 "approached those questions.")
    else:
        steps = "Keep building on these results."
    if unavailable:
        steps += (" For any question without a checked worked "
                  "solution, ask your teacher for help.")
    practices = list(result.get("practice_activities") or [])
    if not practices and result.get("practice"):
        practices = [result["practice"]]
    chosen = [p for p in practices
              if p.get("status") == "selected" and p.get("question")]
    if len(chosen) == 1:
        steps += " Then try the practice activity below."
    elif len(chosen) > 1:
        steps += " Then try the practice activities below."
    next_steps = by_kind.get("next_steps")
    if next_steps is not None:
        next_steps["text"] = steps
    for example in examples:
        if example.get("notice") == READY_NOTICE:
            example["notice"] = CONCISE_READY_NOTICE
    candidates_by_skill = {
        c["focus"]["skill_id"]: c for c in error_candidates}
    skill_ids = {s.get("skill_name"): s.get("skill_id")
                 for s in result.get("skills") or []}
    for card in practices:
        question = card.get("question") or {}
        relationship = card.get("relationship") or {}
        kind = relationship.get("kind")
        if kind == "same_content":
            relationship["message"] = (
                "This practises the same concept as the review above.")
        elif kind == "same_topic_different_content":
            matched = candidates_by_skill.get(
                skill_ids.get(question.get("skill_name")))
            if matched is not None:
                relationship["message"] = (
                    "This is related practice on "
                    f"{question.get('concept_name')}, not "
                    f"{matched['focus']['subtopic_name']}.")
        if card.get("notice") == PRACTICE_SELECTION_NOTICE:
            card["notice"] = CONCISE_PRACTICE_NOTICE
    result["text"] = "\n\n".join(
        s.get("text", "") for s in sections if s.get("text"))
    return result


QUESTION_FIRST_VERSION = "question_first_worked_review_v1"
QUESTION_FIRST_TEMPLATE_VERSION = "phase3_student_feedback_template_v9"
QUESTION_FIRST_READY_NOTICE = (
    "Explain your approach first. Use a hint if needed, then reveal the checked worked steps.")
QUESTION_FIRST_NEXT_STEPS = (
    "Think through the review questions below and explain your approach. "
    "Use the hints if needed, then reveal the checked worked steps to compare your reasoning.")
GENERIC_SOCRATIC_PROMPT = worked_review_math.GENERIC_SOCRATIC_PROMPT
OPTION_CONTRASTS = {
    ("32978__p2__qad057f930f90539d__of1e565b0fb849511", "7w"): {
        "prompt": "Sievennä lauseke:\\n\\n5v + 2v",
        "options": ["7w", "5v2v", "52w", "7v"],
        "correct_answer": "7v", "subtopic_id": "combine_like_terms",
        "text": "Your selected option uses w, while both terms in the question use v. How did you choose the variable letter in your answer?"},
    ("32978__p2__qad057f930f90539d__of1e565b0fb849511", "5v2v"): {
        "prompt": "Sievennä lauseke:\\n\\n5v + 2v",
        "options": ["7w", "5v2v", "52w", "7v"],
        "correct_answer": "7v", "subtopic_id": "combine_like_terms",
        "text": "Your selected option places the terms next to each other without the original plus sign. How did you represent the addition?"},
    ("512961__p2__q5f787a330af2b755__o03da091c4f2e8d2f", "2xy"): {
        "prompt": "Sievennä  x + y",
        "options": ["x + y", "2xy", "xy", "x"],
        "correct_answer": "x + y", "subtopic_id": "retain_unlike_terms",
        "text": "Your selected option writes x and y together as a product. What operation is shown between x and y in the question?"},
}


def question_first_preview(preview):
    result = copy.deepcopy(preview)
    result["template_version"] = QUESTION_FIRST_TEMPLATE_VERSION
    result["review_presentation_version"] = QUESTION_FIRST_VERSION
    examples = result.get("review_examples") or []
    for card in examples:
        if card.get("source") != "incorrect_assessment_response":
            continue
        ready = card.get("status") == "ready" and bool(card.get("steps"))
        scaffold = (worked_review_math.pre_reveal_scaffold(card.get("subtopic_id"))
                    if ready and card.get("solution_version")
                    else {"socratic_prompt": GENERIC_SOCRATIC_PROMPT, "progressive_hints": []})
        card.update(scaffold)
        card["presentation_version"] = QUESTION_FIRST_VERSION
        card["scaffold_source"] = "application_authored_method_prompt_v1"
        card["option_contrast"] = ""
        card["option_contrast_source"] = None
        match = OPTION_CONTRASTS.get((card.get("question_id"), card.get("selected_answer")))
        if ready and match and all(card.get(key) == match[key] for key in (
                "prompt", "options", "correct_answer", "subtopic_id")):
            card["option_contrast"] = match["text"]
            card["option_contrast_source"] = "application_authored_exact_option_observation_v1"
        if ready and card.get("notice") in (READY_NOTICE, CONCISE_READY_NOTICE):
            card["notice"] = QUESTION_FIRST_READY_NOTICE
    if examples and any(c.get("status") == "ready" for c in examples):
        next_steps = next((s for s in result.get("sections", []) if s.get("kind") == "next_steps"), None)
        if next_steps is not None:
            text = QUESTION_FIRST_NEXT_STEPS
            if any(c.get("explanation_source") == "provider"
                   for c in examples):
                text = text.replace("the checked worked steps",
                                    "the worked explanations")
            if any(c.get("status") == "unavailable" for c in examples):
                text += " For any question without a checked worked solution, ask your teacher for help."
            practices = result.get("practice_activities") or ([result["practice"]] if result.get("practice") else [])
            chosen = [p for p in practices if p.get("status") == "selected" and p.get("question")]
            if len(chosen) == 1:
                text += " Then try the practice activity below."
            elif len(chosen) > 1:
                text += " Then try the practice activities below."
            next_steps["text"] = text
    result["text"] = "\n\n".join(s.get("text", "") for s in result.get("sections", []) if s.get("text"))
    return result
