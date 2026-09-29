"""Produce deterministic private research tables from fixed-bank scenario results."""
import argparse
import csv
import json
import statistics
from pathlib import Path

import phase3_paths
from feedback_service import (load_assessment_taxonomy, observed_subtopic_counts,
                              validate_assessment_taxonomy)
from session_store import bank_fingerprint

TRACE_FIELDS = ("scenario", "seed", "bank_sha256", "skill_id", "position",
                "half", "skill_attempt", "question_id", "selected_index",
                "correct", "true_probability", "observed_accuracy_to_date",
                "kt_probability", "conformal_item_status", "conformal_regime")
SUMMARY_FIELDS = ("scenario", "seed", "bank_sha256", "checkpoint", "skill_id",
                  "observed_correct", "n_items", "observed_accuracy",
                  "mean_true_probability", "mean_kt_probability", "mean_absolute_gap",
                  "conformal_status", "conformal_lower", "conformal_upper",
                  "conformal_calibration_status", "graph_prerequisites")
SUBTOPIC_FIELDS = ("scenario", "seed", "bank_sha256", "checkpoint", "skill_id",
                   "skill_name", "subtopic_id", "subtopic_name", "observed_correct",
                   "n_items")
MATRIX_SUBTOPIC_FIELDS = ("profile", "seed", "checkpoint", "skill_id", "skill_name",
                          "subtopic_id", "subtopic_name", "observed_correct", "n_items")
MATRIX_CONFORMAL_FIELDS = ("profile", "seed", "variant", "checkpoint", "skill_id",
                           "status", "regime", "n_items", "calibrated_k")


def compile_report(scenarios: list[dict]) -> dict:
    if not scenarios:
        raise ValueError("at least one scenario is required")
    if len({s["bank_sha256"] for s in scenarios}) != 1:
        raise ValueError("all scenarios must use the same fixed bank")
    keys = [(s["scenario"]["name"], s["scenario"]["seed"]) for s in scenarios]
    if len(set(keys)) != len(keys):
        raise ValueError("scenario name and seed must identify a unique run")
    trace_rows = []
    summary_rows = []
    subtopic_rows = []
    for scenario in sorted(scenarios, key=lambda s: (s["scenario"]["name"],
                                                    s["scenario"]["seed"])):
        if scenario.get("schema") != "phase3_fixed_scenario_v1":
            raise ValueError("unsupported scenario result schema")
        rows = scenario["responses"]
        if len(rows) != 40 or [r["position"] for r in rows] != list(range(1, 41)):
            raise ValueError("scenario must contain 40 fixed-order responses")
        kt = scenario["kt"].get("p_correct_before_each_answer")
        if kt is not None and len(kt) != 40:
            raise ValueError("KT trace must have 40 probabilities")
        conformal = scenario["conformal"]
        items = conformal.get("items", [])
        if items and len(items) != 40:
            raise ValueError("conformal item decisions must have 40 entries")
        name = scenario["scenario"]["name"]
        seed = scenario["scenario"]["seed"]
        fingerprint = scenario["bank_sha256"]
        skill_rows = {}
        for i, row in enumerate(rows):
            sid = row["skill_id"]
            skill_rows.setdefault(sid, []).append(row)
            item = items[i] if items else {}
            trace_rows.append({
                "scenario": name, "seed": seed, "bank_sha256": fingerprint,
                "skill_id": sid, "position": row["position"],
                "half": row["half"], "skill_attempt": row["skill_attempt"],
                "question_id": row["question_id"],
                "selected_index": row["selected_index"], "correct": row["correct"],
                "true_probability": row["true_probability"],
                "observed_accuracy_to_date": sum(r["correct"] for r in skill_rows[sid])
                / len(skill_rows[sid]),
                "kt_probability": kt[i] if kt is not None else None,
                "conformal_item_status": item.get("status"),
                "conformal_regime": item.get("regime"),
            })
        for checkpoint, limit in (("midpoint", 20), ("end", 40)):
            feed = scenario["observed"][checkpoint]
            subtopics = feed["teacher"].get("subtopics", [])
            if subtopics:
                if (sum(row["out_of"] for row in subtopics) != limit
                        or sum(row["correct"] for row in subtopics) !=
                        feed["teacher"]["total"]["correct"]):
                    raise ValueError("subtopic totals disagree with observed checkpoint")
                subtopic_rows.extend({
                    "scenario": name, "seed": seed, "bank_sha256": fingerprint,
                    "checkpoint": checkpoint, "skill_id": row["skill_id"],
                    "skill_name": row["skill_name"],
                    "subtopic_id": row["subtopic_id"],
                    "subtopic_name": row["subtopic_name"],
                    "observed_correct": row["correct"], "n_items": row["out_of"],
                } for row in subtopics)
            for skill in feed["teacher"]["skills"]:
                sid = skill["skill_id"]
                positions = [i for i, row in enumerate(rows[:limit])
                             if row["skill_id"] == sid]
                probabilities = [rows[i]["true_probability"] for i in positions]
                predicted = [kt[i] for i in positions] if kt is not None else None
                interval = conformal.get("checkpoints", {}).get(checkpoint, {}).get(sid, {})
                graph = scenario["graph"].get("prerequisites", {}).get(sid)
                total = skill["total"]
                summary_rows.append({
                    "scenario": name, "seed": seed, "bank_sha256": fingerprint,
                    "checkpoint": checkpoint, "skill_id": sid,
                    "observed_correct": total["correct"], "n_items": total["out_of"],
                    "observed_accuracy": total["correct"] / total["out_of"],
                    "mean_true_probability": statistics.mean(probabilities),
                    "mean_kt_probability": statistics.mean(predicted) if predicted else None,
                    "mean_absolute_gap": (statistics.mean(abs(kt[i] - rows[i]["true_probability"])
                                                          for i in positions)
                                          if kt is not None else None),
                    "conformal_status": interval.get("status"),
                    "conformal_lower": interval.get("lower"),
                    "conformal_upper": interval.get("upper"),
                    "conformal_calibration_status": (
                        conformal.get("midpoint_status") if checkpoint == "midpoint" else
                        conformal.get("end_status")),
                    "graph_prerequisites": graph,
                })
    return {
        "schema": "phase3_scenario_report_v1", "bank_sha256": scenarios[0]["bank_sha256"],
        "scope": "researcher_only_fixed_40_question_bank",
        "interpretation": "Mean absolute gap compares simulated response probabilities to KT pre-answer probabilities; it is not a calibration guarantee or observed accuracy error.",
        "scenario_count": len(scenarios), "summary": summary_rows,
        "trace": trace_rows, "subtopics": subtopic_rows,
    }


