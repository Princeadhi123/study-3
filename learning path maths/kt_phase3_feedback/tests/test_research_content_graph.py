"""Structural tests for the bounded content graph renderer/validator.

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
import research_content_graph as rcg


def make_question():
    return {"question_id": "ex1", "skill_id": "skill_one",
            "item_id": "i1", "exercise_id": "e1", "text": "1 + 1?",
            "options": ["2", "3"], "answer_index": 0,
            "content_text": "1 + 1? [OPTIONS] 2 | 3"}


def make_graph(source_file=None):
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
        "schema": "phase3_bounded_content_graph_v1",
        "scope": "offline_research_only_not_learner_approved",
        "status": "draft_pending_educator_review",
        "used_for_student_advice": False,
        "claim_boundary":
            "descriptive_content_map_not_mastery_or_misconception_diagnosis",
        "sources": sources,
        "nodes": [
            {"id": "skill_one", "type": "skill", "label": "Skill one"},
            {"id": "concept_a", "type": "concept", "label": "Concept A",
             "scope": "assessed_content"},
            {"id": "concept_b", "type": "concept", "label": "Concept B",
             "scope": "supporting_not_assessed"},
            {"id": "err_pat", "type": "possible_error_pattern",
             "label": "Example pattern", "prevalence": "not_established"}],
        "exercises": [{
            "id": "ex1", "type": "exercise", "source_id": sources[0]["id"],
            "position": 1, "role": sources[0]["role"],
            "skill_id": "skill_one", "concept_id": "concept_a",
            "question": make_question(),
            "status": "content_mapping_draft_pending_educator_review"}],
        "content_edges": [
            {"source": "ex1", "target": "concept_a", "relation": "assesses",
             "status": "content_mapping_draft_pending_educator_review"},
            {"source": "concept_a", "target": "skill_one",
             "relation": "is_part_of",
             "status": "content_mapping_draft_pending_educator_review"}],
        "pedagogical_annotations": [
            {"id": "prerequisite_concept_b_to_concept_a",
             "kind": "proposed_prerequisite", "source": "concept_b",
             "target": "concept_a", "rationale": "Test rationale.",
             "review_status": "pending", "reviewer_id": None,
             "review_notes": None, "used_for_routing": False,
             "used_for_student_claims": False},
            {"id": "error_example_err_pat", "kind": "possible_error_example",
             "source": "ex1", "target": "err_pat", "option_index": 1,
             "option_text": "3", "rationale": "Test.",
             "alternative_explanations": ["guessing"],
             "review_status": "pending", "reviewer_id": None,
             "review_notes": None, "used_for_routing": False,
             "used_for_student_claims": False}]}


def add_assessed_concept(graph, cid="concept_c", parent="skill_one"):
    graph["nodes"].append({"id": cid, "type": "concept", "label": "C",
                           "scope": "assessed_content"})
    graph["content_edges"].append(
        {"source": cid, "target": parent, "relation": "is_part_of",
         "status": "content_mapping_draft_pending_educator_review"})


class ValidationTests(unittest.TestCase):
    def test_valid_graph_passes(self):
        self.assertTrue(rcg.validate_graph(make_graph()))

    def test_duplicate_node_or_exercise_id_rejected(self):
        graph = make_graph()
        graph["nodes"].append(copy.deepcopy(graph["nodes"][0]))
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_missing_assesses_mapping_rejected(self):
        graph = make_graph()
        graph["content_edges"] = [
            e for e in graph["content_edges"] if e["relation"] != "assesses"]
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_extra_different_assesses_target_rejected(self):
        graph = make_graph()
        add_assessed_concept(graph)
        graph["content_edges"].append(
            {"source": "ex1", "target": "concept_c", "relation": "assesses",
             "status": "content_mapping_draft_pending_educator_review"})
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_duplicate_edge_rejected(self):
        graph = make_graph()
        graph["content_edges"].append(
            copy.deepcopy(graph["content_edges"][0]))
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_wrong_skill_parent_rejected(self):
        graph = make_graph()
        for e in graph["content_edges"]:
            if e["relation"] == "is_part_of":
                e["target"] = "concept_b"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_extra_skill_parent_rejected(self):
        graph = make_graph()
        graph["nodes"].append(
            {"id": "skill_two", "type": "skill", "label": "Skill two"})
        graph["content_edges"].append(
            {"source": "concept_a", "target": "skill_two",
             "relation": "is_part_of",
             "status": "content_mapping_draft_pending_educator_review"})
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_exercise_skill_ownership_mismatch_rejected(self):
        graph = make_graph()
        graph["nodes"].append(
            {"id": "skill_two", "type": "skill", "label": "Skill two"})
        graph["exercises"][0]["skill_id"] = "skill_two"
        graph["exercises"][0]["question"]["skill_id"] = "skill_two"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_supporting_concept_with_parent_rejected(self):
        graph = make_graph()
        graph["content_edges"].append(
            {"source": "concept_b", "target": "skill_one",
             "relation": "is_part_of",
             "status": "content_mapping_draft_pending_educator_review"})
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_assesses_to_supporting_concept_rejected(self):
        graph = make_graph()
        graph["content_edges"][0]["target"] = "concept_b"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_unknown_edge_endpoint_rejected(self):
        graph = make_graph()
        graph["content_edges"][0]["target"] = "ghost"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_prerequisite_in_content_edges_rejected(self):
        graph = make_graph()
        graph["content_edges"][0]["relation"] = "prerequisite"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_malformed_edge_rejected(self):
        graph = make_graph()
        graph["content_edges"].append({"source": "ex1"})
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_exercise_role_source_mismatch_rejected(self):
        graph = make_graph()
        graph["exercises"][0]["source_id"] = "practice_pool"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_embedded_identity_mismatch_rejected(self):
        graph = make_graph()
        graph["exercises"][0]["question"]["question_id"] = "other"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_non_draft_exercise_status_rejected(self):
        graph = make_graph()
        graph["exercises"][0]["status"] = "approved"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_noncontiguous_positions_rejected(self):
        graph = make_graph()
        second = copy.deepcopy(graph["exercises"][0])
        second["id"] = "ex2"
        second["question"]["question_id"] = "ex2"
        second["position"] = 3
        graph["exercises"].append(second)
        graph["content_edges"].append(
            {"source": "ex2", "target": "concept_a", "relation": "assesses",
             "status": "content_mapping_draft_pending_educator_review"})
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_bad_options_rejected(self):
        for mutate in (
                lambda o: o.clear(),
                lambda o: o.append("2"),
                lambda o: o.append("")):
            graph = make_graph()
            options = graph["exercises"][0]["question"]["options"]
            mutate(options)
            q = graph["exercises"][0]["question"]
            q["content_text"] = q["text"] + " [OPTIONS] " + " | ".join(options)
            with self.assertRaises(ValueError):
                rcg.validate_graph(graph)

    def test_pattern_prevalence_rejected(self):
        graph = make_graph()
        graph["nodes"][3]["prevalence"] = "common"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_empty_rationale_or_alternatives_rejected(self):
        for ann_index, key, value in (
                (0, "rationale", ""), (1, "alternative_explanations", []),
                (1, "alternative_explanations", [""])):
            graph = make_graph()
            graph["pedagogical_annotations"][ann_index][key] = value
            with self.assertRaises(ValueError):
                rcg.validate_graph(graph)

    def test_answer_key_error_annotation_rejected(self):
        graph = make_graph()
        for ann in graph["pedagogical_annotations"]:
            if ann["kind"] == "possible_error_example":
                ann["option_index"] = 0
                ann["option_text"] = "2"
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)

    def test_active_or_reviewed_annotation_rejected(self):
        for mutate in (
                lambda a: a.update(review_status="approved"),
                lambda a: a.update(used_for_routing=True),
                lambda a: a.update(used_for_student_claims=True),
                lambda a: a.update(reviewer_id="r1")):
            graph = make_graph()
            mutate(graph["pedagogical_annotations"][0])
            with self.assertRaises(ValueError):
                rcg.validate_graph(graph)

    def test_global_flags_rejected(self):
        graph = make_graph()
        graph["used_for_student_advice"] = True
        with self.assertRaises(ValueError):
            rcg.validate_graph(graph)


class RenderTests(unittest.TestCase):
    def test_escapes_all_content(self):
        graph = make_graph()
        graph["nodes"][0]["label"] = "<script>alert(1)</script>"
        graph["pedagogical_annotations"][0]["rationale"] = "<b>x</b>"
        page = rcg.render_graph(graph)
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)

    def test_offline_no_external_resources(self):
        page = rcg.render_graph(make_graph())
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("cdn", page.lower())
        self.assertIn("Bounded content graph", page)
        self.assertIn("correct option 1: 2", page)
        self.assertIn("ex1", page)


class CaptureTests(unittest.TestCase):
    def _tiny_fixture(self, tmp):
        graph = make_graph(source_file={
            "id": "tiny_bank", "file": "tiny_bank_private.json",
            "sha256": "pending", "role": "assessment"})
        question = copy.deepcopy(make_question())
        source_raw = json.dumps({"questions": [question]}).encode("utf-8")
        source_hash = hashlib.sha256(source_raw).hexdigest()
        graph["sources"][0]["sha256"] = source_hash
        (tmp / "tiny_bank_private.json").write_bytes(source_raw)
        graph["exercises"][0]["question"] = question
        raw = json.dumps(graph).encode("utf-8")
        (tmp / "content_graph_private.json").write_bytes(raw)
        module_sha = hashlib.sha256(
            rcg.CATALOG_PATH.read_bytes()).hexdigest()
        (tmp / "manifest.json").write_text(json.dumps({
            "graph_sha256": hashlib.sha256(raw).hexdigest(),
            "authoring_module_sha256": module_sha,
            "exercise_counts": {"tiny_bank": 1}}), encoding="utf-8")
        return source_hash

    def _patched(self, tmp, source_hash):
        return mock.patch.multiple(
            catalog,
            CAPTURE=tmp,
            SOURCE_HASHES={"tiny_bank_private.json": source_hash})

    def test_synthetic_capture_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source_hash = self._tiny_fixture(tmp)
            with self._patched(tmp, source_hash):
                graph = rcg.load_capture(tmp)
            self.assertEqual(graph["exercises"][0]["id"], "ex1")

    def test_manifest_graph_hash_mismatch_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source_hash = self._tiny_fixture(tmp)
            manifest = json.loads(
                (tmp / "manifest.json").read_text(encoding="utf-8"))
            manifest["graph_sha256"] = "0" * 64
            (tmp / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8")
            with self._patched(tmp, source_hash):
                with self.assertRaises(SystemExit):
                    rcg.load_capture(tmp)

    def test_authoring_module_hash_mismatch_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source_hash = self._tiny_fixture(tmp)
            manifest = json.loads(
                (tmp / "manifest.json").read_text(encoding="utf-8"))
            manifest["authoring_module_sha256"] = "0" * 64
            (tmp / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8")
            with self._patched(tmp, source_hash):
                with self.assertRaises(SystemExit):
                    rcg.load_capture(tmp)

    def test_source_hash_mismatch_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source_hash = self._tiny_fixture(tmp)
            with self._patched(tmp, "0" * 64):
                with self.assertRaises(SystemExit):
                    rcg.load_capture(tmp)
            self.assertTrue(source_hash)

    def test_embedded_record_mismatch_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source_hash = self._tiny_fixture(tmp)
            graph = json.loads(
                (tmp / "content_graph_private.json").read_text(
                    encoding="utf-8"))
            graph["exercises"][0]["question"]["options"][0] = "changed"
            raw = json.dumps(graph).encode("utf-8")
            (tmp / "content_graph_private.json").write_bytes(raw)
            manifest = json.loads(
                (tmp / "manifest.json").read_text(encoding="utf-8"))
            manifest["graph_sha256"] = hashlib.sha256(raw).hexdigest()
            (tmp / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8")
            with self._patched(tmp, source_hash):
                with self.assertRaises(SystemExit):
                    rcg.load_capture(tmp)

    def test_existing_output_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "index.html"
            target.write_text("occupied", encoding="utf-8")
            with self.assertRaises(SystemExit):
                rcg.main(["--output", str(target)])


if __name__ == "__main__":
    unittest.main()
