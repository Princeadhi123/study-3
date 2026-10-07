# Phase 4 evidence and paper readiness

Audited 2026-10-06. This is a claim/provenance assessment, not a publication
acceptance prediction. No model, bank, calibration, serving behavior or
previous result was changed.

## Verdict

Phase 4 is an **exploratory selected-bank historical reanalysis**, not an
untouched independent test or external validation. Its results remain useful,
but the paper must separate training-loss holdout, checkpoint-selection
independence, student independence and conformal-calibration independence.
These are different properties.

The proposed question is:

> On systematically selected warm/cold MCQ banks, how do frozen KT
> history/no-history predictions differ, and how do existing conformal
> thresholds behave when transferred to the selected historical subset?

This question does not require mastery estimation or complete assessment
sessions. Whether it is a sufficient novel contribution depends on related
work, the venue and the explanation of what this study adds.

## What the audit establishes

The read-only [audit script](audit_independence.py) checked hashes against
the frozen replay manifest, traced all target locators into the original
prediction dump, reconstructed the calibration partition, checked the saved
joint-selection epoch and scanned all 82,231 retained sequence windows.
The aggregate output is
[independence_audit_20261006.json](artifacts/selected_bank_v3_20261005/independence_audit_20261006.json).
No raw student identifiers are emitted.

| Property | Measured evidence | Interpretation |
|---|---|---|
| Target training-loss holdout | 860 `test_warm` and 152 `test_cold_item` positions; no `train` targets | Evaluated target positions were excluded from training loss |
| Student training overlap | All 531 students have retained `train` positions; warm 423/423, cold 146/146 | Within-student prediction, not unseen-student generalization |
| Student validation overlap | All 531 students also have retained `val` positions | Student separation from development is not established |
| Cold selection reuse | All 152 cold targets belong to the cold pool used by joint checkpoint selection | Cold results are not selection-independent |
| Conformal partition | 13,421 calibration and 13,421 evaluation students; Phase 4 overlaps zero calibration students | Calibration/evaluation student separation is preserved |
| Earlier evaluation reuse | All 1,012 Phase 4 locators occur in the original evaluated prediction dump and its conformal evaluation half | Selected-subset reanalysis, not a new evaluation cohort |
| Replay integrity | Every captured prefix equals the corresponding original prefix; no target has a later `train` position in its own window | No structural mismatch found in the audited replay construction |

Student overlap alone is **not** evidence that KT saw the current answer at
prediction time. Training is masked to `train` positions. The model uses
shifted preceding-response features and causal attention; the retained
same-CPU full/blind verification agrees for all 1,012 targets. These checks
address response leakage, not independence from model selection. No immutable
raw-event ID is available, so the audit does not rule out every possible source
duplicate merely because stored locators are distinct.

## Why cold is a development pool here

The deployed run is `skill_item_content_option`, seed 42,
`best_model_joint.pt`, epoch 45. Its configured selection criterion is:

```text
0.5 * val_auc + 0.5 * test_cold_item_auc
```

The saved best-epoch record and training history reproduce this choice.
The training code evaluates `val`, `test_warm` and `test_cold_item` each
epoch, saves the best joint checkpoint, and early-stops on joint-score
stagnation. `compare_tuning.py` ranks configurations by cold AUC at the
chosen checkpoint, and retained tuning scripts use cold performance to guide
later sweeps.

Thus, "test_cold_item" is a source label, not an untouched-test guarantee.
Describe it as a held-out-item **development/selection pool** in the paper.
Warm test AUC is not a direct term in the joint criterion, but warm metrics
were monitored/reported; the available records cannot establish that they
never influenced informal development decisions.

The audit verifies current source and saved-artifact consistency, not a
cryptographic attestation of the historical training code/environment.
The historical CUDA dump lacks a checkpoint-file hash and exact backend
provenance; its discrepancy must remain disclosed.

## Bank and conformal independence

The bank objective maximizes distinct-student support, not predicted KT
performance or student success rate. That is preferable to selecting
questions because the model looks good on them. However, the support came
from the calibration-excluded evaluation subset later used for replay.
This is availability-informed selection on the evaluation distribution,
not a blindly sampled representative assessment.

Cold item IDs are absent from the trained item vocabulary. This does not
establish unseen content: visible prompts can recur under other IDs, and
skipped cold answers can be preceding inputs for later training predictions.
Do not describe the cold regime as entirely unseen questions or students.

Conformal calibration students were correctly excluded from Phase 4 targets.
That does not undo earlier cold-outcome use in model selection, nor establish
exchangeability for dependent chronological responses and a selected subset.
The reported coverage is an empirical threshold-transfer sensitivity check.
No-history KT has no matching calibration; score coverage is unavailable
because no genuine all-selected ten-question blocks exist.

## Existing topic evidence, not new experiments

These support counts are copied from frozen `research_results.json`.
Student counts across topics are not additive.

| Topic | Warm events / errors | Cold events / errors |
|---|---:|---:|
| Percentages | 32 / 0 | 33 / 0 |
| Divisibility/primes | 32 / 0 | 34 / 1 |
| Combining like terms | 740 / 52 | 63 / 0 |
| Fraction multiplication/quantities | 56 / 11 | 22 / 3 |

Warm pooled history benefit is heavily weighted toward combining like terms.
Its saved paired AUC and loss intervals favor history. Fraction evidence is
mixed: event-weighted loss intervals cross zero, whereas saved student-balanced
loss intervals favor history. Percentages/divisibility warm groups have no
errors, so discrimination AUC is undefined. Small subgroup intervals are not
robust generalization evidence; these are multiple exploratory comparisons,
not a new confirmatory family of tests.

## Claim boundaries

- Supported: the frozen model's history predictions improve pooled warm
  metrics relative to the same targets without history, in this selected
  historical sample and under the CPU-reference qualification.
- Supported: transferred conformal thresholds have 94.42% overall warm
  coverage but only 61.90% incorrect-label coverage in this subset.
- Supported: bank selection is globally support-optimal only within the
  frozen catalogue and declared eligibility/design rules.
- Not supported: independent final cold generalization, calibrated
  no-history probabilities, mastery diagnosis, fixed-assessment coverage,
  or improved learning/practice decisions.
- Inconclusive: cold history benefit, with four incorrect responses.

No result should be described as invalid solely because the same learners
have earlier training responses. The valid target is within-student future
response prediction; the limitations concern broader claims and selection.

## Prioritized next work

1. Keep the current outputs frozen and write the methods/results as a
   retrospective, exploratory study. Rename roles in prose, not source
   split labels or old artifacts. Establish novelty against related work
   before choosing a venue.
2. Inspect available historical-generator provenance and, if feasible,
   specify a matching CUDA rerun. Do not loosen tolerance, tune checkpoints
   on Phase 4, or relabel the failed check. A CPU-only paper must explicitly
   use the qualified evidence and explain the unresolved reference mismatch.
3. If an independent performance claim is required, design a new study with
   an untouched evaluation cohort. Merely repartitioning already inspected
   data cannot make it untouched. New students can evaluate the frozen model;
   a fresh retrospective split requires refitting the entire model-selection
   process without its final test and disclosing prior dataset exposure.
4. Define conformal calibration/evaluation for the intended target and
   predictor with a separate evaluation cohort. Freeze thresholds before
   evaluation. Synthetic cohorts can test assumptions/implementation but
   cannot establish real-student coverage.
5. Reuse the 54 patterns across both banks as an explicitly synthetic
   regression/stress-test appendix, not as a repair for selection reuse.

The immediate research decision is paper scope, not another mastery model or
an indiscriminate larger batch of synthetic outputs.
