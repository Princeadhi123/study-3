"""Render a frozen provider-replay report to a local HTML review page.

Pure stdlib presentation: reads ONLY the supplied captured report JSON
(never the bank, sessions, caches, or providers), emits a self-contained
HTML file with no JavaScript and no external assets. The output file is
created exclusively (``x`` mode) and never overwrites an existing file.
Every dynamic string is HTML-escaped. This is a mechanical renderer for
human review; it performs no recomputation, no evaluation, and no
quality judgments.
"""
import argparse
import html
import json
import sys
from pathlib import Path

REPORT_SCHEMA = "phase3_provider_replay_review_v1"

_ESCAPE = html.escape


def _esc(value) -> str:
    return _ESCAPE(str(value))


_FACT_PANELS = (
    ("scenario_labels", "Scenario labels"),
    ("feedback_packages", "Feedback packages"),
    ("end_packages", "End packages"),
    ("end_selection_matches_rules",
     "End selections matching rules baseline"),
    ("new_provider_calls_this_invocation",
     "New provider calls (this invocation)"),
    ("fallback_packages", "Packages with fallback"),
)

_NOT_TESTED = (
    ("independent_provider_rerun_stability_tested",
     "Independent provider rerun stability"),
    ("educator_preference_labels_available",
     "Educator preference labels"),
    ("educational_effectiveness_tested",
     "Educational effectiveness"),
)


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
    metadata = execution.get("metadata")
    if isinstance(metadata, dict):
        usage = metadata.get("usage")
        if isinstance(usage, dict):
            usage_text = ", ".join(f"{k}={v}" for k, v in usage.items())
            rows.append(
                f'<div class="kv"><span class="k">usage</span>'
                f'<span class="v mono">{_esc(usage_text)}</span></div>')
        for key in ("model_version", "prompt_version", "status"):
            if key in metadata and metadata[key] is not None:
                rows.append(
                    f'<div class="kv"><span class="k">{_esc(key)}</span>'
                    f'<span class="v mono">{_esc(metadata[key])}</span>'
                    f'</div>')
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
    selected_id = review.get("selected_candidate_id")
    baseline_id = baseline.get("candidate_id")
    audience = _esc(package.get("audience", ""))
    checkpoint = _esc(package.get("checkpoint", ""))
    head = (f'<summary><span class="chip mono">{audience} '
            f'{checkpoint}</span> '
            f'<span class="chip mono">selected: {_esc(selected_id)}</span>'
            f'</summary>')
    meta = [
        f'<div class="kv"><span class="k">selected candidate</span>'
        f'<span class="v mono">{_esc(selected_id)}</span></div>',
        f'<div class="kv"><span class="k">baseline candidate</span>'
        f'<span class="v mono">{_esc(baseline_id)}</span></div>',
        f'<div class="kv"><span class="k">fallback reason</span>'
        f'<span class="v mono">'
        f'{_esc(trace.get("fallback_reason"))}</span></div>',
        f'<div class="kv"><span class="k">requires human review</span>'
        f'<span class="v mono">'
        f'{_esc(review.get("requires_human_review"))}</span></div>',
    ]
    evidence = (review.get("sanitized_evidence")
                if isinstance(review.get("sanitized_evidence"), dict)
                else {})
    total = evidence.get("total")
    if isinstance(total, dict) and "correct" in total \
            and "out_of" in total:
        meta.append(
            f'<div class="kv"><span class="k">observed end total</span>'
            f'<span class="v mono">{_esc(total["correct"])} of '
            f'{_esc(total["out_of"])}</span></div>')
    if trace.get("latency_ms") is not None:
        meta.append(
            '<div class="kv"><span class="k">Replay wall time (includes '
            'cache overhead; not hosted latency)</span>'
            f'<span class="v mono">{_esc(trace["latency_ms"])} ms</span>'
            '</div>')
    texts = (
        '<div class="compare">'
        '<div class="col"><h4>Deterministic baseline</h4>'
        f'<p>{_esc(baseline.get("text", ""))}</p></div>'
        '<div class="col"><h4>Provider path</h4>'
        f'<p>{_esc(message.get("text", ""))}</p></div>'
        '</div>')
    execs = (_execution_block("Selector execution",
                              package.get("selector_execution"))
             + _execution_block("Generator execution",
                                package.get("generator_execution")))
    return ("<details class=\"pkg\">" + head
            + '<div class="pkgbody">' + "".join(meta) + texts
            + '<div class="execrow">' + execs + "</div></div>"
            "</details>")