def write_report(report: dict, out_dir: Path) -> None:
    out_dir = Path(out_dir)
    outputs = [out_dir / name for name in
               ("scenario_report.json", "scenario_summary.csv", "per_skill_trace.csv",
                "subtopic_summary.csv")]
    if any(path.exists() for path in outputs):
        raise FileExistsError("Refusing to overwrite a scenario report output")
    out_dir.mkdir(parents=True, exist_ok=True)
    with outputs[0].open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    for path, fields, rows in ((outputs[1], SUMMARY_FIELDS, report["summary"]),
                               (outputs[2], TRACE_FIELDS, report["trace"]),
                               (outputs[3], SUBTOPIC_FIELDS, report["subtopics"])):
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                values = {field: row[field] for field in fields}
                if "graph_prerequisites" in values:
                    values["graph_prerequisites"] = (
                        json.dumps(values["graph_prerequisites"], ensure_ascii=False)
                        if values["graph_prerequisites"] is not None else "")
                writer.writerow(values)


def compile_matrix_taxonomy(batch: dict, bank: dict, taxonomy: dict) -> dict:
    validate_assessment_taxonomy(bank, taxonomy)
    if (batch.get("schema") != "phase3_research_matrix_batch_v1"
            or batch.get("scope") != "researcher_only"
            or batch.get("base_bank_sha256") != bank_fingerprint(bank)
            or batch.get("run_count") != len(batch.get("results", []))):
        raise ValueError("research batch and taxonomy bank do not match")
    observed_rows, conformal_rows, seen = [], [], set()
    question_order = [q["question_id"] for q in bank["questions"]]
    for run in batch["results"]:
        profile, seed = run["scenario"]["profile"], run["scenario"]["seed"]
        if (profile, seed) in seen or run["base_bank_sha256"] != bank_fingerprint(bank):
            raise ValueError("research batch has duplicate runs or mixed banks")
        seen.add((profile, seed))
        baseline = run["baseline"]
        rows = baseline["responses"]
        if ([row["question_id"] for row in rows] != question_order
                or baseline["bank_sha256"] != bank_fingerprint(bank)):
            raise ValueError("baseline question order or bank changed")
        if any(row["skill_id"] != question["skill_id"] or
               row["correct"] is not (row["selected_index"] == question["answer_index"])
               for row, question in zip(rows, bank["questions"])):
            raise ValueError("baseline response correctness disagrees with approved answer key")
        for variant in run["variants"]:
            order = variant["question_order"]
            if (not variant["observed_counts_unchanged"] or len(order) != 40
                    or len(set(order)) != 40
                    or set(order[:20]) != set(question_order[:20])
                    or set(order[20:]) != set(question_order[20:])):
                raise ValueError("paired variant changed the observed question sets")
        for checkpoint, limit, k in (("midpoint", 20, 5), ("end", 40, 10)):
            counts = observed_subtopic_counts(taxonomy, rows[:limit])
            total = baseline["observed"][checkpoint]["teacher"]["total"]
            if (sum(r["out_of"] for r in counts) != total["out_of"]
                    or sum(r["correct"] for r in counts) != total["correct"]):
                raise ValueError("subtopic scores disagree with baseline scoring")
            observed_rows.extend({
                "profile": profile, "seed": seed, "checkpoint": checkpoint,
                "skill_id": row["skill_id"], "skill_name": row["skill_name"],
                "subtopic_id": row["subtopic_id"],
                "subtopic_name": row["subtopic_name"],
                "observed_correct": row["correct"], "n_items": row["out_of"],
            } for row in counts)
            for variant, diagnostics in [("baseline", baseline["conformal"])] + [
                    (v["kind"], v["conformal"]) for v in run["variants"]]:
                calibrated_k = (diagnostics["midpoint_calibrated_k"] if checkpoint ==
                                "midpoint" else diagnostics.get(
                                    "end_calibrated_k", diagnostics.get("calibrated_k")))
                if calibrated_k != k:
                    raise ValueError("skill conformal gate is not calibrated for checkpoint size")
                skills = diagnostics["checkpoints"][checkpoint]
                if set(skills) != set(bank["skill_names"]):
                    raise ValueError("skill conformal checkpoints do not match bank")
                for sid, decision in skills.items():
                    if decision["n_items"] != k:
                        raise ValueError("conformal checkpoint must stay skill-level")
                    conformal_rows.append({
                        "profile": profile, "seed": seed, "variant": variant,
                        "checkpoint": checkpoint, "skill_id": sid,
                        "status": decision["status"], "regime": decision["regime"],
                        "n_items": decision["n_items"], "calibrated_k": k,
                    })
    return {"schema": "phase3_matrix_taxonomy_report_v1", "scope": "researcher_only",
            "base_bank_sha256": bank_fingerprint(bank),
            "taxonomy_schema": taxonomy["schema"], "taxonomy_relation": taxonomy["relation"],
            "run_count": len(seen), "subtopics": observed_rows,
            "skill_conformal": conformal_rows,
            "interpretation": "Subtopic counts are observed correct/out-of only. Conformal statuses are skill-level historical k=5/k=10 diagnostics, not calibrated for this fixed bank or any subtopic."}


