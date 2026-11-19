"""Stage 2 Wave 3: Multi-Goal Shared-Subgraph Marginal ROI & Roadside Incremental Scan tests (verified: offline)."""
from __future__ import annotations

from dataclasses import replace
import unittest

from mcagent.client import FATAL, ApiError
from mcagent.loop import CampaignPlanner, GoalSpec, LoopConfig, MainLoop, reset_world_memo
from mcagent.loop.goals import GOALS, GoalPlanStep, rank_actionable_goals
from mcagent.runtime import State
from mcagent.skills import Skill, base as skill_base
from mcagent.skills.base import rec
from mcagent.skills.testing import CountingExecutor
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


class _FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


class _OneTickSkill(Skill):
    name = "roadside_probe_skill"
    description = "一步完成的测试技能，用于触发技能完成边界的沿途扫描"
    params = {}
    goal = "test:roadside"
    verified = "offline"

    def run(self, ex, state):
        yield rec("noop")


class _MockRoadsideWorld:
    """支持可控 find_blocks / get_entities 响应与时钟推进的轻量世界替身。"""

    def __init__(
        self,
        *,
        clock: _FakeClock,
        step_advance_s: float = 0.0,
        dimension: str = "overworld",
        blocks_resp=None,
        entities_resp=None,
        blocks_err: Exception | None = None,
        entities_err: Exception | None = None,
    ) -> None:
        self.clock = clock
        self.step_advance_s = step_advance_s
        self.dimension = dimension
        self.blocks_resp = blocks_resp if blocks_resp is not None else {"blocks": []}
        self.entities_resp = entities_resp if entities_resp is not None else {"entities": []}
        self.blocks_err = blocks_err
        self.entities_err = entities_err
        self.find_blocks_calls: list[dict] = []
        self.get_entities_calls: list[dict] = []

    def wait_ready(self, timeout=120):
        return self.status()

    def status(self):
        return {
            "ready": True,
            "alive": True,
            "health": 20,
            "food": 20,
            "is_day": True,
            "dimension": self.dimension,
            "pos": [10.5, 64.0, 10.5],
        }

    def inventory(self):
        return {"items": []}

    def events(self, since=0, limit=100):
        return {"events": [], "next_id": since}

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None):
        self.find_blocks_calls.append({"types": list(types or ()), "radius": radius, "limit": limit})
        if self.blocks_err is not None:
            raise self.blocks_err
        return self.blocks_resp

    def get_entities(self, radius=32, types=None):
        self.get_entities_calls.append({"radius": radius, "types": list(types or ())})
        if self.entities_err is not None:
            raise self.entities_err
        return self.entities_resp


class _NoScanClient:
    """不实现 find_blocks / get_entities 的最小客户端，验证安全降级。"""

    def wait_ready(self, timeout=120):
        return self.status()

    def status(self):
        return {
            "ready": True,
            "alive": True,
            "health": 20,
            "food": 20,
            "is_day": True,
            "dimension": "overworld",
            "pos": [0.5, 64.0, 0.5],
        }

    def inventory(self):
        return {"items": []}

    def events(self, since=0, limit=100):
        return {"events": [], "next_id": since}


