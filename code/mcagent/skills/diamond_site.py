"""Local terrain gates for diamond prospecting and nearby workstation access."""
from __future__ import annotations

import math
from itertools import product

from ..client import ApiError
from ..runtime import Replan
from .field import move_local, scan_blocks
from .items import AIR, PLACEABLE_BLOCKS, bare
from .mining import SOLID_SUPPORT, safe_mine_column
from .observations import observe_block as _block

Cell = tuple[int, int, int]
CARDINALS = ((1, 0), (0, 1), (-1, 0), (0, -1))
DIAMOND_ORES = frozenset(("diamond_ore", "deepslate_diamond_ore"))
CONTAINERS = frozenset(("chest", "trapped_chest", "barrel"))
_STABLE = SOLID_SUPPORT | frozenset(PLACEABLE_BLOCKS) | DIAMOND_ORES | {"bedrock", "obsidian"}
_FLUID = {"water", "lava", "bubble_column", "seagrass", "tall_seagrass", "kelp", "kelp_plant"}
_FACES = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))


def _cell(pos) -> Cell:
    return tuple(map(math.floor, pos))


def _center(cell: Cell) -> tuple[float, float, float]:
    return cell[0] + 0.5, float(cell[1]), cell[2] + 0.5


def _above(cell: Cell, dy: int) -> Cell:
    x, y, z = cell
    return x, y + dy, z


def _dry_name(block: dict) -> str:
    name = bare(block.get("name"))
    if name in _FLUID or (block.get("properties") or {}).get("waterlogged") in (True, "true"):
        return ""
    return name


def _standing(ex, cell: Cell) -> bool:
    return (_dry_name(_block(ex, _above(cell, -1))) in _STABLE
            and all(_dry_name(_block(ex, _above(cell, dy))) in AIR for dy in (0, 1)))


def _safe_status(ex, state, dimension: str | None = None) -> dict:
    status = state.refresh_status(ex.client, max_age_s=0)
    pos = status.get("pos")
    current_dimension = bare(status.get("dimension"))
    if (not pos or len(pos) != 3 or not current_dimension or status.get("ready") is not True
            or status.get("alive") is not True or status.get("on_ground") is not True
            or any(status.get(flag) for flag in ("in_water", "submerged", "in_lava", "on_fire", "in_vehicle"))
            or (dimension is not None and current_dimension != dimension)
            or not _standing(ex, _cell(pos))):
        raise Replan("钻石线未确认当前维度、站立承重或干燥地面，请先回到安全站位")
    return status


def _in_reach(ex, stand, target: Cell, *, reach: float) -> bool:
    """Conservative straight sight line within the local interaction budget."""
    eye = (stand[0], stand[1] + 1.62, stand[2])
    center = tuple(axis + 0.5 for axis in target)
    if math.dist(eye, center) > reach:
        return False
    cell = _cell(eye)
    delta = tuple(b - a for a, b in zip(eye, center))
    sign = tuple(1 if value > 0 else -1 for value in delta)
    while cell != target:
        if _dry_name(_block(ex, cell)) not in AIR:
            return False
        crossings = tuple(
            ((cell[i] + (sign[i] > 0)) - eye[i]) / d if cell[i] != target[i] else math.inf
            for i, d in enumerate(delta)
        )
        first = min(crossings)
        hit = tuple(math.isclose(t, first, rel_tol=0, abs_tol=1e-9) for t in crossings)
        stepped = tuple(v + sign[i] if hit[i] else v for i, v in enumerate(cell))
        if sum(hit) > 1:
            # Be conservative when the ray touches a voxel corner or edge exactly.
            corners = product(*(tuple({a, b}) for a, b in zip(cell, stepped)))
            if any(_dry_name(_block(ex, corner)) not in AIR
                   for corner in corners if corner not in (cell, stepped)):
                return False
        cell = stepped
    return True


def _safe_excavation(ex, origin: Cell, cuts, protected, *, landing: Cell | None = None) -> bool:
    """Check where to cut using current support, clearances and observed neighboring fluid."""
    if not all(_standing(ex, cell) for cell in {origin, *protected[-2:]}):
        return False
    preserved = {_above(cell, dy) for cell in {origin, *protected} for dy in (-1, 0, 1)}
    cuts = set(cuts)
    if cuts & preserved:
        return False
    if landing is not None:
        try:
            if not safe_mine_column(ex, landing):
                return False
        except ApiError as exc:
            if exc.code != "not_loaded":
                raise
            return False
    for cut in cuts:
        x, y, z = cut
        if _dry_name(_block(ex, _above(cut, 1))) not in _STABLE | AIR:
            return False
        if not all(_dry_name(_block(ex, (x + dx, y + dy, z + dz))) for dx, dy, dz in _FACES):
            return False
        block = _block(ex, cut)
        allowed = AIR | SOLID_SUPPORT if landing is not None else DIAMOND_ORES
        name = _dry_name(block)
        if name not in allowed or (name not in AIR and block.get("diggable") is False):
            return False
    return True


def _nearby(ex, state, kinds, *, radius: int, limit: int) -> list[Cell]:
    observed = scan_blocks(ex, state, kinds, radius=radius, limit=limit)
    origin = observed.get("origin")
    return sorted((tuple(hit["pos"]) for hit in observed["blocks"]),
                  key=lambda target: (math.dist(origin, _center(target)), target))


def _approach(ex, state, target: Cell, status, *, dimension: str, protected):
    """Use the current stance or one observed dry cardinal foothold beside it."""
    if _in_reach(ex, status["pos"], target, reach=4):
        return status
    x, y, z = _cell(status["pos"])
    for dx, dz in CARDINALS:
        stand = (x + dx, y, z + dz)
        if (_standing(ex, stand) and _in_reach(ex, _center(stand), target, reach=4)
                and _safe_excavation(ex, stand, (target,), protected)):
            after = yield from move_local(ex, state, _center(stand), rng=1, require_move=True)
            checked = _safe_status(ex, state, dimension)
            if _cell(after["pos"]) != stand or _cell(checked["pos"]) != stand:
                raise Replan("移动回执未确认到达钻矿旁的观察站位")
            return checked
    return None
