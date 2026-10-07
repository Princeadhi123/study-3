"""Resolve selected targets and baselines once, without loading KT weights."""
import argparse
import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from phase4_common import (
    BANK_DIR, PROTOCOL_VERSION, ROOT, bank_lookup, base_student, fingerprint,
    item_id, load_json, paths, rendering_key, smoothed, student_partition, write_json,
)


def group_blocks(events, lookup, student_code, record_index, k):
    """Group all same-skill test targets BEFORE selected-bank membership filtering."""
    by_skill = defaultdict(list)
    for position, event in enumerate(events):
        if event["split"] in ("test_warm", "test_cold_item"):
            by_skill[event["skill"]].append((position, event))
    result = []
    for skill, members in by_skill.items():
        for start in range(0, len(members) - k + 1, k):
            chunk = members[start:start + k]
            kinds = {e["split"] for _, e in chunk}
            kind = ("warm_only" if kinds == {"test_warm"} else
                    "cold_only" if kinds == {"test_cold_item"} else "mixed")
            selected = [lookup.get(rendering_key(e)) for _, e in chunk]
            result.append({
                "skill_index": skill, "kind": kind,
                "student_code": student_code,
                "fully_selected": all(q is not None for q in selected),
                "target_ids": [f"{record_index}:{p}" for p, _ in chunk],
                "question_ids": [q["question"]["question_id"] if q else None for q in selected],
            })
    return result


def match_ordered_bank(events, ordered_keys):
    if len(events) < len(ordered_keys):
        return 0
    keys = [rendering_key(e) for e in events]
    return sum(keys[start:start + len(ordered_keys)] == ordered_keys
               for start in range(len(keys) - len(ordered_keys) + 1))


