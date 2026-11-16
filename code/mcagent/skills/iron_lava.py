"""Fill an empty bucket only from a freshly observed, safely reachable source."""
from __future__ import annotations

import math
from itertools import pairwise

from ..client import ApiError
from ..runtime import EnvBlocked, Replan
from .base import Skill, rec
from .field import move_local, scan_blocks
from .interaction import use_item_on
from .items import AIR, PLACEABLE_BLOCKS, bare
from .mining import SOLID_SUPPORT
from .registry import register


_RADIUS = 16
_LIMIT = 12
_MAX_ROUTE = 8
_CARDINALS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_SUPPORT = SOLID_SUPPORT | set(PLACEABLE_BLOCKS) | {"bedrock", "obsidian", "blackstone"}


def _observe(ex, pos):
    try:
        return ex.client.get_block(list(pos))
    except ApiError as exc:
        if exc.code != "not_loaded":
            raise
        return {}


def _lava_source(block):
    """§3.4 exposes properties. The lava level encoding is pending a target probe."""
    level = (block.get("properties") or {}).get("level")
    return bare(block.get("name")) == "lava" and str(level) == "0"


def _dry_stance(ex, cell):
    x, y, z = cell
    return (bare(_observe(ex, (x, y - 1, z)).get("name")) in _SUPPORT
            and all(bare(_observe(ex, (x, y + dy, z)).get("name")) in AIR for dy in (0, 1)))


def _grounded_cell(ex, status):
    if (status.get("on_ground") is not True or status.get("alive") is False
            or any(status.get(flag) for flag in ("in_lava", "in_water", "on_fire", "in_vehicle"))):
        raise Replan("先脱离液体或危险状态，再装岩浆")
    cell = tuple(map(math.floor, status["pos"]))
    if not _dry_stance(ex, cell):
        raise Replan("当前脚下承重和净空未确认，先退到干燥平台")
    return cell


def _approach_safe(ex, source, stance, direction):
    """The source stays two cells away with one observed dry retreat cell behind us."""
    dx, dz = direction
    x, y, z = stance
    return (_dry_stance(ex, stance)
            and _dry_stance(ex, (x + dx, y, z + dz))
            and all(bare(_observe(ex, (x - dx, y + dy, z - dz)).get("name")) in AIR
                    for dy in (0, 1))
            and _lava_source(_observe(ex, source)))


def _observed_route(ex, start, end):
    """Allow a short same-level route only if each cell on a two-segment path is dry."""
    if abs(start[0] - end[0]) + abs(start[2] - end[2]) > _MAX_ROUTE:
        return False
    for corner in ((start[0], start[1], end[2]), (end[0], start[1], start[2])):
        cells = []
        for first, last in pairwise((start, corner, end)):
            steps = max(abs(first[0] - last[0]), abs(first[2] - last[2]))
            if steps:
                dx = (last[0] - first[0]) // steps
                dz = (last[2] - first[2]) // steps
                cells.extend((first[0] + i * dx, first[1], first[2] + i * dz)
                             for i in range(1, steps + 1))
        if all(_dry_stance(ex, cell) for cell in cells):
            return True
    return False


def _blocked(missing):
    return EnvBlocked(
        "未确认岩浆源格，请到新区域重新观察" if missing == "lava_source"
        else "发现岩浆源，但没有已复核的干燥站位和退路，请换观察位置",
        missing_predicate=missing,
        searched_volume=f"当前视点半径 {_RADIUS} 格内最多 {_LIMIT} 个暴露岩浆候选",
        wake_on="chunk_moved",
    )


def _completed_lava_advancement(ex):
    try:
        result = ex.client.advancements(status="completed", include_recipes=False)
    except ApiError as exc:
        if exc.code != "not_ready":
            raise
        return False
    return result.get("ready") is True and any(
        bare(row.get("id")) == "story/lava_bucket" and row.get("status") == "completed"
        for row in result.get("advancements", ())
    )


@register
class FillLavaBucket(Skill):
    name = "fill_lava_bucket"
    description = "从已复核的可达岩浆源装桶，保留安全退路"
    goal = "acq:lava_bucket"
    verified = "offline"

    @classmethod
    def blocked(cls, state):
        return "" if state.count("lava_bucket") or state.count("bucket") else "先制作一个空桶"

    @classmethod
    def material_plan(cls, args: dict, state) -> tuple[tuple[str, int, str], ...]:
        return () if state.count("lava_bucket") else (("bucket", 1, "borrow"),)

    def run(self, ex, state):
        state.invalidate_inventory()
        state.refresh_inventory(ex.client)
        if state.count("lava_bucket"):
            return
        if reason := self.blocked(state):
            raise Replan(reason)

        scan = scan_blocks(ex, state, "lava", radius=_RADIUS, limit=_LIMIT)
        sources = [tuple(block["pos"]) for block in scan["blocks"] if _lava_source(block)]
        if not sources:
            raise _blocked("lava_source")
        status = state.refresh_status(ex.client, max_age_s=0)
        if status.get("dimension") != scan["dimension"]:
            raise Replan("维度变化后应重新搜索岩浆源")
        start = _grounded_cell(ex, status)
        options = [
            (source, (x + 2 * dx, start[1], z + 2 * dz), (dx, dz))
            for source in sources for x, y, z in (source,) if start[1] - y in (0, 1)
            for dx, dz in _CARDINALS
        ]
        options.sort(key=lambda entry: math.dist(start, entry[1]))
        choice = next((option for option in options if _observed_route(ex, start, option[1])
                       and _approach_safe(ex, *option)), None)
        if choice is None:
            raise _blocked("safe_lava_access")
        source, stance, direction = choice
        if stance != start:
            dest = (stance[0] + 0.5, stance[1], stance[2] + 0.5)
            yield from move_local(ex, state, dest, max_distance=_MAX_ROUTE, require_move=True)

        status = state.refresh_status(ex.client, max_age_s=0)
        if status.get("dimension") != scan["dimension"] or _grounded_cell(ex, status) != stance:
            raise Replan("未到达预先核对的岩浆装桶站位")
        if not _lava_source(_observe(ex, source)):
            raise _blocked("lava_source")
        if not _approach_safe(ex, source, stance, direction):
            raise _blocked("safe_lava_access")
        state.invalidate_inventory()
        state.refresh_inventory(ex.client)
        lava_before, empty_before = state.count("lava_bucket"), state.count("bucket")
        if lava_before:
            return
        yield from use_item_on(ex, state, source, "bucket")
        if state.count("lava_bucket") <= lava_before or state.count("bucket") >= empty_before:
            raise Replan("装桶后背包未确认空桶转为岩浆桶")
        yield rec("lava_bucket_evidence", inventory_confirmed=True,
                  advancement_confirmed=_completed_lava_advancement(ex))

    def verify_done(self, state, before_counts):
        return "" if state.count("lava_bucket") else "背包未确认岩浆桶"
