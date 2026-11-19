"""reach.py L2 原语离线契约单测（verified: offline）。"""
from __future__ import annotations

import unittest
from typing import Any

from mcagent.client import ApiError
from mcagent.runtime import Replan
from mcagent.skills.reach import (
    clear_line_of_sight,
    reach_and_act,
    select_column_base,
    settle_dry_landing,
)
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches, voxel_cache_for


def _drive_ret(gen, ex):
    """用 testing.drive() 驱动生成器（强校验一步一动作），并捕获 StopIteration 返回值。"""
    box: dict[str, Any] = {}

    def _wrap():
        box["value"] = yield from gen

    steps = drive(_wrap(), ex)
    return steps, box.get("value")


class _BlockReadCountingWorld(GridWorld):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.block_reads: list[tuple[int, int, int]] = []

    def get_block(self, pos):
        self.block_reads.append(tuple(pos))
        return super().get_block(pos)


class SelectColumnBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_voxel_caches()

    def tearDown(self) -> None:
        reset_all_voxel_caches()

    def test_canopy_oak_log_descends_to_base_using_voxel_cache(self):
        blocks = {(5, y, 5): "oak_log" for y in range(64, 72)}
        blocks[(5, 63, 5)] = "dirt"
        world = _BlockReadCountingWorld(pos=(0.5, 64.0, 0.5), blocks=blocks)
        ex, state = world.session()

        cache = voxel_cache_for(state)
        cache.ingest_find_blocks(
            {"blocks": [{"name": "minecraft:oak_log", "pos": [5, y, 5]} for y in range(64, 72)]},
            client=world,
        )

        base = select_column_base(ex.client, state, [5, 71, 5])
        self.assertEqual(base, [5, 64, 5])
        # 64..71 全部命中 voxel_cache，仅有终止格 (5, 63, 5) 触发一次 get_block
        self.assertEqual(world.block_reads, [(5, 63, 5)])

    def test_stops_at_air_gap_and_respects_max_drop(self):
        blocks = {
            (2, 71, 2): "oak_log",
            (2, 70, 2): "oak_log",
            (2, 69, 2): "oak_log",
            # y=68 为 air 断层
            (2, 67, 2): "oak_log",
        }
        world = GridWorld(pos=(0.5, 64.0, 0.5), blocks=blocks)
        ex, state = world.session()

        self.assertEqual(select_column_base(ex.client, state, [2, 71, 2]), [2, 69, 2])

        full_col = {(3, y, 3): "oak_log" for y in range(62, 72)}
        world2 = GridWorld(pos=(0.5, 64.0, 0.5), blocks=full_col)
        ex2, state2 = world2.session()
        self.assertEqual(
            select_column_base(ex2.client, state2, [3, 71, 3], match_names="minecraft:oak_log", max_drop=3),
            [3, 68, 3],
        )

    def test_stops_safely_on_unloaded_chunk(self):
        class UnloadedWorld(GridWorld):
            def get_block(self, pos):
                if tuple(pos) == (1, 69, 1):
                    raise ApiError("not_loaded", "chunk not loaded")
                return super().get_block(pos)

        world = UnloadedWorld(blocks={(1, 71, 1): "oak_log", (1, 70, 1): "oak_log"})
        ex, state = world.session()
        self.assertEqual(select_column_base(ex.client, state, [1, 71, 1], match_names=["oak_log"]), [1, 70, 1])


class SettleDryLandingTests(unittest.TestCase):
    def test_transient_airborne_settles_after_two_polls(self):
        world = GridWorld(pos=(0.5, 64.0, 0.5))
        world.st["on_ground"] = False
        polls = {"n": 0}
        orig_status = world.status

        def flaky_status(timeout=10):
            polls["n"] += 1
            if polls["n"] >= 3:
                world.st["on_ground"] = True
            return orig_status(timeout=timeout)

        world.status = flaky_status
        ex, state = world.session()
        polls["n"] = 0
        world.st["on_ground"] = False

        slept: list[float] = []
        st = settle_dry_landing(ex, state, delays=(0.12, 0.16, 0.22), sleep_fn=slept.append)
        self.assertTrue(st["on_ground"])
        self.assertEqual(slept, [0.12, 0.16])

    def test_already_grounded_or_wet_or_hazardous_skips_sleeping(self):
        for overrides in (
            {"on_ground": True},
            {"on_ground": False, "in_water": True},
            {"on_ground": False, "in_lava": True},
            {"on_ground": False, "on_fire": True},
            {"on_ground": False, "in_vehicle": True},
            {"on_ground": False, "alive": False},
        ):
            with self.subTest(overrides=overrides):
                world = GridWorld()
                world.st.update(overrides)
                ex, state = world.session()
                slept: list[float] = []
                settle_dry_landing(ex, state, sleep_fn=slept.append)
                self.assertEqual(slept, [])


class ClearLineOfSightTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_voxel_caches()

    def tearDown(self) -> None:
        reset_all_voxel_caches()

    def test_already_visible_takes_zero_actions(self):
        world = GridWorld(pos=(0.5, 64.0, 0.5), blocks={(2, 65, 0): "iron_ore"})
        ex, state = world.session()
        steps, ok = _drive_ret(clear_line_of_sight(ex, state, [2, 65, 0]), ex)
        self.assertTrue(ok)
        self.assertEqual(steps, [])
        self.assertEqual(ex.acts, [])

    def test_clears_leaves_and_dirt_and_invalidates_cache(self):
        world = GridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (1, 65, 0): "oak_leaves",
                (2, 65, 0): "dirt",
                (3, 65, 0): "iron_ore",
            },
        )
        ex, state = world.session()
        cache = voxel_cache_for(state)
        cache.ingest_find_blocks(
            {"blocks": [{"name": "oak_leaves", "pos": [1, 65, 0]}, {"name": "dirt", "pos": [2, 65, 0]}]},
            client=world,
        )

        steps, ok = _drive_ret(clear_line_of_sight(ex, state, [3, 65, 0], max_clear=2), ex)
        self.assertTrue(ok)
        self.assertEqual(ex.acts, ["dig", "dig"])
        self.assertEqual([s["pos"] for s in steps], [[1, 65, 0], [2, 65, 0]])
        self.assertTrue(all(s["reason"] == "clear_los" for s in steps))
        self.assertEqual(cache.get_block(world, [1, 65, 0])["name"], "air")

    def test_stone_requires_held_pickaxe(self):
        # 背包里有镐但未手持 -> 拒绝挖石头
        world = GridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(1, 65, 0): "stone", (2, 65, 0): "iron_ore"},
            items={"iron_pickaxe": 1},
        )
        ex, state = world.session()
        steps, ok = _drive_ret(clear_line_of_sight(ex, state, [2, 65, 0]), ex)
        self.assertFalse(ok)
        self.assertEqual(steps, [])
        self.assertIn((1, 65, 0), world.blocks)

        # 手持镐 (status.held) -> 允许挖石头
        world.st["held"] = {"name": "minecraft:iron_pickaxe"}
        steps2, ok2 = _drive_ret(clear_line_of_sight(ex, state, [2, 65, 0]), ex)
        self.assertTrue(ok2)
        self.assertEqual(len(steps2), 1)
        self.assertEqual(steps2[0]["pos"], [1, 65, 0])

        # 通过 refreshed inv["held"] 也应识别
        world3 = GridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(1, 65, 0): "deepslate", (2, 65, 0): "diamond_ore"},
        )
        world3.inventory = lambda timeout=10: {"items": [], "held": {"name": "diamond_pickaxe"}}
        ex3, state3 = world3.session()
        state3.invalidate_inventory()
        steps3, ok3 = _drive_ret(clear_line_of_sight(ex3, state3, [2, 65, 0]), ex3)
        self.assertTrue(ok3)
        self.assertEqual(len(steps3), 1)

    def test_refuses_unsafe_protected_and_underfoot_blocks(self):
        # 1) 脚下正下方格（如向下看脚底方块的视线阻挡）禁止清除
        world_feet = GridWorld(
            pos=(0.5, 66.0, 0.5),
            blocks={(0, 65, 0): "dirt", (0, 64, 0): "iron_ore"},
        )
        ex_f, st_f = world_feet.session()
        steps_f, ok_f = _drive_ret(clear_line_of_sight(ex_f, st_f, [0, 64, 0]), ex_f)
        self.assertFalse(ok_f)
        self.assertEqual(steps_f, [])

        # 2) 流体 / 基岩 / 功能站 / 容器 / 上方岩浆 均禁止清除
        for bad_block, extra in (
            ("water", {}),
            ("lava", {}),
            ("bedrock", {}),
            ("crafting_table", {}),
            ("furnace", {}),
            ("chest", {}),
            ("red_bed", {}),
            ("cyan_shulker_box", {}),
            ("dirt", {(1, 66, 0): "lava"}),
        ):
            with self.subTest(bad_block=bad_block, extra=extra):
                blocks = {(1, 65, 0): bad_block, (2, 65, 0): "iron_ore", **extra}
                w = GridWorld(pos=(0.5, 64.0, 0.5), blocks=blocks)
                w.st["held"] = {"name": "diamond_pickaxe"}
                ex, state = w.session()
                steps, ok = _drive_ret(clear_line_of_sight(ex, state, [2, 65, 0]), ex)
                self.assertFalse(ok)
                self.assertEqual(steps, [])

        # 3) protected_fn 拒绝
        w_prot = GridWorld(pos=(0.5, 64.0, 0.5), blocks={(1, 65, 0): "dirt", (2, 65, 0): "iron_ore"})
        ex_p, st_p = w_prot.session()
        steps_p, ok_p = _drive_ret(
            clear_line_of_sight(ex_p, st_p, [2, 65, 0], protected_fn=lambda pos: list(pos) == [1, 65, 0]),
            ex_p,
        )
        self.assertFalse(ok_p)
        self.assertEqual(steps_p, [])

        # 4) 超出 reach 的遮挡格与 max_clear=1 截断
        w_far = GridWorld(pos=(0.5, 64.0, 0.5), blocks={(4, 65, 0): "dirt", (5, 65, 0): "iron_ore"})
        ex_far, st_far = w_far.session()
        steps_far, ok_far = _drive_ret(clear_line_of_sight(ex_far, st_far, [5, 65, 0], reach=3.0), ex_far)
        self.assertFalse(ok_far)
        self.assertEqual(steps_far, [])

        w_cap = GridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(1, 65, 0): "dirt", (2, 65, 0): "dirt", (3, 65, 0): "iron_ore"},
        )
        ex_cap, st_cap = w_cap.session()
        steps_cap, ok_cap = _drive_ret(clear_line_of_sight(ex_cap, st_cap, [3, 65, 0], max_clear=1), ex_cap)
        self.assertFalse(ok_cap)
        self.assertEqual([s["pos"] for s in steps_cap], [[1, 65, 0]])

    def test_dig_replan_yields_error_and_preemption_propagates(self):
        class FailingDigWorld(GridWorld):
            def dig(self, pos, collect=True, *, stop_on=None):
                raise ApiError("dig_failed", "cannot break")

        w = FailingDigWorld(pos=(0.5, 64.0, 0.5), blocks={(1, 65, 0): "dirt", (2, 65, 0): "iron_ore"})
        ex, state = w.session()
        steps, ok = _drive_ret(clear_line_of_sight(ex, state, [2, 65, 0]), ex)
        self.assertFalse(ok)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["error_code"], "dig_failed")

        class PreemptDigWorld(GridWorld):
            def dig(self, pos, collect=True, *, stop_on=None):
                raise ApiError("interrupted", "抢占")

        w2 = PreemptDigWorld(pos=(0.5, 64.0, 0.5), blocks={(1, 65, 0): "dirt", (2, 65, 0): "iron_ore"})
        ex2, state2 = w2.session()
        with self.assertRaises(Replan):
            _drive_ret(clear_line_of_sight(ex2, state2, [2, 65, 0]), ex2)


class ReachAndActTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_voxel_caches()

    def tearDown(self) -> None:
        reset_all_voxel_caches()

    def test_already_reachable_skips_move_to(self):
        world = GridWorld(pos=(0.5, 64.0, 0.5), blocks={(2, 65, 0): "oak_log"})
        ex, state = world.session()
        steps, resolved = _drive_ret(reach_and_act(ex, state, [2, 65, 0], skill_name="chop"), ex)
        self.assertEqual(resolved, [2, 65, 0])
        self.assertEqual(steps, [])
        self.assertEqual(ex.acts, [])

    def test_allow_column_base_resolves_canopy_and_approaches(self):
        class StandBesideWorld(GridWorld):
            def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
                self.calls.append(("move_to", {"pos": pos, "rng": rng}))
                # 模拟 move_to(rng=2) 停在树干旁空地，而非钻进实心树干里
                self.st["pos"] = [pos[0] - 1.5, float(pos[1]), pos[2] + 0.5]
                return {"pos": list(self.st["pos"])}

        blocks = {(10, y, 0): "oak_log" for y in range(64, 72)}
        blocks[(10, 63, 0)] = "dirt"
        world = StandBesideWorld(pos=(0.5, 64.0, 0.5), blocks=blocks)
        ex, state = world.session()

        steps, resolved = _drive_ret(
            reach_and_act(
                ex, state, [10, 71, 0],
                skill_name="chop",
                allow_column_base=True,
                match_names="oak_log",
                move_rng=2,
            ),
            ex,
        )
        self.assertEqual(resolved, [10, 64, 0])
        self.assertEqual(ex.acts, ["move_to"])
        self.assertEqual(steps[0]["pos"], [10, 64, 0])

    def test_move_to_then_clear_los_succeeds_one_action_per_yield(self):
        class ApproachShortWorld(GridWorld):
            def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
                self.calls.append(("move_to", {"pos": pos, "rng": rng}))
                # 停在 [7.5, 64.0, 0.5]，眼前 (8, 65, 0) 有树叶遮挡 (9, 65, 0)
                self.st["pos"] = [7.5, 64.0, 0.5]
                return {"pos": list(self.st["pos"])}

        world = ApproachShortWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(8, 65, 0): "oak_leaves", (9, 65, 0): "iron_ore"},
        )
        ex, state = world.session()
        steps, resolved = _drive_ret(reach_and_act(ex, state, [9, 65, 0], skill_name="mine"), ex)
        self.assertEqual(resolved, [9, 65, 0])
        self.assertEqual(ex.acts, ["move_to", "dig"])
        self.assertEqual([s["op"] for s in steps], ["move_to", "dig"])

    def test_path_not_found_within_reach_falls_through_to_clear_los(self):
        class NearBlockedWorld(GridWorld):
            def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
                raise ApiError("path_not_found", "buried target")

        world = NearBlockedWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(1, 65, 0): "dirt", (2, 65, 0): "iron_ore"},
        )
        ex, state = world.session()
        steps, resolved = _drive_ret(reach_and_act(ex, state, [2, 65, 0], skill_name="mine"), ex)
        self.assertEqual(resolved, [2, 65, 0])
        self.assertEqual([s["op"] for s in steps], ["move_to", "dig"])
        self.assertEqual(steps[0]["error_code"], "path_not_found")

    def test_settle_dry_flag_waits_for_grounding(self):
        class AirborneMoveWorld(GridWorld):
            def __init__(self):
                super().__init__(pos=(0.5, 64.0, 0.5), blocks={(9, 65, 0): "iron_ore"})
                self.polls_after_move = 0

            def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
                self.st["pos"] = [7.5, 64.0, 0.5]
                self.st["on_ground"] = False
                return {"pos": list(self.st["pos"])}

            def status(self, timeout=10):
                if not self.st.get("on_ground", True):
                    self.polls_after_move += 1
                    if self.polls_after_move >= 2:
                        self.st["on_ground"] = True
                return super().status(timeout=timeout)

        world = AirborneMoveWorld()
        ex, state = world.session()
        _, resolved = _drive_ret(reach_and_act(ex, state, [9, 65, 0], settle_dry=True), ex)
        self.assertEqual(resolved, [9, 65, 0])
        self.assertTrue(state.st["on_ground"])

    def test_stalled_invokes_recover_move_once(self):
        class StalledWorld(GridWorld):
            def __init__(self):
                super().__init__(
                    pos=(0.5, 64.0, 0.5),
                    blocks={
                        (1, 63, 0): "stone",
                        (1, 64, 0): "air",
                        (1, 65, 0): "air",
                        (3, 65, 0): "iron_ore",
                    },
                )
                self.move_count = 0

            def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
                self.move_count += 1
                if self.move_count == 1:
                    raise ApiError("stalled", "stuck")
                return super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)

        world = StalledWorld()
        ex, state = world.session()
        steps, resolved = _drive_ret(
            reach_and_act(ex, state, [3, 65, 0], skill_name="mine", reach=2.5),
            ex,
        )
        self.assertEqual(resolved, [3, 65, 0])
        self.assertEqual(steps[0]["error_code"], "stalled")
        self.assertEqual(steps[1]["op"], "move_to_recover")
        self.assertEqual(world.move_count, 2)

    def test_unreachable_marks_blacklist_and_replan_until_moved(self):
        class UnreachableWorld(GridWorld):
            def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
                raise ApiError("path_not_found", "no path")

        world = UnreachableWorld(pos=(0.5, 64.0, 0.5), blocks={(20, 65, 0): "iron_ore"})
        ex, state = world.session()

        with self.assertRaises(Replan):
            _drive_ret(reach_and_act(ex, state, [20, 65, 0], skill_name="mine"), ex)
        self.assertEqual(ex.acts, ["move_to"])

        # 同一站位再次调用：命中黑名单直接 Replan，不发任何动作
        ex.acts.clear()
        with self.assertRaises(Replan):
            _drive_ret(reach_and_act(ex, state, [20, 65, 0], skill_name="mine"), ex)
        self.assertEqual(ex.acts, [])

        # 移动 >= 6 格后，站位黑名单被自动清空，可重新尝试 move_to
        world.st["pos"] = [8.5, 64.0, 0.5]
        with self.assertRaises(Replan):
            _drive_ret(reach_and_act(ex, state, [20, 65, 0], skill_name="mine"), ex)
        self.assertEqual(ex.acts, ["move_to"])

    def test_preemption_propagates_immediately(self):
        class PreemptWorld(GridWorld):
            def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
                raise ApiError("interrupted", "动作被安全层抢占")

        world = PreemptWorld(pos=(0.5, 64.0, 0.5), blocks={(15, 65, 0): "iron_ore"})
        ex, state = world.session()
        with self.assertRaises(Replan):
            _drive_ret(reach_and_act(ex, state, [15, 65, 0], skill_name="mine"), ex)
        # 抢占不得写入不可达黑名单
        self.assertFalse(voxel_cache_for(state).is_unreachable("mine", [15, 65, 0]))


class FieldAndMiningIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_voxel_caches()

    def tearDown(self) -> None:
        reset_all_voxel_caches()

    def test_sort_ore_candidates_prioritizes_abs_dy_le_8(self):
        from mcagent.skills.mining import sort_ore_candidates

        origin = [0.5, 64.0, 0.5]
        coords = [[1, 76, 0], [10, 69, 0], [4, 64, 0], [2, 50, 0]]
        ordered = sort_ore_candidates(origin, coords, max_dy=8)
        self.assertEqual(ordered, [[4, 64, 0], [10, 69, 0], [1, 76, 0], [2, 50, 0]])

        dicts = [{"pos": [1, 76, 0], "name": "iron_ore"}, {"pos": [6, 67, 0], "name": "iron_ore"}]
        ordered_dicts = sort_ore_candidates(origin, dicts, max_dy=8)
        self.assertEqual([d["pos"] for d in ordered_dicts], [[6, 67, 0], [1, 76, 0]])

    def test_reach_ore_delegates_to_reach_and_act_with_los_clear(self):
        from mcagent.skills.mining import reach_ore

        world = GridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(1, 65, 0): "dirt", (2, 65, 0): "iron_ore"},
        )
        ex, state = world.session()

        orig_move = world.move_to

        def stay_put(*args, **kwargs):
            orig_move(*args, **kwargs)
            world.st["pos"] = [0.5, 64.0, 0.5]
            return {"pos": [0.5, 64.0, 0.5]}

        world.move_to = stay_put
        steps, resolved = _drive_ret(reach_ore(ex, state, [2, 65, 0]), ex)
        self.assertEqual(resolved, [2, 65, 0])
        self.assertEqual([s["op"] for s in steps], ["move_to", "dig"])

    def test_field_scan_blocks_ingests_voxel_cache_and_skips_blacklisted(self):
        from mcagent.skills.field import scan_blocks
        from mcagent.skills.scripted_world import ScriptedWorld

        world = ScriptedWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(2, 64, 0): "iron_ore", (3, 64, 0): "iron_ore"},
        )
        world.st["dimension"] = "overworld"
        world.queue(
            "find_blocks",
            {"blocks": [{"name": "iron_ore", "pos": [2, 64, 0]}, {"name": "iron_ore", "pos": [3, 64, 0]}]},
        )
        ex, state = world.session()
        cache = voxel_cache_for(state)
        cache.mark_unreachable("mine_iron", [2, 64, 0], dimension="overworld", stance_cell=(0, 64, 0))

        res = scan_blocks(ex, state, "iron_ore", radius=8, skill_name="mine_iron")
        self.assertEqual(res["status"], "found")
        self.assertEqual([b["pos"] for b in res["blocks"]], [[3, 64, 0]])
        self.assertGreaterEqual(cache.stats()["ingested"], 2)


if __name__ == "__main__":
    unittest.main()
