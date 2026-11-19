"""共享世界资源台账：按维度、类别与整数格坐标缓存暴露方块及可达性状态。

遵守 `AGENTS.md` §9（一文件一职责、零阻塞动作、零 HTTP 调用、导入不触发 `@register`）：
- 坐标经 `skills.sightline.block_cell` 规整为整数角点 `(bx, by, bz)`；
- 兼容 `/find_blocks` 明细与摘要模式；`crying_obsidian`/`obsidian` 同记资源与结构指纹；
- 状态粘性：`protected`/`depleted` 永久保留，`reachable` 不被 `untested` 降级，
  `unreachable` 在冷却窗（默认 120s，可注入单调时钟）内不被重扫洗白；
- 按 `State` 弱引用隔离台账；`summarize()`/`format_memo_summary()` 绝不输出三维坐标（J17）。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, Optional
import weakref

from .items import bare
from .sightline import Cell, block_cell, block_center, eye_to_block_dist, point3

ALL_LOG_BLOCKS: tuple[str, ...] = (
    "oak_log", "birch_log", "spruce_log", "jungle_log", "acacia_log",
    "dark_oak_log", "cherry_log", "mangrove_log", "pale_oak_log",
)
BED_BLOCKS: tuple[str, ...] = tuple(
    f"{c}_bed" for c in ("white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
                         "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black")
)
CATEGORY_BLOCKS: dict[str, tuple[str, ...]] = {
    "wood": ALL_LOG_BLOCKS,
    "water": ("water",),
    "lava": ("lava", "lava_cauldron"),
    "iron_ore": ("iron_ore", "deepslate_iron_ore", "raw_iron_block"),
    "coal_ore": ("coal_ore", "deepslate_coal_ore"),
    "diamond_ore": ("diamond_ore", "deepslate_diamond_ore"),
    "obsidian": ("obsidian", "crying_obsidian"),
    "station": (
        "crafting_table", "furnace", "blast_furnace", "smoker", "enchanting_table",
        "anvil", "chipped_anvil", "damaged_anvil",
        "smithing_table", "jukebox", "note_block", "crafter", "beacon",
        *BED_BLOCKS,
    ),
}
STRUCTURE_FINGERPRINTS: dict[str, tuple[str, ...]] = {
    "village": ("hay_block", "bell", "composter", "lectern"),
    "stronghold": ("stone_bricks", "mossy_stone_bricks", "cracked_stone_bricks", "end_portal_frame"),
    "ruined_portal": ("crying_obsidian", "obsidian", "gold_block"),
    "bee_nest": ("bee_nest",),
    "trial_chambers": ("trial_spawner", "vault"),
    "deep_dark": ("sculk_catalyst", "sculk_shrieker"),
    "archaeology": ("suspicious_sand", "suspicious_gravel"),
    "powder_snow": ("powder_snow",),
    "nether_fortress": ("nether_bricks", "nether_brick_fence", "nether_brick_stairs", "nether_wart"),
    "bastion_remnant": ("polished_blackstone_bricks", "gilded_blackstone", "cracked_polished_blackstone_bricks", "chiseled_polished_blackstone"),
    "end_city": ("purpur_block", "purpur_pillar", "end_stone_bricks"),
    "lush_caves": ("moss_block", "clay", "azalea", "glow_berries", "cave_vines"),
    "amethyst_geode": ("amethyst_block", "budding_amethyst", "calcite"),
    "pale_garden": ("pale_oak_log", "pale_moss_block", "creaking_heart"),
    "pillager_outpost": ("dark_oak_log", "white_banner"),
    "dried_ghast": ("dried_ghast", "bone_block"),
    "meadow": ("allium", "emerald_ore"),
    "swamp": ("lily_pad", "blue_orchid"),
    "mangrove_swamp": ("mangrove_log", "mangrove_roots", "muddy_mangrove_roots", "mangrove_propagule"),
    "cold_biome": ("packed_ice", "blue_ice", "snow_block", "end_stone"),
    "trial_vault_ominous": ("vault", "trial_spawner", "chiseled_tuff_bricks"),
}
VALID_STATUSES: tuple[str, ...] = ("untested", "reachable", "unreachable", "protected", "depleted")

_BLOCK_TO_CATEGORY: dict[str, str] = {b: c for c, bs in CATEGORY_BLOCKS.items() for b in bs}
_CATEGORY_LABELS: dict[str, str] = {
    "wood": "原木", "water": "水源", "lava": "熔岩", "coal_ore": "煤矿",
    "iron_ore": "铁矿", "diamond_ore": "钻石矿", "obsidian": "黑曜石", "station": "工作站", "structure": "结构",
}
_STATUS_LABELS: dict[str, str] = {"untested": "未探测", "reachable": "已确认可达", "unreachable": "可重探"}
_FALLBACK_CAT = "_pos"


def _norm_dim(dimension: Optional[str]) -> str:
    return bare(dimension).strip() or "overworld"


def _finite_float(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _parse_cell(value: Any) -> Optional[Cell]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        return None
    return (int(value[0]), int(value[1]), int(value[2]))


def _origin_dist(origin: Any, cell: Cell) -> float:
    """复用 `sightline` 计算玩家/参考坐标到方块几何中心的欧氏距离。"""
    if isinstance(origin, Mapping) or hasattr(origin, "snapshot"):
        return eye_to_block_dist(origin.snapshot() if hasattr(origin, "snapshot") else origin, cell)
    return math.dist(point3(origin, label="参考坐标"), block_center(cell))


def classify_block(name: str) -> Optional[str]:
    """将方块名映射到核心资源类别（兼容 `minecraft:` 前缀与全部 `*_bed`）。"""
    b = bare(name).strip().lower()
    if not b:
        return None
    return _BLOCK_TO_CATEGORY[b] if b in _BLOCK_TO_CATEGORY else ("station" if len(b) > 4 and b.endswith("_bed") else None)


def structure_tags_for(names: Iterable[str]) -> dict[str, list[str]]:
    """根据方块名集合识别命中的结构指纹，返回 `{结构标签: [命中方块...]}`。"""
    seen = {bare(n).strip().lower() for n in names if bare(n).strip()}
    return {t: m for t, ms in STRUCTURE_FINGERPRINTS.items() if (m := [x for x in ms if x in seen])} if seen else {}


@dataclass
class ResourceEntry:
    """台账中的单格资源记录。"""

    category: str
    block: str
    pos: tuple[int, int, int]
    dimension: str
    status: str = "untested"
    error_code: str = ""
    last_dist: Optional[float] = None
    attempts: int = 0
    updated_at: float = 0.0
    tag: Optional[str] = None


class WorldResourceMemo:
    """单会话世界资源台账：按 `(dimension, category, pos)` 存储并执行状态粘性与容量裁剪。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_per_bucket: int = 256, unreachable_cooldown_s: float = 120.0) -> None:
        if max_per_bucket < 1:
            raise ValueError("max_per_bucket 必须 >= 1")
        self._clock, self._max_per_bucket, self._default_cooldown_s = clock, int(max_per_bucket), float(unreachable_cooldown_s)
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str, Cell], ResourceEntry] = {}
        self._stations: dict[tuple[str, str], list[Cell]] = {}
        self._landmarks: dict[tuple[str, str], list[tuple[Cell, dict[str, Any]]]] = {}
        self._portals: dict[str, list[Cell]] = {}
        self._blocked_regions: dict[tuple[str, tuple[int, ...]], float] = {}
        self.visited_biomes: dict[str, set[str]] = {}
        self.survey_sectors_done: dict[str, set[int]] = {}
        self.landmark_version: int = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._stations.clear()
            self._landmarks.clear()
            self._portals.clear()
            self._blocked_regions.clear()
            self.visited_biomes.clear()
            self.survey_sectors_done.clear()
            self.landmark_version = 0

    def _cooldown(self, override: Optional[float]) -> float:
        return self._default_cooldown_s if override is None else float(override)

    @staticmethod
    def _is_entry_blocked(entry: ResourceEntry, now: float, cooldown_s: float) -> bool:
        return entry.status in ("protected", "depleted") or (entry.status == "unreachable" and (now - entry.updated_at) < cooldown_s)

    def _put(self, entry: ResourceEntry) -> ResourceEntry:
        dim, cat = entry.dimension, entry.category
        self._entries[(dim, cat, entry.pos)] = entry
        bucket = [k for k in self._entries if k[0] == dim and k[1] == cat]
        while len(bucket) > self._max_per_bucket:
            victim = min(bucket, key=lambda k: (self._entries[k].status == "protected", self._entries[k].updated_at))
            self._entries.pop(victim, None)
            bucket.remove(victim)
        return replace(entry)

    def _upsert_seen(self, category: str, block: str, cell: Cell, dim: str, dist_val: Optional[float], tag: Optional[str], status: str, drop_fb: bool) -> ResourceEntry:
        now = float(self._clock())
        prev = self._entries.get((dim, category, cell))
        used_fb = prev is None and category != _FALLBACK_CAT and (prev := self._entries.get((dim, _FALLBACK_CAT, cell))) is not None
        if prev is None:
            return self._put(ResourceEntry(category, block, cell, dim, status, "", dist_val, 0, now, tag))
        next_status, next_err, next_at = status, ("" if status in ("untested", "reachable") else prev.error_code), now
        if status == "untested":
            if prev.status in ("protected", "depleted") or (prev.status == "unreachable" and (now - prev.updated_at) < self._default_cooldown_s):
                next_status, next_err, next_at = prev.status, prev.error_code, prev.updated_at
            elif prev.status == "reachable":
                next_status, next_err = "reachable", ""
        elif status == "unreachable" and prev.status == "protected":
            next_status, next_at = "protected", prev.updated_at
        saved = self._put(ResourceEntry(
            category, block or prev.block, cell, dim, next_status, next_err,
            dist_val if dist_val is not None else prev.last_dist, prev.attempts, next_at, tag if tag is not None else prev.tag,
        ))
        if used_fb and drop_fb:
            self._entries.pop((dim, _FALLBACK_CAT, cell), None)
        return saved

    def record_seen(self, category: str, block: str, pos: Any, *, dimension: str = "overworld", dist: Optional[float] = None, tag: Optional[str] = None, status: str = "untested") -> ResourceEntry:
        """记录观察到的资源方块，并按状态粘性规则合并已有条目。"""
        if not isinstance(category, str) or not category.strip():
            raise ValueError(f"非法资源类别: {category!r}")
        if status not in VALID_STATUSES:
            raise ValueError(f"非法状态 {status!r}，必须属于 {VALID_STATUSES}")
        with self._lock:
            return self._upsert_seen(category, bare(block).strip(), block_cell(pos), _norm_dim(dimension), _finite_float(dist), tag, status, True)

    def _update_status(self, pos: Any, status: str, *, dimension: str, category: Optional[str], block: Optional[str] = None, error_code: Optional[str] = None, count_attempt: bool = True) -> ResourceEntry:
        dim, cell, bname = _norm_dim(dimension), block_cell(pos), bare(block).strip() if block else ""
        with self._lock:
            now = float(self._clock())
            cats = [category] if category is not None else (sorted(k[1] for k in self._entries if k[0] == dim and k[2] == cell) or [(classify_block(bname) if bname else None) or _FALLBACK_CAT])
            fb = self._entries.get((dim, _FALLBACK_CAT, cell))
            if category is not None and category != _FALLBACK_CAT and status == "reachable":
                self._entries.pop((dim, _FALLBACK_CAT, cell), None)
            last_saved: Optional[ResourceEntry] = None
            for cat in cats:
                prev = self._entries.get((dim, cat, cell))
                prot = (prev is not None and prev.status == "protected") or (fb is not None and fb.status == "protected")
                eff = "protected" if (status == "unreachable" and prot) else status
                err = "" if eff == "reachable" else (str(error_code) if error_code is not None else (prev.error_code if prev else ""))
                last_saved = self._put(ResourceEntry(
                    cat, bname or (prev.block if prev else ""), cell, dim, eff, err, prev.last_dist if prev else None,
                    (prev.attempts if prev else 0) + (1 if count_attempt else 0), now, prev.tag if prev else None,
                ))
            assert last_saved is not None
            return last_saved

    def mark_reachable(self, pos: Any, *, dimension: str = "overworld", category: Optional[str] = None, block: Optional[str] = None) -> ResourceEntry:
        return self._update_status(pos, "reachable", dimension=dimension, category=category, block=block)

    def mark_unreachable(self, pos: Any, *, dimension: str = "overworld", category: Optional[str] = None, error_code: str = "", block: Optional[str] = None) -> ResourceEntry:
        return self._update_status(pos, "unreachable", dimension=dimension, category=category, block=block, error_code=error_code)

    def mark_protected(self, pos: Any, *, dimension: str = "overworld", category: Optional[str] = None, block: Optional[str] = None) -> ResourceEntry:
        return self._update_status(pos, "protected", dimension=dimension, category=category, block=block)

    def mark_depleted(self, pos: Any, *, dimension: str = "overworld", category: Optional[str] = None) -> ResourceEntry:
        return self._update_status(pos, "depleted", dimension=dimension, category=category, count_attempt=False)

    def is_blocked(self, pos: Any, *, dimension: str = "overworld", category: Optional[str] = None, unreachable_cooldown_s: Optional[float] = None) -> bool:
        """判断目标格在给定维度下是否受限（`protected`/`depleted` 永久受限，`unreachable` 冷却内受限）。"""
        dim, cell, cd = _norm_dim(dimension), block_cell(pos), self._cooldown(unreachable_cooldown_s)
        with self._lock:
            now = float(self._clock())
            entries = ([e for k, e in self._entries.items() if k[0] == dim and k[2] == cell] if category is None
                       else [e for k in ((dim, category, cell), (dim, _FALLBACK_CAT, cell)) if (e := self._entries.get(k)) is not None])
            return any(self._is_entry_blocked(e, now, cd) for e in entries)

    def query(self, category: str, *, dimension: str = "overworld", origin: Any = None, include_unreachable: bool = False, unreachable_cooldown_s: Optional[float] = None) -> list[ResourceEntry]:
        """查询指定维度与类别的可选候选（剔除 `protected`/`depleted` 与未过冷却的 `unreachable`）。"""
        dim, cd = _norm_dim(dimension), self._cooldown(unreachable_cooldown_s)
        with self._lock:
            now = float(self._clock())
            out = [
                replace(e) for (edim, ecat, cell), e in self._entries.items()
                if edim == dim and ecat == category and e.status not in ("protected", "depleted")
                and not ((fb := self._entries.get((dim, _FALLBACK_CAT, cell))) is not None and category != _FALLBACK_CAT and self._is_entry_blocked(fb, now, cd))
                and (e.status != "unreachable" or include_unreachable or (now - e.updated_at) >= cd)
            ]
        out.sort(key=(lambda e: (_origin_dist(origin, e.pos), -e.updated_at)) if origin is not None else (lambda e: (e.last_dist if e.last_dist is not None else math.inf, -e.updated_at)))
        return out

    def _record_station_locked(self, kind: str, cell: Cell, dim: str) -> None:
        k = bare(kind).strip().lower()
        if not k:
            raise ValueError(f"非法工作站类型: {kind!r}")
        cells = self._stations.setdefault((dim, k), [])
        if any(math.dist(cell, existing) <= 1.5 for existing in cells):
            return
        cells.append(cell)

    def _record_landmark_locked(self, kind: str, cell: Cell, dim: str, meta: Optional[Mapping[str, Any]] = None) -> bool:
        k = bare(kind).strip().lower()
        if not k:
            raise ValueError(f"非法地标类型: {kind!r}")
        items = self._landmarks.setdefault((dim, k), [])
        for existing_cell, existing_meta in items:
            if math.dist(cell, existing_cell) <= 8.0:
                if isinstance(meta, Mapping):
                    existing_meta.update(meta)
                return False
        items.append((cell, dict(meta) if isinstance(meta, Mapping) else {}))
        self.landmark_version += 1
        return True

    def record_station(self, kind: str, pos: Any, *, dimension: str = "overworld") -> None:
        """记录工作站坐标，同维度同类型 1.5 格内去重。"""
        k = bare(kind).strip().lower()
        if not k:
            raise ValueError(f"非法工作站类型: {kind!r}")
        dim, cell = _norm_dim(dimension), block_cell(pos)
        with self._lock:
            self._record_station_locked(k, cell, dim)

    def has_station(self, kind: str, *, dimension: str = "overworld") -> bool:
        """判断指定维度是否存在指定类型的工作站。"""
        return bool(self.stations(kind, dimension=dimension))

    def stations(self, kind: Optional[str] = None, *, dimension: str = "overworld") -> list[tuple[int, int, int]]:
        """查询指定维度（及可选工作站类型）已记录的工作站整数格坐标列表。"""
        dim = _norm_dim(dimension)
        k = bare(kind).strip().lower() if kind is not None else None
        if kind is not None and not k:
            return []
        with self._lock:
            out: list[tuple[int, int, int]] = []
            seen: set[Cell] = set()
            for (d, sk), cells in self._stations.items():
                if d != dim:
                    continue
                if k is not None:
                    matched = (
                        sk == k
                        or (k == "bed" and sk.endswith("_bed"))
                        or (k == "furnace" and sk in ("blast_furnace", "smoker"))
                        or (k == "anvil" and sk in ("chipped_anvil", "damaged_anvil"))
                    )
                    if not matched:
                        continue
                for cell in cells:
                    if cell not in seen:
                        seen.add(cell)
                        out.append(tuple(cell))
            return out

    def record_landmark(self, kind: str, pos: Any, *, dimension: str = "overworld", meta: Optional[dict] = None) -> bool:
        """记录结构/地标位置，同维度同类型 8.0 格内去重；新增时递增 `landmark_version`。"""
        k = bare(kind).strip().lower()
        if not k:
            raise ValueError(f"非法地标类型: {kind!r}")
        dim, cell = _norm_dim(dimension), block_cell(pos)
        with self._lock:
            return self._record_landmark_locked(k, cell, dim, meta)

    def has_landmark(self, kind: Optional[str] = None, *, dimension: str = "overworld") -> bool:
        """判断指定维度是否已记录（指定类型的）地标。"""
        dim = _norm_dim(dimension)
        k = bare(kind).strip().lower() if kind is not None else None
        if kind is not None and not k:
            return False
        with self._lock:
            if k is None:
                return any(bool(items) for (d, _), items in self._landmarks.items() if d == dim)
            return bool(self._landmarks.get((dim, k)))

    def landmarks(self, kind: Optional[str] = None, *, dimension: str = "overworld") -> list[tuple[int, int, int]]:
        """查询指定维度（及可选地标类型）已记录的地标整数格坐标列表。"""
        dim = _norm_dim(dimension)
        k = bare(kind).strip().lower() if kind is not None else None
        if kind is not None and not k:
            return []
        with self._lock:
            if k is None:
                return [
                    tuple(cell)
                    for (d, _), items in self._landmarks.items()
                    if d == dim
                    for cell, _ in items
                ]
            return [tuple(cell) for cell, _ in self._landmarks.get((dim, k), ())]

    def record_active_portal(self, pos: Any, *, dimension: str = "overworld") -> None:
        """记录已激活传送门坐标，同维度 2.0 格内去重。"""
        dim, cell = _norm_dim(dimension), block_cell(pos)
        with self._lock:
            cells = self._portals.setdefault(dim, [])
            if any(math.dist(cell, existing) <= 2.0 for existing in cells):
                return
            cells.append(cell)

    def has_active_portal(self, *, dimension: str = "overworld") -> bool:
        """判断指定维度是否存在已激活传送门。"""
        dim = _norm_dim(dimension)
        with self._lock:
            return bool(self._portals.get(dim))

    def record_biome(self, biome: str, *, dimension: str = "overworld") -> bool:
        """记录已访问生物群系（按维度隔离）；若为首次记录返回 `True`，空值或已存在返回 `False`。"""
        b = bare(biome).strip().lower() if isinstance(biome, str) else ""
        if not b:
            return False
        dim = _norm_dim(dimension)
        with self._lock:
            biomes = self.visited_biomes.setdefault(dim, set())
            if b in biomes:
                return False
            biomes.add(b)
            return True

    def biomes(self, *, dimension: str = "overworld") -> set[str]:
        """返回指定维度已访问生物群系集合的副本。"""
        dim = _norm_dim(dimension)
        with self._lock:
            return set(self.visited_biomes.get(dim, ()))

    def mark_sector_done(self, sector_idx: int, *, dimension: str = "overworld") -> None:
        """标记指定维度的已勘查扇区索引（必须为非布尔非负整数）。"""
        if not isinstance(sector_idx, int) or isinstance(sector_idx, bool) or sector_idx < 0:
            raise ValueError(f"非法扇区索引: {sector_idx!r}")
        dim = _norm_dim(dimension)
        with self._lock:
            self.survey_sectors_done.setdefault(dim, set()).add(int(sector_idx))

    def done_sectors(self, *, dimension: str = "overworld") -> set[int]:
        """返回指定维度已勘查扇区索引集合的副本。"""
        dim = _norm_dim(dimension)
        with self._lock:
            return set(self.survey_sectors_done.get(dim, ()))

    def record_blocked_region(self, skill_name: str, chunk: tuple, *, ttl_s: float = 120.0) -> None:
        """记录指定技能在目标区块上的临时受阻标记（带 TTL 冷却）。"""
        key = (str(skill_name).strip(), tuple(int(v) for v in chunk))
        with self._lock:
            self._blocked_regions[key] = float(self._clock()) + max(0.0, float(ttl_s))

    def is_region_blocked(self, skill_name: str, chunk: tuple) -> bool:
        """判断指定技能在目标区块的受阻冷却是否仍有效；过期则清理并返回 `False`。"""
        key = (str(skill_name).strip(), tuple(int(v) for v in chunk))
        with self._lock:
            expires_at = self._blocked_regions.get(key)
            if expires_at is None:
                return False
            if float(self._clock()) >= expires_at:
                self._blocked_regions.pop(key, None)
                return False
            return True

    def ingest_find_blocks(self, response: Any, *, dimension: str = "overworld", origin: Any = None, category: Optional[str] = None) -> int:
        """吸收 `find_blocks` 明细或摘要响应；同时记录资源分类与结构指纹标签。"""
        if not isinstance(response, Mapping):
            return 0
        payload = response.get("result") if "blocks" not in response and isinstance(response.get("result"), Mapping) else response
        blocks = payload.get("blocks")
        rows = ([(r.get("name"), r.get("pos"), r.get("dist")) for r in blocks if isinstance(r, Mapping)] if isinstance(blocks, list) else
                [(n, i.get("nearest") if i.get("nearest") is not None else i.get("pos"), i.get("dist")) for n, i in blocks.items() if isinstance(i, Mapping)] if isinstance(blocks, Mapping) else None)
        if rows is None:
            return 0
        dim, ingested = _norm_dim(dimension), 0
        with self._lock:
            prot_depl_cells = {
                ec for (ed, _, ec), e in self._entries.items()
                if ed == dim and e.status in ("protected", "depleted")
            }
            for raw_name, raw_pos, raw_dist in rows:
                bname = bare(raw_name).strip()
                try:
                    cell = block_cell(raw_pos)
                except Exception:
                    continue
                if not bname:
                    continue
                dist_val = _finite_float(raw_dist)
                if dist_val is None and origin is not None:
                    dist_val = _origin_dist(origin, cell)
                tags = list(structure_tags_for((bname,)))
                ptag = tags[0] if tags else None
                targets = ([(category, ptag if category == "structure" else None)] if category is not None
                           else ([(c, ptag)] if (c := classify_block(bname)) is not None else []) + [("structure", t) for t in tags])
                for cat, tag in targets:
                    saved = self._upsert_seen(cat, bname, cell, dim, dist_val, tag, "untested", False)
                    if saved.status not in ("protected", "depleted") and cell not in prot_depl_cells:
                        if cat == "station":
                            self._record_station_locked(bname, cell, dim)
                        elif cat == "structure" and tag:
                            self._record_landmark_locked(tag, cell, dim, {"block": bname})
                if targets:
                    self._entries.pop((dim, _FALLBACK_CAT, cell), None)
                    ingested += 1
        return ingested

    def summarize(self, dimension: str = "overworld", origin: Any = None, categories: Optional[Iterable[str]] = None, max_items: int = 6, *, unreachable_cooldown_s: Optional[float] = None) -> str:
        """生成绝对不含任何原始三维坐标的脱敏一行资源摘要（遵守 J17）。"""
        if max_items <= 0:
            return ""
        dim = _norm_dim(dimension)
        with self._lock:
            cat_order = tuple(categories) if categories is not None else (*_CATEGORY_LABELS, *sorted({k[1] for k in self._entries if k[0] == dim and k[1] != _FALLBACK_CAT and k[1] not in _CATEGORY_LABELS}))
        segments: list[str] = []
        for cat in cat_order:
            if len(segments) >= max_items:
                break
            entries = self.query(cat, dimension=dim, origin=origin, unreachable_cooldown_s=unreachable_cooldown_s)
            if not entries:
                continue
            by_tag: dict[Optional[str], ResourceEntry] = {}
            for e in (entries if cat == "structure" else entries[:1]):
                by_tag.setdefault(e.tag if cat == "structure" else None, e)
            label = _CATEGORY_LABELS.get(cat, cat)
            for e in list(by_tag.values())[: max_items - len(segments)]:
                d = _origin_dist(origin, e.pos) if origin is not None else e.last_dist
                dist_part = f" 约{max(0, round(d))}格" if d is not None else ""
                name = f"{e.tag}({e.block or cat})" if (cat == "structure" and e.tag) else (e.block or cat)
                segments.append(f"{label}: {name}{dist_part}({_STATUS_LABELS.get(e.status, e.status)})")
        return " | ".join(segments)

    def summary_for_context(self, *, dimension: str = "overworld") -> dict[str, Any]:
        """生成绝对不含坐标与实体 ID 的上下文摘要字典。"""
        dim = _norm_dim(dimension)
        with self._lock:
            now = float(self._clock())
            cd = self._default_cooldown_s
            stations_ready = sorted({
                ("bed" if sk.endswith("_bed") else sk)
                for (d, sk), cells in self._stations.items()
                if d == dim and cells
            })
            landmark_kinds: set[str] = {
                lk for (d, lk), items in self._landmarks.items()
                if d == dim and items
            }
            resource_counts: dict[str, int] = {}
            for (edim, ecat, cell), e in self._entries.items():
                if edim != dim or ecat == _FALLBACK_CAT:
                    continue
                fb = self._entries.get((dim, _FALLBACK_CAT, cell))
                if self._is_entry_blocked(e, now, cd) or (fb is not None and self._is_entry_blocked(fb, now, cd)):
                    continue
                if ecat == "structure":
                    if e.tag:
                        landmark_kinds.add(e.tag)
                else:
                    resource_counts[ecat] = resource_counts.get(ecat, 0) + 1
            summary: dict[str, Any] = {
                "dimension": dim,
                "stations_ready": stations_ready,
                "landmarks_discovered": sorted(landmark_kinds),
                "active_portal": bool(self._portals.get(dim)),
                "resource_counts": dict(sorted(resource_counts.items())),
                "landmark_version": int(self.landmark_version),
            }
            if bs := self.visited_biomes.get(dim):
                summary["visited_biomes"] = sorted(bs)
            if ss := self.survey_sectors_done.get(dim):
                summary["survey_sectors_done"] = sorted(ss)
            return summary

    def save_checkpoint(self, path: str | os.PathLike[str]) -> None:
        """原子保存台账检查点为 UTF-8 JSON（`schema_version=1`）。"""
        target = Path(path)
        with self._lock:
            stations_out: dict[str, dict[str, list[list[int]]]] = {}
            for (dim, kind), cells in self._stations.items():
                if cells:
                    stations_out.setdefault(dim, {})[kind] = [list(c) for c in cells]
            landmarks_out: dict[str, dict[str, list[dict[str, Any]]]] = {}
            for (dim, kind), items in self._landmarks.items():
                if items:
                    landmarks_out.setdefault(dim, {})[kind] = [
                        {"pos": list(cell), "meta": dict(meta)} for cell, meta in items
                    ]
            portals_out: dict[str, list[list[int]]] = {
                dim: [list(c) for c in cells]
                for dim, cells in self._portals.items()
                if cells
            }
            resources_out = [
                {
                    "category": e.category,
                    "block": e.block,
                    "pos": list(e.pos),
                    "dimension": e.dimension,
                    "status": e.status,
                    "error_code": e.error_code,
                    "last_dist": e.last_dist,
                    "attempts": e.attempts,
                    "updated_at": e.updated_at,
                    "tag": e.tag,
                }
                for e in self._entries.values()
            ]
            payload = {
                "schema_version": 1,
                "landmark_version": int(self.landmark_version),
                "stations": stations_out,
                "landmarks": landmarks_out,
                "portals": portals_out,
                "resources": resources_out,
                "visited_biomes": {dim: sorted(bs) for dim, bs in self.visited_biomes.items() if bs},
                "survey_sectors_done": {dim: sorted(ss) for dim, ss in self.survey_sectors_done.items() if ss},
            }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(target.parent),
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as fh:
                tmp_path = fh.name
                fh.write(serialized)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
            tmp_path = None
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def load_checkpoint(self, path: str | os.PathLike[str]) -> bool:
        """安全读取并校验 JSON 检查点（`schema_version=1`），成功则原子替换内部状态。"""
        try:
            target = Path(path)
            raw = target.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, Mapping):
                return False
            schema_ver = data.get("schema_version")
            if isinstance(schema_ver, bool) or schema_ver != 1:
                return False
            lv = data.get("landmark_version")
            if not isinstance(lv, int) or isinstance(lv, bool) or lv < 0:
                return False

            stations_raw = data.get("stations")
            landmarks_raw = data.get("landmarks")
            portals_raw = data.get("portals")
            resources_raw = data.get("resources")
            if not (
                isinstance(stations_raw, Mapping)
                and isinstance(landmarks_raw, Mapping)
                and isinstance(portals_raw, Mapping)
                and isinstance(resources_raw, list)
            ):
                return False

            new_stations: dict[tuple[str, str], list[Cell]] = {}
            for raw_dim, kind_map in stations_raw.items():
                if not isinstance(raw_dim, str) or not isinstance(kind_map, Mapping):
                    return False
                dim = _norm_dim(raw_dim)
                for raw_kind, cells_raw in kind_map.items():
                    if not isinstance(raw_kind, str) or not isinstance(cells_raw, list):
                        return False
                    k = bare(raw_kind).strip().lower()
                    if not k:
                        return False
                    parsed_cells: list[Cell] = []
                    for c in cells_raw:
                        cell = _parse_cell(c)
                        if cell is None:
                            return False
                        parsed_cells.append(cell)
                    if parsed_cells:
                        new_stations[(dim, k)] = parsed_cells

            new_landmarks: dict[tuple[str, str], list[tuple[Cell, dict[str, Any]]]] = {}
            for raw_dim, kind_map in landmarks_raw.items():
                if not isinstance(raw_dim, str) or not isinstance(kind_map, Mapping):
                    return False
                dim = _norm_dim(raw_dim)
                for raw_kind, items_raw in kind_map.items():
                    if not isinstance(raw_kind, str) or not isinstance(items_raw, list):
                        return False
                    k = bare(raw_kind).strip().lower()
                    if not k:
                        return False
                    parsed_items: list[tuple[Cell, dict[str, Any]]] = []
                    for item in items_raw:
                        if not isinstance(item, Mapping):
                            return False
                        cell = _parse_cell(item.get("pos"))
                        if cell is None:
                            return False
                        meta = item.get("meta", {})
                        if not isinstance(meta, Mapping):
                            return False
                        parsed_items.append((cell, dict(meta)))
                    if parsed_items:
                        new_landmarks[(dim, k)] = parsed_items

            new_portals: dict[str, list[Cell]] = {}
            for raw_dim, cells_raw in portals_raw.items():
                if not isinstance(raw_dim, str) or not isinstance(cells_raw, list):
                    return False
                dim = _norm_dim(raw_dim)
                parsed_cells = []
                for c in cells_raw:
                    cell = _parse_cell(c)
                    if cell is None:
                        return False
                    parsed_cells.append(cell)
                if parsed_cells:
                    new_portals[dim] = parsed_cells

            new_entries: dict[tuple[str, str, Cell], ResourceEntry] = {}
            for item in resources_raw:
                if not isinstance(item, Mapping):
                    return False
                cat = item.get("category")
                if not isinstance(cat, str) or not cat.strip():
                    return False
                block = item.get("block", "")
                if not isinstance(block, str):
                    return False
                cell = _parse_cell(item.get("pos"))
                if cell is None:
                    return False
                raw_dim = item.get("dimension", "overworld")
                if not isinstance(raw_dim, str):
                    return False
                dim = _norm_dim(raw_dim)
                status = item.get("status", "untested")
                if status not in VALID_STATUSES:
                    return False
                error_code = item.get("error_code", "")
                if not isinstance(error_code, str):
                    return False
                last_dist_raw = item.get("last_dist")
                last_dist = None if last_dist_raw is None else _finite_float(last_dist_raw)
                if last_dist_raw is not None and last_dist is None:
                    return False
                attempts = item.get("attempts", 0)
                if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
                    return False
                updated_at = _finite_float(item.get("updated_at", 0.0))
                if updated_at is None:
                    return False
                tag = item.get("tag")
                if tag is not None and not isinstance(tag, str):
                    return False
                new_entries[(dim, cat, cell)] = ResourceEntry(
                    cat, bare(block).strip(), cell, dim, status, error_code, last_dist, attempts, updated_at, tag
                )

            new_biomes: dict[str, set[str]] = {}
            if "visited_biomes" in data:
                biomes_raw = data["visited_biomes"]
                if not isinstance(biomes_raw, Mapping):
                    return False
                for raw_dim, biome_list in biomes_raw.items():
                    if not isinstance(raw_dim, str) or not isinstance(biome_list, list):
                        return False
                    dim = _norm_dim(raw_dim)
                    parsed_biomes: set[str] = set()
                    for raw_b in biome_list:
                        if not isinstance(raw_b, str):
                            return False
                        b = bare(raw_b).strip().lower()
                        if not b:
                            return False
                        parsed_biomes.add(b)
                    if parsed_biomes:
                        new_biomes.setdefault(dim, set()).update(parsed_biomes)

            new_sectors: dict[str, set[int]] = {}
            if "survey_sectors_done" in data:
                sectors_raw = data["survey_sectors_done"]
                if not isinstance(sectors_raw, Mapping):
                    return False
                for raw_dim, sector_list in sectors_raw.items():
                    if not isinstance(raw_dim, str) or not isinstance(sector_list, list):
                        return False
                    dim = _norm_dim(raw_dim)
                    parsed_sectors: set[int] = set()
                    for s in sector_list:
                        if not isinstance(s, int) or isinstance(s, bool) or s < 0:
                            return False
                        parsed_sectors.add(int(s))
                    if parsed_sectors:
                        new_sectors.setdefault(dim, set()).update(parsed_sectors)

            with self._lock:
                self._entries = new_entries
                self._stations = new_stations
                self._landmarks = new_landmarks
                self._portals = new_portals
                self.visited_biomes = new_biomes
                self.survey_sectors_done = new_sectors
                self.landmark_version = lv
            return True
        except Exception:
            return False


