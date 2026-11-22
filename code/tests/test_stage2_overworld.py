"""Stage 2 Batch 2C Overworld T2 Quick Wins 离线单测。"""
from __future__ import annotations

from dataclasses import replace
import math
import unittest

from mcagent.client import ApiError
from mcagent.loop.goals import GOAL_MAP, rank_actionable_goals
from mcagent.loop.policy import RulePlanner
from mcagent.runtime import EnvBlocked, Replan, State
from mcagent.skills import REGISTRY, contract_errors
import mcagent.skills.t2_overworld as t2
from mcagent.skills.t2_overworld import (
    BiomeQuickAction, CatchAquaticMob, HarvestHoneyAndWax, MakeSignGlow,
    SniperDuel, SummonIronGolem, TameAnimal,
)
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


def _flat_floor(x0=-5, x1=5, z0=-5, z1=5, y=63, block="stone"):
    return {(x, y, z): block for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)}


class OverworldTestWorld(GridWorld):
    def __init__(self, pos=(0.5, 64.0, 0.5), blocks=None, items=None, entities=None, dimension="overworld"):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st["dimension"] = dimension
        self.entities: list[dict] = list(entities or [])
        self.armor: dict[str, dict] = {}
        self.held: dict | None = None
        self.adv_completed: set[str] = set()
        self.fail_move_to: Exception | None = None
        self.instant_golem: bool = False
        self.tame_after_feeds: int = 1
        self._feeds: dict[int, int] = {}
        self._furnaces: dict[tuple[int, int, int], dict[str, list[dict]]] = {}

    def inventory(self, timeout=10):
        inv = super().inventory(timeout)
        inv["armor"] = dict(self.armor)
        if self.held:
            inv["held"] = dict(self.held)
        return inv

    def equip(self, item, slot="hand", *, stop_on=None):
        res = super().equip(item, slot=slot, stop_on=stop_on)
        if slot in ("head", "torso", "legs", "feet"):
            self.armor[slot] = {"name": item, "count": 1, "durability": [65, 65]}
        elif slot == "hand":
            self.held = {"name": item, "count": 1}
        return res

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
        if self.fail_move_to is not None:
            self.calls.append(("move_to", {"x": x, "z": z, "y": y, "pos": pos}))
            raise self.fail_move_to
        return super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None):
        self.calls.append(("find_blocks", {"types": types, "radius": radius, "limit": limit}))
        wanted = {t.removeprefix("minecraft:") for t in (types or ())}
        px, py, pz = self.st["pos"]
        hits = []
        for pos, name in self.blocks.items():
            if wanted and name not in wanted:
                continue
            d = math.dist((px, py, pz), (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5))
            if d <= radius:
                hits.append({"name": name, "pos": list(pos), "dist": d})
        hits.sort(key=lambda h: h["dist"])
        return {"blocks": hits[:limit]}

    def get_entities(self, radius=32, types=None):
        self.calls.append(("get_entities", {"radius": radius, "types": types}))
        wanted = {t.removeprefix("minecraft:") for t in (types or ())}
        px, py, pz = self.st["pos"]
        out = []
        for ent in self.entities:
            if wanted and ent.get("name") not in wanted and ent.get("type") not in wanted:
                continue
            if math.dist((px, py, pz), ent["pos"]) <= radius:
                out.append(dict(ent))
        return {"entities": out}

    def advancements(self, status="completed", include_recipes=False):
        return {"ready": True, "advancements": [{"id": a, "status": "completed"} for a in sorted(self.adv_completed)]}

    def dig(self, pos, collect=True, *, stop_on=None):
        self.calls.append(("dig", {"pos": list(pos)}))
        dug = self.blocks.pop(tuple(pos), "air")
        if dug in ("copper_ore", "deepslate_copper_ore") and collect:
            self.items["raw_copper"] = self.items.get("raw_copper", 0) + 3
        elif dug != "air" and collect:
            self.items[dug] = self.items.get(dug, 0) + 1
        return {"dug": dug}

    def craft(self, item, count=1, *, stop_on=None):
        self.calls.append(("craft", {"item": item, "count": count}))

        def _take_any(names, qty):
            if sum(self.items.get(n, 0) for n in names) < qty:
                raise ApiError("missing_item", f"缺少 {names[0]}")
            rem = qty
            for n in names:
                got = min(self.items.get(n, 0), rem)
                if got:
                    self.items[n] -= got
                    rem -= got
                if not rem:
                    break

        logs = ("oak_log", "birch_log", "spruce_log", "jungle_log", "acacia_log", "dark_oak_log", "cherry_log", "mangrove_log", "pale_oak_log")
        planks = ("oak_planks", "birch_planks", "spruce_planks", "jungle_planks", "acacia_planks", "dark_oak_planks", "cherry_planks", "mangrove_planks", "pale_oak_planks")
        if item == "iron_block":
            _take_any(("iron_ingot",), 9 * count)
            self.items["iron_block"] = self.items.get("iron_block", 0) + count
        elif item == "copper_block":
            _take_any(("copper_ingot",), 9 * count)
            self.items["copper_block"] = self.items.get("copper_block", 0) + count
        elif item == "oak_sign":
            _take_any(("oak_planks",), 6 * count)
            _take_any(("stick",), count)
            self.items["oak_sign"] = self.items.get("oak_sign", 0) + 3 * count
        elif item.endswith("_planks"):
            log = item.removesuffix("_planks") + "_log"
            _take_any((log,), count)
            self.items[item] = self.items.get(item, 0) + 4 * count
        elif item == "stick":
            _take_any(planks, 2 * count)
            self.items["stick"] = self.items.get("stick", 0) + 4 * count
        elif item == "crafting_table":
            _take_any(planks, 4 * count)
            self.items["crafting_table"] = self.items.get("crafting_table", 0) + count
        elif item == "campfire":
            _take_any(logs, 3 * count)
            _take_any(("stick",), 3 * count)
            _take_any(("coal", "charcoal"), count)
            self.items["campfire"] = self.items.get("campfire", 0) + count
        elif item == "glass_bottle":
            _take_any(("glass",), 3 * count)
            self.items["glass_bottle"] = self.items.get("glass_bottle", 0) + 3 * count
        elif item == "wooden_axe":
            _take_any(planks, 3 * count)
            _take_any(("stick",), 2 * count)
            self.items["wooden_axe"] = self.items.get("wooden_axe", 0) + count
        elif item == "brush":
            _take_any(("copper_ingot",), count)
            _take_any(("feather",), count)
            _take_any(("stick",), count)
            self.items["brush"] = self.items.get("brush", 0) + count
        elif item == "leather_boots":
            _take_any(("leather",), 4 * count)
            self.items["leather_boots"] = self.items.get("leather_boots", 0) + count
        elif item == "dried_ghast":
            _take_any(("soul_sand",), count)
            _take_any(("ghast_tear",), 8 * count)
            self.items["dried_ghast"] = self.items.get("dried_ghast", 0) + count
        elif item == "tripwire_hook":
            _take_any(("iron_ingot",), count)
            _take_any(("stick",), count)
            _take_any(planks, count)
            self.items["tripwire_hook"] = self.items.get("tripwire_hook", 0) + 2 * count
        elif item == "crossbow":
            _take_any(("stick",), 3 * count)
            _take_any(("iron_ingot",), count)
            _take_any(("string",), 2 * count)
            _take_any(("tripwire_hook",), count)
            self.items["crossbow"] = self.items.get("crossbow", 0) + count
        return {"crafted": item, "count": count}

    def _furnace_at(self, pos):
        key = tuple(pos)
        return self._furnaces.setdefault(key, {"input": [], "fuel": [], "output": []})

    def window_view(self, target, *, stop_on=None):
        pos = tuple(target["pos"])
        self.calls.append(("window_view", {"pos": list(pos)}))
        kind = self.blocks.get(pos, "furnace")
        sec = self._furnace_at(pos)
        return {
            "window": kind,
            "target": kind,
            "sections": {k: [dict(x) for x in v] for k, v in sec.items()},
        }

    def window_act(self, target, ops, *, stop_on=None):
        pos = tuple(target["pos"])
        self.calls.append(("window_act", {"pos": list(pos), "ops": ops}))
        sec = self._furnace_at(pos)
        done = []
        for op in ops:
            if op["op"] == "put":
                item, cnt, to = op["item"], int(op["count"]), op["to"]
                if self.items.get(item, 0) < cnt:
                    raise ApiError("missing_item", f"缺少 {item}")
                self.items[item] -= cnt
                sec[to].append({"name": item, "count": cnt, "slot": 0})
                done.append({"op": "put", "item": item, "count": cnt, "to": to})
            elif op["op"] == "take":
                frm = op["from"]
                took = {}
                for entry in list(sec[frm]):
                    name, cnt = entry["name"], int(entry["count"])
                    self.items[name] = self.items.get(name, 0) + cnt
                    took[name] = took.get(name, 0) + cnt
                sec[frm].clear()
                done.append({"op": "take", "from": frm, "took": took})
        if sec["input"] and sec["fuel"]:
            for entry in list(sec["input"]):
                if entry["name"] == "raw_copper":
                    sec["output"].append({"name": "copper_ingot", "count": entry["count"], "slot": 2})
            sec["input"].clear()
        return {"ok": True, "done": done}

    def place(self, item, pos=None, *, stop_on=None):
        res = super().place(item, pos=pos, stop_on=stop_on)
        if self.instant_golem and item == "carved_pumpkin":
            hx, hy, hz = pos
            for x in range(hx - 1, hx + 2):
                for z in range(hz - 1, hz + 2):
                    for y in (hy, hy - 1, hy - 2):
                        if self.blocks.get((x, y, z)) in ("iron_block", "carved_pumpkin"):
                            self.blocks.pop((x, y, z), None)
        return res

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None):
        self.calls.append(("interact", {"pos": pos, "entity_id": entity_id, "item": item}))
        if item:
            self._need(item)
        if item == "glow_ink_sac":
            self.items["glow_ink_sac"] -= 1
            return {"changed": True}
        if item == "water_bucket" and entity_id is not None:
            ent = next((e for e in self.entities if e["id"] == entity_id), None)
            if ent:
                self.entities.remove(ent)
                self.items["water_bucket"] -= 1
                b = f"{ent['name']}_bucket"
                self.items[b] = self.items.get(b, 0) + 1
                return {"changed": True}
        if item in ("bone", "cod", "salmon") and entity_id is not None:
            ent = next((e for e in self.entities if e["id"] == entity_id), None)
            if ent:
                self.items[item] -= 1
                self._feeds[entity_id] = self._feeds.get(entity_id, 0) + 1
                if self._feeds[entity_id] >= self.tame_after_feeds:
                    ent["tamed"] = True
                return {"changed": True, "tamed": bool(ent.get("tamed"))}
        if item == "bucket" and pos is not None:
            if self.blocks.get(tuple(pos)) == "water":
                self.items["bucket"] -= 1
                self.items["water_bucket"] = self.items.get("water_bucket", 0) + 1
                return {"changed": True}
        if item == "water_bucket" and pos is not None:
            self.items["water_bucket"] -= 1
            self.items["bucket"] = self.items.get("bucket", 0) + 1
            self.blocks[(pos[0], pos[1] + 1, pos[2])] = "water"
            return {"changed": True}
        if item == "shears" and pos is not None:
            if self.blocks.get(tuple(pos)) in ("bee_nest", "beehive"):
                self.items["honeycomb"] = self.items.get("honeycomb", 0) + 3
                return {"changed": True}
        if item == "glass_bottle" and pos is not None:
            self.items["glass_bottle"] -= 1
            self.items["honey_bottle"] = self.items.get("honey_bottle", 0) + 1
            return {"changed": True}
        if item == "honeycomb" and pos is not None:
            key = tuple(pos)
            old = self.blocks.get(key, "air")
            if not old.startswith("waxed_"):
                self.items["honeycomb"] -= 1
                self.blocks[key] = f"waxed_{old}"
                return {"changed": True}
        if item and item.endswith("_axe") and pos is not None:
            key = tuple(pos)
            old = self.blocks.get(key, "air")
            if old.startswith("waxed_"):
                self.blocks[key] = old.removeprefix("waxed_")
                return {"changed": True}
        if item == "brush" and entity_id is not None:
            self.items["armadillo_scute"] = self.items.get("armadillo_scute", 0) + 1
            return {"changed": True}
        return {"changed": False}

    def shoot(self, entity_id=None, pos=None, weapon="bow", charge_ms=1100, *, stop_on=None):
        self.calls.append(("shoot", {"entity_id": entity_id, "pos": pos, "weapon": weapon, "charge_ms": charge_ms}))
        if self.items.get("arrow", 0) > 0:
            self.items["arrow"] -= 1
        return {"fired": True, "weapon": weapon}


