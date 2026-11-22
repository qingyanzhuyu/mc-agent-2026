"""soak-deep-20261121/seg-01 三次真实死亡与 INC-002 离线回归单测（verified: offline）。

复现并锁死 5 项根因修复：
1. Death #2：`EmergencyEscape._has_active_hazard` 必须识别 `safety_intent["kind"]`，禁止误降级为 `scout_island`；
2. Death #2：`SafetyMonitor._dispatch` 不得擦除未过期 `hurt_interrupt` 意图，且 `Safety.hazards` 须感知 10–18m 远程箭矢受击；
3. Death #2：多怪逃生向量（18m 范围）避开近处蜘蛛时不得直冲 13.4m 远程骷髅或苦力怕；
4. Death #3：`move_to` 超时后兜底疾跑前必须在新坐标重扫敌群，避免盲冲前方苦力怕；
5. Death #1：立起 2 格挡箭墙切断面敌射界后，下一拍不得踏出掩体送死，且夜间残血/裸装重生地表驻留避险。
"""
from __future__ import annotations

import math
import time
import unittest
from copy import deepcopy

import mcagent.loop  # noqa: F401
import mcagent.skills  # noqa: F401
from mcagent.client import ApiError
from mcagent.loop import RulePlanner, reset_world_memo
from mcagent.runtime import Executor, Safety, SafetyMonitor, State
from mcagent.skills import base as skill_base
from mcagent.skills.combat_eval import TacticalDecision, counterattack_hostile, decide_combat_or_flee
from mcagent.skills.escape import _is_ranged_los_blocked, _resolve_flee_vector
from mcagent.skills.mine_return import return_to_surface_from_mine
from mcagent.skills.safety_escape import EmergencyEscape
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches, voxel_cache_for
from mcagent.skills.world_memo import reset_all_world_memos


def _reset() -> None:
    skill_base._COOLDOWN.clear()
    reset_world_memo()
    reset_all_world_memos()
    reset_all_voxel_caches()


class _FakeMonitorClient:
    def __init__(self, status: dict, events: list[dict] | None = None):
        self._status = deepcopy(status)
        self._events = list(events or [])
        self._seq = 0

    def status(self, timeout=10):
        return deepcopy(self._status)

    def events(self, since=0, limit=200):
        out = [e for e in self._events if int(e.get("seq", 0)) > since]
        if out:
            self._seq = max(self._seq, max(int(e.get("seq", 0)) for e in out))
        return {"events": out, "last_seq": max(since, self._seq), "has_more": False}

    def get_block(self, pos, use_cache=True):
        return {"name": "stone", "pos": list(pos), "diggable": True}

    def stop(self):
        return {"ok": True}