def prepare(out_dir):
    if out_dir.exists():
        raise FileExistsError("Refusing to overwrite a replay capture")
    vocab = load_json(paths.VOCAB)
    lookup, questions = bank_lookup(vocab)
    calibration = load_json(paths.ACTIVE_CALIBRATION)
    split_report = load_json(paths.SPLIT_REPORT)
    with np.load(paths.PREDICTIONS, allow_pickle=True) as dump:
        raw_students = dump["students"]
        meta = json.loads(str(dump["meta"]))
    students, excluded = student_partition(raw_students, meta, calibration)
    student_codes = {student: i for i, student in enumerate(students)}
    expected_banks = {
        reg: [key for q in load_json(BANK_DIR / (reg + "_bank_private.json"))["questions"]
              for key, member in lookup.items() if member["question"] == q]
        for reg in ("warm", "cold")}
    ordered_sets = {(member["regime"], member["question"]["skill_id"]): set()
                    for member in lookup.values()}
    for member in lookup.values():
        q = member["question"]
        ordered_sets[(member["regime"], q["skill_id"])].add(q["question_id"])
    out_dir.mkdir(parents=True, exist_ok=False)
    targets, blocks = [], []
    support = {qid: {"n": 0, "correct": 0, "with_history_n": 0,
                     "students": set(), "with_history_students": set()}
               for qid in questions}
    train_global, train_skill, train_rendering = [0, 0], defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    seen_order, seen_students, seen_question, collision_keys = [], set(), set(), Counter()
    split_counts, calibration_labels = Counter(), {"warm": Counter(), "cold": Counter()}
    block_counts, block_students = Counter(), defaultdict(set)
    ordered_40 = {"warm": 0, "cold": 0}
    skill_indices = {vocab["skill_vocab"][q["question"]["skill_id"]] for q in questions.values()}
    subset_count = 0
    with gzip.open(paths.SEQUENCES, "rt", encoding="utf-8") as source, gzip.open(
            out_dir / "replay_windows_private.jsonl.gz", "xt", encoding="utf-8") as windows:
        for record_index, line in enumerate(source):
            record = json.loads(line)
            student = base_student(record["student_id"])
            if student not in student_codes:
                raise ValueError("Sequence student absent from prediction universe")
            if student not in seen_students:
                seen_students.add(student)
                seen_order.append(student)
            code = student_codes[student]
            is_excluded = student in excluded
            events = record["events"]
            if not events or len(events) > split_report["max_seq_len_window"]:
                raise ValueError("Invalid retained window length")
            preceding_n, preceding_correct, positions = Counter(), Counter(), []
            for position, event in enumerate(events):
                split_counts[event["split"]] += 1
                if event["correct"] not in (0, 1):
                    raise ValueError("Non-binary observed label")
                key = rendering_key(event)
                if event["split"] == "train":
                    train_global[0] += event["correct"]
                    train_global[1] += 1
                    train_skill[event["skill"]][0] += event["correct"]
                    train_skill[event["skill"]][1] += 1
                    if key in lookup:
                        train_rendering[key][0] += event["correct"]
                        train_rendering[key][1] += 1
                if event["split"] in ("test_warm", "test_cold_item"):
                    regime = "warm" if event["split"] == "test_warm" else "cold"
                    if is_excluded:
                        calibration_labels[regime][str(event["correct"])] += 1
                    member = lookup.get(key)
                    if member and not is_excluded:
                        q = member["question"]
                        if member["regime"] != regime or item_id(event) != q["item_id"]:
                            raise ValueError("Matched target item/regime differs")
                        expected_item = vocab["item_vocab"].get(q["item_id"], 1)
                        if event["item"] != expected_item or (expected_item == 1) != (regime == "cold"):
                            raise ValueError("Target vocabulary/split inconsistency")
                        qid = q["question_id"]
                        first_key = (code, qid)
                        target = {
                            "target_id": f"{record_index}:{position}", "record_index": record_index,
                            "subset_index": subset_count, "position": position,
                            "student_code": code, "question_id": qid, "skill_id": q["skill_id"],
                            "skill_index": event["skill"], "item_index": event["item"],
                            "regime": regime, "y": event["correct"], "history_n": position,
                            "same_skill_history_n": preceding_n[event["skill"]],
                            "same_skill_history_correct": preceding_correct[event["skill"]],
                            "first_student_question_occurrence": first_key not in seen_question,
                        }
                        seen_question.add(first_key)
                        collision_keys[(code, event["item_instance"], event["skill"],
                                        event["content_text"], event["t"])] += 1
                        targets.append(target)
                        positions.append(position)
                        s = support[qid]
                        s["n"] += 1
                        s["correct"] += event["correct"]
                        s["students"].add(code)
                        if position:
                            s["with_history_n"] += 1
                            s["with_history_students"].add(code)
                preceding_n[event["skill"]] += 1
                preceding_correct[event["skill"]] += event["correct"]
            if not is_excluded:
                for block in group_blocks(events, lookup, code, record_index, 10):
                    if block["skill_index"] not in skill_indices:
                        continue
                    group = (block["skill_index"], block["kind"])
                    block_counts[group] += 1
                    block_students[group].add(code)
                    if block["fully_selected"]:
                        qids = set(block["question_ids"])
                        block["distinct_question_count"] = len(qids)
                        skill_id = next(q["question"]["skill_id"] for q in questions.values()
                                        if vocab["skill_vocab"][q["question"]["skill_id"]] == block["skill_index"])
                        block["skill_id"] = skill_id
                        block["fixed_question_set_bank"] = next(
                            (reg for reg in ("warm", "cold") if qids == ordered_sets[(reg, skill_id)]), None)
                        blocks.append(block)
                for regime in ordered_40:
                    ordered_40[regime] += match_ordered_bank(events, expected_banks[regime])
            if positions:
                windows.write(json.dumps({
                    "student_id": f"research_student_{code}",
                    "source_record_index": record_index,
                    "events": events[:max(positions) + 1],
                }, ensure_ascii=True) + "\n")
                subset_count += 1
            if (record_index + 1) % 10000 == 0:
                print(f"Resolved {record_index + 1} retained windows", flush=True)
    if seen_order != students or record_index + 1 != split_report["sequence_records_written"]:
        raise ValueError("Prepared student/window universe differs from frozen inputs")
    if dict(split_counts) != split_report["split_event_counts"]:
        raise ValueError("Source split totals differ")
    for regime in ("warm", "cold"):
        if dict(calibration_labels[regime]) != calibration["item_level"]["groups"][regime]["n"]:
            raise ValueError("Active calibration membership/label totals differ")
    summaries = []
    for qid, s in support.items():
        counts = {**{k: s[k] for k in ("n", "correct", "with_history_n")},
                  "incorrect": s["n"] - s["correct"], "students": len(s["students"]),
                  "with_history_students": len(s["with_history_students"])}
        saved = questions[qid]["saved_support"]
        if any(counts[k] != saved[k] for k in counts):
            raise ValueError("Resolved exact-rendering support differs from frozen selected review")
        summaries.append({"question_id": qid, "regime": questions[qid]["regime"], **counts})
    for target in targets:
        event_key = next(key for key, member in lookup.items()
                         if member["question"]["question_id"] == target["question_id"])
        prior = smoothed(train_skill[target["skill_index"]], smoothed(train_global))
        target["baseline_global"] = smoothed(train_global)
        target["baseline_skill"] = prior
        target["baseline_rendering"] = smoothed(train_rendering[event_key], prior)
        target["baseline_history"] = (target["same_skill_history_correct"] + 2 * prior) / (
            target["same_skill_history_n"] + 2)
    write_json(out_dir / "targets_private.json", targets)
    write_json(out_dir / "selected_blocks_private.json", blocks)
    write_json(out_dir / "training_baselines.json", {
        "global": train_global, "skill": dict(train_skill),
        "renderings": [{"question_id": member["question"]["question_id"],
                        "counts": train_rendering[key]} for key, member in lookup.items()],
    })
    summary = {
        "targets": len(targets), "unique_students": len({t["student_code"] for t in targets}),
        "captured_windows": subset_count, "selected_blocks": len(blocks),
        "ordered_40_contiguous_record_matches": ordered_40,
        "apparent_repeated_timestamp_rendering_occurrences": sum(n - 1 for n in collision_keys.values()),
        "regimes": {reg: {"events": sum(t["regime"] == reg for t in targets),
                         "students": len({t["student_code"] for t in targets if t["regime"] == reg})}
                    for reg in ("warm", "cold")},
        "per_question_support": summaries,
        "all_test_skill_blocks": [
            {"skill_index": s, "kind": kind, "blocks": n,
             "students": len(block_students[(s, kind)])}
            for (s, kind), n in sorted(block_counts.items())],
        "selected_block_kinds": dict(Counter(b["kind"] for b in blocks)),
    }
    inputs = {k: fingerprint(p) for k, p in (
        ("protocol", ROOT / "PROTOCOL.md"), ("sequences", paths.SEQUENCES),
        ("vocab", paths.VOCAB), ("split_report", paths.SPLIT_REPORT),
        ("checkpoint", paths.CHECKPOINT), ("config", paths.RUN_CONFIG),
        ("predictions", paths.PREDICTIONS), ("calibration", paths.ACTIVE_CALIBRATION),
        ("certificate", BANK_DIR / "optimality_certificate.json"),
        ("preparation_method", Path(__file__)), ("common_method", ROOT / "phase4_common.py"))}
    write_json(out_dir / "replay_manifest.json", {
        "protocol_version": PROTOCOL_VERSION, "status": "targets_prepared_no_new_inference",
        "inputs": inputs, "summary": summary,
        "capture_hashes": {name: fingerprint(out_dir / name) for name in (
            "targets_private.json", "selected_blocks_private.json",
            "replay_windows_private.jsonl.gz", "training_baselines.json")},
        "limitations": ["Retained-window historical replay, not fixed assessment sessions.",
                        "Calibration students excluded as targets; training overlap and checkpoint selection remain.",
                        "Repeated attempts retained; uncertainty must cluster by base student."],
    })
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ("per_question_support", "all_test_skill_blocks")}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    prepare(parser.parse_args().out_dir)
