"""L2 window primitives for villager trading, smithing table, and brewing stand."""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Generator
from typing import Optional

from ..runtime import Executor, Replan, State
from .field import _cell
from .items import bare
from .windows import (
    put_window_item, select_window_option, take_container_item,
    window_amount, window_section, window_view,
)

_MERCHANT_WINDOWS = frozenset({"merchant", "villager", "wandering_trader"})
_SMITHING_WINDOWS = frozenset({"smithing", "smithing_table"})
_BREWING_WINDOWS = frozenset({"brewing_stand", "brewing"})


def _require_sections(view: dict, allowed: frozenset[str], required: tuple[str, ...], label: str) -> None:
    sections = view.get("sections") or {}
    if (bare(view.get("window")) not in allowed
            or not all(isinstance(sections.get(name), list) for name in required)):
        raise Replan(f"当前窗口不是有效的{label}界面")


def _require_inventory(ex: Executor, state: State, items: list[str], label: str) -> Counter[str]:
    state.refresh_inventory(ex.client)
    needed = Counter(items)
    missing = [f"{item}x{qty}(现有{state.count(item)})" for item, qty in needed.items() if state.count(item) < qty]
    if missing:
        raise Replan(f"{label}材料不足: {', '.join(missing)}")
    return needed


def _parse_payments(opt: dict) -> Counter[str] | None:
    if "give" in opt and opt["give"] is not None:
        raw = opt["give"]
        entries = [raw] if isinstance(raw, dict) else (list(raw) if isinstance(raw, (list, tuple)) else None)
        if entries is None:
            return None
    else:
        keys = ("input1", "input2") if ("input1" in opt or "input2" in opt) else ("cost", "cost2")
        entries = [opt.get(k) for k in keys if opt.get(k) is not None]
    needed: Counter[str] = Counter()
    for entry in entries:
        if entry is None:
            continue
        if not isinstance(entry, dict):
            return None
        name, cnt = bare(entry.get("name")), entry.get("count", 1)
        if not name or type(cnt) is not int or cnt < 1:
            return None
        needed[name] += cnt
    return needed or None


def _parse_reward(opt: dict) -> tuple[str, int] | None:
    raw = opt.get("get")
    if raw is None:
        raw = opt.get("output") if opt.get("output") is not None else opt.get("result")
    if isinstance(raw, str):
        name, cnt = bare(raw), 1
    elif isinstance(raw, dict):
        name, cnt = bare(raw.get("name")), raw.get("count", 1)
    else:
        return None
    return (name, cnt) if name and type(cnt) is int and cnt >= 1 else None


def trade_once(
    ex: Executor, state: State, entity_id: int, *, preferred_items: tuple[str, ...] = (),
) -> Generator[dict, None, dict]:
    """Inspect a merchant window, pick the best affordable option, and execute one trade."""
    if type(entity_id) is not int or entity_id < 0:
        raise Replan("村民 entity_id 必须是非负整数")
    target = {"entity_id": entity_id}
    view = yield from window_view(ex, target)
    options = view.get("options")
    if options is None:
        info = view.get("info")
        options = info.get("options") if isinstance(info, dict) else None
    if bare(view.get("window")) not in _MERCHANT_WINDOWS or not isinstance(options, list):
        raise Replan("当前窗口不是有效的村民交易界面或缺少交易选项列表")

    state.refresh_inventory(ex.client)
    preferred = tuple(bare(x) for x in preferred_items if bare(x))
    best_rank: tuple[int, int, int, int] | None = None
    chosen: tuple[int, Counter[str], str, int] | None = None

    for loop_idx, opt in enumerate(options):
        if not isinstance(opt, dict):
            continue
        if opt.get("available") is False or opt.get("enabled") is False or opt.get("disabled") is True:
            continue
        uses, max_uses = opt.get("uses"), opt.get("max_uses")
        if type(uses) is int and type(max_uses) is int and uses >= max_uses:
            continue
        index = opt["index"] if type(opt.get("index")) is int else loop_idx
        needed, reward = _parse_payments(opt), _parse_reward(opt)
        if needed is None or reward is None:
            continue
        get_name, get_count = reward
        if not all(state.count(item) >= qty for item, qty in needed.items()):
            continue
        rank = (
            (0, preferred.index(get_name), sum(needed.values()), index)
            if get_name in preferred else (1, 0, sum(needed.values()), index)
        )
        if best_rank is None or rank < best_rank:
            best_rank, chosen = rank, (index, needed, get_name, get_count)

    if chosen is None:
        raise Replan("村民没有当前背包可支付且可用的交易选项")

    chosen_index, needed, get_name, get_count = chosen
    before_get = state.count(get_name)
    before_give = {k: state.count(k) for k in needed}

    def verify_trade(_before: dict, _after: dict, st: State) -> bool:
        for k, qty in needed.items():
            if k != get_name and before_give[k] - st.count(k) < qty:
                return False
        return st.count(get_name) - before_get >= get_count - needed.get(get_name, 0)

    res = yield from select_window_option(ex, state, target, chosen_index, times=1, verify=verify_trade)
    return {
        "entity_id": entity_id,
        "option": chosen_index,
        "gave": dict(needed),
        "got": {get_name: max(get_count, state.count(get_name) - before_get)},
        "receipt": res["receipt"],
        "view": res["view"],
    }


