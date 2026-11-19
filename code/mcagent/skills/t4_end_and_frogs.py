"""Stage 3 Wave A T4 技能：末地船鞘翅掠夺（+60）与三变体青蛙顺序单拴（+60）。"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from ..client import ApiError
from ..runtime import EnvBlocked, Executor, Replan, State
from . import nav
from .base import Skill, failure, rec
from .field import scan_blocks, scan_entities
from .interaction import _clear_posture
from .items import AIR, NAV_FAIL_CODES, bare, is_safe_solid_floor, total_placeable
from .observations import observe_block, refresh_inventory
from .registry import register
from .sightline import (
    ENTITY_EYE_REACH, STATION_EYE_REACH, block_cell, block_center,
    can_reach_block, can_see_block, eye_to_block_dist, eye_to_point_dist, feet_pos,
)
from .world_memo import memo_for

_FRAME_TYPES = ("item_frame", "glow_item_frame")
_SHIP_BLOCKS = ("purpur_block", "purpur_stairs", "brewing_stand", "dragon_head")
_FROG_ADV = "minecraft:husbandry/leash_all_frog_variants"
_FROG_VARIANTS = ("temperate", "warm", "cold")
_COLD_BIOMES = frozenset({
    "deep_dark", "snowy_plains", "snowy_taiga", "taiga", "old_growth_pine_taiga",
    "old_growth_spruce_taiga", "grove", "snowy_slopes", "frozen_peaks", "jagged_peaks",
    "frozen_river", "frozen_ocean", "deep_frozen_ocean", "ice_spikes",
    "end_highlands", "end_midlands", "the_end", "small_end_islands", "end_barrens",
})
_WARM_BIOMES = frozenset({
    "desert", "savanna", "savanna_plateau", "windswept_savanna", "badlands",
    "eroded_badlands", "wooded_badlands", "mangrove_swamp", "jungle", "sparse_jungle",
    "bamboo_jungle", "warm_ocean", "nether_wastes", "crimson_forest", "warped_forest",
    "soul_sand_valley", "basalt_deltas",
})
_BIOME_LANDMARKS = {"cold": ("cold_biome", "deep_dark"), "warm": ("mangrove_swamp",), "temperate": ("swamp", "meadow")}


def _dim(src: Any) -> str:
    raw = src.get("dimension") if isinstance(src, Mapping) else (getattr(src, "st", None) or {}).get("dimension")
    n = bare(raw).strip().lower()
    return "the_end" if n in ("the_end", "end") else ("the_nether" if n in ("the_nether", "nether") else (n or "overworld"))

def _biome_temp(dim: str, biome: str) -> str:
    d, b = bare(dim).strip().lower(), bare(biome).strip().lower()
    if d in ("the_end", "end") or b in _COLD_BIOMES:
        return "cold"
    return "warm" if (d in ("the_nether", "nether") or b in _WARM_BIOMES) else "temperate"

def _is_empty_bag(state: State) -> bool:
    inv = state.inventory or {}
    if any(isinstance(it, dict) and int(it.get("count", 0)) > 0 for it in (inv.get("items") or ())):
        return False
    armor = inv.get("armor") or {}
    extras = (*(armor.values() if isinstance(armor, dict) else ()), inv.get("held"), inv.get("offhand"))
    return not any(isinstance(eq, dict) and eq.get("name") and int(eq.get("count", 1)) > 0 for eq in extras)

def _is_preemption(exc: BaseException) -> bool:
    return failure(exc)[0] in ("replan", "interrupted") or "抢占" in str(exc)

def _reset_posture(ex: Executor, state: State, *, force: bool = False) -> None:
    st = state.st or {}
    if force or st.get("sneaking") or st.get("blocking"):
        try:
            _clear_posture(ex, state, sneak=False, shield=False)
        except Exception:
            pass

def _scan_ready(ex: Executor, state: State, types: Any, radius: int, *, entity: bool = False, limit: int = 16, skill: str = "") -> list[dict]:
    scan = scan_entities(ex, state, types, radius=radius) if entity else scan_blocks(ex, state, types, radius=radius, limit=limit, skill_name=skill)
    if scan.get("ready") is False:
        raise Replan("世界加载尚未就绪（ready=false）")
    return scan["entities" if entity else "blocks"]

def _query_advancements(ex: Executor) -> dict | None:
    fn = getattr(ex.client, "advancements", None)
    if not callable(fn):
        return None
    try:
        return fn(status="all", include_recipes=False)
    except TypeError:
        try: return fn()
        except (ApiError, AttributeError): return None
    except (ApiError, AttributeError):
        return None

def _criteria_remaining(state: State, adv_id: str, *, expected_criteria: Sequence[str] | None = None, advancements: Any = None) -> tuple[str, ...]:
    try:
        from ..loop.subgoals import advancement_criteria_remaining
        return tuple(advancement_criteria_remaining(state, adv_id, expected_criteria=expected_criteria, advancements=advancements))
    except ImportError:
        expected = tuple(sorted(str(c) for c in (expected_criteria or ())))
        rows = advancements.get("advancements") if isinstance(advancements, Mapping) else advancements
        row = next((r for r in rows if isinstance(r, Mapping) and bare(r.get("id")) == bare(adv_id)), None) if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)) else None
        if row is None:
            return expected
        if row.get("status") == "completed" or row.get("done") is True:
            return ()
        done = {bare(str(k)) for k, v in (row.get("criteria") or {}).items() if v} if isinstance(row.get("criteria"), Mapping) else set()
        for k in ("completed_criteria", "criteria_done"):
            if isinstance(val := row.get(k), Sequence) and not isinstance(val, (str, bytes)):
                done.update(bare(str(c)) for c in val)
        return tuple(c for c in expected if bare(c) not in done)

def _is_air(ex: Executor, cell: tuple[int, int, int]) -> bool:
    return bare(observe_block(ex, list(cell)).get("name")) in AIR

def _is_solid(ex: Executor, cell: tuple[int, int, int]) -> bool:
    return is_safe_solid_floor(observe_block(ex, list(cell)))

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
    fx, fy, fz = block_cell(feet_pos(st))
    tx, _, tz = target
    cx, cz, gaps = fx, fz, []
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
        else:
            break
    return gaps

def _find_tadpole_spot(ex: Executor, state: State, st: dict, skill_name: str) -> tuple[int, int, int] | None:
    if waters := _scan_ready(ex, state, "water", 16, limit=4, skill=skill_name):
        return block_cell(min(waters, key=lambda b: eye_to_block_dist(st, b["pos"]))["pos"])
    fx, fy, fz = block_cell(feet_pos(st))
    cands = [(fx + dx, fy - 1, fz + dz) for dx in range(-3, 4) for dz in range(-3, 4)
             if (dx or dz) and _is_solid(ex, (fx + dx, fy - 1, fz + dz)) and _is_air(ex, (fx + dx, fy, fz + dz))
             and can_reach_block(ex, st, (fx + dx, fy - 1, fz + dz), reach=STATION_EYE_REACH)]
    return min(cands, key=lambda c: eye_to_block_dist(st, c), default=None)


@register
class LootEndShipElytra(Skill):
    name, goal, verified = "loot_end_ship_elytra", "t4:end_elytra", "offline"
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
        dest_cell: tuple[int, int, int] | None = None
        if frames:
            dest_cell = block_cell(min(frames, key=lambda e: eye_to_point_dist(st, e["pos"]))["pos"])
        elif ship_blks := _scan_ready(ex, state, _SHIP_BLOCKS, 48, limit=16, skill=self.name):
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
            if needs_gap and state.count("ender_pearl") >= 1 and float(st.get("health", 20)) > 8.0 and dist > 6.0:
                yield rec("use_item", ex.use_item("ender_pearl", target={"pos": dest_pt}),
                          item="ender_pearl", target={"pos": dest_pt})
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
    name, goal, verified = "leash_frog_variants", "t4:leash_all_frog_variants", "offline"
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

    def run(self, ex: Executor, state: State) -> Iterator[dict]:
        self._leashed_variants = set()
        st = state.refresh_status(ex.client, max_age_s=0)
        refresh_inventory(ex, state)
        if reason := self.blocked(state):
            raise Replan(reason)
        if state.count("lead") < 1:
            yield rec("craft", ex.craft("lead", 1), item="lead", count=1)
            refresh_inventory(ex, state)
            if state.count("lead") < 1:
                raise Replan("合成拴绳后背包仍未获得 lead")

        self._last_adv = _query_advancements(ex)
        remaining = _criteria_remaining(state, _FROG_ADV, expected_criteria=_FROG_VARIANTS, advancements=self._last_adv)
        if not remaining:
            return

        cur_dim = _dim(st)
        cur_temp = _biome_temp(cur_dim, str(st.get("biome") or ""))
        frogs = _scan_ready(ex, state, "frog", 32, entity=True)
        if frogs and cur_temp in remaining:
            frog = min(frogs, key=lambda e: eye_to_point_dist(st, e["pos"]))
            frog_id = frog["id"]
            if eye_to_point_dist(st, frog["pos"]) > 3.5:
                yield rec("move_to", ex.move_to(entity_id=frog_id, rng=2), entity_id=frog_id)
                st = state.refresh_status(ex.client, max_age_s=0)
                if eye_to_point_dist(st, frog["pos"]) > ENTITY_EYE_REACH + 1.0:
                    fresh = next((e for e in _scan_ready(ex, state, "frog", 16, entity=True) if e["id"] == frog_id), None)
                    if fresh is None or eye_to_point_dist(st, fresh["pos"]) > ENTITY_EYE_REACH:
                        raise Replan("靠近青蛙后仍超出拴绳交互距离")
            yield rec("interact", ex.interact(entity_id=frog_id, item="lead"), entity_id=frog_id, item="lead")
            refresh_inventory(ex, state)
            self._last_adv = _query_advancements(ex)
            self._leashed_variants.add(cur_temp)
            return

        if cur_temp in remaining and state.count("tadpole_bucket") >= 1:
            spot = _find_tadpole_spot(ex, state, st, self.name)
            if spot is None:
                raise EnvBlocked(f"当前 {cur_temp} 群系缺少释放蝌蚪桶的水源或平地", missing_predicate="tadpole_release_spot",
                                 searched_volume="半径 16 格水源及周边 3 格平地", wake_on="chunk_moved")
            if eye_to_block_dist(st, spot) > 3.5 or not can_see_block(ex, st, spot):
                dest = list(block_center(spot))
                yield rec("move_to", ex.move_to(pos=dest, rng=2), pos=dest)
            yield rec("interact", ex.interact(pos=list(spot), item="tadpole_bucket"),
                      pos=list(spot), item="tadpole_bucket")
            refresh_inventory(ex, state)
            return

        memo = memo_for(state)
        for temp in remaining:
            for lk in _BIOME_LANDMARKS.get(temp, ()):
                if lms := memo.landmarks(lk, dimension=cur_dim):
                    dest = list(block_center(min(lms, key=lambda c: eye_to_block_dist(st, c))))
                    yield rec("move_to", ex.move_to(pos=dest, rng=2), pos=dest, purpose=f"seek_frog_{temp}")
                    return

        raise EnvBlocked(f"缺少剩余青蛙温度变体 {','.join(remaining)} 对应群系或实体",
                         missing_predicate=f"frog_variant:{remaining[0]}",
                         searched_volume="半径 32 格实体与世界台账群系地标", wake_on="chunk_moved")
