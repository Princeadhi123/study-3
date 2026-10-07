"""Plot frozen aggregate results without loading KT or rerunning evaluation."""
import argparse
import hashlib
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
VARIANTS = ("skill_only", "skill_item", "skill_item_content", "skill_item_content_option")
METRICS = ("auc", "log_loss", "brier")
COLORS = ("#216e69", "#be643d", "#4776a8", "#927335", "#6e6875", "#333d48")
QUALIFICATION = (
    "Qualified CPU replay; original CUDA parity failed. Cold: only 4 errors. "
    "Historical prediction, not mastery or learning validation."
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def capture_data(capture, output):
    """Freeze exact published values once; future renders use only this capture."""
    sources = {}

    def source(path):
        raw = path.read_bytes()
        sources[str(path.relative_to(PROJECT))] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    runs = PROJECT / "kt_phase1" / "modeling" / "runs"
    comparison = source(runs / "comparison.json")
    history = source(runs / VARIANTS[-1] / "training_history.json")
    config = source(runs / VARIANTS[-1] / "config.json")
    selected = comparison[VARIANTS[-1]]["by_joint"]
    epoch = next(row for row in history if row["epoch"] == selected["epoch"])
    for split in ("val", "test_warm", "test_cold_item"):
        for metric in METRICS:
            if epoch["eval"][split][metric] != selected[split][metric]:
                raise ValueError("Phase 1 comparison and saved history disagree")
    manifest = source(capture / "replay_manifest.json")
    checkpoint_hash = hashlib.sha256((runs / VARIANTS[-1] / "best_model_joint.pt").read_bytes()).hexdigest()
    if checkpoint_hash != manifest["inputs"]["checkpoint"]["sha256"]:
        raise ValueError("Phase 1 checkpoint differs from Phase 4 frozen input")
    result = source(capture / "research_results.json")
    if result["status"] != "historical_replay_complete_cpu_reference_qualified":
        raise ValueError("Unexpected Phase 4 result status; review labels before plotting")
    phase2 = PROJECT / "kt_phase2_inference" / "artifacts" / "conformal_rank_fix_20260929"
    coverage = source(phase2 / "k10_coverage.json")
    calibration = source(phase2 / "k10_calibration.json")
    if sources[str((phase2 / "k10_calibration.json").relative_to(PROJECT))] != manifest["inputs"]["calibration"]["sha256"]:
        raise ValueError("Conformal report is not paired with the captured calibration")
    if coverage["alpha"] != calibration["alpha"]:
        raise ValueError("Conformal alpha mismatch")
    data = {
        "schema": "saved_aggregate_plot_data_v1",
        "source_sha256": sources,
        "checkpoint_sha256": checkpoint_hash,
        "phase1": {
            "variant": config["variant"], "selected_epoch": selected["epoch"],
            "joint_weight": config["joint_weight"], "history": history,
            "comparison": {v: comparison[v]["by_joint"] for v in VARIANTS},
        },
        "phase4": result,
        "phase2_conformal": coverage,
    }
    save_json(output, data)
    return data


def style():
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10,
        "figure.facecolor": "#f6f3ed", "axes.facecolor": "#fffdf8",
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.titleweight": "bold", "grid.alpha": 0.18,
        "savefig.facecolor": "#f6f3ed",
    })


def finish(fig, output, name, title, note):
    fig.suptitle(title, fontsize=17, fontweight="bold", x=0.07, ha="left")
    fig.text(0.07, 0.035, note, fontsize=9, ha="left", va="bottom")
    fig.tight_layout(rect=(0.03, 0.12, 0.99, 0.9))
    for ext in ("png", "svg"):
        fig.savefig(output / f"{name}.{ext}", dpi=170)
    plt.close(fig)


def point_interval(ax, x, value, interval, color, label=None):
    if value is None:
        return
    if interval and interval["lower"] is not None and interval["upper"] is not None:
        ax.vlines(x, interval["lower"], interval["upper"], color=color, linewidth=2)
        ax.plot([x, x], [interval["lower"], interval["upper"]], "_", color=color)
    ax.plot(x, value, "o", color=color, label=label)