_STATE_MEMOS: weakref.WeakKeyDictionary[Any, WorldResourceMemo] = weakref.WeakKeyDictionary()
_DEFAULT_MEMO = WorldResourceMemo()
_MEMO_LOCK = threading.RLock()


def memo_for(state: Any) -> WorldResourceMemo:
    """获取与指定 `State` 弱引用绑定的资源台账；不可弱引用或 `None` 时回退全局默认台账。"""
    if state is None:
        return _DEFAULT_MEMO
    with _MEMO_LOCK:
        try:
            if (memo := _STATE_MEMOS.get(state)) is None:
                memo = WorldResourceMemo()
                _STATE_MEMOS[state] = memo
            return memo
        except TypeError:
            return _DEFAULT_MEMO


def reset_all_world_memos() -> None:
    """清空全部按 `State` 隔离的弱引用台账与全局默认台账。"""
    with _MEMO_LOCK:
        for memo in list(_STATE_MEMOS.values()):
            memo.clear()
        _STATE_MEMOS.clear()
        _DEFAULT_MEMO.clear()


def format_memo_summary(state: Any, **kwargs: Any) -> str:
    """从 `State` 提取维度与参考位置后返回脱敏资源摘要。"""
    snap = state.snapshot() if hasattr(state, "snapshot") else (getattr(state, "st", None) if isinstance(getattr(state, "st", None), Mapping) else (state if isinstance(state, Mapping) else {}))
    opts = dict(kwargs)
    if "dimension" not in opts:
        opts["dimension"] = (snap.get("dimension") if isinstance(snap, Mapping) else None) or "overworld"
    if "origin" not in opts and isinstance(snap, Mapping) and isinstance(pos := snap.get("pos"), (list, tuple)) and len(pos) == 3 and all(_finite_float(v) is not None for v in pos):
        opts["origin"] = snap
    return memo_for(state).summarize(**opts)


__all__ = [
    "ALL_LOG_BLOCKS", "BED_BLOCKS", "CATEGORY_BLOCKS", "STRUCTURE_FINGERPRINTS", "VALID_STATUSES",
    "ResourceEntry", "WorldResourceMemo", "classify_block", "structure_tags_for",
    "memo_for", "reset_all_world_memos", "format_memo_summary",
]
