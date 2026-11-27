"""共享世界资源台账：按维度、类别与整数格坐标缓存暴露方块及可达性状态。

遵守 `AGENTS.md` §9（一文件一职责、零阻塞动作、零 HTTP 调用、导入不触发 `@register`）：
- 坐标经 `skills.sightline.block_cell` 规整为整数角点 `(bx, by, bz)`；
- 兼容 `/find_blocks` 明细与摘要模式；`crying_obsidian`/`obsidian` 同记资源与结构指纹；
- 状态粘性：`protected`/`depleted` 永久保留，`reachable` 不被 `untested` 降级，
  `unreachable` 在冷却窗（默认 120s，可注入单调时钟）内不被重扫洗白；
- 按 `State` 弱引用隔离台账；`summarize()`/`format_memo_summary()` 绝不输出三维坐标（J17）。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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
    "copper_ore": ("copper_ore", "deepslate_copper_ore", "raw_copper_block"),
    "lapis_ore": ("lapis_ore", "deepslate_lapis_ore"),
    "gold_ore": ("gold_ore", "deepslate_gold_ore", "nether_gold_ore", "raw_gold_block"),
    "redstone_ore": ("redstone_ore", "deepslate_redstone_ore"),
    "diamond_ore": ("diamond_ore", "deepslate_diamond_ore"),
    "emerald_ore": ("emerald_ore", "deepslate_emerald_ore"),
    "quartz_ore": ("nether_quartz_ore",),
    "ancient_debris": ("ancient_debris",),
    "obsidian": ("obsidian", "crying_obsidian"),
    "station": (
        "crafting_table", "furnace", "blast_furnace", "smoker", "enchanting_table",
        "anvil", "chipped_anvil", "damaged_anvil",
        "smithing_table", "jukebox", "note_block", "crafter", "beacon",
        "brewing_stand", "stonecutter", "chest", "trapped_chest", "barrel", "ender_chest",
        "campfire", "soul_campfire", "respawn_anchor", "bell", "grindstone", "loom",
        "cartography_table", "fletching_table", "composter", "lectern",
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
BIOME_BLOCK_FINGERPRINTS: dict[str, tuple[str, ...]] = {
    "savanna": ("acacia_log",),
    "badlands": ("terracotta", "red_sand"),
    "jungle": ("jungle_log", "bamboo", "cocoa"),
    "warm_ocean_ruins": ("magma_block", "suspicious_sand"),
}
ENTITY_LANDMARK_TAGS: dict[str, tuple[str, ...]] = {
    "villager": ("village",),
    "iron_golem": ("village",),
    "pillager": ("pillager_outpost",),
    "allay": ("pillager_outpost",),
    "breeze": ("trial_chambers",),
    "blaze": ("nether_fortress",),
    "wither_skeleton": ("nether_fortress",),
    "piglin_brute": ("bastion_remnant",),
    "shulker": ("end_city",),
    "axolotl": ("lush_caves",),
    "armadillo": ("savanna",),
    "parrot": ("jungle",),
    "goat": ("meadow",),
    "tadpole": ("swamp",),
    "frog": ("swamp",),
    "sheep": ("sheep_flock",),
}
VALID_STATUSES: tuple[str, ...] = ("untested", "reachable", "unreachable", "protected", "depleted")
SPIRAL_RING_RADII: tuple[int, ...] = (48, 96, 160, 240, 320)

_BLOCK_TO_CATEGORY: dict[str, str] = {b: c for c, bs in CATEGORY_BLOCKS.items() for b in bs}
_CATEGORY_LABELS: dict[str, str] = {
    "wood": "原木", "water": "水源", "lava": "熔岩", "coal_ore": "煤矿",
    "copper_ore": "铜矿", "iron_ore": "铁矿", "lapis_ore": "青金石矿",
    "gold_ore": "金矿", "redstone_ore": "红石矿", "diamond_ore": "钻石矿",
    "emerald_ore": "绿宝石矿", "quartz_ore": "下界石英矿", "ancient_debris": "远古残骸",
    "obsidian": "黑曜石", "station": "工作站", "structure": "结构",
}
_STATUS_LABELS: dict[str, str] = {"untested": "未探测", "reachable": "已确认可达", "unreachable": "可重探"}
_FALLBACK_CAT = "_pos"
_CAMP_STATIONS: tuple[str, ...] = ("chest", "bed", "furnace", "crafting_table")
_EDIBLE_RECOVERY_FOODS: frozenset[str] = frozenset({
    "cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_chicken",
    "cooked_rabbit", "cooked_cod", "cooked_salmon", "bread", "baked_potato",
    "golden_carrot", "golden_apple", "enchanted_golden_apple", "apple",
    "carrot", "melon_slice", "sweet_berries", "glow_berries", "pumpkin_pie",
    "cookie", "beetroot", "beetroot_soup", "mushroom_stew", "rabbit_stew",
    "honey_bottle", "dried_kelp", "beef", "porkchop", "mutton", "chicken",
    "cod", "salmon",
})
_UNRECOVERABLE_DEATH_CAUSE_KEYWORDS: tuple[str, ...] = (
    "lava", "void", "out_of_world", "fell_out_of_world", "in_fire", "on_fire", "magma",
)
_CORPSE_VALUABLE_ITEMS: frozenset[str] = frozenset({
    "obsidian", "shield", "flint_and_steel", "lava_bucket", "water_bucket",
    "bucket", "enchanting_table", "bow", "crossbow", "ender_pearl", "blaze_rod",
})


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


def _parse_pair2(value: Any) -> Optional[tuple[int, int]]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        return None
    return (int(value[0]), int(value[1]))


def _extract_xz(pos: Any) -> tuple[float, float]:
    if not isinstance(pos, Sequence) or isinstance(pos, (str, bytes, bytearray)) or len(pos) not in (2, 3):
        raise ValueError(f"非法平面坐标: {pos!r}")
    x = _finite_float(pos[0])
    z = _finite_float(pos[-1])
    if x is None or z is None:
        raise ValueError(f"非法平面坐标: {pos!r}")
    return (x, z)


def _normalize_stored_items(items: Optional[Mapping[str, Any]]) -> dict[str, int]:
    if not isinstance(items, Mapping):
        return {}
    out: dict[str, int] = {}
    for raw_name, raw_count in items.items():
        if not isinstance(raw_name, str):
            continue
        name = bare(raw_name).strip().lower()
        if not name:
            continue
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count > 0:
            out[name] = out.get(name, 0) + int(raw_count)
    return out


def _validate_strict_items(items: Any) -> Optional[dict[str, int]]:
    if not isinstance(items, Mapping):
        return None
    out: dict[str, int] = {}
    for raw_name, raw_count in items.items():
        if not isinstance(raw_name, str):
            return None
        name = bare(raw_name).strip().lower()
        if not name:
            return None
        if not isinstance(raw_count, int) or isinstance(raw_count, bool) or raw_count <= 0:
            return None
        out[name] = out.get(name, 0) + int(raw_count)
    return out


_CAMP_CHEST_ROLES: tuple[str, ...] = ("kit", "vault", "bulk")


def _copy_camp(camp: Mapping[str, Any]) -> dict[str, Any]:
    chests_raw = camp.get("chests") if isinstance(camp.get("chests"), Mapping) else {}
    chests = {
        str(role): list(pos)
        for role, pos in chests_raw.items()
        if str(role) in _CAMP_CHEST_ROLES and pos is not None
    }
    chest_items_raw = camp.get("chest_items") if isinstance(camp.get("chest_items"), Mapping) else {}
    chest_items = {
        str(role): dict(items)
        for role, items in chest_items_raw.items()
        if str(role) in _CAMP_CHEST_ROLES and isinstance(items, Mapping)
    }
    return {
        "pos": list(camp["pos"]),
        "chest": list(camp["chest"]) if camp.get("chest") is not None else None,
        "bed": list(camp["bed"]) if camp.get("bed") is not None else None,
        "furnace": list(camp["furnace"]) if camp.get("furnace") is not None else None,
        "crafting_table": list(camp["crafting_table"]) if camp.get("crafting_table") is not None else None,
        "stored_items": dict(camp.get("stored_items", {})),
        "shelter_tier": int(camp.get("shelter_tier", 0) or 0),
        "chests": chests,
        "chest_items": chest_items,
        "mine_entry": list(camp["mine_entry"]) if camp.get("mine_entry") is not None else None,
        "kit_depleted": bool(camp.get("kit_depleted", False)),
    }


def _copy_death(death: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "pos": list(death["pos"]),
        "cause": str(death["cause"]),
        "lost_items": dict(death.get("lost_items", {})),
        "recorded_at": float(death["recorded_at"]),
    }


_CARDINAL_HEADINGS: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"非法{label}: {value!r}")
    return int(value)


def _parse_heading(value: Any) -> Optional[list[int]]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        return None
    dx, dz = int(value[0]), int(value[1])
    return [dx, dz] if (dx, dz) in _CARDINAL_HEADINGS else None


def _copy_shaft(shaft: Mapping[str, Any]) -> dict[str, Any]:
    raw_bi = shaft.get("branch_index")
    branch_index = {str(k): int(v) for k, v in raw_bi.items()} if isinstance(raw_bi, Mapping) else {}
    out: dict[str, Any] = {
        "entry": [int(v) for v in shaft["entry"]],
        "lowest_y": int(shaft["lowest_y"]),
        "lowest_pos": [int(v) for v in shaft["lowest_pos"]],
        "heading": [int(v) for v in shaft["heading"]],
        "branch_index": branch_index,
    }
    raw_seals = shaft.get("seal_cells")
    if isinstance(raw_seals, Sequence) and not isinstance(raw_seals, (str, bytes)):
        parsed_seals = [[int(v) for v in c[:3]] for c in raw_seals if isinstance(c, (list, tuple)) and len(c) >= 3]
        if parsed_seals:
            out["seal_cells"] = parsed_seals
    if shaft.get("sealed") is True:
        out["sealed"] = True
    return out


def _is_recovery_kit_item(name: str) -> bool:
    b = bare(name).strip().lower()
    if not b:
        return False
    return (
        b == "shield"
        or b == "turtle_helmet"
        or b in _EDIBLE_RECOVERY_FOODS
        or any(
            len(b) > len(sfx) and b.endswith(sfx)
            for sfx in ("_pickaxe", "_sword", "_helmet", "_chestplate", "_leggings", "_boots")
        )
    )


def _is_valuable_lost_item(name: str) -> bool:
    b = bare(name).strip().lower()
    if not b:
        return False
    return b.startswith(("iron_", "diamond", "netherite_")) or b in _CORPSE_VALUABLE_ITEMS


_DEFAULT_CORPSE_MAX_DIST = 80.0
_DEFAULT_CORPSE_MAX_AGE_S = 240.0


def _env_limit(name: str, default: float) -> float:
    """读取捡尸窗口的环境变量覆盖值；空值、非数字、非正数一律回落到默认值。"""
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return float(default)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(val) or val <= 0.0:
        return float(default)
    return val


def corpse_recovery_limits() -> tuple[float, float]:
    """返回当前生效的（最大距离, 最大时效秒数），支持 `MC_CORPSE_MAX_DIST`/`MC_CORPSE_MAX_AGE_S` 覆盖。"""
    return (
        _env_limit("MC_CORPSE_MAX_DIST", _DEFAULT_CORPSE_MAX_DIST),
        _env_limit("MC_CORPSE_MAX_AGE_S", _DEFAULT_CORPSE_MAX_AGE_S),
    )


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


def entity_landmark_tags(entity_name: str) -> tuple[str, ...]:
    """根据实体名称映射对应的规范结构/群系地标标签（单点真相源，供 survey 与 roadside 共用）。"""
    ename = bare(entity_name).strip().lower() if isinstance(entity_name, str) else ""
    return ENTITY_LANDMARK_TAGS.get(ename, ()) if ename else ()


def _biome_tag_matches(alt: str, candidate: str) -> bool:
    """支持精确群系/地标名与子变种前缀/后缀匹配（如 savanna -> windswept_savanna / savanna_plateau）。"""
    c = bare(candidate).strip().lower()
    a = bare(alt).strip().lower()
    if not c or not a:
        return False
    return c == a or c.startswith(f"{a}_") or c.endswith(f"_{a}") or f"_{a}_" in c


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
        self._station_meta: dict[tuple[str, str, Cell], dict[str, Any]] = {}
        self._landmarks: dict[tuple[str, str], list[tuple[Cell, dict[str, Any]]]] = {}
        self._portals: dict[str, list[Cell]] = {}
        self._blocked_regions: dict[tuple[str, tuple[int, ...]], float] = {}
        self.visited_biomes: dict[str, set[str]] = {}
        self.survey_sectors_done: dict[str, set[int]] = {}
        self._camps: dict[str, dict[str, Any]] = {}
        self._mine_shafts: dict[str, dict[str, Any]] = {}
        self._last_deaths: dict[str, dict[str, Any]] = {}
        self._spawn_pos: dict[str, Cell] = {}
        self._visited_grid: dict[str, set[tuple[int, int]]] = {}
        self._spiral_cursor: dict[str, dict[str, int]] = {}
        self._spiral_rings_completed: dict[str, int] = {}
        self._void_edges: dict[str, list[tuple[int, int]]] = {}
        self.landmark_version: int = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._stations.clear()
            self._station_meta.clear()
            self._landmarks.clear()
            self._portals.clear()
            self._blocked_regions.clear()
            self.visited_biomes.clear()
            self.survey_sectors_done.clear()
            self._camps.clear()
            self._mine_shafts.clear()
            self._last_deaths.clear()
            self._spawn_pos.clear()
            self._visited_grid.clear()
            self._spiral_cursor.clear()
            self._spiral_rings_completed.clear()
            self._void_edges.clear()
            self.landmark_version = 0

    def set_spawn(self, pos: Any, *, dimension: str = "overworld", overwrite: bool = False) -> list[int]:
        """锚定指定维度的首次出生点坐标（单局默认不覆盖），供保护区外缘选址与重生测距使用。"""
        dim = _norm_dim(dimension)
        cell = block_cell(pos)
        with self._lock:
            if overwrite or dim not in self._spawn_pos:
                self._spawn_pos[dim] = cell
            return list(self._spawn_pos[dim])

    def get_spawn(self, *, dimension: str = "overworld") -> Optional[list[int]]:
        """返回指定维度已锚定的出生点坐标副本，未记录时返回 `None`。"""
        dim = _norm_dim(dimension)
        with self._lock:
            cell = self._spawn_pos.get(dim)
            return list(cell) if cell is not None else None

    @property
    def spawn_pos(self) -> Optional[list[int]]:
        """主世界已锚定的出生点坐标副本。"""
        return self.get_spawn(dimension="overworld")

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

    @staticmethod
    def _station_kind_matches(stored_kind: str, query_kind: str) -> bool:
        return (
            stored_kind == query_kind
            or (query_kind == "bed" and stored_kind.endswith("_bed"))
            or (query_kind == "furnace" and stored_kind in ("blast_furnace", "smoker"))
            or (query_kind == "anvil" and stored_kind in ("chipped_anvil", "damaged_anvil"))
            or (query_kind == "chest" and stored_kind in ("trapped_chest", "barrel"))
        )

    def _is_near_camp_locked(self, cell: Cell, dim: str, *, radius: float = 10.0) -> bool:
        camp = self._camps.get(dim)
        if not isinstance(camp, Mapping):
            return False
        cpos = camp.get("pos")
        if isinstance(cpos, (list, tuple)) and len(cpos) >= 3:
            try:
                if math.dist(cell, (float(cpos[0]), float(cpos[1]), float(cpos[2]))) <= radius:
                    return True
            except (TypeError, ValueError):
                pass
        for field_k in ("chest", "bed", "furnace", "crafting_table"):
            fpos = camp.get(field_k)
            if isinstance(fpos, (list, tuple)) and len(fpos) >= 3:
                try:
                    if math.dist(cell, (float(fpos[0]), float(fpos[1]), float(fpos[2]))) <= 1.5:
                        return True
                except (TypeError, ValueError):
                    pass
        return False

    def _record_station_locked(
        self, kind: str, cell: Cell, dim: str, *, scope: Optional[str] = None, status: str = "active",
    ) -> None:
        k = bare(kind).strip().lower()
        if not k:
            raise ValueError(f"非法工作站类型: {kind!r}")
        now = float(self._clock())
        cells = self._stations.setdefault((dim, k), [])
        target_cell = cell
        for existing in cells:
            if math.dist(cell, existing) <= 1.5:
                target_cell = existing
                break
        else:
            cells.append(cell)
        meta_key = (dim, k, target_cell)
        prev = self._station_meta.get(meta_key)
        inferred_scope = (
            scope
            if scope in ("camp", "field")
            else ("camp" if (prev and prev.get("scope") == "camp") or self._is_near_camp_locked(target_cell, dim) else "field")
        )
        self._station_meta[meta_key] = {
            "scope": inferred_scope,
            "placed_at": float(prev["placed_at"]) if (prev and "placed_at" in prev) else now,
            "verified_at": now,
            "status": str(status or "active"),
        }

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

    def record_station(
        self, kind: str, pos: Any, *, dimension: str = "overworld", scope: Optional[str] = None,
    ) -> None:
        """记录工作站坐标，同维度同类型 1.5 格内去重，并登记 camp/field 标签与核验时间。"""
        k = bare(kind).strip().lower()
        if not k:
            raise ValueError(f"非法工作站类型: {kind!r}")
        if scope is not None and scope not in ("camp", "field"):
            raise ValueError(f"非法工作站作用域: {scope!r}")
        dim, cell = _norm_dim(dimension), block_cell(pos)
        with self._lock:
            self._record_station_locked(k, cell, dim, scope=scope)

    def remove_station(self, kind: Optional[str], pos: Any, *, dimension: str = "overworld") -> bool:
        """从工作站台账销账（主动回收或现场核验已消失/被毁时调用）。"""
        dim, cell = _norm_dim(dimension), block_cell(pos)
        k = bare(kind).strip().lower() if kind is not None else None
        if kind is not None and not k:
            return False
        removed = False
        with self._lock:
            for (d, sk) in list(self._stations.keys()):
                if d != dim:
                    continue
                if k is not None and not self._station_kind_matches(sk, k):
                    continue
                cells = self._stations.get((d, sk)) or []
                kept: list[Cell] = []
                for existing in cells:
                    if math.dist(cell, existing) <= 1.5:
                        removed = True
                        self._station_meta.pop((d, sk, existing), None)
                    else:
                        kept.append(existing)
                if kept:
                    self._stations[(d, sk)] = kept
                else:
                    self._stations.pop((d, sk), None)
            # 若普通资源台账 _entries 中也登记了该位置的工作站，一并标为 depleted 防脏复访
            entry = self._entries.get((dim, "station", cell))
            if entry is not None:
                entry.status = "depleted"
                entry.updated_at = float(self._clock())
            # 同步清理 camp 中与被拆床位/工作站完全一致的坐标字段
            camp = self._camps.get(dim)
            if isinstance(camp, dict):
                for field_k, family in (("bed", "bed"), ("furnace", "furnace"), ("crafting_table", "crafting_table")):
                    fpos = camp.get(field_k)
                    if (
                        isinstance(fpos, (list, tuple))
                        and len(fpos) >= 3
                        and (k is None or self._station_kind_matches(k, family) or self._station_kind_matches(family, k))
                        and math.dist(cell, (float(fpos[0]), float(fpos[1]), float(fpos[2]))) <= 1.5
                    ):
                        camp[field_k] = None
        return removed

    def get_station_meta(
        self, kind: Optional[str], pos: Any, *, dimension: str = "overworld",
    ) -> Optional[dict[str, Any]]:
        """查询工作站的元数据副本（scope / placed_at / verified_at / status）。"""
        dim, cell = _norm_dim(dimension), block_cell(pos)
        k = bare(kind).strip().lower() if kind is not None else None
        with self._lock:
            for (d, sk, scell), meta in self._station_meta.items():
                if d != dim:
                    continue
                if k is not None and not self._station_kind_matches(sk, k):
                    continue
                if math.dist(cell, scell) <= 1.5:
                    return dict(meta)
        return None

    def is_camp_station(self, kind: Optional[str], pos: Any, *, dimension: str = "overworld") -> bool:
        """判断指定工作站是否属于基地常驻设施（scope=='camp' 或紧邻基地锚点）。"""
        dim, cell = _norm_dim(dimension), block_cell(pos)
        with self._lock:
            meta = self.get_station_meta(kind, cell, dimension=dim)
            if meta is not None and meta.get("scope") == "camp":
                return True
            return self._is_near_camp_locked(cell, dim)

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
                if k is not None and not self._station_kind_matches(sk, k):
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

    def record_biome(self, biome: str, *, pos: Any = None, dimension: str = "overworld") -> bool:
        """记录已访问生物群系（按维度隔离）；提供 `pos` 时同步登记群系锚点，首次发现递增 `landmark_version`。"""
        b = bare(biome).strip().lower() if isinstance(biome, str) else ""
        if not b:
            return False
        dim = _norm_dim(dimension)
        cell: Optional[Cell] = None
        if pos is not None:
            try:
                cell = block_cell(pos)
            except Exception:
                cell = None
        with self._lock:
            biomes = self.visited_biomes.setdefault(dim, set())
            is_new = b not in biomes
            if is_new:
                biomes.add(b)
            if cell is not None:
                items = self._landmarks.setdefault((dim, b), [])
                if len(items) < 4 and not any(math.dist(cell, existing_cell) <= 32.0 for existing_cell, _ in items):
                    items.append((cell, {"biome": b, "source": "biome"}))
            if is_new:
                self.landmark_version += 1
            return is_new

    def biomes(self, *, dimension: str = "overworld") -> set[str]:
        """返回指定维度已访问生物群系集合的副本。"""
        dim = _norm_dim(dimension)
        with self._lock:
            return set(self.visited_biomes.get(dim, ()))

    def biome_or_landmark_positions(self, req: str, *, dimension: str = "overworld") -> list[tuple[int, int, int]]:
        """按地标或群系表达（支持 `a|b` 与群系子变种前缀/后缀匹配）查询已记录锚点坐标。"""
        alts = [bare(a).strip().lower() for a in str(req).split("|") if bare(a).strip()]
        if not alts:
            return []
        dim = _norm_dim(dimension)
        with self._lock:
            out: list[tuple[int, int, int]] = []
            seen: set[Cell] = set()
            for (d, lk), items in self._landmarks.items():
                if d != dim:
                    continue
                if any(_biome_tag_matches(alt, lk) for alt in alts):
                    for cell, _ in items:
                        if cell not in seen:
                            seen.add(cell)
                            out.append(tuple(cell))
            return out

    def is_island_survey_exhausted(self, *, dimension: str = "overworld") -> bool:
        """判断指定维度虚空岛是否已探尽（完成全部螺旋环，或已在 >=6 个方位扇区触达虚空边界且至少完成 4 个扇区）。"""
        dim = _norm_dim(dimension)
        with self._lock:
            rings_done = int(self._spiral_rings_completed.get(dim, (self._spiral_cursor.get(dim) or {}).get("ring", 0)))
            if rings_done >= len(SPIRAL_RING_RADII):
                return True
            edges = self._void_edges.get(dim, ())
            sectors_done = self.survey_sectors_done.get(dim, ())
            if len(edges) >= 6 and (len(sectors_done) >= 4 or rings_done >= 1):
                sp = self._spawn_pos.get(dim)
                sx, sz = (float(sp[0]), float(sp[2])) if sp is not None else (0.0, 0.0)
                octants = {
                    int(((math.atan2(ez - sz, ex - sx) + math.pi) / (2.0 * math.pi)) * 8.0) % 8
                    for ex, ez in edges
                }
                if len(octants) >= 6:
                    return True
            return False

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

    def mark_grid_visited(self, pos: Any, *, dimension: str = "overworld") -> tuple[int, int]:
        """将给定坐标所在的 32×32 粗网格单元标记为已访问并返回单元索引 `(gx, gz)`。"""
        dim = _norm_dim(dimension)
        x, z = _extract_xz(pos)
        cell = (math.floor(x) // 32, math.floor(z) // 32)
        with self._lock:
            self._visited_grid.setdefault(dim, set()).add(cell)
            return cell

    def visited_grid_cells(self, *, dimension: str = "overworld") -> set[tuple[int, int]]:
        """返回指定维度已访问 32×32 粗网格单元集合的防御性副本。"""
        dim = _norm_dim(dimension)
        with self._lock:
            return set(self._visited_grid.get(dim, ()))

    def is_grid_visited(self, pos: Any, *, dimension: str = "overworld") -> bool:
        """判断给定坐标所在的 32×32 粗网格单元在指定维度是否已访问。"""
        dim = _norm_dim(dimension)
        x, z = _extract_xz(pos)
        cell = (math.floor(x) // 32, math.floor(z) // 32)
        with self._lock:
            return cell in self._visited_grid.get(dim, ())

    def get_spiral_cursor(self, *, dimension: str = "overworld") -> dict[str, int]:
        """返回指定维度当前螺旋环探索游标 `{"ring", "sector", "radius"}`。"""
        dim = _norm_dim(dimension)
        with self._lock:
            cur = self._spiral_cursor.get(dim) or {}
            ring = int(cur.get("ring", 0))
            sector = int(cur.get("sector", 0))
            return {"ring": ring, "sector": sector, "radius": SPIRAL_RING_RADII[ring]}

    def advance_spiral_cursor(self, *, dimension: str = "overworld", sectors_per_ring: int = 8) -> dict[str, int]:
        """推进指定维度的螺旋环探索游标并在完成整环时递增已完成环数与外扩环索引。"""
        dim = _norm_dim(dimension)
        n_sec = max(1, int(sectors_per_ring))
        with self._lock:
            cur = self._spiral_cursor.get(dim) or {}
            ring = int(cur.get("ring", 0))
            sector = int(cur.get("sector", 0))
            sector += 1
            if sector >= n_sec:
                sector = 0
                self._spiral_rings_completed[dim] = max(self._spiral_rings_completed.get(dim, ring), ring) + 1
                ring = min(ring + 1, len(SPIRAL_RING_RADII) - 1)
            self._spiral_cursor[dim] = {"ring": ring, "sector": sector}
            return {"ring": ring, "sector": sector, "radius": SPIRAL_RING_RADII[ring]}

    def spiral_rings_completed(self, *, dimension: str = "overworld") -> int:
        """返回指定维度已完成的螺旋环数。"""
        dim = _norm_dim(dimension)
        with self._lock:
            return int(self._spiral_rings_completed.get(dim, (self._spiral_cursor.get(dim) or {}).get("ring", 0)))

    def record_void_edge(self, pos: Any, *, dimension: str = "overworld") -> bool:
        """记录虚空/悬崖边界点（每维度上限 64 点，12 格内去重）。"""
        dim = _norm_dim(dimension)
        x, z = _extract_xz(pos)
        pt = (math.floor(x), math.floor(z))
        with self._lock:
            edges = self._void_edges.get(dim, ())
            if len(edges) >= 64 or any(math.hypot(pt[0] - ex, pt[1] - ez) <= 12.0 for ex, ez in edges):
                return False
            self._void_edges.setdefault(dim, []).append(pt)
            return True

    def void_edges(self, *, dimension: str = "overworld") -> list[tuple[int, int]]:
        """返回指定维度已记录虚空边界点列表的防御性副本。"""
        dim = _norm_dim(dimension)
        with self._lock:
            return list(self._void_edges.get(dim, ()))

    def is_near_void_edge(self, pos: Any, *, dimension: str = "overworld", margin: float = 12.0) -> bool:
        """判断给定平面位置是否靠近已记录的虚空边界点。"""
        try:
            x, z = _extract_xz(pos)
        except ValueError:
            return False
        dim = _norm_dim(dimension)
        lim = float(margin)
        with self._lock:
            return any(math.hypot(x - ex, z - ez) < lim for ex, ez in self._void_edges.get(dim, ()))

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

    def _camp_has_recovery_kit_locked(self, dim: str) -> bool:
        camp = self._camps.get(dim)
        if camp is None or (camp.get("chest") is None and not camp.get("chests")):
            return False
        if any(count > 0 and _is_recovery_kit_item(name) for name, count in camp.get("stored_items", {}).items()):
            return True
        for items in (camp.get("chest_items") or {}).values():
            if any(count > 0 and _is_recovery_kit_item(name) for name, count in items.items()):
                return True
        return False

    def set_camp(
        self,
        pos: Any,
        *,
        dimension: str = "overworld",
        chest: Any = None,
        bed: Any = None,
        furnace: Any = None,
        crafting_table: Any = None,
        stored_items: Optional[Mapping[str, int]] = None,
        shelter_tier: Optional[int] = None,
        chests: Optional[Mapping[str, Any]] = None,
        mine_entry: Any = None,
        kit_depleted: Optional[bool] = None,
        chest_role: Optional[str] = None,
    ) -> dict[str, Any]:
        """创建或更新指定维度的主营地档案（兼容旧单箱与三区仓库），同步登记非空工作站并返回防御性副本。"""
        dim = _norm_dim(dimension)
        pos_cell = block_cell(pos)
        station_updates: dict[str, Cell] = {}
        for kind, raw_val in (
            ("chest", chest),
            ("bed", bed),
            ("furnace", furnace),
            ("crafting_table", crafting_table),
        ):
            if raw_val is not None:
                station_updates[kind] = block_cell(raw_val)
        chest_updates: dict[str, Cell] = {}
        if isinstance(chests, Mapping):
            for role, raw_pos in chests.items():
                rname = str(role).strip().lower()
                if rname in _CAMP_CHEST_ROLES and raw_pos is not None:
                    chest_updates[rname] = block_cell(raw_pos)
        norm_role = str(chest_role).strip().lower() if chest_role else None
        if norm_role in _CAMP_CHEST_ROLES and "chest" in station_updates:
            chest_updates[norm_role] = station_updates["chest"]
        mine_cell = block_cell(mine_entry) if mine_entry is not None else None
        if shelter_tier is not None and (isinstance(shelter_tier, bool) or int(shelter_tier) not in (0, 1, 2)):
            raise ValueError(f"shelter_tier 必须为 0/1/2；收到 {shelter_tier!r}")
        with self._lock:
            existing = self._camps.get(dim)
            camp = _copy_camp(existing) if existing is not None else {
                "pos": list(pos_cell),
                "chest": None,
                "bed": None,
                "furnace": None,
                "crafting_table": None,
                "stored_items": {},
                "shelter_tier": 0,
                "chests": {},
                "chest_items": {},
                "mine_entry": None,
                "kit_depleted": False,
            }
            camp["pos"] = list(pos_cell)
            for kind, cell in station_updates.items():
                camp[kind] = list(cell)
                self._record_station_locked(kind, cell, dim, scope="camp")
            for rname, cell in chest_updates.items():
                camp["chests"][rname] = list(cell)
                self._record_station_locked("chest", cell, dim, scope="camp")
            if "chest" in station_updates and "kit" not in camp["chests"] and norm_role is None:
                camp["chests"]["kit"] = list(station_updates["chest"])
            if camp["chest"] is None and camp["chests"]:
                primary = camp["chests"].get("kit") or next(iter(camp["chests"].values()))
                camp["chest"] = list(primary)
            if shelter_tier is not None:
                camp["shelter_tier"] = max(int(camp.get("shelter_tier", 0)), int(shelter_tier))
            if mine_cell is not None:
                camp["mine_entry"] = list(mine_cell)
            if kit_depleted is not None:
                camp["kit_depleted"] = bool(kit_depleted)
            if stored_items is not None:
                normalized = _normalize_stored_items(stored_items)
                if norm_role in _CAMP_CHEST_ROLES:
                    camp["chest_items"][norm_role] = normalized
                    merged: dict[str, int] = {}
                    for bucket in camp["chest_items"].values():
                        for k, v in bucket.items():
                            merged[k] = merged.get(k, 0) + v
                    camp["stored_items"] = merged
                else:
                    camp["stored_items"] = normalized
            self._camps[dim] = camp
            return _copy_camp(camp)

    def get_camp(self, *, dimension: str = "overworld") -> Optional[dict[str, Any]]:
        """返回指定维度主营地档案的防御性副本，不存在时返回 `None`。"""
        dim = _norm_dim(dimension)
        with self._lock:
            camp = self._camps.get(dim)
            return _copy_camp(camp) if camp is not None else None

    def update_camp_stored(
        self,
        stored_items: Mapping[str, int],
        *,
        dimension: str = "overworld",
        role: Optional[str] = None,
    ) -> None:
        """更新指定维度主营地的箱内存储物品清单（支持按 kit/vault/bulk 角色聚合）。"""
        dim = _norm_dim(dimension)
        normalized = _normalize_stored_items(stored_items)
        norm_role = str(role).strip().lower() if role else None
        with self._lock:
            camp = self._camps.get(dim)
            if camp is None:
                return
            if norm_role in _CAMP_CHEST_ROLES:
                camp.setdefault("chest_items", {})[norm_role] = normalized
                merged: dict[str, int] = {}
                for bucket in camp["chest_items"].values():
                    for k, v in bucket.items():
                        merged[k] = merged.get(k, 0) + v
                camp["stored_items"] = merged
            else:
                camp["stored_items"] = normalized

    def set_camp_kit_depleted(self, depleted: bool, *, dimension: str = "overworld") -> None:
        """设置指定维度营地热备是否耗尽标记。"""
        dim = _norm_dim(dimension)
        with self._lock:
            camp = self._camps.get(dim)
            if camp is not None:
                camp["kit_depleted"] = bool(depleted)

    def camp_has_recovery_kit(self, *, dimension: str = "overworld") -> bool:
        """判断指定维度主营地箱子中是否存有基础恢复物资（镐/剑/盾/护甲/可食用食物）。"""
        dim = _norm_dim(dimension)
        with self._lock:
            return self._camp_has_recovery_kit_locked(dim)

    def get_mine_shaft(self, *, dimension: str = "overworld") -> Optional[dict[str, Any]]:
        """返回指定维度主矿井档案的防御性副本，未记录时返回 `None`。"""
        dim = _norm_dim(dimension)
        with self._lock:
            shaft = self._mine_shafts.get(dim)
            return _copy_shaft(shaft) if shaft is not None else None

    @property
    def primary_mine_shaft(self) -> Optional[dict[str, Any]]:
        """主世界主矿井档案的防御性副本，未记录时返回 `None`。"""
        return self.get_mine_shaft(dimension="overworld")

    def set_mine_shaft(
        self,
        entry: Any,
        *,
        dimension: str = "overworld",
        lowest_y: Optional[int] = None,
        lowest_pos: Any = None,
        heading: Sequence[int] = (1, 0),
        branch_index: Optional[Mapping[str, int]] = None,
    ) -> dict[str, Any]:
        """创建或覆盖指定维度的主矿井档案并返回防御性副本。"""
        dim = _norm_dim(dimension)
        entry_cell = block_cell(entry)
        low_cell = block_cell(lowest_pos) if lowest_pos is not None else entry_cell
        low_y = _require_int(lowest_y, "最低Y坐标") if lowest_y is not None else int(low_cell[1])
        head = _parse_heading(heading)
        if head is None:
            raise ValueError(f"非法矿井朝向: {heading!r}")
        norm_branches: dict[str, int] = {}
        if branch_index is not None:
            if not isinstance(branch_index, Mapping):
                raise ValueError(f"非法支道索引映射: {branch_index!r}")
            for raw_k, raw_v in branch_index.items():
                if isinstance(raw_k, bool) or isinstance(raw_k, float):
                    raise ValueError(f"非法支道层键: {raw_k!r}")
                try:
                    lvl = int(raw_k)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"非法支道层键: {raw_k!r}") from exc
                cnt = _require_int(raw_v, "支道计数")
                if cnt < 0:
                    raise ValueError(f"非法支道计数: {raw_v!r}")
                norm_branches[str(lvl)] = cnt
        shaft: dict[str, Any] = {
            "entry": list(entry_cell),
            "lowest_y": low_y,
            "lowest_pos": list(low_cell),
            "heading": head,
            "branch_index": norm_branches,
        }
        with self._lock:
            self._mine_shafts[dim] = shaft
            return _copy_shaft(shaft)

    def update_mine_shaft(
        self,
        *,
        dimension: str = "overworld",
        lowest_y: Optional[int] = None,
        lowest_pos: Any = None,
        heading: Optional[Sequence[int]] = None,
        branch_level: Optional[int] = None,
        branch_idx: Optional[int] = None,
        seal_cells: Optional[Sequence[Any]] = None,
        sealed: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        """增量更新已存在的指定维度主矿井档案（仅深化最低深度，并支持井口封堵坐标登记），不存在时返回 `None`。"""
        dim = _norm_dim(dimension)
        new_low_y = _require_int(lowest_y, "最低Y坐标") if lowest_y is not None else None
        lp = block_cell(lowest_pos) if lowest_pos is not None else None
        parsed_heading: Optional[list[int]] = None
        if heading is not None:
            parsed_heading = _parse_heading(heading)
            if parsed_heading is None:
                raise ValueError(f"非法矿井朝向: {heading!r}")
        parsed_branch_level: Optional[int] = None
        parsed_branch_idx: Optional[int] = None
        if branch_level is not None and branch_idx is not None:
            parsed_branch_level = _require_int(branch_level, "支道层Y坐标")
            parsed_branch_idx = _require_int(branch_idx, "支道索引")
            if parsed_branch_idx < 0:
                raise ValueError(f"非法支道索引: {branch_idx!r}")
        parsed_seals: Optional[list[list[int]]] = None
        if seal_cells is not None:
            parsed_seals = [list(block_cell(c)) for c in seal_cells]
        with self._lock:
            existing = self._mine_shafts.get(dim)
            if existing is None:
                return None
            if new_low_y is not None and new_low_y < int(existing["lowest_y"]):
                existing["lowest_y"] = new_low_y
                if lp is not None:
                    existing["lowest_pos"] = list(lp)
            elif lp is not None and lp[1] < min(int(existing["lowest_y"]), int(existing["lowest_pos"][1])):
                existing["lowest_pos"] = list(lp)
                existing["lowest_y"] = int(lp[1])
            elif lp is not None and lp[1] < int(existing["lowest_pos"][1]):
                existing["lowest_pos"] = list(lp)
            if parsed_heading is not None:
                existing["heading"] = parsed_heading
            if parsed_branch_level is not None and parsed_branch_idx is not None:
                existing["branch_index"][str(parsed_branch_level)] = parsed_branch_idx
            if parsed_seals is not None:
                if parsed_seals:
                    existing["seal_cells"] = parsed_seals
                else:
                    existing.pop("seal_cells", None)
            if sealed is not None:
                if sealed:
                    existing["sealed"] = True
                else:
                    existing.pop("sealed", None)
            return _copy_shaft(existing)

    def next_mine_branch(self, level_y: int, *, dimension: str = "overworld") -> int:
        """分配并递增指定层高的下一个支道索引，若矿井档案不存在则自动初始化默认档案。"""
        lvl = _require_int(level_y, "矿井层高")
        dim = _norm_dim(dimension)
        key = str(lvl)
        with self._lock:
            shaft = self._mine_shafts.get(dim)
            if shaft is None:
                shaft = {
                    "entry": [0, lvl, 0],
                    "lowest_y": lvl,
                    "lowest_pos": [0, lvl, 0],
                    "heading": [1, 0],
                    "branch_index": {},
                }
                self._mine_shafts[dim] = shaft
            idx = int(shaft["branch_index"].get(key, 0))
            shaft["branch_index"][key] = idx + 1
            return idx

    def record_death(
        self,
        pos: Any,
        *,
        cause: str = "",
        lost_items: Optional[Mapping[str, int]] = None,
        dimension: str = "overworld",
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """记录指定维度的最近一次死亡位置、死因、掉落物品与时间戳，返回防御性副本。"""
        dim = _norm_dim(dimension)
        cell = list(block_cell(pos))
        cause_str = str(cause or "").strip().lower()
        items = _normalize_stored_items(lost_items or {})
        with self._lock:
            raw_time = self._clock() if now is None else now
            recorded_at = _finite_float(raw_time)
            if recorded_at is None:
                raise ValueError(f"非法死亡时间戳: {raw_time!r}")
            death: dict[str, Any] = {
                "pos": cell,
                "cause": cause_str,
                "lost_items": items,
                "recorded_at": recorded_at,
            }
            self._last_deaths[dim] = death
            return _copy_death(death)

    def get_last_death(self, *, dimension: str = "overworld") -> Optional[dict[str, Any]]:
        """返回指定维度最近一次死亡记录的防御性副本，不存在时返回 `None`。"""
        dim = _norm_dim(dimension)
        with self._lock:
            death = self._last_deaths.get(dim)
            return _copy_death(death) if death is not None else None

    def clear_last_death(self, *, dimension: str = "overworld") -> None:
        """清除指定维度的最近死亡记录。"""
        dim = _norm_dim(dimension)
        with self._lock:
            self._last_deaths.pop(dim, None)

    def should_attempt_corpse_recovery(
        self,
        cur_pos: Any,
        *,
        dimension: str = "overworld",
        now: Optional[float] = None,
        max_dist: Optional[float] = None,
        max_age_s: Optional[float] = None,
    ) -> tuple[bool, str]:
        """判断是否应当尝试可控捡尸：校验记录存在、非岩浆/虚空/火焰死因、时效、距离与铁级以上高价值掉落。

        `max_dist`/`max_age_s` 省略时取 `corpse_recovery_limits()`（默认 80 格 / 240s，
        可用环境变量 `MC_CORPSE_MAX_DIST`/`MC_CORPSE_MAX_AGE_S` 放宽）。
        """
        if max_dist is None or max_age_s is None:
            env_dist, env_age = corpse_recovery_limits()
            max_dist = env_dist if max_dist is None else max_dist
            max_age_s = env_age if max_age_s is None else max_age_s
        dim = _norm_dim(dimension)
        with self._lock:
            death = self._last_deaths.get(dim)
            if death is None:
                return (False, "no_death_record")
            cause = str(death.get("cause", "")).strip().lower()
            if cause == "fire" or any(kw in cause for kw in _UNRECOVERABLE_DEATH_CAUSE_KEYWORDS):
                return (False, "unrecoverable_cause")
            raw_time = self._clock() if now is None else now
            cur_time = _finite_float(raw_time)
            if cur_time is None:
                raise ValueError(f"非法时间戳: {raw_time!r}")
            if (cur_time - float(death["recorded_at"])) > float(max_age_s):
                return (False, "expired")
            cur_pt = point3(cur_pos)
            death_pt = tuple(float(v) for v in death["pos"])
            if math.dist(cur_pt, death_pt) >= float(max_dist):
                return (False, "too_far")
            has_valuables = any(
                count > 0 and _is_valuable_lost_item(name)
                for name, count in death.get("lost_items", {}).items()
            )
            if not has_valuables:
                return (False, "no_valuable_items")
            return (True, "eligible")

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
            if dim in self._spawn_pos:
                summary["has_spawn_anchor"] = True
            if camp := self._camps.get(dim):
                summary["has_camp"] = True
                summary["camp_has_chest"] = camp.get("chest") is not None or bool(camp.get("chests"))
                summary["camp_has_recovery_kit"] = self._camp_has_recovery_kit_locked(dim)
                summary["camp_stored_count"] = sum(camp["stored_items"].values())
                summary["camp_shelter_tier"] = int(camp.get("shelter_tier", 0) or 0)
                if camp.get("chests"):
                    summary["camp_chest_roles"] = sorted(camp["chests"].keys())
                if camp.get("kit_depleted"):
                    summary["camp_kit_depleted"] = True
            if shaft := self._mine_shafts.get(dim):
                summary["has_mine_shaft"] = True
                summary["mine_shaft_lowest_y"] = int(shaft["lowest_y"])
            if grid := self._visited_grid.get(dim):
                summary["visited_grid_count"] = len(grid)
            if (cursor := self._spiral_cursor.get(dim)) and int(cursor.get("ring", 0)) > 0:
                summary["spiral_ring"] = int(cursor["ring"])
            if edges := self._void_edges.get(dim):
                summary["void_edges_count"] = len(edges)
            if self.is_island_survey_exhausted(dimension=dim):
                summary["island_survey_exhausted"] = True
            return summary

    def save_checkpoint(self, path: str | os.PathLike[str]) -> None:
        """原子保存台账检查点为 UTF-8 JSON（`schema_version=1`）。"""
        target = Path(path)
        with self._lock:
            stations_out: dict[str, dict[str, list[list[int]]]] = {}
            for (dim, kind), cells in self._stations.items():
                if cells:
                    stations_out.setdefault(dim, {})[kind] = [list(c) for c in cells]
            station_meta_out: list[dict[str, Any]] = [
                {
                    "dimension": dim,
                    "kind": kind,
                    "pos": list(cell),
                    "scope": str(meta.get("scope") or "field"),
                    "placed_at": float(meta.get("placed_at", 0.0)),
                    "verified_at": float(meta.get("verified_at", 0.0)),
                    "status": str(meta.get("status") or "active"),
                }
                for (dim, kind, cell), meta in self._station_meta.items()
            ]
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
                "station_meta": station_meta_out,
                "landmarks": landmarks_out,
                "portals": portals_out,
                "resources": resources_out,
                "visited_biomes": {dim: sorted(bs) for dim, bs in self.visited_biomes.items() if bs},
                "survey_sectors_done": {dim: sorted(ss) for dim, ss in self.survey_sectors_done.items() if ss},
                "camps": {dim: _copy_camp(camp) for dim, camp in self._camps.items()},
                "primary_mine_shafts": {dim: _copy_shaft(s) for dim, s in self._mine_shafts.items()},
                "last_deaths": {dim: _copy_death(death) for dim, death in self._last_deaths.items()},
                "spawn_positions": {dim: list(cell) for dim, cell in self._spawn_pos.items()},
                "visited_grid": {dim: [list(c) for c in sorted(cells)] for dim, cells in self._visited_grid.items() if cells},
                "spiral_cursor": {
                    dim: {"ring": int(cur["ring"]), "sector": int(cur["sector"])}
                    for dim, cur in self._spiral_cursor.items()
                },
                "void_edges": {dim: [list(pt) for pt in edges] for dim, edges in self._void_edges.items() if edges},
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

            new_camps: dict[str, dict[str, Any]] = {}
            if "camps" in data:
                camps_raw = data["camps"]
                if not isinstance(camps_raw, Mapping):
                    return False
                for raw_dim, camp_raw in camps_raw.items():
                    if not isinstance(raw_dim, str) or not isinstance(camp_raw, Mapping):
                        return False
                    dim = _norm_dim(raw_dim)
                    pos_cell = _parse_cell(camp_raw.get("pos"))
                    if pos_cell is None:
                        return False
                    parsed_camp: dict[str, Any] = {"pos": list(pos_cell)}
                    for field in _CAMP_STATIONS:
                        raw_station = camp_raw.get(field)
                        if raw_station is None:
                            parsed_camp[field] = None
                        else:
                            st_cell = _parse_cell(raw_station)
                            if st_cell is None:
                                return False
                            parsed_camp[field] = list(st_cell)
                    stored_items = _validate_strict_items(camp_raw.get("stored_items", {}))
                    if stored_items is None:
                        return False
                    parsed_camp["stored_items"] = stored_items
                    raw_tier = camp_raw.get("shelter_tier", 0)
                    if isinstance(raw_tier, bool) or not isinstance(raw_tier, int) or raw_tier not in (0, 1, 2):
                        return False
                    parsed_camp["shelter_tier"] = int(raw_tier)
                    chests_out: dict[str, list[int]] = {}
                    raw_chests = camp_raw.get("chests", {})
                    if not isinstance(raw_chests, Mapping):
                        return False
                    for rname, rpos in raw_chests.items():
                        if not isinstance(rname, str) or rname not in _CAMP_CHEST_ROLES:
                            return False
                        rcell = _parse_cell(rpos)
                        if rcell is None:
                            return False
                        chests_out[rname] = list(rcell)
                    if not chests_out and parsed_camp.get("chest") is not None:
                        chests_out["kit"] = list(parsed_camp["chest"])
                    parsed_camp["chests"] = chests_out
                    chest_items_out: dict[str, dict[str, int]] = {}
                    raw_ci = camp_raw.get("chest_items", {})
                    if not isinstance(raw_ci, Mapping):
                        return False
                    for rname, ritems in raw_ci.items():
                        if not isinstance(rname, str) or rname not in _CAMP_CHEST_ROLES:
                            return False
                        validated = _validate_strict_items(ritems)
                        if validated is None:
                            return False
                        chest_items_out[rname] = validated
                    parsed_camp["chest_items"] = chest_items_out
                    raw_mine = camp_raw.get("mine_entry")
                    if raw_mine is None:
                        parsed_camp["mine_entry"] = None
                    else:
                        mcell = _parse_cell(raw_mine)
                        if mcell is None:
                            return False
                        parsed_camp["mine_entry"] = list(mcell)
                    raw_dep = camp_raw.get("kit_depleted", False)
                    if not isinstance(raw_dep, bool):
                        return False
                    parsed_camp["kit_depleted"] = raw_dep
                    new_camps[dim] = parsed_camp

            new_spawn_pos: dict[str, Cell] = {}
            if "spawn_positions" in data:
                spawns_raw = data["spawn_positions"]
                if not isinstance(spawns_raw, Mapping):
                    return False
                for raw_dim, raw_sp in spawns_raw.items():
                    if not isinstance(raw_dim, str):
                        return False
                    scell = _parse_cell(raw_sp)
                    if scell is None:
                        return False
                    new_spawn_pos[_norm_dim(raw_dim)] = scell

            new_last_deaths: dict[str, dict[str, Any]] = {}
            if "last_deaths" in data:
                deaths_raw = data["last_deaths"]
                if not isinstance(deaths_raw, Mapping):
                    return False
                for raw_dim, death_raw in deaths_raw.items():
                    if not isinstance(raw_dim, str) or not isinstance(death_raw, Mapping):
                        return False
                    dim = _norm_dim(raw_dim)
                    pos_cell = _parse_cell(death_raw.get("pos"))
                    if pos_cell is None:
                        return False
                    cause_raw = death_raw.get("cause", "")
                    if not isinstance(cause_raw, str):
                        return False
                    lost_items = _validate_strict_items(death_raw.get("lost_items", {}))
                    if lost_items is None:
                        return False
                    recorded_at = _finite_float(death_raw.get("recorded_at"))
                    if recorded_at is None:
                        return False
                    new_last_deaths[dim] = {
                        "pos": list(pos_cell),
                        "cause": cause_raw.strip().lower(),
                        "lost_items": lost_items,
                        "recorded_at": recorded_at,
                    }

            new_mine_shafts: dict[str, dict[str, Any]] = {}
            if "primary_mine_shafts" in data:
                shafts_raw = data["primary_mine_shafts"]
                if not isinstance(shafts_raw, Mapping):
                    return False
                for raw_dim, shaft_raw in shafts_raw.items():
                    if not isinstance(raw_dim, str) or not isinstance(shaft_raw, Mapping):
                        return False
                    dim = _norm_dim(raw_dim)
                    entry_cell = _parse_cell(shaft_raw.get("entry"))
                    if entry_cell is None:
                        return False
                    lowest_y_raw = shaft_raw.get("lowest_y")
                    if isinstance(lowest_y_raw, bool) or not isinstance(lowest_y_raw, int):
                        return False
                    lowest_pos_cell = _parse_cell(shaft_raw.get("lowest_pos"))
                    if lowest_pos_cell is None:
                        return False
                    head = _parse_heading(shaft_raw.get("heading"))
                    if head is None:
                        return False
                    bi_raw = shaft_raw.get("branch_index", {})
                    if not isinstance(bi_raw, Mapping):
                        return False
                    branch_index_out: dict[str, int] = {}
                    for raw_k, raw_v in bi_raw.items():
                        if not isinstance(raw_k, str):
                            return False
                        try:
                            lvl = int(raw_k)
                        except ValueError:
                            return False
                        if str(lvl) != raw_k:
                            return False
                        if isinstance(raw_v, bool) or not isinstance(raw_v, int) or raw_v < 0:
                            return False
                        branch_index_out[str(lvl)] = int(raw_v)
                    shaft_entry: dict[str, Any] = {
                        "entry": list(entry_cell),
                        "lowest_y": int(lowest_y_raw),
                        "lowest_pos": list(lowest_pos_cell),
                        "heading": head,
                        "branch_index": branch_index_out,
                    }
                    raw_seals = shaft_raw.get("seal_cells")
                    if raw_seals is not None:
                        if not isinstance(raw_seals, list):
                            return False
                        parsed_seals: list[list[int]] = []
                        for sc in raw_seals:
                            sc_cell = _parse_cell(sc)
                            if sc_cell is None:
                                return False
                            parsed_seals.append(list(sc_cell))
                        if parsed_seals:
                            shaft_entry["seal_cells"] = parsed_seals
                    if shaft_raw.get("sealed") is True:
                        shaft_entry["sealed"] = True
                    new_mine_shafts[dim] = shaft_entry

            new_station_meta: dict[tuple[str, str, Cell], dict[str, Any]] = {}
            if "station_meta" in data:
                sm_raw = data["station_meta"]
                if not isinstance(sm_raw, list):
                    return False
                for row in sm_raw:
                    if not isinstance(row, Mapping):
                        return False
                    r_dim = row.get("dimension")
                    r_kind = row.get("kind")
                    r_cell = _parse_cell(row.get("pos"))
                    r_scope = row.get("scope", "field")
                    if (
                        not isinstance(r_dim, str)
                        or not isinstance(r_kind, str)
                        or r_cell is None
                        or r_scope not in ("camp", "field")
                    ):
                        return False
                    nk = bare(r_kind).strip().lower()
                    if not nk:
                        return False
                    nd = _norm_dim(r_dim)
                    placed_at = row.get("placed_at", 0.0)
                    verified_at = row.get("verified_at", 0.0)
                    new_station_meta[(nd, nk, r_cell)] = {
                        "scope": r_scope,
                        "placed_at": float(placed_at) if isinstance(placed_at, (int, float)) and not isinstance(placed_at, bool) and math.isfinite(float(placed_at)) else 0.0,
                        "verified_at": float(verified_at) if isinstance(verified_at, (int, float)) and not isinstance(verified_at, bool) and math.isfinite(float(verified_at)) else 0.0,
                        "status": str(row.get("status") or "active"),
                    }

            new_visited_grid: dict[str, set[tuple[int, int]]] = {}
            if "visited_grid" in data:
                grid_raw = data["visited_grid"]
                if not isinstance(grid_raw, Mapping):
                    return False
                for raw_dim, cells_raw in grid_raw.items():
                    if not isinstance(raw_dim, str) or not isinstance(cells_raw, list):
                        return False
                    dim = _norm_dim(raw_dim)
                    parsed_grid: set[tuple[int, int]] = set()
                    for item in cells_raw:
                        pair = _parse_pair2(item)
                        if pair is None:
                            return False
                        parsed_grid.add(pair)
                    if parsed_grid:
                        new_visited_grid.setdefault(dim, set()).update(parsed_grid)

            new_spiral_cursor: dict[str, dict[str, int]] = {}
            new_rings_completed: dict[str, int] = {}
            if "spiral_cursor" in data:
                cursor_raw = data["spiral_cursor"]
                if not isinstance(cursor_raw, Mapping):
                    return False
                for raw_dim, cur_raw in cursor_raw.items():
                    if not isinstance(raw_dim, str) or not isinstance(cur_raw, Mapping):
                        return False
                    ring = cur_raw.get("ring")
                    sector = cur_raw.get("sector")
                    if isinstance(ring, bool) or not isinstance(ring, int) or not (0 <= ring < len(SPIRAL_RING_RADII)):
                        return False
                    if isinstance(sector, bool) or not isinstance(sector, int) or sector < 0:
                        return False
                    dim = _norm_dim(raw_dim)
                    new_spiral_cursor[dim] = {"ring": int(ring), "sector": int(sector)}
                    new_rings_completed[dim] = int(ring)

            new_void_edges: dict[str, list[tuple[int, int]]] = {}
            if "void_edges" in data:
                edges_raw = data["void_edges"]
                if not isinstance(edges_raw, Mapping):
                    return False
                for raw_dim, pts_raw in edges_raw.items():
                    if not isinstance(raw_dim, str) or not isinstance(pts_raw, list):
                        return False
                    dim = _norm_dim(raw_dim)
                    parsed_pts: list[tuple[int, int]] = []
                    for item in pts_raw:
                        pair = _parse_pair2(item)
                        if pair is None:
                            return False
                        parsed_pts.append(pair)
                    if parsed_pts:
                        new_void_edges[dim] = parsed_pts[:64]

            with self._lock:
                self._entries = new_entries
                self._stations = new_stations
                self._station_meta = new_station_meta
                self._landmarks = new_landmarks
                self._portals = new_portals
                self.visited_biomes = new_biomes
                self.survey_sectors_done = new_sectors
                self._camps = new_camps
                self._mine_shafts = new_mine_shafts
                self._last_deaths = new_last_deaths
                self._spawn_pos = new_spawn_pos
                self._visited_grid = new_visited_grid
                self._spiral_cursor = new_spiral_cursor
                self._spiral_rings_completed = new_rings_completed
                self._void_edges = new_void_edges
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
    "ALL_LOG_BLOCKS", "BED_BLOCKS", "BIOME_BLOCK_FINGERPRINTS", "CATEGORY_BLOCKS", "ENTITY_LANDMARK_TAGS",
    "SPIRAL_RING_RADII", "STRUCTURE_FINGERPRINTS", "VALID_STATUSES",
    "ResourceEntry", "WorldResourceMemo", "classify_block", "entity_landmark_tags", "structure_tags_for",
    "memo_for", "reset_all_world_memos", "format_memo_summary",
]
