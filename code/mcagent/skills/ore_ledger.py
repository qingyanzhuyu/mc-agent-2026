"""全矿种台账记账、镐级门禁判定、顺手采矿与升级镐后回访 L2 原语（不注册到 REGISTRY）。

核心闭环：
1. 任何扫描发现的矿石无论当前镐级是否足够，均统一写入 `WorldResourceMemo`（按维度与矿类分类，随 checkpoint 持久化）；
2. 若当前镐级足够且安全触手可及（非脚下、无岩浆/水邻接、无贴脸敌怪），顺手装备合规镐采下并标记 `depleted`；
3. 若当前镐级不足（挖不动，如木镐见铁矿、石镐见钻石/金/红石/绿宝石矿、铁镐见远古残骸），绝不挖碎，保留在台账中；
4. 后续升级镐后，通过 `query_harvestable_memo_ores()` 查回台账中已满足镐级门禁的矿点导航复采，缺失/已被挖走则同步标记 `depleted`。
"""
from __future__ import annotations

import math
from collections.abc import Collection, Generator, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Optional

from ..client import ApiError
from ..runtime import Replan
from .base import failure, rec
from .items import PICKAXE_ORDER, STONE_OR_BETTER_PICKS, bare, block_name
from .mining import under_feet
from .sightline import DIG_EYE_REACH, block_cell, can_reach_block, eye_to_block_dist
from .voxel_cache import client_get_blocks_batch, voxel_cache_for
from .world_memo import CATEGORY_BLOCKS, memo_for

if TYPE_CHECKING:
    from ..runtime import Executor, State

Cell = tuple[int, int, int]

WOOD_OR_BETTER_PICKS: tuple[str, ...] = (
    "netherite_pickaxe", "diamond_pickaxe", "iron_pickaxe",
    "copper_pickaxe", "stone_pickaxe", "golden_pickaxe", "wooden_pickaxe",
)
STONE_OR_BETTER_PICK_ORDER: tuple[str, ...] = (
    "netherite_pickaxe", "diamond_pickaxe", "iron_pickaxe", "copper_pickaxe", "stone_pickaxe",
)
IRON_OR_BETTER_PICK_ORDER: tuple[str, ...] = (
    "netherite_pickaxe", "diamond_pickaxe", "iron_pickaxe",
)
DIAMOND_OR_BETTER_PICK_ORDER: tuple[str, ...] = (
    "netherite_pickaxe", "diamond_pickaxe",
)

ORE_CATEGORIES: tuple[str, ...] = (
    "diamond_ore", "ancient_debris", "emerald_ore", "gold_ore",
    "iron_ore", "lapis_ore", "redstone_ore", "coal_ore", "copper_ore", "quartz_ore",
)

OVERWORLD_ALL_ORES: tuple[str, ...] = (
    "diamond_ore", "deepslate_diamond_ore",
    "emerald_ore", "deepslate_emerald_ore",
    "gold_ore", "deepslate_gold_ore", "raw_gold_block",
    "iron_ore", "deepslate_iron_ore", "raw_iron_block",
    "lapis_ore", "deepslate_lapis_ore",
    "redstone_ore", "deepslate_redstone_ore",
    "coal_ore", "deepslate_coal_ore",
    "copper_ore", "deepslate_copper_ore", "raw_copper_block",
)
NETHER_ALL_ORES: tuple[str, ...] = (
    "ancient_debris", "nether_gold_ore", "nether_quartz_ore",
)

