"""Canonical locations of the FROZEN Phase-1 artifacts this phase consumes.

Phase 2 never trains, never fine-tunes, and never writes anything back into
`kt_phase1/`. Everything below is opened read-only; every Phase-2 output goes
to `kt_phase2_inference/artifacts/`.

The deployed model is Phase 1's variant D (`skill_item_content_option`) at its
`best_model_joint.pt` checkpoint -- epoch 45, seed 42, the deployment
recommendation documented in `kt_phase1/modeling/README.md` ("Reading the
results"). `best_model.pt` (epoch 68, best val AUC) is deliberately NOT the
default: it trades ~0.003 test_cold_item AUC for warm-item gains, and the
cold-item regime (a question the model has never trained on) is the regime a
live tutoring loop actually operates in.
"""
from pathlib import Path

PHASE2_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PHASE2_ROOT.parent
PHASE1 = REPO_ROOT / "kt_phase1"

# --- frozen Phase-1 inputs (read-only) ---------------------------------
INTERACTIONS = PHASE1 / "data_v2" / "kt_interactions_v2_item_level.csv.gz"
PREPARED = PHASE1 / "modeling" / "prepared_v2"
SEQUENCES = PREPARED / "sequences.jsonl.gz"
VOCAB = PREPARED / "vocab.json"
SPLIT_REPORT = PREPARED / "split_report.json"
ARTIFACTS = PHASE2_ROOT / "artifacts"


def _find_text_embeddings() -> Path:
    """Locate the frozen sentence-embedding table, wherever it was rsynced to.

    It lives on LUMI at the path recorded in the run config
    (`/projappl/project_462001308/math_kt/embeddings/text_embeddings_v2.npz`)
    and is ~2 GB, so it gets copied back to whichever local directory had
    room rather than to one canonical spot. Both plausible landing sites are
    checked instead of forcing the file to be moved.
    """
    candidates = [
        PREPARED / "text_embeddings_v2.npz",
        ARTIFACTS / "text_embeddings_v2.npz",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]  # the canonical one, for the error message


# Variant D cannot run without this -- see frozen_model.py, which refuses to
# load rather than silently falling back to content_dim=0.
TEXT_EMBEDDINGS = _find_text_embeddings()

RUN_DIR = PHASE1 / "modeling" / "runs" / "skill_item_content_option"
CHECKPOINT = RUN_DIR / "best_model_joint.pt"
RUN_CONFIG = RUN_DIR / "config.json"

# `model.py` / `dataset.py` live in Phase 1 and are imported, not copied, so
# the architecture used at inference is byte-identical to the trained one.
PHASE1_MODELING = PHASE1 / "modeling"

# --- Phase-2 outputs ---------------------------------------------------
SKILL_CATALOG = ARTIFACTS / "skill_catalog.csv"
SKILL_ITEM_MAP = ARTIFACTS / "skill_item_map.csv"
STUDENT_SKILL = ARTIFACTS / "student_skill_first_encounter.csv.gz"
PREDICTIONS = ARTIFACTS / "predictions_d.npz"
CALIBRATION = ARTIFACTS / "conformal_calibration.json"
COVERAGE_REPORT = ARTIFACTS / "conformal_coverage_report.json"
KG_NODES = ARTIFACTS / "kg_nodes.csv"
KG_EDGES = ARTIFACTS / "kg_edges.csv"
KG_GRAPHML = ARTIFACTS / "skill_graph.graphml"


def require(path: Path, hint: str = "") -> Path:
    """Fail loudly and early on a missing frozen input.

    Phase 2's failure mode of concern is silent degradation, not crashes:
    `dataset.py` sets `content_dim = 0` when the embedding table is absent,
    which would build a structurally different model rather than erroring.
    Every consumer calls this first instead.
    """
    if not path.exists():
        msg = f"Required input not found: {path}"
        if hint:
            msg += f"\n  {hint}"
        raise FileNotFoundError(msg)
    return path
