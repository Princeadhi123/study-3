import copy
import json
import tempfile
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

import phase3_paths  # noqa: F401
import worked_example_selection
import worked_review_math
from assessment_worked_review import (
    ASSESSMENT_WORKED_REVIEW_VERSION, CONCISE_PRACTICE_NOTICE,
    CONCISE_READY_NOTICE, CONCISE_TEMPLATE_VERSION,
    QUESTION_FIRST_NEXT_STEPS,
    QUESTION_FIRST_READY_NOTICE, QUESTION_FIRST_TEMPLATE_VERSION,
    QUESTION_FIRST_VERSION, READY_NOTICE, UNAVAILABLE_NOTICE,
    build_assessment_review_examples, concise_student_preview,
    question_first_preview)
from aitta_generator import AittaGenerator
from demo_service import ConflictError, DemoService
from evidence_feedback import (
    build_evidence, build_research_evidence, canonical_digest,
    run_multi_feedback)
from evidence_feedback_policy import (
    build_candidates, build_review_bundles)
from feedback_practice import (
    MULTI_INTEGRATED_TEMPLATE_VERSION, PRACTICE_SELECTION_NOTICE)
from full_feedback import (
    CachedFullFeedbackGenerator, FULL_FEEDBACK_SECTION_KEYS,
    FullFeedbackAittaGenerator, MULTI_FULL_FEEDBACK_SCHEMA,
    build_multi_full_input, multi_baseline_sections,
    validate_full_input)
from replay_provider_scenarios import CaptureCache
from research_content_descriptions import MATHEMATICAL_TASKS
from research_runtime import load_research_bank
from session_store import bank_fingerprint
from tests.test_kt_adapter import FakeKT
from tests.test_demo_service import GatedDiagnostics, wait_for
from tests.helpers import (
    FROZEN_RESEARCH_FILES, requires_private_artifacts)
from live_diagnostics import LiveDiagnostics


API_KEY = "test-aitta-key-0123456789abcdef"
BASE_URL = "https://aitta.example.test"
MODEL = "openai/gpt-oss-120b"
VALID_SECTIONS = {
    "assessment_summary": "You have completed the assessment.",
    "observed_strengths": "Your observed answers provide a starting point.",
    "review_focus": "Review the selected assessed content with support.",
    "next_steps": "Work through the supplied example with your teacher."}

SKILLS = ("pct", "alg", "div", "fri")
SKILL_NAMES = {"pct": "Prosenttilaskenta", "alg": "Algebra",
               "div": "Jaollisuus", "fri": "Murtoluvut"}
SUBTOPICS = {"pct": ("percentage_amount", "Percentage of an amount"),
             "alg": ("combine_like_terms", "Combining like terms"),
             "div": ("divisibility", "Divisibility"),
             "fri": ("fraction_product", "Fraction products")}


def _item(skill, i):
    if skill == "pct":
        percent, whole = 10 * (i + 1), 50 + 10 * i
        answer = percent * whole // 100
        return (f"Kuinka paljon on {percent} % luvusta {whole}?",
                [str(answer - 2), str(answer), str(answer + 4)])
    if skill == "alg":
        a, b = i + 3, i + 2
        return (f"Sievennä {a}x + {b}x",
                [f"{a + b - 1}x", f"{a + b}x", f"{a + b + 1}x"])
    if skill == "div":
        return (f"{12 * (i + 2)} on jaollinen luvulla...",
                ["5", "3", "7"])
    return (f"{i + 1}/8 · 1/2",
            [f"{i + 1}/8", f"{i + 1}/16", f"{i + 1}/32"])


def make_grounded_bank(unsupported_fri=False):
    questions = []
    for half in (0, 1):
        for slot in range(5):
            for skill in SKILLS:
                index = half * 5 + slot
                text, options = _item(skill, index)
                if unsupported_fri and skill == "fri" and index == 0:
                    text = "Perustele omin sanoin laskusi."
                questions.append({
                    "question_id": f"{skill}__q{index}",
                    "skill_id": skill,
                    "item_id": f"item-{skill}-{index}",
                    "exercise_id": f"ex-{skill}-{index}",
                    "text": text,
                    "options": options,
                    "answer_index": 1,
                    "content_text": f"{text} [OPTIONS] " + " | ".join(options)})
    return {"protocol": "offline_mcq_demo_only",
            "review_status": "approved",
            "skill_names": dict(SKILL_NAMES),
            "questions": questions}


def make_grounded_taxonomy(bank):
    return {
        "schema": "phase3_assessment_taxonomy_draft_v1",
        "status": "approved_for_descriptive_research_prototype",
        "student_feedback_status": "observed_counts_only_no_subtopic_mastery_or_conformal",
        "bank_fingerprint": bank_fingerprint(bank),
        "relation": "is_part_of_not_prerequisite",
        "topics": [{"skill_id": sid, "skill_name": SKILL_NAMES[sid],
                    "subtopics": [{
                        "id": SUBTOPICS[sid][0], "name": SUBTOPICS[sid][1],
                        "question_ids": [q["question_id"] for q in
                                         bank["questions"]
                                         if q["skill_id"] == sid]}]}
                   for sid in SKILLS]}


def grounded_rows(bank, wrong_positions=(), offset=1):
    rows = []
    for index, q in enumerate(bank["questions"]):
        selected = q["answer_index"]
        if index in wrong_positions:
            selected = (q["answer_index"] + offset) % len(q["options"])
        rows.append({"question_id": q["question_id"],
                     "selected_index": selected})
    return rows


def positions(bank, skill, half):
    return [i for i, q in enumerate(bank["questions"])
            if q["skill_id"] == skill and i // 20 == half]


def context_for(bank, taxonomy, rows):
    return {"bank": bank, "taxonomy": taxonomy, "responses": rows}


def candidate_for(evidence, subtopic_id):
    return next(c for c in build_candidates(evidence, "end")
                if (c["focus"] or {}).get("subtopic_id") == subtopic_id)


class RecordingOpener:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append({
            "body": json.loads(request.data.decode("utf-8")),
            "headers": dict(request.header_items())})
        if self.error is not None:
            raise self.error
        class _Response:
            def __init__(self, data):
                self._data = json.dumps(data).encode("utf-8")

            def read(self, limit=-1):
                return self._data

            def close(self):
                pass
        return _Response(self.payload)


def wire_reply(candidate_id, sections=None, worked_reviews=None):
    reply = {"candidate_id": candidate_id,
             "sections": sections or dict(VALID_SECTIONS)}
    if worked_reviews is not None:
        reply["worked_reviews"] = worked_reviews
    return {"choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": json.dumps(reply)}}],
            "model": MODEL,
            "usage": {"prompt_tokens": 10, "completion_tokens": 20,
                      "total_tokens": 30}}


def fixture_reviews(payload):
    """Deterministic worked_reviews for every projected provider
    example in the payload (selected cards, including ones whose local
    solver was unavailable), fulfilling the grounded reply contract in
    tests that are not about provider wording."""
    from full_feedback import worked_review_examples_for_reply
    expected = worked_review_examples_for_reply(payload) or []
    return [{"question_id": e["question_id"],
             "steps": ["Fixture authored step."],
             "reasoning_question": "Why does the fixture step work?",
             "socratic_prompt":
                 f"What is this question {e['question_id']} asking you "
                 "to find?",
             "progressive_hints": [
                 f"Hint one for {e['question_id']}: identify the "
                 "given quantities.",
                 f"Hint two for {e['question_id']}: which operation "
                 "connects them?"]}
            for e in expected]


class GroundedBuilderTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_grounded_bank()
        self.taxonomy = make_grounded_taxonomy(self.bank)

    def _evidence(self, rows):
        return build_evidence(self.bank, self.taxonomy, rows)

    def test_earliest_incorrect_percentage_and_algebra(self):
        wrong = sorted({positions(self.bank, "pct", 0)[1],
                        positions(self.bank, "pct", 1)[0],
                        positions(self.bank, "alg", 0)[2],
                        positions(self.bank, "alg", 0)[3]})
        rows = grounded_rows(self.bank, wrong)
        evidence = self._evidence(rows)
        ctx = context_for(self.bank, self.taxonomy, rows)
        candidates = [candidate_for(evidence, "percentage_amount"),
                      candidate_for(evidence, "combine_like_terms")]
        cards = build_assessment_review_examples(
            evidence, candidates, ctx)
        self.assertEqual(len(cards), 2)
        expected_qids = [self.bank["questions"][wrong[0]]["question_id"],
                         self.bank["questions"][wrong[1]]["question_id"]]
        for card, qid, cand in zip(cards, expected_qids, candidates):
            self.assertEqual(card["source"],
                             "incorrect_assessment_response")
            self.assertEqual(card["version"],
                             ASSESSMENT_WORKED_REVIEW_VERSION)
            self.assertEqual(
                card["selection_policy"],
                "earliest_incorrect_in_selected_subtopic_v1")
            self.assertEqual(card["title"],
                             "Worked review of your assessment answer")
            self.assertEqual(card["candidate_id"],
                             cand["candidate_id"])
            self.assertEqual(card["question_id"], qid)
            question = next(q for q in self.bank["questions"]
                            if q["question_id"] == qid)
            self.assertEqual(card["skill_id"], question["skill_id"])
            self.assertEqual(card["subtopic_id"],
                             cand["focus"]["subtopic_id"])
            self.assertEqual(card["for_subtopic_name"],
                             cand["focus"]["subtopic_name"])
            self.assertEqual(card["prompt"], question["text"])
            self.assertEqual(card["options"], question["options"])
            wrong_index = next(
                r["selected_index"] for r in ctx["responses"]
                if r["question_id"] == qid)
            self.assertEqual(card["selected_answer"],
                             question["options"][wrong_index])
            self.assertEqual(card["correct_answer"],
                             question["options"][question["answer_index"]])
            self.assertEqual(card["status"], "ready")
            self.assertTrue(card["steps"])
            self.assertEqual(card["explanation"], "\n".join(card["steps"]))
            self.assertTrue(card["reasoning_question"])
            self.assertEqual(card["solution_version"],
                             worked_review_math.SOLUTION_VERSION)
            self.assertTrue(card["notice"])
            for forbidden in ("answer_index", "selected_index",
                              "item_id", "exercise_id", "content_text"):
                self.assertNotIn(forbidden, card)
        prompts = {c["prompt"] for c in cards}
        self.assertNotIn("Find 30% of 90.", prompts)
        self.assertNotIn("Simplify 7a − 2a.", prompts)

    def test_wrong_option_variants_share_source_and_steps(self):
        pos = positions(self.bank, "pct", 0)[0]
        cards = []
        for offset in (1, 2):
            rows = grounded_rows(self.bank, [pos], offset=offset)
            evidence = self._evidence(rows)
            ctx = context_for(self.bank, self.taxonomy, rows)
            card = build_assessment_review_examples(
                evidence, [candidate_for(evidence, "percentage_amount")],
                ctx)[0]
            cards.append(card)
        self.assertEqual(cards[0]["question_id"], cards[1]["question_id"])
        self.assertEqual(cards[0]["steps"], cards[1]["steps"])
        self.assertNotEqual(cards[0]["selected_answer"],
                            cards[1]["selected_answer"])
        self.assertNotEqual(
            canonical_digest(cards[0]), canonical_digest(cards[1]))

    def test_earliest_correct_selects_next_incorrect(self):
        pos = positions(self.bank, "pct", 0)
        rows = grounded_rows(self.bank, [pos[2]])
        evidence = self._evidence(rows)
        card = build_assessment_review_examples(
            evidence, [candidate_for(evidence, "percentage_amount")],
            context_for(self.bank, self.taxonomy, rows))[0]
        self.assertEqual(card["question_id"],
                         self.bank["questions"][pos[2]]["question_id"])

    def test_never_earlier_wrong_in_different_subtopic(self):
        alg_first = positions(self.bank, "alg", 0)[0]
        pct_later = positions(self.bank, "pct", 0)[3]
        rows = grounded_rows(self.bank, [alg_first, pct_later])
        evidence = self._evidence(rows)
        card = build_assessment_review_examples(
            evidence, [candidate_for(evidence, "percentage_amount")],
            context_for(self.bank, self.taxonomy, rows))[0]
        self.assertEqual(card["question_id"],
                         self.bank["questions"][pct_later]["question_id"])

    def test_one_focus_exactly_one_card_all_correct_zero(self):
        rows = grounded_rows(self.bank, positions(self.bank, "div", 0))
        evidence = self._evidence(rows)
        cards = build_assessment_review_examples(
            evidence, [candidate_for(evidence, "divisibility")],
            context_for(self.bank, self.taxonomy, rows))
        self.assertEqual(len(cards), 1)
        rows_ok = grounded_rows(self.bank)
        evidence_ok = self._evidence(rows_ok)
        only = build_candidates(evidence_ok, "end")
        self.assertEqual(
            build_assessment_review_examples(
                evidence_ok, only,
                context_for(self.bank, self.taxonomy, rows_ok)), [])

    def test_none_context_returns_no_examples(self):
        rows = grounded_rows(self.bank, [positions(self.bank, "pct", 0)[0]])
        evidence = self._evidence(rows)
        self.assertEqual(build_assessment_review_examples(
            evidence, [candidate_for(evidence, "percentage_amount")]), [])
        self.assertEqual(build_assessment_review_examples(
            evidence, [candidate_for(evidence, "percentage_amount")],
            None), [])

    def test_unsupported_question_keeps_source_unavailable(self):
        bank = make_grounded_bank(unsupported_fri=True)
        taxonomy = make_grounded_taxonomy(bank)
        pos = positions(bank, "fri", 0)[0]
        rows = grounded_rows(bank, [pos])
        evidence = build_evidence(bank, taxonomy, rows)
        cand = next(c for c in build_candidates(evidence, "end")
                    if (c["focus"] or {}).get("subtopic_id")
                    == "fraction_product")
        card = build_assessment_review_examples(
            evidence, [cand],
            {"bank": bank, "taxonomy": taxonomy, "responses": rows})[0]
        question = bank["questions"][pos]
        self.assertEqual(card["question_id"], question["question_id"])
        self.assertEqual(card["prompt"], question["text"])
        self.assertEqual(card["status"], "unavailable")
        self.assertEqual(card["steps"], [])
        self.assertEqual(card["explanation"], "")
        self.assertIsNone(card["solution_version"])
        self.assertEqual(
            card["reasoning_question"],
            "Explain your approach to this question to your teacher, "
            "then work through it together.")
        self.assertIn("no replacement example has been invented",
                      card["notice"])
        self.assertEqual(card["selected_answer"],
                         question["options"][2])
        self.assertEqual(card["correct_answer"],
                         question["options"][question["answer_index"]])

    def test_candidate_must_be_permitted(self):
        rows = grounded_rows(self.bank, [positions(self.bank, "pct", 0)[0]])
        evidence = self._evidence(rows)
        ctx = context_for(self.bank, self.taxonomy, rows)
        forged = copy.deepcopy(
            candidate_for(evidence, "percentage_amount"))
        forged["candidate_id"] = "review_forged"
        with self.assertRaises(ValueError):
            build_assessment_review_examples(evidence, [forged], ctx)

    def test_tampered_inputs_rejected(self):
        pos = positions(self.bank, "pct", 0)[0]
        rows = grounded_rows(self.bank, [pos])
        evidence = self._evidence(rows)
        ctx = context_for(self.bank, self.taxonomy, rows)
        cand = [candidate_for(evidence, "percentage_amount")]
        mutations = []
        bad_rows = copy.deepcopy(rows)
        bad_rows[0]["selected_index"] = True
        mutations.append(dict(ctx, responses=bad_rows))
        bad_rows = copy.deepcopy(rows)
        bad_rows[0]["selected_index"] = 99
        mutations.append(dict(ctx, responses=bad_rows))
        bad_rows = list(reversed(rows))
        mutations.append(dict(ctx, responses=bad_rows))
        bad_rows = copy.deepcopy(rows)
        bad_rows[pos]["selected_index"] = self.bank["questions"][pos][
            "answer_index"]
        mutations.append(dict(ctx, responses=bad_rows))
        bad_bank = copy.deepcopy(self.bank)
        bad_bank["questions"][pos]["answer_index"] = 0
        mutations.append(dict(ctx, bank=bad_bank))
        bad_bank = copy.deepcopy(self.bank)
        bad_bank["questions"][pos]["text"] = "Muutettu kysymys?"
        mutations.append(dict(ctx, bank=bad_bank))
        bad_bank = copy.deepcopy(self.bank)
        bad_bank["questions"][pos]["options"] = ["x", "y", "z"]
        mutations.append(dict(ctx, bank=bad_bank))
        bad_tax = copy.deepcopy(self.taxonomy)
        bad_tax["topics"][0]["subtopics"][0]["question_ids"] = [
            q["question_id"] for q in self.bank["questions"]
            if q["skill_id"] == "alg"]
        mutations.append(dict(ctx, taxonomy=bad_tax))
        mutations.append({"bank": self.bank, "taxonomy": self.taxonomy})
        mutations.append("not-a-dict")
        for bad in mutations:
            with self.assertRaises(ValueError):
                build_assessment_review_examples(evidence, cand, bad)
        bad_evidence = copy.deepcopy(evidence)
        bad_evidence["subtopics"][0]["incorrect"] += 1
        with self.assertRaises(ValueError):
            build_assessment_review_examples(bad_evidence, cand, ctx)


