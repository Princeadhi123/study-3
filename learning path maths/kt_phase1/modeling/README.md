# Math-path KT models: four variants

Trains and compares input configurations of the same causal Transformer KT
architecture (SAKT/DKT-style). All four variants train on the same v2 data
(`kt_phase1/data_v2/kt_interactions_v2_item_level.csv.gz`, prepared by
`prepare_sequences_v2.py`) — same item/skill vocab, same train/val/test/
cold-item split for every variant, so A/B/C/D are directly comparable. See
`../README_v2.md` for what v2 extracts (parsed multiple-choice options +
the student's actual selected answer, for every exercise type that has them)
that v1 (now `../legacy_v1_item_level/`) discarded.

| Variant | Inputs |
|---|---|
| A `skill_only` | `skill_id` + correctness history |
| B `skill_item` | + `item_id` (`ExerciseId + PreOrd`), response time, attempt number |
| C `skill_item_content` | + frozen multilingual sentence embedding of the CURRENT question's full content: question text plus, for exercise types that offer discrete options (mcq/single_answer_text), the offered option VALUES — never which one is correct or which one the student picked, see `build_content_text()` in `prepare_sequences_v2.py` |
| D `skill_item_content_option` | C's inputs + the frozen multilingual embedding of the actual TEXT the student selected/answered on the PREVIOUS step (never the current one, that would leak the label) |

All four share one architecture (`KTTransformer` in `model.py`); only the
active input features differ, so any performance difference between runs is
attributable to the added features, not to a different model family.

C and D are meant to be read as a pair for the research question "does
knowing the full current question (including its options) help, and does
also remembering *what* the student previously selected help further?" —
not as two disconnected model families. D deliberately embeds the previous
selection's TEXT (e.g. `"-24"`), not a bare `nn.Embedding(option_position)`:
an index embedding would wrongly force "option 1" to mean the same thing on
every question, when option 1 is `"4"` on one question and `"20"` on
another. It's looked up in the exact same `content_vectors` table as the
current question's content, via its own learned projection (`option_proj`
in `model.py`). D is also the direct building block for a future
misconception-aware variant (swap that text-embedding lookup for a
`misconception_embed(prev_misconception_id)` once
`reports_v2/distractor_catalog.csv` has been annotated — no architecture
change needed beyond that one lookup).

## On Finnish text — no translation needed

Model C embeds the raw Finnish/DSL `text` field directly with a multilingual
sentence-transformer (`paraphrase-multilingual-MiniLM-L12-v2` by default).
Multilingual embedding models are trained so that semantically similar
content in different languages lands in a shared vector space, so no
Finnish-to-English translation step is required for training. Translation is
only useful later, for human-readable feedback/reporting, not for this model.

## Files

- `prepare_sequences.py` — reads `kt_interactions.csv.gz`, builds per-student
  chronological sequences, builds skill/item vocabularies, assigns each
  interaction to `train` / `val` / `test_warm` / `test_cold_item` (see below),
  and splits students longer than `--max-seq-len` into multiple overlapping
  windows so no interactions are discarded (see "Adjusting scale" below).
- `embed_questions.py` — one-time step, only needed for variant C. Embeds
  every unique `text` value with a multilingual sentence-transformer.
  Requires internet on first run to download the model from Hugging Face;
  cache it once and copy `~/.cache/huggingface` across if compute nodes have
  no internet.
- `dataset.py` — PyTorch `Dataset`; turns each student into one fixed-length,
  right-padded, causally-ordered sequence.
- `model.py` — the shared `KTTransformer` architecture and `build_model()`
  factory for the three variants.
- `train.py` — trains one variant, evaluates every epoch, saves the best
  checkpoint (by validation AUC) and full metric history.
- `compare_runs.py` — after training all three, prints a side-by-side AUC
  comparison table and writes `runs/comparison.json`.
- `run_all_variants.sh` — SLURM-style script that runs the full pipeline
  end-to-end on the supercomputer; adapt the `#SBATCH` header to your cluster.

## Evaluation splits (why there are four, not two)

Each student's own chronological history is split by time:

