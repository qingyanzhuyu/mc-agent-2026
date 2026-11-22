"""L2 目标接近与视线清理原语：`select_column_base`、`settle_dry_landing`、`clear_line_of_sight`、`reach_and_act`。

统一复用 `skills.sightline` 的眼高与方块中心几何（绝不使用脚底到角点距离），
并通过 `skills.voxel_cache` 共享体素观测与站位级不可达黑名单；每发一个阻塞动作严格 `yield rec(...)` 一次。
"""
from __future__ import annotations

import math
import time
from collections.abc import Collection, Generator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..client import ApiError
from ..runtime import Replan
from . import nav
from .base import failure, rec
from .items import AIR, NAV_FAIL_CODES, PASSABLE_COVER, PICKAXE_ORDER, bare
from .sightline import (
    DIG_EYE_REACH, block_cell, block_center, can_reach_block, can_see_block,
    eye_pos, eye_to_block_dist, feet_pos, voxel_line_cells,
)
from .voxel_cache import voxel_cache_for

if TYPE_CHECKING:
    from ..runtime import Executor, State

Pos = Sequence[float]
_LAVA_BLOCKS = frozenset({"lava", "flowing_lava"})
_FORBIDDEN_LOS_BLOCKS = _LAVA_BLOCKS | frozenset({
    "water", "flowing_water", "bubble_column", "bedrock", "obsidian", "crying_obsidian",
    "reinforced_deepslate", "fire", "soul_fire", "tnt", "crafting_table", "furnace",
    "blast_furnace", "smoker", "chest", "trapped_chest", "ender_chest", "barrel",
    "enchanting_table", "anvil", "chipped_anvil", "damaged_anvil", "brewing_stand",
    "beacon", "lodestone", "spawner", "trial_spawner", "vault", "end_portal_frame",
    "nether_portal", "end_portal", "end_gateway",
})
_SOFT_CLEAR_BLOCKS = PASSABLE_COVER | frozenset({
    "snow", "snow_block", "powder_snow", "cobweb", "dirt", "grass_block", "coarse_dirt",
    "rooted_dirt", "podzol", "mycelium", "mud", "clay", "gravel", "sand", "red_sand",
    "soul_sand", "soul_soil", "moss_block", "pale_moss_block", "farmland", "dirt_path",
    "vine", "glow_lichen", "leaf_litter",
})
_STONE_CLEAR_BLOCKS = frozenset({
    "stone", "cobblestone", "deepslate", "cobbled_deepslate", "netherrack", "andesite",
    "diorite", "granite", "tuff", "calcite", "sandstone", "red_sandstone",
    "blackstone", "basalt", "smooth_basalt",
})


def _dim_of(source: Any) -> str:
    dim = source.get("dimension") if isinstance(source, Mapping) else (getattr(source, "st", None) or {}).get("dimension")
    return str(dim or "overworld").strip() or "overworld"


def _is_preemption(exc: BaseException) -> bool:
    code, _ = failure(exc)
    return code in ("replan", "interrupted") or "抢占" in str(exc)


def _is_protected(protected_fn: Optional[Callable[[list[int]], bool]], cell: Sequence[int]) -> bool:
    if protected_fn is None:
        return False
    try:
        return bool(protected_fn(list(cell)))
    except Exception:
        return False


def _read_cached_block(client: Any, cache: Any, pos: Sequence[int], *, dimension: str) -> Optional[dict]:
    try:
        blk = cache.get_block(client, list(pos), dimension=dimension)
    except ApiError as exc:
        if exc.code in ("not_loaded", "not_ready"):
            return None
        raise
    return blk if isinstance(blk, dict) else None


def _is_dry(st: Mapping[str, Any]) -> bool:
    return (
        st.get("alive") is not False and not st.get("in_water")
        and not st.get("in_lava") and not st.get("on_fire") and not st.get("in_vehicle")
    )


def _holding_pickaxe(ex: "Executor", state: "State", st: Mapping[str, Any]) -> bool:
    def _held_name(src: Any) -> str:
        held = src.get("held") if isinstance(src, Mapping) else None
        if isinstance(held, Mapping):
            return bare(held.get("name")).strip()
        return bare(held).strip() if isinstance(held, str) else ""
    return _held_name(st) in PICKAXE_ORDER or _held_name(state.refresh_inventory(ex.client)) in PICKAXE_ORDER


