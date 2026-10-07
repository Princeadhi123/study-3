"""Source-masked review preparation and hypothetical response-oracle sensitivity."""
import argparse
import csv
import hashlib
import json
import math
import random
from collections import Counter
from pathlib import Path

from evidence_feedback import build_research_evidence


ROOT = Path(__file__).resolve().parent
SMOKE = ROOT / "artifacts" / "shadow_smoke_20261006"
OUTPUT = ROOT / "artifacts" / "practice_comparison_20261006"
REVIEW_SEED = 20261006
RESPONSE_SEED = 20261006
WORLDS = (("uniform_easy", -1.5, None), ("uniform_zero", 0.0, None),
          ("uniform_hard", 1.5, None), ("heterogeneous_17", None, 17),
          ("heterogeneous_29", None, 29), ("heterogeneous_43", None, 43))
RUBRIC = [
    {"id": "relevance",
     "question": "Is this question relevant to the observed errors and assessed content?",
     "choices": ["appropriate", "needs_revision", "uncertain", "not_applicable"]},
    {"id": "mathematical_validity",
     "question": "Is the stem self-contained, with exactly one correct offered option?",
     "choices": ["appropriate", "needs_revision", "uncertain", "not_applicable"]},
    {"id": "clarity",
     "question": "Can the learner understand what to do from the wording and options?",
     "choices": ["appropriate", "needs_revision", "uncertain", "not_applicable"]},
    {"id": "challenge_fit",
     "question": "Is this a reasonable next practice activity given the supplied evidence?",
     "choices": ["appropriate", "needs_revision", "uncertain", "not_applicable"]},
    {"id": "claim_scope",
     "question": "Does the displayed suggestion make unsupported mastery, misconception or learning-benefit claims?",
     "choices": ["no_concern", "concern", "uncertain", "not_applicable"]},
]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assumed_probability(correct, total, option_count, difficulty):
    """Explicit conditional oracle; no KT score or method identity is an input."""
    rate = (correct + 1) / (total + 2)
    theta = math.log(rate / (1 - rate))
    guess = 1 / option_count
    response = 1 / (1 + math.exp(-(theta - difficulty)))
    return guess + (1 - guess - 0.05) * response


def response_draw(case_id, question_id):
    raw = f"{RESPONSE_SEED}|{case_id}|{question_id}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / 2**64


def difficulty_assignment(questions, constant=None, seed=None):
    rng = random.Random(seed)
    return {q["question_id"]: constant if constant is not None else rng.uniform(-3, 3)
            for q in sorted(questions, key=lambda q: q["question_id"])}


def reviewer_option(q, names):
    if q is None:
        return {"suggested_question": None,
                "notice": "No specific practice question suggested; completed-answer feedback remains available."}
    return {"suggested_question": {"topic": names[q["skill_id"]], "text": q["text"],
                                   "options": list(q["options"])},
            "notice": "Suggested practice, not a mastery diagnosis or a promise of learning improvement."}


