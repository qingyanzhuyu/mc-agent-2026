"""ep39: preserve real mining failures without counting its truncated fourth attempt.

The episode had three position-change Replans, successful scouts between them
(3/3/4), then a successful sand dig at the run's 40-step limit. That cancellation
created fingerprint 8a936dfcdc; the real three failures belong to 5c7e937cc1.
The third attempt's recorded collection movement is explicitly injected below,
not simulated physics. Earlier attempts share its diagnostic, not this fixture.
"""
import json
import tempfile
import unittest
from pathlib import Path

from mcagent.loop import CampaignPlanner, LoopConfig, MainLoop, SoakObserver, reset_world_memo
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN, failure
from mcagent.skills.testing import drive
from tests.test_loop_soak import RichGridWorld


class CollectionMovementWorld(RichGridWorld):
    def __init__(self):
        super().__init__(pos=(-7.5, 66, 50.5), blocks={
            (-7, 65, 50): "sand", (-7, 64, 50): "sand",
            (-6, 66, 50): "sand", (-6, 65, 50): "sand",
            (-6, 63, 50): "sandstone",
        }, items={"wooden_pickaxe": 1})
        self.st["held"] = {"name": "wooden_pickaxe"}

    def dig(self, pos, **kwargs):
        result = super().dig(pos, **kwargs)
        if pos == [-6, 66, 50]:
            # ep39 seq305 dig -> seq309 status, before the next lower-block dig.
            self.st["pos"] = [-5.5, 66, 50.5]
        return result


class MiningAccountingTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_ep39_real_replans_survive_scout_successes_without_budget_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = SoakObserver(tmp)
            for scout_count in (3, 3, 4):
                world = CollectionMovementWorld()
                ex, state = world.session()
                skill = make("mine_stone", {"count": 8})
                obs.on_skill_start(skill, "rule", "ep39 position-change failure", state)
                with self.assertRaisesRegex(Replan, "站位.*变化") as caught:
                    drive(skill, ex, state)
                code, _ = failure(caught.exception)
                skill.abort()
                obs.on_skill_end(skill, "replan", state, error_code=code, error_msg=str(caught.exception))
                self.assertEqual(ex.acts, ["dig", "move_to", "dig"])
                self.assertEqual(world.blocks[(-6, 65, 50)], "sand")
                for _ in range(scout_count):
                    scout_world = RichGridWorld(blocks={(6, 63, 0): "grass_block"})
                    scout_ex, scout_state = scout_world.session()
                    scout = make("scout_island")
                    obs.on_skill_start(scout, "rule", "ep39 other-goal success", scout_state)
                    drive(scout, scout_ex, scout_state)
                    obs.on_skill_end(scout, "completed", scout_state)

            # ep39 seq375 was a successful sand dig, followed by max_steps cutoff.
            world = RichGridWorld(pos=(-13.4, 67.1, 33.5), blocks={
                (-15, 68, 33): "sand", (-15, 67, 33): "sand",
                (-15, 65, 33): "sandstone",
            }, items={"wooden_pickaxe": 1})
            world.st["held"] = {"name": "wooden_pickaxe"}
            ex, state = world.session()
            loop = MainLoop(world, state, ex,
                            CampaignPlanner(mode="focus", focus_skill="mine_stone", focus_args={"count": 8}),
                            observer=obs,
                            config=LoopConfig(mode="focus", max_steps=1, enable_monitor=False, idle_sleep_s=0),
                            log=lambda *_: None)
            result = loop.run()
            self.assertEqual((result.steps, result.skills_replan, result.stop_reason),
                             (1, 0, "max_steps_reached"))
            self.assertEqual(ex.acts, ["dig"])
            self.assertEqual(world.items["sand"], 1)
            incidents = [json.loads(p.read_text()) for p in Path(tmp, "incidents").glob("*.json")]
            self.assertEqual(len(incidents), 1)
            self.assertEqual((incidents[0]["fingerprint"], incidents[0]["detail"]["recent_failures"],
                              incidents[0]["count"], incidents[0]["skill_context"]["outcome"]),
                             ("5c7e937cc1", 3, 1, "replan"))
            self.assertEqual(obs.skill_stats["mine_stone"],
                             {"started": 4, "ok": 0, "replan": 3, "budget_stopped": 1, "bug": 0})
            self.assertEqual(obs.skill_stats["scout_island"]["ok"], 10)
            self.assertEqual(obs.env_variance_counts, {"mine_stone:replan": 3})
            timeline = [json.loads(line) for line in Path(tmp, "timeline.jsonl").read_text().splitlines()]
            mining_ends = [r["outcome"] for r in timeline
                           if r["event"] == "skill_end" and r["skill"] == "mine_stone"]
            self.assertEqual(mining_ends, ["replan", "replan", "replan", "budget_stopped"])


if __name__ == "__main__":
    unittest.main()
