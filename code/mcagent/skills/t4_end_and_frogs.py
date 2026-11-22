"""Stage 3 Wave A T4 技能：末地船鞘翅掠夺（+60）、三变体青蛙顺序单拴（+60）与三种蛙明灯收集（+60）。"""
from __future__ import annotations

import math
from collections.abc import Generator, Iterator, Mapping, Sequence
from typing import Any

from ..client import ApiError
from ..runtime import EnvBlocked, Executor, Replan, State
from . import nav
from .base import Skill, failure, rec
from .field import scan_blocks, scan_entities
from .interaction import _clear_posture
from .items import AIR, NAV_FAIL_CODES, bare, is_safe_solid_floor, total_placeable
from .landmark_nav import goto_known_landmark_or_biome
from .observations import observe_block, refresh_inventory
from .registry import register
from .sightline import (
    DIG_EYE_REACH, ENTITY_EYE_REACH, STATION_EYE_REACH, block_cell, block_center,
    can_reach_block, can_see_block, eye_to_block_dist, eye_to_point_dist, feet_pos,
)
from .voxel_cache import voxel_cache_for
from .world_memo import memo_for

_FRAME_TYPES = ("item_frame", "glow_item_frame")
_SHIP_BLOCKS = ("purpur_block", "purpur_stairs", "brewing_stand", "dragon_head")
_FROG_ADV, _FROG_VARIANTS = "minecraft:husbandry/leash_all_frog_variants", ("temperate", "warm", "cold")
_FROGLIGHT_ADV = "minecraft:husbandry/froglights"
_FROGLIGHT_BY_VARIANT: dict[str, str] = {
    "temperate": "ochre_froglight",
    "cold": "verdant_froglight",
    "warm": "pearlescent_froglight",
}
_FROGLIGHT_ITEMS: tuple[str, ...] = ("ochre_froglight", "verdant_froglight", "pearlescent_froglight")
_MAGMA_ANCHORS = "basalt_deltas|bastion_remnant"
_MELEE_WEAPONS = (
    "netherite_sword", "diamond_sword", "iron_sword", "stone_sword",
    "netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "mace", "wooden_sword",
)
_HOSTILE_THREATS = (
    "zombie", "skeleton", "creeper", "spider", "drowned", "husk", "stray",
    "enderman", "pillager", "hoglin", "piglin_brute", "ghast", "blaze", "wither_skeleton",
)
_COLD_BIOMES = frozenset({
    "deep_dark", "snowy_plains", "snowy_taiga", "taiga", "old_growth_pine_taiga", "old_growth_spruce_taiga",
    "grove", "snowy_slopes", "frozen_peaks", "jagged_peaks", "frozen_river", "frozen_ocean", "deep_frozen_ocean",
    "ice_spikes", "end_highlands", "end_midlands", "the_end", "small_end_islands", "end_barrens",
})
_WARM_BIOMES = frozenset({
    "desert", "savanna", "savanna_plateau", "windswept_savanna", "badlands", "eroded_badlands", "wooded_badlands",
    "mangrove_swamp", "jungle", "sparse_jungle", "bamboo_jungle", "warm_ocean", "nether_wastes", "crimson_forest",
    "warped_forest", "soul_sand_valley", "basalt_deltas",
})
_TEMPERATE_BIOMES = frozenset({
    "swamp", "meadow", "plains", "forest", "birch_forest", "dark_forest",
    "flower_forest", "sunflower_plains", "cherry_grove", "river", "beach", "lush_caves",
})
_BIOME_LANDMARKS: dict[str, str] = {
    "cold": "|".join(("cold_biome", *sorted(_COLD_BIOMES))),
    "warm": "|".join(sorted(_WARM_BIOMES)),
    "temperate": "|".join(sorted(_TEMPERATE_BIOMES)),
}

def _dim(src: Any) -> str:
    raw = src.get("dimension") if isinstance(src, Mapping) else (getattr(src, "st", None) or {}).get("dimension")
    n = bare(raw).strip().lower()
    return "the_end" if n in ("the_end", "end") else ("the_nether" if n in ("the_nether", "nether") else (n or "overworld"))

def _biome_temp(dim: str, biome: str) -> str:
    d, b = bare(dim).strip().lower(), bare(biome).strip().lower()
    return "cold" if (d in ("the_end", "end") or b in _COLD_BIOMES) else ("warm" if (d in ("the_nether", "nether") or b in _WARM_BIOMES) else "temperate")

def _is_empty_bag(state: State) -> bool:
    inv = state.inventory or {}
    if any(isinstance(it, dict) and int(it.get("count", 0)) > 0 for it in (inv.get("items") or ())): return False
    armor = inv.get("armor") or {}
    extras = (*(armor.values() if isinstance(armor, dict) else ()), inv.get("held"), inv.get("offhand"))
    return not any(isinstance(eq, dict) and eq.get("name") and int(eq.get("count", 1)) > 0 for eq in extras)

def _is_preemption(exc: BaseException) -> bool: return failure(exc)[0] in ("replan", "interrupted") or "抢占" in str(exc)

def _inv_names(state: State) -> list[str]:
    inv, out, seen = state.inventory or {}, [], set()
    for row in (*(inv.get("items") or ()), inv.get("held"), inv.get("offhand")):
        if isinstance(row, Mapping) and (n := bare(row.get("name"))) and n not in seen:
            seen.add(n)
            out.append(n)
    return out

