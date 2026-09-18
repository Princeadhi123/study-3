"""Stage 2 -- one forward pass over the frozen model; dump every held-out prediction.

Conformal calibration, coverage verification, and the knowledge graph's
`predictive_dependency` layer all need the same thing: the frozen model's
probability for each held-out event, paired with the truth, the student, the
skill, the item and the split it came from. Computing that once and caching
it keeps every downstream stage cheap and makes them reproducible without a
GPU.

Output: `artifacts/predictions_d.npz`, one entry per evaluable event, with
parallel arrays:

    record_idx   int32   index into the sequences file (a student *window*)
    position     int16   position within that window
    student_idx  int32   index into the `students` string array
    skill_idx    int16   the model's skill_vocab index
    item_idx     int32   the model's item_vocab index (1 = __UNK__, which is
                         what every cold item resolves to by construction)
    split        int8    SPLIT_CODE: 1=val 2=test_warm 3=test_cold_item
    y            int8    observed correctness
    p            float32 P(correct) from the frozen model

Only `val`, `test_warm` and `test_cold_item` positions are kept: `train`,
`skip_cold_in_train` and `context` positions are either seen during training
or excluded from evaluation by construction, so they carry no information
about held-out behaviour and would corrupt a conformal calibration set.

## Cost

This is ~3.9M scored events over 54,398 windows of length 400 through a
3-layer d192 transformer. On a GPU it is a few minutes; on CPU it is hours.
Prefer running it inside a LUMI allocation with `--device cuda` and copying
the (small) `.npz` back. `--limit-records` exists for smoke tests -- the
resulting calibration is NOT valid for deployment, and the output is tagged
`partial=True` so the calibration stage refuses it.

Note on memory: `KTSequenceDataset` materialises all 54,398 parsed windows
up front (it is Phase 1's loader, reused deliberately so inference sees
exactly the training-time featurisation). Budget several GB of RAM.

    python dump_predictions.py --device cuda
"""
import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader

import paths
from frozen_model import SPLIT_CODE, embedding_coverage, load_frozen_model

# The three splits that are genuinely held out. See Phase 1's README,
# "Evaluation splits (why there are four, not two)".
KEEP_SPLITS = {
    SPLIT_CODE["val"]: "val",
    SPLIT_CODE["test_warm"]: "test_warm",
    SPLIT_CODE["test_cold_item"]: "test_cold_item",
}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--checkpoint", default=str(paths.CHECKPOINT))
    p.add_argument("--sequences", default=str(paths.SEQUENCES),
                   help="Override for smoke tests on a truncated sequences file.")
    p.add_argument("--out", default=str(paths.PREDICTIONS))
    p.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    p.add_argument("--batch-size", type=int, default=128)
    p.add_argument("--num-workers", type=int, default=0)
    p.add_argument("--limit-records", type=int, default=0,
                   help="Score only the first N windows (smoke test; output is marked partial).")
    p.add_argument("--min-coverage", type=float, default=0.99,
                   help="Abort if fewer than this fraction of event texts resolve in the "
                        "embedding table -- the signature of a mismatched table.")
    return p.parse_args()


