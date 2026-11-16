"""验证主循环在 stalled 错误下的冷却升级机制与 SoakObserver 探索恢复抑制策略。

包含：
1. TestMainLoopStalledCooldown:
   - 连续 3 次 stalled 失败使冷却时长升级为 3 * cooldown_s，成功完成技能后重置连败计数；
   - path_not_found 与 stalled 混合失败共享连败计数，第 3 次失败升级为 3 * cooldown_s；
   - 非寻路类失败（timeout）、死亡类错误（dead）或伴随死亡的 stalled 不累加计数并重置连败。
2. TestSoakObserverStalledSuppression:
   - scout_island 在主循环中作为环境恢复任务（environment_recovery=True）遇到 stalled 时，
     抑制 REPLAN_OSCILLATION 报警、计入波动计数，且冷却正常升级为 3 * cooldown_s；
   - 恢复型 scout_island 遇到 stalled 时清除先前累积的目标失败时间戳，防止跨轮误报；
   - 对照测试：非恢复模式、其他错误码（timeout / bad_args）或非 scout_island 技能仍正常上报异常单。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.loop.auditor import SoakObserver
from mcagent.loop.engine import LoopConfig, MainLoop, RunResult
from mcagent.loop.policy import CampaignPlanner, PlanDecision
from mcagent.loop.starter_skills import reset_world_memo
from mcagent.runtime import Replan, State
from mcagent.skills import Skill
from mcagent.skills import base as skill_base
from mcagent.skills.testing import CountingExecutor
from tests.test_loop_soak import RichGridWorld


def _make_stub_skill(
    skill_name: str,
    goal_id: str,
    error_code: Optional[str] = None,
    *,
    cooldown: float = 15.0,
    mark_dead: bool = False,
) -> Skill:
    """构造仅本地使用的最小技能桩，不注册进全局 REGISTRY 且不依赖其它技能模块。"""

    class _StubSkill(Skill):
        name = skill_name
        goal = goal_id
        cooldown_s = cooldown

        def run(self, ex, state):
            if error_code is None:
                return iter(())
            if mark_dead:
                state.st["alive"] = False
            raise Replan(f"stub:{error_code}") from ApiError(error_code, f"injected {error_code}")

    return _StubSkill()


def _run_loop_sequence(
    state: State,
    decisions: list[PlanDecision],
    *,
    observer: Optional[SoakObserver] = None,
) -> tuple[RunResult, list[float]]:
    """以确定性决策序列运行 MainLoop，并记录每次 mark_failed 写入的冷却时长。"""
    world = RichGridWorld()
    planner = CampaignPlanner(registry={}, ledger=())
    steps = [*decisions, PlanDecision(None, "idle", "测试结束")]
    config = LoopConfig(idle_sleep_s=0, enable_monitor=False)

    recorded: list[float] = []
    original = Skill.mark_failed

    def record(skill, **kwargs):
        original(skill, **kwargs)
        recorded.append(skill_base._COOLDOWN[skill.goal] - 100.0)

    with patch.object(planner, "select", side_effect=steps), patch(
        "mcagent.skills.base.time.time", return_value=100.0,
    ), patch.dict(skill_base._COOLDOWN, clear=True), patch.object(Skill, "mark_failed", record):
        loop = MainLoop(
            world,
            state,
            CountingExecutor(world, state),
            planner,
            config=config,
            observer=observer,
            log=lambda *_: None,
        )
        result = loop.run()
    return result, recorded


class _BaseNavLoopTestCase(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        self.addCleanup(skill_base._COOLDOWN.clear)
        self.addCleanup(reset_world_memo)
        reset_world_memo()
        self.state = State()
        self.state.set_status({
            "pos": [0, 64, 0],
            "dimension": "overworld",
            "is_day": True,
            "alive": True,
            "ready": True,
        })
        self.state.set_inventory({"items": []})


class TestMainLoopStalledCooldown(_BaseNavLoopTestCase):
    """测试主循环针对 stalled 与寻路失败的冷却阶梯退避策略。"""

    def test_three_consecutive_stalled_failures_escalate_cooldown_and_success_resets(self):
        skills = [
            _make_stub_skill("scout_island", "explore:island", "stalled"),
            _make_stub_skill("scout_island", "explore:island", "stalled"),
            _make_stub_skill("scout_island", "explore:island", "stalled"),
            _make_stub_skill("scout_island", "explore:island", None),
            _make_stub_skill("scout_island", "explore:island", "stalled"),
        ]
        decisions = [PlanDecision(s, "rule", "测试") for s in skills]
        result, recorded = _run_loop_sequence(self.state, decisions)

        self.assertEqual(result.skills_replan, 4)
        self.assertEqual(result.skills_completed, 1)
        self.assertEqual(recorded, [15.0, 15.0, 45.0, 15.0])

    def test_mixed_path_not_found_and_stalled_share_streak(self):
        skills = [
            _make_stub_skill("scout_island", "explore:island", "path_not_found"),
            _make_stub_skill("scout_island", "explore:island", "stalled"),
            _make_stub_skill("scout_island", "explore:island", "stalled"),
        ]
        decisions = [PlanDecision(s, "rule", "测试") for s in skills]
        result, recorded = _run_loop_sequence(self.state, decisions)

        self.assertEqual(result.skills_replan, 3)
        self.assertEqual(recorded, [15.0, 15.0, 45.0])

    def test_non_path_or_death_failure_resets_stalled_streak(self):
        cases = (
            ("timeout_resets_streak", "timeout", False, 0),
            ("dead_resets_streak", "dead", False, 1),
            ("stalled_while_dead_resets_streak", "stalled", True, 1),
        )
        for label, breaker_code, mark_dead, expected_deaths in cases:
            with self.subTest(label):
                skills = [
                    _make_stub_skill("scout_island", "explore:island", "stalled"),
                    _make_stub_skill("scout_island", "explore:island", "stalled"),
                    _make_stub_skill("scout_island", "explore:island", breaker_code, mark_dead=mark_dead),
                    _make_stub_skill("scout_island", "explore:island", "stalled"),
                ]
                decisions = [PlanDecision(s, "rule", "测试") for s in skills]
                result, recorded = _run_loop_sequence(self.state, decisions)

                self.assertEqual(result.skills_replan, 4)
                self.assertEqual(result.deaths_recovered, expected_deaths)
                self.assertEqual(recorded, [15.0, 15.0, 15.0, 15.0])


class TestSoakObserverStalledSuppression(_BaseNavLoopTestCase):
    """测试 SoakObserver 对环境恢复阶段 scout_island 遇到 stalled 失败时的震荡抑制与记录。"""

    def test_mainloop_recovery_scout_stalled_suppresses_oscillation_and_escalates_cooldown(self):
        with tempfile.TemporaryDirectory() as run_dir:
            obs = SoakObserver(run_dir, ledger=(), target_goal=None)
            decisions = [
                PlanDecision(
                    _make_stub_skill("scout_island", "explore:island", "stalled"),
                    "rule",
                    "recovery",
                    environment_recovery=True,
                )
                for _ in range(3)
            ]
            result, recorded = _run_loop_sequence(self.state, decisions, observer=obs)

            self.assertEqual(result.skills_replan, 3)
            self.assertEqual(obs.env_variance_counts.get("scout_island:stalled"), 3)
            self.assertEqual(list(obs.incidents_dir.glob("*.json")), [])

            timeline_rows = [
                json.loads(line)
                for line in obs.timeline_path.read_text(encoding="utf-8").splitlines()
            ]
            ends = [r for r in timeline_rows if r.get("event") == "skill_end"]
            self.assertEqual(len(ends), 3)
            for r in ends:
                self.assertEqual(r.get("error_code"), "stalled")
                self.assertIs(r.get("environment_recovery"), True)

            self.assertEqual(recorded, [15.0, 15.0, 45.0])

    def test_recovery_scout_stalled_clears_prior_goal_failures(self):
        skill = _make_stub_skill("scout_island", "explore:island")
        with tempfile.TemporaryDirectory() as run_dir:
            obs = SoakObserver(run_dir, ledger=(), target_goal=None)

            for _ in range(2):
                obs.on_skill_end(skill, "replan", self.state, error_code="stalled", environment_recovery=False)
            self.assertEqual(len(obs._goal_failures.get(skill.goal, [])), 2)
            self.assertEqual(list(obs.incidents_dir.glob("*.json")), [])

            obs.on_skill_end(skill, "replan", self.state, error_code="stalled", environment_recovery=True)
            self.assertNotIn(skill.goal, obs._goal_failures)
            self.assertEqual(list(obs.incidents_dir.glob("*.json")), [])

            for _ in range(2):
                obs.on_skill_end(skill, "replan", self.state, error_code="stalled", environment_recovery=False)
            self.assertEqual(len(obs._goal_failures.get(skill.goal, [])), 2)
            self.assertEqual(list(obs.incidents_dir.glob("*.json")), [])

            obs.on_skill_end(skill, "replan", self.state, error_code="stalled", environment_recovery=False)
            tickets = [json.loads(p.read_text(encoding="utf-8")) for p in obs.incidents_dir.glob("*.json")]
            self.assertEqual([(t["code"], t["category"]) for t in tickets], [("REPLAN_OSCILLATION", "strategy")])

    def test_contrast_non_recovery_and_other_errors_or_skills_still_report(self):
        cases = (
            ("non_recovery_scout_stalled", "scout_island", "explore:island", "stalled", False, 3, ("REPLAN_OSCILLATION", "strategy")),
            ("recovery_scout_timeout", "scout_island", "explore:island", "timeout", True, 3, ("REPLAN_OSCILLATION", "strategy")),
            ("recovery_scout_bad_args", "scout_island", "explore:island", "bad_args", True, 1, ("CONTRACT_OR_FATAL_ERROR", "bug")),
            ("recovery_other_skill_stalled", "chop_tree", "gather:wood", "stalled", True, 3, ("REPLAN_OSCILLATION", "strategy")),
        )
        for label, skill_name, goal_id, err_code, env_rec, repeats, expected in cases:
            with self.subTest(label), tempfile.TemporaryDirectory() as run_dir:
                obs = SoakObserver(run_dir, ledger=(), target_goal=None)
                skill = _make_stub_skill(skill_name, goal_id)
                for _ in range(repeats):
                    obs.on_skill_end(skill, "replan", self.state, error_code=err_code, environment_recovery=env_rec)
                tickets = [json.loads(p.read_text(encoding="utf-8")) for p in obs.incidents_dir.glob("*.json")]
                self.assertEqual([(t["code"], t["category"]) for t in tickets], [expected])


if __name__ == "__main__":
    unittest.main()
