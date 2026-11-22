"""Stage 2 Overworld T2 Quick Wins L3 技能（7 项）。"""
from __future__ import annotations

from collections.abc import Sequence
import math
import time

from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, rec
from .encounter import _ARROWS, _retreat_clear, shoot_projectile, verify_shot_lane
from .field import (
    _cell, _safe_support, _standable_cell, collect_drops, equip_item, move_local, place_block,
    scan_blocks, scan_entities,
)
from .furnace import collect_furnace, load_furnace
from .furnace_lease import ensure_plank_fuel
from .interaction import _clear_posture, use_item_on
from .items import AIR, STONE_OR_BETTER_PICKS, bare, best_pickaxe
from .landmark_nav import goto_known_landmark_or_biome
from .mine_return import ensure_crafting_table_ready
from .observations import completed_advancements, observe_block, refresh_inventory
from .registry import register
from .sightline import (
    API_DIG_EYE_REACH, DIG_EYE_REACH, ENTITY_EYE_REACH, STATION_EYE_REACH,
    block_center, can_reach_block, eye_to_block_dist, eye_to_point_dist,
)
from .voxel_cache import voxel_cache_for
from .windows import window_amount, window_section, window_view
from .world_memo import memo_for
from .workstations import find_known_station, reclaim_field_station

_CARDINAL = ((1, 0), (-1, 0), (0, 1), (0, -1))
_OVERWORLD_WOODS = ("oak", "birch", "spruce", "jungle", "acacia", "dark_oak", "cherry", "mangrove", "pale_oak")
_STEM_WOODS = ("crimson", "warped")
_LOG_TO_PLANK = {
    **{f"{w}_log": f"{w}_planks" for w in _OVERWORLD_WOODS},
    **{f"stripped_{w}_log": f"{w}_planks" for w in _OVERWORLD_WOODS},
    **{f"{w}_wood": f"{w}_planks" for w in _OVERWORLD_WOODS},
    **{f"stripped_{w}_wood": f"{w}_planks" for w in _OVERWORLD_WOODS},
    **{f"{w}_stem": f"{w}_planks" for w in _STEM_WOODS},
    **{f"stripped_{w}_stem": f"{w}_planks" for w in _STEM_WOODS},
    **{f"{w}_hyphae": f"{w}_planks" for w in _STEM_WOODS},
    **{f"stripped_{w}_hyphae": f"{w}_planks" for w in _STEM_WOODS},
}
_LOG_ITEMS = tuple(_LOG_TO_PLANK.keys())
_PLANK_ITEMS = tuple(dict.fromkeys((*_LOG_TO_PLANK.values(), "bamboo_planks")))
_SIGN_BLOCKS = tuple(f"{w}_{k}" for w in (*_OVERWORLD_WOODS, "bamboo", *_STEM_WOODS) for k in ("sign", "wall_sign"))
_UNWAXED_COPPER = ("copper_block", "exposed_copper", "weathered_copper", "oxidized_copper", "cut_copper",
                   "exposed_cut_copper", "weathered_cut_copper", "oxidized_cut_copper", "chiseled_copper", "copper_grate", "copper_bulb")
_WAXED_COPPER = tuple(f"waxed_{c}" for c in _UNWAXED_COPPER)
_COPPER_ORES = ("copper_ore", "deepslate_copper_ore")
_FISH_SPECIES = ("cod", "salmon", "pufferfish", "tropical_fish")
_FISH_BUCKETS = tuple(f"{s}_bucket" for s in _FISH_SPECIES)
_SCULK_BLOCKS = ("sculk_sensor", "sculk_shrieker", "calibrated_sculk_sensor")
_WATER_PASSABLE = AIR | {"water", "flowing_water"}
_BURNABLE_PLANK_ITEMS = tuple(f"{w}_planks" for w in _OVERWORLD_WOODS) + ("bamboo_planks",)
_BURNABLE_LOG_ITEMS = tuple(
    n for w in _OVERWORLD_WOODS for n in (f"{w}_log", f"stripped_{w}_log", f"{w}_wood", f"stripped_{w}_wood")
)
_SMELT_FUELS: tuple[tuple[str, float], ...] = (
    ("coal", 8.0),
    ("charcoal", 8.0),
    ("coal_block", 80.0),
    *((p, 1.5) for p in _BURNABLE_PLANK_ITEMS),
    ("stick", 0.5),
    *((lg, 1.5) for lg in _BURNABLE_LOG_ITEMS),
)


def _obs(ex, pos) -> str: return bare(observe_block(ex, pos).get("name"))
def _is_air(ex, pos) -> bool: return _obs(ex, pos) in AIR
def _hdist(a, b) -> float: return math.hypot(a[0] - b[0], a[2] - b[2])


def _dim(state) -> str:
    raw = bare((state.st or {}).get("dimension") or "overworld").strip().lower()
    if raw in ("nether", "the_nether"):
        return "the_nether"
    if raw in ("end", "the_end"):
        return "the_end"
    return raw or "overworld"


def _blocked(msg: str, pred: str, vol: str) -> EnvBlocked:
    return EnvBlocked(msg, missing_predicate=pred, searched_volume=vol, wake_on="chunk_moved")


def _inv_item(state, pred) -> str | None:
    return next((n for r in (state.inventory or {}).get("items") or ()
                 if (n := bare(r.get("name"))) and pred(n) and state.count(n) >= 1), None)


def _count_logs(state) -> int: return sum(state.count(n) for n in _LOG_ITEMS)
def _count_planks(state) -> int: return sum(state.count(n) for n in _PLANK_ITEMS)
def _has_campfire(state) -> bool: return state.count("campfire") >= 1 or state.count("soul_campfire") >= 1


def _can_craft_sticks(state, need: int, *, reserve_logs: int = 0, reserve_planks: int = 0) -> bool:
    if state.count("stick") >= need:
        return True
    deficit = need - state.count("stick")
    if state.count("bamboo") >= deficit * 2:
        return True
    batches = math.ceil(deficit / 4)
    avail_planks = _count_planks(state) + max(0, _count_logs(state) - reserve_logs) * 4 - reserve_planks
    return avail_planks >= batches * 2


def _can_craft_campfire(state) -> bool:
    if _has_campfire(state):
        return True
    if state.count("coal") + state.count("charcoal") < 1 or _count_logs(state) < 3:
        return False
    return _can_craft_sticks(state, 3, reserve_logs=3)


def _can_craft_glass_bottle(state) -> bool:
    return state.count("glass_bottle") >= 1 or state.count("glass") >= 3


def _can_craft_wooden_axe(state) -> bool:
    if _inv_item(state, lambda n: n.endswith("_axe")) is not None:
        return True
    avail_planks = _count_planks(state) + _count_logs(state) * 4
    if avail_planks < 3:
        return False
    return _can_craft_sticks(state, 2, reserve_planks=3)


def _can_craft_brush(state) -> bool:
    if state.count("brush") >= 1:
        return True
    return state.count("copper_ingot") >= 1 and state.count("feather") >= 1 and _can_craft_sticks(state, 1)


