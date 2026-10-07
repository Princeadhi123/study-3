# Status, Architecture, and Roadmap

Runtime transition updated: 2026-10-07. This file is the current handoff for the local
synthetic demo in this repository. It describes working software for private
research review — not a validated educational product.

## 1. Executive status

- **Working**: an interactive localhost application with a student page and a
  private teacher review page, driven by `demo_api.py` + `demo_service.py`.
  This is a real interactive app, not only a batch report.
- The legacy `api.py` boundary remains unchanged and is separate; `demo_api.py`
  is the current entry point.
- All feedback text is a **draft requiring human educator review**. No real
  student pilot exists, and nothing here is approved for learner delivery.
- Assessment content: 4 skills (Arithmetic, Prices, Fractions, Percentages),
  40 fixed ordered questions (10 per skill, 5 per half), 15 descriptive
  subtopics. Read-only bank:
  `../kt_phase2_inference/artifacts/test_question_bank_text_only_approved_v2.json`;
  taxonomy `assessment_taxonomy_draft.json` (descriptive AI-assisted content
  audit — not independently educator-approved).
- Each new session explicitly selects the demo bank or one 40-question warm/
  cold research bank. Research banks retain their pending educator approval
  status and use separate validation with graph v3 assessed-concept taxonomies.
  Source hashes, canonical bank/taxonomy hashes, bank mode and intended item
  regime are bound to the session and checked after restart.
- This file contains no private keys, exam answer keys, session ids, PIN
  values, or credentials.

## 2. Feature status

| Area | Status |
|---|---|
| Student + teacher web UI (`web_demo/`) | Implemented |
| Local PIN teacher auth, owner-token student auth | Implemented (local demo separation only) |
| Async provider jobs (Jev selection + Aitta opening) | Implemented |
| Frozen KT research diagnostics | Implemented; active conformal computation/output removed |
| Warm/cold research sessions and v3 taxonomies | Implemented; synthetic only, pending educator review |
| Teacher task context and 24-question practice drafts | Implemented; no learner release |
| Assessment graph, reviews ledger, English display layer, poll/disclosure persistence | Implemented |
| 54-case Scenario Library browser UI | Implemented: readable names, search/filters, shared six-tab live detail renderer |
| Selected / filtered / all-scenario replay | Implemented: original-answer resubmission, progress/cancel, saved versions and original-result comparison |
| Historical result preservation | Original report remains immutable; completed replay snapshots remain readable after policy/taxonomy/bank changes |
| Blind baseline-vs-selected comparison | Implemented pilot tooling: balanced A/B, persisted shuffled order, immutable judgments, completion-only reveal |
| Evidence Lab responsive UI | Implemented: three workspaces, session search, keyboard controls, synthetic consent |
| Richer Aitta evidence explanation | **Pending** |
| Reviewed practice questions / worked solutions / practice loop | **Pending** |
| Independent educator preference benchmark | **Pending** |
| Calibration validity on a fixed assessment | **Pending** |
| Real-student privacy review + production deployment | **Pending** |

The candidate-ordering method and method-review actions are draft
heuristics. The existing demo bank remains available. Warm/cold research
banks and the 24-question practice source are not educator-approved.

## 3. Architecture

```text
browser (index.html / teacher.html, app.js)
   |  GET/POST JSON over loopback HTTP
demo_api.py  --  loopback-only boundary, owner token + teacher cookie
   |
DemoService (demo_service.py)
   |-- bank-bound ordered MCQ scoring (one selected 40-question bank)
   |-- observed evidence + assessment graph at n=20 / n=40
   |-- branch A: feedback baseline + Jev selection + Aitta opening
   |            (one provider worker; per-audience isolation)
   |-- branch B: private frozen KT research diagnostics
   |             (own diagnostics worker; snapshot rows at queue admission)
   +-- teacher-only v3 task context / 24-question practice drafts
                 (optional KT selector requires augmented embeddings)
```

- Provider/diagnostic work triggers **only at n=20 and n=40**, never on poll.
- At question 20: no Jev/Aitta call; a deterministic neutral student pause,
  plus private graph + diagnostics for the teacher.
- At question 40: deterministic baseline feedback is shown immediately while
  the provider job is pending; both end audiences are processed by one
  provider worker.
- Diagnostics snapshot the answer rows at queue admission so later answers
  cannot race the evaluation.
- KT uses current answers on CPU via the frozen variant D
  (`skill_item_content_option`); no retraining or calibration loading.
  Diagnostics carry labels `uncalibrated_for_20_40_question_feed` and
  `used_for_student_advice: false`.
