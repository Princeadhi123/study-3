"""Stage 5c -- querying the graph: "the gate flagged skill B; what do we drop back to?"

This is the glue between Layer 3 (the conformal gate) and the Socratic LLM.
When `CheckpointTriggerResult.status is CONFIDENT_STRUGGLE` on a target
skill, the LLM needs a *foundational* skill to teach back to -- not the
topic the student just failed, which is the one they have already
demonstrated they cannot do unaided.

`KnowledgeGraph.remediation_lookup` is shaped to be passed straight into
`ConformalGate.checkpoint(..., remediation_lookup=...)`.


## Only the derived `prerequisite` layer is traversed

Deliberately. `curriculum_order` alone would happily route a student
struggling with angles back to "multiplication", because multiplication is
taught earlier -- a precedence of 1.00 in this dataset, and pedagogically
useless. `co_occurrence` has no direction to follow at all. Remediation
therefore follows `prerequisite` edges, which additionally require the
frozen model to actually lean on the source skill when predicting the
target, and to do so asymmetrically.

If the `prerequisite` layer is absent (the counterfactual probe has not been
run), the honest answer is "no grounded recommendation", and that is what
is returned. `--allow-weak-evidence` opts into a `curriculum_order` fallback
for exploration, and every result it produces is tagged `evidence="weak"`
so a caller can refuse to act on it.


## Depth

Depth 1 is the direct prerequisite. Depth 2 exists because the immediate
prerequisite is sometimes itself not mastered -- the ranking penalises
distance geometrically (`--depth-decay`) so a direct, strong prerequisite
always outranks a remote one.

    python kg_query.py --skill-name "Murtolukujen kerto- ja jakolasku"
"""
import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import paths

PREREQ = "prerequisite"
FALLBACK = "curriculum_order"


class KnowledgeGraph:
    def __init__(self, nodes: dict, in_edges: dict, relations: set):
        self.nodes = nodes                  # skill_id -> node dict
        self.in_edges = in_edges            # relation -> target -> [(source, weight, detail)]
        self.relations = relations
        self.by_name = {n["skill_name"]: sid for sid, n in nodes.items()}

    @classmethod
    def load(cls, nodes_path: Optional[Path] = None, edges_path: Optional[Path] = None):
        nodes_path = Path(nodes_path or paths.KG_NODES)
        edges_path = Path(edges_path or paths.KG_EDGES)
        paths.require(nodes_path, "Run build_knowledge_graph.py first.")
        paths.require(edges_path, "Run build_knowledge_graph.py first.")

        nodes = {}
        with open(nodes_path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                nodes[r["skill_id"]] = r

        in_edges = defaultdict(lambda: defaultdict(list))
        relations = set()
        with open(edges_path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                relations.add(r["relation"])
                in_edges[r["relation"]][r["target"]].append(
                    (r["source"], float(r["weight"]), r["detail"]))
        return cls(nodes, in_edges, relations)

    def resolve(self, skill: str) -> Optional[str]:
        """Accept either a skill_id or a human-readable skill_name."""
        if skill in self.nodes:
            return skill
        return self.by_name.get(skill)

    def foundational_for(self, skill: str, max_results: int = 3, max_depth: int = 2,
                         depth_decay: float = 0.5, allow_weak_evidence: bool = False) -> list:
        """Ranked foundational skills to remediate towards, strongest first."""
        target = self.resolve(skill)
        if target is None:
            return []

        relation = PREREQ
        evidence = "grounded"
        if not self.in_edges.get(PREREQ):
            if not allow_weak_evidence:
                return []
            relation, evidence = FALLBACK, "weak"

        best = {}
        frontier = [(target, 1.0)]
        seen = {target}
        for depth in range(1, max_depth + 1):
            nxt = []
            for node, carried in frontier:
                for source, weight, detail in self.in_edges[relation].get(node, []):
                    if source == target:
                        continue
                    score = carried * weight * (depth_decay ** (depth - 1))
                    prev = best.get(source)
                    if prev is None or score > prev["score"]:
                        node_info = self.nodes.get(source, {})
                        best[source] = {
                            "skill_id": source,
                            "skill_name": node_info.get("skill_name", source),
                            "score": score,
                            "depth": depth,
                            "edge_weight": weight,
                            "relation": relation,
                            "evidence": evidence,
                            "detail": detail,
                            # Population accuracy on the foundational skill:
                            # useful context for the LLM ("this is hard for
                            # everyone" vs "this student specifically").
                            "population_accuracy": (float(node_info["accuracy"])
                                                    if node_info.get("accuracy") else None),
                        }
                    if source not in seen:
                        seen.add(source)
                        nxt.append((source, score))
            frontier = nxt
            if not frontier:
                break

        ranked = sorted(best.values(), key=lambda d: -d["score"])
        return ranked[:max_results]

    def remediation_lookup(self, max_results: int = 3, max_depth: int = 2,
                           allow_weak_evidence: bool = False):
        """Adapter for `ConformalGate.checkpoint(remediation_lookup=...)`."""

        def lookup(skill_id: str) -> Optional[dict]:
            candidates = self.foundational_for(
                skill_id, max_results=max_results, max_depth=max_depth,
                allow_weak_evidence=allow_weak_evidence)
            if not candidates:
                return {
                    "target_skill_id": skill_id,
                    "candidates": [],
                    "note": ("No grounded prerequisite edge for this skill. Either the "
                             "counterfactual probe has not been run, or this skill has no "
                             "foundational dependency that clears the evidence thresholds. "
                             "Remediate within the skill itself rather than jumping back."),
                }
            return {"target_skill_id": skill_id, "candidates": candidates}

        return lookup


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--skill", help="skill_id or exact skill_name")
    p.add_argument("--skill-name", dest="skill", help=argparse.SUPPRESS)
    p.add_argument("--max-results", type=int, default=3)
    p.add_argument("--max-depth", type=int, default=2)
    p.add_argument("--allow-weak-evidence", action="store_true")
    args = p.parse_args()

    kg = KnowledgeGraph.load()
    print(f"{len(kg.nodes)} nodes; relations present: {sorted(kg.relations)}")
    if not args.skill:
        return
    out = kg.foundational_for(args.skill, args.max_results, args.max_depth,
                              allow_weak_evidence=args.allow_weak_evidence)
    if not out:
        sys.stdout.buffer.write(
            f"No foundational skill found for {args.skill!r}.\n".encode("utf-8"))
        if PREREQ not in kg.relations:
            print("(The `prerequisite` layer is missing -- run "
                  "probe_predictive_dependency.py, then rebuild the graph.)")
        return
    for r in out:
        sys.stdout.buffer.write(
            (f"  score={r['score']:.4f} depth={r['depth']} evidence={r['evidence']}  "
             f"{r['skill_name']}\n    {r['detail']}\n").encode("utf-8"))


if __name__ == "__main__":
    main()
