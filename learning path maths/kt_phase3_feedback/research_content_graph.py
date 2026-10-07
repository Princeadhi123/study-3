"""Validate and render the frozen bounded content graphs for educators.

Offline presentation and structural checking only. Supports the v1 graph,
the v2 descriptive-task graph and the v3 24-question practice capture; reads
each frozen capture plus manifest, verifies graph, authoring-module, parent
and source hashes and embedded exercise records before rendering. No model,
provider, network or mapping regeneration. Never mutates captures.
"""
import argparse
import hashlib
import html
import json
from pathlib import Path

import research_content_catalog as catalog
import research_content_descriptions as descriptions
import research_content_descriptions_v3 as descriptions_v3

ROOT = Path(__file__).resolve().parent
V1_CAPTURE = ROOT / "artifacts" / "bounded_content_graph_20261006"
DEFAULT_CAPTURE = ROOT / "artifacts" / "bounded_content_graph_v3_20261007"
CATALOG_PATH = ROOT / "research_content_catalog.py"
DESCRIPTIONS_PATH = ROOT / "research_content_descriptions.py"
DESCRIPTIONS_FILENAME = "research_content_descriptions.py"
V3_AUTHORING_PATH = ROOT / "research_content_descriptions_v3.py"
V3_AUTHORING_FILENAME = "research_content_descriptions_v3.py"
V1_PARENT_CAPTURE = V1_CAPTURE / "content_graph_private.json"
V2_PARENT_CAPTURE = (ROOT / "artifacts" / "bounded_content_graph_v2_20261006" /
                     "content_graph_private.json")

V1_SCHEMA = "phase3_bounded_content_graph_v1"
V2_SCHEMA = "phase3_bounded_content_graph_v2"
V3_SCHEMA = "phase3_bounded_content_graph_v3"
GRAPH_SCHEMAS = {V1_SCHEMA, V2_SCHEMA, V3_SCHEMA}
GRAPH_SCOPE = "offline_research_only_not_learner_approved"
GRAPH_STATUS = "draft_pending_educator_review"
CLAIM_BOUNDARY = "descriptive_content_map_not_mastery_or_misconception_diagnosis"
DRAFT = "content_mapping_draft_pending_educator_review"
NODE_TYPES = {"skill", "concept", "possible_error_pattern"}
SOURCE_ROLES = {"assessment", "practice"}
RELATIONS = {"assesses", "is_part_of"}
V1_ANNOTATION_KINDS = {"proposed_prerequisite", "possible_error_example"}
V2_ANNOTATION_KINDS = {"proposed_support_link", "possible_error_example"}
LINK_KINDS = {"proposed_prerequisite", "proposed_support_link"}
V1_EXERCISE_KEYS = {"id", "type", "source_id", "position", "role",
                    "skill_id", "concept_id", "question", "status"}
V2_EXERCISE_KEYS = V1_EXERCISE_KEYS | {
    "exercise_format", "mathematical_task",
    "additional_task_demands", "interpretation_boundary"}
EXERCISE_FORMATS = {"short_verbal_prompt", "expression_with_instruction",
                    "symbolic_expression", "word_problem"}
SUPPORT_TYPES = {"standard_procedure_component", "conceptual_support"}
DEMAND_SCOPE = "task_requirement_not_measured_skill"
PATTERN_SCOPE = "hypothetical_approach_not_observed_cause"
GENERATION_SCOPE_KEYS = {"provider_authority_changed",
                         "student_diagnosis_permitted",
                         "reading_skill_score_permitted",
                         "unreviewed_routing_permitted"}
REVIEW_INPUT_TYPE = "automated_LLM_review_not_independent_educator_validation"


def _fail(message):
    raise ValueError("invalid content graph: " + message)


def _nonempty_str(value):
    return isinstance(value, str) and bool(value)


def expected_sources(manifest_sources=None):
    """Expected source rows from a manifest or the original catalog bundle."""
    if manifest_sources:
        return [{"id": row["id"], "file": row["file"],
                 "sha256": row["sha256"], "role": row["role"]}
                for row in manifest_sources]
    rows = []
    for filename, digest in catalog.SOURCE_HASHES.items():
        source_id = filename.removesuffix("_private.json")
        role = "practice" if filename.startswith("practice") else "assessment"
        rows.append({"id": source_id, "file": filename, "sha256": digest,
                     "role": role})
    return rows


