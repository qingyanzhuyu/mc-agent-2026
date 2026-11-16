from __future__ import annotations

import unittest

from mcagent.client import ApiError
from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills.base import Skill, failure
from mcagent.skills.encounter import shoot_projectile, verify_shot_lane
from mcagent.skills.field import collect_drops, mine_block_batch, place_block, scan_blocks, scan_entities
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

    def test_mine_block_batch_supports_five_block_reach_diagonal_walk_and_dig_unconfirmed(self):
        first = (3, 62, 2)   # eye dist ~4.77 <= 5.0
        second = (2, 62, 1)  # diagonal below
        world = ScriptedWorld(
            blocks={(0, 63, 0): "stone", (1, 63, 0): "stone", first: "iron_ore", second: "iron_ore"},
            items={"iron_pickaxe": 1},
        )

        def inv_with_eff(timeout=10):
            inv = ScriptedWorld.inventory(world, timeout)
            for row in inv["items"]:
                if row["name"] == "iron_pickaxe":
                    row["durability"] = [200, 250]
                    row["enchantments"] = [{"name": "efficiency", "level": 3}]
                    inv["held"] = row
            return inv

        world.inventory = inv_with_eff

        def dig_walk_and_break(w, *, pos, **kwargs):
            self.assertEqual(pos, list(second))
            w.st["pos"] = [1.5, 64.0, 0.5]
            w.blocks.pop(second)
            w.items["raw_iron"] = 1
            return {"dug": "iron_ore", "dig_ms": 210, "tool": "iron_pickaxe", "gained": {"raw_iron": 1}}

        world.queue(
            "dig",
            ApiError("dig_unconfirmed", "server did not confirm break", {
                "pos": list(first), "eye_dist": 4.77, "on_ground": True, "ping_ms": 42,
            }),
            dig_walk_and_break,
        )
        ex, state = world.session()
        steps = drive(mine_block_batch(
            ex, state, [first, second], block_types="iron_ore", drop="raw_iron", tool="iron_pickaxe",
        ), ex)
        self.assertEqual([s.get("error_code") for s in steps], ["dig_unconfirmed", None])
        self.assertEqual(state.pos, [1.5, 64.0, 0.5])
        self.assertEqual(state.count("raw_iron"), 1)
        world.assert_exhausted()

        silk_world = ScriptedWorld(blocks={(1, 64, 0): "diamond_ore"}, items={"diamond_pickaxe": 1})

        def inv_with_silk(timeout=10):
            inv = ScriptedWorld.inventory(silk_world, timeout)
            for row in inv["items"]:
                if row["name"] == "diamond_pickaxe":
                    row["durability"] = [500, 1561]
                    row["enchantments"] = [{"name": "silk_touch", "level": 1}]
                    inv["held"] = row
            return inv

        silk_world.inventory = inv_with_silk

        def silk_dig(w, *, pos, **kwargs):
            w.blocks.pop((1, 64, 0))
            w.items["diamond_ore"] = 1
            return {"dug": "diamond_ore", "gained": {"diamond_ore": 1}}

        silk_world.queue("dig", silk_dig)
        ex2, state2 = silk_world.session()
        with self.assertRaisesRegex(Replan, "silk_touch"):
            drive(mine_block_batch(
                ex2, state2, [(1, 64, 0)], block_types="diamond_ore", drop="diamond", tool="diamond_pickaxe",
            ), ex2)

        unconfirmed_only = ScriptedWorld(blocks={(1, 64, 0): "iron_ore"}, items={"iron_pickaxe": 1})
        unconfirmed_only.inventory = lambda timeout=10: {
            "items": [{"name": "iron_pickaxe", "count": 1, "slot": 0, "durability": [100, 250]}],
            "held": {"name": "iron_pickaxe", "count": 1, "slot": 0, "durability": [100, 250]},
        }
        unconfirmed_only.queue("dig", ApiError("dig_unconfirmed", "unconfirmed", {"pos": [1, 64, 0], "eye_dist": 4.9}))
        ex3, state3 = unconfirmed_only.session()
        with self.assertRaisesRegex(Replan, "dig_unconfirmed") as caught:
            drive(mine_block_batch(
                ex3, state3, [(1, 64, 0)], block_types="iron_ore", drop="raw_iron", tool="iron_pickaxe",
            ), ex3)
        self.assertEqual(failure(caught.exception)[0], "dig_unconfirmed")


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
