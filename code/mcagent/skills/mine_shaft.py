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
from .items import AIR, NAV_FAIL_CODES, STONE_OR_BETTER_PICKS, bare, best_pickaxe, block_name, pick_placeable
from .mining import SOLID_SUPPORT, MineStepUnsafe, prepare_mining_pick, safe_mine_column, under_feet
from .ore_ledger import (
    mine_reachable_ores_and_record,
    ore_category,
    ores_for_dimension,
    pickaxe_for_ore,
    record_ore_observations,
)
from .sightline import DIG_EYE_REACH, block_cell, can_reach_block, eye_to_block_dist
from .voxel_cache import client_get_blocks_batch, has_batch_get_blocks, voxel_cache_for
from .world_memo import memo_for

Cell = tuple[int, int, int]
IRON_LAYER_Y, DIAMOND_LAYER_Y = 16, -58
HERRINGBONE_RIB_LENGTH = 16
HERRINGBONE_RIB_SPACING = 3
CARDINALS: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
WATER_BLOCKS: frozenset[str] = frozenset({"water", "flowing_water", "bubble_column"})
LAVA_BLOCKS: frozenset[str] = frozenset({"lava", "flowing_lava"})
BONUS_BRANCH_ORES: tuple[str, ...] = (
    "coal_ore", "deepslate_coal_ore", "lapis_ore", "deepslate_lapis_ore", "redstone_ore", "deepslate_redstone_ore",
)
PLACE_EYE_REACH: float = DIG_EYE_REACH
_FLUID_BLOCKS = WATER_BLOCKS | LAVA_BLOCKS
_NATURAL_CAVE_AIR: frozenset[str] = frozenset({"cave_air"})
_SOLID_ENCLOSURE: frozenset[str] = SOLID_SUPPORT | frozenset({"bedrock", "obsidian", "netherrack", "basalt", "blackstone"})
_GRAVITY_OR_UTILITY_BLOCKS: frozenset[str] = frozenset({
    "sand", "red_sand", "gravel", "furnace", "blast_furnace", "smoker",
    "crafting_table", "chest", "anvil", "chipped_anvil", "damaged_anvil",
})
_FACES: tuple[Cell, ...] = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
_HORIZ_FACES: tuple[Cell, ...] = ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))
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
    # 服务端 /dig 会"报成功但方块还在"（也曾出现挖掉却不给掉落），所以必须复核：
    # 有 warning 直接判失败；再读一次目标格，仍是实心方块也判失败。
    # 否则调用方会把"已采"记进台账，同一矿点整轮不再回来，东西却没到手。
    if isinstance(res, Mapping) and res.get("warning"):
        return False
    try:
        still = block_name(ex, pos)
    except Exception:
        still = None
    if still is not None and still not in AIR:
        return False
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
    if shaft.get("sealed") and shaft.get("seal_cells") and cur[1] >= entry[1] - 1:
        if math.dist(cur, entry) > float(max_dist):
            return shaft
        if math.dist(cur, entry) > 2.5:
            try:
                res = _bounded_move_to(ex, list(entry), rng=2, timeout=30.0)
                yield rec("move_to", res, pos=list(entry), reason="navigate_to_shaft_unseal")
            except Replan as err:
                if _is_preemption(err):
                    raise
                code, _ = failure(err)
                if code not in _ROUTE_FAIL_CODES and "timeout" not in str(err).lower():
                    raise
                yield rec("shaft_nav_fallback", pos=list(entry), error_code=code)
                return shaft
        yield from unseal_shaft_entrance(ex, state, dimension=dimension, shaft=shaft)
        shaft = memo_for(state).get_mine_shaft(dimension=dimension) or shaft
        _, cur = _cur_cell(ex, state)
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


