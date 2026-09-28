"""Localhost-only JSON API for the Phase 3 MCQ prototype.

This is a thin prototype boundary around MCQSessionService. Student routes
return public questions and student feedback only; researcher routes require an
explicit local role header and expose private session diagnostics. The header is
route separation, not production authentication.
"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import phase3_paths
from kt_adapter import trace_responses
from mcq_service import MCQSessionService
from session_store import SessionStore

LOCAL_HOSTS = {"127.0.0.1", "localhost"}
MAX_BODY_BYTES = 1_000_000
SESSION_PATH = re.compile(
    r"\A/sessions/(?P<sid>[a-f0-9]{32})/(?P<action>questions|responses|half-submissions|snapshot)\Z")
RESEARCH_PATH = re.compile(
    r"\A/research/sessions/(?P<sid>[a-f0-9]{32})/(?P<action>diagnostics|kt-estimate)\Z")


def student_submission_result(result: dict) -> dict:
    """Remove teacher diagnostics before returning a checkpoint to a student."""
    feed = result.get("feed")
    return {
        "session_id": result["session_id"],
        "accepted": result["accepted"],
        "position": result["position"],
        "checkpoint": result["checkpoint"],
        "feedback": feed["student"] if feed else None,
    }


class Phase3RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    service: MCQSessionService

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, format, *args):
        # Keep local prototype logs focused; request bodies and answers are private.
        return

    def _dispatch(self, method: str):
        try:
            status, payload = self._route(method)
        except FileNotFoundError as exc:
            status, payload = 404, {"error": str(exc)}
        except PermissionError as exc:
            status, payload = 403, {"error": str(exc)}
        except ValueError as exc:
            status, payload = 400, {"error": str(exc)}
        except Exception:
            status, payload = 500, {"error": "internal_server_error"}
        self._send_json(status, payload)

    def _route(self, method: str) -> tuple[int, dict]:
        parsed = urlparse(self.path)
        path = parsed.path

        if method == "POST" and path == "/sessions":
            return 201, self.service.start_session()

        match = SESSION_PATH.fullmatch(path)
        if match:
            session_id = match.group("sid")
            action = match.group("action")
            if method == "GET" and action == "questions":
                half = self._query_half(parsed.query)
                return 200, self.service.questions(session_id, half)
            if method == "GET" and action == "snapshot":
                return 200, self.service.snapshot(session_id)
            if method == "POST" and action == "responses":
                result = self.service.submit_response(
                    session_id, self._json_body())
                return 200, student_submission_result(result)
            if method == "POST" and action == "half-submissions":
                body = self._json_body()
                if set(body) != {"half", "responses"} or not isinstance(body["half"], int):
                    raise ValueError("body must contain half and responses")
                result = self.service.submit_half(
                    session_id, body["half"], body["responses"])
                return 200, student_submission_result(result)
            raise ValueError("unsupported session route or method")

        match = RESEARCH_PATH.fullmatch(path)
        if match:
            self._require_researcher()
            session_id = match.group("sid")
            action = match.group("action")
            if method == "GET" and action == "diagnostics":
                session = self.service.private_record(session_id)
                return 200, {
                    "session_id": session_id,
                    "bank_sha256": session["bank_sha256"],
                    "created_at": session["created_at"],
                    "status": session["status"],
                    "question_order": session["question_order"],
                    "responses": session["responses"],
                    "checkpoints": session["checkpoints"],
                }
            if method == "POST" and action == "kt-estimate":
                body = self._json_body(optional=True)
                if set(body) - {"device"}:
                    raise ValueError("body may contain only device")
                device = body.get("device", "cpu")
                if device not in ("cpu", "cuda"):
                    raise ValueError("device must be cpu or cuda")
                rows = self.service.private_response_rows(session_id)
                return 200, {
                    "session_id": session_id,
                    "model_estimate": trace_responses(
                        self.service.bank, rows, device=device),
                }
            raise ValueError("unsupported research route or method")

        raise FileNotFoundError("route not found")

    def _query_half(self, query: str) -> int:
        values = parse_qs(query, keep_blank_values=True)
        if set(values) != {"half"} or len(values["half"]) != 1:
            raise ValueError("questions requires one half query parameter")
        half = values["half"][0]
        if half not in ("1", "2"):
            raise ValueError("half must be 1 or 2")
        return int(half)

    def _json_body(self, optional: bool = False) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Content-Length must be an integer") from None
        if length < 0 or length == 0:
            if optional:
                return {}
            raise ValueError("request body must be a JSON object")
        if length > MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        try:
            body = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("request body must be valid JSON") from None
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    def _require_researcher(self):
        if self.headers.get("X-Phase3-Role") != "researcher":
            raise PermissionError("researcher routes require X-Phase3-Role: researcher")

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def make_server(service: MCQSessionService, host: str = "127.0.0.1",
                port: int = 0) -> ThreadingHTTPServer:
    if host not in LOCAL_HOSTS:
        raise ValueError("Phase 3 prototype API may bind only to localhost")

    class BoundHandler(Phase3RequestHandler):
        pass

    BoundHandler.service = service
    return ThreadingHTTPServer((host, port), BoundHandler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", choices=sorted(LOCAL_HOSTS))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    service = MCQSessionService.from_default(
        SessionStore(phase3_paths.SESSIONS))
    server = make_server(service, args.host, args.port)
    host, port = server.server_address[:2]
    print(f"Phase 3 local API listening at http://{host}:{port}")
    print("Student routes are public payloads only; researcher routes require "
          "X-Phase3-Role: researcher (local route separation, not auth).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
