"""共享主矿井导航、水体封堵/绕行与分层分支采掘 L2 原语。

所有阻塞动作均通过 `Executor` 发出，遵守"一次动作后立即 yield 一条记录"契约；
禁止 `@register`、禁止继承 `Skill`、禁止导入 `mcagent.loop`。
"""
from __future__ import annotations

import math
from collections.abc import Collection, Generator, Mapping, Sequence
from typing import Any

from ..runtime import EnvBlocked, Executor, Replan, State
from .base import failure, rec
from .items import AIR, NAV_FAIL_CODES, STONE_OR_BETTER_PICKS, bare, block_name, pick_placeable
from .mining import SOLID_SUPPORT, MineStepUnsafe, prepare_mining_pick, safe_mine_column, under_feet
from .sightline import DIG_EYE_REACH, block_cell, can_reach_block, eye_to_block_dist
from .voxel_cache import client_get_blocks_batch, has_batch_get_blocks, voxel_cache_for
from .world_memo import memo_for

Cell = tuple[int, int, int]
IRON_LAYER_Y, DIAMOND_LAYER_Y = 16, -58
CARDINALS: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
WATER_BLOCKS: frozenset[str] = frozenset({"water", "flowing_water", "bubble_column"})
LAVA_BLOCKS: frozenset[str] = frozenset({"lava", "flowing_lava"})
BONUS_BRANCH_ORES: tuple[str, ...] = (
    "coal_ore", "deepslate_coal_ore", "lapis_ore", "deepslate_lapis_ore", "redstone_ore", "deepslate_redstone_ore",
)
PLACE_EYE_REACH: float = DIG_EYE_REACH
_FLUID_BLOCKS = WATER_BLOCKS | LAVA_BLOCKS
_FACES: tuple[Cell, ...] = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
_ROUTE_FAIL_CODES = frozenset((*NAV_FAIL_CODES, "timeout"))
_DIG_FAIL_CODES = frozenset({"dig_unconfirmed", "out_of_reach", "protected_region"})


def _is_preemption(err: BaseException) -> bool:
    return isinstance(err, EnvBlocked) or failure(err)[0] == "interrupted" or "抢占" in str(err)