def find_natural_cave_breach_cells(
    ex: Executor,
    target_cells: Sequence[Sequence[int]],
    *,
    known_air: Collection[Sequence[int]] = (),
    player_cell: Sequence[int] | None = None,
    check_enclosed_air: bool = True,
) -> list[Cell]:
    """探测目标格组 6 邻面是否接触 MC 自然矿洞（`cave_air` 或封闭地下岩层中 >=2 格连通空气）。"""
    targets: list[Cell] = []
    seen_t: set[Cell] = set()
    for raw in target_cells:
        c = (int(raw[0]), int(raw[1]), int(raw[2]))
        if c not in seen_t:
            seen_t.add(c)
            targets.append(c)
    if not targets:
        return []

    known: set[Cell] = {(int(c[0]), int(c[1]), int(c[2])) for c in known_air} | seen_t
    neighbors: list[Cell] = []
    seen_nb: set[Cell] = set()
    for tx, ty, tz in targets:
        for dx, dy, dz in _FACES:
            nb = (tx + dx, ty + dy, tz + dz)
            if nb not in known and nb not in seen_nb:
                seen_nb.add(nb)
                neighbors.append(nb)
    if not neighbors:
        return []

    probe_cells: list[Cell] = list(neighbors)
    pcell: Cell | None = None
    ceiling_c: Cell | None = None
    roof_c: Cell | None = None
    wall_cells: list[Cell] = []
    if check_enclosed_air and player_cell is not None:
        px, py, pz = int(player_cell[0]), int(player_cell[1]), int(player_cell[2])
        pcell = (px, py, pz)
        ceiling_c = (px, py + 2, pz)
        roof_c = (px, py + 3, pz)
        wall_cells = [
            c for c in ((px + 1, py + 1, pz), (px - 1, py + 1, pz), (px, py + 1, pz + 1), (px, py + 1, pz - 1))
            if c not in known
        ]
        for c in (ceiling_c, roof_c, *wall_cells):
            if c not in seen_nb:
                probe_cells.append(c)

    blocks = _batch_blocks(ex, probe_cells)
    breaches: list[Cell] = []
    breach_set: set[Cell] = set()
    air_candidates: list[Cell] = []

    for nb in neighbors:
        bname = bare((blocks.get(nb) or {}).get("name"))
        if bname in _NATURAL_CAVE_AIR:
            breaches.append(nb)
            breach_set.add(nb)
        elif bname == "air":
            air_candidates.append(nb)

    if check_enclosed_air and pcell is not None and air_candidates:
        assert ceiling_c is not None and roof_c is not None
        ceil_solid = bare((blocks.get(ceiling_c) or {}).get("name")) in _SOLID_ENCLOSURE
        roof_solid = bare((blocks.get(roof_c) or {}).get("name")) in _SOLID_ENCLOSURE
        has_overhead = ceil_solid or roof_solid
        solid_walls = sum(
            1 for wc in wall_cells
            if bare((blocks.get(wc) or {}).get("name")) in _SOLID_ENCLOSURE
        )
        enclosure_count = (1 if has_overhead else 0) + solid_walls
        if has_overhead and enclosure_count >= 2:
            outward_cells: list[Cell] = []
            seen_out: set[Cell] = set()
            for nx, ny, nz in air_candidates:
                for dx, dy, dz in _FACES:
                    nb2 = (nx + dx, ny + dy, nz + dz)
                    if nb2 not in known and nb2 not in blocks and nb2 not in seen_out:
                        seen_out.add(nb2)
                        outward_cells.append(nb2)
            blocks2 = _batch_blocks(ex, outward_cells) if outward_cells else {}
            all_blocks = {**blocks, **blocks2}
            for nb in air_candidates:
                if nb in breach_set:
                    continue
                nx, ny, nz = nb
                if any(
                    nb2 not in known and bare((all_blocks.get(nb2) or {}).get("name")) in {"air", "cave_air"}
                    for dx, dy, dz in _FACES
                    for nb2 in ((nx + dx, ny + dy, nz + dz),)
                ):
                    breaches.append(nb)
                    breach_set.add(nb)

    return breaches


def column_touches_natural_cave(
    ex: Executor,
    target_cells: Sequence[int] | Sequence[Sequence[int]],
    *,
    known_air: Collection[Sequence[int]] = (),
    player_cell: Sequence[int] | None = None,
    prev_cell: Sequence[int] | None = None,
) -> bool:
    """判断目标格组或单格落点列 `(x, y..y+2, z)` 是否紧邻自然矿洞/裂谷。"""
    if (
        isinstance(target_cells, Sequence)
        and len(target_cells) == 3
        and all(isinstance(v, int) and not isinstance(v, bool) for v in target_cells)
    ):
        tx, ty, tz = int(target_cells[0]), int(target_cells[1]), int(target_cells[2])
        cells: list[Cell] = [(tx, ty, tz), (tx, ty + 1, tz), (tx, ty + 2, tz)]
    else:
        cells = [(int(c[0]), int(c[1]), int(c[2])) for c in target_cells]
    eff_player = player_cell if player_cell is not None else prev_cell
    eff_known: list[Sequence[int]] = list(known_air)
    if prev_cell is not None:
        px, py, pz = int(prev_cell[0]), int(prev_cell[1]), int(prev_cell[2])
        eff_known.extend(((px, py, pz), (px, py + 1, pz), (px, py + 2, pz)))
    return bool(find_natural_cave_breach_cells(
        ex, cells, known_air=eff_known, player_cell=eff_player,
    ))