def write_matrix_taxonomy(report: dict, out_dir: Path) -> None:
    out_dir = Path(out_dir)
    outputs = [out_dir / name for name in
               ("matrix_taxonomy_report.json", "subtopic_observed.csv",
                "skill_conformal.csv")]
    if any(path.exists() for path in outputs):
        raise FileExistsError("Refusing to overwrite a matrix taxonomy output")
    out_dir.mkdir(parents=True, exist_ok=True)
    with outputs[0].open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    for path, fields, rows in ((outputs[1], MATRIX_SUBTOPIC_FIELDS, report["subtopics"]),
                               (outputs[2], MATRIX_CONFORMAL_FIELDS,
                                report["skill_conformal"])):
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenarios", type=Path, nargs="+", help="private scenario JSON outputs")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--research-matrix", action="store_true",
                        help="report taxonomy counts and separate skill-level conformal from a saved matrix")
    args = parser.parse_args()
    if args.research_matrix:
        if len(args.scenarios) != 1:
            parser.error("--research-matrix requires one saved matrix batch")
        bank = json.loads(phase3_paths.require(phase3_paths.APPROVED_BANK).read_text(
            encoding="utf-8"))
        taxonomy = load_assessment_taxonomy(bank, required=True)
        batch = json.loads(args.scenarios[0].read_text(encoding="utf-8"))
        write_matrix_taxonomy(compile_matrix_taxonomy(batch, bank, taxonomy),
                              args.out_dir)
        print(f"Wrote private matrix taxonomy report to {args.out_dir}")
    else:
        scenarios = [json.loads(path.read_text(encoding="utf-8")) for path in args.scenarios]
        report = compile_report(scenarios)
        write_report(report, args.out_dir)
        print(f"Wrote private scenario report to {args.out_dir}")


if __name__ == "__main__":
    main()
