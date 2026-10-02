"""Replay frozen synthetic observed counts, never the bank, responses, or KT.

Identical provider requests share captured responses. This tests integration
coverage, not independent rerun stability, educational quality, or learning.
"""
import argparse
import copy
import hashlib
import json
import os
import time
from pathlib import Path

from aitta_generator import AittaGenerator, _validated_generation_payload
from jev_selector import (
    CRITERIA, MODEL, SELECTION_INSTRUCTIONS, SELECTION_PROMPT_VERSION,
    JevSelector, _validated_selection_payload)
from synthetic_feedback import (
    CANDIDATE_VERSION, GENERATION_INSTRUCTIONS, GENERATION_SCHEMA, INPUT_SCHEMA,
    POLICY_VERSION, PROMPT_VERSION, SELECTION_SCHEMA, _validate_input,
    run_synthetic_feedback)

SKILL_ALIASES = {
    "skill_5918fabc5b75": ("skill_a", "Arithmetic", "Peruslaskutoimitukset"),
    "skill_82e4c6d79fe0": ("skill_b", "Prices", "Hinta"),
    "skill_304b0fda845f": ("skill_c", "Fractions", "Murtoluvut"),
    "skill_91bcb30d3f0b": ("skill_d", "Percentages", "Prosenttilaskenta"),
}
ROLES = (("student", "midpoint"), ("student", "end"), ("teacher", "end"))


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


def write_new(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True)


def freeze(source):
    raw = source.read_bytes()
    original = json.loads(raw.decode("utf-8"))
    if (original.get("schema") != "phase3_fixed40_comparison_v1"
            or original.get("scope") !=
            "private_synthetic_fixed_40_cold_start_not_student_validation"):
        raise ValueError("only the saved synthetic fixed-40 artifact is allowed")
    scenarios = original["scenarios"]
    if len(scenarios) != 54 or len({s["name"] for s in scenarios}) != 54:
        raise ValueError("expected all 54 distinct scenario labels")
    order = [r["question_id"] for r in scenarios[0]["responses"]]
    cases = []
    for case in scenarios:
        rows = case["responses"]
        if (len(rows) != 40
                or [r["position"] for r in rows] != list(range(1, 41))
                or [r["question_id"] for r in rows] != order
                or any(type(r["correct"]) is not bool for r in rows)):
            raise ValueError("saved response contract differs across scenarios")
        observed = {}
        for checkpoint, limit in (("midpoint", 20), ("end", 40)):
            feed = case["observed"][checkpoint]
            skills = feed["skills"]
            if ([s["skill_id"] for s in skills] != list(SKILL_ALIASES)
                    or len(skills) != 4):
                raise ValueError("saved skill order does not match the explicit mapping")
            public = []
            for skill in skills:
                sid = skill["skill_id"]
                alias, name, original_name = SKILL_ALIASES[sid]
                matched = [r for r in rows[:limit] if r["skill_id"] == sid]
                if (skill["skill_name"] != original_name
                        or type(skill["correct"]) is not int
                        or type(skill["out_of"]) is not int
                        or (skill["correct"], skill["out_of"]) !=
                        (sum(r["correct"] for r in matched), len(matched))):
                    raise ValueError("saved observed counts disagree with response labels")
                public.append({"skill_id": alias, "skill_name": name,
                               "correct": skill["correct"], "out_of": skill["out_of"]})
            if checkpoint == "midpoint" and "total" in feed:
                raise ValueError("saved midpoint unexpectedly includes a total")
            if checkpoint == "end" and feed["total"] != {
                    "correct": sum(s["correct"] for s in public), "out_of": 40}:
                raise ValueError("saved end total disagrees with skill counts")
            _validate_input({"schema": INPUT_SCHEMA, "data_origin": "synthetic",
                             "audience": "student", "checkpoint": checkpoint,
                             "skills": public})
            observed[checkpoint] = public
        cases.append({"name": case["name"], "group": case["group"],
                      "profile": case["profile"], "seed": case["seed"],
                      "skills": observed})
    return {"schema": "phase3_frozen_provider_inputs_v1",
            "source_file": source.name, "source_sha256": hashlib.sha256(raw).hexdigest(),
            "scope": "synthetic_observed_counts_only",
            "validation": "counts_checked_against_saved_labels_not_private_answer_keys",
            "cases": cases}


