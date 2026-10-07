"""V2 structural tests for the descriptive content-graph renderer/validator.

Synthetic fixtures only; no frozen capture recomputation, model, or network.
"""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import research_content_catalog as catalog
import research_content_descriptions as descriptions
import research_content_graph as rcg


def make_v2_question():
    return {"question_id": "ex1", "skill_id": "skill_one",
            "item_id": "i1", "exercise_id": "e1", "text": "1 + 1?",
            "options": ["2", "3"], "answer_index": 0,
            "content_text": "1 + 1? [OPTIONS] 2 | 3"}


def make_v2_graph(source_file=None):
    sources = [
        {"id": "warm_bank", "file": "warm_bank_private.json",
         "sha256": catalog.SOURCE_HASHES["warm_bank_private.json"],
         "role": "assessment"},
        {"id": "cold_bank", "file": "cold_bank_private.json",
         "sha256": catalog.SOURCE_HASHES["cold_bank_private.json"],
         "role": "assessment"},
        {"id": "practice_pool", "file": "practice_pool_private.json",
         "sha256": catalog.SOURCE_HASHES["practice_pool_private.json"],
         "role": "practice"}]
    if source_file is not None:
        sources = [source_file]
    return {
        "schema": "phase3_bounded_content_graph_v2",
        "scope": "offline_research_only_not_learner_approved",
        "status": "draft_pending_educator_review",
        "used_for_student_advice": False,
        "claim_boundary":
            "descriptive_content_map_not_mastery_or_misconception_diagnosis",
        "description_boundary": descriptions.TASK_BOUNDARY,
        "generation_scope": {
            "provider_authority_changed": False,
            "student_diagnosis_permitted": False,
            "reading_skill_score_permitted": False,
            "unreviewed_routing_permitted": False},
        "review_input": {
            "source": "user_supplied_Gemini_comments",
            "type": "automated_LLM_review_not_independent_educator_validation",
            "model_version": None,
            "educator_reviews_completed": 0},
        "sources": sources,
        "nodes": [
            {"id": "skill_one", "type": "skill", "label": "Skill one"},
            {"id": "concept_a", "type": "concept", "label": "Concept A",
             "scope": "assessed_content"},
            {"id": "concept_b", "type": "concept", "label": "Concept B",
             "scope": "supporting_not_assessed"},
            {"id": "err_pat", "type": "possible_error_pattern",
             "label": "Possible approach: example",
             "prevalence": "not_established",
             "interpretation_scope":
                 "hypothetical_approach_not_observed_cause"}],
        "exercises": [{
            "id": "ex1", "type": "exercise", "source_id": sources[0]["id"],
            "position": 1, "role": sources[0]["role"],
            "skill_id": "skill_one", "concept_id": "concept_a",
            "question": make_v2_question(),
            "status": "content_mapping_draft_pending_educator_review",
            "exercise_format": "short_verbal_prompt",
            "mathematical_task": "Add the two stated integers.",
            "additional_task_demands": [{
                "id": "read_short_prompt",
                "description": "Read the short prompt.",
                "scope": "task_requirement_not_measured_skill"}],
            "interpretation_boundary": descriptions.TASK_BOUNDARY}],
        "content_edges": [
            {"source": "ex1", "target": "concept_a", "relation": "assesses",
             "status": "content_mapping_draft_pending_educator_review"},
            {"source": "concept_a", "target": "skill_one",
             "relation": "is_part_of",
             "status": "content_mapping_draft_pending_educator_review"}],
        "pedagogical_annotations": [
            {"id": "support_concept_b_to_concept_a",
             "kind": "proposed_support_link", "source": "concept_b",
             "target": "concept_a", "rationale": "Test rationale.",
             "review_status": "pending", "reviewer_id": None,
             "review_notes": None, "used_for_routing": False,
             "used_for_student_claims": False,
             "parent_annotation_id": "prerequisite_concept_b_to_concept_a",
             "support_type": "conceptual_support",
             "interpretation_boundary": descriptions.SUPPORT_BOUNDARY},
            {"id": "error_example_err_pat", "kind": "possible_error_example",
             "source": "ex1", "target": "err_pat", "option_index": 1,
             "option_text": "3", "rationale": "Consistent with a guess.",
             "alternative_explanations": ["guessing"],
             "review_status": "pending", "reviewer_id": None,
             "review_notes": None, "used_for_routing": False,
             "used_for_student_claims": False,
             "interpretation_boundary": descriptions.PATTERN_BOUNDARY}]}


