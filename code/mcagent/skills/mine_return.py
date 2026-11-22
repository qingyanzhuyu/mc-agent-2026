"""地下矿道回地表/营地与就地临时工作台 L2 原语。

遵守 AGENTS.md §9 全部约束：
- 纯 L2 原语，禁止 `@register`、禁止继承 `Skill`、禁止导入 `mcagent.loop`；
- 每次阻塞动作立即 `yield rec(...)`，复用 `skills.sightline` 与 `voxel_cache`。
"""
from __future__ import annotations

import math
from collections.abc import Generator, Mapping, Sequence
from typing import Any, Optional

from ..client import ApiError
from ..runtime import EnvBlocked, Executor, Replan, State
from .base import failure, rec
from .items import (
    AIR,
    MINE_TRAILS,
    NAV_FAIL_CODES,
    PICKAXE_ORDER,
    STONE_OR_BETTER_PICKS,
    bare,
    best_pickaxe,
    is_clear_space,
    is_safe_solid_floor,
    pick_placeable,
    total_placeable,
)
from .mining import SOLID_SUPPORT, prepare_mining_pick
from .recipes import RECIPES, craft_recipe
from .sightline import STATION_EYE_REACH, block_cell, can_reach_block, eye_to_block_dist
from .voxel_cache import client_get_blocks_batch, has_batch_get_blocks, voxel_cache_for
from .world_memo import ALL_LOG_BLOCKS, memo_for

Cell = tuple[int, int, int]
_PLANK_ITEMS: tuple[str, ...] = tuple(item for item in RECIPES if item.endswith("_planks"))
_ANY_PICKAXE: frozenset[str] = frozenset(PICKAXE_ORDER)
_SOFT_HAND_DIG: frozenset[str] = frozenset({
    "dirt", "grass_block", "coarse_dirt", "rooted_dirt", "podzol", "mud", "clay", "gravel", "sand", "red_sand", "snow_block", "moss_block",
})
_VALID_TABLE_SUPPORTS: frozenset[str] = SOLID_SUPPORT | frozenset(_PLANK_ITEMS) | frozenset({
    "grass_block", "dirt", "coarse_dirt", "sandstone", "oak_log", "birch_log", "spruce_log",
})
_UNSAFE_CLIMB: frozenset[str] = frozenset({
    "water", "lava", "flowing_water", "flowing_lava", "bedrock", "obsidian", "crying_obsidian", "crafting_table", "chest", "furnace",
})
_FALLING_ROOF: frozenset[str] = frozenset({"sand", "red_sand", "gravel", "water", "lava", "flowing_water", "flowing_lava"})
_LAVA_FLUIDS: frozenset[str] = frozenset({"lava", "flowing_lava"})
_FLUID_FACES: tuple[Cell, ...] = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
_HAND_ESCAPE_ROCKS: frozenset[str] = frozenset({
    "stone", "cobblestone", "andesite", "diorite", "granite", "tuff", "deepslate",
    "cobbled_deepslate", "sandstone", "red_sandstone", "coal_ore", "copper_ore",
    "iron_ore", "deepslate_coal_ore", "deepslate_copper_ore", "deepslate_iron_ore",
    "calcite", "dripstone_block", "netherrack",
})
_ROUTE_ERRS: frozenset[str] = frozenset((*NAV_FAIL_CODES, "timeout"))
_CEILING_CHECK_MAX_Y: float = 52.0


def _is_preemption(err: BaseException) -> bool:
    return isinstance(err, EnvBlocked) or failure(err)[0] == "interrupted" or "抢占" in str(err)


def _bounded_move_to(ex: Executor, pos: Sequence[float | int], *, rng: int = 2, timeout: float = 30.0) -> dict:
    dest = list(pos)
    try:
        return ex.move_to(pos=dest, rng=rng, timeout=timeout, retries=0)
    except TypeError:
        return ex.move_to(pos=dest, rng=rng)


def _cur_status(ex: Executor, state: State) -> tuple[dict[str, Any], list[float], str]:
    st = state.refresh_status(ex.client, max_age_s=0) or {}
    return st, list(st.get("pos") or state.pos), bare(st.get("dimension")).strip().lower() or "overworld"


def _probe_name(ex: Executor, pos: Sequence[int]) -> str:
    try:
        return bare(ex.client.get_block(list(pos)).get("name"))
    except ApiError as exc:
        if exc.code in ("not_loaded", "not_ready"):
            return ""
        raise