class RegistryContractTests(unittest.TestCase):
    def test_all_seven_skills_registered_with_clean_contracts(self):
        expected = {
            "summon_iron_golem": SummonIronGolem,
            "sniper_duel": SniperDuel,
            "make_sign_glow": MakeSignGlow,
            "catch_aquatic_mob": CatchAquaticMob,
            "tame_animal": TameAnimal,
            "harvest_honey_wax": HarvestHoneyAndWax,
            "biome_quick_action": BiomeQuickAction,
        }
        for name, cls in expected.items():
            self.assertIs(REGISTRY.get(name), cls)
            self.assertEqual(contract_errors(cls), [], name)
            self.assertEqual(cls.verified, "live", name)
        self.assertIsNotNone(t2)


class SummonIronGolemTests(unittest.TestCase):
    def _assert_t_shape(self, placed_iron, pumpkin_pos):
        self.assertEqual(len(placed_iron), 4)
        base = min(placed_iron, key=lambda p: p[1])
        bx, by, bz = base
        self.assertIn((bx, by + 1, bz), placed_iron)
        self.assertEqual(pumpkin_pos, (bx, by + 2, bz))
        arms = sorted(p for p in placed_iron if p not in (base, (bx, by + 1, bz)))
        self.assertEqual(len(arms), 2)
        self.assertTrue(
            arms == [(bx - 1, by + 1, bz), (bx + 1, by + 1, bz)]
            or arms == [(bx, by + 1, bz - 1), (bx, by + 1, bz + 1)]
        )

    def test_happy_path_with_iron_blocks_and_t_shape(self):
        world = OverworldTestWorld(blocks=_flat_floor(), items={"iron_block": 4, "carved_pumpkin": 1})
        ex, state = world.session()
        before = {"carved_pumpkin": 1}
        skill = SummonIronGolem()
        drive(skill, ex, state)
        placed = [(c[1]["item"], tuple(c[1]["pos"])) for c in world.calls if c[0] == "place"]
        self.assertEqual([it for it, _ in placed], ["iron_block"] * 4 + ["carved_pumpkin"])
        self._assert_t_shape([p for _, p in placed[:4]], placed[4][1])
        self.assertEqual(skill.verify_done(state, before), "")

    def test_crafts_from_36_iron_ingots_and_handles_instant_spawn(self):
        blocks = _flat_floor()
        blocks[(2, 64, 0)] = "crafting_table"
        world = OverworldTestWorld(blocks=blocks, items={"iron_ingot": 36, "carved_pumpkin": 1})
        world.instant_golem = True
        ex, state = world.session()
        skill = SummonIronGolem()
        drive(skill, ex, state)
        self.assertEqual(sum(1 for c in world.calls if c[0] == "craft" and c[1]["item"] == "iron_block"), 4)
        self.assertEqual(state.count("carved_pumpkin"), 0)
        self.assertEqual(skill.verify_done(state, {"carved_pumpkin": 1}), "")

    def test_env_blocked_when_no_clear_t_shape(self):
        blocks = _flat_floor(-1, 1, -1, 1)
        for y in (64, 65):
            for x in range(-3, 4):
                for z in range(-3, 4):
                    if (x, z) != (0, 0):
                        blocks[(x, y, z)] = "stone"
        world = OverworldTestWorld(blocks=blocks, items={"iron_block": 4, "carved_pumpkin": 1})
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(SummonIronGolem(), ex, state)


