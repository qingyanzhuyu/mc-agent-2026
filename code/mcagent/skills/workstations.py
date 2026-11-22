"""统一工作站台账记账、现场核验销账与四级安全回收辅助原语（L2，不注册到 REGISTRY）。

分级回收规则：
1. 野外用完即收（`TIER1_IMMEDIATE_RECLAIM`）：`crafting_table`、`smithing_table`、`jukebox`
   —— 仅限野外新放（`newly_placed=True` 且非 `camp` 基地设施）且不在保护区 → `ex.dig(..., collect=True)` + `remove_station`；
2. 有镐且清空才收（`TIER2_PICKAXE_RECLAIM`）：`furnace`、`blast_furnace`、`smoker`、`anvil`、`chipped_anvil`、`damaged_anvil`、`stonecutter`、`brewing_stand`、`crafter`、`grindstone`
   —— 须持有木镐及以上（防徒手挖碎销毁）；若为熔炼站还必须无活跃 `FurnaceLease` 且确认炉内已取空；
3. 高价值严苛工具回收（`TIER3_DIAMOND_PICK_RECLAIM`）：`enchanting_table`
   —— 须持有钻石镐或下界合金镐才允许回收，否则只记台账不拆；
4. 绝不自动拆除（`NEVER_AUTO_RECLAIM` + 全部 `camp` 基地设施 + 床位另走 30 格无怪策略）：
   `chest`、`trapped_chest`、`barrel`、`ender_chest`、`campfire`、`soul_campfire`、`respawn_anchor`、`beacon`。
"""
from __future__ import annotations

import math
from collections.abc import Collection, Generator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Optional

from ..client import ApiError
from ..runtime import Replan
from .base import failure, rec
from .furnace_lease import lease_book_for
from .items import bare, best_pickaxe
from .sightline import DIG_EYE_REACH, block_cell, eye_to_block_dist
from .voxel_cache import voxel_cache_for
from .world_memo import BED_BLOCKS, CATEGORY_BLOCKS, memo_for


def _state_snap(state: Any) -> dict:
    snap = getattr(state, "snapshot", None)
    if callable(snap):
        data = snap()
        if isinstance(data, Mapping):
            return dict(data)
    st = getattr(state, "st", None)
    return dict(st) if isinstance(st, Mapping) else {}


def _is_protected_cell(cell: tuple[int, int, int]) -> bool:
    try:
        from .camp_plan import is_protected_cell
        return bool(is_protected_cell(cell))
    except Exception:
        return False


def _record_protected_cell(cell: tuple[int, int, int], memo: Any, dim: str) -> None:
    try:
        from .camp_plan import record_protected_cell
        record_protected_cell(cell, memo, dim)
    except Exception:
        pass

if TYPE_CHECKING:
    from ..runtime import Executor, State

STATION_BLOCKS: frozenset[str] = frozenset(CATEGORY_BLOCKS.get("station", ())) | frozenset(BED_BLOCKS)

TIER1_IMMEDIATE_RECLAIM: frozenset[str] = frozenset({
    "crafting_table", "smithing_table", "jukebox",
})
TIER2_PICKAXE_RECLAIM: frozenset[str] = frozenset({
    "furnace", "blast_furnace", "smoker",
    "anvil", "chipped_anvil", "damaged_anvil",
    "stonecutter", "brewing_stand", "crafter", "grindstone",
})
TIER3_DIAMOND_PICK_RECLAIM: frozenset[str] = frozenset({
    "enchanting_table",
})
NEVER_AUTO_RECLAIM: frozenset[str] = frozenset({
    "chest", "trapped_chest", "barrel", "ender_chest",
    "campfire", "soul_campfire", "respawn_anchor", "beacon",
})
_SMELTING_STATIONS: frozenset[str] = frozenset({"furnace", "blast_furnace", "smoker"})
_DIAMOND_OR_BETTER_PICKS: tuple[str, ...] = ("netherite_pickaxe", "diamond_pickaxe")


def is_station_block(name: Any) -> bool:
    b = bare(name).strip().lower()
    if not b:
        return False
    return b in STATION_BLOCKS or (len(b) > 4 and b.endswith("_bed"))


def _norm_dim(state: Any, dimension: Optional[str]) -> str:
    if dimension:
        return str(dimension).removeprefix("minecraft:").strip().lower() or "overworld"
    raw = _state_snap(state).get("dimension") or "overworld"
    return str(raw).removeprefix("minecraft:").strip().lower() or "overworld"


