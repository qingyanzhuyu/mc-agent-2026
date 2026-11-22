"""首批高性价比基础生存 L3 技能（对应 docs/11 §二/§七 T1 底仓前置）。

遵守 AGENTS.md §9.2 全部硬规则：
- 只收标量参数，绝不收坐标与实体 id；
- 每发一个阻塞动作立即 `yield rec(...)`；
- 动作一律经由 `Executor`，绝不直调 `Client` 动作方法；
- 每个技能自带 `verify_done(state, before_counts)` 声明自己的成功判据，供主循环核对；
- 默认 `verified = "offline"`，真机验收留档前不混入正式赛默认菜单。
"""
from __future__ import annotations

from collections import deque
import math
import time
from typing import TYPE_CHECKING, Generator, Iterator, Optional

from ..client import ApiError
from ..runtime import EnvBlocked, Replan, has_equipped_armor
from ..skills import nav
from ..skills.base import Param, Skill, failure, rec
from ..skills.items import AIR, MINE_TRAILS, NAV_FAIL_CODES, bare, best_pickaxe, is_clear_space, pick_placeable, total_placeable
from ..skills.mine_return import is_underground_in_mine, return_to_surface_from_mine
from ..skills.recipes import craft_recipe
from ..skills.registry import register
from ..skills.sightline import (
    API_DIG_EYE_REACH, DIG_EYE_REACH, STATION_EYE_REACH,
    block_center, can_reach_block, eye_pos, eye_to_block_dist, voxel_line_cells,
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
    "netherite_axe", "diamond_axe", "iron_axe", "copper_axe", "stone_axe", "golden_axe", "wooden_axe",
)
STONE_Targets: tuple[str, ...] = ("stone", "cobblestone", "deepslate", "cobbled_deepslate")
PEEL_SURFACE: tuple[str, ...] = ("grass_block", "dirt", "coarse_dirt", "podzol", "leaf_litter")
SAFE_GROUND: tuple[str, ...] = (
    "grass_block", "dirt", "coarse_dirt", "stone", "cobblestone",
    "sand", "sandstone", "gravel", "andesite", "diorite", "granite", "tuff", "oak_planks", "pale_oak_planks",
)
EDIBLE_FOODS: tuple[str, ...] = (
    "cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_chicken", "cooked_cod", "cooked_salmon",
    "bread", "baked_potato", "golden_carrot", "apple", "carrot", "melon_slice", "sweet_berries",
    "glow_berries", "beef", "porkchop", "mutton", "chicken",
)
STRUCTURE_FINGERPRINTS: dict[str, tuple[str, ...]] = {
    "village": ("hay_block", "bell", "composter", "lectern"),
    "stronghold": ("stone_bricks", "mossy_stone_bricks", "cracked_stone_bricks", "end_portal_frame"),
    "ruined_portal": ("crying_obsidian", "obsidian", "gold_block"),
    "bee_nest": ("bee_nest",),
}
_SOFT_LOS_BLOCKS: frozenset[str] = frozenset({
    "oak_leaves", "birch_leaves", "spruce_leaves", "jungle_leaves", "acacia_leaves", "dark_oak_leaves",
    "cherry_leaves", "mangrove_leaves", "pale_oak_leaves", "azalea_leaves", "flowering_azalea_leaves",
    "snow", "snow_layer", "short_grass", "tall_grass", "fern", "large_fern", "vine", "dead_bush",
    "cobweb", "leaf_litter", "moss_carpet", "azalea", "flowering_azalea", "sweet_berry_bush", "bamboo",
})

# 运行期动态记录触发过 protected_region 的点位、已放置工作台与脱困落点（非硬编码坐标，符合 §2B.1）
PROTECTED_ZONES: list[tuple[float, float]] = []
VISITED_ANCHORS: list[tuple[float, float]] = []
KNOWN_TABLES: list[tuple[float, float, float]] = []
RECENT_RECOVERY_LANDINGS: deque[tuple[float, float, float]] = deque(maxlen=10)


def reset_world_memo() -> None:
    PROTECTED_ZONES.clear()
    VISITED_ANCHORS.clear()
    KNOWN_TABLES.clear()
    RECENT_RECOVERY_LANDINGS.clear()
    reset_all_world_memos()
    MINE_TRAILS.clear()
    try:
        from ..skills.voxel_cache import reset_all_voxel_caches
        reset_all_voxel_caches()
    except ImportError:
        pass


def _lazy_skill_mod(name: str):
    try:
        return __import__(f"mcagent.skills.{name}", fromlist=[name])
    except ImportError:
        return None


def _voxel_cache(state: "State"):
    mod = _lazy_skill_mod("voxel_cache")
    return mod.voxel_cache_for(state) if mod is not None else None


def _reach_mod():
    return _lazy_skill_mod("reach")


def _escape_mod():
    return _lazy_skill_mod("escape")


def _bounded_move_to(ex: "Executor", pos, *, rng=2, timeout: float = 30.0):
    try:
        return ex.move_to(pos=pos, rng=rng, timeout=timeout, retries=0)
    except TypeError:
        return ex.move_to(pos=pos, rng=rng)


def _record_memo_attr(memo, attr: str, kind: str, pos, dim: str) -> None:
    if callable(fn := getattr(memo, attr, None)):
        try:
            fn(kind, pos, dimension=dim)
        except TypeError:
            fn(kind, pos)


def _record_station(memo, kind: str, pos, dim: str) -> None:
    _record_memo_attr(memo, "record_station", kind, pos, dim)


def _record_landmark(memo, kind: str, pos, dim: str) -> None:
    _record_memo_attr(memo, "record_landmark", kind, pos, dim)


def _record_recovery_landing(arrived) -> None:
    pt = (float(arrived[0]), float(arrived[1]), float(arrived[2]))
    if sum(1 for x, y, z in RECENT_RECOVERY_LANDINGS
           if math.hypot(pt[0] - x, pt[2] - z) <= 5.0 and abs(pt[1] - y) <= 2.0) >= 2:
        raise Replan("探索脱困落点在同一凹坑内反复打转，停止局部恢复并重新规划")
    RECENT_RECOVERY_LANDINGS.append(pt)


def _get_block(ex: "Executor", vc, pos, dim: str = "overworld", *, force: bool = False) -> dict:
    return vc.get_block(ex.client, pos, dimension=dim, force=force) if vc is not None else ex.client.get_block(pos)


def _prefetch_blocks(ex: "Executor", vc, positions, dim: str = "overworld") -> None:
    if vc is None:
        return
    from ..skills.voxel_cache import has_batch_get_blocks
    if has_batch_get_blocks(getattr(ex, "client", None)):
        vc.get_blocks_batch(ex.client, positions, dimension=dim)


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
    return (st if st.get("pos") else {**st, "pos": cur}), cur


def _dim_of(st: dict) -> str:
    return str(st.get("dimension") or "overworld")


