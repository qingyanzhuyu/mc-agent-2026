"""Sprint 1 TDD 离线单测：survival_food / GatherFood。"""
from __future__ import annotations

from copy import deepcopy
import math
import unittest
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.runtime import EnvBlocked
from mcagent.skills import REGISTRY, contract_errors, make
from mcagent.skills.base import FORBIDDEN_PARAMS
from mcagent.skills.items import bare
from mcagent.skills.survival_food import (
    COOKED_FOODS,
    FOOD_RESTORE,
    PASSIVE_FOOD_MOBS,
    RAW_MEAT_TO_COOKED,
    GatherFood,
    cooked_food_count,
    inventory_food_restore,
    raw_meat_count,
)
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.world_memo import memo_for


EXPECTED_FOOD_RESTORE: dict[str, int] = {
    "cooked_beef": 8,
    "cooked_porkchop": 8,
    "cooked_mutton": 6,
    "cooked_chicken": 6,
    "cooked_salmon": 6,
    "cooked_cod": 5,
    "cooked_rabbit": 5,
    "golden_carrot": 6,
    "bread": 5,
    "baked_potato": 5,
    "pumpkin_pie": 8,
    "apple": 4,
    "golden_apple": 4,
    "enchanted_golden_apple": 4,
    "carrot": 3,
    "beef": 3,
    "porkchop": 3,
    "rabbit": 3,
    "mutton": 2,
    "chicken": 2,
    "cod": 2,
    "salmon": 2,
    "sweet_berries": 2,
    "glow_berries": 2,
    "melon_slice": 2,
    "cookie": 2,
    "potato": 1,
    "beetroot": 1,
    "dried_kelp": 1,
    "mushroom_stew": 6,
    "beetroot_soup": 6,
    "rabbit_stew": 10,
}

EXPECTED_COOKED_FOODS: frozenset[str] = frozenset({
    "cooked_beef",
    "cooked_porkchop",
    "cooked_mutton",
    "cooked_chicken",
    "cooked_salmon",
    "cooked_cod",
    "cooked_rabbit",
    "bread",
    "baked_potato",
    "golden_carrot",
    "pumpkin_pie",
    "golden_apple",
    "enchanted_golden_apple",
    "mushroom_stew",
    "beetroot_soup",
    "rabbit_stew",
    "apple",
})

EXPECTED_RAW_MEAT_TO_COOKED: dict[str, str] = {
    "beef": "cooked_beef",
    "porkchop": "cooked_porkchop",
    "mutton": "cooked_mutton",
    "chicken": "cooked_chicken",
    "rabbit": "cooked_rabbit",
    "cod": "cooked_cod",
    "salmon": "cooked_salmon",
}

_PASSIVE_DROPS: dict[str, list[tuple[str, int]]] = {
    "sheep": [("mutton", 2), ("white_wool", 1)],
    "cow": [("beef", 2), ("leather", 1)],
    "pig": [("porkchop", 2)],
    "chicken": [("chicken", 1), ("feather", 1)],
    "rabbit": [("rabbit", 1)],
}


def _animal(name: str, entity_id: int, pos=(2.5, 64.0, 0.5), **extra) -> dict:
    return {
        "id": entity_id,
        "name": name,
        "type": "animal",
        "pos": [float(v) for v in pos],
        "health": 10,
        **extra,
    }


def _hostile(name: str, entity_id: int, pos=(8.5, 64.0, 0.5), **extra) -> dict:
    return {
        "id": entity_id,
        "name": name,
        "type": "hostile",
        "pos": [float(v) for v in pos],
        "health": 20,
        **extra,
    }


