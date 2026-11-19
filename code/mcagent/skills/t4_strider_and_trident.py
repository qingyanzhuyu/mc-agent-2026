"""Stage 3 T4 炽足兽双项与被动机会三叉戟投掷技能（3 项 L3 注册技能）。"""
from __future__ import annotations

import math, sys
from collections.abc import Generator, Iterable, Mapping, Sequence
from typing import Any, Optional

from ..client import ApiError
from ..runtime import EnvBlocked, Executor, Replan, State
from .base import Param, Skill, failure, rec
from .interaction import _clear_posture, use_item_on
from .items import AIR, bare, is_clear_space, is_safe_solid_floor, pick_placeable, total_placeable
from .observations import observe_block, refresh_inventory
from .registry import register
from .sightline import (DIG_EYE_REACH, ENTITY_EYE_REACH, STATION_EYE_REACH, block_cell,
                        can_reach_block, eye_to_block_dist, eye_to_point_dist, feet_pos)
from .t2_trials_oriented import _place_cell
from .world_memo import memo_for

_CARDINALS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_B13_FALLBACK_CODES = frozenset({"cannot_use", "unsupported", "unsupported_api", "bad_args"})
_LIQUIDS = frozenset({"water", "flowing_water", "lava", "flowing_lava", "bubble_column"})
_SAFE_TRIDENT_TARGETS = (
    "armor_stand", "cow", "sheep", "pig", "chicken", "rabbit", "squid", "glow_squid",
    "cod", "salmon", "horse", "donkey", "mule", "llama", "goat", "frog", "turtle", "camel", "armadillo",
)
_SAFE_TRIDENT_SET = frozenset(_SAFE_TRIDENT_TARGETS)
_B13_UNSUPPORTED_MSG = "api_b13_unsupported: 诡异菌钓竿未触发耐久 -1，当前端点不支持 B13 加速"

def _dim(state: State) -> str:
    raw = bare((state.st or {}).get("dimension")).strip().lower()
    return "the_nether" if raw in ("the_nether", "nether") else ("the_end" if raw in ("the_end", "end") else (raw or "overworld"))
def _inv_stacks(state: State, *, include_armor: bool = False) -> list[Mapping[str, Any]]:
    inv = state.inventory or {}
    armor = inv.get("armor") if include_armor and isinstance(inv.get("armor"), Mapping) else {}
    return [r for r in (inv.get("held"), inv.get("offhand"), *(inv.get("items") or ()), *armor.values())
            if isinstance(r, Mapping) and bare(r.get("name")) and (r.get("count", 1) or 0) >= 1]
def _gate_base(state: State, empty_msg: str) -> str:
    if not state.ready: return "世界尚未就绪（ready=false）"
    if (state.st or {}).get("alive") is False: return "角色当前未存活，无法执行炽足兽或三叉戟技能"
    return "" if _inv_stacks(state, include_armor=True) else empty_msg
def _in_strider(state: State) -> bool:
    veh = (state.st or {}).get("in_vehicle")
    return bare(veh.get("name") or veh.get("type")).lower() == "strider" if isinstance(veh, Mapping) else (isinstance(veh, str) and bare(veh).lower() == "strider")
def _obs_call(fn, *args, **kwargs):
    try: res = fn(*args, **kwargs)
    except ApiError as exc:
        if exc.code in ("not_ready", "not_loaded"): raise Replan("世界加载尚未就绪（not_ready）") from exc
        raise
    if isinstance(res, Mapping) and res.get("ready") is False: raise Replan("世界加载尚未就绪（ready=false）")
    return res
def _find_blocks(ex: Executor, types: Sequence[str], *, radius: int = 48, limit: int = 24) -> list[dict]:
    res = _obs_call(ex.client.find_blocks, [bare(t) for t in types], radius=radius, limit=limit)
    raw = res.get("blocks") if isinstance(res, Mapping) else res
    if isinstance(raw, Mapping):
        return [{"name": bare(k), "pos": list(block_cell(v["nearest"])), "dist": v.get("dist")} for k, v in raw.items() if isinstance(v, Mapping) and v.get("nearest")]
    return [{**b, "name": bare(b.get("name")), "pos": list(block_cell(b["pos"]))} for b in (raw or ()) if isinstance(b, Mapping) and b.get("pos")]
