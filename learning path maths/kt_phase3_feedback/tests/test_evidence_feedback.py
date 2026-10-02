"""Software checks for the observed-evidence feedback draft path.

Pure plumbing tests: scoring/aggregation contracts, payload boundaries,
callback fallback behaviour, and fixed wording. These are not an
educator grader and make no educational-quality claims.
"""
import copy
import json
import unittest

import evidence_feedback
from evidence_feedback import (
    EVIDENCE_SCHEMA, build_evidence, run_feedback, selection_payload,
    validate_evidence)
from evidence_feedback_policy import (
    GENERATION_SCHEMA, MIDPOINT_TEXT, POLICY_VERSION, REVIEW_STATUS,
    SELECTION_SCHEMA)
from tests.helpers import make_bank, make_taxonomy, responses


def fixture():
    bank = make_bank()
    return bank, make_taxonomy(bank)


def submissions(bank, wrong=(), count=40, shift=1):
    wrong = set(wrong)
    rows = []
    for i, question in enumerate(bank["questions"][:count]):
        index = (question["answer_index"] if i not in wrong
                 else (question["answer_index"] + shift)
                 % len(question["options"]))
        rows.append({"question_id": question["question_id"],
                     "selected_index": index})
    return rows


def positions(bank, skill_id, half=None):
    return [i for i, q in enumerate(bank["questions"])
            if q["skill_id"] == skill_id
            and (half is None or i // 20 == half)]


def split_taxonomy(bank, first_ids):
    """Split skill sA into two subtopics; first_ids names the first."""
    taxonomy = make_taxonomy(bank)
    topic = taxonomy["topics"][0]
    assert topic["skill_id"] == "sA"
    qids = [q["question_id"] for q in bank["questions"]
            if q["skill_id"] == "sA"]
    first = set(first_ids)
    topic["subtopics"] = [
        {"id": "sub_sA_first", "name": "Subtopic sA first",
         "question_ids": [q for q in qids if q in first]},
        {"id": "sub_sA_second", "name": "Subtopic sA second",
         "question_ids": [q for q in qids if q not in first]}]
    return taxonomy


def end_evidence(wrong=(), bank=None, taxonomy=None, shift=1):
    bank = bank or make_bank()
    taxonomy = taxonomy or make_taxonomy(bank)
    return build_evidence(bank, taxonomy,
                          submissions(bank, wrong, shift=shift))


class RecordingSelector:
    def __init__(self, reply=None, mutate=None):
        self.reply = reply
        self.mutate = mutate
        self.payloads = []

    def select(self, payload):
        self.payloads.append(payload)
        if self.mutate is not None:
            self.mutate(payload)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class RecordingGenerator:
    def __init__(self, reply=None, mutate=None):
        self.reply = reply
        self.mutate = mutate
        self.payloads = []

    def generate(self, payload):
        self.payloads.append(payload)
        if self.mutate is not None:
            self.mutate(payload)
        if isinstance(self.reply, Exception):
            raise self.reply
        return (self.reply(payload) if callable(self.reply)
                else self.reply)


def valid_opening(payload):
    return {"candidate_id": payload["selected_candidate"]
            ["candidate_id"],
            "opening": ("Keep going when you feel ready."
                        if payload["checkpoint"] == "midpoint"
                        else "Thank you for completing this "
                             "assessment.")}


def valid_teacher_opening(payload):
    return {"candidate_id": payload["selected_candidate"]
            ["candidate_id"],
            "opening": "The assessment record is ready for review."}


def sections(review):
    return {s["kind"] for s in review["message"]["sections"]}


class BuildEvidenceTests(unittest.TestCase):
    def test_end_evidence_shape_and_counts(self):
        evidence = end_evidence()
        self.assertEqual(evidence["schema"], EVIDENCE_SCHEMA)
        self.assertEqual(evidence["data_origin"], "synthetic")
        self.assertEqual(evidence["checkpoint"], "end")
        for key in ("bank_sha256", "taxonomy_sha256"):
            self.assertRegex(evidence[key], r"[a-f0-9]{64}\Z")
        self.assertEqual(evidence["total"],
                         {"correct": 40, "incorrect": 0, "out_of": 40})
        self.assertEqual(
            evidence["halves"],
            [{"correct": 20, "incorrect": 0, "out_of": 20}] * 2)
        self.assertEqual(len(evidence["skills"]), 4)
        for skill in evidence["skills"]:
            self.assertEqual(
                {k: skill[k] for k in ("correct", "incorrect",
                                       "out_of")},
                {"correct": 10, "incorrect": 0, "out_of": 10})
            self.assertEqual(
                skill["halves"],
                [{"correct": 5, "incorrect": 0, "out_of": 5}] * 2)
        self.assertEqual(len(evidence["subtopics"]), 4)
        for sub in evidence["subtopics"]:
            self.assertEqual(sub["subtopic_id"],
                             "sub_" + sub["skill_id"])

    def test_midpoint_evidence_counts(self):
        bank, taxonomy = fixture()
        evidence = build_evidence(
            bank, taxonomy, submissions(bank, {0, 1}, count=20))
        self.assertEqual(evidence["checkpoint"], "midpoint")
        self.assertEqual(evidence["total"],
                         {"correct": 18, "incorrect": 2, "out_of": 20})
        self.assertEqual(evidence["halves"][1],
                         {"correct": 0, "incorrect": 0, "out_of": 0})
        for skill in evidence["skills"]:
            self.assertEqual(skill["out_of"], 5)

    def test_wrong_option_identity_does_not_change_evidence(self):
        # The same correctness pattern via different wrong options must
        # produce identical evidence and identical feedback.
        bank, taxonomy = fixture()
        first = build_evidence(bank, taxonomy,
                               submissions(bank, {3}, shift=1))
        second = build_evidence(bank, taxonomy,
                                submissions(bank, {3}, shift=2))
        self.assertEqual(first, second)
        self.assertEqual(
            run_feedback(first, "student", "end"),
            run_feedback(second, "student", "end"))

    def test_malformed_unordered_extra_submissions_rejected(self):
        bank, taxonomy = fixture()
        base = submissions(bank)
        cases = []
        extra = copy.deepcopy(base)
        extra[0]["note"] = "x"
        cases.append(extra)
        missing = copy.deepcopy(base)
        del missing[0]["selected_index"]
        cases.append(missing)
        unordered = copy.deepcopy(base)
        unordered[0], unordered[1] = unordered[1], unordered[0]
        cases.append(unordered)
        for bad_index in (-1, 3, True, 1.5, "0"):
            bad = copy.deepcopy(base)
            bad[2]["selected_index"] = bad_index
            cases.append(bad)
        cases.append(base[:39])
        cases.append("not a list")
        cases.append(None)
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    build_evidence(bank, taxonomy, case)

    def test_wrong_bank_or_taxonomy_binding_rejected(self):
        bank, taxonomy = fixture()
        valid_submissions = submissions(bank)
        other_bank = make_bank()
        other_bank["questions"][0]["text"] = "A different prompt?"
        other_taxonomy = make_taxonomy(other_bank)
        with self.assertRaises(ValueError):
            build_evidence(bank, other_taxonomy, valid_submissions)
        broken = make_taxonomy(bank)
        broken["topics"][1]["subtopics"][0]["question_ids"].pop()
        with self.assertRaises(ValueError):
            build_evidence(bank, broken, valid_submissions)
        renamed = make_taxonomy(bank)
        renamed["topics"][0]["skill_name"] = "Renamed"
        with self.assertRaises(ValueError):
            build_evidence(bank, renamed, valid_submissions)


class EvidenceValidationTests(unittest.TestCase):
    def assert_rejected(self, mutate):
        evidence = end_evidence({0})
        mutate(evidence)
        with self.assertRaises(ValueError):
            validate_evidence(evidence)

    def test_rejects_inconsistent_sums(self):
        self.assert_rejected(lambda e: e["total"].update(correct=38))
        self.assert_rejected(
            lambda e: e["skills"][0].update(correct=8))
        self.assert_rejected(
            lambda e: e["halves"][0].update(correct=18))

    def test_rejects_bool_counts(self):
        self.assert_rejected(
            lambda e: e["total"].update(correct=True))
        self.assert_rejected(
            lambda e: e["skills"][0].update(incorrect=False))
        self.assert_rejected(
            lambda e: e["subtopics"][0]["halves"][0].update(
                out_of=True))

    def test_rejects_bad_fingerprints_and_checkpoint(self):
        self.assert_rejected(lambda e: e.update(bank_sha256="zz"))
        self.assert_rejected(
            lambda e: e.update(taxonomy_sha256="A" * 64))
        self.assert_rejected(lambda e: e.update(checkpoint="midpoint"))
        self.assert_rejected(lambda e: e.update(schema="other"))

    def test_rejects_widened_or_missing_fields(self):
        self.assert_rejected(lambda e: e.update(session_id="s"))
        self.assert_rejected(lambda e: e.pop("subtopics"))
        self.assert_rejected(
            lambda e: e["skills"][0].update(question_id="q"))
        self.assert_rejected(lambda e: e["subtopics"].pop())

    def test_selection_payload_contract(self):
        payload = selection_payload(end_evidence({0}), "student", "end")
        self.assertEqual(set(payload), {"schema", "audience",
                                        "checkpoint", "evidence",
                                        "candidates"})
        self.assertEqual(payload["schema"], SELECTION_SCHEMA)
        fresh = evidence_feedback.validate_selection_payload(payload)
        self.assertEqual(fresh, payload)
        complete_evidence = end_evidence()
        with self.assertRaises(ValueError):
            selection_payload(complete_evidence, "teacher", "midpoint")


class ReviewPolicyTests(unittest.TestCase):
    def test_all_correct_offers_optional_consolidation(self):
        review = run_feedback(end_evidence(), "student", "end")
        self.assertEqual(
            [c["candidate_id"] for c in review["candidates"]],
            ["optional_consolidation"])
        self.assertEqual(review["selected_candidate_id"],
                         "optional_consolidation")
        self.assertEqual(review["baseline_candidate_id"],
                         "optional_consolidation")
        self.assertIn("optional_review", sections(review))
        self.assertNotIn("review_focus", sections(review))
        self.assertNotIn("support", sections(review))
        self.assertIn("observed_highlight", sections(review))
        self.assertTrue(review["requires_human_review"])

    def test_all_wrong_supported_without_fabricated_highlight(self):
        review = run_feedback(end_evidence(set(range(40))),
                              "student", "end")
        self.assertEqual(len(review["candidates"]), 4)
        for candidate in review["candidates"]:
            self.assertEqual(candidate["strategy"], "supported_review")
            self.assertTrue(candidate["action"].startswith(
                "Start with a teacher"))
        self.assertIn("support", sections(review))
        self.assertNotIn("observed_highlight", sections(review))
        self.assertNotIn("highest observed",
                         review["message"]["text"].casefold())
        self.assertTrue(review["requires_human_review"])

    def test_same_totals_different_subtopic_errors_focus_differs(self):
        bank = make_bank()
        sA_ids = [q["question_id"] for q in bank["questions"]
                  if q["skill_id"] == "sA"]
        taxonomy = split_taxonomy(bank, sA_ids[:5])
        pos = {q["question_id"]: i
               for i, q in enumerate(bank["questions"])}
        first = build_evidence(bank, taxonomy, submissions(
            bank, {pos[qid] for qid in sA_ids[:5]}))
        second = build_evidence(bank, taxonomy, submissions(
            bank, {pos[qid] for qid in sA_ids[5:]}))
        self.assertEqual(
            [s["correct"] for s in first["skills"]],
            [s["correct"] for s in second["skills"]])
        self.assertEqual(first["total"], second["total"])
        review_first = run_feedback(first, "student", "end")
        review_second = run_feedback(second, "student", "end")
        self.assertEqual(review_first["selected_candidate_id"],
                         "review_sub_sA_first")
        self.assertEqual(review_second["selected_candidate_id"],
                         "review_sub_sA_second")
        self.assertNotEqual(review_first["message"]["text"],
                            review_second["message"]["text"])

    def test_ties_disclosed_without_uniquely_weak_claim(self):
        # Two skills each with two errors: equal count and fraction.
        bank, taxonomy = fixture()
        wrong = set(positions(bank, "sA")[:2]) | set(
            positions(bank, "sB")[:2])
        review = run_feedback(
            build_evidence(bank, taxonomy, submissions(bank, wrong)),
            "student", "end")
        self.assertIn("tie", sections(review))
        self.assertIn("not a uniquely weakest area",
                      review["message"]["text"])
        self.assertEqual(review["selected_candidate_id"],
                         "review_sub_sA")

    def test_one_question_error_carries_sparse_note(self):
        bank = make_bank()
        sA_ids = [q["question_id"] for q in bank["questions"]
                  if q["skill_id"] == "sA"]
        taxonomy = split_taxonomy(bank, {sA_ids[0]})
        pos = {q["question_id"]: i
               for i, q in enumerate(bank["questions"])}
        evidence = build_evidence(
            bank, taxonomy, submissions(bank, {pos[sA_ids[0]]}))
        review = run_feedback(evidence, "student", "end")
        focus = review["candidates"][0]["focus"]
        self.assertEqual(focus["out_of"], 1)
        self.assertIn("limited_evidence", sections(review))
        self.assertIn("Only one question assessed this content",
                      review["message"]["text"])

    def test_reversed_halves_are_counts_not_causal_claims(self):
        bank, taxonomy = fixture()
        first_half_wrong = build_evidence(
            bank, taxonomy,
            submissions(bank, set(positions(bank, "sA", half=0))))
        second_half_wrong = build_evidence(
            bank, taxonomy,
            submissions(bank, set(positions(bank, "sA", half=1))))
        self.assertEqual(
            [h["correct"] for h in first_half_wrong["halves"]],
            [15, 20])
        self.assertEqual(
            [h["correct"] for h in second_half_wrong["halves"]],
            [20, 15])
        review = run_feedback(second_half_wrong, "teacher", "end")
        self.assertIn("half_observations", sections(review))
        text = review["message"]["text"]
        self.assertIn("First half: 20 of 20 correct.", text)
        self.assertIn("Second half: 15 of 20 correct.", text)
        self.assertIn("does not establish learning, improvement, "
                      "decline, or fatigue", text)
        student = run_feedback(second_half_wrong, "student", "end")
        self.assertNotIn("half_observations", sections(student))

    def test_every_package_is_a_human_review_draft(self):
        bank, taxonomy = fixture()
        evidences = [build_evidence(bank, taxonomy, submissions(bank)),
                     build_evidence(bank, taxonomy,
                                    submissions(bank, set(range(40)))),
                     build_evidence(bank, taxonomy,
                                    submissions(bank, {0, 5})),
                     build_evidence(bank, taxonomy,
                                    submissions(bank, count=20))]
        for evidence in evidences:
            for audience, checkpoint in (
                    ("student", evidence["checkpoint"]),
                    ("teacher", evidence["checkpoint"])):
                if audience == "teacher" and checkpoint == "midpoint":
                    continue
                review = run_feedback(evidence, audience, checkpoint)
                self.assertTrue(review["requires_human_review"])
                self.assertEqual(review["status"],
                                 "draft_not_for_learner_delivery")


class MidpointTests(unittest.TestCase):
    def test_midpoint_sanitizes_everything(self):
        bank, taxonomy = fixture()
        evidence = build_evidence(bank, taxonomy,
                                  submissions(bank, count=20))
        selector = RecordingSelector({"candidate_id": "neutral"})
        generator = RecordingGenerator(valid_opening)
        review = run_feedback(evidence, "student", "midpoint",
                              selector, generator)
        self.assertEqual(review["sanitized_evidence"], {})
        self.assertEqual(selector.payloads, [])
        self.assertEqual(len(generator.payloads), 1)
        payload = generator.payloads[0]
        self.assertEqual(set(payload), {"schema", "audience",
                                        "checkpoint",
                                        "selected_candidate"})
        self.assertEqual(payload["schema"], GENERATION_SCHEMA)
        self.assertEqual(payload["selected_candidate"], {
            "candidate_id": "neutral",
            "strategy": "neutral_encouragement",
            "review_status": REVIEW_STATUS})
        blob = json.dumps(payload)
        for marker in ("correct", "out_of", "sub_sA", "Skill sA",
                       "subtopic", "action", "total", "halves"):
            self.assertNotIn(marker, blob)
        self.assertEqual(
            [s["kind"] for s in review["message"]["sections"]],
            ["encouragement"])
        self.assertEqual(review["message"]["text"],
                         "Keep going when you feel ready.")
        self.assertNotRegex(review["message"]["text"], r"\d")
        self.assertEqual(
            review["trace"]["selection_source"],
            "rules_single_candidate")


class SelectorFallbackTests(unittest.TestCase):
    def assert_baseline_fallback(self, selector, reason):
        generator = RecordingGenerator(valid_opening)
        review = run_feedback(end_evidence({0, 1}), "student", "end",
                              selector, generator)
        baseline = review["candidates"][0]["candidate_id"]
        self.assertEqual(review["selected_candidate_id"], baseline)
        self.assertEqual(review["trace"]["fallback_reason"], reason)
        self.assertEqual(review["trace"]["selection_source"], "rules")
        self.assertEqual(generator.payloads, [])
        self.assertEqual(review["message"],
                         review["template_baseline"])
        return review

    def test_selector_arbitrary_id_and_extra_keys_fall_back(self):
        for reply in ({"candidate_id": "not_a_candidate"},
                      {"candidate_id": "review_sub_sA", "extra": 1},
                      {"candidate_id": 7}, {}, None, "review_sub_sA"):
            with self.subTest(reply=reply):
                self.assert_baseline_fallback(
                    RecordingSelector(reply), "invalid_selection")

    def test_selector_exception_falls_back_and_skips_generator(self):
        self.assert_baseline_fallback(
            RecordingSelector(RuntimeError("secret-marker")),
            "selector_error")

    def test_selector_payload_mutation_falls_back(self):
        def mutate(payload):
            payload["candidates"].append(
                {"candidate_id": "evil", "strategy": "x",
                 "review_status": "x", "focus": None, "action": None})
            payload["evidence"]["total"]["correct"] = 0

        self.assert_baseline_fallback(
            RecordingSelector({"candidate_id": "review_sub_sA"},
                              mutate=mutate),
            "mutated_selection_payload")

    def test_valid_alternative_candidate_is_permitted(self):
        evidence = end_evidence({0, 1})
        candidates = evidence and run_feedback(
            evidence, "student", "end")["candidates"]
        target = candidates[1]["candidate_id"]
        review = run_feedback(
            evidence, "student", "end",
            RecordingSelector({"candidate_id": target}))
        self.assertEqual(review["selected_candidate_id"], target)
        self.assertEqual(review["trace"]["selection_source"],
                         "injected_selector")
        self.assertIsNone(review["trace"]["fallback_reason"])
        self.assertFalse(
            review["trace"]["selection_matches_baseline"])


class GeneratorFallbackTests(unittest.TestCase):
    def test_valid_opening_is_used(self):
        review = run_feedback(end_evidence({0}), "student", "end",
                              generator=RecordingGenerator(
                                  valid_opening))
        self.assertEqual(review["trace"]["phrasing_source"],
                         "injected_generator_opening_only")
        self.assertIn("Thank you for completing this assessment.",
                      review["message"]["text"])
        self.assertTrue(review["requires_human_review"])

    def test_invalid_generated_text_falls_back_to_fixed(self):
        bad_openings = [
            "You answered 5 questions.", "Subtopic sA needs work.",
            "This shows mastery.", "Your score was high.",
            "Skill sA was strongest."]
        for opening in bad_openings:
            with self.subTest(opening=opening):
                review = run_feedback(
                    end_evidence({0}), "student", "end",
                    generator=RecordingGenerator(
                        {"candidate_id": "review_sub_sA",
                         "opening": opening}))
                self.assertEqual(review["trace"]["fallback_reason"],
                                 "invalid_generation")
                self.assertEqual(review["message"],
                                 review["template_baseline"])
                self.assertNotIn(opening, json.dumps(review))

    def test_teacher_second_person_opening_rejected(self):
        review = run_feedback(
            end_evidence({0}), "teacher", "end",
            generator=RecordingGenerator(
                {"candidate_id": "review_sub_sA",
                 "opening": "You completed this assessment."}))
        self.assertEqual(review["trace"]["fallback_reason"],
                         "invalid_generation")
        self.assertEqual(review["message"], review["template_baseline"])
        review = run_feedback(
            end_evidence({0}), "teacher", "end",
            generator=RecordingGenerator(valid_teacher_opening))
        self.assertEqual(review["trace"]["phrasing_source"],
                         "injected_generator_opening_only")

    def test_generator_exception_and_bad_reply_fall_back(self):
        review = run_feedback(
            end_evidence({0}), "student", "end",
            generator=RecordingGenerator(RuntimeError("x")))
        self.assertEqual(review["trace"]["fallback_reason"],
                         "generator_error")
        for reply in ({"candidate_id": "neutral", "opening": "Ok."},
                      {"opening": "Ok."},
                      {"candidate_id": "review_sub_sA",
                       "opening": "Ok.", "extra": 1},
                      None, "text"):
            with self.subTest(reply=reply):
                review = run_feedback(
                    end_evidence({0}), "student", "end",
                    generator=RecordingGenerator(reply))
                self.assertEqual(review["trace"]["fallback_reason"],
                                 "invalid_generation")

    def test_generator_payload_mutation_falls_back(self):
        def mutate(payload):
            payload["selected_candidate"]["candidate_id"] = "evil"

        review = run_feedback(
            end_evidence({0}), "student", "end",
            generator=RecordingGenerator(valid_opening,
                                         mutate=mutate))
        self.assertEqual(review["trace"]["fallback_reason"],
                         "invalid_generation")
        self.assertNotIn("evil", json.dumps(review))

    def test_callbacks_cannot_mutate_inputs_or_results(self):
        seen = {}

        def selector_mutate(payload):
            seen["selector"] = copy.deepcopy(payload)
            payload["evidence"]["skills"][0]["correct"] = 0

        def generator_mutate(payload):
            seen["generator"] = copy.deepcopy(payload)
            payload["audience"] = "teacher"

        evidence = end_evidence({0, 1})
        original = copy.deepcopy(evidence)
        review = run_feedback(
            evidence, "student", "end",
            RecordingSelector({"candidate_id": "review_sub_sA"},
                              mutate=selector_mutate),
            RecordingGenerator(valid_opening, mutate=generator_mutate))
        self.assertEqual(evidence, original)
        self.assertEqual(review["trace"]["fallback_reason"],
                         "mutated_selection_payload")
        self.assertEqual(review["audience"], "student")
        review["candidates"][0]["candidate_id"] = "mutated"
        review["sanitized_evidence"]["total"]["correct"] = 0
        again = run_feedback(evidence, "student", "end")
        self.assertEqual(again["candidates"][0]["candidate_id"],
                         "review_sub_sA")
        self.assertEqual(again["sanitized_evidence"]["total"]
                         ["correct"], 38)

    def test_trace_policy_version(self):
        review = run_feedback(end_evidence({0}), "student", "end")
        self.assertEqual(review["trace"]["policy_version"],
                         POLICY_VERSION)
        self.assertFalse(
            review["trace"]["provider_advantage_demonstrated"])


if __name__ == "__main__":
    unittest.main()
