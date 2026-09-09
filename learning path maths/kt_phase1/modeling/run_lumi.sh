#!/bin/bash -l
# LUMI end-to-end runner for the three math KT variants.
#
# Before submitting:
#   1. The configured LUMI project ID is project_462001308, OR submit
#      with: sbatch --account=project_462001308 run_lumi.sh
#   2. Put this modeling directory in:
#        /projappl/project_462001308/math_kt/code/modeling
#   3. Put kt_interactions.csv.gz in:
#        /projappl/project_462001308/math_kt/raw/
#   4. Load/activate a Python environment with torch, numpy, pandas,
#      scikit-learn, sentence-transformers, and a ROCm-enabled PyTorch build
#      (LUMI-G uses AMD MI250x GPUs; the device name in PyTorch is still
#      "cuda" -- see LUMI_SETUP.md). You may set KT_PYTHON to the
#      environment's python executable (or a container-exec wrapper).
#
# Storage layout:
#   /projappl/.../math_kt/   persistent: code, raw data, embeddings, checkpoints
#   /scratch/.../math_kt/   temporary: copied raw input and prepared sequences
#
# Parallel training: the three variants are fully independent (same prepared
# data, no shared state), so they are launched concurrently, one per GPU, on
# a single node -- roughly 3x faster wall-clock than training them one after
# another. This needs --gpus-per-node=3 (LUMI-G nodes have 8 GCDs available,
# so requesting 3 of them is fine) and enough CPUs/memory for three
# independent training processes at once (each loads its own full copy of
# the dataset into RAM). If you change VARIANTS below, keep --gpus-per-node,
# --cpus-per-task (7 per variant) and --mem (60G per variant) in sync.
#SBATCH --job-name=math_kt
#SBATCH --partition=small-g
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=3
#SBATCH --cpus-per-task=21
#SBATCH --mem=180G
#SBATCH --time=1-00:00:00
#SBATCH --output=math_kt_%j.out
#SBATCH --error=math_kt_%j.err
# Do not hard-code your account in this file if you prefer:
# sbatch --account=project_462001308 run_lumi.sh

set -euo pipefail

PROJECT_ID="${KT_PROJECT_ID:-project_462001308}"
PROJECT_ROOT="${KT_PROJECT_ROOT:-/projappl/${PROJECT_ID}/math_kt}"
SCRATCH_ROOT="${KT_SCRATCH_ROOT:-/scratch/${PROJECT_ID}/math_kt}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CODE_DIR="${KT_CODE_DIR:-${SLURM_SUBMIT_DIR:-${SCRIPT_DIR}}}"
# LUMI-G software environment: AMD ROCm GPU + Python.
module load LUMI/25.03
module load partition/G
module load cray-python/3.11.7
module load rocm/6.2.4
PYTHON="${KT_PYTHON:-/projappl/project_462001308/math_kt/mathkt-env/bin/python}"

RAW_PROJECT="${PROJECT_ROOT}/raw/kt_interactions.csv.gz"
RAW_SCRATCH="${SCRATCH_ROOT}/raw/kt_interactions.csv.gz"
MAX_SEQ_LEN="${KT_MAX_SEQ_LEN:-400}"
CONTEXT_OVERLAP_FRAC="${KT_CONTEXT_OVERLAP_FRAC:-0.25}"
# Include preprocessing settings in the directory name so a rerun cannot
# accidentally reuse a sequences.jsonl.gz produced by the old truncation
# pipeline (or by a different window/overlap configuration).
PREP_DIR="${KT_PREP_DIR:-${SCRATCH_ROOT}/prepared_w${MAX_SEQ_LEN}_o${CONTEXT_OVERLAP_FRAC//./p}}"
EMBED_DIR="${PROJECT_ROOT}/embeddings"
RUNS_DIR="${PROJECT_ROOT}/runs"

if [[ "${PROJECT_ID}" == "project_XXXXXXX" ]]; then
    echo "Set KT_PROJECT_ID or edit PROJECT_ID before submitting."
    exit 2
fi
if [[ ! -f "${RAW_PROJECT}" ]]; then
    echo "Missing raw dataset: ${RAW_PROJECT}"
    exit 2
fi

mkdir -p "${SCRATCH_ROOT}/raw" "${PREP_DIR}" "${EMBED_DIR}" "${RUNS_DIR}"
cd "${CODE_DIR}"

if [[ ! -x "${PYTHON}" ]]; then
    echo "Python executable not found: ${PYTHON}"
    echo "Create /projappl/project_462001308/math_kt/mathkt-env or set KT_PYTHON."
    exit 2
fi
N_GPUS_NEEDED=3
"${PYTHON}" -c "
import torch
n = torch.cuda.device_count()
print('PyTorch:', torch.__version__)
print('GPUs visible:', n)
assert torch.cuda.is_available(), 'ROCm GPU is not available'
assert n >= ${N_GPUS_NEEDED}, f'Need >= ${N_GPUS_NEEDED} GPUs for parallel training, only {n} visible -- check --gpus-per-node.'
"

# Avoid filling a single Lustre location with a second copy if it is already
# present from a previous job. Scratch copies can be regenerated at any time.
if [[ ! -f "${RAW_SCRATCH}" ]]; then
    cp "${RAW_PROJECT}" "${RAW_SCRATCH}"
