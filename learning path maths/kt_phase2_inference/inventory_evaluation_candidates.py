"""Inventory held-out answers for selected or all skills, excluding calibration students.

No model inference or bank selection. Scan every retained window without a
bank-item filter; join supported renderings to the existing private MCQ catalog.
"""
import argparse
import csv
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

import paths
from audit_bank_coverage import Counts, SPLITS, TEST, base_student, histogram_summary, item_id, provenance
from build_text_only_bank import BLOCKING_FLAGS
from conformal_calibrate import partition_students
from review_mcq_inventory import normalized


def student_partition(raw_students, prediction_meta, calibration):
    """Reproduce load_predictions' first-seen base-student ordering exactly."""
    if prediction_meta.get("partial") is not False:
        raise ValueError("Prediction dump must explicitly be a complete run")
    for field in ("variant", "seed"):
        if prediction_meta[field] != calibration["model"][field]:
            raise ValueError("Prediction dump and active calibration model differ")
    checkpoint = lambda value: value.replace("\\", "/").rsplit("/", 1)[-1]
    if checkpoint(prediction_meta["checkpoint"]) != checkpoint(
            calibration["model"]["checkpoint"]):
        raise ValueError("Prediction checkpoint differs from active calibration")
    students = list(dict.fromkeys(base_student(str(s)) for s in raw_students))
    protocol = calibration["calibration_protocol"]
    if protocol["split_source"] != "test_warm + test_cold_item, partitioned BY STUDENT":
        raise ValueError("Unsupported calibration partition protocol")
    fraction = protocol["calib_frac"]
    if not 0 < fraction < 1:
        raise ValueError("Calibration fraction must be between zero and one")
    mask = partition_students(len(students), fraction, protocol["seed"])
    if (int(mask.sum()) != protocol["n_calib_students"]
            or int((~mask).sum()) != protocol["n_eval_students"]):
        raise ValueError("Grouped student counts differ from active calibration")
    return students, {s for s, selected in zip(students, mask) if selected}


def load_catalog(inventory, flags_path, skills, vocab):
    """Keep answer keys in the input catalog, not in the candidate outputs."""
    catalog = {}
    seen = set()
    with gzip.open(inventory, "rt", encoding="utf-8") as source, gzip.open(
            flags_path, "rt", encoding="utf-8") as flags:
        for line in source:
            flag_line = flags.readline()
            if not flag_line:
                raise ValueError("Review flags shorter than MCQ inventory")
            q, f = json.loads(line), json.loads(flag_line)
            identity = (q["skill_id"], q["question_id"])
            if identity != (f["skill_id"], f["question_id"]) or identity in seen:
                raise ValueError("Review flags misaligned or duplicate catalog identity")
            seen.add(identity)
            if q["skill_id"] not in skills:
                continue
            instance, marker, digest = q["question_id"].rpartition("__o")
            if not marker or not digest or not q["in_model_catalog"]:
                raise ValueError("Invalid catalog question or model skill")
            if q["content_text"] != q["text"] + " [OPTIONS] " + " | ".join(q["options"]):
                raise ValueError("Catalog content does not match ordered options")
            if item_id({"item_instance": instance}) != q["item_id"]:
                raise ValueError("Catalog source item disagrees with question ID")
            key = (instance, vocab["skill_vocab"][q["skill_id"]], q["content_text"])
            if key in catalog:
                raise ValueError("Ambiguous exact rendering in catalog")
            catalog[key] = {
                "question_id": q["question_id"], "item_id": q["item_id"],
                "skill_id": q["skill_id"], "skill_name": skills[q["skill_id"]],
                "text": q["text"], "options": q["options"],
                "review_flags": f["flags"], "review_status": q["review_status"],
            }
        if flags.readline():
            raise ValueError("Review flags longer than MCQ inventory")
    return catalog


def load_all_skills(path, vocab):
    expected = {skill: index for skill, index in vocab["skill_vocab"].items() if index > 1}
    skills = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"skill_id", "skill_name", "vocab_idx"}.issubset(reader.fieldnames or []):
            raise ValueError("Skill catalog missing required columns")
        for row in reader:
            skill = row["skill_id"]
            if skill not in expected:
                continue
            if (skill in skills or not row["skill_name"].strip()
                    or row["vocab_idx"] != str(expected[skill])):
                raise ValueError("Invalid, duplicate or misindexed model skill")
            skills[skill] = row["skill_name"]
    if set(skills) != set(expected):
        raise ValueError("Skill catalog does not cover every model skill")
    return dict(sorted(skills.items()))


