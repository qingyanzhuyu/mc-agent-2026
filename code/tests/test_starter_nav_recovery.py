"""starter_skills 与 nav 模块在积雪平原（snowy plains）、移动卡顿（stalled）及地形脱困单测。

覆盖契约：
1. is_clear_space 与 ScoutIsland 头顶净空检查（包含薄雪 layers=1/2 与短草短干草等无害植被）；
2. ScoutIsland 与 GatherWood 遭遇 stalled 时的 nav.recover_move 现场脱困与单次上限防死循环；
3. ScoutIsland 结合 nav.breach_1x2、nav.step_up_1x2、nav.sneak_bridge 与 not cands 的 L2 逃逸（含 unmocked 与 mock-wrapped 确定性测试）；
4. CraftGear 与 MineStone 捕获 stalled 并正确回退（换工作台/换石头）。
"""
from __future__ import annotations

import math
from typing import Any, Callable, Iterator, Optional, Sequence
import unittest
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.loop import reset_world_memo
from mcagent.loop.starter_skills import (
    KNOWN_TABLES,
    PROTECTED_ZONES,
    VISITED_ANCHORS,
    _is_protected,
    has_known_table,
)
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills import nav
from mcagent.skills.base import _COOLDOWN, failure, rec
from mcagent.skills.items import AIR, NAV_FAIL_CODES, is_clear_space
from mcagent.skills.testing import drive
from mcagent.skills.world_memo import memo_for
from tests.test_loop_soak import RichGridWorld


class NavRecoverySkillWorld(RichGridWorld):
    """支持方块属性、move_to 故障注入与位置变动的离线单测夹具。"""

    def __init__(
        self,
        *,
        pos=(0.5, 64.0, 0.5),
        blocks=None,
        block_properties=None,
        items=None,
        move_failures=None,
        move_handler=None,
        keep_inventory: bool = True,
    ):
        super().__init__(pos=pos, blocks=blocks, items=items, keep_inventory=keep_inventory)
        self.block_properties: dict[tuple[int, int, int], dict] = {}
        if block_properties:
            for k, v in block_properties.items():
                self.block_properties[self._norm_coord(k)] = dict(v)
        self.move_failures: dict[tuple, Any] = {}
        if move_failures:
            for k, v in move_failures.items():
                if isinstance(k, (tuple, list)):
                    self.move_failures[tuple(k)] = v
                else:
                    self.move_failures[k] = v
        self.move_handler: Optional[Callable] = move_handler
        self.move_count = 0

    @staticmethod
    def _norm_coord(pos: Sequence[float]) -> tuple[int, int, int]:
        return (int(math.floor(pos[0])), int(math.floor(pos[1])), int(math.floor(pos[2])))

    @staticmethod
    def _int_coord(pos: Sequence[float]) -> tuple[int, int, int]:
        return (int(pos[0]), int(pos[1]), int(pos[2]))

    def get_block(self, pos) -> dict:
        k = tuple(pos)
        norm_k = self._norm_coord(pos)
        int_k = self._int_coord(pos)

        name = self.blocks.get(k)
        if name is None:
            name = self.blocks.get(norm_k)
        if name is None:
            name = self.blocks.get(int_k, "air")

        props = self.block_properties.get(k)
        if props is None:
            props = self.block_properties.get(norm_k)
        if props is None:
            props = self.block_properties.get(int_k)

        res = {"name": name}
        if props is not None:
            res["properties"] = dict(props)
        return res

    def dig(self, pos, collect=True, *, stop_on=None) -> dict:
        k = tuple(pos)
        norm_k = self._norm_coord(pos)
        int_k = self._int_coord(pos)

        self.block_properties.pop(k, None)
        self.block_properties.pop(norm_k, None)
        self.block_properties.pop(int_k, None)

        if k not in self.blocks:
            if norm_k in self.blocks:
                self.blocks[k] = self.blocks.pop(norm_k)
            elif int_k in self.blocks:
                self.blocks[k] = self.blocks.pop(int_k)

        return super().dig(pos, collect=collect, stop_on=stop_on)

    def place(self, item, pos=None, *, stop_on=None) -> dict:
        if pos is None:
            ix, iy, iz = self._norm_coord(self.st["pos"])
            for dx, dz in ((1, 0), (0, 1), (-1, 0), (0, -1)):
                cand = (ix + dx, iy, iz + dz)
                if cand not in self.blocks or self.blocks[cand] == "air":
                    pos = list(cand)
                    break
            if pos is None:
                pos = [ix + 1, iy, iz]
        return super().place(item, pos=pos, stop_on=stop_on)

    def move_to(
        self,
        x=None,
        z=None,
        y=None,
        entity_id=None,
        rng=1,
        pos=None,
        timeout=None,
        *,
        stop_on=None,
    ) -> dict:
        self.move_count += 1
        dest = [float(v) for v in pos] if pos is not None else [
            float(x),
            float(self.st["pos"][1] if y is None else y),
            float(z),
        ]

        if self.move_handler is not None:
            res = self.move_handler(dest, rng=rng, move_count=self.move_count)
            if res is not None:
                return res

        k = tuple(dest)
        norm_k = self._norm_coord(dest)
        int_k = self._int_coord(dest)

        err = (
            self.move_failures.get(k)
            or self.move_failures.get(norm_k)
            or self.move_failures.get(int_k)
            or self.move_failures.get("*")
        )
        if isinstance(err, list):
            err = err.pop(0) if err else None
        elif callable(err):
            err = err(dest)

        if err:
            self.calls.append(("move_to", {"pos": list(dest), "x": x, "y": y, "z": z}))
            raise ApiError(err, f"injected navigation failure: {err}")

        return super().move_to(
            x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on
        )


