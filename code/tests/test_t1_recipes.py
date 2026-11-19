"""T1 合成脚本的显式动作回执与库存证据夹具。"""
from __future__ import annotations

import unittest
from collections import deque

from mcagent.client import ApiError
from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills.recipes import craft_recipe
from mcagent.skills.testing import GridWorld, drive


class CraftWorld(GridWorld):
    def __init__(self, *, items, tables=(), crafts=(), moves=()):
        super().__init__(items=items, blocks={tuple(pos): "crafting_table" for pos in tables})
        self.crafts = deque(crafts)
        self.moves = deque(moves)

    def find_blocks(self, types, radius, limit):
        assert types == ["crafting_table"]
        self.calls.append(("find_blocks", {"radius": radius, "limit": limit}))
        return {"blocks": [{"name": name, "pos": list(pos)} for pos, name in self.blocks.items()]}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, *, stop_on=None):
        self.calls.append(("move_to", {"pos": pos, "rng": rng}))
        observed = self.moves.popleft()
        if isinstance(observed, ApiError):
            raise observed
        self.st["pos"] = list(observed)
        return {"pos": list(pos)}

    def craft(self, item, count=1, *, stop_on=None):
        self.calls.append(("craft", {"item": item, "count": count}))
        result, changes = self.crafts.popleft()
        for name, delta in changes.items():
            self.items[name] = self.items.get(name, 0) + delta
        if isinstance(result, ApiError):
            raise result
        return result


