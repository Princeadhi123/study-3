"""Tests for the Aitta connectivity CLI probes (fully offline).

All sockets, DNS, proxy inspection, and openers are mocked; no network,
no real credentials, no file writes. These verify safe fixed-shape
records, bounds, sanitization, and CLI exit codes — not transport
quality.
"""
import io
import json
import socket
import ssl
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import aitta_generator
import transport_diagnostics as td
from aitta_generator import AittaGenerator
from tests.test_aitta_generator import BASE_URL, FAKE_KEY
from tests.test_jev_selector import FakeOpener, FakeResponse

MODELS_URL = BASE_URL + "/models"
HOST = "aitta.example"
PORT = 443


def make_generator(opener=None):
    return AittaGenerator(FAKE_KEY, BASE_URL,
                          opener=opener if opener is not None
                          else FakeOpener(b"{}"))


def ok_dns(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
             ("10.0.0.1", 443))]


class StatusResponse(FakeResponse):
    status = 200


def run_check(generator, timeout=10.0, direct=False, dns=ok_dns,
              proxies=None, bypass=False):
    with mock.patch.object(td.socket, "getaddrinfo", side_effect=dns), \
         mock.patch.object(td.urllib.request, "getproxies",
                           return_value=proxies or {}), \
         mock.patch.object(td.urllib.request, "proxy_bypass",
                           return_value=bypass):
        return td.run_connectivity_check(
            generator, timeout=timeout, direct=direct)


def check(report, stage):
    found = [c for c in report["checks"] if c["stage"] == stage]
    assert len(found) == 1
    return found[0]


class ReportShapeTests(unittest.TestCase):
    def test_success_report_shape_and_request(self):
        class StatusOpener(FakeOpener):
            def open(self, request, timeout=None):
                self.requests.append(request)
                self.timeouts.append(timeout)
                response = StatusResponse(self.body)
                response.status = 200
                self.responses.append(response)
                return response

        opener = StatusOpener()
        report = run_check(make_generator(opener), timeout=12.5)
        self.assertEqual(report["schema"], td.CONNECTIVITY_SCHEMA)
        self.assertEqual(report["timeout_seconds"], 12.5)
        self.assertEqual(
            report["proxy"],
            {"https_proxy_configured": False,
             "target_bypasses_proxy": False,
             "proxy_selected": False})
        self.assertEqual(
            [c["stage"] for c in report["checks"]],
            ["target_dns", "http_models_get"])
        dns = check(report, "target_dns")
        self.assertEqual(dns["status"], "ok")
        self.assertIsNone(dns["failure_kind"])
        self.assertIsNone(dns["http_status"])
        http = check(report, "http_models_get")
        self.assertEqual(http["status"], "ok")
        self.assertEqual(http["http_status"], 200)
        request = opener.requests[0]
        self.assertEqual(request.full_url, MODELS_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"),
                         f"Bearer {FAKE_KEY}")
        self.assertEqual(request.get_header("Accept"),
                         "application/json")
        self.assertEqual(opener.timeouts, [12.5])
        response = opener.responses[0]
        self.assertFalse(hasattr(response, "read_limit"))
        self.assertTrue(response.closed)

    def test_record_keys_fixed(self):
        report = run_check(make_generator())
        for record in report["checks"]:
            self.assertEqual(
                set(record),
                {"stage", "status", "elapsed_seconds", "failure_kind",
                 "http_status", "next_steps"})
            self.assertIn(record["status"], ("ok", "failed", "skipped"))
            self.assertIsInstance(record["elapsed_seconds"], float)
            for step in record["next_steps"]:
                self.assertIsInstance(step, str)

    def test_no_direct_stages_by_default(self):
        report = run_check(make_generator())
        stages = [c["stage"] for c in report["checks"]]
        self.assertNotIn("direct_tcp", stages)
        self.assertNotIn("direct_tls", stages)


