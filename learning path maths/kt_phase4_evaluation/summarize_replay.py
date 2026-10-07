"""Analyze frozen capture/predictions; never reconstruct target data here."""
import argparse
from pathlib import Path

import numpy as np

from metrics import conformal_item_report, evaluate, interval
from phase4_common import (
    BOOTSTRAP_DRAWS, PROTOCOL_VERSION, ROOT, SEED, fingerprint, load_json,
    paths, write_json,
)

BASELINES = ("baseline_global", "baseline_skill", "baseline_rendering", "baseline_history")


def verify_inference_gate(capture, allow_cpu_reference):
    inference = load_json(capture / "inference_report.json")
    if inference["status"] == "passed":
        return inference, None
    if not allow_cpu_reference:
        raise ValueError("Inference failed historical parity; explicit qualified addendum required")
    if inference["status"] != "parity_failure_investigate":
        raise ValueError("Unsupported inference failure; do not bypass")
    verification = load_json(capture / "cpu_reference_verification.json")
    if verification["status"] != "passed" or verification["violations"] != 0:
        raise ValueError("Same-CPU all-target verification did not pass")
    if (verification["tolerance"] != 1e-6 or
            verification["maximum_absolute_difference"] > verification["tolerance"] or
            verification["targets"] != inference["targets"]):
        raise ValueError("CPU verification scope/tolerance is inconsistent")
    for name, path in (
        ("predictions", capture / "predictions_replay_private.json"),
        ("checkpoint", paths.CHECKPOINT), ("embeddings", paths.TEXT_EMBEDDINGS),
        ("replay_manifest", capture / "replay_manifest.json"),
        ("diagnostic", capture / "parity_diagnostic.json"),
        ("addendum", ROOT / "CPU_REFERENCE_ADDENDUM.md"),
    ):
        if fingerprint(path) != verification[name]:
            raise ValueError("CPU qualification input changed")
    if verification["predictions"] != inference["predictions"]:
        raise ValueError("CPU qualification belongs to different predictions")
    return inference, {
        "status": "same_cpu_input_equivalence_passed_original_cuda_reference_parity_failed",
        "verification": fingerprint(capture / "cpu_reference_verification.json"),
        "addendum": verification["addendum"],
        "original_tolerance": inference["parity_tolerance"],
        "original_violations": inference["parity_violations"],
        "original_maximum_gap": inference["maximum_history_cached_probability_gap"],
        "historical_reference_cause": "Unresolved backend/implementation/provenance difference; not proven to be a specific GPU precision mode.",
    }


def threshold_reference_sensitivity(rows, calibration):
    with np.load(paths.PREDICTIONS, allow_pickle=True) as original:
        keys = original["record_idx"].astype(np.int64) * 400 + original["position"]
        wanted = [r["record_index"] * 400 + r["position"] for r in rows]
        indices = np.searchsorted(keys, wanted)
        if np.any(indices >= len(keys)) or not np.array_equal(keys[indices], wanted):
            raise ValueError("Threshold reference target missing")
        cached = original["p"][indices].astype(float)
    result = {}
    for regime in ("warm", "cold"):
        mask = np.array([r["regime"] == regime for r in rows])
        current = np.array([r["p_history"] for r in rows])[mask]
        old = cached[mask]
        y = np.array([r["y"] for r in rows])[mask]
        q = calibration["item_level"]["groups"][regime]["q_hat"]
        current_sets = np.column_stack((current <= q["0"], 1 - current <= q["1"]))
        old_sets = np.column_stack((old <= q["0"], 1 - old <= q["1"]))
        coverage = current_sets[np.arange(len(y)), y]
        old_coverage = old_sets[np.arange(len(y)), y]
        result[regime] = {
            "events": len(y), "sets_changed": int(np.sum(np.any(current_sets != old_sets, axis=1))),
            "true_label_coverage_changed": int(np.sum(coverage != old_coverage)),
            "current_empirical_coverage": float(coverage.mean()),
            "cached_reference_empirical_coverage": float(old_coverage.mean()),
            "maximum_probability_gap": float(np.max(np.abs(current - old))),
        }
    return result


def groups(rows):
    result = {}
    for regime in ("warm", "cold"):
        selected = [r for r in rows if r["regime"] == regime]
        result[regime + "/all"] = selected
        result[regime + "/first"] = [r for r in selected if r["first_student_question_occurrence"]]
        for skill in sorted({r["skill_id"] for r in selected}):
            result[regime + "/" + skill] = [r for r in selected if r["skill_id"] == skill]
    return result


