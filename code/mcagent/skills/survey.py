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
from .world_memo import SPIRAL_RING_RADII, STRUCTURE_FINGERPRINTS, entity_landmark_tags, memo_for, structure_tags_for

NETHER_BIOMES: frozenset[str] = frozenset({"nether_wastes", "soul_sand_valley", "crimson_forest", "warped_forest", "basalt_deltas"})
_HAZARD_BLOCKS: frozenset[str] = AIR | {"water", "lava", "flowing_water", "flowing_lava", "bubble_column", "fire", "soul_fire", "powder_snow", "magma_block", "cactus", "campfire", "soul_campfire", "sweet_berry_bush"}
_NETHER_ADJ_HAZARDS: frozenset[str] = frozenset({"lava", "flowing_lava", "fire", "soul_fire", "magma_block"})
_NETHER_OVERHEAD_HAZARDS: frozenset[str] = frozenset({"lava", "flowing_lava", "fire", "soul_fire"})
_CARDINALS: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
_PASSABLE: frozenset[str] = AIR | PASSABLE_COVER
_REQUIRED_DIM: dict[str, str] = {"nether_fortress": "the_nether", "bastion_remnant": "the_nether", "nether_biomes": "the_nether", "stronghold": "overworld", "trial_chambers": "overworld"}
_ENTITY_LANDMARKS: dict[str, str] = {"villager": "village", "iron_golem": "village", "pillager": "pillager_outpost", "allay": "pillager_outpost", "breeze": "trial_chambers", "blaze": "nether_fortress", "wither_skeleton": "nether_fortress", "piglin_brute": "bastion_remnant", "sheep": "sheep_flock"}

def _norm_dim(dim: Any) -> str:
    """将维度名称规整为 overworld / the_nether / the_end 等标准短名。"""
    s = "" if dim is None else bare(str(dim)).strip().lower()
    return "the_nether" if s in ("nether", "the_nether") else ("the_end" if s in ("end", "the_end") else (s or "overworld"))

def _state_dict(state: Any) -> dict[str, Any]:
    raw = state.snapshot() if hasattr(state, "snapshot") else getattr(state, "st", None)
    return dict(raw) if isinstance(raw, Mapping) else {}

def _require_ready_dim(ex: Any, state: Any, expected_dim: Optional[str] = None) -> tuple[dict[str, Any], str]:
    st = state.refresh_status(ex.client, max_age_s=0)
    if st.get("ready") is False: raise Replan("世界尚未就绪，等待区块加载后继续")
    cur_dim = _norm_dim(st.get("dimension"))
    if expected_dim is not None and cur_dim != _norm_dim(expected_dim): raise Replan(f"当前维度为 {cur_dim}，需要 {_norm_dim(expected_dim)}")
    return st, cur_dim


def resolve_eye_bearings(obs1: tuple[float, float, float], obs2: tuple[float, float, float]) -> Optional[tuple[float, float]]:
    """根据两次末影之眼投掷观测 `(x, z, yaw_deg)` 求解前方交点 `(x, z)`。"""
    for obs in (obs1, obs2):
        if not isinstance(obs, Sequence) or isinstance(obs, (str, bytes)) or len(obs) != 3 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) for v in obs):
            return None
    (x1, z1, yaw1), (x2, z2, yaw2) = (float(v) for v in obs1), (float(v) for v in obs2)
    dx1, dz1, dx2, dz2 = -math.sin(math.radians(yaw1)), math.cos(math.radians(yaw1)), -math.sin(math.radians(yaw2)), math.cos(math.radians(yaw2))
    det = dz1 * dx2 - dx1 * dz2
    if abs(det) < 1e-4: return None
    t1, t2 = (dx2 * (z2 - z1) - dz2 * (x2 - x1)) / det, (dx1 * (z2 - z1) - dz1 * (x2 - x1)) / det
    if t1 <= 0.0 or t2 <= 0.0: return None
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
        return [{"name": bare(n), "pos": s.get("nearest") if s.get("nearest") is not None else s.get("pos"), "dist": s.get("dist")}
                for n, s in blocks.items() if isinstance(s, Mapping) and (s.get("nearest") is not None or s.get("pos") is not None)]
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
        res = scan_blocks(ex, state, types=types, radius=eff_r, limit=eff_l, verify=False)
        if res.get("ready") is False:
            return [], set(), 0
        hits = list(res.get("blocks") or [])
    except TypeError:
        for fn in (lambda: find_fn(types=type_list, radius=eff_r, limit=eff_l), lambda: find_fn(types=type_list, radius=eff_r), find_fn):
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

