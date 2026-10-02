"""Tests for the provider-independent synthetic feedback foundation."""
import copy
import json
import unittest

from synthetic_feedback import (
    CANDIDATE_VERSION, GENERATION_INSTRUCTIONS, POLICY_VERSION,
    PROMPT_VERSION, run_synthetic_feedback)

END_SKILLS = [
    {"skill_id": "skill_a", "skill_name": "Arithmetic",
     "correct": 9, "out_of": 10},
    {"skill_id": "skill_b", "skill_name": "Prices",
     "correct": 5, "out_of": 10},
    {"skill_id": "skill_c", "skill_name": "Fractions",
     "correct": 7, "out_of": 10},
    {"skill_id": "skill_d", "skill_name": "Percentages",
     "correct": 6, "out_of": 10},
]
MIDPOINT_SKILLS = [
    {"skill_id": "skill_a", "skill_name": "Arithmetic",
     "correct": 4, "out_of": 5},
    {"skill_id": "skill_b", "skill_name": "Prices",
     "correct": 2, "out_of": 5},
    {"skill_id": "skill_c", "skill_name": "Fractions",
     "correct": 3, "out_of": 5},
    {"skill_id": "skill_d", "skill_name": "Percentages",
     "correct": 3, "out_of": 5},
]


def make_input(audience="student", checkpoint="end", skills=None):
    if skills is None:
        skills = (MIDPOINT_SKILLS if checkpoint == "midpoint"
                  else END_SKILLS)
    return {"schema": "phase3_synthetic_feedback_input_v1",
            "data_origin": "synthetic",
            "audience": audience,
            "checkpoint": checkpoint,
            "skills": copy.deepcopy(skills)}


class RecordingSelector:
    def __init__(self, reply):
        self.reply = reply
        self.payloads = []

    def select(self, payload):
        self.payloads.append(payload)
        return self.reply


class RecordingGenerator:
    def __init__(self, reply):
        self.reply = reply
        self.payloads = []

    def generate(self, payload):
        self.payloads.append(payload)
        return self.reply(payload) if callable(self.reply) else self.reply


class RaisingSelector:
    def __init__(self, message="selector failure"):
        self.message = message
        self.payloads = []

    def select(self, payload):
        self.payloads.append(payload)
        raise RuntimeError(self.message)


class RaisingGenerator:
    def __init__(self, message="generator failure"):
        self.message = message
        self.payloads = []

    def generate(self, payload):
        self.payloads.append(payload)
        raise RuntimeError(self.message)


def valid_opening(payload):
    opening = ("Keep going when you feel ready."
               if payload["checkpoint"] == "midpoint"
               else "Thank you for completing this assessment.")
    return {"candidate_id": payload["selected_candidate"]
            ["candidate_id"],
            "opening": opening}


