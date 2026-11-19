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
from typing import TYPE_CHECKING, Generator, Iterator, Optional

from ..client import ApiError
from ..runtime import EnvBlocked, Replan
from ..skills import nav
from ..skills.base import Param, Skill, failure, rec
from ..skills.items import AIR, MINE_TRAILS, NAV_FAIL_CODES, bare, best_pickaxe, is_clear_space
from ..skills.recipes import craft_recipe
from ..skills.registry import register
from ..skills.sightline import (
    API_DIG_EYE_REACH,
    DIG_EYE_REACH,
    STATION_EYE_REACH,
    block_center,
    can_reach_block,
    eye_to_block_dist,
)
from ..skills.world_memo import WorldResourceMemo, memo_for, reset_all_world_memos

if TYPE_CHECKING:
    from ..runtime import Executor, State

ALL_LOGS: tuple[str, ...] = (
    "oak_log", "birch_log", "spruce_log", "jungle_log",
    "acacia_log", "dark_oak_log", "cherry_log", "mangrove_log", "pale_oak_log",
)
ALL_PLANKS: tuple[str, ...] = (
    "oak_planks", "birch_planks", "spruce_planks", "jungle_planks",
    "acacia_planks", "dark_oak_planks", "cherry_planks", "mangrove_planks", "pale_oak_planks",
)
AXE_ORDER: tuple[str, ...] = (
    "netherite_axe", "diamond_axe", "iron_axe", "copper_axe",
    "stone_axe", "golden_axe", "wooden_axe",
)
STONE_Targets: tuple[str, ...] = ("stone", "cobblestone", "deepslate", "cobbled_deepslate")
PEEL_SURFACE: tuple[str, ...] = ("grass_block", "dirt", "coarse_dirt", "podzol", "leaf_litter")
SAFE_GROUND: tuple[str, ...] = (
    "grass_block", "dirt", "coarse_dirt", "stone", "cobblestone",
    "sand", "sandstone", "gravel", "andesite", "diorite", "granite",
    "tuff", "oak_planks", "pale_oak_planks",
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
    reset_all_world_memos()
    MINE_TRAILS.clear()


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


def _refresh_stance(ex: "Executor", state: "State") -> tuple[dict, list[float]]:
    st = state.refresh_status(ex.client, max_age_s=0)
    cur = st.get("pos") or state.pos
    if not st.get("pos"):
        st = {**st, "pos": cur}
    return st, cur


def _dim_of(st: dict) -> str:
    return str(st.get("dimension") or "overworld")


def _run_nav_recovery(
    ex: "Executor",
    state: "State",
    cur: list[float],
    *,
    dest=None,
    min_displacement: float = 0.8,
) -> Generator[dict, None, tuple[bool, list[float]]]:
    """调用 L2 地形脱困原语并复测位移，返回 `(escaped, after_pos)`。"""
    before_pos = list(cur)
    try:
        yield from nav.recover_move(ex, state, dest=dest, protected_fn=_is_protected)
    except Replan as rec_err:
        if failure(rec_err)[0] not in NAV_FAIL_CODES:
            raise
    _, after = _refresh_stance(ex, state)
    return math.dist(after, before_pos) >= min_displacement, after


def _probe_block_name(ex: "Executor", pos: list) -> Optional[str]:
    """只读查询目标格方块名；未加载或未就绪时返回 None。"""
    try:
        return bare(ex.client.get_block(pos).get("name"))
    except ApiError as e:
        if e.code in ("not_loaded", "not_ready"):
            return None
        raise


def _scout_hits(summary: object) -> dict[str, list[str]]:
    blocks_map = (summary.get("blocks") or {}) if isinstance(summary, dict) else {}
    if isinstance(blocks_map, list):
        seen = {bare(b.get("name", "")) for b in blocks_map if isinstance(b, dict)}
        return {
            tag: [b for b in names if b in seen]
            for tag, names in STRUCTURE_FINGERPRINTS.items()
            if any(b in seen for b in names)
        }
    if not isinstance(blocks_map, dict):
        return {}
    return {
        tag: [b for b in names if b in blocks_map]
        for tag, names in STRUCTURE_FINGERPRINTS.items()
        if any(b in blocks_map for b in names)
    }


def _is_high_canopy(
    ex: "Executor",
    st: dict,
    cur: list[float],
    pt: tuple[int, int, int],
) -> bool:
    """判断是否为头顶悬空或被遮挡且高差>4格的不可达树冠原木。"""
    same_col = pt[0] == math.floor(cur[0]) and pt[2] == math.floor(cur[2])
    cx, _, cz = block_center(pt)
    return (
        math.hypot(cx - cur[0], cz - cur[2]) < 3.0
        and (pt[1] - cur[1]) > 4.0
        and not (same_col and can_reach_block(ex, st, pt, reach=DIG_EYE_REACH))
    )


def _no_wood_blocked() -> EnvBlocked:
    return EnvBlocked(
        "半径 64 内及已知原木台账均没有可砍的裸露原木（或均在保护区/不可达处）",
        missing_predicate="exposed_log",
        searched_volume="半径 64 + 共享原木台账",
        wake_on="chunk_moved",
    )


def _collect_live_woods(
    ex: "Executor",
    st: dict,
    cur: list[float],
    dim: str,
    memo: WorldResourceMemo,
    bad_positions: set[tuple[int, int, int]],
    blocks: list,
) -> tuple[list[dict], set[tuple[int, int, int]]]:
    cands: list[dict] = []
    live_keys: set[tuple[int, int, int]] = set()
    for b in blocks:
        p = b.get("pos") if isinstance(b, dict) else None
        if not (isinstance(p, list) and len(p) == 3):
            continue
        pt = (int(p[0]), int(p[1]), int(p[2]))
        live_keys.add(pt)
        if (
            pt in bad_positions
            or memo.is_blocked(pt, dimension=dim, category="wood")
            or _is_under_feet(cur, p)
            or _is_protected(p)
        ):
            continue
        if _is_high_canopy(ex, st, cur, pt):
            bad_positions.add(pt)
            continue
        cands.append({
            "pos": [pt[0], pt[1], pt[2]],
            "name": bare(b.get("name", "")),
            "from_memo": False,
            "memo_status": "live",
        })
    return cands, live_keys


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
    if item == "stick":
        return total_planks(state) + 4 * total_logs(state) >= 2 or state.count("bamboo") >= 2
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
        st, cur = _refresh_stance(ex, state)
        dim = _dim_of(st)
        memo = memo_for(state)
        r = int(self.args["radius"])
        summary = ex.client.find_blocks(radius=r, limit=40)
        memo.ingest_find_blocks(summary, dimension=dim, origin=st)
        hits = _scout_hits(summary)

        # 若角色刚在探矿阶梯井底受阻（当前位置紧邻最近一条下探轨迹末端），先分段沿阶梯退回井口地表再外迁。
        if MINE_TRAILS:
            trail = MINE_TRAILS[-1]
            tail = trail[-1]
            if math.dist(cur, tail) <= 5.0 and trail[0][1] > cur[1] + 2.0:
                MINE_TRAILS.pop()
                indices = list(range(len(trail) - 1, -1, -10))
                if indices[-1] != 0:
                    indices.append(0)
                for idx in indices:
                    if idx == len(trail) - 1:
                        continue
                    wp = list(trail[idx])
                    try:
                        moved_up = ex.move_to(pos=wp, rng=2)
                    except Replan as e:
                        code, _ = failure(e)
                        if code not in NAV_FAIL_CODES:
                            raise
                        yield rec("move_to", pos=wp, fingerprints=hits, error_code=code)
                        break
                    yield rec("move_to", moved_up, pos=wp, fingerprints=hits)
                st, cur = _refresh_stance(ex, state)

        target_wood = None
        cur_wood_dist = 0.0
        if total_logs(state) == 0:
            woods = [
                entry for entry in memo.query("wood", dimension=dim, origin=st)
                if entry.status in ("untested", "reachable") and not _is_protected(entry.pos)
            ]
            if woods:
                cand_dist = eye_to_block_dist(st, woods[0].pos)
                if cand_dist > 28.0:
                    target_wood = woods[0]
                    cur_wood_dist = cand_dist

        # limit 适度放大至 250~350 以覆盖近处地表之外的远端陆地（bot_session.js 上限 500）
        land_limit = min(350, max(250, r * 7))
        land = ex.client.find_blocks(types=list(SAFE_GROUND), radius=min(r, 28), limit=land_limit)
        recent_anchors = VISITED_ANCHORS[-24:]
        cands = []
        climb_cands = []
        for b in (land.get("blocks") or []):
            p = b.get("pos")
            if not (isinstance(p, list) and len(p) == 3):
                continue
            dist = math.hypot(p[0] - cur[0], p[2] - cur[2])
            if _is_protected(p, radius=8.0):
                continue
            visited_pen = 0
            for vx, vz in recent_anchors:
                d_anchor = math.hypot(p[0] - vx, p[2] - vz)
                if d_anchor < 8.0:
                    visited_pen += 2
                elif d_anchor < 14.0:
                    visited_pen += 1
            if target_wood is not None:
                cand_wood_dist = eye_to_block_dist(
                    {"pos": [p[0] + 0.5, p[1] + 1.0, p[2] + 0.5], "sneaking": st.get("sneaking")},
                    target_wood.pos,
                )
                closer_to_wood = 0 if cand_wood_dist < cur_wood_dist - 8.0 else 1
                wood_order = cand_wood_dist if closer_to_wood == 0 else -dist
            else:
                closer_to_wood = 1
                wood_order = -dist
            if dist >= 5.0 and abs(p[1] - cur[1]) <= 10.0:
                cands.append((visited_pen, -dist, closer_to_wood, wood_order, p))
            elif dist >= 3.0 and 10.0 < (p[1] - cur[1]) <= 28.0:
                climb_cands.append((visited_pen, p[1] - cur[1], closer_to_wood, wood_order, p))

        attempted_recovery = False
        if not cands and not climb_cands:
            attempted_recovery = True
            escaped, arrived = yield from _run_nav_recovery(
                ex, state, cur, dest=list(target_wood.pos) if target_wood is not None else None
            )
            if escaped:
                VISITED_ANCHORS.append((float(arrived[0]), float(arrived[2])))
                return
            raise Replan("周边未找到可安全落脚的新陆地方块（防虚空不盲走）")
        # 落水时先找最近的干燥落脚点；在陆地上优先低惩罚组，缺木时同组优先靠近已知远木，否则优先未访问远端；平层无点时试爬回地表。
        cands.sort(
            key=(lambda item: (-item[1], item[0]))
            if st.get("in_water")
            else (lambda item: (item[0], item[2], item[3], item[1]))
        )
        climb_cands.sort(key=lambda item: (item[0], item[1]))
        all_cands = cands + climb_cands
        checked: list[list] = []
        attempts = 0
        last_nav_err: Optional[Replan] = None
        for _, _, _, _, chosen in all_cands:
            dest = [chosen[0] + 0.5, chosen[1] + 1, chosen[2] + 0.5]
            # 相邻方块通常属于同一片地形，失败后换一片；同时限制额外观察与动作预算。
            if any(math.dist(dest, old) < 3.0 for old in checked):
                continue
            if len(checked) >= 16:
                break
            checked.append(dest)
            # 规范 §3.3 的“暴露”只保证某一面可见，侧露石壁和水底都不是干燥站位。
            try:
                if any(
                    not is_clear_space(ex.client.get_block([chosen[0], chosen[1] + dy, chosen[2]]))
                    for dy in (1, 2)
                ):
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
                if code not in NAV_FAIL_CODES:
                    raise
                last_nav_err = e
                # 失败动作也让出一步，不能在同一个 Safety 边界内直接发下一个动作。
                yield rec("move_to", pos=dest, fingerprints=hits, error_code=code)
                st, cur = _refresh_stance(ex, state)
                if code == "stalled" and not attempted_recovery:
                    attempted_recovery = True
                    escaped, arrived = yield from _run_nav_recovery(ex, state, cur, dest=dest)
                    if escaped:
                        VISITED_ANCHORS.append((float(arrived[0]), float(arrived[2])))
                        return
                if attempts >= 3:
                    break
                continue
            yield rec("move_to", moved, pos=dest, fingerprints=hits)
            arrived = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
            VISITED_ANCHORS.append((float(arrived[0]), float(arrived[2])))
            return
        if not attempted_recovery:
            attempted_recovery = True
            fallback_dest = cands[0][4] if cands else (list(target_wood.pos) if target_wood is not None else None)
            escaped, arrived = yield from _run_nav_recovery(ex, state, cur, dest=fallback_dest)
            if escaped:
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
        memo = memo_for(state)

        axe = best_axe(state)
        if axe and bare((state.st.get("held") or {}).get("name")) != axe:
            yield rec("equip", ex.equip(axe, "hand"), item=axe)

        preferred_species: Optional[str] = next(
            (lg for lg in ALL_LOGS if state.count(lg) >= 1), None
        )
        bad_positions: set[tuple[int, int, int]] = set()
        last_nav_err: Optional[Replan] = None
        used_recovery = False
        attempts = 0
        stale_memo_skips = 0

        def _mark_nav_unreachable(err: Replan, code_str: str) -> None:
            nonlocal last_nav_err
            bad_positions.add(t_key)
            memo.mark_unreachable(
                t_key,
                dimension=dim,
                category="wood",
                error_code=code_str,
                block=chosen["name"],
            )
            if target[1] > cur[1] + 3:
                for dy in range(0, 5):
                    bad_positions.add((t_key[0], t_key[1] + dy, t_key[2]))
            last_nav_err = err

        while total_logs(state) - start_logs < want and attempts < want + 6 and stale_memo_skips < 32:
            st, cur = _refresh_stance(ex, state)
            dim = _dim_of(st)

            found = ex.client.find_blocks(types=list(ALL_LOGS), radius=32, limit=32)
            memo.ingest_find_blocks(found, dimension=dim, origin=st, category="wood")
            cands, live_keys = _collect_live_woods(
                ex, st, cur, dim, memo, bad_positions, found.get("blocks") or []
            )

            if not cands:
                found64 = ex.client.find_blocks(types=list(ALL_LOGS), radius=64, limit=64)
                memo.ingest_find_blocks(found64, dimension=dim, origin=st, category="wood")
                cands, live_keys = _collect_live_woods(
                    ex, st, cur, dim, memo, bad_positions, found64.get("blocks") or []
                )
                for entry in memo.query("wood", dimension=dim, origin=st):
                    pt = entry.pos
                    if (
                        pt in live_keys
                        or pt in bad_positions
                        or _is_under_feet(cur, pt)
                        or _is_protected(pt)
                    ):
                        continue
                    if _is_high_canopy(ex, st, cur, pt):
                        bad_positions.add(pt)
                        continue
                    cands.append({
                        "pos": [pt[0], pt[1], pt[2]],
                        "name": entry.block,
                        "from_memo": True,
                        "memo_status": entry.status,
                    })

            if not cands:
                if total_logs(state) > start_logs:
                    return
                if last_nav_err is not None:
                    if not used_recovery and failure(last_nav_err)[0] == "stalled":
                        used_recovery = True
                        st, cur = _refresh_stance(ex, state)
                        yield from _run_nav_recovery(ex, state, cur)
                    raise last_nav_err
                raise _no_wood_blocked()

            if preferred_species is None:
                preferred_species = bare(cands[0].get("name", "")) or None
            cands.sort(
                key=lambda b: (
                    0 if not b["from_memo"] else 1,
                    0 if b["memo_status"] in ("live", "reachable", "untested") else 1,
                    # 先试当前高度附近或原地眼距可达的树干，避免远端高树冠耗尽尝试预算；山坡高树仍可后备尝试。
                    float(b["pos"][1]) > cur[1] + 4.0 and eye_to_block_dist(st, b["pos"]) > DIG_EYE_REACH,
                    0
                    if (
                        preferred_species
                        and bare(b.get("name", "")) == preferred_species
                        and eye_to_block_dist(st, b["pos"]) <= 18.0
                    )
                    else 1,
                    0 if eye_to_block_dist(st, b["pos"]) <= DIG_EYE_REACH else 1,
                    round(math.hypot(block_center(b["pos"])[0] - cur[0], block_center(b["pos"])[2] - cur[2]), 3),
                    0 if b["memo_status"] == "reachable" else 1,
                    float(b["pos"][1]),
                )
            )
            chosen = cands[0]
            target = chosen["pos"]
            t_key = (int(target[0]), int(target[1]), int(target[2]))
            from_memo = bool(chosen["from_memo"])

            if from_memo and eye_to_block_dist(st, target) <= 36.0:
                probed = _probe_block_name(ex, target)
                if probed is not None and probed not in ALL_LOGS:
                    memo.mark_depleted(t_key, dimension=dim, category="wood")
                    bad_positions.add(t_key)
                    stale_memo_skips += 1
                    continue
                if probed is None and eye_to_block_dist(st, target) <= DIG_EYE_REACH:
                    bad_positions.add(t_key)
                    stale_memo_skips += 1
                    continue

            attempts += 1
            moved_for_target = False
            need_move = (
                (from_memo and eye_to_block_dist(st, target) > 36.0)
                or not can_reach_block(ex, st, target, reach=DIG_EYE_REACH)
            )
            if need_move:
                try:
                    yield rec("move_to", ex.move_to(pos=target, rng=3), pos=target)
                except Replan as e:
                    code, _ = failure(e)
                    if code not in NAV_FAIL_CODES:
                        raise
                    if code == "stalled" and not used_recovery:
                        yield rec("move_to", pos=target, error_code=code)
                        used_recovery = True
                        st, cur = _refresh_stance(ex, state)
                        escaped, _ = yield from _run_nav_recovery(ex, state, cur, dest=target)
                        if escaped:
                            continue
                        _mark_nav_unreachable(e, code)
                        continue
                    _mark_nav_unreachable(e, code)
                    yield rec("move_to", pos=target, error_code=code)
                    continue
                moved_for_target = True

            if from_memo and moved_for_target:
                st, cur = _refresh_stance(ex, state)
                probed = _probe_block_name(ex, target)
                if probed is not None and probed not in ALL_LOGS:
                    memo.mark_depleted(t_key, dimension=dim, category="wood")
                    bad_positions.add(t_key)
                    continue
                if probed is None:
                    bad_positions.add(t_key)
                    continue

            try:
                dug_res = ex.dig(target, collect=True)
                memo.mark_reachable(t_key, dimension=dim, category="wood", block=chosen["name"])
                after_name = _probe_block_name(ex, target)
                if after_name is not None and after_name not in ALL_LOGS:
                    memo.mark_depleted(t_key, dimension=dim, category="wood")
                yield rec("dig", dug_res, pos=target)
            except Replan as e:
                code, _ = failure(e)
                if code == "protected_region":
                    _mark_protected(target)
                    memo.mark_protected(t_key, dimension=dim, category="wood", block=chosen["name"])
                    raise Replan(f"原木 {target} 位于出生点保护区，已标记外迁") from e
                if code in ("unreachable", "dig_timeout", "path_not_found"):
                    bad_positions.add(t_key)
                    memo.mark_unreachable(
                        t_key,
                        dimension=dim,
                        category="wood",
                        error_code=code,
                        block=chosen["name"],
                    )
                    last_nav_err = e
                    yield rec("dig", pos=target, error_code=code)
                    continue
                raise
            state.refresh_inventory(ex.client)

        if total_logs(state) <= start_logs:
            if last_nav_err is not None:
                if not used_recovery and failure(last_nav_err)[0] == "stalled":
                    used_recovery = True
                    st, cur = _refresh_stance(ex, state)
                    yield from _run_nav_recovery(ex, state, cur)
                raise last_nav_err
            raise _no_wood_blocked()

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
            choices=("wooden_pickaxe", "stone_pickaxe", "stone_sword", "furnace", "stick"),
            default="wooden_pickaxe",
        ),
    }
    goal = "craft:{item}"
    cooldown_s = 20.0
    verified = "offline"

    @classmethod
    def blocked(cls, state: "State") -> str:
        if (total_logs(state) + total_planks(state) + total_cobble(state) < 2
                and not can_afford_gear(state, "stick")):
            return "缺少原木、木板或圆石原料"
        return ""

    def can_start(self, state: "State") -> bool:
        target = str(self.args["item"])
        if state.count(target) >= 1:
            return False
        if target == "wooden_pickaxe" and best_pickaxe(state) is not None:
            return False
        return can_afford_gear(state, target, assume_no_table=False)

    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        target = str(self.args["item"])
        if target == "stick":
            state.invalidate_inventory()
            state.refresh_inventory(ex.client)
            if total_planks(state) < 2 and state.count("bamboo") < 2:
                yield from _prepare_recipe_planks(ex, state, 2, "stick")
            yield from craft_recipe(ex, state, "stick", goal_qty=state.count("stick") + 1)
            return
        p_need, s_need, c_need = _GEAR_NEEDS[target]
        st, _ = _refresh_stance(ex, state)
        dim = _dim_of(st)
        memo = memo_for(state)
        state.refresh_inventory(ex.client)

        if total_cobble(state) < c_need:
            raise Replan(f"合成 {target} 圆石不足：需 {c_need}，现 {total_cobble(state)}")

        nearby_resp = ex.client.find_blocks(types=["crafting_table"], radius=16, limit=5)
        memo.ingest_find_blocks(nearby_resp, dimension=dim, origin=st, category="station")
        nearby_tables = nearby_resp.get("blocks") or []
        valid_tables = [
            {"name": bare(b.get("name") or "crafting_table"), "pos": b["pos"], "from_memo": False}
            for b in nearby_tables
            if isinstance(b.get("pos"), list)
            and len(b["pos"]) == 3
            and not _is_protected(b["pos"])
            and not memo.is_blocked(b["pos"], dimension=dim, category="station")
        ]
        valid_tables.sort(key=lambda b: eye_to_block_dist(st, b["pos"]))
        if not valid_tables:
            valid_tables = [
                {"name": entry.block, "pos": list(entry.pos), "from_memo": True}
                for entry in memo.query("station", dimension=dim, origin=st)
                if entry.block == "crafting_table"
                and entry.status in ("untested", "reachable")
                and eye_to_block_dist(st, entry.pos) <= 48.0
                and not _is_protected(entry.pos)
            ]
        table_ready = False
        for tb in valid_tables:
            tpos = tb["pos"]
            from_memo = bool(tb.get("from_memo"))
            if from_memo and eye_to_block_dist(st, tpos) <= 36.0:
                probed = _probe_block_name(ex, tpos)
                if probed is not None and probed != "crafting_table":
                    _forget_known_table(tpos)
                    memo.mark_depleted(tpos, dimension=dim, category="station")
                    continue
                if probed is None and eye_to_block_dist(st, tpos) <= STATION_EYE_REACH:
                    continue
            if not can_reach_block(ex, st, tpos, reach=STATION_EYE_REACH):
                try:
                    moved = ex.move_to(pos=tpos, rng=2)
                except Replan as e:
                    code, _ = failure(e)
                    if code not in NAV_FAIL_CODES:
                        raise
                    _forget_known_table(tpos)
                    memo.mark_unreachable(
                        tpos,
                        dimension=dim,
                        category="station",
                        error_code=code,
                        block="crafting_table",
                    )
                    # 失败动作也必须让出一步，给 Safety 抢占与状态刷新留出边界。
                    yield rec("move_to", pos=tpos, error_code=code)
                    st, _ = _refresh_stance(ex, state)
                    continue
                yield rec("move_to", moved, pos=tpos)
                st, _ = _refresh_stance(ex, state)
                if from_memo:
                    probed = _probe_block_name(ex, tpos)
                    if probed != "crafting_table":
                        _forget_known_table(tpos)
                        if probed is not None:
                            memo.mark_depleted(tpos, dimension=dim, category="station")
                        continue
                if not can_reach_block(ex, st, tpos, reach=STATION_EYE_REACH):
                    # 成功响应也要核对实际到达后的眼距与视线遮挡，不能把仍够不到或隔墙的台当作可用锚点。
                    _forget_known_table(tpos)
                    memo.mark_unreachable(
                        tpos,
                        dimension=dim,
                        category="station",
                        error_code="unreachable",
                        block="crafting_table",
                    )
                    continue
            _record_known_table(tpos)
            memo.mark_reachable(tpos, dimension=dim, category="station", block="crafting_table")
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
                placed_pos = (
                    placed["pos"]
                    if isinstance(placed, dict) and isinstance(placed.get("pos"), list)
                    else state.pos
                )
                _record_known_table(placed_pos)
                memo.mark_reachable(
                    placed_pos,
                    dimension=dim,
                    category="station",
                    block="crafting_table",
                )
                yield rec("place", placed, item="crafting_table")
            except Replan as e:
                code, _ = failure(e)
                if code == "protected_region":
                    _mark_protected(state.pos)
                    memo.mark_protected(state.pos, dimension=dim)
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
        # 树冠的叶片和树干可能仍在地表上方，不能把它们当作采石地层。
        if not floor or floor in AIR or floor in ("lava", "water") or floor.endswith(("_leaves", "_log", "_wood")):
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
        yield from self._prepare_pickaxe(ex, state)

        start_cobble = total_cobble(state)
        want = int(self.args["count"])
        budget = want + 18
        memo = memo_for(state)
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
            st, cur = _refresh_stance(ex, state)
            dim = _dim_of(st)
            stones = ex.client.find_blocks(types=list(STONE_Targets), radius=16, limit=24).get("blocks") or []
            # 下界保持 cur[1] - 1.5 防深坑掉物，上界放宽至头顶眼距可达层（cur[1] + 4.0 且眼距合理，或原地眼距可达）
            cands = [
                b["pos"] for b in stones
                if isinstance(b.get("pos"), list) and len(b["pos"]) == 3
                and (int(b["pos"][0]), int(b["pos"][1]), int(b["pos"][2])) not in bad_positions
                and (cur[1] - 1.5) <= float(b["pos"][1])
                and (
                    float(b["pos"][1]) <= (cur[1] + 2.5)
                    or (float(b["pos"][1]) <= (cur[1] + 4.0) and eye_to_block_dist(st, b["pos"]) <= 8.0)
                    or eye_to_block_dist(st, b["pos"]) <= DIG_EYE_REACH
                )
                and not _is_under_feet(cur, b["pos"])
                and not _is_protected(b["pos"])
                and not memo.is_blocked(b["pos"], dimension=dim)
            ]
            cands.sort(key=lambda p: eye_to_block_dist(st, p))
            target = cands[0] if cands else None

            if target is None:
                # 地表无平层裸露石头：从脚边安全方向开凿探槽，核对每列的底板，再沿阶梯下行贴身拾取。
                if stair_origin is None and stair_steps_taken < 6:
                    ix, fy, iz = math.floor(cur[0]), math.floor(cur[1]), math.floor(cur[2])
                    stair_columns.add((ix, iz))
                    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        sx1, sz1 = ix + dx, iz + dz
                        if (
                            (sx1, sz1) in stair_columns
                            or _is_protected([sx1, fy - 1, sz1])
                            or memo.is_blocked([sx1, fy - 1, sz1], dimension=dim)
                        ):
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
                                # 拾取表土可能让角色提前踩进还未清完的阶梯列。
                                dug_res = ex.dig([sx, clear_y, sz], collect=bname not in PEEL_SURFACE)
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
                            if code not in NAV_FAIL_CODES:
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
                            if code not in NAV_FAIL_CODES:
                                raise
                            yield rec("move_to", pos=dest, error_code=code)
                        else:
                            yield rec("move_to", moved, pos=dest)
                        stair_origin = None
                        continue
                raise Replan("附近未找到可安全挖掘的石头或非悬空表土")

            t_key = (int(target[0]), int(target[1]), int(target[2]))
            if not can_reach_block(ex, st, target, reach=DIG_EYE_REACH):
                try:
                    yield rec("move_to", ex.move_to(pos=target, rng=2), pos=target)
                except Replan as e:
                    code, _ = failure(e)
                    if code in NAV_FAIL_CODES:
                        bad_positions.add(t_key)
                        yield rec("move_to", pos=target, error_code=code)
                        continue
                    raise
            try:
                yield from self._prepare_pickaxe(ex, state)
                st, cur = _refresh_stance(ex, state)
                if _is_under_feet(cur, target) or not can_reach_block(ex, st, target, reach=API_DIG_EYE_REACH):
                    bad_positions.add(t_key)
                    continue
                before_dig_cobble = total_cobble(state)
                dug_res = ex.dig(target, collect=True)
                yield rec("dig", dug_res, pos=target)
            except Replan as e:
                code, _ = failure(e)
                if code == "protected_region":
                    _mark_protected(target)
                    memo.mark_protected(target, dimension=dim)
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
