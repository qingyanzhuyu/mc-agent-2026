"""P4 遭遇观察与撤离。阈值与静态视线需在靶场标定。"""
from __future__ import annotations

import math
import time
from collections.abc import Collection
from dataclasses import dataclass

from ..client import ApiError
from ..runtime import EnvBlocked, Replan, has_equipped_armor
from .base import failure, rec
from .combat_materials import carried_sword
from .encounter import _line_cells, _retreat_clear, verify_shot_lane
from .field import _cell, _grounded, _position, _safe_support, _standable_cell, move_local, scan_entities
from .items import bare
from .observations import completed_advancements as completed
from .sightline import eye_pos

KILL_MOB = "minecraft:adventure/kill_a_mob"
DEFLECT = "minecraft:story/deflect_arrow"
SHOOT_ARROW = "minecraft:adventure/shoot_arrow"
OL_BETSY = "minecraft:adventure/ol_betsy"
SKELETONS = frozenset(("skeleton", "parched"))
_NON_MELEE_HOSTILES = frozenset(("ghast", "phantom", "blaze", "ender_dragon", "guardian", "elder_guardian"))
_IRON_PLUS_ARMOR = frozenset(
    f"{tier}_{slot}"
    for tier in ("iron", "diamond", "netherite")
    for slot in ("helmet", "chestplate", "leggings", "boots")
)
_CARDINAL = ((1, 0), (-1, 0), (0, 1), (0, -1))
_SCAN_RADIUS = 20


class CrowdAmbush(Replan):
    """交战中探测到 2 只以上近战敌对生物贴脸，触发垫高柱/倒水防围殴。"""

    def __init__(self, message: str, *, crowd: list[dict], origin: dict):
        super().__init__(message)
        self.crowd = crowd
        self.origin = origin


@dataclass(frozen=True)
class Encounter:
    origin: dict
    retreat: tuple[float, float, float]
    target: dict | None
    aim: tuple[float, float, float]
    min_distance: int
    max_distance: int
    splash_radius: int
    open_field: bool = False


def gate_reason(state, status):
    if status.get("ready") is not True or not status.get("dimension") or status.get("alive") is not True:
        return "缺少就绪且存活的战斗状态"
    if state.in_safe_mode or status.get("on_ground") is not True or any(
            status.get(field) for field in (
                "in_lava", "in_water", "submerged", "on_fire", "in_vehicle", "is_sleeping", "jumping",
            )):
        return "需要在干燥、有支撑且未持续跳跃的位置，并脱离安全模式"
    health = status.get("health")
    if type(health) not in (int, float) or not math.isfinite(health) or health < 14:
        return "战斗前至少需要 14 点生命"
    return ""


def _safe_status(ex, state, previous=None, *, stay=False):
    status = state.refresh_status(ex.client, max_age_s=0)
    reason = gate_reason(state, status)
    if reason or not _grounded(ex, status):
        raise Replan(reason or "无法复核当前脚下支撑")
    if previous is not None and (
        status["dimension"] != previous["dimension"] or status["health"] < previous["health"]
        or (stay and _cell(status["pos"]) != _cell(previous["pos"]))
    ):
        raise Replan("交战期间受伤、换维度或离开已核对的站位，撤离重规划")
    return status


def _cover(ex, destination, danger):
    eye = eye_pos(destination)
    return any(_safe_support(ex, cell) for cell in list(_line_cells(eye, danger))[1:-1])


def has_open_field_kit(state) -> bool:
    """判断是否具备开阔地接战条件：剑、盾牌或铁+护甲、且生命 >= 14。"""
    inv = state.inventory or {}
    held = inv.get("held")
    held_name = bare(held.get("name") if isinstance(held, dict) else held)
    has_sword = carried_sword(state) is not None or held_name.endswith("_sword")
    offhand = inv.get("offhand")
    offhand_name = bare(offhand.get("name")) if isinstance(offhand, dict) else ""
    has_shield = state.count("shield") >= 1 or offhand_name == "shield"
    has_armor = has_equipped_armor(state) or any(state.count(a) >= 1 for a in _IRON_PLUS_ARMOR)
    hp = state.snapshot().get("health")
    hp_ok = type(hp) in (int, float) and math.isfinite(hp) and hp >= 14
    return bool(has_sword and (has_shield or has_armor) and hp_ok)


