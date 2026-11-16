"""共享眼高、方块中心距离与 3D DDA 视线遮挡模块单测（verified: offline）。"""
from __future__ import annotations

import math
import unittest

from mcagent.client import ApiError
from mcagent.runtime import Replan
from mcagent.skills.sightline import (
    API_DIG_EYE_REACH,
    DIG_EYE_REACH,
    ENTITY_EYE_REACH,
    EYE_HEIGHT_SNEAKING,
    EYE_HEIGHT_STANDING,
    STATION_EYE_REACH,
    block_cell,
    block_center,
    can_reach_block,
    can_see_block,
    eye_height,
    eye_pos,
    eye_to_aabb_dist,
    eye_to_block_dist,
    eye_to_point_dist,
    feet_pos,
    voxel_line_cells,
)
from mcagent.skills.testing import GridWorld


class SightlineGeometryTests(unittest.TestCase):
    def test_eye_height_switches_between_standing_and_sneaking(self):
        self.assertEqual(EYE_HEIGHT_STANDING, 1.62)
        self.assertEqual(EYE_HEIGHT_SNEAKING, 1.27)
        self.assertEqual(DIG_EYE_REACH, 4.5)
        self.assertEqual(API_DIG_EYE_REACH, 5.0)
        self.assertEqual(STATION_EYE_REACH, 3.5)
        self.assertEqual(ENTITY_EYE_REACH, 4.0)

        self.assertEqual(eye_height({"pos": [0.5, 64.0, 0.5], "sneaking": False}), 1.62)
        self.assertEqual(eye_height({"pos": [0.5, 64.0, 0.5], "sneaking": True}), 1.27)
        self.assertEqual(eye_pos([0.5, 64.0, 0.5]), (0.5, 65.62, 0.5))
        self.assertEqual(eye_pos({"pos": [0.5, 64.0, 0.5], "sneaking": True}), (0.5, 65.27, 0.5))
        self.assertEqual(eye_pos({"pos": [0.5, 64.0, 0.5], "sneaking": True}, sneaking=False), (0.5, 65.62, 0.5))

    def test_rejects_invalid_bool_or_non_finite_coordinates(self):
        for bad in (None, {}, {"pos": [1, 2]}, [True, 64, 0], [0.5, float("nan"), 0.5], [0.5, float("inf"), 0.5]):
            with self.subTest(bad=bad):
                with self.assertRaises(Replan):
                    feet_pos(bad)
        for bad_block in ([False, 64, 0], [1, float("nan"), 0], [1, 2]):
            with self.subTest(bad_block=bad_block):
                with self.assertRaises(Replan):
                    block_cell(bad_block)

    def test_block_center_eliminates_positive_negative_axis_asymmetry(self):
        st = {"pos": [0.5, 64.0, 0.5], "sneaking": False}
        self.assertEqual(block_center([-3, 64, 0]), (-2.5, 64.5, 0.5))
        self.assertEqual(block_center([3, 64, 0]), (3.5, 64.5, 0.5))
        # 对称的东/西 3 格工作台到眼睛的距离严格相等（3.202 格 <= 3.5），旧脚底角点算法西侧为 3.54 > 3.5
        east = eye_to_block_dist(st, [3, 64, 0])
        west = eye_to_block_dist(st, [-3, 64, 0])
        self.assertAlmostEqual(east, west, places=9)
        self.assertLessEqual(west, STATION_EYE_REACH)

    def test_overhead_trunk_vs_diagonal_downward_block_distance(self):
        st = {"pos": [0.5, 64.0, 0.5], "sneaking": False}
        # 头顶 +5 格原木 [0, 69, 0]：脚底距离 5.05，但眼睛 (65.62) 到中心 (69.5) 仅 3.88 <= 4.5
        self.assertAlmostEqual(eye_to_block_dist(st, [0, 69, 0]), 3.88, places=6)
        self.assertLessEqual(eye_to_block_dist(st, [0, 69, 0]), DIG_EYE_REACH)
        # 斜上方 [2, 68, 0]：旧脚底角点 4.30 > 4.2，实际眼睛到中心仅 ~3.51 <= 4.5
        self.assertAlmostEqual(eye_to_block_dist(st, [2, 68, 0]), math.hypot(2.0, 2.88), places=6)
        self.assertLessEqual(eye_to_block_dist(st, [2, 68, 0]), DIG_EYE_REACH)

        # 斜下方 [2, 61, 0]：旧脚底角点 3.39 < 4.0，但实际眼睛到中心 4.58 > 4.5（原版够不到中心）
        self.assertGreater(eye_to_block_dist(st, [2, 61, 0]), DIG_EYE_REACH)

    def test_aabb_and_point_distances(self):
        st = {"pos": [0.5, 64.0, 0.5], "sneaking": False}
        # 眼睛在 (0.5, 65.62, 0.5)，[0, 70, 0] 底面在 y=70.0，AABB 最近点距离 = 70.0 - 65.62 = 4.38
        self.assertAlmostEqual(eye_to_aabb_dist(st, [0, 70, 0]), 4.38, places=6)
        # 眼睛已在 [0, 65, 0] 格内时，AABB 距离为 0
        self.assertEqual(eye_to_aabb_dist(st, [0, 65, 0]), 0.0)
        self.assertAlmostEqual(eye_to_point_dist(st, [0.5, 65.62, 3.5]), 3.0, places=6)