def _sweep_entities(ex: Any, state: Any, dim: str) -> set[str]:
    """扫描周边实体并把结构/群系相关实体地标写入 WorldResourceMemo。"""
    get_ents = getattr(getattr(ex, "client", None), "get_entities", None)
    if not callable(get_ents):
        return set()
    try:
        try:
            res = get_ents(radius=48)
        except TypeError:
            res = get_ents()
    except ApiError as exc:
        if exc.code in ("not_ready", "not_loaded"):
            return set()
        raise
    ents = (res.get("entities") or ()) if isinstance(res, Mapping) else (res if isinstance(res, list) else ())
    memo, found = memo_for(state), set()
    for ent in ents:
        if not isinstance(ent, Mapping) or ent.get("pos") is None:
            continue
        ename = bare(ent.get("name")).strip().lower()
        tags = entity_landmark_tags(ename) or ((t,) if (t := _ENTITY_LANDMARKS.get(ename)) else ())
        for tag in tags:
            try:
                if memo.record_landmark(tag, ent["pos"], dimension=dim, meta={"entity": ename}):
                    found.add(tag)
            except (Replan, ValueError):
                continue
    return found

def _blocked_check(state: Any, not_ready_msg: str, empty_msg: str, *, allowed_dims: tuple[str, ...] = ()) -> str:
    if not state.ready:
        return not_ready_msg
    cur_dim = _norm_dim((state.st or {}).get("dimension"))
    if allowed_dims and cur_dim not in allowed_dims:
        return f"当前维度 {cur_dim} 不支持结构或群系探索"
    has_items = any(isinstance(r, dict) and int(r.get("count", 0)) >= 1 for r in ((state.inventory or {}).get("items") or ()))
    return empty_msg if (cur_dim == "overworld" and not has_items and not memo_for(state).has_landmark(dimension=cur_dim)) else ""

def _block_name_at(ex: Any, cache: Any, cell: tuple[int, int, int], dim: str, *, allow_fallback: bool = True) -> str:
    try:
        blk = cache.get_block(ex.client, cell, dimension=dim)
    except ApiError as exc:
        if exc.code not in ("not_loaded", "not_ready"):
            raise
        blk = None
    if allow_fallback and (not isinstance(blk, Mapping) or not blk.get("name")):
        blk = observe_block(ex, list(cell))
    return bare(blk.get("name")).strip().lower() if isinstance(blk, Mapping) else ""

def _is_standable_dry(ex: Any, cache: Any, wx: int, wy: int, wz: int, dim: str, *, allow_fallback: bool = True) -> bool:
    support = _block_name_at(ex, cache, (wx, wy - 1, wz), dim, allow_fallback=allow_fallback)
    if not support or support in _HAZARD_BLOCKS or support in PASSABLE_COVER:
        return False
    return (
        _block_name_at(ex, cache, (wx, wy, wz), dim, allow_fallback=allow_fallback) in _PASSABLE
        and _block_name_at(ex, cache, (wx, wy + 1, wz), dim, allow_fallback=allow_fallback) in _PASSABLE
    )

def _is_nether_safe_waypoint(ex: Any, cache: Any, wx: int, wy: int, wz: int, dim: str, *, allow_fallback: bool = True) -> bool:
    if not _is_standable_dry(ex, cache, wx, wy, wz, dim, allow_fallback=allow_fallback):
        return False
    if dim != "the_nether":
        return True
    if _block_name_at(ex, cache, (wx, wy + 2, wz), dim, allow_fallback=allow_fallback) in _NETHER_OVERHEAD_HAZARDS:
        return False
    supported_neighbors = 0
    for dx, dz in _CARDINALS:
        nx, nz = wx + dx, wz + dz
        nb_body = _block_name_at(ex, cache, (nx, wy, nz), dim, allow_fallback=allow_fallback)
        nb_floor = _block_name_at(ex, cache, (nx, wy - 1, nz), dim, allow_fallback=allow_fallback)
        if nb_body in _NETHER_ADJ_HAZARDS or nb_floor in _NETHER_ADJ_HAZARDS:
            return False
        if (nb_floor and nb_floor not in _HAZARD_BLOCKS and nb_floor not in PASSABLE_COVER) or (
            (nb_low := _block_name_at(ex, cache, (nx, wy - 2, nz), dim, allow_fallback=allow_fallback))
            and nb_low not in _HAZARD_BLOCKS and nb_low not in PASSABLE_COVER
        ):
            supported_neighbors += 1
    return supported_neighbors >= 2

