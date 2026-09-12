"""Open-set LLM misconception tagging for the math-path distractor catalog.

Unlike tag_distractors.py (which validates the approach against Eedi's
*known*, fixed misconception taxonomy -- a closed-set "pick the best of
these 2500 candidates" problem), the math-path data
(kt_phase1/reports_v2/distractor_catalog.csv) has no such taxonomy: nobody
has ever labeled what conceptual error each wrong answer represents. So this
script builds one bottom-up:

  1. elicit   -- for every (item_id, wrong option_value) pair, ask the LLM to
                 describe the likely misconception in a short free-text
                 phrase (using the question text + correct answer from
                 reports_v2/item_context.csv for grounding). Processed
                 exercise_type by exercise_type, most-impactful type first
                 (by total times_selected), and within each type, highest-
                 times_selected distractors first -- so tagging effort (and
                 your later review time) goes where it matters most, and you
                 can inspect/stop after any one type before continuing.
  2. cluster  -- embed every elicited description (local sentence-
                 transformer, no API calls) and greedily cluster by cosine
                 similarity into a discovered taxonomy: each cluster becomes
                 one misconception_id, with the member closest to the
                 cluster centroid used as its canonical misconception_label.
  3. verify   -- flag low-confidence assignments (small clusters, or a small
                 margin between the assigned cluster and the next-nearest
                 one -- see tagging_common.retrieval_margin) and ask the LLM
                 a second, differently-framed critique question: given the
                 assigned label AND the nearest alternative label, does the
                 assignment actually fit, or is a different/new description
                 more accurate? This is deliberately not just "confirm
                 yes/no" -- see the project discussion on why a second LLM
                 pass needs new information, not just a repeat, to be worth
                 anything.
  4. export   -- write the tagged catalog + a human review queue (sorted by
                 impact) + a cluster summary for a fast human sanity pass.

Every stage is checkpointed (tagging_common.JsonlCheckpoint) so a run can be
safely resumed (SLURM time limit, rate limit, laptop closed) without redoing
already-completed LLM calls.

Usage (typically on LUMI, see run_lumi_tagging.sh):
  python tag_math_distractors.py --stage all \\
      --distractor-catalog "../learning path maths/kt_phase1/reports_v2/distractor_catalog.csv" \\
      --item-context      "../learning path maths/kt_phase1/reports_v2/item_context.csv" \\
      --out-dir math_tagging_out

Smoke test locally, without hitting the LLM for everything, with --limit:
  python tag_math_distractors.py --stage elicit --limit 5 --types MATH_DRILLER
"""
import argparse
import csv
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
from sentence_transformers import SentenceTransformer

from tagging_common import (
    JsonlCheckpoint,
    call_with_retry,
    default_model,
    get_client,
    greedy_cluster,
    nearest_two_centroids,
    reasoning_effort_kwargs,
)

ROOT = Path(__file__).parent
EMBED_MODEL_NAME = "all-mpnet-base-v2"

# Illustrative worked examples of the *open-set* elicitation task (no fixed
# candidate list -- the model must name the error itself). Same worked
# arithmetic errors as tag_distractors.py's FEWSHOT_EXAMPLES, reused here for
# consistency between the two tagging approaches, reframed for free-text
# output instead of ID selection.
ELICIT_FEWSHOT = """### Worked Example 1
Question: Simplify 3/4 + 1/2
Correct answer: 5/4
Student's wrong answer: 4/6
Reasoning: 4/6 comes from 3+1=4 on top and 4+2=6 on the bottom -- the student
added the numerators and denominators straight across instead of finding a
common denominator first.
Misconception: Adds numerators and denominators separately instead of finding a common denominator

### Worked Example 2
Question: Work out -3 - (-5)
Correct answer: 2
Student's wrong answer: -8
Reasoning: -8 = -3 - 5, so the student treated "- (-5)" as "- 5", ignoring
that subtracting a negative flips the sign to addition.
Misconception: Treats subtracting a negative number the same as subtracting a positive number

### Worked Example 3
Question: Order 0.3, 0.25, 0.4 from smallest to largest
Correct answer: 0.25, 0.3, 0.4
Student's wrong answer: 0.4, 0.3, 0.25
Reasoning: The student ranked ".4" > ".3" > ".25" as if longer decimal
strings were smaller, i.e. compared digit-strings rather than place value.
Misconception: Compares decimals by digit-string length instead of place value
"""

