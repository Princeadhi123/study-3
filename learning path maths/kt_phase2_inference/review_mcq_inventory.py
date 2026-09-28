"""Audit the private MCQ inventory for review risks, never approve questions."""
import argparse
import gzip
import json
import re
from collections import Counter
from pathlib import Path

import paths

VISUAL = re.compile(
    r"\b(?:kuva\w*|kuvassa|kuvan|oheis\w*|viereis\w*|vasemmalla|"
    r"alla\s+olev\w*|yl[äa]puolell\w*|geogebra|applet\w*|"
    r"tauluk\w*|kaavio\w*|graaf\w*|piirrok\w*|"
    r"ympyr[äa]\s+[a-z]\b|kulma\s+[a-z]\b|piste\s+[a-z]\b|"
    r"alue\w*\s+[a-z0-9]\b)\b", re.IGNORECASE)
MARKUP = re.compile(r"<(?:img|svg|video|audio)\b|\\(?:frac|dfrac|sqrt|Large)\b|\$|https?://", re.IGNORECASE)


def normalized(value):
    return " ".join(value.split()).casefold()


def visible_key(q):
    return normalized(q["text"]), tuple(sorted(normalized(v) for v in q["options"]))


def review_flags(q, conflicting=False):
    text = normalized(q["text"])
    flags = []
    if VISUAL.search(text):
        flags.append("possible_missing_visual_or_external_context")
    if len(text) < 35 or text.startswith(("valitse oikea vaihtoehto", "geometria:", "heittää nopalla")):
        flags.append("underspecified_prompt_heuristic")
    if MARKUP.search(q["text"]) or any(MARKUP.search(v) for v in q["options"]):
        flags.append("markup_or_embedded_media_render_check")
    if len(q["options"]) == 2:
        flags.append("two_choice_guessing_risk")
    if normalized(q["skill_name"]) in {"klikkaa tästä", "click here"}:
        flags.append("nonskill_label")
    if conflicting:
        flags.append("same_visible_content_conflicting_answer_keys")
    return flags


def audit(inventory: Path, output: Path, report_path: Path) -> dict:
    keys = {}
    conflicting = set()
    total = 0
    with gzip.open(inventory, "rt", encoding="utf-8") as stream:
        for line in stream:
            q = json.loads(line)
            key = visible_key(q)
            answer = normalized(q["options"][q["answer_index"]])
            previous = keys.setdefault(key, answer)
            if previous != answer:
                conflicting.add(key)
            total += 1
    counts = Counter()
    with gzip.open(inventory, "rt", encoding="utf-8") as source, gzip.open(
            output, "xt", encoding="utf-8") as dest:
        for line in source:
            q = json.loads(line)
            flags = review_flags(q, visible_key(q) in conflicting)
            if flags:
                counts.update(flags)
                counts["flagged_questions"] += 1
            dest.write(json.dumps({"question_id": q["question_id"],
                                   "skill_id": q["skill_id"], "flags": flags},
                                  ensure_ascii=False) + "\n")
    report = {
        "status": "automated_triage_only; zero questions approved",
        "source": str(inventory),
        "audited_questions": total,
        "flagged_questions": counts["flagged_questions"],
        "flag_counts_overlapping": {k: counts[k] for k in sorted(counts)
                                    if k != "flagged_questions"},
        "distinct_visible_content_groups_with_conflicting_keys": len(conflicting),
        "interpretation": "Flags are conservative review leads, not proven errors; unflagged is not verified correct. Identical exported text/options may conceal differing images. Source contains no media rendering, so mathematical correctness, equivalence, and skill alignment cannot be certified by this audit. Keep keys and artifacts private; require original rendered content and educator review.",
    }
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return report


def verify_review(inventory: Path, flags_path: Path, report_path: Path,
                  bank_path: Path, manual_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    counts = Counter()
    with gzip.open(inventory, "rt", encoding="utf-8") as source, gzip.open(
            flags_path, "rt", encoding="utf-8") as flags:
        for line in source:
            flag_line = flags.readline()
            if not flag_line:
                raise ValueError("Flags end before inventory")
            q, reviewed = json.loads(line), json.loads(flag_line)
            if (q["question_id"], q["skill_id"]) != (reviewed["question_id"], reviewed["skill_id"]):
                raise ValueError("Flags do not match inventory order")
            counts["audited"] += 1
            if reviewed["flags"]:
                counts["flagged"] += 1
            counts.update(reviewed["flags"])
        if flags.readline():
            raise ValueError("Flags exceed inventory")
    if (counts["audited"] != report["audited_questions"]
            or counts["flagged"] != report["flagged_questions"]
            or {k: counts[k] for k in report["flag_counts_overlapping"]}
            != report["flag_counts_overlapping"]):
        raise ValueError("Audit report disagrees with row flags")
    bank = json.loads(bank_path.read_text(encoding="utf-8"))
    manual = json.loads(manual_path.read_text(encoding="utf-8"))
    items = manual["items"]
    if (len(items) != 40 or manual["approved"] != 0
            or len(bank["questions"]) != 40
            or any(item["position"] != i or item["exercise_id"] != q["exercise_id"]
                   or not item["status"] or not item["reason"]
                   for i, (item, q) in enumerate(zip(items, bank["questions"]), 1))):
        raise ValueError("Manual 40-question review is incomplete or mismatched")
    return dict(Counter(item["status"] for item in items))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path,
                        default=paths.ARTIFACTS / "mcq_inventory_private.jsonl.gz")
    parser.add_argument("--out", type=Path,
                        default=paths.ARTIFACTS / "mcq_review_flags_private.jsonl.gz")
    parser.add_argument("--report", type=Path,
                        default=paths.ARTIFACTS / "mcq_review_report.json")
    parser.add_argument("--bank", type=Path,
                        default=paths.ARTIFACTS / "test_question_bank.json")
    parser.add_argument("--manual", type=Path,
                        default=paths.ARTIFACTS / "mcq_40_review.json")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        statuses = verify_review(*(paths.require(p) for p in (
            args.inventory, args.out, args.report, args.bank, args.manual)))
        print(f"Verified audit and all 40 manual review notes: {statuses}. Approved: 0")
        return
    if args.out.exists() or args.report.exists():
        raise FileExistsError("Review output or report already exists; refusing to overwrite")
    report = audit(paths.require(args.inventory), args.out, args.report)
    print(f"Audited {report['audited_questions']:,} unreviewed questions; "
          f"flagged {report['flagged_questions']:,} for triage. Approved: 0")


if __name__ == "__main__":
    main()