def _retreat(ex, origin, aim, *, require_cover: bool = True):
    origin_pos = origin["pos"] if isinstance(origin, dict) else origin
    sneaking = origin.get("sneaking") if isinstance(origin, dict) else None
    x, y, z = _cell(origin_pos)
    candidates = [(x + 0.5 + dx * stride, y, z + 0.5 + dz * stride)
                  for stride in (2, 3) for dx, dz in _CARDINAL]
    for dest in sorted(candidates, key=lambda p: math.dist(p, aim), reverse=True):
        dest_source = {"pos": dest, "sneaking": sneaking} if sneaking is not None else dest
        if (math.dist(dest, aim) >= math.dist(origin_pos, aim) and _standable_cell(ex, dest)
                and _retreat_clear(ex, origin_pos, dest)
                and (not require_cover or _cover(ex, dest_source, aim))):
            return dest
    return None


def _open_retreat(ex, origin, aim):
    return _retreat(ex, origin, aim, require_cover=False)


def _aim(entity):
    x, y, z = _position(entity.get("pos"))
    return (x, y + 0.5, z)


def _live_hostile(entity) -> bool:
    health = entity.get("health")
    return entity.get("type") == "hostile" and type(health) in (int, float) and health > 0


def _hostiles_near(entities, target_id, origin):
    return any(entity["id"] != target_id and entity.get("type") == "hostile"
               and math.dist(_position(entity["pos"]), origin) <= 6 for entity in entities)


def _hostiles_near_target(entities, target, radius: float = 6.0) -> int:
    tpos = _position(target["pos"])
    tid = target["id"]
    return sum(
        1 for entity in entities
        if entity["id"] != tid and _live_hostile(entity) and math.dist(_position(entity["pos"]), tpos) <= radius
    )


def _hostile_pack_size(entities, center, radius: float = 8.0) -> int:
    return sum(
        1 for entity in entities
        if _live_hostile(entity) and math.dist(_position(entity["pos"]), center) <= radius
    )


def crowd_melee_hostiles(entities, origin_pos, *, radius: float = 5.0) -> list[dict]:
    """返回玩家附近贴脸（<=radius 格、同层）的近战敌对生物列表，按距离升序。"""
    origin = _position(origin_pos)
    close = [
        entity for entity in entities
        if _live_hostile(entity)
        and bare(entity.get("name")) not in _NON_MELEE_HOSTILES
        and math.dist(_position(entity["pos"]), origin) <= radius
        and abs(_position(entity["pos"])[1] - origin[1]) <= 2.5
    ]
    close.sort(key=lambda entity: math.dist(_position(entity["pos"]), origin))
    return close


def _other_in_shot(entities, target_id, origin, aim, splash_radius):
    """§3.5 快照中的旁观者门槛。弹道由服务器解算，几何门槛需实测。"""
    eye = eye_pos(origin)
    delta = tuple(b - a for a, b in zip(eye, aim))
    length_sq = sum(v * v for v in delta)
    if length_sq <= 0:
        return True
    length = math.sqrt(length_sq)
    for entity in entities:
        if entity["id"] == target_id or entity.get("type") == "item":
            continue
        point = _aim(entity)
        if splash_radius and math.dist(point, aim) < splash_radius:
            return True
        projection = sum((p - a) * d for p, a, d in zip(point, eye, delta)) / length_sq
        if 0 < projection <= 1 + 2 / length:
            nearest = tuple(a + d * projection for a, d in zip(eye, delta))
            if math.dist(point, nearest) < 1.5:
                return True
    return False


def _clear_shot(ex, state, scene, entities):
    lane = verify_shot_lane(ex, state, scene.aim, retreat_pos=scene.retreat,
                           max_distance=scene.max_distance)
    target_id = scene.target["id"] if scene.target else None
    return lane["status"] == "clear" and not _other_in_shot(
        entities, target_id, scene.origin, scene.aim, scene.splash_radius)


