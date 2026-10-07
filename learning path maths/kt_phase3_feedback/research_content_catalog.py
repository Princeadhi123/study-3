"""Authored content mappings and unapproved pedagogical hypotheses.

Offline only. No student diagnosis, routing, model inference, or approval.
Mapping rules cover the frozen research capture and reject unfamiliar stems.
"""
import copy
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CAPTURE = ROOT / "artifacts" / "shadow_smoke_20261006"
SOURCE_HASHES = {
    "warm_bank_private.json": "a45ab6b80105596e1b08ab734894aaf3ce614079185345926bab80cb59280139",
    "cold_bank_private.json": "fc92f4f869da2ef92e02172bb952cf7f7520d435760277881b8137263cb8dec2",
    "practice_pool_private.json": "7b83e6118e492c4b648be89a23fb16f6e2894aae5202fcbdf10595fd6fd34b18",
}
PERCENT = "skill_3697ce51cadb"
NUMBER = "skill_8aaddb61bde0"
ALGEBRA = "skill_b986b3d2a5d8"
FRACTION = "skill_e8d49a51337a"
CONCEPTS = [
    ("percentage_amount", "Calculate a percentage of an amount", PERCENT),
    ("divisibility", "Identify an offered divisor of a whole number", NUMBER),
    ("prime_recognition", "Identify a prime among offered numbers", NUMBER),
    ("combine_like_terms", "Combine coefficients of like terms", ALGEBRA),
    ("retain_unlike_terms", "Recognize terms that cannot be combined", ALGEBRA),
    ("fraction_product", "Multiply two explicitly written fractions", FRACTION),
    ("fraction_of_quantity", "Find a fractional part of a whole quantity", FRACTION),
    ("fraction_times_integer", "Multiply a fraction by an integer", FRACTION),
    ("whole_multiplication", "Whole-number multiplication", None),
    ("integer_division", "Whole-number divisibility and division", None),
    ("fraction_meaning", "Meaning of numerator, denominator and fractional part", None),
    ("signed_arithmetic", "Addition and subtraction of signed coefficients", None),
    ("percent_meaning", "Percent as a proportion out of one hundred", None),
]
UNLIKE_STEMS = {
    "Sievenna 3x - 1", "Sievenna x + y", "Sievenna 2x + 6y",
    "Sievenna x - y", "Sievenna -2x - y", "Sievenna -4x + 3",
}
LIKE_STEMS = {
    "Sievenna lauseke: 5v + 2v", "Sievenna lauseke: 3x + 4x",
    "Sievenna 2x - 4x", "Sievenna 3x + 5x", "Sievenna x + 6x",
    "Sievenna -5x + 2x", "Sievenna -3x - 2x", "Sievenna -3x + x",
    "Sievenna 4x - 3x", "Sievenna -x - 3x", "Sievenna 6x - x",
    "Sievenna 8x - 4x", "Sievenna 5x + 2x", "Sievenna 5x + 5x",
}
QUANTITY_QUESTION = "20844__p3__qe45703b1ec35d645__o77dd23a420c1a944"


def normalized_stem(text):
    return " ".join(text.replace("\\n", " ").replace("\u2212", "-")
                    .replace("\u00e4", "a").split())


def concept_for(question):
    """Closed, content-checked rules; never silently classify a new exercise."""
    sid, stem = question["skill_id"], normalized_stem(question["text"])
    if sid == PERCENT and re.fullmatch(r"\d+ % luvusta \d+", stem):
        return "percentage_amount"
    if sid == NUMBER:
        if stem == "Alkuluku?":
            return "prime_recognition"
        if re.fullmatch(r"\d+ on jaollinen luvulla\.\.\.", stem):
            return "divisibility"
    if sid == ALGEBRA:
        if stem in UNLIKE_STEMS:
            return "retain_unlike_terms"
        if stem in LIKE_STEMS:
            return "combine_like_terms"
    if sid == FRACTION:
        if question["question_id"] == QUANTITY_QUESTION:
            return "fraction_of_quantity"
        fraction = r"\d+[\u2044/]\d+"
        if re.fullmatch(fraction + r"\s*[\u00b7*]\s*" + fraction, stem):
            return "fraction_product"
        if re.fullmatch(fraction + r"\s*\*\s*\d+", stem):
            return "fraction_times_integer"
    raise ValueError("Unmapped content: " + question["question_id"])


