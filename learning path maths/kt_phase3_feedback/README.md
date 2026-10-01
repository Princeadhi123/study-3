# Phase 3 — end-to-end MCQ feedback prototype

Phase 3 is the integration layer for the 40-question diagnostic test. It uses
Phase 2's approved private bank and deterministic scorer, then optionally adds
a cautious frozen variant-D KT estimate. It does **not** retrain or modify the
model, and it does not copy answer keys or model artifacts into this folder.

## Boundary

- Phase 1: source data and model training — closed.
- Phase 2: private inventory, text-only bank construction, approval, scoring,
  observed feed, and frozen-model loader.
- Phase 3: test sessions, ordered response capture, private event storage,
  KT input adaptation, feedback composition, and provenance.

Current bank:

```text
../kt_phase2_inference/artifacts/test_question_bank_text_only_approved_v2.json
```

That file contains private answer keys. Phase 3 references it; it must not be
copied into student payloads or committed to a public artifact store.

## End-to-end flow

```text
Approved immutable Phase 2 bank (fixed order, private answer keys)
        |
        |  student path — localhost only
        v
api.py -> MCQSessionService
        |   serve questions 1-20, accept ordered responses
        |     -> midpoint feed at response 20
        |   serve questions 21-40, accept ordered responses
        |     -> end feed at response 40
        v
Phase 2 observed scorer + bank-matched descriptive taxonomy
        |
        v
deterministic compose_student_message (bounded style selector; no LLM)
        |
        v
api.py projects student fields only:
  message, message_source, per-skill and per-subtopic counts;
  end also returns total {"correct": ..., "out_of": 40}
  (midpoint carries no aggregate grade)

private SessionStore -> ignored artifacts/sessions/:
  selected index/text, key-derived correctness, timestamps,
  bank sha256, checkpoint feeds

        |  private research branch — never student-facing, no LLM
        v
scenario_runner.py
  synthetic fixed-order baselines over the same approved bank
        |
        v
frozen KT trace -> historical k5/k10 conformal diagnostics
(not validated for this fixed bank)
-> bank-matched assessment taxonomy -> observed-error practice candidates
-> scenario_report.py / visualize_scenarios.py (ignored artifacts only)
```

The live student API never runs KT, conformal, assessment-graph, or LLM
code; those exist only in the private research branch and researcher routes.

## Files

| file | role |
|---|---|
| `phase3_paths.py` | Phase 2/artifact locations; named to avoid a `paths.py` collision |
| `schemas.py` | public question and submitted-response contracts |
| `session_store.py` | private JSON session records under ignored `artifacts/sessions/` |
| `mcq_service.py` | starts sessions, serves halves, accepts ordered responses, checkpoints at 20/40 |
| `kt_adapter.py` | validates text coverage and converts submissions to variant-D tensors |
| `feedback_service.py` | returns observed feed plus optional, separate `model_estimate` |
| `provenance.py` | records artifact identities/hashes for reproducibility |
| `api.py` | localhost-only JSON API for sessions and researcher diagnostics |
| `demo_cli.py` | scripted local end-to-end run |
| `scenario_runner.py` | fixed-bank synthetic scenario generator for researcher diagnostics |
| `scenario_report.py` | compiles scenario runs into a deterministic report (`phase3_scenario_report_v1`) |
| `visualize_scenarios.py` | optional PNG figures from a scenario report (requires matplotlib) |
| `tests/` | synthetic-bank unit tests; no private artifacts required |

## Public and private data

Student-facing payloads contain only:

```json
{"question_id": "...", "skill_id": "...", "text": "...", "options": ["..."]}
```

Submitted response rows contain only:

```json
{"question_id": "...", "selected_index": 0}
```

The private session record additionally stores `selected_text`, `correct`,
timestamps, and checkpoint feeds. `artifacts/` is ignored because those records
may contain private answer/scoring data.

The bank-matched `assessment_taxonomy_draft.json` maps each question to one
**descriptive** subtopic, not a prerequisite. At midpoint and end, student and
teacher feedback may include `subtopics` with `skill_id`, `skill_name`,
`subtopic_id`, `subtopic_name`, and observed `correct`/`out_of` counts. There
are no question IDs, answer keys, KT probabilities, conformal decisions, or
subtopic mastery claims in these rows. The default service requires this
bank-matched taxonomy; unrecognized synthetic banks remain taxonomy-free
unless a validated mapping is supplied explicitly. The feedback text and
bounded style-selector context still use only skill-level observed evidence.

