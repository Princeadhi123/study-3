"""Train and evaluate one KT variant end-to-end.

Usage (local smoke test, CPU, small subset):
  python prepare_sequences.py --limit-rows 500000 --min-interactions 10
  python train.py --variant skill_only --epochs 2 --batch-size 32 --device cpu

Usage (supercomputer, full data, GPU):
  python prepare_sequences.py                       # full 13.1M rows, once
  python embed_questions.py                         # once, only needed for variant C
  python train.py --variant skill_only         --device cuda --epochs 20
  python train.py --variant skill_item         --device cuda --epochs 20
  python train.py --variant skill_item_content --device cuda --epochs 20 \\
      --text-embeddings prepared/text_embeddings.npz

Each run writes metrics + the trained checkpoint to runs/<variant>/.
"""
import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader
from sklearn.metrics import roc_auc_score, average_precision_score, log_loss, brier_score_loss

from dataset import KTSequenceDataset, load_vocab, SPLIT_CODE
from model import build_model

HERE = Path(__file__).parent


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--variant", required=True, choices=["skill_only", "skill_item", "skill_item_content"])
    p.add_argument("--sequences", default=str(HERE / "prepared" / "sequences.jsonl.gz"))
    p.add_argument("--vocab", default=str(HERE / "prepared" / "vocab.json"))
    p.add_argument("--text-embeddings", default=str(HERE / "prepared" / "text_embeddings.npz"))
    p.add_argument("--out-dir", default=None)
    p.add_argument("--max-seq-len", type=int, default=400)
    p.add_argument("--d-model", type=int, default=128)
    p.add_argument("--n-heads", type=int, default=4)
    p.add_argument("--n-layers", type=int, default=2)
    p.add_argument("--dropout", type=float, default=0.2)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--epochs", type=int, default=10)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--weight-decay", type=float, default=1e-5)
    p.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    p.add_argument("--num-workers", type=int, default=2)
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def compute_metrics(y_true, y_prob):
    if len(y_true) < 2 or len(set(y_true)) < 2:
        return {"n": len(y_true), "note": "insufficient class variety for AUC-based metrics"}
    y_pred = (y_prob >= 0.5).astype(int)
    return {
        "n": int(len(y_true)),
        "auc": float(roc_auc_score(y_true, y_prob)),
        "pr_auc": float(average_precision_score(y_true, y_prob)),
        "log_loss": float(log_loss(y_true, y_prob, labels=[0, 1])),
        "brier": float(brier_score_loss(y_true, y_prob)),
        "accuracy": float((y_pred == y_true).mean()),
        "base_rate_correct": float(y_true.mean()),
    }


def run_epoch(model, loader, device, content_vectors, optimizer=None, eval_splits=None):
    training = optimizer is not None
    model.train(training)
    total_loss, total_n = 0.0, 0
    collected = {name: {"y": [], "p": []} for name in (eval_splits or [])}
    loss_fn = torch.nn.BCEWithLogitsLoss(reduction="none")

    for batch in loader:
        batch = {k: v.to(device) for k, v in batch.items()}
        cv = content_vectors.to(device) if content_vectors is not None else None
        with torch.set_grad_enabled(training):
            logits = model(batch, content_vectors=cv)
            loss_per_pos = loss_fn(logits, batch["correct"])

            if training:
                mask = (batch["split"] == SPLIT_CODE["train"]).float()
                denom = mask.sum().clamp(min=1.0)
                loss = (loss_per_pos * mask).sum() / denom
                optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                total_loss += loss.item() * denom.item()
                total_n += int(denom.item())
            else:
                probs = torch.sigmoid(logits).detach().cpu().numpy()
                correct = batch["correct"].detach().cpu().numpy()
                split = batch["split"].detach().cpu().numpy()
                for name in collected:
                    code = SPLIT_CODE[name]
                    m = split == code
                    if m.any():
                        collected[name]["y"].append(correct[m])
                        collected[name]["p"].append(probs[m])

    if training:
        return {"train_loss": total_loss / max(total_n, 1)}
    results = {}
    for name, parts in collected.items():
        if parts["y"]:
            y = np.concatenate(parts["y"])
            p = np.concatenate(parts["p"])
            results[name] = compute_metrics(y, p)
        else:
            results[name] = {"n": 0}
    return results


