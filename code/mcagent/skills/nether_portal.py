"""Observe and complete a minimum Nether portal frame; cross using fresh landing evidence."""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from itertools import islice

from ..runtime import EnvBlocked, Replan
from .base import failure, rec
from .field import equip_item, place_block, scan_blocks
from .items import AIR, PLACEABLE_BLOCKS, best_pickaxe
from .nether_site import (
    OBSIDIAN, PORTAL, SCAFFOLDS, _above, _approach, _cell, _center, _dimension, _ground,
    _hazard, _name, _near, _neighbors, _pos, _stable, _standable, _standing, _walk,
)

_HAND_SCAFFOLDS = frozenset({"dirt", *(b for b in PLACEABLE_BLOCKS if b.endswith("_planks"))})

_RING = ((1, 0), (2, 0), (0, 1), (3, 1), (0, 2), (3, 2), (0, 3), (3, 3), (1, 4), (2, 4))
_FRAME_LIMIT = 16
_POLL_LIMIT = 12


@dataclass(frozen=True)
class Frame:
    base: tuple[int, int, int]
    axis: str

    def at(self, u, v):
        x, y, z = self.base
        return x + (u if self.axis == "x" else 0), y + v, z + (u if self.axis == "z" else 0)

    @property
    def ring(self):
        return tuple(self.at(u, v) for u, v in _RING)

    @property
    def inside(self):
        return tuple(self.at(u, v) for u in (1, 2) for v in (1, 2, 3))


@dataclass(frozen=True)
class Entry:
    foot: tuple[int, int, int]
    route: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True)
class BuildPlan:
    frame: Frame
    entry: Entry
    missing: int
    needs_scaffold: bool


def _entry_options(ex, start, foot, axis=None):
    if start == foot:
        yield Entry(foot, ())
        return
    if axis == "x":
        neighbors = [site for site in _neighbors(foot) if site[0] == foot[0]]
    elif axis == "z":
        neighbors = [site for site in _neighbors(foot) if site[2] == foot[2]]
    else:
        neighbors = _neighbors(foot)
    if abs(start[1] - foot[1]) > 1:
        return
    for x, _, z in neighbors:
        stand = (x, start[1], z)
        route = _approach(ex, start, stand)
        if route is not None and (foot[1] <= stand[1] or _name(ex, _above(_above(stand))) in AIR):
            yield Entry(foot, tuple(route))


def _find_activated_portal(ex, state, start):
    found = scan_blocks(ex, state, PORTAL, radius=16, limit=8)
    options = []
    seen = set()
    for block in found["blocks"]:
        x, y, z = block["pos"]
        axis = (block.get("properties") or {}).get("axis")
        for offset in range(3):
            foot = (x, y - offset, z)
            if foot in seen:
                continue
            seen.add(foot)
            if _name(ex, foot) == PORTAL and _standable(ex, foot, through_portal=True):
                options.extend(_entry_options(ex, start, foot, axis))
    return min(options, key=lambda entry: len(entry.route), default=None)


def _attachment(ex, pos, placed=()):
    return any(neighbor in placed or _stable(ex, neighbor)
               for neighbor in (*_neighbors(pos), _ground(pos), _above(pos)))


def _placement_order(ex, frame, stand):
    observed = {pos: _name(ex, pos) for pos in frame.ring}
    if any(name not in AIR | {OBSIDIAN} for name in observed.values()):
        return None
    if any(_name(ex, pos) not in AIR for pos in frame.inside):
        return None
    remaining = [pos for pos, name in observed.items() if name in AIR]
    if any(pos[::2] == stand[::2] or not _near(_center(stand), pos, radius=4.25) for pos in remaining):
        return None
    queue = []
    placed = set(frame.ring) - set(remaining)
    corners = tuple(frame.at(u, v) for u in (0, 3) for v in (0, 4))
    while remaining:
        pos = next((pos for pos in remaining if _attachment(ex, pos, placed)), None)
        if pos is not None:
            queue.append(("obsidian", pos))
        else:
            # A corner provides a reference face between otherwise disconnected ring sections.
            # Reclaim that same block immediately after attaching one permanent block.
            for corner in corners:
                if (_name(ex, corner) not in AIR or not _near(_center(stand), corner, radius=4.25)
                        or not _attachment(ex, corner, placed)):
                    continue
                pos = next((pos for pos in remaining if pos in (*_neighbors(corner),
                                                                  _above(corner), _ground(corner))), None)
                if pos is not None:
                    queue.extend((("support", corner), ("obsidian", pos), ("remove", corner)))
                    break
            if pos is None:
                return None
        placed.add(pos)
        remaining.remove(pos)
    return queue


