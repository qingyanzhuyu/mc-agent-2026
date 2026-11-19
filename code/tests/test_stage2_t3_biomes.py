"""Stage 2 Wave 3 群系 T3 技能离线单元测试。"""
from __future__ import annotations

import math
import unittest
from copy import deepcopy

from mcagent.runtime import EnvBlocked, Replan, State
from mcagent.skills.registry import REGISTRY, contract_errors
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.t3_biomes import (
    AllayDeliverItem,
    AssistAxolotlHunt,
    BoatWithGoat,
    CatchTadpole,
    KillNearSculkCatalyst,
    ManageWolfArmor,
    SilkTouchBeeNest,
    SpyglassObserve,
    lure_entity,
)
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches

_SKILLS = (
    KillNearSculkCatalyst,
    AssistAxolotlHunt,
    ManageWolfArmor,
    AllayDeliverItem,
    BoatWithGoat,
    SilkTouchBeeNest,
    CatchTadpole,
    SpyglassObserve,
)


def _flat_floor(x0=-10, x1=10, z0=-10, z1=10, y=63, block="stone") -> dict[tuple[int, int, int], str]:
    return {(x, y, z): block for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)}


def _run_gen(gen, ex):
    """驱动带 return 值的 L2 生成器，强制一步最多一个阻塞动作。"""
    for i in range(200):
        before = len(ex.acts)
        try:
            next(gen)
        except StopIteration as stop:
            fired = ex.acts[before:]
            if len(fired) > 1:
                raise AssertionError(f"第 {i + 1} 步发了 {len(fired)} 个阻塞动作 {fired}")
            return stop.value
        fired = ex.acts[before:]
        if len(fired) > 1:
            raise AssertionError(f"第 {i + 1} 步发了 {len(fired)} 个阻塞动作 {fired}")
    raise AssertionError("生成器超过 200 步未结束")


