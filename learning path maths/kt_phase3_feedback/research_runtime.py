"""Read-only bindings for synthetic research sessions and teacher practice.

This module never changes approval metadata. Only assessed v3 concepts enter
the descriptive taxonomy; support links and possible-error annotations do not.
"""
import copy
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

import phase3_paths
from mcq_test import validate_bank_structure
from research_content_graph import DEFAULT_CAPTURE, V3_SCHEMA, load_capture
from research_content_descriptions_v3 import POOL_HASH, POOL_PATH
from schemas import validate_submission
from session_store import bank_fingerprint
from test_feed import build_test_feed

RESEARCH_PROTOCOL = "offline_historical_evaluation_bank"
RESEARCH_STATUS = "independently_math_checked; educator_approval_pending"
TAXONOMY_SCHEMA = "phase3_research_assessment_taxonomy_v1"
TAXONOMY_STATUS = "descriptive_pending_formal_educator_review"
BANK_LABELS = {
    "demo": "Demo bank",
    "warm": "Warm research bank",
    "cold": "Cold research bank",
}
AUGMENTED_EMBEDDINGS = (
    phase3_paths.PHASE2_ARTIFACTS /
    "text_embeddings_v2_synthetic_practice_20261007.npz")


@lru_cache(maxsize=1)
def _capture():
    try:
        graph = load_capture(DEFAULT_CAPTURE)
    except SystemExit as exc:
        raise ValueError("research graph binding is unavailable or changed") from exc
    if graph["schema"] != V3_SCHEMA:
        raise ValueError("research runtime requires graph v3")
    return graph


def _source(source_id):
    graph = _capture()
    return next(s for s in graph["sources"] if s["id"] == source_id)


def _read_bound(path, digest):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("research source hash mismatch")
    return json.loads(raw.decode("utf-8"))


def _bank_path(mode):
    if mode not in ("warm", "cold"):
        raise ValueError("research bank must be warm or cold")
    return (phase3_paths.ARTIFACTS / "shadow_smoke_20261006" /
            f"{mode}_bank_private.json")


@lru_cache(maxsize=2)
def _bound_bank(mode):
    return _read_bound(_bank_path(mode), _source(f"{mode}_bank")["sha256"])


def validate_research_bank(bank):
    if (bank.get("protocol") != RESEARCH_PROTOCOL
            or bank.get("review_status") != RESEARCH_STATUS
            or bank.get("item_regime") not in ("warm", "cold")):
        raise ValueError("explicit pending-review research bank required")
    validate_bank_structure(bank)
    if bank != _bound_bank(bank["item_regime"]):
        raise ValueError("research bank differs from its frozen v3 source")
    return bank["questions"]


def research_questions(bank, half):
    questions = validate_research_bank(bank)
    if type(half) is not int or half not in (1, 2):
        raise ValueError("half must be 1 or 2")
    return [{k: copy.deepcopy(q[k])
             for k in ("question_id", "skill_id", "text", "options")}
            for q in questions[(half - 1) * 20:half * 20]]


def score_research_checkpoint(bank, responses):
    """Observed scoring only, with the same ordered-prefix contract as demo."""
    questions = validate_research_bank(bank)
    if not isinstance(responses, list) or len(responses) not in (20, 40):
        raise ValueError("research checkpoint requires 20 or 40 ordered answers")
    observed = []
    for index, (row, question) in enumerate(zip(responses, questions)):
        selected = validate_submission(row, question, index)
        observed.append({
            "question_id": question["question_id"],
            "skill_id": question["skill_id"],
            "correct": selected == question["answer_index"],
        })
    return build_test_feed(observed, bank["skill_names"])


def research_taxonomy(bank):
    validate_research_bank(bank)
    graph = _capture()
    source_id = f"{bank['item_regime']}_bank"
    exercises = {e["id"]: e for e in graph["exercises"]
                 if e["source_id"] == source_id}
    concepts = {n["id"]: n for n in graph["nodes"]
                if n["type"] == "concept" and n["scope"] == "assessed_content"}
    topics = []
    for sid, name in bank["skill_names"].items():
        groups = {}
        for question in bank["questions"]:
            if question["skill_id"] != sid:
                continue
            ex = exercises[question["question_id"]]
            concept_id = ex["concept_id"]
            matches = [edge for edge in graph["content_edges"]
                       if edge["relation"] == "assesses"
                       and edge["source"] == ex["id"]]
            if (ex["question"] != question or len(matches) != 1
                    or matches[0]["target"] != concept_id):
                raise ValueError("question must have exactly one assessed concept")
            group = groups.setdefault(concept_id, {
                "id": concept_id, "name": concepts[concept_id]["label"],
                "question_ids": [],
            })
            group["question_ids"].append(question["question_id"])
        topics.append({"skill_id": sid, "skill_name": name,
                       "subtopics": list(groups.values())})
    return {
        "schema": TAXONOMY_SCHEMA, "status": TAXONOMY_STATUS,
        "student_feedback_status": "observed_counts_only",
        "bank_fingerprint": bank_fingerprint(bank),
        "bank_identity": source_id,
        "graph_schema": V3_SCHEMA,
        "graph_sha256": hashlib.sha256(
            (DEFAULT_CAPTURE / "content_graph_private.json").read_bytes()).hexdigest(),
        "relation": "is_part_of_not_prerequisite", "topics": topics,
    }