- The graph relation is `is_part_of_not_prerequisite` — part-of groupings,
  not prerequisites; no conformal gate is used for advice.
- Boundary: student payloads exclude diagnostics, private question/item IDs,
  content text, source paths and answer keys. Submissions use opaque
  session/position tokens plus an option index. Jev receives allow-listed
  aggregate observed skill/subtopic counts and permitted candidates only —
  never raw responses, question texts, session IDs, or research probabilities.

## 4. Student and teacher experience

- **Student**: starts with `synthetic: true` attestation, receives an owner
  token; question payloads are key-free; resume via localStorage; answers are
  index-only ordered responses (last identical retry is idempotent); scores
  are hidden at midpoint; the end shows total + per-skill observed counts.
- **Teacher**: three workspaces — Live sessions, Scenario library, Blind
  comparison. Live and scenario results share six tabs — Summary, Answers & scores,
  Skill map, Model diagnostics, Feedback drafts, Educator review — plus private
  JSON export. Primary labels describe patterns, while seeds and technical IDs stay
  in provenance/settings. Five simulation profiles exist:
  `all_correct`, `all_incorrect`, `weak_fractions_only`,
  `first_half_correct_second_half_wrong`, `alternating` — these are response
  patterns, not diagnoses.
- **Selection decision panel** (Feedback tab): shows applied focus, candidate
  ID, observed incorrect/out-of counts, baseline comparison, per-candidate
  Selected/Baseline marks, selection provenance (Jev cached/fresh/local
  fallback/unverified), Aitta provenance, and selector model version only for
  confirmed hosted executions.
- **Review tab**: five unscored fields from `educator_review_contract.py` —
  `evidence_support`, `method_content`, `audience_tone`, `taxonomy`, `scope`
  — plus optional note/reviewer label, bound to the feedback message hash,
  policy, bank, and candidate. Stale-version submissions return 409. Records
  carry `scored: false`, `approves_learner_delivery: false`.
- **Polling**: every ~2 s; unchanged session detail JSON skips redraw;
  real redraws preserve open/closed disclosure state per session+tab and per
  research checkpoint; unsaved review edits survive polls.
- **Finnish/English display layer** (in `app.js`): authored mappings use
  public prompt text, with research topic labels and short-prompt translations.
  Finnish originals remain selectable; untranslated English-view prompts are
  explicitly labeled as original wording. English options display decimal dots
  and "EUR" while indices and wire values are unchanged; raw JSON and exports
  keep original Finnish wording; these are prototype translations, not
  independently educator-checked.

### Retained replay and comparison protocol

`research_workspace.py` loads the retained integrated report lazily, validates its
package coverage and observed counts, and pins its raw-file SHA-256 for the
process. `--replay-report PATH` selects another report. Library summaries are
recomputed from the packages; the current retained report yields 54 cases,
108 end packages, 69 baseline matches and 39 differences. Browsing does not
run providers or inference, and no historical artifact is rewritten.

`scenario_replays.py` adds explicit asynchronous selected/filtered/all replay. It
checks the original response-source hash, bank/order/index bindings, and current
code fingerprints before admission. Each case is a fresh `DemoService` session
using the same 40 saved answers and the same scoring/graph/feedback/diagnostics
workers as live sessions. Rules-only mode is the default even on a hosted server;
hosted calls require confirmation and share the existing budget/cache. There is one
active batch and sequential case admission; cancellation stops after the current
case, and interrupted batches never resume automatically.

Batch records live in `replay_runs/`; completed full detail snapshots are preserved
in `replay_runs/results/`. Replay sessions are hidden from the live session list.
Result-version selection preserves the current detail tab and separates original
saved outputs from fresh replay jobs. Summary compares score, feedback selection/text,
and compatible KT traces with the original report, not with an assumed ground truth.
Older replay snapshots and their review notes remain read-only if current policy,
taxonomy, or bank validation rejects their live metadata. New replays can be reviewed
through the same hash-bound educator form as live sessions. The original report and
blind-comparison cohort are never silently replaced by replay results.

`--replay-source PATH` defaults to `artifacts/assessment_pipeline_20261001.json`.
Restart after Python changes; tracked Phase 2/3 source changes produce a replay
conflict rather than silently running stale loaded code. No model retraining or
fixed-bank calibration claim is introduced.

### Research status guide

