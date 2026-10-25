"""Safe transport telemetry for the observed-evidence Aitta seam.

``InstrumentedEvidenceAittaGenerator`` subclasses
``EvidenceAittaGenerator`` and inserts a transparent observing opener
between the existing ``AittaGenerator`` and its transport. The wrapper
exists so a private hosted smoke run can tell *where* a failure
happened (connect, response read, close, or post-body contract checks)
without recording anything sensitive.

Boundaries:

- The observer records only a fixed telemetry triple: ``stage``,
  ``failure_kind``, and a sanitized numeric ``http_status``. It never
  inspects, reprs, or stores exception messages, ``URLError.reason``
  strings, URLs, headers, request or response bodies, or credentials.
- The observing opener delegates ``open`` with the same request and
  timeout unchanged; the observing response delegates ``read`` with the
  same limit and ``close`` unchanged. Exceptions always propagate to
  the existing adapter, so the no-redirects policy, 1 MiB response cap,
  and ``AittaGenerationError('Aitta request failed')`` sanitization are
  all preserved exactly. The observer itself still performs no retries;
  any bounded automatic retry lives in the hosted ``CaptureCache``,
  keyed on the sanitized triple, not here.
- The observer resets before every ``generate`` call, including
  payloads rejected by local validation, so telemetry can never report
  a stale stage from an earlier request.
- ``last_metadata`` is the wrapped generator metadata plus the
  telemetry triple; when the call failed after the body was fully
  received, ``failure_stage`` reports
  ``'response_contract_or_opening_validation'`` because the transport
  succeeded and the failure came from the adapter's response/contract
  validation. Otherwise ``failure_stage`` reports the observed stage.
"""
import argparse
import copy
import datetime
import email.utils
import json
import math
import queue
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from evidence_providers import EvidenceAittaGenerator
from jev_selector import _validate_timeout

STAGES = frozenset((
    "not_called", "open_started", "response_received",
    "body_read_started", "body_received", "open_failed",
    "body_read_failed", "close_failed"))
FAILURE_KINDS = frozenset(
    ("timeout", "http_error", "network_error", "transport_error",
     "tls_certificate_error", "tls_error"))
BODY_RECEIVED_FAILURE = "response_contract_or_opening_validation"


def _classify(exc):
    """Map an exception type to a safe failure kind; never reads text."""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        return "http_error", (code if type(code) is int
                              and 100 <= code <= 599 else None)
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout", None
        if isinstance(reason, ssl.SSLCertVerificationError):
            return "tls_certificate_error", None
        if isinstance(reason, ssl.SSLError):
            return "tls_error", None
        return "network_error", None
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout", None
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls_certificate_error", None
    if isinstance(exc, ssl.SSLError):
        return "tls_error", None
    return "transport_error", None


def _retry_after_seconds(exc):
    """Validated Retry-After delay from an HTTPError, or None.

    Parses only the structured header value (delta seconds or an HTTP
    date); exception text and other headers are never read.
    """
    if not isinstance(exc, urllib.error.HTTPError):
        return None
    try:
        value = exc.headers.get("Retry-After") if exc.headers else None
    except Exception:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        delay = float(value)
        if math.isfinite(delay) and delay >= 0:
            return delay
    except ValueError:
        pass
    try:
        moment = email.utils.parsedate_to_datetime(value)
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0.0, (moment - now).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class _TransportObserver:
    """Fixed-shape telemetry state; one instance per wrapped call."""

    def __init__(self):
        self._local = threading.local()
        self._retry_after_local = threading.local()
        self._fallback_state = {"stage": "not_called", "failure_kind": None,
                                "http_status": None}

    @property
    def state(self):
        return self._local.__dict__.get("state", self._fallback_state)

    @state.setter
    def state(self, value):
        self._local.state = value
        self._fallback_state = value

    @property
    def retry_after(self):
        """Validated Retry-After delay for this thread, or None."""
        return self._retry_after_local.__dict__.get("value")

    def reset(self):
        self.state = {"stage": "not_called", "failure_kind": None,
                      "http_status": None}
        self._retry_after_local.value = None

    def reached(self, stage):
        current = dict(self.state)
        current["stage"] = stage
        self.state = current

    def failed(self, stage, exc):
        kind, status = _classify(exc)
        self.state = {"stage": stage, "failure_kind": kind,
                      "http_status": status}
        self._retry_after_local.value = _retry_after_seconds(exc)


