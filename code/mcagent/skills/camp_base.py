"""营地基础存储、补给与受控捡尸技能（Part B / Sprint 1.3）。"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
import math
from typing import TYPE_CHECKING, Any, Optional

from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, failure, rec
from .field import _drop_name, _observed_block, collect_drops, place_block, scan_blocks, scan_entities
from .items import AIR, PICKAXE_ORDER, STONE_OR_BETTER_PICKS, bare, is_safe_solid_floor
from .observations import refresh_inventory
from .recipes import RECIPES, _table_position, craft_recipe
from .registry import register
from .sightline import STATION_EYE_REACH, block_cell, can_reach_block, eye_to_block_dist, point3
from .windows import is_storage_window, put_window_item, take_container_item, window_section, window_view
from .world_memo import ALL_LOG_BLOCKS, memo_for

if TYPE_CHECKING:
    from ..runtime import Executor, State

INTERACT_EYE_REACH: float = STATION_EYE_REACH
SWORD_ORDER = ("netherite_sword", "diamond_sword", "iron_sword", "copper_sword", "stone_sword", "golden_sword", "wooden_sword")
STONE_OR_BETTER_SWORDS = frozenset({"stone_sword", "copper_sword", "iron_sword", "diamond_sword", "netherite_sword"})
ARMOR_SLOTS = ("helmet", "chestplate", "leggings", "boots")
_ARMOR_BY_SLOT: dict[str, tuple[str, ...]] = {
    "helmet": ("netherite_helmet", "diamond_helmet", "iron_helmet", "turtle_helmet", "copper_helmet", "chainmail_helmet", "golden_helmet", "leather_helmet"),
    **{s: tuple(f"{t}_{s}" for t in ("netherite", "diamond", "iron", "copper", "chainmail", "golden", "leather")) for s in ARMOR_SLOTS[1:]},
}
COOKED_FOOD_ORDER = ("golden_carrot", "cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_salmon", "cooked_chicken", "cooked_cod", "cooked_rabbit", "baked_potato", "bread")
_EDIBLE_FOODS = frozenset(COOKED_FOOD_ORDER) | frozenset({"apple", "carrot", "melon_slice", "sweet_berries", "glow_berries", "pumpkin_pie", "cookie", "golden_apple", "enchanted_golden_apple", "beetroot", "beetroot_soup", "mushroom_stew", "rabbit_stew", "honey_bottle", "dried_kelp"})
_PLANK_ITEMS = tuple(item for item in RECIPES if item.endswith("_planks"))
_CONTAINER_BLOCKS = ("chest", "trapped_chest", "barrel")
_HIGH_VALUE = frozenset({"diamond", "obsidian", "iron_ingot", "shield"})
_GEAR_SUFFIX = ("_pickaxe", "_sword", "_helmet", "_chestplate", "_leggings", "_boots")
_ADJACENT = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


def _state_snap(state: Any) -> Mapping[str, Any]:
    snap = state.snapshot() if hasattr(state, "snapshot") else getattr(state, "st", None)
    return snap if isinstance(snap, Mapping) else {}


def _state_dim(state: Any) -> str:
    return bare(_state_snap(state).get("dimension") or "overworld").strip() or "overworld"


def _bag_count(state: "State", item: str) -> int:
    target = bare(item)
    return sum(int(r.get("count", 0) or 0) for r in ((getattr(state, "inventory", None) or {}).get("items") or ())
               if isinstance(r, Mapping) and bare(r.get("name")) == target)


def _excess_best_one(state: "State", order: Sequence[str], eligible: Optional[Collection[str]] = None) -> list[tuple[str, int]]:
    best = next((it for it in order if state.count(it) >= 1), None)
    return [(it, ex_qty) for it in order if (eligible is None or it in eligible) and (ex_qty := state.count(it) - (1 if it == best else 0)) > 0]


def compute_deposit_plan(state: "State") -> list[tuple[str, int]]:
    """按随身配额计算应存入营地箱的富余物资清单 `[(item, qty), ...]`。"""
    plan = _excess_best_one(state, PICKAXE_ORDER, STONE_OR_BETTER_PICKS)
    plan.extend(_excess_best_one(state, SWORD_ORDER, STONE_OR_BETTER_SWORDS))
    if (shields := state.count("shield")) > 1:
        plan.append(("shield", shields - 1))
    for slot in ARMOR_SLOTS:
        plan.extend(_excess_best_one(state, _ARMOR_BY_SLOT[slot]))
    keep_food = 12
    for food in COOKED_FOOD_ORDER:
        if (cnt := state.count(food)) > 0:
            kept = min(cnt, keep_food)
            keep_food -= kept
            if cnt - kept > 0:
                plan.append((food, cnt - kept))
    for item, quota in (("iron_ingot", 4), ("diamond", 0), ("obsidian", 0)):
        if (cnt := state.count(item)) > quota:
            plan.append((item, cnt - quota))
    return plan


def has_surplus_for_deposit(state: "State") -> bool:
    """判断当前是否携带足够触发营地存箱的富余或高价值物资。"""
    if state.count("iron_ingot") >= 8 or state.count("diamond") >= 3 or state.count("obsidian") >= 4:
        return True
    plan = compute_deposit_plan(state)
    return sum(q for _, q in plan) >= 4 and any(bare(i) in _HIGH_VALUE or bare(i).endswith(_GEAR_SUFFIX) for i, _ in plan)


def _take_if_missing(state: "State", stored: Mapping[str, int], order: Sequence[str], owned: Optional[Collection[str]] = None) -> Optional[str]:
    if any(state.count(x) >= 1 for x in (owned or order)):
        return None
    pick = next((x for x in order if (owned is None or x in owned) and stored.get(x, 0) >= 1), None)
    if pick is not None or owned is None or any(state.count(x) >= 1 for x in order):
        return pick
    return next((x for x in order if stored.get(x, 0) >= 1), None)


def compute_withdraw_plan(state: "State", stored_items: Mapping[str, int]) -> list[tuple[str, int]]:
    """根据身上缺口与箱内已知存货计算应取出的补给清单 `[(item, qty), ...]`。"""
    stored: dict[str, int] = {}
    for raw_k, raw_v in (stored_items or {}).items():
        if (name := bare(raw_k)) and isinstance(raw_v, int) and not isinstance(raw_v, bool) and raw_v > 0:
            stored[name] = stored.get(name, 0) + raw_v
    slots = ((PICKAXE_ORDER, STONE_OR_BETTER_PICKS), (SWORD_ORDER, None), (("shield",), None), *((_ARMOR_BY_SLOT[s], None) for s in ARMOR_SLOTS))
    plan = [(item, 1) for order, owned in slots if (item := _take_if_missing(state, stored, order, owned))]
    if sum(state.count(f) for f in _EDIBLE_FOODS) < 6:
        need = max(0, 8 - sum(state.count(f) for f in COOKED_FOOD_ORDER))
        for food in COOKED_FOOD_ORDER:
            if need <= 0:
                break
            if (avail := stored.get(food, 0)) > 0:
                take = min(avail, need)
                plan.append((food, take))
                need -= take
    return plan


def needs_camp_withdraw(state: "State") -> bool:
    """检查当前维度营地箱记录的存货是否能补齐角色当前装备/食物缺口。"""
    camp = memo_for(state).get_camp(dimension=_state_dim(state))
    stored = camp.get("stored_items") if isinstance(camp, Mapping) and camp.get("chest") else None
    return isinstance(stored, Mapping) and bool(stored) and bool(compute_withdraw_plan(state, stored))


def is_near_camp(state: "State", max_dist: float = 48.0) -> bool:
    """判断角色当前是否位于本维度营地 `max_dist` 格欧氏距离内。"""
    camp = memo_for(state).get_camp(dimension=_state_dim(state))
    target = camp.get("pos") or camp.get("chest") if isinstance(camp, Mapping) else None
    raw_pos = _state_snap(state).get("pos")
    if target is None or raw_pos is None:
        return False
    try:
        return math.dist(point3(raw_pos, label="当前坐标"), point3(target, label="营地坐标")) <= max_dist
    except Replan:
        return False


def _has_chest_or_materials(state: "State") -> bool:
    return state.count("chest") >= 1 or sum(state.count(p) for p in _PLANK_ITEMS) + sum(state.count(l) for l in ALL_LOG_BLOCKS) * 4 >= 8


def _aggregate_container(view: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in window_section(dict(view), "container"):
        if isinstance(entry, Mapping) and (name := bare(entry.get("name"))) and (qty := int(entry.get("count", 0) or 0)) > 0:
            counts[name] = counts.get(name, 0) + qty
    return counts


def _sync_camp_memo(memo: Any, dim: str, chest_cell: tuple[int, int, int], stored_items: Optional[dict[str, int]] = None) -> None:
    memo.record_station("chest", chest_cell, dimension=dim)
    camp = memo.get_camp(dimension=dim)
    camp_pos = list(camp["pos"]) if isinstance(camp, Mapping) and camp.get("pos") else list(chest_cell)
    stations = {k: list(min(near, key=lambda c: math.dist(chest_cell, c)))
                for k in ("bed", "furnace", "crafting_table")
                if (near := [c for c in memo.stations(k, dimension=dim) if math.dist(chest_cell, c) <= 16.0])}
    memo.set_camp(camp_pos, dimension=dim, chest=list(chest_cell), stored_items=stored_items, **stations)
    if stored_items is not None:
        memo.update_camp_stored(stored_items, dimension=dim)


def _find_place_spot(ex: "Executor", state: "State", *, require_top_air: bool = True) -> tuple[int, int, int]:
    status = state.refresh_status(ex.client, max_age_s=0)
    px, py, pz = block_cell(status.get("pos"))
    for dy in (0, 1, -1):
        for dx, dz in _ADJACENT:
            cand = (px + dx, py + dy, pz + dz)
            if (cand[0] == px and cand[2] == pz and abs(cand[1] - py) <= 1) or bare(_observed_block(ex, cand).get("name")) not in AIR:
                continue
            if (is_safe_solid_floor(_observed_block(ex, (cand[0], cand[1] - 1, cand[2])))
                    and (not require_top_air or bare(_observed_block(ex, (cand[0], cand[1] + 1, cand[2])).get("name")) in AIR)
                    and can_reach_block(ex, status, cand, reach=INTERACT_EYE_REACH)):
                return cand
    raise EnvBlocked("周围未找到可安全放置营地设施的实心支撑空位", missing_predicate="camp_place_spot", searched_volume="角色周围 1 格实心支撑平面", wake_on="chunk_moved")


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
        table_pos = _find_place_spot(ex, state, require_top_air=False)
        yield from place_block(ex, state, "crafting_table", table_pos)
        memo_for(state).record_station("crafting_table", table_pos, dimension=dim)
    if "chest" in RECIPES:
        yield from craft_recipe(ex, state, "chest", goal_qty=1)
        return
    before = state.count("chest")
    yield rec("craft", ex.craft("chest", 1), item="chest", count=1)
    refresh_inventory(ex, state)
    if state.count("chest") <= before:
        raise Replan("合成 chest 后背包未确认产出")


def _open_camp_chest(ex: "Executor", state: "State", dim: str, *, allow_create: bool, skill_name: str, open_window: bool = True):
    status, memo = state.refresh_status(ex.client, max_age_s=0), memo_for(state)
    camp, chest_pos = memo.get_camp(dimension=dim), None
    if isinstance(camp, Mapping) and camp.get("chest"):
        cpos = block_cell(camp["chest"])
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
            chest_pos = _find_place_spot(ex, state, require_top_air=True)
            yield from place_block(ex, state, "chest", chest_pos)
            memo.record_station("chest", chest_pos, dimension=dim)
    if not open_window:
        return chest_pos, None, None
    status = state.refresh_status(ex.client, max_age_s=0)
    if not can_reach_block(ex, status, chest_pos, reach=INTERACT_EYE_REACH):
        yield rec("move_to", ex.move_to(pos=list(chest_pos), rng=2), pos=list(chest_pos))
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


@register
class ManageCampChest(Skill):
    name, goal, verified, cooldown_s = "manage_camp_chest", "camp:{mode}", "offline", 10.0
    description = "在营地建箱存富余物资、重生开箱重装或受控捡尸"
    params = {"mode": Param(str, "营地操作模式", default="deposit", choices=("deposit", "withdraw", "setup", "recover_corpse"))}

    @classmethod
    def blocked(cls, state: "State") -> str:
        st = _state_snap(state)
        if st.get("ready") is False:
            return "世界加载尚未就绪"
        if st.get("alive") is False:
            return "角色未存活，无法操作营地箱子"
        dim, memo = _state_dim(state), memo_for(state)
        camp = memo.get_camp(dimension=dim)
        has_camp_chest = (isinstance(camp, Mapping) and bool(camp.get("chest"))) or memo.has_station("chest", dimension=dim)
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
        if mode == "deposit":
            return bool(compute_deposit_plan(state)) and ((isinstance(camp, Mapping) and bool(camp.get("chest"))) or _has_chest_or_materials(state))
        if mode == "withdraw":
            if not ((isinstance(camp, Mapping) and camp.get("chest")) or memo.has_station("chest", dimension=dim)):
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

    def run(self, ex: "Executor", state: "State"):
        self._op_result: Optional[dict[str, Any]] = None
        mode, status = self.args["mode"], state.refresh_status(ex.client, max_age_s=0)
        if status.get("ready") is False or status.get("alive") is False:
            raise Replan("世界未就绪或角色未存活，无法执行营地操作")
        dim, memo = bare(status.get("dimension") or "overworld").strip() or "overworld", memo_for(state)
        if mode == "setup":
            chest_pos, _, _ = yield from _open_camp_chest(ex, state, dim, allow_create=True, skill_name=self.name, open_window=False)
            _sync_camp_memo(memo, dim, chest_pos)
            self._op_result = {"mode": "setup", "chest": list(chest_pos)}
            return
        if mode == "recover_corpse":
            ok, reason = memo.should_attempt_corpse_recovery(status.get("pos"), dimension=dim)
            if not ok:
                raise Replan(reason or "当前不满足受控捡尸条件")
            last_death = memo.get_last_death(dimension=dim) if hasattr(memo, "get_last_death") else status.get("last_death")
            if not isinstance(last_death, Mapping) or not last_death.get("pos"):
                memo.clear_last_death(dimension=dim)
                raise Replan("缺少有效死亡点坐标，无法捡尸")
            death_pos = point3(last_death["pos"], label="死亡点坐标")
            yield rec("move_to", ex.move_to(pos=list(death_pos), rng=2), pos=list(death_pos))
            after = state.refresh_status(ex.client, max_age_s=0)
            if after.get("ready") is False or after.get("alive") is False:
                raise Replan("前往死亡点途中角色死亡或世界未就绪")
            refresh_inventory(ex, state)
            if (scan := scan_entities(ex, state, "item", radius=16, limit=40)).get("ready") is True:
                lost = {bare(x) for x in (last_death.get("lost_items") or last_death.get("items") or ()) if bare(x)}
                seen = list(dict.fromkeys(n for ent in scan.get("entities") or () if (n := _drop_name(ent))))
                for drop_item in [n for n in seen if n in lost] + [n for n in seen if n not in lost]:
                    try:
                        yield from collect_drops(ex, state, drop_item, count=1, radius=12, max_targets=3)
                    except (EnvBlocked, Replan):
                        continue
            memo.clear_last_death(dimension=dim)
            self._op_result = {"mode": "recover_corpse", "death_pos": list(death_pos)}
            return
        refresh_inventory(ex, state)
        allow_create = mode == "deposit"
        if allow_create and not compute_deposit_plan(state):
            raise Replan("当前背包没有需要存入营地箱子的富余物资")
        chest_pos, target, view = yield from _open_camp_chest(ex, state, dim, allow_create=allow_create, skill_name=self.name)
        moved: dict[str, int] = {}
        if allow_create:
            for item, qty in compute_deposit_plan(state):
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
            if not moved:
                raise Replan("未能向营地箱子存入任何计划物资")
        else:
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
        _sync_camp_memo(memo, dim, chest_pos, stored_items=_aggregate_container(view))
        self._op_result = {"mode": mode, "chest": list(chest_pos), ("deposited" if allow_create else "withdrawn"): moved}

    def verify_done(self, state: "State", before_counts: Optional[dict[str, int]] = None) -> str:
        mode, dim, memo = self.args["mode"], _state_dim(state), memo_for(state)
        camp, res = memo.get_camp(dimension=dim), getattr(self, "_op_result", None)
        if mode == "recover_corpse":
            if not isinstance(res, Mapping) or res.get("mode") != "recover_corpse":
                return "尚未完成受控捡尸流程"
            return "捡尸结束后未清除死亡记录" if (hasattr(memo, "get_last_death") and memo.get_last_death(dimension=dim)) else ""
        if not isinstance(camp, Mapping) or not camp.get("chest"):
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
