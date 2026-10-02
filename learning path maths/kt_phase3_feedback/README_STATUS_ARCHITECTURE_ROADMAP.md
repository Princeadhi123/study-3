# Phase 3: status, architecture, known limitations, and development roadmap

**Snapshot date:** 2026-10-02 (updated for the hosted Jev/Aitta synthetic smoke run and mock-tested phrasing v2; the 2026-10-01 research findings and test counts below are unchanged historical snapshots)
**Scope:** the approved, fixed-order 40-question mathematics assessment  
**Purpose:** a standalone technical and research handoff for collaborators, educational review, and discussion with another assistant such as Gemini.

This document separates what is implemented from what is proposed. It does not contain an answer key, credentials, raw response sequences, or student identifiers. The referenced private artifacts must not be uploaded indiscriminately with this document.

## 1. Executive summary

The project has a functioning local assessment and feedback baseline. It serves an approved bank, accepts ordered submissions, scores deterministically, and produces observed feedback after question 20 and question 40. It also has a private research workflow using frozen knowledge-tracing (KT) predictions and historical conformal calibrations.

The final graph for the **current assessment-feedback scope** is `assessment_taxonomy_draft.json`: a bank-matched topic/subtopic hierarchy. It is not the global prerequisite graph from Phase 2. It maps questions to assessed content and supports descriptive counts and draft practice candidates based on observed errors.

The most recent complete Phase 3 test run passed **75 tests with zero failures, errors, or skips**. This demonstrates the tested software contracts, not the validity of educational recommendations or a learning benefit.

The current local flow is ready for expert review and a controlled synthetic-data Jev/LLM experiment. It is not a production deployment, a real student pilot, or a calibrated mastery assessment.

**The Jev selector and Aitta phrasing adapters are implemented for the synthetic review path only — the live student flow is unchanged. Two hosted smoke runs (2026-10-02: 2 Jev + 3 Aitta requests each on fixed synthetic fixtures) technically verified both adapters end to end — run1 with `synthetic_phrasing_v1`, then a second run with `synthetic_phrasing_v2`. A frozen 54-scenario replay added 98 fresh Jev calls across the saved matrix (all Aitta responses reused the three captured v2 contexts — reuse is not independent calls). The historical scenario evaluations used no provider calls.** Current student messages and the private teacher-end summary are deterministic templates. A provider-independent synthetic review foundation (`synthetic_feedback.py`, result schema `phase3_synthetic_feedback_review_v1`) now exists locally: it validates caller-attested synthetic inputs, builds fresh allowlisted payloads, applies a rules-baseline candidate selection and deterministic template rendering, and exposes optional selector/generator injection with deterministic fallback. `jev_selector.py` adds a real TypeSafe Jev HTTP selector adapter on that seam (`POST /v1/systemone`, bearer auth, no redirects, 1 MiB cap, sanitized failures), and `aitta_generator.py` adds an Aitta OpenAI-compatible phrasing adapter that wires an even narrower context — audience, checkpoint, and the selected candidate's id/strategy/review-status only, never evidence, counts, or skill names. Both are covered by mocked-opener tests and were technically verified in the 2026-10-02 hosted synthetic runs — run1 with `synthetic_phrasing_v1`, then a v2 smoke run (all calls successful) and a 54-scenario replay (see §15.0). No improved-output or educational-benefit claim follows.

## 2. Project boundaries and source of truth

| Layer | Responsibility | Current boundary |
|---|---|---|
| Phase 1: `../kt_phase1/` | Data preparation, sequence modeling, training, frozen model artifacts | Not retrained or modified by this feedback work |
| Phase 2: `../kt_phase2_inference/` | Private question inventory/bank, approval, deterministic scoring, frozen KT loader, historical conformal artifacts, global research graph | Read-only dependencies for Phase 3; the global graph is not used by the current assessment-feedback path |
| Phase 3: this directory | Sessions, response validation/storage, observed feedback, bank-specific taxonomy, private diagnostics, reports, and future model-mediated feedback | Active integration layer |

The approved bank is referenced at:

```text
../kt_phase2_inference/artifacts/test_question_bank_text_only_approved_v2.json
```

The bank contains private keys. It must not be copied into student payloads or sent to a hosted selector/generator.

The current bank has **40 questions, four skills, ten questions per skill**, with five questions per skill in each half. The assessed skills are basic arithmetic (`Peruslaskutoimitukset`), prices (`Hinta`), fractions (`Murtoluvut`), and percentages (`Prosenttilaskenta`). The current descriptive taxonomy has **15 subtopics**. Question order is fixed in the saved comparisons.

Changing the bank is a versioned assessment change, not a silent replacement. Sessions and the taxonomy are bound to a bank fingerprint.

## 3. Implementation status at a glance

| Component | Status |
|---|---|
| Approved fixed-40 bank and deterministic scorer | Implemented |
| Ordered single-response and 20-response-half submissions | Implemented |
| Midpoint/end checkpoint handling | Implemented |
| Student-only API projection | Implemented for local prototype |
| Descriptive skill/subtopic observed counts | Implemented |
| Distinct all-correct, all-incorrect, equal-score, and tie-safe end messages | Implemented |
| Nonjudgmental midpoint encouragement | Implemented |
| Private file-backed sessions and bank binding | Implemented; not a production transaction store |
| Frozen variant-D KT adaptation/tracing | Implemented as research metadata |
| Historical k=5/k=10 conformal application | Implemented privately; fixed-bank validity unresolved |
| Assessment-only feedback graph and observed-error practice candidates | Implemented privately; pending educator review |
| Global prerequisite routing in active Phase 3 feedback | Removed from current scope |
| Private deterministic teacher-end report | Implemented in evaluation artifacts, not a deployed teacher portal |
| Synthetic feedback review foundation (`synthetic_feedback.py`) | Implemented locally; review-only drafts, rules baseline plus injected-callback seam, no provider calls |
| TypeSafe Jev selector adapter (`jev_selector.py`) | Implemented, mock-tested, hosted smoke-verified on synthetic fixtures (run1); no real-data authorization |
| Aitta / LLM phrasing generator adapter | Implemented and mock-tested; `synthetic_phrasing_v2` smoke-verified on synthetic fixtures (3 calls in the v2 run; full replay reused those captures); deterministic fallback seam exists |
| TypeSafe agent skill (typesafe-ai/skills) | Read by reviewer for API shape; not installed into agent tooling |
| Expert-labelled feedback-selection benchmark | Not yet created |
| Independent educational review by Jo | Planned, not completed |
| Real student pilot / learning-effect evaluation | Not run |
| Production authentication and access control | Not implemented |

