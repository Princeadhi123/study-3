"""All-target same-CPU unmodified-input versus response-blind verification."""
import argparse
import importlib
import json
from pathlib import Path

import numpy as np
import psutil

from phase4_common import BATCH_SIZE, CPU_THREADS, ROOT, fingerprint, load_json, paths, write_json

CPU_REFERENCE_TOLERANCE = 1e-6


def verify(capture):
    output = capture / "cpu_reference_verification.json"
    if output.exists():
        raise FileExistsError("Refusing to overwrite all-target CPU verification")
    report = load_json(capture / "inference_report.json")
    diagnostic = load_json(capture / "parity_diagnostic.json")
    predictions = capture / "predictions_replay_private.json"
    if fingerprint(predictions) != report["predictions"]:
        raise ValueError("Original CPU predictions changed")
    if fingerprint(paths.CHECKPOINT) != report["checkpoint"]:
        raise ValueError("Checkpoint changed")
    if fingerprint(capture / "replay_manifest.json") != report["replay_manifest"]:
        raise ValueError("Replay capture changed")
    if diagnostic["settings"]["original_dump_device"] != "cuda":
        raise ValueError("Historical-reference diagnostic differs from expected CUDA dump")
    if psutil.virtual_memory().available < 3 * 1024 ** 3:
        raise RuntimeError("Need 3 GiB available for shared embedding loader")
    torch = importlib.import_module("torch")
    torch.set_num_threads(CPU_THREADS)
    kt = importlib.import_module("memory_loader").load_memory_efficient_model(
        capture / "replay_windows_private.jsonl.gz")
    targets = load_json(predictions)
    by_subset = {}
    for target in targets:
        by_subset.setdefault(target["subset_index"], []).append(target)
    original_probabilities = {}
    for start in range(0, len(kt.dataset), BATCH_SIZE):
        indices = list(range(start, min(start + BATCH_SIZE, len(kt.dataset))))
        encoded = [kt.dataset[index] for index in indices]
        batch = {key: torch.stack([r[key] for r in encoded]) for key in encoded[0]}
        p = kt.probs(batch).cpu().numpy()
        for batch_index, subset_index in enumerate(indices):
            for target in by_subset[subset_index]:
                original_probabilities[target["target_id"]] = float(
                    p[batch_index, target["position"]])
        if start % (BATCH_SIZE * 10) == 0:
            print("Verified full-input windows", min(start + BATCH_SIZE, len(kt.dataset)),
                  "/", len(kt.dataset), flush=True)
    differences = [abs(original_probabilities[t["target_id"]] - t["p_history"]) for t in targets]
    result = {
        "status": "passed" if max(differences, default=0) <= CPU_REFERENCE_TOLERANCE else "failed",
        "comparison": "Same-CPU original unmodified dataset inputs versus saved response-blind CPU replay.",
        "targets": len(targets), "windows": len(kt.dataset),
        "maximum_absolute_difference": max(differences, default=0),
        "tolerance": CPU_REFERENCE_TOLERANCE,
        "violations": sum(x > CPU_REFERENCE_TOLERANCE for x in differences),
        "original_cuda_reference_check": report["status"],
        "predictions": fingerprint(predictions),
        "checkpoint": fingerprint(paths.CHECKPOINT),
        "embeddings": fingerprint(paths.TEXT_EMBEDDINGS),
        "replay_manifest": fingerprint(capture / "replay_manifest.json"),
        "diagnostic": fingerprint(capture / "parity_diagnostic.json"),
        "addendum": fingerprint(ROOT / "CPU_REFERENCE_ADDENDUM.md"),
        "method": fingerprint(Path(__file__)),
    }
    write_json(output, result)
    print(json.dumps(result, indent=2), flush=True)
    if result["status"] != "passed":
        raise RuntimeError("All-target same-CPU input-equivalence check failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    verify(parser.parse_args().capture_dir)