class TestSoakDeepSeg01Deaths(unittest.TestCase):
    def setUp(self) -> None:
        _reset()

    def tearDown(self) -> None:
        _reset()

    def test_emergency_escape_accepts_kind_key_in_safety_intent_at_ranged_dist(self) -> None:
        state = State()
        now = time.monotonic()
        state.set_status({
            "ready": True,
            "alive": True,
            "health": 13.0,
            "food": 17,
            "dimension": "overworld",
            "pos": [-65.5, 73.0, -10.5],
            "nearest_hostile": {"id": 115516, "name": "zombie", "dist": 12.6},
        })
        state.set_safety_intent({
            "kind": "flee",
            "why": "受击应急脱险 zombie 距离 12.6",
            "severity": "soft",
            "source": "hurt_interrupt",
            "at": now,
            "expires_at": now + 10.0,
        })
        self.assertEqual(EmergencyEscape.blocked(state), "")
        planner = RulePlanner(min_verified="offline")
        dec = planner._choose_safety_escape(state)
        self.assertIsNotNone(dec)
        assert dec is not None and dec.skill is not None
        self.assertEqual(dec.skill.name, "emergency_escape")
        self.assertEqual(dec.skill.args.get("intent"), "flee")

        # 过期意图不得放行
        state.set_safety_intent({
            "kind": "flee",
            "why": "expired",
            "at": now - 20.0,
            "expires_at": now - 1.0,
        })
        self.assertNotEqual(EmergencyEscape.blocked(state), "")

    def test_safety_monitor_preserves_hurt_interrupt_and_emits_arrow_hurt_flee(self) -> None:
        st_payload = {
            "ready": True,
            "alive": True,
            "health": 13.0,
            "food": 17,
            "armor_points": 0,
            "dimension": "overworld",
            "pos": [-63.5, 74.0, -13.5],
            "on_ground": True,
            "nearest_hostile": {"id": 115422, "name": "skeleton", "dist": 13.4},
        }
        client = _FakeMonitorClient(st_payload)
        state = State()
        ex = Executor(client, state)
        mon = SafetyMonitor(client, state, ex)
        # 首次 boot 追平空事件
        mon.tick(force_status=True)

        # 写入 hurt_interrupt 意图后，下一拍 hazards 为空不得将其清零
        now = time.monotonic()
        state.set_safety_intent({
            "kind": "flee",
            "why": "受击应急脱险 skeleton 距离 13.4",
            "severity": "soft",
            "source": "hurt_interrupt",
            "at": now,
            "expires_at": now + 10.0,
        })
        mon._dispatch(reconcile=True)
        self.assertIsNotNone(state.safety_intent)
        assert state.safety_intent is not None
        self.assertEqual(state.safety_intent.get("kind"), "flee")

        # 13.4m 远程骷髅箭矢命中：Safety.hazards 必须生成 flee 意图
        client._events.append({
            "seq": 65,
            "t": 1795281286204,
            "type": "hurt",
            "health": 9.0,
            "damage": 4.0,
            "source": "arrow",
            "source_entity": {"id": 115422, "name": "skeleton"},
        })
        client._status["health"] = 9.0
        state.drain(client)
        state.set_status(client._status)
        hazards = Safety(state.config).hazards(state)
        flee_h = [h for h in hazards if h["kind"] == "flee"]
        self.assertTrue(flee_h, f"expected arrow hurt flee hazard, got {hazards}")
        self.assertTrue(flee_h[0].get("hurt_triggered"))
        self.assertEqual(flee_h[0]["severity"], "soft")

        # 血量跌至 hp_crit (<=8) 时，远程受击 flee 应升级为 urgent 抢占非脱险动作
        client._status["health"] = 6.0
        state.set_status(client._status)
        hazards_low = Safety(state.config).hazards(state)
        flee_low = [h for h in hazards_low if h["kind"] == "flee"]
        self.assertTrue(flee_low)
        self.assertEqual(flee_low[0]["severity"], "urgent")

        # 脱险拉远（>14m 残血远程预警圈）并 clear_recent_hurt 后不再复发
        client._status["nearest_hostile"] = {"id": 115422, "name": "skeleton", "dist": 15.5}
        state.set_status(client._status)
        state.clear_recent_hurt()
        hazards_after = Safety(state.config).hazards(state)
        self.assertFalse(any(h["kind"] == "flee" for h in hazards_after))

    def test_flee_multi_hostile_avoids_running_into_13m_skeleton(self) -> None:
        # 精确复现 Death #2 几何：玩家 (-50.5, 74, -22.3)，蜘蛛东南 (-48, 73, -26.9)，骷髅西北 (-57.3, 74, -10.7)
        class _DualThreatWorld(GridWorld):
            def get_entities(self, radius=32, types=None):
                ents = [
                    {"id": 115471, "type": "hostile", "name": "spider", "pos": [-48.0, 73.0, -26.9], "dist": 5.3},
                    {"id": 115422, "type": "hostile", "name": "skeleton", "pos": [-57.3, 74.0, -10.7], "dist": 13.4},
                ]
                return {"entities": [e for e in ents if e["dist"] <= radius]}

        blocks = {(x, 73, z): "grass_block" for x in range(-70, -35) for z in range(-40, 0)}
        w = _DualThreatWorld(blocks=blocks, pos=[-50.5, 74.0, -22.3])
        ex, state = w.session()
        state.events.append({
            "type": "hurt",
            "source": "arrow",
            "source_entity": {"id": 115422, "name": "skeleton"},
        })
        ux, uz, _ = _resolve_flee_vector(
            ex,
            -50.5,
            74.0,
            -22.3,
            {"id": 115471, "name": "spider", "dist": 5.3, "pos": [-48.0, 73.0, -26.9]},
            voxel_cache_for(state),
            "overworld",
            state=state,
        )
        tx, tz = -50.5 + ux * 10.0, -22.3 + uz * 10.0
        dist_to_skeleton = math.hypot(tx - (-57.3), tz - (-10.7))
        dist_to_spider = math.hypot(tx - (-48.0), tz - (-26.9))
        # 旧逻辑直冲 (-56.2, -11.8) 距骷髅仅 1.5m；新逻辑必须同时远离蜘蛛且不直扑骷髅
        self.assertGreater(dist_to_skeleton, 11.0, f"fled toward skeleton! target=({tx:.1f},{tz:.1f})")
        self.assertGreater(dist_to_spider, 7.0, f"fled toward spider! target=({tx:.1f},{tz:.1f})")

    def test_flee_rescans_hostiles_before_retry_sprint_avoiding_creeper(self) -> None:
        # 复现 Death #3：初始远离南侧僵尸（向 +Z 逃），move_to 超时时已走到 (0.5, 64, 10.5)，此时正前方 +Z (0.5, 64, 13.5) 出现苦力怕
        blocks = {(x, 63, z): "grass_block" for x in range(-20, 21) for z in range(-10, 30)}

        class _CreeperAheadWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 64.0, 0.5])
                self.phase = 0
                self.control_yaws: list[float] = []
                self.st.update(
                    dimension="overworld",
                    ready=True,
                    alive=True,
                    health=15.0,
                    nearest_hostile={"id": 1, "name": "zombie", "dist": 4.0, "pos": [0.5, 64.0, -3.5]},
                )

            def get_entities(self, radius=32, types=None):
                if self.phase == 0:
                    return {"entities": [{"id": 1, "type": "hostile", "name": "zombie", "pos": [0.5, 64.0, -3.5], "dist": 4.0}]}
                return {"entities": [
                    {"id": 2, "type": "hostile", "name": "creeper", "pos": [0.5, 64.0, 13.5], "dist": 3.0},
                ]}

            def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
                # 模拟 5s 超时前玩家已经走到 (0.5, 64, 10.5)，迎面撞见苦力怕
                self.phase = 1
                self.st["pos"] = [0.5, 64.0, 10.5]
                self.st["nearest_hostile"] = {"id": 2, "name": "creeper", "dist": 3.0, "pos": [0.5, 64.0, 13.5]}
                raise ApiError("timeout", "Read timed out")

            def control(self, **kwargs):
                if "yaw" in kwargs:
                    self.control_yaws.append(float(kwargs["yaw"]))
                yaw = float(kwargs.get("yaw", 0.0))
                dx, dz = -math.sin(yaw) * 4.0, -math.cos(yaw) * 4.0
                self.st["pos"] = [self.st["pos"][0] + dx, 64.0, self.st["pos"][2] + dz]
                self.st["nearest_hostile"] = {"id": 2, "name": "creeper", "dist": 7.5}
                return {"ms": kwargs.get("ms", 800), "pos": list(self.st["pos"]), "moved": 4.0}

        w = _CreeperAheadWorld()
        ex, state = w.session()
        drive(EmergencyEscape(intent="flee"), ex, state)
        self.assertTrue(w.control_yaws, "expected fallback control sprint after move_to timeout")
        retry_yaw = w.control_yaws[-1]
        # 苦力怕在 +Z 方向 (dz=+3)；sprint 方向 (-sin(yaw), -cos(yaw)) 的 Z 分量必须为负（背离苦力怕），绝不可继续冲向 +Z
        sprint_dz = -math.cos(retry_yaw)
        self.assertLess(sprint_dz, -0.5, f"retry sprint ran toward creeper! yaw={retry_yaw:.2f}, dz={sprint_dz:.2f}")

    def test_flee_behind_barricade_keeps_cover_instead_of_stepping_out(self) -> None:
        # 复现 Death #1：玩家 (-22.9, 66, 57.7) 残血 hp=1.0，已在 (-22, 66, 57) 与 (-22, 67, 57) 立好 2 格挡箭墙，骷髅在 (-16.5, 65.8, 56.1)
        blocks = {(x, 65, z): "grass_block" for x in range(-35, -10) for z in range(45, 70)}
        blocks[(-22, 66, 57)] = "dirt"
        blocks[(-22, 67, 57)] = "dirt"

        class _BarricadedWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[-22.9, 66.0, 57.7])
                self.move_calls = 0
                self.control_calls = 0
                self.st.update(
                    dimension="overworld",
                    ready=True,
                    alive=True,
                    health=1.0,
                    nearest_hostile={"id": 115017, "name": "skeleton", "dist": 6.4, "pos": [-16.5, 65.8, 56.1]},
                )

            def get_entities(self, radius=32, types=None):
                return {"entities": [
                    {"id": 115017, "type": "hostile", "name": "skeleton", "pos": [-16.5, 65.8, 56.1], "dist": 6.4},
                ]}

            def move_to(self, *args, **kwargs):
                self.move_calls += 1
                return super().move_to(*args, **kwargs)

            def control(self, **kwargs):
                self.control_calls += 1
                yaw = float(kwargs.get("yaw", 0.0))
                self.st["pos"] = [
                    self.st["pos"][0] - math.sin(yaw) * 2.0,
                    66.0,
                    self.st["pos"][2] - math.cos(yaw) * 2.0,
                ]
                return {"ms": kwargs.get("ms", 450), "pos": list(self.st["pos"]), "moved": 2.0}

        w = _BarricadedWorld()
        ex, state = w.session()
        self.assertTrue(
            _is_ranged_los_blocked(ex, -22.9, 66.0, 57.7, [-16.5, 65.8, 56.1], voxel_cache_for(state), "overworld")
        )
        steps = drive(EmergencyEscape(intent="flee"), ex, state)
        self.assertEqual(w.move_calls, 0, "must not step out of barricade cover via move_to")
        self.assertGreaterEqual(w.control_calls, 1)
        self.assertTrue(any(s.get("skeleton_cover") for s in steps))

    def test_nighttime_naked_and_low_hp_survival_guard(self) -> None:
        planner = RulePlanner(min_verified="offline")

        # 1) 夜间地表空手裸装重生 -> 驻留避险待天亮
        st_naked_night = State()
        st_naked_night.set_status({
            "ready": True,
            "alive": True,
            "health": 20.0,
            "food": 20,
            "armor_points": 0,
            "dimension": "overworld",
            "is_day": False,
            "pos": [-63.5, 69.0, 16.5],
        })
        st_naked_night.set_inventory({"items": []})
        dec_naked = planner.choose_proactive_survival(st_naked_night)
        self.assertIsNotNone(dec_naked)
        assert dec_naked is not None
        self.assertIsNone(dec_naked.skill)
        self.assertEqual(dec_naked.source, "idle")

        # 2) 夜间地表残血无甲无粮（复现 Death #1 hp=5, food=16）-> 驻留避险，不再强推 smelt_iron
        st_low_night = State()
        st_low_night.set_status({
            "ready": True,
            "alive": True,
            "health": 5.0,
            "food": 16,
            "armor_points": 0,
            "dimension": "overworld",
            "is_day": False,
            "pos": [-12.0, 71.0, 49.0],
        })
        st_low_night.set_inventory({"items": [{"name": "stone_pickaxe", "count": 1, "slot": 0}, {"name": "stone_sword", "count": 1, "slot": 1}]})
        dec_low = planner.choose_proactive_survival(st_low_night)
        self.assertIsNotNone(dec_low)
        assert dec_low is not None
        self.assertIsNone(dec_low.skill)
        self.assertEqual(dec_low.source, "idle")

        # 3) 白天或地下矿洞或健康武装状态不得被拦截
        st_day = State()
        st_day.set_status({
            "ready": True,
            "alive": True,
            "health": 20.0,
            "food": 20,
            "armor_points": 0,
            "dimension": "overworld",
            "is_day": True,
            "pos": [-63.5, 69.0, 16.5],
        })
        st_day.set_inventory({"items": []})
        dec_day = planner.choose_proactive_survival(st_day)
        self.assertFalse(dec_day is not None and dec_day.source == "idle")

        st_underground = State()
        st_underground.set_status({
            "ready": True,
            "alive": True,
            "health": 20.0,
            "food": 20,
            "armor_points": 0,
            "dimension": "overworld",
            "is_day": False,
            "pos": [-63.5, 30.0, 16.5],
        })
        st_underground.set_inventory({"items": []})
        dec_ug = planner.choose_proactive_survival(st_underground)
        self.assertFalse(dec_ug is not None and dec_ug.source == "idle")

    def test_return_to_surface_places_cobblestone_step_in_open_cave_without_pickaxe(self) -> None:
        # 洞穴空腔内四周齐脚均为 air、脚下为 stone，背包无镐但有圆石 -> 必须放圆石台阶爬出而非秒返 False
        blocks = {(x, 43, z): "stone" for x in range(-3, 5) for z in range(-3, 4)}
        blocks[(0, 46, 0)] = "stone"  # 顶部岩盖触发 is_underground_in_mine

        class _OpenCaveWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 44.0, 0.5], items={"cobblestone": 16})
                self.st.update(dimension="overworld", ready=True, alive=True, health=20.0, food=20.0)
                self.placed: list[list[int]] = []

            def place(self, item, pos=None, **kwargs):
                cell = tuple(int(v) for v in pos)
                self.blocks[cell] = item
                self.placed.append(list(cell))
                return {"placed": item, "pos": list(cell)}

            def dig(self, pos, **kwargs):
                cell = tuple(int(v) for v in pos)
                self.blocks[cell] = "air"
                return {"dug": "stone"}

        w = _OpenCaveWorld()
        ex, state = w.session()
        gen = return_to_surface_from_mine(ex, state, target_y=48.0, max_climb_steps=2)
        steps = []
        try:
            while True:
                steps.append(next(gen))
        except StopIteration:
            pass
        self.assertTrue(w.placed, f"expected cobblestone step placed into cave air, steps={steps}")
        self.assertGreaterEqual(w.st["pos"][1], 45.0)

    def test_return_to_surface_hand_digs_stone_when_pickaxe_gone_preventing_softlock(self) -> None:
        # 复现 seg-01 末尾 [-30, 44, 70] 断镐软锁：无镐且四周仅剩实心石头 -> 启用防软锁徒手破岩开阶脱困
        blocks = {
            (0, 43, 0): "stone",
            (1, 44, 0): "stone",
            (0, 46, 0): "stone",
            (1, 45, 0): "stone",
            (1, 46, 0): "stone",
            (1, 47, 0): "stone",
        }

        class _StoneTrapWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 44.0, 0.5], items={"raw_iron": 8})
                self.st.update(dimension="overworld", ready=True, alive=True, health=20.0, food=20.0)
                self.dug: list[list[int]] = []

            def dig(self, pos, **kwargs):
                cell = tuple(int(v) for v in pos)
                self.blocks[cell] = "air"
                self.dug.append(list(cell))
                return {"dug": "stone"}

        w = _StoneTrapWorld()
        ex, state = w.session()
        gen = return_to_surface_from_mine(ex, state, target_y=48.0, max_climb_steps=1)
        steps = []
        try:
            while True:
                steps.append(next(gen))
        except StopIteration:
            pass
        self.assertGreaterEqual(len(w.dug), 3, f"expected hand-dig stone anti-softlock climb out, steps={steps}")
        self.assertGreaterEqual(w.st["pos"][1], 45.0)

    def test_flee_fast_pursuer_spider_uses_sprint_bursts_not_slow_move_to(self) -> None:
        # 复现 seg-02 t=817s 蜘蛛追杀：蜘蛛移速 6m/s 高于 move_to 4.3m/s，近距逃跑必须用连续疾跑跳而非慢速 move_to
        blocks = {(x, 63, z): "grass_block" for x in range(-25, 25) for z in range(-25, 25)}

        class _SpiderChaseWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 64.0, 0.5], items={})
                self.move_calls = 0
                self.control_calls = 0
                self.st.update(
                    dimension="overworld",
                    ready=True,
                    alive=True,
                    health=7.5,
                    nearest_hostile={"id": 119482, "name": "spider", "dist": 4.0, "pos": [0.5, 64.0, -3.5]},
                )

            def get_entities(self, radius=32, types=None):
                d = math.dist(self.st["pos"][:3], [0.5, 64.0, -3.5])
                return {"entities": [{"id": 119482, "type": "hostile", "name": "spider", "pos": [0.5, 64.0, -3.5], "dist": d}]}

            def move_to(self, *args, **kwargs):
                self.move_calls += 1
                return super().move_to(*args, **kwargs)

            def control(self, **kwargs):
                self.control_calls += 1
                yaw = float(kwargs.get("yaw", 0.0))
                self.st["pos"] = [
                    self.st["pos"][0] - math.sin(yaw) * 4.5,
                    64.0,
                    self.st["pos"][2] - math.cos(yaw) * 4.5,
                ]
                d = math.dist(self.st["pos"][:3], [0.5, 64.0, -3.5])
                self.st["nearest_hostile"] = {"id": 119482, "name": "spider", "dist": d, "pos": [0.5, 64.0, -3.5]}
                return {"ms": kwargs.get("ms", 900), "pos": list(self.st["pos"]), "moved": 4.5}

        w = _SpiderChaseWorld()
        ex, state = w.session()
        drive(EmergencyEscape(intent="flee"), ex, state)
        self.assertEqual(w.move_calls, 0, "must not use slow non-sprinting move_to while chased by spider")
        self.assertGreaterEqual(w.control_calls, 2)

    def test_counterattack_finishes_wounded_lone_spider_at_7_5_hp(self) -> None:
        # 复现 seg-02 t=808s：石剑首轮已重创蜘蛛至残血，次轮 hp=7.5 时面对落单蜘蛛应直接补刀击杀而非转身被咬死
        from unittest.mock import patch

        class _FinishSpiderWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks={(x, 63, z): "grass_block" for x in range(-5, 6) for z in range(-5, 6)},
                                 pos=[0.5, 64.0, 0.5], items={"stone_sword": 1})
                self.attack_rounds = 0
                self.spider_alive = True
                self.st.update(
                    dimension="overworld",
                    ready=True,
                    alive=True,
                    health=14.0,
                    held={"name": "stone_sword", "count": 1},
                    nearest_hostile={"id": 119482, "name": "spider", "dist": 2.0, "pos": [2.5, 64.0, 0.5]},
                )

            def get_entities(self, radius=32, types=None):
                if not self.spider_alive:
                    return {"entities": []}
                return {"entities": [{"id": 119482, "type": "hostile", "name": "spider", "pos": [2.5, 64.0, 0.5], "dist": 2.0}]}

            def attack(self, entity_id, timeout_ms=20000, **kwargs):
                self.attack_rounds += 1
                if self.attack_rounds == 1:
                    self.st["health"] = 7.5
                    return {"killed": False, "hits": 2}
                self.spider_alive = False
                self.st["nearest_hostile"] = None
                return {"killed": True, "hits": 2}

        w = _FinishSpiderWorld()
        ex, state = w.session()
        dec = decide_combat_or_flee(ex, state)
        self.assertEqual(dec.action, "fight")
        with patch("mcagent.skills.combat_eval.wait_attack_cooldown", return_value=None):
            gen = counterattack_hostile(ex, state, dec)
            ok = False
            try:
                while True:
                    next(gen)
            except StopIteration as stop:
                ok = bool(stop.value)
        self.assertTrue(ok, "expected counterattack_hostile to finish lone wounded spider at 7.5 HP")
        self.assertEqual(w.attack_rounds, 2)

    def test_mine_stone_skips_stone_touching_lava(self) -> None:
        from mcagent.loop.starter_skills import MineStone

        blocks = {
            (0, 63, 0): "stone",
            (2, 64, 0): "stone",
            (3, 64, 0): "lava",   # 紧邻 (2,64,0)，绝不可挖
            (-2, 64, 0): "stone",  # 安全石头
        }
        w = GridWorld(blocks=blocks, pos=[0.5, 64.0, 0.5], items={"stone_pickaxe": 1})
        ex, state = w.session()
        self.assertTrue(MineStone._touches_lava(ex, [2, 64, 0], vc=voxel_cache_for(state), dim="overworld"))
        self.assertFalse(MineStone._touches_lava(ex, [-2, 64, 0], vc=voxel_cache_for(state), dim="overworld"))

    def test_death_clears_stale_safety_intent_and_low_hp_iron_crafts_shield_first(self) -> None:
        # 1) death/spawn 事件必须清空过期 escape_lava 意图，禁止重生后发无意义脱险
        st_payload = {"ready": True, "alive": True, "health": 20.0, "food": 20, "dimension": "overworld", "pos": [-63.5, 69.0, 16.5]}
        client = _FakeMonitorClient(st_payload)
        state = State()
        state.drain(client)
        state.set_safety_intent({"kind": "escape_lava", "why": "在岩浆里", "severity": "urgent"})
        client._events.append({"seq": 343, "t": 1795284168317, "type": "death", "pos": [-2.5, 64, 61.5]})
        state.drain(client)
        self.assertIsNone(state.safety_intent)

        # 2) 低血量（hp=6.8）持 3 铁锭与木板且无盾：优先快造盾牌而非先造桶去追美西螈
        planner = RulePlanner(min_verified="offline")
        st_low = State()
        st_low.set_status({
            "ready": True,
            "alive": True,
            "health": 6.8,
            "food": 17,
            "armor_points": 0,
            "dimension": "overworld",
            "is_day": True,
            "pos": [-61.6, 66.0, 27.5],
        })
        st_low.set_inventory({
            "items": [
                {"name": "iron_ingot", "count": 3, "slot": 0},
                {"name": "oak_planks", "count": 8, "slot": 1},
                {"name": "stone_sword", "count": 1, "slot": 2},
                {"name": "cooked_beef", "count": 4, "slot": 3},
            ]
        })
        dec = planner.choose_proactive_survival(st_low)
        self.assertIsNotNone(dec)
        assert dec is not None and dec.skill is not None
        self.assertEqual(dec.skill.name, "craft_iron_gear")
        self.assertEqual(dec.skill.args.get("item"), "shield")

    def test_climb_out_stairs_avoids_direction_touching_hidden_lava(self) -> None:
        # 复现 seg-04 t=89s 死亡：+X 方向待挖阶梯紧邻隐蔽岩浆 (2, 59, 0)，-X 方向为安全干石 -> 必须绕开 +X 改选 -X
        from mcagent.skills.mine_return import _climb_out_stairs

        blocks = {
            (0, 57, 0): "stone",
            (0, 60, 0): "stone",
            # +X 方向（优先遍历）紧邻隐蔽岩浆
            (1, 58, 0): "stone",
            (1, 59, 0): "stone",
            (1, 60, 0): "stone",
            (1, 61, 0): "stone",
            (2, 59, 0): "lava",
            # -X 方向完全安全
            (-1, 58, 0): "stone",
            (-1, 59, 0): "stone",
            (-1, 60, 0): "stone",
            (-1, 61, 0): "stone",
        }

        class _HiddenLavaClimbWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 58.0, 0.5], items={"stone_pickaxe": 1})
                self.st.update(dimension="overworld", ready=True, alive=True, health=20.0, food=20.0)
                self.dug: list[tuple[int, int, int]] = []

            def dig(self, pos, **kwargs):
                cell = tuple(int(v) for v in pos)
                self.blocks[cell] = "air"
                self.dug.append(cell)
                return {"dug": "stone"}

        w = _HiddenLavaClimbWorld()
        ex, state = w.session()
        gen = _climb_out_stairs(ex, state, target_y=64.0, max_steps=1)
        try:
            while True:
                next(gen)
        except StopIteration:
            pass
        self.assertTrue(w.dug, "expected climbing out via safe -X direction")
        self.assertFalse(any(c[0] == 1 for c in w.dug), f"dug into lava-adjacent +X cells: {w.dug}")
        self.assertTrue(any(c[0] == -1 for c in w.dug), f"expected safe -X climb cells: {w.dug}")
        self.assertLess(w.st["pos"][0], 0.0)

    def test_smelt_iron_void_risk_blacklist_persists_across_chunks_and_escape_move(self) -> None:
        # 复现 seg-05 INC-001：smelt_iron 趋近悬崖铁矿 [-40, 60, 92] 触发 void_risk，脱险位移跨区块后仍必须持续拉黑该矿
        from mcagent.runtime import Replan
        from mcagent.skills.iron import SmeltIron, reset_iron_memo

        reset_iron_memo()
        cliff_ore = [-40, 60, 92]

        class _CrossChunkCliffWorld(GridWorld):
            def find_blocks(self, types=None, radius=32, limit=64, **_):
                if types and any("iron" in t for t in types):
                    return {"blocks": [{"name": "iron_ore", "pos": list(cliff_ore)}]}
                return {"blocks": []}

            def move_to(self, *args, **kwargs):
                self.st["pos"] = [-44.7, 62.0, 93.3]
                raise Replan("动作被安全层抢占: void_risk: 脚下连续空气且正在下落：疑似坠落/深坑")

        w = _CrossChunkCliffWorld(
            blocks={(-17, 63, 51): "stone", tuple(cliff_ore): "iron_ore"},
            pos=[-16.5, 64.0, 51.5],
            items={"stone_pickaxe": 1, "coal": 4, "furnace": 1},
        )
        ex, state = w.session()
        state.st.update(dimension="overworld", ready=True, alive=True, health=20.0)
        with self.assertRaisesRegex(Replan, "void_risk"):
            drive(SmeltIron(count=3), ex, state)

        # 模拟脱险后移动到另一区块 [-52.7, 54.2, 83.7]（距离 >11 格，会触发 clear_stance_blacklist_if_moved）
        w.st["pos"] = [-52.7, 54.2, 83.7]
        state.refresh_status(ex.client, max_age_s=0)
        cands = SmeltIron()._ore_candidates(ex, state, radius=32)
        self.assertNotIn(cliff_ore, cands, "cliff ore must remain globally blacklisted across chunks after void_risk")

    def test_smelt_iron_blocks_low_hp_unarmored_deep_cave_descent(self) -> None:
        # 复现 seg-05 t=239s：hp=5 无甲无盾无粗铁时禁止再下矿寻铁；hp=9.5 时过滤深洞远矿 [-77, 43, 60]
        from mcagent.skills.iron import SmeltIron, reset_iron_memo

        reset_iron_memo()
        st = State()
        st.set_status({"dimension": "overworld", "ready": True, "alive": True, "health": 5.0, "armor_points": 0, "pos": [-52.5, 50.0, 76.7]})
        st.set_inventory({"items": [{"name": "stone_pickaxe", "count": 1, "slot": 0}, {"name": "coal", "count": 4, "slot": 1}]})
        self.assertIn("低血量", SmeltIron.blocked(st))

        # 若背包已有粗铁，则允许继续熔炼以补齐铁盾
        st.set_inventory({
            "items": [
                {"name": "stone_pickaxe", "count": 1, "slot": 0},
                {"name": "coal", "count": 4, "slot": 1},
                {"name": "raw_iron", "count": 3, "slot": 2},
            ]
        })
        self.assertEqual(SmeltIron.blocked(st), "")

        # hp=9.5 无甲无盾：跳过深洞远矿 [-77, 43, 60]，保留地表/浅层安全矿 [-50, 62, 76]
        deep_ore = [-77, 43, 60]
        shallow_ore = [-50, 62, 76]

        class _MixedOreWorld(GridWorld):
            def find_blocks(self, types=None, radius=32, limit=64, **_):
                if types and any("iron" in t for t in types):
                    return {
                        "blocks": [
                            {"name": "iron_ore", "pos": list(deep_ore)},
                            {"name": "iron_ore", "pos": list(shallow_ore)},
                        ]
                    }
                return {"blocks": []}

        w = _MixedOreWorld(
            blocks={(-53, 53, 76): "stone", tuple(deep_ore): "iron_ore", tuple(shallow_ore): "iron_ore"},
            pos=[-52.5, 54.0, 76.7],
            items={"stone_pickaxe": 1, "coal": 4},
        )
        w.st.update(dimension="overworld", ready=True, alive=True, health=9.5, armor_points=0)
        ex, st2 = w.session()
        cands = SmeltIron()._ore_candidates(ex, st2, radius=32)
        self.assertNotIn(deep_ore, cands)
        self.assertIn(shallow_ore, cands)

    def test_void_risk_zero_hostile_flee_never_sprints_off_ravine_cliff(self) -> None:
        # 复现 seg-05 t=212s/230s：无敌对生物的 void_risk 不得触发盲目 12 格 move_to 或滞空疾跑跳进峡谷
        from mcagent.skills.escape import _flee_hostile, _is_sprint_yaw_safe

        blocks = {
            # 身后 (+Z) 是安全实心悬崖顶，前方 (-Z, yaw=0.0) 是 10 格深峡谷全空气
            (0, 61, 1): "stone",
            (0, 61, 2): "stone",
            (1, 61, 1): "stone",
            (-1, 61, 1): "stone",
            # 脚下 (0, 61, 0) 为空气（刚滑出悬崖边缘）
        }

        class _CliffVoidWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 62.0, 0.5], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=11.0,
                    on_ground=False, nearest_hostile=None,
                )
                self.controls: list[dict] = []
                self.moves: list[list[float]] = []

            def move_to(self, x=None, z=None, y=None, pos=None, rng=1, timeout=None, **kwargs):
                target = list(pos) if pos is not None else [x, y, z]
                self.moves.append(target)
                self.st["pos"] = list(target)
                self.st["on_ground"] = True
                return {"pos": list(target)}

            def control(self, **kwargs):
                self.controls.append(dict(kwargs))
                return {"pos": list(self.st["pos"])}

        w = _CliffVoidWorld()
        ex, state = w.session()
        cache = voxel_cache_for(state)
        # 滞空在悬崖边缘时，朝 -Z 深坑 (yaw=0.0) 的 _is_sprint_yaw_safe 必须返回 False
        self.assertFalse(_is_sprint_yaw_safe(ex, 0.5, 62.0, 0.5, 0.0, cache, "overworld"))

        gen = _flee_hostile(ex, state)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(ok)
        self.assertEqual(w.controls, [], "zero-hostile void_risk must never blind sprint-jump off cliff")
        self.assertTrue(w.moves, "expected short move onto confirmed solid cliff ground")
        self.assertGreaterEqual(w.moves[0][2], 1.0, f"should step back onto safe +Z ledge, got {w.moves[0]}")

    def test_close_melee_burst_blocked_in_cave_immediately_sprints_alternate_corridor(self) -> None:
        # 复现 seg-05 t=265s：洞穴内贴脸僵尸(dist=0.5)首段疾跑受阻(burst_blocked)时，立即改沿侧向通道二次疾跑，禁止直接回退慢速 move_to
        from mcagent.skills.escape import _flee_hostile

        blocks: dict[tuple[int, int, int], str] = {}
        # 地板全铺石块
        for x in range(-6, 7):
            for z in range(-6, 7):
                blocks[(x, 41, z)] = "stone"
        # 逃离方向 -Z 被 2 格高石墙挡住（位于 z=-2，让首段跑 0.7m 后撞墙，+X 为畅通洞穴通道）
        for x in range(-2, 3):
            blocks[(x, 42, -2)] = "stone"
            blocks[(x, 43, -2)] = "stone"

        class _TightCaveZombieWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 42.0, 0.5], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=5.0,
                    nearest_hostile={"id": 124816, "name": "zombie", "dist": 0.5, "pos": [0.5, 42.0, 1.0]},
                )
                self.controls: list[dict] = []
                self.move_calls: int = 0

            def control(self, **kwargs):
                self.controls.append(dict(kwargs))
                if len(self.controls) == 1:
                    # 首段疾跑被岩壁/拐角卡住，仅移动 0.7 格
                    self.st["pos"] = [0.5, 42.0, -0.2]
                    self.st["nearest_hostile"] = {"id": 124816, "name": "zombie", "dist": 1.2, "pos": [0.5, 42.0, 1.0]}
                else:
                    # 侧向二次疾跑沿畅通通道脱离
                    yaw = float(kwargs.get("yaw", 0.0))
                    nx = self.st["pos"][0] + (-math.sin(yaw)) * 8.0
                    nz = self.st["pos"][2] + (-math.cos(yaw)) * 8.0
                    self.st["pos"] = [nx, 42.0, nz]
                    self.st["nearest_hostile"] = {"id": 124816, "name": "zombie", "dist": 9.0, "pos": [0.5, 42.0, 1.0]}
                return {"pos": list(self.st["pos"])}

            def move_to(self, *args, **kwargs):
                self.move_calls += 1
                return {"pos": list(self.st["pos"])}

        w = _TightCaveZombieWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(ok)
        self.assertGreaterEqual(len(w.controls), 2, f"expected immediate alternate sprint after burst_blocked, got {w.controls}")
        self.assertEqual(w.move_calls, 0, "should escape via alternate corridor sprint without stalling in move_to")

    def test_skeleton_behind_cover_pinned_against_wall_falls_through_instead_of_looping(self) -> None:
        # 复现 seg-06 INC-001 Cluster 1（102x 刷屏）：掩体后背靠墙体时短疾跑位移 <0.5 格，绝不可原位返回 True 假完成，必须落入完整脱离
        blocks = {(x, 67, z): "stone" for x in range(-10, 11) for z in range(-10, 11)}
        # 骷髅在 +Z (0.5, 68, 8.5)，掩体墙在 (0, 68..69, 2) 切断射界
        blocks[(0, 68, 2)] = "stone"
        blocks[(0, 69, 2)] = "stone"

        class _PinnedCoverWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 68.0, 0.5], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=15.0,
                    nearest_hostile={"id": 88, "name": "skeleton", "dist": 8.0, "pos": [0.5, 68.0, 8.5]},
                )
                self.controls = 0
                self.moves = 0

            def control(self, **kwargs):
                self.controls += 1
                # 模拟被地形角落卡住：短疾跑位移 0.0 格
                return {"pos": list(self.st["pos"]), "moved": 0.0}

            def move_to(self, *args, **kwargs):
                self.moves += 1
                self.st["pos"] = [0.5, 68.0, -11.5]
                self.st["nearest_hostile"] = {"id": 88, "name": "skeleton", "dist": 20.0, "pos": [0.5, 68.0, 8.5]}
                return {"pos": list(self.st["pos"])}

        w = _PinnedCoverWorld()
        ex, state = w.session()
        steps = drive(EmergencyEscape(intent="flee"), ex, state)
        self.assertGreaterEqual(w.controls, 1)
        self.assertGreaterEqual(w.moves, 1, f"pinned cover sprint must fall through to real move_to escape, steps={steps}")

    def test_skeleton_close_already_los_blocked_skips_zero_disp_cover_loop(self) -> None:
        # 复现 seg-06 INC-001 Cluster 2（332x 刷屏）：骷髅距 2.4 格且已在拐角掩体后（already_los_blocked=True），
        # 不得再走 0 位移 cover move_to 假完成，必须直接触发贴脸疾跑/远逃
        blocks = {(x, 68, z): "stone" for x in range(-10, 11) for z in range(-10, 11)}
        # 拐角石柱在 (0, 69..70, 1) 阻断玩家 (0.5, 69, 0.5) 与近距骷髅 (0.5, 69, 2.9) 的直线
        blocks[(0, 69, 1)] = "stone"
        blocks[(0, 70, 1)] = "stone"

        class _CloseCornerSkelWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 69.0, 0.5], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=7.3,
                    nearest_hostile={"id": 89, "name": "skeleton", "dist": 2.4, "pos": [0.5, 69.0, 2.9]},
                )
                self.cover_moves = 0
                self.controls = 0

            def control(self, **kwargs):
                self.controls += 1
                self.st["pos"] = [0.5, 69.0, -7.5]
                self.st["nearest_hostile"] = {"id": 89, "name": "skeleton", "dist": 10.4, "pos": [0.5, 69.0, 2.9]}
                return {"pos": list(self.st["pos"]), "moved": 8.0}

            def move_to(self, *args, **kwargs):
                if kwargs.get("rng") == 0:
                    self.cover_moves += 1
                    return {"pos": list(self.st["pos"])}
                return super().move_to(*args, **kwargs)

        w = _CloseCornerSkelWorld()
        ex, state = w.session()
        steps = drive(EmergencyEscape(intent="flee"), ex, state)
        self.assertEqual(w.cover_moves, 0, f"must not enter 0-disp skeleton cover move_to when already LOS-blocked: {steps}")
        self.assertGreaterEqual(w.controls, 1, "expected immediate close-range sprint burst away from skeleton")

    def test_eat_food_passes_when_use_item_confirms_food_increase_despite_laggy_inventory(self) -> None:
        # 复现 seg-06 INC-002：use_item 返回 food_before=17 -> food=19，但服务端背包槽位扣减包延迟 1 拍仍显示 chicken=1
        from mcagent.loop.starter_skills import EatFood

        class _LaggyEatWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks={(0, 63, 0): "stone"}, pos=[0.5, 64.0, 0.5], items={"chicken": 1})
                self.st.update(dimension="overworld", ready=True, alive=True, health=10.3, food=17)

            def use_item(self, item, **kwargs):
                self.st["food"] = 19
                # 故意延迟扣减 self.items["chicken"]，模拟 Mineflayer set_slot 晚于 consume resolve
                return {"used": item, "food_before": 17, "food": 19, "health_before": 10.3, "health": 10.3}

        w = _LaggyEatWorld()
        ex, state = w.session()
        state.refresh_inventory(ex.client)
        before_counts = {"chicken": state.count("chicken")}
        sk = EatFood()
        drive(sk, ex, state)
        self.assertEqual(sk.verify_done(state, before_counts), "")
        self.assertTrue(state.inventory_dirty, "must mark inventory_dirty when slot sync lagged behind confirmed eat")

    def test_nighttime_low_hp_hold_guard_persists_when_hostile_within_10_to_12m(self) -> None:
        # 复现 seg-07 t=318s：夜间 hp=1.1 无甲且敌怪处于 8.8~11.5 格（safe_12=False）时，
        # 仍必须命中夜间残血驻留守卫，严禁退回派发 survey_island/scout_island/breed_animal 冲进暗林送死
        planner = RulePlanner(min_verified="offline")
        st = State()
        st.set_status({
            "ready": True, "alive": True, "health": 1.1, "food": 17, "armor_points": 0,
            "dimension": "overworld", "is_day": False, "time_of_day": 18797,
            "pos": [-66.5, 64.0, -46.5],
            "nearest_hostile": {"id": 129949, "name": "creeper", "dist": 10.5},
        })
        st.set_inventory({"items": [{"name": "stone_sword", "count": 1, "slot": 0}]})
        dec = planner.choose_proactive_survival(st)
        self.assertIsNotNone(dec)
        assert dec is not None
        self.assertIsNone(dec.skill)
        self.assertEqual(dec.source, "idle")
        self.assertIn("夜间残血无甲", dec.reason)

    def test_unshielded_close_creeper_flee_uses_instant_sprint_burst_before_move_to(self) -> None:
        # 复现 seg-07 t=337s：无盾直面 2.8m 贴脸苦力怕时，必须立即原生疾跑跳脱离起爆半径，而非直接慢速 move_to 被炸死
        blocks = {(x, 65, z): "grass_block" for x in range(-14, 15) for z in range(-14, 15)}

        class _CloseCreeperWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 66.0, 0.5], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=1.1,
                    nearest_hostile={"id": 129949, "name": "creeper", "dist": 2.8, "pos": [0.5, 66.0, 3.3]},
                )
                self.controls: list[dict] = []
                self.moves: int = 0

            def control(self, **kwargs):
                self.controls.append(dict(kwargs))
                self.st["pos"] = [0.5, 66.0, -7.5]
                self.st["nearest_hostile"] = {"id": 129949, "name": "creeper", "dist": 10.8, "pos": [0.5, 66.0, 3.3]}
                return {"pos": list(self.st["pos"]), "moved": 8.0}

            def move_to(self, *args, **kwargs):
                self.moves += 1
                return super().move_to(*args, **kwargs)

        w = _CloseCreeperWorld()
        ex, state = w.session()
        drive(EmergencyEscape(intent="flee"), ex, state)
        self.assertGreaterEqual(len(w.controls), 1, "expected immediate sprint-jump burst for unshielded <=3.5m creeper")
        self.assertTrue(w.controls[0].get("sprint") and w.controls[0].get("jump"))
        self.assertEqual(w.moves, 0, "should clear creeper blast radius via fast control burst without slow move_to")

    def test_survey_island_yields_immediately_on_close_creeper_instead_of_post_scan(self) -> None:
        # 复现 seg-07 t=334..337s：survey_sector move_to 结束后若苦力怕已迫近 <7m，立即让位脱险而非继续扫 2.4s 方块
        from mcagent.runtime import Replan
        from mcagent.skills.survey import SurveyIsland

        blocks = {(x, 65, z): "grass_block" for x in range(-20, 21) for z in range(-20, 21)}

        class _CreeperSurveyWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 66.0, 0.5], items={"stone_pickaxe": 1})
                self.st.update(dimension="overworld", ready=True, alive=True, health=10.0, biome="forest")
                self.find_blocks_after_move = 0
                self.moved_once = False

            def find_blocks(self, *args, **kwargs):
                if self.moved_once:
                    self.find_blocks_after_move += 1
                return {"blocks": []}

            def move_to(self, *args, **kwargs):
                self.moved_once = True
                self.st["pos"] = [8.5, 66.0, 0.5]
                self.st["nearest_hostile"] = {"id": 129949, "name": "creeper", "dist": 5.4, "pos": [13.9, 66.0, 0.5]}
                return {"pos": list(self.st["pos"])}

        w = _CreeperSurveyWorld()
        ex, state = w.session()
        with self.assertRaisesRegex(Replan, "让位脱险"):
            drive(SurveyIsland(dimension="overworld", sectors=2, radius=16), ex, state)
        self.assertEqual(w.find_blocks_after_move, 0, "must abort post-move find_blocks scan when creeper is <7m")

    def test_main_loop_reconnects_after_3_unreleased_busy_stops(self) -> None:
        # 复现 seg-08：服务端出现挂死的 busy:dig 锁（/stop 持续返回 released=False），
        # MainLoop 连续 3 次未释放后必须自动执行 /disconnect + /connect + wait_ready 自愈，而非整场僵死
        from mcagent.client import ApiError
        from mcagent.loop.engine import LoopConfig, MainLoop
        from mcagent.loop.policy import PlanDecision
        from mcagent.skills.base import Skill

        class _AlwaysBusySkill(Skill):
            name = "busy_probe"
            description = "probe"
            goal = "probe:busy"
            cooldown_s = 0.0
            verified = "offline"

            def run(self, ex, state):
                if False:
                    yield {}
                raise ApiError("busy", "Another action (dig) is currently executing", {"busy": "dig"})

        class _StuckBusyClient:
            def __init__(self):
                self.disconnects = 0
                self.connects = 0
                self.stops = 0
                self.st = {"ready": True, "alive": True, "health": 20.0, "food": 20, "dimension": "overworld", "pos": [0.5, 64.0, 0.5]}

            def status(self):
                return dict(self.st)

            def inventory(self):
                return {"items": [], "empty_slots": 36}

            def events(self, since=0, limit=200):
                return {"events": [], "last_seq": since}

            def stop(self):
                self.stops += 1
                return {"stopped": True, "action": "dig", "released": False, "wait_ms": 5000}

            def disconnect(self):
                self.disconnects += 1
                return {"ok": True}

            def connect(self, timeout=20):
                self.connects += 1
                return {"ok": True}

            def wait_ready(self, timeout=60):
                return dict(self.st)

        class _FixedPlanner:
            mode = "campaign"
            def __init__(self):
                self.calls = 0
            def select(self, state, recent_failures=None):
                self.calls += 1
                if self.calls <= 3:
                    return PlanDecision(_AlwaysBusySkill(), "rule", "probe")
                return PlanDecision(None, "idle", "done", stop_episode=True)

        client = _StuckBusyClient()
        state = State()
        ex = Executor(client, state, log=lambda *_: None)
        loop = MainLoop(
            client, state, ex, _FixedPlanner(),
            config=LoopConfig(max_steps=10, max_seconds=30, enable_roadside_scan=False, idle_sleep_s=0.0),
            log=lambda *_: None,
            sleep=lambda *_: None,
        )
        loop.run()
        self.assertGreaterEqual(client.stops, 3)
        self.assertEqual(client.disconnects, 1, "expected /disconnect self-heal after 3 unreleased busy stops")
        self.assertEqual(client.connects, 1, "expected /connect self-heal after /disconnect")

    def test_combat_eval_never_counterattacks_in_water_or_with_outer_creeper(self) -> None:
        # 复现 seg-09 Death #1：水中游泳或 16m 外圈存在苦力怕且无盾时，严禁反杀近身僵尸，必须先脱险
        from mcagent.skills.combat_eval import decide_combat_or_flee

        class _WaterCreeperWorld(GridWorld):
            def __init__(self, *, in_water: bool, outer_creeper: bool):
                blocks = {(x, 61, z): "water" if in_water else "stone" for x in range(-4, 5) for z in range(-4, 5)}
                super().__init__(blocks=blocks, pos=[0.5, 62.0, 0.5], items={"stone_sword": 1})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=20.0, food=20,
                    in_water=in_water, submerged=False,
                    nearest_hostile={"id": 132688, "name": "zombie", "dist": 3.2, "pos": [3.7, 62.0, 0.5]},
                )
                self._outer_creeper = outer_creeper

            def get_entities(self, radius=16, types=None):
                ents = [{"id": 132688, "type": "hostile", "name": "zombie", "dist": 3.2, "pos": [3.7, 62.0, 0.5], "health": 20}]
                if self._outer_creeper:
                    ents.append({"id": 132662, "type": "hostile", "name": "creeper", "dist": 13.1, "pos": [0.5, 62.0, 13.6], "health": 20})
                return {"entities": ents}

        w1 = _WaterCreeperWorld(in_water=True, outer_creeper=False)
        ex1, st1 = w1.session()
        self.assertEqual(decide_combat_or_flee(ex1, st1).action, "flee")

        w2 = _WaterCreeperWorld(in_water=False, outer_creeper=True)
        ex2, st2 = w2.session()
        self.assertEqual(decide_combat_or_flee(ex2, st2).action, "flee")

    def test_fast_pursuer_flee_rotates_away_from_repeated_blocked_yaw(self) -> None:
        # 复现 seg-09 Death #2（t=110..114s）：蜘蛛贴脸连跑时，受阻角度写入 tried_yaws，绝不可连续 3 次撞同一个 yaw=0.0
        from mcagent.skills.escape import _flee_hostile, _yaw_diff

        blocks = {(x, 68, z): "stone" for x in range(-12, 13) for z in range(-12, 13)}

        class _SpiderBlockedYawWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 69.0, 0.5], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=17.3,
                    nearest_hostile={"id": 132686, "name": "spider", "dist": 1.1, "pos": [1.6, 69.0, 0.5]},
                )
                self.yaws: list[float] = []

            def control(self, **kwargs):
                yaw = float(kwargs.get("yaw", 0.0))
                self.yaws.append(yaw)
                # 前两次方向被树干卡死（0 位移），第 3 次起不同方向成功甩脱
                if len(self.yaws) <= 2:
                    return {"pos": list(self.st["pos"]), "moved": 0.0}
                self.st["pos"] = [0.5, 69.0, 11.5]
                self.st["nearest_hostile"] = {"id": 132686, "name": "spider", "dist": 11.5, "pos": [1.6, 69.0, 0.5]}
                return {"pos": list(self.st["pos"]), "moved": 11.0}

        w = _SpiderBlockedYawWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(ok)
        self.assertGreaterEqual(len(w.yaws), 3)
        # 每次撞墙后的下一次疾跑角度必须显著不同（>= 0.65 rad），禁止重复撞同一方向
        self.assertGreaterEqual(_yaw_diff(w.yaws[1], w.yaws[0]), 0.65, f"repeated blocked yaw: {w.yaws}")
        self.assertGreaterEqual(_yaw_diff(w.yaws[2], w.yaws[1]), 0.65, f"repeated blocked yaw: {w.yaws}")

    def test_nighttime_naked_respawn_guard_holds_even_with_hostile_at_11m(self) -> None:
        # 复现 seg-09 t=60.9s：夜间空手无甲重生时敌怪游荡至 11m（safe_12=False），仍必须驻留待天亮而非裸奔砍树
        planner = RulePlanner(min_verified="offline")
        st = State()
        st.set_status({
            "ready": True, "alive": True, "health": 20.0, "food": 20, "armor_points": 0,
            "dimension": "overworld", "is_day": False, "time_of_day": 20477,
            "pos": [-63.5, 69.0, 16.5],
            "nearest_hostile": {"id": 132688, "name": "zombie", "dist": 11.0},
        })
        st.set_inventory({"items": []})
        dec = planner.choose_proactive_survival(st)
        self.assertIsNotNone(dec)
        assert dec is not None
        self.assertIsNone(dec.skill)
        self.assertEqual(dec.source, "idle")
        self.assertIn("夜间空手无甲", dec.reason)

    def test_smelt_iron_filters_deep_cave_ores_when_unarmored_at_10_3_hp_no_food(self) -> None:
        # 复现 seg-10 t=62s：hp=10.3 无甲无盾无粮时，禁止再追 Y=48 远距深洞暴露矿，优先转安全矿井或地表浅矿
        from mcagent.skills.iron import SmeltIron, reset_iron_memo

        reset_iron_memo()
        deep_cave_ore = [-77, 48, 36]
        shallow_ore = [-60, 62, 26]

        class _MidHpCaveWorld(GridWorld):
            def find_blocks(self, types=None, radius=32, limit=64, **_):
                if types and any("iron" in t for t in types):
                    return {
                        "blocks": [
                            {"name": "iron_ore", "pos": list(deep_cave_ore)},
                            {"name": "iron_ore", "pos": list(shallow_ore)},
                        ]
                    }
                return {"blocks": []}

        w = _MidHpCaveWorld(
            blocks={(-64, 53, 26): "stone", tuple(deep_cave_ore): "iron_ore", tuple(shallow_ore): "iron_ore"},
            pos=[-63.7, 54.0, 26.3],
            items={"stone_pickaxe": 1, "coal": 4},
        )
        w.st.update(dimension="overworld", ready=True, alive=True, health=10.3, armor_points=0)
        ex, state = w.session()
        cands = SmeltIron()._ore_candidates(ex, state, radius=32)
        self.assertNotIn(deep_cave_ore, cands)
        self.assertIn(shallow_ore, cands)

    def test_flee_runs_second_sprint_when_zombie_still_within_3_5m_after_first_burst(self) -> None:
        # 复现 seg-10 t=149..152s：首段疾跑移出 2.3m（未撞墙），但僵尸紧追仍距 0.8m（<3.5m）时，
        # 必须以最新坐标重算向量并补二次疾跑拉脱，严禁直接落入慢速 move_to 绕回僵尸嘴边被咬死
        from mcagent.skills.escape import _flee_hostile

        blocks = {(x, 62, z): "stone" for x in range(-14, 15) for z in range(-14, 15)}

        class _PersistentZombieWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 63.0, 0.5], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=7.3,
                    nearest_hostile={"id": 134975, "name": "zombie", "dist": 1.6, "pos": [0.5, 63.0, -1.1]},
                )
                self.controls = 0
                self.moves = 0

            def control(self, **kwargs):
                self.controls += 1
                if self.controls == 1:
                    # 首段位移 2.3m（>=1.5m 未撞墙），但僵尸追至 0.8m
                    self.st["pos"] = [0.5, 63.0, 2.8]
                    self.st["nearest_hostile"] = {"id": 134975, "name": "zombie", "dist": 0.8, "pos": [0.5, 63.0, 2.0]}
                else:
                    # 二次疾跑拉脱至安全距离 10.0m
                    self.st["pos"] = [0.5, 63.0, 12.0]
                    self.st["nearest_hostile"] = {"id": 134975, "name": "zombie", "dist": 10.0, "pos": [0.5, 63.0, 2.0]}
                return {"pos": list(self.st["pos"]), "moved": 2.3 if self.controls == 1 else 9.2}

            def move_to(self, *args, **kwargs):
                self.moves += 1
                return {"pos": list(self.st["pos"])}

        w = _PersistentZombieWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(ok)
        self.assertGreaterEqual(w.controls, 2, "expected second sprint burst while zombie is still <3.5m")
        self.assertEqual(w.moves, 0, "should break melee range via second burst without slow move_to")

    def test_low_hp_pursuing_zombie_prioritizes_flee_above_stationary_eat_food(self) -> None:
        # 复现 seg-11 t=637s（INC-003）：hp=0.9 无甲且僵尸距 5.5m 正在追击时，
        # 必须优先 emergency_escape(flee) 拉开安全距，严禁原地站桩 1.6s 吃肉被追上秒杀
        planner = RulePlanner(min_verified="offline")
        st = State()
        st.set_status({
            "ready": True, "alive": True, "health": 0.9, "food": 16, "armor_points": 0,
            "dimension": "overworld", "is_day": True, "pos": [-40.3, 61.0, -290.1],
            "nearest_hostile": {"id": 139004, "name": "zombie", "dist": 5.5, "pos": [-45.8, 61.0, -290.1]},
        })
        st.set_inventory({"items": [{"name": "mutton", "count": 2, "slot": 0}]})
        dec = planner.choose(st)
        self.assertIsNotNone(dec.skill)
        assert dec.skill is not None
        self.assertEqual(dec.skill.name, "emergency_escape")
        self.assertEqual(dec.skill.args.get("intent"), "flee")

        # 拉开至 12m 安全距离后，才停步进食回血
        st.set_status({
            "ready": True, "alive": True, "health": 0.9, "food": 16, "armor_points": 0,
            "dimension": "overworld", "is_day": True, "pos": [-28.3, 61.0, -290.1],
            "nearest_hostile": {"id": 139004, "name": "zombie", "dist": 12.0, "pos": [-40.3, 61.0, -290.1]},
        })
        dec_safe = planner.choose(st)
        self.assertIsNotNone(dec_safe.skill)
        assert dec_safe.skill is not None
        self.assertEqual(dec_safe.skill.name, "eat_food")

    def test_dry_land_flee_avoids_jumping_into_river(self) -> None:
        # 复现 seg-12 t=147s（INC-001）：角色站在河岸干地 [-21.5, 63, -83.5] 遇到河对岸方向来的苦力怕时，
        # 正背后是河道深水；_resolve_flee_vector(avoid_water=True) 必须沿干地河岸转向逃离，严禁盲目跳进河里
        from mcagent.skills.voxel_cache import voxel_cache_for

        class _RiverbankWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks={}, pos=[-21.5, 63.0, -83.5], items={})
                self.st.update(
                    dimension="overworld",
                    ready=True,
                    alive=True,
                    health=18.3,
                    food=17,
                    in_water=False,
                    on_ground=True,
                    is_day=False,
                    nearest_hostile={"id": 140071, "name": "creeper", "dist": 8.9, "pos": [-21.5, 63.0, -92.4]},
                )

            def get_block(self, pos, use_cache: bool = True):
                x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
                # z >= -81 为深河（y<=62 为 water），z <= -82 为草地干岸（y<=62 为 grass_block）
                if z >= -81:
                    return {"name": "water"} if y <= 62 else {"name": "air"}
                return {"name": "grass_block"} if y <= 62 else {"name": "air"}

        w = _RiverbankWorld()
        ex, state = w.session()
        cache = voxel_cache_for(state)
        ux, uz, _ = _resolve_flee_vector(
            ex, -21.5, 63.0, -83.5, w.st["nearest_hostile"], cache, "overworld",
            state=state, avoid_water=True, in_water=False,
        )
        # 正背敌方向为 +z（跳进河里）；修正后必须沿干岸（±x，且 uz <= 0.35）逃离而非冲入河道
        self.assertLessEqual(uz, 0.35, f"flee vector ({ux:.2f}, {uz:.2f}) plunged directly into river (+z)")
        self.assertGreaterEqual(abs(ux), 0.7, f"expected lateral riverbank escape, got ({ux:.2f}, {uz:.2f})")

    def test_in_water_flee_uses_sprint_swim_not_5s_blocking_move_to(self) -> None:
        # 复现 seg-12 t=172..182s（INC-001）：角色在河中被苦力怕+三叉戟溺尸追杀时，
        # _flee_hostile 必须使用短步疾跑游泳 control(ms=800) 游离/上岸，严禁发起 5s 阻塞跨河 move_to 卡死原地吃三叉戟
        from mcagent.skills.escape import _flee_hostile

        class _RiverTridentWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks={}, pos=[-15.1, 62.3, -78.7], items={})
                self.st.update(
                    dimension="overworld",
                    ready=True,
                    alive=True,
                    health=10.3,
                    food=17,
                    in_water=True,
                    submerged=False,
                    oxygen=20,
                    is_day=False,
                    nearest_hostile={"id": 140071, "name": "creeper", "dist": 8.1, "pos": [-23.2, 62.3, -78.7]},
                )
                self.controls: list[dict] = []
                self.moves: list[dict] = []

            def get_entities(self, radius: int = 16, types=None):
                return {
                    "entities": [
                        dict(self.st["nearest_hostile"]),
                        {"id": 140183, "name": "drowned", "dist": 9.5, "pos": [-19.0, 62.0, -84.0]},
                    ]
                }

            def control(self, **kwargs):
                self.controls.append(dict(kwargs))
                yaw = float(kwargs.get("yaw", 0.0))
                dx, dz = -math.sin(yaw) * 2.0, -math.cos(yaw) * 2.0
                self.st["pos"] = [self.st["pos"][0] + dx, 62.4, self.st["pos"][2] + dz]
                # 游 2 步后拉远至安全距离
                new_dist = math.hypot(self.st["pos"][0] - (-23.2), self.st["pos"][2] - (-78.7))
                self.st["nearest_hostile"] = {"id": 140071, "name": "creeper", "dist": round(new_dist, 1), "pos": [-23.2, 62.3, -78.7]}
                return {"pos": list(self.st["pos"]), "moved": 2.0}

            def move_to(self, *args, **kwargs):
                self.moves.append(dict(kwargs))
                return {"pos": list(self.st["pos"])}

        w = _RiverTridentWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(ok)
        self.assertGreaterEqual(len(w.controls), 1, "expected fast sprint-swim control bursts in water")
        self.assertEqual(len(w.moves), 0, f"must not stall on 5s open-water move_to while fleeing in water: {w.moves}")

    def test_in_water_night_triggers_proactive_surface_before_llm_idle_sink(self) -> None:
        # 复现 seg-12 t=122..130s 与 t=152..160s：夜间刚结束 flee 仍浮在水中（oxy=20）时，
        # _extract_escape_intent 必须立刻触发 surface 登岸，避免进 LLM 规划 7-8 秒下沉到河底氧量见底；
        # 白天无怪且氧量充足时仍不误触发。
        from mcagent.loop.policy import _extract_escape_intent

        st_night = State()
        st_night.set_status({
            "ready": True, "alive": True, "health": 18.3, "food": 17, "oxygen": 20,
            "in_water": True, "submerged": False, "is_day": False, "time_of_day": 15000,
            "pos": [-15.7, 62.5, -72.2],
        })
        intent_night = _extract_escape_intent(st_night)
        self.assertIsNotNone(intent_night)
        assert intent_night is not None
        self.assertEqual(intent_night[0], "surface")

        st_day = State()
        st_day.set_status({
            "ready": True, "alive": True, "health": 20.0, "food": 20, "oxygen": 20,
            "in_water": True, "submerged": False, "is_day": True, "time_of_day": 1000,
            "pos": [-15.7, 62.5, -72.2],
        })
        self.assertIsNone(_extract_escape_intent(st_day))

    def test_trident_hurt_from_drowned_triggers_safety_flee_hazard(self) -> None:
        # 复现 seg-12 t=176.6s/180.5s：溺尸投掷三叉戟（source="trident"，单次 8 伤）命中无甲角色时，
        # 即使最近敌怪超出近战距，Safety.hazards 必须立即发出 hurt_triggered flee（残血时 urgent）。
        from mcagent.runtime import Safety

        st = State()
        st.set_status({
            "ready": True, "alive": True, "health": 2.3, "food": 17, "armor_points": 0,
            "in_water": True, "oxygen": 20, "pos": [-9.5, 62.2, -82.5],
        })
        st.set_inventory({"items": []})
        st.last_hurt_at = time.monotonic()
        st.last_hurt_info = {
            "source": "trident",
            "damage": 8,
            "source_entity": {"id": 140183, "name": "drowned"},
        }
        hazards = Safety().hazards(st)
        flee_h = [h for h in hazards if h.get("kind") == "flee"]
        self.assertTrue(flee_h, f"expected hurt-triggered flee for trident hit, got {hazards}")
        self.assertEqual(flee_h[0].get("severity"), "urgent")
        self.assertTrue(flee_h[0].get("hurt_triggered"))

    def test_seg13_pillager_at_3_5m_after_first_burst_runs_followup_sprint_not_slow_move_to(self) -> None:
        # 复现 seg-13 Death #2（t=3318..3323s）：首段 <2.5m 疾跑躲开僵尸后，掠夺者处于 dist=3.5m（此前 <3.5 严格不等漏判），
        # 必须立即连补原生疾跑跳拉开安全距，严禁直接落入慢速 move_to 停滞 4.7s 被掠夺者+僵尸围杀
        from mcagent.skills.escape import _flee_hostile

        blocks = {(x, 64, z): "grass_block" for x in range(-55, -25) for z in range(-55, -25)}

        class _PillagerZombieWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-34.4, 65.0, -41.7], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=12.4, food=17, armor_points=0,
                    nearest_hostile={"id": 150485, "name": "zombie", "dist": 1.6, "pos": [-33.0, 65.0, -42.5]},
                )
                self.controls = 0
                self.moves = 0

            def control(self, **kwargs):
                self.controls += 1
                if self.controls == 1:
                    self.st["pos"] = [-38.7, 65.0, -39.3]
                    # 恰好 dist=3.5m（此前 <3.5 漏判点）
                    self.st["nearest_hostile"] = {"id": 150656, "name": "pillager", "dist": 3.5, "pos": [-35.2, 65.0, -39.3]}
                    return {"pos": list(self.st["pos"]), "moved": 5.1}
                self.st["pos"] = [-46.0, 65.0, -35.0]
                self.st["nearest_hostile"] = {"id": 150656, "name": "pillager", "dist": 11.6, "pos": [-35.2, 65.0, -39.3]}
                return {"pos": list(self.st["pos"]), "moved": 8.5}

            def move_to(self, *args, **kwargs):
                self.moves += 1
                return {"pos": list(self.st["pos"])}

        w = _PillagerZombieWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(ok)
        self.assertGreaterEqual(w.controls, 2, "expected follow-up sprint burst when pillager is at 3.5m")
        self.assertEqual(w.moves, 0, "must escape via follow-up sprint burst without dropping into stalling move_to")

    def test_seg13_cave_blocked_cover_rotates_yaw_and_caps_move_to_timeout(self) -> None:
        # 复现 seg-13 Death #1（t=3220..3226s）：矿洞掩体后撤撞墙 moved=0 时，必须记录受阻角并换向带跳疾跑，
        # 且即使回落至 move_to，其超时上限也必须收紧至 <=2.5s（杜绝 5.0s 站桩连吃两箭）
        from mcagent.skills.escape import _flee_hostile

        blocks: dict[tuple[int, int, int], str] = {}
        for x in range(-125, -108):
            for z in range(18, 30):
                blocks[(x, 44, z)] = "stone"
        # 玩家在 (-117, 45, 23)，骷髅在 (-122, 45, 23)（距离 5.0m），中间 (-119, 45..46, 23) 为挡箭墙
        blocks[(-119, 45, 23)] = "cobblestone"
        blocks[(-119, 46, 23)] = "cobblestone"

        class _CaveSkeletonWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-116.3, 45.0, 23.3], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=10.3, food=17, armor_points=0,
                    nearest_hostile={"id": 150387, "name": "skeleton", "dist": 4.8, "pos": [-121.1, 45.0, 23.3]},
                )
                self.control_calls: list[dict] = []
                self.move_timeouts: list[float] = []

            def control(self, **kwargs):
                self.control_calls.append(dict(kwargs))
                if len(self.control_calls) == 1:
                    # 第一次 cover 后撤撞墙原地 0 位移
                    return {"pos": list(self.st["pos"]), "moved": 0.0}
                # 第二次换向疾跑成功拉开距离
                self.st["pos"] = [-116.3, 45.0, 29.0]
                self.st["nearest_hostile"] = {"id": 150387, "name": "skeleton", "dist": 10.5, "pos": [-121.1, 45.0, 23.3]}
                return {"pos": list(self.st["pos"]), "moved": 5.7}

            def move_to(self, *args, **kwargs):
                self.move_timeouts.append(float(kwargs.get("timeout", 99.0)))
                return {"pos": list(self.st["pos"])}

        w = _CaveSkeletonWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(ok)
        self.assertGreaterEqual(len(w.control_calls), 2, "expected alternate-yaw sprint burst after cover stall")
        self.assertTrue(w.control_calls[0].get("jump"), "cover retreat burst must include jump=True for 1-block steps")
        self.assertEqual(len(w.move_timeouts), 0, "alternate-yaw sprint should escape without stalling in move_to")

    def test_seg13_flee_fallback_move_to_timeout_is_capped_at_2_2s(self) -> None:
        # 验证 _flee_hostile 兜底 move_to 超时上限压紧至 <=2.5s（2.2s），杜绝 5.0s 原地卡滞
        from mcagent.skills.escape import _flee_hostile

        blocks = {(x, 64, z): "grass_block" for x in range(-20, 20) for z in range(-20, 20)}

        class _MidDistZombieWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[0.5, 65.0, 0.5], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=14.0, food=18, armor_points=0,
                    nearest_hostile={"id": 999, "name": "zombie", "dist": 5.8, "pos": [-5.3, 65.0, 0.5]},
                )
                self.timeouts: list[float] = []

            def move_to(self, *args, **kwargs):
                self.timeouts.append(float(kwargs.get("timeout", 99.0)))
                self.st["pos"] = [10.5, 65.0, 0.5]
                self.st["nearest_hostile"] = {"id": 999, "name": "zombie", "dist": 15.8, "pos": [-5.3, 65.0, 0.5]}
                return {"pos": list(self.st["pos"])}

        w = _MidDistZombieWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        try:
            while True:
                next(gen)
        except StopIteration:
            pass
        self.assertTrue(w.timeouts, "expected fallback move_to to run for mid-distance zombie")
        self.assertLessEqual(max(w.timeouts), 2.5, f"flee move_to timeout must be <=2.5s, got {w.timeouts}")

    def test_seg14_void_risk_safe_ground_uses_rng_0_so_bot_steps_off_hole(self) -> None:
        # 复现 seg-14 INC-002（t=391.3s）：hp=0.7 悬空在 4 格坑口 [-72.4, 67, -34.5] 时，
        # 无敌怪 void_risk 脱险必须用 rng=0 真正踏上相邻实心格（-73, 67, -36），杜绝 rng=1 30ms 误报到达坠死
        from mcagent.skills.escape import _flee_hostile

        blocks = {
            (-73, 66, -36): "grass_block",
            (-73, 62, -35): "grass_block",  # 4 格坑底
        }

        class _HoleEdgeWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-72.4, 67.0, -34.5], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=0.7, food=16,
                    on_ground=False, nearest_hostile=None,
                )
                self.move_calls: list[dict] = []

            def move_to(self, *args, **kwargs):
                self.move_calls.append(dict(kwargs))
                self.st["pos"] = [-72.5, 67.0, -35.5]
                self.st["on_ground"] = True
                return {"pos": list(self.st["pos"])}

        w = _HoleEdgeWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(ok)
        self.assertEqual(len(w.move_calls), 1)
        self.assertEqual(w.move_calls[0].get("rng"), 0, "void_risk safe_ground recovery must use rng=0 to step onto solid block")

    def test_seg14_survey_has_active_threat_ignores_passive_heal_without_hostiles(self) -> None:
        # 复现 seg-14 INC-001（survey_island REPLAN_OSCILLATION）：残血无怪挂 soft heal 意图时，
        # survey._has_active_threat 不应误判为近身敌怪威胁导致反复中断振荡；挂 flee 意图时仍必须立即让位
        from mcagent.skills.survey import _has_active_threat

        st_obj = State()
        st_snap = {"health": 0.7, "in_lava": False, "on_fire": False, "submerged": False, "nearest_hostile": None}
        st_obj.set_safety_intent({"kind": "heal", "severity": "soft", "why": "血量 0.7"})
        self.assertFalse(_has_active_threat(st_obj, st_snap))

        st_obj.set_safety_intent({"kind": "flee", "severity": "urgent", "why": "近身苦力怕"})
        self.assertTrue(_has_active_threat(st_obj, st_snap))

    def test_seg15_arrow_hurt_chains_second_sprint_burst_to_break_skeleton_16m_range(self) -> None:
        # 复现 seg-15 INC-001（t=45..78s）：空手无盾夜间被骷髅 155783 远射（source="arrow"），
        # move_to(2.2s) 超时后首段兜底疾跑仍停在 14.8m 空地（<18m 射程内），必须顺势补第二段 800ms 疾跑跳一次性拉断 16m 仇恨
        from mcagent.client import ApiError
        from mcagent.skills.escape import _flee_hostile

        blocks = {(x, 70, z): "grass_block" for x in range(-80, -30) for z in range(-5, 30)}

        class _HilltopSkeletonWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-64.0, 71.0, 12.5], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=16.0, food=20, armor_points=0,
                    is_day=False,
                    nearest_hostile={"id": 156003, "name": "zombie", "dist": 9.4, "pos": [-73.4, 71.0, 12.5]},
                )
                self.controls = 0
                self.move_targets: list[list[float]] = []

            def move_to(self, *args, **kwargs):
                pos = kwargs.get("pos") or (args[0] if args else None)
                if pos:
                    self.move_targets.append(list(pos))
                # 模拟 2.2s 超时前走了 7m
                self.st["pos"] = [-57.0, 71.0, 12.5]
                self.st["nearest_hostile"] = {"id": 155783, "name": "skeleton", "dist": 12.0, "pos": [-69.0, 71.0, 12.5]}
                raise ApiError("timeout", "timeout")

            def control(self, **kwargs):
                self.controls += 1
                if self.controls == 1:
                    self.st["pos"] = [-52.5, 71.0, 12.5]
                    self.st["nearest_hostile"] = {"id": 155783, "name": "skeleton", "dist": 16.5, "pos": [-69.0, 71.0, 12.5]}
                    return {"pos": list(self.st["pos"]), "moved": 4.5}
                self.st["pos"] = [-47.5, 71.0, 12.5]
                self.st["nearest_hostile"] = {"id": 155783, "name": "skeleton", "dist": 21.5, "pos": [-69.0, 71.0, 12.5]}
                return {"pos": list(self.st["pos"]), "moved": 5.0}

        w = _HilltopSkeletonWorld()
        ex, state = w.session()
        state.last_hurt_at = time.monotonic()
        state.last_hurt_info = {
            "source": "arrow",
            "damage": 4,
            "source_entity": {"id": 155783, "name": "skeleton"},
        }
        gen = _flee_hostile(ex, state)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(ok)
        self.assertGreaterEqual(w.controls, 2, "must chain second 800ms sprint burst while still inside 18m arrow range")
        # 确认无盾受箭时 flee_stride 已扩展为 18.0m（相对起点 -64.0 落点 ~ -46.0）
        self.assertTrue(w.move_targets)
        self.assertGreaterEqual(abs(w.move_targets[0][0] - (-64.0)), 17.5)

    def test_seg16_crit_hp_unarmored_expands_ranged_flee_dist_to_14m_and_blocks_cave_smelt(self) -> None:
        # 复现 seg-16 INC-003（t=1409.9s）：残血 hp=5.4 无甲无盾身处 Y=51 暗洞且骷髅距 10.3m（刚超旧 10.0m 阈值），
        # 1) effective_hostile_flee_dist 对残血无甲远程射手必须扩展至 14.0m 立即触发逃跑；
        # 2) SmeltIron.blocked 即使背包已有少量 raw_iron，在残血暗洞（hp<=6, Y<58）也必须阻断下洞熔炼/寻矿。
        from mcagent.runtime import effective_hostile_flee_dist
        from mcagent.skills.iron import SmeltIron

        st = State()
        st.set_status({
            "ready": True, "alive": True, "health": 5.4, "food": 20, "armor_points": 0,
            "dimension": "overworld", "pos": [-55.3, 51.0, -281.1],
            "nearest_hostile": {"id": 161524, "name": "skeleton", "dist": 10.3, "pos": [-65.0, 51.0, -278.0]},
        })
        st.set_inventory({"items": [
            {"name": "stone_pickaxe", "count": 1, "slot": 0},
            {"name": "raw_iron", "count": 2, "slot": 1},
            {"name": "coal", "count": 4, "slot": 2},
        ]})
        self.assertGreaterEqual(effective_hostile_flee_dist(st, "skeleton"), 14.0)
        self.assertTrue(bool(SmeltIron.blocked(st)), "SmeltIron must stay blocked at crit hp=5.4 inside Y=51 cave")

        # 回到地表安全高度（Y=68）且回血至 hp=12 后恢复放行
        st.set_status({
            "ready": True, "alive": True, "health": 12.0, "food": 20, "armor_points": 0,
            "dimension": "overworld", "pos": [-55.3, 68.0, -281.1],
        })
        self.assertEqual(SmeltIron.blocked(st), "")

    def test_seg17_in_water_bobbing_uses_horizontal_xz_displacement_and_rotates_to_open_water(self) -> None:
        # 复现 seg-17 Death #1 与 17 连卡死（[-87.7, 62.4, 13.3]）：水中撞岸壁上下扑腾 Δy=0.7 但水平 Δxz=0.0 时，
        # 必须按水平位移 Δxz<0.5 判停滞并记录 tried_yaws，转向开放水道游离，杜绝 17 轮重复同角撞墙
        from mcagent.skills.escape import _flee_hostile

        blocks: dict[tuple[int, int, int], str] = {}
        # 玩家在 (-88, 62, 13) 水中；西侧 (-89, 62..64, 13) 是 3 格高石壁；南/北 (-88, 62, 12/14) 是开放水道
        blocks[(-88, 62, 13)] = "water"
        blocks[(-88, 63, 13)] = "air"
        for z in (12, 14):
            blocks[(-88, 62, z)] = "water"
            blocks[(-88, 63, z)] = "air"
        for y in (61, 62, 63, 64):
            blocks[(-89, y, 13)] = "stone"
            blocks[(-87, y, 13)] = "stone"

        class _RiverCliffWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-87.7, 62.2, 13.3], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=18.0, food=20, armor_points=0,
                    in_water=True, submerged=False, on_ground=False,
                    nearest_hostile={"id": 163469, "name": "skeleton", "dist": 4.2, "pos": [-83.5, 62.2, 13.3]},
                )
                self.yaws: list[float] = []

            def control(self, **kwargs):
                yaw = float(kwargs.get("yaw", 0.0))
                self.yaws.append(yaw)
                if len(self.yaws) == 1:
                    # 首轮撞墙上下扑腾：Δy=+0.7m 但 Δxz=0.0m
                    self.st["pos"] = [-87.7, 62.9, 13.3]
                    return {"pos": list(self.st["pos"]), "moved": 0.7}
                # 换向沿开放水道游离成功
                self.st["pos"] = [-87.7, 62.3, 21.0]
                self.st["nearest_hostile"] = {"id": 163469, "name": "skeleton", "dist": 12.0, "pos": [-83.5, 62.2, 13.3]}
                return {"pos": list(self.st["pos"]), "moved": 7.7}

        w = _RiverCliffWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(ok)
        self.assertGreaterEqual(len(w.yaws), 2)
        diff = abs(math.atan2(math.sin(w.yaws[1] - w.yaws[0]), math.cos(w.yaws[1] - w.yaws[0])))
        self.assertGreaterEqual(diff, 0.65, f"must rotate yaw away from bobbing cliff wall, got yaws={w.yaws}")

    def test_seg17_water_pit_trap_digs_soft_bank_after_two_horizontal_stalls(self) -> None:
        # 复现 seg-17 Death #2（[-101.5, 61.3, 20.3]）：角色陷在四周 2 格高泥土壁水坑且背包无垫脚块时，
        # 连续 2 次水中水平受阻（Δxz<0.5）后必须破拆 1 格紧邻松软土岸开出登岸台阶，严禁原地扑腾至死
        from mcagent.skills.escape import _flee_hostile

        blocks: dict[tuple[int, int, int], str] = {
            (-102, 60, 20): "dirt",
            (-102, 61, 20): "water",
            (-102, 62, 20): "air",
            (-102, 63, 20): "air",
        }
        # 四周 2 格高（y=61, 62）全是草/土坑壁，上方 y=63 为空气
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            blocks[(-102 + dx, 61, 20 + dz)] = "dirt"
            blocks[(-102 + dx, 62, 20 + dz)] = "grass_block"
            blocks[(-102 + dx, 63, 20 + dz)] = "air"

        class _DirtWaterPitWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-101.5, 61.3, 20.5], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=17.0, food=20, armor_points=0,
                    in_water=True, submerged=False, on_ground=False,
                    nearest_hostile={"id": 164834, "name": "zombie", "dist": 3.5, "pos": [-98.0, 63.0, 20.5]},
                )
                self.dug: list[list[int]] = []

            def control(self, **kwargs):
                # 坑内四周无水平位移，仅垂直扑腾
                self.st["pos"] = [-101.5, 61.9, 20.5]
                return {"pos": list(self.st["pos"]), "moved": 0.6}

            def dig(self, pos, **kwargs):
                self.dug.append([int(pos[0]), int(pos[1]), int(pos[2])])
                self.blocks[(int(pos[0]), int(pos[1]), int(pos[2]))] = "air"
                return {"dug": "grass_block", "pos": list(pos)}

        w = _DirtWaterPitWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        try:
            while True:
                next(gen)
        except StopIteration:
            pass
        self.assertTrue(w.dug, "expected _flee_hostile to dig a soft bank block after 2 water-pit horizontal stalls")

    def test_seg18_crit_hp_unarmored_close_hostile_emits_urgent_flee(self) -> None:
        # 复现 seg-18 Death #1（t=1216.0s, [-40.5, 2, 77.4]）：残血 hp=2.7 无甲时僵尸距 2.3m 必须触发 urgent 抢占，
        # 绝不允许仅发 soft flee 放任 mine_diamond 继续挖块走入僵尸一击毙命
        from mcagent.runtime import Safety, State

        s = State()
        s.st = {
            "ready": True, "alive": True, "dimension": "overworld",
            "pos": [-40.5, 3.0, 78.5], "health": 2.7, "food": 13, "armor_points": 0,
            "nearest_hostile": {"id": 169414, "name": "zombie", "dist": 2.3},
        }
        hazards = Safety().hazards(s)
        flee_h = [h for h in hazards if h.get("kind") == "flee"]
        self.assertTrue(flee_h, f"expected flee hazard, got {hazards}")
        self.assertEqual(flee_h[0].get("severity"), "urgent")

    def test_seg18_mine_diamond_blocked_at_low_hp_unarmored_unshielded(self) -> None:
        # 复现 seg-18 Death #1：hp=2.7 无甲无盾且无钻石时，MineDiamond.blocked() 必须拒绝下探深矿
        from mcagent.runtime import State
        from mcagent.skills.diamond import MineDiamond

        s = State()
        s.st = {
            "ready": True, "alive": True, "dimension": "overworld",
            "pos": [-34.5, 38.0, 70.5], "health": 2.7, "food": 13, "armor_points": 0,
        }
        reason = MineDiamond.blocked(s)
        self.assertTrue(reason, "MineDiamond.blocked() must refuse deep mining at hp=2.7 unarmored/unshielded")

    def test_seg18_airborne_cliff_over_water_rejects_plunge_and_avoids_water(self) -> None:
        # 复现 seg-18 Death #2（t=1292.26s, [-33.7, 68.2, -17]）：起跳滞空于 9 格深水峡谷崖边（实心地形在 y=65、水在 y=62）时，
        # _is_sprint_yaw_safe 绝不可把真实滞空误判为空壳单测世界，且干地逃敌须避开水体与深崖
        from mcagent.skills.escape import _is_sprint_yaw_safe, _pick_cliff_safe_yaw
        from mcagent.skills.voxel_cache import voxel_cache_for

        blocks: dict[tuple[int, int, int], str] = {}
        # 玩家在 (-34, 68, -17) 滞空：-z 方向是 9 格深峡谷（y=62 为 water，上方全 air），+z 与 ±x 方向 y=65 为 sand 干地
        for x in range(-42, -26):
            for z in range(-25, -10):
                for y in range(60, 71):
                    blocks[(x, y, z)] = "air"
                if z <= -18:
                    blocks[(x, 62, z)] = "water"
                    blocks[(x, 61, z)] = "water"
                    blocks[(x, 60, z)] = "sand"
                else:
                    blocks[(x, 65, z)] = "sand"
                    blocks[(x, 64, z)] = "sandstone"

        w = GridWorld(blocks=blocks, pos=[-33.7, 68.2, -17.0])
        w.st.update(dimension="overworld", on_ground=False, in_water=False, submerged=False)
        ex, state = w.session()
        cache = voxel_cache_for(state)
        # 朝 -z 冲入深水峡谷（yaw = 0.0 -> dx=0, dz=-1）必须被判不安全
        self.assertFalse(_is_sprint_yaw_safe(ex, -33.7, 68.2, -17.0, 0.0, cache, "overworld", avoid_water=True))
        # 改选方向应避开 -z 峡谷并落向 +z 实心干地
        safe_yaw = _pick_cliff_safe_yaw(ex, -33.7, 68.2, -17.0, 0.0, cache, "overworld", avoid_water=True)
        self.assertIsNotNone(safe_yaw)
        assert safe_yaw is not None
        self.assertGreater(-math.cos(safe_yaw), -0.3, f"must not steer toward -z cliff plunge, got yaw={safe_yaw}")

    def test_seg18_escape_water_under_skeleton_fire_caps_timeout_and_hands_off(self) -> None:
        # 复现 seg-18 Death #2（t=1299s）：水下遭骷髅 5.9m 射击时 escape_water_to_shore 严禁发起 15s 长 move_to 站桩吃箭，
        # 超时必须压紧至 <=2.2s
        from mcagent.skills.escape import escape_water_to_shore

        blocks: dict[tuple[int, int, int], str] = {}
        for x in range(-38, -30):
            for z in range(-25, -17):
                blocks[(x, 60, z)] = "dirt"
                blocks[(x, 61, z)] = "water"
                blocks[(x, 62, z)] = "water"
                blocks[(x, 63, z)] = "air"
        # 西侧有干岸 (-36, 63, -21)
        blocks[(-36, 62, -21)] = "grass_block"
        blocks[(-36, 63, -21)] = "air"
        blocks[(-36, 64, -21)] = "air"

        timeouts: list[float] = []

        class _WaterFireWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-33.4, 61.0, -21.0])
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=10.3, food=17,
                    in_water=True, submerged=True, on_ground=False,
                    nearest_hostile={"id": 169608, "name": "skeleton", "dist": 5.9, "pos": [-33.0, 63.0, -15.5]},
                )

            def move_to(self, x=None, z=None, *, y=None, entity_id=None, rng=1, pos=None, timeout=None, **kwargs):
                if timeout is not None:
                    timeouts.append(float(timeout))
                target = pos if pos is not None else [x, y if y is not None else self.st["pos"][1], z]
                self.st["pos"] = [float(target[0]), float(target[1]), float(target[2])]
                self.st["in_water"] = False
                self.st["submerged"] = False
                self.st["on_ground"] = True
                return {"pos": list(self.st["pos"])}

        w = _WaterFireWorld()
        ex, state = w.session()
        gen = escape_water_to_shore(ex, state, max_steps=3)
        try:
            while True:
                next(gen)
        except StopIteration:
            pass
        self.assertTrue(timeouts, "expected shore move_to call")
        self.assertLessEqual(max(timeouts), 2.5, f"expected hostile-aware water shore timeout <=2.5s, got {timeouts}")

    def test_seg19_invisible_border_zero_stall_pivots_to_opposite_hemisphere(self) -> None:
        # 复现 seg-19 Death #1（t=640.9s, [-151.9, 74, 32.3]）：Z=32.0 存在服务端隐形边界（方块视角全空气），
        # 朝 -Z 冲刺发生服务端 forcedMove 回弹（moved=0）时，必须记录受阻角并切入 +Z 反半球不跳脱墙
        from mcagent.skills.escape import _flee_hostile

        blocks: dict[tuple[int, int, int], str] = {}
        for x in range(-160, -144):
            for z in range(24, 42):
                blocks[(x, 73, z)] = "grass_block"
                blocks[(x, 74, z)] = "air"
                blocks[(x, 75, z)] = "air"

        class _InvisibleBorderWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-151.9, 74.0, 32.3])
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=5.0, food=15, armor_points=0,
                    on_ground=True, in_water=False, submerged=False,
                    nearest_hostile={"id": 172555, "name": "spider", "dist": 1.4, "pos": [-150.8, 74.0, 33.2]},
                )
                self.controls: list[dict] = []

            def get_entities(self, radius=18, types=None, **kwargs):
                return {"entities": [dict(self.st["nearest_hostile"])]}

            def control(self, **kwargs):
                yaw = float(kwargs.get("yaw", 0.0))
                self.controls.append(dict(kwargs))
                dx, dz = -math.sin(yaw), -math.cos(yaw)
                # 凡推向 Z <= 32.0（dz <= 0.1）均触发服务端隐形边界 forcedMove 回弹，moved=0
                if dz <= 0.1:
                    return {"pos": list(self.st["pos"]), "moved": 0.0, "on_ground": False}
                # 切入 +Z 半球后脱离边界并甩开蜘蛛
                nx = self.st["pos"][0] + dx * 6.0
                nz = self.st["pos"][2] + dz * 6.0
                self.st["pos"] = [nx, 74.0, nz]
                d_sp = math.hypot(nx - (-150.8), nz - 33.2)
                self.st["nearest_hostile"] = {"id": 172555, "name": "spider", "dist": round(d_sp, 2), "pos": [-150.8, 74.0, 33.2]}
                return {"pos": list(self.st["pos"]), "moved": 6.0, "on_ground": True}

        w = _InvisibleBorderWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(ok, f"expected escape past Z=32.0 invisible border, controls={w.controls}")
        self.assertGreater(w.st["pos"][2], 34.0, f"expected bot to move into +Z hemisphere, pos={w.st['pos']}")
        self.assertTrue(
            any(c.get("jump") is False for c in w.controls[1:]),
            f"expected non-jumping sprint pivot after zero-stall rubberband, controls={w.controls}",
        )

    def test_seg19_blocked_yaw_memory_persists_across_escape_calls_at_same_pos(self) -> None:
        # 复现 seg-19 跨轮受阻角记忆：同位置记录朝 -Z（yaw=0.0）受阻后，下一轮 _flee_hostile 必须避开 -Z 不再反复撞墙
        from mcagent.skills.escape import _flee_hostile, _record_blocked_yaw

        blocks: dict[tuple[int, int, int], str] = {}
        for x in range(-160, -144):
            for z in range(24, 42):
                blocks[(x, 73, z)] = "grass_block"
                blocks[(x, 74, z)] = "air"
                blocks[(x, 75, z)] = "air"

        class _MemWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-151.9, 74.0, 32.3])
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=5.0, food=15, armor_points=0,
                    on_ground=True, in_water=False, submerged=False,
                    nearest_hostile={"id": 172555, "name": "spider", "dist": 1.4, "pos": [-151.9, 74.0, 33.7]},
                )
                self.yaws: list[float] = []

            def get_entities(self, radius=18, types=None, **kwargs):
                return {"entities": [dict(self.st["nearest_hostile"])]}

            def control(self, **kwargs):
                yaw = float(kwargs.get("yaw", 0.0))
                self.yaws.append(yaw)
                dx, dz = -math.sin(yaw), -math.cos(yaw)
                self.st["pos"] = [self.st["pos"][0] + dx * 6.0, 74.0, self.st["pos"][2] + dz * 6.0]
                self.st["nearest_hostile"] = {"id": 172555, "name": "spider", "dist": 10.0, "pos": [-151.9, 74.0, 33.7]}
                return {"pos": list(self.st["pos"]), "moved": 6.0, "on_ground": True}

        w = _MemWorld()
        ex, state = w.session()
        # 模拟上一轮朝 -Z（yaw=0.0）撞隐形边界受阻
        _record_blocked_yaw(state, "overworld", -151.9, 32.3, 0.0)
        gen = _flee_hostile(ex, state)
        try:
            while True:
                next(gen)
        except StopIteration:
            pass
        self.assertTrue(w.yaws)
        first_diff = abs(math.atan2(math.sin(w.yaws[0] - 0.0), math.cos(w.yaws[0] - 0.0)))
        self.assertGreaterEqual(first_diff, 0.65, f"must not re-ram remembered blocked yaw=0.0, got {w.yaws}")

    def test_seg20_escape_water_digs_soft_bank_instead_of_15s_move_to_on_two_block_pit(self) -> None:
        # 复现 seg-20 t=282.9..328.9s（[-101.1, 62.5, 20.7]）：无怪时陷在 2 格高松软泥土/草水坑且无垫脚方块，
        # escape_water_to_shore 必须直接破拆 1 格紧邻松软土岸开台阶，绝不可干等 3×15s 长 move_to 超时招来僵尸偷袭
        from mcagent.skills.escape import escape_water_to_shore

        blocks: dict[tuple[int, int, int], str] = {
            (-102, 61, 20): "dirt",
            (-102, 62, 20): "water",
            (-102, 63, 20): "air",
            (-102, 64, 20): "air",
        }
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            blocks[(-102 + dx, 62, 20 + dz)] = "dirt"
            blocks[(-102 + dx, 63, 20 + dz)] = "grass_block"
            blocks[(-102 + dx, 64, 20 + dz)] = "air"

        class _TwoBlockPitWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-101.5, 62.3, 20.5], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=20.0, food=20,
                    in_water=True, submerged=False, on_ground=False, nearest_hostile=None,
                )
                self.dug: list[list[int]] = []
                self.move_timeouts: list[float] = []

            def dig(self, pos, **kwargs):
                self.dug.append([int(pos[0]), int(pos[1]), int(pos[2])])
                self.blocks[(int(pos[0]), int(pos[1]), int(pos[2]))] = "air"
                return {"dug": "grass_block", "pos": list(pos)}

            def move_to(self, x=None, z=None, *, y=None, entity_id=None, rng=1, pos=None, timeout=None, **kwargs):
                if timeout is not None:
                    self.move_timeouts.append(float(timeout))
                target = pos if pos is not None else [x, y if y is not None else self.st["pos"][1], z]
                self.st["pos"] = [float(target[0]), float(target[1]), float(target[2])]
                self.st["in_water"] = False
                self.st["on_ground"] = True
                return {"pos": list(self.st["pos"])}

        w = _TwoBlockPitWorld()
        ex, state = w.session()
        gen = escape_water_to_shore(ex, state, max_steps=4)
        try:
            while True:
                next(gen)
        except StopIteration:
            pass
        self.assertTrue(w.dug, "expected escape_water_to_shore to dig soft bank block on 2-block-high water pit")
        self.assertTrue(all(t <= 5.0 for t in w.move_timeouts), f"expected bounded shore timeout <=5.0s, got {w.move_timeouts}")

    def test_seg20_pick_cliff_safe_yaw_avoids_sprinting_180_into_close_zombie(self) -> None:
        # 复现 seg-20 Death #1（t=16.43s）：背敌方向（+z）遇深崖时，_pick_cliff_safe_yaw 必须改选侧向（±x），
        # 绝不可在侧向安全时选 180°（-z）直冲 4m 处的僵尸正脸
        from mcagent.skills.escape import _pick_cliff_safe_yaw
        from mcagent.skills.voxel_cache import voxel_cache_for

        blocks: dict[tuple[int, int, int], str] = {}
        for x in range(-42, -28):
            for z in range(44, 60):
                for y in range(56, 65):
                    blocks[(x, y, z)] = "air"
                if z >= 54:
                    # +z 方向是深崖（无地面）
                    continue
                blocks[(x, 60, z)] = "stone"

        w = GridWorld(blocks=blocks, pos=[-34.5, 61.0, 52.3])
        w.st.update(dimension="overworld", on_ground=True)
        ex, state = w.session()
        cache = voxel_cache_for(state)
        # 僵尸在 -z 方向 (0, -1)，首选逃跑角朝 +z（yaw=pi）受深崖阻挡
        safe_yaw = _pick_cliff_safe_yaw(
            ex, -34.5, 61.0, 52.3, math.pi, cache, "overworld",
            avoid_water=True, hostile_dir=(0.0, -1.0),
        )
        self.assertIsNotNone(safe_yaw)
        assert safe_yaw is not None
        # 不能直冲 -z（-cos(yaw) < -0.70 表示直冲僵尸）
        self.assertGreater(-math.cos(safe_yaw), -0.70, f"must not pick 180° straight into zombie, got yaw={safe_yaw}")

    def test_seg21_escape_water_does_not_ping_pong_flee_when_hostile_outside_flee_dist(self) -> None:
        # 复现 seg-21 INC-001（154× STUCK_NO_PROGRESS，[-83.3, 62.2, 17.7]）：
        # 浮在水面且 enderman 距 10.6m（>= 9.0m 逃跑预警圈）时，escape_water_to_shore 绝不可转交 _flee_hostile
        # 后 0 位移直接返回 True 死循环，而必须留在上岸流程（上岸/垫脚/挖松软土岸）真正脱水上岸
        from mcagent.skills.escape import escape_water_to_shore

        blocks: dict[tuple[int, int, int], str] = {
            (-84, 61, 17): "dirt",
            (-84, 62, 17): "water",
            (-84, 63, 17): "air",
            (-84, 64, 17): "air",
            (-83, 62, 17): "grass_block",
            (-83, 63, 17): "air",
            (-83, 64, 17): "air",
        }

        class _EndermanBankWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-83.3, 62.2, 17.7], items={})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=7.7, food=17, armor_points=0,
                    in_water=True, submerged=False, on_ground=False,
                    nearest_hostile={"id": 175467, "name": "enderman", "dist": 10.6, "pos": [-93.9, 64.0, 17.7]},
                )
                self.controls = 0
                self.moves = 0

            def control(self, **kwargs):
                self.controls += 1
                return {"pos": list(self.st["pos"]), "moved": 0.0}

            def move_to(self, x=None, z=None, *, y=None, entity_id=None, rng=1, pos=None, timeout=None, **kwargs):
                self.moves += 1
                target = pos if pos is not None else [x, y if y is not None else self.st["pos"][1], z]
                self.st["pos"] = [float(target[0]), float(target[1]), float(target[2])]
                self.st["in_water"] = False
                self.st["on_ground"] = True
                return {"pos": list(self.st["pos"])}

        w = _EndermanBankWorld()
        ex, state = w.session()
        gen = escape_water_to_shore(ex, state, max_steps=3)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(ok)
        self.assertFalse(w.st["in_water"], "bot must actually exit water onto dry shore")
        self.assertEqual(w.controls, 0, "must not bounce into _flee_hostile control loop when enderman is outside flee_dist")
        self.assertGreaterEqual(w.moves, 1)

    def test_seg22_enclosed_dry_stone_pocket_breaches_one_block_instead_of_looping(self) -> None:
        # 复现 seg-22 INC-002（166× SKILL_POSTCONDITION_FAIL，(-83, 62, 17)）：
        # 角色被四面 y=62 实心石/圆石墙且 y=64 顶石围困在 1×1 干地凹坑中，背包有 stone_pickaxe 时，
        # _flee_hostile 在零位移受阻后必须速破 1 格脚部方块（如 (-84, 62, 17) cobblestone）开出 2 格门洞逃离，严禁原地撞墙 166 轮
        from mcagent.skills.escape import _flee_hostile

        blocks: dict[tuple[int, int, int], str] = {
            (-83, 61, 17): "stone",
            (-83, 62, 17): "air",
            (-83, 63, 17): "air",
            (-83, 64, 17): "air",
            (-83, 65, 17): "stone",
        }
        # 四面 y=62 均为实心墙，y=63 为空气，y=64 顶石封顶（无法一步走上台阶）
        for (dx, dz), mat in (((-1, 0), "cobblestone"), ((1, 0), "stone"), ((0, -1), "stone"), ((0, 1), "stone")):
            blocks[(-83 + dx, 61, 17 + dz)] = "stone"
            blocks[(-83 + dx, 62, 17 + dz)] = mat
            blocks[(-83 + dx, 63, 17 + dz)] = "air"
            blocks[(-83 + dx, 64, 17 + dz)] = "stone"

        class _StonePocketWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-82.7, 62.0, 17.7], items={"stone_pickaxe": 1, "cobblestone": 15})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=7.7, food=12, armor_points=0,
                    on_ground=True, in_water=False, submerged=False,
                    nearest_hostile={"id": 177001, "name": "creeper", "dist": 6.5, "pos": [-76.5, 64.0, 17.7]},
                )
                self.dug: list[list[int]] = []

            def move_to(self, *args, **kwargs):
                from mcagent.client import ApiError
                raise ApiError("path_not_found", "no path out of 1x1 pocket")

            def control(self, **kwargs):
                if not self.dug:
                    return {"pos": list(self.st["pos"]), "moved": 0.0, "on_ground": True}
                # 破墙后顺利迈出门洞脱困
                self.st["pos"] = [-85.5, 62.0, 17.5]
                self.st["nearest_hostile"] = {"id": 177001, "name": "creeper", "dist": 9.5, "pos": [-76.5, 64.0, 17.7]}
                return {"pos": list(self.st["pos"]), "moved": 2.8, "on_ground": True}

            def dig(self, pos, **kwargs):
                self.dug.append([int(pos[0]), int(pos[1]), int(pos[2])])
                self.blocks[(int(pos[0]), int(pos[1]), int(pos[2]))] = "air"
                return {"dug": "cobblestone", "pos": list(pos)}

        w = _StonePocketWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(w.dug, "expected _flee_hostile to dig a 1-block doorway breach out of enclosed dry stone pocket")
        self.assertEqual(w.dug[0], [-84, 62, 17], f"expected breach away from east creeper at (-84,62,17), got {w.dug}")
        self.assertTrue(ok)

    def test_seg23_crit_hp_skeleton_skips_stationary_barricade_and_sprints_away(self) -> None:
        # 复现 seg-23 Death #1（t=587.36s, [-156.7, 48, -13.1]）：hp=3.3 残血（一箭即死）且骷髅在 11.1m 时，
        # 严禁原地站桩放圆石挡箭墙吃箭，必须直接拉开距离脱离 16m 射程
        from mcagent.skills.escape import _flee_hostile

        blocks: dict[tuple[int, int, int], str] = {}
        for x in range(-168, -148):
            for z in range(-28, -4):
                blocks[(x, 47, z)] = "stone"
                blocks[(x, 48, z)] = "air"
                blocks[(x, 49, z)] = "air"

        class _CritSkelWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-156.5, 48.0, -13.3], items={"cobblestone": 16})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=3.3, food=17, armor_points=0,
                    on_ground=True, in_water=False, submerged=False,
                    nearest_hostile={"id": 178557, "name": "skeleton", "dist": 11.1, "pos": [-160.7, 48.0, -23.3]},
                )
                self.placed: list[list[int]] = []

            def place(self, item, pos=None, **kwargs):
                self.placed.append(list(pos or []))
                return {"placed": item, "pos": list(pos or [])}

        w = _CritSkelWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        try:
            while True:
                next(gen)
        except StopIteration:
            pass
        self.assertEqual(w.placed, [], f"must not stop to place stationary barricade at hp=3.3 / 11.1m, placed={w.placed}")

    def test_seg23_dry_pocket_breach_equips_pickaxe_before_digging_stone(self) -> None:
        # 复现 seg-23 INC-001（DIG_TOOL_WARNING）：封闭石穴速破圆石/石头前必须先切出石镐再挖，避免空手/圆石慢挖 7s
        from mcagent.skills.escape import _dig_breach_cell
        from mcagent.skills.voxel_cache import voxel_cache_for

        blocks = {(-84, 62, 18): "stone"}

        class _EquipDigWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-83.5, 62.0, 18.5], items={"stone_pickaxe": 1, "cobblestone": 15})
                self.st["held"] = {"name": "cobblestone", "count": 15}
                self.order: list[str] = []

            def equip(self, item, slot="hand", **kwargs):
                self.order.append(f"equip:{item}")
                self.st["held"] = {"name": item, "count": 1}
                return {"held": item}

            def dig(self, pos, **kwargs):
                self.order.append(f"dig:{self.st['held']['name']}")
                return {"dug": "stone", "pos": list(pos)}

        w = _EquipDigWorld()
        ex, state = w.session()
        gen = _dig_breach_cell(ex, state, voxel_cache_for(state), "overworld", (-84, 62, 18))
        try:
            while True:
                next(gen)
        except StopIteration:
            pass
        self.assertEqual(w.order, ["equip:stone_pickaxe", "dig:stone_pickaxe"])

    def test_seg24_iron_excluded_persists_across_chunks_and_blacklists_same_vein_cluster(self) -> None:
        # 复现 seg-24 10 分钟采铁空转：[-7, 5, 49] 超时后必须联动拉黑紧邻同矿脉 [-7, 5, 48]，
        # 且跨区块移动后不可清空 _SESSION_EXCLUDED，避免回主矿井底部反复重试同一批死矿
        from mcagent.client import ApiError
        from mcagent.skills.iron import SmeltIron, _session_excluded_for, reset_iron_memo

        reset_iron_memo()
        self.addCleanup(reset_iron_memo)

        class _UnreachableVeinWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(
                    blocks={
                        (-7, 5, 49): "iron_ore",
                        (-7, 5, 48): "iron_ore",
                        (3, 15, 50): "stone",
                        (3, 16, 50): "air",
                        (3, 17, 50): "air",
                    },
                    pos=[3.5, 16.0, 50.5],
                    items={"stone_pickaxe": 1, "furnace": 1, "coal": 4},
                )
                self.st.update(dimension="overworld", ready=True, alive=True, health=18.0, food=18, on_ground=True)
                self.move_targets: list[tuple[list, float]] = []

            def find_blocks(self, types=None, radius=32, limit=64, **kwargs):
                return {
                    "blocks": [
                        {"name": "iron_ore", "pos": [-7, 5, 49], "dist": 14.9},
                        {"name": "iron_ore", "pos": [-7, 5, 48], "dist": 15.1},
                    ]
                }

            def move_to(self, x=None, z=None, *, y=None, entity_id=None, rng=1, pos=None, timeout=None, **kwargs):
                target = list(pos) if pos is not None else [x, y, z]
                self.move_targets.append((target, float(timeout or 0.0)))
                raise ApiError("timeout", "unreachable separate cavern")

        w = _UnreachableVeinWorld()
        ex, state = w.session()
        skill = SmeltIron(count=3)
        gen = skill._collect(ex, state)
        try:
            for _ in range(3):
                next(gen)
        except Exception:
            pass
        # 1) 远距/深层跨洞矿 move_to 超时必须压紧至 <=12.0s
        self.assertTrue(w.move_targets)
        self.assertLessEqual(w.move_targets[0][1], 12.0, f"expected distant/deep ore timeout <=12s, got {w.move_targets}")
        # 2) [-7, 5, 49] 超时后同矿脉 [-7, 5, 48] 必须联动拉黑，不再二次发起 move_to
        excl = _session_excluded_for(ex.client, state, {"dimension": "overworld", "pos": [3.5, 16.0, 50.5]})
        self.assertIn((-7, 5, 49), excl)
        self.assertIn((-7, 5, 48), excl)
        # 3) 跨区块后（[-34, 61, 52]）仍保留不可达矿点黑名单
        excl_other_chunk = _session_excluded_for(ex.client, state, {"dimension": "overworld", "pos": [-34.5, 61.0, 52.3]})
        self.assertIn((-7, 5, 49), excl_other_chunk)
        self.assertIn((-7, 5, 48), excl_other_chunk)

    def test_seg25_safe_dry_shore_away_caps_get_block_probes_and_early_exits(self) -> None:
        # 复现 seg-25 INC-004 死前 2.37s 卡顿（440.15s..442.52s）：地下洞穴水池 [57, 40, -158] 遇 7.7m 苦力怕时，
        # _find_safe_dry_shore_away 曾对 168 列×4 高度发 917 次同步 /get_block 冻结 2.37s 致苦力怕逼近至 3.2m 引爆；
        # 必须几何先筛 + 环层早停 + 列预算封顶，将探测次数压低一个数量级。
        from mcagent.skills.escape import _find_safe_dry_shore_away
        from mcagent.skills.voxel_cache import voxel_cache_for

        class _CountingCaveWorld(GridWorld):
            def __init__(self, blocks: dict[tuple[int, int, int], str]) -> None:
                super().__init__(blocks=blocks, pos=[57.7, 40.7, -157.1], items={})
                self.st.update(dimension="overworld", ready=True, alive=True, in_water=True)
                self.get_block_calls = 0

            def get_block(self, pos):
                self.get_block_calls += 1
                key = (int(pos[0]), int(pos[1]), int(pos[2]))
                return {"name": self.blocks.get(key, "stone"), "pos": list(key)}

        # 场景 A：全岩壁/水体无干岸的洞穴，苦力怕在西侧 (50.6, -156.7)
        w_empty = _CountingCaveWorld({(57, 40, -158): "water", (57, 41, -158): "air"})
        ex_e, st_e = w_empty.session()
        threats = [(50.6, -156.7, "creeper", 7.1, 184282)]
        res_e = _find_safe_dry_shore_away(
            ex_e, st_e.snapshot(), voxel_cache_for(st_e), "overworld", threats,
        )
        self.assertIsNone(res_e)
        self.assertLessEqual(
            w_empty.get_block_calls, 220,
            f"all-stone cave probe must stay <=220 get_block calls (was 917), got {w_empty.get_block_calls}",
        )

        # 场景 B：东侧 2 格处 (59, 40, -158) 存在可直接登岸（dy=0）的安全干岸，必须在第 2 环完成即早停
        blocks_ledge = {
            (57, 40, -158): "water",
            (59, 39, -158): "stone",
            (59, 40, -158): "air",
            (59, 41, -158): "air",
        }
        w_ledge = _CountingCaveWorld(blocks_ledge)
        ex_l, st_l = w_ledge.session()
        res_l = _find_safe_dry_shore_away(
            ex_l, st_l.snapshot(), voxel_cache_for(st_l), "overworld", threats,
        )
        self.assertEqual(res_l, (59, 40, -158))
        self.assertLessEqual(
            w_ledge.get_block_calls, 90,
            f"ring-2 climbable shore must early-exit <=90 get_block calls, got {w_ledge.get_block_calls}",
        )

    def test_seg25_unshielded_creeper_at_4_8m_sprints_immediately_without_move_to(self) -> None:
        # 复现 seg-25 414.3s 苦力怕 #1 炸残血（hp 11.5 -> 2.0）：无盾在 4.8m 遇苦力怕时，
        # 严禁先走慢速 2.2s move_to（6/6 超时并让苦力怕在 2.05s 内引爆），必须立即防跳崖疾跑跳拉开至 9m+。
        from mcagent.skills.escape import _flee_hostile

        blocks = {(x, 42, z): "stone" for x in range(44, 70) for z in range(-152, -138)}

        class _CreeperCaveWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[52.7, 43.0, -144.3], items={"stone_pickaxe": 1})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=11.5, food=10,
                    armor_points=0, in_water=False, on_ground=True,
                    nearest_hostile={"id": 184594, "name": "creeper", "dist": 4.8, "pos": [48.4, 43.0, -144.5]},
                )
                self.moves = 0
                self.controls = 0

            def move_to(self, *args, **kwargs):
                self.moves += 1
                return {"pos": list(self.st["pos"])}

            def control(self, **kwargs):
                self.controls += 1
                self.st["pos"] = [58.0, 43.0, -144.3]
                self.st["nearest_hostile"] = {"id": 184594, "name": "creeper", "dist": 9.6, "pos": [48.4, 43.0, -144.5]}
                return {"pos": list(self.st["pos"]), "moved": 5.3}

        w = _CreeperCaveWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        ok = False
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertTrue(ok)
        self.assertEqual(w.moves, 0, "unshielded creeper at 4.8m must NOT gamble slow move_to before sprint burst")
        self.assertGreaterEqual(w.controls, 1)

    def test_seg25_climb_out_stairs_ignores_leaf_litter_and_stops_on_non_rising_steps(self) -> None:
        # 复现 seg-25 INC-001（gather_wood STUCK_NO_PROGRESS ×4）：地表 Y=64 铺满 leaf_litter 时，
        # _climb_out_stairs 不得把非实体地被植物（leaf_litter/short_grass）当成实体台阶反复在 47.5<->48.5 横跳 45 步；
        # 且一旦 move_to 返回但海拔未上升 >=0.5，必须在 <=3 步内及时中止而非空转 64 步。
        from mcagent.skills.mine_return import _climb_out_stairs

        blocks = {
            (47, 63, 50): "grass_block",
            (48, 63, 50): "grass_block",
            (46, 63, 50): "grass_block",
            (47, 63, 51): "grass_block",
            (47, 63, 49): "grass_block",
            (48, 64, 50): "leaf_litter",
            (46, 64, 50): "short_grass",
            (47, 64, 51): "leaf_litter",
            (47, 64, 49): "poppy",
        }
        w = GridWorld(blocks=blocks, pos=[47.5, 64.0, 50.5], items={"stone_pickaxe": 1})
        w.st.update(dimension="overworld", ready=True, alive=True, health=11.5, food=7)
        ex, state = w.session()
        recs = []
        gen = _climb_out_stairs(ex, state, target_y=69.0, max_steps=12)
        ok = True
        try:
            while True:
                recs.append(next(gen))
        except StopIteration as stop:
            ok = bool(stop.value)
        self.assertFalse(ok)
        self.assertEqual(recs, [], "must not treat leaf_litter/short_grass/poppy as climbable solid stairs")

        # 场景 B：即使存在实心石块台阶，但 move_to(rng=1) 未实际抬升 Y（停在 Y=64），必须在 <=3 步内中止
        blocks_solid = {
            (47, 63, 50): "stone",
            (48, 64, 50): "stone",
            (46, 64, 50): "stone",
            (47, 64, 51): "stone",
            (47, 64, 49): "stone",
        }

        class _NoRiseWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks_solid, pos=[47.5, 64.0, 50.5], items={"stone_pickaxe": 1})
                self.st.update(dimension="overworld", ready=True, alive=True, health=11.5, food=7)
                self.moves = 0

            def move_to(self, *args, **kwargs):
                self.moves += 1
                # 模拟 rng=1 停留在同层 Y=64 未爬升
                return {"pos": [47.5, 64.0, 50.5]}

        w2 = _NoRiseWorld()
        ex2, st2 = w2.session()
        gen2 = _climb_out_stairs(ex2, st2, target_y=69.0, max_steps=20)
        ok2 = True
        try:
            while True:
                next(gen2)
        except StopIteration as stop:
            ok2 = bool(stop.value)
        self.assertFalse(ok2)
        self.assertLessEqual(w2.moves, 3, f"must abort within <=3 non-rising move_to steps, got {w2.moves}")

    def test_seg26_open_sky_riverbank_below_hilltop_camp_not_treated_as_underground_mine(self) -> None:
        # 复现 seg-26 [72..104]（INC-003 DIG_TOOL_WARNING ×7）：营地在山顶 Y=73（ref_y=73），
        # 角色站在露天河岸 Y=62.4（<= 73-8）树林旁时，is_underground_in_mine 必须核验头顶是否存在非树冠实心岩顶，
        # 绝不能把露天河谷误判为矿道导致 gather_wood 卡死 4 分钟并徒手挖 7 块石头。
        from mcagent.skills.mine_return import is_underground_in_mine, return_to_surface_from_mine
        from mcagent.skills.world_memo import memo_for, reset_all_world_memos

        reset_all_world_memos()
        self.addCleanup(reset_all_world_memos)

        # 场景 A：河岸 Y=62.4，头顶仅空气与橡树树冠（oak_leaves），非矿井
        blocks_open = {
            (-91, 61, 15): "grass_block",
            (-91, 66, 15): "oak_leaves",
            (-91, 67, 15): "oak_log",
        }
        w_open = GridWorld(blocks=blocks_open, pos=[-90.5, 62.4, 15.3], items={})
        w_open.st.update(dimension="overworld", ready=True, alive=True, health=20.0, food=20)
        ex_o, st_o = w_open.session()
        memo_for(st_o).set_camp(pos=[-86, 73, 16], dimension="overworld")
        self.assertFalse(is_underground_in_mine(ex_o, st_o))

        recs = []
        gen = return_to_surface_from_mine(ex_o, st_o, max_climb_steps=4)
        ok_ret = False
        try:
            while True:
                recs.append(next(gen))
        except StopIteration as stop:
            ok_ret = bool(stop.value)
        self.assertTrue(ok_ret)
        self.assertEqual(recs, [], "open-sky riverbank must return immediately without hand-digging stone")

        # 场景 B：同海拔 Y=62.4 但头顶存在实心石顶 -> 确为山洞/矿道，必须判 True
        blocks_cave = {
            (-91, 61, 15): "stone",
            (-91, 66, 15): "stone",
        }
        w_cave = GridWorld(blocks=blocks_cave, pos=[-90.5, 62.4, 15.3], items={})
        w_cave.st.update(dimension="overworld", ready=True, alive=True, health=20.0, food=20)
        ex_c, st_c = w_cave.session()
        memo_for(st_c).set_camp(pos=[-86, 73, 16], dimension="overworld")
        self.assertTrue(is_underground_in_mine(ex_c, st_c))

        # 场景 C：低于海平面深矿 Y=48（<=56 且 <= ref_y-8），即使未探测顶石仍判 True
        w_deep = GridWorld(blocks={(-91, 47, 15): "stone"}, pos=[-90.5, 48.0, 15.3], items={})
        w_deep.st.update(dimension="overworld", ready=True, alive=True, health=20.0, food=20)
        ex_d, st_d = w_deep.session()
        memo_for(st_d).set_camp(pos=[-86, 73, 16], dimension="overworld")
        self.assertTrue(is_underground_in_mine(ex_d, st_d))

    def test_seg26_close_range_followup_rejects_180_turn_back_into_skeleton(self) -> None:
        # 复现 seg-26 [19..23]（INC-001 SKILL_POSTCONDITION_FAIL）：贴脸 <2.5m 骷髅在西侧(-X)，
        # 东侧(+X)为深崖/悬空而北侧(-Z)为通畅矿道时，close-range _pick_cliff_safe_yaw 必须透传 hostile_dir，
        # 拒绝掉头 180° 冲回西侧骷髅脸上，改沿北侧通道脱险。
        from mcagent.skills.escape import _flee_hostile

        blocks: dict[tuple[int, int, int], str] = {}
        # 当前脚下 (-71, 28, 45) 及西侧(-X)和北侧(-Z)有地面，东侧(+X)为深坑，南侧(+Z)为深坑
        for x in range(-78, -70):
            blocks[(x, 28, 45)] = "stone"
        for z in range(36, 46):
            blocks[(-71, 28, z)] = "stone"

        class _CloseRangeCaveWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-70.5, 29.0, 45.5], items={"stone_pickaxe": 1})
                self.st.update(
                    dimension="overworld", ready=True, alive=True, health=17.0, food=20,
                    armor_points=0, in_water=False, on_ground=True,
                    nearest_hostile={"id": 187365, "name": "skeleton", "dist": 1.8, "pos": [-72.3, 29.0, 45.5]},
                )
                self.control_yaws: list[float] = []

            def control(self, **kwargs):
                yaw = float(kwargs.get("yaw", 0.0))
                self.control_yaws.append(yaw)
                # 沿所选 yaw 移动 5m
                dx, dz = -math.sin(yaw) * 5.0, -math.cos(yaw) * 5.0
                nx, nz = self.st["pos"][0] + dx, self.st["pos"][2] + dz
                self.st["pos"] = [nx, 29.0, nz]
                d = math.hypot(nx - (-72.3), nz - 45.5)
                self.st["nearest_hostile"] = {"id": 187365, "name": "skeleton", "dist": round(d, 2), "pos": [-72.3, 29.0, 45.5]}
                return {"pos": list(self.st["pos"]), "moved": 5.0}

        w = _CloseRangeCaveWorld()
        ex, state = w.session()
        gen = _flee_hostile(ex, state)
        try:
            while True:
                next(gen)
        except StopIteration:
            pass
        self.assertTrue(w.control_yaws)
        # 西向(-X)对应 dx = -sin(yaw) < -0.70（即朝向骷髅正脸）；必须拒绝西向而选北向(-Z)
        first_dx = -math.sin(w.control_yaws[0])
        self.assertGreater(
            first_dx, -0.70,
            f"close-range escape must NOT turn 180 deg straight into west skeleton, yaw={w.control_yaws[0]}",
        )

    def test_seg27_gather_food_bounds_move_to_timeout_and_skips_distant_memo_furnace(self) -> None:
        # 复现 seg-27 1177s..1327s 150s 卡死：gather_food 的 _reach_block / _hunt_animals 曾无 timeout 调 ex.move_to
        # 导致每个不可达熔炉/动物挂死 150s，且会跨 40+ 格去翻陈旧地下熔炉；必须透传有界 timeout<=20s 并过滤 >32m 记忆熔炉。
        from mcagent.client import ApiError
        from mcagent.skills.survival_food import _find_or_place_stations, _reach_block
        from mcagent.skills.world_memo import memo_for, reset_all_world_memos

        reset_all_world_memos()
        self.addCleanup(reset_all_world_memos)

        blocks = {
            (-36, 59, 49): "stone",
            (-26, 60, 49): "furnace",   # 10 格内近炉
            (-96, 49, 49): "furnace",   # 60 格远陈旧地下炉
        }

        class _FoodMoveTimeoutWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks=blocks, pos=[-36.5, 60.0, 49.5], items={"porkchop": 2, "coal": 1})
                self.st.update(dimension="overworld", ready=True, alive=True, health=18.0, food=16, on_ground=False)
                self.move_timeouts: list[float | None] = []

            def move_to(self, x=None, z=None, *, y=None, entity_id=None, rng=1, pos=None, timeout=None, retries=None, **kwargs):
                self.move_timeouts.append(timeout)
                raise ApiError("timeout", "bounded timeout")

        w = _FoodMoveTimeoutWorld()
        ex, state = w.session()
        memo = memo_for(state)
        memo.record_station("furnace", (-96, 49, 49), dimension="overworld")
        memo.record_station("furnace", (-26, 60, 49), dimension="overworld")

        # 1) >32m 的陈旧地下熔炉必须被过滤，仅保留 10 格内近炉
        gen_s = _find_or_place_stations(ex, state, "furnace", ("smoker", "furnace"), "gather_food")
        stations = []
        try:
            while True:
                next(gen_s)
        except StopIteration as stop:
            stations = list(stop.value or [])
        station_cells = [tuple(p) for _, p in stations]
        self.assertIn((-26, 60, 49), station_cells)
        self.assertNotIn((-96, 49, 49), station_cells, "distant >32m stale underground furnace must be skipped")

        # 2) _reach_block 必须传有界 timeout<=20.0，绝不裸调 150s 默认超时
        gen_r = _reach_block(ex, state, (-26, 60, 49), 4.0)
        ok_r = True
        try:
            while True:
                next(gen_r)
        except StopIteration as stop:
            ok_r = bool(stop.value)
        self.assertFalse(ok_r)
        self.assertTrue(w.move_timeouts)
        self.assertIsNotNone(w.move_timeouts[0])
        self.assertLessEqual(float(w.move_timeouts[0]), 20.0)

    def test_seg27_eat_food_retries_once_on_noop_use_item_after_window_close(self) -> None:
        # 复现 seg-27 INC-003（1174.2s）：关闭熔炉窗口后瞬时 use_item("cooked_beef") 首次空返回（food 16->16），
        # EatFood 必须自动补试 1 次并在第 2 次成功进食（food 16->20），消除 P1 SKILL_POSTCONDITION_FAIL。
        from mcagent.loop.starter_skills import EatFood

        class _FlakyEatWorld(GridWorld):
            def __init__(self) -> None:
                super().__init__(blocks={}, pos=[-36.6, 60.0, 49.5], items={"cooked_beef": 2})
                self.st.update(dimension="overworld", ready=True, alive=True, health=8.2, food=16)
                self.eat_calls = 0

            def use_item(self, item: str, **kwargs):
                self.eat_calls += 1
                if self.eat_calls == 1:
                    return {"used": item, "food_before": 16, "food": 16}
                self.items[item] = max(0, self.items.get(item, 0) - 1)
                self.st["food"] = 20
                return {"used": item, "food_before": 16, "food": 20}

        w = _FlakyEatWorld()
        ex, state = w.session()
        skill = EatFood()
        before_counts = {"cooked_beef": 2}
        recs = list(skill.run(ex, state))
        self.assertEqual(w.eat_calls, 2)
        self.assertEqual(len(recs), 2)
        self.assertEqual(skill.verify_done(state, before_counts), "")

    def test_seg28_crit_hp_blocks_allay_outpost_and_landmark_backtrack(self) -> None:
        # 复现 seg-25（hp=2.0）与 seg-28（hp=0.9）两度复发的致死链：刚从蜘蛛/苦力怕逃出仅剩 0.9 血无甲，
        # allay_deliver_item 立即启动并借 goto_known_landmark_or_biome 折返掠夺者前哨站 [-83, 68, -33] 撞回怪堆秒死；
        # 1) AllayDeliverItem.blocked 在无甲低血（<=10）及全 T3 技能在残血（<=hp_crit=6）必须阻断；
        # 2) goto_known_landmark_or_biome 在残血（<=hp_crit=6）必须直接返回 False 严禁发出 move_to。
        from mcagent.skills.landmark_nav import goto_known_landmark_or_biome
        from mcagent.skills.t3_biomes import AllayDeliverItem
        from mcagent.skills.world_memo import memo_for, reset_all_world_memos

        reset_all_world_memos()
        self.addCleanup(reset_all_world_memos)

        class _OutpostWorld(GridWorld):
            def __init__(self, health: float) -> None:
                super().__init__(blocks={}, pos=[-100.7, 67.0, -35.1], items={"cobblestone": 8})
                self.st.update(dimension="overworld", ready=True, alive=True, health=health, food=16, armor_points=0)
                self.moves = 0

            def move_to(self, *args, **kwargs):
                self.moves += 1
                return {"pos": list(self.st["pos"])}

        # 场景 A：残血 hp=0.9 无甲 -> blocked 拦截且 landmark_nav 拒绝折返
        w_crit = _OutpostWorld(health=0.9)
        ex_c, st_c = w_crit.session()
        memo_for(st_c).record_landmark("pillager_outpost", (-83, 68, -33), dimension="overworld")
        self.assertTrue(bool(AllayDeliverItem.blocked(st_c)), "AllayDeliverItem must be blocked at hp=0.9")

        gen = goto_known_landmark_or_biome(ex_c, st_c, "pillager_outpost")
        moved = True
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            moved = bool(stop.value)
        self.assertFalse(moved)
        self.assertEqual(w_crit.moves, 0, "landmark_nav must NOT walk back to outpost at crit hp=0.9")

        # 场景 B：无甲低血 hp=8.0 -> AllayDeliverItem 仍阻断；满血 hp=18.0 -> 放行
        w_low = _OutpostWorld(health=8.0)
        _, st_l = w_low.session()
        self.assertTrue(bool(AllayDeliverItem.blocked(st_l)), "AllayDeliverItem must be blocked unarmored at hp=8.0")

        w_full = _OutpostWorld(health=18.0)
        _, st_f = w_full.session()
        self.assertEqual(AllayDeliverItem.blocked(st_f), "")


if __name__ == "__main__":
    unittest.main()
