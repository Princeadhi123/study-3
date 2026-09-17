#!/bin/bash -l
# LUMI Phase-4 hyperparameter sweep, follow-up to tune_lumi_phase3.sh.
#
# Why this exists: runs_tune3/ (round 3, phases G1-G5) showed:
#   1. Phase G1 (pushing capacity past d192_l3 to d192_l4/d256_l3/d256_l4)
#      is a dead end for test_cold_item: it buys val/warm AUC only, and
#      d256_l3 is strictly WORSE than d192_l3 on cold AUC for both content
#      variants. d192_l3 remains the capacity sweet spot.
#   2. Phase G4 (capacity scaling on skill_item, no content features) barely
#      moved cold AUC (.8898-.8915 vs round-2's 128/2 best of .8905) --
#      confirms the capacity gain is specific to variants with content
#      embeddings to route through the extra layer. Not worth pursuing
#      further for skill_item.
#   3. Phase G5 (general --dropout retuned at d192_l3) gave no improvement
#      over the default 0.2 -- same conclusion as round 2, just confirmed
#      at the new capacity.
#   4. Phase G2 (item_id_dropout / item_embed_weight_decay retuned AT the
#      new d192_l3 capacity) found new single-seed bests for both content
#      variants, beating every previous round:
#        - skill_item_content:        item_embed_wd=0.05   (val .9333 / cold .9121)
#        - skill_item_content_option: item_id_dropout=0.50 (val .9342 / cold .9138)
#      (both at d192_l3, item_id_dropout/wd otherwise unchanged from round 2).
#   5. BUT Phase G3's reseed (43, 44) only re-ran the OLD round-2
#      regularization (id=0.40/wd=0.02 for content, id=0.40/wd=0.005 for
#      content_option) at d192_l3 -- NOT the new G2 winners above. That
#      reseed showed cold AUC dropping from seed 42's .9116/.9128 to
#      .9091-.9092/.9103-.9107 at seeds 43/44 -- a ~0.0024-0.0025 gap,
#      bigger than the ~0.001-0.002 noise band phase D measured at the old
#      128/2 capacity. That noise band applies to the OLD config; the G2
#      winners have never been reseeded at all, so it's still unknown
#      whether their apparent gain over the old config is real or another
#      lucky seed-42 result.
#
# This is the last open question from rounds 1-3 (every other axis --
# width/depth beyond d192_l3, general dropout, skill_item capacity -- has
# already returned a clear, closed-out "no further gain"). This script
# reseeds ONLY the two Phase G2 winners at seeds 43 and 44, so their
# cold-AUC gain over the round-2/Phase-G3 baseline can be judged against
# the same noise band, not just a single seed-42 run:
#   Phase H [idx 0-3]  Reseed (43, 44) x {skill_item_content wd=0.05,
#                       skill_item_content_option id=0.50}, both at d192_l3.
#
# Decision rule once these 4 runs land (see compare_tuning.py output):
#   - If cold AUC at seeds 43/44 stays clearly above the Phase-G3 noise
#     floor (~.909-.911 for content, ~.910-.911 for content_option) for
#     BOTH new configs -> adopt d192_l3 + these G2 regularization values as
#     final, fold into run_lumi.sh defaults, re-run the full A/B/C/D
#     comparison. Tuning is done.
#   - If it collapses into that same noise band -> the G2 "win" was seed
#     luck; fall back to the plain d192_l3 + round-2 regularization
#     (skill_item_content: id=0.40/wd=0.02; skill_item_content_option:
#     id=0.40/wd=0.005), which is still the best CONFIRMED result across
#     3 seeds. Tuning is done either way -- no further phase is indicated
#     by anything measured so far.
#
# SLURM array tasks run in index order; --array=0-3, no %N throttle, so all
# 4 tasks run fully in parallel, each on its own GPU, writing to its own
# independent runs_tune4/<variant>/<run>/ directory.
#
# Usage:
#   1. Run run_lumi.sh (data prep + embeddings), tune_lumi.sh (round 1),
#      tune_lumi_phase2.sh (round 2), and tune_lumi_phase3.sh (round 3)
#      first -- this script does not redo any of them and assumes the same
#      prepared_v2 sequences/vocab/embeddings.
#   2. sbatch --account=project_462001308 tune_lumi_phase4.sh
#   3. After tasks finish: python compare_tuning.py --runs-dir
#      runs_tune4/<variant> per variant, same as rounds 1-3. Compare its
#      by_joint test_cold_item AUC against runs_tune3's idG3_seed43/44
#      (same seeds, old regularization) to judge whether G2's gain holds up.
#
#SBATCH --job-name=math_kt_tune4
#SBATCH --partition=small-g
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=7
#SBATCH --mem=64G
#SBATCH --time=18:00:00
#SBATCH --array=0-3
#SBATCH --output=math_kt_tune4_%A_%a.out
#SBATCH --error=math_kt_tune4_%A_%a.err
# Do not hard-code your account in this file if you prefer:
# sbatch --account=project_462001308 tune_lumi_phase4.sh

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
RUNS_TUNE_ROOT="${KT_RUNS_TUNE_ROOT:-${PROJECT_ROOT}/runs_tune4}"

if [[ ! -f "${PREP_V2_DIR}/sequences.jsonl.gz" ]]; then
    echo "Missing prepared sequences: ${PREP_V2_DIR}/sequences.jsonl.gz"
    echo "Run run_lumi.sh first (it does data prep + embedding once, reused here)."
    exit 2
fi
if [[ ! -x "${PYTHON}" ]]; then
    echo "Python executable not found: ${PYTHON}"
    exit 2
fi

# Flat manifest. Fields (comma-separated, no spaces):
#   variant,item_id_dropout,item_embed_wd,patience,epochs,seed,dropout,d_model,n_layers,run_name
# "-" means "use train.py's default" for that flag (we simply omit the arg).
#
# Round-3 (Phase G2) winners this manifest reseeds (see runs_tune3/<variant>/):
#   skill_item_content:          d192_l3, id=0.40 wd=0.05  (seed 42: val .9333 / cold .9121)
#   skill_item_content_option:   d192_l3, id=0.50 wd=0.005 (seed 42: val .9342 / cold .9138)
MANIFEST=(
  # --- Phase H: reseed (43, 44) each content variant's Phase G2 winner to
  #     get error bars before trusting the regularization-retune gain as
  #     real, the same check Phase G3 already did for the OLD round-2
  #     regularization at this capacity (idx 0-3) ---
  "skill_item_content,0.40,0.05,-,-,43,-,192,3,idH_seed43"
  "skill_item_content,0.40,0.05,-,-,44,-,192,3,idH_seed44"
  "skill_item_content_option,0.50,0.005,-,-,43,-,192,3,idH_seed43"
  "skill_item_content_option,0.50,0.005,-,-,44,-,192,3,idH_seed44"
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
