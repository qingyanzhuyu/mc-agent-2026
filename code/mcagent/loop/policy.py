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
from typing import Callable, Optional

from ..planner import skill_choice
from ..planner.llm import LLMClient, LLMError
from ..runtime import State
from ..skills import REGISTRY, Skill, make
from ..skills.items import best_pickaxe
from .starter_skills import (
    PROTECTED_ZONES,
    can_afford_gear,
    has_known_table,
    total_cobble,
    total_logs,
    total_planks,
)

STONE_OR_BETTER_PICKS = frozenset({
    "stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe",
})


@dataclass
class PlanDecision:
    skill: Optional[Skill]
    source: str          # "rule" | "llm" | "llm_fallback_rule" | "focus" | "idle"
    reason: str
    llm_ms: Optional[float] = None
    llm_attempts: int = 0
    llm_errors: tuple[str, ...] = ()


def build_state_summary(state: State) -> str:
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
    return "\n".join(lines)


class RulePlanner:
    """无状态科技树派生器：只看当拍 `State` 决定下一步做什么。"""

    def __init__(self, registry: Optional[dict[str, type[Skill]]] = None):
        self.registry = REGISTRY if registry is None else registry

    def _try_make(self, name: str, args: Optional[dict], state: State) -> Optional[Skill]:
        cls = self.registry.get(name)
        if cls is None or cls.blocked(state):
            return None
        inst = cls(**(args or {}))
        if inst.is_cooling() or not inst.can_start(state):
            return None
        return inst

    def choose(self, state: State) -> PlanDecision:
        hp = float(state.st.get("health", 20))
        food = float(state.st.get("food", 20))

        # 1) 生存优先：饥饿或掉血时优先进食
        if food <= 14 or (hp < 18 and food < 20):
            eat = self._try_make("eat_food", {}, state)
            if eat is not None:
                return PlanDecision(eat, "rule", f"生存补给 hp={hp} food={food}")

        # safe_mode 锁存期不主动发起高风险远征，只允许进食或原地轻度合成
        if state.in_safe_mode:
            for item in ("stone_sword", "stone_pickaxe", "wooden_pickaxe"):
                if state.count(item) < 1 and can_afford_gear(state, item, assume_no_table=True):
                    c = self._try_make("craft_gear", {"item": item}, state)
                    if c is not None:
                        return PlanDecision(c, "rule", f"safe_mode 期间原地合成 {item}")
            return PlanDecision(None, "idle", f"safe_mode 避险驻留中（剩 {state.safe_mode_remaining:.0f}s）")

        pick = best_pickaxe(state)
        assume_no_tbl = not has_known_table(state.pos)

        # 2) 阶段一：无任何镐 -> 砍树 -> 合成木镐
        if pick is None:
            if can_afford_gear(state, "wooden_pickaxe", assume_no_table=assume_no_tbl):
                s = self._try_make("craft_gear", {"item": "wooden_pickaxe"}, state)
                if s is not None:
                    return PlanDecision(s, "rule", "材料齐全，合成木镐")
            w = self._try_make("gather_wood", {"count": 3}, state)
            if w is not None:
                return PlanDecision(w, "rule", "尚无镐，先砍 3 块原木")
            sc = self._try_make("scout_island", {"radius": 32}, state)
            if sc is not None:
                return PlanDecision(sc, "rule", "附近无可用树木或受保护区阻挡，外迁探索")
            return PlanDecision(None, "idle", "无镐阶段所有候选技能均在冷却")

        # 3) 阶段二：仅有木镐 -> 挖圆石 -> 升级石镐（解锁 mine_stone + upgrade_tools）
        if pick not in STONE_OR_BETTER_PICKS:
            if can_afford_gear(state, "stone_pickaxe", assume_no_table=assume_no_tbl):
                sp = self._try_make("craft_gear", {"item": "stone_pickaxe"}, state)
                if sp is not None:
                    return PlanDecision(sp, "rule", "圆石与木料充足，升级石镐")
            if total_cobble(state) < 3:
                ms = self._try_make("mine_stone", {"count": 8}, state)
                if ms is not None:
                    return PlanDecision(ms, "rule", "用木镐采集首批圆石")
            else:
                gw = self._try_make("gather_wood", {"count": 2}, state)
                if gw is not None:
                    return PlanDecision(gw, "rule", "补原木用于石镐木棍与工作台")
            sc = self._try_make("scout_island", {"radius": 32}, state)
            if sc is not None:
                return PlanDecision(sc, "rule", "石器前置受阻，探索新区域")
            return PlanDecision(None, "idle", "木镐升石镐阶段技能冷却中")

        # 4) 阶段三：已有石镐+ -> 补石剑防身 + 熔炉（铁器前置）+ 维持基础物资与探索
        if state.count("stone_sword") < 1 and state.count("iron_sword") < 1:
            if can_afford_gear(state, "stone_sword", assume_no_table=assume_no_tbl):
                sw = self._try_make("craft_gear", {"item": "stone_sword"}, state)
                if sw is not None:
                    return PlanDecision(sw, "rule", "合成石剑保障夜间自卫")
            if total_cobble(state) < 2:
                ms = self._try_make("mine_stone", {"count": 6}, state)
                if ms is not None:
                    return PlanDecision(ms, "rule", "补圆石合成石剑")
            else:
                gw = self._try_make("gather_wood", {"count": 2}, state)
                if gw is not None:
                    return PlanDecision(gw, "rule", "补原木合成石剑")

        if state.count("furnace") < 1:
            if can_afford_gear(state, "furnace", assume_no_table=assume_no_tbl):
                fn = self._try_make("craft_gear", {"item": "furnace"}, state)
                if fn is not None:
                    return PlanDecision(fn, "rule", "合成熔炉备烧铁与熟食")
            if total_cobble(state) < 8:
                ms = self._try_make("mine_stone", {"count": 8}, state)
                if ms is not None:
                    return PlanDecision(ms, "rule", "攒够 8 圆石合成熔炉")
            else:
                gw = self._try_make("gather_wood", {"count": 2}, state)
                if gw is not None:
                    return PlanDecision(gw, "rule", "补原木以放置工作台合熔炉")

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
        self.rules = RulePlanner(self.registry)
        self.on_llm_telemetry = on_llm_telemetry

    def select(self, state: State, recent_failures: list[str]) -> PlanDecision:
        if self.mode == "focus":
            assert self.focus_skill is not None
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
            return self.rules.choose(state)

        summary = build_state_summary(state)
        fail_str = "\n".join(f"- {x}" for x in recent_failures[-5:])
        t0 = time.monotonic()
        errors: list[str] = []
        used = 0
        chosen: Optional[Skill] = None
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
            )
        except LLMError as e:
            errors = [f"LLMError: {e}"]
            used = max(1, used)
        dt_ms = round((time.monotonic() - t0) * 1000.0, 1)

        if self.on_llm_telemetry is not None:
            self.on_llm_telemetry({
                "ms": dt_ms,
                "ok": chosen is not None,
                "skill": chosen.name if chosen else None,
                "args": chosen.args if chosen else None,
                "attempts": used,
                "errors": list(errors),
            })

        if chosen is not None:
            return PlanDecision(chosen, "llm", f"LLM 选择 {chosen}", dt_ms, used, tuple(errors))

        fallback = self.rules.choose(state)
        return PlanDecision(
            fallback.skill,
            "llm_fallback_rule" if fallback.skill else "idle",
            f"LLM 未产出合法选择（{'; '.join(errors[:2])}）-> 规则兜底: {fallback.reason}",
            dt_ms,
            used,
            tuple(errors),
        )