def _reference_surface_y(state: State, dim: str) -> Optional[float]:
    memo = memo_for(state)
    ys: list[float] = []
    shaft = memo.get_mine_shaft(dimension=dim)
    if isinstance(shaft, Mapping) and isinstance(shaft.get("entry"), (list, tuple)) and len(shaft["entry"]) >= 3:
        ys.append(float(shaft["entry"][1]))
    camp = memo.get_camp(dimension=dim)
    if isinstance(camp, Mapping):
        ys.extend(float(camp[k][1]) for k in ("pos", "crafting_table", "mine_entry")
                  if isinstance(camp.get(k), (list, tuple)) and len(camp[k]) >= 3)
    spawn = memo.get_spawn(dimension=dim)
    if isinstance(spawn, (list, tuple)) and len(spawn) >= 3:
        ys.append(float(spawn[1]))
    if MINE_TRAILS and MINE_TRAILS[-1]:
        ys.append(float(MINE_TRAILS[-1][0][1]))
    return max(ys) if ys else None


def _has_solid_mine_ceiling(ex: Executor, pos: Sequence[float | int]) -> bool:
    """检查角色正上方 2..8 格是否存在非树冠实心岩顶/土顶（区分山谷露天河岸与真实山洞/矿道）。"""
    cx, cy, cz = block_cell(pos)
    cells = [(cx, cy + dy, cz) for dy in (2, 3, 4, 5, 6, 7, 8)]
    if has_batch_get_blocks(getattr(ex, "client", None)):
        snap = client_get_blocks_batch(ex.client, cells)
        names = [bare((snap.get(c) or {}).get("name")) for c in cells]
    else:
        names = [_probe_name(ex, c) for c in cells]
    return any(
        n and not is_clear_space(n) and not n.endswith("_leaves") and not n.endswith("_log")
        for n in names
    )


def is_underground_in_mine(ex: Executor, state: State) -> bool:
    """判断角色是否身处主世界地下矿道（低于海平面深矿，或虽低于高坡营地但头顶确有实心岩顶；露天河谷/海滩不误判）。"""
    st = state.st if isinstance(state.st, Mapping) and state.st.get("pos") is not None else state.refresh_status(ex.client, max_age_s=0)
    if not isinstance(st, Mapping) or (bare(st.get("dimension")).strip().lower() or "overworld") != "overworld":
        return False
    pos = st.get("pos") or state.pos
    if not isinstance(pos, (list, tuple)) or len(pos) < 3:
        return False
    py = float(pos[1])
    ref_y = _reference_surface_y(state, "overworld")
    if ref_y is not None and py <= ref_y - 8.0:
        # 低于海平面浅层（Y<=56）直接判地下；Y>56（如 Y=62 河岸/林地而营地在 Y=73 山顶）必须核验头顶实心岩顶，
        # 避免把露天河谷误判为矿井并触发 gather_wood 徒手挖石头爬山。
        if py <= 56.0:
            return True
        return _has_solid_mine_ceiling(ex, pos)
    if py >= _CEILING_CHECK_MAX_Y:
        return False
    return _has_solid_mine_ceiling(ex, pos)


def _shaft_return_waypoints(cur: Sequence[float], entry: Sequence[float]) -> list[list[float]]:
    """沿矿井起止点构造分段上升路点，避免深井跨 30+ 格一次寻路超时或 path_not_found。"""
    dest = [float(entry[0]) + (0.5 if float(entry[0]).is_integer() else 0.0),
            float(entry[1]),
            float(entry[2]) + (0.5 if float(entry[2]).is_integer() else 0.0)]
    dist = math.dist(cur, entry)
    if dist <= 18.0:
        return [dest]
    segs = max(2, min(5, int(math.ceil(dist / 12.0))))
    return [[round(float(cur[i]) + (dest[i] - float(cur[i])) * (k / segs), 2) for i in range(3)]
            for k in range(1, segs + 1)]


def _step_touches_lava(ex: Executor, cells: Sequence[Cell]) -> bool:
    """检查待挖阶梯方块的六邻面是否紧邻岩浆，避免向上开阶时挖破隐蔽岩浆池。"""
    checked: set[Cell] = set()
    for cx, cy, cz in cells:
        for ox, oy, oz in _FLUID_FACES:
            nb = (cx + ox, cy + oy, cz + oz)
            if nb in checked:
                continue
            checked.add(nb)
            if _probe_name(ex, nb) in _LAVA_FLUIDS:
                return True
    return False


