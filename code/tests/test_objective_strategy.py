"""战略层异步预取、八类换目标触发时机、多技能单次调用与遥测单测（verified: offline）。

硬约束：
- 异步时序一律通过 `threading.Event` 与可注入假时钟 `FakeClock` 控制，不使用 `time.sleep`；
- 不写依赖操作系统时钟精度的断言（兼容 Windows GBK 复核）；
- 文件读写显式指定 `encoding="utf-8"`。
"""
from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mcagent.loop import (
    GOAL_MAP,
    CampaignPlanner,
    LoopConfig,
    MainLoop,
    SoakObserver,
    reset_world_memo,
)
from mcagent.loop import __main__ as loop_main
from mcagent.loop.goals import GoalSpec
from mcagent.planner import llm as llm_mod
from mcagent.planner import skill_choice
from mcagent.planner.llm import LLMClient, LLMError
from mcagent.runtime import EnvBlocked, RuntimeConfig, State
from mcagent.skills import REGISTRY, Param, Skill
from mcagent.skills import base as skill_base
from mcagent.skills.base import rec
from mcagent.skills.testing import CountingExecutor
from tests.test_loop_soak import RichGridWorld


class FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.t = initial

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


def _ok_json(obj: dict, prompt_tokens: int = 210, completion_tokens: int = 25) -> tuple[int, str]:
    return 200, json.dumps({
        "choices": [{"message": {"content": json.dumps(obj, ensure_ascii=False)}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }, ensure_ascii=False)


class RescueSkill(Skill):
    name = "emergency_camp"
    description = "战术层无技能时的兜底驻扎技能"
    params = {" radius_check": Param(int, default=1, min=1, max=3)} if False else {"steps": Param(int, default=1, min=1, max=3)}
    goal = "rescue:camp"
    verified = "offline"

    def run(self, ex, state):
        yield rec("noop")


class RuntimeToggleSkill(Skill):
    name = "toggle_gate_skill"
    description = "可由测试开关动态控制受阻的技能"
    goal = "test:toggle"
    verified = "offline"
    is_blocked = False

    @classmethod
    def blocked(cls, state: State) -> str:
        return "测试门禁开启" if cls.is_blocked else ""

    def run(self, ex, state):
        yield rec("noop")


class ObjectiveStrategyAsyncAndTriggersTests(unittest.TestCase):
    def setUp(self) -> None:
        llm_mod._reset_semaphores()
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        RuntimeToggleSkill.is_blocked = False
        self.clock = FakeClock()

    def tearDown(self) -> None:
        llm_mod._reset_semaphores()
        skill_base._COOLDOWN.clear()
        RuntimeToggleSkill.is_blocked = False

    def _fresh_state(self, **st_overrides) -> State:
        cfg = replace(RuntimeConfig(), model_timeout_s=8.0, model_min_interval_s=0.001)
        state = State(cfg)
        base = {"ready": True, "alive": True, "health": 20, "food": 20, "is_day": True, "pos": [0.5, 64.0, 0.5]}
        base.update(st_overrides)
        state.set_status(base)
        state.set_inventory({"items": []})
        return state

    def _wait_worker(self, planner: CampaignPlanner) -> None:
        assert planner.scheduler is not None
        worker = planner.scheduler._worker
        if worker is not None:
            worker.join(timeout=3.0)
            self.assertFalse(worker.is_alive(), "后台规划线程未在预期事件释放后退出")

    def test_timeout_and_exception_never_block_select_and_return_rule_objective(self) -> None:
        state = self._fresh_state()
        entered = threading.Event()
        release = threading.Event()

        def blocking_poster(url, headers, data, timeout, proxy):
            entered.set()
            release.wait(timeout=3.0)
            self.clock.advance(0.1)
            return _ok_json({"goal_id": "mine_stone", "budget_s": 120, "why": "迟到且已超时"})

        llm = LLMClient(key="k", fast_proxy="", poster=blocking_poster, clock=self.clock, log=None)
        telemetry_rows: list[dict] = []
        planner = CampaignPlanner(
            mode="campaign",
            strategy="llm",
            llm=llm,
            min_verified="offline",
            clock=self.clock,
            on_objective_telemetry=telemetry_rows.append,
        )

        # 第 1 拍：后台线程卡在 release 上，select() 不等待，立即返回规则 Objective (upgrade_tools)
        dec1 = planner.select(state, [])
        self.assertTrue(entered.wait(timeout=2.0))
        self.assertIsNotNone(dec1.objective)
        assert dec1.objective is not None
        self.assertEqual((dec1.objective.goal_id, dec1.objective.source), ("upgrade_tools", "rule"))
        self.assertEqual(dec1.skill.name, "gather_wood")

        # 推进假时钟越过 model_timeout_s (8s)：后台仍未完成，select() 立即超时回退，保持规则 Objective
        self.clock.advance(9.0)
        dec2 = planner.select(state, [])
        assert dec2.objective is not None
        self.assertEqual((dec2.objective.goal_id, dec2.objective.source), ("upgrade_tools", "rule"))
        self.assertTrue(any(r["source"] == "llm_fallback_rule" and not r["adopted"] for r in telemetry_rows))

        # 释放迟到线程：结果已过 deadline，后续边界绝不采纳 "mine_stone"
        release.set()
        self._wait_worker(planner)
        dec3 = planner.select(state, [])
        assert dec3.objective is not None
        self.assertEqual((dec3.objective.goal_id, dec3.objective.source), ("upgrade_tools", "rule"))

        # 模型抛错分支：select() 同样不阻塞、保持规则 Objective
        err_llm = LLMClient(
            key="k",
            fast_proxy="",
            poster=lambda *a: (_ for _ in ()).throw(LLMError("gateway down", kind="net")),
            clock=self.clock,
            log=None,
        )
        err_rows: list[dict] = []
        err_planner = CampaignPlanner(
            mode="campaign",
            strategy="llm",
            llm=err_llm,
            min_verified="offline",
            clock=self.clock,
            on_objective_telemetry=err_rows.append,
        )
        with patch("mcagent.planner.llm.time.sleep"):
            d_err1 = err_planner.select(state, [])
            self._wait_worker(err_planner)
            d_err2 = err_planner.select(state, [])
        assert d_err1.objective is not None and d_err2.objective is not None
        self.assertEqual((d_err1.objective.source, d_err2.objective.source), ("rule", "rule"))
        self.assertTrue(any(r["source"] == "llm_fallback_rule" and not r["adopted"] for r in err_rows))

    def test_delayed_result_adopted_at_next_boundary_and_stale_result_discarded(self) -> None:
        state = self._fresh_state()
        entered = threading.Event()
        release = threading.Event()

        def controlled_poster(url, headers, data, timeout, proxy):
            entered.set()
            release.wait(timeout=3.0)
            return _ok_json({"goal_id": "mine_stone", "budget_s": 135, "why": "先稳拿圆石分"})

        llm = LLMClient(key="k", fast_proxy="", poster=controlled_poster, clock=self.clock, log=None)
        rows: list[dict] = []
        planner = CampaignPlanner(
            mode="campaign",
            strategy="llm",
            llm=llm,
            min_verified="offline",
            clock=self.clock,
            on_objective_telemetry=rows.append,
        )

        # 边界 1：尚未返回 -> 立即用规则 upgrade_tools
        d1 = planner.select(state, [])
        self.assertTrue(entered.wait(timeout=2.0))
        assert d1.objective is not None
        self.assertEqual((d1.objective.goal_id, d1.objective.source), ("upgrade_tools", "rule"))

        # 技能 1 执行期间模型在 deadline 内返回 -> 下一边界采纳替换！
        self.clock.advance(1.5)
        release.set()
        self._wait_worker(planner)

        d2 = planner.select(state, [])
        assert d2.objective is not None
        self.assertEqual((d2.objective.goal_id, d2.objective.source, d2.objective.budget_s), ("mine_stone", "llm", 135.0))
        adopted_rows = [r for r in rows if r["source"] == "llm" and r["adopted"]]
        self.assertEqual(len(adopted_rows), 1)
        self.assertEqual(adopted_rows[0]["replaced_goal_id"], "upgrade_tools")

        # 过期场景 A：模型在思考 prep_wood 期间，当前技能已经把原木砍完使 prep_wood 达成 -> 下一边界校验丢弃
        state_stale = self._fresh_state()
        entered2 = threading.Event()
        release2 = threading.Event()

        def stale_goal_poster(*_a):
            entered2.set()
            release2.wait(timeout=3.0)
            return _ok_json({"goal_id": "prep_wood", "budget_s": 90, "why": "去砍树"})

        llm_stale = LLMClient(key="k", fast_proxy="", poster=stale_goal_poster, clock=self.clock, log=None)
        stale_rows: list[dict] = []
        p_stale = CampaignPlanner(
            mode="campaign", strategy="llm", llm=llm_stale, min_verified="offline",
            clock=self.clock, on_objective_telemetry=stale_rows.append,
        )
        p_stale.select(state_stale, [])
        self.assertTrue(entered2.wait(timeout=2.0))
        # 技能执行完，背包已有 4 块原木（prep_wood 已达成！）
        state_stale.set_inventory({"items": [{"name": "oak_log", "count": 4, "slot": 0}]})
        release2.set()
        self._wait_worker(p_stale)

        d_after = p_stale.select(state_stale, [])
        assert d_after.objective is not None
        self.assertNotEqual(d_after.objective.goal_id, "prep_wood")
        self.assertTrue(any(r["source"] == "llm_fallback_rule" and not r["adopted"] for r in stale_rows))

        # 过期场景 B：后台请求尚未返回时发生死亡重生 -> 世代作废，迟到旧目标绝不覆盖重生后的新目标
        state_die = self._fresh_state()
        entered3 = threading.Event()
        release3 = threading.Event()
        respawn_calls = {"n": 0}

        def respawn_race_poster(*_a):
            respawn_calls["n"] += 1
            if respawn_calls["n"] == 1:
                entered3.set()
                release3.wait(timeout=3.0)
                return _ok_json({"goal_id": "prep_wood", "budget_s": 90, "why": "死前旧目标"})
            return _ok_json({"goal_id": "mine_stone", "budget_s": 150, "why": "重生后新目标"})

        llm_die = LLMClient(key="k", fast_proxy="", poster=respawn_race_poster, clock=self.clock, log=None)
        die_rows: list[dict] = []
        p_die = CampaignPlanner(
            mode="campaign", strategy="llm", llm=llm_die, min_verified="offline",
            clock=self.clock, on_objective_telemetry=die_rows.append,
        )
        p_die.select(state_die, [])
        self.assertTrue(entered3.wait(timeout=2.0))
        state_die._record_death(mono_ts=self.clock())
        p_die.select(state_die, [])  # 触发 respawn -> invalidate 旧世代
        release3.set()
        self._wait_worker(p_die)
        d_post_die = p_die.select(state_die, [])
        assert d_post_die.objective is not None
        self.assertNotEqual(d_post_die.objective.source, "llm")
        self.assertTrue(any(not r["adopted"] and r["source"] == "llm_fallback_rule" for r in die_rows))

    def test_all_eight_triggers_covered_one_by_one(self) -> None:
        calls: list[str] = []

        def make_planner(ledger=None, registry=None, review_s: float = 600.0):
            calls.clear()
            rows: list[dict] = []

            def poster(url, headers, data, timeout, proxy):
                user_text = json.loads(data)["messages"][-1]["content"]
                calls.append(user_text)
                menu_section = user_text.split("可选目标", 1)[1] if "可选目标" in user_text else user_text
                if "- toggle_goal:" in menu_section:
                    gid = "toggle_goal"
                elif "- mine_stone:" in menu_section:
                    gid = "mine_stone"
                else:
                    gid = "upgrade_tools"
                return _ok_json({"goal_id": gid, "budget_s": 120, "why": "测试响应"})

            llm = LLMClient(key="k", fast_proxy="", poster=poster, clock=self.clock, log=None)
            kw = dict(
                mode="campaign",
                strategy="llm",
                llm=llm,
                min_verified="offline",
                review_interval_s=review_s,
                clock=self.clock,
                on_objective_telemetry=rows.append,
            )
            if ledger is not None:
                kw["ledger"] = ledger
            if registry is not None:
                kw["registry"] = registry
            return CampaignPlanner(**kw), rows

        # 触发器 1：当前目标已达成（goal_achieved）
        st1 = self._fresh_state()
        p1, rows1 = make_planner()
        p1.select(st1, [])
        self._wait_worker(p1)
        p1.select(st1, [])  # 采纳 mine_stone
        self.assertEqual(len(calls), 1)
        st1.set_inventory({"items": [
            {"name": "wooden_pickaxe", "count": 1, "slot": 0},
            {"name": "cobblestone", "count": 4, "slot": 1},
        ]})
        p1.select(st1, [])
        self._wait_worker(p1)
        self.assertEqual(len(calls), 2)
        self.assertIn("goal_achieved", [r["trigger"] for r in rows1])

        # 触发器 2：目标链被环境挂起（env_blocked）
        st2 = self._fresh_state()
        p2, rows2 = make_planner()
        p2.select(st2, [])
        self._wait_worker(p2)
        p2.select(st2, [])
        self.assertEqual(len(calls), 1)
        p2.record_environment_block(
            REGISTRY["gather_wood"](count=3),
            EnvBlocked("无树", missing_predicate="wood", searched_volume="r16", wake_on="chunk_moved"),
            st2,
        )
        p2.select(st2, [])
        self._wait_worker(p2)
        self.assertIn("env_blocked", [r["trigger"] for r in rows2])

        # 触发器 3：超出 budget_s（budget_exceeded）
        st3 = self._fresh_state()
        p3, rows3 = make_planner(review_s=3600.0)
        p3.select(st3, [])
        self._wait_worker(p3)
        p3.select(st3, [])  # mine_stone budget_s=120
        self.assertEqual(len(calls), 1)
        self.clock.advance(125.0)
        p3.select(st3, [])
        self._wait_worker(p3)
        self.assertEqual(len(calls), 2)
        self.assertIn("budget_exceeded", [r["trigger"] for r in rows3])

        # 触发器 4：连续失败达到台账 max_fails_per_ep（中间穿插 scout_island 成功也不清空连败）
        fragile = GoalSpec(
            id="fragile_goal", title="易败目标", tier="T1", score=25,
            skill="gather_wood", skill_args=(("count", 3),), max_fails_per_ep=2, budget_s=300.0, stage="graduated",
        )
        st4 = self._fresh_state()
        p4, rows4 = make_planner(ledger=(fragile, GOAL_MAP["prep_wood"], GOAL_MAP["prep_wooden_pickaxe"], GOAL_MAP["mine_stone"]))
        p4.select(st4, [])  # 规则首选 fragile_goal
        self._wait_worker(p4)
        p4.set_objective(p4.rule_objective(st4))
        self.assertEqual(p4.current_objective.goal_id, "fragile_goal")
        p4.record_skill_outcome("gather_wood", ok=False, elapsed_s=5.0)
        p4.record_skill_outcome("scout_island", ok=True, elapsed_s=3.0)  # 外迁成功不应重置目标连败
        p4.select(st4, [])
        self.assertNotIn("max_fails_reached", [r["trigger"] for r in rows4])
        p4.record_skill_outcome("gather_wood", ok=False, elapsed_s=5.0)
        p4.select(st4, [])
        self._wait_worker(p4)
        self.assertIn("max_fails_reached", [r["trigger"] for r in rows4])

        # 触发器 5：死亡重生（State.deaths 增加）
        st5 = self._fresh_state()
        p5, rows5 = make_planner()
        p5.select(st5, [])
        self._wait_worker(p5)
        p5.select(st5, [])
        self.assertEqual(len(calls), 1)
        st5._record_death(mono_ts=self.clock())
        p5.select(st5, [])
        self._wait_worker(p5)
        self.assertEqual(len(calls), 2)
        self.assertIn("respawn", [r["trigger"] for r in rows5])

        # 触发器 6：昼夜切换（day_night_changed）
        st6 = self._fresh_state(is_day=True)
        p6, rows6 = make_planner()
        p6.select(st6, [])
        self._wait_worker(p6)
        p6.select(st6, [])
        self.assertEqual(len(calls), 1)
        st6.set_status({**st6.st, "is_day": False})
        p6.select(st6, [])
        self._wait_worker(p6)
        self.assertEqual(len(calls), 2)
        self.assertIn("day_night_changed", [r["trigger"] for r in rows6])

        # 触发器 7：每 600 秒例行复核（periodic_review）
        st7 = self._fresh_state()
        p7, rows7 = make_planner(review_s=600.0)
        p7.select(st7, [])
        self._wait_worker(p7)
        # 批一个长预算 1200s 以单独触发 600s 复核而非超时
        from mcagent.planner.objective import Objective
        p7.set_objective(Objective("mine_stone", 1200.0, "长预算", "llm", self.clock()), now=self.clock())
        self.clock.advance(599.0)
        p7.select(st7, [])
        self.assertNotIn("periodic_review", [r["trigger"] for r in rows7])
        self.clock.advance(2.0)
        p7.select(st7, [])
        self._wait_worker(p7)
        self.assertIn("periodic_review", [r["trigger"] for r in rows7])

        # 触发器 8：战术层对当前目标给不出任何技能（no_tactical_skill）
        toggle_goal = GoalSpec(
            id="toggle_goal", title="门禁目标", tier="T1", score=30,
            skill="toggle_gate_skill", budget_s=300.0, stage="graduated",
        )
        reg8 = {**REGISTRY, "toggle_gate_skill": RuntimeToggleSkill}
        st8 = self._fresh_state()
        p8, rows8 = make_planner(
            ledger=(toggle_goal, GOAL_MAP["prep_wood"], GOAL_MAP["prep_wooden_pickaxe"], GOAL_MAP["mine_stone"]),
            registry=reg8,
        )
        d8_first = p8.select(st8, [])
        self._wait_worker(p8)
        p8.select(st8, [])  # 采纳模型选出的 toggle_goal
        self.assertEqual(d8_first.skill.name, "toggle_gate_skill")
        RuntimeToggleSkill.is_blocked = True  # 使当前目标战术展开失灵
        d8_next = p8.select(st8, [])
        self._wait_worker(p8)
        self.assertIn("no_tactical_skill", [r["trigger"] for r in rows8])
        self.assertEqual(d8_next.skill.name, "gather_wood")

    def test_consecutive_multi_skill_progression_calls_llm_only_once(self) -> None:
        blocks = {}
        for i in range(6):
            blocks[(2 + i, 64, 0)] = "oak_log"
        for i in range(12):
            blocks[(2 + i, 64, 2)] = "stone"
        blocks[(12, 63, 5)] = "grass_block"

        llm_call_count = {"n": 0}

        def poster(url, headers, data, timeout, proxy):
            llm_call_count["n"] += 1
            return _ok_json({"goal_id": "upgrade_tools", "budget_s": 300, "why": "一次定下石镐目标"})

        world = RichGridWorld(blocks=blocks, keep_inventory=True)
        state = State(replace(RuntimeConfig(), model_min_interval_s=0.001))
        ex = CountingExecutor(world, state)
        llm = LLMClient(key="k", fast_proxy="", poster=poster, log=None)

        with tempfile.TemporaryDirectory() as tmp:
            obs = SoakObserver(tmp, source="local-target", target_goal="upgrade_tools")
            planner = CampaignPlanner(
                mode="campaign",
                strategy="llm",
                llm=llm,
                min_verified="offline",
                target_goal="upgrade_tools",
                on_objective_telemetry=obs.on_objective_decision,
            )
            cfg = LoopConfig(
                max_steps=80,
                idle_sleep_s=0.0,
                respawn_poll_s=0.001,
                enable_monitor=False,
                target_goal="upgrade_tools",
                stop_on_goal=True,
            )
            res = MainLoop(world, state, ex, planner, config=cfg, observer=obs, log=lambda *_: None).run()
            self._wait_worker(planner)

            # 连续跑完 gather_wood -> craft_gear(wooden_pickaxe) -> mine_stone -> craft_gear(stone_pickaxe) 共 >=4 个技能
            self.assertEqual(res.stop_reason, "goal_reached")
            self.assertGreaterEqual(res.skills_completed, 4)
            self.assertGreaterEqual(state.count("stone_pickaxe"), 1)
            # 整个多技能推进期间模型只被调用 1 次！
            self.assertEqual(llm_call_count["n"], 1)

            tl_rows = [
                json.loads(ln)
                for ln in (Path(tmp) / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
            obj_rows = [r for r in tl_rows if r["event"] == "objective_decision"]
            self.assertGreaterEqual(len(obj_rows), 1)
            for req_key in ("trigger", "source", "goal_id", "ms", "total_tokens", "errors", "adopted", "replaced_goal_id"):
                self.assertIn(req_key, obj_rows[0])

    def test_survival_and_safe_mode_stay_above_objective_without_calling_llm(self) -> None:
        calls = {"n": 0}
        llm = LLMClient(
            key="k",
            fast_proxy="",
            poster=lambda *a: (calls.__setitem__("n", calls["n"] + 1), _ok_json({"goal_id": "mine_stone"}))[1],
            clock=self.clock,
            log=None,
        )
        planner = CampaignPlanner(mode="campaign", strategy="llm", llm=llm, min_verified="offline", clock=self.clock)
        state = self._fresh_state()
        planner.select(state, [])
        self._wait_worker(planner)
        planner.select(state, [])  # 采纳 mine_stone
        self.assertEqual(calls["n"], 1)
        assert planner.current_objective is not None
        self.assertEqual(planner.current_objective.goal_id, "mine_stone")

        # 饥饿且背包有食物：立即吃面包，不改当前 Objective，也不调模型
        state.set_status({**state.st, "food": 10, "health": 16})
        state.set_inventory({"items": [{"name": "bread", "count": 2, "slot": 0}]})
        eat_dec = planner.select(state, [])
        self.assertEqual(eat_dec.skill.name, "eat_food")
        self.assertEqual(eat_dec.source, "rule")
        assert eat_dec.objective is not None
        self.assertEqual(eat_dec.objective.goal_id, "mine_stone")
        self.assertEqual(calls["n"], 1)

        # safe_mode 期间即使发生死亡，也不在 safe_mode 期间发起目标决策；等冷却结束才补发
        state.set_status({**state.st, "food": 20, "health": 20})
        state.safe_mode_until = time.monotonic() + 30.0
        state._record_death(mono_ts=self.clock())
        safe_dec = planner.select(state, [])
        self.assertEqual(safe_dec.source, "idle")
        self.assertEqual(calls["n"], 1)

        state.safe_mode_until = 0.0
        after_safe_dec = planner.select(state, [])
        self._wait_worker(planner)
        self.assertIsNotNone(after_safe_dec.skill)
        self.assertEqual(calls["n"], 2)

    def test_skill_level_llm_only_used_as_fallback_when_tactical_gives_no_skill(self) -> None:
        # 1) 常规阶段战术层能展开技能：skill_choice.choose 一次都不被调用
        state = self._fresh_state()
        llm_ok = LLMClient(
            key="k", fast_proxy="", poster=lambda *a: _ok_json({"goal_id": "mine_stone"}), clock=self.clock, log=None
        )
        p_normal = CampaignPlanner(mode="campaign", strategy="llm", llm=llm_ok, min_verified="offline", clock=self.clock)
        with patch.object(skill_choice, "choose", wraps=skill_choice.choose) as spy_skill_choose:
            p_normal.select(state, [])
            self._wait_worker(p_normal)
            p_normal.select(state, [])
            spy_skill_choose.assert_not_called()

        # 2) 台账为空（战术层给不出任何目标技能），且仅注册 emergency_camp -> 异步触发技能级模型兜底
        llm_fb = LLMClient(
            key="k",
            fast_proxy="",
            poster=lambda *a: _ok_json({"skill": "emergency_camp", "args": {"steps": 2}, "why": "兜底驻扎"}),
            clock=self.clock,
            log=None,
        )
        p_fb = CampaignPlanner(
            mode="campaign",
            strategy="llm",
            llm=llm_fb,
            ledger=(),
            registry={"emergency_camp": RescueSkill},
            min_verified="offline",
            clock=self.clock,
        )
        d_first = p_fb.select(state, [])
        self.assertEqual(d_first.source, "idle")  # 首拍非阻塞立即返回
        self._wait_worker(p_fb)
        d_second = p_fb.select(state, [])
        self.assertEqual(d_second.source, "llm")
        self.assertIsNotNone(d_second.skill)
        self.assertEqual(d_second.skill.name, "emergency_camp")

    def test_require_iron_gate_and_dry_cli_strategy_llm(self) -> None:
        calls = {"n": 0}
        llm = LLMClient(
            key="k",
            fast_proxy="",
            poster=lambda *a: (calls.__setitem__("n", calls["n"] + 1), _ok_json({"goal_id": "mine_stone"}))[1],
            clock=self.clock,
            log=None,
        )
        state = self._fresh_state()
        p_gated = CampaignPlanner(
            mode="campaign", strategy="llm", llm=llm, min_verified="offline", require_iron=True, clock=self.clock
        )
        p_gated.select(state, [])
        self.assertEqual(calls["n"], 0)  # 铁器前不调模型

        # 拿到铁锭并触发新事件后放行
        state.set_inventory({"items": [{"name": "iron_ingot", "count": 1, "slot": 0}]})
        state.set_status({**state.st, "is_day": False})
        p_gated.select(state, [])
        self._wait_worker(p_gated)
        self.assertEqual(calls["n"], 1)

        # CLI --dry --strategy llm 离线干跑探测通过
        out = io.StringIO()
        with patch.object(loop_main.Client, "probe", return_value="unauthorized"), redirect_stdout(out):
            rc = loop_main.main(["--dry", "--strategy", "llm"])
        self.assertEqual(rc, 0)
        self.assertIn("strategy=llm", out.getvalue())


if __name__ == "__main__":
    unittest.main()
