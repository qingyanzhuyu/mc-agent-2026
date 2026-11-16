"""L2 战术原语兜底与方块通过性判定单测（verified: offline）。

严格验证：
1. mcagent.skills.items:
   - AIR、NAV_FAIL_CODES、PASSABLE_COVER 集合契约
   - is_clear_space (字符串/字典、minecraft: 前缀、snow layers 边界与类型防御、非空检验)
   - is_safe_solid_floor (实体安全地面判定、排除空气/地被植物/雪层1..2/危险伤害液体)
2. mcagent.skills.nav:
   - _read_block: 正常读取、对 not_loaded / not_ready 静默返回 None，对 bad_args 与其它 ApiError 原样上抛
   - recover_move 战术原语:
     * 清理自身站立格/头部软覆盖（雪层、草、蛛网、树叶），对 diggable=False/protected_fn/snow_block 跳过
     * 按面向 dest 的欧氏距离优先排列候选方向
     * 战术 (a) 邻近 1 格位移（平步、1格上台阶、1格下落），位移 >= min_displacement 即时终止；捕获 NAV_FAIL_CODES
     * 战术 (b) breach_1x2 破拆软墙与硬墙（需装备镐）；拒绝基岩/黑曜石/矿石/工作台/箱子/传送门/刷怪笼/液体/保护格及不稳定顶板（水/岩浆/沙子/沙砾）
     * 战术 (c) step_up_1x2 翻越 2 格高台（即便障碍不可挖或无镐，消耗 >=2 垫块交替上垫）
     * 战术 (d) sneak_bridge 潜行搭桥跨越 1~3 格深坑/水坑并在抵达安全落点后干净释放潜行姿态
     * 安全抢占（ApiError("interrupted") 或 Replan("动作被安全层抢占")）即刻穿透上抛
     * 全流程严格遵循一步一动作（drive() 驱动验证）
"""
from __future__ import annotations

import math
import unittest
from typing import Any, Callable, Iterator, Optional, Sequence

from mcagent.client import ApiError
from mcagent.runtime import Replan, State
from mcagent.skills.items import (
    AIR,
    NAV_FAIL_CODES,
    PASSABLE_COVER,
    is_clear_space,
    is_safe_solid_floor,
)
from mcagent.skills.testing import CountingExecutor, GridWorld, drive

try:
    from mcagent.skills.nav import _read_block, recover_move
except (ImportError, AttributeError):
    _read_block = None  # type: ignore[assignment]
    recover_move = None  # type: ignore[assignment]


# -----------------------------------------------------------------------------
# 替身世界：专用于导航恢复测试的扩展 GridWorld
# -----------------------------------------------------------------------------