class WorkedSolutionMathTests(unittest.TestCase):
    def _solved(self, text, options, answer_index, subtopic):
        question = {"question_id": "q", "skill_id": "s", "text": text,
                    "options": options, "answer_index": answer_index,
                    "exercise_id": "e"}
        return worked_review_math.solution(question, subtopic)

    def test_percentage_amount(self):
        solved = self._solved("Kuinka paljon on 50 % luvusta 500?",
                              ["200", "250", "300"], 1,
                              "percentage_amount")
        self.assertIn("250", solved["steps"][-1])

    def test_negative_like_terms(self):
        solved = self._solved("Sievennä -3x - 2x", ["-5x", "-x", "5x"], 0,
                              "combine_like_terms")
        self.assertIn("-5x", solved["steps"][-1])

    def test_implied_coefficient_like_terms(self):
        solved = self._solved("Sievennä x + 6x", ["6x", "7x", "x"], 1,
                              "combine_like_terms")
        self.assertIn("7x", solved["steps"][-1])

    def test_retain_unlike_terms(self):
        solved = self._solved("Sievennä -2x - y", ["-3xy", "-2x - y", "-2xy"],
                              1, "retain_unlike_terms")
        self.assertIn("-2x - y", solved["steps"][-1])
        with self.assertRaises(ValueError):
            self._solved("Sievennä x + x + y", ["2x + y"], 0,
                         "retain_unlike_terms")

    def test_fraction_products(self):
        solved = self._solved("4/6 · 5/4", ["20/24", "9/10", "5/6"], 0,
                              "fraction_product")
        self.assertTrue(any("20/24" in s for s in solved["steps"]))
        self.assertTrue(any("5/6" in s for s in solved["steps"]))
        solved = self._solved("1/7 · 11", ["11/7", "12/7", "11/77"], 0,
                              "fraction_times_integer")
        self.assertIn("11/7", solved["steps"][-1])

    def test_prime_and_composite(self):
        solved = self._solved("Alkuluku?", ["5", "9", "12"], 0,
                              "prime_recognition")
        self.assertTrue(solved["steps"])
        with self.assertRaises(ValueError):
            self._solved("Alkuluku?", ["5", "9", "12"], 1,
                         "prime_recognition")

    def test_divisibility(self):
        solved = self._solved("25 on jaollinen luvulla...",
                              ["5", "3", "7"], 0, "divisibility")
        self.assertTrue(any("25" in s for s in solved["steps"]))
        with self.assertRaises(ValueError):
            self._solved("25 on jaollinen luvulla...",
                         ["3", "5", "7"], 0, "divisibility")

    def test_decimal_price_expressions(self):
        solved = self._solved("10 € - 2,5 €", ["7.5", "8.5", "12.5"], 0,
                              "price_subtraction")
        self.assertTrue(any("7.5" in s for s in solved["steps"]))
        solved = self._solved(
            "250 gramman suklaapatukka maksaa 2,20 euroa. Mikä on "
            "suklaapatukan kilohinta?", ["8.8", "5.5", "0.88"], 0,
            "price_unit_price")
        self.assertTrue(any("8.8" in s for s in solved["steps"]))
        solved = self._solved(
            "2,5 desilitran limutölkki maksaa 2,65 euroa. Mikä on limun "
            "litrahinta?", ["10.6", "1.06", "6.62"], 0,
            "price_unit_price")
        self.assertTrue(any("10.6" in s for s in solved["steps"]))

    def test_decimal_display(self):
        self.assertEqual(worked_review_math.decimal_display(
            Fraction(1, 4)), "0.25")
        self.assertEqual(worked_review_math.decimal_display(
            Fraction(-3, 2)), "−1.5")
        self.assertEqual(worked_review_math.decimal_display(
            Fraction(1, 3)), "1/3")

    def test_precedence_emits_multiplication_before_additions(self):
        value, steps = worked_review_math.arithmetic("7+22+3*5")
        self.assertEqual(value, 44)
        self.assertTrue(steps[0].startswith("3 × (5)"))
        self.assertEqual(steps[-1], "29 + (15) = 44.")

    def test_unsupported_constructs_rejected(self):
        for expression in ("__import__('os')", "2**3", "x+1",
                           "eval('1+1')"):
            with self.assertRaises(ValueError):
                worked_review_math.arithmetic(expression)
        with self.assertRaises(ValueError):
            worked_review_math.solution(
                {"question_id": "q", "skill_id": "s",
                 "text": "Kuinka paljon on 50 % luvusta 500?",
                 "options": ["200", "999", "250"], "answer_index": 1,
                 "exercise_id": "e"}, "percentage_amount")

    def test_tampered_correct_option_disagrees_solution(self):
        with self.assertRaises(ValueError):
            self._solved("Kuinka paljon on 50 % luvusta 500?",
                         ["200", "251", "300"], 1, "percentage_amount")

    def test_algebra_term_parser_accepts_valid_forms(self):
        for expression in ("x+y", "-2x-y", "3x-1", "x+6x", "-3x-2x",
                           "-3+2x", "0x+3x", " x + y ", "x − y"):
            self.assertTrue(worked_review_math._algebra_terms(expression),
                            expression)
        self.assertEqual(worked_review_math.algebra("x + x + y"),
                         {"x": 2, "y": 1})
        self.assertEqual(worked_review_math.algebra("0x+3x"), {"x": 3})

    def test_algebra_term_parser_rejects_malformed(self):
        for expression in ("xx", "2x3x", "x++y", "x+", "+", "2*x",
                           "x^2", "x/2", "x+y!", "9" * 10000 + "!",
                           "x" * 10000):
            with self.assertRaises(ValueError, msg=expression[:20]):
                worked_review_math._algebra_terms(expression)

    def test_no_findall_algebra_parsing_remains(self):
        import inspect
        source = inspect.getsource(worked_review_math)
        self.assertNotIn("re.findall", source)


class GroundedPipelineTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_grounded_bank()
        self.taxonomy = make_grounded_taxonomy(self.bank)
        self.wrong = sorted({positions(self.bank, "pct", 0)[0],
                             positions(self.bank, "alg", 0)[0]})
        self.rows = grounded_rows(self.bank, self.wrong)
        self.evidence = build_evidence(
            self.bank, self.taxonomy, self.rows)
        self.context = context_for(self.bank, self.taxonomy, self.rows)
        self.bundle = next(
            b for b in build_review_bundles(self.evidence)
            if {c["focus"]["subtopic_id"]
                for c in b["review_candidates"] if c["focus"]}
            == {"percentage_amount", "combine_like_terms"})
        self.slots = [{"schema": "phase3_feedback_practice_context_v1",
                       "status": "abstained", "selected_question_id": None,
                       "pool_sha256": None}
                      for _ in self.bundle["review_candidates"]
                      if _["focus"] is not None]

    def _generator(self, opener):
        inner = AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                               opener=opener)
        return FullFeedbackAittaGenerator(inner)

    def _payload(self):
        return build_multi_full_input(
            self.evidence, "student", self.bundle, self.slots,
            assessment_context=self.context)

    def test_payload_carries_context_and_grounded_examples(self):
        payload = self._payload()
        self.assertEqual(payload["assessment_context"], self.context)
        self.assertIsNot(payload["assessment_context"], self.context)
        self.assertEqual(len(payload["review_examples"]), 2)
        self.assertTrue(all(e["source"] == "incorrect_assessment_response"
                            for e in payload["review_examples"]))
        normalized = validate_full_input(copy.deepcopy(payload))
        self.assertEqual(normalized, payload)

    def test_validate_rejects_tampered_examples_and_context(self):
        for mutate in (
                lambda p: p["review_examples"][0].update(
                    selected_answer="forged"),
                lambda p: p["review_examples"][0]["steps"].append("fake"),
                lambda p: p["review_examples"].pop(),
                lambda p: p.update(assessment_context=None),
                lambda p: p["assessment_context"]["responses"][0].update(
                    selected_index=0),
                lambda p: p["assessment_context"]["bank"]["questions"][0]
                    .update(answer_index=0),
                lambda p: p["assessment_context"]["taxonomy"]["topics"][0]
                    .update(skill_name="Forged")):
            payload = self._payload()
            mutate(payload)
            with self.assertRaises(ValueError):
                validate_full_input(payload)

    def test_none_context_payload_has_empty_examples(self):
        payload = build_multi_full_input(
            self.evidence, "student", self.bundle, self.slots)
        self.assertIsNone(payload["assessment_context"])
        self.assertEqual(payload["review_examples"], [])
        normalized = validate_full_input(copy.deepcopy(payload))
        self.assertEqual(normalized, payload)

    def test_wire_contains_only_selected_cards(self):
        payload = self._payload()
        opener = RecordingOpener(
            wire_reply(candidate_id=self.bundle["candidate_id"],
                       worked_reviews=fixture_reviews(payload)))
        self._generator(opener).generate(copy.deepcopy(payload))
        body = opener.calls[0]["body"]
        context = json.loads(body["messages"][1]["content"])
        self.assertEqual(context["schema"], MULTI_FULL_FEEDBACK_SCHEMA)
        self.assertNotIn("assessment_context", context)
        self.assertNotIn("responses", context)
        self.assertNotIn("taxonomy", context)
        self.assertNotIn("bank", context)
        cards = context["review_examples"]
        self.assertEqual(len(cards), 2)
        selected_qids = {c["question_id"] for c in cards}
        raw = json.dumps(context)
        for question in self.bank["questions"]:
            if question["question_id"] in selected_qids:
                continue
            self.assertNotIn(question["text"], raw,
                             question["question_id"])
            self.assertNotIn(question["question_id"], raw)
        for forbidden in ('"answer_index"', '"selected_index"',
                          '"item_id"', '"exercise_id"', '"content_text"',
                          '"bank_sha256"', "p_correct", "pool_q"):
            self.assertNotIn(forbidden, raw)

    def test_run_multi_feedback_grounded_review_and_fallback(self):
        review = run_multi_feedback(
            self.evidence, practice_contexts=self.slots,
            assessment_context=self.context)
        self.assertEqual(review["worked_review_version"],
                         ASSESSMENT_WORKED_REVIEW_VERSION)
        cards = review["review_examples"]
        self.assertEqual(len(cards), 2)
        self.assertTrue(all(c["status"] == "ready" for c in cards))
        self.assertTrue(all(c["source"] == "incorrect_assessment_response"
                            for c in cards))
        next_steps = next(s for s in review["message"]["sections"]
                          if s["kind"] == "next_steps")
        self.assertIn("assessment answer", next_steps["text"])
        opener = RecordingOpener(error=RuntimeError("down"))
        review2 = run_multi_feedback(
            self.evidence, generator=self._generator(opener),
            practice_contexts=self.slots,
            assessment_context=self.context)
        self.assertEqual(review2["trace"]["fallback_reason"],
                         "generator_error")
        self.assertEqual(review2["review_examples"], cards)
        self.assertEqual(review2["worked_review_version"],
                         ASSESSMENT_WORKED_REVIEW_VERSION)

    def test_generator_mutation_cannot_change_stored_examples(self):
        review = run_multi_feedback(
            self.evidence, practice_contexts=self.slots,
            assessment_context=self.context)

        class Mutating:
            supports_full_feedback = True
            execution = None

            def generate(self, payload):
                payload["review_examples"][0]["steps"] = ["injected"]
                return {"candidate_id": payload["selected_candidate"]
                        ["candidate_id"], "sections": dict(VALID_SECTIONS)}

        review2 = run_multi_feedback(
            self.evidence, generator=Mutating(),
            practice_contexts=self.slots,
            assessment_context=self.context)
        self.assertEqual(review2["review_examples"],
                         review["review_examples"])

    def test_run_multi_feedback_without_context_keeps_examples_empty(self):
        review = run_multi_feedback(
            self.evidence, practice_contexts=self.slots)
        self.assertNotIn("worked_review_version", review)
        self.assertNotIn("review_examples", review)

    def test_baseline_grounded_and_legacy_wording(self):
        cards = build_assessment_review_examples(
            self.evidence, self.bundle["review_candidates"],
            self.context)
        grounded = multi_baseline_sections(
            self.evidence, self.bundle["review_candidates"],
            review_examples=cards)
        legacy = multi_baseline_sections(
            self.evidence, self.bundle["review_candidates"])
        g_next = next(s for s in grounded if s["kind"] == "next_steps")
        l_next = next(s for s in legacy if s["kind"] == "next_steps")
        self.assertIn("assessment answer", g_next["text"])
        self.assertIn("checked worked steps", g_next["text"])
        self.assertNotEqual(g_next["text"], l_next["text"])

    def test_aggregate_only_baseline_uses_empty_grounded_wording(self):
        payload = build_multi_full_input(
            self.evidence, "student", self.bundle, self.slots)
        self.assertEqual(payload["review_examples"], [])
        baseline = {s["kind"]: s["text"]
                    for s in payload["baseline_sections"]}
        self.assertNotIn("below", baseline["next_steps"])
        review = run_multi_feedback(
            self.evidence, practice_contexts=self.slots)
        steps = {s["kind"]: s["text"]
                 for s in review["message"]["sections"]}
        self.assertIn("below", steps["next_steps"])
        empty = multi_baseline_sections(
            self.evidence, self.bundle["review_candidates"],
            review_examples=[])
        e_steps = {s["kind"]: s["text"] for s in empty}
        self.assertNotIn("below", e_steps["next_steps"])

    def test_observed_strengths_variants(self):
        zero_rows = grounded_rows(
            self.bank, range(len(self.bank["questions"])))
        zero_evidence = build_evidence(
            self.bank, self.taxonomy, zero_rows)
        zero_candidates = build_candidates(zero_evidence, "end")[:1]
        strengths = next(
            s["text"] for s in multi_baseline_sections(
                self.evidence, self.bundle["review_candidates"],
                review_examples=[])
            if s["kind"] == "observed_strengths")
        self.assertIn("answered questions correctly", strengths)

        def observed(examples):
            return next(s["text"] for s in multi_baseline_sections(
                zero_evidence, zero_candidates,
                review_examples=examples)
                if s["kind"] == "observed_strengths")

        card = {"candidate_id": zero_candidates[0]["candidate_id"],
                "status": "unavailable"}
        self.assertEqual(observed(None), observed([card]))
        self.assertIn("review examples", observed(None))
        self.assertNotEqual(observed([]), observed(None))
        self.assertIn("assessment answers with your teacher",
                      observed([]))
        for examples in (None, [], [card]):
            with_strengths = next(
                s["text"] for s in multi_baseline_sections(
                    self.evidence, self.bundle["review_candidates"],
                    review_examples=examples)
                if s["kind"] == "observed_strengths")
            self.assertEqual(with_strengths, strengths)

    def test_ab_wrong_options_change_wire_and_reuse_cache(self):
        payloads = []
        for offset in (1, 2):
            rows = copy.deepcopy(self.rows)
            question = self.bank["questions"][self.wrong[0]]
            rows[self.wrong[0]] = {
                "question_id": question["question_id"],
                "selected_index": ((question["answer_index"] + offset)
                                   % len(question["options"]))}
            payloads.append(build_multi_full_input(
                self.evidence, "student", self.bundle, self.slots,
                assessment_context=context_for(
                    self.bank, self.taxonomy, rows)))
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), 4)
            opener = RecordingOpener(
                wire_reply(candidate_id=self.bundle["candidate_id"],
                           worked_reviews=fixture_reviews(payloads[0])))
            cached = CachedFullFeedbackGenerator(
                self._generator(opener), cache, MODEL,
                "endpointhash" * 8)
            cached.generate(copy.deepcopy(payloads[0]))
            self.assertFalse(cached.execution["reused"])
            cached.generate(copy.deepcopy(payloads[1]))
            self.assertFalse(cached.execution["reused"])
            self.assertEqual(len(opener.calls), 2)
            bodies = [json.loads(c["body"]["messages"][1]["content"])
                      for c in opener.calls]
            self.assertNotEqual(
                bodies[0]["review_examples"][0]["selected_answer"],
                bodies[1]["review_examples"][0]["selected_answer"])
            self.assertEqual(
                bodies[0]["review_examples"][0]["question_id"],
                bodies[1]["review_examples"][0]["question_id"])
            cached.generate(copy.deepcopy(payloads[0]))
            self.assertTrue(cached.execution["reused"])
            self.assertEqual(len(opener.calls), 2)
            self.assertEqual(
                len(list(Path(tmp).glob("aitta_full_*.json"))), 2)

    def test_tampered_payload_rejected_before_http(self):
        opener = RecordingOpener(
            wire_reply(candidate_id=self.bundle["candidate_id"]))
        generator = self._generator(opener)
        payload = self._payload()
        payload["assessment_context"]["responses"][0][
            "selected_index"] = 0
        with self.assertRaises(ValueError):
            generator.generate(payload)
        self.assertEqual(opener.calls, [])

    def test_generator_systemexit_preserves_grounded_cards(self):
        review = run_multi_feedback(
            self.evidence, practice_contexts=self.slots,
            assessment_context=self.context)

        class Aborting:
            supports_full_feedback = True
            execution = None

            def generate(self, payload):
                raise SystemExit("abort")

        aborted = run_multi_feedback(
            self.evidence, generator=Aborting(),
            practice_contexts=self.slots,
            assessment_context=self.context)
        self.assertEqual(aborted["trace"]["fallback_reason"],
                         "generator_error")
        self.assertEqual(aborted["review_examples"],
                         review["review_examples"])
        self.assertTrue(all(
            c["source"] == "incorrect_assessment_response"
            for c in aborted["review_examples"]))


