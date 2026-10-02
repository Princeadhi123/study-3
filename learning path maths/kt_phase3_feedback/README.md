# Phase 3 — synthetic assessment and educator-review demo

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
- poll-safe dashboard disclosure and review-form state.

Pending:

- a UI for browsing all 54 saved replay scenarios;
- a blind baseline-vs-provider educator comparison;
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

The teacher page shows six tabs:

1. Overview — session status, answer count, provider/diagnostic job state;
2. Evidence — observed totals and skill/subtopic counts plus private question
   records;
3. Graph — descriptive hierarchy and observed counts;
4. Research — private frozen KT and conformal diagnostics;
5. Feedback — baseline versus selected drafts, candidates, provider trace,
   and the selection-decision panel;
6. Review — educator review fields, notes, and the saved review ledger.

Teacher actions include synthetic simulation profiles, JSON export, and
logout. Simulation profiles are response patterns, not learner diagnoses.

The dashboard polls approximately every two seconds. Unchanged payloads skip
redraws; expanded sections and unsaved review edits are preserved across real
updates.

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

Student requests use `X-Demo-Session-Token`. Teacher requests use the opaque
`demo_teacher` HttpOnly cookie. The legacy `api.py` uses a different route set
and is not the current UI backend.

## 9. Module map

| Area | Files |
|---|---|
| Current web boundary/UI | `demo_api.py`, `web_demo/app.js`, `web_demo/index.html`, `web_demo/teacher.html`, `web_demo/styles.css` |
| Demo orchestration | `demo_service.py` |
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
- `artifacts/integrated_feedback_20261002_offline/` — deterministic version of
  the same integrated report path;
- `artifacts/provider_replay_20261002/` — older generic-candidate Jev/Aitta
  replay: Jev matched the rules baseline on all 108 end packages;
- `artifacts/evidence_feedback_20261002_v2/` — deterministic
  observed-evidence review: 172 scenarios, 516 packages, and 14,641 checked
  skill-total vectors;
- `artifacts/assessment_pipeline_20261001/` — 54-scenario observed/KT summary,
  conformal checkpoint rows, and research diagnostics;
- `artifacts/report_fixed40_baselines_20260929/` — earlier ten-scenario
  baseline report with simulated probability, KT probability, observed
  accuracy, and conformal summaries;
- `artifacts/cleanup_result_artifacts_v2.json` — receipt for the approved
  cleanup of 336 obsolete files.

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
python -m unittest tests.test_demo_api tests.test_demo_service tests.test_live_diagnostics
node --check web_demo/app.js
```

Full local test discovery:

```powershell
python -m unittest discover -s tests -v
```

Most tests use synthetic or fake banks/providers. The optional real-bank test
runs only when the approved Phase 2 bank is available. Browser verification
scripts under `artifacts/live_demo_20261002/verification/` prove UI behavior
or validate captured data; they do not rerun provider calls or establish
educational validity.

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

1. Build the 54-case Scenario Library UI from the retained replay report.
2. Add blinded educator comparison for baseline versus provider outputs.
3. Expand feedback carefully with bounded, evidence-grounded composition.
4. Create a reviewed practice-activity bank and worked explanations.
5. Validate KT/conformal behavior for this fixed assessment before any use in
   advice.
6. Only after educator and privacy review, consider a supervised pilot and
   production hardening.
