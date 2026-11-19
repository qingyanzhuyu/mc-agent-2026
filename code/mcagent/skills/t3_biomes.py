"""Stage 2 Wave 3 群系 T3 技能：1 项 L2 引诱原语 + 8 项 L3 注册技能（+270 pts）。"""
from __future__ import annotations

import math, sys
from collections.abc import Generator, Mapping, Sequence

from ..client import ApiError
from ..runtime import EnvBlocked, Executor, Replan, State
from .base import Param, Skill, rec
from .encounter import _ARROWS, _retreat_clear, combat_melee, shoot_projectile
from .field import _grounded, equip_item, place_block, scan_blocks, scan_entities
from .interaction import _clear_posture, use_item_on
from .items import AIR, bare, is_clear_space, is_safe_solid_floor
from .observations import observe_block, refresh_inventory
from .registry import register
from .sightline import (DIG_EYE_REACH, ENTITY_EYE_REACH, STATION_EYE_REACH, block_cell,
                        block_center, can_reach_block, can_see_block, eye_to_block_dist, eye_to_point_dist)
from .voxel_cache import voxel_cache_for

_CARDINALS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_MELEE = ("netherite_sword", "diamond_sword", "iron_sword", "stone_sword", "netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "mace", "wooden_sword")
_AQUATIC_PREY = ("drowned", "guardian", "elder_guardian", "squid", "glow_squid", "cod", "salmon", "pufferfish", "tropical_fish", "tadpole")
_BOATS = ("oak_boat", "spruce_boat", "birch_boat", "jungle_boat", "acacia_boat", "dark_oak_boat", "mangrove_boat", "cherry_boat", "pale_oak_boat", "bamboo_raft")
_ALLAY_PREF = ("cobblestone", "dirt", "oak_planks", "stick", "apple", "cookie")
_NON_KILLABLE = frozenset({"player", "item", "experience_orb", "boat", "chest_boat", "minecart", "armor_stand", "allay", "villager", "iron_golem", "wolf", "cat", "axolotl"})

def _dim(state: State) -> str: return str((state.st or {}).get("dimension") or "overworld")
def _pt(c: tuple[int, int, int]) -> tuple[float, float, float]: return (c[0] + 0.5, float(c[1]), c[2] + 0.5)
def _is_air(ex: Executor, pos: Sequence[int]) -> bool: return bare(observe_block(ex, list(pos)).get("name")) in AIR
def _has_floor(ex: Executor, c: tuple[int, int, int]) -> bool: return is_safe_solid_floor(observe_block(ex, [c[0], c[1] - 1, c[2]]))
def _gate(state: State, ok: bool, msg: str) -> str: return "世界尚未就绪（ready=false）" if not state.ready else ("" if ok else msg)
def _env_blocked(msg: str, pred: str, vol: str, *, wake_on: str = "chunk_moved") -> EnvBlocked:
    return EnvBlocked(msg, missing_predicate=pred, searched_volume=vol, wake_on=wake_on)
def _ready(scan: dict) -> dict:
    if scan.get("ready") is False: raise Replan("世界加载尚未就绪（ready=false）")
    return scan
def _req_ents(ex: Executor, state: State, types, radius: int, msg: str, pred: str) -> list[dict]:
    ents = _ready(scan_entities(ex, state, types, radius=radius))["entities"]
    if not ents: raise _env_blocked(msg, pred, f"半径 {radius} 格实体")
    return ents
def _req_blks(ex: Executor, state: State, types, radius: int, msg: str, pred: str, skill: str = "") -> list[dict]:
    blks = _ready(scan_blocks(ex, state, types, radius=radius, limit=8, skill_name=skill))["blocks"]
    if not blks: raise _env_blocked(msg, pred, f"半径 {radius} 格")
    return blks
def _is_standable(ex: Executor, c: tuple[int, int, int]) -> bool:
    return _has_floor(ex, c) and is_clear_space(observe_block(ex, [c[0], c[1], c[2]])) and is_clear_space(observe_block(ex, [c[0], c[1] + 1, c[2]]))
def _overlaps(c: tuple[int, int, int], p: Sequence[float]) -> bool:
    px, py, pz = block_cell(p)
    return c[0] == px and c[2] == pz and abs(c[1] - py) <= 1
def _inv_entries(state: State) -> list[dict]:
    inv = state.inventory or {}
    return [r for r in (inv.get("items") or ()) if isinstance(r, dict)] + [inv[k] for k in ("held", "offhand") if isinstance(inv.get(k), dict)]
def _pick_melee(state: State, *, allow_trident: bool = False) -> str | None:
    return next((w for w in (("trident", *_MELEE) if allow_trident else _MELEE) if state.count(w) >= 1), None) or next(
        (n for r in _inv_entries(state) if (n := bare(r.get("name"))) and state.count(n) >= 1 and (n.endswith(("_sword", "_axe")) or n == "mace" or (allow_trident and n == "trident"))), None)
def _has_bow(state: State) -> bool: return state.count("bow") >= 1 and any(state.count(a) >= 1 for a in _ARROWS)
def _pick_boat(state: State) -> str | None:
    return next((b for b in _BOATS if state.count(b) >= 1), None) or next(
        (n for r in _inv_entries(state) if (n := bare(r.get("name"))) and state.count(n) >= 1 and n.endswith(("_boat", "_raft")) and "chest" not in n), None)
def _pick_allay_item(state: State) -> str | None:
    return next((i for i in _ALLAY_PREF if state.count(i) >= 2), None) or next(
        (n for r in (state.inventory or {}).get("items") or () if isinstance(r, dict) and (n := bare(r.get("name"))) and state.count(n) >= 2), None)
def _has_silk_val(obj: object) -> bool:
    if isinstance(obj, str): return bare(obj) == "silk_touch"
    if isinstance(obj, Mapping):
        if bare(obj.get("name") or obj.get("id")) == "silk_touch":
            lvl = obj.get("level", obj.get("lvl", 1))
            return isinstance(lvl, int) and not isinstance(lvl, bool) and lvl > 0
        return any((bare(k) == "silk_touch" and ((isinstance(v, int) and not isinstance(v, bool) and v > 0) or v is True))
                   or (bare(k) in ("enchantments", "levels", "stored_enchantments") and _has_silk_val(v)) for k, v in obj.items())
    return isinstance(obj, Sequence) and not isinstance(obj, (bytes, bytearray)) and any(_has_silk_val(el) for el in obj)
def _find_silk_tool(state: State) -> str | None:
    return next((n for r in _inv_entries(state) if (n := bare(r.get("name"))) and state.count(n) >= 1
                 and (_has_silk_val(r.get("enchantments")) or _has_silk_val(r.get("components")))), None)
def _standable_near(ex: Executor, target: Sequence[float], origin: Sequence[float], radius: int = 3, *, blk: tuple[int, int, int] | None = None, reach: float = STATION_EYE_REACH) -> tuple[int, int, int] | None:
    tx, ty, tz = blk if blk is not None else block_cell(target)
    cands = [(tx + dx, ty + dy, tz + dz) for dy in (0, -1, 1, -2, 2) for dx in range(-radius, radius + 1) for dz in range(-radius, radius + 1)
             if (blk is None or not (dx == 0 and dz == 0 and abs(dy) <= 1)) and _is_standable(ex, (tx + dx, ty + dy, tz + dz))
             and (blk is None or can_reach_block(ex, _pt((tx + dx, ty + dy, tz + dz)), blk, reach=reach))]
    return min(cands, key=(lambda c: math.dist(_pt(c), origin)) if blk is not None else (lambda c: (math.dist(_pt(c), target), math.dist(_pt(c), origin)))) if cands else None
def _approach_block(ex: Executor, state: State, target: tuple[int, int, int], *, reach: float = STATION_EYE_REACH) -> Generator[dict, None, dict]:
    st = state.refresh_status(ex.client, max_age_s=0)
    if can_reach_block(ex, st, target, reach=reach) and not _overlaps(target, st["pos"]): return st
    cell = _standable_near(ex, target, st["pos"], 3, blk=target, reach=reach) or _standable_near(ex, target, st["pos"], 3, blk=target, reach=DIG_EYE_REACH)
    if cell is None: raise _env_blocked(f"目标方块 {list(target)} 附近无安全可达站位", "standable_approach", "目标附近 3 格")
    dest = list(_pt(cell)); yield rec("move_to", ex.move_to(pos=dest, rng=1), pos=dest)
    st = state.refresh_status(ex.client, max_age_s=0)
    if eye_to_block_dist(st, target) > DIG_EYE_REACH: raise Replan(f"接近目标方块 {list(target)} 后仍超出眼距")
    return st
def _approach_ent(ex: Executor, state: State, ent: dict, *, reach: float = ENTITY_EYE_REACH) -> Generator[dict, None, dict]:
    st = state.refresh_status(ex.client, max_age_s=0)
    if eye_to_point_dist(st, ent["pos"]) <= reach: return st
    cell = _standable_near(ex, ent["pos"], st["pos"], 2)
    dest = list(_pt(cell)) if cell is not None else [float(v) for v in ent["pos"]]
    yield rec("move_to", ex.move_to(pos=dest, rng=2), pos=dest)
    return state.refresh_status(ex.client, max_age_s=0)
def _find_retreat(ex: Executor, st: dict, away_from: Sequence[float]) -> list[float] | None:
    ox, oy, oz = block_cell(st["pos"])
    cands = sorted(([ox + dx + 0.5, float(oy), oz + dz + 0.5] for dx, dz in _CARDINALS), key=lambda p: math.dist(p, away_from), reverse=True)
    return next((c for c in cands if _retreat_clear(ex, st["pos"], c)), None)
def _check_hp(st: dict, min_hp: float = 10.0) -> None:
    if not st.get("alive", True) or float(st.get("health", 20)) < min_hp: raise Replan(f"血量低于 {min_hp:.0f}，触发止损")

# ---- L2 原语（不注册）
def lure_entity(ex: Executor, state: State, entity_types: str | Sequence[str], bait_item: str, target_pos: Sequence[float], radius: int = 24) -> Generator[dict, None, dict]:
    """手持诱饵接近并沿安全路点将指定实体引诱至 target_pos 附近（4.5 格内）。"""
    bait, tpos = bare(bait_item), tuple(float(v) for v in target_pos)
    refresh_inventory(ex, state)
    if state.count(bait) < 1: raise Replan(f"背包缺少诱饵 {bait}")
    yield from equip_item(ex, state, bait, "hand")
    st = state.refresh_status(ex.client, max_age_s=0)
    ent = min(_req_ents(ex, state, entity_types, radius, f"半径 {radius} 格内未发现可引诱实体", f"visible_entity:{entity_types}"), key=lambda e: eye_to_point_dist(st, e["pos"]))
    if math.dist(ent["pos"], tpos) <= 4.5: return ent
    if math.dist(st["pos"], ent["pos"]) > 6.0:
        yield from _approach_ent(ex, state, ent, reach=5.5)
        ent = next((e for e in _ready(scan_entities(ex, state, entity_types, radius=max(radius, 24)))["entities"] if e["id"] == ent["id"]), None)
        if ent is None: raise Replan("接近后引诱目标实体丢失")
    for _ in range(6):
        if math.dist(ent["pos"], tpos) <= 4.5: return ent
        st = state.refresh_status(ex.client, max_age_s=0)
        dist = math.dist(st["pos"], tpos)
        wp = tuple(a + (b - a) * (1.0 if dist <= 4.0 else 4.0 / dist) for a, b in zip(st["pos"], tpos))
        if (cell := _standable_near(ex, wp, st["pos"], 2)) is None: raise Replan("引诱路径缺少安全可站立路点")
        dest = list(_pt(cell)); yield rec("move_to", ex.move_to(pos=dest, rng=1), pos=dest)
        ent = next((e for e in _ready(scan_entities(ex, state, entity_types, radius=max(radius, 24)))["entities"] if e["id"] == ent["id"]), None)
        if ent is None: raise Replan("引诱途中目标实体丢失")
    if math.dist(ent["pos"], tpos) <= 4.5: return ent
    raise Replan("引诱路点耗尽，实体仍未到达目标位置附近")

# ---- L3 技能基类与 8 项注册技能
class _BiomeSkill(Skill):
    verified, _done_msg = "offline", "未确认群系技能完成"
    def _begin(self, ex: Executor, state: State) -> dict:
        self._done = False; refresh_inventory(ex, state)
        st = state.refresh_status(ex.client, max_age_s=0)
        if reason := self.blocked(state): raise Replan(reason)
        if not self.can_start(state): raise Replan(f"{self.name} 当前参数条件不满足")
        return st
    def run(self, ex: Executor, state: State):
        try: yield from self._run(ex, state)
        finally:
            if any(state.st.get(k) for k in ("sneaking", "blocking", "jumping")): _clear_posture(ex, state, sneak=False, shield=False, jump=False)
    def _run(self, ex: Executor, state: State): raise NotImplementedError
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        return "" if (getattr(self, "_done", False) or (bool(before_counts) and self._delta_done(state, before_counts))) else self._done_msg
    def _delta_done(self, state: State, before: dict) -> bool: return False

@register
class KillNearSculkCatalyst(_BiomeSkill):
    name, goal, description = "kill_near_sculk_catalyst", "t3:sculk_catalyst", "在幽匿催发体 8 格内击杀生物触发幽匿蔓延"
    params = {"radius": Param(int, "搜索幽匿催发体半径", default=24, min=8, max=64)}
    _done_msg = "未确认在幽匿催发体 8 格内完成击杀"
    @classmethod
    def blocked(cls, state: State) -> str: return _gate(state, state.hp >= 10 and (_pick_melee(state) is not None or _has_bow(state)), "生命值不足 10 或缺少近战武器/弓箭")
    def _run(self, ex: Executor, state: State):
        st, radius = self._begin(ex, state), self.args["radius"]
        cats = _req_blks(ex, state, "sculk_catalyst", radius, f"半径 {radius} 格内未发现幽匿催发体", "visible_block:sculk_catalyst", self.name)
        ccenter = block_center(min(cats, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
        mobs = [e for e in _ready(scan_entities(ex, state, None, radius=radius))["entities"]
                if e.get("type") in ("hostile", "animal", "other") and bare(e.get("name")) not in _NON_KILLABLE and math.dist(e["pos"], ccenter) <= 8.0]
        if not mobs: raise _env_blocked("幽匿催发体 8 格内未发现可击杀生物", "mob_near:sculk_catalyst", "催发体 8 格内")
        target = min(mobs, key=lambda e: (0 if e.get("type") == "hostile" else 1, math.dist(e["pos"], ccenter)))
        if (weapon := _pick_melee(state)) is not None:
            yield from equip_item(ex, state, weapon, "hand")
            st = yield from _approach_ent(ex, state, target, reach=ENTITY_EYE_REACH); _check_hp(st)
            fresh = next((e for e in _ready(scan_entities(ex, state, bare(target["name"]), radius=radius))["entities"] if e["id"] == target["id"]), None)
            if fresh is None or math.dist(fresh["pos"], ccenter) > 8.0: raise Replan("目标生物已离开幽匿催发体 8 格范围")
            if (retreat := _find_retreat(ex, st, fresh["pos"])) is None: raise Replan("催发体附近缺少同高安全退路")
            yield from combat_melee(ex, state, fresh, retreat_pos=retreat, radius=min(16, radius))
        else:
            if (retreat := _find_retreat(ex, st, target["pos"])) is None: raise Replan("远程击杀缺少同高安全退路")
            yield from shoot_projectile(ex, state, entity=target, weapon="bow", retreat_pos=retreat, max_distance=min(32, radius))
        self._done = True

@register
class AssistAxolotlHunt(_BiomeSkill):
    name, goal, description = "assist_axolotl_hunt", "t3:axolotl_hunt", "协助美西螈击杀水生猎物触发并肩作战"
    params = {"radius": Param(int, "搜索美西螈与猎物半径", default=24, min=8, max=64)}
    _done_msg = "未确认协助美西螈击杀水生猎物"
    @classmethod
    def blocked(cls, state: State) -> str: return _gate(state, state.hp >= 10 and _pick_melee(state, allow_trident=True) is not None, "生命值不足 10 或缺少近战武器（剑/斧/三叉戟/重锤）")
    def _run(self, ex: Executor, state: State):
        st, radius = self._begin(ex, state), self.args["radius"]
        axos = _ready(scan_entities(ex, state, "axolotl", radius=radius))["entities"]
        if not axos and state.count("axolotl_bucket") >= 1:
            if waters := _ready(scan_blocks(ex, state, "water", radius=min(radius, 16), limit=6, skill_name=self.name))["blocks"]:
                wpos = block_cell(min(waters, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
                yield from _approach_block(ex, state, wpos, reach=STATION_EYE_REACH)
                yield from use_item_on(ex, state, wpos, "axolotl_bucket")
                axos = _ready(scan_entities(ex, state, "axolotl", radius=radius))["entities"]
        if not axos: raise _env_blocked(f"半径 {radius} 格内未发现美西螈", "visible_entity:axolotl", f"半径 {radius} 格实体")
        axo = min(axos, key=lambda e: eye_to_point_dist(st, e["pos"]))
        preys = [e for e in _ready(scan_entities(ex, state, _AQUATIC_PREY, radius=radius))["entities"] if math.dist(e["pos"], axo["pos"]) <= 16.0]
        if not preys: raise _env_blocked("美西螈 16 格内未发现水生猎物", "visible_entity:aquatic_prey", "美西螈 16 格内")
        prey = min(preys, key=lambda e: math.dist(e["pos"], axo["pos"]))
        yield from equip_item(ex, state, _pick_melee(state, allow_trident=True), "hand")
        st = yield from _approach_ent(ex, state, prey, reach=ENTITY_EYE_REACH); _check_hp(st)
        if (retreat := _find_retreat(ex, st, prey["pos"])) is not None and _grounded(ex, st):
            yield from combat_melee(ex, state, prey, retreat_pos=retreat, radius=min(16, radius))
        else:
            receipt = ex.attack(prey["id"], timeout_ms=8000, stop_on=("hurt", "death"))
            yield rec("attack", receipt, entity_id=prey["id"])
            if receipt.get("killed") is not True: raise Replan("水生协助攻击未确认击杀猎物")
        self._done = True

@register
class ManageWolfArmor(_BiomeSkill):
    name, goal, description = "manage_wolf_armor", "t3:wolf_armor_{mode}", "用剪刀脱下狼铠或用犰狳鳞甲修复狼铠"
    params = {"mode": Param(str, "操作模式", default="repair", choices=("remove", "repair")), "radius": Param(int, "搜索驯服狼半径", default=24, min=8, max=64)}
    _done_msg = "未确认脱下或修复狼铠"
    @classmethod
    def blocked(cls, state: State) -> str: return _gate(state, state.count("shears") >= 1 or state.count("armadillo_scute") >= 1, "背包缺少剪刀或犰狳鳞甲")
    def can_start(self, state: State) -> bool: return not self.blocked(state) and state.count("shears" if self.args["mode"] == "remove" else "armadillo_scute") >= 1
    def _delta_done(self, state: State, before: dict) -> bool:
        return state.count("wolf_armor") > before.get("wolf_armor", 0) if self.args["mode"] == "remove" else state.count("armadillo_scute") < before.get("armadillo_scute", 0)
    def _run(self, ex: Executor, state: State):
        st, mode, radius = self._begin(ex, state), self.args["mode"], self.args["radius"]
        wolves = _req_ents(ex, state, "wolf", radius, f"半径 {radius} 格内未发现狼", "visible_entity:wolf")
        wolf = min(wolves, key=lambda e: (0 if e.get("tamed") else 1, 0 if e.get("has_armor") or e.get("armor") or e.get("wolf_armor") else 1, eye_to_point_dist(st, e["pos"])))
        yield from _approach_ent(ex, state, wolf, reach=ENTITY_EYE_REACH)
        before_wa, before_sc = state.count("wolf_armor"), state.count("armadillo_scute")
        ev = yield from use_item_on(ex, state, wolf["id"], "shears" if mode == "remove" else "armadillo_scute")
        if not ((state.count("wolf_armor") > before_wa) if mode == "remove" else (state.count("armadillo_scute") < before_sc or ev["receipt"].get("changed") is True)):
            raise Replan(f"狼铠操作 {mode} 未确认生效")
        self._done = True

@register
class AllayDeliverItem(_BiomeSkill):
    name, goal, description = "allay_deliver_item", "t3:allay_deliver", "给悦灵物品并丢出同类物品等待其捡回交付"
    params = {"radius": Param(int, "搜索悦灵半径", default=24, min=8, max=64)}
    _done_msg = "未确认悦灵交付同类物品"
    @classmethod
    def blocked(cls, state: State) -> str: return _gate(state, _pick_allay_item(state) is not None, "背包缺少数量 >= 2 的可丢弃同类物品")
    def _run(self, ex: Executor, state: State):
        st, radius = self._begin(ex, state), self.args["radius"]
        allay = min(_req_ents(ex, state, "allay", radius, f"半径 {radius} 格内未发现悦灵", "visible_entity:allay"), key=lambda e: eye_to_point_dist(st, e["pos"]))
        if (item := _pick_allay_item(state)) is None: raise Replan("背包缺少数量 >= 2 的可交付物品")
        yield from _approach_ent(ex, state, allay, reach=ENTITY_EYE_REACH)
        before_hand = state.count(item)
        yield from use_item_on(ex, state, allay["id"], item)
        if (after_hand := state.count(item)) >= before_hand: raise Replan(f"交予悦灵 {item} 后未确认背包数量减少")
        receipt = ex.drop(item, 1); yield rec("drop", receipt, item=item, count=1)
        delivered = bool(receipt.get("delivered"))
        for _ in range(6):
            state.refresh_status(ex.client, max_age_s=0); refresh_inventory(ex, state)
            if state.count(item) >= after_hand: delivered = True; break
        if not delivered: raise Replan(f"等待悦灵捡回并交付 {item} 未在轮询窗口内确认")
        self._done = True

@register
class BoatWithGoat(_BiomeSkill):
    name, goal, description = "boat_with_goat", "t3:boat_goat", "在山羊旁放船引诱上船并同乘后安全下船"
    params = {"radius": Param(int, "搜索山羊半径", default=24, min=8, max=64)}
    _done_msg = "未确认与山羊同乘船并安全下船"
    @classmethod
    def blocked(cls, state: State) -> str: return _gate(state, _pick_boat(state) is not None, "背包缺少船或竹筏（*_boat / *_raft）")
    def _find_boat_cell(self, ex: Executor, gpos: Sequence[float], ppos: Sequence[float]) -> tuple[int, int, int] | None:
        gx, gy, gz = block_cell(gpos)
        cands = [(gx + dx, gy + dy, gz + dz) for dy in (0, -1, 1) for dx in range(-2, 3) for dz in range(-2, 3)
                 if not _overlaps((gx + dx, gy + dy, gz + dz), ppos) and _is_air(ex, (gx + dx, gy + dy, gz + dz)) and _is_air(ex, (gx + dx, gy + dy + 1, gz + dz))
                 and (_has_floor(ex, (gx + dx, gy + dy, gz + dz)) or bare(observe_block(ex, [gx + dx, gy + dy - 1, gz + dz]).get("name")) == "water")]
        return min(cands, key=lambda c: math.dist(_pt(c), gpos)) if cands else None
    def _run(self, ex: Executor, state: State):
        st, radius = self._begin(ex, state), self.args["radius"]
        goat = min(_req_ents(ex, state, "goat", radius, f"半径 {radius} 格内未发现山羊", "visible_entity:goat"), key=lambda e: eye_to_point_dist(st, e["pos"]))
        st = yield from _approach_ent(ex, state, goat, reach=5.0)
        if (bcell := self._find_boat_cell(ex, goat["pos"], st["pos"])) is None: raise _env_blocked("山羊附近缺少放置船的平坦空位", "clear_site:boat", "山羊周边 2 格")
        floor_cell, boat_pos = (bcell[0], bcell[1] - 1, bcell[2]), _pt(bcell)
        yield from _approach_block(ex, state, floor_cell, reach=STATION_EYE_REACH)
        yield from use_item_on(ex, state, floor_cell, _pick_boat(state))
        if state.count("wheat") >= 1: yield from lure_entity(ex, state, ("goat",), "wheat", boat_pos, radius=radius)
        boats = [e for e in _ready(scan_entities(ex, state, None, radius=min(32, radius)))["entities"]
                 if (n := bare(e.get("name"))) in ("boat", "chest_boat", "raft") or n.endswith(("_boat", "_raft"))]
        if not boats: raise Replan("放置船后未扫描到船实体")
        yield from _approach_ent(ex, state, (boat_ent := min(boats, key=lambda e: math.dist(e["pos"], boat_pos))), reach=ENTITY_EYE_REACH)
        riding = True
        try:
            ev = yield from use_item_on(ex, state, boat_ent["id"])
            if not (bool(ev["after"]["status"].get("in_vehicle")) or ev["receipt"].get("changed") is True or ev["receipt"].get("mounted") is True):
                riding = False; raise Replan("右键船后未确认进入载具")
            yield rec("dismount", ex.dismount()); riding = False
        finally:
            if riding and state.st.get("alive") is not False:
                try:
                    if state.refresh_status(ex.client, max_age_s=0).get("in_vehicle"): ex.dismount()
                except (ApiError, Replan):
                    if sys.exc_info()[0] is None: raise
        self._done = True

@register
class SilkTouchBeeNest(_BiomeSkill):
    """保守策略：API `/get_block` 不暴露蜂巢内部蜜蜂数，优先夜间/雨天（蜜蜂归巢）或巢旁可见蜜蜂时精准采集，可选放营火。"""
    name, goal, description = "silk_touch_bee_nest", "t3:silk_nest", "夜间或蜜蜂归巢时用精准采集工具完整挖取蜂巢"
    params = {"radius": Param(int, "搜索蜂巢半径", default=24, min=8, max=64)}
    _done_msg = "未确认精准采集获得蜂巢"
    @classmethod
    def blocked(cls, state: State) -> str: return _gate(state, _find_silk_tool(state) is not None, "背包缺少带精准采集（silk_touch）附魔的工具")
    def _delta_done(self, state: State, before: dict) -> bool: return state.count("bee_nest") > before.get("bee_nest", 0)
    def _is_night_or_rain(self, st: dict) -> bool:
        rain = bool(st.get("is_raining") or st.get("raining") or str(st.get("weather") or "").lower() in ("rain", "thunder"))
        tod = st.get("time_of_day", st.get("time"))
        return rain or (isinstance(tod, (int, float)) and not isinstance(tod, bool) and (12500 <= int(tod) % 24000 <= 23500))
    def _run(self, ex: Executor, state: State):
        st, radius = self._begin(ex, state), self.args["radius"]
        nests = _req_blks(ex, state, "bee_nest", radius, f"半径 {radius} 格内未发现自然蜂巢", "visible_block:bee_nest", self.name)
        npos = block_cell(min(nests, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
        # API /get_block 不暴露蜂巢内部蜜蜂数：保守校验夜间/雨天归巢，或巢旁可见蜜蜂（黄昏前活跃蜂巢）
        if not self._is_night_or_rain(st):
            if not any(math.dist(e["pos"], block_center(npos)) <= 16.0 for e in _ready(scan_entities(ex, state, "bee", radius=radius))["entities"]):
                raise _env_blocked("非夜间/雨天且蜂巢旁未见蜜蜂，保守等待入夜归巢再挖", "bee_nest_occupied:night_or_rain", f"半径 {radius} 格", wake_on="night")
        st = yield from _approach_block(ex, state, npos, reach=DIG_EYE_REACH)
        nx, ny, nz = npos
        if state.count("campfire") >= 1 and not any(bare(observe_block(ex, [nx, ny - d, nz]).get("name")) in ("campfire", "soul_campfire") for d in (1, 2, 3)):
            cpos = next(((nx, ny - d, nz) for d in (1, 2, 3) if _is_air(ex, (nx, ny - d, nz)) and _has_floor(ex, (nx, ny - d, nz)) and not _overlaps((nx, ny - d, nz), st["pos"]) and can_reach_block(ex, st, (nx, ny - d, nz), reach=DIG_EYE_REACH)), None)
            if cpos is not None: yield from place_block(ex, state, "campfire", cpos)
        yield from equip_item(ex, state, _find_silk_tool(state), "hand")
        yield from _approach_block(ex, state, npos, reach=DIG_EYE_REACH)
        before_n, receipt = state.count("bee_nest"), ex.dig(list(npos), collect=True)
        voxel_cache_for(state).invalidate(npos, dimension=_dim(state)); yield rec("dig", receipt, pos=list(npos))
        refresh_inventory(ex, state)
        if state.count("bee_nest") <= before_n: raise Replan("精准采集蜂巢后背包未确认获得 bee_nest")
        self._done = True

@register
class CatchTadpole(_BiomeSkill):
    name, goal, description = "catch_tadpole", "t3:tadpole_bucket", "靠近蝌蚪并用水桶将其装入蝌蚪桶"
    params = {"radius": Param(int, "搜索蝌蚪半径", default=24, min=8, max=64)}
    _done_msg = "未确认捕获蝌蚪装桶"
    @classmethod
    def blocked(cls, state: State) -> str: return _gate(state, state.count("water_bucket") >= 1, "背包缺少水桶（water_bucket）")
    def _delta_done(self, state: State, before: dict) -> bool: return state.count("tadpole_bucket") > before.get("tadpole_bucket", 0)
    def _run(self, ex: Executor, state: State):
        st, radius = self._begin(ex, state), self.args["radius"]
        tad = min(_req_ents(ex, state, "tadpole", radius, f"半径 {radius} 格内未发现蝌蚪", "visible_entity:tadpole"), key=lambda e: eye_to_point_dist(st, e["pos"]))
        yield from _approach_ent(ex, state, tad, reach=ENTITY_EYE_REACH)
        before_tb, before_wb = state.count("tadpole_bucket"), state.count("water_bucket")
        ev = yield from use_item_on(ex, state, tad["id"], "water_bucket")
        if state.count("tadpole_bucket") <= before_tb and not (state.count("water_bucket") < before_wb and ev["receipt"].get("changed") is True):
            raise Replan("捕捞蝌蚪后未确认获得蝌蚪桶")
        self._done = True

@register
class SpyglassObserve(_BiomeSkill):
    name, goal, description = "spyglass_observe", "t3:spyglass_{target}", "使用望远镜瞄准并观察恶魂或鹦鹉"
    params = {"target": Param(str, "观察目标", default="parrot", choices=("ghast", "parrot")), "radius": Param(int, "搜索目标半径", default=48, min=8, max=64)}
    _done_msg = "未确认望远镜观察目标生物"
    @classmethod
    def blocked(cls, state: State) -> str: return _gate(state, state.count("spyglass") >= 1, "背包缺少望远镜（spyglass）")
    def _run(self, ex: Executor, state: State):
        st, target, radius = self._begin(ex, state), self.args["target"], self.args["radius"]
        ents = _req_ents(ex, state, target, radius, f"半径 {radius} 格内未发现 {target}", f"visible_entity:{target}")
        visible = [e for e in ents if can_see_block(ex, st, e["pos"])]
        if not visible: raise _env_blocked(f"半径 {radius} 格内 {target} 视线均被遮挡", f"visible_los:{target}", f"半径 {radius} 格视线")
        ent = min(visible, key=lambda e: eye_to_block_dist(st, e["pos"]))
        yield rec("look", ex.look(entity_id=ent["id"]), entity_id=ent["id"])
        receipt = ex.use_item("spyglass", target={"entity_id": ent["id"]}, ms=600)
        yield rec("use_item", receipt, item="spyglass", target={"entity_id": ent["id"]}, ms=600)
        if isinstance(receipt, dict) and receipt.get("ok") is False: raise Replan(f"望远镜观察 {target} 返回失败回执")
        self._done = True
