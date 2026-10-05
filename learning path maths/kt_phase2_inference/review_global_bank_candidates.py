"""Close the content review of every source skill that can supply both banks.

Source skills unable to supply ten distinct prompts in either regime are
pruned by an availability upper bound, not by a quality assumption.
"""
import argparse
import gzip
import json
import re
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path

import paths
from audit_stronger_banks import BAD_OPTIONS, check_question, checked_numeric, fingerprint
from build_evaluation_banks import embedding_matches
from build_text_only_bank import BLOCKING_FLAGS, FIELDS
from review_mcq_inventory import normalized

ALIGNED_FAMILIES = {
    "skill_3697ce51cadb": {"percent_of"},
    "skill_b986b3d2a5d8": {"linear_simplification"},
    "skill_8aaddb61bde0": {"divisibility", "prime_identification"},
    "skill_e8d49a51337a": {"fraction_arithmetic"},
    "skill_d65977bf1b26": {"fraction_arithmetic"},
    "skill_5f921ad8dd86": set(),
}
DRAGON_QUARTER = (
    "Norjalaiset lohik\u00e4\u00e4rmeet harrastavat silmukoiden tekemist\u00e4 "
    "ilmassa. Nuoremmat lohik\u00e4\u00e4rmeet osaavat tehd\u00e4 per\u00e4kk\u00e4isi\u00e4 "
    "silmukoita luonnollisesti vanhempia enemm\u00e4n. Suurin per\u00e4kk\u00e4isten "
    "silmukoiden m\u00e4\u00e4r\u00e4 on 28. Vanhimmat lohik\u00e4\u00e4rmeet tekev\u00e4t "
    "silmukoita parhaimmillaan vain nelj\u00e4sosan parhaasta tuloksesta. Kuinka "
    "monta silmukkaa vanhat lohik\u00e4\u00e4rmeet tekev\u00e4t per\u00e4kk\u00e4in parhaimmillaan?")


def broad_eligible(row):
    opts = [normalized(v) for v in row["options"]]
    return (row["students"] >= 2 and not BLOCKING_FLAGS.intersection(row["review_flags"])
            and len(opts) >= 3 and len(set(opts)) == len(opts)
            and not set(opts) & BAD_OPTIONS)