## 4. Current implemented architecture

### 4.1 Live local student branch

```text
Approved private bank + matching assessment taxonomy
                   |
                   v
        api.py -> MCQSessionService
                   |
        Ordered, strictly validated submissions
                   |
        Private SessionStore records observed events
                   |
                   v
        Phase 2 deterministic checkpoint scorer
                   |
        Descriptive skill/subtopic counts
                   |
        Deterministic student message
        Optional bounded plain/warm style selection
                   |
                   v
        Student-only API projection
        - checkpoint 20: no aggregate grade
        - checkpoint 40: observed total and scope disclaimer
```

This branch does not use KT, conformal statuses, or global prerequisites to choose student advice. The fact that diagnostic code exists elsewhere does not mean it is executed on a student request.

### 4.2 Private evaluation and report branches

```text
Saved synthetic response sequences on the same approved bank
                   |
       +-----------+-------------------------+
       |                                     |
       v                                     v
Observed scoring / preserved feedback    Saved frozen KT probabilities
       |                                     |
Assessment taxonomy hierarchy           Historical conformal gates
       |                                 k=5 midpoint / k=10 end
Observed-error practice candidates           |
       |                                     |
       +----------------+--------------------+
                        |
                 Private comparison report
                 - student midpoint/end examples
                 - teacher-end observed summary
                 - assessment practice candidates
                 - separately labelled research diagnostics
```

These are **parallel branches**. Conformal does not gate the assessment taxonomy's observed-error candidates. A prediction cannot erase an already observed incorrect answer.

Teacher/research presentation is private. The report is not an authenticated teacher dashboard and does not grant a teacher access to a real student's session.

## 5. Assessment taxonomy: what the graph means

Source: `assessment_taxonomy_draft.json`.

Its relationship is explicitly:

```text
is_part_of_not_prerequisite
```

Conceptually:

```text
Mathematics -> assessed skill -> descriptive subtopic -> mapped questions
```

Validation checks the fingerprint, skill IDs/names, subtopic label structure, and complete one-to-one question coverage. Unknown question IDs, duplicate/omitted mappings, and assignment to the wrong parent skill are refused. This structural validation does not establish that a content label is pedagogically correct.

The approval record is for an AI-assisted descriptive prototype audit. It is **not independent educator certification**. Jo's review can help assess whether labels and wording are educationally appropriate.

The graph supports:

- observed correct/incorrect counts by assessed content;
- identifying which assessed subtopics contain observed errors;
- draft suggestions to review that content;
- distinguishing two sequences with the same skill total but different subtopic error patterns.

It does not establish:

- prerequisites or causal learning dependencies;
- latent mastery or future success;
- a specific misconception;
- fatigue, attention, effort, or emotional state;
- that a topic with no observed errors never needs practice.

### 5.1 Current descriptive practice logic

`feedback_service.assessment_feedback_graph` accepts validated ordered responses at 20 or 40 answers and the matching taxonomy. It returns hierarchy counts and per-skill draft recommendations:

- `ASSESSMENT_SUBTOPIC_PRACTICE`: at least one assessed subtopic has an observed incorrect answer;
- `NONE`: no incorrect answer was observed for that skill in the answered questions.

All error-bearing subtopics are listed in taxonomy order; there is no learned ranking or educator-approved priority rule yet. Unanswered subtopics can appear with zero attempted items in the hierarchy and do not create error-based candidates.

The draft messages explicitly avoid misconception diagnosis. Candidates are private and pending educator review; they are not automatically added to the student message or the current style-selector input.

## 6. Current feedback behavior and audiences

### 6.1 Student midpoint, after 20 answers

- Shows observed skill and available subtopic counts.
- Does not show an aggregate grade.
- Uses completion encouragement and an optional pause before continuing.
- Does not infer fatigue, speed, improvement, or mastery.
- Does not return keys or a per-answer correctness field during submissions.

The midpoint's narrative remains general. Its per-topic counts are more informative than its prose. However, a single-item subtopic count can indirectly reveal that item's correctness. Reviewing whether these counts or more personalized advice could cue remaining answers or change assessment behavior is an unresolved educational-design task; absence of a per-answer field does not eliminate this inference risk.

### 6.2 Student end, after 40 answers

- Shows observed skill/subtopic counts and the final `correct/out_of` total.
- States that these results describe this assessment, not overall mastery.
- Distinguishes all-correct from all-incorrect performance.
- Reports tied highest/lowest scores without arbitrarily selecting one tied topic.
- Uses a neutral choice of practice topic when all skill scores are equal.
- Supports `plain` and `warm` wording.

### 6.3 Private teacher-end report

Includes a deterministic observed-result narrative, total, skill/subtopic counts, assessment-only practice candidates, and limitations. Historical conformal diagnostics are included in a **separate research section**, not as established student mastery.

No teacher-specific generative narrative, real teacher identity/access control, class dashboard, or deployed teacher endpoint has been implemented.

## 7. Current bounded selector is not an LLM integration

`compose_student_message` currently accepts an optional style-selector callback. A valid response is restricted to a single `style` value of `plain` or `warm`. Exceptions, malformed responses, extra fields, and other styles fall back to the plain deterministic template.