def _validate_v2_root(graph):
    if graph.get("description_boundary") != descriptions.TASK_BOUNDARY:
        _fail("description_boundary")
    scope = graph.get("generation_scope")
    if (not isinstance(scope, dict) or set(scope) != GENERATION_SCOPE_KEYS
            or any(v is not False for v in scope.values())):
        _fail("generation_scope")
    review = graph.get("review_input")
    if (not isinstance(review, dict)
            or review.get("type") != REVIEW_INPUT_TYPE
            or not _nonempty_str(review.get("source"))
            or review.get("model_version") is not None
            or review.get("educator_reviews_completed") != 0):
        _fail("review_input")


def _validate_v2_exercise(ex):
    if set(ex) != V2_EXERCISE_KEYS:
        _fail("exercise v2 fields")
    if ex["exercise_format"] not in EXERCISE_FORMATS:
        _fail("exercise format")
    if not _nonempty_str(ex["mathematical_task"]):
        _fail("mathematical_task")
    if ex["interpretation_boundary"] != descriptions.TASK_BOUNDARY:
        _fail("exercise interpretation_boundary")
    demands = ex["additional_task_demands"]
    if not isinstance(demands, list):
        _fail("additional_task_demands")
    demand_ids = set()
    for demand in demands:
        if (not isinstance(demand, dict)
                or set(demand) != {"id", "description", "scope"}
                or not _nonempty_str(demand["id"])
                or not _nonempty_str(demand["description"])
                or demand["scope"] != DEMAND_SCOPE
                or demand["id"] in demand_ids):
            _fail("task demand entry")
        demand_ids.add(demand["id"])
    if ex["exercise_format"] == "word_problem" and not demands:
        _fail("word_problem needs task demands")


