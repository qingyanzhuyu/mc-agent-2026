"""Regression tests for Run-03 soak fixes (wooden pickaxe climb-out, anchor fallback, underground shelter guard)."""
from __future__ import annotations

import unittest

from mcagent.client import ApiError
from mcagent.loop.policy import RulePlanner
from mcagent.runtime import Executor, Replan, RuntimeConfig, State
from mcagent.skills.mine_return import return_to_surface_from_mine
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


def _path_not_found() -> Replan:
    err = Replan("path_not_found")
    err.__cause__ = ApiError("path_not_found", "blocked")
    return err


class _ClimbClient:
    def __init__(self, *, pos: list[float], inv_items: list[dict], blocks: dict[tuple[int, int, int], str],
                 move_cb=None):
        self.pos = list(pos)
        self.inv_items = list(inv_items)
        self.held = None
        self.blocks = dict(blocks)
        self.move_cb = move_cb
        self.dug: list[list[int]] = []
        self.moves: list[list[float]] = []

    def status(self):
        return {
            "pos": list(self.pos),
            "dimension": "overworld",
            "alive": True,
            "health": 20.0,
            "food": 20.0,
            "held": self.held,
            "is_day": True,
            "nearest_hostile": None,
        }

    def inventory(self):
        return {"items": list(self.inv_items), "held": self.held, "armor": {}, "offhand": None}

    def get_block(self, pos):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        return {"name": self.blocks.get(key, "stone")}

    def equip(self, item, dest="hand"):
        self.held = {"name": item, "count": 1}
        return {"ok": True, "item": item}

    def dig(self, pos, collect=False):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        self.dug.append(list(key))
        old = self.blocks.get(key, "stone")
        self.blocks[key] = "air"
        return {"dug": old, "tool": (self.held or {}).get("name", "hand")}

    def move_to(self, pos, rng=1, timeout=30.0, **kwargs):
        self.moves.append(list(pos))
        if self.move_cb is not None:
            return self.move_cb(self, pos)
        self.pos = [float(pos[0]), float(pos[1]), float(pos[2])]
        return {"pos": list(self.pos)}


class _DirectExecutor(Executor):
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


class TestSoakR03Fixes(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()

    def tearDown(self):
        reset_all_world_memos()

    def test_climb_out_stairs_allows_wooden_pickaxe_without_stone_pick_replan(self):
        blocks = {
            (0, 50, 0): "air",
            (0, 51, 0): "air",
            (0, 52, 0): "stone",
            (1, 50, 0): "stone",
            (1, 51, 0): "stone",
            (1, 52, 0): "stone",
            (1, 53, 0): "stone",
        }
        client = _ClimbClient(
            pos=[0.5, 50.0, 0.5],
            inv_items=[{"name": "wooden_pickaxe", "count": 1}],
            blocks=blocks,
        )
        state = State(RuntimeConfig())
        ex = _DirectExecutor(client, state)
        state.refresh_status(client, max_age_s=0)
        state.refresh_inventory(client)
        memo_for(state).set_mine_shaft(entry=[0, 62, 0], dimension="overworld")

        # Anchor route blocked -> must use _climb_out_stairs with wooden_pickaxe
        def _fail_anchor_only(cli, dest):
            if abs(dest[1] - 62.0) < 0.1 and abs(dest[0] - 0.5) < 0.6:
                raise _path_not_found()
            cli.pos = [float(dest[0]), float(dest[1]), float(dest[2])]
            return {"pos": list(cli.pos)}

        client.move_cb = _fail_anchor_only
        gen = return_to_surface_from_mine(ex, state, target_y=55.0, max_climb_steps=3)
        records = []
        try:
            while True:
                records.append(next(gen))
        except StopIteration as stop:
            returned = stop.value

        self.assertTrue(returned)
        self.assertGreaterEqual(client.pos[1], 51.0)
        self.assertTrue(any(r.get("op") == "dig" for r in records))
        self.assertEqual((client.held or {}).get("name"), "wooden_pickaxe")

    def test_return_to_surface_tries_direct_anchor_when_interpolated_midpoint_blocked(self):
        client = _ClimbClient(
            pos=[-14.5, 58.0, 102.5],
            inv_items=[],
            blocks={},
        )
        state = State(RuntimeConfig())
        ex = _DirectExecutor(client, state)
        state.refresh_status(client, max_age_s=0)
        memo_for(state).set_mine_shaft(entry=[-40, 70, 70], dimension="overworld")

        def _only_direct_entry_succeeds(cli, dest):
            if abs(dest[0] - (-39.5)) <= 1.0 and abs(dest[1] - 70.0) <= 1.0 and abs(dest[2] - 70.5) <= 1.0:
                cli.pos = [-39.5, 70.0, 70.5]
                return {"pos": list(cli.pos)}
            raise _path_not_found()

        client.move_cb = _only_direct_entry_succeeds
        gen = return_to_surface_from_mine(ex, state, max_climb_steps=0)
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            returned = stop.value

        self.assertTrue(returned)
        self.assertGreaterEqual(client.pos[1], 69.0)
        self.assertTrue(any(abs(m[1] - 70.0) <= 1.0 for m in client.moves))

    def test_policy_does_not_dispatch_tier1_shelter_when_underground_below_shaft_entry(self):
        state = State(RuntimeConfig())
        state.set_status({
            "pos": [-19.5, 56.0, 102.5],
            "dimension": "overworld",
            "ready": True,
            "alive": True,
            "health": 20.0,
            "food": 20.0,
            "is_day": True,
            "nearest_hostile": None,
        })
        state.set_inventory({
            "items": [
                {"name": "cobblestone", "count": 96, "slot": 0},
                {"name": "stone_pickaxe", "count": 1, "slot": 1},
                {"name": "stone_sword", "count": 1, "slot": 2},
                {"name": "cooked_beef", "count": 8, "slot": 3},
            ],
            "empty_slots": 30,
        })
        memo_for(state).set_mine_shaft(entry=[-40, 70, 70], dimension="overworld")

        decision = RulePlanner(min_verified="offline").choose_proactive_survival(state)
        self.assertTrue(decision is None or decision.skill.name != "build_spawn_shelter")


if __name__ == "__main__":
    unittest.main()