- `train` — first ~70% of that student's interactions.
- `val` — next ~15%, used for early-stopping / model selection.
- `test_warm` — final ~15%, the model's ordinary held-out future.
- `test_cold_item` — a randomly chosen 5% of `item_id`s are removed from
  `train` *entirely* (wherever they occur in time). Any occurrence of one of
  these items in a student's val/test range is scored separately as
  `test_cold_item`; any occurrence in the train range is excluded from loss
  entirely (`skip_cold_in_train`, never trained on).

This directly tests the concern raised earlier: since only ~10% of exact
question instances repeat more than twice, `test_cold_item` measures whether
the model actually generalizes to items/questions it never trained on, rather
than just memorizing frequently-seen `item_id`s. Compare `test_cold_item` AUC
across A/B/C — that comparison is the main point of building variant C.

**Important:** none of these four splits hold out *students* — the same
student's own history is cut into train/val/test_warm chronologically, so
val/test_warm are "unseen" only in the sense of "later in time for that
student", not "never touched this student/skill/item before". `test_cold_item`
is the only split that is unseen in a stronger sense (a whole `item_id`,
regardless of where it falls in time). If you need a fully cold-start-student
evaluation (a model that has *never* seen the student either), that would
require an additional student-level split, which this pipeline does not
currently produce.

**Cold items are excluded from `item_vocab`, not merely excluded from
training loss.** They always resolve to `__UNK__` (index 1) in `skill_item`
and `skill_item_content`. This matters: earlier versions of this pipeline
gave every cold item its own vocab slot, whose embedding row was never a
training target and therefore stayed at its random initialization forever —
i.e. it injected untrained noise into the interaction/query features
whenever a cold item appeared, artificially depressing `test_cold_item` AUC
for the item-aware variants relative to `skill_only` (which never touches
`item_id` at all). Routing cold items to `__UNK__` fixes that and also
matches how a genuinely novel item would be handled at inference time.

## Running

### Local smoke test (CPU, a few minutes, sanity-check only)

```bash
cd kt_phase1/modeling
python prepare_sequences.py --limit-rows 300000 --min-interactions 15 \
    --max-seq-len 100 --out-dir prepared_smoke
python train.py --variant skill_only --sequences prepared_smoke/sequences.jsonl.gz \
    --vocab prepared_smoke/vocab.json --out-dir runs_smoke/skill_only \
    --max-seq-len 100 --epochs 2 --batch-size 32 --device cpu --num-workers 0
```

Repeat for `skill_item` and (after running `embed_questions.py` on the smoke
sequences) `skill_item_content`. This has already been verified to run
end-to-end without errors and produce sane metrics (AUC ~0.78-0.83 on a
300k-row/585-student subset — not meaningful on its own, just confirms the
pipeline works).

### Full run on the supercomputer

```bash
cd kt_phase1/modeling
sbatch run_all_variants.sh
# or, inside an interactive GPU allocation:
bash run_all_variants.sh
```

This runs data prep once (full 13.1M rows, all 27,433 students), embeds
question text once, trains all three variants for 30 epochs each on GPU, and
prints the comparison table.

### Adjusting scale

`--max-seq-len 400` is the model's fixed attention window, not a hard cap on
how much of a student's history gets used. On the full dataset, median
student length is 231 interactions, but the distribution has a long tail:
34% of students (9,453 of 27,433) have **more** than 400 interactions, and
they account for 80% of all interactions. `prepare_sequences.py` splits any
student longer than `--max-seq-len` into multiple overlapping chronological
windows (see `chunk_student_events`) instead of keeping only their most
recent `max-seq-len` events — naively truncating like that would have
discarded 51% of the entire dataset (6.7M of 13.1M rows), overwhelmingly
from the `train` range of exactly the students who generate the most data.

Each window after the first overlaps the previous one by
`--context-overlap-frac` (default 25%) of `max-seq-len`, so a window's early
positions have real prior history to attend to (marked `split="context"`,
excluded from loss/eval) instead of being wrongly treated as the very start
of that student's history. Every real interaction contributes to train/eval
exactly once, in whichever window it's "new" in.

