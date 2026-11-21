"""Diamond collection and equipment entry points for the T1 harness."""
from __future__ import annotations

from collections.abc import Generator
import math

from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, failure, rec
from .diamond_site import (
    CARDINALS, CONTAINERS, DIAMOND_ORES, _above, _approach, _block, _cell,
    _dry_name, _in_reach, _nearby, _safe_excavation, _safe_status, _standing,
)
from .field import equip_item, mine_block_batch, place_block, scan_blocks
from .items import AIR, NAV_FAIL_CODES, PICKAXE_ORDER, PLACEABLE_BLOCKS, bare, record_mine_trail
from .mine_shaft import (
    DIAMOND_LAYER_Y, branch_mine_level, column_has_water,
    navigate_to_shaft, plug_or_detour_water,
)
from .mining import SOLID_SUPPORT, MineStepUnsafe, mine_step
from .observations import refresh_inventory as _fresh_inventory
from .recipes import RECIPES, craft_recipe
from .registry import register
from .sightline import DIG_EYE_REACH, STATION_EYE_REACH, eye_to_block_dist
from .voxel_cache import client_get_blocks_batch, has_batch_get_blocks
from .windows import is_storage_window, take_container_item, window_amount, window_view

_ARMOR = ("diamond_boots", "diamond_chestplate", "diamond_helmet", "diamond_leggings")
_DIAMOND_PICKS = ("diamond_pickaxe", "netherite_pickaxe")
_IRON_PICKS = tuple(pick for pick in PICKAXE_ORDER if pick in (*_DIAMOND_PICKS, "iron_pickaxe"))
_CLEAR_FAIL_CODES = frozenset({
    *NAV_FAIL_CODES, "out_of_reach", "dig_unconfirmed", "protected_region",
    "not_loaded", "cannot_interact", "no_los", "los_blocked", "cannot_clear",
    "timeout", "replan",
})
_MAX_DESCENT = 24
_MAX_ORES = 24
_MIN_Y = DIAMOND_LAYER_Y
_SCAN_RADIUS = 32

LAPIS_ORES = frozenset(("lapis_ore", "deepslate_lapis_ore"))
REDSTONE_ORES = frozenset(("redstone_ore", "deepslate_redstone_ore"))
BONUS_ORES = LAPIS_ORES | REDSTONE_ORES
_BONUS_ORE_INFO = {
    "lapis_ore": ("lapis_ore", LAPIS_ORES, "lapis_lazuli"),
    "deepslate_lapis_ore": ("lapis_ore", LAPIS_ORES, "lapis_lazuli"),
    "redstone_ore": ("redstone_ore", REDSTONE_ORES, "redstone"),
    "deepslate_redstone_ore": ("redstone_ore", REDSTONE_ORES, "redstone"),
}
_BONUS_STABLE = SOLID_SUPPORT | frozenset(PLACEABLE_BLOCKS) | DIAMOND_ORES | BONUS_ORES | {"bedrock", "obsidian"}
_FACES = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
_MAX_BONUS_ORES = 4


def _voxel_cache(state):
    try:
        from . import voxel_cache
        return voxel_cache.voxel_cache_for(state)
    except ImportError:
        return None


def _world_memo(state):
    try:
        from .world_memo import memo_for
        return memo_for(state)
    except ImportError:
        return None


def _reach_mod():
    try:
        from . import reach
        return reach
    except ImportError:
        return None


def _bounded_move_to(ex, pos, *, rng=1, timeout=30.0):
    try:
        return ex.move_to(pos=pos, rng=rng, timeout=timeout, retries=0)
    except TypeError:
        return ex.move_to(pos=pos, rng=rng)


def _record_station(state, kind: str, pos, dimension: str = "overworld") -> None:
    try:
        from .world_memo import memo_for
    except ImportError:
        return
    fn = getattr(memo_for(state), "record_station", None)
    if callable(fn):
        fn(kind, pos, dimension=dimension)