def _get_entities(ex: Executor, *, radius: int = 48, types: Sequence[str] | None = None) -> list[dict]:
    wanted = [bare(t) for t in types] if types else None
    raw = _obs_call(ex.client.get_entities, radius=radius, types=wanted)
    ents, wset = (raw.get("entities") if isinstance(raw, Mapping) else raw), (set(wanted) if wanted else None)
    return [dict(e) for e in (ents or ()) if isinstance(e, Mapping) and type(e.get("id")) is int and e.get("pos")
            and (not wset or bare(e.get("name")) in wset or bare(e.get("type")) in wset)]
def _nearest_strider(ex: Executor, st: Mapping[str, Any], prefix: str = "附近") -> dict:
    striders = _get_entities(ex, radius=48, types=("strider",))
    if not striders: raise EnvBlocked(f"{prefix}未发现炽足兽", missing_predicate="visible_entity:strider", searched_volume="半径 48 格", wake_on="chunk_moved")
    return min(striders, key=lambda e: eye_to_point_dist(st, e["pos"]))
def _equip_slot(ex: Executor, state: State, item: str, slot: str = "hand") -> Generator[dict, None, None]:
    item, inv = bare(item), state.inventory or {}
    if bare(((inv.get("offhand") if slot == "off-hand" else inv.get("held")) or {}).get("name")) != item:
        yield rec("equip", ex.equip(item, slot), item=item, slot=slot); refresh_inventory(ex, state)
def _reset_posture(ex: Executor, state: State) -> None:
    if any((state.st or {}).get(k) for k in ("sneaking", "blocking", "jumping")):
        _clear_posture(ex, state, sneak=False, shield=False, jump=False)
        state.st.update(sneaking=False, blocking=False, jumping=False)
def _safe_dismount_cleanup(ex: Executor, state: State, riding: bool) -> None:
    if riding and (state.st or {}).get("alive") is not False:
        try:
            if state.refresh_status(ex.client, max_age_s=0).get("in_vehicle"): ex.dismount()
        except (ApiError, Replan):
            if sys.exc_info()[0] is None: raise
def _item_durability(state: State, item: str) -> Optional[int]:
    target = bare(item)
    for stack in _inv_stacks(state, include_armor=False):
        if bare(stack.get("name")) != target: continue
        for src in (stack, *(stack[k] for k in ("components", "nbt", "metadata") if isinstance(stack.get(k), Mapping))):
            dur = src.get("durability")
            if isinstance(dur, Sequence) and not isinstance(dur, (str, bytes)) and dur and type(dur[0]) is int: return int(dur[0])
            if type(dur) is int: return int(dur)
            if type(dmg := src.get("damage")) is int: return int(src.get("max_durability") or src.get("maxDamage") or 100) - int(dmg)
    return None
def _has_adv(ex: Executor, adv_id: str) -> bool:
    if not callable(fn := getattr(ex.client, "advancements", None)): return False
    try: res = fn()
    except Exception: return False
    done = res.get("completed") if isinstance(res, Mapping) else None
    return isinstance(done, Iterable) and not isinstance(done, (str, bytes)) and (adv_id in done or bare(adv_id) in {bare(x) for x in done})
def _boost_confirmed(ex: Executor, state: State, before_dur: Optional[int], before_cnt: int, before_adv: bool, receipt: Any = None) -> bool:
    after_dur = _item_durability(state, "warped_fungus_on_a_stick")
    if (before_dur is not None and after_dur is not None and after_dur < before_dur) or state.count("warped_fungus_on_a_stick") < before_cnt: return True
    if not before_adv and _has_adv(ex, "minecraft:nether/ride_strider"): return True
    if not isinstance(receipt, Mapping): return False
    if receipt.get("boosted") is True or receipt.get("durability_changed") is True: return True
    if type(receipt.get("durability_delta")) is int and receipt["durability_delta"] < 0: return True
    return isinstance(used := receipt.get("used"), Mapping) and int(used.get("warped_fungus_on_a_stick") or 0) > 0
