"""Sprint 1.1 安全监控与紧急脱困离线单测（verified: offline）。

覆盖：
1. `mcagent.runtime`:
   - `_same_escape_kind` 对 `"safe_mode"` 与活跃脱困意图的匹配边界；
   - `Safety.hazards` 与 `SafetyMonitor._dispatch` 在 `safe_mode` + 活跃逃生 / 近身非苦力怕敌怪 / 孤立冷却下的分级与 `/stop` 行为；
   - `SafetyMonitor._check_support`（`guard=False` 下的 1~2 格台阶、4+ 格深坑、岩浆坑、世界底部 `y<=-60`、`void_air`、空方块名，以及 `void_guard=True` 红线）；
   - `has_equipped_armor` 与 `effective_hostile_flee_dist`。
2. `mcagent.skills.escape` & `mcagent.skills.safety_escape`:
   - 地下 1×2 隧道双格封堵（`tunnel_seal=True`）、开放洞穴与备块不足回退；
   - 原地受困逃生失败的 `verify_done` 非空校验；
   - 骷髅遮挡断视线掩体选择（`skeleton_cover=True`）与 `verify_done`；
   - `EmergencyEscape` 中途 `abort()` 正确关闭内层生成器并收盾。
"""
from __future__ import annotations

import dataclasses
import math
import time
import unittest
from typing import Any, Callable, Optional

from mcagent.runtime import (
    Executor,
    RuntimeConfig,
    Safety,
    SafetyMonitor,
    State,
    _same_escape_kind,
    effective_hostile_flee_dist,
    has_equipped_armor,
)
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.escape import _try_seal_underground_tunnel, escape_hazard_step
from mcagent.skills.safety_escape import EmergencyEscape
from mcagent.skills.testing import CountingExecutor, GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches


class _ProbeSafetyClient:
    """只读/急停探针替身，用于离线验证 SafetyMonitor._check_support 与 _dispatch。"""

    def __init__(self, blocks: Optional[dict[tuple[int, int, int], Any]] = None, st: Optional[dict] = None) -> None:
        self.blocks: dict[tuple[int, int, int], Any] = dict(blocks or {})
        self.st: dict[str, Any] = dict(st or {
            "ready": True,
            "alive": True,
            "health": 20.0,
            "food": 20,
            "pos": [0.5, 64.0, 0.5],
            "on_ground": True,
        })
        self.stops = 0

    def status(self, **_: Any) -> dict:
        return {**self.st, "pos": list(self.st["pos"])} if "pos" in self.st else dict(self.st)

    def events(self, since: int = 0, limit: int = 200) -> dict:
        return {"events": [], "last_seq": since}

    def inventory(self) -> dict:
        return {"items": []}

    def get_block(self, pos: list[int], use_cache: bool = False) -> dict:
        key = (math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2]))
        raw = self.blocks.get(key, "air")
        if isinstance(raw, dict):
            return {"pos": list(key), **raw}
        return {"pos": list(key), "name": raw}

    def stop(self) -> dict:
        self.stops += 1
        return {"stopped": True, "released": True}


class _Sprint1World(GridWorld):
    """支持控制/转向/方块字典值与位移回调的离线逃生测试世界。"""

    def __init__(
        self,
        pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
        blocks: Optional[dict] = None,
        items: Optional[dict] = None,
        *,
        health: float = 20.0,
        food: int = 20,
        armor: Optional[dict] = None,
        nearest_hostile: Optional[dict] = None,
        pin_position: bool = False,
    ) -> None:
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st.update({
            "health": health,
            "food": food,
            "nearest_hostile": nearest_hostile,
        })
        if armor is not None:
            self.st["armor"] = armor
        self.pin_position = pin_position
        self.move_requests: list[dict] = []
        self.control_requests: list[dict] = []
        self.on_move: Optional[Callable[[list[float]], None]] = None
        self.on_control: Optional[Callable[[dict], None]] = None

    def get_block(self, pos: list[int], use_cache: bool = False) -> dict:
        key = (math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2]))
        raw = self.blocks.get(key, "air")
        return dict(raw) if isinstance(raw, dict) else {"name": raw}

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
        self.move_requests.append({"pos": list(pos) if pos is not None else None, "rng": rng, "timeout": timeout})
        if self.pin_position:
            self.calls.append(("move_to", {"x": x, "z": z, "y": y, "pos": pos}))
            return {"pos": list(self.st["pos"])}
        dest = [float(v) for v in pos] if pos is not None else [
            float(x), float(self.st["pos"][1] if y is None else y), float(z),
        ]
        res = super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)
        if self.on_move is not None:
            self.on_move(dest)
        return res

    def control(self, **keys: Any) -> dict:
        self.calls.append(("control", dict(keys)))
        self.control_requests.append(dict(keys))
        if self.on_control is not None:
            self.on_control(dict(keys))
        return {"ok": True, **keys}

    def look(self, yaw=None, pitch=None, *, pos=None, entity_id=None) -> dict:
        self.calls.append(("look", {"yaw": yaw, "pitch": pitch, "pos": pos, "entity_id": entity_id}))
        return {"ok": True}


