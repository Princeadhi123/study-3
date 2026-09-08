import csv
import gzip
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).parent
DATA = OUT / "data"
REPORTS = OUT / "reports"
SOURCE = DATA / "kt_interactions_sorted.csv.gz"
OUTPUT = DATA / "kt_interactions.csv.gz"
SAMPLE = DATA / "kt_interactions_item_level_sample.csv"

OUTPUT_FIELDS = [
    "student_id", "timestamp", "item_id", "item_instance_id", "exercise_id",
    "preorder", "skill_id", "skill_name", "correctness", "response_time_ms",
    "attempt_number", "text", "exercise_name", "exercise_type", "round_id",
    "round_name", "submission_count", "question_idx",
]


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
        "exercise_name": "", "exercise_type": "", "interactions": 0, "correct": 0,
        "response_sum": 0.0, "response_count": 0, "first_timestamp": "", "last_timestamp": "",
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
                "correctness": row["correctness"],
                "response_time_ms": row["response_time_ms"],
                "attempt_number": row["attempt_number"],
                "text": row["text"],
                "exercise_name": row["exercise_name"],
                "exercise_type": row["exercise_type"],
                "round_id": row["round_id"],
                "round_name": row["round_name"],
                "submission_count": row["submission_count"],
                "question_idx": row["question_idx"],
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
            stat["interactions"] += 1
            stat["correct"] += int(row["correctness"])
            response_time = safe_number(row["response_time_ms"])
            if response_time is not None:
                stat["response_sum"] += response_time
                stat["response_count"] += 1
            timestamp = row["timestamp"]
            stat["first_timestamp"] = min(stat["first_timestamp"] or timestamp, timestamp)
            stat["last_timestamp"] = max(stat["last_timestamp"] or timestamp, timestamp)

    item_rows = []
    for item_id, stat in item_stats.items():
        item_rows.append({
            "item_id": item_id,
            "exercise_id": stat["exercise_id"],
            "preorder": stat["preorder"],
            "exercise_name": stat["exercise_name"],
            "exercise_type": stat["exercise_type"],
            "skill_count": len(stat["skill_ids"]),
            "question_variant_count": len(stat["variants"]),
            "interactions": stat["interactions"],
            "correct": stat["correct"],
            "accuracy": stat["correct"] / stat["interactions"],
            "mean_response_time_ms": stat["response_sum"] / stat["response_count"] if stat["response_count"] else "",
            "first_timestamp": stat["first_timestamp"],
            "last_timestamp": stat["last_timestamp"],
        })
    item_rows.sort(key=lambda row: row["item_id"])
    write_csv(
        REPORTS / "item_summary.csv",
        [
            "item_id", "exercise_id", "preorder", "exercise_name", "exercise_type",
            "skill_count", "question_variant_count", "interactions", "correct", "accuracy",
            "mean_response_time_ms", "first_timestamp", "last_timestamp",
        ],
        item_rows,
    )

    report = {
        "source_file": str(SOURCE),
        "output_file": str(OUTPUT),
        "source_rows": counters["source_rows"],
        "output_rows": counters["output_rows"],
        "independent_item_count": len(item_stats),
        "exact_question_instance_count": sum(len(stat["variants"]) for stat in item_stats.values()),
        "item_id_definition": "ExerciseId + PreOrd",
        "item_instance_id_definition": "ExerciseId + PreOrd + SHA1(text) prefix",
        "chronological_order_preserved": True,
        "notes": [
            "item_id is the independent item slot used for baseline KT.",
            "item_instance_id distinguishes different generated question texts within the same item slot.",
            "The source was already sorted by student and timestamp, so the transformed output preserves that order.",
        ],
    }
    (REPORTS / "item_level_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