def _pick_ray_waypoint(
    ex: Any, state: Any, origin: tuple[float, float, float], dim: str,
    ux: float, uz: float, dists: Sequence[int],
    *, angle_offsets_deg: Sequence[float] = (0.0,),
) -> Optional[list[float]]:
    ox, oy, oz = origin
    base_y, cache, memo = math.floor(oy), voxel_cache_for(state), memo_for(state)
    for deg in angle_offsets_deg:
        rad = math.radians(deg)
        c, s = math.cos(rad), math.sin(rad)
        rx, rz = ux * c - uz * s, ux * s + uz * c
        for dist in dists:
            wx, wz = math.floor(ox + dist * rx), math.floor(oz + dist * rz)
            if memo.is_near_void_edge((wx, wz), dimension=dim):
                continue
            for dy in (0, 1, -1, 2, -2):
                if _is_nether_safe_waypoint(ex, cache, wx, base_y + dy, wz, dim):
                    return [wx + 0.5, float(base_y + dy), wz + 0.5]
    return None

def _extract_eye_bearing(origin: tuple[float, float, float], receipt: Any) -> Optional[float]:
    """从 /use_item("ender_eye") 回执中提取 MC yaw 约定方位角（0=+Z, 90=-X；规范 §4.5）。"""
    if not isinstance(receipt, Mapping):
        return None
    raw_deg = receipt.get("bearing_deg")
    if isinstance(raw_deg, (int, float)) and not isinstance(raw_deg, bool) and math.isfinite(float(raw_deg)):
        return float(raw_deg)
    vec = receipt.get("eye_vector")
    if isinstance(vec, Sequence) and not isinstance(vec, (str, bytes)) and len(vec) in (2, 3):
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) for v in vec):
            dx, dz = float(vec[0]), float(vec[-1])
            if math.hypot(dx, dz) >= 1e-4:
                return math.degrees(math.atan2(-dx, dz))
    landed = receipt.get("landed_at")
    if isinstance(landed, Sequence) and not isinstance(landed, (str, bytes)) and len(landed) == 3:
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) for v in landed):
            dx, dz = float(landed[0]) - origin[0], float(landed[2]) - origin[2]
            if math.hypot(dx, dz) >= 1e-4:
                return math.degrees(math.atan2(-dx, dz))
    return None

def _spiral_anchor(memo: Any, st: Mapping[str, Any], dim: str) -> tuple[float, float, float]:
    spawn = memo.get_spawn(dimension=dim)
    if spawn is None:
        spawn = memo.set_spawn(feet_pos(st), dimension=dim)
    return (float(spawn[0]), float(spawn[1]), float(spawn[2]))

