#!/bin/bash -l
#SBATCH --job-name=kt_phase2
#SBATCH --partition=small-g
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=1-00:00:00
#SBATCH --array=0-1
#SBATCH --output=kt_phase2_%A_%a.out
#SBATCH --error=kt_phase2_%A_%a.err

set -euo pipefail

module load LUMI/25.03
module load partition/G
module load cray-python/3.11.7
module load rocm/6.2.4

PROJECT_ID="${KT_PROJECT_ID:-project_462001308}"
PROJECT_ROOT="/projappl/${PROJECT_ID}/math_kt"
SCRATCH_ROOT="/scratch/${PROJECT_ID}/math_kt"
SCRIPT_DIR="$(cd -- "${SLURM_SUBMIT_DIR:?Submit from the kt_phase2_inference directory}" && pwd)"
ARTIFACTS="${SCRIPT_DIR}/artifacts"
PYTHON="${KT_PYTHON:-${PROJECT_ROOT}/mathkt-env/bin/python}"
export KT_PHASE2_PREPARED="${SCRATCH_ROOT}/prepared_v2_w400_o0p25_g30_c8"
export KT_PHASE2_RUN_DIR="${PROJECT_ROOT}/runs/skill_item_content_option"
export KT_PHASE2_TEXT_EMBEDDINGS="${PROJECT_ROOT}/embeddings/text_embeddings_v2.npz"

if [[ ! -x "$PYTHON" ]]; then
    echo "Python executable not found: $PYTHON" >&2
    exit 2
fi
for file in \
    "$SCRIPT_DIR/../kt_phase1/modeling/dataset.py" \
    "$SCRIPT_DIR/../kt_phase1/modeling/model.py" \
    "$KT_PHASE2_PREPARED/sequences.jsonl.gz" \
    "$KT_PHASE2_PREPARED/vocab.json" \
    "$KT_PHASE2_PREPARED/split_report.json" \
    "$KT_PHASE2_RUN_DIR/best_model_joint.pt" \
    "$KT_PHASE2_RUN_DIR/config.json" \
    "$KT_PHASE2_TEXT_EMBEDDINGS"; do
    if [[ ! -r "$file" ]]; then
        echo "Missing input: $file" >&2
        exit 2
    fi
done

if ! printf '%s  %s\n' \
    'a539546e3dd9df67ba898bee129c68d727b7f7602486f8e50360ba8f0c21ee81' \
    "$KT_PHASE2_RUN_DIR/best_model_joint.pt" | sha256sum -c --status; then
    echo "Checkpoint differs from the selected epoch-45 variant-D model" >&2
    exit 2
fi
if ! printf '%s  %s\n' \
    '17763b1833b9cb1f3c24c79f77395b8beab6df40d1d2e179ca7457cd6a26cbed' \
    "$KT_PHASE2_RUN_DIR/config.json" | sha256sum -c --status; then
    echo "Run config differs from the selected variant-D model" >&2
    exit 2
fi

mkdir -p "$ARTIFACTS"
case "${SLURM_ARRAY_TASK_ID:?Submit with sbatch as a job array}" in
    0) outputs=("$ARTIFACTS/predictions_d.npz") ;;
    1) outputs=("$ARTIFACTS/predictive_dependency.csv" "$ARTIFACTS/predictive_dependency.meta.json") ;;
    *) echo "Unexpected array task: $SLURM_ARRAY_TASK_ID" >&2; exit 2 ;;
esac
for output in "${outputs[@]}"; do
    if [[ -e "$output" ]]; then
        echo "Output already exists: $output; refusing to overwrite it" >&2
        exit 2
    fi
done

"$PYTHON" -c 'import torch; print("PyTorch:", torch.__version__, "GPU:", torch.cuda.is_available(), flush=True); assert torch.cuda.is_available()'
case "$SLURM_ARRAY_TASK_ID" in
    0) "$PYTHON" "$SCRIPT_DIR/dump_predictions.py" --device cuda ;;
    1) "$PYTHON" "$SCRIPT_DIR/probe_predictive_dependency.py" --device cuda --max-windows 4000 ;;
esac
