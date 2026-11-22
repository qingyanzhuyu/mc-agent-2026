"""Stage 2 Wave 3 群系 T3 技能：1 项 L2 引诱原语 + 8 项 L3 注册技能（+270 pts）。"""
from __future__ import annotations

import math, sys
from collections.abc import Generator, Mapping, Sequence

from ..client import ApiError
from ..runtime import EnvBlocked, Executor, Replan, State, has_equipped_armor
from .base import Param, Skill, rec
from .encounter import _ARROWS, _retreat_clear, combat_melee, shoot_projectile
from .field import _grounded, equip_item, place_block, scan_blocks, scan_entities
from .interaction import _clear_posture, use_item_on
from .items import AIR, bare, is_clear_space, is_safe_solid_floor
from .landmark_nav import goto_known_landmark_or_biome
from .observations import observe_block, refresh_inventory
from .registry import register
from .sightline import (DIG_EYE_REACH, ENTITY_EYE_REACH, STATION_EYE_REACH, block_cell,
                        block_center, can_reach_block, can_see_block, eye_to_block_dist, eye_to_point_dist)
from .voxel_cache import voxel_cache_for
from .workstations import find_known_station, reclaim_field_station, record_placed_station

_CARDINALS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_MELEE = ("netherite_sword", "diamond_sword", "iron_sword", "stone_sword", "netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "mace", "wooden_sword")
_AQUATIC_PREY = ("drowned", "guardian", "elder_guardian", "squid", "glow_squid", "cod", "salmon", "pufferfish", "tropical_fish", "tadpole")
_BOAT_WOODS = ("oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "mangrove", "cherry", "pale_oak")
_BOATS = tuple(f"{w}_boat" for w in _BOAT_WOODS) + ("bamboo_raft",)
_ALLAY_PREF = ("cobblestone", "dirt", "oak_planks", "stick", "apple", "cookie")
_NON_KILLABLE = frozenset({"player", "item", "experience_orb", "boat", "chest_boat", "minecart", "armor_stand", "allay", "villager", "iron_golem", "wolf", "cat", "axolotl"})

def _dim(state: State) -> str: return str((state.st or {}).get("dimension") or "overworld")
def _pt(c: tuple[int, int, int]) -> tuple[float, float, float]: return (c[0] + 0.5, float(c[1]), c[2] + 0.5)
def _is_air(ex: Executor, pos: Sequence[int]) -> bool: return bare(observe_block(ex, list(pos)).get("name")) in AIR
def _has_floor(ex: Executor, c: tuple[int, int, int]) -> bool: return is_safe_solid_floor(observe_block(ex, [c[0], c[1] - 1, c[2]]))
def _gate(state: State, ok: bool, msg: str) -> str:
    if not state.ready:
        return "世界尚未就绪（ready=false）"
    hp = (state.st or {}).get("health")
    hp_crit = float(getattr(getattr(state, "config", None), "hp_crit", 6.0) or 6.0)
    if isinstance(hp, (int, float)) and not isinstance(hp, bool) and float(hp) <= hp_crit:
        return f"残血危急(hp={hp:g}<={hp_crit:g})，暂停高风险群系/结构探索优先回血"
    return "" if ok else msg
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
def _req_ents_or_landmark(ex: Executor, state: State, types, radius: int, msg: str, pred: str, landmark: str) -> Generator[dict, None, list[dict]]:
    try: return _req_ents(ex, state, types, radius, msg, pred)
    except EnvBlocked:
        if not (yield from goto_known_landmark_or_biome(ex, state, landmark)): raise
        return _req_ents(ex, state, types, radius, msg, pred)
def _req_blks_or_landmark(ex: Executor, state: State, types, radius: int, msg: str, pred: str, landmark: str, skill: str = "") -> Generator[dict, None, list[dict]]:
    try: return _req_blks(ex, state, types, radius, msg, pred, skill=skill)
    except EnvBlocked:
        if not (yield from goto_known_landmark_or_biome(ex, state, landmark)): raise
        return _req_blks(ex, state, types, radius, msg, pred, skill=skill)
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
def _wood_log_count(state: State, wood: str) -> int:
    return sum(state.count(n) for n in (f"{wood}_log", f"{wood}_wood", f"stripped_{wood}_log", f"stripped_{wood}_wood"))
def _boat_craft_plan(state: State) -> tuple[str, str] | None:
    for wood in _BOAT_WOODS:
        if state.count(f"{wood}_planks") >= 5:
            return (f"{wood}_boat", f"{wood}_planks")
    if state.count("bamboo_planks") >= 5:
        return ("bamboo_raft", "bamboo_planks")
    for wood in _BOAT_WOODS:
        if state.count(f"{wood}_planks") + _wood_log_count(state, wood) * 4 >= 5:
            return (f"{wood}_boat", f"{wood}_planks")
    if state.count("bamboo_planks") + (state.count("bamboo_block") + state.count("stripped_bamboo_block")) * 2 >= 5:
        return ("bamboo_raft", "bamboo_planks")
    return None
def _can_craft_shears(state: State) -> bool: return state.count("iron_ingot") >= 2
def _can_craft_wolf_armor(state: State) -> bool: return state.count("armadillo_scute") >= 6
def _can_craft_spyglass(state: State) -> bool: return state.count("copper_ingot") >= 2 and state.count("amethyst_shard") >= 1
def _wolf_has_armor(wolf: Mapping) -> bool: return bool(wolf.get("has_armor") or wolf.get("armor") or wolf.get("wolf_armor"))
def _wolf_armor_full_durability(wolf: Mapping) -> bool:
    if wolf.get("armor_damaged") is False: return True
    if "armor_damage" in wolf and wolf.get("armor_damage") == 0: return True
    dur = wolf.get("armor_durability")
    if isinstance(dur, Sequence) and not isinstance(dur, (str, bytes)) and len(dur) >= 2 and isinstance(dur[0], (int, float)) and isinstance(dur[1], (int, float)):
        return dur[0] >= dur[1]
    if isinstance(dur, Mapping):
        cur, mx = dur.get("current", dur.get("cur")), dur.get("max")
        if isinstance(cur, (int, float)) and isinstance(mx, (int, float)): return cur >= mx
    wa = wolf.get("wolf_armor") if isinstance(wolf.get("wolf_armor"), Mapping) else (wolf.get("armor") if isinstance(wolf.get("armor"), Mapping) else None)
    if isinstance(wa, Mapping):
        if wa.get("damaged") is False: return True
        if "damage" in wa and wa.get("damage") == 0: return True
        wdur = wa.get("durability")
        if isinstance(wdur, Sequence) and not isinstance(wdur, (str, bytes)) and len(wdur) >= 2 and isinstance(wdur[0], (int, float)) and isinstance(wdur[1], (int, float)):
            return wdur[0] >= wdur[1]
    return False
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
                   or (bare(k) in ("enchantments", "levels") and _has_silk_val(v)) for k, v in obj.items())
    return isinstance(obj, Sequence) and not isinstance(obj, (bytes, bytearray)) and any(_has_silk_val(el) for el in obj)
def _find_silk_tool(state: State) -> str | None:
    return next((n for r in _inv_entries(state) if (n := bare(r.get("name"))) and n not in ("book", "enchanted_book")
                 and state.count(n) >= 1 and (_has_silk_val(r.get("enchantments")) or _has_silk_val(r.get("components")))), None)
def _find_place_cell(ex: Executor, st: dict, radius: int = 3) -> tuple[int, int, int] | None:
    ox, oy, oz = block_cell(st["pos"])
    cands = [(ox + dx, oy + dy, oz + dz) for dy in (0, -1, 1) for dx in range(-radius, radius + 1) for dz in range(-radius, radius + 1)
             if not _overlaps((ox + dx, oy + dy, oz + dz), st["pos"])
             and _is_air(ex, (ox + dx, oy + dy, oz + dz)) and _has_floor(ex, (ox + dx, oy + dy, oz + dz))
             and can_reach_block(ex, st, (ox + dx, oy + dy, oz + dz), reach=DIG_EYE_REACH)]
    return min(cands, key=lambda c: eye_to_block_dist(st, c)) if cands else None
def _craft_item(ex: Executor, state: State, item: str, *, need_table: bool = True, err_msg: str | None = None) -> Generator[dict, None, None]:
    tpos: tuple[int, int, int] | None = None
    newly_placed = False
    if need_table:
        st = state.refresh_status(ex.client, max_age_s=0)
        tables = _ready(scan_blocks(ex, state, "crafting_table", radius=16, limit=5))["blocks"]
        if tables:
            tcell = block_cell(min(tables, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
            record_placed_station(state, "crafting_table", tcell, dimension=_dim(state))
            if not can_reach_block(ex, st, tcell, reach=STATION_EYE_REACH):
                yield from _approach_block(ex, state, tcell, reach=STATION_EYE_REACH)
        elif (known := find_known_station(ex, state, "crafting_table", max_dist=16.0)) is not None:
            if not can_reach_block(ex, st, known, reach=STATION_EYE_REACH):
                yield from _approach_block(ex, state, known, reach=STATION_EYE_REACH)
        elif state.count("crafting_table") >= 1:
            if (pcell := _find_place_cell(ex, st)) is not None:
                yield from place_block(ex, state, "crafting_table", pcell)
                tpos = pcell
                newly_placed = True
    before = state.count(item)
    yield rec("craft", ex.craft(item, 1), item=item, count=1)
    refresh_inventory(ex, state)
    if state.count(item) <= before:
        raise Replan(err_msg or f"合成 {item} 后背包数量未增加")
    if tpos is not None and newly_placed:
        yield from reclaim_field_station(ex, state, "crafting_table", tpos, newly_placed=newly_placed)
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
    if eye_to_point_dist(st, ent["pos"]) > 6.0:
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
    verified = "live"
    params = {"radius": Param(int, "搜索幽匿催发体半径", default=24, min=8, max=64)}
    _done_msg = "未确认在幽匿催发体 8 格内完成击杀"
    @classmethod
    def blocked(cls, state: State) -> str: return _gate(state, state.hp >= 10 and (_pick_melee(state) is not None or _has_bow(state)), "生命值不足 10 或缺少近战武器/弓箭")
    def _run(self, ex: Executor, state: State):
        st, radius = self._begin(ex, state), self.args["radius"]
        cats = yield from _req_blks_or_landmark(ex, state, "sculk_catalyst", radius, f"半径 {radius} 格内未发现幽匿催发体", "visible_block:sculk_catalyst", "deep_dark", self.name)
        st = state.refresh_status(ex.client, max_age_s=0)
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
    verified = "live"
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
        if not axos and (yield from goto_known_landmark_or_biome(ex, state, "lush_caves")):
            st = state.refresh_status(ex.client, max_age_s=0)
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
        if (weapon := _pick_melee(state, allow_trident=True)) is None: raise Replan("背包缺少近战武器")
        yield from equip_item(ex, state, weapon, "hand")
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
    name, goal, description = "manage_wolf_armor", "t3:wolf_armor_{mode}", "备齐剪刀或狼铠后脱下狼铠或用犰狳鳞甲修复狼铠"
    verified = "live"
    params = {"mode": Param(str, "操作模式", default="repair", choices=("remove", "repair")), "radius": Param(int, "搜索驯服狼半径", default=24, min=8, max=64)}
    _done_msg = "未确认脱下或修复狼铠"
    @classmethod
    def blocked(cls, state: State) -> str:
        ok = state.count("shears") >= 1 or _can_craft_shears(state) or state.count("armadillo_scute") >= 1
        return _gate(state, ok, "背包缺少剪刀（或 2 个铁锭合成剪刀）或犰狳鳞甲")
    def can_start(self, state: State) -> bool:
        if self.blocked(state): return False
        return (state.count("shears") >= 1 or _can_craft_shears(state)) if self.args["mode"] == "remove" else state.count("armadillo_scute") >= 1
    def _delta_done(self, state: State, before: dict) -> bool:
        return state.count("wolf_armor") > before.get("wolf_armor", 0) if self.args["mode"] == "remove" else state.count("armadillo_scute") < before.get("armadillo_scute", 0)
    def _run(self, ex: Executor, state: State):
        st, mode, radius = self._begin(ex, state), self.args["mode"], self.args["radius"]
        wolves = yield from _req_ents_or_landmark(ex, state, "wolf", radius, f"半径 {radius} 格内未发现狼", "visible_entity:wolf", "savanna|badlands")
        st = state.refresh_status(ex.client, max_age_s=0)
        wolf = min(wolves, key=lambda e: (0 if e.get("tamed") else 1, 0 if _wolf_has_armor(e) else 1, eye_to_point_dist(st, e["pos"])))
        if mode == "remove":
            if state.count("shears") < 1:
                if not _can_craft_shears(state): raise Replan("背包缺少剪刀且铁锭不足 2 个，无法合成剪刀")
                yield from _craft_item(ex, state, "shears", need_table=False, err_msg="用铁锭合成剪刀后背包未确认增加 shears")
            if not _wolf_has_armor(wolf):
                if state.count("wolf_armor") < 1 and _can_craft_wolf_armor(state):
                    yield from _craft_item(ex, state, "wolf_armor", need_table=True, err_msg="用犰狳鳞甲合成狼铠后背包未确认增加 wolf_armor")
                if state.count("wolf_armor") >= 1:
                    yield from _approach_ent(ex, state, wolf, reach=ENTITY_EYE_REACH)
                    before_put = state.count("wolf_armor")
                    ev_put = yield from use_item_on(ex, state, wolf["id"], "wolf_armor")
                    if state.count("wolf_armor") >= before_put and ev_put["receipt"].get("changed") is not True:
                        raise Replan("给驯服狼穿戴狼铠后未确认生效")
                elif "has_armor" in wolf and wolf.get("has_armor") is False:
                    raise Replan("驯服狼尚未穿戴狼铠，且背包缺少 wolf_armor 或 6 个 armadillo_scute 合成狼铠")
            yield from _approach_ent(ex, state, wolf, reach=ENTITY_EYE_REACH)
            before_wa = state.count("wolf_armor")
            yield from use_item_on(ex, state, wolf["id"], "shears")
            if state.count("wolf_armor") <= before_wa:
                raise Replan("狼铠操作 remove 未确认生效")
        else:
            if "has_armor" in wolf and not _wolf_has_armor(wolf):
                raise _env_blocked("驯服狼尚未穿戴狼铠，无法执行狼铠修复", "wolf_armor:damaged", f"半径 {radius} 格实体", wake_on="chunk_moved")
            if _wolf_armor_full_durability(wolf):
                raise _env_blocked("驯服狼所穿狼铠当前满耐久，无法消耗犰狳鳞甲修复", "wolf_armor:damaged", f"半径 {radius} 格实体", wake_on="chunk_moved")
            yield from _approach_ent(ex, state, wolf, reach=ENTITY_EYE_REACH)
            before_sc = state.count("armadillo_scute")
            ev = yield from use_item_on(ex, state, wolf["id"], "armadillo_scute")
            if state.count("armadillo_scute") >= before_sc and ev["receipt"].get("changed") is not True:
                raise _env_blocked("使用犰狳鳞甲修复狼铠未消耗（狼铠可能满耐久或未穿戴受损狼铠），挂起等待受损后重试", "wolf_armor:damaged", f"半径 {radius} 格实体", wake_on="chunk_moved")
        self._done = True

@register
class AllayDeliverItem(_BiomeSkill):
    name, goal, description = "allay_deliver_item", "t3:allay_deliver", "给悦灵物品并丢出同类物品等待其捡回交付"
    verified = "live"
    params = {"radius": Param(int, "搜索悦灵半径", default=24, min=8, max=64)}
    _done_msg = "未确认悦灵交付同类物品"
    @classmethod
    def blocked(cls, state: State) -> str:
        base = _gate(state, _pick_allay_item(state) is not None, "背包缺少数量 >= 2 的可丢弃同类物品")
        if base:
            return base
        hp = (state.st or {}).get("health")
        if isinstance(hp, (int, float)) and not isinstance(hp, bool) and float(hp) <= 10.0 and not has_equipped_armor(state):
            return f"无甲低血(hp={hp:g}<=10)，禁止前往掠夺者前哨站寻找悦灵"
        return ""
    def _run(self, ex: Executor, state: State):
        st, radius = self._begin(ex, state), self.args["radius"]
        allays = yield from _req_ents_or_landmark(ex, state, "allay", radius, f"半径 {radius} 格内未发现悦灵", "visible_entity:allay", "pillager_outpost")
        st = state.refresh_status(ex.client, max_age_s=0)
        allay = min(allays, key=lambda e: eye_to_point_dist(st, e["pos"]))
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
    name, goal, description = "boat_with_goat", "t3:boat_goat", "备齐或合成木船并在山羊旁放船引诱上船同乘后下船"
    verified = "live"
    params = {"radius": Param(int, "搜索山羊半径", default=24, min=8, max=64)}
    _done_msg = "未确认与山羊同乘船并安全下船"
    @classmethod
    def blocked(cls, state: State) -> str:
        return _gate(state, _pick_boat(state) is not None or _boat_craft_plan(state) is not None,
                     "背包缺少船/竹筏，且无足够同种木板（>=5）或原木（>=2）合成木船")
    def _ensure_boat(self, ex: Executor, state: State) -> Generator[dict, None, str]:
        if (boat := _pick_boat(state)) is not None: return boat
        if (plan := _boat_craft_plan(state)) is None:
            raise Replan("背包缺少船且同种木板/原木不足以合成木船")
        boat_item, planks_item = plan
        while state.count(planks_item) < 5:
            yield from _craft_item(ex, state, planks_item, need_table=False, err_msg=f"合成木板 {planks_item} 后背包数量未增加")
        yield from _craft_item(ex, state, boat_item, need_table=True, err_msg=f"合成木船 {boat_item} 后背包数量未增加")
        if (boat := _pick_boat(state)) is None: raise Replan(f"合成 {boat_item} 后背包仍未找到可用木船")
        return boat
    def _find_boat_cell(self, ex: Executor, gpos: Sequence[float], ppos: Sequence[float]) -> tuple[int, int, int] | None:
        gx, gy, gz = block_cell(gpos)
        cands = [(gx + dx, gy + dy, gz + dz) for dy in (0, -1, 1) for dx in range(-2, 3) for dz in range(-2, 3)
                 if not _overlaps((gx + dx, gy + dy, gz + dz), ppos) and _is_air(ex, (gx + dx, gy + dy, gz + dz)) and _is_air(ex, (gx + dx, gy + dy + 1, gz + dz))
                 and (_has_floor(ex, (gx + dx, gy + dy, gz + dz)) or bare(observe_block(ex, [gx + dx, gy + dy - 1, gz + dz]).get("name")) == "water")]
        return min(cands, key=lambda c: math.dist(_pt(c), gpos)) if cands else None
    def _run(self, ex: Executor, state: State):
        st, radius = self._begin(ex, state), self.args["radius"]
        boat_item = yield from self._ensure_boat(ex, state)
        goats = yield from _req_ents_or_landmark(ex, state, "goat", radius, f"半径 {radius} 格内未发现山羊", "visible_entity:goat", "jagged_peaks|frozen_peaks|snowy_slopes|meadow")
        st = state.refresh_status(ex.client, max_age_s=0)
        goat = min(goats, key=lambda e: eye_to_point_dist(st, e["pos"]))
        st = yield from _approach_ent(ex, state, goat, reach=5.0)
        if (bcell := self._find_boat_cell(ex, goat["pos"], st["pos"])) is None: raise _env_blocked("山羊附近缺少放置船的平坦空位", "clear_site:boat", "山羊周边 2 格")
        floor_cell, boat_pos = (bcell[0], bcell[1] - 1, bcell[2]), _pt(bcell)
        yield from _approach_block(ex, state, floor_cell, reach=STATION_EYE_REACH)
        yield from use_item_on(ex, state, floor_cell, boat_item)
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
    verified = "live"
    params = {"radius": Param(int, "搜索蜂巢半径", default=24, min=8, max=64)}
    _done_msg = "未确认精准采集获得蜂巢"
    @classmethod
    def blocked(cls, state: State) -> str: return _gate(state, _find_silk_tool(state) is not None, "背包缺少带精准采集（silk_touch）附魔的工具")
    def _delta_done(self, state: State, before: dict) -> bool: return state.count("bee_nest") > before.get("bee_nest", 0)
    def _is_night_or_rain(self, st: dict) -> bool:
        rain = bool(st.get("is_raining") or st.get("raining") or str(st.get("weather") or "").lower() in ("rain", "thunder"))
        tod = st.get("time_of_day", st.get("time"))
        night = (isinstance(tod, (int, float)) and not isinstance(tod, bool) and (12500 <= int(tod) % 24000 <= 23500)) or (tod is None and st.get("is_day") is False)
        return rain or night
    def _run(self, ex: Executor, state: State):
        st, radius = self._begin(ex, state), self.args["radius"]
        nests = yield from _req_blks_or_landmark(ex, state, "bee_nest", radius, f"半径 {radius} 格内未发现自然蜂巢", "visible_block:bee_nest", "bee_nest", self.name)
        st = state.refresh_status(ex.client, max_age_s=0)
        npos = block_cell(min(nests, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
        # API /get_block 不暴露蜂巢内部蜜蜂数：导航到锚点后仍严格复核夜间/雨天归巢，或巢旁可见蜜蜂（黄昏前活跃蜂巢）
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
    name, goal, description = "catch_tadpole", "t3:tadpole_bucket", "必要时先装水再靠近蝌蚪用水桶将其装入蝌蚪桶"
    verified = "live"
    params = {"radius": Param(int, "搜索蝌蚪半径", default=24, min=8, max=64)}
    _done_msg = "未确认捕获蝌蚪装桶"
    @classmethod
    def blocked(cls, state: State) -> str:
        return _gate(state, state.count("water_bucket") >= 1 or state.count("bucket") >= 1, "背包缺少水桶（water_bucket）或空桶（bucket）")
    def _delta_done(self, state: State, before: dict) -> bool: return state.count("tadpole_bucket") > before.get("tadpole_bucket", 0)
    def _ensure_water_bucket(self, ex: Executor, state: State, radius: int) -> Generator[dict, None, None]:
        if state.count("water_bucket") >= 1: return
        st = state.refresh_status(ex.client, max_age_s=0)
        waters = _ready(scan_blocks(ex, state, "water", radius=min(radius, 24), limit=8, skill_name=self.name))["blocks"]
        if not waters:
            raise _env_blocked("背包仅有空桶且附近未发现水源，无法装水捕捞蝌蚪", "visible_block:water", f"半径 {min(radius, 24)} 格")
        wblk = min(waters, key=lambda b: (0 if (b.get("properties") or {}).get("level") in (None, 0, "0") else 1, eye_to_block_dist(st, b["pos"])))
        wpos = block_cell(wblk["pos"])
        yield from _approach_block(ex, state, wpos, reach=STATION_EYE_REACH)
        before_wb = state.count("water_bucket")
        yield from use_item_on(ex, state, wpos, "bucket")
        if state.count("water_bucket") <= before_wb:
            raise Replan("向水源使用空桶后未确认获得水桶（water_bucket）")
    def _run(self, ex: Executor, state: State):
        st, radius = self._begin(ex, state), self.args["radius"]
        tads = yield from _req_ents_or_landmark(ex, state, "tadpole", radius, f"半径 {radius} 格内未发现蝌蚪", "visible_entity:tadpole", "swamp|mangrove_swamp")
        yield from self._ensure_water_bucket(ex, state, radius)
        st = state.refresh_status(ex.client, max_age_s=0)
        tad = min(tads, key=lambda e: eye_to_point_dist(st, e["pos"]))
        yield from _approach_ent(ex, state, tad, reach=ENTITY_EYE_REACH)
        before_tb, before_wb = state.count("tadpole_bucket"), state.count("water_bucket")
        ev = yield from use_item_on(ex, state, tad["id"], "water_bucket")
        if state.count("tadpole_bucket") <= before_tb and not (state.count("water_bucket") < before_wb and ev["receipt"].get("changed") is True):
            raise Replan("捕捞蝌蚪后未确认获得蝌蚪桶")
        self._done = True

@register
class SpyglassObserve(_BiomeSkill):
    name, goal, description = "spyglass_observe", "t3:spyglass_{target}", "备齐或合成望远镜后瞄准并观察恶魂或鹦鹉"
    verified = "live"
    params = {"target": Param(str, "观察目标", default="parrot", choices=("ghast", "parrot")), "radius": Param(int, "搜索目标半径", default=48, min=8, max=64)}
    _done_msg = "未确认望远镜观察目标生物"
    @classmethod
    def blocked(cls, state: State) -> str:
        return _gate(state, state.count("spyglass") >= 1 or _can_craft_spyglass(state),
                     "背包缺少望远镜（spyglass）及合成材料（2 铜锭 + 1 紫水晶碎片）")
    def _run(self, ex: Executor, state: State):
        st, target, radius = self._begin(ex, state), self.args["target"], self.args["radius"]
        if state.count("spyglass") < 1:
            if not _can_craft_spyglass(state):
                raise Replan("背包缺少望远镜及合成材料（2 铜锭 + 1 紫水晶碎片）")
            yield from _craft_item(ex, state, "spyglass", need_table=True, err_msg="合成望远镜后背包未确认增加 spyglass")
            st = state.refresh_status(ex.client, max_age_s=0)
        if target == "parrot":
            ents = yield from _req_ents_or_landmark(ex, state, target, radius, f"半径 {radius} 格内未发现 {target}", f"visible_entity:{target}", "jungle")
            st = state.refresh_status(ex.client, max_age_s=0)
        else:
            ents = _req_ents(ex, state, target, radius, f"半径 {radius} 格内未发现 {target}", f"visible_entity:{target}")
        visible = [e for e in ents if can_see_block(ex, st, e["pos"])]
        if not visible: raise _env_blocked(f"半径 {radius} 格内 {target} 视线均被遮挡", f"visible_los:{target}", f"半径 {radius} 格视线")
        ent = min(visible, key=lambda e: eye_to_point_dist(st, e["pos"]))
        yield rec("look", ex.look(entity_id=ent["id"]), entity_id=ent["id"])
        receipt = ex.use_item("spyglass", target={"entity_id": ent["id"]}, ms=600)
        yield rec("use_item", receipt, item="spyglass", target={"entity_id": ent["id"]}, ms=600)
        if isinstance(receipt, dict) and receipt.get("ok") is False: raise Replan(f"望远镜观察 {target} 返回失败回执")
        self._done = True
