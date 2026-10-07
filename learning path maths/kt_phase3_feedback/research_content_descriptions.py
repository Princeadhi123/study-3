"""Author-owned v2 task descriptions, derived only from the frozen v1 map.

These describe exercises, not reasons for learner responses. No response data,
scoring, routing, provider calls, or educator approval are introduced.
"""
import copy
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PARENT_CAPTURE = ROOT / "artifacts" / "bounded_content_graph_20261006"
PARENT_HASH = "742ebc235fe526b0830f7909bce543b3ac2aa47b85bbf8f453508e0ba6abd5e4"
FORMATS = {
    "percentage_amount": "short_verbal_prompt",
    "divisibility": "short_verbal_prompt",
    "prime_recognition": "short_verbal_prompt",
    "combine_like_terms": "expression_with_instruction",
    "retain_unlike_terms": "expression_with_instruction",
    "fraction_product": "symbolic_expression",
    "fraction_of_quantity": "word_problem",
    "fraction_times_integer": "symbolic_expression",
}
MATHEMATICAL_TASKS = {
    "percentage_amount": "Interpret the stated percentage and calculate that proportion of the given amount.",
    "divisibility": "Identify which offered integer divides the given whole number without remainder.",
    "prime_recognition": "Identify a prime number among the offered numbers.",
    "combine_like_terms": "Identify matching variable parts and combine their coefficients, accounting for the signs shown.",
    "retain_unlike_terms": "Distinguish unlike variable parts or constants and retain terms that cannot be combined.",
    "fraction_product": "Multiply the numerators and denominators of the two fractions and select an equivalent offered result.",
    "fraction_of_quantity": "Interpret the stated fractional part of a quantity and calculate the corresponding amount.",
    "fraction_times_integer": "Multiply the written fraction by the integer and select an equivalent offered result.",
}
TASK_BOUNDARY = (
    "Task requirements are not separately measured skills. An incorrect choice "
    "does not establish which step was difficult or why the choice was made."
)
SUPPORTS = {
    ("whole_multiplication", "fraction_product"): (
        "standard_procedure_component",
        "The standard fraction-product procedure includes multiplying the numerators and denominators."),
    ("fraction_meaning", "fraction_of_quantity"): (
        "conceptual_support",
        "Interpreting a fractional part can support translating the stated quantity into a calculation."),
    ("signed_arithmetic", "combine_like_terms"): (
        "standard_procedure_component",
        "Combining coefficients in the expressions with signed terms includes signed addition or subtraction."),
    ("integer_division", "divisibility"): (
        "standard_procedure_component",
        "Checking divisibility can involve division or multiplication facts and whether a remainder occurs."),
    ("divisibility", "prime_recognition"): (
        "conceptual_support",
        "Understanding divisors can support applying the definition of a prime number."),
    ("percent_meaning", "percentage_amount"): (
        "conceptual_support",
        "Interpreting percent as a proportion can support calculating a percentage of an amount."),
    ("retain_unlike_terms", "combine_like_terms"): (
        "conceptual_support",
        "Distinguishing variable parts can support decisions about combining terms; these ideas may be learned together."),
}
SUPPORT_BOUNDARY = (
    "This describes a possible procedural or conceptual connection, not a "
    "validated teaching sequence or a requirement for demonstrated prior mastery."
)
PATTERN_LABELS = {
    "add_fraction_components": "Possible approach: add fraction components",
    "replace_sum_with_product": "Possible approach: replace a variable sum with a product",
    "percent_as_addend": "Possible approach: treat the written percentage as an addend",
    "combine_unlike_variables": "Possible approach: combine unlike variable terms",
}
PATTERN_DESCRIPTIONS = {
    "add_fraction_components": "For 1/3 times 1/3, the option 2/6 is consistent with adding the two numerators and the two denominators instead of multiplying them.",
    "replace_sum_with_product": "For x + y, the option xy is consistent with replacing addition by multiplication.",
    "percent_as_addend": "For 10 percent of 100, the option 110 is consistent with calculating 100 + 10 instead of the requested proportion.",
    "combine_unlike_variables": "For 2x + 6y, the option 8xy is consistent with adding the coefficients and joining the unlike variable parts.",
}
PATTERN_BOUNDARY = (
    "The offered option is compatible with this hypothetical approach, but a "
    "selected option alone does not establish the learner's method or cause."
)


def describe_tasks(parent):
    """Enrich a captured map without recomputing or changing its assignments."""
    graph = copy.deepcopy(parent)
    if graph["schema"] != "phase3_bounded_content_graph_v1":
        raise ValueError("Expected the frozen v1 descriptive map")
    graph["schema"] = "phase3_bounded_content_graph_v2"
    graph["description_boundary"] = TASK_BOUNDARY
    graph["generation_scope"] = {
        "provider_authority_changed": False,
        "student_diagnosis_permitted": False,
        "reading_skill_score_permitted": False,
        "unreviewed_routing_permitted": False,
    }
    graph["review_input"] = {
        "source": "user_supplied_Gemini_comments",
        "type": "automated_LLM_review_not_independent_educator_validation",
        "model_version": None,
        "educator_reviews_completed": 0,
    }
    for exercise in graph["exercises"]:
        concept = exercise["concept_id"]
        exercise["exercise_format"] = FORMATS[concept]
        exercise["mathematical_task"] = MATHEMATICAL_TASKS[concept]
        demands = []
        if exercise["exercise_format"] in (
                "short_verbal_prompt", "expression_with_instruction"):
            demands.append({
                "id": "interpret_short_instruction",
                "description": "Interpret the brief Finnish task wording or instruction.",
                "scope": "task_requirement_not_measured_skill",
            })
        elif exercise["exercise_format"] == "word_problem":
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
        exercise["additional_task_demands"] = demands
        exercise["interpretation_boundary"] = TASK_BOUNDARY
    for node in graph["nodes"]:
        if node["type"] == "possible_error_pattern":
            node["label"] = PATTERN_LABELS[node["id"]]
            node["interpretation_scope"] = "hypothetical_approach_not_observed_cause"
    for annotation in graph["pedagogical_annotations"]:
        if annotation["kind"] == "proposed_prerequisite":
            support_type, description = SUPPORTS[
                (annotation["source"], annotation["target"])]
            annotation["parent_annotation_id"] = annotation["id"]
            annotation["id"] = annotation["id"].replace("prerequisite_", "support_", 1)
            annotation["kind"] = "proposed_support_link"
            annotation["support_type"] = support_type
            annotation["rationale"] = description
            annotation["interpretation_boundary"] = SUPPORT_BOUNDARY
        elif annotation["kind"] == "possible_error_example":
            annotation["rationale"] = PATTERN_DESCRIPTIONS[annotation["target"]]
            annotation["interpretation_boundary"] = PATTERN_BOUNDARY
        else:
            raise ValueError("Unrecognized parent annotation")
    return graph


def build_descriptive_update():
    raw = (PARENT_CAPTURE / "content_graph_private.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != PARENT_HASH:
        raise ValueError("Frozen parent map changed")
    return describe_tasks(json.loads(raw.decode("utf-8")))
