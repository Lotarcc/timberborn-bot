"""Dependency-free inference for the two decision heads.

Training happens on the GPU box with torch/sklearn, but both heads export to plain
JSON so the play loop runs anywhere (no torch, no numpy, no sklearn). This is what
lets the Mac (Python 3.14, no torch wheels) run the trained policy at full speed.

  CartModel - a decision tree (NLP_2.0's CART head), JSON node graph.
  MlpModel  - the LIDSNet MLP, JSON weight matrices + pure-Python forward.

Both consume a multi-hot feature vector (list[int]) from features.StateFeaturizer.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _softmax(xs: List[float]) -> List[float]:
    m = max(xs)
    exps = [math.exp(x - m) for x in xs]
    total = sum(exps) or 1.0
    return [e / total for e in exps]


class CartModel:
    """Pure-Python CART inference. Tree node JSON:
    internal: {"f": feature_index, "l": node, "r": node}   # go r if vec[f] else l
    leaf:     {"label": str, "proba": {label: p, ...}}
    """

    def __init__(self, tree: dict, labels: List[str], vocab: List[str]):
        self.tree = tree
        self.labels = labels
        self.vocab = vocab

    @classmethod
    def load(cls, path: str | Path) -> "CartModel":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["tree"], data["labels"], data["vocab"])

    def predict(self, vec: List[int]) -> Tuple[str, float]:
        node = self.tree
        while "label" not in node:
            node = node["r"] if vec[node["f"]] else node["l"]
        proba = node.get("proba", {})
        label = node["label"]
        return label, float(proba.get(label, 1.0))


class MlpModel:
    """Pure-Python forward pass for the LIDSNet MLP. JSON:
    {"layers":[{"W":[[..]],"b":[..],"act":"relu"|null}], "labels":[..], "vocab":[..]}
    W is stored [out][in] so y[o] = sum_i W[o][i]*x[i] + b[o].
    """

    def __init__(self, layers: List[dict], labels: List[str], vocab: List[str]):
        self.layers = layers
        self.labels = labels
        self.vocab = vocab

    @classmethod
    def load(cls, path: str | Path) -> "MlpModel":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["layers"], data["labels"], data["vocab"])

    def _forward(self, vec: List[int]) -> List[float]:
        x = [float(v) for v in vec]
        for layer in self.layers:
            W, b = layer["W"], layer["b"]
            y = [sum(w_i * x_i for w_i, x_i in zip(row, x)) + bias
                 for row, bias in zip(W, b)]
            if layer.get("act") == "relu":
                y = [v if v > 0 else 0.0 for v in y]
            x = y
        return x

    def predict(self, vec: List[int]) -> Tuple[str, float]:
        logits = self._forward(vec)
        probs = _softmax(logits)
        best = max(range(len(probs)), key=lambda i: probs[i])
        return self.labels[best], probs[best]

    def predict_proba(self, vec: List[int]) -> Dict[str, float]:
        probs = _softmax(self._forward(vec))
        return {lab: p for lab, p in zip(self.labels, probs)}


def load_if_exists(path: str | Path, kind: str) -> Optional[object]:
    p = Path(path)
    if not p.exists():
        return None
    return CartModel.load(p) if kind == "cart" else MlpModel.load(p)


__all__ = ["CartModel", "MlpModel", "load_if_exists"]
