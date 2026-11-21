"""Run-01 (soak8h_r01) 真机复盘最小 TDD 回归单测：busy 逃生自愈、interrupted 释放动作锁与 eat_food 状态刷新。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mcagent.client import ApiError
from mcagent.loop.engine import LoopConfig, MainLoop
from mcagent.loop.policy import PlanDecision
from mcagent.loop.starter_skills import EatFood
from mcagent.runtime import Executor, State
from mcagent.skills import base as skill_base
from mcagent.skills.base import Skill
from mcagent.skills.safety_escape import EmergencyEscape
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches


class SoakR01FixesTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_voxel_caches()
        skill_base._COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_all_voxel_caches()
        skill_base._COOLDOWN.clear()

    def test_emergency_escape_recovers_from_transient_busy_via_stop_and_retry(self) -> None:
        blocks = {(x, 63, z): "stone" for x in range(-14, 15) for z in range(-14, 15)}

        class _BusyOnceWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 64.0, 0.5], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=4.0,
                    nearest_hostile={"id": 88, "name": "creeper", "dist": 4.5, "pos": [0.5, 64.0, 5.0]},
                )
                self.move_attempts = 0
                self.stop_calls = 0

            def stop(self):
                self.stop_calls += 1
                return {"stopped": True, "released": True}

            def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
                self.move_attempts += 1
                if self.move_attempts == 1:
                    raise ApiError("busy", "Another action (craft) is currently executing", {"busy": "craft"})
                return super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)

        w = _BusyOnceWorld()
        ex, state = w.session()
        steps = drive(EmergencyEscape(intent="flee"), ex, state)
        self.assertGreaterEqual(w.stop_calls, 1)
        self.assertGreaterEqual(w.move_attempts, 2)
        self.assertTrue(any(s.get("error_code") == "busy" for s in steps))
        self.assertTrue(any(s.get("op") == "move_to" and s.get("error_code") is None for s in steps))

    def test_engine_releases_lock_on_interrupted_and_never_cools_down_emergency_escape_on_busy(self) -> None:
        class _BusyEscapeStub(Skill):
            name = "emergency_escape"
            description = "stub"
            goal = "safety:flee"
            cooldown_s = 10.0
            verified = "offline"

            def run(self, ex, state):
                yield {"op": "move_to"}
                raise ApiError("busy", "Another action (craft) is currently executing")

        class _FakeClient:
            def __init__(self):
                self.stop_calls = 0

            def wait_ready(self, timeout=120):
                return {"ready": True, "alive": True, "health": 20.0, "food": 20.0, "pos": [0.5, 64.0, 0.5], "dimension": "overworld"}

            def status(self):
                return {"ready": True, "alive": True, "health": 20.0, "food": 20.0, "pos": [0.5, 64.0, 0.5], "dimension": "overworld"}

            def inventory(self):
                return {"items": [], "empty_slots": 36}

            def events(self, since=0, limit=50):
                return {"events": [], "next": since}

            def stop(self):
                self.stop_calls += 1
                return {"stopped": True, "released": True}

        class _Planner:
            mode = "campaign"
            rules = SimpleNamespace(active_goals=(), completed_advancements=set(), ledger_map={})

            def __init__(self):
                self.calls = 0

            def select(self, state, recent_failures):
                self.calls += 1
                return PlanDecision(_BusyEscapeStub(), "rule", "test", safety_escape=True)

            def blocked_targets(self, *args, **kwargs):
                return {}

            def release_skill_materials(self):
                pass

        client = _FakeClient()
        state = State()
        with tempfile.TemporaryDirectory() as td:
            ex = Executor(client, state, log_path=str(Path(td) / "actions.jsonl"))
            loop = MainLoop(
                client, state, ex, _Planner(),
                config=LoopConfig(enable_monitor=False, enable_roadside_scan=False, max_steps=2),
                log=lambda *_: None,
            )
            loop.run()
        self.assertGreaterEqual(client.stop_calls, 1)
        self.assertNotIn("safety:flee", skill_base._COOLDOWN)

    def test_eat_food_refreshes_inventory_and_status_for_verify_done(self) -> None:
        class _FoodWorld(GridWorld):
            def __init__(self):
                super().__init__(pos=[0.5, 64.0, 0.5], items={"cooked_beef": 2})
                self.st.update(dimension="overworld", ready=True, alive=True, health=10.0, food=12.0)

            def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None):
                self.items[item] = max(0, self.items.get(item, 0) - 1)
                self.st["food"] = 20.0
                self.calls.append(("use_item", {"item": item}))
                return {"used": item}

        w = _FoodWorld()
        ex, state = w.session()
        before = {"cooked_beef": 2}
        skill = EatFood()
        drive(skill, ex, state)
        self.assertEqual(state.st["food"], 20.0)
        self.assertEqual(state.count("cooked_beef"), 1)
        self.assertEqual(skill.verify_done(state, before), "")


if __name__ == "__main__":
    unittest.main()