def _safe_bonus_excavation(ex, origin, ore, protected) -> bool:
    """Verify stance support, dry neighbors, and non-preserved target for bonus ore digging."""
    preserved = {_above(cell, dy) for cell in {origin, *protected} for dy in (-1, 0, 1)}
    if ore in preserved:
        return False
    x, y, z = ore
    if has_batch_get_blocks(getattr(ex, "client", None)):
        probe = [
            _above(cell, dy)
            for cell in (origin, *(protected[-2:] if protected else ()))
            for dy in (-1, 0, 1)
        ] + [ore, _above(ore, 1), *((x + dx, y + dy, z + dz) for dx, dy, dz in _FACES)]
        client_get_blocks_batch(ex.client, probe)
    if not _standing(ex, origin):
        return False
    if protected and not all(_standing(ex, cell) for cell in protected[-2:]):
        return False
    if _dry_name(_block(ex, _above(ore, 1))) not in _BONUS_STABLE | AIR:
        return False
    if not all(_dry_name(_block(ex, (x + dx, y + dy, z + dz))) for dx, dy, dz in _FACES):
        return False
    block = _block(ex, ore)
    return _dry_name(block) in BONUS_ORES and block.get("diggable") is not False


def _world_block(state) -> str:
    st = state.snapshot()
    if not st.get("ready") or not st.get("alive") or not bare(st.get("dimension")):
        return "尚无当前维度及可行动状态，无法观察钻石线地形或窗口"
    return ""


def _durable(entry, minimum: int) -> bool:
    durability = (entry or {}).get("durability")
    return (isinstance(durability, (list, tuple)) and len(durability) == 2
            and type(durability[0]) is int and durability[0] >= minimum)


def _has_silk(entry) -> bool:
    return any(
        isinstance(ench, dict) and bare(ench.get("name")) == "silk_touch"
        and type(ench.get("level")) is int and ench["level"] > 0
        for ench in ((entry or {}).get("enchantments") or ())
    )


def _pick(inventory, names, minimum: int) -> str | None:
    held = inventory.get("held") or {}
    entries = (held, *(inventory.get("items") or ()))
    held_name = bare(held.get("name"))
    if held_name in names and _durable(held, minimum) and not _has_silk(held):
        return held_name
    non_silk = next((name for name in names if any(
        bare(entry.get("name")) == name and _durable(entry, minimum) and not _has_silk(entry)
        for entry in entries
    )), None)
    if non_silk is not None:
        return non_silk
    if held_name in names and _durable(held, minimum):
        return held_name
    return next((name for name in names if any(
        bare(entry.get("name")) == name and _durable(entry, minimum) for entry in entries
    )), None)


def _hand_pick(ex, state, minimum: int):
    inv = _fresh_inventory(ex, state)
    pick = _pick(inv, _IRON_PICKS, minimum)
    if pick is None:
        raise Replan(f"采钻需要铁镐或更高档镐，至少确认 {minimum} 点剩余耐久，请补备镐")
    yield from equip_item(ex, state, pick, min_durability=minimum)
    return pick


def _nearby_loot(ex, state, names, goal_qty: int, status):
    """Only open exposed containers from the observed stance, and operate a single slot action."""
    for pos in _nearby(ex, state, CONTAINERS, radius=4, limit=3):
        dimension = bare(status.get("dimension"))
        current = _safe_status(ex, state, dimension or None)
        if not _in_reach(ex, current, pos, reach=3):
            continue
        block = _dry_name(_block(ex, pos))
        if block not in CONTAINERS:
            continue
        target = {"pos": list(pos)}
        try:
            view = yield from window_view(ex, target)
        except Replan as exc:
            code, _ = failure(exc)
            if code in {"out_of_reach", "path_not_found", "cannot_interact", "not_loaded", "no_window", "stalled"}:
                yield rec("window_view", target=target, error_code=code)
                continue
            raise
        current = _safe_status(ex, state, dimension or None)
        if (not is_storage_window(view, block) or _cell(current["pos"]) != _cell(status["pos"])
                or not _in_reach(ex, current, pos, reach=3)):
            continue
        for item in names:
            available = window_amount(view, "container", item)
            take = min(available, goal_qty - state.count(item))
            if take > 0:
                taken = yield from take_container_item(ex, state, target, "container", item, take, view=view)
                if state.count(item) >= goal_qty:
                    return
                if taken.get("inventory_full"):
                    raise Replan(
                        f"从容器取出 {item} 时背包已满（inventory_full，本次实收 {taken['count']} 个，"
                        f"现持 {state.count(item)}/{goal_qty}），剩余物品留在容器内，请先腾出空位"
                    )
                view = taken["view"]


