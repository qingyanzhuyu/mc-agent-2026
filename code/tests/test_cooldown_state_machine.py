"""冷却上限、同位冷却空转 3 层脱困状态机与 EnvBlocked 同格外迁上限离线单测（verified: offline）。

全链路注入 FakeClock / StubClient / StubExecutor，并 patch `time.sleep`、`time.monotonic` 与
`mcagent.skills.base.time.time`，确保零真实墙钟睡眠、100% 确定性。
"""
from __future__ import annotations

import dataclasses
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.loop import CampaignPlanner, GoalSpec, LoopConfig, MainLoop, RulePlanner, reset_world_memo
from mcagent.runtime import EnvBlocked, Replan, RuntimeConfig, State
from mcagent.skills import Param, Skill
from mcagent.skills import base as skill_base
from mcagent.skills.base import rec


class _FakeClock:
    """确定性虚拟时钟：记录所有 sleep 时长并同步推进时间，零真实睡眠。"""

    def __init__(self, initial: float = 1000.0) -> None:
        self.now = float(initial)
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        s = float(seconds)
        self.sleeps.append(round(s, 4))
        self.now += s

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)

    def __call__(self) -> float:
        return self.now


class _StubClient:
    """离线最小客户端夹具。"""

    def __init__(self, pos: tuple[float, float, float] = (0.0, 64.0, 0.0)) -> None:
        self.pos: list[float] = [float(pos[0]), float(pos[1]), float(pos[2])]
        self.dimension: str = "overworld"
        self.items: dict[str, int] = {}
        self.alive: bool = True
        self.ready: bool = True

    def wait_ready(self, timeout: float = 120.0) -> dict:
        return self.status()

    def status(self) -> dict:
        return {
            "ready": self.ready,
            "alive": self.alive,
            "health": 20.0,
            "food": 20.0,
            "oxygen": 300.0,
            "dimension": self.dimension,
            "is_day": True,
            "pos": list(self.pos),
        }

    def inventory(self) -> dict:
        return {
            "items": [
                {"name": name, "count": count, "slot": idx}
                for idx, (name, count) in enumerate(self.items.items())
                if count > 0
            ]
        }

    def events(self, since: int = 0, limit: int = 200) -> dict:
        return {"events": [], "last_seq": since, "has_more": False}

    def advancements(self, **kwargs) -> dict:
        return {"advancements": []}

    def disconnect(self) -> None:
        pass


class _StubExecutor:
    def __init__(self, client: _StubClient) -> None:
        self.client = client


def _make_state(pos: tuple[float, float, float] = (0.0, 64.0, 0.0)) -> State:
    cfg = dataclasses.replace(RuntimeConfig.load(), status_ttl_s=0.0)
    st = State(cfg)
    st.set_status({
        "ready": True,
        "alive": True,
        "health": 20.0,
        "food": 20.0,
        "oxygen": 300.0,
        "dimension": "overworld",
        "is_day": True,
        "pos": list(pos),
    })
    st.set_inventory({"items": []})
    return st


def _patch_clock(clock: _FakeClock) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch("time.sleep", side_effect=clock.sleep))
    stack.enter_context(patch("time.monotonic", side_effect=clock))
    stack.enter_context(patch("mcagent.skills.base.time.time", side_effect=clock))
    return stack


class _TwoGoalSwitchPlanner(CampaignPlanner):
    """初始仅暴露 goal_a；goal_a 被 abandon_goal 后立即解锁 goal_b。"""

    def __init__(self, *, goal_a: GoalSpec, goal_b: GoalSpec, **kwargs) -> None:
        super().__init__(ledger=(goal_a, goal_b), **kwargs)
        self._goal_a = goal_a
        self._goal_b = goal_b
        self.rules.active_goals = (goal_a,)

    def abandon_goal(self, goal_id: str | None) -> bool:
        ok = super().abandon_goal(goal_id)
        if ok and goal_id == self._goal_a.id:
            self.rules.active_goals = (self._goal_a, self._goal_b)
        return ok


class CooldownStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        self.addCleanup(skill_base._COOLDOWN.clear)
        self.addCleanup(reset_world_memo)

    # ------------------------------------------------------------------
    # 1. failure_cooldown_cap_s 封顶与短技能 1.5x 退避
    # ------------------------------------------------------------------
    def test_failure_cooldown_cap_for_long_and_short_skills(self) -> None:
        for error_code, skill_cd, expected_seq in (
            ("path_not_found", 30.0, [8.0, 8.0, 8.0]),
            ("stalled", 4.0, [4.0, 4.0, 6.0]),
        ):
            with self.subTest(error_code=error_code, cooldown_s=skill_cd):
                skill_base._COOLDOWN.clear()
                clock = _FakeClock()
                client = _StubClient()
                state = _make_state()
                ex = _StubExecutor(client)

                run_count = {"n": 0}
                observed_delays: list[float] = []

                class _FlakySkill(Skill):
                    name = "flaky_skill"
                    goal = f"act:{error_code}"
                    cooldown_s = skill_cd

                    def run(self, _ex, _state):
                        run_count["n"] += 1
                        yield rec("probe", {"attempt": run_count["n"]})
                        raise Replan("blocked") from ApiError(error_code, f"mock {error_code}")

                goal = GoalSpec(
                    id="goal_cap",
                    title="测试冷却封顶",
                    tier="T1",
                    score=10,
                    skill="flaky_skill",
                    done_items=("emerald>=1",),
                    stage="graduated",
                )
                reg = {"flaky_skill": _FlakySkill}
                planner = CampaignPlanner(mode="campaign", strategy="legacy", registry=reg, ledger=(goal,))

                orig_select = planner.select

                def _select_and_expire(st, recent_failures):
                    # 每次上轮技能刚 mark_failed 后进入下一拍 select，先记录刚写入的冷却时长，再推进时钟让冷却到期
                    if goal_key in skill_base._COOLDOWN and len(observed_delays) < 3:
                        observed_delays.append(round(skill_base._COOLDOWN[goal_key] - clock.now, 4))
                        clock.advance(50.0)
                    return orig_select(st, recent_failures)

                goal_key = f"act:{error_code}"
                planner.select = _select_and_expire  # type: ignore[method-assign]

                cfg = LoopConfig(
                    max_steps=4,
                    idle_sleep_s=1.0,
                    enable_monitor=False,
                    failure_cooldown_cap_s=8.0,
                )
                loop = MainLoop(client, state, ex, planner, config=cfg, log=lambda *_: None,
                                sleep=clock.sleep, clock=clock)
                with _patch_clock(clock):
                    res = loop.run()

                self.assertEqual(res.stop_reason, "max_steps_reached")
                self.assertEqual(loop._path_failures.get(goal_key), 3)
                self.assertEqual(observed_delays, expected_seq)

    # ------------------------------------------------------------------
    # 2. 同位冷却空转 3 层状态机：local_retry -> relocate -> goal_abandoned -> goal_b
    # ------------------------------------------------------------------
    def test_same_position_idle_spin_escalates_three_tiers_and_completes_goal_b(self) -> None:
        clock = _FakeClock()
        client = _StubClient(pos=(0.0, 64.0, 0.0))
        state = _make_state(pos=(0.0, 64.0, 0.0))
        ex = _StubExecutor(client)

        counts = {"fail_a": 0, "scout": 0, "win_b": 0}

        class _FailA(Skill):
            name = "fail_a"
            goal = "act:a"
            cooldown_s = 30.0

            def run(self, _ex, _state):
                counts["fail_a"] += 1
                yield rec("try_a", {"n": counts["fail_a"]})
                raise Replan("stalled") from ApiError("stalled", "no target")

        class _ScoutStub(Skill):
            name = "scout_island"
            goal = "explore:island"
            cooldown_s = 30.0
            params = {"radius": Param(int, "半径", default=36)}

            def run(self, _ex, _state):
                counts["scout"] += 1
                yield rec("scout", {"n": counts["scout"]})
                raise Replan("cannot relocate")

        class _WinB(Skill):
            name = "win_b"
            goal = "act:b"
            cooldown_s = 10.0

            def run(self, _ex, st):
                counts["win_b"] += 1
                _ex.client.items["diamond"] = 1
                st.invalidate_inventory()
                st.refresh_inventory(_ex.client)
                yield rec("win_b", {"ok": True})

        goal_a = GoalSpec(
            id="goal_a", title="受阻目标 A", tier="T1", score=10,
            skill="fail_a", done_items=("emerald>=1",), stage="graduated",
        )
        goal_b = GoalSpec(
            id="goal_b", title="备选目标 B", tier="T1", score=5,
            skill="win_b", done_items=("diamond>=1",), stage="graduated",
        )
        reg = {"fail_a": _FailA, "scout_island": _ScoutStub, "win_b": _WinB}
        planner = _TwoGoalSwitchPlanner(goal_a=goal_a, goal_b=goal_b, mode="campaign", strategy="legacy", registry=reg)

        # scout_island 初始已在冷却中，触发 goal_a 失败后立即进入同位冷却空转
        skill_base._COOLDOWN["explore:island"] = clock.now + 30.0

        cfg = LoopConfig(
            idle_sleep_s=1.0,
            max_seconds=60.0,
            target_goal="goal_b",
            stop_on_goal=True,
            enable_monitor=False,
        )
        loop = MainLoop(client, state, ex, planner, config=cfg, log=lambda *_: None,
                        sleep=clock.sleep, clock=clock)
        with _patch_clock(clock):
            res = loop.run()

        self.assertEqual(res.stop_reason, "goal_reached")
        self.assertEqual(loop.failure_escalations, ["local_retry", "relocate", "goal_abandoned"])
        self.assertIn("goal_a", planner.rules._abandoned_goals)
        self.assertGreaterEqual(counts["fail_a"], 2)  # 初次 + Tier 1 local_retry 后至少重试一次
        self.assertEqual(counts["scout"], 1)           # Tier 2 同格只执行一次外迁，绝不重复外迁死循环
        self.assertEqual(counts["win_b"], 1)
        self.assertGreaterEqual(state.count("diamond"), 1)
        self.assertLess(clock.total_slept, 10.0)       # 绝不硬睡 30s / 76.5s

    def test_tier2_relocate_falls_back_to_emergency_escape_when_no_scout(self) -> None:
        clock = _FakeClock()
        client = _StubClient(pos=(0.0, 64.0, 0.0))
        state = _make_state(pos=(0.0, 64.0, 0.0))
        ex = _StubExecutor(client)

        counts = {"fail_a": 0, "escape": 0, "win_b": 0}

        class _FailA(Skill):
            name = "fail_a"
            goal = "act:a"
            cooldown_s = 30.0

            def run(self, _ex, _state):
                counts["fail_a"] += 1
                yield rec("try_a", {"n": counts["fail_a"]})
                raise Replan("stalled")

        class _EscapeStub(Skill):
            name = "emergency_escape"
            goal = "safety:escape"
            cooldown_s = 30.0
            params = {"intent": Param(str, "意图", default="flee")}

            def run(self, _ex, _state):
                counts["escape"] += 1
                yield rec("escape", {"n": counts["escape"]})
                # 未能移动 >= 2.0 格就结束，应保留 Tier 2 状态并在冷却空转后进入 Tier 3

        class _WinB(Skill):
            name = "win_b"
            goal = "act:b"

            def run(self, _ex, st):
                counts["win_b"] += 1
                _ex.client.items["diamond"] = 1
                st.invalidate_inventory()
                st.refresh_inventory(_ex.client)
                yield rec("win_b", {"ok": True})

        goal_a = GoalSpec(
            id="goal_a", title="受阻目标 A", tier="T1", score=10,
            skill="fail_a", done_items=("emerald>=1",), stage="graduated",
        )
        goal_b = GoalSpec(
            id="goal_b", title="备选目标 B", tier="T1", score=5,
            skill="win_b", done_items=("diamond>=1",), stage="graduated",
        )
        reg = {"fail_a": _FailA, "emergency_escape": _EscapeStub, "win_b": _WinB}
        planner = _TwoGoalSwitchPlanner(goal_a=goal_a, goal_b=goal_b, mode="campaign", strategy="legacy", registry=reg)

        cfg = LoopConfig(
            idle_sleep_s=1.0,
            max_seconds=60.0,
            target_goal="goal_b",
            stop_on_goal=True,
            enable_monitor=False,
        )
        loop = MainLoop(client, state, ex, planner, config=cfg, log=lambda *_: None,
                        sleep=clock.sleep, clock=clock)
        with _patch_clock(clock):
            res = loop.run()

        self.assertEqual(res.stop_reason, "goal_reached")
        self.assertEqual(loop.failure_escalations, ["local_retry", "relocate", "goal_abandoned"])
        self.assertEqual(counts["escape"], 1)
        self.assertEqual(counts["win_b"], 1)
        self.assertLess(clock.total_slept, 10.0)

    # ------------------------------------------------------------------
    # 3. 单聚焦目标触达 Tier 3 goal_abandoned 时干净退出 goal_stopped_out
    # ------------------------------------------------------------------
    def test_single_focused_goal_abandoned_exits_with_goal_stopped_out(self) -> None:
        for strategy in ("legacy", "rule"):
            with self.subTest(strategy=strategy):
                skill_base._COOLDOWN.clear()
                reset_world_memo()
                clock = _FakeClock()
                client = _StubClient(pos=(0.0, 64.0, 0.0))
                state = _make_state(pos=(0.0, 64.0, 0.0))
                ex = _StubExecutor(client)
                scout_runs = {"n": 0}

                class _FailA(Skill):
                    name = "fail_a"
                    goal = "act:a"
                    cooldown_s = 30.0

                    def run(self, _ex, _state):
                        yield rec("try_a")
                        raise Replan("stuck") from ApiError("path_not_found", "no path")

                class _ScoutFail(Skill):
                    name = "scout_island"
                    goal = "explore:island"
                    cooldown_s = 30.0
                    params = {"radius": Param(int, "半径", default=36)}

                    def run(self, _ex, _state):
                        scout_runs["n"] += 1
                        yield rec("scout")
                        raise Replan("blocked")

                goal_a = GoalSpec(
                    id="goal_a", title="单聚焦目标 A", tier="T1", score=10,
                    skill="fail_a", done_items=("emerald>=1",), stage="graduated",
                )
                reg = {"fail_a": _FailA, "scout_island": _ScoutFail}
                planner = CampaignPlanner(
                    mode="campaign", strategy=strategy, target_goal="goal_a", registry=reg, ledger=(goal_a,)
                )
                skill_base._COOLDOWN["explore:island"] = clock.now + 30.0

                cfg = LoopConfig(
                    idle_sleep_s=1.0,
                    max_seconds=60.0,
                    target_goal="goal_a",
                    stop_on_goal=True,
                    enable_monitor=False,
                )
                loop = MainLoop(client, state, ex, planner, config=cfg, log=lambda *_: None,
                                sleep=clock.sleep, clock=clock)
                with _patch_clock(clock):
                    res = loop.run()

                self.assertEqual(res.stop_reason, "goal_stopped_out")
                self.assertEqual(loop.failure_escalations, ["local_retry", "relocate", "goal_abandoned"])
                self.assertEqual(scout_runs["n"], 1)
                self.assertIn("goal_a", planner.rules._abandoned_goals)
                self.assertLess(clock.total_slept, 10.0)

    # ------------------------------------------------------------------
    # 4. safe_mode 驻留空转不触发同位冷却空转状态机
    # ------------------------------------------------------------------
    def test_safe_mode_idle_does_not_trigger_cooldown_spin(self) -> None:
        clock = _FakeClock()
        client = _StubClient(pos=(0.0, 64.0, 0.0))
        state = _make_state(pos=(0.0, 64.0, 0.0))
        state.safe_mode_until = clock.now + 30.0
        ex = _StubExecutor(client)

        class _NeverRun(Skill):
            name = "never_run"
            goal = "act:never"
            cooldown_s = 30.0

            def run(self, _ex, _state):
                raise AssertionError("safe_mode 期间不应执行普通目标技能")

        goal_a = GoalSpec(
            id="goal_a", title="目标 A", tier="T1", score=10,
            skill="never_run", done_items=("emerald>=1",), stage="graduated",
        )
        reg = {"never_run": _NeverRun}
        planner = CampaignPlanner(mode="campaign", strategy="rule", target_goal="goal_a", registry=reg, ledger=(goal_a,))

        cfg = LoopConfig(
            idle_sleep_s=1.0,
            max_seconds=6.0,
            target_goal="goal_a",
            stop_on_goal=True,
            enable_monitor=False,
        )
        loop = MainLoop(client, state, ex, planner, config=cfg, log=lambda *_: None,
                        sleep=clock.sleep, clock=clock)
        with _patch_clock(clock):
            res = loop.run()

        self.assertEqual(res.stop_reason, "max_seconds_reached")
        self.assertGreaterEqual(len(clock.sleeps), 5)
        self.assertEqual(loop.failure_escalations, [])
        self.assertEqual(loop.failure_tier_state, "normal")
        self.assertEqual(loop._idle_spin_count, 0)

    # ------------------------------------------------------------------
    # 5. 移动 >= 2.0 格后 failure_tier_state 重置回 normal
    # ------------------------------------------------------------------
    def test_moving_at_least_two_blocks_resets_failure_tier_to_normal(self) -> None:
        # 场景 A：冷却空转升至 Tier 1 (local_retry) 后，在下一拍冷却空转前位置移动 >= 2.0 格，状态机重置回 normal
        clock = _FakeClock()
        client = _StubClient(pos=(0.0, 64.0, 0.0))
        state = _make_state(pos=(0.0, 64.0, 0.0))
        ex = _StubExecutor(client)

        class _FailUntilMoved(Skill):
            name = "fail_until_moved"
            goal = "act:move_check"
            cooldown_s = 30.0

            def run(self, _ex, st):
                if st.pos[0] < 2.0:
                    yield rec("try")
                    raise Replan("stuck at origin")
                _ex.client.items["emerald"] = 1
                st.invalidate_inventory()
                st.refresh_inventory(_ex.client)
                yield rec("done")

        class _ScoutNoMove(Skill):
            name = "scout_island"
            goal = "explore:island"
            cooldown_s = 30.0
            params = {"radius": Param(int, "半径", default=36)}

            def run(self, _ex, _state):
                yield rec("scout")
                raise Replan("scout blocked")

        goal = GoalSpec(
            id="goal_move", title="移动复位测试", tier="T1", score=10,
            skill="fail_until_moved", done_items=("emerald>=1",), stage="graduated",
        )
        reg = {"fail_until_moved": _FailUntilMoved, "scout_island": _ScoutNoMove}
        planner = CampaignPlanner(mode="campaign", strategy="legacy", target_goal="goal_move", registry=reg, ledger=(goal,))
        skill_base._COOLDOWN["explore:island"] = clock.now + 30.0

        observed_states_on_sleep: list[str] = []
        moved_flag = {"done": False}
        loop_ref: dict[str, MainLoop] = {}

        def _sleep_and_move(s: float) -> None:
            clock.sleep(s)
            lp = loop_ref["loop"]
            observed_states_on_sleep.append(lp.failure_tier_state)
            # 在进入 local_retry 之后的下一次空闲睡眠时模拟玩家走位 >= 2.0 格
            if lp.failure_tier_state == "local_retry" and not moved_flag["done"]:
                moved_flag["done"] = True
                client.pos = [2.5, 64.0, 0.0]

        cfg = LoopConfig(
            idle_sleep_s=1.0,
            max_seconds=120.0,
            target_goal="goal_move",
            stop_on_goal=True,
            enable_monitor=False,
        )
        loop = MainLoop(client, state, ex, planner, config=cfg, log=lambda *_: None,
                        sleep=_sleep_and_move, clock=clock)
        loop_ref["loop"] = loop

        orig_select = planner.select

        def _select_and_expire_on_moved(st, recent_failures):
            # 当已在 2.5 格处触发重置为 normal 后，立即快进冷却让技能在新位置完成目标
            if moved_flag["done"] and loop.failure_tier_state == "normal" and lp_ready_to_finish["go"]:
                clock.advance(40.0)
            return orig_select(st, recent_failures)

        lp_ready_to_finish = {"go": False}

        def _track_reset(pos=None):
            if moved_flag["done"]:
                lp_ready_to_finish["go"] = True
            return MainLoop._reset_failure_tier(loop, pos)

        loop._reset_failure_tier = _track_reset  # type: ignore[method-assign]
        planner.select = _select_and_expire_on_moved  # type: ignore[method-assign]

        with _patch_clock(clock):
            res = loop.run()

        self.assertEqual(res.stop_reason, "goal_reached")
        self.assertIn("local_retry", observed_states_on_sleep)
        self.assertEqual(loop.failure_tier_state, "normal")
        self.assertEqual(loop._failure_tier, 0)
        self.assertTrue(lp_ready_to_finish["go"])

    def test_tier2_relocate_moving_two_blocks_resets_failure_tier_to_normal(self) -> None:
        # 场景 B：Tier 2 relocate 派发 scout_island 成功移动 >= 2.0 格后，failure_tier_state 立即重置回 normal
        clock = _FakeClock()
        client = _StubClient(pos=(0.0, 64.0, 0.0))
        state = _make_state(pos=(0.0, 64.0, 0.0))
        ex = _StubExecutor(client)

        class _FailOriginWinMoved(Skill):
            name = "fail_origin"
            goal = "act:origin"
            cooldown_s = 30.0

            def run(self, _ex, st):
                if st.pos[0] < 2.0:
                    yield rec("try")
                    raise Replan("stuck")
                _ex.client.items["emerald"] = 1
                st.invalidate_inventory()
                st.refresh_inventory(_ex.client)
                yield rec("done")

        class _ScoutMovesPlayer(Skill):
            name = "scout_island"
            goal = "explore:island"
            cooldown_s = 30.0
            params = {"radius": Param(int, "半径", default=36)}

            def run(self, _ex, st):
                _ex.client.pos = [3.0, 64.0, 0.0]
                yield rec("move", pos=[3.0, 64.0, 0.0])

        goal = GoalSpec(
            id="goal_reloc", title="外迁复位测试", tier="T1", score=10,
            skill="fail_origin", done_items=("emerald>=1",), stage="graduated",
        )
        reg = {"fail_origin": _FailOriginWinMoved, "scout_island": _ScoutMovesPlayer}
        planner = CampaignPlanner(mode="campaign", strategy="legacy", target_goal="goal_reloc", registry=reg, ledger=(goal,))
        skill_base._COOLDOWN["explore:island"] = clock.now + 30.0

        cfg = LoopConfig(
            idle_sleep_s=1.0,
            max_seconds=30.0,
            target_goal="goal_reloc",
            stop_on_goal=True,
            enable_monitor=False,
        )
        loop = MainLoop(client, state, ex, planner, config=cfg, log=lambda *_: None,
                        sleep=clock.sleep, clock=clock)
        with _patch_clock(clock):
            res = loop.run()

        self.assertEqual(res.stop_reason, "goal_reached")
        self.assertEqual(loop.failure_escalations, ["local_retry", "relocate"])
        self.assertEqual(loop.failure_tier_state, "normal")
        self.assertEqual(loop._failure_tier, 0)
        self.assertGreaterEqual(state.count("emerald"), 1)

    # ------------------------------------------------------------------
    # 6. EnvBlocked 同格环境恢复外迁上限 (_max_env_recovery_per_cell)
    # ------------------------------------------------------------------
    def test_env_blocked_same_cell_recovery_cap_stops_scout_loop(self) -> None:
        class _FindOre(Skill):
            name = "find_ore"
            goal = "acq:iron_ingot"

            def run(self, _ex, _state):
                return iter(())

        class _ScoutStub(Skill):
            name = "scout_island"
            goal = "explore:island"
            params = {"radius": Param(int, "半径", default=36)}

            def run(self, _ex, _state):
                return iter(())

        goal = GoalSpec(
            id="iron", title="找铁", tier="T1", score=10,
            skill="find_ore", done_items=("iron_ingot>=1",), stage="graduated",
        )
        reg = {"find_ore": _FindOre, "scout_island": _ScoutStub}
        planner = RulePlanner(reg, ledger=(goal,), target_goal="iron")
        state = _make_state(pos=(1.0, 64.0, 1.0))

        err = EnvBlocked("暂无裸露铁矿", missing_predicate="exposed_iron", searched_volume="半径 32", wake_on="chunk_moved")
        planner.record_environment_block(_FindOre(), err, state)

        cap = planner._max_env_recovery_per_cell
        self.assertGreater(cap, 0)

        for i in range(cap):
            dec = planner.choose(state)
            self.assertIsNotNone(dec.skill, msg=f"第 {i + 1} 次同格环境恢复应派发 scout_island")
            self.assertEqual(dec.skill.name, "scout_island")
            self.assertTrue(dec.environment_recovery)

        # 达到同格上限后，必须停止在同格继续循环派发 scout_island
        for _ in range(2):
            capped_dec = planner.choose(state)
            self.assertIsNone(capped_dec.skill)
            self.assertEqual(capped_dec.source, "idle")
            self.assertFalse(capped_dec.environment_recovery)

        # 换到新区块后：chunk_moved 挂起被唤醒，若再次挂起则新区块拥有独立恢复预算
        state.set_status({"dimension": "overworld", "pos": [17.0, 64.0, 1.0]})
        self.assertEqual(planner.blocked_targets(state, "iron"), {})
        planner.record_environment_block(_FindOre(), err, state)
        fresh_dec = planner.choose(state)
        self.assertIsNotNone(fresh_dec.skill)
        self.assertEqual(fresh_dec.skill.name, "scout_island")
        self.assertTrue(fresh_dec.environment_recovery)


if __name__ == "__main__":
    unittest.main()
