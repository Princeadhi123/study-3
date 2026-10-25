"""The hosted demo pins its Aitta model regardless of AITTA_MODEL."""
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import demo_service

FAKE_ENV = {"AITTA_API_KEY": "fake-aitta-test-key",
            "AITTA_BASE_URL": "https://aitta.example/v1"}
EXPECTED_ENDPOINT = "https://aitta.example/v1/chat/completions"
PINNED_MODEL = "openai/gpt-oss-120b"


def _blocked_open(self, request, timeout=None):
    raise AssertionError("network access is not allowed in this test")


class HostedModelPinTests(unittest.TestCase):
    def _run(self, env_model):
        env = dict(FAKE_ENV)
        env["AITTA_MODEL"] = env_model
        service = demo_service.DemoService.__new__(demo_service.DemoService)
        with tempfile.TemporaryDirectory() as tmp:
            service.root = Path(tmp)
            service.worked_example_generator = None
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(urllib.request.OpenerDirector,
                                      "open", _blocked_open), \
                    mock.patch.object(
                        demo_service, "FullFeedbackAittaGenerator",
                        wraps=demo_service.FullFeedbackAittaGenerator
                    ) as full_spy, \
                    mock.patch.object(
                        demo_service, "MidpointAittaGenerator",
                        wraps=demo_service.MidpointAittaGenerator
                    ) as midpoint_spy, \
                    mock.patch.object(
                        demo_service, "WorkedExampleAittaGenerator",
                        wraps=demo_service.WorkedExampleAittaGenerator
                    ) as worked_spy:
                service._hosted_adapters(call_budget=1, timeout=120)
        inners = [full_spy.call_args[0][0],
                  midpoint_spy.call_args[0][0],
                  worked_spy.call_args[0][0]]
        for inner in inners:
            self.assertEqual(inner._model, PINNED_MODEL)
            self.assertEqual(inner._timeout, 120)
            self.assertEqual(inner._api_key, FAKE_ENV["AITTA_API_KEY"])
            self.assertEqual(inner._endpoint, EXPECTED_ENDPOINT)

    def test_stale_env_model_is_overridden(self):
        for env_model in ("google/gemma-4-31b-it", "openai/gpt-oss-120b"):
            with self.subTest(env_model=env_model):
                self._run(env_model)


if __name__ == "__main__":
    unittest.main()
