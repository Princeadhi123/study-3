"""Layer 3, step 2: malrule context aggregation & prompt formatting.

IMPORTANT scalability note: `hypothesized_step_trace` below is a LOOKUP into
the offline-built, one-time-cost misconception catalog produced by
../../misconception_tagging_distractors/tag_math_distractors.py (see that
folder's MATH_TAGGING.md), i.e. `out/distractor_catalog_final_v2.csv`
joined on (item_id, selected_option_value, is_correct_option=0). That
catalog was built with ~19.5k *offline, batch, one-time* LLM elicit calls
followed by embedding + clustering (no LLM in the loop after that), which is
what makes it viable at 13.9M-interaction scale. This module deliberately
does NOT call an LLM to (re-)diagnose a malrule per student error -- doing
that live, per struggling-student-moment, is exactly the "not scalable"
failure mode: the catalog only needs tagging once per (item, distractor)
pair, which is orders of magnitude fewer than once per interaction. The only
LLM call in the whole online path is the single feedback-generation call in
feedback_client.py, and only for the small subset of interactions that
actually trip a conformal low-mastery alert.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

SYSTEM_INSTRUCTIONS = (
    "You are a metacognitive math coach operating under the MetaCLASS framework. "
    "Do not reveal the exact answer. You must output valid JSON with exactly three keys:\n"
    "- pedagogical_move: Must be one of ['Strategic Hinting', 'Withholding', "
    "'Error Identification', 'Encouraging', 'Probing']. (Use 'Probing' if the "
    "uncertainty metric is high).\n"
    "- latent_diagnosis: A precise logical statement of the procedural misconception.\n"
    "- socratic_feedback: The natural language response to the student."
)

# Above this conformal interval width, the point estimate is not trustworthy
# enough to say "confidently weak" vs. "confidently strong" -- the student's
# behavior on this skill is erratic/inconsistent rather than a stable low
# mastery signal. Chosen relative to alpha=0.10's typical interval widths on
# a well-calibrated ~0.93 AUC model; re-tune against your own calibration
# set's realized widths rather than treating this as universal.
HIGH_UNCERTAINTY_WIDTH_THRESHOLD = 0.35


@dataclass(frozen=True)
class RecentError:
    """One recent incorrect interaction on the target skill."""

    question_text: str
    student_input_string: str
    distractor_selected: str
    item_id: str


def uncertainty_label(interval_width: float, threshold: float = HIGH_UNCERTAINTY_WIDTH_THRESHOLD) -> str:
    """Maps a conformal interval width to the coarse, LLM-facing uncertainty
    category the prompt spec asks for."""
    return "Uncertain/Erratic Behavior" if interval_width >= threshold else "High Confidence of Failure"


def get_recent_incorrect_interactions(
    interactions: pd.DataFrame,
    student_id: str,
    skill_id: str,
    n: int = 3,
    student_col: str = "student_id",
    skill_col: str = "skill_id",
    correct_col: str = "is_correct",
    timestamp_col: str = "timestamp",
) -> list[RecentError]:
    """Pulls the last `n` incorrect interactions a student had on `skill_id`,
    most recent first, out of the item-level interaction log (see
    ../data_v2/kt_interactions_v2_item_level_misconceptions.csv.gz for the
    real schema this is meant to read).
    """
    mask = (
        (interactions[student_col] == student_id)
        & (interactions[skill_col] == skill_id)
        & (interactions[correct_col] == 0)
    )
    recent = interactions.loc[mask].sort_values(timestamp_col, ascending=False).head(n)

    errors = []
    for _, row in recent.iterrows():
        errors.append(
            RecentError(
                question_text=str(row.get("question_text", "")),
                student_input_string=str(row.get("answer_raw", "")),
                distractor_selected=str(row.get("selected_option_value", "")),
                item_id=str(row.get("item_id", "")),
            )
        )
    return errors


def lookup_hypothesized_step_trace(
    error: RecentError,
    misconception_lookup: dict[tuple[str, str], str],
    fallback: str = "No repeatable malrule on file for this distractor (below tagging threshold).",
) -> str:
    """Looks up the offline-discovered misconception label for
    (item_id, distractor_selected) in the tagged catalog (loaded once by the
    caller from out/distractor_catalog_final_v2.csv into a plain dict, e.g.
    `{(row.item_id, row.selected_option_value): row.final_misconception_label
    for row in catalog.itertuples()}`). This is a dict lookup, not an LLM
    call -- the whole point of doing the tagging offline once (see module
    docstring)."""
    return misconception_lookup.get((error.item_id, error.distractor_selected), fallback)


def build_malrule_payload(
    skill_id: str,
    interval_width: float,
    recent_errors: list[RecentError],
    misconception_lookup: dict[tuple[str, str], str],
) -> dict:
    """Assembles the uncertainty-aware JSON payload for Layer 4 (the LLM
    feedback call), per the four required fields."""
    return {
        "target_weak_skill": skill_id,
        "uncertainty_metric": uncertainty_label(interval_width),
        "recent_errors": [
            {
                "question_text": err.question_text,
                "student_input_string": err.student_input_string,
                "distractor_selected": err.distractor_selected,
                "hypothesized_step_trace": lookup_hypothesized_step_trace(err, misconception_lookup),
            }
            for err in recent_errors
        ],
        "system_instructions": SYSTEM_INSTRUCTIONS,
    }