class BiomeWorld(ScriptedWorld):
    """支持群系 T3 全套交互、诱饵跟随、附魔元数据、船乘骑与望远镜的离线夹具。"""

    def __init__(
        self,
        pos=(0.5, 64.0, 0.5),
        *,
        blocks=None,
        items=None,
        item_meta=None,
        entities=None,
        hp=20.0,
        time_of_day=6000,
        is_raining=False,
        in_water=False,
    ):
        floor = _flat_floor()
        if blocks:
            floor.update({tuple(k): v for k, v in blocks.items()})
        super().__init__(pos=pos, blocks=floor, items=items)
        self.st.update(
            dimension="overworld",
            health=hp,
            time_of_day=time_of_day,
            is_raining=is_raining,
            in_water=in_water,
            in_vehicle=False,
            on_ground=not in_water,
        )
        self.entities: list[dict] = [deepcopy(e) for e in (entities or ())]
        self.item_meta: dict[str, dict] = {k: deepcopy(v) for k, v in (item_meta or {}).items()}
        self.held: dict | None = None
        self.on_move = None
        self.spawn_boat = True
        self.boat_mounts = True
        self.allay_accepts = True
        self.allay_deliver_after = 2
        self._pending_deliver: tuple[str, int] | None = None
        self._deliver_polls = 0
        self.dig_grants_nest = True
        self.tadpole_catch_ok = True
        self.attack_kills = True
        self.kill_damage = 0.0
        self.spyglass_ok = True
        self.next_eid = 900

    def inventory(self, timeout=10):
        if self._pending_deliver is not None:
            self._deliver_polls += 1
            if self.allay_deliver_after >= 0 and self._deliver_polls >= self.allay_deliver_after:
                item, cnt = self._pending_deliver
                self.items[item] = self.items.get(item, 0) + cnt
                self._pending_deliver = None
        rows = []
        for i, (n, c) in enumerate(self.items.items()):
            if c > 0:
                row = {"name": n, "count": c, "slot": i}
                if n in self.item_meta:
                    row.update(deepcopy(self.item_meta[n]))
                rows.append(row)
        inv: dict = {"items": rows}
        if self.held:
            inv["held"] = deepcopy(self.held)
        return inv

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None):
        self.calls.append(("find_blocks", {"types": types, "radius": radius, "limit": limit}))
        allowed = {t.removeprefix("minecraft:") for t in (types or ())}
        px, py, pz = self.st["pos"]
        hits = []
        for (bx, by, bz), name in sorted(self.blocks.items()):
            if allowed and name not in allowed:
                continue
            d = math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5))
            if d <= radius:
                hits.append({"name": name, "pos": [bx, by, bz], "dist": round(d, 2)})
        hits.sort(key=lambda b: b["dist"])
        return {"blocks": hits[:limit]}

    def get_entities(self, radius=32, types=None):
        self.calls.append(("get_entities", {"radius": radius, "types": types}))
        allowed = {t.removeprefix("minecraft:") for t in (types or ())}
        px, py, pz = self.st["pos"]
        return {
            "entities": [
                deepcopy(e)
                for e in self.entities
                if (not allowed or e.get("name") in allowed or e.get("type") in allowed)
                and math.dist((px, py, pz), e["pos"]) <= radius
            ]
        }

    def equip(self, item, slot="hand", *, stop_on=None):
        res = GridWorld.equip(self, item, slot=slot, stop_on=stop_on)
        if slot == "hand":
            self.held = {"name": item, "count": self.items[item], **deepcopy(self.item_meta.get(item, {}))}
        return res

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
        res = GridWorld.move_to(self, x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)
        if self.on_move:
            self.on_move(self, res["pos"])
        return res

    def dig(self, pos, collect=True, *, stop_on=None):
        self.calls.append(("dig", {"pos": list(pos)}))
        dug = self.blocks.pop(tuple(int(v) for v in pos), "air")
        if dug == "bee_nest" and collect and self.dig_grants_nest:
            self.items["bee_nest"] = self.items.get("bee_nest", 0) + 1
        elif dug not in ("air", "bee_nest") and collect:
            self.items[dug] = self.items.get(dug, 0) + 1
        return {"dug": dug}

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None):
        self.calls.append(("interact", {"pos": pos, "entity_id": entity_id, "item": item}))
        if item is not None:
            self._need(item)
        if pos is not None and item == "axolotl_bucket":
            self.items["axolotl_bucket"] -= 1
            self.items["water_bucket"] = self.items.get("water_bucket", 0) + 1
            self.next_eid += 1
            self.entities.append({"id": self.next_eid, "name": "axolotl", "type": "animal", "pos": [pos[0] + 0.5, float(pos[1]), pos[2] + 0.5]})
            return {"changed": True}
        if pos is not None and item and item.endswith(("_boat", "_raft")):
            self.items[item] -= 1
            if self.spawn_boat:
                self.next_eid += 1
                self.entities.append({"id": self.next_eid, "name": "boat", "type": "other", "pos": [pos[0] + 0.5, pos[1] + 1.0, pos[2] + 0.5]})
            return {"changed": True}
        ent = next((e for e in self.entities if e["id"] == entity_id), None)
        if ent is None:
            return {"changed": False}
        if ent["name"] == "wolf":
            if item == "shears" and ent.get("has_armor"):
                ent["has_armor"] = False
                self.items["wolf_armor"] = self.items.get("wolf_armor", 0) + 1
                return {"changed": True}
            if item == "armadillo_scute" and ent.get("has_armor"):
                self.items["armadillo_scute"] -= 1
                return {"changed": True}
            return {"changed": False}
        if ent["name"] == "allay" and item is not None:
            if not self.allay_accepts:
                return {"changed": False}
            self.items[item] -= 1
            ent["held"] = item
            return {"changed": True}
        if ent["name"] in ("boat", "raft") or ent["name"].endswith(("_boat", "_raft")):
            if self.boat_mounts:
                self.st["in_vehicle"] = True
                return {"changed": True, "mounted": True}
            return {"changed": False}
        if ent["name"] == "tadpole" and item == "water_bucket":
            if not self.tadpole_catch_ok:
                return {"changed": False}
            self.items["water_bucket"] -= 1
            self.items["tadpole_bucket"] = self.items.get("tadpole_bucket", 0) + 1
            self.entities = [e for e in self.entities if e["id"] != entity_id]
            return {"changed": True}
        return {"changed": False}

    def drop(self, item, count=1, *, stop_on=None):
        self.calls.append(("drop", {"item": item, "count": count}))
        self._need(item)
        self.items[item] -= count
        self._pending_deliver = (item, count)
        self._deliver_polls = 0
        return {"dropped": item, "count": count}

    def dismount(self, *, stop_on=None):
        self.calls.append(("dismount", {}))
        self.st["in_vehicle"] = False
        return {"dismounted": True, "pos": list(self.st["pos"])}

    def look(self, yaw=None, pitch=None, *, pos=None, entity_id=None):
        self.calls.append(("look", {"yaw": yaw, "pitch": pitch, "pos": pos, "entity_id": entity_id}))
        return {"ok": True}

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None):
        self.calls.append(("use_item", {"item": item, "target": target, "ms": ms}))
        self._need(item)
        if item == "spyglass":
            return {"ok": self.spyglass_ok, "item": item}
        self.items[item] -= 1
        return {"used": item, "target": target}

    def attack(self, entity_id, timeout_ms=20000, *, max_hits=None, interval_ms=None, stop_on=None):
        self.calls.append(("attack", {"entity_id": entity_id}))
        if self.kill_damage:
            self.st["health"] = max(0.0, float(self.st["health"]) - self.kill_damage)
        if self.attack_kills:
            self.entities = [e for e in self.entities if e["id"] != entity_id]
            return {"killed": True, "entity_id": entity_id}
        return {"killed": False, "entity_id": entity_id}

    def shoot(self, entity_id=None, pos=None, weapon="bow", charge_ms=1100, *, stop_on=None):
        self.calls.append(("shoot", {"entity_id": entity_id, "weapon": weapon}))
        self._need("arrow")
        self.items["arrow"] -= 1
        if entity_id is not None:
            self.entities = [e for e in self.entities if e["id"] != entity_id]
        return {"fired": True, "entity_id": entity_id}


class BiomeContractTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_contract_registry_verified_and_empty_bag_blocked(self):
        empty_ready = State()
        empty_ready.set_status({"ready": True, "alive": True, "health": 20, "pos": [0.5, 64.0, 0.5]})
        empty_ready.set_inventory({"items": []})
        for cls in _SKILLS:
            with self.subTest(skill=cls.name):
                self.assertEqual(contract_errors(cls), [])
                self.assertEqual(cls.verified, "offline")
                self.assertIs(REGISTRY[cls.name], cls)
                self.assertTrue(cls.blocked(State()))
                self.assertTrue(cls.blocked(empty_ready))
                self.assertTrue(cls().verify_done(empty_ready))
        self.assertNotIn("lure_entity", REGISTRY)

    def test_abort_cleans_up_active_posture(self):
        world = BiomeWorld(
            items={"spyglass": 1},
            entities=[{"id": 1, "name": "parrot", "type": "animal", "pos": [4.5, 65.0, 0.5]}],
        )
        world.st["sneaking"] = True
        ex, state = world.session()
        skill = SpyglassObserve(target="parrot")
        skill.step(ex, state)
        skill.abort()
        self.assertFalse(world.st["sneaking"])
        self.assertTrue(any(m == "posture" for m, _ in world.calls))


class LureEntityTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_lure_entity_approaches_and_guides_to_target(self):
        world = BiomeWorld(
            pos=(0.5, 64.0, 0.5),
            items={"wheat": 3},
            entities=[{"id": 10, "name": "goat", "type": "animal", "pos": [7.5, 64.0, 0.5]}],
        )

        def follow(w, new_pos):
            goat = w.entities[0]
            # 玩家移动后山羊向玩家拉近
            gx, gy, gz = goat["pos"]
            goat["pos"] = [gx + (new_pos[0] - gx) * 0.7, gy, gz + (new_pos[2] - gz) * 0.7]

        world.on_move = follow
        ex, state = world.session()
        ent = _run_gen(lure_entity(ex, state, ("goat",), "wheat", (-2.5, 64.0, 0.5)), ex)
        self.assertEqual(ent["id"], 10)
        self.assertLessEqual(math.dist(ent["pos"], (-2.5, 64.0, 0.5)), 4.5)
        self.assertEqual(ex.acts[0], "equip")
        self.assertIn("move_to", ex.acts)

    def test_lure_entity_missing_bait_or_entity(self):
        w1 = BiomeWorld(items={}, entities=[{"id": 1, "name": "goat", "type": "animal", "pos": [2.5, 64.0, 0.5]}])
        ex1, st1 = w1.session()
        with self.assertRaises(Replan):
            _run_gen(lure_entity(ex1, st1, ("goat",), "wheat", (0.5, 64.0, 0.5)), ex1)

        w2 = BiomeWorld(items={"wheat": 2}, entities=[])
        ex2, st2 = w2.session()
        with self.assertRaises(EnvBlocked):
            _run_gen(lure_entity(ex2, st2, ("goat",), "wheat", (0.5, 64.0, 0.5)), ex2)

    def test_lure_entity_lost_midway_raises_replan(self):
        world = BiomeWorld(
            items={"wheat": 2},
            entities=[{"id": 11, "name": "goat", "type": "animal", "pos": [2.5, 64.0, 0.5]}],
        )
        world.on_move = lambda w, _: w.entities.clear()
        ex, state = world.session()
        with self.assertRaises(Replan):
            _run_gen(lure_entity(ex, state, ("goat",), "wheat", (-6.5, 64.0, 0.5)), ex)


class KillNearSculkCatalystTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_melee_kill_near_catalyst_happy_path(self):
        world = BiomeWorld(
            blocks={(4, 64, 0): "sculk_catalyst"},
            items={"iron_sword": 1},
            entities=[{"id": 21, "name": "zombie", "type": "hostile", "pos": [3.5, 64.0, 0.5]}],
        )
        ex, state = world.session()
        skill = KillNearSculkCatalyst()
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertIn("attack", ex.acts)

    def test_ranged_kill_when_only_bow_and_arrow(self):
        world = BiomeWorld(
            blocks={(5, 64, 0): "sculk_catalyst"},
            items={"bow": 1, "arrow": 4},
            entities=[{"id": 22, "name": "zombie", "type": "hostile", "pos": [4.5, 64.0, 0.5]}],
        )
        ex, state = world.session()
        skill = KillNearSculkCatalyst()
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertIn("shoot", ex.acts)

    def test_missing_catalyst_or_mob_out_of_8_blocks_and_hp_stoploss(self):
        w_no_cat = BiomeWorld(items={"iron_sword": 1}, entities=[{"id": 1, "name": "zombie", "type": "hostile", "pos": [2.5, 64.0, 0.5]}])
        ex1, st1 = w_no_cat.session()
        with self.assertRaises(EnvBlocked):
            drive(KillNearSculkCatalyst(), ex1, st1)

        w_far = BiomeWorld(
            blocks={(0, 64, 4): "sculk_catalyst"},
            items={"iron_sword": 1},
            entities=[{"id": 2, "name": "zombie", "type": "hostile", "pos": [0.5, 64.0, -8.0]}],
        )
        ex2, st2 = w_far.session()
        with self.assertRaises(EnvBlocked):
            drive(KillNearSculkCatalyst(), ex2, st2)

        w_low = BiomeWorld(hp=8.0, blocks={(2, 64, 0): "sculk_catalyst"}, items={"iron_sword": 1})
        _, st3 = w_low.session()
        self.assertTrue(KillNearSculkCatalyst.blocked(st3))


class AssistAxolotlHuntTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_assist_hunt_happy_path_and_bucket_release(self):
        world = BiomeWorld(
            blocks={(2, 63, 0): "water"},
            items={"iron_sword": 1, "axolotl_bucket": 1},
            entities=[{"id": 31, "name": "drowned", "type": "hostile", "pos": [3.5, 64.0, 0.5]}],
        )
        ex, state = world.session()
        skill = AssistAxolotlHunt()
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertIn("interact", ex.acts)
        self.assertIn("attack", ex.acts)

    def test_assist_hunt_in_water_uses_direct_attack(self):
        world = BiomeWorld(
            in_water=True,
            items={"trident": 1},
            entities=[
                {"id": 32, "name": "axolotl", "type": "animal", "pos": [2.5, 64.0, 0.5]},
                {"id": 33, "name": "squid", "type": "animal", "pos": [3.0, 64.0, 0.5]},
            ],
        )
        ex, state = world.session()
        skill = AssistAxolotlHunt()
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertIn("attack", ex.acts)

    def test_missing_axolotl_or_prey_raises_env_blocked(self):
        w1 = BiomeWorld(items={"iron_sword": 1}, entities=[{"id": 1, "name": "cod", "type": "animal", "pos": [2.5, 64.0, 0.5]}])
        ex1, st1 = w1.session()
        with self.assertRaises(EnvBlocked):
            drive(AssistAxolotlHunt(), ex1, st1)

        w2 = BiomeWorld(items={"iron_sword": 1}, entities=[{"id": 2, "name": "axolotl", "type": "animal", "pos": [2.5, 64.0, 0.5]}])
        ex2, st2 = w2.session()
        with self.assertRaises(EnvBlocked):
            drive(AssistAxolotlHunt(), ex2, st2)


class ManageWolfArmorTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_remove_and_repair_modes(self):
        w_rem = BiomeWorld(
            items={"shears": 1},
            entities=[{"id": 41, "name": "wolf", "type": "animal", "tamed": True, "has_armor": True, "pos": [2.5, 64.0, 0.5]}],
        )
        ex1, st1 = w_rem.session()
        self.assertFalse(ManageWolfArmor(mode="repair").can_start(st1))
        s_rem = ManageWolfArmor(mode="remove")
        self.assertTrue(s_rem.can_start(st1))
        drive(s_rem, ex1, st1)
        self.assertEqual(s_rem.verify_done(st1), "")
        self.assertEqual(st1.count("wolf_armor"), 1)

        w_rep = BiomeWorld(
            items={"armadillo_scute": 2},
            entities=[{"id": 42, "name": "wolf", "type": "animal", "tamed": True, "has_armor": True, "pos": [2.5, 64.0, 0.5]}],
        )
        ex2, st2 = w_rep.session()
        self.assertFalse(ManageWolfArmor(mode="remove").can_start(st2))
        s_rep = ManageWolfArmor(mode="repair")
        drive(s_rep, ex2, st2)
        self.assertEqual(s_rep.verify_done(st2), "")
        self.assertEqual(st2.count("armadillo_scute"), 1)

    def test_unarmored_wolf_replan_and_missing_wolf_blocked(self):
        w1 = BiomeWorld(
            items={"shears": 1},
            entities=[{"id": 43, "name": "wolf", "type": "animal", "tamed": True, "has_armor": False, "pos": [2.5, 64.0, 0.5]}],
        )
        ex1, st1 = w1.session()
        with self.assertRaises(Replan):
            drive(ManageWolfArmor(mode="remove"), ex1, st1)

        w2 = BiomeWorld(items={"shears": 1}, entities=[])
        ex2, st2 = w2.session()
        with self.assertRaises(EnvBlocked):
            drive(ManageWolfArmor(mode="remove"), ex2, st2)


class AllayDeliverItemTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_hand_drop_and_deliver_happy_path(self):
        world = BiomeWorld(
            items={"cookie": 3},
            entities=[{"id": 51, "name": "allay", "type": "other", "pos": [2.5, 64.5, 0.5]}],
        )
        ex, state = world.session()
        skill = AllayDeliverItem()
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertIn("interact", ex.acts)
        self.assertIn("drop", ex.acts)
        self.assertEqual(state.count("cookie"), 2)

    def test_single_item_blocked_and_delivery_timeout_replan(self):
        w_one = BiomeWorld(items={"cookie": 1})
        _, st_one = w_one.session()
        self.assertTrue(AllayDeliverItem.blocked(st_one))

        w_slow = BiomeWorld(
            items={"cookie": 3},
            entities=[{"id": 52, "name": "allay", "type": "other", "pos": [2.5, 64.5, 0.5]}],
        )
        w_slow.allay_deliver_after = -1  # 永不归还
        ex, state = w_slow.session()
        with self.assertRaises(Replan):
            drive(AllayDeliverItem(), ex, state)


class BoatWithGoatTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_place_lure_board_and_dismount_happy_path(self):
        world = BiomeWorld(
            items={"oak_boat": 1, "wheat": 2},
            entities=[{"id": 61, "name": "goat", "type": "animal", "pos": [3.5, 64.0, 0.5]}],
        )
        ex, state = world.session()
        skill = BoatWithGoat()
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertIn("dismount", ex.acts)
        self.assertFalse(world.st["in_vehicle"])

    def test_abort_while_mounted_dismounts_cleanly(self):
        world = BiomeWorld(
            items={"bamboo_raft": 1},
            entities=[{"id": 62, "name": "goat", "type": "animal", "pos": [2.5, 64.0, 0.5]}],
        )
        ex, state = world.session()
        skill = BoatWithGoat()
        for _ in range(10):
            if skill.step(ex, state):
                break
            if world.st["in_vehicle"]:
                skill.abort()
                break
        self.assertFalse(world.st["in_vehicle"])
        self.assertTrue(any(m == "dismount" for m, _ in world.calls))

    def test_missing_goat_env_blocked_and_no_boat_entity_replan(self):
        w1 = BiomeWorld(items={"oak_boat": 1}, entities=[])
        ex1, st1 = w1.session()
        with self.assertRaises(EnvBlocked):
            drive(BoatWithGoat(), ex1, st1)

        w2 = BiomeWorld(items={"oak_boat": 1}, entities=[{"id": 63, "name": "goat", "type": "animal", "pos": [2.5, 64.0, 0.5]}])
        w2.spawn_boat = False
        ex2, st2 = w2.session()
        with self.assertRaises(Replan):
            drive(BoatWithGoat(), ex2, st2)


class SilkTouchBeeNestTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_silk_touch_detection_formats(self):
        # 无精准采集被拦截
        w_no = BiomeWorld(items={"diamond_axe": 1}, item_meta={"diamond_axe": {"enchantments": [{"name": "fortune", "level": 3}]}})
        _, st_no = w_no.session()
        self.assertTrue(SilkTouchBeeNest.blocked(st_no))

        # enchantments 列表格式
        w_e = BiomeWorld(items={"diamond_axe": 1}, item_meta={"diamond_axe": {"enchantments": [{"name": "silk_touch", "level": 1}]}})
        _, st_e = w_e.session()
        self.assertEqual(SilkTouchBeeNest.blocked(st_e), "")

        # components 嵌套格式
        w_c = BiomeWorld(
            items={"iron_pickaxe": 1},
            item_meta={"iron_pickaxe": {"components": {"minecraft:enchantments": {"levels": {"minecraft:silk_touch": 1}}}}},
        )
        _, st_c = w_c.session()
        self.assertEqual(SilkTouchBeeNest.blocked(st_c), "")

    def test_night_harvest_with_campfire_and_daytime_conservative_block(self):
        meta = {"diamond_axe": {"enchantments": [{"name": "silk_touch", "level": 1}]}}
        # 夜间 + 营火 -> 先放营火再精准采集
        w_night = BiomeWorld(
            blocks={(2, 65, 0): "bee_nest"},
            items={"diamond_axe": 1, "campfire": 1},
            item_meta=meta,
            time_of_day=15000,
        )
        ex1, st1 = w_night.session()
        skill = SilkTouchBeeNest()
        drive(skill, ex1, st1)
        self.assertEqual(skill.verify_done(st1), "")
        self.assertIn("place", ex1.acts)
        self.assertIn("dig", ex1.acts)
        self.assertEqual(st1.count("bee_nest"), 1)

        # 白天且无雨且无蜜蜂 -> 保守 EnvBlocked(wake_on="night")
        w_day = BiomeWorld(blocks={(2, 65, 0): "bee_nest"}, items={"diamond_axe": 1}, item_meta=meta, time_of_day=6000)
        ex2, st2 = w_day.session()
        with self.assertRaises(EnvBlocked) as ctx:
            drive(SilkTouchBeeNest(), ex2, st2)
        self.assertEqual(ctx.exception.wake_on, "night")

        # 白天但有蜜蜂活动 -> 允许保守采集
        w_bee = BiomeWorld(
            blocks={(2, 65, 0): "bee_nest"},
            items={"diamond_axe": 1},
            item_meta=meta,
            entities=[{"id": 71, "name": "bee", "type": "animal", "pos": [3.0, 65.5, 0.5]}],
            time_of_day=6000,
        )
        ex3, st3 = w_bee.session()
        s3 = SilkTouchBeeNest()
        drive(s3, ex3, st3)
        self.assertEqual(s3.verify_done(st3), "")

        # 雨天无营火也能正常精准采集；挖完未收到蜂巢则抛 Replan
        w_rain = BiomeWorld(blocks={(2, 65, 0): "bee_nest"}, items={"diamond_axe": 1}, item_meta=meta, time_of_day=6000, is_raining=True)
        ex4, st4 = w_rain.session()
        s4 = SilkTouchBeeNest()
        drive(s4, ex4, st4)
        self.assertEqual(s4.verify_done(st4), "")

        w_nodrop = BiomeWorld(blocks={(2, 65, 0): "bee_nest"}, items={"diamond_axe": 1}, item_meta=meta, time_of_day=15000)
        w_nodrop.dig_grants_nest = False
        ex5, st5 = w_nodrop.session()
        with self.assertRaises(Replan):
            drive(SilkTouchBeeNest(), ex5, st5)


class CatchTadpoleTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_catch_tadpole_happy_path_and_errors(self):
        world = BiomeWorld(
            items={"water_bucket": 1},
            entities=[{"id": 81, "name": "tadpole", "type": "animal", "pos": [2.5, 64.0, 0.5]}],
        )
        ex, state = world.session()
        skill = CatchTadpole()
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("tadpole_bucket"), 1)

        w_none = BiomeWorld(items={"water_bucket": 1}, entities=[])
        ex_none, st_none = w_none.session()
        with self.assertRaises(EnvBlocked):
            drive(CatchTadpole(), ex_none, st_none)

        w_fail = BiomeWorld(
            items={"water_bucket": 1},
            entities=[{"id": 82, "name": "tadpole", "type": "animal", "pos": [2.5, 64.0, 0.5]}],
        )
        w_fail.tadpole_catch_ok = False
        ex2, st2 = w_fail.session()
        with self.assertRaises(Replan):
            drive(CatchTadpole(), ex2, st2)


class SpyglassObserveTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_observe_parrot_and_ghast(self):
        for target in ("parrot", "ghast"):
            with self.subTest(target=target):
                world = BiomeWorld(
                    items={"spyglass": 1},
                    entities=[{"id": 91, "name": target, "type": "animal", "pos": [6.5, 66.5, 0.5]}],
                )
                ex, state = world.session()
                skill = SpyglassObserve(target=target)
                steps = drive(skill, ex, state)
                self.assertEqual(skill.verify_done(state), "")
                ops = [s["op"] for s in steps if s]
                self.assertEqual(ops, ["look", "use_item"])
                self.assertEqual(skill.goal, f"t3:spyglass_{target}")

    def test_missing_or_occluded_target_raises_env_blocked(self):
        w_none = BiomeWorld(items={"spyglass": 1}, entities=[])
        ex0, st0 = w_none.session()
        with self.assertRaises(EnvBlocked):
            drive(SpyglassObserve(target="parrot"), ex0, st0)

        wall = {(3, y, z): "stone" for y in range(64, 70) for z in range(-3, 4)}
        world = BiomeWorld(
            blocks=wall,
            items={"spyglass": 1},
            entities=[{"id": 92, "name": "ghast", "type": "hostile", "pos": [8.5, 66.5, 0.5]}],
        )
        ex, state = world.session()
        with self.assertRaises(EnvBlocked) as ctx:
            drive(SpyglassObserve(target="ghast"), ex, state)
        self.assertIn("visible_los", ctx.exception.missing_predicate)


if __name__ == "__main__":
    unittest.main()
