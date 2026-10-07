# CPU-reference qualification before performance analysis

Addendum version: `selected_bank_v3_replay_v1_cpu_reference_addendum1`.
Authored 2026-10-05 after numerical diagnostics and before KT performance
metrics or conformal coverage were inspected/computed.

## Observed verification deviation

The original protocol required absolute agreement with the historical dump
within `1e-4`. The completed CPU replay failed that check on 87 of 1,012
targets; the maximum gap was 0.0008821487426757812. The original failed
`inference_report.json`, predictions, logs and protocol are retained unchanged.
This addendum does not raise that tolerance or mark the check as passed.

The historical dump records CUDA execution, but does not record its exact
PyTorch/kernel environment or checkpoint-file hash. Current CPU full inputs,
response-blind inputs and altered current/future-response inputs agree on
the twelve largest-gap diagnostic cases; changing CPU fastpath/kernel settings
changes probabilities by less than the cross-reference discrepancy.

This identifies a historical-reference mismatch, not its precise cause.
CPU/CUDA numerical differences are a candidate explanation; missing historical
implementation/provenance details cannot be ruled out. Do not claim a specific
precision mode, GPU bug or proven bitwise identity with the original generator.

## Additional independent input-construction gate

Before summarizing CPU results, score all 540 captured windows using the
original trained dataset's unmodified input encoding on the same frozen CPU
model. Compare every selected target probability with its previously computed
response-blind CPU replay. Require absolute agreement within `1e-6`.

This is a tighter **same-CPU full-input versus blind-input** check, not a
replacement pass for the failed CUDA/reference comparison. Bind it to the
saved predictions, checkpoint, embedding file, capture and diagnostic hashes.
Failure means stop; do not exclude targets, tune a tolerance or regenerate
predictions to improve performance.

The shared-embedding loader changes storage ownership only: exact float32
vectors and inherited trained-dataset encoding are reused, with the original
strict checkpoint/dimension checks. Its unit tests compare all encoded fields
and vector values against the copied-storage implementation.

## Permitted exploratory outputs

If the same-CPU gate passes:

- Report fresh CPU history/no-history predictions on the original identical
  targets, preserving every predefined baseline, metric, smoothing constant,
  cluster-bootstrap seed/draw count and sensitivity analysis.
- Label results `historical_replay_complete_cpu_reference_qualified`, not a
  strict original-protocol pass or confirmation of historical CUDA parity.
- Reuse frozen baseline statistics, not refit or rerun their derivation.
- State the original failed check and unresolved historical-reference cause
  with the results. No targets, questions, labels or checkpoints are changed.

For conformal prediction, applying the historical thresholds to current CPU
history probabilities is **empirical threshold-transfer sensitivity analysis**.
Do not claim a matching new calibration guarantee. Compare prediction-set
membership with sets from the original cached history probabilities using the
same thresholds and targets; report any changes and label-conditional support.
No-history remains uncalibrated. Genuine selected score-block support is zero,
so no score-coverage result is created.

Full historical generator reproduction or matching CPU calibration would
require a separately specified next study. This addendum does not authorize
student advice, mastery claims, learning-effect claims or automatic progression.
