"""Rank the configs trained by tune_lumi.sh (a grid over --item-id-dropout x
--item-embed-weight-decay for one variant) and pick a winner.

Unlike compare_runs.py (which compares the four *variants* A/B/C/D against
each other, one run each), this scans a directory of *tuned configs of the
same variant* -- runs_tune/<variant>/id<x>_wd<y>/training_history.json for
every combination tune_lumi.sh trained -- and ranks them the same three ways
(by_val, by_cold, by_joint) train.py/compare_runs.py already use, so the
winner is chosen with the exact same criteria as everything else in this
pipeline (see README "Reading the results").

Usage (after tune_lumi.sh's array job finishes and you've rsynced
runs_tune/ back, same as runs/ in LUMI_SETUP.md):
    python compare_tuning.py --runs-dir runs_tune/skill_item_content_option
"""
import argparse
import json
from pathlib import Path

from compare_runs import best_epoch_by, best_epoch_by_joint, _epoch_summary, METRICS

HERE = Path(__file__).parent


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runs-dir", required=True,
                   help="Directory containing one subdirectory per tuned config "
                        "(e.g. id0.3_wd0.01/), each with training_history.json, "
                        "as written by tune_lumi.sh.")
    p.add_argument("--out", default=None,
                   help="Where to write the ranked comparison JSON "
                        "(default: <runs-dir>/tuning_comparison.json).")
    p.add_argument("--joint-weight", type=float, default=0.5,
                   help="Must match --joint-weight tune_lumi.sh (KT_JOINT_WEIGHT) was run with.")
    p.add_argument("--rank-by", default="by_joint", choices=["by_val", "by_cold", "by_joint"],
                   help="Which view's test_cold_item/val AUC to sort configs by. by_joint "
                        "(default) matches the deployment recommendation in README.")
    return p.parse_args()


def main():
    args = parse_args()
    runs_dir = Path(args.runs_dir)
    out_path = Path(args.out) if args.out else runs_dir / "tuning_comparison.json"

    configs = sorted(p.parent.name for p in runs_dir.glob("*/training_history.json"))
    if not configs:
        raise SystemExit(f"No training_history.json found under {runs_dir}/*/ "
                          f"-- has the tune_lumi.sh array job finished and been synced here?")

    summary = {}
    for name in configs:
        history = json.loads((runs_dir / name / "training_history.json").read_text(encoding="utf-8"))
        best_val = best_epoch_by(history, "val")
        best_cold = best_epoch_by(history, "test_cold_item")
        best_joint = best_epoch_by_joint(history, weight=args.joint_weight)
        if best_val is None:
            summary[name] = {"status": "no_valid_epoch"}
            continue
        entry = {"status": "ok", "by_val": _epoch_summary(best_val)}
        if best_cold is not None:
            entry["by_cold"] = _epoch_summary(best_cold)
        if best_joint is not None:
            entry["by_joint"] = _epoch_summary(best_joint)
        summary[name] = entry

    ranked = [n for n in configs if summary[n].get("status") == "ok"]
    ranked.sort(key=lambda n: summary[n][args.rank_by]["test_cold_item"]["auc"], reverse=True)

    out = {"rank_by": args.rank_by, "joint_weight": args.joint_weight,
           "ranking": ranked, "configs": summary}
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    header = (f"{'config':<22}{'val_auc':>10}{'warm_auc':>10}{'cold_auc':>10}"
              f"{'(epoch)':>10}   [{args.rank_by}]")
    print(header)
    print("-" * len(header))
    for name in ranked:
        e = summary[name][args.rank_by]
        print(f"{name:<22}{e['val']['auc']:>10.4f}{e['test_warm']['auc']:>10.4f}"
              f"{e['test_cold_item']['auc']:>10.4f}{e['epoch']:>10d}")
    for name in configs:
        if summary[name].get("status") != "ok":
            print(f"{name:<22}{summary[name]['status']:>10}")

    if ranked:
        winner = ranked[0]
        print(f"\nWinner ({args.rank_by}, by test_cold_item AUC): {winner}")
        print(f"Checkpoint: {runs_dir / winner / 'best_model_joint.pt'} "
              f"(or best_model.pt / best_model_cold.pt for the other two views)")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
