"""End-to-end synthetic feedback replay with real Jev/Aitta opt-in.

Validated frozen scoring, graph, KT and conformal results are joined locally.
Only observed evidence enters Jev; only opening context enters Aitta. No
research diagnosis enters learner wording. This is not a live student route.
"""
import argparse
import copy
import hashlib
import json
import math
import time
from pathlib import Path

import phase3_paths
from aitta_generator import (
    AittaGenerator, MAX_TOKENS, REASONING_EFFORT, RESPONSE_FORMAT)
from evaluate_evidence_feedback import _check_review, _check_saved, write_new
from evidence_feedback import (
    canonical_digest, run_feedback, selection_payload, validate_evidence,
    validate_selection_payload)
from evidence_feedback_policy import (
    POLICY_VERSION, PRIORITY_DESCRIPTION, REPORT_SCHEMA, REVIEW_STATUS,
    SELECTION_INSTRUCTIONS, SELECTION_PROMPT_VERSION, SELECTION_SCHEMA)
from evidence_providers import (
    EvidenceJevSelector, _validated_opening_payload)
from jev_selector import (
    ENDPOINT, MODEL, QUESTION_ID, _read_env_file, _validate_timeout)
from replay_provider_scenarios import CaptureCache
from schemas import validate_submission
from session_store import bank_fingerprint
from synthetic_feedback import (
    GENERATION_INSTRUCTIONS, PROMPT_VERSION, _build_candidates)
from transport_diagnostics import InstrumentedEvidenceAittaGenerator

PIPELINE_SCHEMA = "phase3_integrated_synthetic_pipeline_v1"
ROLES = (("student", "midpoint"), ("student", "end"), ("teacher", "end"))
DEFAULT_SCENARIO = "all_skills_equal"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _check_graph(graph, evidence):
    _require(graph["bank_sha256"] == evidence["bank_sha256"]
             and graph["relation"] == "is_part_of_not_prerequisite"
             and graph["scope"] == "approved_bank_topics_subtopics_only",
             "graph binding or scope changed")
    for checkpoint in ("midpoint", "end"):
        if checkpoint == evidence["checkpoint"]:
            scored = graph["checkpoints"][checkpoint]
            _require(scored["checkpoint"] == checkpoint
                     and scored["bank_sha256"] == evidence["bank_sha256"],
                     "graph checkpoint binding changed")
            topics = {s["skill_id"]: (s["correct"], s["out_of"])
                      for s in scored["topics"]}
            expected = {s["skill_id"]: (s["correct"], s["out_of"])
                        for s in evidence["skills"]}
            leaves = {
                s["subtopic_id"]: (s["correct"], s["incorrect"], s["out_of"])
                for topic in scored["topics"] for s in topic["subtopics"]}
            expected_leaves = {
                s["subtopic_id"]: (s["correct"], s["incorrect"], s["out_of"])
                for s in evidence["subtopics"]}
            _require(topics == expected and leaves == expected_leaves,
                     "frozen graph disagrees with observed evidence")


def _check_diagnostics(case, bank):
    probabilities = case["kt_pre_answer_probability"]
    _require(isinstance(probabilities, list) and len(probabilities) == 40
             and all(_probability(p) for p in probabilities),
             "KT requires forty finite pre-answer probabilities")
    conformal = case["conformal"]
    _require(conformal["status"] == "exploratory_only_not_fixed_bank_validated"
             and conformal["midpoint_calibrated_k"] == 5
             and conformal["end_calibrated_k"] == 10,
             "conformal scope or calibration sizes changed")
    for checkpoint, limit, n_items in (("midpoint", 20, 5), ("end", 40, 10)):
        rows = conformal["checkpoints"][checkpoint]
        _require(set(rows) == set(bank["skill_names"]),
                 "conformal checkpoint skill coverage changed")
        for sid, row in rows.items():
            indexes = [i for i, q in enumerate(bank["questions"][:limit])
                       if q["skill_id"] == sid]
            decisions = row["item_decisions"]
            _require(row["skill_id"] == sid
                     and row["skill_name"] == bank["skill_names"][sid]
                     and row["n_items"] == n_items
                     and len(decisions) == len(indexes) == n_items,
                     "conformal checkpoint alignment changed")
            _require(all(_probability(d["p_correct"])
                         and abs(d["p_correct"] - probabilities[i]) <= 1e-9
                         for d, i in zip(decisions, indexes)),
                     "conformal item inputs disagree with frozen KT")
            _require(_probability(row["point_estimate"])
                     and abs(row["point_estimate"] -
                             sum(probabilities[i] for i in indexes) / n_items) <= 1e-9
                     and _probability(row["lower"]) and _probability(row["upper"])
                     and row["lower"] <= row["upper"],
                     "conformal interval or point estimate is inconsistent")


