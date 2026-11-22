"""离线单测：统一工作站台账（记账/销账/作用域/持久化）、四级安全回收与床位 30 格无怪带走策略。"""
from __future__ import annotations

import os
import re
import tempfile
import unittest

import mcagent.loop  # noqa: F401
from mcagent.runtime import State
from mcagent.skills import base as skill_base
from mcagent.skills.field import place_block
from mcagent.skills.furnace_lease import FurnaceLease, lease_book_for
from mcagent.skills.iron import SmeltIron
from mcagent.skills.survival_camp import SleepInBed, is_area_hostile_free
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.workstations import (
    can_reclaim_station,
    find_known_station,
    reclaim_field_station,
    verify_or_prune_station,
)
from mcagent.skills.world_memo import CATEGORY_BLOCKS, format_memo_summary, memo_for, reset_all_world_memos


class _StationWorld(GridWorld):
    def __init__(self, *, pos=(0.5, 64.0, 0.5), blocks=None, items=None, entities=None, is_day=False, tod=15000):
        super().__init__(pos=pos, items=items or {})
        self.st["dimension"] = "overworld"
        self.st["is_day"] = is_day
        self.st["time_of_day"] = tod
        self.st["on_ground"] = True
        self.st["is_sleeping"] = False
        self.blocks: dict[tuple[int, int, int], str] = dict(blocks or {})
        self.entities_list: list[dict] = list(entities or [])
        self.dug: list[tuple[int, int, int]] = []
        self.placed: list[tuple[str, tuple[int, int, int]]] = []
        self.equipped: list[str] = []
        self._events: list[dict] = []

    def get_block(self, pos):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        if key in self.blocks:
            return {"name": self.blocks[key], "pos": list(key), "diggable": True}
        if key[1] <= 63:
            return {"name": "stone", "pos": list(key), "diggable": True}
        return {"name": "air", "pos": list(key), "diggable": False}

    def get_blocks_batch(self, positions, **kw):
        return {(int(p[0]), int(p[1]), int(p[2])): self.get_block(p) for p in positions}

    def advancements(self, **kw):
        return {"completed": []}

    def events(self, since=0, **kw):
        return {"events": [e for e in self._events if e.get("seq", 0) > (since or 0)], "last_seq": len(self._events)}

    def find_blocks(self, types=None, radius=16, limit=8, **kw):
        want = set(types or ())
        out = []
        px, py, pz = self.st["pos"]
        for k, name in self.blocks.items():
            if want and name not in want:
                continue
            d = ((k[0] + 0.5 - px) ** 2 + (k[1] + 0.5 - py) ** 2 + (k[2] + 0.5 - pz) ** 2) ** 0.5
            if d <= radius:
                out.append({"name": name, "pos": list(k), "dist": d})
        out.sort(key=lambda b: b["dist"])
        return {"blocks": out[:limit]}

    def get_entities(self, radius=32):
        return {"entities": list(self.entities_list)}

    def move_to(self, x=None, z=None, *, y=None, pos=None, rng=1, timeout=None, retries=None, stop_on=None, entity_id=None):
        if pos is not None:
            self.st["pos"] = [float(pos[0]), float(pos[1]), float(pos[2])]
        elif x is not None and z is not None:
            self.st["pos"] = [float(x), float(y if y is not None else self.st["pos"][1]), float(z)]
        return {"pos": list(self.st["pos"])}

    def equip(self, item, slot="hand", *, stop_on=None):
        self.equipped.append(item)
        self.st["held"] = {"name": item}
        return {"equipped": item, "slot": slot}

    def place(self, item, pos=None, *, stop_on=None):
        target = tuple(int(v) for v in pos) if pos is not None else (int(self.st["pos"][0]) + 1, int(self.st["pos"][1]), int(self.st["pos"][2]))
        self.blocks[target] = item
        self.items[item] = max(0, self.items.get(item, 0) - 1)
        self.placed.append((item, target))
        return {"placed": item, "pos": list(target)}

    def dig(self, pos, collect=True, *, stop_on=None):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        old = self.blocks.pop(key, "air")
        self.dug.append(key)
        if collect and old != "air":
            self.items[old] = self.items.get(old, 0) + 1
        return {"dug": old, "pos": list(key)}

    def interact(self, *args, **kwargs):
        # 模拟睡眠事件 + 睡醒进入白天（is_sleeping=False）
        self._events.append({"seq": len(self._events) + 1, "type": "sleep"})
        self.st["is_day"] = True
        self.st["time_of_day"] = 100
        self.st["is_sleeping"] = False
        return {"ok": True, "slept": True}


class WorkstationLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        skill_base._COOLDOWN.clear()

    def test_expanded_categories_scope_remove_and_checkpoint_roundtrip(self) -> None:
        for expected in ("brewing_stand", "stonecutter", "chest", "campfire", "respawn_anchor", "bell", "grindstone", "loom"):
            self.assertIn(expected, CATEGORY_BLOCKS["station"])

        st = State()
        memo = memo_for(st)
        memo.set_camp([0, 64, 0], dimension="overworld", crafting_table=[1, 64, 0], furnace=[2, 64, 0])
        self.assertTrue(memo.is_camp_station("crafting_table", [1, 64, 0], dimension="overworld"))

        memo.record_station("smithing_table", [50, 64, 0], dimension="overworld", scope="field")
        memo.record_station("brewing_stand", [52, 64, 0], dimension="overworld")
        self.assertFalse(memo.is_camp_station("smithing_table", [50, 64, 0], dimension="overworld"))
        meta = memo.get_station_meta("smithing_table", [50, 64, 0], dimension="overworld")
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta["scope"], "field")

        # 登记矿道封口元数据一起持久化
        memo.set_mine_shaft([10, 64, 10], dimension="overworld", lowest_y=58, lowest_pos=[16, 58, 10], heading=(1, 0))
        memo.update_mine_shaft(dimension="overworld", seal_cells=[[11, 63, 10], [11, 64, 10]], sealed=True)

        with tempfile.TemporaryDirectory() as tmp:
            ckpt = os.path.join(tmp, "memo.json")
            memo.save_checkpoint(ckpt)
            self.assertTrue(os.path.exists(ckpt))

            st2 = State()
            memo2 = memo_for(st2)
            self.assertTrue(memo2.load_checkpoint(ckpt))
            self.assertTrue(memo2.is_camp_station("crafting_table", [1, 64, 0], dimension="overworld"))
            self.assertIn((50, 64, 0), memo2.stations("smithing_table", dimension="overworld"))
            self.assertIn((52, 64, 0), memo2.stations("brewing_stand", dimension="overworld"))
            shaft2 = memo2.get_mine_shaft(dimension="overworld")
            self.assertIsNotNone(shaft2)
            assert shaft2 is not None
            self.assertTrue(shaft2["sealed"])
            self.assertEqual(shaft2["seal_cells"], [[11, 63, 10], [11, 64, 10]])

            # J17 合规：面向 LLM 摘要不得泄露任何坐标数字三元组
            summary = format_memo_summary(st2)
            self.assertIsNone(re.search(r"\b50\b|\b52\b|\b64\b", summary))

            # 销账接口 remove_station 同时清理列表、元数据与营地绑定
            self.assertTrue(memo2.remove_station("smithing_table", [50, 64, 0], dimension="overworld"))
            self.assertEqual(memo2.stations("smithing_table", dimension="overworld"), [])
            self.assertIsNone(memo2.get_station_meta("smithing_table", [50, 64, 0], dimension="overworld"))
            self.assertTrue(memo2.remove_station("crafting_table", [1, 64, 0], dimension="overworld"))
            self.assertIsNone((memo2.get_camp(dimension="overworld") or {}).get("crafting_table"))

    def test_place_block_auto_records_and_verify_or_prune_cleans_destroyed(self) -> None:
        w = _StationWorld(items={"brewing_stand": 1})
        ex, state = w.session()
        list(place_block(ex, state, "brewing_stand", [1, 64, 0]))
        self.assertIn((1, 64, 0), memo_for(state).stations("brewing_stand"))
        self.assertEqual(find_known_station(ex, state, "brewing_stand", max_dist=16.0), (1, 64, 0))

        # 苦力怕炸毁酿造台 -> 现场核验立即自动销账
        w.blocks.pop((1, 64, 0), None)
        self.assertFalse(verify_or_prune_station(ex, state, "brewing_stand", [1, 64, 0]))
        self.assertEqual(memo_for(state).stations("brewing_stand"), [])
        self.assertIsNone(find_known_station(ex, state, "brewing_stand", max_dist=16.0))


class TieredReclamationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        skill_base._COOLDOWN.clear()

    def test_tier1_field_crafting_table_reclaimed_but_reused_or_camp_kept(self) -> None:
        w = _StationWorld(blocks={(1, 64, 0): "crafting_table"})
        ex, state = w.session()
        memo_for(state).record_station("crafting_table", [1, 64, 0], scope="field")

        # 复用已有工作台不自动拆
        ok_reuse, _, _ = can_reclaim_station(state, "crafting_table", [1, 64, 0], newly_placed=False)
        self.assertFalse(ok_reuse)

        # 野外新放立即回收并销账
        gen = reclaim_field_station(ex, state, "crafting_table", [1, 64, 0], newly_placed=True)
        steps = []
        try:
            while True:
                steps.append(next(gen))
        except StopIteration as stop:
            reclaimed = stop.value
        self.assertTrue(reclaimed)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "dig")
        self.assertEqual(memo_for(state).stations("crafting_table"), [])
        self.assertEqual(w.items.get("crafting_table", 0), 1)

        # 基地 camp 工作站永不自动拆
        w2 = _StationWorld(blocks={(1, 64, 0): "crafting_table"})
        ex2, state2 = w2.session()
        memo_for(state2).set_camp([0, 64, 0], crafting_table=[1, 64, 0])
        ok_camp, _, _ = can_reclaim_station(state2, "crafting_table", [1, 64, 0], newly_placed=True)
        self.assertFalse(ok_camp)

    def test_tier2_furnace_requires_pickaxe_empty_and_no_active_lease(self) -> None:
        # 无镐禁止徒手挖碎熔炉
        w_nopick = _StationWorld(blocks={(1, 64, 0): "furnace"}, items={})
        _, st_nopick = w_nopick.session()
        self.assertFalse(can_reclaim_station(st_nopick, "furnace", [1, 64, 0], newly_placed=True, furnace_emptied=True)[0])

        # 有石镐但炉内未清空 -> 不拆
        w_pick = _StationWorld(blocks={(1, 64, 0): "furnace"}, items={"stone_pickaxe": 1})
        ex, st_pick = w_pick.session()
        memo_for(st_pick).record_station("furnace", [1, 64, 0], scope="field")
        self.assertFalse(can_reclaim_station(st_pick, "furnace", [1, 64, 0], newly_placed=True, furnace_emptied=False)[0])

        # 有活跃 FurnaceLease -> 不拆
        lease_book_for(st_pick).register_lease(
            (1, 64, 0),
            dimension="overworld",
            station_kind="furnace",
            input_item="raw_iron",
            count=3,
            expected_output="iron_ingot",
            consumer="smelt_iron",
            started_at=0.0,
            ready_at=9999999999.0,
        )
        self.assertFalse(can_reclaim_station(st_pick, "furnace", [1, 64, 0], newly_placed=True, furnace_emptied=True)[0])

        # 租约完成后有镐清空 -> 自动装备镐回收并销账
        lease_book_for(st_pick).complete_lease((1, 64, 0), dimension="overworld")
        gen = reclaim_field_station(ex, st_pick, "furnace", [1, 64, 0], newly_placed=True, furnace_emptied=True)
        steps = []
        try:
            while True:
                steps.append(next(gen))
        except StopIteration as stop:
            reclaimed = stop.value
        self.assertTrue(reclaimed)
        self.assertEqual([s["op"] for s in steps], ["equip", "dig"])
        self.assertEqual(memo_for(st_pick).stations("furnace"), [])
        self.assertEqual(w_pick.items.get("furnace", 0), 1)

    def test_tier3_enchanting_table_requires_diamond_pick_and_tier4_never_reclaimed(self) -> None:
        w_iron = _StationWorld(items={"iron_pickaxe": 1})
        _, st_iron = w_iron.session()
        self.assertFalse(can_reclaim_station(st_iron, "enchanting_table", [1, 64, 0], newly_placed=True)[0])

        w_dia = _StationWorld(items={"diamond_pickaxe": 1})
        _, st_dia = w_dia.session()
        ok, _, pick = can_reclaim_station(st_dia, "enchanting_table", [1, 64, 0], newly_placed=True)
        self.assertTrue(ok)
        self.assertEqual(pick, "diamond_pickaxe")

        # Tier 4 永不自动拆除
        for blk in ("chest", "barrel", "ender_chest", "campfire", "soul_campfire", "respawn_anchor", "beacon"):
            self.assertFalse(can_reclaim_station(st_dia, blk, [1, 64, 0], newly_placed=True)[0], blk)

    def test_smelt_iron_reuses_ledger_furnace_beyond_radius_16(self) -> None:
        # 熔炉在 24 格处（超出 find_blocks radius=16，但在台账 32 格复用范围内）
        w = _StationWorld(blocks={(24, 64, 0): "furnace"}, items={"stone_pickaxe": 1, "raw_iron": 1, "coal": 1})
        ex, state = w.session()
        memo_for(state).record_station("furnace", [24, 64, 0], dimension="overworld")
        skill = SmeltIron(count=1)
        gen = skill._furnace(ex, state, item="raw_iron")
        steps = []
        try:
            while True:
                steps.append(next(gen))
        except StopIteration as stop:
            fpos = stop.value
        self.assertEqual(fpos, [24, 64, 0])
        self.assertFalse(getattr(skill, "_furnace_newly_placed", True))
        self.assertEqual(steps[0]["op"], "move_to")


