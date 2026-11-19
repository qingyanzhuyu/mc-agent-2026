"""水体/岩浆/火焰/敌对生物紧急脱困原语（L2）。

所有阻塞动作均通过 `Executor` 发出，遵守"一次动作后立即 yield 一条记录"契约；
只读观察经 `voxel_cache_for(state)` / `ex.client.find_blocks` 完成。安全抢占 `Replan` 立即原样上抛。
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Callable, Generator, Optional

from ..client import ApiError
from ..runtime import Replan
from .base import failure, rec
from .items import AIR, NAV_FAIL_CODES, bare, is_clear_space, is_safe_solid_floor, pick_placeable, total_placeable
from .sightline import DIG_EYE_REACH, block_cell, eye_to_block_dist
from .voxel_cache import voxel_cache_for

if TYPE_CHECKING:
    from ..runtime import Executor, State

Cell = tuple[int, int, int]
_WATER_BLOCKS = frozenset({"water", "flowing_water", "bubble_column"})
_FLUID_OR_FIRE = _WATER_BLOCKS | {"lava", "flowing_lava", "fire", "soul_fire"}
_SOFT_OVERHANG = frozenset({
    "dirt", "grass_block", "coarse_dirt", "rooted_dirt",
    "sand", "red_sand", "clay", "mud", "gravel", "snow", "snow_block",
})
_CARDINALS: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
_SWIM_YAWS: tuple[float, ...] = (0.0, math.pi / 2, math.pi, -math.pi / 2)
_SHORE_OFFSETS: tuple[tuple[int, int], ...] = tuple(
    sorted(
        ((dx, dz) for dx in range(-6, 7) for dz in range(-6, 7) if (dx, dz) != (0, 0)),
        key=lambda d: (max(abs(d[0]), abs(d[1])), d[0] * d[0] + d[1] * d[1]),
    )
)


def _is_preemption(err: BaseException) -> bool:
    code, _ = failure(err)
    return code in ("replan", "interrupted") or "抢占" in str(err)


def _is_soft_overhang(name: str) -> bool:
    return name in _SOFT_OVERHANG or (name.endswith("_leaves") and len(name) > len("_leaves"))


def _is_water_cell(blk: Optional[dict]) -> bool:
    if not isinstance(blk, dict):
        return False
    return bare(blk.get("name")) in _WATER_BLOCKS or (blk.get("properties") or {}).get("waterlogged") in (True, "true")


def _refresh_status(ex: "Executor", state: "State") -> dict:
    return state.refresh_status(ex.client, max_age_s=0) or {}


def _read_block(cache: Any, client: Any, cell: Cell, dim: str) -> Optional[dict]:
    try:
        blk = cache.get_block(client, list(cell), dimension=dim)
    except ApiError as err:
        if err.code in ("not_loaded", "not_ready"):
            return None
        raise
    return blk if isinstance(blk, dict) else None


def _is_dry_standable(cache: Any, client: Any, cell: Cell, dim: str) -> bool:
    sx, sy, sz = cell
    below = _read_block(cache, client, (sx, sy - 1, sz), dim)
    body = _read_block(cache, client, cell, dim)
    above = _read_block(cache, client, (sx, sy + 1, sz), dim)
    if not (below and body and above) or _is_water_cell(body) or _is_water_cell(above):
        return False
    return is_safe_solid_floor(below) and is_clear_space(body) and is_clear_space(above)


def _exec_step(
    op: str, call: Callable[[], dict], *, on_ok: Optional[Callable[[], None]] = None, **fields: Any,
) -> Generator[dict, None, bool]:
    try:
        res = call()
        if on_ok is not None:
            on_ok()
        yield rec(op, res, **fields)
        return True
    except Replan as err:
        if _is_preemption(err):
            raise
        yield rec(op, error_code=failure(err)[0], **fields)
        return False


def _move_to_bounded(ex: "Executor", target_pos: list[float], *, rng: float = 1, timeout: float = 15.0) -> Generator[dict, None, bool]:
    try:
        try:
            res = ex.move_to(pos=target_pos, rng=rng, timeout=timeout, retries=0)
        except TypeError:
            res = ex.move_to(pos=target_pos, rng=rng)
        yield rec("move_to", res, pos=target_pos)
        return True
    except Replan as err:
        if _is_preemption(err):
            raise
        code, _ = failure(err)
        if code in NAV_FAIL_CODES or code == "timeout":
            yield rec("move_to", pos=target_pos, error_code=code)
            return False
        raise


def _control_step(ex: "Executor", **kwargs: Any) -> Generator[dict, None, tuple[bool, str]]:
    try:
        res = ex.control(**kwargs)
        yield rec("control", res, **{k: v for k, v in kwargs.items() if k in ("yaw", "ms")})
        return True, ""
    except AttributeError:
        yield rec("control", error_code="unsupported_api")
        return False, "unsupported_api"
    except Replan as err:
        if _is_preemption(err):
            raise
        code, _ = failure(err)
        yield rec("control", error_code=code)
        return False, code


def _find_soft_overhang(ex: "Executor", st: dict, cache: Any, dim: str, tried_digs: set[Cell]) -> Optional[Cell]:
    pos = st.get("pos")
    if not pos or len(pos) < 3:
        return None
    fx, fy, fz = block_cell(pos)
    scan_top = fy + 4 if st.get("submerged") else fy + 2
    y0: Optional[int] = None
    for y in range(fy + 1, scan_top + 1):
        blk = _read_block(cache, ex.client, (fx, y, fz), dim)
        if blk is None:
            return None
        if bare(blk.get("name")) not in (_WATER_BLOCKS | AIR):
            y0 = y
            break
    if y0 is None:
        return None
    lowest = (fx, y0, fz)
    if lowest in tried_digs or eye_to_block_dist(st, lowest) > DIG_EYE_REACH:
        return None
    count = 0
    for k in range(4):
        blk = _read_block(cache, ex.client, (fx, y0 + k, fz), dim)
        if blk is None:
            return None
        name = bare(blk.get("name"))
        if k < 3 and _is_soft_overhang(name) and blk.get("diggable") is not False:
            count += 1
            continue
        return lowest if (count >= 1 and name in AIR) else None
    return None


def _find_dry_shore(ex: "Executor", st: dict, cache: Any, dim: str, tried_shores: set[Cell]) -> Optional[Cell]:
    pos = st.get("pos")
    if not pos or len(pos) < 3:
        return None
    fx, fy, fz = block_cell(pos)
    for dx, dz in _SHORE_OFFSETS:
        sx, sz = fx + dx, fz + dz
        for dy in (0, 1, 2, -1):
            cand = (sx, fy + dy, sz)
            if cand not in tried_shores and _is_dry_standable(cache, ex.client, cand, dim):
                return cand
    return None


def _find_step_cell(ex: "Executor", st: dict, cache: Any, dim: str, tried_places: set[Cell]) -> Optional[Cell]:
    pos = st.get("pos")
    if not pos or len(pos) < 3:
        return None
    fx, fy, fz = block_cell(pos)
    self_cells = {(fx, fy, fz), (fx, fy + 1, fz)}
    for sy in (fy, fy + 1):
        for dx, dz in _CARDINALS:
            cand = (fx + dx, sy, fz + dz)
            if cand in self_cells or cand in tried_places:
                continue
            blk = _read_block(cache, ex.client, cand, dim)
            head = _read_block(cache, ex.client, (cand[0], sy + 1, cand[2]), dim)
            if not (blk and head):
                continue
            if bare(blk.get("name")) not in (AIR | _WATER_BLOCKS) or bare(head.get("name")) not in (AIR | _WATER_BLOCKS):
                continue
            for ox, oy, oz in ((0, -1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (0, 1, 0)):
                nb = _read_block(cache, ex.client, (cand[0] + ox, sy + oy, cand[2] + oz), dim)
                nname = bare(nb.get("name")) if nb else ""
                if nname and nname not in AIR and nname not in _FLUID_OR_FIRE:
                    return cand
    return None


def escape_water_to_shore(ex: "Executor", state: "State", *, max_steps: int = 10) -> Generator[dict, None, bool]:
    """水中/水下受困脱困：软顶破拆 -> 确认干岸移动 -> 浅盆垫脚 -> 主动上浮游泳。"""
    st = _refresh_status(ex, state)
    if not st.get("in_water") and not st.get("submerged"):
        return True

    cache = voxel_cache_for(state)
    tried_shores, tried_digs, tried_places = set(), set(), set()
    swim_idx, control_unsupported = 0, False

    for _ in range(max(0, int(max_steps))):
        dim = str(st.get("dimension") or "overworld")
        if (dig_cell := _find_soft_overhang(ex, st, cache, dim, tried_digs)) is not None:
            tried_digs.add(dig_cell)
            dig_pos = list(dig_cell)
            yield from _exec_step(
                "dig", lambda: ex.dig(dig_pos), on_ok=lambda: cache.invalidate(dig_pos, dimension=dim), pos=dig_pos,
            )
        elif (shore := _find_dry_shore(ex, st, cache, dim, tried_shores)) is not None:
            tried_shores.add(shore)
            yield from _move_to_bounded(ex, [shore[0] + 0.5, float(shore[1]), shore[2] + 0.5], rng=1, timeout=15.0)
        else:
            state.refresh_inventory(ex.client)
            step_cell = _find_step_cell(ex, st, cache, dim, tried_places) if total_placeable(state) >= 1 else None
            if step_cell is not None:
                tried_places.add(step_cell)
                step_pos = list(step_cell)
                block = pick_placeable(state)
                yield from _exec_step(
                    "place",
                    lambda: ex.place(block, pos=step_pos),
                    on_ok=lambda: cache.invalidate(step_pos, dimension=dim),
                    item=block,
                    pos=step_pos,
                )
            elif control_unsupported:
                break
            else:
                yaw = _SWIM_YAWS[swim_idx % len(_SWIM_YAWS)]
                swim_idx += 1
                ok, err_code = yield from _control_step(ex, forward=True, jump=True, yaw=yaw, ms=600)
                if not ok and err_code == "unsupported_api":
                    control_unsupported = True

        st = _refresh_status(ex, state)
        if not st.get("in_water") and not st.get("submerged"):
            return True

    st = _refresh_status(ex, state)
    return not st.get("in_water") and not st.get("submerged")


def _extract_water_pos(found: Any) -> Optional[list[float]]:
    blocks = found.get("blocks") if isinstance(found, dict) else None
    if isinstance(blocks, dict):
        pos = (blocks.get("water") or {}).get("nearest")
        return [pos[0] + 0.5, float(pos[1]), pos[2] + 0.5] if (pos and len(pos) >= 3) else None
    if isinstance(blocks, list):
        for blk in blocks:
            pos = blk.get("pos") if isinstance(blk, dict) else None
            if pos and len(pos) >= 3:
                return [pos[0] + 0.5, float(pos[1]), pos[2] + 0.5]
    return None


def _douse_or_escape_lava(ex: "Executor", state: "State") -> Generator[dict, None, bool]:
    st = _refresh_status(ex, state)
    if not st.get("on_fire") and not st.get("in_lava"):
        return True

    state.refresh_inventory(ex.client)
    if state.count("water_bucket") >= 1:
        equip_ok = True
        if bare((st.get("held") or {}).get("name")) != "water_bucket":
            equip_ok = yield from _exec_step(
                "equip", lambda: ex.equip("water_bucket", "hand"), item="water_bucket", slot="hand",
            )
        if equip_ok:
            yield from _exec_step("use_item", lambda: ex.use_item("water_bucket"), item="water_bucket")
            st = _refresh_status(ex, state)
            if not st.get("on_fire") and not st.get("in_lava"):
                return True

    if st.get("on_fire") and not st.get("in_lava"):
        try:
            water_pos = _extract_water_pos(ex.client.find_blocks(types=["water"], radius=8, limit=6))
        except (ApiError, AttributeError):
            water_pos = None
        if water_pos is not None:
            yield from _move_to_bounded(ex, water_pos, rng=1, timeout=12.0)
            st = _refresh_status(ex, state)
            if not st.get("on_fire") and not st.get("in_lava"):
                return True

    dim = str(st.get("dimension") or "overworld")
    dry = _find_dry_shore(ex, st, voxel_cache_for(state), dim, set())
    moved = (
        (yield from _move_to_bounded(ex, [dry[0] + 0.5, float(dry[1]), dry[2] + 0.5], rng=1, timeout=12.0))
        if dry is not None
        else False
    )
    if not moved:
        yield from _control_step(ex, back=True, jump=True, ms=600)

    st = _refresh_status(ex, state)
    return not st.get("on_fire") and not st.get("in_lava")


def _flee_hostile(ex: "Executor", state: "State") -> Generator[dict, None, bool]:
    st = _refresh_status(ex, state)
    pos = st.get("pos")
    if not pos or len(pos) < 3:
        return False
    px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
    dim = str(st.get("dimension") or "overworld")
    cache = voxel_cache_for(state)

    hostile = st.get("nearest_hostile") or {}
    h_pos = hostile.get("pos") if isinstance(hostile, dict) else None
    ux, uz = 1.0, 0.0
    if isinstance(h_pos, (list, tuple)) and len(h_pos) >= 3:
        dx, dz = px - float(h_pos[0]), pz - float(h_pos[2])
        if (norm := math.hypot(dx, dz)) >= 1e-6:
            ux, uz = dx / norm, dz / norm
    else:
        fx, fy, fz = block_cell(pos)
        for dx, dz in _CARDINALS:
            if _is_dry_standable(cache, ex.client, (fx + dx, fy, fz + dz), dim):
                ux, uz = float(dx), float(dz)
                break

    away_yaw = math.atan2(-ux, -uz)
    moved = yield from _move_to_bounded(ex, [round(px + ux * 8.0, 2), py, round(pz + uz * 8.0, 2)], rng=2, timeout=12.0)
    if not moved:
        yield from _control_step(ex, forward=True, sprint=True, jump=True, yaw=away_yaw, ms=800)

    st_after = _refresh_status(ex, state)
    after_hostile = st_after.get("nearest_hostile") or {}
    if not after_hostile:
        return True
    flee_dist = float(getattr(getattr(state, "config", None), "hostile_flee_dist", 6.0) or 6.0)
    if (h_dist := after_hostile.get("dist")) is not None and float(h_dist) >= flee_dist:
        return True
    after_pos = st_after.get("pos")
    if after_pos and len(after_pos) >= 3 and math.dist(after_pos[:3], [px, py, pz]) >= 1.5:
        if isinstance(h_pos, (list, tuple)) and len(h_pos) >= 3:
            return math.dist(after_pos[:3], h_pos[:3]) > math.dist([px, py, pz], h_pos[:3]) + 0.5
        return True
    return False


def escape_hazard_step(ex: "Executor", state: "State", intent: str) -> Generator[dict, None, bool]:
    """按安全意图分派单轮应急脱困流程。"""
    if intent in ("surface", "escape_water"):
        return (yield from escape_water_to_shore(ex, state))
    if intent in ("douse", "escape_lava"):
        return (yield from _douse_or_escape_lava(ex, state))
    if intent == "flee":
        return (yield from _flee_hostile(ex, state))
    return False