class SnowyPlainsScoutHeadroomTests(unittest.TestCase):
    """验证积雪平原地形下 ScoutIsland 的头顶净空检查（is_clear_space）。"""

    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()
        VISITED_ANCHORS.clear()
        KNOWN_TABLES.clear()
        PROTECTED_ZONES.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()
        VISITED_ANCHORS.clear()

    def test_is_clear_space_contract(self):
        # 常见空气与无害地被植物
        self.assertTrue(is_clear_space("air"))
        self.assertTrue(is_clear_space({"name": "air"}))
        self.assertTrue(is_clear_space("short_grass"))
        self.assertTrue(is_clear_space({"name": "short_grass"}))
        self.assertTrue(is_clear_space("fern"))
        self.assertTrue(is_clear_space("leaf_litter"))

        # 薄雪 layers <= 2：支持 int 1 和 2
        self.assertTrue(is_clear_space({"name": "snow", "properties": {"layers": 1}}))
        self.assertTrue(is_clear_space({"name": "snow", "properties": {"layers": 2}}))

        # 厚雪 layers >= 3
        self.assertFalse(is_clear_space({"name": "snow", "properties": {"layers": 3}}))
        self.assertFalse(is_clear_space({"name": "snow", "properties": {"layers": 4}}))

        # 树叶、水与实体硬块不可作为头顶净空
        self.assertFalse(is_clear_space("oak_leaves"))
        self.assertFalse(is_clear_space({"name": "oak_leaves"}))
        self.assertFalse(is_clear_space("water"))
        self.assertFalse(is_clear_space({"name": "water"}))
        self.assertFalse(is_clear_space("stone"))

    def test_scout_island_snowy_plains_thin_snow_and_short_grass_candidates(self):
        # 生成 16 个互隔 >=3 格、距离 (0.5, 64.0, 0.5) 在 5~28 格范围内的 grass_block 候选点
        blocks = {}
        props = {}
        for i in range(16):
            angle = 2.0 * math.pi * i / 16.0
            bx = int(round(14.0 * math.cos(angle)))
            bz = int(round(14.0 * math.sin(angle)))
            blocks[(bx, 63, bz)] = "grass_block"

            # 头顶 dy=1 覆盖 thin snow (layers=1 或 layers=2) 或 short_grass
            if i % 3 == 0:
                blocks[(bx, 64, bz)] = "snow"
                props[(bx, 64, bz)] = {"layers": 1}
            elif i % 3 == 1:
                blocks[(bx, 64, bz)] = "snow"
                props[(bx, 64, bz)] = {"layers": 2}
            else:
                blocks[(bx, 64, bz)] = "short_grass"
            # dy=2 保持空气

        world = NavRecoverySkillWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            block_properties=props,
        )
        ex, state = world.session()

        steps = drive(make("scout_island", {"radius": 32}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to"])
        self.assertEqual(len(VISITED_ANCHORS), 1)
        self.assertEqual(ex.acts, ["move_to"])

    def test_scout_island_skips_thick_snow_leaves_and_water_headroom(self):
        # 安排 5 个被阻挡的候选点和 1 个合法的薄雪候选点
        blocks = {
            (20, 63, 0): "grass_block",  # 候选项1：dy=1 为 4 层厚雪
            (20, 64, 0): "snow",
            (16, 63, 0): "grass_block",  # 候选项2：dy=1 为 3 层厚雪
            (16, 64, 0): "snow",
            (12, 63, 0): "grass_block",  # 候选项3：dy=1 为橡木树叶
            (12, 64, 0): "oak_leaves",
            (-16, 63, 0): "grass_block",  # 候选项4：dy=1 为水
            (-16, 64, 0): "water",
            (0, 63, 14): "grass_block",  # 候选项5：dy=1 为草但 dy=2 为橡木树叶
            (0, 64, 14): "short_grass",
            (0, 65, 14): "oak_leaves",
            (0, 63, -10): "grass_block",  # 候选项6（唯一合法）：dy=1 薄雪(layers=2)，dy=2 空气
            (0, 64, -10): "snow",
        }
        props = {
            (20, 64, 0): {"layers": 4},
            (16, 64, 0): {"layers": 3},
            (0, 64, -10): {"layers": 2},
        }
        world = NavRecoverySkillWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            block_properties=props,
        )
        ex, state = world.session()

        steps = drive(make("scout_island", {"radius": 32}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to"])
        self.assertEqual(steps[0]["pos"], [0.5, 64.0, -9.5])
        self.assertEqual(VISITED_ANCHORS, [(0.5, -9.5)])


class StalledNavRecoveryIntegrationTests(unittest.TestCase):
    """ScoutIsland 与 GatherWood 处理 stalled 时的 nav.recover_move 集成测试。"""

    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()
        VISITED_ANCHORS.clear()
        KNOWN_TABLES.clear()
        PROTECTED_ZONES.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()
        VISITED_ANCHORS.clear()

    def test_scout_island_stalled_snow_feet_unmocked_recover_move(self):
        """复现 ep-0010: bot 停在雪地 (-36.3, 71.0, 2.3)，脚底是 snow layers='1'，move_to 报错 stalled。"""
        blocks = {
            (-37, 70, 2): "grass_block",
            (-37, 71, 2): "snow",  # 脚下雪
            (-36, 70, 2): "grass_block",
            (-38, 70, 2): "grass_block",
            (-37, 70, 3): "grass_block",
            (-37, 70, 1): "grass_block",
            (-20, 70, 2): "grass_block",  # 远端探索候选
        }
        props = {
            (-37, 71, 2): {"layers": 1},
        }
        world = NavRecoverySkillWorld(
            pos=(-36.3, 71.0, 2.3),
            blocks=blocks,
            block_properties=props,
            move_failures={(-19.5, 71.0, 2.5): "stalled"},
        )
        ex, state = world.session()

        steps = drive(make("scout_island", {"radius": 32}), ex, state)
        self.assertGreaterEqual(len(steps), 2)
        # 第一步 move_to 汇报 stalled
        self.assertEqual(steps[0]["op"], "move_to")
        self.assertEqual(steps[0]["error_code"], "stalled")
        # 成功脱困并登记 VISITED_ANCHORS
        self.assertEqual(len(VISITED_ANCHORS), 1)
        esc_x, esc_z = VISITED_ANCHORS[0]
        self.assertGreaterEqual(math.hypot(esc_x - (-36.3), esc_z - 2.3), 0.8)
        # 强化断言：确认脚下雪被成功挖掘清除
        self.assertIn(("dig", {"pos": [-37, 71, 2]}), world.calls)
        self.assertNotIn((-37, 71, 2), world.blocks)

    def test_gather_wood_stalled_unmocked_recover_move_and_retry(self):
        """GatherWood 移动前往原木遇到 stalled，脱困 >=0.8m 后不拉黑目标并重试成功砍伐。"""
        blocks = {
            (-37, 70, 2): "grass_block",
            (-37, 71, 2): "snow",
            (-36, 70, 2): "grass_block",
            (-38, 70, 2): "grass_block",
            (-37, 70, 3): "grass_block",
            (-37, 70, 1): "grass_block",
            (-28, 71, 2): "oak_log",  # 目标原木
        }
        props = {
            (-37, 71, 2): {"layers": 1},
        }
        # 第一次前往原木产生 stalled 错误，脱困重试时成功
        world = NavRecoverySkillWorld(
            pos=(-36.3, 71.0, 2.3),
            blocks=blocks,
            block_properties=props,
            move_failures={(-28.0, 71.0, 2.0): ["stalled"]},
        )
        ex, state = world.session()

        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        ops = [s["op"] for s in steps]
        self.assertIn("move_to", ops)
        self.assertEqual(steps[0]["error_code"], "stalled")
        self.assertIn("dig", ops)
        self.assertEqual(state.count("oak_log"), 1)
        # 目标未被 mark_unreachable 拉黑，而是成功砍伐后标为 depleted
        entry = memo_for(state)._entries.get(("overworld", "wood", (-28, 71, 2)))
        self.assertIsNotNone(entry)
        self.assertEqual(entry.status, "depleted")
        self.assertNotEqual(entry.status, "unreachable")
        self.assertEqual(entry.error_code, "")
        # 强化断言：确认脚下雪被清除
        self.assertIn(("dig", {"pos": [-37, 71, 2]}), world.calls)
        self.assertNotIn((-37, 71, 2), world.blocks)

    def test_gather_wood_stalled_recovery_bounded_to_once_per_run(self):
        """GatherWood 的 stalled 脱困每次 run() 最多触发一次；若重试依然 stalled 则拉黑并抛 Replan。"""
        blocks = {
            (0, 63, 0): "grass_block",
            (8, 64, 0): "oak_log",
        }
        world = NavRecoverySkillWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            move_failures={(8.0, 64.0, 0.0): "stalled"},
        )
        ex, state = world.session()

        recovery_invocations = 0

        def dummy_recover(ex_in, state_in, dest=None, *, protected_fn=None, min_displacement=0.8):
            nonlocal recovery_invocations
            recovery_invocations += 1
            pos = [1.5, 64.0, 0.5]
            yield rec("move_to_recover", ex_in.move_to(pos=pos, rng=0.5), pos=pos)

        with patch.object(nav, "recover_move", side_effect=dummy_recover, create=True):
            with self.assertRaises(Replan):
                drive(make("gather_wood", {"count": 1}), ex, state)

        self.assertEqual(recovery_invocations, 1)
        self.assertTrue(memo_for(state).is_blocked((8, 64, 0), category="wood"))

    def test_gather_wood_stalled_recovery_fails_displacement_bound(self):
        """若 stalled 脱困位移不足 0.8m，不重试目标且立即将目标拉黑进台账。"""
        blocks = {
            (0, 63, 0): "grass_block",
            (8, 64, 0): "oak_log",
        }
        world = NavRecoverySkillWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            move_failures={(8.0, 64.0, 0.0): "stalled"},
        )
        ex, state = world.session()

        def dummy_recover_stuck(ex_in, state_in, dest=None, *, protected_fn=None, min_displacement=0.8):
            pos = [0.6, 64.0, 0.5]
            yield rec("move_to_recover", ex_in.move_to(pos=pos, rng=0.5), pos=pos)

        with patch.object(nav, "recover_move", side_effect=dummy_recover_stuck, create=True):
            with self.assertRaises(Replan):
                drive(make("gather_wood", {"count": 1}), ex, state)

        self.assertTrue(memo_for(state).is_blocked((8, 64, 0), category="wood"))

    def test_gather_wood_path_not_found_never_invokes_recover_move(self):
        """path_not_found 失败直接拉黑目标，绝不调用 nav.recover_move。"""
        blocks = {
            (0, 63, 0): "grass_block",
            (8, 64, 0): "oak_log",
        }
        world = NavRecoverySkillWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            move_failures={(8.0, 64.0, 0.0): "path_not_found"},
        )
        ex, state = world.session()

        with patch.object(nav, "recover_move", create=True) as mock_recover:
            with self.assertRaises(Replan):
                drive(make("gather_wood", {"count": 1}), ex, state)
            mock_recover.assert_not_called()

        self.assertTrue(memo_for(state).is_blocked((8, 64, 0), category="wood"))


