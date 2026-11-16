"""战略层目标选择契约：Objective 数据结构、目标菜单渲染、合法性校验与提示词。

职责边界（遵循 AGENTS.md §9 与 J11 / J17）：
- 战略层（规则或模型）统一输出 `Objective(goal_id, budget_s, why, source, started_at)`，两者可互换、可对照。
- 模型在这条路径上只选台账内的 `goal_id` 与时间预算，不接触坐标、实体 id 或写死科技顺序。
- 校验器强制核验：在台账内、stage 允许、本局未达成、未被环境挂起、`RulePlanner(target_goal=goal_id)`
  可展开出至少一个可启动技能；不合法时回喂错误原文重问一次，仍不合法交由调用方回退规则 `Objective`。
- 本文件只出战略决策，不调 `Executor`、不直接发动作 HTTP。
"""
from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Collection, Mapping, Optional, Sequence

from ..runtime import State
from .llm import LLMClient, LLMError

if TYPE_CHECKING:
    from ..loop.goals import GoalSpec

OBJECTIVE_SOURCES: tuple[str, ...] = ("rule", "llm")
MIN_BUDGET_S: float = 60.0
MAX_BUDGET_S: float = 1800.0
_FORBIDDEN_KEYS: frozenset[str] = frozenset({"pos", "x", "y", "z", "entity_id"})

OBJECTIVE_SYSTEM_PROMPT = """你是 Minecraft 8 小时生存赛的战略目标规划器。每次只输出一个单行 JSON 对象，不要输出任何额外文字，不要使用 Markdown 代码块。

格式必须严格是：
{"goal_id": "...", "budget_s": 180, "why": "..."}

要求（错一条就会被校验拒绝并重问）：
- goal_id：只能从「可选目标」清单里挑选一个未达成、未挂起的目标 id，禁止自创或选择挂起目标
- budget_s：分配给该目标的时间预算（秒，60 到 1800 之间的数字；可省略，省略时采用台账默认预算）
- why：一句话简短理由（30 字以内），禁止包含任何坐标或实体编号
- 前置自动展开：选定目标后，底层战术规则器会自动按依赖链补齐未完成的前置目标与原料，无需手排步骤
- 决策原则：综合剩余比赛时间、昼夜、当前背包物资、目标分值与预计剩余成本权衡性价比；避开近期连续失败或刚超时的目标"""


def clamp_budget(value: Any, default_budget_s: float = 180.0) -> float:
    """将时间预算夹紧在 [60, 1800] 秒区间；缺省取台账默认预算并同样夹紧。"""
    raw = default_budget_s if value is None else float(value)
    if not math.isfinite(raw):
        raw = default_budget_s
    return round(max(MIN_BUDGET_S, min(MAX_BUDGET_S, raw)), 1)


def _is_finite_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val)


@dataclass(frozen=True)
class Objective:
    """战略层输出的目标契约（规则战略与模型战略同构，可互换对照）。"""

    goal_id: str
    budget_s: float
    why: str = ""
    source: str = "rule"          # "rule" | "llm"
    started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not isinstance(self.goal_id, str) or not self.goal_id.strip():
            raise ValueError("Objective.goal_id 必须是非空字符串")
        if self.source not in OBJECTIVE_SOURCES:
            raise ValueError(f"Objective.source 必须是 {OBJECTIVE_SOURCES} 之一；收到 {self.source!r}")
        object.__setattr__(self, "budget_s", clamp_budget(self.budget_s))

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "budget_s": self.budget_s,
            "why": self.why,
            "source": self.source,
            "started_at": self.started_at,
        }


@dataclass(frozen=True)
class ObjectiveCandidate:
    """目标菜单中的单行可选目标视图（按台账声明顺序展示，不透传规则排序）。"""

    goal_id: str
    title: str
    score: int
    est_remaining_cost_s: float
    prereqs_met: bool
    missing_prereqs: tuple[str, ...] = ()
    default_budget_s: float = 180.0


@dataclass(frozen=True)
class SuspendedGoalInfo:
    """被环境阻塞挂起的目标及其唤醒条件。"""

    goal_id: str
    title: str
    score: int
    wake_on: str
    missing_predicate: str
    message: str


def _loop_modules():
    from ..loop import goals as goals_mod
    from ..loop.policy import RulePlanner
    return goals_mod, RulePlanner


