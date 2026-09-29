"""Produce deterministic private tables from fixed-bank scenario results."""
import argparse
import csv
import json
import statistics
from pathlib import Path

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenarios", type=Path, nargs="+", help="private scenario JSON outputs")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    scenarios = [json.loads(path.read_text(encoding="utf-8")) for path in args.scenarios]
    report = compile_report(scenarios)
    write_report(report, args.out_dir)
    print(f"Wrote private scenario report to {args.out_dir}")


if __name__ == "__main__":
    main()
