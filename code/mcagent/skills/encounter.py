"""本轮可见的 Minecraft 实体交战和射界复核。计分以服务器进度为准。"""
from __future__ import annotations

import math
import time

from ..client import ApiError
from ..runtime import EnvBlocked, Replan
from .base import failure, rec
from .field import _bounded, _cell, _grounded, _observed_block, _position, _standable_cell, scan_entities
from .items import AIR, bare, pick_placeable, total_placeable
from .observations import completed_advancements
from .sightline import eye_pos, voxel_line_cells


_ARROWS = ("arrow", "spectral_arrow", "tipped_arrow")
_AMMO = (*_ARROWS, "firework_rocket")
MELEE_TACTICS = frozenset({"auto", "kite", "bunker_hit", "direct"})
_KITE_HOSTILES = frozenset({"creeper"})
_TALL_HOSTILES = frozenset({"wither_skeleton", "enderman", "iron_golem"})
_LOW_HOSTILES = frozenset({"spider", "cave_spider", "silverfish", "endermite", "slime", "magma_cube"})
_BUNKER_REACH = 3.5


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


def verify_shot_lane(ex, state, pos, *, retreat_pos, min_horizontal=0, max_distance=32,
                     elevated_ok=False):
    """§3.1、§3.4、§4.7：复核射距、直线无遮挡和同高退路（高柱位可由 elevated_ok 豁免同高退路）。"""
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
    if not _grounded(ex, st) or (
        (not elevated_ok or retreat_pos is not None) and not _retreat_clear(ex, start, retreat_pos)
    ):
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
            time.sleep(0.15)
        if _completed(ex, advancement):
            return True
    raise Replan(f"动作已发出，进度 {advancement} 在观测窗口内未确认")


def _has_offhand_shield(state, status) -> bool:
    for source in (status, getattr(state, "inventory", None)):
        if isinstance(source, dict) and bare((source.get("offhand") or {}).get("name")) == "shield":
            return True
    return False


def _target_aim_point(target) -> tuple[float, float, float]:
    tx, ty, tz = _position(target["pos"])
    name = bare(target.get("name"))
    offset = 0.55 if name in _LOW_HOSTILES else (1.2 if name in _TALL_HOSTILES else 0.9)
    return (tx, ty + offset, tz)


def _aim_angles(eye, point) -> tuple[float, float]:
    dx, dy, dz = point[0] - eye[0], point[1] - eye[1], point[2] - eye[2]
    horiz = math.hypot(dx, dz)
    yaw = math.atan2(-dx, -dz)
    pitch = math.atan2(dy, horiz) if horiz > 1e-6 else (-math.pi / 2 if dy < 0 else math.pi / 2)
    return yaw, pitch


def _solid_observed(ex, cell) -> bool:
    name = bare(_observed_block(ex, cell).get("name"))
    return bool(name and name not in AIR)


def _is_bunker_position(ex, st, target) -> bool:
    """判断玩家是否已处于高柱或门框/打脚缝掩体位，避免 /attack 寻路走出安全位。"""
    ppos, tpos = _position(st.get("pos")), _position(target["pos"])
    dy = ppos[1] - tpos[1]
    horiz = math.hypot(ppos[0] - tpos[0], ppos[2] - tpos[2])
    if horiz > _BUNKER_REACH:
        return False
    if 1.5 <= dy <= 4.0:
        return True
    if abs(dy) >= 1.5:
        return False
    pcell, tcell = _cell(ppos), _cell(tpos)
    between = list(_line_cells(
        (pcell[0] + 0.5, pcell[1] + 0.5, pcell[2] + 0.5),
        (tcell[0] + 0.5, pcell[1] + 0.5, tcell[2] + 0.5),
    ))[1:-1]
    if any(_solid_observed(ex, (cx, pcell[1], cz)) or _solid_observed(ex, (cx, pcell[1] + 1, cz))
           for cx, _, cz in between):
        return True
    if bare(target.get("name")) in _TALL_HOSTILES:
        if _solid_observed(ex, (pcell[0], pcell[1] + 2, pcell[2])) or any(
            _solid_observed(ex, (cx, pcell[1] + 2, cz)) for cx, _, cz in between
        ):
            return True
    return False


