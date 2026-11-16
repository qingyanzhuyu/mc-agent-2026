"""Witness a dry obsidian harvest or confirmed fluid cooling before sending dig."""
from __future__ import annotations

from math import ceil, dist

from ..runtime import EnvBlocked, Replan
from .base import failure, rec
from .field import equip_item, scan_blocks
from .interaction import use_item_on
from .items import AIR, PICKAXE_ORDER, bare
from .nether_site import (
    FLUID, GRAVITY, OBSIDIAN, PORTAL, _above, _approach, _block, _cell, _center, _dimension,
    _flag, _ground, _name, _near, _neighbors, _pos, _stable, _standing, _walk,
)
from .windows import is_storage_window, take_container_item, window_amount, window_view

_CONTAINERS = ("chest", "trapped_chest", "barrel")
_HAZARDS = frozenset({"fire", "soul_fire", "magma_block", "powder_snow", PORTAL})
_WINDOW_UNREACHABLE = {
    "out_of_reach", "out_of_range", "path_not_found", "cannot_interact", "not_loaded", "no_window", "stalled",
}
_PICKS = PICKAXE_ORDER[:2]


def _durable_pick(state):
    inv = state.inventory
    slots = [*inv.get("items", ()), inv.get("held")]
    for item in _PICKS:
        for slot in slots:
            if not slot or bare(slot.get("name")) != item:
                continue
            durability = slot.get("durability")
            if (isinstance(durability, (list, tuple)) and durability
                    and type(durability[0]) is int and durability[0] > 0):
                return item
    return None


def _line_clear(ex, pos, target, *, wet=False):
    """Confirm a short exposed face before asking an action that can auto-path."""
    eye = (pos[0], pos[1] + 1.62, pos[2])
    center = tuple(v + 0.5 for v in target)
    samples = max(1, ceil(4 * dist(eye, center)))
    cells = {_cell(tuple(a + (b - a) * (step / samples) for a, b in zip(eye, center)))
             for step in range(1, samples)}
    cells.discard(target)
    allowed = AIR | {"water"} if wet else AIR
    return all(_name(ex, cell) in allowed for cell in cells)


def _work_route(ex, status, target):
    start = _cell(_pos(status))
    if target[::2] != start[::2] and _near(_pos(status), target, radius=3.75):
        return []
    x, _, z = target
    stands = [(x + dx, start[1], z + dz) for dx, dz in
              ((1, 0), (-1, 0), (0, 1), (0, -1))]
    routes = [_approach(ex, start, site) for site in stands if _near(_center(site), target, radius=3.75)]
    return min((route for route in routes if route is not None), key=len, default=None)


def _at_work_site(ex, state, dimension, target, *, wet=False):
    status = state.refresh_status(ex.client, max_age_s=0)
    return (_dimension(status) == dimension and _standing(ex, status)
            and _cell(_pos(status))[::2] != target[::2] and _near(_pos(status), target, radius=3.75)
            and _line_clear(ex, _pos(status), target, wet=wet))


def _still_safe(ex, state, dimension, target, health):
    status = state.refresh_status(ex.client, max_age_s=0)
    return (_dimension(status) == dimension and _standing(ex, status)
            and status.get("health", 20) >= health and _near(_pos(status), target, radius=4))


def _take_available(ex, state, item, goal_qty, dimension):
    """Only open observed nearby containers, one take operation per request."""
    found = scan_blocks(ex, state, _CONTAINERS, radius=8, limit=3)
    for hit in found["blocks"]:
        pos = tuple(hit["pos"])
        status = state.refresh_status(ex.client, max_age_s=0)
        route = _work_route(ex, status, pos)
        if route is None:
            continue
        yield from _walk(ex, state, route, dimension)
        if not _at_work_site(ex, state, dimension, pos) or _name(ex, pos) != hit["name"]:
            continue
        target = {"pos": list(pos)}
        try:
            view = yield from window_view(ex, target)
        except Replan as exc:
            code, _ = failure(exc)
            if code not in _WINDOW_UNREACHABLE:
                raise
            yield rec("window_view", target=target, error_code=code)
            continue
        if (not is_storage_window(view, hit["name"])
                or not _at_work_site(ex, state, dimension, pos)):
            continue
        state.refresh_inventory(ex.client)
        available = window_amount(view, "container", item)
        take = min(goal_qty - state.count(item), available)
        if take > 0:
            taken = yield from take_container_item(ex, state, target, "container", item, take, view=view)
            if state.count(item) >= goal_qty:
                return
            if taken.get("inventory_full"):
                raise Replan(
                    f"从容器取出 {item} 时背包已满（inventory_full，本次实收 {taken['count']} 个，"
                    f"现持 {state.count(item)}/{goal_qty}），剩余物品留在容器内，请先腾出空位"
                )