class SniperDuelTests(unittest.TestCase):
    def test_happy_path_at_52_blocks(self):
        blocks = _flat_floor(-4, 4, -4, 4)
        skel = {"id": 7, "name": "skeleton", "type": "hostile", "pos": [52.5, 64.0, 0.5]}
        world = OverworldTestWorld(blocks=blocks, items={"bow": 1, "arrow": 8}, entities=[skel])
        ex, state = world.session()
        skill = SniperDuel()
        drive(skill, ex, state)
        shoots = [c for c in world.calls if c[0] == "shoot"]
        self.assertEqual(len(shoots), 1)
        self.assertEqual(shoots[0][1]["weapon"], "bow")
        self.assertEqual(skill.verify_done(state, {"arrow": 8}), "")

    def test_steps_back_from_46_blocks_to_ge_50(self):
        blocks = _flat_floor(-12, 4, -4, 4)
        skel = {"id": 8, "name": "skeleton", "type": "hostile", "pos": [46.5, 64.0, 0.5]}
        world = OverworldTestWorld(blocks=blocks, items={"bow": 1, "arrow": 8}, entities=[skel])
        ex, state = world.session()
        skill = SniperDuel()
        drive(skill, ex, state)
        self.assertLessEqual(world.st["pos"][0], -3.5)
        self.assertEqual(len([c for c in world.calls if c[0] == "shoot"]), 1)
        self.assertEqual(skill.verify_done(state, {"arrow": 8}), "")

    def test_blocked_shot_lane_replans_and_missing_skeleton_env_blocked(self):
        blocks = _flat_floor(-4, 4, -4, 4)
        blocks[(10, 65, 0)] = "stone"
        skel = {"id": 9, "name": "skeleton", "type": "hostile", "pos": [52.5, 64.0, 0.5]}
        world = OverworldTestWorld(blocks=blocks, items={"bow": 1, "arrow": 8}, entities=[skel])
        ex, state = world.session()
        with self.assertRaises(Replan):
            drive(SniperDuel(), ex, state)

        empty_world = OverworldTestWorld(blocks=_flat_floor(), items={"bow": 1, "arrow": 8}, entities=[])
        ex2, state2 = empty_world.session()
        with self.assertRaises(EnvBlocked):
            drive(SniperDuel(), ex2, state2)


