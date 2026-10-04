# Evidence Lab — Phase 3 synthetic assessment and educator review

Phase 3 is the integration layer for a fixed 40-question mathematics
assessment. The current project is an interactive localhost prototype with:

- a student assessment page;
- a private teacher dashboard;
- deterministic scoring and observed evidence;
- a descriptive assessment graph;
- optional hosted Jev candidate selection;
- optional hosted Aitta opening phrasing;
- private frozen KT and conformal research diagnostics;
- educator review records and JSON export.

All sessions are synthetic and all generated feedback is a draft requiring
educator review. This is not a real student pilot, a validated mastery
assessment, or an approved learner-delivery system.

## 1. Current status

Implemented:

- `demo_api.py` localhost HTTP boundary;
- `web_demo/index.html` student flow;
- `web_demo/teacher.html` educator dashboard;
- `demo_service.py` session orchestration, persistence, async provider jobs,
  diagnostics jobs, review records, and teacher payloads;
- fixed-order 40-question assessment over the approved Phase 2 bank;
- midpoint and end checkpoints at responses 20 and 40;
- deterministic baseline feedback with Jev/Aitta fallback;
- provider execution provenance, including fresh/cached/local/fallback states;
- English display labels and prototype question translations;
- poll-safe dashboard disclosure and review-form state;
- responsive Evidence Lab assessment and educator workspace, keyboard-accessible
  session controls, session search, and explicit synthetic-response consent;
- a searchable 54-case Scenario Library with plain-language names and shared
  live/session detail views;
- asynchronous selected, filtered, or full-set replay of original answers through
  the current pipeline, with run history, cancellation, version selection, and
  original-versus-replay changes;
- a source-masked A/B educator-review protocol with persisted randomization,
  immutable judgments, resume, and provenance-bound JSON export.

Pending:

- independent educator recruitment, protocol review, and preference evaluation;
- richer, bounded evidence-grounded feedback composition;
- a reviewed practice-question/worked-solution bank;
- calibrated KT/conformal validity for this fixed assessment;
- production authentication, storage, privacy review, and deployment.

The detailed architecture, retained metrics, verification evidence, and
prioritized roadmap are in `README_STATUS_ARCHITECTURE_ROADMAP.md`.

## 2. Assessment and evidence model

The assessment uses the approved Phase 2 bank:

```text
../kt_phase2_inference/artifacts/test_question_bank_text_only_approved_v2.json
```

The bank contains private answer keys. It is referenced in place and must not
be copied into student payloads or public artifacts.

Assessment structure:

- four skills: Arithmetic, Prices, Fractions, Percentages;
- ten questions per skill in a fixed order;
- midpoint after 20 answers and end after 40;
- 15 descriptive subtopics from `assessment_taxonomy_draft.json`.

The taxonomy relation is `is_part_of_not_prerequisite`. It groups questions by
assessed content; it does not establish prerequisites, mastery,
misconceptions, fatigue, attention, or future performance.

## 3. Run the interactive demo

Run commands from this repository folder. Do not start a second instance while
one is already listening on the same port.

Rules-only mode makes no provider calls:

```powershell
python demo_api.py --host 127.0.0.1 --port 8766 --providers rules
```

Hosted mode is optional and requires a local Jev key file:

```powershell
python demo_api.py --host 127.0.0.1 --port 8766 --providers hosted `
  --jev-env-file "C:/path/to/jev.env" `
  --call-budget 12 --provider-timeout 120
```

Aitta is configured separately through environment variables or the
workspace-root `.env` file (`AITTA_API_KEY`, `AITTA_BASE_URL`, optional
`AITTA_MODEL`) — not a Phase 3-local `.env`. Do not print or commit those
values.

`--teacher-pin` may supply a six-digit local demo PIN. If omitted, the server
generates one and prints it at startup. Do not commit the PIN.

Pages:

- Student: `http://127.0.0.1:8766/`
- Teacher: `http://127.0.0.1:8766/teacher`

The server accepts only loopback hosts and rejects non-local Host or Origin
values. The teacher PIN is local route separation, not production
authentication.

## 4. Student and educator flow

### Student page

