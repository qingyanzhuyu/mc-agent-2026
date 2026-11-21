"""全目标池 CLI 解锁、核心枢纽可恢复挂起/唤醒与防 scout_island 空转单测（verified: offline）。"""
from __future__ import annotations

import io
import json
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from mcagent.loop import (
    CampaignPlanner,
    GOALS,
    GoalSpec,
    RulePlanner,
    __main__ as loop_main,
    reset_world_memo,
)
from mcagent.loop.context import MAX_CONTEXT_CHARS, build_decision_context
from mcagent.loop.goals import (
    DEFAULT_CAMPAIGN_STAGES,
    GOAL_MAP,
    GOAL_STAGES,
    compute_downstream_counts,
    compute_hub_goal_ids,
)
from mcagent.loop.starter_skills import ScoutIsland
from mcagent.planner.llm import LLMClient
from mcagent.planner.objective import resolve_objective
from mcagent.runtime import EnvBlocked, State
from mcagent.skills import Param, REGISTRY, Skill
from mcagent.skills import base as skill_base


class _FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = float(initial)

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    def __call__(self) -> float:
        return self.now


def _make_state(
    items: list[dict] | None = None,
    *,
    pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
    dimension: str = "overworld",
    is_day: bool = True,
) -> State:
    st = State()
    st.set_status({
        "ready": True,
        "alive": True,
        "health": 20.0,
        "food": 20.0,
        "dimension": dimension,
        "is_day": is_day,
        "pos": list(pos),
    })
    st.set_inventory({"items": items or []})
    return st


def _starter_done_state(pos: tuple[float, float, float] = (0.5, 64.0, 0.5)) -> State:
    """起步 6 项（木/木镐/圆石/石镐/石剑/熔炉）全部就绪、只差铁锭的状态。"""
    return _make_state(
        items=[
            {"name": "oak_log", "count": 4, "slot": 0},
            {"name": "oak_planks", "count": 8, "slot": 1},
            {"name": "stick", "count": 4, "slot": 2},
            {"name": "stone_pickaxe", "count": 1, "slot": 3},
            {"name": "stone_sword", "count": 1, "slot": 4},
            {"name": "furnace", "count": 1, "slot": 5},
            {"name": "cobblestone", "count": 20, "slot": 6},
        ],
        pos=pos,
    )