def _ore_environment(ex, pos):
    """Inspect the exposed block, its six faces and whether the drop has solid floor."""
    block = _block(ex, pos)
    if bare(block.get("name")) != OBSIDIAN or block.get("diggable") is False:
        return "unsafe"
    names = {}
    for near in (*_neighbors(pos), _ground(pos), _above(pos)):
        snapshot = _block(ex, near)
        name = bare(snapshot.get("name"))
        if not name or _flag(snapshot, "waterlogged") or name in _HAZARDS | {"bubble_column"}:
            return "unsafe"
        names[near] = name
    if names[_above(pos)] in GRAVITY:
        return "unsafe"
    if "lava" in names.values():
        return "lava"
    if not _stable(ex, _ground(pos)):
        return "unsafe"
    return "wet" if "water" in names.values() else "dry"


def _fetch_water(ex, state, dimension):
    if state.count("water_bucket"):
        return True
    if not state.count("bucket"):
        return False
    found = scan_blocks(ex, state, "water", radius=8, limit=6)
    for block in found["blocks"]:
        pos = tuple(block["pos"])
        if (str((block.get("properties") or {}).get("level")) != "0"
                or not _at_work_site(ex, state, dimension, pos)):
            continue
        # Bucket source level and its actual conversion are endpoint probes.
        old_empty, old_full = state.count("bucket"), state.count("water_bucket")
        yield from use_item_on(ex, state, list(pos), "bucket")
        state.refresh_inventory(ex.client)
        if state.count("bucket") != old_empty - 1 or state.count("water_bucket") != old_full + 1:
            raise Replan("舀水后未确认空桶变为水桶")
        return True
    return False


def _cool_ore(ex, state, pos, dimension):
    """Pour once from dry ground, then let block reads decide whether cooling succeeded."""
    if dimension != "overworld" or not (state.count("water_bucket") or state.count("bucket")):
        return False
    status = state.refresh_status(ex.client, max_age_s=0)
    current = _cell(_pos(status))
    prohibited = {current, *_neighbors(current)}
    targets = [side for side in _neighbors(pos) if side[::2] not in {p[::2] for p in prohibited}
               and _name(ex, side) in AIR and _stable(ex, _ground(side))
               and _near(_pos(status), _ground(side), radius=3.75)]
    if not targets or not (yield from _fetch_water(ex, state, dimension)):
        return False
    water = targets[0]
    anchor = _ground(water)
    if (not _at_work_site(ex, state, dimension, anchor)
            or _name(ex, water) not in AIR or not _stable(ex, anchor)):
        return False
    old_empty, old_full = state.count("bucket"), state.count("water_bucket")
    yield from use_item_on(ex, state, list(anchor), "water_bucket")
    state.refresh_inventory(ex.client)
    if state.count("bucket") != old_empty + 1 or state.count("water_bucket") != old_full - 1:
        raise Replan("放水后未确认水桶变为空桶")
    if not _at_work_site(ex, state, dimension, pos, wet=True):
        raise Replan("放水后作业站位被水流影响，停止采掘")
    return _name(ex, water) == "water" and _ore_environment(ex, pos) == "wet"