class ScoutIslandL2PrimitivesRecoveryTests(unittest.TestCase):
    """ScoutIsland 结合 breach_1x2、step_up_1x2、sneak_bridge 与 not cands 的脱困。"""

    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()
        VISITED_ANCHORS.clear()
        KNOWN_TABLES.clear()
        PROTECTED_ZONES.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()
        VISITED_ANCHORS.clear()

    def test_scout_island_escaping_stalled_via_unmocked_breach_1x2(self):
        """ScoutIsland 遇 stalled 后通过真实 nav.recover_move (breach_1x2) 挖开泥土墙脱困。"""
        blocks = {
            (0, 63, 0): "grass_block",  # 站立脚下
            (1, 63, 0): "grass_block",  # 待破拆列的底面承重
            (1, 64, 0): "dirt",         # 挡路下半格
            (1, 65, 0): "dirt",         # 挡路上半格
            (-1, 64, 0): "obsidian",    # 其余三面用黑曜石墙围住（不可破拆，无相邻平地）
            (-1, 65, 0): "obsidian",
            (0, 64, 1): "obsidian",
            (0, 65, 1): "obsidian",
            (0, 64, -1): "obsidian",
            (0, 65, -1): "obsidian",
            (10, 63, 0): "grass_block", # 远端探索候选
        }
        world = NavRecoverySkillWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"wooden_pickaxe": 1},
            move_failures={(10.5, 64.0, 0.5): "stalled"},
        )
        ex, state = world.session()

        steps = drive(make("scout_island", {"radius": 32}), ex, state)
        ops = [s["op"] for s in steps]
        self.assertEqual(steps[0]["op"], "move_to")
        self.assertEqual(steps[0]["error_code"], "stalled")
        self.assertIn("dig", ops)
        self.assertNotIn((1, 64, 0), world.blocks)
        self.assertNotIn((1, 65, 0), world.blocks)
        self.assertIn((-1, 64, 0), world.blocks)
        self.assertEqual(VISITED_ANCHORS, [(1.5, 0.5)])

    def test_scout_island_escaping_stalled_via_unmocked_step_up_1x2(self):
        """ScoutIsland 遇 stalled 后通过真实 nav.recover_move (step_up_1x2) 垫高翻越 2 格黑曜石高台。"""
        blocks = {
            (0, 63, 0): "grass_block",   # 站立地面
            (1, 63, 0): "grass_block",   # 高台底座
            (1, 64, 0): "obsidian",      # 2格高台下半
            (1, 65, 0): "obsidian",      # 2格高台上半（台面 y=66）
            (-1, 63, 0): "grass_block",  # 侧向垫脚立柱底板
            (0, 64, 1): "obsidian",      # 侧面围墙
            (0, 65, 1): "obsidian",
            (0, 64, -1): "obsidian",
            (0, 65, -1): "obsidian",
            (10, 63, 0): "grass_block",  # 远端探索候选
        }
        world = NavRecoverySkillWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"cobblestone": 5},
            move_failures={
                (10.5, 64.0, 0.5): "stalled",
                (-0.5, 64.0, 0.5): "stalled",  # 阻止地面侧向步进，迫使进入 Step 5 step_up_1x2
            },
        )
        ex, state = world.session()

        steps = drive(make("scout_island", {"radius": 32}), ex, state)
        ops = [s["op"] for s in steps]
        self.assertEqual(steps[0]["op"], "move_to")
        self.assertEqual(steps[0]["error_code"], "stalled")
        self.assertIn("place", ops)
        self.assertEqual(world.st["pos"], [1.5, 66.0, 0.5])
        self.assertEqual(VISITED_ANCHORS, [(1.5, 0.5)])

    def test_scout_island_escaping_stalled_via_unmocked_sneak_bridge(self):
        """ScoutIsland 遇 stalled 后通过真实 nav.recover_move (sneak_bridge) 潜行铺桥跨越 1 格空隙。"""
        blocks = {
            (0, 63, 0): "grass_block",   # 站立地面
            # (1, 63, 0) 为空气（1格空隙）
            (2, 63, 0): "grass_block",   # 对岸安全立足陆地（距自身 1.5 < 5.0，不会被选为 scout 候选）
            (-1, 64, 0): "obsidian",     # 其余三面黑曜石围住
            (-1, 65, 0): "obsidian",
            (0, 64, 1): "obsidian",
            (0, 65, 1): "obsidian",
            (0, 64, -1): "obsidian",
            (0, 65, -1): "obsidian",
            (10, 63, 0): "grass_block",  # 远端探索候选
        }
        world = NavRecoverySkillWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"cobblestone": 5},
            move_failures={(10.5, 64.0, 0.5): "stalled"},
        )
        ex, state = world.session()

        steps = drive(make("scout_island", {"radius": 32}), ex, state)
        ops = [s["op"] for s in steps]
        self.assertEqual(steps[0]["op"], "move_to")
        self.assertEqual(steps[0]["error_code"], "stalled")
        self.assertEqual(world.blocks.get((1, 63, 0)), "cobblestone")
        self.assertFalse(world.st["sneaking"])
        self.assertEqual(VISITED_ANCHORS, [(2.5, 0.5)])

    def test_scout_island_stalled_recovery_bounded_to_once_per_run(self):
        """ScoutIsland 首次 stalled 触发脱困但位移不足 0.8m，第二次候选仍 stalled 时不再重复调用脱困，以 Replan 结束。"""
        blocks = {
            (10, 63, 0): "grass_block",
            (14, 63, 0): "grass_block",
        }
        world = NavRecoverySkillWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            move_failures={
                (10.5, 64.0, 0.5): "stalled",
                (14.5, 64.0, 0.5): "stalled",
            },
        )
        ex, state = world.session()

        recovery_calls = 0

        def dummy_recover_stuck(ex_in, state_in, dest=None, *, protected_fn=None, min_displacement=0.8):
            nonlocal recovery_calls
            recovery_calls += 1
            pos = [0.6, 64.0, 0.5]
            yield rec("move_to_recover", ex_in.move_to(pos=pos, rng=0.5), pos=pos)

        with patch.object(nav, "recover_move", side_effect=dummy_recover_stuck, create=True):
            with self.assertRaises(Replan):
                drive(make("scout_island", {"radius": 32}), ex, state)

        self.assertEqual(recovery_calls, 1)
        self.assertEqual(VISITED_ANCHORS, [])

    def test_scout_island_escaping_stalled_via_breach_1x2(self):
        """ScoutIsland 遇 stalled 后通过 mock-wrapped breach_1x2 破壁脱困，验证一步一动作与锚点登记。"""
        blocks = {
            (0, 63, 0): "grass_block",
            (1, 64, 0): "dirt",
            (1, 65, 0): "dirt",
            (10, 63, 0): "grass_block",
        }
        world = NavRecoverySkillWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"wooden_pickaxe": 1},
            move_failures={(10.5, 64.0, 0.5): "stalled"},
        )
        ex, state = world.session()

        def recover_with_breach(ex_in, state_in, dest=None, *, protected_fn=None, min_displacement=0.8):
            self.assertIsNotNone(protected_fn)
            yield from nav.breach_1x2(ex_in, state_in, [(1, 65, 0), (1, 64, 0)])
            pos = [1.5, 64.0, 0.5]
            yield rec("move_to_recover", ex_in.move_to(pos=pos, rng=0.5), pos=pos)

        with patch.object(nav, "recover_move", side_effect=recover_with_breach, create=True):
            steps = drive(make("scout_island", {"radius": 32}), ex, state)

        self.assertEqual(steps[0]["op"], "move_to")
        self.assertEqual(steps[0]["error_code"], "stalled")
        self.assertNotIn((1, 64, 0), world.blocks)
        self.assertNotIn((1, 65, 0), world.blocks)
        self.assertEqual(VISITED_ANCHORS, [(1.5, 0.5)])

    def test_scout_island_escaping_stalled_via_step_up_1x2(self):
        """ScoutIsland 遇 stalled 后通过真实的 nav.step_up_1x2 垫高脱困，验证一步一动作与 protected_fn。"""
        blocks = {
            (0, 63, 0): "grass_block",
            (10, 63, 0): "grass_block",
        }
        world = NavRecoverySkillWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"dirt": 5},
            move_failures={(10.5, 64.0, 0.5): "stalled"},
        )
        ex, state = world.session()

        def recover_with_step_up(ex_in, state_in, dest=None, *, protected_fn=None, min_displacement=0.8):
            self.assertIsNotNone(protected_fn)
            yield from nav.step_up_1x2(ex_in, state_in, [[1, 64, 0], [0, 65, 0]])

        with patch.object(nav, "recover_move", side_effect=recover_with_step_up, create=True):
            steps = drive(make("scout_island", {"radius": 32}), ex, state)

        self.assertEqual(steps[0]["op"], "move_to")
        self.assertEqual(steps[0]["error_code"], "stalled")
        ops = [s["op"] for s in steps]
        self.assertIn("place", ops)
        self.assertEqual(VISITED_ANCHORS, [(1.5, 0.5)])

    def test_scout_island_escaping_stalled_via_sneak_bridge(self):
        """ScoutIsland 遇 stalled 后通过 mock-wrapped nav.sneak_bridge 铺桥脱困，确认松开潜行。"""
        blocks = {
            (0, 63, 0): "grass_block",
            (10, 63, 0): "grass_block",
        }
        world = NavRecoverySkillWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"cobblestone": 5},
            move_failures={(10.5, 64.0, 0.5): "stalled"},
        )
        ex, state = world.session()

        def recover_with_bridge(ex_in, state_in, dest=None, *, protected_fn=None, min_displacement=0.8):
            self.assertIsNotNone(protected_fn)
            yield from nav.sneak_bridge(ex_in, state_in, [[1, 63, 0], [2, 63, 0]])
            pos = [2.5, 64.0, 0.5]
            yield rec("move_to_recover", ex_in.move_to(pos=pos, rng=0.5), pos=pos)

        with patch.object(nav, "recover_move", side_effect=recover_with_bridge, create=True):
            steps = drive(make("scout_island", {"radius": 32}), ex, state)

        self.assertFalse(world.st["sneaking"])
        self.assertEqual(VISITED_ANCHORS, [(2.5, 0.5)])

    def test_scout_island_not_cands_recover_move_success(self):
        """当周边未搜索到安全陆地方块 (not cands) 时，自动调用一次 nav.recover_move，位移 >=0.8m 则成功返回。"""
        world = NavRecoverySkillWorld(pos=(0.5, 64.0, 0.5), blocks={})
        ex, state = world.session()

        def recover_from_empty(ex_in, state_in, dest=None, *, protected_fn=None, min_displacement=0.8):
            pos = [2.0, 64.0, 0.5]
            yield rec("move_to_recover", ex_in.move_to(pos=pos, rng=0.5), pos=pos)

        with patch.object(nav, "recover_move", side_effect=recover_from_empty, create=True):
            drive(make("scout_island", {"radius": 32}), ex, state)

        self.assertEqual(VISITED_ANCHORS, [(2.0, 0.5)])

    def test_scout_island_not_cands_recover_move_insufficient_displacement(self):
        """当 not cands 且脱困位移不足 0.8m 时，抛出 Replan 避免空转。"""
        world = NavRecoverySkillWorld(pos=(0.5, 64.0, 0.5), blocks={})
        ex, state = world.session()

        def recover_fail(ex_in, state_in, dest=None, *, protected_fn=None, min_displacement=0.8):
            pos = [0.6, 64.0, 0.5]
            yield rec("move_to_recover", ex_in.move_to(pos=pos, rng=0.5), pos=pos)

        with patch.object(nav, "recover_move", side_effect=recover_fail, create=True):
            with self.assertRaisesRegex(Replan, "周边未找到可安全落脚的新陆地方块"):
                drive(make("scout_island", {"radius": 32}), ex, state)

        self.assertEqual(VISITED_ANCHORS, [])


