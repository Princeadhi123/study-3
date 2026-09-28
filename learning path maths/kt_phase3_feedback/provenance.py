"""Provenance records for the Phase 3 prototype.

The manifest identifies the exact bank/model inputs used in a run without
copying private answer keys or multi-GB frozen artifacts into Phase 3.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import phase3_paths

MIB = 1024 * 1024


def sha256_file(path: Path, limit_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    remaining = limit_bytes
    with Path(path).open("rb") as stream:
        while True:
            size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            if size <= 0:
                break
            chunk = stream.read(size)
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return digest.hexdigest()


def file_record(path: Path, role: str, hash_mode: str = "full") -> dict:
    path = phase3_paths.require(Path(path))
    record = {
        "role": role,
        "path": str(path),
        "size_bytes": path.stat().st_size,
    }
    if hash_mode == "full":
        record["sha256"] = sha256_file(path)
    elif hash_mode == "prefix_1mib":
        record["sha256_first_mib"] = sha256_file(path, MIB)
    else:
        raise ValueError("hash_mode must be 'full' or 'prefix_1mib'")
    return record


def manifest(bank_path: Path | None = None) -> dict:
    phase3_paths.require(phase3_paths.PHASE2_ROOT / "paths.py")
    import paths as phase2_paths

    bank_path = bank_path or phase3_paths.APPROVED_BANK
    return {
        "schema": "phase3_provenance_v1",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "bank": file_record(bank_path, "approved MCQ bank"),
        "checkpoint": file_record(phase2_paths.CHECKPOINT, "frozen variant-D checkpoint"),
        "run_config": file_record(phase2_paths.RUN_CONFIG, "frozen run config"),
        "vocab": file_record(phase2_paths.VOCAB, "skill/item vocabulary"),
        "embeddings": file_record(
            phase2_paths.TEXT_EMBEDDINGS,
            "frozen text embeddings",
            hash_mode="prefix_1mib",
        ),
    }


def write_manifest(out: Path, bank_path: Path | None = None) -> dict:
    data = manifest(bank_path)
    with Path(out).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
    return data
