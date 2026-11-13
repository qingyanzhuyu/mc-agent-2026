"""离线故障注入与主循环边界异常单测（verified: offline）。

严密覆盖 7 项核心边界需求：
1. 60s 双死触发 safe_mode 驻留防三连死（禁止外出砍树/挖石/探索，仅材料充足时允许原地轻度合成，否则返回 idle）；
2. MainLoop 会话级灾难自愈（not_connected 与 death_loop_cooldown，mock sleep 0 等待，自动重连自愈，P0 DEATH_LOOP_TRIGGERED 异常包）；
3. 死因分类准确性（FATAL_ENV_DEATH P0 bug vs COMBAT_OR_MOB_DEATH P1 strategy）；
4. 连续卡死检测（STUCK_NO_PROGRESS P1 strategy，位移与背包双判定）；
5. 动作级零 RTT 异常捕获（DIG_TOOL_WARNING strategy vs MOVE_RANGE_SHORTFALL bug）；
6. ReplayClient 边界与 CLI 入口（ReplayExhausted, ReplayMismatch, CLI --replay / 缺少标志拒绝码 2）；
7. tools/test-island/rcon_ctl.py 纯函数校验（所有 fill 指令单块体积 <= 32768 原版硬上限）。
"""
from __future__ import annotations

import importlib.util
import io
import json
import math
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from mcagent.client import DEFAULT_BASE, ApiError, Config
from mcagent.loop import (
    CampaignPlanner,
    LoopConfig,
    MainLoop,
    RulePlanner,
    SoakObserver,
    replay_incident,
    reset_world_memo,
)
from mcagent.loop import __main__ as loop_main
from mcagent.loop.tape import ReplayClient, ReplayExhausted, ReplayMismatch
from mcagent.runtime import Executor, State
from mcagent.skills import REGISTRY, Skill, make, rec, register
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import CountingExecutor, GridWorld


# ============================================================================
# 辅助离线世界与测试假体
# ============================================================================

class EdgeCaseMockClient(GridWorld):
    """支持注入各类网络与服务端故障的离线测试客户端。"""

    def __init__(self, pos=(0.5, 64.0, 0.5), blocks=None, items=None):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.connect_count = 0
        self.ensure_connected_count = 0
        self.wait_ready_count = 0
        self.status_errors: list[ApiError] = []
        self.action_errors: list[ApiError] = []

    def connect(self, timeout: float = 20) -> dict:
        self.connect_count += 1
        return {"ok": True}

    def disconnect(self) -> dict:
        return {"ok": True}

    def ensure_connected(self, min_interval_s: float = 0.0, timeout: float = 20) -> dict:
        self.ensure_connected_count += 1
        return self.connect(timeout=timeout)

    def wait_ready(self, timeout: float = 120) -> dict:
        self.wait_ready_count += 1
        return self.status()

    def status(self, timeout: float = 10) -> dict:
        if self.status_errors:
            err = self.status_errors.pop(0)
            raise err
        return {
            **self.st,
            "alive": True,
            "ready": True,
            "pos": list(self.st["pos"]),
            "health": self.st.get("health", 20),
            "food": self.st.get("food", 20),
        }

    def find_blocks(self, types=None, radius=32, limit=20, properties=None, mature=None) -> dict:
        return {
            "total": 1,
            "blocks": [{"name": "oak_log", "pos": [1, 64, 0], "dist": 1.0}],
        }

    def dig(self, pos, collect=True, *, stop_on=None) -> dict:
        if self.action_errors:
            err = self.action_errors.pop(0)
            raise err
        return super().dig(pos, collect=collect, stop_on=stop_on)


# ============================================================================
# 1. 60s 双死触发 safe_mode 驻留防三连死
# ============================================================================

