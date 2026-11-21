"""夜间复用或建造安全床位并验证睡眠。"""
from __future__ import annotations

from dataclasses import dataclass
import math

from ..runtime import EnvBlocked, Replan
from .base import Skill, failure, rec
from .field import collect_drops, move_local, place_block, scan_blocks, scan_entities
from .interaction import use_item_on
from .items import AIR, bare
from .recipes import RECIPES, _ingredient_items, craft_recipe
from .registry import register
from .sightline import ENTITY_EYE_REACH, block_center
from .survival import (
    _PLANKS, _SLEEP_PROGRESS, _SUPPORT, _above, _approach, _block, _can_reach, _cell,
    _fresh_inventory, _has_progress, _neighbors, _on_ground, _plank_plan,
    _site_missing, _stand_for, _standable,
)
from .world_memo import memo_for


_BEDS = tuple(item for item in RECIPES if item.endswith("_bed"))
_COLORS = tuple(item.removesuffix("_bed") for item in _BEDS)
_WOOLS = tuple(f"{color}_wool" for color in _COLORS)
_DYE_SOURCES: dict[str, tuple[tuple[str, int], ...]] = {
    "white_dye": (("bone_meal", 1), ("lily_of_the_valley", 1)), "blue_dye": (("lapis_lazuli", 1), ("cornflower", 1)),
    "black_dye": (("ink_sac", 1), ("wither_rose", 1)), "brown_dye": (("cocoa_beans", 1),),
    "yellow_dye": (("dandelion", 1), ("sunflower", 2)), "light_blue_dye": (("blue_orchid", 1),),
    "red_dye": (("poppy", 1), ("red_tulip", 1), ("beetroot", 1), ("rose_bush", 2)), "gray_dye": (("closed_eyeblossom", 1),),
    "orange_dye": (("orange_tulip", 1), ("open_eyeblossom", 1), ("torchflower", 1)), "magenta_dye": (("allium", 1), ("lilac", 2)),
    "pink_dye": (("pink_tulip", 1), ("pink_petals", 1), ("peony", 2), ("cactus_flower", 1)), "cyan_dye": (("pitcher_plant", 2),),
    "light_gray_dye": (("azure_bluet", 1), ("oxeye_daisy", 1), ("white_tulip", 1)),
}
_BED_RADIUS, _EARLY_NIGHT_START, _NIGHT_END = 12, 12542, 23459


def _can_sleep_now(status):
    """§4.4 / §8 1.3.4：原版早夜 12542 起（早于 is_day 翻转的 13000）及雷暴均可入睡并返回 slept: true。"""
    if status.get("thundering") is True:
        return True
    tod = status.get("time_of_day")
    if isinstance(tod, (int, float)) and not isinstance(tod, bool):
        if _EARLY_NIGHT_START <= int(tod) % 24000 <= _NIGHT_END:
            return True
        return True if status.get("is_day") is False else False
    return (not status["is_day"]) if isinstance(status.get("is_day"), bool) else None


def _sleep_window(status):
    if status.get("ready") is not True or status.get("alive") is not True:
        raise Replan("世界加载尚未就绪或角色未存活（not_ready），请等 ready=true 后再睡")
    if bare(status.get("dimension")) != "overworld":
        raise Replan("只有确认在主世界才能点床睡觉，请先返回主世界")
    can_sleep = _can_sleep_now(status)
    if can_sleep is False:
        raise EnvBlocked("现在是白天，等到夜晚再尝试睡觉", missing_predicate="sleep_at_night",
                         searched_volume="当前主世界 /status 昼夜快照", wake_on="night")
    if can_sleep is None:
        raise Replan("缺少当前主世界的昼夜信息，请刷新状态")
    if not _on_ground(status):
        raise Replan("睡前未确认安全站立，先离开液体或恢复安全站位")
    danger = status.get("nearest_hostile")
    if danger is not None and (not isinstance(danger.get("dist"), (int, float)) or danger["dist"] <= 12):
        raise Replan("床边附近有敌对生物，请让安全层处理后再睡")


