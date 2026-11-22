"""考古与嗅探兽生态 L3 技能：刷取陶片/嗅探兽蛋、孵蛋、种远古种子与喂食幼年嗅探兽（+135 分）。"""
from __future__ import annotations

import math
from collections.abc import Callable, Generator, Sequence

from ..client import ApiError
from ..runtime import EnvBlocked, Executor, Replan, State
from .base import Param, Skill, failure, rec
from .field import _drop_name, place_block, scan_blocks, scan_entities
from .interaction import _clear_posture, use_item_on
from .items import AIR, bare, is_clear_space, is_safe_solid_floor
from .landmark_nav import goto_known_landmark_or_biome
from .observations import completed_advancements, observe_block, refresh_inventory
from .registry import register
from .sightline import (DIG_EYE_REACH, ENTITY_EYE_REACH, STATION_EYE_REACH, block_cell,
                        block_center, can_reach_block, eye_to_block_dist, eye_to_point_dist)
from .voxel_cache import voxel_cache_for
from .world_memo import memo_for

_SUSPICIOUS = ("suspicious_sand", "suspicious_gravel")
_WATER_BLOCKS = frozenset({"water", "flowing_water", "bubble_column", "seagrass", "tall_seagrass", "kelp", "kelp_plant"})
_BRUSH_PASSABLE = AIR | _WATER_BLOCKS
_HOES = ("netherite_hoe", "diamond_hoe", "iron_hoe", "copper_hoe", "stone_hoe", "wooden_hoe", "golden_hoe")
_SNIFFER_CROPS = {"torchflower_seeds": "torchflower_crop", "pitcher_pod": "pitcher_crop"}
_TILLABLE = ("dirt", "grass_block")
_BRUSH_MS, _MAX_BRUSH_STROKES, _MAX_BRUSH_BLOCKS = 1800, 4, 6
_BRUSH_FALLBACK_CODES = frozenset({"cannot_use", "unsupported_api", "bad_args"})

def _gate(state: State, ok: bool, msg: str) -> str:
    if not state.ready: return "世界尚未就绪（ready=false）"
    if state.st.get("alive") is False: return "角色已死亡，暂无法执行当前技能"
    return "" if ok else msg
def _dim(state: State) -> str: return bare((state.st or {}).get("dimension") or "overworld") or "overworld"
def _pt(c: tuple[int, int, int]) -> tuple[float, float, float]: return (c[0] + 0.5, float(c[1]), c[2] + 0.5)
def _obs_name(ex: Executor, pos: Sequence[int]) -> str: return bare(observe_block(ex, list(pos)).get("name"))
def _env_blocked(msg: str, pred: str, vol: str, *, wake_on: str = "chunk_moved") -> EnvBlocked:
    return EnvBlocked(msg, missing_predicate=pred, searched_volume=vol, wake_on=wake_on)
def _ready(scan: dict) -> dict:
    if scan.get("ready") is False: raise Replan("世界加载尚未就绪（ready=false）")
    return scan
def _inv_entries(state: State) -> list[dict]:
    inv = state.inventory or {}
    return [r for r in (inv.get("items") or ()) if isinstance(r, dict)] + [inv[k] for k in ("held", "offhand") if isinstance(inv.get(k), dict)]
def _sherd_count(state: State) -> int:
    return sum(state.count(n) for n in {bare(r.get("name")) for r in _inv_entries(state) if bare(r.get("name")).endswith("_pottery_sherd")})
def _can_craft_brush(state: State) -> bool:
    return state.count("copper_ingot") >= 1 and state.count("feather") >= 1 and state.count("stick") >= 1
def _pick_hoe(state: State) -> str | None:
    return next((h for h in _HOES if state.count(h) >= 1), None) or next(
        (n for r in _inv_entries(state) if (n := bare(r.get("name"))).endswith("_hoe") and state.count(n) >= 1), None)
