"""世界资源台账高级状态/检查点与 EmergencyEscape 技能离线单测（verified: offline）。"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from mcagent.runtime import State
from mcagent.skills import REGISTRY, bind_args, contract_errors, make, rec
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.safety_escape import EmergencyEscape
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.world_memo import WorldResourceMemo, reset_all_world_memos

_COORD_TRIPLET_RE = re.compile(r"-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?")
_BRACKETED_COORD_RE = re.compile(r"[\[(]\s*-?\d+[^\]\)\n]*-?\d+[^\]\)\n]*-?\d+\s*[\])]")


class _UseItemGridWorld(GridWorld):
    """在 GridWorld 基础上补充 `use_item` 动作以离线测试水桶灭火分支。"""

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None) -> dict:
        self.calls.append(("use_item", {"item": item, "target": target}))
        self._need(item)
        return {"used": item}


def _drive_skill_result(skill: EmergencyEscape, ex, state) -> tuple[bool, list[dict]]:
    """驱动 skill.run() 并同时捕获布尔返回值与每步动作记录（每步至多一个阻塞动作）。"""
    box: dict[str, object] = {}

    def _wrapper():
        box["ret"] = yield from skill.run(ex, state)

    steps = drive(_wrapper(), ex)
    return bool(box.get("ret")), [s for s in steps if s is not None]


class WorldMemoStationLandmarkPortalTests(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()
        self.addCleanup(reset_all_world_memos)
        self.now = [100.0]
        self.memo = WorldResourceMemo(clock=lambda: self.now[0])

    def test_station_dedup_matching_and_dimension_isolation(self):
        self.memo.record_station("crafting_table", [0.4, 64.0, 0.4])
        # <= 1.5 格（1.0 与 sqrt(2)≈1.414）去重
        self.memo.record_station("crafting_table", [1.9, 64.0, 0.9])
        self.memo.record_station("crafting_table", [1, 65, 0])
        self.assertEqual(self.memo.stations("crafting_table"), [(0, 64, 0)])

        # > 1.5 格（2.0 与 sqrt(3)≈1.732）保留为独立工作站
        self.memo.record_station("crafting_table", [2, 64, 0])
        self.memo.record_station("crafting_table", [1, 65, 1])
        self.assertEqual(self.memo.stations("crafting_table"), [(0, 64, 0), (2, 64, 0), (1, 65, 1)])

        # bed 兼容所有 *_bed，furnace 兼容 blast_furnace / smoker
        self.memo.record_station("red_bed", [10, 64, 10])
        self.memo.record_station("minecraft:white_bed", [15, 64, 10])
        self.memo.record_station("light_blue_bed", [20, 64, 10])
        self.assertTrue(self.memo.has_station("bed"))
        self.assertTrue(self.memo.has_station("red_bed"))
        self.assertFalse(self.memo.has_station("green_bed"))
        self.assertEqual(self.memo.stations("bed"), [(10, 64, 10), (15, 64, 10), (20, 64, 10)])

        self.memo.record_station("blast_furnace", [30, 64, 0])
        self.memo.record_station("smoker", [35, 64, 0])
        self.assertTrue(self.memo.has_station("furnace"))
        self.assertEqual(self.memo.stations("furnace"), [(30, 64, 0), (35, 64, 0)])

        # stations(None) 返回当前维度所有工作站，且跨维度隔离
        all_ow = self.memo.stations(None)
        self.assertEqual(
            set(all_ow),
            {(0, 64, 0), (2, 64, 0), (1, 65, 1), (10, 64, 10), (15, 64, 10), (20, 64, 10), (30, 64, 0), (35, 64, 0)},
        )
        self.assertFalse(self.memo.has_station("crafting_table", dimension="the_nether"))
        self.assertFalse(self.memo.has_station("bed", dimension="the_nether"))
        self.assertEqual(self.memo.stations(None, dimension="the_nether"), [])

        self.memo.record_station("crafting_table", [0, 64, 0], dimension="the_nether")
        self.assertTrue(self.memo.has_station("crafting_table", dimension="the_nether"))
        self.assertEqual(self.memo.stations(None, dimension="the_nether"), [(0, 64, 0)])

        # 空类型校验
        for bad_kind in ("", "   ", "minecraft:"):
            with self.subTest(bad_kind=bad_kind):
                with self.assertRaises(ValueError):
                    self.memo.record_station(bad_kind, [0, 64, 0])
        self.assertFalse(self.memo.has_station(""))
        self.assertEqual(self.memo.stations(""), [])

    def test_landmark_dedup_version_and_ingest_skipping(self):
        self.assertEqual(self.memo.landmark_version, 0)
        self.assertFalse(self.memo.has_landmark(None))
        self.assertFalse(self.memo.has_landmark("village"))

        self.assertTrue(self.memo.record_landmark("village", [100, 64, 100], meta={"block": "bell"}))
        self.assertEqual(self.memo.landmark_version, 1)

        # 同维度同类型 <= 8.0 格去重，不递增 landmark_version，同时合并 meta
        self.assertFalse(self.memo.record_landmark("village", [108, 64, 100], meta={"confirmed": True}))
        self.assertEqual(self.memo.landmark_version, 1)
        self.assertEqual(self.memo.landmarks("village"), [(100, 64, 100)])
        self.assertEqual(self.memo._landmarks[("overworld", "village")][0][1], {"block": "bell", "confirmed": True})

        # > 8.0 格 / 不同类型 / 不同维度均新增并递增 landmark_version
        self.assertTrue(self.memo.record_landmark("village", [109, 64, 100]))
        self.assertEqual(self.memo.landmark_version, 2)
        self.assertTrue(self.memo.record_landmark("ruined_portal", [100, 64, 100]))
        self.assertEqual(self.memo.landmark_version, 3)
        self.assertTrue(self.memo.record_landmark("village", [100, 64, 100], dimension="the_nether"))
        self.assertEqual(self.memo.landmark_version, 4)

        self.assertTrue(self.memo.has_landmark(None))
        self.assertTrue(self.memo.has_landmark("village"))
        self.assertFalse(self.memo.has_landmark("stronghold"))
        self.assertEqual(self.memo.landmarks("village"), [(100, 64, 100), (109, 64, 100)])
        self.assertEqual(set(self.memo.landmarks(None)), {(100, 64, 100), (109, 64, 100)})
        self.assertEqual(self.memo.landmarks(None, dimension="the_nether"), [(100, 64, 100)])

        with self.assertRaises(ValueError):
            self.memo.record_landmark("   ", [0, 64, 0])
        self.assertFalse(self.memo.has_landmark(""))
        self.assertEqual(self.memo.landmarks(""), [])

        # ingest_find_blocks 跳过 protected / depleted 格子，自动记录干净工作站与结构地标
        self.memo.mark_protected([30, 64, 30])  # 无类型保护
        self.memo.mark_depleted([40, 64, 40], category="station")
        self.memo.mark_protected([50, 64, 50], category="structure")

        self.memo.ingest_find_blocks({
            "blocks": [
                {"name": "crafting_table", "pos": [30, 64, 30], "dist": 5.0},
                {"name": "red_bed", "pos": [40, 64, 40], "dist": 6.0},
                {"name": "hay_block", "pos": [50, 64, 50], "dist": 7.0},
                {"name": "smoker", "pos": [60, 64, 60], "dist": 8.0},
                {"name": "composter", "pos": [200, 64, 200], "dist": 9.0},
            ],
        })
        self.assertEqual(self.memo.stations("furnace"), [(60, 64, 60)])
        self.assertFalse(self.memo.has_station("crafting_table"))
        self.assertFalse(self.memo.has_station("bed"))
        self.assertEqual(self.memo.landmarks("village"), [(100, 64, 100), (109, 64, 100), (200, 64, 200)])
        self.assertEqual(self.memo.landmark_version, 5)

    def test_active_portal_dedup_and_dimension_isolation(self):
        self.assertFalse(self.memo.has_active_portal(dimension="overworld"))
        self.assertFalse(self.memo.has_active_portal(dimension="the_nether"))

        self.memo.record_active_portal([0, 64, 0], dimension="overworld")
        self.memo.record_active_portal([2, 64, 0], dimension="overworld")  # dist == 2.0 <= 2.0 去重
        self.assertTrue(self.memo.has_active_portal(dimension="overworld"))
        self.assertFalse(self.memo.has_active_portal(dimension="the_nether"))
        self.assertEqual(self.memo._portals["overworld"], [(0, 64, 0)])

        self.memo.record_active_portal([3, 64, 0], dimension="overworld")  # dist == 3.0 > 2.0 保留
        self.assertEqual(self.memo._portals["overworld"], [(0, 64, 0), (3, 64, 0)])

        self.memo.record_active_portal([0, 64, 0], dimension="the_nether")
        self.assertTrue(self.memo.has_active_portal(dimension="the_nether"))

    def test_blocked_region_ttl_with_fake_clock_and_isolation(self):
        self.memo.record_blocked_region("gather_wood", (3, -2), ttl_s=30.0)
        self.assertTrue(self.memo.is_region_blocked("gather_wood", (3, -2)))
        self.assertFalse(self.memo.is_region_blocked("mine_ore", (3, -2)))
        self.assertFalse(self.memo.is_region_blocked("gather_wood", (4, -2)))

        self.now[0] = 129.99
        self.assertTrue(self.memo.is_region_blocked("gather_wood", (3, -2)))

        # 到期自动清理并返回 False
        self.now[0] = 130.0
        self.assertFalse(self.memo.is_region_blocked("gather_wood", (3, -2)))
        self.assertNotIn(("gather_wood", (3, -2)), self.memo._blocked_regions)

        # ttl_s <= 0 立即到期
        self.memo.record_blocked_region("gather_wood", (0, 0), ttl_s=0.0)
        self.assertFalse(self.memo.is_region_blocked("gather_wood", (0, 0)))

    def test_summary_for_context_fields_and_j17_sanitization(self):
        self.memo.record_station("red_bed", [1234, 67, -8910])
        self.memo.record_station("crafting_table", [2345, 68, -7654])
        self.memo.record_landmark("village", [-4321, 71, 8765], meta={"entity_id": "ent_9999"})
        self.memo.record_active_portal([5555, 64, -6666])
        self.memo.record_seen("wood", "oak_log", [1234, 67, -8910], dist=14.2)
        self.memo.record_seen("iron_ore", "iron_ore", [3333, 45, -4444], dist=20.0)
        self.memo.record_seen("iron_ore", "iron_ore", [3334, 45, -4445], dist=21.0, status="depleted")
        self.memo.record_seen("lava", "lava", [7777, 11, -8888], dist=25.0, status="protected")
        self.memo.record_seen("structure", "stone_bricks", [9999, 30, -1010], tag="stronghold")
        self.memo.record_station("smoker", [444, 70, -555], dimension="the_nether")

        ctx = self.memo.summary_for_context(dimension="overworld")
        self.assertEqual(
            set(ctx.keys()),
            {"dimension", "stations_ready", "landmarks_discovered", "active_portal", "resource_counts", "landmark_version"},
        )
        self.assertEqual(ctx["dimension"], "overworld")
        self.assertEqual(ctx["stations_ready"], ["bed", "crafting_table"])
        self.assertEqual(ctx["landmarks_discovered"], ["stronghold", "village"])
        self.assertTrue(ctx["active_portal"])
        self.assertEqual(ctx["resource_counts"], {"iron_ore": 1, "wood": 1})
        self.assertEqual(ctx["landmark_version"], 1)

        serialized = json.dumps(ctx, ensure_ascii=False)
        self.assertIsNone(_COORD_TRIPLET_RE.search(serialized))
        self.assertIsNone(_BRACKETED_COORD_RE.search(serialized))
        for forbidden in (
            "1234", "67", "-8910",
            "2345", "68", "-7654",
            "-4321", "71", "8765",
            "5555", "64", "-6666",
            "3333", "45", "-4444",
            "7777", "11", "-8888",
            "9999", "30", "-1010",
            "entity_id", "ent_9999",
        ):
            self.assertNotIn(forbidden, serialized)

        nether_ctx = self.memo.summary_for_context(dimension="the_nether")
        self.assertEqual(nether_ctx["stations_ready"], ["smoker"])
        self.assertEqual(nether_ctx["landmarks_discovered"], [])
        self.assertFalse(nether_ctx["active_portal"])
        self.assertEqual(nether_ctx["resource_counts"], {})


class WorldMemoCheckpointTests(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()
        self.addCleanup(reset_all_world_memos)

    def _populate_memo(self, memo: WorldResourceMemo) -> None:
        memo.record_station("crafting_table", [10, 64, 20])
        memo.record_station("red_bed", [14, 64, 22])
        memo.record_station("blast_furnace", [5, 70, -8], dimension="the_nether")
        memo.record_landmark("village", [100, 64, -200], meta={"block": "bell", "note": "村庄钟声", "beds": 3})
        memo.record_landmark("fortress", [-50, 58, 90], dimension="the_nether", meta={"blaze": True})
        memo.record_active_portal([12, 64, 18], dimension="overworld")
        memo.record_active_portal([2, 60, 3], dimension="the_nether")
        memo.record_seen("wood", "spruce_log", [30, 65, 40], dist=15.5, status="reachable")
        e_unreach = memo.mark_unreachable(
            [35, 64, 42],
            category="iron_ore",
            block="deepslate_iron_ore",
            error_code="no_path",
        )
        self.assertEqual(e_unreach.attempts, 1)
        memo.mark_unreachable([35, 64, 42], category="iron_ore", error_code="blocked")
        memo.record_seen("structure", "hay_block", [102, 64, -198], dist=22.0, tag="village")
        memo.mark_protected([50, 62, 50], category="obsidian", block="crying_obsidian")
        memo.record_seen("coal_ore", "coal_ore", [-10, 40, 12], dimension="overworld", status="depleted")

    def test_save_creates_nested_dirs_utf8_and_full_roundtrip(self):
        now = [250.0]
        orig = WorldResourceMemo(clock=lambda: now[0])
        self._populate_memo(orig)

        with tempfile.TemporaryDirectory() as tmp:
            ckpt = Path(tmp) / "nested" / "checkpoints" / "world_memo.json"
            orig.save_checkpoint(ckpt)

            self.assertTrue(ckpt.exists())
            leftovers = [p.name for p in ckpt.parent.iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

            raw_text = ckpt.read_text(encoding="utf-8")
            parsed = json.loads(raw_text)
            self.assertEqual(parsed["schema_version"], 1)
            self.assertIn("村庄钟声", raw_text)

            restored = WorldResourceMemo(clock=lambda: now[0])
            self.assertTrue(restored.load_checkpoint(ckpt))

            self.assertEqual(restored.landmark_version, orig.landmark_version)
            self.assertEqual(restored._stations, orig._stations)
            self.assertEqual(restored._landmarks, orig._landmarks)
            self.assertEqual(restored._portals, orig._portals)
            self.assertEqual(restored._entries, orig._entries)
            self.assertEqual(restored.stations("bed"), orig.stations("bed"))
            self.assertEqual(restored.landmarks("village"), orig.landmarks("village"))
            self.assertTrue(restored.has_active_portal(dimension="overworld"))
            self.assertTrue(restored.has_active_portal(dimension="the_nether"))
            self.assertTrue(restored.is_blocked([35, 64, 42], category="iron_ore"))
            self.assertEqual(restored.summary_for_context(), orig.summary_for_context())

            # 原子覆写现有检查点
            updated = WorldResourceMemo(clock=lambda: 500.0)
            updated.record_station("smoker", [9, 64, 9])
            updated.record_landmark("stronghold", [300, 30, -300], meta={"portal_eyes": 2})
            updated.save_checkpoint(ckpt)
            self.assertEqual([p.name for p in ckpt.parent.iterdir() if p.name.endswith(".tmp")], [])

            fresh = WorldResourceMemo(clock=lambda: 500.0)
            self.assertTrue(fresh.load_checkpoint(ckpt))
            self.assertEqual(fresh.stations("furnace"), [(9, 64, 9)])
            self.assertFalse(fresh.has_station("bed"))
            self.assertEqual(fresh.landmarks("stronghold"), [(300, 30, -300)])
            self.assertEqual(fresh.landmark_version, 1)
            self.assertEqual(len(fresh), 0)

    def test_load_checkpoint_failures_do_not_corrupt_existing_memo(self):
        memo = WorldResourceMemo(clock=lambda: 100.0)
        memo.record_station("crafting_table", [1, 64, 1])
        memo.record_landmark("village", [80, 64, 80], meta={"bell": True})
        memo.record_active_portal([5, 64, 5])
        memo.record_seen("wood", "oak_log", [10, 64, 10], status="reachable")

        base_payload = {
            "schema_version": 1,
            "landmark_version": 1,
            "stations": {"overworld": {"crafting_table": [[1, 64, 1]]}},
            "landmarks": {"overworld": {"village": [{"pos": [80, 64, 80], "meta": {}}]}},
            "portals": {"overworld": [[5, 64, 5]]},
            "resources": [
                {
                    "category": "wood",
                    "block": "oak_log",
                    "pos": [10, 64, 10],
                    "dimension": "overworld",
                    "status": "reachable",
                    "error_code": "",
                    "last_dist": 5.0,
                    "attempts": 1,
                    "updated_at": 100.0,
                    "tag": None,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self.assertFalse(memo.load_checkpoint(tmp_dir / "nonexistent.json"))

            raw_bad_cases = [
                ("invalid_json", "{truncated"),
                ("non_dict_list", "[1, 2, 3]"),
                ("non_dict_str", "\"hello\""),
            ]
            for name, text in raw_bad_cases:
                p = tmp_dir / f"{name}.json"
                p.write_text(text, encoding="utf-8")
                with self.subTest(case=name):
                    self.assertFalse(memo.load_checkpoint(p))

            corrupt_dicts: dict[str, dict] = {
                "schema_v2": {**base_payload, "schema_version": 2},
                "schema_bool": {**base_payload, "schema_version": True},
                "landmark_ver_neg": {**base_payload, "landmark_version": -1},
                "landmark_ver_bool": {**base_payload, "landmark_version": True},
                "bad_station_cell": {**base_payload, "stations": {"overworld": {"crafting_table": [[1, 64]]}}},
                "bad_station_bool_coord": {**base_payload, "stations": {"overworld": {"crafting_table": [[1, True, 2]]}}},
                "bad_landmark_meta": {
                    **base_payload,
                    "landmarks": {"overworld": {"village": [{"pos": [80, 64, 80], "meta": "bad"}]}},
                },
                "bad_portal_cell": {**base_payload, "portals": {"overworld": [[1.5, 64, 0]]}},
                "bad_resource_status": {
                    **base_payload,
                    "resources": [{**base_payload["resources"][0], "status": "unknown_status"}],
                },
                "bad_resource_attempts": {
                    **base_payload,
                    "resources": [{**base_payload["resources"][0], "attempts": -1}],
                },
                "bad_resource_pos": {
                    **base_payload,
                    "resources": [{**base_payload["resources"][0], "pos": "10,64,10"}],
                },
            }
            for name, data in corrupt_dicts.items():
                p = tmp_dir / f"{name}.json"
                p.write_text(json.dumps(data), encoding="utf-8")
                with self.subTest(case=name):
                    self.assertFalse(memo.load_checkpoint(p))

        # 所有失败加载后原台账完好无损
        self.assertEqual(memo.landmark_version, 1)
        self.assertEqual(memo.stations("crafting_table"), [(1, 64, 1)])
        self.assertEqual(memo.landmarks("village"), [(80, 64, 80)])
        self.assertTrue(memo.has_active_portal())
        self.assertEqual(len(memo), 1)
        self.assertEqual(memo.query("wood")[0].status, "reachable")

    def test_clear_resets_all_memo_structures(self):
        memo = WorldResourceMemo(clock=lambda: 10.0)
        self._populate_memo(memo)
        memo.record_blocked_region("gather_wood", (1, 2), ttl_s=60.0)
        self.assertGreater(memo.landmark_version, 0)
        self.assertGreater(len(memo), 0)

        memo.clear()
        self.assertEqual(len(memo), 0)
        self.assertEqual(memo.landmark_version, 0)
        self.assertEqual(memo.stations(None), [])
        self.assertEqual(memo.stations(None, dimension="the_nether"), [])
        self.assertEqual(memo.landmarks(None), [])
        self.assertEqual(memo.landmarks(None, dimension="the_nether"), [])
        self.assertFalse(memo.has_active_portal(dimension="overworld"))
        self.assertFalse(memo.has_active_portal(dimension="the_nether"))
        self.assertFalse(memo.is_region_blocked("gather_wood", (1, 2)))
        self.assertEqual(memo.query("wood"), [])


class EmergencyEscapeSkillTests(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()
        _COOLDOWN.clear()
        self.addCleanup(reset_all_world_memos)
        self.addCleanup(_COOLDOWN.clear)

    def test_registration_contract_and_intent_validation(self):
        self.assertIs(REGISTRY.get("emergency_escape"), EmergencyEscape)
        self.assertEqual(contract_errors(EmergencyEscape), [])
        self.assertEqual(EmergencyEscape.goal, "safety:{intent}")
        self.assertEqual(EmergencyEscape.verified, "live")
        self.assertEqual(EmergencyEscape.params["intent"].choices, ("surface", "flee", "douse"))

        for intent in ("surface", "flee", "douse"):
            with self.subTest(intent=intent):
                sk = make("emergency_escape", {"intent": intent})
                self.assertEqual(sk.goal, f"safety:{intent}")
                self.assertEqual(sk.args["intent"], intent)

        with self.assertRaises(ValueError):
            make("emergency_escape", {"intent": "fly"})
        with self.assertRaises(ValueError):
            EmergencyEscape(intent="teleport")
        _, errs = bind_args(EmergencyEscape, {"intent": "swim"})
        self.assertTrue(errs)

    def test_delegates_to_escape_hazard_step_when_resolved(self):
        for expected_ret in (True, False):
            with self.subTest(expected_ret=expected_ret):
                seen_args: list[tuple] = []

                def fake_escape(ex, state, intent):
                    seen_args.append((ex, state, intent))
                    yield rec("move_to", ex.move_to(pos=[2.0, 66.0, 0.0]), intent=intent)
                    return expected_ret

                world = GridWorld(pos=(0.5, 64.0, 0.5))
                ex, state = world.session()
                skill = make("emergency_escape", {"intent": "surface"})

                with patch("mcagent.skills.safety_escape._resolve_escape_hazard_step", return_value=fake_escape):
                    ret, steps = _drive_skill_result(skill, ex, state)

                self.assertIs(ret, expected_ret)
                self.assertEqual(seen_args, [(ex, state, "surface")])
                self.assertEqual(len(steps), 1)
                self.assertEqual(steps[0]["op"], "move_to")
                self.assertEqual(skill._start_pos, (0.5, 64.0, 0.5))

    def test_fallback_single_step_for_all_three_intents(self):
        with patch("mcagent.skills.safety_escape._resolve_escape_hazard_step", return_value=None):
            # 1) surface: 上浮 +2y
            w_surface = GridWorld(pos=(1.5, 60.0, 2.5))
            ex, state = w_surface.session()
            ret, steps = _drive_skill_result(make("emergency_escape", {"intent": "surface"}), ex, state)
            self.assertTrue(ret)
            self.assertEqual([s["op"] for s in steps], ["move_to"])
            self.assertEqual(w_surface.st["pos"], [1.5, 62.0, 2.5])

            # 2a) douse 有水桶: use_item("water_bucket")
            w_douse_bucket = _UseItemGridWorld(pos=(1.5, 64.0, 2.5), items={"water_bucket": 1})
            ex, state = w_douse_bucket.session()
            ret, steps = _drive_skill_result(make("emergency_escape", {"intent": "douse"}), ex, state)
            self.assertTrue(ret)
            self.assertEqual([s["op"] for s in steps], ["use_item"])
            self.assertEqual(steps[0]["intent"], "douse")
            self.assertIn(("use_item", {"item": "water_bucket", "target": None}), w_douse_bucket.calls)

            # 2b) douse 无水桶: 横向移动 +2x
            w_douse_move = GridWorld(pos=(1.5, 64.0, 2.5))
            ex, state = w_douse_move.session()
            ret, steps = _drive_skill_result(make("emergency_escape", {"intent": "douse"}), ex, state)
            self.assertTrue(ret)
            self.assertEqual([s["op"] for s in steps], ["move_to"])
            self.assertEqual(w_douse_move.st["pos"], [3.5, 64.0, 2.5])

            # 3) flee: 远离 nearest_hostile 8.0 格
            w_flee = GridWorld(pos=(10.0, 64.0, 10.0))
            w_flee.st["nearest_hostile"] = {"name": "zombie", "pos": [7.0, 64.0, 6.0], "dist": 5.0}
            ex, state = w_flee.session()
            ret, steps = _drive_skill_result(make("emergency_escape", {"intent": "flee"}), ex, state)
            self.assertTrue(ret)
            self.assertEqual([s["op"] for s in steps], ["move_to"])
            # 向量 (10-7, 10-6) = (3, 4)，单位向量 (0.6, 0.8)，目标 = (10+4.8, 64, 10+6.4)
            self.assertAlmostEqual(w_flee.st["pos"][0], 14.8, places=6)
            self.assertAlmostEqual(w_flee.st["pos"][1], 64.0, places=6)
            self.assertAlmostEqual(w_flee.st["pos"][2], 16.4, places=6)
            self.assertAlmostEqual(math.hypot(w_flee.st["pos"][0] - 10.0, w_flee.st["pos"][2] - 10.0), 8.0, places=6)

    def test_verify_done_for_surface_douse_and_flee(self):
        def _state_with(**fields) -> State:
            st = State()
            base = {"ready": True, "alive": True, "pos": [0.5, 64.0, 0.5]}
            base.update(fields)
            st.set_status(base)
            return st

        # surface
        sk_surface = make("emergency_escape", {"intent": "surface"})
        self.assertEqual(sk_surface.verify_done(_state_with(in_water=False, submerged=False), {}), "")
        self.assertNotEqual(sk_surface.verify_done(_state_with(in_water=True, submerged=True, oxygen=20), {}), "")
        self.assertEqual(sk_surface.verify_done(_state_with(in_water=True, submerged=False, oxygen=15), {}), "")
        self.assertNotEqual(sk_surface.verify_done(_state_with(in_water=True, submerged=False, oxygen=14), {}), "")

        # douse
        sk_douse = make("emergency_escape", {"intent": "douse"})
        self.assertEqual(sk_douse.verify_done(_state_with(on_fire=False, in_lava=False), {}), "")
        self.assertNotEqual(sk_douse.verify_done(_state_with(on_fire=True, in_lava=False), {}), "")
        self.assertNotEqual(sk_douse.verify_done(_state_with(on_fire=False, in_lava=True), {}), "")

        # flee
        sk_flee = make("emergency_escape", {"intent": "flee"})
        self.assertEqual(sk_flee.verify_done(_state_with(nearest_hostile=None), {}), "")
        self.assertEqual(sk_flee.verify_done(_state_with(nearest_hostile={"name": "zombie", "dist": 6.0}), {}), "")
        self.assertNotEqual(sk_flee.verify_done(_state_with(nearest_hostile={"name": "zombie", "dist": 2.5}), {}), "")

        # flee 位移 >= 4.0 格时即使敌对生物仍近也判定通过
        world = GridWorld(pos=(0.5, 64.0, 0.5))
        world.st["nearest_hostile"] = {"name": "zombie", "pos": [-2.0, 64.0, 0.5], "dist": 2.5}
        ex, state = world.session()
        sk_flee_run = make("emergency_escape", {"intent": "flee"})
        with patch("mcagent.skills.safety_escape._resolve_escape_hazard_step", return_value=None):
            drive(sk_flee_run, ex, state)
        # 模拟位移 >= 4.0 但敌对生物追近到 dist=2.5
        state.set_status({
            "pos": [4.5, 64.0, 0.5],
            "nearest_hostile": {"name": "zombie", "pos": [2.0, 64.0, 0.5], "dist": 2.5},
        })
        self.assertEqual(sk_flee_run.verify_done(state, {}), "")


if __name__ == "__main__":
    unittest.main()