def _fresh_sleep_status(ex, state):
    status = state.refresh_status(ex.client, max_age_s=0)
    _sleep_window(status)
    return status


def _held_bed(state): return next((item for item in _BEDS if state.count(item)), None)
def _wool_bed(state): return next((b for b, w in zip(_BEDS, _WOOLS) if state.count(w) >= 3), None)
def _partial_wool(state): wool = max(_WOOLS, key=state.count); return wool if state.count(wool) else None
def _sheared_bed(state, wool): return _wool_bed(state) if wool is None else (wool.removesuffix("_wool") + "_bed" if state.count(wool) >= 3 else None)


def _known_bed_in_memo(state) -> bool:
    memo = memo_for(state)
    if memo.has_station("bed", dimension="overworld"):
        return True
    camp = memo.get_camp(dimension="overworld")
    return isinstance(camp, dict) and camp.get("bed") is not None


def _record_bed_memo(state, pos, bed) -> None:
    memo = memo_for(state)
    memo.record_station(bed, pos, dimension="overworld")
    camp = memo.get_camp(dimension="overworld")
    if isinstance(camp, dict) and camp.get("bed") is None and camp.get("pos") is not None and math.dist(camp["pos"], pos) <= 16:
        memo.set_camp(camp["pos"], dimension="overworld", bed=pos)


def _craft_planks_from_log(ex, state):
    for plank in _PLANKS:
        recipe = RECIPES[plank]
        if all(sum(state.count(n) for n in _ingredient_items(ing)) >= need for ing, need in recipe.ingredients):
            yield from craft_recipe(ex, state, plank, goal_qty=state.count(plank) + recipe.output)
            if _plank_plan(state, 3):
                return


@dataclass(frozen=True)
class _WoolDyePlan:
    """混色羊毛染色凑床方案。"""

    color: str
    target_wool: str
    target_bed: str
    dye_item: str
    dyes_needed: int
    direct_dyes: int
    dye_source_crafts: tuple[tuple[str, int], ...]
    reserved_items: tuple[tuple[str, int, str], ...]


def plan_wool_dyeing(state) -> _WoolDyePlan | None:
    """当持有至少 3 块混色羊毛时，规划消耗染料与原材料的最小染色方案。"""
    if _wool_bed(state) is not None:
        return None
    wool_counts = {w: state.count(w) for w in _WOOLS if state.count(w) > 0}
    if sum(wool_counts.values()) < 3:
        return None

    best_key: tuple[int, int, int, int] | None = None
    best_plan: _WoolDyePlan | None = None
    for idx, c in enumerate(_COLORS):
        target_wool, dye_item = f"{c}_wool", f"{c}_dye"
        owned_target = state.count(target_wool)
        dyes_needed = 3 - owned_target
        direct = min(dyes_needed, state.count(dye_item))
        shortfall, source_crafts = dyes_needed - direct, []
        for src_item, yield_qty in _DYE_SOURCES.get(dye_item, ()):
            if shortfall <= 0:
                break
            avail = state.count(src_item)
            batches = min(avail, math.ceil(shortfall / yield_qty)) if avail > 0 else 0
            if batches > 0:
                source_crafts.append((src_item, batches))
                shortfall = max(0, shortfall - batches * yield_qty)
        if shortfall > 0:
            continue

        reserved = [(target_wool, owned_target, "consume")] if owned_target > 0 else []
        donor_left = dyes_needed
        for wool, qty in wool_counts.items():
            if wool != target_wool and donor_left > 0:
                take = min(donor_left, qty)
                reserved.append((wool, take, "consume"))
                donor_left -= take
        if direct > 0:
            reserved.append((dye_item, direct, "consume"))
        reserved.extend((s, b, "consume") for s, b in source_crafts)

        rank = (dyes_needed, len(source_crafts), sum(b for _, b in source_crafts), idx)
        if best_key is None or rank < best_key:
            best_key, best_plan = rank, _WoolDyePlan(c, target_wool, f"{c}_bed", dye_item, dyes_needed, direct, tuple(source_crafts), tuple(reserved))
    return best_plan


