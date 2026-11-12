# skills —— 执行层

**职责**：一个目标 → 一串 `Executor` 动作。**不写 HTTP，不写决策**。

```
base.py       Skill 基类：name / goal / can_start / step / 失败冷却（已验：编译 + 导入）
__init__.py   空注册表 + register()/get()
```

## 唯一的架构约束

> `step()` 每次**最多一个动作**，中间状态存 `self`。

这不是代码风格，是因为 Safety 闸只能在动作之间抢占。反例：

```python
# 错：一次 step 连做 20 个动作，血量掉到 2 没人知道
def step(self, ex, state):
    for b in blocks:
        ex.move_to(...); ex.dig(...)
```

正确写法是让每个动作都能被循环观察一次：

```python
def step(self, ex, state):
    if self.target is None:
        self.target = 观察()
        return False          # 这一步没有动作
    if 远:
        ex.move_to(...)       # 一个动作
        return False
    ex.dig(...)               # 下一个 step 才挖
    self.target = None
    return False
```

## 加一个技能

```python
from .base import Skill

class GetStone(Skill):
    name = "get_stone"
    goal = "acq:cobblestone"

    def can_start(self, state):
        return state.count("wooden_pickaxe") > 0

    def step(self, ex, state):
        ...
        return True
```

然后在 `__init__.py` 里 `register(GetStone)`。

## 失败处理

- 前置条件不成立（找不到目标、缺材料）→ 抛 `runtime.Replan`，循环换下一个技能
- 认证/接口级错误 → 抛 `runtime.Fatal`，整个停
- 同一目标反复失败 → 基类 `mark_failed()` / `is_cooling()` 自动冷却 30 秒

## 现在**没有**具体技能（故意的）

写技能必然要依赖真实世界的反馈：`dist` 阈值多少合适、`craft` 的 `count` 是次数还是个个数、
`place` 不给坐标会不会放到脚下。这些**只有真机跑过才知道**，所以先空着。

第一次验证的顺序见 `docs/04` 的 C 组。
