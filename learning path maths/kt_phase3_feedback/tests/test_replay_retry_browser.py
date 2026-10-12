import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import phase3_paths  # noqa: F401
from aitta_generator import AittaGenerator
from demo_api import make_server
from demo_service import DemoService
from full_feedback import (
    CachedFullFeedbackGenerator, FullFeedbackAittaGenerator)
from replay_provider_scenarios import CaptureCache
from tests.helpers import make_bank, make_taxonomy
from tests.test_demo_service import fake_diagnostics
from tests.test_research_browser import Browser
from tests.test_replay_retry import MODEL, SuccessOpener
from tests.test_scenario_replays import replay_fixture


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1",
                     "opt-in installed Edge + websocket-client browser test")
class ReplayRetryBrowserTests(unittest.TestCase):
    def setUp(self):
        edge = Path(os.environ.get(
            "EDGE_BINARY",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"))
        if not edge.is_file():
            self.skipTest("Microsoft Edge is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bank = make_bank()
        self.taxonomy = make_taxonomy(self.bank)
        self.source, self.report, _ = replay_fixture(
            self.root, self.bank, self.taxonomy, count=4)
        self.opener = SuccessOpener()
        (self.root / "captures").mkdir()
        self.cache = CaptureCache(self.root / "captures", None)
        native = FullFeedbackAittaGenerator(
            AittaGenerator("fake-key-not-real-000000000000",
                           "https://aitta.example.test", model=MODEL,
                           opener=self.opener))
        generator = CachedFullFeedbackGenerator(
            native, self.cache, MODEL, "endpointhash" * 8)
        self.service = DemoService(
            root=self.root / "data", bank=self.bank,
            taxonomy=self.taxonomy,
            diagnostics=fake_diagnostics(self.bank),
            replay_report=self.report, replay_source=self.source,
            provider_mode="hosted", generator=generator)
        self.addCleanup(self.service.close, wait=True)
        self.service.cache = self.cache
        self.pin = f"{secrets.randbelow(1000000):06d}"
        self.server = make_server(self.service, self.pin)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        profile = self.root / "edge"
        self.process = subprocess.Popen([
            str(edge), "--remote-debugging-port=0",
            f"--user-data-dir={profile}", "--headless=new",
            "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--window-size=1440,1100",
            "about:blank"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_browser)
        port_file = profile / "DevToolsActivePort"
        for _ in range(100):
            if port_file.exists():
                break
            time.sleep(.1)
        port = port_file.read_text().splitlines()[0]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json", timeout=10) as response:
            pages = json.load(response)
        self.browser = Browser(next(
            p["webSocketDebuggerUrl"] for p in pages
            if p["type"] == "page"))
        self.browser.viewport(1440, 1100)

    def stop_browser(self):
        if hasattr(self, "browser"):
            try:
                self.browser.command("Browser.close")
            except Exception:
                pass
            self.browser.socket.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)

    def _open_replay_dialog(self):
        b = self.browser
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.js("document.querySelector('#replay-selected').click()")
        b.wait("document.querySelector('dialog[open]')")
        b.js("document.querySelector('#replay-provider-mode')"
             ".value='hosted'; document.querySelector("
             "'#replay-provider-mode').dispatchEvent(new Event('change'))")

    def _wait_run(self, index):
        self.browser.wait(
            "document.querySelector('.run-history > summary')"
            f".textContent.includes('Replay history ({index})') && "
            "document.querySelector('.run-history > summary')"
            ".textContent.includes('Latest: Complete')", timeout=60)
        runs = self.service.replays.list()["runs"]
        return self.service.replays.get(runs[0]["id"])

    def test_hosted_retry_checkbox_flow(self):
        self.opener.failures = 1
        b = self.browser
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)}; "
             "document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('.scenario-row')")
        self._open_replay_dialog()
        self.assertTrue(b.js(
            "document.querySelector('#replay-retry-failed').checked"))
        self.assertFalse(b.js(
            "document.querySelector('#replay-retry-failed')"
            ".closest('label').hidden"))
        self.assertFalse(b.js(
            "document.querySelector('#replay-hosted-consent').checked"))
        b.js("document.querySelector('#confirm-replay').click()")
        self.assertTrue(b.js(
            "Boolean(document.querySelector('dialog[open]'))"))
        b.js("document.querySelector('#replay-hosted-consent')"
             ".checked=true")
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")
        first = self._wait_run(1)
        self.assertEqual(first["cases"][0]["provider_status"], "fallback")
        self.assertEqual(len(self.opener.calls), 1)
        captures = self.root / "captures"
        base_files = [p for p in captures.glob("aitta_full_*.json")
                      if "_attempt_" not in p.name]
        self.assertEqual(len(base_files), 1)
        base_bytes = base_files[0].read_bytes()
        self.assertFalse(json.loads(base_bytes)["ok"])
        self.assertTrue(first["retry_failed_provider_calls"])
        b.js(f"document.querySelector('#scenario-version').value="
             f"{json.dumps(first['id'])}; document.querySelector("
             "'#scenario-version').dispatchEvent(new Event('change'))")
        b.wait("document.querySelector('#scenario-ws-detail')"
               ".textContent.includes('fresh hosted call failed')",
               timeout=30)
        self._open_replay_dialog()
        self.assertTrue(b.js(
            "document.querySelector('#replay-retry-failed').checked"))
        b.js("document.querySelector('#replay-retry-failed')"
             ".checked=false")
        b.js("document.querySelector('#replay-hosted-consent')"
             ".checked=true")
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")
        second = self._wait_run(2)
        self.assertEqual(second["cases"][0]["provider_status"], "fallback")
        self.assertFalse(second["retry_failed_provider_calls"])
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(list(captures.glob("*_attempt_*.json")), [])
        b.wait("document.querySelector('.run-history')"
               ".textContent.includes('capture reuse')")
        self._open_replay_dialog()
        b.js("document.querySelector('#replay-hosted-consent')"
             ".checked=true")
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")
        third = self._wait_run(3)
        self.assertEqual(third["cases"][0]["provider_status"], "ready")
        self.assertTrue(third["retry_failed_provider_calls"])
        self.assertEqual(len(self.opener.calls), 2)
        attempt_files = list(
            captures.glob("aitta_full_*_attempt_*.json"))
        self.assertEqual(len(attempt_files), 1)
        self.assertEqual(base_files[0].read_bytes(), base_bytes)
        b.wait("document.querySelector('.run-history')"
               ".textContent.includes('retry failed calls enabled')")
        b.js(f"document.querySelector('#scenario-version').value="
             f"{json.dumps(third['id'])}; document.querySelector("
             "'#scenario-version').dispatchEvent(new Event('change'))")
        b.wait("document.querySelector('#scenario-ws-detail')"
               ".textContent.includes('Aitta provenance')", timeout=30)
        detail = b.js("document.querySelector('#scenario-ws-detail')"
                      ".textContent")
        self.assertIn("fresh hosted retry", detail)
        self.assertEqual(b.errors, [])


if __name__ == "__main__":
    unittest.main()
