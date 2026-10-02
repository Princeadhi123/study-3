"""Lead-authored draft policy for observed-answer feedback, not mastery.

Priorities are transparent presentation heuristics, not calibrated educational
decisions. Practice instructions require educator review. No provider executes
this module, and no diagnostic model is used.
"""
from fractions import Fraction

POLICY_VERSION = "observed_subtopic_feedback_v2"
SELECTION_SCHEMA = "phase3_evidence_selection_v1"
GENERATION_SCHEMA = "phase3_evidence_opening_v1"
REVIEW_SCHEMA = "phase3_evidence_feedback_review_v1"
REPORT_SCHEMA = "phase3_evidence_feedback_report_v1"
REVIEW_STATUS = "draft_pending_educator_review"
SELECTION_PROMPT_VERSION = "jev_observed_subtopic_selection_v1"
GENERATION_PROMPT_VERSION = "evidence_opening_v1"

SELECTION_INSTRUCTIONS = (
    "Choose one supplied candidate for a private synthetic assessment-feedback "
    "review. Each review candidate names an assessed subtopic with observed "
    "incorrect answers and a fixed draft practice action. Choose a manageable "
    "starting point supported by the counts; prefer repeated observed errors "
    "over an isolated error when otherwise appropriate. Counts from different "
    "questions or halves do not establish learning, decline, fatigue, mastery, "
    "a misconception, or prerequisites. Do not invent a candidate, claim an "
    "optimal learning path, or treat the presentation priority as validated "
    "educational need. All candidates and actions await educator review. "
    "Return only the permitted candidate choice."
)

SCOPE = (
    "These results describe answers on this assessment, not overall mastery. "
    "The suggested review step is a draft, not a diagnosis."
)
MIDPOINT_TEXT = (
    "You have completed the first part of the assessment. "
    "Take a short break if you want, then continue when you are ready."
)
HALF_LIMITATION = (
    "The two halves contain different questions. This comparison does not "
    "establish learning, improvement, decline, or fatigue."
)
PRIORITY_DESCRIPTION = (
    "Draft display heuristic: most incorrect answers first, then highest "
    "incorrect fraction; exact ties retain taxonomy order for display only. "
    "This is not a validated measure of need, difficulty, or an optimal path."
)

# These teach a general method; they do not claim to explain the student's error.
PRACTICE_ACTIONS = {
    "arithmetic_order_of_operations": (
        "Review one worked example of order of operations. Mark brackets first, "
        "then multiplication and division from left to right, then addition "
        "and subtraction from left to right. Try a new expression and explain "
        "the order you used."
    ),
    "arithmetic_division_by_ten": (
        "Use a place-value table to review division by ten. Track how each "
        "digit's place value changes, then try a new number and check by "
        "multiplying your result by ten."
    ),
    "price_subtraction": (
        "Write both prices in the same currency unit. Review a worked "
        "subtraction with decimal places aligned, then try a new pair of "
        "prices and check by addition."
    ),
    "price_unit_price": (
        "Review how to divide total price by quantity to find the price of "
        "one unit. Write the unit beside your answer. Try a new example and "
        "multiply the unit price by the quantity to check the total."
    ),
    "price_proportional_cost": (
        "Review a worked example linking quantity and cost at a fixed unit "
        "price. Find the price of one unit, then multiply by the requested "
        "quantity. Try a new quantity and explain the units."
    ),
    "fractions_compare": (
        "Review how to compare fractions using a common denominator or a "
        "number line. Try a new pair and explain why one is larger, "
        "rather than comparing the numerators alone."
    ),
    "fractions_multiply": (
        "Review a worked multiplication of fractions: multiply the numerators "
        "and the denominators, then simplify. Try a new example and explain "
        "the meaning of the fraction of a quantity."
    ),
    "fractions_divide_whole": (
        "Review dividing a fraction by a nonzero whole number using "
        "multiplication by the reciprocal of that whole number. Try a new "
        "example and multiply the quotient by the divisor to check it."
    ),
    "fractions_multiply_whole": (
        "Review writing a whole number as a fraction with denominator one "
        "before multiplying it by a fraction. Try a new example, simplify "
        "if possible, and explain what the multiplication represents."
    ),
    "fractions_add_same_denominator": (
        "Review adding fractions with the same denominator: add the "
        "numerators and keep the denominator, then simplify if possible. "
        "Try a new example and check it with a drawing."
    ),
    "fractions_add_different_denominators": (
        "Review finding a common denominator and rewriting both fractions "
        "as equivalent fractions before adding. Try a new example and "
        "explain each rewrite."
    ),
    "percent_of_number": (
        "Review converting a percentage to a fraction out of one hundred "
        "or a decimal, then multiplying by the whole. Try a new example "
        "and check that you identified the whole correctly."
    ),
    "percent_find_whole": (
        "Review a worked example where a part and its percentage are known. "
        "Express the percentage as a decimal and divide the part by it "
        "when it is nonzero. Check by finding that percentage of your result."
    ),
    "percent_as_multiplier": (
        "Review expressing a percentage as a decimal multiplier by dividing "
        "by one hundred. Try a new percentage and explain how multiplying "
        "by it represents that percentage of a number."
    ),
    "percent_find_rate": (
        "Identify the part and the nonzero whole. Review dividing the part "
        "by the whole and multiplying by one hundred to find the percentage. "
        "Try a new example and state what the whole represents."
    ),
}


def candidate_priority(row):
    return (-row["incorrect"],
            -Fraction(row["incorrect"], row["out_of"]))


