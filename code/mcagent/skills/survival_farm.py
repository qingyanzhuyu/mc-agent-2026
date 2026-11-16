"""按原版放置、繁殖判据组织一次有界的农牧操作。"""
from __future__ import annotations

import math
from itertools import combinations

from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, failure, rec
from .field import collect_drops, scan_blocks, scan_entities
from .interaction import use_item_on
from .items import AIR, bare
from .recipes import craft_recipe
from .registry import register
from .survival import (
    _BREED_PROGRESS, _PLANT_PROGRESS, _above, _approach, _block, _can_reach, _center,
    _fresh_inventory, _has_progress, _plank_plan, _site_missing, _stand_for,
)


# criteria-1.21.11.json husbandry/plant_seed，物品映射到本次必须观察到的作物方块。
_CROPS = {
    "wheat_seeds": "wheat", "beetroot_seeds": "beetroots", "pumpkin_seeds": "pumpkin_stem",
    "melon_seeds": "melon_stem", "nether_wart": "nether_wart", "torchflower_seeds": "torchflower_crop",
    "pitcher_pod": "pitcher_crop",
}
_WHEAT = "wheat_seeds"
_HOES = ("netherite_hoe", "diamond_hoe", "iron_hoe", "copper_hoe", "stone_hoe", "wooden_hoe", "golden_hoe")
_TILLABLE = ("dirt", "grass_block")
_GRASS = ("short_grass", "tall_grass")
_BLOCK_RADIUS = 12
_BLOCK_LIMIT = 10
_ANIMAL_RADIUS = 16
_FEEDS = {
    "chicken": (_WHEAT,), "cow": ("wheat",), "sheep": ("wheat",),
    "pig": ("carrot", "potato", "beetroot"),
}


def _find_seed(state):
    return next((seed for seed in _CROPS if state.count(seed) >= 1), None)


def _held_hoe(state):
    return next((item for item in _HOES if state.count(item)), None)


def _has_farm_tools(state):
    return _held_hoe(state) or (state.count("stick") >= 2 and _plank_plan(state, 2))


def _gather_wheat(ex, state, goal_qty):
    grass = scan_blocks(ex, state, _GRASS, radius=_BLOCK_RADIUS, limit=8)
    for row in grass["blocks"]:
        pos = tuple(row["pos"])
        status = state.refresh_status(ex.client, max_age_s=0)
        _fresh_inventory(ex, state)
        if state.count(_WHEAT) >= goal_qty:
            return
        stand = _stand_for(ex, status, _center(pos))
        if stand is None:
            continue
        yield from _approach(ex, state, status, stand, _center(pos))
        if _block(ex, pos) not in _GRASS:
            continue
        try:
            dug = ex.dig(pos, collect=True, stop_on=("hurt", "death"))
        except Replan as exc:
            code, extra = failure(exc)
            state.refresh_status(ex.client, max_age_s=0)
            _fresh_inventory(ex, state)
            if code in {"dig_unconfirmed", "out_of_reach", "protected_region"}:
                yield rec("dig", pos=pos, error_code=code, error=extra)
                continue
            raise
        yield rec("dig", dug, pos=pos)
        state.refresh_status(ex.client, max_age_s=0)
        _fresh_inventory(ex, state)
        if _block(ex, pos) not in AIR:
            continue
        if state.count(_WHEAT) >= goal_qty:
            return
        try:
            yield from collect_drops(ex, state, _WHEAT, count=1)
        except EnvBlocked:
            continue  # 草不保证掉种子，仍可试其余有界候选。
        if state.count(_WHEAT) >= goal_qty:
            return
    raise _site_missing(f"可达草丛未凑齐 {goal_qty} 粒小麦种子，请换观测区或补种子",
                        "reachable_wheat_seeds", _BLOCK_RADIUS, 8)


def _planting_site(ex, state, seed):
    base = "soul_sand" if seed == "nether_wart" else "farmland"
    types = (base,) if seed == "nether_wart" else (base, *_TILLABLE)
    candidates = scan_blocks(ex, state, types, radius=_BLOCK_RADIUS, limit=_BLOCK_LIMIT)["blocks"]
    candidates.sort(key=lambda row: row["name"] != base)
    status = state.refresh_status(ex.client, max_age_s=0)
    for row in candidates:
        pos = tuple(row["pos"])
        if _block(ex, _above(pos)) in AIR:
            stand = _stand_for(ex, status, _center(pos))
            if stand is not None:
                return base, pos, stand, status
    raise _site_missing("未观察到上方净空并且有安全站位的播种地块",
                        f"reachable_planting_site:{base}", _BLOCK_RADIUS, _BLOCK_LIMIT)


