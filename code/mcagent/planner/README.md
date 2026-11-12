# planner —— 决策层

**职责**：看状态 → 输出**一个** `Decision`。不直接发 HTTP，不调 `Executor`。

```
decision.py    Decision 数据结构 + 校验器（已用 9 个用例验过，纯函数）
__init__.py    对外只暴露 5 个名字
```

## 为什么要校验器

模型最常见的两种坏输出是「编一个不存在的方法」和「少给必填参数」。

`validate()` 用 `inspect.signature(Client.method)` 反射真实签名，所以：

- 客户端新增方法 → 校验器自动认
- 客户端参数改名 → 校验器自动拒绝旧参数名

**不要**改成手写白名单——那必然和 `client.py` 漂移。

签名之外还有一类漏网：`move_to` / `shoot` / `interact` 的参数全都有默认值，
签名查不出问题但语义上必须成对，所以 `_cross_rules()` 单独覆盖。

## 边界（重要）

```
planner/   出决策        ← 不许出现 Executor 调用
skills/    执行决策      ← 不许出现 HTTP
runtime/   执行器        单飞锁 / 错误分类 / 重试 / 打点
```

## 现在**没有**的东西（故意的）

- 没有 LLM 客户端：端点行为已实测，但没跑通完整决策循环 → 不进仓库
- 没有策略实现：主线 DAG 要等计分表

加实现时：新建 `xxx.py`，导出 `def next_decision(state_summary: str, allowed: list[str]) -> dict`，
返回**未校验**的 dict，调用方统一走 `validate()`。
**跑通之前不要合进 main**（`AGENTS.md` §2 第 6 条）。
