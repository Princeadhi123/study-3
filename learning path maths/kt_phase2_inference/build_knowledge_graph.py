"""L4b -- the skill knowledge graph: separated evidence layers, one derived conclusion.

Nodes: the **560 skills that have an embedding row** in the model's
`skill_vocab` (see `build_skill_catalog.py` for why this is 560 and not the
293 in `reports_v2/item_context.csv`). The 561st observed skill has no
labeled interactions, hence no embedding row, and is excluded rather than
carried as a dead node.


## Why layered, and not a single "prerequisite" edge set

A graph whose edges are "skills that correlate" would not survive scrutiny:
in a curriculum, *everything* correlates with everything, because the
curriculum itself orders the observations. Co-occurrence, temporal
precedence and predictive dependence are three different claims with three
different failure modes, and collapsing them into one opaque weight makes
the result unfalsifiable.

So each edge carries an explicit `relation` and its own provenance:

| relation | source | what it claims | what it does NOT claim |
|---|---|---|---|
| `curriculum_order` | observed first-encounter times per student | students met A before B | nothing causal -- this is the teaching order, which may be arbitrary |
| `co_occurrence` | skills sharing items | A and B are exercised together | no direction at all |
| `content_similarity` | cosine of mean frozen content embeddings | A and B are about similar material | similarity is not dependence |
| `predictive_dependency` | counterfactual probes of the frozen model (`probe_predictive_dependency.py`) | the model's belief about B moves when A's outcome is changed | dependence *inside the model*, which inherits the model's biases |
| `prerequisite` | **derived** from the three above | A is plausibly foundational for B | still not a randomised causal claim |

`prerequisite` is the only layer the live gate queries. Keeping the
evidence beneath it separate and inspectable is what makes the graph
refinable by a domain expert instead of a black box -- an edge can be
rejected by pointing at which evidence it rests on.


## The derived prerequisite rule

A -> B is emitted as `prerequisite` when all three hold:

1. **Temporal precedence** -- students reliably meet A before B
   (`precedence >= --min-precedence`, on `>= --min-support` students).
2. **Model dependence** -- changing A's outcome moves the model's
   prediction on B by at least `--min-dependency` (requires the probe;
   without it the graph is emitted with evidence layers only and *no*
   `prerequisite` edges, rather than pretending temporal order alone is
   enough).
3. **Asymmetry** -- the A->B dependency exceeds the B->A dependency by
   `--min-asymmetry`. A genuine prerequisite is directional; two facets of
   one topic are not, and this is what separates them.

Requirement 2 is what makes the graph *model-grounded* rather than a
restatement of the timetable, and requirement 3 is what stops the
curriculum's own ordering from manufacturing direction out of nothing.

    python build_knowledge_graph.py                       # evidence layers only
    python build_knowledge_graph.py --dependency artifacts/predictive_dependency.csv
"""
import argparse
import csv
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

import paths

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--catalog", default=str(paths.SKILL_CATALOG))
    p.add_argument("--skill-item-map", default=str(paths.SKILL_ITEM_MAP))
    p.add_argument("--student-skill", default=str(paths.STUDENT_SKILL))
    p.add_argument("--dependency", default=str(paths.ARTIFACTS / "predictive_dependency.csv"),
                   help="Output of probe_predictive_dependency.py. Optional; without it "
                        "no `prerequisite` edges are derived.")
    p.add_argument("--content-similarity",
                   default=str(paths.ARTIFACTS / "skill_content_similarity.csv"),
                   help="Optional cosine-similarity edge list (needs the embedding table).")
    p.add_argument("--out-dir", default=str(paths.ARTIFACTS))

    p.add_argument("--min-support", type=int, default=100,
                   help="Minimum students who saw BOTH skills, for a curriculum_order edge.")
    p.add_argument("--min-precedence", type=float, default=0.65,
                   help="Fraction of those students who met A before B.")
    p.add_argument("--min-cooccurrence", type=int, default=3,
                   help="Minimum shared items for a co_occurrence edge.")
    p.add_argument("--min-dependency", type=float, default=0.01,
                   help="Minimum counterfactual shift in P(correct) on B, in probability "
                        "units, for A to count as influencing B.")
    p.add_argument("--min-asymmetry", type=float, default=0.005,
                   help="Required excess of dependency(A->B) over dependency(B->A).")
    return p.parse_args()


