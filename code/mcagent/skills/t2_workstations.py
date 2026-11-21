"""T2 工作站与实体交互技能：村民交易、盔甲纹饰、下界合金锄、酿造药水与治疗僵尸村民。"""
from __future__ import annotations

import math
import time
from typing import Optional

from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, rec
from .field import _cell, _observed_block, _standable_cell, move_local, place_block, scan_blocks, scan_entities
from .interaction import use_item, use_item_on
from .items import bare
from .landmark_nav import goto_known_landmark_or_biome
from .observations import refresh_inventory
from .recipes import RECIPES, craft_recipe
from .registry import register
from .sightline import ENTITY_EYE_REACH, STATION_EYE_REACH, can_reach_block, eye_to_block_dist, eye_to_point_dist, feet_pos
from .trade_smith_brew import brew_batch, smith_once, trade_once
from .world_memo import memo_for

_ADJACENT = ((1, 0), (-1, 0), (0, 1), (0, -1))
_TRIM_MATERIALS = ("iron_ingot", "copper_ingot", "gold_ingot", "lapis_lazuli", "emerald", "diamond", "redstone", "quartz", "amethyst_shard", "netherite_ingot")
_ARMOR_SUFFIXES = ("_helmet", "_chestplate", "_leggings", "_boots")
_BREW_INGREDIENTS = {"mundane": "redstone", "weakness": "fermented_spider_eye", "weakness_splash": "fermented_spider_eye"}


def _find_adjacent_stand(ex, pos, *, origin=None, max_dist: Optional[float] = None):
    bx, by, bz = _cell(pos)
    for dy in (0, -1, 1):
        for dx, dz in _ADJACENT:
            cand = (bx + dx, by + dy, bz + dz)
            if (origin is None or max_dist is None or math.dist(origin, (cand[0] + 0.5, cand[1], cand[2] + 0.5)) <= max_dist) and _standable_cell(ex, cand):
                return cand
    return None


def _find_station_nearby(ex, state, station: str, radius: int, *, skill_name: str = ""):
    status = state.refresh_status(ex.client, max_age_s=0)
    if status.get("ready") is False:
        raise Replan("世界区块和实体加载尚未就绪（not_ready），请等 ready=true 后再试")
    dim = str(status.get("dimension") or "overworld")
    scan = scan_blocks(ex, state, (station,), radius=radius, limit=8, skill_name=skill_name)
    if scan.get("ready") is False:
        raise Replan("世界区块加载尚未就绪（not_ready），暂无法扫描工作站")
    for block in sorted(scan.get("blocks") or (), key=lambda b: eye_to_block_dist(status, b["pos"])):
        pos = tuple(block["pos"])
        if can_reach_block(ex, status, pos, reach=STATION_EYE_REACH):
            memo_for(state).record_station(station, pos, dimension=dim)
            return list(pos)
        stand = _find_adjacent_stand(ex, pos)
        if stand is not None:
            cx, cy, cz = stand
            yield from move_local(ex, state, (cx + 0.5, cy, cz + 0.5), rng=1, max_distance=min(max(radius, 16), 64))
        else:
            yield rec("move_to", ex.move_to(pos=list(pos), rng=2), pos=list(pos))
        status = state.refresh_status(ex.client, max_age_s=0)
        if bare(_observed_block(ex, pos).get("name")) == station and (can_reach_block(ex, status, pos, reach=STATION_EYE_REACH) or eye_to_block_dist(status, pos) <= STATION_EYE_REACH + 1.0):
            memo_for(state).record_station(station, pos, dimension=dim)
            return list(pos)
    return None


def _ensure_station(ex, state, station: str, radius: int, *, skill_name: str = "", landmark: Optional[str] = None):
    if (found := (yield from _find_station_nearby(ex, state, station, radius, skill_name=skill_name))) is not None:
        return found
    refresh_inventory(ex, state)
    status = state.refresh_status(ex.client, max_age_s=0)
    dim = str(status.get("dimension") or "overworld")
    if state.count(station) >= 1:
        px, py, pz = _cell(status.get("pos"))
        for dx, dz in _ADJACENT:
            cand = (px + dx, py, pz + dz)
            if _standable_cell(ex, cand):
                yield from place_block(ex, state, station, cand)
                memo_for(state).record_station(station, cand, dimension=dim)
                return list(cand)
    if landmark and (yield from goto_known_landmark_or_biome(ex, state, landmark)):
        if (found := (yield from _find_station_nearby(ex, state, station, radius, skill_name=skill_name))) is not None:
            return found
    raise EnvBlocked(f"{radius} 格内未找到可达的 {station}，且缺少可放置工作站或安全落点", missing_predicate=f"station_ready:{station}", searched_volume=f"半径 {radius} 格 {station}", wake_on="chunk_moved")