def collect_bonus_ores(
    ex,
    state,
    *,
    protected=(),
    tried=None,
    max_blocks: int = 2,
    radius: int = 6,
    observed=None,
) -> Generator[dict, None, int]:
    """Opportunistically record and safely mine reachable lapis/redstone ores at Y <= 0."""
    status = _safe_status(ex, state, "overworld")
    origin = _cell(status["pos"])
    if origin[1] > 0 or max_blocks <= 0:
        return 0
    if observed is not None:
        raw_blocks = observed.get("blocks") or ()
    else:
        scan = scan_blocks(ex, state, list(BONUS_ORES), radius=radius, limit=max(4, max_blocks * 2))
        raw_blocks = scan.get("blocks") or ()
    candidates: list[tuple[tuple[int, int, int], str]] = []
    seen_pos: set[tuple[int, int, int]] = set()
    for block in raw_blocks:
        bname = bare(block.get("name"))
        raw_pos = block.get("pos")
        if bname not in BONUS_ORES or not raw_pos or len(raw_pos) != 3:
            continue
        pos = _cell(raw_pos)
        if pos not in seen_pos:
            seen_pos.add(pos)
            candidates.append((pos, bname))
    if not candidates:
        return 0
    memo = _world_memo(state)
    if memo is not None:
        for pos, bname in candidates:
            category, _, _ = _BONUS_ORE_INFO[bname]
            memo.record_seen(
                category,
                bname,
                pos,
                dimension="overworld",
                dist=eye_to_block_dist(status, pos),
            )
    candidates.sort(key=lambda item: (eye_to_block_dist(status, item[0]), item[0]))
    if _pick(_fresh_inventory(ex, state), _IRON_PICKS, 1) is None:
        return 0
    tried_set = tried if tried is not None else set()
    vc = _voxel_cache(state)
    mined = 0
    for pos, bname in candidates:
        if mined >= max_blocks:
            break
        if pos in tried_set:
            continue
        if vc is not None and vc.is_unreachable("mine_diamond", pos, dimension="overworld", stance_cell=origin):
            continue
        if not (_in_reach(ex, status, pos, reach=DIG_EYE_REACH)
                and _safe_bonus_excavation(ex, origin, pos, protected)):
            continue
        if mined > 0 and _pick(_fresh_inventory(ex, state), _IRON_PICKS, 1) is None:
            break
        tried_set.add(pos)
        yield from _hand_pick(ex, state, 1)
        stand = _safe_status(ex, state, "overworld")
        stand_cell = _cell(stand["pos"])
        if not (_in_reach(ex, stand, pos, reach=DIG_EYE_REACH)
                and _safe_bonus_excavation(ex, stand_cell, pos, protected)):
            status = stand
            origin = stand_cell
            continue
        category, family, drop_item = _BONUS_ORE_INFO[bname]
        try:
            yield from mine_block_batch(
                ex,
                state,
                (pos,),
                block_types=family,
                drop=drop_item,
                count=1,
                max_blocks=1,
            )
        except Replan as exc:
            code, _ = failure(exc)
            if code in {"dig_unconfirmed", "out_of_reach", "protected_region"}:
                if vc is not None:
                    vc.mark_unreachable("mine_diamond", pos, dimension="overworld", stance_cell=stand_cell)
                if memo is not None:
                    memo.mark_unreachable(pos, dimension="overworld", category=category, error_code=code, block=bname)
                status = _safe_status(ex, state, "overworld")
                origin = _cell(status["pos"])
                yield rec("bonus_ore_skip", pos=list(pos), error_code=code)
                continue
            raise
        if vc is not None:
            vc.invalidate(pos, dimension="overworld")
        if memo is not None:
            memo.mark_depleted(pos, dimension="overworld", category=category)
        mined += 1
        status = _safe_status(ex, state, "overworld")
        origin = _cell(status["pos"])
    return mined