class TestSafeModeAntiDeathLoop(unittest.TestCase):
    """验证 60s 内发生 2 次死亡触发 safe_mode，防范第 3 次死亡引发 5 分钟惩罚。"""

    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()
        self.state = State()
        self.state.set_status({
            "ready": True,
            "alive": True,
            "health": 20,
            "food": 20,
            "pos": [0.5, 64.0, 0.5],
        })
        self.state.set_inventory({"items": []})

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def _trigger_two_deaths(self):
        # 第一次死亡
        self.state.set_status({
            "ready": True,
            "alive": True,
            "health": 20,
            "food": 20,
            "pos": [0.5, 64.0, 0.5],
            "last_death": {"t": 1700000000000, "pos": [0, 64, 0], "cause": "Bot fell out of the world"},
        })
        # 5 秒内第二次死亡
        self.state.set_status({
            "ready": True,
            "alive": True,
            "health": 20,
            "food": 20,
            "pos": [0.5, 64.0, 0.5],
            "last_death": {"t": 1700000005000, "pos": [0, 64, 0], "cause": "Bot fell out of the world"},
        })

    def test_two_deaths_in_60s_enters_safe_mode(self):
        self._trigger_two_deaths()
        self.assertEqual(self.state.deaths_in(60), 2)
        self.assertTrue(self.state.in_safe_mode)
        self.assertGreater(self.state.safe_mode_remaining, 0.0)

    def test_safe_mode_blocks_field_foraging_and_returns_idle_when_empty_inventory(self):
        """空包且进入 safe_mode 时，严禁派发 gather_wood/mine_stone/scout_island，必须返回 idle。"""
        self._trigger_two_deaths()
        planner = RulePlanner()
        decision = planner.choose(self.state)

        self.assertIsNone(decision.skill)
        self.assertEqual(decision.source, "idle")
        self.assertIn("safe_mode 避险驻留中", decision.reason)

    def test_safe_mode_contrast_with_normal_mode(self):
        """对比无 safe_mode 状态：正常空包时规划器会派发砍树或外迁。"""
        planner = RulePlanner()
        normal_decision = planner.choose(self.state)
        # 正常空包时必定派发采集原木，而不是 idle
        self.assertIsNotNone(normal_decision.skill)
        self.assertEqual(normal_decision.skill.name, "gather_wood")

        # 触发 safe_mode 后立刻阻断
        self._trigger_two_deaths()
        safe_decision = planner.choose(self.state)
        self.assertIsNone(safe_decision.skill)
        self.assertEqual(safe_decision.source, "idle")

    def test_safe_mode_allows_onsite_crafting_if_materials_sufficient(self):
        """当安全模式期间背包已有足够材料，允许原地合成木镐/石剑/石镐以防身。"""
        self._trigger_two_deaths()
        planner = RulePlanner()

        # 1) 材料足够合成木镐（10 木板）
        self.state.set_inventory({"items": [{"name": "oak_planks", "count": 10, "slot": 0}]})
        d1 = planner.choose(self.state)
        self.assertIsNotNone(d1.skill)
        self.assertEqual(d1.skill.name, "craft_gear")
        self.assertEqual(d1.skill.args["item"], "wooden_pickaxe")
        self.assertIn("safe_mode 期间原地合成 wooden_pickaxe", d1.reason)

        # 2) 材料足够合成石剑（工作台 + 2 棍 + 2 圆石）
        self.state.set_inventory({"items": [
            {"name": "crafting_table", "count": 1, "slot": 0},
            {"name": "stick", "count": 2, "slot": 1},
            {"name": "cobblestone", "count": 2, "slot": 2},
        ]})
        d2 = planner.choose(self.state)
        self.assertIsNotNone(d2.skill)
        self.assertEqual(d2.skill.name, "craft_gear")
        self.assertEqual(d2.skill.args["item"], "stone_sword")
        self.assertIn("safe_mode 期间原地合成 stone_sword", d2.reason)

        # 3) 已有石剑且圆石充足合石镐
        self.state.set_inventory({"items": [
            {"name": "crafting_table", "count": 1, "slot": 0},
            {"name": "stick", "count": 2, "slot": 1},
            {"name": "cobblestone", "count": 3, "slot": 2},
            {"name": "stone_sword", "count": 1, "slot": 3},
        ]})
        d3 = planner.choose(self.state)
        self.assertIsNotNone(d3.skill)
        self.assertEqual(d3.skill.name, "craft_gear")
        self.assertEqual(d3.skill.args["item"], "stone_pickaxe")

    def test_safe_mode_in_campaign_planner_bypasses_llm_and_focus_mode(self):
        """无论是否启用 LLM 或 focus 模式，进入 safe_mode 后均不能冒进发起外出动作。"""
        self._trigger_two_deaths()

        # 1) LLM 模式下进入 safe_mode：短路绕过 LLM 直接走 safe_mode 规则
        mock_llm = MagicMock()
        campaign = CampaignPlanner(mode="campaign", use_llm=True, llm=mock_llm, min_verified="offline")
        d = campaign.select(self.state, [])
        self.assertEqual(d.source, "idle")
        self.assertIn("safe_mode", d.reason)
        mock_llm.generate.assert_not_called()

        # 2) focus 模式下进入 safe_mode：直接返回 idle
        focus = CampaignPlanner(mode="focus", focus_skill="gather_wood", min_verified="offline")
        d_focus = focus.select(self.state, [])
        self.assertIsNone(d_focus.skill)
        self.assertEqual(d_focus.source, "idle")
        self.assertIn("safe_mode 冷却中", d_focus.reason)


