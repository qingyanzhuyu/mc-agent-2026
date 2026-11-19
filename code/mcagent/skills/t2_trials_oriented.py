"""Stage 2 Wave 2 试炼密室与朝向放置 L2 原语（3 项）+ L3 技能（5 项）。"""
from __future__ import annotations

import math
from collections.abc import Generator, Sequence

from ..runtime import EnvBlocked, Executor, Replan, State
from .base import Param, Skill, rec
from .encounter import _retreat_clear, combat_melee
from .field import equip_item, scan_blocks, scan_entities
from .interaction import _clear_posture, use_item_on
from .items import AIR, bare, is_clear_space, is_safe_solid_floor
from .observations import observe_block, refresh_inventory
from .registry import register
from .sightline import DIG_EYE_REACH, STATION_EYE_REACH, block_cell, can_reach_block, eye_to_block_dist
from .voxel_cache import voxel_cache_for

_CARDINALS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_OFFSETS_6 = ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (0, 1, 0), (0, -1, 0))
_FACING_DELTA = {"north": (0, 0, -1), "south": (0, 0, 1), "east": (1, 0, 0), "west": (-1, 0, 0)}
_DELTA_FACING = {v: k for k, v in _FACING_DELTA.items()}
_TRIAL_HOSTILES = ("breeze", "bogged", "skeleton", "stray", "zombie", "husk", "spider", "cave_spider", "slime", "silverfish")
_MELEE_WEAPONS = ("netherite_sword", "diamond_sword", "iron_sword", "stone_sword", "netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "mace", "wooden_sword")
_AXES = ("netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "golden_axe", "wooden_axe")
_BOOKS = ("book", "enchanted_book", "writable_book", "written_book")
_UNWAXED_OX_BULBS = ("exposed_copper_bulb", "weathered_copper_bulb", "oxidized_copper_bulb")
_ALL_OX_BULBS = _UNWAXED_OX_BULBS + tuple(f"waxed_{b}" for b in _UNWAXED_OX_BULBS)
_OX_STAGE = {"copper_bulb": 0, "exposed_copper_bulb": 1, "weathered_copper_bulb": 2, "oxidized_copper_bulb": 3}

def _dim(state: State) -> str: return str((state.st or {}).get("dimension") or "overworld")
def _pt(c: tuple[int, int, int]) -> tuple[float, float, float]: return (c[0] + 0.5, float(c[1]), c[2] + 0.5)
def _is_air(ex: Executor, pos: Sequence[int]) -> bool: return bare(observe_block(ex, list(pos)).get("name")) in AIR
def _has_floor(ex: Executor, c: tuple[int, int, int]) -> bool: return is_safe_solid_floor(observe_block(ex, [c[0], c[1] - 1, c[2]]))
def _is_lit(blk: dict) -> bool: return (blk.get("properties") or {}).get("lit") in (True, "true")
def _pick_axe(state: State) -> str | None: return next((a for a in _AXES if state.count(a) >= 1), None)
def _gate(state: State, ok: bool, msg: str) -> str: return "世界尚未就绪（ready=false）" if not state.ready else ("" if ok else msg)
def _env_blocked(msg: str, pred: str, vol: str) -> EnvBlocked: return EnvBlocked(msg, missing_predicate=pred, searched_volume=vol, wake_on="chunk_moved")

def _ready_scan(scan: dict) -> dict:
    if scan.get("ready") is False: raise Replan("世界加载尚未就绪（ready=false）")
    return scan

def _is_standable(ex: Executor, cell: tuple[int, int, int]) -> bool:
    x, y, z = cell
    return _has_floor(ex, cell) and is_clear_space(observe_block(ex, [x, y, z])) and is_clear_space(observe_block(ex, [x, y + 1, z]))

def _overlaps_player(cell: tuple[int, int, int], player_pos: Sequence[float]) -> bool:
    px, py, pz = block_cell(player_pos)
    return cell[0] == px and cell[2] == pz and abs(cell[1] - py) <= 1

def _pick_switch(state: State) -> str | None:
    for item in ("lever", "stone_button"):
        if state.count(item) >= 1: return item
    return next((n for r in (state.inventory or {}).get("items") or ()
                 if isinstance(r, dict) and (n := bare(r.get("name"))).endswith("_button") and state.count(n) >= 1), None)

def _standable_near_block(ex: Executor, target: tuple[int, int, int], origin: Sequence[float], *, reach: float = STATION_EYE_REACH) -> tuple[int, int, int] | None:
    tx, ty, tz = target
    cands = [(tx + dx, ty + dy, tz + dz) for dy in (0, -1, 1, -2, 2) for dx in range(-3, 4) for dz in range(-3, 4)
             if not (dx == 0 and dz == 0 and abs(dy) <= 1) and _is_standable(ex, (tx + dx, ty + dy, tz + dz))
             and can_reach_block(ex, _pt((tx + dx, ty + dy, tz + dz)), target, reach=reach)]
    return min(cands, key=lambda c: math.dist(_pt(c), origin)) if cands else None

def _approach_block(ex: Executor, state: State, target: tuple[int, int, int], *, reach: float = STATION_EYE_REACH) -> Generator[dict, None, dict]:
    st = state.refresh_status(ex.client, max_age_s=0)
    if can_reach_block(ex, st, target, reach=reach) and not _overlaps_player(target, st["pos"]): return st
    dest_cell = _standable_near_block(ex, target, st["pos"], reach=reach) or _standable_near_block(ex, target, st["pos"], reach=DIG_EYE_REACH)
    if dest_cell is None: raise _env_blocked(f"目标 {list(target)} 附近无安全可达站位", "standable_approach", "目标附近 3 格")
    dest = list(_pt(dest_cell))
    yield rec("move_to", ex.move_to(pos=dest, rng=1), pos=dest)
    st = state.refresh_status(ex.client, max_age_s=0)
    if eye_to_block_dist(st, target) > DIG_EYE_REACH: raise Replan(f"接近目标 {list(target)} 后仍超出眼距范围")
    return st

def _find_retreat(ex: Executor, st: dict, away_from: Sequence[float]) -> list[float] | None:
    ox, oy, oz = block_cell(st["pos"])
    cands = sorted(([ox + dx + 0.5, float(oy), oz + dz + 0.5] for dx, dz in _CARDINALS), key=lambda p: math.dist(p, away_from), reverse=True)
    return next((c for c in cands if _retreat_clear(ex, st["pos"], c)), None)

def _oriented_candidates(ex: Executor, cell: tuple[int, int, int], origin: Sequence[float], *, want_axis: str | None, max_sides: int) -> list[tuple[int, int, int]]:
    bx, by, bz = cell
    dy_order = (1, 0, -1, -2, 2) if want_axis == "y" else (0, -1, 1, -2, 2)
    cur_cell, cands, seen = block_cell(origin), [], set()
    for dx, dz in _CARDINALS:
        for r in (1, 2):
            hit = next(((bx + dx * r, by + dy, bz + dz * r) for dy in dy_order
                        if (bx + dx * r, by + dy, bz + dz * r) not in seen and _is_standable(ex, (bx + dx * r, by + dy, bz + dz * r))
                        and can_reach_block(ex, _pt((bx + dx * r, by + dy, bz + dz * r)), cell, reach=DIG_EYE_REACH)), None)
            if hit is not None: seen.add(hit); cands.append(hit); break
    if want_axis == "y" and len(cands) < max_sides:
        for dx, dz in ((0, 0), *_CARDINALS):
            for dy in (2, 1, -1, -2):
                cand = (bx + dx, by + dy, bz + dz)
                if cand not in seen and not (dx == 0 and dz == 0 and abs(dy) <= 1) and _is_standable(ex, cand) and can_reach_block(ex, _pt(cand), cell, reach=DIG_EYE_REACH):
                    seen.add(cand); cands.append(cand)
    cands.sort(key=lambda c: (0 if c == cur_cell else 1, math.dist(_pt(c), origin)))
    return cands[:max_sides]

def _place_cell(ex: Executor, state: State, item: str, cell: tuple[int, int, int]) -> Generator[dict, None, None]:
    receipt = ex.place(item, list(cell))
    voxel_cache_for(state).invalidate(cell, dimension=_dim(state))
    yield rec("place", receipt, item=item, pos=list(cell))

# ---- L2 原语（不注册）

def place_oriented(ex: Executor, state: State, pos: Sequence[float], item: str, *, want_facing: str | None = None, want_axis: str | None = None, max_sides: int = 4) -> Generator[dict, None, dict]:
    """绕目标格多侧站位放置方块并复核朝向/轴向；错向时挖回换侧重试。"""
    cell, item = block_cell(pos), bare(item)
    refresh_inventory(ex, state)
    if state.count(item) < 1: raise Replan(f"背包缺少要放置的 {item}")
    if not _is_air(ex, cell): raise Replan(f"放置格 {list(cell)} 非空，无法定向放置 {item}")
    st = state.refresh_status(ex.client, max_age_s=0)
    cands = _oriented_candidates(ex, cell, st["pos"], want_axis=want_axis, max_sides=max(1, int(max_sides)))
    if not cands:
        if not _overlaps_player(cell, st["pos"]) and can_reach_block(ex, st, cell, reach=DIG_EYE_REACH): cands = [block_cell(st["pos"])]
        else: raise Replan(f"放置格 {list(cell)} 周围无可达站位")
    last_facing, last_axis = None, None
    for idx, cand in enumerate(cands):
        st, dest = state.refresh_status(ex.client, max_age_s=0), list(_pt(cand))
        if block_cell(st["pos"]) != cand and math.dist(st["pos"], dest) > 0.6:
            yield rec("move_to", ex.move_to(pos=dest, rng=1), pos=dest)
            st = state.refresh_status(ex.client, max_age_s=0)
        if _overlaps_player(cell, st["pos"]) or eye_to_block_dist(st, cell) > DIG_EYE_REACH or not can_reach_block(ex, st, cell, reach=DIG_EYE_REACH): continue
        refresh_inventory(ex, state)
        if state.count(item) < 1: raise Replan(f"背包缺少要放置的 {item}")
        yield from _place_cell(ex, state, item, cell)
        blk = ex.client.get_block(list(cell))
        if bare(blk.get("name")) in AIR: raise Replan(f"放置 {item} 后 {list(cell)} 仍为空气")
        props = blk.get("properties") or {}
        last_facing, last_axis = props.get("facing"), props.get("axis")
        if (want_facing is None or last_facing == want_facing) and (want_axis is None or last_axis == want_axis): return blk
        if idx < len(cands) - 1:
            dig_r = ex.dig(list(cell), collect=True)
            voxel_cache_for(state).invalidate(cell, dimension=_dim(state))
            yield rec("dig", dig_r, pos=list(cell))
            refresh_inventory(ex, state)
    raise Replan(f"{item} 朝向不匹配：期望 facing={want_facing}/axis={want_axis}，实测 facing={last_facing}/axis={last_axis}")

def power_block_once(ex: Executor, state: State, target_pos: Sequence[float]) -> Generator[dict, None, dict]:
    """在目标相邻空位放置拉杆或按钮并触发一次红石脉冲。"""
    refresh_inventory(ex, state)
    if (sw := _pick_switch(state)) is None: raise Replan("背包缺少拉杆或按钮")
    tcell, st = block_cell(target_pos), state.refresh_status(ex.client, max_age_s=0)
    pick_adj = lambda cur, reach: next(((tcell[0] + dx, tcell[1] + dy, tcell[2] + dz) for dx, dy, dz in _OFFSETS_6
                                        if _is_air(ex, (tcell[0] + dx, tcell[1] + dy, tcell[2] + dz))
                                        and not _overlaps_player((tcell[0] + dx, tcell[1] + dy, tcell[2] + dz), cur["pos"])
                                        and can_reach_block(ex, cur, (tcell[0] + dx, tcell[1] + dy, tcell[2] + dz), reach=reach)), None)
    if (adj := pick_adj(st, STATION_EYE_REACH)) is None:
        st = yield from _approach_block(ex, state, tcell, reach=STATION_EYE_REACH)
        adj = pick_adj(st, DIG_EYE_REACH)
    if adj is None: raise Replan(f"目标 {list(tcell)} 旁无可达空位放置 {sw}")
    yield from _place_cell(ex, state, sw, adj)
    yield rec("interact", ex.interact(pos=list(adj), item=None), pos=list(adj), item=None)
    return {"switch_pos": list(adj), "item": sw, "target": ex.client.get_block(list(tcell)), "switch": ex.client.get_block(list(adj))}

def clear_trial_wave(ex: Executor, state: State, radius: int = 24, max_kills: int = 8, min_hp: float = 10.0) -> Generator[dict, None, int]:
    """清理半径内试炼密室敌对生物，带血量止损与击杀上限。"""
    cleared = 0
    for _ in range(max_kills):
        st = state.refresh_status(ex.client, max_age_s=0)
        if not st.get("alive", True) or float(st.get("health", 20)) < min_hp:
            raise Replan(f"血量低于止损线 {min_hp:.1f}，中止清怪")
        hostiles = [e for e in (_ready_scan(scan_entities(ex, state, _TRIAL_HOSTILES, radius=radius)).get("entities") or ()) if bare(e.get("name")) in _TRIAL_HOSTILES]
        if not hostiles: return cleared
        target = min(hostiles, key=lambda e: math.dist(st["pos"], e["pos"]))
        refresh_inventory(ex, state)
        if (weapon := next((w for w in _MELEE_WEAPONS if state.count(w) >= 1), None)) is not None:
            yield from equip_item(ex, state, weapon, "hand")
        if math.dist(st["pos"], target["pos"]) > 4.5:
            dest = [float(target["pos"][0]), float(target["pos"][1]), float(target["pos"][2])]
            yield rec("move_to", ex.move_to(pos=dest, rng=2), pos=dest)
            st = state.refresh_status(ex.client, max_age_s=0)
            if not st.get("alive", True) or float(st.get("health", 20)) < min_hp:
                raise Replan(f"接近敌对生物后血量低于 {min_hp:.1f}，中止清怪")
        if (retreat := _find_retreat(ex, st, target["pos"])) is None: raise Replan("近战清怪缺少同高安全退路格")
        yield from combat_melee(ex, state, target, retreat_pos=retreat, radius=min(16, radius))
        cleared += 1
    return cleared

# ---- L3 注册技能

class _TrialSkill(Skill):
    verified, _done_msg, _delta_keys = "offline", "未确认技能完成", ()
    def _begin(self, ex: Executor, state: State) -> dict:
        self._done = False
        refresh_inventory(ex, state)
        st = state.refresh_status(ex.client, max_age_s=0)
        if reason := self.blocked(state): raise Replan(reason)
        return st
    def run(self, ex: Executor, state: State):
        try: yield from self._run(ex, state)
        finally:
            if any(state.st.get(k) for k in ("sneaking", "blocking", "jumping")):
                _clear_posture(ex, state, sneak=False, shield=False, jump=False)
    def _run(self, ex: Executor, state: State): raise NotImplementedError
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        ok = getattr(self, "_done", False) or (bool(before_counts) and any(state.count(k) < before_counts.get(k, 0) for k in self._delta_keys))
        return "" if ok else self._done_msg

@register
class UnlockTrialVault(_TrialSkill):
    name, goal, description = "unlock_trial_vault", "t2:under_lock_and_key", "清理试炼怪并用试炼钥匙开启试炼宝库"
    params = {"radius": Param(int, "搜索宝库半径", default=32, min=8, max=64), "clear_wave": Param(bool, "解锁前清试炼怪", default=True)}
    _done_msg, _delta_keys = "未确认试炼宝库解锁", ("trial_key", "ominous_trial_key")
    @classmethod
    def blocked(cls, state: State) -> str:
        return _gate(state, state.count("trial_key") >= 1 or state.count("ominous_trial_key") >= 1, "背包缺少试炼钥匙（trial_key / ominous_trial_key）")
    def _run(self, ex: Executor, state: State):
        st, radius, clear_wave = self._begin(ex, state), self.args["radius"], self.args["clear_wave"]
        blocks = _ready_scan(scan_blocks(ex, state, "vault", radius=radius, limit=8, skill_name=self.name))["blocks"]
        if not blocks: raise _env_blocked(f"半径 {radius} 格内未发现试炼宝库", "visible_block:vault", f"半径 {radius} 格")
        vault = min(blocks, key=lambda b: (1 if (b.get("properties") or {}).get("vault_state") == "inactive" else 0, eye_to_block_dist(st, b["pos"])))
        vpos = block_cell(vault["pos"])
        if clear_wave and any(bare(e.get("name")) in _TRIAL_HOSTILES for e in (scan_entities(ex, state, _TRIAL_HOSTILES, radius=min(16, radius)).get("entities") or ())):
            yield from clear_trial_wave(ex, state, radius=min(radius, 24), max_kills=8)
        yield from _approach_block(ex, state, vpos, reach=STATION_EYE_REACH)
        refresh_inventory(ex, state)
        vprops = observe_block(ex, list(vpos)).get("properties") or {}
        key = "ominous_trial_key" if (vprops.get("ominous") in (True, "true") and state.count("ominous_trial_key") >= 1) else ("trial_key" if state.count("trial_key") >= 1 else "ominous_trial_key")
        before_k, before_vs = state.count(key), vprops.get("vault_state")
        ev = yield from use_item_on(ex, state, vpos, key)
        after_vs = ((ev["after"].get("block") or {}).get("properties") or {}).get("vault_state")
        if state.count(key) >= before_k and after_vs == before_vs and ev["receipt"].get("changed") is not True:
            raise Replan("解锁宝库后未确认钥匙消耗或宝库状态变化")
        self._done = True

@register
class WindChargeBounce(_TrialSkill):
    name, goal, description = "wind_charge_bounce", "t2:who_needs_rockets", "向脚下发射风弹并确认垂直弹跳达到目标高度"
    params = {"target_dy": Param(int, "目标弹跳高度", default=7, min=7, max=16)}
    _done_msg, _delta_keys = "未确认风弹弹跳达到目标高度", ("wind_charge",)
    @classmethod
    def blocked(cls, state: State) -> str:
        return _gate(state, state.count("wind_charge") >= 1, "背包缺少风弹（wind_charge）") or ("" if state.hp >= 10 else f"生命值过低（{state.hp:.1f} < 10）")
    def _run(self, ex: Executor, state: State):
        st, target_dy = self._begin(ex, state), self.args["target_dy"]
        cx, cy, cz = block_cell(st["pos"])
        if not all(_is_air(ex, (cx, cy + dy, cz)) for dy in range(1, target_dy + 3)):
            raise Replan(f"头顶净空不足 {target_dy + 2} 格，无法安全风弹弹跳")
        x, start_y, z = float(st["pos"][0]), float(st["pos"][1]), float(st["pos"][2])
        aim = [x, start_y - 0.5, z]
        yield rec("use_item", ex.use_item("wind_charge", target={"pos": aim}), item="wind_charge", target={"pos": aim})
        max_y = start_y
        for _ in range(5):
            max_y = max(max_y, float(state.refresh_status(ex.client, max_age_s=0)["pos"][1]))
            if max_y - start_y >= target_dy: break
        refresh_inventory(ex, state)
        if max_y - start_y < target_dy: raise Replan(f"风弹弹跳高度不足（{max_y - start_y:.1f} < {target_dy}）")
        self._done = True

@register
class ScrapeLitCopperBulb(_TrialSkill):
    name, goal, description = "scrape_lit_copper_bulb", "t2:lighten_up", "点亮氧化铜灯并用斧刮除氧化层"
    params = {"radius": Param(int, "搜索铜灯半径", default=32, min=8, max=64)}
    _done_msg = "未确认点亮并刮除氧化铜灯"
    @classmethod
    def blocked(cls, state: State) -> str: return _gate(state, _pick_axe(state) is not None, "背包缺少斧头")
    def _run(self, ex: Executor, state: State):
        st, radius = self._begin(ex, state), self.args["radius"]
        blocks = _ready_scan(scan_blocks(ex, state, _ALL_OX_BULBS, radius=radius, limit=12, skill_name=self.name))["blocks"]
        if not blocks: raise _env_blocked(f"半径 {radius} 格内未发现氧化铜灯", "visible_block:oxidized_copper_bulb", f"半径 {radius} 格")
        target = min(blocks, key=lambda b: (1 if bare(b["name"]).startswith("waxed_") else 0, 0 if _is_lit(b) else 1, eye_to_block_dist(st, b["pos"])))
        pos = block_cell(target["pos"])
        yield from _approach_block(ex, state, pos, reach=STATION_EYE_REACH)
        blk = ex.client.get_block(list(pos))
        if not _is_lit(blk):
            yield from power_block_once(ex, state, pos)
            blk = ex.client.get_block(list(pos))
            if not _is_lit(blk): raise Replan(f"给铜灯 {list(pos)} 通电后仍未点亮")
        if (axe := _pick_axe(state)) is None: raise Replan("背包缺少斧头")
        if bare(blk.get("name")).startswith("waxed_"):
            yield from use_item_on(ex, state, pos, axe)
            blk = ex.client.get_block(list(pos))
            if bare(blk.get("name")).startswith("waxed_"): raise Replan(f"刮蜡后铜灯 {list(pos)} 仍带蜡")
        before_name = bare(blk.get("name"))
        yield from use_item_on(ex, state, pos, axe)
        after = ex.client.get_block(list(pos))
        after_name = bare(after.get("name"))
        if _OX_STAGE.get(after_name, 99) >= _OX_STAGE.get(before_name, -1) or not _is_lit(after):
            raise Replan(f"刮除氧化层未确认点亮降级（{before_name} -> {after_name}, lit={_is_lit(after)}）")
        self._done = True

@register
class ReadChiseledBookshelf(_TrialSkill):
    name, goal, description = "read_chiseled_bookshelf", "t2:read_chiseled_bookshelf", "摆放比较器与雕纹书架并放入书籍触发信号"
    params = {"radius": Param(int, "搜索或放置半径", default=16, min=4, max=32)}
    _done_msg, _delta_keys = "未确认比较器读取雕纹书架", _BOOKS
    @classmethod
    def blocked(cls, state: State) -> str:
        return _gate(state, state.count("comparator") >= 1 and state.count("chiseled_bookshelf") >= 1, "背包缺少红石比较器或雕纹书架") or ("" if any(state.count(b) >= 1 for b in _BOOKS) else "背包缺少可放入书架的书籍")
    def _find_pair(self, ex: Executor, st: dict, radius: int) -> tuple[tuple[int, int, int], str | None] | None:
        ox, oy, oz = block_cell(st["pos"])
        lim, fallback = min(radius, 4), None
        for dy in (0, -1, 1):
            for dx in range(-lim, lim + 1):
                for dz in range(-lim, lim + 1):
                    c = (ox + dx, oy + dy, oz + dz)
                    if _overlaps_player(c, st["pos"]) or not _is_air(ex, c) or not _has_floor(ex, c) or not can_reach_block(ex, st, c, reach=DIG_EYE_REACH): continue
                    dirs = [(nx, nz) for nx, nz in _CARDINALS if _is_air(ex, (c[0] + nx, c[1], c[2] + nz)) and _has_floor(ex, (c[0] + nx, c[1], c[2] + nz))]
                    if len(dirs) == 4: return c, None
                    if dirs and fallback is None: fallback = (c, _DELTA_FACING[(dirs[0][0], 0, dirs[0][1])])
        return fallback
    def _run(self, ex: Executor, state: State):
        st = self._begin(ex, state)
        if (pair := self._find_pair(ex, st, self.args["radius"])) is None:
            raise _env_blocked("附近缺少相邻两格平整空位摆放比较器与雕纹书架", "clear_pair:comparator_bookshelf", f"半径 {self.args['radius']} 格")
        comp_pos, want_facing = pair
        comp_blk = yield from place_oriented(ex, state, comp_pos, "comparator", want_facing=want_facing)
        facing = (comp_blk.get("properties") or {}).get("facing")
        if facing not in _FACING_DELTA: raise Replan(f"比较器未返回有效朝向 facing={facing!r}")
        dx, dy, dz = _FACING_DELTA[facing]
        shelf_pos = (comp_pos[0] + dx, comp_pos[1] + dy, comp_pos[2] + dz)
        yield from place_oriented(ex, state, shelf_pos, "chiseled_bookshelf")
        yield from _approach_block(ex, state, shelf_pos, reach=STATION_EYE_REACH)
        refresh_inventory(ex, state)
        if (book := next((b for b in _BOOKS if state.count(b) >= 1), None)) is None: raise Replan("背包缺少可放入书架的书籍")
        before_b, before_comp, before_shelf = state.count(book), ex.client.get_block(list(comp_pos)), ex.client.get_block(list(shelf_pos))
        ev = yield from use_item_on(ex, state, shelf_pos, book)
        after_comp, after_shelf = ex.client.get_block(list(comp_pos)), ev["after"].get("block") or ex.client.get_block(list(shelf_pos))
        if not (state.count(book) < before_b or after_shelf.get("properties") != before_shelf.get("properties") or after_comp.get("properties") != before_comp.get("properties") or ev["receipt"].get("changed") is True):
            raise Replan("放入书籍后未确认消耗或书架/比较器状态更新")
        self._done = True

@register
class TransplantCreakingHeart(_TrialSkill):
    name, goal, description = "transplant_creaking_heart", "t2:heart_transplanter", "垂直摆放苍白橡木原木夹心嘎枝之心"
    params = {"radius": Param(int, "放置搜索半径", default=24, min=6, max=48)}
    _done_msg, _delta_keys = "未确认嘎枝之心垂直夹心摆放完成", ("creaking_heart",)
    @classmethod
    def blocked(cls, state: State) -> str:
        return _gate(state, state.count("creaking_heart") >= 1 and state.count("pale_oak_log") >= 2, "背包缺少嘎枝之心或 2 个苍白橡木原木")
    def _find_column(self, ex: Executor, st: dict, radius: int) -> tuple[int, int, int] | None:
        ox, oy, oz = block_cell(st["pos"])
        lim = min(radius, 4)
        for dy in (0, -1, 1):
            for dx in range(-lim, lim + 1):
                for dz in range(-lim, lim + 1):
                    base = (ox + dx, oy + dy, oz + dz)
                    if _overlaps_player(base, st["pos"]) or not _has_floor(ex, base): continue
                    if all(_is_air(ex, (base[0], base[1] + h, base[2])) for h in (0, 1, 2)) and _standable_near_block(ex, base, st["pos"], reach=DIG_EYE_REACH) is not None:
                        return base
        return None
    def _run(self, ex: Executor, state: State):
        st = self._begin(ex, state)
        if (base := self._find_column(ex, st, self.args["radius"])) is None:
            raise _env_blocked("附近缺少 3 格垂直空位摆放嘎枝之心", "clear_column_3:creaking_heart", f"半径 {self.args['radius']} 格")
        bot, mid, top = base, (base[0], base[1] + 1, base[2]), (base[0], base[1] + 2, base[2])
        yield from place_oriented(ex, state, bot, "pale_oak_log", want_axis="y")
        yield from place_oriented(ex, state, mid, "creaking_heart", want_axis="y")
        yield from place_oriented(ex, state, top, "pale_oak_log", want_axis="y")
        checks = ((bot, "pale_oak_log"), (mid, "creaking_heart"), (top, "pale_oak_log"))
        if not all(bare((b := ex.client.get_block(list(p))).get("name")) == name and (b.get("properties") or {}).get("axis") == "y" for p, name in checks):
            raise Replan("摆放后未确认三格竖向苍白橡木-嘎枝之心-苍白橡木结构与 axis=y")
        self._done = True
