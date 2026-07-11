"""Train the LIDSNet MLP head on the GPU and export to portable JSON.

Faithful to shasankp000/NLP_2.0_pipeline's LIDSNet:
  Linear(D,256)->ReLU->Dropout(.3)->Linear(256,128)->ReLU->Dropout(.3)->Linear(128,C)
  Adam lr=1e-3, CrossEntropy, batch 32, 25 epochs, 0.2 stratified val split.

Exports agent/data/decision_mlp.json (weight matrices [out][in] + labels + vocab) so
model.MlpModel runs a pure-Python forward pass at inference with zero deps. Also saves
a TorchScript .pt for reference. Uses CUDA when available (the 4060 Ti).

Run (on the GPU box):  python -m agent.nlp.train_lidsnet
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from sklearn.model_selection import train_test_split

_DATA = Path(__file__).resolve().parent.parent / "data"


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
    device = "cuda" if torch.cuda.is_available() else "cpu"
    X, y, vocab, labels = _load()
    Xtr, Xva, ytr, yva = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

    Xtr_t = torch.tensor(Xtr, device=device)
    ytr_t = torch.tensor(ytr, device=device)
    Xva_t = torch.tensor(Xva, device=device)
    yva_t = torch.tensor(yva, device=device)

    model = LIDSNet(len(vocab), len(labels)).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.CrossEntropyLoss()

    n = Xtr_t.shape[0]
    batch = 32
    for epoch in range(25):
        model.train()
        perm = torch.randperm(n, device=device)
        for start in range(0, n, batch):
            idx = perm[start:start + batch]
            opt.zero_grad()
            loss = loss_fn(model(Xtr_t[idx]), ytr_t[idx])
            loss.backward()
            opt.step()

    model.eval()
    with torch.no_grad():
        tr_acc = (model(Xtr_t).argmax(1) == ytr_t).float().mean().item()
        va_acc = (model(Xva_t).argmax(1) == yva_t).float().mean().item()

    meta = {"model": "lidsnet", "device": device, "epochs": 25,
            "train_acc": round(tr_acc, 4), "val_acc": round(va_acc, 4)}
    _export_json(model, vocab, labels, meta, _DATA / "decision_mlp.json")

    # TorchScript reference artifact
    try:
        scripted = torch.jit.trace(model.to("cpu").eval(), torch.zeros(1, len(vocab)))
        scripted.save(str(_DATA / "decision_mlp.pt"))
    except Exception as exc:  # pragma: no cover
        print("torchscript export skipped:", exc)

    print(f"LIDSNet[{device}]  train_acc={tr_acc:.4f}  val_acc={va_acc:.4f}  -> decision_mlp.json")


if __name__ == "__main__":
    main()
