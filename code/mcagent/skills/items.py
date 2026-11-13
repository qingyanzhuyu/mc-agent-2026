"""物品与方块的选择器：选哪把镐、拿什么垫脚、这一格是不是空气。

纯读取（背包快照 + `get_block` 观察），不发任何动作。L2 原语和 L3 技能共用，
免得每个技能各写一份"最好的镐"。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..runtime import Replan

if TYPE_CHECKING:
    from ..runtime import Executor, State

AIR = frozenset({"air", "cave_air", "void_air"})

#: 可以拿来垫脚/铺桥的常见整块方块
PLACEABLE_BLOCKS: tuple[str, ...] = (
    "cobblestone", "dirt", "stone", "cobbled_deepslate", "netherrack",
    "oak_planks", "birch_planks", "spruce_planks", "jungle_planks",
    "acacia_planks", "dark_oak_planks", "cherry_planks", "mangrove_planks",
    "sandstone", "andesite", "diorite", "granite",
)
#: 从好到差
PICKAXE_ORDER: tuple[str, ...] = (
    "netherite_pickaxe", "diamond_pickaxe", "iron_pickaxe",
    "stone_pickaxe", "golden_pickaxe", "wooden_pickaxe",
)


def bare(name: object) -> str:
    """去掉 `minecraft:` 前缀。"""
    return str(name or "").removeprefix("minecraft:")


def block_name(ex: "Executor", pos) -> str:
    """观察一格方块的名字（不占动作锁）。"""
    return bare(ex.client.get_block(pos).get("name", ""))


def best_pickaxe(state: "State") -> Optional[str]:
    for p in PICKAXE_ORDER:
        if state.count(p) >= 1:
            return p
    return None


def total_placeable(state: "State", preferred: Optional[str] = None) -> int:
    if preferred:
        return state.count(preferred)
    return sum(state.count(b) for b in PLACEABLE_BLOCKS)


def pick_placeable(state: "State", preferred: Optional[str] = None) -> str:
    """挑一种垫块：指定了就用指定的，否则用背包里最多的那种。没有就 Replan。"""
    if preferred:
        if state.count(preferred) >= 1:
            return preferred
        raise Replan(f"缺少指定垫块 {preferred}")
    best_name, best_cnt = "", 0
    for b in PLACEABLE_BLOCKS:
        cnt = state.count(b)
        if cnt > best_cnt:
            best_name, best_cnt = b, cnt
    if best_cnt < 1:
        raise Replan("背包没有可放置的方块")
    return best_name
