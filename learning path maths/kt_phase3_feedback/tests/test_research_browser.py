import base64
import contextlib
import copy
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
from unittest import mock

from demo_api import make_server
from demo_service import DemoService
from research_runtime import load_research_bank
from research_workspace import DEFAULT_REPORT, ResearchWorkspace
from tests.browser_runtime import (
    cleanup_browser_directory, read_devtools_port)
from tests.helpers import make_bank, make_taxonomy, responses
from tests.test_assessment_worked_review import (
    fixture_reviews, make_grounded_bank, make_grounded_taxonomy)
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
        self.extension_errors = []
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
                details = result["params"]["exceptionDetails"]
                # Browser extensions can inject scripts despite
                # --disable-extensions. Retain their errors separately
                # from application exceptions.
                if details.get("url", "").startswith(
                        "chrome-extension://"):
                    self.extension_errors.append(details)
                else:
                    self.errors.append(details)
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


class BrowserExceptionTests(unittest.TestCase):
    """Unit coverage for the Browser console collector: extension
    exceptions are retained separately; page errors are never lost.
    No real browser or network is used."""

    def _browser(self, messages):
        browser = Browser.__new__(Browser)
        browser.socket = mock.Mock()
        browser.socket.recv.side_effect = [
            json.dumps(message) for message in messages]
        browser.sequence = 0
        browser.errors = []
        browser.extension_errors = []
        return browser

    def test_extension_errors_kept_apart_from_page_errors(self):
        extension = {"exceptionId": 1, "text": "Uncaught",
                     "url": "chrome-extension://abc/inject.js"}
        local = {"exceptionId": 2, "text": "Uncaught",
                 "url": "http://127.0.0.1:1/app.js"}
        missing = {"exceptionId": 3, "text": "Uncaught"}
        csp = {"text": "Refused: Content Security Policy violation",
               "level": "error"}
        browser = self._browser([
            {"method": "Runtime.exceptionThrown",
             "params": {"exceptionDetails": extension}},
            {"method": "Runtime.exceptionThrown",
             "params": {"exceptionDetails": local}},
            {"method": "Runtime.exceptionThrown",
             "params": {"exceptionDetails": missing}},
            {"method": "Log.entryAdded", "params": {"entry": csp}},
            {"id": 1, "result": {}}])
        browser.command("Runtime.enable")
        self.assertEqual(browser.errors, [local, missing, csp])
        self.assertEqual(browser.extension_errors, [extension])

    def test_evaluate_exception_still_raises(self):
        detail = {"text": "Uncaught ReferenceError",
                  "url": "http://127.0.0.1:1/app.js"}
        browser = self._browser([
            {"id": 1, "result": {"exceptionDetails": detail}}])
        with self.assertRaises(AssertionError):
            browser.js("boom()")
        self.assertEqual(browser.errors, [])
        self.assertEqual(browser.extension_errors, [])


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1",
                     "opt-in installed Edge + websocket-client browser test")
