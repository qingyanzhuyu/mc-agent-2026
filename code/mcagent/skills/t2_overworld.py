"""Stage 2 Overworld T2 Quick Wins L3 技能（7 项）。"""
from __future__ import annotations

import math

from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, rec
from .encounter import _ARROWS, _retreat_clear, shoot_projectile, verify_shot_lane
from .field import _cell, _safe_support, _standable_cell, equip_item, move_local, place_block, scan_blocks, scan_entities
from .interaction import _clear_posture, use_item_on
from .items import AIR, bare
from .observations import completed_advancements, observe_block, refresh_inventory
from .registry import register
from .sightline import block_center, eye_to_block_dist
from .world_memo import memo_for

_CARDINAL = ((1, 0), (-1, 0), (0, 1), (0, -1))
_SIGN_BLOCKS = tuple(f"{w}_{k}" for w in ("oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "cherry",
                                          "mangrove", "pale_oak", "bamboo", "crimson", "warped") for k in ("sign", "wall_sign"))
_UNWAXED_COPPER = ("copper_block", "exposed_copper", "weathered_copper", "oxidized_copper", "cut_copper",
                   "exposed_cut_copper", "weathered_cut_copper", "oxidized_cut_copper", "chiseled_copper", "copper_grate", "copper_bulb")
_WAXED_COPPER = tuple(f"waxed_{c}" for c in _UNWAXED_COPPER)
_FISH_SPECIES = ("cod", "salmon", "pufferfish", "tropical_fish")
_FISH_BUCKETS = tuple(f"{s}_bucket" for s in _FISH_SPECIES)
_SCULK_BLOCKS = ("sculk_sensor", "sculk_shrieker", "calibrated_sculk_sensor")

def _obs(ex, pos) -> str: return bare(observe_block(ex, pos).get("name"))
def _is_air(ex, pos) -> bool: return _obs(ex, pos) in AIR
def _hdist(a, b) -> float: return math.hypot(a[0] - b[0], a[2] - b[2])
def _dim(state) -> str: return str((state.st or {}).get("dimension") or "overworld")
def _blocked(msg: str, pred: str, vol: str) -> EnvBlocked:
    return EnvBlocked(msg, missing_predicate=pred, searched_volume=vol, wake_on="chunk_moved")

def _inv_item(state, pred) -> str | None:
    return next((n for r in (state.inventory or {}).get("items") or ()
                 if (n := bare(r.get("name"))) and pred(n) and state.count(n) >= 1), None)

def _standable_near(ex, pt, origin, reach: float = 3.5):
    tx, ty, tz = _cell(pt)
    cands = [(tx + dx + 0.5, float(ty + dy), tz + dz + 0.5) for dy in (0, -1, 1, 2) for dx in range(-3, 4) for dz in range(-3, 4)
             if math.dist((tx + dx + 0.5, float(ty + dy), tz + dz + 0.5), pt) <= reach and _standable_cell(ex, (tx + dx, ty + dy, tz + dz))]
    return min(cands, key=lambda p: math.dist(p, origin)) if cands else None

def _reach_dest(ex, st, target_pos, reach: float, *, is_block: bool = False):
    dist = eye_to_block_dist(st, target_pos) if is_block else math.dist(st["pos"], target_pos)
    if dist <= reach:
        return False, None
    pt = block_center(target_pos) if is_block else target_pos
    if not is_block and reach > 4.0 and dist > 0:
        mid = tuple(a + (b - a) * (max(0.0, dist - min(reach - 2.0, 18.0)) / dist) for a, b in zip(st["pos"], pt))
        if (d := _standable_near(ex, mid, st["pos"], 3.5)) is not None and math.dist(d, pt) <= reach:
            return True, d
    return True, _standable_near(ex, pt, st["pos"], min(reach, 3.5))

def _approach(ex, state, target_pos, *, reach: float = 3.5, max_distance: int = 32, is_block: bool = False):
    need, dest = _reach_dest(ex, state.refresh_status(ex.client, max_age_s=0), target_pos, reach, is_block=is_block)
    if need:
        if dest is None:
            raise _blocked("目标附近无安全站位", "standable_approach", "目标附近")
        yield from move_local(ex, state, dest, rng=1, max_distance=max_distance)