def _climb_out_stairs(
    ex: Executor,
    state: State,
    *,
    target_y: float,
    max_steps: int = 64,
    allow_hand_rock: bool = False,
) -> Generator[dict, None, bool]:
    """当既有矿井路径不通时，在安全实心岩体中向上开凿有界 1×2 阶梯脱困（支持垫块跨空与无镐防软锁徒手破岩）。"""
    vc = voxel_cache_for(state)
    failed_steps: set[Cell] = set()
    no_rise_streak = 0
    for _ in range(max(1, int(max_steps))):
        st, cur, dim = _cur_status(ex, state)
        if st.get("alive") is False or float(cur[1]) >= target_y - 2.0:
            return float(cur[1]) >= target_y - 2.0
        has_pick = best_pickaxe(state) is not None
        can_place = total_placeable(state) >= 1
        ix, fy, iz = block_cell(cur)
        chosen: Optional[tuple[int, int, bool, bool]] = None  # (nx, nz, need_place_step, hand_rock_mode)

        def _can_clear(names: tuple[str, str, str], *, hand_rock: bool) -> bool:
            if any(n in _UNSAFE_CLIMB for n in names):
                return False
            if has_pick:
                return True
            allowed = (_SOFT_HAND_DIG | _HAND_ESCAPE_ROCKS) if hand_rock else _SOFT_HAND_DIG
            return all((not n) or is_clear_space(n) or n in allowed or n.endswith("_leaves") for n in names)

        # Pass 1: 紧邻实心台阶 + 常规可挖通道；Pass 2: 空洞处放垫块造台阶；Pass 3: 无镐防软锁允许徒手破岩
        passes: list[tuple[bool, bool]] = [(False, False)]
        if can_place:
            passes.append((True, False))
        if allow_hand_rock and not has_pick:
            passes.append((False, True))
            if can_place:
                passes.append((True, True))

        for need_place, hand_rock in passes:
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, nz = ix + dx, iz + dz
                if (nx, fy, nz) in failed_steps:
                    continue
                floor_n = _probe_name(ex, (nx, fy, nz))
                if need_place:
                    if not floor_n or not is_clear_space(floor_n):
                        continue
                    sub_n = _probe_name(ex, (nx, fy - 1, nz))
                    if not is_safe_solid_floor(sub_n) or sub_n in _UNSAFE_CLIMB:
                        continue
                else:
                    # 必须校验实心立足地面（过滤 leaf_litter/short_grass/花草等地被覆盖物），避免误当台阶原地反复横跳
                    if not is_safe_solid_floor(floor_n) or floor_n in _UNSAFE_CLIMB:
                        continue
                head_n = _probe_name(ex, (ix, fy + 2, iz))
                s1_n = _probe_name(ex, (nx, fy + 1, nz))
                s2_n = _probe_name(ex, (nx, fy + 2, nz))
                roof_n = _probe_name(ex, (nx, fy + 3, nz))
                if roof_n in _FALLING_ROOF or not _can_clear((head_n, s1_n, s2_n), hand_rock=hand_rock):
                    continue
                dig_cells = ((ix, fy + 2, iz), (nx, fy + 1, nz), (nx, fy + 2, nz))
                if _step_touches_lava(ex, dig_cells):
                    continue
                chosen = (nx, nz, need_place, hand_rock)
                break
            if chosen is not None:
                break
        if chosen is None:
            return False
        nx, nz, need_place, hand_rock = chosen
        if need_place:
            step_cell = [nx, fy, nz]
            blk = pick_placeable(state)
            if not blk:
                return False
            try:
                pres = ex.place(blk, pos=step_cell)
            except Replan as err:
                if _is_preemption(err):
                    raise
                yield rec("place", item=blk, pos=step_cell, error_code=failure(err)[0], reason="mine_return_climb")
                return False
            vc.invalidate(step_cell, dimension=dim); state.refresh_inventory(ex.client)
            yield rec("place", pres, item=blk, pos=step_cell, reason="mine_return_climb")
        for cell in ([ix, fy + 2, iz], [nx, fy + 1, nz], [nx, fy + 2, nz]):
            bn = _probe_name(ex, cell)
            if not bn or is_clear_space(bn):
                continue
            if best_pickaxe(state) is not None:
                try:
                    yield from prepare_mining_pick(ex, state, allowed=_ANY_PICKAXE)
                except Replan as err:
                    if _is_preemption(err):
                        raise
                    return False
            elif bn not in _SOFT_HAND_DIG and not bn.endswith("_leaves") and not (hand_rock and bn in _HAND_ESCAPE_ROCKS):
                return False
            try:
                res = ex.dig(cell, collect=False)
            except Replan as err:
                if _is_preemption(err):
                    raise
                yield rec("dig", pos=cell, error_code=failure(err)[0], reason="mine_return_climb")
                return False
            vc.invalidate(cell, dimension=dim); state.refresh_inventory(ex.client)
            yield rec("dig", res, pos=cell, reason="mine_return_climb")
        up = [nx + 0.5, float(fy + 1), nz + 0.5]
        try:
            res = _bounded_move_to(ex, up, rng=1, timeout=20.0)
        except Replan as err:
            if _is_preemption(err):
                raise
            yield rec("move_to", pos=up, error_code=failure(err)[0], reason="mine_return_climb")
            return False
        yield rec("move_to", res, pos=up, reason="mine_return_climb")
        # 核验实际海拔是否成功抬升 >=0.5 格；若 rng=1 停在同层导致未爬升，封禁该阶并在连续 3 次零抬升后及时退出防空转
        after_pos = res.get("pos") if isinstance(res, Mapping) else None
        if isinstance(after_pos, (list, tuple)) and len(after_pos) >= 3:
            after_y = float(after_pos[1])
        else:
            after_y = float(_cur_status(ex, state)[1][1])
        if after_y < fy + 0.5:
            failed_steps.add((nx, fy, nz))
            no_rise_streak += 1
            if no_rise_streak >= 3:
                return False
        else:
            failed_steps.clear()
            no_rise_streak = 0
    _, cur_end, _ = _cur_status(ex, state)
    return float(cur_end[1]) >= target_y - 2.0


