"""skills —— 一个目标 = 一串动作，自带中间状态。

硬约束（架构性的，不是风格问题）：

    step() 每次最多做一个动作，然后把状态存在 self 里。

原因：Safety 闸必须在**每个动作之间**都有机会抢占。如果 `step()` 里连做 20 个动作，
中间血量掉到 2 也不会有人知道。所以"走路 + 挖"必须拆成两次 `step()`：

    step 1: 距离远 → move_to(...) → return False
    step 2: 到了   → dig(...)     → return True

写新 skill：

    from .base import Skill

    class MySkill(Skill):
        name = "my_skill"
        goal = "goal:my_skill"

        def can_start(self, state) -> bool:
            return True          # 什么条件下允许开始（看背包/维度/血量）

        def step(self, ex, state) -> bool:
            ...                  # 最多一个 ex.xxx() 动作
            return True          # True = 这个目标完成了

规则：
- 观察（`find_blocks` / `inventory` / `events`）不占动作锁，可以在 step 里随便调。
- 动作失败抛 `runtime.Replan`，循环会挑下一个 skill；抛 `runtime.Fatal` 就整个停。
- 不要在这里写 HTTP，只用 `Executor`。
"""
from .base import Skill
from .basic import ChopLogs, CraftItem, MakeTable

REGISTRY: dict[str, type[Skill]] = {
    s.name: s for s in (ChopLogs, CraftItem, MakeTable)
}


def get(name: str) -> type[Skill] | None:
    return REGISTRY.get(name)


def all_names() -> list[str]:
    return sorted(REGISTRY)


__all__ = ["Skill", "ChopLogs", "CraftItem", "MakeTable", "REGISTRY", "get", "all_names"]
