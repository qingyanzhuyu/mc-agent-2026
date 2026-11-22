"""Stage 2 End Rush L2 搭桥原语与 4 项末地 L3 技能。"""
from __future__ import annotations

import math, time
from typing import Iterator, Optional, Sequence

from ..runtime import EnvBlocked, Replan
from . import nav
from .base import Param, Skill, failure, rec
from .field import scan_blocks, scan_entities
from .interaction import _clear_posture, use_item_on
from .items import AIR, bare, is_safe_solid_floor, total_placeable
from .nether_portal import Entry
from .nether_site import _center, _hazard, _neighbors
from .observations import observe_block, refresh_inventory
from .registry import register
from .sightline import STATION_EYE_REACH, block_cell, block_center, can_reach_block, can_see_block, eye_to_block_dist, feet_pos
from .world_memo import STRUCTURE_FINGERPRINTS, memo_for

_CARDINAL = ((1, 0), (-1, 0), (0, 1), (0, -1))
_NON_FLOOR = frozenset({"end_portal", "nether_portal", "end_gateway"})
_RESCUE_ITEMS = ("potion", "chorus_fruit", "ender_pearl", "water_bucket", "hay_block", "cobweb")
_END_SOLID = ("end_stone", "purpur_block", "purpur_pillar", "end_stone_bricks", "obsidian", "cobblestone")
_PORTAL_POLLS, _GATEWAY_POLLS, _LEVITATE_POLLS, _POLL_SLEEP_S = 12, 6, 24, 0.05

def _env_blocked(msg: str, pred: str, vol: str) -> EnvBlocked: return EnvBlocked(msg, missing_predicate=pred, searched_volume=vol, wake_on="chunk_moved")
def _dim(src) -> str:
    n = bare((src.get("dimension") if isinstance(src, dict) else (getattr(src, "st", None) or {}).get("dimension"))).strip().lower()
    return "the_end" if n in ("the_end", "end") else ("the_nether" if n in ("the_nether", "nether") else (n or "overworld"))
def _blk_name(ex, pos) -> str: return bare(observe_block(ex, list(block_cell(pos))).get("name"))
def _is_solid_floor(ex, pos) -> bool:
    blk = observe_block(ex, list(block_cell(pos)))
    return bool(name := bare(blk.get("name"))) and name not in _NON_FLOOR and is_safe_solid_floor(blk)
def _has_solid_footing(ex, st) -> bool:
    if st.get("ready") is False or _hazard(st) or st.get("on_ground") is not True: return False
    x, y, z = feet_pos(st); fy = math.floor(y - 0.01)
    cols = dict.fromkeys(((math.floor(x), math.floor(z)), *((math.floor(x + dx), math.floor(z + dz)) for dx in (-0.35, 0.35) for dz in (-0.35, 0.35))))
    return any(_is_solid_floor(ex, (cx, fy, cz)) for cx, cz in cols)
def _standable(ex, cell, *, allow_portal: bool = False) -> bool:
    x, y, z = block_cell(cell); allowed = AIR | {"end_portal"} if allow_portal else AIR
    if allow_portal and _blk_name(ex, (x, y, z)) == "end_portal": return _blk_name(ex, (x, y + 1, z)) in allowed
    return _is_solid_floor(ex, (x, y - 1, z)) and _blk_name(ex, (x, y, z)) in allowed and _blk_name(ex, (x, y + 1, z)) in allowed
def _move_safe(ex, state, dest, *, rng: int = 1, max_distance: int = 32):
    before = state.refresh_status(ex.client, max_age_s=0)
    if (d := math.dist(feet_pos(before), dest)) > max_distance: raise Replan(f"目标超出局部移动范围 {max_distance} 格")
    if d <= rng + 0.09 and _has_solid_footing(ex, before): return before
    if not _standable(ex, dest): raise Replan(f"位置 {dest} 的承重和两格净空尚未确认")
    rcpt = ex.move_to(pos=list(dest), rng=rng); yield rec("move_to", rcpt, pos=list(dest))
    after = state.refresh_status(ex.client, max_age_s=0)
    if rcpt.get("dimension_changed") or _dim(after) != _dim(before): raise Replan("局部移动发生维度变化，需复核落地安全")
    if math.dist(feet_pos(after), dest) > rng + 0.65 or not _has_solid_footing(ex, after): raise Replan(f"移动后未确认安全到达 {dest} 附近")
    return after
