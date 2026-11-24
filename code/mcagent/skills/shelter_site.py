"""出生点避难所地形选址与轻量整平 L2 原语（支持丘陵 1 格高差削平/垫平与清草/花/低矮树叶）。

遵守 AGENTS.md §9 全部约束：
- 纯 L2 原语，禁止 `@register`、禁止继承 `Skill`、禁止导入 `mcagent.loop`；
- 严禁破坏既有营地箱/工作台/熔炉/床，严禁悬空建房，严禁进水/岩浆/保护区。
"""
from __future__ import annotations

import math
from collections.abc import Callable, Generator, Mapping, Sequence
from typing import Any, Optional

from ..runtime import EnvBlocked, Executor, State
from .camp_plan import is_protected_cell
from .field import _observed_block
from .items import AIR, PASSABLE_COVER, bare, is_safe_solid_floor
from .sightline import DIG_EYE_REACH, STATION_EYE_REACH, block_cell
from .voxel_cache import client_get_blocks_batch, has_batch_get_blocks
from .world_memo import BED_BLOCKS, memo_for

Cell = tuple[int, int, int]
_FLUID_OR_BEDROCK: frozenset[str] = frozenset({
    "water", "lava", "flowing_water", "flowing_lava", "bubble_column", "bedrock", "obsidian", "nether_portal",
})
_PROTECTED_CAMP_BLOCKS: frozenset[str] = frozenset({
    "chest", "trapped_chest", "ender_chest", "furnace", "blast_furnace",
    "smoker", "crafting_table", "torch", "wall_torch",
}) | frozenset(BED_BLOCKS)
_EXTRA_FOLIAGE: frozenset[str] = frozenset({
    "short_grass", "tall_grass", "fern", "large_fern", "dead_bush", "vine",
    "snow", "snow_layer", "leaf_litter", "moss_carpet", "azalea", "flowering_azalea",
    "pink_petals", "wildflowers", "bush", "firefly_bush", "sweet_berry_bush",
    "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
    "red_tulip", "orange_tulip", "white_tulip", "pink_tulip",
    "oxeye_daisy", "cornflower", "lily_of_the_valley", "sunflower",
    "lilac", "rose_bush", "peony", "brown_mushroom", "red_mushroom", "sugar_cane",
})
_CLEARABLE_COVER: frozenset[str] = AIR | PASSABLE_COVER | _EXTRA_FOLIAGE
_TRIM_TERRAIN: frozenset[str] = frozenset({
    "grass_block", "dirt", "coarse_dirt", "podzol", "rooted_dirt", "mud", "clay",
    "sand", "red_sand", "gravel", "stone", "cobblestone", "cobbled_deepslate",
    "andesite", "diorite", "granite", "tuff", "deepslate", "snow_block", "moss_block",
})
_PAD_BLOCKS: tuple[str, ...] = ("dirt", "cobblestone", "cobbled_deepslate", "stone_bricks", "blackstone")
_RING_RADII: tuple[int, ...] = (10, 14, 18, 22, 26, 32, 38)
_RING_DIRS: tuple[tuple[float, float], ...] = (
    (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0),
    (0.707, 0.707), (-0.707, 0.707), (0.707, -0.707), (-0.707, -0.707),
)
MAX_PAD_CELLS = 10
MAX_TRIM_CELLS = 10
MAX_TOTAL_EDITS = 14


def is_clearable_cover(name: str) -> bool:
    """判断方块是否为空、草/花/积雪或低矮树叶等可安全清理的表层覆盖物。"""
    b = bare(name)
    return not b or b in _CLEARABLE_COVER or b.endswith("_leaves")


def _read_blocks(ex: Executor, cells: Sequence[Cell]) -> dict[Cell, dict]:
    if has_batch_get_blocks(getattr(ex, "client", None)):
        return client_get_blocks_batch(ex.client, cells)
    return {c: _observed_block(ex, c) for c in cells}