- requires the `synthetic: true` attestation;
- issues a per-session owner token;
- serves key-free question payloads;
- accepts ordered `{question_id, selected_index}` responses;
- hides scores at midpoint;
- shows deterministic baseline feedback immediately at the end;
- updates the end page if provider-backed feedback later becomes ready.

### Teacher dashboard

The teacher page has three workspaces: **Live sessions**, **Scenario library**,
and **Blind comparison**. Live sessions and scenarios share the same six-tab
renderer (including separate review-draft and disclosure state):

1. Summary — session status, counts, job state, provenance, and replay changes;
2. Answers & scores — observed skill/subtopic totals and private question records;
3. Skill map — descriptive hierarchy and observed counts;
4. Model diagnostics — KT probability charts and conformal checkpoint tables;
5. Feedback drafts — baseline versus selected drafts, candidate selection,
   execution provenance, and halfway student feedback;
6. Educator review — version-bound review fields, notes, and saved review ledger.

Names describe the answer pattern, for example “Errors in Fractions only” or
“High correct-answer probability · Example 1”. Seeds and technical identifiers
remain in audit records rather than primary labels. Repeated live sessions are
distinguished by their creation timestamps. The random seed control is under
Reproducibility settings.

Teacher actions include synthetic simulation profiles, JSON export, and
logout. Simulation profiles are response patterns, not learner diagnoses.

The dashboard polls approximately every two seconds. Unchanged payloads skip
redraws; expanded sections and unsaved review edits are preserved across real
updates.

### Scenario Library

The library reads `artifacts/integrated_feedback_20261002_hosted_full/report.json`
by default. Override it with `--replay-report PATH`. It loads the report lazily
and pins that snapshot for the server process; restart to use a different file
revision. Missing or malformed reports show an unavailable state without
blocking live sessions. Opening the library makes **no provider or model calls**.

Search by case/skill; filter by scenario group or selection agreement; sort by
name or observed score. Each case exposes checkpoint/audience packages,
observed skill/subtopic counts, baseline and selected text, execution provenance,
and private frozen graph/diagnostics. Summary counts are derived from packages,
not copied from the report summary. Selection differences are not quality scores.
Original results are read-only. Individual answers are joined from the matching
private source and bank; if those inputs are unavailable or incompatible, the UI
says so instead of inventing answers. Historical diagnostics stay labelled as saved
inference, not newly computed results.

### Research status guide

The library shows two always-visible boundary notes — observed counts describe
this assessment only, and all scenarios are synthetic test cases — followed by a
collapsed **Research status & next steps** disclosure (`#research-readiness`).
It is static project-wide guidance, not a per-scenario diagnosis or readiness
score, and opening it reveals no drafts, candidate IDs, or scenario mappings.
Six cards summarize, for each component — knowledge tracing, conformal
prediction, feedback focus/wording, the skill map, synthetic replay, and the
blind educator comparison — what it does, why it remains research-only, and the
validation still needed. KT already has recorded predictive evaluation on real
ViLLE interactions and the 40-question bank is source-derived, but existing
results do not establish mastery or calibration in this fixed session setting,
and the deployed checkpoint was selected partly using cold-item evaluation
scores — so that split is not an untouched final test. The stated next step is
to audit existing ViLLE sessions for suitable held-out answers (with only
preceding history per prediction) and evaluate fixed-assessment prediction and
calibration on a set not used for training or model selection, collecting new
learner data only if existing data cannot support the intended claim. Remaining
steps: empirical coverage checks for the intended population, independent
educator review, a reviewed practice-activity bank, taxonomy/prerequisite
review, a consented privacy-reviewed learner study, and a preregistered
comparison protocol. A closing note restates that educator approval, privacy
review, real access controls, TLS, and durable storage are required before any
real learner use. The research diagnostics tab similarly states that KT was
trained and predictively evaluated on real ViLLE data, that it estimates
correctness rather than mastery, that simulated sessions are not additional
real-learner validation, and that conformal ranges are not guarantees for this
assessment.

### Replay scenarios after a change

1. Restart the server after changing Python pipeline code. Replay refuses admission
   if tracked Phase 3/Phase 2 source files differ from their startup fingerprints.
   Refresh the page after UI changes; Python is not hot-reloaded.
