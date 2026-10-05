"""Build private offline research banks from explicit, independent review.

Mathematical checks never use the source key to derive the expected answer.
No question edits, model inference, educator approval or live integration.
"""
import argparse
import csv
import json
import math
import re
from collections import Counter
from fractions import Fraction
from pathlib import Path

import numpy as np

import paths
from audit_bank_coverage import provenance
from review_mcq_inventory import normalized


def simple_text(text):
    return " ".join(text.replace("\\n", " ").split())


def rational(text):
    parts = text.strip().split()
    if len(parts) == 1:
        return Fraction(parts[0])
    if len(parts) == 2 and re.fullmatch(r"\d+", parts[0]):
        return Fraction(parts[0]) + Fraction(parts[1])
    raise ValueError("Unsupported rational number")


def linear(expression, simplified=False):
    expression = expression.replace("\u2212", "-").replace(" ", "")
    tokens = re.findall(r"[+-]?[^+-]+", expression)
    if not tokens or "".join(tokens) != expression:
        raise ValueError("Unsupported linear expression")
    result = Counter()
    seen = set()
    for token in tokens:
        match = re.fullmatch(r"([+-]?)(\d*)([A-Za-z]?)", token)
        if not match or not (match[2] or match[3]):
            raise ValueError("Unsupported linear term")
        variable = match[3]
        coefficient = int(match[2] or 1) * (-1 if match[1] == "-" else 1)
        if simplified and (variable in seen or (coefficient == 0 and len(tokens) > 1)):
            raise ValueError("Offered expression is not fully collected")
        seen.add(variable)
        result[variable] += coefficient
    return {variable: value for variable, value in result.items() if value}


def unique_option(options, predicate):
    matches = []
    for index, value in enumerate(options):
        try:
            if predicate(value):
                matches.append(index)
        except (ValueError, ZeroDivisionError):
            pass
    if len(matches) != 1:
        raise ValueError("Independent math check did not find exactly one correct option")
    return matches[0]


def check_linear_simplification(text, options):
    prefix = "Sievenn\u00e4 "
    if not text.startswith(prefix):
        raise ValueError("Reviewed linear stem changed")
    expression = text[len(prefix):]
    if expression.startswith("lauseke:"):
        expression = expression[len("lauseke:"):].lstrip()
    if not expression:
        raise ValueError("Reviewed linear stem changed")
    coefficients = linear(expression)
    index = unique_option(options, lambda v: linear(v, simplified=True) == coefficients)
    proof = f"Collect like-term coefficients: {coefficients}; fully collected choice is {options[index]!r}."
    return index, proof, []


def check_percent_of(text, options):
    match = re.fullmatch(r"(\d+) % luvusta (\d+)", text)
    if not match:
        raise ValueError("Reviewed percentage stem changed")
    value = Fraction(int(match[1]) * int(match[2]), 100)
    index = unique_option(options, lambda v: rational(v) == value)
    return index, f"{match[1]}/100 * {match[2]} = {value}.", []


def check_divisibility(text, options):
    match = re.fullmatch(r"(\d+) on jaollinen luvulla\.\.\.", text)
    if not match:
        raise ValueError("Reviewed divisibility stem changed")
    value = int(match[1])
    index = unique_option(options, lambda v: int(v) > 0 and value % int(v) == 0)
    proof = f"{value} / {options[index]} = {value // int(options[index])}; other offered divisors leave a remainder."
    return index, proof, []


def check_prime_identification(text, options):
    if text != "Alkuluku?":
        raise ValueError("Reviewed prime stem changed")

    def prime(value):
        n = int(value)
        return n >= 2 and all(n % divisor for divisor in range(2, math.isqrt(n) + 1))

    index = unique_option(options, prime)
    proof = f"{options[index]} has no divisor from 2 through its integer square root; other choices are composite."
    return index, proof, []


