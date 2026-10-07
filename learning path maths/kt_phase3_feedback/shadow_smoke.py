"""Freeze and run a small native, private endpoint recommendation smoke study."""
import argparse
import copy
import hashlib
import importlib
import json
import sys
import time
from collections import Counter
from pathlib import Path

import phase3_paths
import paths
from evidence_feedback import build_research_evidence, canonical_digest, run_feedback
from feedback_service import validate_assessment_taxonomy
from kt_adapter import build_batch, response_events
from scenario_runner import generate_responses
from session_store import bank_fingerprint
from shadow_practice import (
    POLICY_VERSION, QUESTION_KEYS, ShadowPracticeRecommender, normalized_prompt,
    select_predictions, validate_practice_pool)


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
PHASE2 = PROJECT / "kt_phase2_inference" / "artifacts"
PHASE4 = PROJECT / "kt_phase4_evaluation"
BANKS = PHASE2 / "evaluation_banks_support_optimal_20261005"
DEFAULT_CAPTURE = ROOT / "artifacts" / "shadow_smoke_20261006"
BAND = (0.60, 0.80)
QUOTAS = {"skill_3697ce51cadb": 6, "skill_8aaddb61bde0": 2,
          "skill_b986b3d2a5d8": 6, "skill_e8d49a51337a": 6}
PATTERNS = ("all_correct", "all_incorrect", "only_fractions_weak",
            "global_alternating", "stable_strong_s11")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def taxonomy(bank):
    result = {
        "schema": "phase3_assessment_taxonomy_draft_v1",
        "status": "approved_for_descriptive_research_prototype",
        "student_feedback_status": "observed_counts_only_no_subtopic_mastery_or_conformal",
        "relation": "is_part_of_not_prerequisite", "bank_fingerprint": bank_fingerprint(bank),
        "topics": [
            {"skill_id": sid, "skill_name": name,
             "subtopics": [{"id": "research_topic_" + sid, "name": name,
                           "question_ids": [q["question_id"] for q in bank["questions"]
                                            if q["skill_id"] == sid]}]}
            for sid, name in bank["skill_names"].items()],
    }
    validate_assessment_taxonomy(bank, result)
    return result


def cases(bank):
    result = []
    for pattern in PATTERNS:
        if pattern == "stable_strong_s11":
            generated = generate_responses(bank, {"profile": "stable_strong", "seed": 11})
            rows = [{"question_id": r["question_id"], "selected_index": r["selected_index"]}
                    for r in generated]
        else:
            rows = []
            for i, q in enumerate(bank["questions"]):
                correct = (pattern == "all_correct"
                           or pattern == "only_fractions_weak" and q["skill_id"] != "skill_e8d49a51337a"
                           or pattern == "global_alternating" and i % 2 == 0)
                selected = q["answer_index"] if correct else (q["answer_index"] + 1) % len(q["options"])
                rows.append({"question_id": q["question_id"], "selected_index": selected})
        result.append({"name": pattern, "responses": rows})
    return result


def select_pool(rows, banks, demo, vocab):
    excluded = [q for b in (*banks.values(), demo) for q in b["questions"]]
    ids = {q["question_id"] for q in excluded}
    prompts = {normalized_prompt(q["text"]) for q in excluded}
    items = {q["item_id"] for q in excluded}
    eligible = []
    for row in rows:
        q = row["question"]
        if q["skill_id"] not in QUOTAS:
            continue
        if row["status"] != "accepted" or not row["mathematical_basis"] or not row["embedding_rows"]:
            raise ValueError("Unreviewed candidate in closed pool")
        if (vocab["item_vocab"].get(q["item_id"], 1) == 1) != (row["regime"] == "cold"):
            raise ValueError("Candidate vocabulary regime mismatch")
        if q["question_id"] in ids or normalized_prompt(q["text"]) in prompts or q["item_id"] in items:
            continue
        eligible.append(row)
    selected, seen = [], set()
    for skill, quota in QUOTAS.items():
        count = 0
        ordered = sorted((r for r in eligible if r["question"]["skill_id"] == skill),
                         key=lambda r: (-r["support"]["students"], r["question"]["question_id"]))
        for row in ordered:
            prompt = normalized_prompt(row["question"]["text"])
            if prompt in seen:
                continue
            seen.add(prompt)
            selected.append(row)
            count += 1
            if count == quota:
                break
        if count != quota:
            raise ValueError("Insufficient disjoint reviewed practice content")
    pool = {"schema": "phase3_shadow_practice_pool_v1",
            "scope": "research_only_not_learner_approved",
            "questions": [{k: row["question"][k] for k in sorted(QUESTION_KEYS | {"answer_index"})}
                          for row in selected]}
    return validate_practice_pool(pool), selected


