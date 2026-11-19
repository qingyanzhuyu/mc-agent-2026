"""战略层编排器（纯搬移自 policy.py）：持有当前 `Objective`、触发检测、目标切换、战术展开、历史与遥测。

职责：在 `--strategy {rule,llm}` 下管理 `Objective` 生命周期与 `recent_objectives`，仅在 8 类目标级边界事件
上切换目标并写遥测，再委托战术层展开下一技能（生存补给与 `safe_mode` 始终置顶）。
"""
from __future__ import annotations

from collections import deque
import math
import os
from typing import TYPE_CHECKING, Callable, Collection, Optional, Sequence

from ..planner import objective
from ..planner.objective import Objective
from ..planner.scheduler import DecisionScheduler
from ..runtime import State
from ..skills.world_memo import memo_for
from .context import DecisionContext, ObjectiveRecord, build_decision_context, classify_failure_kind, is_daytime
from .goals import is_goal_achieved
from .strategy_async import (
    dispatch_async_objective, dispatch_async_skill_fallback, invalidate_pending_async, poll_async_scheduler,
)

if TYPE_CHECKING:
    from .policy import CampaignPlanner, PlanDecision, RulePlanner

DEFAULT_STRATEGY_REVIEW_S: float = 600.0
DEFAULT_LANDMARK_COOLDOWN_S: float = 15.0


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        val = float(raw) if (raw and raw.strip()) else default
    except ValueError:
        return default
    return val if math.isfinite(val) and val >= 0.0 else default


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if (raw is None or not raw.strip()) else raw.strip().lower() in ("1", "true", "yes", "on")


