"""Tests for the instrumented Aitta transport observer.

Fully offline: fake openers/responses only, no network, no credentials.
These verify fixed-shape telemetry, sanitization, and that requests,
timeouts, and read limits pass through unchanged — not transport
quality or educational content.
"""
import json
import socket
import unittest
import urllib.error

from aitta_generator import (
    _MAX_RESPONSE_BYTES, AittaGenerationError, AittaGenerator)
from evidence_providers import EvidenceAittaGenerator
from tests.test_aitta_generator import BASE_URL, FAKE_KEY, ok_body
from tests.test_evidence_providers import opening_payload
from tests.test_jev_selector import FakeOpener, FakeResponse
from transport_diagnostics import (
    BODY_RECEIVED_FAILURE, FAILURE_KINDS, STAGES,
    InstrumentedEvidenceAittaGenerator)

TIMEOUT = 12.5


def instrumented(inner_opener=None):
    opener = (inner_opener if inner_opener is not None
              else FakeOpener(ok_body(candidate="observed_summary")))
    inner = AittaGenerator(FAKE_KEY, BASE_URL, timeout=TIMEOUT,
                           opener=opener)
    return InstrumentedEvidenceAittaGenerator(inner), opener


def telemetry(metadata):
    transport = metadata["transport"]
    assert set(transport) == {"stage", "failure_kind", "http_status"}
    return transport


class StageTransitionTests(unittest.TestCase):
    def test_success_records_open_read_close(self):
        generator, opener = instrumented()
        result = generator.generate(opening_payload())
        self.assertEqual(result["candidate_id"], "review_sub_sA")
        transport = telemetry(generator.last_metadata)
        self.assertEqual(transport, {"stage": "body_received",
                                     "failure_kind": None,
                                     "http_status": None})
        self.assertNotIn("failure_stage", generator.last_metadata)

    def test_open_timeout(self):
        generator, _ = instrumented(
            FakeOpener(error=TimeoutError("slow connect")))
        payload = opening_payload()
        with self.assertRaises(AittaGenerationError):
            generator.generate(payload)
        metadata = generator.last_metadata
        transport = telemetry(metadata)
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(transport, {"stage": "open_failed",
                                     "failure_kind": "timeout",
                                     "http_status": None})
        self.assertEqual(metadata["failure_stage"], "open_failed")

    def test_socket_timeout_maps_to_timeout(self):
        generator, _ = instrumented(
            FakeOpener(error=socket.timeout("timed out")))
        payload = opening_payload()
        with self.assertRaises(AittaGenerationError):
            generator.generate(payload)
        self.assertEqual(telemetry(generator.last_metadata)
                         ["failure_kind"], "timeout")

    def test_body_read_timeout(self):
        class ReadTimeoutResponse:
            def read(self, limit=-1):
                self.read_limit = limit
                raise TimeoutError("slow body")

            def close(self):
                self.closed = True

        class SlowReader(FakeOpener):
            def open(self, request, timeout=None):
                self.requests.append(request)
                self.timeouts.append(timeout)
                return ReadTimeoutResponse()

        generator, _ = instrumented(SlowReader())
        payload = opening_payload()
        with self.assertRaises(AittaGenerationError):
            generator.generate(payload)
        metadata = generator.last_metadata
        transport = telemetry(metadata)
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(transport["stage"], "body_read_failed")
        self.assertEqual(transport["failure_kind"], "timeout")
        self.assertEqual(metadata["failure_stage"],
                         "body_read_failed")

    def test_http_401_records_numeric_status(self):
        error = urllib.error.HTTPError(
            "https://aitta.example/private-url", 401,
            "denied with secret", {}, None)
        generator, _ = instrumented(FakeOpener(error=error))
        payload = opening_payload()
        with self.assertRaises(AittaGenerationError):
            generator.generate(payload)
        metadata = generator.last_metadata
        transport = telemetry(metadata)
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(transport, {"stage": "open_failed",
                                     "failure_kind": "http_error",
                                     "http_status": 401})
        blob = json.dumps(metadata)
        self.assertNotIn("https://aitta.example/private-url", blob)
        self.assertNotIn("denied with secret", blob)

    def test_urlerror_with_timeout_reason_is_timeout(self):
        generator, _ = instrumented(FakeOpener(
            error=urllib.error.URLError(TimeoutError("read slow"))))
        payload = opening_payload()
        with self.assertRaises(AittaGenerationError):
            generator.generate(payload)
        self.assertEqual(telemetry(generator.last_metadata)
                         ["failure_kind"], "timeout")

    def test_urlerror_with_string_reason_is_network(self):
        generator, _ = instrumented(FakeOpener(
            error=urllib.error.URLError(
                "connection refused by 10.0.0.9")))
        payload = opening_payload()
        with self.assertRaises(AittaGenerationError):
            generator.generate(payload)
        metadata = generator.last_metadata
        transport = telemetry(metadata)
        self.assertEqual(transport["failure_kind"], "network_error")
        self.assertNotIn("connection refused", json.dumps(metadata))
        self.assertNotIn("10.0.0.9", json.dumps(metadata))

    def test_other_exception_is_transport_error(self):
        generator, _ = instrumented(FakeOpener(
            error=RuntimeError(f"boom {FAKE_KEY}")))
        payload = opening_payload()
        with self.assertRaises(AittaGenerationError):
            generator.generate(payload)
        metadata = generator.last_metadata
        transport = telemetry(metadata)
        self.assertEqual(transport["stage"], "open_failed")
        self.assertEqual(transport["failure_kind"], "transport_error")
        self.assertEqual(metadata["failure_stage"], "open_failed")

    def test_close_failure_recorded(self):
        class BadCloseResponse(FakeResponse):
            def close(self):
                raise OSError("close failed")

        class BadCloseOpener(FakeOpener):
            def open(self, request, timeout=None):
                self.requests.append(request)
                self.timeouts.append(timeout)
                return BadCloseResponse(self.body)

        generator, _ = instrumented(BadCloseOpener(
            ok_body(candidate="observed_summary")))
        payload = opening_payload()
        with self.assertRaises(AittaGenerationError):
            generator.generate(payload)
        transport = telemetry(generator.last_metadata)
        self.assertEqual(transport["stage"], "close_failed")
        self.assertEqual(transport["failure_kind"], "transport_error")

    def test_body_received_contract_failure_stage(self):
        generator, _ = instrumented(FakeOpener(b"not-json-at-all"))
        payload = opening_payload()
        with self.assertRaises(AittaGenerationError):
            generator.generate(payload)
        metadata = generator.last_metadata
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(telemetry(metadata)["stage"], "body_received")
        self.assertEqual(metadata["failure_stage"],
                         BODY_RECEIVED_FAILURE)


