"""Stage 3 Wave B T4 下界终局技能：下界合金护甲套装、私域下界基岩顶凋灵卡杀、露天信标激活。"""
from __future__ import annotations

import math, time
from collections.abc import Generator, Mapping, Sequence
from typing import Any

from ..client import ApiError
from ..loop.subgoals import enchantment_ready
from ..runtime import EnvBlocked, Executor, Replan, State
from .base import Param, Skill, failure, rec
from .field import scan_blocks, scan_entities
from .interaction import _clear_posture
from .items import AIR, bare, is_clear_space, is_safe_solid_floor
from .observations import observe_block, refresh_inventory
from .registry import register
from .sightline import (DIG_EYE_REACH, ENTITY_EYE_REACH, STATION_EYE_REACH, block_cell,
                        can_reach_block, eye_to_block_dist, eye_to_point_dist, feet_pos)
from .trade_smith_brew import smith_once, trade_once
from .voxel_cache import voxel_cache_for

ARMOR_SLOTS = ("helmet", "chestplate", "leggings", "boots")
VALID_PYRAMID_BLOCKS = ("iron_block", "gold_block", "emerald_block", "diamond_block", "netherite_block")
INVALID_COPPER_BLOCKS = ("copper_block", "exposed_copper", "weathered_copper", "oxidized_copper", "waxed_copper_block", "waxed_exposed_copper", "waxed_weathered_copper", "waxed_oxidized_copper")
_TEMPLATE = "netherite_upgrade_smithing_template"
_SUPPORT_BLOCKS = ("cobblestone", "netherrack", "cobbled_deepslate", "stone", "blackstone", "basalt", "dirt")
_SWORDS = ("netherite_sword", "diamond_sword", "iron_sword", "stone_sword", "golden_sword", "wooden_sword")
_MELEE = (*_SWORDS, "netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "mace")
_DEBRIS_PICKS, _ANVIL_BLOCKS = ("netherite_pickaxe", "diamond_pickaxe"), ("anvil", "chipped_anvil", "damaged_anvil")
_END_DIMS, _NETHER_DIMS, _WITHER_3RD_CODES = frozenset({"the_end", "end"}), frozenset({"the_nether", "nether"}), frozenset({"place_failed", "protected_region"})
_UNBREAKABLE = frozenset({"bedrock", "end_portal_frame", "barrier", "command_block", "lava", "flowing_lava"})
_BEACON_OFFSETS = ((2, 0), (-2, 0), (0, 2), (0, -2), (3, 0), (-3, 0), (0, 3), (0, -3), (2, 2), (-2, -2), (0, 0))
_GRID_3X3, _SUP_OFFSETS = tuple((dx, dz) for dx in (-1, 0, 1) for dz in (-1, 0, 1)), tuple((dx, dz) for dz in (-2, -1) for dx in (-1, 0, 1))

def _dim(src: Any) -> str: return bare((src if isinstance(src, Mapping) else (getattr(src, "st", None) or {})).get("dimension")).strip().lower() or "overworld"
def _pt(c: tuple[int, int, int]) -> list[float]: return [c[0] + 0.5, float(c[1]), c[2] + 0.5]
def _bname(ex: Executor, c: Sequence[int]) -> str: return bare(observe_block(ex, list(c)).get("name"))
def _is_air(ex: Executor, c: Sequence[int]) -> bool: return _bname(ex, c) in AIR
def _overlaps(c: tuple[int, int, int], p: Sequence[float]) -> bool: px, py, pz = block_cell(p); return c[0] == px and c[2] == pz and abs(c[1] - py) <= 1
def _is_standable(ex: Executor, c: tuple[int, int, int]) -> bool:
    return is_safe_solid_floor(observe_block(ex, [c[0], c[1] - 1, c[2]])) and is_clear_space(observe_block(ex, list(c))) and is_clear_space(observe_block(ex, [c[0], c[1] + 1, c[2]]))
def _is_preemption(exc: BaseException) -> bool: return failure(exc)[0] in ("replan", "interrupted") or "抢占" in str(exc)
def _pick(state: State, names: Sequence[str]) -> str | None: return next((n for n in names if state.count(n) >= 1), None)
def _sky_clear(name: str) -> bool: return not name or name in AIR or name == "glass" or name.endswith("_stained_glass")
def _ready(scan: dict) -> dict:
    if scan.get("ready") is False: raise Replan("世界加载尚未就绪（ready=false）")
    return scan
def _gate_inv(state: State, empty_msg: str) -> str:
    if not state.ready: return "世界尚未就绪（ready=false）"
    inv = state.inventory or {}; armor = inv.get("armor") if isinstance(inv.get("armor"), Mapping) else {}
    return "" if any(isinstance(r, Mapping) and bare(r.get("name")) and int(r.get("count", 1)) > 0 for r in (*(inv.get("items") or ()), inv.get("held"), inv.get("offhand"), *armor.values())) else empty_msg
def _approach_block(ex: Executor, state: State, target: tuple[int, int, int], *, reach: float = STATION_EYE_REACH) -> Generator[dict, None, dict]:
    st = state.refresh_status(ex.client, max_age_s=0)
    if can_reach_block(ex, st, target, reach=reach) and not _overlaps(target, st["pos"]): return st
    tx, ty, tz = target
    cands = [(tx + dx, ty + dy, tz + dz) for dy in (0, -1, 1, -2, 2) for dx in range(-3, 4) for dz in range(-3, 4)
             if not (dx == 0 and dz == 0 and abs(dy) <= 1) and _is_standable(ex, (tx + dx, ty + dy, tz + dz)) and can_reach_block(ex, _pt((tx + dx, ty + dy, tz + dz)), target, reach=reach)]
    dest = _pt(min(cands, key=lambda c: math.dist(_pt(c), feet_pos(st)))) if cands else _pt(target)
    yield rec("move_to", ex.move_to(pos=dest, rng=1), pos=dest); st = state.refresh_status(ex.client, max_age_s=0)
    if eye_to_block_dist(st, target) > max(reach, DIG_EYE_REACH): raise Replan(f"接近目标方块 {list(target)} 后仍超出眼距")
    return st
def _approach_ent(ex: Executor, state: State, ent: Mapping[str, Any], *, reach: float = ENTITY_EYE_REACH) -> Generator[dict, None, dict]:
    st = state.refresh_status(ex.client, max_age_s=0)
    if eye_to_point_dist(st, ent["pos"]) <= reach: return st
    yield rec("move_to", ex.move_to(pos=(dest := [float(v) for v in ent["pos"]]), rng=2), pos=dest); return state.refresh_status(ex.client, max_age_s=0)
def _place_cell(ex: Executor, state: State, item: str, cell: tuple[int, int, int]) -> Generator[dict, None, None]:
    r = ex.place(item, list(cell)); voxel_cache_for(state).invalidate(cell, dimension=_dim(state)); yield rec("place", r, item=item, pos=list(cell))
def _reach_dig(ex: Executor, state: State, cell: tuple[int, int, int]) -> Generator[dict, None, None]:
    yield from _approach_block(ex, state, cell, reach=DIG_EYE_REACH); r = ex.dig(list(cell), collect=True); voxel_cache_for(state).invalidate(cell, dimension=_dim(state)); yield rec("dig", r, pos=list(cell))
def _ensure_block(ex: Executor, state: State, cell: tuple[int, int, int], item: str, *, allow: Sequence[str] = ()) -> Generator[dict, None, None]:
    cur = _bname(ex, cell)
    if cur == item or (allow and cur in allow): return
    if cur and cur not in AIR:
        if cur in _UNBREAKABLE: raise Replan(f"位置 {list(cell)} 存在不可破坏方块 {cur}")
        yield from _reach_dig(ex, state, cell)
    yield from _approach_block(ex, state, cell, reach=DIG_EYE_REACH); yield from _place_cell(ex, state, item, cell)
def _craft_inc(ex: Executor, state: State, item: str, count: int = 1) -> Generator[dict, None, bool]:
    before = state.count(item); yield rec("craft", ex.craft(item, count), item=item, count=count); refresh_inventory(ex, state); return state.count(item) > before
def _equip_hand(ex: Executor, state: State, item: str) -> Generator[dict, None, None]:
    held = (state.inventory or {}).get("held") or (state.st or {}).get("held") or {}
    if bare(held.get("name")) != bare(item): yield rec("equip", ex.equip(bare(item), "hand"), item=bare(item), slot="hand")
def _ensure_station(ex: Executor, state: State, types: Any, item: str, skill: str, st: Mapping[str, Any]) -> Generator[dict, None, tuple[int, int, int] | None]:
    if found := _ready(scan_blocks(ex, state, types, radius=16, limit=4, skill_name=skill))["blocks"]:
        return block_cell(min(found, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
    if state.count(item) < 1: return None
    ox, oy, oz = block_cell(st["pos"])
    cands = [(eye_to_block_dist(st, c := (ox + dx, oy + dy, oz + dz)), c) for dy in (0, -1, 1) for dx in range(-3, 4) for dz in range(-3, 4)
             if not _overlaps(c, st["pos"]) and _is_air(ex, c) and is_safe_solid_floor(observe_block(ex, [c[0], c[1] - 1, c[2]])) and can_reach_block(ex, st, c, reach=STATION_EYE_REACH)]
    if not cands: return None
    yield from _place_cell(ex, state, item, pos := min(cands)[1]); return pos
def _ensure_table(ex: Executor, state: State, skill: str, st: Mapping[str, Any]) -> Generator[dict, None, tuple[int, int, int]]:
    for attempt_craft in (False, True):
        if tpos := (yield from _ensure_station(ex, state, "crafting_table", "crafting_table", skill, st if not attempt_craft else state.refresh_status(ex.client, max_age_s=0))):
            yield from _approach_block(ex, state, tpos, reach=STATION_EYE_REACH); return tpos
        planks = sum(state.count(bare(r.get("name"))) for r in (state.inventory or {}).get("items", ()) if isinstance(r, Mapping) and bare(r.get("name")).endswith("_planks"))
        if attempt_craft or planks < 4 or not (yield from _craft_inc(ex, state, "crafting_table", 1)): break
    raise Replan("3×3 合成需要附近存在或可放置工作台 (crafting_table)")
def _drink_fire_res(ex: Executor, state: State, st: Mapping[str, Any]) -> Generator[dict, None, None]:
    effs = st.get("effects")
    if (isinstance(effs, Mapping) and any(bare(k).lower() == "fire_resistance" and v not in (None, False) for k, v in effs.items())) or (
            isinstance(effs, Sequence) and not isinstance(effs, (str, bytes)) and any(bare(e.get("name") or e.get("id") if isinstance(e, Mapping) else e).lower() == "fire_resistance" for e in effs)): return
    inv = state.inventory or {}
    for r in (*(inv.get("items") or ()), inv.get("held"), inv.get("offhand")):
        if isinstance(r, Mapping) and int(r.get("count", 1)) >= 1 and ("fire_resistance" in (n := bare(r.get("name")).lower()) or (n == "potion" and "fire_resistance" in bare(r.get("potion")).lower())):
            yield rec("use_item", ex.use_item(n), item=n); refresh_inventory(ex, state); return
def _confirm_wither_spawned(ex: Executor) -> bool:
    try:
        raw = ex.client.get_entities(radius=32)
        if any(isinstance(e, Mapping) and bare(e.get("name")).lower() == "wither" for e in ((raw.get("entities") if isinstance(raw, Mapping) else raw) or ())): return True
    except (ApiError, AttributeError): pass
    if callable(adv_fn := getattr(ex.client, "advancements", None)):
        try: obs = adv_fn()
        except (ApiError, TypeError): obs = None
        if isinstance(obs, Mapping):
            return any(bare(r.get("id") if isinstance(r, Mapping) else r).lower() == "nether/summon_wither" and (not isinstance(r, Mapping) or r.get("status", "completed") == "completed") for r in (obs.get("advancements") or obs.get("completed") or ()))
    return False
def _wait_wither_window(ex: Executor, state: State, *, newly_summoned: bool) -> Generator[dict, None, None]:
    for _ in range(12):
        refresh_inventory(ex, state)
        if state.count("nether_star") >= 1 or not (ents := _ready(scan_entities(ex, state, "wither", radius=32))["entities"]): return
        hp = ents[0].get("health")
        if (isinstance(hp, (int, float)) and not isinstance(hp, bool) and float(hp) < 300.0) or (not newly_summoned and hp is None): return
        time.sleep(1.0); yield rec("wait_wither", reason="invul_window")
def _check_sky_col(ex: Executor, x: int, y0: int, z: int) -> tuple[bool, list[int]]:
    digs: list[int] = []
    for y in range(y0 + 2, min(319, y0 + 64) + 1):
        if not _sky_clear(bn := _bname(ex, [x, y, z])):
            if bn in _UNBREAKABLE or y > y0 + 5: return False, digs
            digs.append(y)
    return True, digs
def _melee_hunt(ex: Executor, state: State, mob: str, stop_item: str, stop_cnt: int, *, rounds: int,
                min_hp: float = 0.0, step_in: list[float] | None = None, loot_drop: bool = False) -> Generator[dict, None, None]:
    shield_up = False
    try:
        if wpn := _pick(state, _MELEE): yield from _equip_hand(ex, state, wpn)
        if state.count("shield") >= 1: ex.posture(shield=True); state.st["blocking"] = True; shield_up = True
        for _ in range(rounds):
            refresh_inventory(ex, state)
            if state.count(stop_item) >= stop_cnt: break
            st = state.refresh_status(ex.client, max_age_s=0)
            if min_hp > 0 and float(st.get("health", 20)) < min_hp: raise Replan(f"战斗期间血量低于 {min_hp:g}，止损撤退")
            if not (ents := _ready(scan_entities(ex, state, mob, radius=32))["entities"]): break
            w = min(ents, key=lambda e: eye_to_point_dist(st, e["pos"])); dpos = [float(v) for v in w["pos"]]
            if step_in is not None:
                if eye_to_point_dist(st, w["pos"]) > ENTITY_EYE_REACH: yield rec("move_to", ex.move_to(pos=step_in, rng=1), pos=step_in)
            else: yield from _approach_ent(ex, state, w, reach=ENTITY_EYE_REACH)
            yield rec("attack", ex.attack(entity_id=w["id"], max_hits=6), entity_id=w["id"], max_hits=6); refresh_inventory(ex, state)
            if loot_drop and state.count(stop_item) < stop_cnt: yield rec("move_to", ex.move_to(pos=dpos, rng=1), pos=dpos); refresh_inventory(ex, state)
    finally:
        if shield_up:
            try: _clear_posture(ex, state, shield=False)
            finally: state.st["blocking"] = False

@register
class CraftNetheriteArmorSet(Skill):
    name, goal, verified = "craft_netherite_armor_set", "t4:netherite_armor", "live"
    description = "漏斗零仇恨提模板、复制锻造并合成全套下界合金护甲"
    params = {"prep_only": Param(bool, "仅备齐模板/钻甲/下界合金锭前置套装", default=False)}
    @classmethod
    def material_plan(cls, args: dict, state: State) -> tuple[tuple[str, int, str], ...]:
        need = sum(1 for s in ARMOR_SLOTS if state.count(f"netherite_{s}") < 1); t = state.count(_TEMPLATE); dups = max(0, need - t)
        return (("diamond", dups * 7, "consume"),) if (t >= 1 and dups > 0) else ()
    @classmethod
    def blocked(cls, state: State) -> str:
        if msg := _gate_inv(state, "背包为空，缺少下界合金护甲锻造或提取材料"): return msg
        if all(state.count(f"netherite_{s}") >= 1 for s in ARMOR_SLOTS): return ""
        ok = (any(state.count(k) >= 1 for k in ("hopper", _TEMPLATE, "netherite_ingot", "ancient_debris", "netherite_scrap", "emerald"))
              or any(state.count(f"diamond_{s}") >= 1 for s in ARMOR_SLOTS))
        return "" if ok else "缺少下界合金甲路线物资（需漏斗/锻造模板/钻石护甲/下界合金锭/远古残骸/绿宝石之一）"
    def run(self, ex: Executor, state: State) -> Generator[dict, None, None]:
        self._prepped = False; refresh_inventory(ex, state); st = state.refresh_status(ex.client, max_age_s=0)
        if reason := self.blocked(state): raise Replan(reason)
        missing = [s for s in ARMOR_SLOTS if state.count(f"netherite_{s}") < 1]
        if not missing: self._prepped = True; return
        if state.count(_TEMPLATE) < 1 and state.count("hopper") >= 1:  # Step 1: 堡垒遗迹漏斗零仇恨提取模板
            for ch in sorted(_ready(scan_blocks(ex, state, "chest", radius=32, limit=8, skill_name=self.name))["blocks"], key=lambda b: eye_to_block_dist(st, b["pos"])):
                cx, cy, cz = block_cell(ch["pos"]); hpos = (cx, cy - 1, cz)
                yield from _ensure_block(ex, state, hpos, "hopper"); yield from _approach_block(ex, state, hpos, reach=STATION_EYE_REACH); tgt = {"pos": list(hpos)}
                for _ in range(6):
                    view = ex.window_view(tgt); yield rec("window_view", view, target=tgt)
                    cont = ((view.get("sections") or {}).get("container") if isinstance(view, Mapping) else None) or ()
                    want = next((r for r in cont if isinstance(r, Mapping) and bare(r.get("name")) == _TEMPLATE), None)
                    titem = _TEMPLATE if want else (bare(cont[0].get("name")) if cont and isinstance(cont[0], Mapping) else "")
                    if titem:
                        tcnt = 1 if titem == _TEMPLATE else max(1, int(cont[0].get("count", 1)))
                        try: yield rec("window_act", ex.window_act(tgt, [{"op": "take", "from": "container", "item": titem, "count": tcnt}]), target=tgt, item=titem, count=tcnt)
                        except (ApiError, Replan) as exc:
                            if _is_preemption(exc): raise
                            yield rec("window_act", None, target=tgt, item=titem, error_code=failure(exc)[0]); break
                        refresh_inventory(ex, state)
                        if state.count(_TEMPLATE) >= 1: break
                    time.sleep(0.35)
                if state.count(_TEMPLATE) >= 1: break
        if state.count(_TEMPLATE) >= 1 and state.count(_TEMPLATE) < len(missing) and state.count("diamond") >= 7 and state.count("netherrack") >= 1:
            yield from _ensure_table(ex, state, self.name, state.refresh_status(ex.client, max_age_s=0))
            while state.count(_TEMPLATE) < len(missing) and state.count("diamond") >= 7 and state.count("netherrack") >= 1:
                if not (yield from _craft_inc(ex, state, _TEMPLATE, 1)): break
        need_dia = tuple(f"diamond_{s}" for s in missing if state.count(f"diamond_{s}") < 1)
        if need_dia and state.count("emerald") >= 8:
            for v in sorted(_ready(scan_entities(ex, state, "villager", radius=32))["entities"], key=lambda e: eye_to_point_dist(st, e["pos"])):
                if not need_dia or state.count("emerald") < 8: break
                yield from _approach_ent(ex, state, v, reach=ENTITY_EYE_REACH)
                try: yield from trade_once(ex, state, v["id"], preferred_items=need_dia)
                except (ApiError, Replan) as exc:
                    if _is_preemption(exc): raise
                    continue
                refresh_inventory(ex, state); need_dia = tuple(f"diamond_{s}" for s in missing if state.count(f"diamond_{s}") < 1)
        if state.count("netherite_ingot") < len(missing) and state.count("netherite_scrap") >= 4 and state.count("gold_ingot") >= 4:
            yield from _ensure_table(ex, state, self.name, state.refresh_status(ex.client, max_age_s=0))
            while state.count("netherite_ingot") < len(missing) and state.count("netherite_scrap") >= 4 and state.count("gold_ingot") >= 4:
                qty = min(len(missing) - state.count("netherite_ingot"), state.count("netherite_scrap") // 4, state.count("gold_ingot") // 4)
                if not (yield from _craft_inc(ex, state, "netherite_ingot", qty)): break
        if state.count("netherite_ingot") < len(missing) and (pick := _pick(state, _DEBRIS_PICKS)):
            if debris := [b for b in _ready(scan_blocks(ex, state, "ancient_debris", radius=24, limit=8, skill_name=self.name))["blocks"] if block_cell(b["pos"])[1] <= 22]:
                st = state.refresh_status(ex.client, max_age_s=0); yield from _drink_fire_res(ex, state, st); yield from _equip_hand(ex, state, pick)
                for b in sorted(debris, key=lambda x: eye_to_block_dist(st, x["pos"]))[:4]: yield from _reach_dig(ex, state, block_cell(b["pos"])); refresh_inventory(ex, state)
        missing = [s for s in ARMOR_SLOTS if state.count(f"netherite_{s}") < 1]
        ready_all = state.count(_TEMPLATE) >= len(missing) and state.count("netherite_ingot") >= len(missing) and all(state.count(f"diamond_{s}") >= 1 for s in missing)
        if self.args["prep_only"]:
            if not ready_all: raise Replan(f"下界合金甲前置材料不足（模板 {state.count(_TEMPLATE)}/{len(missing)}，锭 {state.count('netherite_ingot')}/{len(missing)}）")
            self._prepped = True; return
        if not ready_all: raise Replan("锻造下界合金全套材料不足，需先补齐模板、钻甲与下界合金锭")
        tpos = yield from _ensure_station(ex, state, "smithing_table", "smithing_table", self.name, state.refresh_status(ex.client, max_age_s=0))
        if tpos is None: raise EnvBlocked("附近无锻造台且背包缺少 smithing_table", missing_predicate="smithing_table", searched_volume="半径 16 格", wake_on="chunk_moved")
        yield from _approach_block(ex, state, tpos, reach=STATION_EYE_REACH)
        for slot in missing: yield from smith_once(ex, state, tpos, template=_TEMPLATE, base=f"diamond_{slot}", addition="netherite_ingot", expected_output=f"netherite_{slot}"); refresh_inventory(ex, state)
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        missing = [s for s in ARMOR_SLOTS if state.count(f"netherite_{s}") < 1]
        if self.args["prep_only"]:
            ok = getattr(self, "_prepped", False) or (state.count(_TEMPLATE) >= len(missing) and state.count("netherite_ingot") >= len(missing) and all(state.count(f"diamond_{s}") >= 1 for s in missing))
            return "" if ok else "尚未备齐下界合金护甲模板/钻甲/下界合金锭前置套装"
        return "" if not missing else f"背包尚缺下界合金护甲：{', '.join(f'netherite_{s}' for s in missing)}"

@register
class SummonAndTrapWither(Skill):
    name, goal, verified = "summon_and_trap_wither", "t4:summon_wither", "live"
    description = "在私域下界基岩顶召唤并卡杀凋灵获取下界之星"
    params = {"mode": Param(str, "召唤与前置模式", default="nether_roof_trap", choices=("summon_only", "nether_roof_trap", "prep_looting", "prep_skulls"))}
    @classmethod
    def blocked(cls, state: State) -> str:
        if _dim(state) in _END_DIMS: return "赛事红线禁止在共享末地召唤凋灵，仅允许在私域下界操作"
        if msg := _gate_inv(state, "背包为空，缺少凋灵召唤或前置备战物资"): return msg
        ok = (_pick(state, _MELEE) is not None or any(state.count(k) >= 1 for k in ("wither_skeleton_skull", "soul_sand", "soul_soil", "nether_star", "emerald", "enchanted_book")))
        return "" if ok else "背包缺少凋灵备战/召唤物资（需武器、头颅、灵魂沙或绿宝石）"
    def run(self, ex: Executor, state: State) -> Generator[dict, None, None]:
        self._summoned, mode = False, self.args["mode"]; st = state.refresh_status(ex.client, max_age_s=0); refresh_inventory(ex, state); dim = _dim(st)
        if dim in _END_DIMS or (mode in ("summon_only", "nether_roof_trap") and dim not in _NETHER_DIMS):
            raise Replan("赛事红线禁止在共享末地召唤凋灵，须在本岛私域下界 Y=127 基岩顶层执行")
        if reason := self.blocked(state): raise Replan(reason)
        if mode == "prep_looting":
            if enchantment_ready(state, "sword", "looting", 3): return
            if (vills := _ready(scan_entities(ex, state, "villager", radius=32))["entities"]) and state.count("emerald") >= 1:
                v = min(vills, key=lambda e: eye_to_point_dist(st, e["pos"])); yield from _approach_ent(ex, state, v, reach=ENTITY_EYE_REACH)
                try: yield from trade_once(ex, state, v["id"], preferred_items=("enchanted_book",))
                except (ApiError, Replan) as exc:
                    if _is_preemption(exc): raise
                refresh_inventory(ex, state)
            if not enchantment_ready(state, "sword", "looting", 3) and state.count("enchanted_book") >= 1 and (sw := _pick(state, _SWORDS)):
                if (apos := (yield from _ensure_station(ex, state, _ANVIL_BLOCKS, "anvil", self.name, st))) is not None:
                    yield from _approach_block(ex, state, apos, reach=STATION_EYE_REACH); tgt = {"pos": list(apos)}
                    yield rec("window_view", ex.window_view(tgt), target=tgt)
                    ops = [{"op": "put", "item": sw, "count": 1, "to": "left"}, {"op": "put", "item": "enchanted_book", "count": 1, "to": "right", "enchanted": True}, {"op": "take", "from": "output"}]
                    yield rec("window_act", ex.window_act(tgt, ops), target=tgt, item=sw); refresh_inventory(ex, state)
            if not enchantment_ready(state, "sword", "looting", 3): raise Replan("尚未获得抢夺III剑")
            return
        if mode == "prep_skulls":
            if dim not in _NETHER_DIMS: raise Replan("刷取凋灵骷髅头颅须在下界要塞执行")
            if state.count("wither_skeleton_skull") >= 3 and (state.count("soul_sand") + state.count("soul_soil")) >= 4: return
            if not (skels := _ready(scan_entities(ex, state, "wither_skeleton", radius=32))["entities"]):
                raise EnvBlocked("附近未发现凋灵骷髅", missing_predicate="wither_skeleton", searched_volume="下界要塞半径 32 格", wake_on="chunk_moved")
            fx, fy, fz = block_cell(feet_pos(st)); t0 = min(skels, key=lambda e: eye_to_point_dist(st, e["pos"]))
            dx = 1 if t0["pos"][0] > fx + 0.6 else (-1 if t0["pos"][0] < fx + 0.4 else 0); dz = 1 if t0["pos"][2] > fz + 0.6 else (-1 if t0["pos"][2] < fz + 0.4 else 0)
            if (lintel := (fx + dx, fy + 2, fz + dz))[::2] != (fx, fz) and _is_air(ex, lintel) and (sup := _pick(state, _SUPPORT_BLOCKS)) and can_reach_block(ex, st, lintel, reach=DIG_EYE_REACH):
                yield from _place_cell(ex, state, sup, lintel)
            if not _pick(state, _MELEE): raise Replan("刷取头颅缺少近战武器")
            yield from _melee_hunt(ex, state, "wither_skeleton", "wither_skeleton_skull", 3, rounds=6, min_hp=8.0, loot_drop=True)
            if state.count("wither_skeleton_skull") < 3: raise Replan(f"凋灵骷髅头颅不足 3 颗（当前 {state.count('wither_skeleton_skull')} 颗）")
            return
        if mode == "nether_roof_trap" and state.count("nether_star") >= 1: return
        existing = _ready(scan_entities(ex, state, "wither", radius=32))["entities"] if mode == "nether_roof_trap" else []
        step_in, loot = None, None
        if existing:
            w0 = min(existing, key=lambda e: eye_to_point_dist(st, e["pos"]))
            loot = [float(w0["pos"][0]), float(block_cell(feet_pos(st))[1]), float(w0["pos"][2])]
        else:
            if state.count("wither_skeleton_skull") < 3 or (state.count("soul_sand") + state.count("soul_soil")) < 4 or state.count("obsidian") < 1:
                raise Replan("召唤凋灵材料不足：须备齐 3 颗凋灵头颅、4 块灵魂沙/灵魂土与至少 1 块黑曜石")
            bedrocks = _ready(scan_blocks(ex, state, "bedrock", radius=16, limit=64, skill_name=self.name))["blocks"]
            fx, _, fz = block_cell(feet_pos(st))
            centers = {(block_cell(b["pos"])[0], block_cell(b["pos"])[2]) for b in bedrocks if block_cell(b["pos"])[1] == 127} | {(fx, fz)}
            if not (valid := [(cx, cz) for cx, cz in centers if all(_bname(ex, [cx + dx, 127, cz + dz]) == "bedrock" for dx, dz in _GRID_3X3)]):
                raise EnvBlocked("附近未找到 Y=127 的 3×3 平整基岩顶层", missing_predicate="bedrock_ceiling_3x3_y127", searched_volume="半径 16 格 Y=127", wake_on="chunk_moved")
            cx, cz = min(valid, key=lambda c: math.hypot(c[0] - fx, c[1] - fz))
            for dx, dz in _GRID_3X3:
                if (bn := _bname(ex, c126 := (cx + dx, 126, cz + dz))) and bn not in AIR:
                    if bn in _UNBREAKABLE: raise Replan(f"Y=126 位置 {list(c126)} 存在不可破坏方块 {bn}")
                    yield from _reach_dig(ex, state, c126)
            yield from _ensure_block(ex, state, (cx, 124, cz), "obsidian")
            for dx, dz in _SUP_OFFSETS:
                if _is_air(ex, sc := (cx + dx, 124, cz + dz)) and (sup := _pick(state, _SUPPORT_BLOCKS)):
                    yield from _approach_block(ex, state, sc, reach=DIG_EYE_REACH); yield from _place_cell(ex, state, sup, sc)
            for sp in ((cx, 125, cz), (cx, 125, cz - 1), (cx - 1, 125, cz - 1), (cx + 1, 125, cz - 1)):
                yield from _ensure_block(ex, state, sp, "soul_sand" if state.count("soul_sand") >= 1 else "soul_soil", allow=("soul_sand", "soul_soil"))
            for idx, skp in enumerate(((cx - 1, 125, cz - 2), (cx, 125, cz - 2), (cx + 1, 125, cz - 2))):
                yield from _approach_block(ex, state, skp, reach=DIG_EYE_REACH)
                if idx < 2: yield from _place_cell(ex, state, "wither_skeleton_skull", skp)
                else:
                    try: yield from _place_cell(ex, state, "wither_skeleton_skull", skp)
                    except (ApiError, Replan) as exc:
                        code, msg_l = failure(exc)[0], str(exc).lower()
                        if code not in _WITHER_3RD_CODES and not any(c in msg_l for c in _WITHER_3RD_CODES): raise
                        yield rec("place", None, item="wither_skeleton_skull", pos=list(skp), error_code=code)
                        if not _confirm_wither_spawned(ex): raise Replan(f"第 3 颗头颅放置返回 {code} 且未确认凋灵生成") from exc
            retreat = [cx + 0.5, 124.0, cz + 3.5]
            yield rec("move_to", ex.move_to(pos=retreat, rng=1), pos=retreat); self._summoned = True
            if mode == "summon_only": return
            step_in, loot = [cx + 0.5, 124.0, cz + 1.5], [cx + 0.5, 124.0, cz + 0.5]
        yield from _wait_wither_window(ex, state, newly_summoned=not existing)
        if state.count("nether_star") < 1 and _pick(state, _MELEE): yield from _melee_hunt(ex, state, "wither", "nether_star", 1, rounds=8, step_in=step_in)
        refresh_inventory(ex, state)
        if state.count("nether_star") < 1:
            stars = [e for e in _ready(scan_entities(ex, state, "item", radius=32))["entities"] if "nether_star" in bare(e.get("item")).lower()]
            if (dest := ([float(v) for v in stars[0]["pos"]] if stars else loot)) is not None: yield rec("move_to", ex.move_to(pos=dest, rng=1), pos=dest); refresh_inventory(ex, state)
        if state.count("nether_star") < 1: raise Replan("凋灵击杀/窒息后尚未拾取到下界之星")
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        mode = self.args["mode"]
        if mode == "prep_looting": return "" if enchantment_ready(state, "sword", "looting", 3) else "尚未获得抢夺III剑"
        if mode == "prep_skulls": return "" if state.count("wither_skeleton_skull") >= 3 else "凋灵骷髅头颅不足 3 颗"
        if mode == "summon_only": return "" if getattr(self, "_summoned", False) else "尚未确认在私域下界基岩顶召唤凋灵"
        return "" if state.count("nether_star") >= 1 else "背包尚未获得下界之星"

@register
class ActivateBeacon(Skill):
    name, goal, verified = "activate_beacon", "t4:create_beacon", "live"
    description = "用有效矿物块搭建露天3x3信标金字塔并就近激活"
    params: dict[str, Param] = {}
    @classmethod
    def blocked(cls, state: State) -> str:
        if msg := _gate_inv(state, "背包为空，缺少信标及 3x3 有效矿物底座方块"): return msg
        if state.count("beacon") < 1 and not (state.count("nether_star") >= 1 and state.count("obsidian") >= 3 and state.count("glass") >= 5):
            return "背包缺少信标及合成材料（需 1 下界之星 + 3 黑曜石 + 5 玻璃）"
        if (valid_cnt := sum(state.count(b) for b in VALID_PYRAMID_BLOCKS)) < 9:
            copper_cnt = sum(state.count(b) for b in INVALID_COPPER_BLOCKS)
            hint = f"；铜块({copper_cnt})不能激活信标" if copper_cnt > 0 else "；铜块不可作为信标底座"
            return f"有效信标底座矿物块不足 9 块（当前 {valid_cnt} 块，须铁/金/绿宝石/钻石/下界合金块{hint}）"
        return ""
    def run(self, ex: Executor, state: State) -> Generator[dict, None, None]:
        self._activated = False; refresh_inventory(ex, state); st = state.refresh_status(ex.client, max_age_s=0)
        if reason := self.blocked(state): raise Replan(reason)
        if state.count("beacon") < 1:
            yield from _ensure_table(ex, state, self.name, st)
            if not (yield from _craft_inc(ex, state, "beacon", 1)): raise Replan("合成信标后背包未确认获得 beacon")
        fx, fy, fz = block_cell(feet_pos(st)); site, digs = None, []
        for dx, dz in _BEACON_OFFSETS:
            cand = (fx + dx, fy, fz + dz)
            if not is_safe_solid_floor(observe_block(ex, [cand[0], cand[1] - 1, cand[2]])): continue
            ok, col_digs = _check_sky_col(ex, cand[0], cand[1], cand[2])
            if not ok: continue
            site, digs = cand, col_digs
            if not col_digs: break
        if site is None: raise Replan("附近缺少无基岩或高空不透明顶块遮挡的露天平整位置，无法激活信标")
        bx, by, bz = site
        for y in digs: yield from _reach_dig(ex, state, (bx, y, bz))
        for dx, dz in _GRID_3X3:
            if _bname(ex, cell := (bx + dx, by, bz + dz)) in VALID_PYRAMID_BLOCKS: continue
            if not (blk := _pick(state, VALID_PYRAMID_BLOCKS)): raise Replan("有效矿物底座方块耗尽（已排除铜块），无法完成 3x3 金字塔")
            yield from _ensure_block(ex, state, cell, blk, allow=VALID_PYRAMID_BLOCKS); refresh_inventory(ex, state)
        bcell = (bx, by + 1, bz); yield from _ensure_block(ex, state, bcell, "beacon")
        # 零信标 GUI：严禁对信标调用 window_view/window_act，仅保持 10 格内确认放置与露天
        if eye_to_block_dist(state.refresh_status(ex.client, max_age_s=0), bcell) > 10.0: yield from _approach_block(ex, state, bcell, reach=STATION_EYE_REACH)
        if _bname(ex, bcell) != "beacon": raise Replan("放置后未在金字塔顶部确认信标方块")
        self._activated = True
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        return "" if getattr(self, "_activated", False) else "信标尚未完成露天3x3金字塔激活"