@register
class PlantSeed(Skill):
    name = "plant_seed"
    goal = "goal:plant_seed"
    description = "选可达地块并种下符合进度判据的种子"
    verified = "offline"

    @classmethod
    def blocked(cls, state):
        return "" if _find_seed(state) or _has_farm_tools(state) else "先备种子、锄头或木锄原料"

    @classmethod
    def material_plan(cls, args: dict, state) -> tuple[tuple[str, int, str], ...]:
        seed = _find_seed(state)
        plan = ((seed, 1, "consume"),) if seed else ()
        if seed == "nether_wart":
            return plan
        if hoe := _held_hoe(state):
            return (*plan, (hoe, 1, "borrow"))
        if state.count("stick") >= 2 and (planks := _plank_plan(state, 2)):
            return (*plan, ("stick", 2, "consume"), *planks)
        return plan

    def run(self, ex, state):
        if _has_progress(ex, _PLANT_PROGRESS):
            return
        _fresh_inventory(ex, state)
        seed = _find_seed(state) or _WHEAT
        if state.count(seed) == 0:
            yield from _gather_wheat(ex, state, 1)
        base, pos, stand, status = _planting_site(ex, state, seed)
        if base == "farmland" and _block(ex, pos) in _TILLABLE:
            hoe = _held_hoe(state)
            if hoe is None:
                yield from craft_recipe(ex, state, "wooden_hoe", goal_qty=1)
                hoe = "wooden_hoe"
                base, pos, stand, status = _planting_site(ex, state, seed)
        else:
            hoe = None
        yield from _approach(ex, state, status, stand, _center(pos))
        if _block(ex, _above(pos)) not in AIR:
            raise Replan("播种前目标上方净空已变化，请换地块")
        current = _block(ex, pos)
        if current != base:
            if hoe is None or current not in _TILLABLE:
                raise Replan("地块已变化，请找空闲耕地或备相应的锄头")
            yield from use_item_on(ex, state, pos, hoe)
        if _block(ex, pos) != base or _block(ex, _above(pos)) not in AIR:
            raise Replan(f"耕作回执后未观察到可播种的 {base}")
        _fresh_inventory(ex, state)
        before = state.count(seed)
        yield from use_item_on(ex, state, pos, seed)
        if _block(ex, _above(pos)) != _CROPS[seed] or before - state.count(seed) != 1:
            raise Replan("交互后未同时确认新作物和一粒种子的扣除，请检查基底与种子")
        yield rec("verify_crop", {"crop": _CROPS[seed],
                                 "advancement_confirmed": _has_progress(ex, _PLANT_PROGRESS)})


def _feeding_route(args, state):
    animal = args.get("animal", "auto")
    choices = _FEEDS if animal == "auto" else (animal,)
    for species in choices:
        for food in _FEEDS[species]:
            if state.count(food) >= 2:
                return species, food
    animal = "chicken" if animal == "auto" else animal
    return animal, _FEEDS[animal][0]


def _baby(entity):
    """§3.5 未承诺年龄字段，只接受明确的幼年布尔观测。"""
    return entity.get("baby") is True or entity.get("is_baby") is True


def _animals(ex, state, species):
    return [entity for entity in scan_entities(ex, state, species, radius=_ANIMAL_RADIUS)["entities"]
            if entity.get("type") == "animal" and bare(entity.get("name")) == species
            and isinstance(entity.get("health"), (int, float)) and entity["health"] > 0]


def _pair_missing(species):
    return _site_missing(f"未观察到可安全接触的同种 {species} 亲本两只",
                         f"reachable_pair:{species}", _ANIMAL_RADIUS, 40)


def _find_pair(ex, state, species):
    animals = _animals(ex, state, species)
    status = state.refresh_status(ex.client, max_age_s=0)
    eligible = {entity["id"]: entity for entity in animals
                if not _baby(entity) and entity.get("in_love") is not True
                and _stand_for(ex, status, entity["pos"]) is not None}
    for first, second in combinations(eligible.values(), 2):
        if math.dist(first["pos"], second["pos"]) <= 6:
            return first, second
    raise _pair_missing(species)


