"""2026-11-19 官方长跑实测暴露的 3 类卡死/烧 Token 根因回归单测（verified: offline）。

覆盖：
1. CraftGear 优先复用 WorldResourceMemo.stations / KNOWN_TABLES 已有工作台（缺 4 木板不再误报木板不足），
   且寻路失败时不永久遗忘真实存在的工作台；MineStone 下挖阶梯同步写入 MINE_TRAILS；
2. ScoutIsland 在地下矿坑（y<60 且近场 SAFE_GROUND 全无两格净空）时优先沿已知地表站点/草地或
   有界安全向上阶梯脱困回地表，不再 502 次原地 Replan；
3. strategy="llm" 下：rules.choose() 已有规则技能时不触发旧战术 LLM 菜单兜底；真正无技能时对
   skill_fallback 强制冷却与指数退避；MainLoop Tier 3 goal_abandoned(target=None) 不做 0ms 忙等空转。
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.loop.engine import LoopConfig, MainLoop
from mcagent.loop.policy import CampaignPlanner, PlanDecision
from mcagent.loop.starter_skills import (
    KNOWN_TABLES,
    CraftGear,
    MineStone,
    ScoutIsland,
    reset_world_memo as reset_starter_memory,
)
from mcagent.planner import skill_choice
from mcagent.planner.llm import LLMClient
from mcagent.runtime import Executor, Replan, State
from mcagent.skills import base as skill_base
from mcagent.skills.base import Param, Skill, rec
from mcagent.skills.items import MINE_TRAILS
from mcagent.skills.testing import drive
from mcagent.skills.world_memo import memo_for, reset_all_world_memos as reset_world_memo
from tests.test_loop_soak import RichGridWorld as GridWorld


def _ok_json(obj: dict) -> tuple[int, str]:
    return 200, json.dumps({
        "choices": [{"message": {"content": json.dumps(obj, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }, ensure_ascii=False)


class _DummyRescueSkill(Skill):
    name = "dummy_rescue"
    description = "无目标无规则技能时的占位兜底技能"
    params = {"n": Param(int, "步数", default=1, min=1, max=3)}
    goal = "rescue:dummy"
    cooldown_s = 1.0
    verified = "offline"

    def run(self, ex, state):
        yield rec("noop", {"n": self.args["n"]})


class CraftGearReuseTableTests(unittest.TestCase):
    def setUp(self):
        reset_starter_memory()
        reset_world_memo()
        skill_base._COOLDOWN.clear()

    def tearDown(self):
        reset_starter_memory()
        reset_world_memo()
        skill_base._COOLDOWN.clear()

    def test_craft_furnace_reuses_memo_station_table_without_extra_planks(self):
        # 复现官方 t=158s：背包有 127 圆石、仅 1 块云杉木板（不够造新工作台），
        # find_blocks(crafting_table, r=16) 未直接返回，但 memo.stations 记着 [10, 64, 0] 的工作台。
        blocks = {
            (10, 64, 0): "crafting_table",
            (10, 63, 0): "grass_block",
            (9, 63, 0): "grass_block",
            (0, 63, 0): "grass_block",
        }
        world = GridWorld(
            blocks=blocks,
            items={"cobblestone": 127, "spruce_planks": 1, "stick": 4},
            pos=(0.5, 64.0, 0.5),
        )
        ex, state = world.session()
        memo_for(state).record_station("crafting_table", [10, 64, 0], dimension="overworld")

        orig_find = world.find_blocks
        def find_no_table(types=None, radius=16, limit=20, **kw):
            if types and "crafting_table" in types:
                return {"blocks": []}
            return orig_find(types=types, radius=radius, limit=limit, **kw)
        world.find_blocks = find_no_table

        orig_craft = world.craft
        def craft_furnace(item, count=1, **kw):
            if item == "furnace":
                world.items["cobblestone"] -= 8
                world.items["furnace"] = world.items.get("furnace", 0) + 1
                return {"crafted": "furnace", "count": 1}
            return orig_craft(item, count, **kw)
        world.craft = craft_furnace

        sk = CraftGear(item="furnace")
        recs = drive(sk, ex, state, max_steps=10)
        self.assertGreaterEqual(state.count("furnace"), 1, f"未复用已知工作台合成熔炉: {recs}")

    def test_craft_gear_climbs_mine_trail_to_surface_station_and_retains_station_memo(self):
        # 身处地下 y=59 矿井，MINE_TRAILS 记着回地表 y=64 入口，memo.stations 记着地表工作台 [10, 64, 0]；
        # 验证 CraftGear 先沿 MINE_TRAILS 爬出矿井再接近地表工作台，且 memo.stations 持久保留工作台记录。
        blocks = {
            (10, 64, 0): "crafting_table",
            (10, 63, 0): "grass_block",
            (0, 63, 0): "grass_block",
            (0, 58, 0): "stone",
        }
        world = GridWorld(
            blocks=blocks,
            items={"cobblestone": 16, "spruce_planks": 1},
            pos=(0.5, 59.0, 0.5),
        )
        ex, state = world.session()
        memo_for(state).record_station("crafting_table", [10, 64, 0], dimension="overworld")
        MINE_TRAILS.append([(0.5, 64.0, 0.5), (0.5, 59.0, 0.5)])

        orig_craft = world.craft
        def craft_furnace(item, count=1, **kw):
            if item == "furnace":
                world.items["cobblestone"] -= 8
                world.items["furnace"] = world.items.get("furnace", 0) + 1
                return {"crafted": "furnace", "count": 1}
            return orig_craft(item, count, **kw)
        world.craft = craft_furnace

        sk = CraftGear(item="furnace")
        recs = drive(sk, ex, state, max_steps=10)
        self.assertGreaterEqual(state.count("furnace"), 1, f"未先爬井再复用工作台合成熔炉: {recs}")
        self.assertIn((10, 64, 0), memo_for(state).stations("crafting_table", dimension="overworld"))

    def test_mine_stone_stair_descent_records_mine_trails(self):
        # 构造无可直挖石头、必须向下挖 1 格阶梯的环境，验证 MINE_TRAILS 记录了入口与下挖点
        blocks = {}
        for x in range(-3, 5):
            for z in range(-3, 5):
                blocks[(x, 62, z)] = "stone"
                blocks[(x, 63, z)] = "dirt"
        blocks[(1, 63, 0)] = "dirt"
        blocks[(1, 62, 0)] = "dirt"
        blocks[(1, 61, 0)] = "stone"  # 阶梯底板
        world = GridWorld(
            blocks=blocks,
            items={"stone_pickaxe": 1},
            pos=(0.5, 64.0, 0.5),
        )
        ex, state = world.session()
        orig_find = world.find_blocks
        def no_exposed_stones(types=None, radius=16, limit=24, **kw):
            return {"blocks": []}
        world.find_blocks = no_exposed_stones

        sk = MineStone(count=1)
        try:
            drive(sk, ex, state, max_steps=6)
        except Replan:
            pass
        self.assertTrue(len(MINE_TRAILS) >= 1 and len(MINE_TRAILS[-1]) >= 2, f"MineStone 未记录阶梯轨迹: {MINE_TRAILS}")


class ScoutIslandUndergroundEscapeTests(unittest.TestCase):
    def setUp(self):
        reset_starter_memory()
        reset_world_memo()
        skill_base._COOLDOWN.clear()

    def tearDown(self):
        reset_starter_memory()
        reset_world_memo()
        skill_base._COOLDOWN.clear()

    def test_scout_escapes_underground_via_known_surface_station_when_stone_has_no_headroom(self):
        # 复现官方 y=52 矿坑：周边 5..10 格 SAFE_GROUND 全是被埋在石层里的石头（上方全无两格空气），
        # 但 memo.stations 记录了地表工作台 [0, 64, 0]
        blocks = {}
        for dx in range(-10, 11):
            for dz in range(-10, 11):
                if abs(dx) >= 5 or abs(dz) >= 5:
                    for y in range(51, 58):
                        blocks[(dx, y, dz)] = "stone"
        blocks[(0, 63, 0)] = "grass_block"
        blocks[(0, 64, 0)] = "crafting_table"
        blocks[(1, 63, 0)] = "grass_block"
        world = GridWorld(
            blocks=blocks,
            items={"stone_pickaxe": 1, "cobblestone": 64},
            pos=(0.5, 52.0, 0.5),
        )
        ex, state = world.session()
        memo_for(state).record_station("crafting_table", [0, 64, 0], dimension="overworld")
        # 模拟地下 find_blocks(SAFE_GROUND) 被石壁占满，但能正常走回地表站点
        orig_find = world.find_blocks
        def buried_land_find(types=None, radius=16, limit=20, **kw):
            if types and "stone" in types and "grass_block" in types:
                return {"blocks": [{"name": "stone", "pos": [dx, 52, dz]} for dx in (-6, 6) for dz in (-6, 6)]}
            return orig_find(types=types, radius=radius, limit=limit, **kw)
        world.find_blocks = buried_land_find

        sk = ScoutIsland(radius=36)
        recs = drive(sk, ex, state, max_steps=10)
        self.assertGreaterEqual(state.pos[1], 62.0, f"未从 y=52 回到地表站点: pos={state.pos}, recs={recs}")

    def test_scout_digs_upward_staircase_when_trapped_underground_with_pickaxe(self):
        # 彻底封在 y=52 地下无任何地表站点/草地，背包有石镐 -> 安全向上开阶梯至少上升 1 格，不报 Replan
        blocks = {}
        for x in range(-6, 8):
            for y in range(50, 58):
                for z in range(-6, 8):
                    blocks[(x, y, z)] = "stone"
        # 当前站立 1x2 空间 (0,52,0) & (0,53,0) 为空气
        del blocks[(0, 52, 0)]
        del blocks[(0, 53, 0)]
        world = GridWorld(
            blocks=blocks,
            items={"stone_pickaxe": 1, "cobblestone": 32},
            pos=(0.5, 52.0, 0.5),
        )
        ex, state = world.session()
        sk = ScoutIsland(radius=36)
        recs = drive(sk, ex, state, max_steps=15)
        self.assertGreater(state.pos[1], 52.5, f"地下矿坑未向上开阶梯脱困: pos={state.pos}, recs={recs}")


class StrategyTokenBlackHoleAndSpinTests(unittest.TestCase):
    def setUp(self):
        reset_starter_memory()
        reset_world_memo()
        skill_base._COOLDOWN.clear()
        self.now = 100.0

    def tearDown(self):
        reset_starter_memory()
        reset_world_memo()
        skill_base._COOLDOWN.clear()

    def _clock(self) -> float:
        return self.now

    def _wait_worker(self, planner: CampaignPlanner) -> None:
        sched = planner.scheduler
        if sched is not None and sched._worker is not None:
            sched._worker.join(timeout=2.0)

    def test_strategy_llm_does_not_call_skill_fallback_when_rules_has_skill(self):
        # 复现官方 291 万 Token 黑洞：current_objective=None 时，rules.choose() 已经有 scout_island，
        # 不得在后台仍派发旧战术 skill_fallback LLM 调用！
        state = State()
        state.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "pos": [0.5, 64.0, 0.5]})
        state.set_inventory({"items": [{"name": "stone_pickaxe", "count": 1}]})

        calls = {"n": 0}
        def poster(*args):
            calls["n"] += 1
            return _ok_json({"skill": "scout_island", "args": {"radius": 32}, "why": "fallback"})

        llm = LLMClient(key="k", fast_proxy="", poster=poster, clock=self._clock, log=None)
        planner = CampaignPlanner(
            mode="campaign",
            strategy="llm",
            llm=llm,
            ledger=(),  # 模拟活动目标已空（全部挂起/完成）
            registry={"scout_island": ScoutIsland},
            min_verified="offline",
            clock=self._clock,
        )
        with patch.object(skill_choice, "choose", wraps=skill_choice.choose) as spy_choose:
            for _ in range(5):
                dec = planner.select(state, [])
                self._wait_worker(planner)
                self.assertIsNotNone(dec.skill)
                self.assertEqual(dec.skill.name, "scout_island")
                self.assertEqual(dec.source, "rule")
            spy_choose.assert_not_called()
            self.assertEqual(calls["n"], 0)

    def test_skill_fallback_obeys_cooldown_and_exponential_backoff(self):
        # 当 rules.choose() 也真正返回 None 时，首拍允许立刻触发 skill_fallback，
        # 但连续触发必须遵守冷却（>= model_min_interval_s）并在失败时指数退避，防止 5s 一次狂烧 Token。
        state = State()
        state.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "pos": [0.5, 64.0, 0.5]})
        state.set_inventory({"items": []})

        calls = {"n": 0}
        def poster_bad(*args):
            calls["n"] += 1
            return _ok_json({"skill": "nonexistent_skill", "args": {}, "why": "invalid"})

        llm = LLMClient(key="k", fast_proxy="", poster=poster_bad, clock=self._clock, log=None)
        planner = CampaignPlanner(
            mode="campaign",
            strategy="llm",
            llm=llm,
            ledger=(),
            registry={"dummy_rescue": _DummyRescueSkill},
            min_verified="offline",
            clock=self._clock,
        )
        # t=100s：首次真正无技能 -> 立即派发第 1 次（skill_choice.choose 内含 attempts=2，故 poster=2）
        planner.select(state, [])
        self._wait_worker(planner)
        first_calls = calls["n"]
        self.assertGreaterEqual(first_calls, 1)

        # t=105s（仅过 5s < 20s）：回收上次失败结果，但不得立刻重发第 2 次！
        self.now = 105.0
        planner.select(state, [])
        self._wait_worker(planner)
        self.assertEqual(calls["n"], first_calls, "5s 内重复派发了 skill_fallback LLM！")

        # t=119s（仍 < 首次失败指数退避 40s）：依然拦截
        self.now = 119.0
        planner.select(state, [])
        self._wait_worker(planner)
        self.assertEqual(calls["n"], first_calls)

        # t=150s（超过首次失败退避 40s）：允许第 2 次派发
        self.now = 150.0
        planner.select(state, [])
        self._wait_worker(planner)
        self.assertGreater(calls["n"], first_calls)

    def test_tier3_goal_abandoned_with_no_target_sleeps_instead_of_busy_spin(self):
        # 复现官方日志 Tier 1 -> Tier 3 goal_abandoned: target=None abandoned=False 0ms 忙等空转
        world = GridWorld(blocks={(0, 63, 0): "grass_block"}, items={}, pos=(0.5, 64.0, 0.5))
        ex, state = world.session()

        sleeps: list[float] = []
        tick_calls = {"n": 0}

        class _CooldownEmptyPlanner:
            current_objective = None
            def select(self, st, failures):
                tick_calls["n"] += 1
                if len(sleeps) >= 3 or tick_calls["n"] > 40:
                    return PlanDecision(None, "idle", "测试止损退出", stop_episode=True)
                return PlanDecision(None, "idle", "全部冷却中", cooldown_blocked=True)
            def clear_tactical_cooldowns(self, st, goal_id=None, include_relocate=False):
                return []
            def make_relocate_skill(self, st):
                return None
            def abandon_goal(self, gid):
                return False

        cfg = LoopConfig(max_seconds=0.0, idle_sleep_s=1.0, max_steps=0)
        loop = MainLoop(world, state, ex, _CooldownEmptyPlanner(), config=cfg, log=lambda *_: None)
        loop._do_sleep = lambda sec: sleeps.append(sec)
        loop.run()
        self.assertGreaterEqual(len(sleeps), 3, f"Tier 3 target=None 发生了无 sleep 忙等空转 (ticks={tick_calls['n']}, sleeps={sleeps})")
        self.assertLessEqual(tick_calls["n"], 20, f"空转拍数过高: {tick_calls['n']}")


if __name__ == "__main__":
    unittest.main()