def _pick_sector_waypoint(
    ex: Any, state: Any, origin: tuple[float, float, float], dim: str,
    sector_idx: int, total_sectors: int, radius: int,
    *, ring_radius: Optional[int] = None, anchor: Optional[tuple[float, float, float]] = None,
) -> Optional[list[float]]:
    """沿扇区或环形目标射线探测经方块验证的干燥安全落脚路点，并避开虚空/熔岩海与悬崖边缘。"""
    theta = 2.0 * math.pi * (sector_idx % total_sectors) / total_sectors
    ux, uz = math.cos(theta), math.sin(theta)
    ox, oy, oz = origin
    if ring_radius is not None:
        ax, _, az = anchor if anchor is not None else origin
        vx, vz = (ax + ring_radius * ux) - ox, (az + ring_radius * uz) - oz
        if (vlen := math.hypot(vx, vz)) >= 4.0:
            ux, uz = vx / vlen, vz / vlen
    base_y, cache, memo = math.floor(oy), voxel_cache_for(state), memo_for(state)
    raw_rtt = getattr(getattr(ex, "client", None), "smoothed_rtt_s", 0.0)
    high_rtt = (float(raw_rtt) if isinstance(raw_rtt, (int, float)) and not isinstance(raw_rtt, bool) else 0.0) > 0.25
    if high_rtt:
        raw_dists = (min(radius, 32), 16, 8)
        dy_list = (0, 1, -1)
    else:
        raw_dists = (min(radius, 48), min(radius, 40), 32, 24, 16, 8, 4)
        dy_list = (0, 1, -1, 2, -2)
    eff_dists = list(dict.fromkeys(d for d in raw_dists if 1 <= d <= radius)) or [max(1, min(radius, 4))]
    batched = False
    if has_batch_get_blocks(getattr(ex, "client", None)):
        probe_cells = [
            (math.floor(ox + d * ux), base_y + dy + k, math.floor(oz + d * uz))
            for d in eff_dists for dy in dy_list for k in (-1, 0, 1)
        ]
        probe_cells.extend((math.floor(ox), base_y - k, math.floor(oz)) for k in (1, 2))
        if not high_rtt or base_y >= 58:
            probe_cells.extend((math.floor(ox + d * ux), base_y - 4, math.floor(oz + d * uz)) for d in eff_dists)
        cache.get_blocks_batch(ex.client, probe_cells, dimension=dim)
        batched = True
    allow_fb = not batched
    has_origin_floor = any(
        (b := _block_name_at(ex, cache, (math.floor(ox), base_y - k, math.floor(oz)), dim, allow_fallback=allow_fb))
        and b not in AIR
        for k in (1, 2)
    )
    for dist in eff_dists:
        wx, wz = math.floor(ox + dist * ux), math.floor(oz + dist * uz)
        if memo.is_near_void_edge((wx, wz), dimension=dim):
            continue
        for dy in dy_list:
            if _is_nether_safe_waypoint(ex, cache, wx, base_y + dy, wz, dim, allow_fallback=allow_fb):
                return [wx + 0.5, float(base_y + dy), wz + 0.5]
        if has_origin_floor and not (high_rtt and dim == "overworld" and base_y < 58):
            if dim == "the_nether":
                down_col = [_block_name_at(ex, cache, (wx, base_y - k, wz), dim, allow_fallback=allow_fb) for k in range(1, 5)]
                if all(down_col) and (all(b in AIR for b in down_col) or any(b in ("lava", "flowing_lava") for b in down_col)):
                    memo.record_void_edge((wx, wz), dimension=dim)
            else:
                col = [_block_name_at(ex, cache, (wx, base_y + dy, wz), dim, allow_fallback=allow_fb) for dy in range(-4, 3)]
                if all(col) and all(b in AIR for b in col):
                    memo.record_void_edge((wx, wz), dimension=dim)
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

def _sample_site(ex: Any, state: Any, memo: Any, st: Mapping[str, Any], dim: str, sector_idx: int, scan_r: int) -> tuple[str, set[str], int]:
    if st.get("pos"):
        memo.mark_grid_visited(st["pos"], dimension=dim)
    memo.record_biome(st.get("biome", ""), pos=st.get("pos"), dimension=dim)
    _, tags, ingested = _sweep_blocks(ex, state, radius=scan_r, limit=64, landmark_meta={"sector": sector_idx})
    return bare(st.get("biome", "")).strip().lower(), set(tags) | _sweep_entities(ex, state, dim), ingested


_ACTIVE_THREAT_INTENTS = frozenset({"flee", "douse", "escape_lava", "lava", "surface", "void_risk"})


def _has_active_threat(state: Any, st: Mapping[str, Any]) -> bool:
    """检查是否存在紧急逃生类安全意图或近身敌怪（防体检长扫方块 2~3s 期间被苦力怕近身引爆；不因无怪残血 heal 误判振荡）。"""
    intent_raw = getattr(state, "safety_intent", None)
    if isinstance(intent_raw, Mapping):
        kind = str(intent_raw.get("kind") or intent_raw.get("intent") or "").strip().lower()
        if kind in _ACTIVE_THREAT_INTENTS:
            return True
    elif isinstance(intent_raw, str) and intent_raw.strip().lower() in _ACTIVE_THREAT_INTENTS:
        return True
    if st.get("in_lava") or st.get("on_fire") or st.get("submerged"):
        return True
    nh = st.get("nearest_hostile")
    if isinstance(nh, Mapping) and nh:
        hname = bare(nh.get("name", "")).strip().lower()
        raw_d = nh.get("dist")
        if isinstance(raw_d, (int, float)) and not isinstance(raw_d, bool) and math.isfinite(float(raw_d)):
            d = float(raw_d)
            if (hname == "creeper" and d < 7.0) or d < 5.5:
                return True
    return False