def _has_leather_boots(state) -> bool:
    feet = bare((((state.inventory or {}).get("armor") or {}).get("feet") or {}).get("name"))
    return state.count("leather_boots") >= 1 or feet == "leather_boots"


def _can_get_leather_boots(state) -> bool:
    return _has_leather_boots(state) or state.count("leather") >= 4


def _can_craft_dried_ghast(state) -> bool:
    return state.count("soul_sand") >= 1 and state.count("ghast_tear") >= 8


def _can_craft_crossbow(state) -> bool:
    if state.count("crossbow") >= 1:
        return True
    if state.count("string") < 2:
        return False
    if state.count("tripwire_hook") >= 1:
        return state.count("iron_ingot") >= 1 and _can_craft_sticks(state, 3)
    if state.count("iron_ingot") < 2:
        return False
    avail_planks = _count_planks(state) + _count_logs(state) * 4
    return avail_planks >= 1 and _can_craft_sticks(state, 4, reserve_planks=1)


def _standable_near(ex, pt, origin, reach: float = STATION_EYE_REACH, *, target_block=None, passable=AIR):
    tx, ty, tz = _cell(target_block if target_block is not None else pt)
    cands = []
    for dy in (0, -1, 1, 2):
        for dx in range(-3, 4):
            for dz in range(-3, 4):
                cell = (tx + dx, ty + dy, tz + dz)
                cand = (cell[0] + 0.5, float(cell[1]), cell[2] + 0.5)
                if target_block is not None and dx == 0 and dz == 0 and abs(dy) <= 1:
                    continue
                if not _standable_cell(ex, cell):
                    continue
                ok = (can_reach_block(ex, cand, target_block, reach=reach, passable=passable)
                      if target_block is not None else eye_to_point_dist(cand, pt) <= reach)
                if ok:
                    cands.append(cand)
    return min(cands, key=lambda p: math.dist(p, origin)) if cands else None


def _reach_dest(ex, st, target_pos, reach: float, *, is_block: bool = False, passable=AIR):
    if is_block:
        if can_reach_block(ex, st, target_pos, reach=reach, passable=passable):
            return False, None
        return True, _standable_near(ex, block_center(target_pos), st["pos"], min(reach, DIG_EYE_REACH),
                                     target_block=target_pos, passable=passable)
    dist = eye_to_point_dist(st, target_pos)
    if dist <= reach:
        return False, None
    if reach > 4.0 and dist > 0:
        mid = tuple(a + (b - a) * (max(0.0, dist - min(reach - 2.0, 18.0)) / dist) for a, b in zip(st["pos"], target_pos))
        if (d := _standable_near(ex, mid, st["pos"], ENTITY_EYE_REACH)) is not None and eye_to_point_dist(d, target_pos) <= reach:
            return True, d
    return True, _standable_near(ex, target_pos, st["pos"], min(reach, ENTITY_EYE_REACH))


def _approach(ex, state, target_pos, *, reach: float = STATION_EYE_REACH, max_distance: int = 32, is_block: bool = False, passable=AIR):
    need, dest = _reach_dest(ex, state.refresh_status(ex.client, max_age_s=0), target_pos, reach, is_block=is_block, passable=passable)
    if need:
        if dest is None:
            raise _blocked("目标附近无安全站位", "standable_approach", "目标附近")
        yield from move_local(ex, state, dest, rng=1, max_distance=max_distance)


def _scan_req(ex, state, types, radius: int, msg: str, pred: str, *, entity: bool = False, reach: float = ENTITY_EYE_REACH, filt=None):
    scan = scan_entities(ex, state, types, radius=radius) if entity else scan_blocks(ex, state, types, radius=radius, limit=12)
    if scan.get("ready") is False:
        raise Replan("世界加载尚未就绪")
    st = state.refresh_status(ex.client, max_age_s=0)
    if not entity:
        blocks = scan["blocks"]
        if not blocks:
            raise _blocked(msg, pred, f"半径 {radius} 格")
        return sorted(blocks, key=lambda b: eye_to_block_dist(st, b["pos"]))
    for ent in sorted((e for e in scan["entities"] if filt is None or filt(e)), key=lambda e: eye_to_point_dist(st, e["pos"])):
        need, dest = _reach_dest(ex, st, ent["pos"], reach)
        if not need or dest is not None:
            return ent
    raise _blocked(msg, pred, f"半径 {radius} 格实体")


def _scan_req_or_landmark(ex, state, types, radius: int, msg: str, pred: str, *, landmark: str | Sequence[str] | None = None, entity: bool = False, reach: float = ENTITY_EYE_REACH, filt=None):
    try:
        return _scan_req(ex, state, types, radius, msg, pred, entity=entity, reach=reach, filt=filt)
    except EnvBlocked:
        if not landmark or not (yield from goto_known_landmark_or_biome(ex, state, landmark)):
            raise
        return _scan_req(ex, state, types, radius, msg, pred, entity=entity, reach=reach, filt=filt)


def _find_retreat(ex, state, away_from):
    st = state.refresh_status(ex.client, max_age_s=0)
    ox, oy, oz = _cell(st["pos"])
    cands = sorted(((ox + dx + 0.5, float(oy), oz + dz + 0.5) for dx, dz in _CARDINAL), key=lambda p: math.dist(p, away_from), reverse=True)
    return next((c for c in cands if _retreat_clear(ex, st["pos"], c)), None)