2. In Scenario Library, choose **Replay this scenario**, **Replay filtered scenarios**,
   or **Replay all 54 scenarios**. Selected-case replay preserves the active detail
   tab, so you can watch the same view on the new result.
3. Confirm execution mode. **Rules only** makes no hosted calls, even when the server
   is configured for hosted providers. Hosted replay requires explicit consent,
   shares the server's existing call budget and serialized provider worker, and
   retains effective-request cache reuse. It does not force fresh independent calls.
4. Follow Replay history. One batch runs at a time, one case at a time. Each case
   resubmits its exact original 40 question/option-index pairs through `DemoService`,
   recomputing scores, observed evidence, maps, feedback, and requesting fresh
   frozen-model/conformal diagnostics at 20 and 40 answers. No retraining occurs.
   Fallbacks, unavailable diagnostics, and failed cases remain visibly labelled.
5. Select **Result version** to switch between the retained original and new runs.
   Summary shows score changes, changed candidate/text per audience, before/after
   wording, and maximum KT probability difference where both traces are available.
   These comparisons are against the original report, not evidence of improvement.
6. Add reviews to a replayed result through Educator review. Export an individual
   result or the complete batch record. **Stop after current scenario** lets admitted
   provider/model work finish and prevents further cases from starting.

Original answers default to `artifacts/assessment_pipeline_20261001.json`; override
with `--replay-source PATH`. Its raw-file hash must match the selected report's
source hash, and its bank, question order, indices, and observed totals must agree.
A different bank requires a newly validated compatible source/report, not reinterpretation
of old option indices.

Runs are stored under `--root/replay_runs/`, with full completed-result snapshots in
`replay_runs/results/`; their sessions/metadata reuse the normal private store but
are excluded from the Live sessions list. Run records include code/source fingerprints,
status, session links, and changes. Exact start-request retries are idempotent. Server
restarts mark unfinished batches interrupted; they never automatically repeat provider
calls. Completed snapshots and saved review notes remain readable if a later policy,
taxonomy, or bank makes the live metadata incompatible; those older snapshots become
read-only. Nothing rewrites retained reports or retargets existing blind comparisons.

### Source-masked educator comparison

For less-biased review, complete **Blind comparison before browsing drafts**.
Use a pseudonymous reviewer code, select one audience, and declare prior exposure.
The server shuffles all cases for that audience and balances baseline placement
between A and B (within one for odd case counts). The actual allocation is stored,
not reconstructed from a browser seed. Identical-text pairs remain in the review.

Only the current task's observed evidence and A/B text reach the blind-review
response. Case names, candidate IDs, source mapping, execution records, and hashes
remain withheld until **all judgments are saved**. Record preference (A/B/tie/neither),
evidence support for each draft, confidence, and optional rationale. Judgments are
immutable; identical retries are idempotent. Leave and resume through All reviews.

Completed reviews expose descriptive preference counts and a private JSON export
with exact draft text, assignments, judgments, timestamps, report/message/evidence/
package hashes, policy identity, and provider provenance. Records live under
`--root/comparisons/`, separate from live-session reviews and historical reports.
In-progress reviews from a different report revision cannot accept new judgments;
restore the original report and restart, or start a separate review. Completed
records remain exportable even when the original report is unavailable.

This is **pilot review tooling, not a completed or validated benchmark**. Exposure
is self-reported at review start (the UI also remembers opened drafts in the browser
tab); other tabs, local files, later browsing, or writing style can defeat blinding.
The teacher role can access unblinded library routes. Use separate reviewer sessions
and a supervised protocol for a real study. Do not treat correlated scenarios,
duplicate response patterns, cached captures, or repeated reviewer sessions as
independent trials. No inferential significance or provider-superiority claim is
computed. File persistence is single-process local storage, not a production or
tamper-proof research database. A completed review never approves learner delivery.

## 5. Runtime architecture

