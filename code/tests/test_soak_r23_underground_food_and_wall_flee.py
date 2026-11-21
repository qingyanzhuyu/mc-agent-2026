"""Regression tests for Run-23: block underground gather_food dispatch and steer flee around cave walls."""
from __future__ import annotations

import math
import unittest

from mcagent.client import ApiError
from mcagent.loop.policy import RulePlanner
from mcagent.runtime import RuntimeConfig, State
from mcagent.skills.escape import escape_hazard_step
from mcagent.skills.testing import GridWorld
from mcagent.skills.world_memo import reset_all_world_memos


class _CaveWallFleeWorld(GridWorld):
    def __init__(self):
        # Floor at Y=33 around origin; solid 3-high wall directly East (x=1, z=0); open corridor North (x=0, z=-1..-4)
        blocks = {}
        for x in range(-3, 4):
            for z in range(-5, 3):
                blocks[(x, 33, z)] = "stone"
                blocks[(x, 34, z)] = "air"
                blocks[(x, 35, z)] = "air"
                blocks[(x, 36, z)] = "air"
        for dy in (0, 1, 2):
            blocks[(1, 34 + dy, 0)] = "stone"
            blocks[(1, 34 + dy, -1)] = "stone"
            blocks[(1, 34 + dy, 1)] = "stone"
        super().__init__(pos=[0.5, 34.0, 0.5], blocks=blocks, items={})
        self.st.update(
            dimension="overworld",
            ready=True,
            alive=True,
            on_ground=True,
            health=6.2,
            food=17.0,
            nearest_hostile={"id": 55, "name": "zombie", "dist": 6.5, "pos": [-6.0, 34.0, 0.5]},
        )
        self.control_yaws: list[float] = []

    def move_to(self, *args, **kwargs):
        raise ApiError("path_not_found", "cave wall blocks straight away path")

    def control(self, **kwargs):
        yaw = float(kwargs.get("yaw", 0.0))
        self.control_yaws.append(yaw)
        return {"ms": kwargs.get("ms", 800), "pos": list(self.st["pos"]), "moved": 1.0}


def _drive(gen):
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


class TestRun23UndergroundFoodAndWallFlee(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()

    def tearDown(self):
        reset_all_world_memos()

    def test_policy_skips_proactive_gather_food_deep_underground(self):
        rp = RulePlanner(min_verified="offline")
        # Deep underground at Y=34.5 -> must NOT dispatch surface gather_food
        st_cave = State(RuntimeConfig())
        st_cave.set_status({
            "pos": [-113.2, 34.5, 30.5], "dimension": "overworld", "ready": True,
            "alive": True, "health": 20.0, "food": 20.0, "is_day": True, "nearest_hostile": None,
        })
        st_cave.set_inventory({"items": [{"name": "stone_sword", "count": 1, "slot": 0}], "empty_slots": 35})
        dec_cave = rp.choose_proactive_survival(st_cave)
        self.assertIsNone(dec_cave, f"should not dispatch surface gather_food underground, got {dec_cave}")

        # On surface at Y=68.0 -> still dispatches proactive gather_food normally
        st_surf = State(RuntimeConfig())
        st_surf.set_status({
            "pos": [-63.5, 68.0, 16.5], "dimension": "overworld", "ready": True,
            "alive": True, "health": 20.0, "food": 20.0, "is_day": True, "nearest_hostile": None,
        })
        st_surf.set_inventory({"items": [{"name": "stone_sword", "count": 1, "slot": 0}], "empty_slots": 35})
        dec_surf = rp.choose_proactive_survival(st_surf)
        self.assertIsNotNone(dec_surf)
        self.assertEqual(dec_surf.skill.name, "gather_food")

    def test_flee_hostile_steers_around_solid_cave_wall_when_move_to_fails(self):
        w = _CaveWallFleeWorld()
        ex, st = w.session()
        _drive(escape_hazard_step(ex, st, "flee"))
        self.assertTrue(w.control_yaws, "expected control fallback after move_to path_not_found")
        # Direct away_yaw is East (+X), which is -pi/2 (-1.571). Must NOT faceplant into the East wall!
        chosen_yaw = w.control_yaws[0]
        east_dx, east_dz = -math.sin(chosen_yaw), -math.cos(chosen_yaw)
        self.assertLess(east_dx, 0.5, f"expected alternate corridor yaw away from East wall, got yaw={chosen_yaw:.3f}")


if __name__ == "__main__":
    unittest.main()
