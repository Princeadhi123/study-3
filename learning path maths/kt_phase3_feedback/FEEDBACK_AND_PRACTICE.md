# Feedback and future practice: explicit separation

Decision recorded 2026-10-06. This clarifies component responsibility rather
than discarding the prior work. Historical pre-answer prediction remains a
valid evaluation task: the recorded target answer is hidden before prediction.
The issue was conflating that task with explaining an already observed error.

## Implemented architecture

```text
Completed answers (20 / 40)
  -> deterministic scoring and descriptive content graph
  -> observed evidence -> permitted Jev focus -> Aitta opening
  -> feedback draft (existing branch, unchanged)

Completed answers (40 only) + hash-bound 24-question v2 practice pool
  -> same observed history + one unanswered candidate query at a time
  -> frozen KT next-response probabilities
  -> experimental practice policy + observed-error baseline comparison
  -> teacher-private shadow result (new, opt-in; never delivered to students)

Assessment answer prefix (20 / 40)
  -> frozen pre-answer KT trace, without live conformal computation
  -> teacher-private research diagnostics

Saved historical conformal artifacts
  -> preserved offline analysis only; excluded from active API/UI output
```

Feedback baselines are built before research jobs are admitted. Providers and
research use separate workers. Future queries reuse the serialized frozen
model worker, not the provider worker; feedback does not wait for either
research result. No active worker loads conformal calibration.
They do not wait for Jev/Aitta completion or use their generated text.

## Component boundaries

| Component | Input | Authority |
|---|---|---|
| Scoring/content graph | Submitted indices, private keys, matching taxonomy | Observed counts and descriptive grouping |
| Jev classifier | Allow-listed aggregate evidence and permitted candidates | Select a permitted observed-evidence feedback focus |
| Deterministic renderer | Validated evidence and selected focus | Render factual counts and authored review actions |
| Aitta | Narrow communication context and selected candidate identity | Opening only; no replacement of the evidence-rendered body |
| Future KT | Forty observed responses plus one unanswered query | Estimate candidate response correctness |
| Shadow practice policy | Observed topic errors, explicit pool, probabilities, explicit band | Record an experimental recommendation and baseline for research |
| Historical conformal analysis | Preserved predictor outputs and calibration | Offline evidence only; absent from active runtime/API/UI |

No raw response history, answer keys or research probabilities enter Jev/Aitta.
The student snapshot receives neither candidate probabilities nor the shadow
recommendation. Teacher-authenticated session detail/export contains
`checkpoints.end.recommendation` and `recommendation_job`.

`used_for_student_advice` and `used_for_feedback` are false for every shadow
recommendation. Jev failure falls back to rule-based selection; Aitta failure
retains the selected focus with deterministic opening wording. These existing
behaviors are not changed by the new branch.

## Explicit experimental policy

Implementation: [shadow_practice.py](shadow_practice.py).
Version: `observed_error_topic_then_explicit_probability_band_v1`.

1. Validate the completed assessment and recompute observed evidence.
2. Exclude practice questions sharing an assessed question ID or normalized
   prompt. Exclude unassessed topics and topics with no observed errors.
3. For the eligible pool, record a no-KT baseline: most topic errors, then
   highest topic error fraction, then lexicographic question ID.
4. Predict each eligible candidate independently from the same forty-response
   prefix. Candidate B is not appended after candidate A as though A had
   already been answered.
5. Retain predictions in the explicitly configured inclusive probability band.
   Prioritize most topic errors, then highest error fraction, then probability
   closest to the band's midpoint, then lexicographic question ID.
6. If the eligible pool or in-band pool is empty, abstain. Do not force an
   outside-band choice. A missing/incompatible model or invalid predictions
   produce an unavailable result, without changing feedback.

There is no default probability band. A caller must specify it. For example,
`0.40 0.80` is an illustrative experimental choice, not validated ZPD, boredom,
frustration, mastery, or optimal learning difficulty. This implementation
does not authorize adaptive learner delivery or establish educational benefit.
A separate study must freeze the policy and outcomes before evaluating it.