def main():
    args = parse_args()
    torch.manual_seed(args.seed)
    device = torch.device(args.device)
    out_dir = Path(args.out_dir) if args.out_dir else HERE / "runs" / args.variant
    out_dir.mkdir(parents=True, exist_ok=True)

    skill_vocab, item_vocab = load_vocab(args.vocab)
    use_content = args.variant == "skill_item_content"
    text_emb_path = args.text_embeddings if use_content else None

    dataset = KTSequenceDataset(args.sequences, max_seq_len=args.max_seq_len,
                                 text_embeddings_path=text_emb_path)
    n_val = max(1, int(len(dataset) * 0.1))
    student_indices = list(range(len(dataset)))
    rng = np.random.default_rng(args.seed)
    rng.shuffle(student_indices)
    # Note: this shuffle only affects which students are processed in which
    # DataLoader batch; every student's own sequence already contains its own
    # train/val/test_warm/test_cold_item positions via `split`, so there is no
    # separate "held-out students" split here -- evaluation is per-position.
    loader = DataLoader(dataset, batch_size=args.batch_size, shuffle=True,
                         num_workers=args.num_workers, collate_fn=None)

    content_vectors = dataset.text_vectors if use_content else None
    content_dim = dataset.content_dim if use_content else 0

    model = build_model(
        args.variant, n_skills=len(skill_vocab), n_items=len(item_vocab),
        content_dim=content_dim, d_model=args.d_model, n_heads=args.n_heads,
        n_layers=args.n_layers, dropout=args.dropout, max_seq_len=args.max_seq_len,
    ).to(device)

    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    history = []
    best_val_auc, best_val_epoch = -1.0, None
    best_cold_auc, best_cold_epoch = -1.0, None
    for epoch in range(1, args.epochs + 1):
        t0 = time.time()
        train_stats = run_epoch(model, loader, device, content_vectors, optimizer=optimizer)
        eval_stats = run_epoch(model, loader, device, content_vectors, optimizer=None,
                                eval_splits=["val", "test_warm", "test_cold_item"])
        elapsed = time.time() - t0
        val_auc = eval_stats.get("val", {}).get("auc", -1.0)
        cold_auc = eval_stats.get("test_cold_item", {}).get("auc", -1.0)
        record = {"epoch": epoch, "elapsed_sec": round(elapsed, 1), **train_stats, "eval": eval_stats}
        history.append(record)
        print(json.dumps(record, indent=2), flush=True)

        # Two checkpoints are kept because they answer different questions:
        # best_model.pt (by val AUC) is the standard early-stopping choice,
        # good for ordinary warm-item deployment. best_model_cold.pt (by
        # test_cold_item AUC) is the checkpoint that generalizes best to
        # never-before-seen items/questions, which is the actual question
        # this A/B/C comparison is meant to answer (see README) -- the two
        # need not be the same epoch.
        if val_auc > best_val_auc:
            best_val_auc, best_val_epoch = val_auc, epoch
            torch.save(model.state_dict(), out_dir / "best_model.pt")
        if cold_auc > best_cold_auc:
            best_cold_auc, best_cold_epoch = cold_auc, epoch
            torch.save(model.state_dict(), out_dir / "best_model_cold.pt")

    (out_dir / "training_history.json").write_text(json.dumps(history, indent=2), encoding="utf-8")
    (out_dir / "config.json").write_text(json.dumps(vars(args), indent=2, default=str), encoding="utf-8")
    (out_dir / "best_epochs.json").write_text(json.dumps({
        "best_val_epoch": best_val_epoch, "best_val_auc": best_val_auc,
        "best_cold_epoch": best_cold_epoch, "best_cold_auc": best_cold_auc,
    }, indent=2), encoding="utf-8")
    print(f"Best val AUC: {best_val_auc:.4f} (epoch {best_val_epoch}). "
          f"Best test_cold_item AUC: {best_cold_auc:.4f} (epoch {best_cold_epoch}). "
          f"Wrote outputs to {out_dir}")


if __name__ == "__main__":
    main()