class MakeSignGlowTests(unittest.TestCase):
    def test_places_sign_and_applies_glow_ink(self):
        world = OverworldTestWorld(blocks=_flat_floor(), items={"oak_sign": 1, "glow_ink_sac": 1})
        ex, state = world.session()
        skill = MakeSignGlow()
        drive(skill, ex, state)
        self.assertIn("place", [c[0] for c in world.calls])
        self.assertTrue(any(c[0] == "interact" and c[1]["item"] == "glow_ink_sac" for c in world.calls))
        self.assertEqual(skill.verify_done(state, {"glow_ink_sac": 1}), "")

    def test_reuses_existing_nearby_sign(self):
        blocks = _flat_floor()
        blocks[(2, 64, 0)] = "oak_sign"
        world = OverworldTestWorld(blocks=blocks, items={"oak_sign": 1, "glow_ink_sac": 1})
        ex, state = world.session()
        skill = MakeSignGlow()
        drive(skill, ex, state)
        self.assertFalse(any(c[0] == "place" for c in world.calls))
        self.assertTrue(any(c[0] == "interact" and c[1]["pos"] == [2, 64, 0] for c in world.calls))
        self.assertEqual(skill.verify_done(state, {"glow_ink_sac": 1}), "")

    def test_crafts_oak_sign_from_planks_and_stick(self):
        blocks = _flat_floor()
        blocks[(1, 64, 0)] = "crafting_table"
        world = OverworldTestWorld(blocks=blocks, items={"oak_planks": 6, "stick": 1, "glow_ink_sac": 1})
        ex, state = world.session()
        skill = MakeSignGlow()
        drive(skill, ex, state)
        self.assertTrue(any(c[0] == "craft" and c[1]["item"] == "oak_sign" for c in world.calls))
        self.assertEqual(skill.verify_done(state, {"glow_ink_sac": 1}), "")


class AquaticAndTameTests(unittest.TestCase):
    def test_catch_fish_and_axolotl_and_env_blocked(self):
        for target, mob, bucket in (("fish", "cod", "cod_bucket"), ("axolotl", "axolotl", "axolotl_bucket")):
            world = OverworldTestWorld(
                blocks=_flat_floor(),
                items={"water_bucket": 1},
                entities=[{"id": 11, "name": mob, "type": "animal", "pos": [2.5, 64.0, 0.5]}],
            )
            ex, state = world.session()
            skill = CatchAquaticMob(target=target)
            drive(skill, ex, state)
            self.assertEqual(state.count(bucket), 1)
            self.assertEqual(skill.verify_done(state, {bucket: 0}), "")

        empty = OverworldTestWorld(blocks=_flat_floor(), items={"water_bucket": 1})
        ex2, state2 = empty.session()
        with self.assertRaises(EnvBlocked):
            drive(CatchAquaticMob(target="fish"), ex2, state2)

    def test_tame_wolf_and_cat_and_env_blocked(self):
        wolf_w = OverworldTestWorld(
            blocks=_flat_floor(), items={"bone": 4},
            entities=[{"id": 21, "name": "wolf", "type": "animal", "pos": [2.5, 64.0, 0.5], "tamed": False}],
        )
        wolf_w.tame_after_feeds = 2
        ex, state = wolf_w.session()
        skill = TameAnimal()
        drive(skill, ex, state)
        self.assertEqual(sum(1 for c in wolf_w.calls if c[0] == "interact" and c[1]["item"] == "bone"), 2)
        self.assertEqual(skill.verify_done(state, {"bone": 4}), "")

        cat_w = OverworldTestWorld(
            blocks=_flat_floor(), items={"cod": 2},
            entities=[{"id": 22, "name": "cat", "type": "animal", "pos": [2.5, 64.0, 0.5], "tamed": False}],
        )
        ex_c, state_c = cat_w.session()
        skill_c = TameAnimal()
        drive(skill_c, ex_c, state_c)
        self.assertEqual(skill_c.verify_done(state_c, {"cod": 2}), "")

        empty = OverworldTestWorld(blocks=_flat_floor(), items={"bone": 2})
        ex_e, state_e = empty.session()
        with self.assertRaises(EnvBlocked):
            drive(TameAnimal(), ex_e, state_e)


