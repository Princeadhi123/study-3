"""V3 tests for the 24-question descriptive content graph."""
import collections
import unittest

import research_content_catalog as catalog
import research_content_descriptions_v3 as descriptions_v3
import research_content_graph as rcg


class V3CaptureTests(unittest.TestCase):
    def test_v3_capture_loads_and_validates(self):
        graph = rcg.load_capture(rcg.DEFAULT_CAPTURE)
        self.assertEqual(graph["schema"], rcg.V3_SCHEMA)
        self.assertEqual(
            graph["scope"], "offline_research_only_not_learner_approved")
        self.assertEqual(
            graph["claim_boundary"],
            "descriptive_content_map_not_mastery_or_misconception_diagnosis")
        self.assertFalse(graph["used_for_student_advice"])
        self.assertTrue(rcg.validate_graph(graph))
        self.assertEqual(len(graph["exercises"]), 104)

    def test_v3_source_binding_and_counts(self):
        graph = descriptions_v3.build_practice_pool_v2()
        sources = {source["id"]: source for source in graph["sources"]}
        self.assertEqual(
            sources["practice_pool_v2"]["file"],
            "practice_pool_v2_private.json")
        self.assertEqual(
            sources["practice_pool_v2"]["sha256"], descriptions_v3.POOL_HASH)
        practice = [exercise for exercise in graph["exercises"]
                    if exercise["source_id"] == "practice_pool_v2"]
        self.assertEqual(len(practice), 24)
        self.assertEqual(
            collections.Counter(exercise["skill_id"] for exercise in practice),
            {catalog.PERCENT: 6, catalog.NUMBER: 6,
             catalog.ALGEBRA: 6, catalog.FRACTION: 6})
        stems = [catalog.normalized_stem(exercise["question"]["text"])
                 for exercise in practice]
        self.assertEqual(len(set(stems)), 24)

    def test_synthetic_questions_map_to_divisibility(self):
        graph = descriptions_v3.build_practice_pool_v2()
        synthetic = [exercise for exercise in graph["exercises"]
                     if exercise["question"]["item_id"].startswith("synthetic_")]
        self.assertEqual(len(synthetic), 4)
        self.assertEqual(
            {exercise["concept_id"] for exercise in synthetic},
            {"divisibility"})
        self.assertTrue(all(
            exercise["exercise_format"] == "short_verbal_prompt"
            for exercise in synthetic))

    def test_v3_review_and_provenance_boundaries(self):
        graph = descriptions_v3.build_practice_pool_v2()
        synthetic = graph["review_input"]["synthetic_practice_questions"]
        self.assertEqual(synthetic["count"], 4)
        self.assertEqual(synthetic["user_review"], "accepted")
        self.assertEqual(synthetic["formal_educator_review"], "pending")
        self.assertFalse(synthetic["historical_student_content"])
        self.assertFalse(
            graph["practice_source_update"]
            ["synthetic_questions_are_historical_student_content"])
        self.assertEqual(
            graph["practice_source_update"]["from_source_id"],
            "practice_pool")

    def test_v3_render_notes_synthetic_scope(self):
        page = rcg.render_graph(descriptions_v3.build_practice_pool_v2())
        self.assertIn("Practice source v2 contains 24 questions", page)
        self.assertIn("not historical student content", page)
        self.assertIn("no student diagnosis", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)

    def test_v1_and_v2_parent_captures_still_load(self):
        v1 = rcg.load_capture(rcg.V1_CAPTURE)
        v2 = rcg.load_capture(
            rcg.V2_PARENT_CAPTURE.parent)
        self.assertEqual(v1["schema"], rcg.V1_SCHEMA)
        self.assertEqual(v2["schema"], rcg.V2_SCHEMA)
        self.assertEqual(
            sum(1 for exercise in v2["exercises"]
                if exercise["source_id"] == "practice_pool"), 20)


if __name__ == "__main__":
    unittest.main()
