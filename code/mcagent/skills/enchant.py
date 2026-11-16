"""一次附魔及 P6 共用的近场工作站、材料计划。"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from ..runtime import EnvBlocked, Replan
from .base import Skill, failure, rec
from .encounter import _completed, _retreat_clear
from .field import _cell, _grounded, _observed_block, _standable_cell, move_local, place_block, scan_blocks
from .items import bare
from .recipes import RECIPES, craft_recipe
from .registry import register
from .sightline import STATION_EYE_REACH, can_reach_block
from .windows import (
    _receipt_inventory_full, apply_window_ops, take_container_item, window_section, window_view,
)


_ADVANCEMENT = "minecraft:story/enchant_item"
_STATION = "enchanting_table"
_ADJACENT = ((1, 0), (-1, 0), (0, 1), (0, -1))
_NEAR_RADIUS = 8
_NEAR_LIMIT = 5
_REACH = STATION_EYE_REACH
_ENCHANTABLE = frozenset({"bow", "crossbow", "fishing_rod", "trident", "mace"})
_GEAR_SUFFIXES = ("_sword", "_pickaxe", "_axe", "_shovel", "_hoe", "_helmet", "_chestplate", "_leggings", "_boots")


@dataclass(frozen=True)
class _CraftPlan:
    steps: tuple[tuple[str, int], ...]
    consumed: tuple[tuple[str, int, str], ...]
    missing: tuple[tuple[str, int], ...]

    def run(self, ex, state):
        if self.missing:
            details = "、".join(f"{item} ×{count}" for item, count in self.missing)
            raise Replan(f"请补齐本次配方缺少的材料：{details}")
        for item, quantity in self.steps:
            yield from craft_recipe(ex, state, item, goal_qty=quantity)


def _craft_plan(state, product_counts, craftable):
    """基于 P0 配方扣减已有中间件，只计划实际缺额。仅用于无标签的 P6 配方。"""
    consumed, missing, produced = Counter(), Counter(), Counter()
    steps = []

    def reserve(item, quantity):
        from_recipe = min(quantity, produced[item])
        produced[item] -= from_recipe
        quantity -= from_recipe
        on_hand = min(quantity, max(0, state.count(item) - consumed[item]))
        consumed[item] += on_hand
        short = quantity - on_hand
        if short == 0:
            return
        if item not in craftable:
            missing[item] += short
            return
        recipe = RECIPES[item]
        batches = math.ceil(short / recipe.output)
        for ingredient, units in recipe.ingredients:
            reserve(ingredient, units * batches)
        steps.append((item, on_hand + from_recipe + short))
        produced[item] += recipe.output * batches - short

    for item, quantity in product_counts.items():
        reserve(item, quantity)
    return _CraftPlan(tuple(steps), tuple((item, count, "consume") for item, count in consumed.items() if count),
                      tuple(missing.items()))


def _site_state(ex, state):
    status = state.refresh_status(ex.client, max_age_s=0)
    if status.get("ready") is False:
        raise Replan("世界区块和实体加载尚未就绪（not_ready），请等 ready=true 后再试")
    if status.get("jumping") is True or not status.get("dimension") or not _grounded(ex, status):
        raise Replan("当前维度或干燥静止落脚没有确认，请先回到安全站位")
    return status


def _face_reachable(ex, source, pos):
    return can_reach_block(ex, source, pos, reach=_REACH, face_fallback=False)


def _nearby_blocks(ex, state, names):
    """§3.3–3.4 / §4.1：选择当前或有已观察平坦短路的位置。"""
    status = _site_state(ex, state)
    here = _cell(status["pos"])
    candidates = scan_blocks(ex, state, names, radius=_NEAR_RADIUS, limit=_NEAR_LIMIT)
    if candidates.get("ready") is False:
        raise Replan("世界区块加载尚未就绪（not_ready），暂无法扫描近场工作站")
    direct, nearby = [], []
    for block in candidates["blocks"]:
        pos = tuple(block["pos"])
        if _face_reachable(ex, status, pos):
            direct.append((pos, block["name"], None))
            continue
        for dx, dz in _ADJACENT:
            stand = (pos[0] + dx, here[1], pos[2] + dz)
            feet = (stand[0] + 0.5, stand[1], stand[2] + 0.5)
            candidate_source = {"pos": feet, "sneaking": status.get("sneaking")}
            if _face_reachable(ex, candidate_source, pos) and _retreat_clear(ex, status["pos"], feet):
                nearby.append((pos, block["name"], feet))
                break
    return direct + nearby


def _approach_block(ex, state, candidate):
    pos, name, stand = candidate
    if stand is not None:
        yield from move_local(ex, state, stand, max_distance=6)
    status = _site_state(ex, state)
    if bare(_observed_block(ex, pos).get("name")) != name or not _face_reachable(ex, status, pos):
        raise Replan(f"到场后未复核到可触达的 {name}，请换一处安全站位")
    return list(pos)


def _placement_sites(ex, state):
    status = _site_state(ex, state)
    x, y, z = _cell(status["pos"])
    sites = [(x + dx, y, z + dz) for dx, dz in _ADJACENT if _standable_cell(ex, (x + dx, y, z + dz))]
    return sites if len(sites) >= 2 else []


def _place_checked(ex, state, item, pos):
    state.invalidate_inventory()
    state.refresh_inventory(ex.client)
    before = state.count(item)
    yield from place_block(ex, state, item, pos)
    state.refresh_inventory(ex.client)
    if state.count(item) != before - 1:
        raise Replan(f"放置 {item} 后，背包未确认扣除一件")
    return (yield from _approach_block(ex, state, (pos, item, None)))


def _is_enchanted(entry):
    return any(isinstance(ench, dict) and bare(ench.get("name"))
               and type(ench.get("level")) is int and ench["level"] > 0
               for ench in (entry.get("enchantments") or ()))


def _inventory_rows(inventory):
    armor = inventory.get("armor") or {}
    return [row for row in (*(inventory.get("items") or ()), inventory.get("held"),
                            inventory.get("offhand"), *armor.values()) if isinstance(row, dict)]


def _enchantable(inventory):
    for row in inventory.get("items") or ():
        name = bare(row.get("name"))
        if (name in _ENCHANTABLE or name.endswith(_GEAR_SUFFIXES)) and not _is_enchanted(row):
            return name
    return "book" if any(bare(row.get("name")) == "book" for row in inventory.get("items") or ()) else None


def _enchanted_in_bag(inventory, item=None):
    return any((item is None or bare(row.get("name")) == item) and _is_enchanted(row)
               for row in _inventory_rows(inventory))


def _option(view, xp_level, max_lapis):
    """§4.9：附魔台 options 含 index、level_required、lapis_cost、available（兼容旧字段 level_cost/enabled）。"""
    top_options = view.get("options")
    options = top_options if isinstance(top_options, list) else (view.get("info") or {}).get("options") or []
    eligible = []
    for row in options:
        if not isinstance(row, dict):
            continue
        if row.get("available") is False or row.get("enabled") is False:
            continue
        index = row.get("index")
        required = row.get("level_required")
        if required is None:
            required = row.get("level_cost")
        cost = row.get("lapis_cost")
        if not all(type(n) is int for n in (index, required, cost)):
            continue
        if 0 <= index < 3 and 1 <= cost <= min(3, max_lapis) and cost <= required <= xp_level:
            eligible.append((cost, required, index))
    return min(eligible, default=None)


def _enchant_sections(view):
    sections = view.get("sections") or {}
    if bare(view.get("window")) not in ("enchantment", "enchanting_table", "enchanting") or any(
            not isinstance(sections.get(name), list) for name in ("item", "lapis")):
        raise Replan("附魔窗口缺少可核实的 item/lapis 分区，需核对 §4.9 enchantment 快照")


def _enchanted_slot(view, item):
    for section in ("item", "output"):
        if any(bare(row.get("name")) == item and _is_enchanted(row) for row in window_section(view, section)):
            return section
    return None


def _enchant_batch(item, lapis_count, option_index):
    return [
        {"op": "put", "item": item, "count": 1, "to": "item"},
        {"op": "put", "item": "lapis_lazuli", "count": lapis_count, "to": "lapis"},
        {"op": "select", "option": option_index},
        {"op": "take", "from": "item"},
    ]


def _enchant_once(ex, state, pos, item):
    target = {"pos": pos}
    initial = yield from window_view(ex, target)
    _enchant_sections(initial)
    if any(window_section(initial, section) for section in ("item", "lapis", "output")):
        raise Replan("现场附魔窗口已有物品，请先核对并收回原有物品")

    xp_level = state.refresh_status(ex.client, max_age_s=0).get("xp_level", 0)
    inventory = state.refresh_inventory(ex.client)
    if state.count(item) < 1:
        raise Replan(f"附魔前背包缺少待附魔物品 {item}")
    budget = min(3, xp_level, state.count("lapis_lazuli"))
    if budget < 1:
        raise Replan("附魔前经验或青金石已不足")
    if item == "book" and state.count("book") > 1 and inventory.get("empty_slots") == 0:
        raise Replan("背包已满（inventory_full 风险），多本普通书附魔产出附魔书需至少 1 个空位")

    # §4.9：界面不跨调用保持打开，附魔选项在放入物品后才生成，put/select/take 须写在同一次 act 里。
    quoted = _option(initial, xp_level, budget)
    cost, _, index = quoted if quoted is not None else (1, 1, 0)
    result_item = "enchanted_book" if item == "book" else item
    before_lapis = state.count("lapis_lazuli")

    try:
        receipt = yield from apply_window_ops(ex, state, target, _enchant_batch(item, cost, index))
    except Replan as exc:
        code, extra = failure(exc)
        if code == "inventory_full":
            state.refresh_inventory(ex.client)
            raise Replan("附魔后取回物品时背包已满（inventory_full），请先腾出空位") from exc
        err_window = extra.get("window")
        if code == "option_unavailable" and quoted is None and isinstance(err_window, dict):
            xp_level = state.refresh_status(ex.client, max_age_s=0).get("xp_level", 0)
            state.refresh_inventory(ex.client)
            budget = min(3, xp_level, state.count("lapis_lazuli"))
            retry = _option(err_window, xp_level, budget)
            if retry is not None and retry[2] != index and state.count(item) >= 1:
                cost, _, index = retry
                before_lapis = state.count("lapis_lazuli")
                try:
                    receipt = yield from apply_window_ops(ex, state, target, _enchant_batch(item, cost, index))
                except Replan as retry_exc:
                    if failure(retry_exc)[0] == "inventory_full":
                        state.refresh_inventory(ex.client)
                        raise Replan("附魔后取回物品时背包已满（inventory_full），请先腾出空位") from retry_exc
                    raise
            else:
                raise Replan("附魔台无满足当前经验等级与青金石预算的可用选项（option_unavailable）") from exc
        else:
            raise

    after_inv = state.refresh_inventory(ex.client)
    if _receipt_inventory_full(receipt) and not _enchanted_in_bag(after_inv, result_item):
        raise Replan("附魔后取回物品时背包已满（inventory_full），请先腾出空位")
    if not _enchanted_in_bag(after_inv, result_item):
        end_window = receipt.get("window")
        view = end_window if isinstance(end_window, dict) and isinstance(end_window.get("sections"), dict) else None
        if view is not None and (section := _enchanted_slot(view, result_item)):
            yield from take_container_item(ex, state, target, section, result_item, 1, view=view)
            after_inv = state.refresh_inventory(ex.client)
    if before_lapis - state.count("lapis_lazuli") < cost or not _enchanted_in_bag(after_inv, result_item):
        raise Replan("附魔 select/take 后，背包未确认青金石扣除或实际附魔字段")
    yield rec("enchantment_evidence", None, item=result_item, option=index, quoted_lapis=cost,
              advancement_confirmed=_completed(ex, _ADVANCEMENT))


def _enchant_crafts(state, item):
    requirements = {_STATION: 1}
    if item == "book":
        requirements["book"] = 1
    return _craft_plan(state, requirements, {_STATION, "book", "paper"})


@register
class EnchantItem(Skill):
    name = "enchant_item"
    description = "复用或制作附魔台，选择可支付的附魔并在背包复核结果"
    goal = "story:enchant_item"
    verified = "offline"

    @classmethod
    def blocked(cls, state):
        if _enchanted_in_bag(state.inventory):
            return ""
        if not _enchantable(state.inventory):
            return "缺少未附魔的书或装备"
        if state.count("lapis_lazuli") < 1:
            return "缺少青金石"
        if (state.snapshot().get("xp_level") or 0) < 1:
            return "缺少附魔经验等级"
        return ""

    @classmethod
    def material_plan(cls, args, state) -> tuple[tuple[str, int, str], ...]:
        item = _enchantable(state.inventory)
        if not item or _enchanted_in_bag(state.inventory) or cls.blocked(state):
            return ()
        lapis = min(3, state.count("lapis_lazuli"), state.snapshot()["xp_level"])
        result = ((item, 1, "consume" if item == "book" else "borrow"), ("lapis_lazuli", lapis, "consume"))
        craft = _enchant_crafts(state, item)
        if craft.missing:
            return result
        # 计划需要一件作为附魔输入；配方结果已包含原有普通书的同一份预留。
        consumables = tuple(entry for entry in result if entry[0] != "book")
        return consumables + craft.consumed

    def run(self, ex, state):
        if _completed(ex, _ADVANCEMENT):
            return
        state.invalidate_inventory()
        inventory = state.refresh_inventory(ex.client)
        if _enchanted_in_bag(inventory):
            yield rec("enchantment_evidence", None, held_product=True, advancement_confirmed=False)
            return
        _site_state(ex, state)
        if reason := self.blocked(state):
            raise Replan(reason)
        item = _enchantable(inventory)
        candidates = _nearby_blocks(ex, state, (_STATION,))
        if candidates:
            pos = yield from _approach_block(ex, state, candidates[0])
        else:
            craft = _enchant_crafts(state, item)
            sites = _placement_sites(ex, state)
            if craft.missing or not sites:
                raise EnvBlocked("8 格内未确认可达附魔台，需安全落点和足够配方材料",
                                 missing_predicate="station_ready:enchanting_table",
                                 searched_volume="半径 8 格 5 个附魔台候选及四邻落点", wake_on="chunk_moved")
            yield from craft.run(ex, state)
            # 合成可能走向工作台，放置候选须重新观察。
            sites = _placement_sites(ex, state)
            if not sites:
                raise Replan("附魔台制作后需找到承重和可撤的落点")
            pos = yield from _place_checked(ex, state, _STATION, sites[0])
        yield from _enchant_once(ex, state, pos, item)