class ProxyContextTests(unittest.TestCase):
    def test_credential_proxy_url_redacted(self):
        proxies = {"https": "https://user:secretpw@proxy.local:8443"}
        report = run_check(make_generator(), proxies=proxies)
        self.assertEqual(
            report["proxy"],
            {"https_proxy_configured": True,
             "target_bypasses_proxy": False,
             "proxy_selected": True})
        blob = json.dumps(report)
        for leaked in ("secretpw", "proxy.local", "8443"):
            self.assertNotIn(leaked, blob)

    def test_bypass_means_not_selected(self):
        proxies = {"https": "https://proxy.local:8443"}
        report = run_check(make_generator(), proxies=proxies,
                           bypass=True)
        self.assertFalse(report["proxy"]["proxy_selected"])
        self.assertTrue(report["proxy"]["target_bypasses_proxy"])

    def test_proxy_inspection_error_fails_safely(self):
        with mock.patch.object(td.socket, "getaddrinfo",
                               side_effect=ok_dns), \
             mock.patch.object(td.urllib.request, "getproxies",
                               side_effect=RuntimeError("boom")):
            report = td.run_connectivity_check(make_generator())
        self.assertEqual(
            report["proxy"],
            {"https_proxy_configured": None,
             "target_bypasses_proxy": None,
             "proxy_selected": None})
        self.assertIn("http_models_get",
                      [c["stage"] for c in report["checks"]])

    def test_proxy_inspection_blocked_bounded(self):
        block = threading.Event()

        def hanging():
            block.wait(30)

        start = time.perf_counter()
        with mock.patch.object(td.socket, "getaddrinfo",
                               side_effect=ok_dns), \
             mock.patch.object(td.urllib.request, "getproxies",
                               side_effect=hanging), \
             mock.patch.object(td.urllib.request, "proxy_bypass",
                               return_value=False):
            report = td.run_connectivity_check(
                make_generator(), timeout=0.2)
        elapsed = time.perf_counter() - start
        block.set()
        self.assertEqual(
            report["proxy"],
            {"https_proxy_configured": None,
             "target_bypasses_proxy": None,
             "proxy_selected": None})
        self.assertIn("http_models_get",
                      [c["stage"] for c in report["checks"]])
        self.assertLess(elapsed, 15.0)

    def test_proxy_bypass_uses_netloc_with_port(self):
        generator = AittaGenerator(
            FAKE_KEY, "https://aitta.example:8443/v1",
            opener=FakeOpener(b"{}"))
        with mock.patch.object(td.socket, "getaddrinfo",
                               side_effect=ok_dns), \
             mock.patch.object(td.urllib.request, "getproxies",
                               return_value={}), \
             mock.patch.object(td.urllib.request, "proxy_bypass",
                               return_value=False) as bypass:
            td.run_connectivity_check(generator)
        bypass.assert_called_once_with("aitta.example:8443")


class DnsAndClassificationTests(unittest.TestCase):
    def test_dns_gaierror_classified(self):
        report = run_check(
            make_generator(),
            dns=socket.gaierror("secret host detail"))
        dns = check(report, "target_dns")
        self.assertEqual(dns["status"], "failed")
        self.assertEqual(dns["failure_kind"], "dns_error")
        self.assertNotIn("secret", json.dumps(report))

    def test_dns_failure_does_not_suppress_http(self):
        opener = FakeOpener(b"{}")
        report = run_check(make_generator(opener),
                           dns=socket.gaierror("nx"))
        self.assertEqual(len(opener.requests), 1)
        self.assertIn("http_models_get",
                      [c["stage"] for c in report["checks"]])

    def test_dns_timeout_wall_clock_bounded(self):
        block = threading.Event()

        def hanging(*args, **kwargs):
            block.wait(30)

        start = time.perf_counter()
        report = run_check(make_generator(), timeout=0.2,
                           dns=hanging)
        elapsed = time.perf_counter() - start
        block.set()
        dns = check(report, "target_dns")
        self.assertEqual(dns["status"], "failed")
        self.assertEqual(dns["failure_kind"], "timeout")
        self.assertLess(elapsed, 15.0)

    def test_urlerror_reason_classification(self):
        cases = [
            (urllib.error.URLError(TimeoutError("x")), "timeout"),
            (urllib.error.URLError(socket.gaierror("x")), "dns_error"),
            (urllib.error.URLError(ssl.SSLCertVerificationError("x")),
             "tls_certificate_error"),
            (urllib.error.URLError(ssl.SSLError("x")), "tls_error"),
            (urllib.error.URLError(ConnectionRefusedError("x")),
             "connection_error"),
            (urllib.error.URLError(OSError("x")), "connection_error"),
            (urllib.error.URLError(ValueError("x")), "network_error"),
        ]
        for exc, expected in cases:
            with self.subTest(expected=expected):
                opener = FakeOpener(error=exc)
                report = run_check(make_generator(opener))
                http = check(report, "http_models_get")
                self.assertEqual(http["status"], "failed")
                self.assertEqual(http["failure_kind"], expected)
                self.assertIsNone(http["http_status"])

    def test_plain_exceptions_classified(self):
        for exc, expected in [
                (TimeoutError("x"), "timeout"),
                (socket.timeout("x"), "timeout"),
                (ssl.SSLError("x"), "tls_error"),
                (ConnectionResetError("x"), "connection_error"),
                (RuntimeError("x"), "transport_error")]:
            with self.subTest(expected=expected):
                opener = FakeOpener(error=exc)
                report = run_check(make_generator(opener))
                self.assertEqual(
                    check(report, "http_models_get")["failure_kind"],
                    expected)


