"""P4 在纯快照上选定一种武器/弹药路线，并声明该路线的实际物资。"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..runtime import Replan
from .recipes import RECIPES, _ingredient_items, craft_recipe

SWORDS = tuple(f"{tier}_sword" for tier in ("netherite", "diamond", "iron", "copper", "stone", "golden", "wooden"))
ARROWS = ("arrow", "spectral_arrow", "tipped_arrow")
ROCKET = "firework_rocket"
AMMO = (*ARROWS, ROCKET)
MaterialPlan = tuple[tuple[str, int, str], ...]


@dataclass(frozen=True)
class Loadout:
    weapon: str
    ammo: str | None
    crafts: tuple[str, ...]
    materials: MaterialPlan


def carried_sword(state):
    return next((weapon for weapon in SWORDS if state.count(weapon) > 0), None)


def _reserve_recipe(state, item, crafting, requirements):
    """批次内原料折算到开工前库存；只选定实际木板类型。"""
    for ingredient, count in RECIPES[item].ingredients:
        if ingredient in crafting:
            continue
        variants = _ingredient_items(ingredient)
        remaining = count
        for material in variants:
            available = max(0, state.count(material) - requirements[material])
            used = min(available, remaining)
            if used:
                requirements[material] += used
                remaining -= used
            if not remaining:
                break
        if remaining:
            requirements[variants[0]] += remaining


def _loadout(state, weapon, ammo):
    crafts = []
    borrowed = []
    required = Counter()
    if state.count(weapon) > 0:
        borrowed.append((weapon, 1, "borrow"))
    else:
        if weapon == "crossbow" and state.count("tripwire_hook") == 0:
            crafts.append("tripwire_hook")
        crafts.append(weapon)
    if ammo is not None:
        if state.count(ammo):
            required[ammo] = 1
        else:
            crafts.append("arrow")
    for item in crafts:
        _reserve_recipe(state, item, crafts, required)
    materials = tuple((item, amount, "consume") for item, amount in required.items())
    return Loadout(weapon, ammo, tuple(crafts), (*borrowed, *materials))


def _shortages(state, plan):
    missing = [(item, amount - state.count(item)) for item, amount, _ in plan.materials
               if amount > state.count(item)]
    if (not state.count("fishing_rod")
            and (string := next((amount for item, amount, _ in plan.materials if item == "string"), 0))
            and state.count("string") < string + 2):
        missing = [(item, amount) for item, amount in missing if item != "string"]
        missing.append(("string（含给钓竿留的 2 根）", string + 2 - state.count("string")))
    return missing


def shield_plan(state):
    return _loadout(state, "shield", None)


def _ammo_for(state, weapon, *, arrows_only):
    compatible = AMMO if weapon == "crossbow" else ARROWS
    carried = [item for item in compatible if state.count(item)]
    if not carried:
        return "arrow"
    if len(carried) != 1 or (arrows_only and carried[0] == ROCKET):
        return None
    return carried[0]


def ranged_plan(state, *, crossbow_only):
    """§4.7 未说明自动选弹优先序。无法唯一预留时切换计划或退出。"""
    weapons = ("crossbow",) if crossbow_only else ("crossbow", "bow")
    weapons = sorted(weapons, key=lambda item: not state.count(item))
    plans = []
    for weapon in weapons:
        ammo = _ammo_for(state, weapon, arrows_only=not crossbow_only)
        if ammo is None:
            continue
        plan = _loadout(state, weapon, ammo)
        if not _shortages(state, plan):
            return plan
        plans.append(plan)
    return plans[0] if plans else None


def blocked_loadout(state, plan):
    if plan is None:
        return "兼容弹药种类不唯一或当前只有烟花，无法声明本次弹药"
    missing = _shortages(state, plan)
    if missing:
        summary = "、".join(f"{amount} 个 {name}" for name, amount in missing)
        return f"战斗备料还缺 {summary}"
    return ""


def prepare_loadout(ex, state, plan):
    reason = blocked_loadout(state, plan)
    if reason:
        raise Replan(reason)
    for item in plan.crafts:
        yield from craft_recipe(ex, state, item, goal_qty=1)
    if not state.count(plan.weapon) or (plan.ammo is not None and not state.count(plan.ammo)):
        raise Replan("备料后未在背包确认武器和弹药")
