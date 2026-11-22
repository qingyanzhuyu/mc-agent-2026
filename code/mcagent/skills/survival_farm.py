"""按原版放置、繁殖判据组织一次有界的农牧操作。"""
from __future__ import annotations

import math
from itertools import combinations

from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, failure, rec
from .camp_plan import state_dim
from .field import collect_drops, scan_blocks, scan_entities
from .interaction import use_item_on
from .items import AIR, bare
from .recipes import RECIPES, _ingredient_items, craft_recipe
from .registry import register
from .sightline import ENTITY_EYE_REACH, block_center
from .survival import (
    _BREED_PROGRESS, _PLANKS, _PLANT_PROGRESS, _SUPPORT, _above, _approach, _block,
    _can_reach, _cell, _fresh_inventory, _has_progress, _neighbors, _plank_plan,
    _require_ready, _site_missing, _stand_for,
)
from .voxel_cache import voxel_cache_for
from .workstations import reclaim_field_station, record_placed_station


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
_BLOCK_RADIUS, _BLOCK_LIMIT = 12, 10
_ANIMAL_RADIUS = 32
_GRASS_RADIUS, _GRASS_LIMIT = 32, 48
_FEEDS = {
    "chicken": (_WHEAT,), "cow": ("wheat",), "sheep": ("wheat",),
    "pig": ("carrot", "potato", "beetroot"),
}


def _find_seed(state):
    return next((seed for seed in _CROPS if state.count(seed) >= 1), None)


def _held_hoe(state):
    return next((item for item in _HOES if state.count(item)), None)


def _total_planks(state) -> int:
    return sum(state.count(item) for item in _PLANKS)


def _raw_wood_plank_potential(state) -> int:
    total = 0
    for plank in _PLANKS:
        recipe = RECIPES[plank]
        for ing, needed in recipe.ingredients:
            stock = sum(state.count(name) for name in _ingredient_items(ing))
            total += (stock // needed) * recipe.output
    return total


def _has_farm_tools(state):
    if _held_hoe(state):
        return True
    planks_needed = 2 + (2 if state.count("stick") < 2 else 0)
    return (_total_planks(state) + _raw_wood_plank_potential(state)) >= planks_needed


def _craft_planks_from_logs(ex, state, needed_planks: int):
    while _total_planks(state) < needed_planks:
        crafted_any = False
        for plank in _PLANKS:
            recipe = RECIPES[plank]
            if all(
                sum(state.count(name) for name in _ingredient_items(ing)) >= needed
                for ing, needed in recipe.ingredients
            ):
                yield from craft_recipe(ex, state, plank, goal_qty=state.count(plank) + recipe.output)
                crafted_any = True
                break
        if not crafted_any:
            break


def _place_local_table(ex, state):
    status = state.refresh_status(ex.client, max_age_s=0)
    _require_ready(status)
    self_cell = _cell(status["pos"])
    for cand in _neighbors(self_cell):
        if _block(ex, cand) in AIR and _block(ex, _above(cand, -1)) in _SUPPORT:
            res = ex.place(item="crafting_table", pos=list(cand))
            voxel_cache_for(state).invalidate(cand, dimension=state_dim(state))
            yield rec("place", res, item="crafting_table", pos=list(cand))
            record_placed_station(state, "crafting_table", cand)
            _fresh_inventory(ex, state)
            return cand
    return None


def _craft_wooden_hoe(ex, state):
    planks_needed = 2 + (2 if state.count("stick") < 2 else 0)
    if _total_planks(state) + _raw_wood_plank_potential(state) >= planks_needed:
        yield from _craft_planks_from_logs(ex, state, planks_needed)
        if state.count("stick") < 2 and _total_planks(state) >= 2:
            yield from craft_recipe(ex, state, "stick", goal_qty=2)
    try:
        yield from craft_recipe(ex, state, "wooden_hoe", goal_qty=1)
        return
    except EnvBlocked as exc:
        if exc.missing_predicate != "station_ready:crafting_table":
            raise
        if state.count("crafting_table") < 1:
            if _total_planks(state) + _raw_wood_plank_potential(state) < 6:
                raise
            yield from _craft_planks_from_logs(ex, state, 6)
            yield from craft_recipe(ex, state, "crafting_table", goal_qty=1)
        tpos = yield from _place_local_table(ex, state)
        if not tpos:
            raise
    yield from craft_recipe(ex, state, "wooden_hoe", goal_qty=1)
    yield from reclaim_field_station(ex, state, "crafting_table", tpos, newly_placed=True)


def _gather_wheat(ex, state, goal_qty, *, radius=_GRASS_RADIUS, limit=_GRASS_LIMIT):
    grass = scan_blocks(ex, state, _GRASS, radius=radius, limit=limit)
    if grass.get("ready") is False:
        raise Replan("世界区块加载尚未就绪（not_ready），暂无法扫描草丛")
    for row in grass["blocks"]:
        pos = tuple(row["pos"])
        status = state.refresh_status(ex.client, max_age_s=0)
        _fresh_inventory(ex, state)
        if state.count(_WHEAT) >= goal_qty:
            return
        target = block_center(pos)
        stand = _stand_for(ex, status, target, block_pos=pos)
        if stand is None:
            continue
        try:
            yield from _approach(ex, state, status, stand, target, block_pos=pos)
        except Replan:
            continue
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
                        "reachable_wheat_seeds", radius, limit)


