"""Stage 3 T4 不祥试炼宝库、微型隔离村袭击与受控不死图腾触发技能（3 项 L3 注册技能）。"""
from __future__ import annotations

from collections.abc import Generator, Iterable, Mapping, Sequence
import os, time
from typing import Any, Optional

from ..client import ApiError
from ..loop.subgoals import ominous_ready
from ..runtime import EnvBlocked, Executor, Replan, State, _is_unsafe_totem_landing_block, assess_totem_armor, controlled_totem_trigger, validate_controlled_totem_conditions
from .base import Param, Skill, rec
from .interaction import _clear_posture, use_item_on
from .items import AIR, bare, is_safe_solid_floor, pick_placeable, total_placeable
from .nav import step_up_1x2
from .observations import observe_block, refresh_inventory
from .registry import register
from .sightline import DIG_EYE_REACH, ENTITY_EYE_REACH, STATION_EYE_REACH, block_cell, can_reach_block, eye_to_block_dist, eye_to_point_dist, feet_pos
from .t2_trials_oriented import _approach_block, _place_cell, clear_trial_wave
from .world_memo import BED_BLOCKS, memo_for

_CARDINALS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_WEAK_TRIAL_MOBS = frozenset({"silverfish", "slime", "zombie", "spider", "husk", "bogged", "cave_spider"})
_RAIDERS = ("pillager", "vindicator", "ravager", "evoker", "witch", "vex")
_RAIDER_PRIO = {"evoker": 0, "witch": 1, "vindicator": 2, "pillager": 3, "ravager": 4, "vex": 5}
_MELEE = ("netherite_sword", "diamond_sword", "iron_sword", "stone_sword", "netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "mace", "wooden_sword")
_RANGED, _ARROWS = ("crossbow", "bow"), ("arrow", "spectral_arrow", "tipped_arrow")
_RAID_WAVE_POLL_S, _TOTEM_FALL_POLL_S = 1.0, 0.25

def _dim(state: State) -> str: return bare((state.st or {}).get("dimension")) or "overworld"
def _is_air(ex: Executor, pos: Sequence[int]) -> bool: return bare(observe_block(ex, list(pos)).get("name")) in AIR
def _has_floor(ex: Executor, c: tuple[int, int, int]) -> bool: return is_safe_solid_floor(observe_block(ex, [c[0], c[1] - 1, c[2]]))
def _server_difficulty(state: State) -> str:
    return str((state.snapshot() or {}).get("difficulty") or os.environ.get("MC_DIFFICULTY") or getattr(getattr(state, "config", None), "difficulty", "") or "").strip().lower()
def _is_easy_or_peaceful(state: State) -> bool:
    return _server_difficulty(state) in ("easy", "peaceful") or memo_for(state).has_landmark("raid_no_evoker_easy", dimension=_dim(state))
def _is_elevated(state: State) -> bool: return bool((state.st or {}).get("elevated_column") or memo_for(state).has_landmark("totem_pillar", dimension=_dim(state)))
def _overlaps(c: tuple[int, int, int], st: Mapping[str, Any]) -> bool:
    px, py, pz = block_cell(feet_pos(st))
    return c[0] == px and c[2] == pz and abs(c[1] - py) <= 1
def _inv_stacks(state: State, *, include_armor: bool = True) -> list[Mapping[str, Any]]:
    inv = state.inventory or {}
    raw: list[Any] = [*(inv.get("items") or ()), inv.get("held"), inv.get("offhand"),
                      *(inv["armor"].values() if include_armor and isinstance(inv.get("armor"), Mapping) else ())]
    return [r for r in raw if isinstance(r, Mapping) and bare(r.get("name")) and (r.get("count", 1) or 0) >= 1]
def _gate_base(state: State, empty_msg: str) -> str:
    if not state.ready: return "世界尚未就绪（ready=false）"
    if (state.st or {}).get("alive") is False: return "角色当前未存活，无法执行不祥战斗技能"
    return "" if _inv_stacks(state) else empty_msg
def _has_effect(source: Any, names: Iterable[str]) -> bool:
    wanted = {bare(n).strip().lower() for n in names}
    effs = source.get("effects") if isinstance(source, Mapping) and "effects" in source else source
    if isinstance(effs, Mapping): return any(v not in (None, False) and bare(k).strip().lower() in wanted for k, v in effs.items())
    if isinstance(effs, Iterable) and not isinstance(effs, (str, bytes)):
        return any((isinstance(e, str) and bare(e).strip().lower() in wanted) or (isinstance(e, Mapping) and bare(e.get("name") or e.get("id") or e.get("effect")).strip().lower() in wanted) for e in effs)
    return False
def _won_hero(ex: Executor, st: Mapping[str, Any]) -> bool:
    if _has_effect(st, ("hero_of_the_village",)): return True
    if not callable(adv_fn := getattr(ex.client, "advancements", None)): return False
    try: res = adv_fn(status="completed")
    except ApiError: return False
    comp = res.get("completed") if isinstance(res, Mapping) else None
    if isinstance(comp, Mapping): return any(v not in (None, False) and bare(k).strip() == "adventure/hero_of_the_village" for k, v in comp.items())
    if isinstance(comp, Iterable) and not isinstance(comp, (str, bytes)):
        return any(bare(r.get("id") or r.get("name") if isinstance(r, Mapping) else r).strip() == "adventure/hero_of_the_village" for r in comp)
    return False
def _pick_melee(state: State) -> Optional[str]:
    return next((w for w in _MELEE if state.count(w) >= 1), None) or next(
        (n for s in _inv_stacks(state, include_armor=False) if (n := bare(s.get("name")).lower()).endswith(("_sword", "_axe")) or n == "mace"), None)
def _pick_ranged(state: State) -> Optional[str]:
    has_arrow = any(state.count(a) >= 1 for a in _ARROWS)
    if state.count("crossbow") >= 1 and (has_arrow or state.count("firework_rocket") >= 1): return "crossbow"
    return "bow" if (state.count("bow") >= 1 and has_arrow) else None
def _has_weapon(state: State) -> bool: return _pick_melee(state) is not None or any(state.count(r) >= 1 for r in _RANGED)
def _find_bed(state: State) -> Optional[str]:
    return next((b for b in BED_BLOCKS if state.count(b) >= 1), None) or next(
        (n for s in _inv_stacks(state, include_armor=False) if (n := bare(s.get("name")).lower()).endswith("_bed")), None)
def _bottle_rank(stack: Mapping[str, Any]) -> int:
    for src in (stack, *(stack[k] for k in ("components", "nbt", "metadata", "meta") if isinstance(stack.get(k), Mapping))):
        for key in ("ominous_amplifier", "amplifier"):
            if isinstance(v := src.get(key), int) and not isinstance(v, bool): return v
        if isinstance(v := src.get("level"), int) and not isinstance(v, bool): return v - 1
    return 99
def _select_lowest_ominous_bottle(state: State) -> Optional[dict]:
    """从背包记录中选择最小等级不祥之瓶；注：/equip(\"ominous_bottle\") 按物品名称选取且不接受背包槽位参数。"""
    inv = state.inventory or {}
    cands = [dict(r) for r in (inv.get("items") or ()) if isinstance(r, Mapping) and bare(r.get("name")).lower() == "ominous_bottle" and (r.get("count", 1) or 0) >= 1]
    if not cands:
        cands = [dict(r) for k in ("held", "offhand") if isinstance(r := inv.get(k), Mapping) and bare(r.get("name")).lower() == "ominous_bottle" and (r.get("count", 1) or 0) >= 1]
    return min(cands, key=_bottle_rank) if cands else None
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
def _get_entities(ex: Executor, *, radius: int = 32, types: Sequence[str] | None = None) -> list[dict]:
    wanted = [bare(t) for t in types] if types else None
    raw = (_obs_call(ex.client.get_entities, radius=radius, types=wanted)).get("entities")
    wset = set(wanted) if wanted else None
    return [dict(e) for e in (raw or ()) if isinstance(e, Mapping) and type(e.get("id")) is int and e.get("pos") and (not wset or bare(e.get("name")) in wset or bare(e.get("type")) in wset)]
def _is_ominous_vault(ex: Executor, blk: Mapping[str, Any]) -> bool:
    if bare(blk.get("name")) != "vault": return False
    props = blk.get("properties")
    if isinstance(props, Mapping) and "ominous" in props: return props.get("ominous") in (True, "true")
    return (observe_block(ex, list(block_cell(blk["pos"]))).get("properties") or {}).get("ominous") in (True, "true")
def _spawner_mob(blk: Mapping[str, Any]) -> str:
    props, ent = (blk.get("properties") if isinstance(blk.get("properties"), Mapping) else {}), (blk.get("entity") if isinstance(blk.get("entity"), Mapping) else {})
    return bare(blk.get("mob") or blk.get("spawn_entity") or props.get("mob") or props.get("spawn_entity") or ent.get("name")).strip().lower()
def _rank_spawners(st: Mapping[str, Any], spawners: Sequence[dict]) -> list[dict]:
    if not spawners: return []
    scored = sorted(((0 if (m := _spawner_mob(s)) in _WEAK_TRIAL_MOBS else (3 if m == "breeze" else (1 if not m else 2)), eye_to_block_dist(st, s["pos"]), s) for s in spawners), key=lambda r: (r[0], r[1]))
    weak = [s for t, _, s in scored if t == 0]
    return (weak or [s for t, _, s in scored if t < 3] or [s for _, _, s in scored])[:5]
def _equip_slot(ex: Executor, state: State, item: str, slot: str = "hand") -> Generator[dict, None, None]:
    item, inv = bare(item), state.inventory or {}
    cur = (inv.get("offhand") if slot == "off-hand" else inv.get("held")) or {}
    if bare(cur.get("name")) != item: yield rec("equip", ex.equip(item, slot), item=item, slot=slot); refresh_inventory(ex, state)
def _set_posture(ex: Executor, state: State, *, sneak: bool | None = None, shield: bool | None = None) -> None:
    ex.posture(sneak=sneak, shield=shield)
    if sneak is not None: state.st["sneaking"] = sneak
    if shield is not None: state.st["blocking"] = shield
def _reset_posture(ex: Executor, state: State) -> None:
    if any((state.st or {}).get(k) for k in ("sneaking", "blocking", "jumping")):
        _clear_posture(ex, state, sneak=False, shield=False, jump=False)
        state.st.update(sneaking=False, blocking=False, jumping=False)
def _find_place_near(ex: Executor, st: Mapping[str, Any], anchor: tuple[int, int, int], *, bed_footprint: bool = False) -> Optional[tuple[int, int, int]]:
    ax, ay, az = anchor
    def _ok(c: tuple[int, int, int]) -> bool:
        if c == anchor or _overlaps(c, st) or not (_is_air(ex, c) and _has_floor(ex, c)) or not can_reach_block(ex, st, c, reach=DIG_EYE_REACH): return False
        if not bed_footprint: return True
        return _is_air(ex, (c[0], c[1] + 1, c[2])) and any((h := (c[0] + dx, c[1], c[2] + dz)) != anchor and not _overlaps(h, st) and _is_air(ex, h) and _is_air(ex, (h[0], h[1] + 1, h[2])) and _has_floor(ex, h) for dx, dz in _CARDINALS)
    cands = [(eye_to_block_dist(st, c), c) for dy in (0, -1, 1) for dx in range(-3, 4) for dz in range(-3, 4) if _ok(c := (ax + dx, ay + dy, az + dz))]
    return min(cands)[1] if cands else None

class _OminousCombatSkill(Skill):
    verified = "offline"
    def _begin(self, ex: Executor, state: State) -> dict:
        self._done = False; refresh_inventory(ex, state)
        st = state.refresh_status(ex.client, max_age_s=0)
        if reason := self.blocked(state): raise Replan(reason)
        return st
    def run(self, ex: Executor, state: State):
        try: yield from self._run(ex, state)
        finally: _reset_posture(ex, state)
    def _run(self, ex: Executor, state: State): raise NotImplementedError

@register
class UnlockOminousVault(_OminousCombatSkill):
    name, goal, description = "unlock_ominous_vault", "t4:revaulting", "侦察不祥试炼宝库与弱怪笼、饮瓶清笼并用不祥钥匙开库"
    params: dict[str, Param] = {}
    @classmethod
    def blocked(cls, state: State) -> str:
        if msg := _gate_base(state, "背包为空，缺少不祥试炼钥匙或不祥之瓶与武器"): return msg
        if state.count("ominous_trial_key") >= 1 or ominous_ready(state, "trial"): return ""
        if _has_weapon(state) and (state.count("ominous_bottle") >= 1 or _has_effect(state.st, ("trial_omen", "bad_omen"))): return ""
        return "缺少不祥试炼钥匙，且未具备不祥之瓶与战斗武器"
    def _run(self, ex: Executor, state: State):
        st, dim = self._begin(ex, state), _dim(state)
        blocks = _find_blocks(ex, ["vault", "trial_spawner"], radius=48, limit=24)
        vaults = [b for b in blocks if _is_ominous_vault(ex, b)]
        if not vaults:
            raise EnvBlocked("附近未发现不祥试炼宝库", missing_predicate="trial_vault_ominous", searched_volume="半径 48 格", wake_on="chunk_moved")
        vault = min(vaults, key=lambda b: (1 if (b.get("properties") or {}).get("vault_state") == "inactive" else 0, eye_to_block_dist(st, b["pos"])))
        vpos = block_cell(vault["pos"])
        memo_for(state).record_landmark("trial_vault_ominous", vpos, dimension=dim)
        spawners = _rank_spawners(st, [b for b in blocks if bare(b.get("name")) == "trial_spawner"])
        spawner = spawners[0] if spawners else None
        if state.count("ominous_trial_key") < 1 and spawner is not None and total_placeable(state) >= 1:
            fx, fy, fz = block_cell(feet_pos(st))
            sx, _, sz = block_cell(spawner["pos"])
            dx, dz = ((1 if sx >= fx else -1), 0) if abs(sx - fx) >= abs(sz - fz) else (0, (1 if sz >= fz else -1))
            for lintel in ((fx + dx, fy + 1, fz + dz), (fx + dx, fy + 2, fz + dz)):
                if _is_air(ex, lintel) and not _overlaps(lintel, st) and can_reach_block(ex, st, lintel, reach=DIG_EYE_REACH):
                    yield from _place_cell(ex, state, pick_placeable(state), lintel); break
        st = state.refresh_status(ex.client, max_age_s=0)
        if state.count("ominous_trial_key") < 1 and not _has_effect(st, ("trial_omen", "bad_omen")):
            if state.count("ominous_bottle") < 1: raise Replan("背包缺少不祥之瓶（ominous_bottle）且无试炼之兆效果")
            yield rec("use_item", ex.use_item("ominous_bottle"), item="ominous_bottle"); refresh_inventory(ex, state)
        if state.count("ominous_trial_key") < 1:
            try:
                if state.count("shield") >= 1: _set_posture(ex, state, shield=True)
                yield from clear_trial_wave(ex, state, radius=24, max_kills=8, min_hp=10.0); refresh_inventory(ex, state)
                if state.count("ominous_trial_key") < 1:
                    st = state.refresh_status(ex.client, max_age_s=0)
                    if drops := _get_entities(ex, radius=24, types=("item",)):
                        dpos = [float(v) for v in min(drops, key=lambda e: eye_to_point_dist(st, e["pos"]))["pos"]]
                        yield rec("move_to", ex.move_to(pos=dpos, rng=1), pos=dpos); refresh_inventory(ex, state)
                    if state.count("ominous_trial_key") < 1 and spawner is not None:
                        spos = [spawner["pos"][0] + 0.5, float(spawner["pos"][1]), spawner["pos"][2] + 0.5]
                        yield rec("move_to", ex.move_to(pos=spos, rng=1), pos=spos); refresh_inventory(ex, state)
            finally:
                _reset_posture(ex, state)
            if state.count("ominous_trial_key") < 1: raise Replan("清笼后尚未获得不祥试炼钥匙（ominous_trial_key）")
        yield from _approach_block(ex, state, vpos, reach=STATION_EYE_REACH); refresh_inventory(ex, state)
        before_k, before_vs = state.count("ominous_trial_key"), (observe_block(ex, list(vpos)).get("properties") or {}).get("vault_state")
        ev = yield from use_item_on(ex, state, vpos, "ominous_trial_key")
        after_vs = ((ev["after"].get("block") or {}).get("properties") or {}).get("vault_state")
        if state.count("ominous_trial_key") >= before_k and after_vs == before_vs and ev["receipt"].get("changed") is not True:
            raise Replan("解锁不祥宝库后未确认钥匙消耗或宝库状态变化")
        self._done = True
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        ok = getattr(self, "_done", False) or (bool(before_counts) and state.count("ominous_trial_key") < before_counts.get("ominous_trial_key", 0))
        return "" if ok else "未确认解锁不祥试炼宝库"

@register
class WinMicroVillageRaid(_OminousCombatSkill):
    name, goal, description = "win_micro_village_raid", "t4:hero_of_the_village", "搭建微型隔离村碉堡并亲手补刀清扫袭击赢取村庄英雄"
    params = {"prep_only": Param(bool, "仅搭建微型隔离村碉堡前置不触发袭击", default=False)}
    @classmethod
    def blocked(cls, state: State) -> str:
        if msg := _gate_base(state, "背包为空，缺少微型村碉堡材料或袭击战斗装备"): return msg
        has_combat = _has_weapon(state) and (state.count("ominous_bottle") >= 1 or _has_effect(state.st, ("raid_omen", "bad_omen")))
        has_prep = total_placeable(state) >= 12 and (_find_bed(state) is not None or state.count("bell") >= 1)
        return "" if (has_combat or has_prep) else "缺少袭击战斗条件（武器+不祥之瓶/袭击之兆）或微型村碉堡材料（>=12方块+床或钟）"
    def _setup_bunker(self, ex: Executor, state: State, st: dict, dim: str) -> Generator[dict, None, None]:
        memo, vills = memo_for(state), _get_entities(ex, radius=32, types=("villager",))
        if not vills and (vlms := memo.landmarks("village", dimension=dim)):
            vdest = [vlms[0][0] + 0.5, float(vlms[0][1]), vlms[0][2] + 0.5]
            yield rec("move_to", ex.move_to(pos=vdest, rng=4), pos=vdest)
            st, vills = state.refresh_status(ex.client, max_age_s=0), _get_entities(ex, radius=32, types=("villager",))
        if not vills:
            raise EnvBlocked("附近未发现村民以搭建微型村碉堡", missing_predicate="villager", searched_volume="半径 32 格与村庄地标", wake_on="chunk_moved")
        vill = min(vills, key=lambda e: eye_to_point_dist(st, e["pos"]))
        vx, vy, vz = block_cell(vill["pos"])
        if eye_to_point_dist(st, vill["pos"]) > 4.5:
            near = [vx + 2.5, float(vy), vz + 0.5]
            yield rec("move_to", ex.move_to(pos=near, rng=1), pos=near); st = state.refresh_status(ex.client, max_age_s=0)
        enclosure = [(vx + dx, vy, vz + dz) for dx, dz in _CARDINALS] + [(vx + dx, vy + 1, vz + dz) for dx, dz in _CARDINALS] + [(vx, vy + 2, vz)]
        for idx, cell in enumerate(enclosure):
            if total_placeable(state) <= (4 if idx >= 4 else 0): break
            if _is_air(ex, cell) and not _overlaps(cell, st) and can_reach_block(ex, st, cell, reach=DIG_EYE_REACH):
                yield from _place_cell(ex, state, pick_placeable(state), cell); refresh_inventory(ex, state)
        st = state.refresh_status(ex.client, max_age_s=0)
        if (bed := _find_bed(state)) and (bcell := _find_place_near(ex, st, (vx, vy, vz), bed_footprint=True)):
            yield from _place_cell(ex, state, bed, bcell); memo.record_station(bed, bcell, dimension=dim); refresh_inventory(ex, state)
        if state.count("bell") >= 1 and (bell_cell := _find_place_near(ex, st, (vx, vy, vz))):
            yield from _place_cell(ex, state, "bell", bell_cell); memo.record_station("bell", bell_cell, dimension=dim); refresh_inventory(ex, state)
        if total_placeable(state) >= 4:
            px, py, pz = block_cell(feet_pos(state.refresh_status(ex.client, max_age_s=0)))
            dx, dz = next(((ox, oz) for ox, oz in _CARDINALS if (px + ox, py, pz + oz) != (vx, vy, vz) and _is_air(ex, (px + ox, py, pz + oz))), (1, 0))
            tower = [[px + (dx if i % 2 == 0 else 0), py + i, pz + (dz if i % 2 == 0 else 0)] for i in range(4)]
            yield from step_up_1x2(ex, state, tower)
            top = [tower[-1][0] + 0.5, float(tower[-1][1] + 1), tower[-1][2] + 0.5]
            yield rec("move_to", ex.move_to(pos=top, rng=1), pos=top)
        memo.record_landmark("micro_village_bunker", (vx, vy, vz), dimension=dim)
    def _run(self, ex: Executor, state: State):
        self._bunker_ready, self._bot_killed_raiders, self._saw_evoker, self._looted_totem, self._won = False, 0, False, False, False
        st, dim = self._begin(ex, state), _dim(state)
        if not memo_for(state).has_landmark("micro_village_bunker", dimension=dim): yield from self._setup_bunker(ex, state, st, dim)
        self._bunker_ready = True
        if self.args["prep_only"]: return
        st = state.refresh_status(ex.client, max_age_s=0); refresh_inventory(ex, state)
        if not _has_effect(st, ("raid_omen", "bad_omen")):
            if (bottle := _select_lowest_ominous_bottle(state)) is None: raise Replan("背包缺少不祥之瓶（ominous_bottle）且无袭击之兆效果")
            # /equip("ominous_bottle") 与 /use_item("ominous_bottle") 均按物品名称选取，不接受背包槽位编号
            yield from _equip_slot(ex, state, "ominous_bottle", "hand")
            yield rec("use_item", ex.use_item("ominous_bottle"), item="ominous_bottle", bottle_rank=_bottle_rank(bottle), api_selects_by_name=True); refresh_inventory(ex, state)
        if not _has_weapon(state): raise Replan("背包缺少武器，无法在袭击中完成亲手补刀")
        waves_seen, in_wave, silent_polls = 0, False, 0
        try:
            if state.count("shield") >= 1: _set_posture(ex, state, shield=True)
            for _ in range(45):
                st = state.refresh_status(ex.client, max_age_s=0)
                if not st.get("alive", True) or float(st.get("health", 20)) < 6.0: raise Replan("袭击防守血量过低（< 6.0），中止交战")
                if self._bot_killed_raiders >= 1 and _won_hero(ex, st): self._won = True; break
                raiders = _get_entities(ex, radius=48, types=_RAIDERS)
                if not raiders:
                    in_wave = False
                    if silent_polls == 0 and (bells := _find_blocks(ex, ["bell"], radius=24, limit=4)):
                        bpos = block_cell(min(bells, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
                        if not can_reach_block(ex, st, bpos, reach=STATION_EYE_REACH): yield from _approach_block(ex, state, bpos, reach=STATION_EYE_REACH)
                        yield rec("interact", ex.interact(pos=list(bpos)), pos=list(bpos)); raiders = _get_entities(ex, radius=48, types=_RAIDERS)
                    if not raiders:
                        st = state.refresh_status(ex.client, max_age_s=0)
                        if self._bot_killed_raiders >= 1 and _won_hero(ex, st): self._won = True; break
                        silent_polls += 1; has_omen = _has_effect(st, ("raid_omen", "bad_omen"))
                        if (waves_seen == 0 and ((not has_omen and silent_polls >= 3) or silent_polls >= 8)) or (waves_seen >= 1 and not has_omen and silent_polls >= 15) or silent_polls >= 18: break
                        if _RAID_WAVE_POLL_S > 0: time.sleep(_RAID_WAVE_POLL_S)
                        yield rec("wait_raid", {"silent_polls": silent_polls, "waves_seen": waves_seen}); continue
                if not in_wave: waves_seen += 1; in_wave = True
                silent_polls = 0
                if any(bare(r.get("name")) == "evoker" for r in raiders): self._saw_evoker = True
                tgt = min(raiders, key=lambda r: (_RAIDER_PRIO.get(bare(r.get("name")), 9), eye_to_point_dist(st, r["pos"])))
                tid, tname, tpos = tgt["id"], bare(tgt.get("name")), [float(v) for v in tgt["pos"]]
                refresh_inventory(ex, state)
                if (ranged := _pick_ranged(state)) is not None:
                    yield from _equip_slot(ex, state, ranged, "hand")
                    receipt = ex.shoot(entity_id=tid, weapon=ranged); yield rec("shoot", receipt, entity_id=tid, weapon=ranged)
                else:
                    if (melee := _pick_melee(state)) is not None: yield from _equip_slot(ex, state, melee, "hand")
                    if eye_to_point_dist(st, tgt["pos"]) > ENTITY_EYE_REACH: yield rec("move_to", ex.move_to(pos=tpos, rng=2), pos=tpos)
                    receipt = ex.attack(entity_id=tid, max_hits=6); yield rec("attack", receipt, entity_id=tid, max_hits=6)
                if (isinstance(receipt, Mapping) and receipt.get("killed") is True) or all(e["id"] != tid for e in _get_entities(ex, radius=48, types=(tname,))):
                    self._bot_killed_raiders += 1
                if self._saw_evoker and not self._looted_totem:
                    refresh_inventory(ex, state)
                    if state.count("totem_of_undying") < 1:
                        drops = _get_entities(ex, radius=24, types=("item",))
                        lpos = [float(v) for v in min(drops, key=lambda e: eye_to_point_dist(st, e["pos"]))["pos"]] if drops else (tpos if tname == "evoker" else None)
                        if lpos is not None: yield rec("move_to", ex.move_to(pos=lpos, rng=1), pos=lpos); refresh_inventory(ex, state)
                    self._looted_totem = state.count("totem_of_undying") >= 1
        finally: _reset_posture(ex, state)
        if self._bot_killed_raiders < 1: raise Replan("袭击波次中 Bot 未完成至少 1 次亲手补刀，无法保证触发村庄英雄进度")
        if not self._won and _won_hero(ex, state.refresh_status(ex.client, max_age_s=0)): self._won = True
        if not self._won: raise Replan("袭击波次结束但未确认达成村庄英雄（hero_of_the_village）")
        if waves_seen >= 3 and not self._saw_evoker and not self._looted_totem and state.count("totem_of_undying") < 1:
            bpos = (memo_for(state).landmarks("micro_village_bunker", dimension=dim) or [block_cell(feet_pos(st))])[0]
            memo_for(state).record_landmark("raid_no_evoker_easy", bpos, dimension=dim)
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        if self.args["prep_only"]:
            return "" if (getattr(self, "_bunker_ready", False) or memo_for(state).has_landmark("micro_village_bunker", dimension=_dim(state))) else "未确认微型隔离村碉堡搭建完成"
        return "" if (getattr(self, "_won", False) and getattr(self, "_bot_killed_raiders", 0) >= 1) else "尚未完成含亲手补刀的村庄袭击防守"

@register
class TriggerTotemSafely(_OminousCombatSkill):
    name, goal, description = "trigger_totem_safely", "t4:totem_of_undying", "无摔落保护状态副手持不死图腾并从实心高柱受控坠落安全触发图腾复活"
    params: dict[str, Param] = {}
    @classmethod
    def blocked(cls, state: State) -> str:
        if msg := _gate_base(state, "背包为空，缺少不死图腾与垫高方块"): return msg
        if state.count("totem_of_undying") >= 1:
            armor_ok, _, req_height, _, _ = assess_totem_armor(state.inventory, state.st)
            return "" if (not armor_ok or total_placeable(state) >= req_height or _is_elevated(state)) else f"背包垫高方块不足 {req_height} 个，无法搭设受控坠落高柱"
        return "" if _is_easy_or_peaceful(state) else "背包缺少不死图腾（totem_of_undying）"
    def _run(self, ex: Executor, state: State):
        self._triggered, st = False, self._begin(ex, state)
        if state.count("totem_of_undying") < 1:
            if _is_easy_or_peaceful(state):
                raise EnvBlocked("服务端难度为 easy/peaceful，袭击不刷唤魔者且背包无不死图腾（禁止盲找林地府邸）",
                                 missing_predicate="totem_of_undying_or_normal_plus_difficulty", searched_volume="runtime_difficulty_guard", wake_on="inventory_changed")
            raise Replan("背包尚无不死图腾，需先通过普通及以上难度袭击击杀唤魔者获取")
        armor_ok, armor_reason, req_height, req_min_drop, allow_armored = assess_totem_armor(state.inventory, st)
        if not armor_ok: raise Replan(armor_reason)
        yield from _equip_slot(ex, state, "totem_of_undying", "off-hand")
        st = state.refresh_status(ex.client, max_age_s=0)
        if bare(((state.inventory or {}).get("offhand") or {}).get("name")) != "totem_of_undying": raise Replan("副手装备不死图腾未确认，禁止执行受控坠落")
        px, py, pz = block_cell(feet_pos(st))
        if not _is_elevated(state):
            top_z_off = -(1 if (req_height - 1) % 2 == 0 else 0)
            for chk in ([px + 1, py - 1, pz], [px + 1, py - 1, pz + top_z_off]):
                landing = observe_block(ex, chk)
                if not is_safe_solid_floor(landing) or _is_unsafe_totem_landing_block(bare(landing.get("name"))):
                    raise Replan(f"落点 {chk} 非安全实心地面（{bare(landing.get('name')) or 'unknown'}），禁止受控坠落")
            if total_placeable(state) < req_height: raise Replan(f"背包垫高方块不足 {req_height} 个，无法安全搭设受控坠落高柱")
            try:
                pillar = [[px, py + i, pz - (1 if i % 2 == 0 else 0)] for i in range(req_height)]
                yield from step_up_1x2(ex, state, pillar)
                top = [pillar[-1][0] + 0.5, float(pillar[-1][1] + 1), pillar[-1][2] + 0.5]
                yield rec("move_to", ex.move_to(pos=top, rng=1), pos=top)
                px, py, pz = block_cell(feet_pos(state.refresh_status(ex.client, max_age_s=0)))
                memo_for(state).record_landmark("totem_pillar", (px, py, pz), dimension=_dim(state))
            finally: _reset_posture(ex, state)
        landing_col = (px + 1, pz)
        refresh_inventory(ex, state); state.refresh_status(ex.client, max_age_s=0)
        ok, reason, _ = validate_controlled_totem_conditions(state, ex.client, drop_height=req_height, landing_col=landing_col, min_drop=req_min_drop, allow_armored=allow_armored)
        if not ok: raise Replan(f"高柱顶安全落点校验未通过（{reason}），禁止受控坠落")
        try: ex.look(pos=[px + 1.5, float(py), pz + 0.5])
        except (ApiError, Replan): pass
        before_totems = state.count("totem_of_undying")
        with controlled_totem_trigger(state, ex.client, drop_height=req_height, landing_col=landing_col, min_drop=req_min_drop, max_duration_s=3.5, allow_armored=allow_armored):
            yield rec("control", ex.control(forward=True, ms=450), forward=True, ms=450)
            deadline = time.monotonic() + 2.5
            for poll_idx in range(12):
                after_st = state.refresh_status(ex.client, max_age_s=0); refresh_inventory(ex, state)
                if state.count("totem_of_undying") < before_totems or _has_effect(after_st, ("regeneration", "absorption")): self._triggered = True; break
                if not after_st.get("alive", True) or after_st.get("on_ground") is True or poll_idx >= 11 or (rem := deadline - time.monotonic()) <= 0: break
                if _TOTEM_FALL_POLL_S > 0: time.sleep(min(_TOTEM_FALL_POLL_S, rem))
        if not self._triggered: raise Replan("受控坠落后未确认不死图腾消耗或再生/伤害吸收效果")
    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        return "" if getattr(self, "_triggered", False) else "尚未确认受控坠落触发不死图腾"
