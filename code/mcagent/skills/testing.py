"""技能的离线夹具：一个方块世界替身 + 强制"一步一动作"的驱动器。

    world = GridWorld(blocks={(1, 64, 0): "stone"}, items={"wooden_pickaxe": 1})
    ex, state = world.session()
    steps = drive(nav.breach_1x2(ex, state, [[1, 64, 0]]), ex)

**它只验证控制流和契约**（发了哪些动作、顺序对不对、一步是否只发一个）。这里没有重力、
没有挖掘时间、没有寻路失败——过了这里的测试是 `verified: offline`，不是 `verified: live`。
替身只模拟两条已有依据的拒绝：背包没有该物品（`missing_item`）、往自己身体里放方块
（`blocked_by_self`，真机已证实）。别往里加猜出来的服务端行为。
"""
from __future__ import annotations

import math
from typing import Any, Iterator, Optional, Union

from ..client import ApiError
from ..runtime import Executor, State
from .base import Skill


class GridWorld:
    """稀疏方块表 + 背包 + 坐标，接口形状与 `client.Client` 一致。"""

    def __init__(self, pos=(0.5, 64.0, 0.5), blocks: Optional[dict] = None, items: Optional[dict] = None):
        self.st: dict = {"ready": True, "alive": True, "health": 20, "food": 20,
                         "pos": [float(v) for v in pos], "on_ground": True, "sneaking": False}
        self.blocks: dict[tuple, str] = {tuple(k): v for k, v in (blocks or {}).items()}
        self.items: dict[str, int] = dict(items or {})
        #: 每个发到"服务端"的调用：(方法名, 参数字典)
        self.calls: list[tuple[str, dict]] = []

    def session(self) -> tuple["CountingExecutor", State]:
        """配好的 (执行器, 状态)，状态和背包已读过一次。"""
        state = State()
        state.set_status(self.status())
        state.set_inventory(self.inventory())
        return CountingExecutor(self, state), state

    # ---- 观察
    def status(self, timeout: float = 10) -> dict:
        return {**self.st, "pos": list(self.st["pos"])}

    def inventory(self, timeout: float = 10) -> dict:
        return {"items": [{"name": n, "count": c, "slot": i}
                          for i, (n, c) in enumerate(self.items.items()) if c > 0]}

    def events(self, since: int = 0, limit: int = 100) -> dict:
        return {"events": [], "last_seq": since}

    def get_block(self, pos) -> dict:
        return {"name": self.blocks.get(tuple(pos), "air")}

    # ---- 动作
    def _need(self, item: str) -> None:
        if self.items.get(item, 0) < 1:
            raise ApiError("missing_item", f"背包没有 {item}")

    def equip(self, item, slot="hand", *, stop_on=None) -> dict:
        self.calls.append(("equip", {"item": item, "slot": slot}))
        self._need(item)
        return {"equipped": item, "slot": slot}

    def dig(self, pos, collect=True, *, stop_on=None) -> dict:
        self.calls.append(("dig", {"pos": list(pos)}))
        dug = self.blocks.pop(tuple(pos), "air")
        if dug != "air" and collect:
            self.items[dug] = self.items.get(dug, 0) + 1
        return {"dug": dug}

    def place(self, item, pos=None, *, stop_on=None) -> dict:
        self.calls.append(("place", {"item": item, "pos": list(pos)}))
        self._need(item)
        x, y, z = self.st["pos"]
        if list(pos) in ([math.floor(x), math.floor(y) + dy, math.floor(z)] for dy in (0, 1)):
            raise ApiError("blocked_by_self", "目标格与自身碰撞箱重叠")
        self.items[item] -= 1
        self.blocks[tuple(pos)] = item
        return {"placed": item, "pos": list(pos)}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        self.calls.append(("move_to", {"x": x, "z": z, "y": y, "pos": pos}))
        dest = [float(v) for v in pos] if pos is not None else [
            float(x), float(self.st["pos"][1] if y is None else y), float(z)]
        self.st["pos"] = dest
        return {"pos": list(dest)}

    def posture(self, sneak=None, shield=None) -> dict:
        self.calls.append(("posture", {"sneak": sneak, "shield": shield}))
        self.st["sneaking"] = bool(sneak)
        return {"sneaking": bool(sneak), "blocking": bool(shield)}

    def stop(self) -> dict:
        self.calls.append(("stop", {}))
        return {}


class CountingExecutor(Executor):
    """真 Executor，只多记一笔：每次进入 `act()`（即每个阻塞动作）的名字。"""

    def __init__(self, client: Any, state: State):
        super().__init__(client, state, log=lambda *_: None)
        self.acts: list[str] = []

    def act(self, name, fn, *args, **kwargs):
        self.acts.append(name)
        return super().act(name, fn, *args, **kwargs)


def drive(flow: Union[Skill, Iterator], ex: CountingExecutor, state: Optional[State] = None,
          max_steps: int = 200) -> list[Optional[dict]]:
    """把技能或原语跑到结束，返回每一步的记录。一步内发了两个阻塞动作就断言失败。"""
    steps: list[Optional[dict]] = []
    for i in range(max_steps):
        before = len(ex.acts)
        if isinstance(flow, Skill):
            done, record = flow.step(ex, state), flow.last
        else:
            try:
                done, record = False, next(flow)
            except StopIteration:
                done, record = True, None
        fired = ex.acts[before:]
        if len(fired) > 1:
            raise AssertionError(f"第 {i + 1} 步发了 {len(fired)} 个阻塞动作 {fired}：每个动作后都要 yield")
        if done:
            return steps
        steps.append(record)
    raise AssertionError(f"{max_steps} 步还没结束：技能可能在空转")
