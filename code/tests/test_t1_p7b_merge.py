"""Exercise merged material dispatch and Executor recovery through real strategies."""
from __future__ import annotations

import unittest
from unittest.mock import Mock

from mcagent.client import Client, Config
from mcagent.loop.goals import GoalSpec
from mcagent.loop.policy import STRATEGIES, CampaignPlanner
from mcagent.planner.llm import LLMClient
from mcagent.planner.objective import Objective, validate_objective
from mcagent.runtime import Executor, State
from mcagent.skills.iron_gear import CraftIronGear
from test_d0_recovery_and_preempt import FakeResponse, FakeSession, TIMEOUT_ERR


_GOAL = GoalSpec(
    id="craft_tool",
    title="制作打火石",
    tier="T1",
    score=1,
    skill="craft_iron_gear",
    skill_args=(("item", "flint_and_steel"),),
    done_items=("flint_and_steel>=1",),
    stage="graduated",
)
_REGISTRY = {CraftIronGear.name: CraftIronGear}
_STATUS = {"ready": True, "alive": True, "busy": None, "dimension": "overworld"}


def _inventory(counts):
    return {"items": [{"name": name, "count": count, "slot": slot}
                      for slot, (name, count) in enumerate(counts.items()) if count > 0]}


def _state(counts):
    state = State()
    state.set_status(_STATUS.copy())
    state.set_inventory(_inventory(counts))
    return state


def _planner(strategy, **kwargs):
    return CampaignPlanner(strategy=strategy, ledger=(_GOAL,), registry=_REGISTRY, **kwargs)


class P7bIntegrationTests(unittest.TestCase):
    def test_rule_and_legacy_reject_reserved_stock_and_model_target_cannot_expand(self):
        for strategy in ("rule", "legacy"):
            with self.subTest(strategy=strategy):
                state = _state({"iron_ingot": 1, "flint": 1})
                planner = _planner(strategy)
                book = planner.reservations
                self.assertTrue(book.reserve(state, "existing_plan", "iron_ingot", 1))
                self.assertIsNone(planner.select(state, []).skill)
                self.assertEqual([entry.consumer for entry in book.entries], ["existing_plan"])
                self.assertEqual(planner.build_context(state).eligible_goals, ())

                def validate():
                    return validate_objective({"goal_id": _GOAL.id}, state, ledger=(_GOAL,),
                                              registry=_REGISTRY, reservation_book=book)

                valid, errors = validate()
                self.assertFalse(valid)
                self.assertIn("物资被预留", errors[0])
                book.release("existing_plan")
                self.assertEqual(validate(), (True, []))
                self.assertEqual(planner.select(state, []).skill.args, {"item": "flint_and_steel"})

    def test_each_strategy_dispatches_through_timeout_recovery_without_resending(self):
        for strategy in STRATEGIES:
            with self.subTest(strategy=strategy):
                inv = {"iron_ingot": 1, "flint": 1}
                state = _state(inv)
                calls = []

                def handler(method, url, headers, body):
                    endpoint = url.rsplit("/", 1)[-1]
                    calls.append(endpoint)
                    if endpoint == "inventory":
                        return FakeResponse(200, {"ok": True, "result": _inventory(inv)})
                    if endpoint == "status":
                        return FakeResponse(200, {"ok": True, "result": _STATUS})
                    if endpoint == "craft":
                        self.assertEqual((method, body["item"], body["count"]), ("POST", "flint_and_steel", 1))
                        self.assertEqual(inv, {"iron_ingot": 1, "flint": 1})
                        inv.clear()
                        inv["flint_and_steel"] = 1
                        return FakeResponse(200, TIMEOUT_ERR)
                    self.fail(f"意外请求 {method} {endpoint}")

                cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
                client = Client(cfg, log=lambda *_: None)
                client.token = "tok"
                client.session = FakeSession(handler)
                client._limiter = Mock()
                ex = Executor(client, state, log=lambda *_: None)
                no_request = Mock(side_effect=AssertionError("已核验战略目标不应请求模型"))
                llm = LLMClient(key="k", fast_proxy="", poster=no_request, log=None)
                planner = _planner(strategy, llm=llm, clock=lambda: 10.0)
                if strategy == "llm":
                    planner.set_objective(Objective(_GOAL.id, 180.0, source="llm", started_at=10.0))
                decision = planner.select(state, [])
                if strategy == "llm":
                    self.assertEqual(decision.objective_source, "llm")
                skill = decision.skill
                self.assertIsInstance(skill, CraftIronGear)
                self.assertFalse(skill.step(ex, state))
                self.assertTrue(skill.last["result"]["recovered"])
                self.assertTrue(skill.step(ex, state))
                self.assertEqual(calls.count("craft"), 1)
                start = calls.index("craft")
                self.assertEqual(calls[start + 1:start + 3], ["status", "inventory"])
                self.assertEqual(inv, {"flint_and_steel": 1})
                planner.record_skill_outcome(skill.name, ok=True)
                self.assertEqual(planner.reservations.entries, ())
                no_request.assert_not_called()