class HttpStatusTests(unittest.TestCase):
    def test_error_statuses_failed_but_http_obtained(self):
        for code in (301, 401, 403, 404, 429, 503):
            with self.subTest(code=code):
                error = urllib.error.HTTPError(
                    MODELS_URL, code, "secret reason", {},
                    io.BytesIO(b"x"))
                opener = FakeOpener(error=error)
                report = run_check(make_generator(opener))
                http = check(report, "http_models_get")
                self.assertEqual(http["status"], "failed")
                self.assertEqual(http["failure_kind"], "http_error")
                self.assertEqual(http["http_status"], code)
                self.assertNotIn("secret", json.dumps(report))

    def test_httperror_closed_and_not_read(self):
        class TrackedError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__(MODELS_URL, 503, "x", {},
                                 io.BytesIO(b"body"))
                self.closed = False
                self.read_called = False

            def close(self):
                self.closed = True

            def read(self, *args, **kwargs):
                self.read_called = True
                return b""

        error = TrackedError()
        run_check(make_generator(FakeOpener(error=error)))
        self.assertTrue(error.closed)
        self.assertFalse(error.read_called)

    def test_non_2xx_success_response_failed(self):
        class TeapotOpener(FakeOpener):
            def open(self, request, timeout=None):
                response = StatusResponse(self.body)
                response.status = 418
                return response

        report = run_check(make_generator(TeapotOpener()))
        http = check(report, "http_models_get")
        self.assertEqual(http["status"], "failed")
        self.assertEqual(http["failure_kind"], "http_error")
        self.assertEqual(http["http_status"], 418)

    def test_unmatched_http_status_gets_response_steps(self):
        for code in (301, 418):
            with self.subTest(code=code):
                error = urllib.error.HTTPError(
                    MODELS_URL, code, "x", {}, io.BytesIO(b"x"))
                report = run_check(
                    make_generator(FakeOpener(error=error)))
                http = check(report, "http_models_get")
                self.assertEqual(http["http_status"], code)
                text = " ".join(http["next_steps"])
                self.assertIn("Redirects are not followed", text)
                self.assertNotIn("No HTTP response", text)

    def test_late_response_closed_and_not_read(self):
        release = threading.Event()
        cleaned = threading.Event()

        class LateResponse:
            status = 200

            def __init__(self):
                self.read_called = False

            def read(self, *args, **kwargs):
                self.read_called = True
                return b""

            def close(self):
                self.closed = True
                cleaned.set()

        class SlowOpener(FakeOpener):
            def open(self, request, timeout=None):
                release.wait(30)
                self.response = LateResponse()
                return self.response

        opener = SlowOpener()
        report = run_check(make_generator(opener), timeout=0.2)
        http = check(report, "http_models_get")
        self.assertEqual(http["status"], "failed")
        self.assertEqual(http["failure_kind"], "timeout")
        release.set()
        self.assertTrue(cleaned.wait(10))
        self.assertFalse(opener.response.read_called)

    def test_response_close_runtimeerror_still_safe(self):
        class BadCloseResponse(StatusResponse):
            status = 200

            def close(self):
                raise RuntimeError("close detail secret")

        class BadCloseOpener(FakeOpener):
            def open(self, request, timeout=None):
                return BadCloseResponse(self.body)

        report = run_check(make_generator(BadCloseOpener()))
        http = check(report, "http_models_get")
        self.assertEqual(http["status"], "ok")
        self.assertEqual(http["http_status"], 200)
        self.assertNotIn("secret", json.dumps(report))

    def test_httperror_close_oserror_still_safe(self):
        class BadCloseError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__(MODELS_URL, 503, "x", {},
                                 io.BytesIO(b"x"))

            def close(self):
                raise OSError("close detail secret")

        report = run_check(
            make_generator(FakeOpener(error=BadCloseError())))
        http = check(report, "http_models_get")
        self.assertEqual(http["status"], "failed")
        self.assertEqual(http["failure_kind"], "http_error")
        self.assertEqual(http["http_status"], 503)
        self.assertNotIn("secret", json.dumps(report))