class DefaultReviewTests(unittest.TestCase):
    def test_default_packages_for_each_audience_checkpoint(self):
        cases = [("student", "midpoint"), ("student", "end"),
                 ("teacher", "end")]
        for audience, checkpoint in cases:
            with self.subTest(audience=audience, checkpoint=checkpoint):
                result = run_synthetic_feedback(
                    make_input(audience, checkpoint))
                self.assertEqual(
                    result["schema"],
                    "phase3_synthetic_feedback_review_v1")
                self.assertEqual(
                    result["status"], "draft_not_for_learner_delivery")
                self.assertEqual(result["audience"], audience)
                self.assertEqual(result["checkpoint"], checkpoint)
                self.assertTrue(result["requires_human_review"])
                trace = result["trace"]
                self.assertEqual(
                    trace["candidate_version"], CANDIDATE_VERSION)
                self.assertEqual(trace["policy_version"], POLICY_VERSION)
                self.assertEqual(trace["prompt_version"], PROMPT_VERSION)
                self.assertEqual(trace["selection_source"], "rules")
                self.assertEqual(
                    trace["phrasing_source"], "deterministic")
                self.assertEqual(
                    trace["validation"], "deterministic_fixed_text")
                self.assertIsNone(trace["fallback_reason"])
                self.assertIsInstance(trace["latency_ms"], float)
                self.assertEqual(trace["provider_metadata"], {
                    "selector": None, "generator": None,
                    "model_version": None, "cost": None})

    def test_end_total_rendered_deterministically(self):
        for audience in ("student", "teacher"):
            with self.subTest(audience=audience):
                result = run_synthetic_feedback(make_input(audience, "end"))
                message = result["message"]
                self.assertEqual(
                    result["selected_candidate_id"], "observed_summary")
                self.assertEqual(message["opening"],
                                 "You have completed this assessment.")
                self.assertEqual(message["evidence_lines"], [
                    "Arithmetic: 9 of 10 correct on this assessment.",
                    "Prices: 5 of 10 correct on this assessment.",
                    "Fractions: 7 of 10 correct on this assessment.",
                    "Percentages: 6 of 10 correct on this assessment.",
                    "Observed total: 27 of 40 correct on this "
                    "assessment."])
                self.assertIn(
                    "These observations describe this assessment, "
                    "not overall mastery.", message["text"])
                self.assertEqual(result["template_baseline"], message)
                self.assertEqual(result["sanitized_evidence"]["total"],
                                 {"correct": 27, "out_of": 40})

    def test_student_midpoint_sends_and_renders_no_counts(self):
        selector = RecordingSelector({"candidate_id": "neutral"})
        generator = RecordingGenerator(valid_opening)
        result = run_synthetic_feedback(
            make_input("student", "midpoint"), selector, generator)
        self.assertEqual(selector.payloads[0]["evidence"], {})
        self.assertEqual(generator.payloads[0]["evidence"], {})
        self.assertEqual(result["sanitized_evidence"], {})
        for payload in (selector.payloads[0], generator.payloads[0]):
            blob = json.dumps(payload)
            for marker in ("skill_a", "Arithmetic", "correct",
                           "out_of", "total"):
                self.assertNotIn(marker, blob)
        self.assertIsNone(result["trace"]["fallback_reason"])
        self.assertEqual(result["trace"]["phrasing_source"],
                         "injected_generator")
        self.assertEqual(result["message"]["opening"],
                         "Keep going when you feel ready.")
        text = result["message"]["text"]
        self.assertNotIn("of 5", text)
        self.assertNotIn("correct", text)
        self.assertEqual(result["message"]["evidence_lines"], [])

    def test_midpoint_offers_only_neutral_candidate(self):
        result = run_synthetic_feedback(
            make_input("student", "midpoint"))
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(
            result["candidates"][0],
            {"candidate_id": "neutral",
             "strategy": "neutral_encouragement",
             "review_status": "draft_pending_educator_review",
             "permitted_evidence": []})
        self.assertEqual(result["selected_candidate_id"], "neutral")
        self.assertEqual(
            result["message"]["opening"],
            "You are halfway through the assessment. "
            "Continue when you are ready.")

    def test_teacher_midpoint_rejected(self):
        selector = RecordingSelector({"candidate_id": "neutral"})
        with self.assertRaises(ValueError):
            run_synthetic_feedback(
                make_input("teacher", "midpoint"), selector)
        self.assertEqual(selector.payloads, [])


