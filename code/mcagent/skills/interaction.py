"""一次目标交互、空放或有界姿态，独立保留动作回执与外部后验。"""
from __future__ import annotations

import sys
import time

from ..client import ApiError
from ..runtime import Replan
from .base import rec
from .field import _bounded, _cell, _observed_block, _position
from .items import bare


def _target_args(target, *, block=False):
    if type(target) is int and target >= 0:
        return {"entity_id": target}
    if isinstance(target, dict):
        if set(target) == {"entity_id"} and type(target["entity_id"]) is int and target["entity_id"] >= 0:
            return {"entity_id": target["entity_id"]}
        if set(target) != {"pos"}:
            raise Replan("请提供一个现场观察到的方块坐标或实体 id")
        target = target["pos"]
    return {"pos": list(_cell(target) if block else _position(target))}


def _snapshot(ex, state, pos=None):
    view = {"inventory": state.refresh_inventory(ex.client),
            "status": state.refresh_status(ex.client, max_age_s=0)}
    if pos is not None:
        block = _observed_block(ex, pos)
        if not block.get("name"):
            raise Replan(f"交互目标格 {pos} 未加载，需重观测")
        view["block"] = block
    return view


def _check_item(state, item):
    if item is not None and state.count(item) < 1:
        raise Replan(f"背包缺少要使用的 {item}")


def _finish(receipt, before, after, verify):
    evidence = {"receipt": receipt, "before": before, "after": after, "confirmed": False}
    if verify is not None:
        if not verify(evidence):
            raise Replan("交互已发出，独立观察尚未确认预期结果")
        evidence["confirmed"] = True
    return evidence


def use_item_on(ex, state, target, item=None, *, verify=None):
    """§4.4：target 为 pos / entity_id / 对应单键字典；verify(evidence) 检查后验。

    不传 verify 时只返回尝试证据，调用方需独立判定后验（例如作物上方、繁殖或进度）。
    """
    args = _target_args(target, block=True)
    item = bare(item) if item is not None else None
    before = _snapshot(ex, state, args.get("pos"))
    _check_item(state, item)
    receipt = ex.interact(**args, item=item)
    yield rec("interact", receipt, item=item, **args)
    return _finish(receipt, before, _snapshot(ex, state, args.get("pos")), verify)


def use_item(ex, state, item, target=None, *, ms=None, timeout_ms=None, source=None, verify=None):
    """§4.5：target 可省略，有界持时/超时，调用方按物品效果检查后验。"""
    item = bare(item)
    args = _target_args(target) if target is not None else None
    if ms is not None:
        _bounded(ms, 1, 10000, "物品保持时长")
    if timeout_ms is not None:
        # 本地调用预算，服务端的物品专项上限仍需探针确认。
        _bounded(timeout_ms, 1, 60000, "物品等待时长")
    source = "off-hand" if source == "offhand" else source
    if source not in (None, "hand", "off-hand"):
        raise Replan("物品来源只支持 hand 或 off-hand")
    before = _snapshot(ex, state)
    _check_item(state, item)
    try:
        receipt = ex.use_item(item, target=args, ms=ms, timeout_ms=timeout_ms, source=source)
        yield rec("use_item", receipt, item=item, target=args)
    finally:
        if item == "shield":
            _clear_posture(ex, state, sneak=None, shield=False)
    return _finish(receipt, before, _snapshot(ex, state), verify)


def _clear_posture(ex, state, *, sneak, shield):
    # abort() 透过 GeneratorExit 收尾；清理错误不能覆盖已经在处理的抢占或死亡。
    unwinding = sys.exc_info()[0] is not None
    try:
        cleared = ex.posture(sneak=sneak, shield=shield)
        for requested, key in ((sneak, "sneaking"), (shield, "blocking")):
            if requested is not None and cleared.get(key) is not requested:
                raise Replan(f"姿态收尾后未确认 {key}={requested}")
        return cleared
    except (ApiError, Replan):
        if not unwinding:
            raise
    finally:
        if shield is not None:
            state.invalidate_inventory()


def hold_posture(ex, state, *, sneak=None, shield=None, duration_ms=1000, verify=None):
    """§4.11：有限保持指定姿态；verify(evidence) 可提前终止等待。

    只复位本次指定的姿态字段。保持姿态不代表真正挡箭或触发进度。
    """
    _bounded(duration_ms, 1, 10000, "姿态保持时长")
    if (sneak is None and shield is None
            or any(flag is not None and type(flag) is not bool for flag in (sneak, shield))):
        raise Replan("请指定要维持的 sneak 或 shield 姿态")
    if shield:
        state.refresh_inventory(ex.client)
        _check_item(state, "shield")
    before = state.refresh_status(ex.client, max_age_s=0)
    try:
        entered = ex.posture(sneak=sneak, shield=shield)
        deadline = time.monotonic() + duration_ms / 1000
        yield rec("posture", entered, sneak=sneak, shield=shield)
        count = (duration_ms + 499) // 500 + 1
        for _ in range(count):
            after = state.refresh_status(ex.client, max_age_s=0)
            if (not after.get("alive", True) or after.get("health", 20) < before.get("health", 20)
                    or after.get("dimension") != before.get("dimension")):
                raise Replan("维持姿态期间观察到受伤、死亡或维度变化，退出交战")
            for requested, key in ((sneak, "sneaking"), (shield, "blocking")):
                if requested is not None and after.get(key) is not requested:
                    raise Replan(f"未观察到要求的姿态 {key}={requested}")
            evidence = {"receipt": entered, "before": before, "after": after, "confirmed": False}
            if verify is not None and verify(evidence):
                evidence["confirmed"] = True
                break
            delay = min(0.5, deadline - time.monotonic())
            if delay <= 0:
                break
            time.sleep(delay)
            yield rec("wait_posture", None, sneak=sneak, shield=shield)
    finally:
        cleared = _clear_posture(ex, state, sneak=False if sneak is not None else None,
                                 shield=False if shield is not None else None)
    yield rec("posture", cleared, sneak=False if sneak is not None else None,
              shield=False if shield is not None else None)
    if verify is not None and not evidence["confirmed"]:
        raise Replan("姿态等待已到上限，未确认目标效果")
    return evidence
