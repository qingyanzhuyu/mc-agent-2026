# planner —— 决策层

**职责**：看状态 → 输出**一个** `Decision`。不直接发 HTTP。

```
decision.py   Decision 数据结构 + 校验器（反射 Client 签名，防止模型发明接口）
llm.py        端点客户端（思考开关 / 空回复重试 / JSON 提取）+ 提示词
__init__.py   对外只暴露这几个名字
```

## 为什么要校验器

模型最常见的两种坏输出是「编一个不存在的方法」和「少给必填参数」。
`validate()` 用 `inspect.signature(Client.method)` 反射真实签名，所以：

- 客户端新增方法 → 校验器自动认
- 客户端参数改名 → 校验器自动拒绝旧参数名

**不要**改成手写白名单——那必然和 `client.py` 漂移。

## 提示词里只放"策略"，不放"接口细节"

接口参数由 `validate()` 保证，提示词只描述：目标、当前状态、可选动作名清单、生存优先原则。
接口细节写进提示词，会随接口版本变化而过期。

## 加一个新的决策策略

新建 `xxx.py`，导出一个函数：

```python
def next_decision(state_summary: str, allowed: list[str]) -> dict: ...
```

返回**未校验**的 dict 即可，调用方统一走 `validate()`。不要在这个函数里调 `Executor`。

## 现在的状态

- `decision.py` 可用（编译通过，校验逻辑是纯函数，可单测）
- `llm.py` **未在真机验证**：端点字段是实测出来的，但没跑过完整决策循环