class BoundedWorkerTests(unittest.TestCase):
    def test_concrete_errors_returned_and_classified(self):
        for exc, expected in [
                (OSError("x"), "connection_error"),
                (RuntimeError("x"), "transport_error"),
                (ValueError("x"), "transport_error")]:
            with self.subTest(expected=expected):
                ok, value = td._run_bounded(
                    mock.Mock(side_effect=exc), 1.0)
                self.assertFalse(ok)
                kind, _ = td._classify_probe(value)
                self.assertEqual(kind, expected)

    def test_urlerror_via_oserror_catch_classified(self):
        exc = urllib.error.URLError(TimeoutError("x"))
        ok, value = td._run_bounded(mock.Mock(side_effect=exc), 1.0)
        self.assertFalse(ok)
        self.assertIs(value, exc)
        kind, _ = td._classify_probe(value)
        self.assertEqual(kind, "timeout")

    def test_interruption_propagates_from_queue_wait(self):
        with mock.patch.object(td.threading, "Thread") as thread_cls, \
             mock.patch.object(td.queue, "Queue") as queue_cls:
            queue_cls.return_value.get.side_effect = KeyboardInterrupt()
            worker = mock.Mock()
            with self.assertRaises(KeyboardInterrupt):
                td._run_bounded(worker, 1.0)
            thread_cls.return_value.start.assert_called_once()
            queue_cls.return_value.get.assert_called_once_with(
                timeout=1.0)


class DirectProbeTests(unittest.TestCase):
    def test_direct_tcp_tls_ok(self):
        fake_sock = mock.Mock(name="sock")
        fake_tls = mock.Mock(name="tls")
        context = mock.Mock(name="ctx")
        context.wrap_socket.return_value = fake_tls
        with mock.patch.object(td.socket, "create_connection",
                               return_value=fake_sock) as connect, \
             mock.patch.object(td.ssl, "create_default_context",
                               return_value=context):
            report = run_check(make_generator(), direct=True)
        connect.assert_called_once()
        context.wrap_socket.assert_called_once_with(
            fake_sock, server_hostname=HOST)
        tcp = check(report, "direct_tcp")
        tls = check(report, "direct_tls")
        self.assertEqual(tcp["status"], "ok")
        self.assertEqual(tls["status"], "ok")
        fake_tls.close.assert_called_once()

    def test_direct_tcp_failure_skips_tls(self):
        with mock.patch.object(
                td.socket, "create_connection",
                side_effect=ConnectionRefusedError("x")), \
             mock.patch.object(td.ssl, "create_default_context") as ctx:
            report = run_check(make_generator(), direct=True)
        ctx.return_value.wrap_socket.assert_not_called()
        tcp = check(report, "direct_tcp")
        tls = check(report, "direct_tls")
        self.assertEqual(tcp["status"], "failed")
        self.assertEqual(tcp["failure_kind"], "connection_error")
        self.assertEqual(tls["status"], "skipped")

    def test_direct_tls_failure_closes_socket(self):
        fake_sock = mock.Mock(name="sock")
        context = mock.Mock(name="ctx")
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError(
            "bad cert with secret detail")
        with mock.patch.object(td.socket, "create_connection",
                               return_value=fake_sock), \
             mock.patch.object(td.ssl, "create_default_context",
                               return_value=context):
            report = run_check(make_generator(), direct=True)
        tls = check(report, "direct_tls")
        self.assertEqual(tls["status"], "failed")
        self.assertEqual(tls["failure_kind"], "tls_certificate_error")
        fake_sock.close.assert_called_once()
        self.assertNotIn("secret", json.dumps(report))

    def test_direct_tcp_timeout_bounded_and_late_cleanup(self):
        block = threading.Event()
        closed = threading.Event()
        sock = mock.Mock(name="sock")
        sock.close.side_effect = lambda: closed.set()

        def slow_connect(*args, **kwargs):
            block.wait(30)
            return sock

        with mock.patch.object(td.socket, "create_connection",
                               side_effect=slow_connect):
            start = time.perf_counter()
            report = run_check(make_generator(), timeout=0.2,
                               direct=True)
            elapsed = time.perf_counter() - start
        tcp = check(report, "direct_tcp")
        tls = check(report, "direct_tls")
        self.assertEqual(tcp["status"], "failed")
        self.assertEqual(tcp["failure_kind"], "timeout")
        self.assertLess(tcp["elapsed_seconds"], 15.0)
        self.assertEqual(tls["status"], "skipped")
        self.assertLess(elapsed, 15.0)
        block.set()
        self.assertTrue(closed.wait(10))

    def test_direct_tls_timeout_bounded_and_late_cleanup(self):
        block = threading.Event()
        closed = threading.Event()
        fake_sock = mock.Mock(name="sock")
        tls_sock = mock.Mock(name="tls")
        tls_sock.close.side_effect = lambda: closed.set()
        context = mock.Mock(name="ctx")

        def slow_wrap(*args, **kwargs):
            block.wait(30)
            return tls_sock

        context.wrap_socket.side_effect = slow_wrap
        with mock.patch.object(td.socket, "create_connection",
                               return_value=fake_sock), \
             mock.patch.object(td.ssl, "create_default_context",
                               return_value=context):
            report = run_check(make_generator(), timeout=0.2,
                               direct=True)
        tcp = check(report, "direct_tcp")
        tls = check(report, "direct_tls")
        self.assertEqual(tcp["status"], "ok")
        self.assertLess(tcp["elapsed_seconds"], 0.2)
        self.assertEqual(tls["status"], "failed")
        self.assertEqual(tls["failure_kind"], "timeout")
        block.set()
        self.assertTrue(closed.wait(10))