def closed_review(question):
    """Return an independent check or an explicit, lead-reviewed exclusion."""
    skill = question["skill_id"]
    text = " ".join(question["text"].replace("\\n", " ").split())
    try:
        family, proof = check_question(question)
    except LookupError:
        family = None
    if family:
        if family not in ALIGNED_FAMILIES[skill]:
            return None, "Calculation family does not match the source topic."
        return {"family": family, "mathematical_basis": proof}, None
    if skill in ("skill_e8d49a51337a", "skill_d65977bf1b26"):
        match = re.fullmatch(r"Ota (\d+)\u2044(\d+) luvusta (\d+)", text)
        if match:
            # Mixed-number formatting was flattened in these exports; do not
            # guess whether e.g. 21/3 denotes 21/3 or the mixed number 2 1/3.
            if any(re.search(r"\d{2,}\u2044\d+", v) for v in question["options"]):
                return None, "Ambiguous concatenated mixed-number option rendering."
            value = Fraction(int(match[1]), int(match[2])) * int(match[3])
            family, proof = checked_numeric(question, value, "fraction_of_quantity")
            return {"family": family, "mathematical_basis": proof}, None
        if text == DRAGON_QUARTER:
            family, proof = checked_numeric(question, Fraction(28, 4), "fraction_word_problem")
            return {"family": family, "mathematical_basis": proof}, None
        if text.startswith(("Lohik\u00e4\u00e4rme munii", "Lohik\u00e4\u00e4rmeill\u00e4",
                            "Lohik\u00e4\u00e4rmekesteille")):
            return None, "Fraction/ratio operand absent from exported word problem."
    if skill == "skill_3697ce51cadb" and text.startswith("Ohessa on siis nelj\u00e4ll\u00e4"):
        return None, "Divisibility question, not percentage content; referenced list absent."
    if skill == "skill_8aaddb61bde0":
        probability_starts = (
            "Nopalla", "Kahdella nopalla", "Yhdell\u00e4 nopalla", "Jalkapallo",
            "Kun heit\u00e4t noppaa", "Voiko samassa paikassa", "Tikanheitossa")
        if text.startswith(probability_starts):
            return None, "Probability/possibility task, not divisibility or prime content."
    if skill == "skill_b986b3d2a5d8":
        if text.startswith(("Miisa", "Marja", "Mill\u00e4 todenn\u00e4k\u00f6isyydell\u00e4")):
            return None, "Probability task lacks necessary context and is off-topic."
        if "Mink\u00e4laisen lausekkeen teht\u00e4v\u00e4st\u00e4 voi muodostaa?" in text:
            return None, "Original word problem needed to form expression is absent."
        if "Mik\u00e4 on siis" in text:
            return None, "Final answer is already supplied in the worked stem."
        if re.match(r"[123]\. teht\u00e4v\u00e4:", text):
            return None, "Exported equation chain has missing operands/unfinished placeholders."
        if text.endswith("Miten lasku on laskettu?"):
            return None, "Worked numeric/distributivity explanation, not combining-like-terms target."
    if skill == "skill_5f921ad8dd86":
        angle_types = {
            "nollakulma": lambda value: value == 0,
            "suora kulma": lambda value: value == 90,
            "oikokulma": lambda value: value == 180,
            "t\u00e4ysi kulma": lambda value: value == 360,
            "ter\u00e4v\u00e4 kulma": lambda value: 0 < value < 90,
            "tylpp\u00e4 kulma": lambda value: 90 < value < 180,
        }
        if text in angle_types:
            if not all(re.fullmatch(r"\d+\u00b0", v) for v in question["options"]):
                raise ValueError("Unexpected angle-option rendering")
            values = [int(v[:-1]) for v in question["options"]]
            matches = [i for i, v in enumerate(values) if angle_types[text](v)]
            if len(set(values)) != len(values) or len(matches) != 1:
                raise ValueError("Angle concept has no unique offered answer")
            if matches[0] != question["answer_index"]:
                raise ValueError("Independent angle check contradicts source key")
            return {"family": "angle_type",
                    "mathematical_basis": f"Standard angle definition selects {values[matches[0]]} degrees."}, None
        if re.fullmatch(r"\u03b1 = \d+\u00b0, \u03b2 = \d+\u00b0, \u03b3 = \?", text):
            return None, "Relationship of alpha/beta/gamma is unspecified without geometry."
        if ("punai" in text or "sinis" in text or "sinin" in text or "Kuvassa" in text
                or text == "Geometria: perusk\u00e4sitteet"):
            return None, "Missing colored/reference geometry needed for the question."
    raise LookupError(f"Unresolved stem in feasible source skill {skill}: {text}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.out_dir.exists():
        raise FileExistsError("Refusing to overwrite closed review")
    availability = paths.ARTIFACTS / "evaluation_candidate_inventory_all_skills_20261004" / "availability.json"
    inventory = paths.ARTIFACTS / "mcq_inventory_private.jsonl.gz"
    report = json.loads(availability.read_text(encoding="utf-8"))
    prior = json.loads((paths.ARTIFACTS / "stronger_bank_audit_20261005" / "manifest.json").read_text(encoding="utf-8"))
    for key, path in (("availability", availability), ("inventory", inventory), ("vocab", paths.VOCAB)):
        if fingerprint(path) != prior["inputs"][key]:
            raise ValueError("Frozen search input changed")
    candidates = report["warm_candidates"] + report["cold_candidates"]
    broad = [r for r in candidates if broad_eligible(r)]
    prompts = defaultdict(set)
    for row in broad:
        prompts[(row["skill_id"], row["regime"])].add(normalized(row["text"]))
    upper_bounds = [
        {"skill_id": s, "skill_name": name,
         **{reg: len(prompts[(s, reg)]) for reg in ("warm", "cold")}}
        for s, name in report["skills"].items()]
    feasible = {r["skill_id"] for r in upper_bounds if min(r["warm"], r["cold"]) >= 10}
    if feasible != set(ALIGNED_FAMILIES):
        raise ValueError("Capacity frontier differs; lead must review newly feasible topics")
    wanted = {(r["skill_id"], r["question_id"]): r for r in broad if r["skill_id"] in feasible}
    accepted, ledger = [], []
    with gzip.open(inventory, "rt", encoding="utf-8") as stream:
        for line in stream:
            q = json.loads(line)
            row = wanted.pop((q["skill_id"], q["question_id"]), None)
            if row is None:
                continue
            if any(q[k] != row[k] for k in ("text", "options", "item_id")):
                raise ValueError("Catalog/support mismatch")
            try:
                checked, reason = closed_review(q)
            except (ValueError, SyntaxError, ZeroDivisionError) as exc:
                checked, reason = None, "Independent content check rejected: " + str(exc)
            item = {"question": {k: q[k] for k in FIELDS}, "regime": row["regime"],
                    "support": row, "status": "accepted" if checked else "excluded",
                    "reason": reason}
            if checked:
                item.update(checked)
                accepted.append(item)
            ledger.append(item)
    if wanted:
        raise ValueError("Supported source renderings absent from inventory")
    required = {v for row in accepted for v in (
        row["question"]["content_text"], *row["question"]["options"])}
    matches = embedding_matches(paths.TEXT_EMBEDDINGS, required)
    compatible = []
    vocab = json.loads(paths.VOCAB.read_text(encoding="utf-8"))
    for row in accepted:
        q = row["question"]
        if (vocab["item_vocab"].get(q["item_id"], 1) == 1) != (row["regime"] == "cold"):
            raise ValueError("Frozen item regime changed")
        missing = {q["content_text"], *q["options"]} - matches.keys()
        if missing:
            row.update(status="excluded", reason="Missing exact embedding text")
        else:
            row["embedding_rows"] = {"content": matches[q["content_text"]],
                                     "options": [matches[v] for v in q["options"]]}
            compatible.append(row)
    args.out_dir.mkdir(parents=True, exist_ok=False)
    for name, value in (("review_ledger_private.json", ledger),
                        ("closed_pool_private.json", compatible),
                        ("source_skill_capacity_bounds.json", upper_bounds)):
        (args.out_dir / name).write_text(
            json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "scope": "All 560 source skills in frozen calibration-excluded support; 40 questions per regime, same four source topics, ten distinct normalized prompts per topic, >=3 substantive options, >=2 students per exact rendering, stand-alone topic-aligned content.",
        "inputs": {key: fingerprint(path) for key, path in (
            ("availability", availability), ("inventory", inventory), ("vocab", paths.VOCAB),
            ("method", Path(__file__)))},
        "capacity_feasible_source_skills": sorted(feasible),
        "pruned_source_skills": len(upper_bounds) - len(feasible),
        "reviewed_exact_renderings": len(ledger),
        "accepted_compatible_renderings": len(compatible),
        "unresolved_renderings_in_capacity_feasible_skills": 0,
        "excluded_reasons": dict(Counter(r["reason"] for r in ledger if r["status"] == "excluded")),
        "limitations": [
            "Optimality is conditional on declared eligibility and source-skill topic design.",
            "Geometric media and malformed exports are not silently reconstructed.",
            "Source skill labels are retained; reviewed alignment is not curriculum certification.",
            "Existing support capture is reused; no new student/session evidence or model results.",
            "These are not approved student assessments or KT/conformal validation."],
    }
    (args.out_dir / "review_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
