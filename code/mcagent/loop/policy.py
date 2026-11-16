"""规划层：战略层（选计分目标 `Objective`）+ 战术层（`RulePlanner` 按依赖闭包展开下一技能）。

关键边界（遵循 AGENTS.md §9 与 J8 / J11 / J17）：
1. 重生后处于哪个科技阶段由本模块从 `State` 实时推导，兼容 `keepInventory` 开或关。
2. 仅保留两种运行模式：`campaign`（正式比赛跑法）与 `focus`（单技能定向回归）。
3. `campaign` 下支持两套战略实现（`--strategy {rule,llm}`，默认 `rule`）：
   - `rule`：用台账性价比排序 `_rank()` 第一名的 `root_goal` 产出 `Objective(source="rule")`；
   - `llm`：只在 8 类目标事件上通过 `DecisionScheduler` 异步调用模型产出 `Objective(source="llm")`，
     当拍立即由规则 `Objective` 先行推进，`select()` 永不阻塞等待模型；迟到合法结果在下一技能边界替换，
     过期或非法结果丢弃。战术层给出空技能时才用技能级模型选择做异步兜底。
4. 兼容既有 `--llm`（`strategy="rule", use_llm=True`）：原样保留同步技能菜单选择供挂机控制台对照。
"""
from __future__ import annotations

import copy
from collections import deque
import math
import os
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Collection, Mapping, Optional

from ..planner import objective, skill_choice
from ..planner.llm import LLMClient, LLMError
from ..planner.objective import Objective
from ..planner.scheduler import DecisionScheduler
from ..runtime import EnvBlocked, State, _inv_fingerprint
from ..skills import REGISTRY, Skill, bind_args, make
from ..skills.items import best_pickaxe
from .context import (
    DecisionContext,
    ObjectiveRecord,
    build_decision_context,
    classify_failure_kind,
    is_daytime,
)
from .goals import (
    DEFAULT_CAMPAIGN_STAGES,
    GOALS,
    OFFICIAL_CAMPAIGN_STAGES,
    STONE_OR_BETTER_PICKS,
    GoalSpec,
    check_item_expr,
    diagnose_goal_gap,
    is_goal_achieved,
    is_goal_stopped_out,
    is_prereq_satisfied,
    normalize_goal_ids,
    rank_actionable_goals,
    resolve_active_goals,
)
from .reserve import ReservationBook
from .starter_skills import (
    PROTECTED_ZONES,
    can_afford_gear,
    total_cobble,
    total_logs,
    total_planks,
)
from .subgoals import item_available, tool_ready

DEFAULT_STRATEGY_REVIEW_S: float = 600.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if math.isfinite(val) and val >= 0.0 else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _clone_state_for_worker(state: State) -> State:
    snap = State(state.config)
    snap.st = state.snapshot()
    snap.inventory = copy.deepcopy(state.inventory)
    snap.inventory_dirty = state.inventory_dirty
    snap.safe_mode_until = state.safe_mode_until
    return snap


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


def _material_plan(skill: Skill, state: State):
    plan = getattr(type(skill), "material_plan", None)
    return plan(skill.args, state) if callable(plan) else ()


def _supplies(goal: GoalSpec, state: State):
    for requirement, name, args in goal.gather_fallbacks:
        if not check_item_expr(state, requirement):
            yield requirement, name, dict(args)
            if not goal.gather_when_blocked:
                return


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
            is_day = status.get("is_day")
            return is_day if isinstance(is_day, bool) else None
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
        if self.error.wake_on in ("day", "night"):
            return current is (self.error.wake_on == "day")
        return previous is not None


