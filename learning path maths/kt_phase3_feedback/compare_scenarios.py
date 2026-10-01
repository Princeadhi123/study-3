"""Render a private one-page comparison and feedback report from saved evaluations."""
import argparse
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
                     "unvalidated conformal/graph routing, or claims of mastery/misconception. "
                     "LLM integration needs separate consent/privacy review and output validation."
                     "</p></html>")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evaluation", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    outputs = [args.out_dir / name for name in
               ("summary.csv", "feedback.csv", "subtopics.csv", "comparison.png",
                "feedback_report.html", "llm_boundary.json")]
    if any(path.exists() for path in outputs):
        raise FileExistsError("Refusing to overwrite comparison artifacts")
    result = json.loads(args.evaluation.read_text(encoding="utf-8"))
    service = MCQSessionService.from_default()
    summary, feedback, subtopics, skills, diagnostics = summarize(result, service.bank)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(outputs[0], summary)
    write_csv(outputs[1], feedback)
    write_csv(outputs[2], subtopics)
    make_figure(summary, skills, outputs[3])
    make_html(summary, feedback, skills, diagnostics, outputs[4])
    boundary = {"status": "proposal_not_implemented", "current_style_selector_input":
                {"midpoint": result["scenarios"][0]["selector_context"]["midpoint"],
                 "end": result["scenarios"][0]["selector_context"]["end"]},
                "current_student_feedback_fields": ["message", "message_source", "skills",
                                                    "subtopics", "end_only_total", "end_only_summary"],
                "proposed_llm_allowlist": ["checkpoint", "observed_skill_counts",
                                           "descriptive_subtopic_counts", "end_only_observed_total",
                                           "bounded_style_instruction"],
                "do_not_send": ["answer_keys", "raw_question_or_option_text", "question_ids",
                                "selected_answers", "student_or_session_identifiers",
                                "kt_or_conformal_probabilities", "graph_routing",
                                "mastery_or_misconception_claims"],
                "distractor_diagnostic": diagnostics}
    with outputs[5].open("x", encoding="utf-8") as stream:
        json.dump(boundary, stream, ensure_ascii=False, indent=2)
    print(f"Wrote {len(summary)} scenario rows, {len(feedback)} checkpoint rows, "
          f"{len(subtopics)} subtopic rows to {args.out_dir}")
    print(json.dumps(diagnostics))


if __name__ == "__main__":
    main()
