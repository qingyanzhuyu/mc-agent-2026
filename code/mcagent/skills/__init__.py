"""skills —— 执行层（目前只有基类和注册表，没有具体技能）。

硬约束（架构性的，不是风格问题）：

    step() 每次最多做一个动作，然后把状态存在 self 里。

原因：Safety 闸必须在**每个动作之间**都有机会抢占。如果 `step()` 里连做 20 个动作，
中间血量掉到 2 也不会有人知道——而 60 秒死 3 次就会下线 5 分钟。

写新 skill：

    from .base import Skill

    class MySkill(Skill):
        name = "my_skill"
        goal = "goal:my_skill"

        def can_start(self, state) -> bool:
            return True

        def step(self, ex, state) -> bool:
            ...          # 最多一个 ex.xxx() 动作
            return True  # True = 目标完成

规则：
- 观察（`find_blocks` / `inventory` / `events`）不占动作锁，可以在 step 里随便调。
- 动作失败抛 `runtime.Replan`，循环换下一个技能；抛 `runtime.Fatal` 就整个停。
- 不要在这里写 HTTP，只用 `Executor`。
- **没在真机跑通的技能不要合进 main**（`AGENTS.md` §2 第 6 条）。
"""
from .base import Skill

#: name -> Skill 子类。加技能时在这里登记，并在真机验证过之后再合入。
REGISTRY: dict[str, type[Skill]] = {}


def register(cls: type[Skill]) -> type[Skill]:
    REGISTRY[cls.name] = cls
    return cls


def get(name: str) -> type[Skill] | None:
    return REGISTRY.get(name)


def all_names() -> list[str]:
    return sorted(REGISTRY)


__all__ = ["Skill", "REGISTRY", "register", "get", "all_names"]