Checkpoint feedback is split into `student` and `teacher` halves. The
`student` payload contains the deterministic `message`, `message_source`,
and observed per-skill and per-subtopic `correct`/`out_of` counts. The
midpoint checkpoint deliberately carries **no aggregate grade**; only the
end checkpoint adds `total: {"correct": ..., "out_of": 40}` and an
observed-results summary that makes no mastery claim. The `teacher`
half — and KT, conformal, assessment-graph practice candidates, and
answer-key data — stays private and is never returned on student routes.

## Session lifecycle

```python
from mcq_service import MCQSessionService

service = MCQSessionService.from_default()
start = service.start_session()
session_id = start["session_id"]

# Service can accept one response at a time:
service.submit_response(session_id, {
    "question_id": "...",
    "selected_index": 0,
})

# Or a trusted caller can submit a whole ordered 20-question half:
midpoint = service.submit_half(session_id, 1, first_half_rows)
end = service.submit_half(session_id, 2, second_half_rows)
```

Rules enforced by `MCQSessionService` and Phase 2's `mcq_test.py`:

- exactly 40 approved-bank questions;
- 20 responses before midpoint and 40 before end;
- ordered question IDs;
- strict integer selected indices;
- no per-answer correctness returned during the test;
- checkpoint feed only at 20 or 40 responses;
- session is bound to the bank hash, so a changed bank cannot score old answers.

## Local JSON API

From this directory:

```bash
python api.py --host 127.0.0.1 --port 8765
```

The server deliberately binds only to localhost. Student routes return public
question fields and the `student` portion of checkpoint feedback only:

```text
POST /sessions
GET  /sessions/{session_id}/questions?half=1
GET  /sessions/{session_id}/questions?half=2
POST /sessions/{session_id}/responses
POST /sessions/{session_id}/half-submissions
GET  /sessions/{session_id}/snapshot
```

Research routes are separate and require `X-Phase3-Role: researcher`:

```text
GET  /research/sessions/{session_id}/diagnostics
POST /research/sessions/{session_id}/kt-estimate   # body: {"device": "cpu"}
```

The header keeps the route contracts separate for the local prototype; it is
**not** production authentication. Do not expose this server beyond localhost
without a real authentication/authorization layer.

## KT adapter contract

`kt_adapter.trace_responses(bank, responses)` accepts the same ordered response
rows as `score_checkpoint`, then derives private event fields from the bank:

- `skill_id` — must be in the frozen skill vocabulary;
- `item_id` — unknown IDs map to `__UNK__`;
- `content_text` — must exist in the frozen embedding table;
- `selected_text` — derived from `options[selected_index]`; every selectable
  option must exist in the embedding table;
- `correct` — derived privately from `answer_index`;
- `response_time_ms` / `rt_mask` — currently `None`/`0` unless real timing is
  added;
- `attempt` — currently 1 for this single-attempt test;
- `time_bin` — currently 0 until real inter-answer timestamps are captured.

The adapter refuses missing content or option embeddings. The model's selected
text is part of the previous-step interaction only; it is never used as the
current-step query.

KT output is returned separately as:

```json
{
  "kind": "frozen_variant_d_response_trace",
  "model_status": "uncalibrated_for_20_40_question_feed",
  "p_correct_before_each_answer": [0.0]
}
```

It is not correctness evidence, not calibrated mastery, and not a
recommendation.

## Local scripted run

From this directory:

```bash
python demo_cli.py --profile alternating --out artifacts/demo_session_result.json
python demo_cli.py --profile all_correct --kt-device cpu \
  --out artifacts/demo_all_correct_kt_result.json \
  --manifest-out artifacts/provenance_manifest.json
```

Both commands refuse to overwrite outputs. Session JSON files remain private
under `artifacts/sessions/`.

## Fixed-bank scenario diagnostics (researcher only)

`scenario_runner.py` replays synthetic response trajectories over the **fixed
approved 40-question bank in fixed order**. It is a private diagnostic tool:

- response correctness is sampled from synthetic per-question probabilities —
  these are simulation inputs, **not** KT-derived estimates;
- no output is student-facing; scenario results, reports, and figures belong
  under ignored `artifacts/` paths only.

