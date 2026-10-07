"""Author-owned v3 capture with the reviewed 24-question practice pool.

Offline only. Transforms the frozen v2 map by replacing its practice source;
it does not alter scoring, responses, routing, provider calls, or approval.
"""
import copy
import hashlib
import json
from pathlib import Path

import research_content_catalog as catalog
import research_content_descriptions as descriptions


ROOT = Path(__file__).resolve().parent
PARENT_CAPTURE = ROOT / "artifacts" / "bounded_content_graph_v2_20261006"
PARENT_HASH = "5b154d44d2b2b9ff775e9415de0bdcf05e275d40bdb0674759873bf1c23a0aed"
POOL_PATH = (ROOT / "artifacts" / "synthetic_practice_supplement_20261006" /
             "practice_pool_v2_private.json")
POOL_HASH = "da12e7195149e1b7d92b2d2363b9b9a7f16de98af003e81387a333ebc77c38c0"
V3_SCHEMA = "phase3_bounded_content_graph_v3"
SOURCE_BUNDLE = "practice_pool_v2_20261007"


def _task_fields(concept):
    demands = []
    exercise_format = descriptions.FORMATS[concept]
    if exercise_format in ("short_verbal_prompt",
                           "expression_with_instruction"):
        demands.append({
            "id": "interpret_short_instruction",
            "description": "Interpret the brief Finnish task wording or instruction.",
            "scope": "task_requirement_not_measured_skill",
        })
    elif exercise_format == "word_problem":
        demands.extend([
            {"id": "interpret_finnish_narrative",
             "description": "Read the Finnish narrative and identify the requested quantity.",
             "scope": "task_requirement_not_measured_skill"},
            {"id": "extract_relevant_quantity",
             "description": "Identify 28 as the stated maximum number of loops.",
             "scope": "task_requirement_not_measured_skill"},
            {"id": "translate_fraction_phrase",
             "description": "Interpret the Finnish quarter phrase as one quarter of 28 and represent it as 28 / 4 or (1/4) * 28.",
             "scope": "task_requirement_not_measured_skill"},
        ])
    return {
        "exercise_format": exercise_format,
        "mathematical_task": descriptions.MATHEMATICAL_TASKS[concept],
        "additional_task_demands": demands,
        "interpretation_boundary": descriptions.TASK_BOUNDARY,
    }


def _new_exercise(question, position):
    concept = catalog.concept_for(question)
    exercise = {
        "id": question["question_id"], "type": "exercise",
        "source_id": "practice_pool_v2", "position": position,
        "role": "practice", "skill_id": question["skill_id"],
        "concept_id": concept, "question": copy.deepcopy(question),
        "status": "content_mapping_draft_pending_educator_review",
    }
    exercise.update(_task_fields(concept))
    return exercise


def build_practice_pool_v2():
    """Return a descriptive v3 graph bound to the 24-question pool."""
    parent_raw = (PARENT_CAPTURE / "content_graph_private.json").read_bytes()
    if hashlib.sha256(parent_raw).hexdigest() != PARENT_HASH:
        raise ValueError("Frozen v2 parent graph changed")
    pool_raw = POOL_PATH.read_bytes()
    if hashlib.sha256(pool_raw).hexdigest() != POOL_HASH:
        raise ValueError("Reviewed 24-question practice pool changed")
    parent = json.loads(parent_raw.decode("utf-8"))
    pool = json.loads(pool_raw.decode("utf-8"))
    if parent.get("schema") != "phase3_bounded_content_graph_v2":
        raise ValueError("Expected the frozen v2 descriptive map")
    if len(pool.get("questions", [])) != 24:
        raise ValueError("Expected 24 practice questions")

    graph = copy.deepcopy(parent)
    graph["schema"] = V3_SCHEMA
    old_practice = [e for e in parent["exercises"]
                    if e["source_id"] == "practice_pool"]
    old_by_id = {e["id"]: e for e in old_practice}
    if len(old_practice) != 20 or len(old_by_id) != 20:
        raise ValueError("Unexpected v2 parent practice source")

    graph["sources"] = [
        dict(source) for source in parent["sources"]
        if source["id"] != "practice_pool"]
    graph["sources"].append({
        "id": "practice_pool_v2",
        "file": "practice_pool_v2_private.json",
        "sha256": POOL_HASH,
        "role": "practice",
    })

    practice_exercises = []
    seen = set()
    for position, question in enumerate(pool["questions"], 1):
        qid = question["question_id"]
        if qid in seen:
            raise ValueError("Duplicate exercise identity")
        seen.add(qid)
        concept = catalog.concept_for(question)
        if qid in old_by_id:
            exercise = copy.deepcopy(old_by_id[qid])
            if (exercise["question"] != question
                    or exercise["concept_id"] != concept
                    or exercise["skill_id"] != question["skill_id"]):
                raise ValueError("Practice record changed in v2 pool")
            exercise["source_id"] = "practice_pool_v2"
            exercise["position"] = position
        else:
            exercise = _new_exercise(question, position)
        practice_exercises.append(exercise)

    graph["exercises"] = [
        copy.deepcopy(exercise) for exercise in parent["exercises"]
        if exercise["source_id"] != "practice_pool"]
    graph["exercises"].extend(practice_exercises)
    old_ids = set(old_by_id)
    graph["content_edges"] = [
        copy.deepcopy(edge) for edge in parent["content_edges"]
        if edge["relation"] != "assesses" or edge["source"] not in old_ids]
    graph["content_edges"].extend({
        "source": exercise["id"], "target": exercise["concept_id"],
        "relation": "assesses",
        "status": "content_mapping_draft_pending_educator_review"}
        for exercise in practice_exercises)
    graph["practice_source_update"] = {
        "from_source_id": "practice_pool",
        "to_source_id": "practice_pool_v2",
        "reason": "four reviewed synthetic divisibility questions added",
        "synthetic_questions_are_historical_student_content": False,
    }
    graph["review_input"]["synthetic_practice_questions"] = {
        "count": 4,
        "user_review": "accepted",
        "formal_educator_review": "pending",
        "historical_student_content": False,
    }
    return graph
