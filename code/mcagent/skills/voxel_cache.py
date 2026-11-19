"""短时程体素观测缓存与目标级不可达黑名单（按 State 隔离）。

职责：
1. 吸收 `find_blocks` 返回的方块快照，供后续 `get_block` / 树干下探 / 视线清理直接命中，减少重复 RPC；
2. 自动对齐 `client._block_cache_gen`（由 `Executor` 在 `dig`/`place`/`interact`/`use_item` 后递增），
   任何写世界动作都会立刻使旧体素缓存失效；
3. 维护带 TTL 的目标级不可达黑名单（支持绑定站位格，玩家移动 >= `min_dist` 后自动清空站位黑名单）。
"""
from __future__ import annotations

import copy
import math
import threading
import time
import weakref
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from .items import bare

Cell = tuple[int, int, int]
_MAX_CACHE_ENTRIES = 2048


def _norm_dim(dimension: Any) -> str:
    return str(dimension or "overworld").strip() or "overworld"


def _norm_cell(pos: Any) -> Optional[Cell]:
    if not isinstance(pos, Sequence) or isinstance(pos, (str, bytes)) or len(pos) < 3:
        return None
    out: list[int] = []
    for axis in pos[:3]:
        if isinstance(axis, bool) or not isinstance(axis, (int, float)):
            return None
        fval = float(axis)
        if not math.isfinite(fval):
            return None
        out.append(math.floor(fval))
    return (out[0], out[1], out[2])


def _norm_point3(pos: Any) -> Optional[tuple[float, float, float]]:
    if not isinstance(pos, Sequence) or isinstance(pos, (str, bytes)) or len(pos) < 3:
        return None
    out: list[float] = []
    for axis in pos[:3]:
        if isinstance(axis, bool) or not isinstance(axis, (int, float)):
            return None
        fval = float(axis)
        if not math.isfinite(fval):
            return None
        out.append(fval)
    return (out[0], out[1], out[2])