def _can_clear_block(
    ex: "Executor", state: "State", st: Mapping[str, Any], cache: Any, cell: tuple[int, int, int],
    blk: dict, *, reach: float, dimension: str, protected_fn: Optional[Callable[[list[int]], bool]],
) -> bool:
    if eye_to_block_dist(st, cell) > reach or _is_protected(protected_fn, cell):
        return False
    fx, fy, fz = feet_pos(st)
    cx, cy, cz = cell
    if (cx == math.floor(fx) and cz == math.floor(fz) and cy < fy) or blk.get("diggable") is False:
        return False
    name = bare(blk.get("name")).strip()
    if not name or name in _FORBIDDEN_LOS_BLOCKS or name in ("bed", "shulker_box") or name.endswith(("_bed", "_shulker_box")):
        return False
    above = _read_cached_block(ex.client, cache, (cx, cy + 1, cz), dimension=dimension)
    if above is None or bare(above.get("name")).strip() in _LAVA_BLOCKS:
        return False
    if name in _SOFT_CLEAR_BLOCKS or (name.endswith("_leaves") and len(name) > len("_leaves")):
        return True
    return name in _STONE_CLEAR_BLOCKS and _holding_pickaxe(ex, state, st)


def select_column_base(
    client: Any, state: "State", target_pos: Pos,
    *, match_names: Optional[str | Collection[str]] = None, max_drop: int = 8,
) -> list[int]:
    """从 `target_pos` 向下探测同柱匹配方块，返回最低匹配格 `[bx, by, bz]`（防树冠绕行）。"""
    bx, by, bz = block_cell(target_pos)
    dim, cache = _dim_of(state), voxel_cache_for(state)
    if isinstance(match_names, str):
        allowed = {bare(match_names).strip()} - {""}
    elif match_names is not None:
        allowed = {bare(n).strip() for n in match_names if bare(n).strip()}
    else:
        allowed = set()
    if not allowed:
        head = _read_cached_block(client, cache, (bx, by, bz), dimension=dim)
        head_name = bare(head.get("name")).strip() if head else ""
        if not head_name or head_name in AIR:
            return [bx, by, bz]
        allowed = {head_name}
    lowest_y = by
    for dy in range(1, max(0, int(max_drop)) + 1):
        blk = _read_cached_block(client, cache, (bx, by - dy, bz), dimension=dim)
        if blk is None or bare(blk.get("name")).strip() not in allowed:
            break
        lowest_y = by - dy
    return [bx, lowest_y, bz]


def settle_dry_landing(
    ex: "Executor", state: "State",
    *, delays: Sequence[float] = (0.12, 0.16, 0.22), sleep_fn: Callable[[float], Any] = time.sleep,
) -> dict:
    """干地未落地时按 `delays` 短轮询等待 `on_ground=True`；已落地或涉水/遇险时零休眠返回。"""
    st = state.refresh_status(ex.client, max_age_s=0)
    if st.get("on_ground") is True or not _is_dry(st):
        return st
    for delay in delays:
        sleep_fn(delay)
        st = state.refresh_status(ex.client, max_age_s=0)
        if st.get("on_ground") is True or not _is_dry(st):
            break
    return st