def _approach_entity(ex, state, entity: dict, *, reach: float = ENTITY_EYE_REACH, max_distance: int = 64):
    status = state.refresh_status(ex.client, max_age_s=0)
    if status.get("ready") is False:
        raise Replan("世界实体加载尚未就绪（not_ready），请稍后再试")
    if eye_to_point_dist(status, entity["pos"]) <= reach:
        return status
    stand = _find_adjacent_stand(ex, entity["pos"], origin=feet_pos(status), max_dist=max_distance)
    if stand is not None:
        cx, cy, cz = stand
        yield from move_local(ex, state, (cx + 0.5, cy, cz + 0.5), rng=1, max_distance=min(max_distance, 64))
    else:
        yield rec("move_to", ex.move_to(pos=list(entity["pos"]), rng=2), pos=list(entity["pos"]), entity_id=entity["id"])
    status = state.refresh_status(ex.client, max_age_s=0)
    if eye_to_point_dist(status, entity["pos"]) > reach + 1.5:
        raise Replan("接近目标实体后仍超出交互距离")
    return status


def _scan_sorted_entities(ex, state, types: tuple[str, ...], radius: int, *, label: str, pred: str, wake_on: str) -> list[dict]:
    scan = scan_entities(ex, state, types, radius=radius)
    if scan.get("ready") is False:
        raise Replan("世界实体加载尚未就绪（not_ready），请稍后再试")
    entities = scan.get("entities") or []
    if not entities:
        raise EnvBlocked(f"{radius} 格内未发现{label}", missing_predicate=pred, searched_volume=f"半径 {radius} 格{label}", wake_on=wake_on)
    status = state.refresh_status(ex.client, max_age_s=0)
    return sorted(entities, key=lambda e: eye_to_point_dist(status, e["pos"]))


def _scan_sorted_entities_or_landmark(ex, state, types: tuple[str, ...], radius: int, *, label: str, pred: str, wake_on: str, landmark: Optional[str] = None):
    try:
        return _scan_sorted_entities(ex, state, types, radius, label=label, pred=pred, wake_on=wake_on)
    except EnvBlocked:
        if not landmark or not (yield from goto_known_landmark_or_biome(ex, state, landmark)):
            raise
        return _scan_sorted_entities(ex, state, types, radius, label=label, pred=pred, wake_on=wake_on)


def _got_items(got) -> dict[str, int]:
    counts: dict[str, int] = {}
    if isinstance(got, str) and (name := bare(got)):
        counts[name] = 1
    elif isinstance(got, dict):
        pairs = [(got.get("name") or got.get("item"), got.get("count", 1))] if ("name" in got or "item" in got) else list(got.items())
        for raw, qty in pairs:
            if name := bare(raw):
                counts[name] = counts.get(name, 0) + int(qty or 1)
    elif isinstance(got, (list, tuple)):
        for entry in got:
            for k, v in _got_items(entry).items():
                counts[k] = counts.get(k, 0) + v
    return counts


def _trim_inputs(state) -> tuple[Optional[str], Optional[str], Optional[str]]:
    items = [r for r in ((state.inventory or {}).get("items") or ()) if isinstance(r, dict) and int(r.get("count", 0)) >= 1]
    template = next((bare(r.get("name")) for r in items if bare(r.get("name")).endswith("_armor_trim_smithing_template")), None)
    armor = next((bare(r.get("name")) for r in items if bare(r.get("name")).endswith(_ARMOR_SUFFIXES)), None)
    return template, armor, next((m for m in _TRIM_MATERIALS if state.count(m) >= 1), None)


def _has_weakness_splash(inventory) -> bool:
    return any(
        isinstance(r, dict) and int(r.get("count", 0)) >= 1 and bare(r.get("name")) == "splash_potion"
        and (not r.get("potion") or "weakness" in bare(r["potion"]).lower())
        for r in (inventory or {}).get("items") or ()
    )


def _has_weakness_potion(inventory) -> bool:
    return any(
        isinstance(r, dict) and int(r.get("count", 0)) >= 1 and bare(r.get("name")) == "potion"
        and bool(r.get("potion")) and "weakness" in bare(r["potion"]).lower()
        for r in (inventory or {}).get("items") or ()
    )