def check_fraction_definition(text, options):
    if text != "Murtoluku on kahden kokonaisluvun...":
        raise ValueError("Reviewed fraction-definition stem changed")
    answer = "...osam\u00e4\u00e4r\u00e4ksi kirjoitettu luku."
    index = unique_option(options, lambda v: v == answer)
    proof = "A fraction denotes a quotient of integers, with nonzero denominator, not a sum or product."
    notes = ["Nonzero-denominator convention is implicit in the standard fraction definition."]
    return index, proof, notes


def check_largest_fraction(text, options):
    if text != "Suurin murtoluku?":
        raise ValueError("Reviewed largest-fraction stem changed")
    values = [rational(value) for value in options]
    index = unique_option(options, lambda v: rational(v) == max(values))
    proof = f"Compare exact rational values {list(map(str, values))}; maximum is {values[index]}."
    return index, proof, []


def check_half_matching(text, options):
    if text != "puolikas":
        raise ValueError("Reviewed half-matching stem changed")
    index = unique_option(options, lambda v: rational(v) == Fraction(1, 2))
    proof = f"'puolikas' means one half; {options[index]} = 1/2."
    return index, proof, ["Short concept-matching stem; educator should approve its presentation."]


def check_fraction_equality(text, options):
    parts = text.split(" = ")
    if len(parts) != 2:
        raise ValueError("Reviewed fraction-equality stem changed")
    left, right = map(rational, parts)
    truth = left == right
    answer = "tosi" if truth else "valhe"
    index = unique_option(options, lambda v: v == answer)
    proof = f"{left} {'=' if truth else '!='} {right}; select {answer!r}."
    notes = ["Effectively true/false: maybe and heart are nonsemantic distractors, not two additional strong choices."]
    return index, proof, notes


FAMILY_CHECKERS = {
    "linear_simplification": check_linear_simplification,
    "percent_of": check_percent_of,
    "divisibility": check_divisibility,
    "prime_identification": check_prime_identification,
    "fraction_definition": check_fraction_definition,
    "largest_fraction": check_largest_fraction,
    "half_matching": check_half_matching,
    "fraction_equality": check_fraction_equality,
}


def independently_check(question, family):
    text, options = simple_text(question["text"]), question["options"]
    if (type(question["answer_index"]) is not int or len(options) < 2
            or any(not isinstance(v, str) or not v.strip() for v in options)
            or len(set(options)) != len(options)):
        raise ValueError("Malformed source options or key")
    checker = FAMILY_CHECKERS.get(family)
    if checker is None:
        raise ValueError("Unknown independently reviewed family")
    index, proof, notes = checker(text, options)
    if index != question["answer_index"]:
        raise ValueError("Independent answer contradicts source key; do not silently repair")
    return index, proof, notes


def add_decision_range(by_number, first, last, decision):
    for number in range(first, last + 1):
        if number in by_number:
            raise ValueError("Overlapping review decisions")
        by_number[number] = decision


def expand_decisions(queue, decisions):
    by_number = {}
    for family, ranges in decisions["accepted_families"].items():
        for first, last in ranges:
            add_decision_range(by_number, first, last, ("accepted", family))
    for excluded in decisions["excluded_ranges"]:
        first, last = excluded["range"]
        add_decision_range(by_number, first, last, ("excluded", excluded["reason"]))
    numbers = [q["review_number"] for q in queue["candidates"]]
    if len(set(numbers)) != len(numbers) or set(numbers) != set(by_number):
        raise ValueError("Review decisions must cover every candidate exactly once")
    return by_number


def review(queue, decisions):
    decisions_by_number = expand_decisions(queue, decisions)
    rows = []
    for candidate in queue["candidates"]:
        status, detail = decisions_by_number[candidate["review_number"]]
        row = {**candidate, "review_status": status}
        if status == "accepted":
            index, proof, notes = independently_check(candidate["question"], detail)
            row.update(checked_family=detail, checked_answer_index=index,
                       mathematical_basis=proof, quality_notes=notes)
        else:
            row["exclusion_reason"] = detail
        rows.append(row)
    return rows


