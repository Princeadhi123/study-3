"""Stage 3 (v2): assign item_id / item_instance_id item-slot identifiers
(same scheme as legacy_v1_item_level/build_item_level_kt.py) to the sorted
v2 stream, and additionally build the "distractor catalog" -- a deduplicated
list of every (item slot, offered option, correct?, times chosen) tuple seen
across the whole dataset, restricted to exercise_family in {mcq,
single_answer_text} (the only families with discrete options; see
build_v2_raw_clean.py).

The distractor catalog is NOT a misconception mapping -- no misconception_id
is assigned here. It's the input a human (or an LLM-assisted pass) uses
later to tag each distractor with a misconception_id/misconception_label.
Once that tagging exists as a separate small CSV
(item_id, option_value -> misconception_id), it can be left-joined onto
kt_interactions_v2_item_level.csv.gz's selected_option_value/index without
re-running any of the heavy stages above.
"""
import csv
import gzip
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).parent
DATA = OUT / "data_v2"
REPORTS = OUT / "reports_v2"
SOURCE = DATA / "kt_interactions_v2_sorted.csv.gz"
OUTPUT = DATA / "kt_interactions_v2_item_level.csv.gz"
SAMPLE = DATA / "kt_interactions_v2_item_level_sample.csv"

OUTPUT_FIELDS = [
    "student_id", "timestamp", "item_id", "item_instance_id", "exercise_id",
    "preorder", "skill_id", "skill_name", "exercise_type", "exercise_family",
    "correctness", "correctness_available", "response_time_ms",
    "attempt_number", "text", "exercise_name", "round_id", "round_name",
    "submission_count", "question_idx",
    "has_options", "option_count", "options_json",
    "correct_option_index", "correct_option_value",
    "selected_option_index", "selected_option_value", "answer_raw",
]

OPTION_FAMILIES = {"mcq", "single_answer_text"}


