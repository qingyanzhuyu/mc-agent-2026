"""给现场磁石绑定普通指南针，分别复核绑定与进度。"""
from __future__ import annotations

from collections import Counter

from ..runtime import EnvBlocked, Replan
from .base import Skill, failure, rec
from .enchant import _approach_block, _craft_plan, _inventory_rows, _nearby_blocks, _place_checked, _placement_sites, _site_state
from .encounter import _completed, _verify_advancement
from .field import equip_item
from .interaction import use_item_on
from .items import bare
from .registry import register
from .windows import (
    apply_window_ops, is_storage_window, take_container_item, window_amount, window_section, window_view,
)
from .world_memo import memo_for


_ADVANCEMENT = "minecraft:adventure/use_lodestone"
_CONTAINERS = ("chest", "trapped_chest", "barrel")
_COMPASS = "compass"
_STONE = "lodestone"
_STONECUTTER = "stonecutter"
_CHISELED = "chiseled_stone_bricks"
_CHISELED_NEEDED = 8
_STONECUTTER_INPUTS = ("stone_bricks", "stone")
_CRAFTABLE = {_COMPASS, _STONE, _CHISELED, "stone_brick_slab", "stone_bricks"}


def _remaining_iron_for_lodestone(state) -> int:
    if state.count(_COMPASS) >= 1:
        return state.count("iron_ingot")
    return max(0, state.count("iron_ingot") - 4)


def _can_use_stonecutter_materials(state, needed: int = _CHISELED_NEEDED) -> bool:
    if state.count(_STONE) != 0:
        return False
    deficit = max(0, needed - state.count(_CHISELED))
    if deficit <= 0:
        return False
    if state.count("stone") + state.count("stone_bricks") < deficit:
        return False
    return _remaining_iron_for_lodestone(state) >= 1


def _stonecutter_option_index(view: dict, target_item: str = _CHISELED) -> int | None:
    if not isinstance(view, dict):
        return None
    options = view.get("options")
    if not isinstance(options, list):
        info = view.get("info")
        options = info.get("options") if isinstance(info, dict) else None
    if not isinstance(options, list):
        return None
    target_bare = bare(target_item)
    for row in options:
        if not isinstance(row, dict):
            continue
        if row.get("available") is False or row.get("enabled") is False:
            continue
        idx = row.get("index")
        if type(idx) is not int or idx < 0:
            continue
        for key in ("output", "result", "item", "name"):
            val = row.get(key)
            cand = val.get("name") if isinstance(val, dict) else val
            if isinstance(cand, str) and bare(cand) == target_bare:
                return idx
    return None


def _record_stonecutter_station(state, pos, dimension) -> None:
    if pos is None:
        return
    try:
        memo_for(state).record_station(_STONECUTTER, pos, dimension=dimension or "overworld")
    except Exception:
        pass


def _stonecutter_ops(mat: str, qty: int, option: int, input_sec: str, output_sec: str) -> list[dict]:
    return [
        {"op": "put", "item": mat, "count": qty, "to": input_sec},
        {"op": "select", "option": option, "times": qty},
        {"op": "take", "from": output_sec, "item": _CHISELED, "count": qty},
    ]