def _drive_flow(flow: Any, ex: CountingExecutor) -> tuple[bool, list[Optional[dict]]]:
    """通过 skills.testing.drive() 跑完生成器并捕获布尔返回值，校验每步最多 1 个阻塞动作。"""
    box: dict[str, Any] = {}

    def _wrapper():
        box["ret"] = yield from flow

    steps = drive(_wrapper(), ex)
    return bool(box.get("ret")), steps


def _tunnel_1x2_blocks(
    *,
    fy: int = 20,
    x_range: range = range(-6, 7),
    left_wall: bool = True,
    right_wall: bool = True,
    ceiling: bool = True,
) -> dict[tuple[int, int, int], str]:
    """构造沿 X 轴的 1×2 隧道（z=0 通行，z=±1 为侧墙，fy-1 为地板，fy+2 为顶板）。"""
    blocks: dict[tuple[int, int, int], str] = {}
    for x in x_range:
        for z in range(-5, 6):
            blocks[(x, fy - 1, z)] = "stone"
            if ceiling:
                blocks[(x, fy + 2, z)] = "stone"
        if left_wall:
            blocks[(x, fy, -1)] = "stone"
            blocks[(x, fy + 1, -1)] = "stone"
        if right_wall:
            blocks[(x, fy, 1)] = "stone"
            blocks[(x, fy + 1, 1)] = "stone"
    return blocks


def _flat_ground(fy: int = 64, span: int = 10) -> dict[tuple[int, int, int], str]:
    return {(x, fy - 1, z): "stone" for x in range(-span, span + 1) for z in range(-span, span + 1)}


