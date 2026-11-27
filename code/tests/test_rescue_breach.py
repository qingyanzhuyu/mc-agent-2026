"""被困自救（rescue_breach_cell）的离线单测。

真机事故（2026-11-24）：bot 夜间挖洞封口后，逃敌技能每次冲撞还能挤出 0.4~0.57 格，
于是 escape.py 里"零位移"判定永不成立、破墙分支永不执行，17 次冲撞都困在原地。
引擎侧加了兜底：技能连续两次跑完净位移 < 1.5 格，就主动挖一格（头顶封口块优先）。
"""
from __future__ import annotations

import unittest

from mcagent.loop.engine import rescue_breach_cell

BOT = (21.3, 75.0, -6128.3)


def _reader(blocks):
    def read(cell):
        return blocks.get(tuple(cell))

    return read


class RescueBreachCellTest(unittest.TestCase):
    def test_prefers_the_seal_block_above_the_head(self):
        # 头两格是夜间"挖三填一"的泥土封口，四向全是石头
        blocks = {(21, 77, -6129): "dirt"}
        self.assertEqual(rescue_breach_cell(_reader(blocks), BOT), (21, 77, -6129))

    def test_falls_back_to_a_side_wall_when_the_ceiling_is_air(self):
        blocks = {(21, 77, -6129): "air", (21, 76, -6129): "cave_air", (22, 75, -6129): "cobblestone"}
        self.assertEqual(rescue_breach_cell(_reader(blocks), BOT), (22, 75, -6129))

    def test_never_digs_unbreakable_or_fluid_blocks(self):
        blocks = {
            (21, 77, -6129): "bedrock",
            (22, 75, -6129): "water",
            (20, 75, -6129): "lava",
            (21, 75, -6128): "cobblestone",
        }
        self.assertEqual(rescue_breach_cell(_reader(blocks), BOT), (21, 75, -6128))

    def test_returns_none_when_everything_around_is_open(self):
        blocks = {(21, 77, -6129): "air", (22, 75, -6129): "air"}
        self.assertIsNone(rescue_breach_cell(_reader(blocks), BOT))

    def test_tolerates_unreadable_and_bad_positions(self):
        self.assertIsNone(rescue_breach_cell(lambda cell: None, BOT))
        self.assertIsNone(rescue_breach_cell(lambda cell: "stone", None))
        self.assertIsNone(rescue_breach_cell(lambda cell: "stone", ("x", "y", "z")))


if __name__ == "__main__":
    unittest.main()
