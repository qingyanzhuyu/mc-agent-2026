"""Offline tests for Stage 2 Wave 3 high-value alternative speedup routes."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.runtime import Replan
from mcagent.skills.diamond import MineDiamond, collect_bonus_ores
from mcagent.skills.enchant import (
    EnchantItem,
    acquire_books_without_sugarcane,
    disenchant_book_at_grindstone,
    mine_bookshelf_for_books,
)
from mcagent.skills.furnace_lease import lease_book_for, reset_all_furnace_leases
from mcagent.skills.iron import (
    SmeltIron,
    reset_iron_memo,
    stations_for_item,
)
from mcagent.skills.lodestone import AttuneLodestone, cut_chiseled_bricks_at_stonecutter
from mcagent.skills.recipes import RECIPES
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.survival_camp import SleepInBed, plan_wool_dyeing
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


def _station_view(window="furnace", *, input_count=0, output=0, fuel=None, fuel_count=1):
    return {
        "window": window,
        "sections": {
            "input": [{"name": "raw_iron", "count": input_count}] if input_count else [],
            "fuel": [{"name": fuel, "count": fuel_count}] if fuel else [],
            "output": [{"name": "iron_ingot", "count": output}] if output else [],
        },
        "info": {"burning": bool(input_count), "progress": 0.5, "fuel_left": 0.5},
    }


class _AltIronWorld(GridWorld):
    def __init__(self, *, items=None, blocks=None, views=None, pos=(0.5, 64.0, 0.5)):
        ground = {(x, 63, z): "stone" for x in range(-2, 10) for z in range(-2, 3)}
        ground.update(blocks or {})
        super().__init__(pos=pos, blocks=ground, items=items or {})
        self.views = list(views or [])
        self.last_view = _station_view()

    def find_blocks(self, types=None, radius=16, limit=20):
        self.calls.append(("find_blocks", {"types": types, "radius": radius}))
        if types is None:
            return {"radius": radius, "blocks": {}}
        return {
            "blocks": [
                {"pos": list(p), "name": n}
                for p, n in self.blocks.items()
                if n in types and sum((a - b) ** 2 for a, b in zip(p, self.st["pos"])) <= radius ** 2
            ][:limit]
        }

    def window_view(self, target, *, stop_on=None):
        self.calls.append(("window_view", {"target": target}))
        if self.views:
            self.last_view = self.views.pop(0)
        return copy.deepcopy(self.last_view)

    def window_act(self, target, ops, *, stop_on=None):
        self.calls.append(("window_act", {"target": target, "ops": ops}))
        for op in ops:
            if op["op"] == "put":
                assert self.items.get(op["item"], 0) >= op["count"], f"missing {op}"
                self.items[op["item"]] -= op["count"]
            elif op["op"] == "take":
                item = op.get("item")
                if item is None:
                    item = "iron_ingot" if op["from"] == "output" else "bucket"
                qty = op.get("count", 1)
                self.items[item] = self.items.get(item, 0) + qty
        return {}


class IronAltRoutesTests(unittest.TestCase):
    def setUp(self):
        reset_iron_memo()
        reset_all_furnace_leases()
        reset_all_voxel_caches()
        reset_all_world_memos()
        self.addCleanup(reset_iron_memo)
        self.addCleanup(reset_all_furnace_leases)
        self.addCleanup(reset_all_voxel_caches)
        self.addCleanup(reset_all_world_memos)
        sleep_patch = patch("mcagent.skills.iron.time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_station_mapping_and_blast_furnace_reuse_preferred_over_furnace(self):
        self.assertEqual(stations_for_item("raw_iron"), ("blast_furnace", "furnace"))
        self.assertEqual(stations_for_item("beef"), ("smoker", "furnace"))
        self.assertEqual(stations_for_item("cobblestone"), ("furnace",))

        world = _AltIronWorld(
            items={"raw_iron": 3, "coal": 1},
            blocks={(2, 64, 0): "furnace", (4, 64, 0): "blast_furnace"},
            views=[
                _station_view("blast_furnace"),
                _station_view("blast_furnace", input_count=3),
                _station_view("blast_furnace", output=3),
                _station_view("blast_furnace"),
            ],
        )
        ex, state = world.session()
        steps = drive(SmeltIron(count=3), ex, state)
        self.assertEqual(state.count("iron_ingot"), 3)
        moved_targets = [s["pos"] for s in steps if s["op"] == "move_to"]
        self.assertEqual(moved_targets, [[4, 64, 0]])
        self.assertFalse(any(op == "place" for op, _ in world.calls))
        self.assertIn((4, 64, 0), memo_for(state).stations("blast_furnace", dimension="overworld"))

    def test_lava_bucket_fuel_smelts_and_recovers_empty_bucket(self):
        world = _AltIronWorld(
            items={"raw_iron": 3, "lava_bucket": 1, "furnace": 1},
            views=[
                _station_view("furnace"),
                _station_view("furnace", output=3, fuel="bucket"),
                _station_view("furnace", fuel="bucket"),
            ],
        )
        ex, state = world.session()
        self.assertEqual(SmeltIron.blocked(state), "")
        self.assertIn(("lava_bucket", 1, "consume"), SmeltIron.material_plan({"count": 3}, state))
        drive(SmeltIron(count=3), ex, state)
        self.assertEqual(state.count("iron_ingot"), 3)
        self.assertEqual(state.count("lava_bucket"), 0)
        self.assertEqual(state.count("bucket"), 1)
        fuel_takes = [
            req["ops"][0]
            for op, req in world.calls
            if op == "window_act" and req["ops"][0].get("from") == "fuel"
        ]
        self.assertEqual(fuel_takes, [{"op": "take", "from": "fuel", "item": "bucket", "count": 1}])

    def test_leaf_litter_fallback_fuel(self):
        world = _AltIronWorld(
            items={"raw_iron": 1, "leaf_litter": 2, "furnace": 1},
            views=[
                _station_view("furnace"),
                _station_view("furnace", output=1),
                _station_view("furnace"),
            ],
        )
        ex, state = world.session()
        self.assertEqual(SmeltIron.blocked(state), "")
        self.assertIn(("leaf_litter", 2, "consume"), SmeltIron.material_plan({"count": 1}, state))
        drive(SmeltIron(count=1), ex, state)
        self.assertEqual(state.count("iron_ingot"), 1)
        self.assertEqual(state.count("leaf_litter"), 0)
        fuel_puts = [
            req["ops"][0]
            for op, req in world.calls
            if op == "window_act" and req["ops"][0].get("to") == "fuel"
        ]
        self.assertEqual(fuel_puts, [{"op": "put", "item": "leaf_litter", "count": 2, "to": "fuel"}])

    def test_async_lease_start_without_sleep_and_collect_with_empty_bucket(self):
        world = _AltIronWorld(
            items={"raw_iron": 4, "lava_bucket": 1},
            blocks={(2, 64, 0): "blast_furnace"},
            views=[
                _station_view("blast_furnace"),
                _station_view("blast_furnace", output=4, fuel="bucket"),
            ],
        )
        ex, state = world.session()
        skill_start = SmeltIron(count=4, async_lease=True)
        steps_start = drive(skill_start, ex, state)
        self.assertEqual(self.sleep.call_count, 0)
        self.assertEqual(steps_start[-1]["op"], "smelt_lease_started")
        self.assertEqual(steps_start[-1]["result"]["station_kind"], "blast_furnace")
        self.assertTrue(steps_start[-1]["result"]["returns_empty_bucket"])
        self.assertEqual(state.count("iron_ingot"), 0)
        self.assertEqual(skill_start.verify_done(state, {"iron_ingot": 0}), "")

        book = lease_book_for(state)
        leases = book.active_leases(dimension="overworld")
        self.assertEqual(len(leases), 1)
        # Simulate lease completion without sleeping
        leases[0].ready_at = 0.0

        skill_collect = SmeltIron(count=4, async_lease=True)
        drive(skill_collect, ex, state)
        self.assertEqual(self.sleep.call_count, 0)
        self.assertEqual(state.count("iron_ingot"), 4)
        self.assertEqual(state.count("bucket"), 1)
        self.assertEqual(book.active_leases(dimension="overworld"), [])


class _BonusDiamondWorld(ScriptedWorld):
    def __init__(self, *, pos=(0.5, -40.0, 0.5), items=None, blocks=None, exposed=(), picks=None, held=None):
        floor_y = int(pos[1]) - 1
        base = {(int(pos[0]), floor_y, int(pos[2])): "deepslate"}
        base.update(blocks or {})
        super().__init__(pos=pos, items=items, blocks=base)
        self.st["dimension"] = "overworld"
        self.exposed = list(exposed)
        self.picks = dict(picks or {})
        self.held = held

    def inventory(self, timeout=10):
        inv = super().inventory(timeout)
        for entry in inv["items"]:
            if (dur := self.picks.get(entry["name"])) is not None:
                entry["durability"] = [dur, 250]
        inv["held"] = next((copy.deepcopy(e) for e in inv["items"] if e["name"] == self.held), None)
        return inv

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None):
        self.calls.append(("find_blocks", {"types": types, "radius": radius}))
        known = ({"name": self.blocks[p], "pos": list(p)} for p in self.exposed if p in self.blocks)
        return {"blocks": [b for b in known if b["name"] in types][:limit]}


class DiamondAltRoutesTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()
        reset_all_world_memos()
        self.addCleanup(reset_all_voxel_caches)
        self.addCleanup(reset_all_world_memos)

    def test_opportunistic_lapis_and_deepslate_redstone_at_y_le_0_and_memo_recording(self):
        lapis_pos = (2, -39, 0)
        redstone_pos = (-2, -39, 0)
        diamond_pos = (0, -39, 2)
        world = _BonusDiamondWorld(
            pos=(0.5, -40.0, 0.5),
            items={"iron_pickaxe": 1},
            held="iron_pickaxe",
            picks={"iron_pickaxe": 50},
            blocks={
                lapis_pos: "lapis_ore",
                redstone_pos: "deepslate_redstone_ore",
                diamond_pos: "deepslate_diamond_ore",
            },
            exposed=(diamond_pos, lapis_pos, redstone_pos),
        )
        def _ore_dig(w, *, pos, **_):
            name = w.blocks.pop(tuple(pos))
            drop, qty = {
                "deepslate_diamond_ore": ("diamond", 1),
                "lapis_ore": ("lapis_lazuli", 5),
                "deepslate_redstone_ore": ("redstone", 4),
            }[name]
            w.items[drop] = w.items.get(drop, 0) + qty
            return {"dug": name, "gained": {drop: qty}}

        world.queue("dig", _ore_dig, _ore_dig, _ore_dig)
        ex, state = world.session()
        steps = drive(MineDiamond(count=1), ex, state)
        self.assertEqual([s["op"] for s in steps], ["dig", "dig", "dig"])
        self.assertEqual(state.count("diamond"), 1)
        self.assertEqual(state.count("lapis_lazuli"), 5)
        self.assertEqual(state.count("redstone"), 4)
        memo = memo_for(state)
        self.assertTrue(memo.is_blocked(lapis_pos, dimension="overworld", category="lapis_ore"))
        self.assertTrue(memo.is_blocked(redstone_pos, dimension="overworld", category="redstone_ore"))
        world.assert_exhausted()

    def test_skips_bonus_ores_above_y_0_and_skips_lava_adjacent_bonus_ore(self):
        # Above Y=0 -> strict depth gate skips scanning/mining
        high_pos = (2, 11, 0)
        high_world = _BonusDiamondWorld(
            pos=(0.5, 10.0, 0.5),
            items={"iron_pickaxe": 1},
            held="iron_pickaxe",
            picks={"iron_pickaxe": 50},
            blocks={high_pos: "lapis_ore"},
            exposed=(high_pos,),
        )
        ex_hi, state_hi = high_world.session()
        self.assertEqual(drive(collect_bonus_ores(ex_hi, state_hi), ex_hi), [])
        self.assertEqual(ex_hi.acts, [])
        self.assertEqual(high_world.calls, [])

        # At Y <= 0 with lava adjacent -> records in memo, never digs
        unsafe_pos = (2, -39, 0)
        lava_world = _BonusDiamondWorld(
            pos=(0.5, -40.0, 0.5),
            items={"iron_pickaxe": 1},
            held="iron_pickaxe",
            picks={"iron_pickaxe": 50},
            blocks={unsafe_pos: "deepslate_redstone_ore", (3, -39, 0): "lava"},
            exposed=(unsafe_pos,),
        )
        ex_lv, state_lv = lava_world.session()
        self.assertEqual(drive(collect_bonus_ores(ex_lv, state_lv), ex_lv), [])
        self.assertEqual(ex_lv.acts, [])
        seen = memo_for(state_lv).query("redstone_ore", dimension="overworld")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].pos, unsafe_pos)


class _EnchantAltWorld(ScriptedWorld):
    def __init__(self, *, items=None, blocks=None, xp=6, ench_map=None, held=None):
        ground = {(x, 63, z): "stone" for x in range(-2, 5) for z in range(-2, 5)}
        ground.update(blocks or {})
        super().__init__(blocks=ground, items=items or {})
        self.st.update(dimension="overworld", xp_level=xp)
        self.ench_map = dict(ench_map or {})
        self.held = held

    def inventory(self, timeout=10):
        inv = super().inventory(timeout)
        for row in inv["items"]:
            if row["name"] in self.ench_map:
                row.update(copy.deepcopy(self.ench_map[row["name"]]))
        inv["held"] = next((copy.deepcopy(r) for r in inv["items"] if r["name"] == self.held), None)
        return inv


def _find(*matches):
    return {"blocks": [{"name": name, "pos": list(pos)} for name, pos in matches]}


def _adv(adv_id, done=False):
    return {"ready": True, "advancements": [{"id": adv_id, "status": "completed"}] if done else []}


class EnchantAltRoutesTests(unittest.TestCase):
    def test_bookshelf_mining_supplies_books_without_sugarcane_and_avoids_silk_touch(self):
        shelf = (2, 64, 0)
        table = (0, 64, 2)
        world = _EnchantAltWorld(
            items={
                "iron_pickaxe": 1,
                "diamond_axe": 1,
                "iron_axe": 1,
                "diamond": 2,
                "obsidian": 4,
                "lapis_lazuli": 2,
            },
            blocks={shelf: "bookshelf", table: "crafting_table"},
            ench_map={"diamond_axe": {"enchantments": [{"name": "silk_touch", "level": 1}]}},
            held="diamond_axe",
        )
        # Note: diamond_axe has silk_touch, so EnchantItem should NOT early-return? Wait:
        # diamond_axe is enchanted so test mine_bookshelf_for_books directly for silk_touch avoidance,
        # and test full EnchantItem flow with unenchanted iron_axe!
        ex, state = world.session()
        world.queue("find_blocks", _find(("bookshelf", shelf)))

        def equip_axe(w, *, item, **_):
            self.assertEqual(item, "iron_axe")
            w.held = item
            return {"held": {"name": item}}

        def dig_shelf(w, *, pos, **_):
            self.assertEqual(tuple(pos), shelf)
            w.blocks.pop(shelf)
            w.items["book"] = w.items.get("book", 0) + 3
            return {"dug": "bookshelf", "gained": {"book": 3}}

        world.queue("equip", equip_axe)
        world.queue("dig", dig_shelf)
        steps = drive(mine_bookshelf_for_books(ex, state, needed=1), ex)
        self.assertEqual([s["op"] for s in steps], ["equip", "dig"])
        self.assertEqual(state.count("book"), 3)
        world.assert_exhausted()

        # Full EnchantItem run: no enchanting_table, no book, no sugarcane -> mines bookshelf -> crafts & enchants
        world2 = _EnchantAltWorld(
            items={"iron_pickaxe": 1, "iron_axe": 1, "diamond": 2, "obsidian": 4, "lapis_lazuli": 2},
            blocks={shelf: "bookshelf", table: "crafting_table"},
        )
        world2.queue("advancements", _adv("minecraft:story/enchant_item", False), _adv("minecraft:story/enchant_item", True))
        world2.queue(
            "find_blocks",
            _find(),                              # enchanting_table scan -> none
            _find(("bookshelf", shelf)),          # bookshelf scan
            _find(("crafting_table", table)),     # craft_recipe(enchanting_table)
        )
        world2.queue("equip", equip_axe)
        world2.queue("dig", dig_shelf)

        def craft_table(w, *, item, count, **_):
            self.assertEqual((item, count), ("enchanting_table", 1))
            w.items["book"] -= 1
            w.items["diamond"] -= 2
            w.items["obsidian"] -= 4
            w.items["enchanting_table"] = 1
            return {"crafted": item, "gained": 1}

        world2.queue("craft", craft_table)
        world2.queue(
            "window_view",
            {"window": "enchantment", "target": "enchanting_table", "sections": {"item": [], "lapis": []}, "options": []},
        )

        def enchant_act(w, *, ops, **_):
            w.items["lapis_lazuli"] -= 1
            w.ench_map["iron_pickaxe"] = {"enchantments": [{"name": "efficiency", "level": 1}]}
            return {
                "done": ops,
                "window": {"window": "enchantment", "sections": {"item": [], "lapis": []}, "options": []},
            }

        world2.queue("window_act", enchant_act)
        ex2, state2 = world2.session()
        steps2 = drive(EnchantItem(), ex2, state2)
        self.assertEqual(ex2.acts, ["equip", "dig", "craft", "place", "window/view", "window/act"])
        self.assertEqual(state2.count("book"), 2)
        self.assertTrue(steps2[-1]["advancement_confirmed"])
        world2.assert_exhausted()

    def test_grindstone_disenchants_uncursed_book_and_refuses_cursed_books(self):
        grind = (2, 64, 0)
        table = (0, 64, 2)
        # 1) Cursed books (binding_curse / vanishing_curse) are strictly refused
        for curse_payload in (
            {"enchantments": [{"name": "binding_curse", "level": 1}]},
            {"stored_enchantments": [{"name": "minecraft:vanishing_curse", "level": 1}]},
            {"components": {"minecraft:stored_enchantments": {"levels": {"minecraft:binding_curse": 1}}}},
        ):
            with self.subTest(curse=curse_payload):
                cursed_world = _EnchantAltWorld(
                    items={"enchanted_book": 1},
                    blocks={grind: "grindstone"},
                    ench_map={"enchanted_book": curse_payload},
                )
                ex_c, state_c = cursed_world.session()
                steps_c = drive(disenchant_book_at_grindstone(ex_c, state_c, needed=1), ex_c)
                self.assertEqual(steps_c, [])
                self.assertEqual(ex_c.acts, [])
                self.assertEqual(state_c.count("book"), 0)

        # 2) Uncursed enchanted_book disenchanted via grindstone inside full EnchantItem run
        world = _EnchantAltWorld(
            items={"iron_pickaxe": 1, "enchanted_book": 1, "diamond": 2, "obsidian": 4, "lapis_lazuli": 2},
            blocks={grind: "grindstone", table: "crafting_table"},
            ench_map={"enchanted_book": {"stored_enchantments": [{"name": "unbreaking", "level": 1}]}},
        )
        world.queue("advancements", _adv("minecraft:story/enchant_item", False), _adv("minecraft:story/enchant_item", True))
        world.queue(
            "find_blocks",
            _find(),                              # enchanting_table -> none
            _find(),                              # bookshelf -> none
            _find(("grindstone", grind)),         # grindstone -> found
            _find(("crafting_table", table)),     # craft_recipe(enchanting_table)
        )
        world.queue(
            "window_view",
            {"window": "grindstone", "sections": {"input": [], "output": []}},
            {"window": "enchantment", "target": "enchanting_table", "sections": {"item": [], "lapis": []}, "options": []},
        )

        def disenchant_act(w, *, ops, **_):
            self.assertEqual(ops, [
                {"op": "put", "item": "enchanted_book", "count": 1, "to": "input"},
                {"op": "take", "from": "output", "item": "book", "count": 1},
            ])
            w.items["enchanted_book"] -= 1
            w.items["book"] = w.items.get("book", 0) + 1
            return {"done": ops, "window": {"window": "grindstone", "sections": {"input": [], "output": []}}}

        def craft_table(w, *, item, count, **_):
            w.items["book"] -= 1
            w.items["diamond"] -= 2
            w.items["obsidian"] -= 4
            w.items["enchanting_table"] = 1
            return {"crafted": item, "gained": 1}

        def enchant_act(w, *, ops, **_):
            w.items["lapis_lazuli"] -= 1
            w.ench_map["iron_pickaxe"] = {"enchantments": [{"name": "efficiency", "level": 1}]}
            return {
                "done": ops,
                "window": {"window": "enchantment", "sections": {"item": [], "lapis": []}, "options": []},
            }

        world.queue("window_act", disenchant_act, enchant_act)
        world.queue("craft", craft_table)
        ex, state = world.session()
        steps = drive(EnchantItem(), ex, state)
        self.assertEqual(
            ex.acts,
            ["window/view", "window/act", "craft", "place", "window/view", "window/act"],
        )
        self.assertEqual(state.count("enchanted_book"), 0)
        self.assertEqual(state.count("book"), 0)
        self.assertTrue(steps[-1]["advancement_confirmed"])
        world.assert_exhausted()

    def test_acquire_books_without_sugarcane_falls_back_from_empty_bookshelf_to_grindstone(self):
        grind = (2, 64, 0)
        world = _EnchantAltWorld(
            items={"enchanted_book": 1},
            blocks={grind: "grindstone"},
            ench_map={"enchanted_book": {"enchantments": [{"name": "protection", "level": 1}]}},
        )
        world.queue("find_blocks", _find(), _find(("grindstone", grind)))
        world.queue("window_view", {"window": "grindstone", "sections": {"top": [], "result": []}})

        def disenchant(w, *, ops, **_):
            self.assertEqual(ops, [
                {"op": "put", "item": "enchanted_book", "count": 1, "to": "top"},
                {"op": "take", "from": "result", "item": "book", "count": 1},
            ])
            w.items["enchanted_book"] -= 1
            w.items["book"] = 1
            return {"done": ops, "window": {"window": "grindstone", "sections": {"top": [], "result": []}}}

        world.queue("window_act", disenchant)
        ex, state = world.session()
        steps = drive(acquire_books_without_sugarcane(ex, state, needed=1), ex)
        self.assertEqual([s["op"] for s in steps], ["window_view", "window_act"])
        self.assertEqual(state.count("book"), 1)
        world.assert_exhausted()


class _CampAltWorld(ScriptedWorld):
    def __init__(self, items, extra=None):
        floor = {(x, 63, z): "stone" for x in range(-3, 5) for z in range(-3, 4)}
        super().__init__(blocks={**floor, **(extra or {})}, items=items)
        self.st.update({"dimension": "overworld", "is_day": False, "is_sleeping": False})


class SurvivalCampAltRoutesTests(unittest.TestCase):
    def test_mixed_wool_picks_min_dye_color_and_bone_meal_to_white_bed(self):
        # 1) Planner picks color minimizing dye needed (2 white + 1 red + 1 white_dye + 2 red_dye -> white, 1 dye)
        w_min = _CampAltWorld({"white_wool": 2, "red_wool": 1, "white_dye": 1, "red_dye": 2, "oak_planks": 3})
        _, st_min = w_min.session()
        plan = plan_wool_dyeing(st_min)
        self.assertIsNotNone(plan)
        self.assertEqual((plan.color, plan.dyes_needed, plan.dye_item), ("white", 1, "white_dye"))

        # 2) Raw dye sources (lapis_lazuli, ink_sac, cocoa_beans) supported
        for src, color in (("lapis_lazuli", "blue"), ("ink_sac", "black"), ("cocoa_beans", "brown")):
            with self.subTest(src=src):
                w_src = _CampAltWorld({f"{color}_wool": 1, "white_wool": 1, "gray_wool": 1, src: 2, "oak_planks": 3})
                _, st_src = w_src.session()
                p_src = plan_wool_dyeing(st_src)
                self.assertIsNotNone(p_src)
                self.assertEqual((p_src.color, p_src.dyes_needed, p_src.dye_source_crafts), (color, 2, ((src, 2),)))

        # 3) End-to-end SleepInBed with 2 white_wool + 1 red_wool + 1 bone_meal -> white_dye -> white_wool -> white_bed -> sleep
        table = (-2, 64, 0)
        world = _CampAltWorld(
            {"white_wool": 2, "red_wool": 1, "bone_meal": 1, "oak_planks": 3},
            extra={table: "crafting_table"},
        )
        world.queue("advancements", _adv("minecraft:adventure/sleep_in_bed", False), _adv("minecraft:adventure/sleep_in_bed", True))
        world.queue("find_blocks", {"blocks": []}, {"blocks": [{"pos": list(table), "name": "crafting_table"}]})

        def craft_step(w, *, item, count, **_):
            self.assertEqual(count, 1)
            if item == "white_dye":
                w.items["bone_meal"] -= 1
                w.items["white_dye"] = w.items.get("white_dye", 0) + 1
            elif item == "white_wool":
                w.items["red_wool"] -= 1
                w.items["white_dye"] -= 1
                w.items["white_wool"] = w.items.get("white_wool", 0) + 1
            elif item == "white_bed":
                w.items["white_wool"] -= 3
                w.items["oak_planks"] -= 3
                w.items["white_bed"] = 1
            else:
                raise AssertionError(f"unexpected craft {item}")
            return {"crafted": item, "gained": 1}

        def place_bed(w, *, item, pos, **_):
            x, y, z = pos
            w.items[item] -= 1
            w.blocks[(x, y, z)] = w.blocks[(x + 1, y, z)] = item
            return {"placed": item, "pos": pos}

        def sleep_act(w, **_):
            w.st["is_sleeping"] = True
            return {"slept": True}

        world.queue("craft", craft_step, craft_step, craft_step)
        world.queue("place", place_bed)
        world.queue("interact", sleep_act)

        ex, state = world.session()
        mplan = SleepInBed.material_plan({}, state)
        self.assertIn(("bone_meal", 1, "consume"), mplan)
        self.assertIn(("oak_planks", 3, "consume"), mplan)

        steps = drive(SleepInBed(), ex, state)
        self.assertEqual(ex.acts, ["craft", "craft", "craft", "place", "interact"])
        self.assertEqual([req["item"] for op, req in world.calls if op == "craft"], ["white_dye", "white_wool", "white_bed"])
        self.assertTrue(steps[-1]["result"]["advancement_confirmed"])
        world.assert_exhausted()

    def test_mixed_wool_without_sufficient_dye_replans(self):
        world = _CampAltWorld({"white_wool": 1, "red_wool": 1, "blue_wool": 1, "white_dye": 1, "oak_planks": 3})
        world.queue("advancements", _adv("minecraft:adventure/sleep_in_bed", False))
        world.queue("find_blocks", {"blocks": []})
        ex, state = world.session()
        self.assertIsNone(plan_wool_dyeing(state))
        with self.assertRaisesRegex(Replan, "3 块同色羊毛"):
            drive(SleepInBed(), ex, state)
        self.assertEqual(ex.acts, [])
        world.assert_exhausted()


class _LodestoneAltWorld(ScriptedWorld):
    def __init__(self, *, items=None, blocks=None):
        ground = {(x, 63, z): "stone" for x in range(-2, 5) for z in range(-2, 5)}
        ground.update(blocks or {})
        super().__init__(blocks=ground, items=items or {})
        self.st["dimension"] = "overworld"
        self.binding = None
        self.selected = None

    def inventory(self, timeout=10):
        inv = super().inventory(timeout)
        for row in inv["items"]:
            if row["name"] == "compass" and self.binding is not None:
                row["components"] = {
                    "minecraft:lodestone_tracker": {
                        "tracked": True,
                        "target": {"pos": list(self.binding), "dimension": "minecraft:overworld"},
                    }
                }
            if row["name"] == self.selected:
                inv["held"] = row
        return inv


class LodestoneAltRoutesTests(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()
        self.addCleanup(reset_all_world_memos)

    def test_nearby_stonecutter_cuts_8_stone_directly_and_attunes_lodestone(self):
        sc_pos = (2, 64, 0)
        table_pos = (0, 64, 2)
        world = _LodestoneAltWorld(
            items={"compass": 1, "iron_ingot": 1, "stone_bricks": 3, "stone": 5},
            blocks={sc_pos: "stonecutter", table_pos: "crafting_table"},
        )
        world.queue("advancements", _adv("minecraft:adventure/use_lodestone", False), _adv("minecraft:adventure/use_lodestone", True))
        world.queue(
            "find_blocks",
            _find(),                              # lodestone -> none
            _find(),                              # containers -> none
            _find(("stonecutter", sc_pos)),       # stonecutter -> found
            _find(("crafting_table", table_pos)), # craft_recipe(lodestone)
        )
        world.queue(
            "window_view",
            {
                "window": "stonecutter",
                "sections": {"input": [], "output": []},
                "options": [
                    {"index": 0, "output": "stone_brick_stairs", "available": True},
                    {"index": 2, "output": "chiseled_stone_bricks", "available": True},
                ],
            },
        )

        def cut_act(w, *, ops, **_):
            mat = ops[0]["item"]
            qty = ops[0]["count"]
            self.assertEqual(ops[1], {"op": "select", "option": 2, "times": qty})
            self.assertEqual(ops[2], {"op": "take", "from": "output", "item": "chiseled_stone_bricks", "count": qty})
            w.items[mat] -= qty
            w.items["chiseled_stone_bricks"] = w.items.get("chiseled_stone_bricks", 0) + qty
            return {
                "done": ops,
                "window": {
                    "window": "stonecutter",
                    "sections": {"input": [], "output": []},
                    "options": [{"index": 2, "output": "chiseled_stone_bricks", "available": True}],
                },
            }

        def craft_lode(w, *, item, count, **_):
            self.assertEqual((item, count), ("lodestone", 1))
            for ing, need in RECIPES[item].ingredients:
                w.items[ing] -= need
            w.items[item] = 1
            return {"crafted": item, "gained": 1}

        def equip_compass(w, *, item, **_):
            w.selected = item
            return {"held": {"name": item}}

        def bind_lode(w, *, pos, **_):
            w.binding = tuple(pos)
            return {"block": "lodestone", "changed": True}

        world.queue("window_act", cut_act, cut_act)
        world.queue("craft", craft_lode)
        world.queue("equip", equip_compass)
        world.queue("interact", bind_lode)

        ex, state = world.session()
        steps = drive(AttuneLodestone(), ex, state)
        self.assertEqual(
            ex.acts,
            ["window/view", "window/act", "window/act", "craft", "place", "equip", "interact"],
        )
        self.assertEqual(state.count("stone_bricks"), 0)
        self.assertEqual(state.count("stone"), 0)
        self.assertEqual(state.count("chiseled_stone_bricks"), 0)
        self.assertIn(sc_pos, memo_for(state).stations("stonecutter", dimension="overworld"))
        self.assertTrue(steps[-1]["advancement_confirmed"])
        world.assert_exhausted()

    def test_held_stonecutter_placed_and_handles_option_unavailable_discovery(self):
        world = _LodestoneAltWorld(
            items={"compass": 1, "iron_ingot": 1, "stone": 8, "stonecutter": 1},
        )
        ex, state = world.session()
        plan = AttuneLodestone.material_plan({}, state)
        self.assertEqual(
            set(plan),
            {("compass", 1, "consume"), ("iron_ingot", 1, "consume"), ("stone", 8, "consume"), ("stonecutter", 1, "borrow")},
        )

        world.queue("find_blocks", _find())  # no nearby stonecutter -> place held
        world.queue(
            "window_view",
            {"window": "stonecutter", "sections": {"input": [], "output": []}},
        )

        def cut_retry(w, *, ops, **_):
            self.assertEqual(ops[1]["option"], 3)
            w.items["stone"] -= 8
            w.items["chiseled_stone_bricks"] = 8
            return {"done": ops, "window": {"window": "stonecutter", "sections": {"input": [], "output": []}}}

        world.queue(
            "window_act",
            ApiError(
                "option_unavailable",
                "option 0 is stone_slab",
                {
                    "window": {
                        "window": "stonecutter",
                        "sections": {"input": [], "output": []},
                        "options": [{"index": 3, "output": "chiseled_stone_bricks", "available": True}],
                    }
                },
            ),
            cut_retry,
        )

        steps = drive(cut_chiseled_bricks_at_stonecutter(ex, state, needed=8), ex)
        self.assertEqual(state.count("chiseled_stone_bricks"), 8)
        self.assertEqual(state.count("stone"), 0)
        self.assertEqual(state.count("stonecutter"), 0)
        self.assertTrue(any(s["op"] == "place" for s in steps))
        self.assertTrue(memo_for(state).has_station("stonecutter", dimension="overworld"))
        world.assert_exhausted()


if __name__ == "__main__":
    unittest.main()