def _stack_ench_level(stack: Mapping[str, Any], ench_id: str) -> int:
    target = bare(ench_id).lower()
    comp = stack.get("components") if isinstance(stack.get("components"), Mapping) else {}
    for raw in (stack.get("enchantments"), comp.get("minecraft:enchantments")):
        if isinstance(raw, Mapping):
            if (lvl := next((v for k, v in raw.items() if bare(k).lower() == target and type(v) is int), 0)): return lvl
        elif isinstance(raw, Iterable) and not isinstance(raw, (str, bytes)):
            for row in raw:
                if isinstance(row, str) and bare(row).lower() == target: return 1
                if isinstance(row, Mapping) and bare(row.get("name") or row.get("id")).lower() == target:
                    if type(lvl := (row.get("level") if row.get("level") is not None else row.get("lvl", 1))) is int: return lvl
    return 0
def _has_throwable_trident(state: State) -> tuple[bool, bool]:
    tridents = [s for s in _inv_stacks(state, include_armor=False) if bare(s.get("name")) == "trident"]
    return bool(tridents), any(_stack_ench_level(s, "riptide") == 0 for s in tridents)
def _trident_has_loyalty(state: State) -> bool:
    return any(bare(s.get("name")) == "trident" and _stack_ench_level(s, "riptide") == 0 and _stack_ench_level(s, "loyalty") >= 1
               for s in _inv_stacks(state, include_armor=False))
def _is_lava_source(blk: Mapping[str, Any]) -> bool:
    return bare(blk.get("name")) == "lava" and (blk.get("properties") or {}).get("level") in (None, 0, "0")
def _has_rain_cover(ex: Executor, cell: tuple[int, int, int]) -> bool:
    cx, cy, cz = cell
    return any((n := bare(observe_block(ex, [cx, cy + dy, cz]).get("name"))) and n not in AIR and n not in _LIQUIDS
               for dy in range(2, 16 if cy <= -50 else 7))
def _safe_shore_near(ex: Executor, target: Sequence[float], origin: Sequence[float]) -> Optional[tuple[int, int, int]]:
    tx, ty, tz = block_cell(target)
    cands = [c for dy in (0, 1, -1, 2) for dx in range(-4, 5) for dz in range(-4, 5)
             if is_safe_solid_floor(observe_block(ex, [(c := (tx + dx, ty + dy, tz + dz))[0], c[1] - 1, c[2]]))
             and is_clear_space(observe_block(ex, list(c))) and is_clear_space(observe_block(ex, [c[0], c[1] + 1, c[2]]))]
    return min(cands, key=lambda c: (math.dist((c[0] + 0.5, float(c[1]), c[2] + 0.5), target),
                                     math.dist((c[0] + 0.5, float(c[1]), c[2] + 0.5), origin))) if cands else None
def _lure_or_approach_strider(ex: Executor, state: State, st: dict, ent: dict) -> Generator[dict, None, dict]:
    if eye_to_point_dist(st, ent["pos"]) <= ENTITY_EYE_REACH: return ent
    lure = "warped_fungus" if state.count("warped_fungus") >= 1 else "warped_fungus_on_a_stick"
    if state.count(lure) >= 1: yield from _equip_slot(ex, state, lure, "hand")
    sid = ent["id"]
    for _ in range(2):
        st = state.refresh_status(ex.client, max_age_s=0)
        if eye_to_point_dist(st, ent["pos"]) <= ENTITY_EYE_REACH: return ent
        if (shore := _safe_shore_near(ex, ent["pos"], feet_pos(st))) is None:
            raise Replan("炽足兽周边缺少安全实体岸边，禁止涉入岩浆接近")
        dest = [shore[0] + 0.5, float(shore[1]), shore[2] + 0.5]
        yield rec("move_to", ex.move_to(pos=dest, rng=1), pos=dest)
        refreshed, st = _get_entities(ex, radius=48, types=("strider",)), state.refresh_status(ex.client, max_age_s=0)
        ent = next((e for e in refreshed if e["id"] == sid), None) or (min(refreshed, key=lambda e: eye_to_point_dist(st, e["pos"])) if refreshed else None)
        if ent is None: raise Replan("岸边引诱接近后未再观察到炽足兽")
    return ent