Current selector input:

| Checkpoint | Evidence passed to callback |
|---|---|
| Midpoint | Checkpoint and number of questions; no aggregate correct total |
| End | Number of questions, total correct, uniquely strongest skill if one exists, uniquely lowest-scoring review skill if one exists |
| Both | Prerequisite-guidance placeholder stating no approved prerequisite |

The full skills list, subtopic evidence, question text/IDs, selections, KT probabilities, and conformal diagnostics are not currently passed to this callback. The context is copied before calling it so mutation cannot alter factual message data.

The new private assessment graph does not silently expand this interface. Jev candidate selection requires a separate, deliberately designed interface.

## 8. KT: implemented model role and limitations

The research branch uses Phase 1's frozen variant D (`skill_item_content_option`) and its `best_model_joint.pt` checkpoint through Phase 2's loader. Phase 3 does not retrain it.

The adapter derives private correctness and selected text from the bank and validates embedding/vocabulary coverage. Unknown item IDs use `__UNK__`. Missing skill/content/option coverage is refused rather than silently substituting an unrelated embedding.

The outputs are **pre-answer probabilities for each current question**, not a post-test mastery score. Current-step selected text/correctness are not current query features; previous responses can affect subsequent predictions. The model uses history across the sequence, not only a per-skill percentage.

Saved comparisons are fresh-session simulations with no prior student history, no measured response times, and no real elapsed-time bins (`rt_mask=0`, `attempt=1`, `time_bin=0`). Cold-start student conditions and cold-item vocabulary conditions are different concepts and must not be conflated.

A mean KT probability is not interchangeable with observed accuracy or the synthetic probability used to generate answers. Historical predictive discrimination does not establish calibration on this short assessment.

## 9. Conformal: what is present and what it does not guarantee

Conformal **is present in the latest assessment report**, privately. It is not currently the permission to send student feedback.

- Item level: prediction sets/statuses derived from KT probabilities and historical warm/cold calibration groups.
- Checkpoint level: interval around mean KT probability using the model-implied rate uncertainty and a historical residual quantile.
- Midpoint: separate historical k=5 calibration.
- End: corrected historical k=10 calibration.
- Mixed item regimes use the cold-if-any-item-is-cold checkpoint rule.

Internal statuses are `CONFIDENT_STRUGGLE`, `UNCERTAIN_BEHAVIOR`, and `MASTERY_SAFE`. These are code labels; they do not establish latent mastery for the current bank.

The checkpoint gate receives probabilities, skill, regime, and threshold. It does **not** receive the newly observed 0/10 score and correct an overly optimistic prediction using that score.

The finite-sample conformal quantile was corrected, and k=5/k=10 were separated. These fixes are preserved. They did not create new calibration data from the current assessment. Historical distribution, chronology, dependence, and deployment changes still matter.

Conformal coverage is an across-case property under its assumptions, not a certificate that one decision is correct. It is not a text-factuality validator or a misconception detector.

## 10. Completed testing and diagnostic findings

### 10.1 Software verification

Most recent full command:

```text
python -m unittest discover -s tests -v
```

Result: **75 tests passed, zero failures/errors/skips**. The optional real-bank/frozen-KT checks ran in the local environment for this run. Some tests require private local inputs, so another environment may skip them rather than reproduce the same count of executed checks. This 75 count is the 2026-10-01 historical snapshot; the later synthetic feedback foundation plus the Jev and Aitta adapters add their own stdlib-only modules (`tests/test_synthetic_feedback.py` + `tests/test_jev_selector.py` + `tests/test_aitta_generator.py` + replay/renderer tests); the final targeted gate for the replay work passed 99 mocked tests.

Coverage includes ordering, strict option indices, midpoint/end boundaries, role projection, taxonomy matching, feedback extremes/ties, selector fallback/mutation protection, saved-data replay, assessment-only graph scope, and malformed batch rejection.

### 10.2 Synthetic comparison matrix

The saved comparison has 54 scenario labels:

- 20 deterministic extremes, skill differences, ties, alternating/streak, and half-change cases;
- two subtopic-concentration cases with matched final skill totals;
- six stochastic profiles across five seeds each: strong, weak, guessing, learning-like, fatigue-like, and weak fractions;
- two wrong-option variants with the same correctness sequence.

The five seeds are 11, 23, 37, 41, and 53. They are variations of simulation inputs, not independent students or exams.

The retained saved matrix has **53 unique selected-response sequences** because `first_half_strong` and `all_skills_equal` share one sequence. Reports mark the duplication. The generator was updated so a future fresh matrix separates those trajectories; the saved comparison was not silently resampled.

The current package contains 108 student checkpoint messages, 432 checkpoint-by-skill diagnostic records, 216 end-skill practice records, and 54 private teacher-end summaries. The observed subtopic export contains 1,512 checkpoint/subtopic rows.

### 10.3 Observed stochastic profile totals

Each row sums five 40-question synthetic sessions:

| Profile | First halves (100 answers) | Second halves (100 answers) | Final (200 answers) |
|---|---:|---:|---:|
| Stable strong | 84 | 78 | 162 |
| Stable weak | 22 | 38 | 60 |
| Guessing | 22 | 32 | 54 |
| Learning-like | 40 | 66 | 106 |
| Fatigue-like | 69 | 49 | 118 |
| Weak fractions | 64 | 68 | 132 |

These are sampled outcomes from declared profiles. They do not demonstrate actual learning or fatigue.

### 10.4 Preserved conformal stress-test results

| Checkpoint | Struggle | Uncertain | Internal safe label |
|---|---:|---:|---:|
| Midpoint: 216 skill records | 77 | 139 | 0 |
| End: 216 skill records | 100 | 110 | 6 |

Among 29 end-skill records scoring 0/10, 19 were struggle and 10 uncertain; none received the safe label. This shows abstention in some deliberately weak cases, not proof of valid bank-specific coverage.