class SanitizationTests(unittest.TestCase):
    def test_credential_bearing_exception_never_stored(self):
        key_inside_reason = urllib.error.URLError(
            OSError(f"auth bearer {FAKE_KEY} rejected"))
        generator, _ = instrumented(FakeOpener(error=key_inside_reason))
        payload = opening_payload()
        with self.assertRaises(AittaGenerationError) as ctx:
            generator.generate(payload)
        self.assertEqual(str(ctx.exception), "Aitta request failed")
        blob = json.dumps(generator.last_metadata)
        self.assertNotIn(FAKE_KEY, blob)
        self.assertNotIn("rejected", blob)
        self.assertNotIn("auth bearer", blob)

    def test_telemetry_shape_is_fixed(self):
        generator, _ = instrumented()
        generator.generate(opening_payload())
        transport = telemetry(generator.last_metadata)
        self.assertIn(transport["stage"], STAGES)
        self.assertTrue(transport["failure_kind"] is None
                        or transport["failure_kind"] in FAILURE_KINDS)
        self.assertTrue(transport["http_status"] is None
                        or isinstance(transport["http_status"], int))


class ResetAndPassthroughTests(unittest.TestCase):
    def test_invalid_payload_resets_to_not_called(self):
        generator, _ = instrumented()
        generator.generate(opening_payload())
        self.assertEqual(generator.last_metadata["transport"]["stage"],
                         "body_received")
        invalid = {"schema": "wrong"}
        with self.assertRaises(ValueError):
            generator.generate(invalid)
        metadata = generator.last_metadata
        self.assertEqual(metadata["status"], "not_called")
        self.assertEqual(telemetry(metadata),
                         {"stage": "not_called", "failure_kind": None,
                          "http_status": None})

    def test_success_after_failure_resets(self):
        opener = FakeOpener(
            ok_body(candidate="observed_summary"),
            error=TimeoutError("once"))
        generator, _ = instrumented(opener)
        payload = opening_payload()
        with self.assertRaises(AittaGenerationError):
            generator.generate(payload)
        opener.error = None
        generator.generate(opening_payload())
        self.assertEqual(telemetry(generator.last_metadata),
                         {"stage": "body_received",
                          "failure_kind": None, "http_status": None})

    def test_request_timeout_and_read_limit_unchanged(self):
        plain_opener = FakeOpener(ok_body(candidate="observed_summary"))
        plain = AittaGenerator(FAKE_KEY, BASE_URL, timeout=TIMEOUT,
                               opener=plain_opener)
        generator, opener = instrumented()
        payload = opening_payload()
        generator.generate(payload)
        EvidenceAittaGenerator(plain).generate(payload)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(len(plain_opener.requests), 1)
        watched = opener.requests[0]
        plain_request = plain_opener.requests[0]
        self.assertEqual(watched.data, plain_request.data)
        self.assertEqual(watched.full_url, plain_request.full_url)
        self.assertEqual(watched.get_header("Authorization"),
                         plain_request.get_header("Authorization"))
        self.assertEqual(opener.timeouts, [TIMEOUT])
        self.assertEqual(plain_opener.timeouts, [TIMEOUT])
        response = opener.responses[0]
        self.assertEqual(response.read_limit,
                         _MAX_RESPONSE_BYTES + 1)
        self.assertTrue(response.closed)

    def test_sanitizer_still_applies(self):
        generator, _ = instrumented(FakeOpener(
            error=RuntimeError(f"leak {FAKE_KEY}")))
        payload = opening_payload()
        with self.assertRaises(AittaGenerationError) as ctx:
            generator.generate(payload)
        self.assertEqual(str(ctx.exception), "Aitta request failed")
        self.assertNotIn(FAKE_KEY, repr(ctx.exception))
        self.assertNotIn(FAKE_KEY, json.dumps(generator.last_metadata))


if __name__ == "__main__":
    unittest.main()