The scenario library renders two always-visible boundary notes (observed counts
describe this assessment; all scenarios are synthetic test cases, not learners)
plus a collapsed static `#research-readiness` disclosure of project-wide
guidance — explicitly not a per-scenario diagnosis or readiness score. Five cards
(KT, feedback focus/wording, skill map, synthetic replay,
blind educator comparison) each state what the component does, why it remains
research-only, and the next validation step. For KT, recorded predictive
evaluation on real ViLLE interactions already exists and the 40-question bank is
source-derived, but those results do not establish mastery or calibration in
this fixed session setting, and the deployed checkpoint was selected partly
using cold-item evaluation scores — not an untouched final test. The stated
next step audits existing ViLLE sessions for suitable held-out answers (only
preceding history per prediction) and evaluates fixed-assessment prediction and
calibration on a set unused for training or model selection, collecting new
learner data only if existing data cannot support the claim. Remaining steps:
empirical coverage validation, independent educator review plus a reviewed
practice-activity bank, educator taxonomy/prerequisite review, a consented
privacy-reviewed learner study, and a preregistered comparison protocol with a
validated rubric. A closing note lists educator approval, privacy review, real
access controls, TLS, and durable storage as prerequisites for real learner use.
The research diagnostics tab adds the parallel caveat that KT was trained and
predictively evaluated on real ViLLE data, estimates correctness rather than
mastery, that simulated sessions are not additional real-learner validation,
and that KT estimates do not establish difficulty or learning benefit. Opening the
guide reveals no drafts, candidate IDs, or scenario mappings and sets no
exposure flag.

A comparison includes every end case for one audience. Server-side cryptographic
randomization shuffles case order and balances A/B placement. Assignments, exact
text, provenance, and canonical JSON hashes are persisted under the demo root's
`comparisons/` directory, independently from live hash-bound review notes.
The current task returns only observed evidence and source-masked draft text.
Judgments record A/B/tie/neither preference, each draft's evidence support,
confidence, and optional rationale. Identical resubmissions are idempotent;
changes to a saved judgment, out-of-order tasks, incomplete exports, and source
revision conflicts return 409. All task judgments must be saved before source
mapping and execution provenance can be exported. Resume survives server restart.

This is not an independent educator benchmark yet. Initial exposure is self-reported;
style, other tabs/files, and later browsing can compromise masking. Teacher access
still permits unblinded library inspection. Review all cases before opening drafts,
use pseudonymous codes and a supervised protocol, and plan analysis before collecting
judgments. Identical texts are retained, while correlated cases, duplicate sequences,
cache reuse, and repeated reviewers prevent naive independent-trial interpretation.
Only descriptive preferences are shown; no p-values or educational-effectiveness
claims are generated. Completed reviews are not learner-delivery approval.

## 5. What is dynamic, and provenance rules

- Live session counts, evidence, graph and KT values are computed
  per session; stored completed sessions are read, not recomputed.
- Historical conformal evidence remains on disk. Active API projections,
  including replay exports, omit conformal/calibration output without
  rewriting saved captures.
- Jev selects a permitted observed-error focus; a single permitted candidate
  bypasses Jev locally. Candidate priority: highest incorrect count, then
  highest incorrect fraction, then stable taxonomy order — an unvalidated
  presentation heuristic. All-correct yields "optional consolidation."
- The app controls facts and most wording; **Aitta supplies the opening
  sentence only** (verified in `integrated_synthetic_pipeline.aitta_request` /
  `evidence_providers.EvidenceAittaGenerator`). The effective hosted wire for
  Aitta is a generic audience/checkpoint communication context built with a
  legacy generic candidate — **not** the actual review topic: no real focus
  names, performance counts, or raw questions/KT are sent. Only three
  matching contexts exist, which is why replay reuse is high.
- Cache identity is the effective wire request (`CaptureCache`); no automatic
  retry. The call budget counts *new* calls per process, not lifetime totals.
  "fresh"/"cached" statuses reflect actual execution, not latency guesses.
- Provider failures fall back per audience: an Aitta failure can keep a valid
  Jev selection with a deterministic opening. Queue bounds are enforced;
  unavailable diagnostics never fabricate values. A restart converts a
  pending provider job to deterministic fallback and interrupted diagnostics
  to `interrupted_by_restart` — not silently rerun. Reused captures are not
  independent trials.

## 6. Runbook (localhost, PowerShell)

Run these from this repository folder; the commands are alternatives — do
not run both while a server is already listening. Aitta is configured via
env vars in an existing local `.env` file (values not printed here).

```powershell
# rules mode (no providers; generates a teacher PIN at startup)
python demo_api.py --host 127.0.0.1 --port 8766 --providers rules

# hosted mode (optional; requires a Jev env file you hold locally)
python demo_api.py --host 127.0.0.1 --port 8766 --providers hosted `
  --jev-env-file "C:/path/to/jev.env" --call-budget 12 --provider-timeout 120