def _mount_strider(ex: Executor, state: State, st: dict, strider: dict) -> Generator[dict, None, dict]:
    dim, sid = _dim(state), strider["id"]
    saddled = bool(strider.get("saddled")) or memo_for(state).has_landmark("saddled_strider", dimension=dim)
    if not saddled and state.count("saddle") >= 1:
        yield from use_item_on(ex, state, sid, "saddle")
        memo_for(state).record_landmark("saddled_strider", block_cell(strider["pos"]), dimension=dim)
    ev = yield from use_item_on(ex, state, sid)
    after_st = ev["after"]["status"]
    if not (bool(after_st.get("in_vehicle")) or ev["receipt"].get("mounted") is True or ev["receipt"].get("changed") is True):
        raise Replan("右键炽足兽后未确认进入骑乘状态")
    return after_st
def _trigger_strider_boost(ex: Executor, state: State, st: dict, *, strict: bool = True,
                           steer_dir: tuple[int, int] = (1, 0)) -> Generator[dict, None, bool]:
    before_dur, before_cnt = _item_durability(state, "warped_fungus_on_a_stick"), state.count("warped_fungus_on_a_stick")
    before_adv = _has_adv(ex, "minecraft:nether/ride_strider")
    try: receipt = ex.use_item("warped_fungus_on_a_stick")
    except (ApiError, Replan) as exc:
        code, _ = failure(exc)
        if code not in _B13_FALLBACK_CODES: raise
        yield rec("use_item", item="warped_fungus_on_a_stick", error_code=code)
    else:
        yield rec("use_item", receipt, item="warped_fungus_on_a_stick"); refresh_inventory(ex, state)
        if _boost_confirmed(ex, state, before_dur, before_cnt, before_adv, receipt): return True
    fx, fy, fz = feet_pos(state.refresh_status(ex.client, max_age_s=0))
    dest = [fx + 4.0 * steer_dir[0], fy, fz + 4.0 * steer_dir[1]]
    try: receipt2 = ex.move_to(pos=dest, rng=1)
    except (ApiError, Replan) as exc:
        code, _ = failure(exc); refresh_inventory(ex, state)
        ok = _boost_confirmed(ex, state, before_dur, before_cnt, before_adv)
        if not ok and strict: raise Replan(_B13_UNSUPPORTED_MSG) from exc
        yield rec("move_to", pos=dest, error_code=code, b13_fallback=True); return ok
    yield rec("move_to", receipt2, pos=dest, b13_fallback=True); refresh_inventory(ex, state)
    if _boost_confirmed(ex, state, before_dur, before_cnt, before_adv, receipt2): return True
    if strict: raise Replan(_B13_UNSUPPORTED_MSG)
    return False
