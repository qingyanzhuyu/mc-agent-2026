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
from typing import Optional

from ..client import ApiError, Client
from ..runtime import Executor, Fatal, Replan, Safety, SafetyMonitor, State, _inv_fingerprint
from ..skills.base import Skill, failure
from .auditor import SoakObserver
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


@dataclass
class RunResult:
    steps: int
    skills_completed: int
    skills_replan: int
    deaths_recovered: int
    elapsed_s: float
    stop_reason: str


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
        self._seen_deaths_len = 0

    def _budget_exceeded(self, started_mono: float, steps: int, deaths: int) -> Optional[str]:
        if self.config.max_steps > 0 and steps >= self.config.max_steps:
            return "max_steps_reached"
        if self.config.max_seconds > 0 and (time.monotonic() - started_mono) >= self.config.max_seconds:
            return "max_seconds_reached"
        if self.config.max_deaths > 0 and deaths >= self.config.max_deaths:
            return "max_deaths_reached"
        return None

    def _refresh_world_state(self) -> None:
        self.state.refresh_status(self.client)
        self.state.drain(self.client)
        if self.state.inventory_dirty or not self.state.inventory:
            self.state.refresh_inventory(self.client)

    def _is_dead_now(self) -> bool:
        if not self.state.st.get("alive", True):
            return True
        if len(self.state.deaths) > self._seen_deaths_len:
            return True
        return False

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

        monitor = SafetyMonitor(self.client, self.state, self.executor, safety=self.safety) if self.config.enable_monitor else None
        try:
            st = self.client.wait_ready(timeout=120)
            self.state.set_status(st)
            self.state.refresh_inventory(self.client)
            self.state.drain(self.client)
            self._seen_deaths_len = len(self.state.deaths)
            self.log(f"[loop] 启动完成: {self.state.summary()}")

            if monitor is not None:
                monitor.start()
            if self.observer is not None:
                self.observer.on_run_start(self.state)

            while True:
                budget = self._budget_exceeded(started_mono, steps, deaths_recovered)
                if budget:
                    stop_reason = budget
                    break

                pre_inv_fp = _inv_fingerprint(self.state.inventory) or {}
                try:
                    self._refresh_world_state()
                except ApiError as e:
                    if e.code in ("dead", "not_connected", "wrong_server", "death_loop_cooldown"):
                        self._recover_session_or_respawn(pre_inv_fp, initial_err=e)
                        deaths_recovered += 1
                        continue
                    raise

                if self._is_dead_now():
                    self._recover_session_or_respawn(pre_inv_fp)
                    deaths_recovered += 1
                    continue

                decision = self.planner.select(self.state, list(self.recent_failures))
                if decision.skill is None:
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
                try:
                    while True:
                        budget = self._budget_exceeded(started_mono, steps, deaths_recovered)
                        if budget:
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
                            )

                        if self._is_dead_now():
                            died_during_skill = True
                            raise Replan("角色已死亡，中断当前技能等待重生")

                        done = active_skill.step(self.executor, self.state)
                        if done:
                            self._refresh_world_state()
                            skills_ok += 1
                            if self.observer is not None:
                                self.observer.on_skill_end(
                                    active_skill, "completed", self.state, died_or_disconnected=False
                                )
                                self.observer.on_progress_tick(self.state, client=self.client)
                            active_skill = None
                            break
                        steps += 1
                        if self.observer is not None:
                            self.observer.on_step(active_skill, active_skill.last, self.state, client=self.client)
                except (Replan, ApiError) as e:
                    if active_skill is not None:
                        active_skill.abort()
                        code, extra = failure(e)
                        is_death_err = (
                            died_during_skill
                            or code in ("dead", "not_connected", "death_loop_cooldown")
                            or extra.get("interrupted_by") == "death"
                            or self._is_dead_now()
                        )
                        active_skill.mark_failed()
                        skills_replan += 1
                        self.recent_failures.append(f"{active_skill.name}: {e}")
                        self.log(f"[loop] 技能中断 {active_skill}: {e}")
                        if not is_death_err:
                            try:
                                self.state.refresh_status(self.client, max_age_s=0)
                            except ApiError:
                                is_death_err = True
                        if self.observer is not None:
                            self.observer.on_skill_end(
                                active_skill,
                                "replan",
                                self.state,
                                error_code=code,
                                error_msg=str(e),
                                died_or_disconnected=is_death_err,
                            )
                        active_skill = None
                        if is_death_err:
                            init_api_err = e if isinstance(e, ApiError) else None
                            self._recover_session_or_respawn(pre_inv_fp, initial_err=init_api_err)
                            deaths_recovered += 1
                except Fatal as e:
                    if active_skill is not None:
                        active_skill.abort()
                        code, _ = failure(e)
                        if self.observer is not None:
                            self.observer.on_skill_end(
                                active_skill, "fatal", self.state, error_code=code, error_msg=str(e)
                            )
                        active_skill = None
                    raise
                except KeyboardInterrupt:
                    stop_reason = "keyboard_interrupt"
                    break

        finally:
            if active_skill is not None:
                active_skill.abort()
            if monitor is not None:
                monitor.close()
            if self.observer is not None:
                self.observer.on_run_end(self.state, client=self.client)
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
        )
