"""连续异步观察与单目标止损退轮主循环离线单测（verified: offline）。

覆盖 7 项核心异步观察与退轮契约：
1. LoopConfig 默认开启 continuous_observe / async_observe_for_llm，且 MainLoop 构造连续模式 SafetyMonitor；
2. 异步观察路径下 MainLoop 调模型前跳过同步 status/events，直接复用后台线程更新的状态，clean done=True 零同步拉取；
3. 仅当 inventory_dirty=True 时，MainLoop 在 select 前与技能 done=True 后同步补拉 inventory（不补拉 status/events）；
4. done=True 补拉脏背包时，非致命网络错误不崩主循环，致命错误（如 unauthorized）正常向上抛出；
5. enable_monitor=False / async_observe_for_llm=False / 启动 drain 失败未追平（_events_booted=False）时正确退回同步刷新并在追平后恢复复用；
6. 后台 SafetyMonitor 记下死亡后，MainLoop 无需同步刷状态即在缓存 State 上触发 _is_dead_now -> _recover_guarded；
7. PlanDecision(stop_episode=True) 立即退出主循环（不睡 idle_sleep_s），返回 goal_stopped_out，并驱动 SoakObserver 落盘汇总与异常单。
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

from mcagent.client import FATAL, ApiError
from mcagent.loop import (
    LoopConfig,
    MainLoop,
    PlanDecision,
    SoakObserver,
    reset_world_memo,
)
from mcagent.runtime import RuntimeConfig, SafetyMonitor, State
from mcagent.skills import Skill, rec
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import CountingExecutor
from tests.test_loop_edge_cases import EdgeCaseMockClient


class ThreadTrackingMockClient(EdgeCaseMockClient):
    """线程感知离线假客户端：记录每次 status / events / inventory 的调用线程与方法名。"""

    def __init__(self, pos=(0.5, 64.0, 0.5), blocks=None, items=None):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self._trace_lock = threading.Lock()
        self.call_trace: list[tuple[str, str]] = []
        self.events_errors: list[ApiError] = []
        self.inventory_errors: list[ApiError] = []
        self.status_override = None  # Optional[Callable[[str, dict], dict]]

    def clear_trace(self) -> None:
        with self._trace_lock:
            self.call_trace.clear()

    def main_calls(self) -> list[str]:
        with self._trace_lock:
            return [m for t, m in self.call_trace if t == "MainThread"]

    def monitor_calls(self) -> list[str]:
        with self._trace_lock:
            return [m for t, m in self.call_trace if t == "mcagent-safety-monitor"]

    def _record_call(self, method: str) -> str:
        tname = threading.current_thread().name
        with self._trace_lock:
            self.call_trace.append((tname, method))
        return tname

    def status(self, timeout: float = 10) -> dict:
        tname = self._record_call("status")
        st = super().status(timeout=timeout)
        if self.status_override is not None:
            return self.status_override(tname, st)
        return st

    def events(self, since: int = 0, limit: int = 100) -> dict:
        self._record_call("events")
        if self.events_errors:
            raise self.events_errors.pop(0)
        return super().events(since=since, limit=limit)

    def inventory(self, timeout: float = 10) -> dict:
        self._record_call("inventory")
        if self.inventory_errors:
            raise self.inventory_errors.pop(0)
        return super().inventory(timeout=timeout)

    def advancements(self, status: str = "completed", include_recipes: bool = False) -> dict:
        return {"advancements": []}


class _MoveOnlySkill(Skill):
    """只执行一次 move_to（不标脏背包）即结束的测试技能。"""

    name = "test_move_only"
    description = "测试用纯移动技能"
    params = {}
    goal = "test:move_only"
    verified = "offline"

    def run(self, ex, state):
        yield rec("move_to", ex.move_to(x=8, z=0), x=8, z=0)


class _DigOnceSkill(Skill):
    """执行一次 dig（会标记 state.inventory_dirty=True）即结束的测试技能。"""

    name = "test_dig_once"
    description = "测试用单次挖掘技能"
    params = {}
    goal = "test:dig_once"
    verified = "offline"

    def __init__(self, pos=(1, 64, 0), on_dug=None):
        super().__init__()
        self._pos = list(pos)
        self._on_dug = on_dug

    def run(self, ex, state):
        res = ex.dig(self._pos)
        if self._on_dug is not None:
            self._on_dug()
        yield rec("dig", res, pos=self._pos)


class _SpyPlanner:
    """最小确定性规划器替身。"""

    mode = "campaign"

    def __init__(self, selector):
        self._selector = selector
        self.calls = 0

    def select(self, state: State, recent_failures: list[str]) -> PlanDecision:
        self.calls += 1
        return self._selector(self.calls, state, recent_failures)

    def blocked_targets(self, state: State, target_goals=(), refresh: bool = False) -> dict:
        return {}


class _SpyObserver:
    """最小轻量观察者替身，便于在 on_run_start / on_skill_end 精确断言调用轨迹。"""

    def __init__(self, on_start=None, on_skill_end=None):
        self.server_advancements: dict[str, float] = {}
        self.target_goals: tuple[str, ...] = ()
        self.tape = None
        self._on_start = on_start
        self._on_skill_end = on_skill_end
        self.skill_ends: list[tuple[str, str]] = []

    def update_goals_reached(self, state: State) -> dict:
        return {}

    def on_run_start(self, state: State) -> None:
        if self._on_start is not None:
            self._on_start(state)

    def on_progress_tick(self, state: State, client=None, *, force: bool = False) -> dict:
        return {}

    def on_skill_start(self, skill, source: str, reason: str, state: State) -> None:
        pass

    def on_step(self, skill, record: Optional[dict], state: State, client=None) -> None:
        pass

    def on_skill_end(self, skill, outcome: str, state: State, **kwargs) -> None:
        self.skill_ends.append((skill.name, outcome))
        if self._on_skill_end is not None:
            self._on_skill_end(skill, outcome, state, **kwargs)

    def on_death_recovered(self, *args, **kwargs) -> None:
        pass

    def on_run_end(self, state: State, client=None, *, stop_reason: str = "", **kwargs) -> None:
        pass


class AsyncObserveLoopTests(unittest.TestCase):
    """验证异步连续观察、脏背包按需同步、同步回退及单目标止损即时退轮。"""

    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_loop_config_defaults_and_creates_continuous_safety_monitor(self):
        cfg = LoopConfig()
        self.assertIs(cfg.continuous_observe, True)
        self.assertIs(cfg.async_observe_for_llm, True)

        client = ThreadTrackingMockClient()
        state = State()
        ex = CountingExecutor(client, state)
        planner = _SpyPlanner(lambda _n, _st, _fails: PlanDecision(None, "idle", "done"))
        run_cfg = LoopConfig(enable_monitor=True, idle_sleep_s=0.0)

        with patch("mcagent.loop.engine.SafetyMonitor") as mock_monitor_cls:
            mock_monitor_cls.return_value = MagicMock()
            loop = MainLoop(client, state, ex, planner, config=run_cfg, log=lambda *_: None)
            res = loop.run()

        self.assertEqual(res.stop_reason, "idle:done")
        mock_monitor_cls.assert_called_once()
        kwargs = mock_monitor_cls.call_args.kwargs
        self.assertIs(kwargs.get("continuous"), True)
        self.assertIs(kwargs.get("refresh_inventory"), True)
        mock_monitor_cls.return_value.start.assert_called_once()
        mock_monitor_cls.return_value.close.assert_called_once()

    def test_async_observe_skips_sync_status_and_drain_and_reuses_background_state(self):
        rt_cfg = RuntimeConfig(observer_min_interval_s=0.005, status_ttl_s=0.0)
        client = ThreadTrackingMockClient(pos=(0.5, 64.0, 0.5))
        state = State(rt_cfg)
        ex = CountingExecutor(client, state)

        tick_done = threading.Event()
        orig_start = SafetyMonitor.start
        orig_tick = SafetyMonitor.tick

        def wrapped_start(mon):
            client.clear_trace()
            client.st["pos"] = [7.5, 64.0, 0.5]
            return orig_start(mon)

        def wrapped_tick(mon, *args, **kwargs):
            out = orig_tick(mon, *args, **kwargs)
            tick_done.set()
            return out

        select_observed: dict = {}

        def select_fn(call_idx: int, st: State, _fails):
            if call_idx == 1:
                select_observed["pre_select_main"] = client.main_calls()
                self.assertTrue(tick_done.wait(timeout=2.0), "后台 SafetyMonitor 未在 2s 内完成首轮 tick")
                select_observed["monitor_calls"] = client.monitor_calls()
                select_observed["pos"] = list(st.pos)
                return PlanDecision(skill=_MoveOnlySkill(), source="rule", reason="move_once")
            return PlanDecision(skill=None, source="idle", reason="done")

        planner = _SpyPlanner(select_fn)
        cfg = LoopConfig(
            enable_monitor=True,
            continuous_observe=True,
            async_observe_for_llm=True,
            max_steps=2,
            idle_sleep_s=0.0,
        )

        with patch.object(SafetyMonitor, "start", wrapped_start), patch.object(
            SafetyMonitor, "tick", wrapped_tick
        ):
            loop = MainLoop(client, state, ex, planner, config=cfg, log=lambda *_: None)
            res = loop.run()

        self.assertEqual(res.skills_completed, 1)
        self.assertEqual(res.stop_reason, "idle:done")
        self.assertNotIn("status", select_observed["pre_select_main"])
        self.assertNotIn("events", select_observed["pre_select_main"])
        self.assertIn("status", select_observed["monitor_calls"])
        self.assertEqual(select_observed["pos"], [7.5, 64.0, 0.5])

        # clean done=True 后同样不允许在 MainThread 上发 status/events/inventory
        final_main = client.main_calls()
        self.assertFalse(state.inventory_dirty)
        self.assertNotIn("status", final_main)
        self.assertNotIn("events", final_main)
        self.assertNotIn("inventory", final_main)

    def test_async_observe_syncs_only_dirty_inventory_before_select_and_after_skill_done(self):
        rt_cfg = RuntimeConfig(status_ttl_s=0.0)
        client = ThreadTrackingMockClient(pos=(0.5, 64.0, 0.5), blocks={(1, 64, 0): "oak_log"})
        state = State(rt_cfg)
        ex = CountingExecutor(client, state)

        pre_select_checked: dict = {}
        post_done_checked: dict = {}

        def on_start(st: State):
            self.assertTrue(st._status_booted)
            self.assertTrue(st._events_booted)
            st.invalidate_inventory()
            client.clear_trace()

        def on_skill_end(_skill, outcome: str, st: State, **_kwargs):
            post_done_checked["outcome"] = outcome
            post_done_checked["main_calls"] = client.main_calls()
            post_done_checked["dirty"] = st.inventory_dirty
            post_done_checked["oak_log"] = st.count("oak_log")

        def select_fn(call_idx: int, st: State, _fails):
            if call_idx == 1:
                pre_select_checked["main_calls"] = client.main_calls()
                pre_select_checked["dirty"] = st.inventory_dirty
                client.clear_trace()
                return PlanDecision(skill=_DigOnceSkill(), source="rule", reason="dig_once")
            return PlanDecision(skill=None, source="idle", reason="done")

        obs = _SpyObserver(on_start=on_start, on_skill_end=on_skill_end)
        planner = _SpyPlanner(select_fn)
        cfg = LoopConfig(
            enable_monitor=True,
            continuous_observe=True,
            async_observe_for_llm=True,
            max_steps=2,
            idle_sleep_s=0.0,
        )

        with patch.object(SafetyMonitor, "tick", return_value=None):
            loop = MainLoop(client, state, ex, planner, config=cfg, observer=obs, log=lambda *_: None)
            res = loop.run()

        self.assertEqual(res.skills_completed, 1)
        self.assertIn("inventory", pre_select_checked["main_calls"])
        self.assertNotIn("status", pre_select_checked["main_calls"])
        self.assertNotIn("events", pre_select_checked["main_calls"])
        self.assertFalse(pre_select_checked["dirty"])

        self.assertEqual(post_done_checked["outcome"], "completed")
        self.assertIn("inventory", post_done_checked["main_calls"])
        self.assertNotIn("status", post_done_checked["main_calls"])
        self.assertNotIn("events", post_done_checked["main_calls"])
        self.assertFalse(post_done_checked["dirty"])
        self.assertEqual(post_done_checked["oak_log"], 1)

    def test_async_observe_after_done_inventory_fatal_vs_nonfatal_error(self):
        for err_code, expect_raise in (("network", False), ("unauthorized", True)):
            with self.subTest(err_code=err_code):
                client = ThreadTrackingMockClient(blocks={(1, 64, 0): "oak_log"})
                state = State(RuntimeConfig(status_ttl_s=0.0))
                ex = CountingExecutor(client, state)

                def inject_inv_error():
                    client.inventory_errors.append(ApiError(err_code, f"injected {err_code}"))

                obs = _SpyObserver(on_start=lambda _st: client.clear_trace())
                planner = _SpyPlanner(
                    lambda n, _st, _f: (
                        PlanDecision(
                            skill=_DigOnceSkill(on_dug=inject_inv_error),
                            source="rule",
                            reason="dig_with_inv_err",
                        )
                        if n == 1
                        else PlanDecision(skill=None, source="idle", reason="done")
                    )
                )
                cfg = LoopConfig(
                    enable_monitor=True,
                    continuous_observe=True,
                    async_observe_for_llm=True,
                    max_steps=2,
                    idle_sleep_s=0.0,
                    transient_retries=0,
                )

                with patch.object(SafetyMonitor, "tick", return_value=None):
                    loop = MainLoop(client, state, ex, planner, config=cfg, observer=obs, log=lambda *_: None)
                    if expect_raise:
                        with self.assertRaises(ApiError) as ctx:
                            loop.run()
                        self.assertEqual(ctx.exception.code, "unauthorized")
                        self.assertEqual(ctx.exception.kind, FATAL)
                    else:
                        res = loop.run()
                        self.assertEqual(res.skills_completed, 1)
                        self.assertEqual(res.stop_reason, "idle:done")
                        main_calls = client.main_calls()
                        self.assertNotIn("status", main_calls)
                        self.assertNotIn("events", main_calls)

    def test_sync_fallback_when_monitor_disabled_async_disabled_or_not_booted(self):
        # Subtest A: enable_monitor=False
        with self.subTest(case="monitor_disabled"):
            client = ThreadTrackingMockClient()
            state = State(RuntimeConfig(status_ttl_s=0.0))
            ex = CountingExecutor(client, state)
            pre_calls: list[str] = []
            post_calls: list[str] = []

            def select_fn(call_idx: int, _st, _f):
                if call_idx == 1:
                    pre_calls.extend(client.main_calls())
                    client.clear_trace()
                    return PlanDecision(skill=_MoveOnlySkill(), source="rule", reason="move")
                return PlanDecision(skill=None, source="idle", reason="done")

            obs = _SpyObserver(
                on_start=lambda _st: client.clear_trace(),
                on_skill_end=lambda _sk, _out, _st, **_kw: post_calls.extend(client.main_calls()),
            )
            cfg = LoopConfig(enable_monitor=False, max_steps=2, idle_sleep_s=0.0)
            res = MainLoop(
                client, state, ex, _SpyPlanner(select_fn), config=cfg, observer=obs, log=lambda *_: None
            ).run()
            self.assertEqual(res.skills_completed, 1)
            self.assertIn("status", pre_calls)
            self.assertIn("events", pre_calls)
            self.assertIn("status", post_calls)
            self.assertIn("events", post_calls)

        # Subtest B: enable_monitor=True, async_observe_for_llm=False
        with self.subTest(case="async_disabled"):
            client = ThreadTrackingMockClient()
            state = State(RuntimeConfig(status_ttl_s=0.0))
            ex = CountingExecutor(client, state)
            pre_calls = []
            post_calls = []

            def select_fn(call_idx: int, _st, _f):
                if call_idx == 1:
                    pre_calls.extend(client.main_calls())
                    client.clear_trace()
                    return PlanDecision(skill=_MoveOnlySkill(), source="rule", reason="move")
                return PlanDecision(skill=None, source="idle", reason="done")

            obs = _SpyObserver(
                on_start=lambda _st: client.clear_trace(),
                on_skill_end=lambda _sk, _out, _st, **_kw: post_calls.extend(client.main_calls()),
            )
            cfg = LoopConfig(
                enable_monitor=True,
                continuous_observe=True,
                async_observe_for_llm=False,
                max_steps=2,
                idle_sleep_s=0.0,
            )
            with patch.object(SafetyMonitor, "tick", return_value=None):
                res = MainLoop(
                    client, state, ex, _SpyPlanner(select_fn), config=cfg, observer=obs, log=lambda *_: None
                ).run()
            self.assertEqual(res.skills_completed, 1)
            self.assertIn("status", pre_calls)
            self.assertIn("events", pre_calls)
            self.assertIn("status", post_calls)
            self.assertIn("events", post_calls)

        # Subtest C: startup drain raises non-fatal ApiError -> tick 1 sync fallback -> tick 2 async reuse
        with self.subTest(case="startup_events_not_booted"):
            client = ThreadTrackingMockClient()
            client.events_errors.append(ApiError("network", "startup drain failed"))
            state = State(RuntimeConfig(status_ttl_s=0.0))
            ex = CountingExecutor(client, state)
            tick_traces: dict[int, list[str]] = {}

            def on_start(st: State):
                self.assertFalse(st._events_booted)
                client.clear_trace()

            def select_fn(call_idx: int, st: State, _f):
                tick_traces[call_idx] = client.main_calls()
                self.assertTrue(st._events_booted)
                client.clear_trace()
                if call_idx <= 2:
                    return PlanDecision(skill=_MoveOnlySkill(), source="rule", reason=f"move_{call_idx}")
                return PlanDecision(skill=None, source="idle", reason="done")

            obs = _SpyObserver(on_start=on_start)
            cfg = LoopConfig(
                enable_monitor=True,
                continuous_observe=True,
                async_observe_for_llm=True,
                max_steps=3,
                idle_sleep_s=0.0,
            )
            with patch.object(SafetyMonitor, "tick", return_value=None):
                res = MainLoop(
                    client, state, ex, _SpyPlanner(select_fn), config=cfg, observer=obs, log=lambda *_: None
                ).run()

            self.assertEqual(res.skills_completed, 2)
            self.assertIn("status", tick_traces[1])
            self.assertIn("events", tick_traces[1])
            self.assertNotIn("status", tick_traces[2])
            self.assertNotIn("events", tick_traces[2])

    def test_async_observe_detects_death_recorded_by_monitor_on_cached_state(self):
        rt_cfg = RuntimeConfig(observer_min_interval_s=0.005, status_ttl_s=0.0)
        client = ThreadTrackingMockClient()
        state = State(rt_cfg)
        ex = CountingExecutor(client, state)

        death_tick_done = threading.Event()
        recover_entered = threading.Event()
        pre_recover_main: list[str] = []

        def status_override(tname: str, st: dict) -> dict:
            if tname == "mcagent-safety-monitor" and not recover_entered.is_set():
                return {**st, "alive": False, "ready": False, "health": 0}
            return {**st, "alive": True, "ready": True, "health": 20}

        client.status_override = status_override

        orig_start = SafetyMonitor.start
        orig_tick = SafetyMonitor.tick

        def wrapped_start(mon):
            client.clear_trace()
            return orig_start(mon)

        def wrapped_tick(mon, *args, **kwargs):
            out = orig_tick(mon, *args, **kwargs)
            if not state.st.get("alive", True):
                death_tick_done.set()
            return out

        def on_start(_st: State):
            self.assertTrue(death_tick_done.wait(timeout=2.0), "后台 SafetyMonitor 未在 2s 内记录死亡状态")

        obs = _SpyObserver(on_start=on_start)
        planner = _SpyPlanner(lambda _n, _st, _f: PlanDecision(skill=_MoveOnlySkill(), source="rule", reason="move"))
        cfg = LoopConfig(
            enable_monitor=True,
            continuous_observe=True,
            async_observe_for_llm=True,
            max_deaths=1,
            max_steps=1,
            idle_sleep_s=0.0,
            respawn_poll_s=0.001,
        )

        with patch.object(SafetyMonitor, "start", wrapped_start), patch.object(
            SafetyMonitor, "tick", wrapped_tick
        ):
            loop = MainLoop(client, state, ex, planner, config=cfg, observer=obs, log=lambda *_: None)
            orig_recover = loop._recover_guarded

            def spy_recover(pre_inv_fp, initial_err=None):
                pre_recover_main.extend(client.main_calls())
                recover_entered.set()
                return orig_recover(pre_inv_fp, initial_err=initial_err)

            loop._recover_guarded = spy_recover
            res = loop.run()

        self.assertEqual(res.deaths_recovered, 1)
        self.assertEqual(res.stop_reason, "max_deaths_reached")
        self.assertEqual(planner.calls, 0)
        self.assertNotIn("status", pre_recover_main)
        self.assertNotIn("events", pre_recover_main)

    def test_stop_episode_decision_exits_immediately_with_goal_stopped_out(self):
        client = ThreadTrackingMockClient()
        state = State()
        ex = CountingExecutor(client, state)
        def make_stop_decision(_n, _st, _f):
            dec = PlanDecision(skill=None, source="idle", reason="单目标止损")
            dec.stop_episode = True  # 兼容 policy.py 合入前后的 PlanDecision 结构
            return dec

        planner = _SpyPlanner(make_stop_decision)
        cfg = LoopConfig(
            idle_sleep_s=5.0,
            max_seconds=60.0,
            enable_monitor=False,
            target_goal="mine_stone",
        )

        def fail_if_idle_sleep(secs: float) -> None:
            if secs >= 1.0:
                raise AssertionError(f"stop_episode=True 时不应休眠 idle_sleep_s ({secs}s)")

        with tempfile.TemporaryDirectory() as tmp:
            obs = SoakObserver(tmp, target_goal="mine_stone")
            with patch.object(obs, "on_progress_tick", wraps=obs.on_progress_tick) as tick_spy, patch(
                "mcagent.loop.engine.time.sleep", side_effect=fail_if_idle_sleep
            ) as sleep_mock:
                loop = MainLoop(
                    client,
                    state,
                    ex,
                    planner,
                    config=cfg,
                    observer=obs,
                    log=lambda *_: None,
                    sleep=fail_if_idle_sleep,
                )
                res = loop.run()

            self.assertEqual(res.stop_reason, "goal_stopped_out")
            self.assertEqual(planner.calls, 1)
            self.assertFalse(any(c.args and c.args[0] == 5.0 for c in sleep_mock.call_args_list))
            self.assertGreaterEqual(tick_spy.call_count, 1)

            self.assertTrue(obs.summary_path.exists())
            summary_text = obs.summary_path.read_text(encoding="utf-8")
            self.assertIn("GOAL_MISS", summary_text)

            goal_misses = [x for x in obs._incidents.values() if x["code"] == "GOAL_MISS"]
            self.assertEqual(len(goal_misses), 1)
            self.assertEqual(goal_misses[0]["detail"]["stop_reason"], "goal_stopped_out")
            inc_data = json.loads(Path(goal_misses[0]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(inc_data["detail"]["stop_reason"], "goal_stopped_out")


if __name__ == "__main__":
    unittest.main()