class RecoveryGridWorld(GridWorld):
    """支持方块元数据、属性注入与动作异常模拟的替身世界。"""

    def __init__(
        self,
        pos=(0.5, 64.0, 0.5),
        blocks: Optional[dict] = None,
        items: Optional[dict] = None,
        block_props: Optional[dict] = None,
        block_meta: Optional[dict] = None,
        fail_move_codes: Optional[list[str]] = None,
        fail_move_pos: Optional[dict[tuple, str]] = None,
        error_blocks: Optional[dict[tuple, Exception]] = None,
    ):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.block_props: dict[tuple, dict] = dict(block_props or {})
        self.block_meta: dict[tuple, dict] = dict(block_meta or {})
        self.fail_move_codes: list[str] = list(fail_move_codes or [])
        self.fail_move_pos: dict[tuple, str] = dict(fail_move_pos or {})
        self.error_blocks: dict[tuple, Exception] = dict(error_blocks or {})
        self.move_to_error: Optional[Exception] = None
        self.dig_error: Optional[Exception] = None

    def get_block(self, pos) -> dict:
        tpos = (math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2]))
        if tpos in self.error_blocks:
            err = self.error_blocks[tpos]
            if isinstance(err, Exception):
                raise err
            raise ApiError(str(err), f"error at {tpos}")
        raw = self.blocks.get(tpos, "air")
        if isinstance(raw, dict):
            res = dict(raw)
        else:
            res = {"name": raw}
        if tpos in self.block_props and "properties" not in res:
            res["properties"] = dict(self.block_props[tpos])
        if tpos in self.block_meta:
            res.update(self.block_meta[tpos])
        return res

    def dig(self, pos, collect=True, *, stop_on=None) -> dict:
        self.calls.append(("dig", {"pos": list(pos)}))
        if self.dig_error is not None:
            err = self.dig_error
            self.dig_error = None
            raise err
        tpos = (math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2]))
        info = self.get_block(pos)
        if info.get("diggable") is False:
            raise ApiError("not_diggable", f"方块不可挖掘 {pos}")
        dug = self.blocks.pop(tpos, "air")
        dug_name = dug if isinstance(dug, str) else dug.get("name", "air")
        if dug_name != "air" and collect:
            self.items[dug_name] = self.items.get(dug_name, 0) + 1
        return {"dug": dug_name}

    def place(self, item, pos=None, *, stop_on=None) -> dict:
        self.calls.append(("place", {"item": item, "pos": list(pos)}))
        self._need(item)
        x, y, z = self.st["pos"]
        bx, by, bz = math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2])
        fx, fy, fz = math.floor(x), math.floor(y), math.floor(z)
        if (bx, by, bz) in ((fx, fy, fz), (fx, fy + 1, fz)):
            raise ApiError("blocked_by_self", "目标格与自身碰撞箱重叠")
        self.items[item] -= 1
        self.blocks[(bx, by, bz)] = item
        return {"placed": item, "pos": list(pos)}

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
        self.calls.append(("move_to", {"x": x, "z": z, "y": y, "pos": pos}))
        if self.move_to_error is not None:
            err = self.move_to_error
            self.move_to_error = None
            raise err
        dest = [float(v) for v in pos] if pos is not None else [
            float(x), float(self.st["pos"][1] if y is None else y), float(z)
        ]
        if self.fail_move_codes:
            code = self.fail_move_codes.pop(0)
            raise ApiError(code, f"simulated move failure: {code}")
        cell = (math.floor(dest[0]), math.floor(dest[1]), math.floor(dest[2]))
        if cell in self.fail_move_pos:
            code = self.fail_move_pos[cell]
            raise ApiError(code, f"simulated move failure at {cell}")
        self.st["pos"] = dest
        return {"pos": list(dest)}


# -----------------------------------------------------------------------------
# 1. mcagent.skills.items 契约测试
# -----------------------------------------------------------------------------


