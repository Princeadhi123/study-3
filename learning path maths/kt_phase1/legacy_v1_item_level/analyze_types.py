"""One-off analysis: for each ExerciseTypeEnum, how often is PossibleAnswersJson
populated, how well does AnswerJson match one of the offered answerValues, and
how many distinct options are typically offered. Streams the full raw gz once.
"""
import csv
import gzip
import json
from collections import Counter, defaultdict

SOURCE = r"C:\Users\pdaadh\Desktop\study 3\learning path maths\mp_grade_6th_2025_7th_2026_spring.csv.gz"

rows = 0
type_counts = Counter()
type_has_options = Counter()
type_option_counts = defaultdict(Counter)
type_answer_matches_option = Counter()
type_answer_parseable_json = Counter()
item_option_sets = defaultdict(set)  # (exercise_id, preorder) -> set of frozenset(option texts)

with gzip.open(SOURCE, "rt", encoding="utf-8-sig", newline="") as f:
    reader = csv.DictReader(f)
    for row in reader:
        rows += 1
        t = row.get("ExerciseTypeEnum", "").strip()
        type_counts[t] += 1
        pa_raw = row.get("PossibleAnswersJson", "")
        ans_raw = row.get("AnswerJson", "")
        if pa_raw and pa_raw != "[]":
            try:
                options = json.loads(pa_raw)
            except json.JSONDecodeError:
                options = None
            if options:
                type_has_options[t] += 1
                type_option_counts[t][len(options)] += 1
                values = {o.get("answerValue") for o in options if isinstance(o, dict)}
                key = (row.get("ExerciseId", ""), row.get("PreOrd", ""))
                item_option_sets[key].add(frozenset(values))
                if ans_raw in values:
                    type_answer_matches_option[t] += 1
        if rows % 2_000_000 == 0:
            print(f"...{rows:,} rows", flush=True)

print("TOTAL ROWS", rows)
print()
for t, c in type_counts.most_common():
    has_opt = type_has_options[t]
    match = type_answer_matches_option[t]
    print(f"{t:32s} n={c:9,d}  has_options={has_opt:9,d} ({has_opt/c*100:5.1f}%)  answer_matches_option={match:9,d} ({(match/has_opt*100 if has_opt else 0):5.1f}% of has_options)  option_count_dist={dict(type_option_counts[t].most_common(5))}")

# How many (exercise_id, preorder) keys have MORE than one distinct option set (i.e. options vary)?
multi = sum(1 for v in item_option_sets.values() if len(v) > 1)
print()
print(f"item slots with options: {len(item_option_sets):,}, of which option set VARIES across occurrences: {multi:,}")
