# Running the math KT models on LUMI

This describes exactly what to upload, where to put it, and how to submit
`run_lumi.sh`, which already encodes the storage layout below.

## Storage layout

LUMI has three relevant tiers. Use them as follows:

| Tier | Path (from `run_lumi.sh`) | What goes there | Why |
|---|---|---|---|
| **Project (persistent)** | `/projappl/project_462001308/math_kt/` | Code, raw data, question embeddings, trained checkpoints, results | Persistent for the project lifetime |
| **Scratch (temporary)** | `/scratch/project_462001308/math_kt/` | Working raw copy and prepared `sequences.jsonl.gz` | Faster for heavy I/O; reproducible files may be regenerated and scratch can be cleaned |

For your project, use `/projappl/project_462001308/math_kt/` (LUMI also exposes this persistent project space as `/project/project_462001308/` on systems where that path is available). Concretely:

```text
math_kt/
  code/
    modeling/              <- clone/upload the GitHub modeling code here
  raw/
    kt_interactions.csv.gz <- upload this one file (~430 MB, from kt_phase1/data/)
  embeddings/              <- created automatically by the script (Model C only)
  runs/                    <- created automatically; trained checkpoints + metrics land here
```

You do **not** need to upload:

- The uncompressed raw CSV, the Excel split files, or the sample CSVs
- `prepared/` from any local smoke test (LUMI regenerates its own from the full file)
- Chemistry or DigiArvi data (not used by these scripts)

## What to upload

From your machine, upload:

```text
kt_phase1/modeling/                 -> /projappl/project_462001308/math_kt/code/modeling/
kt_phase1/data/kt_interactions.csv.gz -> /projappl/project_462001308/math_kt/raw/kt_interactions.csv.gz
```

### If the GitHub repository is public

On LUMI, clone the repository directly into the required `modeling` directory:

```bash
mkdir -p /projappl/project_462001308/math_kt/code
cd /projappl/project_462001308/math_kt/code
git clone https://github.com/<username>/<repository>.git modeling
cd modeling
```

If the repository already contains a parent folder, make sure that these files
are directly inside `modeling/`:

```text
run_lumi.sh
prepare_sequences.py
embed_questions.py
train.py
model.py
dataset.py
compare_runs.py
```

### Upload the raw data separately

The raw data is intentionally gitignored, so it will not be in GitHub. From
your local machine, use `rsync`, `scp`, or WinSCP:

```bash
rsync -avP "learning path maths/kt_phase1/data/kt_interactions.csv.gz" \
    myuser@lumi.csc.fi:/projappl/project_462001308/math_kt/raw/kt_interactions.csv.gz
```

Create the target directory first if needed:

```bash
mkdir -p /projappl/project_462001308/math_kt/raw
```

`scp` works if `rsync` is unavailable. WinSCP can upload the file to the same
`/projappl/project_462001308/math_kt/raw/` directory.

## Environment: LUMI-G uses AMD GPUs (ROCm), not NVIDIA

LUMI's GPU partition (`small-g`, used by `run_lumi.sh`) has **AMD MI250x** GPUs,
not NVIDIA. You need a **ROCm-enabled PyTorch build**, not the default CUDA
wheel from pip. Two standard ways to get one on LUMI:

### Option A: LUMI's provided PyTorch container (recommended, easiest)

CSC publishes ready-made ROCm+PyTorch singularity containers for LUMI. Check
current instructions with:

```bash
module load LUMI
module spider PyTorch
```

Then set `KT_PYTHON` to a small wrapper script that runs inside the
container, e.g.:

```bash
cat > run_python_in_container.sh <<'EOF'
#!/bin/bash
singularity exec $SIF_PATH python "$@"
EOF
chmod +x run_python_in_container.sh
export KT_PYTHON="$(pwd)/run_python_in_container.sh"
```

(`$SIF_PATH` is the path to the container image; `module spider PyTorch` /
LUMI's documentation gives the current recommended one and load command.)

### Option B: a plain virtual environment with ROCm PyTorch

```bash
module load cray-python
python -m venv ~/mathkt-env
source ~/mathkt-env/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/rocm6.0
pip install numpy pandas scikit-learn sentence-transformers
```

Check the currently supported ROCm version on LUMI (`module avail rocm`) and
match the PyTorch ROCm wheel version to it.

**Either way, `--device cuda` in the scripts is still correct** — ROCm's
PyTorch build keeps the `cuda` device name and `torch.cuda.is_available()`
API for compatibility, so nothing in `train.py`/`model.py` needs to change.

### Internet access for `embed_questions.py`

LUMI compute nodes typically have no internet access. `embed_questions.py`
needs to download the multilingual sentence-transformer model from Hugging
Face the first time. Two options:

1. Run `embed_questions.py` once on a LUMI **login node** (which usually has
   internet) before submitting the GPU job, so the model gets cached under
   `~/.cache/huggingface`. Then the compute-node job in `run_lumi.sh` will
   reuse the same cache.
2. Or download the model on any machine with internet, copy the
   `~/.cache/huggingface/hub/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2`
   folder to the same path in your LUMI home directory.

## Submitting the job

```bash
ssh myuser@lumi.csc.fi
cd /projappl/project_462001308/math_kt/code/modeling

export KT_PROJECT_ID=project_462001308
sbatch --account="${KT_PROJECT_ID}" run_lumi.sh
```

The checked-in `run_lumi.sh` already defaults to `project_462001308`, so you may omit the export. Passing `--account` explicitly is still recommended.

Check status and logs:

```bash
squeue --me
tail -f math_kt_<jobid>.out
```

## What the script does, in order

1. Copies `raw/kt_interactions.csv.gz` from `/project` to `/scratch` (only if
   not already copied — safe to resubmit).
2. Runs `prepare_sequences.py` once into `/scratch/.../prepared/` (the
   compact per-student sequence format all three models train from).
3. Runs `embed_questions.py` once into `/project/.../embeddings/` (persistent,
   since it's expensive to redo and only needed for Model C).
4. Trains all three variants (`skill_only`, `skill_item`, `skill_item_content`)
   into `/project/.../runs/<variant>/`.
5. Runs `compare_runs.py`, writing `/project/.../runs/comparison.json`.

## After the job finishes

Download the results back to your machine:

```bash
rsync -avP myuser@lumi.csc.fi:/projappl/project_462001308/math_kt/runs/ \
    "learning path maths/kt_phase1/modeling/runs/"
```

That brings back `comparison.json`, each variant's `best_model.pt`,
`training_history.json`, and `config.json`.

## Quick pre-flight checklist

- [ ] `run_lumi.sh` uploaded to `code/modeling/`
- [ ] `kt_interactions.csv.gz` uploaded to `raw/`
- [ ] ROCm PyTorch environment set up (container or venv) and `KT_PYTHON` set if using a container wrapper
- [ ] Multilingual embedding model pre-cached (login node run, or copied `~/.cache/huggingface`)
- [x] Project ID configured as `project_462001308`
- [ ] Submitted with `sbatch --account=<project_id> run_lumi.sh` (or edited into the script)