def survey_sector(
    ex: Any, state: Any, *, sector_idx: int = 0, total_sectors: int = 4, radius: int = 48,
    ring_radius: Optional[int] = None, anchor: Optional[tuple[float, float, float]] = None,
) -> Generator[dict, None, dict]:
    """L2 扇区体检原语：高频扫方块/实体、登记网格/群系/地标、避险推进并复扫。"""
    if isinstance(total_sectors, bool) or int(total_sectors) < 1: raise ValueError(f"total_sectors 必须 >= 1；收到 {total_sectors!r}")
    if isinstance(sector_idx, bool) or int(sector_idx) < 0: raise ValueError(f"sector_idx 必须 >= 0；收到 {sector_idx!r}")
    sector_idx, total_sectors, eff_r = int(sector_idx), int(total_sectors), max(1, min(int(radius), 64))
    st, dim = _require_ready_dim(ex, state)
    memo, scan_r = memo_for(state), 64 if eff_r >= 48 else eff_r
    cur_biome, discovered_tags, total_ingested = _sample_site(ex, state, memo, st, dim, sector_idx, scan_r)
    wp = _pick_sector_waypoint(ex, state, feet_pos(st), dim, sector_idx, total_sectors, eff_r, ring_radius=ring_radius, anchor=anchor)
    moved = (yield from _move_step(ex, wp, sector=sector_idx)) if wp is not None else False
    if moved:
        st2 = state.refresh_status(ex.client, max_age_s=0)
        dim = _norm_dim(st2.get("dimension") or dim)
        if _has_active_threat(state, st2):
            return {"sector": sector_idx, "dimension": dim, "moved": moved, "biome": cur_biome, "landmarks": sorted(discovered_tags), "ingested": total_ingested, "aborted_threat": True}
        post_biome, post_tags, post_ingested = _sample_site(ex, state, memo, st2, dim, sector_idx, scan_r)
        cur_biome = post_biome or cur_biome
        discovered_tags.update(post_tags)
        total_ingested += post_ingested
    memo.mark_sector_done(sector_idx, dimension=dim)
    return {"sector": sector_idx, "dimension": dim, "moved": moved, "biome": cur_biome, "landmarks": sorted(discovered_tags), "ingested": total_ingested}


