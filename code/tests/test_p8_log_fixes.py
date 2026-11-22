"""基于 P8 靶场实机日志（p8_plant2..4 / p8_s1_iron / p8_iron2 / mc_v2_live_run2）提炼的离线回归单测。"""
from __future__ import annotations

import unittest
from unittest import mock

from mcagent.client import ApiError
from mcagent.loop.starter_skills import MineStone, _craft_with_space
from mcagent.runtime import Replan
from mcagent.skills.recipes import craft_recipe
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.world_memo import memo_for


class P8LogRegressionTests(unittest.TestCase):
    def test_craft_with_space_waits_for_delayed_set_slot_sync(self):
        """复现 p8_iron2 seq=156 / p8_s1_iron seq=543：/craft ok=True gained=0 后延迟 1 tick 入包。"""
        world = ScriptedWorld(items={"birch_planks": 4})
        polls = {"n": 0}
        orig_inv = world.inventory

        def delayed_inv(timeout=10):
            polls["n"] += 1
            if polls["n"] >= 3:
                world.items["stick"] = 4
            return orig_inv(timeout)

        world.inventory = delayed_inv

        def craft_stick(w, *, item, count=1, **_):
            w.items["birch_planks"] -= 2
            # 服务端回执时 stick 尚未同步到背包
            return {"crafted": item, "gained": 0, "used_table": False}

        world.queue("craft", craft_stick)
        ex, state = world.session()
        with mock.patch("mcagent.loop.starter_skills.time.sleep", return_value=None):
            steps = drive(_craft_with_space(ex, state, "stick"), ex, state)
        self.assertEqual([s["op"] for s in steps], ["craft"])
        self.assertEqual(state.count("stick"), 4)

    def test_craft_with_space_recovers_or_forgets_table_on_updateslot_timeout(self):
        """复现 p8_s1_iron seq=1366：mineflayer updateSlot:0 timeout 包成 bad_args。"""
        # 情况 1：实际已产出，刷背包后直接视作成功，不误抛 Replan
        w1 = ScriptedWorld(items={"oak_planks": 3, "stick": 2})
        polls = {"n": 0}
        orig_inv = w1.inventory

        def delayed_pick(timeout=10):
            polls["n"] += 1
            if polls["n"] >= 2:
                w1.items["wooden_pickaxe"] = 1
            return orig_inv(timeout)

        w1.inventory = delayed_pick
        w1.queue("craft", ApiError("bad_args", "Error: Event updateSlot:0 did not fire within timeout of 20000ms"))
        ex1, st1 = w1.session()
        with mock.patch("mcagent.loop.starter_skills.time.sleep", return_value=None):
            steps = drive(_craft_with_space(ex1, st1, "wooden_pickaxe", table_pos=[2, 64, 0]), ex1, st1)
        self.assertEqual([s["op"] for s in steps], ["craft"])
        self.assertEqual(st1.count("wooden_pickaxe"), 1)

        # 情况 2：确实未产出，遗忘失效工作台并向外抛出 Replan
        w2 = ScriptedWorld(items={"oak_planks": 3, "stick": 2})
        w2.queue("craft", ApiError("bad_args", "Error: Event updateSlot:0 did not fire within timeout of 20000ms"))
        ex2, st2 = w2.session()
        with mock.patch("mcagent.loop.starter_skills.time.sleep", return_value=None):
            with self.assertRaises(Replan):
                drive(_craft_with_space(ex2, st2, "wooden_pickaxe", table_pos=[2, 64, 0]), ex2, st2)
        self.assertTrue(memo_for(st2).is_blocked([2, 64, 0], dimension="overworld", category="station"))

    def test_craft_recipe_waits_for_delayed_set_slot_sync(self):
        """recipes.craft_recipe 同样兼容 /craft 返回后延迟 1-2 tick 才同步的新产出。"""
        world = ScriptedWorld(items={"oak_planks": 4})
        polls = {"n": 0}
        orig_inv = world.inventory

        def delayed_inv(timeout=10):
            polls["n"] += 1
            # 1: craft_recipe 开头的 initial refresh；2: craft 后立即 refresh（尚未到）；3: settle poll 命中
            if polls["n"] >= 3:
                world.items["stick"] = 4
            return orig_inv(timeout)

        world.inventory = delayed_inv

        def craft_stick(w, *, item, count=1, **_):
            w.items["oak_planks"] -= 2
            return {"crafted": item, "gained": 0}

        world.queue("craft", craft_stick)
        ex, state = world.session()
        with mock.patch("mcagent.skills.recipes.time.sleep", return_value=None):
            steps = drive(craft_recipe(ex, state, "stick", goal_qty=4), ex, state)
        self.assertEqual([s["op"] for s in steps], ["craft"])
        self.assertEqual(state.count("stick"), 4)

    def test_mine_stone_stair_recovers_ghost_dirt_rollback_without_breaking_peel_contract(self):
        """复现 p8_iron2 seq=331..347：同格非重力 dirt 幽灵回滚自动补挖一次，同时保留表层剥离契约。"""
        blocks: dict[tuple[int, int, int], str] = {}
        for x in range(-2, 5):
            for z in range(-2, 3):
                blocks[(x, 63, z)] = "dirt"
                blocks[(x, 62, z)] = "stone"
                blocks[(x, 61, z)] = "stone"
        blocks[(1, 64, 0)] = "dirt"
        blocks[(1, 63, 0)] = "dirt"
        blocks[(2, 64, 0)] = "air"
        blocks[(2, 63, 0)] = "air"
        blocks[(2, 62, 0)] = "stone"

        gw = GridWorld(blocks=blocks, pos=[0.5, 64.0, 0.5], items={"wooden_pickaxe": 1})
        gw.find_blocks = lambda types=None, radius=16, limit=24, **kw: {"blocks": []}
        ghost_dirt_digs = {"n": 0}
        orig_dig = gw.dig

        def dig_with_ghost(pos, collect=True, stop_on=None):
            pt = tuple(int(v) for v in pos)
            if pt == (1, 64, 0) and ghost_dirt_digs["n"] == 0:
                ghost_dirt_digs["n"] += 1
                # 首次挖掘返回成功但服务端幽灵回滚：方块仍在 world 中
                return {"dug": "dirt", "dig_ms": 760, "tool": "wooden_pickaxe", "gained": {}}
            if pt == (1, 64, 0):
                ghost_dirt_digs["n"] += 1
            res = orig_dig(pos, collect=collect, stop_on=stop_on)
            if res.get("dug") == "stone":
                gw.items["cobblestone"] = gw.items.get("cobblestone", 0) + 1
            return res

        gw.dig = dig_with_ghost
        ex, state = gw.session()
        skill = MineStone(count=1)
        with mock.patch("mcagent.loop.starter_skills.time.sleep", return_value=None):
            drive(skill, ex, state)
        # (1, 64, 0) 幽灵回滚后被补挖 1 次（总计 2 次），且成功拿到圆石不触发“阶梯净空发生变化”
        self.assertEqual(ghost_dirt_digs["n"], 2)
        self.assertGreaterEqual(state.count("cobblestone"), 1)


if __name__ == "__main__":
    unittest.main()
