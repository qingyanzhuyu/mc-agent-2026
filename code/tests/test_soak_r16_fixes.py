"""Regression tests for Run-16 soak fix: prevent 1400x smelt_iron busy-spin while async iron lease is still smelting."""
from __future__ import annotations

import time
import unittest

from mcagent.loop.policy import RulePlanner
from mcagent.runtime import RuntimeConfig, State
from mcagent.skills.furnace_lease import lease_book_for
from mcagent.skills.iron import SmeltIron
from mcagent.skills.world_memo import reset_all_world_memos


class TestSoakR16Fixes(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()

    def tearDown(self):
        reset_all_world_memos()

    def test_smelt_iron_blocked_while_unready_async_iron_lease_active_and_no_raw_iron(self):
        st = State(RuntimeConfig())
        st.set_status({
            "pos": [0.5, 64.0, 0.5],
            "dimension": "overworld",
            "ready": True,
            "alive": True,
            "health": 20.0,
            "food": 20.0,
            "is_day": True,
            "nearest_hostile": None,
        })
        st.set_inventory({
            "items": [
                {"name": "stone_pickaxe", "count": 1, "slot": 0},
                {"name": "coal", "count": 4, "slot": 1},
            ],
            "armor": {},
            "empty_slots": 34,
        })
        now = time.monotonic()
        lease_book_for(st).register_lease(
            [2, 64, 0],
            dimension="overworld",
            station_kind="furnace",
            input_item="raw_iron",
            expected_output="iron_ingot",
            count=4,
            started_at=now,
            ready_at=now + 40.0,
            consumer="smelt_iron",
        )
        # While lease is still smelting and no raw_iron in bag, SmeltIron.blocked must block to prevent busy-spin
        self.assertNotEqual(SmeltIron.blocked(st), "")

        # Once lease becomes ready, SmeltIron.blocked must unblock immediately to retrieve ingots
        lease_book_for(st).register_lease(
            [2, 64, 0],
            dimension="overworld",
            station_kind="furnace",
            input_item="raw_iron",
            expected_output="iron_ingot",
            count=4,
            started_at=now - 50.0,
            ready_at=now - 10.0,
            consumer="smelt_iron",
        )
        self.assertEqual(SmeltIron.blocked(st), "")


if __name__ == "__main__":
    unittest.main()
