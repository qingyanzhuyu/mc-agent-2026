"""Stage-2 workstations & entity interaction tests (L2 primitives + L3 skills)."""
from __future__ import annotations

import unittest
from copy import deepcopy

import mcagent.skills  # noqa: F401
from mcagent.client import ApiError
from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.registry import REGISTRY, contract_errors
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.t2_workstations import BrewPotion, CureZombieVillager, SmithNetheriteHoe, TradeVillager, TrimArmor
from mcagent.skills.testing import drive
from mcagent.skills.trade_smith_brew import brew_batch, smith_once, trade_once
from mcagent.skills.world_memo import memo_for


class WorkstationWorld(ScriptedWorld):
    """Stateful ScriptedWorld for merchant, smithing, brewing, crafting, and entity ops."""

    def __init__(self, *, blocks=None, items=None, entities=None, merchant_options=None, item_meta=None):
        ground = {(x, 63, z): "stone" for x in range(-4, 5) for z in range(-4, 5)}
        if blocks:
            ground.update({tuple(k): v for k, v in blocks.items()})
        super().__init__(blocks=ground, items=items or {})
        self.st["dimension"] = "overworld"
        self.entities, self.item_meta = list(entities or []), dict(item_meta or {})
        self.merchant_options = list(merchant_options or [])
        self.smith = {"template": [], "base": [], "addition": [], "output": []}
        self.brew = {"bottles": [], "ingredient": [], "fuel": []}
        self.brew_fuel = self.brew_polls = self.brew_time = 0
        self.smith_empty_output = self.brew_hang = False

    def inventory(self, timeout=10):
        inv = super().inventory(timeout)
        for row in inv["items"]:
            row.update(self.item_meta.get(row["name"], {}))
        return inv

    def _take_item(self, item: str, count: int = 1) -> None:
        self._need(item)
        if self.items[item] < count:
            raise ApiError("missing_item", f"need {count} {item}")
        self.items[item] -= count

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None):
        if "find_blocks" in self._replies:
            return self._reply("find_blocks", dict(types=types, radius=radius, limit=limit, properties=properties, mature=mature))
        allowed, (cx, cy, cz), hits = set(types or ()), self.st["pos"], []
        for (bx, by, bz), name in sorted(self.blocks.items()):
            if not allowed or name in allowed:
                d = ((bx - cx) ** 2 + (by - cy) ** 2 + (bz - cz) ** 2) ** 0.5
                if d <= radius:
                    hits.append({"name": name, "pos": [bx, by, bz], "dist": round(d, 2)})
        hits.sort(key=lambda b: b["dist"])
        return {"total": len(hits), "blocks": hits[:limit]}

    def get_entities(self, radius=32, types=None):
        if "get_entities" in self._replies:
            return self._reply("get_entities", dict(radius=radius, types=types))
        allowed, (cx, cy, cz) = set(types or ()), self.st["pos"]
        return {"entities": [
            deepcopy(e) for e in self.entities
            if (not allowed or e.get("name") in allowed or e.get("type") in allowed)
            and ((e["pos"][0] - cx) ** 2 + (e["pos"][1] - cy) ** 2 + (e["pos"][2] - cz) ** 2) ** 0.5 <= radius
        ]}

    def craft(self, item, count=1, *, stop_on=None):
        if "craft" in self._replies:
            return self._reply("craft", dict(item=item, count=count, stop_on=stop_on))
        self.calls.append(("craft", {"item": item, "count": count}))
        if item != "diamond_hoe":
            raise ApiError("unknown_recipe", f"unsupported craft {item}")
        self._take_item("diamond", 2 * count)
        self._take_item("stick", 2 * count)
        self.items["diamond_hoe"] = self.items.get("diamond_hoe", 0) + count
        return {"crafted": item, "gained": count}

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None):
        req = dict(item=item, target=target, ms=ms, timeout_ms=timeout_ms, source=source, stop_on=stop_on)
        if "use_item" in self._replies:
            return self._reply("use_item", req)
        self.calls.append(("use_item", {"item": item, "target": deepcopy(target)}))
        self._take_item(item, 1)
        return {"used": {item: 1}, "thrown": True}

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None):
        req = dict(pos=pos, entity_id=entity_id, item=item, stop_on=stop_on)
        if "interact" in self._replies:
            return self._reply("interact", req)
        self.calls.append(("interact", {"pos": pos, "entity_id": entity_id, "item": item}))
        if item is not None:
            self._take_item(item, 1)
        return {"entity": {"id": entity_id}, "used": {item: 1} if item else {}}

    def window_view(self, target, *, stop_on=None):
        if "window_view" in self._replies:
            return self._reply("window_view", dict(target=target, stop_on=stop_on))
        if "entity_id" in target:
            return {"window": "merchant", "sections": {"payment1": [], "payment2": [], "output": []}, "options": deepcopy(self.merchant_options)}
        block = self.blocks.get(tuple(target["pos"]), "air")
        if block == "smithing_table":
            return {"window": "smithing", "target": "smithing_table", "sections": deepcopy(self.smith)}
        if block == "brewing_stand":
            if self.brew_polls > 0:
                self.brew_polls -= 1
                if self.brew_polls == 0 and not self.brew_hang:
                    self.brew_time = 0
                    ing = self.brew["ingredient"][0]["name"] if self.brew["ingredient"] else ""
                    self.brew["ingredient"] = []
                    for b in self.brew["bottles"]:
                        b.update(name="splash_potion", potion="weakness") if ing == "gunpowder" else b.update(potion="weakness" if ing == "fermented_spider_eye" else "mundane")
            return {"window": "brewing_stand", "target": "brewing_stand", "sections": deepcopy(self.brew), "info": {"fuel_left": self.brew_fuel, "brew_time_left": self.brew_time}}
        raise ApiError("no_window", f"no window for {block}")

    def window_act(self, target, ops, *, stop_on=None):
        if "window_act" in self._replies:
            return self._reply("window_act", dict(target=target, ops=ops, stop_on=stop_on))
        self.calls.append(("window_act", {"target": deepcopy(target), "ops": deepcopy(ops)}))
        done = []
        if "entity_id" in target:
            for op in ops:
                opt = next(o for o in self.merchant_options if o["index"] == op["option"])
                for pay in opt["give"]:
                    self._take_item(pay["name"], pay["count"])
                self.items[opt["get"]["name"]] = self.items.get(opt["get"]["name"], 0) + opt["get"]["count"]
                done.append({"op": "select", "option": op["option"]})
            return {"done": done, "window": self.window_view(target)}
        block = self.blocks.get(tuple(target["pos"]), "air")
        for op in ops:
            if op["op"] == "put":
                item, count, to = op["item"], op.get("count", 1), op["to"]
                self._take_item(item, count)
                if block == "smithing_table":
                    self.smith[to] = [{"name": item, "count": count}]
                    if all(self.smith[k] for k in ("template", "base", "addition")) and not self.smith_empty_output:
                        base, add, tpl = self.smith["base"][0]["name"], self.smith["addition"][0]["name"], self.smith["template"][0]["name"]
                        self.smith["output"] = [{"name": "netherite_hoe" if (base == "diamond_hoe" and add == "netherite_ingot" and "upgrade" in tpl) else base, "count": 1}]
                elif block == "brewing_stand":
                    if to == "fuel":
                        self.brew_fuel += 20 * count
                    elif to == "bottles":
                        self.brew["bottles"].append({"name": item, "count": count, "slot": len(self.brew["bottles"]), **self.item_meta.get(item, {})})
                    elif to == "ingredient":
                        self.brew["ingredient"], self.brew_polls, self.brew_time = [{"name": item, "count": count}], 2, 20
                done.append({"op": "put", "put": item, "count": count, "to": to})
            elif op["op"] == "take" and block == "smithing_table":
                row = self.smith["output"].pop(0)
                for k in ("template", "base", "addition"):
                    self.smith[k] = []
                self.items[row["name"]] = self.items.get(row["name"], 0) + row["count"]
                done.append({"op": "take", "took": {row["name"]: row["count"]}, "from": "output"})
            elif op["op"] == "take" and block == "brewing_stand":
                b = self.brew["bottles"].pop(0)
                self.items[b["name"]] = self.items.get(b["name"], 0) + b["count"]
                if "potion" in b:
                    self.item_meta[b["name"]] = {"potion": b["potion"]}
                done.append({"op": "take", "took": {b["name"]: b["count"]}, "from": "bottles"})
        return {"done": done, "window": self.window_view(target)}


