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
    # 脱困移动必须使用 stop_on=("death",)：逃离怪群/岩浆/火焰时挨一下或受灼烧绝不能中途停步
    try:
        try:
            res = ex.move_to(pos=target_pos, rng=rng, timeout=timeout, retries=0, stop_on=("death",))
        except TypeError:
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
    elif h_dist < 2.5:
        # 贴脸挨打（<2.5 格）时跳过慢速 A* 寻路，先用原生疾跑跳脱离近战命中圈再补 move_to
        before_burst = (px, py, pz)
        ok_burst, _ = yield from _control_step(ex, forward=True, sprint=True, jump=True, yaw=away_yaw, ms=900)
        if ok_burst:
            st_mid = _refresh_status(ex, state)
            mid_hostile = st_mid.get("nearest_hostile") or {}
            safe_mid = 7.0 if is_creeper else float(getattr(getattr(state, "config", None), "hostile_flee_dist", 6.0) or 6.0)
            if not mid_hostile or (
                isinstance(mid_hostile.get("dist"), (int, float)) and float(mid_hostile["dist"]) >= safe_mid + 2.0
            ):
                return True
            mid_pos = st_mid.get("pos")
            if mid_pos and len(mid_pos) >= 3:
                px, py, pz = float(mid_pos[0]), float(mid_pos[1]), float(mid_pos[2])
                if math.dist(before_burst, (px, py, pz)) < 1.5:
                    burst_blocked = True

    flee_stride = 16.0 if is_creeper else 14.0
    moved = yield from _move_to_bounded(
        ex, [round(px + ux * flee_stride, 2), py, round(pz + uz * flee_stride, 2)], rng=2, timeout=12.0,
    )
    if not moved:
        retry_yaw = (
            _pick_alternate_escape_yaw(ex, px, py, pz, ux, uz, cache, dim)
            if burst_blocked
            else away_yaw
        )
        yield from _control_step(ex, forward=True, sprint=True, jump=True, yaw=retry_yaw, ms=800)

    st_after = _refresh_status(ex, state)
    after_hostile = st_after.get("nearest_hostile") or {}
    if not after_hostile:
        return True
    flee_dist = float(getattr(getattr(state, "config", None), "hostile_flee_dist", 6.0) or 6.0)
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
