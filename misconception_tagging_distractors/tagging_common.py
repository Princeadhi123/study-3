"""Shared utilities for LLM-assisted misconception tagging.

Used by both:
  - tag_distractors.py       -- validates the tag-then-verify approach against
                                 Eedi's labeled ground truth (closed-set: pick
                                 from a fixed, known misconception taxonomy).
  - tag_math_distractors.py  -- applies it to the real math-path distractor
                                 catalog (open-set: no fixed taxonomy exists,
                                 so misconceptions are elicited in free text
                                 and then clustered into a discovered taxonomy).

Kept dependency-light (numpy + openai + python-dotenv only) since this also
needs to run standalone on LUMI.
"""
import json
import os
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError

ROOT = Path(__file__).parent
PROJECT_ROOT = ROOT.parent
load_dotenv(PROJECT_ROOT / ".env")


def get_client() -> OpenAI:
    base_url = os.getenv("AITTA_BASE_URL")
    api_key = os.getenv("AITTA_API_KEY")
    if not base_url or not api_key or "paste-your" in api_key:
        raise SystemExit(
            "AITTA_BASE_URL / AITTA_API_KEY are not set. Edit the .env file at the "
            "project root with your Aitta token from https://aitta.csc.fi"
        )
    return OpenAI(base_url=base_url, api_key=api_key)


def default_model() -> str:
    return os.getenv("AITTA_MODEL", "openai/gpt-oss-120b")


def reasoning_effort_kwargs(effort: str) -> dict:
    """extra_body kwargs to cap a reasoning model's (e.g. gpt-oss) internal
    chain-of-thought length before it emits the actual answer -- measured on
    Aitta's openai/gpt-oss-120b: "low" cut latency ~2-4x (5.5s -> 2.4s on a
    realistic elicit prompt) and completion tokens by a similar factor vs.
    the (unset) default, with no observed drop in output format compliance
    or answer quality on spot checks. Silently ignored by non-reasoning
    models (confirmed against meta-llama/Llama-3.3-70B-Instruct: no error,
    just has no effect), so safe to always pass regardless of which model
    ends up handling the call."""
    return {"extra_body": {"reasoning_effort": effort}} if effort else {}


def call_with_retry(client, max_retries: int = 5, **kwargs):
    """Call chat.completions.create, retrying with exponential backoff on 429s
    (Aitta is a shared HPC service and will rate-limit bursts of concurrent
    requests; this lets a large batch job degrade gracefully instead of
    crashing partway through)."""
    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(**kwargs)
        except RateLimitError:
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)


def retrieval_margin(sims_row: np.ndarray, idx_sorted: np.ndarray) -> float:
    """Cosine-similarity gap between the top-1 and top-2 ranked candidates
    for one retrieval query. A cheap, no-extra-call confidence proxy: small
    margin means the top two candidates were nearly tied (ambiguous), large
    margin means the top pick clearly stood out. Used to triage which tags
    need a second (verification) pass or human review instead of treating
    every tag as equally trustworthy."""
    if len(idx_sorted) < 2:
        return 1.0
    return float(sims_row[idx_sorted[0]] - sims_row[idx_sorted[1]])


class JsonlCheckpoint:
    """Append-only checkpoint keyed by an arbitrary string key.

    A full tagging run is thousands of individual LLM calls (~10-15s each --
    see tag_distractors.py's docstring), so on a shared/time-limited cluster
    (LUMI SLURM job time limits, Aitta rate limits) a run WILL occasionally
    need to resume rather than restart. Every completed record is flushed to
    disk immediately, and already-done keys are skipped on the next run of
    the same checkpoint file, so no completed (and already-paid-for) LLM
    call is ever redone.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.done = {}
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # tolerate a partially-written last line after a hard kill
                    self.done[rec["key"]] = rec
        self._fh = self.path.open("a", encoding="utf-8")

    def __len__(self):
        return len(self.done)

    def has(self, key: str) -> bool:
        return key in self.done

    def get(self, key: str):
        return self.done.get(key)

    def write(self, key: str, record: dict):
        record = {"key": key, **record}
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self.done[key] = record

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def greedy_cluster(embeddings: np.ndarray, threshold: float = 0.82):
    """Single-pass greedy clustering by cosine similarity to a running
    cluster centroid: O(n * k) where k is the number of clusters formed so
    far, rather than O(n^2) full pairwise -- adequate for the thousands (not
    millions) of distinct elicited misconception descriptions this is meant
    for. `embeddings` rows must be L2-normalized (as sentence-transformers'
    `normalize_embeddings=True` already produces).

    Order matters (this is a greedy algorithm, not globally optimal): callers
    should feed rows in an order that puts the most-evidenced/most-reliable
    descriptions first if possible, so early clusters are well-anchored.

    Returns (cluster_id_per_row: np.ndarray[int], centroids: list[np.ndarray]).
    """
    centroids = []  # list of (centroid_vector, member_count)
    assignments = np.full(len(embeddings), -1, dtype=int)
    for i, vec in enumerate(embeddings):
        best_c, best_sim = -1, -1.0
        for c, (centroid, _count) in enumerate(centroids):
            sim = float(vec @ centroid)
            if sim > best_sim:
                best_sim, best_c = sim, c
        if best_c != -1 and best_sim >= threshold:
            centroid, count = centroids[best_c]
            new_centroid = (centroid * count + vec) / (count + 1)
            new_centroid = new_centroid / np.linalg.norm(new_centroid)
            centroids[best_c] = (new_centroid, count + 1)
            assignments[i] = best_c
        else:
            centroids.append((vec.copy(), 1))
            assignments[i] = len(centroids) - 1
    return assignments, [c for c, _count in centroids]


def nearest_two_centroids(vec: np.ndarray, centroids: list) -> tuple:
    """Return (best_idx, best_sim, second_best_sim) for one embedding against
    a finalized list of centroid vectors. second_best_sim is -1.0 if there is
    only one centroid. Used post-clustering to compute a per-row confidence
    margin (best_sim - second_best_sim) the same way retrieval_margin does
    for the closed-set case."""
    sims = np.array([float(vec @ c) for c in centroids])
    order = np.argsort(-sims)
    best_idx = int(order[0])
    best_sim = float(sims[order[0]])
    second_sim = float(sims[order[1]]) if len(order) > 1 else -1.0
    return best_idx, best_sim, second_sim