def _pick_sniffer_seed(state: State) -> str | None: return next((s for s in _SNIFFER_CROPS if state.count(s) >= 1), None)
def _is_baby(ent: dict) -> bool: return ent.get("baby") is True or ent.get("is_baby") is True
def _overlaps(c: tuple[int, int, int], pos: Sequence[float]) -> bool:
    px, py, pz = block_cell(pos)
    return c[0] == px and c[2] == pz and abs(c[1] - py) <= 1
def _has_adv(ex: Executor, adv_id: str) -> bool:
    return hasattr(ex.client, "advancements") and bare(adv_id) in completed_advancements(ex)
def _read_oxygen(st: dict) -> float | None:
    for key in ("oxygen", "air", "breath"):
        val = st.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
            return float(val) / 15.0 if 20.0 < float(val) <= 300.0 else float(val)
    return None
def _is_standable(ex: Executor, c: tuple[int, int, int], *, allow_water: bool = False) -> bool:
    x, y, z = c
    body, head = observe_block(ex, [x, y, z]), observe_block(ex, [x, y + 1, z])
    if allow_water and bare(body.get("name")) in _BRUSH_PASSABLE and bare(head.get("name")) in _BRUSH_PASSABLE: return True
    return is_safe_solid_floor(observe_block(ex, [x, y - 1, z])) and is_clear_space(body) and is_clear_space(head)

def _find_approach_cell(ex: Executor, target: tuple[int, int, int], st: dict, *, radius: int = 3,
                        reach: float = STATION_EYE_REACH, allow_water: bool = False) -> tuple[int, int, int] | None:
    tx, ty, tz = target
    passable = _BRUSH_PASSABLE if allow_water else AIR
    cands = [(tx + dx, ty + dy, tz + dz) for dy in (0, -1, 1, -2, 2) for dx in range(-radius, radius + 1) for dz in range(-radius, radius + 1)
             if not (dx == 0 and dz == 0 and abs(dy) <= 1) and _is_standable(ex, (tx + dx, ty + dy, tz + dz), allow_water=allow_water)
             and can_reach_block(ex, _pt((tx + dx, ty + dy, tz + dz)), target, reach=reach, passable=passable)]
    return min(cands, key=lambda c: eye_to_block_dist(st, c)) if cands else None

def _approach_block(ex: Executor, state: State, target: tuple[int, int, int], *, reach: float = STATION_EYE_REACH,
                    allow_water: bool = False) -> Generator[dict, None, dict]:
    st, passable = state.refresh_status(ex.client, max_age_s=0), (_BRUSH_PASSABLE if allow_water else AIR)
    if can_reach_block(ex, st, target, reach=reach, passable=passable) and not _overlaps(target, st["pos"]): return st
    cell = _find_approach_cell(ex, target, st, radius=3, reach=reach, allow_water=allow_water) or (
        _find_approach_cell(ex, target, st, radius=3, reach=DIG_EYE_REACH, allow_water=allow_water) if reach < DIG_EYE_REACH else None)
    if cell is None: raise _env_blocked(f"目标方块 {list(target)} 附近无安全可达站位", "standable_approach", "目标附近 3 格")
    dest = list(_pt(cell)); yield rec("move_to", ex.move_to(pos=dest, rng=1), pos=dest)
    st = state.refresh_status(ex.client, max_age_s=0)
    if not can_reach_block(ex, st, target, reach=DIG_EYE_REACH, passable=passable):
        raise Replan(f"接近目标方块 {list(target)} 后仍超出眼距或视线被遮挡")
    return st

def _approach_entity(ex: Executor, state: State, ent: dict, *, reach: float = ENTITY_EYE_REACH) -> Generator[dict, None, dict]:
    st = state.refresh_status(ex.client, max_age_s=0)
    if eye_to_point_dist(st, ent["pos"]) <= reach: return st
    dest = [float(v) for v in ent["pos"]]; yield rec("move_to", ex.move_to(pos=dest, rng=2), pos=dest)
    return state.refresh_status(ex.client, max_age_s=0)

