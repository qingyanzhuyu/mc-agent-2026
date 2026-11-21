"""分扇区岛体检、群系普查、末影之眼三角定位与结构指纹探索技能。"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Generator, Optional

from ..client import ApiError
from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, rec
from .field import scan_blocks
from .items import AIR, PASSABLE_COVER, bare
from .observations import observe_block
from .registry import register
from .sightline import block_cell, eye_to_block_dist, feet_pos
from .voxel_cache import has_batch_get_blocks, voxel_cache_for
from .world_memo import STRUCTURE_FINGERPRINTS, memo_for, structure_tags_for

NETHER_BIOMES: frozenset[str] = frozenset({
    "nether_wastes", "soul_sand_valley", "crimson_forest", "warped_forest", "basalt_deltas",
})
_HAZARD_BLOCKS: frozenset[str] = AIR | {
    "water", "lava", "flowing_water", "flowing_lava", "bubble_column", "fire", "soul_fire",
    "powder_snow", "magma_block", "cactus", "campfire", "soul_campfire", "sweet_berry_bush",
}
_PASSABLE: frozenset[str] = AIR | PASSABLE_COVER
_REQUIRED_DIM: dict[str, str] = {
    "nether_fortress": "the_nether", "bastion_remnant": "the_nether", "nether_biomes": "the_nether",
    "stronghold": "overworld", "trial_chambers": "overworld",
}


def _norm_dim(dim: Any) -> str:
    """将维度名称规整为 overworld / the_nether / the_end 等标准短名。"""
    s = "" if dim is None else bare(str(dim)).strip().lower()
    if s in ("nether", "the_nether"):
        return "the_nether"
    return "the_end" if s in ("end", "the_end") else (s or "overworld")


def _state_dict(state: Any) -> dict[str, Any]:
    raw = state.snapshot() if hasattr(state, "snapshot") else getattr(state, "st", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _require_ready_dim(ex: Any, state: Any, expected_dim: Optional[str] = None) -> tuple[dict[str, Any], str]:
    st = state.refresh_status(ex.client, max_age_s=0)
    if st.get("ready") is False:
        raise Replan("世界尚未就绪，等待区块加载后继续")
    cur_dim = _norm_dim(st.get("dimension"))
    if expected_dim is not None and cur_dim != _norm_dim(expected_dim):
        raise Replan(f"当前维度为 {cur_dim}，需要 {_norm_dim(expected_dim)}")
    return st, cur_dim


def resolve_eye_bearings(obs1: tuple[float, float, float], obs2: tuple[float, float, float]) -> Optional[tuple[float, float]]:
    """根据两次末影之眼投掷观测 `(x, z, yaw_deg)` 求解前方交点 `(x, z)`。"""
    for obs in (obs1, obs2):
        if (not isinstance(obs, Sequence) or isinstance(obs, (str, bytes)) or len(obs) != 3
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in obs)):
            return None
    (x1, z1, yaw1), (x2, z2, yaw2) = (float(v) for v in obs1), (float(v) for v in obs2)
    dx1, dz1 = -math.sin(math.radians(yaw1)), math.cos(math.radians(yaw1))
    dx2, dz2 = -math.sin(math.radians(yaw2)), math.cos(math.radians(yaw2))
    det = dz1 * dx2 - dx1 * dz2
    if abs(det) < 1e-4:
        return None
    t1 = (dx2 * (z2 - z1) - dz2 * (x2 - x1)) / det
    t2 = (dx1 * (z2 - z1) - dz1 * (x2 - x1)) / det
    if t1 <= 0.0 or t2 <= 0.0:
        return None
    ix, iz = x1 + t1 * dx1, z1 + t1 * dz1
    return (ix, iz) if math.isfinite(ix) and math.isfinite(iz) else None


def _bounded_move_to(ex: Any, pos: Sequence[float], *, rng: int = 2, timeout: float = 25.0) -> dict:
    """带超时与不重试的有限移动；对不支持 timeout/retries 的替身回退基础签名。"""
    dest = list(pos)
    try:
        return ex.move_to(pos=dest, rng=rng, timeout=timeout, retries=0)
    except TypeError:
        return ex.move_to(pos=dest, rng=rng)


def _move_step(ex: Any, pos: Sequence[float], *, raise_on_replan: bool = False, **fields: Any) -> Generator[dict, None, bool]:
    dest = list(pos)
    try:
        receipt = _bounded_move_to(ex, dest, rng=2, timeout=25.0)
    except Replan as exc:
        yield rec("move_to", {"error": str(exc)}, pos=dest, **fields)
        if raise_on_replan:
            raise
        return False
    yield rec("move_to", receipt, pos=dest, **fields)
    return True


def _normalize_find_hits(raw: Any) -> list[dict[str, Any]]:
    blocks = raw.get("blocks") if isinstance(raw, Mapping) else None
    if isinstance(blocks, Mapping):
        return [
            {"name": bare(n), "pos": s["nearest"] if s.get("nearest") is not None else s.get("pos"), "dist": s.get("dist")}
            for n, s in blocks.items() if isinstance(s, Mapping) and (s.get("nearest") is not None or s.get("pos") is not None)
        ]
    if isinstance(blocks, Sequence) and not isinstance(blocks, (str, bytes)):
        return [dict(b, name=bare(b.get("name"))) for b in blocks if isinstance(b, Mapping) and b.get("pos") is not None]
    return []


def _sweep_blocks(
    ex: Any, state: Any, *, types: Optional[Sequence[str] | str] = None,
    radius: int = 48, limit: int = 64, landmark_meta: Optional[dict[str, Any]] = None,
) -> tuple[list[dict[str, Any]], set[str], int]:
    """扫描周边暴露方块并同步写入 WorldResourceMemo 与 VoxelCache。"""
    eff_r, eff_l = max(1, min(int(radius), 64)), max(1, min(int(limit), 64))
    client = getattr(ex, "client", None)
    find_fn = getattr(client, "find_blocks", None)
    if not callable(find_fn):
        return [], set(), 0
    st = _state_dict(state)
    dim, origin = _norm_dim(st.get("dimension")), st.get("pos")
    type_list = [bare(types)] if isinstance(types, str) else ([bare(t) for t in types] if types else None)
    hits: list[dict[str, Any]] = []
    try:
        res = scan_blocks(ex, state, types=types, radius=eff_r, limit=eff_l)
        if res.get("ready") is False:
            return [], set(), 0
        hits = list(res.get("blocks") or [])
    except TypeError:
        for fn in (lambda: find_fn(types=type_list, radius=eff_r, limit=eff_l),
                   lambda: find_fn(types=type_list, radius=eff_r), find_fn):
            try:
                hits = _normalize_find_hits(fn())[:eff_l]
                break
            except TypeError:
                continue
            except ApiError as exc:
                if exc.code in ("not_ready", "not_loaded"):
                    return [], set(), 0
                raise
    payload = {"blocks": hits}
    voxel_cache_for(state).ingest_find_blocks(payload, dimension=dim, client=client)
    memo, new_tags = memo_for(state), set()
    for hit in hits:
        bname, hpos = bare(hit.get("name")).strip().lower(), hit.get("pos")
        if not bname or hpos is None:
            continue
        for tag in structure_tags_for((bname,)):
            try:
                if not memo.is_blocked(hpos, dimension=dim) and memo.record_landmark(tag, hpos, dimension=dim, meta={"block": bname, **(landmark_meta or {})}):
                    new_tags.add(tag)
            except (Replan, ValueError):
                continue
    return hits, new_tags, memo.ingest_find_blocks(payload, dimension=dim, origin=origin)


def _block_name_at(ex: Any, cache: Any, cell: tuple[int, int, int], dim: str) -> str:
    try:
        blk = cache.get_block(ex.client, cell, dimension=dim)
    except ApiError as exc:
        if exc.code not in ("not_loaded", "not_ready"):
            raise
        blk = None
    if not isinstance(blk, Mapping) or not blk.get("name"):
        blk = observe_block(ex, list(cell))
    return bare(blk.get("name")).strip().lower() if isinstance(blk, Mapping) else ""


def _is_standable_dry(ex: Any, cache: Any, wx: int, wy: int, wz: int, dim: str) -> bool:
    support = _block_name_at(ex, cache, (wx, wy - 1, wz), dim)
    if not support or support in _HAZARD_BLOCKS or support in PASSABLE_COVER:
        return False
    return _block_name_at(ex, cache, (wx, wy, wz), dim) in _PASSABLE and _block_name_at(ex, cache, (wx, wy + 1, wz), dim) in _PASSABLE


def _pick_sector_waypoint(
    ex: Any, state: Any, origin: tuple[float, float, float], dim: str, sector_idx: int, total_sectors: int, radius: int,
) -> Optional[list[float]]:
    """沿扇区射线探测第一个经方块验证的干燥安全落脚路点。"""
    theta = 2.0 * math.pi * (sector_idx % total_sectors) / total_sectors
    ux, uz = math.cos(theta), math.sin(theta)
    ox, oy, oz = origin
    base_y, cache = math.floor(oy), voxel_cache_for(state)
    dists = list(dict.fromkeys(d for d in (min(radius // 2, 24), min(radius // 3, 16), 8, 4) if 1 <= d <= radius))
    eff_dists = dists or [max(1, min(radius, 4))]
    if has_batch_get_blocks(getattr(ex, "client", None)):
        probe_cells = [
            (math.floor(ox + dist * ux), base_y + dy + k, math.floor(oz + dist * uz))
            for dist in eff_dists for dy in (0, 1, -1, 2, -2) for k in (-1, 0, 1)
        ]
        cache.get_blocks_batch(ex.client, probe_cells, dimension=dim)
    for dist in eff_dists:
        wx, wz = math.floor(ox + dist * ux), math.floor(oz + dist * uz)
        for dy in (0, 1, -1, 2, -2):
            if _is_standable_dry(ex, cache, wx, base_y + dy, wz, dim):
                return [wx + 0.5, float(base_y + dy), wz + 0.5]
    return None


def _pick_approach_dest(ex: Any, state: Any, target_cell: tuple[int, int, int], dim: str) -> list[float]:
    tx, ty, tz = target_cell
    cache = voxel_cache_for(state)
    cands = [(tx, ty + 1, tz)] + [(tx + dx, ty + dy, tz + dz) for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)) for dy in (0, 1, -1)]
    if has_batch_get_blocks(getattr(ex, "client", None)):
        cache.get_blocks_batch(ex.client, [(cx, cy + k, cz) for cx, cy, cz in cands for k in (-1, 0, 1)], dimension=dim)
    for cx, cy, cz in cands:
        if _is_standable_dry(ex, cache, cx, cy, cz, dim):
            return [cx + 0.5, float(cy), cz + 0.5]
    return [tx + 0.5, float(ty + 1), tz + 0.5]


def survey_sector(ex: Any, state: Any, *, sector_idx: int = 0, total_sectors: int = 4, radius: int = 48) -> Generator[dict, None, dict]:
    """L2 扇区体检原语：扫方块、登记群系/地标、向安全干燥路点推进并复扫。"""
    if isinstance(total_sectors, bool) or int(total_sectors) < 1:
        raise ValueError(f"total_sectors 必须 >= 1；收到 {total_sectors!r}")
    if isinstance(sector_idx, bool) or int(sector_idx) < 0:
        raise ValueError(f"sector_idx 必须 >= 0；收到 {sector_idx!r}")
    sector_idx, total_sectors, eff_r = int(sector_idx), int(total_sectors), max(1, min(int(radius), 64))
    st, dim = _require_ready_dim(ex, state)
    memo = memo_for(state)
    cur_biome = bare(st.get("biome", "")).strip().lower()
    memo.record_biome(st.get("biome", ""), dimension=dim)
    _, new_tags, total_ingested = _sweep_blocks(ex, state, radius=eff_r, limit=64, landmark_meta={"sector": sector_idx})
    discovered_tags = set(new_tags)
    wp = _pick_sector_waypoint(ex, state, feet_pos(st), dim, sector_idx, total_sectors, eff_r)
    moved = (yield from _move_step(ex, wp, sector=sector_idx)) if wp is not None else False
    if moved:
        st2 = state.refresh_status(ex.client, max_age_s=0)
        dim = _norm_dim(st2.get("dimension") or dim)
        cur_biome = bare(st2.get("biome", "")).strip().lower() or cur_biome
        memo.record_biome(st2.get("biome", ""), dimension=dim)
        _, post_tags, post_ingested = _sweep_blocks(ex, state, radius=eff_r, limit=64, landmark_meta={"sector": sector_idx})
        discovered_tags.update(post_tags)
        total_ingested += post_ingested
    memo.mark_sector_done(sector_idx, dimension=dim)
    return {"sector": sector_idx, "dimension": dim, "moved": moved, "biome": cur_biome, "landmarks": sorted(discovered_tags), "ingested": total_ingested}


@register
class SurveyIsland(Skill):
    name, description, goal, verified = "survey_island", "分扇区岛体检并登记结构与群系指纹", "survey:{dimension}", "offline"
    params = {
        "dimension": Param(str, "体检维度", default="overworld", choices=("overworld", "the_nether", "the_end")),
        "sectors": Param(int, "扇区数", default=4, min=1, max=8),
        "radius": Param(int, "扫描半径", default=48, min=16, max=64),
    }

    @classmethod
    def blocked(cls, state) -> str:
        if not state.ready:
            return "世界尚未就绪，等待区块加载后体检"
        cur_dim = _norm_dim((state.st or {}).get("dimension"))
        items = [r for r in ((state.inventory or {}).get("items") or ()) if isinstance(r, dict) and int(r.get("count", 0)) >= 1]
        if cur_dim == "overworld" and not items and not memo_for(state).has_landmark(dimension=cur_dim):
            return "起步空包且尚无地标，先完成基础采集工具后再进行扇区体检"
        return ""

    def can_start(self, state) -> bool:
        if self.blocked(state):
            return False
        dim = _norm_dim(self.args["dimension"])
        if _norm_dim((state.st or {}).get("dimension")) != dim:
            return False
        done = memo_for(state).done_sectors(dimension=dim)
        return any(i not in done for i in range(self.args["sectors"]))

    def run(self, ex, state):
        _, dim = _require_ready_dim(ex, state, self.args["dimension"])
        sectors, radius = self.args["sectors"], self.args["radius"]
        done = memo_for(state).done_sectors(dimension=dim)
        for idx in [i for i in range(sectors) if i not in done]:
            yield from survey_sector(ex, state, sector_idx=idx, total_sectors=sectors, radius=radius)

    def verify_done(self, state, before_counts=None) -> str:
        dim = _norm_dim(self.args["dimension"])
        missing = [i for i in range(self.args["sectors"]) if i not in memo_for(state).done_sectors(dimension=dim)]
        return "" if not missing else f"{dim} 尚有未完成体检的扇区: {missing}"


@register
class DiscoverStructure(Skill):
    name, description, goal, verified = "discover_structure", "按指纹或群系探索并进入目标结构/下界群系", "discover:{structure}", "offline"
    params = {
        "structure": Param(str, "目标结构或群系任务", default="stronghold",
                           choices=("stronghold", "nether_fortress", "bastion_remnant", "trial_chambers", "nether_biomes")),
        "radius": Param(int, "探索半径", default=48, min=16, max=64),
    }

    def __init__(self, **args: Any) -> None:
        super().__init__(**args)
        self._done: bool = False
        self._before_biomes: set[str] = set()

    @classmethod
    def blocked(cls, state) -> str:
        if not state.ready:
            return "世界尚未就绪，无法探索结构或群系"
        cur_dim = _norm_dim((state.st or {}).get("dimension"))
        if cur_dim not in ("overworld", "the_nether"):
            return f"当前维度 {cur_dim} 不支持结构或群系探索"
        items = [r for r in ((state.inventory or {}).get("items") or ()) if isinstance(r, dict) and int(r.get("count", 0)) >= 1]
        if cur_dim == "overworld" and not items and not memo_for(state).has_landmark(dimension=cur_dim):
            return "起步空包且尚无结构地标，先准备基础工具后再探索结构"
        return ""

    def can_start(self, state) -> bool:
        return not self.blocked(state) and _norm_dim((state.st or {}).get("dimension")) == _REQUIRED_DIM[self.args["structure"]]

    def _collect_targets(self, ex: Any, state: Any, structure: str, dim: str, radius: int) -> list[tuple[int, int, int]]:
        fp_blocks = STRUCTURE_FINGERPRINTS.get(structure, ())
        hits, _, _ = _sweep_blocks(ex, state, types=fp_blocks or None, radius=radius, limit=64)
        cells: dict[tuple[int, int, int], None] = {c: None for c in memo_for(state).landmarks(structure, dimension=dim)}
        fp_set = set(fp_blocks)
        for hit in hits:
            if bare(hit.get("name")).strip().lower() in fp_set and hit.get("pos") is not None:
                try:
                    cells[block_cell(hit["pos"])] = None
                except Replan:
                    continue
        return list(cells)

    def run(self, ex, state):
        structure, radius = self.args["structure"], self.args["radius"]
        st, cur_dim = _require_ready_dim(ex, state, _REQUIRED_DIM[structure])
        memo = memo_for(state)
        if structure == "nether_biomes":
            memo.record_biome(st.get("biome", ""), dimension=cur_dim)
            self._before_biomes = memo.biomes(dimension=cur_dim) & NETHER_BIOMES
            if self._before_biomes >= NETHER_BIOMES:
                self._done = True
                return
            for s in range(4):
                yield from survey_sector(ex, state, sector_idx=s, total_sectors=4, radius=radius)
                cur = memo.biomes(dimension=cur_dim) & NETHER_BIOMES
                if (cur - self._before_biomes) or cur >= NETHER_BIOMES:
                    self._done = True
                    return
            raise EnvBlocked("扫描周边扇区未发现新下界群系", missing_predicate="nether_biomes:new",
                             searched_volume=f"4 扇区半径 {radius} 格", wake_on="chunk_moved")
        candidates = self._collect_targets(ex, state, structure, cur_dim, radius)
        for s in range(4):
            if candidates:
                break
            yield from survey_sector(ex, state, sector_idx=s, total_sectors=4, radius=radius)
            candidates = self._collect_targets(ex, state, structure, cur_dim, radius)
        if not candidates:
            raise EnvBlocked(f"周边扇区未发现 {structure} 结构指纹", missing_predicate=f"structure:{structure}",
                             searched_volume=f"4 扇区半径 {radius} 格", wake_on="chunk_moved")
        st = state.refresh_status(ex.client, max_age_s=0)
        target_cell = min(candidates, key=lambda cell: eye_to_block_dist(st, cell))
        if eye_to_block_dist(st, target_cell) > 3.5:
            yield from _move_step(ex, _pick_approach_dest(ex, state, target_cell, cur_dim), raise_on_replan=True, structure=structure)
        st2 = state.refresh_status(ex.client, max_age_s=0)
        memo.record_biome(st2.get("biome", ""), dimension=cur_dim)
        memo.record_landmark(structure, target_cell, dimension=cur_dim, meta={"visited": True})
        self._done = True

    def verify_done(self, state, before_counts=None) -> str:
        structure, memo = self.args["structure"], memo_for(state)
        if structure == "nether_biomes":
            cur = memo.biomes(dimension="the_nether") & NETHER_BIOMES
            return "" if (cur >= NETHER_BIOMES or (self._done and len(cur) > len(self._before_biomes))) else "尚未在下界发现新的下界群系"
        req_dim = _REQUIRED_DIM[structure]
        if not memo.has_landmark(structure, dimension=req_dim):
            return f"尚未记录 {structure} 结构地标"
        if self._done:
            return ""
        st = _state_dict(state)
        try:
            if any(eye_to_block_dist(st, cell) <= 16.0 for cell in memo.landmarks(structure, dimension=req_dim)):
                return ""
        except Replan:
            pass
        return f"尚未进入 {structure} 结构 16 格范围内"


__all__ = ["NETHER_BIOMES", "resolve_eye_bearings", "survey_sector", "SurveyIsland", "DiscoverStructure"]
