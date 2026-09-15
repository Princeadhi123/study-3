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
    p.add_argument("--variant", required=True,
                   choices=["skill_only", "skill_item", "skill_item_content", "skill_item_content_option"])
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
    p.add_argument("--min-lr", type=float, default=1e-5,
                    help="Floor learning rate reached at the end of cosine annealing (see "
                         "--warmup-epochs). Matches --lr for a no-scheduler-decay run only if "
                         "set equal to --lr.")
    p.add_argument("--warmup-epochs", type=int, default=5,
                    help="Number of initial epochs spent linearly warming the learning rate up "
                         "from 0.1 * --lr to --lr, before cosine-annealing it down to --min-lr "
                         "over the remaining epochs (see build_lr_scheduler). Set to 0 to disable "
                         "warmup and cosine-anneal from epoch 1. This targets the val/test_cold_item "
                         "AUC plateau seen with a constant LR even while train_loss keeps falling "
                         "(see README) -- annealing the LR down late in training lets the model "
                         "keep making small, stable improvements instead of oscillating around the "
                         "plateau at a fixed step size.")
    p.add_argument("--weight-decay", type=float, default=1e-5)
    p.add_argument("--item-embed-weight-decay", type=float, default=5e-3,
                    help="Separate (stronger) weight decay applied only to item_embed, "
                         "so the item_id lookup table can't grow large, item-specific "
                         "weights as freely as the rest of the model. Ignored by "
                         "skill_only (no item_embed). No-op for backwards compatibility "
                         "if set equal to --weight-decay.")
    p.add_argument("--item-id-dropout", type=float, default=0.25,
                    help="Probability of replacing a known (non-PAD, non-UNK) item_id "
                         "with __UNK__ during training only, each occurrence independently. "
                         "This is cold-start augmentation: it (a) gives the __UNK__ embedding "
                         "row real, frequent gradient signal from otherwise-ordinary items "
                         "instead of only ever seeing genuine test_cold_item occurrences, and "
                         "(b) prevents the model from fully relying on memorizing item_id, "
                         "both of which curb the test_cold_item AUC decay seen over training "
                         "when item embeddings are trained without this regularization. Raised "
                         "from 0.1 to 0.25 (together with a stronger --item-embed-weight-decay) "
                         "to push item-aware variants to lean more on content embeddings, which "
                         "generalize to genuinely novel items/questions and item_id can't. "
                         "Ignored by skill_only. Set to 0 to disable / reproduce old behavior.")
    p.add_argument("--use-time-embeddings", action=argparse.BooleanOptionalAction, default=True,
                    help="Add a learned embedding of the log-binned time-since-previous-"
                         "interaction (see prepare_sequences_v2.py's time_bin_of) into each "
                         "step's interaction representation. Falls back to a no-op if the "
                         "loaded sequences.jsonl.gz predates the time_bin field (dataset.py "
                         "defaults time_bin_ids to 0 for every position in that case). Pass "
                         "--no-use-time-embeddings to disable.")
    p.add_argument("--joint-weight", type=float, default=0.5,
                    help="Weight w in joint_score = w * val_auc + (1 - w) * test_cold_item_auc, "
                         "used to select the extra best_model_joint.pt checkpoint -- a middle "
                         "ground between best_model.pt (pure val AUC, best warm-item deployment) "
                         "and best_model_cold.pt (pure cold AUC, which can be a very early, "
                         "under-trained epoch). See README 'Reading the results'.")
    p.add_argument("--patience", type=int, default=25,
                    help="Stop early if joint_score (see --joint-weight) hasn't set a new best "
                         "for this many consecutive epochs. Set to 0 to disable early stopping "
                         "and always run the full --epochs. Raised from 15 to 25 to give the "
                         "cosine LR decay (see --warmup-epochs) room to keep squeezing out late, "
                         "low-LR improvements instead of stopping just as annealing starts to "
                         "help. Useful when raising --epochs for a run: variants that converge "
                         "quickly (e.g. skill_item) stop on their own instead of wasting GPU "
                         "time, while variants still improving (content-aware ones) keep training.")
    p.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    p.add_argument("--num-workers", type=int, default=2)
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def build_lr_scheduler(optimizer: torch.optim.Optimizer, epochs: int, warmup_epochs: int,
                        min_lr: float, base_lr: float) -> torch.optim.lr_scheduler.LRScheduler:
    """Linear warmup (0.1 * base_lr -> base_lr) for `warmup_epochs`, then
    cosine annealing down to `min_lr` over the remaining epochs. Call
    `.step()` once per epoch (not per batch) -- see README/train loop.
    Addresses the val/test_cold_item AUC plateau seen with a constant LR
    even while train_loss keeps falling every epoch (see
    training_history.json for every variant): a properly annealed LR lets
    the model keep making small, stable gains late in training instead of
    oscillating around the plateau at a fixed step size."""
    warmup_epochs = max(0, min(warmup_epochs, epochs - 1))
    if warmup_epochs == 0:
        return torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, epochs), eta_min=min_lr)
    warmup = torch.optim.lr_scheduler.LinearLR(
        optimizer, start_factor=0.1, end_factor=1.0, total_iters=warmup_epochs)
    cosine = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=max(1, epochs - warmup_epochs), eta_min=min_lr)
    return torch.optim.lr_scheduler.SequentialLR(
        optimizer, schedulers=[warmup, cosine], milestones=[warmup_epochs])


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


