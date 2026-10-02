"""Render an observed-evidence feedback report to a local HTML review page.

Pure stdlib presentation: reads ONLY the supplied report JSON produced by
``evaluate_evidence_feedback.py`` (never the evidence bank, taxonomy,
providers, or the runner), emits a self-contained HTML file with no
JavaScript and no external assets. The output file is created exclusively
(``x`` mode) and never overwrites an existing file. Every dynamic string
is HTML-escaped. This is a mechanical renderer for human review; it
performs no recomputation, no scoring, and no quality judgments.
"""
import argparse
import html
import json
import sys
from pathlib import Path

from evidence_feedback_policy import REPORT_SCHEMA

_ESCAPE = html.escape


def _esc(value) -> str:
    return _ESCAPE(str(value))


_FACT_PANELS = (
    ("scenario_labels", "Scenario labels"),
    ("feedback_packages", "Feedback packages"),
    ("midpoint_packages", "Midpoint packages"),
    ("end_packages", "End packages"),
    ("new_provider_calls", "New provider calls"),
)

_OPTIONAL_FACTS = (
    ("recorded_hosted_requests", "Recorded hosted requests"),
    ("recorded_hosted_failures", "Recorded hosted failures"),
    ("end_selections_matching_rules",
     "End selections matching rules baseline"),
    ("fallback_packages", "Packages with fallback"),
)

_SUMMARY_FLAGS = (
    ("educator_review_completed", "Educator review completed"),
    ("educational_effectiveness_tested", "Educational effectiveness tested"),
)

_TRACE_FIELDS = (
    "policy_version", "selection_source", "phrasing_source",
    "fallback_reason", "selection_matches_baseline", "validation",
    "provider_advantage_demonstrated",
)


def _kv(key, value) -> str:
    return (f'<div class="kv"><span class="k">{_esc(key)}</span>'
            f'<span class="v mono">{_esc(value)}</span></div>')


def _json_details(title: str, value) -> str:
    blob = json.dumps(value, ensure_ascii=True, indent=2,
                      sort_keys=True)
    return ('<details class="raw"><summary>' + _esc(title)
            + '</summary><pre class="mono">' + _esc(blob)
            + "</pre></details>")


def _message_html(message: dict) -> str:
    """Render sections when present, else the joined message text."""
    sections = message.get("sections")
    if isinstance(sections, list) and sections:
        rows = []
        for section in sections:
            if isinstance(section, dict):
                rows.append(
                    f'<p class="sec"><span class="kind mono">'
                    f'{_esc(section.get("kind", ""))}</span> '
                    f'{_esc(section.get("text", ""))}</p>')
        if rows:
            return "".join(rows)
    return f'<p>{_esc(message.get("text", ""))}</p>'


def _execution_block(title: str, execution: dict | None) -> str:
    if not isinstance(execution, dict):
        return ""
    rows = []
    for key in ("status", "reused", "origin", "capture_file",
                "original_call_latency_ms"):
        if key in execution and execution[key] is not None:
            value = execution[key]
            shown = ("yes" if value else "no") if key == "reused" \
                else _esc(value)
            rows.append(
                f'<div class="kv"><span class="k">{_esc(key)}</span>'
                f'<span class="v mono">{shown}</span></div>')
    rows.append(_json_details("Execution record (verbatim)", execution))
    return (f'<div class="exec"><h4>{_esc(title)}</h4>'
            + "".join(rows) + "</div>")


def _package_details(package: dict) -> str:
    review = package.get("review") if isinstance(package, dict) else {}
    review = review if isinstance(review, dict) else {}
    trace = review.get("trace") if isinstance(review.get("trace"),
                                              dict) else {}
    baseline = (review.get("template_baseline")
                if isinstance(review.get("template_baseline"), dict)
                else {})
    message = (review.get("message")
               if isinstance(review.get("message"), dict) else {})
    audience = _esc(package.get("audience", ""))
    checkpoint = _esc(package.get("checkpoint", ""))
    head = (f'<summary><span class="chip mono">{audience} '
            f'{checkpoint}</span> '
            f'<span class="chip mono">selected: '
            f'{_esc(review.get("selected_candidate_id"))}</span> '
            f'<span class="chip mono">baseline match: '
            f'{_esc(trace.get("selection_matches_baseline"))}</span>'
            f'</summary>')
    meta = [
        _kv("status", review.get("status")),
        _kv("selected candidate", review.get("selected_candidate_id")),
        _kv("baseline candidate", review.get("baseline_candidate_id")),
        _kv("requires human review",
            review.get("requires_human_review")),
    ]
    if "pipeline_wall_ms" in package:
        meta.append(_kv(
            "Pipeline wall time (replay overhead; reused timings are "
            "not fresh hosted latency)",
            f'{package["pipeline_wall_ms"]} ms'))
    texts = (
        '<div class="compare">'
        '<div class="col"><h4>Deterministic baseline</h4>'
        f'{_message_html(baseline)}</div>'
        '<div class="col"><h4>Selected draft</h4>'
        f'{_message_html(message)}</div>'
        '</div>')
    trace_rows = "".join(
        _kv(key, trace.get(key)) for key in _TRACE_FIELDS)
    details = (
        _json_details("Candidates (policy options with fixed actions and "
                      "observed counts)", review.get("candidates"))
        + _json_details("Sanitized review evidence (selector only; "
                        "withheld at midpoint)",
                        review.get("sanitized_evidence")))
    execs = ('<div class="execrow">'
             + _execution_block("Selector execution",
                                package.get("selector_execution"))
             + _execution_block("Generator execution",
                                package.get("generator_execution"))
             + "</div>")
    return ("<details class=\"pkg\">" + head
            + '<div class="pkgbody">' + "".join(meta) + texts
            + '<div class="exec"><h4>Trace provenance</h4>'
            + trace_rows + "</div>" + details + execs + "</div>"
            "</details>")