```text
browser UI (index.html / teacher.html / app.js)
        |
        | loopback JSON only
        v
demo_api.py
        |
DemoService (demo_service.py)
        |
        +-- ordered scoring + observed evidence + descriptive graph
        |
        +-- feedback branch
        |       deterministic baseline
        |       -> optional Jev candidate selection
        |       -> optional Aitta opening
        |       -> draft requiring educator review
        |
        +-- research branch
                frozen KT inference
                -> historical conformal application
                -> private teacher diagnostics
```

Checkpoint behavior:

- provider and diagnostics work is queued only at responses 20 and 40;
- midpoint student feedback is deterministic and neutral;
- at the end, baseline feedback is available while provider work is pending;
- diagnostics snapshot the relevant answer prefix so later answers cannot
  race an already-queued checkpoint job;
- a restart converts an interrupted provider job to deterministic fallback and
  interrupted diagnostics to `interrupted_by_restart`;
- opening a completed session reads its persisted results rather than
  automatically rerunning providers or inference.

## 6. Provider boundaries

Jev:

- chooses only among permitted observed-evidence-supported candidates;
- receives allow-listed aggregate skill/subtopic evidence and candidates;
- does not receive raw responses, question text, answer keys, session IDs, or
  research probabilities;
- is bypassed locally when only one candidate is permitted;
- failure falls back to the deterministic baseline.

Aitta:

- writes only the opening sentence;
- does not author factual counts, mathematical explanations, or method-review
  actions;
- receives a narrow audience/checkpoint communication context rather than the
  actual evidence packet;
- failure preserves the selected candidate with deterministic opening text.

`CaptureCache` reuses a recorded response only when the effective provider
request matches. Reuse is real captured provider output but is not a fresh or
independent provider trial. The dashboard distinguishes fresh hosted, cached
hosted, local resolution, deterministic fallback, and unverified provenance.

The UI's English labels and question translations are display-layer prototype
text. They do not change answer indices, scoring, provider inputs, stored
records, or audit JSON, and they still require educator review.

## 7. Data and privacy boundary

Student routes must not expose:

- answer keys;
- selected-option text beyond the student's own submission;
- raw response history beyond the public session flow;
- KT probabilities;
- conformal intervals or statuses;
- teacher-only graph detail;
- provider execution internals.

Teacher routes may show private answer keys, response records, provider
execution records, and research diagnostics. They require the local teacher
cookie issued by PIN unlock. `artifacts/` contains private key-derived and
provider-capture data and must remain local/private.

KT uses the frozen variant-D model (`skill_item_content_option`) and does not
retrain. Conformal applies existing historical calibration data. Both remain
`exploratory_only_not_fixed_bank_validated` and are not used for student
advice.

## 8. Demo API surface

| Route | Method | Purpose |
|---|---|---|
| `/api/sessions` | POST | create a synthetic session |
| `/api/sessions/{sid}` | GET | student snapshot with owner token |
| `/api/sessions/{sid}/responses` | POST | submit an ordered answer |
| `/api/teacher/unlock` | POST | exchange local PIN for teacher cookie |
| `/api/teacher/logout` | POST | invalidate the teacher cookie |
| `/api/teacher/config` | GET | provider mode, diagnostics status, call budget |
| `/api/teacher/sessions` | GET | list sessions |
| `/api/teacher/simulations` | POST | create a synthetic response-pattern session |
| `/api/teacher/sessions/{sid}` | GET | private session detail |
| `/api/teacher/sessions/{sid}/export` | GET | private JSON export |
| `/api/teacher/sessions/{sid}/reviews` | POST | store educator review |
| `/api/teacher/scenarios` | GET | derived summary and saved case index |
| `/api/teacher/scenarios/{id}` | GET | private original case plus shared detail view |
| `/api/teacher/scenarios/{id}/export` | GET | original case export |
| `/api/teacher/replays` | GET / POST | list / enqueue replay batches |
| `/api/teacher/replays/{id}` | GET | progress, case statuses, and changes |
| `/api/teacher/replays/{id}/cancel` | POST | stop after the current case |
| `/api/teacher/replays/{id}/export` | GET | batch provenance and changes |
| `/api/teacher/replays/{id}/cases/{case_id}` | GET | current or preserved result detail |
| `/api/teacher/replays/{id}/cases/{case_id}/export` | GET | individual replay result export |
| `/api/teacher/comparisons` | GET / POST | list / start source-masked reviews |
| `/api/teacher/comparisons/{id}` | GET | resume current blinded task |
| `/api/teacher/comparisons/{id}/reviews` | POST | save immutable task judgment |
| `/api/teacher/comparisons/{id}/export` | GET | reveal/export a completed review |