def _observe(ex, state, previous=None):
    origin = _safe_status(ex, state, previous, stay=True)
    observed = scan_entities(ex, state, radius=_SCAN_RADIUS)
    if observed.get("ready") is False:
        raise Replan("世界区块或实体加载尚未就绪（not_ready），暂缓战斗选怪")
    echoed = state.snapshot()
    if (
        echoed.get("dimension") != origin["dimension"] or echoed.get("alive") is not True
        or echoed.get("health", -1) < origin["health"] or _cell(echoed["pos"]) != _cell(origin["pos"])
    ):
        raise Replan("实体复查时站位或生命已变化")
    return origin, observed["entities"]


def _no_entity(description, missing, wake_on="chunk_moved"):
    return EnvBlocked(f"未观察到{description}，先切换目标", missing_predicate=missing,
                      searched_volume=f"当前 {_SCAN_RADIUS} 格、至多 40 个实体", wake_on=wake_on)


def select_encounter(ex, state, predicate, *, description, missing, max_distance, aimed=False,
                     night_source=False, backstops=(), min_distance=2, splash_radius=0,
                     rank_key=None, target_isolation_radius=0.0, max_pack_size=0,
                     allow_open_field_mobs: Collection[str] = ()):
    origin, entities = _observe(ex, state)
    candidates = [entity for entity in entities if predicate(entity)]
    if rank_key is not None:
        candidates.sort(key=lambda entity: rank_key(entity, origin["pos"]))
    else:
        candidates.sort(key=lambda entity: math.dist(_position(entity["pos"]), origin["pos"]))
    scenes = [(entity, _aim(entity)) for entity in candidates[:6]]
    scenes.extend((None, pos) for pos in backstops)
    for target, aim in scenes:
        if not min_distance <= math.dist(origin["pos"], aim) <= max_distance:
            continue
        if target is None and not _backstop_present(ex, origin, aim):
            continue
        target_id = target["id"] if target else None
        if _hostiles_near(entities, target_id, origin["pos"]):
            continue
        if target is not None:
            if target_isolation_radius > 0 and _hostiles_near_target(entities, target, target_isolation_radius) > 0:
                continue
            if max_pack_size > 0 and _hostile_pack_size(entities, _position(target["pos"])) >= max_pack_size:
                continue
        retreat = _retreat(ex, origin, aim)
        open_field = False
        if retreat is None and target is not None and bare(target.get("name")) in allow_open_field_mobs:
            retreat = _open_retreat(ex, origin, aim)
            if retreat is not None:
                open_field = True
        if retreat is None:
            continue
        scene = Encounter(origin, retreat, target, aim, min_distance, max_distance, splash_radius,
                          open_field=open_field)
        if not aimed or _clear_shot(ex, state, scene, entities):
            return scene
    wake_on = "night" if not candidates and night_source and origin.get("is_day") else "chunk_moved"
    raise _no_entity(description, missing, wake_on)


def _backstop_present(ex, origin, aim):
    return _cell(aim)[1] == _cell(origin["pos"])[1] + 1 and _safe_support(ex, _cell(aim))


def refresh_encounter(ex, state, scene, *, aimed=False):
    origin, entities = _observe(ex, state, scene.origin)
    target = scene.target
    if target is not None:
        target = next((entity for entity in entities if entity["id"] == scene.target["id"]
                       and bare(entity.get("name")) == bare(scene.target.get("name"))
                       and type(entity.get("health")) in (int, float) and entity["health"] > 0), None)
        if target is None:
            raise Replan("交战目标已离开视野或死亡，停止动作")
    aim = _aim(target) if target is not None else scene.aim
    target_id = target["id"] if target else None
    if _hostiles_near(entities, target_id, origin["pos"]):
        crowd = crowd_melee_hostiles(entities, origin["pos"], radius=5.0)
        if len(crowd) >= 2:
            raise CrowdAmbush("交战期间被 2 只以上近战怪贴脸", crowd=crowd, origin=origin)
        raise Replan("交战距离、旁侧敌对或掩体退路已变化")
    if not scene.min_distance <= math.dist(origin["pos"], aim) <= scene.max_distance or (
        not _retreat_clear(ex, origin["pos"], scene.retreat)
        or (not scene.open_field and not _cover(ex, {"pos": scene.retreat, "sneaking": origin.get("sneaking")}, aim))
        or (target is None and not _backstop_present(ex, origin, aim))
    ):
        raise Replan("交战距离、旁侧敌对或掩体退路已变化")
    current = Encounter(origin, scene.retreat, target, aim, scene.min_distance,
                        scene.max_distance, scene.splash_radius, open_field=scene.open_field)
    if aimed and not _clear_shot(ex, state, current, entities):
        raise Replan("射界不再安全")
    return current