class CraftRecipeTests(unittest.TestCase):
    def run_recipe(self, world, item, *, goal_qty=1):
        ex, state = world.session()
        outcome = {}

        def capture_result():
            outcome.update((yield from craft_recipe(ex, state, item, goal_qty=goal_qty)))

        return drive(capture_result(), ex), outcome

    def test_batches_use_one_request_each_and_record_actual_inventory_delta(self):
        arrows = {"arrow": 4, "flint": -1, "stick": -1, "feather": -1}
        world = CraftWorld(
            items={"arrow": 1, "flint": 2, "stick": 2, "feather": 2},
            tables=[(2, 64, 0)],
            crafts=[({"gained": 1}, arrows), ({"gained": 99}, arrows)],
        )

        steps, result = self.run_recipe(world, "minecraft:arrow", goal_qty=6)

        self.assertEqual([s["op"] for s in steps], ["craft", "craft"])
        self.assertEqual([args["count"] for name, args in world.calls if name == "craft"], [1, 1])
        self.assertEqual(result, {"item": "arrow", "before": 1, "after": 9, "gained": 8,
                                  "consumed": {"flint": 2, "stick": 2, "feather": 2}})

    def test_already_owned_product_skips_table_and_spending(self):
        world = CraftWorld(items={"iron_boots": 1})

        steps, result = self.run_recipe(world, "iron_boots")

        self.assertEqual(steps, [])
        self.assertEqual(world.calls, [])
        self.assertEqual((result["before"], result["after"], result["gained"], result["consumed"]), (1, 1, 0, {}))

    def test_keyword_goal_qty_counts_product_already_held(self):
        world = CraftWorld(items={"shears": 1, "iron_ingot": 2},
                           crafts=[({}, {"shears": 1, "iron_ingot": -2})])

        steps, result = self.run_recipe(world, "shears", goal_qty=2)

        self.assertEqual([step["op"] for step in steps], ["craft"])
        self.assertEqual((result["before"], result["after"], result["gained"]), (1, 2, 1))

    def test_goal_qty_is_keyword_only_and_count_is_not_accepted(self):
        ex, state = CraftWorld(items={}).session()
        for extra_args, kwargs in (((2,), {}), ((), {"count": 2})):
            with self.subTest(extra_args=extra_args, kwargs=kwargs):
                with self.assertRaises(TypeError):
                    craft_recipe(ex, state, "shears", *extra_args, **kwargs)

    def test_total_recipe_materials_are_checked_before_any_action(self):
        world = CraftWorld(items={"diamond": 7}, tables=[(1, 64, 0)])

        with self.assertRaisesRegex(Replan, "diamond ×4"):
            self.run_recipe(world, "diamond_boots", goal_qty=2)

        self.assertEqual(world.calls, [])

    def test_table_requirement_reports_bounded_search_and_wake_condition(self):
        world = CraftWorld(items={"iron_ingot": 3, "stick": 2, "crafting_table": 1})

        with self.assertRaises(EnvBlocked) as caught:
            self.run_recipe(world, "iron_pickaxe")

        self.assertEqual(caught.exception.missing_predicate, "station_ready:crafting_table")
        self.assertIn("半径 16 格", caught.exception.searched_volume)
        self.assertEqual(caught.exception.wake_on, "chunk_moved")
        self.assertEqual([name for name, _ in world.calls], ["find_blocks"])

    def test_no_craft_until_real_position_enters_reach_after_failed_and_false_moves(self):
        world = CraftWorld(
            items={"iron_ingot": 3, "stick": 2},
            tables=[(4, 64, 0), (6, 64, 0), (8, 64, 0)],
            moves=[ApiError("path_not_found", "夹具拒绝"), (0.5, 64, 0.5), (7, 64, 0)],
            crafts=[({"gained": 1}, {"iron_pickaxe": 1, "iron_ingot": -3, "stick": -2})],
        )

        steps, result = self.run_recipe(world, "iron_pickaxe")

        self.assertEqual([s["op"] for s in steps], ["move_to", "move_to", "move_to", "craft"])
        self.assertEqual([s.get("pos") for s in steps[:3]], [[4, 64, 0], [6, 64, 0], [8, 64, 0]])
        self.assertEqual(steps[0]["error_code"], "path_not_found")
        self.assertEqual(result["after"], 1)

    def test_success_receipt_without_inventory_gain_fails(self):
        world = CraftWorld(items={"iron_ingot": 2}, crafts=[({"gained": 1}, {})])

        with self.assertRaisesRegex(Replan, "背包仅增加 0 件"):
            self.run_recipe(world, "shears")

        self.assertEqual([name for name, _ in world.calls], ["craft"])

    def test_vanilla_plank_group_accepts_a_mix_and_needs_no_table(self):
        world = CraftWorld(items={"pale_oak_planks": 1, "cherry_planks": 1}, crafts=[(
            {"gained": 4}, {"pale_oak_planks": -1, "cherry_planks": -1, "stick": 4},
        )])

        steps, result = self.run_recipe(world, "stick")

        self.assertEqual([s["op"] for s in steps], ["craft"])
        self.assertEqual(result["consumed"], {"pale_oak_planks": 1, "cherry_planks": 1})

    def test_bamboo_stick_alternative_has_its_own_batch_yield(self):
        world = CraftWorld(items={"bamboo": 2}, crafts=[({}, {"bamboo": -2, "stick": 1})])

        _, result = self.run_recipe(world, "stick")

        self.assertEqual((result["gained"], result["consumed"]), (1, {"bamboo": 2}))

    def test_bed_wool_is_color_specific(self):
        world = CraftWorld(items={"white_wool": 2, "red_wool": 1, "oak_planks": 3})

        with self.assertRaisesRegex(Replan, "white_wool ×3"):
            self.run_recipe(world, "white_bed")

        self.assertEqual(world.calls, [])

    def test_abandoned_generator_does_not_issue_next_craft(self):
        world = CraftWorld(items={"iron_ingot": 4}, crafts=[({}, {"shears": 1, "iron_ingot": -2})])
        ex, state = world.session()
        flow = craft_recipe(ex, state, "shears", goal_qty=2)

        self.assertEqual(next(flow)["op"], "craft")
        flow.close()

        self.assertEqual([name for name, _ in world.calls], ["craft"])
        self.assertEqual(world.items["shears"], 1)

    def test_inventory_full_and_no_window_and_stalled_table_move(self):
        world_full = CraftWorld(
            items={"iron_ingot": 2},
            crafts=[(ApiError("inventory_full", "bag full", {"gained": 0, "empty_slots": 0}), {})],
        )
        with self.assertRaisesRegex(Replan, "inventory_full"):
            self.run_recipe(world_full, "shears")

        world_partial_enough = CraftWorld(
            items={"arrow": 3, "flint": 1, "stick": 1, "feather": 1},
            tables=[(2, 64, 0)],
            crafts=[(
                ApiError("inventory_full", "partial fit", {"gained": 1, "empty_slots": 0}),
                {"arrow": 1, "flint": -1, "stick": -1, "feather": -1},
            )],
        )
        steps, result = self.run_recipe(world_partial_enough, "arrow", goal_qty=4)
        self.assertEqual(result["after"], 4)
        self.assertTrue(steps[0]["result"]["inventory_full"])

        world_no_win = CraftWorld(
            items={"iron_ingot": 3, "stick": 2},
            tables=[(4, 64, 0), (6, 64, 0)],
            moves=[ApiError("stalled", "blocked"), (5, 64, 0)],
            crafts=[(ApiError("no_window", "table did not open", {"table": [6, 64, 0], "eye_dist": 4.8}), {})],
        )
        with self.assertRaisesRegex(Replan, "no_window"):
            self.run_recipe(world_no_win, "iron_pickaxe")


if __name__ == "__main__":
    unittest.main()
