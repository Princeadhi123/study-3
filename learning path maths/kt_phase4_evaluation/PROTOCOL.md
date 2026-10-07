# Selected-bank KT and conformal evaluation protocol

Protocol version: `selected_bank_v3_replay_v1`, authored 2026-10-05 before
new inference. This is exploratory retrospective evaluation, not a prospective
fixed-assessment validation or an untouched final test.

## Frozen inputs and scope

Use the Phase 2 banks in `evaluation_banks_support_optimal_20261005/`,
version `3_support_optimal_under_declared_rules_20261005`, and their recorded
certificate hashes. Do not change questions, topics, weights or the checkpoint
after seeing results. The model is variant D, `best_model_joint.pt`, seed 42,
with its trained architecture, vocabulary, embedding table and time features.

No training, calibration changes, student-advice decisions or provider calls.
All generated outputs stay in this folder's Git-ignored `artifacts/`.
No raw student identifiers appear in aggregate reports; replay files retain
private numeric student/window locators and observed answer histories.

## Target construction

1. Reconstruct the active k10 conformal calibration/evaluation partition using
   the complete prediction dump's first-seen base-student order and recorded
   calibration seed/fraction. Exclude every active calibration student.
2. Use only stored `test_warm` / `test_cold_item` targets in retained prepared
   windows. Match the bank's item instance, actual skill index and byte-exact
   content including ordered options. Check source item ID and vocabulary regime.
3. Retain all matched target occurrences as the primary sample, including
   repeated attempts. Window locators are not independent student samples.
   Reconcile per-question support with the saved selected review before inference.
4. Do not deduplicate apparent timestamp collisions without an immutable source
   event ID. Record counts, and use first retained occurrence per base student
   and exact bank question as a secondary repeated-attempt sensitivity analysis.
5. Preceding history is only the strictly earlier events in the same retained
   window. It includes prior context, validation/test and skipped-cold events
   as observed history; it is not complete lifetime history. Do not carry state
   across windows or invent a 40-question assessment.

## KT 2 x 2 and leakage boundary

Within each warm/cold regime, use identical target events for:

- **History:** retained prefix through the query, using only preceding
  responses for interaction features.
- **No history:** the same target query as a singleton at position zero,
  using the model's learned start token.

Before each forward pass, replace the target's post-response fields
(correctness, selected-answer index, response-time value/mask, attempt and
time-bin interaction inputs) with neutral placeholders. Keep target skill,
item and question/option content unchanged. Remove all subsequent events.
The true label is stored separately for scoring, never used as current input.
Earlier observed answers remain valid inputs to later predictions.

Preserve training-time feature encoding, causal masking, right padding and
positional semantics. Require target content/option embedding membership.
Report any original dataset fallback-to-row-zero behavior in preceding history;
do not silently change the frozen feature construction. Zero-history targets
remain paired but cannot establish a benefit from absent history.

Use CPU inference with fixed weights, eval mode and no gradients. Runtime
batch size 8 and two CPU threads are resource settings, not sampling rules.
No target subsampling. Compare history replay probabilities with the original
dump at the exact window/position as a numerical parity check (absolute
tolerance `1e-4`); do not substitute cached probabilities for new predictions.
Investigate parity failures before treating new outputs as valid.

## Predefined baselines

Fit counts only on retained events whose split is exactly `train`;
exclude `context`, `val`, `test_*` and `skip_cold_in_train` as fitting targets.
Counts are stored-training-event counts, not all original training interactions.

- Global training correctness: Laplace smoothing `(correct + 1)/(n + 2)`.
- Skill training correctness: the same smoothing; if absent, use global prior.
- Exact-rendering training correctness: same smoothing for source item instance,
  actual skill and ordered content; if absent, use the skill prior.
- History skill rate: `(preceding_same_skill_correct + 2 * skill_prior) /
  (preceding_same_skill_n + 2)`. With no history it equals the skill prior.

No evaluation labels fit a baseline. Preceding labels can update the history
baseline at prediction time because they are already observed. Do not tune
smoothing, choose a best-performing baseline or select items after inference.

## Metrics and uncertainty

Primary metrics: ROC AUC, log loss and Brier score on all eligible target
occurrences, separately by regime and history condition. AUC is undefined
with only one observed class; return null and an explicit reason, not zero.
Clip probabilities only inside log-loss calculation to `[1e-7, 1-1e-7]`.
Report event/student/question counts and correct/incorrect support.

Also report each skill, first-occurrence sensitivity, and student-balanced
log loss/Brier (equal weight per student, not per event). Calibration uses
ten fixed equal-width probability bins, observed/predicted rates and ECE.
Empty bins remain empty; this is descriptive, not a significance test.

Student-cluster bootstrap: 2,000 draws, seed `20261005`, sampling base students
with replacement within each reported regime/subgroup and retaining all their
events. Use paired draws for history-minus-no-history differences. Report
percentile 95% intervals for metrics and paired differences, valid draw counts
and undefined AUC draws. Suppress intervals when fewer than two students or
fewer than 100 valid draws are available; small-sample intervals are not proof
of reliable precision. Never treat question-level student counts as additive.

Warm versus cold is descriptive, not randomized or difficulty-controlled.
Checkpoint selection used validation/cold-item performance, and evaluation
students may overlap training. State these limitations with all results.

## Conformal target and matching

Primary conformal target: **individual binary correctness**, using the frozen
active k10 artifact's existing item-level Mondrian label-conditional sets,
alpha 0.10. Apply only to the history replay matching the historical predictor
construction. Report empirical event coverage, label-conditional coverage,
student-balanced coverage, set sizes, singleton/ambiguous/empty proportions,
and student-cluster uncertainty.

This is a selected-subset empirical check under dependent chronological data,
not a new exchangeability or fixed-bank coverage guarantee. The no-history
predictor has not received matching calibration: do not report its sets as
calibrated or reuse the history procedure's guarantee.

Secondary target: a realized ten-response skill rate for **genuine historical
blocks**, if supported. Form non-overlapping k=10 groups from ALL evaluation
test targets of each skill inside each original window, in target-position
order, exactly as active historical calibration does. Filter for selected-bank
membership only AFTER forming blocks; never regroup selected answers.

Audit warm-only, cold-only and mixed support, distinct-question counts and
whether the question set matches a bank's ten-question skill set. Any-cold
blocks use the historical cold quantile. Evaluate only completely selected
blocks using prequential history probabilities. These probabilities may use
earlier responses within the block; the interval is not a pre-assessment
forecast made before all ten answers. Empty support yields an explicit
limitation, not manufactured blocks or a coverage metric.

Also count contiguous ordered 40-question matches in retained windows as an
availability audit. Even a match alone would not establish intended assessment
administration, deployment equivalence or educational validity.

## Interpretation and later decisions

Report all predefined comparisons, including inconclusive findings. No fixed
"success" threshold is introduced after observing results. Predictive value,
probability calibration, coverage usefulness and deployment fit are separate.

If evidence warrants further work, write one practice-selection policy and
shadow comparison separately, after suitable practice content exists. Do not
change student feedback, infer mastery, automatically advance students or claim
learning gains from retrospective prediction metrics. Feedback development
can proceed independently.
