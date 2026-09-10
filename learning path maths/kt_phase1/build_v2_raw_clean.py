"""Stage 1 (v2): clean the raw export and extract question + options + the
student's actual answer for EVERY exercise type, not just multiple-choice.

Why v2 exists: the v1 pipeline (kt_phase1/legacy_v1_item_level/build_phase1_kt.py)
kept only `text` (the question) and dropped `PossibleAnswersJson` /
`AnswerJson` entirely, so no KT variant could ever see what options were
offered or what the student actually picked. v2 keeps both, raw and parsed.

What the raw data actually looks like (see kt_phase1/legacy_v1_item_level/
analyze_types.py and analyze_correctness.py for the full per-type audit this
is based on):

  - Only 3 of 54 ExerciseTypeEnum values are genuine multiple-choice with
    per-option correctness in PossibleAnswersJson: MATH_DRILLER (30% of all
    rows), VILLE_QUIZ (13%), VOICE_DRILLER (tiny). These get fully parsed
    options + a matched selected-option index -> ready for an option-aware
    / (later) misconception-aware KT variant.
  - CROSSWORD_PUZZLE offers exactly one "option" (the known-correct string)
    -- it's a graded fill-in, not a real choice. Parsed as a 1-option case.
  - The other ~50 types are constructed-response (numeric entry, matching,
    sorting, area clicks, sequence building, ...). They have no discrete
    option set, so `has_options=False`, but the student's raw answer
    (AnswerJson) is ALWAYS preserved verbatim in `answer_raw` -- nothing is
    thrown away, later per-type parsing can build on top of this.
  - A handful of types (MATSEL, ARRANGE_SENTENCES, MATCH_OBJECTS,
    MATCH_PAIRS, CLASSIFICATION, CARDS_GAME, GENERAL_SORTING, MCWI_EXERCISE,
    SELECT_WORDS, SURVEY, IMAGE_CLASSIFICATION) NEVER populate `Correctness`
    (~4.9% of rows). These rows are kept (question/answer still useful
    context) but flagged `correctness_available=False` so KT training code
    can exclude them from the supervised target while still optionally
    using them as sequence context.

Output columns (OUTPUT_FIELDS below) are a superset of the v1 schema, so
existing downstream code that only reads the old columns keeps working.
"""
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
SOURCE_GZ = ROOT / "mp_grade_6th_2025_7th_2026_spring.csv.gz"
SOURCE_PLAIN = ROOT / "mp_grade_6th_2025_7th_2026_spring.csv"
OUT = Path(__file__).parent
DATA = OUT / "data_v2"
REPORTS = OUT / "reports_v2"

OUTPUT_FIELDS = [
    "student_id", "timestamp", "item_id", "skill_id", "skill_name",
    "exercise_type", "exercise_family",
    "correctness", "correctness_available",
    "response_time_ms", "task_time_ms", "attempt_number",
    "text", "exercise_name", "round_id", "round_name",
    "submission_count", "question_idx", "preorder",
    "has_options", "option_count",
    "options_json", "correct_option_index", "correct_option_value",
    "selected_option_index", "selected_option_value",
    "answer_raw",
]

# ---- exercise-type -> family taxonomy (see module docstring) ----
MCQ_TYPES = {"MATH_DRILLER", "VILLE_QUIZ", "VOICE_DRILLER"}
SINGLE_ANSWER_TEXT_TYPES = {"CROSSWORD_PUZZLE"}
MATCHING_TYPES = {"MATCH_PAIRS", "MATCH_OBJECTS"}
ORDERING_SORTING_TYPES = {
    "GENERAL_SORTING", "ARRANGE_SENTENCES", "NUMBER_COMPOSITION",
    "MATH_CONTINUE_NL", "SHAPE_CALCULATION_MATH_LAYOUT",
}
SPATIAL_TYPES = {
    "IDENTIFY_AREAS", "MATH_GEOMETRY", "MATH_DRAW", "IMAGE_GRID",
    "IMAGE_CLASSIFICATION", "IDENT_PICTURE",
}
CLASSIFICATION_SELECTION_TYPES = {
    "CLASSIFICATION", "SELECT_WORDS", "MCWI_EXERCISE", "MATSEL",
}
TIME_TYPES = {"CLOCKTIME", "CLOCK_TRANSLATE"}
NUMERIC_OR_TEXT_ENTRY_TYPES = {
    "MATH_CALCULATION_ORDER", "MATH_SYMBOLIC_EXER", "RUNNER",
    "MATH_UNIT_CONVERSION", "NUMBER_EXER", "MATH_CALC_ROW", "CHOOSE_FRACTION",
    "MATH_ROUNDING", "MATH_FORM_CALCULATION", "MATH_AUDIO_ARITHMETIC",
    "MATH_DECIMAL", "VOCABULARY_TEST", "MATH_CONVERT_FRACTIONS",
    "MATH_LONG_DIVISION", "MATH_PERCENTAGE", "FRAC_SIMPLIFY", "HUNDRED_TABLE",
    "PIC_TO_CALC", "MATH_CHART", "MATH_TIMED_LEVEL_TEST", "MATH_WHAT_NUMBER",
    "MATH_BUILD_NUMBER", "SEQUENCE", "MATH_CALC_FRACTIONS", "NUMBER_LINE",
    "MATH_NUMBER_LINE",
}
OTHER_TYPES = {
    "CARDS_GAME", "RANDOM_WORD", "VILLE_CODING", "SURVEY", "MAGIC_SQUARE",
    "FILL_IN_EXERCISE", "CONCEPT",
}


