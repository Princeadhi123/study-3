"""Freeze and review synthetic observed-answer feedback without providers.

This is a lead-authored coverage harness, not an educational-quality grader.
It never loads KT, sends HTTP requests, or modifies a saved source artifact.
"""
import argparse
import hashlib
import itertools
import json
from pathlib import Path

import phase3_paths
from evidence_feedback import (
    build_evidence, canonical_digest, run_feedback, validate_evidence)
from evidence_feedback_policy import (
    POLICY_VERSION, PRACTICE_ACTIONS, PRIORITY_DESCRIPTION, REPORT_SCHEMA,
    candidate_priority)
from session_store import bank_fingerprint

ROLES = (("student", "midpoint"), ("student", "end"), ("teacher", "end"))


def _submissions(bank, correct_positions, wrong_shift=1):
    return [{"question_id": question["question_id"],
             "selected_index": (
                 question["answer_index"] if i in correct_positions else
                 (question["answer_index"] + wrong_shift) % len(question["options"]))}
            for i, question in enumerate(bank["questions"])]


def _check_saved(case, bank, evidence):
    rows = case["responses"]
    if len(rows) != 40:
        raise ValueError("saved scenario requires exactly 40 answers")
    for i, (row, question) in enumerate(zip(rows, bank["questions"])):
        if (type(row["position"]) is not int or row["position"] != i + 1
                or row["question_id"] != question["question_id"]
                or row["skill_id"] != question["skill_id"]
                or type(row["correct"]) is not bool
                or row["correct"] != (row["selected_index"] == question["answer_index"])):
            raise ValueError("saved response labels disagree with private scorer")
    for checkpoint, count in (("midpoint", 20), ("end", 40)):
        safe = evidence[checkpoint]
        old = case["observed"][checkpoint]
        skills = {s["skill_id"]: (s["correct"], s["out_of"]) for s in safe["skills"]}
        old_skills = {s["skill_id"]: (s["correct"], s["out_of"]) for s in old["skills"]}
        subtopics = {s["subtopic_id"]: (s["correct"], s["out_of"])
                    for s in safe["subtopics"] if s["out_of"]}
        old_subtopics = {s["subtopic_id"]: (s["correct"], s["out_of"])
                        for s in old["subtopics"]}
        if skills != old_skills or subtopics != old_subtopics:
            raise ValueError("new evidence disagrees with preserved observed counts")
        if safe["total"]["correct"] != sum(r["correct"] for r in rows[:count]):
            raise ValueError("new total disagrees with saved answers")
        if checkpoint == "end" and old["total"] != {
                "correct": safe["total"]["correct"], "out_of": 40}:
            raise ValueError("saved end total disagrees with private scorer")


