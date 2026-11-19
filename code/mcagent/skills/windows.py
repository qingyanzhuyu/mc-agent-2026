"""Window observation, single-operation helpers and explicit batch reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from ..client import ApiError
from ..runtime import Executor, Replan, State
from .base import failure, rec
from .items import bare

WindowVerifier = Callable[[dict, dict, State], bool]
PendingVerifier = Callable[[tuple[dict, ...], dict, State], bool]

_STORAGE_WINDOWS = frozenset({
    "generic_9x3", "generic_9x6", "generic_3x3", "hopper", "shulker_box",
})


def is_storage_window(view: dict, expected_block: str | None = None) -> bool:
    """§4.9：箱子/木桶/陷阱箱等容器 window 为 generic_9x3 等，target 为方块名，分区为 container。"""
    sections = view.get("sections") or {}
    if not isinstance(sections.get("container"), list):
        return False
    win = bare(view.get("window"))
    expected = bare(expected_block) if expected_block else ""
    if not win or (win not in _STORAGE_WINDOWS and (not expected or win != expected)):
        return False
    target = view.get("target")
    if expected and isinstance(target, str) and bare(target) != expected:
        return False
    return True


def _receipt_inventory_full(receipt: dict) -> bool:
    if receipt.get("inventory_full") is True:
        return True
    done = receipt.get("done")
    return isinstance(done, list) and any(
        isinstance(step, dict) and step.get("inventory_full") is True for step in done
    )


def _step_matches_op(step: object, req_op: dict) -> bool:
    if step == req_op:
        return True
    if not isinstance(step, dict) or step.get("op") != req_op.get("op"):
        return False
    kind = req_op.get("op")
    if kind == "put":
        placed = bare(step.get("put") or step.get("item"))
        return (placed == bare(req_op.get("item"))
                and step.get("to") == req_op.get("to")
                and (req_op.get("count") is None or step.get("count") == req_op.get("count")))
    if kind == "take":
        if step.get("from") != req_op.get("from"):
            return False
        wanted = req_op.get("item")
        if wanted is None:
            return True
        took = step.get("took")
        if isinstance(took, dict):
            return bare(wanted) in {bare(k) for k in took}
        return bare(step.get("item")) == bare(wanted)
    if kind == "select":
        return step.get("option") == req_op.get("option")
    if kind == "text":
        return step.get("value") == req_op.get("value")
    return False


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
        if len(done) > len(ops) or not all(
            _step_matches_op(step, req_op) for step, req_op in zip(done, ops)
        ):
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
    failure_message: str | None = None, allow_partial_on_full: bool = False,
) -> Generator[dict, None, tuple[dict, int, bool]]:
    state.refresh_inventory(ex.client)
    before = state.count(item)
    try:
        receipt = yield from apply_window_ops(ex, state, target, [
            {"op": "take", "from": section, "item": item, "count": count},
        ])
    except Replan as exc:
        code, _ = failure(exc)
        if code == "inventory_full":
            state.refresh_inventory(ex.client)
            raise Replan(
                f"背包已满（inventory_full），无法从窗口 {section} 取出 {item}；物品留在原槽位，请先腾出空位"
            ) from exc
        raise
    state.refresh_inventory(ex.client)
    gained = state.count(item) - before
    inv_full = _receipt_inventory_full(receipt)
    if gained >= count:
        return receipt, gained, inv_full
    if allow_partial_on_full and gained > 0 and inv_full:
        return receipt, gained, True
    raise Replan(failure_message or f"窗口取出后背包未确认收到 {count} 个 {item}")


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
    # §4.9：output 成品区只能整组取，放不下整组报 inventory_full；普通槽位可部分取出并带 inventory_full: true。
    receipt, gained, inv_full = yield from _take(
        ex, state, target, section, item, count, allow_partial_on_full=(section != "output"),
    )
    after = yield from window_view(ex, target)
    return {
        "receipt": receipt,
        "view": after,
        "count": min(gained, count) if inv_full else count,
        "inventory_full": inv_full,
    }