Important counterexamples:

- Only-fractions-weak scenario: observed fractions 0/10, mean KT about 0.891, interval about [0.735, 1.000], status uncertain. The status avoided a confident safe decision, but the interval still missed the observed zero.
- In that same scenario, percentages scored 10/10 but the gate labelled them struggle. The old global routing therefore proposed review for a topic with no observed errors. The new assessment-only graph does not use that status to produce practice candidates.
- Long-correct-streak scenario: fractions scored 7/10, mean KT about 0.930, interval about [0.807, 1.000], status safe at the existing 0.8 threshold.
- Across the selected synthetic matrix, 102 of 216 end-skill observed rates were outside the intervals.

These are correlated, deliberately selected synthetic stress cases. Do not report 102/216 as an estimate of real-student coverage, or claim a causal mechanism for the KT mismatch from these examples alone.

### 10.5 Graph and distractor findings

Current end-skill practice records: **178 `ASSESSMENT_SUBTOPIC_PRACTICE`, 38 `NONE`**. More candidates or more teal cells in the plot do not mean better advice: the flag means at least one assessed subtopic has an observed error.

The retired global-routing package had 81 direct-review, 19 prerequisite-review, and 116 none records. These are archived diagnostics, not the current feedback decision rule.

Changing wrong options while keeping correctness fixed preserved observed feedback but changed rounded KT probabilities at 39 of 40 positions; maximum absolute change was about 0.088278. This demonstrates sensitivity, not improved predictive accuracy or misconception recognition.

## 11. Fixes already completed

1. Distinct factual endings for all-correct/all-incorrect results.
2. Tie-safe strongest/review wording and equal-score behavior.
3. Nonjudgmental midpoint pause/continue encouragement.
4. Fixed-assessment feed validation: four distinct skills and 5/10 per-skill denominators.
5. Strict integer option indices and half identifiers; booleans do not count as integers.
6. Prevalidation of every row in a half submission before any row is saved, preventing a malformed late row from partially consuming the session.
7. Taxonomy fingerprint and complete question-mapping validation.
8. Explicit duplicate-sequence reporting, plus a corrected future equal-score generator.
9. Saved-response replay/rescope that preserves captured KT/conformal values rather than rerunning them for presentation.
10. Removal of active Phase 3 global prerequisite loading/routing; graph requests now use the matching assessment taxonomy.
11. Separate private teacher-end summaries and research sections; no diagnostics added to student feedback.
12. Updated reports and documentation distinguishing current assessment scope from archived global diagnostics.

Batch prevalidation is not a database transaction or a guarantee against concurrent writes or an I/O failure midway through persistence.

## 12. Current artifacts and how to interpret them

**Current source snapshot:** `artifacts/assessment_pipeline_20261001.json`.

**Current report directory:** `artifacts/assessment_pipeline_20261001/`.

| File | Purpose |
|---|---|
| `feedback_report.html` | Browse student messages, private teacher-end evidence/candidates, and separate diagnostics |
| `comparison.png` | Observed counts, KT means, conformal statuses, and assessed-error practice flags in one figure |
| `summary.csv` | Per-scenario observed and KT summaries |
| `feedback.csv` | Midpoint/end student messages, prior message where available, counts, actual style-selector context |
| `subtopics.csv` | Observed checkpoint/subtopic counts |
| `conformal_checkpoints.csv` | Observed rate beside model mean, interval, regime, and research status |
| `assessment_practice.csv` | End-skill descriptive practice candidates and error-bearing assessed subtopics |
| `teacher_end.json` | Private teacher-end summaries, observed counts, candidates, and separated diagnostics |
| `pipeline_diagnostics.json` | Captured stress-test counts and scope declaration |
| `llm_boundary.json` | Proposed future input boundaries, explicitly not an implemented model client |

Older packages:

- `fixed40_comparison_20261001*`: observed/KT comparison and feedback revisions, without the full later diagnostic branch.
- `full_pipeline_20261001*`: conformal plus the now-retired global-routing scope. Do not use its graph panel as the current design.
- `report_fixed40_baselines_20260929/` and associated figures/feedback: earlier learning/fatigue baseline material, retained as historical context.

All artifacts are private local outputs under ignored paths. A report can contain private derived evidence even when it omits raw answer keys. Review what is shared externally.

## 13. Important files and responsibilities

| File | Responsibility |
|---|---|
| `phase3_paths.py` | Canonical inputs and Phase 2 import setup |
| `schemas.py` | Submission/public-question contracts |
| `session_store.py` | Private JSON persistence and bank fingerprint |
| `mcq_service.py` | Session lifecycle, ordered submissions, checkpoint scoring |
| `feedback_service.py` | Taxonomy validation/counts, assessment graph, deterministic messages, bounded selector, optional research estimate |
| `assessment_taxonomy_draft.json` | Current bank-specific content mapping and approval limitations |
| `kt_adapter.py` | Coverage checks, private event derivation, model tensors and pre-answer traces |
| `api.py` | Local student projection and separate researcher routes |
| `evaluate_scenarios.py` | Full synthetic matrix, feedback replay, diagnostic attachment and assessment-only rescope |
| `compare_scenarios.py` | Comparison tables, plots, role-separated reports and proposed model boundary |
| `scenario_runner.py` | Individual fixed-order scenarios; `graph` accepts a matching taxonomy dict, not a global graph object |
| `scenario_report.py` | Individual-scenario report compiler; current graph summaries are assessment practice candidates |
| `visualize_scenarios.py` | Individual/seed-aggregate trace plots |
| `provenance.py` | Bank/model/config/vocab records; embeddings use an explicitly labelled prefix hash |
| `demo_cli.py` | Scripted local demonstration |
| `synthetic_feedback.py` | Provider-independent synthetic review foundation: input validation, fixed candidates, rules baseline, injection seam, deterministic rendering; no adapters or provider calls |
| `jev_selector.py` | Hosted Jev selector adapter (Selector protocol): payload revalidation, allowlisted state, no redirects, sanitized errors, credential handling via `TYPESAFE_API_KEY`/local `.env` |
| `synthetic_feedback_demo.py` | Prints student-midpoint/student-end/teacher-end review packages from synthetic fixtures; `--mock` uses local test doubles, `--jev` uses hosted Jev selection |
| `tests/` | Contract, regression, API, replay, synthetic-foundation, and local optional-artifact checks |

