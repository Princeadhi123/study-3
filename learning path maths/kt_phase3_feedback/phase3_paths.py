"""Canonical Phase 3 locations and safe access to Phase 2 artifacts."""
import sys
from pathlib import Path

PHASE3_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PHASE3_ROOT.parent
PHASE1_ROOT = PROJECT_ROOT / "kt_phase1"
PHASE2_ROOT = PROJECT_ROOT / "kt_phase2_inference"
PHASE2_ARTIFACTS = PHASE2_ROOT / "artifacts"
ARTIFACTS = PHASE3_ROOT / "artifacts"
SESSIONS = ARTIFACTS / "sessions"
APPROVED_BANK = PHASE2_ARTIFACTS / "test_question_bank_text_only_approved_v2.json"

# Phase 2 modules are the private source of truth for the bank validator,
# observed feed builder, and frozen-model loader. Phase 3 deliberately does
# not copy answer keys, checkpoints, embeddings, or those implementations.
if str(PHASE2_ROOT) not in sys.path:
    sys.path.insert(0, str(PHASE2_ROOT))


def require(path: Path, hint: str = "") -> Path:
    """Fail early when a required local artifact is absent."""
    if not path.exists():
        msg = f"Required input not found: {path}"
        if hint:
            msg += f"\n  {hint}"
        raise FileNotFoundError(msg)
    return path