```bash
# one fixed-order run per spec/profile + seed
python scenario_runner.py --profile learning --seed 7 \
  --out artifacts/scenarios/learning_s7.json

# optional diagnostics: KT trace (--kt-device), conformal gate (--conformal,
# requires --kt-device), bank-matched assessment taxonomy (--graph)
python scenario_runner.py --profile learning --seed 7 --kt-device cpu \
  --conformal --graph --out artifacts/scenarios/learning_s7_kt.json
```

Custom seeded profiles can be supplied with `--spec path/to/spec.json`:

```json
{
  "name": "weak_fractions_seed_42",
  "seed": 42,
  "skill_probabilities": {"skill_304b0fda845f": 0.20},
  "default_probability": 0.75,
  "distractor_policy": "uniform_wrong"
}
```

Deterministic profiles are `all_correct`, `all_incorrect`, `alternating`,
`first_half_correct_second_half_wrong`, `weak_fractions_only`, and
`weak_price_only`. Seeded profiles are `stable_strong`, `stable_weak`,
`learning`, `fatigue`, `weak_fractions`, `guessing`, and custom specs. When an
outcome is incorrect, the runner samples uniformly among wrong options.
`--graph` attaches the **bank-matched assessment taxonomy** — the approved
topic/subtopic map (`relation="is_part_of_not_prerequisite"`), not a global
prerequisite or question-level graph. The runner accepts only a taxonomy
dict for `graph` and rejects global graph objects; when a separate taxonomy
is also supplied, both must describe the same assessment. Item-level
conformal decisions use each item's warm/cold regime; a per-skill checkpoint
uses Phase 2's cold-if-any-item-is-cold rule. The default research gate now
loads the corrected historical `k=10` quantile, and a separate historical
`k=5` calibration is used for the five-answer midpoint in the CLI. Neither corrected
historical block calibration validates this fixed synthetic instrument.
No scenario, report, or student API path makes an LLM call.

When the graph is supplied, the private result contains
`graph.checkpoints` (`midpoint` and `end`) built by
`assessment_feedback_graph`: per-skill observed counts, per-subtopic
correct/incorrect counts, and a `recommendations` map keyed by skill with
`recommendation_type` (`ASSESSMENT_SUBTOPIC_PRACTICE` when the skill has
assessed subtopics with observed incorrect answers, else `NONE`), the
subtopics carrying those errors, and a descriptive message. The graph
scope is `approved_bank_topics_subtopics_only` — there is no conformal
gating of practice candidates and no global prerequisite routing. The taxonomy
and conformal branches are independent; the flow diagram lists both diagnostics,
not a model-based prerequisite gate for assessment feedback. These
candidates describe observed errors inside this assessment only, are
pending educator review, and are private metadata: they are not merged
into observed feeds, student messages, style-selector context, or any API
payload, and they make no prerequisite or misconception inference.

Compile one or more scenario JSON files into a deterministic report (same
`bank_sha256` required; name + seed must be unique):

```bash
python scenario_report.py artifacts/scenarios/*.json \
  --out-dir artifacts/scenario_report
```

This writes `scenario_report.json` (schema `phase3_scenario_report_v1`),
`scenario_summary.csv`, and `per_skill_trace.csv`, refusing to overwrite.

Optional plotting (requires `matplotlib`, used with the non-interactive `Agg`
backend; it is an optional dependency and is not needed by the pipeline):

```bash
python visualize_scenarios.py artifacts/scenario_report/scenario_report.json \
  --out-dir artifacts/figures
# alternatively, include per-skill median/IQR figures when there are multiple seeds
# (choose a fresh directory because existing PNGs are never overwritten):
python visualize_scenarios.py artifacts/scenario_report/scenario_report.json \
  --out-dir artifacts/figures_aggregate --aggregate
```

One figure per scenario + seed + skill shows the simulated response
probability, the KT pre-answer probability when present, and the observed
running per-skill accuracy, with correct/incorrect markers and a midpoint line
at global position 20. Midpoint (approx. k=5) and end (k=10) conformal
statuses are annotated when present. The simulated P and the KT pre-answer P
are distinct estimands; the figures make no calibration claim. Filenames are
sanitized, collisions are detected, and existing images are never overwritten.

Paired order/history/ID sensitivity experiments (the former
`research_matrix.py` runner and the `--research-matrix` report/figure
options) have been **removed** — code and saved outputs alike — to keep
scope on the fixed-40 flow only.

