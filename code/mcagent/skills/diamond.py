"""Diamond collection and equipment entry points for the T1 harness."""
from __future__ import annotations

import math

from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, failure, rec
from .diamond_site import (
    CARDINALS, CONTAINERS, DIAMOND_ORES, _above, _approach, _block, _cell,
    _dry_name, _in_reach, _nearby, _safe_excavation, _safe_status, _standing,
)
from .field import equip_item, mine_block_batch, place_block, scan_blocks
from .items import AIR, PICKAXE_ORDER, bare, record_mine_trail
from .mining import MineStepUnsafe, mine_step
from .observations import refresh_inventory as _fresh_inventory
from .recipes import RECIPES, craft_recipe
from .registry import register
from .windows import take_container_item, window_amount, window_view

_ARMOR = ("diamond_boots", "diamond_chestplate", "diamond_helmet", "diamond_leggings")
_DIAMOND_PICKS = ("diamond_pickaxe", "netherite_pickaxe")
_IRON_PICKS = tuple(pick for pick in PICKAXE_ORDER if pick in (*_DIAMOND_PICKS, "iron_pickaxe"))
_MAX_DESCENT = 24
_MAX_ORES = 16
_MIN_Y = -54
_SCAN_RADIUS = 16


def _world_block(state) -> str:
    st = state.snapshot()
    if not st.get("ready") or not st.get("alive") or not bare(st.get("dimension")):
        return "尚无当前维度及可行动状态，无法观察钻石线地形或窗口"
    return ""


def _durable(entry, minimum: int) -> bool:
    durability = (entry or {}).get("durability")
    return (isinstance(durability, (list, tuple)) and len(durability) == 2
            and type(durability[0]) is int and durability[0] >= minimum)