def effective_include_stages(
    min_verified: str = "offline",
    include_stages: Optional[Collection[str]] = None,
) -> tuple[str, ...]:
    goals_mod, _ = _loop_modules()
    if include_stages is not None:
        return tuple(include_stages)
    return goals_mod.OFFICIAL_CAMPAIGN_STAGES if min_verified == "live" else goals_mod.DEFAULT_CAMPAIGN_STAGES


def compute_remaining_cost_and_prereqs(
    goal: "GoalSpec",
    state: State,
    ledger_map: Mapping[str, "GoalSpec"],
    *,
    completed_advs: Collection[str] = (),
    telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> tuple[float, bool, tuple[str, ...]]:
    """计算目标剩余链路预计成本（秒）、直接/传递前置是否全部满足及缺失前置列表。"""
    goals_mod, _ = _loop_modules()
    seen: set[str] = set()
    missing_prereqs: list[str] = []
    remaining_cost = 0.0

    def _walk(node: "GoalSpec", is_root: bool) -> None:
        nonlocal remaining_cost
        if node.id in seen:
            return
        seen.add(node.id)
        if not is_root and goals_mod.is_prereq_satisfied(node, state, completed_advs):
            return
        for pid in node.prereq_goals:
            parent = ledger_map.get(pid)
            if parent is not None:
                _walk(parent, False)
        if not is_root:
            missing_prereqs.append(node.id)
        if is_root and goals_mod.is_goal_achieved(node, state, completed_advs):
            return
        remaining_cost += goals_mod.effective_goal_cost(node, telemetry)

    _walk(goal, True)
    items_ok = all(goals_mod.check_item_expr(state, expr) for expr in goal.prereq_items)
    prereqs_met = (len(missing_prereqs) == 0) and items_ok
    return round(max(1.0, remaining_cost), 1), prereqs_met, tuple(missing_prereqs)


def _make_probe_planner(
    target_goal: str,
    *,
    ledger: tuple["GoalSpec", ...],
    registry: Optional[dict],
    min_verified: str,
    include_stages: tuple[str, ...],
    completed_advs: Collection[str],
    ep_goal_fails: Optional[Mapping[str, int]],
    ep_goal_elapsed: Optional[Mapping[str, float]],
    telemetry: Optional[Mapping[str, Mapping[str, Any]]],
    environment_blocks: Optional[Mapping[str, Any]],
):
    _, RulePlanner = _loop_modules()
    probe = RulePlanner(
        registry=registry,
        ledger=ledger,
        include_stages=include_stages,
        target_goal=target_goal,
        telemetry=telemetry,
        min_verified=min_verified,
    )
    probe.completed_advancements = set(completed_advs)
    probe.ep_goal_fails = dict(ep_goal_fails or {})
    probe.ep_goal_elapsed = dict(ep_goal_elapsed or {})
    if environment_blocks:
        probe._environment_blocks = {
            k: type(v)(v.error, v.observation) for k, v in dict(environment_blocks).items()
        }
    return probe


def collect_objective_candidates(
    state: State,
    *,
    ledger: Optional[Sequence["GoalSpec"]] = None,
    registry: Optional[dict] = None,
    min_verified: str = "offline",
    include_stages: Optional[Collection[str]] = None,
    focus_goals: Any = None,
    completed_advs: Collection[str] = (),
    ep_goal_fails: Optional[Mapping[str, int]] = None,
    ep_goal_elapsed: Optional[Mapping[str, float]] = None,
    telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
    environment_blocks: Optional[Mapping[str, Any]] = None,
) -> tuple[list[ObjectiveCandidate], list[SuspendedGoalInfo]]:
    """按台账静态声明顺序收集「可选且未达成目标」与「被环境挂起目标」（不透传规则性价比排序）。"""
    goals_mod, _ = _loop_modules()
    all_goals: tuple["GoalSpec", ...] = tuple(goals_mod.GOALS if ledger is None else ledger)
    ledger_map = {g.id: g for g in all_goals}
    eff_stages = effective_include_stages(min_verified, include_stages)
    allowed_stage_set = set(eff_stages)

    focus_closure_ids: Optional[set[str]] = None
    if focus_goals:
        focus_closure = goals_mod.resolve_active_goals(all_goals, include_stages=eff_stages, target_goal=focus_goals)
        focus_closure_ids = {g.id for g in focus_closure}

    eligible: list[ObjectiveCandidate] = []
    suspended: list[SuspendedGoalInfo] = []

    for g in all_goals:
        if focus_closure_ids is not None:
            if g.id not in focus_closure_ids:
                continue
        elif g.stage not in allowed_stage_set:
            continue

        if goals_mod.is_goal_achieved(g, state, completed_advs):
            continue

        probe = _make_probe_planner(
            g.id,
            ledger=all_goals,
            registry=registry,
            min_verified=min_verified,
            include_stages=eff_stages,
            completed_advs=completed_advs,
            ep_goal_fails=ep_goal_fails,
            ep_goal_elapsed=ep_goal_elapsed,
            telemetry=telemetry,
            environment_blocks=environment_blocks,
        )
        probe.refresh_environment_blocks(state)
        blocked_map = probe.blocked_targets(state, g.id, refresh=False)
        if g.id in blocked_map:
            err = blocked_map[g.id]
            suspended.append(
                SuspendedGoalInfo(
                    goal_id=g.id,
                    title=g.title,
                    score=g.score,
                    wake_on=err.wake_on,
                    missing_predicate=err.missing_predicate,
                    message=str(err),
                )
            )
            continue

        if goals_mod.is_goal_stopped_out(g, ep_goal_fails, ep_goal_elapsed):
            continue

        skill, _ = probe.expand_objective_skill(state)
        if skill is None:
            continue

        rem_cost, prereqs_met, missing = compute_remaining_cost_and_prereqs(
            g, state, ledger_map, completed_advs=completed_advs, telemetry=telemetry
        )
        eligible.append(
            ObjectiveCandidate(
                goal_id=g.id,
                title=g.title,
                score=g.score,
                est_remaining_cost_s=rem_cost,
                prereqs_met=prereqs_met,
                missing_prereqs=missing,
                default_budget_s=clamp_budget(g.budget_s),
            )
        )
    return eligible, suspended


def render_objective_menu(
    eligible: Sequence[ObjectiveCandidate],
    suspended: Sequence[SuspendedGoalInfo] = (),
) -> str:
    """渲染紧凑目标菜单（不含规则排名提示、不含坐标或实体 id）。"""
    lines = ["可选目标（只能从这里选 goal_id）："]
    if not eligible:
        lines.append("- （无可选目标）")
    else:
        for item in eligible:
            if item.prereqs_met:
                prereq_tag = "前置=已满足"
            elif item.missing_prereqs:
                short = ",".join(item.missing_prereqs[:2])
                more = f"+{len(item.missing_prereqs) - 2}" if len(item.missing_prereqs) > 2 else ""
                prereq_tag = f"前置=缺({short}{more})"
            else:
                prereq_tag = "前置=缺原料"
            lines.append(
                f"- {item.goal_id}: {item.title} | {item.score}分 | 预计剩余{item.est_remaining_cost_s:.0f}s "
                f"| 默认预算{item.default_budget_s:.0f}s | {prereq_tag}"
            )
    if suspended:
        lines.append("被挂起目标（等待唤醒条件，当前不可选）：")
        for s in suspended:
            lines.append(
                f"- {s.goal_id}: {s.title} | {s.score}分 | 缺失={s.missing_predicate} | 唤醒={s.wake_on}"
            )
    else:
        lines.append("被挂起目标：无")
    return "\n".join(lines)


def resolve_objective(
    obj: Any,
    state: State,
    *,
    ledger: Optional[Sequence["GoalSpec"]] = None,
    registry: Optional[dict] = None,
    min_verified: str = "offline",
    include_stages: Optional[Collection[str]] = None,
    focus_goals: Any = None,
    completed_advs: Collection[str] = (),
    ep_goal_fails: Optional[Mapping[str, int]] = None,
    ep_goal_elapsed: Optional[Mapping[str, float]] = None,
    telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
    environment_blocks: Optional[Mapping[str, Any]] = None,
    source: str = "llm",
    now: Optional[float] = None,
) -> tuple[Optional[Objective], list[str]]:
    """校验候选字典并构造 `Objective`；不合法返回 `(None, 错误原文列表)`。"""
    if not isinstance(obj, dict):
        return None, ["输出必须是 JSON object"]

    errs: list[str] = []
    forbidden = sorted(set(obj) & _FORBIDDEN_KEYS)
    if forbidden:
        errs.append(f"战略目标不允许包含坐标或实体字段: {forbidden}")

    goal_id = obj.get("goal_id")
    if not isinstance(goal_id, str) or not goal_id.strip():
        errs.append("goal_id 必须是非空字符串")

    if "why" in obj and obj["why"] is not None and not isinstance(obj["why"], str):
        errs.append("why 必须是字符串")

    raw_budget = obj.get("budget_s")
    if raw_budget is not None and not _is_finite_number(raw_budget):
        errs.append("budget_s 必须是有限数字")

    if errs:
        return None, errs

    assert isinstance(goal_id, str)
    goal_id = goal_id.strip()
    goals_mod, _ = _loop_modules()
    all_goals: tuple["GoalSpec", ...] = tuple(goals_mod.GOALS if ledger is None else ledger)
    ledger_map = {g.id: g for g in all_goals}
    eff_stages = effective_include_stages(min_verified, include_stages)

    goal = ledger_map.get(goal_id)
    if goal is None:
        eligible, _ = collect_objective_candidates(
            state,
            ledger=all_goals,
            registry=registry,
            min_verified=min_verified,
            include_stages=eff_stages,
            focus_goals=focus_goals,
            completed_advs=completed_advs,
            ep_goal_fails=ep_goal_fails,
            ep_goal_elapsed=ep_goal_elapsed,
            telemetry=telemetry,
            environment_blocks=environment_blocks,
        )
        valid_ids = ", ".join(c.goal_id for c in eligible) or "（无）"
        return None, [f"目标 {goal_id!r} 不在目标台账内；当前可选目标: {valid_ids}"]

    if focus_goals:
        focus_closure = goals_mod.resolve_active_goals(all_goals, include_stages=eff_stages, target_goal=focus_goals)
        if goal_id not in {g.id for g in focus_closure}:
            return None, [f"目标 {goal_id} 不在本轮聚焦目标 {focus_goals!r} 的依赖闭包内"]
    if goal.stage not in set(eff_stages) and not focus_goals:
        return None, [
            f"目标 {goal_id} 的 stage={goal.stage!r} 不在当前允许阶段 {list(eff_stages)} 内（min_verified={min_verified}）"
        ]

    if goals_mod.is_goal_achieved(goal, state, completed_advs):
        return None, [f"目标 {goal_id}（{goal.title}）在本局已达成，请换未达成的目标"]

    probe = _make_probe_planner(
        goal_id,
        ledger=all_goals,
        registry=registry,
        min_verified=min_verified,
        include_stages=eff_stages,
        completed_advs=completed_advs,
        ep_goal_fails=ep_goal_fails,
        ep_goal_elapsed=ep_goal_elapsed,
        telemetry=telemetry,
        environment_blocks=environment_blocks,
    )
    probe.refresh_environment_blocks(state)
    blocked_map = probe.blocked_targets(state, goal_id, refresh=False)
    if goal_id in blocked_map:
        err = blocked_map[goal_id]
        return None, [f"目标 {goal_id} 当前被环境挂起：{err}（唤醒条件={err.wake_on}）"]

    skill, expand_reason = probe.expand_objective_skill(state)
    if skill is None:
        return None, [f"目标 {goal_id} 无法展开出可启动技能：{expand_reason}"]

    budget = clamp_budget(raw_budget, default_budget_s=goal.budget_s)
    why = str(obj.get("why") or "").strip()
    started = time.monotonic() if now is None else float(now)
    return Objective(
        goal_id=goal_id,
        budget_s=budget,
        why=why,
        source=source,
        started_at=started,
    ), []


def validate_objective(
    obj: Any,
    state: State,
    *,
    ledger: Optional[Sequence["GoalSpec"]] = None,
    registry: Optional[dict] = None,
    min_verified: str = "offline",
    include_stages: Optional[Collection[str]] = None,
    focus_goals: Any = None,
    completed_advs: Collection[str] = (),
    ep_goal_fails: Optional[Mapping[str, int]] = None,
    ep_goal_elapsed: Optional[Mapping[str, float]] = None,
    telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
    environment_blocks: Optional[Mapping[str, Any]] = None,
) -> tuple[bool, list[str]]:
    """校验模型的目标选择。返回 `(是否合法, 错误列表)`；错误原文可直接回喂重问。"""
    resolved, errs = resolve_objective(
        obj,
        state,
        ledger=ledger,
        registry=registry,
        min_verified=min_verified,
        include_stages=include_stages,
        focus_goals=focus_goals,
        completed_advs=completed_advs,
        ep_goal_fails=ep_goal_fails,
        ep_goal_elapsed=ep_goal_elapsed,
        telemetry=telemetry,
        environment_blocks=environment_blocks,
        source="llm",
    )
    return resolved is not None, errs


def from_objective_choice(
    obj: Any,
    state: State,
    *,
    ledger: Optional[Sequence["GoalSpec"]] = None,
    registry: Optional[dict] = None,
    min_verified: str = "offline",
    include_stages: Optional[Collection[str]] = None,
    focus_goals: Any = None,
    completed_advs: Collection[str] = (),
    ep_goal_fails: Optional[Mapping[str, int]] = None,
    ep_goal_elapsed: Optional[Mapping[str, float]] = None,
    telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
    environment_blocks: Optional[Mapping[str, Any]] = None,
    source: str = "llm",
    now: Optional[float] = None,
) -> Objective:
    """校验通过后返回 `Objective`；不通过抛 `ValueError`。"""
    resolved, errs = resolve_objective(
        obj,
        state,
        ledger=ledger,
        registry=registry,
        min_verified=min_verified,
        include_stages=include_stages,
        focus_goals=focus_goals,
        completed_advs=completed_advs,
        ep_goal_fails=ep_goal_fails,
        ep_goal_elapsed=ep_goal_elapsed,
        telemetry=telemetry,
        environment_blocks=environment_blocks,
        source=source,
        now=now,
    )
    if resolved is None:
        raise ValueError("目标选择不合法: " + " | ".join(errs))
    return resolved


def choose_objective(
    llm: LLMClient,
    decision_context: str,
    state: State,
    *,
    menu_text: Optional[str] = None,
    ledger: Optional[Sequence["GoalSpec"]] = None,
    registry: Optional[dict] = None,
    min_verified: str = "offline",
    include_stages: Optional[Collection[str]] = None,
    focus_goals: Any = None,
    completed_advs: Collection[str] = (),
    ep_goal_fails: Optional[Mapping[str, int]] = None,
    ep_goal_elapsed: Optional[Mapping[str, float]] = None,
    telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
    environment_blocks: Optional[Mapping[str, Any]] = None,
    attempts: int = 2,
    thinking: bool = False,
    total_timeout: Optional[float] = None,
    usage_out: Optional[dict[str, int]] = None,
    fallback: Optional[Callable[[], Optional[Objective]]] = None,
    now: Optional[float] = None,
) -> tuple[Optional[Objective], list[str], int]:
    """让模型根据决策上下文选一个战略 `Objective`（非法时回喂错误重问一次，仍非法回退 `fallback`）。"""
    check_kwargs: dict[str, Any] = {
        "ledger": ledger,
        "registry": registry,
        "min_verified": min_verified,
        "include_stages": include_stages,
        "focus_goals": focus_goals,
        "completed_advs": completed_advs,
        "ep_goal_fails": ep_goal_fails,
        "ep_goal_elapsed": ep_goal_elapsed,
        "telemetry": telemetry,
        "environment_blocks": environment_blocks,
    }
    user = decision_context.strip()
    if menu_text is not None and menu_text.strip() and "可选目标" not in user:
        user = f"{user}\n\n{menu_text.strip()}"
    elif "可选目标" not in user:
        elig, susp = collect_objective_candidates(state, **check_kwargs)
        user = f"{user}\n\n{render_objective_menu(elig, susp)}"

    errors: list[str] = []
    used = 0
    raw_obj: Optional[dict] = None
    try:
        raw_obj, errors, used = llm.ask_json(
            OBJECTIVE_SYSTEM_PROMPT,
            user,
            lambda o: validate_objective(o, state, **check_kwargs),
            attempts=attempts,
            thinking=thinking,
            total_timeout=total_timeout,
            usage_out=usage_out,
        )
    except LLMError as exc:
        errors = [f"LLMError: {exc}"]
        used = max(1, used)

    if raw_obj is not None:
        resolved, final_errs = resolve_objective(raw_obj, state, source="llm", now=now, **check_kwargs)
        if resolved is not None:
            return resolved, [], used
        errors = final_errs

    if fallback is not None:
        return fallback(), errors, used
    return None, errors, used
