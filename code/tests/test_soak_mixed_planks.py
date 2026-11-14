"""ep-0087 local-target rejection of mixed planks; no official recipe claim."""
import unittest

from mcagent.loop import RulePlanner, reset_world_memo
from mcagent.loop.starter_skills import KNOWN_TABLES, can_afford_gear
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import drive
from tests.test_loop_soak import RichGridWorld


class MixedPlankRecoveryTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_incident_inventory_converts_available_log_before_retrying_pickaxe(self):
        # Actual incident inventory, with an already reachable table.
        world = RichGridWorld(items={"birch_log": 1, "birch_planks": 2,
                                     "stick": 2, "oak_planks": 1},
                              blocks={(3, 64, 0): "crafting_table"})
        ex, state = world.session()
        steps = drive(make("craft_gear", {"item": "wooden_pickaxe"}), ex, state)
        self.assertEqual([s["item"] for s in steps], ["birch_planks", "wooden_pickaxe"])
        self.assertEqual(state.count("wooden_pickaxe"), 1)
        self.assertEqual(state.hp, 20)

    def test_fragmented_planks_without_logs_replan_before_any_craft_and_gather(self):
        world = RichGridWorld(items={"birch_planks": 2, "stick": 2, "oak_planks": 1},
                              blocks={(3, 64, 0): "crafting_table"})
        ex, state = world.session()
        KNOWN_TABLES.append((3, 64, 0))
        self.assertFalse(can_afford_gear(state, "wooden_pickaxe", assume_no_table=False))
        with self.assertRaisesRegex(Replan, "木板不足"):
            drive(make("craft_gear", {"item": "wooden_pickaxe"}), ex, state)
        self.assertEqual(ex.acts, [])
        self.assertEqual(RulePlanner().choose(state).skill.name, "gather_wood")

    def test_each_recipe_rechecks_species_after_previous_recipe_consumes_planks(self):
        world = RichGridWorld(items={"oak_planks": 3, "birch_planks": 3,
                                     "spruce_planks": 3, "birch_log": 1})
        ex, state = world.session()
        self.assertTrue(can_afford_gear(state, "wooden_pickaxe"))
        steps = drive(make("craft_gear", {"item": "wooden_pickaxe"}), ex, state)
        self.assertEqual([(s["op"], s["item"]) for s in steps], [
            ("craft", "stick"), ("craft", "birch_planks"),
            ("craft", "crafting_table"), ("place", "crafting_table"),
            ("craft", "wooden_pickaxe"),
        ])
        self.assertEqual(state.count("wooden_pickaxe"), 1)

    def test_separate_species_can_pay_separate_recipes_without_extra_logs(self):
        world = RichGridWorld(items={"birch_planks": 4, "oak_planks": 3, "stick": 2})
        ex, state = world.session()
        self.assertTrue(can_afford_gear(state, "wooden_pickaxe"))
        steps = drive(make("craft_gear", {"item": "wooden_pickaxe"}), ex, state)
        self.assertEqual([s["item"] for s in steps], [
            "crafting_table", "crafting_table", "wooden_pickaxe",
        ])
        self.assertEqual(state.count("wooden_pickaxe"), 1)

    def test_unknown_recipe_consumption_order_does_not_overpromise_materials(self):
        # 9 total planks suffice only if sticks consume oak first. The API does
        # not let this skill choose the consumed tree species, so gather first.
        world = RichGridWorld(items={"oak_planks": 5, "birch_planks": 4})
        _, state = world.session()
        self.assertFalse(can_afford_gear(state, "wooden_pickaxe"))

    def test_interrupted_conversion_propagates_before_target_craft(self):
        from mcagent.client import ApiError

        class InterruptedWorld(RichGridWorld):
            def craft(self, item, count=1, *, stop_on=None):
                self.calls.append(("craft", {"item": item}))
                raise ApiError("interrupted", "hurt", {"interrupted_by": "hurt"})

        world = InterruptedWorld(items={"birch_log": 1, "birch_planks": 2, "stick": 2},
                                 blocks={(3, 64, 0): "crafting_table"})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "hurt"):
            drive(make("craft_gear", {"item": "wooden_pickaxe"}), ex, state)
        self.assertEqual(ex.acts, ["craft"])

    def test_no_progress_conversion_stops_after_one_yield_without_target_craft(self):
        class NoProgressWorld(RichGridWorld):
            def craft(self, item, count=1, *, stop_on=None):
                if item == "birch_planks":
                    self.calls.append(("craft", {"item": item}))
                    return {"crafted": item, "gained": 0}
                return super().craft(item, count, stop_on=stop_on)

        world = NoProgressWorld(items={"birch_log": 1, "birch_planks": 2, "stick": 2},
                                blocks={(3, 64, 0): "crafting_table"})
        ex, state = world.session()
        skill = make("craft_gear", {"item": "wooden_pickaxe"})
        self.assertFalse(skill.step(ex, state))
        self.assertEqual(skill.last["item"], "birch_planks")
        with self.assertRaisesRegex(Replan, "未增加"):
            drive(skill, ex, state, max_steps=8)
        self.assertEqual(ex.acts, ["craft"])

    def test_abort_after_preparation_never_starts_the_next_recipe(self):
        world = RichGridWorld(items={"birch_log": 1, "birch_planks": 2, "stick": 2},
                              blocks={(3, 64, 0): "crafting_table"})
        ex, state = world.session()
        skill = make("craft_gear", {"item": "wooden_pickaxe"})
        self.assertFalse(skill.step(ex, state))
        self.assertEqual(skill.last["item"], "birch_planks")
        skill.abort()
        self.assertEqual(ex.acts, ["craft"])
        self.assertEqual(world.items.get("wooden_pickaxe", 0), 0)


if __name__ == "__main__":
    unittest.main()
