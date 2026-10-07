"""Render the frozen practice-review pack into one offline HTML page.

Presentation only: embeds reviewer/review_pack.json verbatim and reads the
pack hash from investigator/manifest.json. No experiment data is recomputed.
"""
import argparse
import hashlib
import json
from pathlib import Path

CAPTURE = Path(__file__).parent / "artifacts" / "practice_comparison_20261006"
PACK = CAPTURE / "reviewer" / "review_pack.json"
MANIFEST = CAPTURE / "investigator" / "manifest.json"
DEFAULT_OUTPUT = CAPTURE / "reviewer" / "index.html"

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Practice Recommendation Review</title>
<style>
:root { --cream: #faf6ee; --teal: #0f6b66; --teal-dim: #e3efec; --ink: #222; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--cream); color: var(--ink);
       font-family: Georgia, "Times New Roman", serif; line-height: 1.45; }
header { background: var(--teal); color: #fff; padding: 1.2rem 1.5rem; }
header h1 { margin: 0 0 .3rem; font-size: 1.4rem; }
header p { margin: .15rem 0; font-size: .95rem; }
main { max-width: 1100px; margin: 0 auto; padding: 1rem 1.5rem 3rem; }
.task { background: #fff; border: 1px solid #d8d0bd; border-radius: 6px;
        margin: 1.2rem 0; padding: 1rem 1.2rem; }
.task h2 { font-size: 1.05rem; margin: 0 0 .5rem; color: var(--teal); }
.counts { border-collapse: collapse; margin: .4rem 0 .8rem; }
.counts th, .counts td { border: 1px solid #d8d0bd; padding: .25rem .7rem;
        text-align: left; font-size: .92rem; }
details { margin: .5rem 0 1rem; }
details summary { cursor: pointer; color: var(--teal); }
table.answers { border-collapse: collapse; width: 100%; font-size: .85rem; }
table.answers th, table.answers td { border: 1px solid #e0d8c4;
        padding: .2rem .45rem; text-align: left; vertical-align: top; }
.wrong { background: #f6e3dd; }
.alts { display: flex; gap: 1rem; flex-wrap: wrap; }
.alt { flex: 1 1 320px; background: var(--teal-dim); border: 1px solid #b7cfc9;
       border-radius: 6px; padding: .8rem 1rem; }
.alt h3 { margin: 0 0 .4rem; font-size: 1rem; }
.alt .stem { font-weight: bold; margin: .3rem 0; }
.alt ol { margin: .3rem 0 .5rem; padding-left: 1.4rem; }
fieldset { border: none; margin: .6rem 0 0; padding: 0; }
.dim { margin: .45rem 0; }
.dim label { display: block; font-size: .88rem; margin-bottom: .15rem; }
select, input[type=text], textarea { font-family: inherit; font-size: .92rem;
        padding: .25rem .4rem; border: 1px solid #b9b09a; border-radius: 4px;
        background: #fff; }
textarea { width: 100%; min-height: 3.2rem; }
.pref label { margin-right: 1rem; font-size: .92rem; }
footer-bar { display: block; }
.toolbar { position: sticky; bottom: 0; background: var(--cream);
        border-top: 1px solid #d8d0bd; padding: .8rem 0; display: flex;
        gap: .8rem; align-items: center; flex-wrap: wrap; }
button { background: var(--teal); color: #fff; border: none; border-radius: 4px;
        padding: .5rem 1rem; font-family: inherit; font-size: .95rem;
        cursor: pointer; }
.note { font-size: .85rem; color: #555; }
</style>
</head>
<body>
<header>
  <h1>Practice Recommendation Review</h1>
  <p>Scripted synthetic answers, not real learner data.</p>
  <p>Judge independently. Source-masked, not guaranteed blinded.</p>
  <p>Use uncertain when evidence is insufficient; absent questions use
     not_applicable on question-specific dimensions.</p>
</header>
<main>
  <div class="toolbar" style="position:static;border-top:none;padding:0 0 .6rem">
    <label>Reviewer code (anonymous):
      <input type="text" id="reviewer" autocomplete="off" placeholder="e.g. R-1">
    </label>
  </div>
  <div id="tasks"></div>
  <div class="toolbar">
    <button id="export">Export My Judgments</button>
    <span class="note">Partial judgments remain null; no scores or approval inferred.</span>
  </div>
</main>
<script>
"use strict";
const PACK = __PACK_JSON__;
const PACK_SHA256 = "__PACK_SHA__";
const rubric = PACK.rubric;
const tasks = PACK.tasks;
const prefs = PACK.preference_choices;
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function dimControls(taskId, side) {
  return rubric.map(d => {
    const id = `${taskId}__${side}__${d.id}`;
    const opts = ['<option value=""></option>']
      .concat(d.choices.map(c => `<option value="${esc(c)}">${esc(c)}</option>`))
      .join("");
    return `<div class="dim"><label for="${id}">${esc(d.question)}</label>` +
      `<select id="${id}">${opts}</select></div>`;
  }).join("");
}

function questionHtml(alt) {
  const q = alt.suggested_question;
  if (!q) return `<p class="stem">${esc(alt.notice)}</p>`;
  const options = (q.options || []).map(o => `<li>${esc(o)}</li>`).join("");
  return `<p class="note">${esc(alt.notice)}</p>` +
    `<p class="note">${esc(q.topic)}</p>` +
    `<p class="stem">${esc(q.text)}</p><ol>${options}</ol>`;
}

function taskHtml(task) {
  const counts = task.assessment_summary.map(r =>
    `<tr><td>${esc(r.topic)}</td><td>${r.correct} / ${r.out_of}</td></tr>`)
    .join("");
  const rows = task.assessment_answers.map(a =>
    `<tr class="${a.correct ? "" : "wrong"}"><td>${a.position}</td>` +
    `<td>${esc(a.topic)}</td><td>${esc(a.text)}</td>` +
    `<td>${esc(a.options.join(" | "))}</td>` +
    `<td>${esc(a.selected_option)}</td><td>${a.correct ? "correct" : "wrong"}</td></tr>`)
    .join("");
  const alts = task.alternatives.map(alt =>
    `<div class="alt"><h3>Alternative ${esc(alt.label)}</h3>` +
    questionHtml(alt) +
    `<fieldset>${dimControls(task.task_id, alt.label)}</fieldset></div>`)
    .join("");
  const pref = prefs.map(c =>
    `<label><input type="radio" name="${task.task_id}__pref" ` +
    `value="${esc(c)}"> ${esc(c)}</label>`).join("");
  return `<section class="task" id="${esc(task.task_id)}">` +
    `<h2>Case ${esc(task.task_id)}</h2>` +
    `<p class="note">${esc(task.data_notice)}</p>` +
    `<table class="counts"><tr><th>Topic</th><th>Correct</th></tr>${counts}</table>` +
    `<details><summary>Forty assessment answers</summary>` +
    `<table class="answers"><tr><th>#</th><th>Topic</th><th>Question</th>` +
    `<th>Options</th><th>Selected</th><th></th></tr>${rows}</table></details>` +
    `<div class="alts">${alts}</div>` +
    `<fieldset class="pref"><legend>Paired preference</legend>${pref}</fieldset>` +
    `<div class="dim"><label>Explanation / uncertainty / prior exposure</label>` +
    `<textarea id="${task.task_id}__note"></textarea></div>` +
    `</section>`;
}

document.getElementById("tasks").innerHTML = tasks.map(taskHtml).join("");

function collect() {
  const reviewer = document.getElementById("reviewer").value.trim();
  if (!reviewer) { alert("A reviewer code is required before export."); return null; }
  const reviews = tasks.map(t => {
    const alternatives = {};
    for (const alt of t.alternatives) {
      const dims = {};
      for (const d of rubric) {
        const el = document.getElementById(`${t.task_id}__${alt.label}__${d.id}`);
        dims[d.id] = el && el.value ? el.value : null;
      }
      alternatives[alt.label] = dims;
    }
    const pref = document.querySelector(
      `input[name="${t.task_id}__pref"]:checked`);
    const note = document.getElementById(`${t.task_id}__note`).value;
    return {task_id: t.task_id, alternatives,
            preference: pref ? pref.value : null, note: note};
  });
  return {schema: "source_masked_practice_ratings_v1",
          review_pack_sha256: PACK_SHA256, reviewer_id: reviewer,
          reviews: reviews};
}

document.getElementById("export").addEventListener("click", () => {
  const data = collect();
  if (!data) return;
  const safe = data.reviewer_id.replace(/[^A-Za-z0-9_-]/g, "_");
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const blob = new Blob([JSON.stringify(data, null, 2)],
                        {type: "application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `practice_review_${safe}_${stamp}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
});
</script>
</body>
</html>
"""


def render(pack, pack_sha256):
    embedded = (json.dumps(pack, ensure_ascii=False)
                .replace("<", "\\u003c").replace(">", "\\u003e")
                .replace("&", "\\u0026"))
    return (HTML_TEMPLATE
            .replace("__PACK_JSON__", embedded)
            .replace("__PACK_SHA__", pack_sha256))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    raw = PACK.read_bytes()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if hashlib.sha256(raw).hexdigest() != manifest["review_pack_sha256"]:
        raise SystemExit("review pack hash changed")
    pack = json.loads(raw.decode("utf-8"))
    pack_sha = manifest["review_pack_sha256"]
    target = args.output or DEFAULT_OUTPUT
    if target.exists() and args.output is None:
        raise SystemExit(f"refusing to overwrite existing {target}; "
                         "pass --output for a different path")
    if target.exists() and args.output is not None:
        raise SystemExit(f"refusing to overwrite existing {target}")
    html_text = render(pack, pack_sha)
    target.write_text(html_text, encoding="utf-8")
    print(f"wrote {target} ({len(html_text)} bytes)")


if __name__ == "__main__":
    main()