class StrategyOrchestrator:
    """`CampaignPlanner` 在 `rule` / `llm` 战略模式下的有状态编排内核。"""

    def __init__(self, planner: "CampaignPlanner", *, clock: Callable[[], float]) -> None:
        self.planner, self.clock = planner, clock
        self.current_objective: Optional[Objective] = None
        self._tracked_objective: Optional[Objective] = None
        self._objective_skill_elapsed_s, self._last_objective_decision_at = 0.0, float("-inf")
        self.recent_objectives: deque[ObjectiveRecord] = deque(maxlen=10)
        self._consecutive_goal_fails: dict[str, int] = {}
        self._last_failure_kind, self._last_failure_detail = {}, {}
        self._deaths_initialized, self._last_deaths_marker = False, (0, None)
        self._last_is_day, self._deferred_trigger, self._seen_failure_str = None, None, ""
        self._landmark_initialized: bool = False
        self._last_landmark_version: int = 0
        self._last_landmark_trigger_at: float = float("-inf")
        self.landmark_cooldown_s: float = env_float("MC_LANDMARK_COOLDOWN_S", DEFAULT_LANDMARK_COOLDOWN_S)
        self.scheduler: Optional[DecisionScheduler] = (
            DecisionScheduler(lambda _s: None, require_iron=planner.require_iron, clock=self.clock) if planner.strategy == "llm" else None
        )

    def _poll_landmark_version(self, state: State, now: Optional[float] = None) -> bool:
        """检测 `WorldResourceMemo.landmark_version` 是否递增（发现新村庄/遗迹传送门/要塞/机会实体等地标）。"""
        memo = memo_for(state)
        cur_ver = int(getattr(memo, "landmark_version", 0) or 0)
        if not self._landmark_initialized:
            self._landmark_initialized = True
            self._last_landmark_version = cur_ver
            return False
        if cur_ver < self._last_landmark_version:
            self._last_landmark_version = cur_ver
            return False
        if cur_ver > self._last_landmark_version:
            t_now = self.clock() if now is None else float(now)
            if self.landmark_cooldown_s > 0.0 and (t_now - self._last_landmark_trigger_at) < self.landmark_cooldown_s:
                return False
            self._last_landmark_version = cur_ver
            return True
        return False

    def apply_objective(self, obj: Optional[Objective], *, now: Optional[float] = None) -> None:
        self.current_objective = self._tracked_objective = obj
        self._objective_skill_elapsed_s = 0.0
        if obj is not None:
            self._last_objective_decision_at = self.clock() if now is None else float(now)

    def set_objective(self, obj: Optional[Objective], *, now: Optional[float] = None) -> None:
        if self.scheduler is not None:
            self.scheduler.invalidate()
        self.apply_objective(obj, now=now)

    def sync_external_objective(self) -> None:
        if self.current_objective is not self._tracked_objective:
            self._tracked_objective, self._objective_skill_elapsed_s = self.current_objective, 0.0
            if self.current_objective is not None and self._last_objective_decision_at == float("-inf"):
                self._last_objective_decision_at = self.current_objective.started_at

    def current_objective_elapsed(self, now: float) -> float:
        return 0.0 if self.current_objective is None else max(max(0.0, now - self.current_objective.started_at), self._objective_skill_elapsed_s)

    def make_tactical_planner(self, goal_id: str) -> "RulePlanner":
        from .policy import RulePlanner

        p = self.planner
        tp = RulePlanner(
            p.registry, ledger=p.rules.ledger, include_stages=p.rules.include_stages,
            target_goal=goal_id, telemetry=p.rules.telemetry, min_verified=p.min_verified,
            reservations=p.reservations,
        )
        tp.completed_advancements = p.rules.completed_advancements
        tp.ep_goal_fails, tp.ep_goal_elapsed = p.rules.ep_goal_fails, p.rules.ep_goal_elapsed
        tp._environment_blocks = p.rules._environment_blocks
        tp._abandoned_goals = p.rules._abandoned_goals
        return tp

    def is_iron_complete(self, state: State) -> bool:
        iron_goal = self.planner.rules.ledger_map.get("smelt_iron")
        if iron_goal is not None and is_goal_achieved(iron_goal, state, self.planner.rules.completed_advancements):
            return True
        return state.count("iron_ingot") >= 1 or state.count("iron_pickaxe") >= 1

    def rule_objective(self, state: State, *, exclude_goals: Collection[str] = (), now: Optional[float] = None) -> Optional[Objective]:
        """规则战略：取现有 `_rank()` 性价比第一名且战术可展开的 `root_goal` 封装为 `Objective`。"""
        p = self.planner
        t_now = self.clock() if now is None else float(now)
        excluded, seen_roots = set(exclude_goals), set()
        p.rules.refresh_environment_blocks(state)
        for step in p.rules._rank(state):
            root = step.root_goal
            if root.id in seen_roots or root.id in excluded:
                continue
            seen_roots.add(root.id)
            candidate_obj = Objective(
                goal_id=root.id, budget_s=objective.clamp_budget(root.budget_s),
                why=f"规则性价比第1（{step.path_score}分/{step.path_cost:.0f}s）", source="rule", started_at=t_now,
            )
            if self.make_tactical_planner(root.id).expand_objective_skill(state)[0] is not None:
                return candidate_obj
        return None

    def build_context(
        self, state: State, *, current_objective: Optional[Objective] = None,
        current_objective_elapsed_s: Optional[float] = None, now: Optional[float] = None,
    ) -> DecisionContext:
        p = self.planner
        t_now = self.clock() if now is None else float(now)
        obj = self.current_objective if current_objective is None else current_objective
        obj_elapsed = self.current_objective_elapsed(t_now) if current_objective_elapsed_s is None else float(current_objective_elapsed_s)
        return build_decision_context(
            state, ledger=p.rules.ledger, registry=p.registry, min_verified=p.min_verified,
            include_stages=p.rules.include_stages, focus_goals=p.target_goal,
            completed_advs=p.rules.completed_advancements, ep_goal_fails=p.rules.ep_goal_fails,
            ep_goal_elapsed=p.rules.ep_goal_elapsed, telemetry=p.rules.telemetry,
            environment_blocks=p.rules._environment_blocks, current_objective=obj,
            current_objective_elapsed_s=obj_elapsed, recent_objectives=tuple(self.recent_objectives),
            elapsed_match_s=max(0.0, t_now - p._started_mono), max_match_seconds=p.max_seconds,
            subgoal_predicates=p.subgoal_provider(state) if p.subgoal_provider is not None else None,
            reservations=p.reserve_provider(state) if p.reserve_provider is not None else None,
            reservation_book=p.reservations,
        )

    def emit_objective_telemetry(
        self, *, trigger: str, source: str, obj: Optional[Objective], ms: float,
        prompt_tokens: int = 0, completion_tokens: int = 0, errors: Sequence[str] = (),
        adopted: bool = True, replaced_goal_id: Optional[str] = None,
    ) -> None:
        p = self.planner
        if p.on_objective_telemetry is None:
            return
        total_tok = int(prompt_tokens) + int(completion_tokens)
        cum_p = int(p.llm.stats.get("prompt_tokens", 0)) if p.llm is not None else 0
        cum_c = int(p.llm.stats.get("completion_tokens", 0)) if p.llm is not None else 0
        p.on_objective_telemetry({
            "trigger": trigger, "source": source,
            "goal_id": obj.goal_id if obj is not None else None, "budget_s": obj.budget_s if obj is not None else None,
            "why": obj.why if obj is not None else "",
            "route_branch": getattr(obj, "route_branch", "") if obj is not None else "",
            "ms": round(float(ms), 1),
            "prompt_tokens": int(prompt_tokens), "completion_tokens": int(completion_tokens), "total_tokens": total_tok,
            "cumulative_prompt_tokens": cum_p, "cumulative_completion_tokens": cum_c, "cumulative_total_tokens": cum_p + cum_c,
            "errors": list(errors), "adopted": bool(adopted), "replaced_goal_id": replaced_goal_id,
        })

    def record_objective_history(self, obj: Objective, *, outcome: str, failure_kind: str, now: float, detail: str = "") -> None:
        self.recent_objectives.append(ObjectiveRecord(
            goal_id=obj.goal_id, outcome=outcome, failure_kind=failure_kind,
            elapsed_s=round(self.current_objective_elapsed(now), 1), detail=detail,
        ))

    def on_skill_outcome(self, skill_name: str, *, ok: bool, elapsed_s: float) -> None:
        self.sync_external_objective()
        if self.current_objective is None:
            return
        gid = self.current_objective.goal_id
        if elapsed_s > 0.0:
            self._objective_skill_elapsed_s += float(elapsed_s)
        spec = self.planner.rules.ledger_map.get(gid)
        if not ok:
            self._consecutive_goal_fails[gid] = self._consecutive_goal_fails.get(gid, 0) + 1
            self._last_failure_kind.setdefault(gid, "other_replan")
        elif (spec is not None and spec.skill == skill_name) or skill_name not in ("eat_food", "scout_island"):
            self._consecutive_goal_fails[gid] = 0

    def _detect_trigger(
        self,
        state: State,
        now: float,
        *,
        died_since_last: bool,
        day_night_flipped: bool,
        landmark_discovered: bool = False,
    ) -> Optional[tuple[str, str, str]]:
        p = self.planner
        if self._deferred_trigger:
            trig, self._deferred_trigger = self._deferred_trigger, None
            return trig, ("respawn" if trig == "respawn" else "switched"), ("combat_or_death" if trig == "respawn" else "none")
        if died_since_last:
            return "respawn", "respawn", "combat_or_death"
        if day_night_flipped:
            return "day_night_changed", "switched", "none"
        if self.current_objective is None:
            return "initial", "none", "none"
        spec = p.rules.ledger_map.get(self.current_objective.goal_id)
        if spec is None:
            return "no_tactical_skill", "no_skill", "missing_prereq"
        if is_goal_achieved(spec, state, p.rules.completed_advancements):
            return "goal_achieved", "achieved", "none"
        if spec.id in getattr(p.rules, "_abandoned_goals", ()):
            return "goal_abandoned", "max_fails", self._last_failure_kind.get(spec.id, "other_replan")
        if p.rules.blocked_targets(state, spec.id, refresh=False):
            return "env_blocked", "env_blocked", "env_blocked"
        if self.current_objective_elapsed(now) >= self.current_objective.budget_s:
            return "budget_exceeded", "budget_exceeded", "timeout"
        if spec.max_fails_per_ep > 0 and self._consecutive_goal_fails.get(spec.id, 0) >= spec.max_fails_per_ep:
            return "max_fails_reached", "max_fails", self._last_failure_kind.get(spec.id, "other_replan")
        if landmark_discovered:
            return "landmark_opportunity", "switched", "none"
        if p.review_interval_s > 0.0 and (now - self._last_objective_decision_at) >= p.review_interval_s:
            return "periodic_review", "switched", "none"
        return None

    def _switch_objective(self, state: State, *, trigger: str, old_outcome: str, failure_kind: str, now: float, exclude_goals: Collection[str] = ()) -> None:
        p = self.planner
        prev_obj, prev_elapsed = self.current_objective, self.current_objective_elapsed(now)
        replaced_gid = prev_obj.goal_id if prev_obj is not None else None
        excluded = set(exclude_goals)
        if prev_obj is not None:
            detail = self._last_failure_detail.get(prev_obj.goal_id, "") if failure_kind != "none" else ""
            self.record_objective_history(prev_obj, outcome=old_outcome, failure_kind=failure_kind, now=now, detail=detail)
            spec = p.rules.ledger_map.get(prev_obj.goal_id)
            if trigger in ("max_fails_reached", "goal_abandoned") and spec is not None:
                if spec.max_fails_per_ep > 0:
                    p.rules.ep_goal_fails[spec.id] = max(p.rules.ep_goal_fails.get(spec.id, 0), spec.max_fails_per_ep)
                excluded.add(spec.id)
                self._consecutive_goal_fails[spec.id] = 0
            elif trigger == "budget_exceeded" and spec is not None:
                p.rules.ep_goal_elapsed[spec.id] = max(p.rules.ep_goal_elapsed.get(spec.id, 0.0), prev_obj.budget_s)
                excluded.add(spec.id)

        rule_obj = self.rule_objective(state, exclude_goals=excluded, now=now)
        if rule_obj is None and excluded and trigger == "budget_exceeded":
            rule_obj = self.rule_objective(state, now=now)
        self.apply_objective(rule_obj, now=now)
        self._last_objective_decision_at = now
        if trigger == "landmark_opportunity":
            self._last_landmark_trigger_at = now
        self.emit_objective_telemetry(
            trigger=trigger, source="rule", obj=rule_obj, ms=0.0,
            adopted=rule_obj is not None, replaced_goal_id=replaced_gid,
        )
        if p.strategy == "llm":
            dispatch_async_objective(self, state, trigger=trigger, prev_objective=prev_obj, prev_elapsed_s=prev_elapsed, now=now)

    def _try_expand_current(self, state: State, fired_trigger: Optional[str]) -> Optional["PlanDecision"]:
        from .policy import PlanDecision

        if self.current_objective is None:
            return None
        tactical = self.make_tactical_planner(self.current_objective.goal_id)
        dec = tactical.choose_for_objective(state)
        if dec.skill is None:
            return None
        self.planner._last_selected_goal_id = tactical._last_selected_goal_id or self.current_objective.goal_id
        self.planner.rules._last_selected_goal_id = self.planner._last_selected_goal_id
        return PlanDecision(
            dec.skill, dec.source, f"[目标:{self.current_objective.goal_id}] {dec.reason}",
            environment_recovery=dec.environment_recovery, objective=self.current_objective,
            objective_source=self.current_objective.source, objective_trigger=fired_trigger,
            safety_escape=dec.safety_escape,
        )

    def select(self, state: State, recent_failures: list[str]) -> "PlanDecision":
        from .policy import PlanDecision

        p = self.planner
        self.sync_external_objective()
        now = self.clock()
        p.rules.refresh_environment_blocks(state)

        if recent_failures and recent_failures[-1] != self._seen_failure_str:
            self._seen_failure_str = recent_failures[-1]
            if self.current_objective is not None:
                gid = self.current_objective.goal_id
                self._last_failure_kind[gid] = classify_failure_kind("", self._seen_failure_str)
                self._last_failure_detail[gid] = self._seen_failure_str

        deaths_marker = (len(state.deaths), state.deaths[-1] if state.deaths else None)
        if not self._deaths_initialized:
            self._deaths_initialized, self._last_deaths_marker, died_since_last = True, deaths_marker, False
        else:
            died_since_last = deaths_marker != self._last_deaths_marker
            self._last_deaths_marker = deaths_marker

        cur_day = is_daytime(state)
        day_night_flipped = bool(cur_day is not None and self._last_is_day is not None and cur_day != self._last_is_day)
        if cur_day is not None:
            self._last_is_day = cur_day

        landmark_discovered = self._poll_landmark_version(state, now=now)

        if died_since_last:
            invalidate_pending_async(self, "respawn")
        elif day_night_flipped:
            invalidate_pending_async(self, "day_night_changed")
        elif landmark_discovered:
            invalidate_pending_async(self, "landmark_opportunity")

        if (urgent := p.rules.choose_survival_or_safe_mode(state)) is not None:
            if state.in_safe_mode:
                invalidate_pending_async(self, "safe_mode")
            if died_since_last:
                self._deferred_trigger = "respawn"
            elif day_night_flipped and not self._deferred_trigger:
                self._deferred_trigger = "day_night_changed"
            elif landmark_discovered and not self._deferred_trigger:
                self._deferred_trigger = "landmark_opportunity"
            p._last_selected_goal_id = None
            return PlanDecision(
                urgent.skill, urgent.source, urgent.reason, objective=self.current_objective,
                objective_source=self.current_objective.source if self.current_objective else None,
                safety_escape=urgent.safety_escape,
            )

        fallback_skill_decision = poll_async_scheduler(self, state, now)
        fired_trigger: Optional[str] = None
        if (trig_info := self._detect_trigger(
            state, now,
            died_since_last=died_since_last,
            day_night_flipped=day_night_flipped,
            landmark_discovered=landmark_discovered,
        )) is not None:
            fired_trigger, old_outcome, f_kind = trig_info
            self._switch_objective(state, trigger=fired_trigger, old_outcome=old_outcome, failure_kind=f_kind, now=now)

        if self.current_objective is not None:
            if (expanded := self._try_expand_current(state, fired_trigger)) is not None:
                return expanded
            failed_gid, fired_trigger = self.current_objective.goal_id, "no_tactical_skill"
            self._switch_objective(
                state, trigger="no_tactical_skill", old_outcome="no_skill",
                failure_kind="missing_prereq", now=now, exclude_goals={failed_gid},
            )
            if (expanded2 := self._try_expand_current(state, fired_trigger)) is not None:
                return expanded2

        base_dec = p.rules.choose(state)
        p._last_selected_goal_id = p.rules._last_selected_goal_id
        if base_dec.skill is not None or base_dec.stop_episode:
            return PlanDecision(
                base_dec.skill, base_dec.source, base_dec.reason,
                environment_recovery=base_dec.environment_recovery,
                stop_episode=base_dec.stop_episode,
                objective=self.current_objective,
                objective_source=self.current_objective.source if self.current_objective else None,
                objective_trigger=fired_trigger,
                cooldown_blocked=base_dec.cooldown_blocked,
                blocked_goal_id=base_dec.blocked_goal_id,
                safety_escape=base_dec.safety_escape,
            )

        if fallback_skill_decision is not None:
            return fallback_skill_decision
        if p.strategy == "llm":
            dispatch_async_skill_fallback(self, state, recent_failures)

        return PlanDecision(
            base_dec.skill, base_dec.source, base_dec.reason,
            environment_recovery=base_dec.environment_recovery,
            stop_episode=base_dec.stop_episode,
            objective=self.current_objective,
            objective_source=self.current_objective.source if self.current_objective else None,
            objective_trigger=fired_trigger,
            cooldown_blocked=base_dec.cooldown_blocked,
            blocked_goal_id=base_dec.blocked_goal_id,
            safety_escape=base_dec.safety_escape,
        )