## 2026-09-29 full-bank validation package

All paths below live under ignored `artifacts/`: they are private local
outputs that may contain key-derived scores, and they must not be committed
publicly or stored alongside embeddings or answer keys.

- `artifacts/fixed40_checkpoint_feedback_20260929_v3.json` — **current**
  service replay of the ten saved sessions: midpoint/end student feedback
  (including the end-only `total`) plus overall observed scores.
- `artifacts/report_fixed40_baselines_20260929/` — baseline report compiled
  from the ten saved sessions without rerunning KT: `scenario_report.json`,
  `scenario_summary.csv`, `per_skill_trace.csv`, `subtopic_summary.csv`.
- `artifacts/figures_fixed40_baselines_20260929/` — 48 PNGs: 40 per-seed
  skill traces plus 8 aggregate figures.

Regenerate the baseline figures from the retained report (the existing path
refuses overwrite — use a fresh output name). The v3 feedback file is not
regenerated by this command: it was independently replayed through
`MCQSessionService` from the saved answer rows.

```bash
python visualize_scenarios.py artifacts/report_fixed40_baselines_20260929/scenario_report.json \
  --out-dir artifacts/figures_fixed40_baselines_20260929 --aggregate
```

Grounded results — ten synthetic fixed-order sessions, five seeds per
profile, totals across each profile's five sessions:

| profile | first half | second half | final |
|---|---:|---:|---:|
| learning | 40/100 | 66/100 | 106/200 |
| fatigue | 69/100 | 49/100 | 118/200 |

All 64 Phase 3 tests pass, including the optional real-bank contract test
covering boundary profiles (all correct, all incorrect, first-half-only,
second-half-only) over the approved 40-question bank. Replaying the ten
saved answer sequences through the current session service re-validated the
bank fingerprint, fixed question order, key-derived scoring, student-payload
privacy, and the end-only `total`.

Example — fatigue seed 11 scored 13/20 at midpoint and 25/40 at end. Its
midpoint message was `You have completed 20 questions. You are halfway
through the assessment. Continue when you are ready.` and its end message
was `You have completed all 40 questions. On Hinta, you answered 8 of 10
questions correctly. For your next step, practice more questions on
Peruslaskutoimitukset (5 of 10 correct).`

Limits: these are synthetic trajectories, not a student pilot; the ten runs
are seeded variations of two profiles, neither distinct exams nor a student
cohort. KT calibration and conformal coverage on this fixed bank remain
unvalidated, and graph routing candidates are exploratory pending domain
review.

## Current validation status

The fixed-bank validation stage is in place and passing all 75 Phase 3 tests.
Completed checks so far:

- deterministic boundary profiles score as expected on the approved bank;
- every scenario is a fresh cold-start synthetic session with no prior
  student history (`attempt=1`, `time_bin=0`, and `rt_mask=0`);
- seeded learning, fatigue, stable, guessing, and weak-skill profiles are
  reproducible and record simulated probabilities plus sampled responses;
- incorrect outcomes uniformly select a wrong option;
- observed scoring, KT tracing, conformal diagnostics, and assessment-graph
  candidates remain contractually separate;
- mixed warm/cold skill checkpoints use Phase 2's cold-if-any-item-is-cold
  rule while retaining per-item regimes;
- reports and plots are deterministic and refuse to overwrite outputs;
- the bank-matched assessment taxonomy produces observed-error practice
  candidates only — no global routing and no conformal gating;
- (archived) a real Phase 2 graph query once returned the expected global
  skill-level candidates and no dangling or duplicate graph edges were
  found — retained history, not current scope;
- (archived) private `graph.routing` records once derived from end `k=10`
  checkpoint statuses and grounded prerequisite edges — retained history,
  not current scope;
- no live student pilot has been run and no LLM calls have been made.

The five-seed validation ran `learning` and `fatigue` on seeds
11, 23, 37, 41, and 53 with KT, conformal, and graph diagnostics. The
generated responses behaved as intended:

| profile | simulated first half | actual first half | simulated second half | actual second half |
|---|---:|---:|---:|---:|
| learning | 47.8% | 40% | 67.2% | 66% |
| fatigue | 68.3% | 69% | 51.7% | 49% |

The corresponding KT mean moved in the expected direction, but much less
strongly than the synthetic response parameter:

| profile | mean KT first half | mean KT second half |
|---|---:|---:|
| learning | 52.6% | 57.2% |
| fatigue | 66.4% | 63.5% |

This is an encouraging direction, not a calibration result. All of these runs
answer a cold-start question: how the fixed test, scorer, KT, conformal gate,
and graph respond to a fresh sequence with a rising or falling response
pattern. They do not model warm-start students, prior history, real learning,
or real fatigue. KT remains question/content-sensitive: the sharp per-item
changes reflect the specific next question and response history, not merely
the declared synthetic ability schedule. The end conformal rows are
exploratory because fixed-bank coverage has not been independently validated;
midpoint rows remain an approximate `k=5` diagnostic.

Archived (no longer run — global prerequisite routing is out of scope): the
former graph diagnostic worked as an exploratory global-skill lookup; for the
current bank only price had grounded prerequisite candidates, and basic
arithmetic, fractions, and percentages returned none. Empty meant "no
grounded edge above the evidence threshold", not that no pedagogical
prerequisite exists.

## 2026-10-01 assessment-pipeline report

The current regenerated report is `artifacts/assessment_pipeline_20261001/`
— 54 scenario rows with checkpoint/subtopic CSVs, `comparison.png`, and
`feedback_report.html` using the bank-matched assessment taxonomy for
practice candidates and the saved conformal diagnostics separately. It was
produced by replay/rescope of the saved fixed-40 evaluation: frozen
responses, KT traces, and conformal predictions are preserved as saved, and
no external calls are made. The earlier `full_pipeline_20261001` outputs
used the retired global-routing scope and are kept only as archived history.

```bash
python evaluate_scenarios.py --replay artifacts/full_pipeline_20261001.json \
  --assessment-only --out artifacts/assessment_pipeline_20261001.json
python compare_scenarios.py artifacts/assessment_pipeline_20261001.json \
  --full-pipeline --out-dir artifacts/assessment_pipeline_20261001
```

Both commands refuse to overwrite existing outputs.

## Reading the scenario figures

For an individual scenario figure:

- blue line: the probability used to generate the synthetic response;
- orange line: KT's pre-answer probability when KT diagnostics are enabled;
- gray line: observed running per-skill accuracy;
- green circle: generated answer was correct;
- red cross: generated answer was incorrect;
- black dashed line: global midpoint after question 20.

For an aggregate figure, the blue and orange lines are medians across seeds.
The blue band may be nearly invisible when all seeds share the same simulated
probability schedule. Always read aggregate plots alongside the summary CSV;
neither plot compares ground truth directly to validated mastery.

## Tests

```bash
python -m unittest discover -s tests -v
```

Most tests use a synthetic approved-shaped bank. They do not load the private
40-question artifact or the 2 GB embedding table. `test_real_bank_contract.py`
is an optional local integration test: when the approved v2 bank exists it
verifies the public payload and private scoring path, and it skips otherwise.

## Remaining work

1. Consolidate the validation package further: the paired sensitivity
   tooling was removed to keep scope on the fixed-40 flow (see the
   2026-09-29 package above); remaining consolidation is folding
   human-readable skill names, regime counts, conformal status counts, and
   graph artifact provenance into the report/manifest.
2. Evaluate the model against held-out historical sessions where their
   questions match the current data requirements. This is stronger than
   synthetic-only evidence but still does not validate the fixed instrument if
   the historical sessions differ from its order/content.
3. Ask a domain expert to review graph candidates — and any private
   `graph.routing` recommendation built on them — as acceptable,
   questionable, or rejected. Empty graph output means no grounded edge passed
   the evidence threshold; it is not a claim that no prerequisite exists.
4. If a future pilot becomes available, capture timestamps/response durations
   and cold-start versus prior-history state. Until real fixed-test sessions
   are evaluated, keep midpoint `k=5` approximate and end `k=10` exploratory.
5. Keep observed feedback, KT, conformal, graph, and private routing outputs
   separate. A future LLM composer should only use verified observed counts
   and explicitly reviewed diagnostic context; it must not receive private
   answer keys, raw synthetic ability, unvalidated conformal status,
   unreviewed graph candidates, or private routing records as authoritative
   facts.
6. Student deployment still needs authentication/authorization, production
   data handling, a frontend or framework route layer, and a decision about
   which diagnostics are safe to expose.