def validate_graph(graph):
    if not isinstance(graph, dict):
        _fail("graph must be an object")
    version = graph.get("schema")
    if version not in GRAPH_SCHEMAS:
        _fail("schema")
    is_v2 = version in {V2_SCHEMA, V3_SCHEMA}
    if graph.get("scope") != GRAPH_SCOPE:
        _fail("scope")
    if graph.get("status") != GRAPH_STATUS:
        _fail("status")
    if graph.get("claim_boundary") != CLAIM_BOUNDARY:
        _fail("claim_boundary")
    if graph.get("used_for_student_advice") is not False:
        _fail("used_for_student_advice must be false")
    if is_v2:
        _validate_v2_root(graph)

    sources = graph.get("sources")
    if not isinstance(sources, list) or not sources:
        _fail("sources")
    source_roles = {}
    for source in sources:
        if (not isinstance(source, dict)
                or set(source) != {"id", "file", "sha256", "role"}
                or not _nonempty_str(source.get("id"))
                or not _nonempty_str(source.get("file"))
                or source["role"] not in SOURCE_ROLES):
            _fail("source entry")
        if source["id"] in source_roles:
            _fail("duplicate source id")
        source_roles[source["id"]] = source["role"]

    nodes = graph.get("nodes")
    exercises = graph.get("exercises")
    edges = graph.get("content_edges")
    annotations = graph.get("pedagogical_annotations")
    for name, value in (("nodes", nodes), ("exercises", exercises),
                        ("content_edges", edges),
                        ("pedagogical_annotations", annotations)):
        if not isinstance(value, list):
            _fail(name)

    ids = set()
    node_types = {}
    concept_scope = {}
    exercise_ids = set()
    for node in nodes:
        if (not isinstance(node, dict)
                or not _nonempty_str(node.get("id"))
                or node.get("type") not in NODE_TYPES
                or not _nonempty_str(node.get("label"))):
            _fail("node entry")
        if node["id"] in ids:
            _fail("duplicate id " + node["id"])
        ids.add(node["id"])
        node_types[node["id"]] = node["type"]
        if node["type"] == "concept":
            if node.get("scope") not in (
                    "assessed_content", "supporting_not_assessed"):
                _fail("concept scope")
            concept_scope[node["id"]] = node["scope"]
        if node["type"] == "possible_error_pattern":
            if node.get("prevalence") != "not_established":
                _fail("error pattern prevalence must be not_established")
            if (is_v2 and node.get("interpretation_scope")
                    != PATTERN_SCOPE):
                _fail("v2 error pattern interpretation_scope")

    exercise_by_id = {}
    positions = {}
    allowed_keys = V2_EXERCISE_KEYS if is_v2 else V1_EXERCISE_KEYS
    for ex in exercises:
        if (not isinstance(ex, dict) or set(ex) != allowed_keys
                or ex.get("type") != "exercise"
                or not _nonempty_str(ex.get("id"))
                or ex.get("source_id") not in source_roles
                or ex.get("status") != DRAFT
                or type(ex.get("position")) is not int
                or ex["position"] < 1
                or not _nonempty_str(ex.get("skill_id"))
                or not _nonempty_str(ex.get("concept_id"))):
            _fail("exercise entry")
        if is_v2:
            _validate_v2_exercise(ex)
        if ex["id"] in ids:
            _fail("duplicate id " + ex["id"])
        ids.add(ex["id"])
        exercise_ids.add(ex["id"])
        exercise_by_id[ex["id"]] = ex
        if ex.get("role") != source_roles[ex["source_id"]]:
            _fail("exercise role disagrees with source")
        positions.setdefault(ex["source_id"], []).append(ex["position"])
        question = ex.get("question")
        if not isinstance(question, dict):
            _fail("exercise question")
        options = question.get("options")
        if (question.get("question_id") != ex["id"]
                or question.get("skill_id") != ex["skill_id"]
                or not _nonempty_str(question.get("text"))
                or not isinstance(options, list) or len(options) < 2
                or not all(_nonempty_str(o) for o in options)
                or len(set(options)) != len(options)
                or type(question.get("answer_index")) is not int
                or question["answer_index"] not in range(len(options))):
            _fail("exercise question fields")
        if question.get("content_text") != (
                question["text"] + " [OPTIONS] " + " | ".join(options)):
            _fail("exercise content_text does not match rendered options")
    for source_id, occupied in positions.items():
        if sorted(occupied) != list(range(1, len(occupied) + 1)):
            _fail("exercise positions not contiguous per source")

    assesses_count = {}
    part_of_count = {}
    concept_parent = {}
    seen_edges = set()
    for edge in edges:
        if not isinstance(edge, dict):
            _fail("edge entry")
        if (set(edge) != {"source", "target", "relation", "status"}
                or edge["relation"] not in RELATIONS
                or edge["status"] != DRAFT):
            _fail("edge entry")
        key = (edge["source"], edge["target"], edge["relation"])
        if key in seen_edges:
            _fail("duplicate edge")
        seen_edges.add(key)
        source, target = edge["source"], edge["target"]
        if source not in ids or target not in ids:
            _fail("edge endpoint not in graph")
        if edge["relation"] == "assesses":
            if (source not in exercise_ids
                    or node_types.get(target) != "concept"
                    or concept_scope.get(target) != "assessed_content"):
                _fail("assesses edge must be exercise -> assessed concept")
            assesses_count[source] = assesses_count.get(source, 0) + 1
            if assesses_count[source] > 1:
                _fail("exercise has multiple assesses edges")
            if target != exercise_by_id[source]["concept_id"]:
                _fail("assesses edge disagrees with embedded concept_id")
        else:
            if (concept_scope.get(source) != "assessed_content"
                    or node_types.get(target) != "skill"):
                _fail("is_part_of must be assessed concept -> skill")
            part_of_count[source] = part_of_count.get(source, 0) + 1
            if part_of_count[source] > 1:
                _fail("assessed concept has multiple skill parents")
            concept_parent[source] = target
    for ex in exercises:
        if ex["id"] not in assesses_count:
            _fail("exercise missing assesses edge")
        if concept_parent.get(ex["concept_id"]) != ex["skill_id"]:
            _fail("exercise skill_id disagrees with concept skill parent")
    for concept_id, scope in concept_scope.items():
        owned = concept_id in part_of_count
        if scope == "assessed_content" and not owned:
            _fail("assessed concept missing skill parent")

    annotation_kinds = V2_ANNOTATION_KINDS if is_v2 else V1_ANNOTATION_KINDS
    ann_ids = set()
    for ann in annotations:
        if not isinstance(ann, dict):
            _fail("annotation entry")
        if (ann.get("kind") not in annotation_kinds
                or not _nonempty_str(ann.get("id"))
                or not _nonempty_str(ann.get("rationale"))
                or ann.get("review_status") != "pending"
                or ann.get("reviewer_id") is not None
                or ann.get("review_notes") is not None
                or ann.get("used_for_routing") is not False
                or ann.get("used_for_student_claims") is not False):
            _fail("annotation entry")
        if ann["id"] in ann_ids:
            _fail("duplicate annotation id")
        ann_ids.add(ann["id"])
        if ann["kind"] in LINK_KINDS:
            if (node_types.get(ann.get("source")) != "concept"
                    or node_types.get(ann.get("target")) != "concept"):
                _fail("link endpoints must be concepts")
            if is_v2:
                if (ann.get("support_type") not in SUPPORT_TYPES
                        or not _nonempty_str(
                            ann.get("parent_annotation_id"))
                        or ann.get("interpretation_boundary")
                        != descriptions.SUPPORT_BOUNDARY):
                    _fail("v2 support link fields")
        else:
            exercise = exercise_by_id.get(ann.get("source"))
            if exercise is None:
                _fail("error example source must be an exercise")
            if node_types.get(ann.get("target")) != "possible_error_pattern":
                _fail("error example target must be a pattern node")
            options = exercise["question"]["options"]
            index = ann.get("option_index")
            if (type(index) is not int or index not in range(len(options))
                    or options[index] != ann.get("option_text")):
                _fail("error example option binding")
            if index == exercise["question"]["answer_index"]:
                _fail("error example cannot name the answer key")
            alternatives = ann.get("alternative_explanations")
            if (not isinstance(alternatives, list) or not alternatives
                    or not all(_nonempty_str(a) for a in alternatives)):
                _fail("error example needs alternative explanations")
            if (is_v2 and ann.get("interpretation_boundary")
                    != descriptions.PATTERN_BOUNDARY):
                _fail("v2 error example interpretation_boundary")
    return True


