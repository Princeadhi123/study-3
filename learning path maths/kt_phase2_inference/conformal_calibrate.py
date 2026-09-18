"""Stage 3 -- offline conformal calibration of the frozen variant-D model.

Produces `artifacts/conformal_calibration.json` (the constants the live gate
reads) and `artifacts/conformal_coverage_report.json` (the empirical evidence
that those constants actually deliver the coverage they claim).

Nothing here touches the model's weights. Conformal prediction is a
post-hoc wrapper: the model stays exactly as trained, and calibration only
decides *how much of its output to trust*.


## Two estimands, because "an interval on a coin flip" is not a thing

**Item level -- conformal prediction SETS, not an interval.** The label is
binary, so a split-conformal band `[p - q, p + q]` around the predicted
probability would have the *same width for every student and every item*:
it reduces exactly to thresholding `p`, dressed in conformal notation. The
construction that carries real information is the least-ambiguous
set-valued classifier (Sadinle et al. 2019): nonconformity
`s = 1 - p(y | x)`, and the prediction set is every label whose score falls
under the calibrated quantile. Its output is one of `{correct}`,
`{incorrect}`, `{both}` or `{}` -- which maps 1:1 onto the three
intervention statuses, with the ambiguous band *derived from data* rather
than hand-picked.

**Checkpoint level -- a genuine interval, on the mastery RATE.** Bounds only
become non-trivial once the estimand is an aggregate: "over the next k items
of this skill, this student's success rate is in [0.41, 0.78]". To make the
width vary with the model's own uncertainty rather than be constant, the
nonconformity is *normalized* (Papadopoulos et al.) by the Poisson-binomial
standard deviation the model itself implies for that specific set of items,
`sigma = sqrt(sum p_j (1 - p_j)) / k`:

    s = |realized_rate - mean_p| / max(sigma, sigma_floor)
    interval = mean_p +- q_hat * sigma

A checkpoint over k items the model is sure about gets a tight interval; one
over items it is unsure about gets a wide one.


## Mondrian (group-conditional), not marginal

Two taxonomies, both defensible and both necessary:

1. **Item regime (warm vs. cold).** Phase 1's own numbers show the gap:
   test_warm AUC .922 vs test_cold_item .913. A single marginal quantile
   would be an average of the two and would under-cover exactly the cold
   regime -- i.e. genuinely novel questions, which is the regime a live
   tutoring loop spends most of its time in. The regime is known at
   inference (is this item in `item_vocab`?), so it is a valid taxonomy.

2. **Label-conditional.** ~88% of interactions are correct. Marginal 90%
   coverage can therefore be achieved while covering the `incorrect` class
   far worse -- and `incorrect` is precisely the class the gate exists to
   detect. Label-conditional (Mondrian-by-class) conformal calibrates a
   separate quantile per class, so coverage holds for *both*. The true
   label is unknown at inference, but that is fine: each candidate label is
   tested against its own threshold.

Groups thinner than `--min-group-n` fall back to the pooled quantile, and
the fallback is recorded in the output rather than applied silently.


## What breaks, and why it is reported rather than hidden

Conformal's guarantee assumes calibration and test data are *exchangeable*.
Two violations here, both real:

- **The splits are chronological.** `val` is the middle 15% of each
  student's history and `test_warm` the final 15%; deployment is further
  into the future still. Calibration and deployment are therefore not
  exchangeable, and the 1-alpha guarantee is approximate. This cannot be
  engineered away with this data; it is measured instead (coverage is
  reported on a held-out half, so drift shows up as a gap between nominal
  and empirical coverage) and stated as a limitation.
- **`val` was used to select the checkpoint** (epoch 45, by joint score).
  Its residuals are optimistically biased, so calibrating on it would
  under-cover. `val` is consequently used for *diagnosis only* here; the
  calibration set is a **by-student** half of `test_warm`, and coverage is
  verified on the students in the other half. Splitting by student, not by
  event, matters: two events from the same student are strongly dependent,
  so an event-level split would leak and flatter the coverage numbers.


## Choosing alpha

With an accurate model, a small alpha is what produces a usefully-sized
ambiguous band: at alpha=0.10 an 89%-accurate model returns a singleton
almost every time and `UNCERTAIN_BEHAVIOR` nearly never fires. `--alpha` is
therefore an *intervention budget* knob, and `--alpha-sweep` reports the
status mix and empirical coverage across a range so the operating point can
be chosen deliberately instead of inherited from convention.

    python conformal_calibrate.py --alpha 0.10 --alpha-sweep
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

import paths

WARM, COLD = "warm", "cold"
SPLIT_VAL, SPLIT_WARM, SPLIT_COLD = 1, 2, 3


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--predictions", default=str(paths.PREDICTIONS))
    p.add_argument("--out", default=str(paths.CALIBRATION))
    p.add_argument("--coverage-out", default=str(paths.COVERAGE_REPORT))
    p.add_argument("--alpha", type=float, default=0.10,
                   help="Target miscoverage rate; coverage guarantee is 1 - alpha.")
    p.add_argument("--alpha-sweep", action="store_true",
                   help="Also report status mix / empirical coverage at several alphas.")
    p.add_argument("--calib-frac", type=float, default=0.5,
                   help="Fraction of test_warm STUDENTS used for calibration; the rest "
                        "are held out to verify coverage.")
    p.add_argument("--checkpoint-k", type=int, default=10,
                   help="Number of consecutive same-skill attempts forming one checkpoint.")
    p.add_argument("--sigma-floor", type=float, default=0.02,
                   help="Lower bound on the normalizing sigma, so a checkpoint the model "
                        "is (over)confident about cannot produce a zero-width interval.")
    p.add_argument("--min-group-n", type=int, default=200,
                   help="Groups smaller than this fall back to the pooled quantile.")
    p.add_argument("--seed", type=int, default=20260918)
    p.add_argument("--allow-partial", action="store_true",
                   help="Permit calibrating from a --limit-records prediction dump "
                        "(smoke tests only; never for deployment).")
    return p.parse_args()


def conformal_quantile(scores: np.ndarray, alpha: float) -> float:
    """The finite-sample-corrected (1-alpha) conformal quantile.

    The correction is `ceil((n + 1)(1 - alpha)) / n`, not the plain empirical
    quantile: it is what makes coverage hold at finite n rather than only
    asymptotically. When the correction exceeds 1 the sample is too small to
    certify this alpha at all, and the honest answer is `inf` -- a threshold
    that admits every label, i.e. the set degrades to "don't know" rather
    than to a false claim of confidence.
    """
    n = len(scores)
    if n == 0:
        return float("inf")
    level = np.ceil((n + 1) * (1 - alpha)) / n
    if level > 1.0:
        return float("inf")
    return float(np.quantile(scores, level, method="higher"))


def load_predictions(path: Path, allow_partial: bool) -> dict:
    paths.require(path, "Run dump_predictions.py first (GPU strongly recommended).")
    data = np.load(path, allow_pickle=True)
    meta = json.loads(str(data["meta"]))
    if meta.get("partial") and not allow_partial:
        raise SystemExit(
            f"{path} was produced with --limit-records and covers only part of the data. "
            f"A conformal quantile from a non-representative subset carries no guarantee. "
            f"Re-run dump_predictions.py in full, or pass --allow-partial for a smoke test."
        )
    out = {k: data[k] for k in ("record_idx", "position", "student_idx", "skill_idx",
                                "item_idx", "split", "y", "p")}
    out["students"] = data["students"]
    out["meta"] = meta
    return out


def partition_students(n_students: int, calib_frac: float, seed: int):
    """Split STUDENTS (not events) into calibration and evaluation halves.

    Events from one student are strongly dependent -- same learner, same
    curriculum position, often the same session. An event-level split would
    put near-duplicates on both sides and report a coverage number that is
    optimistic by construction.
    """
    rng = np.random.default_rng(seed)
    perm = rng.permutation(n_students)
    n_calib = int(round(n_students * calib_frac))
    is_calib = np.zeros(n_students, dtype=bool)
    is_calib[perm[:n_calib]] = True
    return is_calib


def lac_scores(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Nonconformity `1 - p(true label)` for the least-ambiguous set classifier."""
    return np.where(y == 1, 1.0 - p, p)