def _can_craft_golden_apple(state) -> bool:
    return state.count("gold_ingot") >= 8 and state.count("apple") >= 1


@register
class TradeVillager(Skill):
    name, goal, verified = "trade_villager", "trade:villager", "offline"
    description = "寻找附近村民或流浪商人并完成一次可支付交易"
    params = {"radius": Param(int, "找村民半径", default=32, min=8, max=64)}
    _trade_result: Optional[dict] = None

    @classmethod
    def blocked(cls, state) -> str:
        items = [r for r in ((state.inventory or {}).get("items") or ()) if isinstance(r, dict) and int(r.get("count", 0)) >= 1]
        return "" if items else "背包为空，缺少可用于村民交易的物资或绿宝石"

    def run(self, ex, state):
        self._trade_result, radius = None, self.args["radius"]
        ordered = yield from _scan_sorted_entities_or_landmark(ex, state, ("villager", "wandering_trader"), radius, label="村民或流浪商人", pred="villager", wake_on="chunk_moved", landmark="village")
        last_exc: Optional[Replan] = None
        for ent in ordered:
            try:
                yield from _approach_entity(ex, state, ent, reach=ENTITY_EYE_REACH, max_distance=radius)
                res = yield from trade_once(ex, state, ent["id"])
            except Replan as exc:
                last_exc = exc
                continue
            self._trade_result = res
            yield rec("trade_evidence", res, entity_id=ent["id"], option=res["option"])
            return
        if last_exc is not None:
            raise last_exc

    def verify_done(self, state, before_counts=None) -> str:
        items = _got_items(self._trade_result.get("got")) if isinstance(self._trade_result, dict) else {}
        if not items:
            return "尚未完成有效的村民交易"
        for item, qty in items.items():
            cur = state.count(item)
            if cur < qty:
                return f"交易后背包未确认收到 {item}"
            if before_counts is not None and cur <= before_counts.get(item, 0):
                return f"交易后背包中 {item} 数量未增加"
        return ""


@register
class TrimArmor(Skill):
    name, goal, verified = "trim_armor", "smith:trim", "offline"
    description = "用锻造模板和矿物在锻造台为一件盔甲添加纹饰"
    params = {"radius": Param(int, "找锻造台半径", default=24, min=8, max=64)}
    _smith_result: Optional[dict] = None

    @classmethod
    def blocked(cls, state) -> str:
        template, armor, material = _trim_inputs(state)
        if not template:
            return "背包缺少盔甲纹饰锻造模板"
        return "背包缺少待锻造的盔甲" if not armor else ("" if material else "背包缺少可用的纹饰矿物材料")

    def run(self, ex, state):
        self._smith_result = None
        refresh_inventory(ex, state)
        template, armor, material = _trim_inputs(state)
        if not (template and armor and material):
            raise Replan(self.blocked(state) or "缺少纹饰锻造所需材料")
        pos = yield from _ensure_station(ex, state, "smithing_table", self.args["radius"], skill_name=self.name)
        res = yield from smith_once(ex, state, pos, template=template, base=armor, addition=material, expected_output=armor)
        self._smith_result = res
        yield rec("trim_armor_evidence", res, template=template, armor=armor, material=material)

    def verify_done(self, state, before_counts=None) -> str:
        res = self._smith_result
        if not isinstance(res, dict):
            return "尚未完成盔甲纹饰锻造"
        armor = bare(res.get("base") or res.get("output"))
        if not armor or state.count(armor) < 1:
            return "纹饰锻造后背包未确认目标盔甲"
        if before_counts is not None:
            for key in ("template", "addition"):
                item = bare(res.get(key))
                if item and item in before_counts and state.count(item) >= before_counts[item]:
                    return f"纹饰锻造后材料 {item} 未确认消耗"
        return ""


