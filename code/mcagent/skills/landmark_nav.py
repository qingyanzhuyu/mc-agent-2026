"""L2 已知地标与群系锚点导航原语（不注册、不触碰 nav.py 真机基线常量）。

供 T2/T3/T4 群系与结构技能统一调用：当原地扫描未发现目标方块或生物时，
从 `WorldResourceMemo` 取出当前维度下最近的地标或群系锚点并导航前往后再复扫。
"""
from __future__ import annotations

from collections.abc import Generator, Sequence
from typing import Any

from ..runtime import Executor, Replan, State
from .base import rec
from .items import bare
from .sightline import block_center, eye_to_block_dist
from .world_memo import memo_for


def _norm_dim(state: State) -> str:
    raw = (state.st or {}).get("dimension") or "overworld"
    dim = bare(str(raw)).strip().lower()
    if dim in ("nether", "the_nether"):
        return "the_nether"
    if dim in ("end", "the_end"):
        return "the_end"
    return dim or "overworld"


def _collect_anchor_cells(state: State, kinds: str | Sequence[str], *, dimension: str) -> list[tuple[int, int, int]]:
    memo = memo_for(state)
    reqs = (kinds,) if isinstance(kinds, str) else tuple(kinds)
    cells: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()
    query_fn = getattr(memo, "biome_or_landmark_positions", None)
    for req in reqs:
        if not isinstance(req, str) or not req.strip():
            continue
        found = (
            query_fn(req, dimension=dimension)
            if callable(query_fn)
            else memo.landmarks(req, dimension=dimension)
        )
        for cell in found:
            if cell not in seen:
                seen.add(cell)
                cells.append(cell)
    return cells


def goto_known_landmark_or_biome(
    ex: Executor,
    state: State,
    kinds: str | Sequence[str],
    *,
    rng: int = 4,
    min_dist: float = 8.0,
) -> Generator[dict, None, bool]:
    """L2 原语：若当前维度台账已记录目标地标或群系锚点，导航至最近锚点并返回 `True`。

    - 严格复用 `sightline.eye_to_block_dist` 排序与测距（AGENTS.md §9.2 #8）；
    - 若最近锚点已在 `min_dist` 格内，不发多余 `move_to`，返回 `False`；
    - 发出单次阻塞 `move_to` 后立刻 `yield` 并刷新状态（AGENTS.md §9.2 #1）。
    """
    st = state.refresh_status(ex.client, max_age_s=0)
    label_check = kinds if isinstance(kinds, str) else ",".join(str(k) for k in kinds)
    hp = (st or {}).get("health")
    hp_crit = min(6.0, float(getattr(getattr(state, "config", None), "hp_crit", 6.0) or 6.0))
    if (
        isinstance(hp, (int, float))
        and not isinstance(hp, bool)
        and float(hp) <= hp_crit
        and not any(tag in label_check.lower() for tag in ("bunker", "shelter", "camp"))
    ):
        # 残血危急（<=6.0，如刚逃离蜘蛛/苦力怕仅剩 0.9~2.0 血）严禁立即折返奔赴野外结构地标撞回怪堆（避难所/碉堡回撤除外）
        return False
    dim = _norm_dim(state)
    cells = _collect_anchor_cells(state, kinds, dimension=dim)
    if not cells:
        return False

    ranked: list[tuple[float, tuple[int, int, int]]] = []
    for cell in cells:
        try:
            d = eye_to_block_dist(st, cell)
        except Replan:
            continue
        if d > float(min_dist):
            ranked.append((d, cell))
    if not ranked:
        return False

    _, best_cell = min(ranked, key=lambda item: item[0])
    cx, cy, cz = block_center(best_cell)
    dest = [cx, float(best_cell[1]), cz]
    label = kinds if isinstance(kinds, str) else ",".join(str(k) for k in kinds)
    try:
        receipt = ex.move_to(pos=dest, rng=max(1, int(rng)), timeout=30.0, retries=0)
    except TypeError:
        receipt = ex.move_to(pos=dest, rng=max(1, int(rng)))
    yield rec("move_to", receipt, pos=dest, landmark=label)
    state.refresh_status(ex.client, max_age_s=0)
    return True


__all__ = ["goto_known_landmark_or_biome"]
