"""Shared paths and frozen protocol settings; no data/model work at import."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
PHASE2 = PROJECT / "kt_phase2_inference"
if str(PHASE2) not in sys.path:
    sys.path.insert(0, str(PHASE2))

import paths
from audit_bank_coverage import base_student, item_id
from audit_stronger_banks import fingerprint
from inventory_evaluation_candidates import student_partition

BANK_DIR = paths.ARTIFACTS / "evaluation_banks_support_optimal_20261005"
PROTOCOL_VERSION = "selected_bank_v3_replay_v1"
SEED = 20261005
BOOTSTRAP_DRAWS = 2000
LOG_EPS = 1e-7
PARITY_TOLERANCE = 1e-4
BATCH_SIZE = 8
CPU_THREADS = 2


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=True, indent=2,
                                     allow_nan=False) + "\n", encoding="utf-8")


def bank_lookup(vocab):
    certificate = load_json(BANK_DIR / "optimality_certificate.json")
    selected = load_json(BANK_DIR / "selected_review_private.json")
    lookup, questions = {}, {}
    for regime in ("warm", "cold"):
        path = BANK_DIR / (regime + "_bank_private.json")
        if fingerprint(path) != certificate["bank_hashes"][regime]:
            raise ValueError("Frozen bank differs from certificate")
        bank = load_json(path)
        for q, reviewed in zip(bank["questions"], selected[regime], strict=True):
            if q != reviewed["question"]:
                raise ValueError("Bank and selected review differ")
            instance, marker, _ = q["question_id"].rpartition("__o")
            if not marker:
                raise ValueError("Invalid source rendering identifier")
            key = (instance, vocab["skill_vocab"][q["skill_id"]], q["content_text"])
            if key in lookup:
                raise ValueError("Ambiguous bank exact-rendering identity")
            lookup[key] = {"regime": regime, "question": q,
                           "saved_support": reviewed["support"]}
            questions[q["question_id"]] = lookup[key]
    return lookup, questions


def rendering_key(event):
    return (event["item_instance"], event["skill"], event["content_text"])


def smoothed(counts, fallback=None):
    correct, n = counts
    return (correct + 1) / (n + 2) if n or fallback is None else fallback
