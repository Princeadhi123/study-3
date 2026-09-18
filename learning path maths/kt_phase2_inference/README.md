# Phase 2 — inference, conformal gating, and the skill knowledge graph

Phase 1 is **closed**. Its tuning phase reached a multi-seed-confirmed stop
(four rounds; see `../kt_phase1/modeling/README.md`, "Rounds 2-4"), and the
deployed model is a single frozen checkpoint. Nothing in this directory
trains, fine-tunes, or writes back into `kt_phase1/`.

The deployed model is **variant D** (`skill_item_content_option`) at
`best_model_joint.pt` — epoch 45, seed 42, `d_model=192`, `n_layers=3`,
5,850,369 parameters. That checkpoint rather than `best_model.pt` because
the joint-selected epoch is the documented deployment recommendation: a
live tutoring loop mostly faces questions the model never trained on, which
is the `test_cold_item` regime.

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
L4  knowledge graph -> remediation target
      L4a  counterfactual probing       probe_predictive_dependency.py
      L4b  graph construction           build_knowledge_graph.py
      L4c  graph query                  kg_query.py
L5  Socratic LLM                        FUTURE -- not in this codebase
```

The gate decides *whether* to intervene; the graph decides *where to send
the student* when it does. They meet at
`ConformalGate.checkpoint(..., remediation_lookup=kg.remediation_lookup())`,
which returns a self-contained `CheckpointTriggerResult` for the Socratic
LLM.

**Nothing here calls an LLM or any agent framework.** Layers 1–4 are
entirely deterministic/statistical — the LLM is Layer 5, downstream of this
pipeline, and is deliberately kept out of the gating decision. See
"Out of scope" below.

### Files

| file | role |
|---|---|
| `paths.py` | every frozen input/output location; `require()` fails loudly on missing inputs |
| `build_skill_catalog.py` | **L0** — the complete 560-skill catalog + per-student first-encounter times |
| `frozen_model.py` | **L1 loader** — strict loader; refuses to run on the wrong embeddings or stale sequences |
| `dump_predictions.py` | **L2** — one forward pass; caches every held-out prediction |
| `conformal_calibrate.py` | **L3a** — offline calibration + empirical coverage report |
| `conformal_gate.py` | **L3b** — the live gate: `InterventionStatus`, `CheckpointTriggerResult` |
| `probe_predictive_dependency.py` | **L4a** — counterfactual probing of the frozen model |
| `build_knowledge_graph.py` | **L4b** — evidence layers + derived `prerequisite` edges |
| `kg_query.py` | **L4c** — "the gate flagged B; what do we drop back to?" |

---

## Run order

L0, L3a, L4b and L4c run on a laptop. L2 and L4a need the frozen model in
memory — the embedding table alone is 2.2 GB and the parsed sequences are
several GB more, so **run them inside a LUMI allocation** and copy the
(small) outputs back.

```bash
# L0 -- ~8 min, streams the 13.9M-row interactions file
python build_skill_catalog.py

# L2 -- GPU; writes artifacts/predictions_d.npz
python dump_predictions.py --device cuda

# L3a -- seconds, from the cached predictions
python conformal_calibrate.py --alpha 0.10 --alpha-sweep

# L4a -- GPU; writes artifacts/predictive_dependency.csv
python probe_predictive_dependency.py --device cuda --max-windows 4000

# L4b -- ~1 min
python build_knowledge_graph.py --dependency artifacts/predictive_dependency.csv

# L4c -- query it
python kg_query.py --skill "Kertaus: Yhtälöt"
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
  which is the one the gate exists to catch. (On synthetic data the two
  class quantiles came out a factor of two apart — 0.897 for y=0 vs 0.419
  for y=1 — confirming the classes genuinely need separate calibration; the
  real quantiles come out of L3a.)

**α is an intervention budget, not a convention.** Larger α shrinks the
ambiguous band (more confident calls, lower coverage); smaller α widens it
(more `UNCERTAIN_BEHAVIOR`, higher coverage). Where the useful operating
point sits depends on the model's real probability spread, which we won't
know until L3a runs on the actual predictions — `--alpha-sweep` reports the
status mix and measured coverage across α so it can be chosen deliberately
rather than inherited from convention.

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
3. **Coverage is a property of the stream, not of one decision.** 90%
   coverage says nothing about whether any individual call is right.
   `CheckpointTriggerResult` therefore carries `alpha`, the measured
   coverage for its regime, and the caveats, so no consumer can quote the
   bound without its conditions.

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

`prerequisite` requires all three of: temporal precedence, model dependence,
and directional asymmetry. The middle condition is what makes the graph
model-grounded instead of a restatement of the timetable; the third stops
the timetable from manufacturing direction.

Why this matters concretely: the strongest `curriculum_order` edges in this
data include **`Kertolasku` (multiplication) → `Kulmat` (angles) at
precedence 1.00**. Pedagogically that is not a prerequisite at all — it is
just what gets taught first. Any graph built on temporal order alone would
route a student struggling with angles back to multiplication. Requiring the
frozen model to actually lean on the source skill, asymmetrically, is what
filters this out.

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
| L1 — frozen loader | **done** — verified against the real checkpoint (384-d table matches `content_proj`) |
| L2 — prediction dump | code complete; **needs LUMI** (embeddings + correct sequences) |
| L3a — conformal calibration | code complete; validated on synthetic data at the correct coverage; needs L2 |
| L3b — conformal gate | **done** — verified end-to-end against the graph |
| L4a — dependency probe | code complete; **needs LUMI** |
| L4b — knowledge graph | evidence layers **done** (560 nodes, 1,705 `curriculum_order`, 11,959 `co_occurrence`); `prerequisite` needs L4a |
| L4c — graph query | **done** — verified end-to-end |
| L5 — Socratic LLM | **future / out of scope** |

---

## What remains, in order

1. **rsync the correct sequences** from LUMI (`prepared_v2_w400_o0p25_g30_c8/`),
   per "Required inputs" above — the local file predates `content_text` /
   `selected_text` / `time_bin` and the loader refuses it.
2. **L2 + L4a on LUMI** (both need the frozen model; ~2.2 GB embedding
   table + multi-GB parsed sequences exceed a laptop's working memory).
   Copy `predictions_d.npz` and `predictive_dependency.csv` back.
3. **L3a locally** — calibrate, then read `conformal_coverage_report.json`
   before trusting anything: empirical coverage must track 1−α on held-out
   students, and the `coverage_y0` column is the one that matters for the
   struggle class.
4. **L4b rerun** with `--dependency` — inspect `prerequisite` edges
   manually before they drive anything.
5. **Human review of the graph** — the `prerequisite` layer is the artifact
   to iterate on with Prof. Cochez; edges can be rejected by pointing at
   which evidence they rest on.

## Out of scope, deliberately

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
