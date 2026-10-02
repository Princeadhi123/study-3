"""Synthetic-only, evidence-grounded feedback drafts, separate from live API.

The private bank scores answers locally. Only aggregate observed counts enter
the review/selector seam. Source labels, KT, simulated ability, distractor
choices, and raw question/answer data cannot determine this feedback.
"""
import copy
import hashlib
import json
import re

import phase3_paths
from evidence_feedback_policy import (
    GENERATION_SCHEMA, POLICY_VERSION, REVIEW_SCHEMA, REVIEW_STATUS,
    SELECTION_SCHEMA, build_candidates, render_message)
from feedback_service import validate_assessment_taxonomy
from mcq_test import score_checkpoint
from schemas import validate_submission
from session_store import bank_fingerprint
from synthetic_feedback import _check_generated_opening

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


def validate_selection_payload(payload):
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


def run_feedback(evidence, audience, checkpoint, selector=None, generator=None):
    """Return an auditable review draft with a fixed evidence-rendered body.

    The selector chooses an observed-error review focus, not a diagnosis.
    The optional generator writes only a neutral opening; it cannot rewrite
    scores, review evidence, or instructions. Invalid callbacks fall back once,
    without exposing their output, and no retry is attempted.
    """
    payload = selection_payload(evidence, audience, checkpoint)
    safe = payload["evidence"]
    candidates = payload["candidates"]
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
    opening = None
    if generator is not None and not selector_failed:
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
    return {
        "schema": REVIEW_SCHEMA, "status": "draft_not_for_learner_delivery",
        "audience": audience, "checkpoint": checkpoint,
        "sanitized_evidence": copy.deepcopy(safe),
        "candidates": copy.deepcopy(candidates),
        "baseline_candidate_id": baseline["candidate_id"],
        "selected_candidate_id": selected["candidate_id"],
        "template_baseline": render_message(safe, audience, checkpoint, baseline),
        "message": render_message(safe, audience, checkpoint, selected, opening),
        "requires_human_review": True,
        "trace": {
            "policy_version": POLICY_VERSION,
            "selection_source": selection_source,
            "phrasing_source": phrasing_source,
            "fallback_reason": fallback_reason,
            "selection_matches_baseline":
                baseline["candidate_id"] == selected["candidate_id"],
            "validation": ("shape_and_heuristic_opening_checks_only"
                           if opening else "deterministic_evidence_and_draft_actions"),
            "provider_advantage_demonstrated": False}}
