"""规划层入口：`PlanDecision`、`RulePlanner`（性价比排序与战术展开器）与薄分派 `CampaignPlanner`。

战略编排与异步预取已拆分至 `strategy.py` 与 `strategy_async.py`；`CampaignPlanner.select()` 按 `mode` 与 `strategy` 分派。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import math
import time
from typing import TYPE_CHECKING, Any, Callable, Collection, Mapping, Optional

from ..planner import skill_choice
from ..planner.llm import LLMClient
from ..planner.objective import Objective
from ..planner.scheduler import DecisionScheduler
from ..runtime import OXYGEN_FULL, EnvBlocked, State, _inv_fingerprint, effective_hostile_flee_dist, has_equipped_armor
from ..skills import REGISTRY, Skill, bind_args, make
from ..skills import base as skill_base
from ..skills import camp_plan as _camp_plan
from ..skills.furnace_lease import best_fuel_for, lease_book_for
from ..skills.items import best_pickaxe
from ..skills.nether_portal import nether_preflight_missing
from ..skills.survival_food import cooked_food_count, has_ready_food_lease, inventory_food_restore, raw_meat_count
from ..skills.world_memo import format_memo_summary, memo_for
from . import subgoals as subgoals_mod
from .context import DecisionContext, ObjectiveRecord, is_daytime
from .goals import (
    DEFAULT_CAMPAIGN_STAGES, GOALS, HUB_DOWNSTREAM_THRESHOLD, OFFICIAL_CAMPAIGN_STAGES,
    STONE_OR_BETTER_PICKS, GoalSpec,
    check_item_expr, compute_hub_goal_ids, diagnose_goal_gap, is_goal_achieved, is_goal_stopped_out,
    is_prereq_satisfied, normalize_goal_ids, rank_actionable_goals, resolve_active_goals,
)
from .reserve import ReservationBook, bind_reservation_book
from .subgoals import item_available, tool_ready
from .starter_skills import PROTECTED_ZONES, can_afford_gear, has_known_table, total_cobble, total_logs, total_planks

if TYPE_CHECKING:
    from .strategy import StrategyOrchestrator

STRATEGIES: tuple[str, ...] = ("legacy", "rule", "llm")
_SAFETY_INTENT_DEFAULT_TTL_S: float = 15.0
_ESCAPE_INTENT_KINDS: frozenset[str] = frozenset({"flee", "douse", "surface", "escape_lava", "lava", "void_risk"})
_SURVIVAL_SWORDS: tuple[str, ...] = ("stone_sword", "iron_sword", "diamond_sword", "netherite_sword")
_WOOL_COLORS: tuple[str, ...] = (
    "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
    "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black",
)
_BED_ITEMS: tuple[str, ...] = tuple(f"{c}_bed" for c in _WOOL_COLORS)
_WOOL_ITEMS: tuple[str, ...] = tuple(f"{c}_wool" for c in _WOOL_COLORS)
DEFAULT_HUB_COOLDOWN_S: float = 180.0
DEFAULT_HUB_WAKE_MOVE_DIST: float = 32.0
DEFAULT_HUB_MAX_RESTARTS: int = 8


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
    stop_episode: bool = False
    objective: Optional[Objective] = None
    objective_source: Optional[str] = None
    objective_trigger: Optional[str] = None
    cooldown_blocked: bool = False
    blocked_goal_id: Optional[str] = None
    safety_escape: bool = False


def _clear_state_safety_intent(state: State) -> None:
    setter = getattr(state, "set_safety_intent", None)
    if callable(setter):
        setter(None)
    else:
        state.safety_intent = None


def _extract_escape_intent(state: State) -> Optional[tuple[str, str]]:
    """提取需要立即脱险的安全意图（兼容 dict/str 格式的 `state.safety_intent` 及直读物理险情）。"""
    st = state.snapshot()
    raw = getattr(state, "safety_intent", None)
    kind = ""
    why = ""
    if isinstance(raw, Mapping):
        now = time.monotonic()
        expires_at = raw.get("expires_at")
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) and now >= float(expires_at):
            _clear_state_safety_intent(state)
            raw = None
        else:
            at_ts = raw.get("at")
            if isinstance(at_ts, (int, float)) and not isinstance(at_ts, bool):
                if now - float(at_ts) > _SAFETY_INTENT_DEFAULT_TTL_S:
                    _clear_state_safety_intent(state)
                    raw = None
        if isinstance(raw, Mapping):
            kind = str(raw.get("kind") or raw.get("intent") or "").strip().lower()
            why = str(raw.get("why") or kind)
    elif isinstance(raw, str):
        kind = raw.strip().lower()
        why = kind

    if kind in _ESCAPE_INTENT_KINDS:
        norm = "escape_lava" if kind in ("escape_lava", "lava") else ("flee" if kind == "void_risk" else kind)
        return norm, why or norm
    if kind == "heal":
        hostile = st.get("nearest_hostile") or {}
        if isinstance(hostile, Mapping) and hostile:
            hname = str(hostile.get("name") or "").removeprefix("minecraft:").strip().lower()
            flee_dist = effective_hostile_flee_dist(state, hname)
            if float(hostile.get("dist", 99.0)) < flee_dist:
                return "flee", f"{why} + {hostile.get('name')} 距离 {hostile.get('dist')}"

    # 直接物理险情兜底（即使后台 monitor 未启动或尚未刷出 safety_intent）
    if st.get("in_lava"):
        return "escape_lava", "in_lava"
    hostile = st.get("nearest_hostile") or {}
    if isinstance(hostile, Mapping) and hostile:
        hname = str(hostile.get("name") or "").removeprefix("minecraft:").strip().lower()
        creeper_dist = float(getattr(state.config, "creeper_urgent_dist", 5.0))
        flee_dist = effective_hostile_flee_dist(state, hname)
        try:
            hdist = float(hostile.get("dist", 99.0))
        except (TypeError, ValueError):
            hdist = 99.0
        if hname == "creeper" and math.isfinite(hdist) and hdist < creeper_dist:
            return "flee", f"近身苦力怕 距离 {hdist:g}"
        if math.isfinite(hdist) and hdist < flee_dist:
            return "flee", f"{hname or 'hostile'} 距离 {hdist:g}"
    if st.get("on_fire") and not st.get("in_water"):
        return "douse", "on_fire"
    oxygen = st.get("oxygen")
    if (st.get("in_water") or st.get("submerged")) and oxygen is not None:
        try:
            ox = float(oxygen)
        except (TypeError, ValueError):
            ox = float(OXYGEN_FULL)
        if math.isfinite(ox) and ox <= 10.0:
            return "surface", f"in_water oxygen={ox:g}"
    return None


def _is_urgent_escape_hazard(state: State) -> bool:
    """判断当前是否存在应优先于进食与 safe_mode 的致命/贴脸敌怪抢占险情。"""
    st = state.snapshot()
    if st.get("in_lava"):
        return True
    oxygen = st.get("oxygen")
    if st.get("submerged") and oxygen is not None:
        try:
            ox = float(oxygen)
        except (TypeError, ValueError):
            ox = float(OXYGEN_FULL)
        if math.isfinite(ox) and ox < float(getattr(state.config, "oxygen_warn", 6.0)):
            return True
    hostile = st.get("nearest_hostile") or {}
    if isinstance(hostile, Mapping) and hostile:
        hname = str(hostile.get("name") or "").removeprefix("minecraft:").strip().lower()
        creeper_dist = float(getattr(state.config, "creeper_urgent_dist", 5.0))
        flee_dist = float(getattr(state.config, "hostile_flee_dist", 6.0))
        try:
            hdist = float(hostile.get("dist", 99.0))
        except (TypeError, ValueError):
            hdist = 99.0
        if hname == "creeper" and math.isfinite(hdist) and hdist < creeper_dist:
            return True
        # safe_mode 期间若真有敌怪守尸/贴近（<6 格），绝不可原地发呆吃第 3 死；常态下贴脸（<=2.5 格）亦先脱险再进食
        if math.isfinite(hdist) and (
            (state.in_safe_mode and hdist < flee_dist) or hdist <= 2.5
        ):
            return True
    raw = getattr(state, "safety_intent", None)
    if isinstance(raw, Mapping):
        now = time.monotonic()
        expires_at = raw.get("expires_at")
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) and now >= float(expires_at):
            return False
        at_ts = raw.get("at")
        if isinstance(at_ts, (int, float)) and not isinstance(at_ts, bool) and now - float(at_ts) > _SAFETY_INTENT_DEFAULT_TTL_S:
            return False
        kind = str(raw.get("kind") or raw.get("intent") or "").strip().lower()
        sev = str(raw.get("severity") or "").strip().lower()
        if sev == "urgent" and kind in _ESCAPE_INTENT_KINDS:
            return True
    return False


def _material_plan(skill: Skill, state: State):
    plan = getattr(type(skill), "material_plan", None)
    return plan(skill.args, state) if callable(plan) else ()


def _supplies(goal: GoalSpec, state: State):
    for requirement, name, args in goal.gather_fallbacks:
        if not check_item_expr(state, requirement):
            yield requirement, name, dict(args)
            if not goal.gather_when_blocked:
                return


def _nether_blast_blocks(state: State) -> int:
    """Count blast-resistant blocks usable for a Nether portal bunker."""
    return total_cobble(state) + state.count("stone_bricks") + state.count("blackstone")


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


@dataclass
class _HubSuspension:
    """核心枢纽目标的可恢复临时挂起记录：位移离开 >= 32 格、切换维度或冷却到期后自动唤醒。"""

    goal_id: str
    dimension: str
    x: float
    z: float
    suspended_at: float
    cooldown_s: float
    restart_count: int = 0
    reason: str = ""

    def should_wake(self, state: State, *, now: float, move_dist: float = DEFAULT_HUB_WAKE_MOVE_DIST) -> Optional[str]:
        if self.cooldown_s >= 0.0 and (now - self.suspended_at) >= self.cooldown_s:
            return "cooldown"
        st = state.snapshot()
        dim = str(st.get("dimension") or "overworld")
        if dim != self.dimension:
            return "dimension_changed"
        pos = st.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 3:
            try:
                px, pz = float(pos[0]), float(pos[2])
                if math.isfinite(px) and math.isfinite(pz) and math.hypot(px - self.x, pz - self.z) >= move_dist:
                    return "moved"
            except (TypeError, ValueError):
                pass
        return None


def build_state_summary(state: State, *, goal_hint: str = "") -> str:
    """给技能级模型选择（兼容 `--llm`）的一屏状态摘要（遵守 J17）。战略层上下文见 `loop/context.py`。"""
    st = state.snapshot()
    pick = best_pickaxe(state) or "无"
    lines = [
        f"位置={st.get('pos')} 维度={st.get('dimension', 'overworld')} 昼夜={'白天' if st.get('is_day', True) else '夜晚'}({st.get('time_of_day')})",
        f"血量={st.get('health', 20)} 饱食={st.get('food', 20)} 氧气={st.get('oxygen', OXYGEN_FULL)} 安全模式={state.in_safe_mode}",
        f"最近敌对={ (st.get('nearest_hostile') or {}).get('name', '无') } 距离={ (st.get('nearest_hostile') or {}).get('dist', '-') }",
        f"物资：最优镐={pick} 原木={total_logs(state)} 木板={total_planks(state)} 木棍={state.count('stick')} "
        f"工作台={state.count('crafting_table')} 圆石={total_cobble(state)} 熔炉={state.count('furnace')} 石剑={state.count('stone_sword')}",
    ]
    if PROTECTED_ZONES:
        lines.append(f"已发现保护区点位数={len(PROTECTED_ZONES)}（需远离）")
    memo_summary = format_memo_summary(state)
    if memo_summary:
        lines.append(f"已知环境资源：{memo_summary}")
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
        hub_min_downstream: int = HUB_DOWNSTREAM_THRESHOLD,
        hub_cooldown_s: float = DEFAULT_HUB_COOLDOWN_S,
        hub_wake_dist: float = DEFAULT_HUB_WAKE_MOVE_DIST,
        hub_max_restarts: int = DEFAULT_HUB_MAX_RESTARTS,
        clock: Callable[[], float] = time.monotonic,
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
        self._abandoned_goals: set[str] = set()
        self._last_expand_cooldown_blocked: bool = False
        self._last_expand_blocked_goal_id: Optional[str] = None
        self._env_recovery_counts: dict[tuple[str, int, int], int] = {}
        self._max_env_recovery_per_cell: int = 4
        self._clock: Callable[[], float] = clock
        self.hub_cooldown_s: float = max(0.0, float(hub_cooldown_s))
        self.hub_wake_dist: float = max(1.0, float(hub_wake_dist))
        self.hub_max_restarts: int = max(0, int(hub_max_restarts))
        self.hub_goal_ids: frozenset[str] = compute_hub_goal_ids(self.ledger, min_downstream=hub_min_downstream)
        self._hub_suspensions: dict[str, _HubSuspension] = {}
        self._hub_restart_counts: dict[str, int] = {}
        self._last_observed_pos: tuple[str, float, float] = ("overworld", 0.0, 0.0)
        self._fallback_explore_turn: int = 0

    def is_hub_goal(self, goal_id: Optional[str]) -> bool:
        """基于台账 DAG 下游闭包自动判断目标是否为核心枢纽。"""
        return bool(goal_id and goal_id in self.hub_goal_ids)

    def _extract_pos(self, state: Optional[State]) -> tuple[str, float, float]:
        if state is None:
            return self._last_observed_pos
        st = state.snapshot()
        dim = str(st.get("dimension") or "overworld")
        pos = st.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 3:
            try:
                px, pz = float(pos[0]), float(pos[2])
                if math.isfinite(px) and math.isfinite(pz):
                    self._last_observed_pos = (dim, px, pz)
                    return self._last_observed_pos
            except (TypeError, ValueError):
                pass
        return self._last_observed_pos

    def _next_hub_cooldown(self, restart_count: int) -> float:
        scale = min(3.0, 1.0 + (2.0 / 3.0) * max(0, int(restart_count)))
        return round(self.hub_cooldown_s * scale, 1)

    def _clear_single_goal_cooldown(self, goal_id: str) -> None:
        spec = self.ledger_map.get(goal_id)
        if spec is None:
            return
        for sname, sargs in ((spec.skill, spec.args_dict), *((fb_s, dict(fb_a)) for _, fb_s, fb_a in spec.gather_fallbacks)):
            cls = self.registry.get(sname)
            if cls is None:
                continue
            bound, errs = bind_args(cls, sargs or {})
            if not errs:
                skill_base._COOLDOWN.pop(cls.goal.format(**bound), None)

    def suspend_hub_goal(self, goal_id: str, *, state: Optional[State] = None, reason: str = "") -> bool:
        """将核心枢纽目标转为可恢复临时挂起；超出累计重启上限时退回本轮放弃。"""
        if not goal_id or goal_id not in self.ledger_map or not self.is_hub_goal(goal_id):
            return False
        spec = self.ledger_map[goal_id]
        if goal_id in self._hub_suspensions:
            self._abandoned_goals.add(goal_id)
            if self._last_selected_goal_id == goal_id:
                self._last_selected_goal_id = None
            return True
        restarts = self._hub_restart_counts.get(goal_id, 0)
        if self.hub_max_restarts > 0 and restarts >= self.hub_max_restarts:
            self._abandoned_goals.add(goal_id)
            if spec.max_fails_per_ep > 0:
                self.ep_goal_fails[goal_id] = max(self.ep_goal_fails.get(goal_id, 0), spec.max_fails_per_ep)
            if self._last_selected_goal_id == goal_id:
                self._last_selected_goal_id = None
            return False
        dim, px, pz = self._extract_pos(state)
        self._hub_suspensions[goal_id] = _HubSuspension(
            goal_id=goal_id,
            dimension=dim,
            x=px,
            z=pz,
            suspended_at=self._clock(),
            cooldown_s=self._next_hub_cooldown(restarts),
            restart_count=restarts,
            reason=reason,
        )
        self._abandoned_goals.add(goal_id)
        if spec.max_fails_per_ep > 0:
            self.ep_goal_fails[goal_id] = max(self.ep_goal_fails.get(goal_id, 0), spec.max_fails_per_ep)
        if self._last_selected_goal_id == goal_id:
            self._last_selected_goal_id = None
        return True

    def record_environment_block(self, skill: Skill, error: EnvBlocked, state: State) -> None:
        self._extract_pos(state)
        self._environment_blocks[skill.goal] = _EnvironmentBlock(error, _EnvironmentBlock.observe(error, state))
        self._last_selected_goal_id = None

    def refresh_environment_blocks(self, state: State) -> list[str]:
        self._extract_pos(state)
        for key, block in list(self._environment_blocks.items()):
            if block.wake_if_changed(state):
                del self._environment_blocks[key]

        now = self._clock()
        woken_hubs: list[str] = []
        for gid, susp in list(self._hub_suspensions.items()):
            if susp.should_wake(state, now=now, move_dist=self.hub_wake_dist) is not None:
                del self._hub_suspensions[gid]
                self._abandoned_goals.discard(gid)
                self.ep_goal_fails[gid] = 0
                if gid in self.ep_goal_elapsed:
                    self.ep_goal_elapsed[gid] = 0.0
                self._hub_restart_counts[gid] = susp.restart_count + 1
                self._clear_single_goal_cooldown(gid)
                woken_hubs.append(gid)

        woken_set = set(woken_hubs)
        for gid in self.hub_goal_ids:
            if gid in self._hub_suspensions or gid in woken_set:
                continue
            spec = self.ledger_map.get(gid)
            if spec is None or is_goal_achieved(spec, state, self.completed_advancements):
                continue
            if spec.max_fails_per_ep > 0 and int(self.ep_goal_fails.get(gid, 0)) >= spec.max_fails_per_ep:
                self.suspend_hub_goal(gid, state=state, reason="max_fails")
        return woken_hubs

    def environment_block(self, goal_key: str) -> Optional[EnvBlocked]:
        block = self._environment_blocks.get(goal_key)
        return block.error if block else None

    def _cell_key(self, state: State) -> tuple[str, int, int]:
        st = state.snapshot()
        pos = st.get("pos") or [0.0, 64.0, 0.0]
        try:
            cx, cz = math.floor(float(pos[0]) / 16.0), math.floor(float(pos[2]) / 16.0)
        except (TypeError, ValueError, IndexError):
            cx, cz = 0, 0
        return str(st.get("dimension") or "overworld"), cx, cz

    def abandon_goal(self, goal_id: Optional[str], *, state: Optional[State] = None) -> bool:
        """将反复空转失败的目标挂起或放弃：核心枢纽转为可恢复挂起，非枢纽叶目标按原规则本轮止损。"""
        if not goal_id or goal_id not in self.ledger_map:
            return False
        if self.is_hub_goal(goal_id):
            self.suspend_hub_goal(goal_id, state=state, reason="abandoned")
            return True
        self._abandoned_goals.add(goal_id)
        spec = self.ledger_map[goal_id]
        if spec.max_fails_per_ep > 0:
            self.ep_goal_fails[goal_id] = max(self.ep_goal_fails.get(goal_id, 0), spec.max_fails_per_ep)
        if self._last_selected_goal_id == goal_id:
            self._last_selected_goal_id = None
        return True

    def clear_tactical_cooldowns(
        self,
        state: State,
        goal_id: Optional[str] = None,
        *,
        include_relocate: bool = False,
    ) -> list[str]:
        """清除导致原地冷却空转的战术技能冷却键；`include_relocate=True` 时一并解除外迁/逃生技能冷却。"""
        relocate_skills = {"scout_island", "emergency_escape"}
        keys: set[str] = set()

        def _collect_skill_key(name: str, args: Optional[dict]) -> None:
            if not include_relocate and name in relocate_skills:
                return
            cls = self.registry.get(name)
            if cls is None:
                return
            bound, errs = bind_args(cls, args or {})
            if not errs:
                keys.add(cls.goal.format(**bound))

        if goal_id and goal_id in self.ledger_map:
            g = self.ledger_map[goal_id]
            _collect_skill_key(g.skill, g.args_dict)
            for _, fb_skill, fb_args in g.gather_fallbacks:
                _collect_skill_key(fb_skill, dict(fb_args))

        for step in self._rank(state):
            g = step.step_goal
            _collect_skill_key(g.skill, g.args_dict)
            for _, fb_skill, fb_args in g.gather_fallbacks:
                _collect_skill_key(fb_skill, dict(fb_args))

        if include_relocate:
            for rname in ("scout_island", "emergency_escape"):
                cls = self.registry.get(rname)
                if cls is not None:
                    bound, errs = bind_args(cls, {})
                    if not errs:
                        keys.add(cls.goal.format(**bound))
                    elif "{" not in cls.goal:
                        keys.add(cls.goal)
                    intent_param = cls.params.get("intent")
                    if intent_param is not None and intent_param.choices:
                        for choice in intent_param.choices:
                            b2, e2 = bind_args(cls, {"intent": choice})
                            if not e2:
                                keys.add(cls.goal.format(**b2))

        cleared: list[str] = []
        for k in sorted(keys):
            if k in skill_base._COOLDOWN:
                skill_base._COOLDOWN.pop(k, None)
                cleared.append(k)

        if not cleared and skill_base._COOLDOWN:
            reloc_goals: set[str] = set()
            for rname in relocate_skills:
                cls = self.registry.get(rname)
                if cls is not None:
                    if "{" not in cls.goal:
                        reloc_goals.add(cls.goal)
                    intent_param = cls.params.get("intent")
                    if intent_param is not None and intent_param.choices:
                        for choice in intent_param.choices:
                            b2, e2 = bind_args(cls, {"intent": choice})
                            if not e2:
                                reloc_goals.add(cls.goal.format(**b2))
            for k in list(skill_base._COOLDOWN):
                if not include_relocate and k in reloc_goals:
                    continue
                skill_base._COOLDOWN.pop(k, None)
                cleared.append(k)
        return cleared

    def _build_emergency_escape(self, intent: str, state: State) -> Optional[Skill]:
        cls = self.registry.get("emergency_escape")
        if cls is None and self.registry is REGISTRY:
            try:
                from ..skills import safety_escape as _safety_escape  # noqa: F401
                cls = self.registry.get("emergency_escape")
            except Exception:
                cls = None
        if cls is None or not skill_choice.meets_verification(cls, self.min_verified) or cls.blocked(state):
            return None
        args: dict[str, Any] = {}
        intent_param = cls.params.get("intent")
        if intent_param is not None:
            choices = tuple(intent_param.choices or ())
            if choices:
                if intent in choices:
                    args["intent"] = intent
                elif intent == "escape_lava" and "douse" in choices:
                    args["intent"] = "douse"
                elif "flee" in choices:
                    args["intent"] = "flee"
            else:
                args["intent"] = "douse" if intent == "escape_lava" else intent
        return self._try_make("emergency_escape", args, state)

    def make_relocate_skill(self, state: State) -> Optional[Skill]:
        """构造单次外迁/脱困技能（优先 `scout_island`，缺失时退回 `emergency_escape`）。"""
        self.clear_tactical_cooldowns(state, include_relocate=True)
        sc_radius = 32 if best_pickaxe(state) not in STONE_OR_BETTER_PICKS else 36
        sc = self._try_make("scout_island", {"radius": sc_radius}, state)
        if sc is not None:
            return sc
        return self._build_emergency_escape("flee", state)

    def _choose_safety_escape(self, state: State) -> Optional[PlanDecision]:
        """消费 `SafetyMonitor` 软/硬安全意图与直读物理险情，优先派发 `EmergencyEscape`，否则平稳回退 `scout_island`。"""
        extracted = _extract_escape_intent(state)
        if extracted is None:
            return None
        intent, why = extracted
        esc = self._build_emergency_escape(intent, state)
        if esc is not None:
            return PlanDecision(esc, "rule", f"安全意图应急脱险 ({intent}: {why})", safety_escape=True)
        # 窄测试注册表未注册 emergency_escape 时，平稳回退到现有 scout_island 脱困
        radius = 16 if intent in ("surface", "douse", "escape_lava") else 24
        sc = self._try_make("scout_island", {"radius": radius}, state)
        if sc is not None:
            return PlanDecision(sc, "rule", f"安全意图兜底脱险 ({intent}: {why})", safety_escape=True)
        return None

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

    def targets_exhausted(self, state: State) -> bool:
        """聚焦目标尚未全部达成、当前无可执行步骤且未因环境条件挂起时判定为本轮止损。"""
        if not self.target_goal:
            return False
        target_ids = normalize_goal_ids(self.target_goal)
        if not target_ids or any(gid not in self.ledger_map for gid in target_ids):
            return False
        target_specs = [self.ledger_map[gid] for gid in target_ids]
        if all(is_goal_achieved(g, state, self.completed_advancements) for g in target_specs):
            return False
        if self._rank(state):
            return False
        return not self.blocked_targets(state, self.target_goal, refresh=False)

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

    def _recover_blocked_skill_prereq(
        self,
        skill_name: str,
        state: State,
        seen_goals: Optional[set[str]] = None,
    ) -> tuple[Optional[Skill], str]:
        """当技能（或补料技能）因 `cls.blocked(state)` 受阻时，回溯对应目标的未满足前置工具/工作站链。"""
        cls = self.registry.get(skill_name)
        if cls is None or not cls.blocked(state):
            return None, ""
        seen = seen_goals if seen_goals is not None else set()
        providers = [g for g in self.active_goals if g.skill == skill_name and g.prereq_goals]
        for prov in providers:
            if prov.id in seen:
                continue
            seen.add(prov.id)
            inst, why = self._expand_unmet_prereq_chain(prov, state, seen)
            if inst is not None:
                return inst, why
        return None, ""

    def _expand_unmet_prereq_chain(
        self,
        goal: GoalSpec,
        state: State,
        seen: set[str],
    ) -> tuple[Optional[Skill], str]:
        """深度优先展开 `goal` 未满足的 `prereq_goals`（工具/工作站耗尽或死后丢失时自动补造）。"""
        for pid in goal.prereq_goals:
            parent = self.ledger_map.get(pid)
            if parent is None or parent.id in seen:
                continue
            if is_prereq_satisfied(parent, state, self.completed_advancements):
                continue
            seen.add(parent.id)
            inst, why = self._expand_unmet_prereq_chain(parent, state, seen)
            if inst is not None:
                return inst, why
            ready = all(check_item_expr(state, expr) for expr in parent.prereq_items)
            if ready:
                cand, _, _ = self._diagnose_skill(parent.skill, parent.args_dict, state)
                if cand is not None:
                    self._last_selected_goal_id = parent.id
                    return cand, f"恢复受阻技能前置 {parent.id}（{parent.title}）"
                rec_inst, rec_why = self._recover_blocked_skill_prereq(parent.skill, state, seen)
                if rec_inst is not None:
                    return rec_inst, rec_why
            if not ready or parent.gather_when_blocked:
                for req_expr, fb_skill, fb_args in _supplies(parent, state):
                    cand, _, _ = self._diagnose_skill(fb_skill, fb_args, state)
                    if cand is not None:
                        self._last_selected_goal_id = parent.id
                        return cand, f"恢复受阻前置 {parent.id} 补齐材料（{req_expr}）"
                    rec_inst, rec_why = self._recover_blocked_skill_prereq(fb_skill, state, seen)
                    if rec_inst is not None:
                        return rec_inst, rec_why
        return None, ""

    def _apply_nether_preflight(
        self,
        state: State,
        skill_name: str,
        args: Optional[Mapping[str, Any]],
    ) -> tuple[Optional[Skill], str, dict[str, Any]]:
        """Enforce overworld pre-flight readiness before entering a Nether portal."""
        eff_args = dict(args or {})
        if skill_name != "build_and_enter_portal" or eff_args.get("prepare_flint"):
            return None, "", eff_args
        st = state.snapshot()
        dim = str(st.get("dimension") or "overworld").removeprefix("minecraft:").strip().lower() or "overworld"
        if dim != "overworld":
            return None, "", eff_args

        cls = self.registry.get("build_and_enter_portal")
        cls_params = getattr(cls, "params", {}) or {} if cls is not None else {}
        if "auto_bunker" in cls_params:
            eff_args["auto_bunker"] = True

        missing = nether_preflight_missing(state)
        if not missing:
            if "strict_preflight" in cls_params:
                eff_args["strict_preflight"] = True
            return None, "", eff_args

        if "camp_kit_backup" in missing:
            if _camp_plan.is_near_camp(state, max_dist=96.0) and _camp_plan.has_surplus_for_deposit(
                state, reservations=self.reservations
            ):
                cand, _, _ = self._diagnose_skill("manage_camp_chest", {"mode": "deposit"}, state)
                if cand is not None:
                    return cand, "下界行前检查：先向主世界营地热备箱存仓备份", eff_args
            else:
                ow_camp = memo_for(state).get_camp(dimension="overworld")
                if not (isinstance(ow_camp, Mapping) and (ow_camp.get("chest") or ow_camp.get("chests"))) and _camp_plan.has_chest_or_materials(state):
                    cand, _, _ = self._diagnose_skill("manage_camp_chest", {"mode": "setup"}, state)
                    if cand is not None:
                        return cand, "下界行前检查：先建立主世界营地热备箱", eff_args

        if "flint_and_steel" in missing:
            if state.count("iron_ingot") >= 1 and state.count("flint") >= 1:
                cand, _, _ = self._diagnose_skill("craft_iron_gear", {"item": "flint_and_steel"}, state)
                if cand is not None:
                    return cand, "下界行前检查：先合成打火石", eff_args
            elif state.count("flint") < 1:
                cand, _, _ = self._diagnose_skill("build_and_enter_portal", {"prepare_flint": True}, state)
                if cand is not None:
                    return cand, "下界行前检查：先采掘砂砾补齐燧石", eff_args

        if "shield" in missing:
            if state.count("iron_ingot") >= 1 and (total_planks(state) + total_logs(state) * 4) >= 6:
                cand, _, _ = self._diagnose_skill("craft_iron_gear", {"item": "shield"}, state)
                if cand is not None:
                    return cand, "下界行前检查：先合成盾牌防恶魂与近战", eff_args

        if "iron_armor" in missing:
            for piece, need in (("iron_chestplate", 8), ("iron_leggings", 7), ("iron_helmet", 5), ("iron_boots", 4)):
                if state.count("iron_ingot") >= need:
                    cand, _, _ = self._diagnose_skill("craft_iron_gear", {"item": piece}, state)
                    if cand is not None:
                        return cand, f"下界行前检查：先合成铁甲({piece})", eff_args

        if "cooked_food>=24" in missing:
            if is_daytime(state) is not False:
                cand, _, _ = self._diagnose_skill("gather_food", {"target_restore": 64, "cook": True}, state)
                if cand is not None:
                    return cand, "下界行前检查：白天补足熟食至 24 份以上", eff_args

        if "cobblestone>=96" in missing:
            if best_pickaxe(state) in STONE_OR_BETTER_PICKS:
                cand, _, _ = self._diagnose_skill("mine_stone", {"count": 32}, state)
                if cand is not None:
                    return cand, "下界行前检查：补挖圆石至 96 块以上", eff_args

        return None, "", eff_args

    def expand_objective_skill(self, state: State) -> tuple[Optional[Skill], str]:
        """针对 `target_goal` 展开下一个可启动技能；无法展开时返回 `(None, 明确原因)`。"""
        self._last_selected_goal_id = None
        self._last_expand_cooldown_blocked = False
        self._last_expand_blocked_goal_id = None
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
                rem_inst, rem_why, eff_args = self._apply_nether_preflight(state, g.skill, g.args_dict)
                if rem_inst is not None:
                    self._last_selected_goal_id = g.id
                    return rem_inst, rem_why
                inst, why_not, cooling_only = self._diagnose_skill(g.skill, eff_args, state)
                if inst is not None:
                    self._last_selected_goal_id = g.id
                    return inst, f"推进目标 {g.id}（{g.title}）"
                if not cooling_only:
                    rec_inst, rec_why = self._recover_blocked_skill_prereq(g.skill, state)
                    if rec_inst is not None:
                        return rec_inst, rec_why
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
                    if not cooling_only:
                        rec_inst, rec_why = self._recover_blocked_skill_prereq(fb_skill, state)
                        if rec_inst is not None:
                            return rec_inst, rec_why
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
            self._last_expand_cooldown_blocked = True
            self._last_expand_blocked_goal_id = cooling_step_id

        return None, "；".join(reasons[:3]) or f"目标 {ranked[0].step_goal.id} 无可启动技能"

    def _choose_nether_landing_bunker(self, state: State) -> Optional[PlanDecision]:
        """Prioritize building a 3x3 blast-resistant portal bunker immediately after Nether arrival."""
        st = state.snapshot()
        dim = str(st.get("dimension") or "overworld").removeprefix("minecraft:").strip().lower() or "overworld"
        if dim != "the_nether":
            return None
        hostile = st.get("nearest_hostile") or {}
        hdist = 99.0
        if isinstance(hostile, Mapping) and hostile:
            try:
                raw_hdist = float(hostile.get("dist", 99.0))
                if math.isfinite(raw_hdist):
                    hdist = raw_hdist
            except (TypeError, ValueError):
                pass
        if hdist < 12.0:
            return None
        nether_camp = memo_for(state).get_camp(dimension="the_nether")
        nether_tier = int(nether_camp.get("shelter_tier", 0)) if isinstance(nether_camp, Mapping) else 0
        if nether_tier < 1 and _nether_blast_blocks(state) >= 20:
            nb_sk = self._try_make("build_spawn_shelter", {"mode": "nether_bunker"}, state)
            if nb_sk is not None:
                return PlanDecision(nb_sk, "rule", "刚入下界尚未建防恶魂碉堡，优先在传送门周圈建 3×3 圆石碉堡与应急箱")
        return None

    def choose_urgent_survival_or_safe_mode(self, state: State) -> Optional[PlanDecision]:
        """在一切目标之上的致命抢占脱险、生存补给、`safe_mode` 避险与软安全意图处理。"""
        # 致命物理险情（岩浆/濒溺/近身苦力怕 <5 格）必须先于进食与 safe_mode 脱险
        if _is_urgent_escape_hazard(state):
            urgent_escape = self._choose_safety_escape(state)
            if urgent_escape is not None:
                return urgent_escape

        hp = float(state.st.get("health", 20))
        food = float(state.st.get("food", 20))
        raw_intent = getattr(state, "safety_intent", None)
        intent_kind = (
            str(raw_intent.get("kind") or raw_intent.get("intent") or "").strip().lower()
            if isinstance(raw_intent, Mapping)
            else (raw_intent.strip().lower() if isinstance(raw_intent, str) else "")
        )

        if food <= 14 or (hp < 18 and food < 20) or (intent_kind in ("heal", "eat") and food < 20):
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

        escape = self._choose_safety_escape(state)
        if escape is not None:
            return escape
        bunker = self._choose_nether_landing_bunker(state)
        if bunker is not None:
            return bunker
        # 濒死低血量且饱食度支持自然回血（food>=18）且周边 16 格无敌怪时，先短暂原地回血避免残血直冲怪区
        regen_hp_floor = 10.0 if not has_equipped_armor(state) else 8.0
        if (
            not self.target_goal
            and food >= 18.0
            and hp < regen_hp_floor
            and not any(state.st.get(flag) for flag in ("in_water", "submerged", "in_lava", "on_fire"))
        ):
            hostile = state.st.get("nearest_hostile") or {}
            hdist = 99.0
            if isinstance(hostile, Mapping) and hostile:
                try:
                    raw_d = float(hostile.get("dist", 99.0))
                    if math.isfinite(raw_d):
                        hdist = raw_d
                except (TypeError, ValueError):
                    pass
            if hdist >= 16.0:
                return PlanDecision(None, "idle", f"低血量自然回血驻留 (hp={hp:.1f}, food={food:.0f})")
        return None

    def choose_proactive_survival(self, state: State) -> Optional[PlanDecision]:
        """Sprint 1.4 主动生存策略层（营地存取、受控捡尸、食物底仓、夜宿跳过、铁盾补齐）。"""
        if self.target_goal:
            return None

        bunker = self._choose_nether_landing_bunker(state)
        if bunker is not None:
            return bunker

        st = state.snapshot()
        hp = float(st.get("health", 20))
        food = float(st.get("food", 20))
        dim = str(st.get("dimension") or "overworld").removeprefix("minecraft:").strip().lower() or "overworld"
        day = is_daytime(state)
        memo = memo_for(state)

        hostile = st.get("nearest_hostile") or {}
        hdist = 99.0
        if isinstance(hostile, Mapping) and hostile:
            try:
                raw_hdist = float(hostile.get("dist", 99.0))
                if math.isfinite(raw_hdist):
                    hdist = raw_hdist
            except (TypeError, ValueError):
                pass
        safe_10 = hdist >= 10.0
        safe_12 = hdist >= 12.0
        safe_gt_12 = hdist > 12.0

        # 5. Respawn Step 1: Camp Chest Withdraw (kit_chest 热备自动穿甲举盾 / vault_chest 取基础料)
        if _camp_plan.needs_camp_withdraw(state) and _camp_plan.is_near_camp(state, max_dist=64.0):
            wd = self._try_make("manage_camp_chest", {"mode": "withdraw"}, state)
            if wd is not None:
                return PlanDecision(wd, "rule", "重生/缺装回营地箱取装备")

        # 6. Respawn Step 2: Armed Controlled Corpse Recovery (严禁空手无食裸装捡尸)
        restore = inventory_food_restore(state)
        has_sword = any(state.count(s) >= 1 for s in _SURVIVAL_SWORDS)
        has_recovery_food = cooked_food_count(state) >= 1 or restore >= 4
        if not state.in_safe_mode and hp >= 14.0 and safe_10 and st.get("pos") is not None and has_sword and has_recovery_food:
            try:
                should_recover = bool(memo.should_attempt_corpse_recovery(st.get("pos"), dimension=dim)[0])
            except Exception:
                should_recover = False
            if should_recover:
                rec_sk = self._try_make("manage_camp_chest", {"mode": "recover_corpse"}, state)
                if rec_sk is not None:
                    return PlanDecision(rec_sk, "rule", "已武装受控捡尸回收铁器以上物资")

        # 6.5 Tier 0 Cold-Start Auto Camp Setup (石镐 + 熔炉 + 造箱木料且营地尚无箱时自动建营)
        camp = memo.get_camp(dimension=dim)
        has_camp_chest = isinstance(camp, Mapping) and bool(camp.get("chest") or camp.get("chests"))
        has_furnace_ready = state.count("furnace") >= 1 or memo.has_station("furnace", dimension=dim)
        has_table_or_12 = (
            state.count("chest") >= 1
            or state.count("crafting_table") >= 1
            or memo.has_station("crafting_table", dimension=dim)
            or (total_planks(state) + total_logs(state) * 4 >= 12)
        )
        cur_pos_y = None
        if isinstance(st.get("pos"), (list, tuple)) and len(st["pos"]) >= 2:
            try:
                cur_pos_y = float(st["pos"][1])
            except (TypeError, ValueError):
                cur_pos_y = None
        shaft = memo.get_mine_shaft(dimension=dim) if dim == "overworld" else None
        shaft_entry_y = None
        if isinstance(shaft, Mapping) and isinstance(shaft.get("entry"), (list, tuple)) and len(shaft["entry"]) >= 2:
            try:
                shaft_entry_y = float(shaft["entry"][1])
            except (TypeError, ValueError):
                shaft_entry_y = None
        near_surface_for_camp = cur_pos_y is None or (
            cur_pos_y >= 52.0 and (shaft_entry_y is None or cur_pos_y >= shaft_entry_y - 6.0)
        )
        if (
            dim == "overworld"
            and safe_12
            and near_surface_for_camp
            and not has_camp_chest
            and best_pickaxe(state) in STONE_OR_BETTER_PICKS
            and has_furnace_ready
            and _camp_plan.has_chest_or_materials(state)
            and has_table_or_12
        ):
            setup_sk = self._try_make("manage_camp_chest", {"mode": "setup"}, state)
            if setup_sk is not None:
                return PlanDecision(setup_sk, "rule", "冷启动石镐与熔炉就绪，自动建出生点营地箱(Tier 0)")

        # 6.6 Tier 1 Blast-Proof Spawn Shelter Auto Upgrade
        shelter_tier = int(camp.get("shelter_tier", 0)) if isinstance(camp, Mapping) else 0
        if dim == "overworld" and safe_12 and near_surface_for_camp and shelter_tier < 1 and total_cobble(state) >= 48:
            sh_sk = self._try_make("build_spawn_shelter", {"mode": "tier1_shelter"}, state)
            if sh_sk is not None:
                return PlanDecision(sh_sk, "rule", "圆石充足，建造出生点 5x5 圆石防爆避难所(Tier 1)")

        # 7. Food Reserve Gate & Ready Food/Iron Lease Retrieval
        if has_ready_food_lease(state) and safe_12:
            gf_lease = self._try_make("gather_food", {"target_restore": 16, "cook": True}, state)
            if gf_lease is not None:
                return PlanDecision(gf_lease, "rule", "熟食熔炉租约已就绪，回炉取回熟肉防遗弃")
        if any(l.expected_output == "iron_ingot" for l in lease_book_for(state).ready_leases(dimension=dim)) and safe_12:
            ir_lease = self._try_make("smelt_iron", {"count": 12, "async_lease": True}, state)
            if ir_lease is not None:
                return PlanDecision(ir_lease, "rule", "铁锭熔炉租约已就绪，回炉取回铁锭防遗弃")
        needs_food_now = food <= 17.0 or hp < 18.0
        proactive_food_ok = has_sword and not (self._hub_suspensions and not self._rank(state))
        if restore < 10 and day is True and safe_12 and (needs_food_now or proactive_food_ok):
            gf = self._try_make("gather_food", {"target_restore": 16, "cook": True}, state)
            if gf is not None:
                return PlanDecision(gf, "rule", f"食物底仓不足(restore={restore}<10)，白天补粮")
        if (
            raw_meat_count(state) >= 2
            and cooked_food_count(state) < 4
            and best_fuel_for(state, 1, allow_lava_bucket=False) is not None
            and has_furnace_ready
            and safe_12
        ):
            gf_cook = self._try_make("gather_food", {"target_restore": 16, "cook": True}, state)
            if gf_cook is not None:
                return PlanDecision(gf_cook, "rule", f"营地有熔炉且持有生肉({raw_meat_count(state)})与燃料，烤肉熟化补仓")

        # 8. Night Bed Sleep Fast-Path
        has_existing_or_held_bed = (
            any(state.count(b) >= 1 for b in _BED_ITEMS)
            or memo.has_station("bed", dimension=dim)
            or (isinstance(camp, Mapping) and camp.get("bed") is not None)
        )
        has_wool_for_bed = any(state.count(w) >= 3 for w in _WOOL_ITEMS)
        has_bed_ready = has_existing_or_held_bed or has_wool_for_bed
        if day is False and dim == "overworld" and safe_gt_12 and has_bed_ready:
            if not has_existing_or_held_bed and total_planks(state) < 3 and total_logs(state) < 1:
                gw = self._try_make("gather_wood", {"count": 2}, state)
                if gw is not None:
                    return PlanDecision(gw, "rule", "夜晚持羊毛造床缺木料，先采原木")
            else:
                sleep_sk = self._try_make("sleep_in_bed", {}, state)
                if sleep_sk is not None:
                    return PlanDecision(sleep_sk, "rule", "夜晚安全入睡跳过黑夜并绑定重生点")

        # 9. Camp Surplus Deposit (尊重 ReservationBook 预留物资；距营地 <=96 格高价/满包回存)
        if _camp_plan.has_surplus_for_deposit(state, reservations=self.reservations) and _camp_plan.is_near_camp(state, max_dist=96.0) and safe_12:
            dep = self._try_make("manage_camp_chest", {"mode": "deposit"}, state)
            if dep is not None:
                return PlanDecision(dep, "rule", "携带高价值或满包富余物资，回营地箱存仓")

        # 10. Post-Iron Shield Readiness Fast-Path (原木可自动拆板计入)
        if (
            state.count("shield") < 1
            and state.count("iron_ingot") >= 1
            and (state.count("iron_pickaxe") >= 1 or state.count("iron_ingot") >= 4)
            and total_planks(state) + total_logs(state) * 4 >= 6
            and safe_12
        ):
            sh = self._try_make("craft_iron_gear", {"item": "shield"}, state)
            if sh is not None:
                return PlanDecision(sh, "rule", "铁器底座补齐盾牌防身")

        return None

    def choose_survival_or_safe_mode(self, state: State) -> Optional[PlanDecision]:
        urgent = self.choose_urgent_survival_or_safe_mode(state)
        if urgent is not None:
            return urgent
        return self.choose_proactive_survival(state)

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
        return PlanDecision(
            None,
            "idle",
            reason,
            cooldown_blocked=self._last_expand_cooldown_blocked,
            blocked_goal_id=self._last_expand_blocked_goal_id,
        )

    def _rank(self, state: State):
        self._extract_pos(state)
        suspended = (
            {g.id for g in self.active_goals if self._environment_blocks and self._block_for_goal(g, state)}
            | set(self._abandoned_goals)
            | set(self._hub_suspensions)
        )
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
                spec = self.ledger_map.get(gid)
                if (
                    spec is not None
                    and self.is_hub_goal(gid)
                    and spec.max_fails_per_ep > 0
                    and self.ep_goal_fails[gid] >= spec.max_fails_per_ep
                ):
                    self.suspend_hub_goal(gid, state=None, reason="max_fails")

        self._last_selected_goal_id = None
        return gid

    def _choose_explore_fallback(
        self,
        state: State,
        *,
        scout_radius: int,
        survey_reason: str,
        scout_reason: str,
    ) -> tuple[Optional[Skill], str, bool]:
        """兜底防空转：优先在 scout_island 之前交替派发 survey_island(sectors=2, radius=32)。"""
        raw_dim = str((state.st or {}).get("dimension") or "overworld").removeprefix("minecraft:").strip().lower()
        survey_dim = raw_dim if raw_dim in ("overworld", "the_nether", "the_end") else "overworld"
        survey_cls = self.registry.get("survey_island")
        survey_args: dict[str, Any] = {"sectors": 2, "radius": 32}
        if survey_cls is not None and "dimension" in getattr(survey_cls, "params", {}):
            survey_args["dimension"] = survey_dim

        prefer_survey = (self._fallback_explore_turn % 2) == 0
        order = (
            (("survey_island", survey_args, survey_reason), ("scout_island", {"radius": scout_radius}, scout_reason))
            if prefer_survey
            else (("scout_island", {"radius": scout_radius}, scout_reason), ("survey_island", survey_args, survey_reason))
        )
        any_cooling = False
        for name, args, reason in order:
            inst, _, cooling = self._diagnose_skill(name, args, state)
            any_cooling = any_cooling or cooling
            if inst is not None:
                self._fallback_explore_turn += 1
                return inst, reason, False
        return None, "", any_cooling

    def choose(self, state: State) -> PlanDecision:
        self._last_selected_goal_id = None
        urgent = self.choose_survival_or_safe_mode(state)
        if urgent is not None:
            return urgent

        # 3) 目标台账解析与按性价比顺序派发
        self.refresh_environment_blocks(state)
        ranked = self._rank(state)
        if ranked:
            cooling_goal_id: Optional[str] = None
            for step in ranked:
                g = step.step_goal
                ready = all(check_item_expr(state, expr) for expr in g.prereq_items)
                if ready:
                    rem_inst, rem_why, eff_args = self._apply_nether_preflight(state, g.skill, g.args_dict)
                    if rem_inst is not None:
                        self._last_selected_goal_id = g.id
                        return PlanDecision(rem_inst, "rule", rem_why)
                    inst, _, cooling_only = self._diagnose_skill(g.skill, eff_args, state)
                    if inst is not None:
                        self._last_selected_goal_id = g.id
                        return PlanDecision(inst, "rule", f"推进目标 {g.id}（{g.title}）")
                    if cooling_only and cooling_goal_id is None:
                        cooling_goal_id = g.id
                if not ready or g.gather_when_blocked:
                    for req_expr, fb_skill, fb_args in _supplies(g, state):
                        inst, _, cooling_only = self._diagnose_skill(fb_skill, fb_args, state)
                        if inst is not None:
                            self._last_selected_goal_id = g.id
                            return PlanDecision(inst, "rule", f"为目标 {g.id} 补齐前置材料（{req_expr}）")
                        if cooling_only and cooling_goal_id is None:
                            cooling_goal_id = g.id

            sc_radius = 32 if best_pickaxe(state) not in STONE_OR_BETTER_PICKS else 36
            fb_skill, fb_reason, fb_cooling = self._choose_explore_fallback(
                state,
                scout_radius=sc_radius,
                survey_reason=f"目标 {ranked[0].step_goal.id} 当前位置受阻或冷却，先扇区外扩体检",
                scout_reason=f"目标 {ranked[0].step_goal.id} 当前位置受阻或冷却，外迁探索",
            )
            if fb_skill is not None:
                return PlanDecision(fb_skill, "rule", fb_reason)
            return PlanDecision(
                None,
                "idle",
                f"目标 {ranked[0].step_goal.id} 无符合 verified={self.min_verified} 且可启动的技能",
                cooldown_blocked=(cooling_goal_id is not None or fb_cooling),
                blocked_goal_id=cooling_goal_id or ranked[0].step_goal.id,
            )

        if self.target_goal:
            if self.blocked_targets(state, self.target_goal, refresh=False):
                cell = self._cell_key(state)
                if self._env_recovery_counts.get(cell, 0) < self._max_env_recovery_per_cell:
                    scout = self._try_make("scout_island", {"radius": 36}, state)
                    if scout is not None:
                        self._env_recovery_counts[cell] = self._env_recovery_counts.get(cell, 0) + 1
                        return PlanDecision(scout, "rule", "聚焦目标因环境挂起，外迁重新观察", environment_recovery=True)
                return PlanDecision(None, "idle", "等待聚焦目标的环境条件变化")
            return PlanDecision(
                None,
                "idle",
                f"目标 {self.target_goal} 已达成或本轮已止损",
                stop_episode=self.targets_exhausted(state),
            )

        # 常规战役模式：已激活台账目标全部达成后维持基础物资安全库存并持续探索
        if total_logs(state) * 4 + total_planks(state) < 8:
            gw = self._try_make("gather_wood", {"count": 3}, state)
            if gw is not None:
                return PlanDecision(gw, "rule", "维持木料安全库存")

        if total_cobble(state) < 16:
            ms = self._try_make("mine_stone", {"count": 8}, state)
            if ms is not None:
                return PlanDecision(ms, "rule", "维持圆石垫块与建造库存")

        fb_skill, fb_reason, fb_cooling = self._choose_explore_fallback(
            state,
            scout_radius=36,
            survey_reason="当前无可推进目标，优先分扇区外扩体检并唤醒挂起枢纽",
            scout_reason="基础石器齐备，持续扫描岛屿结构与矿脉",
        )
        if fb_skill is not None:
            return PlanDecision(fb_skill, "rule", fb_reason)
        return PlanDecision(
            None,
            "idle",
            f"无符合 verified={self.min_verified} 且可启动的兜底技能",
            cooldown_blocked=fb_cooling,
        )


class CampaignPlanner:
    """薄规划分派器：对外保持 `select()` 等接口不变，按 `strategy` 分派 `legacy` 或委托 `StrategyOrchestrator`。"""

    def __init__(
        self, *, mode: str = "campaign", strategy: Optional[str] = "rule", focus_skill: Optional[str] = None,
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
        hub_cooldown_s: float = DEFAULT_HUB_COOLDOWN_S,
        hub_wake_dist: float = DEFAULT_HUB_WAKE_MOVE_DIST,
        hub_max_restarts: int = DEFAULT_HUB_MAX_RESTARTS,
    ):
        from .strategy import DEFAULT_STRATEGY_REVIEW_S, StrategyOrchestrator, env_bool, env_float

        resolved_strategy = ("llm" if use_llm else "rule") if strategy is None else strategy
        if mode not in ("campaign", "focus"):
            raise ValueError(f"mode 只能是 'campaign' 或 'focus'；收到 {mode!r}")
        if resolved_strategy not in STRATEGIES:
            raise ValueError(f"strategy 只能是 {STRATEGIES} 之一；收到 {resolved_strategy!r}")
        if mode == "focus" and not focus_skill:
            raise ValueError("focus 模式必须指定 focus_skill")

        self.mode, self.strategy = mode, resolved_strategy
        self.use_llm = use_llm and resolved_strategy != "legacy"
        self.focus_skill, self.focus_args = focus_skill, dict(focus_args or {})
        self.llm = llm or (LLMClient() if (self.use_llm or resolved_strategy == "llm") else None)
        self.min_verified, self.target_goal = min_verified, target_goal
        self.registry = REGISTRY if registry is None else registry
        self.reservations = ReservationBook()
        self._active_material_consumer: Optional[str] = None
        self.rules = RulePlanner(
            self.registry, ledger=ledger, include_stages=include_stages,
            target_goal=target_goal, telemetry=telemetry, min_verified=min_verified,
            reservations=self.reservations,
            hub_cooldown_s=hub_cooldown_s,
            hub_wake_dist=hub_wake_dist,
            hub_max_restarts=hub_max_restarts,
            clock=clock,
        )
        self.on_llm_telemetry, self.on_objective_telemetry = on_llm_telemetry, on_objective_telemetry
        self.review_interval_s = (
            env_float("MC_STRATEGY_REVIEW_S", DEFAULT_STRATEGY_REVIEW_S) if review_interval_s is None else max(0.0, float(review_interval_s))
        )
        self.require_iron = env_bool("MC_STRATEGY_REQUIRE_IRON", False) if require_iron is None else bool(require_iron)
        self.max_seconds, self._clock = max(0.0, float(max_seconds)), clock
        self._started_mono = self._clock()
        self.subgoal_provider = self._subgoal_snapshot if subgoal_provider is None else subgoal_provider
        self.reserve_provider = self._reservation_snapshot if reserve_provider is None else reserve_provider
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

    def _subgoal_snapshot(self, state: State) -> dict[str, Any]:
        eval_all = getattr(subgoals_mod, "evaluate_all_subgoals", None)
        if callable(eval_all):
            try:
                evaluated = eval_all(state, reservations=self.reservations)
            except TypeError:
                evaluated = eval_all(state)
            if isinstance(evaluated, Mapping):
                return dict(evaluated)

        st = state.snapshot()
        dim = str(st.get("dimension") or "overworld")
        memo = memo_for(state)
        stations = memo.query("station", dimension=dim)
        station_names = {e.block for e in stations if e.block}
        has_table = (
            state.count("crafting_table") > 0
            or "crafting_table" in station_names
            or has_known_table(st.get("pos"))
        )
        has_furnace = state.count("furnace") > 0 or "furnace" in station_names
        structures = memo.query("structure", dimension=dim)
        landmark_tags = sorted({e.tag for e in structures if e.tag})
        has_portal = (
            any(e.tag == "ruined_portal" for e in structures)
            or bool(memo.query("obsidian", dimension=dim))
        )
        hostile = st.get("nearest_hostile")
        if isinstance(hostile, Mapping) and hostile:
            safe_enc = subgoals_mod.safe_encounter(
                state,
                hostile,
                str(hostile.get("name") or "hostile"),
                dimension=dim,
                retreat_safe=not state.in_safe_mode,
                min_health=12.0,
            )
        else:
            safe_enc = bool(
                not state.in_safe_mode
                and float(st.get("health", 20)) >= 12.0
                and not any(st.get(h) for h in ("in_lava", "in_water", "submerged", "on_fire"))
            )
        return {
            "item_available": {
                item: item_available(state, item, count, allocatable=self.reservations.available(state, item))
                for item, count in (("iron_ingot", 3), ("diamond", 4), ("wheat_seeds", 3))
            },
            "tool_ready": {tier: tool_ready(state, "pickaxe", tier, 1) for tier in ("iron", "diamond")},
            "station_ready": {"crafting_table": has_table, "furnace": has_furnace},
            "landmark_reachable": landmark_tags if landmark_tags else False,
            "safe_encounter": safe_enc,
            "active_portal": has_portal,
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

    def abandon_goal(self, goal_id: Optional[str], *, state: Optional[State] = None) -> bool:
        abandoned = self.rules.abandon_goal(goal_id, state=state)
        if abandoned and self.strategy != "legacy" and self.current_objective is not None:
            if self.current_objective.goal_id == goal_id and self.scheduler is not None:
                self.scheduler.invalidate()
        return abandoned

    def clear_tactical_cooldowns(
        self, state: State, goal_id: Optional[str] = None, *, include_relocate: bool = False
    ) -> list[str]:
        return self.rules.clear_tactical_cooldowns(state, goal_id=goal_id, include_relocate=include_relocate)

    def make_relocate_skill(self, state: State) -> Optional[Skill]:
        return self.rules.make_relocate_skill(state)

    def record_environment_block(self, skill: Skill, error: EnvBlocked, state: State) -> None:
        self.release_skill_materials()
        self.rules.record_environment_block(skill, error, state)
        self._last_selected_goal_id = None

    def blocked_targets(self, state: State, target_goals: Any, *, refresh: bool = True) -> dict[str, EnvBlocked]:
        return self.rules.blocked_targets(state, target_goals, refresh=refresh)

    def record_skill_outcome(self, skill_name: str, *, ok: bool, elapsed_s: float = 0.0, completed_advs: Collection[str] = ()) -> None:
        self.release_skill_materials()
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
        if getattr(state, "safety_intent", None) and "emergency_escape" in self.registry:
            esc = self.rules._choose_safety_escape(state)
            if esc is not None:
                return esc
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
            return PlanDecision(
                None, "idle", f"focus 目标 {skill.goal} 冷却中",
                cooldown_blocked=True, blocked_goal_id=self.target_goal,
            )
        if not skill.can_start(state):
            return PlanDecision(None, "idle", f"focus 技能参数 {self.focus_args} 当前不可启动")
        return PlanDecision(skill, "focus", f"定向验收 {skill}")

    def select(self, state: State, recent_failures: list[str]) -> PlanDecision:
        bind_reservation_book(state, self.reservations)
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
            return self._select_focus(state)
        if self.strategy == "legacy":
            dec = self.rules.choose(state)
            self._last_selected_goal_id = self.rules._last_selected_goal_id
            return dec
        if self.strategy == "rule" and self.use_llm:
            return self._select_legacy_skill_llm(state, recent_failures)
        return self._strategy.select(state, recent_failures)
