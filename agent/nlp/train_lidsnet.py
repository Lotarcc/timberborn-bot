"""Train the LIDSNet MLP head and export to portable JSON.

Faithful to shasankp000/NLP_2.0_pipeline's LIDSNet:
  Linear(D,256)->ReLU->Dropout(.3)->Linear(256,128)->ReLU->Dropout(.3)->Linear(128,C)
  Adam lr=1e-3, CrossEntropy, batch 32, 25 epochs.

Exports agent/data/decision_mlp.json (weight matrices [out][in] + labels + vocab) so
model.MlpModel runs a pure-Python forward pass at inference with zero deps. Also saves
a TorchScript .pt for reference. Uses MPS (Apple Silicon) when available, else CUDA,
else CPU - so this same script trains on the M1 Pro's Metal GPU locally AND on the
Windows/CUDA box unchanged.

Task 5b retrain notes (full-economy dataset: 1310 rows, 32 labels, 11 of them with
exactly 1 example, vs. the old 10-label bootstrap-only dataset). This is behavioral
cloning of a DETERMINISTIC expert with 0 feature-vector conflicts (verified in Task
5a), so the target is a well-defined function and there is no distribution-shift
reason to hold data out of training:
  - the old `train_test_split(..., stratify=y)` RAISES with <2-example classes, so
    the exported model is now trained on the FULL dataset instead. A small
    non-stratified holdout is kept purely as an informational sanity accuracy
    number; it is never used to decide what the exported model trains on.
  - the label distribution is heavily skewed (advance_time=600 rows vs. 1-3 for
    several rare economy leaves), so the loss is now weighted by inverse class
    frequency (sklearn's "balanced" formula: n_samples / (n_classes * count[c]),
    mirroring train_cart.py's class_weight="balanced") or training collapses onto
    the majority class.

Run:  python -m agent.nlp.train_lidsnet
Outputs: agent/data/decision_mlp.json (+ decision_mlp.pt) and prints train/holdout
accuracy plus a per-class recall table (worst-first).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Tuple

import numpy as np
import torch
import torch.nn as nn
from sklearn.model_selection import train_test_split

_DATA = Path(__file__).resolve().parent.parent / "data"
EPOCHS = 25
BATCH = 32


class LIDSNet(nn.Module):
    def __init__(self, d_in: int, n_classes: int):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(d_in, 256), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(256, 128), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(128, n_classes),
        )

    def forward(self, x):
        return self.net(x)


def _load():
    rows = json.loads((_DATA / "decision_dataset.json").read_text(encoding="utf-8"))
    vocab = json.loads((_DATA / "decision_vocab.json").read_text(encoding="utf-8"))["vocab"]
    labels = json.loads((_DATA / "decision_labels.json").read_text(encoding="utf-8"))
    vindex = {s: i for i, s in enumerate(vocab)}
    lindex = {l: i for i, l in enumerate(labels)}
    X = np.zeros((len(rows), len(vocab)), dtype=np.float32)
    y = np.zeros(len(rows), dtype=np.int64)
    for r, row in enumerate(rows):
        for s in row["features"]:
            j = vindex.get(s)
            if j is not None:
                X[r, j] = 1.0
        y[r] = lindex[row["label"]]
    return X, y, vocab, labels


def _class_weights(y: np.ndarray, n_classes: int) -> np.ndarray:
    """sklearn's "balanced" formula: n_samples / (n_classes * count[c]). Mirrors
    train_cart.py's class_weight="balanced" so both heads correct for the same
    advance_time-dominated imbalance the same way."""
    counts = np.bincount(y, minlength=n_classes).astype(np.float64)
    counts[counts == 0] = 1.0  # guard; every label is expected to have >=1 example
    n_samples = float(len(y))
    return n_samples / (n_classes * counts)


def _fit(X_np: np.ndarray, y_np: np.ndarray, d_in: int, n_classes: int,
         weight_t: torch.Tensor, device: str) -> LIDSNet:
    Xt = torch.tensor(X_np, device=device)
    yt = torch.tensor(y_np, device=device)
    model = LIDSNet(d_in, n_classes).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.CrossEntropyLoss(weight=weight_t)

    n = Xt.shape[0]
    for _epoch in range(EPOCHS):
        model.train()
        perm = torch.randperm(n, device=device)
        for start in range(0, n, BATCH):
            idx = perm[start:start + BATCH]
            opt.zero_grad()
            loss = loss_fn(model(Xt[idx]), yt[idx])
            loss.backward()
            opt.step()
    return model


def _per_class_recall(y: np.ndarray, preds: np.ndarray,
                       labels: List[str]) -> List[Tuple[str, int, float]]:
    out = []
    for k, lab in enumerate(labels):
        mask = y == k
        n_k = int(mask.sum())
        if n_k == 0:
            continue
        recall = float((preds[mask] == k).mean())
        out.append((lab, n_k, recall))
    out.sort(key=lambda r: r[2])
    return out


def _print_recall_table(rows: List[Tuple[str, int, float]]) -> None:
    print(f"{'label':30s} {'n':>5s} {'recall':>7s}")
    for lab, n_k, recall in rows:
        flag = "  <-- LOW" if recall < 0.9 else ""
        print(f"{lab:30s} {n_k:5d} {recall:7.4f}{flag}")


def _export_json(model: LIDSNet, vocab, labels, meta, path: Path) -> None:
    layers = []
    seq = model.net
    for i, module in enumerate(seq):
        if isinstance(module, nn.Linear):
            act = "relu" if (i + 1 < len(seq) and isinstance(seq[i + 1], nn.ReLU)) else None
            layers.append({
                "W": module.weight.detach().cpu().tolist(),  # [out][in]
                "b": module.bias.detach().cpu().tolist(),
                "act": act,
            })
    path.write_text(json.dumps({"layers": layers, "labels": labels,
                                "vocab": vocab, "meta": meta}), encoding="utf-8")


def main() -> None:
    device = ("mps" if torch.backends.mps.is_available()
               else "cuda" if torch.cuda.is_available() else "cpu")
    X, y, vocab, labels = _load()
    class_w = _class_weights(y, len(labels))
    weight_t = torch.tensor(class_w, dtype=torch.float32, device=device)

    # Sanity holdout ONLY (non-stratified split: stratify=y would raise with the 11
    # single-example classes). Purely informational - never used to decide what the
    # exported model below trains on.
    Xho_tr, Xho_va, yho_tr, yho_va = train_test_split(X, y, test_size=0.2, random_state=42)
    torch.manual_seed(42)
    holdout_model = _fit(Xho_tr, yho_tr, len(vocab), len(labels), weight_t, device)
    holdout_model.eval()
    with torch.no_grad():
        Xho_va_t = torch.tensor(Xho_va, device=device)
        yho_va_t = torch.tensor(yho_va, device=device)
        holdout_acc = (holdout_model(Xho_va_t).argmax(1) == yho_va_t).float().mean().item()

    # Exported model: trained on the FULL dataset so none of the 11 single-example
    # economy classes are ever excluded from training (see module docstring).
    torch.manual_seed(42)
    model = _fit(X, y, len(vocab), len(labels), weight_t, device)
    model.eval()
    with torch.no_grad():
        X_t = torch.tensor(X, device=device)
        y_t = torch.tensor(y, device=device)
        preds = model(X_t).argmax(1)
        tr_acc = (preds == y_t).float().mean().item()

    preds_np = preds.cpu().numpy()
    recall_table = _per_class_recall(y, preds_np, labels)

    meta = {"model": "lidsnet", "device": device, "epochs": EPOCHS,
            "class_weight": "balanced",
            "train_acc": round(tr_acc, 4), "holdout_val_acc": round(holdout_acc, 4)}
    _export_json(model, vocab, labels, meta, _DATA / "decision_mlp.json")

    # TorchScript reference artifact
    try:
        scripted = torch.jit.trace(model.to("cpu").eval(), torch.zeros(1, len(vocab)))
        scripted.save(str(_DATA / "decision_mlp.pt"))
    except Exception as exc:  # pragma: no cover
        print("torchscript export skipped:", exc)

    print(f"LIDSNet[{device}]  train_acc={tr_acc:.4f}  holdout_val_acc={holdout_acc:.4f}  "
          f"-> decision_mlp.json")
    _print_recall_table(recall_table)


if __name__ == "__main__":
    main()