def _choose_plug_block(first: Any, state: State | None = None) -> str | None:
    if state is None:
        st_obj = first
    else:
        st_obj = state
        if getattr(first, "client", None) is not None:
            st_obj.refresh_inventory(first.client)
    for item in ("cobblestone", "cobbled_deepslate", "dirt", "stone", "netherrack"):
        if st_obj.count(item) >= 1:
            return item
    try:
        cand = pick_placeable(st_obj)
    except Replan:
        return None
    return cand if cand not in _GRAVITY_OR_UTILITY_BLOCKS else None


def plug_cave_breach(
    ex: Executor,
    state: State,
    dug_cell: Sequence[int],
    *,
    known_air: Collection[Sequence[int]] = (),
    player_cell: Sequence[int] | None = None,
) -> Generator[dict, None, bool]:
    """挖掘后检测是否打穿自然矿洞；若打穿且可达安全不挡身，立即用非重力实心方块封堵。"""
    cell = (int(dug_cell[0]), int(dug_cell[1]), int(dug_cell[2]))
    st, cur = _cur_cell(ex, state)
    if st.get("alive") is False:
        return False
    pcell = (int(player_cell[0]), int(player_cell[1]), int(player_cell[2])) if player_cell is not None else cur
    if not find_natural_cave_breach_cells(ex, [cell], known_air=known_air, player_cell=pcell):
        return False
    if block_name(ex, list(cell)) not in AIR:
        return False
    if cell in (cur, (cur[0], cur[1] + 1, cur[2])):
        return False
    stance = st if st.get("pos") is not None else state.pos
    if eye_to_block_dist(stance, cell) > DIG_EYE_REACH:
        return False
    plug_blk = _choose_plug_block(ex, state)
    if plug_blk is None:
        return False
    dim = str(st.get("dimension") or "overworld")
    try:
        res = ex.place(plug_blk, pos=list(cell), stop_on=("death",))
    except Replan as err:
        if _is_preemption(err):
            raise
        return False
    voxel_cache_for(state).invalidate(list(cell), dimension=dim)
    yield rec("place", res, item=plug_blk, pos=list(cell), reason="plug_cave_breach")
    state.refresh_inventory(ex.client)
    return True


def safe_horizontal_cell(
    ex: Executor,
    cell: Sequence[int],
    *,
    solid: frozenset[str] = SOLID_SUPPORT,
    avoid_caves: bool = False,
    known_air: Collection[Sequence[int]] = (),
    player_cell: Sequence[int] | None = None,
) -> bool:
    """校验 1x2 水平步进目标格的支撑、顶板、净空、邻格无水无岩浆与可选自然矿洞避让。"""
    x, y, z = int(cell[0]), int(cell[1]), int(cell[2])
    floor_c, body0, body1, roof_c = (x, y - 1, z), (x, y, z), (x, y + 1, z), (x, y + 2, z)
    neighbors = [(x + dx, by + dy, z + dz) for by in (y, y + 1) for dx, dy, dz in _FACES]
    blocks = _batch_blocks(ex, [floor_c, body0, body1, roof_c, *neighbors])
    allow = AIR | solid
    ok = (
        bare((blocks.get(floor_c) or {}).get("name")) in solid
        and all(bare((blocks.get(c) or {}).get("name")) in allow for c in (body0, body1, roof_c))
        and not any(_matches_fluid(blocks.get(c), _FLUID_BLOCKS) for c in neighbors)
    )
    if ok and avoid_caves:
        if column_touches_natural_cave(ex, [body0, body1], known_air=known_air, player_cell=player_cell):
            return False
    return ok