class _ObservedResponse:
    """Delegates read/close to the real response, recording stages."""

    def __init__(self, response, observer):
        self._response = response
        self._observer = observer

    def __getattr__(self, name):
        if name in ("_response", "_observer"):
            raise AttributeError(name)
        return getattr(self._response, name)

    def read(self, limit=-1):
        self._observer.reached("body_read_started")
        try:
            data = self._response.read(limit)
        except Exception as exc:
            self._observer.failed("body_read_failed", exc)
            raise
        self._observer.reached("body_received")
        return data

    def close(self):
        try:
            close = getattr(self._response, "close", None)
            if callable(close):
                close()
        except Exception as exc:
            self._observer.failed("close_failed", exc)
            raise


class _ObservingOpener:
    """Passes the request to the real opener unchanged."""

    def __init__(self, opener, observer):
        self._opener = opener
        self._observer = observer

    def open(self, request, timeout=None):
        self._observer.reached("open_started")
        try:
            response = self._opener.open(request, timeout=timeout)
        except Exception as exc:
            self._observer.failed("open_failed", exc)
            raise
        self._observer.reached("response_received")
        return _ObservedResponse(response, self._observer)


class InstrumentedEvidenceAittaGenerator(EvidenceAittaGenerator):
    """EvidenceAittaGenerator with fixed-shape transport telemetry.

    The inner ``AittaGenerator``'s opener is wrapped once at
    construction; requests, timeouts, read limits, and response bytes
    are forwarded unchanged, and every failure still surfaces as the
    adapter's sanitized ``AittaGenerationError``.
    """

    def __init__(self, generator):
        super().__init__(generator)
        self._observer = _TransportObserver()
        self._generator._opener = _ObservingOpener(
            self._generator._opener, self._observer)

    def __repr__(self):
        return (f"InstrumentedEvidenceAittaGenerator"
                f"({self._generator!r})")

    @property
    def last_metadata(self) -> dict:
        metadata = super().last_metadata
        transport = copy.deepcopy(self._observer.state)
        metadata["transport"] = transport
        if metadata.get("status") == "failed":
            metadata["failure_stage"] = (
                BODY_RECEIVED_FAILURE
                if transport["stage"] == "body_received"
                else transport["stage"])
        return metadata

    def generate(self, payload: dict) -> dict:
        self._observer.reset()
        return super().generate(payload)


CONNECTIVITY_SCHEMA = "phase3_aitta_connectivity_v1"
CONNECTIVITY_FAILURE_KINDS = frozenset(
    ("dns_error", "timeout", "tls_certificate_error", "tls_error",
     "connection_error", "http_error", "network_error",
     "transport_error"))
_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"
_CONFIG_ERROR = "invalid configuration or arguments"
_DIRECT_NOTE = (
    "Direct probes connect without the configured HTTP proxy for "
    "comparison only; they do not change the application route. Use "
    "only if direct access is allowed.")

_DNS_FAILED_STEPS = (
    "Target DNS resolution failed locally; this may indicate a resolver "
    "problem, but a proxied route can resolve differently, so this is "
    "not conclusive.")
_TLS_CERT_STEPS = (
    "TLS certificate verification failed; check the system trust store "
    "and any VPN or proxy certificate configuration. Do not disable "
    "certificate verification.")
_AUTH_STEPS = (
    "The endpoint returned an authentication or permission status; "
    "verify credentials and permissions without exposing key values.")
_NOT_FOUND_STEPS = (
    "The endpoint did not expose a models route (404), but HTTP "
    "connectivity to the service is established.")