class Bed30mHostileFreePickupTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        skill_base._COOLDOWN.clear()

    def test_field_bed_picked_up_when_no_hostiles_within_30m(self) -> None:
        w = _StationWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(1, 64, 0): "white_bed", (2, 64, 0): "white_bed"},
            entities=[{"id": 9, "name": "zombie", "type": "hostile", "dist": 35.0, "pos": [35.5, 64.0, 0.5]}],
            is_day=False,
            tod=15000,
        )
        ex, state = w.session()
        memo_for(state).record_station("bed", [1, 64, 0], scope="field")
        self.assertTrue(is_area_hostile_free(ex, state, radius=30.0))

        steps = drive(SleepInBed(), ex, state)
        dig_steps = [s for s in steps if s.get("op") == "dig" and s.get("reason") == "reclaim_bed_30m_safe"]
        self.assertEqual(len(dig_steps), 1)
        self.assertEqual(w.dug, [(1, 64, 0)])
        self.assertEqual(w.items.get("white_bed", 0), 1)
        self.assertEqual(memo_for(state).stations("bed"), [])

    def test_field_bed_kept_when_hostile_within_30m(self) -> None:
        w = _StationWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(1, 64, 0): "white_bed", (2, 64, 0): "white_bed"},
            entities=[{"id": 9, "name": "skeleton", "type": "hostile", "dist": 22.0, "pos": [22.5, 64.0, 0.5]}],
            is_day=False,
            tod=15000,
        )
        ex, state = w.session()
        memo_for(state).record_station("bed", [1, 64, 0], scope="field")
        self.assertFalse(is_area_hostile_free(ex, state, radius=30.0))

        steps = drive(SleepInBed(), ex, state)
        self.assertFalse(any(s.get("reason") == "reclaim_bed_30m_safe" for s in steps))
        self.assertEqual(w.dug, [])
        self.assertEqual(w.blocks.get((1, 64, 0)), "white_bed")
        self.assertIn((1, 64, 0), memo_for(state).stations("bed"))

    def test_camp_bed_never_picked_up_even_when_30m_clear(self) -> None:
        w = _StationWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(1, 64, 0): "white_bed", (2, 64, 0): "white_bed"},
            entities=[],
            is_day=False,
            tod=15000,
        )
        ex, state = w.session()
        memo_for(state).set_camp([0, 64, 0], bed=[1, 64, 0])

        steps = drive(SleepInBed(), ex, state)
        self.assertFalse(any(s.get("reason") == "reclaim_bed_30m_safe" for s in steps))
        self.assertEqual(w.dug, [])
        self.assertEqual(w.blocks.get((1, 64, 0)), "white_bed")


if __name__ == "__main__":
    unittest.main()