def horizontal_step(
    ex: Executor, state: State, target_cell: Sequence[int], *,
    allowed_picks: frozenset[str] = STONE_OR_BETTER_PICKS, solid: frozenset[str] = SOLID_SUPPORT,
    avoid_caves: bool = True, plug_caves: bool = True, known_air: Collection[Sequence[int]] = (),
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
    known_air_set: set[Cell] = {(int(c[0]), int(c[1]), int(c[2])) for c in known_air} | {(cx, cy, cz), (cx, cy + 1, cz)}
    if not safe_horizontal_cell(
        ex, (x, y, z), solid=solid, avoid_caves=avoid_caves, known_air=known_air_set, player_cell=(cx, cy, cz),
    ):
        raise MineStepUnsafe("cut", "水平掘进目标格存在流体/自然矿洞或缺少支撑/净空")

    dim = str(start.get("dimension") or "overworld")
    for dy in (1, 0):
        tpos = [x, y + dy, z]
        if block_name(ex, tpos) in AIR:
            known_air_set.add((x, y + dy, z))
            continue
        yield from prepare_mining_pick(ex, state, allowed=allowed_picks)
        cur_st, cur_c = _cur_cell(ex, state)
        if (
            cur_c != (cx, cy, cz)
            or not _same_world(start, cur_st)
            or not safe_horizontal_cell(
                ex, (x, y, z), solid=solid, avoid_caves=avoid_caves, known_air=known_air_set, player_cell=(cx, cy, cz),
            )
        ):
            raise MineStepUnsafe("cut" if cur_c == (cx, cy, cz) else "stance", "水平掘进站位或承重变化，停止挖掘")
        if (name := block_name(ex, tpos)) in AIR:
            known_air_set.add((x, y + dy, z))
            continue
        if name not in solid:
            raise MineStepUnsafe("cut", f"水平掘进目标方块 {name} 非安全岩体")
        if not (yield from _dig_and_record(ex, state, tpos, collect=name.endswith("_ore"), dim=dim)) or block_name(ex, tpos) not in AIR:
            raise MineStepUnsafe("cut", f"水平掘进开凿 {tpos} 失败或方块未清除")
        if plug_caves and (yield from plug_cave_breach(ex, state, tpos, known_air=known_air_set, player_cell=(cx, cy, cz))):
            raise MineStepUnsafe("cut", "挖穿自然矿道已立即封堵，转向避开")
        known_air_set.add((x, y + dy, z))

    if (
        not safe_horizontal_cell(
            ex, (x, y, z), solid=solid, avoid_caves=avoid_caves, known_air=known_air_set, player_cell=(cx, cy, cz),
        )
        or any(block_name(ex, [x, y + dy, z]) not in AIR for dy in (1, 0))
    ):
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

_ORE_VALUE_RANK: dict[str, int] = {
    "diamond_ore": 0,
    "ancient_debris": 0,
    "emerald_ore": 1,
    "gold_ore": 2,
    "iron_ore": 3,
    "lapis_ore": 4,
    "redstone_ore": 5,
    "coal_ore": 6,
    "copper_ore": 7,
    "quartz_ore": 7,
}


def _scan_and_collect_bonus(
    ex: Executor, state: State, *, target_ores: Collection[str], bonus_ores: Collection[str],
    allowed_picks: frozenset[str], scan_radius: int, dimension: str,
    excluded_set: set[Cell], tried_bonus: set[Cell],
) -> Generator[dict, None, list[list[int]]]:
    types = list(dict.fromkeys([*target_ores, *bonus_ores, *ores_for_dimension(dimension)]))
    if not types:
        return []
    found = ex.client.find_blocks(types=types, radius=scan_radius, limit=32) or {}
    st, cur_cell = _cur_cell(ex, state)
    cur_pos, stance = st.get("pos") or state.pos, (st if st.get("pos") is not None else state.pos)
    dim = str(st.get("dimension") or dimension)
    blocks_list = [
        entry for entry in ((found.get("blocks") if isinstance(found, Mapping) else None) or ())
        if isinstance(entry, Mapping)
    ]
    memo = memo_for(state)
    memo.ingest_find_blocks(found, dimension=dim, origin=stance)
    record_ore_observations(state, blocks_list, dimension=dim, origin=stance)
    vc = voxel_cache_for(state)
    vc.ingest_find_blocks(found, dimension=dim, client=ex.client)
    vc.clear_stance_blacklist_if_moved(cur_pos)

    target_set = {bare(t).strip().lower() for t in target_ores}
    bonus_set = {bare(b).strip().lower() for b in bonus_ores} - target_set
    target_hits: list[list[int]] = []
    bonus_hits: list[tuple[str, list[int]]] = []
    for entry in blocks_list:
        raw_pos = entry.get("pos")
        if not isinstance(raw_pos, (list, tuple)) or len(raw_pos) < 3:
            continue
        cell = (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
        if cell in excluded_set or under_feet(cur_pos, cell) or vc.is_unreachable("mine_shaft", cell, dimension=dim, stance_cell=cur_cell):
            continue
        bname = bare(entry.get("name")).strip().lower()
        if bname in target_set or (not bname and not bonus_set):
            target_hits.append(list(cell))
        elif (bname in bonus_set or ore_category(bname) is not None) and cell not in tried_bonus:
            bonus_hits.append((bname, list(cell)))
    if target_hits:
        return sorted(target_hits, key=lambda p: eye_to_block_dist(stance, p))

    mined = 0
    bonus_hits.sort(
        key=lambda item: (
            _ORE_VALUE_RANK.get(ore_category(item[0]) or item[0], 99),
            eye_to_block_dist(stance, item[1]),
        )
    )
    for bname, bp in bonus_hits:
        if mined >= 4:
            break
        pick = pickaxe_for_ore(state, bname)
        if pick is None:
            continue
        if not can_reach_block(ex, stance, bp, reach=DIG_EYE_REACH) or not _no_adjacent_fluids(ex, bp):
            continue
        tried_bonus.add((bp[0], bp[1], bp[2]))
        yield from prepare_mining_pick(ex, state, allowed=frozenset((pick,)))
        st_now, cur_now = _cur_cell(ex, state)
        pos_now, stance_now = st_now.get("pos") or state.pos, (st_now if st_now.get("pos") is not None else state.pos)
        if under_feet(pos_now, bp) or not can_reach_block(ex, stance_now, bp, reach=DIG_EYE_REACH) or not _no_adjacent_fluids(ex, bp):
            continue
        if not (yield from _dig_and_record(ex, state, bp, collect=True, dim=dim, reason="bonus_ore")):
            vc.mark_unreachable("mine_shaft", bp, dimension=dim, stance_cell=cur_now)
            continue
        memo.mark_depleted(bp, dimension=dim, category=ore_category(bname) or bname)
        mined += 1
    return []

def branch_mine_level(
    ex: Executor, state: State, *, level_y: int, target_ores: Collection[str],
    bonus_ores: Collection[str] = (), allowed_picks: frozenset[str] = STONE_OR_BETTER_PICKS,
    max_length: int = HERRINGBONE_RIB_LENGTH, scan_stride: int = 3, scan_radius: int = 16,
    dimension: str = "overworld", excluded: Collection[tuple[int, int, int]] = (),
    solid: frozenset[str] = SOLID_SUPPORT,
    rib_spacing: int = HERRINGBONE_RIB_SPACING,
) -> Generator[dict, None, list[list[int]]]:
    """在固定 Y 矿层执行鱼骨挖矿法：左右支道各挖 `max_length`（默认 16）格，回主干道后沿脊柱推进 `rib_spacing`（默认 3）格。

    全程保持自然矿洞避让与挖穿即堵、水体封堵与周期扫矿/顺采伴生矿逻辑不变；一旦发现目标矿立即返回坐标。
    """
    shaft = resolve_shaft_entry(ex, state, dimension=dimension)
    memo = memo_for(state)
    memo.next_mine_branch(level_y, dimension=dimension)
    hx, hz = map(int, shaft.get("heading") or (1, 0))
    if (hx, hz) not in CARDINALS:
        hx, hz = (1, 0)
    left_dir, right_dir = (-hz, hx), (hz, -hx)
    _, spine_node = _cur_cell(ex, state)
    known_air_cells: set[Cell] = {
        (spine_node[0], spine_node[1], spine_node[2]),
        (spine_node[0], spine_node[1] + 1, spine_node[2]),
    }
    for key in ("entry", "lowest_pos"):
        raw_p = shaft.get(key)
        if isinstance(raw_p, (list, tuple)) and len(raw_p) >= 3:
            pc = block_cell(raw_p)
            known_air_cells.add(pc)
            known_air_cells.add((pc[0], pc[1] + 1, pc[2]))
    back_cells = [(spine_node[0] - hx, spine_node[1] + dy, spine_node[2] - hz) for dy in (0, 1, 2)]
    back_blocks = _batch_blocks(ex, back_cells)
    for bc in back_cells:
        if bare((back_blocks.get(bc) or {}).get("name")) in AIR:
            known_air_cells.add(bc)

    excluded_set = {(int(e[0]), int(e[1]), int(e[2])) for e in excluded}
    target_set = {bare(t) for t in target_ores}
    tried_bonus: set[Cell] = set()
    stride = max(1, int(scan_stride))
    rib_len = max(0, int(max_length))
    spacing = max(1, int(rib_spacing))
    if rib_len <= 0:
        return []

    def _scan() -> Generator[dict, None, list[list[int]]]:
        return (yield from _scan_and_collect_bonus(
            ex, state, target_ores=target_ores, bonus_ores=bonus_ores, allowed_picks=allowed_picks,
            scan_radius=scan_radius, dimension=dimension, excluded_set=excluded_set, tried_bonus=tried_bonus,
        ))

    def _dig_straight_corridor(direction: tuple[int, int], steps_budget: int) -> Generator[dict, None, list[list[int]]]:
        """沿单一水平方向笔直掘进至多 `steps_budget` 格；遇矿即返，遇矿洞/流体安全止步不串道。"""
        steps_done, last_scanned = 0, -1
        corridor_visited: set[tuple[int, int]] = set()
        for step_idx in range(steps_budget):
            if step_idx % stride == 0:
                last_scanned = step_idx
                if (hits := (yield from _scan())):
                    return hits
            _, cur_cell = _cur_cell(ex, state)
            corridor_visited.add((cur_cell[0], cur_cell[2]))
            known_air_cells.add((cur_cell[0], cur_cell[1], cur_cell[2]))
            known_air_cells.add((cur_cell[0], cur_cell[1] + 1, cur_cell[2]))
            next_cell = (cur_cell[0] + direction[0], cur_cell[1], cur_cell[2] + direction[1])
            if (next_cell[0], next_cell[2]) in corridor_visited:
                break
            if not safe_horizontal_cell(
                ex, next_cell, solid=solid, avoid_caves=True, known_air=known_air_cells, player_cell=cur_cell,
            ):
                recovered_straight = False
                if column_has_water(ex, [next_cell[0], next_cell[1] - 1, next_cell[2]]):
                    ok, _ = yield from plug_or_detour_water(
                        ex, state, cur_cell, direction, solid=solid, allowed_picks=allowed_picks,
                    )
                    _, cur_cell = _cur_cell(ex, state)
                    corridor_visited.add((cur_cell[0], cur_cell[2]))
                    known_air_cells.add((cur_cell[0], cur_cell[1], cur_cell[2]))
                    known_air_cells.add((cur_cell[0], cur_cell[1] + 1, cur_cell[2]))
                    next_cell = (cur_cell[0] + direction[0], cur_cell[1], cur_cell[2] + direction[1])
                    recovered_straight = (
                        ok
                        and (next_cell[0], next_cell[2]) not in corridor_visited
                        and safe_horizontal_cell(
                            ex, next_cell, solid=solid, avoid_caves=True, known_air=known_air_cells, player_cell=cur_cell,
                        )
                    )
                if not recovered_straight:
                    break
            ahead_hits = [
                [next_cell[0], next_cell[1] + dy, next_cell[2]] for dy in (0, 1)
                if (next_cell[0], next_cell[1] + dy, next_cell[2]) not in excluded_set
                and block_name(ex, [next_cell[0], next_cell[1] + dy, next_cell[2]]) in target_set
            ]
            if ahead_hits:
                return ahead_hits
            corridor_visited.add((next_cell[0], next_cell[2]))
            try:
                yield from horizontal_step(
                    ex, state, next_cell, allowed_picks=allowed_picks, solid=solid,
                    avoid_caves=True, plug_caves=True, known_air=known_air_cells,
                )
            except MineStepUnsafe:
                _, cur_now = _cur_cell(ex, state)
                known_air_cells.add((cur_now[0], cur_now[1], cur_now[2]))
                known_air_cells.add((cur_now[0], cur_now[1] + 1, cur_now[2]))
                break
            known_air_cells.add((next_cell[0], next_cell[1], next_cell[2]))
            known_air_cells.add((next_cell[0], next_cell[1] + 1, next_cell[2]))
            steps_done = step_idx + 1
        if steps_done > 0 and last_scanned != steps_done:
            if (hits := (yield from _scan())):
                return hits
        return []

    def _return_to_spine(node: Cell) -> Generator[dict, None, bool]:
        """沿已开凿的笔直鱼刺支道返回主干道节点。"""
        _, cur_cell = _cur_cell(ex, state)
        if cur_cell == node:
            return True
        dest = [node[0] + 0.5, float(node[1]), node[2] + 0.5]
        try:
            res = _bounded_move_to(ex, dest, rng=1, timeout=20.0)
            yield rec("move_to", res, pos=dest, reason="herringbone_return_spine")
        except Replan as err:
            if _is_preemption(err):
                raise
            return False
        _, after_cell = _cur_cell(ex, state)
        return after_cell[1] == node[1] and math.hypot(after_cell[0] - node[0], after_cell[2] - node[2]) <= 1.5

    # 1) 左支道（默认 16 格）
    if (hits := (yield from _dig_straight_corridor(left_dir, rib_len))):
        return hits
    _, after_left = _cur_cell(ex, state)
    rib_dug = after_left != spine_node

    # 2) 返回主干道节点 -> 右支道（默认 16 格）
    if not (yield from _return_to_spine(spine_node)):
        return []
    if (hits := (yield from _dig_straight_corridor(right_dir, rib_len))):
        return hits
    _, after_right = _cur_cell(ex, state)
    rib_dug = rib_dug or (after_right != spine_node)

    # 3) 返回主干道节点 -> 沿脊柱推进 rib_spacing（默认 3 格，两翼支道间保留 2 格实心岩壁不漏矿）
    if not (yield from _return_to_spine(spine_node)):
        return []
    # 若两侧均未开凿且左右相邻完全悬空无实体岩壁（如 1 格宽孤立矿桥），不再盲目沿脊柱前推。
    if not rib_dug:
        perp_cells = [
            (spine_node[0] + d[0], spine_node[1] + dy, spine_node[2] + d[1])
            for d in (left_dir, right_dir)
            for dy in (-1, 0, 1)
        ]
        perp_blocks = _batch_blocks(ex, perp_cells)
        if not any((n := bare((perp_blocks.get(c) or {}).get("name"))) and n not in AIR for c in perp_cells):
            return []
    if (hits := (yield from _dig_straight_corridor((hx, hz), spacing))):
        return hits

    _, end_cell = _cur_cell(ex, state)
    if end_cell != spine_node:
        memo.update_mine_shaft(dimension=dimension, lowest_y=end_cell[1], lowest_pos=end_cell)
    return []


def seal_shaft_entrance(
    ex: Executor,
    state: State,
    *,
    dimension: str = "overworld",
    shaft: Mapping[str, Any] | None = None,
) -> Generator[dict, None, list[list[int]]]:
    """【已按用户要求停用】原本在井口喉道放 1x2 方块封洞防怪。

    停用原因：封洞后 bot 自己回程时井口被自己堵死，`return_to_surface_from_mine`
    必须先 `_unseal_shaft_from_inside` 拆封，多了一层失败点；实测出现过
    "明明有矿道却上不去、只能重新挖"的情况。这里直接返回空列表：不封洞、不记账。
    """
    return []
    memo = memo_for(state)      # noqa: unreachable —— 保留原实现作参考
    shaft_rec = shaft or memo.get_mine_shaft(dimension=dimension)
    if not isinstance(shaft_rec, Mapping) or shaft_rec.get("sealed") is True:
        return []
    entry_raw, heading_raw = shaft_rec.get("entry"), shaft_rec.get("heading")
    if not entry_raw or not heading_raw:
        return []
    entry = block_cell(entry_raw)
    if entry[1] < 48:
        return []
    hx, hz = int(heading_raw[0]), int(heading_raw[1])
    if (hx, hz) not in CARDINALS:
        return []

    st, cur = _cur_cell(ex, state)
    if st.get("alive") is False:
        return []
    if cur[1] > entry[1] - 2 or cur[1] < entry[1] - 5:
        return []
    if _choose_plug_block(ex, state) is None:
        return []

    stance = st if st.get("pos") is not None else state.pos
    body = {cur, (cur[0], cur[1] + 1, cur[2])}
    # 按真实 1x3 下行阶梯几何从深到浅尝试井口喉道：
    # 平地 entry.y=64 时，第 2/3 级喉道位于地下实心壁内且与地表 y-1 齐平，既可稳固依附岩壁又能彻底挡住地面怪物。
    candidates: tuple[tuple[tuple[Cell, Cell], bool], ...] = (
        (((entry[0] + 3 * hx, entry[1] - 2, entry[2] + 3 * hz), (entry[0] + 3 * hx, entry[1] - 1, entry[2] + 3 * hz)), False),
        (((entry[0] + 2 * hx, entry[1] - 2, entry[2] + 2 * hz), (entry[0] + 2 * hx, entry[1] - 1, entry[2] + 2 * hz)), False),
        (((entry[0] + hx, entry[1] - 1, entry[2] + hz), (entry[0] + hx, entry[1], entry[2] + hz)), False),
        (((entry[0] + hx, entry[1], entry[2] + hz), (entry[0] + hx, entry[1] + 1, entry[2] + hz)), False),
        (((entry[0], entry[1], entry[2]), (entry[0], entry[1] + 1, entry[2])), True),
    )
    probe_cells: list[Cell] = []
    for (c_lo, c_hi), horiz_only in candidates:
        probe_cells.extend((c_lo, c_hi))
        faces = _HORIZ_FACES if horiz_only else _FACES
        for dx, dy, dz in faces:
            nb = (c_lo[0] + dx, c_lo[1] + dy, c_lo[2] + dz)
            if nb != c_hi:
                probe_cells.append(nb)
    blocks = _batch_blocks(ex, probe_cells)

    chosen_pair: tuple[Cell, Cell] | None = None
    for (c_lo, c_hi), horiz_only in candidates:
        if bare((blocks.get(c_lo) or {}).get("name")) not in AIR:
            continue
        if c_lo in body or eye_to_block_dist(stance, c_lo) > DIG_EYE_REACH:
            continue
        hi_name = bare((blocks.get(c_hi) or {}).get("name"))
        if hi_name in AIR and (c_hi in body or eye_to_block_dist(stance, c_hi) > DIG_EYE_REACH):
            continue
        faces = _HORIZ_FACES if horiz_only else _FACES
        has_solid_ref = any(
            nb != c_hi and bare((blocks.get(nb) or {}).get("name")) in _SOLID_ENCLOSURE
            for dx, dy, dz in faces
            for nb in ((c_lo[0] + dx, c_lo[1] + dy, c_lo[2] + dz),)
        )
        if has_solid_ref:
            chosen_pair = (c_lo, c_hi)
            break
    if chosen_pair is None:
        return []

    dim = str(st.get("dimension") or dimension)
    placed_cells: list[list[int]] = []
    for cell in chosen_pair:
        if bare((blocks.get(cell) or {}).get("name")) not in AIR:
            continue
        plug_blk = _choose_plug_block(ex, state)
        if plug_blk is None:
            break
        st_now, cur_now = _cur_cell(ex, state)
        stance_now = st_now if st_now.get("pos") is not None else state.pos
        if cell in (cur_now, (cur_now[0], cur_now[1] + 1, cur_now[2])) or eye_to_block_dist(stance_now, cell) > DIG_EYE_REACH:
            break
        try:
            res = ex.place(plug_blk, pos=list(cell), stop_on=("death",))
        except Replan as err:
            if _is_preemption(err):
                raise
            break
        voxel_cache_for(state).invalidate(list(cell), dimension=dim)
        yield rec("place", res, item=plug_blk, pos=list(cell), reason="seal_shaft_entrance")
        state.refresh_inventory(ex.client)
        placed_cells.append(list(cell))

    if placed_cells:
        memo.update_mine_shaft(dimension=dimension, seal_cells=placed_cells, sealed=True)
    return placed_cells


def unseal_shaft_entrance(
    ex: Executor,
    state: State,
    *,
    dimension: str = "overworld",
    shaft: Mapping[str, Any] | None = None,
) -> Generator[dict, None, int]:
    """拆开矿井井口封堵方块并刷新台账状态，返回实际清除的封堵方块数量。"""
    memo = memo_for(state)
    shaft_rec = shaft or memo.get_mine_shaft(dimension=dimension)
    if not isinstance(shaft_rec, Mapping) or not shaft_rec.get("seal_cells"):
        return 0
    entry_raw = shaft_rec.get("entry")
    entry = block_cell(entry_raw) if entry_raw is not None else None
    dug_count = 0
    remaining: list[list[int]] = []

    for raw_cell in shaft_rec["seal_cells"]:
        cell = block_cell(raw_cell)
        bname = block_name(ex, list(cell))
        if bname in AIR:
            continue
        st, cur = _cur_cell(ex, state)
        dim = str(st.get("dimension") or dimension)
        stance = st if st.get("pos") is not None else state.pos
        if eye_to_block_dist(stance, cell) > DIG_EYE_REACH:
            move_pos = (
                [entry[0] + 0.5, float(entry[1]), entry[2] + 0.5]
                if entry is not None and cur[1] >= cell[1]
                else [cell[0] + 0.5, float(cell[1] - 1), cell[2] + 0.5]
            )
            try:
                moved = _bounded_move_to(ex, move_pos, rng=2, timeout=10.0)
                yield rec("move_to", moved, pos=move_pos, reason="unseal_shaft_entrance")
            except Replan as err:
                if _is_preemption(err):
                    raise
                yield rec("move_to", pos=move_pos, error_code=failure(err)[0], reason="unseal_shaft_entrance")
            st, cur = _cur_cell(ex, state)
            stance = st if st.get("pos") is not None else state.pos
            bname = block_name(ex, list(cell))
            if bname in AIR:
                continue
            if eye_to_block_dist(stance, cell) > DIG_EYE_REACH:
                remaining.append(list(cell))
                continue

        if bname not in ("dirt", "grass_block", "coarse_dirt") and (pick := best_pickaxe(state)) is not None:
            yield from prepare_mining_pick(ex, state, allowed=frozenset({pick}))
        try:
            res = ex.dig(list(cell), collect=False)
        except Replan as err:
            if _is_preemption(err):
                raise
            yield rec("dig", pos=list(cell), error_code=failure(err)[0], reason="unseal_shaft_entrance")
            remaining.append(list(cell))
            continue
        voxel_cache_for(state).invalidate(list(cell), dimension=dim)
        yield rec("dig", res, pos=list(cell), reason="unseal_shaft_entrance")
        state.refresh_inventory(ex.client)
        if block_name(ex, list(cell)) in AIR:
            dug_count += 1
        else:
            remaining.append(list(cell))

    if remaining:
        memo.update_mine_shaft(dimension=dimension, seal_cells=remaining, sealed=True)
    else:
        memo.update_mine_shaft(dimension=dimension, seal_cells=[], sealed=False)
    return dug_count