def prepare_snapshot(source, evidence_inputs, bank, taxonomy, names=None):
    """Join frozen authoritative artifacts, never regenerate their predictions."""
    source_raw = source.read_bytes()
    input_raw = evidence_inputs.read_bytes()
    original = json.loads(source_raw.decode("utf-8"))
    frozen = json.loads(input_raw.decode("utf-8"))
    source_hash = hashlib.sha256(source_raw).hexdigest()
    policy_path = Path(__file__).with_name("evidence_feedback_policy.py")
    _require(original["schema"] == "phase3_fixed40_comparison_v1"
             and original["scope"] ==
             "private_synthetic_fixed_40_cold_start_not_student_validation",
             "only the saved synthetic source is permitted")
    _require(frozen["schema"] == "phase3_evidence_feedback_inputs_v1"
             and frozen["source"]["sha256"] == source_hash
             and original["bank_sha256"] == bank_fingerprint(bank)
             == frozen["source"]["bank_sha256"]
             and frozen["source"]["taxonomy_sha256"] == canonical_digest(taxonomy)
             and frozen["policy"]["version"] == POLICY_VERSION
             and frozen["policy"]["source_sha256"] == _file_hash(policy_path),
             "source, evidence, bank, taxonomy or policy fingerprint changed")
    saved = original["scenarios"]
    _require(len(saved) == 54 and len({c["name"] for c in saved}) == 54,
             "expected the full saved fifty-four-scenario source")
    saved_by_name = {c["name"]: c for c in saved}
    frozen_by_name = {c["name"]: c for c in frozen["scenarios"]}
    selected_names = list(saved_by_name) if names is None else list(names)
    _require(selected_names and len(set(selected_names)) == len(selected_names)
             and all(name in saved_by_name for name in selected_names),
             "scenario names must be distinct saved scenarios")
    provenance = {}
    for label, record in original["diagnostic_provenance"].items():
        path = Path(record["path"])
        _require(path.is_file() and _file_hash(path) == record["sha256"],
                 "frozen diagnostic provenance file changed or is unavailable")
        provenance[label] = {"file": path.name, "sha256": record["sha256"]}
    cases = []
    for name in selected_names:
        saved_case = saved_by_name[name]
        observed = frozen_by_name[name]["evidence"]
        _check_saved(saved_case, bank, observed)
        for index, row in enumerate(saved_case["responses"]):
            validate_submission({
                "question_id": row["question_id"],
                "selected_index": row["selected_index"]}, bank["questions"][index], index)
        for checkpoint in ("midpoint", "end"):
            validate_evidence(observed[checkpoint])
            _check_graph(saved_case["graph"], observed[checkpoint])
        _check_diagnostics(saved_case, bank)
        coverage = saved_case["kt_coverage"]
        cases.append({
            "name": name, "group": saved_case["group"],
            "evidence": copy.deepcopy(observed),
            "assessment_graph": copy.deepcopy(saved_case["graph"]),
            "research_diagnostics": {
                "status": "private_research_not_learner_advice",
                "mode": "frozen_replay_no_model_or_calibration_rerun",
                "kt": {
                    "status": saved_case["kt_status"],
                    "p_correct_before_each_answer":
                        copy.deepcopy(saved_case["kt_pre_answer_probability"]),
                    "coverage": {
                        "question_count": coverage["question_count"],
                        "unknown_item_count": len(coverage["unknown_item_ids"]),
                        "missing_skill_count": len(coverage["missing_skill_indexes"]),
                        "missing_content_count": len(coverage["missing_content_indexes"]),
                        "missing_option_count": len(coverage["missing_option_values"])}},
                "conformal": copy.deepcopy(saved_case["conformal"])}})
    _require(source.read_bytes() == source_raw and evidence_inputs.read_bytes() == input_raw,
             "source inputs changed during preparation")
    return {
        "schema": PIPELINE_SCHEMA, "status": "private_synthetic_review_only",
        "source": {**frozen["source"], "file": source.name},
        "evidence_inputs": {
            "file": evidence_inputs.name, "sha256": hashlib.sha256(input_raw).hexdigest()},
        "policy": copy.deepcopy(frozen["policy"]),
        "diagnostic_provenance": provenance, "scenarios": cases}