def smith_once(
    ex: Executor, state: State, table_pos, *, template: str, base: str, addition: str,
    expected_output: Optional[str] = None,
) -> Generator[dict, None, dict]:
    """Perform one smithing table upgrade/trim and verify inventory deltas."""
    pos = _cell(table_pos)
    target = {"pos": list(pos)}
    template, base, addition = bare(template), bare(base), bare(addition)
    expected_out = bare(expected_output) if expected_output else None
    if not template or not base or not addition:
        raise Replan("锻造台需要提供有效的 template、base 与 addition 物品名")

    initial = yield from window_view(ex, target)
    _require_sections(initial, _SMITHING_WINDOWS, ("template", "base", "addition", "output"), "锻造台")
    if any(window_section(initial, slot) for slot in ("template", "base", "addition")):
        raise Replan("锻造台输入槽已有物品，请先清理后再锻造")

    needed = _require_inventory(ex, state, [template, base, addition], "锻造")
    before_counts = {item: state.count(item) for item in needed}
    if expected_out:
        before_counts[expected_out] = state.count(expected_out)
    before_template, before_base, before_addition = before_counts[template], before_counts[base], before_counts[addition]

    yield from put_window_item(ex, state, target, template, 1, "template")
    yield from put_window_item(ex, state, target, base, 1, "base")
    yield from put_window_item(ex, state, target, addition, 1, "addition")

    view = yield from window_view(ex, target)
    output_entries = window_section(view, "output")
    if not output_entries:
        raise Replan("锻造台放入材料后 output 槽为空，配方无效")
    out_entry = output_entries[0]
    out_item = bare(out_entry.get("name"))
    out_count = int(out_entry.get("count", 1))
    if not out_item or out_count < 1:
        raise Replan("锻造台 output 槽产物无效")
    if expected_out and out_item != expected_out:
        raise Replan(f"锻造台产物为 {out_item}，与预期 {expected_out} 不符")

    before_out = before_counts[out_item] if out_item in before_counts else state.count(out_item)
    take_res = yield from take_container_item(ex, state, target, "output", out_item, out_count, view=view)
    state.refresh_inventory(ex.client)

    consumed_ok = before_template - state.count(template) >= 1 and before_addition - state.count(addition) >= 1
    if out_item == base:
        output_ok = state.count(out_item) >= before_base and take_res["count"] >= out_count
    else:
        output_ok = state.count(out_item) >= before_out + out_count and before_base - state.count(base) >= 1
    if not (consumed_ok and output_ok):
        raise Replan(f"锻造台取出 {out_item} 后背包数量校验失败")

    return {
        "pos": list(pos), "template": template, "base": base, "addition": addition,
        "output": out_item, "count": take_res["count"],
        "receipt": take_res["receipt"], "view": take_res["view"],
    }


def _num_info(view: dict, *keys: str) -> float:
    info = view.get("info")
    if not isinstance(info, dict):
        return 0.0
    for key in keys:
        if key in info and type(info[key]) in (int, float) and math.isfinite(info[key]):
            return float(info[key])
    return 0.0


def _fuel_left(view: dict) -> float:
    return _num_info(view, "fuel_left")


def _brew_time(view: dict) -> float:
    return _num_info(view, "brew_time_left", "brew_time")


def _entry_sig(entry: dict) -> tuple:
    return (entry.get("slot"), bare(entry.get("name")), entry.get("potion"), entry.get("display_name"), entry.get("count", 1))


def _bottle_sig(view: dict) -> tuple[tuple, ...]:
    return tuple(_entry_sig(entry) for entry in window_section(view, "bottles"))


