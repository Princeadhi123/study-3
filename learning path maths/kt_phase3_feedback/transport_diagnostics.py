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
  zero retries, and ``AittaGenerationError('Aitta request failed')``
  sanitization are all preserved exactly.
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
import copy
import socket
import urllib.error

from evidence_providers import EvidenceAittaGenerator

STAGES = frozenset((
    "not_called", "open_started", "response_received",
    "body_read_started", "body_received", "open_failed",
    "body_read_failed", "close_failed"))
FAILURE_KINDS = frozenset(
    ("timeout", "http_error", "network_error", "transport_error"))
BODY_RECEIVED_FAILURE = "response_contract_or_opening_validation"


def _classify(exc):
    """Map an exception type to a safe failure kind; never reads text."""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        return "http_error", (code if type(code) is int
                              and 100 <= code <= 599 else None)
    if isinstance(exc, urllib.error.URLError):
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            return "timeout", None
        return "network_error", None
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout", None
    return "transport_error", None


class _TransportObserver:
    """Fixed-shape telemetry state; one instance per wrapped call."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.state = {"stage": "not_called", "failure_kind": None,
                      "http_status": None}

    def reached(self, stage):
        self.state["stage"] = stage

    def failed(self, stage, exc):
        kind, status = _classify(exc)
        self.state = {"stage": stage, "failure_kind": kind,
                      "http_status": status}


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
