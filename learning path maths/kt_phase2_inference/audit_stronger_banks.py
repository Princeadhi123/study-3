"""Screen frozen all-skill MCQ support, without inference or live-bank changes.

This is a conservative, independently calculable subset, not a certification
of every unrecognized question. Historical counts are reused, not rescanned.
"""
import argparse
import ast
import csv
import gzip
import hashlib
import json
import re
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path

import paths
from build_evaluation_banks import independently_check, embedding_matches
from build_text_only_bank import BLOCKING_FLAGS, FIELDS
from review_mcq_inventory import normalized

SUPPORT_LEVELS = (2, 5, 10, 20)
BAD_OPTIONS = {"maybe", "heart", "ehk\u00e4", "\u2665", "en tied\u00e4", "?", "-"}


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"name": path.name, "bytes": path.stat().st_size,
            "sha256": digest.hexdigest()}


def numeric(value):
    text = value.strip().replace("\u2212", "-").replace("\u2044", "/")
    text = text.replace(",", ".")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?(?:/\d+)?", text):
        raise ValueError("Not an unambiguous numeric option")
    return Fraction(text)


def expression(value):
    text = value.replace("\u2212", "-").replace("\u2044", "/")
    text = text.replace("\u00b7", "*").replace(",", ".")
    if len(text) > 100 or not re.fullmatch(r"[\d\s.+*/()\-]+", text):
        raise ValueError("Unsupported explicit arithmetic")
    tree = ast.parse(text.strip(), mode="eval")

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return Fraction(ast.get_source_segment(text.strip(), node))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            result = visit(node.operand)
            return -result if isinstance(node.op, ast.USub) else result
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
        raise ValueError("Unsupported arithmetic node")

    return visit(tree.body)


def checked_numeric(question, expected, family):
    values = [numeric(option) for option in question["options"]]
    if len(set(values)) != len(values):
        raise ValueError("Numerically equivalent options")
    matches = [i for i, value in enumerate(values) if value == expected]
    if len(matches) != 1:
        raise ValueError("No unique independently correct option")
    if matches[0] != question["answer_index"]:
        raise ValueError("Independent answer contradicts source key")
    return family, f"Exact calculation gives {expected}; one matching option."


def check_question(question):
    """Only accept explicit instructions/operations; never infer missing media."""
    text = " ".join(question["text"].replace("\\n", " ").split())
    opts = [normalized(value) for value in question["options"]]
    if len(opts) < 3 or len(set(opts)) != len(opts) or set(opts) & BAD_OPTIONS:
        raise ValueError("Fewer than three distinct substantive options")
    if type(question["answer_index"]) is not int:
        raise ValueError("Non-integer key")
    family = None
    if re.fullmatch(r"Sievenn\u00e4\s+(?:lauseke:\s*)?[+\-\u2212\dA-Za-z\s]+", text):
        family = "linear_simplification"
    elif re.fullmatch(r"\d+ % luvusta \d+", text):
        family = "percent_of"
    elif re.fullmatch(r"\d+ on jaollinen luvulla\.\.\.", text):
        family = "divisibility"
    elif text == "Alkuluku?":
        family = "prime_identification"
    elif text == "Suurin murtoluku?":
        family = "largest_fraction"
    if family:
        _, proof, notes = independently_check(question, family)
        if notes:
            raise ValueError("Quality notes present")
        if family != "linear_simplification":
            values = [numeric(v) for v in question["options"]]
            if len(set(values)) != len(values):
                raise ValueError("Numerically equivalent options")
        return family, proof
    match = re.fullmatch(r"Kuinka paljon on (\d+) % luvusta (\d+)\?", text)
    if match:
        value = Fraction(int(match[1]) * int(match[2]), 100)
        return checked_numeric(question, value, "percent_of_worded")
    match = re.fullmatch(r"luvun (-?\d+) vastaluku", text)
    if not match:
        match = re.fullmatch(
            r"Olet nyt lukusuoran numerossa (-?\d+)\. Siirry luvun -?\d+ "
            r"vastaluvun kohdalle\. Mihin lukusuoran numeroon p\u00e4\u00e4dyt\?", text)
        if match:
            numbers = re.findall(r"-?\d+", text)
            if numbers[0] != numbers[1]:
                raise ValueError("Inconsistent opposite-number instructions")
    if match:
        return checked_numeric(question, Fraction(-int(match[1])), "opposite_number")
    match = re.fullmatch(
        r"Mik\u00e4 on luvun (-?\d+) itseisarvo, eli \|(-?\d+)\|\?", text)
    if match:
        if match[1] != match[2]:
            raise ValueError("Inconsistent absolute-value instructions")
        return checked_numeric(question, Fraction(abs(int(match[1]))), "absolute_value")
    match = re.fullmatch(r"(-?\d+) < x < (-?\d+)", text)
    if match:
        values = [numeric(v) for v in question["options"]]
        if len(set(values)) != len(values):
            raise ValueError("Numerically equivalent options")
        answers = [v for v in values if int(match[1]) < v < int(match[2])]
        if len(answers) != 1:
            raise ValueError("Inequality lacks unique offered solution")
        return checked_numeric(question, answers[0], "inequality")
    arithmetic = text[4:] if text.startswith("x = ") else text
    if re.fullmatch(r"[\d\s.,+*/()\-\u2212\u2044\u00b7]+", arithmetic):
        if not any(op in arithmetic for op in ("+", "-", "\u2212", "*", "\u00b7")):
            raise ValueError("No explicit arithmetic operation")
        value = expression(arithmetic)
        family = ("fraction_arithmetic" if "/" in arithmetic or "\u2044" in arithmetic
                  else "numeric_arithmetic")
        return checked_numeric(question, value, family)
    raise LookupError("Not covered by independently checked families; needs content review")


