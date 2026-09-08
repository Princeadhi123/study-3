"""Build analysis-ready KT tables from chemistry RDA interaction logs.

The source files are preserved. Outputs are written to chemistry_kt_dataset/.
This pipeline keeps action-level rows, including repeated sub-actions, rather
than incorrectly deduplicating them into one row per exercise.
"""
from pathlib import Path
import json
import re

import numpy as np
import pandas as pd
import pyreadr

ROOT = Path(__file__).parent
SOURCE_DIR = ROOT / "chemistry data"
OUT_DIR = ROOT / "chemistry_kt_dataset"
OUT_DIR.mkdir(exist_ok=True)

EXPECTED_COLUMNS = [
    "TeacherIDCode", "IDCode", "SubmissionTimestamp", "RoundID", "RoundName",
    "ExerciseId", "ExerciseTypeEnum", "ExerciseName", "QuestionIdx",
    "SubmissionTimeOnTaskMs", "AttemptNumber", "PreOrd", "Question",
    "PossibleAnswersJson", "AnswerJson", "Correctness", "TimeMs", "Submission",
]


def read_rda(path):
    objects = pyreadr.read_r(path)
    if not objects:
        raise ValueError(f"No objects found in {path.name}")
    df = next(iter(objects.values())).copy()
    missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name} is missing columns: {missing}")
    return df[EXPECTED_COLUMNS]


def normalize_timestamp(series):
    # 7AB uses R/Excel-style numeric day serials; 8/9 use true datetime values.
    # Detect datetime dtype before numeric coercion: pandas represents datetime64
    # internally as integers, and treating those as day serials would erase them.
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series, errors="coerce")
    numeric = pd.to_numeric(series, errors="coerce")
    numeric_dates = pd.to_datetime(numeric, unit="D", origin="1899-12-30", errors="coerce")
    parsed_dates = pd.to_datetime(series.where(numeric.isna()), errors="coerce")
    return numeric_dates.fillna(parsed_dates)


