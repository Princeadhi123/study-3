#!/bin/bash -l
# LUMI runner for the open-set math misconception tagging pipeline
# (tag_math_distractors.py). See MATH_TAGGING.md for the full pipeline
# description; this script just runs it end-to-end on LUMI with checkpoints
# in persistent project storage so a SLURM time-limit interruption (or a
# resubmit) resumes instead of redoing already-completed LLM calls.
#
# This is an API+CPU job (Aitta LLM calls + a local sentence-transformer for
# clustering), NOT a GPU training job -- unlike kt_phase1/modeling/run_lumi.sh,
# it requests the CPU partition, not small-g.
#
# Before submitting:
#   1. Put this misconception_tagging directory in:
#        /projappl/project_462001308/math_kt/code/misconception_tagging
#      (it imports tagging_common.py by relative path, so keep the two
#      scripts together)
#   2. Make sure reports_v2/distractor_catalog.csv AND reports_v2/item_context.csv
#      exist under the kt_phase1 checkout on this filesystem (item_context.csv
#      is built by kt_phase1/build_v2_item_context.py -- run it once, after
#      build_v2_item_level.py, if it doesn't exist yet: it needs the full
#      kt_interactions_v2_item_level.csv.gz, a few minutes, one streaming pass)
#   3. .env at the repo root has AITTA_BASE_URL / AITTA_API_KEY / AITTA_MODEL
#      set (see https://aitta.csc.fi) -- this must be reachable from wherever
#      this job actually runs; see the connectivity check below.
#   4. A Python environment with the packages in requirements.txt (pandas,
#      numpy, openai, python-dotenv, sentence-transformers -- CPU build of
#      torch is fine, no GPU needed here): pip install -r requirements.txt
#
#SBATCH --job-name=math_misconception_tagging
#SBATCH --partition=small
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=2-00:00:00
#SBATCH --output=math_tagging_%j.out
#SBATCH --error=math_tagging_%j.err
# Do not hard-code your account in this file if you prefer:
# sbatch --account=project_462001308 run_lumi_tagging.sh

set -euo pipefail

PROJECT_ID="${KT_PROJECT_ID:-project_462001308}"
PROJECT_ROOT="${KT_PROJECT_ROOT:-/projappl/${PROJECT_ID}/math_kt}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CODE_DIR="${KT_CODE_DIR:-${SLURM_SUBMIT_DIR:-${SCRIPT_DIR}}}"
KT_PHASE1_DIR="${KT_PHASE1_DIR:-${CODE_DIR}/../learning path maths/kt_phase1}"

module load LUMI/25.03
module load cray-python/3.11.7
PYTHON="${KT_PYTHON:-/projappl/project_462001308/math_kt/mathkt-env/bin/python}"

DISTRACTOR_CATALOG="${KT_DISTRACTOR_CATALOG:-${KT_PHASE1_DIR}/reports_v2/distractor_catalog.csv}"
ITEM_CONTEXT="${KT_ITEM_CONTEXT:-${KT_PHASE1_DIR}/reports_v2/item_context.csv}"
CHECKPOINT_DIR="${PROJECT_ROOT}/misconception_tagging/checkpoints"
OUT_DIR="${PROJECT_ROOT}/misconception_tagging/out"

cd "${CODE_DIR}"

if [[ ! -x "${PYTHON}" ]]; then
    echo "Python executable not found: ${PYTHON}"
    echo "Create /projappl/project_462001308/math_kt/mathkt-env or set KT_PYTHON."
    exit 2
fi
if [[ ! -f "${DISTRACTOR_CATALOG}" ]]; then
    echo "Missing ${DISTRACTOR_CATALOG}"
    echo "(produced by kt_phase1/build_v2_item_level.py, stage 3 of the v2 pipeline)"
    exit 2
fi
if [[ ! -f "${ITEM_CONTEXT}" ]]; then
    echo "Missing ${ITEM_CONTEXT}"
    echo "Run: ${PYTHON} \"${KT_PHASE1_DIR}/build_v2_item_context.py\""
    echo "(needs the full kt_interactions_v2_item_level.csv.gz -- one streaming pass, a few minutes)"
    exit 2
