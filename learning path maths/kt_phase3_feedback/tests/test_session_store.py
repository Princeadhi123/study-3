"""Tests for private session persistence."""
import json
import tempfile
import unittest
from pathlib import Path

import phase3_paths  # noqa: F401 -- installs the Phase 2 import path
from session_store import SessionStore
from tests.helpers import make_bank


class SessionStoreTests(unittest.TestCase):
    def test_create_load_and_save(self):
        bank = make_bank()
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            session = store.create(bank)
            loaded = store.load(session["session_id"])
            self.assertEqual(loaded["question_order"],
                             [q["question_id"] for q in bank["questions"]])
            loaded["status"] = "complete"
            store.save(loaded)
            self.assertEqual(store.load(session["session_id"])["status"],
                             "complete")

    def test_rejects_path_like_session_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            for session_id in ("../x", "x.json", "not-hex"):
                with self.subTest(session_id=session_id):
                    with self.assertRaises(ValueError):
                        store.load(session_id)

    def test_session_file_is_private_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            session = store.create(make_bank())
            data = json.loads((Path(tmp) / f"{session['session_id']}.json")
                              .read_text(encoding="utf-8"))
            self.assertEqual(data["schema"], "phase3_mcq_session_v1")
            self.assertNotIn("answer_index", data)


if __name__ == "__main__":
    unittest.main()