def _resolve_melee_tactic(ex, st, target, tactic: str) -> str:
    if tactic not in MELEE_TACTICS:
        raise Replan(f"未知近战战术: {tactic}")
    if tactic != "auto":
        return tactic
    if bare(target.get("name")) in _KITE_HOSTILES:
        return "kite"
    if _is_bunker_position(ex, st, target):
        return "bunker_hit"
    return "direct"


def _set_shield_guard(ex, state, active: bool) -> bool:
    """§4.11 不占动作锁的盾牌格挡开合；收尾时吞掉异常防止遮蔽主错误。"""
    if not active and state.st.get("alive") is False:
        return False
    try:
        res = ex.posture(shield=active)
        return bool(res.get("blocking", active)) if isinstance(res, dict) else active
    except (ApiError, Replan, AttributeError):
        return False


def _has_foot_gap_lintel(ex, st, target) -> bool:
    """判断玩家与目标之间是否存在头顶门梁（y+1 实心、y 空旷的 1 格打脚缝）。"""
    ppos, tpos = _position(st.get("pos")), _position(target["pos"])
    if abs(ppos[1] - tpos[1]) >= 1.5:
        return False
    pcell, tcell = _cell(ppos), _cell(tpos)
    between = list(_line_cells(
        (pcell[0] + 0.5, pcell[1] + 0.5, pcell[2] + 0.5),
        (tcell[0] + 0.5, pcell[1] + 0.5, tcell[2] + 0.5),
    ))[1:-1]
    return any(
        _solid_observed(ex, (cx, pcell[1] + 1, cz)) and not _solid_observed(ex, (cx, pcell[1], cz))
        for cx, _, cz in between
    )


def _look_at_target(ex, eye, target, *, foot_gap: bool = False) -> tuple[float, float, bool]:
    """§4.17 优先用不占锁 /look 瞄准实体（1 格打脚缝时低瞄脚部防撞门梁）；不支持时返回本地解算 (yaw, pitch)。"""
    if foot_gap:
        tx, ty, tz = _position(target["pos"])
        aim = (tx, ty + 0.35, tz)
        yaw, pitch = _aim_angles(eye, aim)
        try:
            ex.look(pos=list(aim))
            return yaw, pitch, True
        except (ApiError, Replan, AttributeError):
            return yaw, pitch, False
    yaw, pitch = _aim_angles(eye, _target_aim_point(target))
    try:
        ex.look(entity_id=target["id"])
        return yaw, pitch, True
    except (ApiError, Replan, AttributeError):
        return yaw, pitch, False


def _translate_attack_replan(ex, state, target, exc: Replan) -> None:
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
    raise exc


def _hp_critical(state, status) -> bool:
    if status.get("alive") is False:
        return True
    hp_crit = float(getattr(getattr(state, "config", None), "hp_crit", 8.0) or 8.0)
    hp = status.get("health", 20)
    return type(hp) in (int, float) and float(hp) <= hp_crit


def _check_killed(receipt, after, *, require_receipt_killed: bool) -> bool:
    health = (after or {}).get("health")
    alive_after = after is not None and (type(health) not in (int, float) or health > 0)
    if alive_after:
        return False
    if require_receipt_killed:
        return receipt.get("killed") is True
    return after is None or (type(health) in (int, float) and health <= 0) or receipt.get("killed") is True


