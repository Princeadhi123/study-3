"""Research-only paired sensitivity experiments over a locally approved MCQ bank."""
import argparse
import copy
import json
import random
import statistics
from collections import Counter
from pathlib import Path

import phase3_paths
from kt_adapter import coverage_report, trace_responses
from mcq_service import MCQSessionService
from scenario_runner import DETERMINISTIC, PROBABILISTIC, run_scenario
from session_store import SessionStore, bank_fingerprint


def reordered_bank(bank: dict, seed: int) -> dict:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("order seed must be an integer")
    candidate = copy.deepcopy(bank)
    rng = random.Random(seed)
    first = candidate["questions"][:20]
    second = candidate["questions"][20:]
    rng.shuffle(first)
    rng.shuffle(second)
    candidate["questions"] = first + second
    candidate["review_status"] = "research_variant_not_approved_for_student_delivery"
    return candidate


def unknown_item_bank(bank: dict, kt) -> dict:
    candidate = copy.deepcopy(bank)
    for question in candidate["questions"]:
        unseen = "__RESEARCH_UNSEEN__" + question["question_id"]
        if unseen in kt.item_vocab:
            raise ValueError("research item ID unexpectedly exists in model vocabulary")
        question["item_id"] = unseen
    candidate["review_status"] = "research_variant_not_approved_for_student_delivery"
    return candidate


def _ordered_rows(original: list[dict], bank: dict) -> list[dict]:
    lookup = {row["question_id"]: row for row in original}
    if len(lookup) != 40:
        raise ValueError("paired answers require 40 distinct question IDs")
    attempts = Counter()
    rows = []
    for position, question in enumerate(bank["questions"], 1):
        row = lookup.pop(question["question_id"])
        attempts[row["skill_id"]] += 1
        rows.append({**row, "position": position, "half": 1 if position <= 20 else 2,
                     "skill_attempt": attempts[row["skill_id"]]})
    if lookup:
        raise ValueError("research variant changed the question set")
    return rows


def _checked_observed(rows: list[dict], observed: dict) -> None:
    for label, limit in (("midpoint", 20), ("end", 40)):
        expected = {skill["skill_id"]: skill["total"]
                    for skill in observed[label]["teacher"]["skills"]}
        actual = {sid: {"correct": sum(r["correct"] for r in rows[:limit]
                                       if r["skill_id"] == sid),
                        "out_of": sum(r["skill_id"] == sid for r in rows[:limit])}
                  for sid in expected}
        if actual != expected:
            raise ValueError("paired variant changed observed skill counts")


def _diagnostics(bank: dict, rows: list[dict], trace: dict, gate, midpoint_gate) -> dict:
    probabilities = trace["p_correct_before_each_answer"]
    if len(probabilities) != 40 or gate.k != 10 or midpoint_gate.k != 5:
        raise ValueError("research matrix requires 40 predictions and paired k=5/k=10 gates")
    unknown = set(trace["coverage"]["unknown_item_ids"])
    regimes = ["cold" if q["item_id"] in unknown else "warm"
               for q in bank["questions"]]
    items = []
    for position, (p, regime) in enumerate(zip(probabilities, regimes), 1):
        decision = gate.item_decision(p, regime)
        items.append({"position": position, "regime": regime,
                      "prediction_set": list(decision.prediction_set),
                      "status": decision.status.value})
    checkpoints = {}
    for label, count, selected_gate in (("midpoint", 20, midpoint_gate),
                                        ("end", 40, gate)):
        checkpoints[label] = {}
        for sid in bank["skill_names"]:
            indexes = [i for i, row in enumerate(rows[:count]) if row["skill_id"] == sid]
            regime = "cold" if any(regimes[i] == "cold" for i in indexes) else "warm"
            checkpoints[label][sid] = selected_gate.checkpoint(
                [probabilities[i] for i in indexes], sid, regime=regime).to_dict()
    return {"status": "exploratory_only", "midpoint_calibrated_k": 5,
            "end_calibrated_k": 10,
            "midpoint_status": "historical_k5_not_validated_for_fixed_bank",
            "end_status": "historical_k10_not_validated_for_fixed_bank",
            "items": items, "checkpoints": checkpoints}


def _comparison(baseline: dict, rows: list[dict], trace: dict,
                diagnostics: dict) -> dict:
    reference = {row["question_id"]: (baseline["kt"]["p_correct_before_each_answer"][i],
                                      baseline["conformal"]["items"][i]["status"])
                 for i, row in enumerate(baseline["responses"])}
    paired = [(reference[row["question_id"]],
               trace["p_correct_before_each_answer"][i],
               diagnostics["items"][i]["status"])
              for i, row in enumerate(rows)]
    return {
        "mean_signed_kt_change": statistics.mean(p - old[0] for old, p, _ in paired),
        "mean_absolute_kt_change": statistics.mean(abs(p - old[0]) for old, p, _ in paired),
        "item_status_changes": sum(old[1] != status for old, _, status in paired),
        "checkpoint_status_changes": sum(
            baseline["conformal"]["checkpoints"][label][sid]["status"] !=
            diagnostics["checkpoints"][label][sid]["status"]
            for label in ("midpoint", "end") for sid in diagnostics["checkpoints"][label]),
    }


