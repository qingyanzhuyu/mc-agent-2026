"""A bounded descent through one observed, supported staircase column."""

from __future__ import annotations

import math
from collections.abc import Callable, Generator, Iterator, Sequence
from typing import Any

from ..runtime import Executor, Replan, State
from .base import rec
from .items import AIR, PICKAXE_ORDER, STONE_OR_BETTER_PICKS, bare, block_name

PEEL_SURFACE = frozenset(("grass_block", "dirt", "coarse_dirt", "podzol", "leaf_litter"))
SOLID_SUPPORT = frozenset((
    "stone", "cobblestone", "deepslate", "cobbled_deepslate",
    "dirt", "coarse_dirt", "grass_block", "andesite", "diorite", "granite", "tuff",
    "calcite", "dripstone_block", "clay",
    "coal_ore", "deepslate_coal_ore", "copper_ore", "deepslate_copper_ore",
    "lapis_ore", "deepslate_lapis_ore", "gold_ore", "deepslate_gold_ore",
    "redstone_ore", "deepslate_redstone_ore",
    "iron_ore", "deepslate_iron_ore", "raw_iron_block", "raw_copper_block",
))


class MineStepUnsafe(Replan):
    """The inspected column changed. The caller may observe an alternate route."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def under_feet(cur: Sequence[float], pos: Sequence[int]) -> bool:
    return (math.floor(cur[0]) == pos[0] and math.floor(cur[2]) == pos[2]
            and pos[1] < cur[1])


def safe_mine_column(ex: Executor, pos: Sequence[int], *,
                     solid: frozenset[str] = SOLID_SUPPORT) -> bool:
    """Check support, roof and the three cells that would be cut."""
    x, y, z = pos
    return (block_name(ex, [x, y - 1, z]) in solid
            and block_name(ex, [x, y + 3, z]) in AIR | solid
            and all(block_name(ex, [x, y + dy, z]) in AIR | solid for dy in (2, 1, 0)))


def _best_allowed_pick(state: State, allowed: frozenset[str]) -> str | None:
    return next((item for item in PICKAXE_ORDER if item in allowed and state.count(item) > 0), None)


def prepare_mining_pick(ex: Executor, state: State, *,
                        allowed: frozenset[str] = STONE_OR_BETTER_PICKS) -> Iterator[dict]:
    inv = state.refresh_inventory(ex.client)
    pick = _best_allowed_pick(state, allowed)
    if pick is None:
        raise Replan("挖掘前检查背包缺石镐或更好的镐" if allowed == STONE_OR_BETTER_PICKS
                     else "挖掘前缺所需等级的镐")
    if bare((inv.get("held", state.st.get("held")) or {}).get("name")) != pick:
        yield rec("equip", ex.equip(pick, "hand"), item=pick)
        inv = state.refresh_inventory(ex.client)
        if not state.count(pick) or (
                "held" in inv and bare((inv.get("held") or {}).get("name")) != pick):
            raise Replan(f"挖掘前装备 {pick} 后手持未确认")


def mine_step(ex: Executor, state: State, pos: Sequence[int], *,
              allowed_picks: frozenset[str] = STONE_OR_BETTER_PICKS,
              solid: frozenset[str] = SOLID_SUPPORT,
              after_dig: Callable[[], Generator[dict, None, Any]] | None = None,
              ) -> Generator[dict, None, tuple[list[float] | None, Any]]:
    """Descend one column, or pause before moving when after_dig returns evidence."""
    if len(pos) != 3 or any(type(axis) is not int for axis in pos):
        raise Replan("阶梯落点需要三个整数坐标")
    x, y, z = pos
    start = state.refresh_status(ex.client, max_age_s=0)
    cur = start.get("pos") or state.pos
    cx, cy, cz = map(math.floor, cur)
    if start.get("alive") is False:
        raise Replan("探矿阶梯开工前角色未存活")
    if y != cy - 1 or abs(x - cx) + abs(z - cz) != 1:
        raise Replan("阶梯落点须位于现场站位相邻方向且低一格")
    if not safe_mine_column(ex, pos, solid=solid):
        raise MineStepUnsafe("cut", "探矿阶梯站位或承重/净空变化，停止挖掘以保留支撑")

    for dy in (2, 1, 0):
        target = [x, y + dy, z]
        if block_name(ex, target) in AIR:
            continue
        yield from prepare_mining_pick(ex, state, allowed=allowed_picks)
        current = state.refresh_status(ex.client, max_age_s=0)
        cur = current.get("pos") or state.pos
        if (under_feet(cur, target) or tuple(map(math.floor, cur)) != (cx, cy, cz)
                or not _same_world(start, current)):
            raise Replan("探矿阶梯站位或承重/净空变化，停止挖掘以保留支撑")
        if not safe_mine_column(ex, pos, solid=solid):
            raise MineStepUnsafe("cut", "探矿阶梯站位或承重/净空变化，停止挖掘以保留支撑")
        name = block_name(ex, target)
        if name in AIR:
            continue
        if name not in solid:
            raise MineStepUnsafe("cut", "探矿阶梯目标方块变化，停止挖掘以保留支撑")
        result = ex.dig(target, collect=name not in PEEL_SURFACE)
        yield rec("dig", result, pos=target)
        state.refresh_inventory(ex.client)
        if result.get("warning") or _best_allowed_pick(state, allowed_picks) is None:
            raise Replan("探矿工具告警或已无所需镐，停止开凿")
        if after_dig is not None:
            observation = yield from after_dig()
            if observation is not None:
                return None, observation

    if not safe_mine_column(ex, pos, solid=solid) or any(
            block_name(ex, [x, y + dy, z]) not in AIR for dy in (2, 1, 0)):
        raise MineStepUnsafe("move", "探矿阶梯承重/净空变化，停止下行")
    current = state.refresh_status(ex.client, max_age_s=0)
    cur = current.get("pos") or state.pos
    if tuple(map(math.floor, cur)) != (cx, cy, cz) or not _same_world(start, current):
        raise Replan("探矿阶梯站位变化，停止下行")
    dest = [x + 0.5, float(y), z + 0.5]
    result = ex.move_to(pos=dest, rng=1)
    yield rec("move_to", result, pos=dest)
    current = state.refresh_status(ex.client, max_age_s=0)
    cur = current.get("pos") or state.pos
    if (tuple(map(math.floor, cur)) != tuple(pos) or not _same_world(start, current)
            or result.get("dimension_changed")):
        raise Replan("探矿阶梯未确认到达落脚点，停止下行")
    return dest, None


def _same_world(start: dict, current: dict) -> bool:
    return current.get("alive") is not False and start.get("dimension") == current.get("dimension")
