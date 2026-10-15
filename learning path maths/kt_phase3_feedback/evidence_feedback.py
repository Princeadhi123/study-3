"""Synthetic-only, evidence-grounded feedback drafts, separate from live API.

The private bank scores answers locally. Only aggregate observed counts enter
the review/selector seam. Source labels, KT, simulated ability, distractor
choices, and raw question/answer data cannot determine this feedback.

Each review candidate carries a fixed application-owned ``planning`` block
(error pattern, coverage, parent counts, priority group, exact ties) built by
the policy module; the returned review adds ``feedback_plan``, a deep-copied
``phase3_observed_feedback_plan_v1`` snapshot of the selected candidate with
``kt_used=False``. Plans are never forwarded to the opening-only generator,
and providers cannot invent or alter them because the selection payload is
revalidated against freshly rebuilt candidates before any request.
"""
import copy
import hashlib
import json
import re

import phase3_paths
from evidence_feedback_policy import (
    GENERATION_SCHEMA, MULTI_REVIEW_POLICY_VERSION,
    MULTI_SELECTION_PROMPT_VERSION, MULTI_SELECTION_SCHEMA, POLICY_VERSION,
    REVIEW_SCHEMA, REVIEW_STATUS, SELECTION_PROMPT_VERSION,
    SELECTION_SCHEMA, build_candidates, build_feedback_plan,
    build_review_bundles, render_message)
from feedback_service import validate_assessment_taxonomy
from mcq_test import score_checkpoint, validate_bank_structure
from schemas import validate_submission
from session_store import bank_fingerprint
from synthetic_feedback import _check_generated_opening
from test_feed import build_test_feed

EVIDENCE_SCHEMA = "phase3_observed_evidence_v1"
COUNT_KEYS = {"correct", "incorrect", "out_of"}
SKILL_KEYS = COUNT_KEYS | {"skill_id", "skill_name", "halves"}
SUBTOPIC_KEYS = SKILL_KEYS | {"subtopic_id", "subtopic_name"}
EVIDENCE_KEYS = {
    "schema", "data_origin", "bank_sha256", "taxonomy_sha256",
    "checkpoint", "total", "halves", "skills", "subtopics"}
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
HASH_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
AUDIENCES = ("student", "teacher")
CHECKPOINTS = ("midpoint", "end")


def canonical_digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _counts(correct, out_of):
    return {"correct": correct, "incorrect": out_of - correct, "out_of": out_of}


def build_evidence(bank, taxonomy, responses):
    """Score caller-attested synthetic responses; never trust supplied labels.

    This entry point is for offline synthetic review, not real-data provider
    authorization. Its source-of-truth inputs stay local.
    """
    scored = score_checkpoint(bank, responses)
    return _aggregate_evidence(bank, taxonomy, responses, scored)


def build_research_evidence(bank, taxonomy, responses):
    """Private synthetic scoring of frozen research banks, without serving approval."""
    if taxonomy.get("schema") == "phase3_research_assessment_taxonomy_v1":
        from research_runtime import score_research_checkpoint
        return _aggregate_evidence(
            bank, taxonomy, responses, score_research_checkpoint(bank, responses))
    if (bank.get("protocol") != "offline_historical_evaluation_bank"
            or bank.get("review_status") != "independently_math_checked; educator_approval_pending"):
        raise ValueError("explicit independently checked research bank required")
    questions = validate_bank_structure(bank)
    if not isinstance(responses, list) or len(responses) != 40:
        raise ValueError("research scoring requires 40 synthetic responses")
    observed = []
    for index, (row, question) in enumerate(zip(responses, questions)):
        selected = validate_submission(row, question, index)
        observed.append({"question_id": question["question_id"],
                         "skill_id": question["skill_id"],
                         "correct": selected == question["answer_index"]})
    scored = build_test_feed(observed, bank["skill_names"])
    return _aggregate_evidence(bank, taxonomy, responses, scored)


