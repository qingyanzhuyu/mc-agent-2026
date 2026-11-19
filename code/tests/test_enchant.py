"""P6 附魔契约。每次界面变化由该用例显式安排。"""
from __future__ import annotations

import unittest

from mcagent.client import ApiError
from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills.enchant import EnchantItem, _option
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import drive


_STATION = (2, 64, 0)
_TABLE = (0, 64, 2)
_ITEM = "iron_pickaxe"
_ADV = "minecraft:story/enchant_item"
_ENCHANTMENTS = [{"name": "efficiency", "level": 1}]


def _entry(name, count=1, **extra):
    return {"name": name, "count": count, **extra}


def _options():
    return [
        {"index": 0, "level_required": 13, "lapis_cost": 1, "hint": {"enchantment": "efficiency", "level": 2}, "available": False},
        {"index": 1, "level_required": 5, "lapis_cost": 2, "hint": {"enchantment": "efficiency", "level": 1}, "available": True},
        {"index": 2, "level_required": 8, "lapis_cost": 3, "hint": {"enchantment": "unbreaking", "level": 2}, "available": False},
    ]


def _view(item=None, lapis=0, options=(), *, window="enchantment"):
    return {
        "window": window,
        "target": "enchanting_table",
        "sections": {
            "item": [] if item is None else [item],
            "lapis": [_entry("lapis_lazuli", lapis)] if lapis else [],
        },
        "options": list(options),
    }


def _find(*matches):
    return {"blocks": [{"name": name, "pos": list(pos)} for name, pos in matches]}


def _progress(done=False):
    return {"ready": True, "advancements": [{"id": _ADV, "status": "completed"}] if done else []}


class EnchantWorld(ScriptedWorld):
    def __init__(self, *, items=None, at_table=True, xp=6, empty_slots=None):
        ground = {(x, 63, z): "stone" for x in range(-2, 4) for z in range(-2, 4)}
        if at_table:
            ground[_STATION] = "enchanting_table"
        super().__init__(blocks=ground, items={_ITEM: 1, "lapis_lazuli": 2} if items is None else items)
        self.st.update(dimension="overworld", xp_level=xp)
        self.enchanted = set()
        self.empty_slots = empty_slots

    def inventory(self, timeout=10):
        inv = super().inventory(timeout)
        if self.empty_slots is not None:
            inv["empty_slots"] = self.empty_slots
        for row in inv["items"]:
            if row["name"] in self.enchanted:
                row["enchantments"] = _ENCHANTMENTS
        return inv


def _arrange_exchange(world, *, item=_ITEM, enchanted=True, first_slot_available=True):
    result = "enchanted_book" if item == "book" else item

    def act_success(lapis_cost, option_idx):
        def respond(world, *, ops, **request):
            self._check = ops
            assert ops == [
                {"op": "put", "item": item, "count": 1, "to": "item"},
                {"op": "put", "item": "lapis_lazuli", "count": lapis_cost, "to": "lapis"},
                {"op": "select", "option": option_idx},
                {"op": "take", "from": "item"},
            ]
            if enchanted:
                world.items[item] -= 1
                world.items["lapis_lazuli"] -= lapis_cost
                world.items[result] = world.items.get(result, 0) + 1
                world.enchanted.add(result)
            return {
                "done": [
                    {"op": "put", "put": item, "count": 1, "to": "item"},
                    {"op": "put", "put": "lapis_lazuli", "count": lapis_cost, "to": "lapis"},
                    {"op": "select", "option": option_idx},
                    {"op": "take", "took": {result: 1}, "from": "item"},
                ],
                "window": _view(),
            }
        return respond

    self = type("_Holder", (), {})()
    world.queue("window_view", _view())
    if not first_slot_available:
        err_view = _view(_entry(item), lapis=1, options=_options())
        world.queue("window_act", ApiError(
            "option_unavailable",
            "option 0 requires level 13",
            {
                "failed_op": 2,
                "done": [
                    {"op": "put", "put": item, "count": 1, "to": "item"},
                    {"op": "put", "put": "lapis_lazuli", "count": 1, "to": "lapis"},
                ],
                "window": err_view,
            },
        ))
        world.queue("window_view", _view())
        world.queue("window_act", act_success(2, 1))
    else:
        world.queue("window_act", act_success(1, 0))
    if enchanted:
        world.queue("advancements", _progress(True))


