"""Regression tests for Run-20: placed furnace prereq satisfaction and mine trail re-entry."""
from __future__ import annotations

import math
import unittest

from mcagent.loop.goals import GOAL_MAP, is_prereq_satisfied
from mcagent.runtime import RuntimeConfig, State
from mcagent.skills.furnace_lease import lease_book_for, reset_all_furnace_leases
from mcagent.skills.items import MINE_TRAILS
from mcagent.skills.mine_return import _climb_out_stairs, return_to_surface_from_mine
from mcagent.skills.testing import GridWorld
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


def _drive(gen):
    steps = []
    try:
        while True:
            steps.append(next(gen))
    except StopIteration as stop:
        return stop.value, steps


class _TrailWorld(GridWorld):
    def __init__(self, *, pos, blocks=None, items=None, max_move_dist=12.0):
        super().__init__(pos=list(pos), blocks=blocks or {}, items=items or {})
        self.st.update(dimension="overworld", ready=True, alive=True, on_ground=True, health=20.0, food=20.0)
        self.max_move_dist = max_move_dist
        self.move_targets: list[list[float]] = []

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
        dest = [float(v) for v in (pos if pos is not None else [x, y, z])]
        self.move_targets.append(dest)
        if math.dist(self.st["pos"], dest) > self.max_move_dist:
            from mcagent.client import ApiError
            raise ApiError("path_not_found", "too far across side cave")
        self.st["pos"] = list(dest)
        return {"pos": list(dest)}


class _BreakingPickWorld(GridWorld):
    def __init__(self):
        blocks = {
            (1, 30, 0): "stone",
            (0, 32, 0): "stone",
            (1, 31, 0): "stone",
            (1, 32, 0): "stone",
            (1, 33, 0): "stone",
        }
        super().__init__(pos=[0.5, 30.0, 0.5], blocks=blocks, items={"wooden_pickaxe": 1})
        self.st.update(dimension="overworld", ready=True, alive=True, on_ground=True, health=20.0, food=20.0)
        self.digs = 0

    def dig(self, pos, **kwargs):
        self.digs += 1
        cell = tuple(int(v) for v in pos)
        self.blocks[cell] = "air"
        # Last pickaxe breaks after 1st dig
        if self.digs >= 1:
            self.items.pop("wooden_pickaxe", None)
        return {"dug": "stone"}


class TestRun20FurnaceAndTrail(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()
        reset_all_furnace_leases()
        MINE_TRAILS.clear()

    def tearDown(self):
        reset_all_world_memos()
        reset_all_furnace_leases()
        MINE_TRAILS.clear()

    def test_prep_furnace_prereq_satisfied_by_nearby_placed_furnace_or_active_lease(self):
        spec = GOAL_MAP["prep_furnace"]
        st = State(RuntimeConfig())
        st.set_status({"pos": [-44.5, -3.0, 97.5], "dimension": "overworld", "ready": True, "alive": True})
        st.set_inventory({"items": [{"name": "stone_pickaxe", "count": 1, "slot": 0}], "empty_slots": 35})

        # Empty bag, no placed furnace -> unsatisfied
        self.assertFalse(is_prereq_satisfied(spec, st))

        # Place furnace at [-44, -3, 97] in memo -> satisfied!
        memo_for(st).record_station("furnace", [-44, -3, 97], dimension="overworld")
        self.assertTrue(is_prereq_satisfied(spec, st))

        # Respawn 70m away on surface (no furnace in bag, far from placed furnace) -> unsatisfied!
        st.set_status({"pos": [-63.5, 69.0, 16.5], "dimension": "overworld", "ready": True, "alive": True})
        self.assertFalse(is_prereq_satisfied(spec, st))

        # Active furnace lease within 48m -> satisfied!
        st.set_status({"pos": [-44.5, 10.0, 97.5], "dimension": "overworld", "ready": True, "alive": True})
        lease_book_for(st).register_lease(
            [-44, -3, 97],
            dimension="overworld",
            station_kind="furnace",
            input_item="raw_iron",
            expected_output="iron_ingot",
            count=2,
            consumer="smelt_iron",
        )
        self.assertTrue(is_prereq_satisfied(spec, st))

    def test_return_mine_trail_visits_trail_end_first_when_offset_in_side_cave(self):
        # Trail goes from surface Y=64 down to Y=4; bot stepped 9m aside to [-44.5, -3, 97.5]
        trail = [[float(-41.5), float(64 - i * 4), float(88.5)] for i in range(16)]  # Y=64 .. Y=4
        MINE_TRAILS.append(trail)
        w = _TrailWorld(pos=[-44.5, -3.0, 97.5], max_move_dist=13.0)
        ex, st = w.session()
        ok, _ = _drive(return_to_surface_from_mine(ex, st, target_y=64.0, max_climb_steps=0))
        self.assertTrue(ok, f"expected return via trail, targets={w.move_targets}")
        self.assertEqual(w.move_targets[0], trail[-1], f"expected first move to trail[-1], got {w.move_targets[0]}")

    def test_climb_out_stairs_handles_last_pickaxe_breaking_mid_step(self):
        w = _BreakingPickWorld()
        ex, st = w.session()
        ok, _ = _drive(_climb_out_stairs(ex, st, target_y=64.0, max_steps=2))
        self.assertFalse(ok)
        self.assertEqual(w.digs, 1)


if __name__ == "__main__":
    unittest.main()
