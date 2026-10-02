"""Loopback-only HTTP boundary for the local synthetic demo.

Separate from the legacy api.py. Student routes require the per-session
owner token (X-Demo-Session-Token). Teacher routes require the opaque
HttpOnly cookie issued by PIN unlock. This is local demo separation,
not production authentication.
"""
import argparse
import hmac
import json
import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from demo_service import (
    DEFAULT_ROOT, ConflictError, DemoService)

LOCAL_HOSTS = {"127.0.0.1", "localhost"}
MAX_BODY_BYTES = 1_048_576
STATIC_DIR = Path(__file__).with_name("web_demo")
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/teacher": "teacher.html",
    "/teacher.html": "teacher.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'")
TEACHER_COOKIE = "demo_teacher"
PIN_RATE_LIMIT = 10
PIN_RATE_WINDOW = 60.0

SESSION_RE = re.compile(
    r"\A/api/sessions/(?P<sid>[a-f0-9]{32})(?:/(?P<action>responses))?\Z")
TEACHER_SESSION_RE = re.compile(
    r"\A/api/teacher/sessions/(?P<sid>[a-f0-9]{32})"
    r"(?:/(?P<action>export|reviews))?\Z")


def _host_ok(host_header, port):
    if not host_header or any(
            ch in host_header for ch in " /?#@\\"):
        return False
    name, sep, port_text = host_header.rpartition(":")
    if not sep:
        name = host_header
    if name.lower() not in LOCAL_HOSTS:
        return False
    if not sep:
        return port == 80
    if not port_text:
        return False
    try:
        return int(port_text) == port
    except ValueError:
        return False


def _origin_ok(origin, port):
    if origin is None:
        return True
    try:
        parsed = urlparse(origin)
        if (parsed.scheme != "http"
                or parsed.hostname is None
                or parsed.hostname.lower() not in LOCAL_HOSTS
                or parsed.username or parsed.password
                or parsed.path or parsed.params
                or parsed.query or parsed.fragment):
            return False
        effective = parsed.port if parsed.port is not None else 80
    except ValueError:
        return False
    return effective == port


class DemoRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    service: DemoService
    teacher_pin: str
    server_port: int
    teacher_tokens: set
    pin_attempts: defaultdict
    auth_lock: threading.Lock

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, format, *args):
        return

    # ---------------- shared plumbing ----------------

    def _dispatch(self, method):
        self._posted_body = None
        self._body_buffered = False
        try:
            if not _host_ok(self.headers.get("Host"), self.server_port):
                self._drain_body()
                return self._error(400, "invalid_host")
            if method == "POST":
                if self.headers.get("Origin") is not None and not \
                        _origin_ok(self.headers.get("Origin"),
                                   self.server_port):
                    self._drain_body()
                    return self._error(403, "cross_origin_rejected")
                ctype = self.headers.get("Content-Type", "")
                if self.path.startswith("/api/") and not \
                        ctype.startswith("application/json"):
                    self._drain_body()
                    return self._error(400, "content_type_required")
                # Buffer the declared body up front: a later auth or
                # validation rejection then never leaves unread bytes
                # on the socket, which Windows would close with RST.
                self._posted_body = self._read_body()
                self._body_buffered = True
            status, payload, extra = self._route(method)
        except FileNotFoundError:
            status, payload, extra = 404, {"error": "not_found"}, {}
        except PermissionError:
            status, payload, extra = 403, {"error": "forbidden"}, {}
        except ConflictError as exc:
            status, payload, extra = 409, {
                "error": "stale_feedback_revision",
                "current_message_sha256": exc.current}, {}
        except ValueError:
            status, payload, extra = 400, {"error": "invalid_request"}, {}
        except Exception:
            status, payload, extra = 500, {"error": "server_error"}, {}
        self._send_json(status, payload, extra)

    def _route(self, method):
        path = urlparse(self.path).path

        if path == "/favicon.ico" and method == "GET":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None, None, None

        if path in STATIC_FILES and method == "GET":
            return self._static(STATIC_FILES[path])

        if method == "POST" and path == "/api/sessions":
            body = self._json_body()
            if set(body) != {"synthetic"} or body["synthetic"] is not True:
                raise ValueError("session creation requires attestation")
            return 201, self.service.create_session(), {}

        if method == "POST" and path == "/api/teacher/unlock":
            return self._unlock()

        if method == "POST" and path == "/api/teacher/logout":
            self._drain_body()
            self._drop_teacher_cookie()
            return 200, {"unlocked": False}, {
                "Set-Cookie": self._cookie("", expires="Thu, 01 Jan 1970 00:00:00 GMT")}

        match = SESSION_RE.fullmatch(path)
        if match:
            sid = match.group("sid")
            token = self.headers.get("X-Demo-Session-Token", "")
            if method == "GET" and match.group("action") is None:
                return 200, self.service.snapshot(sid, token), {}
            if method == "POST" and match.group("action") == "responses":
                body = self._json_body()
                return 200, self.service.submit_response(
                    sid, token, body), {}
            raise FileNotFoundError("route not found")

        if path == "/api/teacher/simulations" and method == "POST":
            self._require_teacher()
            body = self._json_body()
            if set(body) != {"profile", "seed"}:
                raise ValueError("simulation requires profile and seed")
            return 201, self.service.simulate(
                body["profile"], body["seed"]), {}

        if path == "/api/teacher/config" and method == "GET":
            self._require_teacher()
            return 200, self.service.teacher_config(), {}

        if path == "/api/teacher/sessions" and method == "GET":
            self._require_teacher()
            return 200, {"sessions": self.service.list_sessions()}, {}

        match = TEACHER_SESSION_RE.fullmatch(path)
        if match:
            self._require_teacher()
            sid = match.group("sid")
            action = match.group("action")
            if method == "GET" and action is None:
                return 200, self.service.teacher_session(sid), {}
            if method == "GET" and action == "export":
                payload = self.service.teacher_session(sid)
                return 200, payload, {
                    "Content-Disposition":
                        f'attachment; filename="demo_{sid}.json"'}
            if method == "POST" and action == "reviews":
                return 201, self.service.add_review(
                    sid, self._json_body()), {}
            raise FileNotFoundError("route not found")

        raise FileNotFoundError("route not found")

    # ---------------- auth helpers ----------------

    def _cookie(self, token, expires=None):
        parts = [f"{TEACHER_COOKIE}={token}", "HttpOnly",
                 "SameSite=Strict", "Path=/api"]
        if expires:
            parts.append(f"Expires={expires}")
        return "; ".join(parts)

    def _teacher_cookie(self):
        header = self.headers.get("Cookie") or ""
        for part in header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == TEACHER_COOKIE:
                return value
        return None

    def _require_teacher(self):
        token = self._teacher_cookie()
        if not token:
            raise PermissionError("teacher routes require unlock")
        with self.auth_lock:
            valid = any(hmac.compare_digest(token, known)
                        for known in self.teacher_tokens)
        if not valid:
            raise PermissionError("teacher routes require unlock")

    def _drop_teacher_cookie(self):
        token = self._teacher_cookie()
        if token:
            with self.auth_lock:
                self.teacher_tokens.discard(token)

    def _unlock(self):
        body = self._json_body()
        if set(body) != {"pin"} or not isinstance(body["pin"], str):
            raise ValueError("unlock requires a pin")
        now = time.monotonic()
        address = self.client_address[0]
        token = None
        with self.auth_lock:
            attempts = self.pin_attempts[address]
            while attempts and now - attempts[0] > PIN_RATE_WINDOW:
                attempts.popleft()
            if len(attempts) >= PIN_RATE_LIMIT:
                limited = True
            else:
                limited = False
                attempts.append(now)
                if hmac.compare_digest(body["pin"], self.teacher_pin):
                    token = secrets.token_urlsafe(32)
                    self.teacher_tokens.add(token)
                else:
                    token = None
        if limited:
            return self._error(429, "too_many_attempts")
        if token is None:
            raise PermissionError("invalid pin")
        return 200, {"unlocked": True}, {
            "Set-Cookie": self._cookie(token)}

    # ---------------- io helpers ----------------

    def _read_body(self):
        """Buffer up to MAX_BODY_BYTES of the declared request body."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return None
        if length > MAX_BODY_BYTES:
            self._drain_body()
            return None
        if length <= 0:
            return b""
        data = self.rfile.read(length)
        if len(data) != length:
            self.close_connection = True
        return data

    def _drain_body(self):
        """Consume a declared request body so early rejections keep the
        connection readable; closes it when the body cannot be trusted."""
        if getattr(self, "_body_buffered", False):
            return  # already consumed by _read_body
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        remaining = min(length, MAX_BODY_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        if remaining != 0 or length > MAX_BODY_BYTES:
            self.close_connection = True

    def _json_body(self):
        data = self._posted_body
        if not data:
            raise ValueError("request body missing or too large")
        try:
            body = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid json") from None
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    def _static(self, name):
        path = STATIC_DIR / name
        if not path.is_file():
            raise FileNotFoundError(name)
        data = path.read_bytes()
        ctype = CONTENT_TYPES[path.suffix]
        body = None
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        if path.suffix in (".js", ".css"):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionAbortedError, BrokenPipeError,
                ConnectionResetError):
            pass  # client disconnected mid-response
        return None, None, None

    def _error(self, status, code):
        self._send_json(status, {"error": code}, {})
        return None, None, None

    def _send_json(self, status, payload, extra):
        if payload is None:
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status >= 400:
            # Error responses close the connection so an undrained or
            # unexpected request body can never be parsed as the next
            # HTTP request on this socket.
            self.send_header("Connection", "close")
            self.close_connection = True
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError,
                ConnectionResetError):
            pass  # client disconnected mid-response


class DemoHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Browsers drop keep-alive sockets freely; a reset while reading
        # the next request line is not an application error.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def make_server(service, teacher_pin, host="127.0.0.1", port=0):
    if host not in LOCAL_HOSTS:
        raise ValueError("demo API may bind only to localhost")
    if (not isinstance(teacher_pin, str)
            or not teacher_pin.isdigit() or len(teacher_pin) != 6):
        raise ValueError("teacher PIN must be six digits")

    class BoundHandler(DemoRequestHandler):
        pass

    BoundHandler.service = service
    BoundHandler.teacher_pin = teacher_pin
    BoundHandler.teacher_tokens = set()
    BoundHandler.pin_attempts = defaultdict(deque)
    BoundHandler.auth_lock = threading.Lock()
    server = DemoHTTPServer((host, port), BoundHandler)
    BoundHandler.server_port = server.server_address[1]
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1",
                        choices=sorted(LOCAL_HOSTS))
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--providers", choices=("rules", "hosted"),
                        default="rules")
    parser.add_argument("--jev-env-file", type=Path)
    parser.add_argument("--call-budget", type=int, default=12)
    parser.add_argument("--provider-timeout", type=float, default=120.0)
    parser.add_argument("--teacher-pin")
    args = parser.parse_args(argv)
    pin = args.teacher_pin or f"{secrets.randbelow(1_000_000):06d}"
    service = DemoService(
        root=args.root, provider_mode=args.providers,
        jev_env_file=args.jev_env_file, call_budget=args.call_budget,
        provider_timeout=args.provider_timeout)
    server = make_server(service, pin, host=args.host, port=args.port)
    host, port = server.server_address[:2]
    print(f"Synthetic demo listening at http://{host}:{port}", flush=True)
    print(f"Teacher PIN (local demo only): {pin}", flush=True)
    print("Synthetic-only review drafts; not production auth.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        server.server_close()


if __name__ == "__main__":
    main()
