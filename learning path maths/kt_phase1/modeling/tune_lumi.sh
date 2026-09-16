#!/bin/bash -l
# LUMI hyperparameter sweep across the item-aware KT variants (default:
# skill_item, skill_item_content, skill_item_content_option -- i.e. B, C, D).
#
# Why this exists: everything in run_lumi.sh's ITEM_REG defaults
# (--item-id-dropout 0.25, --item-embed-weight-decay 5e-3) was set by hand,
# one reasoned change at a time (see README "The item-id/cold-item
# tradeoff"), never swept over a grid. Comparing runs/comparison.json against
# lumi_results/runs_previous_20260915_early_stopping/comparison.json shows
# those two knobs are exactly what moved test_cold_item AUC (+0.005 to +0.007
# AUC across variants) while val/test_warm barely moved -- so they are also
# the right knobs to actually search over, instead of guessing further by
# hand. This script trains a small grid of (item_id_dropout,
# item_embed_weight_decay) combinations, one SLURM array task per
# (variant, combination) pair, each pinned to its own GPU.
#
# Why not skill_only (A) too: A has no item_embed at all, so
# --item-id-dropout / --item-embed-weight-decay are literal no-ops for it
# (see train.py's own --help text). Sweeping this grid on A would retrain
# the exact same config 9 times for zero information -- wasted GPU time, not
# a tuning run. B/C/D all have item_embed, so the sweep is meaningful for
# all three, and sweeping all three (not just the current best, D) is the
# right check that a winning combo is a real, transferable fix rather than
# something that only happens to help one variant.
#
# Everything else (architecture, LR schedule, warmup, patience, data prep) is
# held fixed at the values already validated in run_lumi.sh -- this sweep
# isolates the two knobs with actual evidence behind them instead of varying
# everything at once, which would need far more than 27 runs to read
# anything from.
#
# Usage:
#   1. Run run_lumi.sh at least once first (or otherwise ensure
#      ${PREP_V2_DIR}/sequences.jsonl.gz and
#      ${EMBED_DIR}/text_embeddings_v2.npz already exist) -- this script does
#      NOT redo data prep/embedding, it only trains.
#   2. sbatch --account=project_462001308 tune_lumi.sh
#      (or: KT_TUNE_VARIANTS="skill_item_content_option" sbatch ... to sweep
#      only D, e.g. for a fast follow-up/narrower second round)
#   3. After all array tasks finish: python compare_tuning.py
#      --runs-dir <RUNS_TUNE_DIR>/<variant> to rank that variant's configs
#      and pick a winner (run once per variant swept).
#
# Grid: 3 variants x 3 item_id_dropout values x 3 item_embed_weight_decay
# values = 27 combinations -> --array=0-26, one GPU each. Each array task is
# an independent SLURM job (unlike run_lumi.sh's single job backgrounding 4
# processes), so training code stays single-GPU/unchanged -- the "%8" below
# is SLURM's array-throttle: it caps how many of the 27 tasks run *at the
# same time* to 8, matching one LUMI-G node's 8 GCDs, so you get true 8-way
# GPU parallelism without over-requesting beyond what a node actually has.
# The remaining tasks queue automatically and start as soon as a slot frees
# up -- no manual wave-splitting needed. Override at submit time with e.g.
# `sbatch --array=0-26%4 tune_lumi.sh` for less parallelism (lighter on a
# shared allocation) or `%16` if your allocation spans multiple GPU nodes.
#SBATCH --job-name=math_kt_tune
#SBATCH --partition=small-g
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=7
#SBATCH --mem=64G
#SBATCH --time=12:00:00
#SBATCH --array=0-26%8
#SBATCH --output=math_kt_tune_%A_%a.out
#SBATCH --error=math_kt_tune_%A_%a.err
# Do not hard-code your account in this file if you prefer:
# sbatch --account=project_462001308 tune_lumi.sh

set -euo pipefail

PROJECT_ID="${KT_PROJECT_ID:-project_462001308}"
PROJECT_ROOT="${KT_PROJECT_ROOT:-/projappl/${PROJECT_ID}/math_kt}"
SCRATCH_ROOT="${KT_SCRATCH_ROOT:-/scratch/${PROJECT_ID}/math_kt}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CODE_DIR="${KT_CODE_DIR:-${SLURM_SUBMIT_DIR:-${SCRIPT_DIR}}}"
module load LUMI/25.03
module load partition/G
module load cray-python/3.11.7
module load rocm/6.2.4
PYTHON="${KT_PYTHON:-/projappl/project_462001308/math_kt/mathkt-env/bin/python}"

MAX_SEQ_LEN="${KT_MAX_SEQ_LEN:-400}"
CONTEXT_OVERLAP_FRAC="${KT_CONTEXT_OVERLAP_FRAC:-0.25}"
MAX_SESSION_GAP_DAYS="${KT_MAX_SESSION_GAP_DAYS:-30}"
MAX_WINDOWS_PER_STUDENT="${KT_MAX_WINDOWS_PER_STUDENT:-8}"
PREP_V2_DIR="${KT_PREP_V2_DIR:-${SCRATCH_ROOT}/prepared_v2_w${MAX_SEQ_LEN}_o${CONTEXT_OVERLAP_FRAC//./p}_g${MAX_SESSION_GAP_DAYS}_c${MAX_WINDOWS_PER_STUDENT}}"
EMBED_DIR="${PROJECT_ROOT}/embeddings"
RUNS_TUNE_ROOT="${KT_RUNS_TUNE_ROOT:-${PROJECT_ROOT}/runs_tune}"

