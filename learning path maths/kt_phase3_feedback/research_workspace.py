import copy
import hashlib
import json
import re
import secrets
import threading
from collections import Counter
from pathlib import Path

from session_store import utc_now


DEFAULT_REPORT = (Path(__file__).parent / "artifacts" /
                  "integrated_feedback_20261002_hosted_full" / "report.json")
ID_RE = re.compile(r"\A[a-f0-9]{32}\Z")
PROTOCOL = "phase3_blind_draft_comparison_v1"
LIMITATION = (
    "Descriptive educator preferences over chosen synthetic cases, not learning "
    "outcomes or provider superiority. Cases and reused captures are not "
    "independent trials. Prior exposure and writing style may compromise blinding.")


class ResearchConflict(Exception):
    pass


SKILL_LABELS = {"Peruslaskutoimitukset": "Arithmetic", "Hinta": "Prices",
                "Murtoluvut": "Fractions", "Prosenttilaskenta": "Percentages"}
GROUP_LABELS = {"deterministic": "Fixed answer patterns", "seeded": "Sampled answer patterns",
                "subtopic": "Errors in one subtopic", "distractor": "Wrong-option comparisons"}
PROFILE_LABELS = {
    "all_correct": "Every answer correct", "all_incorrect": "Every answer incorrect",
    "first_half_strong": "First 20 correct, last 20 incorrect",
    "first_half_correct_second_half_wrong": "First 20 correct, last 20 incorrect",
    "second_half_strong": "First 20 incorrect, last 20 correct",
    "global_alternating": "Correct and incorrect answers alternate",
    "per_skill_alternating": "Correct and incorrect answers alternate within each skill",
    "alternating": "Correct and incorrect answers alternate within each skill",
    "long_correct_streak": "First 30 correct, last 10 incorrect",
    "long_incorrect_streak": "First 30 incorrect, last 10 correct",
    "all_skills_equal": "Five correct answers in every skill",
    "tied_strongest": "Two skills at 8/10, two at 3/10",
    "tied_weakest": "Two skills at 2/10, two at 8/10",
    "multiple_weak": "Errors in Arithmetic and Prices only",
    "weak_fractions_only": "Errors in Fractions only",
    "stable_strong": "High correct-answer probability",
    "stable_weak": "Low correct-answer probability",
    "weak_fractions": "Lower correct-answer probability in Fractions",
    "learning": "Correct-answer probability increases over time",
    "fatigue": "Correct-answer probability decreases over time",
    "guessing": "Random guessing",
    "wrong_option_1": "Every answer incorrect — wrong-option set A",
    "wrong_option_2": "Every answer incorrect — wrong-option set B"}


def case_display(name, evidence=None):
    evidence = evidence or {}
    match = re.fullmatch(r"(.+)_s(\d+)", name)
    base = match[1] if match else name
    label = PROFILE_LABELS.get(base)
    if label is None:
        label = base
        for row in evidence.get("skills", []):
            label = label.replace(row.get("skill_id", "\0"),
                                  SKILL_LABELS.get(row["skill_name"], row["skill_name"]))
        if label.startswith("only_") and label.endswith("_weak"):
            label = f"Errors in {label[5:-5]} only"
        elif label.startswith("only_") and label.endswith("_strong"):
            label = f"Correct answers in {label[5:-7]} only"
        elif label.startswith("errors_in_"):
            subtopic = next((s["subtopic_name"] for s in evidence.get("subtopics", [])
                            if s.get("subtopic_id") == base[10:]), base[10:].replace("_", " "))
            label = f"Errors in {subtopic} only"
        else:
            label = label.replace("_", " ").capitalize()
    if match:
        variants = ["11", "23", "37", "41", "53"]
        number = variants.index(match[2]) + 1 if match[2] in variants else match[2]
        label += f" · Example {number}"
    halves = evidence.get("halves", [])
    description = (f"First half: {halves[0]['correct']}/{halves[0]['out_of']} correct; "
                   f"second half: {halves[1]['correct']}/{halves[1]['out_of']} correct. " if len(halves) == 2 else "")
    return {"display_name": label, "description": description + "Synthetic response pattern, not a learner diagnosis."}


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def text(value, maximum):
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(c) < 32 and c not in "\n\t" for c in value)):
        raise ValueError("invalid text")
    return value.strip()


