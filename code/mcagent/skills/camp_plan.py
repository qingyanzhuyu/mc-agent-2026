"""营地三区仓库配额、分类、存/取计划与预留保护纯逻辑（与世界 I/O 解耦）。"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
import math
from typing import TYPE_CHECKING, Any, Optional

from ..runtime import Replan
from .items import PICKAXE_ORDER, STONE_OR_BETTER_PICKS, bare
from .recipes import RECIPES
from .sightline import STATION_EYE_REACH, point3
from .world_memo import ALL_LOG_BLOCKS, memo_for

if TYPE_CHECKING:
    from ..loop.reserve import ReservationBook
    from ..runtime import State


def _reserved_item_counts(state: "State", reservations: Optional["ReservationBook"]) -> Mapping[str, int]:
    from ..loop.reserve import reserved_item_counts
    return reserved_item_counts(state, reservations)

INTERACT_EYE_REACH: float = STATION_EYE_REACH
SWORD_ORDER = ("netherite_sword", "diamond_sword", "iron_sword", "copper_sword", "stone_sword", "golden_sword", "wooden_sword")
STONE_OR_BETTER_SWORDS = frozenset({"stone_sword", "copper_sword", "iron_sword", "diamond_sword", "netherite_sword"})
ARMOR_SLOTS = ("helmet", "chestplate", "leggings", "boots")
ARMOR_EQUIP_SLOT: dict[str, str] = {"helmet": "head", "chestplate": "torso", "leggings": "legs", "boots": "feet"}
ARMOR_BY_SLOT: dict[str, tuple[str, ...]] = {
    "helmet": ("netherite_helmet", "diamond_helmet", "iron_helmet", "turtle_helmet", "copper_helmet", "chainmail_helmet", "golden_helmet", "leather_helmet"),
    **{s: tuple(f"{t}_{s}" for t in ("netherite", "diamond", "iron", "copper", "chainmail", "golden", "leather")) for s in ARMOR_SLOTS[1:]},
}
COOKED_FOOD_ORDER = ("golden_carrot", "cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_salmon", "cooked_chicken", "cooked_cod", "cooked_rabbit", "baked_potato", "bread")
EDIBLE_FOODS = frozenset(COOKED_FOOD_ORDER) | frozenset({
    "apple", "carrot", "melon_slice", "sweet_berries", "glow_berries", "pumpkin_pie", "cookie",
    "golden_apple", "enchanted_golden_apple", "beetroot", "beetroot_soup", "mushroom_stew",
    "rabbit_stew", "honey_bottle", "dried_kelp",
})
PLANK_ITEMS = tuple(item for item in RECIPES if item.endswith("_planks"))
HIGH_VALUE_ITEMS = frozenset({
    "diamond", "obsidian", "iron_ingot", "raw_iron", "gold_ingot", "raw_gold", "shield",
    "blaze_rod", "ender_pearl", "ender_eye", "totem_of_undying", "wither_skeleton_skull",
    "ancient_debris", "netherite_scrap", "netherite_ingot", "netherite_upgrade_smithing_template",
    "emerald", "lapis_lazuli", "redstone", "crying_obsidian", "enchanted_book", "nether_star",
})
VAULT_ITEMS = HIGH_VALUE_ITEMS - {"shield"}
GEAR_SUFFIX = ("_pickaxe", "_sword", "_helmet", "_chestplate", "_leggings", "_boots")
BULK_QUOTAS: tuple[tuple[str, int], ...] = (
    ("cobblestone", 64), ("cobbled_deepslate", 64), ("dirt", 32), ("gravel", 16), ("sand", 16),
    ("coal", 8), ("charcoal", 8), ("torch", 16), ("string", 8), ("bone", 8), ("leather", 8),
)
VAULT_QUOTAS: tuple[tuple[str, int], ...] = (
    ("iron_ingot", 4), ("diamond", 0), ("obsidian", 0), ("raw_iron", 6), ("gold_ingot", 0),
    ("blaze_rod", 0), ("ender_pearl", 2), ("totem_of_undying", 1), ("wither_skeleton_skull", 0),
    ("ancient_debris", 0), ("netherite_scrap", 0), ("netherite_ingot", 0),
    ("netherite_upgrade_smithing_template", 0), ("lapis_lazuli", 0), ("redstone", 0), ("emerald", 0),
)


def state_snap(state: Any) -> Mapping[str, Any]:
    snap = state.snapshot() if hasattr(state, "snapshot") else getattr(state, "st", None)
    return snap if isinstance(snap, Mapping) else {}


def state_dim(state: Any) -> str:
    return bare(state_snap(state).get("dimension") or "overworld").strip() or "overworld"


def bag_count(state: "State", item: str) -> int:
    target = bare(item)
    return sum(int(r.get("count", 0) or 0) for r in ((getattr(state, "inventory", None) or {}).get("items") or ())
               if isinstance(r, Mapping) and bare(r.get("name")) == target)


def empty_slot_count(state: "State") -> Optional[int]:
    inv = getattr(state, "inventory", None)
    if not isinstance(inv, Mapping):
        return None
    val = inv.get("empty_slots")
    if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
        return val
    return None


def classify_chest_role(item: str) -> str:
    """按 docs/15 §四 将物品归入 `kit`（应急热备）/ `vault`（战略金库）/ `bulk`（建材杂物）。"""
    name = bare(item).strip().lower()
    if not name:
        return "bulk"
    if name in VAULT_ITEMS:
        return "vault"
    if name == "shield" or name == "turtle_helmet" or name in COOKED_FOOD_ORDER or name in ("water_bucket", "torch") or name.endswith(GEAR_SUFFIX):
        return "kit"
    return "bulk"


def armor_equip_slot_for(item: str) -> Optional[str]:
    """返回护甲/盾牌对应的 `ex.equip` 槽位名（`head`/`torso`/`legs`/`feet`/`off-hand`）。"""
    name = bare(item).strip().lower()
    if name == "shield":
        return "off-hand"
    for suffix, slot in ARMOR_EQUIP_SLOT.items():
        if name.endswith(f"_{suffix}"):
            return slot
    return None


def _excess_best_one(
    state: "State", order: Sequence[str], eligible: Optional[Collection[str]] = None, *, reserved: Mapping[str, int] = (),
) -> list[tuple[str, int]]:
    best = next((it for it in order if state.count(it) >= 1), None)
    out: list[tuple[str, int]] = []
    for it in order:
        if eligible is not None and it not in eligible:
            continue
        keep = (1 if it == best else 0) + int(reserved.get(it, 0))
        if (ex_qty := state.count(it) - keep) > 0:
            out.append((it, ex_qty))
    return out


def compute_deposit_plan(state: "State", *, reservations: Optional["ReservationBook"] = None, include_bulk: Optional[bool] = None) -> list[tuple[str, int]]:
    """按随身配额计算应存入营地箱的富余物资清单 `[(item, qty), ...]`，严格尊重 `ReservationBook` 预留。"""
    reserved = _reserved_item_counts(state, reservations)
    plan = _excess_best_one(state, PICKAXE_ORDER, STONE_OR_BETTER_PICKS, reserved=reserved)
    plan.extend(_excess_best_one(state, SWORD_ORDER, STONE_OR_BETTER_SWORDS, reserved=reserved))
    if (shields := state.count("shield") - (1 + reserved.get("shield", 0))) > 0:
        plan.append(("shield", shields))
    for slot in ARMOR_SLOTS:
        plan.extend(_excess_best_one(state, ARMOR_BY_SLOT[slot], reserved=reserved))
    keep_food = 12
    for food in COOKED_FOOD_ORDER:
        if (cnt := state.count(food)) > 0:
            kept = min(cnt, keep_food)
            keep_food -= kept
            avail = cnt - kept - reserved.get(food, 0)
            if avail > 0:
                plan.append((food, avail))
    for item, quota in VAULT_QUOTAS:
        avail = state.count(item) - quota - reserved.get(item, 0)
        if avail > 0:
            plan.append((item, avail))
    es = empty_slot_count(state)
    want_bulk = (es is not None and es <= 4) if include_bulk is None else bool(include_bulk)
    if want_bulk:
        for item, quota in BULK_QUOTAS:
            avail = state.count(item) - quota - reserved.get(item, 0)
            if avail > 0:
                plan.append((item, avail))
        logs_keep = 8
        for log in ALL_LOG_BLOCKS:
            if (cnt := state.count(log)) > 0:
                kept = min(cnt, logs_keep)
                logs_keep -= kept
                avail = cnt - kept - reserved.get(log, 0)
                if avail > 0:
                    plan.append((log, avail))
    return plan


def has_surplus_for_deposit(state: "State", *, reservations: Optional["ReservationBook"] = None) -> bool:
    """判断当前是否携带足够触发营地存箱的未预留高价值/满包/富余物资。"""
    reserved = _reserved_item_counts(state, reservations)
    unres = lambda it: max(0, state.count(it) - reserved.get(it, 0))
    if (
        unres("iron_ingot") >= 8
        or unres("raw_iron") >= 12
        or unres("diamond") >= 2
        or unres("obsidian") >= 4
        or unres("blaze_rod") >= 2
        or unres("ender_pearl") >= 4
        or unres("totem_of_undying") >= 2
    ):
        return True
    es = empty_slot_count(state)
    plan = compute_deposit_plan(state, reservations=reservations)
    if es is not None and es <= 4 and bool(plan):
        return True
    return sum(q for _, q in plan) >= 4 and any(bare(i) in HIGH_VALUE_ITEMS or bare(i).endswith(GEAR_SUFFIX) for i, _ in plan)


def _take_if_missing(state: "State", stored: Mapping[str, int], order: Sequence[str], owned: Optional[Collection[str]] = None) -> Optional[str]:
    if any(state.count(x) >= 1 for x in (owned or order)):
        return None
    pick = next((x for x in order if (owned is None or x in owned) and stored.get(x, 0) >= 1), None)
    if pick is not None or owned is None or any(state.count(x) >= 1 for x in order):
        return pick
    return next((x for x in order if stored.get(x, 0) >= 1), None)


def compute_withdraw_plan(state: "State", stored_items: Mapping[str, int]) -> list[tuple[str, int]]:
    """根据身上缺口与箱内已知存货计算应取出的补给清单（成品优先，缺成品时取基础料室内快造）。"""
    stored: dict[str, int] = {}
    for raw_k, raw_v in (stored_items or {}).items():
        if (name := bare(raw_k)) and isinstance(raw_v, int) and not isinstance(raw_v, bool) and raw_v > 0:
            stored[name] = stored.get(name, 0) + raw_v
    slots = ((PICKAXE_ORDER, STONE_OR_BETTER_PICKS), (SWORD_ORDER, None), (("shield",), None), *((ARMOR_BY_SLOT[s], None) for s in ARMOR_SLOTS))
    plan = [(item, 1) for order, owned in slots if (item := _take_if_missing(state, stored, order, owned))]
    if sum(state.count(f) for f in EDIBLE_FOODS) < 6:
        need = max(0, 8 - sum(state.count(f) for f in COOKED_FOOD_ORDER))
        for food in COOKED_FOOD_ORDER:
            if need <= 0:
                break
            if (avail := stored.get(food, 0)) > 0:
                take = min(avail, need)
                plan.append((food, take))
                need -= take

    planned_names = {it for it, _ in plan}
    has_pick = any(state.count(p) >= 1 or p in planned_names for p in STONE_OR_BETTER_PICKS)
    has_sword = any(state.count(s) >= 1 or s in planned_names for s in SWORD_ORDER)
    has_armor = any(state.count(a) >= 1 or a in planned_names for s in ARMOR_SLOTS for a in ARMOR_BY_SLOT[s])
    if not (has_pick and has_sword and has_armor):
        if (need_iron := max(0, 6 - state.count("iron_ingot"))) > 0 and stored.get("iron_ingot", 0) > 0:
            plan.append(("iron_ingot", min(need_iron, stored["iron_ingot"])))
        elif (need_raw := max(0, 6 - state.count("iron_ingot") - state.count("raw_iron"))) > 0 and stored.get("raw_iron", 0) > 0:
            plan.append(("raw_iron", min(need_raw, stored["raw_iron"])))
        if (not has_pick or not has_sword) and (need_cob := max(0, 12 - state.count("cobblestone"))) > 0 and stored.get("cobblestone", 0) > 0:
            plan.append(("cobblestone", min(need_cob, stored["cobblestone"])))
        cur_planks = sum(state.count(p) for p in PLANK_ITEMS)
        if (need_planks := max(0, 8 - cur_planks)) > 0:
            for pitem in PLANK_ITEMS:
                if need_planks <= 0:
                    break
                if (avail := stored.get(pitem, 0)) > 0:
                    take = min(avail, need_planks)
                    plan.append((pitem, take))
                    need_planks -= take
        if (need_coal := max(0, 4 - state.count("coal") - state.count("charcoal"))) > 0 and stored.get("coal", 0) > 0:
            plan.append(("coal", min(need_coal, stored["coal"])))
    return plan


def needs_camp_withdraw(state: "State") -> bool:
    """检查当前维度营地箱记录的存货是否能补齐角色当前装备/食物/基础料缺口。"""
    camp = memo_for(state).get_camp(dimension=state_dim(state))
    has_any_chest = isinstance(camp, Mapping) and (bool(camp.get("chest")) or bool(camp.get("chests")))
    stored = camp.get("stored_items") if has_any_chest else None
    return isinstance(stored, Mapping) and bool(stored) and bool(compute_withdraw_plan(state, stored))


def is_near_camp(state: "State", max_dist: float = 48.0) -> bool:
    """判断角色当前是否位于本维度营地 `max_dist` 格欧氏距离内。"""
    camp = memo_for(state).get_camp(dimension=state_dim(state))
    target = camp.get("pos") or camp.get("chest") if isinstance(camp, Mapping) else None
    raw_pos = state_snap(state).get("pos")
    if target is None or raw_pos is None:
        return False
    try:
        return math.dist(point3(raw_pos, label="当前坐标"), point3(target, label="营地坐标")) <= max_dist
    except Replan:
        return False


def has_chest_or_materials(state: "State") -> bool:
    return state.count("chest") >= 1 or sum(state.count(p) for p in PLANK_ITEMS) + sum(state.count(l) for l in ALL_LOG_BLOCKS) * 4 >= 8


def is_protected_cell(pos: Sequence[int]) -> bool:
    try:
        from ..loop.starter_skills import _is_protected
        return bool(_is_protected(pos, radius=6.0))
    except Exception:
        return False


def record_protected_cell(pos: Sequence[int], memo: Any, dim: str) -> None:
    try:
        from ..loop.starter_skills import _mark_protected
        _mark_protected(pos)
    except Exception:
        pass
    try:
        memo.mark_protected(pos, dimension=dim)
    except Exception:
        pass


def adjacent_to_any_chest(cand: tuple[int, int, int], existing: Sequence[tuple[int, int, int]]) -> bool:
    cx, cy, cz = cand
    return any(ey == cy and abs(ex - cx) + abs(ez - cz) <= 1 for ex, ey, ez in existing)


def aggregate_container(view: Mapping[str, Any]) -> dict[str, int]:
    from .windows import window_section
    counts: dict[str, int] = {}
    for entry in window_section(dict(view), "container"):
        if isinstance(entry, Mapping) and (name := bare(entry.get("name"))) and (qty := int(entry.get("count", 0) or 0)) > 0:
            counts[name] = counts.get(name, 0) + qty
    return counts


def sync_camp_memo(
    memo: Any, dim: str, chest_cell: tuple[int, int, int], stored_items: Optional[dict[str, int]] = None,
    *, role: Optional[str] = None, shelter_tier: Optional[int] = None, kit_depleted: Optional[bool] = None,
) -> None:
    memo.record_station("chest", chest_cell, dimension=dim)
    camp = memo.get_camp(dimension=dim)
    camp_pos = list(camp["pos"]) if isinstance(camp, Mapping) and camp.get("pos") else list(chest_cell)
    stations = {k: list(min(near, key=lambda c: math.dist(chest_cell, c)))
                for k in ("bed", "furnace", "crafting_table")
                if (near := [c for c in memo.stations(k, dimension=dim) if math.dist(chest_cell, c) <= 16.0])}
    memo.set_camp(camp_pos, dimension=dim, chest=list(chest_cell), stored_items=stored_items,
                  chest_role=role, shelter_tier=shelter_tier, kit_depleted=kit_depleted, **stations)
    if stored_items is not None:
        memo.update_camp_stored(stored_items, dimension=dim, role=role)
