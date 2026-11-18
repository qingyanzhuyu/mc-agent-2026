"""单目标止损退轮、CraftGear 去重与 LLM 木石饱和菜单过滤离线单测（verified: offline）。"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from mcagent.loop import (
    CampaignPlanner,
    GoalSpec,
    PlanDecision,
    RulePlanner,
    reset_world_memo,
)
from mcagent.loop.goals import GOAL_MAP
from mcagent.loop.starter_skills import CraftGear, GatherWood, MineStone, ScoutIsland
from mcagent.planner import skill_choice
from mcagent.planner.llm import LLMClient
from mcagent.runtime import EnvBlocked, State
from mcagent.skills import Skill, make
from mcagent.skills import base as skill_base


class _PlantSeedStub(Skill):
    name = "plant_seed"
    description = "测试用种地技能"
    params = {}
    goal = "adv:plant_seed"
    verified = "offline"

    def run(self, ex, state):
        return iter(())


class _DeflectArrowStub(Skill):
    name = "deflect_skeleton_arrow"
    description = "测试用盾反技能"
    params = {}
    goal = "adv:deflect_arrow"
    verified = "offline"

    def run(self, ex, state):
        return iter(())


class _CraftIronGearStub(Skill):
    name = "craft_iron_gear"
    description = "测试用铁器合成技能"
    params = {}
    goal = "craft:iron_pickaxe"
    verified = "offline"

    def run(self, ex, state):
        return iter(())


def _make_state(items: list[dict] | None = None, *, pos=(0.5, 64.0, 0.5)) -> State:
    st = State()
    st.set_status({
        "ready": True,
        "alive": True,
        "health": 20,
        "food": 20,
        "dimension": "overworld",
        "is_day": True,
        "pos": list(pos),
    })
    st.set_inventory({"items": items or []})
    return st


def _make_dummy_llm(reply_skill: str = "scout_island", args: dict | None = None) -> LLMClient:
    payload = json.dumps({"skill": reply_skill, "args": args or {}, "why": "测试选择"}, ensure_ascii=False)
    body = json.dumps({
        "choices": [{"message": {"content": payload}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
    })
    return LLMClient(key="dummy", fast_proxy="", poster=lambda *a: (200, body), log=None)


class StopLossSignalTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        self.addCleanup(skill_base._COOLDOWN.clear)
        self.addCleanup(reset_world_memo)

    def test_plan_decision_stop_episode_default_and_explicit(self):
        default_dec = PlanDecision(None, "idle", "默认")
        self.assertFalse(default_dec.stop_episode)
        explicit_dec = PlanDecision(None, "idle", "止损", stop_episode=True)
        self.assertTrue(explicit_dec.stop_episode)

    def test_targets_exhausted_iff_matrix_and_rule_choose_signal(self):
        ledger = (
            GoalSpec(
                id="g_main",
                title="高风险主目标",
                tier="T1",
                score=15,
                skill="plant_seed",
                done_advancements=("minecraft:husbandry/plant_seed",),
                done_items=("wheat>=1",),
                budget_s=60.0,
                max_fails_per_ep=2,
                stop_loss=True,
                stage="graduated",
            ),
            GoalSpec(
                id="g_backup",
                title="次选目标",
                tier="T1",
                score=5,
                skill="scout_island",
                done_items=("diamond>=1",),
                budget_s=120.0,
                max_fails_per_ep=2,
                stop_loss=True,
                stage="graduated",
            ),
        )
        reg = {
            "plant_seed": _PlantSeedStub,
            "scout_island": ScoutIsland,
        }
        state = _make_state()

        # 1) 未设定 target_goal：即使所有目标均耗尽也不得触发 targets_exhausted
        no_target = RulePlanner(reg, ledger=ledger, target_goal=None)
        no_target.ep_goal_fails["g_main"] = 2
        no_target.ep_goal_fails["g_backup"] = 2
        self.assertFalse(no_target.targets_exhausted(state))

        # 2) 单目标尚有可执行步时：False，choose stop_episode=False
        rp = RulePlanner(reg, ledger=ledger, target_goal="g_main")
        self.assertFalse(rp.targets_exhausted(state))
        d_active = rp.choose(state)
        self.assertIsNotNone(d_active.skill)
        self.assertFalse(d_active.stop_episode)

        # 3) 仅处于环境挂起时：False（应派发 scout_island 做环境恢复，而不是直接结束本轮）
        rp.record_environment_block(
            _PlantSeedStub(),
            EnvBlocked("暂无耕地", missing_predicate="farmland", searched_volume="半径 32", wake_on="chunk_moved"),
            state,
        )
        self.assertFalse(rp.targets_exhausted(state))
        d_env = rp.choose(state)
        self.assertEqual(d_env.skill.name, "scout_island")
        self.assertTrue(d_env.environment_recovery)
        self.assertFalse(d_env.stop_episode)

        # 4) 消除环境挂起后，因失败次数触及 max_fails_per_ep 止损：True，choose 返回 stop_episode=True
        rp._environment_blocks.clear()
        rp.ep_goal_fails["g_main"] = 2
        self.assertTrue(rp.targets_exhausted(state))
        d_stopped = rp.choose(state)
        self.assertIsNone(d_stopped.skill)
        self.assertEqual(d_stopped.source, "idle")
        self.assertTrue(d_stopped.stop_episode)

        # 5) 目标已达成（即使历史失败数曾到达上限）：False，且 choose stop_episode=False
        achieved_state = _make_state([{"name": "wheat", "count": 1, "slot": 0}])
        self.assertFalse(rp.targets_exhausted(achieved_state))
        d_done = rp.choose(achieved_state)
        self.assertIsNone(d_done.skill)
        self.assertFalse(d_done.stop_episode)

        # 6) 因 stop_loss=True 且累计耗时 >= budget_s 耗尽：True
        rp_budget = RulePlanner(reg, ledger=ledger, target_goal="g_main")
        rp_budget.ep_goal_elapsed["g_main"] = 60.0
        self.assertTrue(rp_budget.targets_exhausted(state))
        self.assertTrue(rp_budget.choose(state).stop_episode)

        # 7) 多目标部分止损、另一目标仍可推进 -> False；剩余目标达成后 -> True
        rp_multi = RulePlanner(reg, ledger=ledger, target_goal="g_main,g_backup")
        rp_multi.ep_goal_fails["g_main"] = 2
        self.assertFalse(rp_multi.targets_exhausted(state))
        self.assertEqual(rp_multi.choose(state).skill.name, "scout_island")

        diamond_state = _make_state([{"name": "diamond", "count": 1, "slot": 0}])
        self.assertTrue(rp_multi.targets_exhausted(diamond_state))
        self.assertTrue(rp_multi.choose(diamond_state).stop_episode)

    def test_campaign_planner_llm_short_circuits_when_targets_exhausted(self):
        ledger = (
            GoalSpec(
                id="g_seed",
                title="种地",
                tier="T1",
                score=5,
                skill="plant_seed",
                done_advancements=("minecraft:husbandry/plant_seed",),
                budget_s=90.0,
                max_fails_per_ep=2,
                stop_loss=True,
                stage="graduated",
            ),
        )
        reg = {
            "plant_seed": _PlantSeedStub,
            "gather_wood": GatherWood,
            "scout_island": ScoutIsland,
        }
        llm_calls = 0

        def counting_poster(*args):
            nonlocal llm_calls
            llm_calls += 1
            return 200, json.dumps({
                "choices": [{"message": {"content": '{"skill": "scout_island", "args": {}, "why": "不应调用"}'}}],
            })

        llm = LLMClient(key="dummy", fast_proxy="", poster=counting_poster, log=None)
        planner = CampaignPlanner(
            mode="campaign",
            use_llm=True,
            llm=llm,
            min_verified="offline",
            registry=reg,
            ledger=ledger,
            target_goal="g_seed",
        )
        state = _make_state()

        # 先走两次失败触发止损
        planner.rules.ep_goal_fails["g_seed"] = 2
        with patch("mcagent.loop.policy.skill_choice.choose", wraps=skill_choice.choose) as choose_spy:
            dec = planner.select(state, [])

        self.assertEqual(llm_calls, 0)
        choose_spy.assert_not_called()
        self.assertIsNone(dec.skill)
        self.assertEqual(dec.source, "idle")
        self.assertTrue(dec.stop_episode)
        self.assertIn("g_seed", dec.reason)
        self.assertIn("止损", dec.reason)

    def test_campaign_planner_llm_does_not_short_circuit_when_env_blocked(self):
        ledger = (
            GoalSpec(
                id="g_seed",
                title="种地",
                tier="T1",
                score=5,
                skill="plant_seed",
                done_advancements=("minecraft:husbandry/plant_seed",),
                budget_s=90.0,
                max_fails_per_ep=2,
                stop_loss=True,
                stage="graduated",
            ),
        )
        reg = {
            "plant_seed": _PlantSeedStub,
            "scout_island": ScoutIsland,
        }
        llm = _make_dummy_llm("scout_island", {"radius": 32})
        planner = CampaignPlanner(
            mode="campaign",
            use_llm=True,
            llm=llm,
            min_verified="offline",
            registry=reg,
            ledger=ledger,
            target_goal="g_seed",
        )
        state = _make_state()
        planner.record_environment_block(
            _PlantSeedStub(),
            EnvBlocked("暂无耕地", missing_predicate="farmland", searched_volume="半径 32", wake_on="chunk_moved"),
            state,
        )
        dec = planner.select(state, [])
        self.assertEqual(dec.source, "llm")
        self.assertEqual(dec.skill.name, "scout_island")
        self.assertTrue(dec.environment_recovery)
        self.assertFalse(dec.stop_episode)

    def test_campaign_planner_safe_mode_precedes_stop_loss_short_circuit(self):
        ledger = (
            GoalSpec(
                id="g_seed",
                title="种地",
                tier="T1",
                score=5,
                skill="plant_seed",
                done_advancements=("minecraft:husbandry/plant_seed",),
                budget_s=90.0,
                max_fails_per_ep=2,
                stop_loss=True,
                stage="graduated",
            ),
        )
        reg = {"plant_seed": _PlantSeedStub, "scout_island": ScoutIsland}
        llm = _make_dummy_llm("scout_island", {})
        planner = CampaignPlanner(
            mode="campaign",
            use_llm=True,
            llm=llm,
            min_verified="offline",
            registry=reg,
            ledger=ledger,
            target_goal="g_seed",
        )
        state = _make_state()
        state.set_status({"ready": True, "alive": True, "pos": [0.5, 64.0, 0.5], "last_death": {"t": 1700000000000, "pos": [0, 64, 0], "cause": "Zombie"}})
        state.set_status({"ready": True, "alive": True, "pos": [0.5, 64.0, 0.5], "last_death": {"t": 1700000005000, "pos": [0, 64, 0], "cause": "Zombie"}})
        self.assertTrue(state.in_safe_mode)
        planner.rules.ep_goal_fails["g_seed"] = 2

        dec = planner.select(state, [])
        self.assertIsNone(dec.skill)
        self.assertEqual(dec.source, "idle")
        self.assertIn("safe_mode", dec.reason)
        self.assertFalse(dec.stop_episode)


class WoodStoneSaturationMenuTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        self.addCleanup(skill_base._COOLDOWN.clear)
        self.addCleanup(reset_world_memo)

    def _run_llm_select(
        self,
        *,
        state: State,
        target_goal: str | None,
        ledger: tuple[GoalSpec, ...],
        registry: dict[str, type[Skill]],
        reply_skill: str = "scout_island",
    ) -> tuple[PlanDecision, dict[str, type[Skill]]]:
        llm = _make_dummy_llm(reply_skill, {})
        planner = CampaignPlanner(
            mode="campaign",
            use_llm=True,
            llm=llm,
            min_verified="offline",
            registry=registry,
            ledger=ledger,
            target_goal=target_goal,
        )
        with patch("mcagent.loop.policy.skill_choice.choose", wraps=skill_choice.choose) as spy:
            dec = planner.select(state, [])
        return dec, dict(spy.call_args.kwargs["registry"])

    def test_wood_saturation_filters_gather_wood_when_top_step_does_not_want_wood(self):
        ledger = (
            replace(GOAL_MAP["prep_wood"], stage="graduated"),
            replace(GOAL_MAP["plant_seed"], stage="graduated"),
        )
        reg = {
            "plant_seed": _PlantSeedStub,
            "gather_wood": GatherWood,
            "mine_stone": MineStone,
            "scout_island": ScoutIsland,
        }
        # 16 原木 = 64 木板当量（刚好达到 64 上限），不缺木 -> 剔除 gather_wood
        saturated = _make_state([
            {"name": "oak_log", "count": 10, "slot": 0},
            {"name": "birch_planks", "count": 24, "slot": 1},
            {"name": "wooden_pickaxe", "count": 1, "slot": 2},
        ])
        _, seen_reg = self._run_llm_select(
            state=saturated,
            target_goal="plant_seed",
            ledger=ledger,
            registry=reg,
            reply_skill="plant_seed",
        )
        self.assertNotIn("gather_wood", seen_reg)
        self.assertIn("plant_seed", seen_reg)
        self.assertIn("mine_stone", seen_reg)

        # 63 木板当量（低于阈值） -> 保留 gather_wood
        below = _make_state([
            {"name": "oak_log", "count": 15, "slot": 0},
            {"name": "oak_planks", "count": 3, "slot": 1},
            {"name": "wooden_pickaxe", "count": 1, "slot": 2},
        ])
        _, seen_below = self._run_llm_select(
            state=below,
            target_goal="plant_seed",
            ledger=ledger,
            registry=reg,
            reply_skill="plant_seed",
        )
        self.assertIn("gather_wood", seen_below)

    def test_stone_saturation_filters_mine_stone_when_top_step_does_not_want_stone(self):
        ledger = (
            GoalSpec(
                id="deflect_arrow",
                title="抱歉，今天不行",
                tier="T1",
                score=15,
                skill="deflect_skeleton_arrow",
                done_advancements=("minecraft:story/deflect_arrow",),
                stage="graduated",
            ),
        )
        reg = {
            "deflect_skeleton_arrow": _DeflectArrowStub,
            "gather_wood": GatherWood,
            "mine_stone": MineStone,
            "scout_island": ScoutIsland,
        }
        # 48 圆石（含深板岩圆石）达到阈值，且不需要采石 -> 剔除 mine_stone
        saturated = _make_state([
            {"name": "wooden_pickaxe", "count": 1, "slot": 0},
            {"name": "cobblestone", "count": 30, "slot": 1},
            {"name": "cobbled_deepslate", "count": 18, "slot": 2},
        ])
        _, seen_reg = self._run_llm_select(
            state=saturated,
            target_goal="deflect_arrow",
            ledger=ledger,
            registry=reg,
            reply_skill="deflect_skeleton_arrow",
        )
        self.assertNotIn("mine_stone", seen_reg)
        self.assertIn("gather_wood", seen_reg)
        self.assertIn("deflect_skeleton_arrow", seen_reg)

        # 47 圆石（低于阈值） -> 保留 mine_stone
        below = _make_state([
            {"name": "wooden_pickaxe", "count": 1, "slot": 0},
            {"name": "cobblestone", "count": 47, "slot": 1},
        ])
        _, seen_below = self._run_llm_select(
            state=below,
            target_goal="deflect_arrow",
            ledger=ledger,
            registry=reg,
            reply_skill="deflect_skeleton_arrow",
        )
        self.assertIn("mine_stone", seen_below)

    def test_keeps_wood_or_stone_when_top_step_or_unmet_fallback_wants_it(self):
        # 1) 顶层技能直接就是 gather_wood / mine_stone 时即使超限也保留
        wood_goal = (
            GoalSpec(id="need_more_wood", title="就要砍树", tier="T1", score=5, skill="gather_wood", stage="graduated"),
        )
        stone_goal = (
            GoalSpec(id="need_more_stone", title="就要挖石", tier="T1", score=5, skill="mine_stone", stage="graduated"),
        )
        rich_state = _make_state([
            {"name": "wooden_pickaxe", "count": 1, "slot": 0},
            {"name": "oak_log", "count": 25, "slot": 1},
            {"name": "cobblestone", "count": 64, "slot": 2},
        ])
        reg = {
            "gather_wood": GatherWood,
            "mine_stone": MineStone,
            "scout_island": ScoutIsland,
        }
        _, seen_wood_goal = self._run_llm_select(
            state=rich_state,
            target_goal="need_more_wood",
            ledger=wood_goal,
            registry=reg,
            reply_skill="gather_wood",
        )
        self.assertIn("gather_wood", seen_wood_goal)
        self.assertNotIn("mine_stone", seen_wood_goal)

        _, seen_stone_goal = self._run_llm_select(
            state=rich_state,
            target_goal="need_more_stone",
            ledger=stone_goal,
            registry=reg,
            reply_skill="mine_stone",
        )
        self.assertIn("mine_stone", seen_stone_goal)
        self.assertNotIn("gather_wood", seen_stone_goal)

        # 2) gather_fallbacks 中仍有未满足表达需要 gather_wood / mine_stone 时也保留
        fb_ledger = (
            replace(GOAL_MAP["iron_tools"], prereq_goals=(), stage="graduated"),
        )
        fb_reg = {
            "craft_iron_gear": _CraftIronGearStub,
            "gather_wood": GatherWood,
            "mine_stone": MineStone,
            "scout_island": ScoutIsland,
        }
        need_stick_state = _make_state([
            {"name": "iron_ingot", "count": 3, "slot": 0},
            {"name": "oak_log", "count": 20, "slot": 1},
            {"name": "cobblestone", "count": 64, "slot": 2},
            {"name": "wooden_pickaxe", "count": 1, "slot": 3},
        ])
        _, seen_fb_need_stick = self._run_llm_select(
            state=need_stick_state,
            target_goal="iron_tools",
            ledger=fb_ledger,
            registry=fb_reg,
            reply_skill="gather_wood",
        )
        self.assertIn("gather_wood", seen_fb_need_stick)
        self.assertNotIn("mine_stone", seen_fb_need_stick)

        have_stick_state = _make_state([
            {"name": "iron_ingot", "count": 3, "slot": 0},
            {"name": "stick", "count": 2, "slot": 1},
            {"name": "oak_log", "count": 20, "slot": 2},
            {"name": "cobblestone", "count": 64, "slot": 3},
            {"name": "wooden_pickaxe", "count": 1, "slot": 4},
        ])
        _, seen_fb_satisfied = self._run_llm_select(
            state=have_stick_state,
            target_goal="iron_tools",
            ledger=fb_ledger,
            registry=fb_reg,
            reply_skill="craft_iron_gear",
        )
        self.assertNotIn("gather_wood", seen_fb_satisfied)
        self.assertNotIn("mine_stone", seen_fb_satisfied)

        # 3) gather_fallbacks 中仍有未满足圆石表达式需要 mine_stone 时即使 >= 48 也保留
        stone_fb_ledger = (
            GoalSpec(
                id="need_64_cobble",
                title="需要更多圆石前置",
                tier="T1",
                score=10,
                skill="deflect_skeleton_arrow",
                prereq_items=("cobblestone>=64",),
                gather_fallbacks=(("cobblestone>=64", "mine_stone", (("count", 16),)),),
                stage="graduated",
            ),
        )
        stone_fb_reg = {
            "deflect_skeleton_arrow": _DeflectArrowStub,
            "gather_wood": GatherWood,
            "mine_stone": MineStone,
            "scout_island": ScoutIsland,
        }
        need_more_cobble_state = _make_state([
            {"name": "oak_log", "count": 20, "slot": 0},
            {"name": "cobblestone", "count": 48, "slot": 1},
            {"name": "wooden_pickaxe", "count": 1, "slot": 2},
        ])
        _, seen_stone_fb = self._run_llm_select(
            state=need_more_cobble_state,
            target_goal="need_64_cobble",
            ledger=stone_fb_ledger,
            registry=stone_fb_reg,
            reply_skill="mine_stone",
        )
        self.assertIn("mine_stone", seen_stone_fb)
        self.assertNotIn("gather_wood", seen_stone_fb)

    def test_no_filtering_when_target_goal_is_none(self):
        # 对应 test_soak_gather_wood_inc_001.py 场景：oak_log=80 且 target_goal=None，不应剔除菜单
        state = _make_state([
            {"name": "oak_log", "count": 80, "slot": 0},
            {"name": "cobblestone", "count": 160, "slot": 1},
            {"name": "wooden_pickaxe", "count": 1, "slot": 2},
        ])
        reg = {
            "gather_wood": GatherWood,
            "mine_stone": MineStone,
            "scout_island": ScoutIsland,
        }
        _, seen = self._run_llm_select(
            state=state,
            target_goal=None,
            ledger=(replace(GOAL_MAP["prep_wood"], stage="graduated"),),
            registry=reg,
            reply_skill="scout_island",
        )
        self.assertIn("gather_wood", seen)
        self.assertIn("mine_stone", seen)

    def test_saturation_filter_never_empties_last_ready_skill(self):
        ledger = (
            GoalSpec(
                id="deflect_arrow",
                title="盾反",
                tier="T1",
                score=15,
                skill="deflect_skeleton_arrow",
                done_advancements=("minecraft:story/deflect_arrow",),
                stage="graduated",
            ),
        )
        # 注册表里只有 gather_wood 与被阻塞的 mine_stone（无镐 -> blocked）
        state = _make_state([{"name": "oak_log", "count": 25, "slot": 0}])
        reg = {
            "gather_wood": GatherWood,
            "mine_stone": MineStone,
        }
        _, seen = self._run_llm_select(
            state=state,
            target_goal="deflect_arrow",
            ledger=ledger,
            registry=reg,
            reply_skill="gather_wood",
        )
        self.assertIn("gather_wood", seen)
        self.assertTrue(skill_choice.has_ready_skill(state, seen, "offline"))

        # 当 gather_wood 与 mine_stone 均饱和且注册表中无其他可用技能时，至少保留一个可用技能
        both_state = _make_state([
            {"name": "oak_log", "count": 25, "slot": 0},
            {"name": "cobblestone", "count": 64, "slot": 1},
            {"name": "wooden_pickaxe", "count": 1, "slot": 2},
        ])
        _, seen_both = self._run_llm_select(
            state=both_state,
            target_goal="deflect_arrow",
            ledger=ledger,
            registry=reg,
            reply_skill="mine_stone",
        )
        self.assertTrue(skill_choice.has_ready_skill(both_state, seen_both, "offline"))
        self.assertTrue("gather_wood" in seen_both or "mine_stone" in seen_both)


class CraftGearDeduplicationTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        self.addCleanup(skill_base._COOLDOWN.clear)
        self.addCleanup(reset_world_memo)

    def test_blocks_duplicate_carried_gear(self):
        base_materials = [
            {"name": "oak_planks", "count": 16, "slot": 0},
            {"name": "stick", "count": 8, "slot": 1},
            {"name": "cobblestone", "count": 16, "slot": 2},
            {"name": "crafting_table", "count": 1, "slot": 3},
        ]
        for item in ("wooden_pickaxe", "stone_pickaxe", "stone_sword", "furnace"):
            with self.subTest(item=item):
                empty_state = _make_state(base_materials)
                self.assertTrue(make("craft_gear", {"item": item}).can_start(empty_state))

                carried_state = _make_state(base_materials + [{"name": item, "count": 1, "slot": 4}])
                self.assertFalse(make("craft_gear", {"item": item}).can_start(carried_state))

    def test_blocks_wooden_pickaxe_when_any_better_or_other_pickaxe_carried(self):
        base_materials = [
            {"name": "oak_planks", "count": 12, "slot": 0},
            {"name": "stick", "count": 4, "slot": 1},
            {"name": "cobblestone", "count": 6, "slot": 2},
            {"name": "crafting_table", "count": 1, "slot": 3},
        ]
        for pick in ("stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "golden_pickaxe"):
            with self.subTest(pick=pick):
                state = _make_state(base_materials + [{"name": pick, "count": 1, "slot": 4}])
                self.assertFalse(make("craft_gear", {"item": "wooden_pickaxe"}).can_start(state))
                ok, errs = skill_choice.validate_choice(
                    {"skill": "craft_gear", "args": {"item": "wooden_pickaxe"}, "why": "重复木镐"},
                    state,
                    registry={"craft_gear": CraftGear},
                    min_verified="offline",
                )
                self.assertFalse(ok)
                self.assertTrue(errs)

        # 携带木镐或金镐时，升级石镐仍允许启动
        wood_state = _make_state(base_materials + [{"name": "wooden_pickaxe", "count": 1, "slot": 4}])
        self.assertTrue(make("craft_gear", {"item": "stone_pickaxe"}).can_start(wood_state))
        gold_state = _make_state(base_materials + [{"name": "golden_pickaxe", "count": 1, "slot": 4}])
        self.assertTrue(make("craft_gear", {"item": "stone_pickaxe"}).can_start(gold_state))

    def test_still_requires_affordable_materials(self):
        poor_state = _make_state([{"name": "oak_planks", "count": 2, "slot": 0}])
        self.assertFalse(make("craft_gear", {"item": "wooden_pickaxe"}).can_start(poor_state))


if __name__ == "__main__":
    unittest.main()
