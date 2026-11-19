"""Stage-2 Nether Core L3 skills, L2 helpers, and Furnace Lease offline tests."""
from __future__ import annotations

import math
import unittest
from copy import deepcopy
from unittest.mock import patch

import mcagent.skills  # noqa: F401
from mcagent.client import ApiError
from mcagent.loop.reserve import ReservationBook
from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.furnace_lease import (
    FurnaceLeaseBook, best_fuel_for, collect_leased, lease_book_for, load_and_lease, reset_all_furnace_leases,
)
from mcagent.skills.nether_core import (
    BASTION_LOOT, ChargeRespawnAnchor, DistractPiglin, HuntBlaze, HuntWitherSkull,
    LootBastion, MineAncientDebris, drop_item, ensure_gold_armor, loot_container_items,
)
from mcagent.skills.registry import REGISTRY, contract_errors
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches, voxel_cache_for
from mcagent.skills.world_memo import reset_all_world_memos


class NetherWorld(GridWorld):
    """Stateful GridWorld subclass simulating Nether blocks, entities, combat, and windows."""

    def __init__(self, pos=(0.5, 64.0, 0.5), *, blocks=None, items=None, entities=None,
                 armor=None, chests=None, furnaces=None, block_props=None, dimension="the_nether", hp=20.0):
        floor = {(x, 63, z): "netherrack" for x in range(-4, 5) for z in range(-4, 5)}
        if blocks:
            floor.update({tuple(k): v for k, v in blocks.items()})
        super().__init__(pos=pos, blocks=floor, items=items)
        self.st.update(dimension=dimension, health=hp)
        self.entities, self.armor, self.held = list(entities or []), dict(armor or {}), {}
        self.block_props = {tuple(k): dict(v) for k, v in (block_props or {}).items()}
        self.chests = {tuple(k): list(v) for k, v in (chests or {}).items()}
        self.furnaces = {tuple(k): v for k, v in (furnaces or {}).items()}
        self.inv_full_items: set[str] = set()
        self.kill_drops: dict[str, str | None] = {"blaze": "blaze_rod", "wither_skeleton": "wither_skeleton_skull"}
        self.barter_drop, self.respawn_wither, self._eid = False, False, 900

    def inventory(self, timeout: float = 10) -> dict:
        inv = {**super().inventory(timeout), "armor": deepcopy(self.armor)}
        return {**inv, "held": deepcopy(self.held)} if self.held else inv

    def get_block(self, pos) -> dict:
        cell = tuple(int(v) for v in pos)
        out = {"name": self.blocks.get(cell, "air")}
        return {**out, "properties": deepcopy(self.block_props[cell])} if cell in self.block_props else out

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None):
        allowed, (cx, cy, cz), hits = set(types or ()), self.st["pos"], []
        for (bx, by, bz), name in sorted(self.blocks.items()):
            if (not allowed or name in allowed) and (d := math.dist((cx, cy, cz), (bx + 0.5, by + 0.5, bz + 0.5))) <= radius + 0.5:
                e = {"name": name, "pos": [bx, by, bz], "dist": round(d, 2)}
                hits.append({**e, "properties": deepcopy(self.block_props[(bx, by, bz)])} if (bx, by, bz) in self.block_props else e)
        hits.sort(key=lambda b: b["dist"])
        return {"total": len(hits), "blocks": hits[:limit]}

    def get_entities(self, radius=32, types=None):
        allowed, (cx, cy, cz) = set(types or ()), self.st["pos"]
        return {"entities": [deepcopy(e) for e in self.entities
                             if (not allowed or e.get("name") in allowed or e.get("type") in allowed)
                             and math.dist((cx, cy, cz), e["pos"]) <= radius]}

    def equip(self, item, slot="hand", *, stop_on=None) -> dict:
        res = super().equip(item, slot, stop_on=stop_on)
        if slot == "hand":
            self.held = {"name": item, "count": self.items[item]}
        else:
            self.armor[slot] = {"name": item, "count": 1}
        return res

    def _consume(self, item: str, cnt: int = 1) -> None:
        if self.items.get(item, 0) < cnt:
            raise ApiError("missing_item", item)
        self.items[item] -= cnt

    def drop(self, item, count=1, *, stop_on=None) -> dict:
        self.calls.append(("drop", {"item": item, "count": count}))
        self._consume(item, count)
        if self.barter_drop and item == "gold_ingot":
            self._eid += 1
            self.entities.append({"id": self._eid, "type": "item", "name": "item", "item": "crying_obsidianx1", "pos": [2.5, 64.0, 0.5]})
        return {"dropped": item, "count": count}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        res = super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)
        px, py, pz = self.st["pos"]
        kept = []
        for e in self.entities:
            if e.get("type") == "item" and math.dist((px, py, pz), e["pos"]) <= 1.5:
                n = str(e.get("item", "")).rpartition("x")[0] or e.get("item")
                self.items[n] = self.items.get(n, 0) + 1
            else:
                kept.append(e)
        self.entities = kept
        return res

    def craft(self, item, count=1, *, stop_on=None) -> dict:
        self.calls.append(("craft", {"item": item, "count": count}))
        if item != "respawn_anchor":
            raise ApiError("unknown_recipe", item)
        self._consume("crying_obsidian", 6 * count); self._consume("glowstone", 3 * count)
        self.items["respawn_anchor"] = self.items.get("respawn_anchor", 0) + count
        return {"crafted": item, "gained": count}

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None) -> dict:
        self.calls.append(("interact", {"pos": pos, "entity_id": entity_id, "item": item}))
        if item is not None:
            self._consume(item, 1)
        if pos is not None and self.blocks.get(cell := tuple(int(v) for v in pos)) == "respawn_anchor" and item == "glowstone":
            props = self.block_props.setdefault(cell, {"charges": 0})
            props["charges"] = min(4, int(props.get("charges", 0)) + 1)
        return {"interacted": True, "item": item}

    def _resolve_kill(self, eid: int) -> None:
        ent = next((e for e in self.entities if e["id"] == eid), None)
        self.entities = [e for e in self.entities if e["id"] != eid]
        kind = ent.get("name", "") if ent else ""
        if (drop := self.kill_drops.get(kind)):
            self.items[drop] = self.items.get(drop, 0) + 1
        if kind == "wither_skeleton" and self.respawn_wither:
            self._eid += 1
            self.entities.append({"id": self._eid, "type": "hostile", "name": "wither_skeleton", "pos": [2.5, 64.0, 0.5], "health": 20})

    def attack(self, entity_id, timeout_ms=20000, *, max_hits=None, interval_ms=None, stop_on=None) -> dict:
        self.calls.append(("attack", {"entity_id": entity_id})); self._resolve_kill(entity_id)
        return {"killed": True, "entity_id": entity_id}

    def shoot(self, entity_id=None, pos=None, weapon="bow", charge_ms=1100, *, stop_on=None) -> dict:
        self.calls.append(("shoot", {"entity_id": entity_id, "weapon": weapon}))
        self._consume("arrow", 1)
        if entity_id is not None:
            self._resolve_kill(entity_id)
        return {"fired": True, "weapon": weapon}

    def window_view(self, target, *, stop_on=None) -> dict:
        cell = tuple(int(v) for v in target["pos"])
        if cell in self.chests:
            return {"window": "generic_9x3", "target": "chest", "sections": {"container": deepcopy(self.chests[cell])}}
        if cell in self.furnaces:
            return {"window": self.furnaces[cell][0], "target": self.furnaces[cell][0], "sections": deepcopy(self.furnaces[cell][1])}
        raise ApiError("no_window", str(cell))

    def _take_slot(self, rows: list[dict], section: str, item: str, cnt: int) -> dict:
        if item in self.inv_full_items:
            raise ApiError("inventory_full", "inventory full")
        row = next(r for r in rows if r["name"] == item); row["count"] -= cnt
        if row["count"] <= 0:
            rows.remove(row)
        self.items[item] = self.items.get(item, 0) + cnt
        return {"op": "take", "from": section, "took": {item: cnt}}

    def window_act(self, target, ops, *, stop_on=None) -> dict:
        self.calls.append(("window_act", {"target": deepcopy(target), "ops": deepcopy(ops)}))
        cell, done = tuple(int(v) for v in target["pos"]), []
        if cell in self.chests:
            return {"done": [self._take_slot(self.chests[cell], "container", op["item"], op["count"]) for op in ops]}
        _, secs = self.furnaces[cell]
        for op in ops:
            item, cnt = op["item"], op["count"]
            if op["op"] == "put":
                self._consume(item, cnt); secs[op["to"]].append({"name": item, "count": cnt, "slot": 0})
                done.append({"op": "put", "put": item, "count": cnt, "to": op["to"]})
            elif op["op"] == "take":
                done.append(self._take_slot(secs[op["from"]], op["from"], item, cnt))
        return {"done": done}