Implementation: [future_kt.py](future_kt.py).
All forty observed selections remain legitimate history. Query correctness,
selection and response fields are fixed neutral placeholders; candidate keys
are not consulted. The frozen variant D sees a 41-position sequence and only
the final probability is extracted. No-history probability is not substituted.
History lacks response times; its mask is zero, attempts default to one and
time bins to zero, matching the current MCQ adapter's documented assumptions.
This is current-assessment history, not complete lifetime student history.
History content/option embeddings and query skill/content membership are
required. Unknown candidate item IDs use the trained UNK representation.

## Practice pool: not an automatic bank replacement

The initial wiring did not create a pool. A 20-question research-only pool
is now frozen for the native smoke study below; it is not learner-approved.
The existing demo assessment and final warm/cold research banks are preserved.
Their roles are different:

- Demo bank: existing fixed assessment and observed-feedback baseline.
- Final warm/cold banks: frozen Phase 4 evidence and possible later paired
  synthetic research inputs; not automatically practice-approved.
- Practice pool: separately checked, unattempted candidate content compatible
  with the assessed topics and frozen KT representation.

Pool JSON must contain exactly:

```json
{
  "schema": "phase3_shadow_practice_pool_v1",
  "scope": "research_only_not_learner_approved",
  "questions": []
}
```

Each question requires `question_id`, `skill_id`, `item_id`, `text`, `options`
and `content_text`. Content must be exactly the stem followed by
`" [OPTIONS] "` and options joined by `" | "` in offered order. IDs and
normalized prompts must be unique. An optional private `answer_index` is
validated structurally but never used to predict the candidate.
Schema validation does not independently check mathematical correctness,
distractor quality or semantic topic alignment; those are required content
checks before interpreting a native practice experiment.

## Opt-in use

The default demo leaves KT recommendations disabled. The teacher's Skill map
tab still shows graph v3 assessed tasks and all 24 practice drafts, pending
formal educator review. To opt into the existing experimental KT selector,
from this folder:

```powershell
$env:KT_PHASE2_TEXT_EMBEDDINGS = (Resolve-Path "..\kt_phase2_inference\artifacts\text_embeddings_v2_synthetic_practice_20261007.npz").Path
python demo_api.py --providers rules `
  --shadow-practice-pool artifacts/synthetic_practice_supplement_20261006/practice_pool_v2_private.json `
  --shadow-target-band 0.40 0.80
```

The active service rejects other pools, including the original 20-question
smoke pool. Set the environment before starting Python; a stale Phase 2
embedding-path import is rejected. The band is illustrative, not an endorsed
study configuration. Both flags are required
together. This uses the normal synthetic-only localhost demo; it does not
deploy a live ViLLE integration or send recommendations to learners.

The end result is `selected`, `abstained`, or `unavailable`; unconfigured
sessions report `disabled`. Queue capacity is shared with diagnostics.
Interrupted pending recommendation jobs become unavailable after restart;
they are not silently resumed. A completed shadow record includes pool,
assessment and response hashes, policy/band, candidate probabilities, excluded
IDs/reasons and the observed-error baseline. It stays in ignored private
session artifacts.

## Evidence retained and remaining work

Phase 4 results, its CPU qualification and independence audit remain unchanged.
Conformal's individual sets and aggregate score intervals cannot be reused
as per-question probability confidence intervals. Legacy raw gate names,
including `MASTERY_SAFE`, are not validated mastery conclusions.

The new tests use fake models and synthetic fixtures to check query encoding,
policy behavior, privacy, failure isolation and application wiring. They do
not establish native prediction performance, learning benefit or conformal
coverage for recommendations.

### Native smoke run (2026-10-06)

A small frozen native smoke run was executed under
[SHADOW_SMOKE_PROTOCOL.md](SHADOW_SMOKE_PROTOCOL.md); review and frozen
outputs are in `artifacts/shadow_smoke_20261006/` (SMOKE_REVIEW.md). It used
10 cases (5 per research bank) with 20 candidates, all warm item IDs, and an
explicit 0.60-0.80 band. Outcomes: warm 3 selected / 2 abstained, cold 4
selected / 1 abstained, none unavailable; candidate topic quotas 6/2/6/6.
A sampled response-blind check on 8 cases matched full-input probabilities
within 1.1920928955078125e-07 (tolerance 1e-6). All feedback was unchanged.
The main run's 108 candidate predictions are not 108 scenarios. This ran
through the separate offline research seam (`recommend_research`), never an
approval path or public serving, and the pool/quota/band were frozen
experiment inputs - a smoke check, not a validated policy or completed
108-scenario study.