def embedding_matches(embedding_path, required):
    # Load only the text array; never materialize the multi-GB vector matrix.
    with np.load(embedding_path, allow_pickle=True) as data:
        if "vectors" not in data.files:
            raise ValueError("Frozen embedding table missing vector array")
        texts = data["texts"]
        if texts.ndim != 1:
            raise ValueError("Frozen embedding texts must be one-dimensional")
        matches = {}
        for index, text in enumerate(texts):
            if text in required:
                matches[str(text)] = index
    return matches


def attach_compatibility(rows, vocab, matches):
    for row in rows:
        if row["review_status"] != "accepted":
            continue
        q = row["question"]
        if q["skill_id"] not in vocab["skill_vocab"]:
            raise ValueError("Reviewed skill absent from model vocabulary")
        item_index = vocab["item_vocab"].get(q["item_id"], 1)
        if (item_index == 1) != (row["regime"] == "cold"):
            raise ValueError("Reviewed item regime disagrees with frozen vocabulary")
        required = {q["content_text"], *q["options"]}
        missing = sorted(required - matches.keys())
        row["kt_compatibility"] = {
            "compatible": not missing, "missing_embedding_texts": missing,
            "skill_index": vocab["skill_vocab"][q["skill_id"]], "item_index": item_index,
            "content_row": matches.get(q["content_text"]),
            "option_rows": [matches.get(v) for v in q["options"]],
            "scope": "Vocabulary and exact embedding-text membership only; no model performance claim.",
        }