fi

# This is CPU/I/O preparation and runs once. It creates the compact sequence
# representation used by every model epoch. --max-seq-len here MUST match
# --max-seq-len in COMMON below: it's the window size that long students get
# chunked into (see chunk_student_events in prepare_sequences.py), not a
# truncation length, so no interactions are silently dropped.
if [[ ! -f "${PREP_DIR}/sequences.jsonl.gz" ]]; then
    "${PYTHON}" prepare_sequences.py \
        --source "${RAW_SCRATCH}" \
        --out-dir "${PREP_DIR}" \
        --min-interactions 10 \
        --max-seq-len "${MAX_SEQ_LEN}" \
        --context-overlap-frac "${CONTEXT_OVERLAP_FRAC}" \
        --cold-item-fraction 0.05
fi

# Model C only: multilingual embeddings consume Finnish text directly. The
# embedding cache is kept in persistent project storage because it is expensive
# to regenerate and is reusable across training runs.
if [[ ! -f "${EMBED_DIR}/text_embeddings.npz" ]]; then
    "${PYTHON}" embed_questions.py \
        --sequences "${PREP_DIR}/sequences.jsonl.gz" \
        --out "${EMBED_DIR}/text_embeddings.npz" \
        --batch-size 512
fi

COMMON=(
    --sequences "${PREP_DIR}/sequences.jsonl.gz"
    --vocab "${PREP_DIR}/vocab.json"
    --device cuda
    --epochs 30
    --batch-size 256
    --max-seq-len "${MAX_SEQ_LEN}"
    --num-workers 2
    --joint-weight "${KT_JOINT_WEIGHT:-0.5}"
)
# Cold-start regularization for the item-aware variants only (skill_only has
# no item_embed, so these are no-ops for it): randomly resolve some known
# item_ids to __UNK__ during training (--item-id-dropout) and apply a
# separate, stronger weight decay to item_embed (--item-embed-weight-decay).
# Both curb the test_cold_item AUC decay seen when item embeddings are
# trained without them (see README "Reading the results").
ITEM_REG=(
    --item-id-dropout "${KT_ITEM_ID_DROPOUT:-0.1}"
    --item-embed-weight-decay "${KT_ITEM_EMBED_WEIGHT_DECAY:-1e-3}"
)

# ---- train all three variants in parallel, one GPU each ---
# Each is pinned to a distinct GPU via HIP_VISIBLE_DEVICES (ROCm's
# equivalent of CUDA_VISIBLE_DEVICES; PyTorch's "cuda" device still maps to
# the process-local device -- see LUMI_SETUP.md). Indices are 0..N-1 *within this job's own
# allocation*, not physical node-wide GPU IDs, so this is safe regardless of
# which physical GCDs SLURM actually assigned. stdout/stderr for each
# variant is redirected to its own log (they'd otherwise interleave
# unreadably in the shared math_kt_<jobid>.out), and failures are collected
# so one variant crashing doesn't get silently swallowed.
train_variant() {
    local gpu_id="$1" variant="$2"
    shift 2
    local log_dir="${RUNS_DIR}/${variant}"
    mkdir -p "${log_dir}"
    echo "Starting ${variant} on GPU ${gpu_id} (log: ${log_dir}/train.log)"
    HIP_VISIBLE_DEVICES="${gpu_id}" \
        "${PYTHON}" train.py --variant "${variant}" "$@" \
        > "${log_dir}/train.log" 2>&1
}

train_variant 0 skill_only \
    "${COMMON[@]}" --out-dir "${RUNS_DIR}/skill_only" &
PID_SKILL_ONLY=$!

train_variant 1 skill_item \
    "${COMMON[@]}" "${ITEM_REG[@]}" --out-dir "${RUNS_DIR}/skill_item" &
PID_SKILL_ITEM=$!

train_variant 2 skill_item_content \
    "${COMMON[@]}" "${ITEM_REG[@]}" \
    --text-embeddings "${EMBED_DIR}/text_embeddings.npz" \
    --out-dir "${RUNS_DIR}/skill_item_content" &
PID_SKILL_ITEM_CONTENT=$!

status=0
for entry in "PID_SKILL_ONLY:skill_only" "PID_SKILL_ITEM:skill_item" "PID_SKILL_ITEM_CONTENT:skill_item_content"; do
    pid_var="${entry%%:*}"
    variant="${entry##*:}"
    pid="${!pid_var}"
    if wait "${pid}"; then
        echo "${variant} finished OK."
    else
        echo "ERROR: ${variant} training failed (exit code $?). See ${RUNS_DIR}/${variant}/train.log" >&2
        status=1
    fi
done
if [[ "${status}" -ne 0 ]]; then
    echo "One or more variants failed -- see errors above. Not running compare_runs.py." >&2
    exit 1
fi

"${PYTHON}" compare_runs.py \
    --runs-dir "${RUNS_DIR}" \
    --joint-weight "${KT_JOINT_WEIGHT:-0.5}" \
    --out "${RUNS_DIR}/comparison.json"

printf '\nCompleted. Results are in %s\n' "${RUNS_DIR}"