def phase1_plots(data, output):
    p1 = data["phase1"]
    history = p1["history"]
    fig, axes = plt.subplots(1, 3, figsize=(15, 5.5))
    for ax, metric in zip(axes, METRICS):
        for split, label, color in zip(
                ("val", "test_warm", "test_cold_item"),
                ("Validation", "Warm test", "Cold-item test"), COLORS):
            ax.plot([h["epoch"] for h in history],
                    [h["eval"][split][metric] for h in history], label=label, color=color)
        ax.axvline(p1["selected_epoch"], color="#555555", linestyle="--",
                   label=f"Frozen checkpoint: epoch {p1['selected_epoch']}")
        ax.set(xlabel="Epoch", ylabel=metric.replace("_", " ").upper())
        ax.grid()
    axes[0].legend(fontsize=8)
    finish(fig, output, "phase1_training_curves", "Phase 1 | Frozen model training history",
           "Saved skill_item_content_option run; no retraining. Cold-item AUC participated in checkpoint selection;\n"
           "these curves are not an untouched external validation. Higher AUC; lower log loss/Brier is better.")
    fig, axes = plt.subplots(1, 3, figsize=(15, 5.5))
    x = np.arange(len(VARIANTS))
    labels = ("Skill", "Skill + item", "+ content", "+ answer option")
    for ax, metric in zip(axes, METRICS):
        for offset, split, label, color in zip(
                (-0.17, 0.17), ("test_warm", "test_cold_item"), ("Warm", "Cold item"), COLORS):
            ax.plot(x + offset, [p1["comparison"][v][split][metric] for v in VARIANTS],
                    "o", color=color, label=label)
        ax.set(xticks=x, xticklabels=labels, ylabel=metric.replace("_", " ").upper())
        ax.tick_params(axis="x", labelrotation=18)
        ax.grid(axis="y")
    axes[0].legend()
    finish(fig, output, "phase1_variant_comparison", "Phase 1 | Saved variant comparison",
           "Each variant uses its saved by_joint checkpoint from runs/comparison.json; no new ranking or HPO.\n"
           "Cold-item results informed selection; points have no saved uncertainty intervals.")


def phase4_plots(data, output):
    result = data["phase4"]
    groups = [result["groups"][f"{regime}/all"] for regime in ("warm", "cold")]
    models = ("baseline_global", "baseline_skill", "baseline_rendering",
              "baseline_history", "p_no_history", "p_history")
    labels = ("Global", "Skill", "Rendering", "History baseline", "KT no history", "KT history")
    fig, axes = plt.subplots(1, 3, figsize=(15, 6))
    for ax, metric in zip(axes, METRICS):
        for index, (model, label, color) in enumerate(zip(models, labels, COLORS)):
            for regime_index, group in enumerate(groups):
                m = group["models"][model]
                point_interval(ax, regime_index + (index - 2.5) * 0.09,
                               m[metric], m["intervals"][metric], color,
                               label if regime_index == 0 else None)
        ax.set(xticks=(0, 1), xticklabels=("Warm: 860 events", "Cold: 152 events"),
               ylabel=metric.replace("_", " ").upper(), xlim=(-0.45, 1.45))
        ax.grid(axis="y")
    axes[0].legend(fontsize=8)
    finish(fig, output, "phase4_model_metrics", "Phase 4 | KT and baseline prediction metrics",
           QUALIFICATION + "\nWhiskers: saved 95% student-cluster bootstrap intervals; higher AUC, lower losses is better.")
    fig, axes = plt.subplots(1, 3, figsize=(15, 5.5))
    for ax, metric in zip(axes, METRICS):
        for i, (regime, group, color) in enumerate(zip(("Warm", "Cold"), groups, COLORS)):
            d = group["paired_differences"]["p_history_minus_p_no_history"][metric]
            point_interval(ax, i, d["estimate"], d["interval"], color)
        ax.axhline(0, linestyle="--", color="#666666")
        ax.set(xticks=(0, 1), xticklabels=("Warm", "Cold"),
               ylabel=f"History minus no history: {metric}", xlim=(-0.5, 1.5))
        ax.grid(axis="y")
    finish(fig, output, "phase4_history_effect", "Phase 4 | Paired effect of student history",
           QUALIFICATION + "\nSaved paired 95% student-cluster intervals. Positive AUC and negative loss/Brier favor history.")
    fig, axes = plt.subplots(1, 2, figsize=(12, 6))
    for ax, regime, group in zip(axes, ("Warm", "Cold"), groups):
        ax.plot((0, 1), (0, 1), "--", color="#777777", label="Ideal reference")
        for model, label, color in zip(("p_no_history", "p_history"),
                                       ("KT no history", "KT history"), COLORS):
            bins = [b for b in group["models"][model]["calibration"]["bins"] if b["n"]]
            ax.scatter([b["mean_probability"] for b in bins],
                       [b["observed_rate"] for b in bins], color=color,
                       s=[25 + 130 * b["n"] / group["events"] for b in bins], label=label)
            for b in bins:
                ax.annotate(str(b["n"]), (b["mean_probability"], b["observed_rate"]),
                            xytext=(4, -10 if model == "p_no_history" else 6),
                            textcoords="offset points", fontsize=7, color=color)
        ax.set(title=regime, xlabel="Mean predicted probability",
               ylabel="Observed correct fraction", xlim=(0, 1.03), ylim=(0, 1.05))
        ax.grid()
        ax.legend(fontsize=8)
    finish(fig, output, "phase4_reliability", "Phase 4 | KT probability reliability",
           QUALIFICATION + "\nSaved equal-width bins; numbers = events per bin. Empty bins omitted; no bin uncertainty supplied.")