class FoodWorld(GridWorld):
    """面向 GatherFood 的离线可控夹具世界。"""

    def __init__(
        self,
        pos=(0.5, 64.0, 0.5),
        blocks: dict | None = None,
        block_props: dict | None = None,
        items: dict | None = None,
        entities: list[dict] | None = None,
        *,
        delayed_furnace: bool = False,
        furnace_views: list[dict] | None = None,
        omit_is_day: bool = False,
        **status_overrides,
    ):
        floor = {(x, 63, z): "stone" for x in range(-12, 13) for z in range(-12, 13)}
        floor.update(blocks or {})
        super().__init__(pos=pos, blocks=floor, items=items)
        self.st.update({
            "dimension": "overworld",
            "is_day": True,
            "time_of_day": 1000,
            **status_overrides,
        })
        if omit_is_day:
            self.st.pop("is_day", None)
        self.block_props: dict[tuple[int, int, int], dict] = {
            tuple(k): dict(v) for k, v in (block_props or {}).items()
        }
        self.entities: list[dict] = [deepcopy(e) for e in (entities or ())]
        self.held: dict | None = None
        self.offhand: dict | None = None
        self._next_drop_id = 9000
        self.delayed_furnace = delayed_furnace
        self.furnace_views = list(furnace_views) if furnace_views is not None else None
        self.furnace: dict[str, list[dict]] = {"input": [], "fuel": [], "output": []}
        self.last_view: dict = {
            "window": "furnace",
            "sections": deepcopy(self.furnace),
            "info": {"burning": False, "progress": 0.0, "fuel_left": 0.0},
        }

    def inventory(self, timeout=10) -> dict:
        inv = super().inventory(timeout)
        if self.held is not None:
            inv["held"] = dict(self.held)
        if self.offhand is not None:
            inv["offhand"] = dict(self.offhand)
        return inv

    def equip(self, item, slot="hand", *, stop_on=None) -> dict:
        res = super().equip(item, slot=slot, stop_on=stop_on)
        if slot == "off-hand":
            self.offhand = {"name": item, "count": 1}
        else:
            self.held = {"name": item, "count": 1}
        return res

    def get_block(self, pos) -> dict:
        cell = tuple(int(math.floor(v)) for v in pos)
        out = {"name": self.blocks.get(cell, "air")}
        if cell in self.block_props:
            out["properties"] = dict(self.block_props[cell])
        return out

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None) -> dict:
        if self.st.get("ready") is False:
            raise ApiError("not_ready", "chunks loading")
        self.calls.append(("find_blocks", {"types": types, "radius": radius, "limit": limit}))
        wanted = {bare(t) for t in ([types] if isinstance(types, str) else types or ())}
        px, py, pz = self.st["pos"]
        hits = []
        for pos, name in self.blocks.items():
            bname = bare(name)
            if wanted and bname not in wanted:
                continue
            props = self.block_props.get(pos, {})
            if properties and any(props.get(k) != v for k, v in properties.items()):
                continue
            if mature is True and props.get("age", 3) < 2:
                continue
            d = math.dist((px, py, pz), (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5))
            if d <= radius:
                entry = {"name": bname, "pos": list(pos), "dist": round(d, 3)}
                if props:
                    entry["properties"] = dict(props)
                    entry["mature"] = props.get("age", 0) >= 2
                hits.append(entry)
        hits.sort(key=lambda h: h["dist"])
        return {"blocks": hits[:limit]}

    def get_entities(self, radius=32, types=None) -> dict:
        if self.st.get("ready") is False:
            raise ApiError("not_ready", "entities loading")
        self.calls.append(("get_entities", {"radius": radius, "types": types}))
        wanted = {bare(t) for t in ([types] if isinstance(types, str) else types or ())}
        px, py, pz = self.st["pos"]
        out = []
        for ent in self.entities:
            ename = bare(ent.get("name"))
            etype = ent.get("type")
            if wanted and ename not in wanted and etype not in wanted:
                continue
            d = math.dist((px, py, pz), ent["pos"])
            if d <= radius:
                row = deepcopy(ent)
                row["dist"] = round(d, 3)
                out.append(row)
        out.sort(key=lambda e: e["dist"])
        return {"entities": out}

    def _pickup_nearby_drops(self, pickup_radius: float = 2.2) -> None:
        pos = self.st["pos"]
        remaining = []
        for ent in self.entities:
            if ent.get("type") == "item" and math.dist(pos, ent["pos"]) <= pickup_radius:
                spec = str(ent.get("item") or "")
                name, sep, cnt = spec.rpartition("x")
                item_name = bare(name if sep and cnt.isdigit() else spec)
                qty = int(cnt) if sep and cnt.isdigit() else 1
                self.items[item_name] = self.items.get(item_name, 0) + qty
            else:
                remaining.append(ent)
        self.entities = remaining

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        if pos is None and entity_id is not None:
            ent = next((e for e in self.entities if e.get("id") == entity_id), None)
            if ent is None:
                raise ApiError("no_such_entity", f"entity {entity_id} not found")
            pos = list(ent["pos"])
        res = super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)
        self._pickup_nearby_drops()
        return res

    def attack(self, entity_id, timeout_ms=20000, *, max_hits=None, interval_ms=None, stop_on=None) -> dict:
        self.calls.append(("attack", {"entity_id": entity_id, "max_hits": max_hits, "stop_on": stop_on}))
        idx = next((i for i, e in enumerate(self.entities) if e.get("id") == entity_id), None)
        if idx is None:
            raise ApiError("no_such_entity", f"entity {entity_id} not found")
        ent = self.entities.pop(idx)
        mname = bare(ent.get("name"))
        for drop_item, qty in _PASSIVE_DROPS.get(mname, ()):
            self._next_drop_id += 1
            self.entities.append({
                "id": self._next_drop_id,
                "type": "item",
                "name": "item",
                "item": f"{drop_item}x{qty}",
                "pos": list(ent["pos"]),
            })
        return {"killed": True, "hits": 1}

    def dig(self, pos, collect=True, *, stop_on=None) -> dict:
        res = super().dig(pos, collect=collect, stop_on=stop_on)
        cell = tuple(int(math.floor(v)) for v in pos)
        self.block_props.pop(cell, None)
        return res

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None) -> dict:
        self.calls.append(("interact", {"pos": pos, "entity_id": entity_id, "item": item}))
        if pos is not None:
            cell = tuple(int(math.floor(v)) for v in pos)
            if bare(self.blocks.get(cell, "air")) == "sweet_berry_bush":
                props = self.block_props.get(cell, {"age": 3})
                if props.get("age", 3) >= 2:
                    self.items["sweet_berries"] = self.items.get("sweet_berries", 0) + 3
                    self.block_props[cell] = {"age": 1}
                    return {"changed": True, "gained": {"sweet_berries": 3}}
                return {"changed": False}
        return {"changed": True}

    def craft(self, item, count=1, *, stop_on=None) -> dict:
        self.calls.append(("craft", {"item": item, "count": count}))
        name = bare(item)
        if name == "wheat":
            if self.items.get("hay_block", 0) < count:
                raise ApiError("missing_item", "缺少 hay_block")
            self.items["hay_block"] -= count
            self.items["wheat"] = self.items.get("wheat", 0) + 9 * count
        elif name == "bread":
            if self.items.get("wheat", 0) < 3 * count:
                raise ApiError("missing_item", "缺少 wheat")
            self.items["wheat"] -= 3 * count
            self.items["bread"] = self.items.get("bread", 0) + count
        elif name.endswith("_planks"):
            log = name.removesuffix("_planks") + "_log"
            if self.items.get(log, 0) < count:
                raise ApiError("missing_item", f"缺少 {log}")
            self.items[log] -= count
            self.items[name] = self.items.get(name, 0) + 4 * count
        elif name == "crafting_table":
            plank = next((k for k, v in self.items.items() if k.endswith("_planks") and v >= 4 * count), None)
            if plank is None:
                raise ApiError("missing_item", "缺少木板")
            self.items[plank] -= 4 * count
            self.items["crafting_table"] = self.items.get("crafting_table", 0) + count
        elif name == "furnace":
            if self.items.get("cobblestone", 0) < 8 * count:
                raise ApiError("missing_item", "缺少圆石")
            self.items["cobblestone"] -= 8 * count
            self.items["furnace"] = self.items.get("furnace", 0) + count
        else:
            raise ApiError("missing_item", f"夹具未配置配方: {name}")
        return {"crafted": name, "count": count}

    def window_view(self, target, *, stop_on=None) -> dict:
        self.calls.append(("window_view", {"target": deepcopy(target)}))
        if self.furnace_views is not None:
            if self.furnace_views:
                self.last_view = self.furnace_views.pop(0)
            return deepcopy(self.last_view)
        return {
            "window": "furnace",
            "sections": deepcopy(self.furnace),
            "info": {
                "burning": bool(self.furnace["input"] and self.furnace["fuel"]),
                "progress": 0.5 if self.furnace["input"] else 0.0,
                "fuel_left": 0.5 if self.furnace["fuel"] else 0.0,
            },
        }

    def window_act(self, target, ops, *, stop_on=None) -> dict:
        self.calls.append(("window_act", {"target": deepcopy(target), "ops": deepcopy(ops)}))
        for op in ops:
            kind = op["op"]
            if kind == "put":
                item = bare(op["item"])
                count = int(op["count"])
                to = op["to"]
                if self.items.get(item, 0) < count:
                    raise ApiError("missing_item", f"背包缺少 {item}")
                self.items[item] -= count
                slots = self.furnace.setdefault(to, [])
                existing = next((s for s in slots if bare(s["name"]) == item), None)
                if existing is not None:
                    existing["count"] += count
                else:
                    slots.append({"name": item, "count": count})
            elif kind == "take":
                frm = op["from"]
                slots = self.furnace.get(frm, [])
                if not slots:
                    raise ApiError("empty_slot", f"{frm} 槽位为空")
                wanted = bare(op.get("item")) if op.get("item") else None
                entry = next((s for s in slots if wanted is None or bare(s["name"]) == wanted), None)
                if entry is None:
                    raise ApiError("missing_item", f"{frm} 未找到 {wanted}")
                take_qty = min(int(op.get("count") or entry["count"]), entry["count"])
                entry["count"] -= take_qty
                if entry["count"] <= 0:
                    slots.remove(entry)
                iname = bare(entry["name"])
                self.items[iname] = self.items.get(iname, 0) + take_qty
        if self.furnace["input"] and self.furnace["fuel"] and not self.delayed_furnace:
            cooked_slots = self.furnace["output"]
            remaining_input = []
            for entry in self.furnace["input"]:
                raw_name = bare(entry["name"])
                cooked_name = RAW_MEAT_TO_COOKED.get(raw_name)
                if cooked_name:
                    out_entry = next((s for s in cooked_slots if bare(s["name"]) == cooked_name), None)
                    if out_entry is not None:
                        out_entry["count"] += entry["count"]
                    else:
                        cooked_slots.append({"name": cooked_name, "count": entry["count"]})
                else:
                    remaining_input.append(entry)
            self.furnace["input"] = remaining_input
        return {"done": len(ops)}

    def advancements(self, status="completed", include_recipes=False) -> dict:
        return {"ready": True, "advancements": []}