def _center_column_ok(snap: Mapping[Cell, dict], cx: int, cy: int, cz: int) -> bool:
    """只凭中心柱 5 格判断这块地基是否**必然**不合格（粗筛）。

    判定严格等价于 `evaluate_shelter_site` 里 dx=dz=0（内圈列）那一支：
    中心柱不通过 ⇒ 全量评估一定返回 None。因此先用 5 格砍掉绝大多数候选，
    避免为每个候选都去打 5×5×5=125 次 `/get_block`（实测选址一轮上千次请求）。
    """
    if is_protected_cell((cx, cy - 1, cz)) or is_protected_cell((cx, cy, cz)):
        return False
    b_m2 = snap.get((cx, cy - 2, cz)) or {}
    b_m1 = snap.get((cx, cy - 1, cz)) or {}
    names = {bare((snap.get((cx, cy + dy, cz)) or {}).get("name")) for dy in (-1, 0, 1, 2)}
    if any(n in _FLUID_OR_BEDROCK for n in names):
        return False
    n_m2 = bare(b_m2.get("name"))
    n_m1 = bare(b_m1.get("name"))
    n_0 = bare((snap.get((cx, cy, cz)) or {}).get("name"))
    n_1 = bare((snap.get((cx, cy + 1, cz)) or {}).get("name"))
    if is_safe_solid_floor(b_m1):
        if n_0 in _PROTECTED_CAMP_BLOCKS or is_clearable_cover(n_0):
            # 内圈：净空格必须可清理；被保护的营地设施格反而允许
            return is_clearable_cover(n_1) or n_1 in _PROTECTED_CAMP_BLOCKS
        return n_0 in _TRIM_TERRAIN and is_clearable_cover(n_1)
    if is_clearable_cover(n_m1) and n_m2 not in _FLUID_OR_BEDROCK and is_safe_solid_floor(b_m2):
        return is_clearable_cover(n_0) and is_clearable_cover(n_1)
    return False


