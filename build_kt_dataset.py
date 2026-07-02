"""
Build KT-ready dataset from DigiArvi 2026 math logs + item map.

Outputs (kt_dataset/):
  students.csv               one row per student (demographics, attitudes, summary scores)
  items_master.csv           one row per item column per booklet, joined with item-map metadata
  student_responses_long.csv one row per (student, item) in answer sequence  -> KT model input
  similar_item_groups.csv    item groupings (identical across grades via OPLM id; skill groups)
  build_report.txt           matching diagnostics per booklet

Sequence note: logs are wide score matrices without timestamps. The answer
sequence is reconstructed from the test structure (item-map 'testin jarjestys'
+ column order in the export, which follows booklet order):
  attitude survey -> FUNA subtraction -> FUNA reading fluency -> DA math items.
"""
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "Final datasets to Prince"
MAP_FILE = ROOT / "Item map DA 2026 Final.xlsx"
OUT_DIR = ROOT / "kt_dataset"
OUT_DIR.mkdir(exist_ok=True)

report_lines = []


def report(*args):
    line = " ".join(str(a) for a in args)
    report_lines.append(line)
    print(line)


# ---------------------------------------------------------------- item map ---
FIELD_MAP = {
    "oplm jarj": "oplm_id",
    "oplm nimi": "oplm_name",
    "max": "max_points",
    "source": "source",
    "testin jarjestys": "test_order",
    "osioa": "osio_a",
    "tehtava": "task_no",
    "osiob": "osio_b",
    "id": "ville_id",
    "id villessa": "ville_id",
    "nimi": "item_name",
    "tehtavan nimi": "item_name",
    "kuvaus": "description",
    "taso": "level",
    "num taso": "nuta_level",
    "numerotaitoisuus": "nuta_level",
    "nuta taso": "nuta_level",
    "osioita": "n_subitems",
    "ops sisaltoalue": "content_area",
    "ops3": "content_area3",
    "kognitiivinen taso": "cognitive_level",
    "tehtavatyyppi": "task_type",
    "m": "task_type",
    "tekija": "author",
    "koodi": "skill_code",
    "osa-alue": "skill_domain",
    "svensk id": "ville_id_sv",
}


def norm_label(s):
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s


def norm_name(s):
    """Normalize an item name for matching."""
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFKC", s).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def parse_item_map():
    raw = pd.read_excel(MAP_FILE, header=None)
    titles = raw.iloc[0].astype(str)
    labels = raw.iloc[1]

    # locate booklet blocks
    block_cols = []
    for c in range(raw.shape[1]):
        if re.match(r"^\d\.\s?lk", str(titles[c]).strip()):
            block_cols.append(c)
    block_cols.append(raw.shape[1])

    # global ALL-items block (cols 1..11): OPLM id -> IRT B
    all_block = raw.iloc[2:, 1:9]
    all_block.columns = ["new_opml", "oplm_id", "oplm_name", "max", "B", "ops1", "ops2", "ops3"]
    b_by_oplm = {}
    for _, r in all_block.iterrows():
        if pd.notna(r["oplm_id"]) and pd.notna(r["B"]):
            b_by_oplm[str(r["oplm_id"]).strip()] = r["B"]

    booklets = {}
    for i in range(len(block_cols) - 1):
        c0, c1 = block_cols[i], block_cols[i + 1]
        title = str(titles[block_cols[i]]).strip()
        m = re.match(r"^(\d)\.\s?lk ver(\d)(.*)", title)
        grade, ver = int(m.group(1)), int(m.group(2))
        has_sv = "sv" in m.group(3)

        rows = []
        item_name_seen = False
        for r in range(2, raw.shape[0]):
            row = raw.iloc[r, c0:c1]
            if row.isna().all():
                continue
            rec, name_count = {}, 0
            ca_count = 0
            for j, val in enumerate(row):
                lab = norm_label(labels.iloc[c0 + j])
                key = FIELD_MAP.get(lab)
                if key == "item_name":
                    name_count += 1
                    key = "item_name" if name_count == 1 else "item_name_sv"
                if key == "content_area":
                    ca_count += 1
                    key = f"content_area{ca_count}"
                if key and pd.notna(val) and str(val).strip() not in ("", "nan"):
                    rec[key] = val
            if rec.get("oplm_name") and rec.get("item_name"):
                rows.append(rec)
        df = pd.DataFrame(rows)
        keys = []
        k = (grade, f"v{ver}")
        keys.append(k)
        if has_sv:
            keys.append((grade, "v4"))
        for k in keys:
            booklets[k] = df
        report(f"item map block '{title}' -> grade {grade} ver {ver}{'+sv' if has_sv else ''}: {len(df)} items")
    return booklets, b_by_oplm


