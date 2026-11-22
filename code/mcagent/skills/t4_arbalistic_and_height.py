"""Stage 3 T4 穿透一箭穿五与世界最高高度交易技能（2 项 L3 注册技能）。"""
from __future__ import annotations

import math
from collections.abc import Generator, Iterable, Mapping, Sequence
from typing import Any

from ..client import ApiError
from ..loop.subgoals import enchantment_ready, mob_collection_ready
from ..runtime import EnvBlocked, Executor, Replan, State
from .base import Param, Skill, failure, rec
from .interaction import _clear_posture
from .items import AIR, bare, best_pickaxe, is_clear_space, is_safe_solid_floor, pick_placeable, total_placeable
from .nav import step_up_1x2
from .observations import observe_block, refresh_inventory
from .registry import register
from .sightline import (DIG_EYE_REACH, ENTITY_EYE_REACH, STATION_EYE_REACH, block_cell,
                        block_center, can_reach_block, eye_to_block_dist, eye_to_point_dist, feet_pos)
from .trade_smith_brew import trade_once
from .voxel_cache import voxel_cache_for
from .world_memo import memo_for

_MERCHANTS = ("wandering_trader", "villager")
_FISH_BUCKETS = ("cod_bucket", "salmon_bucket", "tropical_fish_bucket", "pufferfish_bucket")
_AQUATIC_BUCKETS = (*_FISH_BUCKETS, "tadpole_bucket", "axolotl_bucket")
_AQUATIC_MOBS = ("cod", "salmon", "tropical_fish", "pufferfish", "tadpole", "axolotl")
_ANVILS = ("anvil", "chipped_anvil", "damaged_anvil")
_PORTAL_RING = ((1, 0), (2, 0), (0, 1), (3, 1), (0, 2), (3, 2), (0, 3), (3, 3), (1, 4), (2, 4))
_WEAPONS = frozenset({"crossbow", "bow", "trident", "mace"})
_CTRL_FALLBACK = frozenset({"unsupported", "unsupported_api", "bad_args", "unknown_op"})
_HIGH_PORTAL_Y, _HIGH_TRADE_Y = 314, 319

def _dim(state: State) -> str: return bare((state.st or {}).get("dimension")) or "overworld"
def _env_blocked(msg: str, pred: str, vol: str) -> EnvBlocked:
    return EnvBlocked(msg, missing_predicate=pred, searched_volume=vol, wake_on="chunk_moved")
def _is_weapon(name: str) -> bool:
    b = bare(name); return b in _WEAPONS or b.endswith(("_sword", "_axe", "_pickaxe", "_shovel", "_hoe"))
def _cur_y(state: State) -> float:
    pos = (state.st or {}).get("pos")
    if isinstance(pos, Sequence) and not isinstance(pos, (str, bytes)) and len(pos) >= 2 and isinstance(pos[1], (int, float)) and not isinstance(pos[1], bool) and math.isfinite(pos[1]):
        return float(pos[1])
    return 0.0
def _inv_stacks(state: State) -> Iterable[Mapping[str, Any]]:
    inv = state.inventory or {}
    return (r for r in (*(inv.get("items") or ()), inv.get("held"), inv.get("offhand"))
            if isinstance(r, Mapping) and (r.get("count", 1) or 0) >= 1 and bare(r.get("name")))
def _has_inv(state: State) -> bool: return any(state.count(bare(s.get("name"))) > 0 for s in _inv_stacks(state))

def _stack_ench_level(stack: Mapping[str, Any], ench_id: str) -> int:
    target, enchants = bare(ench_id).lower(), stack.get("enchantments")
    if isinstance(enchants, Mapping):
        return next((v for k, v in enchants.items() if bare(k).lower() == target and type(v) is int), 0)
    if isinstance(enchants, Iterable) and not isinstance(enchants, (str, bytes)):
        for row in enchants:
            if not isinstance(row, Mapping): continue
            lvl = row.get("level") if row.get("level") is not None else row.get("lvl")
            if bare(row.get("name") or row.get("id")).lower() == target and type(lvl) is int: return lvl
    return 0

