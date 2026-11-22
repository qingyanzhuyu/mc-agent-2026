"""战略层 Objective 结构、菜单过滤、五类非法目标回问与规则回退单测（verified: offline）。

覆盖：
1. 规则战略与模型战略产出同一 `Objective` 类型，且可在 `CampaignPlanner` 中直接互换；
2. `budget_s` 缺省读取台账默认预算，显式传入时夹紧在 [60, 1800]；
3. 五类非法目标（不存在、已达成、被环境挂起、stage 不够、展开不出技能）各自把错误原文回喂重问一次，
   第二次仍非法时回退规则 `Objective`；
4. `min_verified="live"` 时只出现并允许 `graduated` / `live` 目标（排除 `candidate` 与 `draft`）。
"""
from __future__ import annotations

from dataclasses import replace
import json
import unittest

from mcagent.loop import GOAL_MAP, GOALS, CampaignPlanner, RulePlanner, reset_world_memo
from mcagent.loop.goals import GoalSpec
from mcagent.loop.starter_skills import GatherWood, ScoutIsland
from mcagent.planner import llm as llm_mod
from mcagent.planner.llm import LLMClient
from mcagent.planner.objective import (
    MAX_BUDGET_S,
    MIN_BUDGET_S,
    Objective,
    choose_objective,
    collect_objective_candidates,
    from_objective_choice,
    validate_objective,
)
from mcagent.runtime import EnvBlocked, State
from mcagent.skills import Skill
from mcagent.skills import base as skill_base
from mcagent.skills.base import rec