class CraftGearAndMineStoneStalledHandlingTests(unittest.TestCase):
    """验证 CraftGear 与 MineStone 对 stalled 错误的处理契约。"""

    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()
        KNOWN_TABLES.clear()
        PROTECTED_ZONES.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()
        KNOWN_TABLES.clear()

    def test_craft_gear_handles_stalled_on_table_and_places_new_table(self):
        """CraftGear 前往已有工作台遭遇 stalled 时，遗忘该台并在原地合成放置新工作台完成合成。"""
        world = NavRecoverySkillWorld(
            items={"oak_planks": 4, "stick": 2, "cobblestone": 3},
            blocks={(10, 64, 0): "crafting_table"},
            move_failures={(10.0, 64.0, 0.0): "stalled"},
        )
        ex, state = world.session()
        KNOWN_TABLES.append((10, 64, 0))

        steps = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "craft", "place", "craft"])
        self.assertEqual(steps[0]["error_code"], "stalled")
        self.assertEqual(state.count("stone_pickaxe"), 1)
        self.assertNotIn((10, 64, 0), KNOWN_TABLES)
        self.assertTrue(has_known_table(state.pos))
        self.assertTrue(memo_for(state).is_blocked((10, 64, 0), category="station"))

    def test_mine_stone_handles_stalled_on_stone_and_mines_alternative(self):
        """MineStone 前往石头遭遇 stalled 时，记录不可达格并切换至备用石头完成挖掘。"""
        world = NavRecoverySkillWorld(
            pos=(0.5, 64.0, 0.5),
            items={"wooden_pickaxe": 1},
            blocks={
                (5, 64, 0): "stone",  # 优先 (dist 5.12)，但卡顿不可达
                (0, 64, 7): "stone",  # 备选 (dist 6.6)，移动至近旁挖掘
            },
            move_failures={(5.0, 64.0, 0.0): "stalled"},
            move_handler=lambda dest, rng=1, move_count=1: (
                (world.calls.append(("move_to", {"pos": [0.5, 64.0, 5.5]})),
                 world.st.__setitem__("pos", [0.5, 64.0, 5.5]),
                 {"pos": [0.5, 64.0, 5.5]})[2]
                if dest == [0.0, 64.0, 7.0] else None
            ),
        )
        world.st["held"] = {"name": "wooden_pickaxe"}
        ex, state = world.session()

        steps = drive(make("mine_stone", {"count": 1}), ex, state)
        ops = [s["op"] for s in steps]
        self.assertEqual(ops, ["move_to", "move_to", "dig"])
        self.assertEqual(steps[0]["error_code"], "stalled")
        self.assertEqual(steps[0]["pos"], [5, 64, 0])
        self.assertEqual(steps[1]["pos"], [0, 64, 7])
        self.assertEqual(steps[2]["pos"], [0, 64, 7])
        self.assertGreaterEqual(state.count("cobblestone"), 1)


if __name__ == "__main__":
    unittest.main()