def _craft_until(ex, state, item: str, target_qty: int, max_steps: int, fail_label: str):
    """逐次手搓合成直到持有量达到 target_qty，严格遵守 1 阻塞动作 = 1 yield。"""
    for _ in range(max_steps):
        if state.count(item) >= target_qty:
            break
        before = state.count(item)
        yield rec("craft", ex.craft(item, 1), item=item, count=1)
        _fresh_inventory(ex, state)
        if state.count(item) <= before:
            raise Replan(f"{fail_label} {item} 后背包未确认增加")


def _dye_mixed_wool_for_bed(ex, state):
    """按最优染色方案补齐染料并将混色羊毛逐一染成同色，返回目标床名。"""
    plan = plan_wool_dyeing(state)
    if plan is None:
        return None
    yield from _craft_until(ex, state, plan.dye_item, plan.dyes_needed, plan.dyes_needed, "合成染料")
    yield from _craft_until(ex, state, plan.target_wool, 3, plan.dyes_needed, "染色羊毛")
    if state.count(plan.target_wool) < 3:
        raise Replan(f"染色后仍未凑齐 3 块 {plan.target_wool}")
    return plan.target_bed


def _bed_half_supported(ex, pos):
    return _block(ex, _above(pos, -1)) in _SUPPORT and _block(ex, _above(pos)) in AIR and _block(ex, _above(pos, 2)) in AIR


def _bed_observed(ex, pos, bed):
    return _block(ex, pos) == bed and _bed_half_supported(ex, pos) and any(_block(ex, m) == bed and _bed_half_supported(ex, m) for m in _neighbors(pos))


def _existing_bed(ex, state):
    candidates = scan_blocks(ex, state, _BEDS, radius=_BED_RADIUS, limit=8)["blocks"]
    status = state.refresh_status(ex.client, max_age_s=0)
    for row in candidates:
        pos = tuple(row["pos"])
        if _bed_observed(ex, pos, row["name"]) and _stand_for(ex, status, block_center(pos), block_pos=pos) is not None:
            return pos, row["name"]
    return None


def _bed_placement(ex, status):
    """§4.3 无朝向参数，预先验证四种可能延伸方向。"""
    for pos in _neighbors(_cell(status["pos"]), radius=2):
        if all(_standable(ex, cell) and _block(ex, _above(cell, 2)) in AIR for cell in (pos, *_neighbors(pos))):
            return pos
    raise _site_missing("未观察到可安全容纳两格床的平面，请换平坦位置", "supported_bed_space", 3, 4)


def _shear_for_wool(ex, state, reserved_wool):
    """§4.4 支持剪毛，羊颜色和掉落时序用库存及实体复读验证。"""
    for animal in scan_entities(ex, state, "sheep", radius=8, limit=3)["entities"]:
        if animal.get("baby") is True or animal.get("is_baby") is True or animal.get("sheared") is True:
            continue
        status = _fresh_sleep_status(ex, state)
        stand = _stand_for(ex, status, animal["pos"])
        if stand is None:
            continue
        fresh = yield from _approach(ex, state, status, stand, animal["pos"])
        cur = next((r for r in scan_entities(ex, state, "sheep", radius=8)["entities"] if r["id"] == animal["id"]), None)
        if cur is None or not _can_reach(fresh, cur["pos"], ENTITY_EYE_REACH):
            continue
        evidence = yield from use_item_on(ex, state, animal["id"], "shears")
        _fresh_inventory(ex, state)
        if bed := _sheared_bed(state, reserved_wool):
            return bed
        drops = evidence["receipt"].get("gained") or {}
        if isinstance(drops, dict):
            for item in ((reserved_wool,) if reserved_wool else _WOOLS):
                if drops.get(item, 0) or drops.get(f"minecraft:{item}", 0):
                    try:
                        yield from collect_drops(ex, state, item, count=1, max_targets=2)
                    except EnvBlocked:
                        pass
                    if bed := _sheared_bed(state, reserved_wool):
                        return bed
    return None