class InputValidationTests(unittest.TestCase):
    def assert_rejected_before_callbacks(self, evidence):
        selector = RecordingSelector({"candidate_id": "neutral"})
        generator = RecordingGenerator(valid_opening)
        with self.assertRaises(ValueError):
            run_synthetic_feedback(evidence, selector, generator)
        self.assertEqual(selector.payloads, [])
        self.assertEqual(generator.payloads, [])

    def test_rejects_unknown_and_missing_input_keys(self):
        extra = make_input()
        extra["answer_index"] = 1
        missing = make_input()
        del missing["audience"]
        renamed = make_input()
        renamed["session_id"] = renamed.pop("schema")
        for bad in (extra, missing, renamed, None, "input", []):
            with self.subTest(bad=bad):
                self.assert_rejected_before_callbacks(bad)

    def test_rejects_wrong_schema_and_origin_labels(self):
        for key, values in (("schema", ("other_schema", "", None, 1)),
                            ("data_origin", ("real", "measured", False,
                                             True, None))):
            for value in values:
                bad = make_input()
                bad[key] = value
                with self.subTest(key=key, value=value):
                    self.assert_rejected_before_callbacks(bad)

    def test_rejects_unknown_audience_and_checkpoint(self):
        for key, values in (("audience", ("admin", "Student", None, 1)),
                            ("checkpoint", ("start", "End", None, 1))):
            for value in values:
                bad = make_input()
                bad[key] = value
                with self.subTest(key=key, value=value):
                    self.assert_rejected_before_callbacks(bad)

    def test_rejects_unknown_and_missing_skill_keys(self):
        extra = make_input()
        extra["skills"][0]["answer_index"] = 0
        extra2 = make_input()
        extra2["skills"][1]["subtopic"] = "x"
        missing = make_input()
        del missing["skills"][2]["correct"]
        for bad in (extra, extra2, missing):
            with self.subTest(bad=bad["skills"]):
                self.assert_rejected_before_callbacks(bad)

    def test_rejects_bool_and_out_of_range_counts(self):
        cases = []
        bad = make_input()
        bad["skills"][0]["correct"] = True
        cases.append(bad)
        bad = make_input()
        bad["skills"][0]["out_of"] = False
        cases.append(bad)
        bad = make_input()
        bad["skills"][0]["correct"] = 11
        cases.append(bad)
        bad = make_input()
        bad["skills"][0]["correct"] = -1
        cases.append(bad)
        bad = make_input()
        bad["skills"][0]["correct"] = "9"
        cases.append(bad)
        for case in cases:
            with self.subTest(row=case["skills"][0]):
                self.assert_rejected_before_callbacks(case)

    def test_rejects_wrong_denominators(self):
        cases = []
        bad = make_input("student", "end")
        bad["skills"][0]["out_of"] = 5
        cases.append(bad)
        bad = make_input("student", "end")
        bad["skills"][0]["out_of"] = 40
        bad["skills"][0]["correct"] = 40
        cases.append(bad)
        bad = make_input("student", "midpoint")
        bad["skills"][0]["out_of"] = 10
        cases.append(bad)
        bad = make_input("student", "midpoint")
        bad["skills"][0]["out_of"] = 20
        bad["skills"][0]["correct"] = 4
        cases.append(bad)
        for case in cases:
            with self.subTest(row=case["skills"][0]):
                self.assert_rejected_before_callbacks(case)

    def test_rejects_duplicate_and_bad_skill_ids(self):
        dup = make_input()
        dup["skills"][1]["skill_id"] = "skill_a"
        cases = [dup]
        for skill_id in ("", "skill a", "skill.a", "skill_a!",
                         "skill_\xe4", "x" * 41, 7):
            bad = make_input()
            bad["skills"][0]["skill_id"] = skill_id
            cases.append(bad)
        for case in cases:
            with self.subTest(ids=[r["skill_id"]
                                   for r in case["skills"]]):
                self.assert_rejected_before_callbacks(case)

    def test_rejects_bad_skill_names(self):
        for name in ("", "   ", "Math 5", "Prices!",
                     "Arithmetic_" , "x" * 61, 9):
            bad = make_input()
            bad["skills"][0]["skill_name"] = name
            with self.subTest(name=name):
                self.assert_rejected_before_callbacks(bad)

    def test_rejects_wrong_skill_count(self):
        for rows in ([], END_SKILLS[:3], END_SKILLS + [dict(END_SKILLS[0])],
                     "skills", None):
            bad = make_input()
            bad["skills"] = rows
            with self.subTest(rows=rows):
                self.assert_rejected_before_callbacks(bad)