## 14. Current API and security boundary

Student routes:

```text
POST /sessions
GET  /sessions/{session_id}/questions?half=1
GET  /sessions/{session_id}/questions?half=2
POST /sessions/{session_id}/responses
POST /sessions/{session_id}/half-submissions
GET  /sessions/{session_id}/snapshot
```

Research routes:

```text
GET  /research/sessions/{session_id}/diagnostics
POST /research/sessions/{session_id}/kt-estimate
```

The researcher header is local route separation, **not real authentication**. Research diagnostics can expose private responses and checkpoint feeds. The server deliberately binds to localhost; it should not be exposed to a network or real learners without authentication, per-session ownership, authorization, retention controls, and further security review.

There is no authenticated teacher portal or production role-based permission model. File storage does not currently establish transactional/concurrent-session guarantees. These are explicit prototype limits, not claims solved by passing API tests.

## 15. Jev + LLM architecture — local foundation implemented, providers pending

TypeSafe AI's Jev is proposed as a typed decision/selection model. It is not the KT predictor, the conformal calibration mechanism, or the model that writes feedback prose. Jev's HTTP API shape has been verified from live documentation and is implemented behind the seam in `jev_selector.py` (mock-tested and smoke-verified on synthetic fixtures, run1). The phrasing side targets the CSC OpenAI-compatible Aitta endpoint already used by the existing `tagging_common` code; `aitta_generator.py` implements that bounded generator contract (mock-tested; `synthetic_phrasing_v2` smoke-verified on synthetic fixtures).

```text
Validated observed evidence from the approved bank
                         |
Assessment-only hierarchy and policy checks
                         |
Draft candidate builder -> versioned, review-labelled candidate set
                         |
             Rules baseline OR Jev selection
                         |
Selected valid candidate + bounded evidence + audience
                         |
                Optional LLM phrasing
                         |
Schema / ID / factual-reference checks and review flags
                         |
       Valid output OR deterministic fallback
                         |
Local review package: student midpoint, student end, teacher end
```

### 15.0 Implemented provider-independent foundation

`synthetic_feedback.py` (stdlib only, no network/credential/file access, no persistence) implements the local parts of this flow as a review-only foundation:

- strict `phase3_synthetic_feedback_input_v1` validation — caller-attested `data_origin="synthetic"` (attestation, not proof of origin), audience/checkpoint rules (teacher midpoint refused), exactly four validated skill rows with fixed 5/10 denominators;
- the fixed `synthetic_candidates_v1` / `synthetic_policy_v1` set: `observed_summary` then `neutral` at end, `neutral` only at midpoint, all `draft_pending_educator_review`;
- fresh allowlisted payloads per call: student midpoint sends only audience/checkpoint plus permitted candidates (privately held midpoint counts never leave the module); end payloads carry only validated skill rows and the deterministic `{correct, out_of: 40}` total;
- optional injected `Selector`/`Generator` protocols on deep-copied payloads; selector failure or invalid selection falls back to the rules baseline and skips the generator; generator failure or invalid output falls back to fixed wording for the same candidate; no retries;
- generated openings pass `shape_and_heuristic_checks_only` — a shape/lexical guard, not semantic grounding; observed-count lines are always application-rendered; every result is `draft_not_for_learner_delivery` with `requires_human_review=True`;
- a private `phase3_synthetic_feedback_review_v1` trace records candidate/policy/prompt versions, selection and phrasing sources, fallback reason, measured latency, and provider metadata slots that capture injected adapter class names, with `model_version`/`cost` remaining None until a real provider exists.

`jev_selector.py` now implements the selector side against the documented Jev HTTP API: it revalidates the exact `phase3_synthetic_selection_v1` payload before requesting (rejecting private/extra fields and tampered totals, including bool-typed counts), posts only the allowlisted `{audience, checkpoint, evidence, candidates}` state plus the fixed `jev_selection_v1` choice question and per-candidate criteria, resolves single-candidate midpoint selections locally with no request, refuses redirects, caps responses at 1 MiB, validates choice/type/model/usage strictly, keeps no probabilities or confidence, and sanitizes all provider failures to `JevSelectionError` with `last_metadata` status transitions (`not_called` / `not_called_single_candidate` / `completed` / `failed`). Credentials resolve via `JevSelector.from_env()` — `TYPESAFE_API_KEY` in the environment first, else the Phase 3-local `.env`; the repo-root `.env` is never read and nothing loads at import time.

Still pending: the TypeSafe skill package installation (reviewed as documentation, not installed), and a real provider account agreement. Injected synchronous callbacks cannot be forcibly timed out inside the runner — adapters own their own transport timeouts (`JevSelector`/`AittaGenerator` forward their `timeout` to the opener). The review-package trace keeps `provider_metadata.model_version`/`cost` as None placeholders; under `--jev`/`--aitta` the real provider model, token usage, and prompt versions (`jev_selection_v1`/`synthetic_phrasing_v2`) are recorded in each package's adjacent `selector_metadata`/`generator_metadata`.

