"""HTTP boundary tests for the localhost synthetic demo API.

All requests use urllib against a loopback server bound to an ephemeral
port; no credentials, network egress, or real providers are involved.
"""
import http.client
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import phase3_paths  # noqa: F401 -- installs the Phase 2 import path
from demo_api import make_server
from demo_service import DemoService
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_live_diagnostics import FakeGate
from tests.test_kt_adapter import FakeKT
from live_diagnostics import LiveDiagnostics

PIN = "246810"


def wait_for(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class DemoAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bank = make_bank()
        self.service = DemoService(
            root=Path(self.tmp.name), bank=self.bank,
            taxonomy=make_taxonomy(self.bank),
            diagnostics=LiveDiagnostics(
                model_loader=lambda: FakeKT(self.bank),
                gate_loader=lambda: {"midpoint": FakeGate(5),
                                     "end": FakeGate(10)}))
        self.server = make_server(self.service, PIN)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}"
        self.port = port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.service.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None,
                raw_headers=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers=headers or {})
        if body is not None and not (
                raw_headers and any(
                    k.lower() == "content-type" for k in raw_headers)):
            request.add_header("Content-Type", "application/json")
        if raw_headers:
            for key, value in raw_headers.items():
                request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = response.read()
                meta = dict(response.headers)
                try:
                    return response.status, json.loads(payload), meta
                except json.JSONDecodeError:
                    return response.status, payload, meta
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read()), dict(exc.headers)
            except json.JSONDecodeError:
                return exc.code, None, dict(exc.headers)

    def create_session(self):
        status, payload, _ = self.request(
            "POST", "/api/sessions", {"synthetic": True})
        self.assertEqual(status, 201)
        return payload

    def submit_all(self, sid, token, count=40):
        status = 200
        payload = None
        for row in responses(self.bank, count=count):
            status, payload, _ = self.request(
                "POST", f"/api/sessions/{sid}/responses", row,
                headers={"X-Demo-Session-Token": token})
            self.assertEqual(status, 200)
        return payload

    def unlock(self, pin=PIN):
        return self.request("POST", "/api/teacher/unlock", {"pin": pin})

    def teacher_get(self, path, cookie):
        return self.request("GET", path,
                            headers={"Cookie": f"demo_teacher={cookie}"})

    def cookie_from(self, meta):
        return meta["Set-Cookie"].split(";")[0].split("=", 1)[1]

    # ---------- student surface ----------

    def test_create_requires_exact_attestation(self):
        status, _, _ = self.request("POST", "/api/sessions",
                                    {"synthetic": True})
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/sessions",
                                    {"synthetic": False})
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/sessions",
                                    {"synthetic": True, "name": "x"})
        self.assertEqual(status, 400)

    def test_snapshot_requires_owner_token(self):
        created = self.create_session()
        sid = created["session_id"]
        status, _, _ = self.request("GET", f"/api/sessions/{sid}")
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "GET", f"/api/sessions/{sid}",
            headers={"X-Demo-Session-Token": "bogus"})
        self.assertEqual(status, 403)
        status, snap, _ = self.request(
            "GET", f"/api/sessions/{sid}",
            headers={"X-Demo-Session-Token": created["student_token"]})
        self.assertEqual(status, 200)
        self.assertEqual(snap["session_id"], sid)
        self.assertTrue(snap["synthetic"])
        self.assertEqual(snap["answered_count"], 0)
        self.assertEqual(
            snap["provider_job"]["status"], "not_requested")
        question = snap["current_question"]
        self.assertEqual(
            set(question), {"question_id", "skill_id", "text",
                            "options", "position", "half"})
        blob = json.dumps(snap)
        for marker in ("answer_index", "item_id", "correct",
                       "p_correct", "conformal"):
            self.assertNotIn(marker, blob)

    def test_ordered_submission_and_duplicate(self):
        created = self.create_session()
        sid, token = created["session_id"], created["student_token"]
        row = responses(self.bank, count=1)[0]
        status, snap, _ = self.request(
            "POST", f"/api/sessions/{sid}/responses", row,
            headers={"X-Demo-Session-Token": token})
        self.assertEqual(status, 200)
        self.assertEqual(snap["answered_count"], 1)
        status, snap2, _ = self.request(
            "POST", f"/api/sessions/{sid}/responses", row,
            headers={"X-Demo-Session-Token": token})
        self.assertEqual(status, 200)
        self.assertEqual(snap2["answered_count"], 1)
        out_of_order = responses(self.bank, count=20)[5]
        status, _, _ = self.request(
            "POST", f"/api/sessions/{sid}/responses", out_of_order,
            headers={"X-Demo-Session-Token": token})
        self.assertEqual(status, 400)

    # ---------- transport hardening ----------

    def test_foreign_host_and_origin_rejected(self):
        status, _, _ = self.request(
            "GET", "/api/teacher/config",
            raw_headers={"Host": "evil.example"})
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            "POST", "/api/sessions", {"synthetic": True},
            raw_headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "POST", "/api/sessions", {"synthetic": True},
            raw_headers={
                "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 201)

    def test_origin_requires_explicit_loopback_port(self):
        # A bare http://localhost origin means port 80 and must not be
        # accepted for a server on another port.
        for origin in ("http://localhost",
                       "http://localhost:80",
                       "http://localhost:9999",
                       "http://127.0.0.1:9999",
                       f"http://localhost:{self.port + 1}",
                       f"http://user@127.0.0.1:{self.port}",
                       f"http://127.0.0.1:{self.port}/path",
                       f"http://127.0.0.1:{self.port}?x=1",
                       f"http://127.0.0.1:{self.port}#frag"):
            status, _, _ = self.request(
                "POST", "/api/sessions", {"synthetic": True},
                raw_headers={"Origin": origin})
            self.assertEqual(status, 403, origin)
        status, _, _ = self.request(
            "POST", "/api/sessions", {"synthetic": True},
            raw_headers={"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(status, 201)

    def test_bare_host_header_rejected_on_non80_port(self):
        status, _, _ = self.request(
            "GET", "/", raw_headers={"Host": "localhost"})
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            "GET", "/", raw_headers={"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_response_shape_rejected(self):
        created = self.create_session()
        sid, token = created["session_id"], created["student_token"]
        row = responses(self.bank, count=1)[0]
        self.request("POST", f"/api/sessions/{sid}/responses", row,
                     headers={"X-Demo-Session-Token": token})
        for bad in (
                dict(row, extra=1),
                dict(row, selected_index=True),
                dict(row, selected_index="0"),
                {k: v for k, v in row.items()
                 if k != "question_id"},
                None):
            status, _, _ = self.request(
                "POST", f"/api/sessions/{sid}/responses", bad,
                headers={"X-Demo-Session-Token": token})
            self.assertEqual(status, 400, repr(bad))
        # Identical duplicate is still idempotent.
        status, snap, _ = self.request(
            "POST", f"/api/sessions/{sid}/responses", row,
            headers={"X-Demo-Session-Token": token})
        self.assertEqual(status, 200)
        self.assertEqual(snap["answered_count"], 1)

    def test_body_limit_and_content_type(self):
        big = "x" * (1_048_577)
        request = urllib.request.Request(
            self.url + "/api/sessions", data=big.encode(),
            method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=10)
            self.fail("oversized body accepted")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
        except (ConnectionAbortedError, ConnectionResetError,
                urllib.error.URLError):
            pass  # server closed the oversized upload with a 400
        status, _, _ = self.request(
            "POST", "/api/sessions", {"synthetic": True},
            raw_headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 400)

    def test_error_response_closes_connection_cleanly(self):
        # An error response must end the connection so an undrained
        # request body can never be re-parsed as the next request.
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=10)
        conn.request(
            "POST", "/api/teacher/simulations",
            body=json.dumps({"profile": "alternating", "seed": 1}),
            headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual(response.status, 403)
        self.assertEqual(response.getheader("Connection"), "close")
        response.read()
        conn.close()
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=10)
        conn.request(
            "POST", "/api/teacher/unlock",
            body=json.dumps({"pin": PIN}),
            headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.read())["unlocked"])
        conn.close()

    def test_static_whitelist_and_traversal(self):
        status, body, meta = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Synthetic", body)
        self.assertIn("nosniff", meta.get("X-Content-Type-Options", ""))
        status, body, _ = self.request("GET", "/teacher")
        self.assertEqual(status, 200)
        for path in ("/api/../session_store.py", "/../README.md",
                     "/web_demo/../demo_api.py", "/nonexistent.js"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 404, path)
        status, _, meta = self.request("GET", "/styles.css")
        self.assertEqual(status, 200)

    # ---------- teacher gate ----------

    def test_teacher_routes_require_cookie(self):
        status, _, _ = self.request("GET", "/api/teacher/sessions")
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "GET", "/api/teacher/sessions",
            headers={"X-Phase3-Role": "researcher"})
        self.assertEqual(status, 403)
        status, _, _ = self.unlock("000000")
        self.assertEqual(status, 403)
        status, _, meta = self.unlock()
        self.assertEqual(status, 200)
        cookie = self.cookie_from(meta)
        self.assertIn("HttpOnly", meta["Set-Cookie"])
        self.assertIn("SameSite=Strict", meta["Set-Cookie"])
        status, config, _ = self.teacher_get("/api/teacher/config",
                                             cookie)
        self.assertEqual(status, 200)
        self.assertIn("questions", config)
        self.assertIn("selection_instructions", config)
        blob = json.dumps(config)
        for marker in ("api_key", "endpoint", "token", ".env"):
            self.assertNotIn(marker, blob)
        status, listing, _ = self.teacher_get("/api/teacher/sessions",
                                              cookie)
        self.assertEqual(status, 200)
        status, _, _ = self.request(
            "POST", "/api/teacher/logout", {},
            headers={"Cookie": f"demo_teacher={cookie}"})
        self.assertEqual(status, 200)
        status, _, _ = self.teacher_get("/api/teacher/sessions",
                                        cookie)
        self.assertEqual(status, 403)

    def test_student_token_does_not_unlock_teacher(self):
        created = self.create_session()
        status, _, _ = self.request(
            "GET", "/api/teacher/sessions",
            headers={"X-Demo-Session-Token": created["student_token"]})
        self.assertEqual(status, 403)

    def test_pin_rate_limit(self):
        for _ in range(10):
            status, _, _ = self.unlock("999999")
            self.assertEqual(status, 403)
        status, _, _ = self.unlock("999999")
        self.assertEqual(status, 429)
        status, _, _ = self.unlock(PIN)
        self.assertEqual(status, 429)

    # ---------- review + export flow ----------

    def finish_session(self):
        created = self.create_session()
        sid, token = created["session_id"], created["student_token"]
        self.submit_all(sid, token)
        return sid

    def test_teacher_detail_review_and_export(self):
        sid = self.finish_session()
        status, _, meta = self.unlock()
        cookie = self.cookie_from(meta)
        detail_holder = {}

        def ready():
            status, view, _ = self.teacher_get(
                f"/api/teacher/sessions/{sid}", cookie)
            if status == 200:
                detail_holder["view"] = view
                return (view["provider_job"]["status"]
                        in ("ready", "fallback"))
            return False

        self.assertTrue(wait_for(ready))
        view = detail_holder["view"]
        self.assertEqual(len(view["question_responses"]), 40)
        self.assertIn("answer_index",
                      json.dumps(view["question_responses"]))
        self.assertNotIn("student_token", json.dumps(view))
        self.assertNotIn("token_sha256", json.dumps(view))
        digest = view["review_hashes"]["student"]
        judgments = {f["id"]: f["choices"][0]
                     for f in view["review_contract"]["fields"]}
        status, result, _ = self.request(
            "POST", f"/api/teacher/sessions/{sid}/reviews",
            {"audience": "student", "message_sha256": digest,
             "judgments": judgments, "note": "ok"},
            headers={"Cookie": f"demo_teacher={cookie}"})
        self.assertEqual(status, 201)
        status, conflict, _ = self.request(
            "POST", f"/api/teacher/sessions/{sid}/reviews",
            {"audience": "student", "message_sha256": "0" * 64,
             "judgments": judgments, "note": ""},
            headers={"Cookie": f"demo_teacher={cookie}"})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["current_message_sha256"], digest)
        status, exported, meta = self.teacher_get(
            f"/api/teacher/sessions/{sid}/export", cookie)
        self.assertEqual(status, 200)
        self.assertIn("attachment",
                      meta.get("Content-Disposition", ""))
        self.assertNotIn("demo_teacher", json.dumps(exported))
        self.assertNotIn(cookie, json.dumps(exported))

    def test_simulation_endpoint_creates_session(self):
        status, _, meta = self.unlock()
        cookie = self.cookie_from(meta)
        status, payload, _ = self.request(
            "POST", "/api/teacher/simulations",
            {"profile": "alternating", "seed": 3},
            headers={"Cookie": f"demo_teacher={cookie}"})
        self.assertEqual(status, 201)
        self.assertIn("student_url", payload)
        self.assertIn("student_token", payload)
        snap = payload["snapshot"]
        self.assertEqual(snap["answered_count"], 40)
        status, _, _ = self.request(
            "POST", "/api/teacher/simulations",
            {"profile": "evil", "seed": 1},
            headers={"Cookie": f"demo_teacher={cookie}"})
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            "POST", "/api/teacher/simulations",
            {"profile": "all_correct", "seed": 1})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
