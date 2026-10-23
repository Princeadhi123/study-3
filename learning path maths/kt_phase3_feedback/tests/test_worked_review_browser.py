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
    VALID_SECTIONS, fixture_reviews, grounded_rows,
    make_grounded_bank, make_grounded_taxonomy, positions)
from tests.browser_runtime import (
    cleanup_browser_directory, read_devtools_port)
from tests.test_research_browser import Browser
from tests.test_demo_service import wait_for


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
        self.addCleanup(cleanup_browser_directory, self.tmp)
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
            "--disable-gpu", "--no-first-run", "--disable-extensions",
            "--disable-component-update",
            "--disable-background-networking",
            "--no-default-browser-check", "--window-size=1440,1100",
            "about:blank"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_browser)
        port = read_devtools_port(profile)
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

    def _complete_pending(self, wrong=None):
        if wrong is None:
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
        b.wait("!document.querySelector('#end-panel').hidden",
               timeout=30)
        # Released worked-review cards stay in the educator workspace;
        # the assessment page shows only completion and the score.
        summary = "document.querySelector('#end-summary')"
        b.wait(f"{summary}.textContent.includes('Educator workspace')",
               timeout=30)
        student_text = b.js(f"{summary}.textContent")
        self.assertIn("questions correctly", student_text)
        self.assertNotIn("Worked review", student_text)
        self.assertFalse(b.js(
            "Boolean(document.querySelector('.review-example'))"))

    def test_teacher_worked_example_override_rebuilds_draft(self):
        # Two incorrect percentage answers give the chooser a real
        # alternative; index 8 is also wrong so a checked-solution
        # candidate besides the selected one exists (the injected
        # markup in question 0 drops its checked solution). A second
        # session exercises session-switching while the override POST
        # is still in flight.
        wrong = (positions(self.bank, "pct", 0)[:1] + [8, 12]
                 + [positions(self.bank, "alg", 0)[0]])
        sid, token, cards = self._complete_pending(wrong)
        other_sid, _, _ = self._complete_pending()
        delivery = self.service.teacher_session(
            sid)["feedback_delivery"]
        choice = next(
            c for c in delivery["worked_example_choices"]
            if len(c["candidates"]) > 1)
        self.assertEqual(choice["source"], "kt_recommendation")
        self.assertEqual(choice["selected_question_id"],
                         choice["recommended_question_id"])
        target = next(
            (c for c in choice["candidates"]
             if c["question_id"] != choice["selected_question_id"]
             and c.get("checked_solution_available")),
            next(c for c in choice["candidates"]
                 if c["question_id"] != choice["selected_question_id"]))
        b = self.browser
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete'")
        b.wait("document.querySelector('#pin-input')")
        b.js(f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)}; "
             "document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelectorAll('.session-row').length >= 2",
               timeout=30)

        def select_session(target_sid):
            # Session rows have no stable sid attribute; the overview
            # tab embeds it in the technical-identity details JSON.
            marker = (
                "(() => { const d = document.querySelector("
                "'#ws-detail'); return d ? d.textContent.includes("
                f"{json.dumps(target_sid)}) : false; }})()")
            for i in range(4):
                b.js(f"document.querySelectorAll('.session-row')[{i}]"
                     " && document.querySelectorAll('.session-row')"
                     f"[{i}].click()")
                if wait_for(lambda: b.js(marker), timeout=5):
                    # The review card lives in the feedback tab;
                    # hidden panes cannot receive real focus/keys.
                    b.js("document.querySelector("
                         "'#ws-tabs [data-tab=feedback]').click()")
                    return
            self.fail(f"could not select session {target_sid}")

        # Give the second session a dirty editor draft first, so the
        # in-flight override must not disturb it later.
        select_session(other_sid)
        doc = "document.querySelector('#edit-document')"
        b.wait(doc, timeout=30)
        marker_text = "TEACHER-DRAFT-SESSION-TWO"
        b.js(f"{doc}.focus(); {doc}.textContent += "
             f"{json.dumps(marker_text)}; "
             f"{doc}.dispatchEvent(new InputEvent('input'))")
        self.assertTrue(b.js(
            "Boolean([...document.querySelectorAll('button')]"
            ".find(n => n.textContent === 'Save edited draft' "
            "&& !n.disabled))"))

        select_session(sid)
        cid = json.dumps(choice["candidate_id"])
        sel = ("document.querySelector("
               f"'.worked-example-select[data-candidate-id={cid}]')")
        b.wait(sel, timeout=30)
        # The select has an explicit accessible label bound by for/id.
        self.assertTrue(b.js(
            "(() => { const s = document.querySelector("
            f"'.worked-example-select[data-candidate-id={cid}]'); "
            "return s && Boolean(document.querySelector("
            "'label[for=\"' + s.id + '\"]')); })()"))
        fieldset_text = b.js(
            "document.querySelector('.worked-example-choice')"
            ".textContent")
        self.assertIn("lowest pre-answer KT", fieldset_text)
        self.assertIn(
            "Experimental KT estimates do not establish which "
            "explanation will improve learning most.", fieldset_text)
        self.assertIn("all changed question selections",
                      fieldset_text)
        self.assertIn("Only those examples' explanations are "
                      "regenerated", fieldset_text)

        # Keyboard interaction through real CDP key events on the
        # focused select. Headless Edge does not commit <select>
        # arrow-key navigation (the native popup is unavailable), so
        # the alternative is then chosen programmatically and the
        # change event fires as it would after a keyboard commit.
        initial_value = b.js(f"{sel}.value")
        b.js(f"{sel}.focus()")
        self.assertTrue(wait_for(
            lambda: b.js(f"document.activeElement === {sel}"),
            timeout=5))
        arrow_down = {"type": "keyDown", "key": "ArrowDown",
                      "code": "ArrowDown", "windowsVirtualKeyCode": 40,
                      "nativeVirtualKeyCode": 40}
        arrow_up = {"type": "keyUp", "key": "ArrowDown",
                    "code": "ArrowDown", "windowsVirtualKeyCode": 40,
                    "nativeVirtualKeyCode": 40}
        enter_down = {"type": "keyDown", "key": "Enter",
                      "code": "Enter", "windowsVirtualKeyCode": 13,
                      "nativeVirtualKeyCode": 13, "text": "\r",
                      "unmodifiedText": "\r"}
        enter_up = {"type": "keyUp", "key": "Enter", "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13}

        def key(pair):
            b.command("Input.dispatchKeyEvent", pair[0])
            b.command("Input.dispatchKeyEvent", pair[1])
            time.sleep(0.15)

        key((arrow_down, arrow_up))
        key((enter_down, enter_up))
        self.assertEqual(b.errors, [])
        new_value = b.js(f"{sel}.value")
        checked = {c["question_id"] for c in choice["candidates"]
                   if c.get("checked_solution_available")}
        if new_value == initial_value or new_value not in checked:
            # Headless limitation: commit the alternative option
            # programmatically after the keyboard events; prefer a
            # candidate with a checked solution.
            new_value = next(
                (c["question_id"] for c in choice["candidates"]
                 if c["question_id"] != initial_value
                 and c.get("checked_solution_available")),
                next(c["question_id"] for c in choice["candidates"]
                     if c["question_id"] != initial_value))
            b.js(f"{sel}.value = {json.dumps(new_value)}; "
                 f"{sel}.dispatchEvent(new Event('change'))")
        self.assertNotEqual(new_value, initial_value)
        target = next(c for c in choice["candidates"]
                      if c["question_id"] == new_value)

        # Hold the override POST resolution until released, so the
        # busy state and session-switch behaviour are observable.
        b.js(
            "window.__workedRelease = null;"
            "const __origFetch = window.fetch.bind(window);"
            "window.fetch = (input, init) => {"
            "  const req = __origFetch(input, init);"
            "  if (String(input).indexOf('worked-example-selection')"
            "      !== -1) {"
            "    return req.then((res) => new Promise((resolve) => {"
            "      window.__workedRelease = () => resolve(res);"
            "    }));"
            "  }"
            "  return req;"
            "};")
        apply_btn = (f"{sel}.closest('fieldset')"
                     ".querySelector('.worked-example-apply')")
        self.assertFalse(b.js(f"{apply_btn}.disabled"))
        # Activate the apply button with a real keyboard Enter.
        b.js(f"{apply_btn}.focus()")
        self.assertTrue(wait_for(
            lambda: b.js(
                f"document.activeElement === {apply_btn}"),
            timeout=5))
        key((enter_down, enter_up))
        b.wait("window.__workedRelease !== null", timeout=30)
        # While the rebuild is in flight every mutation control for
        # this session is disabled.
        self.assertTrue(b.js(
            f"{sel}.closest('fieldset').disabled"))
        approve = ("[...document.querySelectorAll('button.primary')]"
                   ".find(b => b.textContent.startsWith('Approve'))")
        self.assertTrue(b.js(
            f"Boolean({approve} && {approve}.disabled)"))
        self.assertTrue(b.js(
            "Boolean([...document.querySelectorAll('button')]"
            ".find(n => n.textContent === 'Save edited draft' "
            "&& n.disabled))"))
        self.assertEqual(b.js(
            f"{doc}.getAttribute('aria-disabled')"), "true")

        # Switch to the other session while the first POST is blocked.
        select_session(other_sid)
        b.wait(doc, timeout=30)
        self.assertIn(marker_text, b.js(f"{doc}.textContent"))

        # Resolve the first request; its rebuild must land on the
        # right session and the other session's draft stays dirty.
        b.js("window.__workedRelease()")
        select_session(sid)
        b.wait(f"{sel} && {sel}.value === "
               f"{json.dumps(target['question_id'])}", timeout=30)
        self.assertTrue(wait_for(
            lambda: "Worked example updated" in b.js(
                "document.querySelector('.worked-example-choice')"
                ".textContent"), timeout=15))
        # Teacher-selected card does not wear the KT recommendation
        # reason; it names the original recommendation instead.
        self.assertIn("Teacher-selected example is shown",
                      b.js("document.querySelector"
                           "('.worked-example-choice').textContent"))
        expected_prompt = b.js(
            "englishQuestionText(" +
            json.dumps({"text": target["prompt"]}) + ")")
        expected_selected = b.js(
            "englishOptionText(" +
            json.dumps(target["selected_answer"]) + ")")
        preview_text = (
            "document.querySelector('.feedback-preview') && "
            "document.querySelector('.feedback-preview').textContent")
        self.assertTrue(wait_for(
            lambda: expected_prompt in b.js(preview_text)
            and f"Your selected answer: {expected_selected}"
            in b.js(preview_text), timeout=15))
        meta = self.service._load_meta(sid)
        self.assertEqual(
            len(meta["worked_example_selection_history"]), 1)
        card = next(
            c for c in meta["checkpoints"]["end"]["student_review"]
            ["review_examples"]
            if c["question_id"] == target["question_id"])
        self.assertEqual(card["selection_provenance"]["source"],
                         "teacher_override")

        # Approve the rebuilt draft exactly once; the chooser disables
        # because a release now exists.
        b.wait(f"Boolean({approve} && !{approve}.disabled)",
               timeout=30)
        self.assertIsNone(
            self.service._load_meta(sid).get("feedback_release"))
        b.js(f"{approve}.click()")
        b.wait("document.body.textContent.includes('Released to the "
               "student')", timeout=30)
        b.wait(f"{sel}.closest('fieldset').disabled && "
               f"{apply_btn}.disabled", timeout=30)
        released = self.service.teacher_session(sid)[
            "feedback_delivery"]["released_feedback"]
        self.assertIsNotNone(released)
        self.assertNotIn("p_correct_before_answer",
                         json.dumps(released))
        # The other session's dirty editor draft was never touched.
        select_session(other_sid)
        b.wait(doc, timeout=30)
        self.assertIn(marker_text, b.js(f"{doc}.textContent"))
        self.assertTrue(b.js(
            "Boolean([...document.querySelectorAll('button')]"
            ".find(n => n.textContent === 'Save edited draft' "
            "&& !n.disabled))"))

        # The assessment page still shows completion only.
        b.command("Page.navigate", {
            "url": f"{self.url}/?session={sid}#token={token}"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.wait("!document.querySelector('#end-panel').hidden",
               timeout=30)
        summary = "document.querySelector('#end-summary')"
        b.wait(f"{summary}.textContent.includes('Educator workspace')",
               timeout=30)
        student_text = b.js(f"{summary}.textContent")
        self.assertIn("questions correctly", student_text)
        self.assertNotIn("Worked review", student_text)
        self.assertFalse(b.js(
            "Boolean(document.querySelector('.review-example'))"))
        self.assertEqual(b.errors, [])

    def _two_choice_fixture(self):
        # Two wrong answers across both skills give BOTH choosers at
        # least two alternatives; index 8's candidate lost its checked
        # solution to the injected markup, so two alternatives are
        # required to guarantee a usable target per card.
        wrong = (positions(self.bank, "pct", 0)[:1] + [8, 12]
                 + positions(self.bank, "alg", 0)[:3])
        sid, token, cards = self._complete_pending(wrong)
        delivery = self.service.teacher_session(sid)["feedback_delivery"]
        initial_sections = [dict(s) for s in
                            delivery["preview"]["sections"]]
        initial_practice = json.loads(json.dumps(
            delivery["preview"].get("practice_activities")))
        choices = delivery["worked_example_choices"]
        self.assertEqual(len(choices), 2)
        chosen = {}
        targets = {}
        for choice in choices:
            alts = [c for c in choice["candidates"]
                    if c["question_id"]
                    != choice["selected_question_id"]]
            self.assertGreaterEqual(
                len(alts), 2,
                f"choice {choice['candidate_id']} needs two "
                "alternatives")
            target = next(
                (c for c in alts
                 if c.get("checked_solution_available")), alts[0])
            chosen[choice["candidate_id"]] = target["question_id"]
            targets[choice["candidate_id"]] = target
        return (sid, choices, chosen, targets,
                initial_sections, initial_practice)

    def _open_teacher_feedback(self):
        b = self.browser
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete'")
        b.wait("document.querySelector('#pin-input')")
        b.js(f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)}; "
             "document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelectorAll('.session-row').length >= 1",
               timeout=30)
        b.js("document.querySelectorAll('.session-row')[0].click()")
        b.wait("document.querySelector('#ws-tabs [data-tab=feedback]')",
               timeout=30)
        b.js("document.querySelector("
             "'#ws-tabs [data-tab=feedback]').click()")

    def _worked_dom(self, cid):
        sel = ("document.querySelector("
               "'.worked-example-select"
               f"[data-candidate-id={json.dumps(cid)}]')")
        fieldset = f"{sel}.closest('fieldset')"
        return (sel, fieldset,
                f"{fieldset}.querySelector('.worked-example-apply')")

    def _run_two_choice_batch_apply(self, click_index):
        # Either rebuild button applies every pending dropdown choice
        # in displayed order, whichever button was clicked.
        (sid, choices, chosen, targets, initial_sections,
         initial_practice) = self._two_choice_fixture()
        b = self.browser
        self._open_teacher_feedback()
        cid_a, cid_b = (choices[0]["candidate_id"],
                        choices[1]["candidate_id"])
        sel_a, fs_a, apply_a = self._worked_dom(cid_a)
        sel_b, fs_b, apply_b = self._worked_dom(cid_b)
        b.wait(sel_a, timeout=30)
        b.wait(sel_b, timeout=30)

        # Choose an alternative on both cards.
        for cid, sel in ((cid_a, sel_a), (cid_b, sel_b)):
            b.js(f"{sel}.value = {json.dumps(chosen[cid])}; "
                 f"{sel}.dispatchEvent(new Event('change'))")
        self.assertTrue(b.js(
            f"!{apply_a}.disabled && !{apply_b}.disabled"))

        # A full re-render (tab switch, same as a poll refresh) must
        # not drop either unsaved choice.
        b.js("document.querySelector("
             "'#ws-tabs [data-tab=overview]').click()")
        b.js("document.querySelector("
             "'#ws-tabs [data-tab=feedback]').click()")
        b.wait(sel_a, timeout=30)
        b.wait(sel_b, timeout=30)
        self.assertEqual(b.js(f"{sel_a}.value"), chosen[cid_a])
        self.assertEqual(b.js(f"{sel_b}.value"), chosen[cid_b])
        self.assertTrue(b.js(
            f"!{apply_a}.disabled && !{apply_b}.disabled"))
        for fs in (fs_a, fs_b):
            text = b.js(f"{fs}.textContent")
            self.assertNotIn("Worked example updated", text)
            self.assertNotIn("Rebuilding", text)

        # Hold the first override POST so the in-flight batch is
        # observable; the second POST must then run without another
        # click.
        b.js(
            "window.__workedRelease = null; window.__workedHold = true;"
            "const __origFetch = window.fetch.bind(window);"
            "window.fetch = (input, init) => {"
            "  const u = String(input);"
            "  if (u.indexOf('worked-example-selection') !== -1"
            "      && window.__workedHold) {"
            "    window.__workedHold = false;"
            "    const req = __origFetch(input, init);"
            "    return req.then((res) => new Promise((resolve) => {"
            "      window.__workedRelease = () => resolve(res);"
            "    }));"
            "  }"
            "  return __origFetch(input, init);"
            "};")
        clicked = (apply_a, apply_b)[click_index]
        b.js(f"{clicked}.click()")
        b.wait("window.__workedRelease !== null", timeout=30)
        # While the first rebuild is in flight the whole session is
        # busy, and both pending choices are kept.
        self.assertTrue(b.js(f"{fs_a}.disabled && {fs_b}.disabled"))
        self.assertEqual(b.js(f"{sel_a}.value"), chosen[cid_a])
        self.assertEqual(b.js(f"{sel_b}.value"), chosen[cid_b])
        self.assertIn("Rebuilding", b.js(f"{fs_a}.textContent"))
        self.assertIn("Waiting to update",
                      b.js(f"{fs_b}.textContent"))

        # Resolve the first request; the batch continues on its own
        # until both history entries exist.
        b.js("window.__workedRelease()")
        self.assertTrue(wait_for(
            lambda: len(self.service._load_meta(sid).get(
            "worked_example_selection_history", [])) == 2,
            timeout=15))
        history = self.service._load_meta(sid)[
            "worked_example_selection_history"]
        self.assertEqual({entry["candidate_id"] for entry in history},
                         {cid_a, cid_b})
        b.wait(f"!{fs_a}.disabled && !{fs_b}.disabled", timeout=30)

        # The authoritative preview carries both chosen questions and
        # nothing else changed.
        after = self.service.teacher_session(sid)["feedback_delivery"]
        preview_cards = {c["candidate_id"]: c["question_id"]
                         for c in after["preview"]["review_examples"]}
        self.assertEqual(preview_cards[cid_a], chosen[cid_a])
        self.assertEqual(preview_cards[cid_b], chosen[cid_b])
        self.assertEqual([dict(s) for s in after["preview"]["sections"]],
                         initial_sections)
        self.assertEqual(json.loads(json.dumps(
            after["preview"].get("practice_activities"))),
            initial_practice)

        # The rendered review-example cards match the final preview:
        # each chosen question's prompt, selected option and worked
        # steps appear in the DOM.
        preview_text = (
            "document.querySelector('.feedback-preview') && "
            "document.querySelector('.feedback-preview').textContent")
        for cid in (cid_a, cid_b):
            target = targets[cid]
            expected_prompt = b.js(
                "englishQuestionText(" +
                json.dumps({"text": target["prompt"]}) + ")")
            expected_selected = b.js(
                "englishOptionText(" +
                json.dumps(target["selected_answer"]) + ")")
            self.assertTrue(wait_for(
                lambda: expected_prompt in (b.js(preview_text) or "")
                and f"Your selected answer: {expected_selected}"
                in b.js(preview_text), timeout=15))
        self.assertEqual(b.js(
            "document.querySelectorAll('.review-example').length"),
            len(after["preview"]["review_examples"]))
        self.assertGreaterEqual(b.js(
            "document.querySelectorAll("
            "'.review-example ol.worked-steps li').length"), 2)

        # Both cards report the scoped outcome and neither button can
        # be clicked again: no pending choices remain.
        self.assertEqual(b.js(f"{sel_a}.value"), chosen[cid_a])
        self.assertEqual(b.js(f"{sel_b}.value"), chosen[cid_b])
        self.assertIn("Worked example updated",
                      b.js(f"{fs_a}.textContent"))
        self.assertIn("Worked example updated",
                      b.js(f"{fs_b}.textContent"))
        self.assertTrue(b.js(
            f"{apply_a}.disabled && {apply_b}.disabled"))

        # Approval releases a snapshot carrying both chosen examples.
        approve = ("[...document.querySelectorAll('button.primary')]"
                   ".find(b => b.textContent.startsWith('Approve'))")
        b.wait(f"Boolean({approve} && !{approve}.disabled)",
               timeout=30)
        b.js(f"{approve}.click()")
        b.wait("document.body.textContent.includes('Released to the "
               "student')", timeout=30)
        released = self.service.teacher_session(sid)[
            "feedback_delivery"]["released_feedback"]
        released_cards = {c["candidate_id"]: c["question_id"]
                          for c in released["review_examples"]}
        self.assertEqual(released_cards[cid_a], chosen[cid_a])
        self.assertEqual(released_cards[cid_b], chosen[cid_b])
        self.assertEqual(b.errors, [])

    def test_two_worked_example_choices_apply_from_first_button(self):
        self._run_two_choice_batch_apply(0)

    def test_two_worked_example_choices_apply_from_second_button(self):
        self._run_two_choice_batch_apply(1)

    def test_two_worked_example_choices_partial_conflict_retry(self):
        # When the second POST of the batch conflicts, the applied
        # first selection stays, the second card keeps its pending
        # choice, and a retry resends only the unapplied remainder.
        (sid, choices, chosen, targets, initial_sections,
         initial_practice) = self._two_choice_fixture()
        b = self.browser
        self._open_teacher_feedback()
        cid_a, cid_b = (choices[0]["candidate_id"],
                        choices[1]["candidate_id"])
        sel_a, fs_a, apply_a = self._worked_dom(cid_a)
        sel_b, fs_b, apply_b = self._worked_dom(cid_b)
        b.wait(sel_a, timeout=30)
        b.wait(sel_b, timeout=30)
        for cid, sel in ((cid_a, sel_a), (cid_b, sel_b)):
            b.js(f"{sel}.value = {json.dumps(chosen[cid])}; "
                 f"{sel}.dispatchEvent(new Event('change'))")
        self.assertTrue(b.js(
            f"!{apply_a}.disabled && !{apply_b}.disabled"))

        # Record every worked-example POST body and fail the second
        # request of the batch with a synthetic 409.
        b.js(
            "window.__workedBodies = [];"
            "window.__workedFailSecond = true;"
            "const __origFetch = window.fetch.bind(window);"
            "window.fetch = (input, init) => {"
            "  const u = String(input);"
            "  if (u.indexOf('worked-example-selection') !== -1) {"
            "    window.__workedBodies.push("
            "      init && init.body ? JSON.parse(init.body) : null);"
            "    if (window.__workedFailSecond"
            "        && window.__workedBodies.length === 2) {"
            "      window.__workedFailSecond = false;"
            "      return Promise.resolve(new Response("
            "        JSON.stringify({error: 'conflict'}),"
            "        {status: 409, headers: {"
            "          'Content-Type': 'application/json'}}));"
            "    }"
            "  }"
            "  return __origFetch(input, init);"
            "};")
        b.js(f"{apply_a}.click()")
        self.assertTrue(wait_for(
            lambda: len(self.service._load_meta(sid).get(
            "worked_example_selection_history", [])) == 1,
            timeout=15))
        b.wait(f"!{fs_a}.disabled && !{fs_b}.disabled", timeout=30)

        # Card A applied for real; card B kept its pending choice and
        # carries the scoped conflict note. No history for card B.
        self.assertTrue(wait_for(
            lambda: "before all selections were applied" in b.js(
                f"{fs_b}.textContent"), timeout=15))
        self.assertIn("Worked example updated",
                      b.js(f"{fs_a}.textContent"))
        self.assertNotIn("before all selections",
                         b.js(f"{fs_a}.textContent"))
        self.assertEqual(b.js(f"{sel_a}.value"), chosen[cid_a])
        self.assertEqual(b.js(f"{sel_b}.value"), chosen[cid_b])
        self.assertEqual(
            len(self.service._load_meta(sid).get(
                "worked_example_selection_history", [])), 1)
        partial = self.service.teacher_session(sid)["feedback_delivery"]
        partial_cards = {c["candidate_id"]: c["question_id"]
                         for c in partial["preview"]["review_examples"]}
        self.assertEqual(partial_cards[cid_a], chosen[cid_a])
        self.assertEqual(partial_cards[cid_b],
                         choices[1]["selected_question_id"])
        self.assertEqual(b.js("window.__workedBodies.length"), 2)
        self.assertEqual(b.js("window.__workedBodies[0].candidate_id"),
                         cid_a)
        self.assertEqual(b.js("window.__workedBodies[1].candidate_id"),
                         cid_b)
        self.assertFalse(b.js(f"{apply_b}.disabled"))

        # Retry through the other card's button: only the unapplied
        # remainder is resent, and both cards land correctly.
        b.js(f"{apply_b}.click()")
        self.assertTrue(wait_for(
            lambda: len(self.service._load_meta(sid).get(
            "worked_example_selection_history", [])) == 2,
            timeout=15))
        self.assertEqual(b.js("window.__workedBodies.length"), 3)
        self.assertEqual(b.js("window.__workedBodies[2].candidate_id"),
                         cid_b)
        self.assertEqual(b.js("window.__workedBodies[2].question_id"),
                         chosen[cid_b])
        b.wait(f"!{fs_a}.disabled && !{fs_b}.disabled", timeout=30)
        after = self.service.teacher_session(sid)["feedback_delivery"]
        preview_cards = {c["candidate_id"]: c["question_id"]
                         for c in after["preview"]["review_examples"]}
        self.assertEqual(preview_cards[cid_a], chosen[cid_a])
        self.assertEqual(preview_cards[cid_b], chosen[cid_b])
        self.assertEqual([dict(s) for s in after["preview"]["sections"]],
                         initial_sections)
        self.assertEqual(b.js(f"{sel_a}.value"), chosen[cid_a])
        self.assertEqual(b.js(f"{sel_b}.value"), chosen[cid_b])
        self.assertIn("Worked example updated",
                      b.js(f"{fs_a}.textContent"))
        self.assertIn("Worked example updated",
                      b.js(f"{fs_b}.textContent"))
        self.assertTrue(b.js(
            f"{apply_a}.disabled && {apply_b}.disabled"))
        self.assertEqual(b.errors, [])

    def test_hosted_scoped_example_processing_and_fallback(self):
        # Hosted flow: blocking scoped adapters hold each card-level
        # job pending so the waiting UI is observable; a single click
        # applies both pending dropdown choices, queueing the second
        # scoped job only after the first one completes.
        gates = [threading.Event(), threading.Event()]
        scoped_calls = []

        class Scoped:
            execution = None
            last_rejected_draft = None

            def generate(self, card):
                idx = len(scoped_calls)
                scoped_calls.append(card["question_id"])
                gates[idx].wait(15)
                return {"question_id": card["question_id"],
                        "steps": [f"Provider step for "
                                  f"{card['question_id']}."],
                        "reasoning_question": "Provider check?"}

        class Full:
            supports_full_feedback = True
            execution = None

            def generate(self, payload):
                return {
                    "candidate_id": payload["selected_candidate"]
                    ["candidate_id"],
                    "sections": dict(VALID_SECTIONS),
                    "worked_reviews": fixture_reviews(payload)}

        # Fresh bank: the shared fixture's injected markup drops one
        # candidate's checked solution; this flow needs two usable
        # alternatives.
        bank = make_grounded_bank()
        taxonomy = make_grounded_taxonomy(bank)
        service = DemoService(
            root=Path(self.tmp.name) / "hosted", bank=bank,
            taxonomy=taxonomy,
            provider_mode="hosted", generator=Full(),
            worked_example_generator=Scoped(),
            diagnostics=LiveDiagnostics(
                model_loader=lambda: FakeKT(bank)))
        self.addCleanup(service.close, wait=True)
        pin = f"{secrets.randbelow(1000000):06d}"
        server = make_server(service, pin)
        thread = threading.Thread(
            target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"

        # Two wrong answers per skill give BOTH choosers an eligible
        # alternative; both are preselected and applied by a single
        # click on the second card's button.
        wrong = (positions(bank, "pct", 0)[:2]
                 + positions(bank, "alg", 0)[:2])
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in grounded_rows(bank, wrong):
            service.submit_response(sid, token, row)
        deadline = time.time() + 20
        while time.time() < deadline:
            if service._load_meta(sid)["provider_job"]["status"] in (
                    "ready", "fallback"):
                break
            time.sleep(0.05)
        else:
            self.fail("provider job did not finish")
        detail = service.teacher_session(sid)
        delivery = detail["feedback_delivery"]
        before_sections = [s["text"]
                           for s in delivery["preview"]["sections"]]
        all_choices = delivery["worked_example_choices"]
        self.assertEqual(len(all_choices), 2)
        choice, second_choice = all_choices[0], all_choices[1]
        for candidate_choice in (choice, second_choice):
            self.assertTrue(any(
                cand["question_id"]
                != candidate_choice["selected_question_id"]
                and cand.get("checked_solution_available")
                for cand in candidate_choice["candidates"]))
        target = next(
            c for c in choice["candidates"]
            if c["question_id"] != choice["selected_question_id"]
            and c.get("checked_solution_available"))
        second_target = next(
            c for c in second_choice["candidates"]
            if c["question_id"] != second_choice["selected_question_id"]
            and c.get("checked_solution_available"))

        b = self.browser
        b.command("Page.navigate", {"url": f"{url}/teacher"})
        b.wait("document.readyState === 'complete'")
        b.wait("document.querySelector('#pin-input')")
        b.js(f"document.querySelector('#pin-input').value="
             f"{json.dumps(pin)}; "
             "document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelectorAll('.session-row').length >= 1",
               timeout=30)
        b.js("document.querySelectorAll('.session-row')[0].click()")
        b.wait("document.querySelector('#ws-tabs [data-tab=feedback]')",
               timeout=30)
        b.js("document.querySelector("
             "'#ws-tabs [data-tab=feedback]').click()")
        cid = json.dumps(choice["candidate_id"])
        cid2 = json.dumps(second_choice["candidate_id"])
        sel = ("document.querySelector("
               f"'.worked-example-select[data-candidate-id={cid}]')")
        sel2 = ("document.querySelector("
                f"'.worked-example-select[data-candidate-id={cid2}]')")
        b.wait(sel, timeout=30)
        b.wait(sel2, timeout=30)
        preview_text = ("document.querySelector('.feedback-preview')"
                        " && document.querySelector("
                        "'.feedback-preview').textContent")
        self.assertIn(before_sections[0], b.js(preview_text))
        # Preselect both alternatives, then click the SECOND card's
        # button: the batch still runs in displayed order, so the
        # first card's scoped job is admitted before the second.
        b.js(f"{sel2}.value = "
             f"{json.dumps(second_target['question_id'])}; "
             f"{sel2}.dispatchEvent(new Event('change'))")
        apply2_btn = (f"{sel2}.closest('fieldset')"
                      ".querySelector('.worked-example-apply')")
        b.js(f"{sel}.value = {json.dumps(target['question_id'])}; "
             f"{sel}.dispatchEvent(new Event('change'))")
        apply_btn = (f"{sel}.closest('fieldset')"
                     ".querySelector('.worked-example-apply')")
        self.assertFalse(b.js(f"{apply_btn}.disabled"))
        self.assertFalse(b.js(f"{apply2_btn}.disabled"))
        b.js(f"{apply2_btn}.click()")
        # First scoped pending: waiting panel replaces preview and
        # approval, and reports the scoped status row. While the gate
        # holds, no second scoped call may start.
        b.wait("document.querySelector('.end-feedback-waiting')",
               timeout=30)
        self.assertEqual(
            b.js("document.querySelector('.end-feedback-waiting')"
                 ".getAttribute('role')"), "status")
        self.assertEqual(
            b.js("document.querySelector('.end-feedback-waiting')"
                 ".getAttribute('aria-busy')"), "true")
        self.assertIn("Aitta is updating the selected worked example",
                      b.js("document.querySelector("
                           "'.end-feedback-waiting').textContent"))
        self.assertFalse(b.js(
            "Boolean(document.querySelector('.feedback-preview'))"))
        self.assertFalse(b.js(
            "Boolean(document.querySelector("
            "'details[data-disclosure-key=feedback-editing]'))"))
        self.assertFalse(b.js(
            "Boolean([...document.querySelectorAll('button.primary')]"
            ".find(n => n.textContent.startsWith('Approve')))"))
        self.assertTrue(wait_for(
            lambda: "Worked example generation" in b.js(
                "document.querySelector('.release-status')"
                ".textContent"), timeout=10))
        self.assertEqual(scoped_calls, [target["question_id"]])
        time.sleep(0.5)
        self.assertEqual(scoped_calls, [target["question_id"]])
        b.js("document.querySelector('.end-feedback-waiting')"
             ".scrollIntoView({block:'center'})")
        b.screenshot("worked-example-scoped-pending")

        # Release the first gate: the batch waits for the terminal
        # job, then queues the second scoped call on its own.
        gates[0].set()
        self.assertTrue(wait_for(
            lambda: scoped_calls == [target["question_id"],
                                     second_target["question_id"]],
            timeout=30))
        # The second job is pending again: preview and approval are
        # still withheld behind the waiting panel.
        self.assertTrue(wait_for(
            lambda: b.js("Boolean(document.querySelector("
                         "'.end-feedback-waiting'))"), timeout=30))
        self.assertFalse(b.js(
            "Boolean(document.querySelector('.feedback-preview'))"))
        self.assertFalse(b.js(
            "Boolean([...document.querySelectorAll('button.primary')]"
            ".find(n => n.textContent.startsWith('Approve')))"))

        # Release the second gate: each generated step lands on its
        # own card and the narrative is preserved.
        gates[1].set()
        first_step = f"Provider step for {target['question_id']}."
        second_step = (f"Provider step for "
                       f"{second_target['question_id']}.")
        self.assertTrue(wait_for(
            lambda: first_step in (b.js(preview_text) or "")
            and second_step in (b.js(preview_text) or ""), timeout=30))
        self.assertIn(before_sections[0], b.js(preview_text))
        after = service.teacher_session(sid)["feedback_delivery"]
        self.assertEqual(
            [s["text"] for s in after["preview"]["sections"]],
            before_sections)
        preview_cards = {c["candidate_id"]: c["question_id"]
                         for c in after["preview"]["review_examples"]}
        self.assertEqual(preview_cards[choice["candidate_id"]],
                         target["question_id"])
        self.assertEqual(preview_cards[second_choice["candidate_id"]],
                         second_target["question_id"])
        meta = service._load_meta(sid)
        job = meta["checkpoints"]["end"]["worked_example_job"]
        self.assertEqual(job["status"], "ready")
        self.assertEqual(
            len(meta["worked_example_selection_history"]), 2)
        # Busy cleared without a stale awaited hash: both cards show
        # their chosen question and the observed outcome note.
        b.wait(f"{sel} && !{sel}.closest('fieldset').disabled"
               f" && {sel2} && !{sel2}.closest('fieldset').disabled",
               timeout=30)
        self.assertEqual(b.js(f"{sel}.value"), target["question_id"])
        self.assertEqual(b.js(f"{sel2}.value"),
                         second_target["question_id"])
        first_text = b.js(f"{sel}.closest('fieldset').textContent")
        self.assertIn("explanation was regenerated", first_text)
        self.assertNotIn("being generated", first_text)
        second_text = b.js(
            f"{sel2}.closest('fieldset').textContent")
        self.assertIn("explanation was regenerated", second_text)

        # A failing scoped adapter publishes the explicit checked
        # fallback warning, still without touching the narrative.
        class Failing:
            execution = None
            last_rejected_draft = None

            def generate(self, card):
                raise RuntimeError("provider blew up")

        service.worked_example_generator = Failing()
        another = next(
            c for c in choice["candidates"]
            if c["question_id"] != target["question_id"]
            and c.get("checked_solution_available"))
        b.js(f"{sel}.value = {json.dumps(another['question_id'])}; "
             f"{sel}.dispatchEvent(new Event('change'))")
        b.wait(f"{apply_btn} && !{apply_btn}.disabled", timeout=15)
        b.js(f"{apply_btn}.click()")
        self.assertTrue(wait_for(
            lambda: "AI generation for the selected worked example "
                    "did not complete" in b.js(
                "document.querySelector('.release-status')"
                ".textContent"), timeout=30))
        self.assertTrue(b.js(
            f"Boolean({sel} && "
            f"{sel}.value === {json.dumps(another['question_id'])})"))
        card = next(
            c for c in service._load_meta(sid)["checkpoints"]["end"]
            ["student_review"]["review_examples"]
            if c["question_id"] == another["question_id"])
        self.assertEqual(card["explanation_source"], "application")
        # The chooser note also reports the checked-steps fallback.
        self.assertIn("AI generation did not complete", b.js(
            "document.querySelector('.worked-example-choice')"
            ".textContent"))
        self.assertEqual(b.errors, [])

    def test_scoped_second_pending_ignores_stale_detail(self):
        # A second accepted pending update must stay busy even when a
        # stale teacher detail carrying the previous terminal job is
        # served back; busy clears only on the new job's terminal or a
        # strictly newer superseding job.
        gates = [threading.Event(), threading.Event()]
        scoped_calls = []

        class Scoped:
            execution = None
            last_rejected_draft = None

            def generate(self, card):
                idx = len(scoped_calls)
                scoped_calls.append(card["question_id"])
                gates[idx].wait(15)
                return {"question_id": card["question_id"],
                        "steps": [f"Scoped step {idx + 1}."],
                        "reasoning_question": "Scoped check?"}

        class Full:
            supports_full_feedback = True
            execution = None

            def generate(self, payload):
                return {
                    "candidate_id": payload["selected_candidate"]
                    ["candidate_id"],
                    "sections": dict(VALID_SECTIONS),
                    "worked_reviews": fixture_reviews(payload)}

        bank = make_grounded_bank()
        service = DemoService(
            root=Path(self.tmp.name) / "hosted2", bank=bank,
            taxonomy=make_grounded_taxonomy(bank),
            provider_mode="hosted", generator=Full(),
            worked_example_generator=Scoped(),
            diagnostics=LiveDiagnostics(
                model_loader=lambda: FakeKT(bank)))
        self.addCleanup(service.close, wait=True)
        pin = f"{secrets.randbelow(1000000):06d}"
        server = make_server(service, pin)
        thread = threading.Thread(
            target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"

        wrong = (positions(bank, "pct", 0)[:2]
                 + [positions(bank, "alg", 0)[0]])
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in grounded_rows(bank, wrong):
            service.submit_response(sid, token, row)
        deadline = time.time() + 20
        while time.time() < deadline:
            if service._load_meta(sid)["provider_job"]["status"] in (
                    "ready", "fallback"):
                break
            time.sleep(0.05)
        else:
            self.fail("provider job did not finish")
        delivery = service.teacher_session(sid)["feedback_delivery"]
        choice = next(
            c for c in delivery["worked_example_choices"]
            if sum(cand.get("checked_solution_available")
                   for cand in c["candidates"]) > 1)
        cands = [c for c in choice["candidates"]
                 if c.get("checked_solution_available")]
        first = next(c for c in cands
                     if c["question_id"]
                     != choice["selected_question_id"])
        second = next(c for c in cands
                      if c["question_id"] != first["question_id"])

        b = self.browser
        b.command("Page.navigate", {"url": f"{url}/teacher"})
        b.wait("document.readyState === 'complete'")
        b.wait("document.querySelector('#pin-input')")
        b.js(f"document.querySelector('#pin-input').value="
             f"{json.dumps(pin)}; "
             "document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        b.wait("document.querySelectorAll('.session-row').length >= 1",
               timeout=30)
        b.js("document.querySelectorAll('.session-row')[0].click()")
        b.js("document.querySelector("
             "'#ws-tabs [data-tab=feedback]').click()")
        cid = json.dumps(choice["candidate_id"])
        sel = ("document.querySelector("
               f"'.worked-example-select[data-candidate-id={cid}]')")
        b.wait(sel, timeout=30)

        def apply(question_id):
            b.js(f"{sel}.value = {json.dumps(question_id)}; "
                 f"{sel}.dispatchEvent(new Event('change'))")
            apply_btn = (f"{sel}.closest('fieldset')"
                         ".querySelector('.worked-example-apply')")
            b.wait(f"{apply_btn} && !{apply_btn}.disabled",
                   timeout=15)
            b.js(f"{apply_btn}.click()")

        # First override: pending panel, then completion.
        apply(first["question_id"])
        b.wait("document.querySelector('.end-feedback-waiting')",
               timeout=30)
        gates[0].set()
        b.wait("document.querySelector('.feedback-preview')",
               timeout=30)
        job1 = (service._load_meta(sid)["checkpoints"]["end"]
                ["worked_example_job"])
        self.assertEqual(job1["status"], "ready")

        # A stale detail snapshot carrying job1 terminal is served
        # once during the second accepted pending update.
        stale = json.loads(json.dumps(
            service.teacher_session(sid)))
        stale["feedback_delivery"]["preview"]["sections"][0][
            "text"] += " STALE-MARKER"
        b.js(
            "window.__stale = " + json.dumps(stale) + ";"
            "window.__staleServe = false; window.__staleCount = 0;"
            "const __orig = window.fetch.bind(window);"
            "window.fetch = (input, init) => {"
            f"  const u = String(input);"
            f"  if (window.__staleServe && u.endsWith("
            f"      '/api/teacher/sessions/{sid}')"
            "      && (!init || !init.method"
            "          || init.method === 'GET')) {"
            "    window.__staleCount += 1;"
            "    return Promise.resolve(new Response("
            "      JSON.stringify(window.__stale),"
            "      {headers: {'Content-Type': 'application/json'}}));"
            "  }"
            "  return __orig(input, init);"
            "};")
        b.js("window.__staleServe = true")
        apply(second["question_id"])
        # The stale detail is served and rendered while job2 is
        # pending; busy must persist on the local accepted job.
        self.assertTrue(wait_for(
            lambda: b.js("window.__staleCount >= 1"), timeout=15))
        time.sleep(0.5)
        self.assertTrue(b.js(
            "Boolean(document.querySelector('.end-feedback-waiting'))"))
        self.assertFalse(b.js(
            "Boolean(document.querySelector('.feedback-preview'))"))
        self.assertFalse(b.js(
            "Boolean([...document.querySelectorAll('button.primary')]"
            ".find(n => n.textContent.startsWith('Approve')))"))
        self.assertNotIn("STALE-MARKER", b.js(
            "document.body.textContent"))
        # Real pending detail arrives; still busy. Completion clears.
        b.js("window.__staleServe = false")
        gates[1].set()
        self.assertTrue(wait_for(
            lambda: "Scoped step 2." in (b.js(
                "document.querySelector('.feedback-preview')"
                " && document.querySelector('.feedback-preview')"
                ".textContent") or ""), timeout=30))
        b.wait(f"{sel} && !{sel}.closest('fieldset').disabled",
               timeout=30)
        job2 = (service._load_meta(sid)["checkpoints"]["end"]
                ["worked_example_job"])
        self.assertEqual(job2["status"], "ready")
        self.assertNotEqual(job2["job_id"], job1["job_id"])
        self.assertEqual(scoped_calls, [first["question_id"],
                                        second["question_id"]])
        self.assertEqual(b.errors, [])

    def test_worked_example_chooser_english_display_text(self):
        # Chooser legend and option prompts render through the same
        # English display helpers as the feedback cards; candidate
        # ids, selection flags and the served payload stay raw.
        sid, token, cards = self._complete_pending()
        delivery = self.service.teacher_session(sid)[
            "feedback_delivery"]
        choices = delivery["worked_example_choices"]
        self.assertTrue(choices)
        raw_prompts = {cand["question_id"]: cand["prompt"]
                       for ch in choices for cand in ch["candidates"]}
        expected = {ch["candidate_id"]: ch for ch in choices}
        original = self.service.teacher_session
        injected = [
            "100 % luvusta 75",
            "Sievenn\u00e4 lauseke:\\n\\n5v + 2v",
            "<img src=x onerror=alert(1)>"]

        def patched(session_id):
            v = original(session_id)
            if session_id != sid:
                return v
            chs = v["feedback_delivery"].get(
                "worked_example_choices") or []
            i = 0
            for idx, ch in enumerate(chs):
                ch["subtopic_name"] = (
                    "Samanmuotoisten termien yhdist\u00e4minen")
                if idx == 0 and ch["candidates"]:
                    clone = dict(ch["candidates"][0])
                    clone["question_id"] = "injected_img_q"
                    clone["prompt"] = injected[2]
                    clone["checked_solution_available"] = True
                    ch["candidates"] = list(ch["candidates"]) + [clone]
                for cand in ch["candidates"]:
                    if cand["question_id"] != "injected_img_q":
                        cand["prompt"] = injected[i % 2]
                        i += 1
            return v

        b = self.browser
        self.service.teacher_session = patched
        try:
            b.command("Page.navigate", {"url": f"{self.url}/teacher"})
            b.wait("document.readyState === 'complete'")
            b.wait("document.querySelector('#pin-input')")
            b.js(f"document.querySelector('#pin-input').value="
                 f"{json.dumps(self.pin)}; "
                 "document.querySelector('#unlock-form')"
                 ".requestSubmit()")
            b.wait("!document.querySelector('#teacher-app').hidden")
            b.wait("document.querySelector('.session-row')")
            b.js("document.querySelector('.session-row').click()")
            b.js("document.querySelector("
                 "'#ws-tabs [data-tab=feedback]').click()")
            selects = ("[...document.querySelectorAll("
                       "'.worked-example-select')]")
            b.wait(f"{selects}.length === {len(expected)}", timeout=30)
            for text in json.loads(b.js(
                    "JSON.stringify([...document.querySelectorAll("
                    "'.worked-example-choice legend')]"
                    ".map((n) => n.textContent))")):
                self.assertEqual(
                    text,
                    "Worked example \u2014 Combining like terms")
            sel_data = json.loads(b.js(
                f"JSON.stringify({selects}.map((s) => ({{"
                "cid: s.dataset.candidateId, value: s.value,"
                " options: [...s.options].map((o) => ({value: o.value,"
                " text: o.textContent, selected: o.selected}))"
                " })))"))
            for sdata in sel_data:
                ch = expected[sdata["cid"]]
                self.assertEqual(sdata["value"],
                                 ch["selected_question_id"])
                for opt in sdata["options"]:
                    cand = next(
                        (c for c in ch["candidates"]
                         if c["question_id"] == opt["value"]), None)
                    self.assertTrue(
                        opt["text"].startswith("Question "))
                    if cand is None:
                        continue
                    if opt["value"] == ch["recommended_question_id"]:
                        self.assertIn("(recommended)", opt["text"])
                    if not cand["checked_solution_available"]:
                        self.assertIn("(no checked solution)",
                                      opt["text"])
                    self.assertEqual(
                        opt["selected"],
                        opt["value"] == ch["selected_question_id"])
                    self.assertNotIn("\n", opt["text"])
                    self.assertNotIn("\\n", opt["text"])
                    for marker in ("luvusta", "Sievenn"):
                        self.assertNotIn(marker, opt["text"])
            all_text = b.js(
                "[...document.querySelectorAll("
                "'.worked-example-select option')]"
                ".map((o) => o.textContent).join('|')")
            self.assertIn("What is 100% of 75?", all_text)
            self.assertIn("Simplify: 5v + 2v", all_text)
            # An unknown string is shown literally, never as HTML.
            self.assertIn("<img src=x onerror=alert(1)>", all_text)
            self.assertIsNone(b.js(
                "document.querySelector('#tab-feedback img')"))
            b.viewport(390, 844)
            b.js(f"{selects}[0].scrollIntoView({{block:'center'}})")
            self.assertFalse(b.js(
                "document.documentElement.scrollWidth > "
                "document.documentElement.clientWidth"))
            b.screenshot("worked-chooser-english-390.png")
            b.viewport(1440, 1100)
            b.js(f"{selects}[0].scrollIntoView({{block:'center'}})")
            b.screenshot("worked-chooser-english-1440.png")
        finally:
            self.service.teacher_session = original
        # Display-only: the served payload still carries raw prompts.
        fresh = self.service.teacher_session(sid)[
            "feedback_delivery"]["worked_example_choices"]
        self.assertEqual(
            {cand["question_id"]: cand["prompt"]
             for ch in fresh for cand in ch["candidates"]},
            raw_prompts)
        self.assertEqual(b.errors, [])

    def test_legacy_released_snapshot_key_is_ignored(self):
        b = self.browser
        marker = "LEGACY-RELEASED-SNAPSHOT-MARKER-9f3b"
        b.command("Page.navigate", {"url": f"{self.url}/"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.js(
            "localStorage.setItem('demoReleasedSnapshot', "
            "JSON.stringify({session_id:'legacy-sid', synthetic:true,"
            " bank_mode:'demo', bank_label:'Demo bank',"
            " status:'complete', answered_count:40,"
            " assessment_total:{correct:39,out_of:40},"
            " current_question:null,"
            " feedback:{midpoint:null, end:{sections:[{kind:"
            "'assessment_summary', text:" + json.dumps(marker)
            + "}]}},"
            " feedback_delivery:{status:'released'},"
            " provider_job:{status:'ready'}}))")
        # The stored release is never rendered: the page shows setup on
        # first load and after reload, and no end content appears.
        b.command("Page.reload", {"ignoreCache": True})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.wait("!document.querySelector('#start-panel').hidden")
        self.assertTrue(b.js(
            "document.querySelector('#end-panel').hidden"))
        body = b.js("document.body.textContent")
        self.assertNotIn(marker, body)
        self.assertNotIn("39 of 40", body)
        self.assertFalse(b.js(
            "document.querySelector('#end-summary').textContent"
            ".includes('questions correctly')"))
        b.command("Page.reload", {"ignoreCache": True})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.wait("!document.querySelector('#start-panel').hidden")
        self.assertNotIn(marker, b.js("document.body.textContent"))
        self.assertEqual(b.errors, [])

    def test_release_view_writes_no_snapshot_and_keeps_token_link(self):
        b = self.browser
        # An unrelated in-progress credential must survive viewing and
        # releasing feedback in the educator workspace. It is seeded on
        # the teacher page, which never reads or clears demoSession.
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.js("localStorage.setItem('demoSession', JSON.stringify("
             "{sid:'unrelated-sid', token:'unrelated-token'}))")
        b.wait("document.querySelector('#unlock-panel')")
        b.js(f"document.querySelector('#pin-input').value="
             f"{json.dumps(self.pin)}; "
             "document.querySelector('#unlock-form').requestSubmit()")
        b.wait("!document.querySelector('#teacher-app').hidden")
        # UI-driven simulation keeps its tokenized "Open student view"
        # link for answering the assessment.
        b.js("document.querySelector('#sim-form').requestSubmit()")
        b.wait("!document.querySelector('#ws-detail').hidden",
               timeout=30)
        b.wait("Boolean(document.querySelector('#ws-student-link a'))",
               timeout=30)
        sim_link = b.js(
            "document.querySelector('#ws-student-link a').href")
        self.assertIn("session=", sim_link)
        self.assertIn("token=", sim_link)
        sid = sim_link.split("session=")[1].split("&")[0].split("#")[0]
        self.assertTrue(wait_for(
            lambda: self.service._load_meta(sid)["provider_job"]
            ["status"] in ("ready", "fallback")))
        view = self.service.teacher_session(sid)
        released = self.service.release_feedback(sid, {
            "message_sha256": view["feedback_delivery"]
            ["preview_sha256"], "reviewer_label": "browser-test"})
        self.assertTrue(released["released"])
        release = "document.querySelector('#tab-feedback .release-card')"
        b.js("document.querySelector('#ws-tabs [data-tab=feedback]')"
             ".click()")
        b.wait(f"{release}.textContent.includes('Released to the "
               "student')", timeout=30)
        # Viewing or releasing educator feedback writes no student
        # snapshot and leaves the unrelated credential untouched.
        self.assertIsNone(b.js(
            "localStorage.getItem('demoReleasedSnapshot')"))
        self.assertEqual(
            b.js("JSON.parse(localStorage.getItem('demoSession'))"),
            {"sid": "unrelated-sid", "token": "unrelated-token"})
        # No fallback link opens released feedback on the assessment
        # page; only the tokenized simulation link remains.
        links = b.js(
            "[...document.querySelectorAll('#ws-detail a, "
            "#tab-feedback a')].map((a) => a.getAttribute('href'))")
        self.assertNotIn("/", links)
        self.assertNotIn(
            "Open in Assessment view",
            b.js("document.body.textContent"))
        # The tokenized link opens a completed session that shows the
        # score and the educator-workspace note, never released content.
        b.command("Page.navigate", {"url": sim_link})
        b.wait("document.readyState === 'complete' && "
               "typeof api === 'function'")
        b.wait("!document.querySelector('#end-panel').hidden",
               timeout=30)
        text = b.js(
            "document.querySelector('#end-summary').textContent")
        self.assertIn("questions correctly", text)
        self.assertIn("Educator workspace", text)
        self.assertNotIn("Assessment summary", text)
        self.assertFalse(b.js(
            "Boolean(document.querySelector('.review-example, "
            "#end-summary .practice-activity, "
            "#end-summary .feedback-section'))"))
        self.assertIsNone(b.js(
            "localStorage.getItem('demoReleasedSnapshot')"))
        self.assertEqual(b.errors, [])

    def test_rendered_feedback_dedup_disclaimer_injection(self):
        b = self.browser
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete'")
        payload = {
            "review_examples": [
                {"source": "incorrect_assessment_response",
                 "question_id": "q1", "prompt": "Alkuluku?",
                 "teacher_edited": True,
                 "options": ["2 <img src=x onerror=alert(1)>",
                             "4", "9"],
                 "selected_answer": "4",
                 "correct_answer": "2 <img src=x onerror=alert(1)>",
                 "steps": ["Prime numbers have two divisors."],
                 "reasoning_question": "Explain your choice.",
                 "original_content": {
                     "prompt": "Alkuluku?",
                     "options": ["2 <img src=x onerror=alert(1)>",
                                 "4", "9"],
                     "selected_answer": "4",
                     "correct_answer":
                         "2 <img src=x onerror=alert(1)>",
                     "steps": ["Prime numbers have two divisors."],
                     "reasoning_question": "Explain your choice."},
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

    def test_math_text_is_readable_and_safe(self):
        b = self.browser
        b.command("Page.navigate", {"url": f"{self.url}/teacher"})
        b.wait("document.readyState === 'complete'")
        R_SQRT = r"\(\sqrt{4}\)"
        R_TEX = r"\(x\)"
        R_NESTED = r"\frac{1}{\frac{2}{3}}"
        payload = {
            "sections": [
                {"title": "Review focus",
                 "text": "Fraction work such as 3⁄6 is revisited below."}],
            "review_examples": [
                {"source": "incorrect_assessment_response",
                 "question_id": "q1",
                 "prompt": "3⁄6 · 10⁄1",
                 "options": ["30⁄6", "6⁄24"],
                 "selected_answer": "6⁄24",
                 "correct_answer": "30⁄6",
                 "steps": [
                     r"Multiply: \(\frac{3\times10}{6\times1}"
                     r"=\frac{30}{6}=5\).",
                     r"A mixed number example: \(3\tfrac{4}{5}\)."],
                 "reasoning_question":
                     r"Why is \(\frac{30}{6}\) equal to 5? "
                     "<img src=x onerror=alert(1)>"}],
            "practice_activities": [
                {"status": "selected",
                 "message": "Selected practice.",
                 "question": {"skill_name": "Fractions",
                              "concept_name": "Adding fractions",
                              "text": "1⁄2 + 1⁄4",
                              "options": ["3⁄4", "2⁄6"]}}]}
        result = b.js(
            "(() => { const host = document.createElement('div');"
            "document.body.appendChild(host);"
            f"const payload = {json.dumps(payload)};"
            "const before = JSON.stringify(payload);"
            "renderStructuredFeedback(host, payload);"
            "const text = host.textContent;"
            f"const ta = el('textarea', {json.dumps(R_TEX)});"
            "return {has36: text.includes('3/6'),"
            "has101: text.includes('10/1'),"
            "has306: text.includes('30/6'),"
            "hasGrouped: text.includes('(3 × 10)/(6 × 1)'),"
            "hasMixed: text.includes('3 4/5'),"
            "hasCalc: text.includes('Calculate: 1/2 + 1/4.'),"
            f"noFrac: !text.includes({json.dumps(chr(92) + 'frac')}),"
            f"noTfrac: !text.includes({json.dumps(chr(92) + 'tfrac')}),"
            f"noDelim: !text.includes({json.dumps(chr(92) + '(')}),"
            "imgLiteral: text.includes('<img src=x onerror=alert(1)>'),"
            "imgs: host.querySelectorAll('img').length,"
            "scripts: host.querySelectorAll('script').length,"
            "unchanged: JSON.stringify(payload) === before,"
            f"sqrtRaw: readableMathText({json.dumps(R_SQRT)}),"
            f"nested: readableMathText({json.dumps(R_NESTED)}),"
            "taRaw: ta.value}; })()")
        self.assertTrue(result["has36"])
        self.assertTrue(result["has101"])
        self.assertTrue(result["has306"])
        self.assertTrue(result["hasGrouped"])
        self.assertTrue(result["hasMixed"])
        self.assertTrue(result["hasCalc"])
        self.assertTrue(result["noFrac"])
        self.assertTrue(result["noTfrac"])
        self.assertTrue(result["noDelim"])
        self.assertTrue(result["imgLiteral"])
        self.assertEqual(result["imgs"], 0)
        self.assertEqual(result["scripts"], 0)
        self.assertTrue(result["unchanged"])
        self.assertEqual(result["sqrtRaw"], "\\(\\sqrt{4}\\)")
        self.assertEqual(result["nested"], "1/(2/3)")
        self.assertEqual(result["taRaw"], "\\(x\\)")
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
               ".textContent.includes('Educator workspace')")
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
        self.assertIn("Overall: 38 of 40", teacher_table)
        b.command("Page.navigate", {
            "url": f"{self.url}/?session={sid}#token={token}"})
        b.wait("document.readyState === 'complete'")
        b.wait("!document.querySelector('#end-panel').hidden",
               timeout=30)
        # The released package stays in the educator workspace; the
        # student sees the observed score and a pointer note only.
        student_text = b.js(
            "document.querySelector('" + summary + "').textContent")
        self.assertIn("38 of 40", student_text)
        self.assertIn("Educator workspace", student_text)
        for needle in ("What went well", "Answers to review",
                       "What to do next", "Assessment summary"):
            self.assertNotIn(needle, student_text)
        self.assertFalse(b.js(
            "Boolean(document.querySelector('" + summary +
            " .assessment-results'))"))
        self.assertEqual(b.js(
            "document.querySelectorAll('" + summary +
            " .review-example').length"), 0)
        self.assertFalse(b.js(
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
        self.assertFalse(state["correctVisible"])
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
        b.wait("!document.querySelector('#end-panel').hidden",
               timeout=30)
        # Released worked-review content never reaches the assessment
        # page; only the completion score renders.
        student = b.js(
            "(() => ({"
            "released: document.querySelectorAll('.review-example')"
            ".length,"
            "practice: document.querySelectorAll('#end-summary "
            ".practice-activity').length,"
            "complete: document.querySelector('#end-summary')"
            ".textContent.includes('Educator workspace'),"
            "score: document.querySelector('#end-summary').textContent"
            ".includes('questions correctly'),"
            "teacherEdit: document.querySelector('#end-summary')"
            ".textContent.includes('Teacher reworded prompt.'),"
            "literal: document.body.textContent.includes("
            "'<img src=x onerror=alert(1)>'),"
            "injected: Boolean(document.querySelector("
            "'.review-example img, .review-example script'))}))()")
        self.assertEqual(student["released"], 0)
        self.assertEqual(student["practice"], 0)
        self.assertTrue(student["complete"])
        self.assertTrue(student["score"])
        self.assertFalse(student["teacherEdit"])
        self.assertFalse(student["literal"])
        self.assertFalse(student["injected"])
        b.js("document.querySelector('#end-panel').scrollIntoView()")
        b.screenshot("question-first-student.png")
        b.viewport(390, 844)
        b.wait("document.readyState === 'complete'")
        self.assertFalse(b.js(
            "document.documentElement.scrollWidth > "
            "document.documentElement.clientWidth"))
        b.js("document.querySelector('#end-panel').scrollIntoView()")
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
        self.assertFalse(result["explVisible"])
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
                "solution_version": solved["solution_version"],
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
