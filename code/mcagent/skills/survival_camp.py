"""夜间复用或建造安全床位并验证睡眠。"""
from __future__ import annotations

from ..runtime import EnvBlocked, Replan
from .base import Skill, failure, rec
from .field import collect_drops, place_block, scan_blocks, scan_entities
from .interaction import use_item_on
from .items import AIR, bare
from .recipes import RECIPES, craft_recipe
from .registry import register
from .sightline import ENTITY_EYE_REACH, block_center
from .survival import (
    _SLEEP_PROGRESS, _SUPPORT, _above, _approach, _block, _can_reach, _cell,
    _fresh_inventory, _has_progress, _neighbors, _on_ground, _plank_plan,
    _site_missing, _stand_for, _standable,
)


_BEDS = tuple(item for item in RECIPES if item.endswith("_bed"))
_WOOLS = tuple(item.removesuffix("_bed") + "_wool" for item in _BEDS)
_BED_RADIUS = 12
_EARLY_NIGHT_START = 12542
_NIGHT_END = 23459


def _can_sleep_now(status):
    """§4.4 / §8 1.3.4：原版早夜 12542 起（早于 is_day 翻转的 13000）及雷暴均可入睡并返回 slept: true。"""
    if status.get("thundering") is True:
        return True
    tod = status.get("time_of_day")
    if isinstance(tod, (int, float)) and not isinstance(tod, bool):
        if _EARLY_NIGHT_START <= int(tod) % 24000 <= _NIGHT_END:
            return True
        if status.get("is_day") is False:
            return True
        return False
    if status.get("is_day") is False:
        return True
    if status.get("is_day") is True:
        return False
    return None


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


def _held_bed(state):
    return next((item for item in _BEDS if state.count(item)), None)


def _wool_bed(state):
    return next((bed for bed, wool in zip(_BEDS, _WOOLS) if state.count(wool) >= 3), None)


def _partial_wool(state):
    wool = max(_WOOLS, key=state.count)
    return wool if state.count(wool) else None


def _sheared_bed(state, wool):
    if wool is None:
        return _wool_bed(state)
    return wool.removesuffix("_wool") + "_bed" if state.count(wool) >= 3 else None


def _bed_half_supported(ex, pos):
    return (_block(ex, _above(pos, -1)) in _SUPPORT
            and _block(ex, _above(pos)) in AIR and _block(ex, _above(pos, 2)) in AIR)


def _bed_observed(ex, pos, bed):
    return (_block(ex, pos) == bed and _bed_half_supported(ex, pos)
            and any(_block(ex, mate) == bed and _bed_half_supported(ex, mate) for mate in _neighbors(pos)))


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
    origin = _cell(status["pos"])
    for pos in _neighbors(origin, radius=2):
        if all(_standable(ex, cell) and _block(ex, _above(cell, 2)) in AIR
               for cell in (pos, *_neighbors(pos))):
            return pos
    raise _site_missing("未观察到可安全容纳两格床的平面，请换平坦位置",
                        "supported_bed_space", 3, 4)


def _shear_for_wool(ex, state, reserved_wool):
    """§4.4 支持剪毛，羊颜色和掉落时序用库存及实体复读验证。"""
    sheep = scan_entities(ex, state, "sheep", radius=8, limit=3)["entities"]
    for animal in sheep:
        if animal.get("baby") is True or animal.get("is_baby") is True or animal.get("sheared") is True:
            continue
        status = state.refresh_status(ex.client, max_age_s=0)
        _sleep_window(status)
        stand = _stand_for(ex, status, animal["pos"])
        if stand is None:
            continue
        fresh = yield from _approach(ex, state, status, stand, animal["pos"])
        current = next((row for row in scan_entities(ex, state, "sheep", radius=8)["entities"]
                        if row["id"] == animal["id"]), None)
        if current is None or not _can_reach(fresh, current["pos"], ENTITY_EYE_REACH):
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
    after = evidence["after"]["status"]
    if evidence["receipt"].get("woke") is not True or after.get("is_sleeping") is True:
        raise Replan("点床起床后未确认 woke=true 且脱离 is_sleeping 状态")
    return evidence


