"""三个最小技能。**状态：未在真机验证**（编译通过，逻辑按规范写的）。

它们是 `loop.py` 里那段脚本化流程的"版本化"形态：拆成一次一个动作，
中间可以被 Safety 抢占。等 `probe` 的量测数据回来，再把参数（距离阈值、半径）调准。

照这个写法加技能，不要把逻辑堆进 `loop.py`。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Skill

if TYPE_CHECKING:
    from ..runtime import Executor, State

LOG_TYPES = ("oak_log", "birch_log", "spruce_log", "jungle_log", "acacia_log", "dark_oak_log")


class ChopLogs(Skill):
    name = "chop_logs"
    goal = "acq:log"

    def __init__(self, count: int = 4, radius: int = 32):
        self.count = count
        self.radius = radius
        self.target: dict | None = None

    def can_start(self, state: "State") -> bool:
        return True

    def step(self, ex: "Executor", state: "State") -> bool:
        # 观察不占动作锁，可以随便调
        if not state.inventory:
            state.set_inventory(ex.client.inventory())
        have = sum(state.count(t) for t in LOG_TYPES)
        if have >= self.count:
            return True

        if self.target is None:
            r = ex.client.find_blocks(types=list(LOG_TYPES), radius=self.radius, limit=5)
            blocks = r.get("blocks") or []
            if not isinstance(blocks, list) or not blocks:
                raise _replan(f"{self.radius} 格内没有原木")
            b = blocks[0]
            self.target = {"pos": b["pos"], "name": b["name"]}

        pos = self.target["pos"]
        dist = _dist(state.pos, pos)
        if dist > 3.0:
            ex.move_to(pos[0], pos[2], rng=2)      # 动作 1
            return False
        ex.dig(pos)                                 # 动作 2（不同一次 step）
        self.target = None
        state.set_inventory(ex.client.inventory())
        return False


class MakeTable(Skill):
    name = "make_table"
    goal = "tool:crafting_table"

    def step(self, ex: "Executor", state: "State") -> bool:
        if not state.inventory:
            state.set_inventory(ex.client.inventory())
        if state.count("crafting_table") == 0:
            ex.craft("crafting_table", 1)            # 动作 1（2x2，不需要工作台）
            return False
        ex.place("crafting_table")                   # 动作 2
        return True


class CraftItem(Skill):
    """通用合成：需要先有工作台时，由调用方保证场上有工作台。"""

    name = "craft_item"
    goal = "craft:item"

    def __init__(self, item: str, count: int = 1, need_table: bool = False):
        self.item = item
        self.count = count
        self.need_table = need_table

    def can_start(self, state: "State") -> bool:
        return True

    def step(self, ex: "Executor", state: "State") -> bool:
        ex.craft(self.item, self.count)               # 动作 1
        state.set_inventory(ex.client.inventory())
        return state.count(self.item) > 0


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _replan(msg: str) -> Exception:
    from ..runtime import Replan
    return Replan(msg)
