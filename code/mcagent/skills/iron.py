"""铁矿采集与熔炼 L3；窗口协议见 agent-api-spec-dist §4.9。

只依赖当场暴露面观察；燃烧耗时仅作有界等待预算，不当作成功判据。
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Iterator

from ..runtime import Replan
from .base import Param, Skill, rec
from .items import AIR, bare, best_pickaxe
from .registry import register

if TYPE_CHECKING:
    from ..runtime import Executor, State

IRON_ORES = ("iron_ore", "deepslate_iron_ore")
PICKS = frozenset(("stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe"))
WOODS = ("oak", "birch", "spruce", "jungle", "acacia", "dark_oak", "cherry", "mangrove")
# 半个冶炼单位：煤 8 个，木板/原木 1.5 个；不把下界木材当燃料。
FUELS = (("coal", 16),) + tuple((f"{w}_{suffix}", 3)
                                for suffix in ("planks", "log") for w in WOODS)
RADIUS = 32
POLL_SECONDS = 2.0
SOLID_SUPPORT = frozenset(("stone", "cobblestone", "deepslate", "cobbled_deepslate",
                           "dirt", "grass_block", "andesite", "diorite", "granite"))


def _under_feet(cur, pos) -> bool:
    return (math.floor(cur[0]) == pos[0] and math.floor(cur[2]) == pos[2]
            and pos[1] < cur[1])


def _section(view: dict, section: str) -> list[dict]:
    return (view.get("sections") or {}).get(section) or []


def _amount(view: dict, section: str, item: str) -> int:
    return sum(int(s.get("count", 0)) for s in _section(view, section)
               if bare(s.get("name")) == item)


@register
class SmeltIron(Skill):
    name = "smelt_iron"
    description = "采集暴露铁矿，安全放置或复用熔炉并有界等待取出铁锭"
    params = {"count": Param(int, "期望新增铁锭数（粗铁用完即止）", default=3, min=1, max=16)}
    goal = "acq:iron_ingot"
    verified = "offline"

    @classmethod
    def blocked(cls, state: "State") -> str:
        if state.count("raw_iron") < 1 and best_pickaxe(state) not in PICKS:
            return "背包无粗铁且缺石镐或更好的镐（木镐、金镐不能采铁）"
        if not any(state.count(n) for n, _ in FUELS) and best_pickaxe(state) not in PICKS:
            return "背包缺煤、木板或原木燃料，且无可采煤的镐"
        return ""

    def _mine(self, ex: "Executor", state: "State", pos, ores, drop: str) -> Iterator[dict]:
        cur = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
        if math.dist(cur, pos) > 4:
            yield rec("move_to", ex.move_to(pos=pos, rng=2), pos=pos)
        cur = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
        if _under_feet(cur, pos):
            raise Replan(f"观察到 {drop} 矿 {pos} 已在脚下，停止挖掘以保留支撑")
        if bare(ex.client.get_block(pos).get("name")) not in ores:
            raise Replan(f"到达 {pos} 后 get_block 未确认目标矿 {ores}")
        inv = state.refresh_inventory(ex.client)
        pick = best_pickaxe(state)
        if pick not in PICKS:
            raise Replan(f"在矿 {pos} 前检查背包缺石镐或更好的镐")
        if bare((inv.get("held", state.st.get("held")) or {}).get("name")) != pick:
            yield rec("equip", ex.equip(pick, "hand"), item=pick)
            inv = state.refresh_inventory(ex.client)
            if best_pickaxe(state) not in PICKS or ("held" in inv and
                    bare((inv.get("held") or {}).get("name")) != pick):
                raise Replan(f"在矿 {pos} 前装备 {pick} 后手持未确认")
        cur = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
        if _under_feet(cur, pos):
            raise Replan(f"装备后重新观察 {pos} 已在脚下，停止挖掘以保留支撑")
        before = state.count(drop)
        result = ex.dig(pos, collect=True)
        yield rec("dig", result, pos=pos)
        state.refresh_inventory(ex.client)
        if result.get("warning") or state.count(drop) <= before:
            raise Replan(f"挖掘 {pos} 后背包 {drop} 未确认增加或工具告警：{result.get('warning', '')}")

    def _collect(self, ex: "Executor", state: "State") -> Iterator[dict]:
        if state.count("raw_iron"):
            return
        for _ in range(self.args["count"]):
            blocks = ex.client.find_blocks(types=list(IRON_ORES), radius=RADIUS, limit=16).get("blocks") or []
            cur = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
            candidates = [b["pos"] for b in blocks if isinstance(b.get("pos"), list)
                          and len(b["pos"]) == 3 and not _under_feet(cur, b["pos"])]
            if not candidates:
                if state.count("raw_iron"):
                    return
                raise Replan(f"半径 {RADIUS} 内无暴露铁矿；find_blocks 已搜索 iron_ore/deepslate_iron_ore，环境缺铁矿")
            yield from self._mine(ex, state, candidates[0], IRON_ORES, "raw_iron")
            if state.count("raw_iron") >= self.args["count"]:
                return

    def _fuel_plan(self, state: "State", count: int) -> list[tuple[str, int, int]]:
        plan = []
        remaining = count
        for item, units in FUELS:
            available = state.count(item)
            batch = min(remaining, available * units // 2)
            if batch:
                plan.append((item, math.ceil(batch * 2 / units), batch))
                remaining -= batch
            if not remaining:
                return plan
        raise Replan(f"检查背包煤/木板/原木后燃料不足：需冶炼 {count} 个，缺 {remaining} 个冶炼单位（至少 {math.ceil(remaining / 1.5)} 块木板）")

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
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            pos = [x + dx, y, z + dz]
            support = bare(ex.client.get_block([pos[0], y - 1, pos[2]]).get("name"))
            if support not in SOLID_SUPPORT and support not in {f"{w}_planks" for w in WOODS}:
                continue
            if bare(ex.client.get_block(pos).get("name")) not in AIR:
                continue
            if bare(ex.client.get_block([pos[0], y + 1, pos[2]]).get("name")) not in AIR:
                continue
            yield rec("place", ex.place("furnace", pos=pos), item="furnace", pos=pos)
            if bare(ex.client.get_block(pos).get("name")) != "furnace":
                raise Replan(f"放置后 get_block 在 {pos} 未确认熔炉")
            return pos
        raise Replan("观察脚边四个方向的落点/净空/底板后无安全熔炉位置，环境缺安全承重落点")

    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        state.refresh_inventory(ex.client)
        reason = self.blocked(state)
        if reason:
            raise Replan(f"检查背包后：{reason}")
        yield from self._collect(ex, state)
        count = min(self.args["count"], state.count("raw_iron"))
        # 有镐才顺路采煤，最多两块；没有则转用背包木材，不等待环境刷新。
        if state.count("coal") * 8 < count and best_pickaxe(state) in PICKS:
            for _ in range(2):
                coal = ex.client.find_blocks(types=["coal_ore", "deepslate_coal_ore"], radius=8, limit=4).get("blocks") or []
                cur = state.refresh_status(ex.client, max_age_s=0).get("pos") or state.pos
                nearby = [b["pos"] for b in coal if isinstance(b.get("pos"), list)
                          and len(b["pos"]) == 3 and not _under_feet(cur, b["pos"])]
                if not nearby:
                    break
                yield from self._mine(ex, state, nearby[0], ("coal_ore", "deepslate_coal_ore"), "coal")
                if state.count("coal") * 8 >= count:
                    break
        plan = self._fuel_plan(state, count)
        pos = yield from self._furnace(ex, state)
        target = {"pos": pos}
        view = ex.window_view(target)
        yield rec("window_view", view, target=target)
        if _section(view, "input"):
            raise Replan(f"观察熔炉 {pos} input 仍有原料，先处理已有冶炼任务后重规划")
        if any(bare(s.get("name")) != "iron_ingot" for s in _section(view, "output")):
            raise Replan(f"观察熔炉 {pos} output 被非铁产物占用，停止放料")
        # 窗口 API 每次自动关闭（§4.9）；不进入持续潜行/举盾/拉弓状态。
        for fuel, fuel_count, batch in plan:
            if _section(view, "fuel"):
                yield rec("window_act", ex.window_act(target, [{"op": "take", "from": "fuel"}]), target=target)
            # 每次只放当前批次原料，避免剩余燃烧时间提前吃掉下一批材料。
            yield rec("window_act", ex.window_act(target, [{"op": "put", "item": "raw_iron", "count": batch, "to": "input"}]), target=target)
            yield rec("window_act", ex.window_act(target, [{"op": "put", "item": fuel, "count": fuel_count, "to": "fuel"}]), target=target)
            deadline = time.monotonic() + 10 * batch + 20
            for poll in range(5 * batch + 11):
                if poll:
                    time.sleep(POLL_SECONDS)
                view = ex.window_view(target)
                yield rec("window_view", view, target=target)
                if not _section(view, "input") and _amount(view, "output", "iron_ingot") >= batch:
                    break
                if time.monotonic() >= deadline:
                    break
            else:
                raise Replan(f"观察熔炉 {pos} 超过轮询上限，冶炼 {batch} 个铁超时：{view}")
            if _section(view, "input") or _amount(view, "output", "iron_ingot") < batch:
                raise Replan(f"观察熔炉 {pos} 等待 {10 * batch + 20}s 后铁未烧完，检查燃料/原料：{view}")
            amount = _amount(view, "output", "iron_ingot")
            state.refresh_inventory(ex.client)
            before_count = state.count("iron_ingot")
            yield rec("window_act", ex.window_act(target, [{"op": "take", "from": "output", "item": "iron_ingot", "count": amount}]), target=target)
            state.refresh_inventory(ex.client)
            if state.count("iron_ingot") < before_count + amount:
                raise Replan(f"熔炉 {pos} take output 后背包铁锭未确认增加 {amount} 个")
            view = ex.window_view(target)
            yield rec("window_view", view, target=target)
            if _section(view, "output"):
                raise Replan(f"熔炉 {pos} 取出后 output 仍有剩余产物：{view}")

    def verify_done(self, state: "State", before_counts: dict[str, int]) -> str:
        if state.count("iron_ingot") <= before_counts.get("iron_ingot", 0):
            return "smelt_iron 结束但背包铁锭未增加"
        return ""