def _run_nav_recovery(
    ex: "Executor", state: "State", cur: list[float], *, dest=None, min_displacement: float = 0.8,
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


def _probe_block_name(ex: "Executor", pos: list, vc=None, dim: str = "overworld") -> Optional[str]:
    """只读查询目标格方块名；未加载或未就绪时返回 None。"""
    try:
        return bare(_get_block(ex, vc, pos, dim).get("name"))
    except ApiError as e:
        if e.code in ("not_loaded", "not_ready"):
            return None
        raise


def _clear_los(
    ex: "Executor", state: "State", st: dict, target, *,
    max_clear: int = 2, reach: float = DIG_EYE_REACH, vc=None, dim: str = "overworld",
) -> Generator[dict, None, bool]:
    """优先调用 `reach.clear_line_of_sight` 清理软遮挡，缺失时回退内联软方块清理。"""
    if eye_to_block_dist(st, target) > reach:
        return False
    reach_mod = _reach_mod()
    if reach_mod is not None and hasattr(reach_mod, "clear_line_of_sight"):
        return (yield from reach_mod.clear_line_of_sight(ex, state, target, max_clear=max_clear, reach=reach, protected_fn=_is_protected))
    cur, tcell, eye = st.get("pos") or state.pos, (int(target[0]), int(target[1]), int(target[2])), eye_pos(st)
    ecell, cleared = (math.floor(eye[0]), math.floor(eye[1]), math.floor(eye[2])), 0
    for cell in voxel_line_cells(eye, block_center(tcell)):
        if cleared >= max_clear or cell in (ecell, tcell):
            continue
        bname = _probe_block_name(ex, list(cell), vc=vc, dim=dim)
        if not bname or bname in AIR:
            continue
        if bname not in _SOFT_LOS_BLOCKS or _is_protected(cell) or _is_under_feet(cur, list(cell)):
            break
        try:
            dug = ex.dig(list(cell), collect=False)
            vc and vc.invalidate(cell, dimension=dim)
            yield rec("dig", dug, pos=list(cell))
            cleared += 1
        except Replan as e:
            if (code := failure(e)[0]) in ("unreachable", "dig_timeout", "path_not_found"):
                yield rec("dig", pos=list(cell), error_code=code)
                break
            raise
    return can_reach_block(ex, _refresh_stance(ex, state)[0], tcell, reach=reach)


def _scout_hits(summary: object) -> dict[str, list[str]]:
    blocks_map = (summary.get("blocks") or {}) if isinstance(summary, dict) else {}
    if isinstance(blocks_map, list):
        seen = {bare(b.get("name", "")) for b in blocks_map if isinstance(b, dict)}
    elif isinstance(blocks_map, dict):
        seen = {bare(k) for k in blocks_map}
    else:
        return {}
    return {tag: m for tag, names in STRUCTURE_FINGERPRINTS.items() if (m := [b for b in names if b in seen])}


def _record_scout_landmarks(memo, summary: object, hits: dict[str, list[str]], cur: list[float], dim: str) -> None:
    if not hits:
        return
    blocks_map = (summary.get("blocks") or {}) if isinstance(summary, dict) else {}
    for tag, hit_names in hits.items():
        positions: list[list] = []
        if isinstance(blocks_map, dict):
            for bn in hit_names:
                info = blocks_map.get(bn)
                p = (info.get("nearest") or info.get("pos")) if isinstance(info, dict) else None
                if isinstance(p, (list, tuple)) and len(p) >= 3:
                    positions.append(list(p[:3]))
        elif isinstance(blocks_map, list):
            for b in blocks_map:
                if isinstance(b, dict) and bare(b.get("name", "")) in hit_names:
                    p = b.get("pos")
                    if isinstance(p, (list, tuple)) and len(p) >= 3:
                        positions.append(list(p[:3]))
        _record_landmark(memo, tag, min(positions, key=lambda p: math.dist(cur, p)) if positions else list(cur), dim)


def _is_high_canopy(ex: "Executor", st: dict, cur: list[float], pt: tuple[int, int, int]) -> bool:
    """判断是否为头顶悬空或被遮挡且高差>4格的不可达树冠原木。"""
    same_col = pt[0] == math.floor(cur[0]) and pt[2] == math.floor(cur[2])
    cx, _, cz = block_center(pt)
    return (
        math.hypot(cx - cur[0], cz - cur[2]) < 3.0 and (pt[1] - cur[1]) > 4.0
        and not (same_col and can_reach_block(ex, st, pt, reach=DIG_EYE_REACH))
    )


def _no_wood_blocked() -> EnvBlocked:
    return EnvBlocked(
        "半径 64 内及已知原木台账均没有可砍的裸露原木（或均在保护区/不可达处）",
        missing_predicate="exposed_log", searched_volume="半径 64 + 共享原木台账", wake_on="chunk_moved",
    )


def _is_floating_canopy_log(ex: "Executor", vc, pt: tuple[int, int, int], dim: str) -> bool:
    """检查原木是否悬空在真实地表上方 >=4 格（树干底部已被砍空，move_to(rng=3) 必然 stalled）。"""
    x, y, z = pt
    for dy in (1, 2, 3, 4):
        b = _probe_block_name(ex, [x, y - dy, z], vc=vc, dim=dim)
        if not b or (b not in AIR and not b.endswith("_leaves")):
            return False
    for dy in range(5, 11):
        b = _probe_block_name(ex, [x, y - dy, z], vc=vc, dim=dim)
        if b and b not in AIR and not b.endswith("_leaves"):
            return True
    return False


def _resolve_log_base(
    ex: "Executor", state: "State", vc, pt: tuple[int, int, int], cur: list[float], dim: str,
) -> tuple[tuple[int, int, int], Optional[str]]:
    """沿树干下探到最低一节原木，避免直接寻路高处树冠。"""
    reach_mod = _reach_mod()
    if reach_mod is not None and hasattr(reach_mod, "select_column_base"):
        base = reach_mod.select_column_base(ex.client, state, pt, match_names=ALL_LOGS)
        if isinstance(base, (list, tuple)) and len(base) >= 3:
            bpt = (int(base[0]), int(base[1]), int(base[2]))
            return bpt, _probe_block_name(ex, list(bpt), vc=vc, dim=dim) if bpt != pt else None
    x, y, z, resolved_name = pt[0], pt[1], pt[2], None
    for _ in range(16):
        below = _probe_block_name(ex, [x, y - 1, z], vc=vc, dim=dim)
        if below and below in ALL_LOGS:
            y, resolved_name = y - 1, below
        else:
            break
    return (x, y, z), resolved_name


def _collect_live_woods(
    ex: "Executor", state: "State", st: dict, cur: list[float], dim: str,
    memo: WorldResourceMemo, vc, cur_cell: tuple[int, int, int],
    bad_positions: set[tuple[int, int, int]], blocks: list,
) -> tuple[list[dict], set[tuple[int, int, int]]]:
    cands: list[dict] = []
    live_keys: set[tuple[int, int, int]] = set()
    seen_pts: set[tuple[int, int, int]] = set()
    for b in blocks:
        p = b.get("pos") if isinstance(b, dict) else None
        if not (isinstance(p, list) and len(p) == 3):
            continue
        raw_pt = (int(p[0]), int(p[1]), int(p[2]))
        live_keys.add(raw_pt)
        pt, base_name = _resolve_log_base(ex, state, vc, raw_pt, cur, dim)
        live_keys.add(pt)
        if pt in seen_pts:
            continue
        seen_pts.add(pt)
        if (
            pt in bad_positions or memo.is_blocked(pt, dimension=dim, category="wood")
            or (vc is not None and vc.is_unreachable("gather_wood", pt, dimension=dim, stance_cell=cur_cell))
            or _is_under_feet(cur, list(pt)) or _is_protected(pt)
        ):
            continue
        if _is_high_canopy(ex, st, cur, pt):
            bad_positions.add(pt)
            continue
        cands.append({"pos": [pt[0], pt[1], pt[2]], "name": base_name or bare(b.get("name", "")), "from_memo": False, "memo_status": "live"})
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
    cand = f"{log_name.removesuffix('_log')}_planks"
    return cand if cand in ALL_PLANKS else "oak_planks"


# 配方需求：(所需木板数, 所需木棍数, 所需圆石数)
_GEAR_NEEDS: dict[str, tuple[int, int, int]] = {
    "wooden_pickaxe": (3, 2, 0), "stone_pickaxe": (0, 2, 3), "stone_sword": (0, 1, 2), "furnace": (0, 0, 8),
}


def can_afford_gear(state: "State", item: str, *, assume_no_table: bool = True) -> bool:
    if item == "stick":
        return total_planks(state) + 4 * total_logs(state) >= 2 or state.count("bamboo") >= 2
    if item not in _GEAR_NEEDS:
        return False
    p_need, s_need, c_need = _GEAR_NEEDS[item]
    if total_cobble(state) < c_need:
        return False
    missing_sticks = max(0, s_need - state.count("stick"))
    recipes = ([2] * math.ceil(missing_sticks / 4)
               + ([4] if state.count("crafting_table") < 1 and assume_no_table else [])
               + ([p_need] if p_need else []))
    # ep-0087 local-target 拒绝了 2 birch + 1 oak 合木镐；保守按每次配方同种备料，覆盖任意可用树种。
    potential = tuple(state.count(p) + state.count(lg) * 4 for p, lg in zip(ALL_PLANKS, ALL_LOGS))

    def payable(stacks: tuple[int, ...], rem: list[int]) -> bool:
        if not rem:
            return True
        need = rem[0]
        choices = [i for i, c in enumerate(stacks) if c >= need]
        return bool(choices) and all(
            payable(tuple(c - need if j == i else c for j, c in enumerate(stacks)), rem[1:]) for i in choices
        )

    return payable(potential, recipes)


def _is_valid_pos3(pos: object) -> bool:
    return (
        isinstance(pos, (list, tuple))
        and len(pos) == 3
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in pos)
    )


def _forget_stale_table(state: "State", table: object) -> None:
    if not _is_valid_pos3(table):
        return
    _forget_known_table(table)
    dim = _dim_of(state.st)
    memo_for(state).mark_unreachable(
        table, dimension=dim, category="station", error_code="no_window", block="crafting_table",
    )
    vc = _voxel_cache(state)
    if vc is not None:
        vc.invalidate(table, dimension=dim)


def _wait_craft_inventory_sync(
    ex: "Executor", state: "State", item: str, before: int, *, max_polls: int = 6, sleep_s: float = 0.05,
) -> int:
    """实机 /craft 返回后服务端 set_slot 可能延迟 1-2 tick，短暂复查背包避免误判产出为 0。"""
    state.invalidate_inventory()
    state.refresh_inventory(ex.client)
    if state.count(item) > before:
        return state.count(item)
    for _ in range(max_polls):
        time.sleep(sleep_s)
        state.invalidate_inventory()
        state.refresh_inventory(ex.client)
        if state.count(item) > before:
            return state.count(item)
    return state.count(item)


def _craft_with_space(
    ex: "Executor", state: "State", item: str, *, table_pos: Optional[list] = None,
) -> Iterator[dict]:
    """库存满时尝试用一份现有无附魔食物腾位，no_window 时遗忘失效工作台。"""
    before = state.count(item)
    try:
        result = ex.craft(item, 1)
    except Replan as e:
        code, extra = failure(e)
        if code == "no_window":
            table = extra.get("table")
            _forget_stale_table(state, table if _is_valid_pos3(table) else table_pos)
        if code in ("bad_args", "window_timeout") and ("updateslot" in str(e).lower() or "timeout" in str(e).lower()):
            yield rec("craft", item=item, error_code=code)
            if _wait_craft_inventory_sync(ex, state, item, before) > before:
                return
            _forget_stale_table(state, table_pos)
            raise
        if code != "inventory_full":
            raise
        yield rec("craft", item=item, error_code=code)
        inv = state.refresh_inventory(ex.client)
        gained = extra.get("gained")
        if state.count(item) > before or (
            isinstance(gained, (int, float)) and not isinstance(gained, bool) and gained > 0
        ):
            return
        if inv.get("empty_slots") == 0:
            st = state.refresh_status(ex.client, max_age_s=0)
            raw_food = st.get("food", 20)
            if not isinstance(raw_food, (int, float)) or isinstance(raw_food, bool) or raw_food >= 20:
                raise
            food = next(
                (
                    name
                    for entry in inv.get("items", [])
                    if isinstance(entry, dict)
                    and (name := bare(entry.get("name"))) in EDIBLE_FOODS
                    and entry.get("count") == 1
                    and state.count(name) == 1
                    and not entry.get("custom_name")
                    and not entry.get("enchantments")
                ),
                None,
            )
            if food is None:
                raise
            yield rec("use_item", ex.use_item(food), item=food)
            inv = state.refresh_inventory(ex.client)
        empty_slots = inv.get("empty_slots")
        if not isinstance(empty_slots, int) or isinstance(empty_slots, bool) or empty_slots <= 0:
            raise
        result = ex.craft(item, 1)
    yield rec("craft", result, item=item)
    if state.count(item) <= before:
        _wait_craft_inventory_sync(ex, state, item, before)