def _craft_with_table(ex, state, item: str, err_msg: str, *, require_table: bool = True, reserve_planks: int = 0):
    if require_table:
        st = state.refresh_status(ex.client, max_age_s=0)
        scan = scan_blocks(ex, state, "crafting_table", radius=16, limit=5)
        tpos = None
        if scan["blocks"]:
            tpos = tuple(min(scan["blocks"], key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
        else:
            ready = yield from ensure_crafting_table_ready(ex, state, reserve_planks=reserve_planks, skip_nearby_scan=True)
            if ready is not None:
                tpos = tuple(ready)
        if tpos is None:
            raise _blocked(f"合成 {item} 附近缺少可用工作台", "station_ready:crafting_table", "半径 16 格")
        st = state.refresh_status(ex.client, max_age_s=0)
        if not can_reach_block(ex, st, tpos, reach=STATION_EYE_REACH):
            yield rec("move_to", ex.move_to(pos=list(tpos), rng=2), pos=list(tpos))
            state.refresh_status(ex.client, max_age_s=0)
    before = state.count(item)
    yield rec("craft", ex.craft(item, 1), item=item, count=1)
    refresh_inventory(ex, state)
    if state.count(item) <= before:
        raise Replan(err_msg)


def _ensure_planks(ex, state, need_planks: int, *, reserve_logs: int = 0):
    while _count_planks(state) < need_planks:
        if _count_logs(state) <= reserve_logs:
            raise Replan("木板不足且保留原木后无法继续分解木板")
        log = next((lg for lg in _LOG_ITEMS if state.count(lg) >= 1), None)
        if log is None:
            raise Replan("背包缺少可分解的原木")
        plank = _LOG_TO_PLANK[log]
        yield from _craft_with_table(ex, state, plank, f"合成 {plank} 未增加数量", require_table=False)


def _ensure_sticks(ex, state, need_sticks: int, *, reserve_logs: int = 0, reserve_planks: int = 0):
    while state.count("stick") < need_sticks:
        if state.count("bamboo") < 2 and _count_planks(state) < 2 + reserve_planks:
            yield from _ensure_planks(ex, state, 2 + reserve_planks, reserve_logs=reserve_logs)
        yield from _craft_with_table(ex, state, "stick", "合成木棍未增加数量", require_table=False)


def _clear_floor_site(ex, state, radius: int = 3):
    st = state.refresh_status(ex.client, max_age_s=0)
    ox, oy, oz = _cell(st["pos"])
    cands = sorted(((ox + dx, oy + dy, oz + dz) for dy in (0, 1, -1) for dx in range(-radius, radius + 1)
                    for dz in range(-radius, radius + 1) if (ox + dx, oz + dz) != (ox, oz)),
                   key=lambda p: eye_to_block_dist(st, p))
    return next((p for p in cands if _is_air(ex, p) and _safe_support(ex, (p[0], p[1] - 1, p[2]))
                 and can_reach_block(ex, st, p, reach=DIG_EYE_REACH)), None)


def _use_check(ex, state, target, item: str, check, err_msg: str):
    ev = yield from use_item_on(ex, state, target, item)
    if not check(ev):
        raise Replan(err_msg)


def _pick_water_cell(ex, state, hits: Sequence[dict]):
    ox, oy, oz = _cell(state.refresh_status(ex.client, max_age_s=0)["pos"])
    ordered = sorted(
        hits,
        key=lambda b: (
            0 if (b.get("properties") or {}).get("level") in (None, 0, "0") else 1,
            eye_to_block_dist(state.st, b["pos"]),
        ),
    )
    return next((tuple(b["pos"]) for b in ordered if not (b["pos"][0] == ox and b["pos"][2] == oz and abs(b["pos"][1] - oy) <= 1)), None)


def _fill_water_bucket(ex, state, radius: int):
    if state.count("water_bucket") >= 1:
        return
    if state.count("bucket") < 1:
        raise Replan("背包缺少水桶或空桶")
    hits = _scan_req(ex, state, "water", min(radius, 32), "附近未找到水源，无法装水桶", "visible_block:water")
    wpos = _pick_water_cell(ex, state, hits) or tuple(hits[0]["pos"])
    yield from _approach(ex, state, wpos, reach=STATION_EYE_REACH, max_distance=min(radius, 32), is_block=True, passable=_WATER_PASSABLE)
    st = state.refresh_status(ex.client, max_age_s=0)
    if not can_reach_block(ex, st, wpos, reach=DIG_EYE_REACH, passable=_WATER_PASSABLE):
        raise Replan("接近水源后仍超出眼距或视线受阻")
    before_wb = state.count("water_bucket")
    yield from _use_check(ex, state, wpos, "bucket", lambda ev: state.count("water_bucket") > before_wb or ev["receipt"].get("changed") is True, "装水后未确认获得水桶")
    if state.count("water_bucket") < 1:
        raise Replan("装水后背包仍无水桶")


def _ensure_campfire(ex, state) -> str:
    if state.count("campfire") >= 1:
        return "campfire"
    if state.count("soul_campfire") >= 1:
        return "soul_campfire"
    if not _can_craft_campfire(state):
        raise Replan("背包缺少营火或合成材料（3原木+3木棍+1煤炭/木炭）")
    yield from _ensure_sticks(ex, state, 3, reserve_logs=3)
    yield from _craft_with_table(ex, state, "campfire", "合成营火未增加数量", reserve_planks=12)
    return "campfire"


def _ensure_campfire_under_nest(ex, state, npos: tuple[int, int, int]):
    nx, ny, nz = npos
    if any(_obs(ex, (nx, ny - d, nz)) in ("campfire", "soul_campfire") for d in (1, 2, 3)):
        return
    campfire_item = yield from _ensure_campfire(ex, state)
    ox, oy, oz = _cell(state.refresh_status(ex.client, max_age_s=0)["pos"])
    cpos = next(((nx, ny - d, nz) for d in (1, 2, 3)
                 if _is_air(ex, (nx, ny - d, nz)) and _safe_support(ex, (nx, ny - d - 1, nz))
                 and not ((nx, nz) == (ox, oz) and abs((ny - d) - oy) <= 1)), None)
    if cpos is None:
        raise Replan("蜂巢正下方缺少安全营火位置")
    yield from _approach(ex, state, cpos, reach=DIG_EYE_REACH, max_distance=16, is_block=True)
    yield from place_block(ex, state, campfire_item, cpos)


def _pick_smelt_fuel(state, need_items: int) -> tuple[str, int, int] | None:
    for item, rate in _SMELT_FUELS:
        avail = state.count(item)
        if avail < 1:
            continue
        batch = min(need_items, int(avail * rate))
        if batch >= 1:
            return item, math.ceil(batch / rate), batch
    return None


def _mine_raw_copper(ex, state, target_total: int, radius: int):
    while state.count("copper_ingot") + state.count("raw_copper") < target_total:
        pick = best_pickaxe(state)
        if pick not in STONE_OR_BETTER_PICKS:
            return
        scan = scan_blocks(ex, state, _COPPER_ORES, radius=radius, limit=8)
        if scan.get("ready") is False or not scan["blocks"]:
            return
        st = state.refresh_status(ex.client, max_age_s=0)
        ox, oy, oz = _cell(st["pos"])
        cands = [tuple(b["pos"]) for b in sorted(scan["blocks"], key=lambda b: eye_to_block_dist(st, b["pos"]))
                 if not (b["pos"][0] == ox and b["pos"][2] == oz and b["pos"][1] < oy)]
        if not cands:
            return
        opos = cands[0]
        yield from _approach(ex, state, opos, reach=DIG_EYE_REACH, max_distance=radius, is_block=True)
        yield from equip_item(ex, state, pick, "hand")
        st = state.refresh_status(ex.client, max_age_s=0)
        if not can_reach_block(ex, st, opos, reach=API_DIG_EYE_REACH):
            return
        before_raw = state.count("raw_copper")
        receipt = ex.dig(list(opos), collect=True)
        voxel_cache_for(state).invalidate(opos, dimension=_dim(state))
        yield rec("dig", receipt, pos=list(opos))
        refresh_inventory(ex, state)
        if state.count("raw_copper") <= before_raw:
            try:
                yield from collect_drops(ex, state, "raw_copper", count=1, radius=6, max_targets=2)
            except EnvBlocked:
                pass
            refresh_inventory(ex, state)
            if state.count("raw_copper") <= before_raw:
                return


def _smelt_copper_ingots(ex, state, target_ingots: int, radius: int):
    if state.count("copper_ingot") >= target_ingots:
        return
    need = target_ingots - state.count("copper_ingot")
    if state.count("raw_copper") < need:
        yield from _mine_raw_copper(ex, state, target_ingots, radius)
    need = target_ingots - state.count("copper_ingot")
    if need <= 0 or state.count("raw_copper") < need:
        return
    scan = scan_blocks(ex, state, ("blast_furnace", "furnace"), radius=16, limit=6)
    fpos = None
    station_kind = "furnace"
    newly_placed = False
    if scan.get("ready") is not False and scan["blocks"]:
        st = state.refresh_status(ex.client, max_age_s=0)
        best = min(scan["blocks"], key=lambda b: eye_to_block_dist(st, b["pos"]))
        fpos = tuple(best["pos"])
        station_kind = bare(best.get("name")) or "furnace"
    elif (known := find_known_station(ex, state, "furnace", max_dist=16.0)) is not None:
        fpos = known
    else:
        bag_furnace = "blast_furnace" if state.count("blast_furnace") >= 1 else ("furnace" if state.count("furnace") >= 1 else None)
        if bag_furnace and (site := _clear_floor_site(ex, state, 3)) is not None:
            yield from place_block(ex, state, bag_furnace, site)
            fpos = site
            station_kind = bag_furnace
            newly_placed = True
    if fpos is None:
        return
    yield from ensure_plank_fuel(ex, state, need, allow_lava_bucket=False)
    fuel_choice = _pick_smelt_fuel(state, need)
    if fuel_choice is None:
        return
    fuel_item, fuel_count, batch = fuel_choice
    if batch < need:
        return
    yield from _approach(ex, state, fpos, reach=STATION_EYE_REACH, max_distance=16, is_block=True)
    target = {"pos": list(fpos)}
    view = yield from window_view(ex, target)
    if bare(view.get("window")) not in ("furnace", "blast_furnace"):
        return
    if ready_out := window_amount(view, "output", "copper_ingot"):
        yield from collect_furnace(ex, state, target, item="copper_ingot", count=ready_out, section="output", view=view, inspect_after=False)
        if state.count("copper_ingot") >= target_ingots:
            yield from reclaim_field_station(ex, state, station_kind, fpos, newly_placed=newly_placed, furnace_emptied=True)
            return
        view = yield from window_view(ex, target)
    if not window_section(view, "input"):
        yield from load_furnace(
            ex, state, target, raw_item="raw_copper", raw_count=need,
            fuel_item=fuel_item, fuel_count=fuel_count, view=view, output_item="copper_ingot",
        )
    for attempt in range(16):
        if attempt:
            time.sleep(0.5)
        view = yield from window_view(ex, target)
        if ready_out := window_amount(view, "output", "copper_ingot"):
            yield from collect_furnace(ex, state, target, item="copper_ingot", count=ready_out, section="output", view=view, inspect_after=False)
            if state.count("copper_ingot") >= target_ingots:
                yield from reclaim_field_station(ex, state, station_kind, fpos, newly_placed=newly_placed, furnace_emptied=True)
                return
        elif not window_section(view, "input"):
            yield from reclaim_field_station(ex, state, station_kind, fpos, newly_placed=newly_placed, furnace_emptied=True)
            return


class _OverworldSkill(Skill):
    verified, _done_msg = "offline", "未确认技能完成"

    def _begin(self, ex, state):
        self._done = False
        refresh_inventory(ex, state)
        if reason := self.blocked(state):
            raise Replan(reason)
        if not self.can_start(state):
            raise Replan(f"{self.name} 当前条件不满足")

    def verify_done(self, state, before_counts=None) -> str:
        ok = getattr(self, "_done", False) or (bool(before_counts) and self._delta_done(state, before_counts))
        return "" if ok else self._done_msg

    def _delta_done(self, state, before: dict) -> bool: return False


@register
class SummonIronGolem(_OverworldSkill):
    name, description, goal = "summon_iron_golem", "放置 4 个铁块与雕刻南瓜召唤铁傀儡", "t2:iron_golem"
    verified = "live"
    _done_msg = "未确认铁傀儡建造或雕刻南瓜消耗"

    @classmethod
    def blocked(cls, state) -> str:
        if state.count("carved_pumpkin") < 1: return "背包缺少雕刻南瓜"
        return "" if (state.count("iron_block") >= 4 or state.count("iron_block") * 9 + state.count("iron_ingot") >= 36) else "缺少 4 个铁块或 36 个铁锭"

    def _delta_done(self, state, before: dict) -> bool: return state.count("carved_pumpkin") < before.get("carved_pumpkin", 0)

    def _find_t_shape(self, ex, state):
        ox, oy, oz = _cell(state.refresh_status(ex.client, max_age_s=0)["pos"])
        offsets = sorted(((dx, dz) for dx in range(-3, 4) for dz in range(-3, 4) if dx or dz), key=lambda d: d[0] * d[0] + d[1] * d[1])
        for by in (oy, oy - 1, oy + 1):
            for dx, dz in offsets:
                bx, bz = ox + dx, oz + dz
                if _safe_support(ex, (bx, by - 1, bz)):
                    for ax, az in ((1, 0), (0, 1)):
                        if (ox, oz) not in {(bx, bz), (bx + ax, bz + az), (bx - ax, bz - az)}:
                            pts = ((bx, by, bz), (bx, by + 1, bz), (bx + ax, by + 1, bz + az), (bx - ax, by + 1, bz - az), (bx, by + 2, bz))
                            if all(_is_air(ex, p) for p in (*pts, (bx + ax, by, bz + az), (bx - ax, by, bz - az))):
                                return pts
        return None

    def run(self, ex, state):
        self._begin(ex, state)
        while state.count("iron_block") < 4:
            yield from _craft_with_table(ex, state, "iron_block", "合成铁块未增加数量")
        if (shape := self._find_t_shape(ex, state)) is None:
            raise _blocked("附近缺少 T 字形铁傀儡空地", "clear_t_shape:iron_golem", "半径 3 格")
        for pos in shape[:-1]:
            yield from place_block(ex, state, "iron_block", pos)
        before_p, head = state.count("carved_pumpkin"), shape[-1]
        try:
            yield from place_block(ex, state, "carved_pumpkin", head)
        except Replan:
            refresh_inventory(ex, state)
            if state.count("carved_pumpkin") < before_p and _is_air(ex, head):
                self._done = True; return
            raise
        self._done = True


@register
class SniperDuel(_OverworldSkill):
    name, description, goal = "sniper_duel", "在 50 格以上用弓射杀骷髅完成狙击手对决", "t2:sniper_duel"
    verified = "live"
    params = {"radius": Param(int, "搜索骷髅半径", default=64, min=32, max=64)}
    _done_msg = "未确认 50 格狙击发射"

    @classmethod
    def blocked(cls, state) -> str:
        return "背包缺少弓" if state.count("bow") < 1 else ("" if sum(state.count(a) for a in _ARROWS) >= 4 else "背包箭矢不足 4 支")

    def _delta_done(self, state, before: dict) -> bool:
        return sum(state.count(a) for a in _ARROWS) < sum(before.get(a, 0) for a in _ARROWS)

    def _step_back_site(self, ex, st, target_pos):
        px, py, pz = st["pos"]
        dx, dz = px - target_pos[0], pz - target_pos[2]
        norm = math.hypot(dx, dz)
        ux, uz = (dx / norm, dz / norm) if norm > 1e-6 else (1.0, 0.0)
        for dist in (52.0, 51.0, 54.0, 56.0, 58.0, 60.0):
            for deg in (0, 15, -15, 30, -30):
                rad = math.radians(deg)
                rx, rz = ux * math.cos(rad) - uz * math.sin(rad), ux * math.sin(rad) + uz * math.cos(rad)
                for dy in (0, 1, -1):
                    cell = (math.floor(target_pos[0] + rx * dist), math.floor(py) + dy, math.floor(target_pos[2] + rz * dist))
                    dest = (cell[0] + 0.5, float(cell[1]), cell[2] + 0.5)
                    if 50.5 <= _hdist(dest, target_pos) <= 63.0 and math.dist(st["pos"], dest) <= 64.0 and _standable_cell(ex, cell):
                        return dest
        return None

    def run(self, ex, state):
        self._begin(ex, state); radius = self.args["radius"]
        scan = scan_entities(ex, state, ("skeleton", "parched"), radius=radius)
        if scan.get("ready") is False: raise Replan("世界加载尚未就绪")
        if not scan["entities"]: raise _blocked(f"半径 {radius} 格内未发现骷髅", "visible_entity:skeleton", f"半径 {radius} 格实体")
        st = state.refresh_status(ex.client, max_age_s=0)
        target = min(scan["entities"], key=lambda e: (0 if 50.0 <= _hdist(st["pos"], e["pos"]) <= 64.0 else 1, abs(_hdist(st["pos"], e["pos"]) - 52.0)))
        if _hdist(st["pos"], target["pos"]) < 50.0:
            if (dest := self._step_back_site(ex, st, target["pos"])) is None:
                raise _blocked("无法找到 50 格以上安全后退站位", "sniper_range:>=50", f"半径 {radius} 格")
            yield from move_local(ex, state, dest, rng=1, max_distance=64)
            if _hdist(state.refresh_status(ex.client, max_age_s=0)["pos"], target["pos"]) < 50.0:
                raise _blocked("后退后仍不足 50 格水平射距", "sniper_range:>=50", f"半径 {radius} 格")
        if (retreat := _find_retreat(ex, state, target["pos"])) is None: raise Replan("缺少同高水平撤离格")
        aiming = (target["pos"][0], target["pos"][1] + 0.5, target["pos"][2])
        if (lane := verify_shot_lane(ex, state, aiming, retreat_pos=retreat, min_horizontal=50, max_distance=64))["status"] != "clear":
            raise Replan(lane["reason"])
        yield from shoot_projectile(ex, state, entity=target, weapon="bow", charge_ms=1100, retreat_pos=retreat, min_horizontal=50, max_distance=64)
        self._done = True


@register
class MakeSignGlow(_OverworldSkill):
    name, description, goal = "make_sign_glow", "放置或复用告示牌并用荧光墨囊使其发光", "t2:glow_sign"
    verified = "live"
    _done_msg = "未确认对告示牌使用荧光墨囊"

    @classmethod
    def blocked(cls, state) -> str:
        if state.count("glow_ink_sac") < 1: return "背包缺少荧光墨囊"
        ok = _inv_item(state, lambda n: n.endswith("_sign")) is not None or (state.count("oak_planks") >= 6 and state.count("stick") >= 1)
        return "" if ok else "缺少告示牌或制作材料（6橡木板+1木棍）"

    def _delta_done(self, state, before: dict) -> bool: return state.count("glow_ink_sac") < before.get("glow_ink_sac", 0)

    def run(self, ex, state):
        self._begin(ex, state)
        scan = scan_blocks(ex, state, _SIGN_BLOCKS, radius=8, limit=5)
        if scan["blocks"]:
            sign_pos = tuple(scan["blocks"][0]["pos"])
            yield from _approach(ex, state, sign_pos, reach=STATION_EYE_REACH, max_distance=16, is_block=True)
        else:
            if (sign_item := _inv_item(state, lambda n: n.endswith("_sign"))) is None:
                yield from _craft_with_table(ex, state, "oak_sign", "合成橡木告示牌未增加数量"); sign_item = "oak_sign"
            if (sign_pos := _clear_floor_site(ex, state, 3)) is None:
                raise _blocked("附近缺少放置告示牌的空地", "clear_site:sign", "半径 3 格平面")
            yield from place_block(ex, state, sign_item, sign_pos)
        before_ink = state.count("glow_ink_sac")
        yield from _use_check(ex, state, sign_pos, "glow_ink_sac", lambda ev: state.count("glow_ink_sac") < before_ink or ev["receipt"].get("changed") is True, "未确认荧光墨囊生效")
        self._done = True


@register
class CatchAquaticMob(_OverworldSkill):
    name, description, goal = "catch_aquatic_mob", "靠近鱼类或美西螈并用水桶捕捉", "t2:aquatic_{target}"
    verified = "live"
    params = {"target": Param(str, "目标水生生物", default="fish", choices=("fish", "axolotl")), "radius": Param(int, "搜索半径", default=32, min=8, max=64)}
    _done_msg = "未确认捕获目标水生生物"

    @classmethod
    def blocked(cls, state) -> str:
        return "" if (state.count("water_bucket") >= 1 or state.count("bucket") >= 1) else "背包缺少水桶或空桶"

    def _delta_done(self, state, before: dict) -> bool:
        buckets = _FISH_BUCKETS if self.args["target"] == "fish" else ("axolotl_bucket",)
        return sum(state.count(b) for b in buckets) > sum(before.get(b, 0) for b in buckets)

    def run(self, ex, state):
        self._begin(ex, state); target, radius = self.args["target"], self.args["radius"]
        species, buckets = (_FISH_SPECIES, _FISH_BUCKETS) if target == "fish" else (("axolotl",), ("axolotl_bucket",))
        ent = yield from _scan_req_or_landmark(
            ex, state, species, radius, f"未找到可达的 {target}", f"visible_entity:{target}",
            landmark="lush_caves" if target == "axolotl" else None, entity=True,
        )
        if state.count("water_bucket") < 1:
            yield from _fill_water_bucket(ex, state, radius)
            refreshed = scan_entities(ex, state, species, radius=radius)
            if refreshed.get("ready") is not False:
                ent = next((e for e in refreshed["entities"] if e["id"] == ent["id"]), ent)
        yield from _approach(ex, state, ent["pos"], reach=ENTITY_EYE_REACH, max_distance=radius)
        before_b = sum(state.count(b) for b in buckets)
        yield from _use_check(ex, state, ent["id"], "water_bucket", lambda _: sum(state.count(b) for b in buckets) > before_b, "捕捉水生生物后未确认获得对应生物桶")
        self._done = True


@register
class TameAnimal(_OverworldSkill):
    name, description, goal = "tame_animal", "用骨头驯服狼或用生鳕鱼/鲑鱼驯服猫", "t2:tame"
    verified = "live"
    params = {"radius": Param(int, "搜索动物半径", default=32, min=8, max=64)}
    _done_msg = "未确认动物驯服完成"

    @classmethod
    def blocked(cls, state) -> str:
        return "" if any(state.count(f) >= 1 for f in ("bone", "cod", "salmon")) else "背包缺少驯服食物（bone/cod/salmon）"

    def _delta_done(self, state, before: dict) -> bool: return any(state.count(f) < before.get(f, 0) for f in ("bone", "cod", "salmon"))

    def run(self, ex, state):
        self._begin(ex, state)
        species = (["wolf"] if state.count("bone") >= 1 else []) + (["cat"] if state.count("cod") >= 1 or state.count("salmon") >= 1 else [])
        ent = _scan_req(ex, state, tuple(species), self.args["radius"], "附近未找到可驯服的狼或猫", "visible_entity:tameable", entity=True, filt=lambda e: not e.get("tamed"))
        yield from _approach(ex, state, ent["pos"], reach=ENTITY_EYE_REACH, max_distance=self.args["radius"])
        food = "bone" if bare(ent["name"]) == "wolf" else ("cod" if state.count("cod") >= 1 else "salmon")
        start_food, tamed = state.count(food), False
        for _ in range(4):
            if state.count(food) < 1: break
            ev = yield from use_item_on(ex, state, ent["id"], food)
            fresh = next((e for e in scan_entities(ex, state, bare(ent["name"]), radius=8)["entities"] if e["id"] == ent["id"]), None)
            if (fresh and fresh.get("tamed")) or ev["receipt"].get("tamed") or (hasattr(ex.client, "advancements") and "husbandry/tame_an_animal" in completed_advancements(ex)):
                tamed = True; break
        if not tamed and state.count(food) >= start_food: raise Replan("投喂后未消耗食物且未确认驯服")
        self._done = True


@register
class HarvestHoneyAndWax(_OverworldSkill):
    name, description, goal = "harvest_honey_wax", "安全放置营火采蜜或为铜方块涂蜡/刮蜡", "t2:bee_{mode}"
    verified = "live"
    params = {"mode": Param(str, "蜂巢与涂蜡模式", default="honey", choices=("honey", "wax_on", "wax_off")), "radius": Param(int, "搜索半径", default=32, min=8, max=64)}
    _done_msg = "未确认蜂巢采蜜或铜块涂/刮蜡完成"

    @classmethod
    def _can_honey(cls, state) -> bool:
        return _can_craft_campfire(state) and _can_craft_glass_bottle(state)

    @classmethod
    def _can_wax_on(cls, state) -> bool:
        return state.count("honeycomb") >= 1 or state.count("shears") >= 1

    @classmethod
    def _can_wax_off(cls, state) -> bool:
        return _can_craft_wooden_axe(state)

    @classmethod
    def blocked(cls, state) -> str:
        ok = cls._can_honey(state) or cls._can_wax_on(state) or cls._can_wax_off(state)
        return "" if ok else "缺少蜂巢采蜜或涂蜡/刮蜡所需物品或合成材料"

    def can_start(self, state) -> bool:
        m = self.args["mode"]
        if m == "honey":
            return self._can_honey(state)
        if m == "wax_on":
            return self._can_wax_on(state)
        return self._can_wax_off(state)

    def _shear_honeycomb_from_nest(self, ex, state, radius: int):
        if state.count("honeycomb") >= 1:
            return
        if state.count("shears") < 1:
            raise Replan("背包缺少蜜脾与剪刀，无法采集蜜脾")
        hits = yield from _scan_req_or_landmark(ex, state, ("bee_nest", "beehive"), radius, "未找到蜂巢或蜂箱", "visible_block:bee_nest", landmark="bee_nest")
        npos = tuple(hits[0]["pos"])
        memo_for(state).record_landmark("bee_nest", npos, dimension=_dim(state))
        yield from _approach(ex, state, npos, reach=STATION_EYE_REACH, max_distance=radius, is_block=True)
        yield from _ensure_campfire_under_nest(ex, state, npos)
        yield from _approach(ex, state, npos, reach=STATION_EYE_REACH, max_distance=radius, is_block=True)
        before_hc = state.count("honeycomb")
        ev = yield from use_item_on(ex, state, npos, "shears")
        if state.count("honeycomb") <= before_hc:
            try:
                yield from collect_drops(ex, state, "honeycomb", count=1, radius=6, max_targets=2)
            except EnvBlocked:
                pass
            refresh_inventory(ex, state)
        if state.count("honeycomb") <= before_hc and ev["receipt"].get("changed") is not True:
            raise Replan("剪取蜂巢后未确认获得蜜脾")
        if state.count("honeycomb") < 1:
            raise Replan("剪取蜂巢后背包仍无蜜脾")

    def _obtain_unwaxed_copper(self, ex, state, radius: int) -> tuple[int, int, int]:
        scan = scan_blocks(ex, state, _UNWAXED_COPPER, radius=radius, limit=12)
        if scan.get("ready") is False:
            raise Replan("世界加载尚未就绪")
        if scan["blocks"]:
            st = state.refresh_status(ex.client, max_age_s=0)
            cpos = tuple(min(scan["blocks"], key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
            yield from _approach(ex, state, cpos, reach=STATION_EYE_REACH, max_distance=radius, is_block=True)
            return cpos
        if (bag_copper := next((c for c in _UNWAXED_COPPER if state.count(c) >= 1), None)) is None:
            if state.count("copper_ingot") < 9:
                yield from _smelt_copper_ingots(ex, state, 9, radius)
            if state.count("copper_ingot") >= 9:
                yield from _craft_with_table(ex, state, "copper_block", "合成铜块未增加数量")
                bag_copper = "copper_block"
        if bag_copper is not None:
            if (cpos := _clear_floor_site(ex, state, 3)) is None:
                raise _blocked("附近缺少放置铜方块的空地", "clear_site:copper_block", "半径 3 格平面")
            yield from place_block(ex, state, bag_copper, cpos)
            return cpos
        if (yield from goto_known_landmark_or_biome(ex, state, ("trial_chambers", "bee_nest"))):
            hits = _scan_req(ex, state, _UNWAXED_COPPER, radius, "附近未找到可涂蜡的铜方块", "visible_block:copper_block")
            cpos = tuple(hits[0]["pos"])
            yield from _approach(ex, state, cpos, reach=STATION_EYE_REACH, max_distance=radius, is_block=True)
            return cpos
        raise _blocked("附近未找到可涂蜡的铜方块", "visible_block:copper_block", f"半径 {radius} 格")

    def run(self, ex, state):
        self._begin(ex, state); mode, radius = self.args["mode"], self.args["radius"]
        if mode == "honey":
            if state.count("glass_bottle") < 1:
                if state.count("glass") < 3:
                    raise Replan("背包缺少玻璃瓶或 3 块玻璃")
                yield from _craft_with_table(ex, state, "glass_bottle", "合成玻璃瓶未增加数量")
            hits = yield from _scan_req_or_landmark(ex, state, ("bee_nest", "beehive"), radius, "未找到蜂巢或蜂箱", "visible_block:bee_nest", landmark="bee_nest")
            npos = tuple(hits[0]["pos"])
            memo_for(state).record_landmark("bee_nest", npos, dimension=_dim(state))
            yield from _approach(ex, state, npos, reach=STATION_EYE_REACH, max_distance=radius, is_block=True)
            yield from _ensure_campfire_under_nest(ex, state, npos)
            yield from _approach(ex, state, npos, reach=STATION_EYE_REACH, max_distance=radius, is_block=True)
            before_hb = state.count("honey_bottle")
            yield from _use_check(ex, state, npos, "glass_bottle", lambda ev: state.count("honey_bottle") > before_hb or ev["receipt"].get("changed") is True, "采蜜后未确认获得蜂蜜瓶")
        elif mode == "wax_on":
            if state.count("honeycomb") < 1:
                yield from self._shear_honeycomb_from_nest(ex, state, radius)
            cpos = yield from self._obtain_unwaxed_copper(ex, state, radius)
            before_hc = state.count("honeycomb")
            yield from _use_check(ex, state, cpos, "honeycomb", lambda ev: state.count("honeycomb") < before_hc or _obs(ex, cpos).startswith("waxed_") or ev["receipt"].get("changed") is True, "涂蜡后未确认生效")
            memo_for(state).record_landmark("waxed_copper", cpos, dimension=_dim(state))
        else:
            axe = _inv_item(state, lambda n: n.endswith("_axe"))
            if axe is None:
                yield from _ensure_sticks(ex, state, 2, reserve_planks=3)
                yield from _ensure_planks(ex, state, 3)
                yield from _craft_with_table(ex, state, "wooden_axe", "合成木斧未增加数量", reserve_planks=3)
                axe = "wooden_axe"
            try:
                hits = yield from _scan_req_or_landmark(
                    ex, state, _WAXED_COPPER, radius, "未找到已涂蜡铜方块", "visible_block:waxed_copper",
                    landmark=("waxed_copper", "bee_nest", "trial_chambers"),
                )
                wpos = tuple(hits[0]["pos"])
                yield from _approach(ex, state, wpos, reach=STATION_EYE_REACH, max_distance=radius, is_block=True)
            except EnvBlocked:
                bag_waxed = next((c for c in _WAXED_COPPER if state.count(c) >= 1), None)
                if bag_waxed is None or (wpos := _clear_floor_site(ex, state, 3)) is None:
                    raise
                yield from place_block(ex, state, bag_waxed, wpos)
            yield from _use_check(ex, state, wpos, axe, lambda ev: not _obs(ex, wpos).startswith("waxed_") or ev["receipt"].get("changed") is True, "刮蜡后方块仍带蜡")
        self._done = True


@register
class BiomeQuickAction(_OverworldSkill):
    name, description, goal = "biome_quick_action", "执行潜行避震、刷犰狳、踩细雪、浸水恶魂或弩杀掠夺者", "t2:biome_{action}"
    verified = "live"
    params = {"action": Param(str, "群系顺手任务", default="avoid_vibration", choices=("avoid_vibration", "brush_armadillo", "powder_snow", "dried_ghast", "crossbow_pillager")), "radius": Param(int, "搜索半径", default=32, min=8, max=64)}
    _done_msg = "未确认群系顺手动作完成"

    @classmethod
    def blocked(cls, state) -> str:
        items = [r for r in ((state.inventory or {}).get("items") or ()) if isinstance(r, dict) and int(r.get("count", 0)) >= 1]
        dim = _dim(state)
        memo = memo_for(state)
        if (
            items
            or _has_leather_boots(state)
            or dim == "the_nether"
            or memo.has_landmark("deep_dark", dimension=dim)
            or "deep_dark" in memo.biomes(dimension=dim)
            or memo.has_landmark("dried_ghast", dimension=dim)
            or memo.has_landmark("soul_sand_valley", dimension=dim)
        ):
            return ""
        return "起步空包且未发现深暗/群系前置，暂不启动群系顺手任务"

    def can_start(self, state) -> bool:
        a = self.args["action"]
        if a == "avoid_vibration":
            return True
        if a == "powder_snow":
            return _can_get_leather_boots(state)
        if a == "brush_armadillo":
            return _can_craft_brush(state)
        if a == "dried_ghast":
            return state.count("dried_ghast") >= 1 or _can_craft_dried_ghast(state) or _dim(state) == "the_nether"
        return _can_craft_crossbow(state) and sum(state.count(k) for k in _ARROWS) >= 2

    def _delta_done(self, state, before: dict) -> bool:
        a = self.args["action"]
        if a == "brush_armadillo":
            return state.count("armadillo_scute") > before.get("armadillo_scute", 0)
        if a == "dried_ghast":
            return state.count("dried_ghast") != before.get("dried_ghast", 0)
        if a == "crossbow_pillager":
            return sum(state.count(k) for k in _ARROWS) < sum(before.get(k, 0) for k in _ARROWS)
        return False

    def _avoid_vibration(self, ex, state, radius: int):
        hits = yield from _scan_req_or_landmark(ex, state, _SCULK_BLOCKS, radius, "未找到幽匿传感器或尖啸体", "visible_block:sculk_sensor", landmark="deep_dark")
        sensor_pos = tuple(hits[0]["pos"])
        memo_for(state).record_landmark("sculk_sensor", sensor_pos, dimension=_dim(state))
        st = state.refresh_status(ex.client, max_age_s=0)
        if eye_to_block_dist(st, sensor_pos) > 7.5:
            yield from _approach(ex, state, block_center(sensor_pos), reach=6.5, max_distance=radius, is_block=False)
        ox, oy, oz = _cell(state.refresh_status(ex.client, max_age_s=0)["pos"])
        steps = next((((ox + dx + 0.5, float(oy), oz + dz + 0.5), (ox + 2 * dx + 0.5, float(oy), oz + 2 * dz + 0.5))
                      for dx, dz in _CARDINAL if _standable_cell(ex, (ox + dx, oy, oz + dz)) and _standable_cell(ex, (ox + 2 * dx, oy, oz + 2 * dz))), None)
        if steps is None: raise _blocked("附近缺少两格连续安全潜行地面", "standable_path:2", "四邻 2 格")
        try:
            yield rec("posture", ex.posture(sneak=True), sneak=True)
            yield from move_local(ex, state, steps[0], rng=1, max_distance=8, require_move=True)
            yield from move_local(ex, state, steps[1], rng=1, max_distance=8, require_move=True)
        finally:
            cleared = _clear_posture(ex, state, sneak=False)
        yield rec("posture", cleared, sneak=False)

    def _powder_snow(self, ex, state, radius: int):
        if not _has_leather_boots(state):
            if state.count("leather") < 4:
                raise Replan("背包缺少皮革靴或 4 块皮革")
            yield from _craft_with_table(ex, state, "leather_boots", "合成皮革靴子未增加数量")
        yield from equip_item(ex, state, "leather_boots", slot="feet")
        hits = yield from _scan_req_or_landmark(ex, state, "powder_snow", radius, "未找到细雪方块", "visible_block:powder_snow", landmark="powder_snow")
        memo_for(state).record_landmark("powder_snow", hits[0]["pos"], dimension=_dim(state))
        ps = next((tuple(b["pos"]) for b in hits if _is_air(ex, (b["pos"][0], b["pos"][1] + 1, b["pos"][2])) and _is_air(ex, (b["pos"][0], b["pos"][1] + 2, b["pos"][2]))), None)
        if ps is None: raise _blocked("细雪上方净空不足", "clear_above:powder_snow", f"半径 {radius} 格")
        top = [ps[0] + 0.5, ps[1] + 1.0, ps[2] + 0.5]
        st = state.refresh_status(ex.client, max_age_s=0)
        if math.dist(st["pos"], top) > 3.5:
            if (dest := _standable_near(ex, top, st["pos"], ENTITY_EYE_REACH)) is None: raise _blocked("细雪旁无安全落点", "standable_approach", f"半径 {radius} 格")
            yield from move_local(ex, state, dest, rng=1, max_distance=radius)
        yield rec("move_to", ex.move_to(pos=top, rng=1), pos=top)
        if math.dist(state.refresh_status(ex.client, max_age_s=0)["pos"], top) > 1.65: raise Replan("移动后未确认踏上细雪")

    def _harvest_dried_ghast_in_nether(self, ex, state, radius: int):
        if _can_craft_dried_ghast(state):
            yield from _craft_with_table(ex, state, "dried_ghast", "合成干涸恶魂块未增加数量")
            return
        hits = yield from _scan_req_or_landmark(
            ex, state, "dried_ghast", radius, "下界灵魂沙峡谷附近未找到干涸恶魂块",
            "visible_block:dried_ghast", landmark="dried_ghast|soul_sand_valley",
        )
        dg_pos = tuple(hits[0]["pos"])
        memo_for(state).record_landmark("dried_ghast", dg_pos, dimension=_dim(state))
        yield from _approach(ex, state, dg_pos, reach=DIG_EYE_REACH, max_distance=radius, is_block=True)
        st = state.refresh_status(ex.client, max_age_s=0)
        ox, oy, oz = _cell(st["pos"])
        if dg_pos[0] == ox and dg_pos[2] == oz and dg_pos[1] < oy:
            raise Replan("干涸恶魂块位于脚下支撑格，停止直接下挖")
        if not can_reach_block(ex, st, dg_pos, reach=API_DIG_EYE_REACH):
            raise Replan("接近干涸恶魂块后仍超出挖掘眼距或视线受阻")
        before_dg = state.count("dried_ghast")
        receipt = ex.dig(list(dg_pos), collect=True)
        voxel_cache_for(state).invalidate(dg_pos, dimension=_dim(state))
        yield rec("dig", receipt, pos=list(dg_pos))
        refresh_inventory(ex, state)
        if state.count("dried_ghast") <= before_dg:
            try:
                yield from collect_drops(ex, state, "dried_ghast", count=1, radius=6, max_targets=2)
            except EnvBlocked:
                pass
            refresh_inventory(ex, state)
        if state.count("dried_ghast") <= before_dg:
            raise Replan("挖掘干涸恶魂块后背包未确认获得 dried_ghast")

    def _dried_ghast(self, ex, state, radius: int):
        dim = _dim(state)
        if dim == "the_nether":
            if state.count("dried_ghast") >= 1:
                raise _blocked("已持有干涸恶魂块，下界无法放水，请返回主世界再浸水", "dimension:overworld", "the_nether")
            yield from self._harvest_dried_ghast_in_nether(ex, state, radius)
            return
        if state.count("dried_ghast") < 1:
            if not _can_craft_dried_ghast(state):
                raise _blocked("主世界背包无干涸恶魂块且缺合成材料（1灵魂沙+8恶魂之泪），需先在下界灵魂沙峡谷采集", "item:dried_ghast", dim)
            yield from _craft_with_table(ex, state, "dried_ghast", "合成干涸恶魂块未增加数量")
        try:
            hits = _scan_req(ex, state, "water", radius, "附近未找到水源", "visible_block:water")
            wpos = _pick_water_cell(ex, state, hits) or tuple(hits[0]["pos"])
        except EnvBlocked:
            if state.count("water_bucket") < 1 or (site := _clear_floor_site(ex, state, 3)) is None:
                raise
            yield from _use_check(ex, state, (site[0], site[1] - 1, site[2]), "water_bucket",
                                  lambda ev: _obs(ex, site) == "water" or ev["receipt"].get("changed") is True, "倒水后未确认生成水源")
            wpos = site
        yield from _approach(ex, state, wpos, reach=DIG_EYE_REACH, max_distance=radius, is_block=True, passable=_WATER_PASSABLE)
        st = state.refresh_status(ex.client, max_age_s=0)
        if not can_reach_block(ex, st, wpos, reach=DIG_EYE_REACH, passable=_WATER_PASSABLE):
            raise Replan("接近水源后仍超出放置眼距或视线受阻")
        before_dg = state.count("dried_ghast")
        receipt = ex.place("dried_ghast", pos=list(wpos))
        voxel_cache_for(state).invalidate(wpos, dimension=dim)
        yield rec("place", receipt, item="dried_ghast", pos=list(wpos))
        refresh_inventory(ex, state)
        if state.count("dried_ghast") >= before_dg and _obs(ex, wpos) != "dried_ghast":
            raise Replan("放置干涸恶魂入水未确认")

    def _brush_armadillo(self, ex, state, radius: int):
        if state.count("brush") < 1:
            if not _can_craft_brush(state):
                raise Replan("背包缺少刷子或合成材料（1铜锭+1羽毛+1木棍）")
            yield from _ensure_sticks(ex, state, 1)
            yield from _craft_with_table(ex, state, "brush", "合成刷子未增加数量")
        ent = yield from _scan_req_or_landmark(ex, state, "armadillo", radius, "未找到可达犰狳", "visible_entity:armadillo", landmark="savanna|badlands", entity=True)
        yield from _approach(ex, state, ent["pos"], reach=ENTITY_EYE_REACH, max_distance=radius)
        before_sc = state.count("armadillo_scute")
        yield from _use_check(ex, state, ent["id"], "brush", lambda ev: state.count("armadillo_scute") > before_sc or ev["receipt"].get("changed") is True, "刷犰狳后未确认掉落或交互成功")

    def _crossbow_pillager(self, ex, state, radius: int):
        if state.count("crossbow") < 1:
            if not _can_craft_crossbow(state):
                raise Replan("背包缺少弩或合成材料")
            if state.count("tripwire_hook") < 1:
                yield from _ensure_sticks(ex, state, 4, reserve_planks=1)
                yield from _ensure_planks(ex, state, 1)
                yield from _craft_with_table(ex, state, "tripwire_hook", "合成绊线钩未增加数量", reserve_planks=1)
            else:
                yield from _ensure_sticks(ex, state, 3)
            yield from _craft_with_table(ex, state, "crossbow", "合成弩未增加数量")
        ent = yield from _scan_req_or_landmark(ex, state, "pillager", radius, "未找到可达掠夺者", "visible_entity:pillager", landmark="pillager_outpost", entity=True, reach=24.0)
        yield from _approach(ex, state, ent["pos"], reach=24.0, max_distance=radius)
        if (retreat := _find_retreat(ex, state, ent["pos"])) is None: raise Replan("缺少同高水平撤离格")
        yield from shoot_projectile(ex, state, entity=ent, weapon="crossbow", charge_ms=1300, retreat_pos=retreat, max_distance=32)

    def run(self, ex, state):
        self._begin(ex, state); action, radius = self.args["action"], self.args["radius"]
        yield from getattr(self, f"_{action}")(ex, state, radius)
        self._done = True