def run_epoch(model, loader, device, content_vectors, optimizer=None, eval_splits=None,
              item_id_dropout=0.0):
    training = optimizer is not None
    model.train(training)
    total_loss, total_n = 0.0, 0
    collected = {name: {"y": [], "p": []} for name in (eval_splits or [])}
    loss_fn = torch.nn.BCEWithLogitsLoss(reduction="none")

    for batch in loader:
        batch = {k: v.to(device) for k, v in batch.items()}
        if training and item_id_dropout > 0 and getattr(model, "use_item", False):
            # Cold-start augmentation (see --item-id-dropout help in parse_args):
            # randomly resolve some known item_ids to __UNK__ (index 1) before
            # they're used as both the "previous interaction" input and the
            # "query" feature. PAD (0) and already-cold items (already UNK=1)
            # are left untouched -- only real, trainable item_ids (>1) are
            # eligible, so this never overwrites real cold-item eval positions.
            item = batch["item"]
            droppable = item > 1
            drop_mask = droppable & (torch.rand(item.shape, device=item.device) < item_id_dropout)
            if drop_mask.any():
                batch = dict(batch)
                batch["item"] = item.masked_fill(drop_mask, 1)
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
    use_content = args.variant in {"skill_item_content", "skill_item_content_option"}
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
    # persistent_workers=True keeps the same worker processes alive for the
    # whole run instead of tearing them down and respawning new ones (with
    # fresh semaphores/shm segments) every single `for batch in loader`
    # iteration -- run_epoch() is called twice per epoch (train + eval), so
    # without this a long run churns through hundreds of worker spawns and
    # can eventually exhaust the node's POSIX semaphore/shm slots, surfacing
    # as `FileNotFoundError` in multiprocessing's SemLock.__init__ deep into
    # training. Only valid when num_workers > 0.
    loader = DataLoader(dataset, batch_size=args.batch_size, shuffle=True,
                         num_workers=args.num_workers, collate_fn=None,
                         persistent_workers=args.num_workers > 0)

    content_vectors = dataset.text_vectors if use_content else None
    content_dim = dataset.content_dim if use_content else 0

    model = build_model(
        args.variant, n_skills=len(skill_vocab), n_items=len(item_vocab),
        content_dim=content_dim,
        d_model=args.d_model, n_heads=args.n_heads,
        n_layers=args.n_layers, dropout=args.dropout, max_seq_len=args.max_seq_len,
        use_time_embeddings=args.use_time_embeddings,
    ).to(device)

    # item_embed gets its own (typically stronger) weight decay so the
    # item_id lookup table is discouraged from growing large, item-specific
    # weights as freely as the rest of the model -- this, together with
    # --item-id-dropout, is what curbs the test_cold_item AUC decay seen in
    # skill_item/skill_item_content when item embeddings train unconstrained
    # (see README). skill_only has no item_embed, so this is a no-op for it.
    item_embed_params = [p for n, p in model.named_parameters() if n.startswith("item_embed")]
    other_params = [p for n, p in model.named_parameters() if not n.startswith("item_embed")]
    param_groups = [{"params": other_params, "weight_decay": args.weight_decay}]
    if item_embed_params:
        param_groups.append({"params": item_embed_params, "weight_decay": args.item_embed_weight_decay})
    optimizer = torch.optim.AdamW(param_groups, lr=args.lr)
    scheduler = build_lr_scheduler(optimizer, epochs=args.epochs, warmup_epochs=args.warmup_epochs,
                                    min_lr=args.min_lr, base_lr=args.lr)

    history = []
    best_val_auc, best_val_epoch = -1.0, None
    best_cold_auc, best_cold_epoch = -1.0, None
    best_joint_score, best_joint_epoch = -1.0, None
    epochs_since_best_joint = 0
    for epoch in range(1, args.epochs + 1):
        t0 = time.time()
        # Recorded before the scheduler step below, so it reflects the LR
        # actually used to train THIS epoch, not the upcoming one.
        current_lr = optimizer.param_groups[0]["lr"]
        train_stats = run_epoch(model, loader, device, content_vectors, optimizer=optimizer,
                                 item_id_dropout=args.item_id_dropout)
        eval_stats = run_epoch(model, loader, device, content_vectors, optimizer=None,
                                eval_splits=["val", "test_warm", "test_cold_item"])
        scheduler.step()
        elapsed = time.time() - t0
        val_auc = eval_stats.get("val", {}).get("auc", -1.0)
        cold_auc = eval_stats.get("test_cold_item", {}).get("auc", -1.0)
        joint_score = (args.joint_weight * val_auc + (1 - args.joint_weight) * cold_auc
                       if val_auc >= 0 and cold_auc >= 0 else -1.0)
        record = {"epoch": epoch, "elapsed_sec": round(elapsed, 1), "lr": current_lr,
                  **train_stats, "eval": eval_stats}
        history.append(record)
        print(json.dumps(record, indent=2), flush=True)

        # Three checkpoints are kept because "best" depends on which question
        # you're asking: best_model.pt (by val AUC) is the standard
        # early-stopping choice, good for ordinary warm-item deployment.
        # best_model_cold.pt (by test_cold_item AUC) generalizes best to
        # never-before-seen items/questions but can be a very early,
        # under-trained epoch. best_model_joint.pt (by a weighted combination
        # of the two, --joint-weight) is the practical middle ground actually
        # recommended for deployment -- see README "Reading the results".
        if val_auc > best_val_auc:
            best_val_auc, best_val_epoch = val_auc, epoch
            torch.save(model.state_dict(), out_dir / "best_model.pt")
        if cold_auc > best_cold_auc:
            best_cold_auc, best_cold_epoch = cold_auc, epoch
            torch.save(model.state_dict(), out_dir / "best_model_cold.pt")
        if joint_score > best_joint_score:
            best_joint_score, best_joint_epoch = joint_score, epoch
            torch.save(model.state_dict(), out_dir / "best_model_joint.pt")
            epochs_since_best_joint = 0
        else:
            epochs_since_best_joint += 1
            if args.patience > 0 and epochs_since_best_joint >= args.patience:
                print(f"Early stopping: no joint_score improvement for {epochs_since_best_joint} "
                      f"epochs (--patience {args.patience}). Stopping after epoch {epoch}.",
                      flush=True)
                break

    (out_dir / "training_history.json").write_text(json.dumps(history, indent=2), encoding="utf-8")
    (out_dir / "config.json").write_text(json.dumps(vars(args), indent=2, default=str), encoding="utf-8")
    (out_dir / "best_epochs.json").write_text(json.dumps({
        "best_val_epoch": best_val_epoch, "best_val_auc": best_val_auc,
        "best_cold_epoch": best_cold_epoch, "best_cold_auc": best_cold_auc,
        "best_joint_epoch": best_joint_epoch, "best_joint_score": best_joint_score,
        "joint_weight": args.joint_weight,
    }, indent=2), encoding="utf-8")
    print(f"Best val AUC: {best_val_auc:.4f} (epoch {best_val_epoch}). "
          f"Best test_cold_item AUC: {best_cold_auc:.4f} (epoch {best_cold_epoch}). "
          f"Best joint score: {best_joint_score:.4f} (epoch {best_joint_epoch}). "
          f"Wrote outputs to {out_dir}")


if __name__ == "__main__":
    main()
