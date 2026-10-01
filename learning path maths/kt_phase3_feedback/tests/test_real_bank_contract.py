"""Optional local contract test for the real approved Phase 2 bank.

This test uses only the JSON bank and a temporary session store. It does not
load the frozen checkpoint or the multi-GB embedding table, and it skips when
the private approved artifact is unavailable.
"""
import json
import tempfile
import unittest
from pathlib import Path

import phase3_paths
from mcq_service import MCQSessionService
from scenario_runner import generate_responses
from session_store import SessionStore


@unittest.skipUnless(
    phase3_paths.APPROVED_BANK.exists(),
    "approved v2 bank is a private local artifact",
)
class RealBankContractTests(unittest.TestCase):
    def service(self, tmpdir: str) -> MCQSessionService:
        return MCQSessionService.from_default(SessionStore(Path(tmpdir)))

    def test_start_session_serves_only_public_half_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = self.service(tmpdir)
            start = service.start_session()
            self.assertEqual(20, len(start["questions"]))
            for question in start["questions"]:
                self.assertEqual(
                    {"question_id", "skill_id", "text", "options"},
                    set(question),
                )

    def test_taxonomy_covers_real_bank_and_student_scores_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = self.service(tmpdir)
            self.assertIsNotNone(service.taxonomy)
            self.assertEqual(15, sum(len(topic["subtopics"])
                                     for topic in service.taxonomy["topics"]))
            sid = service.start_session()["session_id"]
            rows = [{"question_id": question["question_id"],
                     "selected_index": question["answer_index"]}
                    for question in service.bank["questions"]]
            midpoint = service.submit_half(sid, 1, rows[:20])["feed"]["student"]
            end = service.submit_half(sid, 2, rows[20:])["feed"]["student"]
            self.assertEqual((20, 20), (sum(r["correct"] for r in midpoint["subtopics"]),
                                        sum(r["out_of"] for r in midpoint["subtopics"])))
            self.assertEqual((40, 40), (sum(r["correct"] for r in end["subtopics"]),
                                        sum(r["out_of"] for r in end["subtopics"])))
            self.assertEqual(15, len(end["subtopics"]))
            self.assertTrue(all(set(row) == {"skill_id", "skill_name", "subtopic_id",
                                             "subtopic_name", "correct", "out_of"}
                                for row in end["subtopics"]))

    def test_approved_bank_scores_two_ordered_halves_privately(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = self.service(tmpdir)
            session_id = service.start_session()["session_id"]
            rows = [{
                "question_id": question["question_id"],
                "selected_index": question["answer_index"],
            } for question in service.bank["questions"]]
            midpoint = service.submit_half(session_id, 1, rows[:20])
            end = service.submit_half(session_id, 2, rows[20:])
            self.assertEqual("midpoint", midpoint["checkpoint"])
            self.assertEqual(20, midpoint["feed"]["teacher"]["total"]["correct"])
            self.assertEqual("end", end["checkpoint"])
            self.assertEqual(40, end["feed"]["teacher"]["total"]["correct"])

    def test_full_bank_boundary_profiles_and_private_checkpoint_contract(self):
        questions = self.service_bank_questions()
        cases = {
            "all_correct": [True] * 40,
            "all_incorrect": [False] * 40,
            "first_half_only": [True] * 20 + [False] * 20,
            "second_half_only": [False] * 20 + [True] * 20,
        }
        for name, outcomes in cases.items():
            with self.subTest(profile=name), tempfile.TemporaryDirectory() as tmpdir:
                service = self.service(tmpdir)
                start = service.start_session()
                sid = start["session_id"]
                self.assertEqual([q["question_id"] for q in questions[:20]],
                                 [q["question_id"] for q in start["questions"]])
                with self.assertRaises(ValueError):
                    service.questions(sid, 2)
                with self.assertRaises(ValueError):
                    service.submit_response(sid, {"question_id": questions[1]["question_id"],
                                                  "selected_index": 0})
                rows = [{"question_id": q["question_id"],
                         "selected_index": q["answer_index"] if correct else
                         (q["answer_index"] + 1) % len(q["options"])}
                        for q, correct in zip(questions, outcomes)]
                for half, limit in ((1, 20), (2, 40)):
                    if half == 2:
                        served = service.questions(sid, 2)["questions"]
                        self.assertEqual([q["question_id"] for q in questions[20:]],
                                         [q["question_id"] for q in served])
                    for position in range(limit - 20, limit):
                        response = service.submit_response(sid, rows[position])
                        self.assertEqual(position + 1, response["position"])
                        self.assertEqual("midpoint" if position == 19 else
                                         "end" if position == 39 else None,
                                         response["checkpoint"])
                        if position not in (19, 39):
                            self.assertIsNone(response["feed"])
                    feed = response["feed"]
                    expected = sum(outcomes[:limit])
                    self.assertEqual({"correct": expected, "out_of": limit},
                                     feed["teacher"]["total"])
                    self.assertEqual(limit, sum(s["out_of"] for s in
                                                feed["student"]["skills"]))
                    self.assertEqual(expected, sum(s["correct"] for s in
                                                   feed["student"]["skills"]))
                    self.assertEqual(limit, sum(s["out_of"] for s in
                                                feed["student"]["subtopics"]))
                    for skill in feed["student"]["skills"]:
                        positions = [i for i, q in enumerate(questions[:limit])
                                     if q["skill_id"] == skill["skill_id"]]
                        self.assertEqual((sum(outcomes[i] for i in positions),
                                          len(positions)),
                                         (skill["correct"], skill["out_of"]))
                    self.assertEqual("deterministic", feed["student"]["message_source"])
                    if half == 1:
                        self.assertNotIn("total", feed["student"])
                        self.assertEqual("You have completed 20 questions. You are halfway "
                                         "through the assessment. Take a moment if you need "
                                         "one, then continue when you are ready.",
                                         feed["student"]["message"])
                    else:
                        self.assertEqual({"correct": expected, "out_of": 40},
                                         feed["student"]["total"])
                        self.assertEqual(
                            "All 40 questions answered. These results describe "
                            "your answers on this assessment, not your overall mastery.",
                            feed["student"]["summary"])
                        self.assertTrue(feed["student"]["message"].startswith(
                            "You have completed all 40 questions."))
                    public = json.dumps(feed["student"])
                    for private in ("answer_index", "selected_index", "question_id",
                                    "selected_text", "conformal", "model_estimate",
                                    "routing", "graph", "teacher"):
                        self.assertNotIn(private, public)
                    self.assertEqual(limit, service.snapshot(sid)["answered_count"])
                record = service.private_record(sid)
                self.assertEqual("complete", record["status"])
                self.assertEqual([q["question_id"] for q in questions],
                                 [r["question_id"] for r in record["responses"]])
                self.assertEqual(outcomes, [r["correct"] for r in record["responses"]])
                with self.assertRaises(ValueError):
                    service.submit_response(sid, rows[0])

    def test_requested_deterministic_matrix(self):
        questions = self.service_bank_questions()
        skills = list(dict.fromkeys(q["skill_id"] for q in questions))
        by_skill = {sid: [i for i, q in enumerate(questions) if q["skill_id"] == sid]
                    for sid in skills}
        self.assertEqual([10] * 4, [len(by_skill[sid]) for sid in skills])
        cases = {"all_correct": set(range(40)), "all_incorrect": set(),
                 "first_strong": set(range(20)), "second_strong": set(range(20, 40)),
                 "global_alternating": set(range(0, 40, 2)),
                 "long_correct_streak": set(range(30)),
                 "long_incorrect_streak": set(range(30, 40)),
                 "all_skills_equal": {i for sid in skills for i in by_skill[sid][:5]},
                 "tied_strongest": {i for sid in skills[:2] for i in by_skill[sid][:8]}
                                   | {i for sid in skills[2:] for i in by_skill[sid][:3]},
                 "tied_weakest": {i for sid in skills[:2] for i in by_skill[sid][:2]}
                                 | {i for sid in skills[2:] for i in by_skill[sid][:8]}}
        for sid in skills:
            cases[f"only_{sid}_weak"] = set(range(40)) - set(by_skill[sid])
            cases[f"only_{sid}_strong"] = set(by_skill[sid])
        cases["multiple_weak"] = set(range(40)) - set(by_skill[skills[0]] + by_skill[skills[1]])
        for name, correct_positions in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                service = self.service(tmp)
                sid = service.start_session()["session_id"]
                rows = [{"question_id": q["question_id"],
                         "selected_index": q["answer_index"] if i in correct_positions else
                         (q["answer_index"] + 1) % len(q["options"])}
                        for i, q in enumerate(questions)]
                midpoint = service.submit_half(sid, 1, rows[:20])["feed"]
                end = service.submit_half(sid, 2, rows[20:])["feed"]
                for limit, feed in ((20, midpoint), (40, end)):
                    self.assertEqual((len([i for i in correct_positions if i < limit]), limit),
                                     (feed["teacher"]["total"]["correct"],
                                      feed["teacher"]["total"]["out_of"]))
                    for score in feed["student"]["skills"]:
                        indexes = [i for i in by_skill[score["skill_id"]] if i < limit]
                        self.assertEqual((len(correct_positions.intersection(indexes)), len(indexes)),
                                         (score["correct"], score["out_of"]))
                    self.assertEqual((feed["teacher"]["total"]["correct"], limit),
                                     (sum(r["correct"] for r in feed["student"]["subtopics"]),
                                      sum(r["out_of"] for r in feed["student"]["subtopics"])))
                message = end["student"]["message"]
                scores = [len(correct_positions.intersection(by_skill[sid])) for sid in skills]
                names = {s["skill_id"]: s["skill_name"]
                         for s in end["student"]["skills"]}
                high, low = max(scores), min(scores)
                if low == 10:
                    self.assertIn("You answered every question correctly "
                                  "on this assessment.", message)
                elif high == 0:
                    self.assertIn("None of the answers on this assessment "
                                  "were correct.", message)
                elif high == low:
                    self.assertIn("Your observed scores were equal across "
                                  "the topics.", message)
                else:
                    top = [sid for sid, s in zip(skills, scores)
                           if s == high]
                    bottom = [sid for sid, s in zip(skills, scores)
                              if s == low]
                    if len(top) == 1:
                        self.assertIn(f"On {names[top[0]]}, you answered "
                                      f"{high} of 10 questions correctly.",
                                      message)
                    else:
                        self.assertIn("Your highest observed scores were "
                                      "on", message)
                        for sid in top:
                            self.assertIn(names[sid], message)
                        self.assertIn(f"({high} of 10 correct in each)",
                                      message)
                    if len(bottom) == 1:
                        self.assertIn("For your next step, practice more "
                                      f"questions on {names[bottom[0]]} "
                                      f"({low} of 10 correct).", message)
                    else:
                        self.assertIn("For your next step, choose one of",
                                      message)
                        for sid in bottom:
                            self.assertIn(names[sid], message)
                        self.assertIn(f"({low} of 10 correct in each)",
                                      message)

    def test_same_skill_total_different_subtopic_errors(self):
        questions = self.service_bank_questions()
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            topic = next(t for t in service.taxonomy["topics"] if
                         len(t["subtopics"]) >= 2 and
                         sum(len(s["question_ids"]) >= 2 for s in t["subtopics"]) >= 2)
            first, second = [s for s in topic["subtopics"] if len(s["question_ids"]) >= 2][:2]
            outputs = []
            for errors in (set(first["question_ids"][:2]), set(second["question_ids"][:2])):
                sid = service.start_session()["session_id"]
                rows = [{"question_id": q["question_id"],
                         "selected_index": ((q["answer_index"] + 1) % len(q["options"])
                                            if q["question_id"] in errors else q["answer_index"])}
                        for q in questions]
                service.submit_half(sid, 1, rows[:20])
                outputs.append(service.submit_half(sid, 2, rows[20:])["feed"]["student"])
            self.assertEqual(outputs[0]["skills"], outputs[1]["skills"])
            self.assertEqual(outputs[0]["message"], outputs[1]["message"])
            self.assertNotEqual(outputs[0]["subtopics"], outputs[1]["subtopics"])
            for result, failed in zip(outputs, (first, second)):
                counts = {r["subtopic_id"]: r["correct"] for r in result["subtopics"]}
                self.assertEqual(len(failed["question_ids"]) - 2, counts[failed["id"]])

    def test_wrong_option_invariance_and_session_rejections(self):
        questions = self.service_bank_questions()
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            first = questions[0]
            sid = service.start_session()["session_id"]
            original = service.snapshot(sid)
            for index in (-1, len(first["options"]), True, 0.0, "0", None):
                with self.subTest(index=index), self.assertRaises(ValueError):
                    service.submit_response(sid, {"question_id": first["question_id"],
                                                  "selected_index": index})
                self.assertEqual(original, service.snapshot(sid))
            with self.assertRaises(ValueError):
                service.submit_response(sid, {"question_id": questions[1]["question_id"],
                                              "selected_index": 0})
            with self.assertRaises(ValueError):
                service.submit_half(sid, 2, [])
            with self.assertRaises(ValueError):
                service.submit_half(sid, 1, [])
            self.assertEqual(original, service.snapshot(sid))
            for q in questions[:19]:
                service.submit_response(sid, {"question_id": q["question_id"],
                                              "selected_index": q["answer_index"]})
            self.assertEqual([], service.snapshot(sid)["checkpoints_available"])
            with self.assertRaises(ValueError):
                service.questions(sid, 2)
            with self.assertRaises(ValueError):
                service.submit_half(sid, 2, [{"question_id": q["question_id"],
                                             "selected_index": q["answer_index"]}
                                            for q in questions[20:]])
            self.assertEqual(19, service.snapshot(sid)["answered_count"])
            results = []
            for shift in (1, 2):
                current = service.start_session()["session_id"]
                rows = [{"question_id": q["question_id"],
                         "selected_index": (q["answer_index"] if i % 2 else
                                            (q["answer_index"] + shift) % len(q["options"]))}
                        for i, q in enumerate(questions)]
                mid = service.submit_half(current, 1, rows[:20])["feed"]
                with self.assertRaises(ValueError):
                    service.submit_response(current, rows[0])
                with self.assertRaises(ValueError):
                    service.submit_half(current, 1, rows[:20])
                with self.assertRaises(ValueError):
                    service.submit_half(current, 2, rows[20:39])
                self.assertEqual(20, service.snapshot(current)["answered_count"])
                for row in rows[20:39]:
                    step = service.submit_response(current, row)
                    self.assertIsNone(step["feed"])
                self.assertEqual(["midpoint"], service.snapshot(current)["checkpoints_available"])
                end = service.submit_response(current, rows[39])["feed"]
                with self.assertRaises(ValueError):
                    service.submit_half(current, 2, rows[20:])
                record = service.private_record(current)
                self.assertEqual([q["options"][r["selected_index"]]
                                  for q, r in zip(questions, rows)],
                                 [r["selected_text"] for r in record["responses"]])
                results.append((mid, end))
            self.assertEqual(results[0], results[1])
            changed = json.loads(json.dumps(service.bank))
            changed["questions"][0]["answer_index"] = (
                changed["questions"][0]["answer_index"] + 1) % len(first["options"])
            other = MCQSessionService(changed, service.store)
            with self.assertRaises(ValueError):
                other.snapshot(sid)
            with self.assertRaises(ValueError):
                other.submit_response(sid, {"question_id": first["question_id"],
                                            "selected_index": first["answer_index"]})

    def test_seeded_profiles_across_five_seeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            bank = service.bank
            profiles = ("stable_strong", "stable_weak", "guessing", "learning",
                        "fatigue", "weak_fractions")
            for profile in profiles:
                patterns = set()
                for seed in (11, 23, 37, 41, 53):
                    with self.subTest(profile=profile, seed=seed):
                        spec = {"profile": profile, "seed": seed}
                        generated = generate_responses(bank, spec)
                        self.assertEqual(generated, generate_responses(bank, spec))
                        self.assertEqual(40, len(generated))
                        patterns.add(tuple(r["correct"] for r in generated))
                        sid = service.start_session()["session_id"]
                        rows = [{"question_id": r["question_id"],
                                 "selected_index": r["selected_index"]} for r in generated]
                        mid = service.submit_half(sid, 1, rows[:20])["feed"]
                        end = service.submit_half(sid, 2, rows[20:])["feed"]
                        self.assertEqual(sum(r["correct"] for r in generated[:20]),
                                         mid["teacher"]["total"]["correct"])
                        self.assertEqual(sum(r["correct"] for r in generated),
                                         end["student"]["total"]["correct"])
                        self.assertEqual(sum(r["correct"] for r in generated),
                                         sum(s["correct"] for s in end["student"]["skills"]))
                        self.assertEqual(sum(r["correct"] for r in generated),
                                         sum(s["correct"] for s in end["student"]["subtopics"]))
                        for q, row in zip(bank["questions"], generated):
                            self.assertEqual(row["correct"],
                                             row["selected_index"] == q["answer_index"])
                        if profile == "guessing":
                            self.assertTrue(all(abs(r["true_probability"] -
                                                    1 / len(q["options"])) < 1e-6
                                                for q, r in zip(bank["questions"], generated)))
                self.assertGreater(len(patterns), 1, profile)

    def test_frozen_kt_wrong_option_sensitivity(self):
        import paths as phase2_paths
        if not all(p.exists() for p in (phase2_paths.TEXT_EMBEDDINGS,
                                         phase2_paths.CHECKPOINT, phase2_paths.VOCAB)):
            self.skipTest("frozen KT inputs are not available locally")
        from frozen_model import load_frozen_model
        from kt_adapter import trace_responses
        bank = self.service_bank_questions()
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            ordered = service.bank["questions"]
            self.assertEqual([q["question_id"] for q in bank],
                             [q["question_id"] for q in ordered])
            sequences = []
            for shift in (1, 2):
                sequences.append([{"question_id": q["question_id"],
                                   "selected_index": (q["answer_index"] if i % 2 else
                                                      (q["answer_index"] + shift) % len(q["options"]))}
                                  for i, q in enumerate(ordered)])
            self.assertTrue(all(len(q["options"]) >= 3 for q in ordered))
            kt = load_frozen_model(device="cpu")
            traces = [trace_responses(service.bank, rows, kt=kt, device="cpu")
                      for rows in sequences]
            a, b = (t["p_correct_before_each_answer"] for t in traces)
            self.assertEqual(40, len(a))
            self.assertEqual(a[0], b[0])
            self.assertTrue(all(0 <= p <= 1 for p in a + b))
            self.assertTrue(any(x != y for x, y in zip(a[1:], b[1:])),
                            "KT did not respond to any changed wrong-option selection")
            self.assertEqual(
                [r["selected_index"] == q["answer_index"]
                 for r, q in zip(sequences[0], ordered)],
                [r["selected_index"] == q["answer_index"]
                 for r, q in zip(sequences[1], ordered)])

    def service_bank_questions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            return self.service(tmpdir).bank["questions"]


if __name__ == "__main__":
    unittest.main()
