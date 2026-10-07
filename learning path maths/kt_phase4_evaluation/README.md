# Phase 4: selected-bank historical evaluation

This separate research workspace consumes the final Phase 2 warm/cold banks
and frozen KT artifacts. It never replaces the demo bank or changes feedback.

Read [PROTOCOL.md](PROTOCOL.md) before inference. The protocol fixes target
eligibility, history/no-history inputs, baselines, metrics, student-cluster
uncertainty and separate item/score conformal targets.

## Current result (2026-10-05)

The final global-bank pair produced 1,012 eligible targets from 531 students.
The completed study is **CPU-reference qualified**, not a strict original
CUDA-reference parity pass. Read
[CPU_REFERENCE_ADDENDUM.md](CPU_REFERENCE_ADDENDUM.md) with the original protocol.

Results and lead-owned checks are retained under
`artifacts/selected_bank_v3_20261005/`:

- [Interpretation](artifacts/selected_bank_v3_20261005/INTERPRETATION.md)
- [Full results](artifacts/selected_bank_v3_20261005/research_results.json)
- [Metric report](artifacts/selected_bank_v3_20261005/research_review.md)
- [Independent result checks](artifacts/selected_bank_v3_20261005/research_result_checks.json)

Warm history KT improves AUC (0.830 versus 0.661) and log loss (0.208 versus
0.252) relative to no history, with paired student-cluster intervals favoring
history. Cold history benefit is inconclusive, with only four incorrect
responses. Warm results are dominated by algebra, not evenly supported topics.

Historical conformal threshold transfer has overall coverage 94.42% warm and
98.68% cold, but incorrect-label coverage is only 61.90% warm and 50% cold
(four cold errors). Do not call the procedure validated from overall coverage.
No genuine selected ten-question blocks or ordered 40-question matches exist.

The original `1e-4` CUDA/cache check failed on 87 targets (max gap 0.00088215).
All-target same-CPU full-input versus blind-input verification is exact.
The original failure is preserved; the precise historical-reference cause is
unresolved. No student advice or pipeline-serving behavior changed.

## Preparation

From this folder:

```powershell
python -m unittest -v test_prepare_replay
python prepare_replay.py --out-dir artifacts/selected_bank_v3_20261005
```

The output folder must not already exist. Preparation streams the original
retained windows to resolve actual target locators and training-only baseline
counts; saved per-question support must reconcile before proceeding. This
is necessary event/block resolution, not a fresh bank-selection search.

## Baselines and paired inference

After preparation, use the existing capture without rescanning:

```powershell
python -m unittest -v test_prepare_replay test_replay_inputs test_metrics test_analysis_gates test_memory_loader
python summarize_replay.py --capture-dir artifacts/selected_bank_v3_20261005 --baseline-only
# Only after sufficient RAM is available and user-owned processes are handled:
python run_inference.py --capture-dir artifacts/selected_bank_v3_20261005
python summarize_replay.py --capture-dir artifacts/selected_bank_v3_20261005
```

Each result refuses overwrites. `baseline_results.json` is frozen and reused
when adding KT results; `research_results.json` adds paired cluster-bootstrap
comparisons and history-only item conformal coverage. Ordinary analysis requires
original historical parity; the explicitly qualified route below instead
preserves that failed check and requires its additional verification/addendum.
The response-blind input builder is tested against current-response
and future-field mutations. No-history singleton predictions are cached by
exact question because their inputs are student-independent; all targets are
still evaluated, not sampled.

`selected_blocks_private.json` is the genuine original-grouping score support
capture. An empty list produces an insufficient-support result, not fabricated
interval evaluation. The current capture contains no such selected blocks.

Generated private files include `targets_private.json`,
`replay_windows_private.jsonl.gz`, `selected_blocks_private.json`,
`training_baselines.json` and `replay_manifest.json`. All stay Git-ignored.
Numeric student codes and window/position locators are still private research
data; no raw identifiers are printed in aggregate logs.

## Execution boundary

Preparation loads no KT weights or embedding vectors. Native inference uses
the strict frozen Phase 2 loader on the captured subset, not all prepared
windows. Do not run it alongside a model-resident demo unless sufficient RAM
is available. Stop a user-owned demo only with explicit permission.

The current machine is CPU-only. A running localhost demo was found to hold
model-scale memory during preflight; inference must wait for sufficient RAM.
Environment observations are not research results or permanent requirements.

`memory_loader.py` shares the original float32 embedding array with Torch
instead of copying it. It inherits the trained dataset's exact encoding and
retains strict Phase 2 loader checks; no Phase 1/2 files are changed. Native
execution requires at least 3 GiB free under this adapter.

An original historical-parity failure is never silently accepted. The qualified
route used for this run is explicit and keeps the failed result:

```powershell
python diagnose_parity.py --capture-dir artifacts/selected_bank_v3_20261005
python verify_cpu_reference.py --capture-dir artifacts/selected_bank_v3_20261005
python summarize_replay.py --capture-dir artifacts/selected_bank_v3_20261005 --allow-cpu-reference-qualified
```

These commands refuse overwrites; the existing diagnostics/results are frozen.
The qualified route requires the recorded addendum and all-target `1e-6`
same-CPU check. It does not raise the original tolerance or claim matched
conformal calibration; it reports historical-threshold sensitivity instead.

The separate historical study does not establish mastery, learning benefit,
calibration for no-history predictions or coverage for the designed fixed
40-question assessment. Any next-question shadow policy is a later, separately
specified experiment, not an automatic consequence of this work.

## Saved Result Plots

`plot_saved_results.py` renders seven figures (PNG and SVG each) from saved
aggregate Phase 1 and Phase 4 results plus the original Phase 2 conformal
reports; no training, inference or model loading is involved and the
existing model and demo are unchanged:

```powershell
python plot_saved_results.py
```

Dependencies are matplotlib and numpy only. Open the gallery at
[artifacts/selected_bank_v3_20261005/plots/index.html](artifacts/selected_bank_v3_20261005/plots/index.html).
`plots/plot_data.json` freezes the exact plotted values and source-result
hashes; rerenders reuse it rather than rederiving data.

All Phase 4 qualifications are retained in the plots: results are CPU-
qualified exploratory evidence (not an original CUDA parity pass), cold
history benefit is inconclusive with only four errors, and there are zero
genuine selected-bank k10 blocks.

## Independence And Paper Readiness

Read [PAPER_READINESS.md](PAPER_READINESS.md) for the completed 2026-10-06 audit and claim boundaries. Phase 4 is an exploratory selected-bank historical reanalysis, not an untouched independent test. All 531 evaluation students have earlier training positions; evaluated target positions themselves are excluded from training loss. All 152 cold targets belong to the cold pool used for joint checkpoint selection. No evaluation student overlaps the active conformal-calibration cohort.

The read-only audit script is `audit_independence.py`. Its frozen aggregate output is [independence_audit_20261006.json](artifacts/selected_bank_v3_20261005/independence_audit_20261006.json). It refuses to overwrite existing audits. No training, inference, recalibration or bank changes accompany this audit.

The opt-in shadow practice code referenced as future work now exists in the
Phase 3 demo; see `../kt_phase3_feedback/FEEDBACK_AND_PRACTICE.md`. A separate
ten-case native synthetic smoke study is now recorded in Phase 3. It does
not change any Phase 4 artifact, historical finding or qualification.