def prepare(capture):
    if capture.exists():
        raise FileExistsError("Refusing to overwrite frozen smoke preparation")
    certificate = load(BANKS / "optimality_certificate.json")
    closed = PHASE2 / "global_bank_closed_review_20261005" / "closed_pool_private.json"
    if sha(closed) != certificate["inputs"]["closed_pool"]["sha256"]:
        raise ValueError("Closed review changed")
    source_paths = {"closed_pool": closed, "protocol": ROOT / "SHADOW_SMOKE_PROTOCOL.md"}
    banks = {}
    for regime in ("warm", "cold"):
        p = BANKS / f"{regime}_bank_private.json"
        if sha(p) != certificate["bank_hashes"][regime]["sha256"]:
            raise ValueError("Final bank changed")
        banks[regime] = load(p)
        source_paths[regime + "_bank"] = p
    source_paths.update({"vocab": paths.VOCAB, "checkpoint": paths.CHECKPOINT,
                         "config": paths.RUN_CONFIG, "embeddings": paths.TEXT_EMBEDDINGS,
                         "mcq_scoring": PROJECT / "kt_phase2_inference" / "mcq_test.py",
                         "kt_adapter": ROOT / "kt_adapter.py",
                         "taxonomy_validator": ROOT / "feedback_service.py",
                         "shared_loader": PHASE4 / "memory_loader.py"})
    phase4_manifest = load(PHASE4 / "artifacts" / "selected_bank_v3_20261005" / "replay_manifest.json")
    phase4_report = load(PHASE4 / "artifacts" / "selected_bank_v3_20261005" / "inference_report.json")
    hashes = {}
    for name, path in source_paths.items():
        print("Hashing " + name, flush=True)
        hashes[name] = {"path": str(path.relative_to(PROJECT)), "sha256": sha(path)}
        if name in ("vocab", "checkpoint", "config") and hashes[name]["sha256"] != phase4_manifest["inputs"][name]["sha256"]:
            raise ValueError("Frozen model input differs from Phase 4")
    # Embedding provenance is also retained by Phase 4 inference.
    expected_embedding = phase4_report["embeddings"]
    if hashes["embeddings"]["sha256"] != expected_embedding["sha256"]:
        raise ValueError("Embedding table differs from frozen inference reference")
    demo_path = PHASE2 / "test_question_bank_text_only_approved_v2.json"
    hashes["demo"] = {"path": str(demo_path.relative_to(PROJECT)), "sha256": sha(demo_path)}
    pool, selected = select_pool(load(closed), banks, load(demo_path), load(paths.VOCAB))
    frozen = {"practice_pool_private.json": pool, "pool_review_private.json": selected}
    for regime, bank in banks.items():
        tax = taxonomy(bank)
        members = cases(bank)
        for case in members:
            build_research_evidence(bank, tax, case["responses"])
        frozen.update({regime + "_bank_private.json": bank,
                       regime + "_taxonomy.json": tax, regime + "_cases_private.json": members})
    capture.mkdir(parents=False)
    for name, data in frozen.items():
        write(capture / name, data)
    write(capture / "preparation_manifest.json", {
        "protocol": "endpoint_shadow_smoke_v1_20261006", "source_hashes": hashes,
        "frozen_hashes": {name: sha(capture / name) for name in frozen},
        "methods": {name: sha(ROOT / name) for name in
                    ("shadow_smoke.py", "future_kt.py", "shadow_practice.py", "evidence_feedback.py")},
        "policy_version": POLICY_VERSION, "target_band": list(BAND), "patterns": list(PATTERNS),
        "pool_topic_counts": dict(Counter(q["skill_id"] for q in pool["questions"])),
        "pool_item_regimes": dict(Counter(r["regime"] for r in selected)),
        "scope": "synthetic_endpoint_smoke_not_108_study_or_learner_validation",
    })
    print("Prepared private smoke capture: " + str(capture), flush=True)


