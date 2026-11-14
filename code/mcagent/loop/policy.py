"""规划层：按当前真实背包与环境状态派生下一技能（规则队列默认 + 可选 LLM 菜单选择）。

关键边界（评审修订 #1 & #2）：
1. 重生后处于哪个科技阶段由本模块从 `State` 实时推导，不放在主循环/Supervisor 中硬记；
   无论服务端 `keepInventory` 开或关，背包一刷新即自动衔接正确阶段。
2. 仅保留两种模式：`campaign`（正式比赛跑法）与 `focus`（单技能定向回归）。
3. 默认纯规则队列（零模型开销）；开启 `--llm` 时记录模型延迟与非法输出并自动兜底回规则队列。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Collection, Mapping, Optional

from ..planner import skill_choice
from ..planner.llm import LLMClient, LLMError
from ..runtime import State
from ..skills import REGISTRY, Skill, make
from ..skills.items import best_pickaxe
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
    rank_actionable_goals,
    resolve_active_goals,
)
from .starter_skills import (
    PROTECTED_ZONES,
    can_afford_gear,
    total_cobble,
    total_logs,
    total_planks,
)


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


def build_state_summary(state: State, *, goal_hint: str = "") -> str:
    """给模型的一屏状态摘要（不暴露实体 id 与可编造坐标列表，遵守 J17）。"""
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
    """基于目标台账（`goals.py`）与当拍真实 `State` 的科技树性价比派生器。"""

    def __init__(
        self,
        registry: Optional[dict[str, type[Skill]]] = None,
        ledger: tuple[GoalSpec, ...] = GOALS,
        include_stages: Collection[str] = DEFAULT_CAMPAIGN_STAGES,
        target_goal: Optional[str] = None,
        telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ):
        self.registry = REGISTRY if registry is None else registry
        self.ledger: tuple[GoalSpec, ...] = tuple(ledger)
        self.ledger_map: dict[str, GoalSpec] = {g.id: g for g in self.ledger}
        self.include_stages: tuple[str, ...] = tuple(include_stages)
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

    def _try_make(self, name: str, args: Optional[dict], state: State) -> Optional[Skill]:
        cls = self.registry.get(name)
        if cls is None or cls.blocked(state):
            return None
        inst = cls(**(args or {}))
        if inst.is_cooling() or not inst.can_start(state):
            return None
        return inst

    def _rank(self, state: State):
        return rank_actionable_goals(
            state,
            self.active_goals,
            completed_advs=self.completed_advancements,
            target_goal=self.target_goal,
            ep_goal_fails=self.ep_goal_fails,
            ep_goal_elapsed=self.ep_goal_elapsed,
            telemetry=self.telemetry,
        )

    def current_goal_hint(self, state: State) -> str:
        """返回当前最高性价比目标步的一行指引与缺口诊断（无待办目标时返回空串）。"""
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
        hp = float(state.st.get("health", 20))
        food = float(state.st.get("food", 20))

        # 1) 生存优先：饥饿或掉血时优先进食
        if food <= 14 or (hp < 18 and food < 20):
            eat = self._try_make("eat_food", {}, state)
            if eat is not None:
                return PlanDecision(eat, "rule", f"生存补给 hp={hp} food={food}")

        # 2) safe_mode 锁存期不主动发起高风险远征，只允许进食或原地轻度合成
        if state.in_safe_mode:
            for item in ("stone_sword", "stone_pickaxe", "wooden_pickaxe"):
                if state.count(item) < 1 and can_afford_gear(state, item, assume_no_table=True):
                    c = self._try_make("craft_gear", {"item": item}, state)
                    if c is not None:
                        return PlanDecision(c, "rule", f"safe_mode 期间原地合成 {item}")
            return PlanDecision(None, "idle", f"safe_mode 避险驻留中（剩 {state.safe_mode_remaining:.0f}s）")

        # 3) 目标台账解析与按性价比顺序派发
        ranked = self._rank(state)
        if ranked:
            for step in ranked:
                g = step.step_goal
                if all(check_item_expr(state, expr) for expr in g.prereq_items):
                    inst = self._try_make(g.skill, g.args_dict, state)
                    if inst is not None:
                        self._last_selected_goal_id = g.id
                        return PlanDecision(inst, "rule", f"推进目标 {g.id}（{g.title}）")
                else:
                    # 仅尝试首个未满足前置表达式对应的补料技能（避免缺圆石且采石冷却时盲目去砍树）
                    for req_expr, fb_skill, fb_args in g.gather_fallbacks:
                        if not check_item_expr(state, req_expr):
                            inst = self._try_make(fb_skill, dict(fb_args), state)
                            if inst is not None:
                                self._last_selected_goal_id = g.id
                                return PlanDecision(inst, "rule", f"为目标 {g.id} 补齐前置材料（{req_expr}）")
                            break

            sc_radius = 32 if best_pickaxe(state) not in STONE_OR_BETTER_PICKS else 36
            sc = self._try_make("scout_island", {"radius": sc_radius}, state)
            if sc is not None:
                return PlanDecision(sc, "rule", f"目标 {ranked[0].step_goal.id} 当前位置受阻或冷却，外迁探索")
            return PlanDecision(None, "idle", f"目标 {ranked[0].step_goal.id} 所有候选技能均在冷却")

        if self.target_goal:
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
        return PlanDecision(None, "idle", "候选技能全部冷却中")


class CampaignPlanner:
    """统一规划入口：支持 `campaign`（默认规则 / `--llm` 混合）与 `focus` 两种模式。"""

    def __init__(
        self,
        *,
        mode: str = "campaign",
        focus_skill: Optional[str] = None,
        focus_args: Optional[dict] = None,
        use_llm: bool = False,
        llm: Optional[LLMClient] = None,
        min_verified: str = "offline",
        registry: Optional[dict[str, type[Skill]]] = None,
        on_llm_telemetry: Optional[Callable[[dict], None]] = None,
        target_goal: Optional[str] = None,
        ledger: tuple[GoalSpec, ...] = GOALS,
        telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ):
        if mode not in ("campaign", "focus"):
            raise ValueError(f"mode 只能是 'campaign' 或 'focus'；收到 {mode!r}")
        if mode == "focus" and not focus_skill:
            raise ValueError("focus 模式必须指定 focus_skill")
        self.mode = mode
        self.focus_skill = focus_skill
        self.focus_args = dict(focus_args or {})
        self.use_llm = use_llm
        self.llm = llm or (LLMClient() if use_llm else None)
        self.min_verified = min_verified
        self.registry = REGISTRY if registry is None else registry
        self.target_goal = target_goal
        include_stages = OFFICIAL_CAMPAIGN_STAGES if min_verified == "live" else DEFAULT_CAMPAIGN_STAGES
        self.rules = RulePlanner(
            self.registry,
            ledger=ledger,
            include_stages=include_stages,
            target_goal=target_goal,
            telemetry=telemetry,
        )
        self.on_llm_telemetry = on_llm_telemetry
        self._last_selected_goal_id: Optional[str] = None

    def record_skill_outcome(
        self,
        skill_name: str,
        *,
        ok: bool,
        elapsed_s: float = 0.0,
        completed_advs: Collection[str] = (),
    ) -> None:
        if self._last_selected_goal_id and self.rules._last_selected_goal_id is None:
            self.rules._last_selected_goal_id = self._last_selected_goal_id
        self.rules.record_skill_outcome(
            skill_name,
            ok=ok,
            elapsed_s=elapsed_s,
            completed_advs=completed_advs,
        )
        self._last_selected_goal_id = None

    def select(self, state: State, recent_failures: list[str]) -> PlanDecision:
        if self.mode == "focus":
            assert self.focus_skill is not None
            self._last_selected_goal_id = self.target_goal if (self.target_goal in self.rules.ledger_map) else None
            if state.in_safe_mode:
                return PlanDecision(None, "idle", f"safe_mode 冷却中（{state.safe_mode_remaining:.0f}s）")
            skill = make(self.focus_skill, self.focus_args, registry=self.registry)
            reason = type(skill).blocked(state)
            if reason:
                return PlanDecision(None, "idle", f"focus 技能前置未满足: {reason}")
            if skill.is_cooling():
                return PlanDecision(None, "idle", f"focus 目标 {skill.goal} 冷却中")
            if not skill.can_start(state):
                return PlanDecision(None, "idle", f"focus 技能参数 {self.focus_args} 当前不可启动")
            return PlanDecision(skill, "focus", f"定向验收 {skill}")

        if not self.use_llm or self.llm is None or state.in_safe_mode:
            dec = self.rules.choose(state)
            self._last_selected_goal_id = self.rules._last_selected_goal_id
            return dec
        if not skill_choice.has_ready_skill(state, self.registry, self.min_verified):
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
                registry=self.registry,
                min_verified=self.min_verified,
                attempts=2,
                thinking=False,
                total_timeout=state.config.model_timeout_s * 2,
                usage_out=usage,
            )
        except LLMError as e:
            errors = [f"LLMError: {e}"]
            used = max(1, used)
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
            for step in self.rules._rank(state):
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
        )