def clear_line_of_sight(
    ex: "Executor", state: "State", target_pos: Pos,
    *, max_clear: int = 2, reach: float = DIG_EYE_REACH, protected_fn: Optional[Callable[[list[int]], bool]] = None,
) -> Generator[dict, None, bool]:
    """清理眼睛到 `target_pos` 中心连线上最多 `max_clear` 个安全遮挡格，返回最终是否可见。"""
    target = block_cell(target_pos)
    st = state.refresh_status(ex.client, max_age_s=0)
    if can_see_block(ex, st, target, face_fallback=True):
        return True
    cache = voxel_cache_for(state)
    for _ in range(max(0, int(max_clear))):
        dim = _dim_of(st)
        blocker: Optional[tuple[tuple[int, int, int], dict]] = None
        for cell in voxel_line_cells(eye_pos(st), block_center(target)):
            if cell == target:
                break
            blk = _read_cached_block(ex.client, cache, cell, dimension=dim)
            name = bare(blk.get("name")).strip() if blk else ""
            if not name:
                return False
            if name not in AIR:
                blocker = (cell, blk)
                break
        if blocker is None:
            break
        cell, blk = blocker
        if not _can_clear_block(ex, state, st, cache, cell, blk, reach=reach, dimension=dim, protected_fn=protected_fn):
            return False
        try:
            res = ex.dig(list(cell))
        except Replan as exc:
            if _is_preemption(exc):
                raise
            code, _ = failure(exc)
            yield rec("dig", pos=list(cell), reason="clear_los", error_code=code)
            return False
        cache.invalidate(cell, dimension=dim)
        yield rec("dig", res, pos=list(cell), reason="clear_los")
        st = state.refresh_status(ex.client, max_age_s=0)
        if can_see_block(ex, st, target, face_fallback=True):
            return True
    return can_see_block(ex, st, target, face_fallback=True)


def reach_and_act(
    ex: "Executor", state: "State", target_pos: Pos, *,
    skill_name: str = "", reach: float = DIG_EYE_REACH, move_rng: int = 3, move_timeout: float = 30.0,
    max_clear: int = 2, allow_column_base: bool = False, match_names: Optional[str | Collection[str]] = None,
    protected_fn: Optional[Callable[[list[int]], bool]] = None, settle_dry: bool = False,
) -> Generator[dict, None, list[int]]:
    """接近并确认眼距与视线可达 `target_pos`，返回最终目标格 `[bx, by, bz]`。"""
    st = state.refresh_status(ex.client, max_age_s=0)
    cache = voxel_cache_for(state)
    stance_cell = block_cell(st.get("pos"))
    cache.clear_stance_blacklist_if_moved(st.get("pos"))
    resolved = list(block_cell(target_pos))
    if allow_column_base:
        resolved = select_column_base(ex.client, state, resolved, match_names=match_names)
    dim = _dim_of(st)
    if skill_name and cache.is_unreachable(skill_name, resolved, dimension=dim, stance_cell=stance_cell):
        raise Replan(f"{skill_name} 目标 {resolved} 在不可达黑名单中")

    if not can_reach_block(ex, st, resolved, reach=reach):
        try:
            try:
                res = ex.move_to(pos=resolved, rng=move_rng, timeout=move_timeout, retries=0)
            except TypeError:
                res = ex.move_to(pos=resolved, rng=move_rng)
            yield rec("move_to", res, pos=resolved)
        except Replan as exc:
            if _is_preemption(exc):
                raise
            code, _ = failure(exc)
            if code not in NAV_FAIL_CODES and code != "timeout":
                raise
            yield rec("move_to", pos=resolved, error_code=code)
            if code == "stalled":
                yield from nav.recover_move(ex, state, dest=resolved, protected_fn=protected_fn)
            else:
                st_after = state.refresh_status(ex.client, max_age_s=0)
                if eye_to_block_dist(st_after, resolved) > reach + 0.5:
                    if skill_name:
                        cache.mark_unreachable(
                            skill_name, resolved, dimension=_dim_of(st_after), stance_cell=block_cell(st_after.get("pos")),
                        )
                    raise Replan(f"接近目标 {resolved} 失败 ({code})") from exc

    st = settle_dry_landing(ex, state) if settle_dry else state.refresh_status(ex.client, max_age_s=0)
    if not can_reach_block(ex, st, resolved, reach=reach) and max_clear > 0 and eye_to_block_dist(st, resolved) <= reach + 0.5:
        yield from clear_line_of_sight(ex, state, resolved, max_clear=max_clear, reach=reach, protected_fn=protected_fn)
        st = state.refresh_status(ex.client, max_age_s=0)

    if not can_reach_block(ex, st, resolved, reach=reach):
        if skill_name:
            cache.mark_unreachable(skill_name, resolved, dimension=_dim_of(st), stance_cell=block_cell(st.get("pos")))
        raise Replan(f"无法触及目标 {resolved}")
    return resolved