class TestRegistryAndContract(unittest.TestCase):
    def test_gather_food_registered_and_contract_valid(self):
        self.assertIn("gather_food", REGISTRY)
        self.assertIs(REGISTRY["gather_food"], GatherFood)
        self.assertEqual(contract_errors(GatherFood), [])
        self.assertEqual(set(GatherFood.params) & FORBIDDEN_PARAMS, set())
        self.assertEqual(GatherFood.verified, "live")
        self.assertGreater(len(GatherFood.description), 0)
        self.assertLessEqual(len(GatherFood.description), 60)
        self.assertEqual(GatherFood.cooldown_s, 15.0)
        self.assertEqual(GatherFood.goal, "survival:gather_food")

        skill = make("gather_food")
        self.assertIsInstance(skill, GatherFood)
        self.assertEqual(skill.args, {"target_restore": 16, "cook": True})

    def test_gather_food_param_bounds_and_types(self):
        self.assertEqual(make("gather_food", {"target_restore": 4, "cook": False}).args,
                         {"target_restore": 4, "cook": False})
        self.assertEqual(make("gather_food", {"target_restore": 64}).args["target_restore"], 64)
        for bad in (
            {"target_restore": 3},
            {"target_restore": 65},
            {"target_restore": True},
            {"cook": "yes"},
            {"pos": [1, 64, 0]},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    make("gather_food", bad)


class TestInventoryHelpers(unittest.TestCase):
    def test_food_constants_match_vanilla_spec(self):
        self.assertEqual(len(FOOD_RESTORE), 32)
        self.assertEqual(FOOD_RESTORE, EXPECTED_FOOD_RESTORE)
        self.assertIsInstance(COOKED_FOODS, frozenset)
        self.assertEqual(COOKED_FOODS, EXPECTED_COOKED_FOODS)
        for raw_item in EXPECTED_RAW_MEAT_TO_COOKED:
            self.assertNotIn(raw_item, COOKED_FOODS)
        self.assertEqual(RAW_MEAT_TO_COOKED, EXPECTED_RAW_MEAT_TO_COOKED)
        self.assertEqual(PASSIVE_FOOD_MOBS, ("cow", "pig", "sheep", "chicken", "rabbit"))

    def test_inventory_food_restore_cooked_and_raw_counts(self):
        _, empty_state = FoodWorld(items={}).session()
        self.assertEqual(inventory_food_restore(empty_state), 0)
        self.assertEqual(cooked_food_count(empty_state), 0)
        self.assertEqual(raw_meat_count(empty_state), 0)

        _, non_food_state = FoodWorld(items={"cobblestone": 32, "oak_log": 8, "stone_sword": 1}).session()
        self.assertEqual(inventory_food_restore(non_food_state), 0)
        self.assertEqual(cooked_food_count(non_food_state), 0)
        self.assertEqual(raw_meat_count(non_food_state), 0)

        mixed_items = {
            "cooked_beef": 2,    # 2*8 = 16 (cooked)
            "beef": 3,           # 3*3 = 9  (raw)
            "bread": 1,          # 1*5 = 5  (cooked)
            "apple": 2,          # 2*4 = 8  (cooked/ready)
            "sweet_berries": 4,  # 4*2 = 8  (uncooked plant food)
            "cobblestone": 16,
            "oak_log": 4,
        }
        _, mixed_state = FoodWorld(items=mixed_items).session()
        self.assertEqual(inventory_food_restore(mixed_state), 46)
        self.assertEqual(cooked_food_count(mixed_state), 5)
        self.assertEqual(raw_meat_count(mixed_state), 3)

        raw_only = {
            "porkchop": 2,
            "mutton": 1,
            "chicken": 1,
            "rabbit": 1,
            "cod": 2,
            "salmon": 1,
        }
        _, raw_state = FoodWorld(items=raw_only).session()
        self.assertEqual(raw_meat_count(raw_state), 8)
        self.assertEqual(cooked_food_count(raw_state), 0)
        self.assertEqual(inventory_food_restore(raw_state), 19)


class TestDaytimeHunting(unittest.TestCase):
    def test_equips_best_weapon_and_prioritizes_sheep_when_wool_lt_3(self):
        world = FoodWorld(
            items={"wooden_axe": 1, "stone_sword": 1, "white_wool": 1},
            entities=[
                _animal("cow", 101, (2.5, 64.0, 0.5)),    # 更近的牛
                _animal("sheep", 102, (4.5, 64.0, 0.5)),  # 稍远的羊（羊毛 < 3 时优先）
                _animal("pig", 103, (6.5, 64.0, 0.5)),    # 凑够 target_restore 后不再打
            ],
        )
        ex, state = world.session()
        before_counts = {"mutton": 0, "beef": 0, "white_wool": 1, "leather": 0}
        skill = make("gather_food", {"target_restore": 8, "cook": False})

        drive(skill, ex, state)

        equips = [req["item"] for op, req in world.calls if op == "equip"]
        self.assertIn("stone_sword", equips)
        self.assertNotIn("wooden_axe", equips)

        attacks = [req["entity_id"] for op, req in world.calls if op == "attack"]
        self.assertEqual(attacks, [102, 101])  # 先羊后牛，够 8 点后提前停手不打猪 (103)
        self.assertGreaterEqual(world.items.get("mutton", 0), 2)
        self.assertGreaterEqual(world.items.get("white_wool", 0), 2)
        self.assertGreaterEqual(world.items.get("beef", 0), 2)
        self.assertGreaterEqual(world.items.get("leather", 0), 1)
        self.assertGreaterEqual(inventory_food_restore(state), 8)
        self.assertEqual(skill.verify_done(state, before_counts), "")

    def test_wool_ge_3_attacks_nearest_passive_mob_first(self):
        world = FoodWorld(
            items={"stone_sword": 1, "white_wool": 3},
            entities=[
                _animal("cow", 201, (2.5, 64.0, 0.5)),
                _animal("sheep", 202, (5.5, 64.0, 0.5)),
            ],
        )
        ex, state = world.session()
        skill = make("gather_food", {"target_restore": 4, "cook": False})

        drive(skill, ex, state)

        attacks = [req["entity_id"] for op, req in world.calls if op == "attack"]
        self.assertEqual(attacks, [201])
        self.assertGreaterEqual(world.items.get("beef", 0), 2)
        self.assertGreaterEqual(inventory_food_restore(state), 4)
        self.assertEqual(skill.verify_done(state), "")

    def test_already_meets_target_restore_stops_without_attacking(self):
        world = FoodWorld(
            items={"cooked_beef": 2},
            entities=[_animal("cow", 203, (2.5, 64.0, 0.5))],
        )
        ex, state = world.session()
        skill = make("gather_food", {"target_restore": 16, "cook": True})

        drive(skill, ex, state)

        self.assertEqual([op for op, _ in world.calls if op == "attack"], [])
        self.assertEqual(skill.verify_done(state), "")


class TestHostileAvoidance(unittest.TestCase):
    def test_skips_passive_within_8_blocks_of_hostile_and_hunts_safe_pig(self):
        world = FoodWorld(
            items={"stone_sword": 1, "white_wool": 3},
            entities=[
                _animal("cow", 301, (4.5, 64.0, 0.5)),      # 距僵尸 5.0 格 <= 8，跳过
                _hostile("zombie", 901, (9.5, 64.0, 0.5)),  # 距玩家 9.0 格
                _animal("pig", 302, (-6.5, 64.0, 0.5)),     # 距僵尸 16.0 格 > 8，安全
            ],
        )
        ex, state = world.session()
        skill = make("gather_food", {"target_restore": 4, "cook": False})

        drive(skill, ex, state)

        attacks = [req["entity_id"] for op, req in world.calls if op == "attack"]
        self.assertEqual(attacks, [302])
        self.assertGreaterEqual(world.items.get("porkchop", 0), 2)
        self.assertEqual(skill.verify_done(state), "")

    def test_all_passives_near_hostiles_raises_env_blocked(self):
        world = FoodWorld(
            items={"stone_sword": 1},
            entities=[
                _animal("cow", 303, (4.5, 64.0, 0.5)),
                _animal("sheep", 304, (6.5, 64.0, 0.5)),
                _hostile("zombie", 902, (8.5, 64.0, 0.5)),
            ],
        )
        ex, state = world.session()
        skill = make("gather_food", {"target_restore": 8, "cook": False})

        with self.assertRaises(EnvBlocked) as ctx:
            drive(skill, ex, state)

        self.assertEqual(ctx.exception.missing_predicate, "food_source")
        self.assertEqual(ctx.exception.wake_on, "chunk_moved")
        self.assertEqual([op for op, _ in world.calls if op == "attack"], [])


class TestFurnaceCooking(unittest.TestCase):
    def test_cooks_raw_beef_in_nearby_furnace_daytime(self):
        world = FoodWorld(
            items={"beef": 2, "coal": 1},
            blocks={(2, 64, 0): "furnace"},
            is_day=True,
        )
        ex, state = world.session()
        before_cooked = cooked_food_count(state)
        before_counts = {"beef": 2, "cooked_beef": 0}
        skill = make("gather_food", {"target_restore": 16, "cook": True})

        with patch("time.sleep", return_value=None):
            drive(skill, ex, state)

        self.assertIn("window_view", [op for op, _ in world.calls])
        self.assertIn("window_act", [op for op, _ in world.calls])
        self.assertEqual(world.items.get("cooked_beef", 0), 2)
        self.assertGreater(cooked_food_count(state), before_cooked)
        self.assertEqual(skill.verify_done(state, before_counts), "")

    def test_cooks_raw_meat_via_memo_station(self):
        world = FoodWorld(
            items={"porkchop": 2, "coal": 1},
            blocks={(4, 64, 0): "furnace"},
            is_day=True,
        )
        ex, state = world.session()
        memo_for(state).record_station("furnace", (4, 64, 0), dimension="overworld")
        before_cooked = cooked_food_count(state)
        skill = make("gather_food", {"target_restore": 16, "cook": True})

        with patch("time.sleep", return_value=None):
            drive(skill, ex, state)

        self.assertEqual(world.items.get("cooked_porkchop", 0), 2)
        self.assertGreater(cooked_food_count(state), before_cooked)
        self.assertEqual(skill.verify_done(state), "")

    def test_nighttime_with_raw_meat_and_fuel_is_allowed_and_cooks_without_hunting(self):
        world = FoodWorld(
            items={"beef": 2, "coal": 1},
            blocks={(2, 64, 0): "furnace"},
            entities=[_animal("cow", 401, (3.5, 64.0, 0.5))],
            is_day=False,
            time_of_day=15000,
        )
        ex, state = world.session()
        self.assertEqual(GatherFood.blocked(state), "")
        skill = make("gather_food", {"target_restore": 16, "cook": True})

        with patch("time.sleep", return_value=None):
            drive(skill, ex, state)

        self.assertEqual([op for op, _ in world.calls if op == "attack"], [])
        self.assertEqual(world.items.get("cooked_beef", 0), 2)
        self.assertEqual(cooked_food_count(state), 2)
        self.assertEqual(skill.verify_done(state), "")

    def test_live_like_delayed_furnace_exits_cleanly_after_bounded_poll(self):
        world = FoodWorld(
            items={"beef": 2, "coal": 1},
            blocks={(2, 64, 0): "furnace"},
            delayed_furnace=True,
            is_day=True,
        )
        ex, state = world.session()
        skill = make("gather_food", {"target_restore": 16, "cook": True})

        with patch("time.sleep", return_value=None):
            drive(skill, ex, state)

        self.assertEqual(world.items.get("beef", 0), 0)
        put_ops = [
            op
            for call_name, req in world.calls
            if call_name == "window_act"
            for op in req["ops"]
            if op.get("op") == "put"
        ]
        self.assertTrue(any(op.get("item") == "beef" and op.get("to") == "input" for op in put_ops))
        self.assertEqual(skill.verify_done(state), "")

    def test_places_held_furnace_and_cooks_raw_meat(self):
        world = FoodWorld(
            items={"beef": 2, "coal": 1, "furnace": 1},
            blocks={},
            is_day=True,
        )
        ex, state = world.session()
        skill = make("gather_food", {"target_restore": 16, "cook": True})

        with patch("time.sleep", return_value=None):
            drive(skill, ex, state)

        places = [req["item"] for op, req in world.calls if op == "place"]
        self.assertIn("furnace", places)
        self.assertEqual(world.items.get("cooked_beef", 0), 2)
        self.assertTrue(memo_for(state).has_station("furnace", dimension="overworld"))
        self.assertEqual(skill.verify_done(state), "")

    def test_cooks_raw_meat_in_smoker(self):
        class SmokerWorld(FoodWorld):
            def window_view(self, target, *, stop_on=None) -> dict:
                res = super().window_view(target, stop_on=stop_on)
                res["window"] = "smoker"
                return res

        world = SmokerWorld(
            items={"mutton": 2, "coal": 1},
            blocks={(2, 64, 0): "smoker"},
            is_day=True,
        )
        ex, state = world.session()
        skill = make("gather_food", {"target_restore": 12, "cook": True})

        with patch("time.sleep", return_value=None):
            drive(skill, ex, state)

        self.assertEqual(world.items.get("cooked_mutton", 0), 2)
        self.assertEqual(skill.verify_done(state), "")


class TestBlockingGates(unittest.TestCase):
    def test_night_without_raw_meat_is_blocked(self):
        for items in ({}, {"coal": 2, "bread": 1}):
            with self.subTest(items=items):
                _, state = FoodWorld(items=items, is_day=False, time_of_day=15000).session()
                self.assertEqual(GatherFood.blocked(state), "夜晚停止地表狩猎补粮")

    def test_early_night_time_of_day_13000_raw_meat_no_fuel_is_blocked(self):
        for world_kwargs in (
            {"omit_is_day": True, "time_of_day": 13000},
            {"is_day": True, "time_of_day": 13000},
        ):
            with self.subTest(world_kwargs=world_kwargs):
                _, state = FoodWorld(items={"beef": 2}, **world_kwargs).session()
                self.assertEqual(GatherFood.blocked(state), "夜晚停止地表狩猎补粮")

    def test_nether_and_end_dimensions_are_blocked(self):
        for dim in ("the_nether", "the_end"):
            with self.subTest(dimension=dim):
                _, state = FoodWorld(items={"beef": 2, "coal": 1}, dimension=dim, is_day=True).session()
                self.assertTrue(GatherFood.blocked(state))

    def test_not_ready_or_dead_is_blocked(self):
        _, not_ready = FoodWorld(ready=False, is_day=True).session()
        self.assertTrue(GatherFood.blocked(not_ready))

        _, dead = FoodWorld(alive=False, is_day=True).session()
        self.assertTrue(GatherFood.blocked(dead))


class TestFallbackFoodSources(unittest.TestCase):
    def test_fallback_hay_block_crafts_wheat_and_bread(self):
        world = FoodWorld(
            blocks={(2, 64, 0): "hay_block", (-2, 64, 0): "crafting_table"},
            entities=[],
            is_day=True,
        )
        ex, state = world.session()
        before_counts = {"bread": 0, "wheat": 0}
        skill = make("gather_food", {"target_restore": 12, "cook": False})

        drive(skill, ex, state)

        digs = [req["pos"] for op, req in world.calls if op == "dig"]
        self.assertIn([2, 64, 0], digs)
        crafted = [req["item"] for op, req in world.calls if op == "craft"]
        self.assertIn("wheat", crafted)
        self.assertIn("bread", crafted)
        self.assertGreaterEqual(state.count("bread"), 3)
        self.assertGreaterEqual(inventory_food_restore(state), 12)
        self.assertEqual(skill.verify_done(state, before_counts), "")

    def test_fallback_sweet_berry_bush_interacts_without_digging(self):
        bush1 = (5, 64, 0)
        bush2 = (5, 64, 2)
        world = FoodWorld(
            blocks={bush1: "sweet_berry_bush", bush2: "sweet_berry_bush"},
            block_props={bush1: {"age": 3}, bush2: {"age": 3}},
            entities=[],
            is_day=True,
        )
        ex, state = world.session()
        before_counts = {"sweet_berries": 0}
        skill = make("gather_food", {"target_restore": 6, "cook": False})

        drive(skill, ex, state)

        interacts = [req["pos"] for op, req in world.calls if op == "interact"]
        self.assertTrue(any(pos in ([5, 64, 0], [5, 64, 2]) for pos in interacts))
        self.assertEqual([op for op, _ in world.calls if op == "dig"], [])
        self.assertGreaterEqual(state.count("sweet_berries"), 3)
        self.assertGreaterEqual(inventory_food_restore(state), 6)
        self.assertEqual(skill.verify_done(state, before_counts), "")


class TestBarrenWorld(unittest.TestCase):
    def test_barren_world_raises_env_blocked(self):
        world = FoodWorld(entities=[], is_day=True)
        ex, state = world.session()
        skill = make("gather_food", {"target_restore": 16, "cook": False})

        with self.assertRaises(EnvBlocked) as ctx:
            drive(skill, ex, state)

        self.assertEqual(ctx.exception.missing_predicate, "food_source")
        self.assertEqual(ctx.exception.wake_on, "chunk_moved")


class TestStepContractAndAbort(unittest.TestCase):
    def test_abort_is_idempotent_resets_flow_and_restarts_cleanly(self):
        world = FoodWorld(
            items={"stone_sword": 1},
            entities=[
                _animal("sheep", 501, (4.5, 64.0, 0.5)),
                _animal("cow", 502, (2.5, 64.0, 0.5)),
            ],
        )
        ex, state = world.session()
        skill = make("gather_food", {"target_restore": 8, "cook": False})

        done = skill.step(ex, state)
        self.assertFalse(done)
        self.assertIsNotNone(skill._flow)

        skill.abort()
        skill.abort()
        self.assertIsNone(skill._flow)

        drive(skill, ex, state)
        self.assertGreaterEqual(inventory_food_restore(state), 8)
        self.assertEqual(skill.verify_done(state), "")

    def test_skipping_unreachable_animal_preserves_one_action_per_step(self):
        class FlakyMoveWorld(FoodWorld):
            def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
                if entity_id == 601:
                    self.calls.append(("move_to", {"entity_id": entity_id, "pos": pos}))
                    raise ApiError("path_not_found", "blocked")
                return super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)

        world = FlakyMoveWorld(
            items={"stone_sword": 1, "white_wool": 3},
            entities=[
                _animal("cow", 601, (8.5, 64.0, 0.5)),   # unreachable -> skipped with error yield
                _animal("pig", 602, (9.5, 64.0, 0.5)),   # reachable -> hunted
            ],
        )
        ex, state = world.session()
        skill = make("gather_food", {"target_restore": 4, "cook": False})

        drive(skill, ex, state)

        attacks = [req["entity_id"] for op, req in world.calls if op == "attack"]
        self.assertEqual(attacks, [602])
        self.assertEqual(skill.verify_done(state), "")


if __name__ == "__main__":
    unittest.main()