**First hosted synthetic smoke run (2026-10-02, run1)** — capture `artifacts/hosted_combined_20261002_run1.json`, frozen review `artifacts/hosted_combined_20261002_run1_review.md` (local gitignored artifacts): 2 Jev + 3 Aitta requests completed on the fixed synthetic fixtures with no fallback; Jev reported model `jev-1.13.0`, Aitta `openai/gpt-oss-120b`; Jev selected `observed_summary` for both end packages, matching the rules baseline; midpoint resolved locally with no Jev call. Recorded end-to-end latency per package: midpoint 11187.868 ms, student end 3569.347 ms, teacher end 2248.344 ms (end figures cover both providers — not standalone Jev latencies). Human review found the midpoint opening's "making solid progress" wording ambiguous (a possible performance/learning implication the v1 heuristic did not reject) and the teacher-end opening addressing the teacher as the assessment taker — motivating `synthetic_phrasing_v2`, which prohibits progress claims and requires third-person teacher wording. No educational-effectiveness or selection-quality claim follows from one synthetic profile.

**v2 smoke run + frozen 54-scenario replay (2026-10-02)** — captures `artifacts/hosted_combined_20261002_v2.json` and `artifacts/provider_replay_20261002/` (`inputs.json`, `baselines.json`, `report.json`, `provider_captures/`, `execution.log`), frozen review `artifacts/provider_replay_20261002/review.md` (local gitignored artifacts; `render_provider_replay.py` renders the report to a local HTML page). The v2 smoke run made 2 Jev + 3 Aitta requests, all successful — the revised openings no longer carry the "making solid progress" claim and no longer address the teacher as the assessment taker; the deterministic teacher-end fallback was likewise corrected to "The assessment record is ready for review.". The replay covered all 54 saved scenario labels — 162 packages (student midpoint, student end, teacher end), 53 distinct response sequences and 49 distinct end count vectors in the source — with 98 fresh Jev calls (ten end selections reused identical requests; Jev never called for midpoints) and **zero** additional Aitta calls (all responses reused the three captured v2 contexts — reuse is not independent calls). All 108 end selections were `observed_summary`, matching the rules baseline; zero fallbacks; Jev reported `jev-1.13.0` with fresh-call client latency min 236.915 / median 291.7005 / max 578.435 ms. This establishes API compatibility and bounded behavior across the saved evidence profiles — not selection quality, not educational benefit, and no model probability/confidence claims.

Draft candidates can be tested before Jo's review only in the explicitly labelled synthetic/local experiment. Educator review and approval are required before treating them as learner-ready; approval has not yet occurred.

In parallel, KT/conformal remain labelled private research diagnostics. They do not authorize student-facing claims in the initial prototype.

### 15.1 Candidate selection

A candidate is a permitted message strategy or review action derived from evidence, not arbitrary generated advice. Examples of strategies include acknowledging an observed strength, proposing one assessed practice area, combining the two, or using neutral encouragement when evidence is sparse.

The current graph's two recommendation types are not yet a fully reviewed, diverse Jev candidate set. It lists all observed-error content; limits, priority rules, candidate identifiers, and allowed combinations still need design and review.

Simple cases may be handled better by deterministic rules. Jev should earn its complexity in a fair comparison rather than be assumed superior because it returns probabilities.

### 15.2 LLM role

The LLM expresses a selected strategy for the intended audience, preserving evidence and scope. It does not score answers, override counts, invent prerequisites, identify fatigue, or claim mastery/misconceptions from a few answers. `aitta_generator.py` now implements this role on the synthetic seam: the model receives only `{audience, checkpoint, selected_candidate: {candidate_id, strategy, review_status}}` — never evidence, counts, or skill names — and writes a neutral opening while application code renders all facts.

Output validation must include strict shape/identifiers and supported numerical references. Deterministic checks cannot guarantee all prose is educationally sound or semantically grounded; unsupported statements must be rejected or flagged, and human review remains necessary.

### 15.3 Failure behavior and provenance

The local foundation already fixes malformed-choice handling, no-retry behavior, and deterministic fallback; the Jev adapter additionally implements a configurable transport timeout (default 30 s, forwarded to the opener) and zero retries. The Aitta generator adapter applies the same transport policy (timeout, zero retries, no redirects, 1 MiB cap, sanitized failures) plus its own output contract — a single `finish_reason="stop"` choice whose JSON content is exactly `{candidate_id, opening}`, validated usage counts, and failure surfaced to the runner as `generator_error`. Abstention semantics for real data and provider failure policy beyond this seam remain unsettled. Do not copy a provider confidence threshold into the pipeline without local validation.

The private run1 capture records model/version, candidate/policy/prompt versions, sanitized evidence, selected candidate, generated text, validation outcome, latency, token usage, and fallback cause. Monetary cost remains unknown; no cost estimate is claimed.

## 16. Hosted-model data boundary

`synthetic_feedback.py` now implements the local allowlist mechanically: every outbound payload is built fresh per call, student-midpoint payloads omit all skills/counts, and end payloads carry only validated skill rows and the deterministic total. Hosted Jev and Aitta clients are implemented (`jev_selector.py`, `aitta_generator.py`) and have made two hosted smoke runs plus one frozen-scenario replay on synthetic fixtures only (run1 and v2: 2 Jev + 3 Aitta each; replay: 98 fresh Jev calls, Aitta fully cached) — nothing here authorizes sending real (non-synthetic) data. Retention terms, provider agreements, and real-data consent remain unsettled, so the lists below stay proposed boundary rules rather than an active data-sharing authorization.

Potentially allowed after agreement and review:

- audience and checkpoint;
- observed skill counts with denominators;
- descriptive assessed subtopic counts with denominators;
- end-only observed total;
- reviewed candidate IDs and their permitted scope;
- bounded style/format instructions.

Do not send in the initial prototype:

- private answer keys or the entire bank;
- raw question/option text or selections;
- question identifiers, student/session identifiers, or timestamp trails;
- KT/conformal probabilities or internal mastery labels as feedback facts;
- global prerequisite routing;
- unreviewed practice candidates as if they were approved;
- unsupported misconception, learning, fatigue, or mastery labels.