def load_nodes(catalog_path: Path):
    """Skills with an embedding row, in `skill_vocab` index order."""
    rows = []
    with open(catalog_path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["vocab_idx"] == "":
                continue  # no embedding row -- see build_skill_catalog.py
            rows.append(r)
    rows.sort(key=lambda r: int(r["vocab_idx"]))
    index = {r["skill_id"]: i for i, r in enumerate(rows)}
    return rows, index


def curriculum_order_counts(student_skill_path: Path, index: dict):
    """before[a, b] = number of students whose first A predates their first B.

    Computed per student over the skills that student actually encountered,
    so a pair's support is "students who saw both", never "all students".
    Ties (identical first timestamps, e.g. the same round) are counted for
    neither direction -- they carry no ordering information.
    """
    n = len(index)
    before = np.zeros((n, n), dtype=np.int32)
    both = np.zeros((n, n), dtype=np.int32)

    def flush(pairs):
        if len(pairs) < 2:
            return
        pairs.sort()  # by first_ts
        ts = [t for t, _ in pairs]
        ids = np.array([i for _, i in pairs])
        m = len(ids)
        ii, jj = np.triu_indices(m, k=1)
        a, b = ids[ii], ids[jj]
        both[a, b] += 1
        both[b, a] += 1
        # ts is sorted, so i < j implies ts[i] <= ts[j]; only strict
        # inequalities count as evidence of ordering.
        strict = np.array([ts[i] < ts[j] for i, j in zip(ii, jj)])
        np.add.at(before, (a[strict], b[strict]), 1)

    cur_student, pairs, n_students = None, [], 0
    with gzip.open(student_skill_path, "rt", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["student_id"] != cur_student:
                flush(pairs)
                n_students += 1
                cur_student, pairs = row["student_id"], []
            i = index.get(row["skill_id"])
            if i is not None:
                pairs.append((row["first_ts"], i))
        flush(pairs)
    return before, both, n_students


def cooccurrence_counts(skill_item_path: Path, index: dict):
    """shared[a, b] = number of items exercising both skills (symmetric)."""
    n = len(index)
    shared = np.zeros((n, n), dtype=np.int32)
    items_per_skill = np.zeros(n, dtype=np.int32)

    by_item = defaultdict(list)
    with open(skill_item_path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            i = index.get(r["skill_id"])
            if i is not None:
                by_item[r["item_id"]].append(i)
                items_per_skill[i] += 1

    for skills in by_item.values():
        if len(skills) < 2:
            continue
        s = np.array(sorted(set(skills)))
        ii, jj = np.triu_indices(len(s), k=1)
        np.add.at(shared, (s[ii], s[jj]), 1)
        np.add.at(shared, (s[jj], s[ii]), 1)
    return shared, items_per_skill, len(by_item)


def load_dependency(path: Path, index: dict):
    """dep[a, b] = mean counterfactual shift in P(correct on B) from A's outcome."""
    if not path.exists():
        return None, {}
    n = len(index)
    dep = np.zeros((n, n), dtype=np.float32)
    meta = {}
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            a, b = index.get(r["source_skill_id"]), index.get(r["target_skill_id"])
            if a is None or b is None:
                continue
            dep[a, b] = float(r["delta_p"])
            meta[(a, b)] = r
    return dep, meta


def main():
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    nodes, index = load_nodes(paths.require(
        Path(args.catalog), "Run build_skill_catalog.py first."))
    print(f"{len(nodes)} skill nodes (those with a model embedding row)", flush=True)

    print("Layer: curriculum_order ...", flush=True)
    before, both, n_students = curriculum_order_counts(
        paths.require(Path(args.student_skill)), index)
    print("Layer: co_occurrence ...", flush=True)
    shared, items_per_skill, n_items = cooccurrence_counts(
        paths.require(Path(args.skill_item_map)), index)
    dep, _dep_meta = load_dependency(Path(args.dependency), index)
    if dep is None:
        print(f"Layer: predictive_dependency ABSENT ({args.dependency} not found) -- "
              f"no `prerequisite` edges will be derived.", flush=True)
    else:
        print("Layer: predictive_dependency ...", flush=True)

    edges = []

    # --- curriculum_order -------------------------------------------------
    support = both
    with np.errstate(invalid="ignore", divide="ignore"):
        precedence = np.where(support > 0, before / np.maximum(support, 1), 0.0)
    mask = (support >= args.min_support) & (precedence >= args.min_precedence)
    a_idx, b_idx = np.nonzero(mask)
    for a, b in zip(a_idx, b_idx):
        n_ab, n_tot = int(before[a, b]), int(support[a, b])
        # z against the null "no ordering preference" (p = 0.5): a large
        # support with precedence barely over the threshold is weak evidence.
        z = (n_ab - n_tot / 2) / np.sqrt(n_tot / 4)
        edges.append({
            "source": nodes[a]["skill_id"], "target": nodes[b]["skill_id"],
            "relation": "curriculum_order", "weight": round(float(precedence[a, b]), 6),
            "support": n_tot, "detail": f"n_before={n_ab};z={z:.1f}",
        })

    # --- co_occurrence (undirected; emitted once, canonical order) ---------
    ii, jj = np.nonzero(np.triu(shared >= args.min_cooccurrence, k=1))
    for a, b in zip(ii, jj):
        inter = int(shared[a, b])
        union = int(items_per_skill[a]) + int(items_per_skill[b]) - inter
        edges.append({
            "source": nodes[a]["skill_id"], "target": nodes[b]["skill_id"],
            "relation": "co_occurrence", "weight": round(inter / union, 6) if union else 0.0,
            "support": inter, "detail": "jaccard_over_items;undirected",
        })

    # --- content_similarity (optional, precomputed) -----------------------
    sim_path = Path(args.content_similarity)
    n_sim = 0
    if sim_path.exists():
        with open(sim_path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r["source_skill_id"] in index and r["target_skill_id"] in index:
                    edges.append({
                        "source": r["source_skill_id"], "target": r["target_skill_id"],
                        "relation": "content_similarity", "weight": round(float(r["cosine"]), 6),
                        "support": int(r.get("n_items", 0) or 0),
                        "detail": "mean_frozen_content_embedding;undirected",
                    })
                    n_sim += 1

    # --- predictive_dependency + derived prerequisite ---------------------
    n_prereq = 0
    if dep is not None:
        a_idx, b_idx = np.nonzero(np.abs(dep) >= args.min_dependency)
        for a, b in zip(a_idx, b_idx):
            edges.append({
                "source": nodes[a]["skill_id"], "target": nodes[b]["skill_id"],
                "relation": "predictive_dependency", "weight": round(float(dep[a, b]), 6),
                "support": "", "detail": "counterfactual_delta_p_on_frozen_D",
            })

        asym = dep - dep.T
        prereq_mask = (
            (support >= args.min_support)
            & (precedence >= args.min_precedence)
            & (dep >= args.min_dependency)
            & (asym >= args.min_asymmetry)
        )
        a_idx, b_idx = np.nonzero(prereq_mask)
        for a, b in zip(a_idx, b_idx):
            # A single score for ranking remediation candidates: how much the
            # model leans on A for B, scaled by how reliably A really does
            # come first. Both factors are in [0, 1]-ish units, and the
            # components stay on the edge so the score can be recomputed or
            # overridden by a domain expert.
            score = float(dep[a, b]) * float(precedence[a, b])
            edges.append({
                "source": nodes[a]["skill_id"], "target": nodes[b]["skill_id"],
                "relation": "prerequisite", "weight": round(score, 6),
                "support": int(support[a, b]),
                "detail": (f"precedence={precedence[a, b]:.3f};"
                           f"dep={dep[a, b]:.4f};asymmetry={asym[a, b]:.4f}"),
            })
            n_prereq += 1

    nodes_path = out_dir / "kg_nodes.csv"
    with open(nodes_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["skill_id", "skill_name", "vocab_idx",
                                          "n_interactions", "n_students", "n_items",
                                          "accuracy", "first_seen", "last_seen"])
        w.writeheader()
        for r in nodes:
            w.writerow({k: r[k] for k in w.fieldnames})

    edges_path = out_dir / "kg_edges.csv"
    with open(edges_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["source", "target", "relation", "weight",
                                          "support", "detail"])
        w.writeheader()
        w.writerows(edges)

    by_rel = defaultdict(int)
    for e in edges:
        by_rel[e["relation"]] += 1

    manifest = {
        "n_nodes": len(nodes),
        "n_students_observed": n_students,
        "n_items_observed": n_items,
        "edges_by_relation": dict(by_rel),
        "thresholds": {
            "min_support": args.min_support, "min_precedence": args.min_precedence,
            "min_cooccurrence": args.min_cooccurrence,
            "min_dependency": args.min_dependency, "min_asymmetry": args.min_asymmetry,
        },
        "predictive_dependency_available": dep is not None,
        "content_similarity_available": sim_path.exists(),
        "interpretation": {
            "curriculum_order": "observed teaching order; descriptive, not causal",
            "co_occurrence": "skills exercised by the same items; undirected",
            "content_similarity": "cosine of mean frozen content embeddings; undirected",
            "predictive_dependency": "counterfactual shift in the frozen model's P(correct) "
                                      "on the target when the source skill's outcome is "
                                      "changed; dependence inside the model",
            "prerequisite": "DERIVED: temporal precedence AND model dependence AND "
                            "directional asymmetry. The only layer the live gate queries. "
                            "Not a randomised causal claim -- a reviewable hypothesis.",
        },
    }
    (out_dir / "kg_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    try:
        import networkx as nx
        g = nx.MultiDiGraph()
        for r in nodes:
            g.add_node(r["skill_id"], label=r["skill_name"], vocab_idx=int(r["vocab_idx"]),
                       n_students=int(r["n_students"]),
                       accuracy=float(r["accuracy"]) if r["accuracy"] else float("nan"))
        for e in edges:
            g.add_edge(e["source"], e["target"], key=e["relation"], relation=e["relation"],
                       weight=float(e["weight"]))
        nx.write_graphml(g, out_dir / "skill_graph.graphml")
        print(f"Wrote {out_dir / 'skill_graph.graphml'}")
    except ImportError:
        print("networkx not installed; skipped GraphML export.")

    print(f"Wrote {nodes_path} ({len(nodes)} nodes)")
    print(f"Wrote {edges_path} ({len(edges):,} edges)")
    for rel, n in sorted(by_rel.items()):
        print(f"  {rel:<24} {n:>8,}")
    if dep is None:
        print("\nNo `prerequisite` edges: the predictive_dependency layer is missing.\n"
              "Temporal order alone is the curriculum's own ordering, not evidence that A "
              "is foundational for B -- run probe_predictive_dependency.py (needs the\n"
              "frozen model + embedding table) and re-run this script.")


if __name__ == "__main__":
    main()