def jev_request(payload):
    normalized = validate_selection_payload(payload)
    candidates = normalized["candidates"]
    return {
        "endpoint": ENDPOINT, "body": {
            "model": MODEL,
            "state": {key: normalized[key] for key in
                      ("audience", "checkpoint", "evidence", "candidates")},
            "questions": {QUESTION_ID: {
                "type": "choice", "instructions": SELECTION_INSTRUCTIONS,
                "criteria": {
                    c["candidate_id"]: c["action"] if c["action"] is not None
                    else c["strategy"] for c in candidates}}}}}


def aitta_request(payload, model, endpoint_hash):
    """The effective legacy wire has no performance data or review focus."""
    normalized = _validated_opening_payload(payload)
    candidate = _build_candidates(normalized["checkpoint"], [])[0]
    context = {
        "audience": normalized["audience"], "checkpoint": normalized["checkpoint"],
        "selected_candidate": {key: candidate[key] for key in
                               ("candidate_id", "strategy", "review_status")}}
    return {
        "endpoint_sha256": endpoint_hash,
        "body": {
            "model": model, "messages": [
                {"role": "system", "content": GENERATION_INSTRUCTIONS},
                {"role": "user", "content": json.dumps(context)}],
            "max_tokens": MAX_TOKENS, "reasoning_effort": REASONING_EFFORT,
            "response_format": dict(RESPONSE_FORMAT)}}


class CachedReviewSelector:
    def __init__(self, native, cache):
        self.native, self.cache, self.execution = native, cache, None

    def select(self, payload):
        request = jev_request(payload)
        reply, self.execution = self.cache.obtain(
            "jev", request, lambda: self.native.select(payload), self.native)
        if reply is None:
            raise RuntimeError("captured_selector_failure")
        return reply


class CachedReviewOpening:
    def __init__(self, native, cache, model, endpoint_hash):
        self.native, self.cache = native, cache
        self.model, self.endpoint_hash = model, endpoint_hash
        self.execution = None

    def generate(self, payload):
        request = aitta_request(payload, self.model, self.endpoint_hash)

        def obtain_opening():
            reply = self.native.generate(payload)
            # Cache the effective wire result, not a case-specific review ID.
            return {"opening": reply["opening"]}

        reply, self.execution = self.cache.obtain(
            "aitta", request, obtain_opening, self.native)
        if reply is None:
            raise RuntimeError("captured_generator_failure")
        return {"candidate_id": payload["selected_candidate"]["candidate_id"],
                "opening": reply["opening"]}


def _contract(snapshot, mode, model=None, endpoint_hash=None, timeout=30.0):
    return {
        "pipeline_schema": PIPELINE_SCHEMA,
        "snapshot_sha256": canonical_digest(snapshot),
        "policy_version": POLICY_VERSION,
        "policy_sha256": snapshot["policy"]["source_sha256"],
        "core_sha256": _file_hash(Path(__file__).with_name("evidence_feedback.py")),
        "provider_adapter_sha256": _file_hash(
            Path(__file__).with_name("evidence_providers.py")),
        "runner_sha256": _file_hash(Path(__file__)),
        "selection_prompt_version": SELECTION_PROMPT_VERSION,
        "selection_instructions_sha256": canonical_digest(SELECTION_INSTRUCTIONS),
        "generation_prompt_version": PROMPT_VERSION,
        "generation_instructions_sha256": canonical_digest(GENERATION_INSTRUCTIONS),
        "provider_mode": mode, "aitta_requested_model": model,
        "aitta_endpoint_sha256": endpoint_hash,
        "provider_timeout_seconds": timeout}