def _aggregate_evidence(bank, taxonomy, responses, scored):
    validate_assessment_taxonomy(bank, taxonomy)
    if len(bank["skill_names"]) != 4:
        raise ValueError("feedback requires exactly four assessed skills")
    rows = []
    for index, response in enumerate(responses):
        question = bank["questions"][index]
        selected = validate_submission(response, question, index)
        rows.append({
            "question_id": question["question_id"],
            "skill_id": question["skill_id"],
            "half": index // 20,
            "correct": selected == question["answer_index"]})

    def aggregate(subset):
        halves = [_counts(sum(r["correct"] for r in subset if r["half"] == half),
                          sum(r["half"] == half for r in subset))
                  for half in (0, 1)]
        return {**_counts(sum(r["correct"] for r in subset), len(subset)),
                "halves": halves}

    topics, leaves = [], []
    for topic in taxonomy["topics"]:
        sid, name = topic["skill_id"], topic["skill_name"]
        topics.append({"skill_id": sid, "skill_name": name,
                       **aggregate([r for r in rows if r["skill_id"] == sid])})
        for subtopic in topic["subtopics"]:
            qids = set(subtopic["question_ids"])
            leaves.append({
                "skill_id": sid, "skill_name": name,
                "subtopic_id": subtopic["id"],
                "subtopic_name": subtopic["name"],
                **aggregate([r for r in rows if r["question_id"] in qids])})
    totals = aggregate(rows)
    if scored["teacher"]["total"] != {
            "correct": totals["correct"], "out_of": totals["out_of"]}:
        raise ValueError("observed evidence disagrees with checkpoint scorer")
    evidence = {
        "schema": EVIDENCE_SCHEMA, "data_origin": "synthetic",
        "bank_sha256": bank_fingerprint(bank),
        "taxonomy_sha256": canonical_digest(taxonomy),
        "checkpoint": "midpoint" if len(rows) == 20 else "end",
        "total": {key: totals[key] for key in COUNT_KEYS},
        "halves": totals["halves"], "skills": topics, "subtopics": leaves}
    return validate_evidence(evidence)


def _exact_dict(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} has unexpected or missing fields")


def _validate_count(value, maximum=40):
    _exact_dict(value, COUNT_KEYS, "count")
    if (any(type(value[key]) is not int for key in COUNT_KEYS)
            or not 0 <= value["correct"] <= value["out_of"] <= maximum
            or value["incorrect"] != value["out_of"] - value["correct"]):
        raise ValueError("invalid observed count")


def _sum_counts(rows):
    return {key: sum(row[key] for row in rows) for key in COUNT_KEYS}


def _validate_halves(row):
    halves = row["halves"]
    if not isinstance(halves, list) or len(halves) != 2:
        raise ValueError("exactly two half-count rows are required")
    for half in halves:
        _validate_count(half, maximum=20)
    if _sum_counts(halves) != {key: row[key] for key in COUNT_KEYS}:
        raise ValueError("half counts disagree with totals")