@requires_private_artifacts(*FROZEN_RESEARCH_FILES)
class WarmBankGroundedTests(unittest.TestCase):
    def setUp(self):
        self.bank, self.taxonomy, _ = load_research_bank("warm")
        self.qpos = {q["question_id"]: i
                     for i, q in enumerate(self.bank["questions"])}
        self.leaves = {}
        for topic in self.taxonomy["topics"]:
            for leaf in topic["subtopics"]:
                self.leaves[leaf["id"]] = leaf

    def _rows(self, wrong_qids):
        wrong = set(wrong_qids)
        return [{"question_id": q["question_id"],
                 "selected_index": (
                     (q["answer_index"] + 1) % len(q["options"])
                     if q["question_id"] in wrong
                     else q["answer_index"])}
                for q in self.bank["questions"]]

    def test_alternating_warm_percentage_and_algebra(self):
        pct = self.leaves["percentage_amount"]
        alg = self.leaves["combine_like_terms"]
        wrong = [qid for leaf in (pct, alg)
                 for i, qid in enumerate(leaf["question_ids"])
                 if i % 2 == 1]
        rows = self._rows(wrong)
        evidence = build_research_evidence(
            self.bank, self.taxonomy, rows)
        ctx = {"bank": self.bank, "taxonomy": self.taxonomy,
               "responses": rows}
        candidates = [candidate_for(evidence, "percentage_amount"),
                      candidate_for(evidence, "combine_like_terms")]
        cards = build_assessment_review_examples(
            evidence, candidates, ctx)
        self.assertEqual(len(cards), 2)
        for card, leaf, cand in zip(cards, (pct, alg), candidates):
            self.assertEqual(card["question_id"],
                             leaf["question_ids"][1])
            question = self.bank["questions"][
                self.qpos[card["question_id"]]]
            self.assertEqual(card["status"], "ready")
            self.assertTrue(card["steps"])
            self.assertEqual(card["prompt"], question["text"])
            self.assertEqual(card["options"], question["options"])
            self.assertEqual(
                card["selected_answer"],
                question["options"][
                    (question["answer_index"] + 1)
                    % len(question["options"])])
            self.assertEqual(
                card["correct_answer"],
                question["options"][question["answer_index"]])
            self.assertEqual(card["candidate_id"],
                             cand["candidate_id"])
            self.assertEqual(card["skill_id"],
                             cand["focus"]["skill_id"])
            self.assertEqual(card["subtopic_id"], leaf["id"])

    def test_same_skill_leaf_never_borrows_earlier_wrong(self):
        combine = self.leaves["combine_like_terms"]
        retain = self.leaves["retain_unlike_terms"]
        first_leaf, second_leaf = sorted(
            (combine, retain),
            key=lambda s: self.qpos[s["question_ids"][0]])
        wrong = [first_leaf["question_ids"][0],
                 second_leaf["question_ids"][1]]
        rows = self._rows(wrong)
        evidence = build_research_evidence(
            self.bank, self.taxonomy, rows)
        ctx = {"bank": self.bank, "taxonomy": self.taxonomy,
               "responses": rows}
        cand = candidate_for(evidence, second_leaf["id"])
        card = build_assessment_review_examples(
            evidence, [cand], ctx)[0]
        self.assertEqual(card["question_id"],
                         second_leaf["question_ids"][1])
        self.assertNotEqual(card["question_id"],
                            first_leaf["question_ids"][0])
        self.assertEqual(card["skill_id"], cand["focus"]["skill_id"])
        self.assertEqual(card["subtopic_id"], second_leaf["id"])
        self.assertIn(card["status"], ("ready", "unavailable"))


class GroundedServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = make_grounded_bank()
        self.taxonomy = make_grounded_taxonomy(self.bank)

    def _service(self, **kwargs):
        kwargs.setdefault(
            "diagnostics",
            LiveDiagnostics(model_loader=lambda: FakeKT(self.bank)))
        service = DemoService(root=Path(self.tmp.name), bank=self.bank,
                              taxonomy=self.taxonomy, **kwargs)
        self.addCleanup(service.close, wait=True)
        return service

    def _complete(self, service, wrong, variant=None):
        created = service.create_session(
            feedback_variant=variant or "question_first")
        sid, token = created["session_id"], created["student_token"]
        snapshot = None
        for row in grounded_rows(self.bank, wrong):
            snapshot = service.submit_response(sid, token, row)
        return sid, token, snapshot

    def _wait(self, service, sid, timeout=15.0):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if service._load_meta(sid)["provider_job"]["status"] in (
                    "ready", "fallback"):
                return
            time.sleep(0.05)
        self.fail("provider job did not finish")

    def test_new_multi_session_stores_grounded_cards(self):
        service = self._service()
        sid, token, _ = self._complete(
            service, [positions(self.bank, "pct", 0)[0]])
        self._wait(service, sid)
        meta = service._load_meta(sid)
        self.assertEqual(meta["worked_review_version"],
                         ASSESSMENT_WORKED_REVIEW_VERSION)
        review = meta["checkpoints"]["end"]["student_review"]
        self.assertEqual(review["worked_review_version"],
                         ASSESSMENT_WORKED_REVIEW_VERSION)
        self.assertTrue(review["review_examples"])
        detail = service.teacher_session(sid)
        preview = detail["feedback_delivery"]["preview"]
        self.assertEqual(
            preview["template_version"],
            "phase3_student_feedback_question_first_full_v2")
        self.assertIn("review_examples", preview)
        cards = preview["review_examples"]
        self.assertTrue(all(c["source"] == "incorrect_assessment_response"
                            for c in cards))
        self.assertNotIn("review_example", preview)
        for card in cards:
            self.assertIn("selected_answer", card)
            self.assertIn("correct_answer", card)
        released = service.release_feedback(
            sid, {"message_sha256": detail["feedback_delivery"]
                  ["preview_sha256"], "reviewer_label": "t"})
        self.assertTrue(released["released"])
        released_cards = service.teacher_session(sid)[
            "feedback_delivery"]["released_feedback"]["review_examples"]
        self.assertEqual(released_cards, cards)

    def test_stale_hash_rejected_and_answer_change_alters_hash(self):
        service = self._service()
        sid, token, _ = self._complete(
            service, [positions(self.bank, "pct", 0)[0]])
        self._wait(service, sid)
        detail = service.teacher_session(sid)
        preview = detail["feedback_delivery"]["preview"]
        self.assertTrue(preview["review_examples"])
        tampered = copy.deepcopy(preview)
        tampered["review_examples"][0]["selected_answer"] = "forged"
        self.assertNotEqual(canonical_digest(tampered),
                            canonical_digest(preview))
        with self.assertRaises(ConflictError):
            service.release_feedback(
                sid, {"message_sha256": "0" * 64})
        self.assertIsNone(
            detail["feedback_delivery"]["released_feedback"])

    def test_restart_recovers_grounded_cards(self):
        root = Path(self.tmp.name)
        service = self._service()
        sid, token, _ = self._complete(
            service, [positions(self.bank, "pct", 0)[0]])
        self._wait(service, sid)
        first = service.teacher_session(sid)[
            "feedback_delivery"]["preview"]
        service.close(wait=True)
        reopened = DemoService(
            root=root, bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=LiveDiagnostics(
                model_loader=lambda: FakeKT(self.bank)))
        self.addCleanup(reopened.close, wait=True)
        second = reopened.teacher_session(sid)[
            "feedback_delivery"]["preview"]
        self.assertEqual(
            second["template_version"],
            "phase3_student_feedback_question_first_full_v2")
        self.assertEqual(second["review_examples"],
                         first["review_examples"])

    def test_question_first_fields_edit_and_release(self):
        service = self._service()
        sid, token, _ = self._complete(
            service, [positions(self.bank, "pct", 0)[0]])
        self._wait(service, sid)
        meta = service._load_meta(sid)
        self.assertEqual(meta["review_presentation_version"],
                         "question_first_full_feedback_v2")
        detail = service.teacher_session(sid)
        preview = detail["feedback_delivery"]["preview"]
        self.assertEqual(
            preview["template_version"],
            "phase3_student_feedback_question_first_full_v2")
        self.assertEqual(preview["review_presentation_version"],
                         "question_first_full_feedback_v2")
        self.assertEqual(len(preview["review_examples"]), 1)
        card = preview["review_examples"][0]
        self.assertEqual(card["subtopic_id"], "percentage_amount")
        self.assertEqual(len(card["progressive_hints"]), 2)
        sections = {s["kind"]: s["text"]
                    for s in preview["sections"]}
        res = service.save_feedback_edit(sid, {
            "message_sha256": detail["feedback_delivery"]
            ["preview_sha256"],
            "sections": sections,
            "content": {"review_examples": [{
                "socratic_prompt": "What did you try first?",
                "option_contrast": "",
                "progressive_hints": ["Edited hint one.",
                                      "Edited hint two."]}]}})
        edited = service.teacher_session(sid)[
            "feedback_delivery"]["preview"]["review_examples"][0]
        self.assertEqual(edited["socratic_prompt"],
                         "What did you try first?")
        self.assertEqual(edited["option_contrast"], "")
        self.assertEqual(edited["progressive_hints"],
                         ["Edited hint one.", "Edited hint two."])
        self.assertTrue(edited["teacher_edited"])
        self.assertEqual(edited["original_content"]["socratic_prompt"],
                         card["socratic_prompt"])
        for bad in ({"progressive_hints": ["only one"]},
                    {"progressive_hints": ["a", ""]},
                    {"progressive_hints": ["a", "b", "c"]},
                    {"progressive_hints": "x"},
                    {"progressive_hints": [5, "b"]},
                    {"socratic_prompt": ""},
                    {"presentation_version": "x"},
                    {"scaffold_source": "x"},
                    {"option_contrast_source": "x"}):
            with self.assertRaises(ValueError):
                service.save_feedback_edit(sid, {
                    "message_sha256": res["preview_sha256"],
                    "sections": sections,
                    "content": {"review_examples": [bad]}})
        released = service.release_feedback(sid, {
            "message_sha256": res["preview_sha256"],
            "reviewer_label": "t"})
        self.assertTrue(released["released"])
        card_after = service.snapshot(sid, token)[
            "feedback"]["end"]["review_examples"][0]
        self.assertEqual(card_after["progressive_hints"],
                         ["Edited hint one.", "Edited hint two."])
        self.assertEqual(card_after["socratic_prompt"],
                         "What did you try first?")
        self.assertEqual(card_after["correct_answer"],
                         card["correct_answer"])

    def test_legacy_single_mode_unchanged(self):
        service = self._service(review_mode="legacy_single")
        sid, token, _ = self._complete(
            service, [positions(self.bank, "pct", 0)[0]])
        self._wait(service, sid)
        meta = service._load_meta(sid)
        self.assertNotIn("worked_review_version", meta)
        review = meta["checkpoints"]["end"]["student_review"]
        self.assertNotIn("worked_review_version", review)
        self.assertNotIn("review_examples", review)

    def test_public_snapshot_hidden_until_release(self):
        service = self._service()
        sid, token, _ = self._complete(
            service, [positions(self.bank, "pct", 0)[0]])
        self._wait(service, sid)
        self.assertIsNone(
            service.snapshot(sid, token)["feedback"]["end"])
        detail = service.teacher_session(sid)
        preview_cards = detail["feedback_delivery"]["preview"][
            "review_examples"]
        released = service.release_feedback(
            sid, {"message_sha256": detail["feedback_delivery"]
                  ["preview_sha256"], "reviewer_label": "t"})
        self.assertTrue(released["released"])
        after = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(after["review_examples"], preview_cards)

    def test_unmarked_historical_multi_session_releases_v4(self):
        root = Path(self.tmp.name)
        service = self._service()
        sid, token, _ = self._complete(
            service, [positions(self.bank, "pct", 0)[0]])
        self._wait(service, sid)
        meta = service._load_meta(sid)
        meta.pop("worked_review_version", None)
        review = meta["checkpoints"]["end"]["student_review"]
        review.pop("review_examples", None)
        review.pop("worked_review_version", None)
        service._save_meta(meta)
        detail = service.teacher_session(sid)
        preview = detail["feedback_delivery"]["preview"]
        self.assertEqual(preview["template_version"],
                         MULTI_INTEGRATED_TEMPLATE_VERSION)
        released = service.release_feedback(
            sid, {"message_sha256": detail["feedback_delivery"]
                  ["preview_sha256"], "reviewer_label": "t"})
        self.assertTrue(released["released"])
        expected = service.teacher_session(sid)[
            "feedback_delivery"]["released_feedback"]
        self.assertIsNotNone(expected)
        service.close(wait=True)
        reopened = DemoService(
            root=root, bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=LiveDiagnostics(
                model_loader=lambda: FakeKT(self.bank)))
        self.addCleanup(reopened.close, wait=True)
        meta2 = reopened._load_meta(sid)
        self.assertNotIn("worked_review_version", meta2)
        after = reopened.teacher_session(sid)[
            "feedback_delivery"]["released_feedback"]
        self.assertEqual(after["template_version"],
                         MULTI_INTEGRATED_TEMPLATE_VERSION)
        self.assertEqual(after, expected)

    def test_grounding_unavailable_terminal_fallback(self):
        service = self._service()
        sid, token, _ = self._complete(
            service, [positions(self.bank, "pct", 0)[0]])
        self._wait(service, sid)
        meta = service._load_meta(sid)
        evidence = meta["checkpoints"]["end"]["evidence"]
        original = service._assessment_context

        def broken(meta_, rows=None):
            raise ValueError("grounding broken")

        service._assessment_context = broken
        try:
            review = service._multi_baseline_review(
                meta, evidence, "grounding_unavailable_test")
        finally:
            service._assessment_context = original
        self.assertEqual(review["review_examples"], [])
        self.assertEqual(review["worked_review_version"],
                         ASSESSMENT_WORKED_REVIEW_VERSION)
        self.assertEqual(review["worked_review_status"], "unavailable")
        self.assertIn("no worked example has been substituted",
                      review["worked_review_notice"])
        self.assertEqual(
            review["trace"]["worked_review_context_status"],
            "unavailable")
        self.assertEqual(review["trace"]["fallback_reason"],
                         "grounding_unavailable_test")
        sections = {s["kind"]: s["text"]
                    for s in review["message"]["sections"]}
        self.assertNotIn("below", sections["next_steps"])
        blob = json.dumps(review["message"])
        self.assertNotIn("Find 30% of 90", blob)
        meta["checkpoints"]["end"]["student_review"] = review
        service._save_meta(meta)
        preview = service.teacher_session(sid)[
            "feedback_delivery"]["preview"]
        self.assertEqual(preview["worked_review_status"],
                         "unavailable")
        self.assertIn("no worked example has been substituted",
                      preview["worked_review_notice"])
        self.assertEqual(preview["review_examples"], [])
        self.assertIsNone(
            service.snapshot(sid, token)["feedback"]["end"])

    def test_worker_grounding_unavailable_never_calls_generator(self):
        calls = []

        class FakeGenerator:
            supports_full_feedback = True
            execution = None

            def generate(self, payload):
                calls.append(payload)
                return {
                    "candidate_id": payload["selected_candidate"]
                    ["candidate_id"],
                    "sections": dict(VALID_SECTIONS),
                    "worked_reviews": fixture_reviews(payload)}

        generator = FakeGenerator()
        service = self._service(provider_mode="hosted",
                                generator=generator)
        sid, token, _ = self._complete(
            service, [positions(self.bank, "pct", 0)[0]])
        self._wait(service, sid)
        self.assertEqual(len(calls), 1)
        meta = service._load_meta(sid)
        self.assertEqual(meta["provider_job"]["status"], "ready")
        meta["provider_job"]["status"] = "pending"
        service._save_meta(meta)
        original = service._assessment_context

        def broken(meta_, rows=None):
            raise ValueError("grounding broken")

        service._assessment_context = broken
        try:
            service._run_multi_provider_job(sid)
        finally:
            service._assessment_context = original
        self.assertEqual(len(calls), 1)
        meta = service._load_meta(sid)
        job = meta["provider_job"]
        self.assertEqual(job["status"], "fallback")
        self.assertEqual(
            job["executions"]["student"]["generator"],
            {"status": "skipped_grounding_unavailable"})
        review = meta["checkpoints"]["end"]["student_review"]
        self.assertEqual(review["worked_review_version"],
                         ASSESSMENT_WORKED_REVIEW_VERSION)
        self.assertEqual(review["worked_review_status"], "unavailable")
        self.assertEqual(review["review_examples"], [])
        self.assertEqual(review["trace"]["fallback_reason"],
                         "assessment_grounding_unavailable")
        self.assertEqual(review["trace"]["phrasing_source"],
                         "deterministic")
        self.assertIn("no worked example has been substituted",
                      review["worked_review_notice"])
        self.assertTrue(review["selected_candidates"])
        focus = next(s["text"] for s in review["message"]["sections"]
                     if s["kind"] == "review_focus")
        self.assertIn("Percentage of an amount", focus)
        self.assertIsNone(
            service.snapshot(sid, token)["feedback"]["end"])

    def test_interrupted_pending_provider_recovers_grounded(self):
        root = Path(self.tmp.name)
        service = self._service()
        sid, token, _ = self._complete(
            service, [positions(self.bank, "pct", 0)[0]])
        self._wait(service, sid)
        meta = service._load_meta(sid)
        meta["provider_job"]["status"] = "pending"
        meta["checkpoints"]["end"]["student_review"] = None
        service._save_meta(meta)
        service.close(wait=True)
        reopened = DemoService(
            root=root, bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=LiveDiagnostics(
                model_loader=lambda: FakeKT(self.bank)))
        self.addCleanup(reopened.close, wait=True)
        meta2 = reopened._load_meta(sid)
        self.assertEqual(meta2["provider_job"]["status"], "fallback")
        review = meta2["checkpoints"]["end"]["student_review"]
        self.assertEqual(review["worked_review_version"],
                         ASSESSMENT_WORKED_REVIEW_VERSION)
        self.assertEqual(review["trace"]["fallback_reason"],
                         "interrupted_by_restart")
        self.assertTrue(review["review_examples"])
        self.assertTrue(all(
            c["source"] == "incorrect_assessment_response"
            for c in review["review_examples"]))

    def test_queue_capacity_fallback_still_grounded(self):
        service = self._service(max_pending_jobs=0)
        sid, token, _ = self._complete(
            service, [positions(self.bank, "pct", 0)[0]])
        meta = service._load_meta(sid)
        self.assertEqual(meta["provider_job"]["status"], "fallback")
        self.assertEqual(meta["provider_job"]["reason"],
                         "demo_provider_queue_capacity")
        review = meta["checkpoints"]["end"]["student_review"]
        self.assertEqual(review["worked_review_version"],
                         ASSESSMENT_WORKED_REVIEW_VERSION)
        self.assertTrue(review["review_examples"])

    def test_service_closing_fallback_still_grounded(self):
        service = self._service()
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        rows = grounded_rows(
            self.bank, [positions(self.bank, "pct", 0)[0]])
        for row in rows[:-1]:
            service.submit_response(sid, token, row)
        service._closing = True
        service.submit_response(sid, token, rows[-1])
        meta = service._load_meta(sid)
        self.assertEqual(meta["provider_job"]["status"], "fallback")
        self.assertEqual(meta["provider_job"]["reason"],
                         "service_closing")
        review = meta["checkpoints"]["end"]["student_review"]
        self.assertEqual(review["worked_review_version"],
                         ASSESSMENT_WORKED_REVIEW_VERSION)
        self.assertTrue(review["review_examples"])

    def _edit_sections(self):
        return {key: f"Edited {key}."
                for key in ("assessment_summary", "observed_strengths",
                            "review_focus", "next_steps")}

    def _two_card_session(self):
        service = self._service()
        wrong = sorted({positions(self.bank, "pct", 0)[0],
                        positions(self.bank, "alg", 0)[0]})
        sid, token, _ = self._complete(service, wrong)
        self._wait(service, sid)
        return service, sid, token

    def test_content_edit_updates_grounded_cards_and_release(self):
        service, sid, token = self._two_card_session()
        delivery = service.teacher_session(sid)["feedback_delivery"]
        preview = delivery["preview"]
        cards = preview["review_examples"]
        self.assertEqual(len(cards), 2)
        self.assertTrue(all(c["status"] == "ready" for c in cards))
        practices = preview["practice_activities"]
        original_cards = copy.deepcopy(cards)
        original_practices = copy.deepcopy(practices)
        original_skills = copy.deepcopy(preview["skills"])
        original_total = copy.deepcopy(preview["total"])
        content = {
            "section_titles": {"review_focus": "Teacher heading"},
            "skill_labels": ["Pct label", "Alg label", "Div label",
                             "Fri label"],
            "review_examples": [
                {"title": "Edited card title",
                 "prompt": "Edited prompt?",
                 "options": ["A", "B", "C"],
                 "selected_answer": "A",
                 "correct_answer": "B",
                 "steps": ["First edited step.", "Second edited step."],
                 "explanation": "Edited explanation.",
                 "reasoning_question": "Why is B correct?",
                 "notice": "Edited notice."},
                {"prompt": "Second card edit."}],
            "practice_activities": [
                {"topic_label": "Edited topic",
                 "message": "Edited practice message.",
                 "notice": "Edited practice notice."},
                {}]}
        res = service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "sections": self._edit_sections(),
            "content": content,
            "reviewer_label": "ms-edit"})
        self.assertTrue(res["saved"])
        meta = service._load_meta(sid)
        self.assertEqual(meta["feedback_edit"]["source_preview"],
                         preview)
        delivery = service.teacher_session(sid)["feedback_delivery"]
        edited = delivery["preview"]
        self.assertEqual(delivery["preview_source"], "teacher_edited")
        self.assertEqual(edited["total"], original_total)
        focus = next(s for s in edited["sections"]
                     if s["kind"] == "review_focus")
        self.assertEqual(focus["title"], "Teacher heading")
        for skill, label, original in zip(
                edited["skills"],
                content["skill_labels"], original_skills):
            self.assertEqual(skill["skill_name"], label)
            self.assertTrue(skill["teacher_edited"])
            self.assertEqual(skill["correct"], original["correct"])
            self.assertEqual(skill["out_of"], original["out_of"])
            self.assertEqual(skill["skill_id"], original["skill_id"])
        card0, card1 = edited["review_examples"]
        self.assertEqual(card0["title"], "Edited card title")
        self.assertEqual(card0["prompt"], "Edited prompt?")
        self.assertEqual(card0["options"], ["A", "B", "C"])
        self.assertEqual(card0["selected_answer"], "A")
        self.assertEqual(card0["correct_answer"], "B")
        self.assertEqual(card0["steps"],
                         ["First edited step.", "Second edited step."])
        self.assertEqual(card0["reasoning_question"],
                         "Why is B correct?")
        self.assertEqual(card0["notice"], "Edited notice.")
        self.assertTrue(card0["teacher_edited"])
        self.assertEqual(card0["original_content"],
                         original_cards[0])
        self.assertEqual(card1["prompt"], "Second card edit.")
        self.assertTrue(card1["teacher_edited"])
        self.assertEqual(card1["original_content"],
                         original_cards[1])
        for card, original in zip(edited["review_examples"],
                                  original_cards):
            for locked in ("question_id", "candidate_id", "skill_id",
                           "subtopic_id", "status", "source",
                           "version", "selection_policy",
                           "solution_version"):
                self.assertEqual(card[locked], original[locked])
        self.assertNotIn("review_example", edited)
        practice0, practice1 = edited["practice_activities"]
        self.assertEqual(practice0["topic_label"], "Edited topic")
        self.assertEqual(practice0["message"],
                         "Edited practice message.")
        self.assertEqual(practice0["notice"],
                         "Edited practice notice.")
        self.assertTrue(practice0["teacher_edited"])
        self.assertEqual(practice0["original_content"],
                         original_practices[0])
        self.assertNotIn("teacher_edited", practice1)
        self.assertEqual(edited["practice"], practice0)
        self.assertIsNone(
            service.snapshot(sid, token)["feedback"]["end"])
        service.release_feedback(
            sid, {"message_sha256": res["preview_sha256"]})
        released = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(released["review_examples"], [card0, card1])
        self.assertEqual(released["practice_activities"][0],
                         practice0)
        service.save_feedback_edit(sid, {
            "message_sha256": res["preview_sha256"],
            "sections": self._edit_sections(),
            "content": {"review_examples":
                        [{"prompt": "Post-release edit."}, {}]}})
        still = service.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(still["review_examples"][0]["prompt"],
                         "Edited prompt?")
        meta = service._load_meta(sid)
        card0 = meta["feedback_edit"]["preview"]["review_examples"][0]
        self.assertEqual(card0["prompt"], "Post-release edit.")
        self.assertEqual(card0["original_content"],
                         original_cards[0])

    def test_content_edit_persists_across_restart(self):
        service, sid, token = self._two_card_session()
        delivery = service.teacher_session(sid)["feedback_delivery"]
        res = service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "sections": self._edit_sections(),
            "content": {"review_examples":
                        [{"title": "Kept title"}, {}]}})
        reopened = self._service()
        preview = reopened.teacher_session(sid)[
            "feedback_delivery"]["preview"]
        self.assertEqual(preview["review_examples"][0]["title"],
                         "Kept title")
        self.assertTrue(
            preview["review_examples"][0]["teacher_edited"])
        self.assertIsNotNone(
            reopened._load_meta(sid)["feedback_edit"]
            ["source_preview"])
        reopened.release_feedback(
            sid, {"message_sha256": res["preview_sha256"]})
        end = reopened.snapshot(sid, token)["feedback"]["end"]
        self.assertEqual(end["review_examples"][0]["title"],
                         "Kept title")

    def test_content_edit_rejects_invalid_and_absent_fields(self):
        service, sid, token = self._two_card_session()
        delivery = service.teacher_session(sid)["feedback_delivery"]
        sha = delivery["preview_sha256"]

        def attempt(content):
            service.save_feedback_edit(sid, {
                "message_sha256": sha,
                "sections": self._edit_sections(),
                "content": content})

        bad = [
            "not-a-dict",
            {"total": {"correct": 40, "out_of": 40}},
            {"sections": {}},
            {"section_titles": {"bogus": "x"}},
            {"section_titles": {}},
            {"skill_labels": ["only", "two"]},
            {"skill_labels": "x"},
            {"worked_review_notice": "no such field"},
            {"review_examples": [{}]},
            {"review_examples": [{}, {}, {}]},
            {"review_examples": "x"},
            {"review_examples": [{"question_id": "forged"}, {}]},
            {"review_examples": [{"status": "ready"}, {}]},
            {"review_examples": [{"teacher_edited": False}, {}]},
            {"review_examples": [{"original_content": {}}, {}]},
            {"review_examples": [{"options": ["a", "b"]}, {}]},
            {"review_examples": [{"title": ""}, {}]},
            {"review_examples": [{"steps": []}, {}]},
            {"review_examples": [{"steps": ["ok", 7]}, {}]},
            {"review_examples": [{"prompt": "x" * 2001}, {}]},
            {"review_examples": [{"prompt": "bad\x01"}, {}]},
            {"review_examples": [{"options":
                                  ["a"] * 31}, {}]},
            {"practice_activities": [{}]},
            {"practice_activities": [{"notice": None}, {}]},
            {"practice_activities": [{"status": "selected"}, {}]},
            {"practice_activities":
             [{"question": {"text": "x"}}, {}]},
            {"practice_activities":
             [{"relationship": {"kind": "same_content"}}, {}]},
            {"practice_activities":
             [{"question": {}}, {}]}]
        for content in bad:
            with self.assertRaises(ValueError, msg=repr(content)):
                attempt(content)
        body = {
            "message_sha256": sha,
            "sections": self._edit_sections(),
            "content": None}
        with self.assertRaises(ValueError):
            service.save_feedback_edit(sid, body)
        for key in ("section_titles", "skill_labels",
                    "review_examples", "practice_activities",
                    "worked_review_notice"):
            with self.assertRaises(ValueError, msg=key):
                attempt({key: None})
        body = {
            "message_sha256": "f" * 64,
            "sections": self._edit_sections(),
            "content": {"review_examples": [{}, {}]}}
        with self.assertRaises(ConflictError):
            service.save_feedback_edit(sid, body)
        res = service.save_feedback_edit(sid, {
            "message_sha256": sha,
            "sections": self._edit_sections(),
            "content": {"review_examples":
                        [{"title": "<script>alert(1)</script>"}, {}]}})
        card = service.teacher_session(sid)["feedback_delivery"][
            "preview"]["review_examples"][0]
        self.assertEqual(card["title"], "<script>alert(1)</script>")
        self.assertTrue(card["teacher_edited"])
        stale = service.save_feedback_edit
        body = {"message_sha256": sha,
                "sections": self._edit_sections(),
                "content": {"review_examples":
                            [{"title": "Again"}, {}]}}
        with self.assertRaises(ConflictError):
            stale(sid, body)
        res2 = stale(sid, {
            "message_sha256": res["preview_sha256"],
            "sections": self._edit_sections(),
            "content": {"review_examples": [{"prompt": "Again."}, {}]}})
        card = service.teacher_session(sid)["feedback_delivery"][
            "preview"]["review_examples"][0]
        self.assertEqual(card["prompt"], "Again.")
        self.assertEqual(card["original_content"]["title"],
                         "Worked review of your assessment answer")
        self.assertNotIn("original_content",
                         card["original_content"])

    def test_content_edit_unavailable_grounding_and_singleton(self):
        service = self._service()
        sid, token, _ = self._complete(
            service, [positions(self.bank, "pct", 0)[0]])
        self._wait(service, sid)
        meta = service._load_meta(sid)
        review = meta["checkpoints"]["end"]["student_review"]
        review["review_examples"] = []
        review["worked_review_status"] = "unavailable"
        review["worked_review_notice"] = "Original notice."
        service._save_meta(meta)
        delivery = service.teacher_session(sid)["feedback_delivery"]
        res = service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "sections": self._edit_sections(),
            "content": {
                "review_examples": [],
                "worked_review_notice": "Edited notice."}})
        preview = service.teacher_session(sid)[
            "feedback_delivery"]["preview"]
        self.assertEqual(preview["worked_review_notice"],
                         "Edited notice.")
        self.assertEqual(preview["review_examples"], [])
        body = {
            "message_sha256": res["preview_sha256"],
            "sections": self._edit_sections(),
            "content": {"review_examples": [{"title": "x"}]}}
        with self.assertRaises(ValueError):
            service.save_feedback_edit(sid, body)

    def test_sections_only_save_preserves_custom_titles(self):
        service, sid, token = self._two_card_session()
        delivery = service.teacher_session(sid)["feedback_delivery"]
        res = service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "sections": self._edit_sections(),
            "content": {"section_titles":
                        {"review_focus": "Custom kept heading"}}})
        res = service.save_feedback_edit(sid, {
            "message_sha256": res["preview_sha256"],
            "sections": {key: f"Again {key}."
                         for key in ("assessment_summary",
                                     "observed_strengths",
                                     "review_focus",
                                     "next_steps")}})
        preview = service.teacher_session(sid)[
            "feedback_delivery"]["preview"]
        focus = next(s for s in preview["sections"]
                     if s["kind"] == "review_focus")
        self.assertEqual(focus["title"], "Custom kept heading")
        self.assertEqual(focus["text"], "Again review_focus.")

    def test_null_notice_card_unmarked_without_edit(self):
        preview = {
            "sections": [{"kind": "review_focus", "title": "Focus",
                          "text": "text"}],
            "practice": {"status": "unavailable",
                         "message": "Unavailable.", "notice": None,
                         "question": None}}
        DemoService._apply_feedback_content(
            preview, {"practice_activities": [{}]})
        self.assertIsNone(preview["practice"]["notice"])
        self.assertNotIn("teacher_edited", preview["practice"])
        DemoService._apply_feedback_content(
            preview, {"practice_activities": [{"notice": None}]})
        self.assertIsNone(preview["practice"]["notice"])
        self.assertNotIn("teacher_edited", preview["practice"])
        DemoService._apply_feedback_content(
            preview, {"practice_activities": [{"notice": "n"}]})
        self.assertEqual(preview["practice"]["notice"], "n")
        self.assertTrue(preview["practice"]["teacher_edited"])


