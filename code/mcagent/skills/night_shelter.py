"""夜间/黄昏快速挖洞封口避险与天亮安全出坑 L3 技能（`NightShelter`）。

支持三种宽松地形模式（通过单次 `voxel_cache.get_blocks_batch` 预检，按优先级择优）：
1. `alcove`（1×2×2 侧坡壁龛）：向山坡/岩壁水平挖入两格并封堵入口 2 格；
2. `dig3_fill1`（经典挖三填一）：脚下垂直挖深 3 格、头顶 `y-1` 封 1 块非重力实体方块，并预检可用阶梯出口；
3. `dig2_anchor_fill1`（浅层锚点封口兜底）：当 `y-3` 为硬石且无镐时，下挖 2 格并在邻格立锚点后于 `y` 封顶。

严格遵守 `AGENTS.md` §9.2：每发一个阻塞动作（`dig` / `place` / `move_to` / `equip` / `use_item`）精确 `yield rec(...)` 一次；
洞内等待期间每轮轮询让出 `rec("shelter_wait", ...)` 以便主循环检查预算与死亡状态。
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Optional

from ..client import ApiError
from ..runtime import Replan
from .base import Param, Skill, failure, rec
from .camp_plan import COOKED_FOOD_ORDER, EDIBLE_FOODS, is_protected_cell, record_protected_cell, state_dim, state_snap
from .items import AIR, PLACEABLE_BLOCKS, bare, best_pickaxe, is_clear_space, is_safe_solid_floor
from .registry import register
from .sightline import block_cell
from .voxel_cache import voxel_cache_for
from .world_memo import memo_for

if TYPE_CHECKING:
    from ..runtime import Executor, State

_CARDINAL_DIRS: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))

_FLUIDS: frozenset[str] = frozenset({
    "water", "lava", "flowing_water", "flowing_lava", "bubble_column",
})

_GRAVITY_BLOCKS: frozenset[str] = frozenset({
    "sand", "red_sand", "gravel", "suspicious_sand", "suspicious_gravel",
    "anvil", "chipped_anvil", "damaged_anvil", "dragon_egg", "pointed_dripstone",
})

_FORBIDDEN_DIG_BLOCKS: frozenset[str] = frozenset({
    "bedrock", "obsidian", "crying_obsidian", "reinforced_deepslate",
    "end_portal_frame", "spawner", "trial_spawner", "vault",
    "chest", "trapped_chest", "ender_chest", "barrel",
    "furnace", "blast_furnace", "smoker", "crafting_table", "enchanting_table",
    "anvil", "chipped_anvil", "damaged_anvil", "brewing_stand", "beacon", "tnt",
    "nether_portal", "end_portal", "end_gateway",
    "magma_block", "cactus", "fire", "soul_fire", "campfire", "soul_campfire", "powder_snow",
}) | _FLUIDS

#: 无镐也可快速徒手挖掘的软方块（不含沙子/沙砾等重力方块，防塌方窒息）
_SOFT_DIG_BLOCKS: frozenset[str] = frozenset({
    "dirt", "grass_block", "coarse_dirt", "rooted_dirt", "podzol", "mycelium",
    "mud", "muddy_mangrove_roots", "clay", "snow_block", "farmland",
    "dirt_path", "moss_block", "pale_moss_block", "soul_sand", "soul_soil",
})

#: 有镐时允许挖掘的常见岩石/矿石方块
_HARD_DIG_BLOCKS: frozenset[str] = frozenset({
    "stone", "cobblestone", "deepslate", "cobbled_deepslate",
    "andesite", "diorite", "granite", "tuff",
    "sandstone", "red_sandstone", "terracotta", "calcite", "dripstone_block",
    "coal_ore", "copper_ore", "iron_ore",
    "deepslate_coal_ore", "deepslate_copper_ore", "deepslate_iron_ore",
    "netherrack", "blackstone", "basalt",
})

#: 封口方块偏好顺序（严格排除沙子/沙砾/混凝土粉末/铁砧等重力方块）
_PREFERRED_CAP_BLOCKS: tuple[str, ...] = (
    "dirt", "cobblestone", "cobbled_deepslate", "stone",
    "andesite", "diorite", "granite", "tuff", "deepslate", "netherrack",
    "mud", "clay", "snow_block", "grass_block", "coarse_dirt", "rooted_dirt", "moss_block",
    "oak_planks", "spruce_planks", "birch_planks", "jungle_planks",
    "acacia_planks", "dark_oak_planks", "cherry_planks", "mangrove_planks",
    "bamboo_planks", "pale_oak_planks", "crimson_planks", "warped_planks",
    "sandstone", "red_sandstone", "terracotta", "calcite",
    "blackstone", "basalt", "end_stone", "stone_bricks", "bricks", "packed_mud",
    *tuple(b for b in PLACEABLE_BLOCKS if b not in _GRAVITY_BLOCKS and not b.endswith("_concrete_powder")),
)
_CAP_BLOCK_ORDER: tuple[str, ...] = tuple(dict.fromkeys(_PREFERRED_CAP_BLOCKS))
_CAP_BLOCK_SET: frozenset[str] = frozenset(_CAP_BLOCK_ORDER)

#: 挖掘后必然直接掉落可放置非重力方块（无需精准采集）的方块名
_GUARANTEED_CAP_DROPS: frozenset[str] = frozenset({
    "dirt", "grass_block", "coarse_dirt", "rooted_dirt", "podzol", "mycelium",
    "mud", "moss_block", "pale_moss_block", "dirt_path", "farmland",
    "cobblestone", "stone", "cobbled_deepslate", "deepslate",
    "andesite", "diorite", "granite", "tuff", "netherrack",
    "sandstone", "red_sandstone", "terracotta", "calcite", "dripstone_block",
    "blackstone", "basalt",
})


def _is_finite_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _is_gravity_block(name: str) -> bool:
    b = bare(name).strip().lower()
    return b in _GRAVITY_BLOCKS or b.endswith("_concrete_powder")


def _is_waterlogged(blk: Any) -> bool:
    if not isinstance(blk, Mapping):
        return False
    props = blk.get("properties")
    return isinstance(props, Mapping) and props.get("waterlogged") in (True, "true")


def _block_name(blk: Any) -> str:
    if isinstance(blk, Mapping):
        return bare(blk.get("name")).strip().lower()
    if isinstance(blk, str):
        return bare(blk).strip().lower()
    return ""


def _is_solid_non_fluid(blk: Any) -> bool:
    if not isinstance(blk, Mapping):
        return False
    name = _block_name(blk)
    if not name or name in _FLUIDS or _is_waterlogged(blk):
        return False
    return is_safe_solid_floor(blk)


def _is_solid_stable(blk: Any) -> bool:
    return _is_solid_non_fluid(blk) and not _is_gravity_block(_block_name(blk))


def _is_hazard_above(blk: Any) -> bool:
    """判断正上方方块是否为流体或重力方块（挖掘下方后会倾泻/塌落）。"""
    if not isinstance(blk, Mapping):
        return False
    name = _block_name(blk)
    return name in _FLUIDS or _is_gravity_block(name) or _is_waterlogged(blk)


def _can_dig_cell(blk: Any, *, has_pick: bool) -> tuple[bool, bool]:
    """返回 `(是否可挖/已净空, 是否需要镐)`。对未知/未加载/流体/基岩/重力方块一律返回 False。"""
    if not isinstance(blk, Mapping) or blk.get("diggable") is False:
        return (False, False)
    name = _block_name(blk)
    if not name or _is_waterlogged(blk) or _is_gravity_block(name):
        return (False, False)
    if name in _FORBIDDEN_DIG_BLOCKS or name.endswith("_shulker_box") or name.endswith("_bed") or name == "bed":
        return (False, False)
    if is_clear_space(blk):
        return (True, False)
    if name in _SOFT_DIG_BLOCKS or name.endswith("_leaves"):
        return (True, False)
    if name in _HARD_DIG_BLOCKS:
        return (has_pick, True)
    return (False, False)


def _guaranteed_cap_drop(blk: Any, *, has_pick: bool) -> bool:
    if not isinstance(blk, Mapping):
        return False
    name = _block_name(blk)
    if name not in _GUARANTEED_CAP_DROPS:
        return False
    if name in _HARD_DIG_BLOCKS and not has_pick:
        return False
    return True


def _is_valid_cap_item(name: str) -> bool:
    b = bare(name).strip().lower()
    if not b or _is_gravity_block(b):
        return False
    return b in _CAP_BLOCK_SET or b.endswith("_planks")


def _inventory_cap_names(state: "State") -> list[str]:
    inv_items = ((getattr(state, "inventory", None) or {}).get("items") or ())
    extra: list[str] = []
    seen = set(_CAP_BLOCK_ORDER)
    for row in inv_items:
        if isinstance(row, Mapping):
            nm = bare(row.get("name")).strip().lower()
            if nm and nm not in seen and _is_valid_cap_item(nm):
                seen.add(nm)
                extra.append(nm)
    return [*_CAP_BLOCK_ORDER, *extra]


def _count_cap_blocks(state: "State") -> int:
    return sum(state.count(name) for name in _inventory_cap_names(state))


def _pick_cap_block(state: "State") -> Optional[str]:
    for name in _inventory_cap_names(state):
        if state.count(name) >= 1:
            return name
    return None


def _pick_edible_food(state: "State") -> Optional[str]:
    for food in COOKED_FOOD_ORDER:
        if state.count(food) >= 1:
            return food
    for food in sorted(EDIBLE_FOODS):
        if state.count(food) >= 1:
            return food
    return None


def _hostile_dist(hostile: Mapping[str, Any], player_pos: Any) -> Optional[float]:
    raw_dist = hostile.get("dist")
    if _is_finite_num(raw_dist):
        return float(raw_dist)
    hpos = hostile.get("pos")
    if (
        isinstance(player_pos, Sequence) and not isinstance(player_pos, (str, bytes)) and len(player_pos) >= 3
        and isinstance(hpos, Sequence) and not isinstance(hpos, (str, bytes)) and len(hpos) >= 3
        and all(_is_finite_num(v) for v in (*player_pos[:3], *hpos[:3]))
    ):
        return math.dist(
            (float(player_pos[0]), float(player_pos[1]), float(player_pos[2])),
            (float(hpos[0]), float(hpos[1]), float(hpos[2])),
        )
    return None


def is_night_shelter_window(st: Mapping[str, Any]) -> bool:
    """判断当前是否处于主世界黄昏/夜间避险窗口（`11800 <= tod <= 23500`，缺 `tod` 时回退 `is_day is False`）。"""
    if not isinstance(st, Mapping):
        return False
    dim = bare(st.get("dimension") or "overworld").strip() or "overworld"
    if dim != "overworld":
        return False
    tod = st.get("time_of_day")
    if _is_finite_num(tod):
        tod_mod = float(tod) % 24000.0
        return 11800.0 <= tod_mod <= 23500.0
    return st.get("is_day") is False


def _shelter_precondition_reason(state: "State") -> str:
    st = state_snap(state)
    if st.get("ready") is False:
        return "世界加载尚未就绪"
    if st.get("alive") is False:
        return "角色未存活，无法挖洞避险"
    dim = state_dim(state)
    if dim != "overworld":
        return f"维度 {dim} 不支持夜间挖洞避险"
    if st.get("in_water") or st.get("in_lava") or st.get("submerged") or st.get("in_vehicle"):
        return "当前处于液体或载具中，无法原地挖洞避险"
    pos = st.get("pos")
    if not (
        isinstance(pos, Sequence) and not isinstance(pos, (str, bytes)) and len(pos) >= 3
        and all(_is_finite_num(v) for v in pos[:3])
    ):
        return "缺少有效的当前三维坐标"
    fx, fy, fz = block_cell(pos[:3])
    if fy < 50:
        return f"当前高度 y={fy} 低于 50，禁止原地挖洞避险"
    if is_protected_cell((fx, fy, fz)):
        return "当前位于保护区内，禁止原地挖洞"
    hostile = st.get("nearest_hostile")
    if isinstance(hostile, Mapping) and hostile:
        hname = bare(hostile.get("name")).strip().lower()
        hdist = _hostile_dist(hostile, pos)
        if hdist is not None:
            if hname == "creeper" and hdist < 7.5:
                return f"附近苦力怕距离 {hdist:.1f}m < 7.5m，需先拉开距离"
            if hdist < 6.0:
                return f"附近敌对生物距离 {hdist:.1f}m < 6.0m，需先拉开距离"
    return ""


def can_dig_night_shelter(state: "State") -> bool:
    """供 `policy.py` 调用的环境/安全前置检查（主世界、非液体/保护区、y>=50、无贴脸敌对）。"""
    return _shelter_precondition_reason(state) == ""


def _collect_core_probe_cells(fx: int, fy: int, fz: int) -> list[tuple[int, int, int]]:
    """阶段 1 核心预检格（约 22 格）：正下方柱 + 4 邻壁 y-3..0，避免高 RTT 下盲查 100 格卡顿。"""
    cells: set[tuple[int, int, int]] = {(fx, fy + dy, fz) for dy in range(-4, 2)}
    for dx, dz in _CARDINAL_DIRS:
        for dy in (-3, -2, -1, 0):
            cells.add((fx + dx, fy + dy, fz + dz))
    return sorted(cells)


def _collect_alcove_cells_for_dir(fx: int, fy: int, fz: int, dx: int, dz: int) -> list[tuple[int, int, int]]:
    cells: set[tuple[int, int, int]] = {
        (fx + dx, fy + 1, fz + dz), (fx + dx, fy + 2, fz + dz),
        (fx + 2 * dx, fy - 1, fz + 2 * dz), (fx + 2 * dx, fy, fz + 2 * dz),
        (fx + 2 * dx, fy + 1, fz + 2 * dz), (fx + 2 * dx, fy + 2, fz + 2 * dz),
        (fx + 3 * dx, fy, fz + 3 * dz), (fx + 3 * dx, fy + 1, fz + 3 * dz),
    }
    for rx, rz in ((-dz, dx), (dz, -dx)):
        for dy in (0, 1):
            cells.add((fx + dx + rx, fy + dy, fz + dz + rz))
            cells.add((fx + 2 * dx + rx, fy + dy, fz + 2 * dz + rz))
    return sorted(cells)


def _collect_exit_cells_for_dir(fx: int, fy: int, fz: int, dx: int, dz: int) -> list[tuple[int, int, int]]:
    cells: set[tuple[int, int, int]] = {
        (fx + dx, fy + 1, fz + dz),
    }
    for dy in range(-2, 3):
        cells.add((fx + 2 * dx, fy + dy, fz + 2 * dz))
        cells.add((fx + 3 * dx, fy + dy, fz + 3 * dz))
    return sorted(cells)


def _plan_alcove(
    snap: Mapping[tuple[int, int, int], dict],
    fx: int, fy: int, fz: int,
    *,
    has_pick: bool,
    inv_caps: int,
) -> Optional[dict[str, Any]]:
    if not _is_solid_non_fluid(snap.get((fx, fy - 1, fz))):
        return None
    for dx, dz in _CARDINAL_DIRS:
        c1_0 = (fx + dx, fy, fz + dz)
        c1_1 = (fx + dx, fy + 1, fz + dz)
        c2_0 = (fx + 2 * dx, fy, fz + 2 * dz)
        c2_1 = (fx + 2 * dx, fy + 1, fz + 2 * dz)
        dig_cells = (c1_0, c1_1, c2_0, c2_1)
        if any(is_protected_cell(c) for c in dig_cells):
            continue
        if not (_is_solid_non_fluid(snap.get((fx + dx, fy - 1, fz + dz)))
                and _is_solid_non_fluid(snap.get((fx + 2 * dx, fy - 1, fz + 2 * dz)))):
            continue
        # 顶盖：内室上方必须稳定实体，入口上方禁止液体/重力方块
        if _is_hazard_above(snap.get((fx + dx, fy + 2, fz + dz))):
            continue
        if not _is_solid_stable(snap.get((fx + 2 * dx, fy + 2, fz + 2 * dz))):
            continue
        # 内室后墙与左右侧壁必须为稳定非流体实心方块；入口两侧不可有流体/落沙
        if not (_is_solid_stable(snap.get((fx + 3 * dx, fy, fz + 3 * dz)))
                and _is_solid_stable(snap.get((fx + 3 * dx, fy + 1, fz + 3 * dz)))):
            continue
        walls_ok = True
        for rx, rz in ((-dz, dx), (dz, -dx)):
            for dy in (0, 1):
                if _is_hazard_above(snap.get((fx + dx + rx, fy + dy, fz + dz + rz))):
                    walls_ok = False
                    break
                if not _is_solid_stable(snap.get((fx + 2 * dx + rx, fy + dy, fz + 2 * dz + rz))):
                    walls_ok = False
                    break
            if not walls_ok:
                break
        if not walls_ok:
            continue

        dig_ok = True
        needs_pick = False
        guaranteed_drops = 0
        for c in dig_cells:
            blk = snap.get(c)
            ok, np = _can_dig_cell(blk, has_pick=has_pick)
            if not ok:
                dig_ok = False
                break
            needs_pick = needs_pick or np
            if not is_clear_space(blk) and _guaranteed_cap_drop(blk, has_pick=has_pick):
                guaranteed_drops += 1
        if not dig_ok or inv_caps + guaranteed_drops < 2:
            continue
        return {
            "mode": "alcove",
            "dir": (dx, dz),
            "dig_cells": dig_cells,
            "seal_cells": (c1_0, c1_1),
            "inner_cell": (fx + 2 * dx, fy, fz + 2 * dz),
            "cap_cell": c1_1,
            "stand_y": fy,
            "needs_pick": needs_pick,
        }
    return None


def _can_clear_exit_pair(
    snap: Mapping[tuple[int, int, int], dict],
    c_lo: tuple[int, int, int],
    c_hi: tuple[int, int, int],
    above: tuple[int, int, int],
    *,
    has_pick: bool,
) -> Optional[bool]:
    """检查两格高出口空间是否可通/可挖且顶部无流体/落沙，返回 `needs_pick` 或 None。"""
    if _is_hazard_above(snap.get(above)):
        return None
    needs_pick = False
    for c in (c_lo, c_hi):
        blk = snap.get(c)
        if is_clear_space(blk):
            continue
        if is_protected_cell(c):
            return None
        ok, np = _can_dig_cell(blk, has_pick=has_pick)
        if not ok:
            return None
        needs_pick = needs_pick or np
    return needs_pick


def _plan_dig3_fill1(
    snap: Mapping[tuple[int, int, int], dict],
    fx: int, fy: int, fz: int,
    *,
    has_pick: bool,
    inv_caps: int,
) -> Optional[dict[str, Any]]:
    d1 = (fx, fy - 1, fz)
    d2 = (fx, fy - 2, fz)
    d3 = (fx, fy - 3, fz)
    floor = (fx, fy - 4, fz)
    pit_cells = (d1, d2, d3)
    if any(is_protected_cell(c) for c in pit_cells):
        return None
    if not _is_solid_non_fluid(snap.get(floor)):
        return None

    needs_pick = False
    guaranteed_drops = 0
    for c in pit_cells:
        blk = snap.get(c)
        if is_clear_space(blk):
            return None  # 不挖入空洞/地下洞穴
        ok, np = _can_dig_cell(blk, has_pick=has_pick)
        if not ok:
            return None
        needs_pick = needs_pick or np
        if _guaranteed_cap_drop(blk, has_pick=has_pick):
            guaranteed_drops += 1

    if inv_caps + guaranteed_drops < 1:
        return None

    # 周围 4 邻：y-1..y-3 无流体/落沙，且 y-1 至少有一侧实心提供 place 支撑面
    has_ref_support = False
    for ndx, ndz in _CARDINAL_DIRS:
        for dy in (-1, -2, -3):
            nb = snap.get((fx + ndx, fy + dy, fz + ndz))
            if _is_hazard_above(nb):
                return None
            if dy == -1 and _is_solid_non_fluid(nb):
                has_ref_support = True
    if not has_ref_support:
        return None

    # 检查至少存在一个可挖出地表阶梯出口方向
    for edx, edz in _CARDINAL_DIRS:
        if not _is_solid_non_fluid(snap.get((fx + edx, fy - 3, fz + edz))):
            continue
        np1 = _can_clear_exit_pair(
            snap, (fx + edx, fy - 2, fz + edz), (fx + edx, fy - 1, fz + edz), (fx + edx, fy, fz + edz),
            has_pick=has_pick,
        )
        if np1 is None:
            continue
        if not _is_solid_non_fluid(snap.get((fx + 2 * edx, fy - 2, fz + 2 * edz))):
            continue
        np2 = _can_clear_exit_pair(
            snap, (fx + 2 * edx, fy - 1, fz + 2 * edz), (fx + 2 * edx, fy, fz + 2 * edz), (fx + 2 * edx, fy + 1, fz + 2 * edz),
            has_pick=has_pick,
        )
        if np2 is None:
            continue
        if _is_solid_non_fluid(snap.get((fx + 3 * edx, fy - 1, fz + 3 * edz))):
            s3_y = fy
            np3 = _can_clear_exit_pair(
                snap, (fx + 3 * edx, fy, fz + 3 * edz), (fx + 3 * edx, fy + 1, fz + 3 * edz), (fx + 3 * edx, fy + 2, fz + 3 * edz),
                has_pick=has_pick,
            )
        elif _is_solid_non_fluid(snap.get((fx + 3 * edx, fy - 2, fz + 3 * edz))):
            s3_y = fy - 1
            np3 = _can_clear_exit_pair(
                snap, (fx + 3 * edx, fy - 1, fz + 3 * edz), (fx + 3 * edx, fy, fz + 3 * edz), (fx + 3 * edx, fy + 1, fz + 3 * edz),
                has_pick=has_pick,
            )
        else:
            continue
        if np3 is None:
            continue
        return {
            "mode": "dig3_fill1",
            "dig_cells": pit_cells,
            "cap_cell": d1,
            "stand_y": fy - 3,
            "exit_dir": (edx, edz),
            "step3_y": s3_y,
            "needs_pick": needs_pick,
            "exit_needs_pick": bool(np1 or np2 or np3),
        }
    return None


def _plan_dig2_anchor_fill1(
    snap: Mapping[tuple[int, int, int], dict],
    fx: int, fy: int, fz: int,
    *,
    has_pick: bool,
    inv_caps: int,
) -> Optional[dict[str, Any]]:
    d1 = (fx, fy - 1, fz)
    d2 = (fx, fy - 2, fz)
    floor = (fx, fy - 3, fz)
    cap = (fx, fy, fz)
    if any(is_protected_cell(c) for c in (d1, d2, cap)):
        return None
    if not is_clear_space(snap.get(cap)) or _is_hazard_above(snap.get((fx, fy + 1, fz))):
        return None
    if not _is_solid_non_fluid(snap.get(floor)):
        return None

    needs_pick = False
    guaranteed_drops = 0
    for c in (d1, d2):
        blk = snap.get(c)
        if is_clear_space(blk):
            return None
        ok, np = _can_dig_cell(blk, has_pick=has_pick)
        if not ok:
            return None
        needs_pick = needs_pick or np
        if _guaranteed_cap_drop(blk, has_pick=has_pick):
            guaranteed_drops += 1

    if inv_caps + guaranteed_drops < 2:
        return None

    for ndx, ndz in _CARDINAL_DIRS:
        for dy in (-1, -2):
            if _is_hazard_above(snap.get((fx + ndx, fy + dy, fz + ndz))):
                return None

    anchors: list[tuple[int, int]] = []
    for adx, adz in _CARDINAL_DIRS:
        acell = (fx + adx, fy, fz + adz)
        if is_protected_cell(acell):
            continue
        if (
            _is_solid_non_fluid(snap.get((fx + adx, fy - 1, fz + adz)))
            and is_clear_space(snap.get(acell))
            and not _is_hazard_above(snap.get((fx + adx, fy + 1, fz + adz)))
        ):
            anchors.append((adx, adz))
    if not anchors:
        return None

    for adx, adz in anchors:
        # 优先选择与锚点不同的方向作为天亮出坑阶梯
        ordered_exits = [d for d in _CARDINAL_DIRS if d != (adx, adz)] + [(adx, adz)]
        for edx, edz in ordered_exits:
            if not _is_solid_non_fluid(snap.get((fx + edx, fy - 2, fz + edz))):
                continue
            np1 = _can_clear_exit_pair(
                snap, (fx + edx, fy - 1, fz + edz), (fx + edx, fy, fz + edz), (fx + edx, fy + 1, fz + edz),
                has_pick=has_pick,
            )
            if np1 is None:
                continue
            if _is_solid_non_fluid(snap.get((fx + 2 * edx, fy - 1, fz + 2 * edz))):
                s2_y = fy
                np2 = _can_clear_exit_pair(
                    snap, (fx + 2 * edx, fy, fz + 2 * edz), (fx + 2 * edx, fy + 1, fz + 2 * edz), (fx + 2 * edx, fy + 2, fz + 2 * edz),
                    has_pick=has_pick,
                )
            elif _is_solid_non_fluid(snap.get((fx + 2 * edx, fy - 2, fz + 2 * edz))):
                s2_y = fy - 1
                np2 = _can_clear_exit_pair(
                    snap, (fx + 2 * edx, fy - 1, fz + 2 * edz), (fx + 2 * edx, fy, fz + 2 * edz), (fx + 2 * edx, fy + 1, fz + 2 * edz),
                    has_pick=has_pick,
                )
            else:
                continue
            if np2 is None:
                continue
            return {
                "mode": "dig2_anchor_fill1",
                "dig_cells": (d1, d2),
                "anchor_cell": (fx + adx, fy, fz + adz),
                "cap_cell": cap,
                "stand_y": fy - 2,
                "exit_dir": (edx, edz),
                "step2_y": s2_y,
                "needs_pick": needs_pick,
                "exit_needs_pick": bool(np1 or np2),
            }
    return None


@register
class NightShelter(Skill):
    name = "night_shelter"
    description = "夜间/黄昏快速挖洞封口避险并在天亮后安全出坑"
    params = {
        "max_wait_s": Param(int, "洞内最大等待秒数", default=600, min=5, max=900),
        "poll_interval_s": Param(float, "洞内等待轮询间隔秒", default=2.0, min=0.0, max=10.0),
    }
    goal = "safety:night_shelter"
    verified = "live"
    cooldown_s = 15.0

    @classmethod
    def blocked(cls, state: "State") -> str:
        reason = _shelter_precondition_reason(state)
        if reason:
            return reason
        if not is_night_shelter_window(state_snap(state)):
            return "当前非黄昏/夜间窗口，暂无需挖洞避险"
        return ""

    def _ensure_pickaxe(self, ex: "Executor", state: "State"):
        pick = best_pickaxe(state)
        if pick is None:
            raise Replan("缺少挖掘硬方块的镐")
        held = bare((((state.inventory or {}).get("held") or {}).get("name")) or ((state_snap(state).get("held") or {}).get("name")))
        if held == pick:
            return
        yield rec("equip", ex.equip(pick, slot="hand", stop_on=("death",)), item=pick, slot="hand")
        state.refresh_inventory(ex.client)

    def _dig_cell(self, ex: "Executor", state: "State", cell: tuple[int, int, int], *, collect: bool, dim: str):
        try:
            res = ex.dig(list(cell), collect=collect, stop_on=("death",))
        except Replan as exc:
            if failure(exc)[0] == "protected_region":
                record_protected_cell(cell, memo_for(state), dim)
            raise
        voxel_cache_for(state).invalidate(cell, dimension=dim)
        yield rec("dig", res, pos=list(cell), collect=collect)
        if collect:
            state.refresh_inventory(ex.client)

    def _place_cell(self, ex: "Executor", state: "State", item: str, cell: tuple[int, int, int], *, dim: str):
        try:
            res = ex.place(item, pos=list(cell), stop_on=("death",))
        except Replan as exc:
            if failure(exc)[0] == "protected_region":
                record_protected_cell(cell, memo_for(state), dim)
            raise
        voxel_cache_for(state).invalidate(cell, dimension=dim)
        yield rec("place", res, item=item, pos=list(cell))
        state.refresh_inventory(ex.client)

    def _dig_if_solid(self, ex: "Executor", state: "State", cell: tuple[int, int, int], *, dim: str):
        try:
            blk = ex.client.get_block(list(cell))
        except ApiError as exc:
            if exc.code in ("not_loaded", "not_ready"):
                return
            raise
        if is_clear_space(blk):
            return
        ok, needs_pick = _can_dig_cell(blk, has_pick=best_pickaxe(state) is not None)
        if not ok:
            raise Replan(f"出坑格 {list(cell)} 被不可挖掘方块 {_block_name(blk)} 阻挡")
        if needs_pick:
            yield from self._ensure_pickaxe(ex, state)
        yield from self._dig_cell(ex, state, cell, collect=False, dim=dim)

    def _wait_inside(self, ex: "Executor", state: "State", *, mode: str, entered_at: float, entry_hp: float) -> Any:
        max_wait_s = float(self.args["max_wait_s"])
        poll_interval_s = float(self.args["poll_interval_s"])
        eff_ttl = min(1.0, max(0.0, poll_interval_s * 0.5)) if poll_interval_s >= 0.5 else 0.0
        dawn_seen_at: Optional[float] = None
        best_hp = entry_hp

        while time.monotonic() - entered_at < max_wait_s:
            try:
                st = state.refresh_status(ex.client, max_age_s=eff_ttl)
            except ApiError:
                st = state.refresh_status(ex.client, max_age_s=0)

            if st.get("alive") is False:
                return False
            if st.get("in_lava") or st.get("submerged") or st.get("on_fire"):
                return True

            now_m = time.monotonic()
            cur_hp = float(st.get("health", best_hp) or best_hp)
            recent_hurt = state.get_recent_hurt(max_age_s=3.0) if hasattr(state, "get_recent_hurt") else None
            if (recent_hurt and now_m >= entered_at + 1.0) or (best_hp - cur_hp >= 2.0):
                return True
            best_hp = max(best_hp, cur_hp)

            tod = st.get("time_of_day")
            tod_mod = (float(tod) % 24000.0) if _is_finite_num(tod) else None
            dawn_reached = (
                (0.0 <= tod_mod <= 11500.0 or tod_mod >= 23500.0)
                if tod_mod is not None
                else (st.get("is_day") is True)
            )
            if dawn_reached and not getattr(state, "in_safe_mode", False):
                nh = st.get("nearest_hostile")
                nh_dist = _hostile_dist(nh, st.get("pos")) if isinstance(nh, Mapping) else None
                hostile_close = nh_dist is not None and nh_dist < 4.0
                if hostile_close and tod_mod is not None and 23500.0 <= tod_mod < 23700.0:
                    if dawn_seen_at is None:
                        dawn_seen_at = now_m
                    if now_m - dawn_seen_at >= 10.0:
                        return True
                else:
                    return True

            food = float(st.get("food", 20.0) or 20.0)
            if (food < 18.0 or (cur_hp < 18.0 and food < 20.0)) and (food_item := _pick_edible_food(state)) is not None:
                yield rec("use_item", ex.use_item(food_item, stop_on=("death",)), item=food_item)
                state.refresh_inventory(ex.client)
                continue

            if poll_interval_s > 0.0:
                rem = max(0.0, max_wait_s - (time.monotonic() - entered_at))
                if rem > 0.0:
                    time.sleep(min(poll_interval_s, rem))
            yield rec("shelter_wait", {"mode": mode, "tod": tod, "hp": cur_hp})
        return True

    def _exit_shelter(self, ex: "Executor", state: "State", plan: Mapping[str, Any], fx: int, fy: int, fz: int, *, dim: str):
        mode = plan["mode"]
        if mode == "alcove":
            c1_0, c1_1 = plan["seal_cells"]
            yield from self._dig_if_solid(ex, state, c1_1, dim=dim)
            yield from self._dig_if_solid(ex, state, c1_0, dim=dim)
            exit_pt = [fx + 0.5, float(fy), fz + 0.5]
            yield rec("move_to", ex.move_to(pos=exit_pt, rng=0.5, timeout=8.0, retries=0, stop_on=("death",)), pos=exit_pt)
            return

        if mode == "dig3_fill1":
            edx, edz = plan["exit_dir"]
            yield from self._dig_if_solid(ex, state, plan["cap_cell"], dim=dim)
            yield from self._dig_if_solid(ex, state, (fx + edx, fy - 1, fz + edz), dim=dim)
            yield from self._dig_if_solid(ex, state, (fx + edx, fy - 2, fz + edz), dim=dim)
            p1 = [fx + edx + 0.5, float(fy - 2), fz + edz + 0.5]
            yield rec("move_to", ex.move_to(pos=p1, rng=0.4, timeout=8.0, retries=0, stop_on=("death",)), pos=p1)

            yield from self._dig_if_solid(ex, state, (fx + 2 * edx, fy, fz + 2 * edz), dim=dim)
            yield from self._dig_if_solid(ex, state, (fx + 2 * edx, fy - 1, fz + 2 * edz), dim=dim)
            p2 = [fx + 2 * edx + 0.5, float(fy - 1), fz + 2 * edz + 0.5]
            yield rec("move_to", ex.move_to(pos=p2, rng=0.4, timeout=8.0, retries=0, stop_on=("death",)), pos=p2)

            s3_y = int(plan["step3_y"])
            yield from self._dig_if_solid(ex, state, (fx + 3 * edx, s3_y + 1, fz + 3 * edz), dim=dim)
            yield from self._dig_if_solid(ex, state, (fx + 3 * edx, s3_y, fz + 3 * edz), dim=dim)
            p3 = [fx + 3 * edx + 0.5, float(s3_y), fz + 3 * edz + 0.5]
            yield rec("move_to", ex.move_to(pos=p3, rng=0.5, timeout=8.0, retries=0, stop_on=("death",)), pos=p3)
            return

        # mode == "dig2_anchor_fill1"
        edx, edz = plan["exit_dir"]
        yield from self._dig_if_solid(ex, state, plan["cap_cell"], dim=dim)
        yield from self._dig_if_solid(ex, state, (fx + edx, fy, fz + edz), dim=dim)
        yield from self._dig_if_solid(ex, state, (fx + edx, fy - 1, fz + edz), dim=dim)
        p1 = [fx + edx + 0.5, float(fy - 1), fz + edz + 0.5]
        yield rec("move_to", ex.move_to(pos=p1, rng=0.4, timeout=8.0, retries=0, stop_on=("death",)), pos=p1)

        s2_y = int(plan["step2_y"])
        yield from self._dig_if_solid(ex, state, (fx + 2 * edx, s2_y + 1, fz + 2 * edz), dim=dim)
        yield from self._dig_if_solid(ex, state, (fx + 2 * edx, s2_y, fz + 2 * edz), dim=dim)
        p2 = [fx + 2 * edx + 0.5, float(s2_y), fz + 2 * edz + 0.5]
        yield rec("move_to", ex.move_to(pos=p2, rng=0.5, timeout=8.0, retries=0, stop_on=("death",)), pos=p2)

    def run(self, ex: "Executor", state: "State"):
        self._exited_cleanly = False
        st = state.refresh_status(ex.client, max_age_s=0)
        state.refresh_inventory(ex.client)
        reason = _shelter_precondition_reason(state)
        if reason:
            raise Replan(reason)

        dim = state_dim(state)
        px, py, pz = st["pos"][:3]
        fx, fy, fz = block_cell((px, py, pz))
        has_pick = best_pickaxe(state) is not None
        inv_caps = _count_cap_blocks(state)

        vc = voxel_cache_for(state)
        snap: dict[tuple[int, int, int], dict] = dict(
            vc.get_blocks_batch(ex.client, _collect_core_probe_cells(fx, fy, fz), dimension=dim)
        )

        def _fetch_missing(cells: Sequence[tuple[int, int, int]]) -> None:
            missing = [c for c in cells if c not in snap]
            if missing:
                snap.update(vc.get_blocks_batch(ex.client, missing, dimension=dim))

        plan: Optional[dict[str, Any]] = None
        # 仅当邻格确实存在实心侧坡壁面时才补查该方向的壁龛格，平地零额外查块
        for dx, dz in _CARDINAL_DIRS:
            adj0 = snap.get((fx + dx, fy, fz + dz))
            if adj0 and not is_clear_space(adj0) and not _is_hazard_above(adj0):
                _fetch_missing(_collect_alcove_cells_for_dir(fx, fy, fz, dx, dz))
                plan = _plan_alcove(snap, fx, fy, fz, has_pick=has_pick, inv_caps=inv_caps)
                if plan is not None:
                    break

        if plan is None:
            # 优先按方向逐个补查出口阶梯（首个方向通过即停止，平地总计仅查 ~30 格而非 100 格）
            for dx, dz in _CARDINAL_DIRS:
                if not _is_solid_non_fluid(snap.get((fx + dx, fy - 3, fz + dz))):
                    continue
                _fetch_missing(_collect_exit_cells_for_dir(fx, fy, fz, dx, dz))
                plan = _plan_dig3_fill1(snap, fx, fy, fz, has_pick=has_pick, inv_caps=inv_caps)
                if plan is not None:
                    break

        if plan is None:
            for dx, dz in _CARDINAL_DIRS:
                _fetch_missing(_collect_exit_cells_for_dir(fx, fy, fz, dx, dz))
                plan = _plan_dig2_anchor_fill1(snap, fx, fy, fz, has_pick=has_pick, inv_caps=inv_caps)
                if plan is not None:
                    break

        if plan is None:
            raise Replan("当前地形或方块/工具条件不满足挖洞避险安全检查")

        mode = plan["mode"]
        if plan.get("needs_pick"):
            yield from self._ensure_pickaxe(ex, state)

        if mode == "alcove":
            for cell in plan["dig_cells"]:
                if is_clear_space(snap.get(cell)):
                    continue
                need_collect = _count_cap_blocks(state) < 2
                yield from self._dig_cell(ex, state, cell, collect=need_collect, dim=dim)
            icx, icy, icz = plan["inner_cell"]
            inner_pt = [icx + 0.5, float(icy), icz + 0.5]
            yield rec("move_to", ex.move_to(pos=inner_pt, rng=0.4, timeout=8.0, retries=0, stop_on=("death",)), pos=inner_pt)
            st_in = state.refresh_status(ex.client, max_age_s=0)
            c1_0, c1_1 = plan["seal_cells"]
            cur_pos = st_in.get("pos") or inner_pt
            if math.hypot(float(cur_pos[0]) - (c1_0[0] + 0.5), float(cur_pos[2]) - (c1_0[2] + 0.5)) < 0.81:
                yield rec("move_to", ex.move_to(pos=inner_pt, rng=0.25, timeout=5.0, retries=0, stop_on=("death",)), pos=inner_pt)
            for sc in (c1_0, c1_1):
                cap_item = _pick_cap_block(state)
                if cap_item is None:
                    raise Replan("缺少非重力封口方块")
                yield from self._place_cell(ex, state, cap_item, sc, dim=dim)
            stand_pos = [icx, int(plan["stand_y"]), icz]
        else:
            # 垂直下挖前若脚底偏移格心过多，先居中防卡边导致 blocked_by_self
            if abs(float(px) - (fx + 0.5)) > 0.2 or abs(float(pz) - (fz + 0.5)) > 0.2:
                center_pt = [fx + 0.5, float(fy), fz + 0.5]
                yield rec("move_to", ex.move_to(pos=center_pt, rng=0.2, timeout=4.0, retries=0, stop_on=("death",)), pos=center_pt)

            req_caps = 1 if mode == "dig3_fill1" else 2
            for cell in plan["dig_cells"]:
                need_collect = _count_cap_blocks(state) < req_caps
                yield from self._dig_cell(ex, state, cell, collect=need_collect, dim=dim)

            # 实机中等待重力落地稳定再封口（GridWorld 等无重力桩 on_ground=True 不等待）
            st_pit = state.refresh_status(ex.client, max_age_s=0)
            for _ in range(5):
                if st_pit.get("on_ground") is not False:
                    break
                time.sleep(0.08)
                st_pit = state.refresh_status(ex.client, max_age_s=0)

            if mode == "dig2_anchor_fill1":
                anchor_item = _pick_cap_block(state)
                if anchor_item is None:
                    raise Replan("缺少锚点方块")
                yield from self._place_cell(ex, state, anchor_item, plan["anchor_cell"], dim=dim)

            cap_item = _pick_cap_block(state)
            if cap_item is None:
                raise Replan("缺少非重力封顶方块")
            yield from self._place_cell(ex, state, cap_item, plan["cap_cell"], dim=dim)
            stand_pos = [fx, int(plan["stand_y"]), fz]

        setter = getattr(state, "set_sealed_shelter", None)
        entered_at = time.monotonic()
        if callable(setter):
            setter({
                "pos": stand_pos,
                "cap": list(plan["cap_cell"]),
                "kind": mode,
                "dim": "overworld",
                "entered_at": entered_at,
            })
        try:
            st_inside = state.refresh_status(ex.client, max_age_s=0)
            entry_hp = float(st_inside.get("health", 20.0) or 20.0)
            should_exit = yield from self._wait_inside(ex, state, mode=mode, entered_at=entered_at, entry_hp=entry_hp)
            if not should_exit:
                return False
            yield from self._exit_shelter(ex, state, plan, fx, fy, fz, dim=dim)
            self._exited_cleanly = True
        finally:
            if callable(setter):
                setter(None)

    def verify_done(self, state: "State", before_counts: Optional[dict[str, int]] = None) -> str:
        if state_snap(state).get("alive") is False:
            return "角色在避难期间未存活"
        if getattr(self, "_exited_cleanly", False):
            return ""
        return "避难所尚未完成安全出坑"