PREREQUISITES = [
    ("whole_multiplication", "fraction_product",
     "The standard product procedure multiplies numerators and denominators."),
    ("fraction_meaning", "fraction_of_quantity",
     "Interpreting a fractional part supports translating the quantity problem."),
    ("signed_arithmetic", "combine_like_terms",
     "These assessed expressions include positive and negative coefficients."),
    ("integer_division", "divisibility",
     "The task asks which offered integer divides the given number."),
    ("divisibility", "prime_recognition",
     "Prime recognition can use the definition involving positive divisors."),
    ("percent_meaning", "percentage_amount",
     "Interpreting the percentage supports calculating a proportion of the amount."),
    ("retain_unlike_terms", "combine_like_terms",
     "Identifying matching variable parts supports deciding which terms to combine."),
]
ERROR_EXAMPLES = [
    ("add_fraction_components", "Adding fraction components instead of multiplying",
     "39343__p10__qd531934ff4d9daa0__o1d81addd66e9ce25", "2\u20446",
     "For 1/3 times 1/3, adding both components produces 2/6 rather than 1/9."),
    ("replace_sum_with_product", "Replacing a sum of variables by a product",
     "512961__p2__q5f787a330af2b755__o03da091c4f2e8d2f", "xy",
     "For x + y, the option xy changes addition into multiplication."),
    ("percent_as_addend", "Adding the written percentage number to the amount",
     "28727__p11__q84a4324415b23825__od1b02122e779a361", "110",
     "For 10 percent of 100, 110 equals 100 + 10 rather than the required 10."),
    ("combine_unlike_variables", "Combining unlike variable terms into one term",
     "512961__p3__q2066178429a236b1__o3026cff1f9f14d42", "8xy",
     "For 2x + 6y, 8xy combines coefficients and changes the variable structure."),
]


def build_catalog(source_dir=CAPTURE):
    """Read hash-bound sources and emit a research-only graph without responses."""
    sources, skills, exercises, edges, lookup = [], {}, [], [], {}
    for filename, expected in SOURCE_HASHES.items():
        raw = (Path(source_dir) / filename).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Frozen graph source changed: " + filename)
        data = json.loads(raw.decode("utf-8"))
        skills.update(data.get("skill_names", {}))
        role = "practice" if filename.startswith("practice") else "assessment"
        source_id = filename.removesuffix("_private.json")
        sources.append({"id": source_id, "file": filename, "sha256": expected,
                        "role": role})
        for position, q in enumerate(data["questions"], 1):
            qid = q["question_id"]
            if qid in lookup:
                raise ValueError("Duplicate exercise identity")
            concept = concept_for(q)
            lookup[qid] = q
            exercises.append({
                "id": qid, "type": "exercise", "source_id": source_id,
                "position": position, "role": role, "skill_id": q["skill_id"],
                "concept_id": concept, "question": copy.deepcopy(q),
                "status": "content_mapping_draft_pending_educator_review"})
            edges.append({"source": qid, "target": concept, "relation": "assesses",
                          "status": "content_mapping_draft_pending_educator_review"})
    nodes = [{"id": sid, "type": "skill", "label": name}
             for sid, name in sorted(skills.items())]
    for cid, label, sid in CONCEPTS:
        nodes.append({"id": cid, "type": "concept", "label": label,
                      "scope": "assessed_content" if sid else "supporting_not_assessed"})
        if sid:
            edges.append({"source": cid, "target": sid, "relation": "is_part_of",
                          "status": "content_mapping_draft_pending_educator_review"})
    annotations = []
    for source, target, rationale in PREREQUISITES:
        annotations.append({
            "id": "prerequisite_" + source + "_to_" + target,
            "kind": "proposed_prerequisite", "source": source, "target": target,
            "rationale": rationale, "review_status": "pending",
            "reviewer_id": None, "review_notes": None,
            "used_for_routing": False, "used_for_student_claims": False})
    for eid, label, qid, option, rationale in ERROR_EXAMPLES:
        q = lookup[qid]
        index = q["options"].index(option)
        if index == q["answer_index"]:
            raise ValueError("Possible-error example is an answer key")
        nodes.append({"id": eid, "type": "possible_error_pattern", "label": label,
                      "prevalence": "not_established"})
        annotations.append({
            "id": "error_example_" + eid, "kind": "possible_error_example",
            "source": qid, "target": eid, "option_index": index,
            "option_text": option, "rationale": rationale,
            "alternative_explanations": ["arithmetic_slip", "guessing", "reading_error"],
            "review_status": "pending", "reviewer_id": None, "review_notes": None,
            "used_for_routing": False, "used_for_student_claims": False})
    return {
        "schema": "phase3_bounded_content_graph_v1",
        "scope": "offline_research_only_not_learner_approved",
        "status": "draft_pending_educator_review",
        "used_for_student_advice": False,
        "claim_boundary": "descriptive_content_map_not_mastery_or_misconception_diagnosis",
        "sources": sources, "nodes": nodes, "exercises": exercises,
        "content_edges": edges, "pedagogical_annotations": annotations}
