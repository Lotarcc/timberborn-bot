"""StateFeaturizer - turn a Timberborn game state into a closed vocabulary of
symbolic feature strings, then a multi-hot vector against a fixed vocab.

This is the analog of NLP_2.0's PreprocessingLayer (which turned an utterance into
`POS=`, `lemma=`, `WH=` strings). Here continuous game quantities are bucketized
into a small closed vocabulary so the same one-hot / CART / tiny-MLP stack transfers
almost verbatim.

Both the action space and the feature vocabulary are DERIVED from the game database
(agent/game_schema.py, itself sourced from agent/data/{buildings,goods,...}.json)
rather than hand-coded here. This keeps the model in sync as buildings/goods/needs
are added to the database, and guarantees the features the policy sees are read the
exact same way game_schema (and, through it, planner.py) reads them - no drift.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from agent import game_schema

# The label space = game_schema's DB-driven action space: build_<snake> for every
# gameplay building, plus the verb actions (designate_cutting, designate_planting,
# demolish_unreachable, advance_time). One classification head predicts one of
# these; execution (WHERE + followups) is handled by planner.candidates_for /
# followups.
ACTIONS: List[str] = list(game_schema.actions())
ACTION_INDEX: Dict[str, int] = {a: i for i, a in enumerate(ACTIONS)}


def feature_strings(state: dict) -> List[str]:
    """The symbolic features for one state. Deterministic and order-independent
    (the vector is multi-hot, so order does not matter). Delegates to
    game_schema.feature_strings - the DB-grounded featurizer that covers the whole
    economy (resources, production capacity, per-category building counts, power
    balance, well-being) - so this module's vocabulary always matches the DB the
    action space itself is derived from."""
    return game_schema.feature_strings(state)


class StateFeaturizer:
    """Holds a fixed vocabulary (built from the training set) and vectorizes states.
    vocab is the sorted list of every feature string seen at fit time; unknown
    strings at inference time are ignored (standard bag-of-features behavior)."""

    def __init__(self, vocab: Sequence[str]):
        self.vocab = list(vocab)
        self.index = {s: i for i, s in enumerate(self.vocab)}

    @classmethod
    def fit(cls, states: Sequence[dict]) -> "StateFeaturizer":
        seen = set()
        for s in states:
            seen.update(feature_strings(s))
        return cls(sorted(seen))

    def transform(self, state: dict) -> List[int]:
        vec = [0] * len(self.vocab)
        for s in feature_strings(state):
            idx = self.index.get(s)
            if idx is not None:
                vec[idx] = 1
        return vec

    def to_dict(self) -> dict:
        return {"vocab": self.vocab}

    @classmethod
    def from_dict(cls, data: dict) -> "StateFeaturizer":
        return cls(data["vocab"])


__all__ = ["ACTIONS", "ACTION_INDEX", "feature_strings", "StateFeaturizer"]
