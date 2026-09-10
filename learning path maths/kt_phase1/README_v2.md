# v2 data pipeline: questions, options, and student answers for every exercise type

## Why this exists

The original pipeline (now in `legacy_v1_item_level/`) kept only the question
text and a 0/1 correctness label; it silently dropped `PossibleAnswersJson`
(the offered options) and `AnswerJson` (what the student actually answered)
for every single row. That made it impossible to build an option-aware or
(eventually) misconception-aware KT model — there was no record of *what
was offered* or *what was picked*, only right/wrong.

v2 fixes this: it re-extracts the raw export so that, for **every** exercise
type, the question, the offered options (when the type has any), and the
student's raw answer are all preserved. See `build_v2_raw_clean.py`'s
docstring for the full type-by-type audit this is based on.

## Pipeline (run in this order)

1. `build_v2_raw_clean.py` — reads the raw export (`../mp_grade_6th_2025_7th_2026_spring.csv.gz`
   or an uncompressed copy at `../mp_grade_6th_2025_7th_2026_spring.csv` if present),
   validates/cleans it, classifies each row into an `exercise_family`, and
   parses `PossibleAnswersJson`/`AnswerJson` into clean option fields where
   the type supports it. Writes `data_v2/kt_interactions_v2.csv.gz` +
   `reports_v2/cleaning_report_v2.json`.
2. `build_v2_sort.py` — external merge sort into chronological
   (student, timestamp, attempt, preorder) order. Writes
   `data_v2/kt_interactions_v2_sorted.csv.gz`.
3. `build_v2_item_level.py` — assigns `item_id`/`item_instance_id` (same
   scheme as v1: `item_id` = independent item slot = `ExerciseId + PreOrd`;
   `item_instance_id` further distinguishes generated question-text variants
   within that slot) and builds the **distractor catalog** (see below).
   Writes `data_v2/kt_interactions_v2_item_level.csv.gz` (the file the
   modeling code actually consumes), `reports_v2/item_summary_v2.csv`, and
   `reports_v2/distractor_catalog.csv`.

Each stage streams the full ~13.9M-row dataset (a few minutes per stage);
none of them require the whole file in memory.

## What "properly extracted" means here, per exercise type

Only 3 of the 54 `ExerciseTypeEnum` values are genuine multiple-choice with
per-option correctness in the source data:

| exercise_type | share of rows | notes |
|---|---|---|
| `MATH_DRILLER` | 30% | 2-4 options, exactly one marked correct |
| `VILLE_QUIZ` | 13% | 1-5 options |
| `VOICE_DRILLER` | tiny | 2-3 options |

Plus one type that offers exactly one "option" (the known-correct string,
not a real choice among distractors):

| exercise_type | share of rows |
|---|---|
| `CROSSWORD_PUZZLE` | 0.8% |

These four (`exercise_family` = `mcq` or `single_answer_text`) get fully
parsed fields:

- `has_options`, `option_count`
- `options_json` — every offered option, `[{"value": ..., "correct": bool}, ...]`
- `correct_option_index` / `correct_option_value`
- `selected_option_index` / `selected_option_value` — which one the student
  actually picked, matched by exact string against `options_json`
  (`selected_option_index = -1` when the raw answer text couldn't be matched
  to any offered option — mostly a formatting/LaTeX-escaping mismatch,
  happens for ~27% of `VILLE_QUIZ` rows; `selected_option_value`, the raw
  text, is still populated in that case)

The other ~50 types (`exercise_family` in `numeric_or_text_entry`,
`matching`, `ordering_sorting`, `spatial`, `classification_selection`,
`time`, `other`) have no discrete option set to parse — but their raw
student answer is **always** preserved verbatim in `answer_raw`, regardless
of family, so nothing is thrown away even where it isn't structurally
decomposed.

**Not every row has a correctness label.** ~4.9% of rows (mostly
`MATCH_PAIRS`, `MATCH_OBJECTS`, `CLASSIFICATION`, `MATSEL`,
`GENERAL_SORTING`, `ARRANGE_SENTENCES`, `MCWI_EXERCISE`, `CARDS_GAME`, ...)
never populate `Correctness` in the source at all. These rows are kept (with
`correctness_available=0`) rather than dropped, so the question/answer is
still available for future work, but the modeling code (`prepare_sequences.py`
/ `prepare_sequences_v2.py`) excludes them from the supervised KT target,
same as v1 implicitly did — a next-step-correctness model needs a known label.

## The distractor catalog: the on-ramp to misconception-aware KT

`reports_v2/distractor_catalog.csv` is a deduplicated list of every distinct
option ever offered for an `mcq`/`single_answer_text` item, with how often
it was offered vs. actually chosen:

```
item_id, exercise_type, option_value, is_correct_option, times_offered, times_selected, selection_rate, misconception_id, misconception_label
```

`misconception_id`/`misconception_label` are intentionally **empty** — this
repo does not invent misconception labels. That's a human (or
LLM-assisted-but-human-reviewed) tagging pass: read `option_value` in
context of the item, decide what conceptual error it represents (e.g. two
distinct wrong answers to the same "do two lines intersect perpendicularly"
question in the catalog clearly encode different misunderstandings), and
fill in an ID/label per distractor. Once that's done, join it back onto
`kt_interactions_v2_item_level.csv.gz` by `(item_id, selected_option_value)`
to get a `misconception_id` per interaction — no need to re-run stages 1-3.
The catalog is pre-sorted by `times_selected` (most-selected wrong answers
first) so tagging effort goes to the distractors that matter most.