def _merchant_opts():
    return [
        {"index": 0, "available": False, "uses": 12, "max_uses": 12, "give": [{"name": "emerald", "count": 1}], "get": {"name": "bread", "count": 6}},
        {"index": 1, "available": True, "uses": 0, "max_uses": 12, "give": [{"name": "emerald", "count": 20}], "get": {"name": "golden_carrot", "count": 3}},
        {"index": 2, "available": True, "uses": 0, "max_uses": 12, "give": [{"name": "emerald", "count": 1}], "get": {"name": "bread", "count": 6}},
        {"index": 3, "available": True, "uses": 0, "max_uses": 12, "give": [{"name": "emerald", "count": 2}], "get": {"name": "golden_carrot", "count": 3}},
    ]


class Stage2WorkstationsTests(unittest.TestCase):
    def setUp(self):
        _COOLDOWN.clear()
        self.addCleanup(_COOLDOWN.clear)

    def test_registry_and_contract_checks(self):
        for cls in (TradeVillager, TrimArmor, SmithNetheriteHoe, BrewPotion, CureZombieVillager):
            with self.subTest(skill=cls.name):
                self.assertEqual(contract_errors(cls), [])
                self.assertEqual(cls.verified, "offline")
                self.assertIs(REGISTRY.get(cls.name), cls)
        for fn in (trade_once, smith_once, brew_batch):
            self.assertNotIn(fn.__name__, REGISTRY)
            self.assertNotIn(fn, REGISTRY.values())

    def test_trade_once_prefers_affordable_option_and_confirms_delta(self):
        world = WorkstationWorld(items={"emerald": 5}, merchant_options=_merchant_opts())
        ex, state = world.session()
        drive(trade_once(ex, state, 101, preferred_items=("golden_carrot",)), ex)
        acts = [c["ops"] for m, c in world.calls if m == "window_act"]
        self.assertEqual(len(acts), 1)
        select_ops = [op for op in acts[0] if op.get("op") == "select"]
        self.assertEqual((len(select_ops), select_ops[0]["option"]), (1, 3))
        self.assertEqual((state.count("emerald"), state.count("golden_carrot")), (3, 3))

    def test_trade_once_no_affordable_option_replans_without_window_act(self):
        world = WorkstationWorld(items={"emerald": 1}, merchant_options=_merchant_opts()[:2])
        ex, state = world.session()
        with self.assertRaises(Replan):
            drive(trade_once(ex, state, 101), ex)
        self.assertEqual(ex.acts, ["window/view"])

    def test_trade_once_window_failure_or_unconfirmed_delta_replans(self):
        for reply in (ApiError("option_unavailable", "locked"), {"done": [{"op": "select", "option": 2}]}):
            with self.subTest(reply=reply):
                world = WorkstationWorld(items={"emerald": 5}, merchant_options=_merchant_opts())
                world.queue("window_act", reply)
                ex, state = world.session()
                with self.assertRaises(Replan):
                    drive(trade_once(ex, state, 101), ex)
                world.assert_exhausted()

    def test_smith_once_armor_trim_and_netherite_upgrade(self):
        w1 = WorkstationWorld(blocks={(2, 64, 0): "smithing_table"}, items={"coast_armor_trim_smithing_template": 1, "iron_chestplate": 1, "emerald": 1})
        ex1, st1 = w1.session()
        drive(smith_once(ex1, st1, (2, 64, 0), template="coast_armor_trim_smithing_template", base="iron_chestplate", addition="emerald", expected_output="iron_chestplate"), ex1)
        self.assertEqual((st1.count("iron_chestplate"), st1.count("coast_armor_trim_smithing_template"), st1.count("emerald")), (1, 0, 0))

        w2 = WorkstationWorld(blocks={(2, 64, 0): "smithing_table"}, items={"netherite_upgrade_smithing_template": 1, "diamond_hoe": 1, "netherite_ingot": 1})
        ex2, st2 = w2.session()
        drive(smith_once(ex2, st2, (2, 64, 0), template="netherite_upgrade_smithing_template", base="diamond_hoe", addition="netherite_ingot", expected_output="netherite_hoe"), ex2)
        self.assertEqual((st2.count("netherite_hoe"), st2.count("diamond_hoe"), st2.count("netherite_ingot")), (1, 0, 0))

    def test_smith_once_empty_output_or_wrong_window_replans(self):
        w_empty = WorkstationWorld(blocks={(2, 64, 0): "smithing_table"}, items={"netherite_upgrade_smithing_template": 1, "diamond_hoe": 1, "netherite_ingot": 1})
        w_empty.smith_empty_output = True
        ex1, st1 = w_empty.session()
        with self.assertRaises(Replan):
            drive(smith_once(ex1, st1, (2, 64, 0), template="netherite_upgrade_smithing_template", base="diamond_hoe", addition="netherite_ingot"), ex1)

        w_bad = WorkstationWorld(blocks={(2, 64, 0): "smithing_table"}, items={"netherite_upgrade_smithing_template": 1, "diamond_hoe": 1, "netherite_ingot": 1})
        w_bad.queue("window_view", {"window": "generic_9x3", "sections": {"container": []}})
        ex2, st2 = w_bad.session()
        with self.assertRaises(Replan):
            drive(smith_once(ex2, st2, (2, 64, 0), template="netherite_upgrade_smithing_template", base="diamond_hoe", addition="netherite_ingot"), ex2)
        self.assertEqual(ex2.acts, ["window/view"])

    def test_brew_batch_happy_path_and_timeout(self):
        world = WorkstationWorld(blocks={(2, 64, 0): "brewing_stand"}, items={"blaze_powder": 1, "potion": 1, "redstone": 1}, item_meta={"potion": {"potion": "water"}})
        ex, state = world.session()
        drive(brew_batch(ex, state, (2, 64, 0), bottle_item="potion", ingredient="redstone", poll_sleep_s=0.0, sleep_fn=lambda _: None), ex)
        self.assertEqual((state.count("blaze_powder"), state.count("redstone"), state.count("potion")), (0, 0, 1))

        w_to = WorkstationWorld(blocks={(2, 64, 0): "brewing_stand"}, items={"blaze_powder": 1, "potion": 1, "redstone": 1})
        w_to.brew_hang = True
        ex_t, st_t = w_to.session()
        with self.assertRaises(Replan):
            drive(brew_batch(ex_t, st_t, (2, 64, 0), bottle_item="potion", ingredient="redstone", max_polls=2, poll_sleep_s=0.0, sleep_fn=lambda _: None), ex_t)

    def test_trade_villager_happy_path_and_env_blocked(self):
        world = WorkstationWorld(items={"emerald": 5}, entities=[{"id": 101, "name": "villager", "type": "villager", "pos": [2.5, 64.0, 0.5]}], merchant_options=_merchant_opts())
        ex, state = world.session()
        before, skill = dict(world.items), TradeVillager()
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state, before), "")

        ex_n, st_n = WorkstationWorld(items={"emerald": 5}, entities=[]).session()
        with self.assertRaises(EnvBlocked) as caught:
            drive(TradeVillager(), ex_n, st_n)
        self.assertEqual((caught.exception.missing_predicate, caught.exception.wake_on), ("villager", "chunk_moved"))

    def test_trim_armor_happy_path_missing_materials_and_missing_station(self):
        world = WorkstationWorld(blocks={(2, 64, 0): "smithing_table"}, items={"coast_armor_trim_smithing_template": 1, "iron_chestplate": 1, "emerald": 1})
        ex, state = world.session()
        before, skill = dict(world.items), TrimArmor()
        drive(skill, ex, state)
        self.assertTrue(memo_for(state).has_station("smithing_table"))
        self.assertEqual(skill.verify_done(state, before), "")

        ex_m, st_m = WorkstationWorld(blocks={(2, 64, 0): "smithing_table"}, items={"iron_chestplate": 1}).session()
        with self.assertRaises(Replan):
            drive(TrimArmor(), ex_m, st_m)

        ex_s, st_s = WorkstationWorld(items={"coast_armor_trim_smithing_template": 1, "iron_chestplate": 1, "emerald": 1}).session()
        with self.assertRaises(EnvBlocked) as caught:
            drive(TrimArmor(), ex_s, st_s)
        self.assertEqual(caught.exception.missing_predicate, "station_ready:smithing_table")

    def test_smith_netherite_hoe_happy_path_and_missing_materials(self):
        world = WorkstationWorld(blocks={(0, 64, 2): "crafting_table", (2, 64, 0): "smithing_table"}, items={"netherite_upgrade_smithing_template": 1, "netherite_ingot": 1, "diamond": 2, "stick": 2})
        ex, state = world.session()
        before, skill = dict(world.items), SmithNetheriteHoe()
        drive(skill, ex, state)
        self.assertEqual(state.count("netherite_hoe"), 1)
        self.assertIn("craft", ex.acts)
        self.assertEqual(skill.verify_done(state, before), "")

        ex_m, st_m = WorkstationWorld(blocks={(2, 64, 0): "smithing_table"}, items={"diamond_hoe": 1}).session()
        with self.assertRaises(Replan):
            drive(SmithNetheriteHoe(), ex_m, st_m)

    def test_brew_potion_mundane_weakness_and_missing_ingredients(self):
        w_mun = WorkstationWorld(items={"brewing_stand": 1, "blaze_powder": 1, "potion": 1, "redstone": 1}, item_meta={"potion": {"potion": "water"}})
        ex1, st1 = w_mun.session()
        b1, s1 = dict(w_mun.items), BrewPotion(recipe="mundane")
        s1._sleep_fn = lambda _: None
        drive(s1, ex1, st1)
        self.assertIn("place", ex1.acts)
        self.assertEqual(s1.verify_done(st1, b1), "")

        w_wk = WorkstationWorld(blocks={(2, 64, 0): "brewing_stand"}, items={"blaze_powder": 1, "potion": 1, "fermented_spider_eye": 1, "gunpowder": 1}, item_meta={"potion": {"potion": "water"}})
        ex2, st2 = w_wk.session()
        b2, s2 = dict(w_wk.items), BrewPotion(recipe="weakness")
        s2._sleep_fn = lambda _: None
        drive(s2, ex2, st2)
        self.assertEqual(st2.count("splash_potion"), 1)
        self.assertEqual(s2.verify_done(st2, b2), "")

        with self.assertRaises(Replan):
            drive(BrewPotion(recipe="mundane"), *WorkstationWorld(blocks={(2, 64, 0): "brewing_stand"}, items={}).session())

    def test_cure_zombie_villager_happy_path_env_blocked_and_missing_items(self):
        world = WorkstationWorld(items={"splash_potion": 1, "golden_apple": 1}, item_meta={"splash_potion": {"potion": "weakness"}}, entities=[{"id": 202, "name": "zombie_villager", "type": "zombie_villager", "pos": [2.5, 64.0, 0.5]}])
        ex, state = world.session()
        before, skill = dict(world.items), CureZombieVillager()
        drive(skill, ex, state)
        self.assertLess(ex.acts.index("use_item"), ex.acts.index("interact"))
        self.assertEqual([c["target"] for m, c in world.calls if m == "use_item"][0], {"entity_id": 202})
        self.assertEqual([(c["entity_id"], c["item"]) for m, c in world.calls if m == "interact"][0], (202, "golden_apple"))
        self.assertEqual((state.count("golden_apple"), skill.verify_done(state, before)), (0, ""))

        with self.assertRaises(EnvBlocked) as caught:
            drive(CureZombieVillager(), *WorkstationWorld(items={"splash_potion": 1, "golden_apple": 1}, item_meta={"splash_potion": {"potion": "weakness"}}, entities=[]).session())
        self.assertEqual((caught.exception.missing_predicate, caught.exception.wake_on), ("zombie_villager", "night"))

        for items in ({"golden_apple": 1}, {"splash_potion": 1}):
            with self.subTest(items=items):
                with self.assertRaises(Replan):
                    drive(CureZombieVillager(), *WorkstationWorld(items=items, item_meta={"splash_potion": {"potion": "weakness"}}).session())
