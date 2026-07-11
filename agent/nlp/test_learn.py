"""Tests for agent/nlp/learn.py - the run-to-run outcome-relabeling loop.

Reuses the thirst-death / survivor trace fixtures already defined on
replay.ReplayTests (see the "shared fixtures" note in agent/replay.py) so these
tests exercise a REAL replay.credit_assignment trace end to end rather than
stubbing it - the override behavior in build_augmented_dataset is derived from
whatever feature vectors that real trace actually produces, not hand-guessed
bucket strings, so this stays correct even if game_schema's bucketing changes.

Runnable BOTH ways:
    .venv/bin/python -m unittest agent.nlp.test_learn -v
    .venv/bin/python -m pytest agent/nlp/test_learn.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from agent import game_schema, replay
from agent.nlp import learn

# Reuse replay.py's own fixture shapes (plain data, not test methods - see the
# "shared fixtures" comment in agent/replay.py) instead of re-deriving them.
_state = replay.ReplayTests._state
_THIRST_DEATH_STEPS = replay.ReplayTests._THIRST_DEATH_STEPS
_SURVIVOR_STEPS = replay.ReplayTests._SURVIVOR_STEPS


def _write_trace(run_id, steps):
    for i, (state_kwargs, action) in enumerate(steps, start=1):
        replay.record_step(run_id, i, _state(**state_kwargs), action)


class _RunFixtureMixin:
    """setUp/tearDown for hermetic run_id-backed tests (temp .jsonl per test)."""

    def setUp(self):
        self._run_ids = []

    def tearDown(self):
        for run_id in self._run_ids:
            try:
                os.remove(replay._run_path(run_id))
            except OSError:
                pass

    def _new_run_id(self, label):
        run_id = "ut_learn_%s_%s" % (label, uuid.uuid4().hex[:8])
        self._run_ids.append(run_id)
        return run_id


# ---------------------------------------------------------------------------
# examples_from_run
# ---------------------------------------------------------------------------

class ExamplesFromRunTests(_RunFixtureMixin, unittest.TestCase):
    def test_well_formed_rows_from_a_real_thirst_death_trace(self):
        run_id = self._new_run_id("thirst")
        _write_trace(run_id, _THIRST_DEATH_STEPS)

        examples = learn.examples_from_run(run_id)
        self.assertTrue(examples)
        valid_actions = set(game_schema.actions())
        for ex in examples:
            self.assertEqual(set(ex.keys()), {"features", "label", "source", "weight"})
            self.assertIsInstance(ex["features"], list)
            self.assertTrue(ex["features"])
            self.assertIn(ex["label"], valid_actions)
            self.assertEqual(ex["source"], "credit_assignment")
            self.assertGreater(ex["weight"], 1.0)
        # No WaterPump ever existed in this trace -> every window's correction
        # agrees with replay's own credit_assignment test for this fixture.
        self.assertTrue(all(ex["label"] == replay.GOAL_WATER_PUMP for ex in examples))

    def test_empty_for_a_survivor_run(self):
        run_id = self._new_run_id("survivor")
        _write_trace(run_id, _SURVIVOR_STEPS)
        self.assertEqual(learn.examples_from_run(run_id), [])

    def test_empty_run_id_returns_empty_list(self):
        self.assertEqual(learn.examples_from_run("does_not_exist_%s" % uuid.uuid4().hex), [])

    def test_skips_windows_with_no_better_action(self):
        # Stall scenario where WaterPump/GathererFlag/LumberjackFlag are ALL already
        # built -> replay._stall_better_action returns None for every window entry
        # (see replay.test_credit_assignment_stall_has_no_better_action_when_survival_
        # buildings_built) -> examples_from_run must defensively skip all of them
        # (None is never a member of game_schema.actions()).
        run_id = self._new_run_id("stall_all_built")
        counts = {
            "DistrictCenter.Folktails": 1,
            "WaterPump.Folktails": 1,
            "GathererFlag.Folktails": 1,
            "LumberjackFlag.Folktails": 1,
        }
        steps = [
            (dict(day=1, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
                  log=5, plank=0, counts=counts), "build_lumberjack_flag"),
        ]
        for i in range(8):
            steps.append(
                (dict(day=1 + i, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
                      log=5, plank=0, counts=counts), "advance_time")
            )
        _write_trace(run_id, steps)

        # Sanity: credit_assignment really does have windows here, just none with a
        # usable better_action - otherwise this test would pass vacuously.
        self.assertTrue(replay.credit_assignment(run_id))
        self.assertEqual(learn.examples_from_run(run_id), [])

    def test_skips_windows_with_empty_features(self):
        # Simulate a run recorded before the record_step/features coupling fix
        # (or any malformed row): credit_assignment forwards whatever "features"
        # the stored row had, which is [] via replay's `row.get("features") or []`
        # fallback when the key is absent entirely.
        run_id = self._new_run_id("no_features")
        for i, (state_kwargs, action) in enumerate(_THIRST_DEATH_STEPS, start=1):
            record = replay.record_step(run_id, i, _state(**state_kwargs), action)
            self.assertIn("features", record)  # the coupling fix actually wrote one

        # Rewrite the run file with "features" stripped from every row, as if it
        # had been recorded by the pre-coupling-fix version of record_step.
        rows = replay.load_run(run_id)
        for row in rows:
            row.pop("features", None)
        path = replay._run_path(run_id)
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row))
                fh.write("\n")

        self.assertEqual(learn.examples_from_run(run_id), [])


# ---------------------------------------------------------------------------
# build_augmented_dataset - the override-merge semantics are the key behavior
# ---------------------------------------------------------------------------

class BuildAugmentedDatasetTests(_RunFixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.mkdtemp(prefix="learn_test_")

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_base(self, rows, name="base.json"):
        path = Path(self._tmpdir) / name
        path.write_text(json.dumps(rows), encoding="utf-8")
        return path

    def test_correction_overrides_base_label_for_the_same_feature_vector(self):
        run_id = self._new_run_id("thirst")
        _write_trace(run_id, _THIRST_DEATH_STEPS)

        examples = learn.examples_from_run(run_id)
        self.assertTrue(examples)
        corrected = examples[0]
        corrected_key = tuple(sorted(corrected["features"]))
        wrong_label = "advance_time"
        self.assertNotEqual(wrong_label, corrected["label"])

        # A second, deliberately UNRELATED base row using a feature string that
        # feature_strings() can never emit (closed vocabulary), so it is
        # guaranteed - by construction, not by luck - to never collide with
        # anything this run's credit_assignment produces.
        untouched_features = ["zzz_test_marker=never_produced_by_feature_strings"]
        base_rows = [
            {"features": list(corrected["features"]), "label": wrong_label},
            {"features": untouched_features, "label": "advance_time"},
        ]
        base_path = self._write_base(base_rows)
        out_path = Path(self._tmpdir) / "out.json"

        # Every distinct feature key this run's examples touch, so the expected
        # added_rows count is derived from the SAME ground truth the function
        # itself will see - not a hand-guessed number that could silently drift
        # if game_schema's bucketing ever changes.
        distinct_keys = {tuple(sorted(ex["features"])) for ex in examples}
        expected_added = len(distinct_keys - {corrected_key})

        stats = learn.build_augmented_dataset([run_id], base=base_path, out=out_path)

        self.assertEqual(stats["base_rows"], 2)
        self.assertEqual(stats["overridden"], 1)
        self.assertEqual(stats["added_rows"], expected_added)
        self.assertGreater(stats["added_rows"], 0)  # a genuinely-new key really was added
        self.assertEqual(stats["total_rows"], 2 + expected_added)
        self.assertEqual(stats["out_path"], str(out_path))

        out_rows = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(len(out_rows), stats["total_rows"])
        by_key = {tuple(sorted(r["features"])): r for r in out_rows}

        # The corrected key now carries the corrected label, not the base one.
        self.assertIn(corrected_key, by_key)
        self.assertEqual(by_key[corrected_key]["label"], corrected["label"])
        self.assertNotEqual(by_key[corrected_key]["label"], wrong_label)

        # The unrelated base row is byte-for-byte untouched.
        untouched_key = tuple(sorted(untouched_features))
        self.assertIn(untouched_key, by_key)
        self.assertEqual(by_key[untouched_key]["label"], "advance_time")
        self.assertEqual(by_key[untouched_key]["features"], untouched_features)

        # Every newly-added key is labeled from this run's corrections, and is a
        # real member of the action space (defensive: examples_from_run already
        # filters this, this re-checks the written file, not just the return value).
        valid_actions = set(game_schema.actions())
        for key in distinct_keys - {corrected_key}:
            self.assertIn(by_key[key]["label"], valid_actions)

    def test_repeated_correction_for_the_same_key_does_not_double_count_overridden(self):
        # Two runs whose credit_assignment both land a correction on the SAME base
        # feature key (the second run's better_action differs from the first's
        # already-corrected label) must still count as exactly one overridden row -
        # "overridden" tracks base rows whose FINAL label differs from their
        # ORIGINAL label, not the number of times a key was touched.
        thirst_run = self._new_run_id("thirst")
        _write_trace(thirst_run, _THIRST_DEATH_STEPS)
        thirst_examples = learn.examples_from_run(thirst_run)
        self.assertTrue(thirst_examples)
        target_key = tuple(sorted(thirst_examples[0]["features"]))

        # A run that, on its own, would ALSO produce a correction for
        # target_key's exact bag-of-features (regardless of stored order) with a
        # DIFFERENT label than the thirst run's correction - a hunger-death trace
        # sharing the same feature vector is unrealistic to construct by hand, so
        # instead directly craft the base row from target_key and rely on
        # examples_from_run twice from the SAME run_id (still two distinct
        # `run_ids` entries from the caller's point of view) to prove the
        # dedup-by-final-state property.
        base_rows = [{"features": list(thirst_examples[0]["features"]), "label": "advance_time"}]
        base_path = self._write_base(base_rows)
        out_path = Path(self._tmpdir) / "out.json"

        stats = learn.build_augmented_dataset(
            [thirst_run, thirst_run], base=base_path, out=out_path
        )
        self.assertEqual(stats["overridden"], 1)

    def test_no_run_ids_leaves_base_dataset_unchanged(self):
        base_rows = [{"features": ["pop=tiny"], "label": "advance_time"}]
        base_path = self._write_base(base_rows)
        out_path = Path(self._tmpdir) / "out.json"

        stats = learn.build_augmented_dataset([], base=base_path, out=out_path)

        self.assertEqual(stats, {
            "base_rows": 1, "added_rows": 0, "overridden": 0,
            "total_rows": 1, "out_path": str(out_path),
        })
        self.assertEqual(json.loads(out_path.read_text(encoding="utf-8")), base_rows)

    def test_out_defaults_to_base_path_in_place(self):
        run_id = self._new_run_id("thirst")
        _write_trace(run_id, _THIRST_DEATH_STEPS)
        base_rows = [{"features": ["zzz_never=collides"], "label": "advance_time"}]
        base_path = self._write_base(base_rows)

        stats = learn.build_augmented_dataset([run_id], base=base_path)

        self.assertEqual(stats["out_path"], str(base_path))
        on_disk = json.loads(base_path.read_text(encoding="utf-8"))
        self.assertEqual(len(on_disk), stats["total_rows"])


# ---------------------------------------------------------------------------
# retrain_command
# ---------------------------------------------------------------------------

class RetrainCommandTests(unittest.TestCase):
    def test_default_is_local_mps(self):
        cmd = learn.retrain_command()
        self.assertIn(".venv/bin/python -m agent.nlp.train_cart", cmd)
        self.assertIn(".venv/bin/python -m agent.nlp.train_lidsnet", cmd)
        self.assertNotIn("ssh", cmd)
        self.assertNotIn("scp", cmd)

    def test_explicit_none_matches_default(self):
        self.assertEqual(learn.retrain_command(None), learn.retrain_command())

    def test_cka_win_returns_scp_ssh_scp_back_sequence(self):
        cmd = learn.retrain_command("cka-win")
        self.assertIn("scp", cmd)
        self.assertIn("ssh cka-win", cmd)
        self.assertIn("train_cart.py", cmd)
        self.assertIn("train_lidsnet.py", cmd)
        # scp-back: the trained artifacts return to the local data dir.
        self.assertIn("decision_cart.json", cmd)
        self.assertIn("decision_mlp.json", cmd)

    def test_unknown_host_raises(self):
        with self.assertRaises(ValueError):
            learn.retrain_command("some-other-box")


# ---------------------------------------------------------------------------
# the coupling fix (Task 7a step 1): record_step rows carry "features", and
# credit_assignment's regret windows forward them from the recorded rows.
# ---------------------------------------------------------------------------

class CouplingTests(_RunFixtureMixin, unittest.TestCase):
    def test_record_step_rows_and_credit_assignment_windows_carry_features(self):
        run_id = self._new_run_id("coupling")
        _write_trace(run_id, _THIRST_DEATH_STEPS)

        rows = replay.load_run(run_id)
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("features", row)
            self.assertIsInstance(row["features"], list)
            self.assertTrue(row["features"])

        entries = replay.credit_assignment(run_id)
        self.assertTrue(entries)
        rows_by_step = {r["step"]: r for r in rows}
        for entry in entries:
            self.assertIn("features", entry)
            # Exactly the features recorded for that step - proves credit_assignment
            # forwards the stored vector rather than recomputing or dropping it.
            self.assertEqual(entry["features"], rows_by_step[entry["step"]]["features"])


if __name__ == "__main__":
    unittest.main()