class ItemsContractTests(unittest.TestCase):
    def test_air_constants(self):
        self.assertEqual(AIR, frozenset({"air", "cave_air", "void_air"}))

    def test_nav_fail_codes(self):
        self.assertEqual(NAV_FAIL_CODES, ("path_not_found", "unreachable", "move_timeout", "stalled"))

    def test_passable_cover_membership(self):
        expected_included = {
            "short_grass", "tall_grass", "fern", "large_fern",
            "short_dry_grass", "tall_dry_grass", "dead_bush", "bush",
            "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
            "oxeye_daisy", "cornflower", "lily_of_the_valley", "leaf_litter",
            "pink_petals", "wildflowers", "moss_carpet", "pale_moss_carpet",
            "brown_mushroom", "red_mushroom", "glow_lichen", "vine",
        }
        expected_excluded = {
            "water", "lava", "powder_snow", "sweet_berry_bush",
            "cactus", "snow_block", "oak_leaves", "snow",
        }
        self.assertTrue(expected_included.issubset(PASSABLE_COVER))
        self.assertTrue(expected_excluded.isdisjoint(PASSABLE_COVER))

    def test_is_clear_space_str_and_dict(self):
        # 常见空气及 minecraft: 前缀
        for block in ("air", "cave_air", "void_air", "minecraft:air", "minecraft:cave_air"):
            self.assertTrue(is_clear_space(block), block)
            self.assertTrue(is_clear_space({"name": block}), block)

        # 常见地被植物
        for plant in ("short_grass", "tall_grass", "fern", "dandelion", "pink_petals", "vine"):
            self.assertTrue(is_clear_space(plant), plant)
            self.assertTrue(is_clear_space(f"minecraft:{plant}"), plant)
            self.assertTrue(is_clear_space({"name": plant}), plant)
            self.assertTrue(is_clear_space({"name": f"minecraft:{plant}"}), plant)

    def test_is_clear_space_snow_layers(self):
        # 默认层数为 1
        self.assertTrue(is_clear_space("snow"))
        self.assertTrue(is_clear_space("minecraft:snow"))
        self.assertTrue(is_clear_space({"name": "snow"}))
        self.assertTrue(is_clear_space({"name": "minecraft:snow"}))

        # layers=1 与 layers=2（含真实服务端日志里的字符串形式）均视为可通行空间
        self.assertTrue(is_clear_space({"name": "snow", "properties": {"layers": 1}}))
        self.assertTrue(is_clear_space({"name": "minecraft:snow", "properties": {"layers": 1}}))
        self.assertTrue(is_clear_space({"name": "snow", "properties": {"layers": 2}}))
        self.assertTrue(is_clear_space({"name": "snow", "properties": {"layers": "1"}}))
        self.assertTrue(is_clear_space({"name": "snow", "properties": {"layers": "2"}}))

        # layers >= 3 或其他异常层数均不可通行
        self.assertFalse(is_clear_space({"name": "snow", "properties": {"layers": 3}}))
        self.assertFalse(is_clear_space({"name": "snow", "properties": {"layers": "3"}}))
        self.assertFalse(is_clear_space({"name": "snow", "properties": {"layers": 8}}))
        self.assertFalse(is_clear_space({"name": "snow", "properties": {"layers": 0}}))
        self.assertFalse(is_clear_space({"name": "snow", "properties": {"layers": True}}))
        self.assertFalse(is_clear_space({"name": "snow", "properties": {"layers": 1.5}}))
        self.assertFalse(is_clear_space({"name": "snow", "properties": {"layers": "bad"}}))
        self.assertTrue(is_clear_space({"name": "snow", "properties": None}))

    def test_is_clear_space_false_cases(self):
        # 空值、空结构与空字符串
        self.assertFalse(is_clear_space(None))
        self.assertFalse(is_clear_space({}))
        self.assertFalse(is_clear_space(""))

        # 实体方块、雪块、树叶、危险流体与阻碍物
        for solid in (
            "stone", "snow_block", "water", "lava", "powder_snow",
            "sweet_berry_bush", "cactus", "oak_leaves",
        ):
            self.assertFalse(is_clear_space(solid), solid)
            self.assertFalse(is_clear_space(f"minecraft:{solid}"), solid)
            self.assertFalse(is_clear_space({"name": solid}), solid)

    def test_is_safe_solid_floor_true_cases(self):
        # 常规安全立足地面
        for safe in ("stone", "grass_block", "dirt", "snow_block"):
            self.assertTrue(is_safe_solid_floor(safe), safe)
            self.assertTrue(is_safe_solid_floor(f"minecraft:{safe}"), safe)
            self.assertTrue(is_safe_solid_floor({"name": safe}), safe)
        # 8层顶层雪相当于完整实体方块
        self.assertTrue(is_safe_solid_floor({"name": "snow", "properties": {"layers": 8}}))
        self.assertTrue(is_safe_solid_floor({"name": "minecraft:snow", "properties": {"layers": 8}}))

    def test_is_safe_solid_floor_false_cases(self):
        # 空值
        self.assertFalse(is_safe_solid_floor(None))
        self.assertFalse(is_safe_solid_floor({}))
        self.assertFalse(is_safe_solid_floor(""))

        # 空气不能作为地面
        for a in ("air", "cave_air", "void_air"):
            self.assertFalse(is_safe_solid_floor(a), a)
            self.assertFalse(is_safe_solid_floor({"name": a}), a)

        # 地被植物不能作为实体地面
        for plant in ("short_grass", "fern", "moss_carpet", "glow_lichen"):
            self.assertFalse(is_safe_solid_floor(plant), plant)
            self.assertFalse(is_safe_solid_floor({"name": plant}), plant)

        # 薄雪层不是实体方块
        self.assertFalse(is_safe_solid_floor("snow"))
        self.assertFalse(is_safe_solid_floor({"name": "snow", "properties": {"layers": 1}}))
        self.assertFalse(is_safe_solid_floor({"name": "snow", "properties": {"layers": 2}}))

        # 危险方块/流体
        for unsafe in ("water", "lava", "powder_snow", "cactus", "magma_block", "sweet_berry_bush"):
            self.assertFalse(is_safe_solid_floor(unsafe), unsafe)
            self.assertFalse(is_safe_solid_floor(f"minecraft:{unsafe}"), unsafe)
            self.assertFalse(is_safe_solid_floor({"name": unsafe}), unsafe)


