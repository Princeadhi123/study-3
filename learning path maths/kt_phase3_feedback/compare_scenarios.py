"""Render a private one-page comparison and feedback report from saved evaluations."""
import argparse
import copy
import csv
import html
import json
import math
import statistics
from pathlib import Path

import phase3_paths
from mcq_service import MCQSessionService

SCHEMA = "phase3_fixed40_comparison_v1"


def summarize(result: dict, bank: dict):
    from session_store import bank_fingerprint
    if result.get("schema") != SCHEMA or result.get("bank_sha256") != bank_fingerprint(bank):
        raise ValueError("evaluation schema or bank fingerprint mismatch")
    skill_names = bank["skill_names"]
    skill_ids = list(skill_names)
    if len(result["scenarios"]) != 54 or len(skill_ids) != 4:
        raise ValueError("expected the complete fixed-40 comparison matrix")
    summary, feedback, subtopics = [], [], []
    seen = set()
    trajectories = {}
    for case in result["scenarios"]:
        name, responses, probabilities = (case["name"], case["responses"],
                                          case["kt_pre_answer_probability"])
        if name in seen or len(responses) != 40 or len(probabilities) != 40:
            raise ValueError("duplicate or incomplete scenario")
        seen.add(name)
        if [r["question_id"] for r in responses] != [q["question_id"] for q in bank["questions"]]:
            raise ValueError("scenario has a different question order")
        if ([r["position"] for r in responses] != list(range(1, 41))
                or any(r["skill_id"] != q["skill_id"] or
                       not isinstance(r["correct"], bool) or
                       type(r["selected_index"]) is not int or
                       r["selected_index"] not in range(len(q["options"])) or
                       r["correct"] != (r["selected_index"] == q["answer_index"])
                       for r, q in zip(responses, bank["questions"]))):
            raise ValueError("response metadata or correctness disagrees with approved bank")
        if any(type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1
               for p in probabilities):
            raise ValueError("KT probabilities must be finite numbers in [0, 1]")
        trajectory = tuple(r["selected_index"] for r in responses)
        aliases = trajectories.setdefault(trajectory, [])
        duplicate_of = aliases[0] if aliases else ""
        aliases.append(name)
        first = sum(r["correct"] for r in responses[:20])
        second = sum(r["correct"] for r in responses[20:])
        observed = case["observed"]
        if (observed["end"]["total"] != {"correct": first + second, "out_of": 40}
                or "total" in observed["midpoint"]):
            raise ValueError("checkpoint totals do not match responses")
        row = {"scenario": name, "group": case["group"], "seed": case["seed"],
               "duplicate_response_sequence_of": duplicate_of,
               "first_20_correct": first, "second_20_correct": second,
               "total_correct": first + second, "total_out_of": 40,
               "kt_first_20_mean": statistics.mean(probabilities[:20]),
               "kt_second_20_mean": statistics.mean(probabilities[20:]),
               "kt_all_40_mean": statistics.mean(probabilities)}
        for sid in skill_ids:
            indexes = [i for i, r in enumerate(responses) if r["skill_id"] == sid]
            if len(indexes) != 10:
                raise ValueError("each skill must have ten fixed questions")
            row[f"skill_{sid}_correct"] = sum(responses[i]["correct"] for i in indexes)
            row[f"kt_{sid}_mean"] = statistics.mean(probabilities[i] for i in indexes)
            scored = next(s for s in observed["end"]["skills"] if s["skill_id"] == sid)
            if (scored["correct"], scored["out_of"]) != (row[f"skill_{sid}_correct"], 10):
                raise ValueError("skill feedback does not match responses")
        summary.append(row)
        for checkpoint, limit in (("midpoint", 20), ("end", 40)):
            payload = observed[checkpoint]
            if (sum(s["correct"] for s in payload["subtopics"]) !=
                    sum(r["correct"] for r in responses[:limit]) or
                    sum(s["out_of"] for s in payload["subtopics"]) != limit):
                raise ValueError("subtopic feedback does not match responses")
            feedback.append({"scenario": name, "checkpoint": checkpoint,
                             "message": payload["message"],
                             "previous_message": case.get("observed_before", {}).get(
                                 checkpoint, {}).get("message", ""),
                             "message_source": payload["message_source"],
                             "total_correct": payload.get("total", {}).get("correct", ""),
                             "total_out_of": payload.get("total", {}).get("out_of", ""),
                             "skills_json": json.dumps(payload["skills"], ensure_ascii=False),
                             "subtopics_json": json.dumps(payload["subtopics"], ensure_ascii=False),
                             "current_style_selector_context_json": json.dumps(
                                 case["selector_context"][checkpoint], ensure_ascii=False)})
            subtopics.extend({"scenario": name, "checkpoint": checkpoint,
                              "skill_name": s["skill_name"],
                              "subtopic": s["subtopic_name"],
                              "correct": s["correct"], "out_of": s["out_of"]}
                             for s in payload["subtopics"])
    pair = {r["name"]: r for r in result["scenarios"] if r["group"] == "distractor"}
    a, b = pair["wrong_option_1"], pair["wrong_option_2"]
    if a["observed"] != b["observed"]:
        raise ValueError("wrong-option pair must preserve observed feedback")
    delta = [abs(x - y) for x, y in zip(a["kt_pre_answer_probability"],
                                        b["kt_pre_answer_probability"])]
    diagnostics = {"distractor_kt_positions_changed": sum(d > 0 for d in delta),
                   "distractor_kt_max_absolute_delta": max(delta),
                   "distractor_observed_feedback_identical": True,
                   "scenario_labels": len(summary),
                   "unique_response_sequences": len(trajectories),
                   "duplicate_response_sequence_groups": [names for names in trajectories.values()
                                                          if len(names) > 1]}
    return summary, feedback, subtopics, skill_names, diagnostics


