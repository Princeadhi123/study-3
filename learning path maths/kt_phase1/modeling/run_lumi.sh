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
#SBATCH --job-name=math_kt
#SBATCH --partition=small-g
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=7
#SBATCH --mem=60G
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
"${PYTHON}" -c "import torch; print('PyTorch:', torch.__version__); print('GPU available:', torch.cuda.is_available()); assert torch.cuda.is_available(), 'ROCm GPU is not available'"

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
)

"${PYTHON}" train.py --variant skill_only \
    "${COMMON[@]}" --out-dir "${RUNS_DIR}/skill_only"

"${PYTHON}" train.py --variant skill_item \
    "${COMMON[@]}" --out-dir "${RUNS_DIR}/skill_item"

"${PYTHON}" train.py --variant skill_item_content \
    "${COMMON[@]}" \
    --text-embeddings "${EMBED_DIR}/text_embeddings.npz" \
    --out-dir "${RUNS_DIR}/skill_item_content"

"${PYTHON}" compare_runs.py \
    --runs-dir "${RUNS_DIR}" \
    --out "${RUNS_DIR}/comparison.json"

printf '\nCompleted. Results are in %s\n' "${RUNS_DIR}"
