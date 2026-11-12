"""Skill 基类。所有具体技能继承它，遵守"一次 step 一个动作"。"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只用于类型提示，避免循环导入
    from ..runtime import Executor, State


class Skill:
    #: 唯一标识，用于注册和日志
    name: str = "skill"
    #: 目标 id，形如 "acq:oak_log" / "tool:stone_pickaxe"
    goal: str = "goal:unnamed"
    #: 同一个目标失败后，多久内不再重试（秒）
    cooldown_s: float = 30.0

    def can_start(self, state: "State") -> bool:
        """当前状态是否允许开始。默认允许。"""
        return True

    def step(self, ex: "Executor", state: "State") -> bool:
        """推进一小步。**最多执行一个动作**。返回 True 表示目标完成。"""
        raise NotImplementedError

    # ---- 失败冷却（循环用它避免反复撞同一堵墙）
    def _fail_key(self) -> str:
        return self.goal

    def is_cooling(self) -> bool:
        until = _COOLDOWN.get(self._fail_key(), 0.0)
        return time.time() < until

    def mark_failed(self) -> None:
        _COOLDOWN[self._fail_key()] = time.time() + self.cooldown_s

    def __str__(self) -> str:
        return f"<{self.name}:{self.goal}>"


_COOLDOWN: dict[str, float] = {}
