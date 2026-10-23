"""Private JSON session store for the offline 40-question MCQ prototype.

Session files contain selected answers and derived correctness, so they stay
under Phase 3's ignored artifacts directory and are never student payloads.
"""
import hashlib
import json
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import sleep

from schemas import SESSION_SCHEMA

SESSION_ID_RE = re.compile(r"[a-f0-9]{32}\Z")


def replace_session_file(source: Path, target: Path) -> None:
    """Atomically replace `target` with `source`, retrying briefly on
    Windows access/sharing/lock conflicts (WinError 5, 32, 33) that can
    transiently hold the destination open. Unrelated I/O errors are not
    retried. Only the replacement itself is retried, never the write,
    and the destination is never truncated or deleted."""
    for attempt in range(6):
        try:
            source.replace(target)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 5:
                raise
            sleep(0.02 * (2 ** attempt))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def bank_fingerprint(bank: dict) -> str:
    canonical = json.dumps(bank, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SessionStore:
    """Small file-backed store suitable for the local prototype only."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    def create(self, bank: dict) -> dict:
        questions = bank.get("questions", [])
        session = {
            "schema": SESSION_SCHEMA,
            "session_id": secrets.token_hex(16),
            "bank_sha256": bank_fingerprint(bank),
            "created_at": utc_now(),
            "status": "in_progress",
            "question_order": [q["question_id"] for q in questions],
            "responses": [],
            "checkpoints": {},
        }
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(session["session_id"])
        with path.open("x", encoding="utf-8") as stream:
            json.dump(session, stream, ensure_ascii=False, indent=2)
        return session

    def load(self, session_id: str) -> dict:
        with self._lock:
            path = self._path(session_id)
            session = json.loads(path.read_text(encoding="utf-8"))
            if (session.get("schema") != SESSION_SCHEMA
                    or session.get("session_id") != session_id):
                raise ValueError("session file is malformed")
            return session

    def save(self, session: dict) -> None:
        with self._lock:
            if session.get("schema") != SESSION_SCHEMA:
                raise ValueError("session has the wrong schema")
            path = self._path(session.get("session_id"))
            tmp = path.with_name(f"{path.name}.{secrets.token_hex(6)}.tmp")
            with tmp.open("w", encoding="utf-8") as stream:
                json.dump(session, stream, ensure_ascii=False, indent=2)
            replace_session_file(tmp, path)

    def _path(self, session_id) -> Path:
        if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
            raise ValueError("session_id must be a 32-character hex token")
        return self.root / f"{session_id}.json"