# -----------------------------------------------------------------------------
# 2. mcagent.skills.nav._read_block 测试
# -----------------------------------------------------------------------------


@unittest.skipIf(_read_block is None, "mcagent.skills.nav._read_block 尚未实现")
class NavReadBlockTests(unittest.TestCase):
    def test_read_block_success(self):
        world = RecoveryGridWorld(blocks={(1, 64, 0): "stone"})
        ex, _ = world.session()
        block = _read_block(ex, [1, 64, 0])
        self.assertIsNotNone(block)
        name = block if isinstance(block, str) else block.get("name")
        self.assertEqual(name, "stone")

    def test_read_block_not_loaded_returns_none(self):
        world = RecoveryGridWorld(error_blocks={(1, 64, 0): ApiError("not_loaded", "Chunk not loaded")})
        ex, _ = world.session()
        self.assertIsNone(_read_block(ex, [1, 64, 0]))

    def test_read_block_not_ready_returns_none(self):
        world = RecoveryGridWorld(error_blocks={(1, 64, 0): ApiError("not_ready", "World not ready")})
        ex, _ = world.session()
        self.assertIsNone(_read_block(ex, [1, 64, 0]))

    def test_read_block_bad_args_reraises(self):
        world = RecoveryGridWorld(error_blocks={(1, 64, 0): ApiError("bad_args", "Bad coordinates")})
        ex, _ = world.session()
        with self.assertRaises(ApiError) as ctx:
            _read_block(ex, [1, 64, 0])
        self.assertEqual(ctx.exception.code, "bad_args")

    def test_read_block_other_apierror_reraises(self):
        world = RecoveryGridWorld(error_blocks={(1, 64, 0): ApiError("internal_error", "Server exception")})
        ex, _ = world.session()
        with self.assertRaises(ApiError) as ctx:
            _read_block(ex, [1, 64, 0])
        self.assertEqual(ctx.exception.code, "internal_error")


# -----------------------------------------------------------------------------
# 3. mcagent.skills.nav.recover_move: 身体空间清理测试
# -----------------------------------------------------------------------------


@unittest.skipIf(recover_move is None, "mcagent.skills.nav.recover_move 尚未实现")
class NavRecoveryBotClearTests(unittest.TestCase):
    def test_digs_snow_layer_at_feet_and_steps_clear(self):
        # 脚底有顶层雪，相邻是安全实体地面
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "dirt",
                (0, 64, 0): {"name": "snow", "properties": {"layers": 1}},
                (1, 63, 0): "dirt",
            },
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        ops = [s["op"] for s in steps]
        self.assertIn("dig", ops)
        self.assertIn("move_to_recover", ops)
        self.assertAlmostEqual(world.st["pos"][0], 1.5)

    def test_digs_soft_cover_at_feet_and_head(self):
        # 头部蛛网 + 脚底草
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "dirt",
                (0, 64, 0): "short_grass",
                (0, 65, 0): "cobweb",
                (1, 63, 0): "dirt",
            },
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        dig_steps = [s for s in steps if s["op"] == "dig"]
        self.assertGreaterEqual(len(dig_steps), 2)
        dug_positions = [tuple(s["pos"]) for s in dig_steps]
        self.assertIn((0, 64, 0), dug_positions)
        self.assertIn((0, 65, 0), dug_positions)

    def test_skips_digging_when_undiggable(self):
        # 脚底标记 diggable=False
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "dirt",
                (0, 64, 0): {"name": "snow", "properties": {"layers": 1}},
                (1, 63, 0): "dirt",
            },
            block_meta={(0, 64, 0): {"diggable": False}},
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        ops = [s["op"] for s in steps]
        self.assertNotIn("dig", ops)
        self.assertIn("move_to_recover", ops)

    def test_skips_digging_when_protected_fn_returns_true(self):
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "dirt",
                (0, 64, 0): {"name": "snow", "properties": {"layers": 1}},
                (1, 63, 0): "dirt",
            },
        )
        ex, state = world.session()
        steps = drive(
            recover_move(
                ex,
                state,
                dest=(5, 64, 0),
                protected_fn=lambda pos: tuple(int(v) for v in pos) == (0, 64, 0),
            ),
            ex,
        )
        ops = [s["op"] for s in steps]
        self.assertNotIn("dig", ops)

    def test_skips_digging_solid_snow_block(self):
        # 脚下是雪块，脚底格是空气，不应挖掘雪块
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "snow_block",
                (1, 63, 0): "snow_block",
            },
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        ops = [s["op"] for s in steps]
        self.assertNotIn("dig", ops)
        self.assertEqual(world.blocks.get((0, 63, 0)), "snow_block")


