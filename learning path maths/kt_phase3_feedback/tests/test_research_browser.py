import base64
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

from demo_api import make_server
from demo_service import DemoService
from research_workspace import DEFAULT_REPORT, ResearchWorkspace
from tests.helpers import make_bank, make_taxonomy
from tests.test_demo_service import fake_diagnostics
from tests.test_scenario_replays import replay_fixture


class Browser:
    def __init__(self, socket_url):
        import websocket
        self.socket = websocket.create_connection(socket_url, timeout=30, suppress_origin=True)
        self.sequence = 0
        self.errors = []
        self.command("Runtime.enable")
        self.command("Page.enable")
        self.command("Log.enable")

    def command(self, method, params=None):
        self.sequence += 1
        self.socket.send(json.dumps({"id": self.sequence, "method": method, "params": params or {}}))
        while True:
            result = json.loads(self.socket.recv())
            if result.get("id") == self.sequence:
                if "error" in result:
                    raise RuntimeError(result["error"])
                return result.get("result", {})
            if result.get("method") == "Runtime.exceptionThrown":
                self.errors.append(result["params"]["exceptionDetails"])
            if result.get("method") == "Log.entryAdded":
                entry = result["params"]["entry"]
                if "Content Security Policy" in entry.get("text", ""):
                    self.errors.append(entry)

    def js(self, expression):
        if expression.startswith("const "):
            expression = f"(() => {{ {expression} }})()"
        result = self.command("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True})
        if result.get("exceptionDetails"):
            raise AssertionError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    def wait(self, expression):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            try:
                if self.js(f"Boolean({expression})"):
                    return
            except (AssertionError, RuntimeError):
                pass
            time.sleep(.1)
        raise AssertionError(f"Browser condition timed out: {expression}")

    def viewport(self, width, height):
        self.command("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})

    def screenshot(self, name):
        destination = os.environ.get("BROWSER_SCREENSHOTS")
        if destination:
            directory = Path(destination)
            if not directory.is_dir():
                raise ValueError("screenshot directory must already exist")
            data = self.command("Page.captureScreenshot", {"format": "png"})
            (directory / name).write_bytes(base64.b64decode(data["data"]))


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1",
                     "opt-in installed Edge + websocket-client browser test")
class ResearchBrowserTests(unittest.TestCase):
    def setUp(self):
        edge = Path(os.environ.get("EDGE_BINARY", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"))
        if not edge.is_file():
            self.skipTest("Microsoft Edge is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        bank = make_bank()
        self.source, self.report, _ = replay_fixture(self.root, bank, make_taxonomy(bank), count=4)
        self.service = DemoService(root=self.root / "data", bank=bank,
                                   taxonomy=make_taxonomy(bank), diagnostics=fake_diagnostics(bank),
                                   replay_report=self.report, replay_source=self.source)
        self.addCleanup(self.service.close, wait=True)
        self.pin = f"{secrets.randbelow(1000000):06d}"
        self.server = make_server(self.service, self.pin)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        profile = self.root / "edge"
        self.process = subprocess.Popen([
            str(edge), "--remote-debugging-port=0", f"--user-data-dir={profile}",
            "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--window-size=1440,1100", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_browser)
        port_file = profile / "DevToolsActivePort"
        for _ in range(100):
            if port_file.exists():
                break
            time.sleep(.1)
        port = port_file.read_text().splitlines()[0]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=10) as response:
            pages = json.load(response)
        self.browser = Browser(next(p["webSocketDebuggerUrl"] for p in pages if p["type"] == "page"))
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

    def navigate(self, path):
        self.browser.command("Page.navigate", {"url": self.url + path})
        self.browser.wait("document.readyState === 'complete' && typeof api === 'function'")

    def assert_no_overflow(self):
        self.assertFalse(self.browser.js(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth"))

    def test_selected_filtered_all_replays_and_shared_review_views(self):
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('.scenario-row')")
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=evidence]').click()")
        self.assertEqual(b.js("document.querySelectorAll('#scenario-tab-evidence .qrow').length"), 40)
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=review]').click()")
        self.assertTrue(b.js("document.querySelector('#scenario-tab-review').textContent.includes('read-only')"))
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=evidence]').click(); document.querySelector('#replay-selected').click()")
        b.wait("document.querySelector('dialog[open]')")
        self.assertTrue(b.js("document.querySelector('#replay-provider-mode').options[1].disabled"))
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")
        b.wait("document.querySelector('#replay-history').textContent.includes('1/1')")
        b.wait("document.querySelector('#scenario-tab-overview').textContent.includes('What changed')")
        self.assertFalse(b.js("document.querySelector('#scenario-tab-evidence').hidden"))
        self.assertEqual(b.js("document.querySelectorAll('#scenario-tab-evidence .qrow').length"), 40)
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=research]').click()")
        self.assertTrue(b.js("Boolean(document.querySelector('#scenario-tab-research svg'))"))
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=review]').click()")
        b.wait("document.querySelector('#scenario-tab-review form')")
        b.js("const f=document.querySelector('#scenario-tab-review form'); f.querySelectorAll('select').forEach(n=>{n.selectedIndex=1;n.dispatchEvent(new Event('change'));}); f.querySelector('textarea').value='Replay review fixture'; f.querySelector('textarea').dispatchEvent(new Event('input')); f.requestSubmit()")
        b.wait("document.querySelector('#scenario-tab-review .ledger').textContent.includes('Replay review fixture')")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.js("document.querySelector('#scenario-ws-detail').scrollIntoView({block:'start'})")
        b.screenshot("replay-review-mobile.png")
        b.viewport(1440, 1100)
        b.screenshot("replay-review-desktop.png")
        b.js("document.querySelector('#scenario-version').value=''; document.querySelector('#scenario-version').dispatchEvent(new Event('change'))")
        b.wait("document.querySelector('#scenario-tab-review').textContent.includes('read-only')")
        b.js("document.querySelector('#scenario-search').value='Every answer'; document.querySelector('#scenario-search').dispatchEvent(new Event('input')); document.querySelector('#replay-filtered').click()")
        b.wait("document.querySelector('dialog[open]')")
        self.assertIn("2 scenarios", b.js("document.querySelector('dialog h2').textContent"))
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("document.querySelector('#replay-history').textContent.includes('2/2')")
        b.js("document.querySelector('#replay-all').click()")
        b.wait("document.querySelector('dialog[open]')")
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("document.querySelector('#replay-history').textContent.includes('4/4')")
        self.assertEqual(len(self.service.replays.list()["runs"]), 3)
        self.assertEqual(len(self.service.list_sessions()), 0)
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=overview]').click()")
        b.js("document.querySelector('#scenario-ws-detail').scrollIntoView({block:'start'})")
        b.screenshot("replay-changes-desktop.png")
        self.assertEqual(b.errors, [])

    def test_workspace_and_assessment_journey(self):
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        self.assertTrue(b.js("document.querySelector('#library-pane').hidden && document.querySelector('#comparison-pane').hidden"))
        self.assertEqual(b.js("document.querySelector('#pin-input').value"), "")
        if DEFAULT_REPORT.is_file():
            self.service.research = ResearchWorkspace(self.root / "data")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('#scenario-search')")
        expected = 54 if DEFAULT_REPORT.is_file() else 4
        self.assertEqual(b.js("document.querySelectorAll('.scenario-row').length"), expected)
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        self.assertEqual(b.js("document.querySelectorAll('#scenario-ws-tabs .tab').length"), 6)
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=feedback]').click()")
        self.assertEqual(b.js("document.querySelectorAll('#scenario-tab-feedback .compare .col').length"), 4)
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=research]').click()")
        if DEFAULT_REPORT.is_file():
            self.assertTrue(b.js("Boolean(document.querySelector('#scenario-tab-research svg'))"))
        self.assert_no_overflow()
        b.js("document.querySelector('#scenario-ws-detail').scrollIntoView({block:'start'})")
        b.screenshot("scenario-library-desktop.png")
        b.js("document.querySelector('#scenario-search').value='NO MATCH'; document.querySelector('#scenario-search').dispatchEvent(new Event('input'))")
        self.assertEqual(b.js("document.querySelectorAll('.scenario-row').length"), 0)
        b.js("document.querySelector('#scenario-search').value=''; document.querySelector('#scenario-search').dispatchEvent(new Event('input'))")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.screenshot("scenario-library-mobile.png")
        b.viewport(1440, 1100)
        self.service.research = ResearchWorkspace(self.root / "data", self.report)
        b.js("document.querySelector('[data-workspace=comparison]').click()")
        b.wait("document.querySelector('.comparison-setup form')")
        b.js("const f=document.querySelector('.comparison-setup form'); f.querySelector('input').value='browser-fixture'; f.requestSubmit()")
        b.wait("document.querySelector('.judgment-form')")
        self.assertTrue(b.js("document.querySelector('#comparison-pane').textContent.includes('Prior exposure declared')"))
        self.assertFalse(b.js("Boolean(document.querySelector('#comparison-pane a[download]'))"))
        b.screenshot("blind-comparison-desktop.png")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.viewport(1440, 1100)
        for index in range(4):
            b.js("const f=document.querySelector('.judgment-form'); f.querySelector('input[value=tie]').checked=true; f.querySelectorAll('select').forEach(n => n.selectedIndex=1); f.requestSubmit()")
            b.wait(f"document.querySelector('#comparison-pane').textContent.includes('{index + 1} of 4 judgments saved')")
        self.assertEqual(b.js("document.querySelectorAll('#comparison-pane .metric-card').length"), 4)
        exported = b.js("fetch(document.querySelector('#comparison-pane a[download]').href).then(r=>r.json())")
        self.assertEqual(len(exported["tasks"]), 4)
        self.assertTrue(all(t["judgment"]["resolved_preference"] == "tie" for t in exported["tasks"]))
        b.js("document.querySelector('[data-workspace=live]').click(); document.querySelector('#sim-form').requestSubmit()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('[data-tab=review]').click()")
        b.wait("document.querySelector('#tab-review form')")
        b.js("const note=document.querySelector('#tab-review textarea'); note.value='Preserve this unsaved note'; note.dispatchEvent(new Event('input'))")
        time.sleep(2.3)
        self.assertEqual(b.js("document.querySelector('#tab-review textarea').value"), "Preserve this unsaved note")
        b.screenshot("live-review-desktop.png")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.viewport(1440, 1100)
        self.navigate("/")
        b.wait("!document.querySelector('#start-panel').hidden")
        self.assertTrue(b.js("document.querySelector('#start-button').disabled"))
        b.screenshot("assessment-start-desktop.png")
        b.js("document.querySelector('#synthetic-consent').click(); document.querySelector('#start-button').click()")
        b.wait("!document.querySelector('#question-panel').hidden")
        self.assertTrue(b.js("Boolean(document.querySelector('#q-options legend'))"))
        self.assertEqual(b.js("document.activeElement.id"), "q-text")
        for index in range(40):
            if index == 20:
                b.wait("!document.querySelector('#pause-panel').hidden")
                self.assertNotIn("correct", b.js("document.querySelector('#pause-message').textContent").lower())
                b.js("document.querySelector('#continue-button').click()")
            b.wait(f"document.querySelector('#q-pos').textContent === '{index + 1}'")
            b.js("document.querySelector('#q-options input').click(); document.querySelector('#submit-answer').click()")
        b.wait("!document.querySelector('#end-panel').hidden")
        b.viewport(390, 844)
        self.assert_no_overflow()
        self.assertTrue(b.js("document.querySelector('#end-summary').textContent.includes('40')"))
        self.assertEqual(b.errors, [])


if __name__ == "__main__":
    unittest.main()
