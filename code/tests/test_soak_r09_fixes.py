"""Regression tests for Run-09 soak fix: brief natural-regen idle pause when critically low HP & full food."""
from __future__ import annotations

import unittest

from mcagent.loop.policy import RulePlanner
from mcagent.runtime import RuntimeConfig, State
from mcagent.skills.world_memo import reset_all_world_memos


class TestSoakR09Fixes(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()

    def tearDown(self):
        reset_all_world_memos()

    def test_critically_low_hp_with_full_food_and_safe_surroundings_pauses_to_regen(self):
        st = State(RuntimeConfig())
        st.set_status({
            "pos": [-71.3, 27.0, 69.7],
            "dimension": "overworld",
            "ready": True,
            "alive": True,
            "health": 3.5,
            "food": 20.0,
            "in_water": False,
            "submerged": False,
            "in_lava": False,
            "on_fire": False,
            "nearest_hostile": None,
        })
        st.set_inventory({
            "items": [{"name": "stone_pickaxe", "count": 1, "slot": 0}],
            "armor": {"head": None, "chest": None, "legs": None, "feet": None},
            "empty_slots": 35,
        })
        dec = RulePlanner(min_verified="offline").choose_urgent_survival_or_safe_mode(st)
        self.assertIsNotNone(dec)
        self.assertIsNone(dec.skill)
        self.assertEqual(dec.source, "idle")
        self.assertIn("回血", dec.reason)

    def test_low_hp_does_not_pause_when_hostile_within_16m_or_food_below_18(self):
        st = State(RuntimeConfig())
        st.set_status({
            "pos": [-71.3, 27.0, 69.7],
            "dimension": "overworld",
            "ready": True,
            "alive": True,
            "health": 5.0,
            "food": 16.0,
            "in_water": False,
            "submerged": False,
            "in_lava": False,
            "on_fire": False,
            "nearest_hostile": None,
        })
        st.set_inventory({
            "items": [{"name": "stone_pickaxe", "count": 1, "slot": 0}],
            "armor": {},
            "empty_slots": 35,
        })
        dec = RulePlanner(min_verified="offline").choose_urgent_survival_or_safe_mode(st)
        self.assertIsNone(dec)

        # With hostile at 12m (outside 10m flee, inside 16m), do not stand idle
        st.set_status({
            "pos": [-71.3, 27.0, 69.7],
            "dimension": "overworld",
            "ready": True,
            "alive": True,
            "health": 5.0,
            "food": 20.0,
            "in_water": False,
            "submerged": False,
            "in_lava": False,
            "on_fire": False,
            "nearest_hostile": {"id": 1, "name": "skeleton", "dist": 12.5},
        })
        dec2 = RulePlanner(min_verified="offline").choose_urgent_survival_or_safe_mode(st)
        self.assertTrue(dec2 is None or dec2.source != "idle")


if __name__ == "__main__":
    unittest.main()
