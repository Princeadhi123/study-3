"""Tests for private session persistence."""
import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_save_retries_transient_winerror32_replace(self):
        # One sharing violation is retried with the same temp file and
        # the save still completes atomically.
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            session = store.create(make_bank())
            session["responses"] = [{"question_id": "q1",
                                     "selected_index": 0,
                                     "correct": True}]
            target = Path(tmp) / f"{session['session_id']}.json"
            real_replace = Path.replace
            attempts = []

            def flaky(source, dest):
                attempts.append(source)
                if len(attempts) == 1:
                    err = PermissionError("sharing violation")
                    err.winerror = 32
                    raise err
                return real_replace(source, dest)

            with mock.patch.object(Path, "replace", autospec=True,
                                   side_effect=flaky), \
                    mock.patch("session_store.sleep") as sleep_mock:
                store.save(session)
            self.assertEqual(len(attempts), 2)
            self.assertIs(attempts[0], attempts[1])
            sleep_mock.assert_called_once_with(0.02)
            self.assertEqual(json.loads(target.read_text("utf-8")),
                             session)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_save_retry_exhaustion_propagates_and_preserves_target(self):
        # A persistent WinError 5 exhausts exactly 6 attempts and 5
        # sleeps, then propagates the same exception leaving the
        # destination bytes untouched.
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            session = store.create(make_bank())
            target = Path(tmp) / f"{session['session_id']}.json"
            before = target.read_bytes()
            session["status"] = "complete"
            err = PermissionError("access denied")
            err.winerror = 5
            with mock.patch.object(Path, "replace", autospec=True,
                                   side_effect=err) as replace_mock, \
                    mock.patch("session_store.sleep") as sleep_mock:
                with self.assertRaises(PermissionError) as caught:
                    store.save(session)
            self.assertIs(caught.exception, err)
            self.assertEqual(replace_mock.call_count, 6)
            self.assertEqual(sleep_mock.call_count, 5)
            self.assertEqual(target.read_bytes(), before)

    def test_save_does_not_retry_unrelated_replace_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            session = store.create(make_bank())
            target = Path(tmp) / f"{session['session_id']}.json"
            before = target.read_bytes()
            session["status"] = "complete"
            for exc in (PermissionError("no winerror"),
                        OSError(5, "input/output error")):
                with self.subTest(exc=exc):
                    with mock.patch.object(Path, "replace", autospec=True,
                                           side_effect=exc) as replace_mock, \
                            mock.patch("session_store.sleep") as sleep_mock:
                        with self.assertRaises(type(exc)):
                            store.save(session)
                    self.assertEqual(replace_mock.call_count, 1)
                    sleep_mock.assert_not_called()
                    self.assertEqual(target.read_bytes(), before)

    def test_load_and_save_hold_store_lock_for_io(self):
        # Reads never hold the target file open across a replace, and
        # both directions of I/O happen under the store lock.
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            session = store.create(make_bank())
            owned = {"read": False, "replace": False}
            real_read = Path.read_text
            real_replace = Path.replace

            def read_spy(path, *args, **kwargs):
                if path.parent == store.root:
                    owned["read"] = (owned["read"]
                                     or store._lock._is_owned())
                return real_read(path, *args, **kwargs)

            def replace_spy(source, target):
                if Path(target).parent == store.root:
                    owned["replace"] = (owned["replace"]
                                        or store._lock._is_owned())
                return real_replace(source, target)

            with mock.patch.object(Path, "read_text", autospec=True,
                                   side_effect=read_spy), \
                    mock.patch.object(Path, "replace", autospec=True,
                                      side_effect=replace_spy):
                store.load(session["session_id"])
                store.save(session)
            self.assertTrue(owned["read"])
            self.assertTrue(owned["replace"])

    def test_concurrent_save_and_load_stay_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            session = store.create(make_bank())
            sid = session["session_id"]

            def worker(index):
                doc = store.load(sid)
                doc["responses"] = [{"question_id": f"q{index}",
                                     "selected_index": 0,
                                     "correct": True}]
                store.save(doc)
                return store.load(sid)

            with concurrent.futures.ThreadPoolExecutor(4) as pool:
                results = list(pool.map(worker, range(30)))
            for result in results:
                self.assertEqual(result["session_id"], sid)
            self.assertEqual(store.load(sid)["session_id"], sid)


if __name__ == "__main__":
    unittest.main()