def seed_successes(cache, source_dir, model, endpoint_hash):
    """Reuse only valid successful captures, never silently retry failures."""
    _require(source_dir.is_dir(), "seed capture directory does not exist")
    seeded = 0
    for source_path in sorted(source_dir.glob("*.json")):
        record = json.loads(source_path.read_text(encoding="utf-8"))
        if record.get("ok") is not True:
            continue
        provider, request, reply = record["provider"], record["request"], record["reply"]
        if provider == "jev":
            state = request["body"]["state"]
            payload = {"schema": SELECTION_SCHEMA, **state}
            _require(request == jev_request(payload),
                     "seeded Jev request does not match the current wire contract")
            _require(isinstance(reply, dict) and set(reply) == {"candidate_id"}
                     and reply["candidate_id"] in {
                         c["candidate_id"] for c in state["candidates"]},
                     "seeded Jev reply is not a permitted selection")
        elif provider == "aitta":
            context = json.loads(request["body"]["messages"][1]["content"])
            checkpoint = context["checkpoint"]
            stub = {
                "schema": "phase3_evidence_opening_v1",
                "audience": context["audience"], "checkpoint": checkpoint,
                "selected_candidate": {
                    "candidate_id": "neutral" if checkpoint == "midpoint" else
                    "optional_consolidation",
                    "strategy": "neutral_encouragement" if checkpoint == "midpoint"
                    else "optional_consolidation", "review_status": REVIEW_STATUS}}
            _require(request == aitta_request(stub, model, endpoint_hash)
                     and isinstance(reply, dict) and set(reply) == {"opening"}
                     and isinstance(reply["opening"], str),
                     "seeded Aitta request or reply does not match the current wire")
        else:
            raise ValueError("unknown seed provider")
        _require(record["metadata"]["status"] == "completed",
                 "seed metadata does not confirm successful execution")
        destination = cache.path(provider, request)
        if destination.exists():
            existing = json.loads(destination.read_text(encoding="utf-8"))
            _require(existing["request"] == request and existing["reply"] == reply,
                     "conflicting successful seed capture")
        else:
            seeded_record = copy.deepcopy(record)
            seeded_record["origin"] = "successful_seed:" + source_dir.parent.name
            seeded_record["source_capture_file"] = source_path.name
            write_new(destination, seeded_record)
            seeded += 1
    return seeded


def request_plan(snapshot):
    distinct = {}
    for case in snapshot["scenarios"]:
        for audience in ("student", "teacher"):
            payload = selection_payload(case["evidence"]["end"], audience, "end")
            if len(payload["candidates"]) > 1:
                request = jev_request(payload)
                distinct[canonical_digest(request)] = request
    return {
        "scenario_labels": len(snapshot["scenarios"]),
        "feedback_packages": len(snapshot["scenarios"]) * len(ROLES),
        "distinct_jev_requests_without_captures": len(distinct),
        "maximum_distinct_aitta_contexts": 3,
        "reuse_is_not_an_independent_provider_trial": True}