def score_block_report(blocks, predictions, calibration):
    if not blocks:
        return {"status": "insufficient_genuine_selected_block_support", "blocks": 0,
                "coverage": None, "mean_width": None,
                "reason": "No all-selected blocks under the original all-test k10 grouping; no regrouping."}
    by_id = {r["target_id"]: r for r in predictions}
    values, students, widths = [], [], []
    for block in blocks:
        rows = [by_id[tid] for tid in block["target_ids"]]
        if len(rows) != calibration["checkpoint_level"]["k"]:
            raise ValueError("Score block differs from calibrated size")
        p = np.asarray([r["p_history"] for r in rows])
        y = np.asarray([r["y"] for r in rows])
        regime = "warm" if block["kind"] == "warm_only" else "cold"
        group = calibration["checkpoint_level"]["groups"][regime]
        sigma = max(float(np.sqrt(np.sum(p * (1 - p))) / len(p)),
                    calibration["checkpoint_level"]["sigma_floor"])
        lower = max(0.0, float(p.mean()) - group["q_hat"] * sigma)
        upper = min(1.0, float(p.mean()) + group["q_hat"] * sigma)
        values.append(float(lower <= y.mean() <= upper))
        widths.append(upper - lower)
        students.append(block["student_code"])
    unique, cluster = np.unique(students, return_inverse=True)
    draws, rng = [], np.random.default_rng(SEED)
    for _ in range(BOOTSTRAP_DRAWS):
        multiplicity = np.bincount(rng.integers(0, len(unique), len(unique)), minlength=len(unique))
        weights = multiplicity[cluster]
        draws.append(float(np.dot(weights, values) / weights.sum()))
    return {"status": "historical_prequential_selected_block_check",
            "blocks": len(blocks), "students": len(unique),
            "coverage": float(np.mean(values)), "mean_width": float(np.mean(widths)),
            "coverage_interval": interval(draws, len(unique)),
            "interpretation": "Prequential historical blocks, not a score forecast before all answers or fixed-assessment guarantee."}


