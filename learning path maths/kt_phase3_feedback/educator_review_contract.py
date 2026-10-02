"""Unscored educator review questions for the private synthetic demo.

Recording a review never changes feedback approval, calibration, or mastery.
Reviews bind to the exact feedback message/version shown to the reviewer.
"""

REVIEW_CONTRACT_VERSION = "phase3_educator_review_notes_v1"
REVIEW_FIELDS = [
    {
        "id": "evidence_support",
        "question": "Is the proposed review focus supported by these observed answers?",
        "choices": ["supported", "needs_revision", "uncertain"],
    },
    {
        "id": "method_content",
        "question": (
            "Are the method-review instructions mathematically and pedagogically "
            "appropriate for this assessed content?"),
        "choices": ["appropriate", "needs_revision", "uncertain"],
    },
    {
        "id": "audience_tone",
        "question": "Is the wording clear and appropriate for the intended audience?",
        "choices": ["appropriate", "needs_revision", "uncertain"],
    },
    {
        "id": "taxonomy",
        "question": "Do the question-to-subtopic labels accurately describe the content?",
        "choices": ["appropriate", "needs_revision", "uncertain"],
    },
    {
        "id": "scope",
        "question": (
            "Could the wording be read as an unsupported mastery, misconception, "
            "learning, or fatigue diagnosis?"),
        "choices": ["no_concern", "concern", "uncertain"],
    },
]

REVIEW_NOTICE = (
    "These are unscored review notes, not educator certification or evidence of "
    "learning benefit. Recording a review does not approve learner delivery."
)
