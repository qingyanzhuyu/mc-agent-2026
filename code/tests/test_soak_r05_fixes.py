"""Regression tests for Run-05 soak fixes (water escape shore-fail cap & iron shaft dry-ground guard)."""
from __future__ import annotations

import unittest

from mcagent.client import ApiError
from mcagent.runtime import EnvBlocked, Executor, Replan, RuntimeConfig, State
from mcagent.skills.escape import escape_water_to_shore
from mcagent.skills.iron import SmeltIron
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.world_memo import reset_all_world_memos


def _stalled() -> Replan:
    err = Replan("stalled")
    err.__cause__ = ApiError("stalled", "Stuck for 8s")
    return err


class _WaterTrapClient:
    def __init__(self):
        self.pos = [0.5, 62.2, 0.5]
        self.in_water = True
        self.move_calls = 0
        self.control_calls = 0
        self.place_calls = 0

    def status(self):
        return {
            "pos": list(self.pos),
            "dimension": "overworld",
            "ready": True,
            "alive": True,
            "in_water": self.in_water,
            "submerged": False,
            "on_ground": not self.in_water,
            "oxygen": 20,
            "health": 20.0,
            "food": 20.0,
        }

    def inventory(self):
        return {
            "items": [{"name": "cobblestone", "count": 8, "slot": 0}],
            "held": None,
            "armor": {},
            "offhand": None,
        }

    def get_block(self, pos):
        x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
        if (x, z) == (0, 0) and y in (61, 62):
            return {"name": "water"}
        if (x, z) == (0, 0) and y >= 63:
            return {"name": "air"}
        # Surrounding dry bank at y=63 (many standable shore cells)
        if y <= 62:
            return {"name": "stone"}
        return {"name": "air"}

    def move_to(self, pos, rng=1, timeout=15.0, **kwargs):
        self.move_calls += 1
        raise _stalled()

    def place(self, item, pos=None):
        self.place_calls += 1
        return {"placed": item, "pos": pos}

    def control(self, **kwargs):
        self.control_calls += 1
        self.in_water = False
        self.pos = [1.5, 63.0, 0.5]
        return {"pos": list(self.pos)}


class _DirectExec(Executor):
    def __init__(self, client):
        self.client = client

    def move_to(self, pos, rng=1, timeout=15.0, retries=0, stop_on=None):
        return self.client.move_to(pos, rng=rng, timeout=timeout)

    def place(self, item, pos=None):
        return self.client.place(item, pos=pos)

    def control(self, **kwargs):
        return self.client.control(**kwargs)


class _WetIronWorld(GridWorld):
    def __init__(self):
        super().__init__(
            pos=[0.5, 62.0, 0.5],
            blocks={
                (0, 61, 0): "water",
                (0, 62, 0): "water",
                (1, 60, 0): "stone",
                (1, 61, 0): "stone",
                (1, 62, 0): "stone",
                (1, 63, 0): "stone",
                (1, 64, 0): "stone",
            },
            items={"stone_pickaxe": 1, "furnace": 1, "coal": 4},
        )
        self.st.update(dimension="overworld", ready=True, alive=True, in_water=True, submerged=False, on_ground=False)

    def find_blocks(self, types=None, radius=32, limit=32, **kwargs):
        return {"blocks": []}


class TestSoakR05Fixes(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()

    def tearDown(self):
        reset_all_world_memos()

    def test_escape_water_stops_spamming_stalled_shore_moves_and_swims_out(self):
        client = _WaterTrapClient()
        state = State(RuntimeConfig())
        ex = _DirectExec(client)

        gen = escape_water_to_shore(ex, state, max_steps=10)
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            escaped = stop.value

        self.assertTrue(escaped)
        self.assertLessEqual(client.move_calls, 2)
        self.assertGreaterEqual(client.control_calls + client.place_calls, 1)

    def test_smelt_iron_refuses_to_dig_shaft_while_in_water(self):
        w = _WetIronWorld()
        ex, st = w.session()
        with self.assertRaises(EnvBlocked) as ctx:
            drive(SmeltIron(count=1), ex, st)
        self.assertEqual(ctx.exception.missing_predicate, "dry_ground")


if __name__ == "__main__":
    unittest.main()