class ResearchBrowserTests(unittest.TestCase):
    def setUp(self):
        edge = Path(os.environ.get("EDGE_BINARY", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"))
        if not edge.is_file():
            self.skipTest("Microsoft Edge is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_browser_directory, self.tmp)
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
            "--headless=new", "--disable-gpu", "--no-first-run",
            "--disable-extensions", "--disable-component-update",
            "--disable-background-networking",
            "--no-default-browser-check",
            "--window-size=1440,1100", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_browser)
        port = read_devtools_port(profile)
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
        # only inside raw candidate JSON. The overview provenance carries
        # replay metadata only on a replayed version, so it confirms the
        # detail view has switched to the fresh replay result.
        pane = "document.querySelector('#scenario-tab-feedback')"
        overview = "document.querySelector('#scenario-tab-overview')"
        b.wait(f"{overview}.textContent.includes('\"replay\": {{')")
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
        b.wait(f"{overview}.textContent.includes('\"replay\": {{')")
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

    def test_scenario_sidebar_sticky_desktop(self):
        bank = load_research_bank("warm")[0]
        with contextlib.redirect_stdout(io.StringIO()):
            source, report = write_capture(self.root / "warm_capture")
        self.service.close(wait=True)
        self.service = DemoService(
            root=self.root / "warm_data", bank=make_bank(),
            taxonomy=make_taxonomy(make_bank()),
            diagnostics=fake_diagnostics(bank),
            replay_report=report, replay_source=source,
            replay_bank_mode="warm", review_mode="legacy_single")
        self.addCleanup(self.service.close, wait=True)
        self.server.RequestHandlerClass.service = self.service
        b = self.browser
        b.viewport(1440, 900)
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelectorAll('.scenario-row').length === 54")
        sidebar = "document.querySelector('.scenario-browser')"
        slist = "document.querySelector('.scenario-list')"
        # Long list overflows its own container inside a viewport-capped
        # sticky sidebar.
        self.assertTrue(b.js(f"{slist}.scrollHeight > {slist}.clientHeight"))
        self.assertEqual(b.js(
            f"getComputedStyle({sidebar}).position"), "sticky")
        self.assertLessEqual(b.js(
            f"{sidebar}.getBoundingClientRect().height"), 868)
        # Select a scenario and open its feedback tab; the sidebar stays
        # pinned while the long detail pane scrolls underneath.
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.js("document.querySelector('#scenario-ws-tabs "
             "[data-tab=feedback]').click()")
        pane = "document.querySelector('#scenario-tab-feedback')"
        b.wait(f"{pane}.childElementCount > 0")
        b.js("window.scrollTo(0, 900)")
        self.assertGreater(b.js("window.scrollY"), 0)
        rect = b.js(f"({{t: {sidebar}.getBoundingClientRect().top,"
                    f" b: {sidebar}.getBoundingClientRect().bottom}})")
        self.assertAlmostEqual(rect["t"], 16, delta=1)
        self.assertLessEqual(rect["b"], 884)
        self.assertLessEqual(b.js(
            f"{slist}.getBoundingClientRect().bottom"), 884)
        # Scrolling the list itself does not move the document.
        scroll_y = b.js("window.scrollY")
        b.js(f"{slist}.scrollTop = {slist}.scrollHeight")
        self.assertEqual(b.js("window.scrollY"), scroll_y)
        self.assertGreater(b.js(f"{slist}.scrollTop"), 0)
        # Selecting a row near the bottom rebuilds the list but keeps
        # the scroll position and the active row in view.
        rows = b.js(f"{slist}.children.length")
        b.js(f"{slist}.children[{rows - 1}].click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        self.assertGreater(b.js(f"{slist}.scrollTop"), 0)
        active = ("[...document.querySelectorAll('.scenario-row')]"
                  ".find((n) => n.classList.contains('active'))")
        self.assertTrue(b.js(f"Boolean({active})"))
        self.assertTrue(b.js(
            f"{active}.getBoundingClientRect().top >= "
            f"{slist}.getBoundingClientRect().top - 1"))
        self.assertTrue(b.js(
            f"{active}.getBoundingClientRect().bottom <= "
            f"{slist}.getBoundingClientRect().bottom + 1"))
        # Rows keep their intrinsic card height: no compressed content
        # clipped inside a row and no overlap between neighbours.
        self.assertEqual(b.js(
            f"[...{slist}.querySelectorAll('.scenario-row')]"
            ".filter((r) => r.scrollHeight > r.clientHeight + 1)"
            ".length"), 0)
        self.assertEqual(b.js(
            "(() => { const rows = "
            "[...document.querySelectorAll('.scenario-row')];"
            "return rows.slice(1).filter((row, i) =>"
            " row.getBoundingClientRect().top <"
            " rows[i].getBoundingClientRect().bottom - 1).length;})()"),
            0)
        b.js(f"{pane}.scrollIntoView({{block:'start'}})")
        b.screenshot("scenario-sidebar-sticky-desktop.png")
        # Still contained at a shorter viewport.
        b.viewport(1440, 650)
        b.js(f"window.scrollTo(0, {sidebar}.offsetTop + 400)")
        self.assertAlmostEqual(b.js(
            f"{sidebar}.getBoundingClientRect().top"), 16, delta=1)
        self.assertLessEqual(b.js(
            f"{sidebar}.getBoundingClientRect().bottom"), 634)
        self.assertLessEqual(b.js(
            f"{slist}.getBoundingClientRect().bottom"), 634)
        # Mobile: static sidebar, original capped list, no page overflow.
        b.viewport(390, 844)
        self.assertEqual(b.js(
            f"getComputedStyle({sidebar}).position"), "static")
        self.assertLessEqual(b.js(
            f"{slist}.getBoundingClientRect().height"), 320)
        self.assert_no_overflow()
        b.screenshot("scenario-sidebar-mobile.png")
        b.viewport(1440, 1100)
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
        b.wait("document.querySelector('#scenario-tab-overview').textContent.includes('\"replay\": {')")
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
        b.wait("!document.querySelector('#scenario-tab-overview').textContent.includes('\"replay\": {')")
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

    def test_summary_modules_without_replay_comparison(self):
        b = self.browser
        ov = "document.querySelector('#scenario-tab-overview')"
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        # DemoUI builds escaped DOM only and distinguishes missing from zero.
        self.assertEqual(b.js("DemoUI.stat('Total', 0).textContent"), "Total0")
        self.assertTrue(b.js("DemoUI.stat('Total', null).textContent.includes('Unavailable')"))
        self.assertTrue(b.js("DemoUI.stat('Total', undefined).textContent.includes('Unavailable')"))
        self.assertEqual(b.js("DemoUI.heading('<b>x</b>').querySelector('h3').textContent"), "<b>x</b>")
        self.assertFalse(b.js("DemoUI.disclosure('More', 'k').open"))
        self.assertEqual(b.js("DemoUI.disclosure('More', 'k').dataset.disclosureKey"), "k")
        # A retained original shows the modular summary and no replay comparison.
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('.scenario-row')")
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        self.assertTrue(b.js(f"{ov}.textContent.includes('Session summary')"))
        self.assertTrue(b.js(f"{ov}.textContent.includes('Session overview')"))
        self.assertEqual(b.js(f"{ov}.querySelectorAll('.ui-stat').length"), 3)
        detail = b.js("fetch('/api/teacher/scenarios/' "
                      "+ document.querySelector('.scenario-row.active').dataset.caseId"
                      ").then(r => r.json())")["detail"]
        stats = b.js(f"[...{ov}.querySelectorAll('.ui-stat')].map(n => n.textContent)")
        self.assertIn(f"Answered{detail['answered_count']}/40", stats)
        self.assertIn(
            f"Original scenario score{detail['observed_total']['correct']}"
            f" / {detail['observed_total']['out_of']}", stats)
        self.assertTrue(all("null" not in s for s in stats))
        self.assertIsNone(b.js(
            f"{ov}.querySelector('.partial-replay-score-notice')"))
        # The sidebar row always describes the retained original.
        sidebar = ("[...document.querySelectorAll('.scenario-row .meta')]"
                   ".map(n => n.textContent)")
        want = (f"Original scenario: {detail['observed_total']['correct']}"
                f"/{detail['observed_total']['out_of']} correct")
        self.assertTrue(b.js(
            f"{sidebar}.some(s => s.includes({json.dumps(want)}))"))
        self.assertTrue(b.js(f"{ov}.textContent.includes('Assessment snapshot')"))
        self.assertTrue(b.js(f"{ov}.textContent.includes('Feedback & diagnostics')"))
        self.assertTrue(b.js(f"{ov}.textContent.includes('Release status')"))
        self.assertTrue(b.js(f"{ov}.textContent.includes('Saved read-only feedback')"))
        self.assertTrue(b.js(f"{ov}.textContent.includes('Explore this session')"))
        self.assertIsNone(b.js(f"{ov}.querySelector('.summary-replay-comparison')"))
        provenance = f"{ov}.querySelector('details[data-disclosure-key=provenance]')"
        self.assertTrue(b.js(f"Boolean({provenance})"))
        self.assertFalse(b.js(f"{provenance}.open"))
        # Each explore action activates its matching workspace tab only.
        for label, tab in (("Inspect answers", "evidence"),
                           ("Inspect saved feedback", "feedback"),
                           ("Open diagnostics", "research")):
            b.js("[...document.querySelectorAll('#scenario-tab-overview .ui-actions button')]"
                 f".find(n => n.textContent === '{label}').click()")
            b.wait(f"!document.querySelector('#scenario-tab-{tab}').hidden")
            b.js("document.querySelector('#scenario-ws-tabs [data-tab=overview]').click()")
            b.wait(f"!{ov}.hidden")
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=evidence]').click()")
        b.js("document.querySelector('#scenario-tab-evidence').scrollIntoView({block:'start'})")
        b.screenshot("answers-evidence-desktop.png")
        b.js("document.querySelector('#scenario-ws-tabs [data-tab=overview]').click()")
        b.js(f"{ov}.scrollIntoView({{block:'start'}})")
        b.screenshot("summary-original-desktop.png")
        # A replayed version no longer renders the replay comparison
        # disclosure; the modular summary and provenance remain.
        b.js("document.querySelector('#replay-selected').click()")
        b.wait("document.querySelector('dialog[open]')")
        b.js("document.querySelector('#confirm-replay').click()")
        b.wait("!document.querySelector('dialog[open]')")
        b.wait("document.querySelector('.run-history > summary').textContent.includes('Latest: Complete')")
        b.wait("document.querySelector('#scenario-version').value !== ''")
        self.assertIsNone(b.js(
            f"{ov}.querySelector('.summary-replay-comparison')"))
        self.assertIsNone(b.js(
            f"{ov}.querySelector('details[data-disclosure-key"
            "=summary-replay-comparison]')"))
        self.assertIsNone(b.js(
            f"{ov}.querySelector('details[data-disclosure-key^=replay-diff]')"))
        self.assertFalse(b.js(
            f"{ov}.textContent.includes('What changed from the retained "
            "original?')"))
        # The replayed version is a full Replay score (all 40 answers
        # resubmitted), while the sidebar row still quotes the original.
        b.wait(f"{ov}.textContent.includes('\"replay\": {{')")
        replay_stats = b.js(
            f"[...{ov}.querySelectorAll('.ui-stat')]"
            ".map(n => n.textContent)")
        self.assertIn(
            f"Replay score{detail['observed_total']['correct']} / 40",
            replay_stats)
        self.assertIsNone(b.js(
            f"{ov}.querySelector('.partial-replay-score-notice')"))
        self.assertTrue(b.js(
            f"{sidebar}.some(s => s.includes({json.dumps(want)}))"))
        self.assertTrue(b.js(f"{ov}.textContent.includes('Session summary')"))
        self.assertTrue(b.js(f"Boolean({provenance})"))
        b.js(f"{provenance}.open = true")
        self.assertTrue(b.js(f"{provenance}.open"))
        # A forced data change re-renders the pane; keyed disclosures survive.
        original_session = self.service.teacher_session
        def marked(session_id):
            view = copy.deepcopy(original_session(session_id))
            view["description"] = "Summary refresh marker"
            return view
        self.service.teacher_session = marked
        try:
            b.js("document.querySelector('#scenario-version')"
                 ".dispatchEvent(new Event('change'))")
            b.wait(f"{ov}.textContent.includes('Summary refresh marker')")
            self.assertTrue(b.js(f"{provenance}.open"))
            self.assertIsNone(b.js(
                f"{ov}.querySelector('.summary-replay-comparison')"))
        finally:
            self.service.teacher_session = original_session
        b.js("document.querySelector('#scenario-version')"
             ".dispatchEvent(new Event('change'))")
        b.wait(f"!{ov}.textContent.includes('Summary refresh marker')")
        self.assertTrue(b.js(f"{provenance}.open"))
        b.js(f"{ov}.scrollIntoView({{block:'start'}})")
        b.screenshot("summary-replay-desktop.png")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.js(f"{ov}.scrollIntoView({{block:'start'}})")
        b.screenshot("summary-replay-mobile.png")
        b.viewport(320, 844)
        self.assert_no_overflow()
        b.screenshot("summary-replay-320.png")
        b.viewport(1440, 1100)
        b.js("document.querySelector('[data-workspace=comparison]').click()")
        b.wait("document.querySelector('.comparison-setup form')")
        b.js("document.querySelector('.comparison-setup').scrollIntoView({block:'start'})")
        b.screenshot("blind-comparison-setup-desktop.png")
        # Scoping is per session view: the retained original has no comparison.
        b.js("document.querySelector('#scenario-version').value=''; document.querySelector('#scenario-version').dispatchEvent(new Event('change'))")
        b.wait(f"!{ov}.textContent.includes('\"replay\": {{')")
        self.assertIsNone(b.js(f"{ov}.querySelector('.summary-replay-comparison')"))
        # A live session keeps the same modular summary.
        b.js("document.querySelector('[data-workspace=live]').click(); document.querySelector('#sim-form').requestSubmit()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.wait("document.querySelector('#tab-overview .ui-stat')")
        self.assertEqual(b.js("document.querySelectorAll('#tab-overview .ui-stat').length"), 3)
        self.assertTrue(b.js("document.querySelector('#tab-overview').textContent.includes('Live synthetic session')"))
        self.assertTrue(b.js("document.querySelector('#tab-overview').textContent.includes('Review feedback')"))
        b.js("document.querySelector('#tab-overview').scrollIntoView({block:'start'})")
        b.screenshot("summary-live-desktop.png")
        # Missing counts render as unavailable — never as null or zero.
        def nulled(session_id):
            view = copy.deepcopy(original_session(session_id))
            view["answered_count"] = None
            view["observed_total"] = {"correct": None, "out_of": None}
            return view
        self.service.teacher_session = nulled
        try:
            b.wait("document.querySelector('#tab-overview').textContent.includes('Unavailable')")
            self.assertIn("AnsweredUnavailable",
                          b.js("[...document.querySelectorAll('#tab-overview .ui-stat')]"
                               ".map(n => n.textContent)"))
            self.assertIn("Observed totalUnavailable",
                          b.js("[...document.querySelectorAll('#tab-overview .ui-stat')]"
                               ".map(n => n.textContent)"))
            self.assertFalse(b.js("[...document.querySelectorAll('#tab-overview .ui-stat')]"
                                  ".some(n => n.textContent.includes('null'))"))
        finally:
            self.service.teacher_session = original_session
        b.wait("document.querySelector('#tab-overview .ui-stat')"
               ".textContent.match(/\\d+\\/40/)")
        # The Feedback heading keeps the midpoint/end distinction accurate.
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        b.wait("document.querySelector('#tab-feedback').childElementCount > 0")
        fb = "document.querySelector('#tab-feedback')"
        self.assertTrue(b.js(f"{fb}.textContent.includes("
                             "'Only end feedback and practice require "
                             "teacher approval.')"))
        self.assertTrue(b.js(f"{fb}.textContent.includes("
                             "'Midpoint is shown automatically')"))
        # Mobile captures scroll the target into view after the viewport change.
        b.viewport(390, 844)
        for tab, shot in (("overview", "summary-live-mobile.png"),
                          ("evidence", "answers-evidence-mobile.png"),
                          ("feedback", "feedback-drafts-mobile.png")):
            b.js(f"document.querySelector('#ws-tabs [data-tab={tab}]').click()")
            b.wait(f"!document.querySelector('#tab-{tab}').hidden")
            b.js(f"document.querySelector('#tab-{tab}').scrollIntoView({{block:'start'}})")
            self.assert_no_overflow()
            b.screenshot(shot)
        b.viewport(320, 844)
        b.js("document.querySelector('#ws-tabs [data-tab=overview]').click()")
        b.js("document.querySelector('#tab-overview').scrollIntoView({block:'start'})")
        self.assert_no_overflow()
        b.screenshot("summary-live-320.png")
        # Library and blind-comparison setup keep the same module rhythm.
        b.viewport(390, 844)
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('.scenario-row')")
        b.js("document.querySelector('#library-pane').scrollIntoView({block:'start'})")
        self.assert_no_overflow()
        b.screenshot("library-modular-mobile.png")
        b.js("document.querySelector('[data-workspace=comparison]').click()")
        b.wait("document.querySelector('.comparison-setup form')")
        b.js("document.querySelector('.comparison-setup').scrollIntoView({block:'start'})")
        self.assert_no_overflow()
        b.screenshot("blind-comparison-setup-mobile.png")
        b.viewport(1440, 1100)
        # The student page uses the same module styling end to end.
        self.navigate("/")
        b.wait("!document.querySelector('#start-panel').hidden")
        b.screenshot("student-setup-desktop.png")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.js("document.querySelector('#start-panel').scrollIntoView({block:'start'})")
        b.screenshot("student-setup-mobile.png")
        b.viewport(1440, 1100)
        b.js("document.querySelector('#synthetic-consent').click(); document.querySelector('#start-button').click()")
        b.wait("!document.querySelector('#question-panel').hidden")
        b.screenshot("student-question-desktop.png")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.js("document.querySelector('#question-panel').scrollIntoView({block:'start'})")
        b.screenshot("student-question-mobile.png")
        b.viewport(1440, 1100)
        for index in range(40):
            if index == 20:
                b.wait("!document.querySelector('#pause-panel').hidden")
                b.js("document.querySelector('#continue-button').click()")
            b.wait(f"document.querySelector('#q-pos').textContent === '{index + 1}'")
            b.js("document.querySelector('#q-options input').click(); document.querySelector('#submit-answer').click()")
        b.wait("!document.querySelector('#end-panel').hidden")
        b.screenshot("student-end-desktop.png")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.js("document.querySelector('#end-panel').scrollIntoView({block:'start'})")
        b.screenshot("student-end-mobile.png")
        b.viewport(1440, 1100)
        self.assertEqual(b.errors, [])

    def test_answers_scores_modules_and_filters(self):
        b = self.browser
        ev = "document.querySelector('#tab-evidence')"
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        # A live 'alternating' simulation produces a mixed-correctness
        # evidence payload; the library scenarios are all- or
        # none-correct by construction.
        b.js("document.querySelector('#sim-profile').value = 'alternating';"
             " document.querySelector('#sim-form').requestSubmit()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=evidence]').click()")
        b.wait(f"{ev}.querySelectorAll('.qrow').length === 40")
        mixed = b.js("fetch('/api/teacher/sessions/'"
                     " + document.querySelector('#ws-export').href"
                     ".match(/sessions\\/([^/]+)/)[1]).then(r => r.json())")
        # Observed totals are three stat cards taken from the recorded total.
        total = mixed["checkpoints"]["end"]["evidence"]["total"]
        stats = b.js(f"[...{ev}.querySelectorAll('.ui-stats .ui-stat')]"
                     ".map(n => n.textContent)")
        self.assertEqual(len(stats), 3)
        self.assertIn(f"Answered{total['out_of']}", stats)
        self.assertIn(f"Correct{total['correct']}", stats)
        self.assertIn(f"Incorrect{total['incorrect']}", stats)
        # Topic results are a separate section with an accessible table.
        topic = (f"[...{ev}.querySelectorAll('.ui-section')]"
                 ".find(s => s.textContent.includes('Topic results'))")
        self.assertTrue(b.js(f"Boolean({topic})"))
        self.assertTrue(b.js(f"Boolean({topic}.querySelector('thead'))"))
        self.assertTrue(b.js(f"Boolean({topic}.querySelector('tbody'))"))
        self.assertTrue(b.js(f"[...{topic}.querySelectorAll('thead th')]"
                             ".every(th => th.scope === 'col')"))
        self.assertEqual(b.js(f"{topic}.querySelectorAll('tbody tr').length"),
                         len(mixed["checkpoints"]["end"]["evidence"]["subtopics"]))
        # Each qrow has a structured summary, labelled answer blocks and a
        # keyed nested meta disclosure; the totals/table stay unfiltered.
        first = mixed["question_responses"][0]
        row0 = f"{ev}.querySelector('.qrow')"
        self.assertEqual(b.js(f"{row0}.dataset.disclosureKey"),
                         f"q:{first['question_id']}")
        self.assertEqual(b.js(f"{row0}.querySelector('.qrow-num').textContent"),
                         f"#{first['position']}")
        badge = ("Correct" if first["correct"] else "Incorrect")
        self.assertEqual(
            b.js(f"{row0}.querySelector('.qrow-badge').textContent"), badge)
        self.assertTrue(b.js(
            f"{row0}.querySelector('.qrow-badge').classList.contains("
            f"'{'correct' if first['correct'] else 'incorrect'}')"))
        self.assertTrue(b.js(f"Boolean({row0}.querySelector('.qrow-skill'))"))
        self.assertTrue(b.js(f"Boolean({row0}.querySelector('.qrow-subtopic'))"))
        b.js(f"{row0}.querySelector('summary').click()")
        self.assertTrue(b.js(f"{row0}.open"))
        self.assertEqual(
            b.js(f"[...{row0}.querySelectorAll('.qrow-answer-label')]"
                 ".map(n => n.textContent)"),
            ["Student answer", "Correct answer"])
        self.assertIn(
            b.js("englishOptionText("
                 f"{json.dumps(first['options'][first['selected_index']])})"),
            b.js(f"{row0}.querySelectorAll('.qrow-answer-value')[0].textContent"))
        self.assertIn(
            b.js("englishOptionText("
                 f"{json.dumps(first['options'][first['answer_index']])})"),
            b.js(f"{row0}.querySelectorAll('.qrow-answer-value')[1].textContent"))
        meta_key = f"q:{first['question_id']}:meta"
        self.assertTrue(b.js(
            f"Boolean({row0}.querySelector("
            f"'details[data-disclosure-key=\"{meta_key}\"]'))"))
        self.assertIn("Question reference",
                      b.js(f"{row0}.querySelector('.qrow-meta').textContent"))
        # Filter controls: outcome buttons are pressed-state toggles, the
        # skill select is labelled, and the status line is live.
        btns = (f"[...{ev}.querySelectorAll('.evidence-filter-btn')]")
        self.assertEqual(b.js(f"{btns}.map(n => n.textContent)"),
                         ["All", "Incorrect", "Correct"])
        self.assertEqual(b.js(f"{btns}.map(n => n.getAttribute('aria-pressed'))"),
                         ["true", "false", "false"])
        self.assertTrue(b.js(
            f"Boolean({ev}.querySelector('.evidence-skill-filter select'))"))
        self.assertEqual(b.js(
            f"{ev}.querySelector('.evidence-filter-status')"
            ".getAttribute('aria-live')"), "polite")
        self.assertEqual(b.js(
            f"{ev}.querySelector('.evidence-filter-status').textContent"),
            "Showing 40 of 40 questions")
        # Outcome filter hides rows in place; totals and topic rows are
        # untouched and the visible count reflects only shown rows.
        b.js(f"{btns}.find(n => n.dataset.outcome === 'incorrect').click()")
        incorrect = total["incorrect"]
        self.assertEqual(b.js(
            f"{ev}.querySelector('.evidence-filter-status').textContent"),
            f"Showing {incorrect} of 40 questions")
        self.assertEqual(b.js(
            f"[...{ev}.querySelectorAll('.qrow')]"
            ".filter(n => !n.hidden).length"), incorrect)
        self.assertTrue(b.js(
            f"[...{ev}.querySelectorAll('.qrow')].filter(n => !n.hidden)"
            ".every(n => n.querySelector('.qrow-badge').textContent === 'Incorrect')"))
        self.assertEqual(b.js(f"{topic}.querySelectorAll('tbody tr').length"),
                         len(mixed["checkpoints"]["end"]["evidence"]["subtopics"]))
        self.assertIn(f"Correct{total['correct']}",
                      b.js(f"[...{ev}.querySelectorAll('.ui-stat')]"
                           ".map(n => n.textContent)"))
        # The opened row survives being hidden and shown again.
        b.js(f"{btns}.find(n => n.dataset.outcome === 'all').click()")
        self.assertTrue(b.js(f"{row0}.open"))
        # Intersecting outcome + skill narrows further; an impossible
        # combination shows the explicit empty message.
        skills = sorted({str(r["skill_id"]) for r in mixed["question_responses"]})
        sel = f"{ev}.querySelector('.evidence-skill-filter select')"
        self.assertEqual(b.js(f"[...{sel}.options].map(o => o.value)"),
                         [""] + skills)
        b.js(f"{sel}.value = {json.dumps(skills[0])};"
             f" {sel}.dispatchEvent(new Event('change'))")
        b.js(f"{btns}.find(n => n.dataset.outcome === 'incorrect').click()")
        expected = sum(1 for r in mixed["question_responses"]
                       if not r["correct"] and str(r["skill_id"]) == skills[0])
        self.assertEqual(b.js(
            f"{ev}.querySelector('.evidence-filter-status').textContent"),
            f"Showing {expected} of 40 questions")
        self.assertEqual(b.js(
            f"[...{ev}.querySelectorAll('.qrow')]"
            ".filter(n => !n.hidden).length"), expected)
        empty_combo = next(
            ((outcome, skill)
             for outcome in ("correct", "incorrect") for skill in skills
             if not any((r["correct"] == (outcome == "correct"))
                        and str(r["skill_id"]) == skill
                        for r in mixed["question_responses"])),
            None)
        if empty_combo:
            outcome, skill = empty_combo
            b.js(f"{sel}.value = {json.dumps(skill)};"
                 f" {sel}.dispatchEvent(new Event('change'))")
            b.js(f"{btns}.find(n => n.dataset.outcome === '{outcome}').click()")
            self.assertEqual(b.js(
                f"{ev}.querySelector('.evidence-filter-status').textContent"),
                "Showing 0 of 40 questions")
            self.assertFalse(b.js(
                f"{ev}.querySelector('.evidence-filter-empty').hidden"))
            self.assertIn("No questions match",
                          b.js(f"{ev}.querySelector('.evidence-filter-empty')"
                               ".textContent"))
        # A changed-detail rerender preserves a non-default filter and
        # the open question disclosure.
        b.js(f"{sel}.value = {json.dumps(skills[0])};"
             f" {sel}.dispatchEvent(new Event('change'))")
        b.js(f"{btns}.find(n => n.dataset.outcome === 'incorrect').click()")
        self.assertEqual(b.js(
            f"{ev}.querySelector('.evidence-filter-status').textContent"),
            f"Showing {expected} of 40 questions")
        original_detail = self.service.teacher_session
        def marked(session_id):
            view = copy.deepcopy(original_detail(session_id))
            view["description"] = "Evidence refresh marker"
            return view
        self.service.teacher_session = marked
        try:
            b.wait("document.querySelector('#tab-overview')"
                   ".textContent.includes('Evidence refresh marker')")
            self.assertTrue(b.js(f"{row0}.open"))
            self.assertTrue(b.js(
                f"Boolean({row0}.querySelector('details[data-disclosure-key"
                f"=\"{meta_key}\"]'))"))
            self.assertEqual(b.js(f"{sel}.value"), skills[0])
            self.assertEqual(b.js(
                f"{btns}.map(n => n.getAttribute('aria-pressed'))"),
                ["false", "true", "false"])
            self.assertEqual(b.js(
                f"{ev}.querySelector('.evidence-filter-status').textContent"),
                f"Showing {expected} of 40 questions")
        finally:
            self.service.teacher_session = original_detail
        b.wait("!document.querySelector('#tab-overview')"
               ".textContent.includes('Evidence refresh marker')")
        # Filter and disclosure state are isolated per selected session.
        b.js(f"{sel}.value = ''; {sel}.dispatchEvent(new Event('change'))")
        b.js("document.querySelector('#sim-seed').value = '7';"
             " document.querySelector('#sim-form').requestSubmit()")
        b.wait(f"{btns}.map(n => n.getAttribute('aria-pressed'))"
               ".join() === 'true,false,false'"
               f" && !{ev}.querySelector('.qrow').open"
               f" && {ev}.querySelectorAll('.qrow').length === 40")
        b.js("[...document.querySelectorAll('.session-row')]"
             ".find(n => !n.classList.contains('active')).click()")
        b.wait(f"{ev}.querySelector('.evidence-filter-status')"
               f".textContent === 'Showing {incorrect} of 40 questions'"
               f" && {row0}.open")
        # The shared scenario detail view gets the same modules, and an
        # impossible filter combination shows the explicit empty message.
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('.scenario-row')")
        sev = "document.querySelector('#scenario-tab-evidence')"
        b.js("document.querySelector('#scenario-search').value = "
             "'Every answer incorrect'; document.querySelector("
             "'#scenario-search').dispatchEvent(new Event('input'))")
        b.wait("document.querySelectorAll('.scenario-row').length === 1")
        b.js("document.querySelector('.scenario-row').click()")
        b.wait(f"{sev}.querySelectorAll('.qrow').length === 40")
        sbtns = (f"[...{sev}.querySelectorAll('.evidence-filter-btn')]")
        b.js(f"{sbtns}.find(n => n.dataset.outcome === 'correct').click()")
        self.assertEqual(b.js(
            f"{sev}.querySelector('.evidence-filter-status').textContent"),
            "Showing 0 of 40 questions")
        self.assertFalse(b.js(
            f"{sev}.querySelector('.evidence-filter-empty').hidden"))
        self.assertIn("No questions match",
                      b.js(f"{sev}.querySelector('.evidence-filter-empty')"
                           ".textContent"))
        # A partial detail renders the waiting text and notice, no cards.
        b.js("const host = document.querySelector('#ws-detail')"
             ".cloneNode(true); host.id = 'fixture-ws';"
             " host.querySelectorAll('[id]').forEach(n => n.id = 'fx-' + n.id);"
             " host.hidden = false; document.body.appendChild(host);"
             " window.__fxView = createDetailView(host, {prefix: 'fx-'});"
             " window.__fxDetail = null;")
        partial = copy.deepcopy(mixed)
        partial["session_id"] = "fixture-partial"
        partial["checkpoints"]["end"]["evidence"] = None
        partial["question_responses"] = []
        partial["response_notice"] = "Individual answers cannot be shown."
        b.js(f"window.__fxDetail = {json.dumps(partial)};"
             " window.__fxView.update(window.__fxDetail)")
        fx = "document.querySelector('#fx-tab-evidence')"
        self.assertIn("End evidence appears once all 40 answers are in.",
                      b.js(f"{fx}.textContent"))
        self.assertIn("Individual answers cannot be shown.",
                      b.js(f"{fx}.textContent"))
        self.assertIsNone(b.js(f"{fx}.querySelector('.ui-stat')"))
        self.assertIsNone(b.js(f"{fx}.querySelector('.qrow')"))
        # No horizontal overflow at desktop or 390px; filter UI lives only
        # inside the teacher evidence tab.
        b.js("document.querySelector('#fixture-ws').remove()")
        self.assertIsNone(b.js("document.querySelector('#start-panel .qrow')"))
        # Return to the live session's Answers & scores tab with filters
        # reset so the capture shows the changed pane itself.
        b.js("document.querySelector('[data-workspace=live]').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('#ws-tabs [data-tab=evidence]').click()")
        b.wait(f"!{ev}.hidden")
        b.js(f"{btns}.find(n => n.dataset.outcome === 'all').click()")
        b.js(f"{sel}.value = ''; {sel}.dispatchEvent(new Event('change'))")
        b.wait(f"{ev}.querySelector('.evidence-filter-status')"
               ".textContent === 'Showing 40 of 40 questions'")
        b.js(f"{ev}.scrollIntoView({{block:'start'}})")
        self.assert_no_overflow()
        b.screenshot("answers-scores-desktop.png")
        # The expanded record keeps its two answer blocks inside the pane.
        b.js(f"{row0}.open = true")
        b.js(f"{row0}.scrollIntoView({{block:'center'}})")
        self.assert_no_overflow()
        b.screenshot("answers-scores-expanded-desktop.png")
        b.viewport(390, 844)
        b.js(f"{ev}.scrollIntoView({{block:'start'}})")
        self.assert_no_overflow()
        b.screenshot("answers-scores-mobile.png")
        b.js(f"{row0}.scrollIntoView({{block:'center'}})")
        self.assert_no_overflow()
        b.screenshot("answers-scores-expanded-mobile.png")
        b.viewport(1440, 1100)
        self.assertEqual(b.errors, [])

    def test_workspace_and_assessment_journey(self):
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        # Log out lives in the masthead and stays hidden until unlock.
        self.assertTrue(b.js("document.querySelector('#logout').hidden"))
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        self.assertFalse(b.js("document.querySelector('#logout').hidden"))
        self.assertIsNotNone(b.js("document.querySelector('.masthead #logout')"))
        self.assertIsNone(b.js("document.querySelector('.workspace-nav #logout')"))
        self.assertEqual(b.js(
            "document.querySelectorAll('.workspace-nav [data-workspace]')"
            ".length"), 3)
        self.assertTrue(b.js("document.querySelector('#library-pane').hidden && document.querySelector('#comparison-pane').hidden"))
        self.assertEqual(b.js("document.querySelector('#pin-input').value"), "")
        b.screenshot("masthead-unlocked-desktop.png")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.screenshot("masthead-unlocked-mobile.png")
        b.viewport(320, 844)
        self.assert_no_overflow()
        b.viewport(1440, 1100)
        # Run configuration lives collapsed inside the Live sessions
        # workspace below the tab bar — never a floating top strip.
        strip = "document.querySelector('#config-strip')"
        run_cfg = "document.querySelector('#run-configuration')"
        nav = "document.querySelector('.workspace-nav')"
        self.assertTrue(b.js(
            f"Boolean({run_cfg}) && {run_cfg}.contains({strip})"))
        self.assertTrue(b.js(
            f"document.querySelector('#live-pane').contains({run_cfg})"))
        self.assertFalse(b.js(f"{run_cfg}.open"))
        self.assertIn("Run configuration", b.js(f"{run_cfg}.textContent"))
        self.assertTrue(b.js(
            f"{nav}.compareDocumentPosition({strip}) "
            "& Node.DOCUMENT_POSITION_FOLLOWING"))
        nav_y = f"{nav}.getBoundingClientRect().top + window.scrollY"
        y0 = b.js(nav_y)
        b.js(f"{run_cfg}.open = true")
        self.assertTrue(b.js(
            f"{strip}.textContent.includes('providers:')"))
        self.assertTrue(b.js(
            f"{strip}.textContent.includes('New provider calls')"))
        self.assertAlmostEqual(b.js(nav_y), y0, delta=1)
        # Refreshing the chips does not collapse the disclosure.
        time.sleep(2.4)
        self.assertTrue(b.js(f"{run_cfg}.open"))
        self.assertAlmostEqual(b.js(nav_y), y0, delta=1)
        # Switching workspaces keeps the tab bar exactly in place.
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('#scenario-search')")
        self.assertAlmostEqual(b.js(nav_y), y0, delta=1)
        b.js("document.querySelector('[data-workspace=comparison]').click()")
        b.wait("document.querySelector('.comparison-setup')")
        self.assertAlmostEqual(b.js(nav_y), y0, delta=1)
        b.js("document.querySelector('[data-workspace=live]').click()")
        b.wait("!document.querySelector('#live-pane').hidden")
        self.assertAlmostEqual(b.js(nav_y), y0, delta=1)
        self.assertIsNone(b.js(
            "document.querySelector('#library-pane #config-strip')"))
        self.assertIsNone(b.js(
            "document.querySelector('#comparison-pane #config-strip')"))
        b.screenshot("run-config-expanded-desktop.png")
        b.js(f"{run_cfg}.open = false")
        b.screenshot("run-config-collapsed-desktop.png")
        b.viewport(390, 844)
        ym = b.js(nav_y)
        b.js(f"{run_cfg}.open = true")
        self.assertAlmostEqual(b.js(nav_y), ym, delta=1)
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('#scenario-search')")
        self.assertAlmostEqual(b.js(nav_y), ym, delta=1)
        b.js("document.querySelector('[data-workspace=live]').click()")
        b.wait("!document.querySelector('#live-pane').hidden")
        self.assert_no_overflow()
        b.screenshot("run-config-expanded-mobile.png")
        b.js(f"{run_cfg}.open = false")
        b.screenshot("run-config-collapsed-mobile.png")
        b.viewport(1440, 1100)
        if DEFAULT_REPORT.is_file():
            self.service.research = ResearchWorkspace(self.root / "data")
        b.js("document.querySelector('[data-workspace=library]').click()")
        b.wait("document.querySelector('#scenario-search')")
        expected = 54 if DEFAULT_REPORT.is_file() else 4
        self.assertEqual(b.js("document.querySelectorAll('.scenario-row').length"), expected)
        self.assertFalse(b.js("Boolean(document.querySelector('#research-readiness'))"))
        self.assertEqual(b.js("document.querySelectorAll('.readiness-card').length"), 0)
        library_text = "document.querySelector('#library-pane').textContent"
        self.assertFalse(b.js(f"{library_text}.includes('Observed answer counts describe this assessment')"))
        self.assertFalse(b.js(f"{library_text}.includes('synthetic test cases, not real learners')"))
        self.assertFalse(b.js("document.querySelector('#library-pane').textContent.toLowerCase().includes('conformal')"))
        self.assertFalse(b.js(f"{library_text}.includes('Replay identity and interpretation limits')"))
        # The library keeps scenario count and replay actions but the
        # retired summary metrics row and its disclaimer are gone.
        self.assertEqual(b.js("document.querySelectorAll('#library-pane .metric-card').length"), 0)
        self.assertIsNone(b.js("document.querySelector('#library-pane .metrics-grid')"))
        self.assertFalse(b.js(f"{library_text}.includes('retained original report')"))
        self.assertTrue(b.js(f"{library_text}.includes('of {expected} scenarios')"))
        self.assertTrue(b.js("document.querySelector('#replay-all').textContent.includes('Replay all "
                             f"{expected} scenarios')"))
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
        b.js("const f=document.querySelector('.comparison-setup form'); f.querySelector('input').value='browser-fixture'; "
             "const e=f.querySelectorAll('select')[1]; if (!e.value) e.value='yes'; f.requestSubmit()")
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
        # An expired session is caught by the next poll: the workspace
        # and the masthead Log out both hide again.
        b.js("fetch('/api/teacher/logout', {method: 'POST', headers: "
             "{'Content-Type': 'application/json'}, body: '{}'})")
        b.wait("!document.querySelector('#unlock-panel').hidden")
        self.assertTrue(b.js("document.querySelector('#teacher-app').hidden"))
        self.assertTrue(b.js("document.querySelector('#logout').hidden"))
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        self.assertFalse(b.js("document.querySelector('#logout').hidden"))
        # The masthead Log out button ends the session itself.
        b.js("document.querySelector('#logout').click()")
        b.wait("!document.querySelector('#unlock-panel').hidden")
        self.assertTrue(b.js("document.querySelector('#logout').hidden"))
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        self.assertFalse(b.js("document.querySelector('#logout').hidden"))
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
        self.assertTrue(b.js("document.querySelector('#end-summary').textContent.includes('Educator workspace')"))
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
        b.wait(f"{summary}.includes('Educator workspace')")
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
            f"{card_text}.includes('Midpoint check-in — automatic')"))
        self.assertTrue(b.js(
            f"{card_text}.includes('Shown directly to the student when "
            "ready. No teacher approval.')"))
        self.assertTrue(b.js(
            f"{card_text}.includes('does not confirm that the student "
            "read it')"))
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
               ".textContent.includes('\"replay\": {')")
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

    def test_failed_replay_overview(self):
        # A case that fails mid-submission keeps its recorded answers
        # but the overview must say the replay stopped — not that it is
        # still in progress with a provider job never requested.
        b = self.browser
        library = self.service.research.library()
        scenario_id = library["scenarios"][0]["id"]
        original = self.service.submit_response
        calls = {"count": 0}

        def wrapped(session_id, token, submission):
            calls["count"] += 1
            if calls["count"] == 28:
                raise PermissionError("test failure")
            return original(session_id, token, submission)

        with mock.patch.object(self.service, "submit_response",
                               side_effect=wrapped):
            with contextlib.redirect_stdout(io.StringIO()):
                run = self.service.replays.start({
                    "request_id": secrets.token_hex(16),
                    "report_sha256": library["report_sha256"],
                    "scenario_ids": [scenario_id],
                    "provider_mode": "rules",
                    "allow_provider_calls": False})
                self.assertTrue(wait_for(
                    lambda: self.service.replays.get(run["id"])["status"]
                    not in ("queued", "running", "cancelling"),
                    timeout=30))
        finished = self.service.replays.get(run["id"])
        self.assertEqual(finished["status"], "complete_with_errors")
        case = finished["cases"][0]
        self.assertEqual(case["status"], "failed")
        sid = case["session_id"]
        detail = self.service.replays.result(run["id"], scenario_id)
        self.assertEqual(detail["answered_count"], 27)
        self.assertEqual(detail["replay_execution"]["status"], "failed")
        # Midpoint diagnostics are separate from generation and finish
        # deterministically; read the detail only once they are ready.
        self.assertTrue(wait_for(
            lambda: (self.service._load_meta(sid)["checkpoints"]
                     ["midpoint"].get("diagnostics_job") or {})
                    .get("status") == "ready",
            timeout=30))
        detail = self.service.replays.result(run["id"], scenario_id)
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("Boolean(document.querySelector('#ws-detail'))")
        b.js("const host = document.querySelector('#ws-detail')"
             ".cloneNode(true); host.id = 'fixture-ws';"
             " host.querySelectorAll('[id]').forEach(n => n.id = 'fx-' + n.id);"
             " host.hidden = false; document.body.appendChild(host);"
             " window.__fxView = createDetailView(host, {prefix: 'fx-'});")
        ov = "document.querySelector('#fx-tab-overview')"
        notice = f"{ov}.querySelector('.replay-failure-notice')"
        b.js(f"window.__fxView.update({json.dumps(detail)})")
        self.assertIn("Replay failed after 27 of 40 answers were "
                      "recorded.", b.js(f"{notice}.textContent"))
        stats = b.js(
            f"[...{ov}.querySelectorAll('.ui-stat')]"
            ".map(n => n.textContent)")
        self.assertIn("StatusReplay failed", stats)
        self.assertIn("Answered27/40", stats)
        total = detail["observed_total"]
        self.assertIn(
            f"Partial replay score{total['correct']} / {total['out_of']}",
            stats)
        self.assertEqual(
            b.js(f"{ov}.querySelector('.partial-replay-score-notice')"
                 ".textContent"),
            "No final replay score is available because not all 40 "
            "answers were recorded.")
        text = b.js(f"{ov}.textContent")
        self.assertFalse(b.js(f"{ov}.textContent.includes"
                              "('In progress')"))
        self.assertFalse(b.js(f"{ov}.textContent.includes"
                              "('resubmitted')"))
        self.assertIn("End-feedback generation", text)
        self.assertIn("Not started - replay stopped before completion",
                      text)
        self.assertIn("Unavailable - replay failed", text)
        rows = b.js(
            f"[...{ov}.querySelectorAll('.kv')]"
            ".map(n => n.textContent)")
        self.assertIn("midpoint diagnosticsready", rows)
        # A plain teacher view of the same session has no
        # replay_execution; the cancellation marker alone still conveys
        # the failure.
        teacher_view = self.service.teacher_session(sid)
        self.assertNotIn("replay_execution", teacher_view)
        b.js(f"window.__fxView.update({json.dumps(teacher_view)})")
        self.assertIn("Replay failed after 27 of 40 answers were "
                      "recorded.", b.js(f"{notice}.textContent"))
        self.assertIn(
            f"Partial replay score{total['correct']} / {total['out_of']}",
            b.js(f"[...{ov}.querySelectorAll('.ui-stat')]"
                 ".map(n => n.textContent)"))
        self.assertTrue(b.js(
            f"Boolean({ov}.querySelector("
            "'.partial-replay-score-notice'))"))
        # A completed replay shows a full Replay score and no partial
        # notice — only a synthetic copy is adjusted, the real session
        # stays at 27 answers.
        full = copy.deepcopy(detail)
        full["answered_count"] = 40
        full["observed_total"] = {
            "correct": total["correct"], "out_of": 40}
        b.js(f"window.__fxView.update({json.dumps(full)})")
        full_stats = b.js(
            f"[...{ov}.querySelectorAll('.ui-stat')]"
            ".map(n => n.textContent)")
        self.assertTrue(any(s.startswith("Replay score")
                            for s in full_stats))
        self.assertFalse(any(s.startswith("Partial replay score")
                             for s in full_stats))
        self.assertIsNone(b.js(
            f"{ov}.querySelector('.partial-replay-score-notice')"))
        # A healthy in-progress session shows neither notice nor the
        # failed status label, and keeps the plain observed-total label.
        fresh = self.service.create_session()
        fresh_view = self.service.teacher_session(fresh["session_id"])
        b.js(f"window.__fxView.update({json.dumps(fresh_view)})")
        self.assertIsNone(b.js(notice))
        self.assertIsNone(b.js(
            f"{ov}.querySelector('.partial-replay-score-notice')"))
        fresh_stats = b.js(
            f"[...{ov}.querySelectorAll('.ui-stat')]"
            ".map(n => n.textContent)")
        self.assertIn("StatusIn progress", fresh_stats)
        self.assertTrue(any(s.startswith("Observed total")
                            for s in fresh_stats))
        # A retained original carries no failure warning.
        retained = b.js("fetch('/api/teacher/scenarios/' "
                        f"+ {json.dumps(scenario_id)})"
                        ".then(r => r.json())")["detail"]
        b.js(f"window.__fxView.update({json.dumps(retained)})")
        self.assertIsNone(b.js(notice))
        b.js("document.querySelector('#fixture-ws').remove()")
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
        b.wait(f"{summary}.includes('Educator workspace')")
        self.assertTrue(b.js(f"{summary}.includes('You answered 20 of 40 questions correctly.')"))
        self.assertFalse(b.js(f"{summary}.includes('Observed result')"))
        self.assertFalse(b.js("document.querySelector('#end-summary .feedback-section')"))
        self.assertFalse(b.js("document.querySelector('#end-summary .skill-list')"))
        self.navigate(result["student_url"])
        b.wait(f"{summary}.includes('You answered 20 of 40 questions correctly.')")
        b.wait(f"{summary}.includes('Educator workspace')")
        b.screenshot("student-total-awaiting-release.png")
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]["status"]
            in ("ready", "fallback")))
        view = self.service.teacher_session(sid)
        self.service.release_feedback(sid, {
            "message_sha256": view["feedback_delivery"]["preview_sha256"],
            "reviewer_label": "browser-reviewer"})
        # Released feedback never reaches the assessment page, even with a
        # valid student token: the end panel keeps the completion score
        # and the educator-workspace note only.
        self.navigate(result["student_url"])
        b.wait(f"{summary}.includes('Educator workspace')")
        self.assertTrue(b.js(f"{summary}.includes('You answered 20 of 40 questions correctly.')"))
        self.assertFalse(b.js(f"{summary}.includes('Assessment summary')"))
        self.assertFalse(b.js(f"{summary}.includes('reviewed and released')"))
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
        self.assertIn("Assessment summary", preview_text)
        self.assertNotIn(preview_text, student_text)
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
        b.wait(f"{summary}.includes('Educator workspace')")
        self.assertFalse(b.js(f"{summary}.includes('Assessment summary')"))
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
            f"{release}.textContent.includes('End feedback & practice')"))
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
        self.assertEqual(b.js(
            f"getComputedStyle({research_dd}.querySelector(':scope > summary'))"
            ".fontWeight"), "400")
        self.assertEqual(b.js(
            f"getComputedStyle({research_dd}.querySelector(':scope > summary'))"
            ".fontWeight"),
            b.js(f"getComputedStyle({edit_dd}.querySelector(':scope > summary'))"
                 ".fontWeight"))
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

    def test_feedback_navigation_and_hierarchy(self):
        import copy
        from unittest import mock
        import feedback_practice
        from tests.test_demo_service import BoundRecommender
        from tests.test_feedback_practice import make_graph, make_pool
        bank = self.service.bank
        pool = make_pool(("sA", "sB"))
        b = self.browser
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
            result = self.service.simulate("alternating", 11)
            sid = result["session_id"]
            self.assertTrue(wait_for(
                lambda: self.service._load_meta(sid)["provider_job"]
                ["status"] in ("ready", "fallback")))
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
            b.wait("!document.querySelector('#ws-detail').hidden")
            b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
            pane = "document.querySelector('#tab-feedback')"
            release = f"{pane}.querySelector('.release-card')"
            b.wait(f"Boolean({release})")
            # Status strip: release state and recorded wording source.
            strip = f"{pane}.querySelector('.feedback-status-strip')"
            pills = b.js(
                f"[...{strip}.querySelectorAll('.fb-pill')]"
                ".map(n => n.textContent)")
            self.assertIn("Midpoint: automatic — no approval", pills)
            self.assertTrue(any(p.startswith("End feedback: ")
                                for p in pills))
            self.assertTrue(any(p.startswith("End wording: ")
                                for p in pills))
            self.assertFalse(any("null" in p for p in pills))
            # Section nav is built from the rendered DOM: all four
            # targets exist on this session and focus their heading.
            nav = f"{pane}.querySelector('.feedback-nav')"
            self.assertEqual(b.js(f"{nav}.getAttribute('aria-label')"),
                             "Feedback sections")
            labels = b.js(
                f"[...{nav}.querySelectorAll('.feedback-nav-btn')]"
                ".map(n => n.textContent)")
            self.assertEqual(labels,
                             ["Midpoint (automatic)", "End feedback",
                              "End practice", "Review & release"])
            for label, target in (
                    ("Midpoint (automatic)",
                     ".midpoint-feedback-card h3"),
                    ("End feedback", ".fb-preview-heading"),
                    ("End practice",
                     ".feedback-preview .practice-activity"),
                    ("Review & release", ".review-release-heading")):
                b.js(f"[...{nav}.querySelectorAll('.feedback-nav-btn')]"
                     f".find(n => n.textContent === '{label}').click()")
                self.assertTrue(b.js(
                    f"{pane}.querySelector('{target}')"
                    " === document.activeElement"))
            # Hierarchy: stage eyebrows, boundaries and labels sit
            # outside the preview region; midpoint never claims it was
            # read and carries no approval requirement.
            midpoint = f"{pane}.querySelector('.midpoint-feedback-card')"
            self.assertEqual(b.js(
                f"{midpoint}.querySelector('.eyebrow').textContent"),
                "After 20 answers")
            self.assertTrue(b.js(
                f"{midpoint}.textContent.includes('Midpoint check-in — "
                "automatic')"))
            self.assertTrue(b.js(
                f"{midpoint}.textContent.includes('Shown directly to "
                "the student when ready. No teacher approval.')"))
            self.assertTrue(b.js(
                f"{midpoint}.textContent.includes('does not confirm "
                "that the student read it')"))
            self.assertFalse(b.js(
                f"{midpoint}.textContent.includes('approval is "
                "required')"))
            self.assertEqual(b.js(
                f"{release}.querySelector('.eyebrow').textContent"),
                "After 40 answers")
            self.assertTrue(b.js(
                f"{release}.textContent.includes('End feedback & "
                "practice')"))
            self.assertTrue(b.js(
                f"{release}.textContent.includes('Teacher approval is "
                "required before end feedback and practice are shown "
                "to the student.')"))
            self.assertEqual(b.js(
                f"{release}.querySelector('.fb-preview-heading')"
                ".textContent"),
                "End feedback preview — awaiting approval")
            self.assertEqual(b.js(
                f"{pane}.querySelector('.fb-preview-label')"
                ".textContent"), "Student preview")
            self.assertIsNone(b.js(
                f"{pane}.querySelector('.feedback-preview "
                ".fb-preview-label')"))
            self.assertTrue(b.js(
                f"{pane}.textContent.includes('released with end "
                "feedback after approval')"))
            b.js(f"{pane}.scrollIntoView({{block:'start'}})")
            self.assert_no_overflow()
            b.screenshot("feedback-top-pending-desktop.png")
            b.js(f"{pane}.querySelector('.review-release-heading')"
                 ".scrollIntoView({block:'start'})")
            b.screenshot("feedback-end-pending-desktop.png")
            # A pending midpoint presents the waiting notice and keeps
            # the 'when ready' boundary — it never claims shown/read.
            original = self.service.teacher_session
            self.addCleanup(
                setattr, self.service, "teacher_session", original)
            def pending_midpoint(sid_arg):
                v = copy.deepcopy(original(sid_arg))
                v["checkpoints"]["midpoint"]["phrasing_job"] = {
                    "status": "pending"}
                return v
            self.service.teacher_session = pending_midpoint
            b.wait(f"{midpoint}.textContent.includes('preparing the "
                   "midpoint check-in')")
            self.assertTrue(b.js(
                f"{midpoint}.textContent.includes('when ready')"))
            self.service.teacher_session = original
            b.wait(f"Boolean({release}.querySelector('.fb-preview-"
                   "heading'))")
            # A released state updates the strip and card truthfully.
            view = self.service.teacher_session(sid)
            self.service.release_feedback(sid, {
                "message_sha256":
                    view["feedback_delivery"]["preview_sha256"],
                "reviewer_label": "browser-reviewer"})
            b.wait(f"{release}.textContent.includes('Released to the "
                   "student')")
            pills = b.js(
                f"[...{strip}.querySelectorAll('.fb-pill')]"
                ".map(n => n.textContent)")
            self.assertIn("End feedback: Released to the student",
                          pills)
            self.assertEqual(b.js(
                f"{release}.querySelector('.fb-preview-heading')"
                ".textContent"), "Released end feedback")
            self.assertEqual(b.js(
                f"{release}.querySelector('.fb-preview-label')"
                ".textContent"), "Student-visible version")
            self.assertTrue(b.js(
                f"{release}.textContent.includes('This version is "
                "released. No further approval is needed unless you "
                "edit it.')"))
            self.assertTrue(b.js(
                f"{release}.textContent.includes('Release complete')"))
            self.assertTrue(b.js(
                f"{release}.textContent.includes('Practice below is "
                "included in this release.')"))
            # Teacher-only chrome must not keep claiming approval is
            # required once this version is released (authored warnings
            # inside the preview are student content and may remain).
            self.assertFalse(b.js(
                f"{release}.textContent.includes('educator approval "
                "required')"))
            self.assertIn("Release complete", b.js(
                f"[...{nav}.querySelectorAll('.feedback-nav-btn')]"
                ".map(n => n.textContent)"))
            b.js(f"{pane}.scrollIntoView({{block:'start'}})")
            b.screenshot("feedback-top-released-desktop.png")
            b.js(f"{pane}.querySelector('.review-release-heading')"
                 ".scrollIntoView({block:'start'})")
            b.screenshot("feedback-end-released-desktop.png")
            # A changed draft after release is labelled as an updated,
            # unreleased version and re-enables approval.
            def changed_draft(sid_arg):
                v = copy.deepcopy(original(sid_arg))
                v["feedback_delivery"]["preview_sha256"] = (
                    "changed-preview-hash")
                return v
            self.service.teacher_session = changed_draft
            b.wait(f"{release}.textContent.includes('Updated end "
                   "feedback — not yet released')")
            self.assertTrue(b.js(
                f"{release}.textContent.includes('needs approval. The "
                "student still sees the previous release.')"))
            self.assertTrue(b.js(
                f"{release}.textContent.includes('belongs to the "
                "updated draft, not the previous release.')"))
            self.assertTrue(any(p.startswith(
                "End feedback: Updated draft") for p in b.js(
                f"[...{strip}.querySelectorAll('.fb-pill')]"
                ".map(n => n.textContent)")))
            self.assertFalse(b.js(
                f"{release}.querySelector('button.primary').disabled"))
            # A teacher-edited document cannot be parsed for practice:
            # the End practice link disappears instead of jumping
            # nowhere.
            def edited_doc(sid_arg):
                v = copy.deepcopy(original(sid_arg))
                v["feedback_delivery"]["preview"]["document_text"] = (
                    "Teacher-edited document wording.")
                v["feedback_delivery"]["preview_sha256"] = (
                    "edited-preview-hash")
                return v
            self.service.teacher_session = edited_doc
            b.wait(f"{release}.textContent.includes('Teacher-edited "
                   "document wording')")
            self.assertNotIn("End practice", b.js(
                f"[...{nav}.querySelectorAll('.feedback-nav-btn')]"
                ".map(n => n.textContent)"))
            self.assertIsNone(b.js(
                f"{pane}.querySelector('.feedback-preview "
                ".practice-activity')"))
            self.service.teacher_session = original
            b.wait(f"Boolean({pane}.querySelector('.feedback-preview "
                   ".practice-activity'))")
            # Read-only scenario view: saved label, no release controls,
            # no approval-required eyebrow, no duplicate control ids.
            b.js("document.querySelector('[data-workspace=library]').click()")
            b.wait("document.querySelector('.scenario-row')")
            b.js("document.querySelector('.scenario-row').click()")
            b.wait("!document.querySelector('#scenario-ws-detail').hidden")
            b.js("document.querySelector('#scenario-ws-tabs "
                 "[data-tab=feedback]').click()")
            spane = "document.querySelector('#scenario-tab-feedback')"
            b.wait(f"Boolean({spane}.querySelector('.saved-end-feedback'))")
            self.assertIn("Saved end feedback", b.js(
                f"[...{spane}.querySelectorAll('.feedback-nav-btn')]"
                ".map(n => n.textContent)"))
            self.assertNotIn("Review & release", b.js(
                f"[...{spane}.querySelectorAll('.feedback-nav-btn')]"
                ".map(n => n.textContent)"))
            self.assertTrue(b.js(
                f"{spane}.textContent.includes('Retained feedback · "
                "read-only')"))
            self.assertFalse(b.js(
                f"{spane}.textContent.includes('educator approval "
                "required')"))
            self.assertTrue(b.js(
                f"{spane}.querySelector('.feedback-status-strip')"
                ".textContent.includes('Saved read-only feedback')"))
            self.assertIsNone(b.js(
                f"{spane}.querySelector('.review-release-heading')"))
            self.assertEqual(b.js(
                "document.querySelectorAll('#release-reviewer').length"),
                1)
            self.assertEqual(b.js(
                "document.querySelectorAll('#scenario-release-reviewer')"
                ".length"), 0)
            b.viewport(390, 844)
            b.js(f"{pane}.scrollIntoView({{block:'start'}})")
            self.assert_no_overflow()
            b.js("document.querySelector('[data-workspace=live]').click()")
            b.wait(f"!{pane}.hidden")
            b.js(f"{pane}.scrollIntoView({{block:'start'}})")
            self.assert_no_overflow()
            b.screenshot("feedback-top-released-mobile.png")
            b.js(f"{pane}.querySelector('.review-release-heading')"
                 ".scrollIntoView({block:'start'})")
            self.assert_no_overflow()
            b.screenshot("feedback-end-released-mobile.png")
            b.viewport(1440, 1100)
            self.assertEqual(b.errors, [])

    def test_worked_review_provider_notice_hidden(self):
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        examples = [
            {"source": "incorrect_assessment_response",
             "explanation_source": "provider",
             "explanation_input": "question_and_recorded_answers_v1",
             "prompt": "What is 10 percent of 50?",
             "options": ["5", "10", "15"],
             "selected_answer": "10",
             "correct_answer": "5",
             "steps": ["Recorded step one.", "Recorded step two."],
             "explanation": "Provider explanation wording."},
            {"source": "incorrect_assessment_response",
             "explanation_source": "provider",
             "prompt": "What is 10 percent of 50?",
             "options": ["5", "10", "15"],
             "selected_answer": "10",
             "correct_answer": "5",
             "steps": ["Recorded step one."],
             "explanation": "Provider explanation wording."},
            {"source": "incorrect_assessment_response",
             "explanation_source": "provider",
             "teacher_edited": True,
             "prompt": "What is 10 percent of 50?",
             "options": ["5", "10", "15"],
             "selected_answer": "10",
             "correct_answer": "5",
             "steps": ["Recorded step one."],
             "explanation": "Teacher-edited wording."},
        ]
        rendered = b.js(
            json.dumps(examples) + ".map((example) => {"
            "  const host = document.createElement('div');"
            "  renderStructuredFeedback(host, "
            "    {review_examples: [example]});"
            "  return host.textContent;})")
        for index, text in enumerate(rendered):
            self.assertNotIn("AI-drafted", text)
            self.assertNotIn("not independently verified", text)
            self.assertNotIn("Teacher review required", text)
            self.assertNotIn("review before release", text)
            self.assertIn("What is 10 percent of 50?", text)
            self.assertIn("Recorded step one.", text)
            self.assertIn("Correct answer:", text)
        self.assertIn("Teacher-edited worked review", rendered[2])
        self.assertEqual(b.errors, [])

    def test_feedback_default_notices_hidden(self):
        hidden_review = (
            "This revisits an answer from this assessment. A selected "
            "option does not establish why it was chosen. Explain the "
            "steps to your teacher before separate practice.")
        hidden_practice = (
            "Synthetic demo activity only. This selection does not "
            "establish mastery or a validated optimal learning "
            "difficulty.")
        custom_notice = "Teacher note: check this wording together."
        custom_practice = "Teacher note: revisit this practice."
        unavailable = (
            "A checked worked solution is unavailable. Ask your "
            "teacher to review this assessment question; no "
            "replacement example has been invented.")
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        payload = {
            "worked_review_notice": hidden_review,
            "review_examples": [
                {"source": "incorrect_assessment_response",
                 "notice": hidden_review,
                 "prompt": "Question one?",
                 "options": ["a", "b"],
                 "selected_answer": "b",
                 "correct_answer": "a",
                 "steps": ["Step one."]},
                {"source": "incorrect_assessment_response",
                 "notice": hidden_review,
                 "prompt": "Question two?",
                 "options": ["a", "b"],
                 "selected_answer": "b",
                 "correct_answer": "a",
                 "steps": ["Step two."]},
                {"source": "incorrect_assessment_response",
                 "notice": custom_notice,
                 "prompt": "Question three?",
                 "options": ["a", "b"],
                 "selected_answer": "b",
                 "correct_answer": "a",
                 "steps": ["Step three."]},
                {"source": "incorrect_assessment_response",
                 "notice": custom_notice,
                 "prompt": "Question four?",
                 "options": ["a", "b"],
                 "selected_answer": "b",
                 "correct_answer": "a",
                 "steps": ["Step four."]},
                {"prompt": "Question five?",
                 "notice": unavailable}],
            "practice_activities": [
                {"status": "selected",
                 "notice": hidden_practice,
                 "topic_label": "sA",
                 "instruction": "Solve it on paper.",
                 "question": {"skill_name": "sA",
                              "concept_name": "sA",
                              "text": "Practice one?",
                              "options": ["x", "y"]}},
                {"status": "selected",
                 "notice": custom_practice,
                 "topic_label": "sB",
                 "instruction": "Solve it aloud.",
                 "question": {"skill_name": "sB",
                              "concept_name": "sB",
                              "text": "Practice two?",
                              "options": ["x", "y"]}},
                {"status": "selected",
                 "notice": custom_practice,
                 "topic_label": "sB",
                 "instruction": "Solve it aloud.",
                 "question": {"skill_name": "sB",
                              "concept_name": "sB",
                              "text": "Practice three?",
                              "options": ["x", "y"]}}]}
        result = b.js(
            "const payload = " + json.dumps(payload) + ";"
            "const before = JSON.stringify(payload);"
            "const host = document.createElement('div');"
            "renderStructuredFeedback(host, payload);"
            "const practiceHost = document.createElement('div');"
            "renderPracticeCard(practiceHost, "
            + json.dumps({"status": "selected",
                          "notice": hidden_practice,
                          "topic_label": "sC",
                          "instruction": "Hidden notice card.",
                          "question": {"skill_name": "sC",
                                       "concept_name": "sC",
                                       "text": "Practice four?",
                                       "options": ["x", "y"]}})
            + ", 0, true, false);"
            "const customHost = document.createElement('div');"
            "renderPracticeCard(customHost, "
            + json.dumps({"status": "selected",
                          "notice": custom_notice,
                          "topic_label": "sC",
                          "instruction": "Custom notice card.",
                          "question": {"skill_name": "sC",
                                       "concept_name": "sC",
                                       "text": "Practice five?",
                                       "options": ["x", "y"]}})
            + ", 0, true, false);"
            "return {main: host.textContent,"
            " practice: practiceHost.textContent,"
            " custom: customHost.textContent,"
            " unchanged: JSON.stringify(payload) === before};")
        for text in (result["main"], result["practice"],
                     result["custom"]):
            self.assertNotIn(hidden_review, text)
            self.assertNotIn(hidden_practice, text)
        main = result["main"]
        self.assertIn("Question one?", main)
        self.assertIn("Step two.", main)
        self.assertIn("Solve it on paper.", main)
        self.assertIn(unavailable, main)
        self.assertEqual(main.count(custom_notice), 1)
        self.assertEqual(main.count(custom_practice), 1)
        self.assertNotIn(hidden_practice, result["practice"])
        self.assertIn("Practice four?", result["practice"])
        self.assertIn(custom_notice, result["custom"])
        self.assertTrue(result["unchanged"])
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
            b.wait(f"{summary}.includes('Educator workspace')")
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
            self.assertFalse(b.js(f"{card}.textContent.includes('does not establish mastery')"))
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
            self.assertIn("Pool practice prompt 1?", card_text_teacher)
            self.assertIn("Browser worked prompt?",
                          example_text_teacher)
            self.navigate(result["student_url"])
            b.wait(f"{summary}.includes('Educator workspace')")
            released_text = b.js(summary)
            self.assertIn("questions correctly", released_text)
            for needle in ("Approved with practice.",
                           "Pool practice prompt 1?",
                           "Browser worked prompt?",
                           "practice activity",
                           "does not practise the review focus",
                           "pool_q1", "p_correct", "answer_index"):
                self.assertNotIn(needle, released_text)
            self.assertFalse(b.js(
                "Boolean(document.querySelector('#end-summary "
                ".practice-activity, #end-summary .review-example'))"))
            b.screenshot("student-released-feedback-practice.png")
            self.navigate(result["student_url"])
            b.wait(f"{summary}.includes('Educator workspace')")
            self.assertNotIn("Pool practice prompt 1?",
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

        class GroundedMultiGenerator(MultiGenerator):
            # The grounded multi request requires provider
            # worked_reviews; the plain fake's two-field reply is a
            # rejection (invalid_generation), not a narrative.
            def generate(self, payload):
                reply = super().generate(payload)
                reply["worked_reviews"] = fixture_reviews(payload)
                return reply

        generator = GroundedMultiGenerator()
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
            self.assertTrue(teacher_fragments)
            go(result["student_url"])
            summary = "document.querySelector('#end-summary').textContent"
            b.wait(f"{summary}.includes('Educator workspace')")
            student_fragments = b.js(
                "[...document.querySelectorAll('#end-summary "
                ".practice-activity, #end-summary .review-example, "
                "#end-summary .feedback-section')]"
                ".map((n) => n.textContent)")
            self.assertEqual(student_fragments, [])
            blob = b.js(summary)
            for needle in ("Pool practice prompt", "Browser worked prompt",
                           "pool_q", "p_correct", "answer_index",
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
        waiting = ("document.querySelector('#tab-feedback "
                   ".end-feedback-waiting')")
        editing = ("document.querySelector('#tab-feedback "
                   "details[data-disclosure-key=feedback-editing]')")
        b.wait(f"Boolean({strip})")
        b.wait(f"Boolean({waiting})")
        self.assertTrue(b.js(f"{strip}.textContent.includes('Processing')"))
        self.assertEqual(b.js(f"{waiting}.getAttribute('role')"),
                         "status")
        self.assertEqual(b.js(f"{waiting}.getAttribute('aria-live')"),
                         "polite")
        self.assertEqual(b.js(f"{waiting}.getAttribute('aria-busy')"),
                         "true")
        self.assertTrue(b.js(
            f"{waiting}.textContent.includes('preparing the end "
            "feedback and practice')"))
        self.assertFalse(b.js(
            f"{strip}.textContent.includes('Hosted feedback is still "
            "processing')"))
        self.assertFalse(b.js(
            f"{strip}.textContent.includes('Wording source')"))
        self.assertFalse(b.js(
            "Boolean(document.querySelector('#tab-feedback "
            ".feedback-preview'))"))
        self.assertFalse(b.js(f"Boolean({editing})"))
        self.assertFalse(b.js(
            "Boolean(document.querySelector('#tab-feedback "
            ".release-card button.primary'))"))
        self.assertTrue(b.js(
            "Boolean(document.querySelector('#tab-feedback "
            ".end-deterministic-comparison'))"))
        research_dd = ("document.querySelector('#tab-feedback "
                       "details[data-disclosure-key=feedback-research]')")
        self.assertTrue(b.js(f"Boolean({research_dd})"))
        self.assertFalse(b.js(f"{research_dd}.open"))
        summary = "document.querySelector('#end-summary').textContent"
        self.navigate(result["student_url"])
        b.wait(f"{summary}.includes('Educator workspace')")
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
        b.wait("Boolean(document.querySelector('#tab-feedback "
               ".feedback-preview'))")
        b.wait(f"Boolean({editing})")
        self.assertFalse(b.js(f"Boolean({waiting})"))
        self.assertTrue(b.js(
            "Boolean(document.querySelector('#tab-feedback "
            ".release-card button.primary'))"))

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
        b.js(f"{editing}.open = true")
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

    def test_end_feedback_waiting_states_hide_current_wording(self):
        # Each active provider stage shows the same accessible
        # Processing panel as the midpoint and hides baseline/edited
        # wording, choices, editor and approval; terminal states
        # resume the normal rendering.
        from tests.test_demo_service import FakeGenerator
        b = self.browser
        self.service.provider_mode = "hosted"
        self.service.generator = FakeGenerator()
        result = self.service.simulate("alternating", 5)
        sid = result["session_id"]
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]["status"]
            == "ready"))
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('#session-list button')")
        b.js("document.querySelector('#session-list button').click()")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]').click()")
        release = "document.querySelector('#tab-feedback .release-card')"
        strip = ("document.querySelector('#tab-feedback "
                 ".release-card .release-status')")
        waiting = ("document.querySelector('#tab-feedback "
                   ".end-feedback-waiting')")
        preview = ("document.querySelector('#tab-feedback "
                   ".feedback-preview')")
        editing = ("document.querySelector('#tab-feedback "
                   "details[data-disclosure-key=feedback-editing]')")
        approve = ("document.querySelector('#tab-feedback "
                   ".release-card button.primary')")
        compare = ("document.querySelector('#tab-feedback "
                   ".end-deterministic-comparison')")
        gen_value = (
            "[...document.querySelectorAll('#tab-feedback "
            ".release-status .kv')].find((n) => n.querySelector('.k')"
            " && n.querySelector('.k').textContent === 'Feedback "
            "generation').querySelector('.v').textContent")
        b.wait(f"Boolean({release}) && Boolean({preview})")
        # Ready: normal rendering, no waiting panel.
        self.assertFalse(b.js(f"Boolean({waiting})"))
        self.assertTrue(b.js(f"Boolean({editing})"))
        self.assertTrue(b.js(f"Boolean({approve})"))

        original = self.service.teacher_session

        # A selected practice keeps its real terminal status label.
        def selected_practice(session_id):
            v = original(session_id)
            delivery = v["feedback_delivery"]
            if (delivery.get("preview") or {}).get("practice") \
                    is not None:
                delivery["preview"]["practice"]["status"] = "selected"
            else:
                delivery["practice_status"] = "selected"
            return v

        def held(status):
            def view(session_id):
                v = selected_practice(session_id)
                v["provider_job"]["status"] = status
                return v
            return view

        try:
            self.service.teacher_session = selected_practice
            b.wait(f"{strip}.textContent.includes('Selected')",
                   timeout=30)
            # Count session-detail polls so held states are proved to
            # survive repeated polling (no transport retry claimed).
            b.js(
                "window.__origFetch = window.fetch;"
                "window.__detailPolls = 0;"
                "window.fetch = function(url, opts) {"
                "  if (typeof url === 'string' && url.indexOf("
                f"      '/api/teacher/sessions/{sid}') === 0)"
                "    window.__detailPolls++;"
                "  return window.__origFetch(url, opts);"
                "};")
            for status, label in (
                    ("waiting_for_practice",
                     "Waiting for practice selection"),
                    ("waiting_for_diagnostics",
                     "Waiting for model diagnostics"),
                    ("pending", "Processing")):
                b.js("window.__detailPolls = 0")
                self.service.teacher_session = held(status)
                b.wait(f"{gen_value} === {json.dumps(label)}",
                       timeout=30)
                b.wait(f"Boolean({waiting})")
                self.assertEqual(
                    b.js(f"{waiting}.getAttribute('role')"), "status")
                self.assertEqual(
                    b.js(f"{waiting}.getAttribute('aria-live')"),
                    "polite")
                self.assertEqual(
                    b.js(f"{waiting}.getAttribute('aria-busy')"),
                    "true")
                self.assertTrue(b.js(
                    f"{waiting}.textContent.includes('Aitta is "
                    "preparing the end feedback and practice')"))
                self.assertFalse(b.js(f"Boolean({preview})"))
                self.assertFalse(b.js(f"Boolean({editing})"))
                self.assertFalse(b.js(f"Boolean({approve})"))
                self.assertFalse(b.js(
                    f"{strip}.textContent.includes('Wording source')"))
                self.assertFalse(b.js(
                    f"{strip}.textContent.includes('Current "
                    "presentation')"))
                # Practice selection keeps its real terminal label.
                self.assertTrue(b.js(
                    f"{strip}.textContent.includes('Selected')"))
                # The deterministic baseline stays available as an
                # explicitly labelled reference, never as current.
                self.assertTrue(b.js(f"Boolean({compare})"))
                self.assertTrue(b.js(
                    f"{compare}.textContent.includes('a reference')"))
                # The stage survives repeated identical polls.
                b.wait("window.__detailPolls >= 2", timeout=30)
                self.assertEqual(b.js(gen_value), label)
                self.assertTrue(b.js(f"Boolean({waiting})"))
            # Held pending on a narrow viewport: no overflow.
            b.viewport(390, 844)
            b.wait(f"Boolean({waiting})", timeout=30)
            self.assert_no_overflow()
            b.screenshot("end-feedback-waiting-pending-390.png")
            b.viewport(1440, 1100)
            # A terminal state is not held in Processing.
            self.service.teacher_session = held("unavailable")
            b.wait(f"{strip}.textContent.includes('Unavailable')",
                   timeout=30)
            self.assertFalse(b.js(f"Boolean({waiting})"))
            self.assertTrue(b.js(f"Boolean({preview})"))
        finally:
            self.service.teacher_session = original
            b.js("if (window.__origFetch) {"
                 "  window.fetch = window.__origFetch; }")
        b.wait(f"Boolean({preview}) && Boolean({editing})", timeout=30)
        self.assertFalse(b.js(f"Boolean({waiting})"))
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
            self.assertIn("Simplify: −4x + 3", preview_text)
            self.navigate(result["student_url"])
            summary = "document.querySelector('#end-summary')"
            b.wait(f"{summary}.textContent.includes('Educator workspace')")
            student_text = b.js(
                "[...document.querySelectorAll('#end-summary > *')]"
                ".slice(1).map((n) => n.textContent).join('')")
            self.assertNotIn(preview_text, student_text)
            released = b.js(f"{summary}.textContent")
            for needle in ("Simplify: −4x + 3", "Combining like terms",
                           "Browser worked prompt?", "pool_q1",
                           "p_correct", "answer_index",
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
        b.wait(f"{summary}.textContent.includes('Educator workspace')")
        self.assertFalse(b.js(
            f"{summary}.textContent.includes({json.dumps(unique)})"))
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
        b.wait(f"{summary}.textContent.includes('Educator workspace')")
        self.assertTrue(b.js(
            f"{summary}.textContent.includes('20 of 40')"))
        self.assertFalse(b.js(
            f"{summary}.textContent.includes('Teacher rephrased summary for the class.')"))
        b.screenshot("student-released-edited.png")
        self.navigate(result["student_url"])
        b.wait(f"{summary}.textContent.includes('Educator workspace')")
        self.assertFalse(b.js(
            f"{summary}.textContent.includes('Teacher rephrased summary for the class.')"))
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
        b.wait(f"{summary}.textContent.includes('Educator workspace')")
        for needle in ("Teacher step <b>one</b>.",
                       "Teacher practice note.",
                       "Explain the edited reasoning.",
                       "Teacher heading",
                       "Edited first prompt text?",
                       "Simplify: 3x + 2x",
                       "Teacher-edited worked review",
                       "verify before release"):
            self.assertFalse(b.js(
                f"{summary}.textContent.includes({json.dumps(needle)})"),
                needle)
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
        b.wait(f"{summary}.textContent.includes('Educator workspace')")
        for needle in ("Teacher practice question?",
                       "Teacher instruction text.", "Teacher option A",
                       "Teacher practice status note.",
                       "Teacher relationship note.",
                       "Teacher-edited practice",
                       "verify before release"):
            self.assertFalse(b.js(
                f"{summary}.textContent.includes({json.dumps(needle)})"),
                needle)
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
            b.wait("document.querySelector('#end-summary').textContent.includes('Educator workspace')")
            sid = self.service.list_sessions()[0]["session_id"]
            view = self.service.teacher_session(sid)
            self.service.release_feedback(sid, {
                "message_sha256":
                    view["feedback_delivery"]["preview_sha256"]})
            self.assertTrue(b.js(
                "document.querySelector('#end-summary').textContent"
                ".includes('20 of 40')"))
            self.assertFalse(b.js(
                "document.querySelector('#end-summary').textContent"
                ".includes('Assessment summary')"))
            b.js("document.querySelector('#new-session').click(); document.querySelector('#display-language').value='en'")
            b.wait("!document.querySelector('#start-panel').hidden")
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value={json.dumps(self.pin)}; document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('#session-list button')")
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

    def test_completed_replay_refreshes_scoped_worked_example(self):
        # A completed replay's aggregate row never changes again, but a
        # teacher-triggered scoped worked-example job still mutates the
        # live session detail. Natural polling must keep reloading the
        # displayed case so the pending job resolves to each terminal
        # state without a manual refresh or re-selection.
        self.service.close(wait=True)
        fixture_root = self.root / "scoped_fixture"
        fixture_root.mkdir()
        bank = make_grounded_bank()
        source, report, _ = replay_fixture(
            fixture_root, bank, make_grounded_taxonomy(bank), count=4)
        gate = threading.Event()
        calls = []

        class Scoped:
            execution = None
            last_rejected_draft = None
            fail = False

            def generate(self, card):
                calls.append(card["question_id"])
                if not gate.wait(30):
                    raise RuntimeError("test gate timed out")
                if self.fail:
                    raise RuntimeError("test provider failure")
                return {"question_id": card["question_id"],
                        "steps": ["Scoped replay explanation."],
                        "reasoning_question": "Explain this checked step?"}

        scoped = Scoped()
        self.service = DemoService(
            root=self.root / "scoped_data", bank=bank,
            taxonomy=make_grounded_taxonomy(bank),
            diagnostics=fake_diagnostics(bank),
            replay_report=report, replay_source=source,
            provider_mode="hosted", generator=FullStubGenerator(),
            worked_example_generator=scoped)
        self.addCleanup(self.service.close, wait=True)
        # Registered after close so it runs first and releases any
        # parked adapter call before the service shuts down.
        self.addCleanup(gate.set)
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
        b.wait(f"document.querySelector('.run-record[data-run-id=\"{run['id']}\"] > summary')"
               ".textContent.includes('Complete')")
        # Let one natural poll settle latestRunPayload on the completed
        # list before the card is selected, so the polling path under
        # test (unchanged run JSON) is the only one that can refresh
        # the displayed detail afterwards.
        b.js("new Promise((resolve) => setTimeout(resolve, 2200))")

        case = self.service.replays.get(run["id"])["cases"][0]
        sid = case["session_id"]
        view = self.service.replays.result(run["id"], case["scenario_id"])
        delivery = view["feedback_delivery"]
        choice = next(
            c for c in delivery["worked_example_choices"]
            if any(cand["question_id"] != c["selected_question_id"]
                   and cand.get("checked_solution_available")
                   for cand in c["candidates"]))
        target = next(
            c for c in choice["candidates"]
            if c["question_id"] != choice["selected_question_id"]
            and c.get("checked_solution_available"))

        b.js(f"document.querySelector('#scenario-version').value={json.dumps(run['id'])};"
             "document.querySelector('#scenario-version').dispatchEvent(new Event('change'))")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        ws = "#scenario-ws-detail"
        b.js(f"document.querySelector('{ws} #scenario-ws-tabs "
             "[data-tab=feedback]').click()")
        cid = json.dumps(choice["candidate_id"])
        sel = (f"document.querySelector('{ws} "
               f".worked-example-select[data-candidate-id={cid}]')")
        apply_btn = (f"{sel}.closest('fieldset')"
                     ".querySelector('.worked-example-apply')")
        b.wait(sel, timeout=30)

        def apply_selection(question_id):
            b.js(f"{sel}.value = {json.dumps(question_id)}; "
                 f"{sel}.dispatchEvent(new Event('change'))")
            b.wait(f"{apply_btn} && !{apply_btn}.disabled", timeout=15)
            b.js(f"{apply_btn}.click()")
            b.wait(f"document.querySelector('{ws} "
                   ".end-feedback-waiting')", timeout=30)
            self.assertFalse(b.js(
                f"Boolean(document.querySelector('{ws} "
                ".feedback-preview'))"))
            self.assertFalse(b.js(
                f"Boolean([...document.querySelectorAll('{ws} "
                "button.primary')].find((n) => "
                "n.textContent.startsWith('Approve')))"))

        def example_job():
            return self.service._load_meta(sid)["checkpoints"]["end"][
                "worked_example_job"]

        def release_status():
            return b.js(f"document.querySelector('{ws} .release-status')"
                        " && "
                        f"document.querySelector('{ws} .release-status')"
                        ".textContent") or ""

        # Pending: the run list cannot change again, so only a reload of
        # the live detail can resolve the busy state.
        apply_selection(target["question_id"])
        saved_list = copy.deepcopy(self.service.replays.list())
        gate.set()
        self.assertTrue(wait_for(
            lambda: example_job()["status"] == "ready", timeout=30),
            "scoped worked-example job did not reach ready")
        b.wait(f"document.querySelector('{ws}').textContent"
               ".includes('Scoped replay explanation.')", timeout=30)
        self.assertFalse(b.js(
            f"Boolean(document.querySelector('{ws} "
            ".end-feedback-waiting'))"))
        self.assertIn("Worked example generation", release_status())
        self.assertIn("Ready", release_status())
        b.wait(f"{sel} && !{sel}.closest('fieldset').disabled",
               timeout=30)
        choice_text = b.js(
            f"document.querySelector('{ws} .worked-example-choice')"
            ".textContent")
        self.assertIn("explanation was regenerated", choice_text)
        self.assertNotIn("being generated", choice_text)
        self.assertEqual(saved_list, self.service.replays.list())
        self.assertEqual([target["question_id"]], calls)
        self.assertEqual(b.errors, [])

        # Fallback: a failing adapter publishes the checked-steps warning
        # through the same natural refresh, still without a manual
        # refresh, re-selection, or any change to the run list.
        gate.clear()
        scoped.fail = True
        another = next(
            c for c in choice["candidates"]
            if c["question_id"] != target["question_id"]
            and c.get("checked_solution_available"))
        apply_selection(another["question_id"])
        saved_list = copy.deepcopy(self.service.replays.list())
        gate.set()
        self.assertTrue(wait_for(
            lambda: "application-checked steps because AI generation "
                    "did not complete" in release_status(), timeout=30),
            "fallback warning did not appear through natural polling")
        self.assertFalse(b.js(
            f"Boolean(document.querySelector('{ws} "
            ".end-feedback-waiting'))"))
        self.assertEqual("fallback", example_job()["status"])
        self.assertEqual(saved_list, self.service.replays.list())
        self.assertEqual([target["question_id"], another["question_id"]],
                         calls)
        self.assertEqual(b.errors, [])

    def test_replay_progress_refresh_and_stage_notice(self):
        # Question-first grounded sessions now wait for terminal end
        # diagnostics before the provider call, like worked-first: a
        # replay whose diagnostics are still running shows the waiting
        # stage notice, keeps refreshing while the run record itself
        # is unchanged, and transitions to complete automatically once
        # diagnostics finish.
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
            # The admission snapshot's session_id can still be None; the
            # worker fills it in later, so always read the live record.
            current = self.service.replays.get(run["id"])
            sid = current["cases"][0]["session_id"]
            if not sid:
                return None
            try:
                return self.service._load_meta(sid)["provider_job"]["status"]
            except FileNotFoundError:
                return None

        detail = "document.querySelector('#scenario-ws-detail')"
        notice = ("document.querySelector("
                  "'#scenario-detail > p[role=\"status\"]')")

        expected_waiting = (
            "Replay result · Question-first · Running. Original results "
            "remain unchanged. Waiting for model diagnostics before "
            "Aitta can draft the feedback. This replay is not complete.")
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

        # --- Part A: provider waits for end diagnostics ---
        gen_gate.set()
        run_a = start_replay()
        self.assertTrue(wait_for(
            lambda: provider_status(run_a) == "waiting_for_diagnostics"),
            "provider job never entered waiting_for_diagnostics")
        stale_run = copy.deepcopy(run_a)
        stale_run["cases"][0]["session_id"] = None
        self.assertEqual(provider_status(stale_run),
                         "waiting_for_diagnostics")
        self.assertEqual(
            self.service.replays.get(run_a["id"])["status"], "running")
        b.js(f"document.querySelector('.run-record[data-run-id=\"{run_a['id']}\"] .run-case').click()")
        b.wait(f"{notice}.textContent.includes('Waiting for model "
               "diagnostics before Aitta can draft the feedback.')")
        assert_notice_stable_during_held_poll(expected_waiting)
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
        # Diagnostics terminal first so the provider job proceeds to
        # the held generator call, keeping the generic pending stage.
        diag_gate.set()
        gen_gate.clear()
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
        # Diagnostics are already terminal, so a finished provider job
        # completes the run directly; the running-row refresh path must
        # still pull the new detail automatically under an unchanged
        # run record JSON.
        self.assertTrue(wait_for(
            lambda: self.service.replays.get(run_b["id"])["status"]
            == "complete"),
            f"replay run {run_b['id']} did not complete")
        # The notice derives from the re-fetched response, so this wait
        # proves the case detail refreshed under unchanged run JSON.
        b.wait(f"{notice}.textContent.includes('Replay is complete')")
        b.wait(f"Boolean({detail}.querySelector('.feedback-preview'))")
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
               "diagnostics before Aitta can draft the feedback. "
               "This replay is not complete.')")
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
            "worked_first_assessment_research_context_v2")
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
            head = (f"[...document.querySelectorAll('#tab-graph "
                    ".topic-head')].find(n => n.querySelector"
                    "('.topic-name').textContent === "
                    f"englishSkillName({json.dumps(topic['skill_name'])}))")
            self.assertTrue(b.js(f"Boolean({head})"))
            expected_count = ("Not yet assessed" if topic["out_of"] == 0
                              else f"{topic['correct']}/{topic['out_of']} "
                                   "observed correct")
            self.assertEqual(b.js(
                f"{head}.querySelector('.topic-count').textContent"),
                expected_count)
        # Focus rows show the observed counts plus exactly one Review
        # focus badge — the old plain-text suffix is gone for good.
        for leaf in [s for t in end["graph"]["topics"] for s in t["subtopics"]
                     if s["subtopic_id"] in focus_subs]:
            li = ("[...document.querySelectorAll('#tab-graph "
                  ".skill-topics li')].find(n => n.querySelector("
                  "'.subtopic-name').textContent === "
                  f"{json.dumps(leaf['subtopic_name'])})")
            self.assertTrue(b.js(f"Boolean({li})"))
            if leaf["out_of"] == 0:
                expected_items = ["Not yet assessed"]
            else:
                expected_items = [f"{leaf['correct']} correct",
                                  f"{leaf['incorrect']} incorrect",
                                  f"of {leaf['out_of']}"]
            self.assertEqual(b.js(
                f"[...{li}.querySelectorAll('.topic-count-item')]"
                ".map(n => n.textContent)"), expected_items)
            self.assertFalse(b.js(
                f"{li}.textContent.includes('selected review focus')"))
            self.assertEqual(b.js(
                f"{li}.querySelectorAll('.skill-badge').length"), 1)
            self.assertEqual(b.js(
                f"{li}.querySelector('.skill-badge').textContent"),
                "Review focus")
        non_focus = next(s for t in end["graph"]["topics"]
                         for s in t["subtopics"]
                         if s["subtopic_id"] not in focus_subs)
        nli = ("[...document.querySelectorAll('#tab-graph "
               ".skill-topics li')].find(n => n.querySelector("
               "'.subtopic-name').textContent === "
               f"{json.dumps(non_focus['subtopic_name'])})")
        self.assertEqual(b.js(
            f"{nli}.querySelectorAll('.skill-badge').length"), 0)
        self.assertTrue(b.js(
            f"{nli}.textContent.includes('observed')"
            f" || {nli}.textContent.includes('correct')"))
        b.js("document.querySelector('#tab-graph .skill-topics')"
             ".scrollIntoView({block: 'center'})")
        b.screenshot("skill-map-topics-desktop.png")

        # A baseline shown while the provider job is still pending is
        # a provisional focus — the badge says so.
        original = self.service.teacher_session
        self.addCleanup(
            setattr, self.service, "teacher_session", original)

        def provisional(sid_arg):
            v = copy.deepcopy(original(sid_arg))
            v["checkpoints"]["end"].pop("student_review", None)
            v["provider_job"]["status"] = "pending"
            return v
        self.service.teacher_session = provisional
        b.wait("document.querySelector('#tab-graph .skill-topics')"
               ".textContent.includes('Review focus (provisional)')")
        self.assertFalse(b.js(
            "document.querySelector('#tab-graph .skill-topics')"
            ".textContent.includes('selected review focus')"))
        self.service.teacher_session = original
        b.wait("document.querySelector('#tab-graph .skill-topics')"
               ".textContent.includes('Review focus')"
               " && !document.querySelector('#tab-graph .skill-topics')"
               ".textContent.includes('provisional')")

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

        # Summaries are structured: number, topic path and subtopic are
        # separate spans, with explicit result pills.
        self.assertEqual(b.js(
            f"{ex_sel}.querySelector('summary .task-num').textContent"),
            f"#{ex_row['position']}")
        self.assertEqual(b.js(
            f"{ex_sel}.querySelector('summary .task-topic').textContent"),
            b.js(f"englishSkillName({json.dumps(ex_ctx['skill_name'])})"))
        self.assertEqual(b.js(
            f"{ex_sel}.querySelector('summary .task-subtopic').textContent"),
            ex_row["subtopic_name"])
        result_pill = ("Incorrect" if not ex_row["correct"] else "Correct")
        self.assertEqual(b.js(
            f"{ex_sel}.querySelector('summary .skill-task-result')"
            ".textContent"), result_pill)
        self.assertTrue(b.js(
            f"{ex_sel}.querySelector('summary .skill-task-result')"
            f".classList.contains('is-{result_pill.lower()}')"))

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
        # The expanded task separates the recorded answer into labelled
        # blocks ahead of the descriptive context and feedback role.
        self.assertEqual(b.js(
            f"[...{ex_sel}.querySelectorAll('.qrow-answer-label')]"
            ".map(n => n.textContent)"),
            ["Student answer", "Correct answer"])
        self.assertIn(
            b.js("englishOptionText("
                 f"{json.dumps(ex_row['options'][ex_row['selected_index']])})"),
            b.js(f"{ex_sel}.querySelectorAll('.qrow-answer-value')"
                 "[0].textContent"))
        self.assertIn(
            b.js("englishOptionText("
                 f"{json.dumps(ex_row['options'][ex_row['answer_index']])})"),
            b.js(f"{ex_sel}.querySelectorAll('.qrow-answer-value')"
                 "[1].textContent"))
        self.assertTrue(b.js(
            f"{ex_sel}.textContent.includes('Feedback role')"))
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
        expected_numbers = {e["question_id"]: i + 1
                            for i, e in enumerate(practices)}
        topic_counts = {}
        for e in practices:
            topic_counts.setdefault(e["skill_id"], []).append(e)
        pane = "document.querySelector('#tab-graph')"
        self.service.teacher_session = selected()
        open_graph()
        self.assertTrue(b.js(
            f"{pane}.textContent.includes('Numbers identify drafts in "
            "bank order; they do not indicate difficulty or a "
            "recommended sequence.')"))
        # Default collapsed view then the open practice module.
        b.js("document.querySelector('#tab-graph')"
             ".scrollIntoView({block: 'start'})")
        b.screenshot("skill-map-default-desktop.png")
        b.viewport(390, 844)
        self.assert_no_overflow()
        b.js("document.querySelector('#tab-graph')"
             ".scrollIntoView({block: 'start'})")
        b.screenshot("skill-map-default-mobile.png")
        b.js("document.querySelector('#tab-graph .skill-topics')"
             ".scrollIntoView({block: 'start'})")
        b.screenshot("skill-map-topics-mobile.png")
        b.viewport(1440, 1100)
        # Numbered cards in stable bank order, grouped by first-seen
        # topic, never renumbered by selection state.
        b.js("document.querySelector('#tab-graph "
             "details[data-disclosure-key=skill-map-practice]').open = true")
        cards = b.js(
            "[...document.querySelectorAll('#tab-graph "
            "details[data-disclosure-key^=\"skill-practice:\"]')]"
            ".map(n => ({key: n.dataset.disclosureKey, num: "
            "n.dataset.practiceNumber, label: n.querySelector("
            "'.skill-task-label').textContent}))")
        self.assertEqual(len(cards), 24)
        self.assertEqual(len({c["key"] for c in cards}), 24)
        self.assertEqual([int(c["num"]) for c in cards],
                         list(range(1, 25)))
        practice_by_qid = {e["question_id"]: e for e in practices}
        for c in cards:
            qid = c["key"].split(":", 1)[1]
            number = expected_numbers[qid]
            self.assertEqual(int(c["num"]), number)
            self.assertTrue(
                c["label"].startswith(f"Practice draft {number}"))
            self.assertIn(
                practice_by_qid[qid]["concept_label"], c["label"])
        groups = b.js(
            "[...document.querySelectorAll('#tab-graph .practice-topic')]"
            ".map(g => ({head: g.querySelector('h4').textContent, "
            "count: g.querySelectorAll('details[data-disclosure-key^="
            "\"skill-practice:\"]').length}))")
        self.assertEqual(len(groups), len(topic_counts))
        for g, exs in zip(groups, topic_counts.values()):
            self.assertEqual(g["count"], len(exs))
        chosen_group = (
            "[...document.querySelectorAll('#tab-graph .practice-topic')]"
            ".find(g => g.querySelector('details[data-disclosure-key="
            f"\"skill-practice:{chosen['question_id']}\"]'))")
        other_group = (
            "[...document.querySelectorAll('#tab-graph .practice-topic')]"
            ".find(g => g.querySelector('details[data-disclosure-key="
            f"\"skill-practice:{other_topic['question_id']}\"]'))")
        self.assertTrue(b.js(
            f"{chosen_group}.querySelector('h4').textContent"
            ".includes('1 selected practice')"))
        self.assertFalse(b.js(
            f"{other_group}.querySelector('h4').textContent"
            ".includes('selected practice')"))
        self.assertTrue(b.js(
            f"{ch}.classList.contains('is-selected')"))
        self.assertFalse(b.js(
            f"{st}.classList.contains('is-selected')"))
        # Keyboard activation on the focused summary toggles the
        # module and its open state survives the polling re-render.
        summary = ("document.querySelector('#tab-graph "
                   "details[data-disclosure-key=skill-map-practice] "
                   "summary')")
        b.js(f"{summary}.focus()")

        def press_enter():
            for ktype in ("keyDown", "keyUp"):
                b.command("Input.dispatchKeyEvent", {
                    "type": ktype, "key": "Enter", "code": "Enter",
                    "keyIdentifier": "Enter", "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                    **({"text": "\r", "unmodifiedText": "\r"}
                       if ktype == "keyDown" else {})})
        press_enter()
        self.assertFalse(b.js(
            "document.querySelector('#tab-graph "
            "details[data-disclosure-key=skill-map-practice]').open"))
        press_enter()
        self.assertTrue(b.js(
            "document.querySelector('#tab-graph "
            "details[data-disclosure-key=skill-map-practice]').open"))
        time.sleep(2.4)
        self.assertTrue(b.js(
            "document.querySelector('#tab-graph "
            "details[data-disclosure-key=skill-map-practice]').open"))
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
        b.js(f"{ch}.open = true")
        b.js(f"{ch}.scrollIntoView({{block: 'center'}})")
        self.assert_no_overflow()
        b.screenshot("skill-map-practice-selected-desktop.png")
        b.viewport(390, 844)
        b.js(f"{ch}.scrollIntoView({{block: 'center'}})")
        self.assert_no_overflow()
        self.assertTrue(b.js(
            f"{summary_text(ch)}.includes('Selected practice')"))
        # The expanded practice draft shows structured summary spans and
        # descriptive/selection context only — never a learner answer,
        # correctness, or a fake result.
        self.assertTrue(b.js(
            f"{ch}.querySelector('summary .task-num').textContent"
            ".startsWith('Practice draft')"))
        self.assertTrue(b.js(
            f"Boolean({ch}.querySelector('summary .task-topic'))"))
        self.assertTrue(b.js(
            f"Boolean({ch}.querySelector('summary .task-subtopic'))"))
        self.assertIsNone(b.js(f"{ch}.querySelector('.qrow-answers')"))
        self.assertIsNone(b.js(f"{ch}.querySelector('.qrow-answer')"))
        for fragment in ("Student answer", "Correct answer",
                         "Incorrect", "Not answered"):
            self.assertFalse(b.js(
                f"{ch}.textContent.includes({json.dumps(fragment)})"))
        self.assertTrue(b.js(f"{ch}.textContent.includes('Task requirement')"))
        self.assertTrue(b.js(f"{ch}.textContent.includes('Selection')"))
        self.assertTrue(b.js(f"{ch}.classList.contains('is-selected')"))
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
        # Bank-order numbering is stable across selection states.
        self.assertEqual(b.js(
            f"{ch}.querySelector('.skill-task-label .task-num')"
            ".textContent"),
            f"Practice draft {expected_numbers[chosen['question_id']]}")

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

    def test_diagnostics_panel_chart_inspector_and_states(self):
        # Redesigned Model diagnostics tab: editorial header, segmented
        # checkpoint picker, accessible before-answer chart with
        # inspector, answer-level table, and honest
        # pending/unavailable/invalid states.
        bank = make_bank()
        rows = responses(bank)
        for index in (4, 25):
            question = bank["questions"][index]
            rows[index] = dict(
                rows[index],
                selected_index=(question["answer_index"] + 1)
                % len(question["options"]))
        created = self.service.create_session()
        sid, token = (created["session_id"], created["student_token"])
        for row in rows:
            self.service.submit_response(sid, token, row)
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["checkpoints"]["end"]
            ["diagnostics_job"]["status"] == "ready"))
        probs = (self.service._load_meta(sid)["checkpoints"]["end"]
                 ["diagnostics"]["kt"]
                 ["p_correct_before_each_answer"])
        self.assertEqual(len(probs), 40)
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)}; "
             "document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('.session-row')")
        b.js("document.querySelector('.session-row').click()")
        b.wait("!document.querySelector('#ws-detail').hidden")
        b.js("document.querySelector('[data-tab=research]').click()")
        b.wait("document.querySelector('#tab-research "
               ".diagnostics-panel')")
        pane = "document.querySelector('#tab-research')"
        text = b.js(f"{pane}.textContent")
        for needle in ("Private research", "Model diagnostics",
                       "not a mastery score", "Fresh inference",
                       "Frozen KT",
                       "Fresh inference using the frozen model",
                       "Before-answer predictions",
                       "not a learning curve",
                       "How to read these diagnostics",
                       "predictively evaluated on real ViLLE data",
                       "simulated sessions are not additional "
                       "real-learner validation",
                       "Why still research-only"):
            self.assertIn(needle, text)
        # Segmented checkpoint picker: two real buttons, one pressed;
        # the active segment is visibly distinct (teal on white text).
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-research .cp-picker "
            ".cp-choice').length"), 2)
        self.assertTrue(b.js(
            "[...document.querySelectorAll('#tab-research .cp-picker "
            ".cp-choice')].find(n => n.getAttribute('aria-pressed') "
            "=== 'true').textContent.includes('All 40 answers')"))
        seg_styles = json.loads(b.js(
            "JSON.stringify([...document.querySelectorAll("
            "'#tab-research .cp-picker .cp-choice')].map((n) => {"
            " const c = getComputedStyle(n);"
            " return {bg: c.backgroundColor, color: c.color,"
            " pressed: n.getAttribute('aria-pressed')}; }))"))
        active = next(s for s in seg_styles if s["pressed"] == "true")
        inactive = next(s for s in seg_styles if s["pressed"] == "false")
        self.assertNotEqual(active["bg"], inactive["bg"])
        self.assertEqual(active["bg"], "rgb(18, 102, 94)")
        self.assertEqual(active["color"], "rgb(255, 255, 255)")
        # 40 interactive data points with honest aria labels.
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-research .kt-point')"
            ".length"), 40)
        aria = b.js(
            "document.querySelector('#tab-research .kt-point')"
            ".getAttribute('aria-label')")
        self.assertIn("Answer 1: predicted correctness "
                      f"{probs[0]:.3f}", aria)
        self.assertIn("observed Correct", aria)
        # Axis ticks include first and last positions plus the
        # second-half boundary on the 40-answer trace.
        ticks = b.js(
            "[...document.querySelectorAll('#tab-research "
            ".axis-label')].map(n => n.textContent)")
        for tick in ("1", "40", "Second half"):
            self.assertIn(tick, ticks)
        # Inspector starts on the first valid point; clicking an
        # incorrect answer selects it and reports the observed outcome.
        inspector = ("document.querySelector('#tab-research "
                     ".kt-inspector')")
        insp = b.js(f"{inspector}.textContent")
        self.assertIn("Answer 1", insp)
        self.assertIn(f"{probs[0]:.3f}", insp)
        b.js("document.querySelectorAll('#tab-research "
             ".kt-point')[4].dispatchEvent("
             "new MouseEvent('click', {bubbles: true}))")
        insp = b.js(f"{inspector}.textContent")
        self.assertIn("Answer 5", insp)
        self.assertIn("Incorrect", insp)
        self.assertIn(f"{probs[4]:.3f}", insp)
        self.assertIsNotNone(b.js(
            "document.querySelector('#tab-research "
            ".kt-point.selected')"))
        # The narrow inspector stacks compact labels over full-width
        # values — no wrapped digits or squeezed words.
        kv_style = json.loads(b.js(
            "(() => { const k = document.querySelector('#tab-research "
            ".kt-inspector .kv .k'); const v = document.querySelector("
            "'#tab-research .kt-inspector .kv.kt-prob .v');"
            " return JSON.stringify({minWidth: getComputedStyle(k)"
            ".minWidth, vHeight: v.getBoundingClientRect().height,"
            " vText: v.textContent}); })()"))
        self.assertEqual(kv_style["minWidth"], "0px")
        self.assertEqual(kv_style["vText"], f"{probs[4]:.3f}")
        self.assertLess(kv_style["vHeight"], 50)
        # Keyboard: focus selects, Enter/Space activates without error.
        b.js("document.querySelectorAll('#tab-research "
             ".kt-point')[2].focus()")
        self.assertIn("Answer 3", b.js(f"{inspector}.textContent"))
        b.js("document.querySelectorAll('#tab-research "
             ".kt-point')[2].dispatchEvent(new KeyboardEvent("
             "'keydown', {key: 'Enter', bubbles: true}))")
        self.assertIn("Answer 3", b.js(f"{inspector}.textContent"))
        # Whole-plot sweep: hovering anywhere inside the plot bounds
        # selects the nearest valid answer by position, whatever the
        # pointer height; the margins leave the selection unchanged.
        insp_h5 = ("document.querySelector('#tab-research .kt-inspector "
                   "h5').textContent")
        spacing = (900 - 2 * 52) / 39
        plot_bottom = 350 - 58
        b.js("document.querySelector('#tab-research .ktchart')"
             ".scrollIntoView({block: 'center'})")

        def svg_client(sx, sy):
            return json.loads(b.js(
                "(() => { const svg = document.querySelector("
                "'#tab-research .ktchart'); const pt = "
                "svg.createSVGPoint(); pt.x = " + repr(float(sx)) +
                "; pt.y = " + repr(float(sy)) + "; const r = "
                "pt.matrixTransform(svg.getScreenCTM());"
                " return JSON.stringify({x: r.x, y: r.y}); })()"))

        def mouse_move(sx, sy):
            c = svg_client(sx, sy)
            b.command("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": c["x"], "y": c["y"]})

        def mouse_click(sx, sy):
            c = svg_client(sx, sy)
            for mtype in ("mousePressed", "mouseReleased"):
                b.command("Input.dispatchMouseEvent", {
                    "type": mtype, "x": c["x"], "y": c["y"],
                    "button": "left", "clickCount": 1})
        mouse_move(52 + 8.2 * spacing, plot_bottom - 10)
        self.assertEqual(b.js(insp_h5), "Answer 9")
        self.assertEqual(b.js(
            "document.querySelector('#tab-research "
            ".kt-point.selected').dataset.index"), "8")
        # A different height at the same position keeps the bucket.
        mouse_move(52 + 8.2 * spacing, 35)
        self.assertEqual(b.js(insp_h5), "Answer 9")
        # Sweep to the ends; a background tap shares the same path.
        mouse_move(52 + 0.3 * spacing, plot_bottom - 20)
        self.assertEqual(b.js(insp_h5), "Answer 1")
        mouse_move(52 + 39 * spacing - 2, plot_bottom - 10)
        self.assertEqual(b.js(insp_h5), "Answer 40")
        mouse_click(52 + 12 * spacing, 60)
        self.assertEqual(b.js(insp_h5), "Answer 13")
        # Margins never move the selection.
        mouse_move(10, 40)
        mouse_move(52 + 20 * spacing, 350 - 10)
        self.assertEqual(b.js(insp_h5), "Answer 13")
        b.screenshot("diagnostics-hover-desktop.png")
        # The inspected answer survives the polling re-render —
        # exactly what the hover request needs while studying a point.
        time.sleep(2.4)
        self.assertEqual(b.js(insp_h5), "Answer 13")
        self.assertEqual(b.js(
            "document.querySelector('#tab-research "
            ".kt-point.selected').dataset.index"), "12")
        # Answer-level data table keeps every position.
        b.js("document.querySelector('#tab-research "
             "details[data-disclosure-key=answer-level-data]')"
             ".open = true")
        table_text = b.js(
            "document.querySelector('#tab-research .kt-table')"
            ".textContent")
        self.assertIn("Predicted correctness", table_text)
        self.assertIn("Incorrect", table_text)
        # Midpoint switch: 20 points, no second-half marker, pressed
        # state moves with the checkpoint — and each checkpoint keeps
        # its own remembered inspected answer.
        b.js("[...document.querySelectorAll('#tab-research .cp-picker "
             ".cp-choice')].find(n => n.textContent.includes("
             "'Midpoint')).click()")
        b.wait("document.querySelectorAll('#tab-research .kt-point')"
               ".length === 20")
        self.assertNotIn("Second half", b.js(f"{pane}.textContent"))
        self.assertTrue(b.js(
            "[...document.querySelectorAll('#tab-research .cp-picker "
            ".cp-choice')].find(n => n.textContent.includes("
            "'Midpoint')).getAttribute('aria-pressed') === 'true'"))
        self.assertEqual(b.js(insp_h5), "Answer 1")
        spacing20 = (900 - 2 * 52) / 19
        mouse_move(52 + 5.2 * spacing20, plot_bottom - 10)
        self.assertEqual(b.js(insp_h5), "Answer 6")
        b.js("[...document.querySelectorAll('#tab-research .cp-picker "
             ".cp-choice')].find(n => n.textContent.trim().startsWith("
             "'End')).click()")
        b.wait("document.querySelectorAll('#tab-research .kt-point')"
               ".length === 40")
        self.assertEqual(b.js(insp_h5), "Answer 13")
        # Revisiting the midpoint restores its own selection too.
        b.js("[...document.querySelectorAll('#tab-research .cp-picker "
             ".cp-choice')].find(n => n.textContent.includes("
             "'Midpoint')).click()")
        b.wait("document.querySelectorAll('#tab-research .kt-point')"
               ".length === 20")
        self.assertEqual(b.js(insp_h5), "Answer 6")
        b.js("[...document.querySelectorAll('#tab-research .cp-picker "
             ".cp-choice')].find(n => n.textContent.trim().startsWith("
             "'End')).click()")
        b.wait("document.querySelectorAll('#tab-research .kt-point')"
               ".length === 40")
        self.assertEqual(b.js(insp_h5), "Answer 13")
        # Mutated payload: one answer record without an observed
        # outcome reads as unavailable — never incorrect — and an
        # invalid probability yields no fabricated point.
        original = self.service.teacher_session

        def degraded(sid_arg):
            v = original(sid_arg)
            v = copy.deepcopy(v)
            v["question_responses"][9]["correct"] = None
            v["checkpoints"]["end"]["diagnostics"]["kt"][
                "p_correct_before_each_answer"][14] = "corrupt"
            return v
        self.service.teacher_session = degraded
        b.wait("[...document.querySelectorAll('#tab-research "
               ".kt-point')].some(n => n.getAttribute('aria-label')"
               ".includes('Answer 10:') && "
               "n.getAttribute('aria-label').includes("
               "'Answer unavailable'))")
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-research .kt-point')"
            ".length"), 39)
        b.js("[...document.querySelectorAll('#tab-research "
             ".kt-point')].find(n => n.getAttribute('aria-label')"
             ".includes('Answer 10:')).dispatchEvent("
             "new MouseEvent('click', {bubbles: true}))")
        insp = b.js(f"{inspector}.textContent")
        self.assertIn("Answer 10", insp)
        self.assertIn("Answer unavailable", insp)
        table_text = b.js(
            "document.querySelector('#tab-research .kt-table')"
            ".textContent")
        self.assertIn("Unavailable", table_text)
        # Sweeping over the corrupt position picks the nearest valid
        # neighbour — never the invalid one itself.
        mouse_move(52 + 14 * spacing, plot_bottom - 10)
        sel_idx = b.js("document.querySelector('#tab-research "
                       ".kt-point.selected').dataset.index")
        self.assertIn(sel_idx, ("13", "15"))
        self.assertEqual(b.js(insp_h5), f"Answer {int(sel_idx) + 1}")
        # An all-invalid ready trace renders the explicit empty state,
        # no fabricated points, and the raw trace disclosure stays.
        def all_invalid(sid_arg):
            v = copy.deepcopy(original(sid_arg))
            v["checkpoints"]["end"]["diagnostics"]["kt"][
                "p_correct_before_each_answer"] = ["corrupt"] * 40
            return v
        self.service.teacher_session = all_invalid
        b.wait(f"{pane}.textContent.includes('No prediction values "
               "available')")
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-research .kt-point')"
            ".length"), 0)
        self.assertIsNotNone(b.js(
            "document.querySelector('#tab-research "
            "details[data-disclosure-key=kt-trace]')"))
        # Saved (frozen replay) inference is labelled distinctly.
        def saved_mode(sid_arg):
            v = copy.deepcopy(original(sid_arg))
            v["checkpoints"]["end"]["diagnostics"]["mode"] = (
                "frozen_replay_no_model_rerun")
            return v
        self.service.teacher_session = saved_mode
        b.wait(f"{pane}.textContent.includes('Saved inference')")
        self.assertIn("Saved historical inference — not recomputed.",
                      b.js(f"{pane}.textContent"))
        self.assertNotIn("Fresh inference using",
                         b.js(f"{pane}.textContent"))
        # Pending and unavailable checkpoints get honest empty states —
        # not a fabricated chart.
        def pending_diag(sid_arg):
            v = copy.deepcopy(original(sid_arg))
            v["checkpoints"]["end"]["diagnostics_job"] = {
                "status": "pending"}
            v["checkpoints"]["end"]["diagnostics"] = {
                "status": "unavailable"}
            return v
        self.service.teacher_session = pending_diag
        b.wait("document.querySelector('#tab-research "
               ".diagnostics-empty[aria-live]')")
        pending_text = b.js(f"{pane}.textContent")
        self.assertIn("Diagnostics are still being prepared.",
                      pending_text)
        self.assertIn("Answers & scores", pending_text)
        self.assertIsNone(b.js(
            "document.querySelector('#tab-research .kt-point')"))

        def unavailable_diag(sid_arg):
            v = copy.deepcopy(original(sid_arg))
            v["checkpoints"]["end"]["diagnostics_job"] = {
                "status": "unavailable", "reason": "test_unavailable"}
            v["checkpoints"]["end"]["diagnostics"] = {
                "status": "unavailable"}
            return v
        self.service.teacher_session = unavailable_diag
        b.wait(f"{pane}.textContent.includes('not a zero probability')")
        self.assertIsNone(b.js(
            "document.querySelector('#tab-research .kt-point')"))
        self.service.teacher_session = original
        b.wait("document.querySelectorAll('#tab-research .kt-point')"
               ".length === 40")
        self.assert_no_overflow()
        # Restore the collapsed default before capturing the panel.
        b.js("document.querySelector('#tab-research "
             "details[data-disclosure-key=answer-level-data]')"
             ".open = false")

        def panel_shot(name):
            box = json.loads(b.js(
                "JSON.stringify((() => { const r = "
                "document.querySelector('#tab-research "
                ".diagnostics-panel').getBoundingClientRect();"
                " return {x: r.x + window.scrollX,"
                " y: r.y + window.scrollY, width: r.width,"
                " height: r.height}; })())"))
            data = b.command("Page.captureScreenshot", {
                "format": "png", "captureBeyondViewport": True,
                "clip": {**box, "scale": 1}})
            (Path(os.environ["BROWSER_SCREENSHOTS"]) / name).write_bytes(
                base64.b64decode(data["data"]))
        if os.environ.get("BROWSER_SCREENSHOTS"):
            panel_shot("diagnostics_panel_desktop.png")
        b.viewport(390, 844)
        self.assert_no_overflow()
        # The horizontally scrolled chart still maps pointer
        # coordinates through the screen CTM — both sweep and tap
        # select the visible answer.
        b.js("document.querySelector('#tab-research .ktchart')"
             ".scrollIntoView({block: 'center'})")
        b.js("document.querySelector('#tab-research .kt-scroll')"
             ".scrollLeft = 200")
        mouse_move(52 + 20 * spacing, plot_bottom - 10)
        self.assertEqual(b.js(insp_h5), "Answer 21")
        mouse_click(52 + 30 * spacing, 100)
        self.assertEqual(b.js(insp_h5), "Answer 31")
        # The polling re-render restores the same scroll position —
        # the teacher does not lose their place on the wide chart.
        time.sleep(2.4)
        self.assertEqual(b.js(
            "document.querySelector('#tab-research .kt-scroll')"
            ".scrollLeft"), 200)
        self.assertEqual(b.js(insp_h5), "Answer 31")
        # The open answer-level table scrolls horizontally rather than
        # breaking header words; the document still does not overflow.
        b.js("document.querySelector('#tab-research "
             "details[data-disclosure-key=answer-level-data]')"
             ".open = true")
        self.assertGreater(
            b.js("document.querySelector('#tab-research .kt-table')"
                 ".scrollWidth"),
            b.js("document.querySelector('#tab-research "
                 ".kt-table-scroll').clientWidth"))
        self.assert_no_overflow()
        if os.environ.get("BROWSER_SCREENSHOTS"):
            b.js("document.querySelector('#tab-research "
                 "details[data-disclosure-key=answer-level-data]')"
                 ".scrollIntoView({block: 'start'})")
            b.screenshot("diagnostics_panel_mobile_table.png")
        b.js("document.querySelector('#tab-research "
             "details[data-disclosure-key=answer-level-data]')"
             ".open = false")
        if os.environ.get("BROWSER_SCREENSHOTS"):
            panel_shot("diagnostics_panel_mobile.png")
            b.js("document.querySelector('#tab-research .kt-grid')"
                 ".scrollIntoView({block: 'start'})")
            b.screenshot("diagnostics_panel_mobile_grid.png")
        b.viewport(1440, 1100)
        # The shared scenario workspace renders the same panel
        # independently of the live session's mutations.
        b.js("document.querySelector('[data-workspace=library]')"
             ".click()")
        b.wait("document.querySelector('.scenario-row')")
        b.js("document.querySelector('.scenario-row').click()")
        b.wait("!document.querySelector('#scenario-ws-detail').hidden")
        b.js("document.querySelector('#scenario-ws-tabs "
             "[data-tab=research]').click()")
        b.wait("document.querySelector('#scenario-tab-research "
               ".diagnostics-panel')")
        self.assertTrue(b.js(
            "document.querySelector('#scenario-tab-research')"
            ".textContent.includes('Model diagnostics')"))
        self.assertEqual(b.errors, [])

    def test_structured_feedback_sections_render_as_continuous_text(self):
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        # The narrative keeps its headings without individual cards.
        payload = {"sections": [
            {"title": "Assessment summary",
             "text": "Summary paragraph one.\n\nSummary paragraph two."},
            {"title": "Observed strengths",
             "text": "Strengths narrative text."},
            {"title": "Review focus", "text": "Focus narrative text."},
            {"title": "Next steps", "text": "Steps narrative text."}]}
        host = "document.querySelector('#isolated-feedback-host')"
        b.js("const host = document.createElement('div');"
             "host.id = 'isolated-feedback-host';"
             "document.body.appendChild(host);"
             f"renderStructuredFeedback(host, {json.dumps(payload)})")
        try:
            b.wait(f"{host}.childElementCount === 4")
            self.assertEqual(b.js(
                f"[...{host}.children].map(n => n.className)"),
                ["feedback-section"] * 4)
            self.assertEqual(b.js(
                f"[...{host}.querySelectorAll('.ui-section')].length"), 0)
            self.assertEqual(b.js(
                f"[...{host}.querySelectorAll('.feedback-section > h3')]"
                ".map(n => n.textContent)"),
                ["Assessment summary", "Observed strengths",
                 "Review focus", "Next steps"])
            self.assertEqual(b.js(
                f"[...{host}.querySelectorAll('.feedback-section')]"
                ".map(n => [...n.querySelectorAll('p')]"
                ".map(p => p.textContent))"),
                [["Summary paragraph one.", "Summary paragraph two."],
                 ["Strengths narrative text."],
                 ["Focus narrative text."],
                 ["Steps narrative text."]])
            styles = b.js(
                f"[...{host}.querySelectorAll('.feedback-section')]"
                ".map(n => { const s = getComputedStyle(n); return {"
                "border: [s.borderTopWidth, s.borderRightWidth,"
                " s.borderBottomWidth, s.borderLeftWidth],"
                "background: s.backgroundColor,"
                "padding: [s.paddingLeft, s.paddingRight],"
                "marginY: [parseFloat(s.marginTop),"
                " parseFloat(s.marginBottom)]}; })")
            for style in styles:
                self.assertEqual(style["border"], ["0px"] * 4)
                self.assertIn(style["background"],
                              ("transparent", "rgba(0, 0, 0, 0)"))
                self.assertEqual(style["padding"], ["0px", "0px"])
                self.assertTrue(all(m > 0 for m in style["marginY"]))
            self.assert_no_overflow()
            self.assertFalse(b.js(
                f"{host}.scrollWidth > {host}.clientWidth"))
            b.js(f"{host}.scrollIntoView({{block: 'start'}})")
            b.screenshot("structured-feedback-sections-desktop.png")
            b.viewport(390, 844)
            self.assert_no_overflow()
            self.assertFalse(b.js(
                f"{host}.scrollWidth > {host}.clientWidth"))
            b.screenshot("structured-feedback-sections-mobile.png")
            b.viewport(1440, 1100)
        finally:
            b.js(f"{host}.remove()")
        self.assertEqual(b.errors, [])

    def test_kv_value_column_alignment(self):
        b = self.browser
        self.navigate("/teacher")
        b.wait("document.querySelector('#unlock-panel')")
        # Long labels keep a straight value column instead of pushing .v.
        b.js(
            "const host = document.createElement('div');"
            "host.id = 'kv-fixture';"
            "host.style.maxWidth = '760px'; host.style.margin = '0 auto';"
            "host.style.padding = '1rem';"
            "host.style.boxSizing = 'border-box';"
            "document.body.appendChild(host);"
            "const row = (k, v) => { const d = document.createElement('div');"
            " d.className = 'kv'; const kk = document.createElement('span');"
            " kk.className = 'k'; kk.textContent = k;"
            " const vv = document.createElement('span');"
            " vv.className = 'v'; vv.textContent = v;"
            " d.append(kk, vv); return d; };"
            "const g1 = document.createElement('div');"
            "g1.className = 'release-status';"
            "const g2 = document.createElement('div');"
            "g2.className = 'selection-evidence';"
            "const task = document.createElement('div');"
            "task.className = 'skill-task';"
            "const insp = document.createElement('div');"
            "insp.className = 'kt-inspector';"
            "host.append(g1, g2, task, insp);"
            "g1.append(row('Release status', 'Draft - educator review "
            "required'), row('Practice 1 (Calculate a percentage of an "
            "amount)', 'Ready'), row('Practice 2 (Combine coefficients of "
            "like terms)', 'Pending educator review'), "
            "row('Feedback safeguards', "
            "'provenance:replay-run-9f8e7d6c5b4a3210-snapshot'));"
            "g2.append(row('Status', 'Complete'), row('Experimental "
            "probability band (not validated learning difficulty)', "
            "'0.42-0.61'), row('Estimated correctness on this question "
            "(not mastery)', '0.600'), row('Practice slot 1', "
            "'Same answers - current scoring'));"
            "task.append(row('Topic', 'Fractions of an amount'), "
            "row('Result', 'Improving'));"
            "insp.append(row('Answer 1', 'Correct - 0.9'), "
            "row('Answer 2', 'Incorrect - 0.4'));")
        host = "document.querySelector('#kv-fixture')"
        measure = (
            "JSON.stringify((() => {"
            " const rects = (sel) => [...document.querySelectorAll(sel)]"
            " .map(r => { const k = r.querySelector('.k')"
            " .getBoundingClientRect();"
            " const v = r.querySelector('.v').getBoundingClientRect();"
            " return {kL: k.left, kR: k.right, vL: v.left, vR: v.right,"
            " kH: k.height, rowR: r.getBoundingClientRect().right}; });"
            " const h = document.querySelector('#kv-fixture');"
            " return {g1: rects('#kv-fixture .release-status .kv'),"
            " g2: rects('#kv-fixture .selection-evidence .kv'),"
            " task: rects('#kv-fixture .skill-task .kv'),"
            " insp: rects('#kv-fixture .kt-inspector .kv'),"
            " hostR: h.getBoundingClientRect().right,"
            " hostOver: h.scrollWidth - h.clientWidth,"
            " taskDisp: getComputedStyle(document.querySelector("
            " '#kv-fixture .skill-task .kv')).display,"
            " inspDisp: getComputedStyle(document.querySelector("
            " '#kv-fixture .kt-inspector .kv')).display,"
            " taskKW: document.querySelector('#kv-fixture .skill-task "
            ".kv .k').getBoundingClientRect().width,"
            " taskKDisp: getComputedStyle(document.querySelector("
            " '#kv-fixture .skill-task .kv .k')).display}; })())")

        def check(m, aligned=True, wrap=True):
            self.assertLessEqual(m["hostOver"], 0)
            self.assert_no_overflow()
            groups = (m["g1"], m["g2"])
            if aligned:
                for rows in groups:
                    vls = [r["vL"] for r in rows]
                    self.assertLessEqual(max(vls) - min(vls), 1)
                    for r in rows:
                        self.assertGreater(r["vL"] - r["kR"], 0)
                        self.assertLessEqual(r["kR"], r["vL"] + 1)
            for rows in groups + (m["task"], m["insp"]):
                for r in rows:
                    self.assertLessEqual(r["vR"], r["rowR"] + 1)
                    self.assertLessEqual(r["rowR"], m["hostR"] + 1)
            # The long labels wrap instead of widening the label column.
            if aligned and wrap:
                self.assertGreater(m["g1"][1]["kH"], m["g1"][0]["kH"] + 4)
                self.assertGreater(m["g2"][1]["kH"], m["g2"][0]["kH"] + 4)

        try:
            b.wait(f"Boolean({host})")
            self.assertIn(
                "provenance:replay-run-9f8e7d6c5b4a3210-snapshot",
                b.js(f"{host}.textContent"))
            self.assertIn("Practice 2 (Combine coefficients of like terms)",
                          b.js(f"{host}.textContent"))
            m = json.loads(b.js(measure))
            check(m, aligned=True)
            self.assertEqual(m["inspDisp"], "block")
            self.assertEqual(m["taskDisp"], "flex")
            self.assertAlmostEqual(m["taskKW"], 150, delta=1)
            self.assertLess(
                m["taskKW"], m["g1"][0]["kR"] - m["g1"][0]["kL"])
            b.js(f"{host}.scrollIntoView({{block: 'start'}})")
            b.screenshot("kv-alignment-desktop.png")
            b.viewport(768, 1024)
            # At 42% basis the long labels fit on one line; alignment
            # is what matters here.
            check(json.loads(b.js(measure)), aligned=True, wrap=False)
            b.viewport(390, 844)
            m = json.loads(b.js(measure))
            check(m, aligned=True)
            # Representatives keep their stacked overrides on narrow screens.
            self.assertEqual(m["taskDisp"], "block")
            self.assertEqual(m["taskKDisp"], "block")
            self.assertEqual(m["inspDisp"], "block")
            b.js(f"{host}.scrollIntoView({{block: 'start'}})")
            b.screenshot("kv-alignment-mobile.png")
            b.viewport(1440, 1100)
        finally:
            b.js(f"{host}.remove()")
        self.assertEqual(b.errors, [])


if __name__ == "__main__":
    unittest.main()
