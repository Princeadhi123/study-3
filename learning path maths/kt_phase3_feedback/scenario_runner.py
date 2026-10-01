"""Private, fixed-bank synthetic trajectories for researcher diagnostics only."""
import argparse
import json
import math
import random
from pathlib import Path

import phase3_paths
from feedback_service import assessment_feedback_graph
from kt_adapter import trace_responses
from mcq_service import MCQSessionService
from session_store import SessionStore, bank_fingerprint

DETERMINISTIC = frozenset({
    "all_correct", "all_incorrect", "alternating",
    "first_half_correct_second_half_wrong", "weak_fractions_only",
    "weak_price_only",
})
PROBABILISTIC = frozenset({
    "stable_strong", "stable_weak", "learning", "fatigue",
    "weak_fractions", "guessing", "custom",
})


def _target_skill(bank: dict, name: str) -> str:
    matches = [sid for sid, label in bank["skill_names"].items()
               if label.casefold() == name.casefold()]
    if len(matches) != 1:
        raise ValueError(f"profile requires exactly one skill named {name!r}")
    return matches[0]


def _probability(value, field: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError(f"{field} must be a finite probability in [0, 1]")
    return float(value)


def validate_spec(bank: dict, spec: dict) -> dict:
    if not isinstance(spec, dict) or set(spec) - {
            "name", "profile", "seed", "skill_probabilities",
            "default_probability", "distractor_policy"}:
        raise ValueError("unknown scenario spec fields")
    profile = spec.get("profile", "custom")
    if profile not in DETERMINISTIC | PROBABILISTIC:
        raise ValueError(f"unknown scenario profile {profile!r}")
    seed = spec.get("seed", 42)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    name = spec.get("name", profile)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("scenario name must be nonblank")
    if spec.get("distractor_policy", "uniform_wrong") != "uniform_wrong":
        raise ValueError("only uniform_wrong distractor policy is supported")
    skills = spec.get("skill_probabilities", {})
    if not isinstance(skills, dict) or set(skills) - set(bank["skill_names"]):
        raise ValueError("skill_probabilities must map bank skill IDs")
    skills = {sid: _probability(p, f"skill_probabilities[{sid}]")
              for sid, p in skills.items()}
    default = spec.get("default_probability", 0.75)
    default = _probability(default, "default_probability")
    if profile != "custom" and (skills or "default_probability" in spec):
        raise ValueError("probability overrides are only supported for custom profiles")
    if profile == "custom" and not skills and "default_probability" not in spec:
        raise ValueError("custom profile requires skill or default probabilities")
    if profile in {"weak_fractions_only", "weak_fractions"}:
        _target_skill(bank, "Murtoluvut")
    if profile == "weak_price_only":
        _target_skill(bank, "Hinta")
    return {"name": name, "profile": profile, "seed": seed,
            "skill_probabilities": skills, "default_probability": default,
            "distractor_policy": "uniform_wrong"}


def generate_responses(bank: dict, spec: dict) -> list[dict]:
    spec = validate_spec(bank, spec)
    rng = random.Random(spec["seed"])
    attempts = {sid: 0 for sid in bank["skill_names"]}
    target = (_target_skill(bank, "Murtoluvut") if spec["profile"] in
              {"weak_fractions", "weak_fractions_only"} else
              _target_skill(bank, "Hinta") if spec["profile"] == "weak_price_only"
              else None)
    rows = []
    for position, question in enumerate(bank["questions"], 1):
        skill = question["skill_id"]
        attempt = attempts[skill]
        attempts[skill] += 1
        profile = spec["profile"]
        if profile == "all_correct":
            p = 1.0
        elif profile == "all_incorrect":
            p = 0.0
        elif profile == "alternating":
            p = float(attempt % 2 == 0)
        elif profile == "first_half_correct_second_half_wrong":
            p = float(position <= 20)
        elif profile in {"weak_fractions_only", "weak_price_only"}:
            p = float(skill != target)
        elif profile == "stable_strong":
            p = 0.8
        elif profile == "stable_weak":
            p = 0.3
        elif profile == "learning":
            p = 0.4 + 0.35 * attempt / 9
        elif profile == "fatigue":
            p = 0.75 - 0.3 * attempt / 9
        elif profile == "weak_fractions":
            p = 0.2 if skill == target else 0.75
        elif profile == "guessing":
            p = 1 / len(question["options"])
        else:
            p = spec["skill_probabilities"].get(skill, spec["default_probability"])
        correct = (p == 1 or (p != 0 and rng.random() < p))
        answer = question["answer_index"]
        wrong = [i for i in range(len(question["options"])) if i != answer]
        selected = answer if correct else rng.choice(wrong)
        rows.append({"position": position, "question_id": question["question_id"],
                     "skill_id": skill, "skill_attempt": attempt + 1,
                     "half": 1 if position <= 20 else 2,
                     "true_probability": round(p, 6),
                     "selected_index": selected, "correct": correct,
                     "option_count": len(question["options"])})
    return rows


def run_scenario(bank: dict, spec: dict, store: SessionStore,
                 kt=None, device: str | None = None,
                 gate=None, graph=None, midpoint_gate=None,
                 taxonomy: dict | None = None) -> dict:
    if gate is not None and kt is None and device is None:
        raise ValueError("conformal diagnostics require a KT trace")
    if midpoint_gate is not None and (gate is None or midpoint_gate.k != 5 or gate.k != 10):
        raise ValueError("midpoint gate requires paired k=5 and k=10 research calibrations")
    if graph is not None and not isinstance(graph, dict):
        raise ValueError("graph must be the bank-matched assessment taxonomy, not a global graph")
    if graph is not None and taxonomy is not None and graph != taxonomy:
        raise ValueError("graph and taxonomy must describe the same assessment")
    service = MCQSessionService(bank, store, taxonomy=graph if graph is not None else taxonomy)
    scenario = validate_spec(service.bank, spec)
    rows = generate_responses(service.bank, spec)
    session_id = service.start_session()["session_id"]
    submissions = [{"question_id": r["question_id"],
                    "selected_index": r["selected_index"]} for r in rows]
    midpoint = service.submit_half(session_id, 1, submissions[:20])["feed"]
    end = service.submit_half(session_id, 2, submissions[20:])["feed"]
    trace = (trace_responses(bank, submissions, kt=kt, device=device or "cpu")
             if kt is not None or device is not None else None)
    if trace is not None and len(trace["p_correct_before_each_answer"]) != 40:
        raise ValueError("KT trace must contain 40 ordered probabilities")
    result = {
        "schema": "phase3_fixed_scenario_v1", "scenario": scenario,
        "bank_sha256": bank_fingerprint(bank), "question_count": 40,
        "responses": rows, "observed": {"midpoint": midpoint, "end": end},
        "kt": trace if trace is not None else {"status": "not_requested"},
        "conformal": {"status": "not_requested"},
        "graph": {"status": "not_requested"},
        "limitations": ["synthetic responses are not student evidence",
                        "KT and conformal are researcher-only diagnostics, not scoring",
                        "simulated response probability and KT P(next correct) are not identical estimands"],
    }
    if gate is not None:
        probabilities = trace["p_correct_before_each_answer"]
        unknown = set(trace["coverage"]["unknown_item_ids"])
        regimes = ["cold" if q["item_id"] in unknown else "warm"
                   for q in bank["questions"]]
        items = []
        checkpoints = {}
        for i, probability in enumerate(probabilities):
            decision = gate.item_decision(probability, regimes[i])
            items.append({"position": i + 1, "regime": regimes[i],
                          "prediction_set": list(decision.prediction_set),
                          "status": decision.status.value})
        for label, count in (("midpoint", 20), ("end", 40)):
            checkpoints[label] = {}
            for sid in bank["skill_names"]:
                indexes = [i for i, row in enumerate(rows[:count])
                           if row["skill_id"] == sid]
                regime = "cold" if any(regimes[i] == "cold" for i in indexes) else "warm"
                checkpoint_gate = midpoint_gate if label == "midpoint" and midpoint_gate else gate
                checkpoints[label][sid] = checkpoint_gate.checkpoint(
                    [probabilities[i] for i in indexes], sid, regime=regime).to_dict()
        result["conformal"] = {
            "status": "exploratory_only", "calibrated_k": gate.k,
            "midpoint_calibrated_k": midpoint_gate.k if midpoint_gate else gate.k,
            "midpoint_status": ("historical_k5_not_validated_for_fixed_bank" if midpoint_gate
                                else "approximate_k5_not_calibrated_k10"),
            "end_status": "k10_protocol_not_validated_for_synthetic_fixed_bank",
            "items": items, "checkpoints": checkpoints,
        }
    if graph is not None:
        result["graph"] = {
            "status": "descriptive_observed_counts_only",
            "scope": "approved_bank_topics_subtopics_only",
            "relation": "is_part_of_not_prerequisite", "bank_sha256": bank_fingerprint(bank),
            "checkpoints": {checkpoint: assessment_feedback_graph(bank, graph, submissions[:limit])
                            for checkpoint, limit in (("midpoint", 20), ("end", 40))}}

    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, help="JSON scenario spec; otherwise use --profile")
    parser.add_argument("--profile", choices=sorted(DETERMINISTIC | PROBABILISTIC - {"custom"}),
                        default="alternating")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--kt-device", choices=("cpu", "cuda"))
    parser.add_argument("--conformal", action="store_true")
    parser.add_argument("--graph", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(f"Refusing to overwrite {args.out}")
    if args.conformal and not args.kt_device:
        parser.error("--conformal requires --kt-device")
    spec = (json.loads(args.spec.read_text(encoding="utf-8")) if args.spec else
            {"profile": args.profile, "seed": args.seed})
    gate = midpoint_gate = graph = None
    if args.conformal:
        from conformal_gate import ConformalGate
        import paths as phase2_paths
        gate = ConformalGate.load()
        midpoint_gate = ConformalGate.load(
            calibration_path=phase2_paths.HISTORICAL_K5_CALIBRATION,
            coverage_path=phase2_paths.HISTORICAL_K5_COVERAGE)
    service = MCQSessionService.from_default(SessionStore(phase3_paths.SESSIONS))
    if args.graph:
        graph = service.taxonomy
    result = run_scenario(service.bank, spec, service.store,
                          device=args.kt_device, gate=gate, graph=graph,
                          midpoint_gate=midpoint_gate)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(f"Wrote private fixed-bank scenario to {args.out}")


if __name__ == "__main__":
    main()
