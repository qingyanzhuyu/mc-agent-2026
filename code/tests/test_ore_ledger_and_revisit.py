"""离线单测：全矿种台账入账、镐级门禁（够镐先挖 / 挖不动留账）、升级镐后回访复采与沿途记矿。"""
from __future__ import annotations

import os
import re
import tempfile
import unittest

import mcagent.loop  # noqa: F401
from mcagent.loop.engine import LoopConfig, MainLoop, _roadside_structure_blocks
from mcagent.loop.policy import CampaignPlanner
from mcagent.loop.starter_skills import MineStone
from mcagent.runtime import State
from mcagent.skills import base as skill_base
from mcagent.skills.diamond import MineDiamond
from mcagent.skills.iron import SmeltIron
from mcagent.skills.mine_shaft import branch_mine_level
from mcagent.skills.ore_ledger import (
    ORE_CATEGORIES,
    can_mine_ore,
    mine_reachable_ores_and_record,
    pickaxe_for_ore,
    query_harvestable_memo_ores,
    record_ore_observations,
)
from mcagent.skills.testing import CountingExecutor, GridWorld, drive
from mcagent.skills.world_memo import CATEGORY_BLOCKS, format_memo_summary, memo_for, reset_all_world_memos


class _OreWorld(GridWorld):
    def __init__(self, *, pos=(0.5, 50.0, 0.5), blocks=None, items=None, find_radius_cap=None, floor_y=49):
        super().__init__(pos=pos, items=items or {})
        self.st.update({
            "dimension": "overworld",
            "on_ground": True,
            "in_water": False,
            "submerged": False,
            "in_lava": False,
            "on_fire": False,
        })
        self.blocks: dict[tuple[int, int, int], str] = dict(blocks or {})
        self.find_radius_cap = find_radius_cap
        self.floor_y = floor_y
        self.dug: list[tuple[int, int, int]] = []
        self.moved: list[list[float]] = []

    def inventory(self, timeout: float = 10) -> dict:
        out = []
        for i, (n, c) in enumerate(self.items.items()):
            if c <= 0:
                continue
            entry = {"name": n, "count": c, "slot": i}
            if n.endswith("_pickaxe"):
                entry["durability"] = [200, 250]
            out.append(entry)
        return {"items": out, "held": dict(self.st["held"]) if isinstance(self.st.get("held"), dict) else None}

    def get_block(self, pos):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        if key in self.blocks:
            name = self.blocks[key]
            return {"name": name, "pos": list(key), "diggable": name not in ("air", "cave_air", "bedrock")}
        if key[1] <= self.floor_y:
            return {"name": "stone", "pos": list(key), "diggable": True}
        return {"name": "air", "pos": list(key), "diggable": False}

    def get_blocks_batch(self, positions, **kw):
        return {(int(p[0]), int(p[1]), int(p[2])): self.get_block(p) for p in positions}

    def find_blocks(self, types=None, radius=16, limit=32, **kw):
        eff_r = min(radius, self.find_radius_cap) if self.find_radius_cap is not None else radius
        want = set(types or ())
        px, py, pz = self.st["pos"]
        out = []
        for k, name in self.blocks.items():
            if want and name not in want:
                continue
            d = ((k[0] + 0.5 - px) ** 2 + (k[1] + 0.5 - py) ** 2 + (k[2] + 0.5 - pz) ** 2) ** 0.5
            if d <= eff_r:
                out.append({"name": name, "pos": list(k), "dist": d})
        out.sort(key=lambda b: b["dist"])
        return {"blocks": out[:limit]}

    def get_entities(self, radius=32, **kw):
        return {"entities": []}

    def equip(self, item, slot="hand", *, stop_on=None):
        held = {"name": item}
        if item.endswith("_pickaxe"):
            held["durability"] = [200, 250]
        self.st["held"] = held
        return {"equipped": item, "slot": slot}

    def move_to(self, x=None, z=None, *, y=None, pos=None, rng=1, timeout=None, retries=None, stop_on=None, entity_id=None):
        if pos is not None:
            target = [float(pos[0]), float(pos[1]), float(pos[2])]
        elif x is not None and z is not None:
            target = [float(x), float(y if y is not None else self.st["pos"][1]), float(z)]
        else:
            target = list(self.st["pos"])
        # 落点站在目标矿旁 1 格处，保证站在实地上且不压脚下
        dest = [target[0] - 1.0 + 0.5, target[1], target[2] + 0.5]
        self.st["pos"] = dest
        self.moved.append(dest)
        return {"pos": list(dest)}

    def dig(self, pos, collect=True, *, stop_on=None):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        old = self.blocks.pop(key, "air")
        self.dug.append(key)
        drop_map = {
            "stone": "cobblestone",
            "coal_ore": "coal",
            "deepslate_coal_ore": "coal",
            "copper_ore": "raw_copper",
            "iron_ore": "raw_iron",
            "deepslate_iron_ore": "raw_iron",
            "lapis_ore": "lapis_lazuli",
            "gold_ore": "raw_gold",
            "redstone_ore": "redstone",
            "diamond_ore": "diamond",
            "deepslate_diamond_ore": "diamond",
            "emerald_ore": "emerald",
        }
        if collect and old in drop_map:
            d = drop_map[old]
            self.items[d] = self.items.get(d, 0) + 1
        return {"dug": old, "pos": list(key)}


class OrePickaxeTierAndLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        skill_base._COOLDOWN.clear()

    def test_all_ore_categories_registered_and_checkpoint_persists_without_j17_leak(self) -> None:
        for cat in ORE_CATEGORIES:
            self.assertIn(cat, CATEGORY_BLOCKS)

        st = State()
        st.st = {"dimension": "overworld", "pos": [0.5, 50.0, 0.5]}
        record_ore_observations(st, [
            {"name": "copper_ore", "pos": [12, 48, 3]},
            {"name": "gold_ore", "pos": [14, 20, -7]},
            {"name": "diamond_ore", "pos": [22, -58, 9]},
            {"name": "emerald_ore", "pos": [30, 80, 5]},
            {"name": "lapis_ore", "pos": [18, 12, 1]},
            {"name": "redstone_ore", "pos": [19, -40, 2]},
        ], dimension="overworld")
        record_ore_observations(st, [
            {"name": "ancient_debris", "pos": [8, 15, 4]},
            {"name": "nether_quartz_ore", "pos": [6, 40, 2]},
        ], dimension="the_nether")

        with tempfile.TemporaryDirectory() as tmp:
            ckpt = os.path.join(tmp, "ores.json")
            memo_for(st).save_checkpoint(ckpt)
            st2 = State()
            self.assertTrue(memo_for(st2).load_checkpoint(ckpt))
            self.assertEqual(memo_for(st2).query("gold_ore", dimension="overworld")[0].pos, (14, 20, -7))
            self.assertEqual(memo_for(st2).query("diamond_ore", dimension="overworld")[0].pos, (22, -58, 9))
            self.assertEqual(memo_for(st2).query("ancient_debris", dimension="the_nether")[0].pos, (8, 15, 4))
            summary = format_memo_summary(st2)
            self.assertIsNone(re.search(r"\b22\b|\b-58\b|\b30\b", summary))

    def test_pickaxe_tier_gate_matches_minecraft_rules(self) -> None:
        def _st(pick):
            s = State()
            s.set_inventory({"items": [{"name": pick, "count": 1, "slot": 0}] if pick else []})
            return s

        st_wood = _st("wooden_pickaxe")
        self.assertEqual(pickaxe_for_ore(st_wood, "coal_ore"), "wooden_pickaxe")
        self.assertEqual(pickaxe_for_ore(st_wood, "copper_ore"), "wooden_pickaxe")
        self.assertIsNone(pickaxe_for_ore(st_wood, "iron_ore"))
        self.assertIsNone(pickaxe_for_ore(st_wood, "lapis_ore"))
        self.assertIsNone(pickaxe_for_ore(st_wood, "diamond_ore"))

        st_stone = _st("stone_pickaxe")
        self.assertEqual(pickaxe_for_ore(st_stone, "iron_ore"), "stone_pickaxe")
        self.assertEqual(pickaxe_for_ore(st_stone, "lapis_ore"), "stone_pickaxe")
        self.assertIsNone(pickaxe_for_ore(st_stone, "gold_ore"))
        self.assertIsNone(pickaxe_for_ore(st_stone, "redstone_ore"))
        self.assertIsNone(pickaxe_for_ore(st_stone, "diamond_ore"))
        self.assertIsNone(pickaxe_for_ore(st_stone, "emerald_ore"))

        st_iron = _st("iron_pickaxe")
        self.assertEqual(pickaxe_for_ore(st_iron, "gold_ore"), "iron_pickaxe")
        self.assertEqual(pickaxe_for_ore(st_iron, "diamond_ore"), "iron_pickaxe")
        self.assertEqual(pickaxe_for_ore(st_iron, "emerald_ore"), "iron_pickaxe")
        self.assertIsNone(pickaxe_for_ore(st_iron, "ancient_debris"))

        st_dia = _st("diamond_pickaxe")
        self.assertTrue(can_mine_ore(st_dia, "ancient_debris"))

    def test_mine_stone_with_wooden_pick_mines_coal_and_records_unmineable_iron_and_diamond(self) -> None:
        w = _OreWorld(
            pos=(0.5, 50.0, 0.5),
            blocks={
                (2, 50, 0): "coal_ore",     # 木镐可挖 -> 应先挖并标记 depleted
                (0, 50, 2): "iron_ore",     # 木镐挖不动 -> 不挖，记入台账
                (-2, 50, 0): "diamond_ore", # 木镐挖不动 -> 不挖，记入台账
                (1, 50, 1): "stone",
            },
            items={"wooden_pickaxe": 1},
        )
        ex, state = w.session()
        drive(MineStone(count=1), ex, state)

        # 煤矿被木镐先挖下来并标记 depleted（query 不再返回已采空矿点，is_blocked 为 True）
        self.assertIn((2, 50, 0), w.dug)
        self.assertEqual(state.count("coal"), 1)
        self.assertTrue(memo_for(state).is_blocked((2, 50, 0), dimension="overworld", category="coal_ore"))
        self.assertEqual(memo_for(state).query("coal_ore", dimension="overworld"), [])

        # 铁矿与钻石矿木镐挖不动 -> 绝不挖碎，完整留存在世界与台账中！
        self.assertNotIn((0, 50, 2), w.dug)
        self.assertNotIn((-2, 50, 0), w.dug)
        self.assertEqual(w.blocks.get((0, 50, 2)), "iron_ore")
        self.assertEqual(w.blocks.get((-2, 50, 0)), "diamond_ore")
        self.assertIn((0, 50, 2), [e.pos for e in memo_for(state).query("iron_ore", dimension="overworld")])
        self.assertIn((-2, 50, 0), [e.pos for e in memo_for(state).query("diamond_ore", dimension="overworld")])

        # 升级石镐后，query_harvestable_memo_ores 立刻能查回此前记下的铁矿，而钻石矿仍因镐级不足暂缓
        w.items["stone_pickaxe"] = 1
        state.invalidate_inventory()
        state.refresh_inventory(w)
        harvestable_iron = query_harvestable_memo_ores(state, ("iron_ore",), dimension="overworld")
        harvestable_dia = query_harvestable_memo_ores(state, ("diamond_ore",), dimension="overworld")
        self.assertEqual(harvestable_iron, [("iron_ore", (0, 50, 2))])
        self.assertEqual(harvestable_dia, [])


class UpgradedPickaxeRevisitTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        skill_base._COOLDOWN.clear()

    def test_smelt_iron_revisits_memo_iron_recorded_while_holding_wooden_pickaxe(self) -> None:
        # 铁矿位于 25 格处，模拟 find_blocks 半径受限未扫到，但之前木镐阶段已记入台账
        w = _OreWorld(
            pos=(0.5, 50.0, 0.5),
            blocks={(25, 50, 0): "iron_ore"},
            items={"stone_pickaxe": 1, "furnace": 1, "coal": 1},
            find_radius_cap=12,
        )
        ex, state = w.session()
        memo_for(state).record_seen("iron_ore", "iron_ore", [25, 50, 0], dimension="overworld", dist=25.0)

        sk = SmeltIron(count=1)
        cands = sk._ore_candidates(ex, state, radius=32)
        self.assertEqual(cands, [[25, 50, 0]])

        # 直接执行 _mine 前往台账铁矿并采下，采完自动标记 depleted
        list(sk._mine(ex, state, [25, 50, 0], ("iron_ore", "deepslate_iron_ore"), "raw_iron"))
        self.assertEqual(state.count("raw_iron"), 1)
        self.assertIn((25, 50, 0), w.dug)
        self.assertTrue(memo_for(state).is_blocked((25, 50, 0), dimension="overworld", category="iron_ore"))
        self.assertEqual(memo_for(state).query("iron_ore", dimension="overworld"), [])

    def test_herringbone_stone_pick_records_diamond_then_iron_pick_mine_diamond_revisits_it(self) -> None:
        # 1) 石镐在鱼骨挖矿时看到附近钻石矿 (0, -58, 4) 与煤矿 (1, -57, 0)：
        #    煤矿可挖先挖；钻石矿挖不动不挖，但记入台账！
        blocks: dict[tuple[int, int, int], str] = {}
        for x in range(-2, 4):
            for y in range(-60, -55):
                for z in range(-4, 6):
                    blocks[(x, y, z)] = "deepslate"
        blocks[(0, -58, 0)] = "air"
        blocks[(0, -57, 0)] = "air"
        blocks[(1, -57, 0)] = "deepslate_coal_ore"
        blocks[(0, -58, 4)] = "deepslate_diamond_ore"

        w = _OreWorld(pos=(0.5, -58.0, 0.5), blocks=blocks, items={"stone_pickaxe": 1})
        ex, state = w.session()
        memo_for(state).set_mine_shaft([0, -58, 0], dimension="overworld", lowest_y=-58, lowest_pos=[0, -58, 0], heading=(1, 0))

        gen = branch_mine_level(
            ex, state, level_y=-58, target_ores=("iron_ore",),
            bonus_ores=("coal_ore", "deepslate_coal_ore"),
            max_length=1, scan_stride=3, scan_radius=12, dimension="overworld",
        )
        for _ in gen:
            pass

        # 煤矿已顺采，钻石矿石镐挖不动绝对没被挖碎，且准确记入台账
        self.assertIn((1, -57, 0), w.dug)
        self.assertNotIn((0, -58, 4), w.dug)
        self.assertEqual(w.blocks.get((0, -58, 4)), "deepslate_diamond_ore")
        self.assertIn((0, -58, 4), [e.pos for e in memo_for(state).query("diamond_ore", dimension="overworld")])

        # 2) 之后升级铁镐，在 40 格处启动 MineDiamond（局部扫描受限未直见该钻）-> 自动查台账回访挖走该钻石并标记 depleted！
        w2 = _OreWorld(
            pos=(40.5, -58.0, 0.5),
            blocks={
                (0, -58, 4): "deepslate_diamond_ore",
                (-1, -59, 4): "deepslate",
                (0, -59, 4): "deepslate",
                (40, -59, 0): "deepslate",
            },
            items={"iron_pickaxe": 1},
            find_radius_cap=12,
            floor_y=-59,
        )
        ex2, state2 = w2.session()
        memo_for(state2).record_seen("diamond_ore", "deepslate_diamond_ore", [0, -58, 4], dimension="overworld", dist=40.0)
        drive(MineDiamond(count=1), ex2, state2)
        self.assertEqual(state2.count("diamond"), 1)
        self.assertIn((0, -58, 4), w2.dug)
        self.assertTrue(memo_for(state2).is_blocked((0, -58, 4), dimension="overworld", category="diamond_ore"))
        self.assertEqual(memo_for(state2).query("diamond_ore", dimension="overworld"), [])


class RoadsideOreLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        skill_base._COOLDOWN.clear()

    def test_roadside_scan_records_exposed_ores_in_single_find_blocks_call(self) -> None:
        overworld_blocks = _roadside_structure_blocks("overworld")
        self.assertIn("iron_ore", overworld_blocks)
        self.assertIn("diamond_ore", overworld_blocks)
        self.assertIn("gold_ore", overworld_blocks)
        self.assertNotIn("glow_berries", overworld_blocks)

        calls: list[dict] = []

        class _RoadsideOreWorld(_OreWorld):
            def find_blocks(self, types=None, radius=16, limit=32, **kw):
                calls.append({"types": list(types or ()), "radius": radius, "limit": limit})
                return {
                    "blocks": [
                        {"name": "hay_block", "pos": [15, 64, 10]},
                        {"name": "iron_ore", "pos": [12, 62, 4]},
                        {"name": "diamond_ore", "pos": [18, -58, 6]},
                    ]
                }

        w = _RoadsideOreWorld(pos=(0.5, 64.0, 0.5))
        state = State()
        state.set_status(w.status())
        ex = CountingExecutor(w, state)
        planner = CampaignPlanner(mode="campaign", min_verified="offline")
        cfg = LoopConfig(enable_monitor=False, enable_roadside_scan=True, roadside_scan_interval_s=0.0)
        loop = MainLoop(w, state, ex, planner, config=cfg, log=lambda *_: None)

        found = loop._maybe_roadside_scan(force=True)
        # 仅 1 次 find_blocks 请求，地标返回不受矿石污染，且铁矿/钻石矿均已自动写入台账
        self.assertEqual(len(calls), 1)
        self.assertEqual(found, ["village"])
        self.assertIn((12, 62, 4), [e.pos for e in memo_for(state).query("iron_ore", dimension="overworld")])
        self.assertIn((18, -58, 6), [e.pos for e in memo_for(state).query("diamond_ore", dimension="overworld")])


if __name__ == "__main__":
    unittest.main()
