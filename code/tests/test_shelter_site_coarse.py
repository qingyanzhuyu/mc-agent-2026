"""避难所选址粗筛（shelter_site._center_column_ok / evaluate_shelter_site）的离线单测。

背景（2026-11-24 真机实测）：`build_spawn_shelter` 仅"选址"阶段就在 100 秒内打了
1369 次 `/get_block`（每个候选地基 5×5×5=125 格全量探测），一块砖都没放，表现成"bot 不动"。

修法：先用中心柱 5 格粗筛，中心柱不合格的候选直接淘汰——该判定严格等价于全量评估里
dx=dz=0 那一支，所以不会改变最终选中结果，只是把请求量从 125/候选降到 5/候选。
"""
from __future__ import annotations

import unittest
from unittest import mock

from mcagent.skills import shelter_site

CY = 64
CX = 10
CZ = -20


def _snap_center(floor="stone", feet="air", head="air", above="air", two_below="stone"):
    """只构造中心柱 5 格（cy-2 .. cy+2）。"""
    return {
        (CX, CY - 2, CZ): {"name": two_below},
        (CX, CY - 1, CZ): {"name": floor},
        (CX, CY, CZ): {"name": feet},
        (CX, CY + 1, CZ): {"name": head},
        (CX, CY + 2, CZ): {"name": above},
    }


def _snap_full(radius=2, floor="stone", two_below="stone"):
    """整块 5×5×5 都是"实心地面 + 上方空气"的完美地基。"""
    snap = {}
    for dx in range(-radius, radius + 1):
        for dz in range(-radius, radius + 1):
            for dy in (-2, -1, 0, 1, 2):
                name = floor if dy == -1 else (two_below if dy == -2 else "air")
                snap[(CX + dx, CY + dy, CZ + dz)] = {"name": name}
    return snap


class CenterColumnCoarseTest(unittest.TestCase):
    def test_flat_ground_is_viable(self):
        self.assertTrue(shelter_site._center_column_ok(_snap_center(), CX, CY, CZ))

    def test_water_rejects(self):
        self.assertFalse(shelter_site._center_column_ok(_snap_center(feet="water"), CX, CY, CZ))

    def test_lava_rejects(self):
        self.assertFalse(shelter_site._center_column_ok(_snap_center(above="lava"), CX, CY, CZ))

    def test_unsupported_column_rejects(self):
        """脚下和脚下第二格都不是可用底面（悬空/断崖）→ 淘汰。"""
        self.assertFalse(
            shelter_site._center_column_ok(_snap_center(floor="air", two_below="air"), CX, CY, CZ)
        )

    def test_head_blocked_rejects(self):
        self.assertFalse(shelter_site._center_column_ok(_snap_center(head="stone"), CX, CY, CZ))

    def test_one_block_dip_using_second_floor_is_viable(self):
        """低一格地形（脚下是可清理空/薄覆盖、第二格才是底面）也允许。"""
        snap = _snap_center(floor="air", two_below="stone")
        self.assertTrue(shelter_site._center_column_ok(snap, CX, CY, CZ))


class EvaluateUsesCoarseFilterFirstTest(unittest.TestCase):
    def test_rejected_candidate_costs_five_cells_not_125(self):
        calls: list[list] = []

        def fake_read(_ex, cells):
            cells = list(cells)
            calls.append(cells)
            return _snap_center(feet="water")          # 中心柱含流体 → 必然淘汰

        with mock.patch.object(shelter_site, "_read_blocks", fake_read):
            self.assertIsNone(shelter_site.evaluate_shelter_site(object(), CX, CY, CZ))

        self.assertEqual(len(calls), 1, "被粗筛淘汰的候选不应再做 125 格全量探测")
        self.assertEqual(len(calls[0]), 5)

    def test_viable_candidate_still_returns_full_evaluation(self):
        calls: list[list] = []

        def fake_read(_ex, cells):
            cells = list(cells)
            calls.append(cells)
            return _snap_full()

        with mock.patch.object(shelter_site, "_read_blocks", fake_read):
            ev = shelter_site.evaluate_shelter_site(object(), CX, CY, CZ)

        self.assertIsNotNone(ev)
        self.assertEqual(ev["center"], (CX, CY, CZ))
        self.assertEqual(ev["edit_cost"], 0)
        self.assertEqual(len(calls), 2, "通过粗筛后仍要跑一次全量评估")
        self.assertEqual(len(calls[1]), 125)


if __name__ == "__main__":
    unittest.main()