def render(report: dict) -> str:
    """Render one captured observed-evidence report dict to HTML."""
    if not isinstance(report, dict) \
            or report.get("schema") != REPORT_SCHEMA:
        raise ValueError(
            f"report schema must be {REPORT_SCHEMA!r}")
    summary = report.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    source = report.get("source")
    source = source if isinstance(source, dict) else {}
    policy = report.get("policy")
    policy = policy if isinstance(policy, dict) else {}
    panels = "".join(
        f'<div class="fact"><div class="num mono">{_esc(summary.get(k))}'
        f'</div><div class="lbl">{_esc(label)}</div></div>'
        for k, label in _FACT_PANELS)
    panels += "".join(
        f'<div class="fact"><div class="num mono">{_esc(summary.get(k))}'
        f'</div><div class="lbl">{_esc(label)}</div></div>'
        for k, label in _OPTIONAL_FACTS if k in summary)
    notes = "".join(
        f'<li>{_esc(label)}: {"yes" if summary.get(k) else "no"}</li>'
        for k, label in _SUMMARY_FLAGS)
    policy_block = (
        '<div class="exec"><h4>Policy</h4>'
        + _kv("version", policy.get("version"))
        + _kv("provider mode", policy.get("provider_mode"))
        + _kv("priority description", policy.get("priority_description"))
        + _kv("source sha256", policy.get("source_sha256"))
        + "</div>")
    coverage_block = _json_details(
        "Coverage (verbatim from report)", report.get("coverage"))
    pipeline_block = (_json_details("Pipeline (verbatim from report)",
                                    report["pipeline"])
                      if "pipeline" in report else "")
    provenance_block = (_json_details(
        "Diagnostic provenance (verbatim from report)",
        report["diagnostic_provenance"])
        if "diagnostic_provenance" in report else "")
    research_note = (
        '<p class="grp">Private research replay details below are frozen '
        'diagnostic joins and stage bookkeeping, not student-facing '
        'feedback text.</p>')
    scenario_sections = []
    scenarios = report.get("scenarios")
    for scenario in scenarios if isinstance(scenarios, list) else []:
        if not isinstance(scenario, dict):
            continue
        packages = scenario.get("packages")
        items = [p for p in packages if isinstance(p, dict)] \
            if isinstance(packages, list) else []
        extras = "".join(
            _json_details(title, scenario[key])
            for key, title in (
                ("pipeline_stages", "Pipeline stages"),
                ("assessment_graph", "Assessment graph (frozen replay)"),
                ("research_diagnostics",
                 "Research diagnostics (private, not learner advice)"))
            if key in scenario)
        extra_block = (research_note + extras) if extras else ""
        scenario_sections.append(
            '<section class="scenario"><h2>'
            f'{_esc(scenario.get("name", ""))}</h2>'
            f'<p class="grp">group <span class="mono">'
            f'{_esc(scenario.get("group", ""))}</span></p>'
            + extra_block
            + "".join(_package_details(p) for p in items)
            + "</section>")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Observed-Evidence Feedback Review</title>
<style>
body{{margin:0;background:#faf6ec;color:#21352a;
 font-family:Georgia,'Times New Roman',serif;line-height:1.5}}
