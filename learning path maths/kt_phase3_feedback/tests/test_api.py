"""Tests for the localhost-only Phase 3 JSON API."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import phase3_paths  # noqa: F401 -- installs the Phase 2 import path
from api import make_server
from mcq_service import MCQSessionService
from session_store import SessionStore
from tests.helpers import make_bank, responses


class Phase3APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bank = make_bank()
        self.service = MCQSessionService(
            self.bank, SessionStore(Path(self.tmp.name)))
        self.server = make_server(self.service)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.url + path,
            data=data,
            headers=headers or {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def start_session(self):
        status, payload = self.request("POST", "/sessions")
        self.assertEqual(status, 201)
        return payload

    def test_start_session_and_public_questions(self):
        payload = self.start_session()
        self.assertEqual(payload["half"], 1)
        self.assertEqual(len(payload["questions"]), 20)
        serialized = json.dumps(payload)
        for private in ("answer_index", "content_text", "item_id",
                        "exercise_id", "correct"):
            self.assertNotIn(private, serialized)

        status, questions = self.request(
            "GET", f"/sessions/{payload['session_id']}/questions?half=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(questions["questions"]), 20)

    def test_half_submission_returns_student_feedback_only(self):
        session = self.start_session()
        sid = session["session_id"]
        status, payload = self.request(
            "POST", f"/sessions/{sid}/half-submissions",
            {"half": 1, "responses": responses(self.bank, count=20)})
        self.assertEqual(status, 200)
        self.assertEqual(payload["checkpoint"], "midpoint")
        self.assertEqual(payload["feedback"]["skills"][0]["correct"], 5)
        self.assertNotIn("teacher", json.dumps(payload))
        self.assertNotIn("answer_index", json.dumps(payload))

        status, second = self.request(
            "GET", f"/sessions/{sid}/questions?half=2")
        self.assertEqual(status, 200)
        self.assertEqual(len(second["questions"]), 20)

    def test_full_session_returns_bounded_student_message(self):
        session = self.start_session()
        sid = session["session_id"]
        status, _ = self.request(
            "POST", f"/sessions/{sid}/half-submissions",
            {"half": 1, "responses": responses(self.bank, count=20)})
        self.assertEqual(status, 200)
        status, payload = self.request(
            "POST", f"/sessions/{sid}/half-submissions",
            {"half": 2,
             "responses": responses(self.bank, count=40)[20:]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["checkpoint"], "end")
        self.assertEqual(
            payload["feedback"]["message"],
            "You have completed all 40 questions. Your results by topic "
            "are shown below. Keep practicing the topics shown below.")
        self.assertEqual(payload["feedback"]["message_source"],
                         "deterministic")
        serialized = json.dumps(payload)
        for term in ("answer_index", "selected_text", "question_id",
                     "content_text", "teacher", "conformal"):
            self.assertNotIn(term, serialized)

    def test_out_of_order_response_is_rejected(self):
        session = self.start_session()
        sid = session["session_id"]
        bad = responses(self.bank, count=20)[1]
        status, payload = self.request(
            "POST", f"/sessions/{sid}/responses", bad)
        self.assertEqual(status, 400)
        self.assertIn("required order", payload["error"])

    def test_researcher_routes_require_role_and_expose_diagnostics(self):
        session = self.start_session()
        sid = session["session_id"]
        row = responses(self.bank, count=1)[0]
        status, _ = self.request(
            "POST", f"/sessions/{sid}/responses", row)
        self.assertEqual(status, 200)

        status, denied = self.request(
            "GET", f"/research/sessions/{sid}/diagnostics")
        self.assertEqual(status, 403)
        self.assertIn("researcher", denied["error"])

        status, diagnostics = self.request(
            "GET", f"/research/sessions/{sid}/diagnostics",
            headers={"X-Phase3-Role": "researcher"})
        self.assertEqual(status, 200)
        self.assertTrue(diagnostics["responses"][0]["correct"])
        self.assertNotIn("answer_index", json.dumps(diagnostics))

    def test_unknown_route_and_nonlocal_bind_are_rejected(self):
        session = self.start_session()
        status, _ = self.request("GET", f"/sessions/{session['session_id']}/nope")
        self.assertEqual(status, 404)
        with self.assertRaises(ValueError):
            make_server(self.service, host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