class HarvestHoneyAndWaxTests(unittest.TestCase):
    def test_honey_places_campfire_beneath_nest_before_interacting(self):
        blocks = _flat_floor()
        blocks[(2, 66, 0)] = "bee_nest"
        world = OverworldTestWorld(blocks=blocks, items={"campfire": 1, "glass_bottle": 1})
        ex, state = world.session()
        skill = HarvestHoneyAndWax(mode="honey")
        drive(skill, ex, state)
        ops = [(c[0], c[1].get("item"), c[1].get("pos")) for c in world.calls if c[0] in ("place", "interact")]
        self.assertEqual(len(ops), 2)
        self.assertEqual(ops[0], ("place", "campfire", [2, 64, 0]))
        self.assertEqual(ops[1], ("interact", "glass_bottle", [2, 66, 0]))
        self.assertEqual(skill.verify_done(state), "")

    def test_honey_replans_without_campfire_spot_and_env_blocked_without_nest(self):
        blocks = _flat_floor()
        blocks[(2, 67, 0)] = "bee_nest"
        for y in (64, 65, 66):
            blocks[(2, y, 0)] = "oak_log"  # no AIR cell beneath nest
        world = OverworldTestWorld(blocks=blocks, items={"campfire": 1, "glass_bottle": 1})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "营火"):
            drive(HarvestHoneyAndWax(mode="honey"), ex, state)
        self.assertFalse(any(c[0] == "interact" for c in world.calls))

        no_nest = OverworldTestWorld(blocks=_flat_floor(), items={"campfire": 1, "glass_bottle": 1})
        ex2, state2 = no_nest.session()
        with self.assertRaises(EnvBlocked):
            drive(HarvestHoneyAndWax(mode="honey"), ex2, state2)

    def test_wax_on_existing_and_from_bag_and_wax_off(self):
        blocks = _flat_floor()
        blocks[(2, 64, 0)] = "copper_block"
        w1 = OverworldTestWorld(blocks=blocks, items={"honeycomb": 1})
        ex1, st1 = w1.session()
        s1 = HarvestHoneyAndWax(mode="wax_on")
        drive(s1, ex1, st1)
        self.assertEqual(w1.blocks[(2, 64, 0)], "waxed_copper_block")
        self.assertEqual(s1.verify_done(st1), "")

        w2 = OverworldTestWorld(blocks=_flat_floor(), items={"copper_block": 1, "honeycomb": 1})
        ex2, st2 = w2.session()
        s2 = HarvestHoneyAndWax(mode="wax_on")
        drive(s2, ex2, st2)
        self.assertTrue(any(c[0] == "place" and c[1]["item"] == "copper_block" for c in w2.calls))
        self.assertEqual(s2.verify_done(st2), "")

        blocks3 = _flat_floor()
        blocks3[(2, 64, 0)] = "waxed_copper_block"
        w3 = OverworldTestWorld(blocks=blocks3, items={"iron_axe": 1})
        ex3, st3 = w3.session()
        s3 = HarvestHoneyAndWax(mode="wax_off")
        drive(s3, ex3, st3)
        self.assertEqual(w3.blocks[(2, 64, 0)], "copper_block")
        self.assertEqual(s3.verify_done(st3), "")


class BiomeQuickActionTests(unittest.TestCase):
    def test_avoid_vibration_happy_path_and_cleanup_on_error_and_abort(self):
        blocks = _flat_floor()
        blocks[(3, 64, 2)] = "sculk_sensor"
        w = OverworldTestWorld(blocks=blocks, items={"stone_pickaxe": 1})
        ex, st = w.session()
        s = BiomeQuickAction(action="avoid_vibration")
        drive(s, ex, st)
        postures = [c[1]["sneak"] for c in w.calls if c[0] == "posture"]
        self.assertEqual(postures, [True, False])
        self.assertFalse(w.st["sneaking"])
        self.assertEqual(s.verify_done(st), "")

        # move_to raises -> finally MUST clear sneak
        w_err = OverworldTestWorld(blocks=dict(blocks), items={"stone_pickaxe": 1})
        w_err.fail_move_to = ApiError("stalled", "stuck")
        ex_e, st_e = w_err.session()
        with self.assertRaises(Replan):
            drive(BiomeQuickAction(action="avoid_vibration"), ex_e, st_e)
        self.assertFalse(w_err.st["sneaking"])
        self.assertEqual([c[1]["sneak"] for c in w_err.calls if c[0] == "posture"], [True, False])

        # abort() mid-sneak -> finally MUST clear sneak
        w_ab = OverworldTestWorld(blocks=dict(blocks), items={"stone_pickaxe": 1})
        ex_a, st_a = w_ab.session()
        s_ab = BiomeQuickAction(action="avoid_vibration")
        self.assertFalse(s_ab.step(ex_a, st_a))
        self.assertTrue(w_ab.st["sneaking"])
        s_ab.abort()
        self.assertFalse(w_ab.st["sneaking"])
        self.assertEqual([c[1]["sneak"] for c in w_ab.calls if c[0] == "posture"], [True, False])

    def test_brush_armadillo(self):
        w = OverworldTestWorld(
            blocks=_flat_floor(), items={"brush": 1},
            entities=[{"id": 31, "name": "armadillo", "type": "animal", "pos": [2.5, 64.0, 0.5]}],
        )
        ex, st = w.session()
        s = BiomeQuickAction(action="brush_armadillo")
        drive(s, ex, st)
        self.assertEqual(st.count("armadillo_scute"), 1)
        self.assertEqual(s.verify_done(st), "")

        with self.assertRaises(EnvBlocked):
            ex2, st2 = OverworldTestWorld(blocks=_flat_floor(), items={"brush": 1}).session()
            drive(BiomeQuickAction(action="brush_armadillo"), ex2, st2)

    def test_powder_snow_equips_leather_boots_before_stepping(self):
        blocks = _flat_floor()
        blocks[(2, 63, 0)] = "powder_snow"
        w = OverworldTestWorld(blocks=blocks, items={"leather_boots": 1})
        ex, st = w.session()
        s = BiomeQuickAction(action="powder_snow")
        drive(s, ex, st)
        acts = [c[0] for c in w.calls if c[0] in ("equip", "move_to")]
        self.assertEqual(acts[0], "equip")
        self.assertEqual(acts[-1], "move_to")
        self.assertEqual(w.armor.get("feet", {}).get("name"), "leather_boots")
        self.assertEqual(s.verify_done(st), "")

        with self.assertRaises(EnvBlocked):
            ex2, st2 = OverworldTestWorld(blocks=_flat_floor(), items={"leather_boots": 1}).session()
            drive(BiomeQuickAction(action="powder_snow"), ex2, st2)

    def test_dried_ghast(self):
        blocks = _flat_floor()
        blocks[(2, 63, 0)] = "water"
        w = OverworldTestWorld(blocks=blocks, items={"dried_ghast": 1})
        ex, st = w.session()
        s = BiomeQuickAction(action="dried_ghast")
        drive(s, ex, st)
        places = [c[1] for c in w.calls if c[0] == "place" and c[1]["item"] == "dried_ghast"]
        self.assertEqual(len(places), 1)
        self.assertEqual(s.verify_done(st), "")

        with self.assertRaises(EnvBlocked):
            ex2, st2 = OverworldTestWorld(blocks=_flat_floor(), items={"dried_ghast": 1}).session()
            drive(BiomeQuickAction(action="dried_ghast"), ex2, st2)

    def test_crossbow_pillager(self):
        blocks = _flat_floor(-4, 20, -4, 4)
        pillager = {"id": 41, "name": "pillager", "type": "hostile", "pos": [15.5, 64.0, 0.5]}
        w = OverworldTestWorld(blocks=blocks, items={"crossbow": 1, "arrow": 4}, entities=[pillager])
        ex, st = w.session()
        s = BiomeQuickAction(action="crossbow_pillager")
        drive(s, ex, st)
        shoots = [c[1] for c in w.calls if c[0] == "shoot"]
        self.assertEqual(len(shoots), 1)
        self.assertEqual(shoots[0]["weapon"], "crossbow")
        self.assertEqual(s.verify_done(st), "")

        with self.assertRaises(EnvBlocked):
            ex2, st2 = OverworldTestWorld(blocks=_flat_floor(), items={"crossbow": 1, "arrow": 4}).session()
            drive(BiomeQuickAction(action="crossbow_pillager"), ex2, st2)


