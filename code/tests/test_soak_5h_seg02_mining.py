"""5h soak seg-02 矿脉连采、水矿洞绕开与残留移动刹车离线单测（verified: offline）。

覆盖 seg-02 实测暴露的四项挖掘效率与稳定性问题：
1. 矿脉连采（vein sweep）：`SmeltIron(count>=2)` 挖满当前目标所需粗铁后，若紧邻 5 格内仍有
   同矿脉暴露铁矿，顺手一次挖完（受上限保护），避免为每 3 块铁重复下矿。
2. 掉落未即时入包容错（seg-02 t=843s）：合规石镐挖掉铁矿且无工具告警但 `raw_iron` 未立即增加时，
   若矿脉还有相邻铁矿或已采得粗铁，不应直接抛 Replan 中止整条矿脉。
3. 水矿洞直接绕开并记忆积水区块（seg-02 t=346s）：阶梯探矿遇水封死时不再在洞内反复回溯，
   立刻 `EnvBlocked` 切去别处，并记录积水区块，同区块内再次调用直接跳过不再重复挖进水洞。
4. 残留水平速度刹车（seg-02 t=1368s）：`move_to` 失败后或进入 `_prospect` 前若存在残留水平速度，
   主动调用 `ex.stop()` 刹停再观测站位。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcagent.runtime import EnvBlocked
from mcagent.skills.iron import SmeltIron, reset_iron_memo
from mcagent.skills.testing import drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches
from mcagent.skills.world_memo import reset_all_world_memos
from tests.test_iron_reach_integration import _ReachIronWorld, _furnace_view


class _VeinSweepWorld(_ReachIronWorld):
    def __init__(self) -> None:
        blocks = {
            (2, 64, 0): "iron_ore",
            (2, 64, 1): "iron_ore",
            (3, 64, 0): "iron_ore",
            (3, 64, 1): "iron_ore",
            (2, 65, 0): "iron_ore",
            (2, 65, 1): "iron_ore",
        }
        super().__init__(
            pos=(0.5, 64.0, 0.5),
            items={"stone_pickaxe": 1, "furnace": 1, "coal": 2},
            blocks=blocks,
            views=[_furnace_view(), _furnace_view(output=3), _furnace_view()],
            track_held=True,
        )
        self.st["held"] = {"name": "stone_pickaxe"}


class _UncollectedMidVeinWorld(_ReachIronWorld):
    """复现 seg-02 t=835..843s：第 2 块铁矿无告警但掉落未立刻入包，第 3 块正常掉落。"""

    def __init__(self) -> None:
        blocks = {
            (2, 64, 0): "iron_ore",
            (2, 64, 1): "iron_ore",
            (3, 64, 0): "iron_ore",
        }
        super().__init__(
            pos=(0.5, 64.0, 0.5),
            items={"stone_pickaxe": 1, "furnace": 1, "coal": 2},
            blocks=blocks,
            views=[_furnace_view(), _furnace_view(output=2), _furnace_view()],
            track_held=True,
        )
        self.st["held"] = {"name": "stone_pickaxe"}
        self._iron_digs = 0

    def dig(self, pos, collect=True, *, stop_on=None):
        target = tuple(pos)
        if self.blocks.get(target) == "iron_ore":
            self._iron_digs += 1
            if self._iron_digs == 2:
                # 无工具告警，方块被挖空，但掉落物暂时未入包
                self.calls.append(("dig", {"pos": list(pos), "collect": collect}))
                self.blocks.pop(target, None)
                return {"dug": "iron_ore", "tool": "stone_pickaxe", "gained": {}}
        return super().dig(pos, collect=collect, stop_on=stop_on)


class _FloodedCaveWorld(_ReachIronWorld):
    """复现 seg-02 t=346s：阶梯前方与周边被水矿洞封死。"""

    def __init__(self) -> None:
        blocks: dict[tuple[int, int, int], str] = {}
        # 脚下与周边填水，仅站立格 (0, 63, 0) 为实心石头
        for x in range(-3, 4):
            for z in range(-3, 4):
                for y in range(60, 66):
                    blocks[(x, y, z)] = "water"
        blocks[(0, 63, 0)] = "stone"
        blocks[(0, 64, 0)] = "air"
        blocks[(0, 65, 0)] = "air"
        super().__init__(
            pos=(0.5, 64.0, 0.5),
            items={"stone_pickaxe": 1, "furnace": 1, "coal": 2},
            blocks=blocks,
            track_held=True,
        )
        self.st["held"] = {"name": "stone_pickaxe"}


class _ResidualVelocityWorld(_ReachIronWorld):
    """复现 seg-02 t=1368s：进入 _prospect 时角色仍带有残留水平速度。"""

    def __init__(self) -> None:
        blocks: dict[tuple[int, int, int], str] = {}
        for x in range(-2, 6):
            for z in range(-2, 3):
                for y in range(60, 67):
                    blocks[(x, y, z)] = "stone"
        for y in (64, 65):
            blocks.pop((0, y, 0), None)
        blocks[(2, 63, 0)] = "iron_ore"
        super().__init__(
            pos=(0.5, 64.0, 0.5),
            items={"stone_pickaxe": 1, "furnace": 1, "coal": 1},
            blocks=blocks,
            views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
            track_held=True,
        )
        self.st["held"] = {"name": "stone_pickaxe"}
        self.st["velocity"] = [-3.5, 0.0, 0.5]
        self._ore_exposed = False
        self.stopped = 0

    def dig(self, pos, collect=True, *, stop_on=None):
        res = super().dig(pos, collect=collect, stop_on=stop_on)
        if tuple(pos) == (1, 63, 0):
            self._ore_exposed = True
        return res

    def find_blocks(self, types=None, radius=16, limit=20):
        if types and "iron_ore" in types and not self._ore_exposed:
            return {"blocks": []}
        return super().find_blocks(types=types, radius=radius, limit=limit)


class Soak5hSeg02MiningTests(unittest.TestCase):
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

    def test_smelt_iron_sweeps_adjacent_vein_ores_in_one_trip(self) -> None:
        """挖完目标所需 3 块铁后，应把紧邻同矿脉其余 3 块也顺手采完。"""
        world = _VeinSweepWorld()
        ex, state = world.session()
        drive(SmeltIron(count=3), ex, state)
        self.assertEqual(state.count("iron_ingot"), 3)
        self.assertEqual(state.count("raw_iron"), 3)
        remaining_ores = [p for p, n in world.blocks.items() if n == "iron_ore"]
        self.assertEqual(remaining_ores, [])

    def test_smelt_iron_skips_uncollected_mid_vein_drop_and_finishes_vein(self) -> None:
        """seg-02 t=843s：无告警但第 2 块掉落未入包时，继续挖第 3 块并完成冶炼。"""
        world = _UncollectedMidVeinWorld()
        ex, state = world.session()
        drive(SmeltIron(count=2), ex, state)
        self.assertEqual(state.count("iron_ingot"), 2)

    def test_smelt_iron_bypasses_flooded_cave_and_remembers_flooded_chunk(self) -> None:
        """seg-02 t=346s：水矿洞立即 EnvBlocked 绕开，同区块二次进入零开销直接跳过。"""
        world = _FloodedCaveWorld()
        ex, state = world.session()
        with self.assertRaises(EnvBlocked) as ctx1:
            drive(SmeltIron(count=3), ex, state)
        self.assertEqual(ctx1.exception.missing_predicate, "dry_iron_shaft")

        # 同区块再次尝试：直接识别已探明水矿洞区块，不再探测也不再开挖
        calls_before = len(world.calls)
        with self.assertRaises(EnvBlocked) as ctx2:
            drive(SmeltIron(count=3), ex, state)
        self.assertEqual(ctx2.exception.missing_predicate, "dry_iron_shaft")
        new_ops = [op for op, _ in world.calls[calls_before:]]
        self.assertNotIn("dig", new_ops)
        self.assertNotIn("move_to", new_ops)

    def test_smelt_iron_stops_residual_velocity_before_prospecting(self) -> None:
        """seg-02 t=1368s：存在残留水平速度进入探矿前必须先调用 ex.stop() 刹停。"""
        world = _ResidualVelocityWorld()
        ex, state = world.session()

        def _stop(*, raise_on_error=False):
            world.stopped += 1
            world.st["velocity"] = [0.0, 0.0, 0.0]

        ex.stop = _stop
        drive(SmeltIron(count=1), ex, state)
        self.assertGreaterEqual(world.stopped, 1)
        self.assertGreaterEqual(state.count("iron_ingot"), 1)


if __name__ == "__main__":
    unittest.main()