def cut_chiseled_bricks_at_stonecutter(ex, state, needed: int = _CHISELED_NEEDED):
    """使用附近或背包中的切石机，将石砖/石头 1:1 切成雕纹石砖以节省石材。"""
    deficit = max(0, needed - state.count(_CHISELED))
    if deficit <= 0:
        return True
    if state.count("stone") + state.count("stone_bricks") < deficit:
        return False

    candidates = _nearby_blocks(ex, state, (_STONECUTTER,))
    if candidates:
        sc_pos = yield from _approach_block(ex, state, candidates[0])
    elif state.count(_STONECUTTER) >= 1:
        sites = _placement_sites(ex, state)
        if not sites:
            return False
        sc_pos = yield from _place_checked(ex, state, _STONECUTTER, sites[0])
    else:
        return False

    status = _site_state(ex, state)
    _record_stonecutter_station(state, sc_pos, status.get("dimension"))

    target = {"pos": sc_pos}
    view = yield from window_view(ex, target)
    if bare(view.get("window")) not in ("stonecutter", "stone_cutter"):
        raise Replan(f"期望打开切石机窗口，实际得到 {view.get('window')!r}")
    sections = view.get("sections") or {}
    input_sec = "input" if "input" in sections else "ingredient"
    output_sec = "output" if "output" in sections else "result"
    if not isinstance(sections.get(input_sec), list) or not isinstance(sections.get(output_sec), list):
        raise Replan("切石机窗口缺少预期的输入/输出分区")
    if window_section(view, input_sec):
        raise Replan("切石机输入槽已有物品，请先核对并清空")

    for mat in _STONECUTTER_INPUTS:
        state.refresh_inventory(ex.client)
        still_need = max(0, needed - state.count(_CHISELED))
        take_qty = min(still_need, state.count(mat))
        if take_qty <= 0:
            continue
        opt_idx = _stonecutter_option_index(view, _CHISELED)
        option = opt_idx if opt_idx is not None else 0
        before = state.count(_CHISELED)
        ops = _stonecutter_ops(mat, take_qty, option, input_sec, output_sec)
        try:
            receipt = yield from apply_window_ops(ex, state, target, ops)
        except Replan as exc:
            code, extra = failure(exc)
            err_window = extra.get("window")
            if code == "option_unavailable" and opt_idx is None and isinstance(err_window, dict):
                retry_idx = _stonecutter_option_index(err_window, _CHISELED)
                state.refresh_inventory(ex.client)
                if retry_idx is not None and retry_idx != option and state.count(mat) >= take_qty:
                    if "done" not in extra or "failed_op" not in extra:
                        yield rec("window_act", target=target, error_code=code, error=extra)
                    retry_ops = _stonecutter_ops(mat, take_qty, retry_idx, input_sec, output_sec)
                    receipt = yield from apply_window_ops(ex, state, target, retry_ops)
                else:
                    raise
            else:
                raise

        state.refresh_inventory(ex.client)
        if state.count(_CHISELED) < before + take_qty:
            end_window = receipt.get("window") if isinstance(receipt, dict) else None
            end_view = (
                end_window
                if isinstance(end_window, dict) and isinstance(end_window.get("sections"), dict)
                else None
            )
            if end_view is not None:
                missing_qty = before + take_qty - state.count(_CHISELED)
                avail_out = window_amount(end_view, output_sec, _CHISELED)
                if avail_out > 0:
                    yield from take_container_item(
                        ex, state, target, output_sec, _CHISELED, min(missing_qty, avail_out), view=end_view
                    )
                    state.refresh_inventory(ex.client)
        if state.count(_CHISELED) < before + take_qty:
            raise Replan(f"切石机切割 {mat} 后背包未确认获得 {take_qty} 个 {_CHISELED}")
        if isinstance(receipt, dict) and isinstance(receipt.get("window"), dict):
            view = receipt["window"]

    return state.count(_CHISELED) >= needed


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
    verified = "live"

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
        if state.count(_STONECUTTER) >= 1 and _can_use_stonecutter_materials(state):
            compass_consumed = _craft_plan(state, {_COMPASS: 1}, _CRAFTABLE).consumed
            counts: Counter[str] = Counter()
            modes: dict[str, str] = {}
            for item, count, mode in compass_consumed:
                counts[item] += count
                modes[item] = mode
            counts["iron_ingot"] += 1
            modes["iron_ingot"] = "consume"
            existing_chiseled = state.count(_CHISELED)
            if existing_chiseled > 0:
                counts[_CHISELED] += existing_chiseled
                modes[_CHISELED] = "consume"
            deficit = max(0, _CHISELED_NEEDED - existing_chiseled)
            for mat in _STONECUTTER_INPUTS:
                take = min(deficit, state.count(mat))
                if take > 0:
                    counts[mat] += take
                    modes[mat] = "consume"
                    deficit -= take
            plan = [(item, count, modes[item]) for item, count in counts.items() if count > 0]
            plan.append((_STONECUTTER, 1, "borrow"))
            return tuple(plan)
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
        if pos is None and _can_use_stonecutter_materials(state):
            yield from cut_chiseled_bricks_at_stonecutter(ex, state, needed=_CHISELED_NEEDED)
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
