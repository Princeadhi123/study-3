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
from demo_api import make_server
from demo_service import DemoService
from live_diagnostics import LiveDiagnostics
from tests.test_kt_adapter import FakeKT
from tests.test_assessment_worked_review import (
    grounded_rows, make_grounded_bank, make_grounded_taxonomy,
    positions)
from tests.test_research_browser import Browser


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1",
                     "opt-in installed Edge + websocket-client browser test")
class WorkedReviewBrowserTests(unittest.TestCase):
    def setUp(self):
        edge = Path(os.environ.get(
            "EDGE_BINARY",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"))
        if not edge.is_file():
            self.skipTest("Microsoft Edge is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_grounded_bank()
        self.bank["questions"][0]["text"] += (
            " <img src=x onerror=alert(1)>")
        self.bank["questions"][0]["content_text"] = (
            self.bank["questions"][0]["text"] + " [OPTIONS] "
            + " | ".join(self.bank["questions"][0]["options"]))
        self.service = DemoService(
            root=Path(self.tmp.name) / "data", bank=self.bank,
            taxonomy=make_grounded_taxonomy(self.bank),
            diagnostics=LiveDiagnostics(
                model_loader=lambda: FakeKT(self.bank)))
        self.addCleanup(self.service.close, wait=True)
        self.pin = f"{secrets.randbelow(1000000):06d}"
        self.server = make_server(self.service, self.pin)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        profile = Path(self.tmp.name) / "edge"
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

    def _complete_and_release(self):
        wrong = [positions(self.bank, "pct", 0)[0],
                 positions(self.bank, "alg", 0)[0]]
        created = self.service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in grounded_rows(self.bank, wrong):
            self.service.submit_response(sid, token, row)
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.service._load_meta(sid)["provider_job"]["status"] in (
                    "ready", "fallback"):
                break
            time.sleep(0.05)
        else:
            self.fail("provider job did not finish")
        detail = self.service.teacher_session(sid)
        preview = detail["feedback_delivery"]["preview"]
        cards = preview["review_examples"]
        self.assertEqual(len(cards), 2)
        released = self.service.release_feedback(sid, {
            "message_sha256": detail["feedback_delivery"]
            ["preview_sha256"], "reviewer_label": "browser-test"})
        self.assertTrue(released["released"])
        return sid, token, cards

    def _assert_card_rendered(self):
        b = self.browser
        b.wait("document.querySelectorAll('.review-example').length >= 2")
        blob = b.js(
            "(() => Array.from(document.querySelectorAll("
            "'.review-example')).map(c => c.textContent)"
            ".join('\\n'))()")
        for needle in ("Your selected answer:", "Correct answer:",
                       "Explain your reasoning:",
                       "checked worked solution is unavailable",
                       "<img src=x onerror=alert(1)>"):
            self.assertIn(needle, blob)
        self.assertTrue(b.js(
            "document.querySelectorAll("
            "'.review-example ol.worked-steps li').length >= 2"))
        disclosure = b.js(
            "document.querySelector("
            "'details[data-disclosure-key^=\"worked-review-original:\"]')"
            " ? document.querySelector("
            "'details[data-disclosure-key^=\"worked-review-original:\"]')"
            ".textContent : null")
        self.assertIsNotNone(disclosure)
        self.assertIn("Original question wording", disclosure)
        self.assertFalse(b.js(
            "Boolean(document.querySelector("
            "'.review-example script, .review-example img'))"))
        self.assertEqual(b.errors, [])

    def test_teacher_preview_and_student_release_render_cards(self):
        sid, token, cards = self._complete_and_release()
        b = self.browser
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete'")
        b.wait("document.querySelector('#pin-input')")
        b.js(f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)}; "
             "document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("const row = document.querySelector('.session-row, "
             "[data-session]'); if (row) row.click();")
        b.wait("document.querySelector('#teacher-app')"
               ".textContent.includes('Worked review')", timeout=30)
        self._assert_card_rendered()
        b.command("Page.navigate", {
            "url": f"{self.url}/?session={sid}#token={token}"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.wait("document.body.textContent.includes('Worked review')",
               timeout=30)
        self._assert_card_rendered()


if __name__ == "__main__":
    unittest.main()