def _verify_overworld_lava_track(ex: Executor, state: State, st: dict, start_cell: tuple[int, int, int],
                                 dx: int, dz: int, length: int = 55, *, allow_repair: bool = False) -> Generator[dict, None, int]:
    sx, ly, sz = start_cell
    for i in range(length):
        cell = (sx + i * dx, ly, sz + i * dz)
        if not _is_lava_source(observe_block(ex, list(cell))):
            if not (allow_repair and state.count("lava_bucket") >= 1 and can_reach_block(ex, st, cell, reach=STATION_EYE_REACH)): return i
            yield from use_item_on(ex, state, cell, "lava_bucket")
            if not _is_lava_source(observe_block(ex, list(cell))): return i
        if not _has_rain_cover(ex, cell):
            roof = (cell[0], ly + 3, cell[2])
            if not (allow_repair and total_placeable(state) >= 1 and can_reach_block(ex, st, roof, reach=DIG_EYE_REACH)): return i
            yield from _place_cell(ex, state, pick_placeable(state), roof); refresh_inventory(ex, state)
            if not _has_rain_cover(ex, cell): return i
    return length
def _find_lava_start(ex: Executor, anchor: Sequence[float]) -> tuple[int, int, int]:
    ax, ay, az = block_cell(anchor)
    return next(((ax + ox, y, az + oz) for ox, oz in ((0, 0), *_CARDINALS) for y in (ay, ay - 1, ay + 1)
                 if _is_lava_source(observe_block(ex, [ax + ox, y, az + oz]))), (ax, ay, az))

class _StriderTridentSkill(Skill):
    verified = "offline"
    def _begin(self, ex: Executor, state: State) -> dict:
        refresh_inventory(ex, state); st = state.refresh_status(ex.client, max_age_s=0)
        if reason := self.blocked(state): raise Replan(reason)
        return st
    def run(self, ex: Executor, state: State):
        try: yield from self._run(ex, state)
        finally: _reset_posture(ex, state)
    def _run(self, ex: Executor, state: State): raise NotImplementedError

@register
class RideStriderBoost(_StriderTridentSkill):
    name, goal, description = "ride_strider_boost", "t4:ride_strider", "引诱炽足兽上鞍骑乘并以诡异菌钓竿双路径触发加速"
    params: dict[str, Param] = {}
    @classmethod
    def blocked(cls, state: State) -> str:
        if msg := _gate_base(state, "背包为空，缺少诡异菌钓竿与鞍"): return msg
        if state.count("warped_fungus_on_a_stick") < 1: return "背包缺少诡异菌钓竿（warped_fungus_on_a_stick）"
        ok = state.count("saddle") >= 1 or _in_strider(state) or memo_for(state).has_landmark("saddled_strider", dimension=_dim(state))
        return "" if ok else "缺少鞍（saddle）且未骑乘或记录已上鞍炽足兽"
    def _run(self, ex: Executor, state: State):
        self._boosted, st, dim = False, self._begin(ex, state), _dim(state)
        riding, strider_pos = _in_strider(state), block_cell(feet_pos(st))
        if not riding:
            strider = yield from _lure_or_approach_strider(ex, state, st, _nearest_strider(ex, st))
            strider_pos, st, riding = block_cell(strider["pos"]), (yield from _mount_strider(ex, state, st, strider)), True
        try:
            yield from _equip_slot(ex, state, "warped_fungus_on_a_stick", "hand")
            yield from _trigger_strider_boost(ex, state, st, strict=True)
            memo_for(state).record_landmark("saddled_strider", strider_pos, dimension=dim)
            yield rec("dismount", ex.dismount()); riding = False; self._boosted = True
        finally:
            _safe_dismount_cleanup(ex, state, riding)
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        return "" if getattr(self, "_boosted", False) else "尚未确认骑乘炽足兽并触发诡异菌钓竿加速"