## Modeling: content now includes options, plus a 4th option-history variant

`modeling/prepare_sequences_v2.py` builds the same per-student chronological
sequence format as v1's `prepare_sequences.py` (identical windowing/
train-val-test/cold-item scheme — see `modeling/README.md`), from the v2
source, with two additions per event:

- `content_text` — the CURRENT question's full **safe** content: question
  text, plus (only for exercise types that offer discrete options —
  `mcq`/`single_answer_text` families) the offered option VALUES joined
  together, e.g. `"(- 2) . (- 2) . (- 2) . (- 2) [OPTIONS] (-2)4 | -4.2 | -8 | -24"`.
  This is what's embedded for the content-aware variants — see
  `build_content_text()`. It never includes which option is correct or
  which one the student picked (both are supervision-adjacent and would
  leak the label if put in the current-step query).
- `selected_text` — the actual TEXT of what the student selected/answered
  on the *previous* step (e.g. `"-24"`), or the `"[NO_OPTIONS]"` sentinel if
  that exercise type has no discrete options. This is deliberately text, not
  a bare option position/index: an index embedding would force "option 1"
  to mean the same thing on every question, when in reality option 1 is
  `"4"` on one question and `"20"` on another — unrelated. `selected_text`
  is embedded into the exact same multilingual space as `content_text` (by
  `embed_questions.py`), so the model sees what was actually picked.
  (The raw position is still kept per event as `option_idx`, for
  reporting/analysis only — the model itself uses `selected_text`.)

All four model variants (`skill_only`, `skill_item`, `skill_item_content`,
`skill_item_content_option`) now train on this same v2-prepared data, so
they share the exact same item/skill vocab and train/val/test/cold-item
split — a fair, apples-to-apples comparison across all four.

`modeling/model.py`'s variant C, `skill_item_content`, embeds `content_text`
(question + its options, when it has any) as both the previous-step
interaction feature and the current-step query. Variant D,
`skill_item_content_option`, is exactly C plus the frozen embedding of
`selected_text` (via a separate learned projection, `option_proj`, of the
same frozen `content_vectors` table — see `_content_vec` in `model.py`)
added to the **previous**-step interaction only (never the query —
including it in the query would leak the current answer, since "which
option will be selected" is close to the label itself). D is the direct
building block for a future misconception-aware variant: swap the
`selected_text` embedding lookup for a `misconception_embed(prev_misconception_id)`
once the catalog above is tagged.

```bash
cd modeling
python prepare_sequences_v2.py                 # full 13.9M rows, once
python embed_questions.py \
    --sequences prepared_v2/sequences.jsonl.gz \
    --out prepared_v2/text_embeddings_v2.npz     # needed for variants C and D
python train.py --variant skill_item_content \
    --sequences prepared_v2/sequences.jsonl.gz \
    --vocab prepared_v2/vocab.json \
    --text-embeddings prepared_v2/text_embeddings_v2.npz \
    --device cuda --epochs 30
python train.py --variant skill_item_content_option \
    --sequences prepared_v2/sequences.jsonl.gz \
    --vocab prepared_v2/vocab.json \
    --text-embeddings prepared_v2/text_embeddings_v2.npz \
    --device cuda --epochs 30
```

`compare_runs.py` and `run_lumi.sh` already know about all four variants
running on the shared v2 data.

## On Finnish text and LUMI (no translation needed)

The content-aware variants (`skill_item_content`, `skill_item_content_option`)
already embed the raw Finnish/DSL question text (plus offered options, see
above) directly with a frozen multilingual sentence-transformer
(`embed_questions.py`, default `paraphrase-multilingual-MiniLM-L12-v2`).
Multilingual sentence-transformers are trained so that semantically
equivalent text in different languages lands in a shared embedding space —
translating Finnish to English first would only add a failure point
(translation errors, loss of math notation) for no benefit to the model;
translation is only useful later for human-readable reporting, not for
training.

Where LUMI actually helps:
- Training the transformer KT models at full scale on GPU (`run_lumi.sh`
  already parallelizes all four variants across GPUs on one node).
- Optionally swapping in a stronger multilingual embedding model (e.g.
  `intfloat/multilingual-e5-large` or `LaBSE`) for variants C/D, since
  embedding 13M rows' worth of unique question+option contents is cheap
  compute-wise but a bigger model needs more VRAM/throughput than a laptop.
- Later, an LLM-assisted first pass over `distractor_catalog.csv` to
  *suggest* misconception groupings for a human to review — still not a
  fully automatic misconception-tagging pipeline, but LUMI (or any
  GPU/API-connected environment) can make the human tagging pass faster.

## Folder layout

```
kt_phase1/
  legacy_v1_item_level/     old pipeline (question+correctness only, no options/answers)
                            -- kept for reference; no longer used by modeling/
  build_v2_raw_clean.py     stage 1
  build_v2_sort.py          stage 2
  build_v2_item_level.py    stage 3
  data_v2/                  stage outputs (kt_interactions_v2*.csv.gz + samples)
  reports_v2/                cleaning/item-level reports + distractor_catalog.csv
  modeling/
    prepare_sequences_v2.py  v2 sequence builder (adds content_text + option_idx);
                              used by ALL FOUR variants now, for a fair comparison
    embed_questions.py        embeds content_text (question + options), used by C/D
    dataset.py                emits content_idx (looked up via content_text) and
                               option_idx tensors
    model.py                  skill_only / skill_item / skill_item_content /
                               skill_item_content_option, all sharing KTTransformer
    train.py, compare_runs.py, run_lumi.sh   updated for the 4-way v2-only setup
```
