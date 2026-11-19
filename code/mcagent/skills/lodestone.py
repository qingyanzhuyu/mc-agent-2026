"""给现场磁石绑定普通指南针，分别复核绑定与进度。"""
from __future__ import annotations

from ..runtime import EnvBlocked, Replan
from .base import Skill, failure, rec
from .enchant import _approach_block, _craft_plan, _inventory_rows, _nearby_blocks, _place_checked, _placement_sites, _site_state
from .encounter import _completed, _verify_advancement
from .field import equip_item
from .interaction import use_item_on
from .items import bare
from .registry import register
from .windows import is_storage_window, take_container_item, window_amount, window_view


_ADVANCEMENT = "minecraft:adventure/use_lodestone"
_CONTAINERS = ("chest", "trapped_chest", "barrel")
_COMPASS = "compass"
_STONE = "lodestone"
_CRAFTABLE = {_COMPASS, _STONE, "chiseled_stone_bricks", "stone_brick_slab", "stone_bricks"}


def _matching_binding(row, pos=None, dimension=None):
    """§3.2 没有约定磁石组件字段。缺少可复核目标时保持未确认。"""
    name = bare(row.get("name"))
    if name == "lodestone_compass":
        # 某些 DTO 直接给出不同的物品名。用净增证明本次变体产生，不推测其中的目标。
        return True
    if name != _COMPASS:
        return False
    components = row.get("components") or {}
    tracker = row.get("lodestone_tracker")
    if tracker is None and isinstance(components, dict):
        tracker = components.get("minecraft:lodestone_tracker")
    target = tracker.get("target") if isinstance(tracker, dict) else None
    if not isinstance(target, dict) or not isinstance(target.get("dimension"), str):
        return False
    point = target.get("pos")
    if not isinstance(point, (list, tuple)) or len(point) != 3 or not all(type(n) is int for n in point):
        return False
    return pos is None or (list(point) == list(pos) and bare(target["dimension"]) == bare(dimension))


def _bound_count(inventory, pos=None, dimension=None):
    count, seen = 0, set()
    for row in _inventory_rows(inventory):
        if not _matching_binding(row, pos, dimension):
            continue
        slot = row.get("slot")
        if type(slot) is int:
            if slot in seen:
                continue
            seen.add(slot)
        count += row.get("count", 1)
    return count


def _find_stone(ex, state):
    """复用已静态验证可达的放置物；否则查有现场证据的近处容器。"""
    blocks = _nearby_blocks(ex, state, (_STONE,))
    if blocks:
        return (yield from _approach_block(ex, state, blocks[0]))
    if state.count(_STONE):
        return None
    for candidate in _nearby_blocks(ex, state, _CONTAINERS):
        pos = yield from _approach_block(ex, state, candidate)
        target = {"pos": pos}
        try:
            view = yield from window_view(ex, target)
        except Replan as exc:
            code, _ = failure(exc)
            if code in {"out_of_reach", "path_not_found", "cannot_interact", "not_loaded", "no_window", "stalled"}:
                yield rec("window_view", target=target, error_code=code)
                continue
            raise
        if (is_storage_window(view, candidate[1])
                and window_amount(view, "container", _STONE) >= 1):
            yield from take_container_item(ex, state, target, "container", _STONE, 1, view=view)
            break
    return None


def _attune(ex, state, pos):
    status = _site_state(ex, state)
    dimension = status["dimension"]
    yield from equip_item(ex, state, _COMPASS)
    yield from _approach_block(ex, state, (pos, _STONE, None))

    def bound(evidence):
        before, after = evidence["before"], evidence["after"]
        return (bare(after["block"].get("name")) == _STONE
                and after["status"].get("dimension") == dimension
                and _bound_count(after["inventory"], pos, dimension) > _bound_count(before["inventory"], pos, dimension))

    yield from use_item_on(ex, state, pos, _COMPASS, verify=bound)
    yield rec("lodestone_binding_evidence", None, variant_confirmed=True)
    confirmed = yield from _verify_advancement(ex, _ADVANCEMENT, 3)
    yield rec("lodestone_advancement_evidence", None, advancement_confirmed=confirmed)


@register
class AttuneLodestone(Skill):
    name = "attune_lodestone"
    description = "复用、取出或制备磁石，以手持普通指南针绑定并确认进度"
    goal = "adventure:use_lodestone"
    verified = "offline"

    @classmethod
    def blocked(cls, state):
        if _bound_count(state.inventory):
            return ""
        if _craft_plan(state, {_COMPASS: 1}, _CRAFTABLE).missing:
            return "缺普通指南针，或缺制作它的 4 铁锭与 1 红石粉"
        return ""

    @classmethod
    def material_plan(cls, args, state) -> tuple[tuple[str, int, str], ...]:
        if _bound_count(state.inventory) or cls.blocked(state):
            return ()
        fallback = _craft_plan(state, {_COMPASS: 1, _STONE: 1}, _CRAFTABLE)
        if not fallback.missing:
            return fallback.consumed
        return _craft_plan(state, {_COMPASS: 1}, _CRAFTABLE).consumed

    def run(self, ex, state):
        if _completed(ex, _ADVANCEMENT):
            return
        state.invalidate_inventory()
        inventory = state.refresh_inventory(ex.client)
        if _bound_count(inventory):
            yield rec("lodestone_binding_evidence", None, variant_confirmed=True, advancement_confirmed=False)
            return
        if reason := self.blocked(state):
            raise Replan(reason)
        pos = yield from _find_stone(ex, state)
        requirements = {_COMPASS: 1}
        if pos is None:
            requirements[_STONE] = 1
        prepare = _craft_plan(state, requirements, _CRAFTABLE)
        if pos is None and (prepare.missing or not _placement_sites(ex, state)):
            missing = "、".join(f"{name} ×{count}" for name, count in prepare.missing)
            raise EnvBlocked(f"8 格内没有可达磁石或容器且自制条件不足（{missing or '缺安全落点'}）",
                             missing_predicate="station_ready:lodestone",
                             searched_volume="半径 8 格 5 个磁石和 5 个容器候选及四邻落点", wake_on="chunk_moved")
        yield from prepare.run(ex, state)
        # 合成工作台可能改变站位，先重扫，再选新落点。
        if pos is not None and prepare.steps:
            reused = _nearby_blocks(ex, state, (_STONE,))
            if not reused:
                raise Replan("指南针已制备，需重新找到可达磁石")
            pos = yield from _approach_block(ex, state, reused[0])
        if pos is None:
            sites = _placement_sites(ex, state)
            if not sites:
                raise Replan("已有磁石，需找到承重并有退路的放置格")
            pos = yield from _place_checked(ex, state, _STONE, sites[0])
        yield from _attune(ex, state, pos)