def _prepare_recipe_planks(ex: "Executor", state: "State", needed: int, target: str) -> Iterator[dict]:
    """每次配方前现查同种木板；最多 needed 次有进展的转化，动作后立即让出。"""
    for _ in range(needed):
        if max(state.count(p) for p in ALL_PLANKS) >= needed:
            return
        logs = [lg for lg in ALL_LOGS if state.count(lg) > 0]
        if not logs:
            raise Replan(f"合成 {target} 同种木板不足：单次需 {needed}，需补充原木")
        plank_item = _planks_for_log(max(logs, key=lambda lg: (state.count(_planks_for_log(lg)), state.count(lg))))
        before = state.count(plank_item)
        yield from _craft_with_space(ex, state, plank_item)
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
    verified = "live"

    @classmethod
    def blocked(cls, state: "State") -> str:
        return "饱食度已满" if float(state.st.get("food", 20)) >= 20 else ("" if best_food(state) else "背包没有可食用食物")

    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        self._food_before: Optional[float] = None
        self._ate_confirmed: bool = False
        state.refresh_inventory(ex.client)
        item = best_food(state)
        if not item:
            raise Replan("背包没有可食用食物")
        before_cnt = sum(state.count(f) for f in EDIBLE_FOODS)
        raw_food_before = (state.st or {}).get("food")
        if isinstance(raw_food_before, (int, float)) and not isinstance(raw_food_before, bool):
            self._food_before = float(raw_food_before)
        for attempt in range(2):
            res = ex.use_item(item)
            if isinstance(res, dict):
                fb, fa = res.get("food_before"), res.get("food")
                if (
                    isinstance(fb, (int, float)) and not isinstance(fb, bool)
                    and isinstance(fa, (int, float)) and not isinstance(fa, bool)
                    and float(fa) > float(fb)
                ):
                    self._ate_confirmed = True
            yield rec("use_item", res, item=item)
            state.invalidate_inventory()
            state.refresh_inventory(ex.client)
            # 若服务端回执已确认进食且饱食上升，但 Mineflayer 槽位扣减包延迟 50~150ms 未同步，
            # 标脏背包避免下一拍用旧槽位重发 use_item 撞上 missing_item。
            if self._ate_confirmed and sum(state.count(f) for f in EDIBLE_FOODS) >= before_cnt:
                state.invalidate_inventory()
            st_after = state.refresh_status(ex.client, max_age_s=0) or {}
            cur_food_after = float(st_after.get("food", 20))
            if (
                self._ate_confirmed
                or sum(state.count(f) for f in EDIBLE_FOODS) < before_cnt
                or cur_food_after >= 20
                or (self._food_before is not None and cur_food_after > self._food_before)
                or attempt > 0
                or state.count(item) <= 0
            ):
                break

    def verify_done(self, state: "State", before_counts: dict[str, int]) -> str:
        before_food = sum(before_counts.get(f, 0) for f in EDIBLE_FOODS)
        after_food = sum(state.count(f) for f in EDIBLE_FOODS)
        cur_food = float((state.st or {}).get("food", 0))
        ate_by_food_delta = (
            getattr(self, "_ate_confirmed", False)
            or (getattr(self, "_food_before", None) is not None and cur_food > float(self._food_before))
        )
        return (
            "eat_food 结束但食物未消耗且饱食度未满"
            if (after_food >= before_food and cur_food < 20 and not ate_by_food_delta)
            else ""
        )


