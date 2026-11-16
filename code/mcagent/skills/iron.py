"""铁矿采集与熔炼 L3；窗口协议见 agent-api-spec-dist §4.9。

只依赖当场暴露面观察；燃烧耗时仅作有界等待预算，不当作成功判据。
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Collection, Iterator, Optional
from weakref import WeakKeyDictionary

from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, failure, rec
from .field import move_local
from .furnace import collect_furnace, inspect_furnace, load_furnace
from .items import AIR, STONE_OR_BETTER_PICKS, bare, best_pickaxe, record_mine_trail
from .mining import (
    SOLID_SUPPORT, MineStepUnsafe, mine_step, prepare_mining_pick, safe_mine_column,
    under_feet,
)
from .recipes import craft_recipe
from .registry import register
from .sightline import API_DIG_EYE_REACH, DIG_EYE_REACH, can_reach_block, eye_to_block_dist
from .windows import window_amount, window_section

if TYPE_CHECKING:
    from ..runtime import Executor, State

IRON_ORES = ("iron_ore", "deepslate_iron_ore", "raw_iron_block")
WOODS = ("oak", "birch", "spruce", "jungle", "acacia", "dark_oak", "cherry", "mangrove", "pale_oak")
# 半个冶炼单位：煤/木炭 8 个，木板/原木 1.5 个；退役木镐 1 个、木棍 0.5 个仅作兜底。
BASE_FUELS = (("coal", 16), ("charcoal", 16)) + tuple(
    (f"{w}_{suffix}", 3) for suffix in ("planks", "log") for w in WOODS
)
RADIUS = 32
MINING_REACH = DIG_EYE_REACH
PROSPECT_STEPS = 6
SHALLOW_IRON_Y = 16
MAX_PROSPECT_STEPS = 56
MAX_ORE_ROUTE_FAILURES = 3
POLL_SECONDS = 2.0
# 同连接、维度、区块和生存阶段共享不可达矿点记忆。
_SESSION_EXCLUDED = WeakKeyDictionary()


def reset_iron_memo() -> None:
    _SESSION_EXCLUDED.clear()


def _session_excluded_for(client: object, state: "State", status: dict) -> set[tuple[int, int, int]]:
    cur = status.get("pos") or state.pos
    deaths = tuple(state.deaths)
    scope = (status.get("dimension"), math.floor(cur[0] / 16), math.floor(cur[2] / 16), deaths[-1:])
    entry = _SESSION_EXCLUDED.get(client)
    if entry is None or entry[0] != scope:
        fresh: set[tuple[int, int, int]] = set()
        _SESSION_EXCLUDED[client] = (scope, fresh)
        return fresh
    return entry[1]


def _fuels_for(state: "State") -> tuple[tuple[str, int], ...]:
    extra: list[tuple[str, int]] = []
    if best_pickaxe(state) in STONE_OR_BETTER_PICKS:
        extra.append(("wooden_pickaxe", 2))
    extra.append(("stick", 1))
    return BASE_FUELS + tuple(extra)


class _OreRouteUnconfirmed(Replan):
    pass


def _log_for_fuel(stock, fuels, count: int) -> Optional[str]:
    non_log = sum(stock(n) * u // 2 for n, u in fuels if not n.endswith("_log"))
    direct = sum(stock(n) * u // 2 for n, u in fuels)
    if non_log >= count or (direct >= count and count < 3):
        return None
    return next((w for w in WOODS if stock(f"{w}_log") > 0), None)


def _fuel_batches(stock, fuels, remaining: int) -> tuple[list[tuple[str, int, int]], int]:
    plan = []
    for item, units in fuels:
        available = stock(item)
        if item == "stick" and available > 2 and remaining * 2 <= (available - 2) * units:
            available -= 2
        batch = min(remaining, available * units // 2)
        if batch:
            plan.append((item, math.ceil(batch * 2 / units), batch))
            remaining -= batch
        if not remaining:
            break
    return plan, remaining


@register
class SmeltIron(Skill):
    name = "smelt_iron"
    description = "采集暴露铁矿，缺矿时有界阶梯探矿，再安全熔炼取出铁锭"
    params = {"count": Param(int, "期望新增铁锭数（粗铁用完即止）", default=3, min=1, max=16)}
    goal = "acq:iron_ingot"
    verified = "offline"

    @classmethod
    def material_plan(cls, args: dict, state: "State") -> tuple[tuple[str, int, str], ...]:
        """Declare the current stock the planned batch can use; mined inputs enter later."""
        count = args.get("count", 3)
        raw = min(count, state.count("raw_iron"))
        blocks = min(state.count("raw_iron_block"), math.ceil((count - raw) / 9))
        fuels = _fuels_for(state)
        stock = {item: state.count(item) for item, _ in fuels}
        materials = {"raw_iron": raw, "raw_iron_block": blocks}
        batch = min(count, raw + 9 * blocks) or count
        for _ in range(batch):
            wood = _log_for_fuel(stock.__getitem__, fuels, batch)
            if wood is None:
                break
            log, plank = f"{wood}_log", f"{wood}_planks"
            materials[log] = materials.get(log, 0) + 1
            stock[log] -= 1
            stock[plank] += 4
        for item, qty, _ in _fuel_batches(stock.__getitem__, fuels, batch)[0]:
            materials[item] = materials.get(item, 0) + min(qty, state.count(item))
        return tuple((item, qty, "consume") for item, qty in materials.items() if qty)

    @classmethod
    def blocked(cls, state: "State") -> str:
        has_iron_input = state.count("raw_iron") >= 1 or state.count("raw_iron_block") >= 1
        if not has_iron_input and best_pickaxe(state) not in STONE_OR_BETTER_PICKS:
            return "背包无粗铁且缺石镐或更好的镐（木镐、金镐不能采铁）"
        if not any(state.count(n) for n, _ in _fuels_for(state)) and best_pickaxe(state) not in STONE_OR_BETTER_PICKS:
            return "背包缺煤、木板或原木燃料，且无可采煤的镐"
        return ""

    def _unpack_raw_iron_blocks(self, ex: "Executor", state: "State") -> Iterator[dict]:
        deficit = max(0, self.args["count"] - state.count("raw_iron"))
        for _ in range(min(state.count("raw_iron_block"), math.ceil(deficit / 9))):
            before = state.count("raw_iron")
            yield rec("craft", ex.craft("raw_iron", 1), item="raw_iron")
            state.refresh_inventory(ex.client)
            if state.count("raw_iron") <= before:
                raise Replan("拆解 raw_iron_block 后背包 raw_iron 未增加")

    def _mine(self, ex: "Executor", state: "State", pos, ores, drop: str) -> Iterator[dict]:
        status = state.refresh_status(ex.client, max_age_s=0)
        dimension = status.get("dimension")
        cur = status.get("pos") or state.pos
        stance = status if status.get("pos") is not None else cur
        if not can_reach_block(ex, stance, pos, reach=MINING_REACH):
            yield rec("move_to", ex.move_to(pos=pos, rng=2), pos=pos)
        status = state.refresh_status(ex.client, max_age_s=0)
        cur = status.get("pos") or state.pos
        stance = status if status.get("pos") is not None else cur
        if status.get("dimension") != dimension:
            raise Replan("移动到矿点期间维度变化，重新观察后再挖")
        if under_feet(cur, pos):
            raise Replan(f"观察到 {drop} 矿 {pos} 已在脚下，停止挖掘以保留支撑")
        if not can_reach_block(ex, stance, pos, reach=API_DIG_EYE_REACH):
            raise _OreRouteUnconfirmed(f"移动后未确认到达矿点 {pos} 的挖掘范围")
        block_type = bare(ex.client.get_block(pos).get("name"))
        if block_type not in ores:
            raise Replan(f"到达 {pos} 后 get_block 未确认目标矿 {ores}")
        yield from prepare_mining_pick(ex, state)
        status = state.refresh_status(ex.client, max_age_s=0)
        cur = status.get("pos") or state.pos
        stance = status if status.get("pos") is not None else cur
        if under_feet(cur, pos):
            raise Replan(f"装备后重新观察 {pos} 已在脚下，停止挖掘以保留支撑")
        if (status.get("dimension") != dimension
                or not can_reach_block(ex, stance, pos, reach=API_DIG_EYE_REACH)
                or bare(ex.client.get_block(pos).get("name")) != block_type):
            raise Replan(f"装备后矿点 {pos} 或站位已变化，重新观察后再挖")
        expected_drop = "raw_iron_block" if block_type == "raw_iron_block" else drop
        before = state.count(expected_drop)
        result = ex.dig(pos, collect=True)
        yield rec("dig", result, pos=pos)
        state.refresh_inventory(ex.client)
        if result.get("warning") or state.count(expected_drop) <= before:
            raise Replan(f"挖掘 {pos} 后背包 {expected_drop} 未确认增加或工具告警：{result.get('warning', '')}")
        if expected_drop == "raw_iron_block":
            yield from self._unpack_raw_iron_blocks(ex, state)

    def _ore_candidates(
        self, ex: "Executor", state: "State", radius=RADIUS, *,
        excluded: Collection[tuple[int, int, int]] = (),
        in_reach_only: bool = False,
        max_dy: Optional[float] = None,
    ) -> list:
        search_radius = math.ceil(API_DIG_EYE_REACH) if in_reach_only else radius
        limit = 16 if in_reach_only else 64
        blocks = ex.client.find_blocks(types=list(IRON_ORES), radius=search_radius, limit=limit).get("blocks") or []
        status = state.refresh_status(ex.client, max_age_s=0)
        cur = status.get("pos") or state.pos
        stance = status if status.get("pos") is not None else cur
        cands = [
            b["pos"] for b in blocks
            if isinstance(b.get("pos"), list) and len(b["pos"]) == 3
            and tuple(b["pos"]) not in excluded
            and (max_dy is None or abs(b["pos"][1] - cur[1]) <= max_dy)
            and not under_feet(cur, b["pos"])
            and (not in_reach_only or can_reach_block(ex, stance, b["pos"], reach=MINING_REACH))
        ]
        if not in_reach_only and len(cands) > 1:
            # 稳定排序：优先高差 ≤12 格的地表崖壁/浅洞露矿，同档内保留原扫描近远顺序。
            cands.sort(key=lambda p: abs(p[1] - cur[1]) > 12.0)
        return cands

    @staticmethod
    def _initial_heading(start: tuple[int, int, int], excluded: Collection[tuple[int, int, int]]) -> tuple[int, int]:
        deep = [p for p in excluded if p[1] <= start[1] - 4]
        if not deep:
            return (1, 0)
        nearest = min(deep, key=lambda p: math.hypot(p[0] - start[0], p[2] - start[2]))
        dx, dz = nearest[0] - start[0], nearest[2] - start[2]
        if abs(dx) >= abs(dz):
            return (1, 0) if dx >= 0 else (-1, 0)
        return (0, 1) if dz >= 0 else (0, -1)

    def _prospect(
        self, ex: "Executor", state: "State", excluded: set[tuple[int, int, int]], *,
        in_reach_only: bool, checked_wide_shallow: bool = False,
    ) -> Iterator[dict]:
        # API §3.3 允许 radius <= 64，但仍只返回暴露矿，不能据空扫描断言环境无矿。
        if not in_reach_only:
            candidates = self._ore_candidates(ex, state, radius=64, excluded=excluded)
            if candidates:
                return candidates
        elif not checked_wide_shallow:
            shallow_wide = self._ore_candidates(ex, state, radius=64, excluded=excluded, max_dy=12.0)
            if shallow_wide:
                return shallow_wide

        cur0 = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
        start = tuple(map(math.floor, cur0))
        start_y = start[1]
        max_steps = min(MAX_PROSPECT_STEPS, max(PROSPECT_STEPS, start_y - SHALLOW_IRON_Y))
        visited: set[tuple[int, int]] = set()
        completed_steps = 0
        backtracks = 0
        heading = self._initial_heading(start, excluded)
        waypoints: list[list[float]] = [[start[0] + 0.5, float(start_y), start[2] + 0.5]]

        def observe_cut():
            if state.count("raw_iron_block") >= 1:
                yield from self._unpack_raw_iron_blocks(ex, state)
            candidates = self._ore_candidates(ex, state, excluded=excluded, in_reach_only=in_reach_only)
            return candidates if candidates or state.count("raw_iron") else None

        try:
            while completed_steps < max_steps:
                cur = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
                x, y, z = map(math.floor, cur)
                visited.add((x, z))
                dirs = (heading,) + tuple(d for d in ((1, 0), (-1, 0), (0, 1), (0, -1)) if d != heading)
                pos = next(([x + dx, y - 1, z + dz] for dx, dz in dirs
                            if (x + dx, z + dz) not in visited
                            and safe_mine_column(ex, [x + dx, y - 1, z + dz])), None)
                if pos is None:
                    # 井底正前与两侧均遇水/砾石受阻时，沿已挖阶梯后退一级尝试侧向分支。
                    if len(waypoints) >= 2 and backtracks < 2:
                        prev = waypoints[-2]
                        status = yield from move_local(ex, state, prev, require_move=True)
                        if tuple(map(math.floor, status.get("pos") or state.pos)) != tuple(map(math.floor, prev)):
                            raise Replan("未确认退到上一阶，保留原阶梯轨迹供退出")
                        backtracks += 1
                        waypoints.pop()
                        continue
                    break
                visited.add((pos[0], pos[2]))
                try:
                    dest, observation = yield from mine_step(ex, state, pos, after_dig=observe_cut)
                except MineStepUnsafe as err:
                    if completed_steps > 0:
                        continue
                    if err.stage == "cut" and any(
                        (x + dx, z + dz) not in visited and safe_mine_column(ex, [x + dx, y - 1, z + dz])
                        for dx, dz in dirs
                    ):
                        continue
                    raise
                if dest is None:
                    return observation
                heading = (pos[0] - x, pos[2] - z)
                waypoints.append(dest)
                completed_steps += 1
                candidates = self._ore_candidates(ex, state, excluded=excluded, in_reach_only=in_reach_only)
                if candidates:
                    return candidates
        finally:
            if completed_steps > 0:
                record_mine_trail(waypoints)

        end = tuple(map(math.floor, state.pos))
        scan_scope = f"仅查近场 {MINING_REACH} 格" if in_reach_only else "半径 32/64"
        raise EnvBlocked(
            f"{scan_scope} 内未找到可采的暴露铁矿，"
            f"安全阶梯探矿仍未发现（Y={start_y} 到 {end[1]}，走完 {completed_steps}/{max_steps} 级，"
            f"浅矿目标 Y≈{SHALLOW_IRON_Y}）；需换区域探索",
            missing_predicate="exposed_iron",
            searched_volume=(f"起点 {start} 暴露矿扫描{scan_scope}；"
                             f"跳过 {len(excluded)} 个已试不可达矿点；至 {end} 阶梯 {completed_steps}/{max_steps} 级"),
            wake_on="chunk_moved",
        )

    def _collect(self, ex: "Executor", state: "State") -> Iterator[dict]:
        if state.count("raw_iron_block") >= 1:
            yield from self._unpack_raw_iron_blocks(ex, state)
        if state.count("raw_iron"):
            return
        status = state.refresh_status(ex.client, max_age_s=0)
        excluded = _session_excluded_for(ex.client, state, status)
        route_failures = len(excluded)
        checked_wide_shallow = False
        for _ in range(self.args["count"] + MAX_ORE_ROUTE_FAILURES + 2):
            # 同一深洞常返回多块矿。直达预算用完后，只试一次 64 格浅表露矿便切入近场阶梯探矿。
            in_reach_only = route_failures >= MAX_ORE_ROUTE_FAILURES
            candidates = self._ore_candidates(ex, state, excluded=excluded, in_reach_only=in_reach_only)
            if not candidates:
                if state.count("raw_iron"):
                    return
                candidates = yield from self._prospect(
                    ex, state, excluded,
                    in_reach_only=in_reach_only,
                    checked_wide_shallow=checked_wide_shallow,
                )
                if in_reach_only:
                    checked_wide_shallow = True
                if state.count("raw_iron"):
                    return
            pos = candidates[0]
            try:
                yield from self._mine(ex, state, pos, IRON_ORES, "raw_iron")
            except Replan as err:
                code, _ = failure(err)
                if isinstance(err, _OreRouteUnconfirmed):
                    code = "arrival_unconfirmed"
                dig_skip = code in ("dig_unconfirmed", "out_of_reach", "protected_region")
                route_skip = code in (
                    "path_not_found", "unreachable", "move_timeout", "stalled", "arrival_unconfirmed",
                )
                if not dig_skip and (
                    (
                        in_reach_only
                        and eye_to_block_dist(state.st, pos) <= MINING_REACH
                        and can_reach_block(ex, state.st, pos, reach=MINING_REACH)
                    )
                    or not route_skip
                ):
                    raise
                excluded.add(tuple(pos))
                route_failures += 1
                yield rec("ore_route_skip", pos=pos, error_code=code)
                state.refresh_inventory(ex.client)
                continue
            if state.count("raw_iron") >= self.args["count"]:
                return

    def _prepare_fuel(self, ex: "Executor", state: "State", count: int) -> Iterator[dict]:
        fuels = _fuels_for(state)
        # 1 原木合成 4 木板可将可熔炼件数由 1 件提升至 6 件；需熔 ≥3 件或直接燃料不足时优先拆板。
        for _ in range(count):
            log_wood = _log_for_fuel(state.count, fuels, count)
            if log_wood is None:
                break
            plank = f"{log_wood}_planks"
            yield from craft_recipe(ex, state, plank, goal_qty=state.count(plank) + 1)

        # 若背包燃料仍不足（如起步造完工具仅剩 1 木板）且附近地表有树，顺手补砍 1 块原木转 4 木板。
        if _fuel_batches(state.count, fuels, count)[1]:
            log_types = [f"{w}_log" for w in WOODS]
            logs = ex.client.find_blocks(types=log_types, radius=24, limit=8).get("blocks") or []
            status = state.refresh_status(ex.client, max_age_s=0)
            cur = status.get("pos") or state.pos
            stance = status if status.get("pos") is not None else cur
            for b in logs:
                lpos = b.get("pos")
                if not (isinstance(lpos, list) and len(lpos) == 3) or under_feet(cur, lpos):
                    continue
                if abs(lpos[1] - cur[1]) > 6.0:
                    continue
                try:
                    if not can_reach_block(ex, stance, lpos, reach=MINING_REACH):
                        yield rec("move_to", ex.move_to(pos=lpos, rng=3), pos=lpos)
                    status = state.refresh_status(ex.client, max_age_s=0)
                    cur = status.get("pos") or state.pos
                    stance = status if status.get("pos") is not None else cur
                    if (under_feet(cur, lpos)
                            or not can_reach_block(ex, stance, lpos, reach=API_DIG_EYE_REACH)
                            or bare(ex.client.get_block(lpos).get("name")) not in log_types):
                        continue
                    before_logs = sum(state.count(x) for x in log_types)
                    yield rec("dig", ex.dig(lpos, collect=True), pos=lpos)
                    state.refresh_inventory(ex.client)
                    if sum(state.count(x) for x in log_types) > before_logs:
                        w = next((w for w in WOODS if state.count(f"{w}_log") >= 1), None)
                        if w is not None:
                            plank = f"{w}_planks"
                            yield from craft_recipe(ex, state, plank, goal_qty=state.count(plank) + 1)
                        break
                except Replan as err:
                    status = state.refresh_status(ex.client, max_age_s=0)
                    cur = status.get("pos") or state.pos
                    stance = status if status.get("pos") is not None else cur
                    code, _ = failure(err)
                    if code in (
                        "path_not_found", "unreachable", "move_timeout", "stalled",
                        "out_of_reach", "dig_unconfirmed", "protected_region",
                    ):
                        yield rec("fuel_wood_skip", pos=lpos, error_code=code)
                        continue
                    raise

    def _fuel_plan(self, state: "State", count: int) -> list[tuple[str, int, int]]:
        plan, remaining = _fuel_batches(state.count, _fuels_for(state), count)
        if not remaining:
            return plan
        raise Replan(
            f"检查背包煤/木板/原木后燃料不足：需冶炼 {count} 个，"
            f"缺 {remaining} 个冶炼单位（至少 {math.ceil(remaining / 1.5)} 块木板）"
        )

    def _furnace(self, ex: "Executor", state: "State") -> Iterator[dict]:
        found = ex.client.find_blocks(types=["furnace"], radius=16, limit=4).get("blocks") or []
        for b in found:
            pos = b.get("pos")
            if not isinstance(pos, list) or len(pos) != 3:
                continue
            if bare(ex.client.get_block(pos).get("name")) != "furnace":
                continue
            yield rec("move_to", ex.move_to(pos=pos, rng=2), pos=pos)
            return pos
        if not state.count("furnace"):
            raise Replan("背包缺熔炉，find_blocks 半径 16 内也未观察到已有熔炉")
        state.refresh_status(ex.client, max_age_s=0)
        x, y, z = map(math.floor, state.pos)
        valid_supports = SOLID_SUPPORT | {f"{w}_planks" for w in WOODS}
        # 先查平层与高一格台阶（适配 1x3 下行阶梯内转身往上一级台阶放炉），无需额外开凿。
        for dy in (0, 1):
            py = y + dy
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                pos = [x + dx, py, z + dz]
                support = bare(ex.client.get_block([pos[0], py - 1, pos[2]]).get("name"))
                if support not in valid_supports:
                    continue
                if bare(ex.client.get_block(pos).get("name")) not in AIR:
                    continue
                if bare(ex.client.get_block([pos[0], py + 1, pos[2]]).get("name")) not in AIR:
                    continue
                yield rec("place", ex.place("furnace", pos=pos), item="furnace", pos=pos)
                if bare(ex.client.get_block(pos).get("name")) != "furnace":
                    raise Replan(f"放置后 get_block 在 {pos} 未确认熔炉")
                return pos
        # 地下狭窄矿井兜底：若四向皆为安全实心岩壁且有石级镐，侧向凿出 1x2 壁龛放炉。
        if best_pickaxe(state) in STONE_OR_BETTER_PICKS:
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                pos = [x + dx, y, z + dz]
                if bare(ex.client.get_block([pos[0], y - 1, pos[2]]).get("name")) not in SOLID_SUPPORT:
                    continue
                if bare(ex.client.get_block([pos[0], y + 2, pos[2]]).get("name")) not in SOLID_SUPPORT:
                    continue
                if not all(bare(ex.client.get_block([pos[0], y + k, pos[2]]).get("name")) in AIR | SOLID_SUPPORT
                           for k in (0, 1)):
                    continue
                for k in (1, 0):
                    cell = [pos[0], y + k, pos[2]]
                    if bare(ex.client.get_block(cell).get("name")) not in AIR:
                        yield from prepare_mining_pick(ex, state)
                        yield rec("dig", ex.dig(cell, collect=True), pos=cell)
                if (bare(ex.client.get_block([pos[0], y - 1, pos[2]]).get("name")) in SOLID_SUPPORT
                        and all(bare(ex.client.get_block([pos[0], y + k, pos[2]]).get("name")) in AIR for k in (0, 1))):
                    yield rec("place", ex.place("furnace", pos=pos), item="furnace", pos=pos)
                    if bare(ex.client.get_block(pos).get("name")) != "furnace":
                        raise Replan(f"放置后 get_block 在 {pos} 未确认熔炉")
                    return pos
        raise Replan("观察脚边四个方向的落点/净空/底板后无安全熔炉位置，环境缺安全承重落点")

    def _poll_and_take(self, ex: "Executor", state: "State", pos, target: dict, batch: int) -> Iterator[dict]:
        deadline = time.monotonic() + 10 * batch + 20
        view = {}
        for poll in range(5 * batch + 11):
            if poll:
                time.sleep(POLL_SECONDS)
            view = yield from inspect_furnace(ex, target)
            if not window_section(view, "input") and window_amount(view, "output", "iron_ingot") >= batch:
                break
            if time.monotonic() >= deadline:
                break
        else:
            raise Replan(f"观察熔炉 {pos} 超过轮询上限，冶炼 {batch} 个铁超时：{view}")
        if window_section(view, "input") or window_amount(view, "output", "iron_ingot") < batch:
            raise Replan(f"观察熔炉 {pos} 等待 {10 * batch + 20}s 后铁未烧完，检查燃料/原料：{view}")
        result = yield from collect_furnace(
            ex, state, target, item="iron_ingot", view=view, require_empty=True,
        )
        return result["view"]

    def _recover_placed_furnace(self, ex: "Executor", state: "State") -> Iterator[dict]:
        """抢占或重连后若粗铁与熔炉均已离手，先检查附近已放熔炉回收在途铁锭，避免重复下矿。"""
        found = ex.client.find_blocks(types=["furnace"], radius=16, limit=4).get("blocks") or []
        for b in found:
            pos = b.get("pos")
            if not (isinstance(pos, list) and len(pos) == 3):
                continue
            if bare(ex.client.get_block(pos).get("name")) != "furnace":
                continue
            yield rec("move_to", ex.move_to(pos=pos, rng=2), pos=pos)
            target = {"pos": pos}
            view = yield from inspect_furnace(ex, target)
            in_raw = window_amount(view, "input", "raw_iron")
            out_ingot = window_amount(view, "output", "iron_ingot")
            if in_raw > 0:
                if not (view.get("info") or {}).get("burning") and not window_section(view, "fuel"):
                    yield from self._prepare_fuel(ex, state, in_raw)
                    fuel, fuel_count, _ = self._fuel_plan(state, in_raw)[0]
                    yield from load_furnace(ex, state, target, fuel_item=fuel, fuel_count=fuel_count, view=view)
                yield from self._poll_and_take(ex, state, pos, target, in_raw + out_ingot)
                return
            if out_ingot > 0 and not window_section(view, "input"):
                yield from collect_furnace(ex, state, target, item="iron_ingot",
                                           view=view, inspect_after=False)
                return

    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        state.invalidate_inventory()
        state.refresh_inventory(ex.client)
        start_ingots = state.count("iron_ingot")
        if state.count("raw_iron") < 1 and state.count("raw_iron_block") < 1 and state.count("furnace") < 1:
            yield from self._recover_placed_furnace(ex, state)
            if state.count("iron_ingot") - start_ingots >= self.args["count"]:
                return
        reason = self.blocked(state)
        if reason:
            if state.count("iron_ingot") > start_ingots:
                return
            raise Replan(f"检查背包后：{reason}")
        remaining_goal = max(1, self.args["count"] - (state.count("iron_ingot") - start_ingots))
        yield from self._collect(ex, state)
        count = min(remaining_goal, state.count("raw_iron"))
        # 有镐才顺路采煤，最多两块；没有则转用背包木材，不等待环境刷新。
        if (state.count("coal") + state.count("charcoal")) * 8 < count and best_pickaxe(state) in STONE_OR_BETTER_PICKS:
            for _ in range(2):
                coal = ex.client.find_blocks(types=["coal_ore", "deepslate_coal_ore"], radius=8, limit=4).get("blocks") or []
                cur = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
                nearby = [b["pos"] for b in coal if isinstance(b.get("pos"), list)
                          and len(b["pos"]) == 3 and not under_feet(cur, b["pos"])]
                if not nearby:
                    break
                yield from self._mine(ex, state, nearby[0], ("coal_ore", "deepslate_coal_ore"), "coal")
                if (state.count("coal") + state.count("charcoal")) * 8 >= count:
                    break
        yield from self._prepare_fuel(ex, state, count)
        plan = self._fuel_plan(state, count)
        pos = yield from self._furnace(ex, state)
        target = {"pos": pos}
        view = yield from inspect_furnace(ex, target)
        # 每次只放当前批次原料，避免剩余燃烧时间提前吃掉下一批材料。
        for fuel, fuel_count, batch in plan:
            yield from load_furnace(
                ex, state, target, raw_item="raw_iron", raw_count=batch, fuel_item=fuel,
                fuel_count=fuel_count, view=view, replace_fuel=True, output_item="iron_ingot",
            )
            view = yield from self._poll_and_take(ex, state, pos, target, batch)

    def verify_done(self, state: "State", before_counts: dict[str, int]) -> str:
        if state.count("iron_ingot") <= before_counts.get("iron_ingot", 0):
            return "smelt_iron 结束但背包铁锭未增加"
        return ""