def _has_multishot_crossbow(state: State) -> bool:
    return any(bare(s.get("name")) == "crossbow" and _stack_ench_level(s, "multishot") >= 1 for s in _inv_stacks(state))
def _has_valid_piercing_iv_crossbow(state: State) -> bool:
    return enchantment_ready(state, "crossbow", "piercing", 4) and any(
        bare(s.get("name")) == "crossbow" and _stack_ench_level(s, "piercing") >= 4 and _stack_ench_level(s, "multishot") == 0
        for s in _inv_stacks(state))
def _has_piercing_book(state: State) -> bool:
    return any(bare(s.get("name")) == "enchanted_book" and (_stack_ench_level(s, "piercing") >= 1 or not s.get("enchantments"))
               for s in _inv_stacks(state))

def _obs_call(fn, *args, **kwargs):
    try: res = fn(*args, **kwargs)
    except ApiError as exc:
        if exc.code in ("not_ready", "not_loaded"): raise Replan("世界加载尚未就绪（not_ready）") from exc
        raise
    if isinstance(res, Mapping) and res.get("ready") is False: raise Replan("世界加载尚未就绪（ready=false）")
    return res

def _get_entities(ex: Executor, state: State, radius: int = 16, types: Sequence[str] | None = None) -> list[dict]:
    res = _obs_call(ex.client.get_entities, radius=radius)
    raw = res.get("entities") if isinstance(res, Mapping) else res
    wanted = {bare(t) for t in types} if types else None
    return [dict(e) for e in (raw or ()) if isinstance(e, Mapping) and type(e.get("id")) is int and e.get("pos")
            and (not wanted or bare(e.get("name")) in wanted or bare(e.get("type")) in wanted)]

def _find_blocks(ex: Executor, types: Sequence[str], radius: int = 16, limit: int = 8) -> list[dict]:
    res = _obs_call(ex.client.find_blocks, types=[bare(t) for t in types], radius=radius, limit=limit)
    raw = res.get("blocks") if isinstance(res, Mapping) else res
    if isinstance(raw, Mapping):
        return [{"name": bare(k), "pos": list(block_cell(v["nearest"])), "dist": v.get("dist")}
                for k, v in raw.items() if isinstance(v, Mapping) and v.get("nearest")]
    return [{"name": bare(b.get("name")), "pos": list(block_cell(b["pos"])), **{k: v for k, v in b.items() if k not in ("name", "pos")}}
            for b in (raw or ()) if isinstance(b, Mapping) and b.get("pos")]

def _place_cell(ex: Executor, state: State, item: str, cell: tuple[int, int, int]) -> Generator[dict, None, None]:
    r = ex.place(item, list(cell)); voxel_cache_for(state).invalidate(cell, dimension=_dim(state)); yield rec("place", r, item=item, pos=list(cell))
def _dig_cell(ex: Executor, state: State, cell: tuple[int, int, int]) -> Generator[dict, None, None]:
    r = ex.dig(list(cell), collect=True); voxel_cache_for(state).invalidate(cell, dimension=_dim(state)); yield rec("dig", r, pos=list(cell))
def _equip_hand(ex: Executor, state: State, item: str) -> Generator[dict, None, None]:
    held = (state.inventory or {}).get("held") or (state.st or {}).get("held") or {}
    if bare(held.get("name")) != bare(item): yield rec("equip", ex.equip(bare(item), "hand"), item=bare(item), slot="hand")
def _safe_look(ex: Executor, **kwargs: Any) -> None:
    try: ex.look(**kwargs)
    except (ApiError, Replan) as exc:
        if failure(exc)[0] != "unsupported_api": raise
