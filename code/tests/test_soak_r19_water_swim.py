"""Regression tests for Run-19 water-escape swim heading persistence and hostile-away steering."""
from __future__ import annotations

import math
import unittest

from mcagent.skills.escape import escape_water_to_shore
from mcagent.skills.testing import GridWorld


class _OpenWaterWorld(GridWorld):
    def __init__(self, *, pos=(0.5, 62.0, 0.5), nearest_hostile=None):
        super().__init__(pos=list(pos), blocks={}, items={})
        self.st.update(
            dimension="overworld",
            ready=True,
            alive=True,
            on_ground=False,
            in_water=True,
            submerged=False,
            health=10.0,
            food=17.0,
            nearest_hostile=nearest_hostile,
        )
        self.yaws: list[float] = []

    def control(self, **kwargs):
        yaw = float(kwargs.get("yaw", 0.0))
        self.yaws.append(yaw)
        # Mineflayer forward vector for yaw is (-sin(yaw), -cos(yaw))
        self.st["pos"][0] += -math.sin(yaw) * 1.2
        self.st["pos"][2] += -math.cos(yaw) * 1.2
        return {"ms": kwargs.get("ms", 600), "pos": list(self.st["pos"]), "moved": 1.2, "in_water": True}


def _drive_gen(gen):
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


class TestWaterSwimHeading(unittest.TestCase):
    def test_open_water_swim_keeps_heading_when_making_progress(self):
        w = _OpenWaterWorld(pos=(0.5, 62.0, 0.5))
        ex, st = w.session()
        _drive_gen(escape_water_to_shore(ex, st, max_steps=4))
        self.assertEqual(len(w.yaws), 4)
        # Must NOT spin in a 4-way square circle when each step advances 1.2m
        self.assertEqual(len(set(round(y, 3) for y in w.yaws)), 1, f"expected steady heading, got {w.yaws}")
        dist = math.hypot(w.st["pos"][0] - 0.5, w.st["pos"][2] - 0.5)
        self.assertGreaterEqual(dist, 4.0, f"expected straight swim >=4m, got {dist:.2f}m")

    def test_water_swim_steers_away_from_nearby_hostile(self):
        # Zombie is directly North at z=-4 (dz = -4.5); away vector is +Z (south), yaw = pi
        w = _OpenWaterWorld(
            pos=(0.5, 62.0, 0.5),
            nearest_hostile={"id": 99, "name": "zombie", "dist": 4.5, "pos": [0.5, 62.0, -4.0]},
        )
        ex, st = w.session()
        _drive_gen(escape_water_to_shore(ex, st, max_steps=3))
        self.assertEqual(len(w.yaws), 3)
        # Bot should have moved southward (z > 3.0), away from zombie at z=-4.0
        self.assertGreater(w.st["pos"][2], 3.0, f"expected swim away from zombie (+Z), ended at {w.st['pos']}")


if __name__ == "__main__":
    unittest.main()