# warm research-bank scenario library (rules; stop any running server first)
python demo_api.py --port 8766 --providers rules --replay-bank-mode warm
```

- `--replay-bank-mode warm` serves the retained warm capture in
  `artifacts/warm_scenarios_v1/`; omitting the flag keeps the original demo
  library. Replay history is listed per bank mode; `--replay-report` and
  `--replay-source` override the per-mode defaults.
- Student: `http://127.0.0.1:8766/` — Teacher: `http://127.0.0.1:8766/teacher`
- Only loopback hosts are accepted; any other Host or Origin — including a
  direct-Origin browser preview bound to a different port — is
  intentionally rejected.
- Rules mode works without provider credentials but needs the frozen model
  assets for real diagnostics (otherwise diagnostics report unavailable).
- Auth: student `X-Demo-Session-Token` header; teacher opaque HttpOnly cookie
  from PIN unlock. The legacy `X-Phase3-Role` header cannot bypass this.
  This is loopback separation, not production authentication.

### API surface

| Route | Method | Purpose |
|---|---|---|
| `/api/sessions` | POST | create session (`{"synthetic": true}`, optional `bank_mode`) |
| `/api/sessions/{sid}` | GET | student snapshot (owner token) |
| `/api/sessions/{sid}/responses` | POST | submit `{question_token, selected_index}` |
| `/api/banks` | GET | available bank labels and 40-question count |
| `/api/teacher/unlock` | POST | issue PIN auth cookie |
| `/api/teacher/logout` | POST | drop teacher cookie |
| `/api/teacher/config` | GET | mode, diagnostics, call budget |
| `/api/teacher/sessions` | GET | session list |
| `/api/teacher/simulations` | POST | `{profile, seed}`, optional `bank_mode` |
| `/api/teacher/sessions/{sid}` | GET | private detail (incl. answer keys) |
| `/api/teacher/sessions/{sid}/export` | GET | JSON export |
| `/api/teacher/sessions/{sid}/reviews` | POST | record educator review |
| `/api/teacher/scenarios` | GET | retained case index and derived metrics |
| `/api/teacher/scenarios/{id}` | GET | original packages and shared detail payload |
| `/api/teacher/scenarios/{id}/export` | GET | original case export |
| `/api/teacher/replays` | GET / POST | list / enqueue batches |
| `/api/teacher/replays/{id}` | GET | progress and original-result differences |
| `/api/teacher/replays/{id}/cancel` | POST | stop after current case |
| `/api/teacher/replays/{id}/export` | GET | batch provenance export |
| `/api/teacher/replays/{id}/cases/{case_id}` | GET | new or preserved case detail |
| `/api/teacher/replays/{id}/cases/{case_id}/export` | GET | individual result export |
| `/api/teacher/comparisons` | GET / POST | list / create review runs |
| `/api/teacher/comparisons/{id}` | GET | current source-masked task |
| `/api/teacher/comparisons/{id}/reviews` | POST | immutable judgment, idempotent retry |
| `/api/teacher/comparisons/{id}/export` | GET | completed source mapping and provenance |

Teacher JSON endpoints contain private answer keys and are never exposed to
student routes.

## 7. Module map

- Boundary/UI: `demo_api.py`, `web_demo/` (`app.js`, `index.html`,
  `teacher.html`, `styles.css`)
- Replay library/comparison/labels: `research_workspace.py`, `web_demo/research.js`
- Replay queue, original response binding, snapshots: `scenario_replays.py`
- Shared live/scenario detail renderer: `createDetailView` in `web_demo/app.js`
- Session core: `demo_service.py` (DemoService orchestration),
  `mcq_service.py` (MCQSessionService, ordered responses),
  `session_store.py` (SessionStore persistence)
- Diagnostics: `live_diagnostics.py`, `kt_adapter.py`
- Feedback: `evidence_feedback.py` (evidence/feedback engine),
  `feedback_service.py` (assessment graph), `evidence_feedback_policy.py`,
  `evidence_providers.py`, `render_evidence_feedback.py`
- Providers/transport: cached Jev/Aitta adapters in
  `integrated_synthetic_pipeline.py`, `CaptureCache` (defined in
  `replay_provider_scenarios.py`), `transport_diagnostics.py`
- Review contract: `educator_review_contract.py`
- Batch/replay (legacy context): `scenario_runner.py`,
  `compare_scenarios.py`, `evaluate_scenarios.py`, `scenario_report.py`,
  `replay_provider_scenarios.py`

## 8. Artifacts and verification evidence