def selection_request(payload):
    state = _validated_selection_payload(payload)
    return {"model": MODEL, "state": state, "questions": {"feedback_candidate": {
        "type": "choice", "instructions": SELECTION_INSTRUCTIONS,
        "criteria": {c["candidate_id"]: CRITERIA[c["candidate_id"]]
                     for c in state["candidates"]}}}}


def generation_request(payload, model):
    normalized = _validated_generation_payload(payload)
    selected = normalized["selected_candidate"]
    return {"model": model, "instructions": GENERATION_INSTRUCTIONS,
            "max_tokens": 1024, "reasoning_effort": "low",
            "response_format": {"type": "json_object"},
            "context": {"audience": normalized["audience"],
                        "checkpoint": normalized["checkpoint"],
                        "selected_candidate": {k: selected[k] for k in
                                               ("candidate_id", "strategy", "review_status")}}}


class CaptureCache:
    def __init__(self, root, budget):
        self.root, self.budget, self.new_calls = root, budget, 0

    def path(self, provider, request):
        return self.root / f"{provider}_{digest(request)}.json"

    def obtain(self, provider, request, callback, adapter):
        path = self.path(provider, request)
        reused = path.exists()
        if reused:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["provider"] != provider or record["request"] != request:
                raise ValueError("cache request does not match its capture")
        else:
            if self.new_calls >= self.budget:
                raise SystemExit("new-call budget exhausted; captures retained for resume")
            self.new_calls += 1
            start = time.perf_counter()
            try:
                reply = callback()
                ok = True
            except Exception:
                reply, ok = None, False
            record = {"provider": provider, "request": request, "ok": ok,
                      "reply": reply, "metadata": adapter.last_metadata,
                      "original_call_latency_ms":
                          round((time.perf_counter() - start) * 1000, 3),
                      "origin": "hosted_replay"}
            write_new(path, record)
            print(f"Captured {provider} call {self.new_calls}: "
                  f"{'completed' if ok else 'failed'}", flush=True)
        execution = {"capture_file": path.name, "reused": reused,
                     "origin": record["origin"], "metadata": record["metadata"],
                     "original_call_latency_ms": record["original_call_latency_ms"]}
        if not record["ok"]:
            return None, execution
        return copy.deepcopy(record["reply"]), execution


class CachedSelector:
    def __init__(self, adapter, cache):
        self.adapter, self.cache, self.execution = adapter, cache, None

    def select(self, payload):
        request = selection_request(payload)
        candidates = request["state"]["candidates"]
        if len(candidates) == 1:
            self.execution = {"status": "local_single_candidate", "reused": False}
            return {"candidate_id": candidates[0]["candidate_id"]}
        reply, self.execution = self.cache.obtain(
            "jev", request, lambda: self.adapter.select(payload), self.adapter)
        if reply is None:
            raise RuntimeError("captured_selector_failure")
        return reply


class CachedGenerator:
    def __init__(self, adapter, cache):
        self.adapter, self.cache, self.execution = adapter, cache, None

    def generate(self, payload):
        request = generation_request(payload, self.adapter._model)
        reply, self.execution = self.cache.obtain(
            "aitta", request, lambda: self.adapter.generate(payload), self.adapter)
        if reply is None:
            raise RuntimeError("captured_generator_failure")
        return reply