def fit_item_level(p, y, regime, alpha, min_group_n):
    """Label-conditional, regime-conditional quantiles for the prediction sets."""
    scores = lac_scores(p, y)
    pooled = {str(c): conformal_quantile(scores[y == c], alpha) for c in (0, 1)}
    pooled_n = {str(c): int((y == c).sum()) for c in (0, 1)}

    groups, fallbacks = {}, []
    for g in (WARM, COLD):
        gm = regime == g
        q, ns = {}, {}
        for c in (0, 1):
            m = gm & (y == c)
            ns[str(c)] = int(m.sum())
            if m.sum() < min_group_n:
                q[str(c)] = pooled[str(c)]
                fallbacks.append({"group": g, "label": c, "n": int(m.sum()),
                                  "reason": f"n < min_group_n ({min_group_n})"})
            else:
                q[str(c)] = conformal_quantile(scores[m], alpha)
        groups[g] = {"q_hat": q, "n": ns}

    return {
        "method": "mondrian_label_conditional_lac",
        "reference": "Sadinle, Lei & Wasserman (2019), least-ambiguous set-valued classifier; "
                     "Mondrian taxonomy = (item_regime, true_label), Vovk et al.",
        "alpha": alpha,
        "groups": groups,
        "pooled": {"q_hat": pooled, "n": pooled_n},
        "fallbacks": fallbacks,
    }


