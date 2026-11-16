from __future__ import annotations

import unittest

from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills.base import Skill
from mcagent.skills.encounter import shoot_projectile, verify_shot_lane
from mcagent.skills.field import collect_drops, place_block, scan_blocks, scan_entities
from mcagent.skills.interaction import hold_posture, use_item_on
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import drive


class FieldTests(unittest.TestCase):
    def test_scans_report_unknown_for_empty_or_changed_observations(self):
        world = ScriptedWorld()
        world.st["dimension"] = "overworld"
        world.queue("find_blocks", {"blocks": [{"name": "iron_ore", "pos": [2, 64, 0]}]})
        world.queue("get_entities", {"entities": []})
        ex, state = world.session()
        blocks = scan_blocks(ex, state, "iron_ore", radius=8)
        entities = scan_entities(ex, state, "hostile", radius=8)
        self.assertEqual((blocks["status"], blocks["blocks"]), ("unknown", []))
        self.assertEqual((entities["status"], entities["entities"]), ("unknown", []))
        self.assertEqual(entities["dimension"], "overworld")
        self.assertEqual(ex.acts, [])

    def test_near_drop_still_moves_and_checks_inventory(self):
        world = ScriptedWorld(blocks={(0, 63, 0): "stone", (1, 63, 0): "stone"})
        world.queue("get_entities", {"entities": [
            {"id": 4, "type": "item", "name": "item", "item": "arrowx1", "pos": [1.45, 64, 0.5]},
        ]})

        def collect(world, **request):
            world.st["pos"] = request["pos"]
            world.items["arrow"] = 1
            return {"pos": request["pos"]}

        world.queue("move_to", collect)
        ex, state = world.session()
        self.assertEqual([step["op"] for step in drive(collect_drops(ex, state, "arrow"), ex)], ["move_to"])
        self.assertEqual(state.count("arrow"), 1)

    def test_unknown_drop_includes_search_and_wake_fields(self):
        world = ScriptedWorld()
        world.queue("get_entities", {"entities": []})
        ex, state = world.session()
        with self.assertRaises(EnvBlocked) as err:
            drive(collect_drops(ex, state, "arrow", radius=5), ex)
        self.assertEqual(err.exception.missing_predicate, "visible_drop:arrow")
        self.assertIn("5", err.exception.searched_volume)
        self.assertEqual(err.exception.wake_on, "chunk_moved")
        self.assertEqual(ex.acts, [])

    def test_place_uses_observed_result_and_skips_existing_block(self):
        already = ScriptedWorld(blocks={(1, 64, 0): "obsidian"})
        ex, state = already.session()
        self.assertEqual(drive(place_block(ex, state, "obsidian", [1, 64, 0]), ex), [])

        false_positive = ScriptedWorld(items={"obsidian": 1})
        false_positive.queue("place", {"placed": "obsidian"})
        ex, state = false_positive.session()
        with self.assertRaisesRegex(Replan, "放置后"):
            drive(place_block(ex, state, "obsidian", [1, 64, 0]), ex)


class InteractionTests(unittest.TestCase):
    def test_interact_success_receipt_is_not_the_postcondition(self):
        world = ScriptedWorld(blocks={(1, 63, 0): "farmland"}, items={"wheat_seeds": 1})
        world.queue("interact", {"changed": True})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "独立观察"):
            drive(use_item_on(ex, state, (1, 63, 0), "wheat_seeds",
                              verify=lambda result: result["after"]["block"]["name"] == "wheat"), ex)
        self.assertEqual(ex.acts, ["interact"])

    def test_abort_clears_only_the_requested_posture(self):
        class LocalSkill(Skill):
            def run(self, ex, state):
                yield from hold_posture(ex, state, shield=True)

        world = ScriptedWorld(items={"shield": 1})
        world.queue("posture", {"sneaking": True, "blocking": True},
                    {"sneaking": True, "blocking": False})
        ex, state = world.session()
        skill = LocalSkill()
        self.assertFalse(skill.step(ex, state))
        skill.abort()
        self.assertEqual(world.calls, [
            ("posture", {"sneak": None, "shield": True}),
            ("posture", {"sneak": None, "shield": False}),
        ])
        world.assert_exhausted()


class EncounterTests(unittest.TestCase):
    def test_crossbow_requires_available_ammo_before_actions(self):
        world = ScriptedWorld(items={"crossbow": 1})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "弹药"):
            drive(shoot_projectile(ex, state, pos=(5.5, 65.5, 0.5), weapon="crossbow",
                                   retreat_pos=(-1.5, 64, 0.5)), ex)
        self.assertEqual(ex.acts, [])

    def test_shot_lane_rejects_intervening_solid_block(self):
        blocks = {(x, 63, 0): "stone" for x in (-2, -1, 0)}
        blocks[(2, 65, 0)] = "stone"
        world = ScriptedWorld(blocks=blocks)
        ex, state = world.session()
        evidence = verify_shot_lane(ex, state, (5.5, 65.5, 0.5), retreat_pos=(-1.5, 64, 0.5))
        self.assertEqual(evidence["status"], "blocked")
        self.assertIn("stone", evidence["reason"])
