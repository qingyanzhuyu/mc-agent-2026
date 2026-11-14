"""目标台账与性价比依赖解析器（对应 docs/09 判据表与 docs/11 任务性价比分层）。

核心职责（遵循 AGENTS.md §9.3 一文件一职责）：
1. 用纯数据 `GoalSpec` 声明每个阶段目标的分值、前置依赖、负责技能、完成判据（服务端进度 + 背包代理双轨）、
   单轮止损线（`budget_s` / `max_fails_per_ep`）、晋级阶段（`draft` -> `candidate` -> `graduated` -> `live`）
   及自动修单收窄白名单（`editable_files`）。
2. 提供无副作用的状态评估与链路性价比（`path_score / path_cost`）排序函数，让 `RulePlanner` 与
   `CampaignPlanner` 无需手写冗长 `if-else` 即可按实测/估算成本自动推进，并严格兼容 `keepInventory` 开/关。
3. 严守单候选目标纪律（同一时刻最多 1 个 `stage == "candidate"`），确保挂机优化的分数与耗时变化可精确归因。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Mapping, Optional

from ..runtime import State
from ..skills import REGISTRY, bind_args
from ..skills.items import best_pickaxe
from .starter_skills import (
    can_afford_gear,
    has_known_table,
    total_cobble,
    total_logs,
    total_planks,
)

GOAL_STAGES: tuple[str, ...] = ("draft", "candidate", "graduated", "live")
DEFAULT_CAMPAIGN_STAGES: tuple[str, ...] = ("candidate", "graduated", "live")
OFFICIAL_CAMPAIGN_STAGES: tuple[str, ...] = ("graduated", "live")
GOAL_TIERS: tuple[str, ...] = ("T0_PREP", "T1", "T2", "T3")

STONE_OR_BETTER_PICKS: frozenset[str] = frozenset({
    "stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe",
})
IRON_OR_BETTER_PICKS: frozenset[str] = frozenset({
    "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe",
})
DIAMOND_OR_BETTER_PICKS: frozenset[str] = frozenset({
    "diamond_pickaxe", "netherite_pickaxe",
})


@dataclass(frozen=True)
class GoalSpec:
    """目标台账单行定义。"""

    id: str
    title: str
    tier: str                                  # "T0_PREP" | "T1" | "T2" | "T3"
    score: int                                 # RULES v2 分值（前置准备项允许为 0）
    skill: str                                 # 达成该目标的主技能名
    skill_args: tuple[tuple[str, Any], ...] = ()
    prereq_goals: tuple[str, ...] = ()         # 前置目标 id
    prereq_items: tuple[str, ...] = ()         # 启动主技能前需满足的物资表达式
    done_advancements: tuple[str, ...] = ()    # 服务端确认达成 advancement id
    done_items: tuple[str, ...] = ()           # 背包代理/实物持有判据表达式
    must_hold_for_children: bool = False       # 作为下游前置时，当前背包是否必须实际持有 done_items
    gather_fallbacks: tuple[tuple[str, str, tuple[tuple[str, Any], ...]], ...] = ()
    budget_s: float = 180.0                    # 单轮时间预算参考/止损上限（秒）
    max_fails_per_ep: int = 0                  # 单轮最大允许失败次数（0=不限，靠技能指数冷却退避）
    stop_loss: bool = False                    # 是否启用硬时间预算止损（T2/T3 或高风险支线开启）
    est_cost_s: float = 30.0                   # 尚无实测遥测时的静态耗时初值（秒）
    stage: str = "draft"                       # "draft" | "candidate" | "graduated" | "live"
    editable_files: tuple[str, ...] = ()       # GOAL_MISS 自动修复时允许改动的收窄白名单

    @property
    def args_dict(self) -> dict[str, Any]:
        return dict(self.skill_args)


@dataclass(frozen=True)
class GoalPlanStep:
    """经链路性价比排序后的可执行目标步。"""

    step_goal: GoalSpec                        # 当前应当立即执行的最近未满足目标（前置或根目标本身）
    root_goal: GoalSpec                        # 驱动该前置链路的顶层收益目标
    chain: tuple[GoalSpec, ...]                # 从 step_goal 到 root_goal 的未满足链路
    path_score: int                            # 整条未完成链路合计可解锁分值
    path_cost: float                           # 整条未完成链路预计/实测耗时成本（秒）
    roi: float                                 # path_score / path_cost


# ============================================================================
# 静态目标台账（按 docs/10 推进批次与 docs/11 T1 依赖链排列）
# - 已跑通的基础木石链标为 graduated；
# - 待逐个接入的新技能目标标为 draft，接入时每次仅将 1 个目标改为 candidate。
# ============================================================================

_STARTER_EDITABLE: tuple[str, ...] = (
    "code/mcagent/loop/starter_skills.py",
    "code/tests/test_loop_soak.py",
)

GOALS: tuple[GoalSpec, ...] = (
    # ---- 批次 0：起步木石与冶炼底座（已集成并离线+联调验证，stage="graduated"） ----
    GoalSpec(
        id="prep_wood",
        title="采集起步原木",
        tier="T0_PREP",
        score=0,
        skill="gather_wood",
        skill_args=(("count", 3),),
        done_items=("@wood_for_pickaxe>=1",),
        must_hold_for_children=False,
        budget_s=90.0,
        est_cost_s=20.0,
        stage="graduated",
        editable_files=_STARTER_EDITABLE,
    ),
    GoalSpec(
        id="prep_wooden_pickaxe",
        title="合成木镐",
        tier="T0_PREP",
        score=0,
        skill="craft_gear",
        skill_args=(("item", "wooden_pickaxe"),),
        prereq_goals=("prep_wood",),
        prereq_items=("@gear_ready:wooden_pickaxe>=1",),
        done_items=("@any_pickaxe>=1",),
        must_hold_for_children=True,
        gather_fallbacks=(
            ("@gear_ready:wooden_pickaxe>=1", "gather_wood", (("count", 3),)),
        ),
        budget_s=90.0,
        est_cost_s=15.0,
        stage="graduated",
        editable_files=_STARTER_EDITABLE,
    ),
    GoalSpec(
        id="mine_stone",
        title="石器时代（采集圆石）",
        tier="T1",
        score=5,
        skill="mine_stone",
        skill_args=(("count", 8),),
        prereq_goals=("prep_wooden_pickaxe",),
        done_advancements=("minecraft:story/mine_stone",),
        done_items=("cobblestone|cobbled_deepslate|blackstone>=3",),
        must_hold_for_children=False,
        budget_s=120.0,
        est_cost_s=25.0,
        stage="graduated",
        editable_files=_STARTER_EDITABLE,
    ),
    GoalSpec(
        id="upgrade_tools",
        title="获得升级（合成石镐）",
        tier="T1",
        score=5,
        skill="craft_gear",
        skill_args=(("item", "stone_pickaxe"),),
        prereq_goals=("prep_wooden_pickaxe", "mine_stone"),
        prereq_items=("@gear_ready:stone_pickaxe>=1",),
        done_advancements=("minecraft:story/upgrade_tools",),
        done_items=("@stone_plus_pickaxe>=1",),
        must_hold_for_children=True,
        gather_fallbacks=(
            ("cobblestone|cobbled_deepslate>=3", "mine_stone", (("count", 8),)),
            ("@gear_ready:stone_pickaxe>=1", "gather_wood", (("count", 2),)),
        ),
        budget_s=150.0,
        est_cost_s=20.0,
        stage="graduated",
        editable_files=_STARTER_EDITABLE,
    ),
    GoalSpec(
        id="prep_stone_sword",
        title="合成石剑防身",
        tier="T0_PREP",
        score=0,
        skill="craft_gear",
        skill_args=(("item", "stone_sword"),),
        prereq_goals=("upgrade_tools",),
        prereq_items=("@gear_ready:stone_sword>=1",),
        done_items=("stone_sword|iron_sword|diamond_sword|netherite_sword>=1",),
        must_hold_for_children=False,
        gather_fallbacks=(
            ("cobblestone|cobbled_deepslate>=2", "mine_stone", (("count", 6),)),
            ("@gear_ready:stone_sword>=1", "gather_wood", (("count", 2),)),
        ),
        budget_s=120.0,
        est_cost_s=20.0,
        stage="graduated",
        editable_files=_STARTER_EDITABLE,
    ),
    GoalSpec(
        id="prep_furnace",
        title="合成熔炉（冶炼前置）",
        tier="T0_PREP",
        score=0,
        skill="craft_gear",
        skill_args=(("item", "furnace"),),
        prereq_goals=("upgrade_tools", "prep_stone_sword"),
        prereq_items=("@gear_ready:furnace>=1",),
        done_items=("furnace>=1",),
        must_hold_for_children=True,
        gather_fallbacks=(
            ("cobblestone|cobbled_deepslate>=8", "mine_stone", (("count", 8),)),
            ("@gear_ready:furnace>=1", "gather_wood", (("count", 2),)),
        ),
        budget_s=150.0,
        est_cost_s=25.0,
        stage="graduated",
        editable_files=_STARTER_EDITABLE,
    ),

    # ---- 批次 1：铁器线（docs/11 §二 合计 30 分，逐个置 candidate 优化） ----
    GoalSpec(
        id="smelt_iron",
        title="来硬的（冶炼出铁锭）",
        tier="T1",
        score=5,
        skill="smelt_iron",
        skill_args=(("count", 3),),
        prereq_goals=("upgrade_tools", "prep_furnace"),
        done_advancements=("minecraft:story/smelt_iron",),
        done_items=("iron_ingot>=1",),
        must_hold_for_children=False,
        budget_s=210.0,
        max_fails_per_ep=4,
        est_cost_s=60.0,
        stage="draft",
        editable_files=("code/mcagent/skills/iron.py", "code/tests/test_iron.py"),
    ),
    GoalSpec(
        id="iron_tools",
        title="这不是铁镐么（合成铁镐）",
        tier="T1",
        score=5,
        skill="craft_iron_gear",
        skill_args=(("item", "iron_pickaxe"),),
        prereq_goals=("smelt_iron",),
        prereq_items=("iron_ingot>=3",),
        done_advancements=("minecraft:story/iron_tools",),
        done_items=("@iron_plus_pickaxe>=1",),
        must_hold_for_children=True,
        gather_fallbacks=(
            ("iron_ingot>=3", "smelt_iron", (("count", 3),)),
            ("stick>=2", "gather_wood", (("count", 2),)),
        ),
        budget_s=150.0,
        max_fails_per_ep=3,
        est_cost_s=30.0,
        stage="draft",
        editable_files=("code/mcagent/skills/iron.py", "code/tests/test_iron.py"),
    ),
    GoalSpec(
        id="obtain_armor",
        title="整装上阵（任意铁甲入包）",
        tier="T1",
        score=5,
        skill="craft_iron_gear",
        skill_args=(("item", "iron_boots"),),
        prereq_goals=("smelt_iron",),
        prereq_items=("iron_ingot>=4",),
        done_advancements=("minecraft:story/obtain_armor",),
        done_items=("iron_helmet|iron_chestplate|iron_leggings|iron_boots>=1",),
        gather_fallbacks=(
            ("iron_ingot>=4", "smelt_iron", (("count", 4),)),
        ),
        budget_s=150.0,
        max_fails_per_ep=3,
        est_cost_s=35.0,
        stage="draft",
        editable_files=("code/mcagent/skills/iron.py", "code/tests/test_iron.py"),
    ),
    GoalSpec(
        id="lava_bucket",
        title="热腾腾的（装满一桶岩浆）",
        tier="T1",
        score=15,
        skill="fill_lava_bucket",
        prereq_goals=("smelt_iron",),
        done_advancements=("minecraft:story/lava_bucket",),
        done_items=("lava_bucket>=1",),
        gather_fallbacks=(
            ("bucket|iron_ingot>=3", "smelt_iron", (("count", 3),)),
        ),
        budget_s=210.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=50.0,
        stage="draft",
        editable_files=("code/mcagent/skills/iron.py", "code/tests/test_iron.py"),
    ),

    # ---- 批次 2：顺手生存项（床、种地、繁殖） ----
    GoalSpec(
        id="sleep_in_bed",
        title="甜蜜的梦（躺床设重生点并防幻翼）",
        tier="T1",
        score=5,
        skill="sleep_in_bed",
        prereq_goals=("prep_wood",),
        done_advancements=("minecraft:adventure/sleep_in_bed",),
        budget_s=150.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=45.0,
        stage="draft",
        editable_files=("code/mcagent/skills/survival.py", "code/tests/test_survival.py"),
    ),
    GoalSpec(
        id="plant_seed",
        title="开荒垦地（种下一颗种子）",
        tier="T1",
        score=5,
        skill="plant_seed",
        prereq_goals=("prep_wood",),
        done_advancements=("minecraft:husbandry/plant_seed",),
        budget_s=90.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=30.0,
        stage="draft",
        editable_files=("code/mcagent/skills/survival.py", "code/tests/test_survival.py"),
    ),
    GoalSpec(
        id="breed_an_animal",
        title="成双成对（繁殖一对动物）",
        tier="T1",
        score=5,
        skill="breed_animal",
        prereq_goals=("prep_wood",),
        done_advancements=("minecraft:husbandry/breed_an_animal",),
        budget_s=150.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=55.0,
        stage="draft",
        editable_files=("code/mcagent/skills/survival.py", "code/tests/test_survival.py"),
    ),

    # ---- 批次 3：战斗、盾牌与线系（合计 55 分） ----
    GoalSpec(
        id="kill_a_mob",
        title="怪物猎人（击杀任一敌对生物）",
        tier="T1",
        score=5,
        skill="hunt_mob",
        prereq_goals=("prep_stone_sword",),
        done_advancements=("minecraft:adventure/kill_a_mob",),
        budget_s=120.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=40.0,
        stage="draft",
        editable_files=("code/mcagent/skills/combat.py", "code/tests/test_combat.py"),
    ),
    GoalSpec(
        id="deflect_arrow",
        title="抱歉，今天不行（举盾挡住弹射物）",
        tier="T1",
        score=15,
        skill="deflect_skeleton_arrow",
        prereq_goals=("smelt_iron",),
        done_advancements=("minecraft:story/deflect_arrow",),
        budget_s=180.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=55.0,
        stage="draft",
        editable_files=("code/mcagent/skills/combat.py", "code/tests/test_combat.py"),
    ),
    GoalSpec(
        id="shoot_arrow",
        title="瞄准目标（射箭命中实体）",
        tier="T1",
        score=5,
        skill="shoot_mob",
        prereq_goals=("kill_a_mob",),
        done_advancements=("minecraft:adventure/shoot_arrow",),
        budget_s=150.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=45.0,
        stage="draft",
        editable_files=("code/mcagent/skills/combat.py", "code/tests/test_combat.py"),
    ),
    GoalSpec(
        id="ol_betsy",
        title="扣下悬刀（用弩射出一箭）",
        tier="T1",
        score=15,
        skill="fire_crossbow",
        prereq_goals=("smelt_iron", "kill_a_mob"),
        done_advancements=("minecraft:adventure/ol_betsy",),
        budget_s=150.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=50.0,
        stage="draft",
        editable_files=("code/mcagent/skills/combat.py", "code/tests/test_combat.py"),
    ),
    GoalSpec(
        id="fishy_business",
        title="腥味生意（自然水域钓上一条鱼）",
        tier="T1",
        score=15,
        skill="catch_fish",
        prereq_goals=("kill_a_mob",),
        done_advancements=("minecraft:husbandry/fishy_business",),
        budget_s=240.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=95.0,
        stage="draft",
        editable_files=("code/mcagent/skills/survival.py", "code/tests/test_survival.py"),
    ),

    # ---- 批次 4：钻石层高分核心与下界门（shiny_gear 4 钻 60 分，性价比全表第一） ----
    GoalSpec(
        id="mine_diamond",
        title="钻石！（深层掘进与暴露面扫描）",
        tier="T1",
        score=15,
        skill="mine_diamond",
        skill_args=(("count", 4),),
        prereq_goals=("iron_tools",),
        done_advancements=("minecraft:story/mine_diamond",),
        done_items=("diamond>=1",),
        must_hold_for_children=False,
        budget_s=420.0,
        max_fails_per_ep=4,
        stop_loss=True,
        est_cost_s=150.0,
        stage="draft",
        editable_files=("code/mcagent/skills/diamond.py", "code/tests/test_diamond.py"),
    ),
    GoalSpec(
        id="shiny_gear",
        title="钻石护体（4 颗钻石合钻石靴拿 60 分）",
        tier="T1",
        score=60,
        skill="craft_diamond_gear",
        skill_args=(("item", "diamond_boots"),),
        prereq_goals=("mine_diamond",),
        prereq_items=("diamond>=4",),
        done_advancements=("minecraft:story/shiny_gear",),
        done_items=("diamond_helmet|diamond_chestplate|diamond_leggings|diamond_boots>=1",),
        gather_fallbacks=(
            ("diamond>=4", "mine_diamond", (("count", 4),)),
        ),
        budget_s=120.0,
        max_fails_per_ep=3,
        est_cost_s=25.0,
        stage="draft",
        editable_files=("code/mcagent/skills/diamond.py", "code/tests/test_diamond.py"),
    ),
    GoalSpec(
        id="form_obsidian",
        title="冰桶挑战（获取黑曜石）",
        tier="T1",
        score=15,
        skill="mine_obsidian",
        skill_args=(("count", 10),),
        prereq_goals=("mine_diamond",),
        done_advancements=("minecraft:story/form_obsidian",),
        done_items=("obsidian>=1",),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=85.0,
        stage="draft",
        editable_files=("code/mcagent/skills/nether.py", "code/tests/test_nether.py"),
    ),
    GoalSpec(
        id="enter_the_nether",
        title="勇往直下（搭建并点燃进入下界门）",
        tier="T1",
        score=30,
        skill="build_and_enter_portal",
        prereq_goals=("form_obsidian", "smelt_iron"),
        done_advancements=("minecraft:story/enter_the_nether",),
        budget_s=240.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=95.0,
        stage="draft",
        editable_files=("code/mcagent/skills/nether.py", "code/tests/test_nether.py"),
    ),
)

GOAL_MAP: dict[str, GoalSpec] = {g.id: g for g in GOALS}


# ============================================================================
# 物资表达式求值与目标达成判据
# ============================================================================

def eval_item_expr(state: State, expr: str) -> tuple[bool, int, int, str]:
    """求值单条物资判据表达式，返回 `(是否满足, 当前数量, 需要数量, 可读说明)`。"""
    raw = expr.strip()
    if ">=" in raw:
        lhs, rhs = raw.split(">=", 1)
        lhs = lhs.strip()
        need = max(1, int(rhs.strip()))
    else:
        lhs = raw
        need = 1

    assume_no_tbl = not has_known_table(state.pos)

    if lhs == "@logs":
        cur = total_logs(state)
        label = "原木"
    elif lhs == "@planks":
        cur = total_planks(state)
        label = "木板"
    elif lhs == "@wood_equiv":
        cur = total_logs(state) * 4 + total_planks(state)
        label = "木板当量"
    elif lhs == "@cobble":
        cur = total_cobble(state)
        label = "圆石"
    elif lhs == "@any_pickaxe":
        cur = 1 if best_pickaxe(state) is not None else 0
        label = "任意镐"
    elif lhs == "@stone_plus_pickaxe":
        cur = 1 if best_pickaxe(state) in STONE_OR_BETTER_PICKS else 0
        label = "石镐及以上"
    elif lhs == "@iron_plus_pickaxe":
        cur = 1 if best_pickaxe(state) in IRON_OR_BETTER_PICKS else 0
        label = "铁镐及以上"
    elif lhs == "@diamond_plus_pickaxe":
        cur = 1 if best_pickaxe(state) in DIAMOND_OR_BETTER_PICKS else 0
        label = "钻石镐及以上"
    elif lhs == "@wood_for_pickaxe":
        ready = (
            best_pickaxe(state) is not None
            or can_afford_gear(state, "wooden_pickaxe", assume_no_table=assume_no_tbl)
        )
        cur = 1 if ready else 0
        label = "木镐所需原木/木板"
    elif lhs.startswith("@gear_ready:"):
        gear_item = lhs.split(":", 1)[1].strip()
        cur = 1 if can_afford_gear(state, gear_item, assume_no_table=assume_no_tbl) else 0
        label = f"合成 {gear_item} 所需材料与工作台条件"
    else:
        names = [x.strip() for x in lhs.split("|") if x.strip()]
        cur = sum(state.count(n) for n in names)
        label = lhs

    return cur >= need, cur, need, f"{label} ({cur}/{need})"


def check_item_expr(state: State, expr: str) -> bool:
    return eval_item_expr(state, expr)[0]


def is_goal_held_in_state(goal: GoalSpec, state: State) -> bool:
    """检查当前背包是否实际持有该目标声明的 `done_items`（用于重生装备丢失后的前置重补）。"""
    if not goal.done_items:
        return False
    return all(check_item_expr(state, expr) for expr in goal.done_items)


def is_goal_achieved(
    goal: GoalSpec,
    state: State,
    completed_advs: Collection[str] = (),
) -> bool:
    """检查某目标在本局是否已达成（服务端确认 advancement 或背包代理判据任一成立）。"""
    if goal.done_advancements and any(a in completed_advs for a in goal.done_advancements):
        return True
    if goal.done_items and all(check_item_expr(state, expr) for expr in goal.done_items):
        return True
    return False


def is_prereq_satisfied(
    goal: GoalSpec,
    state: State,
    completed_advs: Collection[str] = (),
) -> bool:
    """作为下游目标前置时的满足判定：工具/工作站类前置要求当前背包确实持有（兼容 keepInventory=False）。"""
    if goal.must_hold_for_children and goal.done_items:
        return is_goal_held_in_state(goal, state)
    return is_goal_achieved(goal, state, completed_advs)


# ============================================================================
# 台账契约核验、阶段过滤与链路性价比排序
# ============================================================================

def validate_ledger(
    ledger: tuple[GoalSpec, ...] = GOALS,
    registry: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """校验目标台账静态合法性：唯一 ID、无环依赖、单 candidate 纪律、已激活目标技能参数契约。"""
    reg = REGISTRY if registry is None else registry
    errors: list[str] = []
    by_id: dict[str, GoalSpec] = {}

    for g in ledger:
        if not g.id:
            errors.append("存在空目标 id")
            continue
        if g.id in by_id:
            errors.append(f"重复的目标 id: {g.id}")
            continue
        by_id[g.id] = g
        if g.stage not in GOAL_STAGES:
            errors.append(f"{g.id}: 非法 stage={g.stage!r}")
        if g.tier not in GOAL_TIERS:
            errors.append(f"{g.id}: 非法 tier={g.tier!r}")
        if g.score < 0:
            errors.append(f"{g.id}: score 不能为负数 ({g.score})")
        if g.est_cost_s <= 0:
            errors.append(f"{g.id}: est_cost_s 必须 > 0 ({g.est_cost_s})")

    candidates = [g.id for g in ledger if g.stage == "candidate"]
    if len(candidates) > 1:
        errors.append(f"单目标晋级纪律违规：同一时刻最多允许 1 个 candidate 目标，当前有 {candidates}")

    for g in ledger:
        for pid in g.prereq_goals:
            if pid not in by_id:
                errors.append(f"{g.id}: 引用了不存在的前置目标 {pid!r}")

        if g.stage in DEFAULT_CAMPAIGN_STAGES:
            cls = reg.get(g.skill)
            if cls is None:
                errors.append(f"{g.id} (stage={g.stage}): 主技能 {g.skill!r} 未在 skills.REGISTRY 注册")
            else:
                _, arg_errs = bind_args(cls, g.args_dict)
                if arg_errs:
                    errors.append(f"{g.id}: 主技能参数 {g.args_dict} 不符合契约: {' | '.join(arg_errs)}")
            for _, fb_skill, fb_args in g.gather_fallbacks:
                fb_cls = reg.get(fb_skill)
                if fb_cls is None:
                    errors.append(f"{g.id}: 补料技能 {fb_skill!r} 未在 skills.REGISTRY 注册")
                else:
                    _, fb_errs = bind_args(fb_cls, dict(fb_args))
                    if fb_errs:
                        errors.append(f"{g.id}: 补料技能 {fb_skill} 参数非法: {' | '.join(fb_errs)}")

    # 依赖有向无环图（DAG）检测
    visiting: set[str] = set()
    visited: set[str] = set()

    def _dfs(gid: str) -> None:
        if gid in visited or gid not in by_id:
            return
        if gid in visiting:
            errors.append(f"目标前置依赖存在环路，涉及 {gid!r}")
            return
        visiting.add(gid)
        for pid in by_id[gid].prereq_goals:
            _dfs(pid)
        visiting.remove(gid)
        visited.add(gid)

    for gid in by_id:
        _dfs(gid)

    return errors


def resolve_active_goals(
    ledger: tuple[GoalSpec, ...] = GOALS,
    *,
    include_stages: Collection[str] = DEFAULT_CAMPAIGN_STAGES,
    target_goal: Optional[str] = None,
) -> tuple[GoalSpec, ...]:
    """解析本轮激活的目标集合：指定 `target_goal` 时自动收敛为其传递前置闭包 + 目标自身。"""
    by_id = {g.id: g for g in ledger}
    if target_goal:
        if target_goal not in by_id:
            raise KeyError(f"未知目标 id: {target_goal!r}（可选: {', '.join(by_id)}）")
        needed: set[str] = set()

        def _collect(gid: str) -> None:
            if gid in needed or gid not in by_id:
                return
            needed.add(gid)
            for pid in by_id[gid].prereq_goals:
                _collect(pid)

        _collect(target_goal)
        return tuple(g for g in ledger if g.id in needed)

    allowed = set(include_stages)
    return tuple(g for g in ledger if g.stage in allowed)


def effective_goal_cost(
    goal: GoalSpec,
    telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> float:
    """结合挂机实测成本（成功率与 P50 用时）或静态初值计算目标期望耗时成本。"""
    base = max(1.0, float(goal.est_cost_s))
    if not telemetry:
        return base
    row = telemetry.get(goal.id)
    if not isinstance(row, Mapping):
        return base
    episodes = int(row.get("episodes", 0) or 0)
    hits = int(row.get("hits", 0) or 0)
    hit_rate = float(row.get("hit_rate", 0.0) or 0.0)
    p50_s = float(row.get("p50_elapsed_s", 0.0) or 0.0)
    if episodes <= 0:
        return base
    if hits > 0 and p50_s > 0.0:
        return max(1.0, p50_s / max(0.15, hit_rate))
    if episodes >= 3 and hits == 0:
        return base * 4.0
    return base


def is_goal_stopped_out(
    goal: GoalSpec,
    ep_goal_fails: Optional[Mapping[str, int]] = None,
    ep_goal_elapsed: Optional[Mapping[str, float]] = None,
) -> bool:
    """检查目标在本轮内是否已触及失败次数上限或硬时间止损线。"""
    if goal.max_fails_per_ep > 0 and ep_goal_fails is not None:
        if int(ep_goal_fails.get(goal.id, 0)) >= goal.max_fails_per_ep:
            return True
    if goal.stop_loss and goal.budget_s > 0.0 and ep_goal_elapsed is not None:
        if float(ep_goal_elapsed.get(goal.id, 0.0)) >= goal.budget_s:
            return True
    return False


def rank_actionable_goals(
    state: State,
    active_goals: tuple[GoalSpec, ...],
    *,
    completed_advs: Collection[str] = (),
    target_goal: Optional[str] = None,
    ep_goal_fails: Optional[Mapping[str, int]] = None,
    ep_goal_elapsed: Optional[Mapping[str, float]] = None,
    telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> list[GoalPlanStep]:
    """对所有尚未达成且未触发止损的目标计算未满足前置链，按性价比降序返回可执行步列表。"""
    by_id = {g.id: g for g in active_goals}
    order_idx = {g.id: idx for idx, g in enumerate(active_goals)}

    def _unmet_chain(root: GoalSpec) -> Optional[list[GoalSpec]]:
        chain: list[GoalSpec] = []
        seen: set[str] = set()

        def _walk(node: GoalSpec, is_root: bool) -> bool:
            if node.id in seen:
                return True
            if not is_root and is_prereq_satisfied(node, state, completed_advs):
                return True
            if is_goal_stopped_out(node, ep_goal_fails, ep_goal_elapsed):
                return False
            for pid in node.prereq_goals:
                parent = by_id.get(pid)
                if parent is None:
                    continue
                if not _walk(parent, False):
                    return False
            seen.add(node.id)
            chain.append(node)
            return True

        if not _walk(root, True):
            return None
        return chain

    raw_candidates: list[tuple[tuple[int, int, float, int], GoalPlanStep]] = []
    for g in active_goals:
        if is_goal_achieved(g, state, completed_advs):
            continue
        chain = _unmet_chain(g)
        if not chain:
            continue
        p_score = sum(x.score for x in chain if not is_goal_achieved(x, state, completed_advs))
        p_cost = sum(effective_goal_cost(x, telemetry) for x in chain)
        roi = p_score / max(1.0, p_cost)
        prio = (
            1 if (target_goal and g.id == target_goal) else 0,
            1 if p_score > 0 else 0,
            round(roi, 6),
            -order_idx[g.id],
        )
        step = GoalPlanStep(
            step_goal=chain[0],
            root_goal=g,
            chain=tuple(chain),
            path_score=p_score,
            path_cost=round(p_cost, 1),
            roi=round(roi, 4),
        )
        raw_candidates.append((prio, step))

    raw_candidates.sort(key=lambda item: item[0], reverse=True)

    deduped: list[GoalPlanStep] = []
    seen_step_ids: set[str] = set()
    for _, step in raw_candidates:
        if step.step_goal.id in seen_step_ids:
            continue
        seen_step_ids.add(step.step_goal.id)
        deduped.append(step)
    return deduped


def diagnose_goal_gap(
    goal: GoalSpec,
    state: State,
    completed_advs: Collection[str] = (),
    ledger: tuple[GoalSpec, ...] = GOALS,
) -> dict[str, Any]:
    """诊断目标当前尚未达成的精确卡点（已完成前置、缺失前置、瓶颈目标与物资缺口）。"""
    by_id = {g.id: g for g in ledger}
    ordered_prereqs: list[GoalSpec] = []
    seen: set[str] = set()

    def _collect_prereqs(node: GoalSpec) -> None:
        for pid in node.prereq_goals:
            p = by_id.get(pid)
            if p is None or p.id in seen:
                continue
            _collect_prereqs(p)
            seen.add(p.id)
            ordered_prereqs.append(p)

    _collect_prereqs(goal)
    completed_prereqs = [p.id for p in ordered_prereqs if is_prereq_satisfied(p, state, completed_advs)]
    missing_prereqs = [p.id for p in ordered_prereqs if not is_prereq_satisfied(p, state, completed_advs)]

    bottleneck = by_id[missing_prereqs[0]] if missing_prereqs else goal
    missing_items: list[str] = []
    for expr in (*bottleneck.prereq_items, *bottleneck.done_items):
        ok, _, _, desc = eval_item_expr(state, expr)
        if not ok and desc not in missing_items:
            missing_items.append(desc)
    for req_expr, _, _ in bottleneck.gather_fallbacks:
        ok, _, _, desc = eval_item_expr(state, req_expr)
        if not ok and desc not in missing_items:
            missing_items.append(desc)

    if bottleneck.id != goal.id:
        summary = f"目标 {goal.id} 卡在前置 {bottleneck.id}（{bottleneck.title}），缺口：{', '.join(missing_items) or '尚未执行完成'}"
    else:
        summary = f"目标 {goal.id}（{goal.title}）前置已齐备，缺口：{', '.join(missing_items) or '待触发达成判据'}"

    return {
        "goal_id": goal.id,
        "goal_title": goal.title,
        "tier": goal.tier,
        "score": goal.score,
        "skill": goal.skill,
        "skill_args": goal.args_dict,
        "completed_prereqs": completed_prereqs,
        "missing_prereqs": missing_prereqs,
        "bottleneck_goal_id": bottleneck.id,
        "bottleneck_skill": bottleneck.skill,
        "missing_items": missing_items,
        "editable_files": list(goal.editable_files),
        "summary": summary,
    }


def load_goal_telemetry(path: str | Path | None) -> dict[str, dict[str, Any]]:
    """读取隔离落盘的挂机实测成本 JSON（不存在或损坏时安全回退空字典）。"""
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        goals_obj = data.get("goals") if isinstance(data, dict) else None
        return dict(goals_obj) if isinstance(goals_obj, dict) else {}
    except Exception:
        return {}
