# Misconception tagging: from Eedi validation to the real math-path data

This folder now has two tagging pipelines, for two different problems:

| Script | Problem | Taxonomy |
|---|---|---|
| `tag_distractors.py` | Validate the tag-then-verify *approach* against Eedi's labeled data | Closed-set: pick from Eedi's ~2500 known `MisconceptionId`s |
| `tag_math_distractors.py` | Actually tag `../learning path maths/kt_phase1/reports_v2/distractor_catalog.csv` | Open-set: no fixed taxonomy exists, so one is discovered bottom-up |

Both share `tagging_common.py` (Aitta client, retry/backoff, a checkpoint
helper, and clustering utilities).

## Why two different approaches (closed-set vs. open-set)

Eedi's dataset ships with a fixed list of misconceptions, so tagging there is
"pick the best of these ~2500 candidates" -- a retrieval + classification
problem, which is what `tag_distractors.py` measures.

The math-path data has **no such list**. Nobody has ever labeled what
conceptual error each wrong answer represents, so there is nothing to
retrieve candidates from. `tag_math_distractors.py` instead:
1. asks the LLM to *describe* the likely error in free text (no candidate
   list to pick from),
2. embeds every description and greedily clusters them by similarity --
   this **discovers** the taxonomy instead of assuming one exists,
3. the resulting clusters become your `misconception_id`s, with the
   most-central member's description as the `misconception_label`.

## Measured result on Eedi (why verification needs a second run before you trust it)

Running `tag_distractors.py --n-samples 100 --verify-mode uncertain` against
the known-ground-truth Eedi set gave:

- **Top-1 accuracy (pass 1): 32%** (retrieval recall@50-75: 73%, so most of
  the gap is a tagging problem, not a retrieval problem)
- **78/100 tags were low-confidence enough to trigger verification**
- **Verification changed the answer 0/78 times**, even with a "argue for the
  runner-up first" framing designed to force real engagement with the
  alternative rather than a rubber-stamp.

That's a real, measured confirmation of a specific risk: **the same model
verifying its own tag, even with a differently-framed second prompt, did not
catch a single case here.** Both `tag_distractors.py` and
`tag_math_distractors.py` now take a `--verify-model` flag so the verify
stage can run on a *different* Aitta-hosted model than the elicit/tag stage
used (see https://aitta.csc.fi for the current catalog -- e.g. elicit with
`openai/gpt-oss-120b`, verify with `meta-llama/Llama-3.3-70B-Instruct` or
`mistralai/Ministral-3-14B-Reasoning-2512`). Smoke-test a candidate pairing
on a small `--limit` before a full run: model availability on the staging
instance changes, and a differently-sized/trained model may follow the
"Reasoning: ... Verdict: ..." output format less reliably, which is worth
checking on a handful of rows before trusting it at scale. Until you've
actually measured a different-model pairing catching cases same-model
verification missed, keep treating `needs_human_review.csv` as **the**
review queue, not the `verify` stage's CONFIRM/REPLACE verdicts as ground
truth.

## Running the math-data pipeline

Prerequisites (see `../learning path maths/kt_phase1/README_v2.md`):

```bash
cd "../learning path maths/kt_phase1"
python build_v2_raw_clean.py      # stage 1 (if not already run)
python build_v2_sort.py           # stage 2
python build_v2_item_level.py     # stage 3 -- produces reports_v2/distractor_catalog.csv
python build_v2_item_context.py   # stage 4 (new) -- produces reports_v2/item_context.csv,
                                   # needed because distractor_catalog.csv intentionally
                                   # doesn't store question text (see its docstring)
```

Then, from `misconception_tagging/`:

```bash
# elicit -> cluster -> verify -> export, in one go:
python tag_math_distractors.py --stage all

# or step by step, so you can inspect/stop after any stage:
python tag_math_distractors.py --stage elicit --types MATH_DRILLER   # one type at a time
python tag_math_distractors.py --stage elicit                        # remaining types, impact order
python tag_math_distractors.py --stage cluster
python tag_math_distractors.py --stage verify \
    --verify-model meta-llama/Llama-3.3-70B-Instruct   # optional: decorrelate from elicit's model
python tag_math_distractors.py --stage export
```

Every stage writes to `--checkpoint-dir` (default
`misconception_cache/math_tagging/`) as newline-delimited JSON, one line per
completed item, flushed immediately -- safe to Ctrl-C and resume, or
resubmit as a SLURM job after a time-limit kill (see `run_lumi_tagging.sh`).

### Runtime and concurrency

Two independent levers, both on by default:

- **`--concurrency`** (default 8, also settable on LUMI with
  `KT_CONCURRENCY`): `elicit` and `verify` send requests in parallel -- only
  the network call runs on worker threads, every checkpoint write happens
  back on the main thread, so resumability is unaffected. Smoke-tested
  concurrency=6 got a ~3x speedup over sequential (network/GPU-queue bound,
  so it won't scale linearly with thread count -- `tag_distractors.py`'s
  notes measured concurrency up to ~10-15 as safe against Aitta's rate
  limiting, with 20 triggering 429s). Start at the default 8 and watch for
  429 retries in the log before pushing higher.
