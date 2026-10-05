"""Exact support optimization over the frozen, closed-review MCQ universe."""
import argparse
import itertools
import json
from collections import defaultdict
from pathlib import Path

from audit_stronger_banks import fingerprint, representatives
from review_mcq_inventory import normalized


def optimal_assignment(rows, skills, quota=10):
    """Minimum-cost flow: topic quotas -> unique prompts, cost = -students."""
    candidates = []
    for skill in skills:
        candidates.extend(representatives(
            [r for r in rows if r["question"]["skill_id"] == skill], 0))
    prompts = sorted({normalized(r["question"]["text"]) for r in candidates})
    source, sink = 0, 1
    skill_nodes = {s: i + 2 for i, s in enumerate(skills)}
    prompt_nodes = {p: i + 2 + len(skills) for i, p in enumerate(prompts)}
    graph = [[] for _ in range(2 + len(skills) + len(prompts))]

    def edge(start, end, capacity, cost):
        forward = [end, len(graph[end]), capacity, cost]
        reverse = [start, len(graph[start]), 0, -cost]
        graph[start].append(forward)
        graph[end].append(reverse)
        return forward

    for skill in skills:
        edge(source, skill_nodes[skill], quota, 0)
    for prompt in prompts:
        edge(prompt_nodes[prompt], sink, 1, 0)
    references = []
    for row in sorted(candidates, key=lambda r: (
            r["question"]["skill_id"], r["question"]["question_id"])):
        arc = edge(skill_nodes[row["question"]["skill_id"]],
                   prompt_nodes[normalized(row["question"]["text"])],
                   1, -row["support"]["students"])
        references.append((row, arc))
    flow, cost = 0, 0
    while flow < quota * len(skills):
        distance = [None] * len(graph)
        parent = [None] * len(graph)
        distance[source] = 0
        for _ in range(len(graph) - 1):
            changed = False
            for start, edges in enumerate(graph):
                if distance[start] is None:
                    continue
                for index, arc in enumerate(edges):
                    end, _, capacity, weight = arc
                    candidate = distance[start] + weight
                    if capacity and (distance[end] is None or candidate < distance[end]):
                        distance[end] = candidate
                        parent[end] = (start, index)
                        changed = True
            if not changed:
                break
        if distance[sink] is None:
            return None
        node = sink
        while node != source:
            start, index = parent[node]
            arc = graph[start][index]
            arc[2] -= 1
            graph[node][arc[1]][2] += 1
            node = start
        flow += 1
        cost += distance[sink]
    selected = [r for r, arc in references if arc[2] == 0]
    assert len(selected) == quota * len(skills)
    assert sum(r["support"]["students"] for r in selected) == -cost
    assert len({normalized(r["question"]["text"]) for r in selected}) == len(selected)
    return selected