def _llm_reply(payload: str, prompt_tokens: int = 180, completion_tokens: int = 24) -> tuple[int, str]:
    return 200, json.dumps({
        "choices": [{"message": {"role": "assistant", "content": payload}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }, ensure_ascii=False)


class RecordingPoster:
    def __init__(self, replies: list[tuple[int, str]]):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, url: str, headers: dict, data: str, timeout: float, proxy):
        self.calls.append(json.loads(data))
        if not self.replies:
            raise AssertionError("预置假 LLM 回复已耗尽")
        return self.replies.pop(0)


class BlockedSkill(Skill):
    name = "always_blocked_skill"
    description = "测试用受阻技能"
    goal = "test:blocked"
    verified = "offline"

    @classmethod
    def blocked(cls, state: State) -> str:
        return "背包缺少测试钥匙"

    def run(self, ex, state):
        yield rec("noop")


class LiveGatherWood(GatherWood):
    verified = "live"


class LiveScout(ScoutIsland):
    verified = "live"


class ObjectiveValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        llm_mod._reset_semaphores()
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        self.state = State()
        self.state.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "is_day": True})
        self.state.set_inventory({"items": []})

    def tearDown(self) -> None:
        llm_mod._reset_semaphores()
        skill_base._COOLDOWN.clear()

    def _make_llm(self, replies: list[str]) -> tuple[LLMClient, RecordingPoster]:
        poster = RecordingPoster([_llm_reply(r) for r in replies])
        client = LLMClient(key="fake-key", fast_proxy="", poster=poster, log=None)
        return client, poster

    def test_rule_and_llm_produce_same_objective_type_and_are_interchangeable(self) -> None:
        clock_now = 1000.0
        planner = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline", clock=lambda: clock_now)
        rule_obj = planner.rule_objective(self.state, now=clock_now)
        self.assertIsInstance(rule_obj, Objective)
        assert rule_obj is not None
        self.assertEqual(rule_obj.source, "rule")

        llm, _ = self._make_llm(['{"goal_id": "mine_stone", "budget_s": 140, "why": "先拿圆石5分"}'])
        ctx_text = planner.build_context(self.state, now=clock_now).render()
        llm_obj, errs, used = choose_objective(llm, ctx_text, self.state, min_verified="offline", now=clock_now)
        self.assertEqual((errs, used), ([], 1))
        self.assertIsInstance(llm_obj, Objective)
        assert llm_obj is not None
        self.assertEqual(llm_obj.source, "llm")
        self.assertIs(type(rule_obj), type(llm_obj))

        # 两种来源的 Objective 可直接互换交给同一战术层展开技能
        planner.set_objective(llm_obj, now=clock_now)
        d_from_llm = planner.select(self.state, [])
        self.assertIsNotNone(d_from_llm.skill)
        self.assertEqual(d_from_llm.skill.name, "gather_wood")
        self.assertEqual(d_from_llm.objective, llm_obj)
        self.assertEqual(d_from_llm.source, "rule")

        planner.set_objective(rule_obj, now=clock_now)
        d_from_rule = planner.select(self.state, [])
        self.assertIsNotNone(d_from_rule.skill)
        self.assertEqual(d_from_rule.skill.name, "gather_wood")
        self.assertEqual(d_from_rule.objective, rule_obj)

    def test_budget_defaults_to_ledger_and_clamps_to_bounds(self) -> None:
        obj_default = from_objective_choice(
            {"goal_id": "mine_stone", "why": "默认预算"}, self.state, min_verified="offline", now=10.0
        )
        self.assertEqual(obj_default.budget_s, GOAL_MAP["mine_stone"].budget_s)

        obj_low = from_objective_choice(
            {"goal_id": "mine_stone", "budget_s": 5, "why": "过小夹紧"}, self.state, min_verified="offline"
        )
        self.assertEqual(obj_low.budget_s, MIN_BUDGET_S)

        obj_high = from_objective_choice(
            {"goal_id": "mine_stone", "budget_s": 7200, "why": "过大夹紧"}, self.state, min_verified="offline"
        )
        self.assertEqual(obj_high.budget_s, MAX_BUDGET_S)

        for bad_budget in ("120", True, float("nan")):
            ok, errs = validate_objective(
                {"goal_id": "mine_stone", "budget_s": bad_budget}, self.state, min_verified="offline"
            )
            self.assertFalse(ok)
            self.assertTrue(any("budget_s" in e for e in errs))

        ok_coord, errs_coord = validate_objective(
            {"goal_id": "mine_stone", "pos": [1, 64, 2]}, self.state, min_verified="offline"
        )
        self.assertFalse(ok_coord)
        self.assertTrue(any("坐标" in e for e in errs_coord))

    def test_five_invalid_cases_reask_with_feedback_and_fallback_to_rule(self) -> None:
        # 准备自定义台账覆盖全部 5 类非法情况：
        # 1) 不存在：no_such_goal
        # 2) 已达成：prep_wood（已注入原木）
        # 3) 被环境挂起：susp_goal
        # 4) stage 不够：draft_goal (stage="draft")
        # 5) 展开不出技能：blocked_goal (技能 blocked 非空，即便 scout_island 存在也必须判不可展开)
        achieved_state = State()
        achieved_state.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "pos": [0.5, 64.0, 0.5]})
        achieved_state.set_inventory({"items": [
            {"name": "oak_log", "count": 4, "slot": 0},
            {"name": "wooden_pickaxe", "count": 1, "slot": 1},
        ]})

        susp_goal = GoalSpec(
            id="susp_goal", title="挂起目标", tier="T1", score=10,
            skill="gather_wood", skill_args=(("count", 2),), stage="graduated",
        )
        draft_goal = GoalSpec(
            id="draft_goal", title="草稿目标", tier="T1", score=20,
            skill="mine_stone", skill_args=(("count", 3),), stage="draft",
        )
        blocked_goal = GoalSpec(
            id="blocked_goal", title="受阻目标", tier="T1", score=15,
            skill="always_blocked_skill", stage="graduated",
        )
        valid_goal = GOAL_MAP["mine_stone"]
        ledger = (GOAL_MAP["prep_wood"], GOAL_MAP["prep_wooden_pickaxe"], valid_goal, susp_goal, draft_goal, blocked_goal)

        from mcagent.skills import REGISTRY
        reg = {**REGISTRY, "always_blocked_skill": BlockedSkill}
        rp = RulePlanner(registry=reg, ledger=ledger, min_verified="offline")
        rp.record_environment_block(
            GatherWood(count=2),
            EnvBlocked("附近无树", missing_predicate="exposed_log", searched_volume="半径 32", wake_on="chunk_moved"),
            achieved_state,
        )
        rule_fallback = Objective(goal_id="mine_stone", budget_s=120.0, why="规则保底", source="rule", started_at=50.0)

        cases = [
            ("不存在", '{"goal_id": "nonexistent_dragon", "why": "瞎编"}', "不在目标台账内"),
            ("已达成", '{"goal_id": "prep_wood", "why": "重复砍树"}', "已达成"),
            ("被挂起", '{"goal_id": "susp_goal", "why": "硬做挂起项"}', "环境挂起"),
            ("stage不够", '{"goal_id": "draft_goal", "why": "选草稿项"}', "stage='draft'"),
            ("展开不出技能", '{"goal_id": "blocked_goal", "why": "缺钥匙"}', "缺少测试钥匙"),
        ]

        for label, bad_json, expected_substr in cases:
            with self.subTest(case=label, mode="reask_then_fix"):
                llm, poster = self._make_llm([
                    bad_json,
                    '{"goal_id": "mine_stone", "budget_s": 130, "why": "修正为合法目标"}',
                ])
                chosen, errs, used = choose_objective(
                    llm,
                    "当前上下文",
                    achieved_state,
                    ledger=ledger,
                    registry=reg,
                    min_verified="offline",
                    environment_blocks=rp._environment_blocks,
                    fallback=lambda: rule_fallback,
                )
                self.assertEqual((errs, used), ([], 2))
                assert chosen is not None
                self.assertEqual((chosen.goal_id, chosen.source, chosen.budget_s), ("mine_stone", "llm", 130.0))
                second_user_msg = poster.calls[1]["messages"][-1]["content"]
                self.assertIn(expected_substr, second_user_msg)

            with self.subTest(case=label, mode="reask_still_bad_falls_back_to_rule"):
                llm2, _ = self._make_llm([bad_json, bad_json])
                chosen2, errs2, used2 = choose_objective(
                    llm2,
                    "当前上下文",
                    achieved_state,
                    ledger=ledger,
                    registry=reg,
                    min_verified="offline",
                    environment_blocks=rp._environment_blocks,
                    fallback=lambda: rule_fallback,
                )
                self.assertEqual(used2, 2)
                self.assertTrue(any(expected_substr in e for e in errs2), errs2)
                assert chosen2 is not None
                self.assertEqual((chosen2.goal_id, chosen2.source), ("mine_stone", "rule"))

    def test_min_verified_live_only_exposes_and_accepts_graduated_or_live_goals(self) -> None:
        ledger = (
            replace(GOAL_MAP["prep_wood"], stage="graduated", skill="gather_wood"),
            GoalSpec(id="live_goal", title="已真机验证目标", tier="T1", score=15, skill="scout_island", stage="live"),
            GoalSpec(id="cand_goal", title="候选目标", tier="T1", score=30, skill="scout_island", stage="candidate"),
            GoalSpec(id="draft_goal", title="草稿目标", tier="T1", score=60, skill="scout_island", stage="draft"),
        )
        reg = {"gather_wood": LiveGatherWood, "scout_island": LiveScout}

        eligible, _ = collect_objective_candidates(
            self.state, ledger=ledger, registry=reg, min_verified="live"
        )
        self.assertEqual([c.goal_id for c in eligible], ["prep_wood", "live_goal"])

        for ok_id in ("prep_wood", "live_goal"):
            ok, errs = validate_objective({"goal_id": ok_id}, self.state, ledger=ledger, registry=reg, min_verified="live")
            self.assertTrue(ok, errs)

        for rejected_id in ("cand_goal", "draft_goal"):
            ok, errs = validate_objective(
                {"goal_id": rejected_id}, self.state, ledger=ledger, registry=reg, min_verified="live"
            )
            self.assertFalse(ok)
            self.assertTrue(any("stage=" in e for e in errs), errs)


if __name__ == "__main__":
    unittest.main()