def conformal_plots(data, output):
    phase2 = data["phase2_conformal"]
    phase4 = data["phase4"]["conformal"]
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    for ax, regime in zip(axes, ("warm", "cold")):
        original = next(r for r in phase2["item_level"] if r["label"] == f"eval_{regime}")
        transferred = phase4["items"][regime]
        original_values = (
            (original["empirical_coverage"], original["n"]),
            (original["coverage_y0"], original["n_y0"]),
            (original["coverage_y1"], original["n_y1"]),
        )
        subset_values = (transferred, transferred["by_label"]["0"], transferred["by_label"]["1"])
        for i, ((value, n), subset) in enumerate(zip(original_values, subset_values)):
            point_interval(ax, i - 0.12, value, None, COLORS[0],
                           "Phase 2 original evaluation" if i == 0 else None)
            point_interval(ax, i + 0.12, subset["empirical_coverage"],
                           subset["coverage_interval"], COLORS[1],
                           "Phase 4 selected-bank transfer" if i == 0 else None)
            ax.annotate(f"n={n:,}", (i - 0.12, value), xytext=(-22, 7),
                        textcoords="offset points", fontsize=7, color=COLORS[0])
            ax.annotate(f"n={subset['events']:,}", (i + 0.12, subset["empirical_coverage"]),
                        xytext=(3, -13), textcoords="offset points", fontsize=7, color=COLORS[1])
        ax.axhline(1 - phase2["alpha"], color="#666666", linestyle="--",
                   label=f"Nominal target: {1 - phase2['alpha']:.0%}")
        ax.set(title=regime.capitalize(), xticks=(0, 1, 2),
               xticklabels=("Overall", "Incorrect label", "Correct label"),
               ylabel="Empirical true-label coverage", ylim=(-0.04, 1.12), xlim=(-0.5, 2.5))
        ax.grid(axis="y")
    axes[0].legend(fontsize=8, loc="lower left")
    finish(fig, output, "conformal_item_coverage", "Conformal | Original evaluation vs selected-bank transfer",
           "Conformal was added in Phase 2, not Phase 1. Different populations; not an independent paired comparison.\n"
           "Phase 4: history only, qualified CPU, no new guarantee. Bootstrap 100%-100% is not population certainty.")
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.5))
    rows = [next(r for r in phase2["checkpoint_level"] if r["label"] == f"eval_{regime}")
            for regime in ("warm", "cold")]
    for ax, metric, title in zip(axes, ("empirical_coverage", "mean_width"),
                                 ("Empirical score coverage", "Mean interval width (rate units)")):
        ax.bar((0, 1), [r[metric] for r in rows], color=COLORS[:2], width=0.55)
        ax.set(xticks=(0, 1), xticklabels=("Original warm", "Original cold"), ylabel=title)
        ax.grid(axis="y")
        for i, row in enumerate(rows):
            ax.annotate(f"{row[metric]:.4f}\nn={row['n']:,} blocks", (i, row[metric]),
                        xytext=(0, 8), textcoords="offset points", ha="center", fontsize=9)
        ax.set_ylim(0, 1.08 if metric == "empirical_coverage" else 0.32)
    axes[0].axhline(1 - phase2["alpha"], linestyle="--", color="#666666")
    finish(fig, output, "conformal_score_support", "Conformal | Original ten-question score results",
           "Phase 2 historical k=10 grouping, not the designed fixed assessment; no saved uncertainty shown.\n"
           "Phase 4 selected-bank score coverage: unavailable (0 genuine blocks). No synthetic blocks substituted.")


