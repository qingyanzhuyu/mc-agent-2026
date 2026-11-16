"""Craft exact iron equipment using the shared vanilla recipe table."""
from __future__ import annotations

from .base import Param, Skill
from .recipes import RECIPES, _ingredient_items, craft_recipe
from .registry import register


_GEAR = (
    "iron_pickaxe", "iron_boots", "iron_helmet", "iron_chestplate", "iron_leggings",
    "bucket", "shield", "iron_sword", "flint_and_steel", "shears", "compass",
)


def _affordable(item, state):
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
        yield from craft_recipe(ex, state, self.args["item"], goal_qty=1)

    def verify_done(self, state, before_counts):
        item = self.args["item"]
        return "" if state.count(item) >= 1 else f"背包未确认指定铁器 {item}"
