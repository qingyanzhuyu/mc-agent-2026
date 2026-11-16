"""Furnace load, inspect and collection as separate bounded steps."""

from __future__ import annotations

from collections.abc import Generator

from ..runtime import Executor, Replan, State
from .base import failure
from .items import bare
from .windows import (
    _positive_count, _put, _receipt_inventory_full, _take, apply_window_ops, window_amount, window_section, window_view,
)


def inspect_furnace(ex: Executor, target: dict) -> Generator[dict, None, dict]:
    """Use a reachable, observed furnace target and return its current window."""
    view = yield from window_view(ex, target)
    if bare(view.get("window")) != "furnace":
        raise Replan("目标窗口未确认是普通熔炉")
    return view


def load_furnace(
    ex: Executor, state: State, target: dict, *, raw_item: str | None = None,
    raw_count: int = 0, fuel_item: str, fuel_count: int,
    view: dict | None = None, replace_fuel: bool = False, output_item: str | None = None,
) -> Generator[dict, None, list[dict]]:
    """Load a new batch or refuel an observed batch. Inventory is checked per operation."""
    _positive_count(fuel_count)
    if raw_item is not None:
        _positive_count(raw_count)
    elif raw_count != 0:
        raise Replan("炉批原料数量缺少对应的原料物品")
    if view is None:
        view = yield from inspect_furnace(ex, target)
    pos = target.get("pos")
    if raw_item is not None and window_section(view, "input"):
        raise Replan(f"观察熔炉 {pos} input 仍有原料，先处理已有冶炼任务后重规划")
    if output_item is not None and any(
            bare(slot.get("name")) != bare(output_item) for slot in window_section(view, "output")):
        raise Replan(f"观察熔炉 {pos} output 被非铁产物占用，停止放料" if output_item == "iron_ingot"
                     else f"观察熔炉 {pos} output 被其他产物占用，停止放料")
    fuel_slot = window_section(view, "fuel")
    if fuel_slot and not replace_fuel:
        raise Replan(f"观察熔炉 {pos} fuel 槽仍被占用，请先确认燃料")
    inputs = [("fuel", bare(fuel_item), fuel_count)]
    if raw_item is not None:
        inputs.insert(0, ("input", bare(raw_item), raw_count))
    state.refresh_inventory(ex.client)
    required: dict[str, int] = {}
    for _, item, count in inputs:
        required[item] = required.get(item, 0) + count
    for item, count in required.items():
        if state.count(item) < count:
            raise Replan(f"熔炉投放需 {count} 个 {item}，请补足材料")
    receipts = []
    if fuel_slot:
        witness = fuel_slot[0]
        old_item, old_count = bare(witness.get("name")), witness.get("count")
        if len(fuel_slot) != 1 or not old_item or type(old_count) is not int or old_count < 1:
            raise Replan(f"熔炉 {pos} fuel 槽观测不明确，停止换料")
        previous = state.count(old_item)
        try:
            take_receipt = yield from apply_window_ops(ex, state, target, [
                {"op": "take", "from": "fuel"},
            ])
        except Replan as exc:
            if failure(exc)[0] == "inventory_full":
                state.refresh_inventory(ex.client)
                raise Replan(f"背包已满（inventory_full），熔炉 {pos} 换燃料无法取出 {old_item}；物品留在燃料槽") from exc
            raise
        receipts.append(take_receipt)
        state.refresh_inventory(ex.client)
        if state.count(old_item) < previous + old_count:
            if _receipt_inventory_full(take_receipt):
                raise Replan(f"背包已满（inventory_full），熔炉 {pos} 换燃料仅部分取出 {old_item}，燃料槽仍有余量")
            raise Replan(f"熔炉 {pos} 换燃料时未确认收到 {old_count} 个 {old_item}")
    for to, item, count in inputs:
        receipts.append((yield from _put(ex, state, target, {
            "op": "put", "item": item, "count": count, "to": to,
        })))
    return receipts


def collect_furnace(
    ex: Executor, state: State, target: dict, *, item: str, count: int | None = None,
    section: str = "output", view: dict | None = None, inspect_after: bool = True,
    require_empty: bool = False,
) -> Generator[dict, None, dict]:
    """Collect witnessed output or a returned fuel-slot item and verify its bag delta."""
    if section not in ("output", "fuel"):
        raise Replan("熔炉领取仅支持 output 或 fuel 槽位")
    if view is None:
        view = yield from inspect_furnace(ex, target)
    available = window_amount(view, section, item)
    amount = available if count is None else count
    _positive_count(amount)
    if available < amount:
        raise Replan(f"熔炉 {section} 槽未观察到足够的 {item}")
    pos = target.get("pos")
    message = (f"熔炉 {pos} take output 后背包铁锭未确认增加 {amount} 个"
               if item == "iron_ingot" and section == "output" else None)
    # §4.9：output 成品区只能整组取，放不下报 inventory_full；fuel 在不要求清空时允许部分取出。
    allow_partial = section == "fuel" and not require_empty
    receipt, gained, inv_full = yield from _take(
        ex, state, target, section, bare(item), amount,
        failure_message=message, allow_partial_on_full=allow_partial,
    )
    after = None
    if inspect_after or require_empty:
        after = yield from inspect_furnace(ex, target)
        if require_empty and window_section(after, section):
            raise Replan(f"熔炉 {pos} 取出后 {section} 仍有剩余产物：{after}")
    return {
        "receipt": receipt,
        "view": after,
        "count": min(gained, amount) if inv_full else amount,
        "inventory_full": inv_full,
    }
