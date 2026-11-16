"""从有支撑的远端站位向标靶面心发一箭，计分由服务器确认。"""
from __future__ import annotations

import math

from ..client import ApiError
from ..runtime import EnvBlocked, Replan
from .base import Skill, rec
from .enchant import _ADJACENT, _craft_plan, _placement_sites, _place_checked, _site_state
from .encounter import _completed, _retreat_clear, _verify_advancement, shoot_projectile, verify_shot_lane
from .field import _cell, _observed_block, _standable_cell, move_local, scan_blocks
from .items import bare
from .registry import register


_ADVANCEMENT = "minecraft:adventure/bullseye"
_AMMO = ("arrow", "spectral_arrow", "tipped_arrow")
_TARGET = "target"
_CRAFTABLE = {_TARGET, "hay_block", "bow", "arrow"}
_MIN_RANGE = 31  # 判据为 30，留 1 格给触面位置和读数舍入。
_MAX_RANGE = 38
_SCAN_RADIUS = 40
_LIMIT = 6
_STRIDE = 5
_ENABLE_TRAPDOOR_DROP = False


def _weapon(state):
    return "crossbow" if state.count("crossbow") and not state.count("bow") else "bow"


def _arrow_kind(state):
    return next((name for name in _AMMO if state.count(name)), "arrow")


def _ammo_ambiguous(state, weapon):
    return ((weapon == "crossbow" and state.count("firework_rocket") > 0)
            or sum(state.count(name) > 0 for name in _AMMO) > 1)


def _requirements(state, *, target):
    arrow = _arrow_kind(state)
    req = {_weapon(state): 1, arrow: 1}
    if target:
        req[_TARGET] = 1
    return req


def _face_center(feet, target):
    """选朝向玩家的主轴面，瞄点置于该面内侧 0.01 格。"""
    center = [v + 0.5 for v in target]
    axis = max((0, 2), key=lambda axis: abs(feet[axis] - center[axis]))
    center[axis] = target[axis] + (0.01 if feet[axis] < center[axis] else 0.99)
    return center


def _horizontal(a, b):
    return math.hypot(a[0] - b[0], a[2] - b[2])


def _foot_center(cell):
    return cell[0] + 0.5, cell[1], cell[2] + 0.5


def _retreat(ex, feet, target):
    x, y, z = _cell(feet)
    for dx, dz in _ADJACENT:
        point = _foot_center((x + dx, y, z + dz))
        if _cell(point) != tuple(target) and _retreat_clear(ex, feet, point):
            return point
    return None


def _range_ok(feet, aim):
    eye = (feet[0], feet[1] + 1.62, feet[2])
    return _horizontal(feet, aim) >= _MIN_RANGE and math.dist(eye, aim) <= _MAX_RANGE


def _walk_plan(ex, origin, target):
    """只接受有支撑的单向水平走廊，落点各向先过承重再逐格复核。"""
    start = _cell(origin)
    for dx, dz in _ADJACENT:
        line = [(start[0] + dx * n, start[1], start[2] + dz * n)
                for n in range(1, _MIN_RANGE + 4)]
        candidate = next((cell for cell in line
                          if _range_ok(_foot_center(cell), _face_center(_foot_center(cell), target))), None)
        if candidate is None or not _standable_cell(ex, candidate):
            continue
        if _retreat(ex, _foot_center(candidate), target) is None:
            continue
        distance = abs(candidate[0] - start[0]) + abs(candidate[2] - start[2])
        cells = line[:distance]
        if tuple(target) not in cells and all(_standable_cell(ex, cell) for cell in cells):
            return [_foot_center(cells[i]) for i in (*range(_STRIDE - 1, len(cells) - 1, _STRIDE), len(cells) - 1)]
    return None


def _prepare_lane(ex, state, *, allow_place):
    origin = _site_state(ex, state)["pos"]
    observed = scan_blocks(ex, state, _TARGET, radius=_SCAN_RADIUS, limit=_LIMIT)
    if observed.get("ready") is False:
        raise Replan("世界区块加载尚未就绪（not_ready），暂无法扫描标靶射道")
    for block in observed["blocks"]:
        pos = tuple(block["pos"])
        aim = _face_center(origin, pos)
        if _range_ok(origin, aim):
            if (retreat := _retreat(ex, origin, pos)) and verify_shot_lane(
                    ex, state, aim, retreat_pos=retreat, min_horizontal=_MIN_RANGE, max_distance=_MAX_RANGE
            )["status"] == "clear":
                return pos, [], False
        elif _horizontal(origin, aim) < _MIN_RANGE and (route := _walk_plan(ex, origin, pos)):
            return pos, route, False
    if allow_place:
        for pos in _placement_sites(ex, state):
            if route := _walk_plan(ex, origin, pos):
                return pos, route, True
    raise EnvBlocked("没有可复核的 30 格标靶射位与退路，请换一处有安全跑道的空旷区域",
                     missing_predicate="safe_bullseye_lane",
                     searched_volume="半径 40 格 6 个标靶候选及四向最多 34 格的同高走廊",
                     wake_on="chunk_moved")


