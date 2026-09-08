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
from scipy.optimize import linear_sum_assignment

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

# Likert coding (fi + sv), 1 = fully disagree ... 5 = fully agree
LIKERT_CODE = {
    "täysin eri mieltä": 1, "helt av annan åsikt": 1,
    "jokseenkin eri mieltä": 2, "delvis av annan åsikt": 2,
    "ei samaa eikä eri mieltä": 3, "varken av samma eller annan åsikt": 3,
    "jokseenkin samaa mieltä": 4, "delvis av samma åsikt": 4,
    "täysin samaa mieltä": 5, "helt av samma åsikt": 5,
}
PERMISSION_CODE = {"kyllä": 1, "ja": 1, "ei": 0, "nej": 0}
FRIDA_ALVAR_SCORE_DESCRIPTION = (
    "Frida scored 14 points five times. Alvar scored six points fewer than Frida. "
    "What was Alvar's total score?"
)

# short english keys for attitude items (keyword -> code column name)
ATTITUDE_KEYS = [
    ("enjoy school", "att_enjoy_school"),
    ("like maths", "att_like_maths"),
    ("important to know maths", "att_maths_important"),
    ("good at maths", "att_good_at_maths"),
    ("good at reading", "att_good_at_reading"),
    ("good at writing", "att_good_at_writing"),
]