def _scan_req(ex, state, types, radius: int, msg: str, pred: str, *, entity: bool = False, reach: float = 3.5, filt=None):
    scan = scan_entities(ex, state, types, radius=radius) if entity else scan_blocks(ex, state, types, radius=radius, limit=12)
    if scan.get("ready") is False:
        raise Replan("世界加载尚未就绪")
    if not entity:
        if not scan["blocks"]:
            raise _blocked(msg, pred, f"半径 {radius} 格")
        return scan["blocks"]
    st = state.refresh_status(ex.client, max_age_s=0)
    for ent in sorted((e for e in scan["entities"] if filt is None or filt(e)), key=lambda e: math.dist(st["pos"], e["pos"])):
        need, dest = _reach_dest(ex, st, ent["pos"], reach)
        if not need or dest is not None:
            return ent
    raise _blocked(msg, pred, f"半径 {radius} 格实体")

def _find_retreat(ex, state, away_from):
    st = state.refresh_status(ex.client, max_age_s=0)
    ox, oy, oz = _cell(st["pos"])
    cands = sorted(((ox + dx + 0.5, float(oy), oz + dz + 0.5) for dx, dz in _CARDINAL), key=lambda p: math.dist(p, away_from), reverse=True)
    return next((c for c in cands if _retreat_clear(ex, st["pos"], c)), None)

def _craft_with_table(ex, state, item: str, err_msg: str):
    scan = scan_blocks(ex, state, "crafting_table", radius=16, limit=5)
    if scan["blocks"] and eye_to_block_dist(state.st, scan["blocks"][0]["pos"]) > 3.5:
        tpos = list(scan["blocks"][0]["pos"])
        yield rec("move_to", ex.move_to(pos=tpos, rng=2), pos=tpos)
    before = state.count(item)
    yield rec("craft", ex.craft(item, 1), item=item, count=1)
    refresh_inventory(ex, state)
    if state.count(item) <= before:
        raise Replan(err_msg)

def _clear_floor_site(ex, state, radius: int = 3):
    st = state.refresh_status(ex.client, max_age_s=0)
    ox, oy, oz = _cell(st["pos"])
    cands = sorted(((ox + dx, oy + dy, oz + dz) for dy in (0, 1, -1) for dx in range(-radius, radius + 1)
                    for dz in range(-radius, radius + 1) if (ox + dx, oz + dz) != (ox, oz)), key=lambda p: math.dist(p, (ox, oy, oz)))
    return next((p for p in cands if _is_air(ex, p) and _safe_support(ex, (p[0], p[1] - 1, p[2])) and eye_to_block_dist(st, p) <= 4.5), None)

def _use_check(ex, state, target, item: str, check, err_msg: str):
    ev = yield from use_item_on(ex, state, target, item)
    if not check(ev):
        raise Replan(err_msg)

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
            yield from _approach(ex, state, sign_pos, reach=3.5, max_distance=16, is_block=True)
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
    params = {"target": Param(str, "目标水生生物", default="fish", choices=("fish", "axolotl")), "radius": Param(int, "搜索半径", default=32, min=8, max=64)}
    _done_msg = "未确认捕获目标水生生物"
    @classmethod
    def blocked(cls, state) -> str: return "" if state.count("water_bucket") >= 1 else "背包缺少水桶"
    def _delta_done(self, state, before: dict) -> bool:
        buckets = _FISH_BUCKETS if self.args["target"] == "fish" else ("axolotl_bucket",)
        return sum(state.count(b) for b in buckets) > sum(before.get(b, 0) for b in buckets)
    def run(self, ex, state):
        self._begin(ex, state); target, radius = self.args["target"], self.args["radius"]
        species, buckets = (_FISH_SPECIES, _FISH_BUCKETS) if target == "fish" else (("axolotl",), ("axolotl_bucket",))
        ent = _scan_req(ex, state, species, radius, f"未找到可达的 {target}", f"visible_entity:{target}", entity=True)
        yield from _approach(ex, state, ent["pos"], reach=3.5, max_distance=radius)
        before_b = sum(state.count(b) for b in buckets)
        yield from _use_check(ex, state, ent["id"], "water_bucket", lambda _: sum(state.count(b) for b in buckets) > before_b, "捕捉水生生物后未确认获得对应生物桶")
        self._done = True