def _try_move_wp(ex: Executor, state: State, wp: Sequence[float], *, reason: str, rng: int = 2, timeout: float = 30.0) -> Generator[dict, None, tuple[bool, list[float]]]:
    try:
        moved = _bounded_move_to(ex, wp, rng=rng, timeout=timeout)
    except Replan as err:
        if _is_preemption(err) or failure(err)[0] not in _ROUTE_ERRS:
            raise
        yield rec("move_to", pos=list(wp), error_code=failure(err)[0], reason=reason)
        return False, _cur_status(ex, state)[1]
    yield rec("move_to", moved, pos=list(wp), reason=reason)
    return True, _cur_status(ex, state)[1]


def return_to_surface_from_mine(
    ex: Executor, state: State, *, target_y: Optional[float] = None, max_climb_steps: int = 64,
) -> Generator[dict, None, bool]:
    """沿 `MINE_TRAILS` / `primary_mine_shaft` / 营地锚点安全返回地表，失败时尝试有界阶梯抬升。"""
    st, cur, dim = _cur_status(ex, state)
    if dim != "overworld":
        return True
    memo = memo_for(state)
    ref_y = float(target_y) if target_y is not None else (_reference_surface_y(state, dim) or 62.0)
    if float(cur[1]) >= ref_y - 3.0:
        return True
    if target_y is None and float(cur[1]) >= 60.0 and not _has_solid_mine_ceiling(ex, cur):
        return True

    # 1. 若有当前会话记录的阶梯轨迹且起点在上方，先分段原路退回井口
    if MINE_TRAILS and MINE_TRAILS[-1] and MINE_TRAILS[-1][0][1] > cur[1] + 2.0:
        trail = MINE_TRAILS[-1]
        if math.dist(cur, trail[-1]) <= 24.0:
            wps_t: list[Sequence[float]] = []
            last_wp = list(cur)
            for idx in range(len(trail) - 1, -1, -1):
                pt = trail[idx]
                if idx == 0 or (idx == len(trail) - 1 and math.dist(cur, pt) > 2.5) or math.dist(last_wp, pt) >= 8.0:
                    wps_t.append(pt); last_wp = list(pt)
            for wp in wps_t:
                ok, cur = yield from _try_move_wp(ex, state, wp, reason="return_mine_trail")
                if not ok:
                    break
                if float(cur[1]) >= ref_y - 3.0:
                    if MINE_TRAILS and MINE_TRAILS[-1] is trail:
                        MINE_TRAILS.pop()
                    return True
            if MINE_TRAILS and MINE_TRAILS[-1] is trail and float(cur[1]) >= float(trail[0][1]) - 2.0:
                MINE_TRAILS.pop()

    # 2. 沿持久化 primary_mine_shaft 井口与营地/出生点锚点返回地表
    anchors: list[tuple[str, list[float]]] = []
    shaft = memo.get_mine_shaft(dimension=dim)
    if isinstance(shaft, Mapping) and isinstance(shaft.get("entry"), (list, tuple)) and len(shaft["entry"]) >= 3:
        anchors.append(("primary_mine_shaft", [float(v) for v in shaft["entry"][:3]]))
    camp = memo.get_camp(dimension=dim)
    if isinstance(camp, Mapping):
        for key in ("crafting_table", "pos"):
            val = camp.get(key)
            if isinstance(val, (list, tuple)) and len(val) >= 3:
                anchors.append((f"camp_{key}", [float(v) for v in val[:3]]))
    spawn = memo.get_spawn(dimension=dim)
    if isinstance(spawn, (list, tuple)) and len(spawn) >= 3:
        anchors.append(("spawn", [float(v) for v in spawn[:3]]))

    seen_anchors: set[Cell] = set()
    for label, anchor in anchors:
        acell = block_cell(anchor)
        if acell in seen_anchors or float(anchor[1]) <= float(cur[1]) + 2.0 or math.dist(cur, anchor) > 96.0:
            continue
        seen_anchors.add(acell)
        wps = _shaft_return_waypoints(cur, anchor)
        for idx, wp in enumerate(wps):
            ok, cur = yield from _try_move_wp(ex, state, wp, reason=f"return_{label}")
            if ok:
                if float(cur[1]) >= ref_y - 3.0:
                    return True
                continue
            if idx < len(wps) - 1 and math.dist(cur, anchor) <= 64.0:
                ok2, cur = yield from _try_move_wp(ex, state, wps[-1], reason=f"return_{label}_direct")
                if ok2 and float(cur[1]) >= ref_y - 3.0:
                    return True
            break

    # 3. 若矿井寻路受阻且仍处地下，安全向上挖有界阶梯脱困（初始已无镐时启用防软锁徒手破岩）
    if is_underground_in_mine(ex, state) and max_climb_steps > 0:
        if (yield from _climb_out_stairs(
            ex,
            state,
            target_y=ref_y,
            max_steps=max_climb_steps,
            allow_hand_rock=(best_pickaxe(state) is None),
        )):
            return True
    _, cur_final, _ = _cur_status(ex, state)
    return float(cur_final[1]) >= ref_y - 3.0 or not is_underground_in_mine(ex, state)


