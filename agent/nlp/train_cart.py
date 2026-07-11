"""Train the CART head (NLP_2.0's decision-tree classifier) and export to JSON.

sklearn DecisionTreeClassifier(max_depth=6) over the multi-hot feature vectors, then
converted to the plain node graph that model.CartModel reads (no sklearn at runtime).

Run (on any box with sklearn):  python -m agent.nlp.train_cart
Outputs: agent/data/decision_cart.json  (+ prints train/val accuracy)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.tree import DecisionTreeClassifier

_DATA = Path(__file__).resolve().parent.parent / "data"


def _load():
    rows = json.loads((_DATA / "decision_dataset.json").read_text(encoding="utf-8"))
    vocab = json.loads((_DATA / "decision_vocab.json").read_text(encoding="utf-8"))["vocab"]
    labels = json.loads((_DATA / "decision_labels.json").read_text(encoding="utf-8"))
    return rows, vocab, labels


def _vectorize(rows, vocab, labels):
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
    return X, y


def _tree_to_json(clf: DecisionTreeClassifier, labels: List[str]) -> dict:
    t = clf.tree_

    def node(i: int) -> dict:
        if t.children_left[i] == t.children_right[i]:  # leaf
            counts = t.value[i][0]
            total = float(counts.sum()) or 1.0
            proba: Dict[str, float] = {labels[k]: float(counts[k] / total)
                                       for k in range(len(labels)) if counts[k] > 0}
            best = int(counts.argmax())
            return {"label": labels[best], "proba": proba}
        # internal: sklearn goes LEFT when X[f] <= threshold (i.e. feature == 0)
        return {"f": int(t.feature[i]), "l": node(t.children_left[i]),
                "r": node(t.children_right[i])}

    return node(0)


def main() -> None:
    rows, vocab, labels = _load()
    X, y = _vectorize(rows, vocab, labels)
    Xtr, Xva, ytr, yva = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

    clf = DecisionTreeClassifier(max_depth=6, random_state=42)
    clf.fit(Xtr, ytr)
    tr_acc = clf.score(Xtr, ytr)
    va_acc = clf.score(Xva, yva)

    out = {
        "tree": _tree_to_json(clf, labels),
        "labels": labels,
        "vocab": vocab,
        "meta": {"model": "cart", "max_depth": 6,
                 "train_acc": round(float(tr_acc), 4), "val_acc": round(float(va_acc), 4)},
    }
    (_DATA / "decision_cart.json").write_text(json.dumps(out), encoding="utf-8")
    print(f"CART  train_acc={tr_acc:.4f}  val_acc={va_acc:.4f}  -> decision_cart.json")


if __name__ == "__main__":
    main()
