# Endpoint shadow practice smoke protocol

Version `endpoint_shadow_smoke_v1_20261006`. Authored before native predictions.
This is a ten-case synthetic software/behavior smoke test, not the proposed
108-run study, an independent predictive evaluation, or learner delivery.
No UI change, hosted provider call, training or conformal gate is authorized.

## Frozen content and policy

Use the final support-optimal warm/cold banks without changing any question,
order, source protocol or review/approval field. An explicit research-only
scoring seam validates their structure and grades synthetic responses; the
ordinary serving scorer continues rejecting non-approved research banks.
Each research taxonomy groups questions at the existing source-topic level.
These are descriptive groupings, not new prerequisite or misconception labels.

Build one shared private pool of 20 questions from the retained closed review:
six percentage, two divisibility/prime, six combining-like-terms and six fraction
questions. Two divisibility prompts is a content-capacity constraint, not padding.
Exclude IDs, normalized prompts AND source item IDs from both assessment banks
and the current demo. Require accepted mathematical checks, saved exact
embedding rows and frozen-vocabulary consistency. Within each topic, sort by
descending distinct-student support, then question ID; retain the first distinct
prompts up to quota. Do not use success rates, KT predictions or latency for
selection. This reuses historical availability, not independent data.

Freeze inclusive probability band `[0.60, 0.80]`, midpoint `0.70`, as an
experimental heuristic. Do not tune it after observing results. Preserve the
implemented policy: observed-error topics only; prioritize topic incorrect
count, then error fraction, then closeness to midpoint, then question ID.
The no-KT baseline uses the same eligibility and error priorities, followed
by question ID. No candidate qualifies means abstention, not a forced baseline
recommendation. These probabilities do not establish ZPD or mastery.

## Cases

Run each of five patterns on each bank:

1. All correct.
2. All incorrect.
3. Only the source fraction topic incorrect.
4. Alternating correct/incorrect positions globally.
5. Stable strong, independent simulated answer probability 0.80, seed 11,
   using the existing response generator and uniform wrong-option choices.

Deterministic wrong choices use answer-index plus one modulo option count.
No real prior student history is attached. Each candidate sees the same forty
observed synthetic responses plus one unanswered query. Candidate answers are
not accumulated across alternatives. Time/attempt placeholders follow the
existing adapter assumptions. The two banks are not difficulty-matched; these
are paired pattern labels, not independent students.

## Execution and controls

Load frozen variant D/epoch-45 weights on CPU, eval mode, no gradients, using
the already tested Phase 4 shared-embedding loader and retained replay-window
file only as loader schema input. Those real windows are NOT synthetic history.
Verify checkpoint, config, vocabulary and full embedding-file hashes against
the retained Phase 4 inputs before inference. Use two CPU threads and require
at least 3 GiB available RAM; do not terminate user processes to obtain it.

For each eligible case, compare one adapter candidate prediction with the
original batch builder using changed current response fields and an appended
future event. Require maximum absolute gap <= `1e-6`. Failure remains a
failure; do not relax tolerance, drop cases or regenerate favorable predictions.
Also check original deterministic feedback is identical before/after selection.

Separate fault controls use an empty pool and injected all-1.0 probabilities
to check abstention. They are not native model performance observations and
do not contribute to the ten-case selection/abstention counts.

Record each case's inputs/hashes, observed counts, native candidate
probabilities, exclusions, selection and baseline, and post-load elapsed
recommendation time. Loading/hashing are reported separately. Timing is an
unwarmed first-pass measurement, not a deployment latency benchmark.
Native unavailable results fail the smoke gate; preserve them rather than
counting them as policy abstentions. All-correct should abstain without model
queries. Validate chosen IDs, error-topic eligibility, band inclusion and
privacy flags. No ten-case AUC, learning benefit or conformal coverage claim.

## Outputs and interpretation

`artifacts/shadow_smoke_20261006/` contains frozen banks, pool, provenance,
taxonomies, five-pattern inputs per regime and a hash-bound preparation manifest.
Native results and aggregate review are separate outputs, refusing overwrite.
Original Phase 4 outputs and the demo bank remain unchanged.

Report selected/abstained/unavailable case counts and reasons separately by
bank, candidate counts and baseline agreement among selected cases. These are
descriptive synthetic behavior counts, not statistical power, independent
student evidence or recommendation effectiveness.

A passing smoke test allows consideration of a separately frozen broader
study. It does not switch on student recommendations or complete the 108 runs.
