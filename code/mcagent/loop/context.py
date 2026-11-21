"""战略层决策上下文组装（由 harness 客观投影世界状态，不透传规则答案）。

硬约束（见 P9 契约与 J11 / J17）：
1. 必须包含：有资格且未达成目标（id、标题、分值、预计剩余成本、前置是否满足）、被挂起目标及唤醒条件、
   背包摘要、剩余比赛时间与昼夜、最近 5 个目标的结果与失败分类、当前 Objective 及已用时间。
2. 严禁包含：`RulePlanner.current_goal_hint()`、写死的科技顺序、任何三维/区块坐标、实体 id。
3. 支持 `subgoal_predicates` 和 `reservations` 摘要，菜单使用调度器的物资预留条件。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Collection, Mapping, Optional, Sequence

from ..planner.objective import (
    Objective,
    ObjectiveCandidate,
    SuspendedGoalInfo,
    collect_objective_candidates,
    effective_include_stages,
    format_objective_candidate_line,
    render_objective_menu,
    render_suspended_section,
)
from ..runtime import State, _inv_fingerprint, has_equipped_armor
from ..skills.items import best_pickaxe
from ..skills.survival_food import inventory_food_restore
from ..skills.world_memo import memo_for
from .goals import (
    GOALS,
    GoalSpec,
    goal_landmarks_satisfied,
    is_goal_achieved,
    is_prereq_satisfied,
    resolve_active_goals,
)
from .starter_skills import best_axe, best_food, total_cobble, total_logs, total_planks
from .subgoals import evaluate_all_subgoals

if TYPE_CHECKING:
    from .reserve import ReservationBook

DEFAULT_MATCH_SECONDS: float = 8 * 3600.0
MAX_CONTEXT_CHARS: int = 2000

_PENDING_STUB = "待现场观察"

_STATION_LABELS: dict[str, str] = {
    "crafting_table": "工作台",
    "furnace": "熔炉",
    "blast_furnace": "高炉",
    "smoker": "烟熏炉",
    "bed": "床",
    "enchanting_table": "附魔台",
    "nether_portal": "传送门",
    "anvil": "铁砧",
}
_LANDMARK_LABELS: dict[str, str] = {
    "village": "村庄",
    "ruined_portal": "废弃传送门",
    "stronghold": "要塞",
    "bee_nest": "蜂巢",
    "lava_pool": "岩浆池",
    "water_source": "水源",
    "cave_iron": "铁矿洞",
    "fortress": "下界要塞",
    "trial_chambers": "试炼密室",
    "deep_dark": "深暗之域",
    "archaeology": "遗迹",
    "powder_snow": "细雪",
    "pillager_outpost": "前哨站",
    "nether_fortress": "下界要塞",
    "bastion_remnant": "堡垒遗迹",
    "end_city": "末地城",
    "lush_caves": "繁茂洞穴",
    "amethyst_geode": "紫水晶晶洞",
    "pale_garden": "苍白之园",
    "meadow": "草甸",
    "swamp": "沼泽",
    "mangrove_swamp": "红树林沼泽",
    "dried_ghast": "失水恶魂",
    "cold_biome": "寒带群系",
    "trial_vault_ominous": "不祥宝库",
}
_RESOURCE_LABELS: dict[str, str] = {
    "wood": "原木",
    "water": "水源",
    "lava": "熔岩",
    "coal_ore": "煤矿",
    "iron_ore": "铁矿",
    "diamond_ore": "钻石矿",
    "obsidian": "黑曜石",
    "station": "工作站",
}
_SUBGOAL_LANDMARK_LABELS: dict[str, str] = {
    **_LANDMARK_LABELS,
    **_RESOURCE_LABELS,
    "iron": "铁矿",
    "diamond": "钻石",
    "any": "任意",
}

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


_ARMOR_BAG_ITEMS: tuple[str, ...] = (
    "iron_helmet", "iron_chestplate", "iron_leggings", "iron_boots",
    "diamond_helmet", "diamond_chestplate", "diamond_leggings", "diamond_boots",
    "netherite_helmet", "netherite_chestplate", "netherite_leggings", "netherite_boots",
    "leather_helmet", "leather_chestplate", "leather_leggings", "leather_boots",
    "golden_helmet", "golden_chestplate", "golden_leggings", "golden_boots",
    "chainmail_helmet", "chainmail_chestplate", "chainmail_leggings", "chainmail_boots",
    "turtle_helmet",
)


def _summarize_armor(state: State) -> str:
    """生成紧凑的护甲与盾牌佩戴/背包状态摘要。"""
    equipped = has_equipped_armor(state)
    bag_armor = any(state.count(item) >= 1 for item in _ARMOR_BAG_ITEMS)
    has_shield = state.count("shield") >= 1
    if equipped:
        return "已穿戴+盾" if has_shield else "已穿戴"
    if bag_armor:
        return "背包有甲+盾" if has_shield else "背包有甲"
    return "仅盾" if has_shield else "无"


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
    armor_str = _summarize_armor(state)
    edible = best_food(state) or "无"
    food_restore = inventory_food_restore(state)
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
        f"关键装备与汇总：最优镐={pick} | 最优斧={axe} | 护甲={armor_str} | 食物={edible} | 食物量={food_restore} | 原木={logs_n} | 木板={planks_n} | 圆石={cobble_n}\n"
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


def _format_nested_subgoal(key: str, sub: Mapping[str, Any]) -> str:
    if key == "station_ready":
        active = [_STATION_LABELS.get(str(sk), scrub_sensitive_text(str(sk))) for sk, flag in sub.items() if flag]
        return ",".join(a for a in active if a) or "无"
    if key == "landmark_reachable":
        specific = [sk for sk, flag in sub.items() if sk != "any" and flag]
        keys = specific if specific else (["any"] if sub.get("any") else [])
        active = [_SUBGOAL_LANDMARK_LABELS.get(str(sk), scrub_sensitive_text(str(sk))) for sk in keys]
        return ",".join(a for a in active if a) or "无"
    if all(isinstance(sv, bool) for sv in sub.values()):
        active = [scrub_sensitive_text(str(sk)) for sk, flag in sub.items() if flag]
        return ",".join(a for a in active if a) or "无"
    pairs = [f"{scrub_sensitive_text(str(sk))}:{scrub_sensitive_text(str(sv))}" for sk, sv in sub.items()]
    return ",".join(p for p in pairs if p) or "无"


def format_subgoal_summary(predicates: Optional[Mapping[str, Any]]) -> str:
    """将结构化或平铺的子目标谓词映射格式化为紧凑、脱敏的摘要文本（不含前缀）。"""
    if not predicates:
        return ""
    parts: list[str] = []
    for raw_k, raw_v in predicates.items():
        if raw_v == _PENDING_STUB:
            continue
        k = scrub_sensitive_text(str(raw_k))
        if not k:
            continue
        if isinstance(raw_v, Mapping):
            val_str = _format_nested_subgoal(str(raw_k), raw_v)
        elif str(raw_k) in ("safe_encounter", "active_portal") and isinstance(raw_v, bool):
            val_str = "是" if raw_v else "否"
        else:
            val_str = scrub_sensitive_text(str(raw_v))
        parts.append(f"{k}={val_str}")
    joined = scrub_sensitive_text("; ".join(parts))
    return joined if len(joined) <= 260 else joined[:257] + "..."


def count_pending_landmark_goals(
    active_goals: Sequence[GoalSpec],
    state: State,
    completed_advs: Collection[str] = (),
    memo: Any = None,
) -> int:
    """统计激活目标中（自身或未满足前置链）仍缺地标/群系先验的目标数量（零坐标）。"""
    resolved_memo = memo if memo is not None else memo_for(state)
    by_id = {g.id: g for g in active_goals}
    pending = 0
    for g in active_goals:
        if is_goal_achieved(g, state, completed_advs, memo=resolved_memo):
            continue
        seen: set[str] = set()
        stack: list[tuple[GoalSpec, bool]] = [(g, True)]
        needs_landmark = False
        while stack:
            node, is_root = stack.pop()
            if node.id in seen:
                continue
            seen.add(node.id)
            if not is_root and is_prereq_satisfied(node, state, completed_advs, memo=resolved_memo):
                continue
            if not goal_landmarks_satisfied(node, resolved_memo):
                needs_landmark = True
                break
            for pid in node.prereq_goals:
                parent = by_id.get(pid)
                if parent is not None and parent.id not in seen:
                    stack.append((parent, False))
        if needs_landmark:
            pending += 1
    return pending


def format_world_landmarks(world_landmarks: Optional[Mapping[str, Any]]) -> str:
    """将世界资源与地标摘要格式化为脱敏中文单行（空世界返回空串，长度 <= 200 字符）。"""
    if not world_landmarks or not isinstance(world_landmarks, Mapping):
        return ""
    portal_active = bool(world_landmarks.get("active_portal"))
    stations = dict.fromkeys(str(s) for s in (world_landmarks.get("stations_ready") or ()) if str(s).strip())
    landmarks = dict.fromkeys(str(lm) for lm in (world_landmarks.get("landmarks_discovered") or ()) if str(lm).strip())
    resource_counts = world_landmarks.get("resource_counts")

    parts: list[str] = []
    for station in stations:
        if portal_active and station == "nether_portal":
            continue
        label = _STATION_LABELS.get(station, scrub_sensitive_text(station))
        if label:
            parts.append(f"{label}=已就绪")
    for landmark in landmarks:
        label = _LANDMARK_LABELS.get(landmark, scrub_sensitive_text(landmark))
        if label:
            parts.append(f"{label}=已发现")
    if portal_active:
        parts.append("传送门=已激活")
    if world_landmarks.get("has_camp"):
        raw_stored = world_landmarks.get("camp_stored_count", 0)
        stored_cnt = (
            int(raw_stored)
            if isinstance(raw_stored, (int, float)) and not isinstance(raw_stored, bool)
            else 0
        )
        if stored_cnt > 0:
            parts.append(f"营地=已建(存箱={stored_cnt}件)")
        else:
            parts.append("营地=已建")
    if isinstance(resource_counts, Mapping):
        res_parts: list[str] = []
        for res, cnt in resource_counts.items():
            if isinstance(cnt, (int, float)) and not isinstance(cnt, bool) and int(cnt) > 0:
                res_label = _RESOURCE_LABELS.get(str(res), scrub_sensitive_text(str(res)))
                if res_label:
                    res_parts.append(f"{res_label}={int(cnt)}")
        if res_parts:
            parts.append(f"资源({', '.join(res_parts)})")

    raw_pending = world_landmarks.get("pending_landmark_goals", 0)
    if isinstance(raw_pending, int) and not isinstance(raw_pending, bool) and raw_pending > 0:
        parts.append(f"待勘探目标={raw_pending}项")

    if not parts:
        return ""
    line = scrub_sensitive_text(f"已知设施与地标：{'; '.join(parts)}")
    return line if len(line) <= 200 else line[:197] + "..."


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
    world_landmarks: Optional[Mapping[str, Any]] = None
    subgoal_summary: str = ""

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

        if self.subgoal_predicates is not None:
            sub_body = format_subgoal_summary(self.subgoal_predicates)
        elif self.subgoal_summary:
            sub_body = scrub_sensitive_text(self.subgoal_summary)
        else:
            sub_body = ""
        if sub_body and sub_body != _PENDING_STUB:
            sub_line = sub_body if sub_body.startswith("子目标谓词：") else f"子目标谓词：{sub_body}"
            sections.append(sub_line)

        landmarks_line = format_world_landmarks(self.world_landmarks)
        if landmarks_line:
            sections.append(landmarks_line)

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
        prefix = "\n\n".join(sections)
        full_text = f"{prefix}\n\n{menu_str}"
        if len(full_text) <= max_chars:
            return full_text

        # 超长台账保护：按边际 ROI 截取可选目标 Top-K，同时保留被挂起目标与唤醒条件，严格保证 <= max_chars
        susp_block = render_suspended_section(self.suspended_goals, max_items=4)
        if len(susp_block) > 360:
            susp_block = render_suspended_section(self.suspended_goals, max_items=2)
        if len(susp_block) > 360:
            susp_block = susp_block[:357] + "..."

        cur_gid = self.current_objective.goal_id if self.current_objective is not None else None

        def _roi_key(pair: tuple[int, ObjectiveCandidate]) -> tuple[int, int, float, int, int, float, int]:
            idx, c = pair
            m_roi = (
                float(c.marginal_roi)
                if c.marginal_roi is not None
                else (float(c.score) / max(1.0, float(c.est_remaining_cost_s)))
            )
            return (
                1 if (cur_gid is not None and c.goal_id == cur_gid) else 0,
                1 if c.score > 0 else 0,
                round(m_roi, 6),
                1 if c.prereqs_met else 0,
                int(c.score),
                -float(c.est_remaining_cost_s),
                -idx,
            )

        ordered = [c for _, c in sorted(enumerate(self.eligible_goals), key=_roi_key, reverse=True)]
        header = "可选目标（只能从这里选 goal_id）："
        total_elig = len(ordered)
        selected_lines: list[str] = []
        # 预留 footer 与 suspended_block 字符预算
        footer_reserve = len(f"\n- ...(共 {total_elig} 项可选，按边际 ROI 展示 Top-{total_elig})")
        fixed_len = len(prefix) + 2 + len(header) + 1 + len(susp_block) + footer_reserve
        avail = max(0, max_chars - fixed_len)
        used = 0
        for item in ordered:
            line = format_objective_candidate_line(item)
            add_len = len(line) + 1
            if used + add_len > avail:
                break
            selected_lines.append(line)
            used += add_len

        menu_lines = [header]
        if selected_lines:
            menu_lines.extend(selected_lines)
            if len(selected_lines) < total_elig:
                menu_lines.append(f"- ...(共 {total_elig} 项可选，按边际 ROI 展示 Top-{len(selected_lines)})")
        elif total_elig > 0:
            menu_lines.append(f"- ...(共 {total_elig} 项可选，预算受限已折叠)")
        else:
            menu_lines.append("- （无可选目标）")
        menu_lines.append(susp_block)
        compact = f"{prefix}\n\n" + "\n".join(menu_lines)
        return compact if len(compact) <= max_chars else compact[:max_chars]


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
    reservation_book: Optional[ReservationBook] = None,
    extra_fields: Optional[Mapping[str, Any]] = None,
    world_landmarks: Optional[Mapping[str, Any]] = None,
    hub_suspensions: Optional[Mapping[str, Any]] = None,
    abandoned_goals: Collection[str] = (),
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
        reservation_book=reservation_book,
        hub_suspensions=hub_suspensions,
        abandoned_goals=abandoned_goals,
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

    memo = memo_for(state)
    dim = str((state.st or {}).get("dimension") or "overworld")
    live_subgoals = evaluate_all_subgoals(state, memo=memo, reservations=reservation_book)

    if subgoal_predicates is None:
        resolved_subgoals: dict[str, Any] = dict(live_subgoals)
    else:
        resolved_subgoals = {
            k: (live_subgoals[k] if v == _PENDING_STUB and k in live_subgoals else v)
            for k, v in subgoal_predicates.items()
            if not (v == _PENDING_STUB and k not in live_subgoals)
        }

    resolved_landmarks: dict[str, Any] = (
        dict(world_landmarks)
        if world_landmarks is not None
        else (dict(memo.summary_for_context(dimension=dim)) if hasattr(memo, "summary_for_context") else {})
    )
    if world_landmarks is None and not focus_goals:
        eff_stages = effective_include_stages(min_verified, include_stages)
        active_pool = resolve_active_goals(tuple(ledger), include_stages=eff_stages)
        pending_cnt = count_pending_landmark_goals(active_pool, state, completed_advs, memo)
        if pending_cnt > 0:
            resolved_landmarks["pending_landmark_goals"] = pending_cnt
    subgoal_summary_str = format_subgoal_summary(resolved_subgoals)

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
        subgoal_predicates=resolved_subgoals,
        reservations=reservations,
        extra_fields=dict(extra_fields or {}),
        world_landmarks=resolved_landmarks,
        subgoal_summary=subgoal_summary_str,
    )