def run_matrix(bank: dict, spec: dict, store: SessionStore, kt, gate,
               midpoint_gate, graph=None, order_seed: int = 17,
               taxonomy: dict | None = None) -> dict:
    if gate.k != 10 or midpoint_gate.k != 5:
        raise ValueError("research matrix needs matching historical k=5 and k=10 gates")
    coverage = coverage_report(bank, kt)
    if (coverage["missing_skill_indexes"] or coverage["missing_content_indexes"]
            or coverage["missing_option_values"]):
        raise ValueError("research bank is not KT-text-compatible")
    baseline = run_scenario(bank, spec, store, kt=kt, gate=gate,
                            midpoint_gate=midpoint_gate, graph=graph,
                            taxonomy=taxonomy)
    rows = baseline["responses"]
    if len(rows) != 40:
        raise ValueError("research matrix requires 40 baseline answers")
    history = []
    for correct in (True, False):
        history.append([{"question_id": q["question_id"],
                         "selected_index": (q["answer_index"] if correct else
                                            (q["answer_index"] + 1) % len(q["options"]))}
                        for q in bank["questions"][:20]])
    variants = []
    for kind, candidate, previous in (
            ("within_half_order", reordered_bank(bank, order_seed), None),
            ("repeated_items_prior_correct", bank, history[0]),
            ("repeated_items_prior_incorrect", bank, history[1]),
            ("unknown_item_ids_same_text", unknown_item_bank(bank, kt), None)):
        ordered = _ordered_rows(rows, candidate)
        _checked_observed(ordered, baseline["observed"])
        submissions = [{"question_id": r["question_id"],
                        "selected_index": r["selected_index"]} for r in ordered]
        trace = trace_responses(candidate, submissions, kt=kt,
                                history_responses=previous)
        diagnostics = _diagnostics(candidate, ordered, trace, gate, midpoint_gate)
        variants.append({
            "kind": kind, "research_bank_sha256": bank_fingerprint(candidate),
            "question_order": [r["question_id"] for r in ordered],
            "history_kind": ("synthetic_repeated_approved_items" if previous else "none"),
            "history_length": trace["history_length"],
            "observed_counts_unchanged": True,
            "kt": trace, "conformal": diagnostics,
            "graph": baseline["graph"],
            "comparison_to_baseline": _comparison(baseline, ordered, trace, diagnostics),
        })
    return {
        "schema": "phase3_research_matrix_v1", "scope": "researcher_only",
        "base_bank_sha256": baseline["bank_sha256"],
        "scenario": baseline["scenario"], "baseline": baseline,
        "variants": variants,
        "limitations": [
            "synthetic outcomes and histories are not student evidence",
            "reordered variants keep each question and response in the same test half",
            "repeated history uses previously seen questions, not real prior sessions; attempts and time bins remain defaults",
            "history versus no history also changes model position; compare equal-length correct and incorrect histories",
            "unknown item IDs reuse approved text and options; they are not new question content",
            "historical conformal coverage does not validate this fixed instrument",
            "global graph candidates are researcher-only and are not approved prerequisites",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank-path", type=Path, default=phase3_paths.APPROVED_BANK)
    parser.add_argument("--profiles", nargs="+", required=True,
                        choices=sorted(DETERMINISTIC | PROBABILISTIC - {"custom"}))
    parser.add_argument("--seeds", nargs="+", required=True, type=int)
    parser.add_argument("--order-seed", type=int, required=True)
    parser.add_argument("--kt-device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--graph", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    output = args.out.resolve()
    if not output.is_relative_to(phase3_paths.ARTIFACTS.resolve()):
        parser.error("research matrix outputs must stay under ignored Phase 3 artifacts")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    if len(set(args.profiles)) != len(args.profiles) or len(set(args.seeds)) != len(args.seeds):
        parser.error("profile names and seeds must be unique")
    from conformal_gate import ConformalGate
    from frozen_model import load_frozen_model
    import paths as phase2_paths
    service = MCQSessionService.from_bank_path(
        args.bank_path, SessionStore(phase3_paths.SESSIONS))
    kt = load_frozen_model(device=args.kt_device)
    gate = ConformalGate.load()
    midpoint_gate = ConformalGate.load(
        calibration_path=phase2_paths.HISTORICAL_K5_CALIBRATION,
        coverage_path=phase2_paths.HISTORICAL_K5_COVERAGE)
    graph = None
    if args.graph:
        from kg_query import KnowledgeGraph
        graph = KnowledgeGraph.load()
    output.parent.mkdir(parents=True, exist_ok=True)
    results = [run_matrix(service.bank, {"profile": profile, "seed": seed},
                          service.store, kt, gate, midpoint_gate, graph, args.order_seed)
               for profile in args.profiles for seed in args.seeds]
    with output.open("x", encoding="utf-8") as stream:
        json.dump({"schema": "phase3_research_matrix_batch_v1",
                   "scope": "researcher_only", "base_bank_sha256": bank_fingerprint(service.bank),
                   "run_count": len(results), "results": results},
                  stream, ensure_ascii=False, indent=2)
    print(f"Wrote {len(results)} private research matrices to {output}")


if __name__ == "__main__":
    main()