Use only synthetic evidence summaries for initial hosted tests. Actual provider choices, API access, credential storage, data-retention terms, and consent/privacy arrangements remain to be settled. Credentials must never appear in documentation, source control, or chat messages.

A later proposal to send different evidence needs explicit review; this document does not authorize broader data sharing.

## 17. Jo's educational review

The project owner plans to ask Jo, a senior education researcher, to review feedback for student midpoint, student end, and teacher end. This review has not occurred and no endorsement is claimed.

Showing working Jev/LLM examples before this review is reasonable within a clearly labelled local research prototype. Those examples must not be treated as already approved for learners.

For each example, provide:

1. Sanitized observed evidence with denominators.
2. The allowed candidates and selection policy version.
3. The selected candidate and how it was chosen.
4. Template baseline and proposed model-generated message.
5. Output-validation result and any caveats.
6. Audience and checkpoint.

Ask for review of factual grounding, clarity, actionability, tone, misleading diagnosis/labels, list length, and whether midpoint feedback changes the assessment experience. If comparing methods, blind method identity where feasible and keep the evidence consistent.

Expert acceptability is not evidence of a causal learning gain. A student study is a separate later stage.

## 18. Unresolved flaws, limitations, and risks

| Issue | Current evidence / limitation | Needed next step |
|---|---|---|
| High KT estimates for low observed scores | Contrived weak-skill scenarios show discrepancies; cause is not established | Check temporal/query alignment, coverage, history sensitivity, and real matched-protocol predictions |
| Historical conformal mismatch | Selected fixed-bank stress profiles produce missed intervals and inconsistent statuses | Evaluate on real protocol-matched responses and consider recalibration using independent data |
| Sparse subtopic evidence | Some subtopics have few questions | Show denominators and use descriptive language; do not diagnose |
| Long error lists | Candidate builder lists all error-bearing subtopics | Educator-reviewed prioritization and maximum output length |
| Midpoint narrative is generic | No model-personalized midpoint prose exists | Review neutral versus targeted variants and assessment-cueing effects |
| Fixed bank/order and repeated simulation inputs | 54 labels, 53 unique sequences; five seeds per stochastic profile | Report limits; expand independent evaluation design if the research question requires it |
| No Jev/LLM benchmark | No independent preferred-candidate labels | Build reviewed examples and held-out evaluation before claiming improvement |
| No actual student outcomes | Synthetic runs and software tests only | Later ethically reviewed student pilot with predefined outcomes |
| Local-only access model | Research header is self-declared; no teacher portal | Implement real role/ownership access before real deployment |
| Session persistence reliability | File-backed prototype, no full transaction/concurrency guarantee | Test and design persistence/locking appropriate to deployment |
| Future model output risk | Fluent wording may invent plausible claims | Restrict input/output, validate, fallback, and review |
| Taxonomy review status | AI-assisted descriptive prototype approval, not independent certification | Ask for educational/content review without implying it has already happened |

Do not promise every issue can be fixed by tuning a threshold. Better conformal performance may require better KT predictions and matched calibration data. One incorrect confident decision does not prove conformal is universally broken; conversely, good historical coverage does not prove this assessment is calibrated.

## 19. Development roadmap and exit criteria

### Stage A: freeze the observed-evidence baseline

Implemented in substantial part: assessment-only scope, deterministic messages, response validation, report separation, and regression checks.

Before changing the interface, record bank/taxonomy versions, preserve the current scenario artifacts, and specify audience/checkpoint evidence boundaries. Keep global prerequisites out of the current feedback path.

Exit criterion: reproducible local outputs and no ambiguity about current versus archived graph scope.

### Stage B: define the model-mediated feedback experiment

Select Jev access and an LLM/provider; agree to synthetic summary-only inputs. A draft contract now exists as implemented code: `phase3_synthetic_feedback_input_v1` inputs, the `synthetic_candidates_v1`/`synthetic_policy_v1` candidate set, and the `synthetic_phrasing_v2` bounded generation specification with deterministic fallback. Jev's API is verified and implemented; the generator side is the CSC OpenAI-compatible Aitta endpoint already exercised by the existing `tagging_common` code, now implemented behind the seam as `aitta_generator.py` (mock-tested; v2 smoke-verified on synthetic fixtures). These are local review-only artifacts, not provider-agreed contracts — provider agreements and input approval are still pending.

Exit criterion: explicit input/output contract and evidence-supported draft candidates, with review status visible.

### Stage C: implement and test the local Jev/LLM prototype

Provider-free groundwork is implemented: the injected `Selector`/`Generator` seam, validation, deterministic fallback, and the private review-package trace in `synthetic_feedback.py`, plus `synthetic_feedback_demo.py` printing student-midpoint, student-end, and teacher-end packages from identical synthetic fixtures (deterministic, `--mock`, and explicit-opt-in `--jev`/`--aitta`/`--jev --aitta` modes, with an optional `--jev-env-file` for a private key file outside the project). Both adapters are now real: `jev_selector.py` implements the documented Jev HTTP contract and `aitta_generator.py` implements the OpenAI-compatible Aitta phrasing contract — both with sanitized failures, credential loading, and strict response validation — verified in the run1 and v2 hosted synthetic smoke runs and exercised across the frozen 54-scenario replay (§15.0). Still pending: any provider-agreement items and richer reviewed candidate sets.

Already covered locally: invalid candidate IDs, extra/malformed fields, callback exceptions, mutated payloads, fabricated diagnoses/numeric/skill-name/progress prose, fallback paths (including the corrected third-person teacher end fallback), bad HTTP/timeout/redirect/malformed-body responses, unknown choices, and metadata resets. The hosted runs verified successful requests only; actual timeout/failure behavior and variability across hosted reruns remain untested.

Exit criterion: the technical path works end to end, unsupported outputs are rejected/flagged, and no secret/raw private assessment data is exposed. This is not a claim of educational validity.