def _material_scaffold(state, missing):
    if missing:
        if best_pickaxe(state) is None:
            # §8 1.3.11 ②：无合适工具时直接手持当前物品挖；无镐时优先选徒手可 100% 回收的泥土/木板。
            hand_ok = next(
                (item for item in PLACEABLE_BLOCKS if item in _HAND_SCAFFOLDS and item in SCAFFOLDS and state.count(item)),
                None,
            )
            if hand_ok is not None:
                return hand_ok
        return next((item for item in PLACEABLE_BLOCKS if item in SCAFFOLDS and state.count(item)), None)
    return None


def _candidate_plan(ex, state, frame, start):
    if start[1] not in (frame.base[1], frame.base[1] + 1):
        return None
    entries = [entry for u in (1, 2)
               for entry in _entry_options(ex, start, frame.at(u, 1), frame.axis)
               if start not in frame.inside and start not in frame.ring]
    for entry in sorted(entries, key=lambda option: len(option.route)):
        stand = entry.route[-1] if entry.route else start
        order = _placement_order(ex, frame, stand)
        if order is not None:
            return BuildPlan(frame, entry, sum(op == "obsidian" for op, _ in order),
                             any(op == "support" for op, _ in order))
    return None


def _frame_candidates(observed):
    evidence = Counter()
    for pos in observed:
        x, y, z = pos
        for u, v in _RING:
            evidence[Frame((x - u, y - v, z), "x")] += 1
            evidence[Frame((x, y - v, z - u), "z")] += 1
    for frame, count in islice(evidence.most_common(), _FRAME_LIMIT):
        if count < 2:
            break
        yield frame


def _new_frames(start):
    x, y, z = start
    for offset in (1, -1):
        for u in (1, 2):
            yield Frame((x - u, y, z + offset), "x")
            yield Frame((x + offset, y, z - u), "z")


def _choose_build_plan(ex, state, start):
    found = scan_blocks(ex, state, OBSIDIAN, radius=16, limit=12)
    observed = {tuple(block["pos"]) for block in found["blocks"]}
    candidates = (_candidate_plan(ex, state, frame, start) for frame in _frame_candidates(observed))
    missing = []
    for plan in candidates:
        if plan is None:
            continue
        if plan.missing <= state.count(OBSIDIAN) and (
                not plan.needs_scaffold or _material_scaffold(state, plan.missing)):
            return plan
        missing.append(plan.missing)
    for frame in _new_frames(start):
        plan = _candidate_plan(ex, state, frame, start)
        if plan is None:
            continue
        if plan.missing <= state.count(OBSIDIAN) and (
                not plan.needs_scaffold or _material_scaffold(state, plan.missing)):
            return plan
        missing.append(plan.missing)
    if missing:
        need = min(missing)
        if state.count(OBSIDIAN) < need:
            raise Replan(f"已观察到安全门位，缺普通黑曜石：需 {need} 块，包内 {state.count(OBSIDIAN)} 块")
        raise Replan("缺可回收的门框支撑块，请补一块圆石等整块垫材")
    raise EnvBlocked("未确认可安全到达、建造并进入的门位",
                     missing_predicate="safe_portal_site",
                     searched_volume="16 格，最多 8 个激活门、16 处残框及 8 处邻近新门位",
                     wake_on="chunk_moved")


def build_pattern(ex, state, frame, *, stand):
    """Place missing cells of the witnessed 4×5 frame and remove any borrowed corner."""
    order = _placement_order(ex, frame, stand)
    if order is None:
        raise Replan("最小门框的净空、施工距离或有效贴面未确认")
    state.refresh_inventory(ex.client)
    missing = sum(op == "obsidian" for op, _ in order)
    needs_scaffold = any(op == "support" for op, _ in order)
    support = _material_scaffold(state, missing) if needs_scaffold else None
    if state.count(OBSIDIAN) < missing or (needs_scaffold and not support):
        raise Replan("补框前请补实际缺额的普通黑曜石及所需临时整块支撑")
    for op, pos in order:
        status = state.refresh_status(ex.client, max_age_s=0)
        if (_dimension(status) != "overworld" or _cell(_pos(status)) != stand
                or not _standing(ex, status) or not _near(_pos(status), pos, radius=4.25)):
            raise Replan("补框时施工站位或维度已变化")
        if op == "remove":
            if _name(ex, pos) != support:
                raise Replan("无法确认自己放置的临时支撑")
            if support not in _HAND_SCAFFOLDS and (pick := best_pickaxe(state)) is not None:
                yield from equip_item(ex, state, pick)
            before = state.count(support)
            try:
                dug = ex.dig(list(pos), collect=True)
            except Replan as exc:
                if failure(exc)[0] == "dig_unconfirmed":
                    state.refresh_status(ex.client, max_age_s=0)
                    state.refresh_inventory(ex.client)
                    raise Replan("临时施工支撑挖掘返回 dig_unconfirmed，方块仍在原处") from exc
                raise
            yield rec("dig", dug, pos=list(pos), purpose="portal_scaffold")
            state.refresh_inventory(ex.client)
            if _name(ex, pos) not in AIR or state.count(support) < before + 1:
                raise Replan("未确认临时施工支撑已拆除并回收")
            continue
        if not _attachment(ex, pos):
            raise Replan("已观察的相邻施工支撑发生变化")
        item = OBSIDIAN if op == "obsidian" else support
        state.refresh_inventory(ex.client)
        before = state.count(item)
        yield from place_block(ex, state, item, pos)
        state.refresh_inventory(ex.client)
        if state.count(item) != before - 1:
            raise Replan("放置后背包未确认恰好少一块施工材料")
    if any(_name(ex, pos) != OBSIDIAN for pos in frame.ring):
        raise Replan("点火前未确认十格有效框架")
    return frame