def pillar_up(ex, state, *, height: int = 3, item: str | None = None):
    """§4.3、§4.11：持续跳跃配合脚下垫块，原地垫起安全高柱以脱离近战围攻。"""
    _bounded(height, 1, 5, "高柱高度")
    st = state.refresh_status(ex.client, max_age_s=0)
    if not _grounded(ex, st):
        return False
    state.refresh_inventory(ex.client)
    if total_placeable(state, item) < height:
        return False
    fx, fy, fz = _cell(_position(st.get("pos")))
    start_y = float(st["pos"][1])
    for dy in range(2, height + 2):
        name = bare(_observed_block(ex, (fx, fy + dy, fz)).get("name"))
        if name not in AIR:
            return False
    ex.posture(jump=True)
    try:
        for step in range(height):
            state.refresh_inventory(ex.client)
            block = pick_placeable(state, item)
            target_pos = [fx, fy + step, fz]
            try:
                res = ex.place(block, pos=target_pos)
            except Replan as exc:
                code, _ = failure(exc)
                if code in ("blocked_by_self", "occupied", "no_support", "place_failed"):
                    yield rec("place", None, item=block, pos=target_pos, tactic="pillar_up", error_code=code)
                    break
                raise
            yield rec("place", res, item=block, pos=target_pos, tactic="pillar_up")
    finally:
        if state.st.get("alive") is not False:
            try:
                ex.posture(jump=False)
            except (ApiError, Replan, AttributeError):
                pass
    after = state.refresh_status(ex.client, max_age_s=0)
    pos_after = after.get("pos") or [0.0, start_y, 0.0]
    return float(pos_after[1]) >= start_y + min(2.0, float(height))


def dump_water_crowd_control(ex, state):
    """§4.4、§4.5：脚下倒水冲开贴脸群怪并防止苦力怕炸毁方块。"""
    st = state.refresh_status(ex.client, max_age_s=0)
    if not _grounded(ex, st):
        return False
    state.refresh_inventory(ex.client)
    before = state.count("water_bucket")
    if before < 1:
        return False
    fx, fy, fz = _cell(_position(st.get("pos")))
    floor_pos = [fx, fy - 1, fz]
    try:
        res = ex.interact(pos=floor_pos, item="water_bucket")
    except Replan as exc:
        code, _ = failure(exc)
        if code in ("cannot_interact", "out_of_reach", "missing_item"):
            yield rec("interact", None, item="water_bucket", pos=floor_pos, tactic="crowd_water", error_code=code)
            return False
        raise
    yield rec("interact", res, item="water_bucket", pos=floor_pos, tactic="crowd_water")
    state.refresh_inventory(ex.client)
    return state.count("water_bucket") < before or state.count("bucket") > 0 or bool(res)