VERIFY_FEWSHOT = """### Worked Example
Question: Simplify 3/4 + 1/2
Correct answer: 5/4
Student's wrong answer: 4/6
Assigned label: Adds numerators and denominators separately instead of finding a common denominator
Alternative label: Believes a fraction can be simplified by dividing numerator and denominator by different numbers
Reasoning: 4/6 is exactly 3+1 over 4+2 -- a straight "add across" error, not a
simplification error (nothing was simplified/divided here).
Verdict: CONFIRM
"""


def load_distractors(catalog_path: str, item_context_path: str, min_times_selected: int) -> pd.DataFrame:
    catalog = pd.read_csv(catalog_path, encoding="utf-8-sig")
    context = pd.read_csv(item_context_path, encoding="utf-8-sig")
    wrong = catalog[catalog["is_correct_option"] == 0].copy()
    wrong = wrong[wrong["times_selected"] >= min_times_selected]
    merged = wrong.merge(context[["item_id", "text", "correct_option_value", "skill_name"]],
                          on="item_id", how="left")
    merged["context_missing"] = merged["text"].isna()
    merged["text"] = merged["text"].fillna("")
    merged["correct_option_value"] = merged["correct_option_value"].fillna("")
    merged["skill_name"] = merged["skill_name"].fillna("")
    return merged


def ordered_types(df: pd.DataFrame, types_filter: list) -> list:
    impact = df.groupby("exercise_type")["times_selected"].sum().sort_values(ascending=False)
    types = list(impact.index)
    if types_filter:
        types = [t for t in types_filter if t in types]
    return types


