"""Regression tests for Run-04 soak fixes (diamond_site on_ground settle wait, deep mine climb-out steps)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.runtime import Executor, Replan, RuntimeConfig, State
from mcagent.skills import diamond_site
from mcagent.skills.mine_return import return_to_surface_from_mine
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


def _path_not_found() -> Replan:
    err = Replan("path_not_found")
    err.__cause__ = ApiError("path_not_found", "blocked")
    return err


class _SettleClient:
    def __init__(self, statuses: list[dict], blocks: dict[tuple[int, int, int], str]):
        self._statuses = list(statuses)
        self._idx = 0
        self.blocks = dict(blocks)
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        st = self._statuses[min(self._idx, len(self._statuses) - 1)]
        self._idx += 1
        return dict(st)

    def get_block(self, pos):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        return {"name": self.blocks.get(key, "air")}


class _DeepClimbClient:
    def __init__(self, *, start_y: int, target_y: int):
        self.pos = [0.5, float(start_y), 0.5]
        self.target_y = target_y
        self.held = {"name": "iron_pickaxe", "count": 1}
        self.inv_items = [{"name": "iron_pickaxe", "count": 1}]
        self.dug_count = 0

    def status(self):
        return {
            "pos": list(self.pos),
            "dimension": "overworld",
            "ready": True,
            "alive": True,
            "on_ground": True,
            "health": 20.0,
            "food": 20.0,
            "held": self.held,
        }

    def inventory(self):
        return {"items": list(self.inv_items), "held": self.held, "armor": {}, "offhand": None}

    def get_block(self, pos):
        return {"name": "stone"}

    def equip(self, item, dest="hand"):
        self.held = {"name": item, "count": 1}
        return {"ok": True}

    def dig(self, pos, collect=False):
        self.dug_count += 1
        return {"dug": "stone"}

    def move_to(self, pos, rng=1, timeout=30.0, **kwargs):
        # Block anchor jumps (> 3 blocks away), allow 1-block stair climb steps
        if abs(float(pos[1]) - self.pos[1]) > 2.0:
            raise _path_not_found()
        self.pos = [float(pos[0]), float(pos[1]), float(pos[2])]
        return {"pos": list(self.pos)}


class _DirectExec(Executor):
    def __init__(self, client, state: State):
        self.client = client
        self.state = state

    def equip(self, item, dest="hand"):
        res = self.client.equip(item, dest=dest)
        self.state.inventory_dirty = True
        return res

    def dig(self, pos, collect=False):
        return self.client.dig(pos, collect=collect)

    def move_to(self, pos, rng=1, timeout=30.0, retries=0):
        return self.client.move_to(pos, rng=rng, timeout=timeout)


class TestSoakR04Fixes(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()

    def tearDown(self):
        reset_all_world_memos()

    def test_diamond_safe_status_waits_briefly_for_post_move_landing(self):
        base = {
            "dimension": "overworld",
            "ready": True,
            "alive": True,
            "in_water": False,
            "submerged": False,
            "in_lava": False,
            "on_fire": False,
            "in_vehicle": None,
        }
        client = _SettleClient(
            statuses=[
                {**base, "pos": [-139.4, 10.9, 38.5], "on_ground": False},
                {**base, "pos": [-139.4, 10.3, 38.5], "on_ground": False},
                {**base, "pos": [-139.4, 10.0, 38.5], "on_ground": True},
            ],
            blocks={(-140, 9, 38): "stone", (-140, 10, 38): "air", (-140, 11, 38): "air"},
        )
        state = State(RuntimeConfig())
        ex = _DirectExec(client, state)
        with patch.object(diamond_site.time, "sleep", return_value=None):
            st = diamond_site._safe_status(ex, state, dimension="overworld")
        self.assertTrue(st["on_ground"])
        self.assertEqual(client.status_calls, 3)

    def test_return_to_surface_default_climb_reaches_surface_from_deep_mine(self):
        client = _DeepClimbClient(start_y=12, target_y=36)
        state = State(RuntimeConfig())
        ex = _DirectExec(client, state)
        state.refresh_status(client, max_age_s=0)
        state.refresh_inventory(client)
        memo_for(state).set_mine_shaft(entry=[0, 36, 0], dimension="overworld")

        gen = return_to_surface_from_mine(ex, state, target_y=36.0)
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = stop.value

        self.assertTrue(ok)
        self.assertGreaterEqual(client.pos[1], 34.0)


if __name__ == "__main__":
    unittest.main()