- **`--reasoning-effort low`** (default, also `KT_REASONING_EFFORT`):
  `openai/gpt-oss-120b` is a reasoning model that burns most of its latency
  on an internal chain-of-thought before the actual answer. Capping that
  measured ~2-4x lower latency and completion tokens on a realistic elicit
  prompt (5.5s/223 tokens -> 2.4s/87 tokens), with no observed drop in
  output format compliance or label quality on spot checks. It's silently
  ignored by non-reasoning models (confirmed against
  `meta-llama/Llama-3.3-70B-Instruct` -- no error, just no effect), so it's
  safe to leave on for `verify` even if `--verify-model` isn't a reasoning
  model. Pass `--reasoning-effort ""` to disable and use the model's
  default reasoning budget.

At `--min-times-selected 2` (the default), the real catalog has ~19,500
elicit calls. Fully sequential (`--concurrency 1`, `--reasoning-effort ""`)
that's ~7-9s/call, ~40+ hours -- likely to blow past `run_lumi_tagging.sh`'s
2-day SLURM `--time` limit. With both levers on (the defaults), each call
dropped to roughly 2-3s in smoke testing, i.e. combined the two easily bring
elicit down to single-digit hours rather than 40+ -- but that's from small
smoke-test samples, not a full run, so treat it as directional and watch
the first few thousand real calls' pace before assuming it holds at scale.

### Outputs (`--out-dir`, default `misconception_cache/math_tagging_out/`)

- **`distractor_catalog_tagged.csv`** -- the original catalog with
  `misconception_id` / `misconception_label` filled in, plus
  `confidence_margin`, `cluster_size`, `needs_human_review`. This is the
  *pre-review* export; after `apply_review.py` + Gemini/manual review the
  authoritative file is `out/distractor_catalog_final_v2.csv` (see
  "Final artifacts" below), which `merge_into_kt.py` joins onto
  `kt_interactions_v2_item_level.csv.gz` by
  `(item_id, selected_option_value)`.
- **`cluster_summary.csv`** -- one row per discovered misconception,
  sorted by total impact (`times_selected` summed across members). Start
  your sanity check here.
- **`human_review_queue.csv`** -- only the flagged rows (small cluster / low
  confidence margin / missing question context), sorted by impact. This is
  the practical "pass to a human one-by-one" queue -- review the highest-
  impact rows first, since that's where a wrong tag would do the most damage
  once it's feeding a KT model.

### Coverage: which exercise types this tags (and why only those)

The distractor-catalog approach only works where the student's wrong answer
is a *selection from a fixed option list* -- that's what makes a wrong option
a countable, repeatable "distractor" worth an LLM call. In the math-path data
that means the four option-based `exercise_type`s:

| exercise_type | wrong-answer rows in catalog | tagged (times_selected >= 2) |
|---|---|---|
| `MATH_DRILLER` | 140,984 | 16,163 |
| `VILLE_QUIZ` | 12,349 | 3,417 |
| `CROSSWORD_PUZZLE` | 0 | 0 |
| `VOICE_DRILLER` | 104, all selected once | 0 |

So all 19,580 eligible distractors are tagged (0 blank labels/ids in
`distractor_catalog_final_v2.csv`). The remaining ~133.9k wrong-answer
catalog rows are options never/rarely selected (119k with `times_selected`
= 0, 14.6k = 1) -- one-off typos/guesses, not repeatable misconceptions.

Every other exercise type in the KT data (`MATH_CALCULATION_ORDER`,
`FILL_IN_EXERCISE`, `MATH_SYMBOLIC_EXER`, `RUNNER`, ...) has `has_options = 0`
-- free-text/typed answers, so there is no distractor to tag. Measured on
the merged `kt_interactions_v2_item_level_misconceptions.csv.gz` (13.9M
rows): covered types are 43.1% of interactions; 317,261 of 717,708 wrong
interactions in covered types (44.2%) match a tagged distractor = 12.4% of
all 2,558,313 wrong interactions. The remaining covered-type wrongs are
typed free-text answers with no catalog distractor (~264k), one-off
selections below the threshold (~17k), or rows with no option recorded.
Tagging the free-text types would need a different pipeline (elicit from
`answer_raw` directly, deduplicated) -- a separate folder, not this one.

### Why the catalog is keyed by `item_id`, not `item_instance_id`

`build_v2_item_level.py` assigns two identifiers per row: `item_id`
(`ExerciseId + PreOrd` -- one independent "item slot") and
`item_instance_id` (`item_id + SHA1(question text)` -- one specific
randomized rendering of that slot, e.g. a fraction-arithmetic template with
a different pair of numbers substituted in each time). The distractor
catalog (`distractor_stats` in `build_v2_item_level.py`) aggregates
`times_offered`/`times_selected` by `(item_id, option_value)`, deliberately
*not* `(item_instance_id, option_value)`. This wasn't just the path of
least resistance -- pooling at `item_instance_id` was checked and is
impractical, on the actual data:

