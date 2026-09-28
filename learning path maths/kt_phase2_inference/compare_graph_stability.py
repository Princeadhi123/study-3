"""Compare derived prerequisite edges and graph recommendations across independent probe samples."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import paths
from kg_query import KnowledgeGraph


def edges(graph):
    return {(source, target) for target, incoming in graph.in_edges.get("prerequisite", {}).items()
            for source, _weight, _detail in incoming}


def top(graph, target):
    candidates = graph.foundational_for(target)
    return candidates[0]["skill_id"] if candidates else None


def compare(baseline, candidates):
    graphs = [baseline, *candidates]
    edge_sets = [edges(graph) for graph in graphs]
    baseline_edges = edge_sets[0]
    targets = list(baseline.nodes)
    top_by_run = [{target: top(graph, target) for target in targets} for graph in graphs]
    baseline_targets = [target for target in targets if top_by_run[0][target] is not None]
    stable_top = [target for target in baseline_targets
                  if all(recommendations[target] == top_by_run[0][target]
                         for recommendations in top_by_run[1:])]
    common_top = [target for target in baseline_targets
                  if all(recommendations[target] is not None for recommendations in top_by_run[1:])]
    joint_edges = set.intersection(*edge_sets)
    comparison = []
    for graph, current, recommendation in zip(candidates, edge_sets[1:], top_by_run[1:]):
        union = baseline_edges | current
        same_top = sum(recommendation[target] == top_by_run[0][target] for target in baseline_targets)
        comparison.append({
            "n_edges": len(current),
            "n_targets_with_recommendation": sum(recommendation[target] is not None for target in targets),
            "edge_jaccard_with_baseline": len(baseline_edges & current) / len(union) if union else None,
            "baseline_edges_retained": len(baseline_edges & current),
            "baseline_targets_same_top": same_top,
            "baseline_targets_same_top_rate": same_top / len(baseline_targets) if baseline_targets else None,
        })
    return {
        "baseline": {"n_edges": len(baseline_edges),
                     "n_targets_with_recommendation": len(baseline_targets)},
        "comparisons": comparison,
        "all_runs": {
            "n_edges_shared": len(joint_edges),
            "baseline_edges_shared_by_all": len(joint_edges & baseline_edges),
            "baseline_targets_same_top_in_all": len(stable_top),
            "baseline_targets_with_top_in_all": len(common_top),
            "baseline_targets_same_top_rate": len(stable_top) / len(baseline_targets) if baseline_targets else None,
            "same_top_when_all_have_a_top_rate": len(stable_top) / len(common_top) if common_top else None,
            "recommendations_available_by_target": {
                str(n): sum(sum(recommendations[target] is not None for recommendations in top_by_run) == n
                            for target in targets)
                for n in range(len(graphs) + 1)
            },
        },
        "limitations": ["Edges express frozen-model sensitivity, not causal educational prerequisites.",
                        "Probe windows from different seeds can overlap; agreement is not independent validation.",
                        "Comparisons use the same thresholds and frozen model as the baseline graph."],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependency", nargs="+", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=paths.ARTIFACTS / "stability" / "graph_stability_report.json")
    args = parser.parse_args()
    baseline = KnowledgeGraph.load()
    candidates = []
    for dependency in args.dependency:
        paths.require(dependency)
        output_dir = args.out.parent / dependency.stem
        subprocess.run([sys.executable, str(paths.PHASE2_ROOT / "build_knowledge_graph.py"),
                        "--dependency", str(dependency.resolve()), "--out-dir", str(output_dir)], check=True)
        candidates.append(KnowledgeGraph.load(output_dir / "kg_nodes.csv", output_dir / "kg_edges.csv"))
    result = compare(baseline, candidates)
    result["samples"] = [str(dependency) for dependency in args.dependency]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["all_runs"], indent=2))
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
