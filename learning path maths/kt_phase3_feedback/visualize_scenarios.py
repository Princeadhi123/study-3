"""Render private researcher-only figures from a deterministic scenario report.

Reads ``scenario_report.json`` (schema ``phase3_scenario_report_v1``) produced
by ``scenario_report.py`` and writes one PNG per scenario + seed + skill, plus
optional per-skill seed-aggregation figures. Outputs are private diagnostics;
nothing here is student-facing and no calibration claim is made.
"""
import argparse
import json
import re
import textwrap
from pathlib import Path

REPORT_SCHEMA = "phase3_scenario_report_v1"
MIDPOINT_POSITION = 20
CHECKPOINT_LABEL = {
    "midpoint": "midpoint",
    "end": "end",
}
CALIBRATION_LABEL = {
    "approximate_k5_not_calibrated_k10": "k=5 approx, uncalibrated",
    "k10_protocol_not_validated_for_synthetic_fixed_bank":
        "k=10 exploratory, fixed-bank coverage unverified",
}


def _pyplot():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise SystemExit(
            "matplotlib is required for plotting but is not installed; "
            "install it in your environment and re-run "
            f"({exc.__class__.__name__}: {exc})")
    return plt


def _sanitize(part) -> str:
    text = str(part).strip() or "unnamed"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:120]


def _check_collisions(paths: list[Path]) -> None:
    seen = {}
    for path in paths:
        key = path.resolve()
        if key in seen:
            raise ValueError(
                f"filename collision: {path.name} would be produced by "
                "two different outputs; sanitize inputs or rename scenarios")
        seen[key] = path
    for path in paths:
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite {path}")


def _midpoint_x(rows: list[dict]):
    """Attempt-axis x where global position 20 falls for this skill."""
    attempts = [r["skill_attempt"] for r in rows]
    positions = [r["position"] for r in rows]
    before = [a for a, p in zip(attempts, positions) if p <= MIDPOINT_POSITION]
    after = [a for a, p in zip(attempts, positions) if p > MIDPOINT_POSITION]
    if before and after:
        return (before[-1] + after[0]) / 2
    return None


def _humanize(slug: str) -> str:
    return CALIBRATION_LABEL.get(slug, str(slug).replace("_", " "))


def _conformal_for(summary: list[dict], scenario: str, seed, skill: str) -> list:
    notes = []
    for row in summary:
        if (row["scenario"] == scenario and row["seed"] == seed
                and row["skill_id"] == skill
                and row.get("conformal_status")):
            label = CHECKPOINT_LABEL.get(row["checkpoint"], row["checkpoint"])
            bounds = ""
            if (row.get("conformal_lower") is not None
                    and row.get("conformal_upper") is not None):
                bounds = (f" [{row['conformal_lower']:.3f},"
                          f" {row['conformal_upper']:.3f}]")
            calib = row.get("conformal_calibration_status")
            regime = f" ({_humanize(calib)})" if calib else ""
            note = (f"{label}{regime}: "
                    f"{_humanize(row['conformal_status'])}{bounds}")
            notes.append((row["checkpoint"], note))
    order = {"midpoint": 0, "end": 1}
    return [note for _, note in sorted(notes, key=lambda n: order.get(n[0], 2))]


def _caption_lines(notes: list[str]) -> list[str]:
    """Wrap the estimand caveat plus every checkpoint note; never truncate."""
    lines = ["simulated P vs KT pre-answer P: distinct estimands; "
             "no calibration claim"]
    lines.extend(notes)
    wrapped = []
    for line in lines:
        wrapped.extend(textwrap.wrap(line, 96) or [line])
    return wrapped


def _fig_skill(rows: list[dict], summary: list[dict], out_path: Path,
               plt) -> None:
    rows = sorted(rows, key=lambda r: r["position"])
    first = rows[0]
    scenario, seed, skill = first["scenario"], first["seed"], first["skill_id"]
    attempts = [r["skill_attempt"] for r in rows]
    true_p = [r["true_probability"] for r in rows]
    observed = [r["observed_accuracy_to_date"] for r in rows]
    kt_p = [r["kt_probability"] for r in rows]
    has_kt = any(p is not None for p in kt_p)
    correct = [bool(r["correct"]) for r in rows]

    lines = _caption_lines(_conformal_for(summary, scenario, seed, skill))
    caption = "\n".join(lines)
    height = 5.5 + 0.22 * max(0, len(lines) - 3)
    fig, ax = plt.subplots(figsize=(8, height))
    ax.plot(attempts, true_p, "-o", color="tab:blue", markersize=3,
            label="simulated response P (true, synthetic)")
    if has_kt:
        ax.plot(attempts, [p if p is not None else float("nan") for p in kt_p],
                "-s", color="tab:orange", markersize=3,
                label="KT pre-answer P (diagnostic, not calibrated)")
    ax.plot(attempts, observed, "-", color="tab:gray", linewidth=1,
            label="observed running accuracy (per skill)")
    ax.scatter([a for a, c in zip(attempts, correct) if c],
               [o for o, c in zip(observed, correct) if c],
               marker="o", facecolors="none", edgecolors="tab:green", s=60,
               label="correct", zorder=3)
    ax.scatter([a for a, c in zip(attempts, correct) if not c],
               [o for o, c in zip(observed, correct) if not c],
               marker="x", color="tab:red", s=60,
               label="incorrect", zorder=3)
    mid_x = _midpoint_x(rows)
    if mid_x is not None:
        ax.axvline(mid_x, color="black", linestyle="--", linewidth=1,
                   label=f"midpoint (global position {MIDPOINT_POSITION})")
    ax.set_ylim(-0.05, 1.05)
    ax.set_xlabel("skill attempt (within 40-question fixed order)")
    ax.set_ylabel("probability / accuracy")
    ax.set_title(f"{scenario} — seed {seed} — skill {skill}")
    ax.legend(fontsize="small", loc="best")
    fig.text(0.5, 0.02, caption, ha="center", va="bottom", fontsize="small")
    bottom = min(0.06 * len(lines) + 0.12, 0.45)
    fig.tight_layout(rect=(0, bottom, 1, 1))
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def _band(vals: list[float]):
    """Return (median, 25th, 75th percentile) for values across seeds."""
    import statistics
    srt = sorted(vals)
    median = statistics.median(srt)
    if len(srt) == 1:
        return median, srt[0], srt[0]
    q = statistics.quantiles(srt, n=4, method="inclusive")
    return median, q[0], q[2]