def load_capture(capture):
    """Read a frozen capture; verify hashes, sources, records, counts."""
    capture = Path(capture)
    raw = (capture / "content_graph_private.json").read_bytes()
    manifest = json.loads(
        (capture / "manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(raw).hexdigest() != manifest["graph_sha256"]:
        raise SystemExit("frozen graph hash changed; refusing to render")
    graph = json.loads(raw.decode("utf-8"))

    schema = graph.get("schema")
    if schema == V2_SCHEMA:
        if manifest.get("authoring_module") != DESCRIPTIONS_FILENAME:
            raise SystemExit("unexpected authoring module; refusing")
        module_hash = hashlib.sha256(
            DESCRIPTIONS_PATH.read_bytes()).hexdigest()
        if module_hash != manifest["authoring_module_sha256"]:
            raise SystemExit(
                "authoring module hash changed; refusing to render")
        parent_raw = V1_PARENT_CAPTURE.read_bytes()
        parent_hash = hashlib.sha256(parent_raw).hexdigest()
        if (manifest.get("parent_graph_sha256") != parent_hash
                or parent_hash != descriptions.PARENT_HASH):
            raise SystemExit("parent graph hash changed; refusing")
        catalog_hash = hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest()
        if manifest.get("parent_authoring_module_sha256") != catalog_hash:
            raise SystemExit("parent authoring module hash changed; refusing")
    elif schema == V3_SCHEMA:
        if manifest.get("authoring_module") != V3_AUTHORING_FILENAME:
            raise SystemExit("unexpected v3 authoring module; refusing")
        module_hash = hashlib.sha256(
            V3_AUTHORING_PATH.read_bytes()).hexdigest()
        if module_hash != manifest["authoring_module_sha256"]:
            raise SystemExit(
                "v3 authoring module hash changed; refusing to render")
        parent_raw = V2_PARENT_CAPTURE.read_bytes()
        parent_hash = hashlib.sha256(parent_raw).hexdigest()
        if (manifest.get("parent_graph_sha256") != parent_hash
                or parent_hash != descriptions_v3.PARENT_HASH):
            raise SystemExit("v2 parent graph hash changed; refusing")
        parent_module_hash = hashlib.sha256(
            DESCRIPTIONS_PATH.read_bytes()).hexdigest()
        if manifest.get("parent_authoring_module_sha256") != parent_module_hash:
            raise SystemExit("v2 authoring module hash changed; refusing")
        catalog_hash = hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest()
        if manifest.get("catalog_module_sha256") != catalog_hash:
            raise SystemExit("catalog module hash changed; refusing")
        if manifest.get("source_bundle") != descriptions_v3.SOURCE_BUNDLE:
            raise SystemExit("unexpected v3 source bundle; refusing")
    else:
        module_hash = hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest()
        if module_hash != manifest["authoring_module_sha256"]:
            raise SystemExit(
                "authoring module hash changed; refusing to render")

    manifest_sources = manifest.get("sources")
    if manifest_sources is not None:
        if (not isinstance(manifest_sources, list)
                or any(not isinstance(source, dict)
                       or set(source) != {"id", "file", "path", "sha256",
                                          "role"}
                       for source in manifest_sources)):
            raise SystemExit("malformed manifest source binding")
    expected = {row["id"]: row for row in expected_sources(manifest_sources)}
    actual = {s.get("id"): s for s in graph.get("sources", [])
              if isinstance(s, dict)}
    if actual != expected:
        raise SystemExit("graph source binding mismatch")
    original_questions = {}
    if manifest_sources is not None:
        for source in manifest_sources:
            path = Path(source["path"])
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit("unsafe graph source path: " + source["id"])
            source_raw = (ROOT / path).read_bytes()
            if hashlib.sha256(source_raw).hexdigest() != source["sha256"]:
                raise SystemExit(
                    "frozen graph source changed: " + source["file"])
            original_questions[source["id"]] = json.loads(
                source_raw.decode("utf-8"))["questions"]
    else:
        for filename, expected_digest in catalog.SOURCE_HASHES.items():
            source_raw = (catalog.CAPTURE / filename).read_bytes()
            if hashlib.sha256(source_raw).hexdigest() != expected_digest:
                raise SystemExit("frozen graph source changed: " + filename)
            source_id = filename.removesuffix("_private.json")
            original_questions[source_id] = json.loads(
                source_raw.decode("utf-8"))["questions"]

    counts = {}
    embedded = {}
    for ex in graph["exercises"]:
        counts[ex["source_id"]] = counts.get(ex["source_id"], 0) + 1
        embedded.setdefault(ex["source_id"], []).append(ex)
    if counts != dict(manifest["exercise_counts"]):
        raise SystemExit("exercise counts disagree with frozen manifest")
    for source_id, entries in embedded.items():
        records = [e["question"] for e in sorted(
            entries, key=lambda e: e["position"])]
        if records != original_questions.get(source_id):
            raise SystemExit(
                "embedded exercise records differ from source: " + source_id)
    validate_graph(graph)
    return graph


STYLE = """body { margin:0; background:#faf6ee; color:#222;
font-family:Georgia,"Times New Roman",serif; line-height:1.45; }
header { background:#0f6b66; color:#fff; padding:1.2rem 1.5rem; }
header h1 { margin:0 0 .3rem; font-size:1.35rem; }
header p { margin:.15rem 0; font-size:.92rem; }
main { max-width:1050px; margin:0 auto; padding:1rem 1.5rem 3rem; }
h2 { color:#0f6b66; font-size:1.15rem; border-bottom:2px solid #0f6b66;
padding-bottom:.2rem; margin-top:2rem; }
.skill { background:#fff; border:1px solid #d8d0bd; border-radius:6px;
padding:.7rem 1rem; margin:1rem 0; }
.concept { margin:.8rem 0 .8rem .6rem; }
.concept > strong { color:#0f6b66; }
.ex { background:#fff; border:1px solid #e0d8c4; border-radius:5px;
margin:.45rem 0 .45rem 1.2rem; padding:.45rem .7rem; font-size:.9rem; }
.ex .src { display:inline-block; background:#e3efec; border-radius:3px;
padding:0 .4rem; font-size:.78rem; color:#0f6b66; margin-right:.4rem; }
.ex .qid { font-family:Consolas,monospace; font-size:.75rem; color:#666;
word-break:break-all; }
.ex .key { color:#8a3030; font-size:.82rem; }
.ex .task { font-size:.85rem; color:#33503f; margin-top:.25rem; }
.ex ol { margin:.25rem 0; padding-left:1.6rem; }
.ex .demands { font-size:.8rem; color:#555; margin:.2rem 0;
padding-left:1.2rem; }
.pending { border:1px dashed #0f6b66; border-radius:6px; padding:.6rem .9rem;
margin:.5rem 0; background:#fff; }
.note { font-size:.85rem; color:#555; }
.warn { background:#f6e3dd; border:1px solid #c98; border-radius:6px;
padding:.6rem .9rem; font-size:.88rem; }
"""


def _e(value):
    return html.escape(str(value), quote=True)


def _exercise_card(ex):
    q = ex["question"]
    options = "".join(
        f"<li>{_e(option)}</li>" for option in q["options"])
    correct = q["options"][q["answer_index"]]
    card = (
        f'<div class="ex"><span class="src">{_e(ex["source_id"])}'
        f' | {_e(ex["role"])} | pos {ex["position"]}</span>'
        f'<span class="qid">{_e(ex["id"])}</span>'
        f'<div>{_e(q["text"])}</div><ol>{options}</ol>'
        f'<div class="key">correct option '
        f'{q["answer_index"] + 1}: {_e(correct)}'
        " (teacher/research only)</div>")
    if "exercise_format" in ex:
        card += (
            f'<div class="task">format: {_e(ex["exercise_format"])}; '
            f'task: {_e(ex["mathematical_task"])}</div>')
        if ex["additional_task_demands"]:
            demands = "".join(
                f'<li>{_e(d["description"])}</li>'
                for d in ex["additional_task_demands"])
            card += f'<ul class="demands">{demands}</ul>'
        card += f'<div class="task">{_e(ex["interpretation_boundary"])}</div>'
    return card + "</div>"


def render_graph(graph):
    """Offline educator-readable content map; every string escaped."""
    is_v2 = graph.get("schema") in {V2_SCHEMA, V3_SCHEMA}
    by_concept = {}
    supporting = []
    skills = {}
    for node in graph["nodes"]:
        if node["type"] == "skill":
            skills[node["id"]] = node["label"]
        elif node["type"] == "concept":
            if node.get("scope") == "assessed_content":
                by_concept[node["id"]] = []
            else:
                supporting.append(node)
    parents = {e["source"]: e["target"] for e in graph["content_edges"]
               if e["relation"] == "is_part_of"}
    for ex in graph["exercises"]:
        by_concept.setdefault(ex["concept_id"], []).append(ex)
    label = {n["id"]: n.get("label", n["id"]) for n in graph["nodes"]}

    skill_blocks = []
    for skill_id, name in skills.items():
        concepts = [cid for cid, parent in parents.items()
                    if parent == skill_id]
        blocks = []
        for cid in sorted(concepts):
            cards = []
            for ex in sorted(by_concept.get(cid, []),
                             key=lambda e: (e["source_id"], e["position"])):
                cards.append(_exercise_card(ex))
            blocks.append(
                f'<div class="concept"><strong>{_e(label[cid])}</strong>'
                f' <span class="note">assessed content, pending review</span>'
                + "".join(cards) + "</div>")
        skill_blocks.append(
            f'<div class="skill"><h3>{_e(name)}</h3>' +
            "".join(blocks) + "</div>")

    supporting_html = "".join(
        f'<div class="pending"><strong>{_e(n["label"])}</strong>'
        ' <span class="note">supporting concept, not directly assessed; '
        "pending educator review</span></div>" for n in supporting)

    exercises_by_id = {e["id"]: e for e in graph["exercises"]}
    links, errors = [], []
    for ann in graph["pedagogical_annotations"]:
        if ann["kind"] in LINK_KINDS:
            extra = ""
            if is_v2:
                extra = (f' [{_e(ann["support_type"])}] '
                         f'{_e(ann["interpretation_boundary"])} ')
            links.append(
                f'<div class="pending"><strong>{_e(label.get(ann["source"], ann["source"]))}'
                f' -> {_e(label.get(ann["target"], ann["target"]))}</strong>'
                f'<div class="note">proposed connection hypothesis - '
                f'{_e(ann["rationale"])} {extra}Status: pending educator '
                "review; not used for routing or student claims.</div></div>")
        else:
            q = exercises_by_id[ann["source"]]["question"]
            options = "".join(
                f'<li>{"<strong>" if i == ann["option_index"] else ""}'
                f'{_e(option)}'
                f'{"</strong>" if i == ann["option_index"] else ""}</li>'
                for i, option in enumerate(q["options"]))
            alternatives = ", ".join(
                _e(a) for a in ann["alternative_explanations"])
            boundary = (" " + _e(ann["interpretation_boundary"])
                        if is_v2 else "")
            errors.append(
                f'<div class="pending"><strong>{_e(label.get(ann["target"], ann["target"]))}</strong>'
                f'<div class="note">Source question (research view): '
                f'{_e(q["text"])}</div><ol>{options}</ol>'
                f'<div class="note">Highlighted example option '
                f'{ann["option_index"] + 1}: {_e(ann["option_text"])}. '
                f'{_e(ann["rationale"])} '
                f'Alternative explanations: {alternatives}.{boundary} '
                "Illustrative hypothesis only - prevalence not established, "
                "not a misconception diagnosis, pending educator review."
                "</div></div>")

    if is_v2:
        link_heading = ("Proposed procedural/conceptual connections - "
                        "not a learning order")
        error_heading = "Illustrative option interpretations - not diagnoses"
        scope_notice = (
            '<p class="warn">This descriptive map reflects automated '
            'review input, not independent educator validation. '
            'Generation scope: provider authority unchanged, no student '
            'diagnosis, no reading-skill score, no unreviewed routing. '
            'Aitta remains limited to the feedback opening sentence; this '
            'graph is not connected to the provider. Zero educator reviews '
            'recorded.</p>')
        if graph.get("schema") == V3_SCHEMA:
            scope_notice += (
                '<p class="warn">Practice source v2 contains 24 questions. '
                'Four are synthetic divisibility questions accepted in user '
                'review and still pending formal educator review; they are '
                'not historical student content.</p>')
    else:
        link_heading = ("Proposed prerequisite links (hypotheses, "
                        "pending review)")
        error_heading = "Possible-error examples (illustrative only)"
        scope_notice = ""

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bounded content graph - research draft</title>
<style>""" + STYLE + """</style></head><body>
<header><h1>Bounded content graph (research draft)</h1>
<p>Descriptive content map for educator review. Not a mastery or
misconception diagnosis; not learner-approved.</p></header>
<main>
""" + scope_notice + """
<p class="warn">Topic headings are broader than the actual exercises:
fraction division, for example, is not necessarily assessed. Supporting
concepts are not assessed content. All annotations are illustrative
pending-review hypotheses; none route content or reach students.</p>
<h2>Assessed concepts by skill</h2>
""" + "".join(skill_blocks) + """
<h2>Supporting concepts (not assessed)</h2>
""" + supporting_html + """
<h2>""" + link_heading + """</h2>
""" + "".join(links) + """
<h2>""" + error_heading + """</h2>
""" + "".join(errors) + """
</main></body></html>
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    capture = Path(args.capture)
    target = Path(args.output) if args.output else capture / "index.html"
    if target.exists():
        raise SystemExit(f"refusing to overwrite existing {target}")
    graph = load_capture(capture)
    target.write_text(render_graph(graph), encoding="utf-8")
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