def predict_sets(p, regime, cal):
    """Apply the calibrated thresholds: label c is in the set iff 1 - p(c) <= q_hat[c].

    Returned as two boolean arrays (`incl_0`, `incl_1`) rather than an object
    array, so the four possible sets are cheap to count downstream.
    """
    q0 = np.array([cal["groups"][g]["q_hat"]["0"] for g in regime])
    q1 = np.array([cal["groups"][g]["q_hat"]["1"] for g in regime])
    return (p <= q0), ((1.0 - p) <= q1)


def status_of(incl_0, incl_1):
    """Map prediction sets onto the three intervention statuses.

    {correct}            -> MASTERY_SAFE
    {incorrect}          -> CONFIDENT_STRUGGLE
    {correct,incorrect}  -> UNCERTAIN_BEHAVIOR  (model cannot rule either out)
    {}                   -> UNCERTAIN_BEHAVIOR  (both labels non-conforming:
                                                 the observation is atypical of
                                                 anything seen in calibration --
                                                 an abstain, not a confident call)
    """
    status = np.full(len(incl_0), "UNCERTAIN_BEHAVIOR", dtype=object)
    status[incl_1 & ~incl_0] = "MASTERY_SAFE"
    status[incl_0 & ~incl_1] = "CONFIDENT_STRUGGLE"
    return status


def build_checkpoints(pred, mask, k):
    """Group held-out events into non-overlapping k-attempt, same-skill blocks.

    A "checkpoint" is k consecutive attempts by one student on one skill
    inside one window -- the unit a tutoring system would actually decide on.
    Blocks are built per (window, skill) in position order and never span
    windows, so a block is always a genuine contiguous stretch of that
    student's practice on that skill.
    """
    idx = np.nonzero(mask)[0]
    order = np.lexsort((pred["position"][idx], pred["skill_idx"][idx], pred["record_idx"][idx]))
    idx = idx[order]

    blocks = defaultdict(list)
    for i in idx:
        blocks[(int(pred["record_idx"][i]), int(pred["skill_idx"][i]))].append(i)

    out = []
    for (record_idx, skill_idx), members in blocks.items():
        for start in range(0, len(members) - k + 1, k):
            chunk = np.array(members[start:start + k])
            out.append((record_idx, skill_idx, chunk))
    return out


def checkpoint_features(pred, chunks, sigma_floor):
    mean_p, realized, sigma, regime, students, skills = [], [], [], [], [], []
    for _record_idx, skill_idx, chunk in chunks:
        p = pred["p"][chunk].astype(np.float64)
        mean_p.append(p.mean())
        realized.append(pred["y"][chunk].mean())
        # Poisson-binomial SD of the mean of k independent Bernoulli(p_j):
        # the spread the model itself implies for this particular checkpoint.
        sigma.append(max(np.sqrt((p * (1 - p)).sum()) / len(p), sigma_floor))
        # A checkpoint counts as cold if any of its items is one the model
        # never trained on -- the harder regime dominates.
        regime.append(COLD if np.any(pred["split"][chunk] == SPLIT_COLD) else WARM)
        students.append(int(pred["student_idx"][chunk[0]]))
        skills.append(int(skill_idx))
    return (np.array(mean_p), np.array(realized), np.array(sigma),
            np.array(regime, dtype=object), np.array(students), np.array(skills))


