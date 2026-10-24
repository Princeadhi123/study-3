import copy
import hashlib
import json
from pathlib import Path

import phase3_paths


SCHEMA = "phase3_explicit_artifact_recovery_v1"
ORIGINAL_POOL_SHA256 = "da12e7195149e1b7d92b2d2363b9b9a7f16de98af003e81387a333ebc77c38c0"
ORIGINAL_POOL_CANONICAL_SHA256 = "ef0d10ced42dcac13e2bf326bb305b0d287900f8ceae3c05fcac7ff5de5c46a4"


def _local_path(value):
    relative = Path(value)
    root = phase3_paths.PHASE3_ROOT.resolve()
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Recovery paths must be relative to the Phase 3 directory")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Recovery path escapes the Phase 3 directory")
    return path


def activate_recovery(config_path):
    import research_runtime
    from research_content_graph import load_capture

    if research_runtime._capture.cache_info().currsize or research_runtime._bound_bank.cache_info().currsize:
        raise ValueError("Select recovery configuration before loading research data")
    raw = Path(config_path).read_bytes()
    config = json.loads(raw.decode("utf-8"))
    required = {
        "schema", "status", "practice_pool", "practice_pool_sha256",
        "practice_pool_canonical_sha256", "original_practice_pool_sha256",
        "graph_capture", "graph_sha256", "replay_source", "replay_report",
        "runtime_root",
    }
    if set(config) != required or config["schema"] != SCHEMA:
        raise ValueError("Invalid explicit recovery configuration")
    if config["status"] != "reconstructed_inputs_historical_outputs_not_recovered":
        raise ValueError("Recovery configuration must disclose unrecovered history")
    if (config["original_practice_pool_sha256"] != ORIGINAL_POOL_SHA256
            or config["practice_pool_canonical_sha256"] != ORIGINAL_POOL_CANONICAL_SHA256):
        raise ValueError("Recovery configuration does not reference the recorded original data")
    paths = {
        key: _local_path(config[key])
        for key in ("practice_pool", "graph_capture", "replay_source", "replay_report", "runtime_root")
    }
    pool_raw = paths["practice_pool"].read_bytes()
    pool = json.loads(pool_raw.decode("utf-8"))
    canonical = json.dumps(
        pool, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(pool_raw).hexdigest() != config["practice_pool_sha256"]:
        raise ValueError("Recovery practice file changed")
    if hashlib.sha256(canonical).hexdigest() != ORIGINAL_POOL_CANONICAL_SHA256:
        raise ValueError("Recovery practice records differ from the original data")
    graph_path = paths["graph_capture"] / "content_graph_private.json"
    if hashlib.sha256(graph_path.read_bytes()).hexdigest() != config["graph_sha256"]:
        raise ValueError("Recovery content graph changed")
    graph = load_capture(paths["graph_capture"])
    questions = [
        exercise["question"] for exercise in graph["exercises"]
        if exercise["source_id"] == "practice_pool_v2"
    ]
    if questions != pool["questions"]:
        raise ValueError("Recovery graph does not contain the recovered practice records")
    metadata = {
        "schema": SCHEMA,
        "status": config["status"],
        "config_sha256": hashlib.sha256(raw).hexdigest(),
        "original_practice_pool_sha256": ORIGINAL_POOL_SHA256,
        "practice_pool_canonical_sha256": ORIGINAL_POOL_CANONICAL_SHA256,
        "practice_pool_sha256": config["practice_pool_sha256"],
        "graph_sha256": config["graph_sha256"],
        "notice": (
            "Recovery configuration: assessment banks and practice records were "
            "verified against surviving original hashes. Graph manifests and the "
            "offline scenario library were reconstructed. Deleted historical "
            "hosted outputs and sessions have not been recovered."
        ),
    }
    research_runtime.DEFAULT_CAPTURE = paths["graph_capture"]
    research_runtime.POOL_PATH = paths["practice_pool"]
    research_runtime.POOL_HASH = config["practice_pool_sha256"]
    research_runtime.RECOVERY_PROVENANCE = copy.deepcopy(metadata)
    return {**paths, "provenance": metadata}