def brew_batch(
    ex: Executor, state: State, stand_pos, *, bottle_item: str, ingredient: str,
    fuel_item: str = "blaze_powder", max_polls: int = 12, poll_sleep_s: float = 2.0,
    sleep_fn=time.sleep,
) -> Generator[dict, None, dict]:
    """Load fuel/bottle/ingredient into a brewing stand, poll until brewed, and take potion."""
    pos = _cell(stand_pos)
    target = {"pos": list(pos)}
    if type(max_polls) is not int or max_polls < 1:
        raise Replan("酿造台 max_polls 必须是正整数")
    if type(poll_sleep_s) not in (int, float) or not math.isfinite(poll_sleep_s) or poll_sleep_s < 0:
        raise Replan("酿造台 poll_sleep_s 必须是非负有限数")
    bottle_item, ingredient, fuel_item = bare(bottle_item), bare(ingredient), bare(fuel_item)
    if not bottle_item or not ingredient:
        raise Replan("酿造台需要提供有效的 bottle_item 与 ingredient")

    initial = yield from window_view(ex, target)
    _require_sections(initial, _BREWING_WINDOWS, ("bottles", "ingredient", "fuel"), "酿造台")
    if window_section(initial, "ingredient") or len(window_section(initial, "bottles")) >= 3:
        raise Replan("酿造台已有原料或瓶位已满，请先清理后再酿造")

    need_fuel = _fuel_left(initial) <= 0 and window_amount(initial, "fuel", fuel_item) == 0
    if need_fuel and not fuel_item:
        raise Replan("酿造台缺少燃料且未指定 fuel_item")
    _require_inventory(ex, state, [bottle_item, ingredient] + ([fuel_item] if need_fuel else []), "酿造")

    before_bottle, before_ingredient = state.count(bottle_item), state.count(ingredient)
    if need_fuel:
        yield from put_window_item(
            ex, state, target, fuel_item, 1, "fuel",
            verify=lambda b, a, _s: (
                window_amount(a, "fuel", fuel_item) > window_amount(b, "fuel", fuel_item)
                or _fuel_left(a) > _fuel_left(b)
            ),
        )

    bottle_res = yield from put_window_item(ex, state, target, bottle_item, 1, "bottles")
    before_sig = _bottle_sig(bottle_res["view"])
    before_entries = set(before_sig)

    ing_res = yield from put_window_item(
        ex, state, target, ingredient, 1, "ingredient",
        verify=lambda b, a, _s: (
            window_amount(a, "ingredient", ingredient) > window_amount(b, "ingredient", ingredient)
            or _brew_time(a) > 0 or _bottle_sig(a) != before_sig
        ),
    )
    view = ing_res["view"]

    def _brew_done(cur: dict) -> bool:
        return (
            _brew_time(cur) <= 0 and bool(window_section(cur, "bottles"))
            and (window_amount(cur, "ingredient", ingredient) == 0 or _bottle_sig(cur) != before_sig)
        )

    for _ in range(max_polls):
        if _brew_done(view):
            break
        if poll_sleep_s > 0:
            sleep_fn(poll_sleep_s)
        view = yield from window_view(ex, target)
    if not _brew_done(view):
        raise Replan(f"酿造台在 {max_polls} 次轮询内未完成酿造")

    bottles = window_section(view, "bottles")
    changed = [entry for entry in bottles if _entry_sig(entry) not in before_entries]
    entry = changed[0] if changed else bottles[0]
    out_item, out_potion = bare(entry.get("name")), entry.get("potion")
    if not out_item:
        raise Replan("酿造台产物槽物品无效")

    before_out_total = state.count(out_item)
    take_res = yield from take_container_item(ex, state, target, "bottles", out_item, 1, view=view)
    state.refresh_inventory(ex.client)

    ing_ok = before_ingredient - state.count(ingredient) >= 1
    out_ok = (
        state.count(out_item) >= before_bottle if out_item == bottle_item
        else state.count(out_item) >= before_out_total + 1
    )
    if not (ing_ok and out_ok):
        raise Replan(f"酿造台取出 {out_item} 后背包数量校验失败")

    return {
        "pos": list(pos), "bottle_item": bottle_item, "ingredient": ingredient,
        "output": out_item, "potion": out_potion, "count": take_res["count"],
        "receipt": take_res["receipt"], "view": take_res["view"],
    }
