"""Read-only provenance/overlap audit; no inference or metric refitting."""
import argparse
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from phase4_common import ROOT, base_student, load_json, paths, write_json


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def audit(capture, output):
    if output.exists():
        raise FileExistsError("Refusing to overwrite an independence audit")
    manifest = load_json(capture / "replay_manifest.json")
    input_paths = {
        "sequences": paths.SEQUENCES, "vocab": paths.VOCAB,
        "split_report": paths.SPLIT_REPORT, "checkpoint": paths.CHECKPOINT,
        "config": paths.RUN_CONFIG, "predictions": paths.PREDICTIONS,
        "calibration": paths.ACTIVE_CALIBRATION,
    }
    verified = {}
    for name, path in input_paths.items():
        actual = digest(path)
        if actual != manifest["inputs"][name]["sha256"]:
            raise ValueError(f"Frozen input changed: {name}")
        verified[name] = actual
    for name in ("targets_private.json", "replay_windows_private.jsonl.gz"):
        actual = digest(capture / name)
        if actual != manifest["capture_hashes"][name]["sha256"]:
            raise ValueError(f"Replay capture changed: {name}")
        verified[name] = actual
    targets = load_json(capture / "targets_private.json")
    calibration = load_json(paths.ACTIVE_CALIBRATION)
    protocol = calibration["calibration_protocol"]
    config = load_json(paths.RUN_CONFIG)
    best = load_json(paths.RUN_DIR / "best_epochs.json")
    history = load_json(paths.RUN_DIR / "training_history.json")
    selected = next(h for h in history if h["epoch"] == best["best_joint_epoch"])
    joint = (config["joint_weight"] * selected["eval"]["val"]["auc"]
             + (1 - config["joint_weight"]) * selected["eval"]["test_cold_item"]["auc"])
    if not np.isclose(joint, best["best_joint_score"], rtol=0, atol=1e-12):
        raise ValueError("Saved joint selection provenance disagrees")
    score_history = [
        config["joint_weight"] * h["eval"]["val"]["auc"]
        + (1 - config["joint_weight"]) * h["eval"]["test_cold_item"]["auc"]
        for h in history
    ]
    if history[int(np.argmax(score_history))]["epoch"] != best["best_joint_epoch"]:
        raise ValueError("Frozen checkpoint is not the saved joint-selection epoch")

    with np.load(paths.PREDICTIONS, allow_pickle=True) as dump:
        meta = json.loads(str(dump["meta"]))
        if meta["partial"] is not False:
            raise ValueError("Partial prediction source")
        students = list(dict.fromkeys(base_student(str(s)) for s in dump["students"]))
        if meta["variant"] != config["variant"] or meta["seed"] != config["seed"]:
            raise ValueError("Prediction model metadata mismatch")
        if str(meta["checkpoint"]).replace("\\", "/").rsplit("/", 1)[-1] != paths.CHECKPOINT.name:
            raise ValueError("Prediction checkpoint name mismatch")
        rng = np.random.default_rng(protocol["seed"])
        permutation = rng.permutation(len(students))
        n_calib = int(round(len(students) * protocol["calib_frac"]))
        calib_codes = set(int(i) for i in permutation[:n_calib])
        if n_calib != protocol["n_calib_students"] or len(students) - n_calib != protocol["n_eval_students"]:
            raise ValueError("Calibration partition counts mismatch")
        student_codes = {s: i for i, s in enumerate(students)}
        raw_record = dump["record_idx"].astype(np.int64)
        raw_position = dump["position"].astype(np.int64)
        width = config["max_seq_len"]
        keys = raw_record * width + raw_position
        if np.any(np.diff(keys) <= 0):
            raise ValueError("Prediction locators are not strictly ordered and unique")
        target_keys = np.array([t["record_index"] * width + t["position"] for t in targets])
        indexes = np.searchsorted(keys, target_keys)
        if np.any(indexes >= len(keys)) or not np.array_equal(keys[indexes], target_keys):
            raise ValueError("Phase 4 targets missing from original prediction evaluation")
        for field, target_field in (("y", "y"), ("skill_idx", "skill_index"),
                                    ("item_idx", "item_index")):
            if not np.array_equal(dump[field][indexes], [t[target_field] for t in targets]):
                raise ValueError(f"Prediction target mismatch: {field}")
        expected_splits = np.array([2 if t["regime"] == "warm" else 3 for t in targets])
        if not np.array_equal(dump["split"][indexes], expected_splits):
            raise ValueError("Target split mismatch")
        original_split_counts = Counter(int(v) for v in dump["split"])

    by_record = defaultdict(list)
    regimes = {}
    for target in targets:
        if target["student_code"] in calib_codes:
            raise ValueError("Calibration student found in Phase 4 targets")
        by_record[target["record_index"]].append(target)
    target_codes = {t["student_code"] for t in targets}
    for regime in ("warm", "cold"):
        subset = [t for t in targets if t["regime"] == regime]
        regimes[regime] = {
            "targets": len(subset), "students": len({t["student_code"] for t in subset}),
            "incorrect": sum(t["y"] == 0 for t in subset),
            "targets_in_original_phase1_evaluation_and_phase2_evaluation": len(subset),
            "calibration_student_overlap": 0,
            "checkpoint_selection_targets": len(subset) if regime == "cold" else 0,
        }
    captured = {}
    with gzip.open(capture / "replay_windows_private.jsonl.gz", "rt", encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            captured[record["source_record_index"]] = record
    if set(captured) != set(by_record):
        raise ValueError("Captured windows differ from target windows")

    all_splits, cohort_splits = Counter(), defaultdict(Counter)
    seen_records = set()
    records_seen = 0
    for_record_split = Counter()
    future_train = 0
    with gzip.open(paths.SEQUENCES, "rt", encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            record = json.loads(line)
            records_seen += 1
            student = base_student(record["student_id"])
            code = student_codes[student]
            counts = Counter(e["split"] for e in record["events"])
            all_splits.update(counts)
            if code in target_codes:
                cohort_splits[code].update(counts)
            if index not in by_record:
                continue
            seen_records.add(index)
            end = max(t["position"] for t in by_record[index]) + 1
            if (record["events"][:end] != captured[index]["events"]
                    or captured[index]["student_id"] != f"research_student_{code}"):
                raise ValueError("Captured prefix/anonymous owner differs from original")
            for target in by_record[index]:
                event = record["events"][target["position"]]
                if code != target["student_code"] or event["correct"] != target["y"]:
                    raise ValueError("Target owner/label mismatch")
                expected = "test_warm" if target["regime"] == "warm" else "test_cold_item"
                if event["split"] != expected:
                    raise ValueError("Target is not a held-out loss position")
                for_record_split[event["split"]] += 1
                future_train += int(any(e["split"] == "train"
                                        for e in record["events"][target["position"] + 1:]))
    split_report = load_json(paths.SPLIT_REPORT)
    if dict(all_splits) != split_report["split_event_counts"]:
        raise ValueError("Retained sequence split totals differ from report")
    if records_seen != split_report["sequence_records_written"] or seen_records != set(by_record):
        raise ValueError("Retained window count/membership mismatch")
    for code, name in ((1, "val"), (2, "test_warm"), (3, "test_cold_item")):
        if original_split_counts[code] != selected["eval"][name]["n"]:
            raise ValueError("Prediction and selected-epoch evaluated position counts differ")
    for regime in regimes:
        codes = {t["student_code"] for t in targets if t["regime"] == regime}
        regimes[regime]["students_with_train_loss_positions"] = sum(cohort_splits[c]["train"] > 0 for c in codes)
        regimes[regime]["students_with_val_positions"] = sum(cohort_splits[c]["val"] > 0 for c in codes)
    result = {
        "audit_version": "phase4_independence_v1_20261006",
        "classification": "exploratory_selected_bank_reanalysis_not_untouched_external_validation",
        "verified_input_sha256": verified,
        "audit_method_sha256": digest(Path(__file__)),
        "targets": len(targets), "students": len(target_codes),
        "students_with_train_loss_positions": sum(cohort_splits[c]["train"] > 0 for c in target_codes),
        "students_with_val_positions": sum(cohort_splits[c]["val"] > 0 for c in target_codes),
        "cohort_retained_position_counts": dict(sum(cohort_splits.values(), Counter())),
        "target_split_counts": dict(for_record_split),
        "calibration": {"students": n_calib, "evaluation_students": len(students) - n_calib,
                        "phase4_calibration_student_overlap": 0,
                        "all_phase4_targets_reuse_original_evaluation_half": True},
        "checkpoint_selection": {
            "variant": config["variant"], "seed": config["seed"],
            "epoch": best["best_joint_epoch"], "joint_weight": config["joint_weight"],
            "criterion": "joint_weight * val_auc + (1-joint_weight) * test_cold_item_auc",
            "saved_joint_score_verified": True,
            "evaluation_counts_match_original_dump": True,
            "historical_dump_checkpoint_hash_recorded": "checkpoint_sha256" in meta,
            "historical_dump_device": meta["device"],
        },
        "regimes": regimes,
        "structural_checks": {
            "all_target_positions_excluded_from_train_loss": True,
            "targets_with_later_train_position_in_same_window": future_train,
            "all_captured_prefixes_equal_original_retained_prefixes": True,
            "all_target_locators_present_in_original_eval_dump": True,
            "retained_windows": records_seen,
            "retained_split_totals_match": True,
        },
        "limitations": [
            "Current source code and saved artifacts establish available provenance, not a historical code/environment attestation.",
            "No immutable raw event ID: this audit verifies stored locators, not all possible duplicate source events.",
            "Training student overlap is not automatically target-answer leakage; claims are within-student historical prediction.",
            "Cold item IDs are held out of training loss, not necessarily unseen prompts or history inputs.",
            "Cold evaluation participated in joint checkpoint selection; HPO used cold performance too.",
            "Warm test metrics were monitored and reported; no untouched-study claim is established.",
            "Bank selection reused evaluation-subset support, although objective did not optimize KT metrics or success rates.",
            "Conformal calibration/evaluation student separation does not undo prior model-selection use of cold outcomes.",
        ],
    }
    write_json(output, result)
    print(json.dumps({k: result[k] for k in ("classification", "targets", "students",
                                           "students_with_train_loss_positions", "regimes")}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, default=ROOT / "artifacts" / "selected_bank_v3_20261005")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    audit(args.capture_dir, args.output or args.capture_dir / "independence_audit_20261006.json")