def _standable_near(ex, target_cell, origin_st, *, max_r: int = 3, max_eye: float = 4.0, need_los: bool = False):
    tx, ty, tz = block_cell(target_cell)
    valid = [s for dy in (0, -1, 1, 2) for dx in range(-max_r, max_r + 1) for dz in range(-max_r, max_r + 1)
             if not (dx == 0 and dz == 0 and dy in (0, -1))
             and eye_to_block_dist((s := [tx + dx + 0.5, float(ty + dy), tz + dz + 0.5]), (tx, ty, tz)) <= max_eye
             and _standable(ex, (tx + dx, ty + dy, tz + dz)) and (not need_los or can_see_block(ex, s, (tx, ty, tz)))]
    return min(valid, key=lambda s: eye_to_block_dist(origin_st, block_cell(s)), default=None)
def _approach_block(ex, state, target_cell, *, reach: float = STATION_EYE_REACH, max_distance: int = 32, need_los: bool = True, max_r: int = 3):
    st = state.refresh_status(ex.client, max_age_s=0)
    if can_reach_block(ex, st, target_cell, reach=reach, check_los=need_los): return st
    if (dest := _standable_near(ex, target_cell, st, max_r=max_r, max_eye=reach, need_los=need_los)) is None:
        raise _env_blocked("目标方块附近无安全可达站位", "standable_approach", f"目标半径 {max_r} 格")
    return (yield from _move_safe(ex, state, dest, rng=1, max_distance=max_distance))
def _scan_ready(ex, state, types, radius: int, *, limit: int = 12, entity: bool = False):
    scan = scan_entities(ex, state, types, radius=radius) if entity else scan_blocks(ex, state, types, radius=radius, limit=limit)
    if scan.get("ready") is False: raise Replan("世界加载尚未就绪")
    return scan["entities" if entity else "blocks"]
def _plan_gap_toward(ex, st, target_cell, *, max_len: int = 16):
    fx, fy, fz = block_cell(feet_pos(st)); tx, _, tz = block_cell(target_cell)
    cx, cz, edge, gaps = fx, fz, (fx, fy, fz), []
    for _ in range(max_len + 16):
        if (cx, cz) == (tx, tz): break
        dx, dz = tx - cx, tz - cz
        cx, cz = (cx + (1 if dx > 0 else -1), cz) if abs(dx) >= abs(dz) else (cx, cz + (1 if dz > 0 else -1))
        if _blk_name(ex, (cx, fy, cz)) not in AIR or _blk_name(ex, (cx, fy + 1, cz)) not in AIR: break
        if _is_solid_floor(ex, (cx, fy - 1, cz)):
            if gaps: break
            edge = (cx, fy, cz)
        elif _blk_name(ex, (cx, fy - 1, cz)) in AIR:
            gaps.append((cx, fy - 1, cz))
            if len(gaps) >= max_len: break
        else: break
    return edge, gaps
def _require_end(state, min_hp: float = 8.0) -> str:
    if not state.ready: return "状态尚未就绪"
    if _dim(state) != "the_end": return "需先进入末地"
    return f"生命值低于 {min_hp:g}，暂不执行末地高风险动作" if state.hp < min_hp else ""
def _cleanup_posture(ex, state) -> None:
    if (st := state.st or {}).get("sneaking") or st.get("blocking"):
        try: _clear_posture(ex, state, sneak=False, shield=False)
        except Exception: pass

