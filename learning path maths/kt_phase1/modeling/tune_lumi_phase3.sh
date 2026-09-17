#!/bin/bash -l
# LUMI Phase-3 hyperparameter sweep, follow-up to tune_lumi_phase2.sh.
#
# Why this exists: comparing runs_tune/ (round 1) and runs_tune2/ (round 2,
# phases A-F) showed:
#   1. item_id_dropout, item_embed_weight_decay, and general --dropout are
#      all SATURATED at the (d_model=128, n_layers=2) capacity used in
#      rounds 1-2:
#        - skill_item: item_id_dropout 0.40->0.60 (phase A) bought only
#          +0.0005 val AUC / +0.003 cold AUC, and 0.55->0.60 moved almost
#          nothing. Weight decay outside 0.005-0.02 (phase C) was strictly
#          worse. General dropout != 0.2 (phase E) was strictly worse.
#        - content variants: phases A/C/D/E all landed within the seed
#          noise band measured in phase D (~0.001 val AUC, ~0.001-0.002
#          cold AUC between seeds 42/43/44 at the same config).
#   2. The ONE result that clearly broke out of that noise band was Phase F
#      (capacity scaling) for the content-aware variants:
#        - skill_item_content      d192_l3: val .9340 / cold .9116
#          (round-1 best: val .9329 / cold .9097)
#        - skill_item_content_option d192_l3: val .9342 / cold .9128
#          (round-1 best: val .9325 / cold .9091)
#      d192_l2 (wider but not deeper) did NOT help -- the gain is
#      specifically from depth (n_layers 2->3), not width alone. This is
#      the best result across both rounds 1-2 for every metric.
#   3. Phase F used round-1's regularization (item_id_dropout/item_embed_wd
#      tuned for the OLD 128/2 capacity) unchanged -- a bigger model may
#      overfit item embeddings differently, so that pairing is untested.
#   4. Phase F's d192_l3 winners are single-seed (42) -- given the ~0.001
#      noise band from phase D, this needs error bars before being trusted
#      as a real (not lucky-seed) win.
#   5. Capacity scaling was only tried for the content-aware variants;
#      whether skill_item (no content features) also benefits from a
#      deeper transformer, independent of content, is untested.
#
# This script queues a single flat, PRIORITY-ORDERED task list covering,
# in order:
#   Phase G1 [idx 0-5]   Push capacity further for the two content variants
#                        (d192_l4, d256_l3, d256_l4) to see whether the
#                        l2->l3 gain continues with more depth/width, or
#                        whether l3 was a local sweet spot.
#   Phase G2 [idx 6-13]  Re-tune item_id_dropout and item_embed_weight_decay
#                        AT the new d192_l3 capacity for both content
#                        variants (old regularization was tuned for the
#                        smaller 128/2 model).
#   Phase G3 [idx 14-17] Reseed (43, 44) each content variant's d192_l3
#                        winner to get error bars before trusting the
#                        Phase F capacity gain as real.
#   Phase G4 [idx 18-19] Sanity-check capacity scaling on skill_item (no
#                        content features) to see if depth alone helps, or
#                        if the Phase F gain is specific to having content
#                        embeddings to route through the extra layer.
#   Phase G5 [idx 20-21] Re-tune general --dropout AT the new d192_l3
#                        capacity for both content variants. Phase E (round
#                        2) found dropout 0.3/0.4 worse than the default 0.2
#                        -- but only at the OLD, smaller 128/2 capacity.
#                        d192_l3 has roughly double the parameters, and
#                        bigger models often need MORE regularization, not
#                        the same amount -- this was left untested when G2
#                        retuned item_id_dropout/item_embed_wd at the new
#                        capacity, so close that gap here too.
#
# SLURM array tasks run in index order; the manifest ordering is still
# priority-sorted, but the %N concurrency throttle is intentionally left OFF
# (--array=0-21, no %N) so every task requests its own GPU and starts as
# soon as the scheduler can place it -- i.e. all 22 tasks run fully in
# parallel, each writing to its own independent runs_tune3/<variant>/<run>/
# directory.
#
# Usage:
#   1. Run run_lumi.sh (data prep + embeddings), tune_lumi.sh (round 1), and
#      tune_lumi_phase2.sh (round 2) first -- this script does not redo any
#      of them and assumes the same prepared_v2 sequences/vocab/embeddings.
#   2. sbatch --account=project_462001308 tune_lumi_phase3.sh
#      (defaults to the full 0-21 array; override with e.g.
#      `sbatch --array=0-5%6 tune_lumi_phase3.sh` to run only Phase G1)
#   3. After tasks finish: python compare_runs.py --runs-dir
#      <RUNS_TUNE_DIR>/<variant> per variant, same as rounds 1-2.
#
#SBATCH --job-name=math_kt_tune3
#SBATCH --partition=small-g
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=7
#SBATCH --mem=64G
#SBATCH --time=18:00:00
#SBATCH --array=0-21
#SBATCH --output=math_kt_tune3_%A_%a.out
#SBATCH --error=math_kt_tune3_%A_%a.err
# Do not hard-code your account in this file if you prefer:
# sbatch --account=project_462001308 tune_lumi_phase3.sh

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
RUNS_TUNE_ROOT="${KT_RUNS_TUNE_ROOT:-${PROJECT_ROOT}/runs_tune3}"

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
# Round-2 bests this manifest builds on (see runs_tune2/<variant>/):
#   skill_item:                  id_dropout=0.60 wd=0.005            (val .9281 / cold .8905) -- saturated
#   skill_item_content:          d_model=192 n_layers=3, id=0.40 wd=0.02   (val .9340 / cold .9116) -- best overall
#   skill_item_content_option:   d_model=192 n_layers=3, id=0.40 wd=0.005  (val .9342 / cold .9128) -- best overall
MANIFEST=(
  # --- Phase G1: push capacity further for content variants than Phase F's
  #     d192_l3 winner, to see if depth/width keep paying off (idx 0-5) ---
  "skill_item_content,0.40,0.02,-,-,-,-,192,4,idG1_d192_l4"
  "skill_item_content,0.40,0.02,-,-,-,-,256,3,idG1_d256_l3"
  "skill_item_content,0.40,0.02,-,-,-,-,256,4,idG1_d256_l4"
  "skill_item_content_option,0.40,0.005,-,-,-,-,192,4,idG1_d192_l4"
  "skill_item_content_option,0.40,0.005,-,-,-,-,256,3,idG1_d256_l3"
  "skill_item_content_option,0.40,0.005,-,-,-,-,256,4,idG1_d256_l4"

  # --- Phase G2: re-tune item_id_dropout / item_embed_weight_decay AT the
  #     new d192_l3 capacity (round-1/2 values were tuned for the smaller
  #     128/2 model and are untested at this capacity) (idx 6-13) ---
  "skill_item_content,0.30,0.02,-,-,-,-,192,3,idG2_id0.30"
  "skill_item_content,0.50,0.02,-,-,-,-,192,3,idG2_id0.50"
  "skill_item_content,0.40,0.01,-,-,-,-,192,3,idG2_wd0.01"
  "skill_item_content,0.40,0.05,-,-,-,-,192,3,idG2_wd0.05"
  "skill_item_content_option,0.30,0.005,-,-,-,-,192,3,idG2_id0.30"
  "skill_item_content_option,0.50,0.005,-,-,-,-,192,3,idG2_id0.50"
  "skill_item_content_option,0.40,0.01,-,-,-,-,192,3,idG2_wd0.01"
  "skill_item_content_option,0.40,0.05,-,-,-,-,192,3,idG2_wd0.05"

  # --- Phase G3: reseed each content variant's Phase F (d192_l3) winner to
  #     get error bars -- Phase D showed ~0.001 val / ~0.001-0.002 cold AUC
  #     seed noise at the old capacity, so this must be confirmed before
  #     trusting the capacity gain as real (idx 14-17) ---
  "skill_item_content,0.40,0.02,-,-,43,-,192,3,idG3_seed43"
  "skill_item_content,0.40,0.02,-,-,44,-,192,3,idG3_seed44"
  "skill_item_content_option,0.40,0.005,-,-,43,-,192,3,idG3_seed43"
  "skill_item_content_option,0.40,0.005,-,-,44,-,192,3,idG3_seed44"

  # --- Phase G4: sanity-check whether capacity scaling helps skill_item
  #     (no content features) at all, or whether the Phase F gain is
  #     specific to having content embeddings for the extra layer to route
  #     through (idx 18-19) ---
  "skill_item,0.40,0.005,-,-,-,-,192,3,idG4_d192_l3"
  "skill_item,0.60,0.005,-,-,-,-,192,3,idG4_id0.60_d192_l3"

  # --- Phase G5: re-tune general --dropout AT the new d192_l3 capacity --
  #     round-2 phase E only tested this at the old 128/2 capacity, and a
  #     ~2x-larger model may need more (not the same) regularization
  #     (idx 20-21) ---
  "skill_item_content,0.40,0.02,-,-,-,0.3,192,3,idG5_dropout0.3"
  "skill_item_content_option,0.40,0.005,-,-,-,0.3,192,3,idG5_dropout0.3"
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
