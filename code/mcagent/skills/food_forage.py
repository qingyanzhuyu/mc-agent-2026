"""地表主动寻粮外迁原语（纯 L2，无 @register，不导入 mcagent.loop）。"""
from __future__ import annotations

import math
from collections.abc import Generator

from ..client import ApiError
from ..runtime import Replan
from .base import failure, rec
from .items import bare
from .world_memo import memo_for

_FORAGE_GROUND: tuple[str, ...] = (
    "grass_block", "dirt", "coarse_dirt", "podzol", "rooted_dirt", "moss_block", "sand",
)
_NAV_ERRS: frozenset[str] = frozenset({
    "path_not_found", "unreachable", "timeout", "move_timeout", "stalled", "out_of_reach",
})


def forage_surface_stride(ex, state, *, min_dist: float = 16.0, radius: int = 32) -> Generator[dict, None, bool]:
    """白天身处地表且周边无食物源时，主动向未访地表陆地外迁一步（16~32 格）以扩大觅食覆盖。"""
    st = state.refresh_status(ex.client, max_age_s=0) or {}
    pos = st.get("pos")
    if not isinstance(pos, (list, tuple)) or len(pos) < 3:
        return False
    px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
    if py < 56.0 or st.get("in_water") or st.get("submerged") or st.get("in_lava"):
        return False
    hostile = st.get("nearest_hostile") or {}
    if isinstance(hostile, dict):
        try:
            if float(hostile.get("dist", 99.0)) < 12.0:
                return False
        except (TypeError, ValueError):
            pass
    try:
        found = ex.client.find_blocks(types=list(_FORAGE_GROUND), radius=radius, limit=250) or {}
    except ApiError as exc:
        if exc.code in ("not_ready", "not_loaded"):
            return False
        raise
    dim = bare(st.get("dimension") or "overworld").strip().lower() or "overworld"
    memo = memo_for(state)
    visited = memo.visited_grid_cells(dimension=dim)
    cands: list[tuple[int, float, list[float]]] = []
    for blk in found.get("blocks") or ():
        bp = blk.get("pos") if isinstance(blk, dict) else None
        if not isinstance(bp, (list, tuple)) or len(bp) < 3:
            continue
        bx, by, bz = int(bp[0]), int(bp[1]), int(bp[2])
        d = math.hypot(bx + 0.5 - px, bz + 0.5 - pz)
        if d < min_dist or abs((by + 1) - py) > 8.0:
            continue
        in_vis = 1 if (bx // 32, bz // 32) in visited else 0
        cands.append((in_vis, -d, [bx + 0.5, float(by + 1), bz + 0.5]))
    if not cands:
        return False
    cands.sort(key=lambda r: (r[0], r[1]))
    for _, _, dest in cands[:2]:
        try:
            res = ex.move_to(pos=dest, rng=2, timeout=20.0)
        except TypeError:
            try:
                res = ex.move_to(pos=dest, rng=2)
            except Replan as err:
                code, _ = failure(err)
                if code not in _NAV_ERRS:
                    raise
                yield rec("move_to", pos=dest, error_code=code, reason="food_forage_stride")
                continue
        except Replan as err:
            code, _ = failure(err)
            if code not in _NAV_ERRS:
                raise
            yield rec("move_to", pos=dest, error_code=code, reason="food_forage_stride")
            continue
        yield rec("move_to", res, pos=dest, reason="food_forage_stride")
        st_after = state.refresh_status(ex.client, max_age_s=0) or {}
        ap = st_after.get("pos") or dest
        if isinstance(ap, (list, tuple)) and len(ap) >= 3:
            memo.mark_grid_visited(ap, dimension=dim)
            if math.hypot(float(ap[0]) - px, float(ap[2]) - pz) >= 6.0:
                return True
    return False
