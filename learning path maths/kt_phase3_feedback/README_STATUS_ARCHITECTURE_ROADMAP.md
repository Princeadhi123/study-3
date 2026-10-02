# Status, Architecture, and Roadmap

Snapshot date: 2026-10-03. This file is the current handoff for the local
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
- This file contains no private keys, exam answer keys, session ids, PIN
  values, or credentials.

## 2. Feature status

| Area | Status |
|---|---|
| Student + teacher web UI (`web_demo/`) | Implemented |
| Local PIN teacher auth, owner-token student auth | Implemented (local demo separation only) |
| Async provider jobs (Jev selection + Aitta opening) | Implemented |
| Frozen KT + conformal research diagnostics | Implemented |
| Assessment graph, reviews ledger, English display layer, poll/disclosure persistence | Implemented |
| 54-case Scenario Library browser UI | **Pending** |
| Blind baseline-vs-provider comparison | **Pending** |
| Richer Aitta evidence explanation | **Pending** |
| Reviewed practice questions / worked solutions / practice loop | **Pending** |
| Independent educator preference benchmark | **Pending** |
| Calibration validity on a fixed assessment | **Pending** |
| Real-student privacy review + production deployment | **Pending** |

The candidate-ordering method and method-review actions are draft
heuristics. The fixed assessment bank is approved for this synthetic
assessment; a practice bank has not been created.

## 3. Architecture

```text
browser (index.html / teacher.html, app.js)
   |  GET/POST JSON over loopback HTTP
demo_api.py  --  loopback-only boundary, owner token + teacher cookie
   |
DemoService (demo_service.py)
   |-- ordered MCQ scoring (fixed 40-question order)
   |-- observed evidence + assessment graph at n=20 / n=40
   |-- branch A: feedback baseline + Jev selection + Aitta opening
   |            (one provider worker; per-audience isolation)
   +-- branch B: private frozen KT -> conformal research diagnostics
                 (own diagnostics worker; snapshot rows at queue admission)
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
  (`skill_item_content_option`); no retraining. Historical k=5 (mid) / k=10
  (end). Diagnostics carry labels
  `uncalibrated_for_20_40_question_feed` +
  `exploratory_only_not_fixed_bank_validated` and
  `used_for_student_advice: false`.
- The graph relation is `is_part_of_not_prerequisite` — part-of groupings,
  not prerequisites; no conformal gate is used for advice.
- Boundary: student payloads exclude diagnostics, response history beyond
  index-only submissions, and private answer keys. Jev receives allow-listed
  aggregate observed skill/subtopic counts and permitted candidates only —
  never raw responses, question texts, session IDs, or research probabilities.

## 4. Student and teacher experience

- **Student**: starts with `synthetic: true` attestation, receives an owner
  token; question payloads are key-free; resume via localStorage; answers are
  index-only ordered responses (last identical retry is idempotent); scores
  are hidden at midpoint; the end shows total + per-skill observed counts.
- **Teacher**: six tabs — Overview, Evidence, Graph, Research, Feedback,
  Review — plus a private JSON export. Five simulation profiles exist:
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
- **English display layer** (in `app.js`): authored mappings cover all 40
  bank question ids and the four skill names; options display decimal dots
  and "EUR" while indices and wire values are unchanged; raw JSON and exports
  keep original Finnish wording; these are prototype translations, not
  independently educator-checked.

## 5. What is dynamic, and provenance rules

- Live session counts, evidence, graph, KT, and conformal values are computed
  per session; stored completed sessions are read, not recomputed.
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
```

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
| `/api/sessions` | POST | create session (`{"synthetic": true}`) |
| `/api/sessions/{sid}` | GET | student snapshot (owner token) |
| `/api/sessions/{sid}/responses` | POST | submit `{question_id, selected_index}` |
| `/api/teacher/unlock` | POST | issue PIN auth cookie |
| `/api/teacher/logout` | POST | drop teacher cookie |
| `/api/teacher/config` | GET | mode, diagnostics, call budget |
| `/api/teacher/sessions` | GET | session list |
| `/api/teacher/simulations` | POST | `{profile, seed}` synthetic run |
| `/api/teacher/sessions/{sid}` | GET | private detail (incl. answer keys) |
| `/api/teacher/sessions/{sid}/export` | GET | JSON export |
| `/api/teacher/sessions/{sid}/reviews` | POST | record educator review |

Teacher JSON endpoints contain private answer keys and are never exposed to
student routes.

## 7. Module map

- Boundary/UI: `demo_api.py`, `web_demo/` (`app.js`, `index.html`,
  `teacher.html`, `styles.css`)
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

- `live_demo_20261002/` — sessions, metadata, provider captures,
  `verification/` (rework + final drivers, logs, screenshots)
- `integrated_feedback_20261002_hosted_full/` — `report.json`, `review.html`,
  `contract.json`, `inputs.json`, captures (54 cases, 162 packages in the
  stored report); `integrated_feedback_20261002_offline/` offline equivalent
- `assessment_pipeline_20261001.json` — default source input for the
  integrated runner (not its output);
  `evidence_feedback_20261002_v2/inputs.json`
- `cleanup_result_artifacts_v2.json` — receipt for the approved cleanup:
  336 files / 7,013,843 bytes plus 2 empty package folders removed;
  consolidated reports retained byte-identical. Individual `packages/`
  folders no longer exist — do not link them.
- Historical reports, smoke runs, and repair artifacts are preserved for
  provenance; they record past runs, not current runtime state.

Frozen measured figures (from
`artifacts/integrated_feedback_20261002_hosted_full/review.md`, not
rederived): of 108 end packages, **69 match the rules baseline and 39
differ** — software identity comparison including local/cache execution,
not educational accuracy. Live sessions run fresh KT inference; the batch
report replays frozen predictions. Coverage was 54 chosen cases, not all
sequences.

