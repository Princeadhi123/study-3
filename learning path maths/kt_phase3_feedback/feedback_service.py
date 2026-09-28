"""Compose observed MCQ feedback with an explicitly labeled KT estimate."""
import phase3_paths  # adds Phase 2 to sys.path before its modules import `paths`
from kt_adapter import trace_responses
from mcq_test import score_checkpoint


def checkpoint_result(bank: dict, responses: list[dict],
                      include_kt: bool = False, kt=None,
                      device: str = "cpu") -> dict:
    """Return the private scoring result plus an optional frozen-model trace.

    `observed_feed` remains the authoritative feedback from submitted answers.
    `model_estimate` is metadata for a researcher/teacher-facing prototype and
    is deliberately not merged into correctness or mastery text.
    """
    result = {
        "observed_feed": score_checkpoint(bank, responses),
        "model_estimate": {"status": "not_requested"},
    }
    if include_kt:
        result["model_estimate"] = trace_responses(
            bank, responses, kt=kt, device=device)
    return result