def safe_number(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def make_ids(row):
    exercise_id = row["item_id"]
    preorder = row.get("preorder", "") or "unknown"
    item_id = f"{exercise_id}__p{preorder}"
    question_text = row.get("text", "") or row.get("exercise_name", "")
    question_hash = hashlib.sha1(question_text.encode("utf-8")).hexdigest()[:16]
    item_instance_id = f"{item_id}__q{question_hash}"
    return exercise_id, preorder, item_id, item_instance_id


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    item_stats = defaultdict(lambda: {
        "exercise_id": "", "preorder": "", "skill_ids": set(), "variants": set(),
        "exercise_name": "", "exercise_type": "", "exercise_family": "",
        "interactions": 0, "correct": 0, "correctness_known": 0,
        "response_sum": 0.0, "response_count": 0, "first_timestamp": "", "last_timestamp": "",
    })
    # (item_id, option_value) -> stats, only for option-bearing families
    distractor_stats = defaultdict(lambda: {
        "item_id": "", "exercise_type": "", "option_value": "", "is_correct_option": False,
        "times_offered": 0, "times_selected": 0,
    })
    counters = {"source_rows": 0, "output_rows": 0}

    with gzip.open(SOURCE, "rt", encoding="utf-8-sig", newline="") as source, gzip.open(
        OUTPUT, "wt", encoding="utf-8-sig", newline="", compresslevel=6
    ) as output, SAMPLE.open("w", encoding="utf-8-sig", newline="") as sample_file:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(output, fieldnames=OUTPUT_FIELDS)
        sample_writer = csv.DictWriter(sample_file, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        sample_writer.writeheader()

        for row in reader:
            counters["source_rows"] += 1
            exercise_id, preorder, item_id, item_instance_id = make_ids(row)
            transformed = {
                "student_id": row["student_id"],
                "timestamp": row["timestamp"],
                "item_id": item_id,
                "item_instance_id": item_instance_id,
                "exercise_id": exercise_id,
                "preorder": preorder,
                "skill_id": row["skill_id"],
                "skill_name": row["skill_name"],
                "exercise_type": row["exercise_type"],
                "exercise_family": row["exercise_family"],
                "correctness": row["correctness"],
                "correctness_available": row["correctness_available"],
                "response_time_ms": row["response_time_ms"],
                "attempt_number": row["attempt_number"],
                "text": row["text"],
                "exercise_name": row["exercise_name"],
                "round_id": row["round_id"],
                "round_name": row["round_name"],
                "submission_count": row["submission_count"],
                "question_idx": row["question_idx"],
                "has_options": row["has_options"],
                "option_count": row["option_count"],
                "options_json": row["options_json"],
                "correct_option_index": row["correct_option_index"],
                "correct_option_value": row["correct_option_value"],
                "selected_option_index": row["selected_option_index"],
                "selected_option_value": row["selected_option_value"],
                "answer_raw": row["answer_raw"],
            }
            writer.writerow(transformed)
            if counters["output_rows"] < 10000:
                sample_writer.writerow(transformed)
            counters["output_rows"] += 1

            stat = item_stats[item_id]
            stat["exercise_id"] = exercise_id
            stat["preorder"] = preorder
            stat["skill_ids"].add(row["skill_id"])
            stat["variants"].add(item_instance_id)
            stat["exercise_name"] = row["exercise_name"]
            stat["exercise_type"] = row["exercise_type"]
            stat["exercise_family"] = row["exercise_family"]
            stat["interactions"] += 1
            if row["correctness_available"] == "1":
                stat["correctness_known"] += 1
                stat["correct"] += int(row["correctness"])
            response_time = safe_number(row["response_time_ms"])
            if response_time is not None:
                stat["response_sum"] += response_time
                stat["response_count"] += 1
            timestamp = row["timestamp"]
            stat["first_timestamp"] = min(stat["first_timestamp"] or timestamp, timestamp)
            stat["last_timestamp"] = max(stat["last_timestamp"] or timestamp, timestamp)

            if row["exercise_family"] in OPTION_FAMILIES and row["options_json"] not in ("", "[]"):
                try:
                    options = json.loads(row["options_json"])
                except json.JSONDecodeError:
                    options = []
                selected_value = row["selected_option_value"]
                for option in options:
                    key = (item_id, option["value"])
                    d = distractor_stats[key]
                    d["item_id"] = item_id
                    d["exercise_type"] = row["exercise_type"]
                    d["option_value"] = option["value"]
                    d["is_correct_option"] = d["is_correct_option"] or option["correct"]
                    d["times_offered"] += 1
                    if option["value"] == selected_value:
                        d["times_selected"] += 1

            if counters["output_rows"] % 3_000_000 == 0:
                print(f"...item-level pass: {counters['output_rows']:,} rows", flush=True)

    item_rows = []
    for item_id, stat in item_stats.items():
        item_rows.append({
            "item_id": item_id,
            "exercise_id": stat["exercise_id"],
            "preorder": stat["preorder"],
            "exercise_name": stat["exercise_name"],
            "exercise_type": stat["exercise_type"],
            "exercise_family": stat["exercise_family"],
            "skill_count": len(stat["skill_ids"]),
            "question_variant_count": len(stat["variants"]),
            "interactions": stat["interactions"],
            "correctness_known": stat["correctness_known"],
            "correct": stat["correct"],
            "accuracy": stat["correct"] / stat["correctness_known"] if stat["correctness_known"] else "",
            "mean_response_time_ms": stat["response_sum"] / stat["response_count"] if stat["response_count"] else "",
            "first_timestamp": stat["first_timestamp"],
            "last_timestamp": stat["last_timestamp"],
        })
    item_rows.sort(key=lambda row: row["item_id"])
    write_csv(
        REPORTS / "item_summary_v2.csv",
        [
            "item_id", "exercise_id", "preorder", "exercise_name", "exercise_type", "exercise_family",
            "skill_count", "question_variant_count", "interactions", "correctness_known", "correct",
            "accuracy", "mean_response_time_ms", "first_timestamp", "last_timestamp",
        ],
        item_rows,
    )

    distractor_rows = []
    for (item_id, option_value), d in distractor_stats.items():
        distractor_rows.append({
            "item_id": d["item_id"],
            "exercise_type": d["exercise_type"],
            "option_value": d["option_value"],
            "is_correct_option": int(d["is_correct_option"]),
            "times_offered": d["times_offered"],
            "times_selected": d["times_selected"],
            "selection_rate": d["times_selected"] / d["times_offered"] if d["times_offered"] else "",
            # Left blank for later manual/LLM annotation. Fill in and re-join
            # onto kt_interactions_v2_item_level.csv.gz by (item_id, option_value)
            # -- no need to re-run stages 1-3 to add misconception tags.
            "misconception_id": "",
            "misconception_label": "",
        })
    # Most useful ordering for a human/LLM tagging pass: wrong options first,
    # most frequently chosen first (biggest bang-for-buck to tag).
    distractor_rows.sort(key=lambda r: (r["is_correct_option"], -r["times_selected"]))
    write_csv(
        REPORTS / "distractor_catalog.csv",
        ["item_id", "exercise_type", "option_value", "is_correct_option",
         "times_offered", "times_selected", "selection_rate",
         "misconception_id", "misconception_label"],
        distractor_rows,
    )

    report = {
        "source_file": str(SOURCE),
        "output_file": str(OUTPUT),
        "source_rows": counters["source_rows"],
        "output_rows": counters["output_rows"],
        "independent_item_count": len(item_stats),
        "exact_question_instance_count": sum(len(stat["variants"]) for stat in item_stats.values()),
        "distractor_catalog_rows": len(distractor_rows),
        "distractor_catalog_wrong_options": sum(1 for r in distractor_rows if not r["is_correct_option"]),
        "item_id_definition": "ExerciseId + PreOrd",
        "item_instance_id_definition": "ExerciseId + PreOrd + SHA1(text) prefix",
        "chronological_order_preserved": True,
        "notes": [
            "item_id is the independent item slot used for baseline KT.",
            "item_instance_id distinguishes different generated question texts within the same item slot.",
            "distractor_catalog.csv (reports_v2/) lists every distinct option ever offered for mcq/"
            "single_answer_text items, with how often it was offered vs. actually chosen. "
            "misconception_id/misconception_label columns are intentionally empty -- fill them in "
            "(manually, or via an LLM-assisted pass over option_value + item_id) and join back onto "
            "the interaction file by (item_id, selected_option_value) to add misconception-aware "
            "supervision without re-running stages 1-3.",
        ],
    }
    (REPORTS / "item_level_report_v2.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