def exercise_family(exercise_type):
    if exercise_type in MCQ_TYPES:
        return "mcq"
    if exercise_type in SINGLE_ANSWER_TEXT_TYPES:
        return "single_answer_text"
    if exercise_type in MATCHING_TYPES:
        return "matching"
    if exercise_type in ORDERING_SORTING_TYPES:
        return "ordering_sorting"
    if exercise_type in SPATIAL_TYPES:
        return "spatial"
    if exercise_type in CLASSIFICATION_SELECTION_TYPES:
        return "classification_selection"
    if exercise_type in TIME_TYPES:
        return "time"
    if exercise_type in NUMERIC_OR_TEXT_ENTRY_TYPES:
        return "numeric_or_text_entry"
    if exercise_type in OTHER_TYPES:
        return "other"
    return "other"  # future/unseen types default here; check reports/family_report.json


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


def parse_options(possible_answers_raw):
    """Returns a list of {"value": str, "correct": bool} or [] if not parseable/empty."""
    if not possible_answers_raw or possible_answers_raw == "[]":
        return []
    try:
        raw = json.loads(possible_answers_raw)
    except json.JSONDecodeError:
        return []
    options = []
    for entry in raw:
        if not isinstance(entry, dict) or "answerValue" not in entry:
            continue
        options.append({
            "value": entry.get("answerValue"),
            "correct": entry.get("correctness") == 1,
        })
    return options


def extract_option_fields(exercise_type, possible_answers_raw, answer_raw):
    """Returns dict with has_options/option_count/options_json/correct_*/selected_*.
    Only MCQ_TYPES and SINGLE_ANSWER_TEXT_TYPES ever populate options; every
    other type gets has_options=False (answer_raw is still preserved by the
    caller regardless)."""
    empty = {
        "has_options": False, "option_count": 0, "options_json": "[]",
        "correct_option_index": "", "correct_option_value": "",
        "selected_option_index": "", "selected_option_value": "",
    }
    if exercise_type not in MCQ_TYPES and exercise_type not in SINGLE_ANSWER_TEXT_TYPES:
        return empty

    options = parse_options(possible_answers_raw)
    if not options:
        return empty

    correct_idx = next((i for i, o in enumerate(options) if o["correct"]), None)
    selected_idx = next((i for i, o in enumerate(options) if o["value"] == answer_raw), None)
    return {
        "has_options": True,
        "option_count": len(options),
        "options_json": json.dumps(options, ensure_ascii=False),
        "correct_option_index": correct_idx if correct_idx is not None else "",
        "correct_option_value": options[correct_idx]["value"] if correct_idx is not None else "",
        "selected_option_index": selected_idx if selected_idx is not None else -1,
        "selected_option_value": answer_raw,
    }


