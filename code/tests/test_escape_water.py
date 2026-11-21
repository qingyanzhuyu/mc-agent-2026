"""水体/岩浆/着火/避敌脱困原语单测（verified: offline）。

所有测试均经 `skills.testing.drive()` 驱动，严格验证"一次阻塞动作对应一次 yield"、
不伪造完成状态（只要 `in_water` 或 `submerged` 仍为 True 绝不返回 True）、
只拆软顶（1-3 层且顶部确认为 AIR）与安全抢占即时透传。
"""
from __future__ import annotations

import math
import unittest
from typing import Any, Callable, Optional

from mcagent.client import ApiError
from mcagent.runtime import Replan
from mcagent.skills.escape import escape_hazard_step, escape_water_to_shore
from mcagent.skills.testing import CountingExecutor, GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches


class EscapeWorld(GridWorld):
    """支持流体/着火状态、`control`/`use_item`/`find_blocks` 与回调钩子的离线测试世界。"""

    def __init__(
        self,
        pos=(0.5, 64.0, 0.5),
        blocks: Optional[dict] = None,
        items: Optional[dict] = None,
        *,
        in_water: bool = False,
        submerged: bool = False,
        on_fire: bool = False,
        in_lava: bool = False,
        held: Optional[dict] = None,
        nearest_hostile: Optional[dict] = None,
    ):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st.update({
            "in_water": in_water,
            "submerged": submerged,
            "on_fire": on_fire,
            "in_lava": in_lava,
            "held": held,
            "nearest_hostile": nearest_hostile,
        })
        self.move_requests: list[dict] = []
        self.control_requests: list[dict] = []
        self.find_blocks_reply: Optional[dict] = None
        self.on_move: Optional[Callable[[list[float]], None]] = None
        self.on_dig: Optional[Callable[[list[int]], None]] = None
        self.on_place: Optional[Callable[[str, list[int]], None]] = None
        self.on_use_item: Optional[Callable[[str], None]] = None
        self.on_control: Optional[Callable[[dict], None]] = None

    def get_block(self, pos) -> dict:
        key = (math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2]))
        raw = self.blocks.get(key, "air")
        return dict(raw) if isinstance(raw, dict) else {"name": raw}

    def equip(self, item, slot="hand", *, stop_on=None) -> dict:
        res = super().equip(item, slot=slot, stop_on=stop_on)
        self.st["held"] = {"name": item, "count": self.items.get(item, 1)}
        return res

    def dig(self, pos, collect=True, *, stop_on=None) -> dict:
        key = (math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2]))
        res = super().dig(key, collect=collect, stop_on=stop_on)
        if self.on_dig is not None:
            self.on_dig(list(key))
        return res

    def place(self, item, pos=None, *, stop_on=None) -> dict:
        key = (math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2]))
        res = super().place(item, pos=list(key), stop_on=stop_on)
        if self.on_place is not None:
            self.on_place(item, list(key))
        return res

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        self.move_requests.append({"pos": list(pos) if pos is not None else None, "rng": rng, "timeout": timeout})
        dest = [float(v) for v in pos] if pos is not None else [
            float(x), float(self.st["pos"][1] if y is None else y), float(z)
        ]
        if self.on_move is not None:
            self.on_move(dest)
        return super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None) -> dict:
        self.calls.append(("use_item", {"item": item, "target": target}))
        self._need(item)
        if self.on_use_item is not None:
            self.on_use_item(item)
        return {"used": item}

    def control(self, **keys) -> dict:
        self.calls.append(("control", dict(keys)))
        self.control_requests.append(dict(keys))
        if self.on_control is not None:
            self.on_control(dict(keys))
        return {"ok": True, **keys}

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None) -> dict:
        self.calls.append(("find_blocks", {"types": types, "radius": radius, "limit": limit}))
        if self.find_blocks_reply is not None:
            return self.find_blocks_reply
        return {"blocks": []}


def _drive_result(flow, ex: CountingExecutor) -> tuple[bool, list[Optional[dict]]]:
    """通过 skills.testing.drive() 跑完生成器并捕获最终布尔返回值，同时校验每步至多 1 个阻塞动作。"""
    box: dict[str, Any] = {}

    def _wrapper():
        box["ret"] = yield from flow

    steps = drive(_wrapper(), ex)
    return bool(box.get("ret")), steps