def record_placed_station(
    state: "State",
    item: Any,
    pos: Any,
    *,
    dimension: Optional[str] = None,
    scope: Optional[str] = None,
) -> bool:
    """若放置的方块属于工作站/功能块，自动登记至 `WorldResourceMemo` 并标注 camp/field 作用域。"""
    kind = bare(item).strip().lower()
    if not is_station_block(kind):
        return False
    dim = _norm_dim(state, dimension)
    try:
        cell = block_cell(pos)
        memo_for(state).record_station(kind, cell, dimension=dim, scope=scope)
        return True
    except Exception:
        return False


def verify_or_prune_station(
    ex: "Executor",
    state: "State",
    kind: str,
    pos: Any,
    *,
    dimension: Optional[str] = None,
    expected_names: Optional[Collection[str]] = None,
) -> bool:
    """现场核验台账工作站是否仍在原地；若已消失或被替换为其他方块则立即从台账销账。"""
    dim = _norm_dim(state, dimension)
    try:
        cell = block_cell(pos)
    except Exception:
        return False
    k = bare(kind).strip().lower()
    allowed = {bare(n).strip().lower() for n in expected_names} if expected_names else {k}
    if k == "furnace" and not expected_names:
        allowed |= {"blast_furnace", "smoker"}
    elif k == "anvil" and not expected_names:
        allowed |= {"chipped_anvil", "damaged_anvil"}
    elif k == "bed" and not expected_names:
        allowed |= set(BED_BLOCKS)
    elif k == "chest" and not expected_names:
        allowed |= {"trapped_chest", "barrel"}

    try:
        blk = ex.client.get_block(list(cell))
    except ApiError as exc:
        if exc.code in ("not_loaded", "not_ready"):
            return False
        raise
    bname = bare(blk.get("name")).strip().lower() if isinstance(blk, Mapping) else ""
    if bname and (bname in allowed or (k == "bed" and bname.endswith("_bed"))):
        return True
    # 方块已不在原位（被拆/被苦力怕炸毁/过期脏数据）-> 立即销账清理
    memo_for(state).remove_station(k, cell, dimension=dim)
    voxel_cache_for(state).invalidate(cell, dimension=dim)
    return False


def find_known_station(
    ex: "Executor",
    state: "State",
    kind: str,
    *,
    dimension: Optional[str] = None,
    max_dist: float = 32.0,
    expected_names: Optional[Collection[str]] = None,
    verify: bool = True,
) -> Optional[tuple[int, int, int]]:
    """按距离从近到远查询 `WorldResourceMemo` 中同维度可用工作站；不可用旧记录自动销账。"""
    dim = _norm_dim(state, dimension)
    memo = memo_for(state)
    cands = memo.stations(kind, dimension=dim)
    if not cands:
        return None
    st = _state_snap(state)
    pos = st.get("pos")
    origin: Optional[tuple[float, float, float]] = None
    if isinstance(pos, Sequence) and not isinstance(pos, (str, bytes)) and len(pos) >= 3:
        try:
            origin = (float(pos[0]), float(pos[1]), float(pos[2]))
        except (TypeError, ValueError):
            origin = None
    ranked: list[tuple[float, tuple[int, int, int]]] = []
    for cell in cands:
        if memo.is_blocked(cell, dimension=dim, category="station"):
            continue
        dist = math.dist(origin, (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)) if origin is not None else 0.0
        if origin is not None and dist > max_dist:
            continue
        ranked.append((dist, cell))
    ranked.sort(key=lambda item: item[0])
    for _, cell in ranked:
        if not verify or verify_or_prune_station(ex, state, kind, cell, dimension=dim, expected_names=expected_names):
            return cell
    return None