@register
class RideStriderOverworld(_StriderTridentSkill):
    name, goal, description = "ride_strider_overworld", "t4:ride_strider_overworld", "带炽足兽入主世界并在带顶棚的直线全岩浆源通道骑行满50格"
    params: dict[str, Param] = {}
    @classmethod
    def blocked(cls, state: State) -> str:
        if msg := _gate_base(state, "背包为空，缺少诡异菌钓竿与炽足兽骑乘物资"): return msg
        if state.count("warped_fungus_on_a_stick") < 1: return "背包缺少诡异菌钓竿（warped_fungus_on_a_stick）"
        dim, memo = _dim(state), memo_for(state)
        if dim == "the_end": return "当前位于末地，无法执行主世界岩浆骑乘炽足兽"
        if not (state.count("saddle") >= 1 or _in_strider(state) or memo.has_landmark("saddled_strider", dimension=dim)
                or memo.has_landmark("saddled_strider", dimension="overworld")):
            return "缺少鞍（saddle）且未骑乘或记录已上鞍炽足兽"
        if dim == "the_nether" and not any(state.count(k) >= 1 for k in ("lead", "warped_fungus", "warped_fungus_on_a_stick")):
            return "下界转移炽足兽缺少拴绳或诡异菌诱饵"
        return ""
    def _run(self, ex: Executor, state: State):
        self._done, self._net_horiz_dist, st = False, 0.0, self._begin(ex, state)
        if _dim(state) == "the_nether":
            strider = _nearest_strider(ex, st, "下界附近")
            if not (portals := _find_blocks(ex, ["nether_portal"], radius=48, limit=12)):
                raise EnvBlocked("下界附近未发现下界传送门", missing_predicate="nether_portal", searched_volume="半径 48 格", wake_on="chunk_moved")
            strider = yield from _lure_or_approach_strider(ex, state, st, strider)
            if state.count("lead") >= 1: yield from use_item_on(ex, state, strider["id"], "lead")
            else: yield from _equip_slot(ex, state, "warped_fungus" if state.count("warped_fungus") >= 1 else "warped_fungus_on_a_stick", "hand")
            pcell = block_cell(min(portals, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
            ppos = [pcell[0] + 0.5, float(pcell[1]), pcell[2] + 0.5]
            yield rec("move_to", ex.move_to(pos=ppos, rng=1), pos=ppos); st = state.refresh_status(ex.client, max_age_s=0)
            if _dim(state) != "overworld": raise Replan("尚未将炽足兽带入主世界传送门")
        if _dim(state) != "overworld": raise Replan(f"当前维度 {_dim(state)} 非主世界，禁止执行主世界岩浆骑行")
        riding = _in_strider(state)
        strider = None if riding else _nearest_strider(ex, st, "主世界附近")
        start_cell, chosen = _find_lava_start(ex, feet_pos(st) if riding else strider["pos"]), None
        for repair in (False, True):
            for dx, dz in _CARDINALS:
                if (n_ok := (yield from _verify_overworld_lava_track(ex, state, st, start_cell, dx, dz, length=55, allow_repair=repair))) >= 52:
                    chosen = (dx, dz, n_ok); break
            if chosen is not None: break
        if chosen is None:
            raise EnvBlocked("主世界缺少带防雨顶棚的 ≥52 格直线全岩浆源通道（Y=-54 天然岩浆海或 1×55 沟槽）",
                             missing_predicate="overworld_lava_straight_track", searched_volume="主世界 4 轴向 55 格", wake_on="chunk_moved")
        if not riding:
            strider = yield from _lure_or_approach_strider(ex, state, st, strider)
            st, riding = (yield from _mount_strider(ex, state, st, strider)), True
        dx, dz, track_len = chosen
        try:
            yield from _equip_slot(ex, state, "warped_fungus_on_a_stick", "hand")
            sx, sy, sz = feet_pos(st := state.refresh_status(ex.client, max_age_s=0))
            yield from _trigger_strider_boost(ex, state, st, strict=False, steer_dir=(dx, dz))
            for dist_step in (15, 30, 45, min(55, max(52, track_len))):
                target = [sx + dist_step * dx, sy, sz + dist_step * dz]
                yield rec("move_to", ex.move_to(pos=target, rng=2), pos=target, segment_dist=dist_step)
                cur_x, _, cur_z = feet_pos(state.refresh_status(ex.client, max_age_s=0))
                self._net_horiz_dist = math.hypot(cur_x - sx, cur_z - sz)
                if self._net_horiz_dist >= 50.0: break
            if self._net_horiz_dist < 50.0:
                raise Replan(f"主世界岩浆骑行水平欧氏净位移仅 {self._net_horiz_dist:.1f} 格（需 >= 50.0 格，禁止折返）")
            yield rec("dismount", ex.dismount()); riding = False; self._done = True
        finally:
            _safe_dismount_cleanup(ex, state, riding)
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        return "" if (getattr(self, "_done", False) and getattr(self, "_net_horiz_dist", 0.0) >= 50.0) else "尚未确认主世界岩浆直线骑乘炽足兽达到 50 格水平欧氏净位移"

@register
class ThrowOpportunisticTrident(_StriderTridentSkill):
    name, goal, description = "throw_opportunistic_trident", "t4:throw_trident", "机会触发：持非激流三叉戟向安全被动生物或盔甲架投掷命中"
    params: dict[str, Param] = {}
    @classmethod
    def blocked(cls, state: State) -> str:
        if msg := _gate_base(state, "背包为空，未持有可投掷三叉戟"): return msg
        has_any, has_non_riptide = _has_throwable_trident(state)
        if not has_any: return "背包未持有三叉戟（被动机会触发，绝不主动刷溺尸）"
        return "" if has_non_riptide else "背包三叉戟均带有激流（riptide）附魔，禁止投掷冲刺"
    def _run(self, ex: Executor, state: State):
        self._hit_confirmed, st = False, self._begin(ex, state)
        cands = [e for e in _get_entities(ex, radius=32, types=_SAFE_TRIDENT_TARGETS)
                 if bare(e.get("name") or e.get("type")) in _SAFE_TRIDENT_SET and not e.get("tamed") and not e.get("owner")]
        if not cands:
            raise EnvBlocked("半径 32 格内未发现安全被动生物或盔甲架以投掷三叉戟",
                             missing_predicate="safe_passive_or_armor_stand", searched_volume="半径 32 格", wake_on="chunk_moved")
        tgt = min(cands, key=lambda e: (0 if bare(e.get("name") or e.get("type")) == "armor_stand" else 1, eye_to_point_dist(st, e["pos"])))
        tid, tname, tpos = tgt["id"], bare(tgt.get("name") or tgt.get("type")), [float(v) for v in tgt["pos"]]
        before_hp, has_loyalty, before_cnt = tgt.get("health"), _trident_has_loyalty(state), state.count("trident")
        yield from _equip_slot(ex, state, "trident", "hand")
        if eye_to_point_dist(st, tgt["pos"]) > 14.0: yield rec("move_to", ex.move_to(pos=tpos, rng=6), pos=tpos)
        try: ex.look(entity_id=tid)
        except (ApiError, Replan): pass
        receipt = ex.shoot(entity_id=tid, weapon="trident", charge_ms=1100); yield rec("shoot", receipt, entity_id=tid, weapon="trident")
        hit = isinstance(receipt, Mapping) and (receipt.get("hit") is True or receipt.get("killed") is True or bool(receipt.get("damaged")) or receipt.get("ok") is True)
        if not hit:
            after_ent = next((e for e in _get_entities(ex, radius=32, types=(tname,)) if e["id"] == tid), None)
            hit = after_ent is None or (isinstance(before_hp, (int, float)) and isinstance(after_ent.get("health"), (int, float)) and after_ent["health"] < before_hp)
        if not hit: raise Replan("投掷三叉戟未确认命中目标")
        refresh_inventory(ex, state)
        if not has_loyalty and state.count("trident") < before_cnt:
            try: yield rec("move_to", ex.move_to(pos=tpos, rng=1), pos=tpos, retrieve_trident=True)
            except (ApiError, Replan) as exc: yield rec("move_to", pos=tpos, error_code=failure(exc)[0], retrieve_trident=True)
            refresh_inventory(ex, state)
        self._hit_confirmed = True
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        return "" if getattr(self, "_hit_confirmed", False) else "尚未确认投掷三叉戟命中安全目标"