class SharedSubgraphMarginalRoiTests(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        reset_all_world_memos()

    def _empty_state(self) -> State:
        st = State()
        st.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "pos": [0.5, 64.0, 0.5]})
        st.set_inventory({"items": []})
        return st

    def test_shared_prerequisite_hub_outranks_higher_base_roi_dead_end(self) -> None:
        ledger = (
            GoalSpec(
                id="dead_end_side",
                title="略高基础ROI的死胡同支线",
                tier="T2",
                score=15,
                skill="gather_wood",
                est_cost_s=100.0,
                stage="graduated",
            ),
            GoalSpec(
                id="shared_hub",
                title="共享前置枢纽（类似 smelt_iron）",
                tier="T1",
                score=5,
                skill="smelt_iron",
                est_cost_s=100.0,
                stage="graduated",
            ),
            GoalSpec(
                id="branch_a",
                title="高分下游 A",
                tier="T2",
                score=20,
                skill="craft_iron_gear",
                prereq_goals=("shared_hub",),
                est_cost_s=100.0,
                stage="graduated",
            ),
            GoalSpec(
                id="branch_b",
                title="高分下游 B",
                tier="T2",
                score=20,
                skill="fill_lava_bucket",
                prereq_goals=("shared_hub",),
                est_cost_s=100.0,
                stage="graduated",
            ),
            GoalSpec(
                id="branch_c",
                title="高分下游 C",
                tier="T2",
                score=20,
                skill="deflect_skeleton_arrow",
                prereq_goals=("shared_hub",),
                est_cost_s=100.0,
                stage="graduated",
            ),
        )
        ranked = rank_actionable_goals(self._empty_state(), ledger)
        self.assertEqual([s.step_goal.id for s in ranked], ["shared_hub", "dead_end_side"])

        hub_step, dead_end_step = ranked[0], ranked[1]
        # 原始 path_score / path_cost / roi 保持精确原始链路总计：
        # branch_a 原始 ROI = 25/200 = 0.125 < dead_end_side 原始 ROI = 15/100 = 0.15
        self.assertEqual(hub_step.path_score, 25)
        self.assertEqual(hub_step.path_cost, 200.0)
        self.assertAlmostEqual(hub_step.roi, 0.125, places=4)
        self.assertEqual(dead_end_step.path_score, 15)
        self.assertEqual(dead_end_step.path_cost, 100.0)
        self.assertAlmostEqual(dead_end_step.roi, 0.15, places=4)

        # 但共享子图分摊后 marginal_roi 严格反超 dead_end_side
        self.assertEqual(hub_step.shared_dependents, 4)
        self.assertEqual(dead_end_step.shared_dependents, 1)
        assert hub_step.marginal_roi is not None and dead_end_step.marginal_roi is not None
        self.assertGreater(hub_step.marginal_roi, hub_step.roi)
        self.assertGreater(hub_step.marginal_roi, dead_end_step.marginal_roi)
        self.assertAlmostEqual(dead_end_step.marginal_roi, dead_end_step.roi, places=4)

    def test_disjoint_goals_preserve_exact_base_roi_ordering(self) -> None:
        ledger = (
            GoalSpec(id="solo_low", title="低分独立", tier="T1", score=5, skill="gather_wood", est_cost_s=50.0, stage="graduated"),
            GoalSpec(id="solo_high", title="高分独立", tier="T1", score=20, skill="mine_stone", est_cost_s=40.0, stage="graduated"),
            GoalSpec(id="solo_mid", title="中分独立", tier="T1", score=15, skill="scout_island", est_cost_s=60.0, stage="graduated"),
        )
        ranked = rank_actionable_goals(self._empty_state(), ledger)
        self.assertEqual([s.step_goal.id for s in ranked], ["solo_high", "solo_mid", "solo_low"])
        for step in ranked:
            self.assertEqual(step.shared_dependents, 1)
            self.assertEqual(step.marginal_roi, step.roi)

        # 向后兼容：GoalPlanStep 仅传 6 个基础字段依然可构造
        compat = GoalPlanStep(ledger[0], ledger[0], (ledger[0],), 5, 50.0, 0.1)
        self.assertEqual(compat.shared_dependents, 1)
        self.assertIsNone(compat.marginal_roi)

    def test_all_76_goals_keep_shiny_gear_rank_1_on_empty_and_mid_state(self) -> None:
        all_active = tuple(replace(g, stage="graduated") for g in GOALS)
        self.assertEqual(len(all_active), 76)

        ranked_empty = rank_actionable_goals(self._empty_state(), all_active)
        self.assertEqual(ranked_empty[0].step_goal.id, "prep_wood")
        self.assertEqual(ranked_empty[0].root_goal.id, "shiny_gear")
        self.assertGreater(ranked_empty[0].shared_dependents, 1)
        assert ranked_empty[0].marginal_roi is not None
        self.assertGreater(ranked_empty[0].marginal_roi, ranked_empty[0].roi)

        mid_state = self._empty_state()
        mid_state.set_inventory({
            "items": [
                {"name": "stone_pickaxe", "count": 1, "slot": 0},
                {"name": "stone_sword", "count": 1, "slot": 1},
                {"name": "oak_log", "count": 4, "slot": 2},
                {"name": "cobblestone", "count": 8, "slot": 3},
            ]
        })
        ranked_mid = rank_actionable_goals(
            mid_state,
            all_active,
            completed_advs={"minecraft:story/mine_stone", "minecraft:story/upgrade_tools"},
        )
        self.assertEqual(ranked_mid[0].step_goal.id, "prep_furnace")
        self.assertEqual(ranked_mid[0].root_goal.id, "shiny_gear")