def _basin_blocks(fx: int = 0, fy: int = 64, fz: int = 0, *, depth: int = 1, radius: int = 1) -> dict:
    """构造无干岸的封闭水盆：水体被高墙围住，仅保留池底与池水。"""
    blocks: dict[tuple[int, int, int], str] = {}
    for x in range(fx - radius - 1, fx + radius + 2):
        for z in range(fz - radius - 1, fz + radius + 2):
            blocks[(x, fy - 1, z)] = "stone"
            is_wall = abs(x - fx) > radius or abs(z - fz) > radius
            if is_wall:
                for y in range(fy, fy + depth + 3):
                    blocks[(x, y, z)] = "stone"
            else:
                for y in range(fy, fy + depth):
                    blocks[(x, y, z)] = "water"
    return blocks


class TestEscapeWater(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_voxel_caches()

    def tearDown(self) -> None:
        reset_all_voxel_caches()

    def test_already_dry_returns_true_with_zero_actions(self) -> None:
        world = EscapeWorld(pos=(0.5, 64.0, 0.5), in_water=False, submerged=False)
        ex, state = world.session()

        ok, steps = _drive_result(escape_water_to_shore(ex, state), ex)

        self.assertTrue(ok)
        self.assertEqual(steps, [])
        self.assertEqual(ex.acts, [])
        self.assertEqual(world.calls, [])

    def test_submerged_under_two_dirt_layers_breaks_one_per_yield_and_surfaces(self) -> None:
        # 脚底 y=62，头 y=63 为水，y=64、y=65 为两层泥土，y=66 为空气；四面石墙围死仅能破顶
        blocks = _basin_blocks(0, 62, 0, depth=2, radius=0)
        blocks[(0, 64, 0)] = "dirt"
        blocks[(0, 65, 0)] = "dirt"
        blocks[(0, 66, 0)] = "air"
        # 在墙顶外侧 (1, 66, 0) 留一格安全干岸，破开 2 层泥土后上浮再走上岸
        blocks[(1, 65, 0)] = "stone"
        blocks[(1, 66, 0)] = "air"
        blocks[(1, 67, 0)] = "air"

        world = EscapeWorld(pos=(0.5, 62.0, 0.5), blocks=blocks, in_water=True, submerged=True)

        def on_control(req: dict) -> None:
            # 破开泥土后上浮一步到水面，使 (1, 66, 0) 落入 dy in (0, 1, 2, -1) 可及范围
            if req.get("jump"):
                world.st["pos"] = [0.5, 64.0, 0.5]
                world.st["submerged"] = False

        def on_move(dest: list[float]) -> None:
            if dest[1] >= 66.0:
                world.st["in_water"] = False
                world.st["submerged"] = False

        world.on_control = on_control
        world.on_move = on_move
        ex, state = world.session()

        ok, steps = _drive_result(escape_water_to_shore(ex, state, max_steps=6), ex)

        self.assertTrue(ok)
        dig_steps = [s for s in steps if s and s["op"] == "dig"]
        self.assertEqual([s["pos"] for s in dig_steps], [[0, 64, 0], [0, 65, 0]])
        self.assertEqual(ex.acts[:2], ["dig", "dig"])

    def test_does_not_break_stone_bedrock_or_unconfirmed_air_cover(self) -> None:
        for roof_case in (
            {(0, 64, 0): "stone", (0, 65, 0): "air"},
            {(0, 64, 0): "bedrock", (0, 65, 0): "air"},
            {(0, 64, 0): "dirt", (0, 65, 0): "water"},
            {(0, 64, 0): "dirt", (0, 65, 0): "stone", (0, 66, 0): "air"},
        ):
            with self.subTest(roof_case=roof_case):
                blocks = _basin_blocks(0, 62, 0, depth=2, radius=0)
                blocks.update(roof_case)
                world = EscapeWorld(pos=(0.5, 62.0, 0.5), blocks=blocks, in_water=True, submerged=True)
                ex, state = world.session()

                ok, steps = _drive_result(escape_water_to_shore(ex, state, max_steps=2), ex)

                self.assertFalse(ok)
                self.assertNotIn("dig", ex.acts)
                self.assertTrue(all(s["op"] == "control" for s in steps if s))

    def test_confirmed_dry_shore_triggers_bounded_move_to_and_returns_true(self) -> None:
        blocks = {
            (0, 63, 0): "stone",
            (0, 64, 0): "water",
            (2, 63, 0): "grass_block",
            (2, 64, 0): "air",
            (2, 65, 0): "air",
        }
        world = EscapeWorld(pos=(0.5, 64.0, 0.5), blocks=blocks, in_water=True, submerged=False)
        world.on_move = lambda dest: world.st.update({"in_water": False, "submerged": False})
        ex, state = world.session()

        ok, steps = _drive_result(escape_water_to_shore(ex, state), ex)

        self.assertTrue(ok)
        self.assertEqual(ex.acts, ["move_to"])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "move_to")
        self.assertEqual(world.move_requests[0]["pos"], [2.5, 64.0, 0.5])
        self.assertEqual(world.move_requests[0]["timeout"], 15.0)
        self.assertEqual(world.move_requests[0]["rng"], 1)

    def test_trapped_basin_places_step_block_with_cobblestone_then_climbs(self) -> None:
        # 1 格深水池：中心 (0, 64, 0) 为水，周围 (1, 64, 0) 为水且下方 (1, 63, 0) 为石头，
        # 外围墙高 1 格到 y=64，需垫高方块才能上墙顶
        blocks = _basin_blocks(0, 64, 0, depth=1, radius=1)
        # 拆掉外围一圈高于 y=64 的石头，只让水盆周围有 1 格高干岸（但先铺满水导致周围 y=64 均是水，无 dy=0 干岸）
        # 使 (1, 64, 0) 垫块后，(1, 65, 0) 成为合法干岸
        for x in range(-2, 3):
            for z in range(-2, 3):
                if abs(x) == 2 or abs(z) == 2:
                    blocks[(x, 64, z)] = "water"
                    blocks.pop((x, 65, z), None)
                    blocks.pop((x, 66, z), None)
        world = EscapeWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"cobblestone": 2},
            in_water=True,
            submerged=False,
        )
        world.on_move = lambda dest: world.st.update({"in_water": False, "submerged": False})
        ex, state = world.session()

        ok, steps = _drive_result(escape_water_to_shore(ex, state, max_steps=4), ex)

        self.assertTrue(ok)
        self.assertEqual(ex.acts, ["place", "move_to"])
        self.assertEqual(steps[0]["op"], "place")
        self.assertEqual(steps[0]["item"], "cobblestone")
        self.assertEqual(steps[1]["op"], "move_to")
        self.assertEqual(steps[1]["pos"][1], 65.0)

    def test_trapped_basin_without_blocks_swims_via_control(self) -> None:
        blocks = _basin_blocks(0, 64, 0, depth=2, radius=0)
        world = EscapeWorld(pos=(0.5, 64.0, 0.5), blocks=blocks, items={}, in_water=True, submerged=False)
        ex, state = world.session()

        ok, steps = _drive_result(escape_water_to_shore(ex, state, max_steps=3), ex)

        self.assertFalse(ok)
        self.assertEqual(ex.acts, ["control", "control", "control"])
        self.assertEqual(len(steps), 3)
        for req in world.control_requests:
            self.assertTrue(req["forward"])
            self.assertTrue(req["jump"])
            self.assertEqual(req["ms"], 600)
            self.assertIn("yaw", req)
        # 验证朝向有轮换
        yaws = [req["yaw"] for req in world.control_requests]
        self.assertEqual(len(set(yaws)), 3)

    def test_critical_never_fake_complete_while_still_in_water(self) -> None:
        # 即使 move_to 到达目标格但服务端仍回报 in_water=True，也绝对不能返回 True
        blocks = {
            (0, 63, 0): "stone",
            (0, 64, 0): "water",
            (1, 63, 0): "stone",
            (1, 64, 0): "air",
            (1, 65, 0): "air",
        }
        world = EscapeWorld(pos=(0.5, 64.0, 0.5), blocks=blocks, in_water=True, submerged=False)
        ex, state = world.session()

        ok, steps = _drive_result(escape_water_to_shore(ex, state, max_steps=3), ex)

        self.assertFalse(ok)
        self.assertEqual(len(steps), 3)

    def test_control_unsupported_api_and_missing_method_handled_gracefully(self) -> None:
        blocks = _basin_blocks(0, 64, 0, depth=2, radius=0)

        # Case 1: control 抛出 unsupported_api
        world1 = EscapeWorld(pos=(0.5, 64.0, 0.5), blocks=blocks, in_water=True, submerged=False)

        def raise_unsupported(_req):
            raise ApiError("unsupported_api", "no control")

        world1.on_control = raise_unsupported
        ex1, state1 = world1.session()
        ok1, steps1 = _drive_result(escape_water_to_shore(ex1, state1, max_steps=3), ex1)
        self.assertFalse(ok1)
        self.assertEqual(steps1[0]["error_code"], "unsupported_api")

        # Case 2: 基类 GridWorld 根本没有 control 属性（触发 AttributeError）
        world2 = GridWorld(pos=(0.5, 64.0, 0.5), blocks=blocks)
        world2.st.update({"in_water": True, "submerged": False})
        ex2, state2 = world2.session()
        ok2, steps2 = _drive_result(escape_water_to_shore(ex2, state2, max_steps=2), ex2)
        self.assertFalse(ok2)
        self.assertEqual(steps2[0]["error_code"], "unsupported_api")

    def test_hazard_surface_and_escape_water_intents(self) -> None:
        blocks = {
            (0, 63, 0): "stone",
            (0, 64, 0): "water",
            (1, 63, 0): "dirt",
            (1, 64, 0): "air",
            (1, 65, 0): "air",
        }
        for intent in ("surface", "escape_water"):
            with self.subTest(intent=intent):
                world = EscapeWorld(pos=(0.5, 64.0, 0.5), blocks=blocks, in_water=True)
                world.on_move = lambda _: world.st.update({"in_water": False})
                ex, state = world.session()

                ok, _ = _drive_result(escape_hazard_step(ex, state, intent), ex)

                self.assertTrue(ok)
                self.assertEqual(ex.acts, ["move_to"])

    def test_hazard_douse_with_water_bucket_equips_and_uses_item(self) -> None:
        world = EscapeWorld(
            pos=(0.5, 64.0, 0.5),
            items={"water_bucket": 1},
            on_fire=True,
            held={"name": "wooden_pickaxe", "count": 1},
        )
        world.on_use_item = lambda item: world.st.update({"on_fire": False, "in_lava": False})
        ex, state = world.session()

        ok, steps = _drive_result(escape_hazard_step(ex, state, "douse"), ex)

        self.assertTrue(ok)
        self.assertEqual(ex.acts, ["equip", "use_item"])
        self.assertEqual([s["op"] for s in steps], ["equip", "use_item"])

    def test_hazard_douse_with_nearby_water_and_step_away_fallback(self) -> None:
        # Case 1: 着火且无水桶 -> find_blocks 找到附近水源并 move_to
        world1 = EscapeWorld(pos=(0.5, 64.0, 0.5), on_fire=True)
        world1.find_blocks_reply = {"blocks": [{"name": "water", "pos": [4, 64, 0]}]}
        world1.on_move = lambda _: world1.st.update({"on_fire": False})
        ex1, state1 = world1.session()

        ok1, steps1 = _drive_result(escape_hazard_step(ex1, state1, "douse"), ex1)

        self.assertTrue(ok1)
        self.assertEqual(ex1.acts, ["move_to"])
        self.assertEqual(steps1[0]["pos"], [4.5, 64.0, 0.5])

        # Case 2: 在岩浆中且无水桶 -> 立刻沿干岸方向原生疾跑跳出岩浆（禁止被慢速 move_to 卡死烧死）
        blocks_lava = {
            (0, 63, 0): "stone",
            (0, 64, 0): "lava",
            (1, 63, 0): "stone",
            (1, 64, 0): "air",
            (1, 65, 0): "air",
        }
        world2 = EscapeWorld(pos=(0.5, 64.0, 0.5), blocks=blocks_lava, in_lava=True, on_fire=True)
        world2.on_control = lambda _: world2.st.update({"in_lava": False, "on_fire": False})
        ex2, state2 = world2.session()

        ok2, _ = _drive_result(escape_hazard_step(ex2, state2, "escape_lava"), ex2)

        self.assertTrue(ok2)
        self.assertEqual(ex2.acts, ["control"])
        self.assertTrue(world2.control_requests[0]["jump"])
        self.assertTrue(world2.control_requests[0]["sprint"])
        # 干岸在 +X，Mineflayer yaw = atan2(-1, 0) = -pi/2
        self.assertAlmostEqual(world2.control_requests[0]["yaw"], -math.pi / 2, places=4)

        # Case 3: 仅着火（不在岩浆中）且附近无水源时 move_to 失败，退回 control(back=True, jump=True, ms=600)
        world3 = EscapeWorld(pos=(0.5, 64.0, 0.5), blocks=blocks_lava, in_lava=False, on_fire=True)
        world3.on_move = lambda _: (_ for _ in ()).throw(ApiError("path_not_found", "blocked"))
        world3.on_control = lambda _: world3.st.update({"in_lava": False, "on_fire": False})
        ex3, state3 = world3.session()

        ok3, steps3 = _drive_result(escape_hazard_step(ex3, state3, "douse"), ex3)

        self.assertTrue(ok3)
        self.assertEqual(ex3.acts, ["move_to", "control"])
        self.assertEqual(steps3[0]["error_code"], "path_not_found")
        self.assertTrue(world3.control_requests[0]["back"])
        self.assertTrue(world3.control_requests[0]["jump"])
        self.assertEqual(world3.control_requests[0]["ms"], 600)

    def test_hazard_flee_move_away_and_sprint_fallback(self) -> None:
        # Case 1: 正常向远离僵尸方向 move_to
        world1 = EscapeWorld(
            pos=(0.5, 64.0, 0.5),
            nearest_hostile={"name": "zombie", "pos": [0.5, 64.0, 3.5], "dist": 3.0},
        )
        ex1, state1 = world1.session()

        ok1, steps1 = _drive_result(escape_hazard_step(ex1, state1, "flee"), ex1)

        self.assertTrue(ok1)
        self.assertEqual(ex1.acts, ["move_to"])
        self.assertLess(steps1[0]["pos"][2], 0.5)
        self.assertEqual(world1.move_requests[0]["rng"], 2)
        self.assertEqual(world1.move_requests[0]["timeout"], 2.2)

        # Case 2: move_to 寻路失败 -> yield error rec 后转入 control(forward=True, sprint=True, jump=True, yaw=..., ms=800)
        world2 = EscapeWorld(
            pos=(0.5, 64.0, 0.5),
            nearest_hostile={"name": "zombie", "pos": [-2.5, 64.0, 0.5], "dist": 3.0},
        )
        world2.on_move = lambda _: (_ for _ in ()).throw(ApiError("stalled", "stuck"))
        world2.on_control = lambda _: world2.st.update({
            "pos": [8.5, 64.0, 0.5],
            "nearest_hostile": {"name": "zombie", "pos": [-2.5, 64.0, 0.5], "dist": 11.0},
        })
        ex2, state2 = world2.session()

        ok2, steps2 = _drive_result(escape_hazard_step(ex2, state2, "flee"), ex2)

        self.assertTrue(ok2)
        self.assertEqual(ex2.acts, ["move_to", "control"])
        self.assertEqual(steps2[0]["error_code"], "stalled")
        req = world2.control_requests[0]
        self.assertTrue(req["forward"])
        self.assertTrue(req["sprint"])
        self.assertTrue(req["jump"])
        self.assertEqual(req["ms"], 800)
        # 敌人在 -X，逃离方向为 +X，Mineflayer yaw = atan2(-1, 0) = -pi/2
        self.assertAlmostEqual(req["yaw"], -math.pi / 2, places=5)

    def test_unknown_intent_and_preemption_re_raise(self) -> None:
        world = EscapeWorld(pos=(0.5, 64.0, 0.5), in_water=True)
        ex, state = world.session()

        ok_unknown, steps_unknown = _drive_result(escape_hazard_step(ex, state, "fly_away"), ex)
        self.assertFalse(ok_unknown)
        self.assertEqual(steps_unknown, [])

        # 安全抢占 Replan 绝不可被吞掉
        world.on_control = lambda _: (_ for _ in ()).throw(Replan("动作被安全层抢占: escape_lava"))
        with self.assertRaises(Replan):
            _drive_result(escape_water_to_shore(ex, state), ex)


if __name__ == "__main__":
    unittest.main()