### Stage D: expert review and comparative evaluation

Show Jo evidence-grounded examples and the template baseline. Revise candidate wording, prioritization, and message length. Build an independent held-out set for comparing selection methods.

Exit criterion: documented review decisions and evidence of selection/feedback quality, not just successful requests.

### Stage E: KT/conformal investigation in parallel

Check prediction alignment and coverage without altering frozen artifacts. Evaluate actual pre-answer predictions against real responses under the intended short-assessment protocol. Audit training/model-selection/calibration/test separation and within-student dependence. If recalibrating, use separate calibration and evaluation data; do not tune on the evaluation set or fit only to these stress profiles.

Measure discrimination, probability error/calibration, interval coverage/width, abstention, missed struggle and unsupported confident labels by relevant skill/checkpoint/regime. Confidence intervals should respect participant-level dependence.

Exit criterion: evidence supporting the intended use under the target protocol. Only then consider a separate experiment allowing diagnostics to influence permitted feedback actions.

### Stage F: broader assessment and deployment

When questions/topics expand, update the bank and mapping together, review content, revise hard-coded 40/20 and four-skill assumptions, recheck embeddings, revisit calibration applicability, and expand regression coverage. Do not score existing sessions against a changed bank.

Before a real student deployment, add authenticated student/teacher ownership and role boundaries, secure persistence, retention/audit policy, operational monitoring, and appropriate study/consent arrangements.

## 20. Research evaluation plan

A paper should distinguish three evidence tiers:

1. **Software/system correctness:** validation, determinism, privacy/role separation, fallback, artifact integrity, and reproducibility.
2. **Prediction and selection quality:** real held-out KT predictions; calibration/interval diagnostics; independent expert judgements of feedback/candidate selection; cost, latency, and robustness.
3. **Educational impact:** later participant outcomes, including motivation/usability and learning measures when a suitable study is designed.

The current work mainly supports tier 1 and synthetic stress diagnostics. It does not establish tiers 2 or 3 for the deployed assessment-feedback use case.

Suggested comparison arms, not yet implemented:

- deterministic observed-evidence selection;
- Jev selecting from the same candidates;
- optional LLM selector using the same candidates;
- a separately specified experimental conformal-gated policy, only if that research question is justified.

Keep candidates, evidence, audience, and wording method consistent when comparing selection. If one arm has richer evidence or better generation, do not attribute all improvement to its selector.

Jev is not a replacement for conformal: one selects permitted communication/actions, the other characterizes model-prediction uncertainty. A nicer message does not validate a prediction interval. Jev's own judgement of its selections is not independent ground truth.

## 21. Useful local commands

Run commands from this directory. Use one simple command at a time, especially in Windows approval-sensitive environments. No background execution is required for report generation.

### 21.1 Tests

```text
python -m unittest discover -s tests -v
python -m unittest tests.test_scenarios tests.test_feedback_service -v
```

The full suite can load large frozen inputs for optional local tests. Mock/unit checks are faster.

### 21.2 Assessment-only rescope using existing saved diagnostics

```text
python evaluate_scenarios.py --replay artifacts/full_pipeline_20261001.json --assessment-only --out artifacts/assessment_pipeline_review.json
python compare_scenarios.py artifacts/assessment_pipeline_review.json --full-pipeline --out-dir artifacts/assessment_pipeline_review
```

These preserve saved responses, KT traces, and conformal outputs while replacing retired graph scope. Output names above are examples of fresh names; choose another if they already exist. Existing outputs are not overwritten.

### 21.3 Apply historical diagnostics to a saved observed/KT comparison

```text
python evaluate_scenarios.py --replay artifacts/fixed40_comparison_20261001_v2.json --diagnostics --out artifacts/assessment_diagnostics_review.json
```

This applies historical k=5/k=10 gates to saved KT probabilities and uses the matching assessment taxonomy, not a global graph. It does not run the frozen model again, but it does calculate diagnostic intervals. Use rescope instead when those diagnostic values have already been captured and only presentation/scope changes are needed.

### 21.4 Fresh single-scenario run

```text
python scenario_runner.py --profile all_correct --graph --out artifacts/scenario_review_all_correct.json
```

Here `--graph` means the bank-specific taxonomy. It does not require KT. A fresh KT/conformal scenario requires the appropriate model inputs and options and should be distinguished from saved-data rendering.

### 21.5 Local API

```text
python api.py --host 127.0.0.1 --port 8765
```

This intentionally keeps serving until stopped. Do not expose it beyond localhost or start it merely to regenerate a report.

## 22. Reproducibility and sharing guidance

- Preserve captured evidence when changing presentation. Do not resample responses or rerun a model merely to redraw a figure.
- Keep new outputs in a fresh private artifact path and link clearly to the source snapshot.
- Bank fingerprints and taxonomy matching are enforced; diagnostic manifests record calibration/taxonomy files. `provenance.py` can record frozen model/config/vocab inputs; its embedding prefix hash is not a full-file checksum.
- The saved comparison does not automatically provide a complete future Jev/LLM provenance trail. That must be added with provider integration.
- The most recent test count is a snapshot, not a promise all future code passes.
- Share this handoff as design/status context. Do not upload the entire `artifacts/` tree, private bank, embeddings, model files, or raw research sessions to another assistant without explicit review and authorization.
- Open the current assessment report, not the archived global-routing PNG, when discussing current behavior.

## 23. Bottom line

The current baseline is coherent for **observed feedback on this specific assessment**. The assessment-only graph is now aligned with the agreed scope, the tested software flow is functioning, and a controlled Jev/LLM prototype can be developed without using unresolved KT/conformal labels as student-feedback facts.

The next concrete work is to settle the model/provider and input contract, implement a constrained selector/generator experiment with validation/fallback, and prepare evidence-grounded examples for Jo. Educational approval, real-response calibration, and real-student effectiveness remain separate unfinished stages.