if [[ ! -f "${PREP_V2_DIR}/sequences.jsonl.gz" ]]; then
    echo "Missing prepared sequences: ${PREP_V2_DIR}/sequences.jsonl.gz"
    echo "Run run_lumi.sh first (it does data prep + embedding once, reused here)."
    exit 2
fi
if [[ ! -x "${PYTHON}" ]]; then
    echo "Python executable not found: ${PYTHON}"
    exit 2
fi

# Variants x 3x3 grid: item_id_dropout x item_embed_weight_decay. skill_only
# is deliberately excluded (see header comment -- both knobs are no-ops for
# it). Dropout/decay values are centered on/around the hand-picked values
# already shown to help (0.25 / 5e-3) so the sweep can tell us whether to
# push further in the same direction or whether those values were already
# close to a local optimum. Override KT_TUNE_VARIANTS (space-separated) to
# sweep a subset, e.g. only D for a narrower follow-up round.
read -ra VARIANTS <<< "${KT_TUNE_VARIANTS:-skill_item skill_item_content skill_item_content_option}"
ITEM_ID_DROPOUTS=(0.20 0.30 0.40)
ITEM_EMBED_WEIGHT_DECAYS=(0.005 0.01 0.02)

n_wd=${#ITEM_EMBED_WEIGHT_DECAYS[@]}
n_dropout=${#ITEM_ID_DROPOUTS[@]}
n_grid=$(( n_dropout * n_wd ))

task_id="${SLURM_ARRAY_TASK_ID:-0}"
variant_idx=$(( task_id / n_grid ))
grid_rem=$(( task_id % n_grid ))
dropout_idx=$(( grid_rem / n_wd ))
wd_idx=$(( grid_rem % n_wd ))

if (( variant_idx >= ${#VARIANTS[@]} )); then
    echo "Task ${task_id} has no corresponding variant (only ${#VARIANTS[@]} in KT_TUNE_VARIANTS=\"${VARIANTS[*]}\")."
    echo "Adjust --array to 0-\$(( ${#VARIANTS[@]} * n_grid - 1 )) for this many variants, or leave --array=0-26 for the default 3 variants."
    exit 0
fi
VARIANT="${VARIANTS[$variant_idx]}"
ITEM_ID_DROPOUT="${ITEM_ID_DROPOUTS[$dropout_idx]}"
ITEM_EMBED_WEIGHT_DECAY="${ITEM_EMBED_WEIGHT_DECAYS[$wd_idx]}"

if [[ "${VARIANT}" == "skill_only" ]]; then
    echo "skill_only has no item_embed -- --item-id-dropout/--item-embed-weight-decay are" \
         "no-ops for it (see train.py --help). Skipping task ${task_id} rather than wasting a GPU-hour."
    exit 0
fi
if [[ "${VARIANT}" != "skill_item" && ! -f "${EMBED_DIR}/text_embeddings_v2.npz" ]]; then
    echo "Missing text embeddings: ${EMBED_DIR}/text_embeddings_v2.npz (needed for ${VARIANT})"
    echo "Run run_lumi.sh first, or embed_questions.py directly."
    exit 2
fi

RUNS_TUNE_DIR="${RUNS_TUNE_ROOT}/${VARIANT}"
RUN_NAME="id${ITEM_ID_DROPOUT}_wd${ITEM_EMBED_WEIGHT_DECAY}"
OUT_DIR="${RUNS_TUNE_DIR}/${RUN_NAME}"
mkdir -p "${OUT_DIR}"

echo "Task ${task_id}: variant=${VARIANT} item_id_dropout=${ITEM_ID_DROPOUT} item_embed_weight_decay=${ITEM_EMBED_WEIGHT_DECAY}"
echo "Output: ${OUT_DIR}"

cd "${CODE_DIR}"

ARGS=(
    --variant "${VARIANT}"
    --sequences "${PREP_V2_DIR}/sequences.jsonl.gz"
    --vocab "${PREP_V2_DIR}/vocab.json"
    --out-dir "${OUT_DIR}"
    --device cuda
    --epochs "${KT_EPOCHS:-150}"
    --patience "${KT_PATIENCE:-25}"
    --warmup-epochs "${KT_WARMUP_EPOCHS:-5}"
    --min-lr "${KT_MIN_LR:-1e-5}"
    --batch-size 256
    --max-seq-len "${MAX_SEQ_LEN}"
    --num-workers 2
    --joint-weight "${KT_JOINT_WEIGHT:-0.5}"
    --item-id-dropout "${ITEM_ID_DROPOUT}"
    --item-embed-weight-decay "${ITEM_EMBED_WEIGHT_DECAY}"
)
if [[ "${VARIANT}" != "skill_only" && "${VARIANT}" != "skill_item" ]]; then
    ARGS+=(--text-embeddings "${EMBED_DIR}/text_embeddings_v2.npz")
fi

"${PYTHON}" train.py "${ARGS[@]}" > "${OUT_DIR}/train.log" 2>&1
echo "Task ${task_id} (${RUN_NAME}) finished. See ${OUT_DIR}/train.log"
