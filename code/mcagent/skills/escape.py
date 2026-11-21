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
_RANGED_HOSTILES = frozenset({"skeleton", "stray", "bogged", "pillager", "parched"})
_FAST_PURSUERS = frozenset({"spider", "cave_spider", "baby_zombie"})
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


def _control_step(ex: "Executor", *, record_extra: Optional[dict[str, Any]] = None, **kwargs: Any) -> Generator[dict, None, tuple[bool, str]]:
    kwargs.setdefault("stop_on", ("death",))
    extra = dict(record_extra or {})
    try:
        res = ex.control(**kwargs)
        yield rec("control", res, **{k: v for k, v in kwargs.items() if k in ("yaw", "ms")}, **extra)
        return True, ""
    except AttributeError:
        yield rec("control", error_code="unsupported_api", **extra)
        return False, "unsupported_api"
    except Replan as err:
        if _is_preemption(err):
            raise
        code, _ = failure(err)
        yield rec("control", error_code=code, **extra)
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
                before_pos = _valid_vec3(st.get("pos"))
                nh = st.get("nearest_hostile") if isinstance(st.get("nearest_hostile"), dict) else None
                hp = _valid_vec3(nh.get("pos")) if nh else None
                yaw_offset = _SWIM_YAWS[swim_idx % len(_SWIM_YAWS)]
                if before_pos is not None and hp is not None and math.hypot(before_pos[0] - hp[0], before_pos[2] - hp[2]) <= 16.0:
                    ux, uz = before_pos[0] - hp[0], before_pos[2] - hp[2]
                    yaw = (math.atan2(-ux, -uz) + yaw_offset) if math.hypot(ux, uz) > 1e-4 else yaw_offset
                else:
                    yaw = yaw_offset
                ok, err_code = yield from _control_step(ex, forward=True, jump=True, yaw=yaw, ms=600)
                if not ok and err_code == "unsupported_api":
                    control_unsupported = True
                else:
                    st = _refresh_status(ex, state)
                    after_pos = _valid_vec3(st.get("pos"))
                    moved_xz = math.hypot(after_pos[0] - before_pos[0], after_pos[2] - before_pos[2]) if (before_pos and after_pos) else 0.0
                    if not ok or moved_xz < 0.4:
                        swim_idx += 1
                    if not st.get("in_water") and not st.get("submerged"):
                        return True
                    continue

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
    cache = voxel_cache_for(state)
    dry = _find_dry_shore(ex, st, cache, dim, set())

    # 岩浆内每秒掉 8 血且 A* 寻路极易卡死不动：优先立即沿干岸方向原生疾跑跳出岩浆，避免 move_to 停滞烧死
    if st.get("in_lava"):
        pos = _valid_vec3(st.get("pos"))
        yaws: list[float] = []
        if pos is not None and dry is not None:
            dx, dz = (dry[0] + 0.5) - pos[0], (dry[2] + 0.5) - pos[2]
            if math.hypot(dx, dz) > 1e-4:
                yaws.append(math.atan2(-dx, -dz))
        yaws.extend(_SWIM_YAWS)
        control_ok = True
        for burst_idx in range(3):
            yaw = yaws[burst_idx % len(yaws)]
            ok_c, err_c = yield from _control_step(ex, forward=True, sprint=True, jump=True, yaw=yaw, ms=650)
            if not ok_c and err_c == "unsupported_api":
                control_ok = False
                break
            st = _refresh_status(ex, state)
            if st.get("alive") is False:
                return False
            if not st.get("in_lava"):
                break
            # 仍在岩浆中则按最新坐标刷新干岸方向，避免重复撞墙
            pos_now = _valid_vec3(st.get("pos"))
            dry_now = _find_dry_shore(ex, st, cache, dim, set())
            if pos_now is not None and dry_now is not None:
                dx2, dz2 = (dry_now[0] + 0.5) - pos_now[0], (dry_now[2] + 0.5) - pos_now[2]
                if math.hypot(dx2, dz2) > 1e-4:
                    yaws[0] = math.atan2(-dx2, -dz2)
        if not st.get("on_fire") and not st.get("in_lava"):
            return True
        if st.get("in_lava") and not control_ok and dry is not None:
            yield from _move_to_bounded(ex, [dry[0] + 0.5, float(dry[1]), dry[2] + 0.5], rng=1, timeout=4.0)
    else:
        moved = (
            (yield from _move_to_bounded(ex, [dry[0] + 0.5, float(dry[1]), dry[2] + 0.5], rng=1, timeout=6.0))
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


def _collect_nearby_hostiles(
    ex: "Executor", px: float, py: float, pz: float, hostile: dict, *, radius: int = 18,
) -> list[dict[str, Any]]:
    """收集 18 格内全部敌对生物（融合 get_entities 与 /status.nearest_hostile），避免逃离近怪时撞向远程骷髅或苦力怕。"""
    collected: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    get_ent = getattr(ex.client, "get_entities", None)
    if callable(get_ent):
        try:
            res = get_ent(radius=radius, types=["hostile"])
            ents = res.get("entities") if isinstance(res, dict) else None
        except (ApiError, AttributeError, TypeError):
            ents = None
        if isinstance(ents, list):
            for ent in ents:
                if not isinstance(ent, dict):
                    continue
                ep = _valid_vec3(ent.get("pos"))
                if ep is None:
                    continue
                eid = ent.get("id")
                if isinstance(eid, int) and not isinstance(eid, bool):
                    seen_ids.add(eid)
                collected.append(ent)
    if isinstance(hostile, dict):
        hp = _valid_vec3(hostile.get("pos"))
        eid = hostile.get("id")
        if hp is not None and not (isinstance(eid, int) and not isinstance(eid, bool) and eid in seen_ids):
            collected.append(hostile)
    return collected


def _score_escape_dir(
    px: float,
    pz: float,
    cx: float,
    cz: float,
    threats: list[tuple[float, float, str, float, Optional[int]]],
    attacker_id: Optional[int],
    *,
    in_water: bool = False,
) -> float:
    """对候选逃跑单位方向 (cx, cz) 评分：最大化对所有威胁的 4m/8m 净空，重罚朝向苦力怕/骷髅/水中溺尸/刚偷袭者前进。"""
    score = 0.0
    min_clear = float("inf")
    for hx, hz, name, d0, eid in threats:
        is_creeper = name == "creeper"
        is_attacker = attacker_id is not None and eid == attacker_id
        is_ranged = name in _RANGED_HOSTILES or (name == "drowned" and (in_water or is_attacker))
        weight = 2.5 if is_creeper else (1.8 if is_ranged else 1.0)
        if is_attacker:
            weight *= 1.6
        d4 = math.hypot((px + cx * 4.0) - hx, (pz + cz * 4.0) - hz)
        d8 = math.hypot((px + cx * 8.0) - hx, (pz + cz * 8.0) - hz)
        min_d = min(d4, d8)
        min_clear = min(min_clear, min_d / max(1.0, weight * 0.6))
        # 鼓励远离，惩罚接近
        score += (d8 - d0) * weight
        if is_creeper and min_d < 7.5:
            score -= (7.5 - min_d) * 8.0
        elif is_ranged and min_d < 8.5:
            score -= (8.5 - min_d) * 4.0
        elif min_d < 4.5:
            score -= (4.5 - min_d) * 5.0
        if is_attacker and d8 < d0 - 1.0:
            score -= (d0 - d8) * 4.0
    return score + min_clear * 2.0


def _path_enters_water(
    ex: "Executor",
    px: float,
    py: float,
    pz: float,
    cx: float,
    cz: float,
    cache: Any,
    dim: str,
    *,
    probes: tuple[int, ...] = (2, 4, 6),
) -> bool:
    """检测干地逃离方向前 probes 格是否会直接踏入河道/深水（防止干地遇怪反跳进溺尸河里）。"""
    norm = math.hypot(cx, cz)
    if norm <= 1e-6:
        return False
    ux, uz = cx / norm, cz / norm
    _, fy, _ = block_cell([px, py, pz])
    checked: set[tuple[int, int]] = set()
    for dist in probes:
        bx, bz = math.floor(px + ux * dist), math.floor(pz + uz * dist)
        if (bx, bz) in checked:
            continue
        checked.add((bx, bz))
        blk_body = _read_block(cache, ex.client, (bx, fy, bz), dim)
        if _is_water_cell(blk_body):
            return True
        blk_floor = _read_block(cache, ex.client, (bx, fy - 1, bz), dim)
        if _is_water_cell(blk_floor) and (blk_body is None or is_clear_space(blk_body)):
            return True
        if blk_floor is not None and is_clear_space(blk_floor) and (blk_body is None or is_clear_space(blk_body)):
            blk_drop = _read_block(cache, ex.client, (bx, fy - 2, bz), dim)
            if _is_water_cell(blk_drop):
                return True
    return False


def _find_safe_dry_shore_away(
    ex: "Executor",
    st: dict,
    cache: Any,
    dim: str,
    threats: list[tuple[float, float, str, float, Optional[int]]],
    *,
    tried_shores: set[Cell] = frozenset(),
) -> Optional[Cell]:
    """水中脱险时优先寻找 6 格内远离敌怪（不贴近苦力怕/近怪）的实心干岸格。"""
    pos = _valid_vec3(st.get("pos"))
    if pos is None:
        return None
    px, py, pz = pos
    fx, fy, fz = block_cell([px, py, pz])
    best: Optional[tuple[float, Cell]] = None
    for dx, dz in _SHORE_OFFSETS:
        sx, sz = fx + dx, fz + dz
        for dy in (0, 1, 2, -1):
            cand = (sx, fy + dy, sz)
            if cand in tried_shores or not _is_dry_standable(cache, ex.client, cand, dim):
                continue
            cx, cz = cand[0] + 0.5, cand[2] + 0.5
            d_self = math.hypot(cx - px, cz - pz)
            safe = True
            min_clearance = 18.0
            for hx, hz, name, d0, _ in threats:
                d_cand = math.hypot(cx - hx, cz - hz)
                if name == "creeper" and d_cand < max(6.5, min(d0, 8.0)):
                    safe = False
                    break
                if d0 < 9.0 and d_cand < max(4.5, d0 - 0.5):
                    safe = False
                    break
                min_clearance = min(min_clearance, d_cand)
            if not safe:
                continue
            score = min_clearance - 0.85 * d_self
            if best is None or score > best[0]:
                best = (score, cand)
            break
    return best[1] if best is not None else None


def _resolve_flee_vector(
    ex: "Executor",
    px: float,
    py: float,
    pz: float,
    hostile: dict,
    cache: Any,
    dim: str,
    *,
    state: Optional["State"] = None,
    avoid_water: bool = False,
    in_water: bool = False,
) -> tuple[float, float, Optional[list[float]]]:
    """计算远离 18 格内全部敌对生物的单位逃离向量，避免避开近怪时直冲远程骷髅/苦力怕或从干地跳入水中。"""
    ents = _collect_nearby_hostiles(ex, px, py, pz, hostile, radius=18)
    attacker_id: Optional[int] = None
    if state is not None:
        try:
            from .combat_eval import recent_hurt_attacker_id
            attacker_id = recent_hurt_attacker_id(state)
        except Exception:
            attacker_id = None

    threats: list[tuple[float, float, str, float, Optional[int]]] = []
    nearest_pos: Optional[list[float]] = None
    nearest_d = float("inf")
    for ent in ents:
        ep = _valid_vec3(ent.get("pos"))
        if ep is None:
            continue
        dx, dz = px - ep[0], pz - ep[2]
        d = math.hypot(dx, dz)
        name = bare(ent.get("name")).strip().lower()
        eid = ent.get("id") if isinstance(ent.get("id"), int) and not isinstance(ent.get("id"), bool) else None
        if d < nearest_d:
            nearest_d = d
            nearest_pos = [ep[0], ep[1], ep[2]]
        threats.append((ep[0], ep[2], name, d, eid))

    def _eval_dir(c: tuple[float, float]) -> float:
        sc = _score_escape_dir(px, pz, c[0], c[1], threats, attacker_id, in_water=in_water)
        if avoid_water and _path_enters_water(ex, px, py, pz, c[0], c[1], cache, dim):
            sc -= 22.0
        return sc

    if len(threats) == 1:
        hx, hz, _, d, _ = threats[0]
        if d >= 1e-6:
            ux0, uz0 = (px - hx) / d, (pz - hz) / d
            if not (avoid_water and _path_enters_water(ex, px, py, pz, ux0, uz0, cache, dim)):
                return ux0, uz0, nearest_pos
            candidates: list[tuple[float, float]] = [(ux0, uz0)] + [
                (math.cos(i * math.pi / 6.0), math.sin(i * math.pi / 6.0)) for i in range(12)
            ]
            best_dir = max(candidates, key=_eval_dir)
            return best_dir[0], best_dir[1], nearest_pos
    elif len(threats) >= 2:
        rx, rz = 0.0, 0.0
        for hx, hz, name, d, eid in threats:
            if d < 1e-6:
                continue
            is_ranged_t = name in _RANGED_HOSTILES or (name == "drowned" and (in_water or (attacker_id is not None and eid == attacker_id)))
            mult = 2.5 if name == "creeper" else (1.8 if is_ranged_t else 1.0)
            if attacker_id is not None and eid == attacker_id:
                mult *= 1.6
            w = mult / max(0.5, d * d)
            rx += ((px - hx) / d) * w
            rz += ((pz - hz) / d) * w
        candidates = [
            (math.cos(i * math.pi / 6.0), math.sin(i * math.pi / 6.0)) for i in range(12)
        ]
        if (rnorm := math.hypot(rx, rz)) >= 1e-6:
            candidates.insert(0, (rx / rnorm, rz / rnorm))
        best_dir = max(candidates, key=_eval_dir)
        return best_dir[0], best_dir[1], nearest_pos

    fx, fy, fz = block_cell([px, py, pz])
    for dx, dz in _CARDINALS:
        if _is_dry_standable(cache, ex.client, (fx + dx, fy, fz + dz), dim):
            return float(dx), float(dz), None
    return 1.0, 0.0, None


def _is_ranged_los_blocked(
    ex: "Executor", px: float, py: float, pz: float, h_pos: Optional[list[float]], cache: Any, dim: str,
) -> bool:
    """检查玩家当前眼位与远程敌怪眼位之间是否已有实心墙体阻挡射界。"""
    hp = _valid_vec3(h_pos)
    if hp is None:
        return False
    p_cell = block_cell([px, py, pz])
    h_cell = block_cell(hp)
    skip = {
        p_cell,
        (p_cell[0], p_cell[1] + 1, p_cell[2]),
        h_cell,
        (h_cell[0], h_cell[1] + 1, h_cell[2]),
    }
    p_eye = (px, py + 1.62, pz)
    h_eye = (hp[0], hp[1] + 1.62, hp[2])
    for cell in voxel_line_cells(p_eye, h_eye):
        if cell in skip:
            continue
        if _is_solid_wall_block(_read_block(cache, ex.client, cell, dim)):
            return True
    return False


def _is_sprint_yaw_safe(
    ex: "Executor", px: float, py: float, pz: float, yaw: float, cache: Any, dim: str, *, lookahead: int = 5,
) -> bool:
    """检查沿 yaw 疾跑前冲 1..lookahead 格是否存在 >3 格深坑/悬崖或岩浆（仅在脚下实心可观测时启用）。"""
    fx, fy, fz = block_cell([px, py, pz])
    under = _read_block(cache, ex.client, (fx, fy - 1, fz), dim)
    if under is None or not bare(under.get("name")):
        return True
    if not is_safe_solid_floor(under):
        # 脚下悬空/边缘下落（如刚滑出峡谷崖壁）时，只要周邻存在可观测实心地形，仍必须做前方深坑/岩浆检查，
        # 禁止跳过护栏导致滞空疾跑跳直接冲进深坑；纯空壳单测世界则保持兼容返回 True。
        probe_cells = (
            (fx + 1, fy - 1, fz), (fx - 1, fy - 1, fz),
            (fx, fy - 1, fz + 1), (fx, fy - 1, fz - 1),
            (fx, fy - 2, fz), (fx, fy, fz),
        )
        has_terrain = any(
            (nb := _read_block(cache, ex.client, cell, dim)) is not None and is_safe_solid_floor(nb)
            for cell in probe_cells
        )
        if not has_terrain:
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


def _is_yaw_wall_blocked(
    ex: "Executor", px: float, py: float, pz: float, ux: float, uz: float, cache: Any, dim: str,
) -> bool:
    """检查逃离主方向紧邻前 1 格是否存在 2 格高实心墙体阻挡（避免疾跑撞墙空转）。"""
    norm = math.hypot(ux, uz)
    if norm <= 1e-6:
        return False
    sx, sz = int(round(ux / norm)), int(round(uz / norm))
    if (sx, sz) == (0, 0):
        return False
    fx, fy, fz = block_cell([px, py, pz])
    b_head = _read_block(cache, ex.client, (fx + sx, fy + 1, fz + sz), dim)
    if not _is_solid_wall_block(b_head):
        return False
    b_foot = _read_block(cache, ex.client, (fx + sx, fy, fz + sz), dim)
    b_top = _read_block(cache, ex.client, (fx + sx, fy + 2, fz + sz), dim)
    return _is_solid_wall_block(b_foot) or _is_solid_wall_block(b_top)


def _yaw_diff(a: float, b: float) -> float:
    return abs(math.atan2(math.sin(a - b), math.cos(a - b)))


def _pick_alternate_escape_yaw(
    ex: "Executor", px: float, py: float, pz: float, ux: float, uz: float, cache: Any, dim: str,
    *, tried_yaws: Sequence[float] = (),
) -> float:
    """主逃离方向撞墙/无路径时，依次扫描左右正交（±90°）、斜向与反方向可通行干地通道，并跳过已撞墙失败的 tried_yaws。"""
    fx, fy, fz = block_cell([px, py, pz])
    candidate_dirs = [
        (-uz, ux),
        (uz, -ux),
        (-ux, -uz),
        (-uz - ux, ux - uz),
        (uz - ux, -ux - uz),
        (ux - uz, uz + ux),
        (ux + uz, uz - ux),
        (ux, uz),
    ]
    untried_yaws: list[float] = []
    for ox, oz in candidate_dirs:
        norm = math.hypot(ox, oz)
        if norm <= 1e-6:
            continue
        nx, nz = ox / norm, oz / norm
        cand_yaw = math.atan2(-nx, -nz)
        if tried_yaws and any(_yaw_diff(cand_yaw, ty) < 0.65 for ty in tried_yaws):
            continue
        untried_yaws.append(cand_yaw)
        sx, sz = int(round(nx)), int(round(nz))
        if (sx, sz) == (0, 0):
            continue
        for dy in (0, 1, -1):
            if _is_dry_standable(cache, ex.client, (fx + sx, fy + dy, fz + sz), dim):
                return cand_yaw
    if untried_yaws:
        return untried_yaws[0]
    if tried_yaws:
        # 若全部候选都接近 tried_yaws，选与最近受阻角度夹角最大（反向/正交）的角度强制换向
        return max(
            (math.atan2(-ox, -oz) for ox, oz in candidate_dirs if math.hypot(ox, oz) > 1e-6),
            key=lambda y: min(_yaw_diff(y, ty) for ty in tried_yaws),
        )
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


def _try_place_ranged_barricade(
    ex: "Executor", state: "State", st: dict, h_pos: Optional[list[float]], cache: Any, dim: str,
) -> Generator[dict, None, bool]:
    """遭遇骷髅等远程敌怪且缺少天然掩体时，朝敌怪方向原地立 2 格高挡箭墙切断射界。"""
    p = _valid_vec3(st.get("pos"))
    hp = _valid_vec3(h_pos)
    if p is None or hp is None:
        return False
    px, py, pz = p
    fx, fy, fz = block_cell([px, py, pz])
    hx, hz = hp[0] - px, hp[2] - pz
    if not (2.0 <= math.hypot(hx, hz) <= 16.0):
        return False

    if getattr(state, "inventory_dirty", False):
        try:
            state.refresh_inventory(ex.client)
        except AttributeError:
            pass
    if total_placeable(state) < 2:
        return False

    dx, dz = ((1 if hx > 0 else -1), 0) if abs(hx) >= abs(hz) else (0, (1 if hz > 0 else -1))
    floor_blk = _read_block(cache, ex.client, (fx + dx, fy - 1, fz + dz), dim)
    foot_cell = (fx + dx, fy, fz + dz)
    head_cell = (fx + dx, fy + 1, fz + dz)
    foot_blk = _read_block(cache, ex.client, foot_cell, dim)
    head_blk = _read_block(cache, ex.client, head_cell, dim)
    if not (floor_blk and is_safe_solid_floor(floor_blk)):
        return False
    if not (foot_blk and head_blk and is_clear_space(foot_blk) and is_clear_space(head_blk)):
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
            skeleton_cover=True,
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


def _strafe_control_keys(dx: float, dz: float, face_yaw: float) -> dict[str, bool]:
    """保持面敌 face_yaw 不转身的前提下，将目标水平位移 (dx, dz) 映射到 Mineflayer 前后左右键。"""
    norm = math.hypot(dx, dz)
    if norm < 1e-6:
        return {"back": True}
    # prismarine-physics: forward=(-sin(yaw), -cos(yaw)), right=(cos(yaw), -sin(yaw))
    proj_fwd = dx * (-math.sin(face_yaw)) + dz * (-math.cos(face_yaw))
    proj_right = dx * math.cos(face_yaw) + dz * (-math.sin(face_yaw))
    thresh = 0.25 * norm
    keys: dict[str, bool] = {}
    if proj_fwd > thresh:
        keys["forward"] = True
    elif proj_fwd < -thresh:
        keys["back"] = True
    if proj_right > thresh:
        keys["right"] = True
    elif proj_right < -thresh:
        keys["left"] = True
    return keys or {"back": True}


def _resolve_shield_guard_yaw(
    ex: "Executor", state: "State", px: float, py: float, pz: float, fallback_yaw: float,
) -> tuple[float, Optional[int]]:
    """融合 16 格内敌群加权角平分线与最近 hurt 偷袭者，算出最优举盾朝向。"""
    from .combat_eval import observe_nearby_hostiles, recent_hurt_attacker_id, shield_guard_yaw

    attacker_id = recent_hurt_attacker_id(state)
    hostiles = observe_nearby_hostiles(ex, state, radius=16.0)
    yaw = shield_guard_yaw([px, py, pz], hostiles, preferred_attacker_id=attacker_id)
    return (yaw if yaw is not None else fallback_yaw), attacker_id


def _look_at_hostile(
    ex: "Executor", h_pos: Optional[list[float]], face_yaw: float, *, prefer_yaw: bool = False,
) -> None:
    if not hasattr(ex, "look"):
        return
    try:
        if not prefer_yaw and isinstance(h_pos, (list, tuple)) and len(h_pos) >= 3:
            ex.look(pos=[float(h_pos[0]), float(h_pos[1]) + 0.9, float(h_pos[2])])
        else:
            ex.look(yaw=face_yaw, pitch=0.0)
    except (ApiError, Replan, AttributeError):
        pass


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
    is_ranged = h_name in _RANGED_HOSTILES

    # 无敌对生物时的 void_risk 脱险：禁止执行盲目 12 格 move_to + 悬崖疾跑跳，仅就近踏上已确认实心干地
    if not hostile and not _collect_nearby_hostiles(ex, px, py, pz, hostile, radius=18):
        fx, fy, fz = block_cell([px, py, pz])
        under = _read_block(cache, ex.client, (fx, fy - 1, fz), dim)
        if st.get("on_ground") is True and under is not None and is_safe_solid_floor(under):
            return True
        safe_ground = _find_dry_shore(ex, st, cache, dim, set())
        if safe_ground is not None:
            moved_safe = yield from _move_to_bounded(
                ex, [safe_ground[0] + 0.5, float(safe_ground[1]), safe_ground[2] + 0.5], rng=1, timeout=4.0,
            )
            st_safe = _refresh_status(ex, state)
            if moved_safe or st_safe.get("on_ground") is True:
                return True
        return False

    in_water_init = bool(st.get("in_water") or st.get("submerged"))
    ux, uz, h_pos = _resolve_flee_vector(
        ex, px, py, pz, hostile, cache, dim, state=state,
        avoid_water=not in_water_init, in_water=in_water_init,
    )

    away_yaw = math.atan2(-ux, -uz)
    to_mob_yaw = math.atan2(ux, uz)
    burst_blocked = False
    tried_yaws: list[float] = []
    if getattr(state, "inventory_dirty", False):
        try:
            state.refresh_inventory(ex.client)
        except AttributeError:
            pass
    has_shield = _has_shield(state, st)
    shield_yaw, hurt_attacker_id = (
        _resolve_shield_guard_yaw(ex, state, px, py, pz, to_mob_yaw)
        if has_shield
        else (to_mob_yaw, None)
    )
    use_bisector = has_shield and (abs(shield_yaw - to_mob_yaw) > 0.15 or hurt_attacker_id is not None)

    # 苦力怕贴脸（<=3.5 格，或水中 <=6.5 格）时：持盾先举盾后退挡爆，无盾则立即原生疾跑跳/泳离爆炸半径
    creeper_burst_range = 6.5 if st.get("in_water") else 3.5
    if is_creeper and h_dist <= creeper_burst_range:
        if has_shield and h_dist <= 3.5:
            shield_up = _set_shield(ex, state, True)
            try:
                _look_at_hostile(ex, h_pos, shield_yaw, prefer_yaw=use_bisector)
                yield from _control_step(ex, back=True, yaw=shield_yaw, ms=500)
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
            # 无盾直面贴脸/起爆苦力怕（<=3.5 格，引信 1.5s）：跳过慢速 A* move_to，立即原生疾跑跳拉脱爆炸半径
            creeper_pref_yaw = (
                _pick_alternate_escape_yaw(ex, px, py, pz, ux, uz, cache, dim)
                if _is_yaw_wall_blocked(ex, px, py, pz, ux, uz, cache, dim)
                else away_yaw
            )
            safe_creeper_yaw = _pick_cliff_safe_yaw(ex, px, py, pz, creeper_pref_yaw, cache, dim)
            if safe_creeper_yaw is not None:
                ok_cb, _ = yield from _control_step(
                    ex, forward=True, sprint=True, jump=True, yaw=safe_creeper_yaw, ms=900,
                )
                if ok_cb:
                    st_mid = _refresh_status(ex, state)
                    if st_mid.get("alive") is False:
                        return False
                    mid_hostile = st_mid.get("nearest_hostile") or {}
                    if not mid_hostile or (
                        isinstance(mid_hostile.get("dist"), (int, float))
                        and not isinstance(mid_hostile.get("dist"), bool)
                        and float(mid_hostile["dist"]) >= 9.0
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
        if is_ranged and h_dist < 16.0:
            already_los_blocked = bool(
                h_pos is not None and _is_ranged_los_blocked(ex, px, py, pz, h_pos, cache, dim)
            )
            # 若刚建完挡箭墙或已处于实心掩体后方（射界已切断且敌怪距 >=4.5 格尚不能瞬间绕柱贴脸），
            # 沿背敌避墙安全轴带跳疾跑退离；若被角落/墙体卡住导致位移 <0.5 格，记录受阻朝向并落入完整换向脱离逻辑。
            if already_los_blocked and h_dist >= 4.5:
                hold_pref_yaw = (
                    _pick_alternate_escape_yaw(ex, px, py, pz, ux, uz, cache, dim)
                    if _is_yaw_wall_blocked(ex, px, py, pz, ux, uz, cache, dim)
                    else away_yaw
                )
                safe_hold_yaw = _pick_cliff_safe_yaw(ex, px, py, pz, hold_pref_yaw, cache, dim)
                if safe_hold_yaw is not None:
                    yield from _control_step(
                        ex,
                        record_extra={"skeleton_cover": True},
                        forward=True,
                        sprint=True,
                        jump=True,
                        yaw=safe_hold_yaw,
                        ms=450,
                    )
                    st_hold = _refresh_status(ex, state)
                    if st_hold.get("alive") is False:
                        return False
                    hp_after = st_hold.get("pos") or [px, py, pz]
                    if math.hypot(float(hp_after[0]) - px, float(hp_after[2]) - pz) >= 0.5:
                        return True
                    px, py, pz = float(hp_after[0]), float(hp_after[1]), float(hp_after[2])
                    burst_blocked = True
                    tried_yaws.append(safe_hold_yaw)
            cover = (
                _find_skeleton_cover_cell(ex, px, py, pz, h_pos, cache, dim)
                if (
                    not already_los_blocked
                    and h_dist < 10.0
                    and (has_shield or not has_equipped_armor(state) or hp_val < hp_risk)
                )
                else None
            )
            if cover is not None:
                cx, cy, cz = cover[0] + 0.5, float(cover[1]), cover[2] + 0.5
                same_y = cover[1] == block_cell([px, py, pz])[1]
                horiz_to_cover = math.hypot(cx - px, cz - pz)
                if has_shield and same_y and horiz_to_cover <= 2.2:
                    # 持盾进入同高近处掩体：用 /control 锁面敌 yaw 横移/倒退，避免 move_to 转身把后背露给骷髅
                    move_keys = _strafe_control_keys(cx - px, cz - pz, shield_yaw)
                    shield_up = _set_shield(ex, state, True)
                    try:
                        _look_at_hostile(ex, h_pos, shield_yaw, prefer_yaw=use_bisector)
                        ok_strafe, _ = yield from _control_step(
                            ex,
                            record_extra={"skeleton_cover": True},
                            yaw=shield_yaw,
                            ms=650,
                            **move_keys,
                        )
                    finally:
                        if shield_up:
                            _set_shield(ex, state, False)
                    if ok_strafe:
                        st_after_cov = _refresh_status(ex, state)
                        ap = st_after_cov.get("pos") or [px, py, pz]
                        if (
                            math.hypot(float(ap[0]) - px, float(ap[2]) - pz) >= 0.5
                            or (not already_los_blocked and _is_ranged_los_blocked(ex, float(ap[0]), float(ap[1]), float(ap[2]), h_pos, cache, dim))
                        ):
                            return True
                moved_cover = yield from _move_to_bounded(
                    ex, [cx, cy, cz], rng=0, timeout=5.0, skeleton_cover=True,
                )
                if moved_cover:
                    st_after_cov = _refresh_status(ex, state)
                    ap = st_after_cov.get("pos") or [px, py, pz]
                    if (
                        math.hypot(float(ap[0]) - px, float(ap[2]) - pz) >= 0.5
                        or (not already_los_blocked and _is_ranged_los_blocked(ex, float(ap[0]), float(ap[1]), float(ap[2]), h_pos, cache, dim))
                    ):
                        return True
            if has_shield:
                # 持盾面对远程怪且无近处天然掩体：锁威胁方向举盾后撤格挡箭矢，再收盾拉脱
                shield_up = _set_shield(ex, state, True)
                try:
                    _look_at_hostile(ex, h_pos, shield_yaw, prefer_yaw=use_bisector)
                    yield from _control_step(ex, back=True, yaw=shield_yaw, ms=550)
                finally:
                    if shield_up:
                        _set_shield(ex, state, False)
                st_mid = _refresh_status(ex, state)
                mid_pos = st_mid.get("pos")
                if mid_pos and len(mid_pos) >= 3:
                    px, py, pz = float(mid_pos[0]), float(mid_pos[1]), float(mid_pos[2])
            elif (not already_los_blocked and (not has_equipped_armor(state) or hp_val < hp_risk)):
                barricaded = yield from _try_place_ranged_barricade(ex, state, st, h_pos, cache, dim)
                if barricaded:
                    return True
        elif has_shield and hurt_attacker_id is not None and h_dist < 10.0:
            # 刚被偷袭受伤且持盾：立刻转向攻击者举盾短后撤挡下第二击，再收盾拉脱
            shield_up = _set_shield(ex, state, True)
            try:
                _look_at_hostile(ex, h_pos, shield_yaw, prefer_yaw=True)
                yield from _control_step(ex, back=True, yaw=shield_yaw, ms=500)
            finally:
                if shield_up:
                    _set_shield(ex, state, False)
            st_mid = _refresh_status(ex, state)
            mid_pos = st_mid.get("pos")
            if mid_pos and len(mid_pos) >= 3:
                px, py, pz = float(mid_pos[0]), float(mid_pos[1]), float(mid_pos[2])
        if h_dist < 2.5:
            # 贴脸挨打（<2.5 格）时跳过慢速 A* 寻路，先检测正前墙体并绕开，再用防跳崖检查的原生疾跑跳脱离近战命中圈
            burst_pref_yaw = (
                _pick_alternate_escape_yaw(ex, px, py, pz, ux, uz, cache, dim)
                if _is_yaw_wall_blocked(ex, px, py, pz, ux, uz, cache, dim)
                else away_yaw
            )
            safe_burst_yaw = _pick_cliff_safe_yaw(ex, px, py, pz, burst_pref_yaw, cache, dim)
            before_burst = (px, py, pz)
            ok_burst = False
            if safe_burst_yaw is not None:
                ok_burst, _ = yield from _control_step(ex, forward=True, sprint=True, jump=True, yaw=safe_burst_yaw, ms=900)
            if ok_burst:
                st_mid = _refresh_status(ex, state)
                if st_mid.get("alive") is False:
                    return False
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
                        if safe_burst_yaw is not None:
                            tried_yaws.append(safe_burst_yaw)
                # 首段疾跑后若近怪仍在追杀半径内（<=5.5 格，远程 <=6.5 格，或撞墙受阻）：
                # 立即以最新坐标重算背敌向量并最多连补 2 次原生疾跑跳，严禁在掠夺者/僵尸距 3.5m 时直接落入慢速 move_to 原地挨打
                for _ in range(2):
                    if isinstance(mid_hostile, dict) and mid_hostile:
                        in_w_mid = bool(st_mid.get("in_water") or st_mid.get("submerged"))
                        ux, uz, h_pos = _resolve_flee_vector(
                            ex, px, py, pz, mid_hostile, cache, dim, state=state,
                            avoid_water=not in_w_mid, in_water=in_w_mid,
                        )
                        away_yaw = math.atan2(-ux, -uz)
                    mid_dist_val = mid_hostile.get("dist") if isinstance(mid_hostile, dict) else None
                    mid_name = bare(mid_hostile.get("name")).strip().lower() if isinstance(mid_hostile, dict) else ""
                    close_cap = 6.5 if mid_name in _RANGED_HOSTILES else 5.5
                    if not (
                        isinstance(mid_dist_val, (int, float))
                        and not isinstance(mid_dist_val, bool)
                        and float(mid_dist_val) <= close_cap
                    ):
                        break
                    alt_yaw = (
                        _pick_alternate_escape_yaw(ex, px, py, pz, ux, uz, cache, dim, tried_yaws=tried_yaws)
                        if (burst_blocked or _is_yaw_wall_blocked(ex, px, py, pz, ux, uz, cache, dim))
                        else away_yaw
                    )
                    safe_alt_yaw = _pick_cliff_safe_yaw(ex, px, py, pz, alt_yaw, cache, dim)
                    if safe_alt_yaw is None:
                        break
                    before_alt = (px, py, pz)
                    ok_alt, _ = yield from _control_step(
                        ex, forward=True, sprint=True, jump=True, yaw=safe_alt_yaw, ms=800,
                    )
                    if not ok_alt:
                        break
                    st_mid = _refresh_status(ex, state)
                    if st_mid.get("alive") is False:
                        return False
                    mid_hostile = st_mid.get("nearest_hostile") or {}
                    if not mid_hostile or (
                        isinstance(mid_hostile.get("dist"), (int, float))
                        and not isinstance(mid_hostile.get("dist"), bool)
                        and float(mid_hostile["dist"]) >= safe_mid_clear
                    ):
                        return True
                    alt_pos = st_mid.get("pos")
                    moved_alt = 0.0
                    if isinstance(alt_pos, (list, tuple)) and len(alt_pos) >= 3:
                        px, py, pz = float(alt_pos[0]), float(alt_pos[1]), float(alt_pos[2])
                        moved_alt = math.dist(before_alt, (px, py, pz))
                    if moved_alt < 1.5:
                        burst_blocked = True
                        tried_yaws.append(safe_alt_yaw)
                    else:
                        burst_blocked = False
                    if moved_alt < 0.5:
                        break
                    if isinstance(mid_hostile, dict) and mid_hostile:
                        in_w_mid = bool(st_mid.get("in_water") or st_mid.get("submerged"))
                        ux, uz, h_pos = _resolve_flee_vector(
                            ex, px, py, pz, mid_hostile, cache, dim, state=state,
                            avoid_water=not in_w_mid, in_water=in_w_mid,
                        )
                        away_yaw = math.atan2(-ux, -uz)
                    else:
                        ux, uz = -math.sin(safe_alt_yaw), -math.cos(safe_alt_yaw)
                        away_yaw = safe_alt_yaw

    # 高速追击怪（蜘蛛/洞穴蜘蛛/小僵尸，移速高于 move_to 步行 4.3m/s）：禁止用 5s 慢速 move_to 站桩挨咬，直接连续防跳崖疾跑跳拉脱
    if h_name in _FAST_PURSUERS and h_dist < 8.5:
        sprint_supported = True
        fast_safe_dist = max(10.0, effective_hostile_flee_dist(state, h_name))
        consecutive_stalls = 0
        for _ in range(3):
            if _is_yaw_wall_blocked(ex, px, py, pz, ux, uz, cache, dim) or burst_blocked:
                cand_yaw = _pick_alternate_escape_yaw(ex, px, py, pz, ux, uz, cache, dim, tried_yaws=tried_yaws)
            else:
                cand_yaw = away_yaw
            safe_fast_yaw = _pick_cliff_safe_yaw(ex, px, py, pz, cand_yaw, cache, dim)
            if safe_fast_yaw is None:
                sprint_supported = False
                break
            before_fast = (px, py, pz)
            ok_fast, err_fast = yield from _control_step(
                ex, forward=True, sprint=True, jump=True, yaw=safe_fast_yaw, ms=900,
            )
            if not ok_fast and err_fast == "unsupported_api":
                sprint_supported = False
                break
            st_mid = _refresh_status(ex, state)
            if st_mid.get("alive") is False:
                return False
            mid_pos = st_mid.get("pos")
            moved_fast = 0.0
            if isinstance(mid_pos, (list, tuple)) and len(mid_pos) >= 3:
                px, py, pz = float(mid_pos[0]), float(mid_pos[1]), float(mid_pos[2])
                moved_fast = math.dist(before_fast, (px, py, pz))
                burst_blocked = moved_fast < 1.5
                if burst_blocked:
                    tried_yaws.append(safe_fast_yaw)
            if moved_fast < 0.5:
                consecutive_stalls += 1
                if consecutive_stalls >= 2:
                    # 连续 2 个不同角度疾跑均卡在树干/河岸坑位 <0.5 格：交给 A* move_to 绕障爬出，绝不原地站桩挨咬
                    sprint_supported = False
                    break
            else:
                consecutive_stalls = 0
            mid_hostile = st_mid.get("nearest_hostile") or {}
            if not isinstance(mid_hostile, dict) or not mid_hostile:
                return True
            md = mid_hostile.get("dist")
            if isinstance(md, (int, float)) and not isinstance(md, bool) and float(md) >= fast_safe_dist:
                return True
            ux, uz, h_pos = _resolve_flee_vector(ex, px, py, pz, mid_hostile, cache, dim, state=state)
            away_yaw = math.atan2(-ux, -uz)
        if sprint_supported:
            st_after_fast = _refresh_status(ex, state)
            ah = st_after_fast.get("nearest_hostile") or {}
            if not isinstance(ah, dict) or not ah:
                return True
            ad = ah.get("dist")
            if isinstance(ad, (int, float)) and not isinstance(ad, bool) and float(ad) >= effective_hostile_flee_dist(state, h_name):
                return True
            return math.dist((px, py, pz), [float(pos[0]), float(pos[1]), float(pos[2])]) >= 3.0

    # 水中逃敌严禁直接走 5s 阻塞 move_to 跨河寻路（Mineflayer 在水中极易卡死原地上下扑腾 4-5 秒被溺尸三叉戟点杀）：
    # 改用短步疾跑游泳（800ms）优先游向远离敌怪的安全干岸，紧贴干岸 <=2.5 格时才允许 2.5s 短距 move_to 登岸。
    st_cur = state.snapshot() or st
    if st_cur.get("in_water") or st_cur.get("submerged"):
        water_swim_supported = True
        water_safe_dist = max(9.0 if is_creeper else 7.0, effective_hostile_flee_dist(state, h_name))
        tried_shores_flee: set[Cell] = set()
        for _ in range(3):
            cur_hostile = st_cur.get("nearest_hostile") or hostile
            cur_ents = _collect_nearby_hostiles(ex, px, py, pz, cur_hostile if isinstance(cur_hostile, dict) else {}, radius=18)
            cur_threats: list[tuple[float, float, str, float, Optional[int]]] = []
            for ent in cur_ents:
                ep = _valid_vec3(ent.get("pos"))
                if ep is None:
                    continue
                cur_threats.append((
                    ep[0], ep[2],
                    bare(ent.get("name")).strip().lower(),
                    math.hypot(px - ep[0], pz - ep[2]),
                    ent.get("id") if isinstance(ent.get("id"), int) and not isinstance(ent.get("id"), bool) else None,
                ))
            shore = _find_safe_dry_shore_away(ex, st_cur, cache, dim, cur_threats, tried_shores=tried_shores_flee)
            if shore is not None and math.hypot((shore[0] + 0.5) - px, (shore[2] + 0.5) - pz) <= 2.5:
                tried_shores_flee.add(shore)
                moved_bank = yield from _move_to_bounded(
                    ex, [shore[0] + 0.5, float(shore[1]), shore[2] + 0.5], rng=1, timeout=2.5,
                )
                st_cur = _refresh_status(ex, state)
                if st_cur.get("alive") is False:
                    return False
                cpos = st_cur.get("pos")
                if isinstance(cpos, (list, tuple)) and len(cpos) >= 3:
                    px, py, pz = float(cpos[0]), float(cpos[1]), float(cpos[2])
                if not st_cur.get("in_water") and not st_cur.get("submerged"):
                    nh_bank = st_cur.get("nearest_hostile") or {}
                    if not isinstance(nh_bank, dict) or not nh_bank:
                        return True
                    bd = nh_bank.get("dist")
                    if isinstance(bd, (int, float)) and not isinstance(bd, bool) and float(bd) >= water_safe_dist:
                        return True
                    ux, uz, h_pos = _resolve_flee_vector(
                        ex, px, py, pz, nh_bank, cache, dim, state=state, avoid_water=True, in_water=False,
                    )
                    away_yaw = math.atan2(-ux, -uz)
                    break
                if moved_bank:
                    continue
            swim_yaw = away_yaw
            if shore is not None:
                sx_dx, sx_dz = (shore[0] + 0.5) - px, (shore[2] + 0.5) - pz
                if math.hypot(sx_dx, sx_dz) > 1e-4:
                    shore_yaw = math.atan2(-sx_dx, -sx_dz)
                    if not any(_yaw_diff(shore_yaw, ty) < 0.65 for ty in tried_yaws):
                        swim_yaw = shore_yaw
            if swim_yaw == away_yaw and (burst_blocked or _is_yaw_wall_blocked(ex, px, py, pz, ux, uz, cache, dim)):
                swim_yaw = _pick_alternate_escape_yaw(ex, px, py, pz, ux, uz, cache, dim, tried_yaws=tried_yaws)
            before_sw = (px, py, pz)
            ok_sw, err_sw = yield from _control_step(
                ex, forward=True, sprint=True, jump=True, yaw=swim_yaw, ms=800,
            )
            if not ok_sw and err_sw == "unsupported_api":
                water_swim_supported = False
                break
            st_cur = _refresh_status(ex, state)
            if st_cur.get("alive") is False:
                return False
            cpos = st_cur.get("pos")
            moved_sw = 0.0
            if isinstance(cpos, (list, tuple)) and len(cpos) >= 3:
                px, py, pz = float(cpos[0]), float(cpos[1]), float(cpos[2])
                moved_sw = math.dist(before_sw, (px, py, pz))
            if not ok_sw or moved_sw < 0.5:
                burst_blocked = True
                tried_yaws.append(swim_yaw)
            else:
                burst_blocked = False
            nh_sw = st_cur.get("nearest_hostile") or {}
            if not st_cur.get("in_water") and not st_cur.get("submerged"):
                if not isinstance(nh_sw, dict) or not nh_sw:
                    return True
                sd = nh_sw.get("dist")
                if isinstance(sd, (int, float)) and not isinstance(sd, bool) and float(sd) >= water_safe_dist:
                    return True
                ux, uz, h_pos = _resolve_flee_vector(
                    ex, px, py, pz, nh_sw, cache, dim, state=state, avoid_water=True, in_water=False,
                )
                away_yaw = math.atan2(-ux, -uz)
                break
            if not isinstance(nh_sw, dict) or not nh_sw:
                return True
            sd = nh_sw.get("dist")
            if isinstance(sd, (int, float)) and not isinstance(sd, bool) and float(sd) >= water_safe_dist and not st_cur.get("submerged"):
                return True
            ux, uz, h_pos = _resolve_flee_vector(
                ex, px, py, pz, nh_sw, cache, dim, state=state, avoid_water=False, in_water=True,
            )
            away_yaw = math.atan2(-ux, -uz)
        if water_swim_supported and (st_cur.get("in_water") or st_cur.get("submerged")):
            nh_end = st_cur.get("nearest_hostile") or {}
            if not isinstance(nh_end, dict) or not nh_end:
                return True
            ed = nh_end.get("dist")
            if isinstance(ed, (int, float)) and not isinstance(ed, bool) and float(ed) >= effective_hostile_flee_dist(state, h_name):
                return True
            return math.dist((px, py, pz), [float(pos[0]), float(pos[1]), float(pos[2])]) >= 2.5

    # 若掩体退离/前序疾跑刚撞墙受阻（burst_blocked）且敌怪仍逼近 <=5.0 格（如矿洞拐角骷髅距 3m）：
    # 先沿已避开 tried_yaws 的防跳崖替代角疾跑跳脱困，避免直接落入无路径 move_to 原地挨箭
    if burst_blocked and not (st_cur.get("in_water") or st_cur.get("submerged")):
        cur_nh_blk = st_cur.get("nearest_hostile") or hostile
        cur_d_blk = cur_nh_blk.get("dist") if isinstance(cur_nh_blk, dict) else h_dist
        if isinstance(cur_d_blk, (int, float)) and not isinstance(cur_d_blk, bool) and float(cur_d_blk) <= 5.0:
            blk_alt_yaw = _pick_alternate_escape_yaw(ex, px, py, pz, ux, uz, cache, dim, tried_yaws=tried_yaws)
            safe_blk_yaw = _pick_cliff_safe_yaw(ex, px, py, pz, blk_alt_yaw, cache, dim)
            if safe_blk_yaw is not None:
                before_blk = (px, py, pz)
                ok_blk, _ = yield from _control_step(
                    ex, forward=True, sprint=True, jump=True, yaw=safe_blk_yaw, ms=800,
                )
                if ok_blk:
                    st_blk = _refresh_status(ex, state)
                    if st_blk.get("alive") is False:
                        return False
                    st_cur = st_blk
                    bpos = st_blk.get("pos")
                    if isinstance(bpos, (list, tuple)) and len(bpos) >= 3:
                        px, py, pz = float(bpos[0]), float(bpos[1]), float(bpos[2])
                        if math.dist(before_blk, (px, py, pz)) < 1.5:
                            tried_yaws.append(safe_blk_yaw)
                        else:
                            burst_blocked = False
                    nh_blk = st_blk.get("nearest_hostile") or {}
                    if not isinstance(nh_blk, dict) or not nh_blk:
                        return True
                    bd_blk = nh_blk.get("dist")
                    safe_blk_dist = max(7.0 if is_creeper else 6.0, effective_hostile_flee_dist(state, h_name))
                    if isinstance(bd_blk, (int, float)) and not isinstance(bd_blk, bool) and float(bd_blk) >= safe_blk_dist:
                        return True
                    ux, uz, h_pos = _resolve_flee_vector(
                        ex, px, py, pz, nh_blk, cache, dim, state=state, avoid_water=True, in_water=False,
                    )
                    away_yaw = math.atan2(-ux, -uz)

    flee_stride = 16.0 if is_creeper else 12.0
    if not (st_cur.get("in_water") or st_cur.get("submerged")):
        # 干地逃离若远端落点伸入河道，收缩落点到入水前的干地岸段，防止 move_to 把角色带下水
        if _path_enters_water(ex, px, py, pz, ux, uz, cache, dim, probes=(6, 8, 10, 12, int(flee_stride))):
            for cand_stride in (10.0, 8.0, 6.0, 4.0):
                if cand_stride < flee_stride and not _path_enters_water(
                    ex, px, py, pz, ux, uz, cache, dim, probes=tuple(range(2, int(cand_stride) + 1, 2)),
                ):
                    flee_stride = cand_stride
                    break
    # 脱险寻路超时上限压紧至 2.2s（原 5.0s）：一旦地形/矿洞墙体导致 pathfinder 原地重算卡滞，
    # 最多 2.2s 即中止并转入实时换向疾跑跳，严禁原地站桩 5s 连吃 3~4 箭/刀致死。
    moved = yield from _move_to_bounded(
        ex, [round(px + ux * flee_stride, 2), py, round(pz + uz * flee_stride, 2)], rng=2, timeout=2.2,
    )
    if not moved:
        st_retry = _refresh_status(ex, state)
        rpos = st_retry.get("pos")
        if isinstance(rpos, (list, tuple)) and len(rpos) >= 3:
            px, py, pz = float(rpos[0]), float(rpos[1]), float(rpos[2])
        # move_to 超时/失败期间角色可能已移动数格，兜底疾跑前必须在新坐标重扫敌群，避免盲冲苦力怕/骷髅
        retry_hostile = st_retry.get("nearest_hostile") or {}
        in_w_retry = bool(st_retry.get("in_water") or st_retry.get("submerged"))
        if isinstance(retry_hostile, dict) and retry_hostile:
            ux, uz, h_pos = _resolve_flee_vector(
                ex, px, py, pz, retry_hostile, cache, dim, state=state,
                avoid_water=not in_w_retry, in_water=in_w_retry,
            )
            away_yaw = math.atan2(-ux, -uz)
        retry_yaw = (
            _pick_alternate_escape_yaw(ex, px, py, pz, ux, uz, cache, dim, tried_yaws=tried_yaws)
            if (burst_blocked or _is_yaw_wall_blocked(ex, px, py, pz, ux, uz, cache, dim))
            else away_yaw
        )
        safe_retry_yaw = _pick_cliff_safe_yaw(ex, px, py, pz, retry_yaw, cache, dim)
        if safe_retry_yaw is not None:
            yield from _control_step(ex, forward=True, sprint=True, jump=True, yaw=safe_retry_yaw, ms=800)
        elif has_shield:
            # 四周有悬崖/岩浆无法疾跑且寻路失败：持盾面向敌群最优方向短暂格挡保命
            shield_up = _set_shield(ex, state, True)
            try:
                _look_at_hostile(ex, h_pos, shield_yaw, prefer_yaw=True)
                yield from _control_step(ex, back=True, yaw=shield_yaw, ms=450)
            finally:
                if shield_up:
                    _set_shield(ex, state, False)

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
            # move_to 寻路禁疾跑，若结束时高速敌怪仍紧贴 <5.5 格，重算实时敌群方向后补一次防跳崖疾跑跳拉脱
            ap = after_pos_now if (isinstance(after_pos_now, (list, tuple)) and len(after_pos_now) >= 3) else [px, py, pz]
            f_ux, f_uz, _ = _resolve_flee_vector(ex, float(ap[0]), float(ap[1]), float(ap[2]), after_hostile, cache, dim, state=state)
            follow_away_yaw = math.atan2(-f_ux, -f_uz)
            safe_follow_yaw = _pick_cliff_safe_yaw(ex, float(ap[0]), float(ap[1]), float(ap[2]), follow_away_yaw, cache, dim)
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