fi

# Aitta connectivity check: this is a CSC-hosted service, not a general
# internet endpoint, so unlike embed_questions.py's Hugging Face download
# (which needs a login node -- see kt_phase1/modeling/LUMI_SETUP.md), Aitta
# is generally expected to be reachable from LUMI compute nodes. Verify
# before burning the whole job's time budget on a job that can't reach it.
"${PYTHON}" -c "
from tagging_common import get_client, default_model, call_with_retry
client = get_client()
resp = call_with_retry(client, model=default_model(), max_retries=2,
    messages=[{'role': 'user', 'content': 'Reply with exactly: OK'}], max_tokens=10)
print('Aitta reachable, test response:', resp.choices[0].message.content)
"

mkdir -p "${CHECKPOINT_DIR}" "${OUT_DIR}"

# Elicitation processes exercise_type by exercise_type, most-impactful (total
# times_selected) first, and is fully checkpointed -- safe to resubmit this
# whole script if the 2-day time limit is hit mid-elicitation; already-tagged
# (item_id, option_value) pairs are skipped on resume, no wasted LLM calls.
"${PYTHON}" tag_math_distractors.py --stage elicit \
    --distractor-catalog "${DISTRACTOR_CATALOG}" \
    --item-context "${ITEM_CONTEXT}" \
    --checkpoint-dir "${CHECKPOINT_DIR}" \
    --min-times-selected "${KT_MIN_TIMES_SELECTED:-2}" \
    --concurrency "${KT_CONCURRENCY:-8}" \
    --reasoning-effort "${KT_REASONING_EFFORT:-low}"

# Clustering is local (sentence-transformer embeddings + greedy clustering),
# no LLM calls, cheap to redo if you want to try a different --cluster-threshold.
"${PYTHON}" tag_math_distractors.py --stage cluster \
    --checkpoint-dir "${CHECKPOINT_DIR}" \
    --cluster-threshold "${KT_CLUSTER_THRESHOLD:-0.82}"

# Verification: LLM critique pass on low-confidence assignments only
# (small clusters / low centroid margin / missing question context).
# Set KT_VERIFY_MODEL to one of the other models Aitta hosts (see
# https://aitta.csc.fi/models) to decorrelate the critique from whatever
# model made the original elicit-stage error, instead of asking the same
# model to mark its own homework. Defaults to the same model as elicit.
VERIFY_MODEL_ARGS=()
if [[ -n "${KT_VERIFY_MODEL:-}" ]]; then
    VERIFY_MODEL_ARGS=(--verify-model "${KT_VERIFY_MODEL}")
fi
"${PYTHON}" tag_math_distractors.py --stage verify \
    --item-context "${ITEM_CONTEXT}" \
    --checkpoint-dir "${CHECKPOINT_DIR}" \
    --small-cluster-threshold "${KT_SMALL_CLUSTER_THRESHOLD:-2}" \
    --verify-margin "${KT_VERIFY_MARGIN:-0.08}" \
    --concurrency "${KT_CONCURRENCY:-8}" \
    --reasoning-effort "${KT_REASONING_EFFORT:-low}" \
    "${VERIFY_MODEL_ARGS[@]}"

"${PYTHON}" tag_math_distractors.py --stage export \
    --distractor-catalog "${DISTRACTOR_CATALOG}" \
    --checkpoint-dir "${CHECKPOINT_DIR}" \
    --out-dir "${OUT_DIR}" \
    --small-cluster-threshold "${KT_SMALL_CLUSTER_THRESHOLD:-2}" \
    --verify-margin "${KT_VERIFY_MARGIN:-0.08}"

printf '\nCompleted. Results are in %s\n' "${OUT_DIR}"
printf 'Review %s/human_review_queue.csv (sorted by impact) before trusting the low-confidence tags.\n' "${OUT_DIR}"
