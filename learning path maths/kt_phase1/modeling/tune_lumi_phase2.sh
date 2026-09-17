#!/bin/bash -l
# LUMI Phase-2 hyperparameter sweep, follow-up to tune_lumi.sh.
#
# Why this exists: tune_lumi.sh's 27-run grid (item_id_dropout in
# {0.20,0.30,0.40} x item_embed_weight_decay in {0.005,0.01,0.02}, see
# runs_tune/) showed:
#   1. item_id_dropout is the dominant, still-improving knob at the grid's
#      upper edge (0.40) for every variant -- skill_item in particular is
#      still ~linear (no sign of turning over), while the content variants
#      are flattening but not proven to have peaked.
#   2. item_embed_weight_decay's effect (~0.0005-0.002 AUC) is inside the
#      epoch-to-epoch noise band observed in the plateau region of training,
#      and the tested range (0.005-0.02) is narrow -- no real signal either
#      way yet.
#   3. Every run early-stopped on --patience before the cosine LR schedule
#      (sized for --epochs 150) reached --min-lr, so the low-LR "polish"
#      phase of annealing was never actually exercised.
#   4. Everything so far is single-seed (42) -- differences between
#      neighboring configs are often smaller than that seed's own noise.
#
# This script queues a single flat, PRIORITY-ORDERED task list (highest
# priority = lowest array index) covering, in order:
#   Phase A [idx 0-15]  Extend item_id_dropout past 0.40 (the highest-value,
#                       still-open question from round 1).
#   Phase B [idx 16-18] Re-run each variant's current round-1 best config
#                       with --patience raised so the cosine schedule can
#                       finish annealing to --min-lr before stopping, to
#                       check whether the observed plateau is real or just
#                       LR-starved.
#   Phase C [idx 19-27] Widen item_embed_weight_decay to a real range
#                       (0.001 and 0.05/0.1) at each variant's best-so-far
#                       item_id_dropout, since 0.005-0.02 showed no signal.
#   Phase D [idx 28-33] Reseed each variant's round-1 best config (seeds
#                       43, 44) to get error bars before trusting any
#                       "winner" among near-tied configs.
#   Phase E [idx 34-39] Sweep the general --dropout (0.3, 0.4) at each
#                       variant's best item_id_dropout -- untested
#                       interaction so far.
#   Phase F [idx 40-43] Capacity scaling (d_model/n_layers) for the two
#                       content-aware variants only, at their best
#                       regularization setting -- richer inputs may support
#                       a bigger model now that item-embedding overfitting
#                       is under control.
#
# SLURM array tasks run in index order and are throttled (not reordered) by
# the %N suffix, so this ordering *is* the priority: if you cut the sweep
# short (e.g. --array=0-18) you still get the two highest-value phases done.
#
# Usage:
#   1. Run run_lumi.sh (data prep + embeddings) and tune_lumi.sh (round 1)
#      first -- this script does not redo either.
#   2. sbatch --account=project_462001308 tune_lumi_phase2.sh
#      (defaults to the full 0-43 array; override with e.g.
#      `sbatch --array=0-15%8 tune_lumi_phase2.sh` to run only Phase A)
#   3. After tasks finish: python compare_tuning.py --runs-dir
#      <RUNS_TUNE_DIR>/<variant> per variant, same as round 1.
#
#SBATCH --job-name=math_kt_tune2
#SBATCH --partition=small-g
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=7
#SBATCH --mem=64G
#SBATCH --time=18:00:00
#SBATCH --array=0-43%16
#SBATCH --output=math_kt_tune2_%A_%a.out
#SBATCH --error=math_kt_tune2_%A_%a.err
# Do not hard-code your account in this file if you prefer:
# sbatch --account=project_462001308 tune_lumi_phase2.sh

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
RUNS_TUNE_ROOT="${KT_RUNS_TUNE_ROOT:-${PROJECT_ROOT}/runs_tune2}"

if [[ ! -f "${PREP_V2_DIR}/sequences.jsonl.gz" ]]; then
    echo "Missing prepared sequences: ${PREP_V2_DIR}/sequences.jsonl.gz"
    echo "Run run_lumi.sh first (it does data prep + embedding once, reused here)."
    exit 2
fi
if [[ ! -x "${PYTHON}" ]]; then
    echo "Python executable not found: ${PYTHON}"
    exit 2
fi