def _follow_route(ex, state, route):
    for point in route:
        status = _site_state(ex, state)
        if _cell(status["pos"]) == _cell(point):
            continue
        if not _retreat_clear(ex, status["pos"], point):
            raise Replan("射位走廊与上次观察不一致，暂不继续后撤")
        reached = yield from move_local(ex, state, point, max_distance=_STRIDE + 1, require_move=True)
        if _cell(reached["pos"]) != _cell(point):
            raise Replan("尚未到指定安全落脚格，请重新确认射位")


def _stop_active_draw(ex, weapon):
    """§3.1、§4.15：只在仍观测到本次武器正在使用时取消。"""
    try:
        using = ex.client.status().get("using_item")
    except ApiError:
        return
    if isinstance(using, dict) and bare(using.get("name")) == weapon:
        ex.stop()


def _shoot_center(ex, state, pos, weapon):
    start = _site_state(ex, state)
    if bare(_observed_block(ex, pos).get("name")) != _TARGET:
        raise Replan("射位复核时标靶已变化")
    aim = _face_center(start["pos"], pos)
    retreat = _retreat(ex, start["pos"], pos)
    if not _range_ok(start["pos"], aim) or retreat is None:
        raise Replan("当前位置未确认是距离合格的安全射位")
    if _completed(ex, _ADVANCEMENT):
        return
    state.invalidate_inventory()
    state.refresh_inventory(ex.client)
    if _ammo_ambiguous(state, weapon):
        raise Replan("射位复核出现多种弹药，无法确认所计划的箭矢")
    arrow = _arrow_kind(state)
    if not state.count(arrow):
        raise Replan("射位复核时已没有预备的箭矢")
    try:
        result = yield from shoot_projectile(ex, state, pos=aim, weapon=weapon, retreat_pos=retreat,
                                             min_horizontal=_MIN_RANGE, max_distance=_MAX_RANGE)
    finally:
        _stop_active_draw(ex, weapon)
    yield rec("bullseye_shot_evidence", None, horizontal=result["lane"]["horizontal_dist"],
              ammo_spent=result["ammo_spent"], aim=result["receipt"].get("aim"))
    if any(item != arrow or qty != 1 for item, qty in result["ammo_spent"].items()):
        raise Replan("标靶射击实扣与所声明的单箭物资不一致")
    confirmed = yield from _verify_advancement(ex, _ADVANCEMENT, 3)
    yield rec("bullseye_advancement_evidence", None, advancement_confirmed=confirmed)


@register
class HitBullseye(Skill):
    name = "hit_bullseye"
    description = "复用或放置标靶，在合格的远端射位射面心并查进度"
    goal = "adventure:bullseye"
    verified = "offline"

    @classmethod
    def blocked(cls, state):
        if _ammo_ambiguous(state, _weapon(state)):
            return "无法确认弓弩和箭矢的唯一扣减，请只保留一种箭并移除弩的烟花"
        if _craft_plan(state, _requirements(state, target=False), _CRAFTABLE).missing:
            return "缺可用弓弩和箭矢，或缺制作它们的材料"
        return ""

    @classmethod
    def material_plan(cls, args, state) -> tuple[tuple[str, int, str], ...]:
        if cls.blocked(state):
            return ()
        ready = _craft_plan(state, _requirements(state, target=False), _CRAFTABLE)
        fallback = _craft_plan(state, _requirements(state, target=True), _CRAFTABLE)
        used = ready if fallback.missing else fallback
        return tuple((item, count, "borrow" if item in ("bow", "crossbow") else mode)
                     for item, count, mode in used.consumed)

    def run(self, ex, state):
        if _completed(ex, _ADVANCEMENT):
            return
        if _ENABLE_TRAPDOOR_DROP:
            raise Replan("活板门落箭路线待实测，当前仅实现直接射击")
        state.invalidate_inventory()
        state.refresh_inventory(ex.client)
        if reason := self.blocked(state):
            raise Replan(reason)
        weapon = _weapon(state)
        plan = _craft_plan(state, _requirements(state, target=True), _CRAFTABLE)
        pos, route, place = _prepare_lane(ex, state, allow_place=not plan.missing)
        if not place:
            plan = _craft_plan(state, _requirements(state, target=False), _CRAFTABLE)
        yield from plan.run(ex, state)
        if plan.steps:
            pos, route, place = _prepare_lane(ex, state, allow_place=bool(state.count(_TARGET)))
        if place:
            yield from _place_checked(ex, state, _TARGET, pos)
        yield from _follow_route(ex, state, route)
        yield from _shoot_center(ex, state, pos, weapon)