@register
class MineDiamond(Skill):
    name = "mine_diamond"
    description = "优先取回可达钻石，安全下探并收集目标数量"
    params = {"count": Param(int, "本批目标库存，已有钻石抵扣", default=4, min=1, max=16)}
    goal = "acq:diamond"
    verified = "offline"

    @classmethod
    def blocked(cls, state) -> str:
        return "" if state.count("diamond") else _world_block(state)

    @classmethod
    def material_plan(cls, args: dict, state) -> tuple[tuple[str, int, str], ...]:
        if state.count("diamond") >= args.get("count", 4):
            return ()
        pick = _pick(state.inventory, _IRON_PICKS, 1)
        return ((pick, 1, "borrow"),) if pick else ()

    def run(self, ex, state):
        goal_qty = self.args["count"]
        _fresh_inventory(ex, state)
        initial = state.count("diamond")
        if initial >= goal_qty:
            return
        status = _safe_status(ex, state)
        try:
            yield from _nearby_loot(ex, state, ("diamond",), goal_qty, status)
        except Replan:
            looted = state.count("diamond")
            if initial < looted < goal_qty:
                yield self._partial(initial, looted, goal_qty)
            raise
        if state.count("diamond") >= goal_qty:
            return
        start = _safe_status(ex, state, "overworld")
        if _pick(_fresh_inventory(ex, state), _IRON_PICKS, 1) is None:
            looted = state.count("diamond")
            if initial < looted < goal_qty:
                yield self._partial(initial, looted, goal_qty)
            raise Replan("开采暴露钻矿前请补有耐久的铁镐、钻石镐或下界合金镐")
        memo = _world_memo(state)
        existing_shaft = memo.get_mine_shaft(dimension="overworld") if memo is not None else None
        camp = memo.get_camp(dimension="overworld") if (memo is not None and existing_shaft is None) else None
        start_cell = _cell(start["pos"])
        try:
            if existing_shaft is not None:
                low_pos = tuple(existing_shaft["lowest_pos"])
                entry_pos = tuple(existing_shaft["entry"])
                low_y = int(existing_shaft["lowest_y"])
                if start_cell[1] > low_y + 2 and min(math.dist(start_cell, low_pos), math.dist(start_cell, entry_pos)) <= 64.0:
                    yield from navigate_to_shaft(ex, state, target_y=DIAMOND_LAYER_Y, dimension="overworld", max_dist=64.0)
                    start = _safe_status(ex, state, "overworld")
            elif camp is not None and camp.get("mine_entry") is not None:
                me = _cell(camp["mine_entry"])
                if 3.0 < math.dist(start_cell, me) <= 64.0 and start_cell[1] >= me[1] - 2:
                    yield from navigate_to_shaft(ex, state, target_y=DIAMOND_LAYER_Y, dimension="overworld", max_dist=64.0)
                    start = _safe_status(ex, state, "overworld")
        except Replan:
            collected = state.count("diamond")
            if initial < collected < goal_qty:
                yield self._partial(initial, collected, goal_qty)
            raise
        path = [_cell(start["pos"])]
        waypoints = [start["pos"]]
        tried_ores = set()
        tried_bonus: set = set()
        bonus_mined = 0
        branches_done = 0
        visited_columns = {(path[0][0], path[0][2])}
        heading = (
            tuple(existing_shaft["heading"])
            if (existing_shaft is not None and tuple(existing_shaft["heading"]) in CARDINALS)
            else CARDINALS[0]
        )
        stairs = 0
        vc = _voxel_cache(state)
        reach_mod = _reach_mod()
        try:
            while len(tried_ores) < _MAX_ORES:
                status = _safe_status(ex, state, "overworld")
                if vc is not None:
                    vc.clear_stance_blacklist_if_moved(status["pos"])
                origin = _cell(status["pos"])
                ores = _nearby(ex, state, DIAMOND_ORES, radius=_SCAN_RADIUS, limit=_MAX_ORES)
                if vc is not None and ores:
                    vc.ingest_find_blocks(
                        {"blocks": [{"pos": list(o), "name": "diamond_ore"} for o in ores]},
                        dimension="overworld",
                    )
                for ore in ores:
                    if ore in tried_ores or len(tried_ores) >= _MAX_ORES:
                        continue
                    if vc is not None and vc.is_unreachable("mine_diamond", ore, dimension="overworld", stance_cell=origin):
                        continue
                    # Adjacent safe stances may extend the four-block digging budget by one.
                    if math.dist(origin, ore) > 6 or not _safe_excavation(ex, origin, (ore,), path):
                        continue
                    stand = yield from _approach(ex, state, ore, status, dimension="overworld", protected=path)
                    if stand is None:
                        if reach_mod is not None and eye_to_block_dist(status, ore) <= DIG_EYE_REACH:
                            cleared = False
                            try:
                                yield from reach_mod.clear_line_of_sight(
                                    ex, state, ore, max_clear=2, reach=DIG_EYE_REACH,
                                )
                                cleared = True
                            except Replan as exc:
                                code, _ = failure(exc)
                                if (isinstance(exc, EnvBlocked) or code == "interrupted"
                                        or "抢占" in str(exc) or code not in _CLEAR_FAIL_CODES):
                                    raise
                            if cleared:
                                cand = _safe_status(ex, state, "overworld")
                                if (_in_reach(ex, cand, ore, reach=5)
                                        and _safe_excavation(ex, _cell(cand["pos"]), (ore,), path)):
                                    stand = cand
                        if stand is None:
                            if vc is not None:
                                vc.mark_unreachable("mine_diamond", ore, dimension="overworld", stance_cell=origin)
                            continue
                    tried_ores.add(ore)
                    yield from _hand_pick(ex, state, 1)
                    stand = _safe_status(ex, state, "overworld")
                    if (not _in_reach(ex, stand, ore, reach=5)
                            or not _safe_excavation(ex, _cell(stand["pos"]), (ore,), path)):
                        raise Replan("换镐后钻矿的安全站位或可采性已经变化，请重新观察")
                    try:
                        yield from mine_block_batch(ex, state, (ore,), block_types=DIAMOND_ORES,
                                                    drop="diamond", count=1, max_blocks=1)
                    except Replan as exc:
                        code, _ = failure(exc)
                        if code in {"dig_unconfirmed", "out_of_reach", "protected_region"}:
                            if vc is not None:
                                vc.mark_unreachable("mine_diamond", ore, dimension="overworld", stance_cell=origin)
                            _safe_status(ex, state, "overworld")
                            yield rec("ore_skip", pos=list(ore), error_code=code)
                            break
                        raise
                    if vc is not None:
                        vc.invalidate(ore, dimension="overworld")
                    after_mine = _safe_status(ex, state, "overworld")
                    if state.count("diamond") >= goal_qty:
                        if _cell(after_mine["pos"])[1] <= 0 and bonus_mined < _MAX_BONUS_ORES:
                            bonus_mined += yield from collect_bonus_ores(
                                ex,
                                state,
                                protected=path,
                                tried=tried_bonus,
                                max_blocks=min(2, _MAX_BONUS_ORES - bonus_mined),
                            )
                        return
                    break
                else:
                    if origin[1] <= 0 and bonus_mined < _MAX_BONUS_ORES:
                        bonus_mined += yield from collect_bonus_ores(
                            ex,
                            state,
                            protected=path,
                            tried=tried_bonus,
                            max_blocks=min(2, _MAX_BONUS_ORES - bonus_mined),
                        )
                        status = _safe_status(ex, state, "overworld")
                        origin = _cell(status["pos"])
                    stop_descent = (
                        _cell(status["pos"]) != path[-1]
                        or stairs >= _MAX_DESCENT
                        or origin[1] <= _MIN_Y
                    )
                    step = None if stop_descent else (
                        yield from self._down_step(ex, state, origin, path, visited_columns, heading)
                    )
                    if step is None:
                        if not stop_descent:
                            status = _safe_status(ex, state, "overworld")
                            origin = _cell(status["pos"])
                        if _cell(status["pos"]) == path[-1] and origin[1] <= -54 and branches_done < 2:
                            branches_done += 1
                            branch_ores = yield from branch_mine_level(
                                ex,
                                state,
                                level_y=DIAMOND_LAYER_Y,
                                target_ores=DIAMOND_ORES,
                                bonus_ores=tuple(BONUS_ORES),
                                allowed_picks=frozenset(_IRON_PICKS),
                                max_length=12,
                                scan_stride=3,
                                scan_radius=_SCAN_RADIUS,
                                dimension="overworld",
                                excluded=tried_ores,
                            )
                            after_branch = _safe_status(ex, state, "overworld")
                            after_cell = _cell(after_branch["pos"])
                            if after_cell != path[-1]:
                                path.append(after_cell)
                                waypoints.append(after_branch["pos"])
                                visited_columns.add((after_cell[0], after_cell[2]))
                                if memo is not None and memo.get_mine_shaft(dimension="overworld") is not None:
                                    memo.update_mine_shaft(
                                        dimension="overworld",
                                        lowest_y=after_cell[1],
                                        lowest_pos=after_cell,
                                    )
                            if branch_ores:
                                continue
                            if after_cell != origin and branches_done < 2:
                                continue
                        break
                    if step is False:
                        continue  # A fresh scan found a new reachable diamond face during the cut.
                    path.append(_cell(step))
                    waypoints.append(step)
                    visited_columns.add((path[-1][0], path[-1][2]))
                    heading = self._normalize_heading(path[-1][0] - origin[0], path[-1][2] - origin[2], heading)
                    stairs += 1
                    if memo is not None:
                        if memo.get_mine_shaft(dimension="overworld") is None:
                            memo.set_mine_shaft(
                                path[0],
                                dimension="overworld",
                                lowest_y=path[-1][1],
                                lowest_pos=path[-1],
                                heading=heading,
                            )
                        else:
                            memo.update_mine_shaft(
                                dimension="overworld",
                                lowest_y=path[-1][1],
                                lowest_pos=path[-1],
                                heading=heading,
                            )
                    if stairs % 4 == 0:
                        minerals = ["lapis_ore", "deepslate_lapis_ore", "iron_ore", "deepslate_iron_ore"]
                        if path[-1][1] <= 0:
                            minerals += ["redstone_ore", "deepslate_redstone_ore"]
                        observed = scan_blocks(ex, state, minerals, radius=8, limit=6)
                        if observed["blocks"]:
                            yield rec("diamond_prospect", observed)
                            if (path[-1][1] <= 0 and bonus_mined < _MAX_BONUS_ORES
                                    and any(bare(b.get("name")) in BONUS_ORES for b in observed["blocks"])):
                                bonus_mined += yield from collect_bonus_ores(
                                    ex,
                                    state,
                                    protected=path,
                                    tried=tried_bonus,
                                    max_blocks=min(2, _MAX_BONUS_ORES - bonus_mined),
                                    observed=observed,
                                )
        except Replan:
            collected = state.count("diamond")
            if initial < collected < goal_qty:
                yield self._partial(initial, collected, goal_qty)
            raise
        finally:
            record_mine_trail(waypoints)

        collected = state.count("diamond")
        if collected:
            yield self._partial(initial, collected, goal_qty)
        raise EnvBlocked(
            f"钻石现持 {collected}/{goal_qty}，已停在可观察范围内，请换区块找安全暴露矿",
            missing_predicate="reachable_diamond",
            searched_volume=f"半径 {_SCAN_RADIUS} 格暴露面，Y={path[0][1]} 至 {path[-1][1]}，"
                            f"{stairs}/{_MAX_DESCENT} 级阶梯，{len(tried_ores)} 个已采矿点",
            wake_on="chunk_moved",
        )

    @staticmethod
    def _partial(initial: int, collected: int, goal_qty: int) -> dict:
        return rec("diamond_partial", {"held": collected, "gained": collected - initial,
                                       "requested": goal_qty, "inventory_criterion": True})

    @staticmethod
    def _normalize_heading(dx: int, dz: int, fallback: tuple[int, int]) -> tuple[int, int]:
        if abs(dx) >= abs(dz) and dx != 0:
            return (1 if dx > 0 else -1, 0)
        if dz != 0:
            return (0, 1 if dz > 0 else -1)
        return fallback

    def _cut_descent(self, ex, state, origin, path, visited_columns, landing):
        column = (landing[0], landing[2])
        cuts = tuple(_above(landing, dy) for dy in (0, 1, 2))
        visited_columns.add(column)
        remaining = sum(_dry_name(_block(ex, cell)) not in AIR for cell in cuts)
        pick = yield from _hand_pick(ex, state, remaining + 1)
        if (_cell(_safe_status(ex, state, "overworld")["pos"]) != origin
                or not _safe_excavation(ex, origin, cuts, path, landing=landing)):
            return None

        def observe_cut():
            status = _safe_status(ex, state, "overworld")
            if (_cell(status["pos"]) != origin
                    or not _safe_excavation(ex, origin, cuts, path, landing=landing)):
                raise MineStepUnsafe("cut", "阶梯开凿时出现流体或支撑变化，停止使用该列")
            # The callback can pause the movement before the next cell is dug.
            ores = _nearby(ex, state, DIAMOND_ORES, radius=5, limit=6)
            yield from ()
            return any(_in_reach(ex, status, ore, reach=4)
                       and _safe_excavation(ex, origin, (ore,), path)
                       for ore in ores) or None

        dest, evidence = yield from mine_step(
            ex, state, landing, allowed_picks=frozenset((pick,)), after_dig=observe_cut,
        )
        if evidence:
            return False
        after = _safe_status(ex, state, "overworld")
        if _cell(after["pos"]) != landing or not all(_standing(ex, cell) for cell in path[-2:]):
            raise Replan("下一级落脚或回程阶梯支撑未确认，停止下探")
        return dest

    def _down_step(self, ex, state, origin, path, visited_columns, heading):
        x, y, z = origin
        dirs = (heading, *(d for d in CARDINALS if d != heading))
        for dx, dz in dirs:
            landing = (x + dx, y - 1, z + dz)
            column = (landing[0], landing[2])
            cuts = tuple(_above(landing, dy) for dy in (0, 1, 2))
            if column in visited_columns or not _safe_excavation(ex, origin, cuts, path, landing=landing):
                continue
            try:
                return (yield from self._cut_descent(ex, state, origin, path, visited_columns, landing))
            except MineStepUnsafe as exc:
                if exc.stage == "move" or _cell(_safe_status(ex, state, "overworld")["pos"]) != origin:
                    raise
                continue
        if any(
            (x + dx, z + dz) not in visited_columns
            and column_has_water(ex, [x + dx, y - 1, z + dz])
            for dx, dz in dirs
        ):
            recovered, new_heading = yield from plug_or_detour_water(
                ex, state, origin, heading, allowed_picks=frozenset(_IRON_PICKS),
            )
            if recovered:
                cur_st = _safe_status(ex, state, "overworld")
                cur_cell = _cell(cur_st["pos"])
                if cur_cell != origin:
                    return list(cur_st["pos"])
                ndx, ndz = new_heading
                landing = (x + ndx, y - 1, z + ndz)
                column = (landing[0], landing[2])
                cuts = tuple(_above(landing, dy) for dy in (0, 1, 2))
                if column not in visited_columns and _safe_excavation(ex, origin, cuts, path, landing=landing):
                    try:
                        return (yield from self._cut_descent(ex, state, origin, path, visited_columns, landing))
                    except MineStepUnsafe as exc:
                        if exc.stage == "move" or _cell(_safe_status(ex, state, "overworld")["pos"]) != origin:
                            raise
                        return None
        return None


