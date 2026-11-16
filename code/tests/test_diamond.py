import unittest
from copy import deepcopy

from mcagent.client import ApiError
from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills import make
from mcagent.skills.diamond import CraftDiamondGear, MineDiamond
from mcagent.skills.items import MINE_TRAILS
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import drive


START = (0.5, -53.0, 0.5)
ORE = (-2, -52, 0)
STAIR = (1, -54, 0)
CUTS = ((1, -52, 0), (1, -53, 0), STAIR)
TABLE = (1, -53, 0)
CHEST = (0, -53, -1)
IRON = "iron_pickaxe"


class DiamondWorld(ScriptedWorld):
    """Actions affecting inventories are explicitly scripted per case."""

    def __init__(self, *, items=None, blocks=None, exposed=(), picks=None, held=None, armor=None):
        super().__init__(pos=START, items=items, blocks={(0, -54, 0): "stone", **(blocks or {})})
        self.st["dimension"] = "overworld"
        self.exposed = list(exposed)
        self.picks = dict(picks or {})
        self.held = held
        self.armor = armor or {}

    def inventory(self, timeout=10):
        result = super().inventory(timeout)
        for entry in result["items"]:
            if (durability := self.picks.get(entry["name"])) is not None:
                entry["durability"] = [durability, 250]
        result["held"] = next((deepcopy(entry) for entry in result["items"] if entry["name"] == self.held), None)
        result["armor"] = deepcopy(self.armor)
        return result

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None):
        self.calls.append(("find_blocks", {"types": types, "radius": radius}))
        known = ({"name": self.blocks[pos], "pos": list(pos)} for pos in self.exposed if pos in self.blocks)
        return {"blocks": [block for block in known if block["name"] in types][:limit]}


def _equip(world, *, item, **kwargs):
    world.held = item
    return {"equipped": item}


def _dig(drop=None, *, break_pick=False, expose=None, irrigate=None):
    def action(world, *, pos, **kwargs):
        name = world.blocks.pop(tuple(pos))
        if drop:
            world.items[drop] = world.items.get(drop, 0) + 1
        if break_pick:
            del world.items[world.held]
            world.held = None
        if expose:
            world.exposed.append(expose)
        if irrigate:
            world.blocks[irrigate] = "water"
        return {"dug": name, "gained": {drop: 1} if drop else {}}
    return action


def _craft(world, *, item, count, **kwargs):
    assert count == 1
    world.items["diamond"] -= 4 if item == "diamond_boots" else 3
    if item == "diamond_pickaxe":
        world.items["stick"] -= 2
        world.picks[item] = 250
    world.items[item] = world.items.get(item, 0) + 1
    return {"crafted": item, "gained": 1}


def _container(name, count):
    return {"window": "chest", "sections": {"container": [{"name": name, "count": count}]}}


def _take(world, *, target, ops, **kwargs):
    assert target == {"pos": list(CHEST)}
    assert len(ops) == 1
    op = ops[0]
    assert op["op"] == "take" and op["from"] == "container"
    world.items[op["item"]] = world.items.get(op["item"], 0) + op["count"]
    return _container(op["item"], 0)