Student requests use `X-Demo-Session-Token`. Teacher requests use the opaque
`demo_teacher` HttpOnly cookie. The legacy `api.py` uses a different route set
and is not the current UI backend.

## 9. Module map

| Area | Files |
|---|---|
| Current web boundary/UI | `demo_api.py`, `web_demo/app.js`, `web_demo/index.html`, `web_demo/teacher.html`, `web_demo/styles.css` |
| Demo orchestration | `demo_service.py` |
| Retained report, labels + source-masked review | `research_workspace.py`, `web_demo/research.js` |
| New replay orchestration + result snapshots | `scenario_replays.py`, `DemoService` |
| Session/scoring/persistence | `mcq_service.py`, `session_store.py`, `schemas.py` |
| Evidence and graph | `evidence_feedback.py`, `feedback_service.py`, `assessment_taxonomy_draft.json` |
| Feedback policy/rendering | `evidence_feedback_policy.py`, `render_evidence_feedback.py`, `educator_review_contract.py` |
| Providers and capture cache | `evidence_providers.py`, `jev_selector.py`, `aitta_generator.py`, `replay_provider_scenarios.py`, `transport_diagnostics.py` |
| Live diagnostics | `live_diagnostics.py`, `kt_adapter.py` |
| Batch replay/evaluation | `integrated_synthetic_pipeline.py`, `evaluate_evidence_feedback.py`, `evaluate_scenarios.py`, `compare_scenarios.py`, `scenario_runner.py`, `scenario_report.py`, `render_provider_replay.py`, `visualize_scenarios.py` |
| Legacy API/demo | `api.py`, `demo_cli.py`, `synthetic_feedback.py`, `synthetic_feedback_demo.py` |
| Canonical paths/provenance | `phase3_paths.py`, `provenance.py` |

## 10. Retained evidence and metrics

The most useful retained reports are:

- `artifacts/live_demo_20261002/` — live demo sessions, metadata, provider
  captures, and verification evidence;
- `artifacts/integrated_feedback_20261002_hosted_full/` — current
  evidence-focused hosted replay: 54 saved scenarios, 162 packages, 69/108
  end selections matching baseline and 39 differing;
- `artifacts/evidence_feedback_20261002_v2/` — deterministic
  observed-evidence review: 172 scenarios, 516 packages, and 14,641 checked
  skill-total vectors (inputs plus retained summary docs);
- `artifacts/assessment_pipeline_20261001.json` — 54-scenario answer source
  used by replay.

Past reports now deleted by an approved cleanup (no longer on disk; quoted
metrics remain past results): `integrated_feedback_20261002_offline/`
(deterministic equivalent of the hosted report), `provider_replay_20261002/`
(older generic-candidate replay where Jev matched the rules baseline on all
108 end packages), `assessment_pipeline_20261001/` (54-scenario observed/KT
summary and conformal rows), `report_fixed40_baselines_20260929/` (earlier
ten-scenario baselines), old verification logs/screenshots, and cleanup
receipts.

Interpretation rules:

- provider match/difference counts are software comparisons, not accuracy;
- simulated `true_probability` values are scenario parameters, not learner
  truth;
- KT and conformal metrics are exploratory fixed-bank diagnostics;
- cached provider captures are not independent calls;
- no retained metric establishes educational effectiveness, mastery,
  misconceptions, or learning improvement.

See the metrics table in `README_STATUS_ARCHITECTURE_ROADMAP.md` for exact
counts and limitations.

## 11. Batch and legacy commands

Scripted session without the web UI:

```powershell
python demo_cli.py --profile alternating --out artifacts\demo_session_result.json
```

Legacy JSON API (no teacher dashboard or async provider jobs):

```powershell
python api.py --host 127.0.0.1 --port 8765
```

