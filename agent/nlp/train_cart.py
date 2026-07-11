"""Train the CART head (NLP_2.0's decision-tree classifier) and export to JSON.

sklearn DecisionTreeClassifier over the multi-hot feature vectors, then converted to
the plain node graph that model.CartModel reads (no sklearn at runtime).

Task 5b retrain notes (full-economy dataset: 1310 rows, 32 labels, 11 of them with
exactly 1 example, vs. the old 10-label bootstrap-only dataset). This is behavioral
cloning of a DETERMINISTIC expert with 0 feature-vector conflicts (verified in Task
5a: no two rows share a feature vector with different labels), so the target is a
well-defined function and there is no distribution-shift reason to hold out data:
  - stratify=y in train_test_split RAISES with <2-example classes, so the exported
    tree is fit on the FULL dataset instead - a small non-stratified holdout is kept
    purely as an informational sanity number, never used to drop rows from training.
  - the label distribution is heavily skewed (advance_time=600 rows vs. 1-3 for
    several rare economy leaves), so class_weight="balanced" is required or the tree
    collapses towards the majority class (see MAX_DEPTH probe below - without
    balancing, deep trees still starve the 1-example leaves of split priority).
  - max_depth=6 (old bootstrap setting, 21 leaves) is far too shallow for 32 labels:
    empirically it separates only 28% of rows. A depth probe over
    {6,8,10,12,14,16,18,20,24,None} with class_weight="balanced" shows the tree's
    natural (fully-grown) depth is 14 (60 leaves) - every cap >=14 produces the
    IDENTICAL tree and 100% full-set accuracy/recall, confirming the 0-conflict
    property. MAX_DEPTH=14 is therefore the smallest bound that reaches full
    separation, not an arbitrarily large number.

Run (on any box with sklearn):  python -m agent.nlp.train_cart
Outputs: agent/data/decision_cart.json  (+ prints train/holdout accuracy + a
per-class recall table, worst-first).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.tree import DecisionTreeClassifier

_DATA = Path(__file__).resolve().parent.parent / "data"

# Smallest max_depth that reaches the tree's natural (fully-grown) depth on this
# dataset - see the module docstring for the probe that established this value.
MAX_DEPTH = 14


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


def _per_class_recall(y: np.ndarray, preds: np.ndarray,
                       labels: List[str]) -> List[Tuple[str, int, float]]:
    """[(label, n_examples, recall), ...] for every label with >=1 example in y,
    sorted ascending by recall (worst first) so regressions are easy to spot."""
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


def main() -> None:
    rows, vocab, labels = _load()
    X, y = _vectorize(rows, vocab, labels)

    # Sanity holdout ONLY (non-stratified: 11 labels have exactly 1 example and
    # stratify=y would raise ValueError). Purely informational - never used to
    # decide what the exported model below trains on.
    Xho_tr, Xho_va, yho_tr, yho_va = train_test_split(X, y, test_size=0.2, random_state=42)
    holdout_clf = DecisionTreeClassifier(max_depth=MAX_DEPTH, class_weight="balanced",
                                          random_state=42)
    holdout_clf.fit(Xho_tr, yho_tr)
    holdout_acc = float(holdout_clf.score(Xho_va, yho_va))

    # Exported model: fit on the FULL dataset so none of the 11 single-example
    # economy classes are ever excluded from training.
    clf = DecisionTreeClassifier(max_depth=MAX_DEPTH, class_weight="balanced",
                                  random_state=42)
    clf.fit(X, y)
    preds = clf.predict(X)
    tr_acc = float((preds == y).mean())
    recall_table = _per_class_recall(y, preds, labels)

    out = {
        "tree": _tree_to_json(clf, labels),
        "labels": labels,
        "vocab": vocab,
        "meta": {"model": "cart", "max_depth": MAX_DEPTH, "class_weight": "balanced",
                 "n_leaves": int(clf.get_n_leaves()),
                 "train_acc": round(tr_acc, 4), "holdout_val_acc": round(holdout_acc, 4)},
    }
    (_DATA / "decision_cart.json").write_text(json.dumps(out), encoding="utf-8")

    print(f"CART  max_depth={MAX_DEPTH}  leaves={clf.get_n_leaves()}  "
          f"train_acc={tr_acc:.4f}  holdout_val_acc={holdout_acc:.4f}  -> decision_cart.json")
    _print_recall_table(recall_table)


if __name__ == "__main__":
    main()
