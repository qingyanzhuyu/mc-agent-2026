"""P6 附魔契约。每次界面变化由该用例显式安排。"""
from __future__ import annotations

import unittest

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
        {"index": 0, "level_cost": 13, "lapis_cost": 1},
        {"index": 1, "level_cost": 5, "lapis_cost": 2},
        {"index": 2, "level_cost": 8, "lapis_cost": 3},
    ]


def _view(item=None, lapis=0, options=()):
    return {"window": "enchanting_table", "sections": {"item": [] if item is None else [item],
            "lapis": [_entry("lapis_lazuli", lapis)] if lapis else []}, "info": {"options": list(options)}}


def _find(*matches):
    return {"blocks": [{"name": name, "pos": list(pos)} for name, pos in matches]}


def _progress(done=False):
    return {"ready": True, "advancements": [{"id": _ADV, "status": "completed"}] if done else []}


class EnchantWorld(ScriptedWorld):
    def __init__(self, *, items=None, at_table=True, xp=6):
        ground = {(x, 63, z): "stone" for x in range(-2, 4) for z in range(-2, 4)}
        if at_table:
            ground[_STATION] = "enchanting_table"
        super().__init__(blocks=ground, items={_ITEM: 1, "lapis_lazuli": 2} if items is None else items)
        self.st.update(dimension="overworld", xp_level=xp)
        self.enchanted = set()

    def inventory(self, timeout=10):
        inv = super().inventory(timeout)
        for row in inv["items"]:
            if row["name"] in self.enchanted:
                row["enchantments"] = _ENCHANTMENTS
        return inv


def _arrange_exchange(world, *, item=_ITEM, enchanted=True):
    offered = _view(_entry(item), options=_options())
    loaded = _view(_entry(item), lapis=2, options=_options())
    result = "enchanted_book" if item == "book" else item
    sold = _view(_entry(result, enchantments=_ENCHANTMENTS)) if enchanted else loaded

    def put(world, *, ops, **request):
        move, = ops
        world.items[move["item"]] -= move["count"]
        return {"accepted": True}

    def take(world, *, ops, **request):
        move, = ops
        world.items.setdefault(result, 0)
        world.items[result] += move["count"]
        world.enchanted.add(result)
        return {"accepted": True}

    world.queue("window_view", _view(), _view(), offered, offered, loaded, loaded, sold)
    world.queue("window_act", put, put, {"accepted": True})
    if enchanted:
        world.queue("window_view", _view())
        world.queue("window_act", take)
        world.queue("advancements", _progress(True))


class EnchantTests(unittest.TestCase):
    def test_reuse_selects_affordable_option_and_confirms_bag(self):
        world = EnchantWorld()
        world.queue("advancements", _progress())
        world.queue("find_blocks", _find(("enchanting_table", _STATION)))
        _arrange_exchange(world)
        ex, state = world.session()
        steps = drive(EnchantItem(), ex, state)
        actions = [call["ops"][0] for method, call in world.calls if method == "window_act"]
        self.assertEqual(actions, [
            {"op": "put", "item": _ITEM, "count": 1, "to": "item"},
            {"op": "put", "item": "lapis_lazuli", "count": 2, "to": "lapis"},
            {"op": "select", "option": 1, "times": 1},
            {"op": "take", "item": _ITEM, "count": 1, "from": "item"},
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
        with self.assertRaisesRegex(Replan, "select"):
            drive(EnchantItem(), ex, state)
        self.assertEqual(sum(name == "window/act" for name in ex.acts), 3)
        world.assert_exhausted()

    def test_unsupported_window_shape_is_rejected_before_any_put(self):
        world = EnchantWorld()
        world.queue("advancements", _progress())
        world.queue("find_blocks", _find(("enchanting_table", _STATION)))
        world.queue("window_view", {"window": "enchanting_table", "sections": {"container": []}})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "item/lapis"):
            drive(EnchantItem(), ex, state)
        self.assertEqual(ex.acts, ["window/view"])

    def test_abort_after_put_never_sends_a_second_window_mutation(self):
        world = EnchantWorld()
        world.queue("advancements", _progress())
        world.queue("find_blocks", _find(("enchanting_table", _STATION)))
        world.queue("window_view", _view(), _view())
        world.queue("window_act", {"accepted": True})
        ex, state = world.session()
        skill = EnchantItem()
        for _ in range(3):
            self.assertFalse(skill.step(ex, state))
        skill.abort()
        self.assertEqual(ex.acts, ["window/view", "window/view", "window/act"])
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
        self.assertIsNone(_option(_view(options=[{"id": 1, "level": 1, "price": 1}]), 6, 3))