# -----------------------------------------------------------------------------
# 4. mcagent.skills.nav.recover_move: 目标朝向顺序测试
# -----------------------------------------------------------------------------


@unittest.skipIf(recover_move is None, "mcagent.skills.nav.recover_move 尚未实现")
class NavRecoveryDirectionOrderingTests(unittest.TestCase):
    def setUp(self):
        # 4 个方向均有平坦可行走地面
        self.base_blocks = {
            (0, 63, 0): "dirt",
            (1, 63, 0): "dirt",
            (-1, 63, 0): "dirt",
            (0, 63, 1): "dirt",
            (0, 63, -1): "dirt",
        }

    def test_order_towards_positive_x(self):
        world = RecoveryGridWorld(pos=(0.5, 64.0, 0.5), blocks=dict(self.base_blocks))
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(10, 64, 0)), ex)
        self.assertEqual(steps[0]["op"], "move_to_recover")
        self.assertAlmostEqual(world.st["pos"][0], 1.5)
        self.assertAlmostEqual(world.st["pos"][2], 0.5)

    def test_order_towards_negative_z(self):
        world = RecoveryGridWorld(pos=(0.5, 64.0, 0.5), blocks=dict(self.base_blocks))
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(0, 64, -10)), ex)
        self.assertEqual(steps[0]["op"], "move_to_recover")
        self.assertAlmostEqual(world.st["pos"][0], 0.5)
        self.assertAlmostEqual(world.st["pos"][2], -0.5)

    def test_order_towards_negative_x(self):
        world = RecoveryGridWorld(pos=(0.5, 64.0, 0.5), blocks=dict(self.base_blocks))
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(-10, 64, 0)), ex)
        self.assertEqual(steps[0]["op"], "move_to_recover")
        self.assertAlmostEqual(world.st["pos"][0], -0.5)
        self.assertAlmostEqual(world.st["pos"][2], 0.5)

    def test_order_towards_positive_z(self):
        world = RecoveryGridWorld(pos=(0.5, 64.0, 0.5), blocks=dict(self.base_blocks))
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(0, 64, 10)), ex)
        self.assertEqual(steps[0]["op"], "move_to_recover")
        self.assertAlmostEqual(world.st["pos"][0], 0.5)
        self.assertAlmostEqual(world.st["pos"][2], 1.5)


# -----------------------------------------------------------------------------
# 5. mcagent.skills.nav.recover_move: 战术 (a) 邻近位移测试
# -----------------------------------------------------------------------------


@unittest.skipIf(recover_move is None, "mcagent.skills.nav.recover_move 尚未实现")
class NavRecoveryAdjacentStepTests(unittest.TestCase):
    def test_tactic_a_same_level_step(self):
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(0, 63, 0): "dirt", (1, 63, 0): "dirt"},
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "move_to_recover")
        self.assertAlmostEqual(world.st["pos"][0], 1.5)
        self.assertAlmostEqual(world.st["pos"][1], 64.0)

    def test_tactic_a_one_high_step_up(self):
        # 1格台阶：地面在 y=64，站立后脚在 y=65
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(0, 63, 0): "stone", (1, 64, 0): "stone"},
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 65, 0)), ex)
        self.assertEqual(steps[0]["op"], "move_to_recover")
        self.assertAlmostEqual(world.st["pos"][0], 1.5)
        self.assertAlmostEqual(world.st["pos"][1], 65.0)

    def test_tactic_a_one_high_step_down(self):
        # 1格下落：地面在 y=62，站立后脚在 y=63
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(0, 63, 0): "stone", (1, 62, 0): "stone"},
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 63, 0)), ex)
        self.assertEqual(steps[0]["op"], "move_to_recover")
        self.assertAlmostEqual(world.st["pos"][0], 1.5)
        self.assertAlmostEqual(world.st["pos"][1], 63.0)

    def test_tactic_a_catches_all_nav_fail_codes(self):
        for code in ("path_not_found", "unreachable", "move_timeout", "stalled"):
            with self.subTest(code=code):
                world = RecoveryGridWorld(
                    pos=(0.5, 64.0, 0.5),
                    blocks={(0, 63, 0): "dirt", (1, 63, 0): "dirt"},
                    fail_move_codes=[code],
                )
                ex, state = world.session()
                # 第一次优先朝 +X 移动抛出 code，应被成功捕获并记录 error_code，而非崩溃
                steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
                self.assertEqual(len(steps), 1)
                self.assertEqual(steps[0]["op"], "move_to_recover")
                self.assertEqual(steps[0]["error_code"], code)

    def test_tactic_a_empty_world_terminates_bounded(self):
        # 空世界（仅脚下有方块，周围全是悬崖/空虚），最多尝试一次步进即安全终止
        world = RecoveryGridWorld(pos=(0.5, 64.0, 0.5), blocks={(0, 63, 0): "dirt"})
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        # 不应陷入死循环，尝试次数有限
        self.assertLessEqual(len(steps), 4)

    def test_stops_early_when_min_displacement_reached(self):
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(0, 63, 0): "dirt", (1, 63, 0): "dirt", (2, 63, 0): "dirt"},
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0), min_displacement=0.8), ex)
        # 单次位移 1.0 >= 0.8 即停止
        self.assertEqual(len(steps), 1)


