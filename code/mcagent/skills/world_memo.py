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
import math
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
    "station": ("crafting_table", "furnace", "blast_furnace", "smoker", "enchanting_table", "anvil", "chipped_anvil", "damaged_anvil", *BED_BLOCKS),
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

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

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
                    self._upsert_seen(cat, bname, cell, dim, dist_val, tag, "untested", False)
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
