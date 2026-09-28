"""Replay held-out checkpoints without exposing student identifiers or training a model."""
import argparse
import gzip
import json
from collections import defaultdict, deque
from pathlib import Path

import numpy as np

import paths
from conformal_calibrate import (SPLIT_COLD, SPLIT_WARM, build_checkpoints,
                                 load_predictions, partition_students)
from conformal_gate import ConformalGate, InterventionStatus
from kg_query import KnowledgeGraph


LABELS = ("CONFIDENT_STRUGGLE", "MASTERY_SAFE", "UNCERTAIN_BEHAVIOR")
EVAL_SPLITS = {"test_warm", "test_cold_item"}


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def summarize(rows):
    n = len(rows)
    below = sum(row["below_mastery"] for row in rows)
    struggle = [row for row in rows if row["status"] == "CONFIDENT_STRUGGLE"]
    mastery = [row for row in rows if row["status"] == "MASTERY_SAFE"]
    certain = struggle + mastery
    eligible = [row for row in rows if row["prior_status"] is not None]
    eligible_certain = [row for row in eligible if row["status"] != "UNCERTAIN_BEHAVIOR"]
    baseline_struggle = [row for row in eligible if row["prior_status"] == "CONFIDENT_STRUGGLE"]
    baseline_mastery = [row for row in eligible if row["prior_status"] == "MASTERY_SAFE"]
    with_future = [row for row in rows if row["future_rate"] is not None]
    return {
        "n_checkpoints": n,
        "below_mastery_rate": ratio(below, n),
        "status_counts": {status: sum(row["status"] == status for row in rows) for status in LABELS},
        "status_rates": {status: ratio(sum(row["status"] == status for row in rows), n) for status in LABELS},
        "struggle_precision": ratio(sum(row["below_mastery"] for row in struggle), len(struggle)),
        "struggle_recall": ratio(sum(row["status"] == "CONFIDENT_STRUGGLE" and row["below_mastery"] for row in rows), below),
        "mastery_precision": ratio(sum(not row["below_mastery"] for row in mastery), len(mastery)),
        "confident_decision_accuracy": ratio(sum((row["status"] == "CONFIDENT_STRUGGLE") == row["below_mastery"] for row in certain), len(certain)),
        "grounded_target_rate_among_struggle": ratio(sum(row["has_grounded_target"] for row in struggle), len(struggle)),
        "prior_10_baseline": {
            "eligible": len(eligible),
            "coverage": ratio(len(eligible), n),
            "struggle_precision": ratio(sum(row["below_mastery"] for row in baseline_struggle), len(baseline_struggle)),
            "mastery_precision": ratio(sum(not row["below_mastery"] for row in baseline_mastery), len(baseline_mastery)),
            "accuracy": ratio(sum((row["prior_status"] == "CONFIDENT_STRUGGLE") == row["below_mastery"] for row in eligible), len(eligible)),
            "gate_on_same_eligible": {
                "acted": len(eligible_certain),
                "coverage": ratio(len(eligible_certain), len(eligible)),
                "accuracy_when_acted": ratio(sum((row["status"] == "CONFIDENT_STRUGGLE") == row["below_mastery"] for row in eligible_certain), len(eligible_certain)),
                "baseline_accuracy_on_same_checkpoints": ratio(sum((row["prior_status"] == "CONFIDENT_STRUGGLE") == row["below_mastery"] for row in eligible_certain), len(eligible_certain)),
            },
        },
        "next_10_same_skill_heldout": {
            "available": len(with_future),
            "availability": ratio(len(with_future), n),
            "below_mastery_rate": ratio(sum(row["future_rate"] < row["mastery_threshold"] for row in with_future), len(with_future)),
            "n_after_struggle_flag": sum(row["status"] == "CONFIDENT_STRUGGLE" for row in with_future),
            "below_mastery_rate_after_struggle_flag": ratio(
                sum(row["future_rate"] < row["mastery_threshold"] for row in with_future if row["status"] == "CONFIDENT_STRUGGLE"),
                sum(row["status"] == "CONFIDENT_STRUGGLE" for row in with_future)),
        },
    }


