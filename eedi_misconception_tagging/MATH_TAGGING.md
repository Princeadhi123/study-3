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
catch a single case here.** Before trusting `tag_math_distractors.py`'s
`verify` stage on real (unlabeled) data, it's worth re-running
`tag_distractors.py --verify-model <a different model id>` (if Aitta serves
more than one) to see whether a genuinely different model disagrees more
often -- a same-model rerun evidently isn't enough on its own. Until that's
confirmed, treat `needs_human_review.csv` as **the** review queue, not the
`verify` stage's CONFIRM/REPLACE verdicts as ground truth.

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

Then, from `eedi_misconception_tagging/`:

```bash
# elicit -> cluster -> verify -> export, in one go:
python tag_math_distractors.py --stage all

# or step by step, so you can inspect/stop after any stage:
python tag_math_distractors.py --stage elicit --types MATH_DRILLER   # one type at a time
python tag_math_distractors.py --stage elicit                        # remaining types, impact order
python tag_math_distractors.py --stage cluster
python tag_math_distractors.py --stage verify
python tag_math_distractors.py --stage export
```

Every stage writes to `--checkpoint-dir` (default
`misconception_cache/math_tagging/`) as newline-delimited JSON, one line per
completed item, flushed immediately -- safe to Ctrl-C and resume, or
resubmit as a SLURM job after a time-limit kill (see `run_lumi_tagging.sh`).

### Outputs (`--out-dir`, default `misconception_cache/math_tagging_out/`)

- **`distractor_catalog_tagged.csv`** -- the original catalog with
  `misconception_id` / `misconception_label` filled in, plus
  `confidence_margin`, `cluster_size`, `needs_human_review`. Join this back
  onto `kt_interactions_v2_item_level.csv.gz` by
  `(item_id, selected_option_value)` -- no need to re-run any earlier stage.
- **`cluster_summary.csv`** -- one row per discovered misconception,
  sorted by total impact (`times_selected` summed across members). Start
  your sanity check here.
- **`human_review_queue.csv`** -- only the flagged rows (small cluster / low
  confidence margin / missing question context), sorted by impact. This is
  the practical "pass to a human one-by-one" queue -- review the highest-
  impact rows first, since that's where a wrong tag would do the most damage
  once it's feeding a KT model.

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