def _find_place_site(ex: Executor, st: dict, *, radius: int = 3, need_two_air: bool = False) -> tuple[int, int, int] | None:
    ox, oy, oz = block_cell(st["pos"])
    cands = [(ox + dx, oy + dy, oz + dz) for dy in (0, 1, -1) for dx in range(-radius, radius + 1) for dz in range(-radius, radius + 1)
             if not _overlaps((ox + dx, oy + dy, oz + dz), st["pos"]) and _obs_name(ex, (ox + dx, oy + dy, oz + dz)) in AIR
             and (not need_two_air or _obs_name(ex, (ox + dx, oy + dy + 1, oz + dz)) in AIR)
             and is_safe_solid_floor(observe_block(ex, [ox + dx, oy + dy - 1, oz + dz]))
             and can_reach_block(ex, st, (ox + dx, oy + dy, oz + dz), reach=DIG_EYE_REACH)]
    return min(cands, key=lambda c: eye_to_block_dist(st, c)) if cands else None

def _ensure_oxygen(ex: Executor, state: State) -> Generator[dict, None, dict]:
    st = state.refresh_status(ex.client, max_age_s=0)
    if st.get("alive") is False or float(st.get("health", 20)) < 8.0: raise Replan("角色血量过低或已死亡，安全中止考古")
    warn, ox = float(state.config.oxygen_warn), _read_oxygen(st)
    if ox is None or ox > warn: return st
    px, py, pz = block_cell(st["pos"])
    surface = next(((px, py + dy, pz) for dy in range(1, 24)
                    if _obs_name(ex, (px, py + dy, pz)) in AIR and _obs_name(ex, (px, py + dy + 1, pz)) in AIR), None)
    if surface is None: raise Replan(f"水下氧气不足（oxygen={ox:.1f}）且上方无换气口，安全退出考古")
    dest = list(_pt(surface)); yield rec("move_to", ex.move_to(pos=dest, rng=1), pos=dest, reason="surface_for_oxygen")
    st = state.refresh_status(ex.client, max_age_s=0)
    if (ox_after := _read_oxygen(st)) is not None and ox_after <= warn:
        raise Replan(f"上浮换气后氧气仍告急（oxygen={ox_after:.1f}），安全退出考古")
    return st

def _collect_drops(ex: Executor, state: State, pred: Callable[[str], bool], *,
                   fallback_cell: tuple[int, int, int] | None = None, radius: int = 8, allow_water: bool = False) -> Generator[dict, None, None]:
    hits = [e for e in _ready(scan_entities(ex, state, "item", radius=radius))["entities"] if pred(_drop_name(e))]
    st = state.refresh_status(ex.client, max_age_s=0)
    if hits:
        dest = [float(v) for v in min(hits, key=lambda e: eye_to_point_dist(st, e["pos"]))["pos"]]
        yield rec("move_to", ex.move_to(pos=dest, rng=1), pos=dest, reason="pickup_drop")
        refresh_inventory(ex, state); return
    if fallback_cell is not None and eye_to_block_dist(st, fallback_cell) > 2.2:
        near = _find_approach_cell(ex, fallback_cell, st, radius=2, reach=2.5, allow_water=allow_water)
        dest = list(_pt(near)) if near is not None else list(block_center(fallback_cell))
        yield rec("move_to", ex.move_to(pos=dest, rng=1), pos=dest, reason="pickup_near_cell")
        refresh_inventory(ex, state)