@register
class SurveyIsland(Skill):
    name, description, goal, verified = "survey_island", "分扇区岛体检并登记结构与群系指纹", "survey:{dimension}", "live"
    params = {
        "dimension": Param(str, "体检维度", default="overworld", choices=("overworld", "the_nether", "the_end")),
        "sectors": Param(int, "扇区数", default=4, min=1, max=8),
        "radius": Param(int, "扫描半径", default=48, min=16, max=64),
    }

    def __init__(self, **args: Any) -> None:
        super().__init__(**args)
        self._completed_batch: bool = False

    @classmethod
    def blocked(cls, state) -> str:
        return _blocked_check(state, "世界尚未就绪，等待区块加载后体检", "起步空包且尚无地标，先完成基础采集工具后再进行扇区体检")

    def can_start(self, state) -> bool:
        if self._completed_batch or self.blocked(state): return False
        dim = _norm_dim(self.args["dimension"])
        if _norm_dim((state.st or {}).get("dimension")) != dim: return False
        memo = memo_for(state)
        if memo.is_island_survey_exhausted(dimension=dim):
            return False
        if any(i not in memo.done_sectors(dimension=dim) for i in range(self.args["sectors"])): return True
        cur = memo.get_spiral_cursor(dimension=dim)
        return cur["ring"] < len(SPIRAL_RING_RADII) - 1 or cur["sector"] < 8

    def run(self, ex, state):
        st, dim = _require_ready_dim(ex, state, self.args["dimension"])
        sectors, radius, memo = self.args["sectors"], self.args["radius"], memo_for(state)
        spawn = memo.get_spawn(dimension=dim)
        anchor = (float(spawn[0]), float(spawn[1]), float(spawn[2])) if spawn is not None else feet_pos(st)
        missing = [i for i in range(sectors) if i not in memo.done_sectors(dimension=dim)]
        if missing:
            for idx in missing:
                res = yield from survey_sector(ex, state, sector_idx=idx, total_sectors=sectors, radius=radius, ring_radius=radius, anchor=anchor)
                if isinstance(res, Mapping) and res.get("aborted_threat"):
                    raise Replan("扇区体检期间出现近身威胁或安全意图，立即让位脱险")
            if memo.get_spiral_cursor(dimension=dim)["ring"] == 0 and all(i in memo.done_sectors(dimension=dim) for i in range(sectors)):
                memo.advance_spiral_cursor(dimension=dim, sectors_per_ring=1)
        else:
            any_moved = False
            any_landmarks = False
            for _ in range(sectors):
                cur = memo.get_spiral_cursor(dimension=dim)
                res = yield from survey_sector(ex, state, sector_idx=cur["sector"], total_sectors=8, radius=radius, ring_radius=cur["radius"], anchor=anchor)
                if isinstance(res, Mapping) and res.get("aborted_threat"):
                    raise Replan("扇区体检期间出现近身威胁或安全意图，立即让位脱险")
                if isinstance(res, Mapping):
                    if res.get("moved"):
                        any_moved = True
                    if res.get("landmarks"):
                        any_landmarks = True
                memo.advance_spiral_cursor(dimension=dim, sectors_per_ring=8)
            if not any_moved and not any_landmarks:
                raise Replan("扇区外扩全部移动受阻（疑似困在封闭地形），优先脱困后再体检")
        self._completed_batch = True

    def verify_done(self, state, before_counts=None) -> str:
        if self._completed_batch: return ""
        dim = _norm_dim(self.args["dimension"])
        missing = [i for i in range(self.args["sectors"]) if i not in memo_for(state).done_sectors(dimension=dim)]
        return "" if not missing else f"{dim} 尚有未完成体检的扇区: {missing}"