def build_state_summary(state: State, *, goal_hint: str = "") -> str:
    """给技能级模型选择（兼容 `--llm`）的一屏状态摘要。战略层上下文见 `loop/context.py`。"""
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
        self,
        registry: Optional[dict[str, type[Skill]]] = None,
        ledger: tuple[GoalSpec, ...] = GOALS,
        include_stages: Optional[Collection[str]] = None,
        target_goal: Optional[str] = None,
        telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
        min_verified: str = "offline",
        reservations: Optional[ReservationBook] = None,
    ):
        self.registry = REGISTRY if registry is None else registry
        self.min_verified = min_verified
        self.reservations = reservations
        self.ledger: tuple[GoalSpec, ...] = tuple(ledger)
        self.ledger_map: dict[str, GoalSpec] = {g.id: g for g in self.ledger}
        default_stages = OFFICIAL_CAMPAIGN_STAGES if min_verified == "live" else DEFAULT_CAMPAIGN_STAGES
        self.include_stages: tuple[str, ...] = tuple(default_stages if include_stages is None else include_stages)
        self.target_goal: Optional[str] = target_goal
        self.telemetry: dict[str, Any] = dict(telemetry) if telemetry else {}
        self.active_goals: tuple[GoalSpec, ...] = resolve_active_goals(
            self.ledger,
            include_stages=self.include_stages,
            target_goal=self.target_goal,
        )
        self.completed_advancements: set[str] = set()
        self.ep_goal_fails: dict[str, int] = {}
        self.ep_goal_elapsed: dict[str, float] = {}
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
                    block = blocked_branch(prereq, seen)
                    if block is not None:
                        return block
            return self._block_for_goal(goal, state)

        targets = {}
        for gid in normalize_goal_ids(target_goals):
            goal = self.ledger_map.get(gid)
            if goal is not None and not is_goal_achieved(goal, state, self.completed_advancements):
                block = blocked_branch(goal, set())
                if block is not None:
                    targets[gid] = block
        return targets

    def _try_make(self, name: str, args: Optional[dict], state: State) -> Optional[Skill]:
        cls = self.registry.get(name)
        if cls is None or not skill_choice.meets_verification(cls, self.min_verified) or cls.blocked(state):
            return None
        inst = cls(**(args or {}))
        if (self.environment_block(inst.goal) or inst.is_cooling() or not inst.can_start(state)
                or not self._materials_ready(inst, state)):
            return None
        return inst

    def _materials_ready(self, inst: Skill, state: State) -> bool:
        return self.reservations is None or self.reservations.can_reserve_plan(
            state, f"candidate:{inst.goal}", _material_plan(inst, state),
        )

    def _diagnose_skill(self, name: str, args: Optional[dict], state: State) -> tuple[Optional[Skill], str, bool]:
        """检查单个技能在当拍是否可启动，返回 `(实例或 None, 失败原因, 是否仅因冷却受阻)`。"""
        cls = self.registry.get(name)
        if cls is None:
            return None, f"技能 {name!r} 未在 REGISTRY 注册", False
        if not skill_choice.meets_verification(cls, self.min_verified):
            return None, f"技能 {name} 验证等级为 {cls.verified}，低于要求 {self.min_verified}", False
        blk = cls.blocked(state)
        if blk:
            return None, f"技能 {name} 前置受阻（{blk}）", False
        bound, arg_errs = bind_args(cls, args or {})
        if arg_errs:
            return None, f"技能 {name} 参数非法（{' | '.join(arg_errs)}）", False
        inst = cls(**bound)
        env_blk = self.environment_block(inst.goal)
        if env_blk is not None:
            return None, f"技能目标 {inst.goal} 被环境挂起（{env_blk}）", False
        if not inst.can_start(state):
            return None, f"技能 {name}({bound}) 当前不可启动（can_start=False）", False
        if inst.is_cooling():
            return None, f"技能 {name}（{inst.goal}）处于失败冷却期", True
        if not self._materials_ready(inst, state):
            return None, f"技能 {name} 的物资被预留、数量不足或库存待刷新", False
        return inst, "", False

    def expand_objective_skill(self, state: State) -> tuple[Optional[Skill], str]:
        """针对 `target_goal` 展开下一个可启动技能；无法展开时返回 `(None, 明确原因)`。"""
        self._last_selected_goal_id = None
        ranked = self._rank(state)
        if not ranked:
            if self.target_goal:
                blocked = self.blocked_targets(state, self.target_goal, refresh=False)
                if blocked:
                    first_err = next(iter(blocked.values()))
                    return None, f"目标链被环境挂起（{first_err}，唤醒={first_err.wake_on}）"
                for g in self.active_goals:
                    if not is_goal_achieved(g, state, self.completed_advancements) and is_goal_stopped_out(
                        g, self.ep_goal_fails, self.ep_goal_elapsed
                    ):
                        return None, f"目标链节点 {g.id} 已触及本轮失败或时间止损上限"
                return None, f"目标 {self.target_goal} 已达成或无未完成步骤"
            return None, "当前台账无未完成目标"

        reasons: list[str] = []
        cooling_step_id: Optional[str] = None
        for step in ranked:
            g = step.step_goal
            ready = all(check_item_expr(state, expr) for expr in g.prereq_items)
            if ready:
                inst, why_not, cooling_only = self._diagnose_skill(g.skill, g.args_dict, state)
                if inst is not None:
                    self._last_selected_goal_id = g.id
                    return inst, f"推进目标 {g.id}（{g.title}）"
                reasons.append(f"{g.id}: {why_not}")
                if cooling_only and cooling_step_id is None:
                    cooling_step_id = g.id
            if not ready or g.gather_when_blocked:
                matched_fb = False
                for req_expr, fb_skill, fb_args in _supplies(g, state):
                    matched_fb = True
                    inst, why_not, cooling_only = self._diagnose_skill(fb_skill, fb_args, state)
                    if inst is not None:
                        self._last_selected_goal_id = g.id
                        return inst, f"为目标 {g.id} 补齐前置材料（{req_expr}）"
                    reasons.append(f"{g.id}({req_expr}): {why_not}")
                    if cooling_only and cooling_step_id is None:
                        cooling_step_id = g.id
                if not ready and not matched_fb:
                    unmet = [expr for expr in g.prereq_items if not check_item_expr(state, expr)]
                    reasons.append(f"{g.id}: 缺少前置物资 {unmet} 且无补料技能")

        if cooling_step_id is not None:
            sc_radius = 32 if best_pickaxe(state) not in STONE_OR_BETTER_PICKS else 36
            sc = self._try_make("scout_island", {"radius": sc_radius}, state)
            if sc is not None:
                return sc, f"目标 {cooling_step_id} 当前位置受阻或冷却，外迁探索"

        return None, "；".join(reasons[:3]) or f"目标 {ranked[0].step_goal.id} 无可启动技能"

    def choose_survival_or_safe_mode(self, state: State) -> Optional[PlanDecision]:
        """在一切目标之上的生存补给与 `safe_mode` 避险处理（命中即返回，否则返回 None）。"""
        hp = float(state.st.get("health", 20))
        food = float(state.st.get("food", 20))

        if food <= 14 or (hp < 18 and food < 20):
            eat = self._try_make("eat_food", {}, state)
            if eat is not None:
                return PlanDecision(eat, "rule", f"生存补给 hp={hp} food={food}")

        if state.in_safe_mode:
            for item in ("stone_sword", "stone_pickaxe", "wooden_pickaxe"):
                if state.count(item) < 1 and can_afford_gear(state, item, assume_no_table=True):
                    c = self._try_make("craft_gear", {"item": item}, state)
                    if c is not None:
                        return PlanDecision(c, "rule", f"safe_mode 期间原地合成 {item}")
            return PlanDecision(None, "idle", f"safe_mode 避险驻留中（剩 {state.safe_mode_remaining:.0f}s）")
        return None

    def choose_for_objective(self, state: State) -> PlanDecision:
        """战术层入口：先处理生存补给与 `safe_mode`，再针对 `target_goal` 展开下一技能。"""
        self._last_selected_goal_id = None
        urgent = self.choose_survival_or_safe_mode(state)
        if urgent is not None:
            return urgent
        self.refresh_environment_blocks(state)
        inst, reason = self.expand_objective_skill(state)
        if inst is not None:
            return PlanDecision(inst, "rule", reason)
        return PlanDecision(None, "idle", reason)

    def _rank(self, state: State):
        suspended = {g.id for g in self.active_goals if self._environment_blocks and self._block_for_goal(g, state)}
        return rank_actionable_goals(
            state,
            self.active_goals,
            completed_advs=self.completed_advancements,
            target_goal=self.target_goal,
            ep_goal_fails=self.ep_goal_fails,
            ep_goal_elapsed=self.ep_goal_elapsed,
            telemetry=self.telemetry,
            suspended_goals=suspended,
        )

    def current_goal_hint(self, state: State) -> str:
        """返回当前最高性价比目标步的一行指引与缺口诊断（仅供旧 `--llm` 技能级模式兼容使用）。"""
        ranked = self._rank(state)
        if not ranked:
            return ""
        top = ranked[0]
        step_g = top.step_goal
        root_g = top.root_goal
        gap = diagnose_goal_gap(step_g, state, self.completed_advancements, self.ledger)
        if step_g.id != root_g.id:
            return (
                f"优先推进 {step_g.id}（{step_g.title}，服务于 {root_g.id} 链路 {top.path_score}分/预计{top.path_cost:.0f}s）"
                f" | {gap['summary']}"
            )
        return (
            f"优先推进 {step_g.id}（{step_g.title}，链路 {top.path_score}分/预计{top.path_cost:.0f}s）"
            f" | {gap['summary']}"
        )

    def record_skill_outcome(
        self,
        skill_name: str,
        *,
        ok: bool,
        elapsed_s: float = 0.0,
        completed_advs: Collection[str] = (),
    ) -> Optional[str]:
        """同步服务端已达成进度，并将本轮技能耗时与失败次数记入对应目标以支持单轮止损。"""
        if completed_advs:
            self.completed_advancements.update(completed_advs)

        gid = self._last_selected_goal_id
        matched_goal = self.ledger_map.get(gid) if gid else None
        if matched_goal is not None:
            valid_for_goal = (
                matched_goal.skill == skill_name
                or any(fb_skill == skill_name for _, fb_skill, _ in matched_goal.gather_fallbacks)
            )
            if not valid_for_goal:
                gid = None

        if gid is None:
            for g in self.active_goals:
                if g.skill == skill_name and not is_goal_stopped_out(g, self.ep_goal_fails, self.ep_goal_elapsed):
                    if g.done_advancements and any(a in self.completed_advancements for a in g.done_advancements):
                        continue
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
        urgent = self.choose_survival_or_safe_mode(state)
        if urgent is not None:
            return urgent

        # 3) 目标台账解析与按性价比顺序派发
        self.refresh_environment_blocks(state)
        ranked = self._rank(state)
        if ranked:
            for step in ranked:
                g = step.step_goal
                ready = all(check_item_expr(state, expr) for expr in g.prereq_items)
                if ready:
                    inst = self._try_make(g.skill, g.args_dict, state)
                    if inst is not None:
                        self._last_selected_goal_id = g.id
                        return PlanDecision(inst, "rule", f"推进目标 {g.id}（{g.title}）")
                if not ready or g.gather_when_blocked:
                    for req_expr, fb_skill, fb_args in _supplies(g, state):
                        inst = self._try_make(fb_skill, fb_args, state)
                        if inst is not None:
                            self._last_selected_goal_id = g.id
                            return PlanDecision(inst, "rule", f"为目标 {g.id} 补齐前置材料（{req_expr}）")

            sc_radius = 32 if best_pickaxe(state) not in STONE_OR_BETTER_PICKS else 36
            sc = self._try_make("scout_island", {"radius": sc_radius}, state)
            if sc is not None:
                return PlanDecision(sc, "rule", f"目标 {ranked[0].step_goal.id} 当前位置受阻或冷却，外迁探索")
            return PlanDecision(None, "idle",
                                f"目标 {ranked[0].step_goal.id} 无符合 verified={self.min_verified} 且可启动的技能")

        if self.target_goal:
            if self.blocked_targets(state, self.target_goal, refresh=False):
                scout = self._try_make("scout_island", {"radius": 36}, state)
                if scout is not None:
                    return PlanDecision(scout, "rule", "聚焦目标因环境挂起，外迁重新观察", environment_recovery=True)
                return PlanDecision(None, "idle", "等待聚焦目标的环境条件变化")
            return PlanDecision(None, "idle", f"目标 {self.target_goal} 已达成或本轮已止损")

        # 常规战役模式：已激活台账目标全部达成后维持基础物资安全库存并持续探索
        if total_logs(state) * 4 + total_planks(state) < 8:
            gw = self._try_make("gather_wood", {"count": 3}, state)
            if gw is not None:
                return PlanDecision(gw, "rule", "维持木料安全库存")

        if total_cobble(state) < 16:
            ms = self._try_make("mine_stone", {"count": 8}, state)
            if ms is not None:
                return PlanDecision(ms, "rule", "维持圆石垫块与建造库存")

        sc = self._try_make("scout_island", {"radius": 36}, state)
        if sc is not None:
            return PlanDecision(sc, "rule", "基础石器齐备，持续扫描岛屿结构与矿脉")
        return PlanDecision(None, "idle", f"无符合 verified={self.min_verified} 且可启动的兜底技能")


