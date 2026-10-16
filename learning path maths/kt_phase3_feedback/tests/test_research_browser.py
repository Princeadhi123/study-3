import base64
import contextlib
import io
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
from research_runtime import load_research_bank
from research_workspace import DEFAULT_REPORT, ResearchWorkspace
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_assessment_worked_review import (
    make_grounded_bank, make_grounded_taxonomy)
from tests.test_demo_service import (
    GatedDiagnostics, MULTI_SECTIONS, fake_diagnostics, wait_for)
from tests.test_feedback_variant import (
    WORKED_SECTIONS, FullStubGenerator)
from tests.test_research_workspace import judgment
from tests.test_scenario_replays import replay_fixture
from warm_scenarios import write_capture


class GatedFullStubGenerator(FullStubGenerator):
    """FullStubGenerator whose generate() parks on an Event so tests can
    hold the provider job in a non-terminal state deterministically."""

    def __init__(self, gate, **kwargs):
        super().__init__(**kwargs)
        self.gate = gate

    def generate(self, payload):
        self.gate.wait(timeout=30)
        return super().generate(payload)


class Browser:
    def __init__(self, socket_url):
        import websocket
        self.socket = websocket.create_connection(socket_url, timeout=30, suppress_origin=True)
        self.sequence = 0
        self.errors = []
        self.command("Runtime.enable")
        self.command("Page.enable")
        self.command("Log.enable")
        # Headless Edge can become hidden and suspend the UI's visibility-gated polling.
        self.command("Emulation.setFocusEmulationEnabled", {"enabled": True})

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

    def wait(self, expression, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if self.js(f"Boolean({expression})"):
                    return
            except (AssertionError, RuntimeError):
                pass
            time.sleep(.1)
        state = self.js("JSON.stringify({history: document.querySelector('.run-history > summary')?.textContent, visible: !document.hidden, dialogs: [...document.querySelectorAll('dialog')].map(n => n.textContent)})")
        raise AssertionError(f"Browser condition timed out: {expression}\nState: {state}")

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
                                   replay_report=self.report, replay_source=self.source,
                                   review_mode="legacy_single")
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

    def test_warm_54_library_and_selected_replay(self):
        bank = load_research_bank("warm")[0]
        with contextlib.redirect_stdout(io.StringIO()):
            source, report = write_capture(self.root / "warm_capture")
        self.service.close(wait=True)
        self.service = DemoService(
            root=self.root / "warm_data", bank=make_bank(),
            taxonomy=make_taxonomy(make_bank()), diagnostics=fake_diagnostics(bank),
            replay_report=report, replay_source=source, replay_bank_mode="warm",
            review_mode="legacy_single")
        self.addCleanup(self.service.close, wait=True)
        self.server.RequestHandlerClass.service = self.service
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelectorAll('.scenario-row').length === 54")
        self.assertTrue(b.js("document.querySelector('#library-pane').textContent.includes('Warm research bank')"))
        self.assertIn("54", b.js("document.querySelector('#replay-all').textContent"))
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=evidence]').click()")
        b.wait("document.querySelectorAll('#scenario-tab-evidence .qrow').length === 40")
        prompt = json.dumps({"text": bank["questions"][0]["text"]})
        self.assertIn(b.js(f"englishQuestionText({prompt})"),
                      b.js("document.querySelector('#scenario-tab-evidence').textContent"))
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=graph]').click()")
        graph_text = "document.querySelector('#scenario-tab-graph').textContent"
        b.wait(f"{graph_text}.includes('Norwegian dragons')")
        self.assertFalse(b.js(f"{graph_text}.includes('Norjalaiset')"))
        self.assertFalse(b.js(f"{graph_text}.includes('\\\\n')"))
        self.assertTrue(b.js(
            "Boolean([...document.querySelectorAll('#scenario-tab-graph h4')]"
            ".find((h) => h.textContent === 'Simplify: 5v + 2v'))"))
        b.js("document.querySelector('#replay-selected').click()")
        b.wait("document.querySelector('dialog[open]')")
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")
        b.wait("document.querySelector('.run-history > summary').textContent.includes('Latest: Complete')")
        run = self.service.replays.list()["runs"][0]
        self.assertEqual(run["bank_mode"], "warm")
        self.assertEqual(run["completed"], 1)
        self.assertEqual(run["failed"], 0)
        view = self.service.replays.result(run["id"], run["cases"][0]["scenario_id"])
        self.assertEqual(view["bank_mode"], "warm")
        self.assertNotIn("conformal", json.dumps(view["checkpoints"]))
        # The applied feedback plan is readable in the decision panel, not
        # only inside raw candidate JSON. The overview "What changed" block
        # only exists on a replayed version, so it confirms the detail view
        # has switched to the fresh replay result.
        pane = "document.querySelector('#scenario-tab-feedback')"
        overview = "document.querySelector('#scenario-tab-overview')"
        b.wait(f"{overview}.textContent.includes('What changed')")
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=feedback]').click()")
        b.wait(f"{pane}.textContent.includes('Review evidence')")
        ev = (f"{pane}.querySelector('.end-review-evidence')"
              ".textContent")
        self.assertTrue(b.js(
            f"{ev}.includes('Review topics chosen by')"))
        self.assertTrue(b.js(f"{ev}.includes('Application rules')"))
        self.assertTrue(b.js(
            f"{ev}.includes('KT estimates do not select review "
            "topics')"))
        # A replay with observed errors shows the full planning block.
        b.js("document.querySelector('#scenario-search').value='Every answer incorrect';"
             "document.querySelector('#scenario-search').dispatchEvent(new Event('input'))")
        b.wait("document.querySelectorAll('.scenario-row').length === 1")
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.js("document.querySelector('#replay-selected').click()")
        b.wait("document.querySelector('dialog[open]')")
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")
        b.wait("document.querySelector('.run-history > summary').textContent.includes('Replay history (2)')")
        b.wait("document.querySelector('.run-history > summary').textContent.includes('Latest: Complete')")
        # The version picker selects the new run once its detail is loaded.
        b.wait("document.querySelector('#scenario-version').options.length === 2"
               " && document.querySelector('#scenario-version').value !== ''")
        b.wait(f"{overview}.textContent.includes('What changed')")
        b.wait(f"{ev}.includes('Multiple incorrect answers')")
        self.assertTrue(b.js(
            f"{ev}.includes('0 correct, 10 incorrect of 10 assessed')"))
        self.assertIsNone(b.js(
            f"{pane}.querySelector('.decision-table')"))
        self.assertIsNone(b.js(
            f"{pane}.querySelector('.compare')"))
        self.assertFalse(b.js(
            f"{pane}.textContent.includes('Shared feedback draft')"))
        self.assertFalse(b.js(
            f"{pane}.textContent.includes('All candidates')"))
        b.viewport(390, 844)
        self.assert_no_overflow()
        self.assertEqual(b.errors, [])

    def test_selected_filtered_all_replays_and_shared_detail_views(self):
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('.scenario-row')")
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        self.assertEqual(b.js("document.querySelectorAll('#scenario-ws-tabs .tab').length"), 5)
        self.assertIsNone(b.js("document.querySelector('#scenario-ws-tabs [data-tab=review]')"))
        self.assertIsNone(b.js("document.querySelector('#scenario-tab-review')"))
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=evidence]').click()")
        self.assertEqual(b.js("document.querySelectorAll('#scenario-tab-evidence .qrow').length"), 40)
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=evidence]').click(); document.querySelector('#replay-selected').click()")
        b.wait("document.querySelector('dialog[open]')")
        self.assertTrue(b.js("document.querySelector('#replay-provider-mode').options[1].disabled"))
        b.js("window.__suppressReplayClose = true; document.addEventListener('close', (e) => { if (window.__suppressReplayClose && e.target && e.target.classList && e.target.classList.contains('replay-dialog')) e.stopImmediatePropagation(); }, true)")
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")
        self.assertEqual(b.js("document.querySelectorAll('.replay-dialog').length"), 0)
        b.wait("document.querySelector('#replay-history').textContent.includes('1/1')")
        b.wait("document.querySelector('#scenario-tab-overview').textContent.includes('What changed')")
        self.assertFalse(b.js("document.querySelector('#scenario-tab-evidence').hidden"))
        self.assertEqual(b.js("document.querySelectorAll('#scenario-tab-evidence .qrow').length"), 40)
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=research]').click()")
        self.assertTrue(b.js("Boolean(document.querySelector('#scenario-tab-research svg'))"))
        self.assertIsNone(b.js("document.querySelector('#scenario-ws-tabs [data-tab=review]')"))
        self.assertIsNone(b.js("document.querySelector('#scenario-tab-review')"))
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=feedback]').click()")
        b.wait("document.querySelector('#scenario-tab-feedback').childElementCount > 0")
        self.assertFalse(b.js("document.querySelector('#scenario-tab-feedback').hidden"))
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.js("document.querySelector('#scenario-ws-detail').scrollIntoView({block:'start'})")
        b.screenshot("replay-feedback-mobile.png")
        b.viewport(1440, 1100)
        b.screenshot("replay-feedback-desktop.png")
        b.js("document.querySelector('#scenario-version').value=''; document.querySelector('#scenario-version').dispatchEvent(new Event('change'))")
        b.wait("!document.querySelector('#scenario-tab-overview').textContent.includes('What changed')")
        b.js("document.querySelector('#scenario-search').value='Every answer'; document.querySelector('#scenario-search').dispatchEvent(new Event('input')); document.querySelector('#replay-filtered').click()")
        b.wait("document.querySelector('dialog[open]')")
        self.assertEqual(b.js("document.querySelectorAll('.replay-dialog').length"), 1)
        self.assertEqual(b.js("document.querySelectorAll('#confirm-replay').length"), 1)
        self.assertIn("2 scenarios", b.js("document.querySelector('dialog h2').textContent"))
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")
        self.assertEqual(b.js("document.querySelectorAll('.replay-dialog').length"), 0)
        b.wait("document.querySelector('#replay-history').textContent.includes('2/2')")
        # The counter can reach its total before the worker closes the batch.
        # A subsequent replay must wait for the terminal status, not just counts.
        b.wait("document.querySelector('.run-history > summary').textContent.includes('Latest: Complete')")
        b.js("document.querySelector('#replay-all').click()")
        b.wait("document.querySelector('dialog[open]')")
        self.assertEqual(b.js("document.querySelectorAll('.replay-dialog').length"), 1)
        self.assertEqual(b.js("document.querySelectorAll('#confirm-replay').length"), 1)
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")
        self.assertEqual(b.js("document.querySelectorAll('.replay-dialog').length"), 0)
        b.wait("document.querySelector('.run-history > summary').textContent.includes('Latest: Complete') && document.querySelector('.run-history > summary').textContent.includes('4/4')",
               timeout=60)
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
        self.assertFalse(b.js("Boolean(document.querySelector('#research-readiness'))"))
        self.assertEqual(b.js("document.querySelectorAll('.readiness-card').length"), 0)
        library_text = "document.querySelector('#library-pane').textContent"
        self.assertTrue(b.js(f"{library_text}.includes('Observed answer counts describe this assessment')"))
        self.assertTrue(b.js(f"{library_text}.includes('synthetic test cases, not real learners')"))
        self.assertFalse(b.js("document.querySelector('#library-pane').textContent.toLowerCase().includes('conformal')"))
        self.assertFalse(b.js(f"{library_text}.includes('Replay identity and interpretation limits')"))
        self.assertFalse(b.js("Boolean(document.querySelector('.scenario-row.active'))"))
        try:
            exposure_before = b.js("sessionStorage.getItem('replay-exposure')")
        except AssertionError:
            exposure_before = None
        self.assertNotEqual(exposure_before, "yes")
        self.assert_no_overflow()
        b.screenshot("scenario-library-without-guidance-desktop.png")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.screenshot("scenario-library-without-guidance-mobile.png")
        b.viewport(1440, 1100)
        b.js("document.querySelector('[data-workspace=live]').click()")
        b.wait("document.querySelector('#live-pane') && !document.querySelector('#live-pane').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('#scenario-search')")
        self.assertFalse(b.js("Boolean(document.querySelector('#research-readiness'))"))
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        self.assertEqual(b.js("document.querySelectorAll('#scenario-ws-tabs .tab').length"), 5)
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=feedback]').click()")
        b.wait("Boolean(document.querySelector("
               "'#scenario-tab-feedback .saved-end-feedback'))")
        self.assertFalse(b.js("Boolean(document.querySelector("
            "'#scenario-tab-feedback .release-card'))"))
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=research]').click()")
        research_text = "document.querySelector('#scenario-tab-research').textContent"
        self.assertTrue(b.js(f"{research_text}.includes('predictively evaluated on real ViLLE data')"))
        self.assertTrue(b.js(f"{research_text}.includes('not mastery')"))
        self.assertTrue(b.js(f"{research_text}.includes('simulated sessions are not additional real-learner validation')"))
        self.assertTrue(b.js(f"{research_text}.includes('Why still research-only')"))
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
        self.assertEqual(b.js("document.querySelectorAll('#ws-tabs .tab').length"), 5)
        self.assertIsNone(b.js("document.querySelector('#ws-tabs [data-tab=review]')"))
        self.assertIsNone(b.js("document.querySelector('#tab-review')"))
        b.js("document.querySelector('[data-tab=feedback]').click()")
        b.wait("document.querySelector('#tab-feedback').childElementCount > 0")
        self.assertFalse(b.js("document.querySelector('#tab-feedback').hidden"))
        b.screenshot("live-feedback-desktop.png")
        b.js("document.querySelector('[data-tab=overview]').click()")
        self.assertFalse(b.js("document.querySelector('#tab-overview').hidden"))
        self.assertTrue(b.js("document.querySelector('#tab-feedback').hidden"))
        time.sleep(2.3)
        self.assertTrue(b.js("document.querySelector('#tab-overview').textContent.includes('Answered')"))
        self.assertIsNone(b.js("document.querySelector('#tab-review')"))
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
                self.assertEqual(
                    b.js("document.querySelectorAll('#pause-message h3').length"), 4)
                self.assertFalse(b.js("Boolean(document.querySelector('#pause-message .kind'))"))
                pause_text = b.js("document.querySelector('#pause-message').textContent")
                for marker in ("out_of", "p_correct", "answer_index"):
                    self.assertNotIn(marker, pause_text)
                b.js("document.querySelector('#continue-button').click()")
            b.wait(f"document.querySelector('#q-pos').textContent === '{index + 1}'")
            b.js("document.querySelector('#q-options input').click(); document.querySelector('#submit-answer').click()")
        b.wait("!document.querySelector('#end-panel').hidden")
        b.viewport(390, 844)
        self.assert_no_overflow()
        self.assertTrue(b.js("document.querySelector('#end-summary').textContent.includes('your teacher is reviewing your feedback')"))
        self.assertFalse(b.js("document.querySelector('#end-summary').textContent.includes('Observed result')"))
        self.assertEqual(b.errors, [])

    def test_saved_comparison_delete_confirmation_flow(self):
        research = self.service.research
        target = research.create_comparison({
            "reviewer_label": "Reviewer 1", "audience": "student",
            "prior_exposure": False})
        keeper = research.create_comparison({
            "reviewer_label": "keeper", "audience": "student",
            "prior_exposure": False})
        research.add_judgment(target["id"], judgment(target["task"]))
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=comparison]').click()")
        b.wait("document.querySelectorAll('.saved-comparison').length === 2")
        self.assertFalse(b.js(
            "Boolean(document.querySelector('.saved-comparison button'))"))
        delete_button = (
            "[...document.querySelectorAll('.saved-comparison-row')]"
            ".find((r) => r.textContent.includes('Reviewer 1'))"
            ".querySelector('.delete-comparison')")
        self.assertEqual(
            b.js(f"{delete_button}.getAttribute('aria-label')"),
            "Delete saved review for Reviewer 1")
        b.js(f"{delete_button}.click()")
        b.wait("document.querySelector('.delete-comparison-dialog[open]')")
        dialog = "document.querySelector('.delete-comparison-dialog')"
        self.assertFalse(b.js("Boolean(document.querySelector('.judgment-form'))"))
        self.assertTrue(b.js(
            f"{dialog}.textContent.includes('Delete saved review?')"))
        self.assertTrue(b.js(
            f"{dialog}.textContent.includes('Reviewer 1 · student drafts · 1/4')"))
        self.assertTrue(b.js(
            f"{dialog}.textContent.includes('permanently deletes this blind review')"))
        self.assertEqual(b.js("document.activeElement.textContent"), "Cancel")
        b.screenshot("delete-comparison-dialog.png")
        b.js(f"{dialog}.querySelector('.replay-toolbar .ghost').click()")
        b.wait("!document.querySelector('.delete-comparison-dialog[open]')")
        b.wait("document.querySelectorAll('.saved-comparison').length === 2")
        b.js(f"{delete_button}.click()")
        b.wait("document.querySelector('.delete-comparison-dialog[open]')")
        for kind in ("keyDown", "keyUp"):
            b.command("Input.dispatchKeyEvent", {
                "type": kind, "key": "Escape", "code": "Escape",
                "windowsVirtualKeyCode": 27})
        b.wait("!document.querySelector('.delete-comparison-dialog[open]')")
        b.wait("document.querySelectorAll('.saved-comparison').length === 2")
        b.js("window.__savedFetch = window.fetch; window.fetch = (url, init) => "
             "String(url).includes('/delete') ? Promise.resolve(new Response("
             "'{\"error\":\"stub\"}', {status: 500, headers: "
             "{'Content-Type': 'application/json'}})) : "
             "window.__savedFetch(url, init)")
        b.js(f"{delete_button}.click()")
        b.wait("document.querySelector('.delete-comparison-dialog[open]')")
        b.js(f"{dialog}.querySelector('.danger').click()")
        b.wait(f"Boolean({dialog}.querySelector('[role=alert]')"
               " && !document.querySelector('.delete-comparison-dialog "
               "[role=alert]').hidden)")
        b.wait("document.querySelectorAll('.saved-comparison').length === 2")
        self.assertEqual(research.comparison(target["id"])["completed"], 1)
        b.js("window.fetch = window.__savedFetch")
        b.js(f"{dialog}.querySelector('.danger').click()")
        b.wait("!document.querySelector('.delete-comparison-dialog[open]')")
        b.wait("document.querySelectorAll('.saved-comparison').length === 1")
        b.wait("document.querySelector('#comparison-pane [role=status]')"
               ".textContent.includes('Deleted saved review for Reviewer 1')")
        self.assertFalse(b.js(
            "Boolean([...document.querySelectorAll('.saved-comparison')]"
            ".some((n) => n.textContent.includes('Reviewer 1')))"))
        self.assertTrue(b.js(
            "document.querySelector('#comparison-pane')"
            ".textContent.includes('keeper')"))
        with self.assertRaises(FileNotFoundError):
            research.comparison(target["id"])
        b.js("document.querySelector('.saved-comparison').click()")
        b.wait("document.querySelector('.judgment-form')")
        b.js("[...document.querySelectorAll('#comparison-pane button')]"
             ".find((n) => n.textContent === 'All reviews').click()")
        b.wait("document.querySelectorAll('.saved-comparison').length === 1")
        self.navigate("/teacher")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=comparison]').click()")
        b.wait("document.querySelectorAll('.saved-comparison').length === 1")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.screenshot("saved-reviews-mobile.png")
        b.viewport(1440, 1100)
        b.js("document.querySelector('.delete-comparison').click()")
        b.wait("document.querySelector('.delete-comparison-dialog[open]')")
        b.js("document.querySelector('.delete-comparison-dialog "
             ".danger').click()")
        b.wait("!document.querySelector('.delete-comparison-dialog[open]')")
        b.wait("document.querySelector('#comparison-pane')"
               ".textContent.includes('No reviews yet')")
        self.assert_no_overflow()
        self.assertEqual(b.errors, [])

    def test_student_midpoint_checkin_pause_and_continue(self):
        b = self.browser
        created = self.service.create_session()
        sid = created["session_id"]
        token = created["student_token"]
        url = f"/?session={sid}#token={token}"
        for row in responses(make_bank(), count=20):
            self.service.submit_response(sid, token, row)
        self.navigate(url)
        b.wait("!document.querySelector('#pause-panel').hidden")
        self.assertEqual(
            b.js("document.querySelector('#pause-heading').textContent"),
            "Your halfway check-in")
        self.assertEqual(
            b.js("document.activeElement.id"), "pause-heading")
        pause_text = "document.querySelector('#pause-message').textContent"
        b.wait(f"{pause_text}.includes('You have completed 20 of 40 questions.')")
        self.assertEqual(
            b.js("[...document.querySelectorAll('#pause-message h3')]"
                 ".map((n) => n.textContent)"),
            ["Halfway there", "What is going well",
             "Your answers so far", "Before you continue"])
        for label in ("encouragement", "next_step", "kind"):
            self.assertFalse(b.js(f"{pause_text}.includes('{label}')"))
        self.assertFalse(b.js(
            "document.querySelector('#pause-message .kind')"))
        self.assertFalse(b.js(f"{pause_text}.includes('correct answer')"))
        self.assertFalse(b.js(f"{pause_text}.includes('answer is')"))
        self.assertEqual(
            b.js("document.querySelector('#continue-button').textContent"),
            "Continue to the next 20 questions")
        b.js("document.querySelector('#continue-button').click()")
        b.wait("!document.querySelector('#question-panel').hidden")
        b.wait("document.querySelector('#q-pos').textContent === '21'")
        self.navigate(url)
        b.wait("!document.querySelector('#question-panel').hidden")
        self.assertTrue(b.js("document.querySelector('#pause-panel').hidden"))
        for row in responses(make_bank(), count=40)[20:]:
            self.service.submit_response(sid, token, row)
        b.command("Page.reload", {"ignoreCache": True})
        b.wait("document.readyState === 'complete'"
               " && typeof api === 'function'")
        b.wait("!document.querySelector('#end-panel').hidden")
        summary = "document.querySelector('#end-summary').textContent"
        b.wait(f"{summary}.includes('your teacher is reviewing your feedback')")
        self.assertFalse(b.js(
            "document.querySelector('#end-summary .feedback-section')"))
        self.assertEqual(b.errors, [])

    def test_teacher_midpoint_matches_student_message(self):
        b = self.browser
        bank = make_bank()
        empty = self.service.create_session(label="midpoint-empty")
        created = self.service.create_session()
        sid, token = created["session_id"], created["student_token"]
        rows = responses(bank, count=20)
        for index in (0, 5):
            q = bank["questions"][index]
            rows[index] = {"question_id": q["question_id"],
                           "selected_index": (q["answer_index"] + 1) % 3}
        for row in rows:
            snapshot = self.service.submit_response(sid, token, row)
        saved = snapshot["feedback"]["midpoint"]
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelectorAll('#session-list button').length === 2")
        b.js("[...document.querySelectorAll('#session-list button')]"
             ".find((n) => n.textContent.includes('20/40 answered'))"
             ".click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        card = "#tab-feedback .midpoint-feedback-card"
        b.wait(f"Boolean(document.querySelector('{card}'))")
        expected = []
        for section in saved["sections"]:
            if section.get("title"):
                expected.append("H4:" + section["title"])
            expected.append("P:" + section["text"])
        rendered = ("[...document.querySelectorAll('" + card
                    + " .midpoint-feedback-message > *')]"
                      ".filter((n) => n.matches('h4,p'))"
                      ".map((n) => n.tagName + ':' + n.textContent)")
        self.assertEqual(b.js(rendered), expected)
        card_text = f"document.querySelector('{card}').textContent"
        self.assertTrue(b.js(
            f"{card_text}.includes('Student midpoint check-in')"))
        self.assertTrue(b.js(
            f"{card_text}.includes('does not confirm that the student "
            "opened or read it')"))
        self.assertTrue(b.js(
            f"{card_text}.includes('phase3_student_midpoint_v5')"))
        self.assertFalse(b.js(
            f"document.querySelector('{card} input, {card} button, "
            f"{card} select, {card} textarea')"))
        self.assertFalse(b.js(
            f"document.querySelector('{card} .kind')"))
        view = self.service.teacher_session(sid)
        self.assertIsNone(view["feedback_delivery"]["preview"])
        self.assertFalse(b.js(
            "document.querySelector('#tab-feedback').textContent"
            ".includes('Assessment summary')"))
        for row in responses(bank, correct=False, count=40)[20:]:
            self.service.submit_response(sid, token, row)
        b.wait("document.querySelector('#tab-feedback .release-card')")
        b.wait("document.querySelector('#tab-feedback').textContent"
               ".includes('Awaiting educator review')")
        self.assertEqual(b.js(rendered), expected)
        self.assertFalse(b.js(
            "document.querySelector('#tab-feedback .release-card')"
            ".textContent.includes('Released to the student')"))
        self.assertIsNone(
            self.service.snapshot(sid, token)["feedback"]["end"])
        path = self.root / "data" / "metadata" / f"{sid}.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["checkpoints"]["midpoint"].pop("student_message")
        meta["checkpoints"]["midpoint"].pop("student_message_version")
        path.write_text(json.dumps(meta), encoding="utf-8")
        baseline_text = meta["checkpoints"]["midpoint"][
            "baseline_student"]["message"]["text"]
        b.wait(f"document.querySelector('{card} .midpoint-feedback-message "
               "p').textContent === " + json.dumps(baseline_text))
        self.assertFalse(b.js(
            f"document.querySelector('{card} .midpoint-feedback-message "
            "h4')"))
        self.assertFalse(b.js(f"{card_text}.includes('Message version')"))
        b.wait("document.querySelectorAll('#session-list button')"
               ".length === 2")
        b.js("[...document.querySelectorAll('#session-list button')]"
             ".find((n) => !n.classList.contains('active')).click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        b.wait(f"{card_text}.includes('No saved midpoint message is "
               "available in this result.')")
        self.assertEqual(b.errors, [])

    def test_replay_result_shows_saved_student_midpoint(self):
        b = self.browser
        library = self.service.research.library()
        run = self.service.replays.start({
            "request_id": secrets.token_hex(16),
            "report_sha256": library["report_sha256"],
            "scenario_ids": [library["scenarios"][0]["id"]],
            "provider_mode": "rules", "allow_provider_calls": False})
        self.assertTrue(wait_for(
            lambda: self.service.replays.get(run["id"])["status"]
            not in ("queued", "running", "cancelling"), timeout=30))
        finished = self.service.replays.get(run["id"])
        self.assertEqual(finished["status"], "complete")
        case = finished["cases"][0]
        detail = self.service.teacher_session(case["session_id"])
        saved = detail["checkpoints"]["midpoint"]["student_message"]
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('.scenario-row')")
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.wait("document.querySelector('#scenario-version').options.length === 2")
        b.js(f"document.querySelector('#scenario-version').value={json.dumps(run['id'])}; document.querySelector('#scenario-version').dispatchEvent(new Event('change'))")
        b.wait("document.querySelector('#scenario-tab-overview')"
               ".textContent.includes('What changed')")
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=feedback]').click()")
        card = "#scenario-tab-feedback .midpoint-feedback-card"
        b.wait(f"Boolean(document.querySelector('{card}'))")
        rendered = ("[...document.querySelectorAll('" + card
                    + " .midpoint-feedback-message > *')]"
                      ".filter((n) => n.matches('h4,p'))"
                      ".map((n) => n.tagName + ':' + n.textContent)")
        expected = []
        for section in saved["sections"]:
            if section.get("title"):
                expected.append("H4:" + section["title"])
            expected.append("P:" + section["text"])
        self.assertEqual(b.js(rendered), expected)
        self.assertFalse(b.js(
            f"document.querySelector('{card} input, {card} button')"))
        self.assertEqual(b.errors, [])

    def test_english_midpoint_display_student_and_teacher(self):
        b = self.browser
        bank = make_bank()
        finnish = ["Jaollisuus, tekijät, alkuluvut",
                   "Murtolukujen kerto- ja jakolasku",
                   "Prosenttilaskuja",
                   "Samanmuotoisten termien yhdistäminen"]
        fi_strengths = ("You answered questions correctly in Jaollisuus, "
                        "tekijät, alkuluvut, Murtolukujen kerto- ja "
                        "jakolasku. These were your highest-scoring "
                        "topics in this first set.")
        fi_focus = ("Some questions in Prosenttilaskuja, Samanmuotoisten "
                    "termien yhdistäminen were tricky in this first set. "
                    "Take your time reading the next questions.")
        en_strengths = ("You answered questions correctly in "
                        "Divisibility, factors and primes, Fraction "
                        "multiplication and division. These were your "
                        "highest-scoring topics in this first set.")
        en_focus = ("Some questions in Percentages, Combining like "
                    "terms were tricky in this first set. Take your "
                    "time reading the next questions.")
        created = self.service.create_session(label="english-finnish")
        sid, token = created["session_id"], created["student_token"]
        for row in responses(bank, count=20):
            self.service.submit_response(sid, token, row)
        meta_path = self.root / "data" / "metadata" / f"{sid}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        message = meta["checkpoints"]["midpoint"]["student_message"]
        message["sections"][1]["text"] = fi_strengths
        message["sections"][2]["text"] = fi_focus
        message["text"] = "\n\n".join(
            s["text"] for s in message["sections"])
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        self.navigate(f"/?session={sid}#token={token}")
        b.wait("!document.querySelector('#pause-panel').hidden")
        pause = "document.querySelector('#pause-message').textContent"
        self.assertTrue(b.js(f"{pause}.includes({json.dumps(en_strengths)})"))
        self.assertTrue(b.js(f"{pause}.includes({json.dumps(en_focus)})"))
        for label in finnish:
            self.assertFalse(b.js(f"{pause}.includes({json.dumps(label)})"))
        self.assertEqual(
            b.js("englishMidpointText('Tuntematon asia.')"),
            "Tuntematon asia.")
        self.assertEqual(
            b.js("englishMidpointText('Arithmetic already')"),
            "Arithmetic already")
        self.assertEqual(
            b.js("englishMidpointText('Prosenttilaskuja')"), "Percentages")
        self.assertFalse(b.js(
            "document.querySelector('#pause-message img')"))
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('#session-list button')")
        b.js("document.querySelector('#session-search').value='english-finnish';"
             "document.querySelector('#session-search').dispatchEvent(new Event('input'))")
        b.wait("document.querySelectorAll('#session-list button').length === 1")
        b.js("document.querySelector('#session-list button').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        card = "#tab-feedback .midpoint-feedback-card"
        b.wait(f"Boolean(document.querySelector('{card}'))")
        card_text = f"document.querySelector('{card}').textContent"
        self.assertTrue(b.js(f"{card_text}.includes({json.dumps(en_strengths)})"))
        self.assertTrue(b.js(f"{card_text}.includes({json.dumps(en_focus)})"))
        self.assertTrue(b.js(f"{card_text}.includes('displayed with English topic labels')"))
        for label in finnish:
            self.assertFalse(b.js(f"{card_text}.includes({json.dumps(label)})"))
        stored = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["checkpoints"]["midpoint"]
                         ["student_message"]["sections"][1]["text"],
                         fi_strengths)
        public = self.service.snapshot(sid, token)
        self.assertEqual(public["feedback"]["midpoint"]["sections"][1]
                         ["text"], fi_strengths)
        fallback = self.service.create_session(label="english-fallback")
        sid2, token2 = fallback["session_id"], fallback["student_token"]
        for row in responses(bank, count=20):
            self.service.submit_response(sid2, token2, row)
        meta2_path = self.root / "data" / "metadata" / f"{sid2}.json"
        meta2 = json.loads(meta2_path.read_text(encoding="utf-8"))
        saved2 = meta2["checkpoints"]["midpoint"]["student_message"]
        meta2["checkpoints"]["midpoint"]["student_message"] = {
            "sections": [],
            "text": ("Prosenttilaskenta <img src=x "
                     "onerror=window.__injected=1> Prosenttilaskuja"),
            "total": saved2["total"],
            "skills": saved2["skills"]}
        meta2_path.write_text(json.dumps(meta2), encoding="utf-8")
        self.navigate(f"/?session={sid2}#token={token2}")
        b.wait("!document.querySelector('#pause-panel').hidden")
        b.wait(f"{pause}.includes('Percentages')")
        self.assertFalse(b.js(f"{pause}.includes('Prosenttilaskenta')"))
        self.assertFalse(b.js(f"{pause}.includes('Prosenttilaskuja')"))
        self.assertFalse(b.js(
            "document.querySelector('#pause-message img')"))
        self.assertFalse(b.js("Boolean(window.__injected)"))
        b.wait("document.querySelector('#pause-message .midpoint-summary')")
        self.assertEqual(
            b.js("document.querySelector('#pause-message .midpoint-total')"
                 ".textContent"),
            "Overall: 20 of 20 correct in this first set.")
        self.assertEqual(
            b.js("document.querySelectorAll('#pause-message "
                 ".midpoint-topics tbody tr').length"), 4)
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('#session-search').value='english-fallback';"
             "document.querySelector('#session-search').dispatchEvent(new Event('input'))")
        b.wait("document.querySelectorAll('#session-list button').length === 1")
        b.js("document.querySelector('#session-list button').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        b.wait(f"{card_text}.includes('Percentages')")
        self.assertFalse(b.js(f"{card_text}.includes('Prosenttilask')"))
        self.assertFalse(b.js(
            f"document.querySelector('{card} img')"))
        b.wait(f"document.querySelector('{card} .midpoint-summary')")
        self.assertEqual(
            b.js(f"document.querySelector('{card} .midpoint-total')"
                 ".textContent"),
            "Overall: 20 of 20 correct in this first set.")
        self.assertEqual(
            b.js(f"document.querySelectorAll('{card} .midpoint-topics "
                 "tbody tr').length"), 4)
        self.assertEqual(b.errors, [])

    def test_midpoint_v2_summary_student_and_teacher(self):
        b = self.browser
        bank = make_bank()
        bank["skill_names"] = {
            "sA": "Jaollisuus, tekijät, alkuluvut",
            "sB": "Murtolukujen kerto- ja jakolasku",
            "sC": "Prosenttilaskuja",
            "sD": "Samanmuotoisten termien yhdistäminen"}
        taxonomy = make_taxonomy(bank)
        service = DemoService(
            root=self.root / "data_fi", bank=bank, taxonomy=taxonomy,
            diagnostics=fake_diagnostics(bank),
            review_mode="legacy_single")
        self.addCleanup(service.close, wait=True)
        self.server.RequestHandlerClass.service = service
        self.addCleanup(
            setattr, self.server.RequestHandlerClass, "service",
            self.service)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        rows = responses(bank, count=20)
        for index in (2, 3, 6, 7, 10, 11, 14, 15, 18, 19):
            q = bank["questions"][index]
            rows[index] = {"question_id": q["question_id"],
                           "selected_index": (q["answer_index"] + 1) % 3}
        for row in rows:
            service.submit_response(sid, token, row)
        expected_rows = [
            ["Divisibility, factors and primes", "5 of 5 correct",
             "Number factors, divisibility and prime numbers"],
            ["Fraction multiplication and division", "5 of 5 correct",
             "Multiplying and dividing fractions"],
            ["Percentages", "0 of 5 correct",
             "Working with percentages and amounts"],
            ["Combining like terms", "0 of 5 correct",
             "Simplifying algebraic expressions by combining "
             "matching terms"]]
        rows_js = ("[...document.querySelectorAll("
                   "'.midpoint-topics tbody tr')]"
                   ".map((r) => [...r.children]"
                   ".map((c) => c.textContent))")
        self.navigate(f"/?session={sid}#token={token}")
        b.wait("!document.querySelector('#pause-panel').hidden")
        b.wait("document.querySelector('.midpoint-summary')")
        self.assertEqual(
            b.js("document.querySelector('.midpoint-summary h4')"
                 ".textContent"), "Your first 20 answers")
        self.assertEqual(
            b.js("document.querySelector('.midpoint-total')"
                 ".textContent"),
            "Overall: 10 of 20 correct in this first set.")
        self.assertEqual(
            b.js("document.querySelector('.midpoint-topics caption')"
                 ".textContent"),
            "Topic results from the first 20 answers")
        self.assertEqual(
            b.js("[...document.querySelectorAll("
                 "'.midpoint-topics thead th')]"
                 ".map((n) => n.scope + ':' + n.textContent)"),
            ["col:Topic", "col:First-half result",
             "col:What this topic covers"])
        self.assertEqual(b.js(rows_js), expected_rows)
        pause = "document.querySelector('#pause-message').textContent"
        for label in bank["skill_names"].values():
            self.assertFalse(
                b.js(f"{pause}.includes({json.dumps(label)})"))
        self.assertFalse(b.js(
            f"{pause}.includes('correct answer is')"))
        self.assertFalse(b.js(
            f"{pause}.includes('practice')"))
        order = ("[...document.querySelectorAll('#pause-message > *')]"
                 ".map((n) => n.className || n.tagName)")
        self.assertEqual(
            b.js(f"{order}.indexOf('midpoint-summary')"), 1)
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.viewport(1440, 1100)
        b.js("document.querySelector('#continue-button').click()")
        b.wait("!document.querySelector('#question-panel').hidden")
        for row in responses(bank, correct=False, count=40)[20:]:
            service.submit_response(sid, token, row)
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('#session-list button')")
        b.js("document.querySelector('#session-list button').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        card = "#tab-feedback .midpoint-feedback-card"
        b.wait(f"Boolean(document.querySelector('{card}'))")
        b.wait(f"document.querySelector('{card} .midpoint-summary')")
        self.assertEqual(b.js(rows_js), expected_rows)
        self.assertEqual(
            b.js(f"document.querySelector('{card} .midpoint-total')"
                 ".textContent"),
            "Overall: 10 of 20 correct in this first set.")
        self.assertFalse(b.js(
            f"document.querySelector('{card} input, {card} button')"))
        path = self.root / "data_fi" / "metadata" / f"{sid}.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        message = meta["checkpoints"]["midpoint"]["student_message"]
        message.pop("total")
        message.pop("skills")
        meta["checkpoints"]["midpoint"]["student_message_version"] = (
            "phase3_student_midpoint_v1")
        path.write_text(json.dumps(meta), encoding="utf-8")
        b.wait(f"!document.querySelector('{card} .midpoint-summary')")
        b.wait("document.querySelector('#tab-feedback').textContent"
               ".includes('Awaiting educator review')")
        self.assertEqual(b.errors, [])

    def test_teacher_release_gates_student_feedback(self):
        b = self.browser
        result = self.service.simulate("alternating", 5)
        sid = result["session_id"]
        self.navigate(result["student_url"])
        b.wait("!document.querySelector('#end-panel').hidden")
        summary = "document.querySelector('#end-summary').textContent"
        b.wait(f"{summary}.includes('your teacher is reviewing your feedback')")
        self.assertTrue(b.js(f"{summary}.includes('You answered 20 of 40 questions correctly.')"))
        self.assertFalse(b.js(f"{summary}.includes('Observed result')"))
        self.assertFalse(b.js("document.querySelector('#end-summary .feedback-section')"))
        self.assertFalse(b.js("document.querySelector('#end-summary .skill-list')"))
        self.navigate(result["student_url"])
        b.wait(f"{summary}.includes('You answered 20 of 40 questions correctly.')")
        b.wait(f"{summary}.includes('your teacher is reviewing your feedback')")
        b.screenshot("student-total-awaiting-release.png")
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        view = self.service.teacher_session(sid)
        self.service.release_feedback(sid, {
            "message_sha256": view["feedback_delivery"]["preview_sha256"],
            "reviewer_label": "browser-reviewer"})
        b.wait(f"{summary}.includes('Assessment summary')")
        self.assertTrue(b.js(f"{summary}.includes('reviewed and released')"))
        student_text = b.js(
            "[...document.querySelectorAll('#end-summary > *')]"
            ".slice(1).map((n) => n.textContent).join('')")
        b.screenshot("student-released-feedback.png")
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('#session-list button')")
        b.js("document.querySelector('#session-list button').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        release = "document.querySelector('#tab-feedback .release-card')"
        b.wait(f"Boolean({release})")
        b.wait(f"{release}.textContent.includes('Released to the student')")
        self.assertFalse(b.js(f"{release}.textContent.includes('differs from the released version')"))
        self.assertTrue(b.js(f"{release}.textContent.includes('browser-reviewer')"))
        preview_text = b.js(
            "[...document.querySelectorAll('#tab-feedback .feedback-preview > *')]"
            ".map((n) => n.textContent).join('')")
        self.assertEqual(preview_text, student_text)
        const_btn = "document.querySelector('#tab-feedback .release-card button.primary')"
        self.assertEqual(b.js(f"{const_btn}.textContent"), "Already released")
        self.assertTrue(b.js(f"{const_btn}.disabled"))
        b.screenshot("teacher-released-no-change.png")
        second = self.service.simulate("all_incorrect", 6)
        b.wait("document.querySelectorAll('#session-list button').length === 2")
        b.js("document.querySelectorAll('#session-list button')[0].click()")
        b.wait(f"{release}.textContent.includes('Awaiting educator review')")
        b.js("const ri=document.querySelector('#release-reviewer'); ri.value='browser-reviewer'; ri.dispatchEvent(new Event('input'))")
        b.wait(f"!{const_btn}.disabled")
        b.js(f"{const_btn}.click()")
        b.wait(f"{release}.textContent.includes('Released to the student')")
        self.assertFalse(b.js(f"{release}.textContent.includes('differs from the released version')"))
        self.navigate(second["student_url"])
        b.wait(f"{summary}.includes('Assessment summary')")
        b.screenshot("student-released-incorrect.png")
        self.navigate("/teacher")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('.scenario-row')")
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=feedback]').click()")
        b.wait("Boolean(document.querySelector("
               "'#scenario-tab-feedback .saved-end-feedback'))")
        self.assertFalse(b.js("Boolean(document.querySelector('#scenario-tab-feedback .release-card'))"))
        self.assertFalse(b.js("Boolean(document.querySelector("
            "'#scenario-tab-feedback .compare'))"))
        self.assertEqual(b.errors, [])

    def test_shared_feedback_draft_has_no_teacher_card_or_audience_select(self):
        b = self.browser
        result = self.service.simulate("alternating", 5)
        sid = result["session_id"]
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('#session-list button')")
        b.js("document.querySelector('#session-list button').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        release = "document.querySelector('#tab-feedback .release-card')"
        b.wait(f"Boolean({release})")
        self.assertTrue(b.js(
            f"{release}.textContent.includes('Shared feedback — teacher preview and release')"))
        feedback = "document.querySelector('#tab-feedback')"
        b.wait(f"{feedback}.textContent.includes('Review evidence')")
        self.assertFalse(b.js(f"{feedback}.textContent.includes('teacher end draft')"))
        research_dd = ("document.querySelector('#tab-feedback "
                       "details[data-disclosure-key=feedback-research]')")
        self.assertTrue(b.js(f"Boolean({research_dd})"))
        self.assertFalse(b.js(f"{research_dd}.open"))
        edit_dd = ("document.querySelector('#tab-feedback "
                   "details[data-disclosure-key=feedback-editing]')")
        self.assertTrue(b.js(f"Boolean({edit_dd})"))
        self.assertFalse(b.js(f"{edit_dd}.open"))
        b.js(f"{research_dd}.open = true")
        self.assertIsNone(b.js(
            "document.querySelector('#tab-feedback .compare')"))
        self.assertTrue(b.js(
            "Boolean(document.querySelector('#tab-feedback "
            ".end-review-evidence'))"))
        b.js(f"{edit_dd}.open = true")
        time.sleep(2.3)
        self.assertTrue(b.js(f"{research_dd}.open"))
        self.assertTrue(b.js(f"{edit_dd}.open"))
        self.assertEqual(b.js("document.querySelectorAll('#ws-tabs .tab').length"), 5)
        self.assertIsNone(b.js("document.querySelector('#ws-tabs [data-tab=review]')"))
        self.assertIsNone(b.js("document.querySelector('#tab-review')"))
        b.js("document.querySelector('#ws-tabs [data-tab=overview]').click()")
        self.assertFalse(b.js("document.querySelector('#tab-overview').hidden"))
        self.assertTrue(b.js("document.querySelector('#tab-feedback').hidden"))
        self.assertEqual(b.errors, [])

    def test_integrated_feedback_practice_flow(self):
        import copy
        from unittest import mock
        import feedback_practice
        from tests.test_demo_service import BoundRecommender
        from tests.test_feedback_practice import make_graph, make_pool
        bank = self.service.bank
        pool = make_pool(("sA", "sB"))
        b = self.browser
        disabled = self.service.simulate("all_incorrect", 4)
        examples = dict(feedback_practice.REVIEW_EXAMPLES)
        for sub in ("sub_sA", "sub_sB", "sub_sC", "sub_sD"):
            examples[sub] = {
                "prompt": "Browser worked prompt?",
                "explanation": "Browser worked explanation."}
        with mock.patch.object(feedback_practice,
                               "load_current_practice_pool",
                               return_value=copy.deepcopy(pool)), \
             mock.patch.object(feedback_practice,
                               "teacher_content_context",
                               return_value=make_graph(pool)), \
             mock.patch.dict(feedback_practice.REVIEW_EXAMPLES,
                             examples, clear=True):
            self.service.recommender = BoundRecommender(bank, pool)
            result = self.service.simulate("all_incorrect", 7)
            sid = result["session_id"]
            self.assertTrue(wait_for(
                lambda: self.service._load_meta(sid)
                ["checkpoints"]["end"]["recommendation_job"]["status"]
                != "pending"))
            self.navigate(result["student_url"])
            summary = "document.querySelector('#end-summary').textContent"
            b.wait(f"{summary}.includes('your teacher is reviewing your feedback')")
            self.assertTrue(b.js(f"{summary}.includes('You answered 0 of 40 questions correctly.')"))
            self.assertFalse(b.js(f"{summary}.includes('Pool practice prompt')"))
            self.assertFalse(b.js(f"{summary}.includes('practice activity')"))
            self.assertFalse(b.js(
                f"{summary}.includes('Browser worked prompt')"))
            self.navigate("/teacher")
            b.wait("document.querySelector('#unlock-panel')")
            b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
            b.wait("!document.querySelector('#teacher-app').hidden")
            b.wait("document.querySelector('#session-list button')")
            rows0 = "[...document.querySelectorAll('#session-list button')]"
            b.js(f"{rows0}[0].click()")
            release = "document.querySelector('#tab-feedback .release-card')"
            b.wait(f"Boolean({release})")
            b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
            preview = "document.querySelector('#tab-feedback .feedback-preview')"
            b.wait(f"Boolean({preview}) && {preview}.textContent.includes('practice activity')")
            self.assertTrue(b.js(
                f"{preview}.textContent.includes('Worked review example')"))
            self.assertTrue(b.js(
                f"{preview}.textContent.includes('Browser worked prompt?')"))
            card = "document.querySelector('#tab-feedback .practice-activity')"
            self.assertTrue(b.js(f"{card}.textContent.includes('Pool practice prompt 1?')"))
            self.assertTrue(b.js(f"{card}.textContent.includes('Skill sA')"))
            self.assertTrue(b.js(f"{card}.textContent.includes('Concept sA')"))
            self.assertTrue(b.js(f"{card}.textContent.includes('does not establish mastery')"))
            self.assertTrue(b.js(
                f"{card}.textContent.includes('does not practise the review focus')"))
            self.assertFalse(b.js(f"{card}.textContent.includes('p_correct')"))
            self.assertFalse(b.js(f"{card}.textContent.includes('pool_q1')"))
            research_dd = ("document.querySelector('#tab-feedback "
                           "details[data-disclosure-key=feedback-research]')")
            self.assertFalse(b.js(f"{research_dd}.open"))
            self.assertTrue(b.js(
                f"{research_dd}.textContent.includes('Practice selection evidence')"))
            self.assertTrue(b.js(
                f"{research_dd}.textContent.includes('Estimated correctness "
                "on this question (not mastery)')"))
            self.assertTrue(b.js(
                f"{research_dd}.textContent.includes('0.600')"))
            approve = "document.querySelector('#tab-feedback .release-card button.primary')"
            preview_card = "document.querySelector('#tab-feedback .feedback-preview .practice-activity')"
            self.assertEqual(b.js(f"{approve}.textContent"),
                             "Approve feedback and practice")
            self.assertTrue(b.js(
                f"{release}.textContent.includes('One approval releases this feedback and the practice activity')"))
            sections = "[...document.querySelectorAll('#tab-feedback .release-card textarea.edit-section')]"
            self.assertEqual(b.js(f"{sections}.length"), 4)
            b.js(f"{sections}[0].value='Approved with practice.'; {sections}[0].dispatchEvent(new Event('input'))")
            b.js("const sb=[...document.querySelectorAll('#tab-feedback .release-card button')].find((n) => n.textContent.includes('Save edited draft')); sb.click()")
            b.wait(f"{release}.textContent.includes('Edited draft saved')")
            b.wait(f"!{approve}.disabled")
            b.js(f"{approve}.click()")
            b.wait(f"{release}.textContent.includes('Released to the student')")
            b.js("document.querySelector('#tab-feedback .feedback-preview .practice-activity').scrollIntoView({block:'center'})")
            b.screenshot("teacher-practice-activity-visible.png")
            b.screenshot("teacher-approved-feedback-practice.png")
            card_text_teacher = b.js(f"{preview_card}.textContent")
            example_text_teacher = b.js(
                "document.querySelector('#tab-feedback .feedback-preview "
                ".review-example').textContent")
            self.navigate(result["student_url"])
            b.wait(f"{summary}.includes('practice activity')")
            released_text = b.js(summary)
            self.assertIn("Approved with practice.", released_text)
            self.assertIn("Pool practice prompt 1?", released_text)
            self.assertIn("Browser worked prompt?", released_text)
            self.assertIn("does not practise the review focus",
                          released_text)
            for needle in ("pool_q1", "p_correct", "answer_index"):
                self.assertNotIn(needle, released_text)
            card_text_student = b.js(
                "document.querySelector('#end-summary .practice-activity').textContent")
            self.assertEqual(card_text_student, card_text_teacher)
            self.assertEqual(
                b.js("document.querySelector('#end-summary "
                     ".review-example').textContent"),
                example_text_teacher)
            b.js("document.querySelector('#end-summary .practice-activity').scrollIntoView({block:'center'})")
            b.screenshot("student-practice-activity-visible.png")
            b.screenshot("student-released-feedback-practice.png")
            self.navigate(result["student_url"])
            b.wait(f"{summary}.includes('practice activity')")
            self.assertIn("Pool practice prompt 1?",
                          b.js(summary))
            sid_disabled = disabled["session_id"]
            self.assertTrue(wait_for(
                lambda: self.service._load_meta(sid_disabled)
                ["checkpoints"]["end"]["recommendation_job"]["status"]
                != "pending"))
            self.navigate("/teacher")
            b.wait("!document.querySelector('#teacher-app').hidden")
            rows = "[...document.querySelectorAll('#session-list button')]"
            b.wait(f"{rows}.length >= 2")
            count = b.js(f"{rows}.length")
            self.assertGreaterEqual(count, 2)
            b.js(f"{rows}[{count} - 1].click()")
            b.wait(f"Boolean({release})")
            b.wait(f"{preview} && {preview}.textContent.includes('not enabled for this session')")
            self.assertTrue(b.js(
                f"{card}.textContent.includes('not enabled for this session')"))
            self.assertFalse(b.js(
                f"Boolean(document.querySelector('#tab-feedback .practice-activity ul'))"))
            self.assertFalse(b.js(
                f"{card}.textContent.includes('does not practise the review focus')"))
            self.assertFalse(b.js(
                f"Boolean(document.querySelector('#tab-feedback "
                ".practice-activity .practice-options'))"))
            self.assertEqual(b.errors, [])

    def test_multi_two_topic_aligned_flow(self):
        import copy
        from unittest import mock
        import feedback_practice
        import research_runtime
        from tests.test_demo_service import (
            FakeAlignedRecommender, MultiGenerator, MultiSelector,
            RecordingPredictor)
        from tests.test_feedback_practice import make_graph, make_pool
        bank = self.service.bank
        pool = make_pool(("sA", "sB"))
        examples = dict(feedback_practice.REVIEW_EXAMPLES)
        for sub in ("sub_sA", "sub_sB", "sub_sC", "sub_sD"):
            examples[sub] = {
                "prompt": "Browser worked prompt?",
                "explanation": "Browser worked explanation."}
        pin = f"{secrets.randbelow(1000000):06d}"
        selector = MultiSelector(prefer=("sA", "sB"))
        generator = MultiGenerator()
        predictor = RecordingPredictor({"pool_q1": 0.6, "pool_q2": 0.55})
        service = DemoService(
            root=self.root / "multi-data", bank=bank,
            taxonomy=self.service.taxonomy,
            diagnostics=fake_diagnostics(bank),
            provider_mode="hosted",
            selector=selector,
            generator=generator,
            recommender=FakeAlignedRecommender(pool, predictor))
        self.addCleanup(service.close, wait=True)
        server = make_server(service, pin)
        thread = threading.Thread(
            target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        b = self.browser

        def go(path):
            b.command("Page.navigate", {"url": url + path})
            b.wait("document.readyState === 'complete' && "
                   "typeof api === 'function'")

        with mock.patch.object(
                feedback_practice, "load_current_practice_pool",
                return_value=copy.deepcopy(pool)), \
             mock.patch.object(
                research_runtime, "load_current_practice_pool",
                return_value=copy.deepcopy(pool)), \
             mock.patch.object(
                feedback_practice, "teacher_content_context",
                return_value=make_graph(pool)), \
             mock.patch.dict(feedback_practice.REVIEW_EXAMPLES,
                             examples, clear=True):
            result = service.simulate("all_incorrect", 11)
            sid = result["session_id"]
            self.assertTrue(wait_for(
                lambda: service._load_meta(sid)
                ["provider_job"]["status"] == "ready"))
            go("/teacher")
            b.wait("document.querySelector('#unlock-panel')")
            b.js(f"document.querySelector('#pin-input').value={json.dumps(pin)}; document.querySelector('#unlock-form').requestSubmit()")
            b.wait("!document.querySelector('#teacher-app').hidden")
            b.wait("document.querySelector('#session-list button')")
            b.js("document.querySelector('#session-list button').click()")
            release = "document.querySelector('#tab-feedback .release-card')"
            b.wait(f"Boolean({release})")
            b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
            preview = "document.querySelector('#tab-feedback .feedback-preview')"
            strip = ("document.querySelector('#tab-feedback "
                     ".release-card .release-status')")
            b.wait(f"{preview}.textContent.includes('Pool practice prompt 1?')")
            cards = "[...document.querySelectorAll('#tab-feedback .feedback-preview .practice-activity')]"
            self.assertEqual(b.js(f"{cards}.length"), 2)
            skills = b.js(f"{cards}.map((n) => n.textContent)")
            self.assertIn("Pool practice prompt 1?", skills[0])
            self.assertIn("Pool practice prompt 2?", skills[1])
            heads = "[...document.querySelectorAll('#tab-feedback .feedback-preview .practice-activity h3')]"
            self.assertEqual(
                b.js(f"{heads}.map((n) => n.textContent)"),
                ["Related practice — Concept sA",
                 "Related practice — Concept sB"])
            for index in range(2):
                self.assertTrue(b.js(
                    f"{cards}[{index}].textContent.includes('Choose an option and explain your reasoning.')"))
            self.assertTrue(b.js(
                f"{cards}[0].textContent.includes('related practice')"))
            examples_sel = "[...document.querySelectorAll('#tab-feedback .feedback-preview .review-example')]"
            self.assertEqual(b.js(f"{examples_sel}.length"), 2)
            self.assertTrue(b.js(
                f"{strip}.textContent.includes('Review focus 1')"))
            self.assertTrue(b.js(
                f"{strip}.textContent.includes('Review focus 2')"))
            self.assertTrue(b.js(
                f"{strip}.textContent.includes('Practice 1')"))
            self.assertTrue(b.js(
                f"{strip}.textContent.includes('Practice 2')"))
            research_dd = ("document.querySelector('#tab-feedback "
                           "details[data-disclosure-key=feedback-research]')")
            self.assertFalse(b.js(f"{research_dd}.open"))
            self.assertTrue(b.js(
                f"{research_dd}.textContent.includes('Practice slot 1')"))
            self.assertTrue(b.js(
                f"{research_dd}.textContent.includes('Practice slot 2')"))
            approve = ("document.querySelector('#tab-feedback "
                       ".release-card button.primary')")
            self.assertEqual(b.js(f"{approve}.textContent"),
                             "Approve feedback and activities")
            self.assertTrue(b.js(
                f"{release}.textContent.includes('One approval releases "
                "this feedback and all activities')"))
            strip_cfg = "document.querySelector('#config-strip').textContent"
            self.assertTrue(b.js(
                f"{strip_cfg}.includes('review: aligned_two_topic · up to "
                "2 topics / 2 practice · aligned')"))
            b.viewport(390, 900)
            b.wait(f"Boolean({preview})")
            b.js(f"{cards}[1].scrollIntoView({{block:'center'}})")
            self.assert_no_overflow()
            b.screenshot("teacher-multi-topics-390.png")
            b.viewport(1440, 1100)
            teacher_fragments = b.js(
                "[...document.querySelectorAll('#tab-feedback "
                ".feedback-preview .practice-activity, "
                "#tab-feedback .feedback-preview .review-example, "
                "#tab-feedback .feedback-preview .feedback-section')]"
                ".map((n) => n.textContent)")
            b.screenshot("teacher-multi-topics.png")
            b.js(f"{approve}.click()")
            b.wait(f"{release}.textContent.includes('Released to the student')")
            go(result["student_url"])
            summary = "document.querySelector('#end-summary').textContent"
            b.wait(f"{summary}.includes('Pool practice prompt 2?')")
            student_fragments = b.js(
                "[...document.querySelectorAll('#end-summary "
                ".practice-activity, #end-summary .review-example, "
                "#end-summary .feedback-section')]"
                ".map((n) => n.textContent)")
            self.assertEqual(student_fragments, teacher_fragments)
            blob = b.js(summary)
            for needle in ("pool_q", "p_correct", "answer_index",
                           "review_bundle_"):
                self.assertNotIn(needle, blob)
            b.screenshot("student-multi-topics-released.png")

            predictor.probabilities = {"pool_q1": 0.6, "pool_q2": 0.95}
            partial = service.simulate("all_incorrect", 13)
            sid3 = partial["session_id"]
            self.assertTrue(wait_for(
                lambda: service._load_meta(sid3)
                ["provider_job"]["status"] in ("ready", "fallback")))
            go("/teacher")
            b.wait("!document.querySelector('#teacher-app').hidden")
            rows = "[...document.querySelectorAll('#session-list button')]"
            b.wait(f"{rows}.length >= 2")
            b.js(f"{rows}[0].click()")
            b.wait(f"Boolean({release})")
            b.wait(f"{preview}.textContent.includes('Pool practice prompt 1?')")
            self.assertEqual(b.js(f"{cards}.length"), 2)
            abstained_idx = b.js(
                f"{cards}.findIndex((n) => "
                "n.textContent.includes('No eligible practice activity "
                "was selected'))")
            self.assertGreaterEqual(abstained_idx, 0)
            abstained = f"{cards}[{abstained_idx}]"
            self.assertFalse(b.js(
                f"{abstained}.textContent.includes('Pool practice prompt 2?')"))
            self.assertFalse(b.js(
                f"Boolean({abstained}.querySelector('.practice-options'))"))
            self.assertTrue(b.js(
                f"{abstained}.textContent.includes('Practice for Subtopic')"))

            correct = service.simulate("all_correct", 3)
            sid2 = correct["session_id"]
            self.assertTrue(wait_for(
                lambda: service._load_meta(sid2)
                ["provider_job"]["status"] == "ready"))
            self.assertEqual(generator.calls, 3)
            self.assertEqual(selector.calls, 2)
            self.assertEqual(len(predictor.calls), 2)
            go("/teacher")
            b.wait("!document.querySelector('#teacher-app').hidden")
            b.wait(f"{rows}.length >= 3")
            b.js(f"{rows}[0].click()")
            b.wait(f"Boolean({release}) && Boolean({preview})")
            self.assertFalse(b.js(
                f"{preview}.textContent.includes('assessed answers were "
                "incorrect')"))
            self.assertFalse(b.js(
                f"{preview}.textContent.includes('Pool practice prompt')"))
            self.assertFalse(b.js(
                f"Boolean({preview}.querySelector('.practice-options'))"))
            self.assertEqual(b.errors, [])

    def test_provider_status_strip_pending_fallback_and_edit(self):
        b = self.browser
        gate = threading.Event()
        self.addCleanup(gate.set)
        from tests.test_demo_service import (
            BlockingSelector, FakeGenerator)
        self.service.provider_mode = "hosted"
        self.service.selector = BlockingSelector(gate)
        self.service.generator = FakeGenerator()
        result = self.service.simulate("alternating", 5)
        sid = result["session_id"]
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]["status"]
            == "pending"))
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('#session-list button')")
        b.js("document.querySelector('#session-list button').click()")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        release = "document.querySelector('#tab-feedback .release-card')"
        strip = "document.querySelector('#tab-feedback .release-card .release-status')"
        b.wait(f"Boolean({strip})")
        self.assertTrue(b.js(f"{strip}.textContent.includes('Processing')"))
        self.assertTrue(b.js(
            f"{strip}.textContent.includes('Hosted feedback is still processing')"))
        research_dd = ("document.querySelector('#tab-feedback "
                       "details[data-disclosure-key=feedback-research]')")
        self.assertTrue(b.js(f"Boolean({research_dd})"))
        self.assertFalse(b.js(f"{research_dd}.open"))
        summary = "document.querySelector('#end-summary').textContent"
        self.navigate(result["student_url"])
        b.wait(f"{summary}.includes('your teacher is reviewing your feedback')")
        self.assertFalse(b.js(f"{summary}.includes('Assessment summary')"))
        self.navigate("/teacher")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('#session-list button')")
        b.js("document.querySelector('#session-list button').click()")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        b.wait(f"Boolean({strip})")
        gate.set()
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        b.wait(f"{strip}.textContent.includes('Ready')")

        class FailingGenerator:
            execution = None

            def generate(self, payload):
                raise RuntimeError("generator down")

        self.service.generator = FailingGenerator()
        second = self.service.simulate("all_incorrect", 6)
        sid2 = second["session_id"]
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid2)["provider_job"]["status"]
            == "fallback"))
        rows = "[...document.querySelectorAll('#session-list button')]"
        b.wait(f"{rows}.length >= 2")
        b.js(f"{rows}[0].click()")
        b.wait(f"{strip}.textContent.includes('A provider fallback was recorded')")
        self.assertTrue(b.js(
            f"{strip}.textContent.includes('Review the current preview before approval')"))
        self.assertFalse(b.js(
            f"{strip}.textContent.includes('draft wording is deterministic')"))
        edit_dd = ("document.querySelector('#tab-feedback "
                   "details[data-disclosure-key=feedback-editing]')")
        b.js(f"{edit_dd}.open = true")
        b.js("const f=document.querySelector('#tab-feedback "
             "textarea.edit-section'); "
             "f.value='Teacher rephrased during fallback.'; "
             "f.dispatchEvent(new Event('input'))")
        b.js("const btns=[...document.querySelectorAll("
             "'#tab-feedback .release-card button')]; "
             "btns.find((n) => n.textContent === 'Save edited draft')"
             ".click()")
        b.wait(f"{release}.textContent.includes('Edited draft saved')")
        b.wait(f"{strip}.textContent.includes('teacher-edited')")
        self.assertEqual(b.errors, [])

    def test_finnish_practice_english_render_edit_poll_and_release(self):
        import copy
        from unittest import mock
        import feedback_practice
        from tests.test_demo_service import BoundRecommender
        from tests.test_feedback_practice import make_graph, make_pool
        b = self.browser
        from session_store import bank_fingerprint
        bank = self.service.bank
        bank["skill_names"]["sA"] = (
            "Samanmuotoisten termien yhdistäminen")
        for topic in self.service.taxonomy["topics"]:
            if topic["skill_id"] == "sA":
                topic["skill_name"] = (
                    "Samanmuotoisten termien yhdistäminen")
        self.service.taxonomy["bank_fingerprint"] = bank_fingerprint(bank)
        pool = make_pool(("sA", "sB"))
        prompt_fi = "Sievennä  −4x + 3"
        options_fi = ["7x", "x", "−x", "−4x + 3"]
        pool["questions"][0].update({
            "text": prompt_fi, "options": list(options_fi),
            "content_text": (prompt_fi + " [OPTIONS] "
                             + " | ".join(options_fi))})
        graph = make_graph(pool)
        for row in graph["exercises"]:
            if row["question_id"] == "pool_q1":
                row["concept_id"] = "retain_unlike_terms"
                row["concept_label"] = (
                    "Recognize terms that cannot be combined")
        examples = dict(feedback_practice.REVIEW_EXAMPLES)
        for sub in ("sub_sA", "sub_sB", "sub_sC", "sub_sD"):
            examples[sub] = {"prompt": "Browser worked prompt?",
                             "explanation": "Browser worked explanation."}
        with mock.patch.object(feedback_practice,
                               "load_current_practice_pool",
                               return_value=copy.deepcopy(pool)), \
             mock.patch.object(feedback_practice,
                               "teacher_content_context",
                               return_value=copy.deepcopy(graph)), \
             mock.patch.dict(feedback_practice.REVIEW_EXAMPLES,
                             examples, clear=True):
            self.service.recommender = BoundRecommender(bank, pool)
            result = self.service.simulate("all_incorrect", 7)
            sid = result["session_id"]
            self.assertTrue(wait_for(
                lambda: self.service._load_meta(sid)
                ["checkpoints"]["end"]["recommendation_job"]["status"]
                != "pending"))
            self.navigate("/teacher")
            b.wait("document.querySelector('#unlock-panel')")
            b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
            b.wait("!document.querySelector('#teacher-app').hidden")
            b.wait("document.querySelector('#session-list button')")
            b.js("document.querySelector('#session-list button').click()")
            b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
            release = "document.querySelector('#tab-feedback .release-card')"
            preview = "document.querySelector('#tab-feedback .feedback-preview')"
            card = "document.querySelector('#tab-feedback .practice-activity')"
            b.wait(f"Boolean({card}) && {card}.textContent.includes('Simplify: −4x + 3')")
            self.assertTrue(b.js(
                f"{card}.textContent.includes('Combining like terms')"))
            self.assertFalse(b.js(
                f"{card}.textContent.includes('Samanmuotoisten')"))
            shown_options = b.js(
                f"[...{card}.querySelector('.practice-options')"
                ".querySelectorAll('li')]"
                ".map((n) => n.textContent)")
            self.assertEqual(shown_options, options_fi)
            self.assertIsNone(b.js(
                "document.querySelector('#tab-feedback "
                "details[data-disclosure-key=practice-original]')"))
            self.assertFalse(b.js(
                "document.querySelector('#tab-feedback').textContent"
                ".includes('Original question wording')"))
            self.assertFalse(b.js(
                f"{card}.textContent.includes('Sievennä')"))
            research_dd = ("document.querySelector('#tab-feedback "
                           "details[data-disclosure-key=feedback-research]')")
            edit_dd = ("document.querySelector('#tab-feedback "
                       "details[data-disclosure-key=feedback-editing]')")
            self.assertFalse(b.js(f"{research_dd}.open"))
            self.assertFalse(b.js(f"{edit_dd}.open"))
            b.js(f"{edit_dd}.open = true")
            adv_dd = ("document.querySelector('#tab-feedback "
                      "details[data-disclosure-key="
                      "\"feedback-editing-advanced\"]')")
            b.js(f"{adv_dd}.open = true")
            field = "document.querySelector('#tab-feedback textarea.edit-section')"
            b.js(f"{field}.value='Finnish fixture edit.'; "
                 f"{field}.dispatchEvent(new Event('input')); {field}.focus()")
            time.sleep(2.3)
            self.assertTrue(b.js(f"{edit_dd}.open"))
            self.assertEqual(b.js(f"{field}.value"),
                             "Finnish fixture edit.")
            self.assertEqual(b.js("document.activeElement.id"),
                             b.js(f"{field}.id"))
            b.viewport(390, 844)
            self.assert_no_overflow()
            b.viewport(1440, 1100)
            b.js("const sb=[...document.querySelectorAll("
                 "'#tab-feedback .release-card button')]"
                 ".find((n) => n.textContent === 'Save edited draft'); "
                 "sb.click()")
            b.wait(f"{release}.textContent.includes('Edited draft saved')")
            approve = ("document.querySelector('#tab-feedback "
                       ".release-card button.primary')")
            b.wait(f"!{approve}.disabled")
            b.screenshot("teacher-finnish-practice-preview.png")
            b.js(f"{approve}.click()")
            b.wait(f"{release}.textContent.includes('Released to the student')")
            preview_text = b.js(
                f"[...{preview}.children]"
                ".map((n) => n.textContent).join('')")
            self.navigate(result["student_url"])
            summary = "document.querySelector('#end-summary')"
            b.wait(f"{summary}.textContent.includes('Simplify: −4x + 3')")
            student_text = b.js(
                "[...document.querySelectorAll('#end-summary > *')]"
                ".slice(1).map((n) => n.textContent).join('')")
            self.assertEqual(preview_text, student_text)
            released = b.js(f"{summary}.textContent")
            self.assertIn("Combining like terms", released)
            self.assertIn("Browser worked prompt?", released)
            for needle in ("pool_q1", "p_correct", "answer_index",
                           "item_id", "Samanmuotoisten"):
                self.assertNotIn(needle, released)
            self.assertFalse(b.js(
                f"{summary}.textContent.includes('awaiting')"))
            b.screenshot("student-finnish-practice-released.png")
            self.assertEqual(b.errors, [])

    def test_save_ack_blocks_approval_until_saved_preview_shown(self):
        b = self.browser
        result = self.service.simulate("alternating", 5)
        sid = result["session_id"]
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('#session-list button')")
        b.js("document.querySelector('#session-list button').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        release = "document.querySelector('#tab-feedback .release-card')"
        approve = "document.querySelector('#tab-feedback .release-card button.primary')"
        preview = "document.querySelector('#tab-feedback .feedback-preview')"
        b.wait(f"Boolean({release}) && Boolean({preview})")
        b.js(
            "window.__held = []; window.__releasePosts = [];"
            "window.__origFetch = window.fetch;"
            "window.fetch = (u, o) => {"
            " const url = String(u); const init = o || {};"
            " if (init.method === 'POST' && url.endsWith('/release')) {"
            "  window.__releasePosts.push(JSON.parse(init.body)); }"
            " const response = window.__origFetch(url, init);"
            " if ((!init.method || init.method === 'GET')"
            "  && /\\/api\\/teacher\\/sessions\\/[a-f0-9]{32}$/.test(url)) {"
            "  return response.then((res) => new Promise((resolve) => {"
            "   window.__held.push({res, resolve}); })); }"
            " return response; };")
        b.wait("window.__held.length >= 1")
        unique = "Unique summary wording for the approval race."
        b.js("const f=document.querySelector('#tab-feedback "
             "textarea.edit-section'); "
             f"f.value={json.dumps(unique)}; "
             "f.dispatchEvent(new Event('input'))")
        b.js("const btns=[...document.querySelectorAll("
             "'#tab-feedback .release-card button')]; "
             "btns.find((n) => n.textContent === 'Save edited draft')"
             ".click()")
        b.wait(f"{release}.textContent.includes('Edited draft saved')")
        self.assertTrue(b.js(f"{release}.textContent.includes("
                             "'waiting for the saved preview')"))
        self.assertTrue(b.js(f"{approve}.disabled"))
        self.assertTrue(b.js(
            "[...document.querySelectorAll('#tab-feedback "
            "textarea.edit-section')].every((f) => f.disabled)"))
        self.assertEqual(b.js("window.__releasePosts.length"), 0)
        saved_hash = self.service.teacher_session(
            sid)["feedback_delivery"]["preview_sha256"]
        b.js("window.__held.splice(0).forEach((h) => h.resolve(h.res));"
             "window.fetch = (u, o) => {"
             " const url = String(u); const init = o || {};"
             " if (init.method === 'POST' && url.endsWith('/release')) {"
             "  window.__releasePosts.push(JSON.parse(init.body)); }"
             " return window.__origFetch(url, init); };")
        b.wait(f"{preview}.textContent.includes({json.dumps(unique)})"
               f" && !{approve}.disabled")
        b.js(f"{approve}.click()")
        b.wait(f"{release}.textContent.includes('Released to the student')")
        posts = b.js("window.__releasePosts")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["message_sha256"], saved_hash)
        self.navigate(result["student_url"])
        summary = "document.querySelector('#end-summary')"
        b.wait(f"{summary}.textContent.includes({json.dumps(unique)})")
        self.assertEqual(b.errors, [])

    def test_teacher_feedback_editing_flow(self):
        b = self.browser
        result = self.service.simulate("alternating", 5)
        sid = result["session_id"]
        self.service.simulate("all_incorrect", 6)
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        pick_alt = ("[...document.querySelectorAll('#session-list button')]"
                    ".find((n) => n.textContent.includes('alternate'))")
        pick_inc = ("[...document.querySelectorAll('#session-list button')]"
                    ".find((n) => n.textContent.includes("
                    "'Every answer incorrect'))")
        b.wait(f"Boolean({pick_alt})")
        b.js(f"{pick_alt}.click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        release = "document.querySelector('#tab-feedback .release-card')"
        b.wait(f"Boolean({release})")
        approve = "document.querySelector('#tab-feedback .release-card button.primary')"
        sections = "[...document.querySelectorAll('#tab-feedback .release-card textarea.edit-section')]"
        b.wait(f"{sections}.length === 4")
        self.assertFalse(b.js(
            "document.querySelector('#tab-feedback .release-card input[type=number]')"))
        self.assertFalse(b.js(
            "document.querySelector('#tab-feedback .release-card input.score')"))
        self.assertTrue(b.js(
            f"{release}.textContent.includes('Scores, answer keys and practice tasks are application-owned')"))
        self.assertFalse(b.js(
            f"{release}.textContent.includes('are fixed')"))
        self.assertFalse(b.js(f"{approve}.disabled"))
        b.js("const f=document.querySelector('#tab-feedback textarea.edit-section'); "
             "f.value='Teacher rephrased summary for the class.'; "
             "f.dispatchEvent(new Event('input'))")
        self.assertTrue(b.js(f"{approve}.disabled"))
        b.js("const f2=document.querySelectorAll('#tab-feedback textarea.edit-section')[1]; "
             "f2.value=''; f2.dispatchEvent(new Event('input'))")
        b.js(f"{pick_inc}.click()")
        b.wait(f"Boolean({release})")
        b.js(f"{pick_alt}.click()")
        b.wait(f"{release}.textContent.includes('Awaiting educator review')")
        self.assertTrue(b.js(f"{approve}.disabled"))
        self.assertEqual(b.js(
            "document.querySelector('#tab-feedback textarea.edit-section').value"),
            "Teacher rephrased summary for the class.")
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-feedback textarea.edit-section')[1].value"),
            "")
        b.js("window.__origFetch = window.fetch; window.fetch = (u, o) => "
             "(typeof u === 'string' && u.includes('feedback-edits')) "
             "? new Promise((r) => setTimeout(() => r("
             "window.__origFetch(u, o)), 400)) : window.__origFetch(u, o)")
        b.js("const btns0=[...document.querySelectorAll('#tab-feedback .release-card button')]; "
             "btns0.find((n) => n.textContent === 'Save edited draft').click()")
        self.assertTrue(b.js(
            "[...document.querySelectorAll('#tab-feedback textarea.edit-section')]"
            ".every((f) => f.disabled)"))
        b.js("window.fetch = window.__origFetch")
        b.wait(f"{release}.textContent.includes('was not saved')")
        self.assertTrue(b.js(
            "[...document.querySelectorAll('#tab-feedback textarea.edit-section')]"
            ".every((f) => !f.disabled)"))
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-feedback textarea.edit-section')[1].value"),
            "")
        self.assertTrue(b.js(f"{approve}.disabled"))
        b.js("const f2b=document.querySelectorAll('#tab-feedback textarea.edit-section')[1]; "
             "f2b.value='Observed strengths stay supportive.'; "
             "f2b.dispatchEvent(new Event('input'))")
        b.js("const btns=[...document.querySelectorAll('#tab-feedback .release-card button')]; "
             "btns.find((n) => n.textContent === 'Save edited draft').click()")
        b.wait(f"{release}.textContent.includes('Edited draft saved')")
        b.wait(f"{release}.textContent.includes('teacher-edited')")
        self.assertFalse(b.js(f"{approve}.disabled"))
        edited = b.js(
            "document.querySelector('#tab-feedback .feedback-preview')"
            ".textContent")
        self.assertIn("Teacher rephrased summary for the class.", edited)
        b.screenshot("teacher-edited-feedback.png")
        b.js(f"{approve}.click()")
        b.wait(f"{release}.textContent.includes('Released to the student')")
        self.navigate(result["student_url"])
        summary = "document.querySelector('#end-summary')"
        b.wait(f"{summary}.textContent.includes('Teacher rephrased summary for the class.')")
        self.assertTrue(b.js(
            f"{summary}.textContent.includes('20 of 40')"))
        b.screenshot("student-released-edited.png")
        self.navigate(result["student_url"])
        b.wait(f"{summary}.textContent.includes('Teacher rephrased summary for the class.')")
        self.navigate("/teacher")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait(f"Boolean({pick_alt})")
        b.js(f"{pick_alt}.click()")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        b.wait(f"Boolean({release})")
        b.wait(f"{release}.textContent.includes('Released to the student')")
        b.js("const f3=document.querySelector('#tab-feedback textarea.edit-section'); "
             "f3.value='A follow-up edit after release.'; "
             "f3.dispatchEvent(new Event('input'))")
        self.assertTrue(b.js(f"{approve}.disabled"))
        b.js("const btns2=[...document.querySelectorAll('#tab-feedback .release-card button')]; "
             "btns2.find((n) => n.textContent === 'Save edited draft').click()")
        b.wait(f"{release}.textContent.includes('Edited draft saved')")
        b.wait(f"{release}.textContent.includes('differs from the released version')")
        self.assertFalse(b.js(f"{release}.textContent.includes('provider result changed')"))
        self.assertEqual(
            b.js("aittaProvenance({trace: {phrasing_source: "
                 "'injected_generator_full_sections'}}, "
                 "{capture_file: 'aitta_full_x.json', reused: false, "
                 "metadata: {status: 'completed'}})"),
            "Aitta - fresh hosted response (full structured feedback)")
        self.assertEqual(
            b.js("aittaProvenance({trace: {phrasing_source: "
                 "'injected_generator_full_sections'}}, {})"),
            "Injected full-feedback generator - hosted provenance "
            "unavailable")
        self.assertEqual(b.errors, [])

    def test_teacher_grounded_full_package_editing_flow(self):
        from tests.test_assessment_worked_review import (
            grounded_rows, make_grounded_bank, make_grounded_taxonomy,
            positions)
        b = self.browser
        bank = make_grounded_bank()
        taxonomy = make_grounded_taxonomy(bank)
        self.service.close(wait=True)
        self.service = DemoService(
            root=self.root / "grounded_data", bank=bank,
            taxonomy=taxonomy, diagnostics=fake_diagnostics(bank),
            replay_report=self.report, replay_source=self.source)
        self.addCleanup(self.service.close, wait=True)
        self.server.RequestHandlerClass.service = self.service
        created = self.service.create_session()
        sid, token = (created["session_id"],
                      created["student_token"])
        wrong = sorted({positions(bank, "pct", 0)[0],
                        positions(bank, "alg", 0)[0]})
        for row in grounded_rows(bank, wrong):
            self.service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]
            ["status"] in ("ready", "fallback")))
        original_prompt = self.service.teacher_session(sid)[
            "feedback_delivery"]["preview"]["review_examples"][0][
            "prompt"]
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('#session-list button')")
        b.js("document.querySelector('#session-list button').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        release = "document.querySelector('#tab-feedback .release-card')"
        approve = ("document.querySelector('#tab-feedback "
                   ".release-card button.primary')")
        preview = ("document.querySelector('#tab-feedback "
                   ".feedback-preview')")
        b.wait(f"Boolean({release}) && Boolean({preview})")
        edit_dd = ("document.querySelector('#tab-feedback "
                   "details[data-disclosure-key=feedback-editing]')")
        b.js(f"{edit_dd}.open = true")
        b.js("document.querySelector('#tab-feedback "
             "details[data-disclosure-key="
             "\"feedback-editing-advanced\"]').open = true")
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-feedback "
            "textarea.edit-section').length"), 4)
        b.wait("document.querySelectorAll('#tab-feedback "
               "textarea.edit-content').length > 10")
        step0 = ("document.querySelector('#tab-feedback textarea"
                 "[data-content-path=\"review_examples.0.steps\"]')")
        prompt0 = ("document.querySelector('#tab-feedback textarea"
                   "[data-content-path=\"review_examples.0.prompt\"]')")
        reason0 = ("document.querySelector('#tab-feedback textarea"
                   "[data-content-path=\"review_examples.0.reasoning_question\"]')")
        step1 = ("document.querySelector('#tab-feedback textarea"
                 "[data-content-path=\"review_examples.1.steps\"]')")
        practice0 = ("document.querySelector('#tab-feedback textarea"
                     "[data-content-path=\"practice_activities.0.message\"]')")
        title0 = ("document.querySelector('#tab-feedback textarea"
                  "[data-content-path=\"section_titles.review_focus\"]')")
        for expr in (step0, prompt0, reason0, step1, practice0, title0):
            self.assertTrue(b.js(f"Boolean({expr})"), expr)
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-feedback "
            "[data-disclosure-key=\"feedback-editing-advanced\"] "
            "> details.raw').length"), 5)
        self.assertTrue(b.js(
            "![...document.querySelectorAll('#tab-feedback "
            "[data-disclosure-key=\"feedback-editing-advanced\"] "
            "> details.raw')].some((d) => d.open)"))
        self.assertEqual(b.js(
            "document.querySelector('#tab-feedback textarea"
            "[data-content-path=\"review_examples.1.prompt\"]').value"),
            "Simplify: 3x + 2x")
        b.js(f"{step0}.value='Teacher step <b>one</b>.\\nTeacher step two.'; "
             f"{step0}.dispatchEvent(new Event('input'))")
        b.js(f"{prompt0}.value='Edited first prompt text?'; "
             f"{prompt0}.dispatchEvent(new Event('input'))")
        b.js(f"{reason0}.value='Explain the edited reasoning.'; "
             f"{reason0}.dispatchEvent(new Event('input'))")
        b.js(f"{step1}.value='Custom teacher step.'; "
             f"{step1}.dispatchEvent(new Event('input'))")
        b.js(f"{practice0}.value='Teacher practice note.'; "
             f"{practice0}.dispatchEvent(new Event('input'))")
        b.js(f"{title0}.value='Teacher heading'; "
             f"{title0}.dispatchEvent(new Event('input'))")
        b.js("const f=document.querySelector('#tab-feedback "
             "textarea.edit-section'); "
             "f.value='Edited narrative for the grounded card.'; "
             "f.dispatchEvent(new Event('input'))")
        self.assertTrue(b.js(f"{approve}.disabled"))
        time.sleep(2.3)
        self.assertEqual(b.js(f"{step0}.value"),
                         "Teacher step <b>one</b>.\nTeacher step two.")
        self.assertEqual(b.js(f"{title0}.value"), "Teacher heading")
        b.js("const btns=[...document.querySelectorAll("
             "'#tab-feedback .release-card button')]; "
             "btns.find((n) => n.textContent === 'Save edited draft')"
             ".click()")
        b.wait(f"{release}.textContent.includes('Edited draft saved')")
        b.wait(f"{preview}.textContent.includes('Teacher step')")
        self.assertTrue(b.js(f"{release}.textContent.includes("
                             "'Teacher-edited worked review')"))
        self.assertTrue(b.js(f"{preview}.textContent.includes("
                             "'Teacher heading')"))
        self.assertTrue(b.js(f"{preview}.textContent.includes("
                             "'Custom teacher step.')"))
        self.assertTrue(b.js(f"{preview}.textContent.includes("
                             "'Edited first prompt text?')"))
        self.assertTrue(b.js(f"{preview}.textContent.includes("
                             "'Simplify: 3x + 2x')"))
        self.assertFalse(b.js(
            "Boolean(document.querySelector('#tab-feedback "
            ".feedback-preview b'))"))
        original_dd = ("document.querySelector('#tab-feedback "
                       ".feedback-preview details[data-disclosure-key"
                       "=\"worked-review-original-content:0\"]')")
        self.assertTrue(b.js(f"Boolean({original_dd})"))
        self.assertTrue(b.js(
            f"{original_dd}.textContent.includes("
            f"{json.dumps(original_prompt)})"))
        b.wait(f"!{approve}.disabled")
        b.screenshot("teacher-grounded-full-edit.png")
        b.js(f"{approve}.click()")
        b.wait(f"{release}.textContent.includes('Released to the student')")
        self.navigate(f"/?session={sid}#token={token}")
        summary = "document.querySelector('#end-summary')"
        b.wait(f"{summary}.textContent.includes('Teacher step <b>one</b>.')")
        self.assertTrue(b.js(
            f"{summary}.textContent.includes('Teacher practice note.')"))
        self.assertTrue(b.js(
            f"{summary}.textContent.includes('Explain the edited reasoning.')"))
        self.assertTrue(b.js(
            f"{summary}.textContent.includes('Teacher heading')"))
        self.assertTrue(b.js(
            f"{summary}.textContent.includes('Edited first prompt text?')"))
        self.assertTrue(b.js(
            f"{summary}.textContent.includes('Simplify: 3x + 2x')"))
        self.assertTrue(b.js(
            f"{summary}.textContent.includes('Teacher-edited worked review')"))
        self.assertFalse(b.js(
            f"{summary}.textContent.includes('verify before release')"))
        self.assertFalse(b.js(
            "Boolean(document.querySelector('#end-summary b'))"))
        b.screenshot("student-grounded-full-edit.png")
        self.assertEqual(b.errors, [])

    def test_teacher_selected_practice_editing_flow(self):
        import copy
        from unittest import mock
        import feedback_practice
        import research_runtime
        from tests.test_demo_service import (
            FakeAlignedRecommender, MultiGenerator, MultiSelector,
            RecordingPredictor)
        from tests.test_feedback_practice import make_graph, make_pool
        b = self.browser
        bank = make_bank()
        pool = make_pool(("sA", "sB"))
        for patch in (
                mock.patch.object(
                    feedback_practice, "load_current_practice_pool",
                    return_value=copy.deepcopy(pool)),
                mock.patch.object(
                    research_runtime, "load_current_practice_pool",
                    return_value=copy.deepcopy(pool)),
                mock.patch.object(
                    feedback_practice, "teacher_content_context",
                    return_value=make_graph(pool))):
            patch.start()
            self.addCleanup(patch.stop)
        recommender = FakeAlignedRecommender(
            pool, RecordingPredictor({"pool_q1": 0.6, "pool_q2": 0.55}))
        self.service.close(wait=True)
        self.service = DemoService(
            root=self.root / "aligned_data", bank=bank,
            taxonomy=make_taxonomy(bank),
            diagnostics=fake_diagnostics(bank),
            replay_report=self.report, replay_source=self.source,
            provider_mode="hosted",
            selector=MultiSelector(prefer=("sA", "sB")),
            generator=MultiGenerator(), recommender=recommender)
        self.addCleanup(self.service.close, wait=True)
        self.server.RequestHandlerClass.service = self.service
        created = self.service.create_session()
        sid, token = (created["session_id"],
                      created["student_token"])
        wrong_skills = {"sA", "sB", "sC"}
        for question in bank["questions"]:
            index = question["answer_index"]
            if question["skill_id"] in wrong_skills:
                index = (index + 1) % len(question["options"])
            self.service.submit_response(sid, token, {
                "question_id": question["question_id"],
                "selected_index": index})
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]
            ["status"] in ("ready", "fallback")))
        preview = self.service.teacher_session(sid)[
            "feedback_delivery"]["preview"]
        activities = preview["practice_activities"]
        self.assertTrue(all(a["status"] == "selected"
                            for a in activities))
        self.assertTrue(all(a["question"]["options"]
                            for a in activities))
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('#session-list button')")
        b.js("document.querySelector('#session-list button').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        release = "document.querySelector('#tab-feedback .release-card')"
        approve = ("document.querySelector('#tab-feedback "
                   ".release-card button.primary')")
        preview_el = ("document.querySelector('#tab-feedback "
                      ".feedback-preview')")
        b.wait(f"Boolean({release}) && Boolean({preview_el})")
        edit_dd = ("document.querySelector('#tab-feedback "
                   "details[data-disclosure-key=feedback-editing]')")
        b.js(f"{edit_dd}.open = true")
        instruction = ("document.querySelector('#tab-feedback textarea"
                       "[data-content-path=\"practice_activities.0.instruction\"]')")
        question_text = ("document.querySelector('#tab-feedback textarea"
                         "[data-content-path=\"practice_activities.0.question.text\"]')")
        option0 = ("document.querySelector('#tab-feedback textarea"
                   "[data-content-path=\"practice_activities.0.question.options.0\"]')")
        message0 = ("document.querySelector('#tab-feedback textarea"
                     "[data-content-path=\"practice_activities.0.message\"]')")
        relation0 = ("document.querySelector('#tab-feedback textarea"
                      "[data-content-path=\"practice_activities.0.relationship.message\"]')")
        for expr in (instruction, question_text, option0, message0,
                     relation0):
            self.assertTrue(b.js(f"Boolean({expr})"), expr)
        b.js(f"{instruction}.value='Teacher instruction text.'; "
             f"{instruction}.dispatchEvent(new Event('input'))")
        b.js(f"{question_text}.value='Teacher practice question?'; "
             f"{question_text}.dispatchEvent(new Event('input'))")
        b.js(f"{option0}.value='Teacher option A'; "
             f"{option0}.dispatchEvent(new Event('input'))")
        b.js(f"{message0}.value='Teacher practice status note.'; "
             f"{message0}.dispatchEvent(new Event('input'))")
        b.js(f"{relation0}.value='Teacher relationship note.'; "
             f"{relation0}.dispatchEvent(new Event('input'))")
        self.assertTrue(b.js(f"{approve}.disabled"))
        b.js("const btns=[...document.querySelectorAll("
             "'#tab-feedback .release-card button')]; "
             "btns.find((n) => n.textContent === 'Save edited draft')"
             ".click()")
        b.wait(f"{release}.textContent.includes('Edited draft saved')")
        b.wait(f"{preview_el}.textContent.includes("
               "'Teacher practice question?')")
        for needle in ("Teacher instruction text.", "Teacher option A",
                       "Teacher practice status note.",
                       "Teacher relationship note.",
                       "Teacher-edited practice — verify before release"):
            self.assertTrue(b.js(
                f"{preview_el}.textContent.includes({json.dumps(needle)})"),
                needle)
        original_dd = ("document.querySelector('#tab-feedback "
                       ".feedback-preview details[data-disclosure-key"
                       "=\"practice-original-content:0\"]')")
        self.assertTrue(b.js(f"Boolean({original_dd})"))
        self.assertTrue(b.js(
            f"{original_dd}.textContent.includes('Pool practice prompt 1?')"))
        b.wait(f"!{approve}.disabled")
        b.js(f"{approve}.click()")
        b.wait(f"{release}.textContent.includes('Released to the student')")
        self.navigate(f"/?session={sid}#token={token}")
        summary = "document.querySelector('#end-summary')"
        b.wait(f"{summary}.textContent.includes('Teacher practice question?')")
        for needle in ("Teacher instruction text.", "Teacher option A",
                       "Teacher practice status note.",
                       "Teacher relationship note.",
                       "Teacher-edited practice"):
            self.assertTrue(b.js(
                f"{summary}.textContent.includes({json.dumps(needle)})"),
                needle)
        self.assertFalse(b.js(
            f"{summary}.textContent.includes('verify before release')"))
        b.screenshot("student-selected-practice-edit.png")
        self.assertEqual(b.errors, [])

    def test_warm_cold_bank_ui_tokens_translation_and_teacher_context(self):
        from research_runtime import load_research_bank
        b = self.browser
        self.navigate("/")
        b.wait("!document.querySelector('#start-panel').hidden")
        for mode in ("warm", "cold"):
            bank, _, _ = load_research_bank(mode)
            b.js(f"document.querySelector('#bank-mode').value={json.dumps(mode)}")
            b.js("document.querySelector('#synthetic-consent').click(); document.querySelector('#start-button').click()")
            b.wait("!document.querySelector('#question-panel').hidden")
            self.assertIn(mode.title(), b.js("document.querySelector('#session-bank').textContent"))
            self.assertEqual(b.js("document.querySelector('#q-text').textContent"),
                             b.js(f"englishQuestionText({json.dumps({'text': bank['questions'][0]['text']})})"))
            b.js("document.querySelector('#display-language').value='fi'; document.querySelector('#display-language').dispatchEvent(new Event('change'))")
            b.wait(f"document.querySelector('#q-text').textContent === {json.dumps(bank['questions'][0]['text'])}")
            b.viewport(390, 844)
            self.assert_no_overflow()
            for index, question in enumerate(bank["questions"]):
                if index == 20:
                    b.wait("!document.querySelector('#pause-panel').hidden")
                    b.js("document.querySelector('#continue-button').click()")
                b.wait(f"document.querySelector('#q-pos').textContent === '{index + 1}'")
                chosen = (question["answer_index"] if index % 2 == 0
                          else (question["answer_index"] + 1) % len(question["options"]))
                b.js(f"document.querySelector('#q-options input[value=\"{chosen}\"]').click(); document.querySelector('#submit-answer').click()")
            b.wait("!document.querySelector('#end-panel').hidden")
            b.wait("document.querySelector('#end-summary').textContent.includes('reviewing your feedback')")
            sid = self.service.list_sessions()[0]["session_id"]
            view = self.service.teacher_session(sid)
            self.service.release_feedback(sid, {
                "message_sha256":
                    view["feedback_delivery"]["preview_sha256"]})
            b.wait("document.querySelector('#end-summary').textContent.includes('20 of 40')")
            b.js("document.querySelector('#new-session').click(); document.querySelector('#display-language').value='en'")
            b.wait("!document.querySelector('#start-panel').hidden")
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('#session-list button').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=graph]').click()")
        b.wait("document.querySelector('#tab-graph').textContent.includes('Practice drafts (24)')")
        self.assertIn("pending formal educator review", b.js("document.querySelector('#tab-graph').textContent"))
        self.assertNotIn("conformal", b.js("document.body.textContent").lower())
        self.assert_no_overflow()
        self.assertEqual(b.errors, [])

    def test_replay_feedback_variant_flow(self):
        # Aligned mode is required for the worked-first variant; rules
        # mode keeps the whole flow local with no provider calls.
        self.service.close(wait=True)
        bank = make_bank()
        self.service = DemoService(
            root=self.root / "aligned_data", bank=bank,
            taxonomy=make_taxonomy(bank), diagnostics=fake_diagnostics(bank),
            replay_report=self.report, replay_source=self.source)
        self.addCleanup(self.service.close, wait=True)
        self.server.RequestHandlerClass.service = self.service
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelectorAll('.scenario-row').length === 4")

        def replay_case(variant, hosted=False):
            known = {r["id"] for r in self.service.replays.list()["runs"]}
            b.js("document.querySelector('.scenario-row').click()")
            b.wait("!document.querySelector('#scenario-ws-detail').hidden")
            b.js("document.querySelector('#replay-selected').click()")
            b.wait("document.querySelector('dialog[open]')")
            b.js(f"document.querySelector('#replay-feedback-variant').value={json.dumps(variant)}")
            if variant == "worked_first":
                b.js("document.querySelector('#replay-feedback-variant').dispatchEvent(new Event('change'))")
                self.assertIn("educator review is required",
                              b.js("document.querySelector('.replay-dialog').textContent"))
            if hosted:
                b.js("document.querySelector('#replay-provider-mode').value='hosted';"
                     "document.querySelector('#replay-provider-mode').dispatchEvent(new Event('change'));"
                     "document.querySelector('#replay-hosted-consent').click()")
            b.js("document.querySelector('#confirm-replay').click()")
            b.wait("!document.querySelector('dialog[open]')")
            self.assertTrue(wait_for(
                lambda: any(r["id"] not in known
                            and r["feedback_variant"] == variant
                            for r in self.service.replays.list()["runs"])),
                "new replay run was not admitted")
            run = next(r for r in self.service.replays.list()["runs"]
                       if r["id"] not in known)
            self.assertTrue(wait_for(
                lambda: self.service.replays.get(run["id"])["status"]
                == "complete"),
                f"replay run {run['id']} did not complete")
            run = self.service.replays.get(run["id"])
            b.wait(f"document.querySelector('.run-record[data-run-id=\"{run['id']}\"] > summary')"
                   ".textContent.includes('Complete')")
            return run

        first = replay_case("worked_first")
        self.assertEqual(first["feedback_variant"], "worked_first")
        sid_w = first["cases"][0]["session_id"]
        self.assertEqual(
            self.service._load_meta(sid_w)["feedback_variant"],
            "worked_first")
        b.wait("[...document.querySelectorAll('#scenario-version option')].some((o) => o.textContent.includes('Worked-first'))")
        self.assertIn("Worked-first", b.js(
            f"document.querySelector('.run-record[data-run-id=\"{first['id']}\"] > summary').textContent"))
        view = self.service.replays.result(
            first["id"], first["cases"][0]["scenario_id"])
        self.assertEqual(view["feedback_variant"], "worked_first")
        self.assertEqual(
            view["feedback_delivery"]["preview"]["template_version"],
            "phase3_student_feedback_worked_first_full_v1")
        # Rules-only worked-first records deterministic wording; the UI
        # must not imply an Aitta narrative ran.
        b.js(f"document.querySelector('#scenario-version').value={json.dumps(first['id'])};"
             "document.querySelector('#scenario-version').dispatchEvent(new Event('change'))")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        detail_text = b.js(
            "document.querySelector('#scenario-ws-detail').textContent")
        self.assertIn("Worked-first", detail_text)
        self.assertNotIn("shape-checked, not fact-verified", detail_text)

        second = replay_case("question_first")
        self.assertEqual(second["feedback_variant"], "question_first")
        sid_q = second["cases"][0]["session_id"]
        self.assertNotEqual(sid_q, sid_w)
        self.assertEqual(
            self.service._load_meta(sid_q)["feedback_variant"],
            "question_first")
        b.wait("[...document.querySelectorAll('#scenario-version option')].some((o) => o.textContent.includes('Question-first'))")
        self.assert_no_overflow()
        b.viewport(390, 844)
        self.assert_no_overflow()
        self.assertEqual(b.errors, [])

    def test_replay_dialog_disables_worked_first_in_legacy(self):
        # The default setUp service runs legacy_single mode; the
        # worked-first variant is aligned-only and must not be offered.
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelectorAll('.scenario-row').length === 4")
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.js("document.querySelector('#replay-selected').click()")
        b.wait("document.querySelector('dialog[open]')")
        self.assertTrue(b.js(
            "document.querySelector('#replay-feedback-variant')"
            ".options[1].disabled"))
        self.assertEqual(
            "question_first",
            b.js("document.querySelector('#replay-feedback-variant')"
                 ".value"))
        b.js("document.querySelector('dialog[open]').close()")
        self.assertEqual(b.errors, [])

    def test_replay_worked_first_stub_narrative_flow(self):
        # Hosted mode with an injected full-feedback stub exercises the
        # four-section provider narrative path with no network calls.
        self.service.close(wait=True)
        fixture_root = self.root / "grounded_fixture"
        fixture_root.mkdir()
        bank = make_grounded_bank()
        source, report, _ = replay_fixture(
            fixture_root, bank, make_grounded_taxonomy(bank), count=4)
        generator = FullStubGenerator()
        self.service = DemoService(
            root=self.root / "hosted_data", bank=bank,
            taxonomy=make_grounded_taxonomy(bank),
            diagnostics=fake_diagnostics(bank),
            replay_report=report, replay_source=source,
            provider_mode="hosted", generator=generator)
        self.addCleanup(self.service.close, wait=True)
        self.server.RequestHandlerClass.service = self.service
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelectorAll('.scenario-row').length === 4")

        # Scenario index 1 is all-incorrect, producing ready worked cards.
        known = {r["id"] for r in self.service.replays.list()["runs"]}
        b.js("document.querySelectorAll('.scenario-row')[1].click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.js("document.querySelector('#replay-selected').click()")
        b.wait("document.querySelector('dialog[open]')")
        b.screenshot("replay_dialog_desktop.png")
        b.viewport(390, 844)
        b.screenshot("replay_dialog_mobile.png")
        b.viewport(1440, 1100)
        b.js("document.querySelector('#replay-feedback-variant').value='worked_first';"
             "document.querySelector('#replay-feedback-variant').dispatchEvent(new Event('change'))")
        b.js("document.querySelector('#replay-provider-mode').value='hosted';"
             "document.querySelector('#replay-provider-mode').dispatchEvent(new Event('change'));")
        b.js("document.querySelector('#replay-hosted-consent').click()")
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")
        self.assertTrue(wait_for(
            lambda: any(r["id"] not in known
                        for r in self.service.replays.list()["runs"])),
            "new replay run was not admitted")
        run = next(r for r in self.service.replays.list()["runs"]
                   if r["id"] not in known)
        self.assertTrue(wait_for(
            lambda: self.service.replays.get(run["id"])["status"]
            == "complete"),
            f"replay run {run['id']} did not complete")
        run = self.service.replays.get(run["id"])
        self.assertEqual(run["feedback_variant"], "worked_first")
        self.assertGreaterEqual(generator.calls, 1)
        b.wait(f"document.querySelector('.run-record[data-run-id=\"{run['id']}\"] > summary')"
               ".textContent.includes('Complete')")

        case = run["cases"][0]
        view = self.service.replays.result(run["id"], case["scenario_id"])
        preview = view["feedback_delivery"]["preview"]
        texts = {s["kind"]: s["text"] for s in preview["sections"]}
        for kind, text in WORKED_SECTIONS.items():
            if kind == "review_focus":
                self.assertTrue(texts[kind].startswith(text))
            else:
                self.assertEqual(texts[kind], text)
        cards = preview["review_examples"]
        self.assertTrue(cards)
        self.assertTrue(all(c["status"] == "ready" for c in cards))
        self.assertTrue(all(c["steps"] for c in cards))
        before_steps = [c["steps"] for c in cards]
        before_correct = [c["correct_answer"] for c in cards]

        b.js(f"document.querySelector('#scenario-version').value={json.dumps(run['id'])};"
             "document.querySelector('#scenario-version').dispatchEvent(new Event('change'))")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.wait(f"document.querySelector('#scenario-ws-detail').textContent.includes({json.dumps(WORKED_SECTIONS['observed_strengths'])})")
        detail_text = b.js(
            "document.querySelector('#scenario-ws-detail').textContent")
        for text in WORKED_SECTIONS.values():
            self.assertIn(text.split("\n")[0][:60], detail_text)
        self.assertIn("shape-checked, not fact-verified", detail_text)
        for steps in before_steps:
            self.assertIn(steps[0][:40], detail_text)
        b.screenshot("worked_first_result_desktop.png")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.screenshot("worked_first_result_mobile.png")
        b.viewport(1440, 1100)

        # Application-owned facts stay immutable across release.
        sid = case["session_id"]
        released = self.service.release_feedback(
            sid, {"message_sha256":
                  view["feedback_delivery"]["preview_sha256"]})
        self.assertTrue(released["released"])
        after = self.service.teacher_session(sid)
        released_preview = after["feedback_delivery"]["released_feedback"]
        self.assertEqual([c["steps"] for c in
                          released_preview["review_examples"]],
                         before_steps)
        self.assertEqual([c["correct_answer"] for c in
                          released_preview["review_examples"]],
                         before_correct)
        self.assertEqual(released_preview["total"], preview["total"])
        self.assertEqual({s["kind"]: s["text"] for s in
                          released_preview["sections"]}, texts)

        # Question-first on the same answers stays summary-only.
        known = {r["id"] for r in self.service.replays.list()["runs"]}
        b.js("document.querySelectorAll('.scenario-row')[1].click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.js("document.querySelector('#replay-selected').click()")
        b.wait("document.querySelector('dialog[open]')")
        b.js("document.querySelector('#replay-provider-mode').value='hosted';"
             "document.querySelector('#replay-provider-mode').dispatchEvent(new Event('change'));")
        b.js("document.querySelector('#replay-hosted-consent').click()")
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")
        self.assertTrue(wait_for(
            lambda: any(r["id"] not in known
                        for r in self.service.replays.list()["runs"])),
            "new replay run was not admitted")
        run_q = next(r for r in self.service.replays.list()["runs"]
                     if r["id"] not in known)
        self.assertTrue(wait_for(
            lambda: self.service.replays.get(run_q["id"])["status"]
            == "complete"),
            f"replay run {run_q['id']} did not complete")
        view_q = self.service.replays.result(
            run_q["id"], case["scenario_id"])
        preview_q = view_q["feedback_delivery"]["preview"]
        self.assertEqual(preview_q["review_presentation_version"],
                         "question_first_worked_review_v1")
        qtexts = {s["kind"]: s["text"] for s in preview_q["sections"]}
        self.assertNotEqual(qtexts["observed_strengths"],
                            WORKED_SECTIONS["observed_strengths"])
        review_q = view_q["checkpoints"]["end"]["student_review"]
        self.assertEqual(
            review_q["message"]["section_sources"],
            {"assessment_summary": "provider",
             "observed_strengths": "application",
             "review_focus": "application",
             "next_steps": "application"})
        self.assertEqual([c["steps"] for c in
                          preview_q["review_examples"]], before_steps)
        self.assert_no_overflow()
        self.assertEqual(b.errors, [])

    def test_replay_progress_refresh_and_stage_notice(self):
        # Question-first: a provider-ready replay whose end diagnostics
        # are still running must show an explicit stage notice, keep
        # refreshing while the run record itself is unchanged, and
        # transition to complete automatically once diagnostics finish.
        # (Worked-first intentionally waits for diagnostics before the
        # provider call and is covered by the next test.)
        self.service.close(wait=True)
        fixture_root = self.root / "gated_fixture"
        fixture_root.mkdir()
        bank = make_grounded_bank()
        source, report, _ = replay_fixture(
            fixture_root, bank, make_grounded_taxonomy(bank), count=4)
        diag_gate = threading.Event()
        gen_gate = threading.Event()
        generator = GatedFullStubGenerator(gen_gate)
        self.service = DemoService(
            root=self.root / "gated_data", bank=bank,
            taxonomy=make_grounded_taxonomy(bank),
            diagnostics=GatedDiagnostics(bank, diag_gate),
            replay_report=report, replay_source=source,
            provider_mode="hosted", generator=generator)
        self.addCleanup(self.service.close, wait=True)
        self.server.RequestHandlerClass.service = self.service
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelectorAll('.scenario-row').length === 4")

        def start_replay(hosted=True):
            known = {r["id"] for r in self.service.replays.list()["runs"]}
            b.js("document.querySelectorAll('.scenario-row')[1].click()")
            b.wait("!document.querySelector('#scenario-ws-detail').hidden")
            b.js("document.querySelector('#replay-selected').click()")
            b.wait("document.querySelector('dialog[open]')")
            b.js("document.querySelector('#replay-feedback-variant').value='question_first';"
                 "document.querySelector('#replay-feedback-variant').dispatchEvent(new Event('change'))")
            if hosted:
                b.js("document.querySelector('#replay-provider-mode').value='hosted';"
                     "document.querySelector('#replay-provider-mode').dispatchEvent(new Event('change'));")
                b.js("document.querySelector('#replay-hosted-consent').click()")
            b.js("document.querySelector('#confirm-replay').click()")
            b.wait("!document.querySelector('dialog[open]')")
            self.assertTrue(wait_for(
                lambda: any(r["id"] not in known
                            for r in self.service.replays.list()["runs"])),
                "new replay run was not admitted")
            run = next(r for r in self.service.replays.list()["runs"]
                       if r["id"] not in known)
            b.wait(f"document.querySelector('.run-record[data-run-id=\"{run['id']}\"] .run-case')")
            return run

        def provider_status(run):
            sid = run["cases"][0]["session_id"]
            if not sid:
                return None
            try:
                return self.service._load_meta(sid)["provider_job"]["status"]
            except FileNotFoundError:
                return None

        detail = "document.querySelector('#scenario-ws-detail')"
        notice = ("document.querySelector("
                  "'#scenario-detail > p[role=\"status\"]')")

        expected_ready = (
            "Replay result · Question-first · Running. Original results "
            "remain unchanged. Feedback is ready; model diagnostics are "
            "still pending. This replay is not complete.")
        expected_pending = (
            "Replay result · Question-first · Running. Original results "
            "remain unchanged. Feedback is still being prepared; this "
            "replay is not complete.")

        def assert_notice_stable_during_held_poll(expected):
            # Hold one replay-case detail GET open during a natural poll:
            # the confirmed stage suffix must remain while the request is
            # in flight (rewriting the generic prefix there flickered),
            # and an identical payload must not rewrite the live region.
            b.js(
                "window.__origFetch = window.fetch;"
                "window.__noticeLog = [];"
                "window.__detailWaiting = 0;"
                "window.__detailDone = 0;"
                "window.__detailGate = null;"
                "window.__holdDetail = true;"
                "window.__noticeObs = new MutationObserver(() => {"
                "  window.__noticeLog.push(document.querySelector("
                "    '#scenario-detail > p[role=\"status\"]')"
                "    .textContent);});"
                "window.__noticeObs.observe(document.querySelector("
                "  '#scenario-detail > p[role=\"status\"]'),"
                "  {childList: true, characterData: true, subtree: true});"
                "window.fetch = function(url, opts) {"
                "  const isDetail = typeof url === 'string'"
                "    && url.indexOf('/api/teacher/replays/') === 0"
                "    && url.indexOf('/cases/') > 0;"
                "  if (!isDetail) return window.__origFetch(url, opts);"
                "  const begin = window.__holdDetail"
                "    ? new Promise((res) => { window.__detailWaiting++;"
                "        window.__detailGate = res; })"
                "    : Promise.resolve();"
                "  return begin"
                "    .then(() => window.__origFetch(url, opts))"
                "    .then((r) => { window.__detailDone++; return r; });"
                "};")
            try:
                try:
                    b.wait("window.__detailWaiting >= 1")
                    self.assertEqual(expected,
                                     b.js(f"{notice}.textContent"))
                    self.assertEqual([], b.js("window.__noticeLog"))
                finally:
                    # Release any held request before waiting or leaving.
                    b.js("window.__holdDetail = false;"
                         "if (window.__detailGate) {"
                         "  const release = window.__detailGate;"
                         "  window.__detailGate = null; release(); }")
                b.wait("window.__detailDone >= 1")
                self.assertEqual(expected, b.js(f"{notice}.textContent"))
                self.assertEqual([], b.js("window.__noticeLog"))
            finally:
                b.js("window.fetch = window.__origFetch;"
                     "if (window.__noticeObs) window.__noticeObs"
                     ".disconnect();")

        # --- Part A: provider finishes while end diagnostics blocked ---
        gen_gate.set()
        run_a = start_replay()
        self.assertTrue(wait_for(
            lambda: provider_status(run_a) == "ready"),
            "provider job did not reach ready")
        self.assertEqual(
            self.service.replays.get(run_a["id"])["status"], "running")
        b.js(f"document.querySelector('.run-record[data-run-id=\"{run_a['id']}\"] .run-case').click()")
        b.wait(f"{detail}.textContent.includes("
               f"{json.dumps(MULTI_SECTIONS['assessment_summary'])})")
        b.wait(f"{notice}.textContent.includes('Feedback is ready; model "
               "diagnostics are still pending. This replay is not complete.')")
        assert_notice_stable_during_held_poll(expected_ready)
        b.screenshot("qf_pending_diagnostics_desktop.png")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.screenshot("qf_pending_diagnostics_mobile.png")
        b.viewport(1440, 1100)

        # Diagnostics release: the already-selected view must reach the
        # complete state through normal polling, no re-selection.
        diag_gate.set()
        self.assertTrue(wait_for(
            lambda: self.service.replays.get(run_a["id"])["status"]
            == "complete"),
            f"replay run {run_a['id']} did not complete")
        b.wait(f"{notice}.textContent.includes('Replay is complete')")
        self.assertTrue(b.js(
            f"{detail}.textContent.includes("
            f"{json.dumps(MULTI_SECTIONS['assessment_summary'])})"))

        # --- Part B: delayed generator under unchanged run JSON ---
        gen_gate.clear()
        diag_gate.clear()
        run_b = start_replay()
        self.assertTrue(wait_for(lambda: provider_status(run_b) is not None))
        b.js(f"document.querySelector('.run-record[data-run-id=\"{run_b['id']}\"] .run-case').click()")
        b.wait(f"{notice}.textContent.includes('Feedback is still being "
               "prepared; this replay is not complete.')")
        # No final preview while pending; deterministic baseline text is
        # rendered locally, so the provider summary wording is not a
        # discriminating marker — the notice transition is. The midpoint
        # generation-details box carries its own shape-checked disclaimer
        # on backend timing, so it is excluded from the end-feedback
        # wording check.
        detail_text = b.js(
            f"(() => {{ const node = {detail}.cloneNode(true);"
            " node.querySelectorAll('.midpoint-provider-draft')"
            ".forEach((n) => n.remove()); return node.textContent; })()")
        self.assertFalse(b.js(
            f"Boolean({detail}.querySelector('.feedback-preview'))"))
        self.assertNotIn("shape-checked, not fact-verified", detail_text)
        assert_notice_stable_during_held_poll(expected_pending)

        gen_gate.set()
        # End diagnostics remain blocked, so the run record JSON cannot
        # change; the running-row refresh path must still pull the new
        # provider-ready detail automatically.
        self.assertTrue(wait_for(
            lambda: provider_status(run_b) == "ready"),
            "provider job did not reach ready")
        self.assertEqual(
            self.service.replays.get(run_b["id"])["status"], "running")
        # The notice derives from the re-fetched response, so this wait
        # proves the case detail refreshed under unchanged run JSON.
        b.wait(f"{notice}.textContent.includes('Feedback is ready; model "
               "diagnostics are still pending. This replay is not complete.')")
        b.wait(f"Boolean({detail}.querySelector('.feedback-preview'))")
        diag_gate.set()
        self.assertTrue(wait_for(
            lambda: self.service.replays.get(run_b["id"])["status"]
            == "complete"),
            f"replay run {run_b['id']} did not complete")
        b.wait(f"{notice}.textContent.includes('Replay is complete')")
        self.assert_no_overflow()
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.viewport(1440, 1100)

        # Context switch must not retain the previous stage suffix.
        b.js("document.querySelector('#scenario-version').value='';"
             "document.querySelector('#scenario-version')"
             ".dispatchEvent(new Event('change'))")
        b.wait(f"{notice}.textContent.includes('Retained original')")
        self.assertNotIn("Replay is complete",
                         b.js(f"{notice}.textContent"))

        # A same-context reload of the retained original that fails must
        # still recover through Retry: the confirmed notice stays
        # untouched while the request is in flight, the failure shows
        # the error + Retry, and a successful retry restores the
        # retained-original notice.
        b.js(
            "window.__origFetch2 = window.fetch;"
            "window.__failNextScenario = true;"
            "window.fetch = function(url, opts) {"
            "  const isScenario = typeof url === 'string'"
            "    && url.indexOf('/api/teacher/scenarios/') === 0;"
            "  if (isScenario && window.__failNextScenario) {"
            "    window.__failNextScenario = false;"
            "    return Promise.reject(new TypeError('injected failure'));"
            "  }"
            "  return window.__origFetch2(url, opts);"
            "};")
        try:
            b.js("document.querySelectorAll('.scenario-row')[1].click()")
            b.wait(f"{notice}.textContent.includes('Could not load')")
            b.wait(f"Boolean({notice}.querySelector('button'))")
            b.js(f"{notice}.querySelector('button').click()")
            b.wait(f"{notice}.textContent.includes('Retained original')")
            self.assertFalse(b.js(
                f"Boolean({notice}.querySelector('button'))"))
            self.assertNotIn("Could not load",
                             b.js(f"{notice}.textContent"))
        finally:
            b.js("window.fetch = window.__origFetch2;")

        b.js(f"document.querySelector('#scenario-version')"
             f".value={json.dumps(run_b['id'])};"
             "document.querySelector('#scenario-version')"
             ".dispatchEvent(new Event('change'))")
        b.wait(f"{notice}.textContent.includes('Replay is complete')")
        self.assertEqual(b.errors, [])

    def test_replay_worked_first_waits_for_diagnostics(self):
        # Worked-first authoring must not call the provider until the
        # midpoint/end diagnostics jobs are terminal; the notice shows
        # the dedicated waiting stage, then the full narrative appears.
        self.service.close(wait=True)
        fixture_root = self.root / "wf_wait_fixture"
        fixture_root.mkdir()
        bank = make_grounded_bank()
        source, report, _ = replay_fixture(
            fixture_root, bank, make_grounded_taxonomy(bank), count=4)
        diag_gate = threading.Event()
        generator = FullStubGenerator()
        self.service = DemoService(
            root=self.root / "wf_wait_data", bank=bank,
            taxonomy=make_grounded_taxonomy(bank),
            diagnostics=GatedDiagnostics(bank, diag_gate),
            replay_report=report, replay_source=source,
            provider_mode="hosted", generator=generator)
        self.addCleanup(self.service.close, wait=True)
        self.server.RequestHandlerClass.service = self.service
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelectorAll('.scenario-row').length === 4")

        b.js("document.querySelectorAll('.scenario-row')[1].click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.js("document.querySelector('#replay-selected').click()")
        b.wait("document.querySelector('dialog[open]')")
        b.js("document.querySelector('#replay-feedback-variant').value='worked_first';"
             "document.querySelector('#replay-feedback-variant').dispatchEvent(new Event('change'))")
        b.js("document.querySelector('#replay-provider-mode').value='hosted';"
             "document.querySelector('#replay-provider-mode').dispatchEvent(new Event('change'));")
        b.js("document.querySelector('#replay-hosted-consent').click()")
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")

        self.assertTrue(wait_for(
            lambda: self.service.replays.list()["runs"]),
            "new replay run was not admitted")
        run = self.service.replays.list()["runs"][0]

        def provider_status():
            current = next(
                (r for r in self.service.replays.list()["runs"]
                 if r["id"] == run["id"]), None)
            sid = (current or {}).get("cases", [{}])[0].get(
                "session_id")
            if not sid:
                return None
            try:
                return self.service._load_meta(sid)[
                    "provider_job"]["status"]
            except FileNotFoundError:
                return None

        # The provider must stay in the dedicated wait stage while the
        # end diagnostics job is still pending — no generator call.
        self.assertTrue(wait_for(
            lambda: provider_status() == "waiting_for_diagnostics"),
            "provider never entered waiting_for_diagnostics")
        self.assertEqual(generator.calls, 0)
        b.wait(f"document.querySelector('.run-record[data-run-id=\"{run['id']}\"] .run-case')")
        b.js(f"document.querySelector('.run-record[data-run-id=\"{run['id']}\"] .run-case').click()")
        detail = "document.querySelector('#scenario-ws-detail')"
        notice = ("document.querySelector("
                  "'#scenario-detail > p[role=\"status\"]')")
        b.wait(f"{notice}.textContent.includes('Waiting for model "
               "diagnostics before Aitta can draft the worked-first "
               "feedback. This replay is not complete.')")
        self.assertNotIn(WORKED_SECTIONS["observed_strengths"],
                         b.js(f"{detail}.textContent"))
        b.screenshot("worked_first_waiting_diagnostics_desktop.png")

        # Release diagnostics: provider generates, narrative appears,
        # and the run completes through normal polling.
        diag_gate.set()
        self.assertTrue(wait_for(
            lambda: provider_status() == "ready"),
            "provider job did not reach ready")
        self.assertTrue(wait_for(
            lambda: self.service.replays.get(run["id"])["status"]
            == "complete"),
            f"replay run {run['id']} did not complete")
        self.assertEqual(generator.calls, 1)
        self.assertEqual(
            generator.payloads[0]["worked_first_context"]["version"],
            "worked_first_assessment_research_context_v1")
        b.wait(f"{detail}.textContent.includes("
               f"{json.dumps(WORKED_SECTIONS['observed_strengths'])})")
        b.wait(f"{notice}.textContent.includes('Replay is complete')")
        detail_text = b.js(f"{detail}.textContent")
        self.assertIn("shape-checked, not fact-verified", detail_text)
        self.assertIn("Aitta input", detail_text)
        b.screenshot("worked_first_wait_result_desktop.png")
        self.assert_no_overflow()
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.viewport(1440, 1100)
        self.assertEqual(b.errors, [])

    def test_skill_map_evidence_and_task_roles(self):
        # Aligned rules-mode service; a warm-bank session binds the real
        # assessed-task descriptions from the v3 content context.
        self.service.close(wait=True)
        bank = make_bank()
        self.service = DemoService(
            root=self.root / "aligned_data", bank=bank,
            taxonomy=make_taxonomy(bank), diagnostics=fake_diagnostics(bank),
            replay_report=self.report, replay_source=self.source)
        self.addCleanup(self.service.close, wait=True)
        self.server.RequestHandlerClass.service = self.service
        warm_bank, _, _ = load_research_bank("warm")
        created = self.service.create_session(bank_mode="warm")
        partial_sid = created["session_id"]
        for question in warm_bank["questions"][:20]:
            self.service.submit_response(
                partial_sid, created["student_token"],
                {"question_id": question["question_id"],
                 "selected_index": question["answer_index"]})
        result = self.service.simulate("alternating", 5, bank_mode="warm")
        sid = result["session_id"]
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        view = self.service.teacher_session(sid)
        end = view["checkpoints"]["end"]
        review = end["student_review"] or end["baseline_student"]
        self.assertIsNotNone(review)
        preview = view["feedback_delivery"]["preview"]
        examples = preview.get("review_examples") or []
        self.assertTrue(examples)
        example = examples[0]
        rows = {r["question_id"]: r for r in view["question_responses"]}
        focus_subs = set()
        for cand in review.get("selected_candidates") or []:
            for inner in cand.get("review_candidates") or [cand]:
                focus = (inner or {}).get("focus") or {}
                if focus.get("subtopic_id"):
                    focus_subs.add(focus["subtopic_id"])
        self.assertTrue(focus_subs)
        same_sub = next(r for r in view["question_responses"]
                        if r["subtopic_id"] == example["subtopic_id"]
                        and r["question_id"] != example["question_id"])
        other = next(r for r in view["question_responses"]
                     if r["subtopic_id"] not in focus_subs)
        ex_ctx = next(e for e in view["content_context"]["exercises"]
                      if e["question_id"] == example["question_id"])
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('#session-list button')")
        b.js("document.querySelector('#session-search').value='alternating'; document.querySelector('#session-search').dispatchEvent(new Event('input'))")
        b.js("document.querySelector('#session-list button').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=graph]').click()")
        pane = "document.querySelector('#tab-graph')"
        b.wait(f"{pane}.textContent.includes('Assessed tasks (40)')")
        graph = f"{pane}.textContent"
        self.assertTrue(b.js(f"{graph}.includes('Practice drafts (24)')"))
        self.assertTrue(b.js(f"{graph}.includes('not prerequisites')"))
        self.assertTrue(b.js(f"{graph}.includes('pending formal educator review')"))
        self.assertTrue(b.js(f"{graph}.includes('observed correct')"))
        for topic in end["graph"]["topics"]:
            self.assertTrue(b.js(
                f"{graph}.includes(englishSkillName({json.dumps(topic['skill_name'])})"
                f" + ' — ' + {topic['correct']} + '/' + {topic['out_of']}"
                " + ' observed correct')"))
        for leaf in [s for t in end["graph"]["topics"] for s in t["subtopics"]
                     if s["subtopic_id"] in focus_subs]:
            line = (f"{leaf['subtopic_name']}: {leaf['correct']} correct, "
                    f"{leaf['incorrect']} incorrect of {leaf['out_of']}"
                    " · selected review focus")
            self.assertTrue(b.js(f"{graph}.includes({json.dumps(line)})"))

        def card(qid):
            return ("document.querySelector('#tab-graph "
                    f"details[data-disclosure-key=\"skill-task:{qid}\"]')")

        ex_sel = card(example["question_id"])
        b.wait(f"Boolean({ex_sel})")
        self.assertTrue(b.js(
            f"{ex_sel}.textContent.includes('Selected worked-review example')"))
        ex_row = rows[example["question_id"]]
        self.assertTrue(b.js(
            f"{ex_sel}.textContent.includes(englishOptionText("
            f"{json.dumps(ex_row['options'][ex_row['selected_index']])}))"))
        self.assertTrue(b.js(
            f"{ex_sel}.textContent.includes(englishOptionText("
            f"{json.dumps(ex_row['options'][ex_row['answer_index']])}))"))
        self.assertTrue(b.js(
            f"{ex_sel}.textContent.includes('Task requirement')"))
        self.assertTrue(b.js(
            f"{ex_sel}.textContent.includes("
            f"{json.dumps(ex_ctx['mathematical_task'])})"))
        self.assertTrue(b.js(
            f"{ex_sel}.querySelector('summary').textContent"
            f".includes(englishSkillName({json.dumps(ex_ctx['skill_name'])}))"))
        self.assertTrue(b.js(
            f"{card(same_sub['question_id'])}.textContent.includes("
            "'Selected review focus — a different task in this subtopic')"))
        other_sel = card(other["question_id"])
        self.assertTrue(b.js(
            f"{other_sel}.textContent.includes("
            "'Not selected as a worked-review example')"))
        self.assertFalse(b.js(
            f"{other_sel}.textContent.includes('Selected review focus')"))

        # Collapsed summaries carry the selection badges at a glance.
        self.assertTrue(b.js(
            f"{ex_sel}.querySelector('summary').textContent"
            ".includes('Selected worked review')"))
        focus_sel = card(same_sub["question_id"])
        self.assertTrue(b.js(
            f"{focus_sel}.querySelector('summary').textContent"
            ".includes('Review focus')"))
        self.assertFalse(b.js(
            f"{focus_sel}.querySelector('summary').textContent"
            ".includes('Selected worked review')"))
        for fragment in ("Selected worked review", "Review focus"):
            self.assertFalse(b.js(
                f"{other_sel}.querySelector('summary').textContent"
                f".includes({json.dumps(fragment)})"))

        b.js("document.querySelector('#tab-graph "
             "details[data-disclosure-key=skill-map-assessed]').open = true")
        b.js(f"{ex_sel}.open = true")
        b.js("document.querySelector('#tab-graph "
             "details[data-disclosure-key=skill-map-evidence-use]').open = true")
        time.sleep(2.4)
        self.assertTrue(b.js(f"{ex_sel}.open"))
        self.assertTrue(b.js(
            "document.querySelector('#tab-graph "
            "details[data-disclosure-key=skill-map-evidence-use]').open"))
        b.js(f"{ex_sel}.scrollIntoView({{block: 'center'}})")
        self.assert_no_overflow()
        b.screenshot("skill-map-live-desktop.png")
        b.viewport(390, 844)
        b.js(f"{ex_sel}.scrollIntoView({{block: 'center'}})")
        self.assert_no_overflow()
        b.screenshot("skill-map-live-mobile.png")
        b.viewport(1440, 1100)

        # Disclosure scopes are session-specific: the same task key is
        # closed again in the other session.
        b.js("document.querySelector('#session-search').value='manual'; document.querySelector('#session-search').dispatchEvent(new Event('input'))")
        b.js("document.querySelector('#session-list button').click()")
        b.wait("document.querySelector('#ws-title').textContent.includes('Manual')")
        b.js("document.querySelector('#ws-tabs [data-tab=graph]').click()")
        b.wait(f"{pane}.textContent.includes('Not answered')")
        self.assertTrue(b.js(
            f"{graph}.includes('Feedback selection pending')"))
        unanswered = warm_bank["questions"][30]
        un_sel = card(unanswered["question_id"])
        self.assertTrue(b.js(
            f"{un_sel}.querySelector('summary').textContent"
            ".includes('Not answered')"))
        self.assertFalse(b.js(f"{ex_sel}.open"))
        answered = card(warm_bank["questions"][0]["question_id"])
        self.assertTrue(b.js(
            f"{answered}.querySelector('summary').textContent"
            ".includes('Correct')"))
        self.assertEqual(b.errors, [])

    def test_skill_map_retained_original_historical_mapping(self):
        # A retained original whose recorded taxonomy no longer matches
        # the current one keeps an explicit missing-mapping label; the
        # current content label is never substituted as history.
        report = json.loads(self.report.read_text(encoding="utf-8"))
        report["source"]["taxonomy_sha256"] = "0" * 64
        stale_report = self.root / "report_missing_mapping.json"
        stale_report.write_text(json.dumps(report), encoding="utf-8")
        self.service.close(wait=True)
        bank = make_bank()
        self.service = DemoService(
            root=self.root / "mapping_data", bank=bank,
            taxonomy=make_taxonomy(bank), diagnostics=fake_diagnostics(bank),
            replay_report=stale_report, replay_source=self.source)
        self.addCleanup(self.service.close, wait=True)
        self.server.RequestHandlerClass.service = self.service
        cid = self.service.research.library()["scenarios"][0]["id"]
        view = self.service.scenario_detail(cid)["detail"]
        self.assertTrue(view["question_responses"])
        self.assertTrue(all(r["subtopic_id"] is None
                            for r in view["question_responses"]))
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('.scenario-row')")
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=graph]').click()")
        pane = "document.querySelector('#scenario-tab-graph')"
        b.wait(f"{pane}.textContent.includes('Assessed tasks (40)')")
        graph = f"{pane}.textContent"
        self.assertTrue(b.js(f"{graph}.includes('Practice drafts (24)')"))
        self.assertTrue(b.js(
            f"{graph}.includes('Unavailable for this record')"))
        self.assertFalse(b.js(f"{graph}.includes('Subtopic sA')"))
        qid = view["question_responses"][0]["question_id"]
        sel = ("document.querySelector('#scenario-tab-graph "
               f"details[data-disclosure-key=\"skill-task:{qid}\"]')")
        b.js(f"{sel}.open = true")
        self.assertTrue(b.js(
            f"{sel}.textContent.includes("
            "'Historical subtopic mapping is unavailable')"))
        self.assertTrue(b.js(
            f"{sel}.textContent.includes("
            "'No descriptive context is bound to this task')"))
        self.assertTrue(b.js(
            f"{sel}.textContent.includes("
            "'Not selected as a worked-review example')"))
        b.js("document.querySelector('#scenario-tab-graph "
             "details[data-disclosure-key=skill-map-assessed]').open = true")
        b.js(f"{sel}.scrollIntoView({{block: 'center'}})")
        self.assert_no_overflow()
        b.screenshot("skill-map-retained-desktop.png")
        b.viewport(390, 844)
        b.js(f"{sel}.scrollIntoView({{block: 'center'}})")
        self.assert_no_overflow()
        b.screenshot("skill-map-retained-mobile.png")
        b.viewport(1440, 1100)
        self.assertEqual(b.errors, [])

    def test_skill_map_feedback_role_edge_cases(self):
        # Aligned rules-mode service with a warm live session; the view
        # is patched per variant to cover historical null mapping and
        # preview edge cases against the same rendered detail.
        self.service.close(wait=True)
        bank = make_bank()
        self.service = DemoService(
            root=self.root / "edge_data", bank=bank,
            taxonomy=make_taxonomy(bank), diagnostics=fake_diagnostics(bank),
            replay_report=self.report, replay_source=self.source)
        self.addCleanup(self.service.close, wait=True)
        self.server.RequestHandlerClass.service = self.service
        result = self.service.simulate("alternating", 7, bank_mode="warm")
        sid = result["session_id"]
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        view = self.service.teacher_session(sid)
        review = (view["checkpoints"]["end"]["student_review"]
                  or view["checkpoints"]["end"]["baseline_student"])
        examples = view["feedback_delivery"]["preview"].get(
            "review_examples") or []
        self.assertTrue(examples)
        example_qid = examples[0]["question_id"]
        focus_subs = set()
        for cand in review.get("selected_candidates") or []:
            for inner in cand.get("review_candidates") or [cand]:
                focus = (inner or {}).get("focus") or {}
                if focus.get("subtopic_id"):
                    focus_subs.add(focus["subtopic_id"])
        self.assertTrue(focus_subs)
        focus_task = next(r for r in view["question_responses"]
                          if r["subtopic_id"] in focus_subs
                          and r["question_id"] != example_qid)
        original = self.service.teacher_session
        self.addCleanup(
            setattr, self.service, "teacher_session", original)
        b = self.browser

        def open_graph():
            self.navigate("/teacher")
            b.wait("document.querySelector('#unlock-panel')")
            b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
            b.wait("!document.querySelector('#teacher-app').hidden")
            b.wait("document.querySelector('#session-list button')")
            b.js("document.querySelector('#session-list button').click()")
            b.wait("!document.querySelector('#ws-detail').hidden")
            b.js("document.querySelector('#ws-tabs [data-tab=graph]').click()")
            b.wait("document.querySelector('#tab-graph').textContent"
                   ".includes('Assessed tasks (40)')")

        def card(qid):
            return ("document.querySelector('#tab-graph "
                    f"details[data-disclosure-key=\"skill-task:{qid}\"]')")

        ft = card(focus_task["question_id"])
        ex_sel = card(example_qid)

        # A recorded answer with a null historical subtopic id must not
        # be relabeled by the current content concept, even when that
        # concept is the selected focus.
        def missing_mapping(sid_arg):
            patched = original(sid_arg)
            for row in patched["question_responses"]:
                row["subtopic_id"] = None
            return patched
        self.service.teacher_session = missing_mapping
        open_graph()
        self.assertTrue(b.js(
            f"{ft}.textContent.includes('Unavailable for this record')"))
        self.assertTrue(b.js(
            f"{ft}.textContent.includes("
            "'Task requirement (current descriptive context)')"))
        self.assertFalse(b.js(
            f"{ft}.textContent.includes('Selected review focus')"))
        self.assertTrue(b.js(
            f"{ft}.textContent.includes("
            "'Not selected as a worked-review example')"))
        self.assertTrue(b.js(
            f"{ex_sel}.textContent.includes("
            "'Selected worked-review example')"))
        self.assertFalse(b.js(
            f"{ft}.querySelector('summary').textContent"
            ".includes('Review focus')"))
        self.assertTrue(b.js(
            f"{ex_sel}.querySelector('summary').textContent"
            ".includes('Selected worked review')"))

        # An empty current preview list is honored; the retained review
        # example list is not resurrected.
        def empty_examples(sid_arg):
            patched = original(sid_arg)
            patched["feedback_delivery"]["preview"]["review_examples"] = []
            return patched
        self.service.teacher_session = empty_examples
        open_graph()
        for sel in (ft, ex_sel):
            self.assertTrue(b.js(
                f"{sel}.textContent.includes('Selected review focus; no "
                "structured worked-review example is available for this "
                "subtopic')"))
            self.assertFalse(b.js(
                f"{sel}.textContent.includes("
                "'Selected worked-review example')"))
            self.assertTrue(b.js(
                f"{sel}.querySelector('summary').textContent"
                ".includes('Review focus')"))
            self.assertFalse(b.js(
                f"{sel}.querySelector('summary').textContent"
                ".includes('Selected worked review')"))

        # A teacher-edited document never implies an included example.
        def edited_document(sid_arg):
            patched = original(sid_arg)
            patched["feedback_delivery"]["preview"]["document_text"] = (
                "Teacher-edited document wording.")
            return patched
        self.service.teacher_session = edited_document
        open_graph()
        self.assertTrue(b.js(
            f"{ft}.textContent.includes('Selected review focus; "
            "structured example linkage into the teacher-edited "
            "document is unavailable')"))
        self.assertFalse(b.js(
            f"{ex_sel}.textContent.includes("
            "'Selected worked-review example')"))
        self.assertFalse(b.js(
            f"{ex_sel}.querySelector('summary').textContent"
            ".includes('Selected worked review')"))
        self.assertTrue(b.js(
            f"{ex_sel}.querySelector('summary').textContent"
            ".includes('Review focus')"))
        self.assertTrue(b.js(
            f"{ex_sel}.textContent.includes('teacher-edited document "
            "is unavailable')"))

        # An example whose worked solution failed stays honest.
        def unavailable_solution(sid_arg):
            patched = original(sid_arg)
            patched["feedback_delivery"]["preview"]["review_examples"][0][
                "status"] = "unavailable"
            return patched
        self.service.teacher_session = unavailable_solution
        open_graph()
        self.assertTrue(b.js(
            f"{ex_sel}.textContent.includes('Selected worked-review "
            "example — worked solution unavailable')"))
        self.assertEqual(b.errors, [])

    def test_skill_map_practice_selection_badges(self):
        # Practice badges come only from recorded selected_question_id
        # identity on end.practice_contexts (or the legacy flat
        # practice_context); the view is patched per variant.
        self.service.close(wait=True)
        bank = make_bank()
        self.service = DemoService(
            root=self.root / "practice_data", bank=bank,
            taxonomy=make_taxonomy(bank), diagnostics=fake_diagnostics(bank),
            replay_report=self.report, replay_source=self.source)
        self.addCleanup(self.service.close, wait=True)
        self.server.RequestHandlerClass.service = self.service
        result = self.service.simulate("alternating", 9, bank_mode="warm")
        sid = result["session_id"]
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        view = self.service.teacher_session(sid)
        practices = [e for e in view["content_context"]["exercises"]
                     if e["role"] == "practice"]
        self.assertEqual(len(practices), 24)
        chosen = practices[0]
        same_topic = next(e for e in practices
                          if e["skill_id"] == chosen["skill_id"]
                          and e["question_id"] != chosen["question_id"])
        other_topic = next(e for e in practices
                           if e["skill_id"] != chosen["skill_id"])
        original = self.service.teacher_session
        self.addCleanup(
            setattr, self.service, "teacher_session", original)
        b = self.browser

        def open_graph():
            self.navigate("/teacher")
            b.wait("document.querySelector('#unlock-panel')")
            b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
            b.wait("!document.querySelector('#teacher-app').hidden")
            b.wait("document.querySelector('#session-list button')")
            b.js("document.querySelector('#session-list button').click()")
            b.wait("!document.querySelector('#ws-detail').hidden")
            b.js("document.querySelector('#ws-tabs [data-tab=graph]').click()")
            b.wait("document.querySelector('#tab-graph').textContent"
                   ".includes('Practice drafts (24)')")

        def pcard(qid):
            return ("document.querySelector('#tab-graph "
                    f"details[data-disclosure-key=\"skill-practice:{qid}\"]')")

        ch = pcard(chosen["question_id"])
        st = pcard(same_topic["question_id"])
        ot = pcard(other_topic["question_id"])

        def summary_text(sel):
            return f"{sel}.querySelector('summary').textContent"

        # Exact recorded selection: only the matching row is marked.
        def selected(mutate_doc=False):
            def patched(sid_arg):
                v = original(sid_arg)
                v["checkpoints"]["end"]["practice_contexts"] = [
                    {"candidate_id": "c1", "skill_id": chosen["skill_id"],
                     "context": {"status": "selected",
                                 "selected_question_id":
                                     chosen["question_id"],
                                 "pool_sha256": "p"}},
                    {"candidate_id": "c2", "skill_id": same_topic["skill_id"],
                     "context": {"status": "abstained",
                                 "selected_question_id": None,
                                 "pool_sha256": None}}]
                if mutate_doc:
                    v["feedback_delivery"]["preview"]["document_text"] = (
                        "Teacher-edited wording.")
                return v
            return patched
        self.service.teacher_session = selected()
        open_graph()
        b.js("document.querySelector('#tab-graph "
             "details[data-disclosure-key=skill-map-practice]').open = true")
        self.assertTrue(b.js(
            f"{summary_text(ch)}.includes('Selected practice')"))
        self.assertTrue(b.js(
            f"{ch}.textContent.includes('Selected practice')"))
        self.assertTrue(b.js(
            f"{summary_text(st)}.includes('Not selected')"))
        self.assertTrue(b.js(f"{st}.textContent.includes('Not selected')"))
        self.assertFalse(b.js(
            f"{summary_text(st)}.includes('Selected practice')"))
        self.assertFalse(b.js(
            f"{summary_text(ot)}.includes('Selected practice')"))
        b.js(f"{ch}.scrollIntoView({{block: 'center'}})")
        self.assert_no_overflow()
        b.screenshot("skill-map-practice-selected-desktop.png")
        b.viewport(390, 844)
        b.js(f"{ch}.scrollIntoView({{block: 'center'}})")
        self.assert_no_overflow()
        self.assertTrue(b.js(
            f"{summary_text(ch)}.includes('Selected practice')"))
        b.screenshot("skill-map-practice-selected-mobile.png")
        b.viewport(1440, 1100)

        # A teacher-edited document keeps the recorded badge but the
        # section admits linkage cannot be confirmed.
        self.service.teacher_session = selected(mutate_doc=True)
        open_graph()
        self.assertTrue(b.js(
            f"{summary_text(ch)}.includes('Selected practice')"))
        self.assertTrue(b.js(
            "document.querySelector('#tab-graph').textContent"
            ".includes('cannot be confirmed')"))
        self.assertFalse(b.js(
            "document.querySelector('#tab-graph').textContent"
            ".includes('approved')"))

        # An empty multi slot list is a recorded state: the stale flat
        # context is not resurrected.
        def empty_slots(sid_arg):
            v = original(sid_arg)
            v["checkpoints"]["end"]["practice_contexts"] = []
            v["checkpoints"]["end"]["practice_context"] = {
                "status": "selected",
                "selected_question_id": chosen["question_id"],
                "pool_sha256": "stale"}
            return v
        self.service.teacher_session = empty_slots
        open_graph()
        self.assertTrue(b.js(
            f"{summary_text(ch)}.includes('Selection not recorded')"))
        self.assertFalse(b.js(
            f"{summary_text(ch)}.includes('Selected practice')"))

        # Legacy flat context still works when the multi-slot property
        # is absent entirely.
        def legacy_flat(sid_arg):
            v = original(sid_arg)
            v["checkpoints"]["end"].pop("practice_contexts", None)
            v["checkpoints"]["end"]["practice_context"] = {
                "status": "selected",
                "selected_question_id": chosen["question_id"],
                "pool_sha256": "p"}
            return v
        self.service.teacher_session = legacy_flat
        open_graph()
        self.assertTrue(b.js(
            f"{summary_text(ch)}.includes('Selected practice')"))
        self.assertTrue(b.js(
            f"{summary_text(st)}.includes('Not selected')"))

        # A selected slot without a recorded question id cannot mark a
        # row — missing identity, not non-selection.
        def selected_no_id(sid_arg):
            v = original(sid_arg)
            v["checkpoints"]["end"]["practice_contexts"] = [
                {"candidate_id": "c1", "skill_id": chosen["skill_id"],
                 "context": {"status": "selected",
                             "selected_question_id": None,
                             "pool_sha256": "p"}}]
            return v
        self.service.teacher_session = selected_no_id
        open_graph()
        self.assertTrue(b.js(
            f"{summary_text(ch)}.includes('Selection not recorded')"))
        self.assertFalse(b.js(
            f"{summary_text(ch)}.includes('Selected practice')"))
        self.assertFalse(b.js(
            f"{summary_text(ch)}.includes('Not selected')"))

        # No recorded identity at all.
        def unrecorded(sid_arg):
            v = original(sid_arg)
            v["checkpoints"]["end"].pop("practice_contexts", None)
            v["checkpoints"]["end"].pop("practice_context", None)
            return v
        self.service.teacher_session = unrecorded
        open_graph()
        self.assertTrue(b.js(
            f"{summary_text(ch)}.includes('Selection not recorded')"))
        self.assertFalse(b.js(
            f"{summary_text(ch)}.includes('Not selected')"))

        # Terminal non-selection states stay honest.
        def terminal(status):
            def patched(sid_arg):
                v = original(sid_arg)
                v["checkpoints"]["end"]["practice_contexts"] = [
                    {"candidate_id": "c1", "skill_id": chosen["skill_id"],
                     "context": {"status": status,
                                 "selected_question_id": None,
                                 "pool_sha256": None}}]
                return v
            return patched
        for status, label in (("disabled", "Selection disabled"),
                              ("unavailable", "Selection unavailable"),
                              ("abstained", "Not selected")):
            self.service.teacher_session = terminal(status)
            open_graph()
            self.assertTrue(b.js(
                f"{summary_text(ch)}.includes({json.dumps(label)})"))
            self.assertFalse(b.js(
                f"{summary_text(ch)}.includes('Selected practice')"))

        # A provider still waiting on practice is pending, not recorded.
        def pending(sid_arg):
            v = original(sid_arg)
            v["provider_job"]["status"] = "waiting_for_practice"
            return v
        self.service.teacher_session = pending
        open_graph()
        self.assertTrue(b.js(
            f"{summary_text(ch)}.includes('Selection pending')"))
        self.assertEqual(b.errors, [])


if __name__ == "__main__":
    unittest.main()