class ConciseStudentPreviewTests(unittest.TestCase):
    def _fixture(self, wrong=(), unsupported_fri=False,
                 practices=None):
        bank = make_grounded_bank(unsupported_fri=unsupported_fri)
        taxonomy = make_grounded_taxonomy(bank)
        rows = grounded_rows(bank, wrong)
        evidence = build_evidence(bank, taxonomy, rows)
        candidates = build_candidates(evidence, "end")
        focused = [c for c in candidates if c["focus"] is not None]
        examples = build_assessment_review_examples(
            evidence, focused, context_for(bank, taxonomy, rows))
        sections = multi_baseline_sections(
            evidence, candidates, review_examples=examples)
        preview = {
            "template_version":
                "phase3_student_feedback_template_v7",
            "sections": sections,
            "text": "\n\n".join(
                s["text"] for s in sections if s["text"]),
            "total": {"correct": evidence["total"]["correct"],
                      "out_of": evidence["total"]["out_of"]},
            "skills": [{"skill_id": s["skill_id"],
                        "skill_name": s["skill_name"],
                        "correct": s["correct"],
                        "out_of": s["out_of"]}
                       for s in evidence["skills"]],
            "requires_educator_review": True,
            "review_examples": examples}
        if practices:
            preview["practice_activities"] = practices
            preview["practice"] = practices[0]
        return preview, candidates, evidence

    @staticmethod
    def _texts(preview):
        return {s["kind"]: s["text"] for s in preview["sections"]}

    def test_titles_focus_guards_and_preserved_payload(self):
        bank = make_grounded_bank()
        wrong = (positions(bank, "pct", 0)[0],
                 positions(bank, "div", 0)[0])
        preview, candidates, evidence = self._fixture(wrong)
        original = copy.deepcopy(preview)
        result = concise_student_preview(preview, candidates)
        self.assertEqual(preview, original)
        self.assertEqual(result["template_version"],
                         CONCISE_TEMPLATE_VERSION)
        self.assertEqual(
            [s["title"] for s in result["sections"]],
            ["Assessment complete", "What went well",
             "Answers to review", "What to do next"])
        texts = self._texts(result)
        self.assertEqual(texts["assessment_summary"],
                         self._texts(original)["assessment_summary"])
        self.assertEqual(texts["observed_strengths"],
                         self._texts(original)["observed_strengths"])
        self.assertTrue(
            texts["review_focus"].startswith("Review these topics:"))
        for name in ("Percentage of an amount", "Divisibility"):
            self.assertIn(name + ".", texts["review_focus"])
        self.assertIn("One answer was incorrect here",
                      texts["review_focus"])
        self.assertIn("Other areas had the same review priority",
                      texts["review_focus"])
        self.assertIn("not overall mastery", texts["review_focus"])
        self.assertNotIn("draft, not a diagnosis", result["text"])
        self.assertNotIn("If your teacher approves", result["text"])
        self.assertEqual(result["text"],
                         "\n\n".join(s["text"] for s in
                                     result["sections"]))
        self.assertEqual(result["total"], original["total"])
        self.assertEqual(result["skills"], original["skills"])
        for before, after in zip(original["review_examples"],
                                 result["review_examples"]):
            self.assertEqual(after["notice"], CONCISE_READY_NOTICE)
            for key in ("prompt", "options", "selected_answer",
                        "correct_answer", "steps", "status"):
                self.assertEqual(after[key], before[key])

    def test_all_correct_claims_no_weakness(self):
        preview, candidates, _ = self._fixture(())
        result = concise_student_preview(preview, candidates)
        texts = self._texts(result)
        self.assertEqual(texts["review_focus"],
                         "You answered all questions correctly on "
                         "this assessment.")
        self.assertEqual(texts["next_steps"],
                         "Keep building on these results.")
        self.assertNotIn("practice activit", texts["next_steps"])

    def test_unavailable_example_and_abstained_practice(self):
        bank = make_grounded_bank(unsupported_fri=True)
        wrong = (positions(bank, "fri", 0)[0],)
        preview, candidates, _ = self._fixture(
            wrong, unsupported_fri=True,
            practices=[{"status": "abstained",
                        "message": "No eligible practice activity.",
                        "notice": None, "question": None}])
        result = concise_student_preview(preview, candidates)
        example = result["review_examples"][0]
        self.assertEqual(example["status"], "unavailable")
        self.assertEqual(example["notice"], UNAVAILABLE_NOTICE)
        texts = self._texts(result)
        self.assertEqual(
            texts["next_steps"],
            "Review the topics listed above and explain how you "
            "approached those questions. For any question without a "
            "checked worked solution, ask your teacher for help.")

    def test_practice_relationship_and_notice_projection(self):
        bank = make_grounded_bank()
        wrong = (positions(bank, "pct", 0)[0],
                 positions(bank, "div", 0)[0])
        same = {"status": "selected", "message": "m",
                "notice": PRACTICE_SELECTION_NOTICE,
                "question": {"skill_name": "Prosenttilaskenta",
                             "concept_name": "Concept pct",
                             "text": "t", "options": []},
                "relationship": {"kind": "same_content",
                                 "message": "old"}}
        related = {"status": "selected", "message": "m",
                   "notice": PRACTICE_SELECTION_NOTICE,
                   "question": {"skill_name": "Jaollisuus",
                                "concept_name": "Concept div",
                                "text": "t", "options": []},
                   "relationship": {
                       "kind": "same_topic_different_content",
                       "message": "old"}}
        preview, candidates, _ = self._fixture(
            wrong, practices=[same, related])
        result = concise_student_preview(preview, candidates)
        first, second = result["practice_activities"]
        self.assertEqual(first["relationship"]["kind"], "same_content")
        self.assertEqual(first["relationship"]["message"],
                         "This practises the same concept as the "
                         "review above.")
        self.assertEqual(second["relationship"]["kind"],
                         "same_topic_different_content")
        self.assertEqual(second["relationship"]["message"],
                         "This is related practice on Concept div, "
                         "not Divisibility.")
        for card in (first, second):
            self.assertEqual(card["notice"], CONCISE_PRACTICE_NOTICE)
        self.assertTrue(self._texts(result)["next_steps"].endswith(
            "Then try the practice activities below."))