# -----------------------------------------------------------------------------
# 6. mcagent.skills.nav.recover_move: 战术 (b) breach_1x2 测试
# -----------------------------------------------------------------------------


@unittest.skipIf(recover_move is None, "mcagent.skills.nav.recover_move 尚未实现")
class NavRecoveryBreachTests(unittest.TestCase):
    def test_breach_soft_wall_without_pickaxe(self):
        # 无镐挖开泥土挡路墙
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "dirt",
                (1, 63, 0): "dirt",
                (1, 64, 0): "dirt",
                (1, 65, 0): "dirt",
            },
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        ops = [s["op"] for s in steps]
        self.assertNotIn("equip", ops)
        self.assertIn("dig", ops)
        self.assertIn("move_to_recover", ops)
        self.assertAlmostEqual(world.st["pos"][0], 1.5)

    def test_breach_soft_wall_variants(self):
        for soft in ("gravel", "snow_block", "oak_leaves"):
            with self.subTest(soft=soft):
                world = RecoveryGridWorld(
                    pos=(0.5, 64.0, 0.5),
                    blocks={
                        (0, 63, 0): "dirt",
                        (1, 63, 0): "dirt",
                        (1, 64, 0): soft,
                        (1, 65, 0): soft,
                    },
                )
                ex, state = world.session()
                steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
                ops = [s["op"] for s in steps]
                self.assertIn("dig", ops)

    def test_breach_stone_with_pickaxe(self):
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "stone",
                (1, 63, 0): "stone",
                (1, 64, 0): "stone",
                (1, 65, 0): "cobblestone",
            },
            items={"iron_pickaxe": 1},
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        ops = [s["op"] for s in steps]
        self.assertIn("equip", ops)
        self.assertIn("dig", ops)
        self.assertIn("move_to_recover", ops)
        self.assertAlmostEqual(world.st["pos"][0], 1.5)

    def test_breach_stone_refused_without_pickaxe(self):
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "stone",
                (1, 63, 0): "stone",
                (1, 64, 0): "stone",
                (1, 65, 0): "stone",
            },
            items={},  # 无镐
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        ops = [s["op"] for s in steps]
        # 不应空手挖掘石头
        self.assertNotIn("dig", ops)

    def test_breach_refuses_forbidden_materials(self):
        forbidden_blocks = (
            "bedrock", "obsidian", "crying_obsidian", "iron_ore", "deepslate_iron_ore",
            "diamond_ore", "crafting_table", "furnace", "chest", "end_portal_frame",
            "spawner", "water", "lava",
        )
        for forbidden in forbidden_blocks:
            with self.subTest(forbidden=forbidden):
                world = RecoveryGridWorld(
                    pos=(0.5, 64.0, 0.5),
                    blocks={
                        (0, 63, 0): "stone",
                        (1, 63, 0): "stone",
                        (1, 64, 0): forbidden,
                        (1, 65, 0): forbidden,
                    },
                    items={"diamond_pickaxe": 1},
                )
                ex, state = world.session()
                steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
                ops = [s["op"] for s in steps]
                self.assertNotIn("dig", ops)

    def test_breach_refuses_undiggable_or_protected(self):
        # diggable=False
        world1 = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(0, 63, 0): "dirt", (1, 63, 0): "dirt", (1, 64, 0): "dirt", (1, 65, 0): "dirt"},
            block_meta={(1, 64, 0): {"diggable": False}},
        )
        ex1, state1 = world1.session()
        steps1 = drive(recover_move(ex1, state1, dest=(5, 64, 0)), ex1)
        self.assertNotIn("dig", [s["op"] for s in steps1])

        # protected_fn
        world2 = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(0, 63, 0): "dirt", (1, 63, 0): "dirt", (1, 64, 0): "dirt", (1, 65, 0): "dirt"},
        )
        ex2, state2 = world2.session()
        steps2 = drive(
            recover_move(
                ex2,
                state2,
                dest=(5, 64, 0),
                protected_fn=lambda pos: tuple(int(v) for v in pos) == (1, 64, 0),
            ),
            ex2,
        )
        self.assertNotIn("dig", [s["op"] for s in steps2])

    def test_breach_refuses_unsafe_roof(self):
        for unsafe_roof in ("water", "lava", "sand", "gravel"):
            with self.subTest(unsafe_roof=unsafe_roof):
                world = RecoveryGridWorld(
                    pos=(0.5, 64.0, 0.5),
                    blocks={
                        (0, 63, 0): "dirt",
                        (1, 63, 0): "dirt",
                        (1, 64, 0): "dirt",
                        (1, 65, 0): "dirt",
                        (1, 66, 0): unsafe_roof,  # 顶部有塌方/流体危险
                    },
                )
                ex, state = world.session()
                steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
                self.assertNotIn("dig", [s["op"] for s in steps])


