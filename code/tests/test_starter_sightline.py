"""starter_skills 接入 skills.sightline 眼高、方块中心眼距与 3D 视线遮挡单测（verified: offline）。"""
from __future__ import annotations

import unittest

import mcagent.skills as skills_pkg
from mcagent.client import ApiError
from mcagent.loop import reset_world_memo
from mcagent.loop.starter_skills import KNOWN_TABLES
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills import sightline as sightline_mod
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import drive
from tests.test_loop_soak import RichGridWorld


class SightlineSkillWorld(RichGridWorld):
    """支持按目标点注入 move_to 故障或近墙停步的离线测试夹具。"""

    def __init__(self, *, pos=(0.5, 64.0, 0.5), blocks=None, items=None, move_failures=None, stuck_moves=None):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.move_failures = {tuple(k): v for k, v in (move_failures or {}).items()}
        self.stuck_moves = {tuple(k): list(v) for k, v in (stuck_moves or {}).items()}

    def move_to(self, *args, **kwargs):
        pos = kwargs.get("pos")
        key = tuple(pos) if pos is not None else None
        if key in self.move_failures:
            self.calls.append(("move_to", {"pos": list(pos)}))
            raise ApiError(self.move_failures[key], "injected move_to failure")
        if key in self.stuck_moves:
            self.calls.append(("move_to", {"pos": list(pos)}))
            self.st["pos"] = list(self.stuck_moves[key])
            return {"pos": list(self.st["pos"])}
        return super().move_to(*args, **kwargs)

    def place(self, item, pos=None, *, stop_on=None):
        if pos is None:
            ix, iy, iz = int(self.st["pos"][0]), int(self.st["pos"][1]), int(self.st["pos"][2])
            for dx, dz in ((1, 0), (0, 1), (-1, 0), (0, -1)):
                cand = (ix + dx, iy, iz + dz)
                if cand not in self.blocks:
                    pos = list(cand)
                    break
        return super().place(item, pos=pos, stop_on=stop_on)


class SkillsPackageExportTests(unittest.TestCase):
    def test_skills_package_exports_sightline_module(self):
        self.assertIn("sightline", skills_pkg.__all__)
        self.assertIs(getattr(skills_pkg, "sightline", None), sightline_mod)


class GatherWoodSightlineTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_overhead_trunk_within_eye_reach_digs_in_place_without_move_to(self):
        # 脚底 (0.5, 64.0, 0.5) 站立眼睛 (0.5, 65.62, 0.5) 到 [0, 69, 0] 中心 (0.5, 69.5, 0.5) 仅 3.88 <= 4.5
        world = SightlineSkillWorld(
            blocks={(0, 69, 0): "oak_log"},
            move_failures={(0, 69, 0): "path_not_found"},
        )
        ex, state = world.session()
        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["dig"])
        self.assertEqual(steps[0]["pos"], [0, 69, 0])
        self.assertEqual(state.count("oak_log"), 1)

    def test_diagonal_upward_log_within_eye_reach_digs_in_place_without_move_to(self):
        # [2, 68, 0] 旧脚底角点距离 4.30 > 4.2 会误发 move_to，实际眼距仅 3.51 <= 4.5
        world = SightlineSkillWorld(
            blocks={(2, 68, 0): "oak_log"},
            move_failures={(2, 68, 0): "path_not_found"},
        )
        ex, state = world.session()
        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["dig"])
        self.assertEqual(steps[0]["pos"], [2, 68, 0])
        self.assertEqual(state.count("oak_log"), 1)

    def test_high_canopy_beyond_eye_reach_is_still_skipped(self):
        # [0, 71, 0] 眼距 5.88 > 4.5 且水平 < 3.0、高差 > 4.0，仍应直接跳过不发 move_to
        alone = SightlineSkillWorld(blocks={(0, 71, 0): "oak_log"})
        ex, state = alone.session()
        with self.assertRaisesRegex(Replan, "没有可砍的裸露原木"):
            drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, [])

        paired = SightlineSkillWorld(
            blocks={(0, 71, 0): "oak_log", (6, 64, 0): "oak_log"},
            move_failures={(0, 71, 0): "path_not_found"},
        )
        ex2, state2 = paired.session()
        steps = drive(make("gather_wood", {"count": 2}), ex2, state2)
        self.assertEqual([s["op"] for s in steps], ["move_to", "dig"])
        self.assertEqual(steps[0]["pos"], [6, 64, 0])
        self.assertEqual(state2.count("oak_log"), 1)
        self.assertEqual(paired.blocks.get((0, 71, 0)), "oak_log")

    def test_wall_occluded_nearby_log_moves_first_instead_of_blind_dig(self):
        # 2 格实心石墙 (1, 64, 0) + (1, 65, 0) 完全阻断视线，[2, 64, 0] 不得原地隔墙盲挖
        world = SightlineSkillWorld(
            blocks={
                (1, 64, 0): "stone",
                (1, 65, 0): "stone",
                (2, 64, 0): "oak_log",
            }
        )
        ex, state = world.session()
        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "dig"])
        self.assertEqual(steps[0]["pos"], [2, 64, 0])
        self.assertEqual(state.count("oak_log"), 1)


class CraftGearSightlineTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_west_symmetric_table_reused_in_place_without_move_to(self):
        # (-3, 64, 0) 旧脚底角点距离 3.54 > 3.5 误发 move_to，实际眼距 3.20 <= STATION_EYE_REACH
        world = SightlineSkillWorld(
            blocks={(-3, 64, 0): "crafting_table"},
            items={"stick": 2, "cobblestone": 3},
            move_failures={(-3, 64, 0): "path_not_found"},
        )
        ex, state = world.session()
        steps = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["craft"])
        self.assertEqual(state.count("stone_pickaxe"), 1)
        self.assertEqual(KNOWN_TABLES, [(-3.0, 64.0, 0.0)])

    def test_wall_occluded_table_attempts_move_and_falls_back_when_sightline_remains_blocked(self):
        # 2 格实心墙 (1, 64, 0) + (1, 65, 0) 挡住 (2, 64, 0) 工作台：先尝试 move_to，仍看不见则放弃并放新台
        for mode in ("stuck_before_wall", "path_not_found"):
            with self.subTest(mode=mode):
                reset_world_memo()
                _COOLDOWN.clear()
                KNOWN_TABLES.append((2.0, 64.0, 0.0))
                world = SightlineSkillWorld(
                    blocks={
                        (1, 64, 0): "stone",
                        (1, 65, 0): "stone",
                        (2, 64, 0): "crafting_table",
                    },
                    items={"oak_planks": 4, "stick": 2, "cobblestone": 3},
                    move_failures={(2, 64, 0): "path_not_found"} if mode == "path_not_found" else None,
                    stuck_moves={(2, 64, 0): [0.5, 64.0, 0.5]} if mode == "stuck_before_wall" else None,
                )
                ex, state = world.session()
                steps = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
                self.assertEqual([s["op"] for s in steps], ["move_to", "craft", "place", "craft"])
                self.assertEqual(steps[0]["pos"], [2, 64, 0])
                self.assertNotIn((2.0, 64.0, 0.0), KNOWN_TABLES)
                self.assertEqual(state.count("stone_pickaxe"), 1)
                self.assertEqual(world.blocks[(1, 64, 0)], "stone")


class MineStoneSightlineTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_diagonal_upward_exposed_stone_digs_in_place_without_move_to(self):
        # [1, 67, 0] 位于 cur[1] + 3，旧 +2.5 上限会漏选，实际眼距 2.13 <= 4.5 可直接原地挖掘
        world = SightlineSkillWorld(
            blocks={(1, 67, 0): "stone"},
            items={"wooden_pickaxe": 1},
            move_failures={(1, 67, 0): "path_not_found"},
        )
        world.st["held"] = {"name": "wooden_pickaxe"}
        ex, state = world.session()
        steps = drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["dig"])
        self.assertEqual(steps[0]["pos"], [1, 67, 0])
        self.assertEqual(state.count("cobblestone"), 1)

    def test_wall_occluded_stone_moves_first_and_skips_if_still_occluded(self):
        # 2 格黑曜石墙 (1, 64..65, 0) 挡死 [2, 64, 0]：不得隔墙盲挖；若 move_to 后仍被墙挡死则拉黑改挖可见的 [0, 64, 3]
        world = SightlineSkillWorld(
            blocks={
                (1, 64, 0): "obsidian",
                (1, 65, 0): "obsidian",
                (2, 64, 0): "stone",
                (0, 64, 3): "stone",
            },
            items={"wooden_pickaxe": 1},
            stuck_moves={(2, 64, 0): [0.5, 64.0, 0.5]},
        )
        world.st["held"] = {"name": "wooden_pickaxe"}
        ex, state = world.session()
        steps = drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "dig"])
        self.assertEqual(steps[0]["pos"], [2, 64, 0])
        self.assertEqual(steps[1]["pos"], [0, 64, 3])
        self.assertEqual(world.blocks.get((2, 64, 0)), "stone")
        self.assertEqual(state.count("cobblestone"), 1)

    def test_sneaking_stance_switches_eye_height_to_1_27(self):
        # [3, 68, 1] 中心 (3.5, 68.5, 1.5)：站立眼高 65.62 时眼距 4.28 <= 4.5 原地直挖；
        # 潜行眼高 65.27 时眼距 4.52 > 4.5 必须先触发 move_to
        standing_world = SightlineSkillWorld(
            blocks={(3, 68, 1): "stone"},
            items={"wooden_pickaxe": 1},
        )
        standing_world.st["held"] = {"name": "wooden_pickaxe"}
        standing_world.st["sneaking"] = False
        ex_stand, state_stand = standing_world.session()
        stand_steps = drive(make("mine_stone", {"count": 1}), ex_stand, state_stand)
        self.assertEqual([s["op"] for s in stand_steps], ["dig"])

        sneaking_world = SightlineSkillWorld(
            blocks={(3, 68, 1): "stone"},
            items={"wooden_pickaxe": 1},
            stuck_moves={(3, 68, 1): [2.5, 64.0, 1.5]},
        )
        sneaking_world.st["held"] = {"name": "wooden_pickaxe"}
        sneaking_world.st["sneaking"] = True
        ex_sneak, state_sneak = sneaking_world.session()
        sneak_steps = drive(make("mine_stone", {"count": 1}), ex_sneak, state_sneak)
        self.assertEqual([s["op"] for s in sneak_steps], ["move_to", "dig"])


if __name__ == "__main__":
    unittest.main()
