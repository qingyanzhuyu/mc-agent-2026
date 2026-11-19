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
from ..skills.items import STONE_OR_BETTER_PICKS, best_pickaxe
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
GOAL_TIERS: tuple[str, ...] = ("T0_PREP", "T1", "T2", "T3", "T4_PREP", "T4")

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
    tier: str                                  # "T0_PREP" | "T1" | "T2" | "T3" | "T4_PREP" | "T4"
    score: int                                 # RULES v2 分值（前置准备项允许为 0）
    skill: str                                 # 达成该目标的主技能名
    skill_args: tuple[tuple[str, Any], ...] = ()
    prereq_goals: tuple[str, ...] = ()         # 前置目标 id
    prereq_items: tuple[str, ...] = ()         # 启动主技能前需满足的物资表达式
    done_advancements: tuple[str, ...] = ()    # 服务端确认达成 advancement id
    done_items: tuple[str, ...] = ()           # 背包达成代理判据表达式
    must_hold_for_children: bool = False       # 作为下游前置时，是否要求当前持有
    gather_fallbacks: tuple[tuple[str, str, tuple[tuple[str, Any], ...]], ...] = ()
    budget_s: float = 180.0                    # 单轮时间预算参考/止损上限（秒）
    max_fails_per_ep: int = 0                  # 单轮最大允许失败次数（0=不限，靠技能指数冷却退避）
    stop_loss: bool = False                    # 是否启用硬时间预算止损（T2/T3 或高风险支线开启）
    est_cost_s: float = 30.0                   # 尚无实测遥测时的静态耗时初值（秒）
    stage: str = "draft"                       # "draft" | "candidate" | "graduated" | "live"
    editable_files: tuple[str, ...] = ()       # GOAL_MISS 自动修复时允许改动的收窄白名单
    held_items: tuple[str, ...] = ()            # 下游所需实物，未指定时沿用 done_items
    gather_when_blocked: bool = False          # 允许主技能先观察复用场景，无法启动时再尝试补料

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
    shared_dependents: int = 1                 # 共享 step_goal 的可执行计分目标数
    marginal_roi: Optional[float] = None       # 共享子图分摊后的边际性价比（无共享时等于 roi）


# ============================================================================
# 静态目标台账（按 docs/10 推进批次与 docs/11 T1 依赖链排列）
# - 已跑通的基础木石链标为 graduated；
# - 待逐个接入的新技能目标标为 draft，接入时每次仅将 1 个目标改为 candidate。
# ============================================================================

_STARTER_EDITABLE: tuple[str, ...] = (
    "code/mcagent/loop/starter_skills.py",
    "code/tests/test_loop_soak.py",
)

_COMBAT_EDITABLE = ("code/mcagent/skills/combat.py", "code/mcagent/skills/combat_materials.py",
                    "code/mcagent/skills/combat_scene.py", "code/tests/test_combat.py")
_T2_OVERWORLD_EDITABLE: tuple[str, ...] = (
    "code/mcagent/skills/t2_overworld.py",
    "code/tests/test_stage2_overworld.py",
)
_END_RUSH_EDITABLE: tuple[str, ...] = (
    "code/mcagent/skills/end_rush.py",
    "code/tests/test_stage2_end_rush.py",
)
_T2_TRIALS_ORIENTED_EDITABLE: tuple[str, ...] = (
    "code/mcagent/skills/t2_trials_oriented.py",
    "code/tests/test_stage2_trials_oriented.py",
)
_T3_BIOMES_EDITABLE: tuple[str, ...] = (
    "code/mcagent/skills/t3_biomes.py",
    "code/tests/test_stage2_t3_biomes.py",
)
_T4_QUICK_EDITABLE: tuple[str, ...] = (
    "code/mcagent/skills/t4_quick_exploits.py",
    "code/tests/test_stage3_base_and_wave_a.py",
)
_T4_END_FROGS_EDITABLE: tuple[str, ...] = (
    "code/mcagent/skills/t4_end_and_frogs.py",
    "code/tests/test_stage3_base_and_wave_a.py",
)
_FISHING_STOCK = ("string>=2", "hunt_mob", (("target", "spider"), ("drop", "string"), ("count", 2)))
_ARROW_STOCK = ("arrow|spectral_arrow|tipped_arrow>=1", "hunt_mob",
                (("target", "skeleton"), ("drop", "arrow"), ("count", 1)))


