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

```
Layer 1  frozen variant-D KT model            kt_phase1/  (closed)
Layer 2  per-item P(correct)                  dump_predictions.py
Layer 3  conformal gate -> InterventionStatus conformal_calibrate.py + conformal_gate.py
Layer 4  knowledge graph -> remediation target build_knowledge_graph.py + kg_query.py
```

The gate decides *whether* to intervene; the graph decides *where to send
the student* when it does. They meet at
`ConformalGate.checkpoint(..., remediation_lookup=kg.remediation_lookup())`,
which returns a self-contained `CheckpointTriggerResult` for the Socratic
LLM.

### Files

| file | role |
|---|---|
| `paths.py` | every frozen input/output location; `require()` fails loudly on missing inputs |
| `build_skill_catalog.py` | **stage 0** — the complete 560-skill catalog + per-student first-encounter times |
| `frozen_model.py` | **stage 1** — strict loader; refuses to run on the wrong embeddings or stale sequences |
| `dump_predictions.py` | **stage 2** — one forward pass; caches every held-out prediction |
| `conformal_calibrate.py` | **stage 3** — offline calibration + empirical coverage report |
| `conformal_gate.py` | **stage 4** — the live gate: `InterventionStatus`, `CheckpointTriggerResult` |
| `probe_predictive_dependency.py` | **stage 5b** — counterfactual probing of the frozen model |
| `build_knowledge_graph.py` | **stage 5** — evidence layers + derived `prerequisite` edges |
| `kg_query.py` | **stage 5c** — "the gate flagged B; what do we drop back to?" |

---

## Run order

Stages 0, 5 and 5c run on a laptop. Stages 2 and 5b need the frozen model in
memory — the embedding table alone is 2.2 GB and the parsed sequences are
several GB more, so **run them inside a LUMI allocation** and copy the
(small) outputs back.

```bash
# stage 0 -- ~8 min, streams the 13.9M-row interactions file
python build_skill_catalog.py

# stage 2 -- GPU; writes artifacts/predictions_d.npz
python dump_predictions.py --device cuda

# stage 3 -- seconds, from the cached predictions
python conformal_calibrate.py --alpha 0.10 --alpha-sweep

# stage 5b -- GPU; writes artifacts/predictive_dependency.csv
python probe_predictive_dependency.py --device cuda --max-windows 4000

# stage 5 -- ~1 min
python build_knowledge_graph.py --dependency artifacts/predictive_dependency.csv

# stage 5c -- query it
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
uncertainty instead of being constant. In validation, widths ranged from
0.127 (confident mastery) to 0.463 (genuine struggle) — the variation is the
point.

**Mondrian, not marginal.** Two taxonomies, both load-bearing:
- *warm vs. cold item* — Phase 1's own gap (test_warm .922 vs cold .913)
  means one marginal quantile would under-cover exactly the novel-question
  regime a live loop spends its time in.
- *label-conditional* — ~88% of interactions are correct, so marginal 90%
  coverage can be met while badly under-covering the `incorrect` class,
  which is the one the gate exists to catch. In validation the two class
  quantiles came out at **0.897 (y=0) vs 0.419 (y=1)** — a factor of two
  apart, which is how much a single marginal quantile would have been
  wrong by.

**α is an intervention budget, not a convention.** With an accurate model,
small α is what produces a usefully-sized ambiguous band; at α=0.10 an
89%-accurate model returns a singleton almost every time. `--alpha-sweep`
reports the status mix and measured coverage across α so the operating point
is chosen deliberately.

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
carries `skill_id` and `skill_name` on every row. Stage 0 finds 561 observed
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
| `content_similarity` | cosine of mean frozen content embeddings | similar material; not dependence |
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

| stage | state |
|---|---|
| 0 — skill catalog | **done** — 561 skills observed, 560 with embedding rows, 81,218 (skill, item) pairs |
| 1 — frozen loader | **done** — verified against the real checkpoint (384-d table matches `content_proj`) |
| 2 — prediction dump | code complete; **needs LUMI** (embeddings + correct sequences) |
| 3 — conformal calibration | code complete; validated on synthetic data at the correct coverage; needs stage 2 |
| 4 — conformal gate | **done** — verified end-to-end against the graph |
| 5 — knowledge graph | evidence layers **done** (560 nodes, 1,705 `curriculum_order`, 11,959 `co_occurrence`); `prerequisite` needs stage 5b |
| 5b — dependency probe | code complete; **needs LUMI** |
| 5c — graph query | **done** — verified end-to-end |