class BiomePrepAndLandmarkTests(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()

    def tearDown(self):
        reset_all_world_memos()

    def test_catch_axolotl_fills_empty_bucket_from_water_and_navigates_lush_caves(self):
        blocks = _flat_floor(-4, 46, -4, 4)
        blocks[(42, 63, 0)] = "water"
        w = OverworldTestWorld(
            blocks=blocks,
            items={"bucket": 1},
            entities=[{"id": 51, "name": "axolotl", "type": "animal", "pos": [41.5, 64.0, 0.5]}],
        )
        ex, st = w.session()
        memo_for(st).record_biome("lush_caves", pos=[40, 64, 0], dimension="overworld")
        skill = CatchAquaticMob(target="axolotl", radius=24)
        drive(skill, ex, st)
        interacts = [c[1] for c in w.calls if c[0] == "interact"]
        self.assertEqual([it["item"] for it in interacts], ["bucket", "water_bucket"])
        self.assertEqual(st.count("axolotl_bucket"), 1)
        self.assertEqual(skill.verify_done(st, {"axolotl_bucket": 0}), "")

        no_water = OverworldTestWorld(
            blocks=_flat_floor(),
            items={"bucket": 1},
            entities=[{"id": 52, "name": "axolotl", "type": "animal", "pos": [2.5, 64.0, 0.5]}],
        )
        ex2, st2 = no_water.session()
        with self.assertRaises(EnvBlocked):
            drive(CatchAquaticMob(target="axolotl"), ex2, st2)

    def test_honey_crafts_campfire_and_glass_bottle_via_bee_nest_landmark(self):
        blocks = _flat_floor(-4, 46, -4, 4)
        blocks[(2, 64, 0)] = "crafting_table"
        blocks[(41, 64, 1)] = "crafting_table"
        blocks[(42, 66, 0)] = "bee_nest"
        w = OverworldTestWorld(blocks=blocks, items={"oak_log": 4, "coal": 1, "glass": 3})
        ex, st = w.session()
        memo_for(st).record_landmark("bee_nest", [40, 64, 0], dimension="overworld")
        skill = HarvestHoneyAndWax(mode="honey", radius=24)
        drive(skill, ex, st)
        crafted = [c[1]["item"] for c in w.calls if c[0] == "craft"]
        self.assertIn("glass_bottle", crafted)
        self.assertIn("campfire", crafted)
        ops = [(c[0], c[1].get("item"), c[1].get("pos")) for c in w.calls if c[0] in ("place", "interact")]
        self.assertEqual(ops[0], ("place", "campfire", [42, 64, 0]))
        self.assertEqual(ops[1], ("interact", "glass_bottle", [42, 66, 0]))
        self.assertEqual(st.count("honey_bottle"), 1)
        self.assertEqual(skill.verify_done(st), "")

    def test_wax_on_shears_honeycomb_with_campfire_via_landmark_and_records_waxed_copper(self):
        blocks = _flat_floor(-4, 46, -4, 4)
        blocks[(42, 66, 0)] = "bee_nest"
        w = OverworldTestWorld(blocks=blocks, items={"shears": 1, "campfire": 1, "copper_block": 1})
        ex, st = w.session()
        memo_for(st).record_landmark("bee_nest", [40, 64, 0], dimension="overworld")
        skill = HarvestHoneyAndWax(mode="wax_on", radius=24)
        drive(skill, ex, st)
        ops = [(c[0], c[1].get("item")) for c in w.calls if c[0] in ("place", "interact")]
        self.assertEqual(ops, [
            ("place", "campfire"),
            ("interact", "shears"),
            ("place", "copper_block"),
            ("interact", "honeycomb"),
        ])
        self.assertTrue(memo_for(st).has_landmark("waxed_copper", dimension="overworld"))
        self.assertEqual(skill.verify_done(st), "")

    def test_wax_on_mines_smelts_copper_ore_and_crafts_copper_block(self):
        blocks = _flat_floor()
        blocks[(2, 64, 0)] = "furnace"
        blocks[(2, 64, 1)] = "crafting_table"
        blocks[(3, 64, 0)] = "copper_ore"
        blocks[(3, 64, 1)] = "copper_ore"
        blocks[(3, 64, 2)] = "copper_ore"
        w = OverworldTestWorld(blocks=blocks, items={"honeycomb": 1, "stone_pickaxe": 1, "coal": 2})
        ex, st = w.session()
        skill = HarvestHoneyAndWax(mode="wax_on")
        drive(skill, ex, st)
        self.assertEqual(sum(1 for c in w.calls if c[0] == "dig"), 3)
        self.assertTrue(any(c[0] == "window_act" for c in w.calls))
        self.assertTrue(any(c[0] == "craft" and c[1]["item"] == "copper_block" for c in w.calls))
        self.assertTrue(any(v == "waxed_copper_block" for v in w.blocks.values()))
        self.assertEqual(skill.verify_done(st), "")

    def test_wax_off_crafts_wooden_axe_and_navigates_to_waxed_copper_landmark(self):
        blocks = _flat_floor(-4, 46, -4, 4)
        blocks[(2, 64, 0)] = "crafting_table"
        blocks[(42, 64, 0)] = "waxed_copper_block"
        w = OverworldTestWorld(blocks=blocks, items={"oak_log": 2})
        ex, st = w.session()
        memo_for(st).record_landmark("waxed_copper", [40, 64, 0], dimension="overworld")
        skill = HarvestHoneyAndWax(mode="wax_off", radius=24)
        drive(skill, ex, st)
        self.assertTrue(any(c[0] == "craft" and c[1]["item"] == "wooden_axe" for c in w.calls))
        self.assertEqual(w.blocks[(42, 64, 0)], "copper_block")
        self.assertEqual(skill.verify_done(st), "")

    def test_brush_armadillo_crafts_brush_from_copper_feather_stick(self):
        blocks = _flat_floor()
        blocks[(2, 64, 0)] = "crafting_table"
        w = OverworldTestWorld(
            blocks=blocks,
            items={"copper_ingot": 1, "feather": 1, "stick": 1},
            entities=[{"id": 61, "name": "armadillo", "type": "animal", "pos": [2.5, 64.0, 1.5]}],
        )
        ex, st = w.session()
        skill = BiomeQuickAction(action="brush_armadillo")
        drive(skill, ex, st)
        self.assertTrue(any(c[0] == "craft" and c[1]["item"] == "brush" for c in w.calls))
        self.assertEqual(st.count("armadillo_scute"), 1)
        self.assertEqual(skill.verify_done(st), "")

    def test_powder_snow_crafts_leather_boots_from_four_leather_and_equips(self):
        blocks = _flat_floor()
        blocks[(2, 64, 1)] = "crafting_table"
        blocks[(2, 63, 0)] = "powder_snow"
        w = OverworldTestWorld(blocks=blocks, items={"leather": 4})
        ex, st = w.session()
        skill = BiomeQuickAction(action="powder_snow")
        drive(skill, ex, st)
        ops = [c[0] for c in w.calls if c[0] in ("craft", "equip", "move_to")]
        self.assertEqual(ops[0], "craft")
        self.assertEqual(ops[1], "equip")
        self.assertEqual(ops[-1], "move_to")
        self.assertEqual(w.armor.get("feet", {}).get("name"), "leather_boots")
        self.assertEqual(skill.verify_done(st), "")

    def test_dried_ghast_nether_harvest_and_dimension_guard(self):
        blocks = _flat_floor(-4, 46, -4, 4, block="soul_sand")
        blocks[(42, 64, 0)] = "dried_ghast"
        w_nether = OverworldTestWorld(blocks=blocks, items={}, dimension="the_nether")
        ex, st = w_nether.session()
        memo_for(st).record_biome("soul_sand_valley", pos=[40, 64, 0], dimension="the_nether")
        skill = BiomeQuickAction(action="dried_ghast", radius=24)
        self.assertEqual(BiomeQuickAction.blocked(st), "")
        self.assertTrue(skill.can_start(st))
        drive(skill, ex, st)
        self.assertEqual(st.count("dried_ghast"), 1)
        self.assertTrue(any(c[0] == "dig" and c[1]["pos"] == [42, 64, 0] for c in w_nether.calls))
        self.assertEqual(skill.verify_done(st, {"dried_ghast": 0}), "")

        # Already holding dried_ghast in Nether -> EnvBlocked until Overworld
        with self.assertRaises(EnvBlocked) as ctx:
            drive(BiomeQuickAction(action="dried_ghast", radius=24), ex, st)
        self.assertEqual(ctx.exception.missing_predicate, "dimension:overworld")

    def test_dried_ghast_overworld_crafts_recipe_and_places_directly_into_water(self):
        blocks = _flat_floor()
        blocks[(2, 64, 0)] = "crafting_table"
        blocks[(2, 63, 1)] = "water"
        w = OverworldTestWorld(blocks=blocks, items={"soul_sand": 1, "ghast_tear": 8}, dimension="overworld")
        ex, st = w.session()
        skill = BiomeQuickAction(action="dried_ghast")
        drive(skill, ex, st)
        self.assertTrue(any(c[0] == "craft" and c[1]["item"] == "dried_ghast" for c in w.calls))
        places = [c[1] for c in w.calls if c[0] == "place" and c[1]["item"] == "dried_ghast"]
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["pos"], [2, 63, 1])
        self.assertEqual(skill.verify_done(st), "")

        # Overworld without item or recipe -> cannot start
        empty_ow = OverworldTestWorld(blocks=_flat_floor(), items={}, dimension="overworld")
        _, st_empty = empty_ow.session()
        self.assertFalse(BiomeQuickAction(action="dried_ghast").can_start(st_empty))

    def test_crossbow_pillager_crafts_crossbow_when_materials_present(self):
        blocks = _flat_floor(-4, 20, -4, 4)
        blocks[(2, 64, 1)] = "crafting_table"
        pillager = {"id": 71, "name": "pillager", "type": "hostile", "pos": [15.5, 64.0, 0.5]}
        w = OverworldTestWorld(
            blocks=blocks,
            items={"iron_ingot": 2, "string": 2, "stick": 4, "oak_planks": 1, "arrow": 4},
            entities=[pillager],
        )
        ex, st = w.session()
        skill = BiomeQuickAction(action="crossbow_pillager")
        drive(skill, ex, st)
        crafted = [c[1]["item"] for c in w.calls if c[0] == "craft"]
        self.assertEqual(crafted, ["tripwire_hook", "crossbow"])
        self.assertEqual(len([c for c in w.calls if c[0] == "shoot"]), 1)
        self.assertEqual(skill.verify_done(st), "")

    def test_sightline_repositions_around_wall_blocking_copper_block(self):
        blocks = _flat_floor()
        # Solid wall across x=1 blocks LOS from (0.5, 64, 0.5) to (2, 64, 0)
        for z in (-1, 0, 1):
            for y in (64, 65, 66):
                blocks[(1, y, z)] = "stone"
        blocks[(2, 64, 0)] = "copper_block"
        w = OverworldTestWorld(blocks=blocks, items={"honeycomb": 1})
        ex, st = w.session()
        skill = HarvestHoneyAndWax(mode="wax_on")
        drive(skill, ex, st)
        self.assertTrue(any(c[0] == "move_to" for c in w.calls))
        self.assertEqual(w.blocks[(2, 64, 0)], "waxed_copper_block")
        self.assertEqual(skill.verify_done(st), "")

    def test_nine_biome_goals_prereqs_fallbacks_and_planner_expansion(self):
        nine_ids = (
            "axolotl_in_a_bucket",
            "safely_harvest_honey",
            "wax_on",
            "wax_off",
            "avoid_vibration",
            "brush_armadillo",
            "walk_on_powder_snow_with_leather_boots",
            "place_dried_ghast_in_water",
            "whos_the_pillager_now",
        )
        for gid in nine_ids:
            self.assertEqual(GOAL_MAP[gid].stage, "graduated", gid)
            self.assertTrue(GOAL_MAP[gid].requires_landmarks, gid)

        # axolotl_in_a_bucket: with iron_ingot=3 (no bucket) -> expands to craft_iron_gear(bucket)
        st_axo = State()
        st_axo.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "dimension": "overworld", "pos": [0.5, 64.0, 0.5]})
        st_axo.set_inventory({"items": [{"name": "iron_ingot", "count": 3, "slot": 0}]})
        memo_for(st_axo).record_biome("lush_caves", pos=[30, 64, 0], dimension="overworld")
        rp_axo = RulePlanner(REGISTRY, ledger=(replace(GOAL_MAP["axolotl_in_a_bucket"], stage="graduated"),), include_stages=("graduated",), min_verified="offline")
        rp_axo.completed_advancements.add("minecraft:story/smelt_iron")
        sk_axo, _ = rp_axo.expand_objective_skill(st_axo)
        self.assertIsNotNone(sk_axo)
        self.assertEqual(sk_axo.name, "craft_iron_gear")
        self.assertEqual(sk_axo.args["item"], "bucket")

        # wax_on: with iron_ingot=2 (no honeycomb/shears) -> expands to craft_iron_gear(shears)
        st_wax = State()
        st_wax.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "dimension": "overworld", "pos": [0.5, 64.0, 0.5]})
        st_wax.set_inventory({"items": [{"name": "iron_ingot", "count": 2, "slot": 0}, {"name": "oak_log", "count": 3, "slot": 1}]})
        memo_for(st_wax).record_landmark("bee_nest", [30, 66, 0], dimension="overworld")
        rp_wax = RulePlanner(REGISTRY, ledger=(replace(GOAL_MAP["wax_on"], stage="graduated"),), include_stages=("graduated",), min_verified="offline")
        sk_wax, _ = rp_wax.expand_objective_skill(st_wax)
        self.assertIsNotNone(sk_wax)
        self.assertEqual(sk_wax.name, "craft_iron_gear")
        self.assertEqual(sk_wax.args["item"], "shears")

        # brush_armadillo & powder_snow: filtered out when lacking items, actionable once materials present
        st_empty = State()
        st_empty.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "dimension": "overworld", "pos": [0.5, 64.0, 0.5]})
        st_empty.set_inventory({"items": []})
        memo_for(st_empty).record_biome("savanna", pos=[20, 64, 0], dimension="overworld")
        memo_for(st_empty).record_landmark("powder_snow", [20, 64, 0], dimension="overworld")
        g_pair = (
            replace(GOAL_MAP["brush_armadillo"], stage="graduated"),
            replace(GOAL_MAP["walk_on_powder_snow_with_leather_boots"], stage="graduated"),
        )
        self.assertEqual(rank_actionable_goals(st_empty, g_pair), [])

        st_empty.set_inventory({"items": [
            {"name": "copper_ingot", "count": 1, "slot": 0},
            {"name": "feather", "count": 1, "slot": 1},
            {"name": "stick", "count": 1, "slot": 2},
            {"name": "leather", "count": 4, "slot": 3},
        ]})
        self.assertEqual(len(rank_actionable_goals(st_empty, g_pair)), 2)

    def test_smelt_copper_converts_logs_to_planks_before_loading_fuel(self):
        from unittest.mock import patch
        from mcagent.skills.t2_overworld import _smelt_copper_ingots

        blocks = {(x, 63, z): "grass_block" for x in range(-4, 5) for z in range(-4, 5)}
        blocks[(2, 64, 0)] = "furnace"
        w = OverworldTestWorld(blocks=blocks, items={"raw_copper": 3, "oak_log": 1})
        ex, st = w.session()
        with patch("mcagent.skills.t2_overworld.time.sleep", return_value=None):
            gen = _smelt_copper_ingots(ex, st, 3, 16)
            for _ in gen:
                pass
        crafted = [args["item"] for op, args in w.calls if op == "craft"]
        self.assertIn("oak_planks", crafted)
        fuel_puts = [
            op["item"]
            for op_name, args in w.calls
            if op_name == "window_act"
            for op in args["ops"]
            if op.get("op") == "put" and op.get("to") == "fuel"
        ]
        self.assertEqual(fuel_puts, ["oak_planks"])
        self.assertEqual(w.items.get("copper_ingot", 0), 3)
        self.assertEqual(w.items.get("oak_log", 0), 0)
        self.assertEqual(w.items.get("oak_planks", 0), 2)


if __name__ == "__main__":
    unittest.main()
