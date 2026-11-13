"""L2 战术原语：`move_to` 被拒之后的三种手动兜底。

这三段序列先在 `probe --pathfinding --manual-only` 里真机跑通（本地 1.21.11 靶场，
见 docs/06 2026-11-14 01:33），再原样搬到这里；probe 现在直接导入本文件做验收，
所以"测过的"和"上场的"是同一份代码。

每个原语都是生成器：**每发一个阻塞动作 yield 一条记录**。L3 技能用 `yield from` 组合，
probe 用 `for rec in ...` 收集证据。它们不进注册表，模型选不到。

要挖/要放哪些格由调用方给出，且**必须来自当场观察**（`find_blocks` / `get_block`），
不能是模型给的数字。原语只负责把这串格子安全地做完。
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Iterator, Optional, Sequence

from ..runtime import Replan
from .base import rec
from .items import AIR, best_pickaxe, block_name, pick_placeable

if TYPE_CHECKING:
    from ..runtime import Executor, State

Pos = Sequence[float]

#: 离待放格中心超过这个距离就先走近一步（真机实测值，改之前先重跑 probe）
PLACE_REACH = 3.5
#: 玩家水平半宽 0.3 + 半格 0.5，再留一点余量：小于它说明人和这一格水平重叠
SELF_OVERLAP = 0.81
PLAYER_HEIGHT = 1.8


def _fresh_pos(ex: "Executor", state: "State") -> list[float]:
    pos = state.refresh_status(ex.client, max_age_s=0).get("pos")
    if not pos:
        raise Replan("status 里没有坐标，无法继续手动兜底")
    return pos


def _release_sneak(ex: "Executor") -> None:
    try:
        ex.posture(sneak=False)
    except Exception:
        pass  # 收尾不能再抛：这里可能正处在抢占路径上


def breach_1x2(ex: "Executor", state: "State", blocks: Sequence[Pos]) -> Iterator[dict]:
    """换上最好的镐，按顺序挖开 `blocks`（通常是挡路的上、下两格）。已是空气的格跳过。"""
    state.refresh_inventory(ex.client)
    pick = best_pickaxe(state)
    if pick:
        yield rec("equip", ex.equip(pick, "hand"), item=pick)
    for pos in blocks:
        name = block_name(ex, pos)
        if name and name not in AIR:
            yield rec("dig", ex.dig(pos), pos=pos)


def sneak_bridge(ex: "Executor", state: "State", blocks: Sequence[Pos],
                 item: Optional[str] = None) -> Iterator[dict]:
    """潜行着从近到远铺 `blocks`（桥面格，按离人由近到远排好）。结束或被打断都会松开潜行。"""
    entered = ex.posture(sneak=True)
    try:  # 从进入潜行起就要保证能松开：第一个 yield 也可能是被切走的地方
        yield rec("posture", entered, sneak=True)
        for idx, pos in enumerate(blocks):
            cur = _fresh_pos(ex, state)
            center = [pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5]
            if idx > 0 and math.dist(cur, center) > PLACE_REACH:
                prev = blocks[idx - 1]
                edge = [prev[0] + 0.5, prev[1] + 1, prev[2] + 0.5]
                yield rec("move_to_bridge", ex.move_to(pos=edge, rng=1), pos=edge)
                ex.posture(sneak=True)
            state.refresh_inventory(ex.client)
            block = pick_placeable(state, item)
            yield rec("place", ex.place(block, pos), item=block, pos=pos)
    finally:
        _release_sneak(ex)
    yield {"op": "posture", "sneak": False}


def step_up_1x2(ex: "Executor", state: "State", blocks: Sequence[Pos],
                item: Optional[str] = None) -> Iterator[dict]:
    """按 `blocks` 的顺序在相邻两列交替垫块往上走。

    脚下那一格直接放会被服务端拒绝（`blocked_by_self`，真机已证实），所以要两列交替：
    待放格和自己重叠时，先迈到上一块刚放好的方块顶上，再回头放。已有方块的格跳过。
    """
    last: Optional[list[float]] = None
    for pos in blocks:
        cur = _fresh_pos(ex, state)
        horiz = abs(cur[0] - (pos[0] + 0.5)) < SELF_OVERLAP and abs(cur[2] - (pos[2] + 0.5)) < SELF_OVERLAP
        vert = cur[1] < pos[1] + 1.0 and cur[1] + PLAYER_HEIGHT > pos[1]
        if horiz and vert and last is not None:
            top = [last[0] + 0.5, last[1] + 1, last[2] + 0.5]
            yield rec("step_up_to", ex.move_to(pos=top, rng=1), pos=top)
        if block_name(ex, pos) in AIR:
            state.refresh_inventory(ex.client)
            block = pick_placeable(state, item)
            yield rec("place", ex.place(block, pos), item=block, pos=pos)
        last = list(pos)
