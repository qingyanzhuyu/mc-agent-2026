"""starter_skills 与 voxel_cache / reach / escape / world_memo 集成测试（verified: offline）。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.loop import reset_world_memo
from mcagent.loop.starter_skills import RECENT_RECOVERY_LANDINGS, VISITED_ANCHORS
from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills import escape as escape_mod
from mcagent.skills import make
from mcagent.skills import nav as nav_mod
from mcagent.skills.base import _COOLDOWN, rec
from mcagent.skills.testing import drive
from mcagent.skills.voxel_cache import voxel_cache_for
from mcagent.skills.world_memo import memo_for
from tests.test_loop_soak import RichGridWorld


class IntegrationWorld(RichGridWorld):
    """支持追踪 get_block/move_to kwargs 与注入导航故障的离线夹具。"""

    def __init__(self, *, pos=(0.5, 64.0, 0.5), blocks=None, items=None, move_failures=None):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.move_failures = {tuple(k): v for k, v in (move_failures or {}).items()}
        self.get_block_calls: list[tuple[int, int, int]] = []
        self.move_kwargs: list[dict] = []

    def get_block(self, pos):
        self.get_block_calls.append((int(pos[0]), int(pos[1]), int(pos[2])))
        return super().get_block(pos)

    def move_to(self, *args, **kwargs):
        self.move_kwargs.append(dict(kwargs))
        pos = kwargs.get("pos")
        key = tuple(pos) if pos is not None else None
        if key in self.move_failures:
            self.calls.append(("move_to", {"pos": list(pos)}))
            raise ApiError(self.move_failures[key], "injected navigation failure")
        return super().move_to(*args, **kwargs)


class GatherWoodReachIntegrationTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_trunk_base_resolution_walks_to_base_and_skips_floating_canopy(self):
        # 仅高处树冠 (6, 68, 0) 被 find_blocks 返回时（或整柱都在时），应下探并走向树基 [6, 64, 0]；
        # 悬空树冠 (0, 71, 0) 下方无原木，仍被跳过。
        blocks = {
            (0, 71, 0): "oak_log",
            (6, 64, 0): "oak_log",
            (6, 65, 0): "oak_log",
            (6, 66, 0): "oak_log",
            (6, 67, 0): "oak_log",
            (6, 68, 0): "oak_log",
        }
        world = IntegrationWorld(blocks=blocks, move_failures={(6, 68, 0): "path_not_found"})

        orig_find = world.find_blocks

        def canopy_first_find(types=None, radius=16, limit=20, **kw):
            res = orig_find(types=types, radius=radius, limit=limit, **kw)
            if types and "oak_log" in types:
                res["blocks"] = [
                    {"name": "oak_log", "pos": [0, 71, 0], "dist": 7.0},
                    {"name": "oak_log", "pos": [6, 68, 0], "dist": 6.8},
                    {"name": "oak_log", "pos": [6, 67, 0], "dist": 6.4},
                ]
            return res

        world.find_blocks = canopy_first_find
        ex, state = world.session()
        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "dig"])
        self.assertEqual(steps[0]["pos"], [6, 64, 0])
        self.assertEqual(steps[1]["pos"], [6, 64, 0])
        self.assertEqual(state.count("oak_log"), 1)

    def test_clears_overhanging_leaves_blocking_sightline_and_uses_voxel_cache(self):
        # [3, 65, 0] 眼距 <= 4.5，被 (2, 65, 0) 树叶遮挡：应清理树叶后直接砍木，不抛 EnvBlocked
        world = IntegrationWorld(
            blocks={(2, 65, 0): "oak_leaves", (3, 65, 0): "oak_log"},
            move_failures={(3, 65, 0): "path_not_found"},
        )
        ex, state = world.session()
        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["dig", "dig"])
        self.assertEqual(steps[0]["pos"], [2, 65, 0])
        self.assertEqual(steps[1]["pos"], [3, 65, 0])
        self.assertEqual(state.count("oak_log"), 1)
        vc = voxel_cache_for(state)
        self.assertGreater(vc.stats()["ingested"], 0)

        # 不可达条目写入 voxel_cache 后，同站位重入应跳过而不重复 move_to
        reset_world_memo()
        _COOLDOWN.clear()
        world2 = IntegrationWorld(
            blocks={(8, 64, 0): "oak_log"},
            move_failures={(8, 64, 0): "path_not_found"},
        )
        ex2, state2 = world2.session()
        with self.assertRaises(Replan):
            drive(make("gather_wood", {"count": 1}), ex2, state2)
        vc2 = voxel_cache_for(state2)
        self.assertTrue(vc2.is_unreachable("gather_wood", (8, 64, 0), dimension="overworld", stance_cell=(0, 64, 0)))

        # 清空 world_memo 但保留 voxel_cache 验证 vc 黑名单独立生效
        memo_for(state2).clear()
        ex2.acts.clear()
        with self.assertRaises(EnvBlocked):
            drive(make("gather_wood", {"count": 1}), ex2, state2)
        self.assertEqual(ex2.acts, [])


class ScoutIslandReachIntegrationTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_water_escape_invokes_escape_module_when_stuck_in_water(self):
        world = IntegrationWorld(blocks={(0, 63, 0): "water", (0, 64, 0): "water"})
        world.st["in_water"] = True
        ex, state = world.session()
        called = 0

        def fake_escape(ex_in, state_in):
            nonlocal called
            called += 1
            pos = [3.5, 64.0, 0.5]
            yield rec("move_to", ex_in.move_to(pos=pos, rng=1), pos=pos)
            world.st["in_water"] = False
            return True

        with patch.object(escape_mod, "escape_water_to_shore", side_effect=fake_escape):
            steps = drive(make("scout_island", {"radius": 16}), ex, state)
        self.assertEqual(called, 1)
        self.assertEqual(steps[0]["pos"], [3.5, 64.0, 0.5])
        self.assertEqual(VISITED_ANCHORS, [(3.5, 0.5)])

    def test_records_structure_fingerprints_as_landmarks(self):
        world = IntegrationWorld(
            blocks={(10, 63, 0): "grass_block", (4, 64, 2): "hay_block", (5, 64, 2): "bell"}
        )
        ex, state = world.session()
        drive(make("scout_island", {"radius": 16}), ex, state)
        memo = memo_for(state)
        self.assertTrue(memo.has_landmark("village"))
        self.assertEqual(memo.landmarks("village"), [(4, 64, 2)])

    def test_pocket_loop_guard_replans_after_repeated_nearby_recoveries(self):
        def make_recover(landing):
            def _rec(ex_in, state_in, dest=None, *, protected_fn=None):
                yield rec("move_to_recover", ex_in.move_to(pos=landing, rng=1), pos=landing)
            return _rec

        for idx, landing in enumerate(([2.5, 64.0, 0.5], [3.0, 64.5, 1.0], [2.8, 64.0, 0.8])):
            world = IntegrationWorld(pos=(0.5, 64.0, 0.5), blocks={})
            ex, state = world.session()
            with patch.object(nav_mod, "recover_move", side_effect=make_recover(landing)):
                if idx < 2:
                    drive(make("scout_island", {"radius": 16}), ex, state)
                else:
                    with self.assertRaisesRegex(Replan, "反复打转"):
                        drive(make("scout_island", {"radius": 16}), ex, state)
        self.assertEqual(len(RECENT_RECOVERY_LANDINGS), 2)


class CraftGearAndMineStoneIntegrationTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_craft_gear_records_reused_and_placed_table_and_handles_strict_mock_move(self):
        # 1) 复用远处工作台 + strict mock move_to（不接受 timeout/retries 时回退）
        world = IntegrationWorld(
            blocks={(8, 64, 0): "crafting_table"},
            items={"stick": 2, "cobblestone": 3},
        )
        ex, state = world.session()
        orig_move = ex.move_to

        def strict_move_to(*, pos=None, rng=1):
            return orig_move(pos=pos, rng=rng)

        ex.move_to = strict_move_to
        drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
        self.assertIn((8, 64, 0), memo_for(state).stations("crafting_table"))

        # 2) 新放野外工作台先入账、合成完成后自动回收并销账；基地 camp 范围内的工作台则保留在台账中
        reset_world_memo()
        _COOLDOWN.clear()
        world2 = IntegrationWorld(items={"oak_planks": 4, "stick": 2, "cobblestone": 3})
        ex2, state2 = world2.session()
        steps2 = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex2, state2)
        self.assertEqual(steps2[-1].get("reason"), "reclaim_station")
        self.assertEqual(memo_for(state2).stations("crafting_table"), [])
        self.assertEqual(state2.count("crafting_table"), 1)

    def test_mine_stone_uses_voxel_cache_and_clears_soft_los_block(self):
        # (2, 65, 0) 的 dirt 遮挡 (3, 65, 0) 的 stone；且存在多个仅触发一次 get_block 的探槽观察
        world = IntegrationWorld(
            blocks={(2, 65, 0): "dirt", (3, 65, 0): "stone"},
            items={"wooden_pickaxe": 1},
            move_failures={(3, 65, 0): "path_not_found"},
        )
        world.st["held"] = {"name": "wooden_pickaxe"}
        ex, state = world.session()
        steps = drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["dig", "dig"])
        self.assertEqual(steps[0]["pos"], [2, 65, 0])
        self.assertEqual(steps[1]["pos"], [3, 65, 0])
        self.assertEqual(state.count("cobblestone"), 1)

        # 验证 voxel_cache 在阶梯探槽过程中命中缓存减少了 get_block RPC 次数
        reset_world_memo()
        _COOLDOWN.clear()
        stair_world = IntegrationWorld(
            blocks={
                (1, 63, 0): "dirt", (1, 62, 0): "dirt",
                (2, 62, 0): "dirt", (2, 61, 0): "dirt",
                (2, 61, 1): "stone",
            },
            items={"wooden_pickaxe": 1},
        )
        stair_world.st["held"] = {"name": "wooden_pickaxe"}
        ex_s, state_s = stair_world.session()
        drive(make("mine_stone", {"count": 1}), ex_s, state_s)
        self.assertGreater(voxel_cache_for(state_s).stats()["hits"], 0)

    def test_floating_canopy_above_real_ground_skipped_before_move_to(self):
        # 复现 2026-11-19 local-target 实机：历史 bot 砍空树干后留下的悬空树冠 (12, 70, 0)（离草地 (12, 64, 0) 高 6 格）
        # 应在发 move_to 前直接跳过，直接走向较远但接地的真实树干 (16, 65, 0)
        world = IntegrationWorld(
            pos=(0.5, 65.0, 0.5),
            blocks={
                (12, 64, 0): "grass_block",
                (12, 70, 0): "birch_log",
                (16, 64, 0): "grass_block",
                (16, 65, 0): "oak_log",
            },
        )
        ex, state = world.session()
        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        moved_targets = [s["pos"] for s in steps if s["op"] == "move_to"]
        self.assertNotIn([12, 70, 0], moved_targets)
        self.assertEqual(moved_targets, [[16, 65, 0]])
        self.assertEqual(state.count("oak_log"), 1)


if __name__ == "__main__":
    unittest.main()
