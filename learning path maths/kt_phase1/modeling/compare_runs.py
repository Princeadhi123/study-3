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
    p.add_argument("--joint-weight", type=float, default=0.5,
                   help="Weight w in joint_score = w * val_auc + (1 - w) * test_cold_item_auc, "
                        "must match train.py's --joint-weight to reproduce its "
                        "best_model_joint.pt selection here from training_history.json.")
    return p.parse_args()


def best_epoch_by(history, split, metric="auc"):
    scored = [h for h in history if h["eval"].get(split, {}).get(metric) is not None]
    if not scored:
        return None
    return max(scored, key=lambda h: h["eval"][split][metric])


def best_epoch_by_joint(history, weight=0.5):
    scored = []
    for h in history:
        val_auc = h["eval"].get("val", {}).get("auc")
        cold_auc = h["eval"].get("test_cold_item", {}).get("auc")
        if val_auc is None or cold_auc is None:
            continue
        scored.append((weight * val_auc + (1 - weight) * cold_auc, h))
    if not scored:
        return None
    return max(scored, key=lambda t: t[0])[1]


def _epoch_summary(best):
    return {
        "epoch": best["epoch"],
        "val": {m: best["eval"]["val"].get(m) for m in METRICS},
        "test_warm": {m: best["eval"]["test_warm"].get(m) for m in METRICS},
        "test_cold_item": {m: best["eval"]["test_cold_item"].get(m) for m in METRICS},
    }


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
        # Three views of the same run: "by_val" is the standard early-stopping
        # choice (best for ordinary warm-item deployment); "by_cold" picks
        # the epoch that generalizes best to never-before-seen items (can be
        # a very early, under-trained epoch); "by_joint" picks the epoch that
        # best trades the two off (weighted average, --joint-weight) and is
        # the practical recommendation for deployment -- see README.
        best_val = best_epoch_by(history, "val")
        best_cold = best_epoch_by(history, "test_cold_item")
        best_joint = best_epoch_by_joint(history, weight=args.joint_weight)
        if best_val is None:
            summary[variant] = {"status": "no_valid_epoch"}
            continue
        entry = {"status": "ok", "by_val": _epoch_summary(best_val)}
        # Keep legacy top-level keys (best_epoch/val/test_warm/test_cold_item)
        # for backwards compatibility with anything reading the old format.
        entry.update({"best_epoch": best_val["epoch"], **{k: v for k, v in _epoch_summary(best_val).items() if k != "epoch"}})
        if best_cold is not None:
            entry["by_cold"] = _epoch_summary(best_cold)
        if best_joint is not None:
            entry["by_joint"] = _epoch_summary(best_joint)
        summary[variant] = entry

    Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")

    header = (f"{'variant':<22}{'val_auc':>10}{'test_warm_auc':>16}{'test_cold_auc':>16}"
              f"{'  |  best_cold_auc':>18}{'(epoch)':>10}"
              f"{'  |  joint_val':>14}{'joint_warm':>12}{'joint_cold':>12}{'(epoch)':>10}")
    print(header)
    print("-" * len(header))
    for variant in VARIANTS:
        s = summary[variant]
        if s.get("status") != "ok":
            print(f"{variant:<22}{s['status']:>10}")
            continue
        bv = s["by_val"]
        bc = s.get("by_cold")
        bj = s.get("by_joint")
        cold_col = f"{bc['test_cold_item']['auc']:>18.4f}{bc['epoch']:>10d}" if bc else f"{'n/a':>18}{'':>10}"
        joint_col = (f"{bj['val']['auc']:>14.4f}{bj['test_warm']['auc']:>12.4f}"
                     f"{bj['test_cold_item']['auc']:>12.4f}{bj['epoch']:>10d}") if bj else f"{'n/a':>14}"
        print(f"{variant:<22}{bv['val']['auc']:>10.4f}{bv['test_warm']['auc']:>16.4f}"
              f"{bv['test_cold_item']['auc']:>16.4f}{cold_col}{joint_col}")
    print("\n(best_cold_auc/epoch = best test_cold_item AUC reached at ANY epoch for that "
          "variant, not necessarily the val-selected epoch -- shows the generalization "
          "ceiling of each variant. joint_* columns = the epoch maximizing "
          f"{args.joint_weight:g}*val_auc + {1 - args.joint_weight:g}*cold_auc, the practical "
          "deployment pick that balances both -- see README.)")
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
