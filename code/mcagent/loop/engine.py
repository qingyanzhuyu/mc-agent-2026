"""统一比赛主循环（兼顾 8 小时正式赛与赛前持续挂机压测）。

设计原则（评审修订 #1 / #4）：
- 测试循环就是正式赛主循环：`登录就绪 -> 选技能 -> 一步一动作推进 -> 死亡/断线自愈 -> 继续`，不维护两套代码。
- 死亡重生后不主动发 `POST /posture`（规范 §4.11 服务端自动复位，`Client.status()` 同步本地缓存）。
- 重生后的阶段衔接完全交给 `CampaignPlanner` 从刷新后的真实背包与环境推导。
- `SoakObserver` 通过观察者接口挂接，关闭时零额外开销，开启时实时产出流水、异常包与进度曲线。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from ..client import FATAL, ApiError, Client
from ..runtime import EnvBlocked, Executor, Fatal, Replan, Safety, SafetyMonitor, State, _inv_fingerprint
from ..skills.base import Skill, failure
from .auditor import SoakObserver
from .goals import GOAL_MAP, GoalSpec, is_goal_achieved, normalize_goal_ids
from .policy import CampaignPlanner


@dataclass
class LoopConfig:
    mode: str = "campaign"
    max_steps: int = 0            # 0 = 不限步数（按时间或信号停止）
    max_seconds: float = 0.0      # 0 = 不限时（正式赛可设 28800 = 8h）
    max_deaths: int = 0           # 0 = 不限死亡次数（持续自愈重来）
    idle_sleep_s: float = 1.0
    respawn_poll_s: float = 1.0
    respawn_timeout_s: float = 90.0
    enable_monitor: bool = True
    disconnect_on_exit: bool = False
    target_goal: Optional[str] = None
    stop_on_goal: bool = False
    # 读状态遇到网关抖动（5xx / 网络 / 超时 / 服务端 internal 等非致命错误）时的自愈参数
    transient_retries: int = 3         # 单拍内重试次数，用尽则跳过这一拍
    transient_backoff_s: float = 1.0   # 首次退避秒数，此后翻倍
    transient_backoff_max_s: float = 15.0
    outage_limit_s: float = 600.0      # 连续故障超过此时长才停机；0 = 永不因此停机
    continuous_observe: bool = True
    async_observe_for_llm: bool = True


# 会话级错误：交给 `_recover_session_or_respawn` 处理，而不是按瞬时故障退避
SESSION_CODES: frozenset[str] = frozenset({"dead", "not_connected", "wrong_server", "death_loop_cooldown"})


@dataclass
class RunResult:
    steps: int
    skills_completed: int
    skills_replan: int
    deaths_recovered: int
    elapsed_s: float
    stop_reason: str
    transient_faults: int = 0          # 因读状态瞬时故障而跳过的拍数


class MainLoop:
    """比赛与持续测试共用的核心控制循环。"""

    def __init__(
        self,
        client: Client,
        state: State,
        executor: Executor,
        planner: CampaignPlanner,
        *,
        config: Optional[LoopConfig] = None,
        observer: Optional[SoakObserver] = None,
        safety: Optional[Safety] = None,
        log=print,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.state = state
        self.executor = executor
        self.planner = planner
        self.config = config or LoopConfig(mode=planner.mode)
        self.observer = observer
        self.safety = safety or Safety(state.config)
        self.log = log
        self.recent_failures: deque[str] = deque(maxlen=12)
        self._path_failures: dict[str, int] = {}
        self._seen_deaths_len = 0
        self._server_advancements: set[str] = set()
        self._sleep = sleep
        self._clock = clock
        self._outage_since: Optional[float] = None
        self.transient_faults = 0

    def _resolve_goal_spec(self, gid: str) -> Optional[GoalSpec]:
        if hasattr(self.planner, "rules") and hasattr(self.planner.rules, "ledger_map"):
            g = self.planner.rules.ledger_map.get(gid)
            if g is not None:
                return g
        if self.observer is not None and hasattr(self.observer, "_ledger_map"):
            g = self.observer._ledger_map.get(gid)
            if g is not None:
                return g
        return GOAL_MAP.get(gid)

    def _resolve_target_goal_specs(self) -> list[GoalSpec]:
        """本轮聚焦的全部目标（一个或多个，config.target_goal 可写成 "a,b"）。"""
        specs = [self._resolve_goal_spec(gid) for gid in normalize_goal_ids(self.config.target_goal)]
        return [g for g in specs if g is not None]

    def _sync_and_check_target_goal(self) -> bool:
        completed_advs = set(self._server_advancements)
        if self.observer is not None:
            completed_advs.update(self.observer.server_advancements)
            self._server_advancements.update(completed_advs)
        if hasattr(self.planner, "rules") and hasattr(self.planner.rules, "completed_advancements"):
            self.planner.rules.completed_advancements.update(completed_advs)
        if self.observer is not None:
            self.observer.update_goals_reached(self.state)
        if self.config.target_goal:
            specs = self._resolve_target_goal_specs()
            return bool(specs) and all(is_goal_achieved(g, self.state, completed_advs) for g in specs)
        return False

    def _budget_exceeded(self, started_mono: float, steps: int, deaths: int) -> Optional[str]:
        if self.config.max_steps > 0 and steps >= self.config.max_steps:
            return "max_steps_reached"
        if self.config.max_seconds > 0 and (time.monotonic() - started_mono) >= self.config.max_seconds:
            return "max_seconds_reached"
        if self.config.max_deaths > 0 and deaths >= self.config.max_deaths:
            return "max_deaths_reached"
        return None

    def _refresh_world_state(self, *, inventory_only: bool = False) -> None:
        if not inventory_only:
            self.state.refresh_status(self.client)
            self.state.drain(self.client)
        if self.state.inventory_dirty or not self.state.inventory:
            self.state.refresh_inventory(self.client)

    def _use_async_observation(self, monitor: Optional[SafetyMonitor]) -> bool:
        return bool(
            monitor is not None
            and self.config.async_observe_for_llm
            and self.state._status_booted
            and self.state._events_booted
        )

    # ---- 瞬时故障自愈 -------------------------------------------------
    def _backoff_s(self, attempt: int) -> float:
        base = max(0.0, self.config.transient_backoff_s)
        return min(max(base, self.config.transient_backoff_max_s), base * (2 ** attempt))

    def _note_outage(self, e: ApiError) -> None:
        if self._outage_since is None:
            self._outage_since = self._clock()
        self.recent_failures.append(f"loop: 读状态瞬时故障 {e}")

    def _outage_exceeded(self) -> bool:
        limit = self.config.outage_limit_s
        return bool(limit > 0 and self._outage_since is not None
                    and self._clock() - self._outage_since >= limit)

    def _refresh_with_backoff(self, *, inventory_only: bool = False) -> bool:
        """刷新状态；非致命、非会话类错误按指数退避重试。

        返回 True 表示刷新成功；False 表示重试用尽、本拍应跳过。
        会话类错误（`SESSION_CODES`）与致命错误原样抛出，由调用方处理。
        """
        for attempt in range(max(0, self.config.transient_retries) + 1):
            try:
                self._refresh_world_state(inventory_only=inventory_only)
            except ApiError as e:
                if e.kind == FATAL or e.code in SESSION_CODES:
                    raise
                self._note_outage(e)
                if attempt >= self.config.transient_retries:
                    self.transient_faults += 1
                    self.log(f"[loop] 读状态连续失败 {attempt + 1} 次（{e}），跳过本拍")
                    return False
                wait_s = self._backoff_s(attempt)
                self.log(f"[loop] 读状态瞬时故障（{e}），{wait_s:.1f}s 后重试 ({attempt + 1}/{self.config.transient_retries})")
                self._sleep(wait_s)
                continue
            self._outage_since = None
            return True
        return False

    def _recover_guarded(self, pre_inv_fp: dict[str, int], initial_err: Optional[ApiError] = None) -> bool:
        """会话/重生恢复；恢复本身遇到非致命错误时不让进程退出，下一拍再试。"""
        try:
            self._recover_session_or_respawn(pre_inv_fp, initial_err=initial_err)
        except ApiError as e:
            if e.kind == FATAL:
                raise
            self._note_outage(e)
            self.transient_faults += 1
            wait_s = self._backoff_s(self.config.transient_retries)
            self.log(f"[loop] 会话恢复未完成（{e}），{wait_s:.1f}s 后下一拍重试")
            self._sleep(wait_s)
            return False
        self._outage_since = None
        return True

    def _wait_ready_guarded(self) -> Optional[dict]:
        """启动时等 ready；网关不可用时退避重试，超过停机上限返回 None。"""
        attempt = 0
        while True:
            try:
                st = self.client.wait_ready(timeout=120)
            except ApiError as e:
                if e.kind == FATAL:
                    raise
                self._note_outage(e)
                if self._outage_exceeded():
                    return None
                wait_s = self._backoff_s(attempt)
                attempt += 1
                self.log(f"[loop] 启动等待 ready 失败（{e}），{wait_s:.1f}s 后重试")
                self._sleep(wait_s)
                continue
            self._outage_since = None
            return st

    def _is_dead_now(self) -> bool:
        if not self.state.st.get("alive", True):
            return True
        if len(self.state.deaths) > self._seen_deaths_len:
            return True
        return False

    def _abort_fatal_skill(self, skill: Optional[Skill], err: Exception) -> None:
        """中止遭遇致命错误的技能并记录收尾切片与 fatal 事件。"""
        if skill is None:
            return
        if self.observer is not None and self.observer.tape is not None:
            self.observer.tape.end_skill_slice()
        skill.abort()
        code, _ = failure(err)
        if self.observer is not None:
            self.observer.on_skill_end(skill, "fatal", self.state, error_code=code, error_msg=str(err))

    def _recover_session_or_respawn(
        self,
        pre_death_inv: dict[str, int],
        *,
        initial_err: Optional[ApiError] = None,
    ) -> bool:
        """等待角色重生并回到比赛服 `ready=True`，刷新真实背包，不发多余 `/posture`。"""
        t0 = time.monotonic()
        deadline = t0 + self.config.respawn_timeout_s
        triggered_death_loop = bool(initial_err and initial_err.code == "death_loop_cooldown")
        last_st = self.state.snapshot()

        if initial_err is not None and initial_err.code == "death_loop_cooldown":
            rem = float(initial_err.extra.get("cooldown_remaining_seconds", 60) or 60)
            wait_s = min(310.0, max(1.0, rem + 2.0))
            self.log(f"[loop] 触发 death_loop_cooldown，冷却 {wait_s:.1f}s 后重连...")
            time.sleep(wait_s)
            deadline = max(deadline, time.monotonic() + max(60.0, self.config.respawn_timeout_s))
            self.client.connect(timeout=20)

        while time.monotonic() < deadline:
            try:
                st = self.client.status()
                self.state.set_status(st)
                last_st = st
                if st.get("alive", True) and st.get("ready", False):
                    break
                if not st.get("ready", False) and st.get("alive", True):
                    self.client.wait_ready(timeout=min(45.0, max(5.0, deadline - time.monotonic())))
                    st = self.client.status()
                    self.state.set_status(st)
                    last_st = st
                    if st.get("alive", True) and st.get("ready", False):
                        break
            except ApiError as e:
                if e.code == "not_connected":
                    self.log("[loop] 断线恢复：按 ≥30s 间隔重连...")
                    try:
                        self.client.ensure_connected()
                        st = self.client.wait_ready(timeout=60)
                        self.state.set_status(st)
                        last_st = st
                        if st.get("alive", True) and st.get("ready", False):
                            break
                    except ApiError as e2:
                        if e2.code == "death_loop_cooldown":
                            e = e2
                        else:
                            time.sleep(max(0.05, self.config.respawn_poll_s))
                            continue
                if e.code == "death_loop_cooldown":
                    triggered_death_loop = True
                    rem = float(e.extra.get("cooldown_remaining_seconds", 60) or 60)
                    wait_s = min(310.0, max(1.0, rem + 2.0))
                    self.log(f"[loop] 触发 death_loop_cooldown，冷却 {wait_s:.1f}s 后重连...")
                    time.sleep(wait_s)
                    deadline = max(deadline, time.monotonic() + max(60.0, self.config.respawn_timeout_s))
                    self.client.connect(timeout=20)
                    st = self.client.wait_ready(timeout=60)
                    self.state.set_status(st)
                    last_st = st
                    if st.get("alive", True) and st.get("ready", False):
                        break
                elif e.code == "wrong_server":
                    st = self.client.wait_ready(timeout=45)
                    self.state.set_status(st)
                    last_st = st
                    if st.get("alive", True) and st.get("ready", False):
                        break
            time.sleep(max(0.01, self.config.respawn_poll_s))
        else:
            raise ApiError("not_ready", f"重生/会话恢复超时 ({self.config.respawn_timeout_s}s)")

        respawn_ms = (time.monotonic() - t0) * 1000.0
        self.state.invalidate_inventory()
        inv = self.state.refresh_inventory(self.client)
        self.state.drain(self.client)
        self._seen_deaths_len = len(self.state.deaths)

        ld = (last_st.get("last_death") or {}) if isinstance(last_st, dict) else {}
        cause = str(ld.get("cause") or "")
        death_pos = ld.get("pos")
        after_fp = _inv_fingerprint(inv) or {}
        self.log(f"[loop] 重生就绪 ({respawn_ms:.0f}ms) pos={self.state.pos} cause={cause!r}")

        if self.observer is not None:
            self.observer.on_death_recovered(
                self.state,
                respawn_ms=respawn_ms,
                cause=cause,
                death_pos=death_pos,
                before_inv_fp=pre_death_inv,
                after_inv_fp=after_fp,
                triggered_death_loop=triggered_death_loop,
            )
        return True

    def run(self) -> RunResult:
        started_mono = time.monotonic()
        steps = 0
        skills_ok = 0
        skills_replan = 0
        deaths_recovered = 0
        stop_reason = "stopped"
        active_skill: Optional[Skill] = None

        monitor = (
            SafetyMonitor(
                self.client,
                self.state,
                self.executor,
                safety=self.safety,
                continuous=self.config.continuous_observe,
                refresh_inventory=self.config.continuous_observe,
            )
            if self.config.enable_monitor
            else None
        )
        try:
            st = self._wait_ready_guarded()
            if st is None:
                self.log(f"[loop] 启动后连续 {self.config.outage_limit_s:.0f}s 等不到 ready，停机")
                stop_reason = "transient_outage"
                return RunResult(0, 0, 0, 0, round(time.monotonic() - started_mono, 2),
                                 stop_reason, self.transient_faults)
            self.state.set_status(st)
            try:
                self.state.refresh_inventory(self.client)
                self.state.drain(self.client)
            except ApiError as e:
                if e.kind == FATAL:
                    raise
                self.state.invalidate_inventory()  # 留给第一拍的刷新补读
                self._note_outage(e)
            self._seen_deaths_len = len(self.state.deaths)
            self.log(f"[loop] 启动完成: {self.state.summary()}")

            if monitor is not None:
                monitor.start()
            if self.observer is not None:
                self.observer.on_run_start(self.state)
                if self.config.target_goal:
                    self.observer.on_progress_tick(self.state, client=self.client, force=True)

            if self._sync_and_check_target_goal() and self.config.stop_on_goal:
                stop_reason = "goal_reached"

            while stop_reason != "goal_reached":
                budget = self._budget_exceeded(started_mono, steps, deaths_recovered)
                if budget:
                    stop_reason = budget
                    break

                pre_inv_fp = _inv_fingerprint(self.state.inventory) or {}
                try:
                    if self._use_async_observation(monitor):
                        if self.state.inventory_dirty or not self.state.inventory:
                            refreshed = self._refresh_with_backoff(inventory_only=True)
                        else:
                            refreshed = True
                    else:
                        refreshed = self._refresh_with_backoff()
                except ApiError as e:
                    if e.code not in SESSION_CODES:
                        raise  # 只剩致命错误
                    if self._recover_guarded(pre_inv_fp, initial_err=e):
                        deaths_recovered += 1
                    elif self._outage_exceeded():
                        stop_reason = "transient_outage"
                        break
                    continue
                if not refreshed:
                    if self._outage_exceeded():
                        self.log(f"[loop] 连续 {self.config.outage_limit_s:.0f}s 读不到状态，停机")
                        stop_reason = "transient_outage"
                        break
                    continue

                if self._is_dead_now():
                    if self._recover_guarded(pre_inv_fp):
                        deaths_recovered += 1
                    elif self._outage_exceeded():
                        stop_reason = "transient_outage"
                        break
                    continue

                if self._sync_and_check_target_goal() and self.config.stop_on_goal:
                    stop_reason = "goal_reached"
                    break

                decision = self.planner.select(self.state, list(self.recent_failures))
                if decision.skill is None:
                    if getattr(decision, "stop_episode", False):
                        if self.observer is not None:
                            self.observer.on_progress_tick(self.state, client=self.client)
                        self.log(f"[loop] 单目标止损退轮: {decision.reason}")
                        stop_reason = "goal_stopped_out"
                        break
                    if self.config.idle_sleep_s > 0:
                        time.sleep(self.config.idle_sleep_s)
                    if self.observer is not None:
                        self.observer.on_progress_tick(self.state, client=self.client)
                    # 若在单次零等待测试中无技能可跑，防止紧凑忙等
                    if self.config.idle_sleep_s == 0:
                        stop_reason = f"idle:{decision.reason}"
                        break
                    continue

                active_skill = decision.skill
                self.log(f"[loop] -> {active_skill} ({decision.source}: {decision.reason})")
                if self.observer is not None:
                    self.observer.on_skill_start(active_skill, decision.source, decision.reason, self.state)

                died_during_skill = False
                skill_t0 = time.monotonic()
                goal_reached_now = False
                skill_done = False
                try:
                    while True:
                        budget = self._budget_exceeded(started_mono, steps, deaths_recovered)
                        if budget:
                            if self.observer is not None and self.observer.tape is not None:
                                self.observer.tape.end_skill_slice()
                            active_skill.abort()
                            if self.observer is not None:
                                self.observer.on_skill_end(
                                    active_skill, "budget_stopped", self.state, died_or_disconnected=False
                                )
                            active_skill = None
                            stop_reason = budget
                            return RunResult(
                                steps, skills_ok, skills_replan, deaths_recovered,
                                round(time.monotonic() - started_mono, 2), stop_reason,
                                self.transient_faults,
                            )

                        if self._is_dead_now():
                            died_during_skill = True
                            raise Replan("角色已死亡，中断当前技能等待重生")

                        done = active_skill.step(self.executor, self.state)
                        if done:
                            skill_done = True
                            break
                        steps += 1
                        if self.observer is not None:
                            self.observer.on_step(active_skill, active_skill.last, self.state, client=self.client)
                except (Replan, ApiError) as e:
                    if isinstance(e, ApiError) and e.kind == FATAL:
                        self._abort_fatal_skill(active_skill, e)
                        active_skill = None
                        raise
                    if active_skill is not None:
                        skill_dt = max(0.0, time.monotonic() - skill_t0)
                        failed_name = active_skill.name
                        if self.observer is not None and self.observer.tape is not None:
                            self.observer.tape.end_skill_slice()
                        active_skill.abort()
                        code, extra = failure(e)
                        is_death_err = (
                            died_during_skill
                            or code in ("dead", "not_connected", "death_loop_cooldown")
                            or (
                                code == "interrupted"
                                and extra.get("cause", extra.get("interrupted_by")) == "death"
                            )
                            or self._is_dead_now()
                        )
                        self.recent_failures.append(f"{active_skill.name}: {e}")
                        self.log(f"[loop] 技能中断 {active_skill}: {e}")
                        if not is_death_err:
                            try:
                                self.state.refresh_status(self.client, max_age_s=0)
                            except ApiError as refresh_err:
                                if refresh_err.kind == FATAL:
                                    raise
                                # 会话类错误按死亡/断线恢复；网关抖动不算死亡，留给下一拍退避刷新
                                if refresh_err.code in SESSION_CODES:
                                    is_death_err = True
                                else:
                                    self._note_outage(refresh_err)
                        environment = e if isinstance(e, EnvBlocked) and not is_death_err else None
                        if environment is not None:
                            self._path_failures.pop(active_skill.goal, None)
                            self.planner.record_environment_block(active_skill, environment, self.state)
                        else:
                            skills_replan += 1
                            if code in ("path_not_found", "stalled") and not is_death_err:
                                streak = self._path_failures.get(active_skill.goal, 0) + 1
                                self._path_failures[active_skill.goal] = streak
                            else:
                                streak = 0
                                self._path_failures.pop(active_skill.goal, None)
                            if streak >= 3:
                                active_skill.mark_failed(cooldown_s=3 * active_skill.cooldown_s)
                            else:
                                active_skill.mark_failed()
                        if self.observer is not None:
                            self.observer.on_skill_end(
                                active_skill,
                                "env_blocked" if environment is not None else "replan",
                                self.state,
                                error_code="env_blocked" if environment is not None else code,
                                error_msg=str(e),
                                died_or_disconnected=is_death_err,
                                **({"environment": environment} if environment is not None else {}),
                                **({"environment_recovery": True} if decision.environment_recovery and not is_death_err else {}),
                            )
                        if environment is None and hasattr(self.planner, "record_skill_outcome"):
                            self.planner.record_skill_outcome(
                                failed_name,
                                ok=False,
                                elapsed_s=skill_dt,
                                completed_advs=(
                                    set(self.observer.server_advancements)
                                    if self.observer is not None
                                    else set(self._server_advancements)
                                ),
                            )
                        active_skill = None
                        if is_death_err:
                            init_api_err = e if isinstance(e, ApiError) else None
                            if self._recover_guarded(pre_inv_fp, initial_err=init_api_err):
                                deaths_recovered += 1
                except Fatal as e:
                    self._abort_fatal_skill(active_skill, e)
                    active_skill = None
                    raise
                except KeyboardInterrupt:
                    stop_reason = "keyboard_interrupt"
                    break

                if skill_done and active_skill is not None:
                    skill_dt = max(0.0, time.monotonic() - skill_t0)
                    finished_skill = active_skill
                    active_skill = None
                    finished_name = finished_skill.name
                    self._path_failures.pop(finished_skill.goal, None)
                    if self.observer is not None and self.observer.tape is not None:
                        self.observer.tape.end_skill_slice()
                    try:
                        if monitor is not None and self.config.async_observe_for_llm:
                            if self.state.inventory_dirty:
                                self.state.refresh_inventory(self.client)
                        else:
                            # 技能已完成；这里读不到状态不应把它记成失败，留给下一拍刷新与恢复
                            self._refresh_with_backoff()
                    except ApiError as refresh_err:
                        if refresh_err.kind == FATAL:
                            raise
                    skills_ok += 1
                    force_tick = any(
                        g.skill == finished_name and g.done_advancements
                        for g in self._resolve_target_goal_specs()
                    )
                    if self.observer is not None:
                        self.observer.on_skill_end(
                            finished_skill, "completed", self.state, died_or_disconnected=False
                        )
                        self.observer.on_progress_tick(
                            self.state, client=self.client, force=force_tick
                        )
                    elif force_tick:
                        try:
                            adv = self.client.advancements(status="completed", include_recipes=False)
                            for item in (adv.get("advancements") or []):
                                aid = item.get("id") if isinstance(item, dict) else None
                                if aid:
                                    self._server_advancements.add(aid)
                        except Exception:
                            pass
                    completed_advs = set(self._server_advancements)
                    if self.observer is not None:
                        completed_advs.update(self.observer.server_advancements)
                    if hasattr(self.planner, "record_skill_outcome"):
                        self.planner.record_skill_outcome(
                            finished_name,
                            ok=True,
                            elapsed_s=skill_dt,
                            completed_advs=completed_advs,
                        )
                    if self._sync_and_check_target_goal() and self.config.stop_on_goal:
                        stop_reason = "goal_reached"
                        goal_reached_now = True

                if goal_reached_now:
                    break

        finally:
            if active_skill is not None:
                active_skill.abort()
            release_materials = getattr(self.planner, "release_skill_materials", None)
            if release_materials is not None:
                release_materials()
            if monitor is not None:
                monitor.close()
            if self.observer is not None:
                # 外迁最后一步可能耗尽预算。还未进入下一规划拍时，保留挂起证据。
                blocked = self.planner.blocked_targets(self.state, self.observer.target_goals, refresh=False)
                self.observer.on_run_end(
                    self.state, client=self.client, stop_reason=stop_reason,
                    **({"environment_blocked": blocked} if blocked else {}),
                )
            if self.config.disconnect_on_exit:
                try:
                    self.client.disconnect()
                except Exception:
                    pass

        return RunResult(
            steps=steps,
            skills_completed=skills_ok,
            skills_replan=skills_replan,
            deaths_recovered=deaths_recovered,
            elapsed_s=round(time.monotonic() - started_mono, 2),
            stop_reason=stop_reason,
            transient_faults=self.transient_faults,
        )