def build_elicit_prompt(row) -> list:
    if row["context_missing"]:
        context_note = (
            "(Question text is unavailable for this item -- reason as best you can from "
            "the wrong answer alone, and say UNKNOWN if there truly isn't enough information.)"
        )
        question_block = f"Exercise type: {row['exercise_type']}\n{context_note}"
        correct_block = "(unknown)"
    else:
        question_block = f"Question:\n{row['text']}"
        correct_block = row["correct_option_value"]
    system = (
        "You are a maths education expert. A student answered a maths question incorrectly. "
        "Given the question, the correct answer, and the student's wrong answer, briefly reason "
        "about the arithmetic/algebraic/conceptual error the wrong answer implies (one or two "
        "sentences), then state the misconception as a SHORT (<= 15 words) general phrase that "
        "would apply to any student making the same kind of error -- not specific to this "
        "question's numbers. Respond in exactly this format:\n"
        "Reasoning: <your reasoning>\nMisconception: <short general phrase, or UNKNOWN>\n\n"
        f"{ELICIT_FEWSHOT}"
    )
    user = (
        f"{question_block}\n\n"
        f"Correct answer: {correct_block}\n"
        f"Student's wrong answer: {row['option_value']}\n\n"
        "Reasoning:"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_elicit_response(text: str) -> tuple:
    if not text:
        return None, None
    reasoning_match = re.search(r"Reasoning\s*:\s*(.*?)(?=\nMisconception\s*:|\Z)", text,
                                 flags=re.IGNORECASE | re.DOTALL)
    label_match = re.search(r"Misconception\s*:\s*(.*)", text, flags=re.IGNORECASE)
    reasoning = reasoning_match.group(1).strip() if reasoning_match else None
    label = label_match.group(1).strip() if label_match else None
    if label and label.upper().startswith("UNKNOWN"):
        label = None
    return label, reasoning


def elicit_needs_retry(rec) -> bool:
    """True if a checkpointed elicit record has no usable label AND isn't an
    explicit "Misconception: UNKNOWN" -- i.e. the call actually failed
    (empty response, or truncated before it reached the Misconception line,
    both seen in practice with reasoning models burning their whole token
    budget on hidden chain-of-thought) rather than the model genuinely
    having nothing to go on. Without this distinction both cases look
    identical (label: null) and a resume would skip real failures forever."""
    if rec is None:
        return True
    if rec.get("label"):
        return False
    return not re.search(r"Misconception\s*:\s*UNKNOWN", rec.get("raw") or "", flags=re.IGNORECASE)


def stage_elicit(args):
    client = get_client()
    model = args.model
    df = load_distractors(args.distractor_catalog, args.item_context, args.min_times_selected)
    types = ordered_types(df, args.types.split(",") if args.types else None)
    print(f"{len(df):,} wrong-answer distractors to elicit, across {len(types)} exercise types "
          f"(processing order, most-impactful first): {types}")

    with JsonlCheckpoint(args.checkpoint_dir + "/elicit.jsonl") as ckpt:
        for etype in types:
            sub = df[df["exercise_type"] == etype].sort_values("times_selected", ascending=False)
            if args.limit:
                sub = sub.head(args.limit)
            todo = [r for _, r in sub.iterrows()
                    if elicit_needs_retry(ckpt.get(f"{r['item_id']}\t{r['option_value']}"))]
            n_retries = sum(1 for r in todo if ckpt.has(f"{r['item_id']}\t{r['option_value']}"))
            print(f"[{etype}] {len(sub)} distractors, {len(todo)} not yet elicited "
                  f"({n_retries} of those are retries of a previously failed/truncated call) "
                  f"(impact = {sub['times_selected'].sum():,} total selections)")
            t0 = time.time()
            # Only the network call runs in worker threads; every checkpoint
            # write happens back on the main thread as futures complete, so
            # JsonlCheckpoint's single-writer append-and-fsync stays safe
            # without needing its own lock.
            with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                future_to_row = {}
                for row in todo:
                    key = f"{row['item_id']}\t{row['option_value']}"
                    # A prior failure at this key means the call ran out of
                    # max_tokens (often the reasoning model's hidden
                    # chain-of-thought ate the whole budget) before ever
                    # reaching the answer -- give retries more room instead
                    # of just repeating the same failure.
                    tokens = args.retry_max_tokens if ckpt.has(key) else args.max_tokens
                    future = executor.submit(call_with_retry, client, model=model,
                                              messages=build_elicit_prompt(row), temperature=0.0,
                                              max_tokens=tokens, **reasoning_effort_kwargs(args.reasoning_effort))
                    future_to_row[future] = row
                done = 0
                for future in as_completed(future_to_row):
                    row = future_to_row[future]
                    response = future.result()
                    raw_text = response.choices[0].message.content
                    label, reasoning = parse_elicit_response(raw_text)
                    key = f"{row['item_id']}\t{row['option_value']}"
                    ckpt.write(key, {
                        "item_id": row["item_id"], "option_value": row["option_value"],
                        "exercise_type": etype, "times_selected": int(row["times_selected"]),
                        "context_missing": bool(row["context_missing"]),
                        "label": label, "reasoning": reasoning, "raw": raw_text,
                    })
                    done += 1
                    if done % 25 == 0 or done == len(todo):
                        elapsed = time.time() - t0
                        print(f"  [{etype}] {done}/{len(todo)} elicited ({elapsed:.0f}s elapsed, "
                              f"concurrency={args.concurrency})")
    print(f"Elicitation checkpoint: {args.checkpoint_dir}/elicit.jsonl")


def stage_cluster(args):
    ckpt = JsonlCheckpoint(args.checkpoint_dir + "/elicit.jsonl")
    records = [r for r in ckpt.done.values() if r.get("label")]
    skipped_unknown = len(ckpt.done) - len(records)
    if not records:
        raise SystemExit("No elicited labels with a non-UNKNOWN misconception found -- run --stage elicit first.")
    # Highest-impact descriptions anchor clusters first (greedy clustering is
    # order-sensitive), so common misconceptions form solid, well-evidenced
    # clusters before rarer variants are folded in or spun off as new ones.
    records.sort(key=lambda r: -r["times_selected"])

    print(f"Clustering {len(records)} elicited labels ({skipped_unknown} UNKNOWN skipped) "
          f"with {EMBED_MODEL_NAME}...")
    embedder = SentenceTransformer(EMBED_MODEL_NAME)
    embeddings = embedder.encode([r["label"] for r in records], normalize_embeddings=True,
                                  show_progress_bar=True)
    assignments, centroids = greedy_cluster(embeddings, threshold=args.cluster_threshold)
    n_clusters = len(centroids)
    print(f"Formed {n_clusters} clusters from {len(records)} labels (threshold={args.cluster_threshold})")

    cluster_members = defaultdict(list)
    for rec, cid, emb in zip(records, assignments, embeddings):
        cluster_members[int(cid)].append((rec, emb))

    cluster_label = {}
    for cid, members in cluster_members.items():
        centroid = centroids[cid]
        best = max(members, key=lambda m: float(m[1] @ centroid))
        cluster_label[cid] = best[0]["label"]

    # Unlike elicit/verify, clustering is a full recompute every time (no
    # per-item LLM call to avoid redoing), so start cluster.jsonl fresh
    # instead of appending -- otherwise every re-run (e.g. while tuning
    # --cluster-threshold) leaves the previous run's now-stale rows in the
    # file, duplicating every key that appears in both runs.
    cluster_path = Path(args.checkpoint_dir + "/cluster.jsonl")
    if cluster_path.exists():
        cluster_path.unlink()
    with JsonlCheckpoint(cluster_path) as out:
        for rec, cid, emb in zip(records, assignments, embeddings):
            best_idx, best_sim, second_sim = nearest_two_centroids(emb, centroids)
            margin = best_sim - second_sim if second_sim >= 0 else 1.0
            key = f"{rec['item_id']}\t{rec['option_value']}"
            out.write(key, {
                "item_id": rec["item_id"], "option_value": rec["option_value"],
                "exercise_type": rec["exercise_type"], "times_selected": rec["times_selected"],
                "elicited_label": rec["label"],
                "misconception_id": f"MC_{cid:05d}",
                "misconception_label": cluster_label[cid],
                "cluster_size": len(cluster_members[cid]),
                "confidence_margin": margin,
                "context_missing": rec["context_missing"],
            })
    print(f"Cluster assignments: {args.checkpoint_dir}/cluster.jsonl")


def needs_review(rec, small_cluster_threshold, margin_threshold) -> bool:
    return (
        rec["context_missing"]
        or rec["cluster_size"] <= small_cluster_threshold
        or rec["confidence_margin"] < margin_threshold
    )


def build_verify_prompt(elicit_rec, cluster_rec, alt_label: str) -> list:
    if elicit_rec.get("context_missing"):
        question_block = f"Exercise type: {cluster_rec['exercise_type']}\n(question text unavailable)"
        correct_block = "(unknown)"
    else:
        question_block = f"Question:\n{elicit_rec.get('_text', '')}"
        correct_block = elicit_rec.get("_correct", "")
    system = (
        "You are a maths education expert reviewing an automatically-clustered misconception "
        "label. Given the question, correct answer, wrong answer, the misconception label it was "
        "assigned, and the nearest alternative label it could have been assigned instead, decide "
        "whether the assignment is correct. Reason briefly, then respond in exactly this format:\n"
        "Reasoning: <your reasoning>\nVerdict: CONFIRM or REPLACE: <corrected short phrase>\n\n"
        f"{VERIFY_FEWSHOT}"
    )
    user = (
        f"{question_block}\n\n"
        f"Correct answer: {correct_block}\n"
        f"Student's wrong answer: {cluster_rec['option_value']}\n"
        f"Assigned label: {cluster_rec['misconception_label']}\n"
        f"Alternative label: {alt_label}\n\n"
        "Reasoning:"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def stage_verify(args):
    cluster_ckpt = JsonlCheckpoint(args.checkpoint_dir + "/cluster.jsonl")
    elicit_ckpt = JsonlCheckpoint(args.checkpoint_dir + "/elicit.jsonl")
    df_ctx = pd.read_csv(args.item_context, encoding="utf-8-sig")[["item_id", "text", "correct_option_value"]]
    ctx_lookup = {r.item_id: (r.text, r.correct_option_value) for r in df_ctx.itertuples()}

    all_recs = list(cluster_ckpt.done.values())
    by_label = defaultdict(list)
    for r in all_recs:
        by_label[r["misconception_label"]].append(r)
    flagged = [r for r in all_recs if needs_review(r, args.small_cluster_threshold, args.verify_margin)]
    flagged.sort(key=lambda r: -r["times_selected"])
    if args.limit:
        flagged = flagged[:args.limit]
    print(f"{len(flagged)}/{len(all_recs)} assignments flagged for verification "
          f"(small cluster <= {args.small_cluster_threshold}, or margin < {args.verify_margin}, or missing context)")

    client = get_client()
    verify_model = args.verify_model or args.model
    if verify_model != args.model:
        print(f"Verify pass using {verify_model} (elicit pass used {args.model})")
    # Nearest alternative label per flagged item, approximated by the label
    # of the largest OTHER cluster within the same exercise_type (a cheap
    # proxy -- avoids re-embedding/re-searching centroids here). Keep the
    # biggest cluster_size seen per label so "largest" can actually be
    # ranked (a plain set here would pick an arbitrary, hash-order label).
    type_label_size = defaultdict(dict)
    for r in all_recs:
        sizes = type_label_size[r["exercise_type"]]
        sizes[r["misconception_label"]] = max(sizes.get(r["misconception_label"], 0), r["cluster_size"])

    with JsonlCheckpoint(args.checkpoint_dir + "/verify.jsonl") as out:
        todo = []
        for rec in flagged:
            key = f"{rec['item_id']}\t{rec['option_value']}"
            if out.has(key):
                continue
            sizes = type_label_size[rec["exercise_type"]]
            others = sorted((l for l in sizes if l != rec["misconception_label"]), key=lambda l: -sizes[l])
            alt_label = others[0] if others else "(no alternative candidates for this exercise type)"
            elicit_rec = dict(elicit_ckpt.get(key) or {})
            text, correct_val = ctx_lookup.get(rec["item_id"], ("", ""))
            elicit_rec["_text"] = text
            elicit_rec["_correct"] = correct_val
            todo.append((key, rec, build_verify_prompt(elicit_rec, rec, alt_label)))

        t0 = time.time()
        # As in stage_elicit: only the network call runs in worker threads,
        # checkpoint writes stay on the main thread as futures complete.
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            future_to_item = {
                executor.submit(call_with_retry, client, model=verify_model, messages=messages,
                                 temperature=0.0, max_tokens=args.max_tokens,
                                 **reasoning_effort_kwargs(args.reasoning_effort)): (key, rec)
                for key, rec, messages in todo
            }
            done = 0
            for future in as_completed(future_to_item):
                key, rec = future_to_item[future]
                response = future.result()
                raw_text = response.choices[0].message.content or ""
                verdict_match = re.search(r"Verdict\s*:\s*(CONFIRM|REPLACE\s*:\s*.*)", raw_text,
                                           flags=re.IGNORECASE | re.DOTALL)
                verdict = verdict_match.group(1).strip() if verdict_match else raw_text.strip()
                out.write(key, {"item_id": rec["item_id"], "option_value": rec["option_value"],
                                 "verdict": verdict, "raw": raw_text})
                done += 1
                if done % 25 == 0 or done == len(todo):
                    elapsed = time.time() - t0
                    print(f"  verified {done}/{len(todo)} ({elapsed:.0f}s elapsed, concurrency={args.concurrency})")
    print(f"Verification results: {args.checkpoint_dir}/verify.jsonl")


def stage_export(args):
    catalog = pd.read_csv(args.distractor_catalog, encoding="utf-8-sig")
    cluster_ckpt = JsonlCheckpoint(args.checkpoint_dir + "/cluster.jsonl")
    verify_path = Path(args.checkpoint_dir + "/verify.jsonl")
    verify_ckpt = JsonlCheckpoint(args.checkpoint_dir + "/verify.jsonl") if verify_path.exists() else None

    tagged_rows = []
    review_rows = []
    for _, row in catalog.iterrows():
        key = f"{row['item_id']}\t{row['option_value']}"
        rec = cluster_ckpt.get(key)
        out_row = dict(row)
        if rec is None:
            out_row["misconception_id"] = row.get("misconception_id", "")
            out_row["misconception_label"] = row.get("misconception_label", "")
            out_row["confidence_margin"] = ""
            out_row["cluster_size"] = ""
            out_row["needs_human_review"] = ""
            out_row["verify_verdict"] = ""
            tagged_rows.append(out_row)
            continue
        flagged = needs_review(rec, args.small_cluster_threshold, args.verify_margin)
        verdict = verify_ckpt.get(key)["verdict"] if verify_ckpt and verify_ckpt.has(key) else ""
        out_row["misconception_id"] = rec["misconception_id"]
        out_row["misconception_label"] = rec["misconception_label"]
        out_row["confidence_margin"] = rec["confidence_margin"]
        out_row["cluster_size"] = rec["cluster_size"]
        out_row["needs_human_review"] = int(flagged)
        out_row["verify_verdict"] = verdict
        tagged_rows.append(out_row)
        if flagged:
            review_rows.append(out_row)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tagged_df = pd.DataFrame(tagged_rows)
    tagged_df.to_csv(out_dir / "distractor_catalog_tagged.csv", index=False, encoding="utf-8-sig")

    review_df = pd.DataFrame(review_rows)
    if not review_df.empty:
        review_df = review_df.sort_values("times_selected", ascending=False)
    review_df.to_csv(out_dir / "human_review_queue.csv", index=False, encoding="utf-8-sig")

    summary = defaultdict(lambda: {"misconception_label": "", "exercise_types": set(),
                                    "member_count": 0, "total_times_selected": 0})
    for rec in cluster_ckpt.done.values():
        s = summary[rec["misconception_id"]]
        s["misconception_label"] = rec["misconception_label"]
        s["exercise_types"].add(rec["exercise_type"])
        s["member_count"] += 1
        s["total_times_selected"] += rec["times_selected"]
    summary_rows = [
        {"misconception_id": mid, "misconception_label": s["misconception_label"],
         "member_count": s["member_count"], "total_times_selected": s["total_times_selected"],
         "exercise_types": ",".join(sorted(s["exercise_types"]))}
        for mid, s in summary.items()
    ]
    summary_df = pd.DataFrame(summary_rows).sort_values("total_times_selected", ascending=False)
    summary_df.to_csv(out_dir / "cluster_summary.csv", index=False, encoding="utf-8-sig")

    print(f"Wrote {len(tagged_df):,} rows to {out_dir / 'distractor_catalog_tagged.csv'}")
    print(f"Wrote {len(review_df):,} rows needing human review to {out_dir / 'human_review_queue.csv'} "
          f"(sorted by impact, highest times_selected first)")
    print(f"Wrote {len(summary_df):,} discovered misconception clusters to {out_dir / 'cluster_summary.csv'} "
          f"(sorted by total impact)")
    print("\nJoin distractor_catalog_tagged.csv's misconception_id back onto "
          "kt_interactions_v2_item_level.csv.gz by (item_id, selected_option_value) -- "
          "no need to re-run the earlier v2 pipeline stages.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=["elicit", "cluster", "verify", "export", "all"], default="all")
    kt_default = ROOT.parent / "learning path maths" / "kt_phase1"
    parser.add_argument("--distractor-catalog", default=str(kt_default / "reports_v2" / "distractor_catalog.csv"))
    parser.add_argument("--item-context", default=str(kt_default / "reports_v2" / "item_context.csv"))
    parser.add_argument("--checkpoint-dir", default=str(ROOT / "misconception_cache" / "math_tagging"))
    parser.add_argument("--out-dir", default=str(ROOT / "misconception_cache" / "math_tagging_out"))
    parser.add_argument("--min-times-selected", type=int, default=2,
                         help="Skip distractors selected fewer than this many times total (long tail, low impact)")
    parser.add_argument("--types", default="",
                         help="Comma-separated exercise_type allowlist/processing order override "
                              "(default: all types found, most-impactful first)")
    parser.add_argument("--limit", type=int, default=0,
                         help="Cap items processed per type (elicit) or overall (verify) -- for smoke testing")
    parser.add_argument("--model", default=default_model())
    parser.add_argument("--verify-model", default=None,
                         help="Model to use for the verify stage's LLM critique, if different from "
                              "--model (a different model decorrelates its errors from the elicit "
                              "pass's, e.g. one of the other models Aitta hosts -- see "
                              "https://aitta.csc.fi/models; default: same model as --model)")
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--retry-max-tokens", type=int, default=2400,
                         help="max_tokens used when retrying an elicit call that previously failed "
                              "(empty response or truncated before the Misconception line) -- higher "
                              "than --max-tokens since the usual cause is the reasoning budget "
                              "eating the whole completion before any visible answer.")
    parser.add_argument("--reasoning-effort", default="low", choices=["low", "medium", "high", ""],
                         help="Caps a reasoning model's (gpt-oss) internal chain-of-thought length "
                              "before the answer -- measured ~2-4x lower latency and completion "
                              "tokens vs. unset, no observed quality/format drop on spot checks. "
                              "Silently ignored by non-reasoning models (e.g. Llama-3.3), so safe to "
                              "leave on even when --verify-model isn't a reasoning model. Pass '' to "
                              "disable (use the model's default reasoning budget).")
    parser.add_argument("--concurrency", type=int, default=8,
                         help="Parallel in-flight requests to Aitta for elicit/verify. Sequential "
                              "(concurrency=1) is far too slow for the full catalog -- e.g. ~19.5K "
                              "elicit calls at the ~7-9s/call measured on gpt-oss-120b is ~40+ hours "
                              "sequential. tag_distractors.py measured concurrency up to ~10-15 as "
                              "safe against Aitta's rate limiting; 20 triggered 429s in testing.")
    parser.add_argument("--cluster-threshold", type=float, default=0.82,
                         help="Cosine similarity threshold for two elicited labels to join the same cluster")
    parser.add_argument("--small-cluster-threshold", type=int, default=2,
                         help="Clusters with this many members or fewer are flagged for human review")
    parser.add_argument("--verify-margin", type=float, default=0.08,
                         help="Assignments with (best - 2nd-best cluster similarity) below this are flagged")
    args = parser.parse_args()

    stages = ["elicit", "cluster", "verify", "export"] if args.stage == "all" else [args.stage]
    for stage in stages:
        print(f"\n=== stage: {stage} ===")
        {"elicit": stage_elicit, "cluster": stage_cluster,
         "verify": stage_verify, "export": stage_export}[stage](args)


if __name__ == "__main__":
    main()
