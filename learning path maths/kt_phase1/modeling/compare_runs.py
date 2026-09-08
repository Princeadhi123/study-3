"""Summarize and compare the three trained KT variants side by side.

Reads runs/<variant>/training_history.json for each variant (written by
train.py) and prints/saves the best-epoch metrics for val / test_warm /
test_cold_item, so you can see at a glance whether adding item_id (B) or
question content (C) actually improved on the skill-only baseline (A),
and specifically whether C generalizes better to items never seen in training.
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).parent
VARIANTS = ["skill_only", "skill_item", "skill_item_content"]
METRICS = ["auc", "pr_auc", "log_loss", "brier", "accuracy"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runs-dir", default=str(HERE / "runs"))
    p.add_argument("--out", default=str(HERE / "runs" / "comparison.json"))
    return p.parse_args()


def best_epoch_by_val_auc(history):
    scored = [h for h in history if h["eval"].get("val", {}).get("auc") is not None]
    if not scored:
        return None
    return max(scored, key=lambda h: h["eval"]["val"]["auc"])


def main():
    args = parse_args()
    runs_dir = Path(args.runs_dir)
    summary = {}
    for variant in VARIANTS:
        history_path = runs_dir / variant / "training_history.json"
        if not history_path.exists():
            summary[variant] = {"status": "not_trained_yet"}
            continue
        history = json.loads(history_path.read_text(encoding="utf-8"))
        best = best_epoch_by_val_auc(history)
        if best is None:
            summary[variant] = {"status": "no_valid_epoch"}
            continue
        summary[variant] = {
            "status": "ok",
            "best_epoch": best["epoch"],
            "val": {m: best["eval"]["val"].get(m) for m in METRICS},
            "test_warm": {m: best["eval"]["test_warm"].get(m) for m in METRICS},
            "test_cold_item": {m: best["eval"]["test_cold_item"].get(m) for m in METRICS},
        }

    Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")

    header = f"{'variant':<22}{'val_auc':>10}{'test_warm_auc':>16}{'test_cold_auc':>16}"
    print(header)
    print("-" * len(header))
    for variant in VARIANTS:
        s = summary[variant]
        if s.get("status") != "ok":
            print(f"{variant:<22}{s['status']:>10}")
            continue
        print(f"{variant:<22}{s['val']['auc']:>10.4f}{s['test_warm']['auc']:>16.4f}{s['test_cold_item']['auc']:>16.4f}")
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
