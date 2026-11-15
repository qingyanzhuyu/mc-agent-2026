from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import unittest
from unittest.mock import Mock, patch

from mcagent.client import Config
from mcagent.loop import GOAL_MAP, CampaignPlanner, RulePlanner
from mcagent.loop import __main__ as loop_main
from mcagent.loop.starter_skills import GatherWood, ScoutIsland
from mcagent.runtime import State


class LiveGatherWood(GatherWood):
    verified = "live"


class LiveScoutIsland(ScoutIsland):
    verified = "live"


class VerificationGateTests(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.state.set_status({"ready": True, "alive": True, "health": 20, "food": 20})
        self.state.set_inventory({"items": []})
        self.goal = GOAL_MAP["prep_wood"]

    def test_rule_queue_skips_offline_goal_and_uses_live_fallback(self):
        registry = {"gather_wood": GatherWood, "scout_island": LiveScoutIsland}
        local = RulePlanner(registry=registry, ledger=(self.goal,))
        self.assertIsInstance(local.choose(self.state).skill, GatherWood)

        strict = RulePlanner(registry=registry, ledger=(self.goal,), min_verified="live")
        self.assertIsInstance(strict.choose(self.state).skill, LiveScoutIsland)

    def test_explicit_goal_respects_floor_even_when_candidate_is_forced(self):
        candidate = replace(self.goal, stage="candidate")
        registry = {"gather_wood": GatherWood, "scout_island": ScoutIsland}
        strict = CampaignPlanner(
            target_goal=candidate.id, ledger=(candidate,), registry=registry, min_verified="live",
        )
        self.assertIsNone(strict.select(self.state, []).skill)

    def test_explicit_skill_uses_same_verification_floor(self):
        for cls, floor, allowed in (
            (GatherWood, "offline", True),
            (GatherWood, "live", False),
            (LiveGatherWood, "live", True),
        ):
            with self.subTest(skill=cls.__name__, floor=floor):
                planner = CampaignPlanner(
                    mode="focus", focus_skill="gather_wood", focus_args={"count": 2},
                    registry={"gather_wood": cls}, min_verified=floor,
                )
                decision = planner.select(self.state, [])
                if allowed:
                    self.assertIsInstance(decision.skill, cls)
                    self.assertEqual(decision.skill.args, {"count": 2})
                else:
                    self.assertIsNone(decision.skill)
                    self.assertIn("verified=live", decision.reason)

    def test_model_and_its_rule_fallback_both_reject_offline_skill(self):
        model = Mock(stats={})
        offline_choice = {"skill": "gather_wood", "args": {"count": 3}}

        def choose_offline(system, user, validate, **kwargs):
            self.assertNotIn("- gather_wood(", user)
            self.assertIn("- scout_island(", user)
            ok, errors = validate(offline_choice)
            self.assertFalse(ok)
            return None, errors, 1

        model.ask_json.side_effect = choose_offline
        planner = CampaignPlanner(
            use_llm=True, llm=model, ledger=(self.goal,), min_verified="live",
            registry={"gather_wood": GatherWood, "scout_island": LiveScoutIsland},
        )
        decision = planner.select(self.state, [])
        self.assertEqual(decision.source, "llm_fallback_rule")
        self.assertIsInstance(decision.skill, LiveScoutIsland)

    def test_cli_focus_offline_rejected_before_connecting_to_either_official_host(self):
        for hostname in ("apiin.nmo.net.cn", "mc-api.windywindie.top"):
            with self.subTest(hostname=hostname):
                cfg = Config(base=f"https://{hostname}/api/v1", username="fixture", password="fixture")
                with patch.object(Config, "load", return_value=cfg), patch.object(loop_main, "Client") as client, \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                    code = loop_main.main(["--live", "--local-target", "--mode", "focus", "--skill", "gather_wood"])

                self.assertEqual(code, 2)
                self.assertIn("需要 verified=live", err.getvalue())
                client.assert_not_called()