def seed_demo(cache, path, generator):
    """Reuse v2 smoke responses only when their full effective request matches."""
    demo = json.loads(path.read_text(encoding="utf-8"))
    if demo["mode"] != "jev_and_aitta":
        raise ValueError("seed must be a combined hosted capture")
    for package in demo["packages"]:
        review = package["review"]
        if (review["trace"]["prompt_version"] != PROMPT_VERSION
                or review["trace"]["fallback_reason"] is not None):
            raise ValueError("seed prompt version or validation does not match")
        selected = next(c for c in review["candidates"]
                        if c["candidate_id"] == review["selected_candidate_id"])
        common = {"audience": review["audience"], "checkpoint": review["checkpoint"],
                  "evidence": review["sanitized_evidence"]}
        payload = dict(common, schema=GENERATION_SCHEMA,
                       selected_candidate=selected, prompt_version=PROMPT_VERSION,
                       instructions=GENERATION_INSTRUCTIONS)
        metadata = package["generator_metadata"]
        if metadata["model_version"] != generator._model:
            raise ValueError("seed model does not match configured Aitta model")
        request = generation_request(payload, generator._model)
        target = cache.path("aitta", request)
        if not target.exists():
            write_new(target, {"provider": "aitta", "request": request, "ok": True,
                              "reply": {"candidate_id": selected["candidate_id"],
                                        "opening": review["message"]["opening"]},
                              "metadata": metadata, "original_call_latency_ms": None,
                              "origin": path.name})


def evidence_for(case, audience, checkpoint):
    return {"schema": INPUT_SCHEMA, "data_origin": "synthetic",
            "audience": audience, "checkpoint": checkpoint,
            "skills": copy.deepcopy(case["skills"][checkpoint])}


def prepare(source, output):
    if not output.exists():
        output.mkdir()
    snapshot_path = output / "inputs.json"
    if snapshot_path.exists():
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if snapshot["schema"] != "phase3_frozen_provider_inputs_v1":
            raise ValueError("unexpected snapshot schema")
    else:
        snapshot = freeze(source)
        write_new(snapshot_path, snapshot)
    contract = {"snapshot_sha256": digest(snapshot),
                "candidate_version": CANDIDATE_VERSION, "policy_version": POLICY_VERSION,
                "selection_prompt_version": SELECTION_PROMPT_VERSION,
                "generation_prompt_version": PROMPT_VERSION,
                "generation_instructions_sha256": digest(GENERATION_INSTRUCTIONS)}
    contract_path = output / "contract.json"
    if contract_path.exists():
        if json.loads(contract_path.read_text(encoding="utf-8")) != contract:
            raise ValueError("contract changed; use a new output directory")
    else:
        write_new(contract_path, contract)
    baseline_path = output / "baselines.json"
    if not baseline_path.exists():
        write_new(baseline_path, [
            {"scenario": case["name"], "audience": audience, "checkpoint": checkpoint,
             "review": run_synthetic_feedback(evidence_for(case, audience, checkpoint))}
            for case in snapshot["cases"] for audience, checkpoint in ROLES])
    vectors = {tuple(s["correct"] for s in c["skills"]["end"])
               for c in snapshot["cases"]}
    print(f"Frozen {len(snapshot['cases'])} scenario labels; "
          f"{len(snapshot['cases']) * len(ROLES)} packages; "
          f"{len(vectors)} distinct end count vectors.", flush=True)
    print(f"At most {2 * len(vectors)} distinct Jev requests and "
          "5 distinct Aitta contexts, before reusing smoke captures.", flush=True)
    return snapshot, contract


