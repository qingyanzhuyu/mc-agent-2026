# skills —— 执行层

**职责**：一个目标 → 一串 `Executor` 动作。**不写 HTTP，不写决策**。
规则的权威版本在仓库根 `AGENTS.md` §9；这里只说怎么上手。

```
base.py       Skill 基类 / Param 参数声明 / rec() / failure()
registry.py   @register、REGISTRY、make()、契约检查、自动发现
items.py      只读选择器：最好的镐、垫块、空气判断
nav.py        L2 战术原语：breach_1x2 / sneak_bridge / step_up_1x2
testing.py    离线夹具：GridWorld 替身 + drive()
<领域>.py      L3 意图技能（还没有，见最后一节）
```

## 两层

| | L3 意图技能 | L2 战术原语 |
|---|---|---|
| 形式 | `Skill` 子类 + `@register` | 普通生成器函数 |
| 谁调 | 模型、规则队列 | 只有 L3 和 `probe_path.py` |
| 参数 | 物品名、数量这类标量 | 当场观察到的方块坐标 |
| 模型能选到吗 | 能 | 不能 |

## 唯一的架构约束

> 每发一个阻塞动作就 `yield` 一次。

Safety 闸只能在动作之间抢占。反例：

```python
# 错：一步连做 20 个动作，血量掉到 2 没人知道
def run(self, ex, state):
    for b in blocks:
        ex.move_to(pos=b); ex.dig(b)
    yield
```

正确写法——代码照样是从上往下读的一条线，只是每个动作后停一下：

```python
def run(self, ex, state):
    for b in blocks:                                  # 观察随便调，不用 yield
        yield rec("move_to", ex.move_to(pos=b, rng=3), pos=b)
        yield rec("dig", ex.dig(b), pos=b)
```

基类的 `step()` 每次只推进到下一个 `yield`，所以对主循环来说它仍然是"一步一动作"的状态机。
要用 L2 原语就 `yield from nav.breach_1x2(ex, state, [...])`。

## 加一个技能

完整模板和四步流程见 `AGENTS.md` §9.4。要点：

- 类属性声明 `name` / `description` / `params` / `goal`，加 `@register`——**不用改任何名单**，菜单和校验自动跟上。
- `blocked(state)` 返回空串表示可以进菜单，否则返回一句原因（模型会看到）。
- 参数只许 `str/int/float/bool`，不许叫 `pos/x/y/z/entity_id`——注册时会直接报错。
- 要收尾的状态（潜行、举盾）写在 `finally` 里，且从进入该状态起就在 `try` 里面。

## 离线测试

```python
from mcagent.skills import nav
from mcagent.skills.testing import GridWorld, drive

world = GridWorld(blocks={(1, 64, 0): "stone"}, items={"wooden_pickaxe": 1})
ex, state = world.session()
steps = drive(nav.breach_1x2(ex, state, [[1, 64, 0]]), ex)   # 一步发两个动作会断言失败
assert [s["op"] for s in steps] == ["equip", "dig"]
```

`GridWorld` 只验证控制流：没有重力、没有挖掘时间、没有寻路失败。过了这里是 `verified: offline`，
离 `verified: live` 还差一次真机。

## 失败处理

- 前置条件不成立（找不到目标、缺材料）→ 抛 `runtime.Replan`，文案写清楚缺什么——它会作为"最近失败原因"回喂给模型
- 认证/接口级错误 → `Executor` 会抛 `runtime.Fatal`，整个停
- 同一目标反复失败 → 基类 `mark_failed()` / `is_cooling()` 按 `goal` 冷却 30 秒
- 被循环切走 → 循环调 `abort()`，`finally` 收尾；下次新建实例从头观察

## 现在**没有** L3 技能（故意的）

技能依赖真实世界的反馈：`dist` 阈值多少合适、`craft` 的 `count` 是次数还是个数。这些只有真机跑过才知道。
`nav.py` 的三个原语是例外——它们的动作序列已在本地 1.21.11 靶场跑通（`docs/06` 2026-11-14 01:33），
改成生成器之后还没有重新上真机。

还缺一块：现在调用方得自己给出"要挖/要放哪些格"。由起点和终点自动算出这串格子的 `reach_pos`
还没写，它要先在 probe 里验证。
