"""Conservative geometry from live block and status snapshots for the Nether line."""
from __future__ import annotations

from math import dist, floor, isfinite

from ..runtime import Replan
from .base import rec
from .items import AIR, PLACEABLE_BLOCKS, bare
from .observations import observe_block

CARDINAL = ((1, 0), (-1, 0), (0, 1), (0, -1))
PORTAL = "nether_portal"
OBSIDIAN = "obsidian"
FLUID = frozenset({"water", "lava", "bubble_column"})
GRAVITY = frozenset({"sand", "red_sand", "gravel", "suspicious_sand", "suspicious_gravel"})
SOLID = (frozenset(PLACEABLE_BLOCKS) | {
    "bedrock", "grass_block", "coarse_dirt", "rooted_dirt", "podzol", "deepslate",
    "stone_bricks", "granite", "blackstone", "basalt", "smooth_basalt", "polished_basalt",
    "nether_bricks", "soul_soil", "crying_obsidian", OBSIDIAN,
})
# Soul sand and partial/collidable shapes are intentionally excluded from landed-position checks.
SCAFFOLDS = frozenset(PLACEABLE_BLOCKS) - GRAVITY


def _block(ex, pos):
    return observe_block(ex, list(pos))


def _name(ex, pos):
    return bare(_block(ex, pos).get("name"))


def _pos(status):
    pos = status.get("pos")
    if (not isinstance(pos, (list, tuple)) or len(pos) != 3
            or any(type(v) not in (int, float) or not isfinite(v) for v in pos)):
        raise Replan("缺少可靠站位坐标，请刷新状态")
    return tuple(pos)


def _cell(pos):
    return tuple(map(floor, pos))


def _dimension(status):
    name = bare(status.get("dimension"))
    return {"the_nether": "nether", "the_end": "end"}.get(name, name)


def _ground(pos):
    x, y, z = pos
    return x, y - 1, z


def _center(pos):
    x, y, z = pos
    return [x + 0.5, y, z + 0.5]


def _neighbors(pos):
    x, y, z = pos
    return [(x + dx, y, z + dz) for dx, dz in CARDINAL]


def _above(pos):
    x, y, z = pos
    return x, y + 1, z


def _flag(block, key):
    return str((block.get("properties") or {}).get(key, "")).lower() == "true"


def _stable(ex, pos):
    block = _block(ex, pos)
    name = bare(block.get("name"))
    return (name in SOLID or name.endswith("_ore")) and not _flag(block, "waterlogged")


def _standable(ex, pos, *, through_portal=False):
    allowed = AIR | {PORTAL} if through_portal else AIR
    return (_stable(ex, _ground(pos))
            and _name(ex, pos) in allowed and _name(ex, _above(pos)) in allowed)


def _hazard(status):
    return (status.get("alive") is False or any(status.get(key) for key in
            ("on_fire", "in_lava", "in_water", "submerged", "in_vehicle")))


def _standing(ex, status, *, through_portal=False):
    return (status.get("ready") is True and not _hazard(status) and status.get("on_ground") is True
            and _standable(ex, _cell(_pos(status)), through_portal=through_portal))


def _near(pos, target, radius=4):
    """Conservative block-center reach from a standing player's eye. Endpoint threshold awaits P8."""
    return dist((pos[0], pos[1] + 1.62, pos[2]), tuple(v + 0.5 for v in target)) <= radius


def _approach(ex, start, dest):
    """Return a witnessed same-level route, choosing either of two axis-aligned paths."""
    if start[1] != dest[1] or abs(start[0] - dest[0]) + abs(start[2] - dest[2]) > 6:
        return None
    for axes in ((0, 2), (2, 0)):
        steps = []
        current = list(start)
        for axis in axes:
            while current[axis] != dest[axis]:
                current[axis] += 1 if current[axis] < dest[axis] else -1
                steps.append(tuple(current))
        if all(_standable(ex, step) for step in steps):
            return steps
    return None


def _walk(ex, state, route, dimension, *, through_portal=False):
    """Use exact-cell short steps; stop if the server lands anywhere else."""
    for dest in route:
        before = state.refresh_status(ex.client, max_age_s=0)
        x, y, z = previous = _cell(_pos(before))
        if (_dimension(before) != dimension or not _standing(ex, before, through_portal=through_portal)
                or dest[1] != y or abs(dest[0] - x) + abs(dest[2] - z) != 1
                or not _standable(ex, dest)):
            raise Replan("下一格的承重、净空或同维度路线已经变化")
        yield rec("move_to", ex.move_to(pos=_center(dest), rng=0, stop_on=("hurt", "death")),
                  from_cell=list(previous), pos=list(dest))
        after = state.refresh_status(ex.client, max_age_s=0)
        if (_dimension(after) != dimension or _cell(_pos(after)) != dest or not _standing(ex, after)
                or after.get("health", 20) < before.get("health", 20)):
            raise Replan("移动后未确认到达预定安全格，停止下一步")
