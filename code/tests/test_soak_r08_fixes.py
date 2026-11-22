"""Regression tests for Run-08 soak fix: flee_hostile must not blind-sprint off a ravine cliff."""
from __future__ import annotations

import math
import unittest

from mcagent.client import ApiError
from mcagent.runtime import Executor, Replan, RuntimeConfig, State
from mcagent.skills.escape import _flee_hostile
from mcagent.skills.world_memo import reset_all_world_memos


def _timeout() -> Replan:
    err = Replan("timeout")
    err.__cause__ = ApiError("timeout", "move_to timeout")
    return err


class _RavineEdgeClient:
    """Bot stands at (0.5, 65, 0.5) on solid stone; zombie is at (0.5, 65, -4.5) (north).
    Away direction is +Z (south), which drops into a 20-block ravine at z >= 2.
    East (+X) and West (-X) have solid ground at y=64."""

    def __init__(self):
        self.pos = [0.5, 65.0, 0.5]
        self.controls: list[dict] = []

    def status(self):
        return {
            "pos": list(self.pos),
            "dimension": "overworld",
            "ready": True,
            "alive": True,
            "on_ground": True,
            "health": 6.0,
            "food": 18.0,
            "nearest_hostile": {"id": 1, "name": "zombie", "dist": 5.0, "pos": [0.5, 65.0, -4.5]},
        }

    def inventory(self):
        return {"items": [], "held": None, "armor": {}, "offhand": None}

    def get_block(self, pos):
        x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
        # Ravine drop-off for z >= 2 (air all the way down to y=44)
        if z >= 2:
            return {"name": "stone" if y <= 44 else "air"}
        # Safe solid ground everywhere for z <= 1
        return {"name": "grass_block" if y == 64 else ("stone" if y < 64 else "air")}

    def move_to(self, pos, rng=2, timeout=12.0, **kwargs):
        raise _timeout()

    def control(self, **kwargs):
        self.controls.append(dict(kwargs))
        yaw = float(kwargs.get("yaw", 0.0))
        dx = -math.sin(yaw) * 3.0
        dz = -math.cos(yaw) * 3.0
        self.pos = [self.pos[0] + dx, self.pos[1], self.pos[2] + dz]
        return {"pos": list(self.pos)}


class _DirectExec(Executor):
    def __init__(self, client):
        self.client = client

    def move_to(self, pos, rng=2, timeout=12.0, retries=0, stop_on=None):
        return self.client.move_to(pos, rng=rng, timeout=timeout)

    def control(self, **kwargs):
        return self.client.control(**kwargs)


class TestSoakR08Fixes(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()

    def tearDown(self):
        reset_all_world_memos()

    def test_flee_hostile_does_not_blind_sprint_off_ravine_cliff(self):
        client = _RavineEdgeClient()
        state = State(RuntimeConfig())
        ex = _DirectExec(client)

        gen = _flee_hostile(ex, state)
        try:
            while True:
                next(gen)
        except StopIteration:
            pass

        self.assertTrue(client.controls, "expected fallback control step along a safe lateral direction")
        # Must NOT have sprinted into the ravine (z >= 2.0)
        self.assertLess(client.pos[2], 2.0, f"Bot sprinted off cliff into ravine: pos={client.pos}, controls={client.controls}")


if __name__ == "__main__":
    unittest.main()