def replay(snapshot, output, contract, selector=None, generator=None):
    """Execute the combined review stages, retaining each completed package."""
    package_dir = output / "packages"
    package_dir.mkdir(exist_ok=True)
    scenarios = []
    for case_index, case in enumerate(snapshot["scenarios"]):
        packages = []
        for role_index, (audience, checkpoint) in enumerate(ROLES):
            path = package_dir / f"{case_index:03d}_{role_index}.json"
            if path.exists():
                package = json.loads(path.read_text(encoding="utf-8"))
                _require(package["contract"] == contract
                         and package["scenario"] == case["name"]
                         and package["audience"] == audience
                         and package["checkpoint"] == checkpoint,
                         "saved package contract or identity changed")
            else:
                if selector is not None:
                    selector.execution = None
                    generator.execution = None
                evidence = case["evidence"][checkpoint]
                start = time.perf_counter()
                review = run_feedback(evidence, audience, checkpoint, selector, generator)
                _check_review(evidence, audience, checkpoint, review)
                if selector is None:
                    selector_execution = {"status": "offline_rules_no_provider"}
                    generator_execution = {"status": "offline_template_no_provider"}
                else:
                    selector_execution = copy.deepcopy(selector.execution) or {
                        "status": "local_single_candidate", "reused": False}
                    generator_execution = copy.deepcopy(generator.execution) or {
                        "status": "skipped_selector_failure", "reused": False}
                package = {
                    "scenario": case["name"], "audience": audience,
                    "checkpoint": checkpoint, "contract": contract, "review": review,
                    "selector_execution": selector_execution,
                    "generator_execution": generator_execution,
                    "pipeline_wall_ms": round(
                        (time.perf_counter() - start) * 1000, 3)}
                write_new(path, package)
            _check_review(case["evidence"][checkpoint], audience, checkpoint, package["review"])
            packages.append(package)
        scenarios.append({
            "name": case["name"], "group": case["group"], "packages": packages,
            "pipeline_stages": [
                {"stage": "deterministic_scoring",
                 "status": "frozen_observed_results_checked_against_private_bank"},
                {"stage": "assessment_graph",
                 "status": "frozen_taxonomy_graph_checked_against_observed_counts"},
                {"stage": "knowledge_tracing",
                 "status": "frozen_pre_answer_predictions_replayed_not_retrained"},
                {"stage": "conformal",
                 "status": "frozen_intervals_replayed_and_KT_alignment_checked"},
                {"stage": "jev_and_aitta",
                 "status": contract["provider_mode"]}],
            "assessment_graph": copy.deepcopy(case["assessment_graph"]),
            "research_diagnostics": copy.deepcopy(case["research_diagnostics"])})
        print(f"Integrated {case_index + 1}/{len(snapshot['scenarios'])}: "
              f"{case['name']}", flush=True)
    all_packages = [p for case in scenarios for p in case["packages"]]
    end = [p for p in all_packages if p["checkpoint"] == "end"]
    captures = []
    capture_root = output / "provider_captures"
    if capture_root.exists():
        captures = [json.loads(path.read_text(encoding="utf-8"))
                    for path in capture_root.glob("*.json")]
    calls_now = selector.cache.new_calls if selector is not None else 0
    summary = {
        "scenario_labels": len(scenarios), "feedback_packages": len(all_packages),
        "midpoint_packages": len(scenarios), "end_packages": len(end),
        "new_provider_calls": calls_now,
        "recorded_hosted_requests": len(captures),
        "recorded_hosted_failures": sum(not c["ok"] for c in captures),
        "end_selections_matching_rules": sum(
            p["review"]["trace"]["selection_matches_baseline"] for p in end),
        "fallback_packages": sum(
            p["review"]["trace"]["fallback_reason"] is not None for p in all_packages),
        "educator_review_completed": False, "educational_effectiveness_tested": False}
    return {
        "schema": REPORT_SCHEMA, "pipeline_schema": PIPELINE_SCHEMA,
        "status": "draft_not_for_learner_delivery",
        "source": copy.deepcopy(snapshot["source"]),
        "policy": {**snapshot["policy"],
                   "priority_description": PRIORITY_DESCRIPTION,
                   "provider_mode": contract["provider_mode"]},
        "contract": contract, "summary": summary,
        "pipeline": {
            "status": "completed_with_fallbacks" if summary["fallback_packages"] else "completed",
            "research_mode": "frozen_KT_and_conformal_replay",
            "student_feedback_evidence": "observed_counts_and_assessed_subtopics_only",
            "graph_relation": "is_part_of_not_prerequisite",
            "provider_failures": "captured_once_no_retries_deterministic_fallback",
            "reuse_policy": "identical_effective_requests_share_captured_responses",
            "latency_warning": "reused package wall time is not fresh hosted latency",
            "live_student_api_changed": False},
        "coverage": request_plan(snapshot),
        "diagnostic_provenance": copy.deepcopy(snapshot["diagnostic_provenance"]),
        "scenarios": scenarios}