def write_summary(path, fieldnames, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def open_source():
    if SOURCE_PLAIN.exists():
        return SOURCE_PLAIN.open("r", encoding="utf-8-sig", newline="")
    return gzip.open(SOURCE_GZ, "rt", encoding="utf-8-sig", newline="")


def main():
    DATA.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)

    counters = Counter()
    type_counts = Counter()
    type_valid_counts = Counter()
    family_counts = Counter()
    family_correctness_available = Counter()
    family_has_options = Counter()

    output_path = DATA / "kt_interactions_v2.csv.gz"
    sample_path = DATA / "kt_interactions_v2_sample.csv"

    with open_source() as source, gzip.open(
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

            # Unlike v1, we do NOT require correctness in {0,1} to keep a row --
            # ~4.9% of rows (matching/sorting/classification-style types) never
            # populate Correctness at all, but the question+answer is still
            # useful context. They're flagged correctness_available=False.
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

            correctness_available = correctness_raw in ("0", "1")
            correctness = correctness_raw if correctness_available else ""
            skill_id = make_skill_id(round_name)
            text = row.get("Question", "") or row.get("ExerciseName", "")
            family = exercise_family(exercise_type)
            answer_raw = row.get("AnswerJson", "")
            option_fields = extract_option_fields(
                exercise_type, row.get("PossibleAnswersJson", ""), answer_raw
            )

            output = {
                "student_id": student_id,
                "timestamp": timestamp,
                "item_id": item_id,
                "skill_id": skill_id,
                "skill_name": round_name,
                "exercise_type": exercise_type,
                "exercise_family": family,
                "correctness": correctness,
                "correctness_available": int(correctness_available),
                "response_time_ms": clean_number(row.get("TimeMs")),
                "task_time_ms": clean_number(row.get("SubmissionTimeOnTaskMs")),
                "attempt_number": clean_number(row.get("AttemptNumber"), integer=True),
                "text": text,
                "exercise_name": row.get("ExerciseName", ""),
                "round_id": row.get("RoundID", ""),
                "round_name": round_name,
                "submission_count": clean_number(row.get("Submission"), integer=True),
                "question_idx": row.get("QuestionIdx", ""),
                "preorder": row.get("PreOrd", ""),
                "answer_raw": answer_raw,
                **option_fields,
            }
            writer.writerow(output)
            if counters["valid_rows"] < 10000:
                sample_writer.writerow(output)

            counters["valid_rows"] += 1
            type_valid_counts[exercise_type] += 1
            family_counts[family] += 1
            if correctness_available:
                family_correctness_available[family] += 1
            if option_fields["has_options"]:
                family_has_options[family] += 1

            if counters["valid_rows"] % 3_000_000 == 0:
                print(f"...processed {counters['valid_rows']:,} valid rows", flush=True)

    report = {
        "source": str(SOURCE_PLAIN if SOURCE_PLAIN.exists() else SOURCE_GZ),
        "output_file": str(output_path),
        "output_columns": OUTPUT_FIELDS,
        "source_rows": counters["source_rows"],
        "valid_rows": counters["valid_rows"],
        "dropped_rows": counters["source_rows"] - counters["valid_rows"],
        "drop_counts": {k: v for k, v in counters.items() if k.startswith("dropped_")},
        "valid_rows_by_exercise_type": dict(type_valid_counts.most_common()),
        "source_rows_by_exercise_type": dict(type_counts.most_common()),
        "rows_by_family": dict(family_counts.most_common()),
        "correctness_available_by_family": dict(family_correctness_available),
        "has_options_by_family": dict(family_has_options),
        "notes": [
            "v2 differs from v1 (kt_phase1/legacy_v1_item_level/build_phase1_kt.py) by NOT "
            "requiring Correctness in {0,1} to keep a row, and by preserving "
            "PossibleAnswersJson/AnswerJson (raw + parsed) for every exercise type.",
            "has_options=True (option_count>0, options_json populated) only for exercise_family "
            "'mcq' (MATH_DRILLER, VILLE_QUIZ, VOICE_DRILLER) and 'single_answer_text' "
            "(CROSSWORD_PUZZLE). These are the rows usable for option-aware / (later, once "
            "manually or LLM-tagged) misconception-aware KT.",
            "selected_option_index is -1 when the student's raw answer text could not be "
            "matched to any offered option string (formatting mismatch) -- selected_option_value "
            "(= the raw answer) is still populated in that case.",
            "answer_raw is the verbatim AnswerJson string for every row regardless of family, "
            "so no type's response data is discarded even where it isn't structurally parsed.",
            "correctness_available=False for rows where the source never populates Correctness "
            "(mostly exercise_family in {matching, classification_selection, ordering_sorting} "
            "-- e.g. MATCH_PAIRS, CLASSIFICATION, MATSEL). Exclude these from supervised KT "
            "targets; they can still be used as sequence context if desired.",
        ],
    }
    (REPORTS / "cleaning_report_v2.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k not in (
        "valid_rows_by_exercise_type", "source_rows_by_exercise_type",
    )}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