# ------------------------------------------------------------- log parsing ---
DEMO_COLS = {
    "ver", "orig_order", "teacheridcode", "idcode", "permission", "gender",
    "city", "school", "home_lang", "strong_lang", "friend_lang", "school_lang",
}
SUMMARY_PAT = re.compile(r"^(missing_all%|sum_)", re.I)
ITEM_PAT = re.compile(r"_(\d+)_Q:", re.I)


def classify_columns(cols):
    demo, attitude, funa, da, summary = [], [], [], [], []
    for c in cols:
        name = str(c)
        low = norm_label(name)
        if low in DEMO_COLS:
            demo.append(c)
        elif SUMMARY_PAT.match(low):
            summary.append(c)
        elif name.upper().startswith("FUNA"):
            funa.append(c)
        elif ITEM_PAT.search(name):
            da.append(c)
        else:
            attitude.append(c)
    return demo, attitude, funa, da, summary


def load_log(path):
    raw = pd.read_excel(path, header=None)
    hdr = None
    for r in range(10):
        if "IDCode" in raw.iloc[r].astype(str).values:
            hdr = r
            break
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr]]
    df = df.reset_index(drop=True)
    sheet = pd.ExcelFile(path).sheet_names[0]
    m = re.search(r"(\d)lk_v(\d)", sheet)
    grade, ver = int(m.group(1)), f"v{m.group(2)}"
    return df, grade, ver


# --------------------------------------------------------------- matching ---
def base_name(col):
    """'AlgA21_Pallon paino_2_Q:Pallot...' -> ('AlgA21_Pallon paino', 2, 'Pallot...')"""
    m = re.match(r"^(.*)_(\d+)_Q:(.*)$", str(col), re.S)
    if m:
        return m.group(1), int(m.group(2)), m.group(3).strip()
    return str(col), 1, ""


def sim(a, b):
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def order_key(v):
    digits = re.sub(r"\D", "", str(v))
    return int(digits) if digits else 10**6


def match_booklet(da_cols, map_df):
    """Return list of (col, map_row_index or None, method, confidence)."""
    map_df = map_df.copy()
    if "test_order" in map_df.columns:
        map_df["_ord"] = map_df["test_order"].map(order_key)
    else:
        map_df["_ord"] = range(len(map_df))
    map_df = map_df.sort_values(["_ord"], kind="stable").reset_index(drop=True)

    # group columns by base name; group map rows by item_name
    col_info = [base_name(c) for c in da_cols]
    used = set()
    result = []

    # pass 1: name-based (base name vs item_name / item_name_sv)
    for ci, (bname, sub, qtxt) in enumerate(col_info):
        best, best_s = None, 0.0
        for mi in range(len(map_df)):
            if mi in used:
                continue
            row = map_df.iloc[mi]
            s = max(sim(bname, row.get("item_name", "")),
                    sim(bname, row.get("item_name_sv", "")) if pd.notna(row.get("item_name_sv")) else 0)
            # prefer matching sub-item position within same-name group
            if s > best_s:
                best, best_s = mi, s
        if best is not None and best_s >= 0.75:
            # among equal-name rows pick the sub'th one
            row_name = map_df.iloc[best].get("item_name", "")
            same = [mi for mi in range(len(map_df))
                    if mi not in used and sim(row_name, map_df.iloc[mi].get("item_name", "")) >= 0.999]
            pick = same[min(sub - 1, len(same) - 1)] if same else best
            used.add(pick)
            result.append((ci, pick, "name", round(best_s, 2)))
        else:
            result.append((ci, None, "pending", round(best_s, 2)))

    # pass 2: positional fallback for unmatched, in order
    free = [mi for mi in range(len(map_df)) if mi not in used]
    fi = 0
    final = []
    for ci, pick, method, conf in result:
        if pick is None and fi < len(free):
            pick, method = free[fi], "positional"
            fi += 1
        final.append((ci, pick, method, conf))
    return final, map_df


