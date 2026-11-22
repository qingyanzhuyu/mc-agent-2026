"""Offline deterministic tests for safety_intent & hazard consumption in policy/loop."""
from __future__ import annotations

import dataclasses
import time
import unittest
from typing import Any, Iterator
from unittest.mock import patch

from mcagent.loop import CampaignPlanner, LoopConfig, MainLoop, RulePlanner, reset_world_memo
from mcagent.runtime import Replan, RuntimeConfig, State
from mcagent.skills import Param, Skill, rec
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import CountingExecutor, GridWorld


class _MockEmergencyEscape(Skill):
    name = "emergency_escape"
    description = "Mock emergency escape skill"
    params = {
        "intent": Param(str, "escape intent", choices=("flee", "douse", "surface"), default="flee"),
    }
    goal = "safety:{intent}"
    cooldown_s = 5.0
    verified = "offline"

    def run(self, ex: Any, state: State) -> Iterator[dict]:
        yield rec("move_to", ex.move_to(pos=[8.5, 64.0, 0.5], rng=1), intent=self.args["intent"])


class _MockEmergencyEscapeLive(_MockEmergencyEscape):
    verified = "live"


class _MockScoutIsland(Skill):
    name = "scout_island"
    description = "Mock scout island fallback"
    params = {"radius": Param(int, "scan radius", default=32, min=12, max=64)}
    goal = "explore:island"
    cooldown_s = 12.0
    verified = "offline"

    def run(self, ex: Any, state: State) -> Iterator[dict]:
        yield rec("move_to", ex.move_to(pos=[4.5, 64.0, 0.5], rng=1), radius=self.args["radius"])


class _MockScoutIslandLive(_MockScoutIsland):
    verified = "live"


class _MockGatherWood(Skill):
    name = "gather_wood"
    description = "Mock gather wood normal skill"
    params = {"count": Param(int, "log count", default=3, min=1, max=16)}
    goal = "acq:wood"
    cooldown_s = 20.0
    verified = "offline"

    def run(self, ex: Any, state: State) -> Iterator[dict]:
        yield rec("dig", ex.dig((1, 64, 0)), count=self.args["count"])


class _MockGatherWoodLive(_MockGatherWood):
    verified = "live"


class _MockCraftGear(Skill):
    name = "craft_gear"
    description = "Mock craft gear skill"
    params = {"item": Param(str, "item", default="wooden_pickaxe")}
    goal = "craft:{item}"
    cooldown_s = 10.0
    verified = "offline"

    def run(self, ex: Any, state: State) -> Iterator[dict]:
        yield rec("craft", ex.craft(self.args["item"], 1), item=self.args["item"])