def validate_research_taxonomy(bank, taxonomy):
    if taxonomy != research_taxonomy(bank):
        raise ValueError("research taxonomy must match the v3 assessed concepts")


def load_research_bank(mode):
    bank = copy.deepcopy(_bound_bank(mode))
    taxonomy = research_taxonomy(bank)
    return bank, taxonomy, {
        "bank_mode": mode, "bank_identity": f"{mode}_bank",
        "bank_label": BANK_LABELS[mode],
        "session_mode": "synthetic_research",
        "validation_mode": "hash_bound_research_pending_educator_review",
        "review_status": RESEARCH_STATUS,
        "source_path": _bank_path(mode).relative_to(phase3_paths.PHASE3_ROOT).as_posix(),
        "source_sha256": _source(f"{mode}_bank")["sha256"],
        "bank_sha256": bank_fingerprint(bank),
        "graph_schema": V3_SCHEMA,
        "graph_sha256": taxonomy["graph_sha256"],
        "declared_item_regime": mode,
        "item_encoding": ("frozen_vocabulary_UNK_index_1" if mode == "cold"
                          else "frozen_learned_item_vocabulary"),
        "educator_approved": False,
    }


def load_current_practice_pool():
    pool = _read_bound(POOL_PATH, POOL_HASH)
    graph_questions = [e["question"] for e in _capture()["exercises"]
                       if e["source_id"] == "practice_pool_v2"]
    if len(pool["questions"]) != 24 or pool["questions"] != graph_questions:
        raise ValueError("current practice must match all 24 graph v3 records")
    return pool


def validate_current_practice_pool(pool):
    if pool != load_current_practice_pool():
        raise ValueError("new practice drafts require the frozen 24-question v2 pool")


def require_augmented_embeddings():
    selected = os.environ.get("KT_PHASE2_TEXT_EMBEDDINGS")
    if (not selected or Path(selected).resolve() != AUGMENTED_EMBEDDINGS.resolve()
            or not AUGMENTED_EMBEDDINGS.is_file()):
        raise ValueError(
            "KT-assisted v2 practice requires KT_PHASE2_TEXT_EMBEDDINGS "
            "to select text_embeddings_v2_synthetic_practice_20261007.npz")
    # Phase 2 resolves its environment at import time. Refuse a stale import.
    import paths
    if Path(paths.TEXT_EMBEDDINGS).resolve() != AUGMENTED_EMBEDDINGS.resolve():
        raise ValueError("restart after selecting the augmented embedding table")


def teacher_content_context(bank_mode):
    """Allow-listed task context only; no support/error hypotheses or routing."""
    if bank_mode not in BANK_LABELS:
        raise ValueError("unknown bank mode")
    load_current_practice_pool()
    graph = _capture()
    concepts = {n["id"]: n["label"] for n in graph["nodes"]
                if n["type"] == "concept" and n["scope"] == "assessed_content"}
    sources = {"practice_pool_v2"}
    if bank_mode != "demo":
        sources.add(f"{bank_mode}_bank")
    return {
        "schema": "phase3_teacher_content_context_v1",
        "graph_schema": V3_SCHEMA, "status": TAXONOMY_STATUS,
        "pool_source": "practice_pool_v2_private.json",
        "pool_source_sha256": POOL_HASH, "practice_question_count": 24,
        "used_for_student_advice": False, "used_for_provider_selection": False,
        "requires_educator_review": True, "approves_learner_delivery": False,
        "exercises": [{
            "question_id": e["id"], "source_id": e["source_id"],
            "role": e["role"], "skill_id": e["skill_id"],
            "concept_id": e["concept_id"], "concept_label": concepts[e["concept_id"]],
            "text": e["question"]["text"],
            "options": copy.deepcopy(e["question"]["options"]),
            "mathematical_task": e["mathematical_task"],
            "interpretation_boundary": e["interpretation_boundary"],
        } for e in graph["exercises"] if e["source_id"] in sources],
    }