def render_report(report, output):
    lines = ["# Selected-bank historical evaluation", "",
             f"Status: `{report['status']}`. Protocol: `{PROTOCOL_VERSION}`.", "",
             "## Overall results", "",
             "| Regime | Predictor | Events | Students | Incorrect | AUC | Log loss | Brier |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    fmt = lambda x: "undefined" if x is None else f"{x:.5f}"
    for regime in ("warm", "cold"):
        group = report["groups"][regime + "/all"]
        for name, values in group["models"].items():
            lines.append(f"| {regime} | {name} | {group['events']} | {group['students']} | "
                         f"{group['incorrect']} | {fmt(values['auc'])} | "
                         f"{fmt(values['log_loss'])} | {fmt(values['brier'])} |")
    lines += ["", "Full JSON includes student-cluster intervals, student-balanced losses,",
              "fixed-bin calibration, each skill and first-occurrence sensitivity."]
    if report["status"] != "baseline_only_kt_pending":
        lines += ["Paired KT comparisons use identical target/student draws.",
                  "Negative history-minus-no-history loss differences favor history;",
                  "positive AUC differences favor history."]
    else:
        lines += ["Paired KT comparisons are pending; no KT results are reported here."]
    lines += ["", "## Interpretation boundaries", "",
              "- Historical, selection-informed targets; not an untouched final test.",
              "- Warm/cold questions are not randomized or difficulty-matched.",
              "- Student dependence and repeated attempts remain; counts are not independent.",
              "- Cold has very few incorrect responses; AUC/calibration conclusions can be fragile.",
              "- No mastery, educational effectiveness or learner-delivery claim.",
              "- No selected-bank ten-question blocks or verified 40-question assessment sessions are invented."]
    if "conformal" in report:
        lines += ["", "## Conformal", "",
                  "Frozen item prediction sets apply only to history replay; their empirical",
                  "coverage on this selected subset is not a new coverage guarantee.",
                  "No-history calibration is unmatched and not evaluated as calibrated.",
                  f"Score-block status: `{report['conformal']['scores']['status']}`."]
    if report.get("cpu_reference_qualification"):
        qualification = report["cpu_reference_qualification"]
        lines += ["", "## Numerical Qualification", "",
                  "The original CUDA/reference parity check FAILED and is not relabelled passed.",
                  f"Maximum gap: {qualification['original_maximum_gap']:.9f}; "
                  f"violations: {qualification['original_violations']}.",
                  "All-target same-CPU unmodified versus response-blind verification passed",
                  "the tighter 1e-6 check. These are qualified exploratory CPU results",
                  "under the recorded addendum, not a strict original-protocol pass.",
                  "Historical conformal thresholds are a transfer-sensitivity check only;",
                  "the exact historical-reference difference remains unresolved."]
    else:
        lines += ["", "KT inference and conformal results are pending; this is baseline-only."]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def summarize(capture, baseline_only, allow_cpu_reference=False):
    name = "baseline_results.json" if baseline_only else "research_results.json"
    output = capture / name
    if output.exists():
        raise FileExistsError("Refusing to overwrite a research result")
    manifest = load_json(capture / "replay_manifest.json")
    if fingerprint(ROOT / "PROTOCOL.md") != manifest["inputs"]["protocol"]:
        raise ValueError("Protocol changed after target preparation")
    for filename, expected in manifest["capture_hashes"].items():
        if fingerprint(capture / filename) != expected:
            raise ValueError("Frozen capture changed")
    targets = load_json(capture / "targets_private.json")
    prior_baselines = None
    qualification = None
    if baseline_only:
        rows, columns, pairs = targets, list(BASELINES), []
    else:
        inference, qualification = verify_inference_gate(capture, allow_cpu_reference)
        prediction_path = capture / "predictions_replay_private.json"
        if fingerprint(prediction_path) != inference["predictions"]:
            raise ValueError("Inference output changed")
        rows = load_json(prediction_path)
        if len(rows) != len(targets) or any(
                any(row[k] != target[k] for k in target)
                for row, target in zip(rows, targets, strict=True)):
            raise ValueError("Paired inference targets differ from capture")
        columns = list(BASELINES) + ["p_history", "p_no_history"]
        pairs = [("p_history", "p_no_history")]
        pairs += [("p_history", base) for base in BASELINES]
        pairs += [("p_no_history", base) for base in BASELINES[:3]]
        prior_path = capture / "baseline_results.json"
        if prior_path.exists():
            prior_baselines = load_json(prior_path)
            if prior_baselines["capture"] != fingerprint(capture / "replay_manifest.json"):
                raise ValueError("Baseline report belongs to another capture")
            if prior_baselines["methods"]["metrics"] != fingerprint(ROOT / "metrics.py"):
                raise ValueError("Metric definitions changed since baseline analysis")
    result = {
        "protocol_version": PROTOCOL_VERSION,
        "status": "baseline_only_kt_pending" if baseline_only else
                  "historical_replay_complete_cpu_reference_qualified" if qualification else
                  "historical_replay_complete",
        "cpu_reference_qualification": qualification,
        "capture": fingerprint(capture / "replay_manifest.json"),
        "methods": {"analysis": fingerprint(Path(__file__)), "metrics": fingerprint(ROOT / "metrics.py")},
        "groups": {},
        "limitations": manifest["limitations"] + [
            "Small selected-bank support and scarce cold errors limit precision.",
            "Warm/cold is descriptive; not a difficulty-controlled comparison.",
            "No student-facing decision changes or learning-effect conclusions."],
    }
    for key, members in groups(rows).items():
        frozen = prior_baselines["groups"][key]["models"] if prior_baselines else None
        result["groups"][key] = evaluate(members, columns, pairs, frozen=frozen)
        print("Analyzed", key, "events", len(members), flush=True)
    if not baseline_only:
        calibration = load_json(paths.ACTIVE_CALIBRATION)
        if fingerprint(paths.ACTIVE_CALIBRATION) != manifest["inputs"]["calibration"]:
            raise ValueError("Conformal artifact changed")
        item = {}
        for regime in ("warm", "cold"):
            members = [r for r in rows if r["regime"] == regime]
            item[regime] = conformal_item_report(members, calibration)
            item[regime]["by_label"] = {
                str(label): conformal_item_report([r for r in members if r["y"] == label], calibration)
                for label in (0, 1)}
        result["conformal"] = {
            "target": "individual_correctness_history_only", "items": item,
            "no_history": "not_calibrated_for_this_predictor_do_not_transfer_guarantee",
            "scores": score_block_report(load_json(capture / "selected_blocks_private.json"), rows, calibration),
        }
        if qualification:
            result["conformal"]["status"] = "historical_threshold_transfer_sensitivity_only"
            result["conformal"]["threshold_reference_sensitivity"] = threshold_reference_sensitivity(
                rows, calibration)
    write_json(output, result)
    render_report(result, capture / ("baseline_review.md" if baseline_only else "research_review.md"))
    print("RESULT", output, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--allow-cpu-reference-qualified", action="store_true",
                        help="Require the explicit addendum and passed all-target same-CPU gate.")
    args = parser.parse_args()
    summarize(args.capture_dir, args.baseline_only, args.allow_cpu_reference_qualified)