def wake_from_bed(ex, state, pos):
    """§4.4 / §8 1.3.4：睡着时再点床触发起床，woke=true 表示服务端已确认下床，2s 未下床报 cannot_sleep。"""
    try:
        evidence = yield from use_item_on(ex, state, pos)
    except Replan as exc:
        if failure(exc)[0] == "cannot_sleep":
            raise Replan("点击床起床失败：2 秒内服务端未确认下床（cannot_sleep）") from exc
        raise
    if evidence["receipt"].get("woke") is not True or evidence["after"]["status"].get("is_sleeping") is True:
        raise Replan("点床起床后未确认 woke=true 且脱离 is_sleeping 状态")
    return evidence


def _click_bed(ex, state, pos):
    try:
        return (yield from use_item_on(ex, state, pos))
    except Replan as exc:
        if failure(exc)[0] == "cannot_sleep":
            raise Replan("点床或下床未被服务端确认（cannot_sleep），请检查周围敌对或床位状态") from exc
        raise


def _since_seq(ex, state):
    snap = ex.client.events(since=state.last_seq, limit=30)
    return None if snap.get("has_more") else snap.get("last_seq", state.last_seq)


def _sleep_confirmed(ex, evidence, since):
    receipt, after = evidence["receipt"], evidence["after"]["status"]
    if bare(after.get("dimension")) != "overworld" or after.get("alive") is not True:
        raise Replan("点床后未确认角色仍活着且处于主世界")
    if receipt.get("slept") is False:
        if receipt.get("daytime") is True and _can_sleep_now(after) is False:
            raise EnvBlocked("现在是白天（daytime=true），点床仅绑定重生点未入睡",
                             missing_predicate="sleep_at_night", searched_volume="当前主世界 /status 昼夜快照", wake_on="night")
        raise Replan("点床返回 slept=false，服务端拒绝入睡，请检查昼夜时间或床边环境")
    if _has_progress(ex, _SLEEP_PROGRESS):
        return {"advancement_confirmed": True}
    if after.get("is_sleeping") is True:
        return {"observed_sleeping": True, "advancement_confirmed": False}
    if since is not None and receipt.get("slept") is True:
        after_events = ex.client.events(since=since, limit=30).get("events") or ()
        if any(e.get("type") == "sleep" and e.get("seq", -1) > since for e in after_events):
            return {"observed_sleep_event": True, "advancement_confirmed": False}
    raise Replan("点床回执后未复核到睡眠状态、事件或睡眠进度")