@register
class ScoutIsland(Skill):
    name = "scout_island"
    description = "扫描周边资源与结构指纹并沿实心陆地向外探索"
    params = {"radius": Param(int, "扫描半径", default=32, min=12, max=64)}
    goal = "explore:island"
    cooldown_s = 12.0
    verified = "live"

    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        st, cur = _refresh_stance(ex, state)
        scout_start_pos = list(cur)
        dim, memo, r = _dim_of(st), memo_for(state), int(self.args["radius"])
        if _is_valid_pos3(cur):
            memo.mark_grid_visited(cur, dimension=dim)
        summary = ex.client.find_blocks(radius=r, limit=64)
        memo.ingest_find_blocks(summary, dimension=dim, origin=st)
        hits = _scout_hits(summary)
        _record_scout_landmarks(memo, summary, hits, cur, dim)

        # 若角色刚在探矿阶梯井底受阻，先分段沿阶梯退回井口地表再外迁。
        if MINE_TRAILS and math.dist(cur, MINE_TRAILS[-1][-1]) <= 16.0 and MINE_TRAILS[-1][0][1] > cur[1] + 2.0:
            trail = MINE_TRAILS[-1]
            indices = list(range(len(trail) - 1, -1, -10)) + ([0] if (len(trail) - 1) % 10 != 0 else [])
            trail_nav_failed = False
            moved_trail = False
            for idx in indices:
                if idx == len(trail) - 1:
                    continue
                wp = list(trail[idx])
                try:
                    moved_up = _bounded_move_to(ex, wp, rng=2, timeout=30.0)
                except Replan as e:
                    code, _ = failure(e)
                    if code not in NAV_FAIL_CODES:
                        raise
                    trail_nav_failed = True
                    yield rec("move_to", pos=wp, fingerprints=hits, error_code=code)
                    break
                moved_trail = True
                yield rec("move_to", moved_up, pos=wp, fingerprints=hits)
            st, cur = _refresh_stance(ex, state)
            if moved_trail and _is_valid_pos3(cur):
                memo.mark_grid_visited(cur, dimension=dim)
            if MINE_TRAILS and MINE_TRAILS[-1] is trail and (cur[1] >= trail[0][1] - 1.5 or (trail_nav_failed and cur[1] >= 62.0)):
                MINE_TRAILS.pop()

        target_wood, cur_wood_dist = None, 0.0
        if total_logs(state) == 0:
            woods = [e for e in memo.query("wood", dimension=dim, origin=st) if e.status in ("untested", "reachable") and not _is_protected(e.pos)]
            if woods and (cand_dist := eye_to_block_dist(st, woods[0].pos)) > 28.0:
                target_wood, cur_wood_dist = woods[0], cand_dist

        # limit 适度放大至 250~350 以覆盖近处地表之外的远端陆地（bot_session.js 上限 500）
        land = ex.client.find_blocks(types=list(SAFE_GROUND), radius=min(r, 28), limit=min(350, max(250, r * 7)))
        visited_grid = memo.visited_grid_cells(dimension=dim)
        cursor = memo.get_spiral_cursor(dimension=dim)
        theta = 2.0 * math.pi * (int(cursor["sector"]) % 8) / 8.0
        sux, suz = math.cos(theta), math.sin(theta)
        spawn = memo.get_spawn(dimension=dim)
        if spawn is not None:
            vx = float(spawn[0]) + float(cursor["radius"]) * sux - float(cur[0])
            vz = float(spawn[2]) + float(cursor["radius"]) * suz - float(cur[2])
            if (vlen := math.hypot(vx, vz)) >= 4.0:
                sux, suz = vx / vlen, vz / vlen
        recent_anchors = VISITED_ANCHORS[-24:]
        naked_on_surface = (
            float(cur[1]) >= 56.0
            and not has_equipped_armor(state)
            and not any(state.count(w) > 0 for w in ("wooden_sword", "stone_sword", "iron_sword", "diamond_sword", "netherite_sword"))
        )
        cands, climb_cands = [], []
        for b in (land.get("blocks") or []):
            p = b.get("pos")
            if not (isinstance(p, list) and len(p) == 3) or _is_protected(p, radius=8.0):
                continue
            if naked_on_surface and float(p[1]) < 54.0:
                continue
            if memo.is_near_void_edge(p, dimension=dim, margin=8.0):
                continue
            dist = math.hypot(p[0] - cur[0], p[2] - cur[2])
            visited_pen = sum(2 if d < 8.0 else (1 if d < 14.0 else 0)
                              for vx, vz in recent_anchors for d in (math.hypot(p[0] - vx, p[2] - vz),))
            in_visited_grid = (math.floor(p[0]) // 32, math.floor(p[2]) // 32) in visited_grid
            # 仅对远距候选（>=12 格）优先奖励未访 32x32 粗网格，避免贴着网格边界的 5-9 格碎步压过 20+ 格远移
            grid_visited_pen = 0 if (not in_visited_grid and dist >= 12.0) else 1
            spiral_proj = (p[0] - cur[0]) * sux + (p[2] - cur[2]) * suz
            if target_wood is not None:
                cw = eye_to_block_dist({"pos": [p[0] + 0.5, p[1] + 1.0, p[2] + 0.5], "sneaking": st.get("sneaking")}, target_wood.pos)
                closer_to_wood = 0 if cw < cur_wood_dist - 8.0 else 1
                wood_order = cw if closer_to_wood == 0 else -dist
            else:
                closer_to_wood, wood_order = 1, -dist
            if dist >= 5.0 and abs(p[1] - cur[1]) <= 10.0:
                cands.append((grid_visited_pen, visited_pen, -dist, closer_to_wood, wood_order, -spiral_proj, p))
            elif dist >= 3.0 and 10.0 < (p[1] - cur[1]) <= 28.0:
                climb_cands.append((grid_visited_pen, visited_pen, p[1] - cur[1], closer_to_wood, wood_order, -spiral_proj, p))

        attempted_recovery, attempted_water_escape = False, False

        def _try_water_escape() -> Generator[dict, None, bool]:
            nonlocal attempted_water_escape, st, cur
            esc = _escape_mod()
            if attempted_water_escape or not st.get("in_water") or esc is None or not hasattr(esc, "escape_water_to_shore"):
                return False
            attempted_water_escape = True
            before_esc = list(cur)
            esc_res = yield from esc.escape_water_to_shore(ex, state)
            st, cur = _refresh_stance(ex, state)
            if esc_res or not st.get("in_water") or math.dist(cur, before_esc) >= 0.8:
                VISITED_ANCHORS.append((float(cur[0]), float(cur[2])))
                if _is_valid_pos3(cur):
                    memo.mark_grid_visited(cur, dimension=dim)
                return True
            return False

        def _try_staircase_climb_out(
            *,
            allow_above_sea: bool = False,
            min_target_y: Optional[float] = None,
            goal_y: Optional[float] = None,
        ) -> Generator[dict, None, bool]:
            """被周围方块围困在矿井/地表凹坑时，优先持镐或徒手挖软壁开 1~4 级 1x2 阶梯，否则用两列交替 step_up_1x2 连续垫块搭路脱困。"""
            nonlocal st, cur
            if not allow_above_sea and cur[1] >= 62.0:
                return False
            vc = _voxel_cache(state)
            unsafe = {"water", "lava", "flowing_water", "flowing_lava", "bedrock", "crafting_table"}
            liquids = {"water", "lava", "flowing_water", "flowing_lava"}
            falling = {"sand", "red_sand", "gravel", "water", "lava", "flowing_water", "flowing_lava"}
            soft_diggable = {
                "dirt", "grass_block", "coarse_dirt", "podzol", "rooted_dirt",
                "sand", "red_sand", "gravel", "mud", "clay", "snow_block", "snow",
                "short_grass", "tall_grass", "leaf_litter",
            }
            harvestable_placeable = {
                "dirt", "grass_block", "coarse_dirt", "podzol", "rooted_dirt",
                "sand", "red_sand", "gravel", "mud", "clay",
            }
            start_y = float(cur[1])
            target_y = max(start_y, float(min_target_y) if min_target_y is not None else start_y)
            climb_cap_y = (
                min(start_y + 5.0, float(goal_y))
                if goal_y is not None and float(goal_y) > start_y + 0.5
                else target_y + 3.5
            )

            def _is_soft(name: str) -> bool:
                return name in soft_diggable or name.endswith("_leaves")

            def _valid_step_dir(ix: int, fy: int, iz: int, dx: int, dz: int, has_pick: bool) -> bool:
                nx, nz = ix + dx, iz + dz
                if _is_protected([nx, fy, nz]) or _is_protected([nx, fy + 1, nz]):
                    return False
                floor_n = bare(_get_block(ex, vc, [nx, fy, nz], dim).get("name", ""))
                if not floor_n or floor_n in AIR or floor_n in unsafe:
                    return False
                roof_n = bare(_get_block(ex, vc, [nx, fy + 3, nz], dim).get("name", ""))
                head_n = bare(_get_block(ex, vc, [ix, fy + 2, iz], dim).get("name", ""))
                s1_n = bare(_get_block(ex, vc, [nx, fy + 1, nz], dim).get("name", ""))
                s2_n = bare(_get_block(ex, vc, [nx, fy + 2, nz], dim).get("name", ""))
                if roof_n in falling or any(bn in unsafe for bn in (head_n, s1_n, s2_n)):
                    return False
                if not has_pick and any(bn and bn not in AIR and not _is_soft(bn) for bn in (head_n, s1_n, s2_n)):
                    return False
                return True

            for _step in range(4):
                if float(cur[1]) > target_y + 1.5:
                    break
                state.refresh_inventory(ex.client)
                pick = best_pickaxe(state)
                ix, fy, iz = math.floor(cur[0]), math.floor(cur[1]), math.floor(cur[2])
                chosen_dir = None
                overhead_cells: list[list[int]] = [[ix, fy + 2, iz]]
                _prefetch_blocks(
                    ex, vc,
                    [[ix, fy + 2, iz]] + [
                        [ix + dx, fy + k, iz + dz]
                        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)) for k in (0, 1, 2, 3)
                    ],
                    dim,
                )
                for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    if _valid_step_dir(ix, fy, iz, dx, dz, bool(pick)):
                        chosen_dir = (ix + dx, fy, iz + dz)
                        break
                if chosen_dir is None:
                    # 凹坑中央四邻均为空时，沿四向扫描 1~3 格内的坑壁并先贴近再向上挖阶梯；
                    # 若 GoalNear(rng>=1) 因距离=1 格原地未动，直接隔空开凿 2 格外的坑壁并一并踏上台阶。
                    for step_dist in (1, 2, 3):
                        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                            mx, mz = ix + dx * step_dist, iz + dz * step_dist
                            if not _valid_step_dir(mx, fy, mz, dx, dz, bool(pick)):
                                continue
                            path_clear = True
                            for k in range(1, step_dist + 1):
                                px, pz = ix + dx * k, iz + dz * k
                                mid_b = _get_block(ex, vc, [px, fy, pz], dim)
                                mid_h = _get_block(ex, vc, [px, fy + 1, pz], dim)
                                mid_f = bare(_get_block(ex, vc, [px, fy - 1, pz], dim).get("name", ""))
                                if not (
                                    mid_f and mid_f not in AIR and mid_f not in unsafe
                                    and is_clear_space(mid_b) and is_clear_space(mid_h)
                                ):
                                    path_clear = False
                                    break
                            if not path_clear:
                                continue
                            step_dest = [mx + 0.5, float(fy), mz + 0.5]
                            try:
                                moved_m = _bounded_move_to(ex, step_dest, rng=1, timeout=15.0)
                            except Replan as e:
                                if failure(e)[0] not in NAV_FAIL_CODES:
                                    raise
                                yield rec("move_to", pos=step_dest, fingerprints=hits, error_code=failure(e)[0])
                                continue
                            yield rec("move_to", moved_m, pos=step_dest, fingerprints=hits)
                            st, cur = _refresh_stance(ex, state)
                            ix, fy, iz = math.floor(cur[0]), math.floor(cur[1]), math.floor(cur[2])
                            if _valid_step_dir(ix, fy, iz, dx, dz, bool(pick)):
                                chosen_dir = (ix + dx, fy, iz + dz)
                                overhead_cells = [[ix, fy + 2, iz]]
                            elif step_dist == 1 and _valid_step_dir(mx, fy, mz, dx, dz, bool(pick)):
                                chosen_dir = (mx + dx, fy, mz + dz)
                                overhead_cells = [[mx, fy + 2, mz]]
                            break
                        if chosen_dir is not None:
                            break
                if chosen_dir is None:
                    break
                nx, fy, nz = chosen_dir
                dig_targets = [
                    p for p in (*overhead_cells, [nx, fy + 1, nz], [nx, fy + 2, nz])
                    if (bn := bare(_get_block(ex, vc, p, dim).get("name", ""))) and bn not in AIR
                ]
                if dig_targets and pick and bare((state.st.get("held") or {}).get("name")) != pick:
                    yield rec("equip", ex.equip(pick, "hand"), item=pick)
                    st, cur = _refresh_stance(ex, state)
                for dig_pos in dig_targets:
                    try:
                        yield from nav.dig_with_recovery(ex, state, dig_pos, collect=False)
                    except Replan as e:
                        if failure(e)[0] == "nothing_to_dig":
                            vc and vc.invalidate(dig_pos, dimension=dim)
                            yield rec("dig", pos=dig_pos, error_code="nothing_to_dig")
                            continue
                        raise
                    vc and vc.invalidate(dig_pos, dimension=dim)
                up_dest = [nx + 0.5, float(fy + 1), nz + 0.5]
                before_step_pos = list(cur)
                try:
                    moved_u = _bounded_move_to(ex, up_dest, rng=1, timeout=20.0)
                except Replan as e:
                    if failure(e)[0] not in NAV_FAIL_CODES:
                        raise
                    yield rec("move_to", pos=up_dest, fingerprints=hits, error_code=failure(e)[0])
                    break
                yield rec("move_to", moved_u, pos=up_dest, fingerprints=hits)
                st, cur = _refresh_stance(ex, state)
                if math.dist(cur, before_step_pos) < 0.35:
                    break

            escaped_up = float(cur[1]) > target_y + 0.5
            # 挖阶梯未成功抬升时，用两列交替 step_up_1x2 连续搭块攀升（最多 5 级），
            # 背包无垫块时优先就近安全挖取 1~3 块软土充当垫脚料；途中遇到高一格坑沿即直接跨出。
            if not escaped_up:
                state.refresh_inventory(ex.client)
                if total_placeable(state) == 0:
                    ix, fy, iz = math.floor(cur[0]), math.floor(cur[1]), math.floor(cur[2])
                    harvested = 0
                    for dy_off in (0, -1):
                        if harvested >= 3:
                            break
                        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                            if harvested >= 3:
                                break
                            hp = [ix + dx, fy + dy_off, iz + dz]
                            if _is_protected(hp):
                                continue
                            hname = bare(_get_block(ex, vc, hp, dim).get("name", ""))
                            if hname not in harvestable_placeable:
                                continue
                            if dy_off == -1:
                                under_h = bare(_get_block(ex, vc, [hp[0], hp[1] - 1, hp[2]], dim).get("name", ""))
                                if not under_h or under_h in AIR or under_h in unsafe:
                                    continue
                            near_liquid = any(
                                bare(_get_block(ex, vc, [hp[0] + adx, hp[1] + ady, hp[2] + adz], dim).get("name", "")) in liquids
                                for adx, ady, adz in ((0, 1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))
                            )
                            if near_liquid:
                                continue
                            try:
                                yield from nav.dig_with_recovery(ex, state, hp, collect=True)
                            except Replan as e:
                                code_h, _ = failure(e)
                                if code_h in ("replan", "interrupted") or "抢占" in str(e):
                                    raise
                                continue
                            vc and vc.invalidate(hp, dimension=dim)
                            harvested += 1
                    if harvested > 0:
                        state.refresh_inventory(ex.client)
                        st, cur = _refresh_stance(ex, state)

                # 宽坑中央搭柱前先贴近坑壁（1~3 格内），确保搭到顶后可直接跨上坑沿而非悬在坑中央孤柱上
                if total_placeable(state) > 0:
                    ix, fy, iz = math.floor(cur[0]), math.floor(cur[1]), math.floor(cur[2])
                    adjacent_wall = any(
                        (wn := bare(_get_block(ex, vc, [ix + dx, fy, iz + dz], dim).get("name", "")))
                        and wn not in AIR and wn not in unsafe
                        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
                    )
                    if not adjacent_wall:
                        approached = False
                        for step_dist in (1, 2, 3):
                            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                                wx, wz = ix + dx * (step_dist + 1), iz + dz * (step_dist + 1)
                                wall_n = bare(_get_block(ex, vc, [wx, fy, wz], dim).get("name", ""))
                                if not wall_n or wall_n in AIR or wall_n in unsafe:
                                    continue
                                path_clear = True
                                for k in range(1, step_dist + 1):
                                    px, pz = ix + dx * k, iz + dz * k
                                    mid_b = _get_block(ex, vc, [px, fy, pz], dim)
                                    mid_h = _get_block(ex, vc, [px, fy + 1, pz], dim)
                                    mid_f = bare(_get_block(ex, vc, [px, fy - 1, pz], dim).get("name", ""))
                                    if not (
                                        mid_f and mid_f not in AIR and mid_f not in unsafe
                                        and is_clear_space(mid_b) and is_clear_space(mid_h)
                                    ):
                                        path_clear = False
                                        break
                                if not path_clear:
                                    continue
                                wall_dest = [ix + dx * step_dist + 0.5, float(fy), iz + dz * step_dist + 0.5]
                                try:
                                    moved_w = _bounded_move_to(ex, wall_dest, rng=1, timeout=15.0)
                                except Replan as e:
                                    if failure(e)[0] not in NAV_FAIL_CODES:
                                        raise
                                    yield rec("move_to", pos=wall_dest, fingerprints=hits, error_code=failure(e)[0])
                                    continue
                                yield rec("move_to", moved_w, pos=wall_dest, fingerprints=hits)
                                st, cur = _refresh_stance(ex, state)
                                approached = True
                                break
                            if approached:
                                break

                pillar_cols: set[tuple[int, int]] = set()
                for _layer in range(6):
                    if float(cur[1]) >= climb_cap_y - 0.2:
                        break
                    ix, fy, iz = math.floor(cur[0]), math.floor(cur[1]), math.floor(cur[2])
                    head_blk = _get_block(ex, vc, [ix, fy + 2, iz], dim)
                    if not is_clear_space(head_blk):
                        head_name = bare(head_blk.get("name", ""))
                        roof_above = bare(_get_block(ex, vc, [ix, fy + 3, iz], dim).get("name", ""))
                        state.refresh_inventory(ex.client)
                        pick = best_pickaxe(state)
                        can_clear_head = (
                            bool(head_name)
                            and head_name not in unsafe
                            and roof_above not in falling
                            and not _is_protected([ix, fy + 2, iz])
                            and (bool(pick) or _is_soft(head_name))
                        )
                        if not can_clear_head:
                            break
                        if pick and bare((state.st.get("held") or {}).get("name")) != pick:
                            yield rec("equip", ex.equip(pick, "hand"), item=pick)
                            st, cur = _refresh_stance(ex, state)
                        try:
                            yield from nav.dig_with_recovery(ex, state, [ix, fy + 2, iz], collect=False)
                        except Replan as e:
                            code_d, _ = failure(e)
                            if code_d in ("replan", "interrupted") or "抢占" in str(e):
                                raise
                            break
                        vc and vc.invalidate([ix, fy + 2, iz], dimension=dim)

                    # 已至少抬升 1 格后，先检查是否已触达非搭柱坑沿（高 1 格且上方两格净空）：跨出无需额外垫块
                    if _layer > 0:
                        stepped_rim = False
                        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                            rx, rz = ix + dx, iz + dz
                            if (rx, rz) in pillar_cols or _is_protected([rx, fy, rz]):
                                continue
                            rim_floor = bare(_get_block(ex, vc, [rx, fy, rz], dim).get("name", ""))
                            if not rim_floor or rim_floor in AIR or rim_floor in unsafe:
                                continue
                            if not (
                                is_clear_space(_get_block(ex, vc, [rx, fy + 1, rz], dim))
                                and is_clear_space(_get_block(ex, vc, [rx, fy + 2, rz], dim))
                            ):
                                continue
                            rim_dest = [rx + 0.5, float(fy + 1), rz + 0.5]
                            before_rim_y = float(cur[1])
                            try:
                                moved_r = _bounded_move_to(ex, rim_dest, rng=1, timeout=20.0)
                            except Replan as e:
                                if failure(e)[0] not in NAV_FAIL_CODES:
                                    raise
                                yield rec("move_to", pos=rim_dest, fingerprints=hits, error_code=failure(e)[0])
                                continue
                            yield rec("move_to", moved_r, pos=rim_dest, fingerprints=hits)
                            st, cur = _refresh_stance(ex, state)
                            if float(cur[1]) >= before_rim_y + 0.5:
                                stepped_rim = True
                                break
                        if stepped_rim:
                            break

                    if _layer >= 5:
                        break
                    state.refresh_inventory(ex.client)
                    avail_blocks = total_placeable(state)
                    if avail_blocks < 1:
                        break

                    # 挑选相邻列构建 1 块或两列交替 2 块台阶
                    step_options: list[tuple[tuple[int, int, int], int, int, list[list[int]]]] = []
                    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nx, nz = ix + dx, iz + dz
                        if _is_protected([nx, fy, nz]):
                            continue
                        if not (
                            is_clear_space(_get_block(ex, vc, [nx, fy, nz], dim))
                            and is_clear_space(_get_block(ex, vc, [nx, fy + 1, nz], dim))
                            and is_clear_space(_get_block(ex, vc, [nx, fy + 2, nz], dim))
                        ):
                            continue
                        below1_blk = _get_block(ex, vc, [nx, fy - 1, nz], dim)
                        below1 = bare(below1_blk.get("name", ""))
                        place_seq: list[list[int]] = []
                        if below1 and below1 not in AIR and below1 not in unsafe:
                            place_seq = [[nx, fy, nz]]
                        elif is_clear_space(below1_blk) and not _is_protected([nx, fy - 1, nz]):
                            below2 = bare(_get_block(ex, vc, [nx, fy - 2, nz], dim).get("name", ""))
                            if below2 and below2 not in AIR and below2 not in unsafe:
                                place_seq = [[nx, fy - 1, nz], [nx, fy, nz]]
                        if not place_seq or avail_blocks < len(place_seq):
                            continue
                        is_partner = 0 if (_layer > 0 and (nx, nz) in pillar_cols) else 1
                        near_wall = 0 if any(
                            (wn := bare(_get_block(ex, vc, [nx + wdx, fy + 1, nz + wdz], dim).get("name", "")))
                            and wn not in AIR and wn not in unsafe
                            for wdx, wdz in ((1, 0), (-1, 0), (0, 1), (0, -1))
                        ) else 1
                        step_options.append(((is_partner, near_wall, len(place_seq)), nx, nz, place_seq))

                    if not step_options:
                        break
                    step_options.sort(key=lambda item: item[0])
                    climbed_layer = False
                    for _, nx, nz, place_seq in step_options:
                        up_dest = [nx + 0.5, float(fy + 1), nz + 0.5]
                        before_y = float(cur[1])
                        try:
                            pref = pick_placeable(state)
                            use_item = pref if state.count(pref) >= len(place_seq) else None
                            yield from nav.step_up_1x2(ex, state, place_seq, item=use_item)
                            for placed_p in place_seq:
                                vc and vc.invalidate(placed_p, dimension=dim)
                            moved_u = _bounded_move_to(ex, up_dest, rng=1, timeout=20.0)
                            yield rec("move_to", moved_u, pos=up_dest, fingerprints=hits)
                            st, cur = _refresh_stance(ex, state)
                        except Replan as e:
                            code_s, _ = failure(e)
                            if code_s in ("replan", "interrupted") or "抢占" in str(e):
                                raise
                            if code_s in NAV_FAIL_CODES:
                                yield rec("move_to", pos=up_dest, fingerprints=hits, error_code=code_s)
                            continue
                        if float(cur[1]) >= before_y + 0.5:
                            pillar_cols.add((ix, iz))
                            pillar_cols.add((nx, nz))
                            climbed_layer = True
                            break
                    if not climbed_layer:
                        break
                escaped_up = float(cur[1]) > target_y + 0.5

            if escaped_up:
                RECENT_RECOVERY_LANDINGS.clear()
                VISITED_ANCHORS.append((float(cur[0]), float(cur[2])))
                if _is_valid_pos3(cur):
                    memo.mark_grid_visited(cur, dimension=dim)
            return escaped_up

        def _recover_with_guard(dest_hint) -> Generator[dict, None, bool]:
            nonlocal st, cur
            escaped, arrived = yield from _run_nav_recovery(ex, state, cur, dest=dest_hint)
            if not escaped:
                return False
            st, cur = _refresh_stance(ex, state)
            try:
                _record_recovery_landing(arrived)
            except Replan:
                if (yield from _try_staircase_climb_out(
                    allow_above_sea=True, min_target_y=max(float(cur[1]), float(scout_start_pos[1])),
                )):
                    return True
                raise
            VISITED_ANCHORS.append((float(arrived[0]), float(arrived[2])))
            if _is_valid_pos3(arrived):
                memo.mark_grid_visited(arrived, dimension=dim)
            return True

        def _try_underground_surface_escape() -> Generator[dict, None, bool]:
            """身处地下矿坑且近场石壁全无两格净空时，优先回已知地表站点/草地，否则安全向上挖阶梯脱困。"""
            nonlocal st, cur
            # 1) 优先尝试回到已知地表工作站或高处草地
            surface_targets: list[list[float]] = []
            station_cells: list[tuple[int, int, int]] = []
            if hasattr(memo, "stations"):
                station_cells.extend(memo.stations(dimension=dim))
            for kt in KNOWN_TABLES:
                station_cells.append((math.floor(kt[0]), math.floor(kt[1]), math.floor(kt[2])))
            seen_st: set[tuple[int, int, int]] = set()
            for sc in station_cells:
                if sc in seen_st or sc[1] <= cur[1] + 1.5 or _is_protected(sc):
                    continue
                seen_st.add(sc)
                if eye_to_block_dist(st, sc) <= 48.0:
                    surface_targets.append([sc[0] + 0.5, float(sc[1]), sc[2] + 0.5])
            try:
                grass = ex.client.find_blocks(types=["grass_block", "sand"], radius=min(max(r, 32), 48), limit=30)
                for gb in (grass.get("blocks") or []):
                    gp = gb.get("pos")
                    if isinstance(gp, list) and len(gp) == 3 and gp[1] > cur[1] + 1.5 and not _is_protected(gp, radius=8.0):
                        surface_targets.append([gp[0] + 0.5, float(gp[1] + 1), gp[2] + 0.5])
            except ApiError:
                pass
            surface_targets.sort(
                key=lambda t: (
                    abs(t[1] - cur[1]) > 24.0,
                    math.hypot(t[0] - cur[0], t[2] - cur[2]) < 2.5,
                    math.hypot(t[0] - cur[0], t[2] - cur[2]),
                )
            )
            for dest in surface_targets[:4]:
                before_s = list(cur)
                try:
                    moved_s = _bounded_move_to(ex, dest, rng=2, timeout=30.0)
                except Replan as e:
                    if failure(e)[0] not in NAV_FAIL_CODES:
                        raise
                    yield rec("move_to", pos=dest, fingerprints=hits, error_code=failure(e)[0])
                    st, cur = _refresh_stance(ex, state)
                    continue
                yield rec("move_to", moved_s, pos=dest, fingerprints=hits)
                st, cur = _refresh_stance(ex, state)
                # 防止官方服务端因同 X/Z（水平距 <= rng）直接返 ok=true 却原地未攀升（如地下 Y=52 目标正上方 Y=63）
                climbed = (float(cur[1]) - float(before_s[1])) >= 0.8
                reached = math.dist(cur, dest) <= 3.0
                if math.dist(cur, before_s) < 0.8 or (not climbed and not reached):
                    continue
                VISITED_ANCHORS.append((float(cur[0]), float(cur[2])))
                if _is_valid_pos3(cur):
                    memo.mark_grid_visited(cur, dimension=dim)
                return True

            # 2) 若周边全封死在地下或地表凹坑，优先安全向上开 1x2 阶梯或连续交替垫块脱困
            surface_goal_y = min((float(t[1]) for t in surface_targets), default=None)
            return (yield from _try_staircase_climb_out(
                allow_above_sea=bool(surface_targets),
                goal_y=surface_goal_y,
            ))

        if not cands and not climb_cands:
            if (yield from _try_water_escape()):
                return
            if (yield from _try_underground_surface_escape()):
                return
            if (yield from _recover_with_guard(list(target_wood.pos) if target_wood is not None else None)):
                return
            raise Replan("周边未找到可安全落脚的新陆地方块（防虚空不盲走）")

        # 落水时先找最近的干燥落脚点；在陆地上优先低惩罚组，缺木时同组优先靠近已知远木，否则优先沿螺旋外推方向探索未访问 32x32 网格。
        cands.sort(
            key=(lambda x: (-x[2], x[1]))
            if st.get("in_water")
            else (lambda x: (x[3], x[0], x[1], x[4] if target_wood is not None else x[5], x[2]))
        )
        climb_cands.sort(key=lambda x: (x[0], x[1], x[2]))
        checked, attempts, last_nav_err = [], 0, None
        for *_, chosen in cands + climb_cands:
            dest = [chosen[0] + 0.5, chosen[1] + 1, chosen[2] + 0.5]
            if any(math.dist(dest, old) < 3.0 for old in checked):
                continue
            if len(checked) >= 16:
                break
            checked.append(dest)
            try:
                if any(not is_clear_space(ex.client.get_block([chosen[0], chosen[1] + dy, chosen[2]])) for dy in (1, 2)):
                    continue
            except ApiError as e:
                if e.code == "not_loaded":
                    continue
                raise
            attempts += 1
            before_c = list(cur)
            try:
                moved = _bounded_move_to(ex, dest, rng=2, timeout=30.0)
            except Replan as e:
                code, _ = failure(e)
                if code not in NAV_FAIL_CODES:
                    raise
                last_nav_err = e
                yield rec("move_to", pos=dest, fingerprints=hits, error_code=code)
                st, cur = _refresh_stance(ex, state)
                if code == "stalled" and (yield from _try_water_escape()):
                    return
                if code == "stalled" and not attempted_recovery:
                    attempted_recovery = True
                    if (yield from _recover_with_guard(dest)):
                        return
                if attempts >= 3:
                    break
                continue
            yield rec("move_to", moved, pos=dest, fingerprints=hits)
            st, cur = _refresh_stance(ex, state)
            arrived = list(cur)
            # 防止同 X/Z 假成功：原地未位移且距目标仍 > 3 格时不视为到达，继续尝试或转脱困
            if math.dist(arrived, before_c) < 0.8 and math.dist(arrived, dest) > 3.0:
                if attempts >= 3:
                    break
                continue
            VISITED_ANCHORS.append((float(arrived[0]), float(arrived[2])))
            if _is_valid_pos3(arrived):
                memo.mark_grid_visited(arrived, dimension=dim)
            return

        if (yield from _try_water_escape()):
            return
        if attempts == 0 and (yield from _try_underground_surface_escape()):
            return
        # 周边陆地站位明显高于自身（>= 2 格深坑：候选站位 y+1 > cur.y + 1.5）时，
        # 优先向上挖阶梯或两列交替垫块爬出，避免先跑水平脱困把刚垫高的一格又走回坑底。
        high_stand_ys = [float(c[-1][1] + 1.0) for c in cands + climb_cands if float(c[-1][1] + 1.0) > cur[1] + 1.5]
        if high_stand_ys:
            if (yield from _try_staircase_climb_out(allow_above_sea=True, goal_y=min(high_stand_ys))):
                return
        if not attempted_recovery:
            fallback_dest = cands[0][-1] if cands else (list(target_wood.pos) if target_wood is not None else None)
            if (yield from _recover_with_guard(fallback_dest)):
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
    verified = "live"

    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        state.refresh_status(ex.client, max_age_s=0)
        state.refresh_inventory(ex.client)
        start_logs, want, memo, vc = total_logs(state), int(self.args["count"]), memo_for(state), _voxel_cache(state)

        axe = best_axe(state)
        if axe and bare((state.st.get("held") or {}).get("name")) != axe:
            yield rec("equip", ex.equip(axe, "hand"), item=axe)

        st0, cur0 = _refresh_stance(ex, state)
        if is_underground_in_mine(ex, state):
            near_logs = ex.client.find_blocks(types=list(ALL_LOGS), radius=10, limit=8) or {}
            has_level_log = any(
                isinstance(b.get("pos"), list) and len(b["pos"]) == 3
                and abs(float(b["pos"][1]) - float(cur0[1])) <= 4.0
                for b in (near_logs.get("blocks") or [])
            )
            if not has_level_log:
                returned = yield from return_to_surface_from_mine(ex, state)
                if not returned and is_underground_in_mine(ex, state):
                    raise Replan("身处地下矿道且尚未沿主矿井返回地表，停止盲搜地表原木")

        preferred_species: Optional[str] = next((lg for lg in ALL_LOGS if state.count(lg) >= 1), None)
        bad_positions: set[tuple[int, int, int]] = set()
        last_nav_err: Optional[Replan] = None
        used_recovery, attempts, stale_memo_skips = False, 0, 0

        def _mark_nav_unreachable(err: Replan, code_str: str) -> None:
            nonlocal last_nav_err
            bad_positions.add(t_key)
            memo.mark_unreachable(t_key, dimension=dim, category="wood", error_code=code_str, block=chosen["name"])
            vc and vc.mark_unreachable("gather_wood", t_key, dimension=dim, stance_cell=cur_cell)
            if target[1] > cur[1] + 3:
                bad_positions.update((t_key[0], t_key[1] + dy, t_key[2]) for dy in range(5))
            last_nav_err = err

        while total_logs(state) - start_logs < want and attempts < want + 6 and stale_memo_skips < 32:
            st, cur = _refresh_stance(ex, state)
            dim = _dim_of(st)
            cur_cell = (math.floor(cur[0]), math.floor(cur[1]), math.floor(cur[2]))
            vc and vc.clear_stance_blacklist_if_moved(cur)

            found = ex.client.find_blocks(types=list(ALL_LOGS), radius=32, limit=32)
            memo.ingest_find_blocks(found, dimension=dim, origin=st, category="wood")
            vc and vc.ingest_find_blocks(found, dimension=dim, client=ex.client)
            cands, live_keys = _collect_live_woods(ex, state, st, cur, dim, memo, vc, cur_cell, bad_positions, found.get("blocks") or [])

            if not cands:
                found64 = ex.client.find_blocks(types=list(ALL_LOGS), radius=64, limit=64)
                memo.ingest_find_blocks(found64, dimension=dim, origin=st, category="wood")
                vc and vc.ingest_find_blocks(found64, dimension=dim, client=ex.client)
                cands, live_keys = _collect_live_woods(ex, state, st, cur, dim, memo, vc, cur_cell, bad_positions, found64.get("blocks") or [])
                for entry in memo.query("wood", dimension=dim, origin=st):
                    pt = entry.pos
                    if (pt in live_keys or pt in bad_positions
                            or (vc and vc.is_unreachable("gather_wood", pt, dimension=dim, stance_cell=cur_cell))
                            or _is_under_feet(cur, pt) or _is_protected(pt)):
                        continue
                    if _is_high_canopy(ex, st, cur, pt):
                        bad_positions.add(pt)
                        continue
                    cands.append({"pos": [pt[0], pt[1], pt[2]], "name": entry.block, "from_memo": True, "memo_status": entry.status})

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
            cands.sort(key=lambda b: (
                0 if not b["from_memo"] else 1,
                0 if b["memo_status"] in ("live", "reachable", "untested") else 1,
                float(b["pos"][1]) > cur[1] + 4.0 and eye_to_block_dist(st, b["pos"]) > DIG_EYE_REACH,
                0 if (preferred_species and bare(b.get("name", "")) == preferred_species and eye_to_block_dist(st, b["pos"]) <= 18.0) else 1,
                0 if eye_to_block_dist(st, b["pos"]) <= DIG_EYE_REACH else 1,
                round(math.hypot(block_center(b["pos"])[0] - cur[0], block_center(b["pos"])[2] - cur[2]), 3),
                0 if b["memo_status"] == "reachable" else 1,
                float(b["pos"][1]),
            ))
            chosen = cands[0]
            target, from_memo = chosen["pos"], bool(chosen["from_memo"])
            t_key = (int(target[0]), int(target[1]), int(target[2]))

            if from_memo and eye_to_block_dist(st, target) <= 36.0:
                probed = _probe_block_name(ex, target, vc=vc, dim=dim)
                if probed is not None and probed not in ALL_LOGS:
                    memo.mark_depleted(t_key, dimension=dim, category="wood")
                    bad_positions.add(t_key)
                    stale_memo_skips += 1
                    continue
                if probed is None and eye_to_block_dist(st, target) <= DIG_EYE_REACH:
                    bad_positions.add(t_key)
                    stale_memo_skips += 1
                    continue

            if (eye_to_block_dist(st, target) > DIG_EYE_REACH
                    and _is_floating_canopy_log(ex, vc, t_key, dim)):
                bad_positions.update((t_key[0], t_key[1] + dy, t_key[2]) for dy in range(5))
                memo.mark_unreachable(
                    t_key, dimension=dim, category="wood", error_code="floating_canopy", block=chosen["name"],
                )
                vc and vc.mark_unreachable("gather_wood", t_key, dimension=dim, stance_cell=cur_cell)
                stale_memo_skips += 1
                continue

            attempts += 1
            moved_for_target = False
            if (from_memo and eye_to_block_dist(st, target) > 36.0) or not can_reach_block(ex, st, target, reach=DIG_EYE_REACH):
                if not (from_memo and eye_to_block_dist(st, target) > 36.0) and eye_to_block_dist(st, target) <= DIG_EYE_REACH:
                    yield from _clear_los(ex, state, st, target, max_clear=2, reach=DIG_EYE_REACH, vc=vc, dim=dim)
                    st, cur = _refresh_stance(ex, state)
                if (from_memo and eye_to_block_dist(st, target) > 36.0) or not can_reach_block(ex, st, target, reach=DIG_EYE_REACH):
                    try:
                        yield rec("move_to", _bounded_move_to(ex, target, rng=3, timeout=30.0), pos=target)
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
                    st, cur = _refresh_stance(ex, state)
                    if eye_to_block_dist(st, target) <= DIG_EYE_REACH and not can_reach_block(ex, st, target, reach=DIG_EYE_REACH):
                        yield from _clear_los(ex, state, st, target, max_clear=2, reach=DIG_EYE_REACH, vc=vc, dim=dim)

            if from_memo and moved_for_target:
                st, cur = _refresh_stance(ex, state)
                probed = _probe_block_name(ex, target, vc=vc, dim=dim)
                if probed is not None and probed not in ALL_LOGS:
                    memo.mark_depleted(t_key, dimension=dim, category="wood")
                    bad_positions.add(t_key)
                    continue
                if probed is None:
                    bad_positions.add(t_key)
                    continue

            try:
                dug_res = yield from nav.dig_with_recovery(ex, state, target, collect=True)
                if vc is not None:
                    vc.invalidate(t_key, dimension=dim)
                memo.mark_reachable(t_key, dimension=dim, category="wood", block=chosen["name"])
                after_name = _probe_block_name(ex, target, vc=vc, dim=dim)
                if after_name is not None and after_name not in ALL_LOGS:
                    memo.mark_depleted(t_key, dimension=dim, category="wood")
            except Replan as e:
                code, _ = failure(e)
                if code == "protected_region":
                    _mark_protected(target)
                    memo.mark_protected(t_key, dimension=dim, category="wood", block=chosen["name"])
                    raise Replan(f"原木 {target} 位于出生点保护区，已标记外迁") from e
                if code in ("unreachable", "dig_timeout", "path_not_found"):
                    bad_positions.add(t_key)
                    memo.mark_unreachable(t_key, dimension=dim, category="wood", error_code=code, block=chosen["name"])
                    if vc is not None:
                        vc.mark_unreachable("gather_wood", t_key, dimension=dim, stance_cell=cur_cell)
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
        return f"gather_wood 正常结束但原木未增加（before={before}, after={total_logs(state)}）" if total_logs(state) <= before else ""