def _is_standable(ex: Executor, c: tuple[int, int, int]) -> bool:
    return (is_safe_solid_floor(observe_block(ex, [c[0], c[1] - 1, c[2]]))
            and is_clear_space(observe_block(ex, list(c))) and is_clear_space(observe_block(ex, [c[0], c[1] + 1, c[2]])))
def _overlaps(cell: tuple[int, int, int], st: dict) -> bool:
    px, py, pz = block_cell(feet_pos(st)); return cell[0] == px and cell[2] == pz and abs(cell[1] - py) <= 1
def _approach_block(ex: Executor, state: State, cell: tuple[int, int, int], *, reach: float = STATION_EYE_REACH) -> Generator[dict, None, dict]:
    st = state.refresh_status(ex.client, max_age_s=0)
    if can_reach_block(ex, st, cell, reach=reach) and not _overlaps(cell, st): return st
    tx, ty, tz = cell
    cands = [(tx + dx, ty + dy, tz + dz) for dy in (0, -1, 1, -2, 2) for dx in range(-3, 4) for dz in range(-3, 4)
             if not (dx == 0 and dz == 0 and abs(dy) <= 1) and _is_standable(ex, (tx + dx, ty + dy, tz + dz))
             and can_reach_block(ex, [tx + dx + 0.5, float(ty + dy), tz + dz + 0.5], cell, reach=reach)]
    if not cands: raise Replan(f"目标方块 {list(cell)} 附近无实心承重站位，防坠落中止接近")
    best = min(cands, key=lambda c: math.dist((c[0] + 0.5, float(c[1]), c[2] + 0.5), feet_pos(st)))
    yield rec("move_to", ex.move_to(pos=(dest := [best[0] + 0.5, float(best[1]), best[2] + 0.5]), rng=1), pos=dest)
    return state.refresh_status(ex.client, max_age_s=0)
def _approach_ent(ex: Executor, state: State, ent: Mapping[str, Any], *, reach: float = ENTITY_EYE_REACH) -> Generator[dict, None, dict]:
    st = state.refresh_status(ex.client, max_age_s=0)
    if eye_to_point_dist(st, ent["pos"]) <= reach: return st
    yield rec("move_to", ex.move_to(pos=(dest := [float(v) for v in ent["pos"]]), rng=2), pos=dest); return state.refresh_status(ex.client, max_age_s=0)
def _nudge_entity_toward(ex: Executor, state: State, ent: Mapping[str, Any], target_pos: Sequence[float]) -> Generator[dict, None, None]:
    ep, tp = [float(v) for v in ent["pos"]], [float(v) for v in target_pos]
    dx, dz = tp[0] - ep[0], tp[2] - ep[2]; norm = math.hypot(dx, dz)
    ux, uz = (dx / norm, dz / norm) if norm > 1e-3 else (1.0, 0.0)
    behind = [ep[0] - ux * 1.1, ep[1], ep[2] - uz * 1.1]
    yield rec("move_to", ex.move_to(pos=behind, rng=0.8), pos=behind); _safe_look(ex, pos=tp)
    try: receipt = ex.control(forward=True, ms=450)
    except (ApiError, Replan) as exc:
        if (code := failure(exc)[0]) in _CTRL_FALLBACK:
            yield rec("control", error_code=code); yield rec("move_to", ex.move_to(pos=tp, rng=0.8), pos=tp); return
        raise
    yield rec("control", receipt, forward=True, ms=450)

class _T4Skill(Skill):
    verified = "offline"
    def _begin(self, ex: Executor, state: State) -> dict:
        refresh_inventory(ex, state); st = state.refresh_status(ex.client, max_age_s=0)
        if reason := self.blocked(state): raise Replan(reason)
        return st
    def run(self, ex: Executor, state: State):
        try: yield from self._run(ex, state)
        finally:
            if any(state.st.get(k) for k in ("sneaking", "blocking", "jumping")):
                _clear_posture(ex, state, sneak=False, shield=False, jump=False)
                state.st.update(sneaking=False, blocking=False, jumping=False)
    def _run(self, ex: Executor, state: State): raise NotImplementedError