def enrich_from_sequences(path, checkpoints, pred, k, mastery_threshold):
    by_record = defaultdict(list)
    for index, (record_idx, skill_idx, chunk) in enumerate(checkpoints):
        by_record[record_idx].append((int(pred["position"][chunk[0]]),
                                     int(pred["position"][chunk[-1]]), skill_idx, index))
    prior = [None] * len(checkpoints)
    future = [None] * len(checkpoints)
    matched = 0
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for record_idx, line in enumerate(stream):
            pending = by_record.pop(record_idx, None)
            if not pending:
                continue
            events = json.loads(line)["events"]
            requests = defaultdict(list)
            for start, end, skill_idx, index in pending:
                requests[start].append((skill_idx, index))
                later = [ev["correct"] for ev in events[end + 1:]
                         if ev["skill"] == skill_idx and ev["split"] in EVAL_SPLITS]
                if len(later) >= k:
                    future[index] = float(np.mean(later[:k]))
            history = defaultdict(lambda: deque(maxlen=k))
            for position, ev in enumerate(events):
                for skill_idx, index in requests.get(position, ()):
                    matched += 1
                    if len(history[skill_idx]) == k:
                        prior[index] = ("CONFIDENT_STRUGGLE" if np.mean(history[skill_idx]) < mastery_threshold
                                        else "MASTERY_SAFE")
                history[ev["skill"]].append(ev["correct"])
    if by_record or matched != len(checkpoints):
        raise ValueError("Prediction window indices do not match the sequences file")
    return prior, future


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, default=paths.PREDICTIONS)
    parser.add_argument("--sequences", type=Path, default=paths.SEQUENCES)
    parser.add_argument("--out", type=Path, default=paths.ARTIFACTS / "checkpoint_replay.jsonl")
    parser.add_argument("--report", type=Path, default=paths.ARTIFACTS / "checkpoint_decision_report.json")
    args = parser.parse_args()

    pred = load_predictions(args.predictions, False)
    gate = ConformalGate.load()
    graph = KnowledgeGraph.load()
    protocol = gate.cal["calibration_protocol"]
    student_is_calib = partition_students(len(pred["students"]), protocol["calib_frac"], protocol["seed"])
    if int(student_is_calib.sum()) != protocol["n_calib_students"] or int((~student_is_calib).sum()) != protocol["n_eval_students"]:
        raise ValueError("Student partition disagrees with saved calibration")
    eval_mask = np.isin(pred["split"], (SPLIT_WARM, SPLIT_COLD)) & ~student_is_calib[pred["student_idx"]]
    chunks = build_checkpoints(pred, eval_mask, gate.k)
    idx_to_skill = {int(node["vocab_idx"]): skill_id for skill_id, node in graph.nodes.items()}
    if any(skill not in idx_to_skill for _, skill, _ in chunks):
        raise ValueError("A checkpoint skill is missing from the frozen graph vocabulary")
    prior, future = enrich_from_sequences(paths.require(args.sequences), chunks, pred, gate.k, 0.80)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    summary = {"warm": [], "cold": [], "all": []}
    lookup = graph.remediation_lookup()
    with open(args.out, "w", encoding="utf-8") as output:
        for index, (record_idx, skill_idx, chunk) in enumerate(chunks):
            regime = "cold" if np.any(pred["split"][chunk] == SPLIT_COLD) else "warm"
            decision = gate.checkpoint(pred["p"][chunk], idx_to_skill[skill_idx],
                                       regime=regime, remediation_lookup=lookup)
            result = decision.to_dict()
            realized = float(pred["y"][chunk].mean())
            grounded = bool(result["remediation_target"] and result["remediation_target"]["candidates"])
            row = {
                "checkpoint_idx": index, "record_idx": record_idx,
                "start_position": int(pred["position"][chunk[0]]),
                "end_position": int(pred["position"][chunk[-1]]),
                "student_idx": int(pred["student_idx"][chunk[0]]),
                "observed_rate": realized, "below_mastery": realized < decision.mastery_threshold,
                "prior_status": prior[index], "future_rate": future[index],
                "has_grounded_target": grounded, "mastery_threshold": decision.mastery_threshold,
                "status": decision.status.value, "regime": regime,
            }
            output.write(json.dumps({**row, "decision": result}, ensure_ascii=False) + "\n")
            summary[regime].append(row)
            summary["all"].append(row)
    report = {
        "protocol": {"calibration": protocol, "n_students": len(pred["students"]),
                     "checkpoint_k": gate.k, "mastery_threshold": 0.80,
                     "baseline": "last 10 actual same-skill outcomes before checkpoint starts within its window; abstains if fewer than 10",
                     "future": "next 10 held-out same-skill outcomes after checkpoint ends within its window; descriptive, not an intervention effect",
                     "replay_contains_pseudonymous_record_and_student_indices": True},
        "regimes": {regime: summarize(rows) for regime, rows in summary.items()},
    }
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    for regime, stats in report["regimes"].items():
        print(f"{regime}: n={stats['n_checkpoints']:,}, status={stats['status_counts']}, "
              f"struggle_precision={stats['struggle_precision']}, "
              f"mastery_precision={stats['mastery_precision']}")
    print(f"Wrote {args.out} and {args.report}")


if __name__ == "__main__":
    main()