def bridge_segment(ex, state, blocks: Sequence, *, item: Optional[str] = None, max_blocks: int = 16) -> Iterator[dict]:
    """L2 原语：带虚空承重保护与材料止损的安全潜行搭桥（不进注册表）。"""
    if not isinstance(max_blocks, int) or isinstance(max_blocks, bool) or max_blocks < 1: raise Replan("max_blocks 必须是正整数")
    st = state.refresh_status(ex.client, max_age_s=0); refresh_inventory(ex, state)
    if not _has_solid_footing(ex, st): raise Replan("搭桥起点脚下无安全实体支撑或处于危险状态，拒绝搭桥")
    to_place, seen = [], set()
    for raw in blocks:
        if (cell := block_cell(raw)) in seen: continue
        seen.add(cell)
        if _is_solid_floor(ex, cell): continue
        if _blk_name(ex, cell) not in AIR: raise Replan(f"搭桥目标格 {list(cell)} 非空气且不可安全承重")
        to_place.append(cell)
        if len(to_place) >= max_blocks: break
    if not to_place: return []
    if (avail := total_placeable(state, item)) < len(to_place): raise Replan(f"搭桥方块不足：需 {len(to_place)} 块，当前仅 {avail} 块")
    yield from nav.sneak_bridge(ex, state, [list(c) for c in to_place], item=item)
    after = state.refresh_status(ex.client, max_age_s=0)
    if not _has_solid_footing(ex, after) or any(not _is_solid_floor(ex, c) for c in to_place):
        raise Replan("搭桥完成后未确认桥面实心方块与安全落脚支撑")
    return to_place

class _EndSkill(Skill):
    verified, _done_msg = "offline", "未确认技能完成"
    def _begin(self, ex, state) -> dict:
        self._done = False
        st = state.refresh_status(ex.client, max_age_s=0); refresh_inventory(ex, state)
        if reason := self.blocked(state): raise Replan(reason)
        return st
    def run(self, ex, state) -> Iterator[dict]:
        try: yield from self._exec(ex, state)
        finally: _cleanup_posture(ex, state)
    def _exec(self, ex, state) -> Iterator[dict]: raise NotImplementedError
    def verify_done(self, state, before_counts=None) -> str: return "" if getattr(self, "_done", False) else self._done_msg