Current protected artifacts under `artifacts/`:

- `assessment_pipeline_20261001.json` — 54-scenario answer source
- `shadow_smoke_20261006/` — frozen banks, original pool, taxonomies, cases
  and smoke outputs
- `practice_comparison_20261006/` — reviewer pack and comparison analysis
- `bounded_content_graph_v3_20261007/` — current descriptive graph with the
  24-question practice pool
- `bounded_content_graph_v2_20261006/` — preserved v2 graph
- `bounded_content_graph_20261006/` — preserved v1 graph
- `synthetic_practice_supplement_20261006/` — synthetic supplement, balanced
  v2 pool and KT compatibility check

Approved cleanups removed historical runtime, replay and test artifacts,
including `live_demo_20261002/`,
`integrated_feedback_20261002_hosted_full/`,
`evidence_feedback_20261002_v2/`, `sessions/`,
`practice_pool_capacity_20261006.json`,
`integrated_feedback_20261002_offline/`, `provider_replay_20261002/`,
`assessment_pipeline_20261001/`, `report_fixed40_baselines_20260929/`,
`ui_verification_20261004/`, `hosted_combined_20261002_run1_review.md`, and
`cleanup_result_artifacts_v2.json`. These files and directories no longer
exist on disk — do not link them. Reports and metrics quoted below from
those sources are past verification results, citable as history but not
re-inspectable.

Frozen measured figures (from the now-deleted hosted replay review, not
rederived): of 108 end packages, **69 match the rules baseline and 39
differ** — software identity comparison including local/cache execution,
not educational accuracy. Live sessions run fresh KT inference; the batch
report replayed frozen predictions. Coverage was 54 chosen cases, not all
sequences.

### Earlier metrics and what they validate

Rows citing directories marked as removed above describe past results from
deleted artifacts; they are retained as history, not on-disk evidence.

| Evidence source | Metrics / findings | Interpretation boundary |
|---|---|---|
| `artifacts/report_fixed40_baselines_20260929/scenario_report.json` | Observed accuracy, simulated `mean_true_probability`, `mean_kt_probability`, `mean_absolute_gap`, conformal status and interval per checkpoint/skill. | Early KT/conformal behaviour check. `true_probability` is a simulator parameter, not learner ground truth; the report also predates the current descriptive-only graph. |
| `artifacts/assessment_pipeline_20261001/summary.csv` | 54 scenario labels with first-half, second-half, total observed scores and KT means. | Compares observed patterns with model probabilities; not calibration or mastery validation. |
| `artifacts/assessment_pipeline_20261001/conformal_checkpoints.csv` + `pipeline_diagnostics.json` | 432 checkpoint-skill rows; midpoint statuses `UNCERTAIN_BEHAVIOR` 139 / `CONFIDENT_STRUGGLE` 77; end statuses `MASTERY_SAFE` 6 / `UNCERTAIN_BEHAVIOR` 110 / `CONFIDENT_STRUGGLE` 100; 102 end rows have observed rate outside the interval; one `MASTERY_SAFE` row had observed rate 7/10 below its 0.8 threshold. | Synthetic stress-test counts over correlated fixed-bank cases; not empirical coverage and not a safe learner decision signal. |
| `artifacts/assessment_pipeline_20261001/llm_boundary.json` | Distractor probe: 39 KT item positions changed, max absolute probability delta 0.088278, while observed feedback stayed identical; 54 labels contain 53 unique response sequences. | Shows option identity can affect KT while the feedback branch remains observed-count-based; not evidence of misconception detection. |
| `artifacts/evidence_feedback_20261002_v2/` (deleted) | 172 review scenarios, 516 feedback packages, 14,641 skill-total vectors, 239 software tests, zero provider calls. | Past deterministic policy/evidence-binding validation; does not measure educational quality or exhaust answer sequences. |
| `artifacts/provider_replay_20261002/review.md` | Older generic candidate set: 162 packages, 108 end packages, 98 fresh Jev calls, zero fallbacks; Jev selected `observed_summary` and matched rules on all 108 end packages; Jev latency median 291.7005 ms. | Provider/API compatibility on the old selector contract only; not comparable as a current Jev quality result because the candidate set later changed. |
| `artifacts/integrated_feedback_20261002_hosted_full/review.md` (deleted) | Past evidence-focused replay: 162 packages, 108 end packages, 69 baseline matches / 39 differences, zero recorded failures/fallbacks; 98 successful distinct Jev requests, three Aitta contexts, Jev median capture latency about 318 ms. | Past software/provenance evidence. A different selection is not a better selection; educator review is still required. |
| `artifacts/hosted_combined_20261002_run1_review.md` | Three-package hosted smoke test; caught unsafe midpoint progress wording and teacher-audience wording. | Human semantic review found issues that lexical/interface checks missed. |

