"""Run private fixed-bank synthetic scenarios with real scoring and frozen KT."""
import argparse
import copy
import hashlib
import json
import math
import tempfile
from pathlib import Path

import phase3_paths
from kt_adapter import trace_responses
from mcq_service import MCQSessionService
from scenario_runner import generate_responses
from session_store import SessionStore, bank_fingerprint

SEEDS = (11, 23, 37, 41, 53)
PROFILES = ("stable_strong", "stable_weak", "guessing", "learning", "fatigue",
            "weak_fractions")


def scenario_inputs(bank: dict, taxonomy: dict):
    questions = bank["questions"]
    by_skill = {sid: [i for i, q in enumerate(questions) if q["skill_id"] == sid]
                for sid in bank["skill_names"]}
    skills = list(by_skill)
    assert len(questions) == 40 and len(skills) == 4
    assert all(len(indexes) == 10 for indexes in by_skill.values())
    all_positions = set(range(40))
    cases = {
        "all_correct": all_positions, "all_incorrect": set(),
        "first_half_strong": set(range(20)), "second_half_strong": set(range(20, 40)),
        "global_alternating": set(range(0, 40, 2)),
        "per_skill_alternating": {i for indexes in by_skill.values() for i in indexes[::2]},
        "long_correct_streak": set(range(30)),
        "long_incorrect_streak": set(range(30, 40)),
        "all_skills_equal": {indexes[j] for indexes in by_skill.values()
                             for j in (0, 2, 4, 7, 9)},
        "tied_strongest": set().union(*(set(by_skill[s][:8]) for s in skills[:2]),
                                       *(set(by_skill[s][:3]) for s in skills[2:])),
        "tied_weakest": set().union(*(set(by_skill[s][:2]) for s in skills[:2]),
                                     *(set(by_skill[s][:8]) for s in skills[2:])),
        "multiple_weak": all_positions - set(by_skill[skills[0]] + by_skill[skills[1]]),
    }
    for sid in skills:
        cases[f"only_{sid}_weak"] = all_positions - set(by_skill[sid])
        cases[f"only_{sid}_strong"] = set(by_skill[sid])
    for name, correct_positions in cases.items():
        yield name, "deterministic", 42, correct_positions, 1, None
    topic = next(t for t in taxonomy["topics"] if
                 sum(len(s["question_ids"]) >= 2 for s in t["subtopics"]) >= 2)
    subtopics = [s for s in topic["subtopics"] if len(s["question_ids"]) >= 2][:2]
    position_by_id = {q["question_id"]: i for i, q in enumerate(questions)}
    for subtopic in subtopics:
        errors = {position_by_id[qid] for qid in subtopic["question_ids"][:2]}
        yield f"errors_in_{subtopic['id']}", "subtopic", 42, all_positions - errors, 1, None
    for profile in PROFILES:
        for seed in SEEDS:
            yield f"{profile}_s{seed}", "seeded", seed, None, 1, profile
    for shift in (1, 2):
        yield f"wrong_option_{shift}", "distractor", 42, set(range(1, 40, 2)), shift, None