def verify_portal_landing(ex, state):
    """Read the destination in place. Unknown support or return footing suspends movement."""
    status = state.refresh_status(ex.client, max_age_s=0)
    if _dimension(status) != "nether" or status.get("busy") or not _standing(ex, status, through_portal=True):
        raise Replan("已停止移动，等待下界落点的维度、姿态、承重及两格净空确认")
    foot = _cell(_pos(status))
    exits = [side for side in _neighbors(foot) if _standable(ex, side)]
    if not exits:
        raise Replan("下界落脚窄台未见可走向的承重点，留在原地")
    return {
        "dimension": "nether", "pos": list(_pos(status)), "sneaking": status.get("sneaking"),
        "blocking": status.get("blocking"), "exits": [list(pos) for pos in exits],
        "return_portal": next((list(pos) for pos in (foot, *_neighbors(foot))
                               if _name(ex, pos) == PORTAL and _standable(ex, pos, through_portal=True)), None),
    }


def traverse_portal(ex, state, entry):
    """Approach an active gate, take one entry step, stop before destination reads."""
    yield from _walk(ex, state, entry.route, "overworld")
    status = state.refresh_status(ex.client, max_age_s=0)
    foot = _cell(_pos(status))
    if (_dimension(status) != "overworld" or not _standing(ex, status, through_portal=True)
            or _name(ex, entry.foot) != PORTAL or not _standable(ex, entry.foot, through_portal=True)
            or (foot != entry.foot and (
                abs(foot[0] - entry.foot[0]) + abs(foot[2] - entry.foot[2]) != 1
                or abs(foot[1] - entry.foot[1]) > 1))):
        raise Replan("穿门前未确认活动传送门、相邻承重和净空")
    if foot[1] < entry.foot[1] and _name(ex, _above(_above(foot))) not in AIR:
        raise Replan("进门上踏的头部空间不足")
    stopped = False
    try:
        if foot != entry.foot:
            yield rec("move_to", ex.move_to(pos=_center(entry.foot), rng=0, stop_on=("hurt", "death")),
                      pos=list(entry.foot), purpose="enter_portal")
        for attempt in range(_POLL_LIMIT):
            now = state.refresh_status(ex.client, max_age_s=0)
            if _hazard(now) or now.get("health", 20) < status.get("health", 20):
                raise Replan("穿门时观察到受伤或流体危险，停止移动")
            if _dimension(now) == "nether":
                # §3.1 / §8 1.3.8 ②：切维度后 ready 会先变 false 等下界区块与实体到齐，期间动作报 not_ready。
                if now.get("ready") is True:
                    break
                if attempt + 1 < _POLL_LIMIT:
                    time.sleep(0.5)
                    yield rec("wait_portal", reason="await_nether_ready")
                continue
            if now.get("ready") and (_dimension(now) != "overworld" or _cell(_pos(now)) != entry.foot):
                raise Replan("移动回执未确认站在传送门内，取消继续移动")
            if attempt + 1 < _POLL_LIMIT:
                time.sleep(0.5)
                yield rec("wait_portal", reason="await_dimension")
        else:
            if _dimension(now) == "nether":
                raise Replan("已进入下界但世界加载尚未就绪（ready=false），等待区块和实体到齐后复核落点")
            raise EnvBlocked("站在活动门内仍未确认维度变化",
                             missing_predicate="nether_transition",
                             searched_volume=f"1 次进门，最多 {_POLL_LIMIT} 次状态读取",
                             wake_on="chunk_moved")
        try:
            stop_receipt = ex.client.stop()
        except Exception as exc:
            if failure(exc)[0] == "not_ready":
                raise Replan("下界切维度加载期间动作接口返回 not_ready，请等 ready=true 后复核落点") from exc
            raise Replan(f"穿门后 /stop 失败: {exc}") from exc
        stopped = True
        yield rec("stop", stop_receipt, purpose="portal_landing")
        if isinstance(stop_receipt, dict) and stop_receipt.get("released") is False:
            raise Replan(
                f"穿门后 /stop 等待 {stop_receipt.get('wait_ms')}ms 仍未释放动作锁"
                f"（released=false，action={stop_receipt.get('action')}），暂停后续动作防撞 busy"
            )
        return verify_portal_landing(ex, state)
    finally:
        if not stopped:
            ex.stop()