def _planting_site(ex, state, seed):
    base = "soul_sand" if seed == "nether_wart" else "farmland"
    types = (base,) if seed == "nether_wart" else (base, *_TILLABLE)
    scan = scan_blocks(ex, state, types, radius=_BLOCK_RADIUS, limit=_BLOCK_LIMIT)
    if scan.get("ready") is False:
        raise Replan("世界区块加载尚未就绪（not_ready），暂无法扫描播种地块")
    candidates = scan["blocks"]
    candidates.sort(key=lambda row: row["name"] != base)
    status = state.refresh_status(ex.client, max_age_s=0)
    _require_ready(status)
    for row in candidates:
        pos = tuple(row["pos"])
        if _block(ex, _above(pos)) in AIR:
            stand = _stand_for(ex, status, block_center(pos), block_pos=pos)
            if stand is not None:
                return base, pos, stand, status
    raise _site_missing("未观察到上方净空并且有安全站位的播种地块",
                        f"reachable_planting_site:{base}", _BLOCK_RADIUS, _BLOCK_LIMIT)


@register
class PlantSeed(Skill):
    name = "plant_seed"
    goal = "goal:plant_seed"
    description = "选可达地块并种下符合进度判据的种子"
    verified = "live"

    @classmethod
    def blocked(cls, state):
        if state.snapshot().get("ready") is False:
            return "世界正在加载区块和实体（ready=false），请稍候"
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
                yield from _craft_wooden_hoe(ex, state)
                hoe = "wooden_hoe"
                base, pos, stand, status = _planting_site(ex, state, seed)
        else:
            hoe = None
        yield from _approach(ex, state, status, stand, block_center(pos), block_pos=pos)
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


def _scan_live_animals(ex, state, query, allowed):
    scan = scan_entities(ex, state, query, radius=_ANIMAL_RADIUS)
    if scan.get("ready") is False:
        raise Replan(f"世界实体加载尚未就绪（not_ready），暂无法观察 {query or 'auto'}")
    return [entity for entity in scan["entities"]
            if entity.get("type") == "animal" and bare(entity.get("name")) in allowed
            and isinstance(entity.get("health"), (int, float)) and entity["health"] > 0]


def _animals(ex, state, species):
    return _scan_live_animals(ex, state, species, {species})


def _pair_missing(species):
    return _site_missing(f"未观察到可安全接触的同种 {species} 亲本两只", f"reachable_pair:{species}", _ANIMAL_RADIUS, 40)


def _scan_animal_pairs(ex, state, requested: str) -> tuple[str | None, tuple[dict, dict] | None]:
    allowed = set(_FEEDS) if requested == "auto" else {requested}
    animals = _scan_live_animals(ex, state, None if requested == "auto" else requested, allowed)
    status = state.refresh_status(ex.client, max_age_s=0)
    _require_ready(status)
    by_species: dict[str, dict[int, dict]] = {}
    for e in animals:
        if not _baby(e) and e.get("in_love") is not True and _stand_for(ex, status, e["pos"]) is not None:
            by_species.setdefault(bare(e.get("name")), {})[e["id"]] = e
    paired = {
        sp: p for sp, el in by_species.items()
        if (p := next(((a, b) for a, b in combinations(el.values(), 2) if math.dist(a["pos"], b["pos"]) <= 6), None))
    }
    if not paired:
        return None, None
    if requested != "auto":
        return requested, paired[requested]
    sp = (next((s for s in _FEEDS if s in paired and any(state.count(f) >= 2 for f in _FEEDS[s])), None)
          or ("chicken" if "chicken" in paired else next(s for s in _FEEDS if s in paired)))
    return sp, paired[sp]


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
    verified = "live"

    @classmethod
    def blocked(cls, state):
        if state.snapshot().get("ready") is False:
            return "世界正在加载区块和实体（ready=false），请稍候"
        has_food = state.count(_WHEAT) >= 1 or any(state.count(f) >= 2 for fs in _FEEDS.values() for f in fs)
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
        requested = self.args["animal"]
        if requested != "auto":
            req_species, req_food = _feeding_route(self.args, state)
            if req_food != _WHEAT and state.count(req_food) < 2:
                raise Replan(f"繁殖 {req_species} 需要两份 {req_food}，请先备足饲料")
        species, parents = _scan_animal_pairs(ex, state, requested)
        if species is None or parents is None:
            raise _pair_missing(_feeding_route(self.args, state)[0])
        food = next((f for f in _FEEDS[species] if state.count(f) >= 2), _FEEDS[species][0])
        if state.count(food) < 2:
            if food != _WHEAT:
                raise Replan(f"附近有成对 {species}，但需要两份 {food}，请先备足饲料")
            yield from _gather_wheat(ex, state, 2, radius=_GRASS_RADIUS, limit=_GRASS_LIMIT)
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
            if not _can_reach(status, pair[parent_id]["pos"], ENTITY_EYE_REACH):
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