# ------------------------------------------------------------------- main ---
def main():
    booklets, b_by_oplm = parse_item_map()

    students_all, items_all, responses_all = [], [], []

    for path in sorted(LOG_DIR.glob("*.xlsx")):
        df, grade, ver = load_log(path)
        demo, attitude, funa, da, summary = classify_columns(df.columns)
        booklet = f"{grade}lk_{ver}"
        map_df = booklets.get((grade, ver))
        report(f"\n=== {path.name}")
        report(f"  booklet={booklet} students={len(df)} demo={len(demo)} attitude={len(attitude)} "
               f"funa={len(funa)} da_items={len(da)} summary={len(summary)} "
               f"map_items={'-' if map_df is None else len(map_df)}")

        # ---- students table
        stu = df[demo + attitude + summary].copy()
        cols, seen = [], {}
        for c in stu.columns:
            name = norm_label(c) if norm_label(c) in DEMO_COLS else c
            seen[name] = seen.get(name, 0) + 1
            cols.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
        stu.columns = cols
        stu.insert(0, "booklet", booklet)
        stu.insert(1, "grade", grade)
        stu.insert(2, "version", ver)
        stu = stu.rename(columns={"idcode": "student_id"})
        students_all.append(stu)

        # ---- item matching
        matches, map_sorted = ([], None)
        if map_df is not None and len(map_df):
            matches, map_sorted = match_booklet(da, map_df)
            n_name = sum(1 for *_, m, _ in [(a, b, c, d) for a, b, c, d in matches] if m == "name")
            n_pos = sum(1 for a, b, c, d in matches if c == "positional")
            n_un = sum(1 for a, b, c, d in matches if b is None)
            report(f"  match: name={n_name} positional={n_pos} unmatched={n_un} / {len(da)} cols")

        # sequence: attitude excluded from responses; funa first, then da
        seq_cols = [("funa_subtraction" if "Subtraction" in c or "subtraction" in c.lower()
                     else "funa_reading", c) for c in funa]
        seq_cols += [("da_math", c) for c in da]

        match_by_col = {}
        if map_sorted is not None:
            for ci, mi, method, conf in matches:
                match_by_col[da[ci]] = (mi, method, conf)

        item_rows = {}
        for seq, (phase, col) in enumerate(seq_cols, start=1):
            bname, sub, qtxt = base_name(col)
            item_uid = f"{booklet}_i{seq:03d}"
            rec = {
                "item_uid": item_uid, "booklet": booklet, "grade": grade, "version": ver,
                "seq_in_test": seq, "phase": phase, "column_name": col,
                "task_base_name": bname, "subitem_no": sub, "question_text_in_log": qtxt,
            }
            if phase == "da_math" and col in match_by_col:
                mi, method, conf = match_by_col[col]
                if mi is not None:
                    row = map_sorted.iloc[mi]
                    oplm = str(row.get("oplm_name", "")).strip()
                    rec.update({
                        "match_method": method, "match_confidence": conf,
                        "oplm_id": row.get("oplm_id"), "oplm_name": oplm,
                        "ville_id": row.get("ville_id"), "source": row.get("source"),
                        "test_order_in_map": row.get("test_order"),
                        "item_name_map": row.get("item_name"),
                        "description": row.get("description"),
                        "level": row.get("level"), "nuta_level": row.get("nuta_level"),
                        "content_area1": row.get("content_area1"),
                        "content_area2": row.get("content_area2"),
                        "content_area3": row.get("content_area3"),
                        "cognitive_level": row.get("cognitive_level"),
                        "task_type": row.get("task_type"),
                        "skill_code": row.get("skill_code"),
                        "skill_domain": row.get("skill_domain"),
                        "max_points": row.get("max_points"),
                        "irt_b": b_by_oplm.get(oplm.split()[0] if oplm else "", np.nan),
                    })
            elif phase.startswith("funa"):
                rec.update({
                    "content_area1": "S2" if phase == "funa_subtraction" else "READ",
                    "skill_domain": "ArithmeticFluency" if phase == "funa_subtraction" else "ReadingFluency",
                    "description": qtxt,
                })
            item_rows[col] = rec
            items_all.append(rec)

        # ---- long responses
        ids = df["IDCode"] if "IDCode" in df.columns else df[[c for c in df.columns if norm_label(c) == "idcode"][0]]
        for phase, col in seq_cols:
            rec = item_rows[col]
            vals = pd.to_numeric(df[col], errors="coerce")
            mask = vals.notna()
            if not mask.any():
                continue
            part = pd.DataFrame({
                "student_id": ids[mask].values,
                "booklet": booklet,
                "grade": grade,
                "version": ver,
                "item_uid": rec["item_uid"],
                "seq_in_test": rec["seq_in_test"],
                "phase": phase,
                "score": vals[mask].values,
            })
            responses_all.append(part)

    # ---------------------------------------------------------- write out ---
    students = pd.concat(students_all, ignore_index=True)
    items = pd.DataFrame(items_all)
    responses = pd.concat(responses_all, ignore_index=True)
    responses = responses.sort_values(
        ["grade", "version", "student_id", "seq_in_test"], kind="stable").reset_index(drop=True)
    # KT convenience: join domain + difficulty onto responses
    responses = responses.merge(
        items[["item_uid", "content_area1", "cognitive_level", "skill_domain",
               "oplm_id", "oplm_name", "irt_b", "task_base_name", "description"]],
        on="item_uid", how="left")

    students.to_csv(OUT_DIR / "students.csv", index=False, encoding="utf-8-sig")
    items.to_csv(OUT_DIR / "items_master.csv", index=False, encoding="utf-8-sig")
    responses.to_csv(OUT_DIR / "student_responses_long.csv", index=False, encoding="utf-8-sig")

    # ---- similar item groups
    da_items = items[items["phase"] == "da_math"].copy()
    groups = []
    # (a) identical item reused across booklets -> same oplm_name
    for name, g in da_items.dropna(subset=["oplm_name"]).groupby("oplm_name"):
        if len(g) > 1:
            groups.append({
                "group_type": "identical_item_cross_grade",
                "group_key": name,
                "n_items": len(g),
                "item_uids": ";".join(g["item_uid"]),
                "booklets": ";".join(sorted(g["booklet"].unique())),
                "reasoning": f"Same OPLM item '{name}' administered in multiple booklets (anchor item) -> directly comparable across grades.",
            })
    # (b) skill groups: same content area + cognitive level
    for (ca, cog), g in da_items.dropna(subset=["content_area1", "cognitive_level"]).groupby(
            ["content_area1", "cognitive_level"]):
        groups.append({
            "group_type": "skill_group",
            "group_key": f"{ca}_cog{cog}",
            "n_items": len(g),
            "item_uids": ";".join(g["item_uid"]),
            "booklets": ";".join(sorted(g["booklet"].unique())),
            "reasoning": f"Items sharing OPS content area {ca} and cognitive level {cog} -> same latent knowledge component for KT.",
        })
    pd.DataFrame(groups).to_csv(OUT_DIR / "similar_item_groups.csv", index=False, encoding="utf-8-sig")

    report("\n=== TOTALS ===")
    report("students:", len(students), "| items:", len(items), "| responses:", len(responses))
    report("identical-item groups:", sum(1 for g in groups if g["group_type"] == "identical_item_cross_grade"))
    (OUT_DIR / "build_report.txt").write_text("\n".join(report_lines), encoding="utf-8")


if __name__ == "__main__":
    main()