# ============================================================================
# 2. MainLoop 会话级灾难自愈（not_connected 与 death_loop_cooldown）
# ============================================================================

class TestMainLoopDisasterRecovery(unittest.TestCase):
    """测试运行中途网络断开或触发 death_loop_cooldown 时的自愈能力与异常建档。"""

    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_mainloop_recovers_from_status_not_connected(self):
        """运行中途拉状态抛 not_connected：主循环重连并自愈，不崩溃退出。"""
        client = EdgeCaseMockClient()
        state = State()
        ex = CountingExecutor(client, state)
        planner = CampaignPlanner(mode="campaign", min_verified="offline")
        cfg = LoopConfig(max_deaths=1, idle_sleep_s=0.0, respawn_poll_s=0.001, enable_monitor=False)

        # 首次 wait_ready 成功，随后技能中抛 not_connected，并在恢复轮询中再抛一次以触发 ensure_connected
        call_seq = {"n": 0}
        orig_status = client.status

        def status_with_transient_dc(*args, **kwargs):
            call_seq["n"] += 1
            if call_seq["n"] in (2, 3):
                raise ApiError("not_connected", "TCP connection reset by peer")
            return orig_status(*args, **kwargs)

        client.status = status_with_transient_dc

        with tempfile.TemporaryDirectory() as tmp:
            obs = SoakObserver(tmp)
            loop = MainLoop(client, state, ex, planner, config=cfg, observer=obs, log=lambda *_: None)
            with patch("time.sleep"):
                res = loop.run()

            self.assertEqual(res.deaths_recovered, 1)
            self.assertEqual(res.stop_reason, "max_deaths_reached")
            self.assertGreaterEqual(client.ensure_connected_count, 1)

    def test_mainloop_recovers_from_death_loop_cooldown_and_observer_records_p0(self):
        """自愈过程中遭遇 death_loop_cooldown：MainLoop 睡眠等待冷却后重连，SoakObserver 准确立档 P0 DEATH_LOOP_TRIGGERED。"""
        client = EdgeCaseMockClient()
        state = State()
        ex = CountingExecutor(client, state)
        planner = CampaignPlanner(mode="campaign", min_verified="offline")
        cfg = LoopConfig(max_deaths=1, idle_sleep_s=0.0, respawn_poll_s=0.001, enable_monitor=False)

        call_seq = {"n": 0}
        orig_status = client.status

        def status_with_cooldown(*args, **kwargs):
            call_seq["n"] += 1
            if call_seq["n"] == 2:
                # 主循环刷新触发死亡检测
                raise ApiError("dead", "Player is dead")
            if call_seq["n"] == 3:
                # 恢复中查询 status 触发 5 分钟防刷死亡惩罚
                raise ApiError(
                    "death_loop_cooldown",
                    "Death loop detected; cooldown 300s active",
                    extra={"cooldown_remaining_seconds": 300.0},
                )
            return orig_status(*args, **kwargs)

        client.status = status_with_cooldown

        with tempfile.TemporaryDirectory() as tmp:
            obs = SoakObserver(tmp)
            loop = MainLoop(client, state, ex, planner, config=cfg, observer=obs, log=lambda *_: None)
            slept_seconds: list[float] = []

            with patch("time.sleep", side_effect=slept_seconds.append):
                res = loop.run()

            self.assertEqual(res.deaths_recovered, 1)
            self.assertEqual(res.stop_reason, "max_deaths_reached")

            # 验证 time.sleep 被调用过且包含冷却等待（300s + 2s 缓冲 = 302s，上限 310s）
            self.assertTrue(any(s >= 300.0 for s in slept_seconds))

            # 验证 SoakObserver 准确生成 P0 bug 级 DEATH_LOOP_TRIGGERED 异常单
            incidents = list(obs._incidents.values())
            death_loop_incidents = [x for x in incidents if x["code"] == "DEATH_LOOP_TRIGGERED"]
            self.assertEqual(len(death_loop_incidents), 1)

            inc = death_loop_incidents[0]
            self.assertEqual(inc["severity"], "P0")
            self.assertEqual(inc["category"], "bug")
            self.assertIn("DEATH_LOOP_TRIGGERED", inc["code"])

            # 检查落盘 JSON 文件字段完整性
            inc_file = Path(inc["path"])
            self.assertTrue(inc_file.exists())
            data = json.loads(inc_file.read_text(encoding="utf-8"))
            self.assertEqual(data["severity"], "P0")
            self.assertEqual(data["category"], "bug")
            self.assertEqual(data["code"], "DEATH_LOOP_TRIGGERED")

    def test_mainloop_action_level_network_recovery(self):
        """动作级执行期间遭遇 not_connected：技能中止、主循环自愈重连恢复并推进。"""
        client = EdgeCaseMockClient(blocks={(1, 64, 0): "oak_log"})
        state = State()
        ex = CountingExecutor(client, state)
        planner = CampaignPlanner(
            mode="focus",
            focus_skill="gather_wood",
            focus_args={"count": 1},
            min_verified="offline",
        )
        cfg = LoopConfig(max_deaths=1, idle_sleep_s=0.0, respawn_poll_s=0.001, enable_monitor=False)

        # 连续抛 3 次 not_connected，耗尽 Executor 重试，触发技能 Replan(not_connected)
        client.action_errors = [
            ApiError("not_connected", "Network drop 1"),
            ApiError("not_connected", "Network drop 2"),
            ApiError("not_connected", "Network drop 3"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            obs = SoakObserver(tmp)
            loop = MainLoop(client, state, ex, planner, config=cfg, observer=obs, log=lambda *_: None)
            with patch("time.sleep"):
                res = loop.run()

            self.assertEqual(res.deaths_recovered, 1)
            self.assertEqual(res.stop_reason, "max_deaths_reached")
            self.assertGreaterEqual(res.skills_replan, 1)


# ============================================================================
# 3. 死因分类准确性
# ============================================================================

class TestDeathCauseClassification(unittest.TestCase):
    """验证环境/致命死因归为 P0 bug (FATAL_ENV_DEATH)，普通刷怪战斗死亡归为 P1 strategy (COMBAT_OR_MOB_DEATH)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.obs = SoakObserver(self.tmp.name)
        self.state = State()
        self.state.set_status({"ready": True, "alive": True, "pos": [0.5, 64.0, 0.5]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_fatal_env_deaths_classified_as_p0_bug(self):
        env_causes = [
            "Bot fell out of the world",
            "Bot fell into the void",
            "Bot tried to swim in lava",
            "Bot drowned",
            "Bot suffocated in a wall",
            "Bot fell from a high place",
        ]
        for cause in env_causes:
            with tempfile.TemporaryDirectory() as tmp:
                obs = SoakObserver(tmp)
                obs.on_death_recovered(
                    self.state,
                    respawn_ms=450.0,
                    cause=cause,
                    death_pos=[0.0, -64.0, 0.0],
                    before_inv_fp={},
                    after_inv_fp={},
                    triggered_death_loop=False,
                )
                incidents = list(obs._incidents.values())
                self.assertEqual(len(incidents), 1, f"Failed for cause: {cause}")
                inc = incidents[0]
                self.assertEqual(inc["code"], "FATAL_ENV_DEATH", f"Mismatch code for {cause}")
                self.assertEqual(inc["severity"], "P0", f"Mismatch severity for {cause}")
                self.assertEqual(inc["category"], "bug", f"Mismatch category for {cause}")

                # 磁盘文件校验
                fpath = Path(inc["path"])
                self.assertTrue(fpath.exists())
                content = json.loads(fpath.read_text(encoding="utf-8"))
                self.assertEqual(content["severity"], "P0")
                self.assertEqual(content["category"], "bug")

    def test_combat_and_mob_deaths_classified_as_p1_strategy(self):
        combat_causes = [
            "Bot was slain by Skeleton",
            "Bot was slain by Zombie",
            "Bot was blown up by Creeper",
            "Bot was shot by Skeleton",
            "",
            "Unknown death cause",
        ]
        for cause in combat_causes:
            with tempfile.TemporaryDirectory() as tmp:
                obs = SoakObserver(tmp)
                obs.on_death_recovered(
                    self.state,
                    respawn_ms=500.0,
                    cause=cause,
                    death_pos=[10.0, 64.0, 10.0],
                    before_inv_fp={},
                    after_inv_fp={},
                    triggered_death_loop=False,
                )
                incidents = list(obs._incidents.values())
                self.assertEqual(len(incidents), 1, f"Failed for cause: {cause}")
                inc = incidents[0]
                self.assertEqual(inc["code"], "COMBAT_OR_MOB_DEATH", f"Mismatch code for {cause}")
                self.assertEqual(inc["severity"], "P1", f"Mismatch severity for {cause}")
                self.assertEqual(inc["category"], "strategy", f"Mismatch category for {cause}")


# ============================================================================
# 4. 连续卡死检测（STUCK_NO_PROGRESS）
# ============================================================================

class TestContinuousStuckDetection(unittest.TestCase):
    """测试连续 N 步位移 < 0.8 格且背包不变时自动立档 STUCK_NO_PROGRESS (P1, strategy)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.obs = SoakObserver(self.tmp.name, stuck_steps=5)
        self.state = State()
        self.state.set_status({"ready": True, "alive": True, "pos": [10.0, 64.0, 10.0]})
        self.state.set_inventory({"items": [{"name": "dirt", "count": 2, "slot": 0}]})
        self.skill = make("gather_wood", {"count": 1})
        self.obs.on_skill_start(self.skill, "focus", "testing", self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stuck_steps_5_triggers_incident_on_small_movement_and_same_inv(self):
        # 模拟 1~4 步：微小抖动，位移 < 0.8，此时尚未达到 5 步阈值
        positions = [
            [10.0, 64.0, 10.0],
            [10.1, 64.0, 10.1],
            [10.05, 64.0, 10.05],
            [10.12, 64.0, 10.08],
        ]
        for p in positions:
            self.state.patch_action({"pos": p})
            self.obs.on_step(self.skill, {"op": "move_to", "pos": p}, self.state)

        # 4 步时不应有卡死告警
        self.assertEqual(len(self.obs._incidents), 0)

        # 第 5 步：仍旧位移极小 (<0.8 格)，背包不变
        self.state.patch_action({"pos": [10.15, 64.0, 10.12]})
        self.obs.on_step(self.skill, {"op": "move_to", "pos": [10.15, 64.0, 10.12]}, self.state)

        # 准确触发 STUCK_NO_PROGRESS
        incidents = list(self.obs._incidents.values())
        self.assertEqual(len(incidents), 1)
        inc = incidents[0]
        self.assertEqual(inc["code"], "STUCK_NO_PROGRESS")
        self.assertEqual(inc["severity"], "P1")
        self.assertEqual(inc["category"], "strategy")
        self.assertLess(inc["detail"]["max_displacement"], 0.8)
        self.assertEqual(len(self.obs._stuck_window), 0)  # 窗口已清空防止紧凑重复刷屏

    def test_stuck_not_triggered_when_displacement_exceeds_threshold(self):
        # 第 5 步发生有效位移 (>= 0.8)
        positions = [
            [10.0, 64.0, 10.0],
            [10.1, 64.0, 10.0],
            [10.2, 64.0, 10.0],
            [10.3, 64.0, 10.0],
            [10.9, 64.0, 10.0],  # 距离第 0 步位移 0.9 >= 0.8
        ]
        for p in positions:
            self.state.patch_action({"pos": p})
            self.obs.on_step(self.skill, {"op": "move_to", "pos": p}, self.state)

        self.assertEqual(len(self.obs._incidents), 0)

    def test_stuck_not_triggered_when_inventory_changes(self):
        # 5 步位移很小，但背包在第 5 步多了一件物品（例如原地钓鱼或拾取）
        positions = [
            [10.0, 64.0, 10.0],
            [10.1, 64.0, 10.0],
            [10.05, 64.0, 10.0],
            [10.08, 64.0, 10.0],
            [10.1, 64.0, 10.0],
        ]
        for i, p in enumerate(positions):
            self.state.patch_action({"pos": p})
            if i == 4:
                self.state.set_inventory({
                    "items": [
                        {"name": "dirt", "count": 2, "slot": 0},
                        {"name": "cobblestone", "count": 1, "slot": 1},
                    ]
                })
            self.obs.on_step(self.skill, {"op": "move_to", "pos": p}, self.state)

        self.assertEqual(len(self.obs._incidents), 0)


# ============================================================================
# 5. 动作级零 RTT 异常捕获（DIG_TOOL_WARNING 与 MOVE_RANGE_SHORTFALL）
# ============================================================================

class TestActionZeroRttChecks(unittest.TestCase):
    """测试利用动作返回值进行零额外 RTT 校验（挖掘工具告警与位移偏差过大）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.obs = SoakObserver(self.tmp.name)
        self.state = State()
        self.state.set_status({"ready": True, "alive": True, "pos": [0.0, 64.0, 0.0]})
        self.skill = make("gather_wood", {"count": 1})
        self.obs.on_skill_start(self.skill, "focus", "testing", self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dig_with_warning_records_dig_tool_warning_strategy(self):
        rec_obj = {
            "op": "dig",
            "pos": [1, 64, 1],
            "result": {
                "dug": "obsidian",
                "warning": "Wrong tool used: bare hands on obsidian; took 250s",
            },
        }
        self.obs.on_step(self.skill, rec_obj, self.state)

        incidents = list(self.obs._incidents.values())
        self.assertEqual(len(incidents), 1)
        inc = incidents[0]
        self.assertEqual(inc["code"], "DIG_TOOL_WARNING")
        self.assertEqual(inc["severity"], "P2")
        self.assertEqual(inc["category"], "strategy")
        self.assertIn("Wrong tool used", inc["detail"]["warning"])

    def test_dig_without_warning_records_no_incident(self):
        rec_obj = {
            "op": "dig",
            "pos": [1, 64, 1],
            "result": {"dug": "oak_log"},
        }
        self.obs.on_step(self.skill, rec_obj, self.state)
        self.assertEqual(len(self.obs._incidents), 0)

    def test_move_range_shortfall_records_bug_when_horizontal_err_exceeds_6(self):
        # 目标 [20.0, 64.0, 30.0]，返回实际终点 [10.0, 64.0, 25.0]
        # 水平偏差 = hypot(10-20, 25-30) = hypot(-10, -5) = sqrt(125) ≈ 11.18 > 6.0
        rec_obj = {
            "op": "move_to",
            "pos": [20.0, 64.0, 30.0],
            "result": {"pos": [10.0, 64.0, 25.0]},
        }
        self.obs.on_step(self.skill, rec_obj, self.state)

        incidents = list(self.obs._incidents.values())
        self.assertEqual(len(incidents), 1)
        inc = incidents[0]
        self.assertEqual(inc["code"], "MOVE_RANGE_SHORTFALL")
        self.assertEqual(inc["severity"], "P2")
        self.assertEqual(inc["category"], "bug")
        self.assertGreater(inc["detail"]["horiz_err"], 6.0)

    def test_move_range_within_tolerance_records_no_incident(self):
        # 目标 [20.0, 64.0, 30.0]，返回终点 [18.0, 64.0, 28.0]
        # 水平偏差 = hypot(-2, -2) = sqrt(8) ≈ 2.83 <= 6.0
        rec_obj = {
            "op": "move_to",
            "pos": [20.0, 64.0, 30.0],
            "result": {"pos": [18.0, 64.0, 28.0]},
        }
        self.obs.on_step(self.skill, rec_obj, self.state)
        self.assertEqual(len(self.obs._incidents), 0)


# ============================================================================
# 6. ReplayClient 边界与 CLI 入口
# ============================================================================

class TestReplayClientAndCliBoundaries(unittest.TestCase):
    """测试 ReplayClient 边界条件（耗尽/路径不匹配/错误透传）与命令行入口安全拦截。"""

    def test_replay_client_exhausted(self):
        entries = [{"path": "status", "ok": True, "result": {"ready": True, "alive": True}}]
        client = ReplayClient(entries)
        res = client.call("status")
        self.assertTrue(res.get("ready"))

        with self.assertRaises(ReplayExhausted) as ctx:
            client.call("status")
        self.assertIn("磁带已耗尽", str(ctx.exception))

    def test_replay_client_mismatch_strict_path(self):
        entries = [{"path": "status", "ok": True, "result": {"ready": True, "alive": True}}]
        client = ReplayClient(entries, strict_path=True)
        with self.assertRaises(ReplayMismatch) as ctx:
            client.call("inventory")
        self.assertIn("请求序列偏离录制磁带", str(ctx.exception))

    def test_replay_client_allows_divergence_when_strict_path_false(self):
        entries = [{"path": "status", "ok": True, "result": {"ready": True, "alive": True}}]
        client = ReplayClient(entries, strict_path=False)
        res = client.call("inventory")
        self.assertTrue(res.get("ready"))

    def test_replay_client_replays_apierror(self):
        entries = [{
            "path": "dig",
            "ok": False,
            "error": {"code": "protected_region", "message": "cannot dig here", "extra": {}, "http": 403},
        }]
        client = ReplayClient(entries)
        with self.assertRaises(ApiError) as ctx:
            client.call("dig")
        self.assertEqual(ctx.exception.code, "protected_region")
        self.assertEqual(ctx.exception.http, 403)

    def test_cli_replay_incident_returns_zero(self):
        """测试 CLI 运行 --replay <INC-xxx.json> 返回码为 0。"""
        # 临时注册一个极简单步技能用于确定性离线回放
        @register
        class EdgeReplayTestSkill(Skill):
            name = "edge_replay_test_skill"
            description = "Test skill for offline replay"
            params = {}
            goal = "test:edge_replay"
            verified = "offline"

            def run(self, ex: Executor, state: State):
                st = ex.client.status()
                yield rec("status", st)

        try:
            with tempfile.TemporaryDirectory() as tmp:
                inc_path = Path(tmp) / "INC-001-P1-test-edge_replay_test_skill.json"
                incident_data = {
                    "id": "INC-001",
                    "severity": "P1",
                    "category": "bug",
                    "code": "TEST_REPLAY_CODE",
                    "title": "Replay test title",
                    "skill_context": {
                        "skill": "edge_replay_test_skill",
                        "args": {},
                        "initial_status": {"ready": True, "alive": True, "pos": [0.5, 64.0, 0.5]},
                        "initial_inventory": {"items": []},
                    },
                    "http_tape": [
                        {"path": "status", "thread": "main", "ok": True, "result": {"ready": True, "alive": True}},
                    ],
                }
                inc_path.write_text(json.dumps(incident_data, ensure_ascii=False), encoding="utf-8")

                with io.StringIO() as buf, redirect_stdout(buf):
                    rc = loop_main.main(["--replay", str(inc_path)])
                    out = buf.getvalue()

                self.assertEqual(rc, 0)
                report = json.loads(out)
                self.assertEqual(report["skill"], "edge_replay_test_skill")
                self.assertEqual(report["outcome"], "completed")
                self.assertEqual(report["steps_executed"], 1)
        finally:
            REGISTRY.pop("edge_replay_test_skill", None)

    def test_cli_missing_live_flag_on_official_base_rejects_with_code_2(self):
        mock_cfg = Config(base=DEFAULT_BASE, username="any_user", password="any_password")
        with patch.object(Config, "load", return_value=mock_cfg):
            with io.StringIO() as buf_err, redirect_stderr(buf_err):
                rc = loop_main.main(["--user", "test_bot"])
                err_msg = buf_err.getvalue()

        self.assertEqual(rc, 2)
        self.assertIn("当前指向官方端点，请先在 docs/05 占位并显式加 --live", err_msg)

    def test_cli_missing_local_target_flag_on_custom_base_rejects_with_code_2(self):
        mock_cfg = Config(base="http://192.168.1.100:8080/api/v1", username="any_user", password="any_password")
        with patch.object(Config, "load", return_value=mock_cfg):
            with io.StringIO() as buf_err, redirect_stderr(buf_err):
                rc = loop_main.main(["--user", "test_bot"])
                err_msg = buf_err.getvalue()

        self.assertEqual(rc, 2)
        self.assertIn("当前指向非官方端点，请显式加 --local-target", err_msg)

    def test_cli_invalid_skill_args_json_rejects_with_code_2(self):
        with io.StringIO() as buf_err, redirect_stderr(buf_err):
            rc = loop_main.main(["--skill-args", "{broken_json", "--local-target"])
        self.assertEqual(rc, 2)

    def test_cli_missing_credentials_rejects_with_code_2(self):
        mock_cfg = Config(base="http://127.0.0.1:8080/api/v1", username="", password="")
        with patch.object(Config, "load", return_value=mock_cfg):
            with io.StringIO() as buf_err, redirect_stderr(buf_err):
                rc = loop_main.main(["--local-target"])
        self.assertEqual(rc, 2)


# ============================================================================
# 7. tools/test-island/rcon_ctl.py 纯函数校验
# ============================================================================

class TestRconCtlPureFunctions(unittest.TestCase):
    """测试 tools/test-island/rcon_ctl.py 纯函数，严密断言全部 fill 单块体积 <= 32768。"""

    @classmethod
    def setUpClass(cls):
        rcon_script = Path(__file__).resolve().parent.parent.parent / "tools" / "test-island" / "rcon_ctl.py"
        cls.rcon_path = rcon_script
        spec = importlib.util.spec_from_file_location("rcon_ctl", str(rcon_script))
        assert spec is not None and spec.loader is not None
        cls.rcon_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.rcon_mod)

    def test_build_scene_commands_all_presets(self):
        mod = self.rcon_mod
        presets = ("empty_day", "night_combat", "low_hp_hunger", "stone_tier")
        for p in presets:
            cmds = mod.build_scene_commands(p, "PlayerTester", cx=10, cy=70, cz=-20)
            self.assertTrue(isinstance(cmds, list) and len(cmds) > 0)
            # 基础重置
            self.assertIn("clear PlayerTester", cmds)
            self.assertIn("effect clear PlayerTester", cmds)
            self.assertIn("xp set PlayerTester 0 levels", cmds)
            self.assertIn("gamemode survival PlayerTester", cmds)
            # 坐标定位
            self.assertTrue(any(c.startswith("tp PlayerTester 10 70 -20") for c in cmds))

        # 场景专属指令检查
        night_cmds = mod.build_scene_commands("night_combat", "P1", cx=0, cy=70, cz=0)
        self.assertIn("time set midnight", night_cmds)
        self.assertTrue(any("zombie" in c for c in night_cmds))

        hunger_cmds = mod.build_scene_commands("low_hp_hunger", "P1", cx=0, cy=70, cz=0)
        self.assertTrue(any("damage P1 10" in c for c in hunger_cmds))

        stone_cmds = mod.build_scene_commands("stone_tier", "P1", cx=0, cy=70, cz=0)
        self.assertTrue(any("give P1 minecraft:wooden_pickaxe 1" in c for c in stone_cmds))

        # 非法场景
        with self.assertRaises(ValueError):
            mod.build_scene_commands("unsupported_scene_name", "P1")

    def test_build_void_perimeter_commands_all_fill_under_vanilla_limit(self):
        """全面验证 build_void_perimeter_commands 产出的所有 fill 单块体积 <= 32768。"""
        mod = self.rcon_mod
        VANILLA_FILL_HARD_CAP = 32768

        test_cases = [
            # 默认常用参数
            {"cx": 0, "cz": 0, "radius": 40, "ring_width": 12, "y_min": -64, "y_max": 128},
            # 大半径宽外圈
            {"cx": 100, "cz": -200, "radius": 64, "ring_width": 32, "y_min": -64, "y_max": 320},
            # 小半径紧凑外圈
            {"cx": -50, "cz": 50, "radius": 16, "ring_width": 8, "y_min": 0, "y_max": 128},
            # 奇数与不对称参数
            {"cx": 13, "cz": -27, "radius": 37, "ring_width": 19, "y_min": -64, "y_max": 128},
            # 浅层
            {"cx": 0, "cz": 0, "radius": 10, "ring_width": 5, "y_min": 60, "y_max": 75},
        ]

        for tc in test_cases:
            cmds = mod.build_void_perimeter_commands(**tc)
            self.assertGreater(len(cmds), 0, f"No commands generated for {tc}")

            for cmd in cmds:
                self.assertTrue(cmd.startswith("fill "), f"Command must start with fill: {cmd}")
                self.assertTrue(cmd.endswith(" minecraft:air"), f"Command must end with air: {cmd}")

                parts = cmd.split()
                self.assertEqual(len(parts), 8, f"Unexpected command structure: {cmd}")
                x1, y1, z1 = int(parts[1]), int(parts[2]), int(parts[3])
                x2, y2, z2 = int(parts[4]), int(parts[5]), int(parts[6])

                dx = abs(x2 - x1) + 1
                dy = abs(y2 - y1) + 1
                dz = abs(z2 - z1) + 1
                volume = dx * dy * dz

                self.assertGreater(volume, 0)
                self.assertLessEqual(
                    volume,
                    VANILLA_FILL_HARD_CAP,
                    f"Volume {volume} (dx={dx}, dy={dy}, dz={dz}) exceeds 32768 limit in cmd: {cmd}",
                )


if __name__ == "__main__":
    unittest.main()