def evaluate_shelter_site(ex: Executor, cx: int, cy: int, cz: int, *, radius: int = 2) -> Optional[dict[str, Any]]:
    """评估 5×5 避难所候选地基：支持 ±1 格高差轻量削平/垫平，严禁破坏营地箱/悬空/进水/保护区。"""
    if is_protected_cell((cx, cy, cz)):
        return None
    # 粗筛：先花 5 次请求看中心柱，不合格的候选直接淘汰（原先一律 125 次）
    if not _center_column_ok(_read_blocks(ex, [(cx, cy + dy, cz) for dy in (-2, -1, 0, 1, 2)]), cx, cy, cz):
        return None
    cells = [
        (cx + dx, cy + dy, cz + dz)
        for dx in range(-radius, radius + 1)
        for dz in range(-radius, radius + 1)
        for dy in (-2, -1, 0, 1, 2)
    ]
    snap = _read_blocks(ex, cells)

    pad_cells: list[Cell] = []
    trim_cells: list[Cell] = []
    foliage_cells: list[Cell] = []

    for dx in range(-radius, radius + 1):
        for dz in range(-radius, radius + 1):
            fx, fz = cx + dx, cz + dz
            is_perimeter = max(abs(dx), abs(dz)) == radius
            b_m2 = snap.get((fx, cy - 2, fz), {})
            b_m1 = snap.get((fx, cy - 1, fz), {})
            b_0 = snap.get((fx, cy, fz), {})
            b_1 = snap.get((fx, cy + 1, fz), {})
            b_2 = snap.get((fx, cy + 2, fz), {})

            n_m2 = bare((b_m2 or {}).get("name"))
            n_m1 = bare((b_m1 or {}).get("name"))
            n_0 = bare((b_0 or {}).get("name"))
            n_1 = bare((b_1 or {}).get("name"))
            n_2 = bare((b_2 or {}).get("name"))

            if is_protected_cell((fx, cy - 1, fz)) or is_protected_cell((fx, cy, fz)):
                return None
            if any(n in _FLUID_OR_BEDROCK for n in (n_m1, n_0, n_1, n_2)):
                return None
            if is_perimeter and (n_0 in _PROTECTED_CAMP_BLOCKS or n_1 in _PROTECTED_CAMP_BLOCKS or n_2 in _PROTECTED_CAMP_BLOCKS):
                return None

            if is_safe_solid_floor(b_m1):
                if (not is_perimeter and n_0 in _PROTECTED_CAMP_BLOCKS) or is_clearable_cover(n_0):
                    if n_0 and n_0 not in AIR and n_0 not in _PROTECTED_CAMP_BLOCKS:
                        foliage_cells.append((fx, cy, fz))
                    if not is_clearable_cover(n_1):
                        if is_perimeter or n_1 not in _PROTECTED_CAMP_BLOCKS:
                            return None
                    elif n_1 and n_1 not in AIR:
                        foliage_cells.append((fx, cy + 1, fz))
                elif n_0 in _TRIM_TERRAIN and is_clearable_cover(n_1):
                    trim_cells.append((fx, cy, fz))
                    if n_1 and n_1 not in AIR:
                        foliage_cells.append((fx, cy + 1, fz))
                else:
                    return None
            elif is_clearable_cover(n_m1) and n_m2 not in _FLUID_OR_BEDROCK and is_safe_solid_floor(b_m2):
                if not is_clearable_cover(n_0) or not is_clearable_cover(n_1):
                    return None
                if n_m1 and n_m1 not in AIR:
                    foliage_cells.append((fx, cy - 1, fz))
                pad_cells.append((fx, cy - 1, fz))
                if n_0 and n_0 not in AIR:
                    foliage_cells.append((fx, cy, fz))
                if n_1 and n_1 not in AIR:
                    foliage_cells.append((fx, cy + 1, fz))
            else:
                return None

    edit_cost = len(pad_cells) + len(trim_cells)
    if len(pad_cells) > MAX_PAD_CELLS or len(trim_cells) > MAX_TRIM_CELLS or edit_cost > MAX_TOTAL_EDITS:
        return None
    return {
        "center": (cx, cy, cz),
        "pad_cells": pad_cells,
        "trim_cells": trim_cells,
        "foliage_cells": foliage_cells,
        "edit_cost": edit_cost,
    }


def _probe_surface_ys(ex: Executor, cx: int, base_y: int, cz: int, *, max_delta: int = 4) -> list[int]:
    """在 `[base_y - max_delta, base_y + max_delta]` 批量探查真实地表高度，解决丘陵固定 Y 失配。"""
    cells = [(cx, base_y + dy, cz) for dy in range(-max_delta - 1, max_delta + 2)]
    snap = _read_blocks(ex, cells)
    hits: list[int] = []
    for dy in range(-max_delta, max_delta + 1):
        sy = base_y + dy
        below = snap.get((cx, sy - 1, cz), {})
        here_n = bare((snap.get((cx, sy, cz)) or {}).get("name"))
        if is_safe_solid_floor(below) and (is_clearable_cover(here_n) or here_n in _PROTECTED_CAMP_BLOCKS):
            hits.append(sy)
    if base_y not in hits:
        hits.append(base_y)
    hits.sort(key=lambda y: abs(y - base_y))
    return hits[:3]


