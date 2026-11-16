"""T1 采矿、铁线、合成、近场采掘、钻石线与 starter_skills 接入 sightline 单测（verified: offline）。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcagent.loop import reset_world_memo
from mcagent.runtime import EnvBlocked
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.diamond import CraftDiamondGear
from mcagent.skills.diamond_site import _in_reach
from mcagent.skills.field import mine_block_batch
from mcagent.skills.iron import SmeltIron, reset_iron_memo
from mcagent.skills.recipes import _at_table, craft_recipe
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import drive
from tests.test_diamond import DiamondWorld, _craft as _diamond_craft
from tests.test_iron import IronWorld, view as _furnace_view
from tests.test_loop_soak import RichGridWorld
from tests.test_t1_recipes import CraftWorld


class SmeltIronSightlineTests(unittest.TestCase):
    def setUp(self):
        reset_iron_memo()
        reset_world_memo()
        patcher = patch("mcagent.skills.iron.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(reset_iron_memo)
        self.addCleanup(reset_world_memo)

    def test_overhead_diagonal_iron_ore_is_dug_in_place_without_move_to(self):
        # 脚底 (0.5, 64.0, 0.5) 到角点 (1, 68, 0) 为 4.06 > 4，但站立眼距仅 3.05 <= 4.5
        world = IronWorld(
            blocks={(1, 68, 0): "iron_ore"},
            views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
        )
        ex, state = world.session()
        drive(SmeltIron(count=1), ex, state)
        self.assertFalse(any(op == "move_to" for op, _ in world.calls))
        self.assertEqual([args["pos"] for op, args in world.calls if op == "dig"], [[1, 68, 0]])
        self.assertEqual(state.count("iron_ingot"), 1)

    def test_wall_occluded_iron_ore_moves_first_and_is_excluded_from_in_reach_scan(self):
        # (1, 64, 0) + (1, 65, 0) 两格实心石墙封死视线，不能在原地隔墙盲挖 (2, 64, 0)
        world = IronWorld(
            blocks={
                (1, 64, 0): "stone",
                (1, 65, 0): "stone",
                (2, 64, 0): "iron_ore",
                (0, 64, 2): "iron_ore",
            },
            views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
        )
        ex, state = world.session()
        skill = SmeltIron(count=1)
        # in_reach_only=True 时只保留真正视线畅通且眼距 <= 4.5 的近场矿点
        in_reach = skill._ore_candidates(ex, state, in_reach_only=True)
        self.assertEqual(in_reach, [[0, 64, 2]])

        # 仅剩被石墙遮挡的 (2, 64, 0) 时，_mine 必须先发 move_to 绕墙而非原地盲发 dig
        world2 = IronWorld(
            blocks={
                (1, 64, 0): "stone",
                (1, 65, 0): "stone",
                (2, 64, 0): "iron_ore",
            },
            views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
        )
        orig_move = world2.move_to

        def move_around_wall(*args, **kwargs):
            orig_move(*args, **kwargs)
            world2.st["pos"] = [2.5, 64.0, 1.5]
            return {"pos": [2.5, 64.0, 1.5]}

        world2.move_to = move_around_wall
        ex2, state2 = world2.session()
        drive(SmeltIron(count=1), ex2, state2)
        action_order = [op for op, _ in world2.calls if op in ("move_to", "dig")]
        self.assertEqual(action_order, ["move_to", "dig"])
        self.assertEqual(state2.count("iron_ingot"), 1)


class CraftRecipeSightlineTests(unittest.TestCase):
    def test_west_negative_coordinate_table_crafts_in_place_without_move_to(self):
        # (-3, 64, 0) 旧脚底角点距离 3.54 > 3.5，实际眼睛到中心距离 3.20 <= 3.5
        world = CraftWorld(
            items={"iron_ingot": 3, "stick": 2},
            tables=[(-3, 64, 0)],
            crafts=[({"gained": 1}, {"iron_pickaxe": 1, "iron_ingot": -3, "stick": -2})],
        )
        ex, state = world.session()
        steps = drive(craft_recipe(ex, state, "iron_pickaxe", goal_qty=1), ex)
        self.assertEqual([s["op"] for s in steps], ["craft"])
        self.assertFalse(any(op == "move_to" for op, _ in world.calls))
        self.assertEqual(state.count("iron_pickaxe"), 1)

    def test_wall_occluded_nearby_table_is_not_at_table_and_triggers_move_to(self):
        # 工作台 (2, 64, 0) 眼距 2.29 < 3.5，但被 (1, 64..65, 0) 实心墙遮挡视线
        world = CraftWorld(
            items={"iron_ingot": 3, "stick": 2},
            tables=[(2, 64, 0)],
            moves=[(2.5, 64.0, 1.5)],
            crafts=[({"gained": 1}, {"iron_pickaxe": 1, "iron_ingot": -3, "stick": -2})],
        )
        world.blocks[(1, 64, 0)] = "stone"
        world.blocks[(1, 65, 0)] = "stone"
        ex, state = world.session()
        self.assertFalse(_at_table(ex, state, [2, 64, 0]))

        steps = drive(craft_recipe(ex, state, "iron_pickaxe", goal_qty=1), ex)
        self.assertEqual([s["op"] for s in steps], ["move_to", "craft"])
        self.assertEqual(steps[0]["pos"], [2, 64, 0])
        self.assertEqual(state.count("iron_pickaxe"), 1)


class MineBlockBatchSightlineTests(unittest.TestCase):
    def test_wall_occluded_ore_is_excluded_without_blind_dig(self):
        world = ScriptedWorld(
            blocks={(1, 64, 0): "stone", (1, 65, 0): "stone", (2, 64, 0): "iron_ore"},
        )
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(mine_block_batch(ex, state, [(2, 64, 0)], block_types="iron_ore", drop="raw_iron"), ex)
        self.assertEqual(ex.acts, [])

    def test_sneaking_eye_height_enforces_five_block_reach_boundary(self):
        # 脚底 y=64.0 时，目标 (0, 70, 0) 中心 y=70.5：
        # 站立眼高 65.62 -> 眼距 4.88 <= 5.0（可挖）；潜行眼高 65.27 -> 眼距 5.23 > 5.0（过滤）
        sneak_world = ScriptedWorld(blocks={(0, 70, 0): "iron_ore"})
        sneak_world.st["sneaking"] = True
        ex_sneak, state_sneak = sneak_world.session()
        with self.assertRaises(EnvBlocked):
            drive(
                mine_block_batch(ex_sneak, state_sneak, [(0, 70, 0)], block_types="iron_ore", drop="raw_iron"),
                ex_sneak,
            )
        self.assertEqual(ex_sneak.acts, [])

        stand_world = ScriptedWorld(blocks={(0, 70, 0): "iron_ore"})
        stand_world.st["sneaking"] = False

        def dig_high_ore(w, *, pos, **kwargs):
            w.blocks.pop(tuple(pos))
            w.items["raw_iron"] = 1
            return {"dug": "iron_ore", "gained": {"raw_iron": 1}}

        stand_world.queue("dig", dig_high_ore)
        ex_stand, state_stand = stand_world.session()
        steps = drive(
            mine_block_batch(ex_stand, state_stand, [(0, 70, 0)], block_types="iron_ore", drop="raw_iron"),
            ex_stand,
        )
        self.assertEqual([s["op"] for s in steps], ["dig"])
        self.assertEqual(state_stand.count("raw_iron"), 1)


class DiamondSightlineTests(unittest.TestCase):
    def test_craft_diamond_gear_reuses_west_negative_axis_table_in_place(self):
        west_table = (-3, -53, 0)
        world = DiamondWorld(
            items={"diamond": 4},
            blocks={west_table: "crafting_table"},
            exposed=(west_table,),
        )
        world.queue("craft", _diamond_craft)
        ex, state = world.session()
        steps = drive(CraftDiamondGear(item="diamond_boots"), ex, state)
        self.assertEqual([s["op"] for s in steps], ["craft"])
        self.assertEqual(state.count("diamond_boots"), 1)
        world.assert_exhausted()

    def test_diamond_site_in_reach_accepts_status_mapping_and_tuple_with_sneaking_eye(self):
        # 脚底 (0.5, -53.0, 0.5) 到 (0, -48, 0) 中心 (0.5, -47.5, 0.5)：
        # 站立眼 (-51.38) 距离 3.88 <= 4.0；潜行眼 (-51.73) 距离 4.23 > 4.0
        target = (0, -48, 0)
        world = DiamondWorld(blocks={target: "diamond_ore"})
        ex, _ = world.session()
        self.assertTrue(_in_reach(ex, (0.5, -53.0, 0.5), target, reach=4.0))
        self.assertTrue(_in_reach(ex, {"pos": [0.5, -53.0, 0.5], "sneaking": False}, target, reach=4.0))
        self.assertFalse(_in_reach(ex, {"pos": [0.5, -53.0, 0.5], "sneaking": True}, target, reach=4.0))


class StarterSkillsSightlineTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_gather_wood_digs_overhead_trunk_in_place_without_move_to(self):
        # 头顶正上方 +5 格原木 [0, 69, 0]：眼距 3.88 <= 4.5，无需地面寻路直接原地挖掘
        world = RichGridWorld(blocks={(0, 69, 0): "oak_log"})
        ex, state = world.session()
        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["dig"])
        self.assertEqual(steps[0]["pos"], [0, 69, 0])
        self.assertEqual(state.count("oak_log"), 1)

    def test_craft_gear_reuses_west_negative_axis_table_in_place(self):
        world = RichGridWorld(
            items={"stick": 2, "cobblestone": 3},
            blocks={(-3, 64, 0): "crafting_table"},
        )
        ex, state = world.session()
        steps = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["craft"])
        self.assertEqual(state.count("stone_pickaxe"), 1)

    def test_mine_stone_digs_west_elevated_stone_in_place_without_move_to(self):
        # (-4, 65, 0) 旧脚底角点距离 4.64 > 4.0，但眼睛 (0.5, 65.62, 0.5) 到中心 (-3.5, 65.5, 0.5) 仅 4.00 <= 4.5
        world = RichGridWorld(
            blocks={(-4, 65, 0): "stone"},
            items={"wooden_pickaxe": 1},
        )
        world.st["held"] = {"name": "wooden_pickaxe"}
        ex, state = world.session()
        steps = drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["dig"])
        self.assertEqual(state.count("cobblestone"), 1)


if __name__ == "__main__":
    unittest.main()