class QuestionFirstPreviewTests(unittest.TestCase):
    def _concise(self, wrong=(), unsupported_fri=False, practices=None):
        preview, candidates, _ = ConciseStudentPreviewTests._fixture(
            self, wrong=wrong, unsupported_fri=unsupported_fri,
            practices=practices)
        return concise_student_preview(preview, candidates)

    @staticmethod
    def _texts(preview):
        return {s["kind"]: s["text"] for s in preview["sections"]}

    def test_scaffold_covers_all_supported_methods(self):
        self.assertEqual(len(worked_review_math.PRE_REVEAL_SCAFFOLDS),
                         20)
        self.assertEqual(len(worked_review_math.PRE_REVEAL_ALIASES), 3)
        covered = (set(worked_review_math.PRE_REVEAL_SCAFFOLDS)
                   | set(worked_review_math.PRE_REVEAL_ALIASES))
        self.assertEqual(covered,
                         set(worked_review_math._SOLUTION_HANDLERS))
        for key, wording in worked_review_math.PRE_REVEAL_SCAFFOLDS.items():
            scaffold = worked_review_math.pre_reveal_scaffold(key)
            self.assertEqual(scaffold["socratic_prompt"], wording[0])
            self.assertEqual(scaffold["progressive_hints"],
                             list(wording[1:]))
            self.assertEqual(len(scaffold["progressive_hints"]), 2)
        for alias, target in worked_review_math.PRE_REVEAL_ALIASES.items():
            self.assertEqual(
                worked_review_math.pre_reveal_scaffold(alias),
                worked_review_math.pre_reveal_scaffold(target))

    def test_unknown_subtopic_falls_back_to_generic_prompt(self):
        scaffold = worked_review_math.pre_reveal_scaffold("unknown_x")
        self.assertEqual(scaffold["socratic_prompt"],
                         worked_review_math.GENERIC_SOCRATIC_PROMPT)
        self.assertEqual(scaffold["progressive_hints"], [])

    def test_overlay_fields_preserve_source_and_input(self):
        bank = make_grounded_bank()
        wrong = (positions(bank, "pct", 0)[0],
                 positions(bank, "div", 0)[0])
        concise = self._concise(wrong)
        original = copy.deepcopy(concise)
        result = question_first_preview(concise)
        self.assertEqual(concise, original)
        self.assertEqual(result["template_version"],
                         QUESTION_FIRST_TEMPLATE_VERSION)
        self.assertEqual(result["review_presentation_version"],
                         QUESTION_FIRST_VERSION)
        self.assertNotIn("review_presentation_version", concise)
        self.assertEqual(concise["template_version"],
                         CONCISE_TEMPLATE_VERSION)
        sections = {s["kind"]: s for s in result["sections"]}
        concise_sections = {s["kind"]: s for s in concise["sections"]}
        self.assertEqual(sections["review_focus"]["title"],
                         concise_sections["review_focus"]["title"])
        self.assertEqual(sections["next_steps"]["title"],
                         concise_sections["next_steps"]["title"])
        self.assertTrue(sections["next_steps"]["text"].startswith(
            QUESTION_FIRST_NEXT_STEPS))
        self.assertEqual(
            result["text"],
            "\n\n".join(s["text"] for s in result["sections"]
                        if s["text"]))
        for before, after in zip(concise["review_examples"],
                                 result["review_examples"]):
            for key in ("prompt", "options", "selected_answer",
                        "correct_answer", "steps",
                        "reasoning_question", "status", "question_id",
                        "subtopic_id"):
                self.assertEqual(after[key], before[key])
            self.assertNotIn("socratic_prompt", before)
            self.assertEqual(after["presentation_version"],
                             QUESTION_FIRST_VERSION)
            self.assertEqual(after["scaffold_source"],
                             "application_authored_method_prompt_v1")
            self.assertEqual(
                after["socratic_prompt"],
                worked_review_math.pre_reveal_scaffold(
                    after["subtopic_id"])["socratic_prompt"])
            self.assertEqual(len(after["progressive_hints"]), 2)
            self.assertEqual(after["option_contrast"], "")
            self.assertIsNone(after["option_contrast_source"])
            self.assertEqual(after["notice"], QUESTION_FIRST_READY_NOTICE)

    @staticmethod
    def _mini(cards):
        sections = [{"kind": "review_focus", "title": "t",
                     "text": "focus"},
                    {"kind": "next_steps", "title": "t",
                     "text": "next"}]
        return {"sections": sections, "text": "focus\n\nnext",
                "review_examples": cards}

    def test_unavailable_card_generic_prompt_no_hints(self):
        bank = make_grounded_bank(unsupported_fri=True)
        pos = positions(bank, "fri", 0)[0]
        concise = self._concise(
            (pos,), unsupported_fri=True,
            practices=[{"status": "abstained",
                        "message": "No eligible practice activity.",
                        "notice": None, "question": None}])
        self.assertEqual(concise["review_examples"][0]["status"],
                         "unavailable")
        result = question_first_preview(concise)
        card = result["review_examples"][0]
        self.assertEqual(card["socratic_prompt"],
                         worked_review_math.GENERIC_SOCRATIC_PROMPT)
        self.assertEqual(card["progressive_hints"], [])
        self.assertEqual(card["option_contrast"], "")
        self.assertIsNone(card["option_contrast_source"])
        self.assertEqual(card["scaffold_source"],
                         "application_authored_method_prompt_v1")
        self.assertEqual(card["notice"], UNAVAILABLE_NOTICE)

    def test_all_correct_keeps_concise_next_steps_no_focus(self):
        concise = self._concise(())
        result = question_first_preview(concise)
        self.assertEqual(result["review_examples"], [])
        self.assertEqual(result["template_version"],
                         QUESTION_FIRST_TEMPLATE_VERSION)
        self.assertEqual(self._texts(result), self._texts(concise))
        self.assertEqual(self._texts(result)["next_steps"],
                         "Keep building on these results.")

    def test_next_steps_mixed_availability_and_practice_counts(self):
        bank = make_grounded_bank()
        wrong = (positions(bank, "pct", 0)[0],)
        practice = lambda i: {
            "status": "selected", "message": "m", "notice": None,
            "question": {"skill_name": "Prosenttilaskenta",
                         "concept_name": f"Concept {i}",
                         "text": "t", "options": []},
            "relationship": {"kind": "different_content",
                             "message": "x"}}
        for count in (0, 1, 2):
            concise = self._concise(
                wrong, practices=[practice(i) for i in range(count)])
            result = question_first_preview(concise)
            text = self._texts(result)["next_steps"]
            self.assertTrue(text.startswith(QUESTION_FIRST_NEXT_STEPS))
            if count == 0:
                self.assertNotIn("practice activit", text)
            elif count == 1:
                self.assertTrue(text.endswith(
                    "Then try the practice activity below."))
            else:
                self.assertTrue(text.endswith(
                    "Then try the practice activities below."))
        concise_mixed = self._concise(
            (positions(bank, "pct", 0)[0],
             positions(bank, "fri", 0)[0]),
            unsupported_fri=True)
        result = question_first_preview(concise_mixed)
        text = self._texts(result)["next_steps"]
        self.assertTrue(text.startswith(QUESTION_FIRST_NEXT_STEPS))
        self.assertIn("ask your teacher for help", text)

    def test_preserve_narrative_keeps_sections_and_versions(self):
        bank = make_grounded_bank()
        wrong = (positions(bank, "pct", 0)[0],
                 positions(bank, "div", 0)[0])
        full = self._concise(wrong)
        full["template_version"] = (
            "phase3_student_feedback_worked_first_full_v1")
        full["review_presentation_version"] = (
            "worked_first_full_feedback_v1")
        original = copy.deepcopy(full)
        result = question_first_preview(full, preserve_narrative=True)
        self.assertEqual(full, original)
        self.assertEqual(result["template_version"],
                         original["template_version"])
        self.assertEqual(result["review_presentation_version"],
                         original["review_presentation_version"])
        self.assertEqual(result["sections"], original["sections"])
        self.assertEqual(result["text"], original["text"])
        for before, after in zip(original["review_examples"],
                                 result["review_examples"]):
            self.assertEqual(after["presentation_version"],
                             QUESTION_FIRST_VERSION)
            self.assertEqual(after["scaffold_source"],
                             "application_authored_method_prompt_v1")
            self.assertTrue(after["socratic_prompt"])
            self.assertEqual(after["steps"], before["steps"])
            self.assertEqual(after["notice"],
                             QUESTION_FIRST_READY_NOTICE)

    def test_preserve_narrative_unavailable_generic_fallback(self):
        bank = make_grounded_bank(unsupported_fri=True)
        pos = positions(bank, "fri", 0)[0]
        full = self._concise((pos,), unsupported_fri=True)
        result = question_first_preview(full, preserve_narrative=True)
        card = result["review_examples"][0]
        self.assertEqual(card["socratic_prompt"],
                         worked_review_math.GENERIC_SOCRATIC_PROMPT)
        self.assertEqual(card["progressive_hints"], [])
        self.assertEqual(card["option_contrast"], "")
        self.assertIsNone(card["option_contrast_source"])
        self.assertEqual(card["presentation_version"],
                         QUESTION_FIRST_VERSION)
        self.assertEqual(result["sections"], full["sections"])
        self.assertEqual(result["text"], full["text"])


EXPECTED_WARM_CONTRASTS = {
    ("32978__p2__qad057f930f90539d__of1e565b0fb849511", "7w"):
        "Your selected option uses w, while both terms in the "
        "question use v. How did you choose the variable letter in "
        "your answer?",
    ("32978__p2__qad057f930f90539d__of1e565b0fb849511", "5v2v"):
        "Your selected option places the terms next to each other "
        "without the original plus sign. How did you represent the "
        "addition?",
    ("512961__p2__q5f787a330af2b755__o03da091c4f2e8d2f", "2xy"):
        "Your selected option writes x and y together as a product. "
        "What operation is shown between x and y in the question?"}


@requires_private_artifacts(*FROZEN_RESEARCH_FILES)
class WarmBankQuestionFirstTests(unittest.TestCase):
    def setUp(self):
        self.bank, self.taxonomy, _ = load_research_bank("warm")
        self.questions = {q["question_id"]: q
                          for q in self.bank["questions"]}
        self.leaf_for = {}
        for topic in self.taxonomy["topics"]:
            for leaf in topic["subtopics"]:
                for qid in leaf["question_ids"]:
                    self.leaf_for[qid] = leaf["id"]

    def _card(self, qid, selected):
        question = self.questions[qid]
        correct = question["options"][question["answer_index"]]
        self.assertNotEqual(selected, correct)
        solved = worked_review_math.solution(
            question, self.leaf_for[qid])
        return {"source": "incorrect_assessment_response",
                "status": "ready", "question_id": qid,
                "prompt": question["text"],
                "options": list(question["options"]),
                "selected_answer": selected,
                "correct_answer": correct,
                "subtopic_id": self.leaf_for[qid],
                "steps": solved["steps"],
                "reasoning_question": solved["reasoning_question"],
                "solution_version": solved["solution_version"],
                "notice": CONCISE_READY_NOTICE}

    @staticmethod
    def _overlay(card):
        return question_first_preview(
            QuestionFirstPreviewTests._mini([card])
        )["review_examples"][0]

    def test_exact_contrast_records_from_actual_bank(self):
        for (qid, selected), expected in EXPECTED_WARM_CONTRASTS.items():
            out = self._overlay(self._card(qid, selected))
            self.assertEqual(out["option_contrast"], expected)
            self.assertEqual(
                out["option_contrast_source"],
                "application_authored_exact_option_observation_v1")
            scaffold = worked_review_math.pre_reveal_scaffold(
                out["subtopic_id"])
            self.assertEqual(out["socratic_prompt"],
                             scaffold["socratic_prompt"])
            self.assertEqual(len(out["progressive_hints"]), 2)
            self.assertEqual(out["notice"], QUESTION_FIRST_READY_NOTICE)

    def test_contrast_suppressed_by_each_mismatch(self):
        for qid, selected in EXPECTED_WARM_CONTRASTS:
            mutations = (
                lambda c: c.update(question_id="other_qid"),
                lambda c: c.update(prompt="Muutettu kysymys?"),
                lambda c: c.update(
                    options=list(reversed(c["options"]))),
                lambda c: c.update(correct_answer="nomatch"),
                lambda c: c.update(subtopic_id="divisibility"),
                lambda c: c.update(status="unavailable", steps=[]))
            for mutate in mutations:
                card = self._card(qid, selected)
                mutate(card)
                out = self._overlay(card)
                self.assertEqual(out["option_contrast"], "",
                                 (qid, selected))
                self.assertIsNone(out["option_contrast_source"])

    def test_all_wrong_options_preserve_answers_and_steps(self):
        seen = set()
        for qid, question in self.questions.items():
            leaf = self.leaf_for[qid]
            scaffold = worked_review_math.pre_reveal_scaffold(leaf)
            self.assertEqual(len(scaffold["progressive_hints"]), 2,
                             (qid, leaf))
            correct = question["options"][question["answer_index"]]
            for option in question["options"]:
                if option == correct:
                    continue
                card = self._card(qid, option)
                out = self._overlay(card)
                self.assertEqual(out["selected_answer"], option)
                self.assertEqual(out["correct_answer"], correct)
                self.assertEqual(out["steps"], card["steps"])
                self.assertTrue(out["steps"])
                self.assertEqual(out["presentation_version"],
                                 QUESTION_FIRST_VERSION)
                if out["option_contrast"]:
                    seen.add((qid, option))
                    self.assertEqual(out["option_contrast"],
                                     EXPECTED_WARM_CONTRASTS[
                                         (qid, option)])
        self.assertEqual(seen, set(EXPECTED_WARM_CONTRASTS))


SELECTION_WRONG = (0, 4, 8, 1, 5)


def selection_diagnostics(probabilities):
    return {"status": "ready", "checkpoint": "end", "answer_count": 40,
            "kt": {"kind": "frozen_variant_d_response_trace",
                   "coverage": {
                       "question_count": 40,
                       "missing_skill_count": 0,
                       "missing_content_count": 0,
                       "missing_option_count": 0},
                   "p_correct_before_each_answer":
                       list(probabilities)}}


def selection_probabilities():
    probs = [0.5] * 40
    probs[0] = 0.8
    probs[4] = 0.2
    probs[8] = 0.2
    probs[1] = 0.1
    probs[5] = 0.7
    probs[12] = 0.001  # answered correctly; must be excluded
    return probs


