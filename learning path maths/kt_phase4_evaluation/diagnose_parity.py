"""Discriminate input-construction errors from reference-backend differences.

This is a numerical diagnostic, not target selection for performance reporting.
All original evaluation targets remain unchanged.
"""
import argparse
import gzip
import importlib
import json
from pathlib import Path

import numpy as np

from phase4_common import CPU_THREADS, fingerprint, load_json, paths, write_json
from replay_inputs import prediction_inputs


def diagnose(capture):
    output = capture / "parity_diagnostic.json"
    if output.exists():
        raise FileExistsError("Refusing to overwrite numerical diagnostics")
    rows = load_json(capture / "predictions_replay_private.json")
    with np.load(paths.PREDICTIONS, allow_pickle=True) as original:
        meta = json.loads(str(original["meta"]))
        keys = original["record_idx"].astype(np.int64) * 400 + original["position"]
        indices = np.searchsorted(keys, [r["record_index"] * 400 + r["position"] for r in rows])
        cached = original["p"][indices]
    by_id = {r["target_id"]: float(p) for r, p in zip(rows, cached, strict=True)}
    ranked = sorted(rows, key=lambda r: (-abs(r["p_history"] - by_id[r["target_id"]]),
                                       r["record_index"], r["position"]))
    selected = ranked[:12]
    wanted = {r["record_index"] for r in selected}
    records = {}
    with gzip.open(paths.SEQUENCES, "rt", encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            if index in wanted:
                record = json.loads(line)
                record["student_id"] = "diagnostic_private"
                record["source_record_index"] = index
                records[index] = record
            if len(records) == len(wanted):
                break
    if set(records) != wanted:
        raise ValueError("Diagnostic source windows missing")
    subset = capture / "diagnostic_full_windows_private.jsonl.gz"
    if subset.exists():
        raise FileExistsError("Refusing to overwrite diagnostic windows")
    order = sorted(records)
    with gzip.open(subset, "xt", encoding="utf-8") as stream:
        for index in order:
            stream.write(json.dumps(records[index], ensure_ascii=True) + "\n")
    torch = importlib.import_module("torch")
    torch.set_num_threads(CPU_THREADS)
    loader = importlib.import_module("memory_loader")
    kt = loader.load_memory_efficient_model(subset)
    source_indices = {index: i for i, index in enumerate(order)}

    def predict(batch_rows, positions):
        batch = {key: torch.from_numpy(np.stack([r[key] for r in batch_rows]))
                 for key in batch_rows[0]}
        return kt.probs(batch).cpu().numpy()[np.arange(len(positions)), positions]

    full_inputs, blind_inputs, positions = [], [], []
    for row in selected:
        encoded = {key: value.numpy() for key, value in
                   kt.dataset[source_indices[row["record_index"]]].items()}
        full_inputs.append(encoded)
        blind, position = prediction_inputs(encoded, row["position"], True)
        blind_inputs.append(blind)
        positions.append(position)
    settings = {
        "torch_version": torch.__version__, "original_dump_device": meta.get("device"),
        "original_dump_meta": meta,
        "default_float32_matmul_precision": torch.get_float32_matmul_precision(),
        "default_mha_fastpath": torch.backends.mha.get_fastpath_enabled(),
        "default_mkldnn": torch.backends.mkldnn.enabled,
    }
    fast_full = predict(full_inputs, positions)
    fast_blind = predict(blind_inputs, positions)
    mutated = [{key: values.copy() for key, values in row.items()} for row in full_inputs]
    for row, position in zip(mutated, positions, strict=True):
        row["correct"][position:] = 1 - row["correct"][position:]
        row["selected_text_idx"][position:] = 0
        row["rt"][position:] = 50
        row["rt_mask"][position:] = 1
        row["attempt"][position:] = 7
        row["time_bin_ids"][position:] = 31
    fast_mutated = predict(mutated, positions)
    torch.backends.mha.set_fastpath_enabled(False)
    dense_full = predict(full_inputs, positions)
    dense_blind = predict(blind_inputs, positions)
    dense_mutated = predict(mutated, positions)
    torch.backends.mkldnn.enabled = False
    plain_full = predict(full_inputs, positions)
    original_values = np.array([by_id[r["target_id"]] for r in selected])
    previous_values = np.array([r["p_history"] for r in selected])
    report = {
        "purpose": "Diagnose numerical parity, not change evaluated target sample.",
        "settings": settings,
        "diagnostic_cases": len(selected),
        "maximum_absolute_differences": {
            "fast_full_vs_response_blind": float(np.max(np.abs(fast_full - fast_blind))),
            "fast_full_vs_post_response_future_mutation": float(np.max(np.abs(fast_full - fast_mutated))),
            "dense_full_vs_response_blind": float(np.max(np.abs(dense_full - dense_blind))),
            "dense_full_vs_post_response_future_mutation": float(np.max(np.abs(dense_full - dense_mutated))),
            "fast_vs_dense_full": float(np.max(np.abs(fast_full - dense_full))),
            "dense_vs_plain_full": float(np.max(np.abs(dense_full - plain_full))),
            "fast_full_vs_original_dump": float(np.max(np.abs(fast_full - original_values))),
            "dense_full_vs_original_dump": float(np.max(np.abs(dense_full - original_values))),
            "plain_full_vs_original_dump": float(np.max(np.abs(plain_full - original_values))),
            "fast_blind_vs_previous_replay": float(np.max(np.abs(fast_blind - previous_values))),
        },
        "cases": [{
            "target_id": row["target_id"], "position": row["position"],
            "original_cached": float(old), "previous_blind": float(previous),
            "fast_full": float(ff), "fast_blind": float(fb), "fast_mutated": float(fm),
            "dense_full": float(df), "dense_blind": float(db), "dense_mutated": float(dm),
            "plain_full": float(pf),
        } for row, old, previous, ff, fb, fm, df, db, dm, pf in zip(
            selected, original_values, previous_values, fast_full, fast_blind,
            fast_mutated, dense_full, dense_blind, dense_mutated, plain_full, strict=True)],
        "checkpoint": fingerprint(paths.CHECKPOINT),
        "method": fingerprint(Path(__file__)),
    }
    write_json(output, report)
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    diagnose(parser.parse_args().capture_dir)
