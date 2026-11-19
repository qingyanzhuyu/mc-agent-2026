"""战略层收敛契约、实时子目标、地标触发、检查点生命周期与路线分支单测（verified: offline）。

硬约束：
- 异步时序一律通过 `threading.Event` 与可注入假时钟 `FakeClock` 控制，不使用真实 `time.sleep`；
- 离线单测不发起真实网络请求；
- 文件读写显式指定 `encoding="utf-8"`。
"""
from __future__ import annotations

import io
import json
import re
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mcagent.loop import (
    CampaignPlanner,
    LoopConfig,
    MainLoop,
    reset_world_memo,
)
from mcagent.loop import __main__ as loop_main
from mcagent.loop import subgoals as subgoals_mod
from mcagent.planner import llm as llm_mod
from mcagent.planner import objective as objective_mod
from mcagent.planner.llm import LLMClient
from mcagent.planner.objective import Objective
from mcagent.planner.scheduler import DecisionScheduler
from mcagent.runtime import RuntimeConfig, State
from mcagent.skills import base as skill_base
from mcagent.skills.testing import CountingExecutor
from mcagent.skills.world_memo import memo_for
from tests.test_loop_soak import RichGridWorld

_COORD_TRIPLET_RE = re.compile(
    r"[\[\(]\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*[\]\)]"
)
_POS_KV_RE = re.compile(r"\bpos\s*[=:]", re.IGNORECASE)


class FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.t = initial

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