class Sprint1RuntimeSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def test_same_escape_kind_safe_mode_matching(self) -> None:
        for active in ("flee", "surface", "douse", "  flee "):
            for kind in ("safe_mode", "SAFE_MODE", "  Safe_Mode \t"):
                with self.subTest(active=active, kind=kind):
                    self.assertTrue(_same_escape_kind(active, kind))

        for empty_active in (None, "", "   ", "\t\n"):
            with self.subTest(active=empty_active):
                self.assertFalse(_same_escape_kind(empty_active, "safe_mode"))

    def test_safe_mode_downgrades_to_soft_and_no_stop_when_active_escape_running(self) -> None:
        for intent in ("flee", "surface", "douse"):
            with self.subTest(intent=intent):
                client = _ProbeSafetyClient()
                state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
                ex = Executor(client, state, log=lambda *_: None)
                mon = SafetyMonitor(client, state, ex)

                state.set_status(client.status())
                state.safe_mode_until = time.monotonic() + 65.0
                state.set_active_escape(intent)

                hazards = Safety(state.config).hazards(state)
                safe_hazards = [h for h in hazards if h["kind"] == "safe_mode"]
                self.assertEqual(len(safe_hazards), 1)
                self.assertEqual(safe_hazards[0]["severity"], "soft")

                mon._dispatch(reconcile=False)
                self.assertEqual(client.stops, 0)

    def test_safe_mode_with_close_non_creeper_hostile_prioritizes_flee_and_zero_stop(self) -> None:
        for dist in (0.8, 3.0):
            with self.subTest(dist=dist):
                client = _ProbeSafetyClient(st={
                    "ready": True,
                    "alive": True,
                    "health": 20.0,
                    "food": 20,
                    "pos": [0.5, 64.0, 0.5],
                    "on_ground": True,
                    "nearest_hostile": {"name": "zombie", "dist": dist},
                })
                state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
                ex = Executor(client, state, log=lambda *_: None)
                mon = SafetyMonitor(client, state, ex)

                state.set_status(client.status())
                state.safe_mode_until = time.monotonic() + 65.0

                hazards = Safety(state.config).hazards(state)
                self.assertGreaterEqual(len(hazards), 2)
                self.assertEqual(hazards[0]["kind"], "flee")
                safe_hazards = [h for h in hazards if h["kind"] == "safe_mode"]
                self.assertEqual(len(safe_hazards), 1)
                self.assertEqual(safe_hazards[0]["severity"], "soft")

                mon._dispatch(reconcile=False)
                self.assertEqual(client.stops, 0)

    def test_safe_mode_without_active_escape_or_close_hostile_stays_urgent_and_stops(self) -> None:
        for hostile in (None, {"name": "zombie", "dist": 8.0}):
            with self.subTest(hostile=hostile):
                client = _ProbeSafetyClient(st={
                    "ready": True,
                    "alive": True,
                    "health": 20.0,
                    "food": 20,
                    "pos": [0.5, 64.0, 0.5],
                    "on_ground": True,
                    "nearest_hostile": hostile,
                })
                state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
                ex = Executor(client, state, log=lambda *_: None)
                mon = SafetyMonitor(client, state, ex)

                state.set_status(client.status())
                state.safe_mode_until = time.monotonic() + 65.0
                state.set_active_escape(None)

                hazards = Safety(state.config).hazards(state)
                safe_hazards = [h for h in hazards if h["kind"] == "safe_mode"]
                self.assertEqual(len(safe_hazards), 1)
                self.assertEqual(safe_hazards[0]["severity"], "urgent")

                mon._dispatch(reconcile=False)
                self.assertEqual(client.stops, 1)

    def test_check_support_controlled_1block_stair_never_triggers_void_risk(self) -> None:
        # 1) 负垂直速度
        for floor_block in ("stone", "oak_stairs"):
            with self.subTest(mode="neg_vel", floor=floor_block):
                client = _ProbeSafetyClient(
                    blocks={(1, 63, 0): "air", (1, 62, 0): floor_block},
                    st={
                        "ready": True, "alive": True, "health": 20.0, "food": 20,
                        "pos": [1.5, 63.2, 0.5], "on_ground": False, "velocity": [0.0, -0.35, 0.0],
                    },
                )
                state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
                mon = SafetyMonitor(client, state, Executor(client, state, log=lambda *_: None))
                mon.tick()
                self.assertEqual(client.stops, 0)
                self.assertFalse(state.get_support().get("risk"))

        # 2) 缺失 velocity 字段（依赖相邻两拍 prev_y=64.0 -> y=63.2）
        client_no_vel = _ProbeSafetyClient(
            blocks={(1, 63, 0): "air", (1, 62, 0): "stone"},
            st={
                "ready": True, "alive": True, "health": 20.0, "food": 20,
                "pos": [1.5, 64.0, 0.5], "on_ground": True,
            },
        )
        state_no_vel = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
        mon_no_vel = SafetyMonitor(client_no_vel, state_no_vel, Executor(client_no_vel, state_no_vel, log=lambda *_: None))
        mon_no_vel.tick()
        client_no_vel.st["pos"] = [1.5, 63.2, 0.5]
        client_no_vel.st["on_ground"] = False
        state_no_vel.status_at = float("-inf")
        mon_no_vel.tick()
        self.assertEqual(client_no_vel.stops, 0)
        self.assertFalse(state_no_vel.get_support().get("risk"))

        # 3) 零速度
        client_zero = _ProbeSafetyClient(
            blocks={(1, 63, 0): "air", (1, 62, 0): "stone"},
            st={
                "ready": True, "alive": True, "health": 20.0, "food": 20,
                "pos": [1.5, 63.2, 0.5], "on_ground": False, "velocity": [0.0, 0.0, 0.0],
            },
        )
        state_zero = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
        mon_zero = SafetyMonitor(client_zero, state_zero, Executor(client_zero, state_zero, log=lambda *_: None))
        mon_zero.tick()
        self.assertEqual(client_zero.stops, 0)
        self.assertFalse(state_zero.get_support().get("risk"))

    def test_check_support_controlled_2block_step_never_triggers_void_risk(self) -> None:
        client = _ProbeSafetyClient(
            blocks={(1, 63, 0): "air", (1, 62, 0): "air", (1, 61, 0): "stone"},
            st={
                "ready": True, "alive": True, "health": 20.0, "food": 20,
                "pos": [1.5, 63.2, 0.5], "on_ground": False, "velocity": [0.0, -0.5, 0.0],
            },
        )
        state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
        mon = SafetyMonitor(client, state, Executor(client, state, log=lambda *_: None))
        mon.tick()
        self.assertEqual(client.stops, 0)
        self.assertFalse(state.get_support().get("risk"))

    def test_check_support_4plus_block_pit_while_falling_triggers_void_risk(self) -> None:
        client = _ProbeSafetyClient(
            blocks={(1, 63, 0): "air", (1, 62, 0): "air", (1, 61, 0): "air", (1, 60, 0): "air", (1, 59, 0): "stone"},
            st={
                "ready": True, "alive": True, "health": 20.0, "food": 20,
                "pos": [1.5, 63.2, 0.5], "on_ground": False, "velocity": [0.0, -0.5, 0.0],
            },
        )
        state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
        mon = SafetyMonitor(client, state, Executor(client, state, log=lambda *_: None))
        mon.tick()
        self.assertEqual(client.stops, 1)
        self.assertTrue(state.get_support().get("risk"))

    def test_check_support_lava_pit_below_while_falling_triggers_void_risk(self) -> None:
        for lava_block in ("lava", "flowing_lava"):
            with self.subTest(lava_block=lava_block):
                client = _ProbeSafetyClient(
                    blocks={(1, 63, 0): "air", (1, 62, 0): "air", (1, 61, 0): lava_block, (1, 60, 0): "stone"},
                    st={
                        "ready": True, "alive": True, "health": 20.0, "food": 20,
                        "pos": [1.5, 63.2, 0.5], "on_ground": False, "velocity": [0.0, -0.5, 0.0],
                    },
                )
                state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
                mon = SafetyMonitor(client, state, Executor(client, state, log=lambda *_: None))
                mon.tick()
                self.assertEqual(client.stops, 1)
                self.assertTrue(state.get_support().get("risk"))

    def test_check_support_near_world_bottom_triggers_void_risk(self) -> None:
        for y_pos, vel in ((-60.0, [0.0, 0.0, 0.0]), (-62.0, [0.0, -0.5, 0.0])):
            with self.subTest(y=y_pos, vel=vel):
                under_y = math.floor(y_pos - 0.01)
                client = _ProbeSafetyClient(
                    blocks={(1, under_y, 0): "air", (1, under_y - 1, 0): "stone"},
                    st={
                        "ready": True, "alive": True, "health": 20.0, "food": 20,
                        "pos": [1.5, y_pos, 0.5], "on_ground": False, "velocity": vel,
                    },
                )
                state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
                mon = SafetyMonitor(client, state, Executor(client, state, log=lambda *_: None))
                mon.tick()
                self.assertEqual(client.stops, 1)
                self.assertTrue(state.get_support().get("risk"))

    def test_check_support_void_air_under_feet_triggers_void_risk(self) -> None:
        client = _ProbeSafetyClient(
            blocks={(1, 63, 0): "void_air", (1, 62, 0): "stone"},
            st={
                "ready": True, "alive": True, "health": 20.0, "food": 20,
                "pos": [1.5, 63.2, 0.5], "on_ground": False, "velocity": [0.0, 0.0, 0.0],
            },
        )
        state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
        mon = SafetyMonitor(client, state, Executor(client, state, log=lambda *_: None))
        mon.tick()
        self.assertEqual(client.stops, 1)
        self.assertTrue(state.get_support().get("risk"))

    def test_check_support_empty_unknown_block_while_falling_triggers_void_risk(self) -> None:
        client = _ProbeSafetyClient(
            blocks={(1, 63, 0): {"name": ""}},
            st={
                "ready": True, "alive": True, "health": 20.0, "food": 20,
                "pos": [1.5, 63.2, 0.5], "on_ground": False, "velocity": [0.0, -0.5, 0.0],
            },
        )
        state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
        mon = SafetyMonitor(client, state, Executor(client, state, log=lambda *_: None))
        mon.tick()
        self.assertEqual(client.stops, 1)
        self.assertTrue(state.get_support().get("risk"))

    def test_check_support_void_guard_true_triggers_immediately_on_air(self) -> None:
        client = _ProbeSafetyClient(
            blocks={(1, 63, 0): "air", (1, 62, 0): "stone"},
            st={
                "ready": True, "alive": True, "health": 20.0, "food": 20,
                "pos": [1.5, 63.2, 0.5], "on_ground": False, "velocity": [0.0, -0.35, 0.0],
            },
        )
        state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
        state.set_risk_context(void_guard=True)
        mon = SafetyMonitor(client, state, Executor(client, state, log=lambda *_: None))
        mon.tick()
        self.assertEqual(client.stops, 1)
        self.assertTrue(state.get_support().get("risk"))

    def test_has_equipped_armor(self) -> None:
        # 1) False cases
        self.assertFalse(has_equipped_armor(State(RuntimeConfig())))

        s_empty = State(RuntimeConfig())
        s_empty.set_status({"armor": {}, "equipment": {}})
        s_empty.set_inventory({"items": [], "armor": {}})
        self.assertFalse(has_equipped_armor(s_empty))

        s_air = State(RuntimeConfig())
        s_air.set_status({"armor": {"head": {"name": "air"}, "chest": {"name": "minecraft:air"}}})
        self.assertFalse(has_equipped_armor(s_air))

        s_weapon_only = State(RuntimeConfig())
        s_weapon_only.set_status({"equipment": {"hand": {"name": "iron_sword"}}})
        self.assertFalse(has_equipped_armor(s_weapon_only))

        # 2) True cases
        s_armor = State(RuntimeConfig())
        s_armor.set_status({"armor": {"chest": {"name": "iron_chestplate"}}})
        self.assertTrue(has_equipped_armor(s_armor))

        s_equip = State(RuntimeConfig())
        s_equip.set_status({"equipment": {"chest": {"name": "diamond_chestplate"}}})
        self.assertTrue(has_equipped_armor(s_equip))

        s_inv = State(RuntimeConfig())
        s_inv.set_inventory({"items": [], "armor": {"feet": {"name": "leather_boots"}}})
        self.assertTrue(has_equipped_armor(s_inv))

        s_pts = State(RuntimeConfig())
        s_pts.set_status({"armor_points": 4})
        self.assertTrue(has_equipped_armor(s_pts))

    def test_effective_hostile_flee_dist(self) -> None:
        # Default armored + hp=20 -> 6.0
        s_armored = State(RuntimeConfig())
        s_armored.set_status({"health": 20.0, "armor": {"chest": {"name": "iron_chestplate"}}})
        self.assertEqual(effective_hostile_flee_dist(s_armored), 6.0)
        self.assertEqual(effective_hostile_flee_dist(s_armored, "skeleton"), 6.0)

        # Unarmored + hp=20 -> 10.0 for skeleton, 9.0 for zombie/empty
        s_naked = State(RuntimeConfig())
        s_naked.set_status({"health": 20.0})
        self.assertEqual(effective_hostile_flee_dist(s_naked, "skeleton"), 10.0)
        self.assertEqual(effective_hostile_flee_dist(s_naked, "minecraft:skeleton"), 10.0)
        self.assertEqual(effective_hostile_flee_dist(s_naked, "zombie"), 9.0)
        self.assertEqual(effective_hostile_flee_dist(s_naked, ""), 9.0)

        # Armored + hp=10 (< hp_risk=14) -> 10.0 for skeleton, 9.0 for zombie
        s_low_hp = State(RuntimeConfig())
        s_low_hp.set_status({"health": 10.0, "armor": {"chest": {"name": "iron_chestplate"}}})
        self.assertEqual(effective_hostile_flee_dist(s_low_hp, "skeleton"), 10.0)
        self.assertEqual(effective_hostile_flee_dist(s_low_hp, "zombie"), 9.0)

        # Custom larger RuntimeConfig(hostile_flee_dist=12.0) -> 12.0
        cfg_12 = dataclasses.replace(RuntimeConfig(), hostile_flee_dist=12.0)
        s_custom = State(cfg_12)
        s_custom.set_status({"health": 20.0, "armor": {"chest": {"name": "iron_chestplate"}}})
        self.assertEqual(effective_hostile_flee_dist(s_custom, "zombie"), 12.0)
        s_custom_naked = State(cfg_12)
        s_custom_naked.set_status({"health": 20.0})
        self.assertEqual(effective_hostile_flee_dist(s_custom_naked, "skeleton"), 12.0)