def _pick_weapon(state: State) -> tuple[str, str] | None:
    melee = next((w for w in _MELEE_WEAPONS if state.count(w) >= 1), None) or next(
        (n for n in _inv_names(state) if state.count(n) >= 1 and (n.endswith(("_sword", "_axe")) or n == "mace")), None
    )
    if melee is not None:
        return (melee, "melee")
    if state.count("arrow") < 1:
        return None
    return next(((r, "ranged") for r in ("crossbow", "bow") if state.count(r) >= 1), None)

def _reset_posture(ex: Executor, state: State, *, force: bool = False) -> None:
    st = state.st or {}
    if force or st.get("sneaking") or st.get("blocking"):
        try:
            _clear_posture(ex, state, sneak=False, shield=False)
            if isinstance(state.st, dict):
                state.st.update(sneaking=False, blocking=False)
        except Exception:
            pass

def _scan_ready(ex: Executor, state: State, types: Any, radius: int, *, entity: bool = False, limit: int = 16, skill: str = "") -> list[dict]:
    scan = scan_entities(ex, state, types, radius=radius) if entity else scan_blocks(ex, state, types, radius=radius, limit=limit, skill_name=skill)
    if scan.get("ready") is False: raise Replan("世界加载尚未就绪（ready=false）")
    return scan["entities" if entity else "blocks"]

def _query_advancements(ex: Executor) -> dict | None:
    if not callable(fn := getattr(ex.client, "advancements", None)): return None
    try: return fn(status="all", include_recipes=False)
    except TypeError:
        try: return fn()
        except (ApiError, AttributeError): return None
    except (ApiError, AttributeError): return None

def _criteria_remaining(state: State, adv_id: str, *, expected_criteria: Sequence[str] | None = None, advancements: Any = None) -> tuple[str, ...]:
    try:
        from ..loop.subgoals import advancement_criteria_remaining
        return tuple(advancement_criteria_remaining(state, adv_id, expected_criteria=expected_criteria, advancements=advancements))
    except ImportError:
        expected = tuple(sorted(str(c) for c in (expected_criteria or ())))
        rows = advancements.get("advancements") if isinstance(advancements, Mapping) else advancements
        row = next((r for r in rows if isinstance(r, Mapping) and bare(r.get("id")) == bare(adv_id)), None) if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)) else None
        if row is None: return expected
        if row.get("status") == "completed" or row.get("done") is True: return ()
        done = {bare(str(k)) for k, v in (row.get("criteria") or {}).items() if v} if isinstance(row.get("criteria"), Mapping) else set()
        for k in ("completed_criteria", "criteria_done"):
            if isinstance(val := row.get(k), Sequence) and not isinstance(val, (str, bytes)): done.update(bare(str(c)) for c in val)
        return tuple(c for c in expected if bare(c) not in done)

def _is_air(ex: Executor, cell: tuple[int, int, int]) -> bool: return bare(observe_block(ex, list(cell)).get("name")) in AIR
def _is_solid(ex: Executor, cell: tuple[int, int, int]) -> bool: return is_safe_solid_floor(observe_block(ex, list(cell)))
def _standable(ex: Executor, cell: tuple[int, int, int]) -> bool:
    x, y, z = cell
    return _is_solid(ex, (x, y - 1, z)) and _is_air(ex, (x, y, z)) and _is_air(ex, (x, y + 1, z))

def _standable_near(ex: Executor, target: tuple[int, int, int], st: dict, *, max_r: int = 2, max_eye: float = 3.0) -> list[float] | None:
    tx, ty, tz = target
    cands = [[tx + dx + 0.5, float(ty + dy), tz + dz + 0.5]
             for dy in (0, -1, 1) for dx in range(-max_r, max_r + 1) for dz in range(-max_r, max_r + 1)
             if not (dx == 0 and dz == 0 and dy in (0, -1)) and _standable(ex, (tx + dx, ty + dy, tz + dz))
             and eye_to_block_dist([tx + dx + 0.5, float(ty + dy), tz + dz + 0.5], target) <= max_eye]
    return min(cands, key=lambda p: eye_to_point_dist(st, p), default=None)

def _plan_gap_toward(ex: Executor, st: dict, target: tuple[int, int, int], *, max_len: int = 8) -> list[tuple[int, int, int]]:
    fx, fy, fz = block_cell(feet_pos(st)); tx, _, tz = target; cx, cz, gaps = fx, fz, []
    for _ in range(max_len + 12):
        if (cx, cz) == (tx, tz): break
        dx, dz = tx - cx, tz - cz
        cx, cz = (cx + (1 if dx > 0 else -1), cz) if abs(dx) >= abs(dz) else (cx, cz + (1 if dz > 0 else -1))
        if not (_is_air(ex, (cx, fy, cz)) and _is_air(ex, (cx, fy + 1, cz))): break
        if _is_solid(ex, (cx, fy - 1, cz)):
            if gaps: break
        elif _is_air(ex, (cx, fy - 1, cz)):
            gaps.append((cx, fy - 1, cz))
            if len(gaps) >= max_len: break
        else: break
    return gaps

def _is_enclosed_1x1_pit(ex: Executor, cell: tuple[int, int, int]) -> bool:
    """检查目标格四面是否均为实心方块且上方为空气（1×1 防脱逃水坑）。"""
    x, y, z = cell
    return _is_air(ex, (x, y + 1, z)) and all(
        _is_solid(ex, (x + dx, y, z + dz)) for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
    )