def _already_has_gear(item: str, state) -> bool:
    if item == "diamond_boots":
        return any(state.count(armor) for armor in _ARMOR)
    return _pick(state.inventory, _DIAMOND_PICKS, 1) is not None


@register
class CraftDiamondGear(Skill):
    name = "craft_diamond_gear"
    description = "使用现有钻石甲或钻石级镐，必要时制作钻石靴、钻石镐"
    params = {"item": Param(str, "钻石装备", choices=("diamond_boots", "diamond_pickaxe"))}
    goal = "gear:{item}"
    verified = "offline"

    @classmethod
    def blocked(cls, state) -> str:
        return "" if any(_already_has_gear(item, state) for item in cls.params["item"].choices) else _world_block(state)

    @classmethod
    def material_plan(cls, args: dict, state) -> tuple[tuple[str, int, str], ...]:
        item = args["item"]
        if _already_has_gear(item, state):
            return ()
        ingredients = RECIPES[item].ingredients
        if any(state.count(ingredient) < count for ingredient, count in ingredients):
            return ()  # 背包无法自制，这一拍仍可观察容器成品。
        return tuple((ingredient, count, "consume") for ingredient, count in ingredients)

    def run(self, ex, state):
        item = self.args["item"]
        _fresh_inventory(ex, state)
        if _already_has_gear(item, state):
            return
        status = _safe_status(ex, state)
        names = _ARMOR if item == "diamond_boots" else _DIAMOND_PICKS
        yield from _nearby_loot(ex, state, names, 1, status)
        if _already_has_gear(item, state):
            return
        _fresh_inventory(ex, state)
        for ingredient, count in RECIPES[item].ingredients:
            if state.count(ingredient) < count:
                raise Replan(f"合成 {item} 缺材料：需要 {count} 个 {ingredient}，请先补足")
        current = _safe_status(ex, state, bare(status.get("dimension")) or None)
        dim = bare(current.get("dimension")) or "overworld"
        tables = _nearby(ex, state, ("crafting_table",), radius=4, limit=5)
        table = next((c for c in tables if _in_reach(ex, current, c, reach=STATION_EYE_REACH)), None)
        if table is not None:
            _record_station(state, "crafting_table", table, dimension=dim)
        else:
            x, y, z = _cell(current["pos"])
            sites = [(x + dx, y, z + dz) for dx, dz in CARDINALS
                     if _standing(ex, (x + dx, y, z + dz))]
            if len(sites) < 2 or not state.count("crafting_table"):
                raise EnvBlocked("缺少可达工作台或可保留退路的放台位，请携带工作台并换站位",
                                 missing_predicate="station_ready:crafting_table",
                                 searched_volume="半径 4 格至多 5 个暴露工作台和 4 个同层邻接放台位",
                                 wake_on="chunk_moved")
            yield from place_block(ex, state, "crafting_table", sites[0])
            _record_station(state, "crafting_table", sites[0], dimension=dim)
            _fresh_inventory(ex, state)
        yield from craft_recipe(ex, state, item, goal_qty=1)
        if not _already_has_gear(item, state):
            raise Replan("钻石装备合成后未确认可用成品，检查背包和耐久")
