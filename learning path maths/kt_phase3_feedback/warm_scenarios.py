"""Capture 54 warm-bank synthetic cases without changing historical evidence."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

import phase3_paths
from evaluate_scenarios import SEEDS, scenario_inputs
from evidence_feedback import build_research_evidence, canonical_digest, run_feedback
from evidence_feedback_policy import POLICY_VERSION, REPORT_SCHEMA
from feedback_service import assessment_feedback_graph
from live_diagnostics import LiveDiagnostics
from research_runtime import load_research_bank
from scenario_runner import generate_responses
from schemas import validate_submission
from session_store import bank_fingerprint

DEFAULT_CAPTURE = phase3_paths.ARTIFACTS / "warm_scenarios_v1"


def capture_source():
    bank, taxonomy, provenance = load_research_bank("warm")
    # Keep the frozen bank intact while adapting the legacy fraction profile label.
    simulation_bank = {**bank, "skill_names": {
        sid: ("Murtoluvut" if label == "Murtolukujen kerto- ja jakolasku" else label)
        for sid, label in bank["skill_names"].items()}}
    cases = []
    for name, group, seed, positions, shift, profile in scenario_inputs(bank, taxonomy):
        if profile is None:
            rows = [{
                "position": index + 1, "question_id": question["question_id"],
                "skill_id": question["skill_id"], "correct": index in positions,
                "true_probability": float(index in positions),
                "selected_index": (question["answer_index"] if index in positions else
                                   (question["answer_index"] + shift) % len(question["options"])),
            } for index, question in enumerate(bank["questions"])]
        else:
            rows = generate_responses(simulation_bank, {"profile": profile, "seed": seed})
        for index, (row, question) in enumerate(zip(rows, bank["questions"])):
            selected = validate_submission({
                "question_id": row["question_id"], "selected_index": row["selected_index"]},
                question, index)
            if row["correct"] != (selected == question["answer_index"]):
                raise ValueError("synthetic correctness disagrees with the warm bank")
        cases.append({"name": name, "group": group, "seed": seed,
                      "profile": profile, "responses": rows})
    if len(cases) != 54 or len({case["name"] for case in cases}) != 54:
        raise ValueError("expected 54 unique warm scenarios")
    return {
        "schema": "phase3_fixed40_comparison_v1",
        "scope": "private_synthetic_fixed_40_cold_start_not_student_validation",
        "bank_mode": "warm", "bank_sha256": bank_fingerprint(bank),
        "taxonomy_sha256": canonical_digest(taxonomy),
        "bank_provenance": provenance, "seed_set": list(SEEDS),
        "simulation_warning": (
            "Profile names and true_probability describe synthetic sampling only, "
            "not learner learning, fatigue, guessing, or ground truth."),
        "scenarios": cases,
    }


def render_report(source_path, diagnostics=None):
    """Read the captured answers; never resample inputs when rendering drafts."""
    raw = Path(source_path).read_bytes()
    source = json.loads(raw)
    bank, taxonomy, provenance = load_research_bank("warm")
    if (source["bank_mode"] != "warm"
            or source["bank_sha256"] != bank_fingerprint(bank)
            or source["taxonomy_sha256"] != canonical_digest(taxonomy)
            or source["bank_provenance"] != provenance
            or len(source["scenarios"]) != 54):
        raise ValueError("source does not match the warm bank and taxonomy")
    cases = []
    for case in source["scenarios"]:
        rows = [{key: row[key] for key in ("question_id", "selected_index")}
                for row in case["responses"]]
        if len(rows) != 40:
            raise ValueError("each warm scenario requires 40 answers")
        packages = []
        for audience, checkpoint, limit in (
                ("student", "midpoint", 20), ("student", "end", 40),
                ("teacher", "end", 40)):
            evidence = build_research_evidence(bank, taxonomy, rows[:limit])
            review = run_feedback(evidence, audience, checkpoint)
            packages.append({
                "audience": audience, "checkpoint": checkpoint, "review": review,
                "selector_execution": {"status": "offline_rules_no_provider"},
                "generator_execution": {"status": "offline_template_no_provider"},
            })
        graphs = {checkpoint: assessment_feedback_graph(bank, taxonomy, rows[:limit])
                  for checkpoint, limit in (("midpoint", 20), ("end", 40))}
        diagnostic = (diagnostics.evaluate(bank, taxonomy, rows) if diagnostics else
                      {"status": "not_requested", "kt": {"status": "not_requested"}})
        if diagnostics is not None and diagnostic["status"] != "ready":
            raise ValueError(f"frozen KT unavailable for {case['name']}")
        if "conformal" in json.dumps(diagnostic).lower():
            raise ValueError("warm captures must not contain active conformal output")
        cases.append({
            "name": case["name"], "group": case["group"], "packages": packages,
            "assessment_graph": {"checkpoints": graphs},
            "research_diagnostics": copy.deepcopy(diagnostic),
        })
        print(f"Captured warm scenario {len(cases)}/54: {case['name']}", flush=True)
    return {
        "schema": REPORT_SCHEMA, "pipeline_schema": "phase3_integrated_synthetic_pipeline_v1",
        "status": "draft_not_for_learner_delivery",
        "source": {"bank_mode": "warm", "bank_sha256": source["bank_sha256"],
                   "taxonomy_sha256": source["taxonomy_sha256"],
                   "bank_provenance": provenance, "sha256": hashlib.sha256(raw).hexdigest()},
        "policy": {"version": POLICY_VERSION, "provider_mode": "rules"},
        "diagnostic_provenance": (cases[0]["research_diagnostics"]["kt"].get("provenance", {})),
        "educator_approved": False, "approves_learner_delivery": False,
        "scenarios": cases,
    }


def write_capture(directory, diagnostics=None):
    directory = Path(directory)
    # A new directory prevents partial or completed evidence from being overwritten.
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / "source.json"
    source.write_text(json.dumps(capture_source(), ensure_ascii=False, indent=2),
                      encoding="utf-8")
    report = render_report(source, diagnostics)
    with (directory / "report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return source, directory / "report.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_CAPTURE)
    parser.add_argument("--with-kt", action="store_true",
                        help="capture frozen KT traces; no conformal or hosted calls")
    args = parser.parse_args(argv)
    source, report = write_capture(
        args.out_dir, LiveDiagnostics() if args.with_kt else None)
    print(f"Warm source: {source}\nWarm report: {report}", flush=True)


if __name__ == "__main__":
    main()