@register
class TameAnimal(_OverworldSkill):
    name, description, goal = "tame_animal", "用骨头驯服狼或用生鳕鱼/鲑鱼驯服猫", "t2:tame"
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
        yield from _approach(ex, state, ent["pos"], reach=3.5, max_distance=self.args["radius"])
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
    params = {"mode": Param(str, "蜂巢与涂蜡模式", default="honey", choices=("honey", "wax_on", "wax_off")), "radius": Param(int, "搜索半径", default=32, min=8, max=64)}
    _done_msg = "未确认蜂巢采蜜或铜块涂/刮蜡完成"
    @classmethod
    def blocked(cls, state) -> str:
        ok = (state.count("campfire") >= 1 and state.count("glass_bottle") >= 1) or state.count("honeycomb") >= 1 or _inv_item(state, lambda n: n.endswith("_axe"))
        return "" if ok else "缺少蜂巢采蜜或涂蜡/刮蜡所需物品"
    def can_start(self, state) -> bool:
        m = self.args["mode"]
        return (state.count("campfire") >= 1 and state.count("glass_bottle") >= 1) if m == "honey" else (state.count("honeycomb") >= 1 if m == "wax_on" else _inv_item(state, lambda n: n.endswith("_axe")) is not None)
    def run(self, ex, state):
        self._begin(ex, state); mode, radius = self.args["mode"], self.args["radius"]
        if mode == "honey":
            npos = tuple(_scan_req(ex, state, ("bee_nest", "beehive"), radius, "未找到蜂巢或蜂箱", "visible_block:bee_nest")[0]["pos"])
            memo_for(state).record_landmark("bee_nest", npos, dimension=_dim(state))
            yield from _approach(ex, state, npos, reach=3.5, max_distance=radius, is_block=True)
            nx, ny, nz = npos
            if not any(_obs(ex, (nx, ny - d, nz)) in ("campfire", "soul_campfire") for d in (1, 2, 3)):
                ox, oy, oz = _cell(state.refresh_status(ex.client, max_age_s=0)["pos"])
                cpos = next(((nx, ny - d, nz) for d in (1, 2, 3) if _is_air(ex, (nx, ny - d, nz)) and _safe_support(ex, (nx, ny - d - 1, nz)) and not ((nx, nz) == (ox, oz) and abs((ny - d) - oy) <= 1)), None)
                if cpos is None: raise Replan("蜂巢正下方缺少安全营火位置")
                yield from place_block(ex, state, "campfire", cpos)
            before_hb = state.count("honey_bottle")
            yield from _use_check(ex, state, npos, "glass_bottle", lambda ev: state.count("honey_bottle") > before_hb or ev["receipt"].get("changed") is True, "采蜜后未确认获得蜂蜜瓶")
        elif mode == "wax_on":
            scan = scan_blocks(ex, state, _UNWAXED_COPPER, radius=radius, limit=12)
            if scan["blocks"]:
                cpos = tuple(scan["blocks"][0]["pos"]); yield from _approach(ex, state, cpos, reach=3.5, max_distance=radius, is_block=True)
            elif state.count("copper_block") >= 1 and (cpos := _clear_floor_site(ex, state, 3)) is not None:
                yield from place_block(ex, state, "copper_block", cpos)
            else: raise _blocked("附近未找到可涂蜡的铜方块", "visible_block:copper_block", f"半径 {radius} 格")
            before_hc = state.count("honeycomb")
            yield from _use_check(ex, state, cpos, "honeycomb", lambda ev: state.count("honeycomb") < before_hc or _obs(ex, cpos).startswith("waxed_") or ev["receipt"].get("changed") is True, "涂蜡后未确认生效")
        else:
            axe = _inv_item(state, lambda n: n.endswith("_axe"))
            wpos = tuple(_scan_req(ex, state, _WAXED_COPPER, radius, "未找到已涂蜡铜方块", "visible_block:waxed_copper")[0]["pos"])
            yield from _approach(ex, state, wpos, reach=3.5, max_distance=radius, is_block=True)
            yield from _use_check(ex, state, wpos, axe, lambda ev: not _obs(ex, wpos).startswith("waxed_") or ev["receipt"].get("changed") is True, "刮蜡后方块仍带蜡")
        self._done = True

