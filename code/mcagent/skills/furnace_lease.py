"""Non-blocking furnace lease primitives, fuel selection, and per-state lease book."""

from __future__ import annotations

import math
import os
import threading
import time
import weakref
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from ..runtime import Executor, Replan, State
from .base import failure
from .items import bare
from .recipes import craft_recipe
from .windows import _positive_count, _put, _take, window_amount, window_section, window_view
from .world_memo import memo_for

SMELTING_STATIONS: tuple[str, ...] = ("furnace", "blast_furnace", "smoker")


def _lease_grace_s() -> float:
    """租约过期宽限秒数（`ready_at + grace` 之后仍未领取即作废）；0 表示关闭过期清理。"""
    raw = str(os.environ.get("MC_LEASE_GRACE_S", "") or "").strip()
    if not raw:
        return 300.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 300.0
    return val if math.isfinite(val) and val >= 0.0 else 300.0


STATION_SECONDS_PER_ITEM: dict[str, float] = {"furnace": 10.0, "blast_furnace": 5.0, "smoker": 5.0}
_WOODS = ("oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "cherry", "mangrove", "pale_oak")
LOG_TO_PLANK_FUEL: dict[str, str] = {
    **{f"{w}_log": f"{w}_planks" for w in _WOODS},
    **{f"stripped_{w}_log": f"{w}_planks" for w in _WOODS},
    **{f"{w}_wood": f"{w}_planks" for w in _WOODS},
    **{f"stripped_{w}_wood": f"{w}_planks" for w in _WOODS},
    "bamboo_block": "bamboo_planks",
    "stripped_bamboo_block": "bamboo_planks",
}
RAW_LOG_FUELS: frozenset[str] = frozenset(LOG_TO_PLANK_FUEL)
_COAL_FUELS: frozenset[str] = frozenset({"coal", "charcoal"})
EXTENDED_FUEL_BURNS: dict[str, float] = {
    "lava_bucket": 100.0, "coal_block": 80.0, "dried_kelp_block": 20.0, "blaze_rod": 12.0,
    "coal": 8.0, "charcoal": 8.0, "bamboo_planks": 1.5, "bamboo_mosaic": 1.5, "bamboo_block": 1.5,
    "leaf_litter": 0.5, "stick": 0.5,
    **{f"{w}_planks": 1.5 for w in _WOODS},
    **{f"{w}_log": 1.5 for w in _WOODS},
}
_SMELT_OUTPUTS: dict[str, str] = {
    "raw_iron": "iron_ingot", "iron_ore": "iron_ingot", "deepslate_iron_ore": "iron_ingot",
    "raw_gold": "gold_ingot", "gold_ore": "gold_ingot", "deepslate_gold_ore": "gold_ingot",
    "nether_gold_ore": "gold_ingot", "raw_copper": "copper_ingot", "copper_ore": "copper_ingot",
    "deepslate_copper_ore": "copper_ingot", "ancient_debris": "netherite_scrap",
    "cobblestone": "stone", "cobbled_deepslate": "deepslate", "stone": "smooth_stone",
    "sand": "glass", "red_sand": "glass", "netherrack": "nether_brick", "clay_ball": "brick",
    "clay": "terracotta", "beef": "cooked_beef", "porkchop": "cooked_porkchop",
    "chicken": "cooked_chicken", "mutton": "cooked_mutton", "rabbit": "cooked_rabbit",
    "cod": "cooked_cod", "salmon": "cooked_salmon", "potato": "baked_potato",
    "kelp": "dried_kelp", "chorus_fruit": "popped_chorus_fruit", "wet_sponge": "sponge",
    "cactus": "green_dye", "sea_pickle": "lime_dye",
}


def _norm_dim(dimension: Any) -> str:
    return bare(dimension).strip() or "overworld"


def _norm_pos(pos: Any) -> tuple[int, int, int]:
    if not isinstance(pos, Sequence) or isinstance(pos, (str, bytes)) or len(pos) != 3:
        raise Replan(f"非法熔炉坐标: {pos!r}")
    for axis in pos:
        if isinstance(axis, bool) or not isinstance(axis, (int, float)) or not math.isfinite(float(axis)):
            raise Replan(f"非法熔炉坐标轴: {pos!r}")
    return (int(pos[0]), int(pos[1]), int(pos[2]))


def smelt_seconds_per_item(station_kind: str) -> float:
    """Return nominal seconds per smelted item for a valid station kind."""
    kind = bare(station_kind).strip()
    if kind not in SMELTING_STATIONS:
        raise Replan(f"不支持的熔炼工作站类型: {station_kind!r}")
    return STATION_SECONDS_PER_ITEM[kind]


def smelt_output_for(input_item: str, expected_output: str | None = None) -> str:
    """Resolve expected smelted output item name from `input_item` or `expected_output`."""
    explicit = bare(expected_output).strip() if expected_output is not None else ""
    if explicit:
        return explicit
    raw = bare(input_item).strip()
    if raw in _SMELT_OUTPUTS:
        return _SMELT_OUTPUTS[raw]
    if raw.endswith(("_log", "_wood")):
        return "charcoal"
    raise Replan(f"未知熔炼输入物品的产物映射: {input_item!r}")


def best_fuel_for(
    state: State, items_needed: int, *, allow_lava_bucket: bool = True, allow_logs: bool = True,
) -> tuple[str, int] | None:
    """Pick the single owned fuel item for `items_needed` items.

    Priority tiers: coal/charcoal first -> other non-log fuels (planks, sticks, etc. minimizing waste)
    -> raw logs as last resort when `allow_logs=True`.
    """
    if type(items_needed) is not int or items_needed <= 0:
        return None
    best_key: tuple[int, float, int, int, str] | None = None
    best_choice: tuple[str, int] | None = None
    for name, burn in EXTENDED_FUEL_BURNS.items():
        if name == "lava_bucket" and not allow_lava_bucket:
            continue
        is_log = name in RAW_LOG_FUELS
        if is_log and not allow_logs:
            continue
        needed = math.ceil(round(items_needed / burn, 12))
        if state.count(name) < needed:
            continue
        tier = 0 if name in _COAL_FUELS else (2 if is_log else 1)
        key = (tier, round(needed * burn - items_needed, 9), 1 if name == "lava_bucket" else 0, needed, name)
        if best_key is None or key < best_key:
            best_key, best_choice = key, (name, needed)
    return best_choice


def ensure_plank_fuel(
    ex: Executor,
    state: State,
    items_needed: int,
    *,
    allow_lava_bucket: bool = True,
) -> Generator[dict, None, int]:
    """Convert owned burnable logs into planks before smelting when non-log fuel cannot cover `items_needed`.

    Returns the number of log/wood blocks crafted into planks. Stops cleanly on `inventory_full`
    so callers can still fall back if the bag has no free slot for a new plank stack.
    """
    if type(items_needed) is not int or items_needed <= 0:
        return 0
    converted = 0
    max_crafts = max(1, math.ceil(items_needed / 3.0))
    for _ in range(max_crafts):
        if best_fuel_for(state, items_needed, allow_lava_bucket=allow_lava_bucket, allow_logs=False) is not None:
            break
        log_item = next(
            (lg for lg, pl in LOG_TO_PLANK_FUEL.items() if state.count(lg) >= 1 and state.count(pl) >= 1),
            None,
        ) or next((lg for lg in LOG_TO_PLANK_FUEL if state.count(lg) >= 1), None)
        if log_item is None:
            break
        plank_item = LOG_TO_PLANK_FUEL[log_item]
        before = state.count(plank_item)
        try:
            yield from craft_recipe(ex, state, plank_item, goal_qty=before + 1)
        except Replan as exc:
            if failure(exc)[0] == "inventory_full":
                break
            raise
        if state.count(plank_item) <= before:
            break
        converted += 1
    return converted


@dataclass
class FurnaceLease:
    """Active lease representing an unattended smelting batch in a station."""

    pos: tuple[int, int, int]
    dimension: str
    station_kind: str
    input_item: str
    expected_output: str
    count: int
    started_at: float
    ready_at: float
    consumer: str
    returns_empty_bucket: bool = False
    #: 登记时刻（账本时钟）。过期判定只看"登记到现在过了多久"，不看 `ready_at` 绝对值 ——
    #: 否则那些刻意把 `ready_at` 置 0 表示"已就绪"的调用会被误判为过期。
    registered_at: float = 0.0


class FurnaceLeaseBook:
    """Thread-safe registry of active furnace leases keyed by `(dimension, cell)`."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._leases: dict[tuple[str, tuple[int, int, int]], FurnaceLease] = {}

    def register_lease(
        self, pos: Sequence[int | float], *, dimension: str = "overworld",
        station_kind: str = "furnace", input_item: str, expected_output: str | None = None,
        count: int, started_at: float | None = None, ready_at: float | None = None,
        consumer: str = "", returns_empty_bucket: bool = False, reservation_book: Any = None,
        state: State | None = None, fuel_item: str | None = None, fuel_count: int = 0,
    ) -> FurnaceLease:
        """Validate, register, and optionally transfer stock reservations into transit."""
        cell, dim = _norm_pos(pos), _norm_dim(dimension)
        kind = bare(station_kind).strip()
        sec_per_item = smelt_seconds_per_item(kind)
        _positive_count(count)
        raw_input = bare(input_item).strip()
        if not raw_input:
            raise Replan("熔炼输入物品不能为空")
        exp_out = smelt_output_for(raw_input, expected_output)
        started = float(self._clock()) if started_at is None else float(started_at)
        ready = started + count * sec_per_item if ready_at is None else float(ready_at)
        lease = FurnaceLease(
            pos=cell, dimension=dim, station_kind=kind, input_item=raw_input,
            expected_output=exp_out, count=count, started_at=started, ready_at=ready,
            consumer=str(consumer), returns_empty_bucket=bool(returns_empty_bucket),
            registered_at=float(self._clock()),
        )
        with self._lock:
            self._leases[(dim, cell)] = lease
        if reservation_book is not None and state is not None and consumer:
            reservation_book.move_to_container(state, consumer, raw_input, count)
            if fuel_item and type(fuel_count) is int and fuel_count >= 1:
                reservation_book.move_to_container(state, consumer, bare(fuel_item), fuel_count)
        return lease

    def _expire_locked(self, now: float) -> None:
        """惰性清理过期租约：登记后超过「本批冶炼时长 + 宽限期」仍未领取的租约自动作废。

        没有这一步时，死亡/断线/换维度/炉子被炸都会让租约永久留在账本里：
        `can_reclaim_station` 永远报"存在活跃熔炉租约暂不回收"（野外熔炉再也收不回），
        `ready_leases()` 又永远返回它，每次规划都领一条早已失效的租约并失败。
        宽限期默认 300s（可用 `MC_LEASE_GRACE_S` 覆盖，设 0 关闭）。
        """
        grace = _lease_grace_s()
        if grace <= 0:
            return
        for key, lease in list(self._leases.items()):
            registered = float(lease.registered_at or lease.started_at or 0.0)
            if registered <= 0.0:
                continue
            expected = max(0.0, float(lease.ready_at) - float(lease.started_at))
            if now - registered > expected + grace:
                self._leases.pop(key, None)

    def _list_leases(self, *, dimension: str | None = None, cutoff: float | None = None) -> list[FurnaceLease]:
        dim = _norm_dim(dimension) if dimension is not None else None
        with self._lock:
            self._expire_locked(float(self._clock()))
            out = [
                lease for (ldim, _), lease in self._leases.items()
                if (dim is None or ldim == dim) and (cutoff is None or lease.ready_at <= cutoff)
            ]
        out.sort(key=lambda item: (item.ready_at, item.pos))
        return out

    def ready_leases(self, *, now: float | None = None, dimension: str | None = None) -> list[FurnaceLease]:
        """Return leases whose `ready_at` has elapsed, sorted by `(ready_at, pos)`."""
        cutoff = float(self._clock()) if now is None else float(now)
        return self._list_leases(dimension=dimension, cutoff=cutoff)

    def active_leases(self, *, dimension: str | None = None) -> list[FurnaceLease]:
        """Return all active leases, sorted by `(ready_at, pos)`."""
        return self._list_leases(dimension=dimension)

    def get_lease(self, pos: Sequence[int | float], *, dimension: str = "overworld") -> Optional[FurnaceLease]:
        """Return the active lease at `(dimension, pos)` if present（过期租约会被惰性清除）."""
        with self._lock:
            self._expire_locked(float(self._clock()))
            return self._leases.get((_norm_dim(dimension), _norm_pos(pos)))

    def complete_lease(self, pos: Sequence[int | float], *, dimension: str = "overworld") -> Optional[FurnaceLease]:
        """Pop and return the active lease at `(dimension, pos)` if present."""
        with self._lock:
            return self._leases.pop((_norm_dim(dimension), _norm_pos(pos)), None)

    def clear(self) -> None:
        """Remove all active leases."""
        with self._lock:
            self._leases.clear()


_REGISTRY_LOCK = threading.Lock()
_LEASE_BOOKS: weakref.WeakKeyDictionary[Any, FurnaceLeaseBook] = weakref.WeakKeyDictionary()
_FALLBACK_BOOK: Optional[FurnaceLeaseBook] = None


def lease_book_for(state: Any) -> FurnaceLeaseBook:
    """Return the per-`State` `FurnaceLeaseBook`, with a shared fallback for unweakrefable states."""
    global _FALLBACK_BOOK
    with _REGISTRY_LOCK:
        if state is not None:
            try:
                book = _LEASE_BOOKS.get(state)
                if book is None:
                    book = FurnaceLeaseBook()
                    _LEASE_BOOKS[state] = book
                return book
            except TypeError:
                pass
        if _FALLBACK_BOOK is None:
            _FALLBACK_BOOK = FurnaceLeaseBook()
        return _FALLBACK_BOOK


def reset_all_furnace_leases() -> None:
    """Clear all per-state furnace lease books (primarily for test isolation)."""
    global _FALLBACK_BOOK
    with _REGISTRY_LOCK:
        _LEASE_BOOKS.clear()
        _FALLBACK_BOOK = None


def _sync_lease_fields(book: FurnaceLeaseBook, lease: FurnaceLease, *, count: int, returns_empty_bucket: bool) -> None:
    with book._lock:
        active = book.get_lease(lease.pos, dimension=lease.dimension)
        for item in (lease,) if active is None or active is lease else (lease, active):
            item.count = count
            item.returns_empty_bucket = returns_empty_bucket


def load_and_lease(
    ex: Executor, state: State, pos: Sequence[int | float], *, input_item: str,
    count: int, fuel_item: str, fuel_count: int = 1, station_kind: str = "furnace",
    expected_output: str | None = None, consumer: str = "", reservation_book: Any = None,
) -> Generator[dict, None, FurnaceLease]:
    """Load input and fuel into an empty station and register an active lease without sleeping."""
    kind = bare(station_kind).strip()
    smelt_seconds_per_item(kind)
    _positive_count(count)
    _positive_count(fuel_count)
    raw, fuel = bare(input_item).strip(), bare(fuel_item).strip()
    if not raw or not fuel:
        raise Replan("熔炉装料需要有效的输入物品与燃料物品")
    exp_out, cell = smelt_output_for(raw, expected_output), _norm_pos(pos)
    target = {"pos": list(cell)}
    state.refresh_inventory(ex.client)
    if raw == fuel:
        if state.count(raw) < count + fuel_count:
            raise Replan(f"熔炉装料需 {count + fuel_count} 个 {raw}，背包不足")
    elif state.count(raw) < count or state.count(fuel) < fuel_count:
        raise Replan(f"熔炉装料所需 {raw}/{fuel} 背包库存不足")
    view = yield from window_view(ex, target)
    if bare(view.get("window")).strip() != kind:
        raise Replan(f"目标窗口 {view.get('window')!r} 与预期工作站 {kind} 不符")
    if window_section(view, "input") or window_section(view, "fuel"):
        raise Replan(f"熔炉 {cell} 的 input 或 fuel 槽非空，停止装料")
    yield from _put(ex, state, target, {"op": "put", "item": raw, "count": count, "to": "input"})
    yield from _put(ex, state, target, {"op": "put", "item": fuel, "count": fuel_count, "to": "fuel"})
    dim = _norm_dim((state.snapshot() or {}).get("dimension"))
    try:
        rec_fn = getattr(memo_for(state), "record_station", None)
        if callable(rec_fn):
            rec_fn(kind, cell, dimension=dim)
    except Exception:
        pass
    return lease_book_for(state).register_lease(
        cell, dimension=dim, station_kind=kind, input_item=raw, expected_output=exp_out,
        count=count, consumer=consumer, returns_empty_bucket=(fuel == "lava_bucket"),
        reservation_book=reservation_book, state=state, fuel_item=fuel, fuel_count=fuel_count,
    )


def collect_leased(ex: Executor, state: State, lease: FurnaceLease) -> Generator[dict, None, int]:
    """Collect available smelted output (and empty bucket if returned) for a lease."""
    target = {"pos": list(lease.pos)}
    view = yield from window_view(ex, target)
    if bare(view.get("window")).strip() not in SMELTING_STATIONS:
        raise Replan(f"目标窗口 {view.get('window')!r} 不是有效熔炼工作站")
    out_item = bare(lease.expected_output).strip()
    available, orig_count, collected = window_amount(view, "output", out_item), lease.count, 0
    if available > 0:
        _, gained, inv_full = yield from _take(ex, state, target, "output", out_item, available)
        collected = min(gained, available) if inv_full else available
    remaining, book = max(0, orig_count - collected), lease_book_for(state)
    if collected > 0:
        _sync_lease_fields(book, lease, count=remaining, returns_empty_bucket=lease.returns_empty_bucket)
    bucket_collected = False
    bucket_avail = window_amount(view, "fuel", "bucket") if lease.returns_empty_bucket else 0
    if bucket_avail > 0:
        _, bucket_gained, _ = yield from _take(
            ex, state, target, "fuel", "bucket", bucket_avail, allow_partial_on_full=True,
        )
        bucket_collected = bucket_gained >= bucket_avail
    batch_done = collected >= orig_count or not window_section(view, "input")
    bucket_done = (not lease.returns_empty_bucket) or bucket_collected
    if batch_done and bucket_done:
        book.complete_lease(lease.pos, dimension=lease.dimension)
    else:
        _sync_lease_fields(book, lease, count=remaining, returns_empty_bucket=(not bucket_done))
    return collected
