"""5h soak seg-06 故障回归离线单测（verified: offline）。

覆盖 seg-06 实测确认的两类根因：
1. `mining.SOLID_SUPPORT` 缺少 `smooth_basalt` / `amethyst_block` / `sandstone` 等常见无重力地下岩层，
   导致阶梯探矿在 Y=40 碰到紫水晶晶洞外层即误判不安全并停挖。
2. `SmeltIron._prospect()` 干矿井走完触发 `EnvBlocked` 后，爬回矿口 `start_chunk` 会再次满足
   `chunk_moved` 唤醒条件，导致重复走下同一条已挖空的 30 级死胡同阶梯 5 次；应记录已耗尽探矿井口区块，
   回到同一井口区块时零开销直接跳过并外迁。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcagent.runtime import EnvBlocked
from mcagent.skills.iron import SmeltIron, reset_iron_memo
from mcagent.skills.mining import safe_mine_column
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches
from mcagent.skills.world_memo import reset_all_world_memos
from tests.test_iron_reach_integration import _ReachIronWorld


class _DryExhaustedShaftWorld(_ReachIronWorld):
    """复现 seg-06 t=642..1066s：从 (-86, 68, 11) 挖下 2 级到达空矿底后 EnvBlocked，再退回矿口。"""

    def __init__(self) -> None:
        blocks = {
            (-86, 67, 11): "stone",
            (-85, 66, 11): "stone", (-85, 67, 11): "stone", (-85, 68, 11): "stone",
            (-85, 69, 11): "stone", (-85, 70, 11): "stone",
        }
        super().__init__(
            pos=(-85.5, 68.0, 11.5),
            items={"stone_pickaxe": 1, "furnace": 1, "coal": 2},
            blocks=blocks,
            track_held=True,
        )
        self.st["held"] = {"name": "stone_pickaxe"}


class Soak5hSeg06MiningTests(unittest.TestCase):
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

    def test_smooth_basalt_geode_and_sandstone_are_safe_stair_support(self) -> None:
        for rock in ("smooth_basalt", "amethyst_block", "sandstone", "diamond_ore", "deepslate_diamond_ore"):
            with self.subTest(rock=rock):
                blocks = {
                    (1, 38, 0): rock,
                    (1, 39, 0): rock,
                    (1, 40, 0): "stone",
                    (1, 41, 0): "stone",
                    (1, 42, 0): "stone",
                }
                world = GridWorld(pos=(0.5, 40.0, 0.5), blocks=blocks)
                ex, _ = world.session()
                self.assertTrue(safe_mine_column(ex, [1, 39, 0]), f"{rock} 应允许作为安全阶梯岩层")

    def test_exhausted_dry_shaft_start_chunk_is_not_re_entered_after_climbing_back(self) -> None:
        world = _DryExhaustedShaftWorld()
        ex, state = world.session()

        # 第 1 次下矿：挖 1 级后在无支撑处触发 EnvBlocked
        with self.assertRaises(EnvBlocked):
            drive(SmeltIron(count=3), ex, state)
        self.assertTrue(any(op == "move_to" for op, _ in world.calls))

        # 模拟 scout_island 沿 mine_trail 爬回原矿口 (-85.5, 68.0, 11.5)
        world.st["pos"] = [-85.5, 68.0, 11.5]
        calls_before = len(world.calls)

        # 第 2 次在同一矿口区块调用：必须直接跳过，不得再次走下同一条空阶梯
        with self.assertRaises(EnvBlocked):
            drive(SmeltIron(count=3), ex, state)
        new_ops = [op for op, _ in world.calls[calls_before:]]
        self.assertNotIn("dig", new_ops)
        self.assertNotIn("move_to", new_ops)


if __name__ == "__main__":
    unittest.main()
