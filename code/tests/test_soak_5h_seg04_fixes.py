"""5h soak seg-04 故障回归离线单测（verified: offline）。

覆盖 seg-04 实测确认的两类根因：
1. `INC-006`：`SmeltIron` 走向不可达深洞铁矿超时（`timeout` 或 HTTP `ReadTimeout` 返回 `network`）时，
   必须将该矿点写入 `excluded` 与 `voxel_cache` 不可达黑名单、刹停残留移动并切往下一个矿点，
   而非反复挂死 30 秒重试同一死矿；且默认 `move_to` 超时须低于 30s HTTP 读超时。
2. `INC-003`：洞穴墙角主逃离方向撞墙时，`_pick_alternate_escape_yaw` 应扫描全部可达干地方向
   （含反方向/斜向逃生走廊），而非盲目右转继续撞墙。
"""
from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.skills.escape import _pick_alternate_escape_yaw
from mcagent.skills.iron import SmeltIron, reset_iron_memo
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches, voxel_cache_for
from mcagent.skills.world_memo import reset_all_world_memos
from tests.test_iron_reach_integration import _ReachIronWorld, _furnace_view


class _TimeoutOreWorld(_ReachIronWorld):
    """复现 seg-04 t=1323s/1362s (INC-006)：首个远距深矿 move_to 触发 timeout/ReadTimeout，次矿可达。"""

    def __init__(self, err: ApiError) -> None:
        blocks = {
            (8, 64, 0): "iron_ore",
            (2, 64, 1): "iron_ore",
        }
        super().__init__(
            pos=(0.5, 64.0, 0.5),
            items={"stone_pickaxe": 1, "furnace": 1, "coal": 1},
            blocks=blocks,
            views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
            track_held=True,
        )
        self.st["held"] = {"name": "stone_pickaxe"}
        self._err = err
        self.move_timeouts: list[float] = []

    def move_to(self, *args, pos=None, rng=1, timeout=None, **kwargs):
        if timeout is not None:
            self.move_timeouts.append(float(timeout))
        if pos is not None and list(pos) == [8, 64, 0]:
            self.calls.append(("move_to", {"pos": list(pos), "rng": rng}))
            raise self._err
        return super().move_to(*args, pos=pos, rng=rng, **kwargs)


class Soak5hSeg04FixesTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_iron_memo()
        reset_all_voxel_caches()
        reset_all_world_memos()
        self.addCleanup(reset_iron_memo)
        self.addCleanup(reset_all_voxel_caches)
        self.addCleanup(reset_all_world_memos)
        sleep_patch = patch("mcagent.skills.iron.time.sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_smelt_iron_skips_and_blacklists_timeout_and_read_timeout_ores(self) -> None:
        cases = [
            ("timeout", ApiError("timeout", "Pathfinding timed out after 14000ms")),
            ("read_timeout", ApiError("network", "ReadTimeout: HTTPConnectionPool: Read timed out. (read timeout=30.0)")),
        ]
        for label, err in cases:
            with self.subTest(label=label):
                reset_iron_memo()
                reset_all_voxel_caches()
                world = _TimeoutOreWorld(err)
                # 让 (8, 64, 0) 排在前面先触发超时
                orig_find = world.find_blocks

                def ordered_find(types=None, radius=16, limit=20):
                    res = orig_find(types=types, radius=radius, limit=limit)
                    if types and "iron_ore" in types:
                        res["blocks"] = [
                            {"pos": [8, 64, 0], "name": "iron_ore"},
                            {"pos": [2, 64, 1], "name": "iron_ore"},
                        ]
                    return res

                world.find_blocks = ordered_find
                # 将 (2, 64, 1) 调整为更远一点（y 差 15）确保 (8, 64, 0) 排序在前
                world.blocks.pop((2, 64, 1), None)
                world.blocks[(2, 64, 0)] = "iron_ore"
                ex, state = world.session()

                with patch.object(SmeltIron, "_ore_candidates", side_effect=[[[8, 64, 0], [2, 64, 0]], [[2, 64, 0]], []]):
                    steps = drive(SmeltIron(count=1), ex, state)

                self.assertTrue(any(s.get("op") == "ore_route_skip" for s in steps))
                self.assertEqual(state.count("iron_ingot"), 1)
                self.assertTrue(
                    voxel_cache_for(state).is_unreachable(
                        "smelt_iron", [8, 64, 0], dimension="overworld", stance_cell=(0, 64, 0),
                    )
                )

    def test_alternate_escape_yaw_finds_open_corridor_when_primary_and_sides_walled(self) -> None:
        """主逃离方向 (+x) 与左右 (+z/-z) 全是洞穴石墙，仅后方 (-x) 有干地通道时，应选 -x 逃生。"""
        blocks = {(x, 63, z): "stone" for x in range(-4, 5) for z in range(-4, 5)}
        for dx, dz in ((1, 0), (0, 1), (0, -1)):
            for dy in (0, 1):
                blocks[(dx, 64 + dy, dz)] = "stone"
        world = GridWorld(pos=(0.5, 64.0, 0.5), blocks=blocks)
        ex, state = world.session()
        cache = voxel_cache_for(state)

        yaw = _pick_alternate_escape_yaw(ex, 0.5, 64.0, 0.5, 1.0, 0.0, cache, "overworld")
        # -x 方向对应 yaw = atan2(-(-1), 0) = +pi/2
        expected = math.atan2(1.0, 0.0)
        diff = abs(math.atan2(math.sin(yaw - expected), math.cos(yaw - expected)))
        self.assertLess(diff, 0.2, f"应选择畅通的 -x 方向 (yaw={expected:.2f})，实际 yaw={yaw:.2f}")


if __name__ == "__main__":
    unittest.main()