def _pick(inventory, names, minimum: int) -> str | None:
    held = inventory.get("held") or {}
    entries = (held, *(inventory.get("items") or ()))
    held_name = bare(held.get("name"))
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
        if not _in_reach(ex, current["pos"], pos, reach=3):
            continue
        block = _dry_name(_block(ex, pos))
        if block not in CONTAINERS:
            continue
        target = {"pos": list(pos)}
        try:
            view = yield from window_view(ex, target)
        except Replan as exc:
            code, _ = failure(exc)
            if code in {"out_of_reach", "path_not_found", "cannot_interact", "not_loaded"}:
                yield rec("window_view", target=target, error_code=code)
                continue
            raise
        current = _safe_status(ex, state, dimension or None)
        if (bare(view.get("window")) != block or _cell(current["pos"]) != _cell(status["pos"])
                or not _in_reach(ex, current["pos"], pos, reach=3)):
            continue
        for item in names:
            available = window_amount(view, "container", item)
            take = min(available, goal_qty - state.count(item))
            if take > 0:
                yield from take_container_item(ex, state, target, "container", item, take, view=view)
                if state.count(item) >= goal_qty:
                    return


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
        yield from _nearby_loot(ex, state, ("diamond",), goal_qty, status)
        if state.count("diamond") >= goal_qty:
            return
        start = _safe_status(ex, state, "overworld")
        if _pick(_fresh_inventory(ex, state), _IRON_PICKS, 1) is None:
            looted = state.count("diamond")
            if initial < looted < goal_qty:
                yield self._partial(initial, looted, goal_qty)
            raise Replan("开采暴露钻矿前请补有耐久的铁镐、钻石镐或下界合金镐")
        path = [_cell(start["pos"])]
        waypoints = [start["pos"]]
        tried_ores = set()
        visited_columns = {(path[0][0], path[0][2])}
        heading = CARDINALS[0]
        stairs = 0
        try:
            while len(tried_ores) < _MAX_ORES:
                status = _safe_status(ex, state, "overworld")
                origin = _cell(status["pos"])
                ores = _nearby(ex, state, DIAMOND_ORES, radius=_SCAN_RADIUS, limit=_MAX_ORES)
                for ore in ores:
                    if ore in tried_ores or len(tried_ores) >= _MAX_ORES:
                        continue
                    # Adjacent safe stances may extend the four-block digging budget by one.
                    if math.dist(origin, ore) > 6 or not _safe_excavation(ex, origin, (ore,), path):
                        continue
                    stand = yield from _approach(ex, state, ore, status, dimension="overworld", protected=path)
                    if stand is None:
                        continue
                    tried_ores.add(ore)
                    yield from _hand_pick(ex, state, 1)
                    stand = _safe_status(ex, state, "overworld")
                    if (not _in_reach(ex, stand["pos"], ore, reach=4)
                            or not _safe_excavation(ex, _cell(stand["pos"]), (ore,), path)):
                        raise Replan("换镐后钻矿的安全站位或可采性已经变化，请重新观察")
                    yield from mine_block_batch(ex, state, (ore,), block_types=DIAMOND_ORES,
                                                drop="diamond", count=1, max_blocks=1)
                    _safe_status(ex, state, "overworld")
                    if state.count("diamond") >= goal_qty:
                        return
                    break
                else:
                    if (_cell(status["pos"]) != path[-1] or stairs >= _MAX_DESCENT or origin[1] <= _MIN_Y):
                        break
                    step = yield from self._down_step(ex, state, origin, path, visited_columns, heading)
                    if step is None:
                        break
                    if step is False:
                        continue  # A fresh scan found a new reachable diamond face during the cut.
                    path.append(_cell(step))
                    waypoints.append(step)
                    heading = (path[-1][0] - origin[0], path[-1][2] - origin[2])
                    stairs += 1
                    if stairs % 4 == 0:
                        minerals = ["lapis_ore", "deepslate_lapis_ore", "iron_ore", "deepslate_iron_ore"]
                        if path[-1][1] < 0:
                            minerals += ["redstone_ore", "deepslate_redstone_ore"]
                        observed = scan_blocks(ex, state, minerals, radius=8, limit=6)
                        if observed["blocks"]:
                            yield rec("diamond_prospect", observed)
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

    def _down_step(self, ex, state, origin, path, visited_columns, heading):
        x, y, z = origin
        for dx, dz in (heading, *(d for d in CARDINALS if d != heading)):
            landing = (x + dx, y - 1, z + dz)
            column = (landing[0], landing[2])
            cuts = tuple(_above(landing, dy) for dy in (0, 1, 2))
            if column in visited_columns or not _safe_excavation(ex, origin, cuts, path, landing=landing):
                continue
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
                return any(_in_reach(ex, status["pos"], ore, reach=4)
                           and _safe_excavation(ex, origin, (ore,), path)
                           for ore in ores) or None

            try:
                dest, evidence = yield from mine_step(
                    ex, state, landing, allowed_picks=frozenset((pick,)), after_dig=observe_cut,
                )
            except MineStepUnsafe as exc:
                if exc.stage == "move" or _cell(_safe_status(ex, state, "overworld")["pos"]) != origin:
                    raise
                continue
            if evidence:
                return False
            after = _safe_status(ex, state, "overworld")
            if _cell(after["pos"]) != landing or not all(_standing(ex, cell) for cell in path[-2:]):
                raise Replan("下一级落脚或回程阶梯支撑未确认，停止下探")
            return dest
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
        tables = _nearby(ex, state, ("crafting_table",), radius=4, limit=5)
        if not any(math.dist(current["pos"], cell) <= 3.5
                   and _in_reach(ex, current["pos"], cell, reach=3.5) for cell in tables):
            x, y, z = _cell(current["pos"])
            sites = [(x + dx, y, z + dz) for dx, dz in CARDINALS
                     if _standing(ex, (x + dx, y, z + dz))]
            if len(sites) < 2 or not state.count("crafting_table"):
                raise EnvBlocked("缺少可达工作台或可保留退路的放台位，请携带工作台并换站位",
                                 missing_predicate="station_ready:crafting_table",
                                 searched_volume="半径 4 格至多 5 个暴露工作台和 4 个同层邻接放台位",
                                 wake_on="chunk_moved")
            yield from place_block(ex, state, "crafting_table", sites[0])
            _fresh_inventory(ex, state)
        yield from craft_recipe(ex, state, item, goal_qty=1)
        if not _already_has_gear(item, state):
            raise Replan("钻石装备合成后未确认可用成品，检查背包和耐久")
