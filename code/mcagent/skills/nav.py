"""L2 战术原语：`move_to` 被拒之后的手动兜底与脱困策略。

这三段序列先在 `probe --pathfinding --manual-only` 里真机跑通（本地 1.21.11 靶场，
见 docs/06 2026-11-14 01:33），再原样搬到这里；probe 现在直接导入本文件做验收，
所以"测过的"和"上场的"是同一份代码。

每个原语都是生成器：**每发一个阻塞动作 yield 一条记录**。L3 技能用 `yield from` 组合，
probe 用 `for rec in ...` 收集证据。它们不进注册表，模型选不到。

要挖/要放哪些格由调用方给出，且**必须来自当场观察**（`find_blocks` / `get_block`），
不能是模型给的数字。原语只负责把这串格子安全地做完。
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Callable, Iterator, Optional, Sequence

from ..client import ApiError
from ..runtime import Fatal, Replan
from .base import failure, rec
from .items import (
    AIR,
    NAV_FAIL_CODES,
    PASSABLE_COVER,
    bare,
    best_pickaxe,
    block_name,
    is_clear_space,
    is_safe_solid_floor,
    pick_placeable,
    total_placeable,
)
from .voxel_cache import voxel_cache_for

if TYPE_CHECKING:
    from ..runtime import Executor, State

Pos = Sequence[float]

#: 离待放格中心超过这个距离就先走近一步（真机实测值，改之前先重跑 probe）
PLACE_REACH = 3.5
#: 玩家水平半宽 0.3 + 半格 0.5，再留一点余量：小于它说明人和这一格水平重叠
SELF_OVERLAP = 0.81
PLAYER_HEIGHT = 1.8

_CARDINAL_DIRS: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))

#: 破拆时上方不能是重力或液体方块，否则会掉落伤害或淹没
_UNSAFE_ROOF_BLOCKS: frozenset[str] = frozenset({
    "water", "lava", "flowing_water", "flowing_lava", "bubble_column",
    "sand", "red_sand", "gravel", "suspicious_sand", "suspicious_gravel",
    "anvil", "chipped_anvil", "damaged_anvil", "pointed_dripstone",
})

#: 自动脱困绝不能破坏的方块（基岩、功能方块、黑曜石、容器、传送门、流体）
_FORBIDDEN_BREACH_BLOCKS: frozenset[str] = frozenset({
    "bedrock", "obsidian", "crying_obsidian",
    "crafting_table", "furnace", "blast_furnace", "smoker",
    "chest", "trapped_chest", "ender_chest", "barrel",
    "end_portal_frame", "spawner", "trial_spawner", "vault",
    "enchanting_table", "anvil", "chipped_anvil", "damaged_anvil",
    "brewing_stand", "beacon", "tnt", "reinforced_deepslate",
    "water", "lava", "flowing_water", "flowing_lava", "bubble_column",
    "fire", "soul_fire", "nether_portal", "end_portal", "end_gateway",
})

#: 无镐时也允许徒手挖掘的软方块
_SOFT_BREACH_BLOCKS: frozenset[str] = frozenset({
    "snow", "snow_block", "powder_snow", "cobweb", "dirt", "grass_block",
    "coarse_dirt", "rooted_dirt", "podzol", "mycelium", "mud",
    "muddy_mangrove_roots", "clay", "sand", "red_sand", "gravel",
    "soul_sand", "soul_soil", "moss_block", "pale_moss_block",
    "farmland", "dirt_path", "hay_block", "sweet_berry_bush",
})

_BRIDGE_GAP_BLOCKS: frozenset[str] = AIR | {"water", "flowing_water"}


def _is_protected(protected_fn: Optional[Callable[[Pos], bool]], pos: Pos) -> bool:
    if protected_fn is None:
        return False
    try:
        return bool(protected_fn(pos))
    except Exception:
        return False


def _read_block(ex: "Executor", pos: Pos) -> Optional[dict]:
    """读取指定位置方块属性；若区块未加载则安全返回 None，遇到参数等错误照常抛出。"""
    p = list(pos) if isinstance(pos, (list, tuple)) else pos
    try:
        res = ex.client.get_block(p)
    except ApiError as e:
        if e.code in ("not_loaded", "not_ready"):
            return None
        raise
    return res if isinstance(res, dict) else None


def _order_directions(start_pos: Sequence[float], dest: Optional[Pos]) -> list[tuple[int, int]]:
    """若有目的地方向，按点积从大到小稳定排序水平四方向。"""
    if dest is not None and len(dest) >= 3:
        try:
            dx_target = float(dest[0]) - float(start_pos[0])
            dz_target = float(dest[2]) - float(start_pos[2])
            if math.isfinite(dx_target) and math.isfinite(dz_target):
                return sorted(_CARDINAL_DIRS, key=lambda d: d[0] * dx_target + d[1] * dz_target, reverse=True)
        except (TypeError, ValueError):
            pass
    return list(_CARDINAL_DIRS)


def _is_forbidden_breach_name(name: str) -> bool:
    if not name:
        return True
    if name in _FORBIDDEN_BREACH_BLOCKS:
        return True
    if name.endswith("_ore") or name == "ore":
        return True
    if name.endswith("_shulker_box") or name == "shulker_box":
        return True
    if name.endswith("_bed") or name == "bed":
        return True
    return False


def _is_soft_cover(name: str) -> bool:
    return (
        name == "snow"
        or name in PASSABLE_COVER
        or name == "cobweb"
        or (name.endswith("_leaves") and len(name) > len("_leaves"))
    )


def _is_soft_breach_name(name: str) -> bool:
    return name in AIR or name in _SOFT_BREACH_BLOCKS or _is_soft_cover(name)


def _can_breach_block(blk: Optional[dict], has_pick: bool) -> bool:
    if blk is None or blk.get("diggable") is False:
        return False
    name = bare(blk.get("name", "")).strip()
    if not name or _is_forbidden_breach_name(name):
        return False
    if _is_soft_breach_name(name):
        return True
    return has_pick


def _is_preemption(e: BaseException) -> bool:
    code, _ = failure(e)
    return code in ("replan", "interrupted") or "抢占" in str(e)


def _step_recover(ex: "Executor", target_pos: list[float]) -> Iterator[dict]:
    """执行一次脱困移动并按单步单动作契约 yield，仅吞掉已知寻路失败码。"""
    try:
        res = ex.move_to(pos=target_pos, rng=0.5, timeout=20.0, retries=0)
        yield rec("move_to_recover", res, pos=target_pos)
    except Replan as e:
        if _is_preemption(e):
            raise
        code, _ = failure(e)
        if code in NAV_FAIL_CODES or code == "timeout":
            yield rec("move_to_recover", pos=target_pos, error_code=code)
        else:
            raise


def _fresh_pos(ex: "Executor", state: "State") -> list[float]:
    pos = state.refresh_status(ex.client, max_age_s=0).get("pos")
    if not pos:
        raise Replan("status 里没有坐标，无法继续手动兜底")
    return pos


def _release_sneak(ex: "Executor") -> None:
    try:
        ex.posture(sneak=False)
    except Exception:
        pass  # 收尾不能再抛：这里可能正处在抢占路径上


def _invalidate_voxel(state: "State", pos: Pos) -> None:
    voxel_cache_for(state).invalidate(pos, dimension=str((state.st or {}).get("dimension") or "overworld"))


def breach_1x2(ex: "Executor", state: "State", blocks: Sequence[Pos]) -> Iterator[dict]:
    """换上最好的镐，按顺序挖开 `blocks`（通常是挡路的上、下两格）。已是空气的格跳过。"""
    state.refresh_inventory(ex.client)
    pick = best_pickaxe(state)
    if pick:
        yield rec("equip", ex.equip(pick, "hand"), item=pick)
    for pos in blocks:
        name = block_name(ex, pos)
        if name and name not in AIR:
            res = ex.dig(pos)
            _invalidate_voxel(state, pos)
            yield rec("dig", res, pos=pos)


def sneak_bridge(ex: "Executor", state: "State", blocks: Sequence[Pos],
                 item: Optional[str] = None) -> Iterator[dict]:
    """潜行着从近到远铺 `blocks`（桥面格，按离人由近到远排好）。结束或被打断都会松开潜行。"""
    entered = ex.posture(sneak=True)
    try:  # 从进入潜行起就要保证能松开：第一个 yield 也可能是被切走的地方
        yield rec("posture", entered, sneak=True)
        for idx, pos in enumerate(blocks):
            cur = _fresh_pos(ex, state)
            center = [pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5]
            if idx > 0 and math.dist(cur, center) > PLACE_REACH:
                prev = blocks[idx - 1]
                edge = [prev[0] + 0.5, prev[1] + 1, prev[2] + 0.5]
                yield rec("move_to_bridge", ex.move_to(pos=edge, rng=1, timeout=20.0, retries=0), pos=edge)
                ex.posture(sneak=True)
            state.refresh_inventory(ex.client)
            block = pick_placeable(state, item)
            res = ex.place(block, pos)
            _invalidate_voxel(state, pos)
            yield rec("place", res, item=block, pos=pos)
    finally:
        _release_sneak(ex)
    yield {"op": "posture", "sneak": False}


def step_up_1x2(ex: "Executor", state: "State", blocks: Sequence[Pos],
                item: Optional[str] = None) -> Iterator[dict]:
    """按 `blocks` 的顺序在相邻两列交替垫块往上走。

    脚下那一格直接放会被服务端拒绝（`blocked_by_self`，真机已证实），所以要两列交替：
    待放格和自己重叠时，先迈到上一块刚放好的方块顶上，再回头放。已有方块的格跳过。
    """
    last: Optional[list[float]] = None
    for pos in blocks:
        cur = _fresh_pos(ex, state)
        horiz = abs(cur[0] - (pos[0] + 0.5)) < SELF_OVERLAP and abs(cur[2] - (pos[2] + 0.5)) < SELF_OVERLAP
        vert = cur[1] < pos[1] + 1.0 and cur[1] + PLAYER_HEIGHT > pos[1]
        if horiz and vert and last is not None:
            top = [last[0] + 0.5, last[1] + 1, last[2] + 0.5]
            yield rec("step_up_to", ex.move_to(pos=top, rng=1, timeout=20.0, retries=0), pos=top)
        if block_name(ex, pos) in AIR:
            state.refresh_inventory(ex.client)
            block = pick_placeable(state, item)
            res = ex.place(block, pos)
            _invalidate_voxel(state, pos)
            yield rec("place", res, item=block, pos=pos)
        last = list(pos)


def recover_move(
    ex: "Executor",
    state: "State",
    dest: Optional[Pos] = None,
    *,
    protected_fn: Optional[Callable[[Pos], bool]] = None,
    min_displacement: float = 0.8,
) -> Iterator[dict]:
    """L2 战术兜底：move_to 被拒或卡死后，就地观察并按四种战术逐级尝试脱困。"""
    start_pos = list(_fresh_pos(ex, state))
    fx, fy, fz = math.floor(start_pos[0]), math.floor(start_pos[1]), math.floor(start_pos[2])

    def _moved_enough() -> bool:
        cur = _fresh_pos(ex, state)
        return math.dist(cur, start_pos) >= min_displacement

    # Step 1: 清理自身所在垂直两格（软方块/落雪/蜘蛛网等）
    for y in (fy, fy + 1):
        pos = [fx, y, fz]
        blk = _read_block(ex, pos)
        if blk is None:
            continue
        name = bare(blk.get("name", "")).strip()
        if (
            name not in AIR
            and _is_soft_cover(name)
            and blk.get("diggable") is not False
            and not _is_protected(protected_fn, pos)
        ):
            try:
                res = ex.dig(pos)
                _invalidate_voxel(state, pos)
                yield rec("dig", res, pos=pos)
            except Replan as e:
                if _is_preemption(e):
                    raise
                code, _ = failure(e)
                yield rec("dig", pos=pos, error_code=code)
            if _moved_enough():
                return

    # Step 2: 按目标方向对水平四方向排序
    dirs = _order_directions(start_pos, dest)

    # Step 3 (tactic a): 迈向无障碍的相邻格 (水平/上一格/下一格)
    tactic_a_attempted = False
    for dx, dz in dirs:
        if tactic_a_attempted:
            break
        nx, nz = fx + dx, fz + dz
        for ny in (fy, fy + 1, fy - 1):
            if _is_protected(protected_fn, [nx, ny, nz]) or _is_protected(protected_fn, [nx, ny + 1, nz]):
                continue
            if not is_safe_solid_floor(_read_block(ex, [nx, ny - 1, nz])):
                continue
            if not (is_clear_space(_read_block(ex, [nx, ny, nz])) and is_clear_space(_read_block(ex, [nx, ny + 1, nz]))):
                continue
            if ny == fy + 1 and not is_clear_space(_read_block(ex, [fx, fy + 2, fz])):
                continue
            if ny == fy - 1 and not is_clear_space(_read_block(ex, [nx, fy + 1, nz])):
                continue

            tactic_a_attempted = True
            yield from _step_recover(ex, [nx + 0.5, float(ny), nz + 0.5])
            if _moved_enough():
                return
            break

    # Step 4 (tactic b): breach_1x2 破拆挡路障碍
    state.refresh_inventory(ex.client)
    has_pick = best_pickaxe(state) is not None

    for dx, dz in dirs:
        nx, nz = fx + dx, fz + dz
        b_top = _read_block(ex, [nx, fy + 1, nz])
        b_bot = _read_block(ex, [nx, fy, nz])
        b_floor = _read_block(ex, [nx, fy - 1, nz])
        b_roof = _read_block(ex, [nx, fy + 2, nz])

        if b_top is None or b_bot is None:
            continue
        if is_clear_space(b_top) and is_clear_space(b_bot):
            continue
        if not is_safe_solid_floor(b_floor):
            continue
        if b_roof is None:
            continue
        roof_name = bare(b_roof.get("name", "")).strip()
        if not roof_name or roof_name in _UNSAFE_ROOF_BLOCKS:
            continue
        if _is_protected(protected_fn, [nx, fy + 1, nz]) or _is_protected(protected_fn, [nx, fy, nz]):
            continue
        if not (_can_breach_block(b_top, has_pick) and _can_breach_block(b_bot, has_pick)):
            continue

        breach_failed = False
        try:
            yield from breach_1x2(ex, state, [[nx, fy + 1, nz], [nx, fy, nz]])
        except Replan as e:
            if _is_preemption(e):
                raise
            code, _ = failure(e)
            yield rec("breach_recover", pos=[nx, fy, nz], error_code=code)
            breach_failed = True

        if _moved_enough():
            return

        if not breach_failed:
            if (
                is_clear_space(_read_block(ex, [nx, fy, nz]))
                and is_clear_space(_read_block(ex, [nx, fy + 1, nz]))
                and is_safe_solid_floor(_read_block(ex, [nx, fy - 1, nz]))
            ):
                yield from _step_recover(ex, [nx + 0.5, float(fy), nz + 0.5])
                if _moved_enough():
                    return
        break

    # Step 5 (tactic c): step_up_1x2 垫高翻越 2 格高台
    state.refresh_inventory(ex.client)
    if total_placeable(state) >= 2:
        b_fx1 = _read_block(ex, [fx, fy + 1, fz])
        bot_col_ok = (
            b_fx1 is not None
            and bare(b_fx1.get("name", "")) in AIR
            and not _is_protected(protected_fn, [fx, fy + 1, fz])
            and is_clear_space(_read_block(ex, [fx, fy + 2, fz]))
            and is_clear_space(_read_block(ex, [fx, fy + 3, fz]))
        )
        if bot_col_ok:
            for dx, dz in dirs:
                nx, nz = fx + dx, fz + dz
                b_n0 = _read_block(ex, [nx, fy, nz])
                b_n1 = _read_block(ex, [nx, fy + 1, nz])
                if b_n0 is None or is_clear_space(b_n0) or not is_safe_solid_floor(b_n1):
                    continue
                if not (is_clear_space(_read_block(ex, [nx, fy + 2, nz])) and is_clear_space(_read_block(ex, [nx, fy + 3, nz]))):
                    continue
                if _is_protected(protected_fn, [nx, fy + 2, nz]) or _is_protected(protected_fn, [nx, fy + 3, nz]):
                    continue

                candidates: list[tuple[int, tuple[int, int], bool]] = []
                for adx, adz in _CARDINAL_DIRS:
                    if (adx, adz) == (dx, dz):
                        continue
                    ax, az = fx + adx, fz + adz
                    if _is_protected(protected_fn, [ax, fy, az]) or _is_protected(protected_fn, [ax, fy + 1, az]):
                        continue
                    if not is_safe_solid_floor(_read_block(ex, [ax, fy - 1, az])):
                        continue
                    if not (is_clear_space(_read_block(ex, [ax, fy + 1, az])) and is_clear_space(_read_block(ex, [ax, fy + 2, az]))):
                        continue
                    b_a0 = _read_block(ex, [ax, fy, az])
                    if b_a0 is None:
                        continue
                    name_a0 = bare(b_a0.get("name", "")).strip()
                    if name_a0 in AIR:
                        candidates.append((0, (ax, az), False))
                    elif _is_soft_cover(name_a0) and b_a0.get("diggable") is not False:
                        candidates.append((1, (ax, az), True))

                if not candidates:
                    continue

                candidates.sort(key=lambda c: c[0])
                _, (ax, az), need_dig = candidates[0]

                if need_dig:
                    dig_ok = True
                    try:
                        res = ex.dig([ax, fy, az])
                        _invalidate_voxel(state, [ax, fy, az])
                        yield rec("dig", res, pos=[ax, fy, az])
                    except Replan as e:
                        if _is_preemption(e):
                            raise
                        code, _ = failure(e)
                        yield rec("dig", pos=[ax, fy, az], error_code=code)
                        dig_ok = False
                    if not dig_ok:
                        if _moved_enough():
                            return
                        break

                step_up_failed = False
                try:
                    yield from step_up_1x2(ex, state, [[ax, fy, az], [fx, fy + 1, fz]])
                except Replan as e:
                    if _is_preemption(e):
                        raise
                    code, _ = failure(e)
                    yield rec("step_up_recover", pos=[fx, fy + 1, fz], error_code=code)
                    step_up_failed = True

                if step_up_failed:
                    if _moved_enough():
                        return
                    break

                # step_up_1x2 停在相邻柱顶 [ax+0.5, fy+1, az+0.5]，必须再迈上目标高台顶面再检查位移
                yield from _step_recover(ex, [nx + 0.5, float(fy + 2), nz + 0.5])
                if _moved_enough():
                    return
                break

    # Step 6 (tactic d): sneak_bridge 潜行搭桥跨空隙
    state.refresh_inventory(ex.client)
    avail = total_placeable(state)
    if avail >= 1:
        tactic_d_attempted = False
        for dx, dz in dirs:
            if tactic_d_attempted:
                break
            bridge_cells: list[Pos] = []
            for k in range(1, 5):
                cx, cz = fx + k * dx, fz + k * dz
                b_floor = _read_block(ex, [cx, fy - 1, cz])
                b_h0 = _read_block(ex, [cx, fy, cz])
                b_h1 = _read_block(ex, [cx, fy + 1, cz])

                if not (is_clear_space(b_h0) and is_clear_space(b_h1)):
                    break

                if (
                    k >= 2
                    and is_safe_solid_floor(b_floor)
                    and not _is_protected(protected_fn, [cx, fy, cz])
                    and not _is_protected(protected_fn, [cx, fy + 1, cz])
                ):
                    if avail >= len(bridge_cells) and len(bridge_cells) > 0:
                        tactic_d_attempted = True
                        bridge_failed = False
                        try:
                            yield from sneak_bridge(ex, state, bridge_cells)
                        except Replan as e:
                            if _is_preemption(e):
                                raise
                            code, _ = failure(e)
                            yield rec(
                                "bridge_recover",
                                pos=bridge_cells[-1],
                                error_code=code,
                            )
                            bridge_failed = True

                        if bridge_failed:
                            if _moved_enough():
                                return
                            break

                        # 搭完桥后跨过桥面踏上安全陆地，再判定位移是否脱困
                        yield from _step_recover(ex, [cx + 0.5, float(fy), cz + 0.5])
                        if _moved_enough():
                            return
                    break

                if (
                    k <= 3
                    and b_floor is not None
                    and bare(b_floor.get("name", "")) in _BRIDGE_GAP_BLOCKS
                    and not _is_protected(protected_fn, [cx, fy - 1, cz])
                ):
                    bridge_cells.append([cx, fy - 1, cz])
                else:
                    break