These historical metrics should inform the Scenario Library and educator
review, but they must remain labelled as replay/stress-test evidence. They do
not establish student learning, calibrated mastery, misconception detection,
or that provider decisions outperform deterministic review heuristics.

## 9. Verification (what each check proves)

Unit-test paths are relative to this repository folder.

- `tests/test_demo_api.py`, `tests/test_demo_service.py`,
  `tests/test_live_diagnostics.py` — unit/software tests with offline fakes.
- Earlier browser verification drivers, logs, and screenshots under
  `artifacts/live_demo_20261002/verification/` were removed by the approved
  cleanup described in section 8. They remain past verification results:
  `decision_drive.py` exercised software fixtures on a rules page (not
  provider proof), `disclosure_drive.py` checked rules/fake disclosure state,
  `english_drive.py` rendered the real 40-question bank with fake
  diagnostics, and `verify_diagnostics.py` checked captured live math without
  rerunning inference. Passing logs on record included
  `english_v2_log.txt`, `decision_english_log.txt`, `disclosure_log.txt`,
  and `diagnostic_verification.json`.

Narrow commands:

```powershell
python -m unittest tests.test_demo_api tests.test_demo_service tests.test_live_diagnostics tests.test_research_workspace tests.test_scenario_replays
node --check web_demo/app.js
node --check web_demo/research.js
```

Caution: a running demo keeps the native KT model resident — an idle server
does not free its weights. Avoid concurrent native-model instances (a
full-suite run hit `MemoryError` under contention; an isolated retry
passed). Coordinate stopping/restarting your own demo server, or ensure
sufficient memory, before a full-suite run.

The workspace implementation has dedicated offline service/API tests and an
opt-in installed-Edge browser journey in `tests/test_research_browser.py`.
See README for invocation. Browser fixtures use fake diagnostics/providers and
synthetic judgments, not educator data; the retained library is read-only.
Earlier screenshots under `artifacts/ui_verification_20261004/` were removed by
the approved cleanup. New screenshots under
`artifacts/research_guidance_ui_20261004/` verify only the research-status guide
(closed/open states at desktop and mobile widths); they are new UI captures, not
fresh provider or research-validity proof.
No hosted provider calls were made for this implementation. Retained historical
report contents, assessment content, model weights, calibration, and provider
policies are unchanged by this UI update.

Verification on 2026-10-04: full discovery ran 341 tests successfully (340 passed,
1 opt-in browser test skipped); that browser journey was separately enabled and
passed in installed Edge at 1440px and 390px viewport widths. The 64 targeted
service/API/diagnostics/research tests passed, as did both JavaScript syntax checks
and `git diff --check`. These are software checks, not research-validity results.

## 10. Prioritised roadmap

### Completed prototype transition (2026-10-07)

The following transition is implemented. New research sessions remain
synthetic-only and pending educator review; no learner-release workflow was
introduced. See `FEEDBACK_AND_PRACTICE.md` for the current launch command and
`tests/test_research_runtime.py` for warm/cold and privacy acceptance checks.

- **Use the 24-question v2 practice pool for new work.** The original
  20-question pool remains frozen smoke evidence only; the accepted current
  pool is `practice_pool_v2_private.json` with four user-review-accepted
  synthetic divisibility questions. KT runs must explicitly select the
  augmented text-embedding table; frozen KT weights stay unchanged, and the
  synthetic item IDs use the cold/unknown-item representation.
- **Remove conformal from the active runtime/UI.** Preserve historical
  Phase 4 conformal artifacts and analysis, but stop live conformal
  computation and teacher-page display in the current prototype.
- **Replace demo assessment items for the research prototype.** Add explicit
  warm/cold bank selection (one 40-question bank per session), a separate
  research-mode serving boundary, matching graph-derived taxonomies and
  separated session provenance. Preserve each bank's KT item regime: cold
  bank item IDs intentionally use the frozen unknown-item representation.
- **Update student and teacher UI.** Add bank selection/labels, update skill
  and question wording, distinguish demo/warm/cold sessions, and expose only
  descriptive task evidence—not answer keys, graph internals or pending
  diagnostic annotations.
- **Connect graph v3 to teacher-facing feedback/practice.** Keep Jev/Aitta
  constrained, use the 24-question pool for KT-assisted practice drafts,
  retain teacher review and implement a separate release gate before any
  learner delivery.