def check_after_action(ex, state, scene, *, stay=True):
    return _safe_status(ex, state, scene.origin, stay=stay)


def prepared_scene(ex, state, scene, choose, *, aimed=False):
    after = check_after_action(ex, state, scene, stay=False)
    if _cell(after["pos"]) != _cell(scene.origin["pos"]):
        return choose()
    return refresh_encounter(ex, state, scene, aimed=aimed)


def stop_activity(ex, state, *, busy=None, items=()):
    """§3.1、§4.15 只中止状态仍报告的同类追击或物品使用，返回 /stop 的锁释放回执。"""
    try:
        status = state.refresh_status(ex.client, max_age_s=0)
    except (ApiError, Replan):
        return None
    using = status.get("using_item")
    if ((busy is not None and status.get("busy") == busy)
            or (isinstance(using, dict) and bare(using.get("name")) in items)):
        try:
            return ex.client.stop()
        except Exception:
            return None
    return None


def retreat_after_failure(ex, state, scene):
    """同维度仍可行动且旧退路可重证时撤进掩体，抢占期间交给 Safety。"""
    try:
        current = state.refresh_status(ex.client, max_age_s=0)
        if (
            state.in_safe_mode or current.get("ready") is not True or not current.get("alive")
            or current.get("dimension") != scene.origin["dimension"]
            or not _grounded(ex, current) or not _retreat_clear(ex, current["pos"], scene.retreat)
            or (not scene.open_field
                and not _cover(ex, {"pos": scene.retreat, "sneaking": current.get("sneaking")}, scene.aim))
        ):
            return
        after = yield from move_local(ex, state, scene.retreat, max_distance=6, require_move=True)
        if not scene.open_field and not _cover(ex, after, scene.aim):
            yield rec("combat_retreat_unconfirmed", None, reason="移动后未重新见证掩体")
    except (ApiError, Replan) as exc:
        yield rec("combat_retreat_unconfirmed", None, reason=str(exc))


def interrupted(ex, state, scene, error):
    # 失败的阻塞动作也占一拍，先留给 Safety；若因主动 /stop、死亡或断线/未就绪中断，则不再补发撤离移动。
    code, extra = failure(error)
    cause = extra.get("cause") or extra.get("interrupted_by")
    if cause is None and "interrupted_by=stop" in str(error):
        cause = "stop"
    meta = {
        key: value
        for key, value in (("error_code", code), ("cause", cause), ("inner_code", extra.get("inner_code")))
        if value is not None
    }
    yield rec("combat_interrupted", None, reason=str(error), **meta)
    if cause in ("stop", "death") or code in ("dead", "not_connected", "not_ready"):
        return
    yield from retreat_after_failure(ex, state, scene)


def confirm_progress(ex, progress_ids, required, *, initial=(), await_all=False, check=None):
    """各 ID 独立取证。失败也保留已观察到的进度。"""
    seen = set(initial)
    for attempt in range(3):
        if attempt:
            if check is not None:
                check()
            time.sleep(0.1)
        seen.update(completed(ex))
        evidence = {key: bare(key) in seen for key in progress_ids}
        yield rec("combat_progress", evidence)
        if evidence[required] and (not await_all or all(evidence.values()) or attempt == 2):
            return evidence
    raise Replan(f"动作已结束，未在轮询预算内看到所需进度 {required}")