@register
class SmithNetheriteHoe(Skill):
    name, goal, verified = "smith_netherite_hoe", "smith:netherite_hoe", "offline"
    description = "用下界合金升级模板与锭将钻石锄升级为下界合金锄"
    params = {"radius": Param(int, "找锻造台半径", default=24, min=8, max=64)}

    @classmethod
    def blocked(cls, state) -> str:
        if state.count("netherite_hoe") >= 1:
            return ""
        if state.count("netherite_upgrade_smithing_template") < 1:
            return "背包缺少下界合金升级锻造模板"
        if state.count("netherite_ingot") < 1:
            return "背包缺少下界合金锭"
        if state.count("diamond_hoe") < 1 and not (state.count("diamond") >= 2 and state.count("stick") >= 2):
            return "背包缺少钻石锄（或 2 颗钻石与 2 根木棍）"
        return ""

    def run(self, ex, state):
        refresh_inventory(ex, state)
        if state.count("netherite_hoe") >= 1:
            return
        if reason := self.blocked(state):
            raise Replan(reason)
        radius = self.args["radius"]
        if state.count("diamond_hoe") < 1:
            if "diamond_hoe" in RECIPES:
                yield from craft_recipe(ex, state, "diamond_hoe", goal_qty=1)
            else:
                yield from _ensure_station(ex, state, "crafting_table", radius, skill_name=self.name)
                yield rec("craft", ex.craft("diamond_hoe", 1), item="diamond_hoe", count=1)
                refresh_inventory(ex, state)
                if state.count("diamond_hoe") < 1:
                    raise Replan("合成 diamond_hoe 后背包未确认产出")
        pos = yield from _ensure_station(ex, state, "smithing_table", radius, skill_name=self.name)
        res = yield from smith_once(ex, state, pos, template="netherite_upgrade_smithing_template", base="diamond_hoe", addition="netherite_ingot", expected_output="netherite_hoe")
        yield rec("netherite_hoe_evidence", res, output="netherite_hoe")

    def verify_done(self, state, before_counts=None) -> str:
        return "" if state.count("netherite_hoe") >= 1 else "背包未确认下界合金锄 netherite_hoe"


@register
class BrewPotion(Skill):
    name, goal, verified = "brew_potion", "brew:{recipe}", "offline"
    description = "在酿造台放入药水与原料酿造平凡药水、虚弱药水或虚弱喷溅药水"
    params = {"recipe": Param(str, "酿造配方", default="mundane", choices=("mundane", "weakness", "weakness_splash")), "radius": Param(int, "找酿造台半径", default=24, min=8, max=64)}
    _max_polls, _poll_sleep_s, _sleep_fn = 48, 0.5, staticmethod(time.sleep)
    _brew_result: Optional[dict] = None
    _primary_ingredient: Optional[str] = None

    @classmethod
    def blocked(cls, state) -> str:
        if state.count("potion") < 1:
            return "背包缺少水瓶或基础药水"
        if state.count("redstone") >= 1 or state.count("fermented_spider_eye") >= 1 or (_has_weakness_potion(state.inventory) and state.count("gunpowder") >= 1):
            return ""
        return "背包缺少红石粉、发酵蛛眼或可喷溅化火药"

    def can_start(self, state) -> bool:
        recipe = self.args["recipe"]
        if state.count("potion") < 1:
            return False
        if recipe == "weakness_splash":
            return state.count("gunpowder") >= 1 and (state.count("fermented_spider_eye") >= 1 or _has_weakness_potion(state.inventory))
        return state.count(_BREW_INGREDIENTS[recipe]) >= 1

    def run(self, ex, state):
        self._brew_result = self._primary_ingredient = None
        recipe, radius = self.args["recipe"], self.args["radius"]
        ing = _BREW_INGREDIENTS[recipe]
        refresh_inventory(ex, state)
        kw = dict(max_polls=self._max_polls, poll_sleep_s=self._poll_sleep_s, sleep_fn=self._sleep_fn)
        if recipe == "weakness_splash":
            already_weak = _has_weakness_potion(state.inventory)
            if state.count("potion") < 1 or state.count("gunpowder") < 1 or (not already_weak and state.count("fermented_spider_eye") < 1):
                raise Replan("缺少酿造 weakness_splash 所需的 potion、fermented_spider_eye 或 gunpowder")
            pos = yield from _ensure_station(ex, state, "brewing_stand", radius, skill_name=self.name, landmark="village")
            if not already_weak:
                yield from brew_batch(ex, state, pos, bottle_item="potion", ingredient="fermented_spider_eye", **kw)
                refresh_inventory(ex, state)
            if state.count("gunpowder") < 1 or state.count("potion") < 1:
                raise Replan("酿造虚弱喷溅药水缺少火药或虚弱药水")
            res = yield from brew_batch(ex, state, pos, bottle_item="potion", ingredient="gunpowder", **kw)
            refresh_inventory(ex, state)
            if bare(res.get("output")) != "splash_potion" and not _has_weakness_splash(state.inventory):
                raise Replan("酿造虚弱喷溅药水后未确认产出 splash_potion")
            self._brew_result, self._primary_ingredient = res, ("gunpowder" if already_weak else "fermented_spider_eye")
            yield rec("brew_potion_evidence", res, recipe=recipe, output=res["output"], potion=res.get("potion"))
            return
        if state.count("potion") < 1 or state.count(ing) < 1:
            raise Replan(f"缺少酿造 {recipe} 所需的 potion 或 {ing}")
        pos = yield from _ensure_station(ex, state, "brewing_stand", radius, skill_name=self.name, landmark="village")
        res = yield from brew_batch(ex, state, pos, bottle_item="potion", ingredient=ing, **kw)
        if recipe == "weakness":
            refresh_inventory(ex, state)
            if state.count("gunpowder") >= 1 and res["output"] == "potion" and state.count("potion") >= 1:
                res = yield from brew_batch(ex, state, pos, bottle_item="potion", ingredient="gunpowder", **kw)
        self._brew_result, self._primary_ingredient = res, ing
        yield rec("brew_potion_evidence", res, recipe=recipe, output=res["output"], potion=res.get("potion"))

    def verify_done(self, state, before_counts=None) -> str:
        res = self._brew_result
        if not isinstance(res, dict) or not res.get("output"):
            return "尚未完成药水酿造"
        out = bare(res["output"])
        if state.count(out) < 1:
            return f"酿造后背包未确认产出 {out}"
        if self.args["recipe"] == "weakness_splash" and not _has_weakness_splash(state.inventory):
            return "酿造后背包未确认虚弱喷溅药水（splash_potion）"
        ing = self._primary_ingredient
        if before_counts is not None and ing and ing in before_counts and state.count(ing) >= before_counts[ing]:
            return f"酿造后原料 {ing} 未确认消耗"
        return ""