def pipeline_tables(result: dict):
    checkpoints, routing, teachers = [], [], []
    status_counts = {"midpoint": {}, "end": {}}
    zero_score_counts = {}
    for case in result["scenarios"]:
        if "conformal" not in case or "graph" not in case or "teacher_end" not in case:
            raise ValueError("full pipeline report requires all diagnostic stages")
        for checkpoint, n_items in (("midpoint", 5), ("end", 10)):
            scores = {s["skill_id"]: s for s in case["observed"][checkpoint]["skills"]}
            for sid, decision in case["conformal"]["checkpoints"][checkpoint].items():
                score = scores[sid]
                if decision["n_items"] != n_items or score["out_of"] != n_items:
                    raise ValueError("conformal block and observed denominator mismatch")
                rate = score["correct"] / score["out_of"]
                lower, upper = decision["lower"], decision["upper"]
                status = decision["status"]
                if not 0 <= lower <= upper <= 1:
                    raise ValueError("invalid saved conformal interval")
                row = {"scenario": case["name"], "checkpoint": checkpoint,
                       "skill_id": sid, "skill_name": score["skill_name"],
                       "observed_correct": score["correct"], "out_of": score["out_of"],
                       "observed_rate": rate, "kt_mean": decision["point_estimate"],
                       "lower": lower, "upper": upper, "status": status,
                       "regime": decision["regime"], "calibrated_k": n_items,
                       "threshold": decision["mastery_threshold"],
                       "observed_rate_in_interval": lower <= rate <= upper,
                       "safe_status_below_observed_threshold": status == "MASTERY_SAFE" and
                       rate < decision["mastery_threshold"],
                       "fixed_bank_coverage_validated": False}
                checkpoints.append(row)
                status_counts[checkpoint][status] = status_counts[checkpoint].get(status, 0) + 1
                if checkpoint == "end" and score["correct"] == 0:
                    zero_score_counts[status] = zero_score_counts.get(status, 0) + 1
        graph = case["graph"]
        if (graph.get("scope") != "approved_bank_topics_subtopics_only" or
                graph.get("relation") != "is_part_of_not_prerequisite" or
                graph.get("bank_sha256") != result["bank_sha256"] or
                "routing" in graph or "prerequisites" in graph):
            raise ValueError("report requires the matching assessment taxonomy, not a global graph")
        end_graph = graph["checkpoints"]["end"]
        scores = {s["skill_id"]: s for s in case["observed"]["end"]["skills"]}
        for sid, recommendation in end_graph["recommendations"].items():
            errors = recommendation["subtopics"]
            expected_type = "ASSESSMENT_SUBTOPIC_PRACTICE" if errors else "NONE"
            if (recommendation["recommendation_type"] != expected_type or
                    sum(s["incorrect"] for s in errors) != scores[sid]["out_of"] - scores[sid]["correct"]):
                raise ValueError("assessment practice candidates disagree with observed errors")
            routing.append({"scenario": case["name"], "skill_id": sid,
                            "skill_name": recommendation["skill_name"],
                            "recommendation_type": expected_type,
                            "subtopic_names": "; ".join(s["subtopic_name"] for s in errors),
                            "assessed_incorrect": sum(s["incorrect"] for s in errors),
                            "candidate_message": recommendation["message"]})
        teachers.append({"scenario": case["name"], **copy.deepcopy(case["teacher_end"])})
    end = [r for r in checkpoints if r["checkpoint"] == "end"]
    counts = {"scenario_count": len(teachers), "checkpoint_skill_rows": len(checkpoints),
              "assessment_practice_rows": len(routing), "gate_status_counts": status_counts,
              "end_zero_score_status_counts": zero_score_counts,
              "end_safe_status_below_observed_threshold": [r for r in end if
                                                          r["safe_status_below_observed_threshold"]],
              "end_observed_rate_outside_interval_count": sum(not r["observed_rate_in_interval"] for r in end),
              "interpretation": "Synthetic stress-test counts over correlated fixed-bank scenarios, not empirical coverage or validated mastery.",
              "graph_scope": "approved_bank_topics_subtopics_only",
              "global_prerequisite_routing_enabled": False,
              "assessment_practice_counts": {kind: sum(r["recommendation_type"] == kind for r in routing)
                                             for kind in ("NONE", "ASSESSMENT_SUBTOPIC_PRACTICE")}}
    return checkpoints, routing, teachers, counts


