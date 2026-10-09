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
from tests.helpers import make_bank, make_taxonomy
from tests.test_demo_service import fake_diagnostics, wait_for
from tests.test_scenario_replays import replay_fixture
from warm_scenarios import write_capture


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

    def test_warm_54_library_and_selected_replay(self):
        bank = load_research_bank("warm")[0]
        with contextlib.redirect_stdout(io.StringIO()):
            source, report = write_capture(self.root / "warm_capture")
        self.service.close(wait=True)
        self.service = DemoService(
            root=self.root / "warm_data", bank=make_bank(),
            taxonomy=make_taxonomy(make_bank()), diagnostics=fake_diagnostics(bank),
            replay_report=report, replay_source=source, replay_bank_mode="warm")
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
        b.wait(f"{pane}.textContent.includes('Applied feedback plan')")
        kt_dd = (f"[...{pane}.querySelectorAll('.feedback-plan dt')]"
                 ".find((n) => n.textContent === 'KT used')")
        self.assertEqual(b.js(f"{kt_dd}.nextElementSibling.textContent"), "No")
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
        b.wait(f"{pane}.textContent.includes('Multiple incorrect answers')")
        self.assertTrue(b.js(f"{pane}.textContent.includes('Multiple assessed items')"))
        self.assertTrue(b.js(f"{pane}.textContent.includes('Percentages \\u2014 0 correct, 10 incorrect of 10')"))
        self.assertTrue(b.js(f"{pane}.textContent.includes('1 of 5')"))
        self.assertTrue(b.js(f"{pane}.textContent.includes('Tied candidates')"))
        self.assertTrue(b.js(f"{pane}.textContent.includes('review_percentage_amount')"))
        self.assertTrue(b.js(f"{pane}.querySelector('.decision-table')"
                             ".textContent.includes('Multiple incorrect answers')"))
        b.viewport(390, 844)
        self.assert_no_overflow()
        self.assertEqual(b.errors, [])

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
        # The counter can reach its total before the worker closes the batch.
        # A subsequent replay must wait for the terminal status, not just counts.
        b.wait("document.querySelector('.run-history > summary').textContent.includes('Latest: Complete')")
        b.js("document.querySelector('#replay-all').click()")
        b.wait("document.querySelector('dialog[open]')")
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")
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
        self.assertEqual(b.js("document.querySelectorAll('#scenario-ws-tabs .tab').length"), 6)
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=feedback]').click()")
        self.assertEqual(b.js("document.querySelectorAll('#scenario-tab-feedback .compare .col').length"), 4)
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
        self.assertTrue(b.js("document.querySelector('#end-summary').textContent.includes('your teacher is reviewing your feedback')"))
        self.assertFalse(b.js("document.querySelector('#end-summary').textContent.includes('Observed result')"))
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
        b.wait("document.querySelectorAll('#scenario-tab-feedback .compare .col').length === 4")
        self.assertFalse(b.js("Boolean(document.querySelector('#scenario-tab-feedback .release-card'))"))
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
            f"{release}.textContent.includes('scores and skill counts are fixed')"))
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
        b.wait(f"{release}.textContent.includes('Teacher-edited draft')")
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


if __name__ == "__main__":
    unittest.main()