def representatives(rows, minimum):
    chosen = {}
    for row in sorted(rows, key=lambda r: (
            -r["support"]["students"], -r["support"]["with_history_students"],
            -r["support"]["n"], r["question"]["question_id"])):
        if row["support"]["students"] < minimum:
            continue
        chosen.setdefault(normalized(row["question"]["text"]), row)
    return list(chosen.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.out_dir.exists():
        raise FileExistsError("Refusing to overwrite audit artifacts")
    availability = paths.ARTIFACTS / "evaluation_candidate_inventory_all_skills_20261004" / "availability.json"
    inventory = paths.ARTIFACTS / "mcq_inventory_private.jsonl.gz"
    report = json.loads(availability.read_text(encoding="utf-8"))
    vocab = json.loads(paths.VOCAB.read_text(encoding="utf-8"))
    inputs = {k: fingerprint(p) for k, p in (
        ("availability", availability), ("inventory", inventory), ("vocab", paths.VOCAB))}
    for key in ("inventory", "vocab"):
        if inputs[key] != report["inputs"][key]:
            raise ValueError(f"Frozen {key} provenance differs")
    candidates = report["warm_candidates"] + report["cold_candidates"]
    identities = {(q["skill_id"], q["question_id"]): q for q in candidates}
    if len(identities) != len(candidates):
        raise ValueError("Duplicate availability identity")
    accepted, rejected = [], Counter()
    unreviewed = []
    catalog_count = 0
    with gzip.open(inventory, "rt", encoding="utf-8") as stream:
        for line in stream:
            question = json.loads(line)
            catalog_count += 1
            identity = (question["skill_id"], question["question_id"])
            support = identities.pop(identity, None)
            if support is None:
                continue
            for field in ("skill_id", "item_id", "text", "options"):
                if question[field] != support[field]:
                    raise ValueError("Exact catalog/support identity differs")
            if question["content_text"] != question["text"] + " [OPTIONS] " + " | ".join(question["options"]):
                raise ValueError("Ordered content mismatch")
            if support["students"] < 2:
                rejected["fewer_than_two_students"] += 1
                continue
            if BLOCKING_FLAGS.intersection(support["review_flags"]):
                rejected["existing_blocking_flag"] += 1
                continue
            try:
                family, proof = check_question(question)
            except LookupError:
                rejected["requires_independent_content_review"] += 1
                unreviewed.append(support)
                continue
            except (ValueError, SyntaxError, ZeroDivisionError) as exc:
                rejected[str(exc)] += 1
                continue
            item_index = vocab["item_vocab"].get(question["item_id"], 1)
            if (item_index == 1) != (support["regime"] == "cold"):
                raise ValueError("Regime differs from frozen vocabulary")
            accepted.append({"question": {k: question[k] for k in FIELDS},
                             "regime": support["regime"], "family": family,
                             "mathematical_basis": proof,
                             "support": {k: v for k, v in support.items()
                                         if k not in ("text", "options")},
                             "item_index": item_index})
    if identities:
        raise ValueError("Supported rendering absent from catalog")
    required = {v for row in accepted
                for v in (row["question"]["content_text"], *row["question"]["options"])}
    matches = embedding_matches(paths.TEXT_EMBEDDINGS, required)
    compatible = []
    for row in accepted:
        q = row["question"]
        missing = {q["content_text"], *q["options"]} - matches.keys()
        if missing:
            rejected["missing_exact_embedding_text"] += 1
            continue
        row["embedding_rows"] = {
            "content": matches[q["content_text"]],
            "options": [matches[v] for v in q["options"]]}
        compatible.append(row)
    args.out_dir.mkdir(parents=True, exist_ok=False)
    (args.out_dir / "checked_pool_private.json").write_text(
        json.dumps(compatible, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    unreviewed.sort(key=lambda q: (-q["students"], -q["n"], q["skill_id"], q["question_id"]))
    (args.out_dir / "unreviewed_supported_private.json").write_text(
        json.dumps(unreviewed, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    by_skill = defaultdict(list)
    for row in compatible:
        by_skill[(row["question"]["skill_id"], row["regime"])].append(row)
    summary = []
    for skill, name in report["skills"].items():
        record = {"skill_id": skill, "skill_name": name}
        for minimum in SUPPORT_LEVELS:
            for regime in ("warm", "cold"):
                rows = representatives(by_skill[(skill, regime)], minimum)
                record[f"{regime}_prompts_students_ge_{minimum}"] = len(rows)
        summary.append(record)
    summary.sort(key=lambda row: (
        -min(row["warm_prompts_students_ge_2"], row["cold_prompts_students_ge_2"]),
        row["skill_id"]))
    with (args.out_dir / "topic_frontier.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    manifest = {
        "protocol": "conservative_all_skill_supported_mcq_screen_v1",
        "inputs": inputs, "embedding_text_membership_checked": True,
        "embedding_file": {"name": paths.TEXT_EMBEDDINGS.name,
                           "bytes": paths.TEXT_EMBEDDINGS.stat().st_size,
                           "prior_sha256": "8bd32840edb58cc6e27d5e9c0eeb9bf21448952a61480bb4629079cbe9fc6370",
                           "full_hash_rechecked": False},
        "scope": {"skills": len(report["skills"]), "catalog_renderings": catalog_count,
                  "supported_renderings": len(candidates), "accepted_compatible": len(compatible),
                  "rejected_or_unreviewed": dict(rejected)},
        "selection_rule": "Independent explicit-family checks, >=3 substantive choices, >=2 evaluation students, existing blocking flags excluded; no model predictions/outcome-rate selection.",
        "support_levels": list(SUPPORT_LEVELS),
        "limitations": report["limitations"] + [
            "Unsupported families remain unreviewed, not proven unsuitable.",
            "Family checks and prompt uniqueness do not validate distractor quality or curriculum.",
            "No statistical-power guarantee; support cutoffs are descriptive sensitivity levels.",
            "Existing all-skill retained-window support reused; not a fresh raw-history scan.",
            "No mastery, conformal coverage, learner approval or live integration claim."],
        "topic_summary": summary,
    }
    (args.out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest["scope"], indent=2))
    print("TOPIC_FRONTIER", json.dumps(summary[:12], ensure_ascii=True))
    print("PRIVATE_OUTPUTS", args.out_dir)


if __name__ == "__main__":
    main()
