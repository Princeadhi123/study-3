# Phase 2 — inference, conformal gating, and the skill knowledge graph

## Current product goal and scope

**Product goal:** an intelligent informational feedback feed shown at the
**midpoint** and **end** of a test, for **students and teachers only**.

The feed describes observed performance and model uncertainty. Where the
evidence supports it, the feed may **automatically select and generate
non-binding "possible skill to review" recommendations**, always in
cautious wording with stated uncertainty — an unstable top-1 suggestion is
never presented as certain. The student or teacher chooses the next step:
the feed never **redirects** anyone to study a skill and never **enforces**
a study action or path.

Everything in this directory is the statistical machinery that could power
such a feed (frozen KT model, conformal gate, knowledge graph). **The live
feed is not implemented** — `test_feed.py` is a pure observed-answer
prototype of it. Automatic redirection, enforced study
actions, and autonomous teaching/tutoring dialogue are possible future
directions and are explicitly out of current scope (see "Out of scope").

Phase 1 is **closed**. Its tuning phase reached a multi-seed-confirmed stop
(four rounds; see `../kt_phase1/modeling/README.md`, "Rounds 2-4"), and the
deployed model is a single frozen checkpoint. Nothing in this directory
trains, fine-tunes, or writes back into `kt_phase1/`.

The deployed model is **variant D** (`skill_item_content_option`) at
`best_model_joint.pt` — epoch 45, seed 42, `d_model=192`, `n_layers=3`,
5,850,369 parameters. That checkpoint rather than `best_model.pt` because
the joint-selected epoch is the documented deployment recommendation: a
live test-time deployment mostly faces questions the model never trained
on, which is the `test_cold_item` regime.