class _ForbiddenLLM:
    """Mock LLM client that fails the test if any LLM call is attempted."""

    def __init__(self) -> None:
        self.calls = 0
        self.stats = {"prompt_tokens": 0, "completion_tokens": 0}

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("LLM.chat should not be called during safety escape")

    def ask_json(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("LLM.ask_json should not be called during safety escape")


class _LoopMockClient(GridWorld):
    def __init__(self, pos=(0.5, 64.0, 0.5), blocks=None, items=None) -> None:
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.wait_ready_calls = 0

    def wait_ready(self, timeout: float = 120) -> dict:
        self.wait_ready_calls += 1
        return self.status()

    def advancements(self, status: str = "completed", include_recipes: bool = False) -> dict:
        return {"advancements": []}


class _FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _make_state(**status_overrides: Any) -> State:
    cfg = dataclasses.replace(RuntimeConfig.load(), status_ttl_s=0.0)
    state = State(cfg)
    st = {
        "ready": True,
        "alive": True,
        "health": 20,
        "food": 20,
        "oxygen": 20,
        "pos": [0.5, 64.0, 0.5],
        "on_ground": True,
        "in_water": False,
        "submerged": False,
        "in_lava": False,
        "on_fire": False,
        "dimension": "overworld",
        "time_of_day": 1000,
    }
    st.update(status_overrides)
    state.set_status(st)
    state.set_inventory({"items": []})
    return state


def _make_registry(
    *,
    escape_cls: type[Skill] | None = _MockEmergencyEscape,
    scout_cls: type[Skill] | None = _MockScoutIsland,
    gather_cls: type[Skill] | None = _MockGatherWood,
) -> dict[str, type[Skill]]:
    reg: dict[str, type[Skill]] = {"craft_gear": _MockCraftGear}
    if escape_cls is not None:
        reg["emergency_escape"] = escape_cls
    if scout_cls is not None:
        reg["scout_island"] = scout_cls
    if gather_cls is not None:
        reg["gather_wood"] = gather_cls
    return reg


class TestSafetyIntentConsumption(unittest.TestCase):
    def setUp(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def test_consumes_dict_and_str_intents_via_rule_and_campaign_planner(self) -> None:
        reg = _make_registry()
        cases = [
            ({"kind": "flee", "why": "zombie 3m", "severity": "soft"}, "flee"),
            ({"kind": "douse", "why": "着火", "severity": "soft"}, "douse"),
            ({"kind": "surface", "why": "氧气 5", "severity": "urgent"}, "surface"),
            ({"kind": "escape_lava", "why": "在岩浆里", "severity": "urgent"}, "douse"),
            ("flee", "flee"),
            ("escape_lava", "douse"),
        ]
        for raw_intent, expected_intent in cases:
            with self.subTest(raw_intent=raw_intent):
                state = _make_state()
                state.safety_intent = raw_intent
                rule_planner = RulePlanner(registry=reg, min_verified="offline")
                dec_rule = rule_planner.choose(state)
                self.assertIsNotNone(dec_rule.skill)
                self.assertEqual(dec_rule.skill.name, "emergency_escape")
                self.assertEqual(dec_rule.skill.args["intent"], expected_intent)
                self.assertTrue(dec_rule.safety_escape)

                camp_planner = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline", registry=reg)
                dec_camp = camp_planner.select(state, [])
                self.assertIsNotNone(dec_camp.skill)
                self.assertEqual(dec_camp.skill.name, "emergency_escape")
                self.assertEqual(dec_camp.skill.args["intent"], expected_intent)
                self.assertTrue(dec_camp.safety_escape)

    def test_min_verified_live_rejects_offline_emergency_escape_and_falls_back(self) -> None:
        # Case 1: emergency_escape is offline, scout_island is live -> falls back to scout_island
        reg_with_live_scout = _make_registry(
            escape_cls=_MockEmergencyEscape,
            scout_cls=_MockScoutIslandLive,
            gather_cls=_MockGatherWoodLive,
        )
        state = _make_state()
        state.set_safety_intent({"kind": "flee", "why": "zombie 3m", "severity": "soft"})

        rule_planner = RulePlanner(registry=reg_with_live_scout, min_verified="live")
        dec_rule = rule_planner.choose(state)
        self.assertIsNotNone(dec_rule.skill)
        self.assertEqual(dec_rule.skill.name, "scout_island")
        self.assertEqual(dec_rule.skill.args["radius"], 24)
        self.assertTrue(dec_rule.safety_escape)

        camp_planner = CampaignPlanner(mode="campaign", strategy="rule", min_verified="live", registry=reg_with_live_scout)
        dec_camp = camp_planner.select(state, [])
        self.assertIsNotNone(dec_camp.skill)
        self.assertEqual(dec_camp.skill.name, "scout_island")
        self.assertTrue(dec_camp.safety_escape)

        # Case 2: both emergency_escape and scout_island are offline -> does NOT dispatch unverified skill
        reg_all_offline = _make_registry(
            escape_cls=_MockEmergencyEscape,
            scout_cls=_MockScoutIsland,
            gather_cls=_MockGatherWood,
        )
        rule_offline = RulePlanner(registry=reg_all_offline, min_verified="live")
        dec_idle = rule_offline.choose(state)
        self.assertIsNone(dec_idle.skill)
        self.assertEqual(dec_idle.source, "idle")
        self.assertFalse(dec_idle.safety_escape)

    def test_narrow_registry_without_emergency_escape_falls_back_to_scout_island(self) -> None:
        reg_no_escape = _make_registry(escape_cls=None, scout_cls=_MockScoutIsland, gather_cls=_MockGatherWood)
        for raw_intent, expected_radius in (
            ({"kind": "flee", "why": "skeleton 4m", "severity": "soft"}, 24),
            ({"kind": "surface", "why": "oxygen 6", "severity": "urgent"}, 16),
            ({"kind": "douse", "why": "on_fire", "severity": "soft"}, 16),
            ({"kind": "escape_lava", "why": "in_lava", "severity": "urgent"}, 16),
        ):
            with self.subTest(raw_intent=raw_intent):
                state = _make_state()
                state.set_safety_intent(raw_intent)
                rule_dec = RulePlanner(registry=reg_no_escape, min_verified="offline").choose(state)
                self.assertIsNotNone(rule_dec.skill)
                self.assertEqual(rule_dec.skill.name, "scout_island")
                self.assertEqual(rule_dec.skill.args["radius"], expected_radius)
                self.assertTrue(rule_dec.safety_escape)

                camp_dec = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline", registry=reg_no_escape).select(state, [])
                self.assertIsNotNone(camp_dec.skill)
                self.assertEqual(camp_dec.skill.name, "scout_island")
                self.assertEqual(camp_dec.skill.args["radius"], expected_radius)
                self.assertTrue(camp_dec.safety_escape)


class TestDirectPhysicalHazards(unittest.TestCase):
    def setUp(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def test_direct_hazards_trigger_escape_even_when_safety_intent_is_none(self) -> None:
        reg = _make_registry()
        hazard_cases = [
            ({"in_water": True, "oxygen": 8}, "surface"),
            ({"in_water": True, "oxygen": 10}, "surface"),
            ({"in_lava": True}, "douse"),
            ({"on_fire": True, "in_water": False}, "douse"),
        ]
        for overrides, expected_intent in hazard_cases:
            with self.subTest(overrides=overrides):
                state = _make_state(**overrides)
                self.assertIsNone(state.safety_intent)

                dec_rule = RulePlanner(registry=reg, min_verified="offline").choose(state)
                self.assertIsNotNone(dec_rule.skill)
                self.assertEqual(dec_rule.skill.name, "emergency_escape")
                self.assertEqual(dec_rule.skill.args["intent"], expected_intent)
                self.assertTrue(dec_rule.safety_escape)

                dec_camp = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline", registry=reg).select(state, [])
                self.assertIsNotNone(dec_camp.skill)
                self.assertEqual(dec_camp.skill.name, "emergency_escape")
                self.assertEqual(dec_camp.skill.args["intent"], expected_intent)
                self.assertTrue(dec_camp.safety_escape)

    def test_in_water_with_ample_oxygen_does_not_trigger_surface_escape(self) -> None:
        reg = _make_registry()
        for overrides in (
            {"in_water": True, "oxygen": 16},
            {"in_water": True, "on_fire": True, "oxygen": 20},
        ):
            with self.subTest(overrides=overrides):
                state = _make_state(**overrides)
                self.assertIsNone(state.safety_intent)

                dec_rule = RulePlanner(registry=reg, min_verified="offline").choose(state)
                self.assertIsNotNone(dec_rule.skill)
                self.assertEqual(dec_rule.skill.name, "gather_wood")
                self.assertFalse(dec_rule.safety_escape)

                dec_camp = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline", registry=reg).select(state, [])
                self.assertIsNotNone(dec_camp.skill)
                self.assertEqual(dec_camp.skill.name, "gather_wood")
                self.assertFalse(dec_camp.safety_escape)


class TestPriorityAndStrategyPreemption(unittest.TestCase):
    def setUp(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def test_safe_mode_stays_above_soft_flee_intent(self) -> None:
        reg = _make_registry()
        state = _make_state()
        state.safe_mode_until = time.monotonic() + 60.0
        state.set_safety_intent({"kind": "flee", "why": "zombie 3m", "severity": "soft"})

        rule_dec = RulePlanner(registry=reg, min_verified="offline").choose(state)
        self.assertIsNone(rule_dec.skill)
        self.assertEqual(rule_dec.source, "idle")
        self.assertFalse(rule_dec.safety_escape)
        self.assertIn("safe_mode", rule_dec.reason)

        for strat, use_llm in (("legacy", False), ("rule", False), ("llm", False), ("rule", True)):
            with self.subTest(strategy=strat, use_llm=use_llm):
                llm = _ForbiddenLLM() if (strat == "llm" or use_llm) else None
                planner = CampaignPlanner(
                    mode="campaign",
                    strategy=strat,
                    use_llm=use_llm,
                    llm=llm,
                    require_iron=False,
                    min_verified="offline",
                    registry=reg,
                )
                dec = planner.select(state, [])
                self.assertIsNone(dec.skill)
                self.assertEqual(dec.source, "idle")
                self.assertFalse(dec.safety_escape)
                if llm is not None:
                    self.assertEqual(llm.calls, 0)

    def test_safety_intent_preempts_normal_goals_across_all_strategies_without_llm(self) -> None:
        reg = _make_registry()
        for strat, use_llm in (("legacy", False), ("rule", False), ("llm", False), ("rule", True)):
            with self.subTest(strategy=strat, use_llm=use_llm):
                state = _make_state()
                llm = _ForbiddenLLM() if (strat == "llm" or use_llm) else None
                planner = CampaignPlanner(
                    mode="campaign",
                    strategy=strat,
                    use_llm=use_llm,
                    llm=llm,
                    require_iron=False,
                    min_verified="offline",
                    registry=reg,
                )

                # Normal state selects normal goal first (when not calling LLM in legacy/rule)
                if strat in ("legacy", "rule") and not use_llm:
                    normal_dec = planner.select(state, [])
                    self.assertIsNotNone(normal_dec.skill)
                    self.assertEqual(normal_dec.skill.name, "gather_wood")
                    self.assertFalse(normal_dec.safety_escape)

                # Safety intent preempts immediately across all strategies, with 0 LLM calls
                state.set_safety_intent({"kind": "flee", "why": "zombie 3m", "severity": "soft"})
                esc_dec = planner.select(state, [])
                self.assertIsNotNone(esc_dec.skill)
                self.assertEqual(esc_dec.skill.name, "emergency_escape")
                self.assertEqual(esc_dec.skill.args["intent"], "flee")
                self.assertTrue(esc_dec.safety_escape)
                if llm is not None:
                    self.assertEqual(llm.calls, 0)


class TestMainLoopSafetyIntentAck(unittest.TestCase):
    def setUp(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def test_mainloop_executes_escape_once_clears_intent_and_resumes_normal_goal(self) -> None:
        executed: list[tuple[str, dict[str, Any]]] = []

        class _TrackedEscape(_MockEmergencyEscape):
            def run(self, ex: Any, state: State) -> Iterator[dict]:
                executed.append((self.name, dict(self.args)))
                yield from super().run(ex, state)

        class _TrackedGather(_MockGatherWood):
            def run(self, ex: Any, state: State) -> Iterator[dict]:
                executed.append((self.name, dict(self.args)))
                yield from super().run(ex, state)

        reg = _make_registry(escape_cls=_TrackedEscape, gather_cls=_TrackedGather)
        client = _LoopMockClient(blocks={(1, 64, 0): "oak_log"})
        clock = _FakeClock()
        cfg = dataclasses.replace(RuntimeConfig.load(), status_ttl_s=0.0)
        state = State(cfg)
        state.set_safety_intent({"kind": "flee", "why": "zombie 3m", "severity": "soft"})

        ex = CountingExecutor(client, state)
        planner = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline", registry=reg, clock=clock)
        loop_cfg = LoopConfig(
            mode="campaign",
            max_steps=3,
            idle_sleep_s=0.0,
            enable_monitor=False,
            respawn_poll_s=0.001,
            respawn_timeout_s=0.05,
        )
        loop = MainLoop(
            client,
            state,
            ex,
            planner,
            config=loop_cfg,
            log=lambda *_: None,
            sleep=clock.sleep,
            clock=clock,
        )
        res = loop.run()

        self.assertEqual(res.stop_reason, "max_steps_reached")
        self.assertGreaterEqual(res.skills_completed, 2)
        self.assertEqual(executed[0], ("emergency_escape", {"intent": "flee"}))
        self.assertEqual(executed[1], ("gather_wood", {"count": 3}))
        self.assertEqual(sum(1 for name, _ in executed if name == "emergency_escape"), 1)
        self.assertIsNone(state.safety_intent)

    def test_mainloop_replan_clears_resolved_hazard_intent(self) -> None:
        executed: list[tuple[str, dict[str, Any]]] = []

        class _ReplanEscape(_MockEmergencyEscape):
            def run(self, ex: Any, state: State) -> Iterator[dict]:
                executed.append((self.name, dict(self.args)))
                yield rec("move_to", ex.move_to(pos=[2.5, 64.0, 0.5], rng=1), intent=self.args["intent"])
                raise Replan("lava already cleared")

        class _TrackedGather(_MockGatherWood):
            def run(self, ex: Any, state: State) -> Iterator[dict]:
                executed.append((self.name, dict(self.args)))
                yield from super().run(ex, state)

        reg = _make_registry(escape_cls=_ReplanEscape, gather_cls=_TrackedGather)
        client = _LoopMockClient(blocks={(1, 64, 0): "oak_log"})
        clock = _FakeClock()
        cfg = dataclasses.replace(RuntimeConfig.load(), status_ttl_s=0.0)
        state = State(cfg)
        state.set_safety_intent({"kind": "escape_lava", "why": "in_lava", "severity": "urgent"})

        ex = CountingExecutor(client, state)
        planner = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline", registry=reg, clock=clock)
        loop_cfg = LoopConfig(
            mode="campaign",
            max_steps=2,
            idle_sleep_s=0.0,
            enable_monitor=False,
            respawn_poll_s=0.001,
            respawn_timeout_s=0.05,
        )
        loop = MainLoop(
            client,
            state,
            ex,
            planner,
            config=loop_cfg,
            log=lambda *_: None,
            sleep=clock.sleep,
            clock=clock,
        )
        res = loop.run()

        self.assertEqual(res.stop_reason, "max_steps_reached")
        self.assertEqual(executed[0], ("emergency_escape", {"intent": "douse"}))
        self.assertEqual(executed[1], ("gather_wood", {"count": 3}))
        self.assertIsNone(state.safety_intent)


class TestSafetyIntentExpiration(unittest.TestCase):
    def setUp(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def test_expired_by_expires_at_is_cleared_and_ignored(self) -> None:
        reg = _make_registry()
        state = _make_state()
        state.set_safety_intent({
            "kind": "flee",
            "why": "stale zombie",
            "severity": "soft",
            "expires_at": 999.0,
        })

        with patch("mcagent.loop.policy.time.monotonic", return_value=1000.0):
            dec = RulePlanner(registry=reg, min_verified="offline").choose(state)
        self.assertIsNone(state.safety_intent)
        self.assertIsNotNone(dec.skill)
        self.assertEqual(dec.skill.name, "gather_wood")
        self.assertFalse(dec.safety_escape)

        state.set_safety_intent({
            "kind": "douse",
            "why": "stale fire",
            "severity": "soft",
            "expires_at": 1000.0,
        })
        with patch("mcagent.loop.policy.time.monotonic", return_value=1000.0):
            dec_camp = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline", registry=reg).select(state, [])
        self.assertIsNone(state.safety_intent)
        self.assertIsNotNone(dec_camp.skill)
        self.assertEqual(dec_camp.skill.name, "gather_wood")
        self.assertFalse(dec_camp.safety_escape)

    def test_expired_by_stale_at_over_15s_is_cleared_and_ignored(self) -> None:
        reg = _make_registry()
        state = _make_state()
        # 1000.0 - 984.0 = 16.0 > 15.0 -> expired
        state.set_safety_intent({
            "kind": "flee",
            "why": "old hostile",
            "severity": "soft",
            "at": 984.0,
        })

        with patch("mcagent.loop.policy.time.monotonic", return_value=1000.0):
            dec = RulePlanner(registry=reg, min_verified="offline").choose(state)
        self.assertIsNone(state.safety_intent)
        self.assertIsNotNone(dec.skill)
        self.assertEqual(dec.skill.name, "gather_wood")
        self.assertFalse(dec.safety_escape)

        # Fresh intent (1000.0 - 990.0 = 10.0 <= 15.0) -> still active and dispatches escape
        state.set_safety_intent({
            "kind": "flee",
            "why": "fresh hostile",
            "severity": "soft",
            "at": 990.0,
        })
        with patch("mcagent.loop.policy.time.monotonic", return_value=1000.0):
            dec_fresh = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline", registry=reg).select(state, [])
        self.assertIsNotNone(state.safety_intent)
        self.assertIsNotNone(dec_fresh.skill)
        self.assertEqual(dec_fresh.skill.name, "emergency_escape")
        self.assertEqual(dec_fresh.skill.args["intent"], "flee")
        self.assertTrue(dec_fresh.safety_escape)


if __name__ == "__main__":
    unittest.main()