class V2ValidationTests(unittest.TestCase):
    def test_valid_v2_graph_passes(self):
        self.assertTrue(rcg.validate_graph(make_v2_graph()))

    def test_v1_graph_still_validated(self):
        from tests.test_research_content_graph import make_graph
        self.assertTrue(rcg.validate_graph(make_graph()))

    def test_invalid_format_rejected(self):
        graph = make_v2_graph()
        graph["exercises"][0]["exercise_format"] = "essay"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_malformed_task_demands_rejected(self):
        for demands in (
                [{"id": "d1", "description": "x"}],
                [{"id": "d1", "description": "x",
                  "scope": "measured_skill"}],
                [{"id": "d1", "description": "",
                  "scope": "task_requirement_not_measured_skill"}],
                [{"id": "d1", "description": "a",
                  "scope": "task_requirement_not_measured_skill"},
                 {"id": "d1", "description": "b",
                  "scope": "task_requirement_not_measured_skill"}]):
            graph = make_v2_graph()
            graph["exercises"][0]["additional_task_demands"] = demands
            with self.assertRaises(ValueError):
                rcg.validate_graph(graph)

    def test_word_problem_needs_demands(self):
        graph = make_v2_graph()
        graph["exercises"][0]["exercise_format"] = "word_problem"
        graph["exercises"][0]["additional_task_demands"] = []
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_injected_score_or_diagnosis_fields_rejected(self):
        for key in ("reading_score", "student_diagnosis",
                    "confidence_estimate"):
            graph = make_v2_graph()
            graph["exercises"][0][key] = 0.5
            with self.assertRaises(ValueError):
                rcg.validate_graph(graph)

    def test_unsupported_link_or_causal_scope_rejected(self):
        graph = make_v2_graph()
        graph["pedagogical_annotations"][0]["kind"] = "proposed_prerequisite"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)
        graph = make_v2_graph()
        graph["pedagogical_annotations"][0]["support_type"] = "required_order"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)
        graph = make_v2_graph()
        graph["pedagogical_annotations"][0]["parent_annotation_id"] = ""
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)
        graph = make_v2_graph()
        graph["nodes"][3]["interpretation_scope"] = "observed_cause"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_bad_boundaries_and_root_rejected(self):
        for mutate in (
                lambda g: g.update(description_boundary="other"),
                lambda g: g["generation_scope"].update(
                    student_diagnosis_permitted=True),
                lambda g: g["review_input"].update(
                    type="educator_validation"),
                lambda g: g["review_input"].update(
                    educator_reviews_completed=1),
                lambda g: g["exercises"][0].update(
                    interpretation_boundary="other"),
                lambda g: g["pedagogical_annotations"][0].update(
                    interpretation_boundary="other"),
                lambda g: g["pedagogical_annotations"][1].update(
                    interpretation_boundary="other")):
            graph = make_v2_graph()
            mutate(graph)
            with self.assertRaises(ValueError):
                rcg.validate_graph(graph)

    def test_active_or_reviewed_annotation_rejected(self):
        for mutate in (
                lambda a: a.update(review_status="approved"),
                lambda a: a.update(used_for_routing=True),
                lambda a: a.update(reviewer_id="r1")):
            graph = make_v2_graph()
            mutate(graph["pedagogical_annotations"][0])
            with self.assertRaises(ValueError):
                rcg.validate_graph(graph)


class V2RenderTests(unittest.TestCase):
    def test_escapes_all_v2_strings(self):
        graph = make_v2_graph()
        graph["exercises"][0]["mathematical_task"] = "<img src=x>"
        graph["exercises"][0]["additional_task_demands"][0][
            "description"] = "<b>bad</b>"
        graph["pedagogical_annotations"][0]["support_type"] = \
            "conceptual_support"
        page = rcg.render_graph(graph)
        self.assertNotIn("<img", page)
        self.assertIn("&lt;img", page)
        self.assertIn("&lt;b&gt;bad&lt;/b&gt;", page)

    def test_v2_sections_and_scope_notice(self):
        page = rcg.render_graph(make_v2_graph())
        self.assertIn(
            "Proposed procedural/conceptual connections - "
            "not a learning order", page)
        self.assertIn(
            "Illustrative option interpretations - not diagnoses", page)
        self.assertIn("conceptual_support", page)
        self.assertIn("Add the two stated integers.", page)
        self.assertIn(descriptions.TASK_BOUNDARY, page)
        self.assertIn(
            "Aitta remains limited to the feedback opening sentence; "
            "this graph is not connected to the provider.", page)
        self.assertIn("no student diagnosis", page)

    def test_boundary_shown_with_empty_demands(self):
        graph = make_v2_graph()
        graph["exercises"][0]["additional_task_demands"] = []
        page = rcg.render_graph(graph)
        self.assertIn(descriptions.TASK_BOUNDARY, page)
        self.assertNotIn("demands", page.split("</main>")[0].split(
            "correct option")[1])
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)