def _recheck_pair(ex, state, species, ids):
    observed = _animals(ex, state, species)
    by_id = {animal["id"]: animal for animal in observed if not _baby(animal)}
    if not all(entity_id in by_id for entity_id in ids):
        raise _pair_missing(species)
    first, second = (by_id[entity_id] for entity_id in ids)
    if math.dist(first["pos"], second["pos"]) > 6:
        raise _pair_missing(species)
    return by_id, observed


def _new_baby(observed, baseline, parents):
    return any(row["id"] not in baseline and _baby(row)
               and all(math.dist(row["pos"], parent["pos"]) <= 6 for parent in parents)
               for row in observed)


@register
class BreedAnimal(Skill):
    name = "breed_animal"
    goal = "goal:breed_an_animal"
    description = "用两份匹配饲料繁殖一对可安全接触的动物"
    params = {"animal": Param(str, "动物种类，自动时优先选择已有饲料的种类",
                              default="auto", choices=("auto", *_FEEDS))}
    verified = "offline"

    @classmethod
    def blocked(cls, state):
        has_food = state.count(_WHEAT) >= 1 or any(
            state.count(food) >= 2 for foods in _FEEDS.values() for food in foods
        )
        return "" if has_food or _has_farm_tools(state) else "先备两份匹配饲料，或准备搜集小麦种子"

    def can_start(self, state):
        _, food = _feeding_route(self.args, state)
        if food == _WHEAT:
            return bool(state.count(food) or _has_farm_tools(state))
        return state.count(food) >= 2

    @classmethod
    def material_plan(cls, args: dict, state) -> tuple[tuple[str, int, str], ...]:
        _, food = _feeding_route(args, state)
        held = min(2, state.count(food)) if food == _WHEAT else 2
        return ((food, held, "consume"),) if held else ()

    def run(self, ex, state):
        if _has_progress(ex, _BREED_PROGRESS):
            return
        _fresh_inventory(ex, state)
        species, food = _feeding_route(self.args, state)
        if state.count(food) < 2:
            if food != _WHEAT:
                raise Replan(f"繁殖 {species} 需要两份 {food}，请先备足饲料")
            yield from _gather_wheat(ex, state, 2)
        try:
            parents = _find_pair(ex, state, species)
        except EnvBlocked:
            if self.args["animal"] != "auto" or species != "cow":
                raise
            species = "sheep"  # 同一份小麦预留仍适用，避免消耗未声明的替代饲料。
            parents = _find_pair(ex, state, species)
        ids = tuple(parent["id"] for parent in parents)
        baseline = set(ids)
        for remaining, parent_id in zip((2, 1), ids):
            pair, seen = _recheck_pair(ex, state, species, ids)
            baseline.update(row["id"] for row in seen)
            status = state.refresh_status(ex.client, max_age_s=0)
            target = pair[parent_id]["pos"]
            stand = _stand_for(ex, status, target)
            if stand is None:
                raise _pair_missing(species)
            status = yield from _approach(ex, state, status, stand, target)
            pair, seen = _recheck_pair(ex, state, species, ids)
            baseline.update(row["id"] for row in seen)
            if not _can_reach(status, pair[parent_id]["pos"], 4):
                raise _pair_missing(species)
            _fresh_inventory(ex, state)
            before = state.count(food)
            if before < remaining:
                raise Replan(f"剩余 {food} 不足 {remaining} 份，暂停喂食")
            yield from use_item_on(ex, state, parent_id, food)
            if before - state.count(food) != 1:
                raise Replan(f"喂食后未确认本次仅消耗一份 {food}")
        for _ in range(3):
            if _has_progress(ex, _BREED_PROGRESS):
                yield rec("verify_breeding", {"advancement_confirmed": True})
                return
            if _new_baby(_animals(ex, state, species), baseline, pair.values()):
                yield rec("verify_breeding", {"advancement_confirmed": False, "observed_offspring": True})
                return
            yield rec("observe_breeding")
        raise Replan("两只亲本已喂食，但尚未观察到繁殖进度或新幼崽")
