"""Merge supervisor-provided DigiArvi item times into the cleaned long KT data.

Inputs are read-only. New outputs are written beside the current DigiArvi KT
outputs under DigiArvi data/kt_dataset_analysis/.
"""
from pathlib import Path
import json
import re
import unicodedata

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "DigiArvi data"
TIME_DIR = DATA_DIR / "time data"
KT_DIR = DATA_DIR / "kt_dataset_analysis"


def booklet_key(filename):
    match = re.search(r"(?:DA|DigiEva)_2026_mat_(\d)lk_v(\d)", filename, re.I)
    return f"{match.group(1)}lk_v{match.group(2)}" if match else None


def norm_text(value):
    value = unicodedata.normalize("NFKC", str(value)).replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def question_parts(column):
    match = re.match(r"^(.*)_(\d+)_Q:(.*)$", str(column), re.S)
    if not match:
        return norm_text(column), 1, ""
    return norm_text(match.group(1)), int(match.group(2)), norm_text(match.group(3))


def read_time_workbook(path):
    frame = pd.read_excel(path, header=0)
    frame.columns = [str(c).strip() for c in frame.columns]
    id_col = next(c for c in frame.columns if c.lower() == "idcode")
    return frame, id_col


def build_column_map(booklet, time_columns, item_rows):
    """Map time headers to item_uids using exact headers, then base/sub/question text."""
    item_rows = item_rows.copy()
    exact = {norm_text(c): c for c in time_columns}
    by_signature = {}
    for col in time_columns:
        base, sub, question = question_parts(col)
        by_signature.setdefault((base, sub, question), []).append(col)

    mapped = []
    used = set()
    for _, item in item_rows.iterrows():
        source_col = item["column_name"]
        if norm_text(source_col) in exact:
            candidate = exact[norm_text(source_col)]
            method = "exact_header"
        else:
            base, sub, question = question_parts(source_col)
            candidates = [c for c in by_signature.get((base, sub, question), []) if c not in used]
            if len(candidates) == 1:
                candidate, method = candidates[0], "normalized_signature"
            else:
                # Some exports changed only the family prefix (e.g. PerA11/PerA22).
                candidates = [
                    c for c in time_columns
                    if c not in used
                    and question_parts(c)[2] == question
                    and question
                ]
                candidate, method = (candidates[0], "question_text") if len(candidates) == 1 else (None, "unmatched")
        if candidate is not None:
            used.add(candidate)
        mapped.append({
            "booklet": booklet,
            "item_uid": item["item_uid"],
            "source_item_column": source_col,
            "time_item_column": candidate,
            "time_match_method": method,
        })
    return pd.DataFrame(mapped)


def main():
    responses = pd.read_csv(KT_DIR / "student_responses_long.csv", encoding="utf-8-sig", low_memory=False)
    items = pd.read_csv(KT_DIR / "items_master.csv", encoding="utf-8-sig", low_memory=False)
    da_items = items[items["phase"] == "da_math"].copy()
    time_parts = []
    map_parts = []
    file_report = []

    time_files = {booklet_key(p.name): p for p in TIME_DIR.glob("*.xlsx") if booklet_key(p.name)}
    for booklet, path in sorted(time_files.items()):
        time_frame, time_id_col = read_time_workbook(path)
        booklet_items = da_items[da_items["booklet"] == booklet]
        if booklet_items.empty:
            file_report.append({"booklet": booklet, "time_file": path.name, "status": "no_matching_kt_booklet"})
            continue
        mapping = build_column_map(
            booklet,
            [c for c in time_frame.columns if re.search(r"_\d+_Q:", c)],
            booklet_items,
        )
        map_parts.append(mapping)
        matched = mapping[mapping["time_item_column"].notna()]
        for _, m in matched.iterrows():
            values = pd.to_numeric(time_frame[m["time_item_column"]], errors="coerce")
            part = pd.DataFrame({
                "student_id": time_frame[time_id_col].astype("string").str.strip(),
                "booklet": booklet,
                "item_uid": m["item_uid"],
                "time_spent_ms": values,
                "time_observed": values.notna(),
                "time_match_method": m["time_match_method"],
                "time_source_file": path.name,
            })
            time_parts.append(part[part["time_observed"]].copy())
        file_report.append({
            "booklet": booklet,
            "time_file": path.name,
            "time_students": int(time_frame[time_id_col].nunique()),
            "kt_students": int(responses.loc[responses["booklet"] == booklet, "student_id"].nunique()),
            "items_in_kt": int(len(booklet_items)),
            "items_matched_to_time_columns": int(len(matched)),
            "items_unmatched_to_time_columns": int(len(booklet_items) - len(matched)),
            "status": "matched",
        })

    mapping_all = pd.concat(map_parts, ignore_index=True) if map_parts else pd.DataFrame()
    times = pd.concat(time_parts, ignore_index=True) if time_parts else pd.DataFrame()
    times = times.drop_duplicates(["student_id", "booklet", "item_uid"], keep="first")

    output = responses.merge(times, on=["student_id", "booklet", "item_uid"], how="left")
    output["time_observed"] = output["time_observed"].eq(True)
    output["time_match_method"] = output["time_match_method"].fillna("no_time_record")
    output["time_source_file"] = output["time_source_file"].fillna("")
    output.to_csv(KT_DIR / "student_responses_long_with_time.csv", index=False, encoding="utf-8-sig")

    item_coverage = (
        output.groupby(["booklet", "item_uid"], dropna=False)
        .agg(
            response_rows=("item_uid", "size"),
            responses_with_time=("time_observed", "sum"),
            median_time_ms=("time_spent_ms", "median"),
            min_time_ms=("time_spent_ms", "min"),
            max_time_ms=("time_spent_ms", "max"),
        )
        .reset_index()
    )
    item_coverage["time_coverage_pct"] = (
        100 * item_coverage["responses_with_time"] / item_coverage["response_rows"]
    ).round(2)
    item_coverage.to_csv(KT_DIR / "item_time_coverage.csv", index=False, encoding="utf-8-sig")
    mapping_all.to_csv(KT_DIR / "item_time_column_mapping.csv", index=False, encoding="utf-8-sig")

    report = {
        "response_rows": int(len(output)),
        "response_rows_with_time": int(output["time_observed"].sum()),
        "response_level_time_coverage_pct": round(100 * output["time_observed"].mean(), 2),
        "time_records_used": int(len(times)),
        "duplicate_time_records_removed": int(sum(len(p) for p in time_parts) - len(times)),
        "missing_time_on_responses": int((~output["time_observed"]).sum()),
        "time_min_ms": float(times["time_spent_ms"].min()) if len(times) else None,
        "time_median_ms": float(times["time_spent_ms"].median()) if len(times) else None,
        "time_max_ms": float(times["time_spent_ms"].max()) if len(times) else None,
        "files": file_report,
    }
    (KT_DIR / "item_time_merge_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
