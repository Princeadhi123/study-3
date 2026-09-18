"""L0 -- the complete skill catalog the knowledge graph is built on.

Why this exists at all: the model's skill space is **560 skills**
(`prepared_v2/split_report.json -> n_skills: 560`), but the only file in the
repo carrying human-readable `skill_name`s alongside them,
`reports_v2/item_context.csv`, is deliberately restricted to the
option-bearing exercise families (12,455 of 26,070 items) and therefore
contains just 293 distinct names. Building the knowledge graph off that file
would silently omit roughly half the curriculum -- and since the conformal
gate can raise CONFIDENT_STRUGGLE on any of the 560 skills, the graph query
would hit missing nodes in production.

`kt_interactions_v2_item_level.csv.gz` carries BOTH `skill_id` and
`skill_name` on every row (`skill_id = "skill_" + sha1(casefold(round_name))[:12]`,
see `build_v2_raw_clean.py:make_skill_id`), so one streaming pass recovers
the full, unabridged mapping plus the descriptive statistics the graph's
evidence layers need.

Outputs (all under `artifacts/`):

- `skill_catalog.csv` -- one row per skill: id, human-readable name, the
  model's `skill_vocab` index (so graph nodes align with
  `KTTransformer.skill_embed` rows), interaction/student/item counts,
  observed accuracy, and first/last timestamps.
- `skill_item_map.csv` -- (skill, item) pairs with counts; the bipartite
  layer, and the substrate for the graph's `co_occurrence` edges.
- `student_skill_first_encounter.csv.gz` -- per (student, skill): the
  timestamp of that student's FIRST interaction with the skill, plus their
  labeled-attempt and correct counts. This is what the `curriculum_order`
  evidence layer is computed from (which skills are, in observed practice,
  encountered before which others) and what "has this student mastered A?"
  is measured against when probing the frozen model.

Memory stays flat: the source file is student-sorted (stage 2,
`build_v2_sort.py`, sorts by student/timestamp/attempt/preorder and stage 3
preserves that order), so per-student state is flushed on each student
boundary. Out-of-order students are a hard error rather than a silently
wrong aggregate.

    python build_skill_catalog.py
"""
import argparse
import csv
import gzip
import sys
from collections import defaultdict
from pathlib import Path

import paths

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

REQUIRED_COLUMNS = [
    "student_id", "timestamp", "item_id", "skill_id", "skill_name",
    "correctness", "correctness_available",
]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--interactions", default=str(paths.INTERACTIONS))
    p.add_argument("--vocab", default=str(paths.VOCAB))
    p.add_argument("--out-dir", default=str(paths.ARTIFACTS))
    p.add_argument("--limit-rows", type=int, default=0,
                   help="Stop after N rows (smoke test only; produces a partial catalog).")
    return p.parse_args()


class SkillStats:
    __slots__ = ("n_interactions", "n_labeled", "n_correct", "n_students",
                 "first_seen", "last_seen", "names")

    def __init__(self):
        self.n_interactions = 0
        self.n_labeled = 0
        self.n_correct = 0
        self.n_students = 0
        self.first_seen = None
        self.last_seen = None
        # skill_id is a hash of the CASEFOLDED name, so two spellings
        # differing only in case/whitespace collapse to one id. Keep every
        # surface form seen so the catalog can report the most common one
        # rather than an arbitrary last-write-wins pick.
        self.names = defaultdict(int)


def flush_student(student_id, per_skill, skills, student_rows_out):
    """Fold one finished student's per-skill state into the global stats."""
    for skill_id, (first_ts, last_ts, n_int, n_lab, n_cor) in per_skill.items():
        skills[skill_id].n_students += 1
        student_rows_out.writerow([student_id, skill_id, first_ts, last_ts,
                                   n_int, n_lab, n_cor])