### Earlier metrics and what they validate

| Evidence source | Metrics / findings | Interpretation boundary |
|---|---|---|
| `artifacts/report_fixed40_baselines_20260929/scenario_report.json` | Observed accuracy, simulated `mean_true_probability`, `mean_kt_probability`, `mean_absolute_gap`, conformal status and interval per checkpoint/skill. | Early KT/conformal behaviour check. `true_probability` is a simulator parameter, not learner ground truth; the report also predates the current descriptive-only graph. |
| `artifacts/assessment_pipeline_20261001/summary.csv` | 54 scenario labels with first-half, second-half, total observed scores and KT means. | Compares observed patterns with model probabilities; not calibration or mastery validation. |
| `artifacts/assessment_pipeline_20261001/conformal_checkpoints.csv` + `pipeline_diagnostics.json` | 432 checkpoint-skill rows; midpoint statuses `UNCERTAIN_BEHAVIOR` 139 / `CONFIDENT_STRUGGLE` 77; end statuses `MASTERY_SAFE` 6 / `UNCERTAIN_BEHAVIOR` 110 / `CONFIDENT_STRUGGLE` 100; 102 end rows have observed rate outside the interval; one `MASTERY_SAFE` row had observed rate 7/10 below its 0.8 threshold. | Synthetic stress-test counts over correlated fixed-bank cases; not empirical coverage and not a safe learner decision signal. |
| `artifacts/assessment_pipeline_20261001/llm_boundary.json` | Distractor probe: 39 KT item positions changed, max absolute probability delta 0.088278, while observed feedback stayed identical; 54 labels contain 53 unique response sequences. | Shows option identity can affect KT while the feedback branch remains observed-count-based; not evidence of misconception detection. |
| `artifacts/evidence_feedback_20261002_v2/` | 172 review scenarios, 516 feedback packages, 14,641 skill-total vectors, 239 software tests, zero provider calls. | Validates deterministic policy, evidence binding, and finite boundary coverage; does not measure educational quality or exhaust answer sequences. |
| `artifacts/provider_replay_20261002/review.md` | Older generic candidate set: 162 packages, 108 end packages, 98 fresh Jev calls, zero fallbacks; Jev selected `observed_summary` and matched rules on all 108 end packages; Jev latency median 291.7005 ms. | Provider/API compatibility on the old selector contract only; not comparable as a current Jev quality result because the candidate set later changed. |
| `artifacts/integrated_feedback_20261002_hosted_full/review.md` | Current evidence-focused replay: 162 packages, 108 end packages, 69 baseline matches / 39 differences, zero recorded failures/fallbacks; 98 successful distinct Jev requests, three Aitta contexts, Jev median capture latency about 318 ms. | Current software/provenance evidence. A different selection is not a better selection; educator review is still required. |
| `artifacts/hosted_combined_20261002_run1_review.md` | Three-package hosted smoke test; caught unsafe midpoint progress wording and teacher-audience wording. | Human semantic review found issues that lexical/interface checks missed. |

These historical metrics should inform the Scenario Library and educator
review, but they must remain labelled as replay/stress-test evidence. They do
not establish student learning, calibrated mastery, misconception detection,
or that provider decisions outperform deterministic review heuristics.

## 9. Verification (what each check proves)

Browser verification paths below are relative to
`artifacts/live_demo_20261002/`; unit-test paths are relative to this
repository folder.

- `tests/test_demo_api.py`, `tests/test_demo_service.py`,
  `tests/test_live_diagnostics.py` — unit/software tests with offline fakes.
- `verification/final/decision_drive.py` — software fixtures + rules page;
  not provider proof. `disclosure_drive.py` — rules/fake disclosure state.
  `english_drive.py` — real 40-question bank display + fake diagnostics.
  `verify_diagnostics.py` — checks captured live math; does not rerun
  inference.
- `verification/rework/browser_drive.py` — shared headless-Edge CDP helper;
  retained.
- Passing logs on record: `verification/final/english_v2_log.txt`,
  `verification/final/decision_english_log.txt`,
  `verification/final/disclosure_log.txt`,
  `verification/final/diagnostic_verification.json`. Screenshots are UI
  captures, not fresh provider proof.

Narrow commands:

```powershell
python -m unittest tests.test_demo_api tests.test_demo_service tests.test_live_diagnostics
node --check web_demo/app.js
```

Caution: a running demo keeps the native KT model resident — an idle server
does not free its weights. Avoid concurrent native-model instances (a
full-suite run hit `MemoryError` under contention; an isolated retry
passed). Coordinate stopping/restarting your own demo server, or ensure
sufficient memory, before a full-suite run.

This document update was verified with CLI and path checks only — no full
test suite or provider calls. Archiving historical artifacts has been
proposed but not performed.

## 10. Prioritised roadmap

1. **Scenario Library UI** — browse the stored 54-case results (separate from
   live hash-bound reviews).
2. **Richer feedback scope** — bounded expansion of observed-evidence review
   (e.g. individualized Aitta context), plus a reviewed practice-activity
   bank (new content; the current assessment bank is not practice-approved).
3. **Blinded educator comparison** — baseline vs provider outputs with hidden
   provenance for preference labels.
4. **Supervised pilot** — only after privacy review and educator approval.
5. **Production hardening** — real auth, TLS, RBAC, durable store, job queue.
6. **Research calibration** — validity work on the fixed assessment, in
   parallel; diagnostics remain research-only until validated.

All of the above are planned work — nothing here implies automatic approval
for learner delivery.