A separate content-only supplement under
`artifacts/synthetic_practice_supplement_20261006/` adds four authored
divisibility prompts accepted in user review and still pending formal
educator review. It does not alter the frozen smoke pool. An augmented
research embedding table,
`kt_phase2_inference/artifacts/text_embeddings_v2_synthetic_practice_20261007.npz`,
makes the four prompts KT-text-compatible; a scripted all-correct smoke check
passed exact coverage and returned cold-item probabilities. The same directory
contains `practice_pool_v2_private.json`, a separate 24-question pool with
6/6/6/6 topic counts. Use it only with `KT_PHASE2_TEXT_EMBEDDINGS` pointing to
the augmented table; the original frozen pool/table and all completed results
remain unchanged.

### Source-masked practice comparison (prepared 2026-10-06)

Ten source-masked review tasks are prepared under
[PRACTICE_COMPARISON_PROTOCOL.md](PRACTICE_COMPARISON_PROTOCOL.md); the offline
reviewer page is `artifacts/practice_comparison_20261006/reviewer/index.html`
(private, git-ignored). Zero human reviews are completed. Across six
hypothetical response worlds the scripted comparison shows no consistent KT
advantage; see `artifacts/practice_comparison_20261006/investigator/COMPARISON_REVIEW.md` for the authored analysis.
No learner delivery and no new KT inference are involved.

### Bounded content graph (prepared 2026-10-06; v3 default)

A descriptive, hash-bound map of the frozen warm/cold banks and the
24-question v2 practice pool is captured under
`artifacts/bounded_content_graph_v3_20261007/` (private, git-ignored) and
rendered offline by `research_content_graph.py`; see
[RESEARCH_CONTENT_GRAPH.md](RESEARCH_CONTENT_GRAPH.md). The v3 update
(`research_content_descriptions_v3.py`) transforms the preserved v2 graph
and replaces only the original 20-question practice source with
`practice_pool_v2_private.json`. The practice source now has 6/6/6/6 topic
counts; the four added divisibility questions are synthetic,
user-review-accepted, pending formal educator review, and not historical
student content.

The v2 update (`research_content_descriptions.py`) reframes annotations as
task descriptions: 7 proposed procedural/conceptual support links - not
strict prerequisites or a learning order - plus 4 cautious option
interpretations. Task requirements are not measured student skills. The
update used automated, user-supplied Gemini review input, not independent
educator validation; no LLM authority is expanded. The original v1 capture
`artifacts/bounded_content_graph_20261006/` and v2 capture
`artifacts/bounded_content_graph_v2_20261006/` are preserved unchanged. All
annotations stay pending educator review (zero completed); nothing routes
content, reaches students, or claims mastery/diagnoses, and conformal
removal, multi-question selection and teacher release remain unimplemented
here.

The smoke pool matches the final research-bank topics, not the different
skill IDs in the unchanged demo assessment. The original 20-question pool and
its smoke results remain preserved as a completed experiment only.

## Implemented transition (decision recorded 2026-10-07)

The following transition requirements are implemented in `demo_api.py`,
`demo_service.py`, `research_runtime.py` and `web_demo/`. Formal educator
approval and learner release remain pending.

The start page and teacher simulations select `demo`, `warm` or `cold`.
Each session binds one 40-question bank for its entire lifetime; restart
validation checks the bank mode, canonical bank/taxonomy hashes and source
provenance. Research source-file hashes remain distinct from canonical JSON
fingerprints. Research taxonomies are derived from v3 `assesses` edges and
are labeled `descriptive_pending_formal_educator_review`. The approved demo
validator remains strict; research banks use a separate validation/scoring
path without changing their approval metadata.

Student HTTP submissions contain only `question_token` and `selected_index`.
Tokens are scoped to a session and position; the latest identical submission
can be retried. Private question/item IDs, keys, graph context and paths stay
server-side. Display translations are keyed by public prompt text; Finnish
originals are available and English fallbacks are labeled. Teacher content
context contains assessed concepts and task descriptions, never proposed
support links or possible-error annotations. It is not sent to Jev/Aitta.