def main():
    args = parse_args()
    src = paths.require(Path(args.interactions),
                        "Run kt_phase1/build_v2_item_level.py (stage 3) first.")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    import json
    skill_vocab = json.loads(Path(args.vocab).read_text(encoding="utf-8"))["skill_vocab"]

    skills = defaultdict(SkillStats)
    skill_items = defaultdict(lambda: [0, 0, 0])  # (skill, item) -> [n_int, n_lab, n_cor]

    seen_students = set()
    cur_student = None
    per_skill = {}
    rows = 0

    student_skill_path = out_dir / "student_skill_first_encounter.csv.gz"
    with gzip.open(src, "rt", encoding="utf-8-sig", newline="") as f, \
            gzip.open(student_skill_path, "wt", encoding="utf-8", newline="") as sf:
        reader = csv.reader(f)
        header = next(reader)
        header = [h.lstrip("\ufeff") for h in header]
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise SystemExit(f"{src} is missing expected column(s): {missing}")
        col = {name: header.index(name) for name in REQUIRED_COLUMNS}
        i_student, i_ts = col["student_id"], col["timestamp"]
        i_item, i_skill, i_name = col["item_id"], col["skill_id"], col["skill_name"]
        i_correct, i_avail = col["correctness"], col["correctness_available"]

        student_rows_out = csv.writer(sf)
        student_rows_out.writerow(["student_id", "skill_id", "first_ts", "last_ts",
                                   "n_interactions", "n_labeled", "n_correct"])

        for row in reader:
            rows += 1
            if args.limit_rows and rows > args.limit_rows:
                break
            if rows % 2_000_000 == 0:
                print(f"  {rows:,} rows, {len(skills)} skills so far", flush=True)

            student = row[i_student]
            if student != cur_student:
                if cur_student is not None:
                    flush_student(cur_student, per_skill, skills, student_rows_out)
                if student in seen_students:
                    raise SystemExit(
                        f"Student {student!r} reappears after its block ended -- the source "
                        f"file is not student-sorted, so per-student aggregates would be "
                        f"wrong. Re-run kt_phase1/build_v2_sort.py (stage 2)."
                    )
                seen_students.add(student)
                cur_student = student
                per_skill = {}

            skill_id = row[i_skill]
            ts = row[i_ts]
            labeled = row[i_avail] == "1"
            correct = 1 if (labeled and row[i_correct] == "1") else 0

            st = skills[skill_id]
            st.n_interactions += 1
            st.n_labeled += labeled
            st.n_correct += correct
            st.names[row[i_name]] += 1
            if st.first_seen is None or ts < st.first_seen:
                st.first_seen = ts
            if st.last_seen is None or ts > st.last_seen:
                st.last_seen = ts

            si = skill_items[(skill_id, row[i_item])]
            si[0] += 1
            si[1] += labeled
            si[2] += correct

            prev = per_skill.get(skill_id)
            if prev is None:
                per_skill[skill_id] = [ts, ts, 1, int(labeled), correct]
            else:
                # Rows are timestamp-ordered within a student, but a defensive
                # min/max costs nothing and keeps the output correct even if
                # that ever stops holding.
                if ts < prev[0]:
                    prev[0] = ts
                if ts > prev[1]:
                    prev[1] = ts
                prev[2] += 1
                prev[3] += int(labeled)
                prev[4] += correct

        if cur_student is not None:
            flush_student(cur_student, per_skill, skills, student_rows_out)

    n_items_per_skill = defaultdict(int)
    for (skill_id, _item) in skill_items:
        n_items_per_skill[skill_id] += 1

    catalog_path = out_dir / "skill_catalog.csv"
    with open(catalog_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["skill_id", "skill_name", "vocab_idx", "n_interactions", "n_labeled",
                    "n_correct", "accuracy", "n_items", "n_students",
                    "first_seen", "last_seen", "name_variants"])
        for skill_id in sorted(skills):
            st = skills[skill_id]
            name = max(st.names.items(), key=lambda kv: (kv[1], kv[0]))[0]
            w.writerow([
                skill_id, name,
                skill_vocab.get(skill_id, ""),  # blank = not in the model's vocab
                st.n_interactions, st.n_labeled, st.n_correct,
                f"{st.n_correct / st.n_labeled:.6f}" if st.n_labeled else "",
                n_items_per_skill[skill_id], st.n_students,
                st.first_seen, st.last_seen,
                len(st.names),
            ])

    map_path = out_dir / "skill_item_map.csv"
    with open(map_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["skill_id", "item_id", "n_interactions", "n_labeled", "n_correct"])
        for (skill_id, item_id) in sorted(skill_items):
            n_int, n_lab, n_cor = skill_items[(skill_id, item_id)]
            w.writerow([skill_id, item_id, n_int, n_lab, n_cor])

    in_vocab = sum(1 for s in skills if s in skill_vocab)
    print(f"\nRows read:            {rows:,}")
    print(f"Students:             {len(seen_students):,}")
    print(f"Distinct skills:      {len(skills):,}  ({in_vocab:,} of them in the model's skill_vocab)")
    print(f"(skill, item) pairs:  {len(skill_items):,}")
    print(f"\nWrote {catalog_path}")
    print(f"Wrote {map_path}")
    print(f"Wrote {student_skill_path}")
    # The model can only reason about skills it has an embedding row for;
    # anything outside skill_vocab resolves to __UNK__ and must be excluded
    # from the graph rather than quietly carried along as a dead node.
    if in_vocab != len(skills):
        print(f"\nNOTE: {len(skills) - in_vocab} skill(s) appear only in rows the sequence "
              f"builder dropped (no known correctness) and have no embedding row. "
              f"They are kept in the catalog with a blank vocab_idx and are excluded "
              f"from the knowledge graph by build_graph.py.")


if __name__ == "__main__":
    main()