def _click_bed(ex, state, pos):
    try:
        return (yield from use_item_on(ex, state, pos))
    except Replan as exc:
        if failure(exc)[0] == "cannot_sleep":
            raise Replan("点床或下床未被服务端确认（cannot_sleep），请检查周围敌对或床位状态") from exc
        raise


def _sleep_confirmed(ex, evidence, since):
    receipt = evidence["receipt"]
    after = evidence["after"]["status"]
    if bare(after.get("dimension")) != "overworld" or after.get("alive") is not True:
        raise Replan("点床后未确认角色仍活着且处于主世界")
    if receipt.get("slept") is False:
        if receipt.get("daytime") is True and _can_sleep_now(after) is False:
            raise EnvBlocked(
                "现在是白天（daytime=true），点床仅绑定重生点未入睡",
                missing_predicate="sleep_at_night",
                searched_volume="当前主世界 /status 昼夜快照",
                wake_on="night",
            )
        raise Replan("点床返回 slept=false，服务端拒绝入睡，请检查昼夜时间或床边环境")
    if _has_progress(ex, _SLEEP_PROGRESS):
        return {"advancement_confirmed": True}
    if after.get("is_sleeping") is True:
        return {"observed_sleeping": True, "advancement_confirmed": False}
    if since is not None and receipt.get("slept") is True:
        after_events = ex.client.events(since=since, limit=30).get("events") or ()
        if any(event.get("type") == "sleep" and event.get("seq", -1) > since for event in after_events):
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
        if status.get("ready") is True and bare(status.get("dimension")) == "overworld":
            return ""
        return "缺少已就绪的主世界状态"

    @classmethod
    def material_plan(cls, args: dict, state) -> tuple[tuple[str, int, str], ...]:
        if state.snapshot().get("is_sleeping") is True:
            return ()
        if bed := _held_bed(state):
            return ((bed, 1, "consume"),)
        planks = _plank_plan(state, 3)
        if not planks:
            return ()
        if bed := _wool_bed(state):
            return ((bed.removesuffix("_bed") + "_wool", 3, "consume"), *planks)
        if state.count("shears"):
            wool = _partial_wool(state)
            held_wool = ((wool, state.count(wool), "consume"),) if wool else ()
            return (("shears", 1, "borrow"), *held_wool, *planks)
        return ()

    def run(self, ex, state):
        if _has_progress(ex, _SLEEP_PROGRESS):
            return
        status = state.refresh_status(ex.client, max_age_s=0)
        if bare(status.get("dimension")) == "overworld" and status.get("is_sleeping") is True:
            return
        _sleep_window(status)
        existing = _existing_bed(ex, state)
        if existing:
            pos, bed = existing
        else:
            _fresh_inventory(ex, state)
            bed = _held_bed(state)
            if bed is None:
                if not _plank_plan(state, 3):
                    raise Replan("自制床缺 3 块木板，请补料或寻找可复用床")
                bed = _wool_bed(state)
                if bed is None and state.count("shears"):
                    bed = yield from _shear_for_wool(ex, state, _partial_wool(state))
                if bed is None:
                    raise Replan("自制床需要 3 块同色羊毛，请剪同色羊或补齐同色羊毛")
                status = state.refresh_status(ex.client, max_age_s=0)
                _sleep_window(status)
                _bed_placement(ex, status)
                yield from craft_recipe(ex, state, bed, goal_qty=1)
            status = state.refresh_status(ex.client, max_age_s=0)
            _sleep_window(status)
            pos = _bed_placement(ex, status)
            before = state.count(bed)
            yield from place_block(ex, state, bed, pos)
            _fresh_inventory(ex, state)
            if before - state.count(bed) != 1:
                raise Replan("摆床后未确认消耗一张床")
        status = state.refresh_status(ex.client, max_age_s=0)
        _sleep_window(status)
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
        event_snapshot = ex.client.events(since=state.last_seq, limit=30)
        since = None if event_snapshot.get("has_more") else event_snapshot.get("last_seq", state.last_seq)
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
            event_snapshot = ex.client.events(since=state.last_seq, limit=30)
            since = None if event_snapshot.get("has_more") else event_snapshot.get("last_seq", state.last_seq)
            evidence = yield from _click_bed(ex, state, pos)
        yield rec("verify_sleep", _sleep_confirmed(ex, evidence, since))