def build_candidates(evidence, checkpoint):
    if checkpoint == "midpoint":
        return [{"candidate_id": "neutral", "strategy": "neutral_encouragement",
                 "review_status": REVIEW_STATUS, "focus": None, "action": None}]
    errors = [s for s in evidence["subtopics"] if s["incorrect"]]
    if not errors:
        return [{"candidate_id": "optional_consolidation",
                 "strategy": "optional_consolidation",
                 "review_status": REVIEW_STATUS, "focus": None,
                 "action": (
                     "If you want to continue, choose an assessed topic and "
                     "explain the method for a new example. Ask a teacher to "
                     "check your reasoning before choosing further work."
                 )}]
    candidates = []
    for row in sorted(errors, key=candidate_priority):
        action = PRACTICE_ACTIONS.get(row["subtopic_id"], (
            f"Review one worked example of {row['subtopic_name']}. "
            "Try a new question on the same content and explain each step. "
            "Compare your method with the explanation or ask a teacher "
            "to check it."
        ))
        support = row["correct"] == 0
        if support:
            action = ("Start with a teacher or a worked explanation rather "
                      "than a long set of questions. " + action)
        candidates.append({
            "candidate_id": "review_" + row["subtopic_id"],
            "strategy": "supported_review" if support else "focused_review",
            "review_status": REVIEW_STATUS, "focus": dict(row), "action": action})
    return candidates


def _section(kind, text):
    return {"kind": kind, "text": text}


def _skill_label(row):
    return {
        "Peruslaskutoimitukset": "Arithmetic", "Hinta": "Prices",
        "Murtoluvut": "Fractions", "Prosenttilaskenta": "Percentages",
    }.get(row["skill_name"], row["skill_name"])


def render_message(evidence, audience, checkpoint, candidate, opening=None):
    if checkpoint == "midpoint":
        text = opening or MIDPOINT_TEXT
        return {"sections": [_section("encouragement", text)], "text": text}
    teacher = audience == "teacher"
    total = evidence["total"]
    sections = [_section("completion", opening or (
        "The completed assessment record is ready for review." if teacher
        else "You have completed the assessment."))]
    sections.append(_section("observed_result", (
        f"Observed result: {total['correct']} of {total['out_of']} correct."
        if teacher else
        f"You answered {total['correct']} of {total['out_of']} questions correctly."
    )))
    skills = evidence["skills"]
    values = [s["correct"] for s in skills]
    if total["correct"] == 0:
        sections.append(_section("support", (
            "No correct answers were recorded. Check the student's reasoning "
            "on one example before choosing instruction." if teacher else
            "No correct answers were recorded on this assessment. "
            "A useful next step is to work through one example with support."
        )))
    elif total["incorrect"] == 0:
        sections.append(_section("observed_highlight", (
            "All four assessed areas had 10 of 10 answers correct."
        )))
    elif len(set(values)) == 1:
        sections.append(_section("balanced_result", (
            f"The same number of answers was correct in each assessed area: "
            f"{values[0]} of 10. This does not establish equal understanding."
        )))
    else:
        best = max(values)
        names = ", ".join(_skill_label(s) for s in skills if s["correct"] == best)
        sections.append(_section("observed_highlight", (
            f"The highest observed skill count was {best} of 10 in {names}. "
            "This comparison is limited to the questions answered."
        )))
    if total["incorrect"] == 0:
        sections.append(_section("optional_review", (
            "No incorrect answers were observed. There is no error-based "
            "review priority from this assessment. " + candidate["action"]
        )))
    else:
        focus = candidate["focus"]
        sections.append(_section("review_focus", (
            f"One possible review starting point is {focus['subtopic_name']}: "
            f"{focus['incorrect']} of {focus['out_of']} answers were incorrect "
            "on this assessed content."
        )))
        if focus["out_of"] == 1:
            sections.append(_section("limited_evidence", (
                "Only one question assessed this content. Check a new example "
                "before treating it as a consistent difficulty."
            )))
        else:
            sections.append(_section("limited_evidence", (
                "These answers suggest content to revisit, not the cause "
                "of an error or a confirmed misconception."
            )))
        tied = [s for s in evidence["subtopics"]
                if s["incorrect"] and candidate_priority(s) == candidate_priority(focus)]
        if len(tied) > 1:
            sections.append(_section("tie", (
                f"{len(tied)} review areas share these error-count and "
                "error-fraction values. This is one option, not a uniquely "
                "weakest area."
            )))
        elif sum(s["incorrect"] > 0 for s in evidence["subtopics"]) > 1:
            sections.append(_section("other_options", (
                "Other assessed areas also had incorrect answers. "
                "Start with one area rather than trying to review everything at once."
            )))
        sections.append(_section("next_action", candidate["action"]))
    if teacher:
        halves = evidence["halves"]
        sections.append(_section("half_observations", (
            f"First half: {halves[0]['correct']} of 20 correct. "
            f"Second half: {halves[1]['correct']} of 20 correct. " + HALF_LIMITATION
        )))
        follow_up = (
            "Ask the student to explain a new example in an assessed topic. "
            "Use that explanation to decide what further work is appropriate."
            if candidate["focus"] is None else
            "Ask the student to explain a new example in the proposed review "
            "area. Use that explanation to decide whether the issue concerns "
            "the method, interpretation, calculation, or something else; "
            "the assessment counts alone do not decide this.")
        sections.append(_section("teacher_follow_up", follow_up))
    sections.append(_section("scope", SCOPE))
    return {"sections": sections, "text": "\n\n".join(s["text"] for s in sections)}