def combat_melee(ex, state, entity, *, retreat_pos, timeout_ms=8000, radius=8,
                 advancement=None, advancement_polls=3, tactic="auto", max_rounds=6):
    """§3.2、§3.5、§3.7、§4.6、§4.11、§4.16、§4.17：支持 auto/kite/bunker_hit/direct 的单目标近战。"""
    _bounded(timeout_ms, 1, 20000, "交战超时")
    _bounded(radius, 1, 16, "近战观察半径")
    _bounded(advancement_polls, 1, 5, "进度读取次数")
    _bounded(max_rounds, 1, 12, "近战交战轮次")
    if _completed(ex, advancement):
        return {"receipt": None, "already_completed": True, "confirmed": True}
    target = _current_entity(ex, state, entity, radius, required=True)
    target_before = target
    st = state.snapshot()
    resolved = _resolve_melee_tactic(ex, st, target, tactic)
    if not _grounded(ex, st):
        raise Replan("近战前未确认站位支撑及短退路")
    if (resolved != "bunker_hit" or retreat_pos is not None) and not _retreat_clear(
        ex, _position(st.get("pos")), retreat_pos
    ):
        raise Replan("近战前未确认站位支撑及短退路")
    initial_inventory = state.refresh_inventory(ex.client)
    has_shield = _has_offhand_shield(state, st)
    shield_raised = False
    receipt: dict = {}
    after = target
    killed = False

    try:
        if resolved == "direct" and not has_shield:
            try:
                receipt = ex.attack(target["id"], timeout_ms=timeout_ms, stop_on=("hurt", "death"))
            except Replan as exc:
                _translate_attack_replan(ex, state, target, exc)
            yield rec("attack", receipt, entity_id=target["id"])
            state.refresh_status(ex.client, max_age_s=0)
            after_inventory = state.refresh_inventory(ex.client)
            after = _current_entity(ex, state, target, radius, required=False)
            killed = _check_killed(receipt, after, require_receipt_killed=True)
            if not killed:
                interrupted_by = receipt.get("interrupted_by")
                if interrupted_by:
                    raise Replan(f"本次近战被中断（interrupted_by={interrupted_by}），未确认击杀")
                raise Replan("本次近战未确认击杀")
        elif resolved == "direct":
            burst_timeout = min(timeout_ms, 3000)
            for round_idx in range(max_rounds):
                if round_idx > 0:
                    st = state.refresh_status(ex.client, max_age_s=0)
                    if _hp_critical(state, st):
                        raise Replan(f"近战期间血量危急（hp={st.get('health')}），中止交战")
                    fresh = _current_entity(ex, state, target, radius, required=False)
                    if fresh is None or (type(fresh.get("health")) in (int, float) and fresh["health"] <= 0):
                        after, killed = fresh, True
                        break
                    target = fresh
                if shield_raised:
                    shield_raised = _set_shield_guard(ex, state, False)
                try:
                    receipt = ex.attack(target["id"], timeout_ms=burst_timeout, max_hits=2,
                                        stop_on=("hurt", "death"))
                except Replan as exc:
                    _translate_attack_replan(ex, state, target, exc)
                yield rec("attack", receipt, entity_id=target["id"], tactic="direct", max_hits=2)
                st = state.refresh_status(ex.client, max_age_s=0)
                after_inventory = state.refresh_inventory(ex.client)
                after = _current_entity(ex, state, target, radius, required=False)
                if receipt.get("interrupted_by") == "stop":
                    raise Replan("本次近战被中断（interrupted_by=stop），未确认击杀")
                if _check_killed(receipt, after, require_receipt_killed=False):
                    killed = True
                    break
                shield_raised = _set_shield_guard(ex, state, True)
                if _hp_critical(state, st):
                    raise Replan(f"近战期间血量危急（hp={st.get('health')}），中止交战")
                if after is not None:
                    target = after
            if not killed:
                interrupted_by = receipt.get("interrupted_by")
                if interrupted_by:
                    raise Replan(f"本次近战被中断（interrupted_by={interrupted_by}），未确认击杀")
                raise Replan("本次近战未确认击杀")
        elif resolved == "kite":
            burst_timeout = min(timeout_ms, 3000)
            for round_idx in range(max_rounds):
                if round_idx > 0:
                    st = state.refresh_status(ex.client, max_age_s=0)
                    if _hp_critical(state, st):
                        raise Replan(f"风筝近战期间血量危急（hp={st.get('health')}），中止交战")
                    fresh = _current_entity(ex, state, target, radius, required=False)
                    if fresh is None or (type(fresh.get("health")) in (int, float) and fresh["health"] <= 0):
                        after, killed = fresh, True
                        break
                    target = fresh
                if shield_raised:
                    shield_raised = _set_shield_guard(ex, state, False)
                try:
                    receipt = ex.attack(target["id"], timeout_ms=burst_timeout, max_hits=1,
                                        stop_on=("hurt", "death"))
                except Replan as exc:
                    _translate_attack_replan(ex, state, target, exc)
                yield rec("attack", receipt, entity_id=target["id"], tactic="kite", max_hits=1)
                st = state.refresh_status(ex.client, max_age_s=0)
                after_inventory = state.refresh_inventory(ex.client)
                after = _current_entity(ex, state, target, radius, required=False)
                if receipt.get("interrupted_by") == "stop":
                    raise Replan("本次近战被中断（interrupted_by=stop），未确认击杀")
                if _check_killed(receipt, after, require_receipt_killed=False):
                    killed = True
                    break
                if has_shield:
                    shield_raised = _set_shield_guard(ex, state, True)
                if _hp_critical(state, st):
                    raise Replan(f"风筝近战期间血量危急（hp={st.get('health')}），中止交战")
                yaw, _, _ = _look_at_target(ex, eye_pos(st), after or target)
                try:
                    back_res = ex.control(back=True, ms=450, yaw=yaw, stop_on=("death",))
                    yield rec("control", back_res, tactic="kite_backstep", entity_id=target["id"])
                except (Replan, AttributeError) as exc:
                    code, _ = failure(exc)
                    if isinstance(exc, AttributeError) or code == "unsupported_api":
                        yield rec("control", None, tactic="kite_backstep", error_code="unsupported_api")
                        if retreat_pos is not None:
                            mv = ex.move_to(pos=list(retreat_pos), rng=1, stop_on=("death",))
                            yield rec("move_to", mv, tactic="kite_retreat", pos=list(retreat_pos))
                    else:
                        raise
                if after is not None:
                    target = after
            if not killed:
                raise Replan("风筝近战达到轮次上限，未确认击杀")
        else:  # bunker_hit: §4.16 + §4.17 定点无伤挥砍，绝不调 /attack 走下掩体
            consecutive_misses = 0
            for round_idx in range(max_rounds):
                if round_idx > 0:
                    st = state.refresh_status(ex.client, max_age_s=0)
                    if not _grounded(ex, st) or _hp_critical(state, st):
                        raise Replan("掩体近战期间站位失稳或血量危急，中止挥砍")
                    fresh = _current_entity(ex, state, target, radius, required=False)
                    if fresh is None or (type(fresh.get("health")) in (int, float) and fresh["health"] <= 0):
                        after, killed = fresh, True
                        break
                    target = fresh
                if shield_raised:
                    shield_raised = _set_shield_guard(ex, state, False)
                yaw, pitch, looked = _look_at_target(
                    ex, eye_pos(st), target, foot_gap=_has_foot_gap_lintel(ex, st, target),
                )
                ctrl_kwargs = {"hit": 1, "stop_on": ("death",)}
                if not looked:
                    ctrl_kwargs.update(yaw=yaw, pitch=pitch)
                receipt = ex.control(**ctrl_kwargs)
                yield rec("control", receipt, entity_id=target["id"], tactic="bunker_hit", hit=1)
                st = state.refresh_status(ex.client, max_age_s=0)
                after_inventory = state.refresh_inventory(ex.client)
                after = _current_entity(ex, state, target, radius, required=False)
                if receipt.get("interrupted_by") == "stop":
                    raise Replan("本次近战被中断（interrupted_by=stop），未确认击杀")
                if _check_killed(receipt, after, require_receipt_killed=False):
                    killed = True
                    receipt = {**receipt, "killed": True}
                    break
                hits = receipt.get("hits")
                if isinstance(hits, list) and hits and all(
                    isinstance(h, dict) and h.get("entity") is None for h in hits
                ):
                    consecutive_misses += 1
                    if consecutive_misses >= 2:
                        raise Replan("掩体定点挥砍连续未命中，目标超出 3 格攻击距离")
                else:
                    consecutive_misses = 0
                if has_shield:
                    shield_raised = _set_shield_guard(ex, state, True)
                if after is not None:
                    target = after
            if not killed:
                raise Replan("掩体定点挥砍达到轮次上限，未确认击杀")
    finally:
        if shield_raised:
            _set_shield_guard(ex, state, False)

    after_inventory = state.refresh_inventory(ex.client)
    health = (after or {}).get("health")
    confirmed = yield from _verify_advancement(ex, advancement, advancement_polls)
    return {"receipt": receipt, "target_before": target_before, "target_after": after,
            "inventory_before": initial_inventory, "inventory_after": after_inventory,
            "confirmed": confirmed or health == 0 or after is None, "tactic": resolved}


def shoot_projectile(ex, state, *, entity=None, pos=None, weapon="bow", charge_ms=1100,
                     retreat_pos, min_horizontal=0, max_distance=32,
                     advancement=None, advancement_polls=3, elevated_ok=False):
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
                            min_horizontal=min_horizontal, max_distance=max_distance,
                            elevated_ok=elevated_ok)
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
