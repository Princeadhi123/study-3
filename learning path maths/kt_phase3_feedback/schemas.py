"""Shared contracts for the Phase 3 prototype.

Students submit only a question ID and selected option index. The private
service derives selected text and correctness from the approved bank.
"""
PUBLIC_QUESTION_FIELDS = ("question_id", "skill_id", "text", "options")
SUBMISSION_FIELDS = ("question_id", "selected_index")
SESSION_SCHEMA = "phase3_mcq_session_v1"


def validate_submission(row, expected_question: dict, position: int) -> int:
    """Validate one student response against the expected bank row.

    Returns the strict selected option index. Raises ValueError for malformed
    input or a question submitted out of order.
    """
    label = f"responses[{position}]"
    if not isinstance(row, dict) or set(row) != set(SUBMISSION_FIELDS):
        raise ValueError(
            f"{label} must have exactly {SUBMISSION_FIELDS}")
    if row["question_id"] != expected_question["question_id"]:
        raise ValueError(
            f"{label}.question_id does not match the required order")
    selected = row["selected_index"]
    if (not isinstance(selected, int) or isinstance(selected, bool)
            or selected not in range(len(expected_question["options"]))):
        raise ValueError(
            f"{label}.selected_index must name a valid option")
    return selected
