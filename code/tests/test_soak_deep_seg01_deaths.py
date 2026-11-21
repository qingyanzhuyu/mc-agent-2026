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

        # 脱险完成后 clear_recent_hurt 不再复发
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


if __name__ == "__main__":
    unittest.main()