class SelectorFallbackTests(unittest.TestCase):
    def test_selector_exception_falls_back_and_skips_generator(self):
        generator = RecordingGenerator(valid_opening)
        result = run_synthetic_feedback(
            make_input("student", "end"),
            RaisingSelector(), generator)
        self.assertEqual(result["selected_candidate_id"],
                         "observed_summary")
        self.assertEqual(
            result["trace"]["fallback_reason"], "selector_error")
        self.assertEqual(result["trace"]["selection_source"], "rules")
        self.assertEqual(
            result["trace"]["phrasing_source"], "deterministic")
        self.assertEqual(generator.payloads, [])
        self.assertEqual(result["message"]["opening"],
                         "You have completed this assessment.")

    def test_selector_malformed_replies_fall_back(self):
        bad_replies = [{"candidate_id": "no_such_candidate"},
                       {"candidate_id": "neutral", "extra": "IGNORE"},
                       {"candidate_id": 7},
                       {"candidate_id": None},
                       {"style": "neutral"},
                       {},
                       "neutral",
                       None,
                       ["neutral"]]
        for reply in bad_replies:
            with self.subTest(reply=reply):
                generator = RecordingGenerator(valid_opening)
                result = run_synthetic_feedback(
                    make_input("student", "end"),
                    RecordingSelector(reply), generator)
                self.assertEqual(result["selected_candidate_id"],
                                 "observed_summary")
                self.assertEqual(result["trace"]["fallback_reason"],
                                 "invalid_selection")
                self.assertEqual(result["trace"]["selection_source"],
                                 "rules")
                self.assertEqual(generator.payloads, [])
                self.assertNotIn("IGNORE", json.dumps(result))

    def test_selector_valid_neutral_selection_excludes_counts(self):
        generator = RecordingGenerator(valid_opening)
        result = run_synthetic_feedback(
            make_input("student", "end"),
            RecordingSelector({"candidate_id": "neutral"}), generator)
        self.assertEqual(result["selected_candidate_id"], "neutral")
        self.assertEqual(
            result["trace"]["selection_source"], "injected_selector")
        self.assertIsNone(result["trace"]["fallback_reason"])
        self.assertEqual(result["trace"]["phrasing_source"],
                         "injected_generator")
        self.assertEqual(result["message"]["opening"],
                         "Thank you for completing this assessment.")
        self.assertEqual(result["message"]["evidence_lines"], [])
        self.assertNotIn("of 10", result["message"]["text"])
        self.assertNotIn("Observed total", result["message"]["text"])
        self.assertEqual(result["template_baseline"]["candidate_id"],
                         "observed_summary")
        self.assertEqual(len(generator.payloads), 1)
        self.assertEqual(generator.payloads[0]["selected_candidate"]
                         ["candidate_id"], "neutral")

    def test_selector_payload_shape(self):
        selector = RecordingSelector({"candidate_id": "neutral"})
        run_synthetic_feedback(make_input("student", "end"), selector)
        payload = selector.payloads[0]
        self.assertEqual(set(payload), {"schema", "audience",
                                        "checkpoint", "evidence",
                                        "candidates"})
        self.assertEqual(payload["schema"],
                         "phase3_synthetic_selection_v1")
        self.assertEqual(payload["audience"], "student")
        self.assertEqual(payload["checkpoint"], "end")
        self.assertEqual(
            [c["candidate_id"] for c in payload["candidates"]],
            ["observed_summary", "neutral"])
        observed = payload["candidates"][0]
        self.assertEqual(observed["permitted_evidence"],
                         ["skill_a", "skill_b", "skill_c", "skill_d",
                          "total"])