_RATE_LIMIT_STEPS = (
    "The endpoint returned a rate-limit status (429); HTTP connectivity "
    "is established.")
_SERVER_ERROR_STEPS = (
    "The endpoint returned a 5xx status; this is a gateway/provider "
    "response and does not conclusively identify the failing component.")
_TIMEOUT_STEPS = (
    "The check timed out on the configured route without an HTTP "
    "response; this cannot distinguish proxy, TLS, or provider causes.")
_HTTP_OK_STEPS = (
    "A successful models GET confirms HTTP reachability only; it does "
    "not prove chat generation or model health.")
_NO_RESPONSE_STEPS = (
    "No HTTP response was obtained on the configured route; this "
    "cannot distinguish proxy, TLS, DNS, or provider causes.")
_HTTP_RESPONSE_STEPS = (
    "An HTTP response was obtained; inspect endpoint routing and "
    "status. Redirects are not followed.")


class _StageTimeout(Exception):
    """Wall-clock bound marker; never carries detail."""


def _run_bounded(worker, timeout):
    """Run ``worker`` on a daemon thread with a wall-clock bound.

    Returns ``(True, value)`` on success, ``(False, exc)`` when the
    worker raised, or ``(False, _StageTimeout())`` when the bound
    elapsed first. The worker owns and cleans up its own resources even
    if it finishes after the caller has already timed out. Expected
    diagnostic failures are returned without thread logging;
    interruptions are not intercepted.
    """
    done = queue.Queue(maxsize=1)

    def run():
        try:
            done.put((True, worker()))
        except (OSError, RuntimeError, ValueError, TypeError,
                AttributeError) as exc:
            done.put((False, exc))

    threading.Thread(target=run, daemon=True).start()
    try:
        return done.get(timeout=timeout)
    except queue.Empty:
        return False, _StageTimeout()


def _classify_reason(reason):
    """Classify a URLError reason by type only; never reads text."""
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, socket.gaierror):
        return "dns_error"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "tls_certificate_error"
    if isinstance(reason, ssl.SSLError):
        return "tls_error"
    if isinstance(reason, (ConnectionError, OSError)):
        return "connection_error"
    return "network_error"


def _classify_probe(exc):
    """Map an exception type to a safe failure kind and http_status."""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        return ("http_error", code if type(code) is int
                and 100 <= code <= 599 else None)
    if isinstance(exc, urllib.error.URLError):
        return _classify_reason(getattr(exc, "reason", None)), None
    if isinstance(exc, (_StageTimeout, TimeoutError, socket.timeout)):
        return "timeout", None
    if isinstance(exc, socket.gaierror):
        return "dns_error", None
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls_certificate_error", None
    if isinstance(exc, ssl.SSLError):
        return "tls_error", None
    if isinstance(exc, (ConnectionError, OSError)):
        return "connection_error", None
    return "transport_error", None


def _proxy_context(host_authority, timeout):
    """Booleans only; never records proxy addresses or env values.

    Bounded like a probe stage: inspection timeouts or errors yield
    null booleans rather than a claimed route. ``host_authority`` is
    the URL netloc (explicit port included) so bypass matching follows
    the same authority form urllib's ProxyHandler uses.
    """
    null = {"https_proxy_configured": None,
            "target_bypasses_proxy": None,
            "proxy_selected": None}

    def work():
        configured = bool(urllib.request.getproxies().get("https"))
        bypass = bool(urllib.request.proxy_bypass(host_authority))
        return configured, bypass

    ok, value = _run_bounded(work, timeout)
    if not ok or not isinstance(value, tuple):
        return dict(null)
    configured, bypass = value
    return {"https_proxy_configured": configured,
            "target_bypasses_proxy": bypass,
            "proxy_selected": bool(configured and not bypass)}


def _models_url(endpoint):
    """Derive the models URL by replacing the chat-completions suffix."""
    if not endpoint.endswith(_CHAT_COMPLETIONS_SUFFIX):
        raise ValueError(_CONFIG_ERROR)
    return endpoint[:-len(_CHAT_COMPLETIONS_SUFFIX)] + "/models"