def clean_frame(df, source_file):
    df = df.copy()
    df["source_file"] = source_file
    df["student_id"] = df["IDCode"].astype("string").str.strip()
    df["teacher_id"] = df["TeacherIDCode"].astype("string").str.strip()
    df["session_id"] = df["RoundID"].astype("Int64").astype("string")
    df["item_id"] = df["ExerciseId"].astype("Int64").astype("string")
    df["question_index"] = pd.to_numeric(df["QuestionIdx"], errors="coerce").astype("Int64")
    df["attempt_number"] = pd.to_numeric(df["AttemptNumber"], errors="coerce").astype("Int64")
    df["question_id"] = df["item_id"] + "_q" + df["question_index"].astype("string")
    df["timestamp"] = normalize_timestamp(df["SubmissionTimestamp"])

    df["time_ms_raw"] = pd.to_numeric(df["TimeMs"], errors="coerce")
    df["time_on_task_ms"] = pd.to_numeric(df["SubmissionTimeOnTaskMs"], errors="coerce")
    # TimeMs is retained raw for audit. Negative and implausibly huge values are
    # not used as clean duration; the source has clear timestamp/overflow artifacts.
    df["time_ms_valid"] = df["time_ms_raw"].between(0, 3_600_000, inclusive="both")
    df["time_ms_clean"] = df["time_ms_raw"].where(df["time_ms_valid"])
    df["time_on_task_ms_valid"] = df["time_on_task_ms"].ge(0)

    df["correctness"] = pd.to_numeric(df["Correctness"], errors="coerce")
    df.loc[~df["correctness"].isin([0, 1]), "correctness"] = np.nan
    df["is_scored"] = df["correctness"].notna()
    df["has_possible_answers"] = df["PossibleAnswersJson"].fillna("[]").astype(str).str.strip().ne("[]")
    df["is_submitted_answer"] = df["AnswerJson"].notna() & df["AnswerJson"].astype(str).str.strip().ne("")

    # Preserve original JSON fields and distinguish missing JSON from malformed JSON.
    def json_valid(value):
        if pd.isna(value) or str(value).strip() == "":
            return np.nan
        try:
            json.loads(str(value))
            return True
        except (TypeError, ValueError, json.JSONDecodeError):
            return False

    df["possible_answers_json_valid"] = df["PossibleAnswersJson"].map(json_valid)
    df["answer_json_valid"] = df["AnswerJson"].map(json_valid)
    df["exercise_type"] = df["ExerciseTypeEnum"].astype("string").str.strip()
    df["topic"] = df["RoundName"].astype("string").str.strip()
    df["question_text"] = df["Question"].astype("string").str.strip()
    df["question_text_available"] = df["question_text"].notna() & df["question_text"].ne("")

    def answer_payload(value):
        if pd.isna(value) or str(value).strip() == "":
            return {}
        try:
            payload = json.loads(str(value))
            return payload if isinstance(payload, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    payloads = df["AnswerJson"].map(answer_payload)
    df["embedded_question"] = payloads.map(lambda p: p.get("question"))
    df["embedded_answer"] = payloads.map(lambda p: p.get("answer"))
    generic_question = df["question_text"].fillna("").str.match(
        r"^exercise:\s*[^,]+,\s*question:\s*\d+\s*$", case=False
    )
    has_embedded_question = df["embedded_question"].notna() & df["embedded_question"].astype(str).str.strip().ne("")
    # Prefer the embedded prompt when the source Question is only a generic
    # exercise/question locator. Otherwise retain the original Question text.
    df["question_text_for_model"] = df["question_text"].where(
        df["question_text_available"] & ~generic_question,
        df["embedded_question"],
    )
    df["question_text_for_model"] = df["question_text_for_model"].fillna(
        df["ExerciseName"].astype("string").str.strip()
    )
    df["question_text_source"] = np.select(
        [has_embedded_question & generic_question, df["question_text_available"] & ~generic_question],
        ["answer_json_embedded", "question_column"],
        default="exercise_name_fallback",
    )
    return df


def main():
    frames = []
    source_report = []
    for path in sorted(SOURCE_DIR.glob("*.rda")):
        df = clean_frame(read_rda(path), path.name)
        frames.append(df)
        source_report.append({
            "source_file": path.name,
            "rows": len(df),
            "students": df["student_id"].nunique(),
            "sessions": df["session_id"].nunique(),
            "exercises": df["item_id"].nunique(),
            "question_ids": df["question_id"].nunique(),
        })

    events = pd.concat(frames, ignore_index=True)
    events["source_row"] = np.arange(len(events), dtype="int64")
    events = events.sort_values(
        ["student_id", "timestamp", "session_id", "Submission", "source_row"],
        kind="stable",
    ).reset_index(drop=True)
    events["sequence_index"] = events.groupby("student_id", dropna=False).cumcount() + 1
    events["session_sequence_index"] = events.groupby(
        ["student_id", "session_id"], dropna=False
    ).cumcount() + 1
    events["event_id"] = "chem_" + pd.Series(
        np.arange(len(events)) + 1, index=events.index
    ).astype(str).str.zfill(8)

    output_columns = [
        "event_id", "source_file", "source_row", "student_id", "teacher_id",
        "session_id", "topic", "item_id", "question_id", "exercise_type",
        "ExerciseName", "question_index", "sequence_index", "session_sequence_index",
        "PreOrd", "timestamp", "attempt_number", "Submission", "question_text",
        "question_text_available", "embedded_question", "embedded_answer",
        "question_text_for_model", "question_text_source", "PossibleAnswersJson",
        "AnswerJson", "correctness", "is_scored", "has_possible_answers",
        "is_submitted_answer", "time_ms_raw",
        "time_ms_clean", "time_ms_valid", "time_on_task_ms", "time_on_task_ms_valid",
        "possible_answers_json_valid", "answer_json_valid",
    ]
    events[output_columns].to_csv(OUT_DIR / "chemistry_kt_events.csv", index=False, encoding="utf-8-sig")
    # Convenience training table: only automatically scored responses. Keep the
    # full event table for behavioral/hint analyses, since unscored tutorial and
    # intermediate actions are still meaningful events.
    events.loc[events["is_scored"], output_columns].to_csv(
        OUT_DIR / "chemistry_kt_scored_events.csv", index=False, encoding="utf-8-sig"
    )

    questions = (
        events.groupby(["item_id", "question_id", "exercise_type", "ExerciseName", "question_text_for_model"], dropna=False)
        .agg(
            topic=("topic", "first"),
            n_events=("event_id", "size"),
            n_students=("student_id", "nunique"),
            n_sessions=("session_id", "nunique"),
            n_scored=("is_scored", "sum"),
            n_with_time=("time_ms_clean", lambda s: s.notna().sum()),
            possible_answers_json=("PossibleAnswersJson", "first"),
        )
        .reset_index()
    )
    questions.to_csv(OUT_DIR / "chemistry_kt_questions.csv", index=False, encoding="utf-8-sig")

    students = (
        events.groupby("student_id", dropna=False)
        .agg(
            n_events=("event_id", "size"),
            n_sessions=("session_id", "nunique"),
            n_exercises=("item_id", "nunique"),
            n_questions=("question_id", "nunique"),
            n_scored=("is_scored", "sum"),
            n_attempts_gt1=("attempt_number", lambda s: (s > 1).sum()),
            first_timestamp=("timestamp", "min"),
            last_timestamp=("timestamp", "max"),
        )
        .reset_index()
    )
    students.to_csv(OUT_DIR / "chemistry_kt_students.csv", index=False, encoding="utf-8-sig")

    report = {
        "total_events": int(len(events)),
        "students": int(events.student_id.nunique()),
        "sessions": int(events.session_id.nunique()),
        "exercises": int(events.item_id.nunique()),
        "questions": int(events.question_id.nunique()),
        "exercise_types": int(events.exercise_type.nunique()),
        "scored_events": int(events.is_scored.sum()),
        "missing_correctness": int((~events.is_scored).sum()),
        "time_ms_raw_nonnull": int(events.time_ms_raw.notna().sum()),
        "time_ms_clean_nonnull": int(events.time_ms_clean.notna().sum()),
        "invalid_time_ms": int((~events.time_ms_valid & events.time_ms_raw.notna()).sum()),
        "time_on_task_nonnull": int(events.time_on_task_ms.notna().sum()),
        "timestamps_nonnull": int(events.timestamp.notna().sum()),
        "attempts_gt_one": int((events.attempt_number > 1).sum()),
        "events_with_nonempty_possible_answers": int(events["has_possible_answers"].sum()),
        "source_files": source_report,
    }
    (OUT_DIR / "chemistry_kt_build_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