def attitude_key(col, idx):
    low = str(col).lower()
    for kw, key in ATTITUDE_KEYS:
        if kw in low:
            return key
    return f"att_extra{idx}"
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

    # pass 2: for still-unmatched columns, find the best remaining map row using an
    # OPTIMAL assignment over text similarity (column base-name/question-text vs.
    # map item_name/description), rather than blindly pairing by column order.
    # Blind positional pairing silently mismatches items whenever a booklet's
    # export column order drifts from the item-map row order (confirmed to
    # happen in some booklets, e.g. grade-4 v3) -- optimal assignment on text
    # similarity avoids that by picking the globally best pairing instead.
    free = [mi for mi in range(len(map_df)) if mi not in used]
    pending = [i for i, (ci, pick, method, conf) in enumerate(result) if pick is None]

    if pending and free:
        n, m = len(pending), len(free)
        sim_matrix = np.zeros((n, m))
        for a, idx in enumerate(pending):
            ci = result[idx][0]
            bname, sub, qtxt = col_info[ci]
            for b, mi in enumerate(free):
                row = map_df.iloc[mi]
                s_name = max(
                    sim(bname, row.get("item_name", "")),
                    sim(bname, row.get("item_name_sv", "")) if pd.notna(row.get("item_name_sv")) else 0,
                )
                s_desc = sim(qtxt, row.get("description", "")) if qtxt else 0.0
                sim_matrix[a, b] = max(s_name, s_desc)
        row_ind, col_ind = linear_sum_assignment(-sim_matrix)
        for a, b in zip(row_ind, col_ind):
            idx = pending[a]
            ci = result[idx][0]
            mi, s = free[b], sim_matrix[a, b]
            # low text overlap -> genuinely no evidence, keep as a flagged
            # positional guess (order-based) instead of a confident text match
            method = "text_assigned" if s >= 0.3 else "unresolved"
            result[idx] = (ci, mi, method, round(float(s), 2))

    final = []
    for ci, pick, method, conf in result:
        final.append((ci, pick, method, conf, conf >= 0.3 or method == "name"))
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
        # Remove export-artifact columns that contain no observed values in any
        # booklet (for example blank/nan headers and unused attitude slots), while
        # retaining partially populated demographic and summary variables.
        stu = stu.dropna(axis=1, how="all")
        # coded columns: Likert 1-5 + permission flag (originals kept)
        for ai, c in enumerate(attitude, start=1):
            src = stu[c] if c in stu.columns else None
            if src is not None and not isinstance(src, pd.Series):
                src = src.iloc[:, 0]
            if src is not None:
                stu[attitude_key(c, ai)] = (
                    src.astype(str).str.strip().str.lower().map(LIKERT_CODE))
        if "permission" in stu.columns:
            stu["permission_flag"] = (
                stu["permission"].astype(str).str.strip().str.lower().map(PERMISSION_CODE))
        stu = stu.dropna(axis=1, how="all")
        stu.insert(0, "booklet", booklet)
        stu.insert(1, "grade", grade)
        stu.insert(2, "version", ver)
        stu = stu.rename(columns={"idcode": "student_id"})
        students_all.append(stu)

        # ---- item matching
        matches, map_sorted = ([], None)
        if map_df is not None and len(map_df):
            matches, map_sorted = match_booklet(da, map_df)
            # Verified corrections for four clear repeated/adjacent-item cases.
            # Apply only when the intended OPLM id exists in the current booklet map.
            verified_overrides = {
                ("2lk_v1", "Kolikot", 1): 48.0,
                ("2lk_v1", "Puolet luvusta", 1): 50.0,
                ("9lk_v2", "AlgB21_Sauvojen pituudet laus", 3): 590.0,
                ("9lk_v4", "FunB22_Linjens riktningskoeff", 2): 620.0,
                ("5lk_v1", "Sanallinen laskulauseke", 1): 150.0,
                ("6lk_v4", "AlgA22_Tal som fattas", 2): 360.0,
            }
            updated_matches = []
            for ci, mi, method, conf, reliable in matches:
                bname, sub, _ = base_name(da[ci])
                target_id = verified_overrides.get((booklet, bname, sub))
                if target_id is not None:
                    candidates = map_sorted.index[map_sorted["oplm_id"] == target_id].tolist()
                    if candidates:
                        mi = candidates[0]
                        method, conf, reliable = "verified_override", 1.0, True
                updated_matches.append((ci, mi, method, conf, reliable))
            matches = updated_matches
            n_name = sum(1 for a, b, c, d, e in matches if c == "name")
            n_text = sum(1 for a, b, c, d, e in matches if c == "text_assigned")
            n_pos = sum(1 for a, b, c, d, e in matches if c == "positional")
            n_un = sum(1 for a, b, c, d, e in matches if b is None)
            n_unrel = sum(1 for a, b, c, d, e in matches if b is not None and not e)
            report(f"  match: name={n_name} text_assigned={n_text} positional={n_pos} "
                   f"unmatched={n_un} unreliable={n_unrel} / {len(da)} cols")

        # sequence: attitude excluded from responses; funa first, then da
        seq_cols = [("funa_subtraction" if "Subtraction" in c or "subtraction" in c.lower()
                     else "funa_reading", c) for c in funa]
        seq_cols += [("da_math", c) for c in da]

        match_by_col = {}
        if map_sorted is not None:
            for ci, mi, method, conf, reliable in matches:
                match_by_col[da[ci]] = (mi, method, conf, reliable)

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
                mi, method, conf, reliable = match_by_col[col]
                if mi is not None:
                    row = map_sorted.iloc[mi]
                    oplm = str(row.get("oplm_name", "")).strip()
                    rec.update({
                        "match_method": method, "match_confidence": conf,
                        "match_reliable": reliable,
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
                        "irt_b": b_by_oplm.get(str(row.get("oplm_id")).strip(), np.nan),
                    })
            elif phase.startswith("funa"):
                rec.update({
                    "content_area1": "S2" if phase == "funa_subtraction" else "READ",
                    "skill_domain": "ArithmeticFluency" if phase == "funa_subtraction" else "ReadingFluency",
                    "description": qtxt,
                    "match_method": "n/a_not_item_map", "match_confidence": np.nan, "match_reliable": True,
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
               "oplm_id", "oplm_name", "irt_b", "task_base_name", "description",
               "match_method", "match_confidence", "match_reliable"]],
        on="item_uid", how="left")

    # optional English translations of item descriptions (keyed by oplm_name only --
    # the translation file has no oplm_id, so it cannot disambiguate cases where the
    # same oplm_name label was reused in the item map for genuinely different
    # questions/oplm_ids; see the oplm_name-ambiguity check below).
    trans_file = OUT_DIR / "item_translations_en.csv"
    if trans_file.exists():
        trans = pd.read_csv(trans_file)[["oplm_name", "description_en"]].drop_duplicates("oplm_name")
        items = items.merge(trans, on="oplm_name", how="left")
        responses = responses.merge(trans, on="oplm_name", how="left")

        # Item-specific corrections for the 27 descriptions whose old translations
        # were demonstrably from a different question. These are keyed by item_uid,
        # not oplm_name, because several labels are reused for different items.
        verified_translation_overrides = {
            "3lk_v1_i165": "Calculate the perimeter of the rectangle in the picture if its sides are 5 cm and 3 cm.",
            "3lk_v2_i179": "Liisa has 120 euros and buys a book costing 27 euros. How much money does she have left?",
            "3lk_v2_i192": "How much do 1.5 kg of apples cost if the price per kilogram is 2.40 euros?",
            "4lk_v1_i103": "Which decimal number is shown in the picture? (A number line with 1.3 marked.)",
            "4lk_v1_i104": "Liisa has 120 euros and buys a book costing 27 euros. How much money does she have left?",
            "4lk_v1_i119": "There are 7 days in one week. How many days are there in 12 weeks?",
            "4lk_v1_i143": "A number is multiplied by 4 and then 3 is added to the product. What is the number?",
            "4lk_v3_i145": "Which decimal number is marked on the number line between two whole numbers? 1.2 (fifths).",
            "5lk_v1_i141": "Simplify the fraction 8/12.",
            "5lk_v1_i142": "Calculate: 3.25 + 1.75.",
            "5lk_v1_i143": "Convert 0.75 to a percentage.",
            "5lk_v1_i179": "What is the probability of drawing a red ball from a box containing 3 red balls and 7 blue balls?",
            "6lk_v1_i147": "Calculate: 5231 + 222.",
            "7lk_v1_i136": "Simplify the fraction 8/12.",
            "7lk_v2_i175": "Which decimal number is marked on the number line between the whole numbers 5 and 6?",
            "7lk_v3_i146": FRIDA_ALVAR_SCORE_DESCRIPTION,
            "7lk_v4_i116": FRIDA_ALVAR_SCORE_DESCRIPTION,
            "8lk_v1_i192": "Infer from the picture the value of the function when x = 3.",
            "8lk_v1_i208": "Which of these numbers is the greatest? 2.4 × 10^6, 2.6 × 10^4, 4.2 × 10^6, 4.6 × 10^2, 6.2 × 10^4, or 6.4 × 10^4.",
            "8lk_v3_i155": FRIDA_ALVAR_SCORE_DESCRIPTION,
            "8lk_v4_i121": FRIDA_ALVAR_SCORE_DESCRIPTION,
            "9lk_v1_i187": "Choose the equation of the line that passes through the points (1, -1) and (3, 1).",
            "9lk_v1_i188": "Choose the equation of the line that passes through the points (-2, -1) and (0, 3).",
            "9lk_v1_i206": "If 6 workers complete a job in 6 hours, how long will it take 4 workers?",
            "9lk_v3_i187": "Calculate the average of the salaries 3600, 3400, 3200, and 3800. Give the answer as a whole number.",
            "9lk_v3_i200": "3x - 4 = 23. What is the solution to this equation?",
            "9lk_v4_i160": "Calculate the average of the salaries 3600, 3400, 3200, and 3800. Give the answer as a whole number.",
            "9lk_v4_i173": "3x - 4 = 23. What is the solution to this equation?",
        }
        for df in (items, responses):
            df["description_en"] = df["item_uid"].map(verified_translation_overrides).fillna(df["description_en"])
            df["translation_source"] = df["item_uid"].map(verified_translation_overrides).map(
                lambda x: "verified_item_specific" if pd.notna(x) else "llm_name_keyed"
            )

        # Sanity-check each merged (description, description_en) pair: if both contain
        # numbers and none are shared, the translation almost certainly belongs to a
        # different item (either a translation-file misalignment, or an ambiguous
        # oplm_name reused for different content). Null it out rather than keep a
        # silently wrong translation, and record the check outcome for transparency.
        num_pat = re.compile(r"\d+[.,]?\d*")

        def nums(s):
            return frozenset(x.replace(",", ".") for x in num_pat.findall(str(s)))

        def check(row):
            if row.get("translation_source") == "verified_item_specific":
                return True
            n1, n2 = nums(row["description"]), nums(row["description_en"])
            if pd.isna(row["description_en"]):
                return np.nan
            return not (n1 and n2) or bool(n1 & n2)

        for df in (items, responses):
            df["description_en_reliable"] = df.apply(check, axis=1)
            bad = df["description_en_reliable"] == False  # noqa: E712
            df.loc[bad, "description_en"] = np.nan
        n_bad = int((items["description_en_reliable"] == False).sum())  # noqa: E712
        report("merged description_en for", trans["description_en"].notna().sum(), "oplm names;",
               "nulled", n_bad, "translations that failed a number-consistency check")

    students.to_csv(OUT_DIR / "students.csv", index=False, encoding="utf-8-sig")
    items.to_csv(OUT_DIR / "items_master.csv", index=False, encoding="utf-8-sig")
    responses.to_csv(OUT_DIR / "student_responses_long.csv", index=False, encoding="utf-8-sig")
    # Auditable translation table keyed by the unique generated item_uid. The
    # original LLM file is keyed only by oplm_name, which is ambiguous for reused
    # labels; this file is the safe table for downstream use.
    translation_cols = [
        "item_uid", "booklet", "grade", "version", "oplm_id", "oplm_name",
        "description", "description_en", "description_en_reliable", "translation_source",
    ]
    items[translation_cols].to_csv(
        OUT_DIR / "item_translations_en_verified.csv", index=False, encoding="utf-8-sig"
    )

    # ---- similar item groups
    da_items = items[items["phase"] == "da_math"].copy()
    groups = []
    # (a) identical item reused across booklets -> same oplm_name AND same oplm_id.
    # oplm_name alone is NOT a safe key: the item map reuses some oplm_name labels
    # for genuinely different questions with different oplm_id (confirmed e.g. for
    # 'AiB1109a' -> two different questions, 'DeA2203a' -> two different questions).
    # Grouping only by oplm_id (not requiring exact oplm_id match) would silently
    # treat non-identical items as the same anchor item for cross-grade comparison.
    for (name, oplm_id), g in da_items.dropna(subset=["oplm_name", "oplm_id"]).groupby(["oplm_name", "oplm_id"]):
        if len(g) > 1:
            groups.append({
                "group_type": "identical_item_cross_grade",
                "group_key": name,
                "n_items": len(g),
                "item_uids": ";".join(g["item_uid"]),
                "booklets": ";".join(sorted(g["booklet"].unique())),
                "reasoning": f"Same OPLM item '{name}' (oplm_id={oplm_id}) administered in multiple booklets (anchor item) -> directly comparable across grades.",
            })
    # flag oplm_names that map to more than one distinct oplm_id, so downstream
    # users know that filtering/joining by oplm_name alone is unsafe for these
    ambiguous_names = (
        da_items.dropna(subset=["oplm_name", "oplm_id"])
        .groupby("oplm_name")["oplm_id"].nunique()
    )
    ambiguous_names = sorted(ambiguous_names[ambiguous_names > 1].index)
    if ambiguous_names:
        report(f"\nWARNING: {len(ambiguous_names)} oplm_name codes map to multiple distinct "
               f"oplm_id/items (item map reused the label for different questions). "
               f"These are grouped by (oplm_name, oplm_id) above, NOT oplm_name alone. "
               f"description_en for these is also unreliable (translation file only keys "
               f"on oplm_name). Ambiguous names: {', '.join(ambiguous_names)}")
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