def freeze(source, bank, taxonomy):
    raw = source.read_bytes()
    original = json.loads(raw.decode("utf-8"))
    if (original.get("schema") != "phase3_fixed40_comparison_v1"
            or original.get("scope") !=
            "private_synthetic_fixed_40_cold_start_not_student_validation"
            or original.get("bank_sha256") != bank_fingerprint(bank)):
        raise ValueError("expected saved synthetic scenarios for this bank")
    saved = original["scenarios"]
    if len(saved) != 54 or len({c["name"] for c in saved}) != 54:
        raise ValueError("expected the preserved 54-scenario matrix")
    subtopic_ids = {s["id"] for t in taxonomy["topics"] for s in t["subtopics"]}
    if subtopic_ids != set(PRACTICE_ACTIONS):
        raise ValueError("the real-bank review requires an exact practice-action mapping")
    cases = []

    def append(name, group, submissions, saved_case=None):
        evidence = {checkpoint: build_evidence(bank, taxonomy, submissions[:count])
                    for checkpoint, count in (("midpoint", 20), ("end", 40))}
        if saved_case is not None:
            _check_saved(saved_case, bank, evidence)
        cases.append({"name": name, "group": group, "evidence": evidence})

    for case in saved:
        submissions = [{"question_id": r["question_id"],
                        "selected_index": r["selected_index"]}
                       for r in case["responses"]]
        append(case["name"], "saved_" + case["group"], submissions, case)
    all_positions = set(range(40))
    for index in range(40):
        append(f"single_incorrect_q{index + 1:02d}", "single_error",
               _submissions(bank, all_positions - {index}))
        append(f"single_correct_q{index + 1:02d}", "single_correct",
               _submissions(bank, {index}))
    index_by_id = {q["question_id"]: i for i, q in enumerate(bank["questions"])}
    for topic in taxonomy["topics"]:
        for subtopic in topic["subtopics"]:
            positions = {index_by_id[qid] for qid in subtopic["question_ids"]}
            append("all_errors_in_" + subtopic["id"], "subtopic_boundary",
                   _submissions(bank, all_positions - positions))
            append("only_correct_in_" + subtopic["id"], "subtopic_boundary",
                   _submissions(bank, positions))
        for half in (0, 1):
            positions = {i for i, q in enumerate(bank["questions"])
                         if q["skill_id"] == topic["skill_id"] and i // 20 == half}
            append(f"only_{topic['skill_id']}_half_{half + 1}_correct",
                   "skill_half_boundary", _submissions(bank, positions))
    if source.read_bytes() != raw:
        raise ValueError("source changed while freezing; no report may be published")
    return {
        "schema": "phase3_evidence_feedback_inputs_v1",
        "source": {"file": source.name, "sha256": hashlib.sha256(raw).hexdigest(),
                   "bank_sha256": bank_fingerprint(bank),
                   "taxonomy_sha256": canonical_digest(taxonomy)},
        "policy": {"version": POLICY_VERSION,
                   "priority_description": PRIORITY_DESCRIPTION,
                   "source_sha256": hashlib.sha256(
                       Path(__file__).with_name("evidence_feedback_policy.py").read_bytes()
                   ).hexdigest(),
                   "provider_mode": "deterministic_no_provider_calls"},
        "coverage": {
            "saved_scenarios": len(saved), "additional_scenarios": len(cases) - len(saved),
            "all_answer_sequences_exhausted": False,
            "independent_hosted_reruns": False,
            "note": "Finite saved and boundary scenarios; not every answer sequence."},
        "scenarios": cases}


def _check_review(evidence, audience, checkpoint, review):
    if review["requires_human_review"] is not True:
        raise ValueError("review approval boundary changed")
    if checkpoint == "midpoint":
        if (review["sanitized_evidence"] != {}
                or len(review["candidates"]) != 1
                or review["selected_candidate_id"] != "neutral"
                or [s["kind"] for s in review["message"]["sections"]] != ["encouragement"]):
            raise ValueError("midpoint must have encouragement only")
        return
    candidates = review["candidates"]
    by_id = {s["subtopic_id"]: s for s in evidence["subtopics"]}
    expected = {s["subtopic_id"] for s in evidence["subtopics"] if s["incorrect"]}
    actual = {c["focus"]["subtopic_id"] for c in candidates if c["focus"]}
    if expected != actual:
        raise ValueError("review candidates must cover exactly observed-error content")
    for candidate in candidates:
        if candidate["focus"] is not None:
            focus = candidate["focus"]
            if focus != by_id[focus["subtopic_id"]] or not focus["incorrect"]:
                raise ValueError("review candidate is not supported by counts")
    if expected:
        chosen = candidates[0]["focus"]
        if candidate_priority(chosen) != min(candidate_priority(by_id[sid]) for sid in expected):
            raise ValueError("baseline is not the documented presentation priority")
    elif review["selected_candidate_id"] != "optional_consolidation":
        raise ValueError("all-correct assessment must not invent an error priority")
    if review["sanitized_evidence"]["total"] != evidence["total"]:
        raise ValueError("feedback changed the observed score")
    if audience == "teacher" and not any(
            s["kind"] == "half_observations" for s in review["message"]["sections"]):
        raise ValueError("teacher summary requires bounded half observations")


def evaluate(snapshot):
    policy_hash = hashlib.sha256(
        Path(__file__).with_name("evidence_feedback_policy.py").read_bytes()).hexdigest()
    if (snapshot.get("schema") != "phase3_evidence_feedback_inputs_v1"
            or snapshot["policy"]["version"] != POLICY_VERSION
            or snapshot["policy"]["source_sha256"] != policy_hash):
        raise ValueError("frozen inputs must match this exact feedback policy")
    scenarios = []
    for case in snapshot["scenarios"]:
        packages = []
        for audience, checkpoint in ROLES:
            evidence = validate_evidence(case["evidence"][checkpoint])
            review = run_feedback(evidence, audience, checkpoint)
            _check_review(evidence, audience, checkpoint, review)
            packages.append({"audience": audience, "checkpoint": checkpoint, "review": review})
        scenarios.append({"name": case["name"], "group": case["group"], "packages": packages})
    count = len(scenarios)
    return {
        "schema": REPORT_SCHEMA, "status": "draft_not_for_learner_delivery",
        "source": snapshot["source"], "policy": snapshot["policy"],
        "inputs_sha256": canonical_digest(snapshot),
        "coverage": snapshot["coverage"],
        "summary": {
            "scenario_labels": count, "feedback_packages": count * 3,
            "midpoint_packages": count, "end_packages": count * 2,
            "new_provider_calls": 0, "educator_review_completed": False,
            "educational_effectiveness_tested": False},
        "scenarios": scenarios}


def check_skill_total_vectors(bank, taxonomy):
    """Check one sequence per possible skill-total vector, not all placements."""
    by_skill = [[i for i, q in enumerate(bank["questions"]) if q["skill_id"] == topic["skill_id"]]
                for topic in taxonomy["topics"]]
    if len(by_skill) != 4 or any(len(indexes) != 10 for indexes in by_skill):
        raise ValueError("skill-total check requires four ten-question skills")
    checked = 0
    for vector in itertools.product(range(11), repeat=4):
        correct = set().union(*(set(indexes[:count]) for indexes, count in zip(by_skill, vector)))
        evidence = build_evidence(bank, taxonomy, _submissions(bank, correct))
        if [s["correct"] for s in evidence["skills"]] != list(vector):
            raise ValueError("skill-total representative was scored incorrectly")
        for audience in ("student", "teacher"):
            review = run_feedback(evidence, audience, "end")
            _check_review(evidence, audience, "end", review)
        checked += 1
    return {
        "skill_total_vectors_checked": checked,
        "representative_sequences_per_vector": 1,
        "all_subtopic_or_order_arrangements_exhausted": False,
        "educational_quality_measured": False}


def write_new(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=(
        phase3_paths.ARTIFACTS / "assessment_pipeline_20261001.json"))
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--check-skill-vectors", action="store_true")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("inputs.json", "report.json", "coverage.json", "policy_snapshot.json"):
        if (args.out_dir / filename).exists():
            raise FileExistsError("refusing to overwrite feedback captures")
    bank = json.loads(phase3_paths.APPROVED_BANK.read_text(encoding="utf-8"))
    taxonomy = json.loads(phase3_paths.ASSESSMENT_TAXONOMY.read_text(encoding="utf-8"))
    snapshot = freeze(args.source, bank, taxonomy)
    policy_raw = Path(__file__).with_name("evidence_feedback_policy.py").read_bytes()
    if hashlib.sha256(policy_raw).hexdigest() != snapshot["policy"]["source_sha256"]:
        raise ValueError("policy changed while freezing")
    write_new(args.out_dir / "policy_snapshot.json", {
        "file": "evidence_feedback_policy.py",
        "sha256": snapshot["policy"]["source_sha256"],
        "source": policy_raw.decode("utf-8")})
    write_new(args.out_dir / "inputs.json", snapshot)
    report = evaluate(snapshot)
    coverage = dict(snapshot["coverage"])
    if args.check_skill_vectors:
        coverage.update(check_skill_total_vectors(bank, taxonomy))
    report["coverage"] = coverage
    if hashlib.sha256(args.source.read_bytes()).hexdigest() != snapshot["source"]["sha256"]:
        raise ValueError("source changed during review; refusing publication")
    write_new(args.out_dir / "report.json", report)
    write_new(args.out_dir / "coverage.json", coverage)
    print(json.dumps({"summary": report["summary"], "coverage": coverage}, indent=2))


if __name__ == "__main__":
    main()