def _validate_id(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError("invalid aggregate content identifier")


def _validate_name(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 160
            or any(not c.isprintable() for c in value)):
        raise ValueError("invalid aggregate content name")


def validate_evidence(evidence):
    """Reject inconsistent aggregates or widened payloads before callbacks."""
    _exact_dict(evidence, EVIDENCE_KEYS, "evidence")
    if (evidence["schema"] != EVIDENCE_SCHEMA
            or evidence["data_origin"] != "synthetic"
            or evidence["checkpoint"] not in CHECKPOINTS):
        raise ValueError("only synthetic checkpoint evidence is permitted")
    for key in ("bank_sha256", "taxonomy_sha256"):
        if not isinstance(evidence[key], str) or not HASH_PATTERN.fullmatch(evidence[key]):
            raise ValueError("evidence requires version fingerprints")
    end = evidence["checkpoint"] == "end"
    expected_out_of = 40 if end else 20
    _validate_count(evidence["total"])
    if evidence["total"]["out_of"] != expected_out_of:
        raise ValueError("checkpoint total does not match answered prefix")
    _validate_halves({**evidence["total"], "halves": evidence["halves"]})
    if [h["out_of"] for h in evidence["halves"]] != [20, 20 if end else 0]:
        raise ValueError("checkpoint half sizes are incorrect")
    skills, subtopics = evidence["skills"], evidence["subtopics"]
    if not isinstance(skills, list) or len(skills) != 4:
        raise ValueError("exactly four assessed skill rows are required")
    if not isinstance(subtopics, list) or not 1 <= len(subtopics) <= 40:
        raise ValueError("assessed subtopic rows are required")
    skill_by_id = {}
    for skill in skills:
        _exact_dict(skill, SKILL_KEYS, "skill")
        _validate_id(skill["skill_id"])
        _validate_name(skill["skill_name"])
        _validate_count({key: skill[key] for key in COUNT_KEYS})
        _validate_halves(skill)
        if (skill["skill_id"] in skill_by_id
                or skill["out_of"] != (10 if end else 5)
                or [h["out_of"] for h in skill["halves"]] != [5, 5 if end else 0]):
            raise ValueError("duplicate skill or invalid skill checkpoint size")
        skill_by_id[skill["skill_id"]] = skill
    if _sum_counts(skills) != evidence["total"]:
        raise ValueError("skill counts disagree with total")
    seen = set()
    for subtopic in subtopics:
        _exact_dict(subtopic, SUBTOPIC_KEYS, "subtopic")
        _validate_id(subtopic["subtopic_id"])
        _validate_id(subtopic["skill_id"])
        _validate_name(subtopic["subtopic_name"])
        _validate_count({key: subtopic[key] for key in COUNT_KEYS})
        _validate_halves(subtopic)
        parent = skill_by_id.get(subtopic["skill_id"])
        if (parent is None or subtopic["skill_name"] != parent["skill_name"]
                or subtopic["subtopic_id"] in seen):
            raise ValueError("subtopic must have a unique id and matching parent")
        seen.add(subtopic["subtopic_id"])
    for sid, skill in skill_by_id.items():
        leaves = [s for s in subtopics if s["skill_id"] == sid]
        if not leaves or _sum_counts(leaves) != {key: skill[key] for key in COUNT_KEYS}:
            raise ValueError("subtopic counts disagree with parent skill")
        for half in (0, 1):
            if _sum_counts([s["halves"][half] for s in leaves]) != skill["halves"][half]:
                raise ValueError("subtopic half counts disagree with parent skill")
    for half in (0, 1):
        if _sum_counts([s["halves"][half] for s in skills]) != evidence["halves"][half]:
            raise ValueError("skill half counts disagree with total")
    return copy.deepcopy(evidence)


MIDPOINT_MESSAGE_VERSION = "phase3_student_midpoint_v5"


def compose_midpoint_message(evidence):
    """Fixed-wording student midpoint message from validated evidence."""
    normalized = validate_evidence(evidence)
    if normalized["checkpoint"] != "midpoint":
        raise ValueError("midpoint message requires midpoint evidence")
    correct = normalized["total"]["correct"]
    skills = normalized["skills"]
    sections = [{"kind": "encouragement", "title": "Halfway there",
                 "text": "You have completed 20 of 40 questions. Here is "
                         "a quick look at your answers so far."}]
    focus_title = "Your answers so far"
    if correct == normalized["total"]["out_of"]:
        strengths = ("You answered every question correctly in this first "
                     "set. Keep taking each question one step at a time.")
        focus = ("You answered every question correctly in this first "
                 "set.")
    elif correct == 0:
        strengths = ("You have completed the first set. This is a "
                     "starting point, not a limit on what you can learn.")
        focus = ("In this first set, your answers were incorrect in "
                 "each topic.")
    else:
        best = max(skill["correct"] for skill in skills)
        strongest = [s for s in skills if s["correct"] == best]
        if len(strongest) == len(skills):
            strengths = ("You answered some questions correctly in each "
                         "topic in this first set.")
        elif best == strongest[0]["out_of"]:
            strengths = (
                "You answered all five questions correctly in "
                + ", ".join(s["skill_name"] for s in strongest)
                + " in this first set. These are encouraging results "
                  "to build on.")
        else:
            strengths = (
                "You answered questions correctly in "
                + ", ".join(s["skill_name"] for s in strongest)
                + ". These were your highest-scoring topics in this "
                  "first set.")
        errored = [s for s in skills if s["incorrect"] > 0]
        lowest = min(skill["correct"] for skill in errored)
        weakest = [s for s in errored if s["correct"] == lowest]
        if len(weakest) == len(skills):
            focus = ("In this first set, some of your answers were "
                     "incorrect in each topic.")
        else:
            focus_title = "Where you had more difficulty"
            names = [s["skill_name"] for s in weakest]
            if len(names) > 2:
                names = ", ".join(names[:-1]) + " and " + names[-1]
            else:
                names = " and ".join(names)
            focus = ("In this first set, you had more incorrect "
                     "answers in " + names
                     + " than in the other topics.")
    sections.extend([
        {"kind": "strengths", "title": "What is going well",
         "text": strengths},
        {"kind": "focus", "title": focus_title,
         "text": focus},
        {"kind": "next_step", "title": "Before you continue",
         "text": "There are 20 questions left. Take a short "
                 "break if you want, then continue one "
                 "question at a time."}])
    return {"sections": sections,
            "text": "\n\n".join(section["text"] for section in sections),
            "total": {"correct": normalized["total"]["correct"],
                      "out_of": normalized["total"]["out_of"]},
            "skills": [{"skill_name": skill["skill_name"],
                        "correct": skill["correct"],
                        "out_of": skill["out_of"]}
                       for skill in normalized["skills"]]}


def _validate_role(audience, checkpoint):
    if audience not in AUDIENCES or checkpoint not in CHECKPOINTS:
        raise ValueError("unsupported feedback audience or checkpoint")
    if audience == "teacher" and checkpoint != "end":
        raise ValueError("teacher review is supported only at assessment end")


def selection_payload(evidence, audience, checkpoint):
    _validate_role(audience, checkpoint)
    normalized = validate_evidence(evidence)
    if normalized["checkpoint"] != checkpoint:
        raise ValueError("feedback checkpoint must match observed evidence")
    safe = normalized if checkpoint == "end" else {}
    return {"schema": SELECTION_SCHEMA, "audience": audience,
            "checkpoint": checkpoint, "evidence": safe,
            "candidates": build_candidates(safe, checkpoint)}


def bundle_selection_payload(evidence):
    normalized = validate_evidence(evidence)
    if normalized["checkpoint"] != "end":
        raise ValueError("bundle selection requires end evidence")
    return {"schema": MULTI_SELECTION_SCHEMA, "audience": "student",
            "checkpoint": "end", "evidence": normalized,
            "candidates": build_review_bundles(normalized)}


def _validate_multi_selection_payload(payload):
    _exact_dict(payload, {"schema", "audience", "checkpoint", "evidence",
                          "candidates"}, "selection payload")
    if payload["schema"] != MULTI_SELECTION_SCHEMA:
        raise ValueError("invalid selection schema")
    _validate_role(payload["audience"], payload["checkpoint"])
    if payload["checkpoint"] != "end":
        raise ValueError("bundle selection requires end evidence")
    safe = validate_evidence(payload["evidence"])
    if safe["checkpoint"] != "end":
        raise ValueError("end selection requires end evidence")
    expected = build_review_bundles(safe)
    if payload["candidates"] != expected:
        raise ValueError("candidates must match the fixed observed-evidence policy")
    return {"schema": MULTI_SELECTION_SCHEMA, "audience": payload["audience"],
            "checkpoint": "end", "evidence": safe, "candidates": expected}


def select_review_bundle(evidence, selector=None):
    payload = bundle_selection_payload(evidence)
    bundles = payload["candidates"]
    baseline = bundles[0]
    selected = baseline
    selection_source = "rules"
    fallback_reason = None
    selector_failed = False
    execution = None
    if len(bundles) == 1:
        selection_source = "rules_single_candidate"
    elif selector is not None:
        callback_payload = copy.deepcopy(payload)
        try:
            reply = selector.select(callback_payload)
        # CaptureCache.obtain raises SystemExit when the new-call budget
        # is exhausted; keep it a recorded fallback. KeyboardInterrupt is
        # deliberately not caught.
        except SystemExit:  # NOSONAR python:S5754 - budget-exhaustion boundary
            fallback_reason = "demo_call_budget_exhausted"
            selector_failed = True
        except Exception:
            fallback_reason = "selector_error"
            selector_failed = True
        else:
            if callback_payload != payload:
                fallback_reason = "mutated_selection_payload"
                selector_failed = True
            elif (not isinstance(reply, dict) or set(reply) != {"candidate_id"}
                  or not isinstance(reply["candidate_id"], str)):
                fallback_reason = "invalid_selection"
                selector_failed = True
            else:
                matches = [b for b in bundles
                           if b["candidate_id"] == reply["candidate_id"]]
                if not matches:
                    fallback_reason = "invalid_selection"
                    selector_failed = True
                else:
                    selected = matches[0]
                    selection_source = "injected_selector"
        execution = copy.deepcopy(getattr(selector, "execution", None))
    return {"bundle": copy.deepcopy(selected), "execution": execution,
            "trace": {
                "selection_source": selection_source,
                "selector_failed": selector_failed,
                "fallback_reason": fallback_reason,
                "selection_matches_baseline":
                    baseline["candidate_id"] == selected["candidate_id"]}}


def validate_selection_payload(payload):
    if (isinstance(payload, dict)
            and payload.get("schema") == MULTI_SELECTION_SCHEMA):
        return _validate_multi_selection_payload(payload)
    _exact_dict(payload, {"schema", "audience", "checkpoint", "evidence",
                          "candidates"}, "selection payload")
    if payload["schema"] != SELECTION_SCHEMA:
        raise ValueError("invalid selection schema")
    _validate_role(payload["audience"], payload["checkpoint"])
    if payload["checkpoint"] == "midpoint":
        if payload["evidence"] != {}:
            raise ValueError("midpoint must not expose performance evidence")
        safe = {}
    else:
        safe = validate_evidence(payload["evidence"])
        if safe["checkpoint"] != "end":
            raise ValueError("end selection requires end evidence")
    expected = build_candidates(safe, payload["checkpoint"])
    if payload["candidates"] != expected:
        raise ValueError("candidates must match the fixed observed-evidence policy")
    return {"schema": SELECTION_SCHEMA, "audience": payload["audience"],
            "checkpoint": payload["checkpoint"], "evidence": safe,
            "candidates": expected}


def _end_message(safe, audience, checkpoint, selected, opening,
                 practice_context):
    if practice_context is None:
        return render_message(safe, audience, checkpoint, selected,
                              opening)
    import feedback_practice
    return feedback_practice.contextual_message(
        safe, audience, selected, practice_context, opening)


def _feedback_validation_label(full_message, opening):
    if full_message is not None:
        return "shape_and_numeric_guardrails_not_semantic_verification"
    if opening:
        return "shape_and_heuristic_opening_checks_only"
    return "deterministic_evidence_and_draft_actions"


def run_feedback(evidence, audience, checkpoint, selector=None,
                 generator=None, practice_context=None):
    """Return an auditable review draft with a fixed evidence-rendered body.

    The selector chooses an observed-error review focus, not a diagnosis.
    The optional generator writes only a neutral opening; it cannot rewrite
    scores, review evidence, or instructions. Invalid callbacks fall back once,
    without exposing their output, and no retry is attempted.
    """
    payload = selection_payload(evidence, audience, checkpoint)
    safe = payload["evidence"]
    candidates = payload["candidates"]
    integrated_ctx = None
    if practice_context is not None:
        if checkpoint != "end":
            raise ValueError("practice context applies only at the end")
        import feedback_practice
        integrated_ctx = feedback_practice.validate_practice_context(
            practice_context, safe)
    baseline = candidates[0]
    selected = baseline
    selection_source = "rules"
    phrasing_source = "deterministic"
    fallback_reason = None
    selector_failed = False
    if len(candidates) == 1:
        selection_source = "rules_single_candidate"
    elif selector is not None:
        callback_payload = copy.deepcopy(payload)
        try:
            reply = selector.select(callback_payload)
        except Exception:
            fallback_reason = "selector_error"
            selector_failed = True
        else:
            if callback_payload != payload:
                fallback_reason = "mutated_selection_payload"
                selector_failed = True
            elif (not isinstance(reply, dict) or set(reply) != {"candidate_id"}
                  or not isinstance(reply["candidate_id"], str)):
                fallback_reason = "invalid_selection"
                selector_failed = True
            else:
                matches = [c for c in candidates
                           if c["candidate_id"] == reply["candidate_id"]]
                if not matches:
                    fallback_reason = "invalid_selection"
                    selector_failed = True
                else:
                    selected = matches[0]
                    selection_source = "injected_selector"
    # Application-owned plan snapshot of the validated selection. Deep-copied
    # so it cannot alias candidates, evidence, or any callback-owned object,
    # and it is never included in the narrow generator payload.
    feedback_plan = copy.deepcopy(build_feedback_plan(selected))
    opening = None
    full_message = None
    generation_prompt_version = None
    if (generator is not None and not selector_failed
            and getattr(generator, "supports_full_feedback", False)
            and checkpoint == "end"):
        from full_feedback import (
            FULL_FEEDBACK_PROMPT_VERSION, build_full_input,
            full_message_sections, validate_full_reply)
        generation_prompt_version = FULL_FEEDBACK_PROMPT_VERSION
        full_input = build_full_input(safe, audience, selected,
                                      integrated_ctx)
        callback_payload = copy.deepcopy(full_input)
        try:
            reply = generator.generate(callback_payload)
        # CaptureCache.obtain raises SystemExit when the new-call budget
        # is exhausted; keep it a recorded generator fallback.
        # KeyboardInterrupt is deliberately not caught.
        except (Exception, SystemExit):  # NOSONAR python:S5754 - budget-exhaustion boundary
            fallback_reason = "generator_error"
        else:
            checked = validate_full_reply(reply, selected["candidate_id"])
            if callback_payload != full_input or checked is None:
                fallback_reason = "invalid_generation"
            else:
                full_message = {"sections": full_message_sections(checked)}
                full_message["text"] = "\n\n".join(
                    s["text"] for s in full_message["sections"])
                phrasing_source = "injected_generator_full_sections"
    elif generator is not None and not selector_failed:
        generation = {
            "schema": GENERATION_SCHEMA, "audience": audience,
            "checkpoint": checkpoint,
            "selected_candidate": {key: selected[key] for key in
                                   ("candidate_id", "strategy", "review_status")}}
        callback_payload = copy.deepcopy(generation)
        try:
            reply = generator.generate(callback_payload)
        except Exception:
            fallback_reason = "generator_error"
        else:
            names = [s["skill_name"] for s in evidence["skills"]]
            names.extend(s["subtopic_name"] for s in evidence["subtopics"])
            checked = _check_generated_opening(
                reply, selected["candidate_id"], names)
            if (callback_payload != generation or checked is None
                    or (audience == "teacher" and re.search(
                        r"\b(?:you|your|yours)\b", checked, re.IGNORECASE))):
                fallback_reason = "invalid_generation"
            else:
                opening = checked
                phrasing_source = "injected_generator_opening_only"
    validation = _feedback_validation_label(full_message, opening)
    return {
        "schema": REVIEW_SCHEMA, "status": "draft_not_for_learner_delivery",
        "audience": audience, "checkpoint": checkpoint,
        "sanitized_evidence": copy.deepcopy(safe),
        "candidates": copy.deepcopy(candidates),
        "baseline_candidate_id": baseline["candidate_id"],
        "selected_candidate_id": selected["candidate_id"],
        "feedback_plan": feedback_plan,
        "template_baseline": render_message(safe, audience, checkpoint, baseline),
        "message": (copy.deepcopy(full_message)
                    if full_message is not None
                    else _end_message(safe, audience, checkpoint, selected,
                                      opening, integrated_ctx)),
        "requires_human_review": True,
        "trace": {
            "policy_version": POLICY_VERSION,
            "selection_prompt_version": SELECTION_PROMPT_VERSION,
            "selection_source": selection_source,
            "phrasing_source": phrasing_source,
            "fallback_reason": fallback_reason,
            "selection_matches_baseline":
                baseline["candidate_id"] == selected["candidate_id"],
            "generation_prompt_version": generation_prompt_version,
            "validation": validation,
            "provider_advantage_demonstrated": False}}


def _multi_generated_message(safe, selected, generator,
                             sel_trace, practice_contexts,
                             assessment_context):
    from full_feedback import (
        MULTI_FULL_FEEDBACK_PROMPT_VERSION, build_multi_full_input,
        full_message_sections, validate_full_reply)
    prompt_version = MULTI_FULL_FEEDBACK_PROMPT_VERSION
    fallback_reason = sel_trace["fallback_reason"]
    if (generator is None or sel_trace["selector_failed"]
            or not getattr(generator, "supports_full_feedback", False)):
        return None, fallback_reason, "deterministic", None
    full_input = build_multi_full_input(
        safe, "student", selected, practice_contexts or [],
        assessment_context=assessment_context)
    callback_payload = copy.deepcopy(full_input)
    try:
        reply = generator.generate(callback_payload)
    # CaptureCache.obtain raises SystemExit when the new-call budget is
    # exhausted; keep it a recorded generator fallback. KeyboardInterrupt
    # is deliberately not caught.
    except (Exception, SystemExit):  # NOSONAR python:S5754 - budget-exhaustion boundary
        return None, "generator_error", "deterministic", prompt_version
    checked = validate_full_reply(reply, selected["candidate_id"])
    if callback_payload != full_input or checked is None:
        return None, "invalid_generation", "deterministic", \
            prompt_version
    message = {"sections": full_message_sections(checked)}
    message["text"] = "\n\n".join(s["text"] for s in message["sections"])
    return (message, fallback_reason,
            "injected_generator_full_sections", prompt_version)


def _guarded_review_message(safe, originals, examples,
                            assessment_context, practice_contexts,
                            full_message, provider_full_narrative=False):
    from full_feedback import guarded_multi_message
    return guarded_multi_message(
        safe, originals,
        review_examples=(examples
                         if assessment_context is not None else None),
        practice_contexts=practice_contexts,
        generated_sections=(full_message["sections"]
                            if full_message is not None else None),
        provider_full_narrative=provider_full_narrative)


def _multi_feedback_validation_label(message, full_message):
    from full_feedback import PROVIDER_FULL_NARRATIVE_GUARDRAILS_VERSION
    if (message["guardrails_version"]
            == PROVIDER_FULL_NARRATIVE_GUARDRAILS_VERSION):
        return ("provider_full_narrative_shape_checks_only_requires_"
                "educator_review")
    if full_message is not None:
        return ("application_owned_facts_and_actions_provider_summary_"
                "shape_checked")
    return "deterministic_evidence_and_draft_actions"


def run_multi_feedback(evidence, selection=None, selector=None,
                       generator=None, practice_contexts=None,
                       assessment_context=None,
                       provider_full_narrative=False):
    from full_feedback import guarded_multi_message
    from assessment_worked_review import (
        ASSESSMENT_WORKED_REVIEW_VERSION,
        build_assessment_review_examples)
    payload = bundle_selection_payload(evidence)
    safe = payload["evidence"]
    bundles = payload["candidates"]
    if selection is None:
        selection = select_review_bundle(evidence, selector)
    matched = [b for b in bundles if b == selection["bundle"]]
    if not matched:
        raise ValueError("selected bundle is not a permitted bundle")
    selected = matched[0]
    originals = selected["review_candidates"]
    examples = build_assessment_review_examples(
        safe, originals, assessment_context)
    plans = [copy.deepcopy(build_feedback_plan(c)) for c in originals]
    sel_trace = selection["trace"]
    full_message, fallback_reason, phrasing_source, \
        generation_prompt_version = _multi_generated_message(
            safe, selected, generator, sel_trace,
            practice_contexts, assessment_context)

    def deterministic(cands, exs=None):
        return guarded_multi_message(safe, cands, review_examples=exs)

    if assessment_context is not None:
        baseline_examples = build_assessment_review_examples(
            safe, bundles[0]["review_candidates"], assessment_context)
        template_baseline = deterministic(
            bundles[0]["review_candidates"], baseline_examples)
    else:
        template_baseline = deterministic(bundles[0]["review_candidates"])
    message = _guarded_review_message(
        safe, originals, examples, assessment_context,
        practice_contexts, full_message,
        provider_full_narrative=provider_full_narrative)
    review = {
        "schema": REVIEW_SCHEMA, "status": "draft_not_for_learner_delivery",
        "audience": "student", "checkpoint": "end",
        "sanitized_evidence": copy.deepcopy(safe),
        "candidates": copy.deepcopy(bundles),
        "baseline_candidate_id": bundles[0]["candidate_id"],
        "selected_candidate_id": selected["candidate_id"],
        "selected_candidate_ids": [c["candidate_id"] for c in originals],
        "selected_candidates": copy.deepcopy(originals),
        "feedback_plans": plans,
        "feedback_plan": copy.deepcopy(plans[0]),
        "template_baseline": template_baseline,
        "message": message,
        "requires_human_review": True,
        "trace": {
            "policy_version": MULTI_REVIEW_POLICY_VERSION,
            "selection_prompt_version": MULTI_SELECTION_PROMPT_VERSION,
            "selection_source": sel_trace["selection_source"],
            "phrasing_source": phrasing_source,
            "guardrails_version": message["guardrails_version"],
            "section_sources": copy.deepcopy(message["section_sources"]),
            "fallback_reason": fallback_reason,
            "selection_matches_baseline":
                bundles[0]["candidate_id"] == selected["candidate_id"],
            "generation_prompt_version": generation_prompt_version,
            "validation": _multi_feedback_validation_label(
                message, full_message),
            "provider_advantage_demonstrated": False}}
    if assessment_context is not None:
        review["review_examples"] = copy.deepcopy(examples)
        review["worked_review_version"] = ASSESSMENT_WORKED_REVIEW_VERSION
    return review