def _fig_aggregate(skill: str, per_attempt: dict[int, dict[str, list]],
                   out_path: Path, plt) -> None:
    attempts = sorted(per_attempt)
    med_true, q1_true, q3_true = zip(
        *(_band(per_attempt[a]["true"]) for a in attempts))
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(attempts, med_true, "-o", color="tab:blue", markersize=3,
            label="median simulated response P across seeds")
    ax.fill_between(attempts, q1_true, q3_true, color="tab:blue", alpha=0.15,
                    label="25–75 percentile (simulated P)")
    if any(per_attempt[a]["kt"] for a in attempts):
        xs, med_kt, q1_kt, q3_kt = [], [], [], []
        for a in attempts:
            vals = per_attempt[a]["kt"]
            if not vals:
                continue
            med, lo, hi = _band(vals)
            xs.append(a)
            med_kt.append(med)
            q1_kt.append(lo)
            q3_kt.append(hi)
        ax.plot(xs, med_kt, "-s", color="tab:orange", markersize=3,
                label="median KT pre-answer P across seeds")
        ax.fill_between(xs, q1_kt, q3_kt, color="tab:orange", alpha=0.15,
                        label="25–75 percentile (KT P)")
    ax.set_ylim(-0.05, 1.05)
    ax.set_xlabel("skill attempt (within 40-question fixed order)")
    ax.set_ylabel("probability")
    ax.set_title(f"seed aggregation — skill {skill}")
    ax.legend(fontsize="small", loc="best")
    fig.text(0.5, 0.01,
             "Median simulated P vs median KT pre-answer P across seeds; "
             "distinct estimands, no calibration claim.",
             ha="center", fontsize="small")
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def render(report: dict, out_dir: Path, aggregate: bool = False) -> list[Path]:
    if report.get("schema") != REPORT_SCHEMA:
        raise ValueError(f"expected schema {REPORT_SCHEMA}")
    trace = report["trace"]
    summary = report["summary"]
    groups = {}
    for row in trace:
        groups.setdefault((row["scenario"], row["seed"], row["skill_id"]),
                          []).append(row)
    planned = []
    jobs = []
    for (scenario, seed, skill), rows in sorted(groups.items()):
        path = out_dir / ("trace_{}_{}_{}.png".format(
            _sanitize(scenario), _sanitize(seed), _sanitize(skill)))
        jobs.append(((scenario, seed, skill), path))
        planned.append(path)
    agg_jobs = []
    if aggregate:
        by_scenario = {}
        for (scenario, seed, skill), rows in groups.items():
            by_scenario.setdefault((scenario, skill), {})[seed] = rows
        for (scenario, skill), seeds in sorted(by_scenario.items()):
            if len(seeds) < 2:
                continue
            path = out_dir / "aggregate_{}_{}.png".format(
                _sanitize(scenario), _sanitize(skill))
            agg_jobs.append(((scenario, skill), seeds, path))
            planned.append(path)
    _check_collisions(planned)
    out_dir.mkdir(parents=True, exist_ok=True)
    plt = _pyplot()
    written = []
    for key, path in jobs:
        _fig_skill(groups[key], summary, path, plt)
        written.append(path)
    for (scenario, skill), seeds, path in agg_jobs:
        per_attempt = {}
        for rows in seeds.values():
            for r in rows:
                slot = per_attempt.setdefault(r["skill_attempt"],
                                              {"true": [], "kt": []})
                slot["true"].append(r["true_probability"])
                if r["kt_probability"] is not None:
                    slot["kt"].append(r["kt_probability"])
        _fig_aggregate(skill, per_attempt, path, plt)
        written.append(path)
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path,
                        help="scenario_report.json (phase3_scenario_report_v1)")
    parser.add_argument("--out-dir", type=Path, required=True,
                        help="private output directory for PNG figures")
    parser.add_argument("--aggregate", action="store_true",
                        help="add per-skill median/IQR figures when a "
                             "scenario has multiple seeds")
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    written = render(report, args.out_dir, aggregate=args.aggregate)
    print(f"Wrote {len(written)} private figure(s) to {args.out_dir}")


if __name__ == "__main__":
    main()