@register
class EnterTheEnd(_EndSkill):
    name, description, goal = "enter_the_end", "补齐要塞末地传送门之眼并安全穿越进入末地", "end:enter_the_end"
    verified = "live"
    params = {"radius": Param(int, "搜索要塞传送门框半径", default=32, min=8, max=64)}
    _done_msg = "尚未确认安全进入末地"
    @classmethod
    def blocked(cls, state) -> str:
        if not state.ready: return "状态尚未就绪"
        if (dim := _dim(state)) == "the_end": return ""
        if dim != "overworld": return f"当前位于 {dim}，需在主世界寻找要塞传送门"
        if state.hp < 8: return "生命值过低，暂不进入末地"
        return "" if (state.count("ender_eye") >= 1 or memo_for(state).has_landmark("end_portal", dimension="overworld")) else "背包缺少末影之眼"
    def verify_done(self, state, before_counts=None) -> str:
        return "" if getattr(self, "_done", False) or _dim(state) == "the_end" else self._done_msg

    def _exec(self, ex, state):
        st = self._begin(ex, state)
        if _dim(st) == "the_end": self._done = True; return
        radius, memo = self.args["radius"], memo_for(state)
        blocks = _scan_ready(ex, state, ("end_portal_frame", "end_portal"), radius, limit=32)
        portals, frames = [b for b in blocks if b["name"] == "end_portal"], [b for b in blocks if b["name"] == "end_portal_frame"]
        if not portals and not frames:
            for kind in ("end_portal", "end_portal_frame"):
                for pos in memo.landmarks(kind, dimension="overworld"):
                    if bare((blk := observe_block(ex, list(pos))).get("name")) == kind:
                        (portals if kind == "end_portal" else frames).append({**blk, "pos": list(pos), "name": kind})
        if not portals and not frames: raise _env_blocked(f"半径 {radius} 格内未发现末地传送门框或激活门", "end_portal_frame", f"半径 {radius} 格")
        if frames: memo.record_landmark("end_portal_frame", frames[0]["pos"], dimension="overworld")
        if not portals:
            unfilled = sorted((b for b in frames if str((b.get("properties") or {}).get("eye", "")).lower() != "true"), key=lambda b: eye_to_block_dist(st, b["pos"]))
            for frame in unfilled:
                refresh_inventory(ex, state)
                if state.count("ender_eye") < 1: raise Replan("末影之眼不足，无法补齐末地传送门框")
                fpos = block_cell(frame["pos"])
                yield from _approach_block(ex, state, fpos, reach=STATION_EYE_REACH, max_distance=radius)
                before_eyes = state.count("ender_eye")
                ev = yield from use_item_on(ex, state, fpos, "ender_eye")
                blk_after = ev["after"].get("block") or observe_block(ex, list(fpos))
                if not (str((blk_after.get("properties") or {}).get("eye", "")).lower() == "true" or state.count("ender_eye") < before_eyes or ev["receipt"].get("changed") is True):
                    raise Replan(f"向 {list(fpos)} 嵌入末影之眼后未确认生效")
            if not (portals := _scan_ready(ex, state, "end_portal", radius)): raise Replan("补齐末影之眼后未观察到激活的末地传送门")
        st = state.refresh_status(ex.client, max_age_s=0)
        pcell = block_cell(min(portals, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
        memo.record_landmark("end_portal", pcell, dimension="overworld")
        yield from self._traverse_end_portal(ex, state, pcell, radius)
        self._done = True

    def _traverse_end_portal(self, ex, state, pcell, radius: int):
        st = state.refresh_status(ex.client, max_age_s=0)
        start = block_cell(feet_pos(st))
        if not _standable(ex, pcell, allow_portal=True): raise Replan("末地传送门上方净空不足")
        adj = [n for dy in (0, 1) for n in _neighbors((pcell[0], pcell[1] + dy, pcell[2])) if _standable(ex, n)]
        entry = Entry(pcell, () if start == pcell else tuple(adj[:1]))
        if start != pcell and entry.route and start != entry.route[0]:
            yield from _move_safe(ex, state, _center(entry.route[0]), rng=1, max_distance=radius)
        st, stopped = state.refresh_status(ex.client, max_age_s=0), False
        try:
            if block_cell(feet_pos(st)) != pcell:
                yield rec("move_to", ex.move_to(pos=_center(pcell), rng=0, stop_on=("hurt", "death")), pos=list(pcell), purpose="enter_end_portal")
            for attempt in range(_PORTAL_POLLS):
                now = state.refresh_status(ex.client, max_age_s=0)
                if _hazard(now) or float(now.get("health", 20)) < float(st.get("health", 20)): raise Replan("穿越末地传送门时观察到受伤或流体危险")
                if _dim(now) == "the_end" and now.get("ready") is True: break
                if attempt + 1 < _PORTAL_POLLS:
                    time.sleep(_POLL_SLEEP_S)
                    yield rec("wait_portal", reason="await_end_ready" if _dim(now) == "the_end" else "await_end_dimension")
            else:
                if _dim(now) == "the_end": raise Replan("已进入末地但区块尚未就绪（ready=false）")
                raise _env_blocked("步入末地传送门后未确认切入末地", "end_transition", f"1 次进门，{_PORTAL_POLLS} 次状态读取")
            try: stop_rcpt = ex.client.stop()
            except Exception as exc:
                raise Replan("末地切维度期间 /stop 返回 not_ready" if failure(exc)[0] == "not_ready" else f"穿门后 /stop 失败: {exc}") from exc
            stopped = True
            yield rec("stop", stop_rcpt, purpose="end_portal_landing")
            if isinstance(stop_rcpt, dict) and stop_rcpt.get("released") is False: raise Replan("穿门后 /stop 未释放动作锁，暂停后续动作")
            landed = state.refresh_status(ex.client, max_age_s=0)
            if _dim(landed) != "the_end" or landed.get("busy") or not _has_solid_footing(ex, landed): raise Replan("末地落点维度、姿态或实体承重未确认")
            memo_for(state).record_landmark("end_spawn", landed["pos"], dimension="the_end")
        finally:
            if not stopped: ex.stop()

@register
class EnterEndGateway(_EndSkill):
    name, description, goal = "enter_end_gateway", "向末地折跃门投掷末影珍珠并确认安全抵达外岛", "end:enter_end_gateway"
    verified = "live"
    params = {"radius": Param(int, "搜索折跃门半径", default=48, min=8, max=64)}
    _done_msg = "尚未通过折跃门安全抵达末地外岛"
    @classmethod
    def blocked(cls, state) -> str:
        return msg if (msg := _require_end(state, 8.0)) else ("" if state.count("ender_pearl") >= 1 else "背包缺少末影珍珠")
    def _exec(self, ex, state):
        st, radius = self._begin(ex, state), self.args["radius"]
        if not (blocks := _scan_ready(ex, state, "end_gateway", radius)):
            raise _env_blocked(f"半径 {radius} 格内未发现末地折跃门", "end_gateway", f"半径 {radius} 格")
        gw_pos = block_cell(min(blocks, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
        memo_for(state).record_landmark("end_gateway", gw_pos, dimension="the_end")
        st = yield from _approach_block(ex, state, gw_pos, reach=8.0, max_distance=radius, need_los=True, max_r=6)
        if not can_reach_block(ex, st, gw_pos, reach=8.0, check_los=True): raise Replan("靠近后仍无法直视或触达末地折跃门")
        start_pos, aim, teleported = feet_pos(st), list(block_center(gw_pos)), False
        yield rec("use_item", ex.use_item("ender_pearl", target={"pos": aim}), item="ender_pearl", target={"pos": aim})
        for attempt in range(_GATEWAY_POLLS):
            now = state.refresh_status(ex.client, max_age_s=0)
            if _dim(now) != "the_end" or _hazard(now): raise Replan("折跃门传送后维度或安全状态异常")
            cur = feet_pos(now)
            if math.hypot(cur[0] - start_pos[0], cur[2] - start_pos[2]) >= 20.0 or math.hypot(cur[0], cur[2]) >= 500.0:
                teleported = True
                if now.get("ready") is True:
                    if _has_solid_footing(ex, now):
                        memo_for(state).record_landmark("end_outer_island", cur, dimension="the_end")
                        self._done = True; return
                    if now.get("on_ground") is True: raise Replan("末地折跃门落点脚下无安全实体支撑（疑似虚空）")
            if attempt + 1 < _GATEWAY_POLLS:
                time.sleep(_POLL_SLEEP_S); yield rec("wait_gateway", reason="await_outer_island")
        raise Replan("折跃门传送后未确认安全实体落点" if teleported else "投掷末影珍珠后未确认外岛位移")

@register
class FindEndCity(_EndSkill):
    name, description, goal = "find_end_city", "在末地外岛探索并搭桥抵达末地城", "end:find_end_city"
    verified = "live"
    params = {"radius": Param(int, "扫描半径", default=48, min=16, max=64), "max_hops": Param(int, "最大探索段数", default=6, min=1, max=16)}
    _done_msg = "尚未发现并抵达末地城"
    @classmethod
    def blocked(cls, state) -> str:
        if msg := _require_end(state, 8.0): return msg
        return "" if (total_placeable(state) >= 8 or state.count("ender_pearl") >= 1) else "缺少搭桥方块（至少 8 块）与末影珍珠"
    def _bridge_gap(self, ex, state, st, edge, gaps, max_blocks: int, max_dist: int):
        if edge and eye_to_block_dist(st, gaps[0]) > nav.PLACE_REACH:
            yield from _move_safe(ex, state, _center(edge), rng=1, max_distance=max_dist)
        return (yield from bridge_segment(ex, state, gaps, max_blocks=max_blocks))
    def _exec(self, ex, state):
        self._begin(ex, state)
        radius, max_hops, memo = self.args["radius"], self.args["max_hops"], memo_for(state)
        for hop in range(max_hops):
            st = state.refresh_status(ex.client, max_age_s=0); refresh_inventory(ex, state)
            if _dim(st) != "the_end" or float(st.get("health", 20)) < 8.0: raise Replan("探索末地城期间离开末地或生命值低于 8，触发止损")
            if total_placeable(state) < 8 and state.count("ender_pearl") < 1: raise Replan("探索末地城方块与末影珍珠储备不足，触发止损")
            if blocks := _scan_ready(ex, state, STRUCTURE_FINGERPRINTS["end_city"], radius):
                best = min(blocks, key=lambda b: eye_to_block_dist(st, b["pos"]))
                cpos = block_cell(best["pos"])
                memo.record_landmark("end_city", cpos, dimension="the_end", meta={"block": best["name"]})
                if eye_to_block_dist(st, cpos) <= 6.0 and _has_solid_footing(ex, st): self._done = True; return
                edge, gaps = _plan_gap_toward(ex, st, cpos, max_len=16)
                if gaps: yield from self._bridge_gap(ex, state, st, edge, gaps, 16, radius)
                st = state.refresh_status(ex.client, max_age_s=0)
                dest = _standable_near(ex, cpos, st, max_r=4, max_eye=6.0) or ([gaps[-1][0] + 0.5, float(gaps[-1][1] + 1), gaps[-1][2] + 0.5] if gaps else None)
                if dest is not None: yield from _move_safe(ex, state, dest, rng=1, max_distance=radius)
                after = state.refresh_status(ex.client, max_age_s=0)
                if eye_to_block_dist(after, cpos) <= 8.0 and _has_solid_footing(ex, after): self._done = True; return
                continue
            if not (yield from self._explore_hop(ex, state, st, hop)): break
        raise _env_blocked(f"探索 {max_hops} 段后未抵达末地城", "end_city", f"{max_hops} 段 × 半径 {radius} 格")
    def _explore_hop(self, ex, state, st, hop: int):
        fx, fy, fz = block_cell(feet_pos(st))
        dirs = _CARDINAL[hop % 4:] + _CARDINAL[:hop % 4]
        for dx, dz in dirs:
            far = None
            for step in range(1, 13):
                if _standable(ex, (c := (fx + dx * step, fy, fz + dz * step))): far = c
                else: break
            if far and abs(far[0] - fx) + abs(far[2] - fz) >= 4:
                yield from _move_safe(ex, state, _center(far), rng=1, max_distance=16); return True
        if total_placeable(state) >= 8:
            for dx, dz in dirs:
                edge, gaps = _plan_gap_toward(ex, st, (fx + dx * 8, fy, fz + dz * 8), max_len=min(6, total_placeable(state) - 2))
                if gaps and (placed := (yield from self._bridge_gap(ex, state, st, edge, gaps, 8, 16))):
                    yield from _move_safe(ex, state, [placed[-1][0] + 0.5, float(placed[-1][1] + 1), placed[-1][2] + 0.5], rng=1, max_distance=16); return True
        return False

def _fall_rescue(ex, state, st) -> Iterator[dict]:
    """按优先级执行单次防摔落救援（1 阻塞动作 = 1 yield）。"""
    refresh_inventory(ex, state)
    fx, fy, fz = block_cell(feet_pos(st))
    if item := next((it for it in ("potion", "chorus_fruit") if state.count(it) >= 1), None):
        yield rec("use_item", ex.use_item(item), item=item, purpose="fall_rescue")
    elif state.count("ender_pearl") >= 1:
        blocks = scan_blocks(ex, state, _END_SOLID, radius=16, limit=8)["blocks"]
        aim = list(block_center(blocks[0]["pos"])) if blocks else [fx + 0.5, float(max(1, fy - 8)), fz + 0.5]
        yield rec("use_item", ex.use_item("ender_pearl", target={"pos": aim}), item="ender_pearl", target={"pos": aim}, purpose="fall_rescue")
    else:
        land = next(((fx, fy - d, fz) for d in range(1, 8) if _blk_name(ex, (fx, fy - d, fz)) in AIR and _is_solid_floor(ex, (fx, fy - d - 1, fz))), (fx, fy - 1, fz))
        if state.count("water_bucket") >= 1:
            yield rec("use_item", ex.use_item("water_bucket", target={"pos": list(land)}), item="water_bucket", target={"pos": list(land)}, purpose="fall_rescue")
        elif blk := next((b for b in ("hay_block", "cobweb") if state.count(b) >= 1), None):
            yield rec("place", ex.place(blk, pos=list(land)), item=blk, pos=list(land), purpose="fall_rescue")
        else:
            raise Replan("高空漂浮结束但背包无可用摔落救援物品")
    refresh_inventory(ex, state)

@register
class LevitateFifty(_EndSkill):
    name, description, goal = "levitate_fifty", "借助潜影贝漂浮上升 50 格并安全落地", "end:levitate_fifty"
    verified = "live"
    params = {"radius": Param(int, "搜索潜影贝半径", default=32, min=8, max=64)}
    _done_msg = "尚未完成 50 格漂浮上升与安全落地"
    @classmethod
    def blocked(cls, state) -> str:
        if msg := _require_end(state, 12.0): return msg
        return "" if any(state.count(it) >= 1 for it in _RESCUE_ITEMS) else "背包缺少摔落救援物品（药水/紫颂果/珍珠/水桶/干草块/蜘蛛网）"
    def verify_done(self, state, before_counts=None) -> str:
        return "" if getattr(self, "_done", False) and getattr(self, "_achieved_rise", 0.0) >= 50.0 else self._done_msg
    def _exec(self, ex, state):
        self._achieved_rise = 0.0
        st, radius = self._begin(ex, state), self.args["radius"]
        if not (ents := _scan_ready(ex, state, "shulker", radius, entity=True)):
            raise _env_blocked(f"半径 {radius} 格内未发现潜影贝", "visible_entity:shulker", f"半径 {radius} 格实体")
        shulker = min(ents, key=lambda e: eye_to_block_dist(st, e["pos"]))
        st = yield from _approach_block(ex, state, block_cell(shulker["pos"]), reach=12.0, max_distance=radius, need_los=True, max_r=6)
        start_y = max_y = feet_pos(st)[1]; now = st
        for _ in range(_LEVITATE_POLLS):
            now = state.refresh_status(ex.client, max_age_s=0)
            cur_y, hp = feet_pos(now)[1], float(now.get("health", 20))
            max_y = max(max_y, cur_y)
            if hp < 8.0 or now.get("alive") is False:
                if now.get("alive", True) and now.get("on_ground") is not True and cur_y - start_y >= 6.0: yield from _fall_rescue(ex, state, now)
                raise Replan(f"漂浮期间生命值过低（hp={hp:.1f}），已触发止损")
            if max_y - start_y >= 50.0:
                self._achieved_rise = max_y - start_y
                yield from _fall_rescue(ex, state, now); self._done = True; return
            yield rec("wait_levitate", None, start_y=start_y, cur_y=cur_y, max_rise=round(max_y - start_y, 2))
        if now.get("on_ground") is not True and feet_pos(now)[1] - start_y >= 6.0: yield from _fall_rescue(ex, state, now)
        raise Replan(f"等待潜影贝漂浮超时，仅上升 {max_y - start_y:.1f} 格（目标 50 格）")
