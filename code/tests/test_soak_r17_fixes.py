"""Regression tests for Run-17 soak fix: force advancement sync after advancement-gated skill completes in campaign mode."""
from __future__ import annotations

import unittest

from mcagent.loop.engine import LoopConfig, MainLoop
from mcagent.loop.policy import CampaignPlanner
from mcagent.runtime import Executor, RuntimeConfig, State
from mcagent.skills.base import Skill, rec
from mcagent.skills.world_memo import reset_all_world_memos


class _FakeObserver:
    def __init__(self):
        self.server_advancements: set[str] = set()
        self.tape = None
        self.force_ticks: list[bool] = []
        self.target_goals: tuple[str, ...] = ()

    def on_run_start(self, *args, **kwargs):
        pass

    def on_run_end(self, *args, **kwargs):
        pass

    def on_skill_start(self, *args, **kwargs):
        pass

    def on_step(self, *args, **kwargs):
        pass

    def on_skill_end(self, *args, **kwargs):
        pass

    def on_progress_tick(self, state, client=None, force=False):
        self.force_ticks.append(bool(force))
        if force and client is not None:
            adv = client.advancements(status="completed", include_recipes=False)
            for item in (adv.get("advancements") or []):
                if isinstance(item, dict) and item.get("id"):
                    self.server_advancements.add(item["id"])

    def update_goals_reached(self, state):
        pass


class _CampaignAdvClient:
    def __init__(self):
        self.adv_calls = 0
        self.planted = False

    def wait_ready(self, timeout=120):
        return self.status()

    def status(self):
        return {
            "pos": [0.5, 64.0, 0.5],
            "dimension": "overworld",
            "ready": True,
            "alive": True,
            "health": 20.0,
            "food": 20.0,
            "is_day": True,
            "nearest_hostile": None,
        }

    def inventory(self):
        return {"items": [], "armor": {}, "offhand": None, "held": None}

    def events(self, since=0, limit=50):
        return {"events": [], "next_seq": since}

    def advancements(self, status="completed", include_recipes=False):
        self.adv_calls += 1
        if self.planted:
            return {"advancements": [{"id": "minecraft:husbandry/plant_seed", "done": True}]}
        return {"advancements": []}


class _InstantPlantSkill(Skill):
    name = "plant_seed"
    goal = "goal:plant_seed"
    verified = "offline"

    def run(self, ex, state):
        ex.client.planted = True
        yield rec("verify_crop", {"crop": "wheat"})


class _OneShotPlanner(CampaignPlanner):
    def __init__(self):
        super().__init__(mode="campaign", strategy="rule", min_verified="offline")
        self.calls = 0

    def select(self, state, recent_failures=()):
        from mcagent.loop.policy import PlanDecision
        self.calls += 1
        if "minecraft:husbandry/plant_seed" in self.rules.completed_advancements:
            return PlanDecision(None, "idle", "done", stop_episode=True)
        return PlanDecision(_InstantPlantSkill(), "rule", "plant seed test")


class TestSoakR17Fixes(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()

    def tearDown(self):
        reset_all_world_memos()

    def test_campaign_mode_forces_advancement_sync_after_plant_seed_completes(self):
        client = _CampaignAdvClient()
        state = State(RuntimeConfig())
        ex = Executor(client, state)
        planner = _OneShotPlanner()
        obs = _FakeObserver()
        engine = MainLoop(
            client,
            state,
            ex,
            planner,
            config=LoopConfig(mode="campaign", max_steps=5, enable_monitor=False, continuous_observe=False),
            observer=obs,
            log=lambda *_: None,
            sleep=lambda *_: None,
        )
        res = engine.run()
        self.assertIn("minecraft:husbandry/plant_seed", planner.rules.completed_advancements)
        self.assertTrue(any(obs.force_ticks), f"expected force=True progress tick, got {obs.force_ticks}")
        self.assertEqual(res.skills_completed, 1)


if __name__ == "__main__":
    unittest.main()