def _ensure_brush(ex: Executor, state: State, skill_name: str) -> Generator[dict, None, None]:
    refresh_inventory(ex, state)
    if state.count("brush") >= 1: return
    if not _can_craft_brush(state): raise Replan("背包缺少刷子（brush）及合成原料（copper_ingot>=1、feather>=1、stick>=1）")
    tables = _ready(scan_blocks(ex, state, "crafting_table", radius=16, limit=5, skill_name=skill_name))["blocks"]
    st = state.refresh_status(ex.client, max_age_s=0)
    if tables:
        tcell = block_cell(min(tables, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
    elif state.count("crafting_table") >= 1:
        if (site := _find_place_site(ex, st)) is None:
            raise _env_blocked("合成刷子缺少放置工作台的空位", "clear_site:crafting_table", "半径 3 格")
        yield from place_block(ex, state, "crafting_table", site)
        memo_for(state).record_station("crafting_table", site, dimension=_dim(state)); tcell = site
    else:
        raise _env_blocked("合成刷子缺少可达工作台", "station_ready:crafting_table", "半径 16 格")
    yield from _approach_block(ex, state, tcell, reach=STATION_EYE_REACH)
    yield rec("craft", ex.craft("brush", 1), item="brush", count=1)
    refresh_inventory(ex, state)
    if state.count("brush") < 1: raise Replan("工作台合成刷子后背包未确认新增 brush")

class _ArchSnifferSkill(Skill):
    verified, _done_msg = "offline", "未确认考古或嗅探兽技能完成"
    def _begin(self, ex: Executor, state: State) -> dict:
        self._done = False; refresh_inventory(ex, state)
        st = state.refresh_status(ex.client, max_age_s=0)
        if reason := self.blocked(state): raise Replan(reason)
        if not self.can_start(state): raise Replan(f"{self.name} 当前参数条件不满足")
        return st
    def run(self, ex: Executor, state: State):
        try: yield from self._run(ex, state)
        finally:
            if any(state.st.get(k) for k in ("sneaking", "blocking", "jumping")):
                _clear_posture(ex, state, sneak=False, shield=False, jump=False)
    def _run(self, ex: Executor, state: State): raise NotImplementedError
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        return "" if (getattr(self, "_done", False) or (bool(before_counts) and self._delta_done(state, before_counts))) else self._done_msg
    def _delta_done(self, state: State, before: dict) -> bool: return False

@register
class BrushArchaeology(_ArchSnifferSkill):
    name, goal, description = "brush_archaeology", "t2:arch_{target}", "备料并用刷子刷取可疑沙子或沙砾获取陶片或嗅探兽蛋"
    params = {"target": Param(str, "考古目标掉落物", default="sherd", choices=("sherd", "sniffer_egg")),
              "radius": Param(int, "搜索可疑方块半径", default=24, min=8, max=64)}
    _done_msg = "未确认刷出目标陶片或嗅探兽蛋"

    @classmethod
    def blocked(cls, state: State) -> str:
        return _gate(state, state.count("brush") >= 1 or _can_craft_brush(state), "缺少刷子（brush）及合成原料（copper_ingot>=1、feather>=1、stick>=1）")
    def _delta_done(self, state: State, before: dict) -> bool:
        return (state.count("sniffer_egg") > before.get("sniffer_egg", 0)) if self.args["target"] == "sniffer_egg" else (
            _sherd_count(state) > sum(v for k, v in before.items() if bare(k).endswith("_pottery_sherd")))
    def _achieved(self, ex: Executor, state: State, target: str, before_qty: int) -> bool:
        return (state.count("sniffer_egg") > before_qty or _has_adv(ex, "husbandry/obtain_sniffer_egg")) if target == "sniffer_egg" else (
            _sherd_count(state) > before_qty or _has_adv(ex, "adventure/salvage_sherd"))
    def _scan_suspicious(self, ex: Executor, state: State, radius: int, target: str) -> Generator[dict, None, list[dict]]:
        blks = _ready(scan_blocks(ex, state, _SUSPICIOUS, radius=radius, limit=12, skill_name=self.name))["blocks"]
        if not blks:
            moved = yield from goto_known_landmark_or_biome(ex, state, "archaeology|warm_ocean_ruins")
            if not moved and target == "sniffer_egg": moved = yield from goto_known_landmark_or_biome(ex, state, "warm_ocean")
            if moved: blks = _ready(scan_blocks(ex, state, _SUSPICIOUS, radius=radius, limit=12, skill_name=self.name))["blocks"]
        if not blks: raise _env_blocked(f"半径 {radius} 格内未发现可疑沙子或沙砾", "visible_block:suspicious_sand", f"半径 {radius} 格")
        return blks

    def _run(self, ex: Executor, state: State):
        self._begin(ex, state)
        target, radius = self.args["target"], self.args["radius"]
        before_qty = state.count("sniffer_egg") if target == "sniffer_egg" else _sherd_count(state)
        if self._achieved(ex, state, target, before_qty): self._done = True; return
        yield from _ensure_brush(ex, state, self.name)
        blks = yield from self._scan_suspicious(ex, state, radius, target)
        st = state.refresh_status(ex.client, max_age_s=0)
        blks.sort(key=lambda b: (0 if (target != "sniffer_egg" or bare(b["name"]) == "suspicious_sand") else 1, eye_to_block_dist(st, b["pos"])))
        memo_for(state).record_landmark("archaeology", block_cell(blks[0]["pos"]), dimension=_dim(state))

        drop_pred = (lambda n: n == "sniffer_egg") if target == "sniffer_egg" else (lambda n: n.endswith("_pottery_sherd"))
        use_interact, approached_any, last_blocked = False, False, None
        for blk in blks[:_MAX_BRUSH_BLOCKS]:
            cell = block_cell(blk["pos"])
            st = yield from _ensure_oxygen(ex, state)
            if _obs_name(ex, cell) not in _SUSPICIOUS: continue
            in_water = bool(st.get("in_water") or st.get("submerged") or _obs_name(ex, (cell[0], cell[1] + 1, cell[2])) in _WATER_BLOCKS)
            try: st = yield from _approach_block(ex, state, cell, reach=DIG_EYE_REACH, allow_water=in_water)
            except EnvBlocked as exc: last_blocked = exc; continue
            approached_any = True
            if hasattr(ex.client, "look"):
                try: ex.look(pos=list(block_center(cell)))
                except (ApiError, Replan): pass
            strokes_left = _MAX_BRUSH_STROKES
            while strokes_left > 0:
                st = yield from _ensure_oxygen(ex, state)
                if _obs_name(ex, cell) not in _SUSPICIOUS: break
                if not can_reach_block(ex, st, cell, reach=DIG_EYE_REACH, passable=_BRUSH_PASSABLE if in_water else AIR):
                    st = yield from _approach_block(ex, state, cell, reach=DIG_EYE_REACH, allow_water=in_water)
                if not use_interact:
                    try: receipt = ex.use_item("brush", target={"pos": list(cell)}, ms=_BRUSH_MS)
                    except Replan as exc:
                        code, _ = failure(exc)
                        if code not in _BRUSH_FALLBACK_CODES: raise
                        use_interact = True; yield rec("use_item", item="brush", target={"pos": list(cell)}, error_code=code); continue
                    yield rec("use_item", receipt, item="brush", target={"pos": list(cell)}, ms=_BRUSH_MS)
                else:
                    yield rec("interact", ex.interact(pos=list(cell), item="brush"), pos=list(cell), item="brush")
                strokes_left -= 1
                voxel_cache_for(state).invalidate(cell, dimension=_dim(state)); refresh_inventory(ex, state)
                if self._achieved(ex, state, target, before_qty): self._done = True; return
                if _obs_name(ex, cell) not in _SUSPICIOUS: break
            yield from _ensure_oxygen(ex, state)
            yield from _collect_drops(ex, state, drop_pred, fallback_cell=cell, allow_water=in_water)
            if self._achieved(ex, state, target, before_qty): self._done = True; return
        if not approached_any and last_blocked is not None: raise last_blocked
        raise Replan(f"已刷完附近可疑方块，暂未确认获得{'嗅探兽蛋' if target == 'sniffer_egg' else '陶片'}")

@register
class HatchAndRaiseSniffer(_ArchSnifferSkill):
    name, goal, description = "hatch_and_raise_sniffer", "t3:sniffer_{mode}", "放置嗅探兽蛋孵化、种植远古种子或喂食幼年嗅探兽"
    params = {"mode": Param(str, "嗅探兽培育目标", default="plant_seed", choices=("plant_seed", "feed_snifflet")),
              "radius": Param(int, "搜索嗅探兽与地块半径", default=24, min=8, max=64)}
    _done_msg = "未确认嗅探兽远古种子种植或幼体喂食完成"

    @classmethod
    def blocked(cls, state: State) -> str:
        memo, dim = memo_for(state), _dim(state)
        ok = (state.count("sniffer_egg") >= 1 or _pick_sniffer_seed(state) is not None
              or memo.has_landmark("sniffer_hatch", dimension=dim) or memo.has_landmark("sniffer", dimension=dim))
        return _gate(state, ok, "缺少嗅探兽蛋、远古种子或已记录的嗅探兽孵化点")
    def _delta_done(self, state: State, before: dict) -> bool:
        return (state.count("torchflower_seeds") < before.get("torchflower_seeds", 0)) if self.args["mode"] == "feed_snifflet" else (
            any(state.count(s) < before.get(s, 0) for s in _SNIFFER_CROPS))
    def _place_egg_to_hatch(self, ex: Executor, state: State, radius: int) -> Generator[dict, None, None]:
        st = state.refresh_status(ex.client, max_age_s=0)
        moss = _ready(scan_blocks(ex, state, "moss_block", radius=min(radius, 16), limit=8, skill_name=self.name))["blocks"]
        egg_cell = next(((c[0], c[1] + 1, c[2]) for blk in sorted(moss, key=lambda b: eye_to_block_dist(st, b["pos"]))
                         if _obs_name(ex, ((c := block_cell(blk["pos"]))[0], c[1] + 1, c[2])) in AIR), None)
        on_moss = egg_cell is not None
        if egg_cell is None and state.count("moss_block") >= 1 and (base := _find_place_site(ex, st, need_two_air=True)) is not None:
            yield from _approach_block(ex, state, base, reach=DIG_EYE_REACH)
            yield from place_block(ex, state, "moss_block", base)
            egg_cell, on_moss = (base[0], base[1] + 1, base[2]), True
        if (egg_cell := egg_cell or _find_place_site(ex, st)) is None: raise _env_blocked("附近缺少放置嗅探兽蛋的安全空位", "clear_site:sniffer_egg", "半径 3 格")
        yield from _approach_block(ex, state, egg_cell, reach=DIG_EYE_REACH)
        yield from place_block(ex, state, "sniffer_egg", egg_cell)
        refresh_inventory(ex, state)
        memo_for(state).record_landmark("sniffer_hatch", egg_cell, dimension=_dim(state), meta={"accelerated": on_moss})
        raise Replan("waiting_sniffer_hatch")
    def _try_plant_seed(self, ex: Executor, state: State, radius: int) -> Generator[dict, None, bool]:
        if (seed := _pick_sniffer_seed(state)) is None:
            yield from _collect_drops(ex, state, lambda n: n in _SNIFFER_CROPS, radius=min(radius, 16))
            seed = _pick_sniffer_seed(state)
        if seed is None: return False
        blks = _ready(scan_blocks(ex, state, ("farmland", *_TILLABLE), radius=min(radius, 16), limit=12, skill_name=self.name))["blocks"]
        st = state.refresh_status(ex.client, max_age_s=0)
        cands = sorted(blks, key=lambda b: (0 if bare(b["name"]) == "farmland" else 1, eye_to_block_dist(st, b["pos"])))
        base_cell = next((c for b in cands if _obs_name(ex, ((c := block_cell(b["pos"]))[0], c[1] + 1, c[2])) in AIR), None)
        if base_cell is None: raise _env_blocked("附近未发现上方净空的耕地或可开垦草地/泥土", "reachable_planting_site:farmland", "半径 16 格")
        if _obs_name(ex, base_cell) != "farmland":
            if (hoe := _pick_hoe(state)) is None: raise Replan("背包缺少锄头（*_hoe），无法开垦耕地种植远古种子")
            yield from _approach_block(ex, state, base_cell, reach=STATION_EYE_REACH)
            yield from use_item_on(ex, state, base_cell, hoe)
            if _obs_name(ex, base_cell) != "farmland": raise Replan("锄地后未确认方块变为 farmland")
        yield from _approach_block(ex, state, base_cell, reach=STATION_EYE_REACH)
        before = state.count(seed)
        yield from use_item_on(ex, state, base_cell, seed)
        above = _obs_name(ex, (base_cell[0], base_cell[1] + 1, base_cell[2]))
        if not (state.count(seed) < before or above == _SNIFFER_CROPS[seed] or _has_adv(ex, "husbandry/plant_any_sniffer_seed")):
            raise Replan(f"种植 {seed} 后未确认背包减少或作物生成")
        self._done = True; return True
    def _feed_baby(self, ex: Executor, state: State, babies: list[dict]) -> Generator[dict, None, bool]:
        baby = min(babies, key=lambda e: eye_to_point_dist(state.refresh_status(ex.client, max_age_s=0), e["pos"]))
        yield from _approach_entity(ex, state, baby, reach=ENTITY_EYE_REACH)
        before = state.count("torchflower_seeds")
        yield from use_item_on(ex, state, baby["id"], "torchflower_seeds")
        return state.count("torchflower_seeds") < before or _has_adv(ex, "husbandry/feed_snifflet")
    def _feed_baby_or_breed(self, ex: Executor, state: State, sniffers: list[dict], radius: int) -> Generator[dict, None, None]:
        if state.count("torchflower_seeds") < 1: yield from _collect_drops(ex, state, lambda n: n == "torchflower_seeds", radius=min(radius, 16))
        if state.count("torchflower_seeds") < 1: raise Replan("背包缺少火把花种子（torchflower_seeds），无法喂食幼年嗅探兽")
        if babies := [e for e in sniffers if _is_baby(e)]:
            if not (yield from self._feed_baby(ex, state, babies)): raise Replan("喂食幼年嗅探兽后未确认火把花种子消耗或进度达成")
            self._done = True; return
        adults = [e for e in sniffers if not _is_baby(e)]
        if len(adults) >= 2 and state.count("torchflower_seeds") >= 2:
            st = state.refresh_status(ex.client, max_age_s=0)
            for parent in sorted(adults, key=lambda e: eye_to_point_dist(st, e["pos"]))[:2]:
                yield from _approach_entity(ex, state, parent, reach=ENTITY_EYE_REACH)
                yield from use_item_on(ex, state, parent["id"], "torchflower_seeds")
            fresh = [e for e in _ready(scan_entities(ex, state, "sniffer", radius=radius))["entities"] if _is_baby(e)]
            if fresh and state.count("torchflower_seeds") >= 1 and (yield from self._feed_baby(ex, state, fresh)): self._done = True; return
            yield from _collect_drops(ex, state, lambda n: n == "sniffer_egg", radius=12)
            raise Replan("waiting_sniffer_hatch")
        raise _env_blocked("附近未发现幼年嗅探兽且不满足成对繁殖条件", "visible_entity:snifflet", f"半径 {radius} 格实体")
    def _run(self, ex: Executor, state: State):
        self._begin(ex, state)
        mode, radius = self.args["mode"], self.args["radius"]
        if _has_adv(ex, "husbandry/plant_any_sniffer_seed" if mode == "plant_seed" else "husbandry/feed_snifflet"): self._done = True; return
        if mode == "plant_seed" and (yield from self._try_plant_seed(ex, state, radius)): return
        sniffers = _ready(scan_entities(ex, state, "sniffer", radius=radius))["entities"]
        if not sniffers and (yield from goto_known_landmark_or_biome(ex, state, "sniffer_hatch|sniffer")):
            sniffers = _ready(scan_entities(ex, state, "sniffer", radius=radius))["entities"]
        if not sniffers:
            if state.count("sniffer_egg") >= 1: yield from self._place_egg_to_hatch(ex, state, radius)
            if _ready(scan_blocks(ex, state, "sniffer_egg", radius=min(radius, 16), limit=4, skill_name=self.name))["blocks"]: raise Replan("waiting_sniffer_hatch")
            raise _env_blocked(f"半径 {radius} 格内未发现嗅探兽或可孵化的嗅探兽蛋", "visible_entity:sniffer", f"半径 {radius} 格实体")
        memo_for(state).record_landmark("sniffer", block_cell(sniffers[0]["pos"]), dimension=_dim(state))
        if mode == "plant_seed":
            if (yield from self._try_plant_seed(ex, state, radius)): return
            raise Replan("等待嗅探兽挖掘火把花种子或瓶子草荚果")
        yield from self._feed_baby_or_breed(ex, state, sniffers, radius)
