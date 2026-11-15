"""验证环境挂起、唤醒与技能寻路退避，无需比赛账号。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.loop.auditor import SoakObserver
from mcagent.loop.engine import LoopConfig, MainLoop
from mcagent.loop.goals import GoalSpec
from mcagent.loop.policy import CampaignPlanner, PlanDecision, RulePlanner
from mcagent.runtime import EnvBlocked, Replan, State
from mcagent.skills import Param, Skill
from mcagent.skills import base as skill_base
from mcagent.skills.testing import CountingExecutor
from tests.test_loop_soak import RichGridWorld


def _no_iron(wake_on="chunk_moved"):
    return EnvBlocked("暂无裸露铁矿", missing_predicate="exposed_iron", searched_volume="半径 64", wake_on=wake_on)


class FindIron(Skill):
    name = "find_iron"
    goal = "acq:iron_ingot"

    def run(self, ex, state):
        if state.pos[0] < 16:
            raise _no_iron()
        return iter(())


class FindGem(Skill):
    name = "find_gem"
    goal = "acq:{material}"
    params = {"material": Param(str, "矿物")}


class FindShelter(Skill):
    name = "find_shelter"
    goal = "find:shelter"


class Scout(Skill):
    name = "scout_island"
    goal = "explore:island"
    params = {"radius": Param(int, "半径", default=36)}


_IRON = GoalSpec("iron", "铁", "T1", 5, FindIron.name, done_items=("iron_ingot>=1",), stage="candidate")
_CHAIN = GoalSpec("tools", "工具", "T1", 5, FindShelter.name, prereq_goals=("iron",), stage="graduated")
_OTHER = GoalSpec("shelter", "避难处", "T1", 5, FindShelter.name, stage="graduated")
_REGISTRY = {cls.name: cls for cls in (FindIron, FindGem, FindShelter, Scout)}


class EnvironmentBlockTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        self.state = State()
        self.state.set_status({"pos": [-1, 64, 0], "dimension": "overworld", "is_day": True})
        self.state.set_inventory({"items": []})
        self.addCleanup(skill_base._COOLDOWN.clear)

    def test_rule_suspends_chain_and_selects_alternative_then_retries_after_chunk_change(self):
        planner = RulePlanner(_REGISTRY, (_IRON, _CHAIN, _OTHER), target_goal="tools,shelter")
        planner.record_environment_block(FindIron(), _no_iron(), self.state)
        self.assertEqual(planner.choose(self.state).skill.name, FindShelter.name)
        self.assertEqual(set(planner.blocked_targets(self.state, "tools,shelter")), {"tools"})

        self.state.set_status({"dimension": "overworld", "pos": [-16, 62, 0]})
        self.assertIn("tools", planner.blocked_targets(self.state, "tools"))
        self.state.set_status({"dimension": "overworld", "pos": [-17, 62, 0]})
        self.assertEqual(planner.blocked_targets(self.state, "tools"), {})
        self.assertIn("iron", [step.step_goal.id for step in planner._rank(self.state)])

    def test_chunk_suspension_requests_scout_in_focused_campaign(self):
        planner = RulePlanner(_REGISTRY, (_IRON,), target_goal="iron")
        planner.record_environment_block(FindIron(), _no_iron(), self.state)
        self.assertEqual(planner.choose(self.state).skill.name, Scout.name)

    def test_day_night_and_inventory_require_real_changed_evidence(self):
        planner = RulePlanner(_REGISTRY, (_IRON,), target_goal="iron")
        planner.record_environment_block(FindIron(), _no_iron("night"), self.state)
        self.state.set_status({"pos": [-1, 64, 0]})
        self.assertIn("iron", planner.blocked_targets(self.state, "iron"))
        self.state.set_status({"is_day": False})
        self.assertEqual(planner.blocked_targets(self.state, "iron"), {})

        planner.record_environment_block(FindIron(), _no_iron("night"), self.state)
        self.assertIn("iron", planner.blocked_targets(self.state, "iron"))
        self.state.set_status({"is_day": True})
        self.assertIn("iron", planner.blocked_targets(self.state, "iron"))
        self.state.set_status({"is_day": False})
        self.assertEqual(planner.blocked_targets(self.state, "iron"), {})

        planner.record_environment_block(FindIron(), _no_iron("inventory_changed"), self.state)
        self.state.invalidate_inventory()
        self.assertIn("iron", planner.blocked_targets(self.state, "iron"))
        self.state.set_inventory({"items": [{"name": "raw_iron", "count": 1, "slot": 0}]})
        self.assertEqual(planner.blocked_targets(self.state, "iron"), {})

    def test_fallback_block_does_not_hide_goal_after_missing_item_is_found(self):
        dependent = GoalSpec(
            "tools", "工具", "T1", 5, FindShelter.name, stage="graduated", prereq_items=("iron_ingot>=3",),
            gather_fallbacks=(("iron_ingot>=3", FindIron.name, ()),),
        )
        planner = RulePlanner(_REGISTRY, (dependent,), target_goal="tools")
        planner.record_environment_block(FindIron(), _no_iron(), self.state)
        self.assertEqual(list(planner.blocked_targets(self.state, "tools")), ["tools"])
        self.state.set_inventory({"items": [{"name": "iron_ingot", "count": 3}]})
        self.assertEqual(planner.blocked_targets(self.state, "tools"), {})
        self.assertEqual(planner.choose(self.state).skill.name, FindShelter.name)

    def test_model_obeys_suspension_and_other_args_stay_available(self):
        planner = CampaignPlanner(registry=_REGISTRY, ledger=(_IRON, _OTHER), use_llm=True)
        planner.record_environment_block(FindIron(), _no_iron(), self.state)
        planner.record_environment_block(FindGem(material="diamond"), _no_iron(), self.state)
        with patch("mcagent.loop.policy.skill_choice.choose", return_value=(FindGem(material="diamond"), [], 1)) as choose:
            decision = planner.select(self.state, [])
        self.assertNotIn(FindIron.name, choose.call_args.kwargs["registry"])
        self.assertIn(FindGem.name, choose.call_args.kwargs["registry"])
        self.assertEqual(decision.skill.name, FindShelter.name)
        with patch("mcagent.loop.policy.skill_choice.choose", return_value=(FindGem(material="coal"), [], 1)):
            decision = planner.select(self.state, [])
        self.assertEqual(decision.skill.args, {"material": "coal"})

    def test_run_end_records_environment_evidence_without_enqueuing_related_miss(self):
        world = RichGridWorld()
        registry = {FindIron.name: FindIron, FindShelter.name: FindShelter}
        ledger = (_IRON, _CHAIN, _OTHER)
        goal = "tools,shelter"
        planner = CampaignPlanner(registry=registry, ledger=ledger, target_goal=goal, mode="focus",
                                  focus_skill=FindIron.name)
        config = LoopConfig(mode="focus", target_goal=goal, idle_sleep_s=0, enable_monitor=False)
        with tempfile.TemporaryDirectory() as run_dir:
            observer = SoakObserver(run_dir, ledger=ledger, target_goal=goal)
            loop = MainLoop(world, self.state, CountingExecutor(world, self.state), planner,
                            config=config, observer=observer, log=lambda *_: None)
            result = loop.run()
            rows = [json.loads(line) for line in observer.timeline_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(result.skills_replan, 0)
            self.assertEqual(planner.rules.ep_goal_fails, {})
            self.assertEqual(rows[-1]["environment_blocked_goals"], {"tools": _no_iron().evidence()})
            self.assertEqual(rows[-2]["outcome"], "env_blocked")
            self.assertEqual(rows[-2]["environment"]["searched_volume"], "半径 64")
            tickets = [json.loads(p.read_text(encoding="utf-8")) for p in Path(run_dir).glob("incidents/*.json")]
            self.assertEqual([ticket["fingerprint"] for ticket in tickets], ["GOAL_MISS:shelter"])

    def test_three_consecutive_path_failures_extend_existing_cooldown_and_success_resets(self):
        class StuckScout(Scout):
            def run(self, ex, state):
                raise Replan("路径不可达") from ApiError("path_not_found", "no path")

        class RecoveredScout(Scout):
            def run(self, ex, state):
                return iter(())

        world = RichGridWorld()
        planner = CampaignPlanner(registry=_REGISTRY, ledger=())
        failures = [StuckScout(), StuckScout(), StuckScout(), RecoveredScout(), StuckScout()]
        steps = [PlanDecision(skill, "rule", "测试") for skill in failures]
        steps.append(PlanDecision(None, "idle", "测试结束"))
        config = LoopConfig(idle_sleep_s=0, enable_monitor=False)
        with patch.object(planner, "select", side_effect=steps), patch(
            "mcagent.skills.base.time.time", return_value=100.0,
        ), patch.dict(skill_base._COOLDOWN, clear=True):
            recorded = []
            original = Skill.mark_failed

            def record(skill, **kwargs):
                original(skill, **kwargs)
                recorded.append(skill_base._COOLDOWN[skill.goal] - 100.0)

            with patch.object(Skill, "mark_failed", record):
                loop = MainLoop(world, self.state, CountingExecutor(world, self.state), planner,
                                config=config, log=lambda *_: None)
                result = loop.run()
            self.assertEqual(result.skills_replan, 4)
            self.assertEqual(recorded, [Scout.cooldown_s, Scout.cooldown_s, 3 * Scout.cooldown_s, Scout.cooldown_s])
            self.assertTrue(Scout().is_cooling())
