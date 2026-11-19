"""本轮可见的 Minecraft 实体交战和射界复核。计分以服务器进度为准。"""
from __future__ import annotations

import math
import time

from ..runtime import EnvBlocked, Replan
from .base import failure, rec
from .field import _bounded, _cell, _grounded, _observed_block, _position, _standable_cell, scan_entities
from .items import AIR, bare
from .observations import completed_advancements
from .sightline import eye_pos, voxel_line_cells


_ARROWS = ("arrow", "spectral_arrow", "tipped_arrow")
_AMMO = (*_ARROWS, "firework_rocket")


def _line_cells(start, end):
    """枚举直线经过的格子，包括两个端点；同一时刻跨多个格界时一并前进。"""
    return voxel_line_cells(start, end)


def _retreat_clear(ex, start, retreat_pos):
    if retreat_pos is None:
        return False
    start_cell, retreat = _cell(start), _cell(retreat_pos)
    if start_cell == retreat or start_cell[1] != retreat[1] or math.dist(retreat_pos, start) > 6:
        return False
    # 只复核同一高度的短退路。对角步同时查两侧，避免误把墙角视为可通行。
    start_center = (start_cell[0] + 0.5, start_cell[1], start_cell[2] + 0.5)
    end_center = (retreat[0] + 0.5, retreat[1], retreat[2] + 0.5)
    prev = start_cell
    for cell in _line_cells(start_center, end_center):
        if not _standable_cell(ex, cell):
            return False
        if prev[0] != cell[0] and prev[2] != cell[2]:
            if not (_standable_cell(ex, (prev[0], cell[1], cell[2]))
                    and _standable_cell(ex, (cell[0], cell[1], prev[2]))):
                return False
        prev = cell
    return True


def verify_shot_lane(ex, state, pos, *, retreat_pos, min_horizontal=0, max_distance=32):
    """§3.1、§3.4、§4.7：复核射距、直线无遮挡和同高退路。"""
    _bounded(min_horizontal, 0, 64, "最小水平射程")
    _bounded(max_distance, 1, 64, "最大射程")
    pos = _position(pos)
    st = state.refresh_status(ex.client, max_age_s=0)
    start = _position(st.get("pos"))
    # 眼高用于保守近似，服务端仍负责实体瞄准和弧线解算。
    eye = eye_pos(st)
    horizontal = math.hypot(pos[0] - eye[0], pos[2] - eye[2])
    distance = math.dist(eye, pos)
    evidence = {"distance": distance, "horizontal_dist": horizontal, "origin": list(start),
                "dimension": st.get("dimension"), "target": list(pos)}
    if horizontal < min_horizontal or distance > max_distance:
        return {**evidence, "status": "blocked", "reason": "射距超出配置范围"}
    if not _grounded(ex, st) or not _retreat_clear(ex, start, retreat_pos):
        return {**evidence, "status": "unknown", "reason": "站位支撑或近处水平退路未确认"}
    cells = list(_line_cells(eye, pos))
    for cell in cells[1:-1]:
        name = bare(_observed_block(ex, cell).get("name"))
        if not name:
            return {**evidence, "status": "unknown", "reason": f"射线格 {cell} 未观测"}
        if name not in AIR:
            return {**evidence, "status": "blocked", "reason": f"射线格 {cell} 出现 {name}"}
    after = state.refresh_status(ex.client, max_age_s=0)
    if (after.get("dimension") != evidence["dimension"]
            or math.dist(start, _position(after.get("pos"))) > 0.25
            or not _grounded(ex, after)):
        return {**evidence, "status": "unknown", "reason": "复核期间站位已变化"}
    return {**evidence, "status": "clear", "reason": ""}


def _current_entity(ex, state, entity, radius, *, required):
    if type(entity.get("id")) is not int or not (name := bare(entity.get("name"))):
        raise Replan("缺少当场观察的实体 id 或种类")
    scanned = scan_entities(ex, state, name, radius=radius)
    fresh = next((candidate for candidate in scanned["entities"]
                  if candidate["id"] == entity["id"]
                  and candidate.get("type") in ("hostile", "animal", "other")), None)
    if fresh is None and required:
        raise EnvBlocked(f"{radius} 格内未能重观测 {name}，需要新观测点",
                         missing_predicate=f"visible_entity:{name}",
                         searched_volume=f"半径 {radius} 格、该类型至多 40 个实体",
                         wake_on="chunk_moved")
    return fresh


def _completed(ex, advancement):
    return advancement is not None and bare(advancement) in completed_advancements(ex)


def _verify_advancement(ex, advancement, polls):
    if advancement is None:
        return False
    for step in range(polls):
        if step:
            yield rec("wait_advancement", None, advancement=advancement)
            time.sleep(0.5)
        if _completed(ex, advancement):
            return True
    raise Replan(f"动作已发出，进度 {advancement} 在观测窗口内未确认")


