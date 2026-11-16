"""战略层决策上下文组装（由 harness 客观投影世界状态，不透传规则答案）。

硬约束（见 P9 契约与 J11 / J17）：
1. 必须包含：有资格且未达成目标（id、标题、分值、预计剩余成本、前置是否满足）、被挂起目标及唤醒条件、
   背包摘要、剩余比赛时间与昼夜、最近 5 个目标的结果与失败分类、当前 Objective 及已用时间。
2. 严禁包含：`RulePlanner.current_goal_hint()`、写死的科技顺序、任何三维/区块坐标、实体 id。
3. 预留可选扩展字段 `subgoal_predicates` 与 `reservations`，供后续接入 `loop/subgoals.py` 与 `loop/reserve.py`；
   本模块零导入依赖那两个尚未合入的文件。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Collection, Mapping, Optional, Sequence

from ..planner.objective import (
    Objective,
    ObjectiveCandidate,
    SuspendedGoalInfo,
    collect_objective_candidates,
    render_objective_menu,
)
from ..runtime import State, _inv_fingerprint
from ..skills.items import best_pickaxe
from .goals import GOALS, GoalSpec
from .starter_skills import best_axe, best_food, total_cobble, total_logs, total_planks

DEFAULT_MATCH_SECONDS: float = 8 * 3600.0
MAX_CONTEXT_CHARS: int = 2000

_COORD_TRIPLE_RE = re.compile(
    r"[\[\(]\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*[\]\)]"
)
_POS_KV_RE = re.compile(r"\bpos\s*[=:]\s*(\[[^\]]*\]|\([^\)]*\)|[^\s,;，；]+)", re.IGNORECASE)
_ENTITY_ID_RE = re.compile(r"\bentity_id\s*[=:]\s*[^\s,;，；\)\]]+", re.IGNORECASE)


def scrub_sensitive_text(text: str) -> str:
    """剥离自由文本里可能夹带的坐标三元组、`pos=...` 与 `entity_id=...`（遵守 J17）。"""
    if not text:
        return ""
    cleaned = _COORD_TRIPLE_RE.sub("<位置>", str(text))
    cleaned = _POS_KV_RE.sub("pos=<已隐去>", cleaned)
    cleaned = _ENTITY_ID_RE.sub("entity_id=<已隐去>", cleaned)
    return " ".join(cleaned.split())


def is_daytime(state: State) -> Optional[bool]:
    """从 `State` 提取当前昼夜布尔态（白天=True，夜晚=False，未知=None）。"""
    st = state.st or {}
    raw_day = st.get("is_day")
    if isinstance(raw_day, bool):
        return raw_day
    tod = st.get("time_of_day")
    if isinstance(tod, (int, float)) and not isinstance(tod, bool):
        tick = float(tod) % 24000.0
        return not (state.config.night_start <= tick < state.config.night_end)
    return None


def describe_day_phase(state: State) -> str:
    day = is_daytime(state)
    if day is True:
        return "白天"
    if day is False:
        return "夜晚"
    return "未知"


def classify_failure_kind(error_code: str = "", message: str = "") -> str:
    """将技能中断或目标未完成原因归入稳定的战略失败分类。"""
    code = (error_code or "").lower()
    msg = (message or "").lower()
    if not code and not msg:
        return "none"
    if "env_blocked" in code or "环境挂起" in msg:
        return "env_blocked"
    if "protected_region" in code or "保护区" in msg:
        return "protected_region"
    if "path" in code or "寻路" in msg or "不可达" in msg:
        return "path_or_nav"
    if any(k in code for k in ("dead", "death", "hurt")) or any(k in msg for k in ("死亡", "抢占", "受击")):
        return "combat_or_death"
    if "timeout" in code or "超时" in msg:
        return "timeout"
    if any(k in code for k in ("missing", "no_recipe", "table")) or any(k in msg for k in ("缺", "没有", "不足")):
        return "missing_prereq"
    return "other_replan"


@dataclass(frozen=True)
class ObjectiveRecord:
    """单个历史目标的执行结果与失败分类摘要。"""

    goal_id: str
    outcome: str                  # "achieved" | "env_blocked" | "budget_exceeded" | "max_fails" | "respawn" | "switched" | "no_skill"
    failure_kind: str = "none"    # classify_failure_kind 输出
    elapsed_s: float = 0.0
    detail: str = ""

    def format_line(self) -> str:
        clean_detail = scrub_sensitive_text(self.detail)[:48]
        tail = f" ({clean_detail})" if clean_detail else ""
        return f"- {self.goal_id}: 结果={self.outcome} | 失败分类={self.failure_kind} | 用时={self.elapsed_s:.0f}s{tail}"


def summarize_inventory_and_status(state: State, *, max_items: int = 16) -> str:
    """生成不含坐标与实体 id 的生存及背包物资摘要。"""
    st = state.st or {}
    hp = st.get("health", 20)
    food = st.get("food", 20)
    dim = st.get("dimension", "overworld")
    hostile_obj = st.get("nearest_hostile")
    hostile_name = hostile_obj.get("name") if isinstance(hostile_obj, Mapping) else None
    threat = str(hostile_name) if hostile_name else "无"

    pick = best_pickaxe(state) or "无"
    axe = best_axe(state) or "无"
    edible = best_food(state) or "无"
    logs_n = total_logs(state)
    planks_n = total_planks(state)
    cobble_n = total_cobble(state)

    fp = _inv_fingerprint(state.inventory) or {}
    sorted_items = sorted(fp.items(), key=lambda kv: (-kv[1], kv[0]))
    if not sorted_items:
        bag_str = "空包"
    else:
        shown = [f"{name}x{cnt}" for name, cnt in sorted_items[:max_items]]
        if len(sorted_items) > max_items:
            shown.append(f"...共{len(sorted_items)}种")
        bag_str = ", ".join(shown)

    return (
        f"状态：维度={dim} | 血量={hp} | 饱食={food} | 安全模式={'是' if state.in_safe_mode else '否'} | 邻近威胁={threat}\n"
        f"关键装备与汇总：最优镐={pick} | 最优斧={axe} | 食物={edible} | 原木={logs_n} | 木板={planks_n} | 圆石={cobble_n}\n"
        f"背包明细：{bag_str}"
    )


def _format_optional_mapping(label: str, data: Any) -> str:
    if not data:
        return ""
    if isinstance(data, Mapping):
        parts = [f"{scrub_sensitive_text(str(k))}={scrub_sensitive_text(str(v))}" for k, v in data.items()]
    elif isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        parts = [scrub_sensitive_text(str(item)) for item in data]
    else:
        parts = [scrub_sensitive_text(str(data))]
    joined = "; ".join(p for p in parts if p)
    return f"{label}：{joined[:220]}" if joined else ""


@dataclass(frozen=True)
class DecisionContext:
    """组装完毕的战略层决策上下文（结构化字段 + 文本渲染）。"""

    remaining_match_s: float
    elapsed_match_s: float
    day_phase: str
    current_objective: Optional[Objective]
    current_objective_elapsed_s: float
    inventory_summary: str
    eligible_goals: tuple[ObjectiveCandidate, ...]
    suspended_goals: tuple[SuspendedGoalInfo, ...]
    recent_objectives: tuple[ObjectiveRecord, ...] = ()
    subgoal_predicates: Optional[Mapping[str, Any]] = None
    reservations: Optional[Any] = None
    extra_fields: Mapping[str, Any] = field(default_factory=dict)

    def render_menu(self) -> str:
        return render_objective_menu(self.eligible_goals, self.suspended_goals)

    def render(self, *, max_chars: int = MAX_CONTEXT_CHARS) -> str:
        if self.current_objective is None:
            cur_line = "当前Objective：无（首次决策）"
        else:
            cur = self.current_objective
            why_clean = scrub_sensitive_text(cur.why)[:40]
            why_part = f", 理由={why_clean}" if why_clean else ""
            cur_line = (
                f"当前Objective：{cur.goal_id}（来源={cur.source}, "
                f"已用={self.current_objective_elapsed_s:.0f}s/{cur.budget_s:.0f}s{why_part}）"
            )

        sections: list[str] = [
            f"赛况时间：剩余比赛时间={self.remaining_match_s:.0f}s（{self.remaining_match_s / 60.0:.1f}min, 已进行={self.elapsed_match_s:.0f}s） | 昼夜={self.day_phase}",
            cur_line,
            self.inventory_summary,
        ]

        sub_line = _format_optional_mapping("子目标谓词", self.subgoal_predicates)
        if sub_line:
            sections.append(sub_line)
        res_line = _format_optional_mapping("物资预留", self.reservations)
        if res_line:
            sections.append(res_line)
        if self.extra_fields:
            ext_line = _format_optional_mapping("附加观测", self.extra_fields)
            if ext_line:
                sections.append(ext_line)

        if self.recent_objectives:
            rec_lines = ["最近目标结果（最多 5 条）：", *(r.format_line() for r in self.recent_objectives[-5:])]
        else:
            rec_lines = ["最近目标结果：暂无历史目标记录"]
        sections.append("\n".join(rec_lines))

        menu_str = self.render_menu()
        full_text = "\n\n".join([*sections, menu_str])
        if len(full_text) <= max_chars:
            return full_text

        # 超长台账极端保护：优先裁剪可选目标列表描述尾部，确保始终 <= max_chars
        budget_for_menu = max(200, max_chars - len("\n\n".join(sections)) - 24)
        trimmed_menu = menu_str[:budget_for_menu].rsplit("\n", 1)[0] + "\n- ...(其余目标略)"
        compact = "\n\n".join([*sections, trimmed_menu])
        return compact[:max_chars]


def build_decision_context(
    state: State,
    *,
    ledger: Sequence[GoalSpec] = GOALS,
    registry: Optional[dict] = None,
    min_verified: str = "offline",
    include_stages: Optional[Collection[str]] = None,
    focus_goals: Any = None,
    completed_advs: Collection[str] = (),
    ep_goal_fails: Optional[Mapping[str, int]] = None,
    ep_goal_elapsed: Optional[Mapping[str, float]] = None,
    telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
    environment_blocks: Optional[Mapping[str, Any]] = None,
    current_objective: Optional[Objective] = None,
    current_objective_elapsed_s: float = 0.0,
    recent_objectives: Sequence[ObjectiveRecord] = (),
    elapsed_match_s: float = 0.0,
    max_match_seconds: float = 0.0,
    subgoal_predicates: Optional[Mapping[str, Any]] = None,
    reservations: Optional[Any] = None,
    extra_fields: Optional[Mapping[str, Any]] = None,
) -> DecisionContext:
    """由 harness 客观组装战略决策上下文（不含 current_goal_hint、科技硬编码、坐标与实体 id）。"""
    total_budget_s = float(max_match_seconds) if max_match_seconds > 0.0 else DEFAULT_MATCH_SECONDS
    elapsed_s = max(0.0, float(elapsed_match_s))
    remaining_s = max(0.0, total_budget_s - elapsed_s)

    eligible, suspended = collect_objective_candidates(
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
    )

    cleaned_suspended = tuple(
        SuspendedGoalInfo(
            goal_id=s.goal_id,
            title=s.title,
            score=s.score,
            wake_on=s.wake_on,
            missing_predicate=scrub_sensitive_text(s.missing_predicate),
            message=scrub_sensitive_text(s.message),
        )
        for s in suspended
    )

    return DecisionContext(
        remaining_match_s=round(remaining_s, 1),
        elapsed_match_s=round(elapsed_s, 1),
        day_phase=describe_day_phase(state),
        current_objective=current_objective,
        current_objective_elapsed_s=round(max(0.0, float(current_objective_elapsed_s)), 1),
        inventory_summary=summarize_inventory_and_status(state),
        eligible_goals=tuple(eligible),
        suspended_goals=cleaned_suspended,
        recent_objectives=tuple(recent_objectives[-5:]),
        subgoal_predicates=dict(subgoal_predicates) if subgoal_predicates is not None else None,
        reservations=reservations,
        extra_fields=dict(extra_fields or {}),
    )