def run(bank: dict, taxonomy: dict, kt) -> dict:
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        service = MCQSessionService(bank, SessionStore(Path(tmp)), taxonomy=taxonomy)
        for name, group, seed, correct_positions, wrong_shift, profile in scenario_inputs(bank, taxonomy):
            if profile is None:
                rows = [{"position": i + 1, "question_id": q["question_id"],
                         "skill_id": q["skill_id"], "correct": i in correct_positions,
                         "true_probability": float(i in correct_positions),
                         "selected_index": (q["answer_index"] if i in correct_positions else
                                            (q["answer_index"] + wrong_shift) % len(q["options"]))}
                        for i, q in enumerate(bank["questions"])]
            else:
                rows = generate_responses(bank, {"profile": profile, "seed": seed})
            assert len(rows) == 40
            assert all(len(q["options"]) >= 3 for q in bank["questions"])
            assert all(r["question_id"] == q["question_id"] and
                       r["correct"] == (r["selected_index"] == q["answer_index"])
                       for q, r in zip(bank["questions"], rows))
            contexts = []

            def capture(context):
                contexts.append(context)
                return None

            service.style_selector = capture
            sid = service.start_session()["session_id"]
            submissions = [{"question_id": r["question_id"],
                            "selected_index": r["selected_index"]} for r in rows]
            midpoint = service.submit_half(sid, 1, submissions[:20])["feed"]
            end = service.submit_half(sid, 2, submissions[20:])["feed"]
            trace = trace_responses(bank, submissions, kt=kt, device="cpu")
            p = trace["p_correct_before_each_answer"]
            assert len(p) == 40 and len(contexts) == 2
            assert all(math.isfinite(x) and 0 <= x <= 1 for x in p)
            for feed, count in ((midpoint, 20), (end, 40)):
                expected = sum(r["correct"] for r in rows[:count])
                assert feed["teacher"]["total"] == {"correct": expected, "out_of": count}
                assert sum(s["correct"] for s in feed["student"]["subtopics"]) == expected
                assert sum(s["out_of"] for s in feed["student"]["subtopics"]) == count
            assert "total" not in midpoint["student"]
            assert end["student"]["total"] == end["teacher"]["total"]
            results.append({"name": name, "group": group, "profile": profile,
                            "seed": seed, "responses": rows,
                            "observed": {"midpoint": midpoint["student"],
                                         "end": end["student"]},
                            "selector_context": {"midpoint": contexts[0], "end": contexts[1]},
                            "kt_pre_answer_probability": p,
                            "kt_status": trace["model_status"],
                            "kt_coverage": trace["coverage"]})
            print(f"{len(results):02d} {name}: {sum(r['correct'] for r in rows[:20])}/20, "
                  f"{sum(r['correct'] for r in rows)}/40", flush=True)
    assert len(results) == 54
    paired = [r for r in results if r["group"] == "distractor"]
    assert [r["responses"][i]["correct"] for i in range(40) for r in paired[:1]] == [
        r["responses"][i]["correct"] for i in range(40) for r in paired[1:]]
    assert paired[0]["observed"] == paired[1]["observed"]
    assert paired[0]["kt_pre_answer_probability"][0] == paired[1]["kt_pre_answer_probability"][0]
    return {"schema": "phase3_fixed40_comparison_v1", "bank_sha256": bank_fingerprint(bank),
            "scope": "private_synthetic_fixed_40_cold_start_not_student_validation",
            "seed_set": list(SEEDS), "kt_status": "uncalibrated_for_20_40_question_feed",
            "scenarios": results}


def replay_feedback(original: dict, bank: dict, taxonomy: dict) -> dict:
    if (original.get("schema") != "phase3_fixed40_comparison_v1"
            or original.get("bank_sha256") != bank_fingerprint(bank)):
        raise ValueError("saved evaluation must match the approved bank and schema")
    updated = copy.deepcopy(original)
    with tempfile.TemporaryDirectory() as tmp:
        contexts = []

        def capture(context):
            contexts.append(context)
            return None

        service = MCQSessionService(bank, SessionStore(Path(tmp)), taxonomy=taxonomy,
                                    style_selector=capture)
        for case, before in zip(updated["scenarios"], original["scenarios"]):
            contexts.clear()
            rows = [{"question_id": r["question_id"], "selected_index": r["selected_index"]}
                    for r in case["responses"]]
            if len(rows) != 40:
                raise ValueError("saved scenario must contain 40 responses")
            sid = service.start_session()["session_id"]
            midpoint = service.submit_half(sid, 1, rows[:20])["feed"]["student"]
            end = service.submit_half(sid, 2, rows[20:])["feed"]["student"]
            current = {"midpoint": midpoint, "end": end}
            for checkpoint in current:
                old_counts = {k: v for k, v in before["observed"][checkpoint].items()
                              if k not in ("message", "message_source")}
                new_counts = {k: v for k, v in current[checkpoint].items()
                              if k not in ("message", "message_source")}
                if old_counts != new_counts:
                    raise ValueError("feedback replay changed observed counts or assessment scope")
            if len(contexts) != 2:
                raise ValueError("expected two selector contexts")
            case["observed_before"] = copy.deepcopy(before["observed"])
            case["selector_context_before"] = copy.deepcopy(before["selector_context"])
            case["observed"] = current
            case["selector_context"] = {"midpoint": contexts[0], "end": contexts[1]}
            if (case["responses"] != before["responses"] or
                    case["kt_pre_answer_probability"] != before["kt_pre_answer_probability"] or
                    case["kt_coverage"] != before["kt_coverage"]):
                raise ValueError("replay must preserve saved response and model data")
    updated["feedback_revision"] = "observed_tie_safe_v2"
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--replay", type=Path,
                        help="replay saved feedback without loading or rerunning KT")
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(f"Refusing to overwrite {args.out}")
    service = MCQSessionService.from_default(SessionStore(phase3_paths.SESSIONS))
    if args.replay:
        source = args.replay.read_bytes()
        result = replay_feedback(json.loads(source), service.bank, service.taxonomy)
        result["replay_source_sha256"] = hashlib.sha256(source).hexdigest()
    else:
        from frozen_model import load_frozen_model
        kt = load_frozen_model(device="cpu")
        result = run(service.bank, service.taxonomy, kt)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(f"Wrote {len(result['scenarios'])} private synthetic evaluations to {args.out}")


if __name__ == "__main__":
    main()
