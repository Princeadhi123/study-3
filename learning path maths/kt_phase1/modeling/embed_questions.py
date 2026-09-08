"""Precompute frozen multilingual sentence embeddings for every unique question
text found in sequences.jsonl.gz. Used only by the content-aware KT variant (C).

No Finnish->English translation is needed: multilingual sentence-transformer
models (e.g. paraphrase-multilingual-MiniLM-L12-v2 or LaBSE) are trained so
that Finnish, Swedish, and English text describing the same content land in a
similar region of the embedding space. We embed the raw Finnish/DSL text
directly.

Output (kt_phase1/modeling/prepared/):
  text_embeddings.npz   arrays: "texts" (object array of strings),
                        "vectors" (float32 [n_unique_texts, dim])

This step requires internet access the first time, to download the model
from Hugging Face. On a supercomputer with no internet on compute nodes,
run this once on a login/head node (or locally) and copy prepared/ across.
"""
import argparse
import gzip
import json
from pathlib import Path

import numpy as np

OUT = Path(__file__).parent / "prepared"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sequences", default=str(OUT / "sequences.jsonl.gz"))
    p.add_argument("--out", default=str(OUT / "text_embeddings.npz"))
    p.add_argument("--model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                   help="Any multilingual sentence-transformers model; must cover Finnish.")
    p.add_argument("--batch-size", type=int, default=256)
    return p.parse_args()


def main():
    args = parse_args()
    from sentence_transformers import SentenceTransformer

    texts = set()
    with gzip.open(args.sequences, "rt", encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            for event in record["events"]:
                t = event.get("text") or ""
                if t:
                    texts.add(t)
    texts = sorted(texts)
    print(f"Unique question texts to embed: {len(texts):,}", flush=True)

    model = SentenceTransformer(args.model)
    vectors = model.encode(
        texts, batch_size=args.batch_size, show_progress_bar=True,
        normalize_embeddings=True, convert_to_numpy=True,
    )
    np.savez_compressed(args.out, texts=np.array(texts, dtype=object), vectors=vectors.astype(np.float32))
    print(f"Wrote {args.out} with shape {vectors.shape}", flush=True)


if __name__ == "__main__":
    main()