def replay(snapshot, contract, output, selector, generator):
    package_dir = output / "packages"
    if not package_dir.exists():
        package_dir.mkdir()
    packages = []
    for case_index, case in enumerate(snapshot["cases"]):
        for role_index, (audience, checkpoint) in enumerate(ROLES):
            package_path = package_dir / f"{case_index:03d}_{role_index}.json"
            if package_path.exists():
                package = json.loads(package_path.read_text(encoding="utf-8"))
                if (package["contract"] != contract
                        or (package["scenario"], package["audience"], package["checkpoint"]) !=
                        (case["name"], audience, checkpoint)):
                    raise ValueError("saved package identity or contract changed")
            else:
                selector.execution = None
                generator.execution = None
                review = run_synthetic_feedback(
                    evidence_for(case, audience, checkpoint), selector, generator)
                baseline = review["template_baseline"]
                if (review["selected_candidate_id"] == baseline["candidate_id"]
                        and review["message"]["evidence_lines"] != baseline["evidence_lines"]):
                    raise ValueError("rendered counts changed under the same strategy")
                if checkpoint == "midpoint" and (
                        review["sanitized_evidence"] or review["message"]["evidence_lines"]):
                    raise ValueError("midpoint evidence boundary was violated")
                package = {"scenario": case["name"], "group": case["group"],
                           "audience": audience, "checkpoint": checkpoint,
                           "contract": contract, "review": review,
                           "selector_execution": copy.deepcopy(selector.execution),
                           "generator_execution": copy.deepcopy(generator.execution)}
                write_new(package_path, package)
            packages.append(package)
        print(f"Saved scenario {case_index + 1}/{len(snapshot['cases'])}: "
              f"{case['name']}", flush=True)
    end = [p for p in packages if p["checkpoint"] == "end"]
    summary = {
        "scenario_labels": len(snapshot["cases"]), "feedback_packages": len(packages),
        "end_packages": len(end),
        "end_selection_matches_rules": sum(
            p["review"]["selected_candidate_id"] ==
            p["review"]["template_baseline"]["candidate_id"] for p in end),
        "fallback_packages": sum(
            p["review"]["trace"]["fallback_reason"] is not None for p in packages),
        "new_provider_calls_this_invocation": selector.cache.new_calls,
        "independent_provider_rerun_stability_tested": False,
        "educator_preference_labels_available": False,
        "educational_effectiveness_tested": False,
    }
    report = {"schema": "phase3_provider_replay_review_v1",
              "scope": "private_synthetic_review_not_learner_delivery",
              "source": {"file": snapshot["source_file"],
                         "sha256": snapshot["source_sha256"]},
              "contract": contract,
              "reuse_policy": "identical_effective_requests_share_captured_responses_including_failures",
              "latency_warning": "cached pipeline latency is replay overhead, not hosted latency",
              "summary": summary, "packages": packages}
    write_new(output / "report.json", report)
    print(json.dumps(summary, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(
        "artifacts/assessment_pipeline_20261001.json"))
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("prepare", "hosted"), default="prepare")
    parser.add_argument("--jev-env-file", type=Path)
    parser.add_argument("--seed-demo", type=Path)
    parser.add_argument("--max-new-calls", type=int, default=0)
    args = parser.parse_args()
    if not args.out_dir.parent.is_dir():
        parser.error("output parent must already exist")
    if args.mode == "hosted" and (
            args.jev_env_file is None or args.max_new_calls < 1):
        parser.error("hosted mode requires an external Jev key file and an explicit call budget")
    if (args.out_dir / "report.json").exists():
        parser.error("report already exists; no provider requests will be made")
    snapshot, contract = prepare(args.source, args.out_dir)
    if args.mode == "prepare":
        return
    # Force the explicit replacement-key file rather than a stale exported key.
    os.environ.pop("TYPESAFE_API_KEY", None)
    try:
        native_selector = JevSelector.from_env(args.jev_env_file)
        native_generator = AittaGenerator.from_env()
    except Exception:
        parser.error("provider configuration is invalid; secret values are not displayed")
    cache_dir = args.out_dir / "provider_captures"
    if not cache_dir.exists():
        cache_dir.mkdir()
    cache = CaptureCache(cache_dir, args.max_new_calls)
    if args.seed_demo:
        seed_demo(cache, args.seed_demo, native_generator)
    replay(snapshot, contract, args.out_dir,
           CachedSelector(native_selector, cache),
           CachedGenerator(native_generator, cache))


if __name__ == "__main__":
    main()
