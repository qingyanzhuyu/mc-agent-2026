"""阶段二架构体检最小 TDD 回归单测：新制铁甲/盾自动穿戴、跨门强制 auto_bunker、地下回营存箱与禁地下建营。"""
from __future__ import annotations

import math
import unittest

from mcagent.client import ApiError
from mcagent.loop.policy import RulePlanner
from mcagent.runtime import RuntimeConfig, State
from mcagent.skills.camp_base import ManageCampChest
from mcagent.skills.iron_gear import CraftIronGear
from mcagent.skills.items import bare
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


class _Phase2World(GridWorld):
    def __init__(self, *, pos=(0.5, 64.0, 0.5), blocks=None, items=None):
        super().__init__(pos=list(pos), blocks=blocks or {}, items=items or {})
        self.st.update(dimension="overworld", ready=True, alive=True, on_ground=True, health=20.0, food=20.0)
        self.equipped: dict[str, str] = {}
        self.containers: dict[tuple[int, int, int], dict[str, int]] = {}

    def find_blocks(self, types=None, radius=32, limit=32, **kwargs):
        wanted = {bare(t) for t in (types or [])}
        px, py, pz = self.st["pos"]
        hits = []
        for (bx, by, bz), name in self.blocks.items():
            bname = bare(name)
            if wanted and bname not in wanted:
                continue
            d = math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5))
            if d <= float(radius):
                hits.append({"name": bname, "pos": [bx, by, bz], "distance": round(d, 2)})
        hits.sort(key=lambda h: h["distance"])
        return {"blocks": hits[: int(limit)]}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
        dest = list(pos) if pos is not None else [x, y, z]
        cur = list(self.st["pos"])
        if float(cur[1]) < 50.0 and float(dest[1]) >= 50.0 and (float(dest[1]) - float(cur[1])) > 16.0:
            raise ApiError("path_not_found", "must ascend shaft first")
        self.st["pos"] = [float(dest[0]), float(dest[1]), float(dest[2])]
        self.calls.append(("move_to", {"pos": dest, "rng": rng}))
        return {"pos": list(self.st["pos"])}

    def equip(self, item, slot="hand", *, stop_on=None):
        self.equipped[slot] = item
        self.calls.append(("equip", {"item": item, "slot": slot}))
        return {"equipped": item, "slot": slot}

    def craft(self, item, count=1, *, stop_on=None):
        self.calls.append(("craft", {"item": item, "count": count}))
        if item == "iron_boots":
            self.items["iron_ingot"] -= 4
            self.items["iron_boots"] = self.items.get("iron_boots", 0) + 1
        elif item == "shield":
            self.items["iron_ingot"] -= 1
            for k in list(self.items):
                if k.endswith("_planks") and self.items[k] >= 6:
                    self.items[k] -= 6
                    break
            self.items["shield"] = self.items.get("shield", 0) + 1
        return {"crafted": item}

    def window_view(self, target, *, stop_on=None):
        pos = tuple(int(v) for v in target["pos"])
        bag = self.containers.setdefault(pos, {})
        return {"window": "generic_9x3", "sections": {"container": [{"name": k, "count": v} for k, v in bag.items() if v > 0]}}

    def window_act(self, target, ops, *, stop_on=None):
        pos = tuple(int(v) for v in target["pos"])
        bag = self.containers.setdefault(pos, {})
        for op in ops:
            if op["op"] == "put":
                qty = min(int(op["count"]), self.items.get(op["item"], 0))
                self.items[op["item"]] -= qty
                bag[op["item"]] = bag.get(op["item"], 0) + qty
        return self.window_view(target)


class Phase2AuditFixesTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()

    def tearDown(self) -> None:
        reset_all_world_memos()

    def test_craft_iron_gear_auto_equips_armor_and_shield(self) -> None:
        w = _Phase2World(
            blocks={(1, 64, 0): "crafting_table"},
            items={"iron_ingot": 6, "oak_planks": 8},
        )
        ex, st = w.session()
        drive(CraftIronGear(item="iron_boots"), ex, st)
        self.assertEqual(w.equipped.get("feet"), "iron_boots")

        drive(CraftIronGear(item="shield"), ex, st)
        self.assertEqual(w.equipped.get("off-hand"), "shield")

    def test_nether_preflight_always_enables_auto_bunker_even_without_gold_armor(self) -> None:
        st = State(RuntimeConfig())
        st.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "pos": [0.5, 64.0, 0.5], "dimension": "overworld"})
        st.set_inventory({
            "items": [
                {"name": "iron_chestplate", "count": 1, "slot": 0},
                {"name": "shield", "count": 1, "slot": 1},
                {"name": "cooked_beef", "count": 24, "slot": 2},
                {"name": "cobblestone", "count": 96, "slot": 3},
                {"name": "flint_and_steel", "count": 1, "slot": 4},
                {"name": "obsidian", "count": 10, "slot": 5},
            ],
            "empty_slots": 25,
        })
        memo_for(st).set_camp([0, 64, 0], dimension="overworld", chest=[1, 64, 0], stored_items={"iron_sword": 1, "cooked_beef": 16})
        rp = RulePlanner(min_verified="offline")
        rem, _, eff_args = rp._apply_nether_preflight(st, "build_and_enter_portal", {})
        self.assertIsNone(rem)
        self.assertTrue(eff_args.get("auto_bunker"))

    def test_underground_camp_setup_blocked_and_deposit_ascends_mine_shaft(self) -> None:
        # 1) 地下 Y=32 不自动触发 Tier 0 建营
        st_deep = State(RuntimeConfig())
        st_deep.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "pos": [0.5, 32.0, 0.5], "dimension": "overworld", "is_day": True})
        st_deep.set_inventory({
            "items": [
                {"name": "stone_pickaxe", "count": 1, "slot": 0},
                {"name": "furnace", "count": 1, "slot": 1},
                {"name": "oak_log", "count": 4, "slot": 2},
                {"name": "cooked_beef", "count": 8, "slot": 3},
            ],
            "empty_slots": 30,
        })
        dec = RulePlanner(min_verified="offline").choose_proactive_survival(st_deep)
        self.assertTrue(dec is None or dec.skill.name != "manage_camp_chest")

        # 2) 地下携带富余铁锭回营地存箱时先沿主矿井返回地表
        w = _Phase2World(
            pos=(0.5, 34.0, 0.5),
            blocks={(0, 33, 0): "stone", (0, 36, 0): "stone", (1, 64, 0): "chest", (1, 63, 0): "grass_block"},
            items={"iron_ingot": 12},
        )
        ex, st = w.session()
        memo = memo_for(st)
        memo.set_mine_shaft([0, 64, 0], dimension="overworld", lowest_y=34, lowest_pos=[0, 34, 0])
        memo.set_camp([0, 64, 0], dimension="overworld", chest=[1, 64, 0])

        steps = drive(ManageCampChest(mode="deposit"), ex, st)
        self.assertTrue(any(s.get("reason") == "return_primary_mine_shaft" for s in steps))
        self.assertGreaterEqual(w.containers[(1, 64, 0)].get("iron_ingot", 0), 8)


if __name__ == "__main__":
    unittest.main()