Raising `--max-seq-len` reduces how many students need to be split into
multiple windows (fewer, more expensive windows per long-history student) at
the cost of GPU memory (attention is O(T²)); raising `--context-overlap-frac`
gives long-history windows more real context per prediction at the cost of
more (non-loss-contributing) compute per epoch. `--max-seq-len` passed to
`prepare_sequences.py` and `train.py` must match — they are the same window.

## Reading the results

`train.py` saves **three** checkpoints per variant, because "best" depends
on which question you're asking:

- `best_model.pt` — the epoch with the best `val` AUC (standard early
  stopping; best pick for ordinary warm-item deployment).
- `best_model_cold.pt` — the epoch with the best `test_cold_item` AUC (not
  necessarily the same epoch as above; can end up being a very early,
  under-trained epoch — see "The item-id/cold-item tradeoff" below).
- `best_model_joint.pt` — the epoch maximizing
  `--joint-weight * val_auc + (1 - joint_weight) * test_cold_item_auc`
  (default weight 0.5). This is the practical recommendation for
  deployment: a middle ground that doesn't sacrifice most of the val/warm
  gains from `item_id`/content just to chase the single best (and often
  barely-trained) cold-item epoch.

`best_epochs.json` in each run directory records all three. `compare_runs.py`
reports, per variant, all views side by side (pass `--joint-weight` to match
whatever `train.py` was run with):

```text
variant             val_auc  test_warm_auc  test_cold_auc | best_cold_auc (epoch) | joint_val joint_warm joint_cold (epoch)
skill_only            ...         ...            ...            ...        ...         ...       ...        ...       ...
skill_item            ...         ...            ...            ...        ...         ...       ...        ...       ...
skill_item_content    ...         ...            ...            ...        ...         ...       ...        ...       ...
```

The left three columns are all evaluated at the val-selected epoch;
`best_cold_auc`/`(epoch)` is the best `test_cold_item` AUC reached at *any*
epoch for that variant (its generalization ceiling); the `joint_*` columns
are the val/warm/cold AUCs at the `best_model_joint.pt` epoch.

Interpretation guide (matches the plan discussed earlier):

- If B beats A on `test_warm` but not on `test_cold_item`, item IDs help for
  seen items but don't generalize — expected, since `item_id` is a bare
  categorical lookup.
- If C beats both A and B on `test_cold_item`, question content is carrying
  real generalization signal, and is the right foundation for live
  simulation (new generated questions won't have a trained `item_id` either,
  but do have text).
- If all three are close, the skill-only model (simplest, cheapest) is the
  right choice for production, and the added complexity of B/C isn't paying
  for itself yet.

Do not pick a "winner" from `val`/`test_warm` alone — `test_cold_item` is the
metric that actually answers the original generalization question.

### The item-id/cold-item tradeoff, and how it's mitigated

The first full run showed a fourth pattern the simple guide above doesn't
cover: C beat B on `test_cold_item` (question content does carry real
generalization signal) but still fell short of A, and both B's and C's
`test_cold_item` AUC **decayed steadily, almost every epoch**, while `val`/
`test_warm` kept climbing — i.e. the model was progressively overfitting
`item_id`, and the shared `__UNK__` embedding that every cold item resolves
to (see below) got progressively less useful the longer training ran.

Two knobs in `train.py` now directly target this (both no-ops for
`skill_only`, which has no `item_embed`):

- `--item-id-dropout` (default `0.1`) — during training only, each
  occurrence of a real, trainable `item_id` is independently replaced with
  `__UNK__` with this probability. This is cold-start augmentation: it gives
  the `__UNK__` row real, frequent gradient signal from ordinary items
  instead of only ever seeing genuine `test_cold_item` occurrences, and it
  keeps the model from fully relying on memorizing `item_id`.
- `--item-embed-weight-decay` (default `1e-3`, vs. the general
  `--weight-decay` default `1e-5`) — `item_embed` gets its own, much
  stronger weight decay via a separate AdamW param group, so it can't grow
  large item-specific weights as freely as the rest of the model.

Re-run the A/B/C comparison after this change and compare `by_joint` (and
the `test_cold_item` trend across epochs in `training_history.json`) to the
pre-regularization numbers to confirm the decay is actually reduced, not
just shifted to fewer epochs of training. (See "Evaluation splits" above for
why cold items resolve to `__UNK__` in the first place.)