@register
class CraftGear(Skill):
    name = "craft_gear"
    description = "自动补齐木板木棍、放置或复用工作台并合成基础工具与熔炉"
    params = {"item": Param(str, "要合成的物品", choices=("wooden_pickaxe", "stone_pickaxe", "stone_sword", "furnace", "stick"), default="wooden_pickaxe")}
    goal = "craft:{item}"
    cooldown_s = 20.0
    verified = "live"

    @classmethod
    def blocked(cls, state: "State") -> str:
        return "缺少原木、木板或圆石原料" if (total_logs(state) + total_planks(state) + total_cobble(state) < 2 and not can_afford_gear(state, "stick")) else ""

    def can_start(self, state: "State") -> bool:
        target = str(self.args["item"])
        if state.count(target) >= 1 or (target == "wooden_pickaxe" and best_pickaxe(state) is not None):
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
        dim, memo = _dim_of(st), memo_for(state)
        state.refresh_inventory(ex.client)
        if total_cobble(state) < c_need:
            raise Replan(f"合成 {target} 圆石不足：需 {c_need}，现 {total_cobble(state)}")

        nearby_resp = ex.client.find_blocks(types=["crafting_table"], radius=16, limit=5)
        memo.ingest_find_blocks(nearby_resp, dimension=dim, origin=st, category="station")
        valid_tables: list[dict] = []
        seen_table_cells: set[tuple[int, int, int]] = set()

        def _add_table_cand(pos_raw, *, from_memo: bool) -> None:
            if not _is_valid_pos3(pos_raw) or _is_protected(pos_raw):
                return
            if memo.is_blocked(pos_raw, dimension=dim, category="station"):
                return
            cell = (math.floor(pos_raw[0]), math.floor(pos_raw[1]), math.floor(pos_raw[2]))
            if cell in seen_table_cells:
                return
            if eye_to_block_dist(st, pos_raw) > 64.0:
                return
            seen_table_cells.add(cell)
            valid_tables.append({"name": "crafting_table", "pos": [cell[0], cell[1], cell[2]], "from_memo": from_memo})

        for b in (nearby_resp.get("blocks") or []):
            if isinstance(b.get("pos"), list) and len(b["pos"]) == 3 and not memo.is_blocked(b["pos"], dimension=dim, category="station"):
                _add_table_cand(b["pos"], from_memo=False)
        for e in memo.query("station", dimension=dim, origin=st):
            if e.block == "crafting_table" and e.status in ("untested", "reachable"):
                _add_table_cand(e.pos, from_memo=True)
        if hasattr(memo, "stations"):
            for spos in memo.stations("crafting_table", dimension=dim):
                _add_table_cand(spos, from_memo=True)
        camp = memo.get_camp(dimension=dim) if hasattr(memo, "get_camp") else None
        if isinstance(camp, dict) and camp.get("crafting_table") is not None:
            _add_table_cand(camp["crafting_table"], from_memo=True)
        for kpos in KNOWN_TABLES:
            _add_table_cand(kpos, from_memo=True)
        valid_tables.sort(key=lambda b: eye_to_block_dist(st, b["pos"]))

        cur_pos_now = st.get("pos") or state.pos
        underground_now = is_underground_in_mine(ex, state)
        local_level_tables = [
            tb for tb in valid_tables
            if abs(float(tb["pos"][1]) - float(cur_pos_now[1])) <= 6.0
            and eye_to_block_dist(st, tb["pos"]) <= 16.0
            and (not tb.get("from_memo") or _probe_block_name(ex, tb["pos"]) == "crafting_table")
        ]
        if underground_now and not local_level_tables:
            if can_afford_gear(state, target, assume_no_table=True):
                # 地下有富余木材或随身工作台时优先就地放台登记，避免长途折返地表
                valid_tables = []
            elif valid_tables and any(float(tb["pos"][1]) > float(cur_pos_now[1]) + 2.0 for tb in valid_tables):
                yield from return_to_surface_from_mine(ex, state)
                st, cur_pos_now = _refresh_stance(ex, state)
        elif (
            valid_tables
            and MINE_TRAILS
            and math.dist(cur_pos_now, MINE_TRAILS[-1][-1]) <= 16.0
            and MINE_TRAILS[-1][0][1] > cur_pos_now[1] + 2.0
            and any(tb["pos"][1] > cur_pos_now[1] + 2.0 for tb in valid_tables)
        ):
            yield from return_to_surface_from_mine(ex, state)
            st, cur_pos_now = _refresh_stance(ex, state)

        table_ready = False
        active_table_pos: Optional[list] = None
        for tb in valid_tables:
            tpos, from_memo = tb["pos"], bool(tb.get("from_memo"))
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
                    moved = _bounded_move_to(ex, tpos, rng=2, timeout=30.0)
                except Replan as e:
                    code, _ = failure(e)
                    if code not in NAV_FAIL_CODES:
                        raise
                    _forget_known_table(tpos)
                    memo.mark_unreachable(tpos, dimension=dim, category="station", error_code=code, block="crafting_table")
                    yield rec("move_to", pos=tpos, error_code=code)
                    st, _ = _refresh_stance(ex, state)
                    continue
                yield rec("move_to", moved, pos=tpos)
                st, _ = _refresh_stance(ex, state)
                if from_memo and (probed := _probe_block_name(ex, tpos)) != "crafting_table":
                    _forget_known_table(tpos)
                    if probed is not None:
                        memo.mark_depleted(tpos, dimension=dim, category="station")
                    continue
                if not can_reach_block(ex, st, tpos, reach=STATION_EYE_REACH):
                    _forget_known_table(tpos)
                    memo.mark_unreachable(tpos, dimension=dim, category="station", error_code="unreachable", block="crafting_table")
                    continue
            _record_known_table(tpos)
            memo.mark_reachable(tpos, dimension=dim, category="station", block="crafting_table")
            _record_station(memo, "crafting_table", tpos, dim)
            active_table_pos = tpos
            table_ready = True
            break

        if not can_afford_gear(state, target, assume_no_table=not table_ready and state.count("crafting_table") < 1):
            raise Replan(f"合成 {target} 木板不足：同种材料无法完成木棍、工作台与目标配方，需补充原木")

        while state.count("stick") < s_need:
            yield from _prepare_recipe_planks(ex, state, 2, "stick")
            before_sticks = state.count("stick")
            yield from _craft_with_space(ex, state, "stick")
            state.refresh_inventory(ex.client)
            if state.count("stick") <= before_sticks:
                raise Replan("合成 stick 后木棍未增加，停止准备工具")

        if not table_ready:
            if state.count("crafting_table") < 1:
                yield from _prepare_recipe_planks(ex, state, 4, "crafting_table")
                yield from _craft_with_space(ex, state, "crafting_table")
                state.refresh_inventory(ex.client)
            try:
                placed = ex.place("crafting_table")
                placed_pos = placed["pos"] if isinstance(placed, dict) and isinstance(placed.get("pos"), list) else state.pos
                _record_known_table(placed_pos)
                memo.mark_reachable(placed_pos, dimension=dim, category="station", block="crafting_table")
                _record_station(memo, "crafting_table", placed_pos, dim)
                active_table_pos = placed_pos
                yield rec("place", placed, item="crafting_table")
            except Replan as e:
                if failure(e)[0] == "protected_region":
                    _mark_protected(state.pos)
                    memo.mark_protected(state.pos, dimension=dim)
                    raise Replan("当前位置处于出生点保护区，无法放置工作台") from e
                raise
        yield from _prepare_recipe_planks(ex, state, p_need, target)
        yield from _craft_with_space(ex, state, target, table_pos=active_table_pos)
        state.refresh_inventory(ex.client)

    def verify_done(self, state: "State", before_counts: dict[str, int]) -> str:
        target = str(self.args["item"])
        return f"craft_gear({target}) 结束但背包数量未增加" if state.count(target) <= before_counts.get(target, 0) else ""


