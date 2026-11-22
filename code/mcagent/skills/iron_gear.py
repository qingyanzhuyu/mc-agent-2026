"""Craft exact iron equipment using the shared vanilla recipe table."""
from __future__ import annotations

from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, rec
from .camp_plan import armor_equip_slot_for
from .mine_return import ensure_crafting_table_ready
from .recipes import RECIPES, _ingredient_items, craft_recipe
from .registry import register
from .world_memo import ALL_LOG_BLOCKS


_GEAR = (
    "iron_pickaxe", "iron_boots", "iron_helmet", "iron_chestplate", "iron_leggings",
    "bucket", "shield", "iron_sword", "flint_and_steel", "shears", "compass",
)
_PLANK_ITEMS = tuple(item for item in RECIPES if item.endswith("_planks"))


def _shield_affordable_with_logs(state) -> bool:
    if state.count("iron_ingot") < 1:
        return False
    return sum(state.count(p) for p in _PLANK_ITEMS) + sum(state.count(l) for l in ALL_LOG_BLOCKS) * 4 >= 6


def _affordable(item, state):
    if item == "shield" and _shield_affordable_with_logs(state):
        return True
    return all(sum(state.count(name) for name in _ingredient_items(ingredient)) >= qty
               for ingredient, qty in RECIPES[item].ingredients)


@register
class CraftIronGear(Skill):
    name = "craft_iron_gear"
    description = "按配方核对铁料并制造指定装备、桶、盾或工具"
    params = {"item": Param(str, "指定成品", choices=_GEAR)}
    goal = "acq:{item}"
    verified = "offline"

    @classmethod
    def blocked(cls, state):
        if any(state.count(item) or _affordable(item, state) for item in _GEAR):
            return ""
        return "缺少铁锭或配套材料，当前无法制作铁器"

    def can_start(self, state):
        item = self.args["item"]
        return bool(state.count(item) or _affordable(item, state))

    @classmethod
    def material_plan(cls, args: dict, state) -> tuple[tuple[str, int, str], ...]:
        item = args["item"]
        if state.count(item):
            return ()
        materials: dict[str, int] = {}
        for ingredient, count in RECIPES[item].ingredients:
            alternatives = _ingredient_items(ingredient)
            for name in alternatives:
                available = max(0, state.count(name) - materials.get(name, 0))
                take = min(count, available)
                if take:
                    materials[name] = materials.get(name, 0) + take
                    count -= take
                if not count:
                    break
            if count:
                name = alternatives[0]
                materials[name] = materials.get(name, 0) + count
        return tuple((name, qty, "consume") for name, qty in materials.items())

    def run(self, ex, state):
        item = self.args["item"]
        if item == "shield":
            while sum(state.count(p) for p in _PLANK_ITEMS) < 6:
                log = next((l for l in ALL_LOG_BLOCKS if state.count(l) >= 1), None)
                if log is None:
                    break
                plank = log.removesuffix("_log") + "_planks"
                yield from craft_recipe(ex, state, plank, goal_qty=state.count(plank) + 4)
        try:
            crafted = yield from craft_recipe(ex, state, item, goal_qty=1)
        except EnvBlocked as err:
            if err.missing_predicate != "station_ready:crafting_table":
                raise
            ready_pos = yield from ensure_crafting_table_ready(
                ex,
                state,
                reserve_planks=(6 if item == "shield" else 0),
                skip_nearby_scan=True,
            )
            if ready_pos is None:
                raise
            crafted = yield from craft_recipe(ex, state, item, goal_qty=1)
        if isinstance(crafted, dict) and int(crafted.get("gained", 0) or 0) > 0:
            slot = armor_equip_slot_for(item)
            if slot is not None and state.count(item) >= 1:
                try:
                    yield rec("equip", ex.equip(item, slot), item=item, slot=slot)
                    state.refresh_inventory(ex.client)
                except Replan:
                    pass

    def verify_done(self, state, before_counts):
        item = self.args["item"]
        return "" if state.count(item) >= 1 else f"背包未确认指定铁器 {item}"