ORE_MIN_PICKS: dict[str, tuple[str, ...]] = {
    "coal_ore": WOOD_OR_BETTER_PICKS,
    "deepslate_coal_ore": WOOD_OR_BETTER_PICKS,
    "copper_ore": WOOD_OR_BETTER_PICKS,
    "deepslate_copper_ore": WOOD_OR_BETTER_PICKS,
    "raw_copper_block": WOOD_OR_BETTER_PICKS,
    "nether_quartz_ore": WOOD_OR_BETTER_PICKS,
    "nether_gold_ore": WOOD_OR_BETTER_PICKS,
    "iron_ore": STONE_OR_BETTER_PICK_ORDER,
    "deepslate_iron_ore": STONE_OR_BETTER_PICK_ORDER,
    "raw_iron_block": STONE_OR_BETTER_PICK_ORDER,
    "lapis_ore": STONE_OR_BETTER_PICK_ORDER,
    "deepslate_lapis_ore": STONE_OR_BETTER_PICK_ORDER,
    "gold_ore": IRON_OR_BETTER_PICK_ORDER,
    "deepslate_gold_ore": IRON_OR_BETTER_PICK_ORDER,
    "raw_gold_block": IRON_OR_BETTER_PICK_ORDER,
    "redstone_ore": IRON_OR_BETTER_PICK_ORDER,
    "deepslate_redstone_ore": IRON_OR_BETTER_PICK_ORDER,
    "diamond_ore": IRON_OR_BETTER_PICK_ORDER,
    "deepslate_diamond_ore": IRON_OR_BETTER_PICK_ORDER,
    "emerald_ore": IRON_OR_BETTER_PICK_ORDER,
    "deepslate_emerald_ore": IRON_OR_BETTER_PICK_ORDER,
    "ancient_debris": DIAMOND_OR_BETTER_PICK_ORDER,
}

_ORE_PRIORITY: dict[str, int] = {
    "diamond_ore": 0, "deepslate_diamond_ore": 0,
    "ancient_debris": 0,
    "emerald_ore": 1, "deepslate_emerald_ore": 1,
    "gold_ore": 2, "deepslate_gold_ore": 2, "raw_gold_block": 2, "nether_gold_ore": 2,
    "iron_ore": 3, "deepslate_iron_ore": 3, "raw_iron_block": 3,
    "lapis_ore": 4, "deepslate_lapis_ore": 4,
    "redstone_ore": 5, "deepslate_redstone_ore": 5,
    "coal_ore": 6, "deepslate_coal_ore": 6,
    "copper_ore": 7, "deepslate_copper_ore": 7, "raw_copper_block": 7,
    "nether_quartz_ore": 7,
}

_BLOCK_TO_ORE_CAT: dict[str, str] = {
    b: cat for cat in ORE_CATEGORIES for b in CATEGORY_BLOCKS.get(cat, ())
}
_HAZARD_FLUIDS: frozenset[str] = frozenset({"water", "flowing_water", "lava", "flowing_lava", "bubble_column"})
_FACES: tuple[Cell, ...] = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
_RECOVERABLE_DIG_CODES: frozenset[str] = frozenset({
    "out_of_reach", "dig_unconfirmed", "protected_region", "not_loaded",
    "no_los", "los_blocked", "cannot_dig", "timeout", "stalled", "replan",
})


def is_ore_block(name: Any) -> bool:
    return bare(name).strip().lower() in _BLOCK_TO_ORE_CAT


def ore_category(name: Any) -> Optional[str]:
    return _BLOCK_TO_ORE_CAT.get(bare(name).strip().lower())


def ores_for_dimension(dimension: str = "overworld") -> tuple[str, ...]:
    dim = bare(dimension).strip().lower() or "overworld"
    if "nether" in dim:
        return NETHER_ALL_ORES
    if "end" in dim:
        return ()
    return OVERWORLD_ALL_ORES


def required_picks_for_ore(block_name_raw: Any) -> tuple[str, ...]:
    b = bare(block_name_raw).strip().lower()
    return ORE_MIN_PICKS.get(b, ())


def _has_usable_pick(state: "State", pick: str) -> bool:
    if state.count(pick) < 1:
        return False
    inv = getattr(state, "inventory", None) or {}
    held = inv.get("held") if isinstance(inv, Mapping) else None
    items = inv.get("items") if isinstance(inv, Mapping) else ()
    entries = [e for e in (held, *(items or ())) if isinstance(e, Mapping) and bare(e.get("name")) == pick]
    if not entries:
        return True
    for entry in entries:
        dur = entry.get("durability")
        if isinstance(dur, (list, tuple)) and len(dur) == 2 and isinstance(dur[0], int) and not isinstance(dur[0], bool):
            if dur[0] > 0:
                return True
        else:
            return True
    return False