@register
class MineStone(Skill):
    name = "mine_stone"
    description = "装备镐安全挖掘石头收集圆石（自动剥离表土并严禁挖脚下）"
    params = {"count": Param(int, "期望新增圆石数", default=8, min=1, max=32)}
    goal = "acq:cobblestone"
    cooldown_s = 20.0
    verified = "live"

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
    def _touches_lava(ex: "Executor", pos, vc=None, dim: str = "overworld") -> bool:
        """检查目标方块六邻面及正上方 2 格是否紧邻岩浆，严禁挖破岩浆池引火烧身。"""
        bx, by, bz = int(pos[0]), int(pos[1]), int(pos[2])
        neighbors = (
            [bx + 1, by, bz], [bx - 1, by, bz],
            [bx, by, bz + 1], [bx, by, bz - 1],
            [bx, by + 1, bz], [bx, by - 1, bz],
        )
        for nb in neighbors:
            name = bare(_get_block(ex, vc, nb, dim).get("name", ""))
            if name in ("lava", "flowing_lava"):
                return True
        return False

    @staticmethod
    def _safe_stair_column(ex: "Executor", x: int, y: int, z: int, vc=None, dim: str = "overworld") -> bool:
        floor = bare(_get_block(ex, vc, [x, y - 1, z], dim).get("name", ""))
        if not floor or floor in AIR or floor in ("lava", "water") or floor.endswith(("_leaves", "_log", "_wood")):
            return False
        # 三格净空上方若是落沙/砾石，挖开后会重新填入槽内；底板实心仍不足以安全下行。
        roof = bare(_get_block(ex, vc, [x, y + 3, z], dim).get("name", ""))
        return bool(roof) and roof not in ("sand", "red_sand", "gravel")

    def _shallow_stone_approach(self, ex: "Executor", cur, stones, tried, vc=None, dim: str = "overworld") -> Optional[list]:
        for block in stones[:4]:
            p = block.get("pos")
            if not (isinstance(p, list) and len(p) == 3 and math.floor(cur[1]) - 2 <= p[1] < cur[1] - 1.5 and not _is_protected(p)):
                continue
            x, y, z = map(int, p)
            sides = sorted(((x + dx, z + dz) for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))),
                           key=lambda q: math.hypot(q[0] + 0.5 - cur[0], q[1] + 0.5 - cur[2]))
            for sx, sz in sides:
                key = (sx, y + 1, sz)
                if key in tried or _is_protected(key):
                    continue
                tried.add(key)
                if (self._safe_stair_column(ex, sx, y + 1, sz, vc=vc, dim=dim)
                        and all(bare(_get_block(ex, vc, [sx, y + dy, sz], dim).get("name", "")) in AIR for dy in (1, 2, 3))):
                    return [sx + 0.5, y + 1, sz + 0.5]
        return None

    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        state.refresh_status(ex.client, max_age_s=0)
        yield from self._prepare_pickaxe(ex, state)

        start_cobble, want, memo, vc = total_cobble(state), int(self.args["count"]), memo_for(state), _voxel_cache(state)
        bad_positions: set[tuple[int, int, int]] = set()
        stair_origin: Optional[tuple[int, int, int, int, int]] = None
        stair_step, stair_steps_taken, approach_moves = 0, 0, 0
        stair_columns: set[tuple[int, int]] = set()
        tried_approaches: set[tuple[int, int, int]] = set()

        for _ in range(want + 18):
            if total_cobble(state) - start_cobble >= want:
                return
            st, cur = _refresh_stance(ex, state)
            dim = _dim_of(st)
            cur_cell = (math.floor(cur[0]), math.floor(cur[1]), math.floor(cur[2]))
            vc and vc.clear_stance_blacklist_if_moved(cur)
            found_stones = ex.client.find_blocks(types=list(STONE_Targets), radius=24, limit=48)
            vc and vc.ingest_find_blocks(found_stones, dimension=dim, client=ex.client)
            stones = found_stones.get("blocks") or []
            cands = [
                b["pos"] for b in stones
                if isinstance(b.get("pos"), list) and len(b["pos"]) == 3
                and (int(b["pos"][0]), int(b["pos"][1]), int(b["pos"][2])) not in bad_positions
                and (cur[1] - 1.5) <= float(b["pos"][1])
                and (float(b["pos"][1]) <= (cur[1] + 2.5)
                     or (float(b["pos"][1]) <= (cur[1] + 4.0) and eye_to_block_dist(st, b["pos"]) <= 8.0)
                     or eye_to_block_dist(st, b["pos"]) <= DIG_EYE_REACH)
                and not _is_under_feet(cur, b["pos"]) and not _is_protected(b["pos"])
                and not memo.is_blocked(b["pos"], dimension=dim)
                and not (vc and vc.is_unreachable("mine_stone", b["pos"], dimension=dim, stance_cell=cur_cell))
            ]
            cands.sort(key=lambda p: eye_to_block_dist(st, p))
            target = cands[0] if cands else None

            if target is None:
                if stair_origin is None and stair_steps_taken < 6:
                    ix, fy, iz = math.floor(cur[0]), math.floor(cur[1]), math.floor(cur[2])
                    stair_columns.add((ix, iz))
                    _prefetch_blocks(
                        ex, vc,
                        [
                            [ix + dx, fy + k, iz + dz]
                            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)) for k in (-2, 0, 2)
                        ],
                        dim,
                    )
                    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        sx1, sz1 = ix + dx, iz + dz
                        if (sx1, sz1) in stair_columns or _is_protected([sx1, fy - 1, sz1]) or memo.is_blocked([sx1, fy - 1, sz1], dimension=dim):
                            continue
                        if bare(_get_block(ex, vc, [sx1, fy, sz1], dim).get("name", "")) == "crafting_table":
                            continue
                        if self._safe_stair_column(ex, sx1, fy - 1, sz1, vc=vc, dim=dim):
                            stair_origin, stair_step = (ix, fy, iz, dx, dz), 1
                            break
                if stair_origin is not None and stair_steps_taken < 6:
                    ox, ofy, oz, dx, dz = stair_origin
                    sx, sy, sz = ox + stair_step * dx, ofy - stair_step, oz + stair_step * dz
                    if (sx, sz) not in stair_columns and self._safe_stair_column(ex, sx, sy, sz, vc=vc, dim=dim):
                        stair_columns.add((sx, sz))
                        stair_steps_taken += 1
                        reobserve_stair = False
                        dug_stair_cells: dict[int, str] = {}
                        for clear_y in (sy + 2, sy + 1, sy):
                            blk = _get_block(ex, vc, [sx, clear_y, sz], dim)
                            bname = bare(blk.get("name", ""))
                            if bname and bname not in AIR and bname not in ("water", "lava", "crafting_table"):
                                if bname in STONE_Targets:
                                    yield from self._prepare_pickaxe(ex, state)
                                st_now = state.refresh_status(ex.client, max_age_s=0)
                                for _ in range(4):
                                    if (st_now.get("on_ground") is not False or not st_now.get("alive", True)
                                            or st_now.get("in_water") or st_now.get("in_lava")):
                                        break
                                    time.sleep(0.05)
                                    st_now = state.refresh_status(ex.client, max_age_s=0)
                                cur = st_now.get("pos") or state.pos
                                if _is_under_feet(cur, [sx, clear_y, sz]):
                                    stair_origin, reobserve_stair = None, True
                                    break
                                dug_res = yield from nav.dig_with_recovery(
                                    ex, state, [sx, clear_y, sz], collect=bname not in PEEL_SURFACE
                                )
                                dug_stair_cells[clear_y] = bname
                                vc and vc.invalidate_radius([sx, clear_y, sz], radius=1, dimension=dim)
                                state.refresh_inventory(ex.client)
                                if total_cobble(state) - start_cobble >= want:
                                    return
                                self._check_dig_tool(state, dug_res)
                        if reobserve_stair:
                            continue
                        for clear_y in (sy + 2, sy + 1, sy):
                            blk = _get_block(ex, vc, [sx, clear_y, sz], dim, force=True)
                            bname = bare(blk.get("name", ""))
                            if bname in AIR:
                                continue
                            # 仅对“刚刚挖过的非重力实心块”原位幽灵回滚补挖一次；
                            # 新坠入的沙/砾石或此前为空的格被回填一律立即中止，守住坍塌安全不变量。
                            cur = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
                            if (
                                bname
                                and bname == dug_stair_cells.get(clear_y)
                                and bname not in ("sand", "red_sand", "gravel", "water", "lava", "flowing_water", "flowing_lava", "crafting_table", "bedrock")
                                and not _is_under_feet(cur, [sx, clear_y, sz])
                            ):
                                dug_res = yield from nav.dig_with_recovery(
                                    ex, state, [sx, clear_y, sz], collect=bname not in PEEL_SURFACE
                                )
                                vc and vc.invalidate_radius([sx, clear_y, sz], radius=1, dimension=dim)
                                state.refresh_inventory(ex.client)
                                if total_cobble(state) - start_cobble >= want:
                                    return
                                self._check_dig_tool(state, dug_res)
                                blk2 = _get_block(ex, vc, [sx, clear_y, sz], dim, force=True)
                                if bare(blk2.get("name", "")) in AIR:
                                    continue
                            raise Replan("阶梯净空发生变化，停止进入并重新观察")
                        try:
                            step_dest = [sx + 0.5, float(sy), sz + 0.5]
                            yield rec("move_to", _bounded_move_to(ex, step_dest, rng=1, timeout=30.0), pos=step_dest)
                            entry_pt = (ox + 0.5, float(ofy), oz + 0.5)
                            step_pt = (sx + 0.5, float(sy), sz + 0.5)
                            if MINE_TRAILS and math.dist(MINE_TRAILS[-1][-1], entry_pt) <= 6.0:
                                MINE_TRAILS[-1].append(step_pt)
                            else:
                                MINE_TRAILS.append([entry_pt, step_pt])
                        except Replan as e:
                            code, _ = failure(e)
                            if code not in NAV_FAIL_CODES:
                                raise
                            yield rec("move_to", pos=[sx + 0.5, sy, sz + 0.5], error_code=code)
                        stair_step += 1
                        continue
                    stair_origin = None
                    continue

                if total_cobble(state) > start_cobble:
                    return
                if approach_moves < 3:
                    dest = self._shallow_stone_approach(ex, cur, stones, tried_approaches, vc=vc, dim=dim)
                    if dest is not None:
                        approach_moves += 1
                        try:
                            moved = _bounded_move_to(ex, dest, rng=1, timeout=30.0)
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
            if self._touches_lava(ex, target, vc=vc, dim=dim):
                bad_positions.add(t_key)
                vc and vc.mark_unreachable("mine_stone", t_key, dimension=dim, stance_cell=cur_cell)
                continue
            if not can_reach_block(ex, st, target, reach=DIG_EYE_REACH):
                if eye_to_block_dist(st, target) <= DIG_EYE_REACH:
                    yield from _clear_los(ex, state, st, target, max_clear=1, reach=DIG_EYE_REACH, vc=vc, dim=dim)
                    st, cur = _refresh_stance(ex, state)
                if not can_reach_block(ex, st, target, reach=DIG_EYE_REACH):
                    try:
                        yield rec("move_to", _bounded_move_to(ex, target, rng=2, timeout=30.0), pos=target)
                    except Replan as e:
                        code, _ = failure(e)
                        if code in NAV_FAIL_CODES:
                            bad_positions.add(t_key)
                            vc and vc.mark_unreachable("mine_stone", t_key, dimension=dim, stance_cell=cur_cell)
                            yield rec("move_to", pos=target, error_code=code)
                            continue
                        raise
            try:
                yield from self._prepare_pickaxe(ex, state)
                st, cur = _refresh_stance(ex, state)
                if _is_under_feet(cur, target) or not can_reach_block(ex, st, target, reach=API_DIG_EYE_REACH):
                    bad_positions.add(t_key)
                    vc and vc.mark_unreachable("mine_stone", t_key, dimension=dim, stance_cell=cur_cell)
                    continue
                before_dig_cobble = total_cobble(state)
                dug_res = yield from nav.dig_with_recovery(ex, state, target, collect=True)
                vc and vc.invalidate(t_key, dimension=dim)
            except Replan as e:
                code, _ = failure(e)
                if code == "protected_region":
                    _mark_protected(target)
                    memo.mark_protected(target, dimension=dim)
                    raise Replan(f"方块 {target} 位于出生点保护区，已标记外迁") from e
                if code in ("unreachable", "dig_timeout", "path_not_found"):
                    bad_positions.add(t_key)
                    vc and vc.mark_unreachable("mine_stone", t_key, dimension=dim, stance_cell=cur_cell)
                    yield rec("dig", pos=target, error_code=code)
                    continue
                raise
            state.refresh_inventory(ex.client)
            if total_cobble(state) - start_cobble >= want:
                return
            self._check_dig_tool(state, dug_res)
            if total_cobble(state) <= before_dig_cobble:
                bad_positions.update((bx, by, bz)
                                     for bx in range(t_key[0] - 2, t_key[0] + 3)
                                     for by in range(t_key[1] - 2, t_key[1] + 3)
                                     for bz in range(t_key[2] - 2, t_key[2] + 3))

        if total_cobble(state) <= start_cobble:
            raise Replan("剥离表土后仍未采集到圆石")

    def verify_done(self, state: "State", before_counts: dict[str, int]) -> str:
        before = before_counts.get("cobblestone", 0) + before_counts.get("cobbled_deepslate", 0)
        return f"mine_stone 结束但圆石未增加（before={before}, after={total_cobble(state)}）" if total_cobble(state) <= before else ""