def _stick_fallbacks(count: int):
    return (
        (f"@stick_stock:{count}>=1", "gather_wood", (("count", 2),)),
        (f"stick>={count}", "craft_gear", (("item", "stick"),)),
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
        done_items=("stone_pickaxe|iron_pickaxe|diamond_pickaxe|netherite_pickaxe>=1",),
        must_hold_for_children=True,
        held_items=("@stone_plus_pickaxe>=1",),
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
        stage="candidate",
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
        prereq_items=("iron_ingot>=3", "stick>=2"),
        done_advancements=("minecraft:story/iron_tools",),
        done_items=("iron_pickaxe>=1",),
        must_hold_for_children=True,
        held_items=("@iron_plus_pickaxe>=1",),
        gather_fallbacks=(
            ("iron_ingot>=3", "smelt_iron", (("count", 3),)),
            *_stick_fallbacks(2),
        ),
        budget_s=150.0,
        max_fails_per_ep=3,
        est_cost_s=30.0,
        stage="draft",
        editable_files=("code/mcagent/skills/iron_gear.py", "code/tests/test_iron.py"),
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
        editable_files=("code/mcagent/skills/iron_gear.py", "code/tests/test_iron.py"),
    ),
    GoalSpec(
        id="lava_bucket",
        title="热腾腾的（装满一桶岩浆）",
        tier="T1",
        score=15,
        skill="fill_lava_bucket",
        prereq_goals=("smelt_iron",),
        prereq_items=("bucket>=1",),
        done_advancements=("minecraft:story/lava_bucket",),
        done_items=("lava_bucket>=1",),
        gather_fallbacks=(
            ("bucket|iron_ingot>=3", "smelt_iron", (("count", 3),)),
            ("bucket>=1", "craft_iron_gear", (("item", "bucket"),)),
        ),
        budget_s=210.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=50.0,
        stage="draft",
        editable_files=("code/mcagent/skills/iron_lava.py", "code/tests/test_iron.py"),
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
        editable_files=("code/mcagent/skills/survival.py", "code/mcagent/skills/survival_camp.py",
                        "code/tests/test_survival.py"),
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
        editable_files=("code/mcagent/skills/survival.py", "code/mcagent/skills/survival_farm.py",
                        "code/tests/test_survival.py"),
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
        editable_files=("code/mcagent/skills/survival.py", "code/mcagent/skills/survival_farm.py",
                        "code/tests/test_survival.py"),
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
        editable_files=_COMBAT_EDITABLE,
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
        editable_files=_COMBAT_EDITABLE,
    ),
    GoalSpec(
        id="shoot_arrow",
        title="瞄准目标（射箭命中实体）",
        tier="T1",
        score=5,
        skill="shoot_mob",
        prereq_goals=("kill_a_mob",),
        done_advancements=("minecraft:adventure/shoot_arrow",),
        gather_when_blocked=True,
        gather_fallbacks=(
            _ARROW_STOCK,
            ("string>=5", "hunt_mob", (("target", "spider"), ("drop", "string"), ("count", 5))),
            *_stick_fallbacks(3),
        ),
        budget_s=150.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=45.0,
        stage="draft",
        editable_files=_COMBAT_EDITABLE,
    ),
    GoalSpec(
        id="ol_betsy",
        title="扣下悬刀（用弩射出一箭）",
        tier="T1",
        score=15,
        skill="fire_crossbow",
        prereq_goals=("smelt_iron", "kill_a_mob"),
        done_advancements=("minecraft:adventure/ol_betsy",),
        gather_when_blocked=True,
        gather_fallbacks=(
            ("arrow|spectral_arrow|tipped_arrow|firework_rocket>=1", _ARROW_STOCK[1], _ARROW_STOCK[2]),
            ("string>=4", "hunt_mob", (("target", "spider"), ("drop", "string"), ("count", 4))),
            *_stick_fallbacks(4),
        ),
        budget_s=150.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=50.0,
        stage="draft",
        editable_files=_COMBAT_EDITABLE,
    ),
    GoalSpec(
        id="fishy_business",
        title="腥味生意（自然水域钓上一条鱼）",
        tier="T1",
        score=15,
        skill="catch_fish",
        prereq_goals=("kill_a_mob",),
        done_advancements=("minecraft:husbandry/fishy_business",),
        gather_when_blocked=True,
        gather_fallbacks=(
            _FISHING_STOCK,
            *_stick_fallbacks(3),
        ),
        budget_s=240.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=95.0,
        stage="draft",
        editable_files=("code/mcagent/skills/survival.py", "code/mcagent/skills/survival_fish.py",
                        "code/tests/test_survival.py"),
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
        editable_files=("code/mcagent/skills/diamond.py", "code/mcagent/skills/diamond_site.py",
                        "code/tests/test_diamond.py"),
    ),
    GoalSpec(
        id="shiny_gear",
        title="钻石护体（4 颗钻石合钻石靴拿 60 分）",
        tier="T1",
        score=60,
        skill="craft_diamond_gear",
        skill_args=(("item", "diamond_boots"),),
        prereq_goals=("mine_diamond",),
        done_advancements=("minecraft:story/shiny_gear",),
        done_items=("diamond_helmet|diamond_chestplate|diamond_leggings|diamond_boots>=1",),
        gather_when_blocked=True,
        gather_fallbacks=(
            ("diamond>=4", "mine_diamond", (("count", 4),)),
        ),
        budget_s=120.0,
        max_fails_per_ep=3,
        est_cost_s=25.0,
        stage="draft",
        editable_files=("code/mcagent/skills/diamond.py", "code/mcagent/skills/diamond_site.py",
                        "code/tests/test_diamond.py"),
    ),
    GoalSpec(
        id="prep_diamond_pickaxe",
        title="合成钻石镐（黑曜石采掘前置）",
        tier="T0_PREP",
        score=0,
        skill="craft_diamond_gear",
        skill_args=(("item", "diamond_pickaxe"),),
        prereq_goals=("mine_diamond",),
        done_items=("@diamond_plus_pickaxe>=1",),
        must_hold_for_children=True,
        gather_when_blocked=True,
        gather_fallbacks=(
            ("diamond>=3", "mine_diamond", (("count", 3),)),
            *_stick_fallbacks(2),
        ),
        budget_s=120.0,
        max_fails_per_ep=3,
        est_cost_s=25.0,
        stage="draft",
        editable_files=("code/mcagent/skills/diamond.py", "code/mcagent/skills/diamond_site.py",
                        "code/tests/test_diamond.py"),
    ),
    GoalSpec(
        id="form_obsidian",
        title="冰桶挑战（获取黑曜石）",
        tier="T1",
        score=15,
        skill="mine_obsidian",
        skill_args=(("count", 1),),
        prereq_goals=("prep_diamond_pickaxe",),
        done_advancements=("minecraft:story/form_obsidian",),
        done_items=("obsidian>=1",),
        gather_when_blocked=True,
        gather_fallbacks=(
            ("@diamond_plus_pickaxe>=1", "craft_diamond_gear", (("item", "diamond_pickaxe"),)),
            ("bucket|water_bucket>=1", "craft_iron_gear", (("item", "bucket"),)),
        ),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=85.0,
        stage="draft",
        editable_files=("code/mcagent/skills/nether.py", "code/mcagent/skills/nether_mining.py",
                        "code/mcagent/skills/nether_site.py", "code/tests/test_nether.py"),
    ),
    GoalSpec(
        id="enter_the_nether",
        title="勇往直下（搭建并点燃进入下界门）",
        tier="T1",
        score=30,
        skill="build_and_enter_portal",
        prereq_goals=("form_obsidian", "smelt_iron"),
        done_advancements=("minecraft:story/enter_the_nether",),
        gather_when_blocked=True,
        gather_fallbacks=(
            ("obsidian>=10", "mine_obsidian", (("count", 10),)),
            ("flint|flint_and_steel>=1", "build_and_enter_portal", (("prepare_flint", True),)),
            ("iron_ingot|flint_and_steel>=1", "smelt_iron", (("count", 1),)),
            ("flint_and_steel>=1", "craft_iron_gear", (("item", "flint_and_steel"),)),
        ),
        budget_s=240.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=95.0,
        stage="draft",
        editable_files=("code/mcagent/skills/nether.py", "code/mcagent/skills/nether_portal.py",
                        "code/mcagent/skills/nether_mining.py", "code/mcagent/skills/nether_site.py",
                        "code/tests/test_nether.py"),
    ),
    GoalSpec(
        id="enchant_item",
        title="附魔师（附魔一件物品）",
        tier="T1",
        score=30,
        skill="enchant_item",
        prereq_goals=("prep_wood",),
        prereq_items=("lapis_lazuli>=1",),
        done_advancements=("minecraft:story/enchant_item",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=160.0,
        stage="draft",
        editable_files=("code/mcagent/skills/enchant.py", "code/tests/test_enchant.py"),
    ),
    GoalSpec(
        id="use_lodestone",
        title="天涯共此石（指南针绑定磁石）",
        tier="T1",
        score=15,
        skill="attune_lodestone",
        prereq_goals=("prep_wood",),
        prereq_items=("compass>=1",),
        done_advancements=("minecraft:adventure/use_lodestone",),
        gather_fallbacks=(("compass>=1", "craft_iron_gear", (("item", "compass"),)),),
        budget_s=320.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=140.0,
        stage="draft",
        editable_files=("code/mcagent/skills/lodestone.py", "code/tests/test_lodestone.py"),
    ),
    GoalSpec(
        id="bullseye",
        title="正中靶心（远距离命中标靶中心）",
        tier="T1",
        score=15,
        skill="hit_bullseye",
        prereq_goals=("shoot_arrow",),
        done_advancements=("minecraft:adventure/bullseye",),
        gather_when_blocked=True,
        gather_fallbacks=(_ARROW_STOCK,),
        budget_s=300.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=135.0,
        stage="draft",
        editable_files=("code/mcagent/skills/target.py", "code/tests/test_target.py"),
    ),

    # ---- Stage 2 Batch 1：结构与群系勘探（5 goals / 165 pts） ----
    GoalSpec(
        id="follow_ender_eye",
        title="隔墙有眼（投掷末影之眼定位要塞）",
        tier="T2",
        score=60,
        skill="discover_structure",
        skill_args=(("structure", "stronghold"),),
        done_advancements=("minecraft:story/follow_ender_eye",),
        budget_s=480.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=320.0,
        stage="draft",
    ),
    GoalSpec(
        id="find_fortress",
        title="阴森堡垒（定位并进入下界要塞）",
        tier="T2",
        score=30,
        skill="discover_structure",
        skill_args=(("structure", "nether_fortress"),),
        prereq_goals=("enter_the_nether",),
        done_advancements=("minecraft:nether/find_fortress",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=200.0,
        stage="draft",
    ),
    GoalSpec(
        id="find_bastion",
        title="光辉岁月（定位并进入猪灵堡垒）",
        tier="T2",
        score=30,
        skill="discover_structure",
        skill_args=(("structure", "bastion_remnant"),),
        prereq_goals=("enter_the_nether",),
        done_advancements=("minecraft:nether/find_bastion",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=200.0,
        stage="draft",
    ),
    GoalSpec(
        id="minecraft_trials_edition",
        title="我的世界：试炼版（定位并踏入试炼密室）",
        tier="T2",
        score=15,
        skill="discover_structure",
        skill_args=(("structure", "trial_chambers"),),
        done_advancements=("minecraft:adventure/minecraft_trials_edition",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=180.0,
        stage="draft",
    ),
    GoalSpec(
        id="explore_nether",
        title="热门旅游景点（探索全部下界群系）",
        tier="T3",
        score=30,
        skill="discover_structure",
        skill_args=(("structure", "nether_biomes"),),
        prereq_goals=("enter_the_nether",),
        done_advancements=("minecraft:nether/explore_nether",),
        budget_s=480.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=300.0,
        stage="draft",
    ),

    # ---- Stage 2 Batch 2A：工作站、锻造与炼药线（5 goals / 180 pts） ----
    GoalSpec(
        id="trade",
        title="这交易划算吗（与村民完成交易）",
        tier="T2",
        score=15,
        skill="trade_villager",
        done_advancements=("minecraft:adventure/trade",),
        budget_s=300.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=150.0,
        stage="draft",
    ),
    GoalSpec(
        id="trim_with_any_armor_pattern",
        title="锻造新风尚（为盔甲锻造纹饰）",
        tier="T2",
        score=30,
        skill="trim_armor",
        done_advancements=("minecraft:adventure/trim_with_any_armor_pattern",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=210.0,
        stage="draft",
    ),
    GoalSpec(
        id="obtain_netherite_hoe",
        title="终极奉献（锻造下界合金锄）",
        tier="T3",
        score=60,
        skill="smith_netherite_hoe",
        prereq_goals=("obtain_ancient_debris", "loot_bastion"),
        done_advancements=("minecraft:husbandry/obtain_netherite_hoe",),
        done_items=("netherite_hoe>=1",),
        budget_s=420.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=240.0,
        stage="draft",
    ),
    GoalSpec(
        id="brew_potion",
        title="本地酿造厂（酿造一瓶药水）",
        tier="T2",
        score=15,
        skill="brew_potion",
        skill_args=(("recipe", "mundane"),),
        prereq_goals=("obtain_blaze_rod",),
        done_advancements=("minecraft:nether/brew_potion",),
        budget_s=300.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=150.0,
        stage="draft",
    ),
    GoalSpec(
        id="cure_zombie_villager",
        title="僵尸科医生（弱化并治愈僵尸村民）",
        tier="T3",
        score=60,
        skill="cure_zombie_villager",
        prereq_goals=("brew_potion",),
        done_advancements=("minecraft:story/cure_zombie_villager",),
        budget_s=480.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=320.0,
        stage="draft",
    ),

    # ---- Stage 2 Batch 2B：下界核心线（7 goals / 230 pts） ----
    GoalSpec(
        id="obtain_ancient_debris",
        title="深藏在地下（挖掘远古残骸）",
        tier="T2",
        score=60,
        skill="mine_ancient_debris",
        prereq_goals=("enter_the_nether", "prep_diamond_pickaxe"),
        done_advancements=("minecraft:nether/obtain_ancient_debris",),
        done_items=("ancient_debris>=1",),
        budget_s=420.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=220.0,
        stage="draft",
    ),
    GoalSpec(
        id="obtain_blaze_rod",
        title="与火共舞（击杀烈焰人获取烈焰棒）",
        tier="T2",
        score=30,
        skill="hunt_blaze",
        prereq_goals=("find_fortress",),
        done_advancements=("minecraft:nether/obtain_blaze_rod",),
        done_items=("blaze_rod>=1",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=180.0,
        stage="draft",
    ),
    GoalSpec(
        id="loot_bastion",
        title="战利品（掠夺猪灵堡垒宝箱）",
        tier="T2",
        score=30,
        skill="loot_bastion",
        prereq_goals=("find_bastion",),
        done_advancements=("minecraft:nether/loot_bastion",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=200.0,
        stage="draft",
    ),
    GoalSpec(
        id="charge_respawn_anchor",
        title="锚已就绪（充能重生锚）",
        tier="T2",
        score=30,
        skill="charge_respawn_anchor",
        prereq_goals=("obtain_crying_obsidian",),
        done_advancements=("minecraft:nether/charge_respawn_anchor",),
        budget_s=300.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=180.0,
        stage="draft",
    ),
    GoalSpec(
        id="obtain_crying_obsidian",
        title="谁在切洋葱（交易或获取哭泣的黑曜石）",
        tier="T2",
        score=15,
        skill="distract_piglin",
        skill_args=(("mode", "barter_crying_obsidian"),),
        prereq_goals=("enter_the_nether",),
        done_advancements=("minecraft:nether/obtain_crying_obsidian",),
        done_items=("crying_obsidian>=1",),
        budget_s=300.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=180.0,
        stage="draft",
    ),
    GoalSpec(
        id="distract_piglin",
        title="噢，闪亮亮（投掷金锭吸引猪灵）",
        tier="T2",
        score=5,
        skill="distract_piglin",
        skill_args=(("mode", "distract"),),
        prereq_goals=("enter_the_nether",),
        done_advancements=("minecraft:nether/distract_piglin",),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=120.0,
        stage="draft",
    ),
    GoalSpec(
        id="get_wither_skull",
        title="怪异的头颅（击杀凋灵骷髅获取头颅）",
        tier="T3",
        score=60,
        skill="hunt_wither_skull",
        prereq_goals=("find_fortress",),
        done_advancements=("minecraft:nether/get_wither_skull",),
        done_items=("wither_skeleton_skull>=1",),
        budget_s=540.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=360.0,
        stage="draft",
    ),

    # ---- Stage 2 Batch 2C：主世界 T2 顺手高分项（14 goals / 260 pts） ----
    GoalSpec(
        id="summon_iron_golem",
        title="招募援助（建造铁傀儡）",
        tier="T2",
        score=30,
        skill="summon_iron_golem",
        prereq_goals=("smelt_iron",),
        done_advancements=("minecraft:adventure/summon_iron_golem",),
        budget_s=300.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=180.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),
    GoalSpec(
        id="sniper_duel",
        title="狙击手的对决（远距射杀骷髅）",
        tier="T2",
        score=30,
        skill="sniper_duel",
        prereq_goals=("shoot_arrow",),
        done_advancements=("minecraft:adventure/sniper_duel",),
        budget_s=300.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=180.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),
    GoalSpec(
        id="make_sign_glow",
        title="发光发热（用荧光墨囊点亮告示牌）",
        tier="T2",
        score=30,
        skill="make_sign_glow",
        prereq_goals=("prep_wood",),
        done_advancements=("minecraft:husbandry/make_a_sign_glow",),
        budget_s=300.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=180.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),
    GoalSpec(
        id="axolotl_in_a_bucket",
        title="最萌捕手（用水桶捕获美西螈）",
        tier="T2",
        score=30,
        skill="catch_aquatic_mob",
        skill_args=(("target", "axolotl"),),
        prereq_goals=("smelt_iron",),
        done_advancements=("minecraft:husbandry/axolotl_in_a_bucket",),
        budget_s=300.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=180.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),
    GoalSpec(
        id="tactical_fishing",
        title="战术性钓鱼（用水桶捞起鱼）",
        tier="T2",
        score=15,
        skill="catch_aquatic_mob",
        skill_args=(("target", "fish"),),
        prereq_goals=("smelt_iron",),
        done_advancements=("minecraft:husbandry/tactical_fishing",),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=120.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),
    GoalSpec(
        id="tame_an_animal",
        title="永恒的伙伴（驯服一只动物）",
        tier="T2",
        score=15,
        skill="tame_animal",
        prereq_goals=("prep_wood",),
        done_advancements=("minecraft:husbandry/tame_an_animal",),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=140.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),
    GoalSpec(
        id="safely_harvest_honey",
        title="与蜂共舞（篝火下安全采蜜）",
        tier="T2",
        score=15,
        skill="harvest_honey_wax",
        skill_args=(("mode", "honey"),),
        prereq_goals=("prep_wood",),
        done_advancements=("minecraft:husbandry/safely_harvest_honey",),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=140.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),
    GoalSpec(
        id="wax_on",
        title="涂蜡（给铜块涂蜜脾）",
        tier="T2",
        score=15,
        skill="harvest_honey_wax",
        skill_args=(("mode", "wax_on"),),
        prereq_goals=("prep_wood",),
        done_advancements=("minecraft:husbandry/wax_on",),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=140.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),
    GoalSpec(
        id="wax_off",
        title="脱蜡（用斧给涂蜡铜块脱蜡）",
        tier="T2",
        score=5,
        skill="harvest_honey_wax",
        skill_args=(("mode", "wax_off"),),
        prereq_goals=("wax_on",),
        done_advancements=("minecraft:husbandry/wax_off",),
        budget_s=180.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=100.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),
    GoalSpec(
        id="avoid_vibration",
        title="潜行避声（潜行靠近幽匿感测体）",
        tier="T2",
        score=15,
        skill="biome_quick_action",
        skill_args=(("action", "avoid_vibration"),),
        done_advancements=("minecraft:adventure/avoid_vibration",),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=160.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),
    GoalSpec(
        id="brush_armadillo",
        title="轻刷犰狳（用刷子刷取犰狳鳞甲）",
        tier="T2",
        score=15,
        skill="biome_quick_action",
        skill_args=(("action", "brush_armadillo"),),
        done_advancements=("minecraft:adventure/brush_armadillo",),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=160.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),
    GoalSpec(
        id="walk_on_powder_snow_with_leather_boots",
        title="轻功雪上漂（穿皮革靴踩细雪）",
        tier="T2",
        score=15,
        skill="biome_quick_action",
        skill_args=(("action", "powder_snow"),),
        done_advancements=("minecraft:adventure/walk_on_powder_snow_with_leather_boots",),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=160.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),
    GoalSpec(
        id="place_dried_ghast_in_water",
        title="干涸恶魂入水（放置干涸恶魂块并浸水）",
        tier="T2",
        score=15,
        skill="biome_quick_action",
        skill_args=(("action", "dried_ghast"),),
        prereq_goals=("enter_the_nether",),
        done_advancements=("minecraft:husbandry/place_dried_ghast_in_water",),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=160.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),
    GoalSpec(
        id="whos_the_pillager_now",
        title="现在谁才是掠夺者（用弩击杀掠夺者）",
        tier="T2",
        score=15,
        skill="biome_quick_action",
        skill_args=(("action", "crossbow_pillager"),),
        prereq_goals=("ol_betsy",),
        done_advancements=("minecraft:adventure/whos_the_pillager_now",),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=160.0,
        stage="draft",
        editable_files=_T2_OVERWORLD_EDITABLE,
    ),

    # ---- Stage 2 Batch 2D：末地冲刺线（4 goals / 210 pts） ----
    GoalSpec(
        id="enter_the_end",
        title="结束了？（填补末地传送门框并进入末地）",
        tier="T2",
        score=60,
        skill="enter_the_end",
        prereq_goals=("follow_ender_eye",),
        done_advancements=("minecraft:story/enter_the_end",),
        budget_s=480.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=280.0,
        stage="draft",
        editable_files=_END_RUSH_EDITABLE,
    ),
    GoalSpec(
        id="enter_end_gateway",
        title="远程折跃（向末地折跃门投掷末影珍珠前往外岛）",
        tier="T2",
        score=30,
        skill="enter_end_gateway",
        prereq_goals=("enter_the_end",),
        done_advancements=("minecraft:end/enter_end_gateway",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=200.0,
        stage="draft",
        editable_files=_END_RUSH_EDITABLE,
    ),
    GoalSpec(
        id="find_end_city",
        title="在游戏尽头的城市（跨岛探索并抵达末地城）",
        tier="T3",
        score=60,
        skill="find_end_city",
        prereq_goals=("enter_end_gateway",),
        done_advancements=("minecraft:end/find_end_city",),
        budget_s=540.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=320.0,
        stage="draft",
        editable_files=_END_RUSH_EDITABLE,
    ),
    GoalSpec(
        id="levitate",
        title="这上面的风景不错（在末地城借潜影贝漂浮抬升 50 格）",
        tier="T3",
        score=60,
        skill="levitate_fifty",
        prereq_goals=("find_end_city",),
        done_advancements=("minecraft:end/levitate",),
        budget_s=480.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=280.0,
        stage="draft",
        editable_files=_END_RUSH_EDITABLE,
    ),

    # ---- Stage 2 Batch 2E：试炼密室与朝向放置线（5 goals / 120 pts） ----
    GoalSpec(
        id="under_lock_and_key",
        title="上锁的宝库（用试炼钥匙开启宝库）",
        tier="T2",
        score=30,
        skill="unlock_trial_vault",
        prereq_goals=("minecraft_trials_edition",),
        done_advancements=("minecraft:adventure/under_lock_and_key",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=200.0,
        stage="draft",
        editable_files=_T2_TRIALS_ORIENTED_EDITABLE,
    ),
    GoalSpec(
        id="who_needs_rockets",
        title="谁还需要火箭？（向脚下发射风弹弹跳 7 格以上）",
        tier="T2",
        score=30,
        skill="wind_charge_bounce",
        prereq_goals=("minecraft_trials_edition",),
        done_advancements=("minecraft:adventure/who_needs_rockets",),
        budget_s=300.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=180.0,
        stage="draft",
        editable_files=_T2_TRIALS_ORIENTED_EDITABLE,
    ),
    GoalSpec(
        id="lighten_up",
        title="点亮它（点亮氧化铜灯并刮除氧化层）",
        tier="T2",
        score=15,
        skill="scrape_lit_copper_bulb",
        prereq_goals=("minecraft_trials_edition",),
        done_advancements=("minecraft:adventure/lighten_up",),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=150.0,
        stage="draft",
        editable_files=_T2_TRIALS_ORIENTED_EDITABLE,
    ),
    GoalSpec(
        id="read_power_of_chiseled_bookshelf",
        title="知识的力量（比较器读取雕纹书架红石信号）",
        tier="T2",
        score=15,
        skill="read_chiseled_bookshelf",
        prereq_goals=("prep_wood",),
        done_advancements=("minecraft:adventure/read_power_of_chiseled_bookshelf",),
        budget_s=240.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=140.0,
        stage="draft",
        editable_files=_T2_TRIALS_ORIENTED_EDITABLE,
    ),
    GoalSpec(
        id="heart_transplanter",
        title="换心术（在苍白橡木原木间对齐放置嘎枝之心）",
        tier="T2",
        score=30,
        skill="transplant_creaking_heart",
        prereq_goals=("prep_wood",),
        done_advancements=("minecraft:adventure/heart_transplanter",),
        budget_s=300.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=200.0,
        stage="draft",
        editable_files=_T2_TRIALS_ORIENTED_EDITABLE,
    ),

    # ---- Stage 2 Batch 2F：群系与生态专项 T3 线（10 goals / 270 pts） ----
    GoalSpec(
        id="kill_mob_near_sculk_catalyst",
        title="它蔓延了（在幽匿催发体旁击杀生物）",
        tier="T3",
        score=30,
        skill="kill_near_sculk_catalyst",
        prereq_goals=("kill_a_mob",),
        done_advancements=("minecraft:adventure/kill_mob_near_sculk_catalyst",),
        budget_s=420.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=260.0,
        stage="draft",
        editable_files=_T3_BIOMES_EDITABLE,
    ),
    GoalSpec(
        id="kill_axolotl_target",
        title="友谊的治愈力量！（协助美西螈击杀猎物）",
        tier="T3",
        score=30,
        skill="assist_axolotl_hunt",
        prereq_goals=("kill_a_mob",),
        done_advancements=("minecraft:husbandry/kill_axolotl_target",),
        budget_s=420.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=260.0,
        stage="draft",
        editable_files=_T3_BIOMES_EDITABLE,
    ),
    GoalSpec(
        id="remove_wolf_armor",
        title="剪断关系（用剪刀卸下狼铠）",
        tier="T3",
        score=30,
        skill="manage_wolf_armor",
        skill_args=(("mode", "remove"),),
        prereq_goals=("tame_an_animal",),
        done_advancements=("minecraft:husbandry/remove_wolf_armor",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=220.0,
        stage="draft",
        editable_files=_T3_BIOMES_EDITABLE,
    ),
    GoalSpec(
        id="repair_wolf_armor",
        title="完好如初（用犰狳鳞甲修复狼铠）",
        tier="T3",
        score=30,
        skill="manage_wolf_armor",
        skill_args=(("mode", "repair"),),
        prereq_goals=("tame_an_animal", "brush_armadillo"),
        done_advancements=("minecraft:husbandry/repair_wolf_armor",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=220.0,
        stage="draft",
        editable_files=_T3_BIOMES_EDITABLE,
    ),
    GoalSpec(
        id="allay_deliver_item_to_player",
        title="找到一个好朋友（让悦灵递送物品）",
        tier="T3",
        score=30,
        skill="allay_deliver_item",
        prereq_goals=("prep_wood",),
        done_advancements=("minecraft:husbandry/allay_deliver_item_to_player",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=240.0,
        stage="draft",
        editable_files=_T3_BIOMES_EDITABLE,
    ),
    GoalSpec(
        id="ride_a_boat_with_a_goat",
        title="羊帆起航！（与山羊同乘一艘船）",
        tier="T3",
        score=30,
        skill="boat_with_goat",
        prereq_goals=("prep_wood",),
        done_advancements=("minecraft:husbandry/ride_a_boat_with_a_goat",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=240.0,
        stage="draft",
        editable_files=_T3_BIOMES_EDITABLE,
    ),
    GoalSpec(
        id="silk_touch_nest",
        title="举巢搬迁（用精准采集挖下蜂巢）",
        tier="T3",
        score=30,
        skill="silk_touch_bee_nest",
        prereq_goals=("enchant_item",),
        done_advancements=("minecraft:husbandry/silk_touch_nest",),
        budget_s=420.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=260.0,
        stage="draft",
        editable_files=_T3_BIOMES_EDITABLE,
    ),
    GoalSpec(
        id="tadpole_in_a_bucket",
        title="呱呱坠桶（用水桶捞起蝌蚪）",
        tier="T3",
        score=30,
        skill="catch_tadpole",
        prereq_goals=("smelt_iron",),
        done_advancements=("minecraft:husbandry/tadpole_in_a_bucket",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=220.0,
        stage="draft",
        editable_files=_T3_BIOMES_EDITABLE,
    ),
    GoalSpec(
        id="spyglass_at_ghast",
        title="那是气球吗？（用望远镜观察恶魂）",
        tier="T3",
        score=15,
        skill="spyglass_observe",
        skill_args=(("target", "ghast"),),
        prereq_goals=("enter_the_nether",),
        done_advancements=("minecraft:adventure/spyglass_at_ghast",),
        budget_s=300.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=200.0,
        stage="draft",
        editable_files=_T3_BIOMES_EDITABLE,
    ),
    GoalSpec(
        id="spyglass_at_parrot",
        title="那是鸟吗？（用望远镜观察鹦鹉）",
        tier="T3",
        score=15,
        skill="spyglass_observe",
        skill_args=(("target", "parrot"),),
        prereq_goals=("prep_wood",),
        done_advancements=("minecraft:adventure/spyglass_at_parrot",),
        budget_s=300.0,
        max_fails_per_ep=3,
        stop_loss=True,
        est_cost_s=200.0,
        stage="draft",
        editable_files=_T3_BIOMES_EDITABLE,
    ),

    # ---- Stage 3 (Wave A, T4_PREP + T4)：高分收口与巧取（2 项准备 + 7 项计分，全部 stage="draft"） ----
    GoalSpec(
        id="prep_ominous_bottle",
        title="预备不祥之瓶（清剿掠夺者前哨站队长）",
        tier="T4_PREP",
        score=0,
        skill="sweep_outpost_captain",
        prereq_goals=("smelt_iron",),
        done_items=("ominous_bottle>=1",),
        must_hold_for_children=True,
        budget_s=300.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=160.0,
        stage="draft",
        editable_files=_T4_QUICK_EDITABLE,
    ),
    GoalSpec(
        id="prep_two_cakes",
        title="预备蛋糕与音符盒（为悦灵生日做准备）",
        tier="T4_PREP",
        score=0,
        skill="allay_cake_birthday",
        skill_args=(("prep_only", True),),
        prereq_goals=("trade",),
        done_items=("cake>=2", "note_block>=1"),
        must_hold_for_children=True,
        budget_s=240.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=130.0,
        stage="draft",
        editable_files=_T4_QUICK_EDITABLE,
    ),
    GoalSpec(
        id="crafters_crafting_crafters",
        title="合成器合成合成器",
        tier="T4",
        score=15,
        skill="crafter_chain",
        prereq_goals=("smelt_iron",),
        done_advancements=("minecraft:adventure/crafters_crafting_crafters",),
        budget_s=240.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=120.0,
        stage="draft",
        editable_files=_T4_QUICK_EDITABLE,
    ),
    GoalSpec(
        id="craft_decorated_pot_using_only_sherds",
        title="慎重修复（仅用陶片合成饰纹陶罐）",
        tier="T4",
        score=30,
        skill="decorated_pot_sherds",
        prereq_goals=("minecraft_trials_edition",),
        done_advancements=("minecraft:adventure/craft_decorated_pot_using_only_sherds",),
        budget_s=300.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=180.0,
        stage="draft",
        editable_files=_T4_QUICK_EDITABLE,
    ),
    GoalSpec(
        id="play_jukebox_in_meadows",
        title="草甸之声（在草甸演奏唱片机）",
        tier="T4",
        score=30,
        skill="jukebox_meadow",
        prereq_goals=("minecraft_trials_edition", "mine_diamond"),
        done_advancements=("minecraft:adventure/play_jukebox_in_meadows",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=200.0,
        stage="draft",
        editable_files=_T4_QUICK_EDITABLE,
    ),
    GoalSpec(
        id="voluntary_exile",
        title="自我放逐（击杀袭击队长）",
        tier="T4",
        score=30,
        skill="sweep_outpost_captain",
        prereq_goals=("smelt_iron",),
        done_advancements=("minecraft:adventure/voluntary_exile",),
        budget_s=300.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=180.0,
        stage="draft",
        editable_files=_T4_QUICK_EDITABLE,
    ),
    GoalSpec(
        id="allay_deliver_cake_to_note_block",
        title="生日快乐歌（让悦灵向音符盒递送蛋糕）",
        tier="T4",
        score=30,
        skill="allay_cake_birthday",
        prereq_goals=("allay_deliver_item_to_player", "prep_two_cakes"),
        done_advancements=("minecraft:husbandry/allay_deliver_cake_to_note_block",),
        budget_s=360.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=190.0,
        stage="draft",
        editable_files=_T4_QUICK_EDITABLE,
    ),
    GoalSpec(
        id="elytra",
        title="天空即为极限（在末地船获取鞘翅）",
        tier="T4",
        score=60,
        skill="loot_end_ship_elytra",
        prereq_goals=("find_end_city",),
        done_advancements=("minecraft:end/elytra",),
        done_items=("elytra>=1",),
        budget_s=420.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=260.0,
        stage="draft",
        editable_files=_T4_END_FROGS_EDITABLE,
    ),
    GoalSpec(
        id="leash_all_frog_variants",
        title="用拴绳牵引所有青蛙变种",
        tier="T4",
        score=60,
        skill="leash_frog_variants",
        prereq_goals=("tadpole_in_a_bucket", "enter_the_end"),
        done_advancements=("minecraft:husbandry/leash_all_frog_variants",),
        budget_s=480.0,
        max_fails_per_ep=2,
        stop_loss=True,
        est_cost_s=300.0,
        stage="draft",
        editable_files=_T4_END_FROGS_EDITABLE,
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
    elif lhs.startswith("@stick_stock:"):
        target = int(lhs.split(":", 1)[1])
        cur = int(state.count("stick") >= target or can_afford_gear(state, "stick"))
        label = f"已持 {target} 木棍或可继续制作"
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
    """检查当前背包是否实际持有下游所需物资。"""
    held_items = goal.held_items or goal.done_items
    return bool(held_items) and all(check_item_expr(state, expr) for expr in held_items)


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
    if goal.must_hold_for_children and (goal.held_items or goal.done_items):
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


def normalize_goal_ids(target: Any) -> tuple[str, ...]:
    """把聚焦目标统一成去重有序的 id 元组：接受 None、"a"、"a,b" 或 ["a", "b"]。

    单目标聚焦是多目标的特例；多个目标表示「同一轮里一起追」，用来暴露只有并行时才出现的
    抢材料、共享状态与规划摇摆问题。
    """
    if not target:
        return ()
    parts = target.split(",") if isinstance(target, str) else list(target)
    out: list[str] = []
    for part in parts:
        gid = str(part).strip()
        if gid and gid not in out:
            out.append(gid)
    return tuple(out)


def resolve_active_goals(
    ledger: tuple[GoalSpec, ...] = GOALS,
    *,
    include_stages: Collection[str] = DEFAULT_CAMPAIGN_STAGES,
    target_goal: Any = None,
) -> tuple[GoalSpec, ...]:
    """解析本轮激活的目标集合：指定 `target_goal`（一个或多个，见 normalize_goal_ids）时
    自动收敛为这些目标的传递前置闭包 + 目标自身。"""
    by_id = {g.id: g for g in ledger}
    targets = normalize_goal_ids(target_goal)
    if targets:
        unknown = [gid for gid in targets if gid not in by_id]
        if unknown:
            raise KeyError(f"未知目标 id: {', '.join(unknown)}（可选: {', '.join(by_id)}）")
        needed: set[str] = set()

        def _collect(gid: str) -> None:
            if gid in needed or gid not in by_id:
                return
            needed.add(gid)
            for pid in by_id[gid].prereq_goals:
                _collect(pid)

        for gid in targets:
            _collect(gid)
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


_SHARED_SUBGRAPH_WEIGHT: float = 0.35
_SHARED_SUBGRAPH_CAP: int = 6


def rank_actionable_goals(
    state: State,
    active_goals: tuple[GoalSpec, ...],
    *,
    completed_advs: Collection[str] = (),
    target_goal: Any = None,
    ep_goal_fails: Optional[Mapping[str, int]] = None,
    ep_goal_elapsed: Optional[Mapping[str, float]] = None,
    telemetry: Optional[Mapping[str, Mapping[str, Any]]] = None,
    suspended_goals: Collection[str] = (),
) -> list[GoalPlanStep]:
    """对所有尚未达成且未触发止损的目标计算未满足前置链，按共享子图边际性价比降序返回可执行步列表。"""
    by_id = {g.id: g for g in active_goals}
    order_idx = {g.id: idx for idx, g in enumerate(active_goals)}
    target_ids = set(normalize_goal_ids(target_goal))

    def _unmet_chain(root: GoalSpec) -> Optional[list[GoalSpec]]:
        chain: list[GoalSpec] = []
        seen: set[str] = set()

        def _walk(node: GoalSpec, is_root: bool) -> bool:
            if node.id in seen:
                return True
            if not is_root and is_prereq_satisfied(node, state, completed_advs):
                return True
            if node.id in suspended_goals:
                return False
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

    # 第 1 遍：收集所有可执行、未达成、未止损的 (root_goal, chain)，并统计共享子图依赖度
    actionable_chains: list[tuple[GoalSpec, list[GoalSpec]]] = []
    node_dependents: dict[str, set[str]] = {}
    node_downstream_score: dict[str, int] = {}
    for g in active_goals:
        if is_goal_achieved(g, state, completed_advs):
            continue
        chain = _unmet_chain(g)
        if not chain:
            continue
        actionable_chains.append((g, chain))
        if g.score > 0:
            for x in chain:
                deps = node_dependents.setdefault(x.id, set())
                if g.id not in deps:
                    deps.add(g.id)
                    node_downstream_score[x.id] = node_downstream_score.get(x.id, 0) + g.score

    # 第 2 遍：保留原始 path_score / path_cost / roi，同时按共享子图分摊成本计算 marginal_roi 排序
    raw_candidates: list[tuple[tuple[int, int, float, float, int], GoalPlanStep]] = []
    for g, chain in actionable_chains:
        p_score = sum(x.score for x in chain if not is_goal_achieved(x, state, completed_advs))
        p_cost = sum(effective_goal_cost(x, telemetry) for x in chain)
        roi = p_score / max(1.0, p_cost)

        amortized_cost = 0.0
        for x in chain:
            raw_x_cost = effective_goal_cost(x, telemetry)
            share_count = len(node_dependents.get(x.id, ()))
            extra_shares = min(_SHARED_SUBGRAPH_CAP, max(0, share_count - 1))
            amortized_cost += raw_x_cost / (1.0 + _SHARED_SUBGRAPH_WEIGHT * extra_shares)

        m_roi = p_score / max(1.0, amortized_cost) if p_score > 0 else 0.0
        step_shared = max(1, len(node_dependents.get(chain[0].id, ())))

        prio = (
            1 if g.id in target_ids else 0,
            1 if p_score > 0 else 0,
            round(m_roi, 6),
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
            shared_dependents=step_shared,
            marginal_roi=round(m_roi, 4),
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
    expected_items = bottleneck.done_items
    if bottleneck.id != goal.id and bottleneck.must_hold_for_children:
        expected_items = bottleneck.held_items or expected_items
    for expr in (*bottleneck.prereq_items, *expected_items):
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
