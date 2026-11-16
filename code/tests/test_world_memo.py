"""通用共享世界资源台账离线单测（verified: offline）。"""
from __future__ import annotations

import gc
import re
import unittest
import weakref

from mcagent.runtime import State
from mcagent.skills import REGISTRY, Skill, load_all, world_memo
from mcagent.skills.world_memo import (
    ALL_LOG_BLOCKS,
    BED_BLOCKS,
    CATEGORY_BLOCKS,
    STRUCTURE_FINGERPRINTS,
    VALID_STATUSES,
    WorldResourceMemo,
    classify_block,
    format_memo_summary,
    memo_for,
    reset_all_world_memos,
    structure_tags_for,
)


class WorldMemoModuleContractTests(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()
        self.addCleanup(reset_all_world_memos)

    def test_exported_without_register_side_effects(self):
        before = dict(REGISTRY)
        loaded = load_all()
        self.assertIn("world_memo", loaded)
        self.assertEqual(dict(REGISTRY), before)
        skill_subclasses = [
            obj for obj in vars(world_memo).values()
            if isinstance(obj, type) and issubclass(obj, Skill) and obj is not Skill
        ]
        self.assertEqual(skill_subclasses, [])

    def test_classification_and_structure_fingerprints(self):
        self.assertEqual(len(ALL_LOG_BLOCKS), 9)
        self.assertEqual(len(BED_BLOCKS), 16)
        self.assertEqual(VALID_STATUSES, ("untested", "reachable", "unreachable", "protected", "depleted"))

        expected = {
            "minecraft:spruce_log": "wood",
            "pale_oak_log": "wood",
            "water": "water",
            "lava": "lava",
            "lava_cauldron": "lava",
            "iron_ore": "iron_ore",
            "deepslate_iron_ore": "iron_ore",
            "raw_iron_block": "iron_ore",
            "coal_ore": "coal_ore",
            "diamond_ore": "diamond_ore",
            "obsidian": "obsidian",
            "crying_obsidian": "obsidian",
            "crafting_table": "station",
            "enchanting_table": "station",
            "damaged_anvil": "station",
            "red_bed": "station",
            "minecraft:light_blue_bed": "station",
            "stone": None,
            "air": None,
            "": None,
        }
        for block_name, cat in expected.items():
            with self.subTest(block_name=block_name):
                self.assertEqual(classify_block(block_name), cat)
        for bed in BED_BLOCKS:
            self.assertEqual(classify_block(bed), "station")
            self.assertIn(bed, CATEGORY_BLOCKS["station"])

        tags = structure_tags_for([
            "minecraft:hay_block", "bell", "stone_bricks", "crying_obsidian",
            "bee_nest", "trial_spawner", "sculk_catalyst", "suspicious_sand", "powder_snow",
        ])
        self.assertEqual(set(tags), set(STRUCTURE_FINGERPRINTS))
        self.assertEqual(tags["village"], ["hay_block", "bell"])
        self.assertEqual(tags["ruined_portal"], ["crying_obsidian"])


class WorldMemoIngestAndQueryTests(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()
        self.addCleanup(reset_all_world_memos)
        self.now = [100.0]
        self.memo = WorldResourceMemo(clock=lambda: self.now[0])

    def test_ingest_detail_and_summary_modes(self):
        detail_resp = {
            "total": 6,
            "blocks": [
                {"name": "minecraft:spruce_log", "pos": [35.2, 65.0, 0.8], "dist": 35.0},
                {"name": "lava", "pos": [52, 62, 0], "dist": 52.0},
                {"name": "water", "pos": [12, 63, 4], "dist": 12.6},
                {"name": "iron_ore", "pos": [8, 50, -3], "dist": 16.2},
                {"name": "crafting_table", "pos": [4, 64, 2], "dist": 4.5},
                {"name": "stone", "pos": [1, 63, 0], "dist": 1.0},
            ],
        }
        n1 = self.memo.ingest_find_blocks(detail_resp, origin=[0.5, 64.0, 0.5])
        self.assertEqual(n1, 5)  # stone 被过滤

        self.assertEqual(self.memo.query("wood")[0].pos, (35, 65, 0))
        self.assertEqual(self.memo.query("wood")[0].block, "spruce_log")
        self.assertEqual(self.memo.query("lava")[0].block, "lava")
        self.assertEqual(self.memo.query("water")[0].block, "water")
        self.assertEqual(self.memo.query("iron_ore")[0].block, "iron_ore")
        self.assertEqual(self.memo.query("station")[0].block, "crafting_table")

        summary_resp = {
            "radius": 48,
            "scanned": 512,
            "blocks": {
                "obsidian": {"count": 4, "nearest": [-20, 60, 15], "dist": 25.0},
                "crying_obsidian": {"count": 1, "nearest": [-18, 61, 15], "dist": 23.4},
                "hay_block": {"count": 3, "nearest": [40, 64, -10], "dist": 41.2},
                "cyan_bed": {"count": 1, "nearest": [42, 65, -11], "dist": 43.0},
                "dirt": {"count": 200, "nearest": [1, 63, 1], "dist": 1.4},
            },
        }
        n2 = self.memo.ingest_find_blocks(summary_resp)
        self.assertEqual(n2, 4)  # dirt 被过滤

        obs = self.memo.query("obsidian")
        self.assertEqual([e.block for e in obs], ["crying_obsidian", "obsidian"])
        structs = self.memo.query("structure")
        self.assertEqual({(e.tag, e.block) for e in structs}, {
            ("ruined_portal", "crying_obsidian"),
            ("ruined_portal", "obsidian"),
            ("village", "hay_block"),
        })
        stations = self.memo.query("station")
        self.assertEqual({e.block for e in stations}, {"crafting_table", "cyan_bed"})

    def test_dist_zero_and_missing_dist_fallback_to_origin(self):
        resp = {
            "blocks": [
                {"name": "oak_log", "pos": [0, 64, 0], "dist": 0},
                {"name": "birch_log", "pos": [10, 64, 0]},
            ],
        }
        self.assertEqual(self.memo.ingest_find_blocks(resp, origin=[0.5, 64.0, 0.5]), 2)
        woods = self.memo.query("wood")
        self.assertEqual(woods[0].block, "oak_log")
        self.assertEqual(woods[0].last_dist, 0.0)
        self.assertIsNotNone(woods[1].last_dist)
        self.assertGreater(woods[1].last_dist, 9.0)

    def test_status_stickiness_cooldown_and_depletion(self):
        pos_log = [15, 65, -4]
        pos_lava = [30, 63, 8]
        pos_iron = [6, 52, 6]
        pos_prot = [2, 64, 2]

        self.memo.ingest_find_blocks({
            "blocks": [
                {"name": "spruce_log", "pos": pos_log, "dist": 16.0},
                {"name": "lava", "pos": pos_lava, "dist": 31.0},
                {"name": "iron_ore", "pos": pos_iron, "dist": 14.0},
                {"name": "oak_log", "pos": pos_prot, "dist": 3.0},
            ],
        })

        # 1) protected 不被后续 find_blocks 洗白，也不被 mark_unreachable 降级
        self.memo.mark_protected(pos_prot)
        self.memo.mark_unreachable(pos_prot, error_code="path_not_found")
        self.memo.ingest_find_blocks({"blocks": [{"name": "oak_log", "pos": pos_prot, "dist": 2.0}]})
        self.assertTrue(self.memo.is_blocked(pos_prot))
        self.assertNotIn((2, 64, 2), [e.pos for e in self.memo.query("wood")])

        # 2) unreachable 在 120s 冷却期内不被普通 find_blocks 洗白，也不重置冷却计时
        self.now[0] = 110.0
        self.memo.mark_unreachable(pos_log, error_code="path_not_found")
        self.assertTrue(self.memo.is_blocked(pos_log))
        self.assertNotIn((15, 65, -4), [e.pos for e in self.memo.query("wood")])
        self.assertIn((15, 65, -4), [e.pos for e in self.memo.query("wood", include_unreachable=True)])

        self.now[0] = 160.0  # 距失败仅 50s (< 120s)
        self.memo.ingest_find_blocks({"blocks": [{"name": "spruce_log", "pos": pos_log, "dist": 12.0}]})
        self.assertTrue(self.memo.is_blocked(pos_log))
        entry_cooling = self.memo.query("wood", include_unreachable=True)[0]
        self.assertEqual(entry_cooling.status, "unreachable")
        self.assertEqual(entry_cooling.error_code, "path_not_found")
        self.assertEqual(entry_cooling.last_dist, 12.0)

        # 距最初失败 110.0 已满 120.5s：允许重探，重扫可洗回 untested
        self.now[0] = 230.5
        self.assertFalse(self.memo.is_blocked(pos_log))
        self.assertIn((15, 65, -4), [e.pos for e in self.memo.query("wood")])
        self.memo.ingest_find_blocks({"blocks": [{"name": "spruce_log", "pos": pos_log, "dist": 11.5}]})
        refreshed = [e for e in self.memo.query("wood") if e.pos == (15, 65, -4)][0]
        self.assertEqual(refreshed.status, "untested")
        self.assertEqual(refreshed.error_code, "")

        # 3) reachable 不被 untested 降级
        self.memo.mark_reachable(pos_lava)
        self.memo.ingest_find_blocks({"blocks": [{"name": "lava", "pos": pos_lava, "dist": 29.0}]})
        lava_entry = self.memo.query("lava")[0]
        self.assertEqual(lava_entry.status, "reachable")
        self.assertEqual(lava_entry.last_dist, 29.0)

        # 4) depleted 永久剔除且不被重扫复活
        self.memo.mark_depleted(pos_iron)
        self.assertTrue(self.memo.is_blocked(pos_iron))
        self.assertEqual(self.memo.query("iron_ore"), [])
        self.memo.ingest_find_blocks({"blocks": [{"name": "iron_ore", "pos": pos_iron, "dist": 10.0}]})
        self.assertTrue(self.memo.is_blocked(pos_iron))
        self.assertEqual(self.memo.query("iron_ore"), [])

    def test_untyped_mark_protected_before_scan_survives_find_blocks(self):
        self.memo.mark_protected([9, 64, 9])
        self.assertTrue(self.memo.is_blocked([9, 64, 9]))
        self.memo.ingest_find_blocks({"blocks": [{"name": "crying_obsidian", "pos": [9, 64, 9], "dist": 9.0}]})
        self.assertTrue(self.memo.is_blocked([9, 64, 9], category="obsidian"))
        self.assertTrue(self.memo.is_blocked([9, 64, 9], category="structure"))
        self.assertEqual(self.memo.query("obsidian"), [])
        self.assertEqual(self.memo.query("structure"), [])

    def test_cross_dimension_isolation_and_origin_sorting(self):
        self.memo.record_seen("lava", "lava", [10, 64, 10], dimension="overworld", dist=10.0)
        self.memo.record_seen("lava", "lava", [10, 64, 10], dimension="the_nether", dist=5.0)
        self.memo.mark_depleted([10, 64, 10], dimension="overworld")

        self.assertTrue(self.memo.is_blocked([10, 64, 10], dimension="overworld"))
        self.assertFalse(self.memo.is_blocked([10, 64, 10], dimension="the_nether"))
        self.assertEqual(self.memo.query("lava", dimension="overworld"), [])
        nether = self.memo.query("lava", dimension="the_nether")
        self.assertEqual(len(nether), 1)
        self.assertEqual(nether[0].dimension, "the_nether")

        self.memo.record_seen("wood", "oak_log", [20, 64, 0], dist=5.0)
        self.memo.record_seen("wood", "spruce_log", [2, 64, 0], dist=20.0)
        self.assertEqual(self.memo.query("wood")[0].block, "oak_log")
        self.assertEqual(self.memo.query("wood", origin=[1.5, 64.0, 0.5])[0].block, "spruce_log")

    def test_bucket_capacity_evicts_oldest_non_protected_first(self):
        bounded = WorldResourceMemo(clock=lambda: self.now[0], max_per_bucket=3)
        self.now[0] = 1.0
        bounded.record_seen("wood", "oak_log", [1, 64, 0], status="protected")
        for x in (2, 3, 4):
            self.now[0] += 1.0
            bounded.record_seen("wood", "birch_log", [x, 64, 0])
        self.assertEqual(len(bounded), 3)
        self.assertTrue(bounded.is_blocked([1, 64, 0]))
        self.assertEqual({e.pos for e in bounded.query("wood")}, {(3, 64, 0), (4, 64, 0)})
        self.assertEqual([e.pos for e in bounded.query("wood", origin=[0.5, 64.0, 0.5])], [(3, 64, 0), (4, 64, 0)])


class WorldMemoIsolationAndSanitizationTests(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()
        self.addCleanup(reset_all_world_memos)

    def test_weakref_isolation_per_state_and_reset(self):
        s1, s2 = State(), State()
        m1, m2 = memo_for(s1), memo_for(s2)
        self.assertIsNot(m1, m2)
        self.assertIs(memo_for(s1), m1)
        m1.record_seen("wood", "oak_log", [10, 64, 10], dist=10.0)
        self.assertEqual(len(m1), 1)
        self.assertEqual(len(m2), 0)

        # 不可弱引用与 None 回退全局默认台账
        self.assertIs(memo_for(None), memo_for({"not": "weakrefable"}))
        memo_for(None).record_seen("water", "water", [3, 63, 3], dist=3.0)
        self.assertEqual(len(memo_for(None)), 1)

        reset_all_world_memos()
        self.assertEqual(len(m1), 0)
        self.assertEqual(len(memo_for(None)), 0)

        # State 销毁后 WeakKeyDictionary 自动回收
        ephemeral = State()
        ref = weakref.ref(ephemeral)
        memo_for(ephemeral).record_seen("lava", "lava", [7, 62, 7], dist=7.0)
        del ephemeral
        gc.collect()
        self.assertIsNone(ref())

    def test_format_memo_summary_never_exposes_3d_coordinates(self):
        state = State()
        state.set_status({"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "sneaking": False})
        memo = memo_for(state)
        memo.ingest_find_blocks({
            "blocks": [
                {"name": "spruce_log", "pos": [123, 68, -456], "dist": 35.2},
                {"name": "lava", "pos": [-987, 61, 345], "dist": 52.0},
                {"name": "hay_block", "pos": [1024, 64, -777], "dist": 48.0},
                {"name": "crying_obsidian", "pos": [42, 70, 88], "dist": 21.0},
            ],
        })
        memo.mark_reachable([-987, 61, 345])

        summary = format_memo_summary(state)
        self.assertIn("原木: spruce_log", summary)
        self.assertIn("熔岩: lava", summary)
        self.assertIn("已确认可达", summary)
        self.assertIn("未探测", summary)
        self.assertIn("village(hay_block)", summary)
        self.assertIn("ruined_portal(crying_obsidian)", summary)

        # J17 严格脱敏：不得出现任何三维坐标元组或原始坐标数字
        self.assertIsNone(re.search(r"-?\d+\s*,\s*-?\d+\s*,\s*-?\d+", summary))
        self.assertIsNone(re.search(r"[\[(]\s*-?\d+[^|\n]*-?\d+[^|\n]*-?\d+\s*[\])]", summary))
        for raw_num in ("123", "-456", "-987", "345", "1024", "-777", "42", "88"):
            self.assertNotIn(raw_num, summary)

        # 维度切换自动隔离
        state.set_status({"pos": [0.5, 64.0, 0.5], "dimension": "the_nether", "sneaking": False})
        self.assertEqual(format_memo_summary(state), "")
        memo.record_seen("lava", "lava", [5, 64, 0], dimension="the_nether")
        nether_summary = format_memo_summary(state)
        self.assertIn("熔岩: lava 约5格(未探测)", nether_summary)


if __name__ == "__main__":
    unittest.main()
