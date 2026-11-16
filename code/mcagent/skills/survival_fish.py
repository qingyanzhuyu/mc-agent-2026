"""有界选择可见岸位并验证单次渔获。"""
from __future__ import annotations

import math

from ..client import ApiError
from ..runtime import Replan
from .base import Skill, rec
from .field import scan_blocks
from .items import AIR, bare
from .recipes import craft_recipe
from .registry import register
from .survival import (
    _FISH_PROGRESS, _above, _approach, _block, _cell, _center, _fresh_inventory,
    _has_progress, _on_ground, _site_missing, _stand_for,
)


_FISH = frozenset({"cod", "salmon", "pufferfish", "tropical_fish"})
_ROD = "fishing_rod"
_WATER_RADIUS = 16
_CAST_REACH = 6
_CAST_TIMEOUT_MS = 45_000


def _open_water_surface(ex, pos):
    """从水面起只证明可见的八格没有遮挡，真实天空判定待探针。"""
    return _block(ex, pos) == "water" and all(_block(ex, _above(pos, n)) in AIR for n in range(1, 9))


def _cast_clear(ex, stand, pos):
    """静态短视线作为可抛门槛，浮漂弹道由服务端决定。"""
    eye, target = _above(stand, 1.62), _above(_center(pos), 0.95)
    samples = max(1, math.ceil(math.dist(eye, target) * 4))
    cells = {_cell(tuple(a + (b - a) * index / samples for a, b in zip(eye, target)))
             for index in range(1, samples)}
    return all(_block(ex, cell) in AIR for cell in cells - {tuple(pos)})


def _cast_site(ex, state):
    candidates = scan_blocks(ex, state, "water", radius=_WATER_RADIUS, limit=10)["blocks"]
    status = state.refresh_status(ex.client, max_age_s=0)
    for row in candidates:
        pos = tuple(row["pos"])
        if not _open_water_surface(ex, pos):
            continue
        stand = _stand_for(ex, status, _center(pos), reach=_CAST_REACH,
                           clear=lambda candidate: _cast_clear(ex, candidate, pos))
        if stand is not None:
            return pos, stand, status
    raise _site_missing("未观察到有安全岸位且水面上方无遮挡的钓点",
                        "reachable_open_water", _WATER_RADIUS, 10)


def _caught_fish(receipt):
    """§4.5 只列字段名。字符串或一个明确的物品对象均独立要求后验。"""
    caught = receipt.get("caught")
    if isinstance(caught, dict):
        caught = caught.get("name") or caught.get("item")
    return bare(caught) if isinstance(caught, str) and bare(caught) in _FISH else None


def _stop_active_fishing(ex, state):
    """阻塞抛收已结束时无需额外动作，只清仍可观察到的钓鱼使用态。"""
    try:
        status = state.refresh_status(ex.client, max_age_s=0)
        if bare((status.get("using_item") or {}).get("name")) == _ROD:
            ex.stop()
    except ApiError:
        pass  # 保留原始回执错误或抢占语义。


@register
class CatchFish(Skill):
    name = "catch_fish"
    goal = "goal:fishy_business"
    description = "在安全水边抛收一竿并核对四种鱼之一"
    verified = "offline"

    @classmethod
    def blocked(cls, state):
        ready = state.count(_ROD) or (state.count("string") >= 2 and state.count("stick") >= 3)
        return "" if ready else "先备钓竿，或 2 根线和 3 根木棍"

    @classmethod
    def material_plan(cls, args: dict, state) -> tuple[tuple[str, int, str], ...]:
        if state.count(_ROD):
            return ((_ROD, 1, "borrow"),)
        return (("string", 2, "consume"), ("stick", 3, "consume"))

    def run(self, ex, state):
        if _has_progress(ex, _FISH_PROGRESS):
            return
        _fresh_inventory(ex, state)
        if not state.count(_ROD):
            if state.count("string") < 2 or state.count("stick") < 3:
                raise Replan("缺钓竿，请补齐 2 根线和 3 根木棍再到工作台制作")
            _cast_site(ex, state)
            yield from craft_recipe(ex, state, _ROD, goal_qty=1)
        pos, stand, status = _cast_site(ex, state)
        fresh = yield from _approach(ex, state, status, stand, _center(pos), reach=_CAST_REACH)
        if not _open_water_surface(ex, pos) or not _cast_clear(ex, fresh["pos"], pos):
            raise Replan("抛竿前复读水面已变化，请换钓点")
        _fresh_inventory(ex, state)
        if not state.count(_ROD):
            raise Replan("抛竿前复读背包未见钓竿")
        before = {name: state.count(name) for name in _FISH}
        try:
            result = ex.use_item(_ROD, target={"pos": list(pos)},
                                 timeout_ms=_CAST_TIMEOUT_MS, stop_on=("hurt", "death"))
            yield rec("use_item", result, item=_ROD, pos=pos)
            after = state.refresh_status(ex.client, max_age_s=0)
            _fresh_inventory(ex, state)
            if not _on_ground(after) or after.get("dimension") != fresh.get("dimension"):
                raise Replan("收竿后未确认安全岸位，请先恢复安全")
            caught = _caught_fish(result)
            if caught is None:
                raise Replan("此竿未确认钓到可计分的鱼，请换钓点或择机再试")
            completed = _has_progress(ex, _FISH_PROGRESS)
            added = state.count(caught) - before[caught]
            if added <= 0 and not completed:
                raise Replan("钓竿回执中的鱼未获入包差量或服务器进度证实")
        except (GeneratorExit, Replan):
            _stop_active_fishing(ex, state)
            raise
        yield rec("verify_fish", {"caught": caught, "gained": added, "advancement_confirmed": completed})