def combat_melee(ex, state, entity, *, retreat_pos, timeout_ms=8000, radius=8,
                 advancement=None, advancement_polls=3):
    """§3.2、§3.5、§3.7、§4.6：对观察到的单个实体近战，分别保存击杀与背包证据。"""
    _bounded(timeout_ms, 1, 20000, "交战超时")
    _bounded(radius, 1, 16, "近战观察半径")
    _bounded(advancement_polls, 1, 5, "进度读取次数")
    if _completed(ex, advancement):
        return {"receipt": None, "already_completed": True, "confirmed": True}
    target = _current_entity(ex, state, entity, radius, required=True)
    st = state.snapshot()
    if not _grounded(ex, st) or not _retreat_clear(ex, _position(st.get("pos")), retreat_pos):
        raise Replan("近战前未确认站位支撑及短退路")
    initial_inventory = state.refresh_inventory(ex.client)
    try:
        receipt = ex.attack(target["id"], timeout_ms=timeout_ms, stop_on=("hurt", "death"))
    except Replan as exc:
        state.refresh_status(ex.client, max_age_s=0)
        state.refresh_inventory(ex.client)
        code, extra = failure(exc)
        if code == "no_such_entity":
            raise Replan(
                f"近战目标实体 id={target['id']} 已脱离追踪范围或失效（no_such_entity），请重新 get_entities 选怪"
            ) from exc
        if code == "stalled":
            raise Replan(
                f"近战追击目标 id={target['id']} 寻路 8 秒无位移（stalled），停止追击"
            ) from exc
        if code == "interrupted":
            raise Replan(
                f"近战被中断（interrupted，cause={extra.get('cause')}）"
            ) from exc
        raise
    yield rec("attack", receipt, entity_id=target["id"])
    state.refresh_status(ex.client, max_age_s=0)
    after_inventory = state.refresh_inventory(ex.client)
    after = _current_entity(ex, state, target, radius, required=False)
    health = (after or {}).get("health")
    if receipt.get("killed") is not True or (type(health) in (int, float) and health > 0):
        interrupted_by = receipt.get("interrupted_by")
        if interrupted_by:
            raise Replan(f"本次近战被中断（interrupted_by={interrupted_by}），未确认击杀")
        raise Replan("本次近战未确认击杀")
    confirmed = yield from _verify_advancement(ex, advancement, advancement_polls)
    return {"receipt": receipt, "target_before": target, "target_after": after,
            "inventory_before": initial_inventory, "inventory_after": after_inventory,
            "confirmed": confirmed or health == 0}


def shoot_projectile(ex, state, *, entity=None, pos=None, weapon="bow", charge_ms=1100,
                     retreat_pos, min_horizontal=0, max_distance=32,
                     advancement=None, advancement_polls=3):
    """§3.2、§3.5、§3.7、§4.7：一次弓或弩射击，独立对账弹药差量与进度。"""
    if (entity is None) == (pos is None):
        raise Replan("请提供恰好一个已观察的目标实体或瞄准点")
    _bounded(charge_ms, 1, 10000, "远程武器蓄力时长")
    _bounded(max_distance, 1, 64, "最大射程")
    _bounded(advancement_polls, 1, 5, "进度读取次数")
    weapon = bare(weapon)
    if weapon not in ("bow", "crossbow"):
        raise Replan("该射击原语只支持弓和弩")
    if _completed(ex, advancement):
        return {"receipt": None, "already_completed": True, "confirmed": True}
    state.refresh_inventory(ex.client)
    if state.count(weapon) < 1:
        raise Replan(f"缺少射击武器 {weapon}")
    ammo = {item: state.count(item) for item in _AMMO}
    compatible = _ARROWS if weapon == "bow" else _AMMO
    if not any(ammo[item] > 0 for item in compatible):
        raise Replan(f"{weapon} 缺少可确认的弹药")
    target = None
    if entity is not None:
        target = _current_entity(ex, state, entity, max_distance, required=True)
        x, y, z = _position(target["pos"])
        aiming = (x, y + 0.5, z)
    else:
        aiming = _position(pos)
    lane = verify_shot_lane(ex, state, aiming, retreat_pos=retreat_pos,
                            min_horizontal=min_horizontal, max_distance=max_distance)
    if lane["status"] != "clear":
        raise Replan(lane["reason"])
    args = {"entity_id": target["id"]} if target else {"pos": list(aiming)}
    receipt = ex.shoot(**args, weapon=weapon, charge_ms=charge_ms, stop_on=("hurt", "death"))
    yield rec("shoot", receipt, weapon=weapon, **args)
    state.refresh_inventory(ex.client)
    spent = {item: count - state.count(item) for item, count in ammo.items() if count != state.count(item)}
    if receipt.get("fired") is not True:
        raise Replan("本次射击未获得 fired=true 的发射回执")
    confirmed = yield from _verify_advancement(ex, advancement, advancement_polls)
    return {"receipt": receipt, "lane": lane, "ammo_spent": spent, "confirmed": confirmed}