class BlockSupport:
    """Counts only; never retains or emits student/event locators."""
    def __init__(self):
        self.n = 0
        self.students = set()
        self.correct = 0
        self.answers = 0
        self.distinct_rendering_blocks = 0
        self.history = Counter()

    def add(self, chunk, student):
        self.n += 1
        self.students.add(student)
        self.answers += len(chunk)
        self.correct += sum(event["correct"] for _, event, _ in chunk)
        identities = {(e["item_instance"], e["skill"], e["content_text"])
                      for _, e, _ in chunk}
        self.distinct_rendering_blocks += int(len(identities) == len(chunk))
        self.history[chunk[0][0]] += 1

    def export(self):
        return {"n": self.n, "students": len(self.students),
                "answer_occurrences": self.answers, "correct": self.correct,
                "incorrect": self.answers - self.correct,
                "blocks_with_k_distinct_renderings": self.distinct_rendering_blocks,
                "preceding_events_at_block_start": histogram_summary(self.history)}


def add_window_blocks(window_targets, student, k, blocks):
    # Preserve the active calibration's non-overlapping same-skill grouping.
    # Catalog membership is checked AFTER grouping, not used to regroup targets.
    for skill, members in window_targets.items():
        for start in range(0, len(members) - k + 1, k):
            chunk = members[start:start + k]
            splits = {event["split"] for _, event, _ in chunk}
            kind = ("warm_only" if splits == {"test_warm"} else
                    "cold_only" if splits == {"test_cold_item"} else "mixed")
            blocks[(skill, kind, "all_test")].add(chunk, student)
            if all(in_catalog for _, _, in_catalog in chunk):
                blocks[(skill, kind, "fully_catalogued")].add(chunk, student)


