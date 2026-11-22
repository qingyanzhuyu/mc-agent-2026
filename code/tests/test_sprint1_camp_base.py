"""Sprint 1 营地存储、补给、受控捡尸与 WorldResourceMemo 离线单测（verified: offline）。"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import tempfile
import unittest

from mcagent.client import ApiError
from mcagent.runtime import Replan, State
from mcagent.skills import REGISTRY, contract_errors, load_all, make
from mcagent.skills.base import FORBIDDEN_PARAMS, PARAM_TYPES, _COOLDOWN
from mcagent.skills.camp_base import (
    ManageCampChest,
    compute_deposit_plan,
    compute_withdraw_plan,
    has_surplus_for_deposit,
    is_near_camp,
    needs_camp_withdraw,
)
from mcagent.skills.items import bare
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches
from mcagent.skills.world_memo import (
    WorldResourceMemo,
    format_memo_summary,
    memo_for,
    reset_all_world_memos,
)

_COORD_TRIPLET_RE = re.compile(r"-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?")
_BRACKETED_COORD_RE = re.compile(r"[\[(]\s*-?\d+[^\]\)\n]*-?\d+[^\]\)\n]*-?\d+\s*[\])]")


def _make_state(
    *,
    pos=(0.5, 64.0, 0.5),
    dimension="overworld",
    items: dict[str, int] | None = None,
    ready: bool = True,
    alive: bool = True,
) -> State:
    state = State()
    state.set_status(
        {
            "ready": ready,
            "alive": alive,
            "health": 20,
            "food": 20,
            "pos": [float(v) for v in pos],
            "dimension": dimension,
            "on_ground": True,
            "sneaking": False,
        }
    )
    state.set_inventory(
        {
            "items": [
                {"name": name, "count": count, "slot": idx}
                for idx, (name, count) in enumerate((items or {}).items())
                if count > 0
            ]
        }
    )
    return state


class CampGridWorld(GridWorld):
    """支持营地建箱、容器存取、合成与掉落物自动拾取的离线网格世界替身。"""

    def __init__(
        self,
        *,
        pos=(0.5, 64.0, 0.5),
        dimension="overworld",
        blocks: dict[tuple[int, int, int], str] | None = None,
        items: dict[str, int] | None = None,
        containers: dict[tuple[int, int, int], dict[str, int]] | None = None,
        drops: list[dict] | None = None,
    ):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st["dimension"] = dimension
        self.containers: dict[tuple[int, int, int], dict[str, int]] = {
            tuple(k): dict(v) for k, v in (containers or {}).items()
        }
        self.drops: list[dict] = [dict(d) for d in (drops or [])]

    def _pickup_nearby_drops(self, max_dist: float = 2.5) -> None:
        cur = self.st["pos"]
        remaining: list[dict] = []
        for drop in self.drops:
            dpos = drop.get("pos")
            if dpos is not None and math.dist(cur, dpos) <= max_dist:
                item_raw = str(drop.get("item", ""))
                name, sep, qty_str = item_raw.rpartition("x")
                if sep and qty_str.isascii() and qty_str.isdigit() and int(qty_str) > 0:
                    item_name = bare(name)
                    qty = int(qty_str)
                else:
                    item_name = bare(item_raw)
                    qty = int(drop.get("count", 1) or 1)
                if item_name and qty > 0:
                    self.items[item_name] = self.items.get(item_name, 0) + qty
            else:
                remaining.append(drop)
        self.drops = remaining

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None) -> dict:
        self.calls.append(("find_blocks", {"types": types, "radius": radius, "limit": limit}))
        cx, cy, cz = self.st["pos"]
        allowed = {bare(t) for t in types} if types else None
        hits: list[dict] = []
        for (bx, by, bz), name in sorted(self.blocks.items()):
            bname = bare(name)
            if bname == "air":
                continue
            if allowed is not None and bname not in allowed:
                continue
            dist = math.dist((cx, cy, cz), (bx + 0.5, by + 0.5, bz + 0.5))
            if dist <= radius:
                hits.append({"name": bname, "pos": [bx, by, bz], "dist": round(dist, 2)})
        hits.sort(key=lambda b: b["dist"])
        return {"total": len(hits), "blocks": hits[:limit]}

    def get_entities(self, radius=32, types=None) -> dict:
        self.calls.append(("get_entities", {"radius": radius, "types": types}))
        cur = self.st["pos"]
        wanted = {bare(t) for t in ([types] if isinstance(types, str) else types or ())}
        out: list[dict] = []
        for ent in self.drops:
            dpos = ent.get("pos")
            if dpos is None or math.dist(cur, dpos) > radius:
                continue
            ename = bare(ent.get("name"))
            etype = bare(ent.get("type"))
            if wanted and ename not in wanted and etype not in wanted:
                continue
            out.append(dict(ent))
        return {"entities": out}

    def place(self, item, pos=None, *, stop_on=None) -> dict:
        res = super().place(item, pos=pos, stop_on=stop_on)
        if bare(item) in ("chest", "trapped_chest", "barrel") and pos is not None:
            self.containers.setdefault(tuple(int(v) for v in pos), {})
        return res

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        res = super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)
        self._pickup_nearby_drops()
        return res

    def _consume_any(self, candidates: tuple[str, ...], needed: int) -> None:
        total = sum(self.items.get(c, 0) for c in candidates)
        if total < needed:
            raise ApiError("missing_item", f"缺少合成材料: {candidates} x{needed}")
        rem = needed
        for c in candidates:
            have = self.items.get(c, 0)
            if have <= 0:
                continue
            used = min(have, rem)
            self.items[c] = have - used
            if self.items[c] <= 0:
                self.items.pop(c, None)
            rem -= used
            if rem == 0:
                break

    def _has_nearby_table(self) -> bool:
        cx, cy, cz = self.st["pos"]
        eye = (cx, cy + 1.62, cz)
        return any(
            bare(name) == "crafting_table" and math.dist(eye, (bx + 0.5, by + 0.5, bz + 0.5)) <= 3.5
            for (bx, by, bz), name in self.blocks.items()
        )

    def craft(self, item, count=1, *, stop_on=None) -> dict:
        self.calls.append(("craft", {"item": item, "count": count}))
        target = bare(item)
        planks = ("oak_planks", "spruce_planks", "birch_planks", "jungle_planks", "acacia_planks", "dark_oak_planks")
        for _ in range(count):
            if target.endswith("_planks"):
                log_name = target.removesuffix("_planks") + "_log"
                self._consume_any((log_name,), 1)
                self.items[target] = self.items.get(target, 0) + 4
            elif target == "crafting_table":
                self._consume_any(planks, 4)
                self.items["crafting_table"] = self.items.get("crafting_table", 0) + 1
            elif target == "chest":
                if not self._has_nearby_table():
                    raise ApiError("no_window", "合成 chest 需要附近有工作台")
                self._consume_any(planks, 8)
                self.items["chest"] = self.items.get("chest", 0) + 1
            else:
                raise ApiError("unsupported_recipe", f"未知离线合成配方: {target}")
        return {"crafted": target, "count": count}

    def _container_cell(self, target: dict) -> tuple[int, int, int]:
        pos = target.get("pos") if isinstance(target, dict) else None
        if not pos or len(pos) != 3:
            raise ApiError("invalid_target", "缺少容器坐标")
        cell = (int(pos[0]), int(pos[1]), int(pos[2]))
        if bare(self.blocks.get(cell, "air")) not in ("chest", "trapped_chest", "barrel"):
            raise ApiError("not_found", f"位置 {cell} 不是容器")
        return cell

    def window_view(self, target, *, stop_on=None) -> dict:
        self.calls.append(("window_view", {"target": target}))
        cell = self._container_cell(target)
        stored = self.containers.setdefault(cell, {})
        container_entries = [
            {"slot": idx, "name": name, "count": qty}
            for idx, (name, qty) in enumerate(sorted(stored.items()))
            if qty > 0
        ]
        return {
            "window": "generic_9x3",
            "target": bare(self.blocks.get(cell, "chest")),
            "sections": {"container": container_entries},
        }

    def window_act(self, target, ops, *, stop_on=None) -> dict:
        self.calls.append(("window_act", {"target": target, "ops": ops}))
        cell = self._container_cell(target)
        stored = self.containers.setdefault(cell, {})
        done: list[dict] = []
        for op in ops:
            kind = op.get("op")
            item = bare(op.get("item"))
            qty = int(op.get("count", 0) or 0)
            if kind == "put" and op.get("to") == "container":
                have = self.items.get(item, 0)
                if have < qty or qty <= 0:
                    raise ApiError("missing_item", f"背包 {item} 不足 {qty}")
                self.items[item] = have - qty
                if self.items[item] <= 0:
                    self.items.pop(item, None)
                stored[item] = stored.get(item, 0) + qty
                done.append({"op": "put", "item": item, "count": qty, "to": "container"})
            elif kind == "take" and op.get("from") == "container":
                have = stored.get(item, 0)
                if have < qty or qty <= 0:
                    raise ApiError("missing_item", f"箱内 {item} 不足 {qty}")
                stored[item] = have - qty
                if stored[item] <= 0:
                    stored.pop(item, None)
                self.items[item] = self.items.get(item, 0) + qty
                done.append({"op": "take", "item": item, "count": qty, "from": "container"})
            else:
                raise ApiError("invalid_op", f"不支持的窗口操作: {op!r}")
        return {"ok": True, "done": done}


class WorldMemoCampAndDeathTests(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()
        reset_all_voxel_caches()
        self.addCleanup(reset_all_world_memos)
        self.addCleanup(reset_all_voxel_caches)
        self.now = [1000.0]
        self.memo = WorldResourceMemo(clock=lambda: self.now[0])

    def test_camp_crud_normalization_station_matching_and_defensive_copies(self):
        self.assertIsNone(self.memo.get_camp())
        self.assertFalse(self.memo.camp_has_recovery_kit())

        raw_stored = {"minecraft:iron_pickaxe": 1, "cooked_beef": 4}
        created = self.memo.set_camp(
            [10.8, 64.0, -3.2],
            chest=[11.9, 64.0, -3.9],
            bed=[12.1, 64.0, -2.5],
            furnace=[9.2, 64.0, -3.1],
            crafting_table=[10.0, 64.0, -4.5],
            stored_items=raw_stored,
        )
        self.assertEqual(created["pos"], [10, 64, -4])
        self.assertEqual(created["chest"], [11, 64, -4])
        self.assertEqual(created["bed"], [12, 64, -3])
        self.assertEqual(created["furnace"], [9, 64, -4])
        self.assertEqual(created["crafting_table"], [10, 64, -5])
        self.assertEqual(created["stored_items"], {"iron_pickaxe": 1, "cooked_beef": 4})

        # 外部修改入参或返回值不得污染台账内部状态
        raw_stored["diamond"] = 99
        created["pos"][0] = 999
        created["chest"][0] = 888
        created["stored_items"]["iron_pickaxe"] = 77

        snap = self.memo.get_camp()
        assert snap is not None
        self.assertEqual(snap["pos"], [10, 64, -4])
        self.assertEqual(snap["chest"], [11, 64, -4])
        self.assertEqual(snap["stored_items"], {"iron_pickaxe": 1, "cooked_beef": 4})
        snap["stored_items"]["cooked_beef"] = 0
        self.assertEqual(self.memo.get_camp()["stored_items"]["cooked_beef"], 4)

        # 自动登记四类营地工作站
        self.assertEqual(self.memo.stations("chest"), [(11, 64, -4)])
        self.assertEqual(self.memo.stations("bed"), [(12, 64, -3)])
        self.assertEqual(self.memo.stations("furnace"), [(9, 64, -4)])
        self.assertEqual(self.memo.stations("crafting_table"), [(10, 64, -5)])

        # 省略字段在 set_camp 更新时必须保留
        updated = self.memo.set_camp([15.2, 65.0, -1.1], furnace=[16, 65, -1])
        self.assertEqual(updated["pos"], [15, 65, -2])
        self.assertEqual(updated["chest"], [11, 64, -4])
        self.assertEqual(updated["bed"], [12, 64, -3])
        self.assertEqual(updated["furnace"], [16, 65, -1])
        self.assertEqual(updated["crafting_table"], [10, 64, -5])
        self.assertEqual(updated["stored_items"], {"iron_pickaxe": 1, "cooked_beef": 4})

        # stations("chest") 兼容 chest / trapped_chest / barrel
        self.memo.record_station("trapped_chest", [30, 64, 30])
        self.memo.record_station("minecraft:barrel", [35, 64, 35])
        self.assertEqual(
            set(self.memo.stations("chest")),
            {(11, 64, -4), (30, 64, 30), (35, 64, 35)},
        )

        # update_camp_stored 与 camp_has_recovery_kit
        self.assertTrue(self.memo.camp_has_recovery_kit())
        self.memo.update_camp_stored({"cobblestone": 64, "diamond": 3, "iron_ingot": 12})
        self.assertFalse(self.memo.camp_has_recovery_kit())
        self.memo.update_camp_stored({"shield": 1})
        self.assertTrue(self.memo.camp_has_recovery_kit())
        self.memo.update_camp_stored({"iron_helmet": 1})
        self.assertTrue(self.memo.camp_has_recovery_kit())
        self.memo.update_camp_stored({"bread": 2})
        self.assertTrue(self.memo.camp_has_recovery_kit())

        # 无 chest 时 camp_has_recovery_kit 必须为 False
        self.memo.set_camp([0, 64, 0], stored_items={"diamond_sword": 1}, dimension="the_end")
        self.assertFalse(self.memo.camp_has_recovery_kit(dimension="the_end"))

        # 跨维度隔离
        self.assertIsNone(self.memo.get_camp(dimension="the_nether"))
        self.assertFalse(self.memo.camp_has_recovery_kit(dimension="the_nether"))
        self.memo.update_camp_stored({"cobblestone": 10}, dimension="the_nether")
        self.assertEqual(self.memo.get_camp(dimension="overworld")["stored_items"], {"bread": 2})

        # clear() 清空营地与死亡记录
        self.memo.record_death([20, 64, 20], cause="zombie", lost_items={"iron_pickaxe": 1})
        self.memo.clear()
        self.assertIsNone(self.memo.get_camp())
        self.assertIsNone(self.memo.get_camp(dimension="the_end"))
        self.assertIsNone(self.memo.get_last_death())
        self.assertEqual(self.memo.stations("chest"), [])

    def test_checkpoint_roundtrip_legacy_compatibility_and_strict_rejection(self):
        self.memo.set_camp(
            [10, 64, 20],
            chest=[11, 64, 20],
            bed=[12, 64, 20],
            furnace=[13, 64, 20],
            crafting_table=[14, 64, 20],
            stored_items={"iron_pickaxe": 1, "cooked_beef": 8},
        )
        self.memo.set_camp([-5, 70, 5], chest=[-4, 70, 5], stored_items={"obsidian": 4}, dimension="the_nether")
        self.memo.record_death([40, 64, -10], cause="zombie", lost_items={"diamond": 2, "shield": 1}, now=950.0)
        self.memo.record_death([8, 60, 8], cause="blaze", lost_items={"blaze_rod": 3}, dimension="the_nether", now=990.0)

        with tempfile.TemporaryDirectory() as tmp:
            ckpt = Path(tmp) / "camp_ckpt.json"
            self.memo.save_checkpoint(ckpt)

            restored = WorldResourceMemo(clock=lambda: self.now[0])
            self.assertTrue(restored.load_checkpoint(ckpt))
            self.assertEqual(restored.get_camp(dimension="overworld"), self.memo.get_camp(dimension="overworld"))
            self.assertEqual(restored.get_camp(dimension="the_nether"), self.memo.get_camp(dimension="the_nether"))
            self.assertEqual(restored.get_last_death(dimension="overworld"), self.memo.get_last_death(dimension="overworld"))
            self.assertEqual(restored.get_last_death(dimension="the_nether"), self.memo.get_last_death(dimension="the_nether"))
            self.assertTrue(restored.camp_has_recovery_kit(dimension="overworld"))
            self.assertFalse(restored.camp_has_recovery_kit(dimension="the_nether"))

            # 向后兼容缺少 camps / last_deaths 的旧版 schema_version=1 检查点
            legacy_payload = {
                "schema_version": 1,
                "landmark_version": 2,
                "stations": {"overworld": {"crafting_table": [[1, 64, 1]]}},
                "landmarks": {},
                "portals": {},
                "resources": [],
            }
            legacy_path = Path(tmp) / "legacy.json"
            legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")
            legacy_memo = WorldResourceMemo()
            self.assertTrue(legacy_memo.load_checkpoint(legacy_path))
            self.assertEqual(legacy_memo.stations("crafting_table"), [(1, 64, 1)])
            self.assertIsNone(legacy_memo.get_camp())
            self.assertIsNone(legacy_memo.get_last_death())

            # 严格拒绝损坏的 camps / last_deaths，且不破坏现有 memo 状态
            base_payload = json.loads(ckpt.read_text(encoding="utf-8"))
            malformed_cases = {
                "camps_not_dict": {**base_payload, "camps": []},
                "camp_entry_not_dict": {**base_payload, "camps": {"overworld": "bad"}},
                "camp_bad_pos": {**base_payload, "camps": {"overworld": {"pos": [1, 2], "stored_items": {}}}},
                "camp_bad_station": {**base_payload, "camps": {"overworld": {"pos": [1, 64, 1], "chest": [1.5, 64, 1], "stored_items": {}}}},
                "camp_bad_stored_type": {**base_payload, "camps": {"overworld": {"pos": [1, 64, 1], "stored_items": []}}},
                "camp_bad_stored_bool": {**base_payload, "camps": {"overworld": {"pos": [1, 64, 1], "stored_items": {"diamond": True}}}},
                "camp_bad_stored_zero": {**base_payload, "camps": {"overworld": {"pos": [1, 64, 1], "stored_items": {"diamond": 0}}}},
                "deaths_not_dict": {**base_payload, "last_deaths": []},
                "death_entry_not_dict": {**base_payload, "last_deaths": {"overworld": []}},
                "death_bad_pos": {**base_payload, "last_deaths": {"overworld": {"pos": [1, True, 3], "cause": "fall", "lost_items": {}, "recorded_at": 1.0}}},
                "death_bad_cause": {**base_payload, "last_deaths": {"overworld": {"pos": [1, 64, 3], "cause": 123, "lost_items": {}, "recorded_at": 1.0}}},
                "death_bad_lost_items": {**base_payload, "last_deaths": {"overworld": {"pos": [1, 64, 3], "cause": "fall", "lost_items": {"diamond": -1}, "recorded_at": 1.0}}},
                "death_missing_recorded_at": {**base_payload, "last_deaths": {"overworld": {"pos": [1, 64, 3], "cause": "fall", "lost_items": {}}}},
                "death_bool_recorded_at": {**base_payload, "last_deaths": {"overworld": {"pos": [1, 64, 3], "cause": "fall", "lost_items": {}, "recorded_at": False}}},
            }
            snapshot_camp = restored.get_camp(dimension="overworld")
            snapshot_death = restored.get_last_death(dimension="overworld")
            for name, payload in malformed_cases.items():
                bad_path = Path(tmp) / f"{name}.json"
                bad_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(case=name):
                    self.assertFalse(restored.load_checkpoint(bad_path))
                    self.assertEqual(restored.get_camp(dimension="overworld"), snapshot_camp)
                    self.assertEqual(restored.get_last_death(dimension="overworld"), snapshot_death)

    def test_j17_zero_coordinates_and_camp_context_summary_fields(self):
        state = _make_state(pos=(0.5, 64.0, 0.5), dimension="overworld")
        memo = memo_for(state)

        # 无营地时省略营地字段
        empty_ctx = memo.summary_for_context(dimension="overworld")
        for key in ("has_camp", "camp_has_chest", "camp_has_recovery_kit", "camp_stored_count"):
            self.assertNotIn(key, empty_ctx)

        # 写入含大数字坐标的营地、死亡与资源记录
        memo.set_camp(
            [1234, 167, -8910],
            chest=[2345, 168, -7654],
            bed=[3456, 169, -6543],
            furnace=[4567, 170, -5432],
            crafting_table=[5678, 171, -4321],
            stored_items={"iron_pickaxe": 2, "cooked_beef": 10},
        )
        memo.record_death([6789, 172, -3210], cause="creeper", lost_items={"diamond": 3})
        memo.record_seen("wood", "oak_log", [7890, 173, -2109], dist=18.0)

        ctx = memo.summary_for_context(dimension="overworld")
        self.assertTrue(ctx["has_camp"])
        self.assertTrue(ctx["camp_has_chest"])
        self.assertTrue(ctx["camp_has_recovery_kit"])
        self.assertEqual(ctx["camp_stored_count"], 12)

        # 其他维度仍无营地字段
        nether_ctx = memo.summary_for_context(dimension="the_nether")
        for key in ("has_camp", "camp_has_chest", "camp_has_recovery_kit", "camp_stored_count"):
            self.assertNotIn(key, nether_ctx)

        # chest=None 时 camp_has_chest / camp_has_recovery_kit 为 False
        memo.set_camp([9876, 165, -1987], stored_items={"diamond_sword": 1}, dimension="the_nether")
        nether_ctx2 = memo.summary_for_context(dimension="the_nether")
        self.assertTrue(nether_ctx2["has_camp"])
        self.assertFalse(nether_ctx2["camp_has_chest"])
        self.assertFalse(nether_ctx2["camp_has_recovery_kit"])
        self.assertEqual(nether_ctx2["camp_stored_count"], 1)

        summ = memo.summarize(dimension="overworld", origin=[0.5, 64.0, 0.5])
        fmt = format_memo_summary(state)
        ctx_json = json.dumps(ctx, ensure_ascii=False)
        nether_json = json.dumps(nether_ctx2, ensure_ascii=False)

        forbidden_nums = (
            "1234", "167", "-8910", "2345", "168", "-7654",
            "3456", "169", "-6543", "4567", "170", "-5432",
            "5678", "171", "-4321", "6789", "172", "-3210",
            "7890", "173", "-2109", "9876", "165", "-1987",
        )
        for text in (summ, fmt, ctx_json, nether_json):
            self.assertIsNone(_COORD_TRIPLET_RE.search(text))
            self.assertIsNone(_BRACKETED_COORD_RE.search(text))
            for num in forbidden_nums:
                self.assertIsNone(re.search(rf"(?<![-\d]){re.escape(num)}(?!\d)", text), f"{num!r} leaked in {text!r}")


class ControlledCorpseRecoveryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.memo = WorldResourceMemo(clock=lambda: self.now[0])

    def test_no_death_record_and_clear_last_death(self):
        ok, reason = self.memo.should_attempt_corpse_recovery([0.0, 64.0, 0.0])
        self.assertFalse(ok)
        self.assertEqual(reason, "no_death_record")

        self.memo.record_death([10, 64, 0], cause="zombie", lost_items={"iron_pickaxe": 1})
        self.memo.record_death([10, 64, 0], cause="zombie", lost_items={"iron_pickaxe": 1}, dimension="the_nether")
        self.memo.clear_last_death(dimension="overworld")

        self.assertIsNone(self.memo.get_last_death(dimension="overworld"))
        self.assertIsNotNone(self.memo.get_last_death(dimension="the_nether"))
        ok2, reason2 = self.memo.should_attempt_corpse_recovery([0.0, 64.0, 0.0], dimension="overworld")
        self.assertFalse(ok2)
        self.assertEqual(reason2, "no_death_record")

    def test_unrecoverable_causes_rejected(self):
        for cause in ("lava", "void", "out_of_world", "fell_out_of_world", "in_fire", "on_fire", "magma", "fire"):
            with self.subTest(cause=cause):
                self.memo.record_death([10, 64, 0], cause=cause, lost_items={"diamond": 3}, now=1000.0)
                ok, reason = self.memo.should_attempt_corpse_recovery([0.0, 64.0, 0.0], now=1010.0)
                self.assertFalse(ok)
                self.assertEqual(reason, "unrecoverable_cause")

    def test_distance_and_age_boundaries(self):
        self.memo.record_death([80, 64, 0], cause="fall", lost_items={"diamond": 1}, now=1000.0)
        # 精确 80.0 格边界拒绝
        ok_exact, reason_exact = self.memo.should_attempt_corpse_recovery([0.0, 64.0, 0.0], now=1010.0)
        self.assertFalse(ok_exact)
        self.assertEqual(reason_exact, "too_far")

        # 小于 80.0 格允许
        ok_near, reason_near = self.memo.should_attempt_corpse_recovery([0.1, 64.0, 0.0], now=1010.0)
        self.assertTrue(ok_near)
        self.assertEqual(reason_near, "eligible")

        # 精确 240.0 秒未超限，大于 240.0 秒过期拒绝
        ok_240, reason_240 = self.memo.should_attempt_corpse_recovery([50.0, 64.0, 0.0], now=1240.0)
        self.assertTrue(ok_240)
        self.assertEqual(reason_240, "eligible")

        ok_exp, reason_exp = self.memo.should_attempt_corpse_recovery([50.0, 64.0, 0.0], now=1240.01)
        self.assertFalse(ok_exp)
        self.assertEqual(reason_exp, "expired")

    def test_low_value_rejected_and_high_value_eligible(self):
        self.memo.record_death(
            [12, 64, 0],
            cause="zombie",
            lost_items={"oak_log": 16, "cobblestone": 32, "dirt": 20, "wooden_pickaxe": 1, "stone_sword": 1},
            now=1000.0,
        )
        ok_low, reason_low = self.memo.should_attempt_corpse_recovery([0.0, 64.0, 0.0], now=1030.0)
        self.assertFalse(ok_low)
        self.assertEqual(reason_low, "no_valuable_items")

        for item in ("iron_pickaxe", "diamond", "shield", "obsidian", "water_bucket", "ender_pearl", "blaze_rod"):
            for cause in ("zombie", "fall", "creeper"):
                with self.subTest(item=item, cause=cause):
                    self.memo.record_death([12, 64, 0], cause=cause, lost_items={item: 1}, now=1000.0)
                    ok, reason = self.memo.should_attempt_corpse_recovery([0.0, 64.0, 0.0], now=1060.0)
                    self.assertTrue(ok)
                    self.assertEqual(reason, "eligible")


class CampPureHelpersTests(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()
        self.addCleanup(reset_all_world_memos)

    def test_compute_deposit_plan_enforces_exact_quotas(self):
        state = _make_state(
            items={
                "diamond_pickaxe": 2,
                "iron_pickaxe": 1,
                "wooden_pickaxe": 1,
                "iron_sword": 2,
                "stone_sword": 1,
                "shield": 3,
                "diamond_helmet": 1,
                "iron_helmet": 2,
                "iron_chestplate": 2,
                "golden_carrot": 8,
                "cooked_beef": 7,
                "bread": 5,
                "iron_ingot": 10,
                "diamond": 3,
                "obsidian": 4,
            }
        )
        plan = dict(compute_deposit_plan(state))
        self.assertEqual(
            plan,
            {
                "diamond_pickaxe": 1,
                "iron_pickaxe": 1,
                "iron_sword": 1,
                "stone_sword": 1,
                "shield": 2,
                "iron_helmet": 2,
                "iron_chestplate": 1,
                "cooked_beef": 3,
                "bread": 5,
                "iron_ingot": 6,
                "diamond": 3,
                "obsidian": 4,
            },
        )
        self.assertNotIn("wooden_pickaxe", plan)
        self.assertNotIn("diamond_helmet", plan)
        self.assertNotIn("golden_carrot", plan)

        exact_quota_state = _make_state(
            items={
                "iron_pickaxe": 1,
                "iron_sword": 1,
                "shield": 1,
                "iron_chestplate": 1,
                "cooked_beef": 12,
                "iron_ingot": 4,
            }
        )
        self.assertEqual(compute_deposit_plan(exact_quota_state), [])

    def test_has_surplus_for_deposit_thresholds(self):
        self.assertTrue(has_surplus_for_deposit(_make_state(items={"iron_ingot": 8})))
        self.assertTrue(has_surplus_for_deposit(_make_state(items={"diamond": 3})))
        self.assertTrue(has_surplus_for_deposit(_make_state(items={"obsidian": 4})))
        # 总存量 >= 4 且含高价值/装备富余
        self.assertTrue(has_surplus_for_deposit(_make_state(items={"iron_ingot": 7, "iron_pickaxe": 2})))
        # 仅食物富余或少量铁锭不触发存箱
        self.assertFalse(has_surplus_for_deposit(_make_state(items={"cooked_beef": 24})))
        self.assertFalse(has_surplus_for_deposit(_make_state(items={"iron_ingot": 7})))

    def test_compute_withdraw_plan_gear_and_food_rules(self):
        stored = {
            "diamond_pickaxe": 1,
            "iron_pickaxe": 1,
            "wooden_pickaxe": 1,
            "iron_sword": 1,
            "shield": 2,
            "iron_helmet": 1,
            "diamond_chestplate": 1,
            "golden_carrot": 4,
            "cooked_beef": 10,
        }
        # 空身角色取出最优工具/武器/盾/护甲 + 8 份熟食
        plan_empty = dict(compute_withdraw_plan(_make_state(items={}), stored))
        self.assertEqual(
            plan_empty,
            {
                "diamond_pickaxe": 1,
                "iron_sword": 1,
                "shield": 1,
                "iron_helmet": 1,
                "diamond_chestplate": 1,
                "golden_carrot": 4,
                "cooked_beef": 4,
            },
        )

        # 仅有木镐时：若箱内只有木镐则不再多取木镐；若箱内有石以上镐则取之
        only_wood_stored = {"wooden_pickaxe": 2, "bread": 5}
        self.assertEqual(
            compute_withdraw_plan(_make_state(items={}), only_wood_stored),
            [("wooden_pickaxe", 1), ("bread", 5)],
        )
        self.assertEqual(
            compute_withdraw_plan(_make_state(items={"wooden_pickaxe": 1}), only_wood_stored),
            [("bread", 5)],
        )
        self.assertEqual(
            compute_withdraw_plan(_make_state(items={"wooden_pickaxe": 1}), {"iron_pickaxe": 1}),
            [("iron_pickaxe", 1)],
        )

        # 可食用食物 < 6 时按 8 - current_cooked 补齐；>= 6 时跳过食物
        part_food_state = _make_state(items={"cooked_beef": 2, "apple": 3})  # edible=5 < 6, cooked=2 -> need 6
        self.assertEqual(
            compute_withdraw_plan(part_food_state, {"cooked_beef": 10}),
            [("cooked_beef", 6)],
        )
        enough_edible_state = _make_state(items={"apple": 6})  # edible=6 >= 6 -> skip food
        self.assertEqual(compute_withdraw_plan(enough_edible_state, {"cooked_beef": 10}), [])

    def test_needs_camp_withdraw_and_is_near_camp(self):
        state = _make_state(pos=(10.0, 64.0, 0.0), items={})
        self.assertFalse(needs_camp_withdraw(state))
        self.assertFalse(is_near_camp(state))

        memo = memo_for(state)
        memo.set_camp([58, 64, 0], chest=[58, 64, 0], stored_items={"iron_pickaxe": 1})
        self.assertTrue(needs_camp_withdraw(state))
        # 恰好 48.0 格内为 True，超过 48.0 格为 False
        self.assertTrue(is_near_camp(state, max_dist=48.0))
        state.set_status({"pos": [9.9, 64.0, 0.0], "dimension": "overworld", "ready": True, "alive": True})
        self.assertFalse(is_near_camp(state, max_dist=48.0))

        # 维度隔离
        state.set_status({"pos": [58.0, 64.0, 0.0], "dimension": "the_nether", "ready": True, "alive": True})
        self.assertFalse(needs_camp_withdraw(state))
        self.assertFalse(is_near_camp(state, max_dist=48.0))


class ManageCampChestSkillTests(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()
        reset_all_voxel_caches()
        _COOLDOWN.clear()
        self.addCleanup(reset_all_world_memos)
        self.addCleanup(reset_all_voxel_caches)
        self.addCleanup(_COOLDOWN.clear)

    @staticmethod
    def _flat_floor(x0: int = -4, x1: int = 12, z0: int = -4, z1: int = 4) -> dict[tuple[int, int, int], str]:
        return {(x, 63, z): "grass_block" for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)}

    def test_contract_registry_blocked_and_can_start_gates(self):
        loaded = load_all()
        self.assertIn("camp_base", loaded)
        self.assertIn("manage_camp_chest", REGISTRY)
        self.assertEqual(contract_errors(ManageCampChest), [])
        self.assertEqual(ManageCampChest.verified, "live")
        self.assertTrue(set(ManageCampChest.params).isdisjoint(FORBIDDEN_PARAMS))
        for p in ManageCampChest.params.values():
            self.assertIn(p.type, PARAM_TYPES)

        # blocked() 在未就绪/死亡以及空包无营地无捡尸记录时拦截，有箱材/营地箱/可捡尸时放行
        self.assertNotEqual(ManageCampChest.blocked(_make_state()), "")
        self.assertEqual(ManageCampChest.blocked(_make_state(items={"oak_log": 2})), "")
        self.assertEqual(ManageCampChest.blocked(_make_state(items={"chest": 1})), "")
        self.assertNotEqual(ManageCampChest.blocked(_make_state(ready=False)), "")
        self.assertNotEqual(ManageCampChest.blocked(_make_state(alive=False)), "")

        # setup can_start
        self.assertFalse(make("manage_camp_chest", {"mode": "setup"}).can_start(_make_state(items={"oak_log": 1})))
        self.assertTrue(make("manage_camp_chest", {"mode": "setup"}).can_start(_make_state(items={"oak_log": 2})))
        self.assertTrue(make("manage_camp_chest", {"mode": "setup"}).can_start(_make_state(items={"chest": 1})))

        # deposit can_start
        dep = make("manage_camp_chest", {"mode": "deposit"})
        self.assertFalse(dep.can_start(_make_state(items={"oak_log": 2})))  # 无富余物资
        self.assertFalse(dep.can_start(_make_state(items={"diamond": 3})))  # 无箱且无造箱材料
        self.assertTrue(dep.can_start(_make_state(items={"diamond": 3, "oak_log": 2})))

        # withdraw can_start
        wd = make("manage_camp_chest", {"mode": "withdraw"})
        empty_state = _make_state(items={})
        self.assertFalse(wd.can_start(empty_state))
        memo_for(empty_state).set_camp([5, 64, 0], chest=[5, 64, 0], stored_items={"iron_pickaxe": 1})
        self.assertTrue(wd.can_start(empty_state))
        full_state = _make_state(
            items={
                "diamond_pickaxe": 1,
                "iron_sword": 1,
                "shield": 1,
                "iron_helmet": 1,
                "iron_chestplate": 1,
                "iron_leggings": 1,
                "iron_boots": 1,
                "cooked_beef": 8,
            }
        )
        memo_for(full_state).set_camp([5, 64, 0], chest=[5, 64, 0], stored_items={"iron_pickaxe": 1})
        self.assertFalse(wd.can_start(full_state))

        # recover_corpse can_start
        rec_skill = make("manage_camp_chest", {"mode": "recover_corpse"})
        self.assertFalse(rec_skill.can_start(empty_state))
        memo_for(empty_state).record_death([10, 64, 0], cause="zombie", lost_items={"iron_pickaxe": 1})
        self.assertTrue(rec_skill.can_start(empty_state))

    def test_setup_with_nearby_table_and_without_table(self):
        # 场景 A：已有附近工作台，从原木合木板再合箱子并放置（不重复放工作台）
        blocks_with_table = {**self._flat_floor(), (1, 64, 1): "crafting_table"}
        world_a = CampGridWorld(blocks=blocks_with_table, items={"oak_log": 2})
        ex_a, state_a = world_a.session()
        memo_for(state_a).record_station("red_bed", [2, 64, 1])
        skill_a = make("manage_camp_chest", {"mode": "setup"})

        steps_a = drive(skill_a, ex_a, state_a)
        self.assertEqual([s["op"] for s in steps_a], ["craft", "craft", "craft", "place"])
        self.assertEqual(skill_a.verify_done(state_a), "")
        camp_a = memo_for(state_a).get_camp()
        assert camp_a is not None
        self.assertIsNotNone(camp_a["chest"])
        chest_cell = tuple(camp_a["chest"])
        self.assertEqual(world_a.blocks.get(chest_cell), "chest")
        self.assertEqual(camp_a["bed"], [2, 64, 1])
        self.assertIsNone(camp_a["crafting_table"])

        # 场景 B：无附近工作台时自动合成并放置工作台后再合箱子放置
        world_b = CampGridWorld(blocks=self._flat_floor(), items={"oak_log": 3})
        ex_b, state_b = world_b.session()
        skill_b = make("manage_camp_chest", {"mode": "setup"})

        steps_b = drive(skill_b, ex_b, state_b)
        ops_b = [s["op"] for s in steps_b]
        self.assertIn("place", ops_b)
        self.assertEqual(ops_b.count("place"), 2)
        self.assertEqual(skill_b.verify_done(state_b), "")
        camp_b = memo_for(state_b).get_camp()
        assert camp_b is not None
        self.assertIsNotNone(camp_b["chest"])
        self.assertIsNotNone(camp_b["crafting_table"])
        self.assertEqual(world_b.blocks.get(tuple(camp_b["chest"])), "chest")
        self.assertEqual(world_b.blocks.get(tuple(camp_b["crafting_table"])), "crafting_table")

    def test_deposit_end_to_end_preserves_carry_quotas_and_syncs_memo(self):
        blocks = {**self._flat_floor(), (8, 64, 0): "chest"}
        initial_items = {
            "iron_pickaxe": 2,
            "stone_pickaxe": 1,
            "iron_sword": 1,
            "iron_chestplate": 2,
            "cooked_beef": 15,
            "iron_ingot": 10,
            "diamond": 3,
            "obsidian": 4,
        }
        world = CampGridWorld(blocks=blocks, items=initial_items, containers={(8, 64, 0): {}})
        ex, state = world.session()
        before_counts = {k: state.count(k) for k in initial_items}

        memo = memo_for(state)
        memo.set_camp([8, 64, 0], chest=[8, 64, 0])

        skill = make("manage_camp_chest", {"mode": "deposit"})
        self.assertTrue(skill.can_start(state))
        steps = drive(skill, ex, state)
        self.assertEqual(steps[0]["op"], "move_to")
        self.assertIn("window_act", [s["op"] for s in steps])
        self.assertEqual(skill.verify_done(state, before_counts=before_counts), "")

        # 验证身上恰好保留随身配额
        self.assertEqual(state.count("iron_pickaxe"), 1)
        self.assertEqual(state.count("stone_pickaxe"), 0)
        self.assertEqual(state.count("iron_sword"), 1)
        self.assertEqual(state.count("iron_chestplate"), 1)
        self.assertEqual(state.count("cooked_beef"), 12)
        self.assertEqual(state.count("iron_ingot"), 4)
        self.assertEqual(state.count("diamond"), 0)
        self.assertEqual(state.count("obsidian"), 0)

        expected_stored = {
            "iron_pickaxe": 1,
            "stone_pickaxe": 1,
            "iron_chestplate": 1,
            "cooked_beef": 3,
            "iron_ingot": 6,
            "diamond": 3,
            "obsidian": 4,
        }
        self.assertEqual(world.containers[(8, 64, 0)], expected_stored)
        camp = memo.get_camp()
        assert camp is not None
        self.assertEqual(camp["stored_items"], expected_stored)

    def test_withdraw_end_to_end_rearms_respawned_bot_and_syncs_memo(self):
        blocks = {**self._flat_floor(), (6, 64, 0): "chest"}
        chest_stock = {
            "diamond_pickaxe": 1,
            "iron_pickaxe": 1,
            "iron_sword": 1,
            "shield": 1,
            "iron_helmet": 1,
            "iron_chestplate": 1,
            "cooked_beef": 12,
        }
        world = CampGridWorld(blocks=blocks, items={}, containers={(6, 64, 0): chest_stock})
        ex, state = world.session()
        before_counts = {k: 0 for k in chest_stock}

        memo = memo_for(state)
        memo.set_camp([6, 64, 0], chest=[6, 64, 0], stored_items=chest_stock)

        skill = make("manage_camp_chest", {"mode": "withdraw"})
        self.assertTrue(skill.can_start(state))
        steps = drive(skill, ex, state)
        self.assertEqual(steps[0]["op"], "move_to")
        self.assertIn("window_act", [s["op"] for s in steps])
        self.assertEqual(skill.verify_done(state, before_counts=before_counts), "")

        self.assertEqual(state.count("diamond_pickaxe"), 1)
        self.assertEqual(state.count("iron_pickaxe"), 0)
        self.assertEqual(state.count("iron_sword"), 1)
        self.assertEqual(state.count("shield"), 1)
        self.assertEqual(state.count("iron_helmet"), 1)
        self.assertEqual(state.count("iron_chestplate"), 1)
        self.assertEqual(state.count("cooked_beef"), 8)

        expected_left = {"iron_pickaxe": 1, "cooked_beef": 4}
        self.assertEqual(world.containers[(6, 64, 0)], expected_left)
        self.assertEqual(memo.get_camp()["stored_items"], expected_left)

    def test_recover_corpse_collects_drops_clears_death_and_rejects_lava(self):
        drops = [
            {"id": 101, "type": "item", "name": "item", "item": "iron_pickaxe", "count": 1, "pos": [10.5, 64.0, 0.5]},
            {"id": 102, "type": "item", "name": "item", "item": "diamondx2", "pos": [14.5, 64.0, 0.5]},
        ]
        world = CampGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=self._flat_floor(-2, 18, -2, 2),
            items={},
            drops=drops,
        )
        ex, state = world.session()
        memo = memo_for(state)
        memo.record_death([10, 64, 0], cause="zombie", lost_items={"iron_pickaxe": 1, "diamond": 2})

        skill = make("manage_camp_chest", {"mode": "recover_corpse"})
        self.assertTrue(skill.can_start(state))
        steps = drive(skill, ex, state)
        self.assertTrue(all(s["op"] == "move_to" for s in steps))
        self.assertEqual(state.count("iron_pickaxe"), 1)
        self.assertEqual(state.count("diamond"), 2)
        self.assertIsNone(memo.get_last_death())
        self.assertEqual(skill.verify_done(state), "")

        # 岩浆死因直接抛 Replan 拒绝捡尸
        memo.record_death([10, 64, 0], cause="lava", lost_items={"diamond": 3})
        lava_skill = make("manage_camp_chest", {"mode": "recover_corpse"})
        self.assertFalse(lava_skill.can_start(state))
        with self.assertRaises(Replan):
            drive(lava_skill, ex, state)


if __name__ == "__main__":
    unittest.main()
