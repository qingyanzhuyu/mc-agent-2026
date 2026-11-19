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
from ..runtime import OXYGEN_FULL, EnvBlocked, State, _inv_fingerprint
from ..skills import REGISTRY, Skill, bind_args, make
from ..skills import base as skill_base
from ..skills.items import best_pickaxe
from ..skills.world_memo import format_memo_summary, memo_for
from . import subgoals as subgoals_mod
from .context import DecisionContext, ObjectiveRecord
from .goals import (
    DEFAULT_CAMPAIGN_STAGES, GOALS, OFFICIAL_CAMPAIGN_STAGES, STONE_OR_BETTER_PICKS, GoalSpec,
    check_item_expr, diagnose_goal_gap, is_goal_achieved, is_goal_stopped_out,
    is_prereq_satisfied, normalize_goal_ids, rank_actionable_goals, resolve_active_goals,
)
from .reserve import ReservationBook
from .subgoals import item_available, tool_ready
from .starter_skills import PROTECTED_ZONES, can_afford_gear, has_known_table, total_cobble, total_logs, total_planks

if TYPE_CHECKING:
    from .strategy import StrategyOrchestrator

STRATEGIES: tuple[str, ...] = ("legacy", "rule", "llm")
_SAFETY_INTENT_DEFAULT_TTL_S: float = 15.0
_ESCAPE_INTENT_KINDS: frozenset[str] = frozenset({"flee", "douse", "surface", "escape_lava", "lava", "void_risk"})


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
        flee_dist = float(getattr(state.config, "hostile_flee_dist", 6.0))
        if hostile and float(hostile.get("dist", 99.0)) < flee_dist:
            return "flee", f"{why} + {hostile.get('name')} 距离 {hostile.get('dist')}"

    # 直接物理险情兜底（即使后台 monitor 未启动或尚未刷出 safety_intent）
    if st.get("in_lava"):
        return "escape_lava", "in_lava"
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

    def _cell_key(self, state: State) -> tuple[str, int, int]:
        st = state.snapshot()
        pos = st.get("pos") or [0.0, 64.0, 0.0]
        try:
            cx, cz = math.floor(float(pos[0]) / 16.0), math.floor(float(pos[2]) / 16.0)
        except (TypeError, ValueError, IndexError):
            cx, cz = 0, 0
        return str(st.get("dimension") or "overworld"), cx, cz

    def abandon_goal(self, goal_id: Optional[str]) -> bool:
        """将当前反复空转失败的目标在本轮标记放弃，使规划器立即切向下一个可得分目标。"""
        if not goal_id or goal_id not in self.ledger_map:
            return False
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
            self._last_expand_cooldown_blocked = True
            self._last_expand_blocked_goal_id = cooling_step_id

        return None, "；".join(reasons[:3]) or f"目标 {ranked[0].step_goal.id} 无可启动技能"

    def choose_survival_or_safe_mode(self, state: State) -> Optional[PlanDecision]:
        """在一切目标之上的生存补给、`safe_mode` 避险与 `SafetyMonitor` 脱险意图处理（命中即返回，否则返回 None）。"""
        hp = float(state.st.get("health", 20))
        food = float(state.st.get("food", 20))
        raw_intent = getattr(state, "safety_intent", None)
        intent_kind = (
            str(raw_intent.get("kind") or raw_intent.get("intent") or "").strip().lower()
            if isinstance(raw_intent, Mapping)
            else (raw_intent.strip().lower() if isinstance(raw_intent, str) else "")
        )

        if food <= 14 or (hp < 18 and food < 20) or (intent_kind == "heal" and food < 20):
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
        return PlanDecision(
            None,
            "idle",
            reason,
            cooldown_blocked=self._last_expand_cooldown_blocked,
            blocked_goal_id=self._last_expand_blocked_goal_id,
        )

    def _rank(self, state: State):
        suspended = (
            {g.id for g in self.active_goals if self._environment_blocks and self._block_for_goal(g, state)}
            | set(self._abandoned_goals)
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
            cooling_goal_id: Optional[str] = None
            for step in ranked:
                g = step.step_goal
                ready = all(check_item_expr(state, expr) for expr in g.prereq_items)
                if ready:
                    inst, _, cooling_only = self._diagnose_skill(g.skill, g.args_dict, state)
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
            sc, _, sc_cooling = self._diagnose_skill("scout_island", {"radius": sc_radius}, state)
            if sc is not None:
                return PlanDecision(sc, "rule", f"目标 {ranked[0].step_goal.id} 当前位置受阻或冷却，外迁探索")
            return PlanDecision(
                None,
                "idle",
                f"目标 {ranked[0].step_goal.id} 无符合 verified={self.min_verified} 且可启动的技能",
                cooldown_blocked=(cooling_goal_id is not None or sc_cooling),
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

        sc, _, sc_cooling = self._diagnose_skill("scout_island", {"radius": 36}, state)
        if sc is not None:
            return PlanDecision(sc, "rule", "基础石器齐备，持续扫描岛屿结构与矿脉")
        return PlanDecision(
            None,
            "idle",
            f"无符合 verified={self.min_verified} 且可启动的兜底技能",
            cooldown_blocked=sc_cooling,
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

    def abandon_goal(self, goal_id: Optional[str]) -> bool:
        abandoned = self.rules.abandon_goal(goal_id)
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