| Metric (measured on `kt_interactions_v2_item_level.csv.gz`, 13.9M rows) | Value |
|---|---|
| Distinct `item_id` | 26,070 |
| Distinct `item_instance_id` | 1,449,163 (~55.6x more) |
| Instances per item | mean 55.6, median 6, max 5,049 |
| Interactions per instance | mean 9.6, **median 1** |
| Instances seen fewer than 5 times ever | 1,226,226 / 1,449,163 (84.6%) |

Tagging at `item_instance_id` granularity would mean:
1. **No signal to tag.** With a median of one interaction per instance,
   almost every option's `times_selected` would be 0 or 1 -- below even
   `--min-times-selected 1`. You'd be eliciting misconceptions from
   unreplicated single data points, exactly the "long-tail
   typo/guess, not a repeatable misconception" case `--min-times-selected`
   already exists to filter out.
2. **~55x (up to ~5,000x for the worst items) more LLM elicit calls for no
   new knowledge.** The misconception a wrong option represents (e.g. "adds
   numerators and denominators separately") is a property of the *template*,
   not the specific randomized numbers -- re-eliciting it separately for
   every instance of the same item would mostly be redundant spend, not new
   signal.

Aggregating at `item_id` is what makes `times_selected` accumulate into
something large enough to reason about at all -- it pools the same
underlying mistake pattern across every instance of a template.

**The cost of this choice**: pooling correctness across instances means
`is_correct_option = is_correct_option OR option["correct"]` can mark an
option value as "correct" globally even if it's wrong in most of the
instances that actually offered it -- e.g. item `23127__p11`'s option `"7"`
is `correct: True` in only a handful of its ~20+ instances (different
randomized numbers give different correct answers) but `correct: False` in
most others (109 offers, 41 selections total); the OR marks it
`is_correct_option=1` catalog-wide, so `load_distractors()`'s
`is_correct_option == 0` filter silently drops it from tagging even on the
interactions where it was genuinely wrong. This is a real, known limitation
of item-level aggregation, not a bug in the item-vs-instance tradeoff
itself -- a fix would track per-value correctness as a rate (e.g. "correct
in X% of instances that offered it") rather than a boolean OR, while still
aggregating at `item_id` (switching to `item_instance_id` to fix this would
reintroduce the sparsity problem above).

### Final artifacts

- **`out/distractor_catalog_final_v2.csv`** -- authoritative catalog:
  all 19,580 eligible distractors with `final_misconception_label`,
  `label_source` (`auto_unflagged` 7,135 / `verify_confirm` 6,674 /
  `verify_replace` 5,107 / `gemini_new_tag` 641 / `gemini_verify_retag`
  15 / `manual_override` 8), plus the original `misconception_id` /
  `misconception_label` and review metadata.
- **`merge_into_kt.py`** -- left-joins the catalog onto the item-level
  interactions by `(item_id, selected_option_value)`. Reads/writes
  verbatim strings (`dtype=str, keep_default_na=False`): pandas' default
  type coercion silently rewrote `correctness` `'1'` -> `'1.0'` (which
  crashes `prepare_sequences_v2.py`'s `int()` parse) and `'N/A'` ->
  `''`, and made the join miss ~17k matches -- don't "simplify" this.
- **Merged output**: `kt_phase1/data_v2/kt_interactions_v2_item_level_misconceptions.csv.gz`
  (full 13.9M rows + 3 misconception columns), plus
  `..._sample.csv` (first 10k rows) and `..._tagged_sample.csv`
  (first 500 tagged rows) for inspection.

### Processing order

`--stage elicit` processes `exercise_type`s in order of total impact (sum of
`times_selected`), and within each type, highest-`times_selected` distractors
first. You can restrict to one type with `--types MATH_DRILLER` to review a
single type's results before continuing to the rest -- exactly the
"one type at a time, then review" workflow discussed before building this.

Clustering (`--stage cluster`) is global, across all elicited types, since
the same underlying misconception (e.g. a sign error) can show up in
multiple exercise types and should land in the same cluster rather than one
per type.

## Tuning knobs

- `--min-times-selected` (default 2): skip long-tail distractors selected
  only once -- usually typos/guesses, not a repeatable misconception, and
  not worth an LLM call.
- `--cluster-threshold` (default 0.82): higher = stricter, more (smaller,
  more specific) clusters; lower = looser, fewer (broader) clusters. There's
  no universally "right" value -- inspect `cluster_summary.csv` after a run
  and adjust; re-clustering is free (no LLM calls, `--stage cluster` only).
- `--small-cluster-threshold` / `--verify-margin`: control what counts as
  "needs review". Loosen these if `human_review_queue.csv` is too long to
  realistically get through; tighten them if spot-checks of the "confident"
  bucket turn up mistakes.
