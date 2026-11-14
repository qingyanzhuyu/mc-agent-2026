"""首批高性价比基础生存 L3 技能（对应 docs/11 §二/§七 T1 底仓前置）。

遵守 AGENTS.md §9.2 全部硬规则：
- 只收标量参数，绝不收坐标与实体 id；
- 每发一个阻塞动作立即 `yield rec(...)`；
- 动作一律经由 `Executor`，绝不直调 `Client` 动作方法；
- 每个技能自带 `verify_done(state, before_counts)` 声明自己的成功判据，供主循环核对；
- 默认 `verified = "offline"`，真机验收留档前不混入正式赛默认菜单。
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Iterator, Optional

from ..client import ApiError
from ..runtime import Replan
from ..skills.base import Param, Skill, failure, rec
from ..skills.items import AIR, bare, best_pickaxe
from ..skills.registry import register

if TYPE_CHECKING:
    from ..runtime import Executor, State

ALL_LOGS: tuple[str, ...] = (
    "oak_log", "birch_log", "spruce_log", "jungle_log",
    "acacia_log", "dark_oak_log", "cherry_log", "mangrove_log",
)
ALL_PLANKS: tuple[str, ...] = (
    "oak_planks", "birch_planks", "spruce_planks", "jungle_planks",
    "acacia_planks", "dark_oak_planks", "cherry_planks", "mangrove_planks",
)
AXE_ORDER: tuple[str, ...] = (
    "netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "golden_axe", "wooden_axe",
)
STONE_Targets: tuple[str, ...] = ("stone", "cobblestone", "deepslate", "cobbled_deepslate")
PEEL_SURFACE: tuple[str, ...] = ("grass_block", "dirt", "coarse_dirt", "podzol")
SAFE_GROUND: tuple[str, ...] = (
    "grass_block", "dirt", "coarse_dirt", "stone", "cobblestone",
    "sand", "sandstone", "gravel", "andesite", "diorite", "granite", "oak_planks",
)
EDIBLE_FOODS: tuple[str, ...] = (
    "cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_chicken", "cooked_cod",
    "cooked_salmon", "bread", "baked_potato", "golden_carrot", "apple", "carrot",
    "melon_slice", "sweet_berries", "glow_berries", "beef", "porkchop", "mutton", "chicken",
)
STRUCTURE_FINGERPRINTS: dict[str, tuple[str, ...]] = {
    "village": ("hay_block", "bell", "composter", "lectern"),
    "stronghold": ("stone_bricks", "mossy_stone_bricks", "cracked_stone_bricks", "end_portal_frame"),
    "ruined_portal": ("crying_obsidian", "obsidian", "gold_block"),
    "bee_nest": ("bee_nest",),
}

# 运行期动态记录触发过 protected_region 的点位与已放置工作台（非硬编码坐标，符合 §2B.1）
PROTECTED_ZONES: list[tuple[float, float]] = []
VISITED_ANCHORS: list[tuple[float, float]] = []
KNOWN_TABLES: list[tuple[float, float, float]] = []


def reset_world_memo() -> None:
    PROTECTED_ZONES.clear()
    VISITED_ANCHORS.clear()
    KNOWN_TABLES.clear()


def _record_known_table(pos) -> None:
    if isinstance(pos, (list, tuple)) and len(pos) >= 3:
        pt = (float(pos[0]), float(pos[1]), float(pos[2]))
        if all(math.dist(pt, old) > 1.0 for old in KNOWN_TABLES):
            KNOWN_TABLES.append(pt)


def _forget_known_table(pos) -> None:
    KNOWN_TABLES[:] = [t for t in KNOWN_TABLES if math.dist(t, pos) > 1.0]


def has_known_table(pos, radius: float = 16.0) -> bool:
    if not (isinstance(pos, (list, tuple)) and len(pos) >= 3):
        return False
    p = (float(pos[0]), float(pos[1]), float(pos[2]))
    return any(math.dist(p, t) <= radius and not _is_protected(t) for t in KNOWN_TABLES)


def _mark_protected(pos) -> None:
    if isinstance(pos, (list, tuple)) and len(pos) >= 3:
        PROTECTED_ZONES.append((float(pos[0]), float(pos[2])))


def _is_protected(pos, radius: float = 6.0) -> bool:
    if not (isinstance(pos, (list, tuple)) and len(pos) >= 3):
        return False
    x, z = float(pos[0]), float(pos[2])
    return any(math.hypot(x - px, z - pz) <= radius for px, pz in PROTECTED_ZONES)


def _is_under_feet(player_pos: list[float], block_pos: list) -> bool:
    fx, fy, fz = math.floor(player_pos[0]), player_pos[1], math.floor(player_pos[2])
    return int(block_pos[0]) == fx and int(block_pos[2]) == fz and float(block_pos[1]) <= fy


def total_logs(state: "State") -> int:
    return sum(state.count(x) for x in ALL_LOGS)


def total_planks(state: "State") -> int:
    return sum(state.count(x) for x in ALL_PLANKS)


def total_cobble(state: "State") -> int:
    return state.count("cobblestone") + state.count("cobbled_deepslate")


def best_axe(state: "State") -> Optional[str]:
    return next((a for a in AXE_ORDER if state.count(a) >= 1), None)


def best_food(state: "State") -> Optional[str]:
    return next((f for f in EDIBLE_FOODS if state.count(f) >= 1), None)


def _planks_for_log(log_name: str) -> str:
    prefix = log_name.removesuffix("_log")
    cand = f"{prefix}_planks"
    return cand if cand in ALL_PLANKS else "oak_planks"


# 配方需求：(所需木板数, 所需木棍数, 所需圆石数)
_GEAR_NEEDS: dict[str, tuple[int, int, int]] = {
    "wooden_pickaxe": (3, 2, 0),
    "stone_pickaxe": (0, 2, 3),
    "stone_sword": (0, 1, 2),
    "furnace": (0, 0, 8),
}


def can_afford_gear(state: "State", item: str, *, assume_no_table: bool = True) -> bool:
    if item not in _GEAR_NEEDS:
        return False
    p_need, s_need, c_need = _GEAR_NEEDS[item]
    if total_cobble(state) < c_need:
        return False
    sticks = state.count("stick")
    missing_sticks = max(0, s_need - sticks)
    recipes = ([2] * math.ceil(missing_sticks / 4)
               + ([4] if state.count("crafting_table") < 1 and assume_no_table else [])
               + ([p_need] if p_need else []))
    # ep-0087 local-target 拒绝了 2 birch + 1 oak 合木镐；官方是否接受混种未确认。
    # 保守按每次配方同种备料，并覆盖网关可能选择的任意可用树种，避免再次空转。
    potential = tuple(state.count(p) + state.count(lg) * 4
                      for p, lg in zip(ALL_PLANKS, ALL_LOGS))

    def payable(stacks: tuple[int, ...], remaining: list[int]) -> bool:
        if not remaining:
            return True
        need = remaining[0]
        choices = [i for i, count in enumerate(stacks) if count >= need]
        return bool(choices) and all(
            payable(tuple(count - need if j == i else count
                          for j, count in enumerate(stacks)), remaining[1:])
            for i in choices
        )

    return payable(potential, recipes)


def _prepare_recipe_planks(ex: "Executor", state: "State", needed: int,
                           target: str) -> Iterator[dict]:
    """每次配方前现查同种木板；最多 needed 次有进展的转化，动作后立即让出。"""
    for _ in range(needed):
        if max(state.count(p) for p in ALL_PLANKS) >= needed:
            return
        logs = [lg for lg in ALL_LOGS if state.count(lg) > 0]
        if not logs:
            raise Replan(f"合成 {target} 同种木板不足：单次需 {needed}，需补充原木")
        log_item = max(logs, key=lambda lg: (state.count(_planks_for_log(lg)), state.count(lg)))
        plank_item = _planks_for_log(log_item)
        before = state.count(plank_item)
        yield rec("craft", ex.craft(plank_item, 1), item=plank_item)
        state.refresh_inventory(ex.client)
        if state.count(plank_item) <= before:
            raise Replan(f"合成 {plank_item} 后木板未增加，停止准备 {target}")
    if max(state.count(p) for p in ALL_PLANKS) < needed:
        raise Replan(f"合成 {target} 同种木板仍不足：单次需 {needed}，停止转化原木")


@register
class EatFood(Skill):
    name = "eat_food"
    description = "食用背包食物恢复饱食度与血量"
    params = {}
    goal = "survive:eat"
    cooldown_s = 10.0
    verified = "offline"

    @classmethod
    def blocked(cls, state: "State") -> str:
        food = float(state.st.get("food", 20))
        if food >= 20:
            return "饱食度已满"
        return "" if best_food(state) else "背包没有可食用食物"

    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        state.refresh_inventory(ex.client)
        item = best_food(state)
        if not item:
            raise Replan("背包没有可食用食物")
        yield rec("use_item", ex.use_item(item), item=item)

    def verify_done(self, state: "State", before_counts: dict[str, int]) -> str:
        before_food = sum(before_counts.get(f, 0) for f in EDIBLE_FOODS)
        after_food = sum(state.count(f) for f in EDIBLE_FOODS)
        if after_food >= before_food and float(state.st.get("food", 0)) < 20:
            return "eat_food 结束但食物未消耗且饱食度未满"
        return ""


@register
class ScoutIsland(Skill):
    name = "scout_island"
    description = "扫描周边资源与结构指纹并沿实心陆地向外探索"
    params = {"radius": Param(int, "扫描半径", default=32, min=12, max=64)}
    goal = "explore:island"
    cooldown_s = 12.0
    verified = "offline"

    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        st = state.refresh_status(ex.client, max_age_s=0)
        cur = st.get("pos") or state.pos
        r = int(self.args["radius"])
        summary = ex.client.find_blocks(radius=r, limit=40)
        blocks_map = (summary.get("blocks") or {}) if isinstance(summary, dict) else {}
        hits = {
            tag: [b for b in names if b in blocks_map]
            for tag, names in STRUCTURE_FINGERPRINTS.items()
            if any(b in blocks_map for b in names)
        }

        # limit 取 250 以覆盖近处 5 格地表之外的远端陆地（limit=40 在普通森林地形会被脚边草方块占满）
        land = ex.client.find_blocks(types=list(SAFE_GROUND), radius=min(r, 28), limit=250)
        cands = []
        for b in (land.get("blocks") or []):
            p = b.get("pos")
            if not (isinstance(p, list) and len(p) == 3):
                continue
            dist = math.hypot(p[0] - cur[0], p[2] - cur[2])
            if dist < 5.0 or abs(p[1] - cur[1]) > 10.0 or _is_protected(p, radius=8.0):
                continue
            visited_pen = sum(1 for vx, vz in VISITED_ANCHORS[-6:] if math.hypot(p[0] - vx, p[2] - vz) < 8.0)
            cands.append((visited_pen, -dist, p))

        if not cands:
            raise Replan("周边未找到可安全落脚的新陆地方块（防虚空不盲走）")
        # 落水时先找最近的干燥落脚点；在陆地上仍优先未访问过的远端区域。
        cands.sort(key=(lambda item: (-item[1], item[0])) if st.get("in_water")
                   else (lambda item: (item[0], item[1])))
        checked: list[list] = []
        attempts = 0
        last_nav_err: Optional[Replan] = None
        for _, _, chosen in cands:
            dest = [chosen[0] + 0.5, chosen[1] + 1, chosen[2] + 0.5]
            # 相邻方块通常属于同一片地形，失败后换一片；同时限制额外观察与动作预算。
            if any(math.dist(dest, old) < 3.0 for old in checked):
                continue
            if len(checked) >= 16:
                break
            checked.append(dest)
            # 规范 §3.3 的“暴露”只保证某一面可见，侧露石壁和水底都不是干燥站位。
            try:
                if any(bare(ex.client.get_block([chosen[0], chosen[1] + dy, chosen[2]]).get("name"))
                       not in AIR for dy in (1, 2)):
                    continue
            except ApiError as e:
                if e.code == "not_loaded":
                    continue
                raise
            attempts += 1
            try:
                moved = ex.move_to(pos=dest, rng=2)
            except Replan as e:
                code, _ = failure(e)
                if code not in ("path_not_found", "unreachable", "move_timeout"):
                    raise
                last_nav_err = e
                # 失败动作也让出一步，不能在同一个 Safety 边界内直接发下一个动作。
                yield rec("move_to", pos=dest, fingerprints=hits, error_code=code)
                state.refresh_status(ex.client, max_age_s=0)
                if attempts >= 3:
                    break
                continue
            yield rec("move_to", moved, pos=dest, fingerprints=hits)
            arrived = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
            VISITED_ANCHORS.append((float(arrived[0]), float(arrived[2])))
            return
        if last_nav_err is not None:
            raise last_nav_err
        raise Replan("周边未找到有两格空气的安全新陆地站位（防虚空不盲走）")

    def verify_done(self, state: "State", before_counts: dict[str, int]) -> str:
        return "" if state.st.get("alive", True) else "探索途中角色死亡"


@register
class GatherWood(Skill):
    name = "gather_wood"
    description = "寻找附近裸露原木并砍伐收集"
    params = {"count": Param(int, "要砍几块原木", default=3, min=1, max=16)}
    goal = "acq:wood"
    cooldown_s = 20.0
    verified = "offline"

    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        state.refresh_status(ex.client, max_age_s=0)
        state.refresh_inventory(ex.client)
        start_logs = total_logs(state)
        want = int(self.args["count"])

        axe = best_axe(state)
        if axe and bare((state.st.get("held") or {}).get("name")) != axe:
            yield rec("equip", ex.equip(axe, "hand"), item=axe)

        preferred_species: Optional[str] = next(
            (lg for lg in ALL_LOGS if state.count(lg) >= 1), None
        )
        bad_positions: set[tuple[int, int, int]] = set()
        last_nav_err: Optional[Replan] = None
        attempts = 0
        while total_logs(state) - start_logs < want and attempts < want + 6:
            attempts += 1
            cur = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
            found = ex.client.find_blocks(types=list(ALL_LOGS), radius=32, limit=32)
            cands = []
            for b in (found.get("blocks") or []):
                p = b.get("pos")
                if not (isinstance(p, list) and len(p) == 3):
                    continue
                pt = (int(p[0]), int(p[1]), int(p[2]))
                if pt in bad_positions or _is_under_feet(cur, p) or _is_protected(p):
                    continue
                # 跳过头顶已悬空的高树冠原木（水平 <3 格且高于脚底 >4 格时地面寻路 GoalNear 必报 path_not_found）
                if math.hypot(p[0] - cur[0], p[2] - cur[2]) < 3.0 and (p[1] - cur[1]) > 4.0:
                    bad_positions.add(pt)
                    continue
                cands.append(b)

            if not cands:
                if total_logs(state) > start_logs:
                    return
                if last_nav_err is not None:
                    raise last_nav_err
                raise Replan("半径 32 内没有可砍的裸露原木（或均在保护区/不可达处）")

            if preferred_species is None:
                preferred_species = bare(cands[0].get("name", "")) or None
            cands.sort(
                key=lambda b: (
                    0 if (preferred_species and bare(b.get("name", "")) == preferred_species and float(b.get("dist", 99)) <= 18.0) else 1,
                    float(b.get("dist", math.dist(cur, b["pos"]))),
                )
            )
            target = cands[0]["pos"]
            t_key = (int(target[0]), int(target[1]), int(target[2]))
            if math.dist(cur, target) > 4.2:
                try:
                    yield rec("move_to", ex.move_to(pos=target, rng=3), pos=target)
                except Replan as e:
                    code, _ = failure(e)
                    if code in ("path_not_found", "unreachable", "move_timeout"):
                        bad_positions.add(t_key)
                        if target[1] > cur[1] + 3:
                            for dy in range(0, 5):
                                bad_positions.add((t_key[0], t_key[1] + dy, t_key[2]))
                        last_nav_err = e
                        continue
                    raise
            try:
                yield rec("dig", ex.dig(target, collect=True), pos=target)
            except Replan as e:
                code, _ = failure(e)
                if code == "protected_region":
                    _mark_protected(target)
                    raise Replan(f"原木 {target} 位于出生点保护区，已标记外迁") from e
                if code in ("unreachable", "dig_timeout", "path_not_found"):
                    bad_positions.add(t_key)
                    last_nav_err = e
                    continue
                raise
            state.refresh_inventory(ex.client)

        if total_logs(state) <= start_logs and last_nav_err is not None:
            raise last_nav_err

    def verify_done(self, state: "State", before_counts: dict[str, int]) -> str:
        before = sum(before_counts.get(k, 0) for k in ALL_LOGS)
        if total_logs(state) <= before:
            return f"gather_wood 正常结束但原木未增加（before={before}, after={total_logs(state)}）"
        return ""


@register
class CraftGear(Skill):
    name = "craft_gear"
    description = "自动补齐木板木棍、放置或复用工作台并合成基础工具与熔炉"
    params = {
        "item": Param(
            str,
            "要合成的物品",
            choices=("wooden_pickaxe", "stone_pickaxe", "stone_sword", "furnace"),
            default="wooden_pickaxe",
        ),
    }
    goal = "craft:{item}"
    cooldown_s = 20.0
    verified = "offline"

    @classmethod
    def blocked(cls, state: "State") -> str:
        if total_logs(state) + total_planks(state) + total_cobble(state) < 2:
            return "缺少原木、木板或圆石原料"
        return ""

    def can_start(self, state: "State") -> bool:
        return can_afford_gear(state, self.args["item"], assume_no_table=False)

    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        target = str(self.args["item"])
        p_need, s_need, c_need = _GEAR_NEEDS[target]
        state.refresh_status(ex.client, max_age_s=0)
        state.refresh_inventory(ex.client)

        if total_cobble(state) < c_need:
            raise Replan(f"合成 {target} 圆石不足：需 {c_need}，现 {total_cobble(state)}")

        nearby_tables = ex.client.find_blocks(types=["crafting_table"], radius=16, limit=5).get("blocks") or []
        valid_tables = [b for b in nearby_tables if isinstance(b.get("pos"), list)
                        and len(b["pos"]) == 3 and not _is_protected(b["pos"])]
        table_ready = False
        for tb in valid_tables:
            tpos = tb["pos"]
            if math.dist(state.pos, tpos) > 3.5:
                try:
                    moved = ex.move_to(pos=tpos, rng=2)
                except Replan as e:
                    code, _ = failure(e)
                    if code not in ("path_not_found", "unreachable", "move_timeout"):
                        raise
                    _forget_known_table(tpos)
                    # 失败动作也必须让出一步，给 Safety 抢占与状态刷新留出边界。
                    yield rec("move_to", pos=tpos, error_code=code)
                    state.refresh_status(ex.client, max_age_s=0)
                    continue
                yield rec("move_to", moved, pos=tpos)
                state.refresh_status(ex.client, max_age_s=0)
                if math.dist(state.pos, tpos) > 3.5:
                    # 成功响应也要核对实际到达位置，不能把仍够不到的台当作可用锚点。
                    _forget_known_table(tpos)
                    continue
            _record_known_table(tpos)
            table_ready = True
            break
        need_table = not table_ready and state.count("crafting_table") < 1

        if not can_afford_gear(state, target, assume_no_table=need_table):
            raise Replan(f"合成 {target} 木板不足：同种材料无法完成木棍、工作台与目标配方，需补充原木")

        while state.count("stick") < s_need:
            yield from _prepare_recipe_planks(ex, state, 2, "stick")
            before_sticks = state.count("stick")
            yield rec("craft", ex.craft("stick", 1), item="stick")
            state.refresh_inventory(ex.client)
            if state.count("stick") <= before_sticks:
                raise Replan("合成 stick 后木棍未增加，停止准备工具")

        if not table_ready:
            if state.count("crafting_table") < 1:
                yield from _prepare_recipe_planks(ex, state, 4, "crafting_table")
                yield rec("craft", ex.craft("crafting_table", 1), item="crafting_table")
                state.refresh_inventory(ex.client)
            try:
                placed = ex.place("crafting_table")
                if isinstance(placed, dict) and isinstance(placed.get("pos"), list):
                    _record_known_table(placed["pos"])
                else:
                    _record_known_table(state.pos)
                yield rec("place", placed, item="crafting_table")
            except Replan as e:
                code, _ = failure(e)
                if code == "protected_region":
                    _mark_protected(state.pos)
                    raise Replan("当前位置处于出生点保护区，无法放置工作台") from e
                raise
        yield from _prepare_recipe_planks(ex, state, p_need, target)
        yield rec("craft", ex.craft(target, 1), item=target)
        state.refresh_inventory(ex.client)

    def verify_done(self, state: "State", before_counts: dict[str, int]) -> str:
        target = str(self.args["item"])
        if state.count(target) <= before_counts.get(target, 0):
            return f"craft_gear({target}) 结束但背包数量未增加"
        return ""


@register
class MineStone(Skill):
    name = "mine_stone"
    description = "装备镐安全挖掘石头收集圆石（自动剥离表土并严禁挖脚下）"
    params = {"count": Param(int, "期望新增圆石数", default=8, min=1, max=32)}
    goal = "acq:cobblestone"
    cooldown_s = 20.0
    verified = "offline"

    @classmethod
    def blocked(cls, state: "State") -> str:
        return "" if best_pickaxe(state) else "背包没有镐"

    @staticmethod
    def _require_pickaxe(state: "State") -> str:
        pick = best_pickaxe(state)
        if not pick:
            raise Replan("背包已无镐，停止采石并重新准备工具")
        return pick

    def _prepare_pickaxe(self, ex: "Executor", state: "State") -> Iterator[dict]:
        # equip/dig 会使背包失效；不能凭旧 status.held 或技能启动时的工具继续挖石。
        inv = state.refresh_inventory(ex.client)
        pick = self._require_pickaxe(state)
        held = inv.get("held", state.st.get("held")) or {}
        if bare(held.get("name")) != pick:
            yield rec("equip", ex.equip(pick, "hand"), item=pick)
            inv = state.refresh_inventory(ex.client)
            self._require_pickaxe(state)
            if "held" in inv and bare((inv.get("held") or {}).get("name")) != pick:
                raise Replan("装备镐后手持未确认，停止采石并重新观察工具")

    def _check_dig_tool(self, state: "State", result: dict) -> None:
        if result.get("warning"):
            raise Replan(f"挖掘工具告警，停止采石：{result['warning']}")
        self._require_pickaxe(state)

    @staticmethod
    def _safe_stair_column(ex: "Executor", x: int, y: int, z: int) -> bool:
        floor = bare(ex.client.get_block([x, y - 1, z]).get("name", ""))
        if not floor or floor in AIR or floor in ("lava", "water"):
            return False
        # 三格净空上方若是落沙/砾石，挖开后会重新填入槽内；底板实心仍不足以安全下行。
        roof = bare(ex.client.get_block([x, y + 3, z]).get("name", ""))
        return bool(roof) and roof not in ("sand", "red_sand", "gravel")

    def _shallow_stone_approach(self, ex: "Executor", cur, stones, tried) -> Optional[list]:
        # 两格下方的暴露石头可能在浅岸边；先观察相邻承重格和净空，
        # 不能直接放宽挖掘高差，把深洞内的暴露石头当作可收集目标。
        for block in stones[:4]:
            p = block.get("pos")
            if not (isinstance(p, list) and len(p) == 3
                    and math.floor(cur[1]) - 2 <= p[1] < cur[1] - 1.5
                    and not _is_protected(p)):
                continue
            x, y, z = map(int, p)
            sides = sorted(((x + dx, z + dz) for dx, dz in
                            ((1, 0), (-1, 0), (0, 1), (0, -1))),
                           key=lambda q: math.hypot(q[0] + 0.5 - cur[0], q[1] + 0.5 - cur[2]))
            for sx, sz in sides:
                key = (sx, y + 1, sz)
                if key in tried or _is_protected(key):
                    continue
                tried.add(key)
                if (self._safe_stair_column(ex, sx, y + 1, sz)
                        and all(bare(ex.client.get_block([sx, y + dy, sz]).get("name", ""))
                                in AIR for dy in (1, 2, 3))):
                    return [sx + 0.5, y + 1, sz + 0.5]
        return None

    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        state.refresh_status(ex.client, max_age_s=0)
        state.refresh_inventory(ex.client)
        pick = best_pickaxe(state)
        if not pick:
            raise Replan("背包没有镐")
        if bare((state.st.get("held") or {}).get("name")) != pick:
            yield rec("equip", ex.equip(pick, "hand"), item=pick)

        start_cobble = total_cobble(state)
        want = int(self.args["count"])
        budget = want + 18
        bad_positions: set[tuple[int, int, int]] = set()
        stair_origin: Optional[tuple[int, int, int, int, int]] = None
        stair_step = 0
        stair_steps_taken = 0
        stair_columns: set[tuple[int, int]] = set()
        tried_approaches: set[tuple[int, int, int]] = set()
        approach_moves = 0

        for _ in range(budget):
            if total_cobble(state) - start_cobble >= want:
                return
            cur = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
            stones = ex.client.find_blocks(types=list(STONE_Targets), radius=16, limit=24).get("blocks") or []
            # 仅选与当前脚底高差在 [-1.5, +2.5] 内的平层/浅层石头，避开地下洞穴暴露块（否则寻路卡 8s 且掉落物掉入洞底捡不到）
            cands = [
                b["pos"] for b in stones
                if isinstance(b.get("pos"), list) and len(b["pos"]) == 3
                and (int(b["pos"][0]), int(b["pos"][1]), int(b["pos"][2])) not in bad_positions
                and (cur[1] - 1.5) <= float(b["pos"][1]) <= (cur[1] + 2.5)
                and not _is_under_feet(cur, b["pos"])
                and not _is_protected(b["pos"])
            ]
            target = cands[0] if cands else None

            if target is None:
                # 地表无平层裸露石头：从脚边安全方向向下开凿阶梯式探槽（每步先核对底板非空气/岩浆/水，防挖穿虚空，且人物随阶梯下行贴身拾取）
                if stair_origin is None and stair_steps_taken < 6:
                    ix, fy, iz = math.floor(cur[0]), math.floor(cur[1]), math.floor(cur[2])
                    stair_columns.add((ix, iz))
                    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        sx1, sz1 = ix + dx, iz + dz
                        if (sx1, sz1) in stair_columns or _is_protected([sx1, fy - 1, sz1]):
                            continue
                        head1 = bare(ex.client.get_block([sx1, fy, sz1]).get("name", ""))
                        if head1 == "crafting_table":
                            continue
                        if self._safe_stair_column(ex, sx1, fy - 1, sz1):
                            stair_origin = (ix, fy, iz, dx, dz)
                            stair_step = 1
                            break
                if stair_origin is not None and stair_steps_taken < 6:
                    ox, ofy, oz, dx, dz = stair_origin
                    sx, sy, sz = ox + stair_step * dx, ofy - stair_step, oz + stair_step * dz
                    if ((sx, sz) not in stair_columns
                            and self._safe_stair_column(ex, sx, sy, sz)):
                        stair_columns.add((sx, sz))
                        # collect 会带动站位；未走完的列也消耗同一个六列预算。
                        stair_steps_taken += 1
                        reobserve_stair = False
                        # 清理阶梯头顶与落脚格（3 格净空满足斜向下台阶碰撞箱）
                        for clear_y in (sy + 2, sy + 1, sy):
                            bname = bare(ex.client.get_block([sx, clear_y, sz]).get("name", ""))
                            if bname and bname not in AIR and bname not in ("water", "lava", "crafting_table"):
                                if bname in STONE_Targets:
                                    yield from self._prepare_pickaxe(ex, state)
                                cur = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
                                if _is_under_feet(cur, [sx, clear_y, sz]):
                                    # 保留当前支撑，丢弃旧直线，从下一拍实测位置建槽。
                                    stair_origin = None
                                    reobserve_stair = True
                                    break
                                dug_res = ex.dig([sx, clear_y, sz], collect=True)
                                yield rec("dig", dug_res, pos=[sx, clear_y, sz])
                                state.refresh_inventory(ex.client)
                                if total_cobble(state) - start_cobble >= want:
                                    return
                                self._check_dig_tool(state, dug_res)
                        if reobserve_stair:
                            continue
                        for clear_y in (sy + 2, sy + 1, sy):
                            if bare(ex.client.get_block([sx, clear_y, sz]).get("name", "")) not in AIR:
                                raise Replan("阶梯净空发生变化，停止进入并重新观察")
                        try:
                            yield rec("move_to", ex.move_to(pos=[sx + 0.5, sy, sz + 0.5], rng=1), pos=[sx + 0.5, sy, sz + 0.5])
                        except Replan as e:
                            code, _ = failure(e)
                            if code not in ("path_not_found", "unreachable", "move_timeout"):
                                raise
                            yield rec("move_to", pos=[sx + 0.5, sy, sz + 0.5], error_code=code)
                        stair_step += 1
                        continue
                    # 直线探槽遇到洞口时，从下一拍实测站位核查其它方向；
                    # 不回挖已走列，且转向不会重置总共六步的下探上限。
                    stair_origin = None
                    continue

                if total_cobble(state) > start_cobble:
                    return
                if approach_moves < 3:
                    dest = self._shallow_stone_approach(ex, cur, stones, tried_approaches)
                    if dest is not None:
                        approach_moves += 1
                        try:
                            moved = ex.move_to(pos=dest, rng=1)
                        except Replan as e:
                            code, _ = failure(e)
                            if code not in ("path_not_found", "unreachable", "move_timeout"):
                                raise
                            yield rec("move_to", pos=dest, error_code=code)
                        else:
                            yield rec("move_to", moved, pos=dest)
                        stair_origin = None
                        continue
                raise Replan("附近未找到可安全挖掘的石头或非悬空表土")

            t_key = (int(target[0]), int(target[1]), int(target[2]))
            if math.dist(cur, target) > 4.0:
                try:
                    yield rec("move_to", ex.move_to(pos=target, rng=2), pos=target)
                except Replan as e:
                    code, _ = failure(e)
                    if code in ("path_not_found", "unreachable", "move_timeout"):
                        bad_positions.add(t_key)
                        yield rec("move_to", pos=target, error_code=code)
                        continue
                    raise
            try:
                yield from self._prepare_pickaxe(ex, state)
                cur = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
                if _is_under_feet(cur, target):
                    bad_positions.add(t_key)
                    continue
                before_dig_cobble = total_cobble(state)
                dug_res = ex.dig(target, collect=True)
                yield rec("dig", dug_res, pos=target)
            except Replan as e:
                code, _ = failure(e)
                if code == "protected_region":
                    _mark_protected(target)
                    raise Replan(f"方块 {target} 位于出生点保护区，已标记外迁") from e
                if code in ("unreachable", "dig_timeout", "path_not_found"):
                    bad_positions.add(t_key)
                    yield rec("dig", pos=target, error_code=code)
                    continue
                raise
            state.refresh_inventory(ex.client)
            if total_cobble(state) - start_cobble >= want:
                return
            self._check_dig_tool(state, dug_res)
            # 若挖开了石头但掉落物掉入悬崖/深坑未能拾取，拉黑该崖边区域改走阶梯探槽
            if total_cobble(state) <= before_dig_cobble:
                for bx in range(t_key[0] - 2, t_key[0] + 3):
                    for by in range(t_key[1] - 2, t_key[1] + 3):
                        for bz in range(t_key[2] - 2, t_key[2] + 3):
                            bad_positions.add((bx, by, bz))

        if total_cobble(state) <= start_cobble:
            raise Replan("剥离表土后仍未采集到圆石")

    def verify_done(self, state: "State", before_counts: dict[str, int]) -> str:
        before = before_counts.get("cobblestone", 0) + before_counts.get("cobbled_deepslate", 0)
        if total_cobble(state) <= before:
            return f"mine_stone 结束但圆石未增加（before={before}, after={total_cobble(state)}）"
        return ""