@register
class BiomeQuickAction(_OverworldSkill):
    name, description, goal = "biome_quick_action", "执行潜行避震、刷犰狳、踩细雪、浸水恶魂或弩杀掠夺者", "t2:biome_{action}"
    params = {"action": Param(str, "群系顺手任务", default="avoid_vibration", choices=("avoid_vibration", "brush_armadillo", "powder_snow", "dried_ghast", "crossbow_pillager")), "radius": Param(int, "搜索半径", default=32, min=8, max=64)}
    _done_msg = "未确认群系顺手动作完成"
    @classmethod
    def blocked(cls, state) -> str:
        items = [r for r in ((state.inventory or {}).get("items") or ()) if isinstance(r, dict) and int(r.get("count", 0)) >= 1]
        feet = bare((((state.inventory or {}).get("armor") or {}).get("feet") or {}).get("name"))
        if items or feet == "leather_boots" or memo_for(state).has_landmark("deep_dark", dimension=_dim(state)):
            return ""
        return "起步空包且未发现深暗/群系前置，暂不启动群系顺手任务"
    def can_start(self, state) -> bool:
        a = self.args["action"]
        if a == "avoid_vibration": return True
        if a == "powder_snow":
            feet = bare((((state.inventory or {}).get("armor") or {}).get("feet") or {}).get("name"))
            return state.count("leather_boots") >= 1 or feet == "leather_boots"
        if a in ("brush_armadillo", "dried_ghast"):
            return state.count("brush" if a == "brush_armadillo" else "dried_ghast") >= 1
        return state.count("crossbow") >= 1 and sum(state.count(k) for k in _ARROWS) >= 2
    def _avoid_vibration(self, ex, state, radius: int):
        hits = _scan_req(ex, state, _SCULK_BLOCKS, radius, "未找到幽匿传感器或尖啸体", "visible_block:sculk_sensor")
        memo_for(state).record_landmark("sculk_sensor", hits[0]["pos"], dimension=_dim(state))
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
        yield from equip_item(ex, state, "leather_boots", slot="feet")
        hits = _scan_req(ex, state, "powder_snow", radius, "未找到细雪方块", "visible_block:powder_snow")
        memo_for(state).record_landmark("powder_snow", hits[0]["pos"], dimension=_dim(state))
        ps = next((tuple(b["pos"]) for b in hits if _is_air(ex, (b["pos"][0], b["pos"][1] + 1, b["pos"][2])) and _is_air(ex, (b["pos"][0], b["pos"][1] + 2, b["pos"][2]))), None)
        if ps is None: raise _blocked("细雪上方净空不足", "clear_above:powder_snow", f"半径 {radius} 格")
        top = [ps[0] + 0.5, ps[1] + 1.0, ps[2] + 0.5]
        st = state.refresh_status(ex.client, max_age_s=0)
        if math.dist(st["pos"], top) > 3.5:
            if (dest := _standable_near(ex, top, st["pos"], 3.5)) is None: raise _blocked("细雪旁无安全落点", "standable_approach", f"半径 {radius} 格")
            yield from move_local(ex, state, dest, rng=1, max_distance=radius)
        yield rec("move_to", ex.move_to(pos=top, rng=1), pos=top)
        if math.dist(state.refresh_status(ex.client, max_age_s=0)["pos"], top) > 1.65: raise Replan("移动后未确认踏上细雪")
    def _dried_ghast(self, ex, state, radius: int):
        hits = _scan_req(ex, state, "water", radius, "附近未找到水源", "visible_block:water")
        ox, _, oz = _cell(state.refresh_status(ex.client, max_age_s=0)["pos"])
        air_spot = next(((b["pos"][0] + dx, b["pos"][1] + dy, b["pos"][2] + dz)
                         for b in hits for dx, dy, dz in ((0, 1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))
                         if (b["pos"][0] + dx, b["pos"][2] + dz) != (ox, oz) and _is_air(ex, (b["pos"][0] + dx, b["pos"][1] + dy, b["pos"][2] + dz))), None)
        tpos = air_spot or tuple(hits[0]["pos"])
        yield from _approach(ex, state, tpos, reach=4.5, max_distance=radius, is_block=True)
        if air_spot is not None: yield from place_block(ex, state, "dried_ghast", tpos)
        else:
            before_dg = state.count("dried_ghast")
            yield rec("place", ex.place("dried_ghast", pos=list(tpos)), item="dried_ghast", pos=list(tpos))
            refresh_inventory(ex, state)
            if state.count("dried_ghast") >= before_dg and _obs(ex, tpos) != "dried_ghast": raise Replan("放置干涸恶魂未确认")
    def run(self, ex, state):
        self._begin(ex, state); action, radius = self.args["action"], self.args["radius"]
        if action in ("avoid_vibration", "powder_snow", "dried_ghast"):
            yield from getattr(self, f"_{action}")(ex, state, radius)
        elif action == "brush_armadillo":
            ent = _scan_req(ex, state, "armadillo", radius, "未找到可达犰狳", "visible_entity:armadillo", entity=True)
            yield from _approach(ex, state, ent["pos"], reach=3.5, max_distance=radius)
            before_sc = state.count("armadillo_scute")
            yield from _use_check(ex, state, ent["id"], "brush", lambda ev: state.count("armadillo_scute") > before_sc or ev["receipt"].get("changed") is True, "刷犰狳后未确认掉落或交互成功")
        else:
            ent = _scan_req(ex, state, "pillager", radius, "未找到可达掠夺者", "visible_entity:pillager", entity=True, reach=24.0)
            yield from _approach(ex, state, ent["pos"], reach=24.0, max_distance=radius)
            if (retreat := _find_retreat(ex, state, ent["pos"])) is None: raise Replan("缺少同高水平撤离格")
            yield from shoot_projectile(ex, state, entity=ent, weapon="crossbow", charge_ms=1300, retreat_pos=retreat, max_distance=32)
        self._done = True