# Flat, priority-ordered manifest. Fields (comma-separated, no spaces):
#   variant,item_id_dropout,item_embed_wd,patience,epochs,seed,dropout,d_model,n_layers,run_name
# "-" means "use train.py's default" for that flag (we simply omit the arg).
#
# Round-1 bests this manifest builds on (see runs_tune/<variant>/):
#   skill_item:                  id_dropout=0.40 wd=0.005  (val .9276 / cold .888)
#   skill_item_content:          id_dropout=0.40 wd=0.02   (val .9321 / cold .9097)
#   skill_item_content_option:   id_dropout=0.40 wd=0.005  (val .9325 / cold .9091)
MANIFEST=(
  # --- Phase A: extend item_id_dropout past 0.40 (idx 0-15) ---
  "skill_item,0.45,0.005,-,-,-,-,-,-,idA0.45_wd0.005"
  "skill_item,0.45,0.01,-,-,-,-,-,-,idA0.45_wd0.01"
  "skill_item,0.50,0.005,-,-,-,-,-,-,idA0.50_wd0.005"
  "skill_item,0.50,0.01,-,-,-,-,-,-,idA0.50_wd0.01"
  "skill_item,0.55,0.005,-,-,-,-,-,-,idA0.55_wd0.005"
  "skill_item,0.55,0.01,-,-,-,-,-,-,idA0.55_wd0.01"
  "skill_item,0.60,0.005,-,-,-,-,-,-,idA0.60_wd0.005"
  "skill_item,0.60,0.01,-,-,-,-,-,-,idA0.60_wd0.01"
  "skill_item_content,0.45,0.01,-,-,-,-,-,-,idA0.45_wd0.01"
  "skill_item_content,0.45,0.02,-,-,-,-,-,-,idA0.45_wd0.02"
  "skill_item_content,0.50,0.01,-,-,-,-,-,-,idA0.50_wd0.01"
  "skill_item_content,0.50,0.02,-,-,-,-,-,-,idA0.50_wd0.02"
  "skill_item_content_option,0.45,0.005,-,-,-,-,-,-,idA0.45_wd0.005"
  "skill_item_content_option,0.45,0.01,-,-,-,-,-,-,idA0.45_wd0.01"
  "skill_item_content_option,0.50,0.005,-,-,-,-,-,-,idA0.50_wd0.005"
  "skill_item_content_option,0.50,0.01,-,-,-,-,-,-,idA0.50_wd0.01"

  # --- Phase B: same as round-1 best but --patience raised so the cosine
  #     schedule (T_max tied to --epochs) can actually reach --min-lr
  #     before early stopping (idx 16-18) ---
  "skill_item,0.40,0.005,45,-,-,-,-,-,idB_patience45"
  "skill_item_content,0.40,0.02,45,-,-,-,-,-,idB_patience45"
  "skill_item_content_option,0.40,0.005,45,-,-,-,-,-,idB_patience45"

  # --- Phase C: widen item_embed_weight_decay to a real range at each
  #     variant's best-so-far item_id_dropout (0.40); 0.005-0.02 showed no
  #     signal above noise so test outside that band (idx 19-27) ---
  "skill_item,0.40,0.001,-,-,-,-,-,-,idC_wd0.001"
  "skill_item,0.40,0.05,-,-,-,-,-,-,idC_wd0.05"
  "skill_item,0.40,0.1,-,-,-,-,-,-,idC_wd0.1"
  "skill_item_content,0.40,0.001,-,-,-,-,-,-,idC_wd0.001"
  "skill_item_content,0.40,0.05,-,-,-,-,-,-,idC_wd0.05"
  "skill_item_content,0.40,0.1,-,-,-,-,-,-,idC_wd0.1"
  "skill_item_content_option,0.40,0.001,-,-,-,-,-,-,idC_wd0.001"
  "skill_item_content_option,0.40,0.05,-,-,-,-,-,-,idC_wd0.05"
  "skill_item_content_option,0.40,0.1,-,-,-,-,-,-,idC_wd0.1"

  # --- Phase D: reseed each variant's round-1 best config to get error
  #     bars before trusting any near-tied "winner" (idx 28-33) ---
  "skill_item,0.40,0.005,-,-,43,-,-,-,idD_seed43"
  "skill_item,0.40,0.005,-,-,44,-,-,-,idD_seed44"
  "skill_item_content,0.40,0.02,-,-,43,-,-,-,idD_seed43"
  "skill_item_content,0.40,0.02,-,-,44,-,-,-,idD_seed44"
  "skill_item_content_option,0.40,0.005,-,-,43,-,-,-,idD_seed43"
  "skill_item_content_option,0.40,0.005,-,-,44,-,-,-,idD_seed44"

  # --- Phase E: general --dropout sweep at each variant's best
  #     item_id_dropout -- untested interaction so far (idx 34-39) ---
  "skill_item,0.40,0.005,-,-,-,0.3,-,-,idE_dropout0.3"
  "skill_item,0.40,0.005,-,-,-,0.4,-,-,idE_dropout0.4"
  "skill_item_content,0.40,0.02,-,-,-,0.3,-,-,idE_dropout0.3"
  "skill_item_content,0.40,0.02,-,-,-,0.4,-,-,idE_dropout0.4"
  "skill_item_content_option,0.40,0.005,-,-,-,0.3,-,-,idE_dropout0.3"
  "skill_item_content_option,0.40,0.005,-,-,-,0.4,-,-,idE_dropout0.4"

  # --- Phase F: capacity scaling for content-aware variants only, at
  #     their best regularization setting (idx 40-43) ---
  "skill_item_content,0.40,0.02,-,-,-,-,192,2,idF_d192_l2"
  "skill_item_content,0.40,0.02,-,-,-,-,192,3,idF_d192_l3"
  "skill_item_content_option,0.40,0.005,-,-,-,-,192,2,idF_d192_l2"
  "skill_item_content_option,0.40,0.005,-,-,-,-,192,3,idF_d192_l3"
)