def main():
    args = parse_args()
    device = args.device
    print(f"Loading frozen model on {device} ...", flush=True)
    kt = load_frozen_model(checkpoint=Path(args.checkpoint),
                           sequences=Path(args.sequences), device=device)
    print(kt.describe(), flush=True)

    cov = embedding_coverage(kt.dataset)
    print(f"Embedding table coverage: content {cov['content_hit_rate']:.4f}, "
          f"selected {cov['selected_hit_rate']:.4f}", flush=True)
    if cov["content_hit_rate"] < args.min_coverage:
        raise SystemExit(
            f"Only {cov['content_hit_rate']:.2%} of question texts resolve in "
            f"{paths.TEXT_EMBEDDINGS.name}. A miss silently maps to row 0 -- a real "
            f"vector, not an 'unknown' slot -- so the model would run on confidently "
            f"wrong content features. Refusing to produce a calibration set from this."
        )

    dataset = kt.dataset
    if args.limit_records:
        dataset.records = dataset.records[:args.limit_records]
        print(f"PARTIAL RUN: scoring only {len(dataset.records)} windows.", flush=True)

    students = [r["student_id"] for r in dataset.records]
    student_to_idx = {}
    student_codes = np.empty(len(students), dtype=np.int32)
    uniq_students = []
    for i, s in enumerate(students):
        code = student_to_idx.get(s)
        if code is None:
            code = len(uniq_students)
            student_to_idx[s] = code
            uniq_students.append(s)
        student_codes[i] = code
    print(f"{len(dataset.records):,} windows from {len(uniq_students):,} distinct students",
          flush=True)

    # shuffle=False is load-bearing: batches arrive in dataset order, so the
    # running offset below reconstructs each row's record_idx exactly.
    loader = DataLoader(dataset, batch_size=args.batch_size, shuffle=False,
                        num_workers=args.num_workers)

    out = {k: [] for k in ("record_idx", "position", "skill_idx", "item_idx",
                           "split", "y", "p")}
    offset = 0
    t0 = time.time()
    for bi, batch in enumerate(loader):
        probs = kt.probs(batch).cpu().numpy()
        split = batch["split"].numpy()
        keep = np.isin(split, list(KEEP_SPLITS))
        if keep.any():
            rows, cols = np.nonzero(keep)
            out["record_idx"].append((rows + offset).astype(np.int32))
            out["position"].append(cols.astype(np.int16))
            out["skill_idx"].append(batch["skill"].numpy()[rows, cols].astype(np.int16))
            out["item_idx"].append(batch["item"].numpy()[rows, cols].astype(np.int32))
            out["split"].append(split[rows, cols].astype(np.int8))
            out["y"].append(batch["correct"].numpy()[rows, cols].astype(np.int8))
            out["p"].append(probs[rows, cols].astype(np.float32))
        offset += split.shape[0]
        if (bi + 1) % 50 == 0:
            done = offset / len(dataset.records)
            elapsed = time.time() - t0
            print(f"  {offset:,}/{len(dataset.records):,} windows "
                  f"({done:.1%}, {elapsed:.0f}s elapsed, "
                  f"~{elapsed / max(done, 1e-9) - elapsed:.0f}s left)", flush=True)

    arrays = {k: np.concatenate(v) for k, v in out.items()}
    arrays["student_idx"] = student_codes[arrays["record_idx"]]

    meta = {
        "checkpoint": str(args.checkpoint),
        "variant": kt.config["variant"],
        "seed": kt.config.get("seed"),
        "device": device,
        "n_events": int(len(arrays["p"])),
        "n_windows": len(dataset.records),
        "n_students": len(uniq_students),
        "partial": bool(args.limit_records),
        "embedding_coverage": cov,
        "split_code": {name: int(code) for name, code in SPLIT_CODE.items()},
        "elapsed_sec": round(time.time() - t0, 1),
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_path, students=np.array(uniq_students, dtype=object),
                        meta=json.dumps(meta), **arrays)

    print(f"\nWrote {out_path}  ({meta['n_events']:,} scored events)")
    for code, name in KEEP_SPLITS.items():
        m = arrays["split"] == code
        if m.any():
            # Reproducing Phase 1's headline accuracy here is the end-to-end
            # check that the frozen model was reassembled correctly; compare
            # against runs/comparison.json before trusting anything downstream.
            acc = ((arrays["p"][m] >= 0.5).astype(np.int8) == arrays["y"][m]).mean()
            print(f"  {name:<16} n={m.sum():>10,}  mean_p={arrays['p'][m].mean():.4f}  "
                  f"base_rate={arrays['y'][m].mean():.4f}  acc={acc:.4f}")


if __name__ == "__main__":
    main()
