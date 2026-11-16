"""Material reservations over inventory snapshots, with explicit settlement."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from ..runtime import State
from ..skills.items import bare

ReservationMode = Literal["consume", "borrow", "in_transit"]
StockMode = Literal["consume", "borrow"]
_BUCKETS = frozenset((
    "bucket", "water_bucket", "lava_bucket", "milk_bucket", "powder_snow_bucket",
    "cod_bucket", "salmon_bucket", "pufferfish_bucket", "tropical_fish_bucket",
    "axolotl_bucket", "tadpole_bucket",
))


@dataclass(frozen=True)
class Reservation:
    consumer: str
    item: str
    count: int
    mode: ReservationMode


class ReservationBook:
    """Single scheduling loop owns this book. Entries in transit are outside inventory."""

    def __init__(self, safety_floor: Mapping[str, int] | None = None):
        self._safety_floor = {bare(item): count for item, count in (safety_floor or {}).items()}
        if any(type(count) is not int or count < 0 for count in self._safety_floor.values()):
            raise ValueError("安全底线须为非负整数")
        self._entries: dict[tuple[str, str, ReservationMode], Reservation] = {}
        self._scope: tuple[str | None, object, float | None] | None = None
        self.stale = False

    @property
    def entries(self) -> tuple[Reservation, ...]:
        return tuple(self._entries.values())

    @staticmethod
    def _state_scope(state: State, status: dict) -> tuple[str | None, object, float | None]:
        dimension = status.get("dimension")
        reported = (status.get("last_death") or {}).get("t")
        try:
            latest_seen = state.deaths[-1]
        except IndexError:
            latest_seen = None
        return bare(dimension) or None, reported, latest_seen

    def observe(self, state: State) -> bool:
        """Detect native death evidence or a known dimension transition, without any API I/O."""
        status = state.snapshot()
        actual = self._state_scope(state, status)
        if status.get("alive") is False:
            self.stale = True
        if self._scope is not None:
            old_dimension, old_reported, old_seen = self._scope
            dimension, reported, seen = actual
            if ((dimension is not None and old_dimension is not None and dimension != old_dimension)
                    or (reported is not None and reported != old_reported) or seen != old_seen):
                self.stale = True
            actual = dimension or old_dimension, reported if reported is not None else old_reported, seen
        self._scope = actual
        return not self.stale

    def reset(self, state: State) -> None:
        """Discard old allocations after the caller has reconciled current stock and windows."""
        self._entries.clear()
        self._scope = None
        self.stale = False
        self.observe(state)

    def _available(self, state: State, item: str, reserved: tuple[Reservation, ...]) -> int:
        exact = state.count(item) - self._safety_floor.get(item, 0)
        exact -= sum(entry.count for entry in reserved if entry.item == item)
        if item not in _BUCKETS:
            return max(0, exact)
        # A bucket held for one goal remains reserved when it becomes a different bucket variant.
        shared = sum(max(0, state.count(name) - self._safety_floor.get(name, 0)) for name in _BUCKETS)
        shared -= sum(entry.count for entry in reserved if entry.item in _BUCKETS)
        return max(0, min(exact, shared))

    def available(self, state: State, item: str, consumer: str | None = None) -> int:
        if not self.observe(state) or state.inventory_dirty:
            return 0
        reserved = tuple(entry for entry in self.entries
                         if entry.mode != "in_transit" and entry.consumer != consumer)
        return self._available(state, bare(item), reserved)

    def reserve(self, state: State, consumer: str, item: str, count: int, *,
                mode: StockMode = "consume") -> bool:
        """Set the requested amount on one consumer/item/mode key, atomically on success."""
        item = bare(item)
        if not consumer or not item or type(count) is not int or count < 1 or mode not in ("consume", "borrow"):
            raise ValueError("预留需要消费者、物品、正整数数量与有效模式")
        if not self.observe(state) or state.inventory_dirty:
            return False
        key = consumer, item, mode
        other = tuple(entry for other_key, entry in self._entries.items()
                      if other_key != key and entry.mode != "in_transit")
        if self._available(state, item, other) < count:
            return False
        self._entries[key] = Reservation(consumer, item, count, mode)
        return True

    def move_to_container(
        self, state: State, consumer: str, item: str, count: int, *, from_mode: StockMode = "consume",
    ) -> bool:
        """Transfer a witnessed departure from stock into a window entry."""
        item = bare(item)
        if type(count) is not int or count < 1 or from_mode not in ("consume", "borrow"):
            raise ValueError("移动到容器需要已确认的正整数数量和库存预留模式")
        if not self.observe(state):
            return False
        key = consumer, item, from_mode
        prior = self._entries.get(key)
        if prior is None or prior.count < count:
            return False
        transit_key = consumer, item, "in_transit"
        transit = self._entries.get(transit_key)
        self._entries[transit_key] = Reservation(
            consumer, item, count + (transit.count if transit else 0), "in_transit",
        )
        if prior.count == count:
            del self._entries[key]
        else:
            self._entries[key] = Reservation(consumer, item, prior.count - count, from_mode)
        return True

    def release(self, consumer: str, item: str | None = None, *,
                mode: ReservationMode | None = None) -> None:
        """Release all matching entries after cancellation, consumption or confirmed recovery."""
        for key, entry in tuple(self._entries.items()):
            if (entry.consumer == consumer and (item is None or entry.item == bare(item))
                    and (mode is None or entry.mode == mode)):
                del self._entries[key]
