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
from tests.test_kt_adapter import FakeKT
from live_diagnostics import LiveDiagnostics
from tests.test_research_workspace import make_report, judgment
from tests.test_scenario_replays import replay_fixture
from research_workspace import ResearchWorkspace
import secrets

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
        report_path = Path(self.tmp.name) / "report.json"
        report_path.write_text(json.dumps(make_report(2)), encoding="utf-8")
        self.service = DemoService(
            root=Path(self.tmp.name), bank=self.bank,
            taxonomy=make_taxonomy(self.bank), replay_report=report_path,
            diagnostics=LiveDiagnostics(
                model_loader=lambda: FakeKT(self.bank)))
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
        self.service.close(wait=True)
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
            snap = self.service.snapshot(sid, token)
            public_row = {"question_token": snap["current_question"]["question_token"],
                          "selected_index": row["selected_index"]}
            status, payload, _ = self.request(
                "POST", f"/api/sessions/{sid}/responses", public_row,
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
            set(question), {"question_token", "skill_id", "skill_name", "text",
                            "options", "position", "half"})
        blob = json.dumps(snap)
        for marker in ("answer_index", "item_id", "correct",
                       "p_correct", "conformal"):
            self.assertNotIn(marker, blob)

    def test_ordered_submission_and_duplicate(self):
        created = self.create_session()
        sid, token = created["session_id"], created["student_token"]
        row = {"question_token": created["snapshot"]["current_question"]["question_token"],
               "selected_index": self.bank["questions"][0]["answer_index"]}
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

    def test_research_bank_selection_over_http_uses_safe_response_tokens(self):
        from research_runtime import load_research_bank
        status, available, _ = self.request("GET", "/api/banks")
        self.assertEqual(status, 200)
        self.assertEqual({b["bank_mode"] for b in available["banks"]}, {"demo", "warm", "cold"})
        _, _, unlock_meta = self.unlock()
        cookie = self.cookie_from(unlock_meta)
        for mode in ("warm", "cold"):
            bank, _, _ = load_research_bank(mode)
            status, created, _ = self.request(
                "POST", "/api/sessions", {"synthetic": True, "bank_mode": mode})
            self.assertEqual(status, 201)
            sid, token = created["session_id"], created["student_token"]
            snap = created["snapshot"]
            for index, q in enumerate(bank["questions"]):
                self.assertEqual(snap["bank_mode"], mode)
                question = snap["current_question"]
                self.assertNotIn("question_id", question)
                self.assertNotIn("item_id", question)
                chosen = q["answer_index"] if index % 2 == 0 else (q["answer_index"] + 1) % len(q["options"])
                status, snap, _ = self.request(
                    "POST", f"/api/sessions/{sid}/responses",
                    {"question_token": question["question_token"], "selected_index": chosen},
                    headers={"X-Demo-Session-Token": token})
                self.assertEqual(status, 200)
            self.assertIsNone(snap["feedback"]["end"])
            self.assertEqual(snap["feedback_delivery"]["status"],
                             "awaiting_teacher_review")
            self.assertEqual(snap["status"], "complete")
            status, view, _ = self.teacher_get(
                f"/api/teacher/sessions/{sid}", cookie)
            self.assertEqual(status, 200)
            preview_sha = view["feedback_delivery"]["preview_sha256"]
            status, released, _ = self.request(
                "POST", f"/api/teacher/sessions/{sid}/release",
                {"message_sha256": preview_sha},
                headers={"Cookie": f"demo_teacher={cookie}"})
            self.assertEqual(status, 200)
            status, snap, _ = self.request(
                "GET", f"/api/sessions/{sid}",
                headers={"X-Demo-Session-Token": token})
            self.assertEqual(snap["feedback"]["end"]["total"], {"correct": 20, "out_of": 40})
            self.assertEqual(snap["feedback_delivery"]["status"], "released")
        for mode in ("merged", None, [], 40):
            status, _, _ = self.request(
                "POST", "/api/sessions", {"synthetic": True, "bank_mode": mode})
            self.assertEqual(status, 400)

    def test_active_http_projection_filters_archived_fields(self):
        created = self.create_session()
        sid = created["session_id"]
        meta = self.service._load_meta(sid)
        meta["checkpoints"]["midpoint"]["diagnostics"] = {
            "conformal": {"skills": {"private": "output"}},
            "kt": {"provenance": {"midpoint_calibration": {"file": "historical.json"}}}}
        self.service._save_meta(meta)
        _, _, headers = self.unlock()
        cookie = self.cookie_from(headers)
        status, view, _ = self.teacher_get(f"/api/teacher/sessions/{sid}", cookie)
        self.assertEqual(status, 200)
        self.assertNotIn("conformal", json.dumps(view["checkpoints"]))
        self.assertNotIn("calibration", json.dumps(view["checkpoints"]))
        self.assertIn("conformal", self.service._load_meta(sid)["checkpoints"]["midpoint"]["diagnostics"])
        status, library, _ = self.teacher_get("/api/teacher/scenarios", cookie)
        case_id = library["scenarios"][0]["id"]
        status, case, _ = self.teacher_get(f"/api/teacher/scenarios/{case_id}/export", cookie)
        self.assertEqual(status, 200)

        def check_keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertNotIn("conformal", key.lower())
                    self.assertNotIn("calibration", key.lower())
                    check_keys(child)
            elif isinstance(value, list):
                for child in value:
                    check_keys(child)
        check_keys(case)

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
        row = {"question_token": created["snapshot"]["current_question"]["question_token"],
               "selected_index": self.bank["questions"][0]["answer_index"]}
        self.request("POST", f"/api/sessions/{sid}/responses", row,
                     headers={"X-Demo-Session-Token": token})
        for bad in (
                dict(row, extra=1),
                dict(row, selected_index=True),
                dict(row, selected_index="0"),
                {k: v for k, v in row.items()
                 if k != "question_token"},
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

    def test_release_requires_teacher_cookie(self):
        sid = self.finish_session()
        for headers in ({}, {"X-Demo-Session-Token": "x"},
                        {"X-Phase3-Role": "researcher"}):
            status, _, _ = self.request(
                "POST", f"/api/teacher/sessions/{sid}/release",
                {"message_sha256": "0" * 64}, headers=headers)
            self.assertEqual(status, 403, headers)

    def test_release_flow_and_validation_over_http(self):
        created = self.create_session()
        sid, token = created["session_id"], created["student_token"]
        status, _, _ = self.request(
            "POST", f"/api/teacher/sessions/{sid}/release",
            {"message_sha256": "0" * 64},
            headers={"Cookie": "demo_teacher=" + self._fresh_cookie()})
        self.assertEqual(status, 400)
        self.submit_all(sid, token)
        cookie = self._fresh_cookie()

        def ready():
            status, view, _ = self.teacher_get(
                f"/api/teacher/sessions/{sid}", cookie)
            if status == 200 and (
                    view["provider_job"]["status"]
                    in ("ready", "fallback")):
                return view
            return None

        self.assertTrue(wait_for(ready))
        view = ready()
        sha = view["feedback_delivery"]["preview_sha256"]
        status, _, _ = self.request(
            "POST", f"/api/teacher/sessions/{sid}/release",
            {"message_sha256": "0" * 64},
            headers={"Cookie": f"demo_teacher={cookie}"})
        self.assertEqual(status, 409)
        status, _, _ = self.request(
            "POST", f"/api/teacher/sessions/{sid}/release",
            {"message_sha256": sha, "sections": []},
            headers={"Cookie": f"demo_teacher={cookie}"})
        self.assertEqual(status, 400)
        status, result, _ = self.request(
            "POST", f"/api/teacher/sessions/{sid}/release",
            {"message_sha256": sha, "reviewer_label": "ed-1"},
            headers={"Cookie": f"demo_teacher={cookie}"})
        self.assertEqual(status, 200)
        self.assertTrue(result["released"])
        status, snap, _ = self.request(
            "GET", f"/api/sessions/{sid}",
            headers={"X-Demo-Session-Token": token})
        self.assertEqual(snap["feedback_delivery"]["status"], "released")
        end = snap["feedback"]["end"]
        self.assertEqual(
            end["template_version"],
            "phase3_student_feedback_template_v2")
        self.assertEqual([s["title"] for s in end["sections"]],
                         ["Assessment summary", "Observed strengths",
                          "Review focus", "Next steps"])
        self.assertFalse(end["requires_educator_review"])
        blob = json.dumps(snap)
        for marker in ("p_correct", "preview_sha256", "ed-1",
                       "reviewer_label"):
            self.assertNotIn(marker, blob)

    def test_feedback_edits_route_validation_and_save(self):
        created = self.create_session()
        sid, token = created["session_id"], created["student_token"]
        self.submit_all(sid, token)
        sections = {
            "assessment_summary": "Assessment summary. Edited draft.",
            "observed_strengths": "Observed strengths. Edited draft.",
            "review_focus": "Review focus. Edited draft.",
            "next_steps": "Next steps. Edited draft."}
        for headers in ({}, {"X-Demo-Session-Token": "x"}):
            status, _, _ = self.request(
                "POST",
                f"/api/teacher/sessions/{sid}/feedback-edits",
                {"message_sha256": "0" * 64, "sections": sections},
                headers=headers)
            self.assertEqual(status, 403)
        cookie = self._fresh_cookie()

        def ready():
            status, view, _ = self.teacher_get(
                f"/api/teacher/sessions/{sid}", cookie)
            if status == 200 and (
                    view["provider_job"]["status"]
                    in ("ready", "fallback")):
                return view
            return None

        self.assertTrue(wait_for(ready))
        sha = ready()["feedback_delivery"]["preview_sha256"]
        status, _, _ = self.request(
            "POST", f"/api/teacher/sessions/{sid}/feedback-edits",
            {"message_sha256": "0" * 64, "sections": sections},
            headers={"Cookie": f"demo_teacher={cookie}"})
        self.assertEqual(status, 409)
        status, _, _ = self.request(
            "POST", f"/api/teacher/sessions/{sid}/feedback-edits",
            {"message_sha256": sha, "sections": sections,
             "total": {"correct": 40, "out_of": 40}},
            headers={"Cookie": f"demo_teacher={cookie}"})
        self.assertEqual(status, 400)
        status, result, _ = self.request(
            "POST", f"/api/teacher/sessions/{sid}/feedback-edits",
            {"message_sha256": sha, "sections": sections,
             "reviewer_label": "ed-1"},
            headers={"Cookie": f"demo_teacher={cookie}"})
        self.assertEqual(status, 200)
        self.assertTrue(result["saved"])
        status, result, _ = self.request(
            "POST", f"/api/teacher/sessions/{sid}/release",
            {"message_sha256": result["preview_sha256"]},
            headers={"Cookie": f"demo_teacher={cookie}"})
        self.assertEqual(status, 200)
        status, snap, _ = self.request(
            "GET", f"/api/sessions/{sid}",
            headers={"X-Demo-Session-Token": token})
        self.assertEqual(snap["feedback_delivery"]["status"],
                         "released")
        self.assertIn("Edited draft.",
                      snap["feedback"]["end"]["text"])
        self.assertNotIn("ed-1", json.dumps(snap))

    def _fresh_cookie(self):
        _, _, meta = self.unlock()
        return self.cookie_from(meta)

    def test_completed_snapshot_shows_total_without_teacher_cookie(self):
        created = self.create_session()
        sid, token = created["session_id"], created["student_token"]
        payload = self.submit_all(sid, token)
        self.assertEqual(payload["assessment_total"],
                         {"correct": 40, "out_of": 40})
        self.assertIsNone(payload["feedback"]["end"])
        status, snap, _ = self.request(
            "GET", f"/api/sessions/{sid}",
            headers={"X-Demo-Session-Token": token})
        self.assertEqual(status, 200)
        self.assertEqual(snap["assessment_total"],
                         {"correct": 40, "out_of": 40})
        self.assertIsNone(snap["feedback"]["end"])
        self.assertEqual(snap["feedback_delivery"]["status"],
                         "awaiting_teacher_review")

    def test_research_routes_require_teacher_cookie(self):
        for method, path, body in (
                ("GET", "/api/teacher/replays", None),
                ("POST", "/api/teacher/replays", {}),
                ("GET", "/api/teacher/replays/" + "a" * 32, None),
                ("GET", "/api/teacher/replays/" + "a" * 32 + "/export", None),
                ("GET", "/api/teacher/replays/" + "a" * 32 + "/cases/" + "b" * 32, None),
                ("GET", "/api/teacher/replays/" + "a" * 32 + "/cases/" + "b" * 32 + "/export", None),
                ("POST", "/api/teacher/replays/" + "a" * 32 + "/cancel", {}),
                ("GET", "/api/teacher/scenarios", None),
                ("GET", "/api/teacher/scenarios/" + "a" * 32, None),
                ("GET", "/api/teacher/comparisons", None),
                ("POST", "/api/teacher/comparisons", {}),
                ("GET", "/api/teacher/comparisons/" + "a" * 32, None),
                ("GET", "/api/teacher/comparisons/" + "a" * 32 + "/export", None),
                ("POST", "/api/teacher/comparisons/" + "a" * 32 + "/reviews", {})):
            status, _, _ = self.request(method, path, body,
                                        headers={"X-Phase3-Role": "researcher"})
            self.assertEqual(status, 403, path)

    def test_research_library_and_blind_review_over_http(self):
        _, _, meta = self.unlock()
        cookie = self.cookie_from(meta)
        headers = {"Cookie": f"demo_teacher={cookie}"}
        status, library, meta = self.teacher_get("/api/teacher/scenarios", cookie)
        self.assertEqual(status, 200)
        self.assertEqual(library["summary"]["scenarios"], 2)
        self.assertEqual(meta["Cache-Control"], "no-store")
        status, case, _ = self.teacher_get(
            "/api/teacher/scenarios/" + library["scenarios"][0]["id"], cookie)
        self.assertEqual(status, 200)
        self.assertEqual(case["name"], "case_0")
        status, view, _ = self.request("POST", "/api/teacher/comparisons", {
            "reviewer_label": "fixture", "audience": "student", "prior_exposure": False}, headers)
        self.assertEqual(status, 201)
        path = "/api/teacher/comparisons/" + view["id"]
        self.assertNotIn("mapping", json.dumps(view))
        status, _, _ = self.teacher_get(path + "/export", cookie)
        self.assertEqual(status, 409)
        body = judgment(view["task"])
        status, next_view, _ = self.request("POST", path + "/reviews", body, headers)
        self.assertEqual(status, 201)
        self.assertEqual(next_view["completed"], 1)
        status, repeated, _ = self.request("POST", path + "/reviews", body, headers)
        self.assertEqual(repeated, next_view)
        status, _, _ = self.request("POST", path + "/reviews", dict(body, preference="B"), headers)
        self.assertEqual(status, 409)
        status, final, _ = self.request("POST", path + "/reviews", judgment(next_view["task"]), headers)
        self.assertEqual(final["status"], "complete")
        status, exported, meta = self.teacher_get(path + "/export", cookie)
        self.assertEqual(status, 200)
        self.assertIn("attachment", meta["Content-Disposition"])
        self.assertEqual(len(exported["tasks"]), 2)
        self.assertFalse(exported["approves_learner_delivery"])
        self.assertNotIn(cookie, json.dumps(exported))
        status, _, _ = self.request("GET", "/comparisons/" + view["id"] + ".json")
        self.assertEqual(status, 404)

    def test_replay_api_submission_export_and_archived_detail(self):
        source, report, _ = replay_fixture(Path(self.tmp.name), self.bank, self.service.taxonomy)
        self.service.research = ResearchWorkspace(self.service.root, report)
        self.service.replays.source_path = source
        _, _, meta = self.unlock()
        cookie = self.cookie_from(meta)
        headers = {"Cookie": f"demo_teacher={cookie}"}
        _, library, _ = self.teacher_get("/api/teacher/scenarios", cookie)
        body = {"request_id": secrets.token_hex(16), "report_sha256": library["report_sha256"],
                "scenario_ids": [library["scenarios"][0]["id"]], "provider_mode": "rules",
                "allow_provider_calls": False}
        status, run, _ = self.request("POST", "/api/teacher/replays", body, headers)
        self.assertEqual(status, 202)
        path = "/api/teacher/replays/" + run["id"]
        self.assertTrue(wait_for(lambda: self.teacher_get(path, cookie)[1]["status"] == "complete"))
        status, result, meta = self.teacher_get(path + "/export", cookie)
        self.assertEqual(status, 200)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(meta["Cache-Control"], "no-store")
        self.assertIn("attachment", meta["Content-Disposition"])
        self.assertNotIn("student_token", json.dumps(result))
        self.assertNotIn("selected_index", json.dumps(result))
        status, retry, _ = self.request("POST", "/api/teacher/replays", body, headers)
        self.assertEqual(retry["id"], result["id"])
        status, _, _ = self.request("POST", "/api/teacher/replays", dict(body, provider_mode="hosted"), headers)
        self.assertEqual(status, 400)
        sid = result["cases"][0]["session_id"]
        status, replay_case, meta = self.teacher_get(path + "/cases/" + body["scenario_ids"][0] + "/export", cookie)
        self.assertEqual(status, 200)
        self.assertEqual(replay_case["session_id"], sid)
        self.assertEqual(len(replay_case["question_responses"]), 40)
        self.assertIn("attachment", meta["Content-Disposition"])
        status, _, _ = self.request("GET", "/api/sessions/" + sid)
        self.assertEqual(status, 403)
        status, case, _ = self.teacher_get("/api/teacher/scenarios/" + body["scenario_ids"][0] + "/export", cookie)
        self.assertEqual(status, 200)
        self.assertTrue(case["detail"]["read_only"])
        self.assertEqual(len(case["detail"]["question_responses"]), 40)
        status, _, _ = self.request("POST", path + "/cancel", {}, headers)
        self.assertEqual(status, 200)

    def test_research_script_has_same_strict_csp(self):
        status, body, meta = self.request("GET", "/research.js")
        self.assertEqual(status, 200)
        self.assertIn(b"initResearchWorkspace", body)
        self.assertIn("script-src 'self'", meta["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", meta["Content-Security-Policy"])

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


class IntegratedPracticeAPITests(DemoAPITests):
    def setUp(self):
        import copy
        import feedback_practice
        from unittest import mock
        from tests.test_demo_service import BoundRecommender
        from tests.test_feedback_practice import make_graph, make_pool
        self.pool = make_pool(("sA", "sB"))
        for patch in (
                mock.patch.object(
                    feedback_practice, "load_current_practice_pool",
                    return_value=copy.deepcopy(self.pool)),
                mock.patch.object(
                    feedback_practice, "teacher_content_context",
                    return_value=make_graph(self.pool))):
            patch.start()
            self.addCleanup(patch.stop)
        super().setUp()
        self._recommender_cls = BoundRecommender

    def _submit_mixed(self, sid, token, count=40):
        payload = None
        for index in range(count):
            snap = self.service.snapshot(sid, token)
            question = self.bank["questions"][index]
            chosen = question["answer_index"]
            if question["skill_id"] == "sA":
                chosen = (chosen + 1) % len(question["options"])
            status, payload, _ = self.request(
                "POST", f"/api/sessions/{sid}/responses",
                {"question_token": snap["current_question"]
                 ["question_token"],
                 "selected_index": chosen},
                headers={"X-Demo-Session-Token": token})
            self.assertEqual(status, 200)
        return payload

    def _wait_terminal(self, sid):
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["checkpoints"]["end"]
            ["recommendation_job"]["status"] != "pending"))

    def test_unified_release_payload_privacy(self):
        self.service.recommender = self._recommender_cls(
            self.bank, self.pool)
        created = self.create_session()
        sid, token = created["session_id"], created["student_token"]
        self._submit_mixed(sid, token)
        self._wait_terminal(sid)
        status, snap, _ = self.request(
            "GET", f"/api/sessions/{sid}",
            headers={"X-Demo-Session-Token": token})
        self.assertEqual(status, 200)
        blob = json.dumps(snap)
        for needle in ("pool_q", "Pool practice prompt", "p_correct",
                       "answer_index", "practice_context"):
            self.assertNotIn(needle, blob)
        self.assertIsNone(snap["feedback"]["end"])
        self.assertEqual(snap["assessment_total"],
                         {"correct": 30, "out_of": 40})
        _, _, meta = self.unlock()
        cookie = self.cookie_from(meta)
        status, view, _ = self.teacher_get(
            f"/api/teacher/sessions/{sid}", cookie)
        self.assertEqual(status, 200)
        delivery = view["feedback_delivery"]
        self.assertTrue(delivery["integrated"])
        card = delivery["preview"]["practice"]
        self.assertEqual(card["status"], "selected")
        self.assertEqual(card["question"]["text"],
                         "Pool practice prompt 1?")
        status, res, _ = self.request(
            "POST", f"/api/teacher/sessions/{sid}/release",
            {"message_sha256": delivery["preview_sha256"]},
            headers={"Cookie": f"demo_teacher={cookie}"})
        self.assertEqual(status, 200)
        self.assertTrue(res["released"])
        status, snap, _ = self.request(
            "GET", f"/api/sessions/{sid}",
            headers={"X-Demo-Session-Token": token})
        end = snap["feedback"]["end"]
        self.assertEqual(end["practice"], card)
        blob = json.dumps(snap)
        for needle in ("pool_q", "p_correct", "answer_index",
                       "pool_sha256", "item_id", "recommendation"):
            self.assertNotIn(needle, blob)


if __name__ == "__main__":
    unittest.main()