class CampaignPlanner:
    """统一规划入口：支持战略/战术两段式目标规划（`--strategy {rule,llm}`）、旧 `--llm` 兼容与 `focus` 模式。"""

    def __init__(
        self,
        *,
        mode: str = "campaign",
        strategy: str = "rule",
        focus_skill: Optional[str] = None,
        focus_args: Optional[dict] = None,
        use_llm: bool = False,
        llm: Optional[LLMClient] = None,
        min_verified: str = "offline",
        registry: Optional[dict[str, type[Skill]]] = None,
        on_llm_telemetry: Optional[Callable[[dict], None]] = None,
        on_objective_telemetry: Optional[Callable[[dict], None]] = None,
        target_goal: Optional[str] = None,
        ledger: tuple[GoalSpec, ...] = GOALS,
        include_stages: Optional[Collection[str]] = None,
        telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
        review_interval_s: Optional[float] = None,
        require_iron: Optional[bool] = None,
        max_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        subgoal_provider: Optional[Callable[[State], Optional[Mapping[str, Any]]]] = None,
        reserve_provider: Optional[Callable[[State], Any]] = None,
    ):
        if mode not in ("campaign", "focus"):
            raise ValueError(f"mode 只能是 'campaign' 或 'focus'；收到 {mode!r}")
        if strategy not in ("rule", "llm"):
            raise ValueError(f"strategy 只能是 'rule' 或 'llm'；收到 {strategy!r}")
        if mode == "focus" and not focus_skill:
            raise ValueError("focus 模式必须指定 focus_skill")

        self.mode = mode
        self.strategy = strategy
        self.focus_skill = focus_skill
        self.focus_args = dict(focus_args or {})
        self.use_llm = use_llm
        self.llm = llm or (LLMClient() if (use_llm or strategy == "llm") else None)
        self.min_verified = min_verified
        self.registry = REGISTRY if registry is None else registry
        self.target_goal = target_goal
        self.reservations = ReservationBook()
        self._active_material_consumer: Optional[str] = None
        self.rules = RulePlanner(
            self.registry,
            ledger=ledger,
            include_stages=include_stages,
            target_goal=target_goal,
            telemetry=telemetry,
            min_verified=min_verified,
            reservations=self.reservations,
        )
        self.on_llm_telemetry = on_llm_telemetry
        self.on_objective_telemetry = on_objective_telemetry
        self.review_interval_s = (
            _env_float("MC_STRATEGY_REVIEW_S", DEFAULT_STRATEGY_REVIEW_S)
            if review_interval_s is None
            else max(0.0, float(review_interval_s))
        )
        self.require_iron = _env_bool("MC_STRATEGY_REQUIRE_IRON", False) if require_iron is None else bool(require_iron)
        self.max_seconds = max(0.0, float(max_seconds))
        self._clock = clock
        self._started_mono = self._clock()
        self.subgoal_provider = subgoal_provider
        self.reserve_provider = reserve_provider

        self.current_objective: Optional[Objective] = None
        self._tracked_objective: Optional[Objective] = None
        self._objective_skill_elapsed_s: float = 0.0
        self._last_objective_decision_at: float = float("-inf")
        self.recent_objectives: deque[ObjectiveRecord] = deque(maxlen=10)
        self._consecutive_goal_fails: dict[str, int] = {}
        self._last_failure_kind: dict[str, str] = {}
        self._last_failure_detail: dict[str, str] = {}
        self._deaths_initialized = False
        self._last_deaths_marker: tuple[int, Optional[float]] = (0, None)
        self._last_is_day: Optional[bool] = None
        self._deferred_trigger: Optional[str] = None
        self._last_selected_goal_id: Optional[str] = None
        self._seen_failure_str: str = ""

        self._scheduler: Optional[DecisionScheduler] = None
        if self.strategy == "llm":
            self._scheduler = DecisionScheduler(
                lambda _summary: None,
                require_iron=self.require_iron,
                clock=self._clock,
            )

    @property
    def scheduler(self) -> Optional[DecisionScheduler]:
        return self._scheduler

    def _apply_objective(self, obj: Optional[Objective], *, now: Optional[float] = None) -> None:
        t_now = self._clock() if now is None else float(now)
        self.current_objective = obj
        self._tracked_objective = obj
        self._objective_skill_elapsed_s = 0.0
        if obj is not None:
            self._last_objective_decision_at = t_now

    def set_objective(self, obj: Optional[Objective], *, now: Optional[float] = None) -> None:
        """显式设置当前 `Objective`（支持规则战略与模型战略的 `Objective` 直接互换注入，并清空旧预取）。"""
        if self._scheduler is not None:
            self._scheduler.invalidate()
        self._apply_objective(obj, now=now)

    def _sync_external_objective_assignment(self) -> None:
        if self.current_objective is not self._tracked_objective:
            self._tracked_objective = self.current_objective
            self._objective_skill_elapsed_s = 0.0
            if self.current_objective is not None and self._last_objective_decision_at == float("-inf"):
                self._last_objective_decision_at = self.current_objective.started_at

    def _current_objective_elapsed(self, now: float) -> float:
        if self.current_objective is None:
            return 0.0
        wall_dt = max(0.0, now - self.current_objective.started_at)
        return max(wall_dt, self._objective_skill_elapsed_s)

    def _make_tactical_planner(self, goal_id: str) -> RulePlanner:
        tp = RulePlanner(
            self.registry,
            ledger=self.rules.ledger,
            include_stages=self.rules.include_stages,
            target_goal=goal_id,
            telemetry=self.rules.telemetry,
            min_verified=self.min_verified,
            reservations=self.reservations,
        )
        tp.completed_advancements = self.rules.completed_advancements
        tp.ep_goal_fails = self.rules.ep_goal_fails
        tp.ep_goal_elapsed = self.rules.ep_goal_elapsed
        tp._environment_blocks = self.rules._environment_blocks
        return tp

    def _is_iron_complete(self, state: State) -> bool:
        iron_goal = self.rules.ledger_map.get("smelt_iron")
        if iron_goal is not None and is_goal_achieved(iron_goal, state, self.rules.completed_advancements):
            return True
        return state.count("iron_ingot") >= 1 or state.count("iron_pickaxe") >= 1

    def rule_objective(
        self,
        state: State,
        *,
        exclude_goals: Collection[str] = (),
        now: Optional[float] = None,
    ) -> Optional[Objective]:
        """规则战略：取现有 `_rank()` 性价比第一名且战术可展开的 `root_goal` 封装为 `Objective`。"""
        t_now = self._clock() if now is None else float(now)
        excluded = set(exclude_goals)
        self.rules.refresh_environment_blocks(state)
        ranked = self.rules._rank(state)
        seen_roots: set[str] = set()
        fallback_first: Optional[Objective] = None
        for step in ranked:
            root = step.root_goal
            if root.id in seen_roots or root.id in excluded:
                continue
            seen_roots.add(root.id)
            candidate_obj = Objective(
                goal_id=root.id,
                budget_s=objective.clamp_budget(root.budget_s),
                why=f"规则性价比第1（{step.path_score}分/{step.path_cost:.0f}s）",
                source="rule",
                started_at=t_now,
            )
            if fallback_first is None:
                fallback_first = candidate_obj
            skill, _ = self._make_tactical_planner(root.id).expand_objective_skill(state)
            if skill is not None:
                return candidate_obj
        return None

    def build_context(
        self,
        state: State,
        *,
        current_objective: Optional[Objective] = None,
        current_objective_elapsed_s: Optional[float] = None,
        now: Optional[float] = None,
    ) -> DecisionContext:
        """为当前 `State` 组装一份战略层 `DecisionContext`。"""
        t_now = self._clock() if now is None else float(now)
        obj = self.current_objective if current_objective is None else current_objective
        obj_elapsed = (
            self._current_objective_elapsed(t_now)
            if current_objective_elapsed_s is None
            else float(current_objective_elapsed_s)
        )
        subgoals = self.subgoal_provider(state) if self.subgoal_provider is not None else self._subgoal_snapshot(state)
        reserves = self.reserve_provider(state) if self.reserve_provider is not None else self._reservation_snapshot(state)
        return build_decision_context(
            state,
            ledger=self.rules.ledger,
            registry=self.registry,
            min_verified=self.min_verified,
            include_stages=self.rules.include_stages,
            focus_goals=self.target_goal,
            completed_advs=self.rules.completed_advancements,
            ep_goal_fails=self.rules.ep_goal_fails,
            ep_goal_elapsed=self.rules.ep_goal_elapsed,
            telemetry=self.rules.telemetry,
            environment_blocks=self.rules._environment_blocks,
            current_objective=obj,
            current_objective_elapsed_s=obj_elapsed,
            recent_objectives=tuple(self.recent_objectives),
            elapsed_match_s=max(0.0, t_now - self._started_mono),
            max_match_seconds=self.max_seconds,
            subgoal_predicates=subgoals,
            reservations=reserves,
        )

    def _subgoal_snapshot(self, state: State) -> dict[str, Any]:
        return {
            "item_available": {
                item: item_available(state, item, count, allocatable=self.reservations.available(state, item))
                for item, count in (("iron_ingot", 3), ("diamond", 4), ("wheat_seeds", 3))
            },
            "tool_ready": {tier: tool_ready(state, "pickaxe", tier, 1) for tier in ("iron", "diamond")},
            **dict.fromkeys(("station_ready", "landmark_reachable", "safe_encounter", "active_portal"), "待现场观察"),
        }

    def _reservation_snapshot(self, state: State) -> dict[str, Any]:
        return {
            "有效": self.reservations.observe(state) and not state.inventory_dirty,
            "账项": [f"{r.item}×{r.count}({r.mode})" for r in self.reservations.entries],
        }

    def release_skill_materials(self) -> None:
        if self._active_material_consumer is not None:
            self.reservations.release(self._active_material_consumer)
            self._active_material_consumer = None

    def _emit_objective_telemetry(
        self,
        *,
        trigger: str,
        source: str,
        obj: Optional[Objective],
        ms: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        errors: Sequence[str] = (),
        adopted: bool = True,
        replaced_goal_id: Optional[str] = None,
    ) -> None:
        if self.on_objective_telemetry is None:
            return
        total_tok = int(prompt_tokens) + int(completion_tokens)
        cum_prompt = int(self.llm.stats.get("prompt_tokens", 0)) if self.llm is not None else 0
        cum_comp = int(self.llm.stats.get("completion_tokens", 0)) if self.llm is not None else 0
        self.on_objective_telemetry({
            "trigger": trigger,
            "source": source,
            "goal_id": obj.goal_id if obj is not None else None,
            "budget_s": obj.budget_s if obj is not None else None,
            "why": obj.why if obj is not None else "",
            "ms": round(float(ms), 1),
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "total_tokens": total_tok,
            "cumulative_prompt_tokens": cum_prompt,
            "cumulative_completion_tokens": cum_comp,
            "cumulative_total_tokens": cum_prompt + cum_comp,
            "errors": list(errors),
            "adopted": bool(adopted),
            "replaced_goal_id": replaced_goal_id,
        })

    def _invalidate_pending_async(self, reason: str) -> None:
        if self._scheduler is None:
            return
        old = self._scheduler.invalidate()
        if old is not None and (old.get("meta") or {}).get("kind") == "objective":
            meta = old.get("meta") or {}
            self._emit_objective_telemetry(
                trigger=str(meta.get("trigger") or "invalidated"),
                source="llm_fallback_rule",
                obj=self.current_objective,
                ms=0.0,
                errors=[f"异步结果因状态突变作废: {reason}"],
                adopted=False,
                replaced_goal_id=None,
            )

    def record_environment_block(self, skill: Skill, error: EnvBlocked, state: State) -> None:
        self.release_skill_materials()
        self.rules.record_environment_block(skill, error, state)
        self._last_selected_goal_id = None

    def blocked_targets(self, state: State, target_goals: Any, *, refresh: bool = True) -> dict[str, EnvBlocked]:
        return self.rules.blocked_targets(state, target_goals, refresh=refresh)

    def record_skill_outcome(
        self,
        skill_name: str,
        *,
        ok: bool,
        elapsed_s: float = 0.0,
        completed_advs: Collection[str] = (),
    ) -> None:
        self.release_skill_materials()
        self._sync_external_objective_assignment()
        if self._last_selected_goal_id and self.rules._last_selected_goal_id is None:
            self.rules._last_selected_goal_id = self._last_selected_goal_id
        self.rules.record_skill_outcome(
            skill_name,
            ok=ok,
            elapsed_s=elapsed_s,
            completed_advs=completed_advs,
        )
        if self.current_objective is not None:
            gid = self.current_objective.goal_id
            if elapsed_s > 0.0:
                self._objective_skill_elapsed_s += float(elapsed_s)
            spec = self.rules.ledger_map.get(gid)
            if not ok:
                self._consecutive_goal_fails[gid] = self._consecutive_goal_fails.get(gid, 0) + 1
                self._last_failure_kind.setdefault(gid, "other_replan")
            elif (spec is not None and spec.skill == skill_name) or skill_name not in ("eat_food", "scout_island"):
                self._consecutive_goal_fails[gid] = 0
        self._last_selected_goal_id = None

    def _record_objective_history(
        self,
        obj: Objective,
        *,
        outcome: str,
        failure_kind: str,
        now: float,
        detail: str = "",
    ) -> None:
        self.recent_objectives.append(
            ObjectiveRecord(
                goal_id=obj.goal_id,
                outcome=outcome,
                failure_kind=failure_kind,
                elapsed_s=round(self._current_objective_elapsed(now), 1),
                detail=detail,
            )
        )

    def _dispatch_async_objective(
        self,
        state: State,
        *,
        trigger: str,
        prev_objective: Optional[Objective],
        prev_elapsed_s: float,
        now: float,
    ) -> bool:
        if self._scheduler is None or self.llm is None:
            return False
        self._scheduler.config = state.config
        ctx = self.build_context(
            state,
            current_objective=prev_objective,
            current_objective_elapsed_s=prev_elapsed_s,
            now=now,
        )
        if not ctx.eligible_goals:
            return False
        rendered_ctx = ctx.render()
        state_snap = _clone_state_for_worker(state)
        completed_snap = set(self.rules.completed_advancements)
        fails_snap = dict(self.rules.ep_goal_fails)
        elapsed_snap = dict(self.rules.ep_goal_elapsed)
        blocks_snap = {
            k: _EnvironmentBlock(v.error, v.observation)
            for k, v in self.rules._environment_blocks.items()
        }
        timeout_budget = max(0.05, float(state.config.model_timeout_s))
        llm_client = self.llm

        def _worker_ask(prompt_text: str) -> dict[str, Any]:
            t0 = self._clock()
            usage: dict[str, int] = {}
            chosen_obj, errs, used = objective.choose_objective(
                llm_client,
                prompt_text,
                state_snap,
                ledger=self.rules.ledger,
                registry=self.registry,
                min_verified=self.min_verified,
                include_stages=self.rules.include_stages,
                focus_goals=self.target_goal,
                completed_advs=completed_snap,
                ep_goal_fails=fails_snap,
                ep_goal_elapsed=elapsed_snap,
                telemetry=self.rules.telemetry,
                environment_blocks=blocks_snap,
                attempts=2,
                thinking=False,
                total_timeout=timeout_budget,
                usage_out=usage,
            )
            dt_ms = round(max(0.0, self._clock() - t0) * 1000.0, 1)
            return {
                "raw_choice": (
                    {"goal_id": chosen_obj.goal_id, "budget_s": chosen_obj.budget_s, "why": chosen_obj.why}
                    if chosen_obj is not None
                    else None
                ),
                "errors": list(errs),
                "attempts": used,
                "ms": dt_ms,
                "usage": usage,
            }

        return self._scheduler.notify(
            "objective",
            rendered_ctx,
            iron_complete=self._is_iron_complete(state),
            meta={"kind": "objective", "trigger": trigger},
            ask_override=_worker_ask,
        )

    def _dispatch_async_skill_fallback(self, state: State, recent_failures: list[str]) -> bool:
        if self._scheduler is None or self.llm is None:
            return False
        ready_registry = {
            name: cls for name, cls in self.registry.items()
            if self.rules.environment_block(cls.goal) is None
        }
        if not skill_choice.has_ready_skill(state, ready_registry, self.min_verified):
            return False
        self._scheduler.config = state.config
        state_snap = _clone_state_for_worker(state)
        summary = build_state_summary(state_snap)
        fail_str = "\n".join(f"- {x}" for x in recent_failures[-5:])
        timeout_budget = max(0.05, float(state.config.model_timeout_s))
        llm_client = self.llm

        def _worker_skill(_prompt: str) -> dict[str, Any]:
            t0 = self._clock()
            usage: dict[str, int] = {}
            errors: list[str] = []
            used = 0
            chosen: Optional[Skill] = None
            try:
                chosen, errors, used = skill_choice.choose(
                    llm_client,
                    summary,
                    state_snap,
                    recent_failures=fail_str,
                    registry=ready_registry,
                    min_verified=self.min_verified,
                    attempts=2,
                    thinking=False,
                    total_timeout=timeout_budget,
                    usage_out=usage,
                )
            except LLMError as exc:
                errors = [f"LLMError: {exc}"]
                used = max(1, used)
            dt_ms = round(max(0.0, self._clock() - t0) * 1000.0, 1)
            return {
                "choice": {"skill": chosen.name, "args": dict(chosen.args), "why": "战术兜底"} if chosen else None,
                "errors": list(errors),
                "attempts": used,
                "ms": dt_ms,
                "usage": usage,
            }

        return self._scheduler.notify(
            "uncovered",
            summary,
            iron_complete=self._is_iron_complete(state),
            meta={"kind": "skill_fallback", "trigger": "no_tactical_skill"},
            ask_override=_worker_skill,
        )

    def _poll_async_scheduler(self, state: State, now: float) -> Optional[PlanDecision]:
        """在技能边界非阻塞检查 `DecisionScheduler` 是否已有完成的战略目标或兜底技能。"""
        if self._scheduler is None:
            return None
        polled = self._scheduler.poll_detailed()
        if polled is None or polled["status"] == "running":
            return None

        meta = polled.get("meta") or {}
        kind = meta.get("kind")
        trigger = str(meta.get("trigger") or "async")
        status = polled["status"]

        if kind == "objective":
            if status != "ready" or not isinstance(polled["value"], dict):
                err_msg = polled.get("error") or f"异步目标决策未就绪 ({status})"
                self._emit_objective_telemetry(
                    trigger=trigger,
                    source="llm_fallback_rule",
                    obj=self.current_objective,
                    ms=float(polled.get("elapsed_ms") or 0.0),
                    errors=[str(err_msg)],
                    adopted=False,
                    replaced_goal_id=None,
                )
                return None

            payload = polled["value"]
            usage = payload.get("usage") or {}
            p_tok = int(usage.get("prompt_tokens", 0))
            c_tok = int(usage.get("completion_tokens", 0))
            dt_ms = float(payload.get("ms") or polled.get("elapsed_ms") or 0.0)
            raw_choice = payload.get("raw_choice")
            if raw_choice is None:
                self._emit_objective_telemetry(
                    trigger=trigger,
                    source="llm_fallback_rule",
                    obj=self.current_objective,
                    ms=dt_ms,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    errors=payload.get("errors") or ["模型未产出合法 Objective"],
                    adopted=False,
                    replaced_goal_id=None,
                )
                return None

            # 在当前技能边界按最新世界状态二次校验（防止执行期间已达成或已挂起的过期结果混入）
            validated, boundary_errs = objective.resolve_objective(
                raw_choice,
                state,
                ledger=self.rules.ledger,
                registry=self.registry,
                min_verified=self.min_verified,
                include_stages=self.rules.include_stages,
                focus_goals=self.target_goal,
                completed_advs=self.rules.completed_advancements,
                ep_goal_fails=self.rules.ep_goal_fails,
                ep_goal_elapsed=self.rules.ep_goal_elapsed,
                telemetry=self.rules.telemetry,
                environment_blocks=self.rules._environment_blocks,
                source="llm",
                now=now,
            )
            if validated is None:
                self._emit_objective_telemetry(
                    trigger=trigger,
                    source="llm_fallback_rule",
                    obj=self.current_objective,
                    ms=dt_ms,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    errors=boundary_errs,
                    adopted=False,
                    replaced_goal_id=None,
                )
                return None

            old_gid = self.current_objective.goal_id if self.current_objective is not None else None
            if self.current_objective is not None and old_gid != validated.goal_id:
                self._record_objective_history(
                    self.current_objective, outcome="switched", failure_kind="none", now=now, detail="采纳模型目标"
                )
            self._apply_objective(validated, now=now)
            self._emit_objective_telemetry(
                trigger=trigger,
                source="llm",
                obj=validated,
                ms=dt_ms,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                errors=[],
                adopted=True,
                replaced_goal_id=old_gid,
            )
            return None

        if kind == "skill_fallback" and status == "ready" and isinstance(polled["value"], dict):
            payload = polled["value"]
            usage = payload.get("usage") or {}
            p_tok = int(usage.get("prompt_tokens", 0))
            c_tok = int(usage.get("completion_tokens", 0))
            dt_ms = float(payload.get("ms") or polled.get("elapsed_ms") or 0.0)
            choice_obj = payload.get("choice")
            ready_registry = {
                name: cls for name, cls in self.registry.items()
                if self.rules.environment_block(cls.goal) is None
            }
            chosen_skill: Optional[Skill] = None
            errs: list[str] = list(payload.get("errors") or [])
            if isinstance(choice_obj, dict):
                ok_choice, v_errs = skill_choice.validate_choice(
                    choice_obj, state, ready_registry, self.min_verified
                )
                if ok_choice:
                    chosen_skill = skill_choice.from_choice(
                        choice_obj, state, ready_registry, self.min_verified
                    )
                else:
                    errs.extend(v_errs)
            if self.on_llm_telemetry is not None:
                cum_p = int(self.llm.stats.get("prompt_tokens", 0)) if self.llm else 0
                cum_c = int(self.llm.stats.get("completion_tokens", 0)) if self.llm else 0
                self.on_llm_telemetry({
                    "ms": dt_ms,
                    "ok": chosen_skill is not None,
                    "skill": chosen_skill.name if chosen_skill else None,
                    "args": chosen_skill.args if chosen_skill else None,
                    "attempts": int(payload.get("attempts") or 0),
                    "errors": errs,
                    "prompt_tokens": p_tok,
                    "completion_tokens": c_tok,
                    "total_tokens": p_tok + c_tok,
                    "cumulative_prompt_tokens": cum_p,
                    "cumulative_completion_tokens": cum_c,
                    "cumulative_total_tokens": cum_p + cum_c,
                })
            if chosen_skill is not None:
                return PlanDecision(
                    chosen_skill,
                    "llm",
                    f"战术层无技能，LLM 兜底选择 {chosen_skill}",
                    dt_ms,
                    int(payload.get("attempts") or 1),
                    tuple(errs),
                    p_tok,
                    c_tok,
                    objective=self.current_objective,
                    objective_source=self.current_objective.source if self.current_objective else None,
                )
        return None

    def _detect_trigger(
        self,
        state: State,
        now: float,
        *,
        died_since_last: bool,
        day_night_flipped: bool,
    ) -> Optional[tuple[str, str, str]]:
        """检查是否命中换目标的触发时机（第 8 条「战术层无技能」在展开时判定）。"""
        if self._deferred_trigger:
            trig = self._deferred_trigger
            self._deferred_trigger = None
            return trig, ("respawn" if trig == "respawn" else "switched"), (
                "combat_or_death" if trig == "respawn" else "none"
            )
        if died_since_last:
            return "respawn", "respawn", "combat_or_death"
        if day_night_flipped:
            return "day_night_changed", "switched", "none"
        if self.current_objective is None:
            return "initial", "none", "none"

        spec = self.rules.ledger_map.get(self.current_objective.goal_id)
        if spec is None:
            return "no_tactical_skill", "no_skill", "missing_prereq"

        if is_goal_achieved(spec, state, self.rules.completed_advancements):
            return "goal_achieved", "achieved", "none"

        if self.rules.blocked_targets(state, spec.id, refresh=False):
            return "env_blocked", "env_blocked", "env_blocked"

        if self._current_objective_elapsed(now) >= self.current_objective.budget_s:
            return "budget_exceeded", "budget_exceeded", "timeout"

        if spec.max_fails_per_ep > 0 and self._consecutive_goal_fails.get(spec.id, 0) >= spec.max_fails_per_ep:
            f_kind = self._last_failure_kind.get(spec.id, "other_replan")
            return "max_fails_reached", "max_fails", f_kind

        if self.review_interval_s > 0.0 and (now - self._last_objective_decision_at) >= self.review_interval_s:
            return "periodic_review", "switched", "none"

        return None

    def _switch_objective(
        self,
        state: State,
        *,
        trigger: str,
        old_outcome: str,
        failure_kind: str,
        now: float,
        exclude_goals: Collection[str] = (),
    ) -> None:
        prev_obj = self.current_objective
        prev_elapsed = self._current_objective_elapsed(now)
        replaced_gid = prev_obj.goal_id if prev_obj is not None else None
        excluded = set(exclude_goals)

        if prev_obj is not None:
            detail = self._last_failure_detail.get(prev_obj.goal_id, "") if failure_kind != "none" else ""
            self._record_objective_history(
                prev_obj, outcome=old_outcome, failure_kind=failure_kind, now=now, detail=detail
            )
            spec = self.rules.ledger_map.get(prev_obj.goal_id)
            if trigger == "max_fails_reached" and spec is not None and spec.max_fails_per_ep > 0:
                self.rules.ep_goal_fails[spec.id] = max(
                    self.rules.ep_goal_fails.get(spec.id, 0), spec.max_fails_per_ep
                )
                excluded.add(spec.id)
                self._consecutive_goal_fails[spec.id] = 0
            elif trigger == "budget_exceeded" and spec is not None:
                self.rules.ep_goal_elapsed[spec.id] = max(
                    self.rules.ep_goal_elapsed.get(spec.id, 0.0), prev_obj.budget_s
                )
                excluded.add(spec.id)

        rule_obj = self.rule_objective(state, exclude_goals=excluded, now=now)
        if rule_obj is None and excluded and trigger == "budget_exceeded":
            # 若台账中仅剩该目标且未配硬止损（stop_loss=False），重批一轮新预算继续推进而不空转
            rule_obj = self.rule_objective(state, now=now)

        self._apply_objective(rule_obj, now=now)
        self._last_objective_decision_at = now
        self._emit_objective_telemetry(
            trigger=trigger,
            source="rule",
            obj=rule_obj,
            ms=0.0,
            adopted=rule_obj is not None,
            replaced_goal_id=replaced_gid,
        )

        if self.strategy == "llm":
            self._dispatch_async_objective(
                state,
                trigger=trigger,
                prev_objective=prev_obj,
                prev_elapsed_s=prev_elapsed,
                now=now,
            )

    def _select_legacy_skill_llm(self, state: State, recent_failures: list[str]) -> PlanDecision:
        """保留原有 `--llm`（`strategy="rule", use_llm=True`）同步技能菜单选择路径，确保挂机工具链完全兼容。"""
        if self.llm is None or state.in_safe_mode:
            dec = self.rules.choose(state)
            self._last_selected_goal_id = self.rules._last_selected_goal_id
            return dec
        self.rules.refresh_environment_blocks(state)
        ready_registry = {
            name: cls for name, cls in self.registry.items()
            if self.rules.environment_block(cls.goal) is None
        }
        if not skill_choice.has_ready_skill(state, ready_registry, self.min_verified):
            dec = self.rules.choose(state)
            self._last_selected_goal_id = self.rules._last_selected_goal_id
            return dec

        goal_hint = self.rules.current_goal_hint(state)
        summary = build_state_summary(state, goal_hint=goal_hint)
        fail_str = "\n".join(f"- {x}" for x in recent_failures[-5:])
        t0 = time.monotonic()
        errors: list[str] = []
        used = 0
        chosen: Optional[Skill] = None
        usage: dict[str, int] = {}
        try:
            chosen, errors, used = skill_choice.choose(
                self.llm,
                summary,
                state,
                recent_failures=fail_str,
                registry=ready_registry,
                min_verified=self.min_verified,
                attempts=2,
                thinking=False,
                total_timeout=state.config.model_timeout_s * 2,
                usage_out=usage,
            )
        except LLMError as e:
            errors = [f"LLMError: {e}"]
            used = max(1, used)
        if chosen is not None and self.rules.environment_block(chosen.goal):
            errors.append(f"{chosen.goal} 环境条件尚未变化")
            chosen = None
        dt_ms = round((time.monotonic() - t0) * 1000.0, 1)
        prompt_tok = int(usage.get("prompt_tokens", 0))
        comp_tok = int(usage.get("completion_tokens", 0))
        total_tok = int(usage.get("total_tokens", prompt_tok + comp_tok))
        cum_prompt = int(usage.get("cumulative_prompt_tokens", self.llm.stats.get("prompt_tokens", 0)))
        cum_comp = int(usage.get("cumulative_completion_tokens", self.llm.stats.get("completion_tokens", 0)))
        cum_total = int(usage.get("cumulative_total_tokens", cum_prompt + cum_comp))

        if self.on_llm_telemetry is not None:
            self.on_llm_telemetry({
                "ms": dt_ms,
                "ok": chosen is not None,
                "skill": chosen.name if chosen else None,
                "args": chosen.args if chosen else None,
                "attempts": used,
                "errors": list(errors),
                "prompt_tokens": prompt_tok,
                "completion_tokens": comp_tok,
                "total_tokens": total_tok,
                "cumulative_prompt_tokens": cum_prompt,
                "cumulative_completion_tokens": cum_comp,
                "cumulative_total_tokens": cum_total,
            })

        if chosen is not None:
            matched_gid: Optional[str] = None
            ranked = self.rules._rank(state)
            for step in ranked:
                g = step.step_goal
                if g.skill == chosen.name and (not g.args_dict or g.args_dict == chosen.args):
                    matched_gid = g.id
                    break
                if any(fb_s == chosen.name for _, fb_s, _ in g.gather_fallbacks):
                    matched_gid = g.id
                    break
            if matched_gid is None:
                for g in self.rules.active_goals:
                    if g.skill == chosen.name and not is_goal_achieved(g, state, self.rules.completed_advancements):
                        matched_gid = g.id
                        break
            self._last_selected_goal_id = matched_gid
            self.rules._last_selected_goal_id = matched_gid
            return PlanDecision(
                chosen, "llm", f"LLM 选择 {chosen}",
                dt_ms, used, tuple(errors), prompt_tok, comp_tok,
                environment_recovery=(
                    chosen.name == "scout_island" and not ranked
                    and bool(self.rules.blocked_targets(state, self.target_goal, refresh=False))
                ),
            )

        fallback = self.rules.choose(state)
        self._last_selected_goal_id = self.rules._last_selected_goal_id
        return PlanDecision(
            fallback.skill,
            "llm_fallback_rule" if fallback.skill else "idle",
            f"LLM 未产出合法选择（{'; '.join(errors[:2])}）-> 规则兜底: {fallback.reason}",
            dt_ms,
            used,
            tuple(errors),
            prompt_tok,
            comp_tok,
            environment_recovery=fallback.environment_recovery,
        )

    def select(self, state: State, recent_failures: list[str]) -> PlanDecision:
        self.release_skill_materials()
        if not self.reservations.observe(state) and not state.inventory_dirty:
            self.reservations.reset(state)
        choice = self._select_skill(state, recent_failures)
        if choice.skill is None or not (plan := _material_plan(choice.skill, state)):
            return choice
        consumer = f"{self._last_selected_goal_id or 'skill'}:{choice.skill.goal}"
        if not self.reservations.reserve_plan(state, consumer, plan):
            return replace(choice, skill=None, source="idle", reason="技能所需物资不足、已被预留或库存待刷新")
        self._active_material_consumer = consumer
        return choice

    def _select_skill(self, state: State, recent_failures: list[str]) -> PlanDecision:
        if self.mode == "focus":
            assert self.focus_skill is not None
            self._last_selected_goal_id = self.target_goal if (self.target_goal in self.rules.ledger_map) else None
            if state.in_safe_mode:
                return PlanDecision(None, "idle", f"safe_mode 冷却中（{state.safe_mode_remaining:.0f}s）")
            self.rules.refresh_environment_blocks(state)
            cls = self.registry.get(self.focus_skill)
            if cls is not None and not skill_choice.meets_verification(cls, self.min_verified):
                return PlanDecision(
                    None, "idle", f"focus 技能 {self.focus_skill} 需要 verified={self.min_verified}，当前为 {cls.verified}"
                )
            skill = make(self.focus_skill, self.focus_args, registry=self.registry)
            environment = self.rules.environment_block(skill.goal)
            if environment is not None:
                return PlanDecision(None, "idle", f"focus 技能环境挂起: {environment}")
            reason = type(skill).blocked(state)
            if reason:
                return PlanDecision(None, "idle", f"focus 技能前置未满足: {reason}")
            if skill.is_cooling():
                return PlanDecision(None, "idle", f"focus 目标 {skill.goal} 冷却中")
            if not skill.can_start(state):
                return PlanDecision(None, "idle", f"focus 技能参数 {self.focus_args} 当前不可启动")
            return PlanDecision(skill, "focus", f"定向验收 {skill}")

        if self.strategy == "rule" and self.use_llm:
            return self._select_legacy_skill_llm(state, recent_failures)

        self._sync_external_objective_assignment()
        now = self._clock()
        self.rules.refresh_environment_blocks(state)

        if recent_failures and recent_failures[-1] != self._seen_failure_str:
            self._seen_failure_str = recent_failures[-1]
            if self.current_objective is not None:
                gid = self.current_objective.goal_id
                self._last_failure_kind[gid] = classify_failure_kind("", self._seen_failure_str)
                self._last_failure_detail[gid] = self._seen_failure_str

        deaths_marker = (len(state.deaths), state.deaths[-1] if state.deaths else None)
        if not self._deaths_initialized:
            self._deaths_initialized = True
            self._last_deaths_marker = deaths_marker
            died_since_last = False
        else:
            died_since_last = deaths_marker != self._last_deaths_marker
            self._last_deaths_marker = deaths_marker

        cur_day = is_daytime(state)
        if cur_day is not None:
            if self._last_is_day is None:
                self._last_is_day = cur_day
                day_night_flipped = False
            else:
                day_night_flipped = cur_day != self._last_is_day
                self._last_is_day = cur_day
        else:
            day_night_flipped = False

        if died_since_last:
            self._invalidate_pending_async("respawn")
        elif day_night_flipped:
            self._invalidate_pending_async("day_night_changed")

        # 1) 生存补给与 safe_mode 处于一切目标之上；safe_mode 期间绝不发起目标决策
        urgent = self.rules.choose_survival_or_safe_mode(state)
        if urgent is not None:
            if state.in_safe_mode:
                self._invalidate_pending_async("safe_mode")
            if died_since_last:
                self._deferred_trigger = "respawn"
            elif day_night_flipped and not self._deferred_trigger:
                self._deferred_trigger = "day_night_changed"
            self._last_selected_goal_id = None
            return PlanDecision(
                urgent.skill,
                urgent.source,
                urgent.reason,
                objective=self.current_objective,
                objective_source=self.current_objective.source if self.current_objective else None,
            )

        # 2) 技能边界非阻塞吸纳异步模型结果（先采纳已就绪的合法 Objective 或兜底技能）
        fallback_skill_decision = self._poll_async_scheduler(state, now)

        # 3) 检查是否命中换目标触发时机
        fired_trigger: Optional[str] = None
        trig_info = self._detect_trigger(
            state, now, died_since_last=died_since_last, day_night_flipped=day_night_flipped
        )
        if trig_info is not None:
            fired_trigger, old_outcome, f_kind = trig_info
            self._switch_objective(state, trigger=fired_trigger, old_outcome=old_outcome, failure_kind=f_kind, now=now)

        # 4) 交给战术层 RulePlanner(target_goal=当前目标) 展开下一技能
        if self.current_objective is not None:
            tactical = self._make_tactical_planner(self.current_objective.goal_id)
            tactical_dec = tactical.choose_for_objective(state)
            if tactical_dec.skill is not None:
                self._last_selected_goal_id = tactical._last_selected_goal_id or self.current_objective.goal_id
                self.rules._last_selected_goal_id = self._last_selected_goal_id
                return PlanDecision(
                    tactical_dec.skill,
                    tactical_dec.source,
                    f"[目标:{self.current_objective.goal_id}] {tactical_dec.reason}",
                    environment_recovery=tactical_dec.environment_recovery,
                    objective=self.current_objective,
                    objective_source=self.current_objective.source,
                    objective_trigger=fired_trigger,
                )

            # 触发器 #8：战术层对当前目标给不出任何技能 -> 立即切目标
            failed_gid = self.current_objective.goal_id
            fired_trigger = "no_tactical_skill"
            self._switch_objective(
                state,
                trigger="no_tactical_skill",
                old_outcome="no_skill",
                failure_kind="missing_prereq",
                now=now,
                exclude_goals={failed_gid},
            )
            if self.current_objective is not None:
                tactical2 = self._make_tactical_planner(self.current_objective.goal_id)
                tactical_dec2 = tactical2.choose_for_objective(state)
                if tactical_dec2.skill is not None:
                    self._last_selected_goal_id = tactical2._last_selected_goal_id or self.current_objective.goal_id
                    self.rules._last_selected_goal_id = self._last_selected_goal_id
                    return PlanDecision(
                        tactical_dec2.skill,
                        tactical_dec2.source,
                        f"[目标:{self.current_objective.goal_id}] {tactical_dec2.reason}",
                        environment_recovery=tactical_dec2.environment_recovery,
                        objective=self.current_objective,
                        objective_source=self.current_objective.source,
                        objective_trigger=fired_trigger,
                    )

        # 5) 战术层对任何目标都给不出技能：--strategy llm 下启用非阻塞技能级模型选择兜底，当拍先走规则余量/外迁兜底
        if fallback_skill_decision is not None:
            return fallback_skill_decision
        if self.strategy == "llm":
            self._dispatch_async_skill_fallback(state, recent_failures)

        base_dec = self.rules.choose(state)
        self._last_selected_goal_id = self.rules._last_selected_goal_id
        return PlanDecision(
            base_dec.skill,
            base_dec.source,
            base_dec.reason,
            environment_recovery=base_dec.environment_recovery,
            objective=self.current_objective,
            objective_source=self.current_objective.source if self.current_objective else None,
            objective_trigger=fired_trigger,
        )
