import csv
import gzip
import heapq
import shutil
from pathlib import Path

import pandas as pd

OUT = Path(__file__).parent
DATA = OUT / "data"
SOURCE = DATA / "kt_interactions.csv.gz"
SORTED = DATA / "kt_interactions_sorted.csv.gz"
TEMP = OUT / "sort_chunks"
CHUNK_SIZE = 250_000


def row_key(row):
    try:
        attempt = int(row["attempt_number"] or 0)
    except ValueError:
        attempt = 0
    try:
        preorder = int(float(row["preorder"] or 0))
    except ValueError:
        preorder = 0
    return (row["student_id"], row["timestamp"], attempt, preorder)


def main():
    TEMP.mkdir(exist_ok=True)
    chunks = []
    for index, frame in enumerate(pd.read_csv(SOURCE, compression="gzip", dtype=str, keep_default_na=False, chunksize=CHUNK_SIZE)):
        frame["_attempt_sort"] = pd.to_numeric(frame["attempt_number"], errors="coerce").fillna(0)
        frame["_preorder_sort"] = pd.to_numeric(frame["preorder"], errors="coerce").fillna(0)
        frame = frame.sort_values(
            ["student_id", "timestamp", "_attempt_sort", "_preorder_sort"],
            kind="stable",
        ).drop(columns=["_attempt_sort", "_preorder_sort"])
        path = TEMP / f"chunk_{index:05d}.csv"
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        chunks.append(path)
        print(f"Wrote sorted chunk {index + 1}: {len(frame):,} rows", flush=True)

    handles = [path.open("r", encoding="utf-8-sig", newline="") for path in chunks]
    readers = [csv.DictReader(handle) for handle in handles]
    fieldnames = readers[0].fieldnames
    heap = []
    for index, reader in enumerate(readers):
        try:
            row = next(reader)
        except StopIteration:
            continue
        heapq.heappush(heap, (row_key(row), index, row))

    with gzip.open(SORTED, "wt", encoding="utf-8-sig", newline="", compresslevel=6) as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        written = 0
        while heap:
            _, index, row = heapq.heappop(heap)
            writer.writerow(row)
            written += 1
            if written % 1_000_000 == 0:
                print(f"Merged {written:,} rows", flush=True)
            try:
                next_row = next(readers[index])
            except StopIteration:
                continue
            heapq.heappush(heap, (row_key(next_row), index, next_row))

    for handle in handles:
        handle.close()
    for path in chunks:
        path.unlink()
    TEMP.rmdir()
    print(f"Wrote {SORTED} with {written:,} rows", flush=True)


if __name__ == "__main__":
    main()