def _find_tadpole_spot(ex: Executor, state: State, st: dict, skill_name: str) -> tuple[tuple[int, int, int], bool] | None:
    """定位或规划 1×1 蝌蚪释放水坑，返回 `(cell, needs_dig)`。"""
    waters = _scan_ready(ex, state, "water", 16, limit=8, skill=skill_name)
    enclosed_waters = [c for b in waters if _is_enclosed_1x1_pit(ex, c := block_cell(b["pos"]))]
    if enclosed_waters:
        return (min(enclosed_waters, key=lambda c: eye_to_block_dist(st, c)), False)
    fx, fy, fz = block_cell(feet_pos(st))
    dig_pits = [
        (fx + dx, fy - 1, fz + dz)
        for dx in range(-3, 4) for dz in range(-3, 4)
        if (dx or dz)
        and _is_solid(ex, (fx + dx, fy - 1, fz + dz))
        and _is_solid(ex, (fx + dx, fy - 2, fz + dz))
        and _is_enclosed_1x1_pit(ex, (fx + dx, fy - 1, fz + dz))
        and can_reach_block(ex, st, (fx + dx, fy - 1, fz + dz), reach=DIG_EYE_REACH)
    ]
    if dig_pits:
        return (min(dig_pits, key=lambda c: eye_to_block_dist(st, c)), True)
    if waters:
        return (block_cell(min(waters, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"]), False)
    cands = [
        (fx + dx, fy - 1, fz + dz)
        for dx in range(-3, 4) for dz in range(-3, 4)
        if (dx or dz) and _is_solid(ex, (fx + dx, fy - 1, fz + dz)) and _is_air(ex, (fx + dx, fy, fz + dz))
        and can_reach_block(ex, st, (fx + dx, fy - 1, fz + dz), reach=STATION_EYE_REACH)
    ]
    best = min(cands, key=lambda c: eye_to_block_dist(st, c), default=None)
    return (best, False) if best is not None else None

def _frog_variant(ent: Mapping[str, Any]) -> str | None:
    meta = ent.get("metadata") if isinstance(ent.get("metadata"), Mapping) else {}
    for raw in (ent.get("variant"), ent.get("frog_variant"), meta.get("variant"), ent.get("color")):
        if not isinstance(raw, str) or not raw.strip():
            continue
        val = bare(raw).strip().lower()
        if val in ("temperate", "orange", "ochre"):
            return "temperate"
        if val in ("cold", "green", "verdant"):
            return "cold"
        if val in ("warm", "white", "pearlescent"):
            return "warm"
    return None

def _matching_frogs(frogs: Sequence[dict], missing_variants: Sequence[str]) -> list[dict]:
    wanted = set(missing_variants)
    explicit = [f for f in frogs if _frog_variant(f) in wanted]
    if explicit:
        return explicit
    return [f for f in frogs if _frog_variant(f) is None]

def _is_small_magma_cube(ent: Mapping[str, Any]) -> bool:
    meta = ent.get("metadata") if isinstance(ent.get("metadata"), Mapping) else {}
    for key in ("size", "scale"):
        raw = ent.get(key) if ent.get(key) is not None else meta.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw) <= 1.0
    height = ent.get("height")
    if isinstance(height, (int, float)) and not isinstance(height, bool):
        return float(height) <= 0.65
    for key in ("max_health", "health"):
        raw = ent.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw) <= 2.0
    return True

def _equip_hand(ex: Executor, state: State, item: str) -> Generator[dict, None, None]:
    held = (state.inventory or {}).get("held") or (state.st or {}).get("held") or {}
    if bare(held.get("name")) != bare(item):
        yield rec("equip", ex.equip(bare(item), "hand"), item=bare(item), slot="hand")

def _is_adv_done(adv_snap: Any, adv_id: str) -> bool:
    if not isinstance(adv_snap, Mapping):
        return False
    target = bare(adv_id)
    comp = adv_snap.get("completed")
    if isinstance(comp, Sequence) and not isinstance(comp, (str, bytes)):
        if any(bare(str(x)) == target for x in comp):
            return True
    rows = adv_snap.get("advancements")
    if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
        for row in rows:
            if isinstance(row, Mapping) and bare(row.get("id")) == target:
                if row.get("status") == "completed" or row.get("done") is True:
                    return True
    return False

def _safe_ship_deck_pt(ex: Executor, state: State, st: dict, ship_blks: list[dict], frame_cells: set[tuple[int, int, int]]) -> list[float] | None:
    cands = []
    for blk in ship_blks:
        bx, by, bz = block_cell(blk["pos"]); deck = (bx, by + 1, bz)
        if deck not in frame_cells and _standable(ex, deck):
            cands.append((0 if (_is_air(ex, (bx, by + 3, bz)) and _is_air(ex, (bx, by + 4, bz))) else 1, -by, eye_to_block_dist(st, deck), deck))
    if not cands: return None
    best = min(cands)[3]; return [best[0] + 0.5, float(best[1]), best[2] + 0.5]

def _ensure_crafting_table(ex: Executor, state: State, skill_name: str, st: dict) -> Generator[dict, None, tuple[int, int, int]]:
    if tables := _scan_ready(ex, state, "crafting_table", 16, limit=4, skill=skill_name):
        tpos = block_cell(min(tables, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
    else:
        if state.count("crafting_table") < 1:
            inv = state.inventory or {}
            planks = sum(int(r.get("count", 0)) for r in (*(inv.get("items") or ()), inv.get("held"), inv.get("offhand"))
                         if isinstance(r, Mapping) and bare(r.get("name")).endswith("_planks"))
            if planks >= 4: yield rec("craft", ex.craft("crafting_table", 1), item="crafting_table", count=1); refresh_inventory(ex, state)
        if state.count("crafting_table") < 1:
            raise EnvBlocked("合成拴绳需要工作台，附近无工作台且缺少 crafting_table 或木板", missing_predicate="crafting_table", searched_volume="半径 16 格", wake_on="chunk_moved")
        fx, fy, fz = block_cell(feet_pos(st))
        cands = [(fx + dx, fy + dy, fz + dz) for dy in (0, -1, 1) for dx in range(-3, 4) for dz in range(-3, 4)
                 if (dx or dz or dy) and _standable(ex, (fx + dx, fy + dy, fz + dz)) and can_reach_block(ex, st, (fx + dx, fy + dy, fz + dz), reach=STATION_EYE_REACH)]
        if not cands: raise EnvBlocked("附近缺少可放置工作台的平整地面", missing_predicate="crafting_table", searched_volume="玩家周边 3 格", wake_on="chunk_moved")
        tpos = min(cands, key=lambda c: eye_to_block_dist(st, c))
        yield rec("place", ex.place("crafting_table", list(tpos)), item="crafting_table", pos=list(tpos)); refresh_inventory(ex, state)
    if eye_to_block_dist(state.refresh_status(ex.client, max_age_s=0), tpos) > STATION_EYE_REACH:
        dest = _standable_near(ex, tpos, st, max_r=2, max_eye=STATION_EYE_REACH) or list(block_center(tpos))
        yield rec("move_to", ex.move_to(pos=dest, rng=1), pos=dest)
    return tpos


@register
class LootEndShipElytra(Skill):
    name, goal, verified = "loot_end_ship_elytra", "t4:end_elytra", "live"
    description = "登临末地船击落鞘翅展示框并拾取鞘翅"
    params = {}

    @classmethod
    def blocked(cls, state: State) -> str:
        if not state.ready:
            return "状态尚未就绪"
        if _is_empty_bag(state):
            return "背包为空，缺少登船方块或末影珍珠"
        if _dim(state) != "the_end":
            return "当前不在末地，需先进入末地"
        if total_placeable(state) < 4 and state.count("ender_pearl") < 1 and state.count("elytra") < 1:
            return "背包缺少登船方块（至少 4 块）或末影珍珠"
        return ""

    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        return "" if state.count("elytra") >= 1 else "背包尚未拾取到鞘翅"

    def run(self, ex: Executor, state: State) -> Iterator[dict]:
        st = state.refresh_status(ex.client, max_age_s=0)
        refresh_inventory(ex, state)
        if _dim(st) != "the_end":
            raise Replan("当前不在末地，无法掠夺末地船鞘翅")
        if state.count("elytra") >= 1:
            return
        if reason := self.blocked(state):
            raise Replan(reason)

        frames = _scan_ready(ex, state, _FRAME_TYPES, 48, entity=True)
        ship_blks = _scan_ready(ex, state, _SHIP_BLOCKS, 48, limit=32, skill=self.name)
        dest_cell: tuple[int, int, int] | None = None
        if frames:
            dest_cell = block_cell(min(frames, key=lambda e: eye_to_point_dist(st, e["pos"]))["pos"])
        elif ship_blks:
            best = min(ship_blks, key=lambda b: eye_to_block_dist(st, b["pos"]))
            dest_cell = block_cell(best["pos"])
            memo_for(state).record_landmark("end_city", dest_cell, dimension="the_end", meta={"block": best["name"]})
        elif lms := memo_for(state).landmarks("end_city", dimension="the_end"):
            dest_cell = min(lms, key=lambda c: eye_to_block_dist(st, c))
        if dest_cell is None:
            raise EnvBlocked("末地船/鞘翅展示框未发现", missing_predicate="end_ship_item_frame",
                             searched_volume="半径 48 格实体与末地船方块及台账", wake_on="chunk_moved")

        shield_raised = posture_touched = False
        try:
            if _scan_ready(ex, state, "shulker", 20, entity=True) and state.count("shield") >= 1:
                ex.posture(shield=True)
                shield_raised = posture_touched = True

            st = state.refresh_status(ex.client, max_age_s=0)
            dist = eye_to_block_dist(st, dest_cell)
            needs_gap = bool(_plan_gap_toward(ex, st, dest_cell, max_len=8)) or abs(dest_cell[1] - feet_pos(st)[1]) >= 3.0
            dest_pt = list(block_center(dest_cell))
            frame_cells = {block_cell(f["pos"]) for f in frames}
            deck_pt = _safe_ship_deck_pt(ex, state, st, ship_blks, frame_cells)
            if needs_gap and deck_pt is not None and state.count("ender_pearl") >= 1 and float(st.get("health", 20)) > 8.0 and dist > 6.0:
                yield rec("use_item", ex.use_item("ender_pearl", target={"pos": deck_pt}),
                          item="ender_pearl", target={"pos": deck_pt})
                refresh_inventory(ex, state)
            elif dist > 3.2:
                try:
                    yield rec("move_to", ex.move_to(pos=dest_pt, rng=2), pos=dest_pt)
                except Replan as exc:
                    if _is_preemption(exc) or (code := failure(exc)[0]) not in (*NAV_FAIL_CODES, "timeout"):
                        raise
                    yield rec("move_to", None, pos=dest_pt, error_code=code)
                    st = state.refresh_status(ex.client, max_age_s=0)
                    refresh_inventory(ex, state)
                    bcells = _plan_gap_toward(ex, st, dest_cell, max_len=min(8, max(0, total_placeable(state))))
                    if not bcells or total_placeable(state) < len(bcells):
                        raise
                    posture_touched = True
                    yield from nav.sneak_bridge(ex, state, [list(c) for c in bcells])

            st = state.refresh_status(ex.client, max_age_s=0)
            near = _scan_ready(ex, state, _FRAME_TYPES, 16, entity=True) or [
                f for f in frames if eye_to_point_dist(st, f["pos"]) <= 16.0
            ]
            if not near:
                raise EnvBlocked("登船后 16 格内未发现鞘翅展示框", missing_predicate="end_ship_item_frame",
                                 searched_volume="末地船半径 16 格实体", wake_on="chunk_moved")
            frame = min(near, key=lambda e: eye_to_point_dist(st, e["pos"]))
            frame_id, frame_pos = frame["id"], [float(v) for v in frame["pos"]]
            frame_cell = block_cell(frame_pos)
            if eye_to_block_dist(st, frame_cell) > 3.0:
                approach = _standable_near(ex, frame_cell, st, max_r=2, max_eye=3.0) or frame_pos
                yield rec("move_to", ex.move_to(pos=approach, rng=1), pos=approach)
                st = state.refresh_status(ex.client, max_age_s=0)
                if eye_to_block_dist(st, frame_cell) > 3.5:
                    raise Replan("靠近鞘翅展示框后仍超出 3.5 格交互眼距")

            if shield_raised:
                ex.posture(shield=False)
                shield_raised = False
            # Step 0 T4-P1 实测协议：/attack(max_hits=1) -> 兜底 /look + /control(hit=True) -> 走近拾取
            try:
                yield rec("attack", ex.attack(entity_id=frame_id, max_hits=1), entity_id=frame_id, max_hits=1)
            except Replan as exc:
                if _is_preemption(exc):
                    raise
                yield rec("attack", None, entity_id=frame_id, max_hits=1, error_code=failure(exc)[0])
            refresh_inventory(ex, state)
            if state.count("elytra") >= 1:
                return
            try:
                ex.look(entity_id=frame_id)
            except (ApiError, Replan) as exc:
                if _is_preemption(exc) or failure(exc)[0] != "unsupported_api":
                    raise
            try:
                yield rec("control", ex.control(hit=True), hit=True, entity_id=frame_id)
            except Replan as exc:
                if _is_preemption(exc) or (code := failure(exc)[0]) != "unsupported_api":
                    raise
                yield rec("control", None, hit=True, entity_id=frame_id, error_code=code)
            refresh_inventory(ex, state)
            if state.count("elytra") >= 1:
                return
            yield rec("move_to", ex.move_to(pos=frame_pos, rng=1), pos=frame_pos, purpose="pickup_elytra")
            refresh_inventory(ex, state)
            if state.count("elytra") < 1:
                raise Replan("击落展示框并靠近拾取后仍未获得鞘翅")
        finally:
            _reset_posture(ex, state, force=posture_touched)


@register
class LeashFrogVariants(Skill):
    name, goal, verified = "leash_frog_variants", "t4:leash_all_frog_variants", "live"
    description = "顺序单拴寒温热三种变体青蛙并核验进度子条件"
    params = {}

    def __init__(self, **args: Any):
        super().__init__(**args)
        self._leashed_variants: set[str] = set()
        self._last_adv: dict | None = None

    @classmethod
    def blocked(cls, state: State) -> str:
        if not state.ready:
            return "状态尚未就绪"
        if _is_empty_bag(state):
            return "背包为空，缺少拴绳或 5 根线"
        return "" if (state.count("lead") >= 1 or state.count("string") >= 5) else "背包缺少拴绳（lead）或 5 根线（string）"

    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        rem = _criteria_remaining(state, _FROG_ADV, expected_criteria=_FROG_VARIANTS, advancements=self._last_adv)
        return "" if (not rem or self._leashed_variants) else f"尚未完成青蛙变体拴绳进度，剩余: {', '.join(rem)}"

    def _leash_frog(
        self, ex: Executor, state: State, st: dict, frog: dict, cur_temp: str, remaining: Sequence[str]
    ) -> Generator[dict, None, None]:
        frog_id = frog["id"]
        if eye_to_point_dist(st, frog["pos"]) > 3.5:
            yield rec("move_to", ex.move_to(entity_id=frog_id, rng=2), entity_id=frog_id)
            st = state.refresh_status(ex.client, max_age_s=0)
            if eye_to_point_dist(st, frog["pos"]) > ENTITY_EYE_REACH + 1.0:
                fresh = next((e for e in _scan_ready(ex, state, "frog", 16, entity=True) if e["id"] == frog_id), None)
                if fresh is None or eye_to_point_dist(st, fresh["pos"]) > ENTITY_EYE_REACH:
                    raise Replan("靠近青蛙后仍超出拴绳交互距离")
        before_rem = set(remaining)
        rcpt = ex.interact(entity_id=frog_id, item="lead")
        yield rec("interact", rcpt, entity_id=frog_id, item="lead")
        refresh_inventory(ex, state)
        self._last_adv = _query_advancements(ex)
        after_rem = set(_criteria_remaining(state, _FROG_ADV, expected_criteria=_FROG_VARIANTS, advancements=self._last_adv))
        if newly_done := (before_rem - after_rem):
            self._leashed_variants.update(newly_done)
            return
        if isinstance(rcpt, Mapping) and rcpt.get("changed") is not False and (
            rcpt.get("leashed") is True or (self._last_adv is None and rcpt.get("changed") is True)
        ):
            self._leashed_variants.add(cur_temp)
            return
        raise Replan(f"栓绳交互 {cur_temp} 青蛙后未确认进度子条件推进")

    def _safe_wait_for_frog(
        self, ex: Executor, state: State, cur_temp: str, remaining: Sequence[str]
    ) -> Generator[dict, None, None]:
        """在 1×1 水坑旁安全等待蝌蚪成长（可喂粘液球加速、发现敌对/低血立刻止损、成长后即刻拴绳）。"""
        posture_touched = False
        try:
            if state.count("shield") >= 1 and _scan_ready(ex, state, _HOSTILE_THREATS, 12, entity=True):
                ex.posture(shield=True)
                if isinstance(state.st, dict):
                    state.st["blocking"] = True
                posture_touched = True
            for _ in range(4):
                st = state.refresh_status(ex.client, max_age_s=0)
                if not st.get("alive", True) or float(st.get("health", 20)) < 10.0:
                    raise Replan("蝌蚪孵化等待期间血量低于 10，安全止损")
                hostiles = _scan_ready(ex, state, _HOSTILE_THREATS, 8, entity=True)
                if any(eye_to_point_dist(st, h["pos"]) <= 6.0 for h in hostiles):
                    raise Replan("蝌蚪孵化等待期间附近出现近身敌对生物，安全止损")
                if grown := _scan_ready(ex, state, "frog", 16, entity=True):
                    if posture_touched:
                        _reset_posture(ex, state, force=True)
                        posture_touched = False
                    frog = min(grown, key=lambda e: eye_to_point_dist(st, e["pos"]))
                    yield from self._leash_frog(ex, state, st, frog, cur_temp, remaining)
                    return
                if state.count("slime_ball") >= 1 and (tads := _scan_ready(ex, state, "tadpole", 12, entity=True)):
                    tad = min(tads, key=lambda e: eye_to_point_dist(st, e["pos"]))
                    if eye_to_point_dist(st, tad["pos"]) > ENTITY_EYE_REACH:
                        yield rec("move_to", ex.move_to(entity_id=tad["id"], rng=2), entity_id=tad["id"])
                    yield rec("interact", ex.interact(entity_id=tad["id"], item="slime_ball"),
                              entity_id=tad["id"], item="slime_ball", purpose="accelerate_tadpole")
                    refresh_inventory(ex, state)
                    continue
                break
        finally:
            _reset_posture(ex, state, force=posture_touched)
        raise Replan("waiting_tadpole_growth")

    def _release_tadpole_in_pit(
        self, ex: Executor, state: State, st: dict, cur_temp: str, remaining: Sequence[str]
    ) -> Generator[dict, None, None]:
        found = _find_tadpole_spot(ex, state, st, self.name)
        if found is None:
            raise EnvBlocked(
                f"当前 {cur_temp} 群系缺少可构筑 1×1 蝌蚪水坑的水源或平地",
                missing_predicate="tadpole_release_spot",
                searched_volume="半径 16 格水源及周边 3 格平地",
                wake_on="chunk_moved",
            )
        spot, needs_dig = found
        if eye_to_block_dist(st, spot) > STATION_EYE_REACH or not can_see_block(ex, st, spot):
            dest = _standable_near(ex, spot, st, max_r=2, max_eye=STATION_EYE_REACH) or list(block_center(spot))
            yield rec("move_to", ex.move_to(pos=dest, rng=2), pos=dest)
            st = state.refresh_status(ex.client, max_age_s=0)
        if needs_dig:
            rcpt = ex.dig(list(spot), collect=True)
            voxel_cache_for(state).invalidate(spot, dimension=_dim(st))
            yield rec("dig", rcpt, pos=list(spot), purpose="dig_tadpole_pit")
            st = state.refresh_status(ex.client, max_age_s=0)
        yield rec("interact", ex.interact(pos=list(spot), item="tadpole_bucket"),
                  pos=list(spot), item="tadpole_bucket")
        refresh_inventory(ex, state)
        yield from self._safe_wait_for_frog(ex, state, cur_temp, remaining)

    def _handle_matching_biome(
        self, ex: Executor, state: State, st: dict, cur_temp: str, remaining: Sequence[str]
    ) -> Generator[dict, None, bool]:
        if frogs := _scan_ready(ex, state, "frog", 32, entity=True):
            frog = min(frogs, key=lambda e: eye_to_point_dist(st, e["pos"]))
            yield from self._leash_frog(ex, state, st, frog, cur_temp, remaining)
            return True
        if state.count("tadpole_bucket") >= 1:
            yield from self._release_tadpole_in_pit(ex, state, st, cur_temp, remaining)
            return True
        if _scan_ready(ex, state, "tadpole", 16, entity=True):
            yield from self._safe_wait_for_frog(ex, state, cur_temp, remaining)
            return True
        return False

    def run(self, ex: Executor, state: State) -> Iterator[dict]:
        self._leashed_variants = set()
        st = state.refresh_status(ex.client, max_age_s=0)
        refresh_inventory(ex, state)
        if reason := self.blocked(state):
            raise Replan(reason)
        if state.count("lead") < 1:
            yield from _ensure_crafting_table(ex, state, self.name, st)
            yield rec("craft", ex.craft("lead", 1), item="lead", count=1)
            refresh_inventory(ex, state)
            if state.count("lead") < 1:
                raise Replan("合成拴绳后背包仍未获得 lead")
            st = state.refresh_status(ex.client, max_age_s=0)

        self._last_adv = _query_advancements(ex)
        remaining = _criteria_remaining(state, _FROG_ADV, expected_criteria=_FROG_VARIANTS, advancements=self._last_adv)
        if not remaining:
            return

        cur_dim = _dim(st)
        cur_temp = _biome_temp(cur_dim, str(st.get("biome") or ""))
        if cur_temp in remaining and (yield from self._handle_matching_biome(ex, state, st, cur_temp, remaining)):
            return

        for temp in remaining:
            query = _BIOME_LANDMARKS.get(temp, "")
            if query and (yield from goto_known_landmark_or_biome(ex, state, query, rng=2, min_dist=4.0)):
                st = state.refresh_status(ex.client, max_age_s=0)
                cur_dim = _dim(st)
                cur_temp = _biome_temp(cur_dim, str(st.get("biome") or ""))
                if cur_temp in remaining:
                    if (yield from self._handle_matching_biome(ex, state, st, cur_temp, remaining)):
                        return
                    raise EnvBlocked(
                        f"已抵达 {cur_temp} 群系锚点但附近无青蛙且缺少蝌蚪桶",
                        missing_predicate="tadpole_bucket",
                        searched_volume="半径 32 格实体与背包",
                        wake_on="inventory_changed",
                    )
                raise Replan(f"已向 {temp} 群系锚点移动但尚未进入目标温度群系（当前 {cur_temp}）")

        raise EnvBlocked(
            f"缺少剩余青蛙温度变体 {','.join(remaining)} 对应群系或实体",
            missing_predicate=f"frog_variant:{remaining[0]}",
            searched_volume="半径 32 格实体与世界台账群系地标",
            wake_on="chunk_moved",
        )


@register
class CollectFroglights(Skill):
    name, goal, verified = "collect_froglights", "t4:froglights", "live"
    description = "下界拆分岩浆怪引导青蛙吞食并收集三种蛙明灯"
    params = {}

    def __init__(self, **args: Any):
        super().__init__(**args)
        self._done: bool = False

    @staticmethod
    def _missing_variants(state: State) -> list[str]:
        return [v for v, item in _FROGLIGHT_BY_VARIANT.items() if state.count(item) < 1]

    @classmethod
    def blocked(cls, state: State) -> str:
        if not state.ready:
            return "状态尚未就绪"
        if not cls._missing_variants(state):
            return ""
        if _is_empty_bag(state):
            return "背包为空，缺少武器与蛙明灯"
        if _dim(state) != "the_nether":
            return "当前不在下界（the_nether），需先进入下界定位岩浆怪"
        if _pick_weapon(state) is None:
            return "背包缺少战斗武器（剑/斧/或配箭的弓/弩），无法拆分大/中型岩浆怪"
        if state.hp < 8:
            return f"生命值过低（{state.hp:.1f} < 8），不宜靠近岩浆怪收集蛙明灯"
        return ""

    def verify_done(self, state: State, before_counts: dict | None = None) -> str:
        missing = [item for item in _FROGLIGHT_ITEMS if state.count(item) < 1]
        if not missing or self._done:
            return ""
        return f"尚未集齐三种蛙明灯，仍缺: {', '.join(missing)}"

    def run(self, ex: Executor, state: State) -> Iterator[dict]:
        self._done = False
        st = state.refresh_status(ex.client, max_age_s=0)
        refresh_inventory(ex, state)
        if not self._missing_variants(state) or _is_adv_done(_query_advancements(ex), _FROGLIGHT_ADV):
            self._done = True
            return
        if reason := self.blocked(state):
            raise Replan(reason)
        picked = _pick_weapon(state)
        if picked is None:
            raise Replan("背包缺少战斗武器")
        weapon, wkind = picked

        cubes = _scan_ready(ex, state, "magma_cube", 32, entity=True)
        if not cubes and (yield from goto_known_landmark_or_biome(ex, state, _MAGMA_ANCHORS, rng=4, min_dist=6.0)):
            st = state.refresh_status(ex.client, max_age_s=0)
            cubes = _scan_ready(ex, state, "magma_cube", 32, entity=True)
        if not cubes:
            raise EnvBlocked(
                "下界玄武岩三角洲或堡垒刷怪笼附近未发现岩浆怪",
                missing_predicate="magma_cube",
                searched_volume="半径 32 格实体与下界台账（basalt_deltas|bastion_remnant）",
                wake_on="chunk_moved",
            )

        missing_vars = self._missing_variants(state)
        frogs = _matching_frogs(_scan_ready(ex, state, "frog", 32, entity=True), missing_vars)
        if not frogs:
            raise EnvBlocked(
                f"附近缺少所需变体青蛙以产出对应蛙明灯（需 {', '.join(missing_vars)}）",
                missing_predicate=f"frog_variant:{missing_vars[0]}",
                searched_volume="半径 32 格实体",
                wake_on="chunk_moved",
            )

        posture_touched = False
        try:
            if state.count("shield") >= 1:
                ex.posture(shield=True)
                if isinstance(state.st, dict):
                    state.st["blocking"] = True
                posture_touched = True
            yield from _equip_hand(ex, state, weapon)

            for _ in range(12):
                refresh_inventory(ex, state)
                missing_vars = self._missing_variants(state)
                if not missing_vars or _is_adv_done(_query_advancements(ex), _FROGLIGHT_ADV):
                    self._done = True
                    return
                st = state.refresh_status(ex.client, max_age_s=0)
                if not st.get("alive", True) or float(st.get("health", 20)) < 8.0:
                    raise Replan("收集蛙明灯途中生命值低于 8，止损撤退")

                cubes = _scan_ready(ex, state, "magma_cube", 32, entity=True)
                frogs = _matching_frogs(_scan_ready(ex, state, "frog", 32, entity=True), missing_vars)
                if not cubes:
                    missing_items = [_FROGLIGHT_BY_VARIANT[v] for v in missing_vars]
                    raise EnvBlocked(
                        f"附近岩浆怪已耗尽，仍缺蛙明灯: {', '.join(missing_items)}",
                        missing_predicate="magma_cube",
                        searched_volume="半径 32 格实体",
                        wake_on="chunk_moved",
                    )
                if not frogs:
                    raise EnvBlocked(
                        f"附近缺少所需变体青蛙（需 {', '.join(missing_vars)}）",
                        missing_predicate=f"frog_variant:{missing_vars[0]}",
                        searched_volume="半径 32 格实体",
                        wake_on="chunk_moved",
                    )

                small_cubes = [c for c in cubes if _is_small_magma_cube(c)]
                big_cubes = [c for c in cubes if not _is_small_magma_cube(c)]
                threatening = [
                    c for c in big_cubes
                    if any(math.dist(c["pos"], f["pos"]) <= 6.0 for f in frogs)
                ]

                if big_cubes and (not small_cubes or threatening):
                    pool = threatening or big_cubes
                    target = min(pool, key=lambda c: eye_to_point_dist(st, c["pos"]))
                    tpos = [float(v) for v in target["pos"]]
                    yield from _equip_hand(ex, state, weapon)
                    if wkind == "melee":
                        if eye_to_point_dist(st, tpos) > ENTITY_EYE_REACH:
                            yield rec("move_to", ex.move_to(pos=tpos, rng=2), pos=tpos, purpose="approach_magma_cube")
                            st = state.refresh_status(ex.client, max_age_s=0)
                        raw_size = target.get("size", 4)
                        hits = 1 if isinstance(raw_size, (int, float)) and raw_size <= 2 else 2
                        yield rec("attack", ex.attack(entity_id=target["id"], max_hits=hits),
                                  entity_id=target["id"], max_hits=hits, purpose="split_magma_cube")
                    else:
                        yield rec("shoot", ex.shoot(entity_id=target["id"], weapon=weapon),
                                  entity_id=target["id"], weapon=weapon, purpose="split_magma_cube")
                    continue

                frog, small = min(
                    ((f, c) for f in frogs for c in small_cubes),
                    key=lambda pair: (math.dist(pair[0]["pos"], pair[1]["pos"]), eye_to_point_dist(st, pair[1]["pos"])),
                )
                spos = [float(v) for v in small["pos"]]
                if math.dist(frog["pos"], spos) > 4.0:
                    if state.count("lead") >= 1 and not frog.get("leashed"):
                        if eye_to_point_dist(st, frog["pos"]) > ENTITY_EYE_REACH:
                            yield rec("move_to", ex.move_to(entity_id=frog["id"], rng=2),
                                      entity_id=frog["id"], purpose="approach_frog")
                        yield rec("interact", ex.interact(entity_id=frog["id"], item="lead"),
                                  entity_id=frog["id"], item="lead", purpose="lead_frog_to_magma")
                        yield rec("move_to", ex.move_to(pos=spos, rng=2), pos=spos, purpose="lure_frog_to_small_magma")
                    elif state.count("slime_ball") >= 1:
                        yield from _equip_hand(ex, state, "slime_ball")
                        yield rec("move_to", ex.move_to(pos=spos, rng=2), pos=spos, purpose="lure_frog_to_small_magma")
                    else:
                        yield rec("move_to", ex.move_to(pos=spos, rng=2), pos=spos, purpose="herd_to_small_magma")
                    continue

                yield rec("move_to", ex.move_to(pos=spos, rng=1), pos=spos, purpose="pickup_froglight")
                refresh_inventory(ex, state)
                if not self._missing_variants(state) or _is_adv_done(_query_advancements(ex), _FROGLIGHT_ADV):
                    self._done = True
                    return

            refresh_inventory(ex, state)
            if not self._missing_variants(state) or _is_adv_done(_query_advancements(ex), _FROGLIGHT_ADV):
                self._done = True
                return
            rem_items = [_FROGLIGHT_BY_VARIANT[v] for v in self._missing_variants(state)]
            raise Replan(f"本轮未能集齐三种蛙明灯，仍缺: {', '.join(rem_items)}")
        finally:
            _reset_posture(ex, state, force=posture_touched)
