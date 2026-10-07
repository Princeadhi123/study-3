"""Session-aware MCQ service for the Phase 3 end-to-end prototype.

This is a service boundary, not a web app. It owns private session state and
calls Phase 2's approved-bank validator/scorer. Students only receive the
public question fields and checkpoint feeds.
"""
import json
from pathlib import Path

from feedback_service import (attach_observed_subtopics, load_assessment_taxonomy,
                              validate_assessment_taxonomy)
from phase3_paths import APPROVED_BANK, SESSIONS, require
from schemas import validate_submission
from session_store import SessionStore, bank_fingerprint, utc_now

from mcq_test import score_checkpoint, student_questions
from research_runtime import research_questions, research_taxonomy, score_research_checkpoint

HALF_LENGTH = 20
FULL_LENGTH = 40


class MCQSessionService:
    def __init__(self, bank: dict, store: SessionStore, style_selector=None,
                 taxonomy: dict | None = None, research_mode: bool = False):
        self.bank = bank
        self.store = store
        self.style_selector = style_selector
        self._question_reader = research_questions if research_mode else student_questions
        self._checkpoint_scorer = score_research_checkpoint if research_mode else score_checkpoint
        # Both calls validate protocol, approval, structure, and prompt
        # uniqueness before a session can be created.
        self._question_reader(self.bank, 1)
        self._question_reader(self.bank, 2)
        self.taxonomy = (taxonomy if taxonomy is not None else
                         research_taxonomy(bank) if research_mode else
                         load_assessment_taxonomy(bank))
        if self.taxonomy is not None:
            validate_assessment_taxonomy(bank, self.taxonomy)

    @classmethod
    def from_default(cls, store: SessionStore | None = None):
        bank = json.loads(require(APPROVED_BANK).read_text(encoding="utf-8"))
        return cls(bank, store or SessionStore(SESSIONS),
                   taxonomy=load_assessment_taxonomy(bank, required=True))

    @classmethod
    def from_bank_path(cls, bank_path: Path, store: SessionStore):
        bank = json.loads(require(Path(bank_path)).read_text(encoding="utf-8"))
        return cls(bank, store)

    def start_session(self) -> dict:
        session = self.store.create(self.bank)
        return {
            "session_id": session["session_id"],
            "half": 1,
            "questions": self._question_reader(self.bank, 1),
        }

    def questions(self, session_id: str, half: int) -> dict:
        session = self._load_current(session_id)
        count = len(session["responses"])
        if half == 1 and count < HALF_LENGTH:
            return {"session_id": session_id, "half": 1,
                    "answered_count": count,
                    "questions": self._question_reader(self.bank, 1)}
        if half == 2 and HALF_LENGTH <= count < FULL_LENGTH:
            return {"session_id": session_id, "half": 2,
                    "answered_count": count,
                    "questions": self._question_reader(self.bank, 2)}
        raise ValueError("requested half is not available in this session state")

    def submit_response(self, session_id: str, row: dict) -> dict:
        session = self._load_current(session_id)
        position = len(session["responses"])
        if position >= FULL_LENGTH or session["status"] == "complete":
            raise ValueError("this test session is already complete")
        question = self.bank["questions"][position]
        selected = validate_submission(row, question, position)
        event = {
            "position": position + 1,
            "question_id": question["question_id"],
            "skill_id": question["skill_id"],
            "selected_index": selected,
            "selected_text": question["options"][selected],
            "correct": selected == question["answer_index"],
            "answered_at": utc_now(),
            "response_time_ms": None,
            "timing_available": False,
            "attempt": 1,
        }
        session["responses"].append(event)
        checkpoint = None
        feed = None
        answered = len(session["responses"])
        if answered in (HALF_LENGTH, FULL_LENGTH):
            checkpoint = "midpoint" if answered == HALF_LENGTH else "end"
            feed = self._checkpoint_scorer(self.bank, self._response_prefix(session))
            if self.taxonomy is not None:
                attach_observed_subtopics(feed, self.bank,
                                          self._response_prefix(session), self.taxonomy)
            from feedback_service import compose_student_message
            message = compose_student_message(feed, self.style_selector)
            feed["student"]["message"] = message["message"]
            feed["student"]["message_source"] = message["message_source"]
            if checkpoint == "end":
                feed["student"]["summary"] = (
                    "All 40 questions answered. These results describe "
                    "your answers on this assessment, not your overall mastery.")
                feed["student"]["total"] = {
                    "correct": feed["teacher"]["total"]["correct"],
                    "out_of": FULL_LENGTH,
                }
            session["checkpoints"][checkpoint] = feed
        session["status"] = "complete" if answered == FULL_LENGTH else "in_progress"
        self.store.save(session)
        return {
            "session_id": session_id,
            "accepted": True,
            "position": answered,
            "checkpoint": checkpoint,
            "feed": feed,
        }

    def submit_half(self, session_id: str, half: int,
                    responses: list[dict]) -> dict:
        if (not isinstance(half, int) or isinstance(half, bool)
                or half not in (1, 2)):
            raise ValueError("half submissions must arrive in order")
        session = self._load_current(session_id)
        expected_start = 0 if half == 1 else HALF_LENGTH
        if len(session["responses"]) != expected_start:
            raise ValueError("half submissions must arrive in order")
        if not isinstance(responses, list) or len(responses) != HALF_LENGTH:
            raise ValueError("a half submission must contain exactly 20 rows")
        for offset, row in enumerate(responses):
            validate_submission(row, self.bank["questions"][expected_start + offset],
                                expected_start + offset)
        result = None
        for row in responses:
            result = self.submit_response(session_id, row)
        return result

    def snapshot(self, session_id: str) -> dict:
        session = self._load_current(session_id)
        return {
            "session_id": session_id,
            "status": session["status"],
            "answered_count": len(session["responses"]),
            "checkpoints_available": sorted(session["checkpoints"]),
        }

    def private_record(self, session_id: str) -> dict:
        """Return the private session record for trusted local diagnostics."""
        return self._load_current(session_id)

    def private_response_rows(self, session_id: str) -> list[dict]:
        """Return key-free submitted rows in bank order for the private KT path."""
        return self._response_prefix(self._load_current(session_id))

    def _response_prefix(self, session: dict) -> list[dict]:
        return [{"question_id": event["question_id"],
                 "selected_index": event["selected_index"]}
                for event in session["responses"]]

    def _load_current(self, session_id: str) -> dict:
        session = self.store.load(session_id)
        if session.get("bank_sha256") != bank_fingerprint(self.bank):
            raise ValueError("session was created against a different bank")
        return session
