"""Stage 5b -- counterfactual probing of the frozen model for skill dependencies.

This is the layer that makes the knowledge graph *model-grounded* instead of
a restatement of the school timetable. It asks, for every candidate ordered
pair of skills (A, B):

    Holding a real student history fixed, if we change the outcome of that
    student's attempts at skill A -- all correct vs. all incorrect -- how
    much does the frozen model's predicted P(correct) on their later skill-B
    items move?

    delta_p(A -> B) = mean[ P(correct on B | A succeeded)
                            - P(correct on B | A failed) ]

A large positive `delta_p` means the model has learned to route evidence
from A into its belief about B -- it treats A as load-bearing for B. That is
a dependency *inside the model*, established by intervention on real
histories rather than by correlating outcomes, which is why it is
qualitatively better evidence than co-occurrence. It is still not a
randomised causal claim about learning: it inherits whatever the model
absorbed from this curriculum, and `build_knowledge_graph.py` labels it
accordingly.


## What exactly is intervened on

Only the **correctness channel** of the source skill's past events. The
previous-step `selected_text` embedding (variant D's extra input) is left
untouched, because rewriting it would mean intervening on two inputs at once
and the resulting shift could not be attributed to either. So `delta_p`
measures the influence of *observed success at A*, holding the semantic
content of what the student actually typed/picked constant. Pass
`--neutralize-selected` to additionally blank the selected-answer text to
the `[NO_OPTIONS]` sentinel at the intervened positions; the two settings
bracket the total contribution of A's history and are worth reporting
together if the numbers disagree.

Targets are restricted to positions that occur *after* the student's first
attempt at A within the same window -- the model is causal, so earlier
positions cannot respond to the intervention and would only dilute the
estimate with structural zeros.


## Sampling and error bars

Scoring every (window, source-skill) pair is ~650k sequence forward passes.
This samples `--max-windows` windows instead, so every number is a Monte
Carlo estimate. Observations inside one window are strongly dependent, so
the reported standard error is **clustered by window** (per-window means
first, then the spread across windows) rather than computed over raw
observations, which would understate it by an order of magnitude.

Requires the frozen model, hence the embedding table. GPU strongly advised.

    python probe_predictive_dependency.py --device cuda --max-windows 4000
"""
import argparse
import csv
import json
import math
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import torch

import paths
from frozen_model import load_frozen_model

NO_OPTIONS_SENTINEL = "[NO_OPTIONS]"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--checkpoint", default=str(paths.CHECKPOINT))
    p.add_argument("--sequences", default=str(paths.SEQUENCES),
                   help="Override for smoke tests on a truncated sequences file.")
    p.add_argument("--out", default=str(paths.ARTIFACTS / "predictive_dependency.csv"))
    p.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--max-windows", type=int, default=4000,
                   help="Number of student windows sampled for probing.")
    p.add_argument("--max-sources-per-window", type=int, default=8)
    p.add_argument("--min-source-events", type=int, default=3,
                   help="A source skill needs at least this many attempts in the window "
                        "for the intervention to be a meaningful change of evidence.")
    p.add_argument("--min-observations", type=int, default=30,
                   help="Drop (A, B) pairs with fewer target observations than this.")
    p.add_argument("--min-windows-per-pair", type=int, default=5,
                   help="Drop (A, B) pairs seen in fewer windows than this -- a pair "
                        "resting on one or two students is noise.")
    p.add_argument("--neutralize-selected", action="store_true",
                   help="Also blank the selected-answer text at intervened positions.")
    p.add_argument("--seed", type=int, default=20260918)
    return p.parse_args()