def _check_record(stage, status, elapsed, failure_kind=None,
                  http_status=None, next_steps=()):
    return {"stage": stage, "status": status,
            "elapsed_seconds": elapsed, "failure_kind": failure_kind,
            "http_status": http_status,
            "next_steps": list(next_steps)}


def _http_next_steps(failure_kind, http_status):
    if failure_kind is None:
        return [_HTTP_OK_STEPS]
    if http_status in (401, 403):
        return [_AUTH_STEPS]
    if http_status == 404:
        return [_NOT_FOUND_STEPS]
    if http_status == 429:
        return [_RATE_LIMIT_STEPS]
    if (http_status is not None and 500 <= http_status <= 599):
        return [_SERVER_ERROR_STEPS]
    if http_status is not None:
        return [_HTTP_RESPONSE_STEPS]
    if failure_kind == "timeout":
        return [_TIMEOUT_STEPS]
    if failure_kind == "tls_certificate_error":
        return [_TLS_CERT_STEPS]
    return [_NO_RESPONSE_STEPS]


def _probe_dns(host, port, timeout):
    start = time.perf_counter()
    ok, value = _run_bounded(
        lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM),
        timeout)
    elapsed = round(time.perf_counter() - start, 3)
    if ok:
        return _check_record("target_dns", "ok", elapsed)
    kind, _ = _classify_probe(value)
    return _check_record("target_dns", "failed", elapsed,
                         failure_kind=kind,
                         next_steps=[_DNS_FAILED_STEPS])


def _safe_http_status(code):
    return code if type(code) is int and 100 <= code <= 599 else None


def _close_probe_response(response):
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except (OSError, RuntimeError, ValueError):
            pass


def _http_probe_result(opener, request, timeout):
    """Open then close inside the worker; return only safe data."""
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        code = exc.code
        _close_probe_response(exc)
        return "http_error", _safe_http_status(code)
    try:
        status = getattr(response, "status",
                         getattr(response, "code", None))
        return "ok", _safe_http_status(status)
    finally:
        _close_probe_response(response)


def _probe_http(generator, models_url, timeout):
    request = urllib.request.Request(
        models_url,
        headers={"Authorization": f"Bearer {generator._api_key}",
                 "Accept": "application/json"},
        method="GET")
    start = time.perf_counter()
    ok, value = _run_bounded(
        lambda: _http_probe_result(generator._opener, request, timeout),
        timeout)
    elapsed = round(time.perf_counter() - start, 3)
    if not ok:
        kind, http_status = _classify_probe(value)
        return _check_record(
            "http_models_get", "failed", elapsed, failure_kind=kind,
            http_status=http_status,
            next_steps=_http_next_steps(kind, http_status))
    outcome, http_status = value
    succeeded = outcome == "ok" and http_status is not None \
        and 200 <= http_status <= 299
    return _check_record(
        "http_models_get", "ok" if succeeded else "failed", elapsed,
        failure_kind=None if succeeded else "http_error",
        http_status=http_status,
        next_steps=_http_next_steps(
            None if succeeded else "http_error", http_status))


def _direct_worker(host, port, timeout, results, cancel):
    """TCP connect then TLS over the same socket; worker-owned socket.

    No HTTP, credentials, or payload cross these sockets; certificate
    and hostname verification are never disabled. Each sub-stage emits
    one sanitized check record on ``results`` so the controller bounds
    them independently; the socket is always closed here, even after
    the controller has given up waiting.
    """
    sock = None
    try:
        start = time.perf_counter()
        try:
            sock = socket.create_connection((host, port),
                                            timeout=timeout)
        except Exception as exc:
            results.put(_check_record(
                "direct_tcp", "failed",
                round(time.perf_counter() - start, 3),
                failure_kind=_classify_probe(exc)[0]))
            results.put(_check_record("direct_tls", "skipped", 0.0))
            return
        results.put(_check_record(
            "direct_tcp", "ok",
            round(time.perf_counter() - start, 3)))
        if cancel.is_set():
            return
        start = time.perf_counter()
        try:
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=host)
        except Exception as exc:
            results.put(_check_record(
                "direct_tls", "failed",
                round(time.perf_counter() - start, 3),
                failure_kind=_classify_probe(exc)[0]))
            return
        results.put(_check_record(
            "direct_tls", "ok",
            round(time.perf_counter() - start, 3)))
    except Exception:
        pass
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass


def _direct_checks(host, port, timeout):
    """Collect direct TCP then TLS records, each bounded by ``timeout``."""
    results = queue.Queue()
    cancel = threading.Event()
    threading.Thread(
        target=_direct_worker,
        args=(host, port, timeout, results, cancel),
        daemon=True).start()
    checks = []
    cancelled = False
    for stage in ("direct_tcp", "direct_tls"):
        if cancelled:
            checks.append(_check_record(
                stage, "skipped", 0.0, next_steps=[_DIRECT_NOTE]))
            continue
        start = time.perf_counter()
        try:
            record = results.get(timeout=timeout)
        except queue.Empty:
            cancel.set()
            cancelled = True
            checks.append(_check_record(
                stage, "failed",
                round(time.perf_counter() - start, 3),
                failure_kind="timeout", next_steps=[_DIRECT_NOTE]))
            continue
        if record["failure_kind"] == "tls_certificate_error":
            record["next_steps"] = [_TLS_CERT_STEPS]
        else:
            record["next_steps"] = [_DIRECT_NOTE]
        checks.append(record)
    return checks


def run_connectivity_check(generator, timeout=10.0, direct=False):
    """Bounded read-only connectivity probes against the Aitta route.

    Performs a target DNS lookup, then always an authenticated GET of
    the derived ``/models`` URL (a proxy can resolve the target even
    when local DNS cannot), and optionally a direct TCP/TLS comparison.
    Each stage has its own wall-clock ``timeout`` bound (not a total
    bound). The report contains fixed safe records only: no addresses,
    paths, URLs, env values, response bodies, or exception text. No
    POST or chat generation is ever requested.
    """
    timeout = _validate_timeout(timeout)
    try:
        parts = urllib.parse.urlsplit(generator._endpoint)
        host = parts.hostname
        port = parts.port or 443
        if parts.scheme != "https" or not host:
            raise ValueError(_CONFIG_ERROR)
        models_url = _models_url(generator._endpoint)
    except (AttributeError, ValueError):
        raise ValueError(_CONFIG_ERROR) from None
    checks = [_probe_dns(host, port, timeout),
              _probe_http(generator, models_url, timeout)]
    if direct:
        checks.extend(_direct_checks(host, port, timeout))
    return {"schema": CONNECTIVITY_SCHEMA,
            "timeout_seconds": timeout,
            "proxy": _proxy_context(parts.netloc, timeout),
            "checks": checks}


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser whose CLI errors become the static config error."""

    def error(self, message):
        raise ValueError(_CONFIG_ERROR)


def main(argv=None):
    parser = _ArgumentParser(
        description="Bounded read-only Aitta connectivity check.")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--env-file", default=None)
    parser.add_argument(
        "--direct", action="store_true",
        help="opt-in direct (proxy-free) TCP/TLS comparison probes")
    try:
        args = parser.parse_args(argv)
        timeout = _validate_timeout(args.timeout)
        from aitta_generator import AittaGenerator
        generator = AittaGenerator.from_env(env_path=args.env_file)
        report = run_connectivity_check(
            generator, timeout=timeout, direct=args.direct)
    except Exception:
        print(json.dumps({"schema": CONNECTIVITY_SCHEMA,
                          "error": _CONFIG_ERROR}))
        return 2
    print(json.dumps(report))
    http_check = next(
        (check for check in report["checks"]
         if check["stage"] == "http_models_get"), None)
    if (http_check is not None and http_check["http_status"] is not None
            and 200 <= http_check["http_status"] <= 299):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