def native_blind_check(kt, bank, responses, pool, recommendation):
    """Compare one query with nonneutral current and arbitrary future response fields."""
    row = recommendation["candidate_predictions"][0]
    q = next(q for q in pool["questions"] if q["question_id"] == row["question_id"])
    observed = response_events(bank, responses)
    mutant = {"question": q, "selected_text": q["options"][-1], "correct": True,
              "response_time_ms": 98765, "attempt": 7, "time_bin": 31}
    gaps = []
    for tail in ([mutant], [mutant, copy.deepcopy(mutant)]):
        batch = build_batch(bank, observed + tail, kt)
        probability = float(kt.probs(batch)[0, 40])
        gaps.append(abs(probability - row["p_correct"]))
    return {"maximum_gap": max(gaps), "tolerance": 1e-6,
            "passed": max(gaps) <= 1e-6}


def run(capture):
    output = capture / "smoke_results_private.json"
    if output.exists():
        raise FileExistsError("Refusing to overwrite native smoke results")
    manifest = load(capture / "preparation_manifest.json")
    for name, expected in manifest["frozen_hashes"].items():
        if sha(capture / name) != expected:
            raise ValueError("Frozen smoke content changed")
    for name, expected in manifest["methods"].items():
        if sha(ROOT / name) != expected:
            raise ValueError("Smoke method changed after preparation")
    for source in manifest["source_hashes"].values():
        print("Checking source " + source["path"], flush=True)
        if sha(PROJECT / source["path"]) != source["sha256"]:
            raise ValueError("Frozen source changed")
    psutil = importlib.import_module("psutil")
    if psutil.virtual_memory().available < 3 * 1024 ** 3:
        raise RuntimeError("Need at least 3 GiB free; no processes will be stopped")
    torch = importlib.import_module("torch")
    torch.set_num_threads(2)
    if str(PHASE4) not in sys.path:
        sys.path.insert(0, str(PHASE4))
    loader = importlib.import_module("memory_loader").load_memory_efficient_model
    print("Loading frozen model/shared embeddings, no training...", flush=True)
    started = time.perf_counter()
    kt = loader(PHASE4 / "artifacts" / "selected_bank_v3_20261005" / "replay_windows_private.jsonl.gz")
    loading_seconds = time.perf_counter() - started
    print("Model loaded: " + kt.describe(), flush=True)
    predictor = lambda bank, rows, candidates: importlib.import_module(
        "future_kt").predict_future_candidates(bank, rows, candidates, kt)
    pool = load(capture / "practice_pool_private.json")
    results, faults = [], []
    for regime in ("warm", "cold"):
        bank = load(capture / f"{regime}_bank_private.json")
        tax = load(capture / f"{regime}_taxonomy.json")
        members = load(capture / f"{regime}_cases_private.json")
        recommender = ShadowPracticeRecommender(pool, manifest["target_band"], predictor)
        for case in members:
            evidence = build_research_evidence(bank, tax, case["responses"])
            feedback = run_feedback(evidence, "student", "end")
            start = time.perf_counter()
            rec = recommender.recommend_research(bank, tax, case["responses"])
            elapsed = time.perf_counter() - start
            unchanged = feedback == run_feedback(
                build_research_evidence(bank, tax, case["responses"]), "student", "end")
            check = (native_blind_check(kt, bank, case["responses"], pool, rec)
                     if rec.get("candidate_predictions") else None)
            selected = rec.get("selected_question_id")
            valid = rec["status"] != "unavailable" and unchanged
            if selected:
                chosen = next(r for r in rec["candidate_predictions"] if r["question_id"] == selected)
                valid = valid and manifest["target_band"][0] <= chosen["p_correct"] <= manifest["target_band"][1]
                valid = valid and next(s for s in evidence["skills"] if s["skill_id"] == chosen["skill_id"])["incorrect"] > 0
            if check is not None:
                valid = valid and check["passed"]
            valid = valid and not rec["used_for_student_advice"] and not rec["used_for_feedback"]
            results.append({
                "regime": regime, "name": case["name"], "observed_total": evidence["total"],
                "recommendation": rec, "post_load_seconds": elapsed,
                "feedback_sha256": canonical_digest(feedback), "feedback_unchanged": unchanged,
                "native_blind_check": check, "software_checks_passed": bool(valid),
            })
            print(f"{regime}/{case['name']}: {rec['status']} checks={bool(valid)}", flush=True)
        all_wrong = next(c for c in members if c["name"] == "all_incorrect")
        empty = ShadowPracticeRecommender(
            {**pool, "questions": []}, manifest["target_band"],
            lambda *args: (_ for _ in ()).throw(AssertionError("empty pool must not predict")))
        empty_result = empty.recommend_research(bank, tax, all_wrong["responses"])
        evidence = build_research_evidence(bank, tax, all_wrong["responses"])
        injected = {"predictions": [
            {"question_id": q["question_id"], "skill_id": q["skill_id"],
             "item_id": q["item_id"],
             "regime": "cold" if kt.item_vocab.get(q["item_id"], 1) == 1 else "warm",
             "p_correct": 1.0}
            for q in pool["questions"]]}
        outside = select_predictions(pool["questions"], injected,
                                     {s["skill_id"]: s for s in evidence["skills"]},
                                     manifest["target_band"])
        faults.append({"regime": regime, "kind": "injected_software_controls_not_native_predictions",
                       "empty_pool_abstained": empty_result["status"] == "abstained",
                       "all_above_band_abstained": outside["selected_question_id"] is None})
    summary = {}
    for regime in ("warm", "cold"):
        members = [r for r in results if r["regime"] == regime]
        selected = [r for r in members if r["recommendation"]["status"] == "selected"]
        summary[regime] = {
            "cases": len(members),
            "status_counts": dict(Counter(r["recommendation"]["status"] for r in members)),
            "reason_counts": dict(Counter(r["recommendation"]["reason"] or "selected" for r in members)),
            "selected_matching_baseline": sum(r["recommendation"]["selected_question_id"] ==
                                               r["recommendation"]["baseline_question_id"] for r in selected),
            "native_queries": sum(len(r["recommendation"].get("candidate_predictions", [])) for r in members),
        }
    passed = (all(r["software_checks_passed"] for r in results)
              and all(f["empty_pool_abstained"] and f["all_above_band_abstained"] for f in faults))
    write(output, {"status": "passed_smoke_checks" if passed else "smoke_checks_failed",
                   "scope": manifest["scope"], "preparation_sha256": sha(capture / "preparation_manifest.json"),
                   "loading_seconds": loading_seconds, "device": "cpu", "threads": 2,
                   "summary": summary, "cases": results, "separate_fault_controls": faults})
    print(json.dumps(summary, indent=2), flush=True)
    if not passed:
        raise RuntimeError("Smoke failures preserved; do not enable student delivery")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--capture-dir", type=Path, default=DEFAULT_CAPTURE)
    args = parser.parse_args()
    if str(PHASE4) not in sys.path:
        sys.path.insert(0, str(PHASE4))
    (prepare if args.command == "prepare" else run)(args.capture_dir)