def main():
    args = parse_args()
    print(f"Loading frozen model on {args.device} ...", flush=True)
    kt = load_frozen_model(checkpoint=Path(args.checkpoint),
                           sequences=Path(args.sequences), device=args.device)
    print(kt.describe(), flush=True)

    dataset = kt.dataset
    sentinel_row = dataset.text_to_row.get(NO_OPTIONS_SENTINEL)
    if args.neutralize_selected and sentinel_row is None:
        raise SystemExit(f"{NO_OPTIONS_SENTINEL!r} is absent from the embedding table; "
                         f"cannot neutralize the selected-answer channel.")

    rng = np.random.default_rng(args.seed)
    n_windows = len(dataset.records)
    sample = rng.permutation(n_windows)[:min(args.max_windows, n_windows)]
    print(f"Probing {len(sample):,} of {n_windows:,} windows", flush=True)

    # Accumulators: per (A, B) -> per-window mean deltas (clustered SE).
    per_window_deltas = defaultdict(list)
    n_obs = defaultdict(int)

    jobs = []  # (window_idx, source_skill, batch_pos_of_pair)
    pending = []  # tensors awaiting a forward pass

    def flush():
        if not pending:
            return
        batch = {k: torch.stack([d[k] for d in pending]) for k in pending[0]}
        probs = kt.probs(batch).cpu().numpy()
        # pending is laid out as [A_correct, A_incorrect] per job, in order.
        for j, (widx, src, valid_mask, skills) in enumerate(jobs):
            delta = probs[2 * j] - probs[2 * j + 1]
            d = delta[valid_mask]
            tgt = skills[valid_mask]
            for b in np.unique(tgt):
                if b == src:
                    continue  # self-influence is not an edge
                m = tgt == b
                per_window_deltas[(src, int(b))].append(float(d[m].mean()))
                n_obs[(src, int(b))] += int(m.sum())
        pending.clear()
        jobs.clear()

    t0 = time.time()
    for n_done, widx in enumerate(sample, 1):
        base = dataset[int(widx)]
        skills = base["skill"].numpy()
        real = base["attn_mask"].numpy() > 0
        if real.sum() < 2:
            continue

        counts = defaultdict(list)
        for pos in np.nonzero(real)[0]:
            s = int(skills[pos])
            if s > 1:  # skip __PAD__ / __UNK__
                counts[s].append(int(pos))

        candidates = [(s, ps) for s, ps in counts.items() if len(ps) >= args.min_source_events]
        # Prefer the skills with the most evidence to flip: a bigger change of
        # evidence gives a better-conditioned estimate of the model's response.
        candidates.sort(key=lambda kv: -len(kv[1]))
        for src, positions in candidates[:args.max_sources_per_window]:
            first = positions[0]
            # Causal model: only positions after A's first attempt can react.
            valid = real.copy()
            valid[:first + 1] = False
            valid &= skills != src
            if not valid.any():
                continue

            pos_idx = torch.tensor(positions, dtype=torch.long)
            for value in (1.0, 0.0):
                d = {k: v.clone() for k, v in base.items()}
                d["correct"][pos_idx] = value
                if args.neutralize_selected:
                    d["selected_text_idx"][pos_idx] = sentinel_row
                pending.append(d)
            jobs.append((int(widx), src, valid, skills))

            if len(pending) >= args.batch_size:
                flush()

        if n_done % 250 == 0:
            el = time.time() - t0
            frac = n_done / len(sample)
            print(f"  {n_done:,}/{len(sample):,} windows ({frac:.1%}, {el:.0f}s, "
                  f"~{el / frac - el:.0f}s left), {len(per_window_deltas):,} pairs so far",
                  flush=True)
    flush()

    rows = []
    for (a, b), vals in per_window_deltas.items():
        if n_obs[(a, b)] < args.min_observations or len(vals) < args.min_windows_per_pair:
            continue
        arr = np.array(vals)
        mean = float(arr.mean())
        # Clustered by window: the spread of per-window means, not of raw
        # observations. Events within a window share a student and a history.
        se = float(arr.std(ddof=1) / math.sqrt(len(arr))) if len(arr) > 1 else float("nan")
        rows.append({
            "source_skill_id": kt.idx_to_skill[a],
            "target_skill_id": kt.idx_to_skill[b],
            "source_idx": a, "target_idx": b,
            "delta_p": round(mean, 6),
            "se_clustered": round(se, 6),
            "t_stat": round(mean / se, 3) if se and not math.isnan(se) and se > 0 else "",
            "n_observations": n_obs[(a, b)],
            "n_windows": len(vals),
        })
    rows.sort(key=lambda r: -r["delta_p"])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else
                           ["source_skill_id", "target_skill_id", "source_idx", "target_idx",
                            "delta_p", "se_clustered", "t_stat", "n_observations", "n_windows"])
        w.writeheader()
        w.writerows(rows)

    meta = {
        "checkpoint": str(args.checkpoint),
        "windows_probed": int(len(sample)),
        "pairs_kept": len(rows),
        "pairs_seen": len(per_window_deltas),
        "intervention": ("correctness channel only" if not args.neutralize_selected
                         else "correctness channel + selected-answer text neutralized"),
        "se": "clustered by window",
        "elapsed_sec": round(time.time() - t0, 1),
        "caveat": "delta_p is a dependency inside the frozen model, established by "
                  "intervention on real histories. It is not a randomised causal claim "
                  "about human learning and inherits this curriculum's biases.",
    }
    out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"\nWrote {out} ({len(rows):,} pairs kept of {len(per_window_deltas):,} seen)")
    for r in rows[:10]:
        print(f"  delta_p={r['delta_p']:+.4f} (se {r['se_clustered']:.4f}, "
              f"n={r['n_observations']}) {r['source_skill_id']} -> {r['target_skill_id']}")


if __name__ == "__main__":
    main()