@register
class SleepInBed(Skill):
    name = "sleep_in_bed"
    goal = "goal:sleep_in_bed"
    description = "夜间优先复用安全的床并确认实际睡眠"
    verified = "offline"

    @classmethod
    def blocked(cls, state):
        status = state.snapshot()
        if not (status.get("ready") is True and bare(status.get("dimension")) == "overworld"):
            return "缺少已就绪的主世界状态"
        if status.get("is_sleeping") is True:
            return ""
        if _held_bed(state) or _wool_bed(state) or plan_wool_dyeing(state) or state.count("shears") >= 1 or _known_bed_in_memo(state):
            return ""
        return "既无床、又无 3 块同色/可染羊毛、又无剪刀，且记忆中无已知床位"

    @classmethod
    def material_plan(cls, args: dict, state) -> tuple[tuple[str, int, str], ...]:
        if state.snapshot().get("is_sleeping") is True:
            return ()
        if bed := _held_bed(state):
            return ((bed, 1, "consume"),)
        if not (planks := _plank_plan(state, 3)):
            return ()
        if bed := _wool_bed(state):
            return ((bed.removesuffix("_bed") + "_wool", 3, "consume"), *planks)
        if dye_plan := plan_wool_dyeing(state):
            return (*dye_plan.reserved_items, *planks)
        if state.count("shears"):
            wool = _partial_wool(state)
            return (("shears", 1, "borrow"), *(((wool, state.count(wool), "consume"),) if wool else ()), *planks)
        return ()

    def run(self, ex, state):
        if _has_progress(ex, _SLEEP_PROGRESS):
            return
        status = state.refresh_status(ex.client, max_age_s=0)
        if bare(status.get("dimension")) == "overworld" and status.get("is_sleeping") is True:
            return
        _sleep_window(status)
        existing = _existing_bed(ex, state)
        if existing is None and (camp := memo_for(state).get_camp(dimension="overworld")) and camp.get("bed") and status.get("pos"):
            camp_bed = tuple(camp["bed"])
            if math.dist(status["pos"], camp_bed) <= 48:
                if math.dist(status["pos"], camp_bed) > 6:
                    try:
                        yield from move_local(ex, state, camp_bed, max_distance=48, require_move=False)
                    except Replan:
                        pass
                existing = _existing_bed(ex, state)
        if existing:
            pos, bed = existing
        else:
            _fresh_inventory(ex, state)
            bed = _held_bed(state)
            if bed is None:
                if not _plank_plan(state, 3):
                    yield from _craft_planks_from_log(ex, state)
                    if not _plank_plan(state, 3):
                        raise Replan("自制床缺 3 块木板，请补料或寻找可复用床")
                bed = _wool_bed(state)
                if bed is None and plan_wool_dyeing(state) is not None:
                    bed = yield from _dye_mixed_wool_for_bed(ex, state)
                if bed is None and state.count("shears"):
                    bed = yield from _shear_for_wool(ex, state, _partial_wool(state))
                if bed is None:
                    raise Replan("自制床需要 3 块同色羊毛，请剪同色羊或补齐同色羊毛")
                _bed_placement(ex, _fresh_sleep_status(ex, state))
                yield from craft_recipe(ex, state, bed, goal_qty=1)
            pos = _bed_placement(ex, _fresh_sleep_status(ex, state))
            before = state.count(bed)
            yield from place_block(ex, state, bed, pos)
            _fresh_inventory(ex, state)
            if before - state.count(bed) != 1:
                raise Replan("摆床后未确认消耗一张床")
        _record_bed_memo(state, pos, bed)
        status = _fresh_sleep_status(ex, state)
        bed_target = block_center(pos)
        stand = _stand_for(ex, status, bed_target, block_pos=pos)
        if stand is None:
            raise _site_missing("未复核到床边可达安全站位", "reachable_bed", _BED_RADIUS, 8)
        fresh = yield from _approach(ex, state, status, stand, bed_target, block_pos=pos)
        _sleep_window(fresh)
        if not _bed_observed(ex, pos, bed):
            raise Replan("点床前未确认两格床体、承重与上方净空")
        if fresh.get("is_sleeping") is True:
            return
        since = _since_seq(ex, state)
        evidence = yield from _click_bed(ex, state, pos)
        if evidence["receipt"].get("woke") is True:
            after_wake = evidence["after"]["status"]
            if after_wake.get("is_sleeping") is True:
                raise Replan("点床返回 woke=true，但角色状态仍为睡眠中")
            if _has_progress(ex, _SLEEP_PROGRESS):
                yield rec("verify_sleep", {"advancement_confirmed": True, "woke": True})
                return
            _sleep_window(after_wake)
            if not _bed_observed(ex, pos, bed):
                raise Replan("下床后复读未确认两格床体、承重与上方净空")
            since = _since_seq(ex, state)
            evidence = yield from _click_bed(ex, state, pos)
        yield rec("verify_sleep", _sleep_confirmed(ex, evidence, since))
