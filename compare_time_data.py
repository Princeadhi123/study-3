"""Compare DA item-time exports with the original DA score exports.

Read-only diagnostic: does not modify the cleaned KT dataset.
Writes kt_dataset/time_data_comparison.csv.
"""
from pathlib import Path
import re
import pandas as pd

ROOT = Path(__file__).parent
SCORE_DIR = ROOT / "Final datasets to Prince"
TIME_DIR = SCORE_DIR / "time data"
OUT = ROOT / "kt_dataset" / "time_data_comparison.csv"


def booklet_key(name):
    m = re.search(r"(?:DigiEva|DA)_2026_mat_(\d)lk_v(\d)", name, re.I)
    return f"{m.group(1)}lk_v{m.group(2)}" if m else None


def load_export(path):
    raw = pd.read_excel(path, header=None)
    header = 0
    for r in range(min(10, len(raw))):
        if "IDCode" in raw.iloc[r].astype(str).values:
            header = r
            break
    df = raw.iloc[header + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[header]]
    df = df.reset_index(drop=True)
    id_col = next((c for c in df.columns if c.lower() == "idcode"), None)
    if id_col is None:
        raise ValueError(f"No IDCode column in {path.name}")
    return df, id_col


score_files = {booklet_key(p.name): p for p in SCORE_DIR.glob("*.xlsx") if booklet_key(p.name)}
time_files = {booklet_key(p.name): p for p in TIME_DIR.glob("*.xlsx") if booklet_key(p.name)}
rows = []

for booklet in sorted(time_files):
    tpath = time_files[booklet]
    spath = score_files.get(booklet)
    tdf, tid = load_export(tpath)
    row = {
        "booklet": booklet,
        "time_file": tpath.name,
        "score_file": "" if spath is None else spath.name,
        "time_rows": len(tdf),
        "score_rows": None,
        "common_students": None,
        "time_only_students": None,
        "score_only_students": None,
        "common_item_columns": None,
        "time_only_item_columns": None,
        "score_only_item_columns": None,
        "time_numeric_cells": None,
        "time_nonnegative_cells": None,
        "time_zero_cells": None,
        "items_with_varying_time": None,
        "items_with_constant_time": None,
        "median_time_ms": None,
        "min_time_ms": None,
        "max_time_ms": None,
        "status": "ok",
    }
    if spath is None:
        row["status"] = "missing_score_workbook"
        rows.append(row)
        continue

    sdf, sid = load_export(spath)
    row["score_rows"] = len(sdf)
    tids = set(tdf[tid].dropna().astype(str))
    sids = set(sdf[sid].dropna().astype(str))
    row["common_students"] = len(tids & sids)
    row["time_only_students"] = len(tids - sids)
    row["score_only_students"] = len(sids - tids)

    # Candidate item columns are question columns with the export's `_Q:` marker.
    # This excludes demographic, attitude, summary, and other non-item fields.
    item_pat = re.compile(r"_\d+_Q:", re.I)
    tcols = {c for c in tdf.columns if item_pat.search(c)}
    scols = {c for c in sdf.columns if item_pat.search(c)}
    common = sorted(tcols & scols)
    row["common_item_columns"] = len(common)
    row["time_only_item_columns"] = len(tcols - scols)
    row["score_only_item_columns"] = len(scols - tcols)

    # Numeric values in common columns are treated as candidate durations.
    if common:
        vals = tdf[common].apply(pd.to_numeric, errors="coerce")
        flat = vals.to_numpy().ravel()
        flat = flat[~pd.isna(flat)]
        row["time_numeric_cells"] = int(len(flat))
        row["time_nonnegative_cells"] = int((flat >= 0).sum())
        row["time_zero_cells"] = int((flat == 0).sum())
        row["median_time_ms"] = float(pd.Series(flat).median()) if len(flat) else None
        row["min_time_ms"] = float(flat.min()) if len(flat) else None
        row["max_time_ms"] = float(flat.max()) if len(flat) else None
        nunique = vals.nunique(dropna=True)
        row["items_with_varying_time"] = int((nunique > 1).sum())
        row["items_with_constant_time"] = int((nunique == 1).sum())

    rows.append(row)

result = pd.DataFrame(rows)
result.to_csv(OUT, index=False, encoding="utf-8-sig")
print(result.to_string(index=False))
print(f"\nWrote {OUT}")