def _drive_val(gen, ex, state=None):
    box = []
    def _wrap():
        box.append((yield from gen))
    return drive(_wrap(), ex, state), box[0]


def _reset_caches():
    reset_all_furnace_leases(); reset_all_voxel_caches(); reset_all_world_memos(); _COOLDOWN.clear()


class NetherCoreTests(unittest.TestCase):
    def setUp(self):
        _reset_caches(); self.addCleanup(_reset_caches)

    def _env_blocked(self, flow, ex, st, pred, wake=None):
        with self.assertRaises(EnvBlocked) as err:
            drive(flow, ex, st)
        self.assertEqual(err.exception.missing_predicate, pred)
        if wake:
            self.assertEqual(err.exception.wake_on, wake)

    def test_registry_contracts_and_dimension_guards(self):
        l3 = (MineAncientDebris, HuntBlaze, LootBastion, ChargeRespawnAnchor, DistractPiglin, HuntWitherSkull)
        for cls in l3:
            self.assertIs(REGISTRY.get(cls.name), cls)
            self.assertEqual((cls.verified, contract_errors(cls)), ("offline", []))
        for fn in (drop_item, ensure_gold_armor, loot_container_items, load_and_lease, collect_leased):
            self.assertNotIn(fn.__name__, REGISTRY); self.assertNotIn(fn, REGISTRY.values())
        for dim, ok in (("overworld", False), ("the_end", False), ("the_nether", True), ("minecraft:the_nether", True)):
            st = NetherWorld(items={"diamond_pickaxe": 1, "gold_ingot": 1}, dimension=dim).session()[1]
            for cls in l3:
                self.assertEqual(cls.blocked(st) == "", ok, (cls.name, dim))
        unready = NetherWorld(items={"diamond_pickaxe": 1, "gold_ingot": 1}).session()[1]
        unready.st["ready"] = False
        for cls in l3:
            self.assertTrue(cls.blocked(unready))

    def test_l2_drop_item_ensure_gold_armor_and_loot_container_items(self):
        ex, st = NetherWorld(items={"gold_ingot": 3}).session()
        steps, res = _drive_val(drop_item(ex, st, "gold_ingot", 2), ex)
        self.assertEqual(([s["op"] for s in steps], res["dropped"], st.count("gold_ingot")), (["drop"], 2, 1))
        self.assertRaises(Replan, drive, drop_item(ex, st, "emerald", 1), ex)

        ex1, st1 = NetherWorld(armor={"head": {"name": "golden_helmet", "count": 1}}).session()
        self.assertEqual((_drive_val(ensure_gold_armor(ex1, st1), ex1), ex1.acts), (([], True), []))
        ex2, st2 = NetherWorld(items={"golden_boots": 1}).session()
        steps2, ok2 = _drive_val(ensure_gold_armor(ex2, st2), ex2)
        self.assertEqual(([s["op"] for s in steps2], ex2.acts, ok2), (["equip"], ["equip"], True))
        ex3, st3 = NetherWorld().session()
        self.assertEqual(_drive_val(ensure_gold_armor(ex3, st3), ex3), ([], False))

        wc = NetherWorld(blocks={(1, 64, 0): "chest"}, chests={(1, 64, 0): [
            {"name": "crying_obsidian", "count": 2}, {"name": "ancient_debris", "count": 1},
            {"name": "netherite_upgrade_smithing_template", "count": 1}]})
        wc.inv_full_items.add("crying_obsidian")
        exc, stc = wc.session()
        steps_c, taken = _drive_val(loot_container_items(exc, stc, (1, 64, 0), wanted=BASTION_LOOT), exc)
        self.assertEqual((taken, steps_c[-1]["error_code"]), (["netherite_upgrade_smithing_template", "ancient_debris"], "inventory_full"))

    def test_mine_ancient_debris_happy_descent_envblocked_and_pick_guard(self):
        ex, st = NetherWorld(blocks={(2, 64, 0): "ancient_debris"}, items={"diamond_pickaxe": 1}).session()
        sk = MineAncientDebris(count=1); drive(sk, ex, st)
        self.assertEqual(sk.verify_done(st), "")
        self.assertNotIn(("the_nether", 2, 64, 0), voxel_cache_for(st)._blocks)

        blocks = {(x, y, z): "netherrack" for x in range(-3, 4) for z in range(-3, 4) for y in (14, 15)}
        blocks[(1, 15, 0)] = "lava"
        blocks.update({(-1, y, 0): "netherrack" for y in (15, 16, 17, 18)})
        w2 = NetherWorld(pos=(0.5, 16.0, 0.5), blocks=blocks, items={"diamond_pickaxe": 1})
        ex2, st2 = w2.session()
        self._env_blocked(MineAncientDebris(count=1, max_steps=4), ex2, st2, "ancient_debris", "chunk_moved")
        self.assertEqual(st2.pos, [-0.5, 15.0, 0.5])
        self.assertFalse(any(c[1]["pos"][0] == 1 for c in w2.calls if c[0] == "dig"))
        self.assertTrue(MineAncientDebris.blocked(NetherWorld(items={"iron_pickaxe": 1}).session()[1]))

    def test_hunt_blaze_ranged_melee_envblocked_and_hp_guard(self):
        ex1, st1 = NetherWorld(items={"bow": 1, "arrow": 4}, entities=[
            {"id": 11, "type": "hostile", "name": "blaze", "pos": [6.5, 64.0, 0.5], "health": 20}]).session()
        sk1 = HuntBlaze(count=1)
        self.assertIn("shoot", [s["op"] for s in drive(sk1, ex1, st1)]); self.assertEqual(sk1.verify_done(st1), "")

        ex2, st2 = NetherWorld(items={"diamond_sword": 1}, entities=[
            {"id": 12, "type": "hostile", "name": "blaze", "pos": [2.5, 64.0, 0.5], "health": 20}]).session()
        sk2 = HuntBlaze(count=1)
        self.assertIn("attack", [s["op"] for s in drive(sk2, ex2, st2)]); self.assertEqual(sk2.verify_done(st2), "")

        ex3, st3 = NetherWorld().session()
        self._env_blocked(HuntBlaze(count=1), ex3, st3, "blaze", "chunk_moved")
        ex4, st4 = NetherWorld(hp=9.0).session()
        self.assertTrue(HuntBlaze.blocked(st4)); self.assertRaises(Replan, drive, HuntBlaze(count=1), ex4, st4)

    def test_loot_bastion_happy_brute_abort_and_envblocked(self):
        w = NetherWorld(blocks={(2, 64, 0): "chest", (2, 64, 1): "gilded_blackstone"}, items={"golden_helmet": 1},
                        chests={(2, 64, 0): [{"name": "crying_obsidian", "count": 1}, {"name": "netherite_upgrade_smithing_template", "count": 1}]})
        ex, st = w.session(); sk = LootBastion(); drive(sk, ex, st)
        self.assertEqual((sk.verify_done(st), [c[1]["ops"][0]["item"] for c in w.calls if c[0] == "window_act"]),
                         ("", ["netherite_upgrade_smithing_template", "crying_obsidian"]))

        w_b = NetherWorld(blocks={(2, 64, 0): "chest"}, items={"golden_helmet": 1},
                          entities=[{"id": 20, "type": "hostile", "name": "piglin_brute", "pos": [4.5, 64.0, 0.5]}])
        ex_b, st_b = w_b.session()
        self.assertRaises(Replan, drive, LootBastion(), ex_b, st_b)
        self.assertEqual([c[0] for c in w_b.calls if c[0] == "window_act"], [])
        ex_e, st_e = NetherWorld(items={"golden_helmet": 1}).session()
        self._env_blocked(LootBastion(), ex_e, st_e, "bastion_chest")

    def test_charge_respawn_anchor_craft_reuse_and_explosion_guard(self):
        ex, st = NetherWorld(blocks={(0, 64, 1): "crafting_table"}, items={"crying_obsidian": 6, "glowstone": 7}).session()
        sk = ChargeRespawnAnchor(charges=4)
        self.assertEqual(([s["op"] for s in drive(sk, ex, st)].count("interact"), sk.verify_done(st)), (4, ""))

        ex_r, st_r = NetherWorld(blocks={(1, 64, 0): "respawn_anchor"}, items={"glowstone": 2},
                                 block_props={(1, 64, 0): {"charges": 2}}).session()
        sk_r = ChargeRespawnAnchor(charges=4)
        self.assertEqual(([s["op"] for s in drive(sk_r, ex_r, st_r)].count("interact"), sk_r.verify_done(st_r)), (2, ""))

        for dim in ("overworld", "the_end"):
            self.assertTrue(ChargeRespawnAnchor.blocked(NetherWorld(dimension=dim).session()[1]))
        w_d = NetherWorld(blocks={(1, 64, 0): "respawn_anchor"}, items={"glowstone": 2}, block_props={(1, 64, 0): {"charges": 0}})
        ex_d, st_d = w_d.session(); orig = w_d.interact
        w_d.interact = lambda *a, **kw: (orig(*a, **kw), w_d.st.update(dimension="overworld"))[0]
        self.assertRaises(Replan, drive, ChargeRespawnAnchor(charges=4), ex_d, st_d)
        self.assertEqual(sum(1 for c in w_d.calls if c[0] == "interact"), 1)

        w_dry = NetherWorld(blocks={(1, 64, 0): "respawn_anchor"}, items={"glowstone": 1}, block_props={(1, 64, 0): {"charges": 1}})
        ex_g, st_g = w_dry.session()
        self.assertRaises(Replan, drive, ChargeRespawnAnchor(charges=4), ex_g, st_g)
        self.assertTrue(all(c[1]["item"] == "glowstone" for c in w_dry.calls if c[0] == "interact"))

    def test_distract_piglin_distract_barter_and_ignores_brute(self):
        piglin = {"id": 31, "type": "hostile", "name": "piglin", "pos": [5.5, 64.0, 0.5]}
        ex1, st1 = NetherWorld(items={"gold_ingot": 2}, entities=[piglin]).session()
        sk1 = DistractPiglin(mode="distract"); drive(sk1, ex1, st1)
        self.assertEqual((sk1.verify_done(st1), st1.count("gold_ingot")), ("", 1))

        w2 = NetherWorld(items={"gold_ingot": 2}, entities=[deepcopy(piglin)]); w2.barter_drop = True
        ex2, st2 = w2.session(); sk2 = DistractPiglin(mode="barter_crying_obsidian"); drive(sk2, ex2, st2)
        self.assertEqual((sk2.verify_done(st2), st2.count("crying_obsidian")), ("", 1))

        ex3, st3 = NetherWorld(items={"gold_ingot": 1}, entities=[
            {"id": 32, "type": "hostile", "name": "piglin_brute", "pos": [4.5, 64.0, 0.5]}]).session()
        self._env_blocked(DistractPiglin(mode="distract"), ex3, st3, "piglin")

    def test_hunt_wither_skull_happy_envblocked_hp_and_max_kills(self):
        ws = {"id": 41, "type": "hostile", "name": "wither_skeleton", "pos": [2.5, 64.0, 0.5], "health": 20}
        ex1, st1 = NetherWorld(items={"diamond_sword": 1}, entities=[ws]).session()
        sk1 = HuntWitherSkull(max_kills=3); drive(sk1, ex1, st1)
        self.assertEqual((sk1.verify_done(st1), st1.count("wither_skeleton_skull")), ("", 1))

        ex2, st2 = NetherWorld().session()
        self._env_blocked(HuntWitherSkull(), ex2, st2, "wither_skeleton", "chunk_moved")
        ex3, st3 = NetherWorld(hp=9.0).session()
        self.assertTrue(HuntWitherSkull.blocked(st3)); self.assertRaises(Replan, drive, HuntWitherSkull(), ex3, st3)

        w4 = NetherWorld(items={"diamond_sword": 1}, entities=[deepcopy(ws)])
        w4.kill_drops["wither_skeleton"], w4.respawn_wither = None, True
        ex4, st4 = w4.session()
        self.assertRaises(Replan, drive, HuntWitherSkull(max_kills=2), ex4, st4)
        self.assertEqual(sum(1 for c in w4.calls if c[0] == "attack"), 2)

    def test_furnace_lease_fuel_book_reserve_and_nonblocking_cycle(self):
        st_leaf = NetherWorld(items={"leaf_litter": 8}).session()[1]
        self.assertEqual((best_fuel_for(st_leaf, 2), best_fuel_for(st_leaf, 3)), (("leaf_litter", 4), ("leaf_litter", 6)))
        st_lava = NetherWorld(items={"lava_bucket": 1}).session()[1]
        self.assertEqual((best_fuel_for(st_lava, 10), best_fuel_for(st_lava, 10, allow_lava_bucket=False)), (("lava_bucket", 1), None))
        for fuel in ("coal", "oak_planks", "stick"):
            self.assertEqual(best_fuel_for(NetherWorld(items={fuel: 8}).session()[1], 2)[0], fuel)

        now = [100.0]
        book, st_r, rbook = FurnaceLeaseBook(clock=lambda: now[0]), NetherWorld(items={"raw_iron": 4, "coal": 2}).session()[1], ReservationBook()
        self.assertTrue(rbook.reserve_plan(st_r, "smelt_a", [("raw_iron", 4, "consume"), ("coal", 1, "consume")]))
        l1 = book.register_lease((1, 64, 0), station_kind="blast_furnace", input_item="raw_iron", count=4,
                                 consumer="smelt_a", reservation_book=rbook, state=st_r, fuel_item="coal", fuel_count=1)
        l2 = book.register_lease((2, 64, 0), station_kind="smoker", input_item="beef", count=2)
        l3 = book.register_lease((3, 64, 0), station_kind="furnace", input_item="raw_gold", count=2)
        self.assertEqual((l1.ready_at - l1.started_at, l2.ready_at - l2.started_at, l3.ready_at - l3.started_at), (20.0, 10.0, 20.0))
        self.assertTrue(all(e.mode == "in_transit" for e in rbook.entries))
        now[0] = 110.0
        self.assertEqual([l.pos for l in book.ready_leases()], [(2, 64, 0)])
        self.assertIsNotNone(book.complete_lease((2, 64, 0))); self.assertEqual(len(book.active_leases()), 2)

        st_b = NetherWorld().session()[1]; lb = lease_book_for(st_b)
        lb.register_lease((0, 64, 0), input_item="raw_iron", count=1)
        reset_all_furnace_leases(); self.assertIsNot(lease_book_for(st_b), lb)

        w = NetherWorld(blocks={(1, 64, 0): "furnace", (2, 64, 0): "blast_furnace"},
                        items={"raw_iron": 2, "raw_gold": 1, "lava_bucket": 1, "coal": 1},
                        furnaces={(1, 64, 0): ("furnace", {"input": [], "fuel": [], "output": []}),
                                  (2, 64, 0): ("blast_furnace", {"input": [], "fuel": [], "output": []})})
        ex, st = w.session()
        with patch("mcagent.skills.furnace_lease.time.sleep") as mock_sleep:
            _, lease1 = _drive_val(load_and_lease(ex, st, (1, 64, 0), input_item="raw_iron", count=2, fuel_item="lava_bucket"), ex)
            _drive_val(load_and_lease(ex, st, (2, 64, 0), input_item="raw_gold", count=1, fuel_item="coal", station_kind="blast_furnace"), ex)
            self.assertEqual((len(lease_book_for(st).active_leases()), lease1.returns_empty_bucket), (2, True))
            w.furnaces[(1, 64, 0)][1].update(input=[], fuel=[], output=[{"name": "iron_ingot", "count": 2}])
            _, got1 = _drive_val(collect_leased(ex, st, lease1), ex)
            self.assertIsNotNone(lease_book_for(st).get_lease((1, 64, 0), dimension="the_nether"))
            w.furnaces[(1, 64, 0)][1]["fuel"] = [{"name": "bucket", "count": 1}]
            _, got2 = _drive_val(collect_leased(ex, st, lease1), ex)
            mock_sleep.assert_not_called()
        self.assertEqual((got1, got2, st.count("iron_ingot"), st.count("bucket")), (2, 0, 2, 1))
        self.assertIsNone(lease_book_for(st).get_lease((1, 64, 0), dimension="the_nether"))
        self.assertIsNotNone(lease_book_for(st).get_lease((2, 64, 0), dimension="the_nether"))
