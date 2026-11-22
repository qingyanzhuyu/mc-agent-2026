"""Offline cross-skill regressions with explicit advancement receipts."""

from mcagent.loop.goals import GOAL_MAP, is_goal_achieved
from mcagent.skills import make
from mcagent.skills.combat_scene import DEFLECT, KILL_MOB, OL_BETSY, SHOOT_ARROW
from mcagent.skills.testing import drive

from test_combat import CombatTestCase, CombatWorld, mob, progress_records


class T1CombatChainTests(CombatTestCase):
    def assert_observed_goals(self, world, state, expected):
        snapshot = world.advancements(status="completed", include_recipes=False)
        self.assertTrue(snapshot["ready"])
        confirmed = {row["id"] for row in snapshot["advancements"] if row["status"] == "completed"}
        for goal_id, achieved in expected.items():
            with self.subTest(goal=goal_id):
                self.assertEqual(is_goal_achieved(GOAL_MAP[goal_id], state, confirmed), achieved)

    def test_one_crossbow_arrow_satisfies_both_ledger_goals(self):
        target = mob()
        world = CombatWorld({"crossbow": 1, "arrow": 2}, entities=[target])
        executor, state = world.session()
        crossbow_goal = GOAL_MAP["ol_betsy"]
        hit_goal = GOAL_MAP["shoot_arrow"]
        self.assert_observed_goals(world, state, {"ol_betsy": False, "shoot_arrow": False})

        world.shot(progress=(OL_BETSY, SHOOT_ARROW))
        records = drive(make(crossbow_goal.skill, crossbow_goal.args_dict), executor, state)
        world.assert_exhausted()
        self.assertEqual(progress_records(records), [{OL_BETSY: True, SHOOT_ARROW: True}])
        self.assert_observed_goals(world, state, {"ol_betsy": True, "shoot_arrow": True})

        self.assertEqual(drive(make(hit_goal.skill, hit_goal.args_dict), executor, state), [])
        self.assertEqual(executor.acts, ["shoot"])
        self.assertEqual(state.count("arrow"), 1)
        request = next(args for op, args in world.calls if op == "shoot")
        self.assertEqual((request["weapon"], request["entity_id"]), ("crossbow", target["id"]))

    def test_skeleton_is_deflected_then_killed_in_the_same_world(self):
        target = mob()
        world = CombatWorld({"shield": 1, "stone_sword": 1}, entities=[target], held="stone_sword")
        executor, state = world.session()
        deflect_goal = GOAL_MAP["deflect_arrow"]
        kill_goal = GOAL_MAP["kill_a_mob"]
        self.assert_observed_goals(world, state, {"deflect_arrow": False, "kill_a_mob": False})

        world.equip_expected()
        world.shield_pulse(progress=(DEFLECT,), using=True)
        records = drive(make(deflect_goal.skill, deflect_goal.args_dict), executor, state)
        world.assert_exhausted()
        self.assertEqual(progress_records(records), [{DEFLECT: True}])
        self.assertEqual(world.entities, [target])
        self.assertFalse(world.st["blocking"])
        self.assertIsNone(world.st["using_item"])
        self.assert_observed_goals(world, state, {"deflect_arrow": True, "kill_a_mob": False})

        world.attack_result(progress=(KILL_MOB,))
        records = drive(make(kill_goal.skill, kill_goal.args_dict), executor, state)
        world.assert_exhausted()
        self.assertEqual(progress_records(records), [{KILL_MOB: True}])
        self.assertEqual(world.entities, [])
        self.assertEqual(executor.acts, ["equip", "use_item", "attack"])
        actions = [(op, args) for op, args in world.calls if op in {"use_item", "posture", "attack"}]
        self.assertEqual([op for op, _ in actions], ["use_item", "posture", "attack"])
        self.assertEqual(actions[0][1]["target"], {"entity_id": target["id"]})
        self.assertIs(actions[1][1]["shield"], False)
        self.assertEqual(actions[2][1]["entity_id"], target["id"])
        self.assert_observed_goals(world, state, {"deflect_arrow": True, "kill_a_mob": True})
