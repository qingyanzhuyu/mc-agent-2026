"""P6 磁石契约。指南针组件与箱面均由各用例提供。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills.lodestone import AttuneLodestone
from mcagent.skills.recipes import RECIPES
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import drive


_STONE = (2, 64, 0)
_CHEST = (0, 64, 2)
_PLACEMENT = (1, 64, 0)
_ADV = "minecraft:adventure/use_lodestone"


def _find(*matches):
    return {"blocks": [{"name": name, "pos": list(pos)} for name, pos in matches]}


def _progress(done=False):
    return {"ready": True, "advancements": [{"id": _ADV, "status": "completed"}] if done else []}


class LodestoneWorld(ScriptedWorld):
    def __init__(self, *, items=None, placed=True):
        ground = {(x, 63, z): "stone" for x in range(-2, 5) for z in range(-2, 5)}
        if placed:
            ground[_STONE] = "lodestone"
        super().__init__(blocks=ground, items={"compass": 1} if items is None else items)
        self.st["dimension"] = "overworld"
        self.binding = None
        self.selected = None

    def inventory(self, timeout=10):
        inv = super().inventory(timeout)
        for row in inv["items"]:
            if row["name"] == "compass" and self.binding is not None:
                row["components"] = {"minecraft:lodestone_tracker": {
                    "tracked": True, "target": {"pos": list(self.binding), "dimension": "minecraft:overworld"}}}
            if row["name"] == self.selected:
                inv["held"] = row
        return inv


def _equip(world, *, item, slot, **kwargs):
    world.selected = item
    return {"held": {"name": item}}


def _bind(world, *, pos, item, **kwargs):
    world.binding = tuple(pos)
    return {"block": "lodestone", "changed": True}


def _craft(world, *, item, count, **kwargs):
    recipe = RECIPES[item]
    for ingredient, need in recipe.ingredients:
        world.items[ingredient] -= need * count
    world.items[item] = world.items.get(item, 0) + recipe.output * count
    return {"crafted": item, "gained": recipe.output * count}


class LodestoneTests(unittest.TestCase):
    def test_existing_world_stone_checks_held_and_binds_then_confirms_progress(self):
        world = LodestoneWorld()
        world.queue("advancements", _progress(), _progress(True))
        world.queue("find_blocks", _find(("lodestone", _STONE)))
        world.queue("equip", _equip)
        world.queue("interact", _bind)
        ex, state = world.session()
        got = drive(AttuneLodestone(), ex, state)
        self.assertEqual(ex.acts, ["equip", "interact"])
        self.assertEqual(world.binding, _STONE)
        self.assertTrue(got[-1]["advancement_confirmed"])
        world.assert_exhausted()

    def test_existing_container_stone_is_taken_as_a_single_verified_item(self):
        world = LodestoneWorld(placed=False)
        world.blocks[_CHEST] = "chest"
        world.queue("advancements", _progress(), _progress(True))
        world.queue("find_blocks", _find(), _find(("chest", _CHEST)))
        world.queue("window_view", {"window": "chest", "sections": {"container": [{"name": "lodestone", "count": 1}]}},
                    {"window": "chest", "sections": {"container": []}})

        def take(world, *, ops, **kwargs):
            self.assertEqual(ops, [{"op": "take", "item": "lodestone", "count": 1, "from": "container"}])
            world.items["lodestone"] = 1
            return {"taken": "lodestone"}

        world.queue("window_act", take)
        world.queue("equip", _equip)
        world.queue("interact", _bind)
        ex, state = world.session()
        drive(AttuneLodestone(), ex, state)
        self.assertEqual(ex.acts, ["window/view", "window/act", "window/view", "place", "equip", "interact"])
        self.assertEqual(world.blocks[_PLACEMENT], "lodestone")
        world.assert_exhausted()

    def test_prefers_prepared_chiseled_bricks_and_crafts_both_outputs(self):
        world = LodestoneWorld(items={"iron_ingot": 5, "redstone": 1, "chiseled_stone_bricks": 8}, placed=False)
        world.blocks[_CHEST] = "crafting_table"
        table = _find(("crafting_table", _CHEST))
        world.queue("advancements", _progress(), _progress(True))
        world.queue("find_blocks", _find(), _find(), table, table)
        world.queue("craft", _craft, _craft)
        world.queue("equip", _equip)
        world.queue("interact", _bind)
        ex, state = world.session()
        plan = AttuneLodestone.material_plan({}, state)
        self.assertEqual(set(plan), {("iron_ingot", 5, "consume"), ("redstone", 1, "consume"),
                                    ("chiseled_stone_bricks", 8, "consume")})
        drive(AttuneLodestone(), ex, state)
        self.assertEqual(ex.acts, ["craft", "craft", "place", "equip", "interact"])
        self.assertEqual(world.binding, _PLACEMENT)
        world.assert_exhausted()

    def test_stone_budget_rounds_batches_after_existing_slabs(self):
        world = LodestoneWorld(items={"iron_ingot": 1, "compass": 1, "stone_brick_slab": 4,
                                      "chiseled_stone_bricks": 2, "stone": 8}, placed=False)
        _, state = world.session()
        self.assertEqual(set(AttuneLodestone.material_plan({}, state)), {
            ("compass", 1, "consume"), ("iron_ingot", 1, "consume"), ("stone", 8, "consume"),
            ("stone_brick_slab", 4, "consume"), ("chiseled_stone_bricks", 2, "consume"),
        })

    def test_missing_compass_and_inputs_replans(self):
        world = LodestoneWorld(items={})
        world.queue("advancements", _progress())
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "普通指南针"):
            drive(AttuneLodestone(), ex, state)
        self.assertEqual(ex.acts, [])

    def test_missing_station_sets_search_area_and_wake_condition(self):
        world = LodestoneWorld(placed=False)
        world.queue("advancements", _progress())
        world.queue("find_blocks", _find(), _find())
        ex, state = world.session()
        with self.assertRaises(EnvBlocked) as caught:
            drive(AttuneLodestone(), ex, state)
        self.assertEqual(caught.exception.missing_predicate, "station_ready:lodestone")
        self.assertIn("5 个容器候选", caught.exception.searched_volume)
        self.assertEqual(caught.exception.wake_on, "chunk_moved")
        self.assertEqual(ex.acts, [])

    def test_fake_interaction_success_cannot_replace_new_binding(self):
        for point in (None, (9, 64, 0)):
            with self.subTest(point=point):
                world = LodestoneWorld()
                world.queue("advancements", _progress())
                world.queue("find_blocks", _find(("lodestone", _STONE)))
                world.queue("equip", _equip)

                def wrong_binding(world, **kwargs):
                    world.binding = point
                    return {"block": "lodestone", "changed": True}

                world.queue("interact", wrong_binding)
                ex, state = world.session()
                with self.assertRaisesRegex(Replan, "尚未确认预期结果"):
                    drive(AttuneLodestone(), ex, state)
                self.assertEqual(ex.acts, ["equip", "interact"])
                world.assert_exhausted()

    def test_existing_bound_compass_avoids_repeat_click(self):
        world = LodestoneWorld()
        world.binding = _STONE
        world.queue("advancements", _progress())
        ex, state = world.session()
        step, = drive(AttuneLodestone(), ex, state)
        self.assertTrue(step["variant_confirmed"])
        self.assertFalse(step["advancement_confirmed"])
        self.assertEqual(ex.acts, [])
        self.assertEqual(AttuneLodestone.material_plan({}, state), ())

    @patch("mcagent.skills.encounter.time.sleep")
    def test_binding_without_progress_ends_in_bounded_replan(self, sleep):
        world = LodestoneWorld()
        world.queue("advancements", *[_progress()] * 4)
        world.queue("find_blocks", _find(("lodestone", _STONE)))
        world.queue("equip", _equip)
        world.queue("interact", _bind)
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "进度.*未确认"):
            drive(AttuneLodestone(), ex, state)
        sleep.assert_called()
        self.assertEqual(world.binding, _STONE)
        world.assert_exhausted()

    def test_abort_after_equip_never_sends_interaction(self):
        world = LodestoneWorld()
        world.queue("advancements", _progress())
        world.queue("find_blocks", _find(("lodestone", _STONE)))
        world.queue("equip", _equip)
        ex, state = world.session()
        skill = AttuneLodestone()
        self.assertFalse(skill.step(ex, state))
        skill.abort()
        self.assertEqual(ex.acts, ["equip"])
        world.assert_exhausted()
