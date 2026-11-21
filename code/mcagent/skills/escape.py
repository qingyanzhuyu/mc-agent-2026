"""水体/岩浆/火焰/敌对生物紧急脱困原语（L2）。

所有阻塞动作均通过 `Executor` 发出，遵守"一次动作后立即 yield 一条记录"契约；
只读观察经 `voxel_cache_for(state)` / `ex.client.find_blocks` 完成。安全抢占 `Replan` 立即原样上抛。
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Callable, Generator, Optional

from ..client import ApiError
from ..runtime import Replan, effective_hostile_flee_dist, has_equipped_armor
from .base import failure, rec
from .items import AIR, NAV_FAIL_CODES, bare, is_clear_space, is_safe_solid_floor, pick_placeable, total_placeable
from .sightline import DIG_EYE_REACH, block_cell, eye_to_block_dist, voxel_line_cells
from .voxel_cache import voxel_cache_for

if TYPE_CHECKING:
    from ..runtime import Executor, State

Cell = tuple[int, int, int]
PLACE_EYE_REACH: float = DIG_EYE_REACH
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


def _valid_vec3(pos: Any) -> Optional[tuple[float, float, float]]:
    if not isinstance(pos, (list, tuple)) or len(pos) < 3:
        return None
    vals = pos[:3]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in vals):
        return None
    return (float(vals[0]), float(vals[1]), float(vals[2]))


def _is_solid_wall_block(blk: Optional[dict]) -> bool:
    if not isinstance(blk, dict):
        return False
    name = bare(blk.get("name")).strip().lower()
    if not name or name in AIR or name in _FLUID_OR_FIRE or is_clear_space(blk):
        return False
    return True


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


def _call_move_once(ex: "Executor", target_pos: list[float], *, rng: float, timeout: float) -> dict:
    try:
        return ex.move_to(pos=target_pos, rng=rng, timeout=timeout, retries=0, stop_on=("death",))
    except TypeError:
        try:
            return ex.move_to(pos=target_pos, rng=rng, timeout=timeout, retries=0)
        except TypeError:
            return ex.move_to(pos=target_pos, rng=rng)


def _move_to_bounded(
    ex: "Executor", target_pos: list[float], *, rng: float = 1, timeout: float = 15.0, **fields: Any,
) -> Generator[dict, None, bool]:
    # 脱困移动必须使用 stop_on=("death",)：逃离怪群/岩浆/火焰时挨一下或受灼烧绝不能中途停步
    for attempt in range(2):
        try:
            res = _call_move_once(ex, target_pos, rng=rng, timeout=timeout)
            yield rec("move_to", res, pos=target_pos, **fields)
            return True
        except Replan as err:
            if _is_preemption(err):
                raise
            code, _ = failure(err)
            if code == "busy" and attempt == 0:
                yield rec("move_to", pos=target_pos, error_code="busy", **fields)
                stop_fn = getattr(ex, "stop", None)
                if callable(stop_fn):
                    stop_fn(raise_on_error=False)
                continue
            if code in NAV_FAIL_CODES or code == "timeout":
                yield rec("move_to", pos=target_pos, error_code=code, **fields)
                return False
            raise
    return False


def _control_step(ex: "Executor", **kwargs: Any) -> Generator[dict, None, tuple[bool, str]]:
    kwargs.setdefault("stop_on", ("death",))
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
    swim_idx, control_unsupported, shore_fails = 0, False, 0

    for _ in range(max(0, int(max_steps))):
        dim = str(st.get("dimension") or "overworld")
        if (dig_cell := _find_soft_overhang(ex, st, cache, dim, tried_digs)) is not None:
            tried_digs.add(dig_cell)
            dig_pos = list(dig_cell)
            yield from _exec_step(
                "dig", lambda: ex.dig(dig_pos), on_ok=lambda: cache.invalidate(dig_pos, dimension=dim), pos=dig_pos,
            )
        elif shore_fails < 2 and (shore := _find_dry_shore(ex, st, cache, dim, tried_shores)) is not None:
            tried_shores.add(shore)
            moved_shore = yield from _move_to_bounded(ex, [shore[0] + 0.5, float(shore[1]), shore[2] + 0.5], rng=1, timeout=15.0)
            shore_fails = 0 if moved_shore else (shore_fails + 1)
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


def _has_shield(state: "State", st: dict) -> bool:
    if bare((st.get("offhand") or {}).get("name")) == "shield":
        return True
    return hasattr(state, "count") and state.count("shield") >= 1


def _set_shield(ex: "Executor", state: "State", active: bool) -> bool:
    if not active and state.st.get("alive") is False:
        return False
    try:
        res = ex.posture(shield=active)
        return bool(res.get("blocking", active)) if isinstance(res, dict) else active
    except (ApiError, Replan, AttributeError):
        return False


def _resolve_flee_vector(
    ex: "Executor", px: float, py: float, pz: float, hostile: dict, cache: Any, dim: str,
) -> tuple[float, float, Optional[list[float]]]:
    """计算远离所有近场敌对生物的单位逃离向量，避免仅靠 /status 缺 pos 时盲目撞向第二只怪。"""
    h_pos = hostile.get("pos") if isinstance(hostile, dict) else None
    if isinstance(h_pos, (list, tuple)) and len(h_pos) >= 3:
        dx, dz = px - float(h_pos[0]), pz - float(h_pos[2])
        if (norm := math.hypot(dx, dz)) >= 1e-6:
            return dx / norm, dz / norm, [float(h_pos[0]), float(h_pos[1]), float(h_pos[2])]

    # 真实服务端 /status.nearest_hostile 不含 pos 字段，补一次 0~1ms 只读 get_entities 合成斥力向量
    get_ent = getattr(ex.client, "get_entities", None)
    if callable(get_ent):
        try:
            res = get_ent(radius=12, types=["hostile"])
            ents = res.get("entities") if isinstance(res, dict) else None
        except (ApiError, AttributeError, TypeError):
            ents = None
        if isinstance(ents, list) and ents:
            rx, rz = 0.0, 0.0
            nearest_pos: Optional[list[float]] = None
            nearest_d = float("inf")
            for ent in ents:
                if not isinstance(ent, dict):
                    continue
                ep = ent.get("pos")
                if not isinstance(ep, (list, tuple)) or len(ep) < 3:
                    continue
                dx, dz = px - float(ep[0]), pz - float(ep[2])
                d = math.hypot(dx, dz)
                if d < nearest_d:
                    nearest_d = d
                    nearest_pos = [float(ep[0]), float(ep[1]), float(ep[2])]
                w = 1.0 / max(0.5, d * d)
                if d >= 1e-6:
                    rx += (dx / d) * w
                    rz += (dz / d) * w
            if (norm := math.hypot(rx, rz)) >= 1e-6:
                return rx / norm, rz / norm, nearest_pos

    fx, fy, fz = block_cell([px, py, pz])
    for dx, dz in _CARDINALS:
        if _is_dry_standable(cache, ex.client, (fx + dx, fy, fz + dz), dim):
            return float(dx), float(dz), None
    return 1.0, 0.0, None


def _is_sprint_yaw_safe(
    ex: "Executor", px: float, py: float, pz: float, yaw: float, cache: Any, dim: str, *, lookahead: int = 5,
) -> bool:
    """检查沿 yaw 疾跑前冲 1..lookahead 格是否存在 >3 格深坑/悬崖或岩浆（仅在脚下实心可观测时启用）。"""
    fx, fy, fz = block_cell([px, py, pz])
    under = _read_block(cache, ex.client, (fx, fy - 1, fz), dim)
    if not (under and is_safe_solid_floor(under)):
        return True
    dx, dz = -math.sin(yaw), -math.cos(yaw)
    checked: set[tuple[int, int]] = {(fx, fz)}
    for step in range(1, lookahead + 1):
        cx, cz = math.floor(px + dx * step), math.floor(pz + dz * step)
        if (cx, cz) in checked:
            continue
        checked.add((cx, cz))
        has_floor = False
        for dy in (1, 0, -1, -2, -3):
            blk = _read_block(cache, ex.client, (cx, fy + dy, cz), dim)
            if blk is None:
                has_floor = True
                break
            bname = bare(blk.get("name"))
            if bname in ("lava", "flowing_lava"):
                return False
            if is_safe_solid_floor(blk):
                has_floor = True
                break
        if not has_floor:
            return False
    return True


def _pick_cliff_safe_yaw(
    ex: "Executor", px: float, py: float, pz: float, preferred_yaw: float, cache: Any, dim: str,
) -> Optional[float]:
    """若首选逃离方向前方有深坑/悬崖/岩浆，依次改选侧向 ±90°/±45°/±135°/180° 安全方向。"""
    for offset in (0.0, math.pi / 2, -math.pi / 2, math.pi / 4, -math.pi / 4, 3 * math.pi / 4, -3 * math.pi / 4, math.pi):
        cand = preferred_yaw + offset
        if _is_sprint_yaw_safe(ex, px, py, pz, cand, cache, dim):
            return cand
    return None


def _pick_alternate_escape_yaw(
    ex: "Executor", px: float, py: float, pz: float, ux: float, uz: float, cache: Any, dim: str,
) -> float:
    """主逃离方向撞墙/无路径时，依次扫描左右正交（±90°）、斜向与反方向可通行干地通道，避免继续撞墙。"""
    fx, fy, fz = block_cell([px, py, pz])
    candidate_dirs = [
        (-uz, ux),
        (uz, -ux),
        (-ux, -uz),
        (-uz - ux, ux - uz),
        (uz - ux, -ux - uz),
    ]
    for ox, oz in candidate_dirs:
        norm = math.hypot(ox, oz)
        if norm <= 1e-6:
            continue
        nx, nz = ox / norm, oz / norm
        sx, sz = int(round(nx)), int(round(nz))
        if (sx, sz) == (0, 0):
            continue
        for dy in (0, 1, -1):
            if _is_dry_standable(cache, ex.client, (fx + sx, fy + dy, fz + sz), dim):
                return math.atan2(-nx, -nz)
    return math.atan2(uz, -ux)  # 右旋 90° 保底


def _try_seal_underground_tunnel(
    ex: "Executor", state: "State", st: dict, h_pos: Optional[list[float]], cache: Any, dim: str,
) -> Generator[dict, None, bool]:
    p = _valid_vec3(st.get("pos"))
    hp = _valid_vec3(h_pos)
    if p is None or hp is None:
        return False
    px, py, pz = p
    fx, fy, fz = block_cell([px, py, pz])

    underground = (
        py < 58.0
        or _is_solid_wall_block(_read_block(cache, ex.client, (fx, fy + 2, fz), dim))
        or _is_solid_wall_block(_read_block(cache, ex.client, (fx, fy + 3, fz), dim))
    )
    if not underground:
        return False

    if getattr(state, "inventory_dirty", False):
        try:
            state.refresh_inventory(ex.client)
        except AttributeError:
            pass
    if total_placeable(state) < 2:
        return False

    hx, hz = hp[0] - px, hp[2] - pz
    d = math.hypot(hx, hz)
    if not (1.5 <= d <= 10.0):
        return False

    dx, dz = ((1 if hx > 0 else -1), 0) if abs(hx) >= abs(hz) else (0, (1 if hz > 0 else -1))
    foot_cell = (fx + dx, fy, fz + dz)
    head_cell = (fx + dx, fy + 1, fz + dz)
    foot_blk = _read_block(cache, ex.client, foot_cell, dim)
    head_blk = _read_block(cache, ex.client, head_cell, dim)
    if not (foot_blk and head_blk and is_clear_space(foot_blk) and is_clear_space(head_blk)):
        return False

    left_solid = (
        _is_solid_wall_block(_read_block(cache, ex.client, (fx + dx - dz, fy, fz + dz + dx), dim))
        or _is_solid_wall_block(_read_block(cache, ex.client, (fx + dx - dz, fy + 1, fz + dz + dx), dim))
    )
    right_solid = (
        _is_solid_wall_block(_read_block(cache, ex.client, (fx + dx + dz, fy, fz + dz - dx), dim))
        or _is_solid_wall_block(_read_block(cache, ex.client, (fx + dx + dz, fy + 1, fz + dz - dx), dim))
    )
    if not (left_solid and right_solid):
        return False

    placed = 0
    for cell in (foot_cell, head_cell):
        if eye_to_block_dist(st, cell) > PLACE_EYE_REACH or total_placeable(state) < 1:
            break
        block = pick_placeable(state)
        cell_pos = list(cell)
        ok = yield from _exec_step(
            "place",
            lambda b=block, p_pos=cell_pos: ex.place(b, pos=p_pos),
            on_ok=lambda p_pos=cell_pos: cache.invalidate(p_pos, dimension=dim),
            item=block,
            pos=cell_pos,
            tunnel_seal=True,
        )
        if not ok:
            break
        placed += 1
        if getattr(state, "inventory_dirty", False):
            try:
                state.refresh_inventory(ex.client)
            except AttributeError:
                pass
        st_next = _refresh_status(ex, state)
        if st_next.get("pos"):
            st = st_next
    return placed >= 2


def _find_skeleton_cover_cell(
    ex: "Executor", px: float, py: float, pz: float, h_pos: Optional[list[float]], cache: Any, dim: str,
) -> Optional[Cell]:
    hp = _valid_vec3(h_pos)
    if hp is None:
        return None
    fx, fy, fz = block_cell([px, py, pz])
    hx, hy, hz = hp
    skel_cell = block_cell([hx, hy, hz])
    skel_eye = (hx, hy + 1.62, hz)

    candidates: list[tuple[float, int, Cell]] = []
    for dx, dz in _CARDINALS:
        for dy in (0, 1, -1):
            cand = (fx + dx, fy + dy, fz + dz)
            if not _is_dry_standable(cache, ex.client, cand, dim):
                continue
            dist_h = math.hypot((cand[0] + 0.5) - hx, (cand[2] + 0.5) - hz)
            if dist_h < 1.5:
                continue
            cand_eye = (cand[0] + 0.5, cand[1] + 1.62, cand[2] + 0.5)
            skip = {
                (cand[0], cand[1], cand[2]),
                (cand[0], cand[1] + 1, cand[2]),
                skel_cell,
                (skel_cell[0], skel_cell[1] + 1, skel_cell[2]),
            }
            for cell in voxel_line_cells(cand_eye, skel_eye):
                if cell in skip:
                    continue
                if _is_solid_wall_block(_read_block(cache, ex.client, cell, dim)):
                    candidates.append((-dist_h, abs(dy), cand))
                    break
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def _flee_hostile(ex: "Executor", state: "State") -> Generator[dict, None, bool]:
    st = _refresh_status(ex, state)
    pos = st.get("pos")
    if not pos or len(pos) < 3:
        return False
    px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
    dim = str(st.get("dimension") or "overworld")
    cache = voxel_cache_for(state)

    hostile = st.get("nearest_hostile") or {}
    h_name = bare(hostile.get("name")).strip().lower() if isinstance(hostile, dict) else ""
    h_dist_raw = hostile.get("dist") if isinstance(hostile, dict) else None
    h_dist = float(h_dist_raw) if isinstance(h_dist_raw, (int, float)) and not isinstance(h_dist_raw, bool) else 99.0
    is_creeper = h_name == "creeper"
    ux, uz, h_pos = _resolve_flee_vector(ex, px, py, pz, hostile, cache, dim)

    away_yaw = math.atan2(-ux, -uz)
    to_mob_yaw = math.atan2(ux, uz)
    burst_blocked = False

    # 苦力怕贴脸（<=3.5 格）且持盾时：先举盾朝向苦力怕后退挡爆，再收盾拉开至安全距离（>=7 格）
    if is_creeper and h_dist <= 3.5:
        if state.inventory_dirty:
            state.refresh_inventory(ex.client)
        if _has_shield(state, st):
            shield_up = _set_shield(ex, state, True)
            try:
                if hasattr(ex, "look"):
                    try:
                        if isinstance(h_pos, (list, tuple)) and len(h_pos) >= 3:
                            ex.look(pos=[float(h_pos[0]), float(h_pos[1]) + 0.9, float(h_pos[2])])
                        else:
                            ex.look(yaw=to_mob_yaw, pitch=0.0)
                    except (ApiError, Replan, AttributeError):
                        pass
                yield from _control_step(ex, back=True, yaw=to_mob_yaw, ms=500)
            finally:
                if shield_up:
                    _set_shield(ex, state, False)
            st_mid = _refresh_status(ex, state)
            mid_hostile = st_mid.get("nearest_hostile") or {}
            if not mid_hostile or (
                isinstance(mid_hostile.get("dist"), (int, float)) and float(mid_hostile["dist"]) >= 7.0
            ):
                return True
            mid_pos = st_mid.get("pos")
            if mid_pos and len(mid_pos) >= 3:
                px, py, pz = float(mid_pos[0]), float(mid_pos[1]), float(mid_pos[2])
    else:
        sealed = yield from _try_seal_underground_tunnel(ex, state, st, h_pos, cache, dim)
        if sealed:
            return True
        hp_raw = st.get("health", 20)
        hp_val = float(hp_raw) if isinstance(hp_raw, (int, float)) and not isinstance(hp_raw, bool) else 20.0
        hp_risk = float(getattr(getattr(state, "config", None), "hp_risk", 14.0) or 14.0)
        if h_name == "skeleton" and h_dist < 10.0 and (not has_equipped_armor(state) or hp_val < hp_risk):
            cover = _find_skeleton_cover_cell(ex, px, py, pz, h_pos, cache, dim)
            if cover is not None:
                moved_cover = yield from _move_to_bounded(
                    ex, [cover[0] + 0.5, float(cover[1]), cover[2] + 0.5], rng=0, timeout=8.0, skeleton_cover=True,
                )
                if moved_cover:
                    st_after_cov = _refresh_status(ex, state)
                    ap = st_after_cov.get("pos") or [px, py, pz]
                    if math.hypot(float(ap[0]) - px, float(ap[2]) - pz) >= 0.5:
                        return True
        if h_dist < 2.5:
            # 贴脸挨打（<2.5 格）时跳过慢速 A* 寻路，先用防跳崖检查的原生疾跑跳脱离近战命中圈再补 move_to
            safe_burst_yaw = _pick_cliff_safe_yaw(ex, px, py, pz, away_yaw, cache, dim)
            before_burst = (px, py, pz)
            ok_burst = False
            if safe_burst_yaw is not None:
                ok_burst, _ = yield from _control_step(ex, forward=True, sprint=True, jump=True, yaw=safe_burst_yaw, ms=900)
            if ok_burst:
                st_mid = _refresh_status(ex, state)
                mid_hostile = st_mid.get("nearest_hostile") or {}
                base_flee = float(getattr(getattr(state, "config", None), "hostile_flee_dist", 6.0) or 6.0)
                safe_mid_clear = (
                    max(9.0, effective_hostile_flee_dist(state, h_name))
                    if is_creeper
                    else max(base_flee + 2.0, effective_hostile_flee_dist(state, h_name))
                )
                if not mid_hostile or (
                    isinstance(mid_hostile.get("dist"), (int, float)) and float(mid_hostile["dist"]) >= safe_mid_clear
                ):
                    return True
                mid_pos = st_mid.get("pos")
                if mid_pos and len(mid_pos) >= 3:
                    px, py, pz = float(mid_pos[0]), float(mid_pos[1]), float(mid_pos[2])
                    if math.dist(before_burst, (px, py, pz)) < 1.5:
                        burst_blocked = True

    flee_stride = 16.0 if is_creeper else 18.0
    moved = yield from _move_to_bounded(
        ex, [round(px + ux * flee_stride, 2), py, round(pz + uz * flee_stride, 2)], rng=2, timeout=12.0,
    )
    if not moved:
        st_retry = _refresh_status(ex, state)
        rpos = st_retry.get("pos")
        if isinstance(rpos, (list, tuple)) and len(rpos) >= 3:
            px, py, pz = float(rpos[0]), float(rpos[1]), float(rpos[2])
        retry_yaw = (
            _pick_alternate_escape_yaw(ex, px, py, pz, ux, uz, cache, dim)
            if burst_blocked
            else away_yaw
        )
        safe_retry_yaw = _pick_cliff_safe_yaw(ex, px, py, pz, retry_yaw, cache, dim)
        if safe_retry_yaw is not None:
            yield from _control_step(ex, forward=True, sprint=True, jump=True, yaw=safe_retry_yaw, ms=800)

    st_after = _refresh_status(ex, state)
    after_hostile = st_after.get("nearest_hostile") or {}
    if moved and isinstance(after_hostile, dict) and after_hostile:
        after_d = after_hostile.get("dist")
        after_pos_now = st_after.get("pos")
        after_hpos = after_hostile.get("pos")
        if (isinstance(after_pos_now, (list, tuple)) and len(after_pos_now) >= 3
                and isinstance(after_hpos, (list, tuple)) and len(after_hpos) >= 3):
            actual_d = math.dist(after_pos_now[:3], after_hpos[:3])
        elif isinstance(after_d, (int, float)) and not isinstance(after_d, bool):
            actual_d = float(after_d)
        else:
            actual_d = 99.0
        if actual_d < 5.5:
            # move_to 寻路禁疾跑，若结束时高速敌怪仍紧贴 <5.5 格，补一次防跳崖疾跑跳拉脱
            ap = after_pos_now if (isinstance(after_pos_now, (list, tuple)) and len(after_pos_now) >= 3) else [px, py, pz]
            safe_follow_yaw = _pick_cliff_safe_yaw(ex, float(ap[0]), float(ap[1]), float(ap[2]), away_yaw, cache, dim)
            if safe_follow_yaw is not None:
                yield from _control_step(ex, forward=True, sprint=True, jump=True, yaw=safe_follow_yaw, ms=900)
            st_after = _refresh_status(ex, state)
            after_hostile = st_after.get("nearest_hostile") or {}
    if not after_hostile:
        return True
    flee_dist = effective_hostile_flee_dist(state, h_name)
    safe_dist = max(flee_dist, 7.0) if is_creeper else flee_dist
    if (after_dist := after_hostile.get("dist")) is not None and float(after_dist) >= safe_dist:
        return True
    after_pos = st_after.get("pos")
    if after_pos and len(after_pos) >= 3 and math.dist(after_pos[:3], [px, py, pz]) >= 1.5:
        if isinstance(h_pos, (list, tuple)) and len(h_pos) >= 3:
            return math.dist(after_pos[:3], h_pos[:3]) > math.dist([px, py, pz], h_pos[:3]) + 0.5
        return True
    return False


def escape_hazard_step(ex: "Executor", state: "State", intent: str) -> Generator[dict, None, bool]:
    """按安全意图分派单轮应急脱困流程（遭遇敌怪时先评估双方战力与躲避可行性，决定反杀或规避）。"""
    if intent in ("surface", "escape_water"):
        return (yield from escape_water_to_shore(ex, state))
    if intent in ("douse", "escape_lava"):
        return (yield from _douse_or_escape_lava(ex, state))
    if intent == "flee":
        st = _refresh_status(ex, state)
        if isinstance(st.get("nearest_hostile"), dict) and st["nearest_hostile"]:
            from .combat_eval import counterattack_hostile, decide_combat_or_flee

            decision = decide_combat_or_flee(ex, state)
            if decision.action == "fight":
                if (yield from counterattack_hostile(ex, state, decision)):
                    return True
        return (yield from _flee_hostile(ex, state))
    return False
