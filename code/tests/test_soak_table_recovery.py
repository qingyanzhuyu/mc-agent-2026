"""local-target INC-001: unreachable tables must not keep crafting in a loop."""
import unittest

from mcagent.client import ApiError
from mcagent.loop import RulePlanner, reset_world_memo
from mcagent.loop.starter_skills import KNOWN_TABLES, has_known_table
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import drive
from tests.test_loop_soak import RichGridWorld


class TableFailureWorld(RichGridWorld):
    def __init__(self, *, items, blocks=None, error="path_not_found"):
        super().__init__(items=items, blocks=blocks or {(6, 66, 0): "crafting_table"})
        self.table_error = error

    def move_to(self, *args, **kwargs):
        if kwargs.get("pos") == [6, 66, 0]:
            self.calls.append(("move_to", {"pos": kwargs["pos"]}))
            raise ApiError(self.table_error, "injected table navigation failure")
        return super().move_to(*args, **kwargs)


class TableRecoveryTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_unreachable_table_falls_back_to_new_table_one_action_per_step(self):
        world = TableFailureWorld(items={"oak_planks": 4, "stick": 2, "cobblestone": 3})
        ex, state = world.session()
        KNOWN_TABLES.append((6, 66, 0))
        steps = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "craft", "place", "craft"])
        self.assertEqual(steps[0]["error_code"], "path_not_found")
        self.assertEqual(state.count("stone_pickaxe"), 1)
        self.assertNotIn((6, 66, 0), KNOWN_TABLES)
        self.assertTrue(has_known_table(state.pos))

    def test_missing_replacement_materials_invalidates_anchor_and_planner_gathers(self):
        # Captured incident had enough sticks/cobble, but only three planks.
        world = TableFailureWorld(items={"oak_planks": 3, "stick": 2, "cobblestone": 7,
                                         "wooden_pickaxe": 1})
        ex, state = world.session()
        KNOWN_TABLES.append((6, 66, 0))
        with self.assertRaisesRegex(Replan, "木板不足"):
            drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
        self.assertEqual(ex.acts, ["move_to"])
        self.assertFalse(has_known_table(state.pos))
        self.assertEqual(RulePlanner().choose(state).skill.name, "gather_wood")

    def test_tries_next_table_without_spending_replacement_materials(self):
        world = TableFailureWorld(items={"stick": 2, "cobblestone": 3},
                                  blocks={(6, 66, 0): "crafting_table", (9, 64, 0): "crafting_table"})
        ex, state = world.session()
        steps = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "move_to", "craft"])
        self.assertEqual(state.count("stone_pickaxe"), 1)
        self.assertEqual(KNOWN_TABLES, [(9.0, 64.0, 0.0)])

    def test_other_failures_propagate_without_crafting_fallback(self):
        world = TableFailureWorld(items={"oak_planks": 4, "stick": 2, "cobblestone": 3},
                                  error="protected_region")
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "protected_region"):
            drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
        self.assertEqual(ex.acts, ["move_to"])

    def test_failed_move_refreshes_position_before_trying_next_table(self):
        class PartwayWorld(TableFailureWorld):
            def move_to(self, *args, **kwargs):
                if kwargs.get("pos") == [6, 66, 0]:
                    self.st["pos"] = [8.5, 64.0, 0.5]
                return super().move_to(*args, **kwargs)

        world = PartwayWorld(items={"stick": 2, "cobblestone": 3},
                             blocks={(6, 66, 0): "crafting_table", (9, 64, 0): "crafting_table"})
        ex, state = world.session()
        steps = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "craft"])
        self.assertEqual(state.pos, [8.5, 64.0, 0.5])
        self.assertEqual(KNOWN_TABLES, [(9.0, 64.0, 0.0)])

    def test_successful_move_outside_existing_reuse_distance_is_not_cached(self):
        class ShortMoveWorld(TableFailureWorld):
            def move_to(self, *args, **kwargs):
                self.calls.append(("move_to", {"pos": kwargs["pos"]}))
                # 故障注入：返回成功但并未实际走近目标，不模拟服务端物理。
                return {"pos": list(self.st["pos"])}

        world = ShortMoveWorld(items={"oak_planks": 4, "stick": 2, "cobblestone": 3})
        ex, state = world.session()
        KNOWN_TABLES.append((6, 66, 0))
        steps = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "craft", "place", "craft"])
        self.assertNotIn((6, 66, 0), KNOWN_TABLES)
        self.assertEqual(state.count("stone_pickaxe"), 1)

    def test_nearby_table_is_reused_without_replacement_materials(self):
        world = TableFailureWorld(items={"stick": 2, "cobblestone": 3},
                                  blocks={(3, 64, 0): "crafting_table"})
        ex, state = world.session()
        steps = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["craft"])
        self.assertEqual(KNOWN_TABLES, [(3.0, 64.0, 0.0)])

    def test_abort_after_failed_move_never_starts_replacement_actions(self):
        world = TableFailureWorld(items={"oak_planks": 4, "stick": 2, "cobblestone": 3})
        ex, state = world.session()
        KNOWN_TABLES.append((6, 66, 0))
        skill = make("craft_gear", {"item": "stone_pickaxe"})
        self.assertFalse(skill.step(ex, state))
        self.assertEqual(skill.last["error_code"], "path_not_found")
        skill.abort()
        self.assertEqual(ex.acts, ["move_to"])
        self.assertFalse(has_known_table(state.pos))


if __name__ == "__main__":
    unittest.main()
