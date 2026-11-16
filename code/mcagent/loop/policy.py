"""规划层入口：`PlanDecision`、`RulePlanner`（性价比排序与战术展开器）与薄分派 `CampaignPlanner`。

战略编排与异步预取已拆分至 `strategy.py` 与 `strategy_async.py`；`CampaignPlanner.select()` 按 `mode` 与 `strategy` 分派。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import time
from typing import TYPE_CHECKING, Any, Callable, Collection, Mapping, Optional

from ..planner import skill_choice
from ..planner.llm import LLMClient
from ..planner.objective import Objective
from ..planner.scheduler import DecisionScheduler
from ..runtime import EnvBlocked, State, _inv_fingerprint
from ..skills import REGISTRY, Skill, bind_args, make
from ..skills.items import best_pickaxe
from .context import DecisionContext, ObjectiveRecord
from .goals import (
    DEFAULT_CAMPAIGN_STAGES, GOALS, OFFICIAL_CAMPAIGN_STAGES, STONE_OR_BETTER_PICKS, GoalSpec,
    check_item_expr, diagnose_goal_gap, is_goal_achieved, is_goal_stopped_out,
    is_prereq_satisfied, normalize_goal_ids, rank_actionable_goals, resolve_active_goals,
)
from .starter_skills import PROTECTED_ZONES, can_afford_gear, total_cobble, total_logs, total_planks

if TYPE_CHECKING:
    from .strategy import StrategyOrchestrator

STRATEGIES: tuple[str, ...] = ("legacy", "rule", "llm")


@dataclass
class PlanDecision:
    skill: Optional[Skill]
    source: str          # "rule" | "llm" | "llm_fallback_rule" | "focus" | "idle"
    reason: str
    llm_ms: Optional[float] = None
    llm_attempts: int = 0
    llm_errors: tuple[str, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    environment_recovery: bool = False
    objective: Optional[Objective] = None
    objective_source: Optional[str] = None
    objective_trigger: Optional[str] = None


@dataclass
class _EnvironmentBlock:
    error: EnvBlocked
    observation: Any

    @staticmethod
    def observe(error: EnvBlocked, state: State) -> Any:
        if error.wake_on == "inventory_changed":
            return _inv_fingerprint(state.inventory) if not state.inventory_dirty else None
        status = state.snapshot()
        if error.wake_on in ("day", "night"):
            return status.get("is_day") if isinstance(status.get("is_day"), bool) else None
        pos = status.get("pos")
        if not isinstance(pos, (list, tuple)) or len(pos) != 3:
            return None
        if not all(isinstance(n, (int, float)) and math.isfinite(n) for n in (pos[0], pos[2])):
            return None
        return status.get("dimension"), math.floor(pos[0] / 16), math.floor(pos[2] / 16)

    def wake_if_changed(self, state: State) -> bool:
        current = self.observe(self.error, state)
        if current is None or current == self.observation:
            return False
        previous, self.observation = self.observation, current
        return (current is (self.error.wake_on == "day")) if self.error.wake_on in ("day", "night") else (previous is not None)


def build_state_summary(state: State, *, goal_hint: str = "") -> str:
    """给技能级模型选择（兼容 `--llm`）的一屏状态摘要（遵守 J17）。战略层上下文见 `loop/context.py`。"""
    st = state.snapshot()
    pick = best_pickaxe(state) or "无"
    lines = [
        f"位置={st.get('pos')} 维度={st.get('dimension', 'overworld')} 昼夜={'白天' if st.get('is_day', True) else '夜晚'}({st.get('time_of_day')})",
        f"血量={st.get('health', 20)} 饱食={st.get('food', 20)} 氧气={st.get('oxygen', 300)} 安全模式={state.in_safe_mode}",
        f"最近敌对={ (st.get('nearest_hostile') or {}).get('name', '无') } 距离={ (st.get('nearest_hostile') or {}).get('dist', '-') }",
        f"物资：最优镐={pick} 原木={total_logs(state)} 木板={total_planks(state)} 木棍={state.count('stick')} "
        f"工作台={state.count('crafting_table')} 圆石={total_cobble(state)} 熔炉={state.count('furnace')} 石剑={state.count('stone_sword')}",
    ]
    if PROTECTED_ZONES:
        lines.append(f"已发现保护区点位数={len(PROTECTED_ZONES)}（需远离）")
    if goal_hint:
        lines.append(f"当前目标指引：{goal_hint}")
    return "\n".join(lines)


class RulePlanner:
    """基于目标台账（`goals.py`）与当拍真实 `State` 的科技树性价比派生器与战术展开器。"""

    def __init__(
        self, registry: Optional[dict[str, type[Skill]]] = None, ledger: tuple[GoalSpec, ...] = GOALS,
        include_stages: Optional[Collection[str]] = None, target_goal: Optional[str] = None,
        telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None, min_verified: str = "offline",
    ):
        self.registry, self.min_verified = (REGISTRY if registry is None else registry), min_verified
        self.ledger: tuple[GoalSpec, ...] = tuple(ledger)
        self.ledger_map: dict[str, GoalSpec] = {g.id: g for g in self.ledger}
        default_stages = OFFICIAL_CAMPAIGN_STAGES if min_verified == "live" else DEFAULT_CAMPAIGN_STAGES
        self.include_stages: tuple[str, ...] = tuple(default_stages if include_stages is None else include_stages)
        self.target_goal, self.telemetry = target_goal, (dict(telemetry) if telemetry else {})
        self.active_goals: tuple[GoalSpec, ...] = resolve_active_goals(self.ledger, include_stages=self.include_stages, target_goal=self.target_goal)
        self.completed_advancements: set[str] = set()
        self.ep_goal_fails, self.ep_goal_elapsed = {}, {}
        self._last_selected_goal_id: Optional[str] = None
        self._environment_blocks: dict[str, _EnvironmentBlock] = {}

    def record_environment_block(self, skill: Skill, error: EnvBlocked, state: State) -> None:
        self._environment_blocks[skill.goal] = _EnvironmentBlock(error, _EnvironmentBlock.observe(error, state))
        self._last_selected_goal_id = None

    def refresh_environment_blocks(self, state: State) -> None:
        for key, block in list(self._environment_blocks.items()):
            if block.wake_if_changed(state):
                del self._environment_blocks[key]

    def environment_block(self, goal_key: str) -> Optional[EnvBlocked]:
        block = self._environment_blocks.get(goal_key)
        return block.error if block else None

    def _block_for_goal(self, goal: GoalSpec, state: State) -> Optional[EnvBlocked]:
        name, args = goal.skill, goal.args_dict
        if not all(check_item_expr(state, expr) for expr in goal.prereq_items):
            for expression, fallback, fallback_args in goal.gather_fallbacks:
                if not check_item_expr(state, expression):
                    name, args = fallback, dict(fallback_args)
                    break
            else:
                return None
        cls = self.registry.get(name)
        if cls is None:
            return None
        bound, errors = bind_args(cls, args)
        return None if errors else self.environment_block(cls.goal.format(**bound))

    def blocked_targets(self, state: State, target_goals: Any, *, refresh: bool = True) -> dict[str, EnvBlocked]:
        if refresh:
            self.refresh_environment_blocks(state)
        if not self._environment_blocks:
            return {}

        def blocked_branch(goal: GoalSpec, seen: set[str]) -> Optional[EnvBlocked]:
            if goal.id in seen:
                return None
            seen.add(goal.id)
            for key in goal.prereq_goals:
                prereq = self.ledger_map.get(key)
                if prereq and not is_prereq_satisfied(prereq, state, self.completed_advancements):
                    if (block := blocked_branch(prereq, seen)) is not None:
                        return block
            return self._block_for_goal(goal, state)

        targets = {}
        for gid in normalize_goal_ids(target_goals):
            goal = self.ledger_map.get(gid)
            if goal is not None and not is_goal_achieved(goal, state, self.completed_advancements):
                if (block := blocked_branch(goal, set())) is not None:
                    targets[gid] = block
        return targets

    def _try_make(self, name: str, args: Optional[dict], state: State) -> Optional[Skill]:
        cls = self.registry.get(name)
        if cls is None or not skill_choice.meets_verification(cls, self.min_verified) or cls.blocked(state):
            return None
        inst = cls(**(args or {}))
        return None if (self.environment_block(inst.goal) or inst.is_cooling() or not inst.can_start(state)) else inst

    def expand_objective_skill(self, state: State) -> tuple[Optional[Skill], str]:
        from .strategy import expand_tactical_skill
        return expand_tactical_skill(self, state)

    def choose_survival_or_safe_mode(self, state: State) -> Optional[PlanDecision]:
        hp, food = float(state.st.get("health", 20)), float(state.st.get("food", 20))
        if food <= 14 or (hp < 18 and food < 20):
            if (eat := self._try_make("eat_food", {}, state)) is not None:
                return PlanDecision(eat, "rule", f"生存补给 hp={hp} food={food}")
        if state.in_safe_mode:
            for item in ("stone_sword", "stone_pickaxe", "wooden_pickaxe"):
                if state.count(item) < 1 and can_afford_gear(state, item, assume_no_table=True):
                    if (c := self._try_make("craft_gear", {"item": item}, state)) is not None:
                        return PlanDecision(c, "rule", f"safe_mode 期间原地合成 {item}")
            return PlanDecision(None, "idle", f"safe_mode 避险驻留中（剩 {state.safe_mode_remaining:.0f}s）")
        return None

    def choose_for_objective(self, state: State) -> PlanDecision:
        self._last_selected_goal_id = None
        if (urgent := self.choose_survival_or_safe_mode(state)) is not None:
            return urgent
        self.refresh_environment_blocks(state)
        inst, reason = self.expand_objective_skill(state)
        return PlanDecision(inst, "rule", reason) if inst is not None else PlanDecision(None, "idle", reason)

    def _rank(self, state: State):
        suspended = {g.id for g in self.active_goals if self._environment_blocks and self._block_for_goal(g, state)}
        return rank_actionable_goals(
            state, self.active_goals, completed_advs=self.completed_advancements, target_goal=self.target_goal,
            ep_goal_fails=self.ep_goal_fails, ep_goal_elapsed=self.ep_goal_elapsed,
            telemetry=self.telemetry, suspended_goals=suspended,
        )

    def current_goal_hint(self, state: State) -> str:
        ranked = self._rank(state)
        if not ranked:
            return ""
        top, step_g, root_g = ranked[0], ranked[0].step_goal, ranked[0].root_goal
        gap = diagnose_goal_gap(step_g, state, self.completed_advancements, self.ledger)
        if step_g.id != root_g.id:
            return f"优先推进 {step_g.id}（{step_g.title}，服务于 {root_g.id} 链路 {top.path_score}分/预计{top.path_cost:.0f}s） | {gap['summary']}"
        return f"优先推进 {step_g.id}（{step_g.title}，链路 {top.path_score}分/预计{top.path_cost:.0f}s） | {gap['summary']}"

    def record_skill_outcome(
        self, skill_name: str, *, ok: bool, elapsed_s: float = 0.0, completed_advs: Collection[str] = ()
    ) -> Optional[str]:
        if completed_advs:
            self.completed_advancements.update(completed_advs)
        gid = self._last_selected_goal_id
        matched = self.ledger_map.get(gid) if gid else None
        if matched and matched.skill != skill_name and not any(fb == skill_name for _, fb, _ in matched.gather_fallbacks):
            gid = None
        if gid is None:
            for g in self.active_goals:
                if g.skill == skill_name and not is_goal_stopped_out(g, self.ep_goal_fails, self.ep_goal_elapsed):
                    if not (g.done_advancements and any(a in self.completed_advancements for a in g.done_advancements)):
                        gid = g.id
                        break
        if gid is not None:
            if elapsed_s > 0.0:
                self.ep_goal_elapsed[gid] = self.ep_goal_elapsed.get(gid, 0.0) + float(elapsed_s)
            if not ok:
                self.ep_goal_fails[gid] = self.ep_goal_fails.get(gid, 0) + 1
        self._last_selected_goal_id = None
        return gid

    def choose(self, state: State) -> PlanDecision:
        self._last_selected_goal_id = None
        if (urgent := self.choose_survival_or_safe_mode(state)) is not None:
            return urgent
        self.refresh_environment_blocks(state)
        ranked = self._rank(state)
        if ranked:
            for step in ranked:
                g = step.step_goal
                if all(check_item_expr(state, expr) for expr in g.prereq_items):
                    if (inst := self._try_make(g.skill, g.args_dict, state)) is not None:
                        self._last_selected_goal_id = g.id
                        return PlanDecision(inst, "rule", f"推进目标 {g.id}（{g.title}）")
                else:
                    for req_expr, fb_skill, fb_args in g.gather_fallbacks:
                        if not check_item_expr(state, req_expr):
                            if (inst := self._try_make(fb_skill, dict(fb_args), state)) is not None:
                                self._last_selected_goal_id = g.id
                                return PlanDecision(inst, "rule", f"为目标 {g.id} 补齐前置材料（{req_expr}）")
                            break
            sc_radius = 32 if best_pickaxe(state) not in STONE_OR_BETTER_PICKS else 36
            if (sc := self._try_make("scout_island", {"radius": sc_radius}, state)) is not None:
                return PlanDecision(sc, "rule", f"目标 {ranked[0].step_goal.id} 当前位置受阻或冷却，外迁探索")
            return PlanDecision(None, "idle", f"目标 {ranked[0].step_goal.id} 无符合 verified={self.min_verified} 且可启动的技能")

        if self.target_goal:
            if self.blocked_targets(state, self.target_goal, refresh=False):
                if (scout := self._try_make("scout_island", {"radius": 36}, state)) is not None:
                    return PlanDecision(scout, "rule", "聚焦目标因环境挂起，外迁重新观察", environment_recovery=True)
                return PlanDecision(None, "idle", "等待聚焦目标的环境条件变化")
            return PlanDecision(None, "idle", f"目标 {self.target_goal} 已达成或本轮已止损")

        if total_logs(state) * 4 + total_planks(state) < 8:
            if (gw := self._try_make("gather_wood", {"count": 3}, state)) is not None:
                return PlanDecision(gw, "rule", "维持木料安全库存")
        if total_cobble(state) < 16:
            if (ms := self._try_make("mine_stone", {"count": 8}, state)) is not None:
                return PlanDecision(ms, "rule", "维持圆石垫块与建造库存")
        if (sc := self._try_make("scout_island", {"radius": 36}, state)) is not None:
            return PlanDecision(sc, "rule", "基础石器齐备，持续扫描岛屿结构与矿脉")
        return PlanDecision(None, "idle", f"无符合 verified={self.min_verified} 且可启动的兜底技能")


class CampaignPlanner:
    """薄规划分派器：对外保持 `select()` 等接口不变，按 `strategy` 分派 `legacy` 或委托 `StrategyOrchestrator`。"""

    def __init__(
        self, *, mode: str = "campaign", strategy: str = "rule", focus_skill: Optional[str] = None,
        focus_args: Optional[dict] = None, use_llm: bool = False, llm: Optional[LLMClient] = None,
        min_verified: str = "offline", registry: Optional[dict[str, type[Skill]]] = None,
        on_llm_telemetry: Optional[Callable[[dict], None]] = None,
        on_objective_telemetry: Optional[Callable[[dict], None]] = None,
        target_goal: Optional[str] = None, ledger: tuple[GoalSpec, ...] = GOALS,
        include_stages: Optional[Collection[str]] = None, telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
        review_interval_s: Optional[float] = None, require_iron: Optional[bool] = None, max_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        subgoal_provider: Optional[Callable[[State], Optional[Mapping[str, Any]]]] = None,
        reserve_provider: Optional[Callable[[State], Any]] = None,
    ):
        from .strategy import DEFAULT_STRATEGY_REVIEW_S, StrategyOrchestrator, env_bool, env_float

        if mode not in ("campaign", "focus"):
            raise ValueError(f"mode 只能是 'campaign' 或 'focus'；收到 {mode!r}")
        if strategy not in STRATEGIES:
            raise ValueError(f"strategy 只能是 {STRATEGIES} 之一；收到 {strategy!r}")
        if mode == "focus" and not focus_skill:
            raise ValueError("focus 模式必须指定 focus_skill")

        self.mode, self.strategy, self.use_llm = mode, strategy, use_llm
        self.focus_skill, self.focus_args = focus_skill, dict(focus_args or {})
        self.llm = llm or (LLMClient() if (use_llm or strategy == "llm") else None)
        self.min_verified, self.target_goal = min_verified, target_goal
        self.registry = REGISTRY if registry is None else registry
        self.rules = RulePlanner(
            self.registry, ledger=ledger, include_stages=include_stages,
            target_goal=target_goal, telemetry=telemetry, min_verified=min_verified,
        )
        self.on_llm_telemetry, self.on_objective_telemetry = on_llm_telemetry, on_objective_telemetry
        self.review_interval_s = (
            env_float("MC_STRATEGY_REVIEW_S", DEFAULT_STRATEGY_REVIEW_S) if review_interval_s is None else max(0.0, float(review_interval_s))
        )
        self.require_iron = env_bool("MC_STRATEGY_REQUIRE_IRON", False) if require_iron is None else bool(require_iron)
        self.max_seconds, self._clock = max(0.0, float(max_seconds)), clock
        self._started_mono = self._clock()
        self.subgoal_provider, self.reserve_provider = subgoal_provider, reserve_provider
        self._last_selected_goal_id: Optional[str] = None
        self._strategy: StrategyOrchestrator = StrategyOrchestrator(self, clock=self._clock)

    @property
    def scheduler(self) -> Optional[DecisionScheduler]: return self._strategy.scheduler
    @property
    def current_objective(self) -> Optional[Objective]: return self._strategy.current_objective
    @current_objective.setter
    def current_objective(self, obj: Optional[Objective]) -> None: self._strategy.current_objective = obj
    @property
    def recent_objectives(self) -> deque[ObjectiveRecord]: return self._strategy.recent_objectives

    def set_objective(self, obj: Optional[Objective], *, now: Optional[float] = None) -> None:
        self._strategy.set_objective(obj, now=now)

    def rule_objective(self, state: State, *, exclude_goals: Collection[str] = (), now: Optional[float] = None) -> Optional[Objective]:
        return self._strategy.rule_objective(state, exclude_goals=exclude_goals, now=now)

    def build_context(self, state: State, **kwargs: Any) -> DecisionContext:
        return self._strategy.build_context(state, **kwargs)

    def record_environment_block(self, skill: Skill, error: EnvBlocked, state: State) -> None:
        self.rules.record_environment_block(skill, error, state)
        self._last_selected_goal_id = None

    def blocked_targets(self, state: State, target_goals: Any, *, refresh: bool = True) -> dict[str, EnvBlocked]:
        return self.rules.blocked_targets(state, target_goals, refresh=refresh)

    def record_skill_outcome(self, skill_name: str, *, ok: bool, elapsed_s: float = 0.0, completed_advs: Collection[str] = ()) -> None:
        if self._last_selected_goal_id and self.rules._last_selected_goal_id is None:
            self.rules._last_selected_goal_id = self._last_selected_goal_id
        self.rules.record_skill_outcome(skill_name, ok=ok, elapsed_s=elapsed_s, completed_advs=completed_advs)
        if self.strategy != "legacy":
            self._strategy.on_skill_outcome(skill_name, ok=ok, elapsed_s=elapsed_s)
        self._last_selected_goal_id = None

    def _select_legacy_skill_llm(self, state: State, recent_failures: list[str]) -> PlanDecision:
        from .strategy_async import select_legacy_skill_llm
        return select_legacy_skill_llm(self, state, recent_failures)

    def _select_focus(self, state: State) -> PlanDecision:
        assert self.focus_skill is not None
        self._last_selected_goal_id = self.target_goal if (self.target_goal in self.rules.ledger_map) else None
        if state.in_safe_mode:
            return PlanDecision(None, "idle", f"safe_mode 冷却中（{state.safe_mode_remaining:.0f}s）")
        self.rules.refresh_environment_blocks(state)
        cls = self.registry.get(self.focus_skill)
        if cls is not None and not skill_choice.meets_verification(cls, self.min_verified):
            return PlanDecision(None, "idle", f"focus 技能 {self.focus_skill} 需要 verified={self.min_verified}，当前为 {cls.verified}")
        skill = make(self.focus_skill, self.focus_args, registry=self.registry)
        if (environment := self.rules.environment_block(skill.goal)) is not None:
            return PlanDecision(None, "idle", f"focus 技能环境挂起: {environment}")
        if reason := type(skill).blocked(state):
            return PlanDecision(None, "idle", f"focus 技能前置未满足: {reason}")
        if skill.is_cooling():
            return PlanDecision(None, "idle", f"focus 目标 {skill.goal} 冷却中")
        if not skill.can_start(state):
            return PlanDecision(None, "idle", f"focus 技能参数 {self.focus_args} 当前不可启动")
        return PlanDecision(skill, "focus", f"定向验收 {skill}")

    def select(self, state: State, recent_failures: list[str]) -> PlanDecision:
        if self.mode == "focus":
            return self._select_focus(state)
        if self.strategy == "legacy":
            if self.use_llm:
                return self._select_legacy_skill_llm(state, recent_failures)
            dec = self.rules.choose(state)
            self._last_selected_goal_id = self.rules._last_selected_goal_id
            return dec
        if self.strategy == "rule" and self.use_llm:
            return self._select_legacy_skill_llm(state, recent_failures)
        return self._strategy.select(state, recent_failures)