def pickaxe_for_ore(state: "State", block_name_raw: Any) -> Optional[str]:
    """返回背包中可合规挖掘该矿石且不会挖碎的最优镐；镐级不足（挖不动）返回 `None`。"""
    allowed = required_picks_for_ore(block_name_raw)
    if not allowed:
        return None
    for pick in allowed:
        if _has_usable_pick(state, pick):
            return pick
    return None


def can_mine_ore(state: "State", block_name_raw: Any) -> bool:
    return pickaxe_for_ore(state, block_name_raw) is not None


def _state_snap(state: Any) -> dict:
    snap = getattr(state, "snapshot", None)
    if callable(snap):
        data = snap()
        if isinstance(data, Mapping):
            return dict(data)
    st = getattr(state, "st", None)
    return dict(st) if isinstance(st, Mapping) else {}


def _norm_dim(state: Any, dimension: Optional[str]) -> str:
    if dimension:
        return bare(dimension).strip().lower() or "overworld"
    return bare(_state_snap(state).get("dimension") or "overworld").strip().lower() or "overworld"


def record_ore_observations(
    state: "State",
    blocks: Iterable[Mapping[str, Any]],
    *,
    dimension: Optional[str] = None,
    origin: Any = None,
) -> int:
    """将扫描见到的所有矿石（含当前镐级挖不动的矿）统一写入 `WorldResourceMemo` 台账。"""
    dim = _norm_dim(state, dimension)
    memo = memo_for(state)
    stance = origin if origin is not None else _state_snap(state)
    added = 0
    for entry in blocks or ():
        if not isinstance(entry, Mapping):
            continue
        bname = bare(entry.get("name")).strip().lower()
        cat = _BLOCK_TO_ORE_CAT.get(bname)
        raw_pos = entry.get("pos")
        if not cat or not isinstance(raw_pos, (list, tuple)) or len(raw_pos) < 3:
            continue
        try:
            cell = block_cell(raw_pos)
            dist = entry.get("dist")
            if not (isinstance(dist, (int, float)) and not isinstance(dist, bool) and math.isfinite(float(dist))):
                try:
                    dist = eye_to_block_dist(stance, cell)
                except Exception:
                    dist = None
            memo.record_seen(cat, bname, cell, dimension=dim, dist=dist)
            added += 1
        except Exception:
            continue
    return added


def query_harvestable_memo_ores(
    state: "State",
    categories: Collection[str],
    *,
    dimension: Optional[str] = None,
    max_dist: float = 48.0,
    excluded: Collection[Sequence[int]] = (),
    origin: Any = None,
) -> list[tuple[str, Cell]]:
    """查询台账中此前记下、且当前镐级已足够开采的矿石坐标，按距离从近到远返回 `[(block_name, cell), ...]`。"""
    dim = _norm_dim(state, dimension)
    memo = memo_for(state)
    stance = origin if origin is not None else _state_snap(state)
    raw_pos = stance.get("pos") if isinstance(stance, Mapping) else stance
    pos3: Optional[tuple[float, float, float]] = None
    if isinstance(raw_pos, Sequence) and not isinstance(raw_pos, (str, bytes)) and len(raw_pos) >= 3:
        try:
            pos3 = (float(raw_pos[0]), float(raw_pos[1]), float(raw_pos[2]))
        except (TypeError, ValueError):
            pos3 = None
    excl_set = {(int(c[0]), int(c[1]), int(c[2])) for c in excluded if isinstance(c, Sequence) and len(c) >= 3}
    ranked: list[tuple[float, str, Cell]] = []
    seen_cells: set[Cell] = set()
    for cat in categories:
        for entry in memo.query(cat, dimension=dim, origin=stance):
            if entry.status in ("depleted", "protected", "unreachable"):
                continue
            cell = entry.pos
            if cell in seen_cells or cell in excl_set:
                continue
            bname = bare(entry.block).strip().lower()
            if not bname:
                defaults = CATEGORY_BLOCKS.get(cat, ())
                bname = defaults[0] if defaults else ""
            if not bname or not can_mine_ore(state, bname):
                continue
            dist = math.dist(pos3, (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)) if pos3 is not None else 0.0
            if pos3 is not None and dist > float(max_dist):
                continue
            seen_cells.add(cell)
            ranked.append((dist, bname, cell))
    ranked.sort(key=lambda row: row[0])
    return [(bname, cell) for _, bname, cell in ranked]