def _harvest_obsidian(ex, state, goal_qty, dimension):
    found = scan_blocks(ex, state, OBSIDIAN, radius=16, limit=16)
    if not found["blocks"]:
        raise EnvBlocked("未观察到可采的普通黑曜石",
                         missing_predicate="safe_obsidian_source",
                         searched_volume="16 格内最多 16 个露面矿点和 8 格内最多 3 个容器",
                         wake_on="chunk_moved")
    if _durable_pick(state) is None:
        raise Replan("容器未补足黑曜石，采掘需要有可见剩余耐久的钻石镐或下界合金镐")
    cooled = False
    for block in found["blocks"]:
        pos = tuple(block["pos"])
        condition = _ore_environment(ex, pos)
        if condition == "unsafe" or condition == "lava" and cooled:
            continue
        status = state.refresh_status(ex.client, max_age_s=0)
        route = _work_route(ex, status, pos)
        if route is None:
            continue
        yield from _walk(ex, state, route, dimension)
        if not _at_work_site(ex, state, dimension, pos, wet=True):
            continue
        condition = _ore_environment(ex, pos)
        if condition == "lava" and not cooled:
            cooled = True
            if not (yield from _cool_ore(ex, state, pos, dimension)):
                continue
        elif condition not in {"dry", "wet"}:
            continue
        pick = _durable_pick(state)
        if pick is None:
            raise Replan("钻石级镐已无可证实的耐久，换镐再采")
        yield from equip_item(ex, state, pick, min_durability=1)
        if (not _at_work_site(ex, state, dimension, pos, wet=True)
                or _ore_environment(ex, pos) not in {"dry", "wet"}):
            continue
        if _block(ex, pos).get("harvest_tool_ok") is False:
            raise Replan("矿面未确认现持钻石级镐可采")
        before = state.count(OBSIDIAN)
        health = state.hp
        try:
            result = ex.dig(list(pos), collect=True, stop_on=("hurt", "death"))
        except Replan as exc:
            code, extra = failure(exc)
            state.refresh_inventory(ex.client)
            if code in {"dig_unconfirmed", "out_of_reach", "protected_region"}:
                if not _still_safe(ex, state, dimension, pos, health):
                    raise Replan("黑曜石挖掘未确认且站位变化，停止采掘") from exc
                yield rec("dig", item=OBSIDIAN, pos=list(pos), error_code=code, error=extra)
                continue
            raise
        yield rec("dig", result, item=OBSIDIAN, pos=list(pos))
        state.refresh_inventory(ex.client)
        if (result.get("warning") or state.count(OBSIDIAN) <= before
                or _name(ex, pos) not in AIR | {"water"}):
            raise Replan("挖掘后未同时确认目标格变化和黑曜石入包，请检查掉落和液体")
        if not _still_safe(ex, state, dimension, pos, health):
            raise Replan("采后站位有变化，重新确认承重再继续")
        if state.count(OBSIDIAN) >= goal_qty:
            return
    raise EnvBlocked(f"安全来源耗尽，黑曜石实持 {state.count(OBSIDIAN)}/{goal_qty}",
                     missing_predicate="safe_obsidian_source",
                     searched_volume="16 格内最多 16 个露面矿点，最多一次放水",
                     wake_on="chunk_moved")


def _has_silk_touch(entry) -> bool:
    return any(
        isinstance(ench, dict) and bare(ench.get("name")) == "silk_touch"
        and type(ench.get("level")) is int and ench["level"] > 0
        for ench in ((entry or {}).get("enchantments") or ())
    )


def _gather_flint(ex, state, dimension):
    yield from _take_available(ex, state, "flint", 1, dimension)
    if state.count("flint"):
        return
    found = scan_blocks(ex, state, "gravel", radius=12, limit=5)
    for block in found["blocks"]:
        pos = tuple(block["pos"])
        if not _at_work_site(ex, state, dimension, pos):
            continue
        around = {*_neighbors(pos), _above(pos)}
        names = [_name(ex, cell) for cell in around]
        if (_name(ex, pos) != "gravel" or not _stable(ex, _ground(pos))
                or any(not name or name in FLUID | GRAVITY | _HAZARDS for name in names)):
            continue
        inv = state.refresh_inventory(ex.client)
        if _has_silk_touch(inv.get("held")):
            alt = next(
                (bare(row.get("name")) for row in (inv.get("items") or ())
                 if isinstance(row, dict) and row.get("count", 0) > 0 and not _has_silk_touch(row)),
                None,
            )
            if alt is None:
                raise Replan("手持工具带有 silk_touch（精准采集），挖砾石不会掉落燧石")
            yield from equip_item(ex, state, alt)
            if not _at_work_site(ex, state, dimension, pos) or _name(ex, pos) != "gravel":
                continue
        health = state.hp
        try:
            result = ex.dig(list(pos), collect=True, stop_on=("hurt", "death"))
        except Replan as exc:
            code, extra = failure(exc)
            state.refresh_inventory(ex.client)
            if code in {"dig_unconfirmed", "out_of_reach", "protected_region"}:
                if not _still_safe(ex, state, dimension, pos, health):
                    raise Replan("挖砾石未确认且站位改变，停止继续挖掘") from exc
                yield rec("dig", pos=list(pos), purpose="gather_flint", error_code=code, error=extra)
                continue
            raise
        yield rec("dig", result, pos=list(pos), purpose="gather_flint")
        state.refresh_inventory(ex.client)
        if not _still_safe(ex, state, dimension, pos, health):
            raise Replan("挖砾石后站位改变，停止继续挖掘")
        if _name(ex, pos) not in AIR:
            raise Replan("挖掘回执未确认砾石格已清空")
        if state.count("flint"):
            return
    raise EnvBlocked("有限范围内未见可安全取得的燧石",
                     missing_predicate="accessible_flint",
                     searched_volume="8 格内最多 3 个容器，12 格内最多 5 处露面砾石",
                     wake_on="chunk_moved")