class DraftPoolAndContextTests(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        self.addCleanup(skill_base._COOLDOWN.clear)
        self.addCleanup(reset_world_memo)

    def test_ledger_wip1_discipline_and_totals_preserved(self) -> None:
        # 不改 goals.py 既有 stage 标记，严格保持 WIP=1（唯一 candidate 为 smelt_iron）
        self.assertEqual(len(GOALS), 104)
        self.assertEqual(sum(1 for g in GOALS if g.score > 0), 89)
        self.assertEqual(sum(g.score for g in GOALS), 2730)
        self.assertEqual([g.id for g in GOALS if g.stage == "candidate"], ["smelt_iron"])

    def test_cli_include_draft_and_goal_stages_parsing(self) -> None:
        parser = loop_main.build_parser()

        # 默认未传开关 -> None（沿用保守默认门禁，仅 7 项）
        args_default = parser.parse_args([])
        self.assertFalse(args_default.include_draft)
        self.assertIsNone(args_default.goal_stages)
        self.assertIsNone(loop_main.resolve_cli_include_stages(args_default))
        self.assertEqual(len(RulePlanner(min_verified="offline").active_goals), 7)

        # --include-draft -> 全 4 阶段 GOAL_STAGES（104 项）
        args_draft = parser.parse_args(["--include-draft"])
        self.assertEqual(loop_main.resolve_cli_include_stages(args_draft), GOAL_STAGES)
        self.assertEqual(len(RulePlanner(min_verified="offline", include_stages=GOAL_STAGES).active_goals), 104)

        # --goal-stages 合法逗号枚举 + 去重；与 --include-draft 合并
        args_custom = parser.parse_args(["--goal-stages", "candidate, graduated, candidate"])
        self.assertEqual(loop_main.resolve_cli_include_stages(args_custom), ("candidate", "graduated"))
        args_merged = parser.parse_args(["--goal-stages", "graduated", "--include-draft"])
        self.assertEqual(loop_main.resolve_cli_include_stages(args_merged), ("draft", "graduated"))

        # 非法 --goal-stages 返回退出码 2
        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            rc_bad = loop_main.main(["--dry", "--goal-stages", "draft,unknown_stage"])
        self.assertEqual(rc_bad, 2)
        self.assertIn("非法 --goal-stages", err_buf.getvalue())

    def test_dry_cli_with_include_draft_activates_104_goals_and_context_under_2000_chars(self) -> None:
        out_default = io.StringIO()
        with patch.object(loop_main.Client, "probe", return_value="unauthorized"), redirect_stdout(out_default):
            rc_def = loop_main.main(["--dry", "--strategy", "llm", "--min-verified", "offline"])
        self.assertEqual(rc_def, 0)
        self.assertIn("已激活 7 项", out_default.getvalue())

        out_draft = io.StringIO()
        with patch.object(loop_main.Client, "probe", return_value="unauthorized"), redirect_stdout(out_draft):
            rc_draft = loop_main.main(["--dry", "--strategy", "llm", "--min-verified", "offline", "--include-draft"])
        self.assertEqual(rc_draft, 0)
        txt = out_draft.getvalue()
        self.assertIn("共 104 项（已激活 104 项）", txt)
        self.assertIn("strategy=llm", txt)

        # 构造带真实坐标的满台账上下文，验证严格 <= 2000 字、零裸坐标、按边际 ROI 截断 Top-K 且保留挂起区
        st = _starter_done_state(pos=(-128.75, 64.0, 419.25))
        cp = CampaignPlanner(mode="campaign", strategy="llm", min_verified="offline", include_stages=GOAL_STAGES)
        cp.rules.completed_advancements.update({"minecraft:story/mine_stone", "minecraft:story/upgrade_tools"})
        cp.abandon_goal("smelt_iron", state=st)
        ctx = cp.build_context(st)
        self.assertGreaterEqual(len(ctx.eligible_goals), 15)
        self.assertTrue(any(s.goal_id == "smelt_iron" for s in ctx.suspended_goals))

        rendered = ctx.render()
        self.assertLessEqual(len(rendered), MAX_CONTEXT_CHARS)
        self.assertIn("按边际 ROI 展示 Top-", rendered)
        self.assertIn("被挂起目标", rendered)
        self.assertIn("smelt_iron", rendered)
        self.assertNotIn("-128.75", rendered)
        self.assertNotIn("419.25", rendered)
        self.assertIsNone(re.search(r"\[\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+", rendered))

    def test_live_plus_include_draft_warns_and_keeps_min_verified_live(self) -> None:
        captured: dict[str, object] = {}

        class _FakeLoop:
            def __init__(self, _c, _s, _e, planner, **kwargs):
                captured["min_verified"] = planner.min_verified
                captured["include_stages"] = planner.rules.include_stages

            def run(self):
                return type("_Res", (), {"stop_reason": "max_steps_reached"})()

        from mcagent.client import Config

        fake_cfg = Config(
            base="https://mc.official.example.com/api/v1",
            username="bot_live",
            password="pwd",
            token_file=None,
        )

        err_buf = io.StringIO()
        out_buf = io.StringIO()
        with (
            patch.object(loop_main.Config, "load", return_value=fake_cfg),
            patch.object(loop_main, "OFFICIAL_HOSTS", frozenset({"mc.official.example.com"})),
            patch.object(loop_main, "MainLoop", _FakeLoop),
            redirect_stderr(err_buf),
            redirect_stdout(out_buf),
        ):
            rc = loop_main.main(["--live", "--include-draft", "--max-steps", "1"])

        self.assertEqual(rc, 0)
        self.assertEqual(captured["min_verified"], "live")
        self.assertEqual(captured["include_stages"], GOAL_STAGES)
        self.assertIn("风险提示", err_buf.getvalue())
        self.assertIn("--min-verified=live", err_buf.getvalue())


class HubRecoveryAndWakeupTests(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        self.addCleanup(skill_base._COOLDOWN.clear)
        self.addCleanup(reset_world_memo)

    def test_dag_auto_detects_hub_goals_without_hardcoding(self) -> None:
        counts = compute_downstream_counts(GOALS)
        hubs = compute_hub_goal_ids(GOALS)
        for expected_hub in (
            "smelt_iron",
            "iron_tools",
            "mine_diamond",
            "prep_diamond_pickaxe",
            "form_obsidian",
            "enter_the_nether",
            "find_fortress",
            "enter_the_end",
        ):
            self.assertIn(expected_hub, hubs, msg=f"{expected_hub} 下游数={counts.get(expected_hub)}")
            self.assertGreaterEqual(counts[expected_hub], 5)

        for leaf in ("plant_seed", "sleep_in_bed", "breed_an_animal", "lava_bucket"):
            self.assertNotIn(leaf, hubs)
            self.assertLess(counts[leaf], 5)

    def test_hub_suspension_switches_immediately_to_zero_iron_parallel_goals(self) -> None:
        clock = _FakeClock(1000.0)
        st = _starter_done_state(pos=(0.5, 64.0, 0.5))
        cp = CampaignPlanner(
            mode="campaign",
            strategy="rule",
            min_verified="offline",
            include_stages=GOAL_STAGES,
            clock=clock,
        )
        cp.rules.completed_advancements.update({"minecraft:story/mine_stone", "minecraft:story/upgrade_tools"})

        # 触发 smelt_iron 达到 max_fails_per_ep（4 次）
        for _ in range(GOAL_MAP["smelt_iron"].max_fails_per_ep):
            cp.rules._last_selected_goal_id = "smelt_iron"
            cp.record_skill_outcome("smelt_iron", ok=False, elapsed_s=15.0)

        self.assertIn("smelt_iron", cp.rules._hub_suspensions)
        dec = cp.select(st, [])
        self.assertIsNotNone(dec.skill)
        self.assertNotEqual(dec.skill.name, "scout_island")
        self.assertNotEqual(dec.skill.name, "smelt_iron")
        self.assertIsNotNone(cp.current_objective)
        self.assertNotEqual(cp.current_objective.goal_id, "smelt_iron")

        # 校验 LLM 目标解析也拒绝当前挂起中的 smelt_iron，同时允许零铁平行目标（如 plant_seed）
        resolved_hub, errs_hub = resolve_objective(
            {"goal_id": "smelt_iron", "why": "强选挂起枢纽"},
            st,
            include_stages=GOAL_STAGES,
            completed_advs=cp.rules.completed_advancements,
            ep_goal_fails=cp.rules.ep_goal_fails,
            hub_suspensions=cp.rules._hub_suspensions,
            abandoned_goals=cp.rules._abandoned_goals,
        )
        self.assertIsNone(resolved_hub)
        self.assertTrue(any("枢纽临时挂起" in e for e in errs_hub))

        resolved_parallel, errs_par = resolve_objective(
            {"goal_id": "plant_seed", "why": "零铁平行得分"},
            st,
            include_stages=GOAL_STAGES,
            completed_advs=cp.rules.completed_advancements,
            ep_goal_fails=cp.rules.ep_goal_fails,
            hub_suspensions=cp.rules._hub_suspensions,
            abandoned_goals=cp.rules._abandoned_goals,
        )
        self.assertIsNotNone(resolved_parallel, msg=str(errs_par))
        self.assertEqual(resolved_parallel.goal_id, "plant_seed")

    def test_hub_wakes_on_32_block_displacement_and_clears_failures(self) -> None:
        clock = _FakeClock(2000.0)
        st = _starter_done_state(pos=(0.5, 64.0, 0.5))
        rp = RulePlanner(min_verified="offline", include_stages=GOAL_STAGES, clock=clock)
        rp.completed_advancements.update({"minecraft:story/mine_stone", "minecraft:story/upgrade_tools"})
        rp.refresh_environment_blocks(st)

        # 通过 abandon_goal 触发枢纽挂起
        self.assertTrue(rp.abandon_goal("smelt_iron"))
        self.assertIn("smelt_iron", rp._hub_suspensions)
        self.assertIn("smelt_iron", rp._abandoned_goals)
        self.assertGreaterEqual(rp.ep_goal_fails["smelt_iron"], 4)

        # 位移不足 32 格（20 格）且冷却未满 -> 仍挂起
        st_near = _starter_done_state(pos=(20.5, 64.0, 0.5))
        clock.advance(60.0)
        self.assertEqual(rp.refresh_environment_blocks(st_near), [])
        self.assertIn("smelt_iron", rp._hub_suspensions)

        # 位移 >= 32 格（33 格） -> 自动唤醒、清零 ep_goal_fails 与 _abandoned_goals、重入规划队列
        st_far = _starter_done_state(pos=(33.5, 64.0, 0.5))
        woken = rp.refresh_environment_blocks(st_far)
        self.assertEqual(woken, ["smelt_iron"])
        self.assertNotIn("smelt_iron", rp._hub_suspensions)
        self.assertNotIn("smelt_iron", rp._abandoned_goals)
        self.assertEqual(rp.ep_goal_fails["smelt_iron"], 0)
        self.assertEqual(rp._hub_restart_counts["smelt_iron"], 1)
        ranked_step_ids = [s.step_goal.id for s in rp._rank(st_far)]
        self.assertIn("smelt_iron", ranked_step_ids)

    def test_hub_wakes_on_cooldown_and_escalates_180_to_300_with_restart_cap(self) -> None:
        clock = _FakeClock(5000.0)
        st = _starter_done_state(pos=(10.0, 64.0, 10.0))
        rp = RulePlanner(
            min_verified="offline",
            include_stages=GOAL_STAGES,
            hub_cooldown_s=180.0,
            hub_max_restarts=2,
            clock=clock,
        )
        rp.completed_advancements.update({"minecraft:story/mine_stone", "minecraft:story/upgrade_tools"})
        rp.refresh_environment_blocks(st)

        # 第 1 次挂起：默认 180s
        rp.abandon_goal("smelt_iron", state=st)
        self.assertEqual(rp._hub_suspensions["smelt_iron"].cooldown_s, 180.0)
        clock.advance(179.0)
        self.assertEqual(rp.refresh_environment_blocks(st), [])
        self.assertIn("smelt_iron", rp._hub_suspensions)

        clock.advance(1.0)
        self.assertEqual(rp.refresh_environment_blocks(st), ["smelt_iron"])
        self.assertEqual(rp.ep_goal_fails["smelt_iron"], 0)

        # 第 2 次挂起：递增至 300s
        rp.abandon_goal("smelt_iron", state=st)
        self.assertEqual(rp._hub_suspensions["smelt_iron"].cooldown_s, 300.0)
        clock.advance(299.0)
        self.assertEqual(rp.refresh_environment_blocks(st), [])
        clock.advance(1.0)
        self.assertEqual(rp.refresh_environment_blocks(st), ["smelt_iron"])
        self.assertEqual(rp._hub_restart_counts["smelt_iron"], 2)

        # 第 3 次触达 hub_max_restarts=2 -> 停止继续唤醒，防单点死循环
        rp.abandon_goal("smelt_iron", state=st)
        self.assertNotIn("smelt_iron", rp._hub_suspensions)
        self.assertIn("smelt_iron", rp._abandoned_goals)
        clock.advance(999.0)
        self.assertEqual(rp.refresh_environment_blocks(_starter_done_state(pos=(500.0, 64.0, 500.0))), [])
        self.assertIn("smelt_iron", rp._abandoned_goals)

    def test_non_hub_leaf_goal_keeps_permanent_stop_loss_behavior(self) -> None:
        clock = _FakeClock(3000.0)
        st = _starter_done_state(pos=(0.5, 64.0, 0.5))
        rp = RulePlanner(min_verified="offline", include_stages=GOAL_STAGES, clock=clock)
        rp.completed_advancements.update({"minecraft:story/mine_stone", "minecraft:story/upgrade_tools"})

        self.assertFalse(rp.is_hub_goal("plant_seed"))
        rp.abandon_goal("plant_seed", state=st)
        self.assertNotIn("plant_seed", rp._hub_suspensions)
        self.assertIn("plant_seed", rp._abandoned_goals)
        self.assertGreaterEqual(rp.ep_goal_fails["plant_seed"], GOAL_MAP["plant_seed"].max_fails_per_ep)

        # 即使位移 100 格且经过 600s，非枢纽叶目标仍保持本轮止损不恢复
        clock.advance(600.0)
        st_far = _starter_done_state(pos=(100.5, 64.0, 0.5))
        self.assertEqual(rp.refresh_environment_blocks(st_far), [])
        self.assertIn("plant_seed", rp._abandoned_goals)
        self.assertNotIn("plant_seed", [s.root_goal.id for s in rp._rank(st_far)])


class SurveyFallbackUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        self.addCleanup(skill_base._COOLDOWN.clear)
        self.addCleanup(reset_world_memo)

    def test_alternates_survey_island_before_scout_island_when_all_goals_suspended(self) -> None:
        clock = _FakeClock(4000.0)
        st = _starter_done_state(pos=(0.5, 64.0, 0.5))
        # 已有营地箱与足量熟食，避免主动生存层（冷启动建箱/补粮）抢占兜底探索验证
        st.set_inventory({
            "items": [
                *list(st.inventory.get("items") or []),
                {"name": "cooked_beef", "count": 4, "slot": 7},
            ]
        })
        from mcagent.skills.world_memo import memo_for
        memo_for(st).set_camp([0, 64, 0], chest=[1, 64, 0], stored_items={})
        # 仅保留默认 7 项且全部完成/挂起（前 6 项达成 + smelt_iron 临时挂起），触发兜底探索
        rp = RulePlanner(min_verified="offline", include_stages=DEFAULT_CAMPAIGN_STAGES, clock=clock)
        rp.completed_advancements.update({"minecraft:story/mine_stone", "minecraft:story/upgrade_tools"})
        rp.abandon_goal("smelt_iron", state=st)

        # 第 1 拍兜底：优先派发 survey_island(sectors=2, radius=32)
        d1 = rp.choose(st)
        self.assertIsNotNone(d1.skill)
        self.assertEqual(d1.skill.name, "survey_island")
        self.assertEqual(d1.skill.args["sectors"], 2)
        self.assertEqual(d1.skill.args["radius"], 32)

        # 第 2 拍兜底：交替派发 scout_island
        d2 = rp.choose(st)
        self.assertIsNotNone(d2.skill)
        self.assertEqual(d2.skill.name, "scout_island")

        # 第 3 拍兜底：再次优先 survey_island
        d3 = rp.choose(st)
        self.assertIsNotNone(d3.skill)
        self.assertEqual(d3.skill.name, "survey_island")

    def test_survey_fallback_respects_min_verified_empty_bag_and_narrow_registry(self) -> None:
        # 1) min_verified="live" 时不选用 verified="offline" 的 survey_island
        class _LiveScout(Skill):
            name = "scout_island"
            description = "live 探索桩"
            params = {"radius": Param(int, "半径", default=36)}
            goal = "explore:island"
            verified = "live"

            def run(self, ex, state):
                return iter(())

        reg_live = {"survey_island": REGISTRY["survey_island"], "scout_island": _LiveScout}
        st = _starter_done_state()
        rp_live = RulePlanner(registry=reg_live, ledger=(), min_verified="live")
        d_live = rp_live.choose(st)
        self.assertIsNotNone(d_live.skill)
        self.assertEqual(d_live.skill.name, "scout_island")

        # 2) 空包且无地标时 survey_island.blocked(state) 非空 -> 平稳回退到 scout_island
        empty_st = _make_state(items=[])
        rp_empty = RulePlanner(
            registry={"survey_island": REGISTRY["survey_island"], "scout_island": ScoutIsland},
            ledger=(),
            min_verified="offline",
        )
        d_empty = rp_empty.choose(empty_st)
        self.assertIsNotNone(d_empty.skill)
        self.assertEqual(d_empty.skill.name, "scout_island")

        # 3) 窄测试注册表未注册 survey_island -> 平稳回退到 scout_island
        rp_narrow = RulePlanner(registry={"scout_island": ScoutIsland}, ledger=(), min_verified="offline")
        d_narrow = rp_narrow.choose(st)
        self.assertIsNotNone(d_narrow.skill)
        self.assertEqual(d_narrow.skill.name, "scout_island")

    def test_strategy_orchestrator_hub_suspension_and_wakeup_end_to_end(self) -> None:
        clock = _FakeClock(6000.0)
        st = _starter_done_state(pos=(0.5, 64.0, 0.5))
        cp = CampaignPlanner(
            mode="campaign",
            strategy="rule",
            min_verified="offline",
            include_stages=GOAL_STAGES,
            clock=clock,
        )
        cp.rules.completed_advancements.update({"minecraft:story/mine_stone", "minecraft:story/upgrade_tools"})

        # 让 smelt_iron 成为当前目标并连败达到 max_fails_per_ep，触发战略层切去零铁平行目标
        from mcagent.planner.objective import Objective

        cp.set_objective(Objective(goal_id="smelt_iron", budget_s=210.0, why="test", source="rule", started_at=clock()))
        for _ in range(GOAL_MAP["smelt_iron"].max_fails_per_ep):
            cp.record_skill_outcome("smelt_iron", ok=False, elapsed_s=10.0)

        dec_switched = cp.select(st, [])
        self.assertIsNotNone(dec_switched.skill)
        self.assertIn("smelt_iron", cp.rules._hub_suspensions)
        self.assertNotEqual(cp.current_objective.goal_id, "smelt_iron")
        self.assertEqual(cp._strategy._consecutive_goal_fails.get("smelt_iron", 0), 0)

        # 位移 >= 32 格后在下一拍 select 自动唤醒 smelt_iron 并允许重新入池
        st_moved = _starter_done_state(pos=(40.5, 64.0, 0.5))
        cp.select(st_moved, [])
        self.assertNotIn("smelt_iron", cp.rules._hub_suspensions)
        self.assertEqual(cp.rules.ep_goal_fails.get("smelt_iron", 0), 0)
        self.assertIn("smelt_iron", [s.step_goal.id for s in cp.rules._rank(st_moved)])


if __name__ == "__main__":
    unittest.main()