header{{background:#1f4a33;color:#f4efe0;padding:2rem 1.25rem 1.5rem;
 border-bottom:4px solid #8fae8f}}
h1,h2,h3{{font-family:Georgia,serif;font-weight:600;color:#1f4a33}}
header h1{{color:#f4efe0;margin:0 0 .5rem}}
.banner{{display:inline-block;background:#f4efe0;color:#1f4a33;
 padding:.35rem .8rem;border:2px solid #8fae8f;border-radius:4px;
 font-weight:700;margin:0}}
.scope{{color:#cfe0cf;font-size:.95rem;margin:.9rem 0 0;
 overflow-wrap:anywhere}}
main{{max-width:1100px;margin:0 auto;padding:1.25rem}}
.mono{{font-family:Consolas,'Courier New',monospace}}
.facts{{display:grid;grid-template-columns:repeat(auto-fit,
 minmax(160px,1fr));gap:.75rem;margin:1.25rem 0}}
.fact{{background:#fffdf4;border:1px solid #c9d6c2;border-top:3px solid
 #1f4a33;border-radius:6px;padding:.8rem}}
.fact .num{{font-size:1.6rem;color:#1f4a33}}
.fact .lbl{{font-size:.8rem;color:#4a5f50}}
.reuse{{background:#f1ead6;border-left:4px solid #8fae8f;
 padding:.8rem 1rem;margin:1.25rem 0;border-radius:0 6px 6px 0}}
.reuse p,.reuse ul{{margin:.4rem 0}}
.scenario{{margin:1.5rem 0}}
.scenario h2{{border-bottom:2px solid #8fae8f;padding-bottom:.2rem;
 overflow-wrap:anywhere}}
.grp{{color:#4a5f50;font-size:.9rem;margin:.2rem 0 .6rem}}
details.pkg{{background:#fffdf4;border:1px solid #c9d6c2;
 border-radius:6px;margin:.6rem 0}}
details.pkg summary{{padding:.7rem .9rem;cursor:pointer;list-style:none}}
details.pkg summary::before{{content:'\\25B8 ';color:#1f4a33}}
details.pkg[open] summary::before{{content:'\\25BE '}}
.chip{{background:#eef3ea;border:1px solid #b7c9b4;border-radius:4px;
 padding:.15rem .5rem;font-size:.85rem;color:#1f4a33}}
.pkgbody{{padding:0 .9rem .9rem}}
.kv{{display:flex;gap:.6rem;padding:.15rem 0;font-size:.92rem}}
.kv .k{{min-width:280px;color:#4a5f50}}
.kv .v{{color:#21352a;overflow-wrap:anywhere}}
.compare{{display:grid;grid-template-columns:1fr 1fr;gap:.9rem;
 margin:.8rem 0}}
.col{{background:#f6f2e2;border:1px solid #ddd3b8;border-radius:6px;
 padding:.7rem .9rem}}
.col h4{{margin:.1rem 0 .4rem;color:#1f4a33}}
.sec{{margin:.3rem 0;font-size:.9rem}}
.kind{{background:#eef3ea;border:1px solid #b7c9b4;border-radius:4px;
 padding:0 .4rem;font-size:.75rem;color:#1f4a33}}
.exec{{background:#eef3ea;border:1px solid #b7c9b4;border-radius:6px;
 padding:.6rem .8rem;margin:.6rem 0}}
.exec h4{{margin:.1rem 0 .4rem;color:#1f4a33}}
.execrow{{display:grid;grid-template-columns:1fr 1fr;gap:.9rem}}
details.raw{{margin:.5rem 0}}
details.raw summary{{cursor:pointer;font-size:.9rem;color:#1f4a33}}
details.raw pre{{background:#fffdf4;border:1px solid #ddd3b8;
 border-radius:6px;padding:.6rem;overflow-x:auto;font-size:.8rem}}
footer{{margin:2rem 0 1rem;font-size:.85rem;color:#4a5f50;
 border-top:1px solid #c9d6c2;padding-top:.8rem}}
@media(max-width:720px){{.compare,.execrow{{grid-template-columns:1fr}}
 .kv .k{{min-width:0;width:45%}}}}
</style></head>
<body><header><h1>Observed-Evidence Feedback Review</h1>
<p class="banner">Synthetic research drafts - human review required</p>
<p class="scope">Source <span class="mono">
{_esc(source.get("file", ""))}</span>
(sha256 <span class="mono">{_esc(source.get("sha256", ""))}</span>)
&middot; bank sha256 <span class="mono">
{_esc(source.get("bank_sha256", ""))}</span>
&middot; taxonomy sha256 <span class="mono">
{_esc(source.get("taxonomy_sha256", ""))}</span></p>
</header><main>
<section class="facts">{panels}</section>
<section class="reuse"><ul>{notes}</ul></section>
{policy_block}
{pipeline_block}
{provenance_block}
{coverage_block}
{"".join(scenario_sections)}
<footer>Rendered mechanically from a captured report. All feedback
packages require human review. Jev probabilities and confidence are not
used as educational approval. When included, KT/conformal values are
private frozen research diagnostics, not student feedback.</footer>
</main></body></html>"""


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render an observed-evidence feedback report JSON "
                    "to a local HTML review page (no network; output "
                    "file created exclusively, never overwritten).")
    parser.add_argument("report", type=Path,
                        help="captured report.json path")
    parser.add_argument("--out", type=Path, required=True,
                        help="HTML output path (must not exist)")
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        page = render(report)
    except (OSError, UnicodeError, json.JSONDecodeError,
            ValueError) as exc:
        parser.error(f"cannot render report: {exc}")
        return
    try:
        with args.out.open("x", encoding="utf-8") as handle:
            handle.write(page)
    except OSError as exc:
        parser.error(f"cannot write output: {exc}")


if __name__ == "__main__":
    main(sys.argv[1:])