def verify_or_deplete_memo_ore(
    ex: "Executor",
    state: "State",
    pos: Sequence[int],
    *,
    expected_names: Collection[str],
    category: str,
    dimension: Optional[str] = None,
) -> bool:
    """回访台账矿点时核验矿块是否仍在；若已被挖走或消失则立即标记 `depleted`。"""
    dim = _norm_dim(state, dimension)
    cell = block_cell(pos)
    allowed = {bare(n).strip().lower() for n in expected_names}
    try:
        actual = block_name(ex, list(cell))
    except ApiError as exc:
        if exc.code in ("not_loaded", "not_ready"):
            return False
        raise
    if actual in allowed:
        return True
    memo_for(state).mark_depleted(cell, dimension=dim, category=category)
    voxel_cache_for(state).invalidate(list(cell), dimension=dim)
    return False


def _is_safe_to_dig_ore(ex: "Executor", cell: Cell) -> bool:
    x, y, z = cell
    nbs = [(x + dx, y + dy, z + dz) for dx, dy, dz in _FACES]
    blocks = client_get_blocks_batch(getattr(ex, "client", None), nbs) if getattr(ex, "client", None) is not None else {}
    return not any(bare((blocks.get(nb) or {}).get("name")) in _HAZARD_FLUIDS for nb in nbs)