def scan(sequences, vocab, split_report, students, excluded, catalog, skills,
         calibration, prediction_meta, raw_students):
    skill_indices = {vocab["skill_vocab"][s]: s for s in skills}
    if len(skill_indices) != len(skills):
        raise ValueError("Requested skills do not have distinct vocabulary indices")
    student_indices = {s: i for i, s in enumerate(students)}
    seen_students = set()
    first_seen = []
    item_counts = defaultdict(Counts)
    rendering_counts = defaultdict(Counts)
    skill_counts = {(s, regime): Counts() for s in skills for regime in ("warm", "cold")}
    mcq_skill_counts = {(s, regime): Counts() for s in skills for regime in ("warm", "cold")}
    checkpoint_k = calibration["checkpoint_level"]["k"]
    if type(checkpoint_k) is not int or checkpoint_k < 1:
        raise ValueError("Active calibration checkpoint k must be a positive integer")
    blocks = defaultdict(BlockSupport)
    global_splits = Counter()
    calibration_labels = {"warm": Counter(), "cold": Counter()}
    evaluation_labels = {"warm": Counter(), "cold": Counter()}
    unmatched_families = Counter()
    records = 0
    excluded_records = 0
    window_ids = raw_students if len(raw_students) == prediction_meta["n_windows"] else None
    with gzip.open(sequences, "rt", encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            student = base_student(record["student_id"])
            if student not in student_indices:
                raise ValueError("Sequence student absent from prediction student universe")
            if window_ids is not None and (records >= len(window_ids) or
                    base_student(str(window_ids[records])) != student):
                raise ValueError("Prediction window ordering differs from sequences")
            if student not in seen_students:
                seen_students.add(student)
                first_seen.append(student)
                if students[len(first_seen) - 1] != student:
                    raise ValueError("First-seen student ordering differs from prediction dump")
            records += 1
            is_excluded = student in excluded
            excluded_records += int(is_excluded)
            events = record["events"]
            if len(events) > split_report["max_seq_len_window"]:
                raise ValueError("Sequence exceeds prepared window length")
            prior_skills = Counter()
            window_targets = defaultdict(list)
            for position, event in enumerate(events):
                split = event["split"]
                if split not in SPLITS or event["correct"] not in (0, 1):
                    raise ValueError("Unknown split or non-binary correctness")
                global_splits[split] += 1
                same_skill = prior_skills[event["skill"]]
                prior_skills[event["skill"]] += 1
                if split not in TEST:
                    continue
                regime = "warm" if split == "test_warm" else "cold"
                labels = calibration_labels if is_excluded else evaluation_labels
                labels[regime][str(event["correct"])] += 1
                if is_excluded or event["skill"] not in skill_indices:
                    continue
                skill = skill_indices[event["skill"]]
                source_item = item_id(event)
                expected_item = vocab["item_vocab"].get(source_item, 1)
                if event["item"] != expected_item or (expected_item == 1) != (regime == "cold"):
                    raise ValueError("Target source item disagrees with vocabulary or split")
                item_counts[(skill, source_item, regime)].add(
                    event, student, position, same_skill)
                skill_counts[(skill, regime)].add(event, student, position, same_skill)
                key = (event["item_instance"], event["skill"], event["content_text"])
                window_targets[skill].append((position, event, key in catalog))
                if key in catalog:
                    rendering_counts[key].add(event, student, position, same_skill)
                    mcq_skill_counts[(skill, regime)].add(
                        event, student, position, same_skill)
                else:
                    unmatched_families[event["exercise_family"]] += 1
            add_window_blocks(window_targets, student, checkpoint_k, blocks)
            if records % 10000 == 0:
                print(f"Scanned {records:,} windows", flush=True)
    if first_seen != students:
        raise ValueError("Sequence student universe differs from prediction dump")
    if (records != split_report["sequence_records_written"]
            or records != prediction_meta["n_windows"]):
        raise ValueError("Sequence window count differs from frozen metadata")
    if dict(global_splits) != split_report["split_event_counts"]:
        raise ValueError("Sequence split counts differ from frozen split report")
    if sum(global_splits[s] for s in ("val", *TEST)) != prediction_meta["n_events"]:
        raise ValueError("Held-out event count differs from prediction dump")
    for regime in ("warm", "cold"):
        for label in ("0", "1"):
            expected = calibration["item_level"]["groups"][regime]["n"][label]
            if calibration_labels[regime][label] != expected:
                raise ValueError("Reconstructed calibration label counts disagree")
    candidates = []
    for key, counts in rendering_counts.items():
        q = catalog[key]
        regime = "warm" if q["item_id"] in vocab["item_vocab"] else "cold"
        candidates.append({**q, "regime": regime, **counts.group(TEST)})
    candidates.sort(key=lambda q: (-q["n"], -q["students"], q["skill_id"], q["question_id"]))
    items = [
        {"skill_id": skill, "skill_name": skills[skill], "item_id": item,
         "regime": regime, **counts.group(TEST)}
        for (skill, item, regime), counts in item_counts.items()
    ]
    items.sort(key=lambda q: (-q["n"], -q["students"], q["skill_id"], q["item_id"]))
    summary = []
    for skill, name in skills.items():
        for regime in ("warm", "cold"):
            group = [q for q in candidates if q["skill_id"] == skill and q["regime"] == regime]
            summary.append({
                "skill_id": skill, "skill_name": name, "regime": regime,
                "all_item_answers": skill_counts[(skill, regime)].group(TEST),
                "catalog_mcq_answers": mcq_skill_counts[(skill, regime)].group(TEST),
                "supported_items": sum(i["skill_id"] == skill and i["regime"] == regime
                                       for i in items),
                "supported_mcq_renderings": len(group),
                "mcq_renderings_with_both_labels": sum(q["correct"] > 0 and q["incorrect"] > 0
                                                     for q in group),
                "mcq_renderings_without_review_flags": sum(not q["review_flags"] for q in group),
                "mcq_renderings_multiple_answers": sum(q["n"] > 1 for q in group),
                "mcq_renderings_multiple_students": sum(q["students"] > 1 for q in group),
                "mcq_renderings_multiple_students_no_blocking_flags": sum(
                    q["students"] > 1 and not BLOCKING_FLAGS.intersection(q["review_flags"])
                    for q in group),
                "max_mcq_answers_per_rendering": max((q["n"] for q in group), default=0),
                "max_mcq_students_per_rendering": max((q["students"] for q in group), default=0),
            })
    checkpoint_support = [
        {"skill_id": skill, "skill_name": name, "k": checkpoint_k,
         "groups": {kind: {scope: blocks[(skill, kind, scope)].export()
                           for scope in ("all_test", "fully_catalogued")}
                    for kind in ("warm_only", "cold_only", "mixed")}}
        for skill, name in skills.items()
    ]
    return {
        "protocol": "calibration_excluded_candidate_availability_v2",
        "partition": {
            **calibration["calibration_protocol"],
            "student_order": "First-seen base student order from complete prediction dump",
            "student_order_sha256": hashlib.sha256(
                json.dumps(students, ensure_ascii=True).encode("utf-8")).hexdigest(),
            "reconstructed_calibration_label_counts": {
                regime: {label: calibration_labels[regime][label] for label in ("0", "1")}
                for regime in ("warm", "cold")},
            "calibration_count_check": "passed for both labels in both regimes",
        },
        "scan": {
            "records": records, "excluded_calibration_records": excluded_records,
            "split_event_counts": dict(global_splits),
            "evaluation_label_counts_all_skills": {
                regime: {label: evaluation_labels[regime][label] for label in ("0", "1")}
                for regime in ("warm", "cold")},
            "target_skill_test_answers_not_in_mcq_catalog_by_family": dict(unmatched_families),
        },
        "skills": skills, "summary": summary,
        "checkpoint_support": checkpoint_support,
        "warm_candidates": [q for q in candidates if q["regime"] == "warm"],
        "cold_candidates": [q for q in candidates if q["regime"] == "cold"],
        "item_coverage": items,
        "limitations": [
            "Availability only: no KT inference, conformal coverage or model-based ranking.",
            "Evaluation students are excluded from conformal calibration, not from KT training.",
            "The checkpoint was selected using val and cold-item metrics; retrospective evidence is selection-informed.",
            "History is preceding retained-window events, including context and skipped cold labels, not lifetime history.",
            "Counts retain repeated attempts; answers are not independent observations or complete assessment sessions.",
            "MCQ candidates require exact item_instance + actual skill label + ordered content match to the private catalog.",
            "Catalog eligibility and automated flags do not establish correctness, text-only solvability or educator approval.",
            "Cold means excluded as training targets; labels may appear in history, and content may occur under other IDs.",
            "Prepared windows are session-split and capped; omitted raw-log events are not inventoried.",
            "No quota, minimum-support gate, embedding compatibility check or bank selection is applied.",
            "Checkpoint blocks use active k, one retained window and actual skill, in target-position order; leftovers are dropped.",
            "Fully catalogued blocks retain the all-test grouping and require every member to match the catalog; they are not a chosen fixed bank.",
            "Mixed blocks contain warm and cold targets; active conformal treats any-cold blocks as cold, not as exclusively cold-item sessions.",
            "Multiple-student and blocking-flag counts are availability descriptors, not approval or statistical-power guarantees.",
        ],
    }


def rank_skills(report):
    summaries = {(s["skill_id"], s["regime"]): s for s in report["summary"]}
    rows = []
    for support in report["checkpoint_support"]:
        skill = support["skill_id"]
        row = {"skill_id": skill, "skill_name": support["skill_name"]}
        for regime in ("warm", "cold"):
            summary = summaries[(skill, regime)]
            counts = summary["catalog_mcq_answers"]
            for output, source in (
                    ("renderings", "supported_mcq_renderings"),
                    ("repeated_student_renderings", "mcq_renderings_multiple_students"),
                    ("repeated_student_renderings_no_blocking_flags",
                     "mcq_renderings_multiple_students_no_blocking_flags")):
                row[regime + "_" + output] = summary[source]
            for field in ("n", "students", "correct", "incorrect"):
                row[regime + "_mcq_" + field] = counts[field]
            row[regime + "_history_median"] = counts["preceding_events_in_window"]["median"]
        row["balanced_repeated_unblocked_renderings"] = min(
            row[regime + "_repeated_student_renderings_no_blocking_flags"]
            for regime in ("warm", "cold"))
        row["balanced_mcq_students"] = min(
            row[regime + "_mcq_students"] for regime in ("warm", "cold"))
        row["checkpoint_k"] = support["k"]
        for kind, scopes in support["groups"].items():
            for scope, counts in scopes.items():
                row[kind + "_" + scope + "_blocks"] = counts["n"]
                row[kind + "_" + scope + "_students"] = counts["students"]
                row[kind + "_" + scope + "_distinct_rendering_blocks"] = (
                    counts["blocks_with_k_distinct_renderings"])
        rows.append(row)
    rows.sort(key=lambda row: (
        -row["balanced_repeated_unblocked_renderings"],
        -row["balanced_mcq_students"],
        -sum(row[r + "_repeated_student_renderings_no_blocking_flags"]
             for r in ("warm", "cold")),
        -sum(row[r + "_mcq_n"] for r in ("warm", "cold")),
        row["skill_id"]))
    return [{"rank": rank, **row} for rank, row in enumerate(rows, 1)]


def prompt_support(report):
    # Do not pool singleton option variants to invent repeated-question support.
    groups = defaultdict(set)
    rendering_counts = Counter()
    for regime in ("warm", "cold"):
        for q in report[regime + "_candidates"]:
            if q["students"] > 1 and not BLOCKING_FLAGS.intersection(q["review_flags"]):
                key = (q["skill_id"], regime)
                groups[key].add(normalized(q["text"]))
                rendering_counts[key] += 1
    rows = []
    for row in report["skill_ranking"]:
        result = {"availability_rank": row["rank"], "skill_id": row["skill_id"],
                  "skill_name": row["skill_name"]}
        for regime in ("warm", "cold"):
            key = (row["skill_id"], regime)
            result[regime + "_distinct_prompts_repeated_nonblocking"] = len(groups[key])
            result[regime + "_renderings_repeated_nonblocking"] = rendering_counts[key]
        result["balanced_distinct_prompts"] = min(
            result[regime + "_distinct_prompts_repeated_nonblocking"]
            for regime in ("warm", "cold"))
        rows.append(result)
    return rows


def reconcile_all_skill_counts(report, coverage):
    for regime in ("warm", "cold"):
        for field, label in (("incorrect", "0"), ("correct", "1")):
            count = sum(s["all_item_answers"][field] for s in report["summary"]
                        if s["regime"] == regime)
            if count != report["scan"]["evaluation_label_counts_all_skills"][regime][label]:
                raise ValueError("All-skill item totals omit evaluation targets")
    totals = {
        "eval_warm": sum(s["groups"]["warm_only"]["all_test"]["n"]
                         for s in report["checkpoint_support"]),
        "eval_cold": sum(s["groups"][kind]["all_test"]["n"]
                         for s in report["checkpoint_support"]
                         for kind in ("cold_only", "mixed")),
    }
    totals["eval_all"] = totals["eval_warm"] + totals["eval_cold"]
    references = {row["label"]: row["n"] for row in coverage["checkpoint_level"]}
    if any(references.get(label) != n for label, n in totals.items()):
        raise ValueError("All-skill checkpoint counts disagree with active coverage report")
    report["scan"]["checkpoint_count_check"] = {
        "status": "passed", "counts": totals,
        "cold_definition": "any cold target, including mixed blocks"}


def write_table(path, rows, renderings):
    identity_fields = ["rank", "regime", "skill_id", "skill_name", "item_id"]
    if renderings:
        identity_fields += ["question_id", "text", "options_json",
                            "review_status", "review_flags_json"]
    count_fields = ["n", "students", "correct", "incorrect", "with_history_n",
                    "with_history_students", "zero_history_n", "with_same_skill_history_n"]
    history_fields = ["history_min", "history_median", "history_max",
                      "same_skill_history_min", "same_skill_history_median",
                      "same_skill_history_max"]
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=identity_fields + count_fields + history_fields)
        writer.writeheader()
        for rank, row in enumerate(rows, 1):
            flat = {field: row[field] for field in identity_fields if field in row}
            flat["rank"] = rank
            if renderings:
                flat["options_json"] = json.dumps(row["options"], ensure_ascii=True)
                flat["review_flags_json"] = json.dumps(row["review_flags"], ensure_ascii=True)
            flat.update({field: row[field] for field in count_fields})
            for stat in ("min", "median", "max"):
                flat["history_" + stat] = row["preceding_events_in_window"][stat]
                flat["same_skill_history_" + stat] = row["preceding_same_skill_events_in_window"][stat]
            writer.writerow(flat)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sequences", type=Path, default=paths.SEQUENCES)
    parser.add_argument("--vocab", type=Path, default=paths.VOCAB)
    parser.add_argument("--split-report", type=Path, default=paths.SPLIT_REPORT)
    parser.add_argument("--predictions", type=Path, default=paths.PREDICTIONS)
    parser.add_argument("--calibration", type=Path, default=paths.ACTIVE_CALIBRATION)
    parser.add_argument("--all-skills", action="store_true",
                        help="Use every model skill from --skill-catalog, ignoring --skill-bank.")
    parser.add_argument("--skill-catalog", type=Path, default=paths.SKILL_CATALOG)
    parser.add_argument("--coverage-report", type=Path, default=paths.ACTIVE_COVERAGE_REPORT,
                        help="All-skills mode checks checkpoint counts, not coverage rates.")
    parser.add_argument("--skill-bank", type=Path, default=paths.ARTIFACTS /
                        "test_question_bank_text_only_approved_v2.json",
                        help="Read only skill_names; never restrict to this bank's item IDs.")
    parser.add_argument("--inventory", type=Path, default=paths.ARTIFACTS / "mcq_inventory_private.jsonl.gz")
    parser.add_argument("--flags", type=Path, default=paths.ARTIFACTS / "mcq_review_flags_private.jsonl.gz")
    parser.add_argument("--out-dir", type=Path, default=paths.ARTIFACTS / "evaluation_candidate_inventory")
    args = parser.parse_args()
    if args.out_dir.exists():
        raise FileExistsError("Refusing to overwrite an existing inventory directory")
    inputs = {key: paths.require(getattr(args, key)) for key in (
        "sequences", "vocab", "split_report", "predictions", "calibration",
        "inventory", "flags")}
    vocab, split_report, calibration = [
        json.loads(inputs[key].read_text(encoding="utf-8"))
        for key in ("vocab", "split_report", "calibration")]
    if args.all_skills:
        inputs["skill_catalog"] = paths.require(args.skill_catalog)
        inputs["coverage_report"] = paths.require(args.coverage_report)
        skills = load_all_skills(inputs["skill_catalog"], vocab)
    else:
        inputs["skill_bank"] = paths.require(args.skill_bank)
        skills = json.loads(inputs["skill_bank"].read_text(encoding="utf-8"))["skill_names"]
        if len(skills) != 4 or any(s not in vocab["skill_vocab"] for s in skills):
            raise ValueError("Expected four model-vocabulary skills")
    with np.load(inputs["predictions"], allow_pickle=True) as data:
        raw_students = data["students"].tolist()
        prediction_meta = json.loads(str(data["meta"]))
    students, excluded = student_partition(raw_students, prediction_meta, calibration)
    catalog = load_catalog(inputs["inventory"], inputs["flags"], skills, vocab)
    report = scan(inputs["sequences"], vocab, split_report, students, excluded, catalog,
                  skills, calibration, prediction_meta, raw_students)
    report["skill_scope"] = "all_model_skills" if args.all_skills else "bank_skills"
    if args.all_skills:
        coverage = json.loads(inputs["coverage_report"].read_text(encoding="utf-8"))
        if coverage["alpha"] != calibration["alpha"]:
            raise ValueError("Reference coverage alpha differs from active calibration")
        reconcile_all_skill_counts(report, coverage)
    report["skill_ranking"] = rank_skills(report)
    report["prompt_support"] = prompt_support(report)
    report["skill_ranking_rule"] = (
        "Descending min(warm,cold) renderings with >1 student and no blocking flags; "
        "then min(warm,cold) catalog students; then total repeated unblocked renderings; "
        "then total catalog answers; then skill ID. Availability only, not model scores, "
        "outcome balance, question approval or power.")
    report["inputs"] = {key: provenance(path) for key, path in inputs.items()}
    report["prediction_metadata"] = prediction_meta
    args.out_dir.mkdir(parents=True, exist_ok=False)
    with (args.out_dir / "availability.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=True, indent=2)
        stream.write("\n")
    write_table(args.out_dir / "warm_candidates.csv", report["warm_candidates"], True)
    write_table(args.out_dir / "cold_candidates.csv", report["cold_candidates"], True)
    write_table(args.out_dir / "item_coverage.csv", report["item_coverage"], False)
    for name in ("skill_ranking", "prompt_support"):
        with (args.out_dir / (name + ".csv")).open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(report[name][0]))
            writer.writeheader()
            writer.writerows(report[name])
    if args.all_skills:
        print(f"Inventoried {len(skills):,} model skills; active checkpoint counts reconciled.")
        for row in report["skill_ranking"][:12]:
            print(f"Rank {row['rank']}: {row['skill_name']}; repeated-student, "
                  f"non-blocking renderings warm={row['warm_repeated_student_renderings_no_blocking_flags']}, "
                  f"cold={row['cold_repeated_student_renderings_no_blocking_flags']}", flush=True)
    else:
        for row in report["summary"]:
            print(f"{row['skill_name']} / {row['regime']}: "
                  f"{row['supported_mcq_renderings']:,} supported MCQ renderings; "
                  f"{row['catalog_mcq_answers']['n']:,} answer occurrences", flush=True)
    print(f"Wrote private aggregate-only inventory to {args.out_dir}")


if __name__ == "__main__":
    main()