def render(report: dict) -> str:
    """Render one captured replay report dict to an HTML string."""
    if not isinstance(report, dict) \
            or report.get("schema") != REPORT_SCHEMA:
        raise ValueError(
            f"report schema must be {REPORT_SCHEMA!r}")
    summary = report.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    source = report.get("source")
    source = source if isinstance(source, dict) else {}
    panels = "".join(
        f'<div class="fact"><div class="num mono">{_esc(summary.get(k))}'
        f'</div><div class="lbl">{_esc(label)}</div></div>'
        for k, label in _FACT_PANELS)
    notes = "".join(
        f'<li>{_esc(label)}: {"yes" if summary.get(k) else "no"}</li>'
        for k, label in _NOT_TESTED)
    groups = {}
    packages = report.get("packages")
    for package in packages if isinstance(packages, list) else []:
        if isinstance(package, dict):
            groups.setdefault(package.get("scenario", "unknown"),
                              []).append(package)
    scenario_sections = []
    for scenario, items in groups.items():
        scenario_sections.append(
            f'<section class="scenario"><h2>{_esc(scenario)}</h2>'
            + "".join(_package_details(p) for p in items)
            + "</section>")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Frozen Scenario Review</title>
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
.scope{{color:#cfe0cf;font-size:.95rem;margin:.9rem 0 0}}
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
.scenario h2{{border-bottom:2px solid #8fae8f;padding-bottom:.2rem}}
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
.kv .v{{color:#21352a}}
.compare{{display:grid;grid-template-columns:1fr 1fr;gap:.9rem;
 margin:.8rem 0}}
.col{{background:#f6f2e2;border:1px solid #ddd3b8;border-radius:6px;
 padding:.7rem .9rem}}
.col h4{{margin:.1rem 0 .4rem;color:#1f4a33}}
.execrow{{display:grid;grid-template-columns:1fr 1fr;gap:.9rem}}
.exec{{background:#eef3ea;border:1px solid #b7c9b4;border-radius:6px;
 padding:.6rem .8rem}}
.exec h4{{margin:.1rem 0 .4rem;color:#1f4a33}}
footer{{margin:2rem 0 1rem;font-size:.85rem;color:#4a5f50;
 border-top:1px solid #c9d6c2;padding-top:.8rem}}
@media(max-width:720px){{.compare,.execrow{{grid-template-columns:1fr}}
 .kv .k{{min-width:0;width:45%}}}}
</style></head>
<body><header><h1>Frozen Scenario Review</h1>
<p class="banner">Synthetic research drafts - human review required</p>
<p class="scope">Scope: {_esc(report.get("scope", ""))} &middot; source
snapshot <span class="mono">{_esc(source.get("file", ""))}</span>
(sha256 <span class="mono">{_esc(source.get("sha256", ""))}</span>)</p>
</header><main>
<section class="facts">{panels}</section>
<section class="reuse">
<p>Provider matches shown here are not independent repeated calls:
identical effective requests share captured responses, including
failures (policy: <span class="mono">{_esc(report.get("reuse_policy",
""))}</span>). Aitta receives only audience, checkpoint, and the selected
candidate's id, strategy, and review status &mdash; never scores or
topic names &mdash; so reused openings are not separate generations.
End selections matching the rules baseline are agreements, not evidence
of an advantage.</p>
<p><span class="mono">{_esc(report.get("latency_warning", ""))}</span></p>
<ul>{notes}</ul></section>
{"".join(scenario_sections)}
<footer>Rendered mechanically from a frozen capture. All packages are
draft_not_for_learner_delivery and require human review. No model
probabilities, confidence scores, or quality judgments are shown.</footer>
</main></body></html>"""


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render a frozen provider-replay report JSON to a "
                    "local HTML review page (no network; output file "
                    "created exclusively, never overwritten).")
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
