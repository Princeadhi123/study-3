import csv
import gzip
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "mp_grade_6th_2025_7th_2026_spring.csv"
OUT = Path(__file__).parent
DATA = OUT / "data"
REPORTS = OUT / "reports"

OUTPUT_FIELDS = [
    "student_id", "timestamp", "item_id", "skill_id", "skill_name",
    "correctness", "response_time_ms", "task_time_ms", "attempt_number",
    "text", "exercise_name", "exercise_type", "round_id", "round_name",
    "submission_count", "question_idx", "preorder",
]


def clean_number(value, integer=False):
    if value in (None, "", "NULL", "None"):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number) or number < 0:
        return ""
    if integer:
        return str(int(number)) if number.is_integer() else ""
    return str(int(number)) if number.is_integer() else str(number)


def valid_timestamp(value):
    if not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def make_skill_id(round_name):
    normalized = re.sub(r"\s+", " ", round_name.strip().casefold())
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"skill_{digest}"


def write_summary(path, fieldnames, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    DATA.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)

    counters = Counter()
    type_counts = Counter()
    type_valid_counts = Counter()
    skill_meta = {}
    student_meta = defaultdict(lambda: {
        "interactions": 0, "correct": 0, "first_timestamp": "", "last_timestamp": "",
        "items": set(), "skills": set(),
    })
    item_meta = defaultdict(lambda: {
        "item_id": "", "item_name": "", "exercise_type": "", "interactions": 0,
        "correct": 0, "students": set(), "skills": set(), "response_time_sum": 0.0,
        "response_time_count": 0,
    })
    skill_stats = defaultdict(lambda: {
        "skill_name": "", "interactions": 0, "correct": 0, "students": set(), "items": set(),
    })

    output_path = DATA / "kt_interactions.csv.gz"
    sample_path = DATA / "kt_interactions_sample.csv"
    with SOURCE.open("r", encoding="utf-8-sig", newline="") as source, gzip.open(
        output_path, "wt", encoding="utf-8-sig", newline="", compresslevel=6
    ) as compressed, sample_path.open("w", encoding="utf-8-sig", newline="") as sample_file:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(compressed, fieldnames=OUTPUT_FIELDS)
        sample_writer = csv.DictWriter(sample_file, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        sample_writer.writeheader()

        for row in reader:
            counters["source_rows"] += 1
            exercise_type = row.get("ExerciseTypeEnum", "").strip()
            type_counts[exercise_type] += 1

            student_id = row.get("IDCode", "").strip()
            timestamp = row.get("SubmissionTimestamp", "").strip()
            item_id = row.get("ExerciseId", "").strip()
            round_name = row.get("RoundName", "").strip()
            correctness_raw = row.get("Correctness", "").strip()

            if not student_id:
                counters["dropped_missing_student_id"] += 1
                continue
            if not valid_timestamp(timestamp):
                counters["dropped_invalid_timestamp"] += 1
                continue
            if not item_id:
                counters["dropped_missing_item_id"] += 1
                continue
            if not round_name:
                counters["dropped_missing_skill"] += 1
                continue
            if correctness_raw not in {"0", "1"}:
                counters["dropped_invalid_correctness"] += 1
                continue

            correctness = int(correctness_raw)
            skill_id = make_skill_id(round_name)
            text = row.get("Question", "") or row.get("ExerciseName", "")
            output = {
                "student_id": student_id,
                "timestamp": timestamp,
                "item_id": item_id,
                "skill_id": skill_id,
                "skill_name": round_name,
                "correctness": correctness,
                "response_time_ms": clean_number(row.get("TimeMs")),
                "task_time_ms": clean_number(row.get("SubmissionTimeOnTaskMs")),
                "attempt_number": clean_number(row.get("AttemptNumber"), integer=True),
                "text": text,
                "exercise_name": row.get("ExerciseName", ""),
                "exercise_type": exercise_type,
                "round_id": row.get("RoundID", ""),
                "round_name": round_name,
                "submission_count": clean_number(row.get("Submission"), integer=True),
                "question_idx": row.get("QuestionIdx", ""),
                "preorder": row.get("PreOrd", ""),
            }
            writer.writerow(output)
            if counters["valid_rows"] < 10000:
                sample_writer.writerow(output)

            counters["valid_rows"] += 1
            type_valid_counts[exercise_type] += 1
            student = student_meta[student_id]
            student["interactions"] += 1
            student["correct"] += correctness
            student["first_timestamp"] = min(student["first_timestamp"] or timestamp, timestamp)
            student["last_timestamp"] = max(student["last_timestamp"] or timestamp, timestamp)
            student["items"].add(item_id)
            student["skills"].add(skill_id)

            item = item_meta[item_id]
            item["item_id"] = item_id
            item["item_name"] = row.get("ExerciseName", "")
            item["exercise_type"] = exercise_type
            item["interactions"] += 1
            item["correct"] += correctness
            item["students"].add(student_id)
            item["skills"].add(skill_id)
            response_time = clean_number(row.get("TimeMs"))
            if response_time:
                item["response_time_sum"] += float(response_time)
                item["response_time_count"] += 1

            skill = skill_stats[skill_id]
            skill["skill_name"] = round_name
            skill["interactions"] += 1
            skill["correct"] += correctness
            skill["students"].add(student_id)
            skill["items"].add(item_id)
            skill_meta[skill_id] = round_name

    student_rows = []
    for student_id, value in student_meta.items():
        student_rows.append({
            "student_id": student_id,
            "interactions": value["interactions"],
            "unique_items": len(value["items"]),
            "unique_skills": len(value["skills"]),
            "correct": value["correct"],
            "accuracy": value["correct"] / value["interactions"],
            "first_timestamp": value["first_timestamp"],
            "last_timestamp": value["last_timestamp"],
        })
    write_summary(
        REPORTS / "student_summary.csv",
        ["student_id", "interactions", "unique_items", "unique_skills", "correct", "accuracy", "first_timestamp", "last_timestamp"],
        student_rows,
    )

    item_rows = []
    for item_id, value in item_meta.items():
        item_rows.append({
            "item_id": item_id,
            "item_name": value["item_name"],
            "exercise_type": value["exercise_type"],
            "interactions": value["interactions"],
            "unique_students": len(value["students"]),
            "unique_skills": len(value["skills"]),
            "correct": value["correct"],
            "accuracy": value["correct"] / value["interactions"],
            "mean_response_time_ms": value["response_time_sum"] / value["response_time_count"] if value["response_time_count"] else "",
        })
    write_summary(
        REPORTS / "item_summary.csv",
        ["item_id", "item_name", "exercise_type", "interactions", "unique_students", "unique_skills", "correct", "accuracy", "mean_response_time_ms"],
        item_rows,
    )

    skill_rows = []
    for skill_id, value in skill_stats.items():
        skill_rows.append({
            "skill_id": skill_id,
            "skill_name": value["skill_name"],
            "interactions": value["interactions"],
            "unique_students": len(value["students"]),
            "unique_items": len(value["items"]),
            "correct": value["correct"],
            "accuracy": value["correct"] / value["interactions"],
        })
    write_summary(
        REPORTS / "skill_summary.csv",
        ["skill_id", "skill_name", "interactions", "unique_students", "unique_items", "correct", "accuracy"],
        skill_rows,
    )

    report = {
        "source_file": str(SOURCE),
        "output_file": str(output_path),
        "output_columns": OUTPUT_FIELDS,
        "source_rows": counters["source_rows"],
        "valid_rows": counters["valid_rows"],
        "dropped_rows": counters["source_rows"] - counters["valid_rows"],
        "drop_counts": {key: value for key, value in counters.items() if key.startswith("dropped_")},
        "unique_students": len(student_meta),
        "unique_items": len(item_meta),
        "unique_skills": len(skill_stats),
        "valid_rows_by_exercise_type": dict(type_valid_counts),
        "source_rows_by_exercise_type": dict(type_counts),
        "response_time_missing_rows": None,
        "notes": [
            "Repeated attempts are retained because they are meaningful for knowledge tracing.",
            "Rows with correctness values other than exactly 0 or 1 are excluded from the supervised KT file.",
            "skill_id is a stable hash of normalized RoundName; skill_name retains the original topic text.",
            "response_time_ms comes from TimeMs; task_time_ms preserves SubmissionTimeOnTaskMs separately.",
            "The text field is the original Question field, with ExerciseName as fallback.",
        ],
    }
    report_path = REPORTS / "cleaning_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "source_rows": counters["source_rows"],
        "valid_rows": counters["valid_rows"],
        "dropped_rows": counters["source_rows"] - counters["valid_rows"],
        "unique_students": len(student_meta),
        "unique_items": len(item_meta),
        "unique_skills": len(skill_stats),
        "output": str(output_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