def mine_reachable_ores_and_record(
    ex: "Executor",
    state: "State",
    *,
    blocks: Optional[Iterable[Mapping[str, Any]]] = None,
    scan_radius: int = 8,
    max_mine: int = 4,
    dimension: Optional[str] = None,
    excluded: Collection[Sequence[int]] = (),
    target_ores: Collection[str] = (),
    skip_target_ores: bool = True,
    allow_scan: bool = False,
) -> Generator[dict, None, int]:
    """L2 原语：全矿种统一入账；对当前镐级够且安全触手可及的矿顺手先挖，挖不动的保留台账待升级镐后回采。

    严格遵守 1 阻塞动作 = 1 `yield`；任何可恢复挖掘失败均降级跳过，不阻断主流程。
    """
    st = _state_snap(state)
    if st.get("alive") is False:
        return 0
    dim = _norm_dim(state, dimension)
    cur_pos = st.get("pos") or getattr(state, "pos", None)
    stance = st if st.get("pos") is not None else cur_pos

    raw_rows: list[Mapping[str, Any]] = list(blocks) if blocks is not None else []
    if blocks is None and allow_scan:
        ore_types = ores_for_dimension(dim)
        find_fn = getattr(getattr(ex, "client", None), "find_blocks", None)
        if ore_types and callable(find_fn):
            try:
                found = find_fn(types=list(ore_types), radius=max(1, int(scan_radius)), limit=24) or {}
            except ApiError as exc:
                if exc.code in ("dead", "not_connected", "wrong_server", "death_loop_cooldown"):
                    raise
                found = {}
            if isinstance(found, Mapping):
                raw_rows = [b for b in (found.get("blocks") or ()) if isinstance(b, Mapping)]
                voxel_cache_for(state).ingest_find_blocks(found, dimension=dim, client=ex.client)

    if not raw_rows:
        return 0

    # 无条件把见到的所有矿（包括当前镐级挖不动的矿）全部记入台账
    record_ore_observations(state, raw_rows, dimension=dim, origin=stance)

    if max_mine <= 0 or any(st.get(flag) for flag in ("in_water", "submerged", "in_lava", "on_fire")):
        return 0
    nh = st.get("nearest_hostile")
    if isinstance(nh, Mapping):
        raw_d = nh.get("dist")
        if isinstance(raw_d, (int, float)) and not isinstance(raw_d, bool) and math.isfinite(float(raw_d)) and float(raw_d) < 8.0:
            return 0

    skip_set = {bare(t).strip().lower() for t in target_ores} if skip_target_ores else set()
    excl_set = {(int(c[0]), int(c[1]), int(c[2])) for c in excluded if isinstance(c, Sequence) and len(c) >= 3}
    memo = memo_for(state)
    vc = voxel_cache_for(state)
    cur_cell = block_cell(cur_pos) if isinstance(cur_pos, Sequence) and len(cur_pos) >= 3 else None

    candidates: list[tuple[int, float, str, Cell]] = []
    seen_cells: set[Cell] = set()
    for row in raw_rows:
        bname = bare(row.get("name")).strip().lower()
        raw_pos = row.get("pos")
        if not bname or bname not in _BLOCK_TO_ORE_CAT or bname in skip_set:
            continue
        if not isinstance(raw_pos, (list, tuple)) or len(raw_pos) < 3:
            continue
        cell = block_cell(raw_pos)
        if cell in seen_cells or cell in excl_set:
            continue
        seen_cells.add(cell)
        if cur_pos is not None and under_feet(cur_pos, cell):
            continue
        if cur_cell is not None and vc.is_unreachable("ore_ledger", cell, dimension=dim, stance_cell=cur_cell):
            continue
        if memo.is_blocked(cell, dimension=dim, category=_BLOCK_TO_ORE_CAT[bname]):
            continue
        # 镐级不足（挖不动）：不挖，留在台账里等升级镐后再来挖！
        if pickaxe_for_ore(state, bname) is None:
            continue
        if stance is None or not can_reach_block(ex, stance, list(cell), reach=DIG_EYE_REACH):
            continue
        candidates.append((_ORE_PRIORITY.get(bname, 9), eye_to_block_dist(stance, cell), bname, cell))

    if not candidates:
        return 0

    candidates.sort(key=lambda item: (item[0], item[1]))
    mined = 0
    for _, _, bname, cell in candidates:
        if mined >= max_mine:
            break
        pick = pickaxe_for_ore(state, bname)
        if pick is None:
            continue
        st_now = state.refresh_status(ex.client, max_age_s=0) if getattr(ex, "client", None) is not None else _state_snap(state)
        if st_now.get("alive") is False:
            break
        pos_now = st_now.get("pos") or cur_pos
        stance_now = st_now if st_now.get("pos") is not None else pos_now
        if pos_now is not None and under_feet(pos_now, cell):
            continue
        if stance_now is None or not can_reach_block(ex, stance_now, list(cell), reach=DIG_EYE_REACH):
            continue
        if not _is_safe_to_dig_ore(ex, cell):
            continue
        actual_name = block_name(ex, list(cell))
        cat = _BLOCK_TO_ORE_CAT[bname]
        if actual_name != bname:
            if actual_name not in _BLOCK_TO_ORE_CAT:
                memo.mark_depleted(cell, dimension=dim, category=cat)
            continue

        held = bare((((getattr(state, "inventory", None) or {}).get("held") or {}).get("name")) or ((st_now.get("held") or {}).get("name")))
        if held != pick and hasattr(ex, "equip"):
            try:
                res_eq = ex.equip(pick, slot="hand")
                yield rec("equip", res_eq, item=pick, slot="hand", reason="opportunistic_ore")
            except (Replan, ApiError) as exc:
                code = failure(exc)[0]
                if code in ("interrupted", "dead", "not_connected", "death_loop_cooldown") or "抢占" in str(exc):
                    raise
                continue
        try:
            res_dig = ex.dig(list(cell), collect=True)
        except (Replan, ApiError) as exc:
            code = failure(exc)[0]
            if code in ("interrupted", "dead", "not_connected", "death_loop_cooldown") or "抢占" in str(exc):
                raise
            if code == "protected_region":
                memo.mark_protected(cell, dimension=dim, category=cat, block=bname)
            elif code in _RECOVERABLE_DIG_CODES and cur_cell is not None:
                vc.mark_unreachable("ore_ledger", cell, dimension=dim, stance_cell=cur_cell)
            continue

        vc.invalidate(list(cell), dimension=dim)
        memo.mark_depleted(cell, dimension=dim, category=cat)
        if getattr(ex, "client", None) is not None:
            state.refresh_inventory(ex.client)
        yield rec("dig", res_dig, pos=list(cell), collect=True, item=bname, reason="opportunistic_ore")
        mined += 1

    return mined
