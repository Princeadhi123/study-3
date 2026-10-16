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

    def _complete_pending(self):
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
        return sid, token, cards

    def _complete_and_release(self):
        sid, token, cards = self._complete_pending()
        detail = self.service.teacher_session(sid)
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
        self.assertIsNone(b.js(
            "document.querySelector("
            "'details[data-disclosure-key^=\"worked-review-original:\"]')"))
        self.assertNotIn("Original question wording", blob)
        self.assertFalse(b.js(
            "Boolean(document.querySelector("
            "'.review-example script, .review-example img'))"))
        self.assertEqual(b.js(
            "(() => { const host = document.querySelectorAll("
            "'.review-example')[0].parentElement; return Array.from("
            "host.querySelectorAll('p')).filter(p => p.textContent"
            ".includes('draft translation')).length; })()"), 1)
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
        b.wait("document.querySelector('.session-row, [data-session]')",
               timeout=30)
        b.js("document.querySelector('.session-row, [data-session]')"
             ".click()")
        b.wait("document.querySelector('#teacher-app')"
               ".textContent.includes('Worked review')", timeout=30)
        self._assert_card_rendered()
        teacher_text = b.js("document.body.textContent")
        self.assertIn("Feedback safeguards", teacher_text)
        self.assertIn(
            "Strengths, review focus and next steps are "
            "application-owned.", teacher_text)
        b.command("Page.navigate", {
            "url": f"{self.url}/?session={sid}#token={token}"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.wait("document.body.textContent.includes('Worked review')",
               timeout=30)
        self._assert_card_rendered()

    def test_rendered_feedback_dedup_disclaimer_injection(self):
        b = self.browser
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete'")
        payload = {
            "review_examples": [
                {"source": "incorrect_assessment_response",
                 "question_id": "q1", "prompt": "Alkuluku?",
                 "options": ["2 <img src=x onerror=alert(1)>",
                             "4", "9"],
                 "selected_answer": "4",
                 "correct_answer": "2 <img src=x onerror=alert(1)>",
                 "steps": ["Prime numbers have two divisors."],
                 "reasoning_question": "Explain your choice.",
                 "notice": "Shared checked warning."},
                {"source": "incorrect_assessment_response",
                 "question_id": "q2", "prompt": "Alkuluku?",
                 "options": ["2", "4", "9"],
                 "selected_answer": "4", "correct_answer": "2",
                 "steps": ["Prime numbers have two divisors."],
                 "notice": "Shared checked warning."}],
            "practice_activities": [
                {"status": "selected",
                 "message": "Duplicate selected message.",
                 "notice": "Same synthetic practice notice.",
                 "topic_label": "Percentages",
                 "relationship": {"kind": "same_content",
                                  "message": "Covers the same area."},
                 "question": {"skill_name": "Prosenttilaskenta",
                              "concept_name": "Percentage",
                              "text": "Alkuluku?",
                              "options": ["a", "b", "c"]}},
                {"status": "selected",
                 "message": "Duplicate selected message.",
                 "notice": "Same synthetic practice notice.",
                 "topic_label": "Combine coefficients of like terms",
                 "relationship": {
                     "kind": "same_topic_different_content",
                     "message": "Related topic, not exact content."},
                 "question": {
                     "skill_name": "Algebra",
                     "concept_name":
                         "Recognize terms that cannot be combined",
                     "text": "Other prompt?",
                     "options": ["a", "b", "c"]}},
                {"status": "selected",
                 "message": "Duplicate selected message.",
                 "notice": "Same synthetic practice notice.",
                 "relationship": {"kind": "different_content",
                                  "message": "Different area."},
                 "question": {"skill_name": "Other",
                              "concept_name": "Other concept",
                              "text": "Other prompt?",
                              "options": ["a", "b", "c"]}},
                {"status": "abstained",
                 "message": "Abstained status message.",
                 "notice": None, "question": None}]}
        result = b.js(
            "(() => { const host = document.createElement('div');"
            "document.body.appendChild(host);"
            f"renderStructuredFeedback(host, {json.dumps(payload)});"
            "const text = host.textContent;"
            "const count = (s) => text.split(s).length - 1;"
            "return {disclaimers: count('draft translation'),"
            "exampleNotice: count('Shared checked warning.'),"
            "practiceNotice: count('Same synthetic practice notice.'),"
            "suppressed: text.includes('Duplicate selected message.'),"
            "abstainedShown: text.includes('Abstained status message.'),"
            "relatedHeading: text.includes('Related practice — "
            "Recognize terms that cannot be combined'),"
            "relatedMessage: text.includes("
            "'Related topic, not exact content.'),"
            "separateHeading: text.includes("
            "'Separate practice — Other concept'),"
            "relMessages: count('Covers the same area.'),"
            "imgs: host.querySelectorAll('img').length,"
            "scripts: host.querySelectorAll('script').length,"
            "details: host.querySelectorAll('details.raw').length,"
            "rawImg: (host.querySelector('details.raw') || "
            "{textContent: ''}).textContent.includes("
            "'<img src=x onerror=alert(1)>'),"
            "original: count('Original question wording')}; })()")
        self.assertEqual(result["disclaimers"], 1)
        self.assertEqual(result["exampleNotice"], 1)
        self.assertEqual(result["practiceNotice"], 1)
        self.assertFalse(result["suppressed"])
        self.assertTrue(result["abstainedShown"])
        self.assertTrue(result["relatedHeading"])
        self.assertTrue(result["relatedMessage"])
        self.assertTrue(result["separateHeading"])
        self.assertEqual(result["relMessages"], 1)
        self.assertEqual(result["imgs"], 0)
        self.assertEqual(result["scripts"], 0)
        self.assertGreaterEqual(result["details"], 1)
        self.assertTrue(result["rawImg"])
        self.assertEqual(result["original"], 0)
        self.assertEqual(b.errors, [])

    def test_concise_summary_table_teacher_and_student(self):
        b = self.browser
        wrong = [positions(self.bank, "pct", 0)[0],
                 positions(self.bank, "alg", 0)[0]]
        created = self.service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in grounded_rows(self.bank, wrong):
            self.service.submit_response(sid, token, row)
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.service._load_meta(sid)["provider_job"][
                    "status"] in ("ready", "fallback"):
                break
            time.sleep(0.05)
        else:
            self.fail("provider job did not finish")
        detail = self.service.teacher_session(sid)
        preview = detail["feedback_delivery"]["preview"]
        self.assertEqual(preview["template_version"],
                         "phase3_student_feedback_template_v9")
        b.command("Page.navigate", {
            "url": f"{self.url}/?session={sid}#token={token}"})
        b.wait("document.readyState === 'complete'")
        summary = "#end-summary"
        b.wait("document.querySelector('" + summary + "')"
               ".textContent.includes('reviewing your feedback')")
        self.assertFalse(b.js(
            "Boolean(document.querySelector('" + summary +
            " .assessment-results'))"))
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete'")
        b.wait("document.querySelector('#pin-input')")
        b.js(f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)}; "
             "document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('.session-row, [data-session]')",
               timeout=30)
        b.js("document.querySelector('.session-row, [data-session]')"
             ".click()")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]')"
             ".click()")
        results = "#tab-feedback .assessment-results"
        b.wait(f"Boolean(document.querySelector('{results}'))",
               timeout=30)
        self.assertEqual(b.js(
            f"document.querySelector('{results} p').textContent"),
            "Overall: 38 of 40 correct on this assessment.")
        self.assertEqual(b.js(
            f"document.querySelectorAll('{results} "
            ".assessment-topics tbody tr').length"), 4)
        table_text = b.js(
            f"document.querySelector('{results} "
            ".assessment-topics').textContent")
        for needle in ("Topic", "Result", "What this topic covers",
                       "Percentages", "Fractions",
                       "Working with percentages and amounts",
                       "Working with fractions",
                       "Questions about this topic."):
            self.assertIn(needle, table_text)
        preview_text = b.js(
            "document.querySelector('#tab-feedback .feedback-preview')"
            ".textContent")
        for needle in ("Assessment complete", "What went well",
                       "Answers to review", "What to do next"):
            self.assertIn(needle, preview_text)
        for needle in ("draft, not a diagnosis",
                       "If your teacher approves"):
            self.assertNotIn(needle, preview_text)
        self.assertEqual(b.js(
            "document.querySelectorAll('#tab-feedback "
            ".review-example').length"), 2)
        teacher_table = b.js(
            f"document.querySelector('{results}').textContent")
        released = self.service.release_feedback(sid, {
            "message_sha256": detail["feedback_delivery"]
            ["preview_sha256"], "reviewer_label": "browser-test"})
        self.assertTrue(released["released"])
        b.command("Page.navigate", {
            "url": f"{self.url}/?session={sid}#token={token}"})
        b.wait("document.readyState === 'complete'")
        b.wait("Boolean(document.querySelector('" + summary +
               " .assessment-results'))", timeout=30)
        student_table = b.js(
            "document.querySelector('" + summary +
            " .assessment-results').textContent")
        self.assertEqual(student_table, teacher_table)
        student_text = b.js(
            "document.querySelector('" + summary + "').textContent")
        for needle in ("Assessment complete", "What went well",
                       "Answers to review", "What to do next"):
            self.assertIn(needle, student_text)
        self.assertEqual(b.js(
            "document.querySelectorAll('" + summary +
            " .review-example').length"), 2)
        self.assertTrue(b.js(
            "Boolean(document.querySelector('" + summary +
            " details.raw'))"))
        b.viewport(390, 844)
        self.assertFalse(b.js(
            "document.documentElement.scrollWidth > "
            "document.documentElement.clientWidth"))
        self.assertEqual(b.errors, [])

    def test_question_first_hint_and_reveal_flow(self):
        sid, token, cards = self._complete_pending()
        b = self.browser
        ready_index = next(
            i for i, c in enumerate(cards)
            if c["status"] == "ready")
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete'")
        b.wait("document.querySelector('#pin-input')")
        b.js(f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)}; "
             "document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelector('.session-row, [data-session]')",
               timeout=30)
        b.js("document.querySelector('.session-row, [data-session]')"
             ".click()")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]')"
             ".click()")
        card_sel = (f"document.querySelectorAll('.review-example')"
                    f"[{ready_index}]")
        b.wait(f"{card_sel} && {card_sel}.querySelector("
               "'details.worked-solution')", timeout=30)
        state = b.js(
            "(() => { const c = " + card_sel + ";"
            "const sol = c.querySelector('details.worked-solution');"
            "const correct = Array.from(sol.querySelectorAll('p'))"
            ".find(p => p.textContent.startsWith('Correct answer:'));"
            "return {think: c.querySelector('.think-first p')"
            " ? c.querySelector('.think-first p').textContent : null,"
            "thinkLabel: c.querySelector('.think-first h4')"
            " ? c.querySelector('.think-first h4').textContent : null,"
            "hintCount: c.querySelectorAll('details.worked-hint')"
            ".length,"
            "nested: c.querySelectorAll('details.worked-hint "
            "details.worked-hint').length,"
            "solOpen: sol.open,"
            "summary: sol.querySelector('summary').textContent,"
            "correctVisible: correct ? correct.checkVisibility()"
            " : null,"
            "topCorrect: Array.from(c.querySelectorAll(':scope > p'))"
            ".some(p => p.textContent.startsWith('Correct answer:'))};"
            "})()")
        self.assertEqual(state["thinkLabel"], "Think first")
        self.assertEqual(state["think"],
                         cards[ready_index]["socratic_prompt"])
        self.assertEqual(state["hintCount"], 2)
        self.assertEqual(state["nested"], 1)
        self.assertFalse(state["solOpen"])
        self.assertEqual(state["summary"],
                         "Reveal checked worked steps")
        self.assertEqual(state["correctVisible"], False)
        self.assertFalse(state["topCorrect"])
        card_text = b.js(card_sel + ".textContent")
        self.assertIn("Your selected answer:", card_text)
        b.js(card_sel + ".querySelector('details.worked-hint"
             " summary').click()")
        self.assertTrue(b.js(card_sel + ".querySelector("
                             "'details.worked-hint').open"))
        inner = card_sel + ".querySelector('details.worked-hint " \
                          "details.worked-hint')"
        self.assertFalse(b.js(inner + ".open"))
        b.js(inner + ".querySelector('summary').click()")
        self.assertTrue(b.js(inner + ".open"))
        self.assertIn(cards[ready_index]["progressive_hints"][0],
                      card_text)
        b.js(card_sel + ".querySelector('details.worked-solution"
             " summary').click()")
        self.assertTrue(b.js(card_sel + ".querySelector("
                             "'details.worked-solution').open"))
        revealed = b.js(card_sel + ".textContent")
        self.assertIn("Correct answer:", revealed)
        for step in cards[ready_index]["steps"]:
            self.assertIn(step, revealed)
        other = b.js(
            "Array.from(document.querySelectorAll('.review-example'))"
            f".filter((c, i) => i !== {ready_index})"
            ".some(c => c.querySelector('details.worked-solution')"
            " && c.querySelector('details.worked-solution').open)")
        self.assertFalse(other)
        b.js(card_sel + ".scrollIntoView()")
        b.screenshot("question-first-revealed.png")
        b.js("document.querySelector('#ws-tabs [data-tab=overview]')"
             ".click()")
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]')"
             ".click()")
        b.wait(card_sel + ".querySelector('details.worked-solution')")
        self.assertTrue(b.js(card_sel + ".querySelector("
                             "'details.worked-solution').open"))
        res = self.service.save_feedback_edit(sid, {
            "message_sha256": self.service.teacher_session(sid)[
                "feedback_delivery"]["preview_sha256"],
            "sections": {s["kind"]: s["text"] for s in
                         self.service.teacher_session(sid)[
                             "feedback_delivery"]["preview"]
                         ["sections"]},
            "content": {"review_examples": [
                ({"socratic_prompt": "Teacher reworded prompt."}
                 if i == ready_index else {})
                for i in range(len(cards))]}})
        self.assertTrue(res["saved"])
        b.wait(card_sel + ".textContent.includes("
               "'Teacher reworded prompt.')", timeout=60)
        self.assertFalse(b.js(card_sel + ".querySelector("
                              "'details.worked-solution').open"))
        detail = self.service.teacher_session(sid)
        released = self.service.release_feedback(sid, {
            "message_sha256": detail["feedback_delivery"]
            ["preview_sha256"], "reviewer_label": "browser-test"})
        self.assertTrue(released["released"])
        b.command("Page.navigate", {
            "url": f"{self.url}/?session={sid}#token={token}"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.wait("document.querySelectorAll('.review-example').length"
               " >= 2", timeout=30)
        student = b.js(
            "(() => { const cards = Array.from("
            "document.querySelectorAll('.review-example'));"
            "return {solutions: cards.filter(c => c.querySelector("
            "'details.worked-solution')).length,"
            "open: cards.filter(c => c.querySelector("
            "'details.worked-solution') && c.querySelector("
            "'details.worked-solution').open).length,"
            "thinkFirst: cards.filter(c => c.querySelector("
            "'.think-first')).length,"
            "literal: document.body.textContent.includes("
            "'<img src=x onerror=alert(1)>'),"
            "injected: Boolean(document.querySelector("
            "'.review-example img, .review-example script'))}; })()")
        self.assertEqual(student["solutions"], 2)
        self.assertEqual(student["open"], 0)
        self.assertEqual(student["thinkFirst"], 2)
        self.assertTrue(student["literal"])
        self.assertFalse(student["injected"])
        b.js("document.querySelector('.review-example')"
             ".scrollIntoView()")
        b.screenshot("question-first-student.png")
        b.viewport(390, 844)
        b.wait("document.readyState === 'complete'")
        self.assertFalse(b.js(
            "document.documentElement.scrollWidth > "
            "document.documentElement.clientWidth"))
        b.js("document.querySelectorAll('.review-example')"
             f"[{ready_index}].scrollIntoView()")
        b.screenshot("question-first-mobile.png")
        self.assertEqual(b.errors, [])

    def test_question_first_render_matrix(self):
        b = self.browser
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete'")
        payload = {
            "review_examples": [
                {"source": "incorrect_assessment_response",
                 "question_id": "q1",
                 "presentation_version":
                     "question_first_worked_review_v1",
                 "status": "ready", "teacher_edited": True,
                 "prompt": "Alkuluku?",
                 "options": ["2", "4", "9"],
                 "selected_answer": "4", "correct_answer": "2",
                 "steps": ["Checked step one."],
                 "socratic_prompt": "How did you approach this?",
                 "option_contrast": "Consider the variable part.",
                 "progressive_hints": ["Hint A.", "Hint B."],
                 "explanation": "Teacher extra explanation.",
                 "original_content": {
                     "prompt": "Alkuluku?", "options": ["2", "4", "9"],
                     "selected_answer": "4", "correct_answer": "2",
                     "steps": ["Checked step one."]}},
                {"source": "incorrect_assessment_response",
                 "question_id": "q2",
                 "presentation_version":
                     "question_first_worked_review_v1",
                 "status": "unavailable",
                 "prompt": "Free text?",
                 "options": [], "selected_answer": "x",
                 "correct_answer": "y", "steps": [],
                 "socratic_prompt": "Generic approach question.",
                 "option_contrast": "", "progressive_hints": [],
                 "notice": "A checked worked solution is "
                           "unavailable."},
                {"source": "incorrect_assessment_response",
                 "question_id": "q3", "status": "ready",
                 "prompt": "Legacy?", "options": ["a", "b"],
                 "selected_answer": "b", "correct_answer": "a",
                 "steps": ["Legacy visible step."]}]}
        result = b.js(
            "(() => { const host = document.createElement('div');"
            "document.body.appendChild(host);"
            f"renderStructuredFeedback(host, {json.dumps(payload)});"
            "const cards = host.querySelectorAll('.review-example');"
            "const sol0 = cards[0].querySelector("
            "'details.worked-solution');"
            "const expl = Array.from(sol0.querySelectorAll('p'))"
            ".find(p => p.textContent === 'Teacher extra "
            "explanation.');"
            "const explVisible = expl ? expl.checkVisibility()"
            " : null;"
            "const orig = cards[0].querySelector("
            "'details.raw:not(.worked-solution):not(.worked-hint)');"
            "const origInside = sol0.querySelector('details.raw');"
            "return {"
            "summary0: sol0.querySelector('summary').textContent,"
            "explVisible,"
            "contrast: cards[0].querySelector('.option-contrast p')"
            " ? cards[0].querySelector('.option-contrast p')"
            ".textContent : null,"
            "hints0: cards[0].querySelectorAll('details.worked-hint')"
            ".length,"
            "origInsideSolution: Boolean(origInside),"
            "summary1: cards[1].querySelector('details.worked-solution"
            " summary').textContent,"
            "hints1: cards[1].querySelectorAll('details.worked-hint')"
            ".length,"
            "generic1: cards[1].querySelector('.think-first p')"
            ".textContent,"
            "steps1: cards[1].querySelectorAll('details.worked-"
            "solution ol.worked-steps li').length,"
            "legacyVisible: Array.from(cards[2].querySelectorAll("
            "':scope > p')).some(p => p.textContent === "
            "'Correct answer: a'),"
            "legacyStepsVisible: cards[2].querySelector("
            "':scope > ol.worked-steps li') !== null,"
            "legacySolution: Boolean(cards[2].querySelector("
            "'details.worked-solution'))}; })()")
        self.assertEqual(result["summary0"],
                         "Reveal answer and worked steps")
        self.assertEqual(result["explVisible"], False)
        self.assertEqual(result["contrast"],
                         "Consider the variable part.")
        self.assertEqual(result["hints0"], 2)
        self.assertTrue(result["origInsideSolution"])
        self.assertEqual(result["summary1"],
                         "Reveal recorded correct answer")
        self.assertEqual(result["hints1"], 0)
        self.assertEqual(result["generic1"],
                         "Generic approach question.")
        self.assertEqual(result["steps1"], 0)
        self.assertTrue(result["legacyVisible"])
        self.assertTrue(result["legacyStepsVisible"])
        self.assertFalse(result["legacySolution"])
        self.assertEqual(b.errors, [])

    def test_question_first_warm_bank_render_and_keyboard(self):
        import assessment_worked_review as awr
        import worked_review_math
        from research_runtime import load_research_bank
        bank, taxonomy, _ = load_research_bank("warm")
        questions = {q["question_id"]: q for q in bank["questions"]}
        leaf_for = {}
        for topic in taxonomy["topics"]:
            for leaf in topic["subtopics"]:
                for qid in leaf["question_ids"]:
                    leaf_for[qid] = leaf["id"]
        pairs = [
            ("32978__p2__qad057f930f90539d__of1e565b0fb849511", "7w"),
            ("32978__p2__qad057f930f90539d__of1e565b0fb849511", "5v2v"),
            ("512961__p2__q5f787a330af2b755__o03da091c4f2e8d2f",
             "2xy")]
        expected_texts = {
            "7w": "Your selected option uses w, while both terms in "
                  "the question use v. How did you choose the "
                  "variable letter in your answer?",
            "5v2v": "Your selected option places the terms next to "
                    "each other without the original plus sign. How "
                    "did you represent the addition?",
            "2xy": "Your selected option writes x and y together as "
                   "a product. What operation is shown between x and "
                   "y in the question?"}
        cards = []
        for qid, selected in pairs:
            question = questions[qid]
            leaf = leaf_for[qid]
            correct = question["options"][question["answer_index"]]
            self.assertNotEqual(selected, correct)
            solved = worked_review_math.solution(question, leaf)
            cards.append({
                "source": "incorrect_assessment_response",
                "status": "ready", "question_id": qid,
                "prompt": question["text"],
                "options": list(question["options"]),
                "selected_answer": selected,
                "correct_answer": correct,
                "subtopic_id": leaf,
                "steps": solved["steps"],
                "reasoning_question": solved["reasoning_question"],
                "notice": awr.CONCISE_READY_NOTICE})
        preview = awr.question_first_preview(
            {"sections": [], "review_examples": cards})
        for card, (_, selected) in zip(preview["review_examples"],
                                       pairs):
            self.assertEqual(card["option_contrast"],
                             expected_texts[selected])
            self.assertEqual(len(card["progressive_hints"]), 2)
        b = self.browser
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete'")
        initial = b.js(
            "(() => { const host = document.createElement('div');"
            "document.body.appendChild(host);"
            f"renderStructuredFeedback(host, {json.dumps(preview)});"
            "window.__qfHost = host;"
            "return Array.from(host.querySelectorAll("
            "'.review-example')).map((c) => ({"
            "think: c.querySelector('.think-first p').textContent,"
            "contrast: c.querySelector('.option-contrast p')"
            " ? c.querySelector('.option-contrast p').textContent"
            " : null,"
            "selected: Array.from(c.querySelectorAll(':scope > p'))"
            ".some(p => p.textContent.startsWith("
            "'Your selected answer:')),"
            "correctVisible: Array.from(c.querySelectorAll("
            "'details.worked-solution p')).some(p =>"
            " p.textContent.startsWith('Correct answer:') &&"
            " p.checkVisibility()),"
            "hintCount: c.querySelectorAll('details.worked-hint')"
            ".length,"
            "summary: c.querySelector('details.worked-solution "
            "summary').textContent,"
            "solOpen: c.querySelector('details.worked-solution')"
            ".open})); })()")
        self.assertEqual(len(initial), 3)
        for state, card in zip(initial, preview["review_examples"]):
            self.assertTrue(state["selected"])
            self.assertFalse(state["solOpen"])
            self.assertFalse(state["correctVisible"])
            self.assertEqual(state["summary"],
                             "Reveal checked worked steps")
            self.assertEqual(state["hintCount"], 2)
            self.assertEqual(state["think"], card["socratic_prompt"])
            self.assertEqual(state["contrast"],
                             card["option_contrast"])
        b.js("window.__qfHost.querySelector('.review-example')"
             ".scrollIntoView()")
        b.screenshot("question-first-warm-card.png")
        summary_js = ("window.__qfHost.querySelectorAll("
                      "'.review-example')[0]")
        def press_key(key, code, vk, text=None):
            b.command("Input.dispatchKeyEvent", {
                "type": "rawKeyDown", "key": key, "code": code,
                "windowsVirtualKeyCode": vk})
            if text is not None:
                b.command("Input.dispatchKeyEvent", {
                    "type": "char", "key": key, "code": code,
                    "text": text, "windowsVirtualKeyCode": vk})
            b.command("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": key, "code": code,
                "windowsVirtualKeyCode": vk})
        b.js(summary_js + ".querySelector('details.worked-hint "
            "summary').focus()")
        press_key("Enter", "Enter", 13, "\r")
        self.assertTrue(b.js(summary_js + ".querySelector("
                             "'details.worked-hint').open"))
        b.js(summary_js + ".querySelector('details.worked-hint "
            "details.worked-hint summary').focus()")
        press_key(" ", "Space", 32, " ")
        self.assertTrue(b.js(summary_js + ".querySelector("
                             "'details.worked-hint "
                             "details.worked-hint').open"))
        self.assertFalse(b.js(summary_js + ".querySelector("
                              "'details.worked-solution').open"))
        b.screenshot("question-first-warm-hints.png")
        b.js(summary_js + ".querySelector('details.worked-solution "
            "summary').focus()")
        press_key("Enter", "Enter", 13, "\r")
        self.assertTrue(b.js(summary_js + ".querySelector("
                             "'details.worked-solution').open"))
        revealed = b.js(summary_js + ".textContent")
        self.assertIn("Correct answer: 7v", revealed)
        for step in preview["review_examples"][0]["steps"]:
            self.assertIn(step, revealed)
        b.screenshot("question-first-warm-solution.png")
        b.viewport(390, 844)
        b.js(summary_js + ".scrollIntoView()")
        self.assertFalse(b.js(
            "document.documentElement.scrollWidth > "
            "document.documentElement.clientWidth"))
        b.screenshot("question-first-warm-mobile.png")
        b.viewport(1440, 1100)
        self.assertEqual(b.errors, [])

    def test_aitta_provenance_guard_labels(self):
        b = self.browser
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete'")
        guarded_trace = {
            "phrasing_source": "injected_generator_full_sections",
            "guardrails_version": "application_owned_multi_feedback_v1",
            "section_sources": {
                "assessment_summary": "provider",
                "observed_strengths": "application",
                "review_focus": "application",
                "next_steps": "application"}}
        hosted_exec = {"capture_file": "aitta_x.json",
                       "reused": False, "origin": "hosted_replay",
                       "metadata": {"status": "completed"}}
        result = b.js(
            "(() => {"
            f"const guarded = {json.dumps(guarded_trace)};"
            f"const hosted = {json.dumps(hosted_exec)};"
            "const hostedLabel = aittaProvenance("
            "{trace: guarded}, hosted);"
            "const injectedLabel = aittaProvenance("
            "{trace: guarded}, {});"
            "const historical = aittaProvenance({trace: {"
            "phrasing_source: "
            "'injected_generator_full_sections'}}, hosted);"
            "return {hostedLabel, injectedLabel, historical}; })()")
        self.assertIn("completion wording; factual sections "
                      "application-owned", result["hostedLabel"])
        self.assertIn("Aitta - fresh hosted response",
                      result["hostedLabel"])
        self.assertIn("completion wording; factual sections "
                      "application-owned", result["injectedLabel"])
        self.assertIn("hosted provenance unavailable",
                      result["injectedLabel"])
        self.assertEqual(result["historical"],
                         "Aitta - fresh hosted response "
                         "(full structured feedback)")
        self.assertEqual(b.errors, [])


if __name__ == "__main__":
    unittest.main()