# -----------------------------------------------------------------------------
# 7. mcagent.skills.nav.recover_move: 战术 (c) step_up_1x2 测试
# -----------------------------------------------------------------------------


@unittest.skipIf(recover_move is None, "mcagent.skills.nav.recover_move 尚未实现")
class NavRecoveryStepUpTests(unittest.TestCase):
    def test_step_up_2_high_ledge_with_placeables(self):
        # 2格高基岩墙（不可挖），但背包有垫块，上方空间开阔，且有相邻立柱列 (-1, 63, 0)
        # 默认 min_displacement=0.8 下也必须完成翻越并落到目标高台 [1.5, 66.0, 0.5]
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "dirt",
                (-1, 63, 0): "dirt",
                (1, 64, 0): "bedrock",
                (1, 65, 0): "bedrock",
                # 台阶顶端 (1, 65, 0) 是实体地面，(1, 66, 0) 和 (1, 67, 0) 是空气
            },
            items={"dirt": 5},
            fail_move_pos={(-1, 64, 0): "path_not_found"},
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 66, 0)), ex)
        ops = [s["op"] for s in steps]
        self.assertIn("place", ops)
        self.assertEqual(ops[-1], "move_to_recover")
        self.assertAlmostEqual(world.st["pos"][0], 1.5)
        self.assertAlmostEqual(world.st["pos"][1], 66.0)

    def test_step_up_refused_when_insufficient_placeables(self):
        # 仅有 1 个方块（无法交替垫高 2 格）
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "dirt",
                (1, 64, 0): "bedrock",
                (1, 65, 0): "bedrock",
            },
            items={"dirt": 1},
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 66, 0)), ex)
        ops = [s["op"] for s in steps]
        self.assertNotIn("place", ops)


# -----------------------------------------------------------------------------
# 8. mcagent.skills.nav.recover_move: 战术 (d) sneak_bridge 测试
# -----------------------------------------------------------------------------


