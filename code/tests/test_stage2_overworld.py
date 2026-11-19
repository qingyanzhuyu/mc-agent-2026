"""Stage 2 Batch 2C Overworld T2 Quick Wins 离线单测。"""
from __future__ import annotations

import math
import unittest

from mcagent.client import ApiError
from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills import REGISTRY, contract_errors
import mcagent.skills.t2_overworld as t2
from mcagent.skills.t2_overworld import (
    BiomeQuickAction, CatchAquaticMob, HarvestHoneyAndWax, MakeSignGlow,
    SniperDuel, SummonIronGolem, TameAnimal,
)
from mcagent.skills.testing import GridWorld, drive


def _flat_floor(x0=-5, x1=5, z0=-5, z1=5, y=63, block="stone"):
    return {(x, y, z): block for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)}


class OverworldTestWorld(GridWorld):
    def __init__(self, pos=(0.5, 64.0, 0.5), blocks=None, items=None, entities=None):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st["dimension"] = "overworld"
        self.entities: list[dict] = list(entities or [])
        self.armor: dict[str, dict] = {}
        self.held: dict | None = None
        self.adv_completed: set[str] = set()
        self.fail_move_to: Exception | None = None
        self.instant_golem: bool = False
        self.tame_after_feeds: int = 1
        self._feeds: dict[int, int] = {}

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

    def craft(self, item, count=1, *, stop_on=None):
        self.calls.append(("craft", {"item": item, "count": count}))
        if item == "iron_block":
            need = 9 * count
            if self.items.get("iron_ingot", 0) < need:
                raise ApiError("missing_item", "缺少铁锭")
            self.items["iron_ingot"] -= need
            self.items["iron_block"] = self.items.get("iron_block", 0) + count
        elif item == "oak_sign":
            if self.items.get("oak_planks", 0) < 6 * count or self.items.get("stick", 0) < count:
                raise ApiError("missing_item", "缺少木板或木棍")
            self.items["oak_planks"] -= 6 * count
            self.items["stick"] -= count
            self.items["oak_sign"] = self.items.get("oak_sign", 0) + 3 * count
        return {"crafted": item, "count": count}

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
            self.assertEqual(cls.verified, "offline", name)
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


if __name__ == "__main__":
    unittest.main()