class GeneratorFallbackTests(unittest.TestCase):
    def test_generator_exception_falls_back(self):
        result = run_synthetic_feedback(
            make_input("student", "end"),
            generator=RaisingGenerator())
        self.assertEqual(result["trace"]["fallback_reason"],
                         "generator_error")
        self.assertEqual(result["trace"]["phrasing_source"],
                         "deterministic")
        self.assertEqual(result["message"]["opening"],
                         "You have completed this assessment.")

    def test_generator_invalid_replies_fall_back(self):
        bad_replies = [
            {"candidate_id": "neutral", "opening": "Valid text."},
            {"candidate_id": "observed_summary", "opening": "Ok.",
             "extra": "IGNORE"},
            {"candidate_id": "observed_summary"},
            {"opening": "Ok."},
            {"candidate_id": "observed_summary", "opening": ""},
            {"candidate_id": "observed_summary", "opening": "   "},
            {"candidate_id": "observed_summary", "opening": "x" * 301},
            {"candidate_id": "observed_summary", "opening": 42},
            {"candidate_id": "observed_summary", "opening": None},
            "opening",
            None,
        ]
        for reply in bad_replies:
            with self.subTest(reply=reply):
                result = run_synthetic_feedback(
                    make_input("student", "end"),
                    generator=RecordingGenerator(reply))
                self.assertEqual(result["trace"]["fallback_reason"],
                                 "invalid_generation")
                self.assertEqual(result["trace"]["phrasing_source"],
                                 "deterministic")
                self.assertEqual(result["message"]["opening"],
                                 "You have completed this assessment.")
                self.assertNotIn("IGNORE", json.dumps(result))

    def test_generator_rejects_numerics_and_banned_terms(self):
        bad_openings = [
            "You answered 5 questions.",
            "Your mastery shows promise.",
            "Unicode digit \xb2 or \xbd here.",
            "This suggests a misconception.",
            "Diagnosis: needs help.",
            "You might be tired.",
            "This shows fatigue.",
            "You have mastered this.",
            "Keep improving.",
            "You learned a lot.",
            "Your score was high.",
            "Half the answers were correct.",
            "A prerequisite review may help.",
            "Fifty percent done.",
            "Arithmetic went well.",
            "fractions were fine.",
            "PRICES",
        ]
        for opening in bad_openings:
            with self.subTest(opening=opening):
                result = run_synthetic_feedback(
                    make_input("student", "end"),
                    generator=RecordingGenerator(
                        {"candidate_id": "observed_summary",
                         "opening": opening}))
                self.assertEqual(result["trace"]["fallback_reason"],
                                 "invalid_generation")
                self.assertEqual(result["message"]["opening"],
                                 "You have completed this assessment.")
                self.assertNotIn(opening, json.dumps(result))

    def test_generator_midpoint_rejects_private_skill_name(self):
        generator = RecordingGenerator(
            {"candidate_id": "neutral",
             "opening": "Arithmetic needs attention."})
        result = run_synthetic_feedback(
            make_input("student", "midpoint"), generator=generator)
        self.assertEqual(result["trace"]["fallback_reason"],
                         "invalid_generation")
        self.assertEqual(result["message"]["opening"],
                         "You are halfway through the assessment. "
                         "Continue when you are ready.")
        self.assertNotIn("Arithmetic",
                         json.dumps(generator.payloads[0]))
        self.assertNotIn("Arithmetic", json.dumps(result))

    def test_generator_valid_opening_flagged_for_human_review(self):
        result = run_synthetic_feedback(
            make_input("student", "end"),
            generator=RecordingGenerator(
                {"candidate_id": "observed_summary",
                 "opening": "Thank you for completing this "
                            "assessment."}))
        self.assertIsNone(result["trace"]["fallback_reason"])
        self.assertEqual(result["trace"]["phrasing_source"],
                         "injected_generator")
        self.assertEqual(result["trace"]["validation"],
                         "shape_and_heuristic_checks_only")
        self.assertTrue(result["requires_human_review"])
        self.assertEqual(
            result["message"]["opening"],
            "Thank you for completing this assessment.")
        self.assertIn("Observed total: 27 of 40",
                      result["message"]["text"])

    def test_generator_payload_shape(self):
        generator = RecordingGenerator(valid_opening)
        result = run_synthetic_feedback(make_input("teacher", "end"),
                                        generator=generator)
        self.assertIsNone(result["trace"]["fallback_reason"])
        self.assertEqual(result["trace"]["phrasing_source"],
                         "injected_generator")
        payload = generator.payloads[0]
        self.assertEqual(set(payload), {"schema", "audience",
                                        "checkpoint", "evidence",
                                        "selected_candidate",
                                        "prompt_version",
                                        "instructions"})
        self.assertEqual(payload["schema"],
                         "phase3_synthetic_generation_v1")
        self.assertEqual(payload["audience"], "teacher")
        self.assertEqual(payload["prompt_version"], PROMPT_VERSION)
        self.assertEqual(payload["instructions"], GENERATION_INSTRUCTIONS)
        self.assertEqual(payload["selected_candidate"]["candidate_id"],
                         "observed_summary")