@register
class CureZombieVillager(Skill):
    name, goal, verified = "cure_zombie_villager", "cure:zombie_villager", "offline"
    description = "向附近僵尸村民投掷虚弱喷溅药水并喂食金苹果进行治疗"
    params = {"radius": Param(int, "找僵尸村民半径", default=32, min=8, max=64)}
    _cured: bool = False
    _crafted_golden_apple: bool = False

    @classmethod
    def blocked(cls, state) -> str:
        if not _has_weakness_splash(state.inventory):
            return "背包缺少虚弱喷溅药水（需先 brew_potion(recipe='weakness_splash')）"
        return "" if (state.count("golden_apple") >= 1 or _can_craft_golden_apple(state)) else "背包缺少金苹果（或 8 个金锭与 1 个苹果）"

    def run(self, ex, state):
        self._cured, self._crafted_golden_apple, radius = False, False, self.args["radius"]
        refresh_inventory(ex, state)
        if reason := self.blocked(state):
            raise Replan(reason)
        if state.count("golden_apple") < 1:
            yield from _ensure_station(ex, state, "crafting_table", radius, skill_name=self.name, landmark="village")
            before_ga = state.count("golden_apple")
            yield rec("craft", ex.craft("golden_apple", 1), item="golden_apple", count=1)
            refresh_inventory(ex, state)
            if state.count("golden_apple") <= before_ga:
                raise Replan("合成 golden_apple 后背包未确认产出")
            self._crafted_golden_apple = True
        zv = (yield from _scan_sorted_entities_or_landmark(ex, state, ("zombie_villager",), radius, label="僵尸村民", pred="zombie_villager", wake_on="night", landmark="village"))[0]
        yield from _approach_entity(ex, state, zv, reach=ENTITY_EYE_REACH, max_distance=radius)
        splash_before = state.count("splash_potion")
        yield from use_item(ex, state, "splash_potion", target={"entity_id": zv["id"]}, verify=lambda _ev: state.count("splash_potion") < splash_before)
        apple_before = state.count("golden_apple")
        yield from use_item_on(ex, state, {"entity_id": zv["id"]}, "golden_apple", verify=lambda _ev: state.count("golden_apple") < apple_before)
        self._cured = True
        yield rec("cure_zombie_villager_evidence", None, entity_id=zv["id"], golden_apple_consumed=True, crafted_golden_apple=self._crafted_golden_apple)

    def verify_done(self, state, before_counts=None) -> str:
        if not self._cured:
            return "尚未完成僵尸村民治疗交互"
        if before_counts is not None:
            if not self._crafted_golden_apple and before_counts.get("golden_apple", 0) > 0 and state.count("golden_apple") >= before_counts["golden_apple"]:
                return "治疗后金苹果未确认消耗"
            if self._crafted_golden_apple and before_counts.get("gold_ingot", 0) >= 8 and state.count("gold_ingot") >= before_counts["gold_ingot"]:
                return "合成并治疗后金锭未确认消耗"
        return ""