def optimize(pool):
    by = defaultdict(list)
    for row in pool:
        by[(row["question"]["skill_id"], row["regime"])].append(row)
    skills = sorted({s for s, _ in by if all(
        len(representatives(by.get((s, reg), []), 2)) >= 10 for reg in ("warm", "cold"))})
    combinations, winner, best = [], None, None
    for topics in itertools.combinations(skills, 4):
        bounds = [representatives(by[(s, reg)], 2)[9]["support"]["students"]
                  for s in topics for reg in ("warm", "cold")]
        upper = min(bounds)
        levels = sorted({r["support"]["students"] for r in pool
                         if r["question"]["skill_id"] in topics
                         and 2 <= r["support"]["students"] <= upper}, reverse=True)
        result = None
        for minimum in levels:
            result = {}
            for reg in ("warm", "cold"):
                eligible = [r for r in pool if r["regime"] == reg
                            and r["question"]["skill_id"] in topics
                            and r["support"]["students"] >= minimum]
                assigned = optimal_assignment(eligible, topics)
                if assigned is None:
                    result = None
                    break
                result[reg] = assigned
            if result is not None:
                break
        certificate = {"topics": list(topics), "minimum_support_upper_bound": upper,
                       "feasible": result is not None}
        if result is not None:
            selected = result["warm"] + result["cold"]
            objective = (min(r["support"]["students"] for r in selected),
                         sum(r["support"]["students"] for r in selected))
            certificate.update(minimum_students_per_rendering=objective[0],
                               sum_per_rendering_student_counts=objective[1])
            if best is None or objective > best:
                best, winner = objective, result
        combinations.append(certificate)
    if winner is None:
        raise ValueError("No eligible four-topic pair exists")
    return winner, {"eligible_source_topics": skills, "combinations": combinations,
                    "objective": {"minimum_students_per_rendering": best[0],
                                  "sum_per_rendering_student_counts": best[1]},
                    "objective_order": [
                        "Maximize minimum distinct-student count per selected exact rendering across both banks.",
                        "Then maximize sum of per-rendering student counts; NOT unique students across banks.",
                        "Stable source/topic/question ordering breaks remaining ties."],
                    "algorithm": "Exhaustive four-source-topic combinations, exact support cutoffs, integer min-cost bipartite flow with topic quotas and global prompt uniqueness within each regime.",
                    "qualification": "Global optimum only within the frozen catalogue/support capture and the recorded content/format/topic eligibility rules; not universally best educational assessment."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.out_dir.exists():
        raise FileExistsError("Refusing to overwrite optimized banks")
    pool_path = args.review_dir / "closed_pool_private.json"
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    winner, certificate = optimize(pool)
    review = json.loads((args.review_dir / "review_manifest.json").read_text(encoding="utf-8"))
    names = {r["skill_id"]: r["skill_name"] for r in json.loads(
        (args.review_dir / "source_skill_capacity_bounds.json").read_text(encoding="utf-8"))}
    args.out_dir.mkdir(parents=True, exist_ok=False)
    selected_reviews = {}
    for reg, selected in winner.items():
        skills = sorted({r["question"]["skill_id"] for r in selected})
        grouped = {s: sorted([r for r in selected if r["question"]["skill_id"] == s],
                            key=lambda r: (-r["support"]["students"], r["question"]["question_id"]))
                   for s in skills}
        ordered = [grouped[s][2 * slot + half] for half in (0, 1)
                   for slot in range(5) for s in skills]
        selected_reviews[reg] = ordered
        bank = {
            "protocol": "offline_historical_evaluation_bank",
            "review_status": "independently_math_checked; educator_approval_pending",
            "research_version": "3_support_optimal_under_declared_rules_20261005",
            "item_regime": reg, "skill_names": {s: names[s] for s in skills},
            "selection": certificate["objective_order"],
            "questions": [r["question"] for r in ordered],
            "limitations": review["limitations"] + [
                "Counts are per rendering; repeated attempts and student overlap remain.",
                "Cold item IDs do not ensure content novelty or equal difficulty.",
                "No complete selected-bank sessions, predictive validation or coverage guarantee.",
                "No serving defaults changed; private research files only."],
        }
        (args.out_dir / (reg + "_bank_private.json")).write_text(
            json.dumps(bank, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    (args.out_dir / "selected_review_private.json").write_text(
        json.dumps(selected_reviews, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    certificate["inputs"] = {
        "closed_pool": fingerprint(pool_path),
        "review_manifest": fingerprint(args.review_dir / "review_manifest.json"),
        "capacity_bounds": fingerprint(args.review_dir / "source_skill_capacity_bounds.json"),
        "optimizer_method": fingerprint(Path(__file__)),
    }
    certificate["bank_hashes"] = {reg: fingerprint(args.out_dir / (reg + "_bank_private.json"))
                                for reg in winner}
    certificate["selected_support_ranges"] = [
        {"regime": reg, "skill_id": skill,
         "min": min(r["support"]["students"] for r in members if r["question"]["skill_id"] == skill),
         "max": max(r["support"]["students"] for r in members if r["question"]["skill_id"] == skill)}
        for reg, members in selected_reviews.items()
        for skill in sorted({r["question"]["skill_id"] for r in members})]
    (args.out_dir / "optimality_certificate.json").write_text(
        json.dumps(certificate, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(certificate, indent=2))


if __name__ == "__main__":
    main()
