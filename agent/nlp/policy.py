"""DecisionPolicy - the runtime front-end that replaces the per-turn LLM.

Featurize the game STATE, run both learned heads (CART + LIDSNet MLP), and return a
ranked list of intents (goal_ids). This mirrors NLP_2.0/AI-Player: the fast learned
heads decide; the slow LLM is only consulted when they disagree AND confidence is low
(arbitration hook), otherwise it never runs. All inference is dependency-free JSON.

Usage:
    policy = DecisionPolicy.load()          # reads agent/data/decision_*.json
    ranked = policy.rank(state)             # [(goal_id, confidence), ...] best first
    goal_id = policy.decide(state)          # top intent
The runtime maps goal_id -> placement via planner.candidates_for and executes; if the
top intent is not executable it walks down `ranked`.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from agent.nlp import features as feat
from agent.nlp import model as M

_DATA = Path(__file__).resolve().parent.parent / "data"

# If the two heads disagree and the winner's confidence is below this, the caller
# may escalate to the LLM (arbitrate hook). Kept conservative.
LOW_CONFIDENCE = 0.6


class DecisionPolicy:
    def __init__(self, featurizer: feat.StateFeaturizer,
                 cart: Optional[M.CartModel], mlp: Optional[M.MlpModel]):
        self.featurizer = featurizer
        self.cart = cart
        self.mlp = mlp
        if cart is None and mlp is None:
            raise ValueError("no decision heads found - train_cart / train_lidsnet first")

    @classmethod
    def load(cls, data_dir: Path = _DATA) -> "DecisionPolicy":
        import json
        vocab = json.loads((data_dir / "decision_vocab.json").read_text(encoding="utf-8"))["vocab"]
        featurizer = feat.StateFeaturizer(vocab)
        cart = M.load_if_exists(data_dir / "decision_cart.json", "cart")
        mlp = M.load_if_exists(data_dir / "decision_mlp.json", "mlp")
        return cls(featurizer, cart, mlp)

    def rank(self, state: dict) -> List[Tuple[str, float]]:
        """Ranked intents best-first. Uses the MLP's full distribution when present
        (richer than CART's leaf proba); CART acts as agreement check / tie-break."""
        vec = self.featurizer.transform(state)
        if self.mlp is not None:
            probs = self.mlp.predict_proba(vec)
            ranked = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
            if self.cart is not None:
                cart_label, cart_conf = self.cart.predict(vec)
                top_label, top_conf = ranked[0]
                # Heads agree -> boost; disagree & MLP unsure -> promote CART's pick.
                if cart_label != top_label and top_conf < LOW_CONFIDENCE:
                    ranked = [(cart_label, max(cart_conf, top_conf))] + \
                             [rc for rc in ranked if rc[0] != cart_label]
            return ranked
        # CART only
        label, conf = self.cart.predict(vec)
        return [(label, conf)]

    def decide(self, state: dict) -> str:
        return self.rank(state)[0][0]

    def disagreement(self, state: dict) -> Optional[Tuple[str, str, float]]:
        """(cart_label, mlp_label, mlp_conf) when the heads disagree AND confidence is
        low - the signal to escalate to the LLM. None otherwise."""
        if self.cart is None or self.mlp is None:
            return None
        vec = self.featurizer.transform(state)
        cart_label, _ = self.cart.predict(vec)
        mlp_label, mlp_conf = self.mlp.predict(vec)
        if cart_label != mlp_label and mlp_conf < LOW_CONFIDENCE:
            return cart_label, mlp_label, mlp_conf
        return None


__all__ = ["DecisionPolicy", "LOW_CONFIDENCE"]