Its private researcher routes require `X-Phase3-Role: researcher`; that header
is only local route separation and cannot be used in the new demo API.

Offline evidence-feedback replay:

```powershell
python evaluate_evidence_feedback.py --out-dir artifacts\evidence_feedback_NEW --check-skill-vectors
python render_evidence_feedback.py artifacts\evidence_feedback_NEW\report.json --out artifacts\evidence_feedback_NEW\review.html
```

Offline integrated replay:

```powershell
python integrated_synthetic_pipeline.py --mode offline --all-scenarios --out-dir artifacts\integrated_feedback_NEW
```

A hosted batch replay is intentionally explicit and bounded:

```powershell
python integrated_synthetic_pipeline.py --mode hosted --all-scenarios `
  --jev-env-file "C:/path/to/jev.env" --provider-timeout 120 `
  --max-new-calls 101 --out-dir artifacts\integrated_feedback_NEW_HOSTED
```

Output directories and reports are created conservatively; existing report
files are not overwritten. Provider captures preserve failure and reuse
provenance.

## 12. Verification

Narrow local checks:

```powershell
python -m unittest tests.test_demo_api tests.test_demo_service tests.test_live_diagnostics tests.test_research_workspace tests.test_scenario_replays
node --check web_demo/app.js
node --check web_demo/research.js
```

Full local test discovery:

```powershell
python -m unittest discover -s tests -v
```

Opt-in browser verification using installed Microsoft Edge and the existing
`websocket-client` Python package (no new browser dependency):

```powershell
$env:RUN_BROWSER_TESTS = "1"
python -m unittest tests.test_research_browser -v
Remove-Item Env:RUN_BROWSER_TESTS
```

Optionally set `BROWSER_SCREENSHOTS` to an existing local output directory, or
`EDGE_BINARY` to the installed Edge executable. The browser test uses an isolated
temporary server, fake model/providers, and synthetic review judgments. When the
retained report exists, it also browses its actual 54-case library. It checks
source reveal timing, filtering, 390px/1440px layout, preserved review drafts,
synthetic consent, selected/filtered/all replay, result-version switching, replay
review submission, and the full 40-answer flow. The optional real-source unit test
replays all 54 retained answer sequences with fake diagnostics and no providers;
it proves orchestration/score preservation, not fresh real-model validity. This verifies software behavior,
not educator preference or educational validity. Browser tests skip during normal
unit discovery unless explicitly enabled.

Most tests use synthetic or fake banks/providers. The optional real-bank test
runs only when the approved Phase 2 bank is available. Earlier browser
verification scripts and screenshots under
`artifacts/live_demo_20261002/verification/` and
`artifacts/ui_verification_20261004/` were removed by the approved cleanup;
they proved UI behavior or validated captured data, never provider calls or
educational validity. New screenshots under
`artifacts/research_guidance_ui_20261004/` verify only the research-status
guide UI.

Caution: a running demo keeps the native KT model resident in memory. Stop or
coordinate the server before a memory-heavy full test run.

## 13. Safety limits

- No real student data may be entered.
- Feedback is not approved for learner delivery.
- A Jev selection differing from the baseline is not automatically better.
- Aitta does not currently generate individualized mathematical advice.
- KT/conformal do not control Jev selection or student feedback.
- Sparse subtopic counts cannot establish mastery or misconceptions.
- The bank is assessment content, not an approved practice-activity bank.
- The local PIN/cookie model is demo separation only; production requires
  real authentication, authorization, TLS, durable storage, privacy review,
  monitoring, and a job queue.

## 14. Next work

1. Review the implemented comparison protocol with educators; preregister the
   evaluation questions, sampling, duplicate handling, and analysis before data collection.
2. Run independent educator reviews using the source-masked workflow; inspect
   evidence-support judgments as well as preferences, stratified by exposure and execution provenance.
3. Expand feedback carefully with bounded, evidence-grounded composition.
4. Create a reviewed practice-activity bank and worked explanations.
5. Validate KT/conformal behavior for this fixed assessment before any use in
   advice.
6. Only after educator and privacy review, consider a supervised pilot and
   production hardening.
