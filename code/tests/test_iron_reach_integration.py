"""Offline integration tests for SmeltIron reach/LOS/voxel_cache/world_memo fixes."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.skills.iron import SmeltIron, reset_iron_memo
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches, voxel_cache_for
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


def _furnace_view(input_count: int = 0, output: int = 0, fuel: str | None = None) -> dict:
    return {
        "window": "furnace",
        "sections": {
            "input": [{"name": "raw_iron", "count": input_count}] if input_count else [],
            "fuel": [{"name": fuel, "count": 1}] if fuel else [],
            "output": [{"name": "iron_ingot", "count": output}] if output else [],
        },
        "info": {"burning": bool(input_count), "progress": 0.5, "fuel_left": 0.5},
    }


class _ReachIronWorld(GridWorld):
    def __init__(self, pos=(0.5, 64.0, 0.5), items=None, blocks=None, views=None, *, track_held: bool = False):
        ground = {(x, 63, z): "stone" for x in range(-2, 12) for z in range(-2, 3)}
        ground.update(blocks or {})
        super().__init__(
            pos=pos,
            blocks=ground,
            items=items if items is not None else {"stone_pickaxe": 1, "furnace": 1, "coal": 1},
        )
        self.views = list(views or [_furnace_view(), _furnace_view(output=1), _furnace_view()])
        self.last_view = _furnace_view()
        self.track_held = track_held

    def equip(self, item, slot="hand", *, stop_on=None) -> dict:
        res = super().equip(item, slot=slot, stop_on=stop_on)
        if self.track_held and slot == "hand":
            self.st["held"] = {"name": item}
        return res

    def find_blocks(self, types=None, radius=16, limit=20):
        self.calls.append(("find_blocks", {"types": types, "radius": radius}))
        if types is None:
            return {"radius": radius, "blocks": {}}
        return {
            "blocks": [
                {"pos": list(p), "name": n}
                for p, n in self.blocks.items()
                if n in types and sum((a - b) ** 2 for a, b in zip(p, self.st["pos"])) <= radius ** 2
            ][:limit]
        }

    def dig(self, pos, collect=True, *, stop_on=None):
        result = super().dig(pos, collect=collect, stop_on=stop_on)
        name = result["dug"]
        if name in ("iron_ore", "deepslate_iron_ore", "coal_ore", "deepslate_coal_ore"):
            self.items[name] -= 1
            drop = "raw_iron" if "iron" in name else "coal"
            self.items[drop] = self.items.get(drop, 0) + 1
        return result

    def window_view(self, target, *, stop_on=None):
        self.calls.append(("window_view", {"target": target}))
        if self.views:
            self.last_view = self.views.pop(0)
        return copy.deepcopy(self.last_view)

    def window_act(self, target, ops, *, stop_on=None):
        self.calls.append(("window_act", {"target": target, "ops": ops}))
        for op in ops:
            if op["op"] == "put":
                assert self.items.get(op["item"], 0) >= op["count"]
                self.items[op["item"]] -= op["count"]
            elif op["from"] == "output":
                self.items["iron_ingot"] = self.items.get("iron_ingot", 0) + op["count"]
        return {}


class IronReachIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_iron_memo()
        reset_all_voxel_caches()
        reset_all_world_memos()
        self.addCleanup(reset_iron_memo)
        self.addCleanup(reset_all_voxel_caches)
        self.addCleanup(reset_all_world_memos)
        sleep_patch = patch("mcagent.skills.iron.time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_elevation_preference_sorting_and_3d_distance_tiebreak(self) -> None:
        blocks = {
            (1, 45, 0): "iron_ore",   # deep: abs(dy)=19 > 12 (tier 2)
            (1, 54, 0): "iron_ore",   # mid:  abs(dy)=10 <= 12 (tier 1)
            (10, 64, 0): "iron_ore",  # shallow far: abs(dy)=0 <= 8 (tier 0)
            (2, 64, 0): "iron_ore",   # shallow near: abs(dy)=0 <= 8 (tier 0)
            (5, 53, 0): "iron_ore",   # mid far: abs(dy)=11 <= 12 (tier 1)
        }
        world = _ReachIronWorld(blocks=blocks)
        ex, state = world.session()
        cands = SmeltIron()._ore_candidates(ex, state, radius=32)
        self.assertEqual(
            cands,
            [[2, 64, 0], [10, 64, 0], [1, 54, 0], [5, 53, 0], [1, 45, 0]],
        )

    def test_voxel_cache_blacklist_skipping_and_dig_invalidation(self) -> None:
        world = _ReachIronWorld(
            blocks={(2, 64, 0): "iron_ore", (3, 64, 0): "iron_ore"},
            views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
        )
        ex, state = world.session()
        vc = voxel_cache_for(state)
        vc.mark_unreachable("smelt_iron", [2, 64, 0], dimension="overworld", stance_cell=(0, 64, 0))

        steps = drive(SmeltIron(count=1), ex, state)
        digs = [args["pos"] for op, args in world.calls if op == "dig"]
        self.assertEqual(digs, [[3, 64, 0]])
        self.assertFalse(any(s.get("op") == "ore_route_skip" for s in steps))
        self.assertEqual(state.count("iron_ingot"), 1)

        # Dug block [3, 64, 0] was invalidated in voxel_cache so get_block observes air instead of stale ore.
        self.assertEqual(vc.get_block(world, [3, 64, 0], dimension="overworld")["name"], "air")

        # Failing dig in _collect marks the ore unreachable in voxel_cache.
        world2 = _ReachIronWorld(
            blocks={(2, 64, 0): "iron_ore", (3, 64, 0): "iron_ore"},
            views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
        )
        orig_dig = world2.dig
        failed = {"done": False}

        def flaky_dig(pos, collect=True, *, stop_on=None):
            if list(pos) == [2, 64, 0] and not failed["done"]:
                failed["done"] = True
                raise ApiError("dig_unconfirmed", "unconfirmed")
            return orig_dig(pos, collect=collect, stop_on=stop_on)

        world2.dig = flaky_dig
        ex2, state2 = world2.session()
        steps2 = drive(SmeltIron(count=1), ex2, state2)
        self.assertTrue(any(s.get("op") == "ore_route_skip" for s in steps2))
        self.assertTrue(
            voxel_cache_for(state2).is_unreachable(
                "smelt_iron", [2, 64, 0], dimension="overworld", stance_cell=(0, 64, 0),
            )
        )

    def test_obstructed_ore_within_reach_clears_los_and_mines(self) -> None:
        for blocker_name, track_held in (("dirt", False), ("stone", True)):
            with self.subTest(blocker=blocker_name):
                world = _ReachIronWorld(
                    blocks={(1, 65, 0): blocker_name, (2, 65, 0): "iron_ore"},
                    views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
                    track_held=track_held,
                )
                ex, state = world.session()
                steps = drive(SmeltIron(count=1), ex, state)
                digs = [args["pos"] for op, args in world.calls if op == "dig"]
                self.assertEqual(digs, [[1, 65, 0], [2, 65, 0]])
                self.assertFalse(any(op == "move_to" for op, _ in world.calls))
                self.assertTrue(any(s.get("op") == "dig" and s.get("reason") == "clear_los" for s in steps))
                self.assertEqual(state.count("iron_ingot"), 1)

    def test_dry_landing_settle_runs_after_approach_move(self) -> None:
        from mcagent.skills import reach as reach_mod

        world = _ReachIronWorld(
            blocks={(8, 64, 0): "iron_ore"},
            views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
        )
        orig_move = world.move_to
        orig_status = world.status
        airborne_polls = {"left": 0}

        def move_airborne(*args, **kwargs):
            res = orig_move(*args, **kwargs)
            world.st["on_ground"] = False
            airborne_polls["left"] = 3
            return res

        def status_settling(timeout=10):
            if airborne_polls["left"] > 0:
                airborne_polls["left"] -= 1
                if airborne_polls["left"] == 0:
                    world.st["on_ground"] = True
            return orig_status(timeout=timeout)

        world.move_to = move_airborne
        world.status = status_settling
        ex, state = world.session()
        slept: list[float] = []
        orig_settle = reach_mod.settle_dry_landing

        def spy_settle(ex_arg, st_arg):
            return orig_settle(ex_arg, st_arg, sleep_fn=slept.append)

        with patch.object(reach_mod, "settle_dry_landing", side_effect=spy_settle) as settle_mock:
            drive(SmeltIron(count=1), ex, state)
        self.assertEqual(settle_mock.call_count, 1)
        self.assertEqual(slept, [0.12])
        self.assertEqual(state.count("iron_ingot"), 1)

    def test_bounded_move_to_passes_timeout_retries_and_falls_back_on_strict_mock(self) -> None:
        world = _ReachIronWorld(
            blocks={(8, 64, 0): "iron_ore"},
            views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
        )
        ex, state = world.session()
        seen_kwargs: list[dict] = []
        orig_move = ex.move_to

        def spy_move_to(*args, **kwargs):
            seen_kwargs.append(dict(kwargs))
            return orig_move(*args, **kwargs)

        ex.move_to = spy_move_to
        drive(SmeltIron(count=1), ex, state)
        self.assertTrue(seen_kwargs)
        self.assertEqual(seen_kwargs[0].get("timeout"), 30.0)
        self.assertEqual(seen_kwargs[0].get("retries"), 0)
        self.assertEqual(seen_kwargs[0].get("rng"), 2)

        # Strict mock that rejects timeout/retries falls back cleanly.
        world2 = _ReachIronWorld(
            blocks={(8, 64, 0): "iron_ore"},
            views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
        )
        ex2, state2 = world2.session()
        orig_move2 = ex2.move_to
        fallback_calls: list[tuple[list, int]] = []

        def strict_move_to(*, pos=None, rng=1):
            fallback_calls.append((list(pos), rng))
            return orig_move2(pos=pos, rng=rng)

        ex2.move_to = strict_move_to
        drive(SmeltIron(count=1), ex2, state2)
        self.assertEqual(fallback_calls, [([8, 64, 0], 2)])
        self.assertEqual(state2.count("iron_ingot"), 1)

    def test_record_station_called_when_reusing_placing_or_recovering_furnace(self) -> None:
        with self.subTest("reuse_furnace"):
            world = _ReachIronWorld(
                items={"raw_iron": 1, "coal": 1},
                blocks={(2, 64, 0): "furnace"},
                views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
            )
            ex, state = world.session()
            recorded: list[tuple[str, list, str]] = []
            memo = memo_for(state)
            memo.record_station = lambda kind, pos, dimension="overworld": recorded.append((kind, list(pos), dimension))
            drive(SmeltIron(count=1), ex, state)
            self.assertEqual(recorded, [("furnace", [2, 64, 0], "overworld")])

        with self.subTest("place_furnace"):
            world = _ReachIronWorld(
                items={"raw_iron": 1, "coal": 1, "furnace": 1},
                views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
            )
            ex, state = world.session()
            recorded = []
            memo = memo_for(state)
            memo.record_station = lambda kind, pos, dimension="overworld": recorded.append((kind, list(pos), dimension))
            drive(SmeltIron(count=1), ex, state)
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0][0], "furnace")
            self.assertEqual(recorded[0][2], "overworld")

        with self.subTest("recover_placed_furnace"):
            world = _ReachIronWorld(
                items={"stone_pickaxe": 1},
                blocks={(2, 64, 0): "furnace"},
                views=[_furnace_view(output=1)],
            )
            ex, state = world.session()
            recorded = []
            memo = memo_for(state)
            memo.record_station = lambda kind, pos, dimension="overworld": recorded.append((kind, list(pos), dimension))
            drive(SmeltIron(count=1), ex, state)
            self.assertEqual(recorded, [("furnace", [2, 64, 0], "overworld")])


if __name__ == "__main__":
    unittest.main()