def _ok_json(obj: dict, prompt_tokens: int = 180, completion_tokens: int = 24) -> tuple[int, str]:
    return 200, json.dumps({
        "choices": [{"message": {"content": json.dumps(obj, ensure_ascii=False)}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }, ensure_ascii=False)


def _fresh_state(**st_overrides) -> State:
    cfg = replace(RuntimeConfig(), model_timeout_s=8.0, model_min_interval_s=0.001)
    state = State(cfg)
    base = {
        "ready": True,
        "alive": True,
        "health": 20,
        "food": 20,
        "is_day": True,
        "dimension": "overworld",
        "pos": [123.5, 64.0, -456.5],
    }
    base.update(st_overrides)
    state.set_status(base)
    state.set_inventory({"items": []})
    return state


def _wait_worker(planner: CampaignPlanner) -> None:
    assert planner.scheduler is not None
    worker = planner.scheduler._worker
    if worker is not None:
        worker.join(timeout=3.0)
        if worker.is_alive():
            raise AssertionError("后台规划线程未在预期事件释放后退出")


def _contains_text(value: object, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_contains_text(k, needle) or _contains_text(v, needle) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_text(item, needle) for item in value)
    return False


class LiveSubgoalsSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_world_memo()
        skill_base._COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_world_memo()
        skill_base._COOLDOWN.clear()

    def test_fallback_snapshot_and_context_without_evaluate_all_subgoals(self) -> None:
        planner = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline")
        empty_state = _fresh_state()
        populated_state = _fresh_state()
        populated_state.set_inventory({
            "items": [
                {"name": "crafting_table", "count": 1, "slot": 0},
                {"name": "iron_ingot", "count": 3, "slot": 1},
            ]
        })
        memo = memo_for(populated_state)
        memo.record_station("furnace", [123, 64, -456], dimension="overworld")
        memo.record_seen("station", "furnace", [123, 64, -456], dimension="overworld")
        memo.record_landmark("village", [321, 68, -654], dimension="overworld")
        memo.record_seen("structure", "bell", [321, 68, -654], dimension="overworld", tag="village")
        memo.record_landmark("ruined_portal", [412, 70, -789], dimension="overworld")
        memo.record_seen("structure", "obsidian", [412, 70, -789], dimension="overworld", tag="ruined_portal")
        memo.record_seen("obsidian", "obsidian", [412, 71, -789], dimension="overworld")

        with patch.object(subgoals_mod, "evaluate_all_subgoals", None):
            empty_snap = planner._subgoal_snapshot(empty_state)
            self.assertEqual(empty_snap["station_ready"], {"crafting_table": False, "furnace": False})
            self.assertIs(empty_snap["landmark_reachable"], False)
            self.assertIs(empty_snap["safe_encounter"], True)
            self.assertIs(empty_snap["active_portal"], False)
            self.assertFalse(_contains_text(empty_snap, "待现场观察"))

            pop_snap = planner._subgoal_snapshot(populated_state)
            self.assertEqual(pop_snap["station_ready"], {"crafting_table": True, "furnace": True})
            self.assertEqual(pop_snap["landmark_reachable"], ["ruined_portal", "village"])
            self.assertIs(pop_snap["safe_encounter"], True)
            self.assertIs(pop_snap["active_portal"], True)
            self.assertFalse(_contains_text(pop_snap, "待现场观察"))

            ctx = planner.build_context(populated_state)
            rendered = ctx.render()
            self.assertLessEqual(len(rendered), 2000)
            self.assertNotIn("待现场观察", rendered)
            self.assertIsNone(_COORD_TRIPLET_RE.search(rendered))
            self.assertIsNone(_POS_KV_RE.search(rendered))
            for coord_num in ("123", "-456", "321", "-654", "412", "-789"):
                self.assertNotIn(coord_num, rendered)

    def test_merged_path_calls_evaluate_all_subgoals_and_propagates_to_context(self) -> None:
        planner = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline")
        state = _fresh_state()
        custom_eval = {
            "item_available": {"iron_ingot": True},
            "tool_ready": {"iron": True},
            "station_ready": {"crafting_table": True, "furnace": False},
            "landmark_reachable": {"any": True, "village": True},
            "safe_encounter": True,
            "active_portal": True,
            "probe_marker": "merged_ok",
        }
        calls: list[tuple[State, object]] = []

        def fake_evaluate_all_subgoals(st: State, *, reservations=None):
            calls.append((st, reservations))
            return dict(custom_eval)

        with patch.object(
            subgoals_mod, "evaluate_all_subgoals", create=True, side_effect=fake_evaluate_all_subgoals
        ):
            snap = planner._subgoal_snapshot(state)
            self.assertEqual(snap, custom_eval)

            ctx = planner.build_context(state)
            self.assertEqual(len(calls), 2)
            self.assertIs(calls[0][0], state)
            self.assertIs(calls[1][1], planner.reservations)
            self.assertEqual(ctx.subgoal_predicates, custom_eval)
            rendered = ctx.render()
            self.assertLessEqual(len(rendered), 2000)
            self.assertNotIn("待现场观察", rendered)
            self.assertIn("probe_marker=merged_ok", rendered)
            self.assertIn("active_portal=是", rendered)


class StrategyConvergenceTests(unittest.TestCase):
    def setUp(self) -> None:
        llm_mod._reset_semaphores()
        reset_world_memo()
        skill_base._COOLDOWN.clear()

    def tearDown(self) -> None:
        llm_mod._reset_semaphores()
        reset_world_memo()
        skill_base._COOLDOWN.clear()

    def test_cli_parse_args_and_resolve_cli_strategy(self) -> None:
        parser = loop_main.build_parser()

        args_default = parser.parse_args([])
        self.assertEqual(args_default.strategy, "rule")
        self.assertEqual(loop_main.resolve_cli_strategy(args_default), "rule")

        args_llm = parser.parse_args(["--llm"])
        self.assertEqual(loop_main.resolve_cli_strategy(args_llm), "llm")

        args_rule_llm = parser.parse_args(["--strategy", "rule", "--llm"])
        self.assertEqual(args_rule_llm.strategy, "rule")
        self.assertEqual(loop_main.resolve_cli_strategy(args_rule_llm), "rule")

        args_legacy_llm = parser.parse_args(["--strategy", "legacy", "--llm"])
        self.assertEqual(args_legacy_llm.strategy, "legacy")
        self.assertEqual(loop_main.resolve_cli_strategy(args_legacy_llm), "legacy")

    def test_campaign_planner_strategy_convergence(self) -> None:
        llm = LLMClient(key="k", fast_proxy="", poster=lambda *a: _ok_json({"goal_id": "prep_wood"}), log=None)

        planner_llm = CampaignPlanner(strategy=None, use_llm=True, llm=llm)
        self.assertEqual(planner_llm.strategy, "llm")
        self.assertIsInstance(planner_llm.scheduler, DecisionScheduler)

        planner_rule = CampaignPlanner(strategy=None, use_llm=False)
        self.assertEqual(planner_rule.strategy, "rule")
        self.assertIsNone(planner_rule.scheduler)

        planner_legacy = CampaignPlanner(strategy="legacy", use_llm=True, llm=llm)
        self.assertEqual(planner_legacy.strategy, "legacy")
        self.assertFalse(planner_legacy.use_llm)
        self.assertIsNone(planner_legacy.scheduler)

    def test_cli_dry_llm_runs_with_strategy_llm(self) -> None:
        buf = io.StringIO()
        with patch.object(loop_main.Client, "probe", return_value="ok"), redirect_stdout(buf):
            rc = loop_main.main(["--dry", "--llm"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("strategy=llm", out)


class LandmarkOpportunityTriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        llm_mod._reset_semaphores()
        reset_world_memo()
        skill_base._COOLDOWN.clear()
        self.clock = FakeClock()

    def tearDown(self) -> None:
        llm_mod._reset_semaphores()
        reset_world_memo()
        skill_base._COOLDOWN.clear()

    def test_landmark_version_increase_triggers_landmark_opportunity_and_async_llm(self) -> None:
        state = _fresh_state()
        call_idx = {"n": 0}
        entered_landmark = threading.Event()
        release_landmark = threading.Event()

        def poster(*_args):
            call_idx["n"] += 1
            if call_idx["n"] == 1:
                return _ok_json({"goal_id": "upgrade_tools", "budget_s": 240, "why": "先升石镐"})
            entered_landmark.set()
            release_landmark.wait(timeout=3.0)
            return _ok_json({
                "goal_id": "mine_stone",
                "budget_s": 180,
                "why": "借地标顺路采石",
                "route_branch": "surface_quick",
            })

        llm = LLMClient(key="k", fast_proxy="", poster=poster, clock=self.clock, log=None)
        rows: list[dict] = []
        planner = CampaignPlanner(
            mode="campaign",
            strategy="llm",
            llm=llm,
            min_verified="offline",
            clock=self.clock,
            on_objective_telemetry=rows.append,
        )

        # 初始化首次目标
        planner.select(state, [])
        _wait_worker(planner)
        d_init = planner.select(state, [])
        assert d_init.objective is not None
        self.assertEqual((d_init.objective.goal_id, d_init.objective.source), ("upgrade_tools", "llm"))

        # landmark_version 递增触发 landmark_opportunity 并派发异步 LLM
        memo = memo_for(state)
        setattr(memo, "landmark_version", int(getattr(memo, "landmark_version", 0)) + 1)

        d_trig = planner.select(state, [])
        self.assertTrue(entered_landmark.wait(timeout=2.0))
        self.assertEqual(d_trig.objective_trigger, "landmark_opportunity")
        self.assertTrue(
            any(r["trigger"] == "landmark_opportunity" and r["source"] == "rule" for r in rows)
        )

        release_landmark.set()
        _wait_worker(planner)
        d_adopted = planner.select(state, [])
        assert d_adopted.objective is not None
        self.assertEqual(d_adopted.objective.goal_id, "mine_stone")
        self.assertEqual(d_adopted.objective.source, "llm")
        self.assertTrue(
            any(r["trigger"] == "landmark_opportunity" and r["source"] == "llm" and r["adopted"] for r in rows)
        )

    def test_landmark_opportunity_deferred_during_safe_mode_and_fired_after(self) -> None:
        state = _fresh_state()
        call_idx = {"n": 0}
        entered_landmark = threading.Event()
        release_landmark = threading.Event()

        def poster(*_args):
            call_idx["n"] += 1
            if call_idx["n"] == 1:
                return _ok_json({"goal_id": "upgrade_tools", "budget_s": 240, "why": "初始目标"})
            entered_landmark.set()
            release_landmark.wait(timeout=3.0)
            return _ok_json({"goal_id": "mine_stone", "budget_s": 180, "why": "脱险后响应地标"})

        llm = LLMClient(key="k", fast_proxy="", poster=poster, clock=self.clock, log=None)
        rows: list[dict] = []
        planner = CampaignPlanner(
            mode="campaign",
            strategy="llm",
            llm=llm,
            min_verified="offline",
            clock=self.clock,
            on_objective_telemetry=rows.append,
        )

        planner.select(state, [])
        _wait_worker(planner)
        planner.select(state, [])
        calls_before = call_idx["n"]

        # 进入 safe_mode 期间发现地标：必须延后到 safe_mode 结束后再切目标并调用 LLM
        state.safe_mode_until = time.monotonic() + 60.0
        memo = memo_for(state)
        setattr(memo, "landmark_version", int(getattr(memo, "landmark_version", 0)) + 1)

        d_safe = planner.select(state, [])
        self.assertIsNone(d_safe.skill)
        self.assertEqual(call_idx["n"], calls_before)
        self.assertEqual(planner._strategy._deferred_trigger, "landmark_opportunity")
        self.assertFalse(any(r["trigger"] == "landmark_opportunity" for r in rows))

        # safe_mode 结束 -> 立即补触发 deferred landmark_opportunity
        state.safe_mode_until = 0.0
        d_after = planner.select(state, [])
        self.assertTrue(entered_landmark.wait(timeout=2.0))
        self.assertEqual(d_after.objective_trigger, "landmark_opportunity")
        self.assertTrue(
            any(r["trigger"] == "landmark_opportunity" and r["source"] == "rule" for r in rows)
        )

        release_landmark.set()
        _wait_worker(planner)
        d_adopted = planner.select(state, [])
        assert d_adopted.objective is not None
        self.assertEqual((d_adopted.objective.goal_id, d_adopted.objective.source), ("mine_stone", "llm"))
        self.assertTrue(
            any(r["trigger"] == "landmark_opportunity" and r["source"] == "llm" and r["adopted"] for r in rows)
        )


class CheckpointLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_world_memo()
        skill_base._COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_world_memo()
        skill_base._COOLDOWN.clear()

    def test_defaults_are_none(self) -> None:
        self.assertIsNone(LoopConfig().checkpoint_path)
        parser = loop_main.build_parser()
        self.assertIsNone(parser.parse_args([]).checkpoint)
        self.assertEqual(parser.parse_args(["--checkpoint", "/tmp/world.ckpt.json"]).checkpoint, "/tmp/world.ckpt.json")

    def test_main_loop_checkpoint_load_goal_save_and_finally_save(self) -> None:
        blocks = {(2 + i, 64, 0): "oak_log" for i in range(4)}
        world = RichGridWorld(blocks=blocks, keep_inventory=True)
        state = State(RuntimeConfig())
        ex = CountingExecutor(world, state)
        planner = CampaignPlanner(
            mode="campaign",
            strategy="rule",
            min_verified="offline",
            target_goal="prep_wood",
        )
        events: list[tuple[str, str]] = []
        memo = memo_for(state)

        with tempfile.TemporaryDirectory() as tmp:
            ckpt_path = str(Path(tmp) / "memo_ckpt.json")
            cfg = LoopConfig(
                max_steps=40,
                idle_sleep_s=0.0,
                respawn_poll_s=0.001,
                enable_monitor=False,
                target_goal="prep_wood",
                stop_on_goal=True,
                checkpoint_path=ckpt_path,
            )
            with (
                patch.object(memo, "load_checkpoint", side_effect=lambda p: events.append(("load", str(p))) or True),
                patch.object(memo, "save_checkpoint", side_effect=lambda p: events.append(("save", str(p)))),
            ):
                res = MainLoop(world, state, ex, planner, config=cfg, log=lambda *_: None).run()

        self.assertEqual(res.stop_reason, "goal_reached")
        self.assertEqual(events[0], ("load", ckpt_path))
        # 目标达成保存 1 次 + finally 退出保存 1 次
        save_events = [ev for ev in events if ev[0] == "save"]
        self.assertGreaterEqual(len(save_events), 2)
        self.assertEqual(events[-1], ("save", ckpt_path))
        self.assertEqual(events, [("load", ckpt_path), ("save", ckpt_path), ("save", ckpt_path)])

    def test_main_loop_checkpoint_finally_save_without_goal_reached(self) -> None:
        blocks = {(2 + i, 64, 0): "oak_log" for i in range(4)}
        world = RichGridWorld(blocks=blocks, keep_inventory=True)
        state = State(RuntimeConfig())
        ex = CountingExecutor(world, state)
        planner = CampaignPlanner(
            mode="campaign",
            strategy="rule",
            min_verified="offline",
            target_goal="prep_wood",
        )
        events: list[tuple[str, str]] = []
        memo = memo_for(state)

        with tempfile.TemporaryDirectory() as tmp:
            ckpt_path = str(Path(tmp) / "memo_ckpt.json")
            cfg = LoopConfig(
                max_steps=1,
                idle_sleep_s=0.0,
                respawn_poll_s=0.001,
                enable_monitor=False,
                checkpoint_path=ckpt_path,
            )
            with (
                patch.object(memo, "load_checkpoint", side_effect=lambda p: events.append(("load", str(p))) or False),
                patch.object(memo, "save_checkpoint", side_effect=lambda p: events.append(("save", str(p)))),
            ):
                res = MainLoop(world, state, ex, planner, config=cfg, log=lambda *_: None).run()

        self.assertEqual(res.stop_reason, "max_steps_reached")
        self.assertEqual(events, [("load", ckpt_path), ("save", ckpt_path)])

    def test_main_loop_no_checkpoint_path_never_calls_load_or_save(self) -> None:
        blocks = {(2 + i, 64, 0): "oak_log" for i in range(4)}
        world = RichGridWorld(blocks=blocks, keep_inventory=True)
        state = State(RuntimeConfig())
        ex = CountingExecutor(world, state)
        planner = CampaignPlanner(
            mode="campaign",
            strategy="rule",
            min_verified="offline",
            target_goal="prep_wood",
        )
        events: list[tuple[str, str]] = []
        memo = memo_for(state)
        cfg = LoopConfig(
            max_steps=40,
            idle_sleep_s=0.0,
            respawn_poll_s=0.001,
            enable_monitor=False,
            target_goal="prep_wood",
            stop_on_goal=True,
            checkpoint_path=None,
        )
        with (
            patch.object(memo, "load_checkpoint", side_effect=lambda p: events.append(("load", str(p))) or True),
            patch.object(memo, "save_checkpoint", side_effect=lambda p: events.append(("save", str(p)))),
        ):
            res = MainLoop(world, state, ex, planner, config=cfg, log=lambda *_: None).run()

        self.assertEqual(res.stop_reason, "goal_reached")
        self.assertEqual(events, [])


class ObjectiveRouteBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        llm_mod._reset_semaphores()
        reset_world_memo()
        skill_base._COOLDOWN.clear()
        self.clock = FakeClock()

    def tearDown(self) -> None:
        llm_mod._reset_semaphores()
        reset_world_memo()
        skill_base._COOLDOWN.clear()

    def test_objective_route_branch_backward_compatibility_and_validation(self) -> None:
        legacy_obj = Objective("prep_wood", 120.0, "先砍木", "rule", 1000.0)
        self.assertEqual(legacy_obj.route_branch, "")
        self.assertEqual(legacy_obj.to_dict()["route_branch"], "")

        branched = Objective("prep_wood", 120.0, route_branch="  iron_chain  ")
        self.assertEqual(branched.route_branch, "iron_chain")
        self.assertEqual(branched.to_dict()["route_branch"], "iron_chain")

        with self.assertRaises(ValueError):
            Objective("prep_wood", 120.0, route_branch=123)  # type: ignore[arg-type]

    def test_resolve_and_choose_objective_route_branch_validation(self) -> None:
        state = _fresh_state()
        for branch in ("iron_chain", "surface_quick", "diamond_nether"):
            with self.subTest(branch=branch):
                resolved, errs = objective_mod.resolve_objective(
                    {"goal_id": "prep_wood", "budget_s": 120, "why": "测试路线", "route_branch": f"  {branch}  "},
                    state,
                    min_verified="offline",
                    now=1000.0,
                )
                self.assertEqual(errs, [])
                assert resolved is not None
                self.assertEqual(resolved.route_branch, branch)
                self.assertEqual(resolved.to_dict()["route_branch"], branch)

        bad_obj, bad_errs = objective_mod.resolve_objective(
            {"goal_id": "prep_wood", "budget_s": 120, "route_branch": 123},
            state,
            min_verified="offline",
            now=1000.0,
        )
        self.assertIsNone(bad_obj)
        self.assertTrue(any("route_branch" in e for e in bad_errs))

        attempt_num = {"n": 0}

        def retrying_poster(*_args):
            attempt_num["n"] += 1
            if attempt_num["n"] == 1:
                return _ok_json({"goal_id": "prep_wood", "budget_s": 120, "why": "非法分支", "route_branch": 123})
            return _ok_json({
                "goal_id": "prep_wood",
                "budget_s": 120,
                "why": "合法分支",
                "route_branch": "  diamond_nether  ",
            })

        llm = LLMClient(key="k", fast_proxy="", poster=retrying_poster, clock=self.clock, log=None)
        chosen, errs, used = objective_mod.choose_objective(
            llm,
            "赛况时间：剩余28800s",
            state,
            min_verified="offline",
            attempts=2,
            now=1000.0,
        )
        self.assertEqual(errs, [])
        self.assertEqual(used, 2)
        assert chosen is not None
        self.assertEqual(chosen.route_branch, "diamond_nether")
        self.assertEqual(chosen.to_dict()["route_branch"], "diamond_nether")

    def test_route_branch_propagates_through_async_orchestrator_and_telemetry(self) -> None:
        state = _fresh_state()
        entered = threading.Event()
        release = threading.Event()

        def poster(*_args):
            entered.set()
            release.wait(timeout=3.0)
            return _ok_json({
                "goal_id": "mine_stone",
                "budget_s": 160,
                "why": "优先快速补石",
                "route_branch": "  surface_quick  ",
            })

        llm = LLMClient(key="k", fast_proxy="", poster=poster, clock=self.clock, log=None)
        rows: list[dict] = []
        planner = CampaignPlanner(
            mode="campaign",
            strategy="llm",
            llm=llm,
            min_verified="offline",
            clock=self.clock,
            on_objective_telemetry=rows.append,
        )

        d_rule = planner.select(state, [])
        self.assertTrue(entered.wait(timeout=2.0))
        assert d_rule.objective is not None
        self.assertEqual(d_rule.objective.route_branch, "")

        release.set()
        _wait_worker(planner)

        d_llm = planner.select(state, [])
        assert d_llm.objective is not None
        self.assertEqual(d_llm.objective.source, "llm")
        self.assertEqual(d_llm.objective.goal_id, "mine_stone")
        self.assertEqual(d_llm.objective.route_branch, "surface_quick")
        assert planner.current_objective is not None
        self.assertEqual(planner.current_objective.route_branch, "surface_quick")

        adopted_llm_rows = [r for r in rows if r["source"] == "llm" and r["adopted"]]
        self.assertEqual(len(adopted_llm_rows), 1)
        self.assertEqual(adopted_llm_rows[0]["route_branch"], "surface_quick")


if __name__ == "__main__":
    unittest.main()
