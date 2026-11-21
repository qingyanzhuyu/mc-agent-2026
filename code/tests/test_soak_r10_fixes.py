"""Regression tests for Run-10 soak fix: do not suicide across deep caves to chase distant surface furnace leases."""
from __future__ import annotations

import unittest

from mcagent.loop.policy import RulePlanner
from mcagent.runtime import RuntimeConfig, State
from mcagent.skills.furnace_lease import lease_book_for
from mcagent.skills.world_memo import reset_all_world_memos


class TestSoakR10Fixes(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()

    def tearDown(self):
        reset_all_world_memos()

    def test_distant_surface_food_lease_not_retrieved_from_deep_cave(self):
        st = State(RuntimeConfig())
        st.set_status({
            "pos": [-48.3, 31.9, 56.4],
            "dimension": "overworld",
            "ready": True,
            "alive": True,
            "health": 16.0,
            "food": 17.0,
            "is_day": True,
            "nearest_hostile": None,
        })
        st.set_inventory({
            "items": [
                {"name": "stone_pickaxe", "count": 1, "slot": 0},
                {"name": "cooked_beef", "count": 4, "slot": 1},
            ],
            "armor": {},
            "empty_slots": 34,
        })
        book = lease_book_for(st)
        book.register_lease(
            [-12, 70, 72],
            dimension="overworld",
            station_kind="furnace",
            input_item="porkchop",
            expected_output="cooked_porkchop",
            count=2,
            started_at=0.0,
            ready_at=0.0,
            consumer="gather_food",
        )
        dec = RulePlanner(min_verified="offline").choose_proactive_survival(st)
        self.assertTrue(dec is None or dec.skill.name != "gather_food")

    def test_nearby_ready_food_lease_still_retrieved_when_healthy(self):
        st = State(RuntimeConfig())
        st.set_status({
            "pos": [-10.5, 70.0, 70.5],
            "dimension": "overworld",
            "ready": True,
            "alive": True,
            "health": 18.0,
            "food": 17.0,
            "is_day": True,
            "nearest_hostile": None,
        })
        st.set_inventory({
            "items": [
                {"name": "stone_pickaxe", "count": 1, "slot": 0},
                {"name": "cooked_beef", "count": 4, "slot": 1},
            ],
            "armor": {},
            "empty_slots": 34,
        })
        book = lease_book_for(st)
        book.register_lease(
            [-12, 70, 72],
            dimension="overworld",
            station_kind="furnace",
            input_item="porkchop",
            expected_output="cooked_porkchop",
            count=2,
            started_at=0.0,
            ready_at=0.0,
            consumer="gather_food",
        )
        dec = RulePlanner(min_verified="offline").choose_proactive_survival(st)
        self.assertIsNotNone(dec)
        self.assertEqual(dec.skill.name, "gather_food")


if __name__ == "__main__":
    unittest.main()