def blind_pack(cases, questions):
    rng = random.Random(REVIEW_SEED)
    ordered = list(cases)
    rng.shuffle(ordered)
    left_methods = ["baseline", "kt"] * (len(ordered) // 2)
    if len(ordered) % 2:
        raise ValueError("Counterbalanced source pack requires an even case count")
    rng.shuffle(left_methods)
    tasks, key = [], []
    for case, left in zip(ordered, left_methods):
        task_id = f"task_{rng.getrandbits(64):016x}"
        right = "kt" if left == "baseline" else "baseline"
        qids = {"baseline": case["baseline_question_id"], "kt": case["selected_question_id"]}
        tasks.append({
            "task_id": task_id, "data_notice": "Scripted synthetic answers, not real learner data.",
            "assessment_summary": case["review_summary"],
            "assessment_answers": case["review_answers"],
            "alternatives": [
                {"label": label, **reviewer_option(questions.get(qids[method]), case["skill_names"])}
                for label, method in (("A", left), ("B", right))],
        })
        key.append({"task_id": task_id, "case_id": case["case_id"],
                    "A": left, "B": right, "question_ids": qids})
    return {"schema": "source_masked_practice_review_v1", "rubric": RUBRIC, "tasks": tasks,
            "notice": "Judge independently. Source-masked, not guaranteed blinded. No ratings have been supplied.",
            "preference_choices": ["A", "B", "either", "neither", "insufficient_information"]}, key


def oracle_study(cases, questions):
    all_worlds = []
    availability = Counter(
        "both_selected" if c["baseline_question_id"] and c["selected_question_id"] else
        "baseline_only" if c["baseline_question_id"] else
        "kt_only" if c["selected_question_id"] else "neither_selected"
        for c in cases)
    for name, constant, seed in WORLDS:
        assignments = difficulty_assignment(list(questions.values()), constant, seed)
        paired, outcomes = [], []
        for case in cases:
            qids = {"baseline": case["baseline_question_id"], "kt": case["selected_question_id"]}
            choices = {}
            for method, qid in qids.items():
                if qid is None:
                    choices[method] = None
                    continue
                q = questions[qid]
                observed = case["skill_counts"][q["skill_id"]]
                p = assumed_probability(observed["correct"], observed["out_of"],
                                        len(q["options"]), assignments[qid])
                u = response_draw(case["case_id"], qid)
                choices[method] = {
                    "question_id": qid, "assumed_probability": p,
                    "uniform_draw": u, "simulated_correct": u < p,
                    "distance_from_0_70": abs(p - 0.70), "assumed_in_band": 0.60 <= p <= 0.80,
                }
            outcomes.append({"case_id": case["case_id"], "choices": choices})
            if all(choices[m] is not None for m in ("baseline", "kt")):
                paired.append(choices)
        means = {}
        for method in ("baseline", "kt"):
            means[method] = {
                "mean_oracle_distance_from_0_70": sum(x[method]["distance_from_0_70"] for x in paired) / len(paired) if paired else None,
                "oracle_in_band_count": sum(x[method]["assumed_in_band"] for x in paired),
                "simulated_correct_count": sum(x[method]["simulated_correct"] for x in paired),
            }
        delta = (means["kt"]["mean_oracle_distance_from_0_70"]
                 - means["baseline"]["mean_oracle_distance_from_0_70"]) if paired else None
        all_worlds.append({"world": name, "difficulty_assignment": assignments,
                           "paired_cases": len(paired), "paired_metrics": means,
                           "kt_minus_baseline_distance": delta, "outcomes": outcomes})
    return {"scope": "hypothetical_conditional_response_oracle_sensitivity_not_real_learning_evidence",
            "availability": dict(availability), "worlds": all_worlds,
            "human_reviews_completed": 0, "KT_probabilities_used_to_generate_oracle": False}


def build(smoke, output):
    if output.exists():
        raise FileExistsError("Refusing to overwrite comparison materials")
    manifest = read(smoke / "preparation_manifest.json")
    result = read(smoke / "smoke_results_private.json")
    if result["status"] != "passed_smoke_checks" or len(result["cases"]) != 10:
        raise ValueError("Expected the complete ten-case passing smoke capture")
    if sha(smoke / "preparation_manifest.json") != result["preparation_sha256"]:
        raise ValueError("Smoke preparation/result binding changed")
    for name, expected in manifest["frozen_hashes"].items():
        if sha(smoke / name) != expected:
            raise ValueError("Frozen smoke data changed")
    pool = read(smoke / "practice_pool_private.json")
    questions = {q["question_id"]: q for q in pool["questions"]}
    cases = []
    for regime in ("warm", "cold"):
        bank = read(smoke / f"{regime}_bank_private.json")
        tax = read(smoke / f"{regime}_taxonomy.json")
        inputs = {c["name"]: c for c in read(smoke / f"{regime}_cases_private.json")}
        for original in (c for c in result["cases"] if c["regime"] == regime):
            rows = inputs[original["name"]]["responses"]
            evidence = build_research_evidence(bank, tax, rows)
            if evidence["total"] != original["observed_total"]:
                raise ValueError("Case observed totals disagree")
            rec = original["recommendation"]
            for field in ("baseline_question_id", "selected_question_id"):
                if rec[field] is not None and rec[field] not in questions:
                    raise ValueError("Unknown comparison question")
            case_id = regime + "/" + original["name"]
            cases.append({
                "case_id": case_id, "skill_names": bank["skill_names"],
                "skill_counts": {s["skill_id"]: {k: s[k] for k in ("correct", "incorrect", "out_of")}
                                 for s in evidence["skills"]},
                "review_summary": [{"topic": s["skill_name"], "correct": s["correct"],
                                    "out_of": s["out_of"]} for s in evidence["skills"]],
                "review_answers": [
                    {"position": i + 1, "topic": bank["skill_names"][q["skill_id"]],
                     "text": q["text"], "options": list(q["options"]),
                     "selected_option": q["options"][r["selected_index"]],
                     "correct": r["selected_index"] == q["answer_index"]}
                    for i, (q, r) in enumerate(zip(bank["questions"], rows))],
                "baseline_question_id": rec["baseline_question_id"],
                "selected_question_id": rec["selected_question_id"],
            })
    pack, key = blind_pack(cases, questions)
    oracle = oracle_study(cases, questions)
    output.mkdir()
    reviewer, investigator = output / "reviewer", output / "investigator"
    reviewer.mkdir()
    investigator.mkdir()
    write(reviewer / "review_pack.json", pack)
    write(investigator / "decoding_key_private.json", key)
    write(investigator / "comparison_inputs_private.json", cases)
    write(investigator / "oracle_sensitivity_private.json", oracle)
    with (reviewer / "ratings_blank.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["task_id", "reviewer_id", "side"] +
                                [f["id"] for f in RUBRIC] + ["note"])
        writer.writeheader()
        writer.writerows({"task_id": t["task_id"], "side": side}
                         for t in pack["tasks"] for side in ("A", "B"))
    with (reviewer / "preferences_blank.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["task_id", "reviewer_id", "preference", "reason"])
        writer.writeheader()
        writer.writerows({"task_id": t["task_id"]} for t in pack["tasks"])
    write(investigator / "manifest.json", {
        "protocol_sha256": sha(ROOT / "PRACTICE_COMPARISON_PROTOCOL.md"),
        "method_sha256": sha(Path(__file__)),
        "source_smoke_results_sha256": sha(smoke / "smoke_results_private.json"),
        "source_preparation_sha256": sha(smoke / "preparation_manifest.json"),
        "review_pack_sha256": sha(reviewer / "review_pack.json"),
        "human_reviews_completed": 0, "review_seed": REVIEW_SEED,
        "response_seed": RESPONSE_SEED, "worlds": list(WORLDS),
    })
    print("Prepared " + str(len(cases)) + " source-masked tasks; human ratings remain blank.")
    for w in oracle["worlds"]:
        delta = w["kt_minus_baseline_distance"]
        print(w["world"], "paired_cases=", w["paired_cases"],
              "KT-minus-baseline oracle distance=", round(delta, 6) if delta is not None else None)
    print("Availability:", oracle["availability"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-dir", type=Path, default=SMOKE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    build(args.smoke_dir, args.output_dir)