def write_csv(path: Path, rows: list[dict]):
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def make_figure(summary: list[dict], skill_names: dict, path: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    labels = [r["scenario"] for r in summary]
    for sid, skill_name in skill_names.items():
        labels = [label.replace(sid, skill_name) for label in labels]
    observed = [[r["first_20_correct"] / 20, r["second_20_correct"] / 20,
                 r["total_correct"] / 40] +
                [r[f"skill_{sid}_correct"] / 10 for sid in skill_names]
                for r in summary]
    kt = [[r[f"kt_{sid}_mean"] for sid in skill_names] for r in summary]
    fig, (left, right) = plt.subplots(1, 2, figsize=(22, 25),
                                      gridspec_kw={"width_ratios": [7, 4]},
                                      sharey=True)
    for ax, values, columns, title in (
        (left, observed, ["first 20", "last 20", "total 40"] +
         list(skill_names.values()), "Observed correct / questions"),
        (right, kt, list(skill_names.values()),
         "Frozen KT: mean P(next correct) before answer (uncalibrated)")):
        image = ax.imshow(np.asarray(values), aspect="auto", vmin=0, vmax=1,
                          interpolation="nearest", cmap="viridis")
        ax.set_xticks(range(len(columns)), columns, rotation=55, ha="right", fontsize=9)
        ax.set_yticks(range(len(labels)), labels, fontsize=8)
        ax.tick_params(axis="y", length=0, labelleft=True)
        ax.set_title(title)
        for boundary in (20, 22, 52):
            ax.axhline(boundary - 0.5, color="white", linewidth=2)
        fig.colorbar(image, ax=ax, fraction=0.02, pad=0.02)
    for i, row in enumerate(summary):
        left.text(2, i, f"{row['total_correct']}/40", color="white" if
                  row["total_correct"] < 26 else "black", fontsize=7,
                  ha="center", va="center")
    fig.suptitle("Approved fixed 40-question bank: 54 synthetic scenarios, same question order\n"
                 "KT is a separate researcher-only diagnostic, not feedback or calibrated mastery",
                 fontsize=14)
    fig.subplots_adjust(left=0.23, right=0.97, top=0.94, bottom=0.07, wspace=0.34)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def make_html(summary: list[dict], feedback: list[dict], skill_names: dict,
              diagnostics: dict, path: Path):
    e = html.escape
    feedback_by_name = {}
    for row in feedback:
        feedback_by_name.setdefault(row["scenario"], {})[row["checkpoint"]] = row
    with path.open("x", encoding="utf-8") as stream:
        stream.write("<!doctype html><html lang='en'><meta charset='utf-8'>"
                     "<title>Private synthetic comparison</title><style>"
                     "body{font:15px system-ui;margin:2rem;max-width:1400px}"
                     "table{border-collapse:collapse;width:100%}th,td{padding:.4rem;"
                     "border:1px solid #ccc;vertical-align:top}tr:nth-child(even){background:#eee}"
                     "img{width:100%;max-width:1250px}details{margin:.3rem 0}"
                     "pre{white-space:pre-wrap;overflow-wrap:anywhere}"
                     "</style><h1>Fixed-bank synthetic comparison</h1>"
                     "<p>54 simulated cold-start sessions on one approved, fixed-order "
                     "40-question bank. No students, no LLM calls. Observed counts and student "
                     "messages come from the real session service; frozen KT is uncalibrated "
                     "research metadata, not a score or mastery label.</p>"
                     "<img src='comparison.png' alt='54-row comparison heatmap'>"
                     "<p>Rows: scenarios; observed panel: first/last half, final, four skills; "
                     "KT panel: mean pre-answer probabilities by skill. Both colors are 0–1 "
                     "but measure different things. Full per-run values are in summary.csv.</p>")
        stream.write(f"<h2>Interpretation limits</h2><p>{diagnostics['scenario_labels']} "
                     f"scenario labels represent {diagnostics['unique_response_sequences']} "
                     "unique selected-response sequences, not independent students. "
                     "All use one bank and one question order; seeds do not measure real learning "
                     "or fatigue. Integrity rejection tests are separate from these valid sessions.</p>")
        if diagnostics["duplicate_response_sequence_groups"]:
            stream.write("<p>Shared sequences: " + e(json.dumps(
                diagnostics["duplicate_response_sequence_groups"])) + "</p>")
        stream.write("<h2>Wrong-option sensitivity</h2><p>Correctness and observed feedback "
                     "are identical for both wrong-option sequences. Frozen KT differs at "
                     f"{diagnostics['distractor_kt_positions_changed']} of 40 positions; "
                     "maximum absolute difference: "
                     f"{diagnostics['distractor_kt_max_absolute_delta']:.6f}. "
                     "This is sensitivity, not evidence of improved accuracy.</p>")
        stream.write("<h2>Actual checkpoint feedback</h2><p>Open a row to inspect the exact "
                     "midpoint/end student messages, per-skill and per-subtopic observed counts, "
                     "and the context passed to the current style selector. All runs are also in "
                     "feedback.csv and subtopics.csv.</p><table><tr><th>Scenario</th>"
                     "<th>Observed 20 / 40</th><th>Feedback details</th></tr>")
        for row in summary:
            name = row["scenario"]
            stream.write(f"<tr><td>{e(name)}</td><td>{row['first_20_correct']}/20, "
                         f"{row['total_correct']}/40</td><td><details><summary>Show feedback</summary>")
            for checkpoint in ("midpoint", "end"):
                data = feedback_by_name[name][checkpoint]
                stream.write(f"<h4>{checkpoint}</h4><p>{e(data['message'])}</p>")
                if data["previous_message"]:
                    stream.write("<details><summary>Before the feedback fix</summary><p>" +
                                 e(data["previous_message"]) + "</p></details>")
                stream.write(f"<details><summary>Skills and subtopics</summary><pre>{e(data['skills_json'])}"
                             f"\n{e(data['subtopics_json'])}</pre></details>"
                             f"<details><summary>Current selector input</summary><pre>"
                             f"{e(data['current_style_selector_context_json'])}</pre></details>")
            stream.write("</details></td></tr>")
        stream.write("</table><h2>Possible future LLM boundary (not implemented)</h2>"
                     "<p>The current bounded style selector receives only checkpoint and "
                     "observed_performance (total_questions; at end: total_correct and uniquely "
                     "strongest/growth skill with display name and correct/total), plus "
                     "prerequisite_guidance with no approved prerequisite. The actual student "
                     "feedback carries observed per-skill and descriptive per-subtopic counts; "
                     "subtopic data are not currently passed to the selector.</p>"
                     "<p>For a future LLM, consider only checkpoint, observed skill counts, "
                     "descriptive subtopic counts, end-only observed total, and strictly "
                     "bounded style instructions. Do not provide answer keys, raw question "
                     "text/IDs, selected options, student identifiers, frozen KT probabilities, "
                     "unvalidated conformal diagnostics, global prerequisite routing, "
                     "or claims of mastery/misconception. "
                     "LLM integration needs separate consent/privacy review and output validation."
                     "</p></html>")


def make_full_figure(summary: list[dict], skill_names: dict,
                     checkpoints: list[dict], routing: list[dict],
                     path: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Patch
    labels = [r["scenario"] for r in summary]
    for sid, skill_name in skill_names.items():
        labels = [label.replace(sid, skill_name) for label in labels]
    observed = [[r["first_20_correct"] / 20, r["second_20_correct"] / 20,
                 r["total_correct"] / 40] +
                [r[f"skill_{sid}_correct"] / 10 for sid in skill_names]
                for r in summary]
    kt = [[r[f"kt_{sid}_mean"] for sid in skill_names] for r in summary]
    end_status = {(r["scenario"], r["skill_id"]): r["status"]
                  for r in checkpoints if r["checkpoint"] == "end"}
    end_practice = {(r["scenario"], r["skill_id"]): r["recommendation_type"]
                    for r in routing}
    status_codes = {"CONFIDENT_STRUGGLE": 0, "UNCERTAIN_BEHAVIOR": 1,
                    "MASTERY_SAFE": 2}
    status_colors = ["#d62728", "#e69f00", "#1f77b4"]
    practice_codes = {"NONE": 0, "ASSESSMENT_SUBTOPIC_PRACTICE": 1}
    practice_colors = ["#9e9e9e", "#008080"]
    status_matrix = [[status_codes[end_status[(r["scenario"], sid)]]
                      for sid in skill_names] for r in summary]
    practice_matrix = [[practice_codes[end_practice[(r["scenario"], sid)]]
                        for sid in skill_names] for r in summary]
    fig, (ax_obs, ax_kt, ax_status, ax_practice) = plt.subplots(
        1, 4, figsize=(30, 25), sharey=True,
        gridspec_kw={"width_ratios": [7, 4, 4, 4]})
    columns = ["first 20", "last 20", "total 40"] + list(skill_names.values())
    image = ax_obs.imshow(np.asarray(observed), aspect="auto", vmin=0,
                          vmax=1, interpolation="nearest", cmap="viridis")
    ax_obs.set_xticks(range(len(columns)), columns, rotation=55,
                      ha="right", fontsize=10)
    ax_obs.set_yticks(range(len(labels)), labels, fontsize=9)
    ax_obs.tick_params(axis="y", length=0, labelleft=True)
    ax_obs.set_title("Observed correct / questions\n(first and last half, "
                     "total of 40, per-skill of 10)", fontsize=12)
    fig.colorbar(image, ax=ax_obs, fraction=0.02, pad=0.02)
    for i, row in enumerate(summary):
        ax_obs.text(2, i, f"{row['total_correct']}/40",
                    color="white" if row["total_correct"] < 26 else "black",
                    fontsize=8, ha="center", va="center")
    image = ax_kt.imshow(np.asarray(kt), aspect="auto", vmin=0, vmax=1,
                         interpolation="nearest", cmap="viridis")
    ax_kt.set_xticks(range(len(skill_names)), list(skill_names.values()),
                     rotation=55, ha="right", fontsize=10)
    ax_kt.set_title("Frozen KT: mean P(next correct)\nbefore answer "
                    "(uncalibrated)", fontsize=12)
    fig.colorbar(image, ax=ax_kt, fraction=0.02, pad=0.02)
    for ax, matrix, codes, colors, bounds, title in (
            (ax_status, status_matrix, status_codes, status_colors,
             [-0.5, 0.5, 1.5, 2.5],
             "End conformal status (k=10)\nexploratory research labels,\n"
             "not established mastery"),
            (ax_practice, practice_matrix, practice_codes, practice_colors,
             [-0.5, 0.5, 1.5],
             "Assessed subtopics with observed errors\n"
             "Descriptive practice candidates, not prerequisites")):
        cmap = ListedColormap(colors)
        ax.imshow(np.asarray(matrix), aspect="auto", cmap=cmap,
                  norm=BoundaryNorm(bounds, cmap.N),
                  interpolation="nearest")
        ax.set_xticks(range(len(skill_names)), list(skill_names.values()),
                      rotation=55, ha="right", fontsize=10)
        ax.set_title(title, fontsize=12)
        ax.tick_params(axis="y", length=0, labelleft=False)
    for ax in (ax_obs, ax_kt, ax_status, ax_practice):
        for boundary in (20, 22, 52):
            ax.axhline(boundary - 0.5, color="white", linewidth=2)
    fig.legend(handles=[Patch(facecolor=color, label=label)
                        for label, color in zip(status_codes,
                                                status_colors)],
               loc="lower center", bbox_to_anchor=(0.72, 0.005),
               fontsize=10, title="Conformal status (research label)",
               title_fontsize=10)
    fig.legend(handles=[Patch(facecolor=color, label=label)
                        for label, color in zip(practice_codes,
                                                practice_colors)],
               loc="lower center", bbox_to_anchor=(0.9, 0.005),
               fontsize=10, title="Assessment practice (observed errors)",
               title_fontsize=10)
    fig.suptitle("Approved fixed 40-question bank: 54 synthetic scenarios, "
                 "one fixed question order\nConformal status names are "
                 "research labels, not established mastery; practice "
                 "candidates describe observed errors in this assessment\n"
                 "only, not prerequisites; no validated student or LLM "
                 "impact", fontsize=16)
    fig.subplots_adjust(left=0.16, right=0.98, top=0.93, bottom=0.17,
                        wspace=0.28)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def make_full_html(summary: list[dict], feedback: list[dict],
                   skill_names: dict, diagnostics: dict,
                   checkpoints: list[dict], routing: list[dict],
                   teachers: list[dict], counts: dict, path: Path):
    e = html.escape
    feedback_by_name = {}
    for row in feedback:
        feedback_by_name.setdefault(row["scenario"], {})[row["checkpoint"]] = row
    end_rows = {(r["scenario"], r["skill_id"]): r for r in checkpoints
                if r["checkpoint"] == "end"}
    mid_rows = {(r["scenario"], r["skill_id"]): r for r in checkpoints
                if r["checkpoint"] == "midpoint"}
    routing_by = {(r["scenario"], r["skill_id"]): r for r in routing}
    teacher_by = {t["scenario"]: t for t in teachers}

    def pretty(payload):
        return e(json.dumps(payload, ensure_ascii=False, indent=2))

    with path.open("x", encoding="utf-8") as stream:
        stream.write("<!doctype html><html lang='en'><meta charset='utf-8'>"
                     "<title>Private synthetic comparison (full pipeline)</title><style>"
                     "body{font:15px system-ui;margin:2rem;max-width:1400px}"
                     "table{border-collapse:collapse;width:100%}th,td{padding:.4rem;"
                     "border:1px solid #ccc;vertical-align:top}tr:nth-child(even){background:#eee}"
                     "img{width:100%;max-width:1250px}details{margin:.3rem 0}"
                     "pre{white-space:pre-wrap;overflow-wrap:anywhere}"
                     ".student{border-left:4px solid #1f77b4;padding-left:.8rem}"
                     ".private{border-left:4px solid #9467bd;padding-left:.8rem}"
                     "</style><h1>Fixed-bank synthetic comparison — private "
                     "research report</h1>"
                     "<p>54 simulated cold-start sessions on one approved, fixed-order "
                     "40-question bank. No students, no LLM calls. Observed counts and student "
                     "messages come from the real session service; frozen KT and conformal "
                     "status are research metadata not validated for this assessment. The "
                     "assessment-graph panel lists observed-error practice candidates inside "
                     "the approved bank taxonomy — descriptive and pending educator review, "
                     "not prerequisites or established mastery.</p>"
                     "<img src='comparison.png' alt='54-row comparison heatmap'>"
                     "<p>Rows: scenarios; observed panel: first/last half, final, four skills; "
                     "KT panel: mean pre-answer probabilities by skill; then end-checkpoint "
                     "conformal status and observed-error practice candidates per skill. "
                     "Practice candidates come only from the bank-matched assessment "
                     "taxonomy — no global prerequisite lookup. Continuous colors are 0–1; "
                     "status and practice colors are categorical labels.</p>")
        stream.write(f"<h2>Interpretation limits</h2><p>{diagnostics['scenario_labels']} "
                     f"scenario labels represent {diagnostics['unique_response_sequences']} "
                     "unique selected-response sequences, not independent students. "
                     "All use one bank and one question order; seeds do not measure real learning "
                     "or fatigue. Integrity rejection tests are separate from these valid sessions.</p>")
        if diagnostics["duplicate_response_sequence_groups"]:
            stream.write("<p>Shared sequences: " + e(json.dumps(
                diagnostics["duplicate_response_sequence_groups"])) + "</p>")
        stream.write("<h2>Wrong-option sensitivity</h2><p>Correctness and observed feedback "
                     "are identical for both wrong-option sequences. Frozen KT differs at "
                     f"{diagnostics['distractor_kt_positions_changed']} of 40 positions; "
                     "maximum absolute difference: "
                     f"{diagnostics['distractor_kt_max_absolute_delta']:.6f}. "
                     "This is sensitivity, not evidence of improved accuracy.</p>")
        stream.write("<h2>Pre-derived pipeline counts</h2>"
                     f"<p>{e(counts['interpretation'])} These are synthetic stress counts "
                     "over the fixed scenario matrix, not real coverage.</p>"
                     f"<ul><li>Scenario count: {counts['scenario_count']}</li>"
                     f"<li>Checkpoint × skill rows: {counts['checkpoint_skill_rows']}</li>"
                     f"<li>Assessment practice rows: {counts['assessment_practice_rows']}</li>"
                     "<li>End rows where observed rate is outside the saved interval: "
                     f"{counts['end_observed_rate_outside_interval_count']}</li></ul>"
                     f"<p>Assessment graph scope: {e(counts['graph_scope'])}; "
                     "global prerequisite routing enabled: "
                     f"{counts['global_prerequisite_routing_enabled']}.</p>")
        for label, table in (
                ("Midpoint status counts (k=5, exploratory)",
                 counts["gate_status_counts"]["midpoint"]),
                ("End status counts (k=10, exploratory)",
                 counts["gate_status_counts"]["end"]),
                ("End zero-score status counts",
                 counts["end_zero_score_status_counts"]),
                ("Assessment practice candidate counts (observed errors)",
                 counts["assessment_practice_counts"])):
            stream.write(f"<h3>{e(label)}</h3><pre>{e(json.dumps(table, indent=2))}</pre>")
        below = counts["end_safe_status_below_observed_threshold"]
        stream.write("<p>End rows labelled MASTERY_SAFE despite an observed rate below "
                     f"the gate threshold: {len(below)}</p>")
        if below:
            stream.write("<details><summary>Rows</summary><pre>" +
                         e(json.dumps([{"scenario": r["scenario"],
                                        "skill_name": r["skill_name"],
                                        "observed": f"{r['observed_correct']}/{r['out_of']}",
                                        "status": r["status"]}
                                       for r in below], indent=2)) +
                         "</pre></details>")
        stream.write("<h2>Per-scenario detail</h2><p>Open a row for the exact student "
                     "midpoint/end messages, observed skill and subtopic counts, the "
                     "current restricted selector context, the private teacher-end "
                     "summary with its pending-review practice candidates, and "
                     "end-only research diagnostics. Student and private/research "
                     "sections are visually separated.</p>")
        for row in summary:
            name = row["scenario"]
            teacher = teacher_by[name]
            stream.write(f"<details><summary>{e(name)} — observed "
                         f"{row['first_20_correct']}/20, {row['total_correct']}/40"
                         "</summary><div class='student'>"
                         "<h3>Student-facing messages (observed counts only)</h3>")
            for checkpoint in ("midpoint", "end"):
                data = feedback_by_name[name][checkpoint]
                stream.write(f"<h4>{checkpoint} student message</h4>"
                             f"<p>{e(data['message'])}</p>")
                if data["previous_message"]:
                    stream.write("<details><summary>Student text before the feedback "
                                 "fix</summary><p>" + e(data["previous_message"]) +
                                 "</p></details>")
                stream.write("<details><summary>Observed skills and subtopics</summary>"
                             f"<pre>{pretty(json.loads(data['skills_json']))}\n"
                             f"{pretty(json.loads(data['subtopics_json']))}</pre></details>"
                             "<details><summary>Current restricted selector context"
                             "</summary><pre>"
                             f"{pretty(json.loads(data['current_style_selector_context_json']))}"
                             "</pre></details>")
            stream.write(f"</div><div class='private'><h3>Private teacher-end summary "
                         "(researcher only)</h3><p>{e(teacher['message'])}</p>"
                         f"<p>message_source: {e(teacher['message_source'])}; observed "
                         f"total: {teacher['total']['correct']}/"
                         f"{teacher['total']['out_of']}</p>"
                         "<details><summary>Observed skill and subtopic counts</summary>"
                         f"<pre>{pretty(teacher['skills'])}\n{pretty(teacher['subtopics'])}"
                         "</pre></details><p>Limitations:</p><ul>" +
                         "".join(f"<li>{e(limit)}</li>"
                                 for limit in teacher["limitations"]) + "</ul>"
                         "<p>Assessment practice candidates describe observed errors "
                         "inside this assessment only and are pending educator review; "
                         "no prerequisite or misconception inference is made.</p>"
                         "<details><summary>Assessment practice candidates "
                         "(pending educator review)</summary><pre>"
                         f"{pretty(teacher['assessment_practice'])}</pre></details>")
            stream.write("<h3>Assessment practice candidates — observed errors "
                         "(descriptive)</h3><table><tr><th>Skill</th>"
                         "<th>Candidate type</th><th>Assessed subtopics</th>"
                         "<th>Observed incorrect</th><th>Candidate message</th></tr>")
            for sid in skill_names:
                rrow = routing_by[(name, sid)]
                stream.write(f"<tr><td>{e(rrow['skill_name'])}</td>"
                             f"<td>{e(rrow['recommendation_type'])}</td>"
                             f"<td>{e(rrow['subtopic_names'])}</td>"
                             f"<td>{rrow['assessed_incorrect']}</td>"
                             f"<td>{e(rrow['candidate_message'])}</td></tr>")
            stream.write("</table><h3>End-checkpoint research diagnostics (private)</h3>"
                         "<table><tr><th>Skill</th><th>Observed</th><th>KT mean</th>"
                         "<th>Interval [lower, upper]</th><th>Status / regime</th></tr>")
            for sid in skill_names:
                crow = end_rows[(name, sid)]
                stream.write(f"<tr><td>{e(crow['skill_name'])}</td>"
                             f"<td>{crow['observed_correct']}/{crow['out_of']}</td>"
                             f"<td>{crow['kt_mean']:.3f}</td>"
                             f"<td>[{crow['lower']:.3f}, {crow['upper']:.3f}]</td>"
                             f"<td>{e(crow['status'])} / {e(crow['regime'])}</td></tr>")
            stream.write("</table><details><summary>Midpoint research table "
                         "(k=5 caveat)</summary><p>Midpoint blocks use the k=5 "
                         "calibration block; status names are exploratory research "
                         "labels, not established mastery.</p>"
                         "<table><tr><th>Skill</th><th>Observed</th><th>KT mean</th>"
                         "<th>Interval [lower, upper]</th><th>Status / regime</th></tr>")
            for sid in skill_names:
                crow = mid_rows[(name, sid)]
                stream.write(f"<tr><td>{e(crow['skill_name'])}</td>"
                             f"<td>{crow['observed_correct']}/{crow['out_of']}</td>"
                             f"<td>{crow['kt_mean']:.3f}</td>"
                             f"<td>[{crow['lower']:.3f}, {crow['upper']:.3f}]</td>"
                             f"<td>{e(crow['status'])} / {e(crow['regime'])}</td></tr>")
            stream.write("</table></details></div></details>")
        stream.write("<hr><p>No Jev or LLM was called. This report tests local composition "
                     "and role separation; teacher-end presentation is private, not a "
                     "deployed authenticated teacher portal.</p></html>")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evaluation", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--full-pipeline", action="store_true",
                        help="include saved conformal diagnostics, bank-matched assessment "
                             "taxonomy practice candidates, and private teacher-end summaries")
    args = parser.parse_args()
    outputs = [args.out_dir / name for name in
               ("summary.csv", "feedback.csv", "subtopics.csv", "comparison.png",
                "feedback_report.html", "llm_boundary.json")]
    if args.full_pipeline:
        outputs.extend(args.out_dir / name for name in
                       ("conformal_checkpoints.csv", "assessment_practice.csv",
                        "teacher_end.json", "pipeline_diagnostics.json"))
    if any(path.exists() for path in outputs):
        raise FileExistsError("Refusing to overwrite comparison artifacts")
    result = json.loads(args.evaluation.read_text(encoding="utf-8"))
    service = MCQSessionService.from_default()
    summary, feedback, subtopics, skills, diagnostics = summarize(result, service.bank)
    full_tables = pipeline_tables(result) if args.full_pipeline else None
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(outputs[0], summary)
    write_csv(outputs[1], feedback)
    write_csv(outputs[2], subtopics)
    if full_tables:
        checkpoints, routing, teachers, counts = full_tables
        write_csv(outputs[6], checkpoints)
        write_csv(outputs[7], routing)
        for path, data in ((outputs[8], teachers), (outputs[9], counts)):
            with path.open("x", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
        make_full_figure(summary, skills, checkpoints, routing, outputs[3])
        make_full_html(summary, feedback, skills, diagnostics, checkpoints, routing,
                       teachers, counts, outputs[4])
        print(json.dumps(counts))
    else:
        make_figure(summary, skills, outputs[3])
        make_html(summary, feedback, skills, diagnostics, outputs[4])
    boundary = {"status": "proposal_not_implemented",
                "graph_scope": "approved_bank_topics_subtopics_only",
                "current_style_selector_input":
                {"midpoint": result["scenarios"][0]["selector_context"]["midpoint"],
                 "end": result["scenarios"][0]["selector_context"]["end"]},
                "current_student_feedback_fields": ["message", "message_source", "skills",
                                                    "subtopics", "end_only_total", "end_only_summary"],
                "proposed_llm_allowlist": ["checkpoint", "observed_skill_counts",
                                           "descriptive_subtopic_counts", "end_only_observed_total",
                                           "bounded_style_instruction",
                                           "educator_reviewed_assessment_practice_candidates"],
                "do_not_send": ["answer_keys", "raw_question_or_option_text", "question_ids",
                                "selected_answers", "student_or_session_identifiers",
                                "kt_or_conformal_probabilities", "global_prerequisite_routing",
                                "unreviewed_assessment_practice_candidates",
                                "mastery_or_misconception_claims"],
                "distractor_diagnostic": diagnostics}
    with outputs[5].open("x", encoding="utf-8") as stream:
        json.dump(boundary, stream, ensure_ascii=False, indent=2)
    print(f"Wrote {len(summary)} scenario rows, {len(feedback)} checkpoint rows, "
          f"{len(subtopics)} subtopic rows to {args.out_dir}")
    print(json.dumps(diagnostics))


if __name__ == "__main__":
    main()
