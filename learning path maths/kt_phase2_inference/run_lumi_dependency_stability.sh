#!/bin/bash -l
#SBATCH --job-name=kt_dep_stability
#SBATCH --partition=small-g
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=1-00:00:00
#SBATCH --array=0-2
#SBATCH --output=kt_dep_stability_%A_%a.out
#SBATCH --error=kt_dep_stability_%A_%a.err

set -euo pipefail
module load LUMI/25.03
module load partition/G
module load cray-python/3.11.7
module load rocm/6.2.4

PROJECT_ID="${KT_PROJECT_ID:-project_462001308}"
ROOT="/projappl/${PROJECT_ID}/math_kt"
SCRIPT_DIR="$(cd -- "${SLURM_SUBMIT_DIR:?Submit from kt_phase2_inference}" && pwd)"
PYTHON="${KT_PYTHON:-${ROOT}/mathkt-env/bin/python}"
export KT_PHASE2_PREPARED="/scratch/${PROJECT_ID}/math_kt/prepared_v2_w400_o0p25_g30_c8"
export KT_PHASE2_RUN_DIR="${ROOT}/runs/skill_item_content_option"
export KT_PHASE2_TEXT_EMBEDDINGS="${ROOT}/embeddings/text_embeddings_v2.npz"

if [[ ! -x "$PYTHON" ]]; then
    echo "Python executable not found: $PYTHON" >&2
    exit 2
fi
for file in "$KT_PHASE2_PREPARED/sequences.jsonl.gz" "$KT_PHASE2_PREPARED/vocab.json" \
    "$KT_PHASE2_RUN_DIR/best_model_joint.pt" "$KT_PHASE2_RUN_DIR/config.json" \
    "$KT_PHASE2_TEXT_EMBEDDINGS" "$SCRIPT_DIR/probe_predictive_dependency.py" \
    "$SCRIPT_DIR/../kt_phase1/modeling/model.py" "$SCRIPT_DIR/../kt_phase1/modeling/dataset.py"; do
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

seeds=(20260919 20260920 20260921)
seed="${seeds[${SLURM_ARRAY_TASK_ID:?Submit as a job array}]:?Invalid array index}"
out_dir="$SCRIPT_DIR/artifacts/stability"
mkdir -p "$out_dir"
out="$out_dir/predictive_dependency_seed_${seed}.csv"
if [[ -e "$out" || -e "${out%.csv}.meta.json" ]]; then
    echo "Output already exists for seed $seed; refusing to overwrite it" >&2
    exit 2
fi
"$PYTHON" -c 'import torch; assert torch.cuda.is_available(), "GPU unavailable"'
"$PYTHON" "$SCRIPT_DIR/probe_predictive_dependency.py" --device cuda --max-windows 20000 --seed "$seed" --out "$out"