class WorkedExampleSelectionTests(unittest.TestCase):
    """Experimental KT worked-example selection inside the frozen
    assessment context; private provenance stays off the wire."""

    def setUp(self):
        self.bank = make_grounded_bank()
        self.taxonomy = make_grounded_taxonomy(self.bank)
        self.rows = grounded_rows(self.bank, SELECTION_WRONG)
        self.evidence = build_evidence(
            self.bank, self.taxonomy, self.rows)
        self.snapshot = worked_example_selection.freeze_selection(
            self.bank, self.rows,
            selection_diagnostics(selection_probabilities()))
        self.context = {
            **context_for(self.bank, self.taxonomy, self.rows),
            worked_example_selection.CONTEXT_KEY: self.snapshot}
        self.pct = candidate_for(self.evidence, "percentage_amount")
        self.alg = candidate_for(self.evidence, "combine_like_terms")

    def _cards(self, candidates=None, context=None):
        return build_assessment_review_examples(
            self.evidence, candidates or [self.pct, self.alg],
            context if context is not None else self.context)

    def _qids(self, skill):
        return {q["question_id"] for q in self.bank["questions"]
                if q["skill_id"] == skill}

    def test_lowest_pre_answer_kt_beats_earliest_position(self):
        cards = self._cards()
        self.assertEqual(cards[0]["question_id"],
                         self.bank["questions"][4]["question_id"])
        self.assertEqual(cards[1]["question_id"],
                         self.bank["questions"][1]["question_id"])
        for card in cards:
            self.assertEqual(
                card["selection_policy"],
                worked_example_selection.POLICY)
            provenance = card["selection_provenance"]
            self.assertEqual(
                provenance["policy"], worked_example_selection.POLICY)
            self.assertEqual(provenance["score_source"],
                             "historical_pre_answer_trace")
        prov = cards[0]["selection_provenance"]
        self.assertEqual(prov["source"], "kt_recommendation")
        self.assertEqual(prov["recommended_question_id"],
                         prov["selected_question_id"])
        self.assertIn("lowest pre-answer KT", prov["reason"])
        qids = {c["question_id"] for c in prov["candidates"]}
        # Only incorrect percentage questions: 0, 4, 8; correct index 12
        # and other skills are excluded.
        self.assertEqual(
            qids, {self.bank["questions"][i]["question_id"]
                   for i in (0, 4, 8)})
        self.assertNotIn(self.bank["questions"][12]["question_id"], qids)
        first = next(c for c in prov["candidates"]
                     if c["question_id"]
                     == self.bank["questions"][4]["question_id"])
        self.assertEqual(first["position"], 5)
        self.assertEqual(first["p_correct_before_answer"], 0.2)
        self.assertTrue(first["checked_solution_available"])

    def test_unchecked_solver_falls_to_next_checked_candidate(self):
        target = self.bank["questions"][4]["question_id"]
        original = worked_review_math.solution

        def solver(question, subtopic_id):
            if question["question_id"] == target:
                raise ValueError("no solver")
            return original(question, subtopic_id)

        with mock.patch.object(worked_review_math, "solution", solver):
            cards = self._cards([self.pct])
        self.assertEqual(cards[0]["question_id"],
                         self.bank["questions"][8]["question_id"])
        self.assertEqual(cards[0]["status"], "ready")
        self.assertFalse(
            next(c for c in cards[0]["selection_provenance"]
                   ["candidates"]
                   if c["question_id"] == target)
            ["checked_solution_available"])

    def test_all_unchecked_keeps_earliest_for_teacher_review(self):
        def solver(question, subtopic_id):
            raise ValueError("no solver")

        with mock.patch.object(worked_review_math, "solution", solver):
            cards = self._cards([self.pct])
        self.assertEqual(cards[0]["question_id"],
                         self.bank["questions"][0]["question_id"])
        self.assertEqual(cards[0]["status"], "unavailable")
        self.assertEqual(cards[0]["steps"], [])
        prov = cards[0]["selection_provenance"]
        self.assertEqual(prov["source"], "earliest_fallback")
        self.assertIn("teacher review", prov["reason"])

    def test_invalid_traces_freeze_unavailable_and_fallback(self):
        base = selection_diagnostics(selection_probabilities())

        def diag(**kt):
            result = copy.deepcopy(base)
            result["kt"].update(kt)
            return result

        cases = [
            diag(**{"p_correct_before_each_answer": [0.5] * 39}),
            diag(**{"p_correct_before_each_answer":
                    [0.5] * 39 + [float("nan")]}),
            diag(**{"p_correct_before_each_answer":
                    [0.5] * 39 + [True]}),
            diag(**{"p_correct_before_each_answer":
                    [0.5] * 39 + [1.5]}),
            diag(kind="other_trace_kind"),
            diag(**{"coverage": {"question_count": 39}}),
            {"status": "pending"},
            None,
            "garbage"]
        for broken in cases:
            frozen = worked_example_selection.freeze_selection(
                self.bank, self.rows, broken)
            self.assertIsNone(frozen["probabilities"], broken)
            self.assertEqual(
                frozen["score_source"],
                "unavailable_or_incompatible_trace")
            context = {**context_for(self.bank, self.taxonomy,
                                     self.rows),
                       worked_example_selection.CONTEXT_KEY: frozen}
            card = self._cards([self.pct], context=context)[0]
            self.assertEqual(card["question_id"],
                             self.bank["questions"][0]["question_id"])
            self.assertEqual(card["selection_provenance"]["source"],
                             "earliest_fallback")
            self.assertIn("unavailable or incompatible",
                          card["selection_provenance"]["reason"])

    def test_snapshot_rejects_mismatched_rows(self):
        other = grounded_rows(self.bank, (0, 1))
        snapshot = worked_example_selection.freeze_selection(
            self.bank, other,
            selection_diagnostics(selection_probabilities()))
        context = {**context_for(self.bank, self.taxonomy, self.rows),
                   worked_example_selection.CONTEXT_KEY: snapshot}
        with self.assertRaises(ValueError):
            self._cards(context=context)

    def test_teacher_override_selects_eligible_question(self):
        qid8 = self.bank["questions"][8]["question_id"]
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["overrides"][self.pct["candidate_id"]] = qid8
        context = {**context_for(self.bank, self.taxonomy, self.rows),
                   worked_example_selection.CONTEXT_KEY: snapshot}
        card = self._cards([self.pct], context=context)[0]
        self.assertEqual(card["question_id"], qid8)
        prov = card["selection_provenance"]
        self.assertEqual(prov["source"], "teacher_override")
        self.assertEqual(prov["recommended_question_id"],
                         self.bank["questions"][4]["question_id"])
        self.assertEqual(prov["selected_question_id"], qid8)

    def test_teacher_override_rejects_ineligible_questions(self):
        for qid in (self.bank["questions"][12]["question_id"],  # correct
                    self.bank["questions"][1]["question_id"],   # other skill
                    "nonexistent__qid"):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot["overrides"][self.pct["candidate_id"]] = qid
            context = {
                **context_for(self.bank, self.taxonomy, self.rows),
                worked_example_selection.CONTEXT_KEY: snapshot}
            with self.assertRaises(ValueError, msg=qid):
                self._cards([self.pct], context=context)

    def test_override_to_unavailable_keeps_unavailable_fields(self):
        target = self.bank["questions"][8]["question_id"]
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["overrides"][self.pct["candidate_id"]] = target
        context = {**context_for(self.bank, self.taxonomy, self.rows),
                   worked_example_selection.CONTEXT_KEY: snapshot}
        original = worked_review_math.solution

        def solver(question, subtopic_id):
            if question["question_id"] == target:
                raise ValueError("no solver")
            return original(question, subtopic_id)

        with mock.patch.object(worked_review_math, "solution", solver):
            card = self._cards([self.pct], context=context)[0]
        self.assertEqual(card["question_id"], target)
        self.assertEqual(card["status"], "unavailable")
        self.assertEqual(card["steps"], [])
        self.assertIsNone(card["solution_version"])
        self.assertEqual(card["notice"], UNAVAILABLE_NOTICE)

    def test_plain_context_keeps_earliest_policy(self):
        context = context_for(self.bank, self.taxonomy, self.rows)
        cards = self._cards(context=context)
        self.assertEqual(cards[0]["question_id"],
                         self.bank["questions"][0]["question_id"])
        self.assertEqual(
            cards[0]["selection_policy"],
            "earliest_incorrect_in_selected_subtopic_v1")
        self.assertNotIn("selection_provenance", cards[0])

    def test_zero_probability_is_valid(self):
        probs = selection_probabilities()
        probs[8] = 0.0
        snapshot = worked_example_selection.freeze_selection(
            self.bank, self.rows, selection_diagnostics(probs))
        context = {**context_for(self.bank, self.taxonomy, self.rows),
                   worked_example_selection.CONTEXT_KEY: snapshot}
        card = self._cards([self.pct], context=context)[0]
        self.assertEqual(card["question_id"],
                         self.bank["questions"][8]["question_id"])
        self.assertEqual(card["selection_provenance"]
                         ["p_correct_before_answer"], 0.0)

    def test_provider_wire_has_no_private_selection_data(self):
        payload = build_multi_full_input(
            self.evidence, "student",
            next(b for b in build_review_bundles(self.evidence)
                 if {c["focus"]["subtopic_id"]
                     for c in b["review_candidates"] if c["focus"]}
                 == {"percentage_amount", "combine_like_terms"}),
            [{"schema": "phase3_feedback_practice_context_v1",
              "status": "abstained", "selected_question_id": None,
              "pool_sha256": None},
             {"schema": "phase3_feedback_practice_context_v1",
              "status": "abstained", "selected_question_id": None,
              "pool_sha256": None}],
            assessment_context=self.context)
        chosen = {e["question_id"] for e in payload["review_examples"]}
        self.assertEqual(
            chosen, {self.bank["questions"][4]["question_id"],
                     self.bank["questions"][1]["question_id"]})
        opener = RecordingOpener(wire_reply(
            candidate_id=payload["selected_candidate"]["candidate_id"],
            worked_reviews=fixture_reviews(payload)))
        inner = AittaGenerator(API_KEY, BASE_URL, model=MODEL,
                               opener=opener)
        FullFeedbackAittaGenerator(inner).generate(
            copy.deepcopy(payload))
        body = opener.calls[0]["body"]
        context = json.loads(body["messages"][1]["content"])
        for card in context["review_examples"]:
            self.assertIn(card["question_id"], chosen)
        raw = json.dumps(context)
        for forbidden in ("selection_provenance",
                          "worked_example_selection",
                          "p_correct_before_answer",
                          "recommended_question_id",
                          "probabilities", "score_source",
                          "checked_solution_available", "overrides"):
            self.assertNotIn(forbidden, raw)