def fit_checkpoint_level(mean_p, realized, sigma, regime, alpha, min_group_n, k, sigma_floor):
    scores = np.abs(realized - mean_p) / sigma
    pooled = conformal_quantile(scores, alpha)
    groups, fallbacks = {}, []
    for g in (WARM, COLD):
        m = regime == g
        if m.sum() < min_group_n:
            groups[g] = {"q_hat": pooled, "n": int(m.sum())}
            fallbacks.append({"group": g, "n": int(m.sum()),
                              "reason": f"n < min_group_n ({min_group_n})"})
        else:
            groups[g] = {"q_hat": conformal_quantile(scores[m], alpha), "n": int(m.sum())}
    return {
        "method": "normalized_split_conformal_mastery_rate",
        "reference": "Papadopoulos et al., normalized nonconformity; sigma = "
                     "Poisson-binomial SD of the mean of the k predicted probabilities.",
        "alpha": alpha,
        "k": k,
        "sigma_floor": sigma_floor,
        "groups": groups,
        "pooled": {"q_hat": pooled, "n": int(len(scores))},
        "fallbacks": fallbacks,
    }


def coverage_block(p, y, regime, cal, label=""):
    incl_0, incl_1 = predict_sets(p, regime, cal)
    covered = np.where(y == 1, incl_1, incl_0)
    size = incl_0.astype(int) + incl_1.astype(int)
    status = status_of(incl_0, incl_1)
    out = {
        "label": label,
        "n": int(len(y)),
        "empirical_coverage": float(covered.mean()) if len(y) else None,
        "mean_set_size": float(size.mean()) if len(y) else None,
        "status_mix": {s: float((status == s).mean()) for s in
                       ("MASTERY_SAFE", "CONFIDENT_STRUGGLE", "UNCERTAIN_BEHAVIOR")},
    }
    # Class-conditional coverage is the number that matters: marginal
    # coverage is dominated by the ~88% correct class and can look fine
    # while the `incorrect` class -- the one the gate exists to catch -- is
    # badly under-covered.
    for c in (0, 1):
        m = y == c
        out[f"coverage_y{c}"] = float(covered[m].mean()) if m.any() else None
        out[f"n_y{c}"] = int(m.sum())
    # Among events the gate would act on, how often was it right? Not a
    # conformal quantity -- a deployment-relevant one.
    acted = status == "CONFIDENT_STRUGGLE"
    out["confident_struggle_precision"] = float((y[acted] == 0).mean()) if acted.any() else None
    safe = status == "MASTERY_SAFE"
    out["mastery_safe_precision"] = float((y[safe] == 1).mean()) if safe.any() else None
    return out