class VoxelCache:
    """线程安全的单会话体素缓存与不可达黑名单。"""

    def __init__(self, *, default_ttl_s: float = 15.0, clock=time.monotonic) -> None:
        self._default_ttl_s = max(0.0, float(default_ttl_s))
        self._clock = clock
        self._lock = threading.RLock()
        # key: (dim, bx, by, bz) -> (expires_at, gen_tag_or_None, block_dict)
        self._blocks: dict[tuple[str, int, int, int], tuple[float, Optional[int], dict]] = {}
        self._last_client_gen: Optional[int] = None
        # key: (skill_name, dim, bx, by, bz) -> (expires_at, stance_cell_or_None)
        self._blacklist: dict[tuple[str, str, int, int, int], tuple[float, Optional[Cell]]] = {}
        self._hits: int = 0
        self._misses: int = 0
        self._ingested: int = 0

    def _sync_client_gen_locked(self, client: Any) -> int:
        gen = int(getattr(client, "_block_cache_gen", 0) or 0)
        if self._last_client_gen is None:
            self._last_client_gen = gen
            # 将此前尚未绑定 gen 的 ingest 条目标记为当前代次，后续代次变化时可一并失效
            for key, (exp, tag, blk) in list(self._blocks.items()):
                if tag is None:
                    self._blocks[key] = (exp, gen, blk)
        elif gen != self._last_client_gen:
            # 丢弃所有旧代次缓存；保留代次变化后新 ingest 的未绑定条目并绑定至新代次
            kept: dict[tuple[str, int, int, int], tuple[float, Optional[int], dict]] = {}
            for key, (exp, tag, blk) in self._blocks.items():
                if tag is None:
                    kept[key] = (exp, gen, blk)
            self._blocks = kept
            self._last_client_gen = gen
        return gen

    def _evict_if_needed_locked(self, now: float) -> None:
        if len(self._blocks) <= _MAX_CACHE_ENTRIES:
            return
        expired = [k for k, (exp, _, _) in self._blocks.items() if now >= exp]
        for k in expired:
            self._blocks.pop(k, None)
        while len(self._blocks) > _MAX_CACHE_ENTRIES:
            self._blocks.pop(next(iter(self._blocks)))

    def ingest_find_blocks(
        self,
        found: dict,
        *,
        dimension: str = "overworld",
        ttl_s: Optional[float] = None,
        client: Any = None,
    ) -> int:
        """吸收 `/find_blocks` 返回结果（兼容列表形态与摘要字典形态），返回入库条数。"""
        if not isinstance(found, Mapping):
            return 0
        raw_blocks = found.get("blocks")
        if not raw_blocks:
            return 0

        entries: list[dict] = []
        if isinstance(raw_blocks, Mapping):
            for name, summary in raw_blocks.items():
                if isinstance(summary, Mapping) and summary.get("nearest") is not None:
                    entries.append({"name": name, "pos": summary.get("nearest"), "dist": summary.get("dist")})
        elif isinstance(raw_blocks, Sequence) and not isinstance(raw_blocks, (str, bytes)):
            entries = [e for e in raw_blocks if isinstance(e, Mapping)]

        dim = _norm_dim(dimension)
        eff_ttl = self._default_ttl_s if ttl_s is None else max(0.0, float(ttl_s))
        if eff_ttl <= 0:
            return 0

        now = self._clock()
        expires_at = now + eff_ttl
        count = 0
        with self._lock:
            gen_tag: Optional[int] = self._sync_client_gen_locked(client) if client is not None else None
            for item in entries:
                cell = _norm_cell(item.get("pos"))
                name = bare(item.get("name"))
                if cell is None or not name:
                    continue
                payload = dict(item)
                payload["name"] = name
                payload["pos"] = list(cell)
                self._blocks[(dim, cell[0], cell[1], cell[2])] = (expires_at, gen_tag, copy.deepcopy(payload))
                count += 1
            if count:
                self._ingested += count
                self._evict_if_needed_locked(now)
        return count

    def get_block(
        self,
        client: Any,
        pos: Any,
        *,
        dimension: str = "overworld",
        ttl_s: float = 15.0,
        force: bool = False,
    ) -> dict:
        """优先命中本地缓存（自动随 `client._block_cache_gen` 失效），未命中回退 `client.get_block`。"""
        cell = _norm_cell(pos)
        if cell is None:
            return client.get_block(pos)

        dim = _norm_dim(dimension)
        key = (dim, cell[0], cell[1], cell[2])
        eff_ttl = max(0.0, float(ttl_s))
        now = self._clock()

        with self._lock:
            gen_before = self._sync_client_gen_locked(client)
            if not force and eff_ttl > 0:
                entry = self._blocks.get(key)
                if entry is not None:
                    expires_at, _, cached = entry
                    if now < expires_at:
                        self._hits += 1
                        return copy.deepcopy(cached)
                    self._blocks.pop(key, None)
            self._misses += 1

        res = client.get_block(list(cell))
        if eff_ttl > 0 and isinstance(res, dict) and bare(res.get("name")):
            stored_at = self._clock()
            with self._lock:
                cur_gen = int(getattr(client, "_block_cache_gen", 0) or 0)
                if cur_gen == gen_before:
                    self._blocks[key] = (stored_at + eff_ttl, cur_gen, copy.deepcopy(res))
                    self._evict_if_needed_locked(stored_at)
        return copy.deepcopy(res) if isinstance(res, dict) else res

    def invalidate(self, pos: Any, *, dimension: str = "overworld") -> None:
        """使单格缓存立即失效。"""
        cell = _norm_cell(pos)
        if cell is None:
            return
        dim = _norm_dim(dimension)
        with self._lock:
            self._blocks.pop((dim, cell[0], cell[1], cell[2]), None)

    def invalidate_radius(self, pos: Any, radius: int = 1, *, dimension: str = "overworld") -> None:
        """使 `pos` 周围切比雪夫半径 `radius` 内的全部体素缓存失效。"""
        cell = _norm_cell(pos)
        if cell is None:
            return
        r = max(0, int(radius))
        dim = _norm_dim(dimension)
        bx, by, bz = cell
        with self._lock:
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    for dz in range(-r, r + 1):
                        self._blocks.pop((dim, bx + dx, by + dy, bz + dz), None)

    def mark_unreachable(
        self,
        skill_name: str,
        pos: Any,
        *,
        dimension: str = "overworld",
        stance_cell: Any = None,
        ttl_s: float = 120.0,
    ) -> None:
        """将目标方块记入技能级不可达黑名单（可选绑定当前站位格）。"""
        cell = _norm_cell(pos)
        if cell is None:
            return
        eff_ttl = max(0.0, float(ttl_s))
        if eff_ttl <= 0:
            return
        dim = _norm_dim(dimension)
        stance = _norm_cell(stance_cell) if stance_cell is not None else None
        key = (str(skill_name), dim, cell[0], cell[1], cell[2])
        with self._lock:
            self._blacklist[key] = (self._clock() + eff_ttl, stance)

    def is_unreachable(
        self,
        skill_name: str,
        pos: Any,
        *,
        dimension: str = "overworld",
        stance_cell: Any = None,
    ) -> bool:
        """检查目标是否在当前有效不可达黑名单内。"""
        cell = _norm_cell(pos)
        if cell is None:
            return False
        dim = _norm_dim(dimension)
        key = (str(skill_name), dim, cell[0], cell[1], cell[2])
        now = self._clock()
        with self._lock:
            entry = self._blacklist.get(key)
            if entry is None:
                return False
            expires_at, recorded_stance = entry
            if now >= expires_at:
                self._blacklist.pop(key, None)
                return False
            if stance_cell is not None and recorded_stance is not None:
                queried = _norm_cell(stance_cell)
                if queried is not None and queried != recorded_stance:
                    return False
            return True

    def clear_stance_blacklist_if_moved(self, cur_pos: Any, *, min_dist: float = 6.0) -> bool:
        """当玩家距站位黑名单记录点移动达到 `min_dist` 格时，清理对应站位黑名单条目。"""
        pt = _norm_point3(cur_pos)
        if pt is None:
            return False
        threshold = max(0.0, float(min_dist))
        now = self._clock()
        removed = False
        with self._lock:
            for key, (expires_at, recorded_stance) in list(self._blacklist.items()):
                if now >= expires_at:
                    self._blacklist.pop(key, None)
                    continue
                if recorded_stance is None:
                    continue
                stance_pt = (recorded_stance[0] + 0.5, float(recorded_stance[1]), recorded_stance[2] + 0.5)
                if math.dist(pt, stance_pt) >= threshold:
                    self._blacklist.pop(key, None)
                    removed = True
        return removed

    def stats(self) -> dict[str, int]:
        """返回缓存命中/未命中/吸收与当前有效黑名单统计。"""
        now = self._clock()
        with self._lock:
            expired = [k for k, (exp, _) in self._blacklist.items() if now >= exp]
            for k in expired:
                self._blacklist.pop(k, None)
            return {
                "hits": self._hits,
                "misses": self._misses,
                "ingested": self._ingested,
                "blacklisted": len(self._blacklist),
            }


_REGISTRY_LOCK = threading.Lock()
_CACHES: weakref.WeakKeyDictionary[Any, VoxelCache] = weakref.WeakKeyDictionary()
_FALLBACK_CACHE: Optional[VoxelCache] = None


def voxel_cache_for(state: Any) -> VoxelCache:
    """获取与指定 `State` 绑定的 `VoxelCache` 实例；同一 `state` 始终返回同一对象。"""
    global _FALLBACK_CACHE
    with _REGISTRY_LOCK:
        if state is None:
            if _FALLBACK_CACHE is None:
                _FALLBACK_CACHE = VoxelCache()
            return _FALLBACK_CACHE
        try:
            cache = _CACHES.get(state)
            if cache is None:
                cache = VoxelCache()
                _CACHES[state] = cache
            return cache
        except TypeError:
            if _FALLBACK_CACHE is None:
                _FALLBACK_CACHE = VoxelCache()
            return _FALLBACK_CACHE


def reset_all_voxel_caches() -> None:
    """清空所有已注册的 `VoxelCache`（主要用于单测隔离与新一局重置）。"""
    global _FALLBACK_CACHE
    with _REGISTRY_LOCK:
        _CACHES.clear()
        _FALLBACK_CACHE = None