class DiamondTests(unittest.TestCase):
    def setUp(self):
        trails = MINE_TRAILS[:]
        MINE_TRAILS.clear()
        self.addCleanup(lambda: MINE_TRAILS.__setitem__(slice(None), trails))

    def test_registered_compatible_arguments_and_offline(self):
        self.assertEqual(make("mine_diamond", {"count": 4}).args["count"], 4)
        self.assertEqual(make("craft_diamond_gear", {"item": "diamond_pickaxe"}).verified, "offline")

    def test_missing_world_context_hides_loot_and_workstation_choices(self):
        world = DiamondWorld()
        world.st.pop("dimension")
        ex, state = world.session()
        for skill in (MineDiamond(count=1), CraftDiamondGear(item="diamond_boots")):
            self.assertIn("维度", skill.blocked(state))
            with self.assertRaisesRegex(Replan, "维度"):
                drive(skill, ex, state)
        self.assertEqual(ex.acts, [])

    def test_mine_already_owned_without_tool_or_scan(self):
        world = DiamondWorld(items={"diamond": 4})
        ex, state = world.session()
        self.assertEqual(drive(MineDiamond(), ex, state), [])
        self.assertEqual(world.calls, [])

    def test_mine_exact_count_credits_existing_diamond(self):
        world = DiamondWorld(items={"diamond": 3, IRON: 1},
                             blocks={ORE: "diamond_ore"}, exposed=(ORE,), picks={IRON: 20})
        world.queue("equip", _equip)
        world.queue("dig", _dig("diamond"))
        ex, state = world.session()
        self.assertEqual(MineDiamond.material_plan({"count": 4}, state), ((IRON, 1, "borrow"),))
        self.assertEqual([row["op"] for row in drive(MineDiamond(count=4), ex, state)], ["equip", "dig"])
        self.assertEqual(state.count("diamond"), 4)
        world.assert_exhausted()

    def test_loot_diamond_before_tool_requirement_one_window_op(self):
        world = DiamondWorld(blocks={CHEST: "chest"}, exposed=(CHEST,))
        world.queue("window_view", _container("diamond", 1), _container("diamond", 0))
        world.queue("window_act", _take)
        ex, state = world.session()
        self.assertEqual([row["op"] for row in drive(MineDiamond(count=1), ex, state)],
                         ["window_view", "window_act", "window_view"])
        self.assertEqual(state.count("diamond"), 1)
        world.assert_exhausted()

    def test_generic_9x3_container_and_partial_inventory_full_stops_before_mining(self):
        world = DiamondWorld(items={IRON: 1}, picks={IRON: 20},
                             blocks={CHEST: "chest", ORE: "diamond_ore"}, exposed=(CHEST, ORE))
        before = {"window": "generic_9x3", "target": "chest",
                  "sections": {"container": [{"name": "diamond", "count": 4}]}}
        after = {"window": "generic_9x3", "target": "chest",
                 "sections": {"container": [{"name": "diamond", "count": 2}]}}

        def partial_take(world, *, target, ops, **kwargs):
            world.items["diamond"] = 2
            return {"done": [{"op": "take", "took": {"diamond": 2}, "from": "container", "inventory_full": True}]}

        world.queue("window_view", before, after)
        world.queue("window_act", partial_take)
        ex, state = world.session()
        skill = MineDiamond(count=4)
        seen = []
        with self.assertRaisesRegex(Replan, "inventory_full"):
            while not skill.step(ex, state):
                seen.append(skill.last)
        self.assertEqual([row["op"] for row in seen],
                         ["window_view", "window_act", "window_view", "diamond_partial"])
        self.assertNotIn("dig", ex.acts)
        world.assert_exhausted()

    def test_copper_or_unknown_durability_is_insufficient(self):
        for item, picks in (("copper_pickaxe", {"copper_pickaxe": 10}), (IRON, {})):
            with self.subTest(item=item):
                world = DiamondWorld(items={item: 1}, blocks={ORE: "diamond_ore"}, exposed=(ORE,), picks=picks)
                ex, state = world.session()
                with self.assertRaisesRegex(Replan, "耐久"):
                    drive(MineDiamond(count=1), ex, state)
                self.assertEqual(ex.acts, [])

    def test_mine_diamond_skips_dig_unconfirmed_and_prefers_non_silk_touch_pick(self):
        second_ore = (0, -52, 2)
        world = DiamondWorld(
            items={"diamond_pickaxe": 1, IRON: 1},
            picks={"diamond_pickaxe": 100, IRON: 40},
            blocks={ORE: "diamond_ore", second_ore: "diamond_ore"},
            exposed=(ORE, second_ore),
        )
        orig_inv = world.inventory

        def inv_with_enchants(timeout=10):
            res = orig_inv(timeout)
            for entry in res["items"]:
                if entry["name"] == "diamond_pickaxe":
                    entry["enchantments"] = [{"name": "silk_touch", "level": 1}]
                elif entry["name"] == IRON:
                    entry["enchantments"] = [{"name": "efficiency", "level": 2}]
            if res.get("held"):
                res["held"] = next(e for e in res["items"] if e["name"] == res["held"]["name"])
            return res

        world.inventory = inv_with_enchants
        world.queue("equip", _equip)
        world.queue(
            "dig",
            ApiError("dig_unconfirmed", "unconfirmed", {"pos": list(ORE), "eye_dist": 4.6}),
            _dig("diamond"),
        )
        ex, state = world.session()
        steps = drive(MineDiamond(count=1), ex, state)
        self.assertEqual(world.held, IRON)
        self.assertIn("ore_skip", [s["op"] for s in steps])
        self.assertEqual(state.count("diamond"), 1)
        world.assert_exhausted()

    def test_supported_descent_then_rescan_for_exposed_ore(self):
        deep_ore = (2, -54, 0)
        world = DiamondWorld(items={IRON: 1}, held=IRON, picks={IRON: 20},
                             blocks={**dict.fromkeys(CUTS, "deepslate"), (1, -55, 0): "deepslate",
                                     deep_ore: "deepslate_diamond_ore"})

        def move(world, *, pos, **kwargs):
            world.st["pos"] = pos
            world.exposed.append(deep_ore)
            return {"pos": pos}

        world.queue("dig", _dig(), _dig(), _dig(), _dig("diamond"))
        world.queue("move_to", move)
        ex, state = world.session()
        steps = drive(MineDiamond(count=1), ex, state)
        self.assertEqual([row["op"] for row in steps], ["dig", "dig", "dig", "move_to", "dig"])
        self.assertEqual([tuple(row["pos"]) for row in steps if row["op"] == "dig"], [*CUTS, deep_ore])
        self.assertEqual(world.blocks[(1, -55, 0)], "deepslate")
        self.assertEqual(world.blocks[(0, -54, 0)], "stone")
        world.assert_exhausted()

    def test_lava_water_and_missing_support_stop_excavation(self):
        for support, hazard in (("deepslate", "lava"), ("deepslate", "water"), ("air", "air")):
            with self.subTest(support=support, hazard=hazard):
                world = DiamondWorld(items={IRON: 1}, picks={IRON: 20},
                                     blocks={**dict.fromkeys(CUTS, "deepslate"),
                                             (1, -55, 0): support, (2, -53, 0): hazard})
                ex, state = world.session()
                with self.assertRaises(EnvBlocked) as blocked:
                    drive(MineDiamond(count=1), ex, state)
                self.assertEqual(blocked.exception.missing_predicate, "reachable_diamond")
                self.assertIn("0/24", blocked.exception.searched_volume)
                self.assertEqual(blocked.exception.wake_on, "chunk_moved")
                self.assertEqual(ex.acts, [])

    def test_water_exposed_by_cut_checks_other_dry_direction(self):
        side_cuts = ((0, -52, 1), (0, -53, 1), (0, -54, 1))
        world = DiamondWorld(items={IRON: 1}, held=IRON, picks={IRON: 20},
                             blocks={**dict.fromkeys((*CUTS, *side_cuts), "deepslate"),
                                     (1, -55, 0): "deepslate", (0, -55, 1): "deepslate"})
        world.queue("dig", _dig(irrigate=(2, -52, 0)), _dig(), _dig(), _dig())
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(MineDiamond(count=1), ex, state)
        self.assertEqual([tuple(args["pos"]) for name, args in world.calls if name == "dig"], [CUTS[0], *side_cuts])
        self.assertEqual(world.st["pos"], [0.5, -54.0, 1.5])

    def test_waterlogged_neighbor_stops_harvest(self):
        class WaterloggedWorld(DiamondWorld):
            def get_block(self, pos):
                if tuple(pos) == (-3, -52, 0):
                    return {"name": "oak_stairs", "properties": {"waterlogged": True}}
                return super().get_block(pos)

        world = WaterloggedWorld(items={IRON: 1}, blocks={ORE: "diamond_ore"},
                                 exposed=(ORE,), picks={IRON: 20})
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(MineDiamond(count=1), ex, state)
        self.assertEqual(ex.acts, [])

    def test_mid_cut_rescan_switches_to_reachable_ore_before_descending(self):
        world = DiamondWorld(items={IRON: 1}, held=IRON, picks={IRON: 20},
                             blocks={**dict.fromkeys(CUTS, "deepslate"), (1, -55, 0): "deepslate",
                                     ORE: "diamond_ore"})
        world.queue("dig", _dig(expose=ORE), _dig("diamond"))
        ex, state = world.session()
        steps = drive(MineDiamond(count=1), ex, state)
        self.assertEqual([tuple(s["pos"]) for s in steps], [CUTS[0], ORE])
        self.assertEqual(world.st["pos"], list(START))
        world.assert_exhausted()

    def test_receipt_success_without_landing_stops_descent(self):
        world = DiamondWorld(items={IRON: 1}, held=IRON, picks={IRON: 20},
                             blocks={(1, -55, 0): "deepslate"})
        world.queue("move_to", {"pos": [1.5, -54.0, 0.5]})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "落脚点"):
            drive(MineDiamond(count=1), ex, state)
        self.assertEqual(ex.acts, ["move_to"])
        world.assert_exhausted()

    def test_false_diamond_receipt_fails_inventory_postcondition(self):
        world = DiamondWorld(items={IRON: 1}, held=IRON, picks={IRON: 20},
                             blocks={ORE: "diamond_ore"}, exposed=(ORE,))
        world.queue("dig", {"dug": "diamond_ore", "gained": {"diamond": 1}})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "背包未确认"):
            drive(MineDiamond(count=1), ex, state)
        self.assertEqual(state.count("diamond"), 0)
        world.assert_exhausted()

    def test_one_diamond_keeps_scoring_evidence_when_batch_is_partial(self):
        world = DiamondWorld(items={IRON: 1}, held=IRON, picks={IRON: 20},
                             blocks={ORE: "diamond_ore"}, exposed=(ORE,))
        world.queue("dig", _dig("diamond"))
        ex, state = world.session()
        skill = MineDiamond(count=4)
        self.assertFalse(skill.step(ex, state))
        self.assertFalse(skill.step(ex, state))
        self.assertEqual(skill.last["op"], "diamond_partial")
        self.assertEqual(skill.last["result"], {"held": 1, "gained": 1, "requested": 4, "inventory_criterion": True})
        with self.assertRaises(EnvBlocked):
            skill.step(ex, state)
        self.assertEqual(ex.acts, ["dig"])

    def test_broken_mining_pick_is_replaced_before_second_harvest(self):
        next_ore = (1, -52, 0)
        world = DiamondWorld(items={IRON: 1, "diamond_pickaxe": 1}, held=IRON, picks={IRON: 1, "diamond_pickaxe": 40},
                             blocks={ORE: "diamond_ore", next_ore: "diamond_ore"}, exposed=(ORE,))
        world.queue("equip", _equip)
        world.queue("dig", _dig("diamond", break_pick=True, expose=next_ore), _dig("diamond"))
        ex, state = world.session()
        steps = drive(MineDiamond(count=2), ex, state)
        self.assertEqual([s["op"] for s in steps], ["dig", "equip", "dig"])
        self.assertEqual(steps[1]["item"], "diamond_pickaxe")
        world.assert_exhausted()

    def test_mine_abort_after_cut_never_issues_next_action(self):
        world = DiamondWorld(items={IRON: 1}, held=IRON, picks={IRON: 20},
                             blocks={**dict.fromkeys(CUTS, "deepslate"), (1, -55, 0): "deepslate"})
        world.queue("dig", _dig())
        ex, state = world.session()
        skill = MineDiamond(count=1)
        self.assertFalse(skill.step(ex, state))
        skill.abort()
        self.assertIsNone(skill._flow)
        self.assertFalse(state.st.get("sneaking"))
        self.assertEqual(ex.acts, ["dig"])
        self.assertEqual(world.st["pos"], list(START))
        world.assert_exhausted()

    def test_existing_armor_and_usable_netherite_pick_skip_actions(self):
        world = DiamondWorld(items={"netherite_pickaxe": 1}, picks={"netherite_pickaxe": 40},
                             armor={"chest": {"name": "diamond_chestplate", "count": 1}})
        ex, state = world.session()
        for item in ("diamond_boots", "diamond_pickaxe"):
            self.assertEqual(drive(CraftDiamondGear(item=item), ex, state), [])
            self.assertEqual(CraftDiamondGear.material_plan({"item": item}, state), ())
        self.assertEqual(world.calls, [])

    def test_loot_diamond_armor_without_diamonds(self):
        world = DiamondWorld(blocks={CHEST: "chest"}, exposed=(CHEST,))
        world.queue("window_view", _container("diamond_helmet", 1), _container("diamond_helmet", 0))
        world.queue("window_act", _take)
        ex, state = world.session()
        steps = drive(CraftDiamondGear(item="diamond_boots"), ex, state)
        self.assertEqual([row["op"] for row in steps], ["window_view", "window_act", "window_view"])
        self.assertEqual(state.count("diamond_helmet"), 1)
        world.assert_exhausted()

    def test_recipe_plans_and_confirmed_crafts(self):
        for item, ingredients in (("diamond_boots", {"diamond": 4}),
                                  ("diamond_pickaxe", {"diamond": 3, "stick": 2})):
            with self.subTest(item=item):
                world = DiamondWorld(items=ingredients, blocks={TABLE: "crafting_table"}, exposed=(TABLE,))
                world.queue("craft", _craft)
                ex, state = world.session()
                self.assertEqual(CraftDiamondGear.material_plan({"item": item}, state),
                                 tuple((name, count, "consume") for name, count in ingredients.items()))
                self.assertEqual([row["op"] for row in drive(CraftDiamondGear(item=item), ex, state)], ["craft"])
                self.assertEqual(state.count(item), 1)
                self.assertEqual(CraftDiamondGear.material_plan({"item": item}, state), ())
                world.assert_exhausted()

    def test_three_diamonds_are_not_enough_for_boots(self):
        world = DiamondWorld(items={"diamond": 3, "stick": 2}, blocks={TABLE: "crafting_table"}, exposed=(TABLE,))
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "缺材料"):
            drive(CraftDiamondGear(item="diamond_boots"), ex, state)
        self.assertEqual(ex.acts, [])

    def test_no_table_environment_fields(self):
        world = DiamondWorld(items={"diamond": 4})
        ex, state = world.session()
        with self.assertRaises(EnvBlocked) as blocked:
            drive(CraftDiamondGear(item="diamond_boots"), ex, state)
        self.assertEqual(blocked.exception.missing_predicate, "station_ready:crafting_table")
        self.assertIn("半径", blocked.exception.searched_volume)
        self.assertEqual(blocked.exception.wake_on, "chunk_moved")
        self.assertEqual(ex.acts, [])

    def test_place_workbench_locally_leaves_another_exit(self):
        world = DiamondWorld(items={"diamond": 4, "crafting_table": 1},
                             blocks={(1, -54, 0): "stone", (0, -54, 1): "stone"})

        def place(world, *, item, pos, **kwargs):
            world.blocks[tuple(pos)] = item
            world.items[item] -= 1
            world.exposed.append(tuple(pos))
            return {"placed": item, "pos": pos}

        world.queue("place", place)
        world.queue("craft", _craft)
        ex, state = world.session()
        steps = drive(CraftDiamondGear(item="diamond_boots"), ex, state)
        self.assertEqual([row["op"] for row in steps], ["place", "craft"])
        self.assertEqual(world.blocks[TABLE], "crafting_table")
        self.assertEqual(state.count("diamond_boots"), 1)
        world.assert_exhausted()

    def test_never_places_table_in_only_exit(self):
        world = DiamondWorld(items={"diamond": 4, "crafting_table": 1}, blocks={(1, -54, 0): "stone"})
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(CraftDiamondGear(item="diamond_boots"), ex, state)
        self.assertEqual(ex.acts, [])

    def test_opaque_or_remote_table_does_not_trigger_unobserved_route(self):
        distant_table = (8, -53, 0)
        world = DiamondWorld(items={"diamond": 4}, exposed=(distant_table, TABLE),
                             blocks={distant_table: "crafting_table", TABLE: "crafting_table",
                                     (1, -52, 0): "deepslate", (1, -53, 0): "crafting_table"})
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(CraftDiamondGear(item="diamond_boots"), ex, state)
        self.assertEqual(ex.acts, [])

    def test_false_craft_receipt_fails_inventory_postcondition(self):
        world = DiamondWorld(items={"diamond": 4}, blocks={TABLE: "crafting_table"}, exposed=(TABLE,))
        world.queue("craft", {"crafted": "diamond_boots", "gained": 1})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "背包仅增加"):
            drive(CraftDiamondGear(item="diamond_boots"), ex, state)
        self.assertEqual(state.count("diamond_boots"), 0)
        world.assert_exhausted()

    def test_craft_abort_after_window_read_does_not_mutate(self):
        world = DiamondWorld(blocks={CHEST: "chest"}, exposed=(CHEST,))
        world.queue("window_view", _container("diamond_helmet", 1))
        ex, state = world.session()
        skill = CraftDiamondGear(item="diamond_boots")
        self.assertFalse(skill.step(ex, state))
        skill.abort()
        self.assertIsNone(skill._flow)
        self.assertEqual(ex.acts, ["window/view"])
        self.assertEqual(state.count("diamond_helmet"), 0)
        world.assert_exhausted()

    def test_unreachable_container_falls_back_to_recipe(self):
        world = DiamondWorld(items={"diamond": 4}, blocks={CHEST: "chest", TABLE: "crafting_table"},
                             exposed=(CHEST, TABLE))
        world.queue("window_view", ApiError("out_of_reach", "too far"))
        world.queue("craft", _craft)
        ex, state = world.session()
        drive(CraftDiamondGear(item="diamond_boots"), ex, state)
        self.assertEqual(ex.acts, ["window/view", "craft"])
        world.assert_exhausted()


if __name__ == "__main__":
    unittest.main()
