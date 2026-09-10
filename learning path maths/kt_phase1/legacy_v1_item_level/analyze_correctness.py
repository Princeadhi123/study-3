"""How often is Correctness populated (0/1) vs blank, per ExerciseTypeEnum?
This determines which exercise types can even serve as supervised KT targets."""
import csv
import gzip
from collections import Counter

SOURCE = r"C:\Users\pdaadh\Desktop\study 3\learning path maths\mp_grade_6th_2025_7th_2026_spring.csv.gz"

rows = 0
type_total = Counter()
type_correctness_01 = Counter()
type_answer_nonblank = Counter()
type_question_nonblank = Counter()

with gzip.open(SOURCE, "rt", encoding="utf-8-sig", newline="") as f:
    reader = csv.DictReader(f)
    for row in reader:
        rows += 1
        t = row.get("ExerciseTypeEnum", "").strip()
        type_total[t] += 1
        if row.get("Correctness", "").strip() in ("0", "1"):
            type_correctness_01[t] += 1
        if row.get("AnswerJson", "").strip():
            type_answer_nonblank[t] += 1
        if row.get("Question", "").strip():
            type_question_nonblank[t] += 1
        if rows % 3_000_000 == 0:
            print(f"...{rows:,}", flush=True)

print("TOTAL", rows)
for t, c in type_total.most_common():
    corr = type_correctness_01[t]
    ans = type_answer_nonblank[t]
    q = type_question_nonblank[t]
    print(f"{t:32s} n={c:9,d}  correctness_01={corr:9,d} ({corr/c*100:5.1f}%)  answer_nonblank={ans/c*100:5.1f}%  question_nonblank={q/c*100:5.1f}%")
