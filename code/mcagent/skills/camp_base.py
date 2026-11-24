"""营地基础存储、三区分级仓库、自动穿甲补给与受控捡尸技能（Sprint 1.3 + Sprint 1.5）。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import TYPE_CHECKING, Any, Optional

from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, failure, rec
from .camp_plan import (
    ARMOR_BY_SLOT as _ARMOR_BY_SLOT, ARMOR_SLOTS, COOKED_FOOD_ORDER, EDIBLE_FOODS as _EDIBLE_FOODS,
    INTERACT_EYE_REACH, PLANK_ITEMS as _PLANK_ITEMS, STONE_OR_BETTER_SWORDS, SWORD_ORDER,
    adjacent_to_any_chest as _adjacent_to_any_chest, aggregate_container as _aggregate_container,
    armor_equip_slot_for, bag_count as _bag_count, classify_chest_role, compute_deposit_plan,
    compute_withdraw_plan, has_chest_or_materials as _has_chest_or_materials, has_surplus_for_deposit,
    is_near_camp, is_protected_cell as _is_protected_cell, needs_camp_withdraw,
    record_protected_cell as _record_protected_cell, state_dim as _state_dim, state_snap as _state_snap,
    sync_camp_memo as _sync_camp_memo,
)
from .field import _drop_name, _observed_block, collect_drops, place_block, scan_blocks, scan_entities
from .items import AIR, PICKAXE_ORDER, STONE_OR_BETTER_PICKS, bare, is_safe_solid_floor
from .mine_return import is_underground_in_mine, return_to_surface_from_mine
from .observations import refresh_inventory
from .recipes import RECIPES, _table_position, craft_recipe
from .registry import register
from .sightline import block_cell, can_reach_block, eye_to_block_dist, point3
from .windows import is_storage_window, put_window_item, take_container_item, window_view
from .world_memo import ALL_LOG_BLOCKS, memo_for

if TYPE_CHECKING:
    from ..runtime import Executor, State

_CONTAINER_BLOCKS = ("chest", "trapped_chest", "barrel")
_ADJACENT = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
_RING_SHIFTS = ((8, 0), (-8, 0), (0, 8), (0, -8), (8, 8), (-8, 8), (8, -8), (-8, -8))


def _bounded_move_to(ex: "Executor", pos: Sequence[float], *, rng: int = 2, timeout: float = 30.0) -> dict:
    dest = list(pos)
    try:
        return ex.move_to(pos=dest, rng=rng, timeout=timeout, retries=0)
    except TypeError:
        return ex.move_to(pos=dest, rng=rng)

def _corpse_move_timeout(
    state: "State", death_pos: Sequence[float], *, base: float = 30.0, cap: float = 180.0,
) -> float:
    """按到死亡点的水平距离放宽单次 `move_to` 超时：远距离捡尸不能沿用 30s 的近距离预算。"""
    try:
        cur = point3(_state_snap(state).get("pos"), label="当前坐标")
        dist = math.dist((float(cur[0]), float(cur[2])), (float(death_pos[0]), float(death_pos[2])))
    except Exception:
        return float(base)
    return float(min(float(cap), max(float(base), dist * 0.4 + 20.0)))


def _find_place_spot(
    ex: "Executor", state: "State", *, require_top_air: bool = True, avoid_chests: Sequence[tuple[int, int, int]] = (),
) -> tuple[int, int, int]:
    status = state.refresh_status(ex.client, max_age_s=0)
    px, py, pz = block_cell(status.get("pos"))
    for dy in (0, 1, -1):
        for dx, dz in _ADJACENT:
            cand = (px + dx, py + dy, pz + dz)
            if (cand[0] == px and cand[2] == pz and abs(cand[1] - py) <= 1) or _is_protected_cell(cand):
                continue
            if avoid_chests and _adjacent_to_any_chest(cand, avoid_chests):
                continue
            if bare(_observed_block(ex, cand).get("name")) not in AIR:
                continue
            if (is_safe_solid_floor(_observed_block(ex, (cand[0], cand[1] - 1, cand[2])))
                    and (not require_top_air or bare(_observed_block(ex, (cand[0], cand[1] + 1, cand[2])).get("name")) in AIR)
                    and can_reach_block(ex, status, cand, reach=INTERACT_EYE_REACH)):
                return cand
    raise EnvBlocked("周围未找到可安全放置营地设施的实心支撑空位", missing_predicate="camp_place_spot", searched_volume="角色周围 1 格实心支撑平面", wake_on="chunk_moved")

def _place_with_protection_retry(
    ex: "Executor", state: "State", item: str, dim: str, *, require_top_air: bool = True, avoid_chests: Sequence[tuple[int, int, int]] = (),
):
    memo = memo_for(state)
    for ring_idx in range(4):
        try:
            spot = _find_place_spot(ex, state, require_top_air=require_top_air, avoid_chests=avoid_chests)
        except EnvBlocked:
            if ring_idx >= 3:
                raise
            px, py, pz = block_cell(state.refresh_status(ex.client, max_age_s=0).get("pos"))
            dx, dz = _RING_SHIFTS[ring_idx % len(_RING_SHIFTS)]
            dest = [px + dx + 0.5, float(py), pz + dz + 0.5]
            yield rec("move_to", _bounded_move_to(ex, dest, rng=2, timeout=30.0), pos=dest)
            continue
        try:
            yield from place_block(ex, state, item, spot)
            return spot
        except Replan as exc:
            if failure(exc)[0] != "protected_region":
                raise
            _record_protected_cell(spot, memo, dim)
            yield rec("protected_region_skip", item=item, pos=list(spot))
            if ring_idx >= 3:
                raise
            dx, dz = _RING_SHIFTS[ring_idx % len(_RING_SHIFTS)]
            dest = [spot[0] + dx + 0.5, float(spot[1]), spot[2] + dz + 0.5]
            yield rec("move_to", _bounded_move_to(ex, dest, rng=2, timeout=30.0), pos=dest)
    raise EnvBlocked(f"多次绕开保护区后仍无法放置 {item}", missing_predicate="camp_place_spot", searched_volume="出生点保护区外缘环带", wake_on="chunk_moved")

def _craft_planks_until(ex: "Executor", state: "State", target_planks: int):
    while sum(state.count(p) for p in _PLANK_ITEMS) < target_planks:
        if (log := next((l for l in ALL_LOG_BLOCKS if state.count(l) >= 1), None)) is None:
            break
        plank = log.removesuffix("_log") + "_planks"
        yield from craft_recipe(ex, state, plank, goal_qty=state.count(plank) + 4)

def _ensure_chest_item(ex: "Executor", state: "State", dim: str):
    refresh_inventory(ex, state)
    if state.count("chest") >= 1:
        return
    yield from _craft_planks_until(ex, state, 8)
    if sum(state.count(p) for p in _PLANK_ITEMS) < 8:
        raise Replan("制作箱子需要 8 块木板，当前木板与原木不足")
    try:
        yield from _table_position(ex, state)
    except EnvBlocked:
        state.refresh_inventory(ex.client)
        if state.count("crafting_table") < 1:
            yield from _craft_planks_until(ex, state, 12)
            if sum(state.count(p) for p in _PLANK_ITEMS) < 12:
                raise Replan("附近无工作台，且木板不足 12 份（无法同时制作工作台与箱子）")
            yield from craft_recipe(ex, state, "crafting_table", goal_qty=1)
        table_pos = yield from _place_with_protection_retry(ex, state, "crafting_table", dim, require_top_air=False)
        memo_for(state).record_station("crafting_table", table_pos, dimension=dim)
    if "chest" in RECIPES:
        yield from craft_recipe(ex, state, "chest", goal_qty=1)
        return
    before = state.count("chest")
    yield rec("craft", ex.craft("chest", 1), item="chest", count=1)
    refresh_inventory(ex, state)
    if state.count("chest") <= before:
        raise Replan("合成 chest 后背包未确认产出")

def _open_specific_chest(ex: "Executor", state: "State", chest_pos: tuple[int, int, int]):
    status = state.refresh_status(ex.client, max_age_s=0)
    if chest_pos[1] > float((status.get("pos") or state.pos)[1]) + 6.0 and is_underground_in_mine(ex, state):
        yield from return_to_surface_from_mine(ex, state)
        status = state.refresh_status(ex.client, max_age_s=0)
    if not can_reach_block(ex, status, chest_pos, reach=INTERACT_EYE_REACH):
        yield rec("move_to", _bounded_move_to(ex, chest_pos, rng=2, timeout=30.0), pos=list(chest_pos))
        status = state.refresh_status(ex.client, max_age_s=0)
        if status.get("ready") is False or status.get("alive") is False:
            raise Replan("接近营地箱子后状态异常或未就绪")
    if bare(_observed_block(ex, chest_pos).get("name")) not in _CONTAINER_BLOCKS or not can_reach_block(ex, status, chest_pos, reach=INTERACT_EYE_REACH):
        raise Replan(f"营地容器 {chest_pos} 不可达或未观测到有效箱子")
    target = {"pos": list(chest_pos)}
    view = yield from window_view(ex, target)
    if not is_storage_window(view):
        raise Replan(f"打开的窗口 {view.get('window')!r} 不是存储容器")
    return chest_pos, target, view


def _open_camp_chest(
    ex: "Executor", state: "State", dim: str, *, allow_create: bool, skill_name: str, open_window: bool = True, role: Optional[str] = None,
):
    status, memo = state.refresh_status(ex.client, max_age_s=0), memo_for(state)
    camp, chest_pos = memo.get_camp(dimension=dim), None
    preferred = (((camp.get("chests") or {}).get(role) if role else None) or camp.get("chest")) if isinstance(camp, Mapping) else None
    if preferred:
        cpos = block_cell(preferred)
        bname = bare(_observed_block(ex, cpos).get("name"))
        if bname in _CONTAINER_BLOCKS or (not bname and eye_to_block_dist(status, cpos) > INTERACT_EYE_REACH):
            chest_pos = cpos
    if chest_pos is None:
        scan = scan_blocks(ex, state, _CONTAINER_BLOCKS, radius=24, limit=6, skill_name=skill_name)
        if scan.get("ready") is False:
            raise Replan("世界区块加载尚未就绪，暂无法搜索营地箱子")
        if blocks := sorted(scan.get("blocks") or (), key=lambda b: eye_to_block_dist(status, b["pos"])):
            chest_pos = block_cell(blocks[0]["pos"])
        else:
            if not allow_create:
                raise EnvBlocked("未找到已记录或半径 24 格内可达的营地箱子", missing_predicate="camp_chest", searched_volume="营地记录与半径 24 格容器", wake_on="chunk_moved")
            yield from _ensure_chest_item(ex, state, dim)
            existing = [block_cell(p) for p in ((camp.get("chests") or {}).values() if isinstance(camp, Mapping) else ())]
            chest_pos = yield from _place_with_protection_retry(ex, state, "chest", dim, require_top_air=True, avoid_chests=existing)
            memo.record_station("chest", chest_pos, dimension=dim)
    return (chest_pos, None, None) if not open_window else (yield from _open_specific_chest(ex, state, chest_pos))


def _deposit_rows(ex: "Executor", state: "State", target: dict, view: dict, rows: Sequence[tuple[str, int]], moved: dict[str, int]):
    for item, qty in rows:
        state.refresh_inventory(ex.client)
        if (put_qty := min(qty, _bag_count(state, item))) <= 0:
            continue
        try:
            res = yield from put_window_item(ex, state, target, item, put_qty, to="container")
        except Replan as exc:
            if failure(exc)[0] in ("container_full", "inventory_full") or "已满" in str(exc):
                break
            raise
        view, moved[item] = res["view"], moved.get(item, 0) + put_qty
    return view


def _withdraw_from_view(ex: "Executor", state: "State", target: dict, view: dict, moved: dict[str, int]):
    for item, qty in compute_withdraw_plan(state, _aggregate_container(view)):
        try:
            res = yield from take_container_item(ex, state, target, "container", item, qty, view=view)
        except Replan as exc:
            if failure(exc)[0] == "inventory_full" or "inventory_full" in str(exc):
                break
            raise
        view = res["view"]
        if (got := int(res.get("count", 0) or 0)) > 0:
            moved[item] = moved.get(item, 0) + got
        if res.get("inventory_full"):
            break
    return view


def _auto_equip_withdrawn(ex: "Executor", state: "State", withdrawn: Mapping[str, int]):
    """取出护甲与盾牌后立刻穿戴到位，防止重生后空身出营。"""
    inv = refresh_inventory(ex, state)
    armor = inv.get("armor") if isinstance(inv.get("armor"), Mapping) else {}
    offhand = bare((inv.get("offhand") or {}).get("name")) if isinstance(inv.get("offhand"), Mapping) else ""
    for item in withdrawn:
        slot = armor_equip_slot_for(item)
        if not slot or state.count(item) < 1:
            continue
        cur = armor.get(slot) or (armor.get("chest") if slot == "torso" else None)
        if (slot == "off-hand" and offhand == "shield") or (isinstance(cur, Mapping) and bare(cur.get("name")) == bare(item)):
            continue
        try:
            yield rec("equip", ex.equip(item, slot), item=item, slot=slot)
            refresh_inventory(ex, state)
        except Replan:
            continue


@register
class ManageCampChest(Skill):
    name, goal, verified, cooldown_s = "manage_camp_chest", "camp:{mode}", "live", 10.0
    description = "在营地建箱存富余物资、重生开箱自动穿甲重装或受控捡尸"
    params = {"mode": Param(str, "营地操作模式", default="deposit", choices=("deposit", "withdraw", "setup", "recover_corpse"))}

    @classmethod
    def blocked(cls, state: "State") -> str:
        st = _state_snap(state)
        if st.get("ready") is False or st.get("alive") is False:
            return "世界加载尚未就绪" if st.get("ready") is False else "角色未存活，无法操作营地箱子"
        dim, memo = _state_dim(state), memo_for(state)
        camp = memo.get_camp(dimension=dim)
        has_camp_chest = (isinstance(camp, Mapping) and bool(camp.get("chest") or camp.get("chests"))) or memo.has_station("chest", dimension=dim)
        if has_camp_chest or _has_chest_or_materials(state):
            return ""
        try:
            pos = point3(st.get("pos"), label="当前坐标")
        except Replan:
            pos = None
        if pos is not None and memo.should_attempt_corpse_recovery(pos, dimension=dim)[0]:
            return ""
        return "当前既无营地箱与造箱木材，也无可执行的受控捡尸记录"

    def can_start(self, state: "State") -> bool:
        if self.blocked(state):
            return False
        mode, dim, memo = self.args["mode"], _state_dim(state), memo_for(state)
        if mode == "setup":
            return _has_chest_or_materials(state)
        camp = memo.get_camp(dimension=dim)
        has_chest = (isinstance(camp, Mapping) and bool(camp.get("chest") or camp.get("chests"))) or memo.has_station("chest", dimension=dim)
        if mode == "deposit":
            return bool(compute_deposit_plan(state)) and (has_chest or _has_chest_or_materials(state))
        if mode == "withdraw":
            if not has_chest:
                return False
            if needs_camp_withdraw(state):
                return True
            stored = camp.get("stored_items") if isinstance(camp, Mapping) else None
            return (not isinstance(stored, Mapping) or not stored) and (
                not any(state.count(p) >= 1 for p in STONE_OR_BETTER_PICKS) or not any(state.count(s) >= 1 for s in SWORD_ORDER))
        try:
            pos = point3(_state_snap(state).get("pos"), label="当前坐标")
        except Replan:
            return False
        return bool(memo.should_attempt_corpse_recovery(pos, dimension=dim)[0])

    def _run_setup(self, ex: "Executor", state: "State", dim: str, memo: Any):
        chest_pos, _, _ = yield from _open_camp_chest(ex, state, dim, allow_create=True, skill_name=self.name, open_window=False, role="kit")
        _sync_camp_memo(memo, dim, chest_pos, role="kit")
        camp = memo.get_camp(dimension=dim) or {}
        chests = dict(camp.get("chests") or {"kit": list(chest_pos)})
        if int(camp.get("shelter_tier", 0)) >= 1:
            for role in ("vault", "bulk"):
                if role in chests:
                    continue
                refresh_inventory(ex, state)
                if not _has_chest_or_materials(state):
                    break
                yield from _ensure_chest_item(ex, state, dim)
                existing = [block_cell(p) for p in chests.values()]
                try:
                    rpos = yield from _place_with_protection_retry(ex, state, "chest", dim, require_top_air=True, avoid_chests=existing)
                except EnvBlocked:
                    break
                chests[role] = list(rpos)
                _sync_camp_memo(memo, dim, rpos, role=role, shelter_tier=2 if len(chests) >= 3 else None)
        self._op_result = {"mode": "setup", "chest": list(chest_pos), "chests": chests}

    def _run_recover_corpse(self, ex: "Executor", state: "State", status: Mapping[str, Any], dim: str, memo: Any):
        ok, reason = memo.should_attempt_corpse_recovery(status.get("pos"), dimension=dim)
        if not ok:
            raise Replan(reason or "当前不满足受控捡尸条件")
        last_death = memo.get_last_death(dimension=dim) if hasattr(memo, "get_last_death") else status.get("last_death")
        if not isinstance(last_death, Mapping) or not last_death.get("pos"):
            memo.clear_last_death(dimension=dim)
            raise Replan("缺少有效死亡点坐标，无法捡尸")
        death_pos = point3(last_death["pos"], label="死亡点坐标")
        yield rec("move_to", _bounded_move_to(ex, death_pos, rng=2, timeout=_corpse_move_timeout(state, death_pos)), pos=list(death_pos))
        after = state.refresh_status(ex.client, max_age_s=0)
        if after.get("ready") is False or after.get("alive") is False:
            raise Replan("前往死亡点途中角色死亡或世界未就绪")
        refresh_inventory(ex, state)
        scan = scan_entities(ex, state, "item", radius=16, limit=40)
        if scan.get("ready") is not True:
            # 区块未就绪（刚重生/切维度）时扫不到掉落物。此时**绝不能**删死亡记录，
            # 否则"捡尸"会变成"尸体永久消失"：日志报成功，物资一件没捡回来。
            raise Replan("掉落实体尚未就绪，保留死亡坐标下一拍再捡")
        lost = {bare(x) for x in (last_death.get("lost_items") or last_death.get("items") or ()) if bare(x)}
        seen = list(dict.fromkeys(n for ent in scan.get("entities") or () if (n := _drop_name(ent))))
        for drop_item in [n for n in seen if n in lost] + [n for n in seen if n not in lost]:
            try:
                yield from collect_drops(ex, state, drop_item, count=1, radius=12, max_targets=3)
            except (EnvBlocked, Replan):
                continue
        memo.clear_last_death(dimension=dim)
        self._op_result = {"mode": "recover_corpse", "death_pos": list(death_pos)}

    def run(self, ex: "Executor", state: "State"):
        self._op_result: Optional[dict[str, Any]] = None
        mode, status = self.args["mode"], state.refresh_status(ex.client, max_age_s=0)
        if status.get("ready") is False or status.get("alive") is False:
            raise Replan("世界未就绪或角色未存活，无法执行营地操作")
        dim, memo = bare(status.get("dimension") or "overworld").strip() or "overworld", memo_for(state)
        if mode in ("setup", "recover_corpse"):
            yield from (self._run_setup(ex, state, dim, memo) if mode == "setup" else self._run_recover_corpse(ex, state, status, dim, memo))
            return
        refresh_inventory(ex, state)
        allow_create = mode == "deposit"
        plan = compute_deposit_plan(state) if allow_create else []
        if allow_create and not plan:
            raise Replan("当前背包没有需要存入营地箱子的未预留富余物资")
        camp = memo.get_camp(dimension=dim) or {}
        chests_map = {r: block_cell(p) for r, p in (camp.get("chests") or {}).items()} if isinstance(camp.get("chests"), Mapping) else {}
        moved: dict[str, int] = {}
        primary_pos: Optional[tuple[int, int, int]] = None
        if allow_create:
            if len(chests_map) >= 2:
                grouped: dict[str, list[tuple[str, int]]] = {}
                for item, qty in plan:
                    role = classify_chest_role(item)
                    grouped.setdefault(role if role in chests_map else ("kit" if "kit" in chests_map else next(iter(chests_map))), []).append((item, qty))
                for role, rows in grouped.items():
                    cpos, target, view = yield from _open_specific_chest(ex, state, chests_map[role])
                    primary_pos = primary_pos or cpos
                    view = yield from _deposit_rows(ex, state, target, view, rows, moved)
                    _sync_camp_memo(memo, dim, cpos, stored_items=_aggregate_container(view), role=role)
            else:
                chest_pos, target, view = yield from _open_camp_chest(ex, state, dim, allow_create=True, skill_name=self.name)
                primary_pos = chest_pos
                view = yield from _deposit_rows(ex, state, target, view, plan, moved)
                _sync_camp_memo(memo, dim, chest_pos, stored_items=_aggregate_container(view), role="kit" if "kit" in chests_map else None)
            if not moved:
                raise Replan("未能向营地箱子存入任何计划物资")
            if memo.camp_has_recovery_kit(dimension=dim):
                memo.set_camp_kit_depleted(False, dimension=dim)
        else:
            role_order = [r for r in ("kit", "vault", "bulk") if r in chests_map]
            if len(role_order) >= 2:
                for role in role_order:
                    cpos, target, view = yield from _open_specific_chest(ex, state, chests_map[role])
                    primary_pos = primary_pos or cpos
                    view = yield from _withdraw_from_view(ex, state, target, view, moved)
                    _sync_camp_memo(memo, dim, cpos, stored_items=_aggregate_container(view), role=role)
                    if not needs_camp_withdraw(state):
                        break
            else:
                chest_pos, target, view = yield from _open_camp_chest(ex, state, dim, allow_create=False, skill_name=self.name)
                primary_pos = chest_pos
                view = yield from _withdraw_from_view(ex, state, target, view, moved)
                _sync_camp_memo(memo, dim, chest_pos, stored_items=_aggregate_container(view))
            if moved:
                yield from _auto_equip_withdrawn(ex, state, moved)
            if not memo.camp_has_recovery_kit(dimension=dim):
                memo.set_camp_kit_depleted(True, dimension=dim)
        self._op_result = {"mode": mode, "chest": list(primary_pos or (0, 0, 0)), ("deposited" if allow_create else "withdrawn"): moved}

    def verify_done(self, state: "State", before_counts: Optional[dict[str, int]] = None) -> str:
        mode, dim, memo = self.args["mode"], _state_dim(state), memo_for(state)
        camp, res = memo.get_camp(dimension=dim), getattr(self, "_op_result", None)
        if mode == "recover_corpse":
            if not isinstance(res, Mapping) or res.get("mode") != "recover_corpse":
                return "尚未完成受控捡尸流程"
            return "捡尸结束后未清除死亡记录" if (hasattr(memo, "get_last_death") and memo.get_last_death(dimension=dim)) else ""
        if not isinstance(camp, Mapping) or not (camp.get("chest") or camp.get("chests")):
            return "未确认营地箱子登记到世界记忆"
        if mode == "setup":
            return ""
        if not isinstance(res, Mapping) or res.get("mode") != mode:
            return f"尚未完成营地箱子 {mode} 操作"
        if mode == "deposit":
            deposited = res.get("deposited") or {}
            if not deposited:
                return "尚未确认向营地箱子存入物资"
            return "存箱后背包中未确认对应物资减少" if (before_counts is not None and not any(state.count(k) < before_counts.get(k, 0) for k in deposited)) else ""
        withdrawn = res.get("withdrawn") or {}
        return "取箱后背包中未确认补给物资增加" if (before_counts is not None and withdrawn and not any(state.count(k) > before_counts.get(k, 0) for k in withdrawn)) else ""
