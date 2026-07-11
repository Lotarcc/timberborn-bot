"""StateFeaturizer - turn a Timberborn game state into a closed vocabulary of
symbolic feature strings, then a multi-hot vector against a fixed vocab.

This is the analog of NLP_2.0's PreprocessingLayer (which turned an utterance into
`POS=`, `lemma=`, `WH=` strings). Here continuous game quantities are bucketized
into a small closed vocabulary so the same one-hot / CART / tiny-MLP stack transfers
almost verbatim. Crucially it reuses planner.py's own accessors, so the features the
policy sees are read the exact same way the expert planner reads them - no drift.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from agent import planner

# The label space = the planner's goal ids. One classification head predicts one of
# these; execution (WHERE + followups) is handled by planner.candidates_for / followups.
ACTIONS: List[str] = [
    "build_lumberjack",
    "build_water_pump",
    "build_water_storage",
    "build_gatherer",
    "build_farm",
    "build_lodge",
    "build_warehouse",
    "build_inventor",
    "build_forester",
    "build_path",
    "designate_cutting",
    "designate_planting",
    "demolish_unreachable",
    "advance_time",
]
ACTION_INDEX: Dict[str, int] = {a: i for i, a in enumerate(ACTIONS)}


def _bucket(value: float, edges: Sequence[float], names: Sequence[str]) -> str:
    """Map value into a named bucket. len(names) must be len(edges)+1."""
    for edge, name in zip(edges, names):
        if value < edge:
            return name
    return names[-1]


def _unreachable_present(state: dict) -> bool:
    buildings = (state.get("buildings") or {}) if isinstance(state, dict) else {}
    listing = buildings.get("list")
    if isinstance(listing, list):
        for b in listing:
            if isinstance(b, dict) and b.get("status") == "finished" and b.get("reachable") is False:
                return True
    return False


def feature_strings(state: dict) -> List[str]:
    """The symbolic features for one state. Deterministic and order-independent
    (the vector is multi-hot, so order does not matter)."""
    f: List[str] = []

    # --- resources ---------------------------------------------------------
    logs = planner._logs_available(state)
    # 12 logs is the pump/inventor gate - make it a bucket boundary.
    f.append("logs=" + _bucket(logs, (1, 12, 30), ("none", "low", "ok", "high")))

    water_days = planner._resource_days(state, "Water")
    f.append("water_days=" + _bucket(water_days, (1.5, 3, 10), ("crit", "low", "ok", "high")))

    food_days = planner._resource_days(state, "Food", ("Berries", "Carrot", "Bread"))
    f.append("food_days=" + _bucket(food_days, (1.5, 3, 10), ("crit", "low", "ok", "high")))

    # --- population --------------------------------------------------------
    pop = (state.get("population") or {}) if isinstance(state, dict) else {}
    total = planner._as_int(pop.get("total"), 0)
    f.append("pop=" + _bucket(total, (6, 15), ("tiny", "small", "large")))
    homeless = planner._as_int(pop.get("homeless"), 0)
    f.append("homeless=" + ("yes" if homeless > 0 else "no"))

    # --- what already exists (drives what to build next) -------------------
    for spec, tag in (
        ("LumberjackFlag", "lumberjack"),
        ("WaterPump", "pump"),
        ("GathererFlag", "gatherer"),
        ("ForesterFlag", "forester"),
        ("Inventor", "inventor"),
        ("Lodge", "lodge"),
        ("EfficientFarmhouse", "farm"),
        ("SmallWarehouse", "warehouse"),
    ):
        f.append(f"has_{tag}=" + ("yes" if planner._building_count(state, spec) > 0 else "no"))

    f.append("has_food_prod=" + ("yes" if planner._has_food_production(state) else "no"))
    f.append("has_storage=" + ("yes" if planner._has_storage(state) else "no"))

    # water storage vs the hazard-buffer target
    units = planner._water_storage_units(state)
    target = planner._water_storage_target(state)
    if units <= 0:
        water_store = "none"
    elif units < target:
        water_store = "under"
    else:
        water_store = "met"
    f.append("water_store=" + water_store)

    # --- situational -------------------------------------------------------
    f.append("building_now=" + ("yes" if planner._sites_under_construction(state) else "no"))
    f.append("unreachable=" + ("yes" if _unreachable_present(state) else "no"))

    hazard = planner._hazard_buffer_days(state)  # next-weather duration + 2
    f.append("drought_in=" + _bucket(hazard, (4, 8), ("soon", "mid", "far")))

    return f


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