class V2CaptureTests(unittest.TestCase):
    def _fixture(self, tmp):
        graph = make_v2_graph(source_file={
            "id": "tiny_bank", "file": "tiny_bank_private.json",
            "sha256": "pending", "role": "assessment"})
        question = copy.deepcopy(make_v2_question())
        source_raw = json.dumps({"questions": [question]}).encode("utf-8")
        source_hash = hashlib.sha256(source_raw).hexdigest()
        graph["sources"][0]["sha256"] = source_hash
        (tmp / "tiny_bank_private.json").write_bytes(source_raw)
        graph["exercises"][0]["question"] = question
        raw = json.dumps(graph).encode("utf-8")
        (tmp / "content_graph_private.json").write_bytes(raw)
        parent = (tmp / "parent_v1.json")
        parent_raw = b"parent-bytes"
        parent.write_bytes(parent_raw)
        (tmp / "catalog_module.py").write_bytes(b"catalog-bytes")
        (tmp / "desc_module.py").write_bytes(b"desc-bytes")
        manifest = {
            "graph_sha256": hashlib.sha256(raw).hexdigest(),
            "authoring_module": rcg.DESCRIPTIONS_FILENAME,
            "authoring_module_sha256":
                hashlib.sha256(b"desc-bytes").hexdigest(),
            "parent_graph_sha256":
                hashlib.sha256(parent_raw).hexdigest(),
            "parent_authoring_module_sha256":
                hashlib.sha256(b"catalog-bytes").hexdigest(),
            "exercise_counts": {"tiny_bank": 1}}
        (tmp / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        patches = mock.patch.multiple(
            rcg,
            DESCRIPTIONS_PATH=tmp / "desc_module.py",
            CATALOG_PATH=tmp / "catalog_module.py",
            V1_PARENT_CAPTURE=parent)
        patches.start()
        self.addCleanup(patches.stop)
        cat = mock.patch.multiple(
            catalog, CAPTURE=tmp,
            SOURCE_HASHES={"tiny_bank_private.json": source_hash})
        cat.start()
        self.addCleanup(cat.stop)
        desc = mock.patch.object(
            descriptions, "PARENT_HASH",
            hashlib.sha256(parent_raw).hexdigest())
        desc.start()
        self.addCleanup(desc.stop)
        return tmp, manifest

    def _rewrite_manifest(self, tmp, manifest, **changes):
        manifest = dict(manifest, **changes)
        (tmp / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8")

    def test_synthetic_v2_capture_loads(self):
        with tempfile.TemporaryDirectory() as d:
            tmp, _ = self._fixture(Path(d))
            graph = rcg.load_capture(tmp)
            self.assertEqual(graph["schema"], "phase3_bounded_content_graph_v2")

    def test_authoring_module_name_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            tmp, manifest = self._fixture(Path(d))
            self._rewrite_manifest(tmp, manifest,
                                   authoring_module="other.py")
            with self.assertRaises(SystemExit):
                rcg.load_capture(tmp)

    def test_authoring_module_hash_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            tmp, manifest = self._fixture(Path(d))
            self._rewrite_manifest(
                tmp, manifest, authoring_module_sha256="0" * 64)
            with self.assertRaises(SystemExit):
                rcg.load_capture(tmp)

    def test_parent_hash_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            tmp, manifest = self._fixture(Path(d))
            self._rewrite_manifest(
                tmp, manifest, parent_graph_sha256="0" * 64)
            with self.assertRaises(SystemExit):
                rcg.load_capture(tmp)

    def test_parent_catalog_hash_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            tmp, manifest = self._fixture(Path(d))
            self._rewrite_manifest(
                tmp, manifest, parent_authoring_module_sha256="0" * 64)
            with self.assertRaises(SystemExit):
                rcg.load_capture(tmp)


if __name__ == "__main__":
    unittest.main()