class WorkedExampleSelectionServiceTests(GroundedServiceTests):
    """End-to-end new-policy selection through the DemoService."""

    def _diagnostics(self, probabilities=None, gate=None):
        if gate is not None:
            return GatedDiagnostics(
                self.bank, gate,
                end_result=selection_diagnostics(
                    probabilities or selection_probabilities()))
        inner = selection_diagnostics(
            probabilities or selection_probabilities())
        bank = self.bank

        class Stub:
            def __init__(self):
                self.inner = LiveDiagnostics(
                    model_loader=lambda: FakeKT(bank))

            def warmup(self):
                return {"status": "ready"}

            @property
            def state(self):
                return {"status": "ready"}

            def evaluate(self, bank, taxonomy, rows):
                if len(rows) == 40:
                    return copy.deepcopy(inner)
                return self.inner.evaluate(bank, taxonomy, rows)

            def predict_future_candidates(self, *args):
                return self.inner.predict_future_candidates(*args)

        return Stub()

    def _selected_card(self, service, sid, candidate_index=0):
        review = service._load_meta(sid)["checkpoints"]["end"][
            "student_review"]
        return review["review_examples"][candidate_index]

    def test_recommendation_uses_kt_not_first_position(self):
        service = self._service(
            diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        meta = service._load_meta(sid)
        self.assertEqual(
            meta["worked_example_selection_policy"],
            worked_example_selection.POLICY)
        end = meta["checkpoints"]["end"]
        self.assertIn(worked_example_selection.CONTEXT_KEY, end)
        card = next(
            c for c in end["student_review"]["review_examples"]
            if c["subtopic_id"] == "percentage_amount")
        self.assertEqual(card["question_id"],
                         self.bank["questions"][4]["question_id"])
        self.assertEqual(card["selection_provenance"]["source"],
                         "kt_recommendation")

    def test_diagnostics_gate_blocks_final_generation(self):
        gate = threading.Event()
        service = self._service(
            diagnostics=self._diagnostics(gate=gate))
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            == "waiting_for_diagnostics"),
            "provider never entered waiting_for_diagnostics")
        self.assertNotIn(worked_example_selection.CONTEXT_KEY,
                         service._load_meta(sid)["checkpoints"]["end"])
        gate.set()
        self._wait(service, sid)
        end = service._load_meta(sid)["checkpoints"]["end"]
        self.assertIn(worked_example_selection.CONTEXT_KEY, end)
        card = next(
            c for c in end["student_review"]["review_examples"]
            if c["subtopic_id"] == "percentage_amount")
        self.assertEqual(card["question_id"],
                         self.bank["questions"][4]["question_id"])

    def test_frozen_snapshot_never_refrozen(self):
        service = self._service(diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        meta = service._load_meta(sid)
        end = meta["checkpoints"]["end"]
        snapshot = end[worked_example_selection.CONTEXT_KEY]
        changed = list(selection_probabilities())
        changed[0] = 0.0
        end["diagnostics"] = selection_diagnostics(changed)
        service._freeze_worked_example_selection(meta)
        self.assertEqual(
            end[worked_example_selection.CONTEXT_KEY], snapshot)

    def test_rules_mode_also_waits_and_selects(self):
        gate = threading.Event()
        service = self._service(
            diagnostics=self._diagnostics(gate=gate))
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self.assertTrue(wait_for(
            lambda: service._load_meta(sid)["provider_job"]["status"]
            == "waiting_for_diagnostics"))
        gate.set()
        self._wait(service, sid)
        card = self._selected_card(service, sid, 0)
        self.assertEqual(card["question_id"],
                         self.bank["questions"][4]["question_id"])

    def test_hosted_question_first_uses_selection(self):
        calls = []

        class FakeGenerator:
            supports_full_feedback = True
            execution = None

            def generate(self, payload):
                calls.append(payload)
                return {
                    "candidate_id": payload["selected_candidate"]
                    ["candidate_id"],
                    "sections": dict(VALID_SECTIONS),
                    "worked_reviews": fixture_reviews(payload)}

        service = self._service(
            provider_mode="hosted", generator=FakeGenerator(),
            diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        self.assertEqual(len(calls), 1)
        examples = calls[0]["review_examples"]
        self.assertEqual(examples[0]["question_id"],
                         self.bank["questions"][4]["question_id"])

    def test_hosted_worked_first_uses_selection(self):
        calls = []

        class FakeGenerator:
            supports_full_feedback = True
            execution = None

            def generate(self, payload):
                calls.append(payload)
                return {
                    "candidate_id": payload["selected_candidate"]
                    ["candidate_id"],
                    "sections": dict(VALID_SECTIONS),
                    "worked_reviews": fixture_reviews(payload)}

        service = self._service(
            provider_mode="hosted", generator=FakeGenerator(),
            diagnostics=self._diagnostics())
        created = service.create_session(
            feedback_variant="worked_first")
        sid, token = (created["session_id"],
                      created["student_token"])
        for row in grounded_rows(self.bank, SELECTION_WRONG):
            service.submit_response(sid, token, row)
        self._wait(service, sid)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["review_examples"][0]["question_id"],
                         self.bank["questions"][4]["question_id"])

    def test_teacher_override_rebuilds_draft_and_audits(self):
        service = self._service(diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        detail = service.teacher_session(sid)
        delivery = detail["feedback_delivery"]
        before_meta = service._load_meta(sid)
        before_end = before_meta["checkpoints"]["end"]
        before_candidates = copy.deepcopy(
            before_end["student_review"]["selected_candidates"])
        before_practice = copy.deepcopy(
            before_end["practice_contexts"])
        before_activities = copy.deepcopy(
            delivery["preview"].get("practice_activities"))
        choices = delivery["worked_example_choices"]
        pct = next(c for c in choices
                   if c["subtopic_name"] == "Percentage of an amount")
        self.assertEqual(
            pct["selected_question_id"],
            self.bank["questions"][4]["question_id"])
        self.assertEqual(
            pct["recommended_question_id"], pct["selected_question_id"])
        self.assertEqual(
            {c["question_id"] for c in pct["candidates"]},
            {self.bank["questions"][i]["question_id"]
             for i in (0, 4, 8)})
        qid8 = self.bank["questions"][8]["question_id"]
        res = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": qid8, "reviewer_label": "teacher-t"})
        self.assertTrue(res["saved"])
        self.assertNotEqual(res["preview_sha256"],
                            delivery["preview_sha256"])
        meta = service._load_meta(sid)
        end = meta["checkpoints"]["end"]
        # Topic selection and practice contexts are untouched by the
        # worked-example override.
        self.assertEqual(end["student_review"]["selected_candidates"],
                         before_candidates)
        self.assertEqual(end["practice_contexts"], before_practice)
        card = next(c for c in end["student_review"]["review_examples"]
                    if c["subtopic_id"] == "percentage_amount")
        self.assertEqual(card["question_id"], qid8)
        question = self.bank["questions"][8]
        row = grounded_rows(self.bank, SELECTION_WRONG)[8]
        # The rebuilt card matches the checked solver output exactly.
        solved = worked_review_math.solution(
            question, "percentage_amount")
        self.assertEqual(card["steps"], solved["steps"])
        self.assertEqual(card["prompt"], question["text"])
        self.assertEqual(card["options"], question["options"])
        self.assertEqual(card["selected_answer"],
                         question["options"][row["selected_index"]])
        self.assertEqual(card["correct_answer"],
                         question["options"][question["answer_index"]])
        # The question-first preview card carries the checked hints
        # for the overridden subtopic.
        preview_card = next(
            c for c in service.teacher_session(sid)
            ["feedback_delivery"]["preview"]["review_examples"]
            if c["question_id"] == qid8)
        scaffold = worked_review_math.pre_reveal_scaffold(
            "percentage_amount")
        self.assertEqual(preview_card["progressive_hints"],
                         scaffold["progressive_hints"])
        self.assertEqual(preview_card["socratic_prompt"],
                         scaffold["socratic_prompt"])
        prov = card["selection_provenance"]
        self.assertEqual(prov["source"], "teacher_override")
        self.assertEqual(prov["recommended_question_id"],
                         self.bank["questions"][4]["question_id"])
        self.assertEqual(
            end[worked_example_selection.CONTEXT_KEY]["overrides"]
            [pct["candidate_id"]], qid8)
        history = meta["worked_example_selection_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["question_id"], qid8)
        self.assertIsNotNone(history[0]["previous_student_review"])
        # The aggregate provider job keeps its original narrative
        # provenance; the scoped job alone records the override.
        self.assertEqual(meta["provider_job"],
                         before_meta["provider_job"])
        self.assertEqual(end["provider_job"],
                         before_end.get("provider_job"))
        self.assertEqual(
            end["student_review"]["trace"]["phrasing_source"],
            "deterministic")
        self.assertEqual(
            end["student_review"]["trace"]
            ["worked_example_override"]["question_id"], qid8)
        # Preview exposes no private ranking fields; choices persist.
        delivery = service.teacher_session(sid)["feedback_delivery"]
        self.assertEqual(delivery["preview"].get("practice_activities"),
                         before_activities)
        raw = json.dumps(delivery["preview"])
        for forbidden in ("selection_provenance",
                          "p_correct_before_answer",
                          "recommended_question_id"):
            self.assertNotIn(forbidden, raw)
        # The audit trail is exported to the teacher but never leaks
        # into the public snapshot.
        exported = service.teacher_session(sid)
        self.assertEqual(exported["worked_example_selection_history"],
                         history)
        snapshot = json.dumps(service.snapshot(sid, token))
        self.assertNotIn("worked_example_selection_history", snapshot)
        self.assertNotIn("worked_example_choices", snapshot)
        pct2 = next(c for c in delivery["worked_example_choices"]
                    if c["candidate_id"] == pct["candidate_id"])
        self.assertEqual(pct2["selected_question_id"], qid8)
        self.assertEqual(pct2["source"], "teacher_override")
        # Idempotent re-select keeps the same digest and history.
        again = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": qid8, "reviewer_label": "teacher-t"})
        self.assertEqual(again["preview_sha256"],
                         res["preview_sha256"])
        self.assertEqual(len(service._load_meta(sid)
                             ["worked_example_selection_history"]), 1)

    def test_override_freeform_conflict_and_structured_edit_kept(self):
        service = self._service(diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        delivery = service.teacher_session(sid)[
            "feedback_delivery"]
        old_hash = delivery["preview_sha256"]
        service.save_feedback_edit(sid, {
            "message_sha256": old_hash,
            "document_text": "Old question description rewrite.",
            "reviewer_label": "editor"})
        delivery = service.teacher_session(sid)[
            "feedback_delivery"]
        self.assertEqual(delivery["preview_source"], "teacher_edited")
        self.assertEqual(delivery["preview"].get("document_text"),
                         "Old question description rewrite.")
        pct = next(c for c in delivery["worked_example_choices"]
                   if c["subtopic_name"] == "Percentage of an amount")
        qid8 = self.bank["questions"][8]["question_id"]
        # A saved free-form document cannot be patched per example;
        # the override is rejected before any mutation.
        with self.assertRaises(ConflictError):
            service.select_worked_example(sid, {
                "message_sha256": delivery["preview_sha256"],
                "candidate_id": pct["candidate_id"],
                "question_id": qid8})
        meta = service._load_meta(sid)
        self.assertIsNotNone(meta["feedback_edit"])
        self.assertFalse(
            meta.get("worked_example_selection_history"))
        self.assertIsNone(
            meta["checkpoints"]["end"].get("worked_example_job"))

        # A saved structured edit survives the override; only the
        # replaced example card is refreshed inside it.
        service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "sections": {"assessment_summary": "Edited summary text.",
                         "observed_strengths": "Edited strengths.",
                         "review_focus": "Edited focus.",
                         "next_steps": "Edited next steps."},
            "reviewer_label": "editor"})
        delivery = service.teacher_session(sid)[
            "feedback_delivery"]
        edited_hash = delivery["preview_sha256"]
        res = service.select_worked_example(sid, {
            "message_sha256": edited_hash,
            "candidate_id": pct["candidate_id"],
            "question_id": qid8})
        meta = service._load_meta(sid)
        edit = meta["feedback_edit"]
        self.assertIsNotNone(edit)
        edited_sections = {s["kind"]: s["text"]
                           for s in edit["preview"]["sections"]}
        self.assertEqual(edited_sections["assessment_summary"],
                         "Edited summary text.")
        edited_card = next(
            c for c in edit["preview"]["review_examples"]
            if c["candidate_id"] == pct["candidate_id"])
        self.assertEqual(edited_card["question_id"], qid8)
        other_cards = [c for c in edit["preview"]["review_examples"]
                       if c["candidate_id"] != pct["candidate_id"]]
        stored = service._load_meta(sid)["checkpoints"]["end"][
            "student_review"]["review_examples"]
        for card in other_cards:
            match = next(c for c in stored
                         if c["candidate_id"] == card["candidate_id"])
            self.assertEqual(card["question_id"],
                             match["question_id"])
        # The scoped job is synchronous application-only in rules
        # mode: no provider call was made or implied.
        job = meta["checkpoints"]["end"]["worked_example_job"]
        self.assertEqual(job["status"], "ready")
        self.assertEqual(job["source"], "application")
        self.assertEqual(job["question_id"], qid8)
        self.assertIsNotNone(
            meta["worked_example_selection_history"][0]
            ["previous_feedback_edit"])
        # Old digests rejected everywhere; new digest works.
        with self.assertRaises(ConflictError):
            service.save_feedback_edit(sid, {
                "message_sha256": old_hash,
                "document_text": "Another rewrite."})
        with self.assertRaises(ConflictError):
            service.release_feedback(
                sid, {"message_sha256": old_hash})
        released = service.release_feedback(
            sid, {"message_sha256": res["preview_sha256"]})
        self.assertTrue(released["released"])
        # No KT data in the released payload.
        raw = json.dumps(service.teacher_session(sid)
                         ["feedback_delivery"]["released_feedback"])
        for forbidden in ("selection_provenance",
                          "p_correct_before_answer",
                          "recommended_question_id"):
            self.assertNotIn(forbidden, raw)
        # Override is rejected after release.
        with self.assertRaises(ConflictError):
            service.select_worked_example(sid, {
                "message_sha256": res["preview_sha256"],
                "candidate_id": pct["candidate_id"],
                "question_id": self.bank["questions"][0]
                ["question_id"]})

    def test_invalid_override_bodies_and_ids_do_not_mutate(self):
        service = self._service(diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        before = service._load_meta(sid)
        digest = service.teacher_session(sid)[
            "feedback_delivery"]["preview_sha256"]
        pct = next(
            c for c in service.teacher_session(sid)
            ["feedback_delivery"]["worked_example_choices"]
            if c["subtopic_name"] == "Percentage of an amount")
        bad_bodies = [
            {"candidate_id": pct["candidate_id"],
             "question_id": self.bank["questions"][8]["question_id"]},
            {"message_sha256": digest,
             "candidate_id": pct["candidate_id"],
             "question_id": self.bank["questions"][8]["question_id"],
             "probability": 0.5},
            {"message_sha256": "0" * 64,
             "candidate_id": pct["candidate_id"],
             "question_id": self.bank["questions"][8]["question_id"]},
            {"message_sha256": digest,
             "candidate_id": "bogus",
             "question_id": self.bank["questions"][8]["question_id"]},
            {"message_sha256": digest,
             "candidate_id": pct["candidate_id"],
             "question_id": self.bank["questions"][12]["question_id"]},
            {"message_sha256": digest,
             "candidate_id": pct["candidate_id"],
             "question_id": self.bank["questions"][1]["question_id"]}]
        for body in bad_bodies:
            with self.assertRaises((ValueError, ConflictError),
                                   msg=body):
                service.select_worked_example(sid, body)
        self.assertEqual(service._load_meta(sid), before)

    def test_legacy_unfrozen_session_keeps_earliest(self):
        gate = threading.Event()
        service = self._service(
            diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        meta = service._load_meta(sid)
        # Simulate a pre-policy record: remove marker and snapshot.
        del meta["worked_example_selection_policy"]
        del meta["checkpoints"]["end"][
            worked_example_selection.CONTEXT_KEY]
        service._save_meta(meta)
        baseline = service._multi_baseline_review(
            meta, meta["checkpoints"]["end"]["evidence"],
            "test", rows=grounded_rows(self.bank, SELECTION_WRONG))
        card = next(c for c in baseline["review_examples"]
                    if c["subtopic_id"] == "percentage_amount")
        self.assertEqual(card["question_id"],
                         self.bank["questions"][0]["question_id"])
        self.assertNotIn(worked_example_selection.CONTEXT_KEY,
                         meta["checkpoints"]["end"])
        with self.assertRaises(ValueError):
            service.select_worked_example(sid, {
                "message_sha256": "0" * 64,
                "candidate_id": "x", "question_id": "y"})

    def test_override_and_snapshot_survive_service_restart(self):
        root = Path(self.tmp.name)
        service = self._service(diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        delivery = service.teacher_session(sid)["feedback_delivery"]
        pct = next(c for c in delivery["worked_example_choices"]
                   if c["subtopic_name"] == "Percentage of an amount")
        qid8 = self.bank["questions"][8]["question_id"]
        res = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": qid8})
        before = service._load_meta(sid)
        service.close(wait=True)
        service = DemoService(
            root=root, bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=self._diagnostics())
        self.addCleanup(service.close, wait=True)
        after = service._load_meta(sid)
        self.assertEqual(
            after["worked_example_selection_history"],
            before["worked_example_selection_history"])
        self.assertEqual(
            after["checkpoints"]["end"]
            [worked_example_selection.CONTEXT_KEY],
            before["checkpoints"]["end"]
            [worked_example_selection.CONTEXT_KEY])
        self.assertEqual(
            after["checkpoints"]["end"]["student_review"],
            before["checkpoints"]["end"]["student_review"])
        reopened = service.teacher_session(sid)
        self.assertEqual(
            reopened["feedback_delivery"]["preview_sha256"],
            res["preview_sha256"])
        self.assertEqual(
            reopened["worked_example_selection_history"],
            before["worked_example_selection_history"])

    def test_broken_diagnostics_falls_back_and_override_works(self):
        from tests.test_demo_service import BrokenDiagnostics
        service = self._service(diagnostics=BrokenDiagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        meta = service._load_meta(sid)
        end = meta["checkpoints"]["end"]
        snapshot = end[worked_example_selection.CONTEXT_KEY]
        self.assertIsNone(snapshot["probabilities"])
        self.assertEqual(snapshot["score_source"],
                         "unavailable_or_incompatible_trace")
        card = next(
            c for c in end["student_review"]["review_examples"]
            if c["subtopic_id"] == "percentage_amount")
        self.assertEqual(card["question_id"],
                         self.bank["questions"][0]["question_id"])
        self.assertEqual(card["selection_provenance"]["source"],
                         "earliest_fallback")
        # A teacher override still works under the fallback trace.
        delivery = service.teacher_session(sid)["feedback_delivery"]
        pct = next(c for c in delivery["worked_example_choices"]
                   if c["subtopic_name"] == "Percentage of an amount")
        self.assertEqual(pct["source"], "earliest_fallback")
        qid8 = self.bank["questions"][8]["question_id"]
        res = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": qid8})
        self.assertTrue(res["saved"])
        card = next(
            c for c in service._load_meta(sid)["checkpoints"]["end"]
            ["student_review"]["review_examples"]
            if c["subtopic_id"] == "percentage_amount")
        self.assertEqual(card["question_id"], qid8)
        self.assertEqual(card["selection_provenance"]["source"],
                         "teacher_override")

    def _pct_choice(self, service, sid):
        delivery = service.teacher_session(sid)["feedback_delivery"]
        choice = next(
            c for c in delivery["worked_example_choices"]
            if c["subtopic_name"] == "Percentage of an amount")
        return choice, delivery

    @staticmethod
    def _full_generator(calls=None):
        class FakeGenerator:
            supports_full_feedback = True
            execution = None

            def generate(self, payload):
                if calls is not None:
                    calls.append(payload)
                return {
                    "candidate_id": payload["selected_candidate"]
                    ["candidate_id"],
                    "sections": dict(VALID_SECTIONS),
                    "worked_reviews": fixture_reviews(payload)}
        return FakeGenerator()

    def _wait_scoped(self, service, sid, statuses, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = (service._load_meta(sid)["checkpoints"]["end"]
                   .get("worked_example_job") or {})
            if job.get("status") in statuses:
                return job
            time.sleep(0.05)
        self.fail("worked_example_job did not finish")

    def test_hosted_scoped_job_updates_only_target_card(self):
        for variant in ("question_first", "worked_first"):
            with self.subTest(variant=variant):
                self._hosted_scoped_target_card(variant)

    def _hosted_scoped_target_card(self, variant):
        gate = threading.Event()
        scoped_calls = []
        full_calls = []

        class Scoped:
            execution = None
            last_rejected_draft = None

            def generate(self, card):
                scoped_calls.append(copy.deepcopy(card))
                gate.wait(10)
                return {"question_id": card["question_id"],
                        "steps": ["Provider step one.",
                                  "Provider step two."],
                        "reasoning_question": "Provider reasoning?",
                        "socratic_prompt":
                            f"Scoped plan for {card['question_id']}?",
                        "progressive_hints": [
                            f"Scoped hint one for "
                            f"{card['question_id']}.",
                            f"Scoped hint two for "
                            f"{card['question_id']}."]}

        service = self._service(
            provider_mode="hosted",
            generator=self._full_generator(full_calls),
            worked_example_generator=Scoped(),
            diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG,
                                       variant=variant)
        self._wait(service, sid)
        self.assertEqual(len(full_calls), 1)
        pct, delivery = self._pct_choice(service, sid)
        before = copy.deepcopy(
            service._load_meta(sid)["checkpoints"]["end"])
        before_review = before["student_review"]
        qid8 = self.bank["questions"][8]["question_id"]
        res = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": qid8})
        self.assertTrue(res["saved"])
        self.assertEqual(res["worked_example_job"]["status"],
                         "pending")
        self.assertEqual(res["worked_example_job"]["question_id"],
                         qid8)
        # The checked replacement card is already stored; only the
        # scoped provider call is outstanding.
        meta = service._load_meta(sid)
        review = meta["checkpoints"]["end"]["student_review"]
        for key in review:
            if key not in ("review_examples", "trace"):
                self.assertEqual(review[key], before_review[key])
        for key in review["trace"]:
            if key not in ("worked_example_override",
                           "worked_review_sources"):
                self.assertEqual(review["trace"][key],
                                 before_review["trace"][key])
        for i, card in enumerate(review["review_examples"]):
            other = before_review["review_examples"][i]
            if card["candidate_id"] == pct["candidate_id"]:
                self.assertEqual(card["question_id"], qid8)
                self.assertEqual(card["explanation_source"],
                                 "application")
                self.assertNotEqual(card["question_id"],
                                    other["question_id"])
            else:
                self.assertEqual(card, other)
        self.assertEqual(meta["checkpoints"]["end"]
                         ["practice_contexts"],
                         before["practice_contexts"])
        # While pending, the replacement card shows only its own
        # application scaffold — never the previous question's
        # provider prompt or hints.
        old_qid = next(
            c["question_id"] for c in before_review["review_examples"]
            if c["candidate_id"] == pct["candidate_id"])
        pending_preview = service.teacher_session(
            sid)["feedback_delivery"]["preview"]
        pending_card = next(
            c for c in pending_preview["review_examples"]
            if c["candidate_id"] == pct["candidate_id"])
        if variant == "question_first":
            self.assertEqual(
                pending_card["scaffold_source"],
                "application_authored_method_prompt_v1")
            self.assertNotIn(old_qid, pending_card["socratic_prompt"])
            for hint in pending_card["progressive_hints"]:
                self.assertNotIn("Scoped hint", hint)
                self.assertNotIn(old_qid, hint)
        # While pending, release/edit/re-selection all conflict.
        pending_hash = res["preview_sha256"]
        for fn in (
                lambda: service.release_feedback(sid, {
                    "message_sha256": pending_hash}),
                lambda: service.save_feedback_edit(sid, {
                    "message_sha256": pending_hash,
                    "sections": {"assessment_summary": "A.",
                                 "observed_strengths": "B.",
                                 "review_focus": "C.",
                                 "next_steps": "D."}}),
                lambda: service.select_worked_example(sid, {
                    "message_sha256": pending_hash,
                    "candidate_id": pct["candidate_id"],
                    "question_id": self.bank["questions"][0]
                    ["question_id"]})):
            with self.assertRaises(ConflictError):
                fn()
        gate.set()
        job = self._wait_scoped(service, sid, {"ready"})
        self.assertEqual(job["candidate_id"], pct["candidate_id"])
        self.assertEqual(len(scoped_calls), 1)
        self.assertEqual(len(full_calls), 1)
        sent = scoped_calls[0]
        self.assertEqual(sent["question_id"], qid8)
        self.assertEqual(sent["subtopic_id"], "percentage_amount")
        self.assertEqual(sent["mathematical_task"],
                         MATHEMATICAL_TASKS["percentage_amount"])
        for field in ("checked_steps", "checked_reasoning_question",
                      "steps", "explanation", "reasoning_question",
                      "socratic_prompt", "progressive_hints",
                      "scaffold_source"):
            self.assertNotIn(field, sent)
        self.assertNotIn("sections", sent)
        self.assertNotIn("review_examples", sent)
        review = service._load_meta(sid)["checkpoints"]["end"][
            "student_review"]
        card = next(c for c in review["review_examples"]
                    if c["candidate_id"] == pct["candidate_id"])
        self.assertEqual(card["steps"], ["Provider step one.",
                                         "Provider step two."])
        self.assertEqual(card["explanation_source"], "provider")
        self.assertTrue(card["checked_steps"] is not None)
        self.assertEqual(card["socratic_prompt"],
                         f"Scoped plan for {qid8}?")
        self.assertEqual(card["progressive_hints"],
                         [f"Scoped hint one for {qid8}.",
                          f"Scoped hint two for {qid8}."])
        self.assertEqual(card["scaffold_source"],
                         "provider_authored_method_prompt_v1")
        self.assertEqual(card["scaffold_validation"],
                         "shape_only_requires_educator_review")
        self.assertEqual(card["scaffold_input"],
                         "question_and_recorded_answers_v1")
        self.assertEqual(review["trace"]["worked_review_sources"]
                         [qid8], "provider")
        # After success the unaffected review fields, other cards and
        # practice remain byte-identical to the pre-override state.
        for key in review:
            if key not in ("review_examples", "trace"):
                self.assertEqual(review[key], before_review[key])
        for i, other in enumerate(review["review_examples"]):
            if other["candidate_id"] != pct["candidate_id"]:
                self.assertEqual(
                    other,
                    before_review["review_examples"][i])
        self.assertEqual(
            service._load_meta(sid)["checkpoints"]["end"]
            ["practice_contexts"], before["practice_contexts"])
        # The replaced question's old source entry is gone once no
        # current card uses it.
        current_ids = {c["question_id"]
                       for c in review["review_examples"]}
        sources = review["trace"]["worked_review_sources"]
        if old_qid not in current_ids:
            self.assertNotIn(old_qid, sources)
        self.assertEqual(set(sources) - current_ids, set())
        # The teacher-facing preview carries the new scaffold; terminal
        # state releases it verbatim.
        delivery = service.teacher_session(sid)["feedback_delivery"]
        preview_card = next(
            c for c in delivery["preview"]["review_examples"]
            if c["candidate_id"] == pct["candidate_id"])
        if variant == "question_first":
            self.assertEqual(preview_card["socratic_prompt"],
                             f"Scoped plan for {qid8}?")
            self.assertEqual(preview_card["progressive_hints"],
                             [f"Scoped hint one for {qid8}.",
                              f"Scoped hint two for {qid8}."])
            self.assertEqual(preview_card["scaffold_source"],
                             "provider_authored_method_prompt_v1")
        released = service.release_feedback(sid, {
            "message_sha256": delivery["preview_sha256"]})
        self.assertTrue(released["released"])
        out = service._load_meta(sid)["feedback_release"][
            "released_feedback"]
        released_card = next(
            c for c in out["review_examples"]
            if c["candidate_id"] == pct["candidate_id"])
        if variant == "question_first":
            self.assertEqual(released_card["progressive_hints"],
                             preview_card["progressive_hints"])
            self.assertEqual(released_card["socratic_prompt"],
                             preview_card["socratic_prompt"])

    def test_hosted_scoped_failure_falls_back_preserving_review(self):
        class Failing:
            execution = None
            last_rejected_draft = None

            def generate(self, card):
                raise RuntimeError("provider blew up")

        service = self._service(
            provider_mode="hosted",
            generator=self._full_generator(),
            worked_example_generator=Failing(),
            diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        pct, delivery = self._pct_choice(service, sid)
        before = copy.deepcopy(
            service._load_meta(sid)["checkpoints"]["end"])
        qid8 = self.bank["questions"][8]["question_id"]
        res = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": qid8})
        job = self._wait_scoped(service, sid, {"fallback"})
        self.assertEqual(job["reason"], "provider_error")
        meta = service._load_meta(sid)
        review = meta["checkpoints"]["end"]["student_review"]
        card = next(c for c in review["review_examples"]
                    if c["candidate_id"] == pct["candidate_id"])
        self.assertEqual(card["question_id"], qid8)
        self.assertEqual(card["explanation_source"], "application")
        for i, other in enumerate(review["review_examples"]):
            if other["candidate_id"] != pct["candidate_id"]:
                self.assertEqual(
                    other, before["student_review"]
                    ["review_examples"][i])
        # Terminal fallback frees release/editing again.
        released = service.release_feedback(sid, {
            "message_sha256": res["preview_sha256"]})
        self.assertTrue(released["released"])

    def test_hosted_without_scoped_adapter_falls_back(self):
        service = self._service(
            provider_mode="hosted",
            generator=self._full_generator(),
            diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        pct, delivery = self._pct_choice(service, sid)
        res = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": self.bank["questions"][8]["question_id"]})
        job = res["worked_example_job"]
        self.assertEqual(job["status"], "fallback")
        self.assertEqual(job["reason"], "adapter_unavailable")

    def test_scoped_job_cancelled_blocks_late_overlay(self):
        gate = threading.Event()

        class Scoped:
            execution = None
            last_rejected_draft = None

            def generate(self, card):
                gate.wait(10)
                return {"question_id": card["question_id"],
                        "steps": ["Late provider step."],
                        "reasoning_question": "Late?",
                        "socratic_prompt": "Late prompt?",
                        "progressive_hints": ["Late hint one.",
                                              "Late hint two."]}

        service = self._service(
            provider_mode="hosted",
            generator=self._full_generator(),
            worked_example_generator=Scoped(),
            diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        pct, delivery = self._pct_choice(service, sid)
        qid8 = self.bank["questions"][8]["question_id"]
        res = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": qid8})
        service.cancel_replay_session(sid)
        job = (service._load_meta(sid)["checkpoints"]["end"]
               ["worked_example_job"])
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["reason"], "replay_case_failed")
        gate.set()
        time.sleep(0.3)
        card = next(
            c for c in service._load_meta(sid)["checkpoints"]["end"]
            ["student_review"]["review_examples"]
            if c["candidate_id"] == pct["candidate_id"])
        self.assertNotEqual(card["steps"], ["Late provider step."])
        self.assertEqual(card["explanation_source"], "application")

    def test_pending_scoped_job_recovers_as_fallback(self):
        root = Path(self.tmp.name)
        service = self._service(diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        meta = service._load_meta(sid)
        meta["checkpoints"]["end"]["worked_example_job"] = {
            "job_id": "abc", "status": "pending",
            "candidate_id": "x", "question_id": "y",
            "queued_at": "2026-10-23T00:00:00+00:00",
            "finished_at": None}
        service._save_meta(meta)
        service.close(wait=True)
        service = DemoService(
            root=root, bank=self.bank, taxonomy=self.taxonomy,
            diagnostics=self._diagnostics())
        self.addCleanup(service.close, wait=True)
        job = (service._load_meta(sid)["checkpoints"]["end"]
               ["worked_example_job"])
        self.assertEqual(job["status"], "fallback")
        self.assertEqual(job["reason"], "interrupted_by_restart")
        # The preserved narrative was not wiped by recovery.
        self.assertIsNotNone(
            service._load_meta(sid)["checkpoints"]["end"]
            ["student_review"].get("review_examples"))

    def test_scoped_queue_capacity_falls_back(self):
        service = self._service(
            provider_mode="hosted",
            generator=self._full_generator(),
            worked_example_generator=object(),
            max_pending_jobs=0,
            diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        pct, delivery = self._pct_choice(service, sid)
        res = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": self.bank["questions"][8]["question_id"]})
        job = res["worked_example_job"]
        self.assertEqual(job["status"], "fallback")
        self.assertEqual(job["reason"],
                         "demo_provider_queue_capacity")

    def test_rules_mode_scoped_job_is_application_only(self):
        service = self._service(diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        pct, delivery = self._pct_choice(service, sid)
        res = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": self.bank["questions"][8]["question_id"]})
        job = res["worked_example_job"]
        self.assertEqual(job["status"], "ready")
        self.assertEqual(job["source"], "application")
        # Selecting the same question again stays idempotent and
        # queues nothing.
        delivery = service.teacher_session(sid)["feedback_delivery"]
        again = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": self.bank["questions"][8]["question_id"]})
        self.assertEqual(again["preview_sha256"],
                         res["preview_sha256"])
        meta = service._load_meta(sid)
        self.assertEqual(
            meta["checkpoints"]["end"]["worked_example_job"]
            ["question_id"],
            self.bank["questions"][8]["question_id"])

    def test_scoped_job_preserves_aggregate_provenance(self):
        gate = threading.Event()
        full_execution = {
            "provider": "aitta_full", "origin": "hosted_replay",
            "metadata": {"status": "completed",
                         "model_version": "openai/gpt-oss-120b",
                         "prompt_version": "aitta_feedback_v9"}}

        class Full:
            supports_full_feedback = True
            execution = None

            def generate(self, payload):
                self.execution = full_execution
                return {
                    "candidate_id": payload["selected_candidate"]
                    ["candidate_id"],
                    "sections": dict(VALID_SECTIONS),
                    "worked_reviews": fixture_reviews(payload)}

        class Scoped:
            execution = None
            last_rejected_draft = None

            def generate(self, card):
                gate.wait(10)
                return {"question_id": card["question_id"],
                        "steps": ["Provider step."],
                        "reasoning_question": "Why?",
                        "socratic_prompt": "Provider prompt?",
                        "progressive_hints": ["Provider hint one.",
                                              "Provider hint two."]}

        service = self._service(
            provider_mode="hosted", generator=Full(),
            worked_example_generator=Scoped(),
            diagnostics=self._diagnostics())
        self.addCleanup(gate.set)
        created = service.create_session(feedback_variant="worked_first")
        sid, token = created["session_id"], created["student_token"]
        for row in grounded_rows(self.bank, SELECTION_WRONG):
            service.submit_response(sid, token, row)
        self._wait(service, sid)
        before_job = copy.deepcopy(
            service._load_meta(sid)["provider_job"])
        self.assertEqual(
            before_job["executions"]["student"]["generator"],
            full_execution)
        before_review = copy.deepcopy(
            service._load_meta(sid)["checkpoints"]["end"]["student_review"])
        self.assertEqual(
            before_review["trace"]["guardrails_version"],
            "provider_full_narrative_shape_checked_v2")
        self.assertEqual(
            set(before_review["message"]["section_sources"].values()),
            {"provider"})

        def assert_narrative_preserved():
            review = service._load_meta(sid)["checkpoints"]["end"][
                "student_review"]
            self.assertEqual(review["message"], before_review["message"])
            self.assertEqual(review.get("provider_context"),
                             before_review.get("provider_context"))
            for key in ("phrasing_source", "guardrails_version",
                        "section_sources", "generation_prompt_version",
                        "validation", "generation_context_version"):
                self.assertEqual(review["trace"].get(key),
                                 before_review["trace"].get(key), key)

        pct, delivery = self._pct_choice(service, sid)
        res = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": self.bank["questions"][8]["question_id"]})
        self.assertEqual(res["worked_example_job"]["status"],
                         "pending")
        self.assertEqual(service._load_meta(sid)["provider_job"],
                         before_job)
        assert_narrative_preserved()
        gate.set()
        self._wait_scoped(service, sid, {"ready"})
        self.assertEqual(service._load_meta(sid)["provider_job"],
                         before_job)
        self.assertEqual(
            service._load_meta(sid)["checkpoints"]["end"]
            ["provider_job"], before_job)
        assert_narrative_preserved()

    def test_hosted_scoped_success_keeps_saved_structured_edit(self):
        gate = threading.Event()

        class Scoped:
            execution = None
            last_rejected_draft = None

            def generate(self, card):
                gate.wait(10)
                return {"question_id": card["question_id"],
                        "steps": ["Scoped provider step."],
                        "reasoning_question": "Scoped check?",
                        "socratic_prompt": "Scoped prompt?",
                        "progressive_hints": ["Scoped hint one.",
                                              "Scoped hint two."]}

        service = self._service(
            provider_mode="hosted",
            generator=self._full_generator(),
            worked_example_generator=Scoped(),
            diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        pct, delivery = self._pct_choice(service, sid)
        service.save_feedback_edit(sid, {
            "message_sha256": delivery["preview_sha256"],
            "sections": {"assessment_summary": "Saved summary edit.",
                         "observed_strengths": "Saved strengths edit.",
                         "review_focus": "Saved focus edit.",
                         "next_steps": "Saved next steps edit."},
            "reviewer_label": "editor"})
        delivery = service.teacher_session(sid)["feedback_delivery"]
        before_edit = copy.deepcopy(
            service._load_meta(sid)["feedback_edit"])
        qid8 = self.bank["questions"][8]["question_id"]
        res = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": qid8})
        self.assertEqual(res["worked_example_job"]["status"],
                         "pending")
        gate.set()
        self._wait_scoped(service, sid, {"ready"})
        edit = service._load_meta(sid)["feedback_edit"]
        self.assertIsNotNone(edit)
        sections = {s["kind"]: s["text"]
                    for s in edit["preview"]["sections"]}
        self.assertEqual(sections["assessment_summary"],
                         "Saved summary edit.")
        cards = {c["candidate_id"]: c
                 for c in edit["preview"]["review_examples"]}
        self.assertEqual(cards[pct["candidate_id"]]["question_id"],
                         qid8)
        self.assertEqual(
            cards[pct["candidate_id"]]["steps"],
            ["Scoped provider step."])
        old_cards = {c["candidate_id"]: c
                     for c in before_edit["preview"]
                     ["review_examples"]}
        for cid, card in cards.items():
            if cid != pct["candidate_id"]:
                self.assertEqual(card, old_cards[cid])

    def test_scoped_failure_records_retry_execution(self):
        exhausted = {
            "provider": "aitta_worked_example",
            "origin": "hosted_replay",
            "metadata": {
                "status": "failed",
                "role": "selected_worked_example",
                "transport": {"stage": "open_failed",
                              "failure_kind": "timeout"}},
            "automatic_retry": {"attempts": 3, "max_attempts": 3,
                                "stop_reason": "attempts_exhausted"}}

        class Failing:
            execution = None
            last_rejected_draft = None

            def generate(self, card):
                self.execution = exhausted
                raise RuntimeError(
                    "raw provider timeout details")

        service = self._service(
            provider_mode="hosted",
            generator=self._full_generator(),
            worked_example_generator=Failing(),
            diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        pct, delivery = self._pct_choice(service, sid)
        service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": pct["candidate_id"],
            "question_id": self.bank["questions"][8]["question_id"]})
        job = self._wait_scoped(service, sid, {"fallback"})
        self.assertEqual(job["reason"], "provider_error")
        self.assertEqual(
            job["execution"]["automatic_retry"]["stop_reason"],
            "attempts_exhausted")
        self.assertEqual(
            job["execution"]["metadata"]["transport"]["failure_kind"],
            "timeout")
        self.assertNotIn("raw provider timeout details",
                         json.dumps(job))

    def _unchecked_scoped_service(self, scoped, subdir,
                                  provider_mode="hosted"):
        # Mutating question text makes its local checked solution
        # unavailable, so the scoped job must generate from the
        # question and task records alone.
        bank = make_grounded_bank()
        bank["questions"][0]["text"] += (
            " <img src=x onerror=alert(1)>")
        bank["questions"][0]["content_text"] = (
            bank["questions"][0]["text"] + " [OPTIONS] "
            + " | ".join(bank["questions"][0]["options"]))
        service = DemoService(
            root=Path(self.tmp.name) / subdir, bank=bank,
            taxonomy=make_grounded_taxonomy(bank),
            provider_mode=provider_mode,
            generator=self._full_generator(),
            worked_example_generator=scoped,
            diagnostics=LiveDiagnostics(
                model_loader=lambda: FakeKT(bank)))
        self.addCleanup(service.close, wait=True)
        created = service.create_session()
        sid, token = created["session_id"], created["student_token"]
        for row in grounded_rows(bank, SELECTION_WRONG):
            service.submit_response(sid, token, row)
        self._wait(service, sid)
        delivery = service.teacher_session(sid)["feedback_delivery"]
        choice = next(
            c for c in delivery["worked_example_choices"]
            if any(not cand.get("checked_solution_available")
                   for cand in c["candidates"]))
        target = next(
            c for c in choice["candidates"]
            if c["question_id"] != choice["selected_question_id"]
            and not c.get("checked_solution_available"))
        res = service.select_worked_example(sid, {
            "message_sha256": delivery["preview_sha256"],
            "candidate_id": choice["candidate_id"],
            "question_id": target["question_id"]})
        return service, sid, choice, target, res

    def test_scoped_no_checked_solution_generates_from_question_and_task(
            self):
        calls = []

        class Scoped:
            execution = None
            last_rejected_draft = None

            def generate(self, card):
                calls.append(copy.deepcopy(card))
                return {"question_id": card["question_id"],
                        "steps": ["Teacher checking is needed for this "
                                  "recorded answer."],
                        "reasoning_question":
                            "What should the teacher verify here?",
                        "socratic_prompt":
                            "How would you plan this one?",
                        "progressive_hints": ["Unclear scaffold hint.",
                                              "Unclear method cue."]}

        service, sid, choice, target, res = (
            self._unchecked_scoped_service(Scoped(), "nocheck"))
        job = self._wait_scoped(service, sid, {"ready"})
        self.assertEqual(job["candidate_id"], choice["candidate_id"])
        self.assertEqual(len(calls), 1)
        sent = calls[0]
        self.assertEqual(sent["question_id"], target["question_id"])
        self.assertEqual(sent["subtopic_id"], "percentage_amount")
        self.assertEqual(sent["mathematical_task"],
                         MATHEMATICAL_TASKS["percentage_amount"])
        for field in ("checked_steps", "checked_reasoning_question",
                      "steps", "explanation", "reasoning_question",
                      "socratic_prompt", "progressive_hints",
                      "scaffold_source"):
            self.assertNotIn(field, sent)
        meta = service._load_meta(sid)
        card = next(
            c for c in meta["checkpoints"]["end"]["student_review"]
            ["review_examples"]
            if c["candidate_id"] == choice["candidate_id"])
        self.assertEqual(card["status"], "ready")
        self.assertEqual(card["explanation_source"], "provider")
        self.assertIsNone(card["solution_version"])
        self.assertEqual(card["explanation_validation"],
                         "shape_only_requires_educator_review")
        # Provider scaffolds are not gated on the local solver.
        self.assertEqual(card["socratic_prompt"],
                         "How would you plan this one?")
        self.assertEqual(card["progressive_hints"],
                         ["Unclear scaffold hint.",
                          "Unclear method cue."])
        self.assertEqual(card["scaffold_source"],
                         "provider_authored_method_prompt_v1")

    def test_scoped_no_checked_solution_failure_keeps_unavailable(self):
        class Failing:
            execution = None
            last_rejected_draft = None

            def generate(self, card):
                raise RuntimeError("provider blew up")

        service, sid, choice, target, res = (
            self._unchecked_scoped_service(Failing(), "nocheck_fail"))
        job = self._wait_scoped(service, sid, {"fallback"})
        self.assertEqual(job["reason"], "provider_error")
        meta = service._load_meta(sid)
        card = next(
            c for c in meta["checkpoints"]["end"]["student_review"]
            ["review_examples"]
            if c["candidate_id"] == choice["candidate_id"])
        self.assertEqual(card["status"], "unavailable")
        self.assertIsNone(card["solution_version"])
        self.assertNotIn("Teacher checking", json.dumps(card))

    def test_rules_mode_no_checked_solution_stays_unavailable(self):
        class Scoped:
            execution = None
            last_rejected_draft = None

            def generate(self, card):
                raise AssertionError("must not be called")

        service, sid, choice, target, res = (
            self._unchecked_scoped_service(
                Scoped(), "nocheck_rules", provider_mode="rules"))
        job = res["worked_example_job"]
        self.assertEqual(job["status"], "unavailable")
        self.assertEqual(job["reason"], "no_checked_solution")

    def test_degraded_record_resolves_recorded_bundle(self):
        service = self._service(diagnostics=self._diagnostics())
        sid, token, _ = self._complete(service, SELECTION_WRONG)
        self._wait(service, sid)
        meta = service._load_meta(sid)
        end = meta["checkpoints"]["end"]
        original = copy.deepcopy(end["student_review"])
        # Lose the recorded selection (queue/restart fallback): the
        # rebuild must resolve the stored candidate id and keep the
        # exact same topics instead of reselecting.
        del end["multi_selection"]
        review = service._multi_baseline_review(
            meta, end["evidence"], "queue_restart",
            rows=grounded_rows(self.bank, SELECTION_WRONG))
        self.assertEqual(review["selected_candidates"],
                         original["selected_candidates"])
        self.assertEqual(review["review_examples"],
                         original["review_examples"])
        card = next(c for c in review["review_examples"]
                    if c["subtopic_id"] == "percentage_amount")
        self.assertEqual(card["question_id"],
                         self.bank["questions"][4]["question_id"])
        self.assertEqual(card["selection_provenance"]["source"],
                         "kt_recommendation")


if __name__ == "__main__":
    unittest.main()