def can_reclaim_station(
    state: "State",
    kind: str,
    pos: Any,
    *,
    dimension: Optional[str] = None,
    newly_placed: bool = False,
    furnace_emptied: bool = True,
) -> tuple[bool, str, Optional[str]]:
    """按四级回收规则校验是否允许自动拆除回收，返回 `(可回收, 原因, 需装备的镐名或None)`。"""
    if not newly_placed:
        return False, "复用已有工作站不自动拆除", None
    k = bare(kind).strip().lower()
    if not k or k in NEVER_AUTO_RECLAIM or k.endswith("_bed") or k == "bed":
        return False, f"{k} 属于禁止自动拆除类型", None
    dim = _norm_dim(state, dimension)
    try:
        cell = block_cell(pos)
    except Exception:
        return False, "坐标无效", None
    if _is_protected_cell(cell):
        return False, "位于保护区内禁止拆除", None
    if memo_for(state).is_camp_station(k, cell, dimension=dim):
        return False, "基地常驻工作站禁止自动拆除", None

    if k in TIER1_IMMEDIATE_RECLAIM:
        return True, "tier1_immediate", None

    if k in TIER2_PICKAXE_RECLAIM:
        pick = best_pickaxe(state)
        if not pick:
            return False, f"无镐禁止徒手挖碎 {k}", None
        if k in _SMELTING_STATIONS:
            if not furnace_emptied:
                return False, f"{k} 炉内未清空暂不回收", None
            if lease_book_for(state).get_lease(cell, dimension=dim) is not None:
                return False, f"{k} 存在活跃熔炉租约暂不回收", None
        return True, "tier2_pickaxe", pick

    if k in TIER3_DIAMOND_PICK_RECLAIM:
        pick = next((p for p in _DIAMOND_OR_BETTER_PICKS if state.count(p) >= 1), None)
        if not pick:
            return False, f"无钻石/下界合金镐禁止拆除 {k}", None
        return True, "tier3_diamond_pick", pick

    return False, f"{k} 未纳入自动回收白名单，保留在台账中", None


def reclaim_field_station(
    ex: "Executor",
    state: "State",
    kind: str,
    pos: Any,
    *,
    dimension: Optional[str] = None,
    newly_placed: bool = False,
    furnace_emptied: bool = True,
) -> Generator[dict, None, bool]:
    """L2 原语：满足分级门禁时原地（或近距）回收野外临时工作站并从 `WorldResourceMemo` 销账。

    严格遵守 1 阻塞动作 = 1 `yield`；任何可恢复失败均安全降级返回 `False`（保留台账，不阻断已完成的主技能）。
    """
    allowed, _why, req_pick = can_reclaim_station(
        state, kind, pos, dimension=dimension, newly_placed=newly_placed, furnace_emptied=furnace_emptied,
    )
    if not allowed:
        return False

    dim = _norm_dim(state, dimension)
    k = bare(kind).strip().lower()
    cell = block_cell(pos)

    st = _state_snap(state)
    if st.get("alive") is False:
        return False
    # 若正在受近身敌怪威胁，直接放弃回收留给台账复用，安全第一
    nh = st.get("nearest_hostile")
    if isinstance(nh, Mapping):
        raw_d = nh.get("dist")
        if isinstance(raw_d, (int, float)) and not isinstance(raw_d, bool) and math.isfinite(float(raw_d)) and float(raw_d) < 8.0:
            return False

    try:
        if eye_to_block_dist(st, cell) > DIG_EYE_REACH:
            return False
    except Exception:
        return False

    if req_pick:
        held = bare((((getattr(state, "inventory", None) or {}).get("held") or {}).get("name")) or ((st.get("held") or {}).get("name")))
        if held != req_pick and hasattr(ex, "equip"):
            try:
                res_eq = ex.equip(req_pick, slot="hand")
                yield rec("equip", res_eq, item=req_pick, slot="hand", reason="reclaim_station")
            except (Replan, ApiError) as exc:
                code = failure(exc)[0]
                if code in ("interrupted", "dead", "not_connected", "death_loop_cooldown") or "抢占" in str(exc):
                    raise
                return False

    try:
        res_dig = ex.dig(list(cell), collect=True)
    except (Replan, ApiError) as exc:
        code = failure(exc)[0]
        if code == "protected_region":
            _record_protected_cell(cell, memo_for(state), dim)
        if code in ("interrupted", "dead", "not_connected", "death_loop_cooldown") or "抢占" in str(exc):
            raise
        return False

    voxel_cache_for(state).invalidate(cell, dimension=dim)
    memo_for(state).remove_station(k, cell, dimension=dim)
    if getattr(ex, "client", None) is not None:
        state.refresh_inventory(ex.client)
    yield rec("dig", res_dig, pos=list(cell), collect=True, item=k, reason="reclaim_station")
    return True