def _same_world(start: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    return current.get("alive") is not False and start.get("dimension") == current.get("dimension")

def _matches_fluid(blk: Any, names: frozenset[str]) -> bool:
    if not isinstance(blk, Mapping):
        return False
    props = blk.get("properties")
    return bare(blk.get("name")) in names or (isinstance(props, Mapping) and props.get("waterlogged") in (True, "true"))

def _batch_blocks(ex: Executor, cells: Sequence[Cell]) -> dict[Cell, dict]:
    client = getattr(ex, "client", None)
    if not cells or client is None:
        return {}
    return client_get_blocks_batch(client, cells)

def _bounded_move_to(ex: Executor, pos: Sequence[float | int], *, rng: int = 2, timeout: float = 30.0) -> dict:
    try:
        return ex.move_to(pos=list(pos), rng=rng, timeout=timeout, retries=0)
    except TypeError:
        return ex.move_to(pos=list(pos), rng=rng)

def _cur_cell(ex: Executor, state: State) -> tuple[dict[str, Any], Cell]:
    st = state.refresh_status(ex.client, max_age_s=0) or {}
    return st, block_cell(st.get("pos") or state.pos)

def _dig_and_record(
    ex: Executor, state: State, pos: list[int], *, collect: bool, dim: str, **rec_fields: Any,
) -> Generator[dict, None, bool]:
    try:
        res = ex.dig(pos, collect=collect)
    except Replan as err:
        if not _is_preemption(err) and failure(err)[0] in _DIG_FAIL_CODES:
            return False
        raise
    voxel_cache_for(state).invalidate(pos, dimension=dim)
    yield rec("dig", res, pos=pos, **rec_fields)
    state.refresh_inventory(ex.client)
    return True


def resolve_shaft_entry(
    ex: Executor, state: State, *, dimension: str = "overworld",
    max_camp_dist: float = 96.0, default_heading: tuple[int, int] = (1, 0),
) -> dict[str, Any]:
    """解析或初始化指定维度的共享主矿井档案，优先复用近距营地 `mine_entry`。"""
    memo = memo_for(state)
    if (existing := memo.get_mine_shaft(dimension=dimension)) is not None:
        return existing
    st = state.st if isinstance(state.st, dict) and state.st.get("pos") is not None else state.refresh_status(ex.client, max_age_s=0)
    cur_cell = block_cell((st or {}).get("pos") or state.pos)
    camp = memo.get_camp(dimension=dimension)
    anchor = block_cell(camp["mine_entry"]) if (camp and camp.get("mine_entry") is not None) else cur_cell
    if math.dist(cur_cell, anchor) > float(max_camp_dist):
        anchor = cur_cell
    return memo.set_mine_shaft(anchor, dimension=dimension, lowest_y=anchor[1], lowest_pos=anchor, heading=default_heading)


def navigate_to_shaft(
    ex: Executor, state: State, *, target_y: int, dimension: str = "overworld", max_dist: float = 96.0,
) -> Generator[dict, None, dict[str, Any]]:
    """返井导航：优先前往矿井最低点，路由失败时降级返回井口并安全退出。"""
    shaft = resolve_shaft_entry(ex, state, dimension=dimension, max_camp_dist=max_dist)
    _, cur = _cur_cell(ex, state)
    entry_raw = shaft.get("entry") if shaft.get("entry") is not None else shaft.get("pos")
    entry = block_cell(entry_raw) if entry_raw is not None else cur
    low = block_cell(shaft["lowest_pos"]) if shaft.get("lowest_pos") is not None else entry
    low_y = int(shaft["lowest_y"]) if shaft.get("lowest_y") is not None else low[1]
    if math.dist(cur, low) <= 4.0 or (
        cur[1] <= int(target_y) + 2 and min(math.hypot(cur[0] - low[0], cur[2] - low[2]), math.hypot(cur[0] - entry[0], cur[2] - entry[2])) <= 24.0
    ):
        return shaft
    anchors: list[Cell] = []
    if math.dist(cur, low) <= float(max_dist) and low_y >= int(target_y) - 4:
        anchors = [low] + ([entry] if entry != low and math.dist(cur, entry) <= float(max_dist) else [])
    elif math.dist(cur, entry) <= float(max_dist):
        anchors = [entry]
    for anchor in anchors:
        if math.dist(cur, anchor) <= 3.0:
            return shaft
        try:
            res = _bounded_move_to(ex, list(anchor), rng=2, timeout=30.0)
            yield rec("move_to", res, pos=list(anchor), reason="navigate_to_shaft")
            return shaft
        except Replan as err:
            if _is_preemption(err):
                raise
            code, _ = failure(err)
            if code not in _ROUTE_FAIL_CODES and "timeout" not in str(err).lower():
                raise
            yield rec("shaft_nav_fallback", pos=list(anchor), error_code=code)
    return shaft


def find_water_cells(ex: Executor, pos: Sequence[int]) -> list[list[int]]:
    """检查给定脚底基准坐标列 `dy in (-1, 0, 1, 2, 3)` 内的含水方块。"""
    x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
    cells = [(x, y + dy, z) for dy in (-1, 0, 1, 2, 3)]
    blocks = _batch_blocks(ex, cells)
    return [list(c) for c in cells if _matches_fluid(blocks.get(c), WATER_BLOCKS)]


def column_has_water(ex: Executor, pos: Sequence[int]) -> bool:
    """判断给定列 `dy in (-1..3)` 是否含水或含水方块。"""
    return bool(find_water_cells(ex, pos))


def safe_horizontal_cell(ex: Executor, cell: Sequence[int], *, solid: frozenset[str] = SOLID_SUPPORT) -> bool:
    """校验 1x2 水平步进目标格的支撑、顶板、净空与邻格无水无岩浆。"""
    x, y, z = int(cell[0]), int(cell[1]), int(cell[2])
    floor_c, body0, body1, roof_c = (x, y - 1, z), (x, y, z), (x, y + 1, z), (x, y + 2, z)
    neighbors = [(x + dx, by + dy, z + dz) for by in (y, y + 1) for dx, dy, dz in _FACES]
    blocks = _batch_blocks(ex, [floor_c, body0, body1, roof_c, *neighbors])
    allow = AIR | solid
    return (
        bare((blocks.get(floor_c) or {}).get("name")) in solid
        and all(bare((blocks.get(c) or {}).get("name")) in allow for c in (body0, body1, roof_c))
        and not any(_matches_fluid(blocks.get(c), _FLUID_BLOCKS) for c in neighbors)
    )


def horizontal_step(
    ex: Executor, state: State, target_cell: Sequence[int], *,
    allowed_picks: frozenset[str] = STONE_OR_BETTER_PICKS, solid: frozenset[str] = SOLID_SUPPORT,
) -> Generator[dict, None, list[float]]:
    """沿当前高度向相邻一格开凿 1x2 水平通道并步入确认到达。"""
    if len(target_cell) != 3:
        raise Replan("水平掘进目标需要三维整数坐标")
    x, y, z = int(target_cell[0]), int(target_cell[1]), int(target_cell[2])
    start, (cx, cy, cz) = _cur_cell(ex, state)
    if start.get("alive") is False:
        raise Replan("水平掘进开工前角色未存活")
    if y != cy or abs(x - cx) + abs(z - cz) != 1:
        raise MineStepUnsafe("stance", "水平掘进落点须与当前站位同高且曼哈顿距离为 1")
    if not safe_horizontal_cell(ex, (x, y, z), solid=solid):
        raise MineStepUnsafe("cut", "水平掘进目标格存在流体或缺少支撑/净空")

    dim = str(start.get("dimension") or "overworld")
    for dy in (1, 0):
        tpos = [x, y + dy, z]
        if block_name(ex, tpos) in AIR:
            continue
        yield from prepare_mining_pick(ex, state, allowed=allowed_picks)
        cur_st, cur_c = _cur_cell(ex, state)
        if cur_c != (cx, cy, cz) or not _same_world(start, cur_st) or not safe_horizontal_cell(ex, (x, y, z), solid=solid):
            raise MineStepUnsafe("cut" if cur_c == (cx, cy, cz) else "stance", "水平掘进站位或承重变化，停止挖掘")
        if (name := block_name(ex, tpos)) in AIR:
            continue
        if name not in solid:
            raise MineStepUnsafe("cut", f"水平掘进目标方块 {name} 非安全岩体")
        if not (yield from _dig_and_record(ex, state, tpos, collect=name.endswith("_ore"), dim=dim)) or block_name(ex, tpos) not in AIR:
            raise MineStepUnsafe("cut", f"水平掘进开凿 {tpos} 失败或方块未清除")

    if not safe_horizontal_cell(ex, (x, y, z), solid=solid) or any(block_name(ex, [x, y + dy, z]) not in AIR for dy in (1, 0)):
        raise MineStepUnsafe("move", "水平掘进移动前净空或支撑变化")
    dest = [x + 0.5, float(y), z + 0.5]
    cur_st, cur_c = _cur_cell(ex, state)
    if cur_c == (x, y, z) and _same_world(start, cur_st) and cur_st.get("on_ground") is True:
        return dest
    res = _bounded_move_to(ex, dest, rng=1, timeout=15.0)
    yield rec("move_to", res, pos=dest)
    after, after_c = _cur_cell(ex, state)
    if after_c != (x, y, z) or not _same_world(start, after) or res.get("dimension_changed"):
        raise MineStepUnsafe("move", f"水平掘进未确认到达目标格 {(x, y, z)}")
    return dest


def _choose_plug_block(ex: Executor, state: State) -> str | None:
    state.refresh_inventory(ex.client)
    for item in ("cobblestone", "cobbled_deepslate", "dirt", "stone"):
        if state.count(item) >= 1:
            return item
    try:
        return pick_placeable(state)
    except Replan:
        return None

def _detour_check(
    ex: Executor, nx: int, ny: int, nz: int, heading: tuple[int, int], perp: tuple[int, int],
    *, solid: frozenset[str], include_perp_horiz: bool,
) -> tuple[bool, tuple[int, int]] | None:
    for d in (heading, perp):
        if safe_mine_column(ex, [nx + d[0], ny - 1, nz + d[1]], solid=solid):
            return True, d
    for d in ((heading, perp) if include_perp_horiz else (heading,)):
        if safe_horizontal_cell(ex, (nx + d[0], ny, nz + d[1]), solid=solid):
            return True, d
    return None

def plug_or_detour_water(
    ex: Executor, state: State, origin: Sequence[int], heading: tuple[int, int], *,
    solid: frozenset[str] = SOLID_SUPPORT, allowed_picks: frozenset[str] = STONE_OR_BETTER_PICKS,
) -> Generator[dict, None, tuple[bool, tuple[int, int]]]:
    """遇水主动 1 格封堵，失败则侧向 90° 开凿 1–2 格水平绕行。"""
    ox, oy, oz = int(origin[0]), int(origin[1]), int(origin[2])
    dx, dz = int(heading[0]), int(heading[1])
    fwd_col = [ox + dx, oy - 1, oz + dz]
    probe = [(ox + dx, oy - 1 + dy, oz + dz) for dy in (0, 1, 2, -1)]
    blocks = _batch_blocks(ex, probe)
    water_cells = [list(c) for c in probe if _matches_fluid(blocks.get(c), WATER_BLOCKS)]
    st = state.refresh_status(ex.client, max_age_s=0) or {}
    dim, stance = str(st.get("dimension") or "overworld"), (st if st.get("pos") is not None else state.pos)
    if water_cells and (blk := _choose_plug_block(ex, state)) is not None:
        for wpos in water_cells:
            if eye_to_block_dist(stance, wpos) > PLACE_EYE_REACH:
                continue
            try:
                res = ex.place(blk, pos=wpos)
            except Replan as err:
                if _is_preemption(err):
                    raise
                break
            voxel_cache_for(state).invalidate(wpos, dimension=dim)
            yield rec("place", res, item=blk, pos=wpos, reason="plug_water")
            state.refresh_inventory(ex.client)
            if safe_mine_column(ex, fwd_col, solid=solid):
                return True, (dx, dz)
            break

    for pdx, pdz in ((-dz, dx), (dz, -dx)):
        nx, ny, nz = ox, oy, oz
        for step_num in (1, 2):
            nxt = (nx + pdx, ny, nz + pdz)
            if not safe_horizontal_cell(ex, nxt, solid=solid):
                break
            try:
                yield from horizontal_step(ex, state, nxt, allowed_picks=allowed_picks, solid=solid)
            except MineStepUnsafe:
                if step_num == 1 and _cur_cell(ex, state)[1] == (ox, oy, oz):
                    break
                return False, (dx, dz)
            nx, ny, nz = nxt
            if (hit := _detour_check(ex, nx, ny, nz, (dx, dz), (pdx, pdz), solid=solid, include_perp_horiz=(step_num == 2))) is not None:
                return hit
        if _cur_cell(ex, state)[1] != (ox, oy, oz):
            return False, (dx, dz)
    return False, (dx, dz)

def _no_adjacent_fluids(ex: Executor, pos: Sequence[int]) -> bool:
    bx, by, bz = int(pos[0]), int(pos[1]), int(pos[2])
    cells = [(bx, by, bz), *((bx + dx, by + dy, bz + dz) for dx, dy, dz in _FACES)]
    return not any(_matches_fluid(_batch_blocks(ex, cells).get(c), _FLUID_BLOCKS) for c in cells)

def _scan_and_collect_bonus(
    ex: Executor, state: State, *, target_ores: Collection[str], bonus_ores: Collection[str],
    allowed_picks: frozenset[str], scan_radius: int, dimension: str,
    excluded_set: set[Cell], tried_bonus: set[Cell],
) -> Generator[dict, None, list[list[int]]]:
    types = list(dict.fromkeys([*target_ores, *bonus_ores]))
    if not types:
        return []
    found = ex.client.find_blocks(types=types, radius=scan_radius, limit=32) or {}
    st, cur_cell = _cur_cell(ex, state)
    cur_pos, stance = st.get("pos") or state.pos, (st if st.get("pos") is not None else state.pos)
    dim = str(st.get("dimension") or dimension)
    memo_for(state).ingest_find_blocks(found, dimension=dim, origin=stance)
    vc = voxel_cache_for(state)
    vc.ingest_find_blocks(found, dimension=dim, client=ex.client)
    vc.clear_stance_blacklist_if_moved(cur_pos)

    target_set, bonus_set = {bare(t) for t in target_ores}, {bare(b) for b in bonus_ores} - {bare(t) for t in target_ores}
    target_hits, bonus_hits = [], []
    for entry in (found.get("blocks") if isinstance(found, Mapping) else None) or []:
        raw_pos = entry.get("pos") if isinstance(entry, Mapping) else None
        if not isinstance(raw_pos, (list, tuple)) or len(raw_pos) < 3:
            continue
        cell = (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
        if cell in excluded_set or under_feet(cur_pos, cell) or vc.is_unreachable("mine_shaft", cell, dimension=dim, stance_cell=cur_cell):
            continue
        bname = bare(entry.get("name"))
        if bname in target_set or (not bname and not bonus_set):
            target_hits.append(list(cell))
        elif bname in bonus_set and cell not in tried_bonus:
            bonus_hits.append(list(cell))
    if target_hits:
        return sorted(target_hits, key=lambda p: eye_to_block_dist(stance, p))

    mined = 0
    for bp in sorted(bonus_hits, key=lambda p: eye_to_block_dist(stance, p)):
        if mined >= 2:
            break
        if not can_reach_block(ex, stance, bp, reach=DIG_EYE_REACH) or not _no_adjacent_fluids(ex, bp):
            continue
        tried_bonus.add((bp[0], bp[1], bp[2]))
        yield from prepare_mining_pick(ex, state, allowed=allowed_picks)
        st_now, cur_now = _cur_cell(ex, state)
        pos_now, stance_now = st_now.get("pos") or state.pos, (st_now if st_now.get("pos") is not None else state.pos)
        if under_feet(pos_now, bp) or not can_reach_block(ex, stance_now, bp, reach=DIG_EYE_REACH) or not _no_adjacent_fluids(ex, bp):
            continue
        if not (yield from _dig_and_record(ex, state, bp, collect=True, dim=dim, reason="bonus_ore")):
            vc.mark_unreachable("mine_shaft", bp, dimension=dim, stance_cell=cur_now)
            continue
        mined += 1
    return []

def branch_mine_level(
    ex: Executor, state: State, *, level_y: int, target_ores: Collection[str],
    bonus_ores: Collection[str] = (), allowed_picks: frozenset[str] = STONE_OR_BETTER_PICKS,
    max_length: int = 12, scan_stride: int = 3, scan_radius: int = 16,
    dimension: str = "overworld", excluded: Collection[tuple[int, int, int]] = (),
    solid: frozenset[str] = SOLID_SUPPORT,
) -> Generator[dict, None, list[list[int]]]:
    """在指定矿层沿主巷道交替左右开凿 1x2 分支并周期扫描目标矿石与顺路伴生矿。"""
    shaft = resolve_shaft_entry(ex, state, dimension=dimension)
    branch_idx = memo_for(state).next_mine_branch(level_y, dimension=dimension)
    hx, hz = map(int, shaft.get("heading") or (1, 0))
    primary, secondary = ((-hz, hx), (hz, -hx)) if branch_idx % 2 == 0 else ((hz, -hx), (-hz, hx))
    _, cur_cell = _cur_cell(ex, state)
    bdir = next((d for d in (primary, secondary) if safe_horizontal_cell(ex, (cur_cell[0] + d[0], cur_cell[1], cur_cell[2] + d[1]), solid=solid)), None)
    if bdir is None:
        perp_cells = [(cur_cell[0] + d[0], cur_cell[1] + dy, cur_cell[2] + d[1]) for d in (primary, secondary) for dy in (-1, 0, 1)]
        perp_blocks = _batch_blocks(ex, perp_cells)
        if any((n := bare((perp_blocks.get(c) or {}).get("name"))) and n not in AIR for c in perp_cells) and safe_horizontal_cell(
            ex, (cur_cell[0] + hx, cur_cell[1], cur_cell[2] + hz), solid=solid
        ):
            bdir = (hx, hz)
    if bdir is None:
        return []

    excluded_set, target_set, tried_bonus = {(int(e[0]), int(e[1]), int(e[2])) for e in excluded}, {bare(t) for t in target_ores}, set()
    visited: set[tuple[int, int]] = {(cur_cell[0], cur_cell[2])}
    stride, limit, steps_done, last_scanned = max(1, int(scan_stride)), max(0, int(max_length)), 0, -1
    scan = lambda: _scan_and_collect_bonus(
        ex, state, target_ores=target_ores, bonus_ores=bonus_ores, allowed_picks=allowed_picks,
        scan_radius=scan_radius, dimension=dimension, excluded_set=excluded_set, tried_bonus=tried_bonus,
    )
    for step_idx in range(limit):
        if step_idx % stride == 0:
            last_scanned = step_idx
            if (hits := (yield from scan())):
                return hits
        _, cur_cell = _cur_cell(ex, state)
        visited.add((cur_cell[0], cur_cell[2]))
        next_cell = (cur_cell[0] + bdir[0], cur_cell[1], cur_cell[2] + bdir[1])
        if (next_cell[0], next_cell[2]) in visited or not safe_horizontal_cell(ex, next_cell, solid=solid):
            turned = False
            if column_has_water(ex, [next_cell[0], next_cell[1] - 1, next_cell[2]]):
                ok, new_dir = yield from plug_or_detour_water(ex, state, cur_cell, bdir, solid=solid, allowed_picks=allowed_picks)
                _, cur_cell = _cur_cell(ex, state)
                visited.add((cur_cell[0], cur_cell[2]))
                cand = (cur_cell[0] + new_dir[0], cur_cell[1], cur_cell[2] + new_dir[1])
                if ok and (cand[0], cand[2]) not in visited and safe_horizontal_cell(ex, cand, solid=solid):
                    bdir, next_cell, turned = new_dir, cand, True
            if not turned:
                turns = ((-bdir[1], bdir[0]), (bdir[1], -bdir[0]))
                alt = next((d for d in (*turns, *(c for c in CARDINALS if c not in turns and c != bdir))
                            if (cur_cell[0] + d[0], cur_cell[2] + d[1]) not in visited
                            and safe_horizontal_cell(ex, (cur_cell[0] + d[0], cur_cell[1], cur_cell[2] + d[1]), solid=solid)), None)
                if alt is None:
                    break
                bdir, next_cell = alt, (cur_cell[0] + alt[0], cur_cell[1], cur_cell[2] + alt[1])
        ahead_hits = [[next_cell[0], next_cell[1] + dy, next_cell[2]] for dy in (0, 1)
                      if (next_cell[0], next_cell[1] + dy, next_cell[2]) not in excluded_set
                      and block_name(ex, [next_cell[0], next_cell[1] + dy, next_cell[2]]) in target_set]
        if ahead_hits:
            return ahead_hits
        visited.add((next_cell[0], next_cell[2]))
        try:
            yield from horizontal_step(ex, state, next_cell, allowed_picks=allowed_picks, solid=solid)
        except MineStepUnsafe:
            break
        steps_done = step_idx + 1
    return (yield from scan()) if last_scanned != steps_done else []
