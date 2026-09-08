"""LLM distractor-tagging prototype against the Eedi misconception labels.

Pipeline:
  1. Retrieval: embed every misconception name (local sentence-transformers
     all-mpnet-base-v2, no API key needed) and every (question, wrong-answer)
     pair, then cosine-similarity-rank the top-K candidate misconceptions.
  2. Tagging: ask an LLM (CSC Aitta OpenAI-compatible endpoint) to pick the
     single best-matching MisconceptionId from that narrowed candidate list.
  3. Scoring: compare the LLM's pick against the ground-truth MisconceptionId
     already present in train.csv, and report top-1 accuracy + whether the
     true label was even inside the retrieved candidate set (retrieval
     recall@K), so you can tell prompt-wording problems apart from retrieval
     problems while iterating.

Scaling note: a misconception is a property of a (question, wrong-answer)
PAIR, not of an individual student attempt. When applying this to a real
student-log dataset with millions of interactions, you only ever need to
call the LLM once per unique (question_id, chosen wrong answer) pair -
typically thousands, not millions - then join that label back onto every
matching interaction. Measured throughput on Aitta: ~10-15s/call with a
top-50 candidate list (dominated by the model's own reasoning trace, not
network overhead), and the service rate-limits above ~10-15 concurrent
in-flight requests (429 at 20). This script parallelizes with a bounded
worker pool and retries on 429s so a large tagging job degrades gracefully
instead of crashing.

Usage:
  python tag_distractors.py --n-samples 50 --min-k 32 --max-k 50 --margin 0.05 --concurrency 8
"""
import argparse
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).parent
PROJECT_ROOT = ROOT.parent
DATA_DIR = PROJECT_ROOT / "eedi_data"
CACHE_DIR = ROOT / "misconception_cache"
CACHE_DIR.mkdir(exist_ok=True)
EMBED_MODEL_NAME = "all-mpnet-base-v2"

load_dotenv(PROJECT_ROOT / ".env")

OPTIONS = ["A", "B", "C", "D"]
QUERY_SEP = " [SEP] "


def build_long_distractors(train: pd.DataFrame) -> pd.DataFrame:
    """One row per (question, wrong-answer-option) that has a labeled misconception."""
    rows = []
    for _, r in train.iterrows():
        correct_opt = str(r["CorrectAnswer"]).strip()
        correct_text = r.get(f"Answer{correct_opt}Text", "")
        for opt in OPTIONS:
            if opt == correct_opt:
                continue
            mid = r.get(f"Misconception{opt}Id")
            if pd.isna(mid):
                continue
            rows.append({
                "QuestionId": r["QuestionId"],
                "ConstructName": r.get("ConstructName", ""),
                "SubjectName": r.get("SubjectName", ""),
                "QuestionText": r["QuestionText"],
                "CorrectAnswerText": correct_text,
                "WrongOption": opt,
                "WrongAnswerText": r.get(f"Answer{opt}Text", ""),
                "TrueMisconceptionId": int(mid),
            })
    return pd.DataFrame(rows)