@register
class DiscoverStructure(Skill):
    name, description, goal, verified = "discover_structure", "按指纹或群系探索并进入目标结构/下界群系", "discover:{structure}", "live"
    params = {"structure": Param(str, "目标结构或群系任务", default="stronghold", choices=("stronghold", "nether_fortress", "bastion_remnant", "trial_chambers", "nether_biomes")), "radius": Param(int, "探索半径", default=48, min=16, max=64)}

    def __init__(self, **args: Any) -> None:
        super().__init__(**args)
        self._done, self._before_biomes = False, set()

    @classmethod
    def blocked(cls, state) -> str:
        return _blocked_check(state, "世界尚未就绪，无法探索结构或群系", "起步空包且尚无结构地标，先准备基础工具后再探索结构", allowed_dims=("overworld", "the_nether"))

    def can_start(self, state) -> bool:
        return not self.blocked(state) and _norm_dim((state.st or {}).get("dimension")) == _REQUIRED_DIM[self.args["structure"]]

    def _collect_targets(self, ex: Any, state: Any, structure: str, dim: str, radius: int) -> list[tuple[int, int, int]]:
        fp_blocks = STRUCTURE_FINGERPRINTS.get(structure, ())
        hits, _, _ = _sweep_blocks(ex, state, types=fp_blocks or None, radius=radius, limit=64)
        cells: dict[tuple[int, int, int], None] = {c: None for c in memo_for(state).landmarks(structure, dimension=dim)}
        fp_set = set(fp_blocks)
        for hit in hits:
            if bare(hit.get("name")).strip().lower() in fp_set and hit.get("pos") is not None:
                try: cells[block_cell(hit["pos"])] = None
                except Replan: continue
        return list(cells)

    def _triangulate_stronghold(self, ex: Any, state: Any, dim: str, radius: int) -> Generator[dict, None, list[tuple[int, int, int]]]:
        """持 >=2 颗末影之眼且尚无要塞坐标时：双点投掷测向 -> resolve_eye_bearings -> 沿交点推进扫描。"""
        memo = memo_for(state)
        state.refresh_inventory(ex.client)
        if state.count("ender_eye") < 2 or not callable(getattr(ex, "use_item", None)):
            return []
        st1 = state.refresh_status(ex.client, max_age_s=0)
        o1 = feet_pos(st1)
        rcpt1 = ex.use_item("ender_eye")
        yield rec("use_item", rcpt1, item="ender_eye", throw=1, structure="stronghold")
        if cands := self._collect_targets(ex, state, "stronghold", dim, radius):
            return cands
        yaw1 = _extract_eye_bearing(o1, rcpt1)
        if yaw1 is None:
            return []
        dx1, dz1 = -math.sin(math.radians(yaw1)), math.cos(math.radians(yaw1))
        wp_lat = (
            _pick_ray_waypoint(ex, state, o1, dim, -dz1, dx1, (32, 28, 24, 20, 16), angle_offsets_deg=(0.0, 15.0, -15.0))
            or _pick_ray_waypoint(ex, state, o1, dim, dz1, -dx1, (32, 28, 24, 20, 16), angle_offsets_deg=(0.0, 15.0, -15.0))
        )
        if wp_lat is None or not (yield from _move_step(ex, wp_lat, purpose="ender_eye_baseline", structure="stronghold")):
            return []
        st2 = state.refresh_status(ex.client, max_age_s=0)
        o2 = feet_pos(st2)
        if cands := self._collect_targets(ex, state, "stronghold", dim, radius):
            return cands
        if math.hypot(o2[0] - o1[0], o2[2] - o1[2]) < 8.0:
            return []
        state.refresh_inventory(ex.client)
        if state.count("ender_eye") < 1:
            return []
        rcpt2 = ex.use_item("ender_eye")
        yield rec("use_item", rcpt2, item="ender_eye", throw=2, structure="stronghold")
        yaw2 = _extract_eye_bearing(o2, rcpt2)
        if yaw2 is None:
            return []
        hit = resolve_eye_bearings((o1[0], o1[2], yaw1), (o2[0], o2[2], yaw2))
        if hit is None:
            return []
        ix, iz = hit
        for _ in range(8):
            st_cur = state.refresh_status(ex.client, max_age_s=0)
            cur = feet_pos(st_cur)
            memo.mark_grid_visited(cur, dimension=dim)
            memo.record_biome(st_cur.get("biome", ""), pos=cur, dimension=dim)
            if cands := self._collect_targets(ex, state, "stronghold", dim, radius):
                return cands
            rem = math.hypot(ix - cur[0], iz - cur[2])
            if rem <= 4.0:
                break
            ux, uz = (ix - cur[0]) / rem, (iz - cur[2]) / rem
            step_max = min(int(math.ceil(rem)), min(radius, 48))
            dists = list(dict.fromkeys(d for d in (step_max, min(step_max, 40), 32, 24, 16, 8, 4) if 1 <= d <= step_max)) or [max(1, step_max)]
            wp = _pick_ray_waypoint(ex, state, cur, dim, ux, uz, dists, angle_offsets_deg=(0.0, 20.0, -20.0, 40.0, -40.0))
            if wp is None or not (yield from _move_step(ex, wp, purpose="ender_eye_approach", structure="stronghold")):
                break
        return self._collect_targets(ex, state, "stronghold", dim, radius)

    def run(self, ex, state):
        structure, radius = self.args["structure"], self.args["radius"]
        st, cur_dim = _require_ready_dim(ex, state, _REQUIRED_DIM[structure])
        memo = memo_for(state)
        anchor = _spiral_anchor(memo, st, cur_dim)
        if structure == "nether_biomes":
            memo.record_biome(st.get("biome", ""), pos=st.get("pos"), dimension=cur_dim)
            self._before_biomes = memo.biomes(dimension=cur_dim) & NETHER_BIOMES
            if self._before_biomes >= NETHER_BIOMES:
                self._done = True; return
            for _ in range(4):
                cur_cursor = memo.get_spiral_cursor(dimension=cur_dim)
                sec, ring_r = cur_cursor["sector"], cur_cursor["radius"]
                theta = 2.0 * math.pi * (sec % 8) / 8
                tx, tz = anchor[0] + ring_r * math.cos(theta), anchor[2] + ring_r * math.sin(theta)
                max_hops = 1 if ring_r <= radius else min(3, math.ceil(ring_r / max(1, radius)))
                for _hop in range(max_hops):
                    res = yield from survey_sector(ex, state, sector_idx=sec, total_sectors=8, radius=radius, ring_radius=ring_r, anchor=anchor)
                    cur_biomes = memo.biomes(dimension=cur_dim) & NETHER_BIOMES
                    if (cur_biomes - self._before_biomes) or cur_biomes >= NETHER_BIOMES:
                        memo.advance_spiral_cursor(dimension=cur_dim, sectors_per_ring=8)
                        self._done = True; return
                    if not res.get("moved"):
                        break
                    pos_now = feet_pos(state.st or st)
                    if math.hypot(tx - pos_now[0], tz - pos_now[2]) <= max(16.0, radius * 0.6):
                        break
                memo.advance_spiral_cursor(dimension=cur_dim, sectors_per_ring=8)
            raise EnvBlocked("扫描周边螺旋扇区未发现新下界群系", missing_predicate="nether_biomes:new", searched_volume=f"4 螺旋扇区半径 {radius} 格", wake_on="chunk_moved")
        candidates = self._collect_targets(ex, state, structure, cur_dim, radius)
        if structure == "stronghold" and not candidates and not memo.has_landmark("stronghold", dimension=cur_dim):
            candidates = yield from self._triangulate_stronghold(ex, state, cur_dim, radius)
        for _ in range(4):
            if candidates: break
            cur_cursor = memo.get_spiral_cursor(dimension=cur_dim)
            yield from survey_sector(ex, state, sector_idx=cur_cursor["sector"], total_sectors=8, radius=radius, ring_radius=cur_cursor["radius"], anchor=anchor)
            memo.advance_spiral_cursor(dimension=cur_dim, sectors_per_ring=8)
            candidates = self._collect_targets(ex, state, structure, cur_dim, radius)
        if not candidates: raise EnvBlocked(f"周边扇区未发现 {structure} 结构指纹", missing_predicate=f"structure:{structure}", searched_volume=f"4 扇区半径 {radius} 格", wake_on="chunk_moved")
        st = state.refresh_status(ex.client, max_age_s=0)
        target_cell = min(candidates, key=lambda cell: eye_to_block_dist(st, cell))
        if eye_to_block_dist(st, target_cell) > 3.5:
            yield from _move_step(ex, _pick_approach_dest(ex, state, target_cell, cur_dim), raise_on_replan=True, structure=structure)
        st2 = state.refresh_status(ex.client, max_age_s=0)
        memo.record_biome(st2.get("biome", ""), pos=st2.get("pos"), dimension=cur_dim)
        memo.record_landmark(structure, target_cell, dimension=cur_dim, meta={"visited": True})
        self._done = True

    def verify_done(self, state, before_counts=None) -> str:
        structure, memo = self.args["structure"], memo_for(state)
        if structure == "nether_biomes":
            cur = memo.biomes(dimension="the_nether") & NETHER_BIOMES
            return "" if (cur >= NETHER_BIOMES or (self._done and len(cur) > len(self._before_biomes))) else "尚未在下界发现新的下界群系"
        req_dim = _REQUIRED_DIM[structure]
        if not memo.has_landmark(structure, dimension=req_dim): return f"尚未记录 {structure} 结构地标"
        # 注意：**不能**只看 `self._done` 就判完成。真机事故（2026-11-24）：记忆里已有
        # stronghold 地标时，run() 每轮把同一个地标再记一次就置 `_done=True`，于是
        # verify_done 立刻返回 ""，策略下一拍又选它 —— 20 分钟内被启动 1879 次、目标
        # follow_ender_eye 却一步没推进。这里改为必须真的走到结构跟前才算完成。
        try:
            if any(eye_to_block_dist(_state_dict(state), cell) <= 16.0 for cell in memo.landmarks(structure, dimension=req_dim)): return ""
        except Replan:
            pass
        return f"尚未进入 {structure} 结构 16 格范围内"


__all__ = ["NETHER_BIOMES", "resolve_eye_bearings", "survey_sector", "SurveyIsland", "DiscoverStructure"]