class CliTests(unittest.TestCase):
    def report_with_status(self, http_status, status="ok"):
        return {"schema": td.CONNECTIVITY_SCHEMA,
                "timeout_seconds": 10.0,
                "proxy": {"https_proxy_configured": False,
                          "target_bypasses_proxy": False,
                          "proxy_selected": False},
                "checks": [
                    {"stage": "target_dns", "status": "ok",
                     "elapsed_seconds": 0.0, "failure_kind": None,
                     "http_status": None, "next_steps": []},
                    {"stage": "http_models_get", "status": status,
                     "elapsed_seconds": 0.0,
                     "failure_kind": (None if status == "ok"
                                      else "http_error"),
                     "http_status": http_status, "next_steps": []}]}

    def invoke(self, argv, env_result=None, env_error=None,
               report=None):
        if env_error is not None:
            env_patch = mock.patch.object(
                aitta_generator.AittaGenerator, "from_env",
                side_effect=env_error)
        else:
            env_patch = mock.patch.object(
                aitta_generator.AittaGenerator, "from_env",
                return_value=(env_result
                              if env_result is not None
                              else make_generator()))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with env_patch, \
             mock.patch.object(td, "run_connectivity_check",
                               return_value=report):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = td.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_exit_0_on_2xx(self):
        code, out, err = self.invoke(
            ["--timeout", "5"],
            report=self.report_with_status(200))
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertEqual(parsed["schema"], td.CONNECTIVITY_SCHEMA)

    def test_exit_1_on_non_2xx(self):
        for http_status in (401, 404, 429, 503, None):
            with self.subTest(http_status=http_status):
                code, _, _ = self.invoke(
                    [], report=self.report_with_status(
                        http_status, "failed"))
                self.assertEqual(code, 1)

    def test_exit_2_invalid_config(self):
        code, out, _ = self.invoke(
            [], env_error=ValueError("secret env path detail"))
        self.assertEqual(code, 2)
        parsed = json.loads(out)
        self.assertEqual(parsed["error"],
                         "invalid configuration or arguments")
        self.assertNotIn("secret", out)
        self.assertNotIn("Traceback", out)

    def test_exit_2_invalid_timeouts(self):
        for value in ("0", "-3", "nan", "inf"):
            with self.subTest(value=value):
                code, out, _ = self.invoke(
                    ["--timeout", value],
                    report=self.report_with_status(200))
                self.assertEqual(code, 2)
                self.assertNotIn("Traceback", out)

    def test_exit_2_nonfloat_timeout_no_leak(self):
        code, out, err = self.invoke(
            ["--timeout", "bad-secret"])
        self.assertEqual(code, 2)
        parsed = json.loads(out)
        self.assertEqual(parsed["error"],
                         "invalid configuration or arguments")
        self.assertNotIn("bad-secret", out)
        self.assertEqual(err, "")

    def test_exit_2_unknown_flag_no_leak_no_stderr(self):
        code, out, err = self.invoke(["--secret-sentinel"])
        self.assertEqual(code, 2)
        parsed = json.loads(out)
        self.assertEqual(parsed["error"],
                         "invalid configuration or arguments")
        self.assertNotIn("secret-sentinel", out)
        self.assertEqual(err, "")

    def test_bool_timeout_rejected(self):
        generator = make_generator()
        with self.assertRaises(ValueError):
            td.run_connectivity_check(generator, timeout=True)

    def test_secret_sentinel_not_leaked(self):
        sentinel = "S3CR3T-sentinel-value"
        code, out, _ = self.invoke(
            [], env_error=ValueError(f"boom {sentinel}"))
        self.assertEqual(code, 2)
        self.assertNotIn(sentinel, out)

    def test_main_propagates_keyboard_interrupt(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
                aitta_generator.AittaGenerator, "from_env",
                side_effect=KeyboardInterrupt()):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaises(KeyboardInterrupt):
                    td.main([])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