task_id="${SLURM_ARRAY_TASK_ID:-0}"
if (( task_id >= ${#MANIFEST[@]} )); then
    echo "Task ${task_id} has no manifest entry (only ${#MANIFEST[@]} entries, 0-$(( ${#MANIFEST[@]} - 1 )))."
    exit 0
fi

IFS=',' read -r VARIANT ITEM_ID_DROPOUT ITEM_EMBED_WD PATIENCE EPOCHS SEED DROPOUT D_MODEL N_LAYERS RUN_NAME <<< "${MANIFEST[$task_id]}"

if [[ "${VARIANT}" != "skill_item" && ! -f "${EMBED_DIR}/text_embeddings_v2.npz" ]]; then
    echo "Missing text embeddings: ${EMBED_DIR}/text_embeddings_v2.npz (needed for ${VARIANT})"
    echo "Run run_lumi.sh first, or embed_questions.py directly."
    exit 2
fi

RUNS_TUNE_DIR="${RUNS_TUNE_ROOT}/${VARIANT}"
OUT_DIR="${RUNS_TUNE_DIR}/${RUN_NAME}"
mkdir -p "${OUT_DIR}"

echo "Task ${task_id}: variant=${VARIANT} item_id_dropout=${ITEM_ID_DROPOUT} item_embed_wd=${ITEM_EMBED_WD} patience=${PATIENCE} epochs=${EPOCHS} seed=${SEED} dropout=${DROPOUT} d_model=${D_MODEL} n_layers=${N_LAYERS}"
echo "Output: ${OUT_DIR}"

cd "${CODE_DIR}"

ARGS=(
    --variant "${VARIANT}"
    --sequences "${PREP_V2_DIR}/sequences.jsonl.gz"
    --vocab "${PREP_V2_DIR}/vocab.json"
    --out-dir "${OUT_DIR}"
    --device cuda
    --epochs "$([[ "${EPOCHS}" == "-" ]] && echo "${KT_EPOCHS:-150}" || echo "${EPOCHS}")"
    --patience "$([[ "${PATIENCE}" == "-" ]] && echo "${KT_PATIENCE:-25}" || echo "${PATIENCE}")"
    --warmup-epochs "${KT_WARMUP_EPOCHS:-5}"
    --min-lr "${KT_MIN_LR:-1e-5}"
    --batch-size 256
    --max-seq-len "${MAX_SEQ_LEN}"
    --num-workers 2
    --joint-weight "${KT_JOINT_WEIGHT:-0.5}"
    --item-id-dropout "${ITEM_ID_DROPOUT}"
    --item-embed-weight-decay "${ITEM_EMBED_WD}"
)
[[ "${SEED}" != "-" ]] && ARGS+=(--seed "${SEED}")
[[ "${DROPOUT}" != "-" ]] && ARGS+=(--dropout "${DROPOUT}")
[[ "${D_MODEL}" != "-" ]] && ARGS+=(--d-model "${D_MODEL}")
[[ "${N_LAYERS}" != "-" ]] && ARGS+=(--n-layers "${N_LAYERS}")
if [[ "${VARIANT}" != "skill_only" && "${VARIANT}" != "skill_item" ]]; then
    ARGS+=(--text-embeddings "${EMBED_DIR}/text_embeddings_v2.npz")
fi

"${PYTHON}" train.py "${ARGS[@]}" > "${OUT_DIR}/train.log" 2>&1
echo "Task ${task_id} (${VARIANT}/${RUN_NAME}) finished. See ${OUT_DIR}/train.log"