def _find_adjacent_table_spot(ex: Executor, state: State) -> Optional[list[int]]:
    _, cur, _ = _cur_status(ex, state)
    x, y, z = block_cell(cur)
    probe = [[x + dx, y + dy + k, z + dz] for dy in (0, 1) for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)) for k in (-1, 0)]
    if has_batch_get_blocks(getattr(ex, "client", None)):
        client_get_blocks_batch(ex.client, probe)
    for dy in (0, 1):
        py = y + dy
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            pos = [x + dx, py, z + dz]
            support = _probe_name(ex, (pos[0], py - 1, pos[2]))
            spot = _probe_name(ex, pos)
            if support in _VALID_TABLE_SUPPORTS and (not spot or spot in AIR):
                return pos
    return None


def _place_local_crafting_table(ex: Executor, state: State) -> Generator[dict, None, Optional[list[int]]]:
    st, cur, dim = _cur_status(ex, state)
    memo = memo_for(state)
    vc = voxel_cache_for(state)
    pos = _find_adjacent_table_spot(ex, state)
    if pos is None and best_pickaxe(state) in STONE_OR_BETTER_PICKS:
        x, y, z = block_cell(cur)
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            cand = [x + dx, y, z + dz]
            sup = _probe_name(ex, (cand[0], y - 1, cand[2]))
            roof = _probe_name(ex, (cand[0], y + 2, cand[2]))
            b0 = _probe_name(ex, cand)
            b1 = _probe_name(ex, (cand[0], y + 1, cand[2]))
            if sup in SOLID_SUPPORT and roof in SOLID_SUPPORT and b0 in (AIR | SOLID_SUPPORT) and b1 in (AIR | SOLID_SUPPORT):
                for cell in ([cand[0], y + 1, cand[2]], cand):
                    if _probe_name(ex, cell) not in AIR:
                        yield from prepare_mining_pick(ex, state)
                        dug = ex.dig(cell, collect=True)
                        vc.invalidate(cell, dimension=dim)
                        yield rec("dig", dug, pos=cell, reason="carve_table_niche")
                pos = cand
                break
    if pos is None:
        return None
    try:
        placed = ex.place("crafting_table", pos=pos)
    except Replan as err:
        if _is_preemption(err):
            raise
        yield rec("place", item="crafting_table", pos=pos, error_code=failure(err)[0])
        return None
    vc.invalidate(pos, dimension=dim)
    yield rec("place", placed, item="crafting_table", pos=pos, reason="temp_crafting_table")
    state.refresh_inventory(ex.client)
    if _probe_name(ex, pos) == "crafting_table":
        memo.record_station("crafting_table", pos, dimension=dim)
        memo.mark_reachable(pos, dimension=dim, category="station", block="crafting_table")
        return pos
    return None


