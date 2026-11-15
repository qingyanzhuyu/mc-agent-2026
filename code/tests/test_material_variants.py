"""离线验证 1.21.11 材料选择与台账前置。"""

import unittest

from mcagent.loop import reset_world_memo
from mcagent.loop.goals import (
    GOAL_MAP,
    GOALS,
    check_item_expr,
    is_goal_achieved,
    is_prereq_satisfied,
    rank_actionable_goals,
    resolve_active_goals,
)
from mcagent.loop.starter_skills import can_afford_gear
from mcagent.skills import make, nav
from mcagent.skills.testing import GridWorld, drive
from tests.test_loop_soak import RichGridWorld


class MaterialVariantTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        self.addCleanup(reset_world_memo)

    def test_pale_oak_only_starts_a_wooden_pickaxe(self):
        world = RichGridWorld(
            blocks={(x, 64, 0): "pale_oak_log" for x in (1, 2, 3)},
            items={"stone_axe": 1, "copper_axe": 1},
        )
        ex, state = world.session()
        steps = drive(make("gather_wood", {"count": 3}), ex, state)
        self.assertEqual((steps[0]["op"], steps[0]["item"]), ("equip", "copper_axe"))
        self.assertEqual(state.count("pale_oak_log"), 3)
        self.assertTrue(can_afford_gear(state, "wooden_pickaxe"))

        steps = drive(make("craft_gear", {"item": "wooden_pickaxe"}), ex, state)
        self.assertIn(("craft", "pale_oak_planks"), [(s["op"], s["item"]) for s in steps])
        self.assertEqual(state.count("wooden_pickaxe"), 1)

    def test_tuff_and_pale_oak_planks_are_placeable_without_counting_as_cobble(self):
        for item in ("tuff", "pale_oak_planks"):
            with self.subTest(item=item):
                world = GridWorld(items={item: 8})
                ex, state = world.session()
                self.assertFalse(check_item_expr(state, "@cobble>=1"))
                self.assertFalse(is_goal_achieved(GOAL_MAP["mine_stone"], state))
                self.assertFalse(can_afford_gear(state, "furnace"))
                drive(nav.sneak_bridge(ex, state, [[1, 63, 0]]), ex)
                self.assertEqual(world.blocks[(1, 63, 0)], item)
                self.assertFalse(world.st["sneaking"])

    def test_copper_pickaxe_unlocks_iron_mining_without_giving_pickaxe_score(self):
        world = GridWorld(items={"copper_pickaxe": 1, "furnace": 1})
        _, state = world.session()
        self.assertTrue(check_item_expr(state, "@stone_plus_pickaxe>=1"))
        self.assertFalse(check_item_expr(state, "@iron_plus_pickaxe>=1"))
        self.assertTrue(is_prereq_satisfied(GOAL_MAP["upgrade_tools"], state))
        self.assertFalse(is_goal_achieved(GOAL_MAP["upgrade_tools"], state))
        self.assertFalse(is_goal_achieved(GOAL_MAP["iron_tools"], state))

        active = resolve_active_goals(GOALS, target_goal="smelt_iron")
        ranked = rank_actionable_goals(state, active, target_goal="smelt_iron")
        self.assertEqual(ranked[0].step_goal.id, "smelt_iron")

    def test_breach_prefers_copper_over_stone_but_stone_still_counts(self):
        world = GridWorld(blocks={(1, 64, 0): "stone"},
                          items={"stone_pickaxe": 1, "copper_pickaxe": 1})
        ex, state = world.session()
        steps = drive(nav.breach_1x2(ex, state, [[1, 64, 0]]), ex)
        self.assertEqual(steps[0]["item"], "copper_pickaxe")
        self.assertTrue(is_goal_achieved(GOAL_MAP["upgrade_tools"], state))
