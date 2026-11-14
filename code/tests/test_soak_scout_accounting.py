"""ep-0033: budget cancellation and recovered scouts are not consecutive failures."""
import json
import tempfile
import unittest
from pathlib import Path

from mcagent.client import ApiError
from mcagent.loop import CampaignPlanner, LoopConfig, MainLoop, SoakObserver, reset_world_memo
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN, failure
from mcagent.skills.testing import drive
from tests.test_loop_soak import RichGridWorld


class ScoutAccountingWorld(RichGridWorld):
    def __init__(self):
        super().__init__(blocks={(x, 63, 0): "grass_block" for x in (6, 10, 14, 18, 22)})
        self.nav_error = ""

    def move_to(self, *args, **kwargs):
        if self.nav_error:
            self.calls.append(("move_to", {"pos": list(kwargs["pos"])}))
            raise ApiError(self.nav_error, "injected scout navigation failure")
        return super().move_to(*args, **kwargs)


class ScoutAccountingTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.world = ScoutAccountingWorld()
        self.ex, self.state = self.world.session()
        self.obs = SoakObserver(self.tmp.name)

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def incidents(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(self.tmp.name, "incidents").glob("*.json"))]

    def scout(self, error_code="", *, postcondition_error=""):
        self.world.nav_error = error_code
        skill = make("scout_island")
        if postcondition_error:
            skill.verify_done = lambda *_: postcondition_error
        self.obs.on_skill_start(skill, "rule", "regression", self.state)
        if error_code:
            with self.assertRaises(Replan) as caught:
                drive(skill, self.ex, self.state)
            code, _ = failure(caught.exception)
            self.assertEqual(code, error_code)
            skill.abort()
            self.obs.on_skill_end(skill, "replan", self.state, error_code=code, error_msg=str(caught.exception))
        else:
            steps = drive(skill, self.ex, self.state)
            self.assertTrue(steps)
            self.obs.on_skill_end(skill, "completed", self.state)

    def budget_stop(self, *, leak_posture=False):
        self.world.nav_error = "path_not_found"
        skill = make("scout_island")
        self.obs.on_skill_start(skill, "focus", "budget regression", self.state)
        # Stop between yielded actions, just as MainLoop does when the run budget expires.
        flow = self._one_step(skill)
        drive(flow, self.ex)
        skill.abort()
        if leak_posture:
            self.state.st["sneaking"] = True
        skill.verify_done = lambda *_: self.fail("An aborted skill must not be verified as completed")
        self.obs.on_skill_end(skill, "budget_stopped", self.state)

    def _one_step(self, skill):
        self.assertFalse(skill.step(self.ex, self.state))
        self.assertEqual(skill.last["error_code"], "path_not_found")
        yield skill.last

    def test_ep33_recovered_then_budget_stopped_does_not_raise_oscillation(self):
        # Recorded order: hurt -> no path -> completed -> budget_stopped.
        self.scout("interrupted")
        self.scout("path_not_found")
        self.scout()
        self.budget_stop()
        self.assertEqual(self.incidents(), [])
        stats = self.obs.skill_stats["scout_island"]
        self.assertEqual((stats["started"], stats["ok"], stats["replan"], stats["budget_stopped"]), (4, 1, 2, 1))
        self.assertNotIn("scout_island:budget_stopped", self.obs.env_variance_counts)
        timeline = [json.loads(line) for line in Path(self.tmp.name, "timeline.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["outcome"] for r in timeline if r["event"] == "skill_end"],
                         ["replan", "replan", "completed", "budget_stopped"])

    def test_budget_stop_neither_adds_nor_clears_real_failure_history(self):
        self.scout("path_not_found")
        self.scout("path_not_found")
        self.budget_stop()
        self.assertEqual(self.incidents(), [])
        self.assertEqual(self.obs.skill_stats["scout_island"]["replan"], 2)
        # Three real failures still trigger the unchanged threshold, even across a cancellation.
        self.scout("path_not_found")
        incidents = self.incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual((incidents[0]["code"], incidents[0]["detail"]["recent_failures"]),
                         ("REPLAN_OSCILLATION", 3))
        self.assertEqual(incidents[0]["skill_context"]["outcome"], "replan")
        self.assertEqual(self.obs.env_variance_counts["scout_island:path_not_found"], 3)

    def test_success_resets_same_goal_but_does_not_hide_later_three_failures(self):
        self.scout("path_not_found")
        self.scout("path_not_found")
        self.scout()
        self.scout("path_not_found")
        self.scout("path_not_found")
        self.assertEqual(self.incidents(), [])
        self.scout("path_not_found")
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(self.incidents()[0]["detail"]["recent_failures"], 3)
        self.assertEqual(self.obs.skill_stats["scout_island"]["replan"], 5)

    def test_success_for_other_goal_does_not_clear_scout_failures(self):
        self.scout("path_not_found")
        self.scout("path_not_found")
        self.world.blocks[(2, 64, 0)] = "oak_log"
        other = make("gather_wood", {"count": 1})
        self.obs.on_skill_start(other, "rule", "other goal completed", self.state)
        drive(other, self.ex, self.state)
        self.obs.on_skill_end(other, "completed", self.state)
        self.scout("path_not_found")
        self.assertEqual(self.incidents()[0]["detail"]["recent_failures"], 3)

    def test_failed_postcondition_does_not_reset_real_failure_history(self):
        self.scout("path_not_found")
        self.scout("path_not_found")
        self.scout(postcondition_error="injected failed postcondition")
        self.scout("path_not_found")
        self.assertEqual({i["code"] for i in self.incidents()},
                         {"SKILL_POSTCONDITION_FAIL", "REPLAN_OSCILLATION"})
        self.assertEqual(self.obs.skill_stats["scout_island"]["ok"], 0)

    def test_budget_stop_still_checks_posture_leaks_and_shows_separate_stat(self):
        self.budget_stop(leak_posture=True)
        self.assertEqual([i["code"] for i in self.incidents()], ["POSTURE_LEAK"])
        stats = self.obs.skill_stats["scout_island"]
        self.assertEqual((stats["replan"], stats["budget_stopped"], stats["bug"]), (0, 1, 1))
        summary = self.obs.write_summary()
        self.assertIn("预算中止", summary)
        self.assertIn("| `scout_island` | 1 | 0 | 0 | 1 | 1 |", summary)

    def test_budget_stop_does_not_suppress_later_contract_or_fatal_errors(self):
        self.budget_stop()
        self.scout("bad_args")
        for outcome in ("fatal", "unexpected_exception"):
            skill = make("scout_island")
            self.obs.on_skill_start(skill, "rule", "fatal regression", self.state)
            self.obs.on_skill_end(skill, outcome, self.state, error_code=outcome, error_msg="injected fatal")
        incidents = self.incidents()
        self.assertEqual(len(incidents), 3)
        self.assertEqual({(i["code"], i["severity"]) for i in incidents}, {("CONTRACT_OR_FATAL_ERROR", "P0")})
        self.assertEqual({i["skill_context"]["outcome"] for i in incidents},
                         {"replan", "fatal", "unexpected_exception"})

    def test_loop_step_budget_after_failed_action_is_not_a_third_skill_failure(self):
        self.scout("path_not_found")
        self.scout("path_not_found")
        planner = CampaignPlanner(mode="focus", focus_skill="scout_island")
        loop = MainLoop(self.world, self.state, self.ex, planner, observer=self.obs,
                        config=LoopConfig(mode="focus", max_steps=1, enable_monitor=False, idle_sleep_s=0),
                        log=lambda *_: None)
        result = loop.run()
        self.assertEqual((result.steps, result.skills_replan, result.stop_reason), (1, 0, "max_steps_reached"))
        self.assertEqual(self.incidents(), [])
        stats = self.obs.skill_stats["scout_island"]
        self.assertEqual((stats["replan"], stats["budget_stopped"]), (2, 1))


if __name__ == "__main__":
    unittest.main()