def execution_kind(execution):
    if (execution.get("capture_file")
            and execution.get("metadata", {}).get("status") == "completed"):
        return "cached_hosted" if execution.get("reused") else "fresh_hosted"
    status = execution.get("status", "unverified")
    if status == "local_single_candidate":
        return "local_single_candidate"
    if status in ("offline_rules_no_provider", "offline_template_no_provider"):
        return "offline"
    return "fallback_or_unverified"


def observed(evidence):
    def counts(row):
        return {key: row[key] for key in ("correct", "incorrect", "out_of")}

    result = {"total": counts(evidence["total"]),
              "halves": [counts(row) for row in evidence.get("halves", [])]}
    for key in ("skills", "subtopics"):
        result[key] = [{**counts(row), **{
            field: row[field] for field in ("skill_name", "subtopic_name")
            if field in row}} for row in evidence.get(key, [])]
    return result


class ResearchWorkspace:
    def __init__(self, root, report_path=DEFAULT_REPORT):
        self.directory = Path(root) / "comparisons"
        self.report_path = Path(report_path)
        self.lock = threading.RLock()
        self._report = None
        self._cases = {}
        self._sha256 = None

    def _load(self):
        if self._report is not None:
            return
        raw = self.report_path.read_bytes()
        report = json.loads(raw)
        if (not isinstance(report, dict)
                or report.get("schema") != "phase3_evidence_feedback_report_v1"
                or report.get("pipeline_schema") != "phase3_integrated_synthetic_pipeline_v1"
                or report.get("status") != "draft_not_for_learner_delivery"
                or not isinstance(report.get("scenarios"), list)
                or not report["scenarios"]
                or not isinstance(report.get("source"), dict)
                or not isinstance(report.get("policy"), dict)):
            raise ValueError("unsupported replay report")
        cases, names = {}, set()
        for case in report["scenarios"]:
            name = text(case["name"], 200)
            text(case["group"], 200)
            if not name or name in names:
                raise ValueError("duplicate or empty scenario")
            names.add(name)
            pairs = set()
            for package in case["packages"]:
                pair = (package["checkpoint"], package["audience"])
                if pair in pairs or pair not in {
                        ("midpoint", "student"), ("end", "student"), ("end", "teacher")}:
                    raise ValueError("invalid package coverage")
                pairs.add(pair)
                review = package["review"]
                for key in ("baseline_candidate_id", "selected_candidate_id"):
                    text(review[key], 300)
                if not isinstance(review["trace"], dict):
                    raise ValueError("invalid trace")
                for key in ("selector_execution", "generator_execution"):
                    execution = package.get(key, {})
                    if (not isinstance(execution, dict)
                            or not isinstance(execution.get("metadata", {}), dict)):
                        raise ValueError("invalid execution record")
                for key in ("template_baseline", "message"):
                    if not text(review[key]["text"], 30000):
                        raise ValueError("empty message")
                if pair[0] == "end":
                    evidence = review["sanitized_evidence"]
                    if evidence.get("data_origin") != "synthetic":
                        raise ValueError("synthetic evidence required")
                    clean = observed(evidence)
                    for row in [clean["total"], *clean["halves"],
                                *clean["skills"], *clean["subtopics"]]:
                        if (any(type(row[k]) is not int or row[k] < 0
                                for k in ("correct", "incorrect", "out_of"))
                                or row["correct"] + row["incorrect"] != row["out_of"]):
                            raise ValueError("invalid observed counts")
                    if (clean["total"]["out_of"] != 40
                            or len(clean["skills"]) != 4 or len(clean["halves"]) != 2):
                        raise ValueError("invalid fixed assessment coverage")
                    for group in ("skills", "halves", "subtopics"):
                        if clean[group] and any(sum(row[key] for row in clean[group])
                                                != clean["total"][key] for key in
                                                ("correct", "incorrect", "out_of")):
                            raise ValueError("inconsistent aggregate evidence")
            if not {("end", "student"), ("end", "teacher")}.issubset(pairs):
                raise ValueError("missing end package")
            cases[digest(name)[:32]] = case
        self._cases = cases
        self._sha256 = hashlib.sha256(raw).hexdigest()
        self._report = report

    def library(self):
        with self.lock:
            try:
                self._load()
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                return {"status": "unavailable", "reason": "saved_replay_missing_or_invalid",
                        "scenarios": []}
            rows, end_packages = [], []
            for case_id, case in self._cases.items():
                packages = [p for p in case["packages"] if p["checkpoint"] == "end"]
                end_packages.extend(packages)
                evidence = observed(packages[0]["review"]["sanitized_evidence"])
                rows.append({"id": case_id, "name": case["name"], "group": case["group"],
                             **case_display(case["name"], packages[0]["review"]["sanitized_evidence"]),
                             "group_label": GROUP_LABELS.get(case["group"], case["group"].replace("_", " ")),
                             "total": evidence["total"], "halves": evidence["halves"],
                             "skills": evidence["skills"],
                             "selection_differences": sum(
                                 p["review"]["baseline_candidate_id"] !=
                                 p["review"]["selected_candidate_id"] for p in packages)})
            differences = sum(row["selection_differences"] for row in rows)
            return {"status": "ready", "report_sha256": self._sha256,
                    "source": copy.deepcopy(self._report["source"]),
                    "policy": copy.deepcopy(self._report["policy"]),
                    "diagnostic_provenance": copy.deepcopy(self._report.get("diagnostic_provenance", {})),
                    "summary": {
                        "scenarios": len(rows), "end_packages": len(end_packages),
                        "selection_matches": len(end_packages) - differences,
                        "selection_differences": differences,
                        "selector_execution": dict(Counter(execution_kind(
                            p.get("selector_execution", {})) for p in end_packages)),
                        "fallback_packages": sum(bool(p["review"]["trace"].get(
                            "fallback_reason")) for p in end_packages)},
                    "educational_effectiveness_tested": False,
                    "limitation": LIMITATION, "scenarios": rows}

    def scenario(self, case_id):
        with self.lock:
            self._load()
            if case_id not in self._cases:
                raise FileNotFoundError("unknown scenario")
            return {"id": case_id, "report_sha256": self._sha256,
                    **copy.deepcopy(self._cases[case_id])}

    def _path(self, comparison_id):
        if not isinstance(comparison_id, str) or not ID_RE.fullmatch(comparison_id):
            raise ValueError("invalid comparison id")
        return self.directory / f"{comparison_id}.json"

    def _read(self, comparison_id):
        record = json.loads(self._path(comparison_id).read_text(encoding="utf-8"))
        if record.get("protocol") != PROTOCOL or record.get("id") != comparison_id:
            raise ValueError("invalid comparison record")
        return record

    def _save(self, record):
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._path(record["id"])
        temporary = target.with_suffix(f".{secrets.token_hex(6)}.tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def create_comparison(self, body):
        if set(body) != {"reviewer_label", "audience", "prior_exposure"}:
            raise ValueError("invalid comparison fields")
        reviewer = text(body["reviewer_label"], 80)
        if (not reviewer or body["audience"] not in ("student", "teacher")
                or type(body["prior_exposure"]) is not bool):
            raise ValueError("invalid comparison context")
        with self.lock:
            if self.library()["status"] != "ready":
                raise ValueError("replay unavailable")
            cases = list(self._cases.items())
            rng = secrets.SystemRandom()
            rng.shuffle(cases)
            sides = ["baseline"] * (len(cases) // 2) + ["selected"] * (len(cases) // 2)
            if len(cases) % 2:
                sides.append(rng.choice(["baseline", "selected"]))
            rng.shuffle(sides)
            tasks = []
            for (case_id, case), side in zip(cases, sides):
                package = next(p for p in case["packages"] if
                               p["checkpoint"] == "end" and p["audience"] == body["audience"])
                review = package["review"]
                mapping = {"A": side, "B": "selected" if side == "baseline" else "baseline"}
                originals = {"baseline": review["template_baseline"]["text"],
                             "selected": review["message"]["text"]}
                messages = {key: originals[value] for key, value in mapping.items()}
                tasks.append({
                    "task_id": secrets.token_hex(16), "scenario_id": case_id,
                    "scenario_name": case["name"], "mapping": mapping,
                    "messages": messages,
                    "message_sha256": {key: digest(value) for key, value in messages.items()},
                    "evidence": observed(review["sanitized_evidence"]),
                    "evidence_sha256": digest(review["sanitized_evidence"]),
                    "package_sha256": digest(package),
                    "identical_text": messages["A"] == messages["B"],
                    "selector_execution": copy.deepcopy(package.get("selector_execution", {})),
                    "generator_execution": copy.deepcopy(package.get("generator_execution", {})),
                    "judgment": None})
            record = {"protocol": PROTOCOL, "id": secrets.token_hex(16),
                      "created_at": utc_now(), "reviewer_label": reviewer,
                      "audience": body["audience"], "prior_exposure": body["prior_exposure"],
                      "report_sha256": self._sha256,
                      "source": copy.deepcopy(self._report["source"]),
                      "policy": copy.deepcopy(self._report["policy"]),
                      "randomization": "cryptographic_shuffle_balanced_AB_persisted_v1",
                      "reveal_policy": "after_all_judgments_saved",
                      "digest_encoding": "sha256_of_sorted_compact_utf8_json_except_report_raw_bytes",
                      "exposure_measurement": "self_report_at_review_start_not_verified_blinding",
                      "approves_learner_delivery": False,
                      "educational_effectiveness_tested": False,
                      "limitation": LIMITATION, "tasks": tasks}
            self._save(record)
            return self._view(record)

    def _view(self, record):
        completed = sum(task["judgment"] is not None for task in record["tasks"])
        complete = completed == len(record["tasks"])
        source_changed = record["report_sha256"] != self._sha256
        view = {key: record[key] for key in (
            "id", "protocol", "created_at", "reviewer_label", "audience", "prior_exposure")}
        view.update({"completed": completed, "total": len(record["tasks"]),
                     "status": "complete" if complete else
                     "source_changed" if source_changed else "in_progress", "task": None})
        if complete:
            preferences = Counter(task["judgment"]["resolved_preference"] for task in record["tasks"])
            view["results"] = {"preferences": {key: preferences[key] for key in
                                  ("baseline", "selected", "tie", "neither")},
                               "identical_pairs": sum(t["identical_text"] for t in record["tasks"]),
                               "limitation": LIMITATION}
        elif not source_changed:
            task = next(t for t in record["tasks"] if t["judgment"] is None)
            view["task"] = {"task_id": task["task_id"], "ordinal": completed + 1,
                            "drafts": copy.deepcopy(task["messages"]),
                            "evidence": copy.deepcopy(task["evidence"])}
        return view

    def comparisons(self):
        with self.lock:
            self.library()
            rows = []
            for path in sorted(self.directory.glob("*.json")):
                view = self._view(self._read(path.stem))
                view.pop("task")
                view.pop("results", None)
                rows.append(view)
            return {"comparisons": sorted(rows, key=lambda row: row["created_at"], reverse=True)}

    def comparison(self, comparison_id):
        with self.lock:
            self.library()
            return self._view(self._read(comparison_id))

    def add_judgment(self, comparison_id, body):
        if set(body) != {"task_id", "preference", "support_a", "support_b", "confidence", "note"}:
            raise ValueError("invalid judgment fields")
        for field, choices in {
                "preference": ("A", "B", "tie", "neither"),
                "support_a": ("supported", "unsupported", "unsure"),
                "support_b": ("supported", "unsupported", "unsure"),
                "confidence": ("low", "moderate", "high")}.items():
            if body[field] not in choices:
                raise ValueError("invalid judgment choice")
        clean = dict(body, task_id=text(body["task_id"], 32), note=text(body["note"], 2000))
        with self.lock:
            self.library()
            record = self._read(comparison_id)
            if record["report_sha256"] != self._sha256:
                raise ResearchConflict("replay revision changed")
            task = next((t for t in record["tasks"] if t["task_id"] == clean["task_id"]), None)
            if task is None:
                raise ResearchConflict("unknown task")
            if task["judgment"] is not None:
                if task["judgment"]["submission"] != clean:
                    raise ResearchConflict("judgment already recorded")
                return self._view(record)
            if task is not next(t for t in record["tasks"] if t["judgment"] is None):
                raise ResearchConflict("task out of order")
            task["judgment"] = {
                "submission": clean, "recorded_at": utc_now(),
                "resolved_preference": task["mapping"].get(clean["preference"], clean["preference"])}
            self._save(record)
            return self._view(record)

    def export_comparison(self, comparison_id):
        with self.lock:
            record = self._read(comparison_id)
            if any(t["judgment"] is None for t in record["tasks"]):
                raise ResearchConflict("finish review before revealing sources")
            return copy.deepcopy(record)
