"""T1 单配方原语。配方来自 Mojang Java 1.21.11 原版服务端 JAR。

发布页 https://www.minecraft.net/en-us/article/minecraft-java-edition-1-21-11
JAR SHA-1 64bb6d763bed0a9f1d632ec347938594144943ed。内嵌
`META-INF/versions/1.21.11/server-1.21.11.jar` 的 `data/minecraft/recipe`
与 `data/minecraft/tags/item` 分别固定产量、原料与可替换材料。
数据保留工作台直制配方，染色床和切石机走各自的专用路线。
"""
from __future__ import annotations

import math
from collections.abc import Generator
from dataclasses import dataclass

from ..client import ApiError
from ..runtime import EnvBlocked, Executor, Replan, State
from .base import failure, rec
from .items import bare, block_name


@dataclass(frozen=True)
class Recipe:
    ingredients: tuple[tuple[str, int], ...]
    output: int = 1
    requires_table: bool = True


_LOG_WOODS = (
    "oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "pale_oak", "mangrove", "cherry",
)
_STEM_WOODS = ("crimson", "warped")
_WOODS = (*_LOG_WOODS, *_STEM_WOODS, "bamboo")
_BED_COLORS = (
    "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
    "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black",
)
_INGREDIENT_TAGS = {
    "#planks": tuple(f"{wood}_planks" for wood in _WOODS),
    "#stone_crafting_materials": ("cobblestone", "blackstone", "cobbled_deepslate"),
    **{
        f"#{wood}_logs": (f"{wood}_log", f"{wood}_wood", f"stripped_{wood}_log", f"stripped_{wood}_wood")
        for wood in _LOG_WOODS
    },
    **{
        f"#{wood}_stems": (f"{wood}_stem", f"{wood}_hyphae", f"stripped_{wood}_stem", f"stripped_{wood}_hyphae")
        for wood in _STEM_WOODS
    },
    "#bamboo_blocks": ("bamboo_block", "stripped_bamboo_block"),
}

# 每个键对应同名原版 recipe JSON。单项标签 iron_tool_materials 和
# diamond_tool_materials 已展开，wooden_tool_materials 引用的标签为 planks。
RECIPES: dict[str, Recipe] = {
    "stick": Recipe((("#planks", 2),), output=4, requires_table=False),
    **{f"{wood}_planks": Recipe(((f"#{wood}_logs", 1),), output=4, requires_table=False) for wood in _LOG_WOODS},
    **{f"{wood}_planks": Recipe(((f"#{wood}_stems", 1),), output=4, requires_table=False) for wood in _STEM_WOODS},
    "bamboo_planks": Recipe((("#bamboo_blocks", 1),), output=2, requires_table=False),
    "crafting_table": Recipe((("#planks", 4),), requires_table=False),
    "furnace": Recipe((("#stone_crafting_materials", 8),)),
    "iron_pickaxe": Recipe((("iron_ingot", 3), ("stick", 2))),
    "iron_helmet": Recipe((("iron_ingot", 5),)),
    "iron_chestplate": Recipe((("iron_ingot", 8),)),
    "iron_leggings": Recipe((("iron_ingot", 7),)),
    "iron_boots": Recipe((("iron_ingot", 4),)),
    "bucket": Recipe((("iron_ingot", 3),)),
    "shield": Recipe((("#planks", 6), ("iron_ingot", 1))),
    "iron_sword": Recipe((("iron_ingot", 2), ("stick", 1))),
    "flint_and_steel": Recipe((("iron_ingot", 1), ("flint", 1)), requires_table=False),
    "shears": Recipe((("iron_ingot", 2),), requires_table=False),
    "compass": Recipe((("iron_ingot", 4), ("redstone", 1))),
    "diamond_boots": Recipe((("diamond", 4),)),
    "diamond_pickaxe": Recipe((("diamond", 3), ("stick", 2))),
    "wooden_hoe": Recipe((("#planks", 2), ("stick", 2))),
    **{f"{color}_bed": Recipe(((f"{color}_wool", 3), ("#planks", 3))) for color in _BED_COLORS},
    "fishing_rod": Recipe((("stick", 3), ("string", 2))),
    "bow": Recipe((("stick", 3), ("string", 3))),
    "arrow": Recipe((("flint", 1), ("stick", 1), ("feather", 1)), output=4),
    "crossbow": Recipe((("stick", 3), ("iron_ingot", 1), ("string", 2), ("tripwire_hook", 1))),
    "tripwire_hook": Recipe((("iron_ingot", 1), ("stick", 1), ("#planks", 1)), output=2),
    "book": Recipe((("paper", 3), ("leather", 1)), requires_table=False),
    "paper": Recipe((("sugar_cane", 3),), output=3),
    "enchanting_table": Recipe((("book", 1), ("diamond", 2), ("obsidian", 4))),
    "stone_bricks": Recipe((("stone", 4),), output=4, requires_table=False),
    "stone_brick_slab": Recipe((("stone_bricks", 3),), output=6),
    "chiseled_stone_bricks": Recipe((("stone_brick_slab", 2),), requires_table=False),
    "lodestone": Recipe((("chiseled_stone_bricks", 8), ("iron_ingot", 1))),
    "hay_block": Recipe((("wheat", 9),)),
    "target": Recipe((("redstone", 4), ("hay_block", 1))),
}

# stick_from_bamboo_item.json；后端可在两个分支都有材料时自行选配方。
_BAMBOO_STICK = Recipe((("bamboo", 2),), requires_table=False)
_TABLE_RADIUS = 16
_TABLE_LIMIT = 5
_TABLE_REACH = 3.5
_MAX_BATCHES = 64