Reference generalisation numbers, quoted as the **mean across seeds 42/43/44**
(not the best seed — see Phase 1's "Reporting the cold-AUC number honestly"):

| | val AUC | test_warm AUC | test_cold_item AUC |
|---|---|---|---|
| `skill_item_content` (C) | .934 | .922 | **.910** |
| `skill_item_content_option` (D) | .934 | .922 | **.911** |

D ≥ C at all three seeds, but the seed-to-seed spread (~.0025) exceeds the
C→D gap (~.0013). "D beats C" is a small, consistently-directional effect,
not an emphatic one.

---

## Prototype status and next steps

**Ready now**

- `artifacts/test_question_bank_text_only_approved_v2.json` is the current
  40-question bank: four skills, ten questions per skill, five per skill in
  each half, locally approved for the offline prototype, and compatible with
  the frozen variant-D text embeddings for every question and every
  selectable option. It contains private answer keys and must never be sent
  to a student.
- `mcq_test.py` provides the callable serving/scoring boundary:
  `student_questions()` returns key-free halves and `score_checkpoint()`
  privately scores ordered 20- or 40-response prefixes.
- `test_feed.py` renders the cautious observed-answer midpoint/end feed.
- `simulate_test_feed.py --bank ... --kt-device cpu` can run an illustrative
  variant-D trace over the approved v2 bank.

**Needed for an end-to-end prototype**

1. **Session storage and API boundary.** Create a test session, serve half 1
   then half 2, persist ordered `question_id`, `skill_id`, `selected_index`,
   timestamps, and the private `correct` result. Students receive only the
   public fields from `mcq_test.student_questions()`.
2. **KT event adapter.** Convert each submitted response into variant-D event
   fields: current `skill_id`, `item_id`, `content_text`, correctness, prior
   `selected_text`, response time/`rt_mask`, attempt number, and `time_bin`.
   Never place the current selection in the current query; it belongs only to
   the next step's history.
3. **Missing-data policy.** If response time is unavailable, keep
   `rt_mask=0` rather than inventing a value. Decide explicitly whether each
   test is cold-start or continues from prior student history.
4. **KT compatibility gate.** Before KT inference, require all skill IDs in
   the vocabulary, every `content_text` and option `selected_text` in the
   embedding table, and unknown `item_id`s routed only to `__UNK__`.
5. **Feedback assembly.** Keep observed counts primary and attach KT output
   as a separate `model_estimate` with an uncalibrated-status label. Do not
   blend it into correctness or present it as mastery.
6. **Role-separated UI.** Show students only their own public questions and
   cautious observed/model-status text; show item/event detail only to an
   authorized teacher or researcher view.
7. **Evaluation.** First replay synthetic all-correct/all-wrong/mixed
   trajectories, then a small human pilot. Compare observed-only feedback
   with observed + KT estimate, and inspect per-skill predictions rather
   than relying on global AUC.

**Claims not yet supported**

- No calibrated mastery claim from five questions per half or ten per skill.
- The reported variant-D cold-item result is mean AUC about `.911`, not proof
  of calibrated feedback on this test.
- No recommendation, redirection, or enforced study path is validated.
- No causal learning effect can be claimed without a separate study.

**Immediate implementation order**

1. Add a small session/response service around `mcq_test.py`.
2. Add a KT adapter that consumes the saved response rows and emits
   per-step `P(next correct)` plus coverage diagnostics.
3. Render observed feedback at 20 and observed + clearly labeled model
   estimate at 40.
4. Only after that works, expand the approved text-only MCQ bank or add more
   skills/questions per skill.

---

## Architecture

Layer and script naming are the same thing: each script implements one
layer (or sub-step) of the architecture, named accordingly — L0 is data
preparation beneath the layers, L5 is the Socratic LLM, which is
*out of scope* here (see "Out of scope").

```
L0  skill catalog (data substrate)      build_skill_catalog.py
L1  frozen variant-D KT model           kt_phase1/  (closed)
L2  per-item P(correct)                 dump_predictions.py
L3  conformal gate -> InterventionStatus
      L3a  offline calibration          conformal_calibrate.py
      L3b  live gate                    conformal_gate.py
L4  knowledge graph -> related-skill suggestion
      L4a  counterfactual probing       probe_predictive_dependency.py
      L4b  graph construction           build_knowledge_graph.py
      L4c  graph query                  kg_query.py
L5  Socratic LLM                        FUTURE -- not in this codebase
```

The gate flags evidence for feedback; it never acts on the student. The
L4 graph/query layer is the experimental mechanism that, when the gate
flags a skill, proposes a candidate related skill to look at — once a
feed is implemented, that could be automatically selected into the feed
as a non-binding "possible skill to review" hint, never as a redirect or
enforced path. They meet at
`ConformalGate.checkpoint(..., remediation_lookup=kg.remediation_lookup())`,
which returns a self-contained `CheckpointTriggerResult`. That result was
designed as the hand-off contract for a Socratic LLM (Layer 5); under the
current feed objective it is simply the structured record a feed renderer
or teacher view would read.

**Nothing here calls an LLM or any agent framework.** Layers 1–4 are
entirely deterministic/statistical — the LLM is Layer 5, downstream of this
pipeline, and is deliberately kept out of the gating decision. See
"Out of scope" below.

### Files

| file | role |
|---|---|
| `paths.py` | every frozen input/output location; `require()` fails loudly on missing inputs; LUMI paths via `KT_PHASE2_*` env overrides |
| `build_skill_catalog.py` | **L0** — the complete 560-skill catalog + per-student first-encounter times |
| `frozen_model.py` | **L1 loader** — strict loader; refuses to run on the wrong embeddings or stale sequences |
| `dump_predictions.py` | **L2** — one forward pass; caches every held-out prediction; groups windows by base student ID |
| `conformal_calibrate.py` | **L3a** — offline calibration + empirical coverage report; by-student split |
| `conformal_gate.py` | **L3b** — the live gate: `InterventionStatus`, `CheckpointTriggerResult` |
| `probe_predictive_dependency.py` | **L4a** — counterfactual probing of the frozen model |
| `build_knowledge_graph.py` | **L4b** — evidence layers + derived `prerequisite` edges (requires measured reverse direction) |
| `kg_query.py` | **L4c** — "the gate flagged B; which skills plausibly underpin it?" |
| `evaluate_checkpoints.py` | **eval** — replays held-out 10-item checkpoints through the gate + graph; writes `checkpoint_replay.jsonl` and `checkpoint_decision_report.json` |
| `compare_graph_stability.py` | **eval** — builds a graph per probe output and reports edge Jaccard, edge retention, and top-recommendation agreement |
| `run_lumi_phase2.sh` | SLURM array: task 0 = L2 predictions, task 1 = L4a probe |
| `run_lumi_dependency_stability.sh` | SLURM array: three independent 20,000-window probes at different seeds, for graph stability |
| `test_feed.py` | **feed prototype** — pure observed-answer midpoint/end-of-test feedback dict; no model, no calibration |
| `question_bank.py` | **offline demo bank** — select distinct scored MCQ templates from the item-level source; answer key stays private |
| `mcq_inventory.py` | **private inventory** — extract all distinct scoring-eligible MCQ renderings from every skill; collapse student events and flag contradictory keys |
| `review_mcq_inventory.py` | **review triage** — audit all inventory rows for exported-text risk indicators; never approves any question |
| `simulate_test_feed.py` | **offline demo** — scripted 20/40 answer trajectory and optional illustrative frozen-KT trace |
| `mcq_test.py` | **callable MCQ path** — key-free `student_questions` + private `score_checkpoint` over an approved bank; no LLM, no model claims |
| `select_text_mcq.py` | **text-only candidate survey** — lists conservative standalone MCQ candidates by skill without approving them |
| `build_text_only_bank.py` | **checked 40-bank builder** — rebuilds the private selection and verifies answer values, prompt uniqueness, and review flags |
| `approve_text_only_bank.py` | **controlled approval** — writes a separate approved bank only if the source exactly matches the checked selection |

`build_test_feed(answers, skill_names)` is a pure function over the
completed-answer prefix of the fixed 40-question test (4 skills × 5
distinct questions per half, new questions on the same skills in the
second half). Pass it 20 rows for midpoint or 40 for end; it returns a
JSON-serializable dict with `student` and `teacher` views, an `evidence`
record, and an empty `suggestions` list. Verify with
`python -m unittest -v test_test_feed.py` from this directory — stdlib
only. This is an **observed-answer prototype, not the live feed** — no
validated model-based midpoint or end-of-test feed exists yet, and the
conformal gate is not applied to these five-answer skill blocks.

What is still missing for the real feed:

- **Identified test data.** `predictions_d.npz` and
  `checkpoint_replay.jsonl` are student *windows*, not identified
  40-question tests — there is no test/session ID to reconstruct one
  from. Deployment needs an authorized source of ordered per-test
  `question_id` / `skill_id` / `correct` events plus the skill names.
- **Production-level content approval.** The v2 text-only bank is locally
  approved for the offline prototype and is KT-compatible, but that approval
  is not external curriculum certification for real students.
- **Test-app integration** that delivers the student and teacher views
  role-safely, at the midpoint and end moments.
- **Held-out real tests** to evaluate — and, if model output is ever
  wired in, to calibrate — the five-answer midpoint and ten-answer
  full-test conditions separately. The windowed checkpoint numbers
  below do not apply to either feed condition.

Optional graph-based suggestions stay gated on the stability review and
expert sign-off in "What remains" items 3–4.

### Offline 40-question demonstration

From this directory, with the item-level source and skill catalog available:

```bash
python question_bank.py --out artifacts/test_question_bank.json
python simulate_test_feed.py --bank artifacts/test_question_bank.json --out artifacts/test_feed_demo.json
python -m unittest -v test_mcq_test.py test_question_bank.py test_simulate_test_feed.py test_test_feed.py
```

The bank builder streams the item-level source, admits only labeled MCQs
with one unambiguous answer key, and takes one rendering per exercise
template. It chooses four skills by eligible-template count for this **demo
only**, or accepts four educator-selected IDs via `--skills S1 S2 S3 S4`.
For each skill it uses ten distinct templates, alternating the sorted
renderings between halves and interleaving the skills. Generated files go
under ignored `artifacts/`; both commands refuse to overwrite an existing
output. The bank contains an **answer key**: never serve that JSON to a
student. The feed demo contains only scripted answer counts, not real
student results or an evaluation of learning.

The first local run found 20/19/19/19 eligible templates for its four
auto-selected skills and produced the 20/40 feed. **These questions have
not been reviewed for classroom use.** One auto-selected skill is named
`KLIKKAA TÄSTÄ` ("click here"), and a question under `Todennäköisyys`
(probability) asks about the multiplication table. Some prompts may also
require images absent from the text export. Counts and distinct IDs alone
do not establish a usable test; have an educator choose the skills and
review the rendered items/answer keys.

`simulate_test_feed.py --responses choices.json` accepts a complete mapping
of `question_id` to selected option index instead of scripted choices.
`--kt-device cuda` (or `cpu`) additionally loads the frozen variant-D
model and produces an **illustrative cold-start trace** of P(correct) before
each scripted answer; it rejects unknown content/selected-answer embeddings
instead of silently using row 0. This optional heavyweight path has a
mocked-input unit test and a completed scripted run against the approved v2
bank (`artifacts/test_feed_text_only_approved_v2_kt_demo.json`). It assumes
no prior student history, no response-time measurements, and synthetic
answers: these probabilities must not be treated as validated
midpoint/end-of-test feedback or evidence that feedback works.

#### Complete private MCQ inventory (pre-review)

Run `python mcq_inventory.py` from this directory to scan the item-level source
and write ignored `artifacts/mcq_inventory_private.jsonl.gz` plus
`artifacts/mcq_inventory_report.json`. Each JSONL row is a distinct rendering
with its option key and skill label; it contains no student events or raw
answers. The report counts source MCQ events, eligible events, and exclusions.
All skills are included, with `in_model_catalog` marking model-compatible
skill IDs. Run `python mcq_inventory.py --verify` to check the written records
and aggregate counts without rescanning the source. The inventory keeps more
than one rendering per exercise template;
`question_bank.py` still deliberately selects only one per template for a
40-question demo. Ambiguous keys for the same rendered question are excluded.
The inventory and answer keys are **private and unreviewed**: do not serve
this file or feed it into KT. Review question context, images, answers and
skill labels first. Existing artifact outputs are never overwritten.

`python review_mcq_inventory.py` audits all inventory entries and writes private
`artifacts/mcq_review_flags_private.jsonl.gz` and aggregate
`artifacts/mcq_review_report.json`; `python review_mcq_inventory.py --verify`
checks the report, per-question flags and the manually inspected 40-question
notes in `artifacts/mcq_40_review.json`. Flags (short/underspecified prompts,
possible missing visuals or applets, markup rendering, two-choice guessing,
non-skill labels and conflicting visible-content keys) are **review leads,
not correctness judgments**. The export omits original media and rendering;
zero questions are certified or approved by these checks.

#### Text-only 40-question replacement (private)

The original `artifacts/test_question_bank.json` is retained as an unreviewed
example with missing diagrams and unsuitable skill labels. The current checked
source is `artifacts/test_question_bank_text_only_v2.json`: 40 distinct
*visible prompts*, 10 each on basic arithmetic, price, fractions and
percentage calculation. Five from each skill appear in each half. All 40
selected answers were independently checked against the provided options and
recorded in `artifacts/text_only_40_review_v2.json`; the explicit ID/answer
selection is kept private in `artifacts/text_only_mcq_selection_private.json`.
Some source exercise templates repeat but their rendered prompts differ, and
the bank validator requires all 40 normalized prompts to be distinct. The
candidate survey is `select_text_mcq.py`; `python build_text_only_bank.py
--verify --out artifacts/test_question_bank_text_only_v2.json --report
artifacts/text_only_40_review_v2.json` checks the bank, source records, flags,
and manually checked answer values.

`python approve_text_only_bank.py --source
artifacts/test_question_bank_text_only_v2.json --out
artifacts/test_question_bank_text_only_approved_v2.json` creates the locally
approved copy without mutating the checked source. That approval records local
project-owner acceptance — not external curriculum certification — and the
bank remains private. Use this approved v2 bank for KT-compatible prototype
work: every question content string and every selectable option resolves in
the frozen embedding table. The v1 bank and approved copy are retained only
as audit artifacts and should not be used for live KT input.

#### Bank historical availability audit

`audit_bank_coverage.py` scans the frozen prepared windows and counts how
often the approved bank's source items and exact renderings already appear as
stored labelled targets:

```bash
python audit_bank_coverage.py --out-dir artifacts/bank_coverage_audit
```

It writes `coverage.json` (full report: per-item and per-question counts by
split, heldout/test groups, history summaries, scope notes and input
provenance hashes) and `coverage.csv` (per-question, per-split summary rows).
The output directory must not already exist — the script refuses to overwrite
it. No model is loaded and no training or inference runs; it is a read-only
scan of the bank, vocabulary, sequences and split report.

Counts are reported at two levels: **item level** (source item matched with
the bank's skill label) and **exact rendering** (source item + skill +
byte-exact rendered content including ordered options). Events where the same
source item carries a *different* skill label than the bank assigns are not
treated as bank-skill coverage: they are excluded from the item/exact totals
and counted separately per item and split, while still remaining available in
preceding history. `context`-split events are never counted as heldout/test
targets. All outputs are aggregate-only — no student identifiers are written.

History columns count preceding events *within the retained window* only;
position zero does not mean a new student. `val` informed checkpoint
selection, `test` is not necessarily untouched, and conformal calibration
students must be excluded from evaluation of conformal prediction coverage.
This is an availability audit, **not** a performance evaluation — no accuracy, coverage
guarantee, or fixed-assessment validity is measured.

#### Evaluation candidate inventory

`inventory_evaluation_candidates.py` inventories how much scored MCQ material
exists for the four bank skills inside the frozen prepared windows:

```bash
python inventory_evaluation_candidates.py --out-dir artifacts/evaluation_candidate_inventory
```

The output directory must not already exist. Before counting, the script
reproduces the **active k10 conformal calibration partition** exactly from the
prediction dump's student metadata — first-seen base student IDs and active
label counts must match or the run fails. Calibration students are then
excluded from candidate counts. Only `test_warm` and `test_cold_item`
stored targets are counted, using
each event's *actual* skill label rather than the bank's label. Because the
script reads only the bank's four skill names (ignoring its item IDs and answer keys), the
scan covers **all** items on those skills, not just catalogued renderings.

Outputs, all aggregate-only and ignored by Git under `artifacts/`:

- `warm_candidates.csv` / `cold_candidates.csv` — ranked distinct MCQ
  renderings (warm = in item vocabulary, cold = unknown item index), ordered
  by stored-target count, then distinct students, then stable IDs; history
  columns are window-local preceding events.
- `item_coverage.csv` — per-item, per-skill, per-regime availability across the four
  skills.
- `availability.json` — full report with provenance, partition confirmation,
  and scope notes.

Candidate rows include prompts/options, class-support counts and review flags —
**no answer keys and no student identifiers**. This is a private research inventory: it
produces *candidates*, not an approved bank, makes no claim about model
quality, conformal coverage, or embedding validity, and the counted windows
are **not** student-training-disjoint.

Passing `--all-skills` widens the same scan from the four bank skills to every
skill in the model catalogue, writing to a fresh `--out-dir` (the default
four-skill behaviour is unchanged):

```bash
python inventory_evaluation_candidates.py --all-skills --out-dir artifacts/evaluation_candidate_inventory_all_skills
```

In this mode the script first validates that the skill catalogue is complete
and vocabulary-aligned — every model-vocabulary skill present, no extras — and
reconciles its all-skill target counts against the active k10 checkpoint
counts (counts, not rates); any mismatch fails the run. Skill summaries include
`skill_ranking.csv`: an **availability-only** ordering of skills, descending
by min(warm, cold) rendering counts within each pool, requiring more than one
student and no existing `BLOCKING_FLAGS`, then min catalogue students across regimes,
then total repeated-unblocked renderings, then total catalogue answers, then
skill ID. Blocking flags are review leads, not approval, and no model output,
correctness, or outcome signal participates in the ranking. The report's
`checkpoint_support` per window/skill distinguishes `warm_only`, `cold_only`
and `mixed` non-overlapping blocks of the active checkpoint size, preserving
the existing k-grouping within each window — windows are never regrouped.
`fully_catalogued` requires every member of an already formed block to match
the MCQ catalogue; a separate count reports blocks with k distinct renderings.
Neither count guarantees that a fixed assessment could be assembled.
`prompt_support.csv` additionally collapses option variants by normalized
visible prompt, counting only variants individually supported by multiple
students and without blocking flags. Singleton variants are not pooled to
invent repeated-question support. No bank files are modified.

#### Global support-optimal warm/cold research banks (2026-10-05)

The current final research selection is
`artifacts/evaluation_banks_support_optimal_20261005/`, research version
`3_support_optimal_under_declared_rules_20261005`:

- [Warm bank](artifacts/evaluation_banks_support_optimal_20261005/warm_bank_private.json)
- [Cold bank](artifacts/evaluation_banks_support_optimal_20261005/cold_bank_private.json)
- [Selection and optimality report](artifacts/evaluation_banks_support_optimal_20261005/review.md)
- [Optimality certificate and bank hashes](artifacts/evaluation_banks_support_optimal_20261005/optimality_certificate.json)
- [Independent output checks](artifacts/evaluation_banks_support_optimal_20261005/independent_optimality_checks.json)

**Precise claim:** globally support-optimal warm/cold MCQ research banks
within the eligible frozen dataset, the shared four-source-topic design,
and the documented content/format rules. This is not a universal
educational-quality optimum, a limitation-free assessment, or KT/conformal
validation.

Each bank has 40 distinct question IDs and normalized visible prompts,
ten per topic and five per topic in each half. Topics are percentage
calculations, divisibility/prime identification, combining like terms,
and fraction multiplication/quantities. Source skill IDs are preserved.

Eligibility requires at least three distinct substantive options, at least
two eligible evaluation students per exact rendering, stand-alone
source-topic-aligned content, independently checked answers, exact frozen
embedding membership and the correct warm/cold item-ID regime. Missing
media, malformed worked chains, supplied target answers, off-topic tasks
and ambiguous mixed-number exports are not silently repaired. Cold item
IDs do not establish content novelty or equal difficulty across regimes.

The objective first maximizes the minimum per-rendering student count
across both banks, then the sum of per-rendering student counts, with
deterministic tie-breaking. The optimum is **2** for the minimum and
**917** for the sum. The sum is not 917 unique students or an independent
sample size; repeated attempts and overlapping students remain.

All 560 model source skills were capacity-checked. Availability upper
bounds prune 554 skills; all 3,570 relevant exact renderings in the six
remaining skills have closed review decisions, with no unresolved cases.
The compatible pool has 2,782 renderings. Five topics remain feasible,
so every one of the five four-topic combinations was examined. Two
combinations are feasible, with maximum support sums 910 and 917; the
other three fail prompt-uniqueness capacity. The independent checks verify
these bounds, the selected banks, mathematical answers and hashes.

Content/selection checks are complete under these rules; historical KT
inference, its evaluation protocol, matching conformal evaluation and
learner-delivery approval are not. Genuine selected-bank ten-question or
sequential 40-question sessions have not been established. Keep these
banks private and research-only; the approved demo bank and serving
defaults are unchanged.

The retained `global_bank_closed_review_20261005/` directory contains the
full relevant decision ledger and all-source capacity bounds. The frozen
all-skill availability capture, MCQ catalogue, vocabulary, embedding table
and `stronger_bank_audit_20261005/manifest.json` remain necessary inputs.
The [cleanup receipt](artifacts/evaluation_banks_support_optimal_20261005/cleanup_receipt_20261005.json)
records removal of superseded bank/audit folders and preparation outputs;
removed historical bank reports are not current on-disk evidence.

Next: bind the retained selection/support evidence into the evaluation
protocol, resolve eligible target events and genuine score-block support,
then run historical KT evaluation. Define conformal's target separately
before writing its evaluation code. No fresh selection or inference was
performed for this documentation update.

#### Callable MCQ test path (backend API surface)

`mcq_test.py` is the deterministic, callable counterpart to the scripted
demo — the functions a trusted backend would invoke once a bank has been
reviewed offline:

- `student_questions(bank, half)` returns the 20 ordered questions of half
  1 or 2 as key-free dicts (`question_id`, `skill_id`, `text`, `options`
  only; options are copies). `half` must be the integer 1 or 2.
- `score_checkpoint(bank, responses)` accepts exactly the ordered prefix
  of 20 (midpoint) or 40 (end) responses, each exactly
  `{'question_id': ..., 'selected_index': ...}` matching the bank's
  question order, grades privately against `answer_index`, and returns
  only the `build_test_feed` output — no key, no selected text.

The bank must contain 40 **distinct visible prompts** (normalized text):
a repeated source exercise template is allowed only when its rendered
questions genuinely differ — identical text with different options does
not count as a new question. Both functions refuse any bank whose
`protocol` is not `offline_mcq_demo_only` or whose `review_status` is not
exactly `"approved"` — `question_bank.py` writes `unreviewed`, so the
assembled bank must be approved offline before this path will serve it. A
newly selected 40-question bank still needs that approval even when every
item renders correctly. Approval is a manual, offline review step: check
every rendered item and answer key, then create an approved copy (for this
text-only bank, `approve_text_only_bank.py` performs that controlled copy).

There is **no LLM, no numeric/free-text grading, and no model-based
claim** in this path; scoring is answer-key matching plus the
observed-answer feed. `score_checkpoint` returns both `student` and
`teacher` views to the trusted caller — **the integrating application
must role-separate them** and must never expose the bank JSON. No web-app
integration exists yet, and this is not production-ready: it presumes an
authenticated backend that owns sessions, ordering, and review state.

---

## Run order

L0, L3a, L4b and L4c run on a laptop. L2 and L4a need the frozen model in
memory — the embedding table alone is 2.2 GB and the parsed sequences are
several GB more, so **run them inside a LUMI allocation** and copy the
(small) outputs back.

```bash
# L0 -- ~8 min, streams the 13.9M-row interactions file
python build_skill_catalog.py

# L2 + L4a on LUMI -- sbatch --account=project_462001308 run_lumi_phase2.sh
# (array task 0 dumps predictions, task 1 runs the dependency probe)

# L3a -- seconds, from the cached predictions
python conformal_calibrate.py --alpha 0.10 --alpha-sweep

# L4b -- ~1 min
python build_knowledge_graph.py --dependency artifacts/predictive_dependency.csv

# L4c -- query it
python kg_query.py --skill "Kertaus: Yhtälöt"

# eval -- replay held-out checkpoints through gate + graph
python evaluate_checkpoints.py

# stability -- after running run_lumi_dependency_stability.sh on LUMI and
# copying artifacts/stability/*.csv back:
python compare_graph_stability.py \
  --dependency artifacts/stability/predictive_dependency_seed_20260919.csv \
             artifacts/stability/predictive_dependency_seed_20260920.csv \
             artifacts/stability/predictive_dependency_seed_20260921.csv
```

### Required inputs not in this repo

Two large files must be rsynced back from LUMI. Both are **verified, not
assumed** — the loader hard-fails rather than degrading:

```bash
rsync -av lumi:/projappl/project_462001308/math_kt/embeddings/text_embeddings_v2.npz \
    kt_phase1/modeling/prepared_v2/        # or kt_phase2_inference/artifacts/, both are searched

rsync -av lumi:/scratch/project_462001308/math_kt/prepared_v2_w400_o0p25_g30_c8/\
{sequences.jsonl.gz,vocab.json,split_report.json} \
    kt_phase1/modeling/prepared_v2/
```

The sequences file matters more than it looks. `dataset.py` treats
`content_text`, `selected_text` and `time_bin` as *optional*, falling back to
plain `text`, index 0 and bin 0 — correct for v1 sequences, catastrophic for
variant D. A pre-Sep-15 `sequences.jsonl.gz` lacks all three, so D would run
with its entire distinguishing input (the previous selected answer) replaced
by one constant vector, and emit confident, well-formed, meaningless
probabilities with no error anywhere. `frozen_model.check_sequence_schema`
exists solely to make that impossible.

---

## Layer 3: what the conformal wrapper actually guarantees

**Item level — prediction sets, not an interval.** The label is binary, so a
band `[p − q, p + q]` around the predicted probability has the *same width
for every student and every item*: it is a fixed threshold on `p` in
conformal costume. The construction used instead is the least-ambiguous
set-valued classifier (Sadinle et al. 2019), whose output is one of
`{correct}`, `{incorrect}`, `{both}`, `{}` — mapping 1:1 onto the three
statuses, with the ambiguous band derived from data rather than hand-picked.

**Checkpoint level — a real interval, on the mastery rate.** Over the k items
of a checkpoint, the estimand is the student's success *rate*. The
nonconformity is normalized by the Poisson-binomial SD the model itself
implies for those specific items, so width tracks the model's own
uncertainty instead of being constant. (In a synthetic end-to-end check of
the mechanism, widths ranged from 0.127 for a confident student to 0.463
for a struggling one — illustrative of the *variable* width, not a
measurement on real data; the real numbers come out of L3a.)

**Mondrian, not marginal.** Two taxonomies, both load-bearing:
- *warm vs. cold item* — Phase 1's own gap (test_warm .922 vs cold .913)
  means one marginal quantile would under-cover exactly the novel-question
  regime a live loop spends its time in.
- *label-conditional* — ~88% of interactions are correct, so marginal 90%
  coverage can be met while badly under-covering the `incorrect` class,
  which is the one the gate exists to catch. On the real dump the two
  classes calibrate separately and both land near nominal (~90.1% for
  `y=0`, ~90.0% for `y=1` at α=0.10), so the Mondrian split is doing its
  job.

**α is an intervention budget, not a convention.** Larger α shrinks the
ambiguous band (more confident calls, lower coverage); smaller α widens it
(more `UNCERTAIN_BEHAVIOR`, higher coverage). `conformal_calibrate.py
--alpha-sweep` reports the status mix and measured coverage across α on
the real dump, so the operating point can be chosen deliberately rather
than inherited from convention; α=0.10 is the current working baseline.

### Limitations, stated rather than buried

1. **Exchangeability does not strictly hold.** The splits are chronological
   (`val` = middle 15% of each student's history, `test_warm` = final 15%),
   and deployment is further into the future still. Conformal's guarantee
   assumes calibration and test data are exchangeable, so the 1−α guarantee
   here is **approximate**. This is measured rather than waved away:
   coverage is reported on held-out students, and drift shows up as a gap
   between nominal and empirical coverage. A production system would want
   adaptive conformal (ACI) with periodic recalibration on recent data.
2. **`val` cannot be the calibration set.** It selected the checkpoint
   (epoch 45, by joint score), so its residuals are optimistically biased.
   It is used for diagnosis only; calibration uses a **by-student** half of
   `test_warm` + `test_cold_item`, and coverage is verified on the other
   half. By student, not by event — two events from one student are
   strongly dependent, and an event-level split would flatter the numbers.
   By *base* student, not by window — prepared records are window-suffixed
   (`student#w0`, `student#w1`, …), so `dump_predictions.py` and
   `conformal_calibrate.py` both strip the `#wN` suffix; without that,
   82,231 windows would masquerade as 82,231 students instead of the real
   26,842, and one student's windows could land on both sides of the split.
3. **Coverage is a property of the stream, not of one decision.** 90%
   coverage says nothing about whether any individual call is right.
   `CheckpointTriggerResult` therefore carries `alpha`, the measured
   coverage for its regime, and the caveats, so no consumer can quote the
   bound without its conditions.

### Measured results (L3a on the real dump, alpha = 0.10)

The tables below preserve the original calibration and checkpoint replay as a
comparison baseline. The finite-sample quantile now selects the exact
`ceil((n + 1) * (1 - alpha))`-th score rather than the next score. Corrected
results are under `artifacts/conformal_rank_fix_20260929/`; the default
research gate loads that folder's `k10_calibration.json` and `k10_coverage.json`.
The historical `k=5` calibration there is separate and is not loaded by the
Phase 3 scenario runner. Do not rerun the calibration CLI without distinct
`--out` and `--coverage-out` paths if preserving either baseline matters.

On the same held-out students, corrected `k=10` item coverage is 90.0499%
(original 90.0509%) and checkpoint coverage is 89.9219% over 79,896 blocks
(original 89.9244%; two fewer covered blocks). The corrected mean checkpoint
interval width is 0.204510 (original 0.204520). The corrected checkpoint
replay (`k10_checkpoint_decision_report.json`) changes two of 79,896 status
calls from `UNCERTAIN_BEHAVIOR` to `MASTERY_SAFE`; struggle calls stay at
13,313. A separately calibrated historical `k=5` run covers 89.8876% of
175,201 held-out blocks, with mean width 0.242186. Thus the fix makes the
quantile exact, not materially better on these empirical metrics; neither
block size is validated on the fixed 20/40-question instrument.

From the original `conformal_coverage_report.json` — 26,842 real students,
split 13,421 calibration / 13,421 evaluation:

| metric | value |
|---|---|
| empirical item coverage | ~90.05% |
| incorrect-class coverage (`y=0`) | ~90.14% |
| correct-class coverage (`y=1`) | ~90.03% |
| confident-struggle precision (item level) | ~65.7% |
| mastery-safe precision (item level) | ~96.8% |

The 65.7% is ~3.3× the ~19.8% base incorrect rate — a real signal, but
roughly one in three item-level struggle flags lands on an answer the
student got right. It warrants gathering more evidence, not an immediate
"you haven't mastered this" — which is why the live path waits for a
multi-item checkpoint.

### Checkpoint-level decisions (`evaluate_checkpoints.py`)

Replays every non-overlapping 10-item same-skill checkpoint of held-out
students through the calibrated gate, then through the graph. Results in
`checkpoint_decision_report.json`; one privacy-safe JSON line per
checkpoint in `checkpoint_replay.jsonl` (pseudonymous indices only).

Across **79,896 held-out checkpoints** at alpha = 0.10:

| | count / value |
|---|---|
| `CONFIDENT_STRUGGLE` | 13,313 |
| `MASTERY_SAFE` | 37,448 |
| `UNCERTAIN_BEHAVIOR` | 29,135 |
| struggle precision (vs same-checkpoint observed rate) | ~98.4% |
| struggle recall | ~61.8% |
| mastery precision | ~99.6% |
| confident-decision accuracy | ~99.3% |
| grounded graph target among struggle calls | ~54.7% |

Warm (49,243) and cold (30,653) checkpoints are reported separately; cold
is slightly less precise (~98.0% struggle precision) and markedly less
graph-covered (~47.9% grounded vs ~59.3% warm).

**Baseline comparison.** A rule "predict struggle iff the student's prior
10 same-skill answers average below the mastery bar" is eligible on 82.9%
of checkpoints, correct on 77.2% of them, and 83.7% on the subset where
the gate acted — versus 99.25% gate accuracy on the same eligible set.
The gate abstains where the baseline guesses, so compare on the common
subset, not headline-vs-headline.

**Honesty caveat.** Same-checkpoint precision is *not* future-outcome
precision. Where another 10 same-skill held-out answers exist later
(~72% of checkpoints), the future below-mastery rate after a struggle
flag is ~66.7% — versus ~27.0% overall. The flag identifies real risk,
but it is descriptive evidence, not an intervention-effect estimate.

---

## Layer 4: why the graph is layered

Nodes are the **560 skills with an embedding row**. Not the 293 in
`reports_v2/item_context.csv` — that file is restricted to option-bearing
exercise families (12,455 of 26,070 items), so building on it would silently
omit half the curriculum while the gate can flag any of the 560. The full
mapping is recovered from `kt_interactions_v2_item_level.csv.gz`, which
carries `skill_id` and `skill_name` on every row. L0 finds 561 observed
skills, exactly 560 of which have an embedding row — an independent
confirmation of `split_report.json`'s `n_skills: 560`. The 561st has 9
unlabeled interactions and is excluded rather than carried as a dead node.

In a curriculum *everything* correlates with everything, because the
curriculum orders the observations. So each edge carries an explicit
`relation` and its provenance, and only the derived layer is queried:

| relation | evidence | claim |
|---|---|---|
| `curriculum_order` | first-encounter times per student | students met A before B — descriptive, **not** causal |
| `co_occurrence` | skills sharing items | exercised together; no direction |
| `content_similarity` | cosine of mean frozen content embeddings | similar material; not dependence. **Optional — no producer script exists yet** (build one by averaging each skill's `content_text` vectors from `text_embeddings_v2.npz` and emitting a pairwise cosine list as `artifacts/skill_content_similarity.csv`); `build_knowledge_graph.py` consumes it via `--content-similarity` if present and skips the layer cleanly if not |
| `predictive_dependency` | counterfactual probes of frozen D | the model's belief about B moves when A's outcome changes |
| `prerequisite` | **derived** from the above | A is plausibly foundational for B |

`prerequisite` requires all four of: temporal precedence, model dependence,
**a measured reverse direction**, and directional asymmetry. The middle
conditions make the graph model-grounded instead of a restatement of the
timetable; the last two stop the timetable from manufacturing direction.
The reverse-direction requirement matters: an earlier version treated an
unmeasured B→A dependency as zero, which manufactured asymmetry out of
missing data — requiring the measurement cut the candidate set from 531
edges to the current **99**.

Why this matters concretely: the strongest `curriculum_order` edges in this
data include **`Kertolasku` (multiplication) → `Kulmat` (angles) at
precedence 1.00**. Pedagogically that is not a prerequisite at all — it is
just what gets taught first. Any graph built on temporal order alone would
name multiplication as the thing to review for a student struggling with
angles. Requiring the frozen model to actually lean on the source skill,
asymmetrically, is what filters this out.

If the probe has not been run, `build_knowledge_graph.py` emits the evidence
layers and **no** `prerequisite` edges, and `kg_query` returns "no grounded
recommendation" rather than falling back to temporal order.
`--allow-weak-evidence` opts into that fallback and tags every result
`evidence="weak"`.

**This is a reviewable hypothesis, not a randomised causal claim.**
`predictive_dependency` is a dependency *inside the model* and inherits
whatever this curriculum taught it. Keeping the evidence layers separate and
inspectable is precisely what lets a domain expert reject an edge by
pointing at what it rests on.

---

## Status

| layer | state |
|---|---|
| L0 — skill catalog | **done** — 561 skills observed, 560 with embedding rows, 81,218 (skill, item) pairs |
| L1 — frozen loader | **done** — verified against the real checkpoint (384-d table matches `content_proj`); LUMI checkpoint hash-verified identical to local |
| L2 — prediction dump | **done** — 3,610,076 scored events on LUMI (`predictions_d.npz`, 17 MB, copied back) |
| L3a — conformal calibration | **done** — ~90.05% empirical item coverage at alpha 0.10 on held-out students; see "Measured results" |
| L3b — conformal gate | **done** — verified end-to-end against the graph |
| L4a — dependency probe | **done** — first 4,000-window probe plus three 20,000-window stability probes (seeds 20260919/20/21) |
| L4b — knowledge graph | **done** — 560 nodes; 1,705 `curriculum_order`, 11,959 `co_occurrence`, 1,095 `predictive_dependency`, **99 `prerequisite`** edges (both directions measured) covering **53 of 560** target skills |
| L4c — graph query | **done** — verified end-to-end |
| eval — checkpoint replay | **done** — `evaluate_checkpoints.py`, 79,896 checkpoints; see "Checkpoint-level decisions" |
| eval — graph stability | **done** — three 20,000-window graphs have 282/280/301 prerequisite edges; `artifacts/stability/graph_stability_report.json` records only 19 of 49 baseline-covered targets with the same top suggestion across all four graphs (where all four have a suggestion) |
| offline 20/40 demo | **done; unreviewed** — private 40-question MCQ bank and scripted student/teacher feeds; optional frozen-KT trace is coded but not run against the full checkpoint |
| L5 — Socratic LLM | **future / out of scope** |

---

## What remains, in order

1. **Define and build the informational feed** — the product milestone
   this machinery serves. `test_feed.py` holds a pure observed-answer
   prototype of it; the live feed is still to build: a feedback view
   shown at the midpoint and end of a test, for students and teachers,
   presenting observed performance and model uncertainty (gate status,
   checkpoint interval, measured coverage) with cautious wording. It may
   automatically select non-binding "possible skill to review"
   recommendations where the evidence supports them — gated on items 3–4
   below — but redirects nobody and enforces no next step.
2. **Live single-student inference interface** — a thin service that keeps
   the frozen model + gate + graph in memory and maintains per-student
   history; the feed would read from this. One open issue: unseen live
   `content_text` currently falls back to embedding row 0 in the dataset
   path — unsafe; a live path must embed new text with the *same*
   sentence encoder or explicitly abstain.
3. **Use the measured graph stability to qualify optional suggestions** —
   not a prerequisite for the feed itself. The three probe outputs and
   `artifacts/stability/graph_stability_report.json` are available: edge
   overlap is limited and top-ranked suggestions change across samples.
   Review which edges persist, their probe support, and their educational
   plausibility before using them in the feed; do not present one top-ranked
   skill as a certain prerequisite. If evidence is insufficient, omit the
   related-skill hint rather than block the rest of the feed.
4. **Human review of the graph** — likewise supports only the optional
   suggestions: the `prerequisite` layer is the artifact to iterate on
   with Prof. Cochez; edges can be rejected by pointing at which evidence
   they rest on.
5. **Recalibration cadence / adaptive conformal** — needs live data.

Automatic redirection to a skill, enforced study actions or paths, and
Layer 5 tutoring dialogue are future directions, not items on this list —
see "Out of scope".

## Out of scope, deliberately

- **Automatic redirection, enforced study, and autonomous tutoring.** The
  feed may automatically choose which information and non-binding
  suggestions to show; it never navigates a student to a skill, enforces
  a study action or path, or conducts a tutoring dialogue. Those are
  possible future directions only.
- **The Socratic LLM layer (Layer 5).** `CheckpointTriggerResult` is the
  hand-off contract; whatever consumes it (a tutoring LLM, a recommendation
  UI) is a separate component. It must never feed back into the gating
  decision — the moment an LLM influences *whether* to intervene, the
  conformal guarantees become unmeasurable.
- **No agent framework is required for the hand-off.** A trigger result is
  a plain dict; a tutoring call is a prompt + an API call. LangChain or
  similar only earns its complexity if the tutoring side grows multi-turn
  tool use (e.g. the LLM querying the KG itself mid-conversation).
- **Misconception-aware modeling** stays in the icebox: no expert labels
  exist, and variant D's `option_proj` slot is already designed to accept a
  `misconception_id` embedding later if that changes.
- **Recalibration cadence** is unresolved by design — adaptive conformal
  (ACI) on recent production data is the production answer, but needs live
  data to tune against.

## Environment

Tested on Python 3.13, torch 2.7.0 (+cu118), numpy 2.2, sklearn 1.6,
networkx 3.4. Laptop RAM (~16 GiB) suffices for L0, L3a, L4b, L4c and for
the gate in production; L2 and L4a assume the model-plus-data footprint of a
LUMI node. `artifacts/` is `.gitignore`d entirely — everything in it is
regenerated, and `student_skill_first_encounter.csv.gz` carries student
identifiers.