def load_misconception_embeddings(embedder, mapping: pd.DataFrame) -> np.ndarray:
    cache_path = CACHE_DIR / f"misconception_embeddings_{EMBED_MODEL_NAME}.npy"
    if cache_path.exists():
        cached = np.load(cache_path)
        if len(cached) == len(mapping):
            return cached
    embeddings = embedder.encode(
        mapping["MisconceptionName"].tolist(),
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    np.save(cache_path, embeddings)
    return embeddings


def retrieve_candidates(query_emb: np.ndarray, misconception_emb: np.ndarray,
                         min_k: int = 50, max_k: int = 75, margin: float = 0.10) -> list[np.ndarray]:
    """Return, per query, the row indices into `mapping` for its candidate misconceptions.

    Dynamic cutoff: always take the top `min_k` candidates, then keep extending up to
    `max_k` only while each additional candidate's cosine similarity stays within
    `margin` of the top-ranked candidate for that query. This avoids padding the LLM's
    context with low-quality candidates just to hit a fixed quota - queries with one
    clear best match get a short list, ambiguous queries (many near-tied candidates)
    get up to `max_k`.

    Returns a list of length n_queries (not a 2D array, since each query can have a
    different number of candidates); this is a drop-in replacement everywhere the
    result is indexed per-query (`mapping.iloc[candidate_idx[i]]`), since DataFrame
    indexing doesn't care about a fixed candidate count.
    """
    sims = query_emb @ misconception_emb.T  # (n_queries, n_misconceptions), both L2-normalized
    order = np.argsort(-sims, axis=1)
    candidates = []
    for i in range(sims.shape[0]):
        idx_sorted = order[i]
        top_sim = sims[i, idx_sorted[0]]
        selected = list(idx_sorted[:min_k])
        for j in range(min_k, max_k):
            cand_idx = idx_sorted[j]
            if sims[i, cand_idx] >= top_sim - margin:
                selected.append(cand_idx)
            else:
                break  # sims are sorted descending, so no later candidate can qualify either
        candidates.append(np.array(selected))
    return candidates


def build_construct_misconception_index(long_df: pd.DataFrame) -> dict:
    """ConstructName -> {MisconceptionId: one QuestionId it came from}.

    The same fine-grained skill (ConstructName) tends to produce the same
    recurring misconceptions across different questions, so misconceptions
    already known to occur for a construct are strong additional retrieval
    candidates on top of pure embedding similarity. Tested on a 200-sample
    holdout: raw embedding recall@50 was 68%; adding this boost (with each
    row's own QuestionId excluded from its own boost set, so it can't just
    read off its own answer) raised it to 77.5%, using only ~5 extra
    candidates on average - cheap since it's a local dict lookup, no extra
    embedding or LLM calls.
    """
    index = {}
    for _, r in long_df.iterrows():
        index.setdefault(r["ConstructName"], {})[r["TrueMisconceptionId"]] = r["QuestionId"]
    return index


def boosted_candidates(mapping: pd.DataFrame, cand_rows: pd.DataFrame, construct_index: dict,
                        construct_name: str, exclude_question_id) -> pd.DataFrame:
    """Merge embedding-retrieved candidates with construct-boosted candidates, deduped."""
    boost_ids = {
        mid for mid, qid in construct_index.get(construct_name, {}).items()
        if qid != exclude_question_id
    }
    boost_ids -= set(cand_rows["MisconceptionId"].values)
    if not boost_ids:
        return cand_rows
    extra_rows = mapping[mapping["MisconceptionId"].isin(boost_ids)]
    return pd.concat([cand_rows, extra_rows], ignore_index=True)


# Illustrative worked examples of expert diagnostic reasoning, shown to the LLM as
# few-shot context. The example IDs (101/205/318) are fictional placeholders - only
# the reasoning pattern and the "reason first, then output the bare ID" format matter,
# since the real candidate lists in this dataset always contain the true numeric
# MisconceptionIds, not these examples' IDs.
FEWSHOT_EXAMPLES = """### Worked Example 1
Question: Simplify 3/4 + 1/2
Correct answer: 5/4
Student's wrong answer: 4/6
Candidate misconceptions (id: name):
101: Adds numerators and denominators separately when adding fractions
205: Believes a fraction can be simplified by dividing numerator and denominator by different numbers
318: Confuses "simplify" with "convert to a decimal"
Reasoning: 4/6 comes from 3+1=4 on top and 4+2=6 on the bottom - the student added the
numerators and denominators straight across instead of finding a common denominator
first. That is a textbook "add across" fraction error, not a simplification or
decimal-conversion error.
MisconceptionId: 101

### Worked Example 2
Question: Work out -3 - (-5)
Correct answer: 2
Student's wrong answer: -8
Candidate misconceptions (id: name):
101: Adds numerators and denominators separately when adding fractions
205: Believes subtracting a negative number is the same as subtracting a positive number
318: Believes multiplying two negatives gives a negative
Reasoning: -8 = -3 - 5, so the student treated "- (-5)" as "- 5", ignoring that
subtracting a negative flips the sign to addition. This is a sign-error misconception
about double negatives, not a multiplication rule.
MisconceptionId: 205

### Worked Example 3
Question: Order 0.3, 0.25, 0.4 from smallest to largest
Correct answer: 0.25, 0.3, 0.4
Student's wrong answer: 0.4, 0.3, 0.25
Candidate misconceptions (id: name):
101: Adds numerators and denominators separately when adding fractions
205: Believes subtracting a negative number is the same as subtracting a positive number
318: Compares decimals by treating the digits after the point as a whole number, so more digits means a bigger value
Reasoning: The student ranked 0.25 as smallest despite it being larger than nothing
else obviously - actually they ranked by reading ".4" > ".3" > ".25" as if ".25" were
twenty-five and ".4" were four, i.e. comparing digit-strings rather than place value.
That matches the decimal-length misconception, not a fraction or sign error.
MisconceptionId: 318
"""


def build_prompt(row: pd.Series, candidates: pd.DataFrame) -> list[dict]:
    options_block = "\n".join(
        f"{c.MisconceptionId}: {c.MisconceptionName}" for c in candidates.itertuples()
    )
    system = (
        "You are a maths education expert. A student answered a multiple-choice "
        "maths question incorrectly. Given the question, the correct answer, the "
        "student's wrong answer, and a list of candidate misconceptions, identify "
        "which misconception ID best explains why the student chose that wrong answer. "
        "Reason briefly about the arithmetic/algebraic error the wrong answer implies, "
        "the way an expert does in the worked examples below, then respond with ONLY "
        "the numeric misconception ID from the candidate list on its own line, and "
        "nothing else. If none of the candidates fit, respond with NONE.\n\n"
        f"{FEWSHOT_EXAMPLES}"
    )
    user = (
        f"Question:\n{row.QuestionText}\n\n"
        f"Correct answer: {row.CorrectAnswerText}\n"
        f"Student's wrong answer: {row.WrongAnswerText}\n\n"
        f"Candidate misconceptions (id: name):\n{options_block}\n\n"
        "MisconceptionId:"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_prediction(text: str | None) -> int | None:
    if not text:
        return None
    text = text.strip()
    if text.upper().startswith("NONE"):
        return None

    # Prefer an explicit labelled answer, because reasoning may contain arithmetic
    # values and other misconception IDs before the final answer.
    labelled = re.findall(r"MisconceptionId\s*:\s*(-?\d+)", text, flags=re.IGNORECASE)
    if labelled:
        return int(labelled[-1])
    if re.search(r"MisconceptionId\s*:\s*NONE\b", text, flags=re.IGNORECASE):
        return None

    # Keep a fallback for concise responses that contain only the numeric ID, or
    # responses that omit the label despite the instruction.
    matches = re.findall(r"-?\d+", text)
    return int(matches[-1]) if matches else None


def call_with_retry(client, max_retries=5, **kwargs):
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


def tag_one(client, model, temperature, max_tokens, row, cand_rows, mapping):
    messages = build_prompt(row, cand_rows)
    response = call_with_retry(
        client, model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
    )
    message = response.choices[0].message
    raw_text = message.content
    reasoning = getattr(message, "reasoning", None)
    predicted_id = parse_prediction(raw_text)

    true_id = row.TrueMisconceptionId
    in_candidates = true_id in cand_rows["MisconceptionId"].values
    return {
        "QuestionId": row.QuestionId,
        "WrongOption": row.WrongOption,
        "TrueMisconceptionId": true_id,
        "TrueMisconceptionName": mapping.loc[mapping.MisconceptionId == true_id, "MisconceptionName"].iloc[0],
        "PredictedMisconceptionId": predicted_id,
        "RawResponse": raw_text,
        "Reasoning": reasoning,
        "FinishReason": response.choices[0].finish_reason,
        "Correct": predicted_id == true_id,
        "TrueLabelInCandidates": in_candidates,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-samples", type=int, default=50, help="Number of labeled distractors to test on")
    parser.add_argument("--min-k", type=int, default=50,
                         help="Minimum number of retrieved candidate misconceptions always shown to the LLM")
    parser.add_argument("--max-k", type=int, default=75,
                         help="Maximum number of retrieved candidates, if enough are within --margin of the top match")
    parser.add_argument("--margin", type=float, default=0.10,
                         help="Cosine-similarity margin below the top candidate's score within which "
                              "candidates beyond --min-k are still included")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--model", default=os.getenv("AITTA_MODEL", "openai/gpt-oss-120b"))
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=1500,
                         help="gpt-oss is a reasoning model: it spends tokens on an internal "
                              "reasoning trace before emitting the final answer, so this needs "
                              "much more headroom than the final answer's own length.")
    parser.add_argument("--concurrency", type=int, default=8,
                         help="Parallel in-flight requests to Aitta. Measured safe up to "
                              "~10-15; 20 triggered 429 rate-limit errors in testing.")
    parser.add_argument("--out", default=str(ROOT / "misconception_cache" / "tagging_results.csv"))
    args = parser.parse_args()

    base_url = os.getenv("AITTA_BASE_URL")
    api_key = os.getenv("AITTA_API_KEY")
    if not base_url or not api_key or "paste-your" in api_key:
        raise SystemExit(
            "AITTA_BASE_URL / AITTA_API_KEY are not set. Edit the .env file at the "
            "project root with your Aitta token from https://aitta.csc.fi"
        )
    client = OpenAI(base_url=base_url, api_key=api_key)

    print("Loading data...")
    train = pd.read_csv(DATA_DIR / "train.csv")
    mapping = pd.read_csv(DATA_DIR / "misconception_mapping.csv")

    long_df = build_long_distractors(train)
    print(f"{len(long_df)} labeled (question, wrong-answer) pairs available")

    sample = long_df.sample(n=min(args.n_samples, len(long_df)), random_state=args.seed).reset_index(drop=True)

    print(f"Loading local embedder ({EMBED_MODEL_NAME})...")
    embedder = SentenceTransformer(EMBED_MODEL_NAME)
    misconception_emb = load_misconception_embeddings(embedder, mapping)

    print("Embedding query pairs for retrieval...")
    # Including SubjectName + ConstructName (the specific maths skill being tested)
    # noticeably improves retrieval recall over question+answer text alone, since
    # misconception names are terse and benefit from that extra topical context.
    # Measured on a 200-sample holdout: MiniLM+construct-only recall@25 was 53%;
    # mpnet+subject+construct recall@50 reached 68%.
    queries = (
        sample["SubjectName"] + QUERY_SEP + sample["ConstructName"] + QUERY_SEP
        + sample["QuestionText"] + QUERY_SEP + sample["WrongAnswerText"]
    ).tolist()
    query_emb = embedder.encode(queries, normalize_embeddings=True, show_progress_bar=True)
    candidate_idx = retrieve_candidates(query_emb, misconception_emb, args.min_k, args.max_k, args.margin)
    avg_k = np.mean([len(c) for c in candidate_idx])
    print(f"Dynamic retrieval: avg {avg_k:.1f} candidates/query (min_k={args.min_k}, max_k={args.max_k}, margin={args.margin})")

    print("Building construct->misconception boost index...")
    construct_index = build_construct_misconception_index(long_df)

    print(f"Tagging {len(sample)} pairs with concurrency={args.concurrency}...")
    t0 = time.time()
    results = [None] * len(sample)
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        future_to_i = {
            executor.submit(
                tag_one, client, args.model, args.temperature, args.max_tokens,
                row,
                boosted_candidates(mapping, mapping.iloc[candidate_idx[i]], construct_index,
                                    row.ConstructName, row.QuestionId),
                mapping,
            ): i
            for i, row in sample.iterrows()
        }
        done = 0
        for future in as_completed(future_to_i):
            i = future_to_i[future]
            result = future.result()
            results[i] = result
            done += 1
            status = "OK  " if result["Correct"] else ("MISS" if result["TrueLabelInCandidates"] else "RETRIEVAL-MISS")
            print(f"[{done}/{len(sample)}] {status}  true={result['TrueMisconceptionId']} pred={result['PredictedMisconceptionId']}")
    elapsed = time.time() - t0
    print(f"Tagged {len(sample)} pairs in {elapsed:.1f}s ({elapsed / len(sample):.1f}s/pair effective)")

    results_df = pd.DataFrame(results)
    Path(args.out).parent.mkdir(exist_ok=True)
    results_df.to_csv(args.out, index=False, encoding="utf-8-sig")

    top1_acc = results_df["Correct"].mean()
    recall_at_k = results_df["TrueLabelInCandidates"].mean()
    print("\n=== Summary ===")
    print(f"Samples tested:        {len(results_df)}")
    print(f"Top-1 accuracy:        {top1_acc:.1%}")
    print(f"Retrieval recall (dynamic top-{args.min_k}-{args.max_k}@margin={args.margin} + construct-boost): {recall_at_k:.1%}  "
          f"(upper bound on top-1 acc given this retrieval step)")
    print(f"Wrote detailed results to {args.out}")


if __name__ == "__main__":
    main()