1. **Educator protocol and evaluation** — review the implemented source-masked
   comparison protocol, preregister sampling/analysis and duplicate handling,
   then collect independent educator judgments. Tool availability is not evidence
   of provider advantage.
2. **Richer feedback scope** — bounded expansion of observed-evidence review
   (e.g. individualized Aitta context), plus a reviewed practice-activity
   bank (new content; the current assessment bank is not practice-approved).
3. **Research analysis** — examine preference and evidence-support judgments
   with exposure, source reuse, scenario dependence, and inter-rater agreement
   accounted for. Validate the rubric before drawing comparative conclusions.
4. **Supervised pilot** — only after privacy review and educator approval.
5. **Production hardening** — real auth, TLS, RBAC, durable store, job queue.
6. **Research calibration** — validity work on the fixed assessment, in
   parallel; diagnostics remain research-only until validated.

The numbered roadmap items remain planned work — nothing here implies automatic approval
for learner delivery.

## 11. KT and warm/cold bank next steps (2026-10-05)

### Decision and scope

Keep the current frozen KT model; do not retrain it or add a mastery layer
for this next study. The active demo now runs KT alone as private research
diagnostics; historical conformal captures remain preserved offline.
Neither controls student advice.
Scoring and feedback remain grounded in observed answers; Jev selects a
permitted observed-evidence feedback focus, not a mastery diagnosis.

KT predicts next-response correctness conditional on preceding history and
the target question. Its probabilities may inform a research proficiency
signal, but neither a hidden state nor `p_correct = 0.85` means 85% mastery.
Temporal features do not by themselves validate learning or forgetting.
An explicit mastery estimate is not required for the current feedback pipeline.

The warm/cold banks are separate research inputs, not replacements for the
demo bank or validated student assessments. Historical responses can support
next-response evaluation without a complete 40-question session; they do not
establish validity for the designed fixed, sequential assessment. The final
selection, protocol, replay capture and qualified exploratory CPU results now
exist in Phase 4. Fixed-assessment validation and learner approval remain pending.

### Current global bank selection

As of 2026-10-05, use the private research pair in
`../kt_phase2_inference/artifacts/evaluation_banks_support_optimal_20261005/`
for the Phase 4 historical study, not the removed original/v2 proposals.
The research version is `3_support_optimal_under_declared_rules_20261005`.

The accurate claim is **globally support-optimal within the frozen eligible
dataset and declared four-topic, 40-question bank rules**, not universally
best MCQs or validated mastery/coverage. The objective maximizes the weakest
question's student support, then total per-rendering support. Its optimum is
a minimum of 2 and a sum of 917; that sum does not count unique students.
The selected topics are algebra (combining like terms), percentages,
divisibility/primes, and fraction multiplication/quantities.

Bank hashes, all-combination results and independent checks are retained in
`optimality_certificate.json` and `independent_optimality_checks.json` in that
folder. Its `review.md` explains the exhaustive capacity/content review and
remaining data constraints; `cleanup_receipt_20261005.json` records the
approved removal of superseded artifacts. Required source evidence and
runtime dependencies remain. This historical selection did not approve the
banks for learners. The later synthetic runtime transition adds explicit
warm/cold session selection while retaining the original demo bank.

### Phase 4 historical study status

The separate workspace is `../kt_phase4_evaluation/`; read its `PROTOCOL.md`,
`CPU_REFERENCE_ADDENDUM.md` and README. Frozen outputs and lead interpretation
are in `artifacts/selected_bank_v3_20261005/` within that workspace.

The capture contains 1,012 eligible responses from 531 students: 860 warm
responses (63 errors) and 152 cold responses (4 errors). Warm KT history
improves AUC (0.830 versus 0.661) and log loss (0.208 versus 0.252), with
paired student-cluster intervals favoring history. Cold history benefit is
inconclusive. Warm pooled results are dominated by algebra.

Historical conformal threshold transfer has warm overall coverage 94.42%,
but incorrect-label coverage 61.90%; cold incorrect-label coverage is 2/4.
These are not validated student-facing uncertainty guarantees. There are
zero genuine selected-bank k10 blocks or contiguous ordered 40-question
matches, so selected-bank score coverage is not evaluated.

The original CUDA/reference `1e-4` parity requirement failed on 87 targets;
maximum probability gap is 0.00088215. All-target same-CPU full-input versus
response-blind predictions agree exactly under the tighter `1e-6` check.
The failed original record remains unchanged, and the precise historical
reference discrepancy is unresolved. The addendum preceded performance
analysis and changes no targets, baseline fitting, metrics or bootstrap rules.
Results are qualified exploratory CPU evidence, not an original-protocol pass.