Active JSON projections remove archived conformal/calibration fields, including
historical replay exports, without rewriting their source captures. The
legacy `api.py` is a separate approved-demo-only prototype; `demo_api.py` is
the active UI entry point.

`tests/test_research_runtime.py` and the service/API tests cover synthetic
mixed-answer warm/cold completion, restart isolation, opaque response tokens,
v3 mappings and current pool/embedding guards. The frozen-input regression in
`tests/test_provenance.py` checks the pre-transition hashes recorded in
`tests/frozen_transition_manifest.json`, including the original embedding table,
weights, banks, smoke/comparison results and all three graph captures.

Recorded requirements retained for context:

1. **Use the 24-question practice pool for new work.** The accepted pool is
   `practice_pool_v2_private.json` with 6/6/6/6 topic counts and four
   user-review-accepted synthetic divisibility questions. Do not delete the
   original 20-question pool; it is frozen evidence for the completed smoke
   run, not the current practice source. KT use of the v2 pool requires the
   augmented embedding table, not the original frozen embedding table.
   - Frozen KT weights and preprocessing remain unchanged; no retraining is
     authorized. The augmented table only appends four exact text embeddings
     for the synthetic prompts/options.
   - The four synthetic `item_id` values are not in the learned item
     vocabulary and therefore use the model's cold/unknown-item
     representation. This makes them queryable, not historically observed
     items.
   - Any KT-assisted v2-pool run must explicitly select
     `kt_phase2_inference/artifacts/text_embeddings_v2_synthetic_practice_20261007.npz`
     through `KT_PHASE2_TEXT_EMBEDDINGS`; the original frozen embedding table
     remains the default and unchanged.
   - `kt_embedding_check.json` proves input compatibility only. Its
     probabilities are a scripted smoke result, not difficulty,
     appropriateness, learning-benefit or performance validation.
2. **Remove conformal from the active prototype.** Stop computing and
   displaying live conformal diagnostics in `demo_service.py`,
   `live_diagnostics.py` and `web_demo/`. Keep historical Phase 4 conformal
   outputs as preserved research evidence; do not erase prior reports or
   claim that removing the UI path invalidates them.
3. **Add a research-bank session mode.** Keep the demo bank preserved, but
   allow the prototype to run either the warm bank or the cold bank as one
   40-question assessment. Do not merge warm and cold into an 80-question
   assessment or mark them approved. The current strict serving validator
   requires a separate research-mode path because the banks are
   `independently_math_checked; educator_approval_pending`.
   - Keep the bank's declared KT item regime in provenance. Cold-bank item
     IDs use the frozen model's unknown-item representation; that is the
     intended cold-item input, not a recovered historical item identity.
   - Both frozen banks use their original exact text embeddings. Only the
     four synthetic practice rows require the augmented embedding table.
   - KT probabilities remain research diagnostics and practice-draft inputs;
     they are not mastery, difficulty or learning-gain claims.
4. **Create matching research taxonomies.** Derive warm-bank and cold-bank
   taxonomies from the graph v3 assessed concepts so observed evidence and
   feedback labels match the selected bank. Keep them descriptive and pending
   formal educator review.
5. **Update the student and teacher UI.** Add/select the research bank,
   update Finnish/English skill and question display handling, distinguish
   demo/warm/cold sessions, and show graph-based task descriptions without
   exposing answer keys, internals, pending support links or possible-error
   interpretations to students.
6. **Connect v3 evidence to the feedback draft.** Use graph v3 task/concept
   descriptions and the 24-question practice pool for teacher-facing feedback
   and KT-assisted practice drafts. Keep provider wording constrained and do
   not diagnose mastery, misconceptions or causes of selected answers.
7. **Complete review and release boundaries.** Record formal educator review
   separately before any student-facing claim or release. Teacher review of a
   draft is not yet learner-delivery approval.
8. **Update tests and status docs.** Cover the 24-question source, warm/cold
   session separation, no active conformal surface, bank-specific taxonomy,
   privacy fields and the unchanged preserved pools/captures.