def main():
    args = parse_args()
    pred = load_predictions(Path(args.predictions), args.allow_partial)
    n_students = len(pred["students"])

    is_calib_student = partition_students(n_students, args.calib_frac, args.seed)
    student_is_calib = is_calib_student[pred["student_idx"]]

    # `val` is diagnostic only -- it selected the checkpoint, so its
    # residuals are optimistic (see module docstring).
    held_out = (pred["split"] == SPLIT_WARM) | (pred["split"] == SPLIT_COLD)
    regime = np.where(pred["split"] == SPLIT_COLD, COLD, WARM).astype(object)

    calib_m = held_out & student_is_calib
    eval_m = held_out & ~student_is_calib
    print(f"Calibration: {calib_m.sum():,} events / {is_calib_student.sum():,} students")
    print(f"Evaluation:  {eval_m.sum():,} events / {(~is_calib_student).sum():,} students")

    item_cal = fit_item_level(pred["p"][calib_m], pred["y"][calib_m],
                              regime[calib_m], args.alpha, args.min_group_n)

    calib_chunks = build_checkpoints(pred, calib_m, args.checkpoint_k)
    cmp_, cre, csd, crg, _, _ = checkpoint_features(pred, calib_chunks, args.sigma_floor)
    ckpt_cal = fit_checkpoint_level(cmp_, cre, csd, crg, args.alpha, args.min_group_n,
                                    args.checkpoint_k, args.sigma_floor)
    print(f"Checkpoints: {len(calib_chunks):,} calibration blocks of k={args.checkpoint_k}")

    calibration = {
        "alpha": args.alpha,
        "model": {
            "checkpoint": pred["meta"]["checkpoint"],
            "variant": pred["meta"]["variant"],
            "seed": pred["meta"]["seed"],
        },
        "calibration_protocol": {
            "split_source": "test_warm + test_cold_item, partitioned BY STUDENT",
            "calib_frac": args.calib_frac,
            "seed": args.seed,
            "n_calib_students": int(is_calib_student.sum()),
            "n_eval_students": int((~is_calib_student).sum()),
            "val_excluded_because": "val selected the checkpoint (epoch 45, joint score); "
                                     "its residuals are optimistically biased.",
            "exchangeability_caveat": "Splits are chronological, so calibration and "
                                       "deployment are not exchangeable and the 1-alpha "
                                       "guarantee is approximate. See the empirical "
                                       "coverage report.",
        },
        "item_level": item_cal,
        "checkpoint_level": ckpt_cal,
    }

    # ---- empirical coverage on students never used for calibration -----
    report = {"alpha": args.alpha, "item_level": [], "checkpoint_level": [], "alpha_sweep": []}
    ep, ey, erg = pred["p"][eval_m], pred["y"][eval_m], regime[eval_m]
    report["item_level"].append(coverage_block(ep, ey, erg, item_cal, "eval_all"))
    for g in (WARM, COLD):
        m = erg == g
        report["item_level"].append(coverage_block(ep[m], ey[m], erg[m], item_cal, f"eval_{g}"))
    vm = (pred["split"] == SPLIT_VAL)
    report["item_level"].append(coverage_block(
        pred["p"][vm], pred["y"][vm], regime[vm], item_cal,
        "val_DIAGNOSTIC_ONLY_optimistic"))

    eval_chunks = build_checkpoints(pred, eval_m, args.checkpoint_k)
    emp, ere, esd, erg2, _, _ = checkpoint_features(pred, eval_chunks, args.sigma_floor)
    for g in (WARM, COLD, "all"):
        m = np.ones(len(emp), dtype=bool) if g == "all" else (erg2 == g)
        if not m.any():
            continue
        q = np.array([ckpt_cal["groups"][x]["q_hat"] for x in erg2[m]])
        lo = np.clip(emp[m] - q * esd[m], 0.0, 1.0)
        hi = np.clip(emp[m] + q * esd[m], 0.0, 1.0)
        report["checkpoint_level"].append({
            "label": f"eval_{g}",
            "n": int(m.sum()),
            "empirical_coverage": float(((ere[m] >= lo) & (ere[m] <= hi)).mean()),
            "mean_width": float((hi - lo).mean()),
            "median_width": float(np.median(hi - lo)),
            "width_p10": float(np.quantile(hi - lo, 0.10)),
            "width_p90": float(np.quantile(hi - lo, 0.90)),
        })

    if args.alpha_sweep:
        for a in (0.20, 0.10, 0.05, 0.02, 0.01):
            cal_a = fit_item_level(pred["p"][calib_m], pred["y"][calib_m],
                                   regime[calib_m], a, args.min_group_n)
            blk = coverage_block(ep, ey, erg, cal_a, f"alpha={a}")
            blk["alpha"] = a
            report["alpha_sweep"].append(blk)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(calibration, indent=2), encoding="utf-8")
    Path(args.coverage_out).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\nWrote {args.out}")
    print(f"Wrote {args.coverage_out}\n")
    print(f"{'block':<34} {'n':>10} {'cov':>7} {'cov|y=0':>8} {'cov|y=1':>8} {'|set|':>6}  status mix")
    for b in report["item_level"]:
        mix = b["status_mix"]
        print(f"{b['label']:<34} {b['n']:>10,} {b['empirical_coverage']:>7.4f} "
              f"{(b['coverage_y0'] or float('nan')):>8.4f} {(b['coverage_y1'] or float('nan')):>8.4f} "
              f"{b['mean_set_size']:>6.3f}  "
              f"safe={mix['MASTERY_SAFE']:.3f} struggle={mix['CONFIDENT_STRUGGLE']:.3f} "
              f"uncertain={mix['UNCERTAIN_BEHAVIOR']:.3f}")
    print()
    for b in report["checkpoint_level"]:
        print(f"{b['label']:<34} {b['n']:>10,} cov={b['empirical_coverage']:.4f} "
              f"width median={b['median_width']:.3f} (p10 {b['width_p10']:.3f} / "
              f"p90 {b['width_p90']:.3f})")


if __name__ == "__main__":
    main()
