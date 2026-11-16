"""生存与农牧技能共用的近场观察和库存计算。"""
from __future__ import annotations

import math

from ..runtime import EnvBlocked, Replan
from .field import move_local
from .items import AIR, PLACEABLE_BLOCKS, bare
from .observations import completed_advancements, observe_block
from .observations import refresh_inventory as _fresh_inventory
from .recipes import RECIPES
from .sightline import block_center, can_see_block, eye_to_point_dist


_SUPPORT = frozenset(PLACEABLE_BLOCKS) | {
    "grass_block", "coarse_dirt", "rooted_dirt", "sand", "gravel", "clay",
    "bedrock", "deepslate", "stone_bricks", "bricks", "blackstone", "soul_soil",
}
_PLANKS = tuple(item for item in RECIPES if item.endswith("_planks"))
_SIDES = ((1, 0), (-1, 0), (0, 1), (0, -1))
_PLANT_PROGRESS = "husbandry/plant_seed"
_BREED_PROGRESS = "husbandry/breed_an_animal"
_SLEEP_PROGRESS = "adventure/sleep_in_bed"
_FISH_PROGRESS = "husbandry/fishy_business"


def _has_progress(ex, advancement):
    return advancement in completed_advancements(ex)


def _plank_plan(state, count):
    plan = []
    for item in _PLANKS:
        qty = min(count, state.count(item))
        if qty > 0:
            plan.append((item, qty, "consume"))
            count -= qty
        if not count:
            return tuple(plan)
    return ()


def _block(ex, pos):
    return bare(observe_block(ex, pos).get("name"))


def _cell(pos):
    return tuple(math.floor(axis) for axis in pos)


def _above(pos, offset=1):
    return pos[0], pos[1] + offset, pos[2]


def _neighbors(pos, radius=1):
    return [(pos[0] + dx * radius, pos[1], pos[2] + dz * radius) for dx, dz in _SIDES]


def _center(pos):
    return pos[0] + 0.5, pos[1], pos[2] + 0.5


def _on_ground(status):
    return (status.get("ready") is True and status.get("alive") is True
            and status.get("on_ground") is True
            and not any(status.get(key) for key in ("in_water", "submerged", "in_lava", "on_fire", "in_vehicle")))


def _require_ready(status):
    if status.get("ready") is not True or status.get("alive") is not True:
        raise Replan("世界区块和实体加载尚未就绪或角色未存活（not_ready），请稍后重试")


def _standable(ex, pos):
    return (_block(ex, _above(pos, -1)) in _SUPPORT and _block(ex, pos) in AIR
            and _block(ex, _above(pos)) in AIR)


def _can_reach(source, target, reach):
    return eye_to_point_dist(source, target) <= reach


def _stand_for(ex, status, target=None, reach=4, clear=None, *, block_pos=None):
    """仅使用目标周围已复核的整格站位。实际交互触达阈值待实测。"""
    if not _on_ground(status):
        return None
    aim = block_center(block_pos) if block_pos is not None else target
    current = status["pos"]
    if (
        _can_reach(status, aim, reach)
        and _standable(ex, _cell(current))
        and (block_pos is None or can_see_block(ex, status, block_pos))
        and (clear is None or clear(current))
    ):
        return tuple(current)
    cell = _cell(block_pos if block_pos is not None else aim)
    sneaking = status.get("sneaking")
    candidates = [_above(p, dy) for p in _neighbors(cell) for dy in (0, 1)]
    candidates.sort(key=lambda pos: math.dist(current, _center(pos)))
    for pos in candidates:
        spot = _center(pos)
        candidate = {"pos": spot, "sneaking": sneaking}
        if (
            math.dist(current, spot) <= 16
            and _can_reach(candidate, aim, reach)
            and _standable(ex, pos)
            and (block_pos is None or can_see_block(ex, candidate, block_pos))
            and (clear is None or clear(spot))
        ):
            return spot
    return None


def _approach(ex, state, status, stand, target=None, reach=4, *, block_pos=None):
    aim = block_center(block_pos) if block_pos is not None else target
    if tuple(status["pos"]) != tuple(stand):
        status = yield from move_local(ex, state, stand, require_move=True)
    fresh = state.refresh_status(ex.client, max_age_s=0)
    if (
        fresh.get("dimension") != status.get("dimension")
        or not _on_ground(fresh)
        or not _can_reach(fresh, aim, reach)
        or not _standable(ex, _cell(fresh["pos"]))
        or (block_pos is not None and not can_see_block(ex, fresh, block_pos))
    ):
        raise Replan("到点后未确认脚下支撑和交互距离，请换可达位置")
    return fresh


def _site_missing(message, predicate, radius, limit, *, wake="chunk_moved"):
    return EnvBlocked(message, missing_predicate=predicate,
                      searched_volume=f"半径 {radius} 格内最多 {limit} 个候选", wake_on=wake)
