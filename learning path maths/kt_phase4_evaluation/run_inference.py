"""Run paired frozen KT predictions only after replay inputs are locked."""
import argparse
import importlib
import json
from pathlib import Path

import numpy as np
import psutil

from phase4_common import (
    BATCH_SIZE, CPU_THREADS, PARITY_TOLERANCE, ROOT, base_student,
    fingerprint, load_json, paths, write_json,
)
from replay_inputs import prediction_inputs


def run(capture):
    output = capture / "predictions_replay_private.json"
    if output.exists() or (capture / "inference_report.json").exists():
        raise FileExistsError("Refusing to overwrite inference outputs")
    manifest = load_json(capture / "replay_manifest.json")
    for name, expected in manifest["capture_hashes"].items():
        if fingerprint(capture / name) != expected:
            raise ValueError("Replay capture changed")
    for name, path in (("protocol", ROOT / "PROTOCOL.md"),
                       ("checkpoint", paths.CHECKPOINT), ("config", paths.RUN_CONFIG),
                       ("vocab", paths.VOCAB), ("predictions", paths.PREDICTIONS),
                       ("calibration", paths.ACTIVE_CALIBRATION)):
        if fingerprint(path) != manifest["inputs"][name]:
            raise ValueError("Protocol or frozen input changed")
    available = psutil.virtual_memory().available
    if available < 3 * 1024 ** 3:
        raise RuntimeError("Need at least 3 GiB available RAM with shared embeddings; "
                           "do not stop a user-owned demo without permission.")
    torch = importlib.import_module("torch")
    load_memory_efficient_model = importlib.import_module("memory_loader").load_memory_efficient_model

    torch.set_num_threads(CPU_THREADS)
    targets = load_json(capture / "targets_private.json")
    kt = load_memory_efficient_model(capture / "replay_windows_private.jsonl.gz")
    print(kt.describe(), flush=True)
    missing_content, missing_selected, history_occurrences = 0, 0, 0
    for target in targets:
        record = kt.dataset.records[target["subset_index"]]
        if record["source_record_index"] != target["record_index"]:
            raise ValueError("Subset window ordering differs")
        events = record["events"]
        event = events[target["position"]]
        if event["content_text"] not in kt.dataset.text_to_row:
            raise ValueError("Target question missing exact embedding")
        for e in events[:target["position"]]:
            history_occurrences += 1
            missing_content += e["content_text"] not in kt.dataset.text_to_row
            missing_selected += e["selected_text"] not in kt.dataset.text_to_row
    no_history = {}
    probabilities = {"history": {}, "no_history": {}}
    for condition in ("history", "no_history"):
        chosen = targets
        if condition == "no_history":
            unique = {}
            for target in targets:
                unique.setdefault(target["question_id"], target)
            chosen = list(unique.values())
        for start in range(0, len(chosen), BATCH_SIZE):
            members = chosen[start:start + BATCH_SIZE]
            inputs, positions = [], []
            for target in members:
                encoded = {key: value.numpy() for key, value in
                           kt.dataset[target["subset_index"]].items()}
                inputs_one, position = prediction_inputs(
                    encoded, target["position"], condition == "history")
                inputs.append(inputs_one)
                positions.append(position)
            batch = {key: torch.from_numpy(np.stack([r[key] for r in inputs]))
                     for key in inputs[0]}
            p = kt.probs(batch).cpu().numpy()[np.arange(len(members)), positions]
            if not np.isfinite(p).all() or np.any((p < 0) | (p > 1)):
                raise ValueError("Invalid frozen probabilities")
            for target, value in zip(members, p, strict=True):
                if condition == "history":
                    probabilities[condition][target["target_id"]] = float(value)
                else:
                    no_history[target["question_id"]] = float(value)
            if start % (BATCH_SIZE * 10) == 0:
                print(f"{condition}: {min(start + len(members), len(chosen))}/{len(chosen)}", flush=True)
    for target in targets:
        probabilities["no_history"][target["target_id"]] = no_history[target["question_id"]]
    parity = []
    with np.load(paths.PREDICTIONS, allow_pickle=True) as original:
        length = kt.config["max_seq_len"]
        keys = original["record_idx"].astype(np.int64) * length + original["position"]
        if np.any(keys[1:] <= keys[:-1]):
            raise ValueError("Original prediction locators are not unique/ordered")
        wanted = np.array([t["record_index"] * length + t["position"] for t in targets])
        indices = np.searchsorted(keys, wanted)
        if np.any(indices >= len(keys)) or not np.array_equal(keys[indices], wanted):
            raise ValueError("Target absent from complete prediction dump")
        for name, field in (("skill_idx", "skill_index"), ("item_idx", "item_index"), ("y", "y")):
            if not np.array_equal(original[name][indices], [t[field] for t in targets]):
                raise ValueError("Original prediction target metadata differs")
        expected_split = [2 if t["regime"] == "warm" else 3 for t in targets]
        if not np.array_equal(original["split"][indices], expected_split):
            raise ValueError("Original target split differs")
        raw_students = original["students"]
        unique_students = list(dict.fromkeys(base_student(str(s)) for s in raw_students))
        for target, student_index in zip(targets, original["student_idx"][indices], strict=True):
            if base_student(str(raw_students[student_index])) != unique_students[target["student_code"]]:
                raise ValueError("Original target student differs")
        original_p = original["p"][indices]
        parity = [abs(probabilities["history"][t["target_id"]] - float(old))
                  for t, old in zip(targets, original_p, strict=True)]
    rows = [{**t, "p_history": probabilities["history"][t["target_id"]],
             "p_no_history": probabilities["no_history"][t["target_id"]]} for t in targets]
    write_json(output, rows)
    report = {
        "status": "passed" if max(parity, default=0) <= PARITY_TOLERANCE else "parity_failure_investigate",
        "targets": len(rows), "no_history_distinct_queries": len(no_history),
        "maximum_history_cached_probability_gap": max(parity, default=0),
        "parity_tolerance": PARITY_TOLERANCE,
        "parity_violations": sum(x > PARITY_TOLERANCE for x in parity),
        "history_embedding_fallback_occurrences": {
            "total_preceding_event_occurrences": history_occurrences,
            "content": missing_content, "selected_text": missing_selected},
        "checkpoint": fingerprint(paths.CHECKPOINT),
        "embeddings": fingerprint(paths.TEXT_EMBEDDINGS),
        "predictions": fingerprint(output),
        "replay_manifest": fingerprint(capture / "replay_manifest.json"),
        "method": fingerprint(Path(__file__)),
        "input_method": fingerprint(ROOT / "replay_inputs.py"),
        "storage_method": fingerprint(ROOT / "memory_loader.py"),
        "loader_mode": "strict_frozen_loader_shared_numpy_cpu_embeddings",
        "device": "cpu", "batch_size": BATCH_SIZE, "threads": CPU_THREADS,
    }
    write_json(capture / "inference_report.json", report)
    print(json.dumps(report, indent=2), flush=True)
    if report["status"] != "passed":
        raise RuntimeError("History parity check failed; do not summarize as valid results")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    run(parser.parse_args().capture_dir)