def assemble(rows, skill_names):
    banks, selected, shortages = {}, {}, []
    for regime in ("warm", "cold"):
        chosen = {}
        for skill in skill_names:
            usable = [r for r in rows if r["regime"] == regime
                      and r["question"]["skill_id"] == skill
                      and r["review_status"] == "accepted"
                      and r["kt_compatibility"]["compatible"]]
            if len(usable) < 10:
                shortages.append({"regime": regime, "skill_id": skill,
                                  "available": len(usable), "required": 10})
            chosen[skill] = usable[:10]
        if any(len(r) != 10 for r in chosen.values()):
            continue
        ordered = [chosen[skill][2 * slot + half] for half in (0, 1)
                   for slot in range(5) for skill in skill_names]
        questions = [r["question"] for r in ordered]
        if (len(skill_names) != 4 or len(questions) != 40
                or len({q["question_id"] for q in questions}) != 40
                or len({normalized(q["text"]) for q in questions}) != 40):
            raise ValueError("Bank must have four skills and forty distinct IDs/prompts")
        selected[regime] = ordered
        banks[regime] = {
            "protocol": "offline_historical_evaluation_bank",
            "review_status": "independently_math_checked; educator_approval_pending",
            "item_regime": regime,
            "selection": "First ten content-reviewed, embedding-compatible frozen representatives per skill; no model/outcome-based selection. Five per skill in each half.",
            "skill_names": skill_names, "questions": questions,
            "limitations": [
                "Private answer keys: never serve this JSON directly to students.",
                "No live feed/session defaults changed; research only.",
                "Cold refers to item-ID holdout, not content-level novelty.",
                "Historical answers are not evidence of complete 40-question assessment sessions.",
                "Not validated for KT, conformal prediction, learner level or learning effects.",
                "Some fractions use effectively true/false choices with nonsemantic distractors.",
            ],
        }
    return banks, selected, shortages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    review_dir = paths.ARTIFACTS / "evaluation_content_review_20261004"
    parser.add_argument("--queue", type=Path, default=review_dir / "review_queue_private.json")
    parser.add_argument("--decisions", type=Path, default=review_dir / "review_decisions_private.json")
    parser.add_argument("--vocab", type=Path, default=paths.VOCAB)
    parser.add_argument("--embeddings", type=Path, default=paths.TEXT_EMBEDDINGS)
    parser.add_argument("--out-dir", type=Path, default=paths.ARTIFACTS / "evaluation_banks_reviewed_20261004")
    args = parser.parse_args()
    if args.out_dir.exists():
        raise FileExistsError("Refusing to overwrite reviewed bank artifacts")
    inputs = {k: paths.require(getattr(args, k)) for k in ("queue", "decisions", "vocab", "embeddings")}
    queue_hash = provenance(inputs["queue"])
    queue, decisions, vocab = [json.loads(inputs[k].read_text(encoding="utf-8"))
                               for k in ("queue", "decisions", "vocab")]
    if queue_hash["sha256"] != decisions["queue_sha256"]:
        raise ValueError("Review decisions are not bound to this frozen queue")
    rows = review(queue, decisions)
    required = set()
    for row in rows:
        if row["review_status"] == "accepted":
            required.update((row["question"]["content_text"], *row["question"]["options"]))
    matches = embedding_matches(inputs["embeddings"], required)
    attach_compatibility(rows, vocab, matches)
    banks, selected, shortages = assemble(rows, queue["skill_names"])
    args.out_dir.mkdir(parents=True, exist_ok=False)
    results = {
        "protocol": "private_reviewed_evaluation_bank_build",
        "status": "both_banks_built" if not shortages else "bank_shortfalls",
        "inputs": {k: provenance(p) for k, p in inputs.items()},
        "content_review_counts": dict(Counter(r["review_status"] for r in rows)),
        "bank_shortfalls": shortages,
        "candidates": rows,
        "bank_review_numbers": {regime: [r["review_number"] for r in members]
                                for regime, members in selected.items()},
        "cross_bank_content_overlap": sorted(
            {q["content_text"] for q in banks.get("warm", {}).get("questions", [])}
            & {q["content_text"] for q in banks.get("cold", {}).get("questions", [])}),
        "cross_bank_prompt_overlap": sorted(
            {normalized(q["text"]) for q in banks.get("warm", {}).get("questions", [])}
            & {normalized(q["text"]) for q in banks.get("cold", {}).get("questions", [])}),
        "limitations": [
            "Independent key/content review is not educator approval or model validation.",
            "Cross-bank matches demonstrate that held-out item IDs need not imply unseen content.",
            "Per-question student counts must not be summed to infer unique bank students.",
            "Selected answer counts include repeated attempts and are not independent samples.",
        ],
    }
    with (args.out_dir / "review_results_private.json").open("x", encoding="utf-8") as stream:
        json.dump(results, stream, ensure_ascii=True, indent=2)
        stream.write("\n")
    for regime, bank in banks.items():
        with (args.out_dir / (regime + "_bank_private.json")).open("x", encoding="utf-8") as stream:
            json.dump(bank, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
    with (args.out_dir / "selected_support.csv").open("x", encoding="utf-8", newline="") as stream:
        fields = ("regime", "position", "review_number", "question_id", "skill_id",
                  "item_id", "n", "students", "correct", "incorrect", "with_history_n",
                  "history_median", "quality_notes")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for regime, members in selected.items():
            for position, row in enumerate(members, 1):
                q, support = row["question"], row["support"]
                writer.writerow({
                    "regime": regime, "position": position, "review_number": row["review_number"],
                    **{k: q[k] for k in ("question_id", "skill_id", "item_id")},
                    **{k: support[k] for k in ("n", "students", "correct", "incorrect", "with_history_n")},
                    "history_median": support["preceding_events_in_window"]["median"],
                    "quality_notes": " | ".join(row["quality_notes"]),
                })
    print(f"Independently checked {len(rows)} candidates; built banks: {list(banks)}; shortages: {shortages}")
    print(f"Private offline outputs: {args.out_dir}")


if __name__ == "__main__":
    main()