def select_shelter_center(ex: Executor, state: State, dim: str) -> tuple[int, int, int]:
    """在营地、当前位置及出生点保护区外缘 r=10..38 环带选出最佳平坦或 1 格可整平 5×5 地基。"""
    st = state.refresh_status(ex.client, max_age_s=0)
    cur = block_cell(st.get("pos"))
    memo = memo_for(state)
    camp = memo.get_camp(dimension=dim)
    spawn = memo.get_spawn(dimension=dim)

    priority_pts: list[Cell] = []
    if isinstance(camp, Mapping) and camp.get("pos"):
        priority_pts.append(block_cell(camp["pos"]))
    priority_pts.append(cur)

    anchor = block_cell(spawn) if spawn is not None else cur
    seen: set[Cell] = set()
    best: Optional[tuple[int, float, Cell]] = None

    def _check_cand(cand: Cell, *, priority_bonus: float = 0.0) -> Optional[Cell]:
        nonlocal best
        if cand in seen:
            return None
        seen.add(cand)
        ev = evaluate_shelter_site(ex, cand[0], cand[1], cand[2], radius=2)
        if ev is None:
            return None
        score = (int(ev["edit_cost"]), round(math.dist(cand, anchor) - priority_bonus, 2), cand)
        if best is None or score < best:
            best = score
        return cand if ev["edit_cost"] == 0 else None

    for pt in priority_pts:
        if (flat := _check_cand(pt, priority_bonus=20.0)) is not None:
            return flat
        for sy in _probe_surface_ys(ex, pt[0], pt[1], pt[2], max_delta=2):
            if (flat := _check_cand((pt[0], sy, pt[2]), priority_bonus=15.0)) is not None:
                return flat

    for r in _RING_RADII:
        for ux, uz in _RING_DIRS:
            rx, rz = anchor[0] + round(r * ux), anchor[2] + round(r * uz)
            for sy in _probe_surface_ys(ex, rx, anchor[1], rz, max_delta=4):
                if (flat := _check_cand((rx, sy, rz))) is not None:
                    return flat

    if best is not None:
        return best[2]

    raise EnvBlocked(
        "出生点保护区外缘 r=10..38 环带未找到平坦或可轻量整平的干燥 5×5 避难所地基",
        missing_predicate="shelter_flat_site",
        searched_volume="出生点环带 r=10..38 5×5 地基（含 ±1 格轻量整平）",
        wake_on="chunk_moved",
    )


def _pick_pad_block(state: State) -> Optional[str]:
    return next((b for b in _PAD_BLOCKS if state.count(b) >= 1), None)


def level_shelter_footprint(
    ex: Executor,
    state: State,
    center: Cell,
    *,
    memo: Any,
    dim: str,
    clear_fn: Callable[..., Generator[dict, None, bool]],
    place_fn: Callable[..., Generator[dict, None, bool]],
    site_eval: Optional[Mapping[str, Any]] = None,
    skip_cells: Sequence[Cell] = (),
) -> Generator[dict, None, dict[str, int]]:
    """对已选定的 5×5 避难所地基执行轻量整平：清草/花/低叶、削平 1 格凸起、垫平 1 格低洼。"""
    ev = site_eval if isinstance(site_eval, Mapping) else evaluate_shelter_site(ex, center[0], center[1], center[2], radius=2)
    if ev is None:
        return {"trimmed": 0, "padded": 0, "cleared": 0}
    skip_set = {tuple(c) for c in skip_cells}
    trimmed, padded, cleared = 0, 0, 0
    for cell in ev.get("foliage_cells") or ():
        if tuple(cell) in skip_set:
            continue
        if (yield from clear_fn(ex, state, cell, skip_names=_PROTECTED_CAMP_BLOCKS)):
            cleared += 1
    for cell in ev.get("trim_cells") or ():
        if tuple(cell) in skip_set:
            continue
        if (yield from clear_fn(ex, state, cell, skip_names=_PROTECTED_CAMP_BLOCKS)):
            trimmed += 1
    for cell in ev.get("pad_cells") or ():
        if tuple(cell) in skip_set:
            continue
        blk = _pick_pad_block(state)
        if blk is None:
            break
        if (yield from place_fn(ex, state, cell, blk, memo=memo, dim=dim)):
            padded += 1
    return {"trimmed": trimmed, "padded": padded, "cleared": cleared}