class SightlineRaycastTests(unittest.TestCase):
    def test_clear_sightline_over_single_low_block_vs_blocked_by_head_wall(self):
        # 站立眼高 65.62 可越过 (1, 64, 0) 的 1 格矮块看到 (2, 64, 0)
        world = GridWorld(pos=(0.5, 64.0, 0.5), blocks={(1, 64, 0): "stone", (2, 64, 0): "iron_ore"})
        ex, state = world.session()
        self.assertTrue(can_see_block(ex, state.st, [2, 64, 0]))
        self.assertTrue(can_reach_block(ex, state.st, [2, 64, 0]))

        # 再加 (1, 65, 0) 形成 2 格实心石墙后，视线被完全阻断
        world.blocks[(1, 65, 0)] = "stone"
        self.assertFalse(can_see_block(ex, state.st, [2, 64, 0]))
        self.assertFalse(can_reach_block(ex, state.st, [2, 64, 0]))

    def test_diagonal_kissing_wall_pinch_is_rejected(self):
        # (1, 64..65, 0) 与 (0, 64..65, 1) 对角封死，射线不得从两石块交界缝隙穿到 (1, 64, 1)
        world = GridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (1, 64, 0): "stone", (1, 65, 0): "stone",
                (0, 64, 1): "stone", (0, 65, 1): "stone",
                (1, 64, 1): "diamond_ore",
            },
        )
        ex, state = world.session()
        self.assertFalse(can_see_block(ex, state.st, [1, 64, 1], face_fallback=False))
        self.assertFalse(can_see_block(ex, state.st, [1, 64, 1], face_fallback=True))

    def test_staircase_overhang_ore_visible_via_exposed_face_when_enabled(self):
        # 复现阶梯探矿几何：玩家站在 (0.5, 64.0, 0.5)，(1, 63, 0) 已挖空，
        # (2, 63, 0) 为铁矿但正上方 (2, 64, 0) 为岩石顶板——中心连线擦过 (2, 64, 0)，但朝向玩家的 -X 暴露面完全可见
        world = GridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(1, 62, 0): "stone", (2, 62, 0): "stone", (2, 63, 0): "iron_ore", (2, 64, 0): "stone"},
        )
        ex, state = world.session()
        self.assertFalse(can_see_block(ex, state.st, [2, 63, 0], face_fallback=False))
        self.assertTrue(can_see_block(ex, state.st, [2, 63, 0], face_fallback=True))

    def test_out_of_reach_short_circuits_without_get_block_requests(self):
        class CountingBlockWorld(GridWorld):
            def __init__(self):
                super().__init__(pos=(0.5, 64.0, 0.5), blocks={(10, 64, 0): "stone"})
                self.block_reads = 0

            def get_block(self, pos):
                self.block_reads += 1
                return super().get_block(pos)

        world = CountingBlockWorld()
        ex, state = world.session()
        self.assertFalse(can_reach_block(ex, state.st, [10, 64, 0], reach=DIG_EYE_REACH))
        self.assertEqual(world.block_reads, 0)

    def test_block_reads_are_deduplicated_within_single_visibility_check(self):
        class CountingBlockWorld(GridWorld):
            def __init__(self):
                super().__init__(
                    pos=(0.5, 64.0, 0.5),
                    blocks={(1, 64, 0): "stone", (1, 65, 0): "stone", (2, 64, 0): "iron_ore"},
                )
                self.queried: list[tuple[int, int, int]] = []

            def get_block(self, pos):
                self.queried.append(tuple(pos))
                return super().get_block(pos)

        world = CountingBlockWorld()
        ex, state = world.session()
        self.assertFalse(can_see_block(ex, state.st, [2, 64, 0], face_fallback=True))
        self.assertEqual(len(world.queried), len(set(world.queried)))

    def test_waterlogged_and_unloaded_chunks_are_rejected_safely(self):
        class CustomBlockWorld(GridWorld):
            def get_block(self, pos):
                if tuple(pos) == (1, 65, 0):
                    return {"name": "oak_trapdoor", "properties": {"waterlogged": True}}
                if tuple(pos) == (-1, 65, 0):
                    raise ApiError("not_loaded", "chunk not loaded")
                return super().get_block(pos)

        world = CustomBlockWorld(pos=(0.5, 64.0, 0.5), blocks={(2, 64, 0): "diamond_ore", (-2, 64, 0): "iron_ore"})
        ex, state = world.session()
        self.assertFalse(
            can_reach_block(ex, state.st, [2, 64, 0], reject_waterlogged=True, face_fallback=False)
        )
        self.assertFalse(can_reach_block(ex, state.st, [-2, 64, 0]))

    def test_voxel_line_cells_includes_endpoints_and_simultaneous_crossings(self):
        cells = voxel_line_cells((0.5, 65.62, 0.5), (2.5, 65.62, 0.5))
        self.assertEqual(cells, [(0, 65, 0), (1, 65, 0), (2, 65, 0)])


if __name__ == "__main__":
    unittest.main()
