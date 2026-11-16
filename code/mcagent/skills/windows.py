"""Window observation, single-operation helpers and explicit batch reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from ..client import ApiError
from ..runtime import Executor, Replan, State
from .base import rec
from .items import bare

WindowVerifier = Callable[[dict, dict, State], bool]
PendingVerifier = Callable[[tuple[dict, ...], dict, State], bool]


def window_section(view: dict, section: str) -> list[dict]:
    return (view.get("sections") or {}).get(section) or []


def window_amount(view: dict, section: str, item: str, *, slot: int | None = None) -> int:
    return sum(int(entry.get("count", 0)) for entry in window_section(view, section)
               if bare(entry.get("name")) == bare(item)
               and (slot is None or type(entry.get("slot")) is int and entry["slot"] == slot))


def window_view(ex: Executor, target: dict) -> Generator[dict, None, dict]:
    view = ex.window_view(target)
    yield rec("window_view", view, target=target)
    if not isinstance(view.get("sections"), dict):
        raise Replan("窗口观察未提供 sections，无法确认槽位")
    return view


def _completed_prefix(extra: dict, ops: tuple[dict, ...]) -> int | None:
    """Only accept a consistent count and an identified first failed operation."""
    done, failed = extra.get("done"), extra.get("failed_op")
    if isinstance(done, list):
        if done != list(ops[:len(done)]):
            return None
        done = len(done)
    if type(done) is not int or not 0 <= done < len(ops):
        return None
    if type(failed) is int and failed == done or isinstance(failed, dict) and failed == ops[done]:
        return done
    return None


def apply_window_ops(
    ex: Executor, state: State, target: dict, ops: Sequence[dict], *,
    verify_pending: PendingVerifier | None = None,
) -> Generator[dict, None, dict]:
    """Apply a batch. Resume a known suffix only if its preconditions and effects can be verified."""
    result: dict = {}
    pending = tuple(ops)
    while pending:
        try:
            result = ex.window_act(target, list(pending))
        except Replan as err:
            cause = err.__cause__
            if not isinstance(cause, ApiError) or str(err) != str(cause):
                # A safety preemption or ambiguous transport error wins over reconciliation.
                raise
            if "done" not in cause.extra or "failed_op" not in cause.extra:
                raise
            completed = _completed_prefix(cause.extra, pending)
            yield rec("window_act", target=target, error_code=cause.code, error=cause.extra)
            view = yield from window_view(ex, target)
            state.refresh_inventory(ex.client)
            if completed is None or completed == 0 or verify_pending is None:
                raise
            pending = pending[completed:]
            if not verify_pending(pending, view, state):
                raise
        else:
            yield rec("window_act", result, target=target)
            return result
    return result


def _positive_count(count: int) -> None:
    if type(count) is not int or count < 1:
        raise Replan("窗口物品数量必须是正整数")


def put_window_item(
    ex: Executor, state: State, target: dict, item: str, count: int, to: str, *,
    slot: int | None = None, verify: WindowVerifier | None = None,
) -> Generator[dict, None, dict]:
    _positive_count(count)
    item = bare(item)
    before = yield from window_view(ex, target)
    op = {"op": "put", "item": item, "count": count, "to": to}
    if slot is not None:
        op["slot"] = slot
    receipt = yield from _put(ex, state, target, op)
    view = yield from window_view(ex, target)
    if verify is not None:
        confirmed = verify(before, view, state)
    else:
        confirmed = window_amount(view, to, item, slot=slot) >= window_amount(before, to, item, slot=slot) + count
    if not confirmed:
        raise Replan(f"投放 {item} 后窗口 {to} 未确认，请核对槽位")
    return {"receipt": receipt, "view": view}


def select_window_option(
    ex: Executor, state: State, target: dict, option: int, *, times: int = 1,
    verify: WindowVerifier,
) -> Generator[dict, None, dict]:
    _positive_count(times)
    before = yield from window_view(ex, target)
    receipt = yield from apply_window_ops(ex, state, target, [
        {"op": "select", "option": option, "times": times},
    ])
    view = yield from window_view(ex, target)
    state.refresh_inventory(ex.client)
    if not verify(before, view, state):
        raise Replan(f"窗口 select {option} 后未确认预期结果")
    return {"receipt": receipt, "view": view}


def _put(ex: Executor, state: State, target: dict, op: dict) -> Generator[dict, None, dict]:
    item, count = op["item"], op["count"]
    state.refresh_inventory(ex.client)
    before = state.count(item)
    if before < count:
        raise Replan(f"窗口投放需 {count} 个 {item}，背包只有 {before} 个")
    receipt = yield from apply_window_ops(ex, state, target, [op])
    state.refresh_inventory(ex.client)
    if state.count(item) > before - count:
        raise Replan(f"窗口投放后背包未确认扣除 {count} 个 {item}")
    return receipt


def _take(
    ex: Executor, state: State, target: dict, section: str, item: str, count: int, *,
    failure_message: str | None = None,
) -> Generator[dict, None, dict]:
    state.refresh_inventory(ex.client)
    before = state.count(item)
    receipt = yield from apply_window_ops(ex, state, target, [
        {"op": "take", "from": section, "item": item, "count": count},
    ])
    state.refresh_inventory(ex.client)
    if state.count(item) < before + count:
        raise Replan(failure_message or f"窗口取出后背包未确认收到 {count} 个 {item}")
    return receipt


def take_container_item(
    ex: Executor, state: State, target: dict, section: str, item: str, count: int, *,
    view: dict | None = None,
) -> Generator[dict, None, dict]:
    _positive_count(count)
    item = bare(item)
    if view is None:
        view = yield from window_view(ex, target)
    if window_amount(view, section, item) < count:
        raise Replan(f"窗口 {section} 未观察到足够的 {item}，需 {count} 个")
    receipt = yield from _take(ex, state, target, section, item, count)
    after = yield from window_view(ex, target)
    return {"receipt": receipt, "view": after, "count": count}