FIGURES = (
    ("phase1_training_curves", "Phase 1 training curves"),
    ("phase1_variant_comparison", "Phase 1 saved variant comparison"),
    ("phase4_model_metrics", "Phase 4 KT and baselines"),
    ("phase4_history_effect", "Phase 4 paired history effect"),
    ("phase4_reliability", "Phase 4 probability reliability"),
    ("conformal_item_coverage", "Conformal item coverage"),
    ("conformal_score_support", "Conformal score support"),
)


def gallery(output):
    sections = "\n".join(
        f'<section><h2>{title}</h2><a href="{name}.svg">Vector SVG</a> | '
        f'<a href="{name}.png">PNG</a><img src="{name}.png" alt="{title}"></section>'
        for name, title in FIGURES
    )
    html = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KT and Conformal Research Plots</title>
<style>
body{background:#f6f3ed;color:#233c3a;font-family:Georgia,serif;margin:0}
main{max-width:1300px;margin:auto;padding:32px 20px}
h1{font-size:clamp(28px,4vw,46px)}p{max-width:1000px;line-height:1.6}
section{margin:36px 0;border-top:1px solid #ccc3b4;padding-top:12px}
img{display:block;width:100%;height:auto;margin-top:12px}a{color:#216e69}
</style><main><h1>KT &amp; Conformal Research Plots</h1>
<p>Saved aggregate results only: no retraining, inference, or new metric fitting.
Phase 1 describes the frozen training run. Phase 4 evaluates selected-bank
historical responses, not completed fixed assessments or learning outcomes.</p>
<p>Phase 4 is qualified CPU evidence: original CUDA parity failed.
Cold has only four incorrect responses. Conformal was added in Phase 2;
Phase 4 transfers historical thresholds, with no new coverage guarantee.
No selected-bank ten-question score coverage was available.</p>
<p>Whiskers, where shown, are the saved 95% student-cluster bootstrap intervals.
They do not account for all selection or transfer uncertainty. Frozen plotted
inputs and source hashes: <a href="plot_data.json">plot_data.json</a>.</p>
""" + sections + "</main></html>\n"
    (output / "index.html").write_text(html, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, default=Path(__file__).resolve().parent /
                        "artifacts" / "selected_bank_v3_20261005")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = args.output_dir or args.capture_dir / "plots"
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / "plot_data.json"
    data = read_json(snapshot) if snapshot.exists() else capture_data(args.capture_dir, snapshot)
    if data["schema"] != "saved_aggregate_plot_data_v1":
        raise ValueError("Unknown plot snapshot schema")
    style()
    phase1_plots(data, output)
    phase4_plots(data, output)
    conformal_plots(data, output)
    gallery(output)
    print(f"Rendered {len(FIGURES)} plot pairs and gallery: {output / 'index.html'}")


if __name__ == "__main__":
    main()