@unittest.skipIf(recover_move is None, "mcagent.skills.nav.recover_move 尚未实现")
class NavRecoverySneakBridgeTests(unittest.TestCase):
    def test_sneak_bridge_air_pit(self):
        # 跨越 2 格悬崖深坑
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "stone",
                # (1, 63, 0) 空气坑
                # (2, 63, 0) 空气坑
                (3, 63, 0): "stone",  # 目标落脚点
            },
            items={"dirt": 5},
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        ops = [s["op"] for s in steps]
        self.assertIn("posture", ops)
        self.assertIn("place", ops)
        # 结束时潜行状态必须已释放
        self.assertFalse(world.st["sneaking"])
        self.assertAlmostEqual(world.st["pos"][0], 3.5)

    def test_sneak_bridge_water_pit(self):
        # 跨越 1 格水面
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "stone",
                (1, 63, 0): "water",
                (2, 63, 0): "stone",
            },
            items={"dirt": 5},
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        ops = [s["op"] for s in steps]
        self.assertIn("place", ops)
        self.assertFalse(world.st["sneaking"])
        self.assertAlmostEqual(world.st["pos"][0], 2.5)

    def test_sneak_bridge_releases_sneak_cleanly(self):
        # 正常结束潜行必定解除
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(0, 63, 0): "stone", (2, 63, 0): "stone"},
            items={"dirt": 5},
        )
        ex, state = world.session()
        drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        self.assertFalse(world.st["sneaking"])

    def test_sneak_bridge_releases_sneak_on_interrupt(self):
        # 中途异常或中断时潜行依然被清理释放
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(0, 63, 0): "stone", (2, 63, 0): "stone"},
            items={"dirt": 5},
        )
        ex, state = world.session()
        flow = recover_move(ex, state, dest=(5, 64, 0))
        first = next(flow)
        self.assertEqual(first.get("op"), "posture")
        self.assertTrue(world.st["sneaking"])
        flow.close()
        self.assertFalse(world.st["sneaking"])

    def test_sneak_bridge_refused_when_insufficient_placeables(self):
        # 坑需要 2 格方块，但背包只有 1 格
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(0, 63, 0): "stone", (3, 63, 0): "stone"},
            items={"dirt": 1},
        )
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        ops = [s["op"] for s in steps]
        self.assertNotIn("place", ops)


# -----------------------------------------------------------------------------
# 9. mcagent.skills.nav.recover_move: 安全层抢占穿透测试
# -----------------------------------------------------------------------------


@unittest.skipIf(recover_move is None, "mcagent.skills.nav.recover_move 尚未实现")
class NavRecoverySafetyPreemptionTests(unittest.TestCase):
    def test_safety_preemption_apierror_interrupted(self):
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(0, 63, 0): "dirt", (1, 63, 0): "dirt"},
        )
        world.move_to_error = ApiError("interrupted", "动作被安全监控层抢占")
        ex, state = world.session()
        with self.assertRaises((ApiError, Replan)) as ctx:
            drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        err = ctx.exception
        is_interrupted = (
            getattr(err, "code", None) == "interrupted"
            or "interrupted" in str(err)
            or getattr(getattr(err, "__cause__", None), "code", None) == "interrupted"
        )
        self.assertTrue(is_interrupted)

    def test_safety_preemption_replan(self):
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(0, 63, 0): "dirt", (1, 63, 0): "dirt"},
        )
        world.move_to_error = Replan("动作被安全层抢占")
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "安全层抢占"):
            drive(recover_move(ex, state, dest=(5, 64, 0)), ex)


# -----------------------------------------------------------------------------
# 10. 一步一动作不变量与多战术级联回退验证
# -----------------------------------------------------------------------------


@unittest.skipIf(recover_move is None, "mcagent.skills.nav.recover_move 尚未实现")
class NavRecoveryOneActionPerStepTests(unittest.TestCase):
    def test_one_action_per_step_on_fallback_chain(self):
        # 复杂回退链：+X 移动被拒 -> 破拆失败 (dig_unconfirmed) -> 继续回退到垫高/搭桥仍严格一步一动作
        world = RecoveryGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "dirt",
                (-1, 63, 0): "dirt",
                (1, 63, 0): "dirt",
                (1, 64, 0): "dirt",
                (1, 65, 0): "dirt",
            },
            items={"dirt": 4},
            fail_move_pos={(-1, 64, 0): "path_not_found"},
        )
        world.dig_error = ApiError("dig_unconfirmed", "block remained after dig")
        ex, state = world.session()
        steps = drive(recover_move(ex, state, dest=(5, 64, 0)), ex)
        ops = [s["op"] for s in steps]
        self.assertIn("move_to_recover", ops)
        self.assertIn("breach_recover", ops)
        self.assertIn("place", ops)
        self.assertAlmostEqual(world.st["pos"][0], 1.5)
        self.assertAlmostEqual(world.st["pos"][1], 66.0)


if __name__ == "__main__":
    unittest.main()