def _ingredient_items(ingredient: str) -> tuple[str, ...]:
    return _INGREDIENT_TAGS.get(ingredient, (ingredient,))


def _craft_capacity(recipe: Recipe, counts: dict[str, int]) -> int:
    return min(
        sum(counts[name] for name in _ingredient_items(ingredient)) // needed
        for ingredient, needed in recipe.ingredients
    )


def _at_table(ex: Executor, state: State, pos: list[int]) -> bool:
    if math.dist(state.pos, pos) > _TABLE_REACH:
        return False
    try:
        return block_name(ex, pos) == "crafting_table"
    except ApiError as exc:
        if exc.code != "not_loaded":
            raise
        return False


def _table_position(ex: Executor, state: State) -> Generator[dict, None, list[int]]:
    state.refresh_status(ex.client, max_age_s=0)
    observed = ex.client.find_blocks(types=["crafting_table"], radius=_TABLE_RADIUS, limit=_TABLE_LIMIT)
    candidates = [
        block["pos"] for block in (observed.get("blocks") or [])[:_TABLE_LIMIT]
        if isinstance(block.get("pos"), list) and len(block["pos"]) == 3
    ]
    for pos in sorted(candidates, key=lambda pos: math.dist(state.pos, pos)):
        if _at_table(ex, state, pos):
            return pos
        if math.dist(state.pos, pos) <= _TABLE_REACH:
            continue
        dimension = state.snapshot().get("dimension")
        try:
            result = ex.move_to(pos=pos, rng=2)
        except Replan as exc:
            code, _ = failure(exc)
            if code not in {"path_not_found", "unreachable", "timeout", "move_timeout"}:
                raise
            yield rec("move_to", pos=pos, error_code=code)
        else:
            yield rec("move_to", result, pos=pos)
        current = state.refresh_status(ex.client, max_age_s=0)
        if current.get("dimension") != dimension or current.get("alive") is False:
            raise Replan("接近工作台时角色死亡或切换维度，需重新核实")
        if _at_table(ex, state, pos):
            return pos

    raise EnvBlocked(
        "没有找到已核实可达的工作台，请先在安全位置放工作台或换区块找台",
        missing_predicate="station_ready:crafting_table",
        searched_volume=f"半径 {_TABLE_RADIUS} 格内最多 {_TABLE_LIMIT} 个暴露工作台",
        wake_on="chunk_moved",
    )


def craft_recipe(ex: Executor, state: State, item: str, *, goal_qty: int = 1) -> Generator[dict, None, dict]:
    """逐次合成至至少持有 goal_qty 件成品，返回前后库存和实耗差量。"""
    item = bare(item)
    recipe = RECIPES.get(item)
    if recipe is None:
        raise Replan(f"不支持的 T1 合成物品 {item}，请选择已登记的成品 ID")
    if type(goal_qty) is not int or goal_qty < 1:
        raise Replan(f"合成 {item} 的目标数量必须为正整数")

    state.invalidate_inventory()
    inventory = state.refresh_inventory(ex.client)
    before = current = state.count(item)
    gained = 0
    consumed: dict[str, int] = {}
    variants = (recipe, _BAMBOO_STICK) if item == "stick" else (recipe,)
    input_items = tuple(dict.fromkeys(
        name for variant in variants for ingredient, _ in variant.ingredients for name in _ingredient_items(ingredient)
    ))
    table = None
    table_dimension = None
    batches = 0
    while current < goal_qty:
        if batches >= _MAX_BATCHES:
            raise Replan(f"合成 {item} 已用完 {_MAX_BATCHES} 批的本轮预算，请按已实收数量重新规划")
        stock = {name: state.count(name) for name in input_items}
        capacity = {variant: _craft_capacity(variant, stock) for variant in variants}
        # 木板木棍与竹子木棍的原料不重叠，合并后才是完整的生产额度。
        if current + sum(variant.output * batches for variant, batches in capacity.items()) < goal_qty:
            requirements = " 或 ".join(
                "、".join(f"{ingredient} ×{needed}" for ingredient, needed in variant.ingredients)
                for variant in variants
            )
            raise Replan(f"合成 {item} 缺材料：还需 {goal_qty - current} 件成品，每批需要 {requirements}")

        if recipe.requires_table:
            if table is None:
                table = yield from _table_position(ex, state)
                table_dimension = state.snapshot().get("dimension")
                state.invalidate_inventory()
                inventory = state.refresh_inventory(ex.client)
                current = state.count(item)
                continue
            current_status = state.refresh_status(ex.client, max_age_s=0)
            if (current_status.get("dimension") != table_dimension or current_status.get("alive") is False
                    or not _at_table(ex, state, table)):
                raise Replan(f"合成 {item} 的工作台已不在当前可达范围，需重新核实")

        # 服务端按产物 ID 选配方。即使走未建模的替代路线，也记录背包实际净减少的材料。
        bag_before = {
            name: state.count(name)
            for stack in inventory.get("items") or ()
            if isinstance(stack, dict) and (name := stack.get("name"))
        }
        # §4.8 没写 count 的精确单位。统一每次请求 1，再读背包核对一份产出。
        yield rec("craft", ex.craft(item, 1), item=item, count=1)
        batches += 1
        inventory = state.refresh_inventory(ex.client)
        after = state.count(item)
        expected = min(variant.output for variant, batches in capacity.items() if batches > 0)
        if after - current < expected:
            raise Replan(f"合成 {item} 后背包仅增加 {after - current} 件，预期至少 {expected} 件")
        gained += after - current
        for name, previous in bag_before.items():
            used = previous - state.count(name)
            if used > 0:
                consumed[name] = consumed.get(name, 0) + used
        current = after

    return {"item": item, "before": before, "after": current, "gained": gained, "consumed": consumed}