class EnchantTests(unittest.TestCase):
    def test_single_batch_enchantment_succeeds_on_first_act(self):
        world = EnchantWorld()
        world.queue("advancements", _progress())
        world.queue("find_blocks", _find(("enchanting_table", _STATION)))
        _arrange_exchange(world, first_slot_available=True)
        ex, state = world.session()
        steps = drive(EnchantItem(), ex, state)
        self.assertEqual(ex.acts, ["window/view", "window/act"])
        self.assertTrue(steps[-1]["advancement_confirmed"])
        self.assertEqual(steps[-1]["option"], 0)
        self.assertEqual(steps[-1]["quoted_lapis"], 1)
        self.assertEqual(state.count("lapis_lazuli"), 1)
        world.assert_exhausted()

    def test_reuse_selects_affordable_option_and_confirms_bag(self):
        world = EnchantWorld()
        world.queue("advancements", _progress())
        world.queue("find_blocks", _find(("enchanting_table", _STATION)))
        _arrange_exchange(world, first_slot_available=False)
        ex, state = world.session()
        steps = drive(EnchantItem(), ex, state)
        batches = [call["ops"] for method, call in world.calls if method == "window_act"]
        self.assertEqual(batches[-1], [
            {"op": "put", "item": _ITEM, "count": 1, "to": "item"},
            {"op": "put", "item": "lapis_lazuli", "count": 2, "to": "lapis"},
            {"op": "select", "option": 1},
            {"op": "take", "from": "item"},
        ])
        self.assertTrue(steps[-1]["advancement_confirmed"])
        self.assertEqual(state.count(_ITEM), 1)
        world.assert_exhausted()

    def test_crafts_and_places_table_and_can_return_enchanted_book(self):
        world = EnchantWorld(items={"book": 2, "diamond": 2, "obsidian": 4, "lapis_lazuli": 2}, at_table=False)
        world.blocks[_TABLE] = "crafting_table"
        world.queue("advancements", _progress())
        world.queue("find_blocks", _find(), _find(("crafting_table", _TABLE)))

        def craft(world, item, count, **kwargs):
            self.assertEqual((item, count), ("enchanting_table", 1))
            for reagent, units in (("book", 1), ("diamond", 2), ("obsidian", 4)):
                world.items[reagent] -= units
            world.items[item] = 1
            return {"crafted": item, "gained": 1}

        world.queue("craft", craft)
        _arrange_exchange(world, item="book")
        ex, state = world.session()
        drive(EnchantItem(), ex, state)
        self.assertEqual(ex.acts[:2], ["craft", "place"])
        self.assertEqual(state.count("enchanted_book"), 1)
        world.assert_exhausted()

    def test_missing_lapis_or_experience_replans_before_touching_window(self):
        for items, xp, message in [({_ITEM: 1}, 6, "青金石"), ({_ITEM: 1, "lapis_lazuli": 2}, 0, "经验")]:
            with self.subTest(message=message):
                world = EnchantWorld(items=items, xp=xp)
                world.queue("advancements", _progress())
                ex, state = world.session()
                with self.assertRaisesRegex(Replan, message):
                    drive(EnchantItem(), ex, state)
                self.assertEqual(ex.acts, [])

    def test_no_station_or_materials_exposes_search_fields(self):
        world = EnchantWorld(at_table=False)
        world.queue("advancements", _progress())
        world.queue("find_blocks", _find())
        ex, state = world.session()
        with self.assertRaises(EnvBlocked) as caught:
            drive(EnchantItem(), ex, state)
        self.assertEqual(caught.exception.missing_predicate, "station_ready:enchanting_table")
        self.assertIn("半径 8 格", caught.exception.searched_volume)
        self.assertEqual(caught.exception.wake_on, "chunk_moved")
        self.assertEqual(ex.acts, [])

    def test_successful_select_receipt_without_enchantment_is_not_success(self):
        world = EnchantWorld()
        world.queue("advancements", _progress())
        world.queue("find_blocks", _find(("enchanting_table", _STATION)))
        _arrange_exchange(world, enchanted=False)
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "select/take"):
            drive(EnchantItem(), ex, state)
        self.assertEqual(ex.acts, ["window/view", "window/act"])
        world.assert_exhausted()

    def test_inventory_full_on_enchant_take_or_stacked_book_replans(self):
        world = EnchantWorld()
        world.queue("advancements", _progress())
        world.queue("find_blocks", _find(("enchanting_table", _STATION)))
        world.queue("window_view", _view(), _view())
        world.queue("window_act", ApiError(
            "inventory_full", "inventory is full",
            {"failed_op": 3, "done": [
                {"op": "put", "put": _ITEM, "count": 1, "to": "item"},
                {"op": "put", "put": "lapis_lazuli", "count": 1, "to": "lapis"},
                {"op": "select", "option": 0},
            ], "window": _view(_entry(_ITEM, enchantments=_ENCHANTMENTS))},
        ))
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "inventory_full"):
            drive(EnchantItem(), ex, state)
        world.assert_exhausted()

        world2 = EnchantWorld(items={"book": 2, "lapis_lazuli": 2}, empty_slots=0)
        world2.queue("advancements", _progress())
        world2.queue("find_blocks", _find(("enchanting_table", _STATION)))
        world2.queue("window_view", _view())
        ex2, state2 = world2.session()
        with self.assertRaisesRegex(Replan, "inventory_full"):
            drive(EnchantItem(), ex2, state2)
        self.assertEqual(ex2.acts, ["window/view"])
        world2.assert_exhausted()

    def test_unsupported_window_shape_is_rejected_before_any_put(self):
        world = EnchantWorld()
        world.queue("advancements", _progress())
        world.queue("find_blocks", _find(("enchanting_table", _STATION)))
        world.queue("window_view", {"window": "enchantment", "sections": {"container": []}})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "item/lapis"):
            drive(EnchantItem(), ex, state)
        self.assertEqual(ex.acts, ["window/view"])

    def test_abort_after_view_never_sends_window_mutation(self):
        world = EnchantWorld()
        world.queue("advancements", _progress())
        world.queue("find_blocks", _find(("enchanting_table", _STATION)))
        world.queue("window_view", _view())
        ex, state = world.session()
        skill = EnchantItem()
        self.assertFalse(skill.step(ex, state))
        skill.abort()
        self.assertEqual(ex.acts, ["window/view"])
        world.assert_exhausted()

    def test_existing_enchanted_item_is_not_reenchanted(self):
        world = EnchantWorld(items={_ITEM: 1})
        world.enchanted.add(_ITEM)
        world.queue("advancements", _progress())
        ex, state = world.session()
        step, = drive(EnchantItem(), ex, state)
        self.assertTrue(step["held_product"])
        self.assertFalse(step["advancement_confirmed"])
        self.assertEqual(ex.acts, [])
        self.assertEqual(EnchantItem.material_plan({}, state), ())

    def test_table_plan_reserves_two_diamonds_and_lapis_without_duplicate_book(self):
        world = EnchantWorld(items={"book": 2, "diamond": 2, "obsidian": 4, "lapis_lazuli": 2})
        _, state = world.session()
        plan = EnchantItem.material_plan({}, state)
        self.assertEqual(sum(qty for item, qty, _ in plan if item == "book"), 2)
        self.assertIn(("diamond", 2, "consume"), plan)
        self.assertIn(("lapis_lazuli", 2, "consume"), plan)

    def test_quote_parser_requires_explicit_affordable_fields(self):
        self.assertEqual(_option(_view(options=_options()), 6, 2), (2, 5, 1))
        self.assertIsNone(_option(_view(options=[{"index": 1, "level_required": 5, "lapis_cost": 2, "available": False}]), 6, 3))
        self.assertEqual(_option({"window": "enchanting_table", "info": {"options": [
            {"index": 1, "level_cost": 5, "lapis_cost": 2},
        ]}}, 6, 2), (2, 5, 1))
        self.assertIsNone(_option(_view(options=[{"id": 1, "level": 1, "price": 1}]), 6, 3))
