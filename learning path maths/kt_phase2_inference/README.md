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
such a feed (frozen KT model, conformal gate, knowledge graph). **The feed
itself is not implemented here.** Automatic redirection, enforced study
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

From `conformal_coverage_report.json` — 26,842 real students, split
13,421 calibration / 13,421 evaluation:

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
| L5 — Socratic LLM | **future / out of scope** |

---

## What remains, in order

1. **Define and build the informational feed** — the product milestone
   this machinery serves, not yet implemented: a feedback view shown at
   the midpoint and end of a test, for students and teachers, presenting
   observed performance and model uncertainty (gate status, checkpoint
   interval, measured coverage) with cautious wording. It may
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