class RoadsideIncrementalScanTests(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        reset_all_world_memos()

    def tearDown(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        reset_all_world_memos()

    def _make_loop(
        self,
        world,
        *,
        clock: _FakeClock,
        interval_s: float = 30.0,
        enable_roadside_scan: bool = True,
        telemetry_sink=None,
        max_steps: int = 3,
    ) -> tuple[MainLoop, State, CampaignPlanner]:
        state = State()
        state.set_status(world.status())
        state.set_inventory(world.inventory())
        registry = {_OneTickSkill.name: _OneTickSkill}
        ledger = (
            GoalSpec(
                id="probe_goal_a",
                title="沿途探查测试目标 A",
                tier="T1",
                score=10,
                skill=_OneTickSkill.name,
                est_cost_s=20.0,
                stage="graduated",
            ),
            GoalSpec(
                id="probe_goal_b",
                title="沿途探查测试目标 B",
                tier="T1",
                score=5,
                skill=_OneTickSkill.name,
                est_cost_s=30.0,
                stage="graduated",
            ),
        )
        planner = CampaignPlanner(
            mode="campaign",
            strategy="rule",
            registry=registry,
            ledger=ledger,
            min_verified="offline",
            clock=clock,
            on_objective_telemetry=telemetry_sink,
        )
        cfg = LoopConfig(
            max_steps=max_steps,
            idle_sleep_s=0.0,
            enable_monitor=False,
            enable_roadside_scan=enable_roadside_scan,
            roadside_scan_interval_s=interval_s,
            roadside_scan_radius=16,
        )
        ex = CountingExecutor(world, state)
        loop = MainLoop(world, state, ex, planner, config=cfg, clock=clock, log=lambda *_: None)
        return loop, state, planner

    def test_roadside_scan_throttling_interval(self) -> None:
        clock = _FakeClock(1000.0)
        world = _MockRoadsideWorld(
            clock=clock,
            blocks_resp={"blocks": [{"name": "hay_block", "pos": [14, 64, 12]}]},
            entities_resp={"entities": [{"id": 1, "name": "wandering_trader", "pos": [12, 64, 11]}]},
        )
        loop, state, _ = self._make_loop(world, clock=clock, interval_s=30.0)

        # 未满 30s：严禁调用 find_blocks / get_entities
        clock.advance(29.9)
        self.assertEqual(loop._maybe_roadside_scan(), [])
        self.assertEqual(len(world.find_blocks_calls), 0)
        self.assertEqual(len(world.get_entities_calls), 0)

        # 满 30s：触发一次扫描并写入地标
        clock.advance(0.1)
        found = loop._maybe_roadside_scan()
        self.assertEqual(set(found), {"village", "wandering_trader"})
        self.assertEqual(len(world.find_blocks_calls), 1)
        self.assertEqual(len(world.get_entities_calls), 1)
        self.assertEqual(world.find_blocks_calls[0]["radius"], 16)
        self.assertEqual(world.find_blocks_calls[0]["limit"], 16)
        self.assertEqual(world.get_entities_calls[0]["radius"], 16)

        # 再次立即调用（未过下一个 30s）：不触发
        clock.advance(25.0)
        self.assertEqual(loop._maybe_roadside_scan(), [])
        self.assertEqual(len(world.find_blocks_calls), 1)
        self.assertEqual(len(world.get_entities_calls), 1)

        # 再过 5s（满第二个 30s）：允许再次扫描，但重复位置不重复递增 landmark_version
        memo = memo_for(state)
        ver_before = memo.landmark_version
        clock.advance(5.0)
        found_repeat = loop._maybe_roadside_scan()
        self.assertEqual(found_repeat, [])
        self.assertEqual(len(world.find_blocks_calls), 2)
        self.assertEqual(len(world.get_entities_calls), 2)
        self.assertEqual(memo.landmark_version, ver_before)

    def test_roadside_discovery_records_memo_and_triggers_landmark_opportunity(self) -> None:
        clock = _FakeClock(2000.0)
        world = _MockRoadsideWorld(
            clock=clock,
            blocks_resp={
                "blocks": [
                    {"name": "minecraft:hay_block", "pos": [15, 64, 10]},
                    {"name": "crying_obsidian", "pos": [30, 64, 10]},
                ],
            },
            entities_resp={
                "entities": [
                    {"id": 7, "name": "minecraft:wandering_trader", "pos": [12, 64, 12]},
                    {"id": 8, "type": "zombie_villager", "pos": [22, 64, 12]},
                ],
            },
        )
        telemetry: list[dict] = []
        loop, state, planner = self._make_loop(
            world, clock=clock, interval_s=30.0, telemetry_sink=telemetry.append, max_steps=2
        )

        # 技能执行时推进时钟 35s，确保第 1 个技能完成边界触发沿途扫描，第 2 拍 select 捕获 landmark_opportunity
        orig_step = _OneTickSkill.step

        def advancing_step(skill_self, ex, st):
            done = orig_step(skill_self, ex, st)
            if done:
                clock.advance(35.0)
            return done

        _OneTickSkill.step = advancing_step
        try:
            res = loop.run()
        finally:
            _OneTickSkill.step = orig_step

        self.assertEqual(res.stop_reason, "max_steps_reached")
        memo = memo_for(state)
        self.assertTrue(memo.has_landmark("village"))
        self.assertTrue(memo.has_landmark("ruined_portal"))
        self.assertTrue(memo.has_landmark("wandering_trader"))
        self.assertTrue(memo.has_landmark("zombie_villager"))
        self.assertGreaterEqual(memo.landmark_version, 4)
        self.assertTrue(
            any(row["trigger"] == "landmark_opportunity" for row in telemetry),
            f"期望触发 landmark_opportunity 遥测，实际: {telemetry}",
        )

        # 验证 StrategyOrchestrator 冷却去重：在冷却窗内再次新增地标不立即刷屏，冷却结束后触发一次
        memo.record_landmark("bee_nest", [80, 65, 80])
        ver_after_nest = memo.landmark_version
        self.assertEqual(ver_after_nest, 5)
        dec_cool = planner.select(state, [])
        self.assertNotEqual(dec_cool.objective_trigger, "landmark_opportunity")

        clock.advance(planner._strategy.landmark_cooldown_s + 1.0)
        dec_after_cool = planner.select(state, [])
        self.assertEqual(dec_after_cool.objective_trigger, "landmark_opportunity")

    def test_roadside_scan_failure_safety_and_fatal_propagation(self) -> None:
        clock = _FakeClock(3000.0)

        # 1) 客户端缺失 find_blocks / get_entities：绝不崩溃
        no_scan_client = _NoScanClient()
        loop1, _, _ = self._make_loop(no_scan_client, clock=clock, interval_s=10.0)
        clock.advance(15.0)
        self.assertEqual(loop1._maybe_roadside_scan(), [])

        # 2) 客户端抛出非致命 ApiError("not_ready")：绝不崩溃
        err_world = _MockRoadsideWorld(
            clock=clock,
            blocks_err=ApiError("not_ready", "chunks loading"),
            entities_err=ApiError("not_ready", "entities loading"),
        )
        loop2, _, _ = self._make_loop(err_world, clock=clock, interval_s=10.0)
        clock.advance(15.0)
        self.assertEqual(loop2._maybe_roadside_scan(), [])
        self.assertEqual(len(err_world.find_blocks_calls), 1)
        self.assertEqual(len(err_world.get_entities_calls), 1)

        # 3) FATAL ApiError 必须向上传播，不静默吞掉
        fatal_err = ApiError("unauthorized", "bad token")
        self.assertEqual(fatal_err.kind, FATAL)
        fatal_world = _MockRoadsideWorld(
            clock=clock,
            blocks_err=fatal_err,
        )
        loop3, _, _ = self._make_loop(fatal_world, clock=clock, interval_s=10.0)
        clock.advance(15.0)
        with self.assertRaises(ApiError):
            loop3._maybe_roadside_scan()


if __name__ == "__main__":
    unittest.main()