def _write_or_match(path, value):
    if path.exists():
        _require(json.loads(path.read_text(encoding="utf-8")) == value,
                 "saved inputs or contract changed; use a fresh output directory")
    else:
        write_new(path, value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=(
        phase3_paths.ARTIFACTS / "assessment_pipeline_20261001.json"))
    parser.add_argument("--evidence-inputs", type=Path, default=(
        phase3_paths.ARTIFACTS / "evidence_feedback_20261002_v2" / "inputs.json"))
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("offline", "hosted"), default="offline")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--scenario", action="append")
    group.add_argument("--all-scenarios", action="store_true")
    parser.add_argument("--jev-env-file", type=Path)
    parser.add_argument("--max-new-calls", type=int, default=0)
    parser.add_argument("--provider-timeout", type=float, default=30.0)
    parser.add_argument("--seed-captures", type=Path)
    args = parser.parse_args(argv)
    if (not args.out_dir.parent.is_dir()
            or (args.out_dir / "report.json").exists()):
        parser.error("output parent must exist and report must not already exist")
    if args.mode == "hosted" and (
            args.jev_env_file is None or args.max_new_calls < 1):
        parser.error("hosted mode requires an external Jev key file and explicit call budget")
    try:
        timeout = _validate_timeout(args.provider_timeout)
    except ValueError:
        parser.error("provider timeout must be a finite positive number")
    try:
        bank = json.loads(phase3_paths.APPROVED_BANK.read_text(encoding="utf-8"))
        taxonomy = json.loads(phase3_paths.ASSESSMENT_TAXONOMY.read_text(encoding="utf-8"))
        names = None if args.all_scenarios else (args.scenario or [DEFAULT_SCENARIO])
        snapshot = prepare_snapshot(args.source, args.evidence_inputs, bank, taxonomy, names)
    except Exception:
        parser.error("synthetic inputs or their frozen diagnostic bindings are invalid")
    args.out_dir.mkdir(exist_ok=True)
    _write_or_match(args.out_dir / "inputs.json", snapshot)
    selector, generator, model, endpoint_hash = None, None, None, None
    if args.mode == "hosted":
        try:
            # An explicitly supplied replacement key file wins over exported keys.
            native_selector = EvidenceJevSelector(
                _read_env_file(args.jev_env_file), timeout=timeout)
            inner_generator = AittaGenerator.from_env()
            inner_generator._timeout = timeout
            native_generator = InstrumentedEvidenceAittaGenerator(inner_generator)
            model = inner_generator._model
            endpoint_hash = hashlib.sha256(
                inner_generator._endpoint.encode("utf-8")).hexdigest()
        except Exception:
            parser.error("provider configuration is invalid; secrets are not displayed")
        cache_dir = args.out_dir / "provider_captures"
        cache_dir.mkdir(exist_ok=True)
        cache = CaptureCache(cache_dir, args.max_new_calls)
        if args.seed_captures:
            seeded = seed_successes(cache, args.seed_captures, model, endpoint_hash)
            print(f"Reused {seeded} successful captures; failed captures were not seeded.",
                  flush=True)
        selector = CachedReviewSelector(native_selector, cache)
        generator = CachedReviewOpening(native_generator, cache, model, endpoint_hash)
    contract = _contract(snapshot, args.mode, model, endpoint_hash, timeout)
    _write_or_match(args.out_dir / "contract.json", contract)
    _write_or_match(args.out_dir / "request_plan.json", request_plan(snapshot))
    print(json.dumps(request_plan(snapshot), indent=2), flush=True)
    report = replay(snapshot, args.out_dir, contract, selector, generator)
    _require(_file_hash(args.source) == snapshot["source"]["sha256"]
             and _file_hash(args.evidence_inputs) == snapshot["evidence_inputs"]["sha256"],
             "source changed during replay; refusing report publication")
    write_new(args.out_dir / "report.json", report)
    print(json.dumps(report["summary"], indent=2), flush=True)


if __name__ == "__main__":
    main()
