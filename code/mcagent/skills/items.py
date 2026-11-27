"""物品与方块的选择器：选哪把镐、拿什么垫脚、这一格是不是空气。

纯读取（背包快照 + `get_block` 观察），不发任何动作。L2 原语和 L3 技能共用，
免得每个技能各写一份"最好的镐"。
"""
from __future__ import annotations

import json
import os

from collections.abc import Mapping
from typing import TYPE_CHECKING, Optional

from ..runtime import Replan

if TYPE_CHECKING:
    from ..runtime import Executor, State

AIR = frozenset({"air", "cave_air", "void_air"})

#: 寻路常见失败状态码
NAV_FAIL_CODES: tuple[str, ...] = ("path_not_found", "unreachable", "move_timeout", "stalled")

#: 常见无害、不阻挡通行的地被植物与覆盖物（25种）
PASSABLE_COVER: frozenset[str] = frozenset({
    "short_grass", "tall_grass", "fern", "large_fern",
    "short_dry_grass", "tall_dry_grass", "dead_bush", "bush",
    "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
    "oxeye_daisy", "cornflower", "lily_of_the_valley", "leaf_litter",
    "pink_petals", "wildflowers", "moss_carpet", "pale_moss_carpet",
    "brown_mushroom", "red_mushroom", "glow_lichen", "vine",
})

#: 危险或非实体液体/伤害性方块（不可作为安全立足地面）
_UNSAFE_FLOOR_BLOCKS: frozenset[str] = frozenset({
    "water", "lava", "flowing_water", "flowing_lava", "bubble_column",
    "powder_snow", "cactus", "magma_block", "sweet_berry_bush",
    "fire", "soul_fire", "campfire", "soul_campfire",
    "wither_rose", "cobweb", "seagrass", "tall_seagrass",
    "kelp", "kelp_plant",
})

#: 可以拿来垫脚/铺桥的常见整块方块
PLACEABLE_BLOCKS: tuple[str, ...] = (
    "cobblestone", "dirt", "stone", "cobbled_deepslate", "netherrack",
    "oak_planks", "birch_planks", "spruce_planks", "jungle_planks",
    "acacia_planks", "dark_oak_planks", "cherry_planks", "mangrove_planks",
    "sandstone", "andesite", "diorite", "granite", "pale_oak_planks", "tuff",
)
#: 从好到差
PICKAXE_ORDER: tuple[str, ...] = (
    "netherite_pickaxe", "diamond_pickaxe", "iron_pickaxe",
    "copper_pickaxe", "stone_pickaxe", "golden_pickaxe", "wooden_pickaxe",
)
STONE_OR_BETTER_PICKS = frozenset({
    "stone_pickaxe", "copper_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe",
})
#: 下探阶梯的路点栈（起点在地表，终点在井底），供外迁技能在井底受困时沿原阶梯退回井口。
MINE_TRAILS: list[list[tuple[float, float, float]]] = []
# 轨迹原本只是进程内存全局，**进程一重启就全丢**：bot 明明刚挖过下行阶梯，
# 重启后回程就"不记得"了，于是重新挖一条。这里落盘到 code/ 下，跨重启保留。
_TRAIL_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".mine_trails.json",
)


def _load_mine_trails() -> None:
    try:
        with open(_TRAIL_STORE_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return
    trails: list[list[tuple[float, float, float]]] = []
    for trail in raw if isinstance(raw, list) else ():
        if isinstance(trail, list) and len(trail) >= 2:
            pts = [(float(p[0]), float(p[1]), float(p[2])) for p in trail
                   if isinstance(p, (list, tuple)) and len(p) >= 3]
            if len(pts) >= 2:
                trails.append(pts)
    if trails:
        MINE_TRAILS[:] = trails[-6:]


def _save_mine_trails() -> None:
    try:
        with open(_TRAIL_STORE_PATH, "w", encoding="utf-8") as fh:
            json.dump([[list(p) for p in trail] for trail in MINE_TRAILS], fh)
    except Exception:
        pass


def record_mine_trail(waypoints: list[list[float]]) -> None:
    if len(waypoints) < 2:
        return
    pts = [(float(p[0]), float(p[1]), float(p[2])) for p in waypoints if len(p) >= 3]
    if len(pts) >= 2 and pts[0][1] > pts[-1][1] + 1.5:
        MINE_TRAILS.append(pts)
        if len(MINE_TRAILS) > 6:
            del MINE_TRAILS[:-6]
        _save_mine_trails()      # 跨重启保留


_load_mine_trails()


def bare(name: object) -> str:
    """去掉 `minecraft:` 前缀。"""
    return str(name or "").removeprefix("minecraft:")


def block_name(ex: "Executor", pos) -> str:
    """观察一格方块的名字（不占动作锁）。"""
    return bare(ex.client.get_block(pos).get("name", ""))


def _extract_block_name(block: object) -> str:
    """从方块字典或名称字符串提取规整后的无前缀方块名。"""
    if isinstance(block, str):
        raw_name = block
    elif isinstance(block, Mapping):
        raw_name = block.get("name")
    else:
        return ""
    return bare(raw_name).strip()


def is_clear_space(block: object) -> bool:
    """判断方块是否为可通行的空旷空间（空气、地被植物或 <=2 层的顶层雪）。"""
    name = _extract_block_name(block)
    if not name:
        return False

    if name in AIR or name in PASSABLE_COVER:
        return True

    if name == "snow":
        props = block.get("properties") if isinstance(block, Mapping) else None
        raw_layers = props.get("layers", 1) if isinstance(props, Mapping) else 1
        if isinstance(raw_layers, bool):
            return False
        if isinstance(raw_layers, int):
            layers = raw_layers
        elif isinstance(raw_layers, str) and raw_layers.strip().isdigit():
            layers = int(raw_layers.strip())
        else:
            return False
        return 1 <= layers <= 2

    return False


def is_safe_solid_floor(block: object) -> bool:
    """判断方块是否为可安全踩踏的实体地面（排除空气、地被、液体与伤害性方块）。"""
    name = _extract_block_name(block)
    if not name or is_clear_space(block):
        return False
    return name not in _UNSAFE_FLOOR_BLOCKS


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