Next work is historical-generator provenance, independently specified
cold/error support, and matching conformal calibration/evaluation. A practice
selection shadow policy remains separately planned and needs suitable content.
Observed-answer feedback remains independent; no KT/conformal student-advice
or mastery authority is enabled. The earlier preparation plan below is retained
as context, not a claim that those deliverables are still wholly unimplemented.

### Original pre-inference deliverables (retained plan)

1. **Selection provenance and freeze.** Use the retained final version and
   certificate hashes; do not rebuild the deleted original proposal or call
   this selection version 1. Bind the current banks, eligibility rules and
   input provenance to the evaluation protocol without changing the bank
   contents. Document overlap and content limitations, keep approval pending,
   and give any later content/selection revision a new version and reason.
2. **Eligible targets and score-block support.** Reuse the saved selected
   historical support in `selected_review_private.json`; do not repeat the
   availability scan without a specific reason. Resolve eligible target-event
   locators, calibration-student exclusion and preceding-history construction
   for replay. Check genuine usable blocks separately; available counts do
   not establish fixed-assessment sessions. Never combine unrelated responses
   into invented ten-question or 40-question assessment sessions.
3. **Written KT evaluation protocol.** Specify warm/cold item IDs crossed with
   history/no-history. Within each regime, use identical target responses for
   both history conditions and never expose the target answer before prediction.
   Define eligibility, history construction, baselines, AUC, log loss, Brier
   score, calibration checks, and student-level uncertainty before inference.
   Account for repeated student observations, limited samples and prior
   checkpoint selection; do not describe reused model-selection data as an
   untouched final test. Warm versus cold is descriptive, not a
   difficulty-controlled comparison. This roadmap is not the completed protocol.

### Evaluation and later decision use

After the three deliverables, run the frozen KT study and report whether
history helps, whether KT improves on the specified baselines, and whether
probabilities are reasonably calibrated under the evaluated conditions.
Report inconclusive findings rather than forcing a success claim. Historical
predictive performance alone does not establish mastery, fixed-assessment
validity, or improved student learning.

Specify conformal evaluation separately, before writing its evaluation code:
choose individual correctness or a ten-question realized score/rate as the
target, and check that calibration and evaluation construction match it.
Do not transfer historical-block coverage claims to the selected fixed bank.
If genuine selected-bank score support is insufficient, document that limit;
do not invent blocks or reuse a mismatched guarantee.

Only after predictive evaluation, and once suitable practice content exists,
consider one bounded KT-assisted decision: selecting the next practice
question. Keep the observed-evidence approach as the baseline and first log
KT's proposed choice in research/shadow mode without changing student advice.
Write the selection policy and comparison criteria before collecting results.
Shadow checks can expose decision behavior but cannot establish learning gains
or outcomes for unchosen questions. A later appropriately approved prospective
comparison is needed to test whether KT-assisted choices help students.

No bank switch, automatic progression, student-facing KT/conformal authority,
or mastery claim is authorized by this plan. Feedback development can proceed
independently while the research study is prepared.

### Shadow practice wiring (2026-10-06)

`future_kt.py`, `shadow_practice.py` and the DemoService/diag_pool wiring now
exist as code: an opt-in teacher-only branch that, only when the paired
`--shadow-practice-pool`/`--shadow-target-band` flags are supplied, runs frozen
KT future queries over an explicit private practice pool after the 40th answer
and stores a private shadow recommendation. Observed-evidence feedback and the
approved demo bank are unchanged; conformal remains historical diagnostics with
no per-item probability intervals. Native evaluation was pending at initial
wiring; the small smoke run below is now complete, but no broader study or
learning benefit is established. See
[FEEDBACK_AND_PRACTICE.md](FEEDBACK_AND_PRACTICE.md).

A small frozen native smoke ran 2026-10-06 under
[SHADOW_SMOKE_PROTOCOL.md](SHADOW_SMOKE_PROTOCOL.md) via the separate offline
research seam (`recommend_research`), not approvals or public serving:
10 cases (5 per research bank), 20 all-warm-item candidates, 0.60-0.80 band,
quotas 6/2/6/6; warm 3 selected / 2 abstained, cold 4 selected / 1 abstained,
none unavailable; sampled blind-vs-full gap 1.1920928955078125e-07 (tolerance
1e-6); all feedback unchanged. This is a smoke check only, not a validated
policy or a completed 108-scenario study. Frozen outputs and review:
`artifacts/shadow_smoke_20261006/` (private, git-ignored).