class IsolationAndPrivacyTests(unittest.TestCase):
    def test_callback_mutations_do_not_leak(self):
        seen = {}

        def mutating_selector(payload):
            seen["selector_evidence"] = copy.deepcopy(
                payload["evidence"])
            payload["evidence"]["skills"][0]["correct"] = 0
            payload["evidence"]["skills"][0]["skill_name"] = "Calculus"
            payload["candidates"].append(
                {"candidate_id": "evil", "strategy": "x",
                 "review_status": "x", "permitted_evidence": []})
            return {"candidate_id": "observed_summary"}

        class Selector:
            def select(self, payload):
                return mutating_selector(payload)

        class Generator:
            def __init__(self):
                self.payloads = []

            def generate(self, payload):
                self.payloads.append(payload)
                seen["generator_evidence"] = copy.deepcopy(
                    payload["evidence"])
                payload["evidence"]["skills"][0]["correct"] = 0
                payload["selected_candidate"]["candidate_id"] = "evil"
                return {"candidate_id": "observed_summary",
                        "opening": "Thank you for completing this "
                                   "assessment."}

        result = run_synthetic_feedback(
            make_input("student", "end"), Selector(), Generator())
        self.assertEqual(
            seen["generator_evidence"],
            {"skills": END_SKILLS,
             "total": {"correct": 27, "out_of": 40}})
        self.assertEqual(
            [c["candidate_id"] for c in result["candidates"]],
            ["observed_summary", "neutral"])
        self.assertEqual(result["selected_candidate_id"],
                         "observed_summary")
        self.assertIn("Arithmetic: 9 of 10 correct",
                      result["message"]["text"])
        blob = json.dumps(result)
        self.assertNotIn("Calculus", blob)
        self.assertNotIn("evil", blob)

    def test_private_markers_rejected_and_absent(self):
        bad = make_input()
        bad["skills"][0]["answer_index"] = 2
        with self.assertRaises(ValueError):
            run_synthetic_feedback(bad)
        selector = RecordingSelector({"candidate_id": "neutral"})
        generator = RecordingGenerator(valid_opening)
        result = run_synthetic_feedback(
            make_input("teacher", "end"), selector, generator)
        blob = (json.dumps(result) + json.dumps(selector.payloads)
                + json.dumps(generator.payloads))
        for marker in ("answer_index", "question_id", "session_id",
                       "selected_index", "answer_key", "bank"):
            self.assertNotIn(marker, blob)

    def test_exception_secret_strings_not_exposed(self):
        secret = "sk-secret-token-abc123"
        result = run_synthetic_feedback(
            make_input("student", "end"),
            RaisingSelector(secret))
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn("RuntimeError", json.dumps(result))
        result = run_synthetic_feedback(
            make_input("student", "end"),
            RecordingSelector({"candidate_id": "neutral"}),
            RaisingGenerator(secret))
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["trace"]["fallback_reason"],
                         "generator_error")

    def test_input_and_result_are_not_shared(self):
        evidence = make_input("student", "end")
        result = run_synthetic_feedback(evidence)
        result["candidates"][0]["candidate_id"] = "mutated"
        result["sanitized_evidence"]["skills"][0]["correct"] = 0
        again = run_synthetic_feedback(evidence)
        self.assertEqual(again["candidates"][0]["candidate_id"],
                         "observed_summary")
        self.assertEqual(again["sanitized_evidence"]["skills"][0]
                         ["correct"], 9)
        self.assertEqual(evidence["skills"][0]["correct"], 9)


if __name__ == "__main__":
    unittest.main()
