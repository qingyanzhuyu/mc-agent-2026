"""5h soak seg-07 regression tests: scout_island pit bounce loop (INC-009) & stale posture leak audit (INC-007)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.loop import LoopConfig, MainLoop, reset_world_memo
from mcagent.loop.auditor import SoakObserver
from mcagent.loop.starter_skills import MINE_TRAILS, RECENT_RECOVERY_LANDINGS
from mcagent.runtime import Replan
from mcagent.skills import Skill, make
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import drive
from mcagent.skills import nav as nav_mod
from tests.test_loop_soak import RichGridWorld


class PitBounceWorld(RichGridWorld):
    """Simulates a 2-block-wide surface pit where rng=1 move_to within 1.05 blocks is a no-op (like bridge GoalNear)."""

    def __init__(self, *, blocks, failures=None, dig_errors=None, items=None):
        super().__init__(blocks=blocks, items=items or {})
        self.failures = failures or {}
        self.dig_errors = dig_errors or {}

    def move_to(self, *args, **kwargs):
        dest = tuple(kwargs["pos"])
        rng = float(kwargs.get("rng", kwargs.get("range", 1)))
        if dest in self.failures:
            self.calls.append(("move_to", {"pos": list(dest)}))
            raise ApiError(self.failures[dest], "injected pit move failure")
        cur = self.st["pos"]
        dx, dy, dz = dest[0] - cur[0], dest[1] - cur[1], dest[2] - cur[2]
        dist = (dx * dx + dy * dy + dz * dz) ** 0.5
        if dist <= max(1.0, rng) + 0.05:
            # Mirror bridge GoalNear(tx, ty, tz, Math.max(1, rng)): already within range -> no movement.
            self.calls.append(("move_to", {"pos": list(dest)}))
            return {"pos": list(cur)}
        return super().move_to(*args, **kwargs)

    def dig(self, pos, **kwargs):
        cell = tuple(int(v) for v in pos)
        if cell in self.dig_errors:
            err = self.dig_errors.pop(cell)
            self.blocks.pop(cell, None)
            raise ApiError(err, f"injected dig error at {cell}")
        return super().dig(pos, **kwargs)


class Seg07ScoutPitEscapeTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_scout_island_does_not_loop_on_half_block_pit_bounce_and_escapes_2_wide_pit(self):
        # Reproduce INC-009: bot starts at [-51.6, 69.0, -28.5], stale MINE_TRAILS top [-54.5, 73.0, -29.5] fails,
        # 3 land candidates fail, recovery drops bot 0.1m down to [-52.6, 68.9, -28.5],
        # staircase step 0 walks back onto [-51.5, 69.0, -28.5] (no real Y gain over 69.0),
        # and next step faces a 1-block gap at (-52, 69, -28) with a climbable wall at (-52, 69, -27).
        blocks = {
            (-56, 69, -36): "grass_block",
            (-52, 68, -37): "grass_block",
            (-49, 69, -36): "grass_block",
            # Pit floor
            (-53, 68, -29): "stone",
            (-52, 68, -29): "stone",
            (-52, 68, -28): "stone",
            # Pit rim at Y=69 around (-52, 69, -28) -> south wall at z=-27
            (-52, 69, -27): "dirt",
            (-52, 70, -27): "dirt",
            (-52, 71, -27): "dirt",
        }
        failures = {
            (-54.5, 73.0, -29.5): "path_not_found",
            (-55.5, 70, -35.5): "path_not_found",
            (-51.5, 69, -36.5): "path_not_found",
            (-48.5, 70, -35.5): "path_not_found",
        }
        world = PitBounceWorld(blocks=blocks, failures=failures, items={"stone_pickaxe": 1})
        world.st["pos"] = [-51.6, 69.0, -28.5]
        MINE_TRAILS.append([[-54.5, 73.0, -29.5], [-51.5, 69.0, -28.5]])
        RECENT_RECOVERY_LANDINGS.extend([(-52.5, 68.9, -28.5), (-52.6, 68.9, -28.5)])

        def _fake_recover(ex_in, state_in, dest=None, *, protected_fn=None):
            world.st["pos"] = [-52.6, 68.9, -28.5]
            yield {"op": "move_to_recover", "pos": [-52.5, 68.0, -28.5], "result": {"pos": [-52.6, 68.9, -28.5]}}

        ex, state = world.session()
        with patch.object(nav_mod, "recover_move", side_effect=_fake_recover):
            drive(make("scout_island", {"radius": 36}), ex, state)

        # Stale unreachable surface trail must be popped instead of replaying forever.
        self.assertEqual(MINE_TRAILS, [])
        # Must genuinely climb ABOVE the starting pit elevation Y=69.0 (not stop at 69.0).
        self.assertGreaterEqual(state.pos[1], 70.0)
        self.assertEqual(list(RECENT_RECOVERY_LANDINGS), [])

    def test_scout_island_ignores_nothing_to_dig_during_staircase_climb(self):
        blocks = {
            (10, 72, 0): "grass_block",
            (14, 72, 0): "grass_block",
            (18, 72, 0): "grass_block",
            (0, 66, 0): "stone",
            (0, 66, 1): "stone",
            (1, 66, 0): "stone", (1, 67, 0): "stone", (1, 68, 0): "stone", (1, 69, 0): "sand",
            (1, 66, 1): "stone", (1, 67, 1): "stone", (1, 68, 1): "stone", (1, 69, 1): "sand",
        }
        failures = {(x + 0.5, 73, 0.5): "path_not_found" for x in (10, 14, 18)}
        world = PitBounceWorld(
            blocks=blocks,
            failures=failures,
            dig_errors={(1, 69, 1): "nothing_to_dig"},
            items={"stone_pickaxe": 1},
        )
        world.st["pos"] = [0.5, 67.0, 0.5]
        RECENT_RECOVERY_LANDINGS.extend([(0.5, 67.0, 0.5), (0.5, 67.0, 1.5)])

        def _fake_recover(ex_in, state_in, dest=None, *, protected_fn=None):
            world.st["pos"] = [0.5, 67.0, 1.5]
            yield {"op": "move_to_recover", "pos": [0.5, 67.0, 1.5], "result": {"pos": [0.5, 67.0, 1.5]}}

        ex, state = world.session()
        with patch.object(nav_mod, "recover_move", side_effect=_fake_recover):
            drive(make("scout_island"), ex, state)
        self.assertGreaterEqual(state.pos[1], 68.0)

    def test_scout_island_replans_instead_of_completing_when_pit_climb_gains_no_elevation(self):
        # Pit surrounded by bedrock walls and no placeable blocks: stepping from 68.9 back to 69.0
        # must NOT clear RECENT_RECOVERY_LANDINGS or falsely complete scout_island.
        blocks = {
            (-56, 69, -36): "grass_block",
            (-52, 68, -37): "grass_block",
            (-49, 69, -36): "grass_block",
            (-53, 68, -29): "stone",
            (-52, 68, -29): "stone",
            (-51, 69, -29): "bedrock",
            (-52, 69, -28): "bedrock",
            (-52, 69, -30): "bedrock",
            (-53, 69, -29): "bedrock",
            (-54, 68, -29): "bedrock",
            (-53, 68, -28): "bedrock",
            (-53, 68, -30): "bedrock",
        }
        failures = {
            (-55.5, 70, -35.5): "path_not_found",
            (-51.5, 69, -36.5): "path_not_found",
            (-48.5, 70, -35.5): "path_not_found",
        }
        world = PitBounceWorld(blocks=blocks, failures=failures, items={"stone_pickaxe": 1})
        world.st["pos"] = [-51.6, 69.0, -28.5]
        RECENT_RECOVERY_LANDINGS.extend([(-52.5, 68.9, -28.5), (-52.6, 68.9, -28.5)])

        def _fake_recover(ex_in, state_in, dest=None, *, protected_fn=None):
            world.st["pos"] = [-52.6, 68.9, -28.5]
            yield {"op": "move_to_recover", "pos": [-52.5, 68.0, -28.5], "result": {"pos": [-52.6, 68.9, -28.5]}}

        ex, state = world.session()
        with patch.object(nav_mod, "recover_move", side_effect=_fake_recover):
            with self.assertRaises(Replan):
                drive(make("scout_island", {"radius": 36}), ex, state)
        self.assertEqual(len(RECENT_RECOVERY_LANDINGS), 2)


class _FakePlaceCraftSkill(Skill):
    name = "craft_gear"
    goal = "craft:stone_pickaxe"
    cooldown_s = 1.0
    verified = "live"

    def run(self, ex, state):
        # Simulate background monitor polling /status mid-place while bridge temporarily holds sneak=True.
        state.st["sneaking"] = True
        state.st["busy"] = "place"
        yield {"op": "craft", "item": "stone_pickaxe", "result": {"crafted": "stone_pickaxe"}}


class Seg07PostureLeakFalsePositiveTests(unittest.TestCase):
    def test_async_observe_refreshes_stale_sneaking_status_before_posture_leak_audit(self):
        world = RichGridWorld(blocks={(0, 63, 0): "grass_block"})
        world.st["sneaking"] = False
        world.st["busy"] = None
        ex, state = world.session()

        class _SingleSkillPlanner:
            mode = "campaign"
            def __init__(self):
                self.ran = False

            def blocked_targets(self, *args, **kwargs):
                return {}

            def select(self, st, recent_failures=()):
                from mcagent.loop.policy import PlanDecision
                if not self.ran:
                    self.ran = True
                    return PlanDecision(_FakePlaceCraftSkill(), "rule", "test")
                return PlanDecision(None, "idle", "done")

        with tempfile.TemporaryDirectory() as td:
            obs = SoakObserver(run_dir=Path(td), source="local-target")
            eng = MainLoop(
                world,
                state,
                ex,
                _SingleSkillPlanner(),
                config=LoopConfig(max_steps=2, max_seconds=0.1, enable_monitor=True, async_observe_for_llm=True),
                observer=obs,
                sleep=lambda _: None,
            )
            # Attach a dummy monitor handle so engine takes the async_observe_for_llm fast path.
            class _DummyMonitor:
                def start(self): pass
                def stop(self): pass
                def close(self): pass
                def is_alive(self): return True
                def has_fresh_sample(self, max_age_s=None): return True
            with patch("mcagent.loop.engine.SafetyMonitor", return_value=_DummyMonitor()):
                eng.run()

            leak_incs = list(obs.incidents_dir.glob("*posture_leak*"))
            self.assertEqual(leak_incs, [], f"unexpected false-positive POSTURE_LEAK: {leak_incs}")


if __name__ == "__main__":
    unittest.main()