def ensure_crafting_table_ready(
    ex: Executor,
    state: State,
    *,
    reserve_planks: int = 0,
    skip_nearby_scan: bool = False,
) -> Generator[dict, None, Optional[list[int]]]:
    """确保 16 格内有可用工作台：地下/缺台时优先用富余木材就地合成放置，否则沿主矿井回营地工作台。"""
    st, cur, dim = _cur_status(ex, state)
    if not skip_nearby_scan:
        observed = ex.client.find_blocks(types=["crafting_table"], radius=16, limit=5) or {}
        if observed.get("blocks"):
            return None

    underground = is_underground_in_mine(ex, state)
    memo = memo_for(state)
    camp = memo.get_camp(dimension=dim)
    known_tables: list[list[int]] = []
    if isinstance(camp, Mapping) and isinstance(camp.get("crafting_table"), (list, tuple)) and len(camp["crafting_table"]) >= 3:
        known_tables.append([int(v) for v in camp["crafting_table"][:3]])
    for spos in memo.stations("crafting_table", dimension=dim):
        c = [int(v) for v in spos[:3]]
        if c not in known_tables:
            known_tables.append(c)

    # 1. 若背包已有工作台，或扣除目标配方所需木板后仍有 >=4 木板等价木材，优先就地合成并放置登记
    state.refresh_inventory(ex.client)
    planks_now = sum(state.count(p) for p in _PLANK_ITEMS)
    logs_now = sum(state.count(lg) for lg in ALL_LOG_BLOCKS)
    can_make_local = state.count("crafting_table") >= 1 or (planks_now + logs_now * 4 - max(0, int(reserve_planks))) >= 4
    if not underground and not known_tables and not can_make_local:
        return None
    if can_make_local:
        if state.count("crafting_table") < 1:
            if planks_now < 4 + max(0, int(reserve_planks)):
                log = next((lg for lg in ALL_LOG_BLOCKS if state.count(lg) >= 1), None)
                if log is not None:
                    plank = log.removesuffix("_log") + "_planks"
                    yield from craft_recipe(ex, state, plank, goal_qty=state.count(plank) + 4)
                    state.refresh_inventory(ex.client)
            if sum(state.count(p) for p in _PLANK_ITEMS) >= 4 + max(0, int(reserve_planks)):
                yield from craft_recipe(ex, state, "crafting_table", goal_qty=1)
                state.refresh_inventory(ex.client)
        if state.count("crafting_table") >= 1:
            placed_pos = yield from _place_local_crafting_table(ex, state)
            if placed_pos is not None:
                return placed_pos

    # 2. 无法就地放台时：先沿共享主矿井/阶梯返回地表，再走向营地/已知工作台
    if underground:
        yield from return_to_surface_from_mine(ex, state)
        st, cur, dim = _cur_status(ex, state)

    for tpos in sorted(known_tables, key=lambda p: eye_to_block_dist(st, p)):
        if math.dist(cur, tpos) > 96.0:
            continue
        if can_reach_block(ex, st, tpos, reach=STATION_EYE_REACH):
            return tpos
        try:
            moved = _bounded_move_to(ex, tpos, rng=2, timeout=30.0)
        except Replan as err:
            if _is_preemption(err) or failure(err)[0] not in _ROUTE_ERRS:
                raise
            yield rec("move_to", pos=tpos, error_code=failure(err)[0], reason="return_camp_table")
            continue
        yield rec("move_to", moved, pos=tpos, reason="return_camp_table")
        st, cur, dim = _cur_status(ex, state)
        if can_reach_block(ex, st, tpos, reach=STATION_EYE_REACH) or eye_to_block_dist(st, tpos) <= 16.0:
            return tpos
    return None