@register
class TradeWorldHeight(_T4Skill):
    name, goal = "trade_world_height", "t4:trade_at_world_height"
    description = "借助高空下界门锚定或流浪商人在世界最高高度完成交易"
    params = {"prep_only": Param(bool, "仅搭建Y=314高空下界门与Y=319交易台前置", default=False)}
    @classmethod
    def blocked(cls, state: State) -> str:
        if not state.ready: return "世界尚未就绪（ready=false）"
        if state.st.get("alive") is False: return "角色当前未存活，无法执行高空交易"
        if not _has_inv(state): return "背包为空，缺少绿宝石、黑曜石或垫高建筑方块"
        if state.count("emerald") < 1 and state.count("obsidian") < 10:
            return "背包缺少绿宝石（>=1）或黑曜石（>=10），无法高空建门或交易"
        if (_dim(state) == "overworld" and _cur_y(state) < 310.0
                and not memo_for(state).has_landmark("high_altitude_portal", dimension="overworld")
                and total_placeable(state) < 16):
            return "主世界低空缺少足够垫高建筑方块（>=16）"
        return ""
    def _trade_at_altitude(self, ex: Executor, state: State, st: dict) -> Generator[dict, None, None]:
        merchants = _get_entities(ex, state, radius=16, types=_MERCHANTS)
        if not merchants: raise _env_blocked("高空平台未发现村民或流浪商人", "high_altitude_merchant", "半径 16 格")
        merchant = min(merchants, key=lambda e: eye_to_point_dist(st, e["pos"]))
        mp = [float(v) for v in merchant["pos"]]
        if mp[1] < _HIGH_TRADE_Y - 0.5:
            yield from _nudge_entity_toward(ex, state, merchant, [mp[0], float(_HIGH_TRADE_Y), mp[2]])
            st = state.refresh_status(ex.client, max_age_s=0)
        if _cur_y(state) < float(_HIGH_TRADE_Y):
            dest = [mp[0], float(_HIGH_TRADE_Y), mp[2]]
            yield rec("move_to", ex.move_to(pos=dest, rng=1), pos=dest); st = state.refresh_status(ex.client, max_age_s=0)
        if eye_to_point_dist(st, mp) > ENTITY_EYE_REACH: yield from _approach_ent(ex, state, merchant, reach=ENTITY_EYE_REACH)
        yield from trade_once(ex, state, merchant["id"]); self._traded = True
    def _build_high_portal_and_platform(self, ex: Executor, state: State, st: dict) -> Generator[dict, None, None]:
        px, py, pz = block_cell(feet_pos(st)); sneaking = False
        try:
            if py < _HIGH_PORTAL_Y:
                refresh_inventory(ex, state)
                if total_placeable(state) < (_HIGH_PORTAL_Y - py): raise Replan(f"垫高至 Y={_HIGH_PORTAL_Y} 途中可放置方块不足（当前 Y={py}）")
                pillar = [[px + (1 if (y - py) % 2 == 0 else 0), y, pz] for y in range(py, _HIGH_PORTAL_Y)]
                yield from step_up_1x2(ex, state, pillar)
                top = [pillar[-1][0] + 0.5, float(pillar[-1][1] + 1), pillar[-1][2] + 0.5]
                yield rec("move_to", ex.move_to(pos=top, rng=1), pos=top); st = state.refresh_status(ex.client, max_age_s=0)
            ex.posture(sneak=True); state.st["sneaking"] = True; sneaking = True; refresh_inventory(ex, state)
            if state.count("obsidian") < 10 or state.count("flint_and_steel") < 1:
                raise Replan("高空建门缺少 10 黑曜石或打火石（flint_and_steel）")
            for cell in (*((px + u, _HIGH_PORTAL_Y - 1, pz + 1) for u in range(4)), (px, _HIGH_PORTAL_Y, pz + 1), (px + 3, _HIGH_PORTAL_Y, pz + 1)):
                cur_st = state.refresh_status(ex.client, max_age_s=0)
                if bare(observe_block(ex, list(cell)).get("name")) in AIR and not _overlaps(cell, cur_st):
                    if blk := pick_placeable(state): yield from _place_cell(ex, state, blk, cell); refresh_inventory(ex, state)
            for u, v in _PORTAL_RING:
                cell = (px + u, _HIGH_PORTAL_Y + v, pz + 1)
                if bare(observe_block(ex, list(cell)).get("name")) != "obsidian":
                    cur_st = state.refresh_status(ex.client, max_age_s=0)
                    if not can_reach_block(ex, cur_st, cell, reach=DIG_EYE_REACH) or _overlaps(cell, cur_st):
                        yield from _approach_block(ex, state, cell, reach=DIG_EYE_REACH)
                    yield from _place_cell(ex, state, "obsidian", cell)
            ignite = [px + 1, _HIGH_PORTAL_Y, pz + 1]
            yield rec("interact", ex.interact(pos=ignite, item="flint_and_steel"), pos=ignite, item="flint_and_steel")
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    fcell = (px + dx, _HIGH_TRADE_Y - 1, pz + dz)
                    cur_st = state.refresh_status(ex.client, max_age_s=0)
                    if bare(observe_block(ex, list(fcell)).get("name")) in AIR and not _overlaps(fcell, cur_st):
                        refresh_inventory(ex, state); yield from _place_cell(ex, state, pick_placeable(state), fcell)
            memo_for(state).record_landmark("high_altitude_portal", (px, _HIGH_PORTAL_Y, pz + 1), dimension="overworld", meta={"platform_y": _HIGH_TRADE_Y})
        finally:
            if sneaking: _clear_posture(ex, state, sneak=False); state.st["sneaking"] = False
    def _run(self, ex: Executor, state: State):
        self._traded, self._prepped = False, False; st, dim, cy = self._begin(ex, state), _dim(state), _cur_y(state)
        if dim == "overworld" and cy >= float(_HIGH_PORTAL_Y):
            if self.args["prep_only"]:
                if not memo_for(state).has_landmark("high_altitude_portal", dimension="overworld"):
                    yield from self._build_high_portal_and_platform(ex, state, st)
                self._prepped = True; return
            yield from self._trade_at_altitude(ex, state, st); return
        if dim in ("the_nether", "nether"):
            portals = _find_blocks(ex, ["nether_portal"], radius=32, limit=8)
            if not portals: raise _env_blocked("下界附近未发现下界传送门", "nether_portal", "半径 32 格")
            vills = _get_entities(ex, state, radius=32, types=("villager",))
            if not vills: raise _env_blocked("下界传送门附近未发现待推回的村民", "nether_villager", "半径 32 格")
            pcell = block_cell(min(portals, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
            yield from _nudge_entity_toward(ex, state, min(vills, key=lambda e: eye_to_point_dist(st, e["pos"])), block_center(pcell))
            pstand = [pcell[0] + 0.5, float(pcell[1]), pcell[2] + 0.5]
            yield rec("move_to", ex.move_to(pos=pstand, rng=0.5), pos=pstand); st = state.refresh_status(ex.client, max_age_s=0)
            if self.args["prep_only"]: self._prepped = True; return
            if _dim(state) == "overworld" and _cur_y(state) >= float(_HIGH_PORTAL_Y):
                yield from self._trade_at_altitude(ex, state, st); return
            raise Replan("已穿过下界门，等待主世界 Y>=314 高空落点就绪后交易")
        if not memo_for(state).has_landmark("high_altitude_portal", dimension="overworld"):
            vills, portals = _get_entities(ex, state, radius=32, types=("villager",)), _find_blocks(ex, ["nether_portal"], radius=32, limit=8)
            if vills and portals:
                pcell = block_cell(min(portals, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
                yield from _nudge_entity_toward(ex, state, min(vills, key=lambda e: eye_to_point_dist(st, e["pos"])), block_center(pcell))
                if obs := _find_blocks(ex, ["obsidian"], radius=16, limit=8):
                    ocell = block_cell(min(obs, key=lambda b: math.dist(block_center(b["pos"]), block_center(pcell)))["pos"])
                    yield from _approach_block(ex, state, ocell, reach=DIG_EYE_REACH)
                    if pick := best_pickaxe(state): yield from _equip_hand(ex, state, pick)
                    yield from _dig_cell(ex, state, ocell); st = state.refresh_status(ex.client, max_age_s=0)
            yield from self._build_high_portal_and_platform(ex, state, st)
        if self.args["prep_only"]: self._prepped = True; return
        if _cur_y(state) >= float(_HIGH_PORTAL_Y):
            yield from self._trade_at_altitude(ex, state, state.refresh_status(ex.client, max_age_s=0)); return
        raise Replan("高空下界门与Y=319平台已就绪，请从下界推回村民后再在世界最高高度交易")
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        if self.args["prep_only"]:
            ok = getattr(self, "_prepped", False) or memo_for(state).has_landmark("high_altitude_portal", dimension="overworld")
            return "" if ok else "高空下界门与Y=319平台尚未建成"
        return "" if getattr(self, "_traded", False) else "尚未在世界最高高度完成交易"

@register
class ArbalisticTrenchShot(_T4Skill):
    name, goal = "arbalistic_trench_shot", "t4:arbalistic"
    description = "备齐穿透IV弩与5种唯一生物并在1×1×5浅槽轴向一箭穿五"
    params = {"prep_only": Param(bool, "仅备齐穿透IV弩或5种唯一生物包不射击", default=False),
              "prep_mode": Param(str, "准备模式", default="all", choices=("all", "crossbow", "mobs"))}
    @classmethod
    def blocked(cls, state: State) -> str:
        if not state.ready: return "世界尚未就绪（ready=false）"
        if state.st.get("alive") is False: return "角色当前未存活，无法执行一箭穿五"
        if not _has_inv(state): return "背包为空，缺少穿透IV弩、附魔前置或水生生物桶"
        has_prep = any(state.count(k) >= 1 for k in ("crossbow", "emerald", "enchanted_book", *_ANVILS))
        has_mobs = any(state.count(b) >= 1 for b in _AQUATIC_BUCKETS) or state.count("water_bucket") >= 1
        if not (_has_valid_piercing_iv_crossbow(state) or has_prep or has_mobs):
            return "缺少穿透IV弩、弩附魔材料（crossbow/emerald/enchanted_book/anvil）及水生生物桶"
        return ""
    def _prep_crossbow(self, ex: Executor, state: State, st: dict) -> Generator[dict, None, None]:
        if _has_valid_piercing_iv_crossbow(state): return
        if state.count("crossbow") >= 1 and state.count("emerald") >= 10 and not _has_piercing_book(state):
            if vills := _get_entities(ex, state, radius=32, types=("villager",)):
                vill = min(vills, key=lambda e: eye_to_point_dist(st, e["pos"]))
                yield from _approach_ent(ex, state, vill, reach=ENTITY_EYE_REACH)
                yield from trade_once(ex, state, vill["id"], preferred_items=("enchanted_book",)); refresh_inventory(ex, state)
        if not _has_valid_piercing_iv_crossbow(state) and state.count("crossbow") >= 1 and _has_piercing_book(state):
            if anvils := _find_blocks(ex, _ANVILS, radius=16, limit=4):
                acell = block_cell(min(anvils, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
                yield from _approach_block(ex, state, acell, reach=STATION_EYE_REACH); target = {"pos": list(acell)}
                ops = [{"op": "put", "item": "crossbow", "count": 1, "to": "left"},
                       {"op": "put", "item": "enchanted_book", "count": 1, "to": "right", "enchanted": True},
                       {"op": "take", "from": "output"}]
                yield rec("window_act", ex.window_act(target, ops), target=target); refresh_inventory(ex, state)
        if not _has_valid_piercing_iv_crossbow(state):
            if _has_multishot_crossbow(state): raise Replan("当前弩带有多重射击附魔，禁用多重射击，需纯穿透IV弩")
            raise Replan("尚未备齐穿透IV（且无多重射击）的弩")
    def _prep_mobs(self, ex: Executor, state: State, st: dict) -> Generator[dict, None, list[dict]]:
        ents = _get_entities(ex, state, radius=16)
        if mob_collection_ready(state, 5, entities=ents): return ents
        if state.count("water_bucket") >= 1:
            targets = [e for e in ents if bare(e.get("name")) in _AQUATIC_MOBS and state.count(f"{bare(e.get('name'))}_bucket") == 0]
            for mob in sorted(targets, key=lambda e: eye_to_point_dist(st, e["pos"]))[: state.count("water_bucket")]:
                yield from _approach_ent(ex, state, mob, reach=ENTITY_EYE_REACH)
                yield rec("interact", ex.interact(entity_id=mob["id"], item="water_bucket"), entity_id=mob["id"], item="water_bucket")
                refresh_inventory(ex, state); ents = _get_entities(ex, state, radius=16)
                if mob_collection_ready(state, 5, entities=ents): return ents
        if not mob_collection_ready(state, 5, entities=ents):
            raise _env_blocked("5种唯一低血量生物尚未集齐", "mob_collection_5", "半径 16 格")
        return ents
    def _run(self, ex: Executor, state: State):
        self._shot, self._prepped = False, False
        st, prep_only, mode = self._begin(ex, state), self.args["prep_only"], self.args["prep_mode"]
        if prep_only:
            if mode in ("crossbow", "all"): yield from self._prep_crossbow(ex, state, st); st = state.refresh_status(ex.client, max_age_s=0)
            if mode in ("mobs", "all"): yield from self._prep_mobs(ex, state, st)
            self._prepped = True; return
        if not _has_valid_piercing_iv_crossbow(state):
            if _has_multishot_crossbow(state): raise Replan("当前弩带有多重射击附魔，禁用多重射击，需纯穿透IV弩")
            raise Replan("缺少有效的穿透IV（且无多重射击）的弩")
        if state.count("arrow") < 1: raise Replan("背包缺少箭矢（arrow>=1），无法发射穿透IV弩")
        ents = _get_entities(ex, state, radius=16)
        if not mob_collection_ready(state, 5, entities=ents):
            raise _env_blocked("5种唯一低血量生物尚未集齐", "mob_collection_5", "半径 16 格")
        fx, fy, fz = block_cell(feet_pos(st)); tx, ty, tz = fx + 2, fy, fz
        for i in range(5):
            cell = (tx + i, ty - 1, tz)
            if (bn := bare(observe_block(ex, list(cell)).get("name"))) and bn not in (AIR | {"water", "flowing_water"}):
                if not can_reach_block(ex, st, cell, reach=DIG_EYE_REACH): st = yield from _approach_block(ex, state, cell, reach=DIG_EYE_REACH)
                yield from _dig_cell(ex, state, cell); st = state.refresh_status(ex.client, max_age_s=0)
        if bare(observe_block(ex, [tx, ty - 1, tz]).get("name")) in AIR and state.count("water_bucket") >= 1:
            if not can_reach_block(ex, st, (tx, ty - 1, tz), reach=DIG_EYE_REACH): st = yield from _approach_block(ex, state, (tx, ty - 1, tz), reach=DIG_EYE_REACH)
            yield rec("interact", ex.interact(pos=[tx, ty - 1, tz], item="water_bucket"), pos=[tx, ty - 1, tz], item="water_bucket"); st = state.refresh_status(ex.client, max_age_s=0)
        buckets, tadpole_released = [b for b in _AQUATIC_BUCKETS if state.count(b) >= 1][:5], False
        for i, bucket in enumerate(buckets):
            cell = (tx + i, ty - 1, tz)
            if not can_reach_block(ex, st, cell, reach=DIG_EYE_REACH): st = yield from _approach_block(ex, state, cell, reach=DIG_EYE_REACH)
            yield rec("interact", ex.interact(pos=list(cell), item=bucket), pos=list(cell), item=bucket)
            voxel_cache_for(state).invalidate(cell, dimension=_dim(state))
            if bucket == "tadpole_bucket": tadpole_released = True
            st = state.refresh_status(ex.client, max_age_s=0)
        if len(buckets) < 5 and (chickens := _get_entities(ex, state, radius=16, types=("chicken",))):
            yield from _nudge_entity_toward(ex, state, min(chickens, key=lambda e: eye_to_point_dist(st, e["pos"])), [tx + len(buckets) + 0.5, float(ty), tz + 0.5])
            st = state.refresh_status(ex.client, max_age_s=0)
        if tadpole_released:
            refresh_inventory(ex, state)
            if tads := _get_entities(ex, state, radius=12, types=("tadpole",)):
                tad = min(tads, key=lambda e: eye_to_point_dist(st, e["pos"]))
                held = (state.inventory or {}).get("held") or (state.st or {}).get("held") or {}
                if _is_weapon(bare(held.get("name"))):
                    if safe := next((bare(s.get("name")) for s in _inv_stacks(state) if not _is_weapon(bare(s.get("name")))), None): yield from _equip_hand(ex, state, safe)
                if eye_to_point_dist(st, tad["pos"]) > ENTITY_EYE_REACH: st = yield from _approach_ent(ex, state, tad, reach=ENTITY_EYE_REACH)
                yield rec("attack", ex.attack(entity_id=tad["id"], max_hits=1), entity_id=tad["id"], max_hits=1)
        pit, fire_y = (tx - 2, ty - 1, tz), float(ty)
        if is_safe_solid_floor(observe_block(ex, [pit[0], ty - 2, pit[2]])):
            if bare(observe_block(ex, list(pit)).get("name")) not in AIR:
                if not can_reach_block(ex, st, pit, reach=DIG_EYE_REACH): st = yield from _approach_block(ex, state, pit, reach=DIG_EYE_REACH)
                yield from _dig_cell(ex, state, pit)
            fire_y = float(ty - 1)
        fire_pos, aim_pos = [tx - 2 + 0.5, fire_y, tz + 0.5], [tx + 4.5, ty - 0.5, tz + 0.5]
        if math.dist(feet_pos(state.refresh_status(ex.client, max_age_s=0)), fire_pos) > 0.6:
            yield rec("move_to", ex.move_to(pos=fire_pos, rng=0.5), pos=fire_pos)
        yield from _equip_hand(ex, state, "crossbow"); _safe_look(ex, pos=aim_pos)
        yield rec("shoot", ex.shoot(pos=aim_pos, weapon="crossbow"), pos=aim_pos, weapon="crossbow")
        refresh_inventory(ex, state); self._shot = True
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        if self.args["prep_only"]:
            m = self.args["prep_mode"]
            if m == "crossbow": return "" if (getattr(self, "_prepped", False) or _has_valid_piercing_iv_crossbow(state)) else "尚未备齐穿透IV（且无多重射击）的弩"
            if m == "mobs": return "" if (getattr(self, "_prepped", False) or mob_collection_ready(state, 5)) else "尚未集齐5种唯一低血量生物包"
            return "" if (getattr(self, "_prepped", False) or (_has_valid_piercing_iv_crossbow(state) and mob_collection_ready(state, 5))) else "尚未备齐穿透IV弩与5种唯一低血量生物包"
        return "" if getattr(self, "_shot", False) else "尚未完成轴向穿透IV一箭穿五射击"
