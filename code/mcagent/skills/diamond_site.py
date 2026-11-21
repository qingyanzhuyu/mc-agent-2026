"""Local terrain gates for diamond prospecting and nearby workstation access."""
from __future__ import annotations

import math
import time

from ..client import ApiError
from ..runtime import Replan
from .field import move_local, scan_blocks
from .items import AIR, PLACEABLE_BLOCKS, bare
from .mining import SOLID_SUPPORT, safe_mine_column
from .observations import observe_block as _block
from .sightline import can_reach_block
from .voxel_cache import client_get_blocks_batch, has_batch_get_blocks

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


_SETTLE_POLLS = 5
_SETTLE_DELAY_S = 0.12


def _safe_status(ex, state, dimension: str | None = None) -> dict:
    status = state.refresh_status(ex.client, max_age_s=0)
    for _ in range(_SETTLE_POLLS):
        if (status.get("on_ground") is True or status.get("ready") is not True
                or status.get("alive") is not True
                or any(status.get(flag) for flag in ("in_water", "submerged", "in_lava", "on_fire", "in_vehicle"))):
            break
        time.sleep(_SETTLE_DELAY_S)
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
    return can_reach_block(
        ex,
        stand,
        target,
        reach=reach,
        passable=AIR,
        reject_waterlogged=True,
        face_fallback=False,
    )


def _safe_excavation(ex, origin: Cell, cuts, protected, *, landing: Cell | None = None) -> bool:
    """Check where to cut using current support, clearances and observed neighboring fluid."""
    preserved = {_above(cell, dy) for cell in {origin, *protected} for dy in (-1, 0, 1)}
    cuts = set(cuts)
    if cuts & preserved:
        return False
    batch: dict[Cell, dict] = {}
    if has_batch_get_blocks(getattr(ex, "client", None)):
        probe: list[Cell] = []
        for cell in {origin, *protected[-2:]}:
            probe.extend((_above(cell, -1), _above(cell, 0), _above(cell, 1)))
        if landing is not None:
            lx, ly, lz = landing
            probe.extend(((lx, ly - 1, lz), (lx, ly, lz), (lx, ly + 1, lz), (lx, ly + 2, lz), (lx, ly + 3, lz)))
        for cut in cuts:
            cx, cy, cz = cut
            probe.append(cut)
            probe.append(_above(cut, 1))
            probe.extend((cx + dx, cy + dy, cz + dz) for dx, dy, dz in _FACES)
        batch = client_get_blocks_batch(ex.client, probe)
    read_blk = (lambda cell: batch[cell] if cell in batch else _block(ex, cell)) if batch else (lambda cell: _block(ex, cell))
    for cell in {origin, *protected[-2:]}:
        if not (_dry_name(read_blk(_above(cell, -1))) in _STABLE
                and all(_dry_name(read_blk(_above(cell, dy))) in AIR for dy in (0, 1))):
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
        if _dry_name(read_blk(_above(cut, 1))) not in _STABLE | AIR:
            return False
        if not all(_dry_name(read_blk((x + dx, y + dy, z + dz))) for dx, dy, dz in _FACES):
            return False
        block = read_blk(cut)
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
    if _in_reach(ex, status, target, reach=4):
        return status
    x, y, z = _cell(status["pos"])
    for dx, dz in CARDINALS:
        stand = (x + dx, y, z + dz)
        next_stance = {"pos": _center(stand), "sneaking": bool(status.get("sneaking"))}
        if (_standing(ex, stand) and _in_reach(ex, next_stance, target, reach=4)
                and _safe_excavation(ex, stand, (target,), protected)):
            after = yield from move_local(ex, state, _center(stand), rng=1, require_move=True)
            checked = _safe_status(ex, state, dimension)
            if _cell(after["pos"]) != stand or _cell(checked["pos"]) != stand:
                raise Replan("移动回执未确认到达钻矿旁的观察站位")
            return checked
    return None
