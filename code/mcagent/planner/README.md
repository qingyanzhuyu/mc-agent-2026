# planner —— 决策层

**职责**：看状态 → 输出**一个** `Decision`。不直接发 HTTP，不调 `Executor`。

```
skill_choice.py  技能菜单 + 选择校验 + choose()        ← 常规路径：模型从菜单里选一个技能
decision.py      Decision 数据结构 + 校验器（纯函数）   ← 兜底路径：没有技能覆盖时出一个原子动作
llm.py           LLM 客户端 + 通用重问循环 ask_json() + 线路降级
llm_fault.py     调用失败的分类（LLMError.kind）与熔断器
scheduler.py     不阻塞动作的模型预取槽
```

## 两条决策路径

| | 常规：选技能 | 兜底：原子动作 |
|---|---|---|
| 模型输出 | `{"skill", "args", "why"}` | `{"goal", "action", "args", "why", "expect", "abort_if"}` |
| 校验器 | `skill_choice.validate_choice()`，读 `skills.REGISTRY` 的声明 | `decision.validate()`，反射 `Client` 签名 |
| 模型碰坐标吗 | 不碰（技能参数注册时就限制为标量） | 会碰，执行前要按 J17 二次核对 |
| 入口 | `skill_choice.choose(llm, summary, state)` | `LLMClient.decide(summary)` |

两个校验器都**不维护名单**：一个读技能自己的声明，一个反射客户端签名。完整规则见 `AGENTS.md` §9.5。

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

## 现在有什么

| 文件 | 状态 |
|---|---|
| `decision.py` | 校验器，纯函数，9 个用例 |
| `llm.py` | LLM 客户端 + 决策重问循环；**15 个离线单测 + 真机 3/3 抽测**（`decide()`） |
| `llm.py` 的降级 / `llm_fault.py` | 代理坏了改直连 + 两级熔断；18 个离线单测，**阈值未经真机标定** |
| `skill_choice.py` | 菜单 / 校验 / `choose()`；离线单测覆盖，**提示词措辞未在真模型上抽测** |

`llm.py` 的三个实测结论已写进代码注释：
1. `model` 只能写 `default`（后端是 qwen3.6-35b-a3b，字面写模型名会 403）
2. 关思考走代理（356ms）、开思考直连（3.2s）——长请求走代理会被掐断
3. 模型 JSON 约束不强，**必须靠 `validate()` 拦 + 回喂错误重问**

## 模型不可用时会发生什么

| 情况 | 行为 |
|---|---|
| 本机代理连不上，或经代理收到 502/503/504 | 同一次调用里用**剩余**时间改直连重试；调用方无感 |
| 代理连续坏 3 次 | 代理线路熔断 60 秒，期间直接走直连；到期试探一次，仍坏就立刻再熔断 |
| 所有线路连续失败 3 次（连不上、超时、非 200、返回 error） | 端点整体熔断 60 秒：`chat()` 不发请求直接抛 `kind="circuit_open"`，`ask_json()` 不重试不睡眠返回 `None` |
| 空 content、调用方只给了不到 1 秒的超时 | 不计入熔断（前者是模型行为，后者是时间预算见底） |
| 超时 | 不改直连——时间已经用完，重发只会让调用方多等 |

三个数（3 次 / 60 秒 / 降级开关）走 `LLM_BREAKER_FAILS`、`LLM_BREAKER_COOLDOWN_S`、`LLM_FALLBACK_DIRECT`。
"直连"是不经本机代理访问**同一个官方端点**，不是换模型服务（`AGENTS.md` §2B.3 不允许后者）。
状态用 `LLMClient.health()` 读：各线路成功次数、降级次数、熔断次数和剩余冷却。

## 现在**没有**的东西

- 没有策略实现：挑哪个目标还是人定的

加实现时：新建 `xxx.py`，导出 `def next_decision(state_summary: str, allowed: list[str]) -> dict`，
返回**未校验**的 dict，调用方统一走 `validate()`。
**跑通之前不要合进 main**（`AGENTS.md` §2 第 6 条）。