class Sprint1SkillsEscapeTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def test_scenario_a_underground_1x2_tunnel_sealing_succeeds_without_moving(self) -> None:
        self.assertTrue(callable(_try_seal_underground_tunnel))

        # y=20 地下 1×2 隧道：escape_hazard_step 与 EmergencyEscape 均封堵 [1, 20, 0] 与 [1, 21, 0]
        blocks = _tunnel_1x2_blocks(fy=20)
        zombie = {"name": "zombie", "pos": [4.5, 20.0, 0.5], "dist": 4.0}

        world_prim = _Sprint1World(
            pos=(0.5, 20.0, 0.5),
            blocks=dict(blocks),
            items={"cobblestone": 4},
            nearest_hostile=dict(zombie),
        )
        ex_prim, st_prim = world_prim.session()
        ok, steps = _drive_flow(escape_hazard_step(ex_prim, st_prim, "flee"), ex_prim)
        self.assertTrue(ok)
        self.assertEqual(ex_prim.acts, ["place", "place"])
        self.assertEqual(sorted(s["pos"] for s in steps if s), [[1, 20, 0], [1, 21, 0]])
        self.assertTrue(all(s and s.get("tunnel_seal") is True for s in steps))

        # 通过 EmergencyEscape(intent="flee") 驱动并校验 verify_done("") 即使玩家未位移、僵尸仍在 4.0 格
        world_skill = _Sprint1World(
            pos=(0.5, 20.0, 0.5),
            blocks=dict(blocks),
            items={"cobblestone": 4},
            nearest_hostile=dict(zombie),
        )
        ex_skill, st_skill = world_skill.session()
        skill = EmergencyEscape(intent="flee")
        skill_steps = drive(skill, ex_skill, st_skill)
        self.assertEqual(ex_skill.acts, ["place", "place"])
        self.assertEqual(sorted(s["pos"] for s in skill_steps if s), [[1, 20, 0], [1, 21, 0]])
        self.assertTrue(all(s and s.get("tunnel_seal") is True for s in skill_steps))
        self.assertEqual(world_skill.st["pos"], [0.5, 20.0, 0.5])
        self.assertEqual(skill.verify_done(st_skill), "")

        # y=64 且 y+2=66 实心顶板同样视为封闭 1×2 隧道
        blocks_64 = _tunnel_1x2_blocks(fy=64, ceiling=True)
        world_64 = _Sprint1World(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks_64,
            items={"cobblestone": 4},
            nearest_hostile={"name": "zombie", "pos": [4.5, 64.0, 0.5], "dist": 4.0},
        )
        ex_64, st_64 = world_64.session()
        skill_64 = EmergencyEscape(intent="flee")
        steps_64 = drive(skill_64, ex_64, st_64)
        self.assertEqual(ex_64.acts, ["place", "place"])
        self.assertEqual(sorted(s["pos"] for s in steps_64 if s), [[1, 64, 0], [1, 65, 0]])
        self.assertTrue(all(s and s.get("tunnel_seal") is True for s in steps_64))
        self.assertEqual(skill_64.verify_done(st_64), "")

    def test_scenario_b_open_cavern_missing_side_wall_falls_through_to_flee_move(self) -> None:
        blocks = _tunnel_1x2_blocks(fy=20, x_range=range(-25, 10), left_wall=False, right_wall=True, ceiling=True)
        world = _Sprint1World(
            pos=(0.5, 20.0, 0.5),
            blocks=blocks,
            items={"cobblestone": 4},
            nearest_hostile={"name": "zombie", "pos": [4.5, 20.0, 0.5], "dist": 4.0},
        )
        world.on_move = lambda dest: world.st.update({
            "nearest_hostile": {"name": "zombie", "pos": [4.5, 20.0, 0.5], "dist": round(math.dist(dest, [4.5, 20.0, 0.5]), 2)},
        })
        ex, state = world.session()

        ok, _ = _drive_flow(escape_hazard_step(ex, state, "flee"), ex)
        self.assertTrue(ok)
        self.assertNotIn("place", ex.acts)
        self.assertIn("move_to", ex.acts)

    def test_scenario_c_insufficient_placeable_blocks_falls_through_to_flee_move(self) -> None:
        blocks = _tunnel_1x2_blocks(fy=20, x_range=range(-25, 10))
        world = _Sprint1World(
            pos=(0.5, 20.0, 0.5),
            blocks=blocks,
            items={"cobblestone": 1},
            nearest_hostile={"name": "zombie", "pos": [4.5, 20.0, 0.5], "dist": 4.0},
        )
        world.on_move = lambda dest: world.st.update({
            "nearest_hostile": {"name": "zombie", "pos": [4.5, 20.0, 0.5], "dist": round(math.dist(dest, [4.5, 20.0, 0.5]), 2)},
        })
        ex, state = world.session()

        ok, _ = _drive_flow(escape_hazard_step(ex, state, "flee"), ex)
        self.assertTrue(ok)
        self.assertNotIn("place", ex.acts)
        self.assertIn("move_to", ex.acts)

    def test_scenario_d_genuine_failed_flee_verify_done_returns_non_empty_reason(self) -> None:
        world = _Sprint1World(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat_ground(64, span=10),
            items={},
            nearest_hostile={"name": "zombie", "pos": [3.0, 64.0, 0.5], "dist": 2.5},
            pin_position=True,
        )
        ex, state = world.session()
        skill = EmergencyEscape(intent="flee")
        drive(skill, ex, state)

        reason = skill.verify_done(state)
        self.assertTrue(bool(reason))

    def test_scenario_e_skeleton_los_break_cover_preferred_and_verified(self) -> None:
        for hp in (20.0, 10.0):
            with self.subTest(hp=hp):
                blocks = _flat_ground(64, span=10)
                blocks[(1, 64, 1)] = "stone"
                blocks[(1, 65, 1)] = "stone"
                skeleton_pos = [5.5, 64.0, 0.5]
                world = _Sprint1World(
                    pos=(0.5, 64.0, 0.5),
                    blocks=blocks,
                    items={},
                    health=hp,
                    nearest_hostile={"name": "skeleton", "pos": list(skeleton_pos), "dist": 5.0},
                )
                world.on_move = lambda dest, sp=skeleton_pos, w=world: w.st.update({
                    "nearest_hostile": {
                        "name": "skeleton",
                        "pos": list(sp),
                        "dist": round(math.dist(dest, sp), 2),
                    },
                })
                ex, state = world.session()

                skill = EmergencyEscape(intent="flee")
                steps = drive(skill, ex, state)

                self.assertEqual(ex.acts, ["move_to"])
                self.assertEqual(len(steps), 1)
                self.assertTrue(steps[0] and steps[0].get("skeleton_cover") is True)
                self.assertEqual(world.move_requests[0]["pos"], [0.5, 64.0, 1.5])
                self.assertEqual(skill.verify_done(state), "")

    def test_scenario_f_abort_mid_run_lowers_shield_on_creeper_backstep(self) -> None:
        world = _Sprint1World(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat_ground(64, span=8),
            items={"shield": 1},
            nearest_hostile={"name": "creeper", "pos": [3.0, 64.0, 0.5], "dist": 2.5},
        )
        ex, state = world.session()
        skill = EmergencyEscape(intent="flee")

        done = skill.step(ex, state)
        self.assertFalse(done)
        self.assertEqual(ex.acts, ["control"])
        self.assertTrue(world.st.get("blocking"))
        self.assertEqual(state.get_active_escape(), "flee")

        skill.abort()
        self.assertFalse(world.st.get("blocking"))
        self.assertIsNone(state.get_active_escape())
        posture_shields = [params["shield"] for op, params in world.calls if op == "posture"]
        self.assertEqual(posture_shields, [True, False])


if __name__ == "__main__":
    unittest.main()
