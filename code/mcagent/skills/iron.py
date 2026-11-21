"""铁矿采集与熔炼 L3；窗口协议见 agent-api-spec-dist §4.9。

只依赖当场暴露面观察；燃烧耗时仅作有界等待预算，不当作成功判据。
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Collection, Iterator, Optional, Sequence
from weakref import WeakKeyDictionary

from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, failure, rec
from .field import move_local
from .furnace import collect_furnace, inspect_furnace, load_furnace
from .furnace_lease import (
    EXTENDED_FUEL_BURNS, FurnaceLease, SMELTING_STATIONS, best_fuel_for,
    collect_leased, lease_book_for, load_and_lease, smelt_seconds_per_item,
)
from .items import AIR, STONE_OR_BETTER_PICKS, bare, best_pickaxe, block_name, record_mine_trail
from .mining import (
    SOLID_SUPPORT, MineStepUnsafe, mine_step, prepare_mining_pick, safe_mine_column,
    under_feet,
)
from .recipes import craft_recipe
from .registry import register
from .sightline import (
    API_DIG_EYE_REACH, DIG_EYE_REACH, STATION_EYE_REACH, can_reach_block, eye_to_block_dist,
)
from .voxel_cache import client_get_blocks_batch, has_batch_get_blocks, voxel_cache_for
from .windows import window_amount, window_section, window_view
from .world_memo import memo_for

if TYPE_CHECKING:
    from ..runtime import Executor, State

IRON_ORES = ("iron_ore", "deepslate_iron_ore", "raw_iron_block")
WOODS = ("oak", "birch", "spruce", "jungle", "acacia", "dark_oak", "cherry", "mangrove", "pale_oak")
_BLAST_INPUTS = frozenset({
    "raw_iron", "iron_ore", "deepslate_iron_ore",
    "raw_gold", "gold_ore", "deepslate_gold_ore", "nether_gold_ore",
    "raw_copper", "copper_ore", "deepslate_copper_ore",
    "ancient_debris",
})
_SMOKER_INPUTS = frozenset({
    "beef", "porkchop", "chicken", "mutton", "rabbit",
    "cod", "salmon", "potato", "kelp",
})


def _fuel_units(item: str) -> int:
    return int(round(EXTENDED_FUEL_BURNS[item] * 2))


# 半个冶炼单位：煤/木炭 8 个，木板/原木 1.5 个；退役木镐 1 个、木棍 0.5 个仅作兜底。
_BASE_FUEL_NAMES = (
    "coal", "charcoal", "coal_block", "dried_kelp_block", "blaze_rod",
    *(f"{w}_planks" for w in WOODS),
    "bamboo_planks", "bamboo_mosaic", "bamboo_block",
    *(f"{w}_log" for w in WOODS),
)
BASE_FUELS = tuple((name, _fuel_units(name)) for name in _BASE_FUEL_NAMES)
RADIUS = 32
MINING_REACH = DIG_EYE_REACH
PROSPECT_STEPS = 6
SHALLOW_IRON_Y = 16
MAX_PROSPECT_STEPS = 56
MAX_ORE_ROUTE_FAILURES = 3
POLL_SECONDS = 0.5
# 同连接、维度、区块和生存阶段共享不可达矿点记忆，及水矿洞/已耗尽矿井区块黑名单。
_SESSION_EXCLUDED = WeakKeyDictionary()
_FLOODED_CHUNKS = WeakKeyDictionary()
_EXHAUSTED_SHAFTS = WeakKeyDictionary()
_WATER_BLOCKS = frozenset({"water", "flowing_water", "bubble_column"})


def reset_iron_memo() -> None:
    _SESSION_EXCLUDED.clear()
    _FLOODED_CHUNKS.clear()
    _EXHAUSTED_SHAFTS.clear()


def _flooded_chunks_for(client: object, dim: str) -> set[tuple[int, int]]:
    by_dim = _FLOODED_CHUNKS.get(client)
    if by_dim is None:
        by_dim = {}
        _FLOODED_CHUNKS[client] = by_dim
    return by_dim.setdefault(dim, set())


def _exhausted_shafts_for(client: object, dim: str) -> set[tuple[int, int]]:
    by_dim = _EXHAUSTED_SHAFTS.get(client)
    if by_dim is None:
        by_dim = {}
        _EXHAUSTED_SHAFTS[client] = by_dim
    return by_dim.setdefault(dim, set())


class _OreDropUncollected(Replan):
    """合规工具挖掘且无告警，但掉落物未立即入包；矿脉尚有邻矿时可继续采完。"""


def _column_has_water(ex: "Executor", pos: Sequence[int]) -> bool:
    x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
    return any(block_name(ex, [x, y + dy, z]) in _WATER_BLOCKS for dy in (-1, 0, 1, 2, 3))


def _halt_residual_motion(ex: "Executor", state: "State") -> dict:
    """残留水平速度过高时主动刹停，防止 path_not_found 后惯性/按键粘连导致阶梯站位偏移。"""
    status = state.refresh_status(ex.client, max_age_s=0)
    vel = status.get("velocity")
    if (isinstance(vel, (list, tuple)) and len(vel) >= 3
            and math.hypot(float(vel[0]), float(vel[2])) > 0.25):
        stop_fn = getattr(ex, "stop", None)
        if callable(stop_fn):
            stop_fn(raise_on_error=False)
            status = state.refresh_status(ex.client, max_age_s=0)
    return status


def stations_for_item(item: str) -> tuple[str, ...]:
    """Return preferred smelting station kinds for `item` in priority order."""
    name = bare(item).strip()
    if name in _BLAST_INPUTS:
        return ("blast_furnace", "furnace")
    if name in _SMOKER_INPUTS:
        return ("smoker", "furnace")
    return ("furnace",)


def _reach_mod():
    try:
        from . import reach
        return reach
    except ImportError:
        return None


def _bounded_move_to(ex: "Executor", pos, *, rng: int = 2, timeout: float = 30.0):
    """Call ex.move_to with bounded timeout/retries, falling back for strict mocks."""
    try:
        return ex.move_to(pos=pos, rng=rng, timeout=timeout, retries=0)
    except TypeError:
        return ex.move_to(pos=pos, rng=rng)


def start_smelt_lease(
    ex: "Executor",
    state: "State",
    pos,
    *,
    input_item: str = "raw_iron",
    count: int,
    fuel_item: str,
    fuel_count: int = 1,
    station_kind: str = "furnace",
    expected_output: Optional[str] = None,
    consumer: str = "smelt_iron",
    reservation_book=None,
) -> Iterator[dict]:
    """Load a smelting station and register a non-blocking `FurnaceLease`."""
    return (
        yield from load_and_lease(
            ex,
            state,
            pos,
            input_item=input_item,
            count=count,
            fuel_item=fuel_item,
            fuel_count=fuel_count,
            station_kind=station_kind,
            expected_output=expected_output,
            consumer=consumer,
            reservation_book=reservation_book,
        )
    )


def collect_smelt_lease(ex: "Executor", state: "State", lease: FurnaceLease) -> Iterator[dict]:
    """Move into station reach if needed and collect output/bucket for `lease`."""
    status = state.refresh_status(ex.client, max_age_s=0)
    stance = status if status.get("pos") is not None else (
        state.st if (state.st or {}).get("pos") is not None else state.pos
    )
    if not can_reach_block(ex, stance, lease.pos, reach=STATION_EYE_REACH):
        yield rec("move_to", _bounded_move_to(ex, list(lease.pos), rng=2, timeout=30.0), pos=list(lease.pos))
    return (yield from collect_leased(ex, state, lease))


def _inspect_station(ex: "Executor", target: dict, station_kind: str = "furnace") -> Iterator[dict]:
    """Inspect a smelting station window, preserving `furnace` error messages."""
    kind = bare(station_kind).strip() or "furnace"
    if kind == "furnace":
        return (yield from inspect_furnace(ex, target))
    view = yield from window_view(ex, target)
    win = bare(view.get("window")).strip()
    if win != kind and win not in SMELTING_STATIONS:
        raise Replan(f"目标窗口未确认是 {kind}")
    return view


def _collect_station(
    ex: "Executor",
    state: "State",
    target: dict,
    *,
    item: str,
    section: str = "output",
    view: Optional[dict] = None,
    station_kind: str = "furnace",
    inspect_after: bool = True,
    require_empty: bool = False,
) -> Iterator[dict]:
    """Collect from any smelting station window (`furnace`, `blast_furnace`, `smoker`)."""
    kind = bare(station_kind).strip() or "furnace"
    if kind == "furnace":
        return (
            yield from collect_furnace(
                ex,
                state,
                target,
                item=item,
                section=section,
                view=view,
                inspect_after=inspect_after,
                require_empty=require_empty,
            )
        )
    if view is None:
        view = yield from _inspect_station(ex, target, station_kind=kind)
    result = yield from collect_furnace(
        ex,
        state,
        target,
        item=item,
        section=section,
        view=view,
        inspect_after=False,
        require_empty=False,
    )
    after = None
    if inspect_after or require_empty:
        after = yield from _inspect_station(ex, target, station_kind=kind)
        if require_empty and window_section(after, section):
            raise Replan(f"熔炉 {target.get('pos')} 取出后 {section} 仍有剩余产物：{after}")
    result["view"] = after
    return result


def _record_station(state: "State", kind: str, pos, dimension: str = "overworld") -> None:
    memo = memo_for(state)
    rec_fn = getattr(memo, "record_station", None)
    if callable(rec_fn):
        rec_fn(kind, pos, dimension=dimension)


def _session_excluded_for(client: object, state: "State", status: dict) -> set[tuple[int, int, int]]:
    cur = status.get("pos") or state.pos
    deaths = tuple(state.deaths)
    scope = (status.get("dimension"), math.floor(cur[0] / 16), math.floor(cur[2] / 16), deaths[-1:])
    entry = _SESSION_EXCLUDED.get(client)
    if entry is None or entry[0] != scope:
        fresh: set[tuple[int, int, int]] = set()
        _SESSION_EXCLUDED[client] = (scope, fresh)
        return fresh
    return entry[1]


def _fuels_for(state: "State") -> tuple[tuple[str, int], ...]:
    extra: list[tuple[str, int]] = []
    if best_pickaxe(state) in STONE_OR_BETTER_PICKS:
        extra.append(("wooden_pickaxe", 2))
    extra.append(("stick", _fuel_units("stick")))
    extra.append(("leaf_litter", _fuel_units("leaf_litter")))
    extra.append(("lava_bucket", _fuel_units("lava_bucket")))
    return BASE_FUELS + tuple(extra)


class _OreRouteUnconfirmed(Replan):
    pass


def _log_for_fuel(stock, fuels, count: int) -> Optional[str]:
    non_log = sum(stock(n) * u // 2 for n, u in fuels if not n.endswith("_log"))
    direct = sum(stock(n) * u // 2 for n, u in fuels)
    if non_log >= count or (direct >= count and count < 3):
        return None
    return next((w for w in WOODS if stock(f"{w}_log") > 0), None)


def _fuel_batches(stock, fuels, remaining: int) -> tuple[list[tuple[str, int, int]], int]:
    plan = []
    for item, units in fuels:
        available = stock(item)
        if item == "stick" and available > 2 and remaining * 2 <= (available - 2) * units:
            available -= 2
        batch = min(remaining, available * units // 2)
        if batch:
            plan.append((item, math.ceil(batch * 2 / units), batch))
            remaining -= batch
        if not remaining:
            break
    return plan, remaining


@register
class SmeltIron(Skill):
    name = "smelt_iron"
    description = "采集暴露铁矿，缺矿时有界阶梯探矿，再安全熔炼取出铁锭"
    params = {
        "count": Param(int, "期望新增铁锭数（粗铁用完即止）", default=3, min=1, max=16),
        "async_lease": Param(bool, "是否采用非阻塞租约模式（装料登记后立即返回或回收就绪租约）", default=False),
    }
    goal = "acq:iron_ingot"
    verified = "offline"

    @classmethod
    def material_plan(cls, args: dict, state: "State") -> tuple[tuple[str, int, str], ...]:
        """Declare the current stock the planned batch can use; mined inputs enter later."""
        count = args.get("count", 3)
        raw = min(count, state.count("raw_iron"))
        blocks = min(state.count("raw_iron_block"), math.ceil((count - raw) / 9))
        fuels = _fuels_for(state)
        stock = {item: state.count(item) for item, _ in fuels}
        materials = {"raw_iron": raw, "raw_iron_block": blocks}
        batch = min(count, raw + 9 * blocks) or count
        for _ in range(batch):
            wood = _log_for_fuel(stock.__getitem__, fuels, batch)
            if wood is None:
                break
            log, plank = f"{wood}_log", f"{wood}_planks"
            materials[log] = materials.get(log, 0) + 1
            stock[log] -= 1
            stock[plank] += 4
        for item, qty, _ in _fuel_batches(stock.__getitem__, fuels, batch)[0]:
            materials[item] = materials.get(item, 0) + min(qty, state.count(item))
        return tuple((item, qty, "consume") for item, qty in materials.items() if qty)

    @classmethod
    def blocked(cls, state: "State") -> str:
        has_iron_input = state.count("raw_iron") >= 1 or state.count("raw_iron_block") >= 1
        if not has_iron_input and best_pickaxe(state) not in STONE_OR_BETTER_PICKS:
            return "背包无粗铁且缺石镐或更好的镐（木镐、金镐不能采铁）"
        if not any(state.count(n) for n, _ in _fuels_for(state)) and best_pickaxe(state) not in STONE_OR_BETTER_PICKS:
            return "背包缺煤、木板或原木燃料，且无可采煤的镐"
        return ""

    def _unpack_raw_iron_blocks(self, ex: "Executor", state: "State") -> Iterator[dict]:
        deficit = max(0, self.args["count"] - state.count("raw_iron"))
        for _ in range(min(state.count("raw_iron_block"), math.ceil(deficit / 9))):
            before = state.count("raw_iron")
            yield rec("craft", ex.craft("raw_iron", 1), item="raw_iron")
            state.refresh_inventory(ex.client)
            if state.count("raw_iron") <= before:
                raise Replan("拆解 raw_iron_block 后背包 raw_iron 未增加")

    def _mine(self, ex: "Executor", state: "State", pos, ores, drop: str) -> Iterator[dict]:
        status = state.refresh_status(ex.client, max_age_s=0)
        dimension = status.get("dimension")
        dim_name = str(dimension or "overworld")
        cur = status.get("pos") or state.pos
        stance = status if status.get("pos") is not None else cur
        reach_mod = _reach_mod()
        if not can_reach_block(ex, stance, pos, reach=MINING_REACH):
            if (reach_mod is not None
                    and status.get("dimension") == dimension
                    and not under_feet(cur, pos)
                    and eye_to_block_dist(stance, pos) <= MINING_REACH):
                yield from prepare_mining_pick(ex, state)
                yield from reach_mod.clear_line_of_sight(
                    ex, state, pos, max_clear=2, reach=API_DIG_EYE_REACH,
                )
                status = state.refresh_status(ex.client, max_age_s=0)
                cur = status.get("pos") or state.pos
                stance = status if status.get("pos") is not None else cur
            if not can_reach_block(ex, stance, pos, reach=MINING_REACH):
                yield rec("move_to", _bounded_move_to(ex, pos, rng=2, timeout=30.0), pos=pos)
                status = state.refresh_status(ex.client, max_age_s=0)
                if (reach_mod is not None and hasattr(reach_mod, "settle_dry_landing")
                        and not status.get("on_ground")
                        and not status.get("in_water")
                        and not status.get("in_lava")):
                    status = reach_mod.settle_dry_landing(ex, state)
                cur = status.get("pos") or state.pos
                stance = status if status.get("pos") is not None else cur
                if (reach_mod is not None
                        and status.get("dimension") == dimension
                        and not under_feet(cur, pos)
                        and not can_reach_block(ex, stance, pos, reach=MINING_REACH)
                        and eye_to_block_dist(stance, pos) <= API_DIG_EYE_REACH):
                    yield from prepare_mining_pick(ex, state)
                    yield from reach_mod.clear_line_of_sight(
                        ex, state, pos, max_clear=2, reach=API_DIG_EYE_REACH,
                    )
        status = state.refresh_status(ex.client, max_age_s=0)
        cur = status.get("pos") or state.pos
        stance = status if status.get("pos") is not None else cur
        if status.get("dimension") != dimension:
            raise Replan("移动到矿点期间维度变化，重新观察后再挖")
        if under_feet(cur, pos):
            raise Replan(f"观察到 {drop} 矿 {pos} 已在脚下，停止挖掘以保留支撑")
        if not can_reach_block(ex, stance, pos, reach=API_DIG_EYE_REACH):
            raise _OreRouteUnconfirmed(f"移动后未确认到达矿点 {pos} 的挖掘范围")
        block_type = bare(ex.client.get_block(pos).get("name"))
        if block_type not in ores:
            raise Replan(f"到达 {pos} 后 get_block 未确认目标矿 {ores}")
        # move_to / clear_line_of_sight 可能触发服务端自动切换手持物品，开挖前无条件复核持镐
        yield from prepare_mining_pick(ex, state)
        status = state.refresh_status(ex.client, max_age_s=0)
        cur = status.get("pos") or state.pos
        stance = status if status.get("pos") is not None else cur
        if under_feet(cur, pos):
            raise Replan(f"装备后重新观察 {pos} 已在脚下，停止挖掘以保留支撑")
        if (status.get("dimension") != dimension
                or not can_reach_block(ex, stance, pos, reach=API_DIG_EYE_REACH)
                or bare(ex.client.get_block(pos).get("name")) != block_type):
            raise Replan(f"装备后矿点 {pos} 或站位已变化，重新观察后再挖")
        expected_drop = "raw_iron_block" if block_type == "raw_iron_block" else drop
        before = state.count(expected_drop)
        result = ex.dig(pos, collect=True)
        yield rec("dig", result, pos=pos)
        voxel_cache_for(state).invalidate(pos, dimension=dim_name)
        state.refresh_inventory(ex.client)
        if result.get("warning"):
            raise Replan(f"挖掘 {pos} 后背包 {expected_drop} 未确认增加或工具告警：{result.get('warning', '')}")
        if state.count(expected_drop) <= before:
            raise _OreDropUncollected(f"挖掘 {pos} 后背包 {expected_drop} 未确认增加或工具告警：")
        if expected_drop == "raw_iron_block":
            yield from self._unpack_raw_iron_blocks(ex, state)

    def _ore_candidates(
        self, ex: "Executor", state: "State", radius=RADIUS, *,
        excluded: Collection[tuple[int, int, int]] = (),
        in_reach_only: bool = False,
        max_dy: Optional[float] = None,
    ) -> list:
        search_radius = math.ceil(API_DIG_EYE_REACH) if in_reach_only else radius
        limit = 16 if in_reach_only else 64
        found = ex.client.find_blocks(types=list(IRON_ORES), radius=search_radius, limit=limit)
        blocks = (found.get("blocks") if isinstance(found, dict) else None) or []
        status = state.refresh_status(ex.client, max_age_s=0)
        cur = status.get("pos") or state.pos
        stance = status if status.get("pos") is not None else cur
        dim = str(status.get("dimension") or "overworld")
        cur_cell = tuple(map(math.floor, cur))
        vc = voxel_cache_for(state)
        vc.ingest_find_blocks(found, dimension=dim, client=ex.client)
        vc.clear_stance_blacklist_if_moved(cur)
        cands = [
            b["pos"] for b in blocks
            if isinstance(b.get("pos"), list) and len(b["pos"]) == 3
            and tuple(b["pos"]) not in excluded
            and not vc.is_unreachable("smelt_iron", b["pos"], dimension=dim, stance_cell=cur_cell)
            and (max_dy is None or abs(b["pos"][1] - cur[1]) <= max_dy)
            and not under_feet(cur, b["pos"])
            and (not in_reach_only or can_reach_block(ex, stance, b["pos"], reach=MINING_REACH))
        ]
        if not in_reach_only and len(cands) > 1:
            cands.sort(
                key=lambda p: (
                    0 if abs(p[1] - cur[1]) <= 8.0 else (1 if abs(p[1] - cur[1]) <= 12.0 else 2),
                    eye_to_block_dist(stance, p),
                )
            )
        return cands

    @staticmethod
    def _initial_heading(start: tuple[int, int, int], excluded: Collection[tuple[int, int, int]]) -> tuple[int, int]:
        deep = [p for p in excluded if p[1] <= start[1] - 4]
        if not deep:
            return (1, 0)
        nearest = min(deep, key=lambda p: math.hypot(p[0] - start[0], p[2] - start[2]))
        dx, dz = nearest[0] - start[0], nearest[2] - start[2]
        if abs(dx) >= abs(dz):
            return (1, 0) if dx >= 0 else (-1, 0)
        return (0, 1) if dz >= 0 else (0, -1)

    def _prospect(
        self, ex: "Executor", state: "State", excluded: set[tuple[int, int, int]], *,
        in_reach_only: bool, checked_wide_shallow: bool = False,
    ) -> Iterator[dict]:
        # API §3.3 允许 radius <= 64，但仍只返回暴露矿，不能据空扫描断言环境无矿。
        if not in_reach_only:
            candidates = self._ore_candidates(ex, state, radius=64, excluded=excluded)
            if candidates:
                return candidates
        elif not checked_wide_shallow:
            shallow_wide = self._ore_candidates(ex, state, radius=64, excluded=excluded, max_dy=12.0)
            if shallow_wide:
                return shallow_wide

        st0 = _halt_residual_motion(ex, state)
        cur0 = st0.get("pos") or state.pos
        dim = str(st0.get("dimension") or "overworld")
        start = tuple(map(math.floor, cur0))
        start_y = start[1]
        start_chunk = (start[0] // 16, start[2] // 16)
        flooded_chunks = _flooded_chunks_for(ex.client, dim)
        exhausted_shafts = _exhausted_shafts_for(ex.client, dim)
        if start_chunk in flooded_chunks:
            raise EnvBlocked(
                f"当前区块 {start_chunk} 已探明存在水矿洞，直接绕开前往其他区域",
                missing_predicate="dry_iron_shaft",
                searched_volume=f"已跳过积水区块 {start_chunk}",
                wake_on="chunk_moved",
            )
        if start_chunk in exhausted_shafts:
            raise EnvBlocked(
                f"当前区块 {start_chunk} 的探矿阶梯已耗尽，直接外迁换区探索",
                missing_predicate="exposed_iron",
                searched_volume=f"已跳过耗尽矿口区块 {start_chunk}",
                wake_on="chunk_moved",
            )
        max_steps = min(MAX_PROSPECT_STEPS, max(PROSPECT_STEPS, start_y - SHALLOW_IRON_Y))
        visited: set[tuple[int, int]] = set()
        completed_steps = 0
        backtracks = 0
        water_blocked = False
        heading = self._initial_heading(start, excluded)
        waypoints: list[list[float]] = [[start[0] + 0.5, float(start_y), start[2] + 0.5]]

        def observe_cut():
            if state.count("raw_iron_block") >= 1:
                yield from self._unpack_raw_iron_blocks(ex, state)
            candidates = self._ore_candidates(ex, state, excluded=excluded, in_reach_only=in_reach_only)
            return candidates if candidates or state.count("raw_iron") else None

        reach_mod = _reach_mod()
        stance_retries = 0

        try:
            while completed_steps < max_steps:
                status = _halt_residual_motion(ex, state)
                if (reach_mod is not None and hasattr(reach_mod, "settle_dry_landing")
                        and status.get("on_ground") is False
                        and not status.get("in_water")
                        and not status.get("in_lava")):
                    status = reach_mod.settle_dry_landing(ex, state)
                cur = status.get("pos") or state.pos
                x, y, z = map(math.floor, cur)
                visited.add((x, z))
                dirs = (heading,) + tuple(d for d in ((1, 0), (-1, 0), (0, 1), (0, -1)) if d != heading)
                pos = next(([x + dx, y - 1, z + dz] for dx, dz in dirs
                            if (x + dx, z + dz) not in visited
                            and safe_mine_column(ex, [x + dx, y - 1, z + dz])), None)
                if pos is None:
                    # 遇水矿洞封死时不再在洞内来回回溯绕圈，直接标记积水区块并切去别处
                    if any(
                        (x + dx, z + dz) not in visited and _column_has_water(ex, [x + dx, y - 1, z + dz])
                        for dx, dz in dirs
                    ):
                        water_blocked = True
                        flooded_chunks.add((x // 16, z // 16))
                        flooded_chunks.add(start_chunk)
                        break
                    # 井底正前与两侧均遇水/砾石受阻时，沿已挖阶梯后退一级尝试侧向分支。
                    if len(waypoints) >= 2 and backtracks < 2:
                        prev = waypoints[-2]
                        status = yield from move_local(ex, state, prev, require_move=True)
                        if tuple(map(math.floor, status.get("pos") or state.pos)) != tuple(map(math.floor, prev)):
                            raise Replan("未确认退到上一阶，保留原阶梯轨迹供退出")
                        backtracks += 1
                        waypoints.pop()
                        continue
                    break
                visited.add((pos[0], pos[2]))
                try:
                    dest, observation = yield from mine_step(ex, state, pos, after_dig=observe_cut)
                except MineStepUnsafe as err:
                    if err.stage == "stance" and stance_retries < 4:
                        visited.discard((pos[0], pos[2]))
                        stance_retries += 1
                        continue
                    if completed_steps > 0:
                        continue
                    if err.stage == "cut" and any(
                        (x + dx, z + dz) not in visited and safe_mine_column(ex, [x + dx, y - 1, z + dz])
                        for dx, dz in dirs
                    ):
                        continue
                    raise
                stance_retries = 0
                if dest is None:
                    return observation
                heading = (pos[0] - x, pos[2] - z)
                waypoints.append(dest)
                completed_steps += 1
                candidates = self._ore_candidates(ex, state, excluded=excluded, in_reach_only=in_reach_only)
                if candidates:
                    return candidates
        finally:
            if completed_steps > 0:
                record_mine_trail(waypoints)

        end = tuple(map(math.floor, state.pos))
        if water_blocked:
            raise EnvBlocked(
                f"探矿遇水矿洞（Y={start_y} 到 {end[1]}，走完 {completed_steps}/{max_steps} 级），直接绕开换区探索",
                missing_predicate="dry_iron_shaft",
                searched_volume=f"起点 {start} 遇水矿洞，已标记积水区块 {start_chunk} 与 {(end[0] // 16, end[2] // 16)}",
                wake_on="chunk_moved",
            )
        if completed_steps > 0:
            exhausted_shafts.add(start_chunk)
            exhausted_shafts.add((end[0] // 16, end[2] // 16))
        scan_scope = f"仅查近场 {MINING_REACH} 格" if in_reach_only else "半径 32/64"
        raise EnvBlocked(
            f"{scan_scope} 内未找到可采的暴露铁矿，"
            f"安全阶梯探矿仍未发现（Y={start_y} 到 {end[1]}，走完 {completed_steps}/{max_steps} 级，"
            f"浅矿目标 Y≈{SHALLOW_IRON_Y}）；需换区域探索",
            missing_predicate="exposed_iron",
            searched_volume=(f"起点 {start} 暴露矿扫描{scan_scope}；"
                             f"跳过 {len(excluded)} 个已试不可达矿点；至 {end} 阶梯 {completed_steps}/{max_steps} 级"),
            wake_on="chunk_moved",
        )

    def _sweep_adjacent_vein(
        self, ex: "Executor", state: "State", excluded: set[tuple[int, int, int]],
    ) -> Iterator[dict]:
        """采满当次目标粗铁后，检查紧邻 5 格内同矿脉暴露铁矿并顺手挖完，避免重复下矿。"""
        if self.args["count"] < 2 or best_pickaxe(state) not in STONE_OR_BETTER_PICKS:
            return
        vein_cap = min(12, self.args["count"] + 6)
        for _ in range(vein_cap):
            if state.count("raw_iron") >= vein_cap or best_pickaxe(state) not in STONE_OR_BETTER_PICKS:
                return
            nearby = self._ore_candidates(ex, state, radius=5, excluded=excluded, max_dy=4.0)
            if not nearby:
                return
            vpos = nearby[0]
            try:
                yield from self._mine(ex, state, vpos, IRON_ORES, "raw_iron")
            except Replan:
                excluded.add(tuple(vpos))
                state.refresh_inventory(ex.client)
                if best_pickaxe(state) not in STONE_OR_BETTER_PICKS:
                    return
                continue

    def _collect(self, ex: "Executor", state: "State") -> Iterator[dict]:
        if state.count("raw_iron_block") >= 1:
            yield from self._unpack_raw_iron_blocks(ex, state)
        status = state.refresh_status(ex.client, max_age_s=0)
        excluded = _session_excluded_for(ex.client, state, status)
        if state.count("raw_iron") >= self.args["count"]:
            return
        if state.count("raw_iron"):
            # 已有部分粗铁：无石镐或紧邻 8 格内没有现成矿脉则立刻熔炼，有矿脉则先采完
            if (best_pickaxe(state) not in STONE_OR_BETTER_PICKS
                    or not self._ore_candidates(ex, state, radius=8, excluded=excluded, max_dy=4.0)):
                return
        route_failures = len(excluded)
        checked_wide_shallow = False
        for _ in range(self.args["count"] + MAX_ORE_ROUTE_FAILURES + 4):
            # 同一深洞常返回多块矿。直达预算用完后，只试一次 64 格浅表露矿便切入近场阶梯探矿。
            in_reach_only = route_failures >= MAX_ORE_ROUTE_FAILURES
            candidates = self._ore_candidates(ex, state, excluded=excluded, in_reach_only=in_reach_only)
            if not candidates:
                if state.count("raw_iron"):
                    return
                candidates = yield from self._prospect(
                    ex, state, excluded,
                    in_reach_only=in_reach_only,
                    checked_wide_shallow=checked_wide_shallow,
                )
                if in_reach_only:
                    checked_wide_shallow = True
                if state.count("raw_iron") >= self.args["count"]:
                    yield from self._sweep_adjacent_vein(ex, state, excluded)
                    return
                if state.count("raw_iron") and not candidates:
                    return
            pos = candidates[0]
            try:
                yield from self._mine(ex, state, pos, IRON_ORES, "raw_iron")
            except _OreDropUncollected:
                excluded.add(tuple(pos))
                state.refresh_inventory(ex.client)
                has_more = bool(self._ore_candidates(ex, state, radius=8, excluded=excluded, max_dy=4.0))
                if not has_more and state.count("raw_iron") < 1:
                    raise
                yield rec("ore_route_skip", pos=pos, error_code="drop_uncollected")
                if not has_more and state.count("raw_iron") >= 1:
                    return
                continue
            except Replan as err:
                code, _ = failure(err)
                if isinstance(err, _OreRouteUnconfirmed):
                    code = "arrival_unconfirmed"
                elif code == "network" and ("timed out" in str(err).lower() or "timeout" in str(err).lower()):
                    code = "move_timeout"
                dig_skip = code in ("dig_unconfirmed", "out_of_reach", "protected_region")
                route_skip = code in (
                    "path_not_found", "unreachable", "move_timeout", "timeout", "stalled", "arrival_unconfirmed",
                )
                if route_skip:
                    _halt_residual_motion(ex, state)
                if not dig_skip and (
                    (
                        in_reach_only
                        and eye_to_block_dist(state.st, pos) <= MINING_REACH
                        and can_reach_block(ex, state.st, pos, reach=MINING_REACH)
                    )
                    or not route_skip
                ):
                    raise
                excluded.add(tuple(pos))
                err_st = state.refresh_status(ex.client, max_age_s=0)
                err_cur = err_st.get("pos") or state.pos
                voxel_cache_for(state).mark_unreachable(
                    "smelt_iron",
                    pos,
                    dimension=str(err_st.get("dimension") or "overworld"),
                    stance_cell=tuple(map(math.floor, err_cur)),
                )
                route_failures += 1
                yield rec("ore_route_skip", pos=pos, error_code=code)
                state.refresh_inventory(ex.client)
                continue
            if state.count("raw_iron") >= self.args["count"]:
                yield from self._sweep_adjacent_vein(ex, state, excluded)
                return

    def _prepare_fuel(self, ex: "Executor", state: "State", count: int) -> Iterator[dict]:
        fuels = _fuels_for(state)
        # 1 原木合成 4 木板可将可熔炼件数由 1 件提升至 6 件；需熔 ≥3 件或直接燃料不足时优先拆板。
        for _ in range(count):
            log_wood = _log_for_fuel(state.count, fuels, count)
            if log_wood is None:
                break
            plank = f"{log_wood}_planks"
            yield from craft_recipe(ex, state, plank, goal_qty=state.count(plank) + 1)

        # 若背包燃料仍不足（如起步造完工具仅剩 1 木板）且附近地表有树，顺手补砍 1 块原木转 4 木板。
        if _fuel_batches(state.count, fuels, count)[1]:
            log_types = [f"{w}_log" for w in WOODS]
            logs = ex.client.find_blocks(types=log_types, radius=24, limit=8).get("blocks") or []
            status = state.refresh_status(ex.client, max_age_s=0)
            cur = status.get("pos") or state.pos
            stance = status if status.get("pos") is not None else cur
            for b in logs:
                lpos = b.get("pos")
                if not (isinstance(lpos, list) and len(lpos) == 3) or under_feet(cur, lpos):
                    continue
                if abs(lpos[1] - cur[1]) > 6.0:
                    continue
                try:
                    if not can_reach_block(ex, stance, lpos, reach=MINING_REACH):
                        yield rec("move_to", _bounded_move_to(ex, lpos, rng=3, timeout=30.0), pos=lpos)
                    status = state.refresh_status(ex.client, max_age_s=0)
                    cur = status.get("pos") or state.pos
                    stance = status if status.get("pos") is not None else cur
                    if (under_feet(cur, lpos)
                            or not can_reach_block(ex, stance, lpos, reach=API_DIG_EYE_REACH)
                            or bare(ex.client.get_block(lpos).get("name")) not in log_types):
                        continue
                    before_logs = sum(state.count(x) for x in log_types)
                    yield rec("dig", ex.dig(lpos, collect=True), pos=lpos)
                    state.refresh_inventory(ex.client)
                    if sum(state.count(x) for x in log_types) > before_logs:
                        w = next((w for w in WOODS if state.count(f"{w}_log") >= 1), None)
                        if w is not None:
                            plank = f"{w}_planks"
                            yield from craft_recipe(ex, state, plank, goal_qty=state.count(plank) + 1)
                        break
                except Replan as err:
                    status = state.refresh_status(ex.client, max_age_s=0)
                    cur = status.get("pos") or state.pos
                    stance = status if status.get("pos") is not None else cur
                    code, _ = failure(err)
                    if code in (
                        "path_not_found", "unreachable", "move_timeout", "stalled",
                        "out_of_reach", "dig_unconfirmed", "protected_region",
                    ):
                        yield rec("fuel_wood_skip", pos=lpos, error_code=code)
                        continue
                    raise

    def _fuel_plan(self, state: "State", count: int) -> list[tuple[str, int, int]]:
        plan, remaining = _fuel_batches(state.count, _fuels_for(state), count)
        if not remaining:
            return plan
        raise Replan(
            f"检查背包煤/木板/原木后燃料不足：需冶炼 {count} 个，"
            f"缺 {remaining} 个冶炼单位（至少 {math.ceil(remaining / 1.5)} 块木板）"
        )

    def _furnace(self, ex: "Executor", state: "State", *, item: str = "raw_iron") -> Iterator[dict]:
        valid_stations = stations_for_item(item)
        dim = str((state.st or {}).get("dimension") or "overworld")
        stance = state.st if (state.st or {}).get("pos") is not None else state.pos
        found = ex.client.find_blocks(types=list(valid_stations), radius=16, limit=8).get("blocks") or []
        valid_pos_list = [b.get("pos") for b in found if isinstance(b.get("pos"), list) and len(b.get("pos")) == 3]
        if len(valid_pos_list) > 1 and has_batch_get_blocks(ex.client):
            client_get_blocks_batch(ex.client, valid_pos_list)
        observed: list[tuple[str, list]] = []
        for pos in valid_pos_list:
            block_name = bare(ex.client.get_block(pos).get("name"))
            if block_name in valid_stations:
                observed.append((block_name, pos))
        if observed:
            observed.sort(key=lambda entry: (valid_stations.index(entry[0]), eye_to_block_dist(stance, entry[1])))
            block_name, pos = observed[0]
            yield rec("move_to", _bounded_move_to(ex, pos, rng=2, timeout=30.0), pos=pos)
            _record_station(state, block_name, pos, dimension=dim)
            self._station_kind = block_name
            return pos
        station_item = next((kind for kind in valid_stations if state.count(kind) >= 1), None)
        if station_item is None:
            raise Replan("背包缺熔炉，find_blocks 半径 16 内也未观察到已有熔炉")
        st = state.refresh_status(ex.client, max_age_s=0)
        dim = str(st.get("dimension") or dim)
        x, y, z = map(math.floor, state.pos)
        valid_supports = SOLID_SUPPORT | {f"{w}_planks" for w in WOODS}
        # 先查平层与高一格台阶（适配 1x3 下行阶梯内转身往上一级台阶放炉），无需额外开凿。
        if has_batch_get_blocks(ex.client):
            client_get_blocks_batch(
                ex.client,
                [
                    [x + dx, y + dy + k, z + dz]
                    for dy in (0, 1) for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)) for k in (-1, 0, 1)
                ],
            )
        for dy in (0, 1):
            py = y + dy
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                pos = [x + dx, py, z + dz]
                support = bare(ex.client.get_block([pos[0], py - 1, pos[2]]).get("name"))
                if support not in valid_supports:
                    continue
                if bare(ex.client.get_block(pos).get("name")) not in AIR:
                    continue
                if bare(ex.client.get_block([pos[0], py + 1, pos[2]]).get("name")) not in AIR:
                    continue
                yield rec("place", ex.place(station_item, pos=pos), item=station_item, pos=pos)
                if bare(ex.client.get_block(pos).get("name")) != station_item:
                    raise Replan(f"放置后 get_block 在 {pos} 未确认熔炉")
                _record_station(state, station_item, pos, dimension=dim)
                self._station_kind = station_item
                return pos
        # 地下狭窄矿井兜底：若四向皆为安全实心岩壁且有石级镐，侧向凿出 1x2 壁龛放炉。
        if best_pickaxe(state) in STONE_OR_BETTER_PICKS:
            if has_batch_get_blocks(ex.client):
                client_get_blocks_batch(
                    ex.client,
                    [
                        [x + dx, y + k, z + dz]
                        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)) for k in (-1, 0, 1, 2)
                    ],
                )
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                pos = [x + dx, y, z + dz]
                if bare(ex.client.get_block([pos[0], y - 1, pos[2]]).get("name")) not in SOLID_SUPPORT:
                    continue
                if bare(ex.client.get_block([pos[0], y + 2, pos[2]]).get("name")) not in SOLID_SUPPORT:
                    continue
                if not all(bare(ex.client.get_block([pos[0], y + k, pos[2]]).get("name")) in AIR | SOLID_SUPPORT
                           for k in (0, 1)):
                    continue
                for k in (1, 0):
                    cell = [pos[0], y + k, pos[2]]
                    if bare(ex.client.get_block(cell).get("name")) not in AIR:
                        yield from prepare_mining_pick(ex, state)
                        yield rec("dig", ex.dig(cell, collect=True), pos=cell)
                if (bare(ex.client.get_block([pos[0], y - 1, pos[2]]).get("name")) in SOLID_SUPPORT
                        and all(bare(ex.client.get_block([pos[0], y + k, pos[2]]).get("name")) in AIR for k in (0, 1))):
                    yield rec("place", ex.place(station_item, pos=pos), item=station_item, pos=pos)
                    if bare(ex.client.get_block(pos).get("name")) != station_item:
                        raise Replan(f"放置后 get_block 在 {pos} 未确认熔炉")
                    _record_station(state, station_item, pos, dimension=dim)
                    self._station_kind = station_item
                    return pos
        raise Replan("观察脚边四个方向的落点/净空/底板后无安全熔炉位置，环境缺安全承重落点")

    def _poll_and_take(
        self,
        ex: "Executor",
        state: "State",
        pos,
        target: dict,
        batch: int,
        *,
        station_kind: Optional[str] = None,
    ) -> Iterator[dict]:
        kind = station_kind or getattr(self, "_station_kind", "furnace")
        sec = smelt_seconds_per_item(kind)
        wait_limit = int(sec * batch + 20)
        deadline = time.monotonic() + sec * batch + 20
        max_polls = int(sec / POLL_SECONDS * batch) + 11
        view = {}
        for poll in range(max_polls):
            if poll:
                time.sleep(POLL_SECONDS)
            view = yield from _inspect_station(ex, target, station_kind=kind)
            if not window_section(view, "input") and window_amount(view, "output", "iron_ingot") >= batch:
                break
            if time.monotonic() >= deadline:
                break
        else:
            raise Replan(f"观察熔炉 {pos} 超过轮询上限，冶炼 {batch} 个铁超时：{view}")
        if window_section(view, "input") or window_amount(view, "output", "iron_ingot") < batch:
            raise Replan(f"观察熔炉 {pos} 等待 {wait_limit}s 后铁未烧完，检查燃料/原料：{view}")
        result = yield from _collect_station(
            ex,
            state,
            target,
            item="iron_ingot",
            view=view,
            station_kind=kind,
            require_empty=True,
        )
        return result["view"]

    def _recover_placed_furnace(self, ex: "Executor", state: "State") -> Iterator[dict]:
        """抢占或重连后若粗铁与熔炉均已离手，先检查附近已放熔炉回收在途铁锭，避免重复下矿。"""
        dim = str((state.st or {}).get("dimension") or "overworld")
        valid_stations = stations_for_item("raw_iron")
        found = ex.client.find_blocks(types=list(valid_stations), radius=16, limit=8).get("blocks") or []
        for b in found:
            pos = b.get("pos")
            if not (isinstance(pos, list) and len(pos) == 3):
                continue
            block_name = bare(ex.client.get_block(pos).get("name"))
            if block_name not in valid_stations:
                continue
            yield rec("move_to", _bounded_move_to(ex, pos, rng=2, timeout=30.0), pos=pos)
            _record_station(state, block_name, pos, dimension=dim)
            self._station_kind = block_name
            target = {"pos": pos}
            view = yield from _inspect_station(ex, target, station_kind=block_name)
            in_raw = window_amount(view, "input", "raw_iron")
            out_ingot = window_amount(view, "output", "iron_ingot")
            if in_raw > 0:
                if not (view.get("info") or {}).get("burning") and not window_section(view, "fuel"):
                    yield from self._prepare_fuel(ex, state, in_raw)
                    fuel, fuel_count, _ = self._fuel_plan(state, in_raw)[0]
                    yield from load_furnace(ex, state, target, fuel_item=fuel, fuel_count=fuel_count, view=view)
                view = yield from self._poll_and_take(
                    ex, state, pos, target, in_raw + out_ingot, station_kind=block_name,
                )
                if view and window_amount(view, "fuel", "bucket") > 0:
                    yield from _collect_station(
                        ex, state, target, item="bucket", section="fuel", view=view,
                        station_kind=block_name, inspect_after=False, require_empty=False,
                    )
                return
            if out_ingot > 0 and not window_section(view, "input"):
                yield from _collect_station(
                    ex, state, target, item="iron_ingot", view=view,
                    station_kind=block_name, inspect_after=False,
                )
                if window_amount(view, "fuel", "bucket") > 0:
                    yield from _collect_station(
                        ex, state, target, item="bucket", section="fuel", view=view,
                        station_kind=block_name, inspect_after=False, require_empty=False,
                    )
                return

    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        state.invalidate_inventory()
        state.refresh_inventory(ex.client)
        start_ingots = state.count("iron_ingot")
        if self.args.get("async_lease"):
            dim = str((state.st or {}).get("dimension") or "overworld")
            book = lease_book_for(state)
            ready_iron = [
                lease for lease in book.ready_leases(dimension=dim)
                if lease.expected_output == "iron_ingot"
            ]
            for lease in ready_iron:
                yield from collect_smelt_lease(ex, state, lease)
                if state.count("iron_ingot") - start_ingots >= self.args["count"]:
                    return
            if state.count("iron_ingot") > start_ingots:
                return
        if (state.count("raw_iron") < 1 and state.count("raw_iron_block") < 1
                and not any(state.count(s) >= 1 for s in stations_for_item("raw_iron"))):
            yield from self._recover_placed_furnace(ex, state)
            if state.count("iron_ingot") - start_ingots >= self.args["count"]:
                return
        reason = self.blocked(state)
        if reason:
            if state.count("iron_ingot") > start_ingots:
                return
            raise Replan(f"检查背包后：{reason}")
        remaining_goal = max(1, self.args["count"] - (state.count("iron_ingot") - start_ingots))
        yield from self._collect(ex, state)
        count = min(remaining_goal, state.count("raw_iron"))
        # 有镐才顺路采煤，最多两块；没有则转用背包木材，不等待环境刷新。
        if (state.count("coal") + state.count("charcoal")) * 8 < count and best_pickaxe(state) in STONE_OR_BETTER_PICKS:
            for _ in range(2):
                coal = ex.client.find_blocks(types=["coal_ore", "deepslate_coal_ore"], radius=16, limit=12).get("blocks") or []
                cur = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
                nearby = [b["pos"] for b in coal if isinstance(b.get("pos"), list)
                          and len(b["pos"]) == 3 and not under_feet(cur, b["pos"])]
                if not nearby:
                    break
                yield from self._mine(ex, state, nearby[0], ("coal_ore", "deepslate_coal_ore"), "coal")
                if (state.count("coal") + state.count("charcoal")) * 8 >= count:
                    break
        yield from self._prepare_fuel(ex, state, count)
        if self.args.get("async_lease"):
            best = best_fuel_for(state, count)
            if best is not None:
                fuel, fuel_count = best
                batch = count
            else:
                fuel, fuel_count, batch = self._fuel_plan(state, count)[0]
            pos = yield from self._furnace(ex, state)
            station_kind = getattr(self, "_station_kind", "furnace")
            lease = yield from start_smelt_lease(
                ex,
                state,
                pos,
                input_item="raw_iron",
                count=batch,
                fuel_item=fuel,
                fuel_count=fuel_count,
                station_kind=station_kind,
                expected_output="iron_ingot",
                consumer="smelt_iron",
            )
            yield rec(
                "smelt_lease_started",
                {
                    "pos": list(lease.pos),
                    "station_kind": lease.station_kind,
                    "count": lease.count,
                    "ready_at": lease.ready_at,
                    "returns_empty_bucket": lease.returns_empty_bucket,
                },
            )
            return
        plan = self._fuel_plan(state, count)
        pos = yield from self._furnace(ex, state)
        target = {"pos": pos}
        station_kind = getattr(self, "_station_kind", "furnace")
        view = yield from _inspect_station(ex, target, station_kind=station_kind)
        # 每次只放当前批次原料，避免剩余燃烧时间提前吃掉下一批材料。
        for fuel, fuel_count, batch in plan:
            yield from load_furnace(
                ex, state, target, raw_item="raw_iron", raw_count=batch, fuel_item=fuel,
                fuel_count=fuel_count, view=view, replace_fuel=True, output_item="iron_ingot",
            )
            view = yield from self._poll_and_take(
                ex, state, pos, target, batch, station_kind=station_kind,
            )
            if fuel == "lava_bucket" and view and window_amount(view, "fuel", "bucket") > 0:
                bucket_res = yield from _collect_station(
                    ex, state, target, item="bucket", section="fuel", view=view,
                    station_kind=station_kind, inspect_after=False, require_empty=False,
                )
                if bucket_res.get("view") is not None:
                    view = bucket_res["view"]

    def verify_done(self, state: "State", before_counts: dict[str, int]) -> str:
        if state.count("iron_ingot") > before_counts.get("iron_ingot", 0):
            return ""
        if self.args.get("async_lease") and any(
            lease.expected_output == "iron_ingot" for lease in lease_book_for(state).active_leases()
        ):
            return ""
        return "smelt_iron 结束但背包铁锭未增加"
