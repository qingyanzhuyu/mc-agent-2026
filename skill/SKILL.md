---
name: mc-agent-repo
description: Use when working in the mc-agent-2026 repository (NJU "AI Agent for Minecraft" competition, Harness track) — writing docs, adding planner/skills code, or answering open questions. Covers where each kind of knowledge lives, the frozen module surfaces, the five repo-specific traps, and the definition of done.
---

# 在 mc-agent-2026 仓库工作

## 何时使用

- 要在 `mc-agent-2026` 里改文档、加代码、回答待确认问题
- 要判断"这条信息该写到哪个文件"
- 要接手别人没做完的活，需要快速知道现在到哪一步了

## 60 秒定位

南大「AI Agent for Minecraft 开发大赛」**Harness 赛道**备赛仓库。目标：写一个 harness，
让一个 Minecraft bot 在共享世界里自主生存 **8 小时**，按原版 advancement 计分。

| 关键 | 值 |
|---|---|
| 初赛 | 11/22 10:00–18:00，8h，取 8 队 |
| 报名/材料截止 | **11/18** |
| 决赛 | 12/6，双赛道同场 |
| 计分 | 游戏表现 60（advancement）+ 工程评审 40 |
| 接口 | `https://mc-api.windywindie.top/api/v1`，规范 v1.3.1 |

## 开工必读（按顺序）

1. `AGENTS.md` — **硬规则**，文件所有权 / 五条禁令 / DoD。违反会被回退。
2. `docs/01-关键判断.md` — J1–J10 当前共识。**别重推已经定下的判断。**
3. `docs/04-待确认.md` — 别问已经有人认领的问题。
4. `docs/05-分工与契约.md` — 你能改哪些文件、模块冻结面。

## 任务路由：什么东西写到哪里

| 手上的东西 | 写到 | 硬要求 |
|---|---|---|
| 官方条款、接口语义 | `docs/00-赛事事实.md` | 附来源（链接 / `api-spec` § 号） |
| 有依据的推论 | `docs/01-关键判断.md` | 编号顺延，**必带"推翻条件"** |
| 实测数字 | `docs/03` 或 `plan/` | 附命令 + `code/mcagent/probe/*.json` 路径 |
| 拿不准的 | `docs/04-待确认.md` | 写清"为什么重要"+"临时假设" |
| 决策逻辑 | `code/mcagent/planner/` | 输出 dict，交 `validate()`；**没跑通不合入** |
| 一个目标的动作序列 | `code/mcagent/skills/` | `step()` 一次最多一个动作；**没跑通不合入** |
| 规划/排期 | `plan/` | 只追加不重写 |

**绝不允许**把猜测写成陈述句。这个仓库里唯一比"没写"更糟的是"写错了但看起来很确定"。

## 常用命令

```bash
cd mc-agent-2026/code          # 包名是 mcagent，必须在 code/ 下跑

python -m mcagent.client       # 验端点（不需要账号）
python -m mcagent.smoke --dry  # 同上，带更多输出
python -m mcagent.smoke        # 登录 + 等 ready（需要 .env，且要占账号）
python -m mcagent.probe        # 量基线，落盘 probe/*.json
```

## 改代码的流程

1. 读 `docs/05` 的冻结面：`client.Client` 的方法签名、`runtime` 的三个类。**不改已有返回结构。**
2. 改完跑：
   ```bash
   cd code && python -m compileall mcagent && python -m mcagent.smoke --dry
   ```
3. 提交信息 `范围: 做了什么`，一次提交一件事。

## 五个坑（本仓库特有，踩过的）

1. **注册表里挂了死代理 `127.0.0.1:7897`** → `requests` 默认会读它，报 `ProxyError` 看着像断网。
   所以 `trust_env = False` 是必须的。而且两个端点的最优路径相反：
   **MC API 直连快（666ms）、LLM 端点走代理快（219ms vs 1520ms）**。
2. **动作互斥且阻塞**：同一 bot 一次只能跑一个动作，重复调用返回 `busy`。
   所以 24 并发只能用来"想"，不能用来"做"。别设计成并行执行。
3. **思考模型的静默失败**：`max_tokens` 给小了，token 全花在 `reasoning_content` 上，
   拿到 `finish_reason=length` + `content=""`。必须把空 content 当失败重试。
   关思考的正确写法是 `chat_template_kwargs={"enable_thinking": False}`，顶层开关无效。
4. **登录会踢人**：同一角色重复登录让旧 token 立即失效。登录前必须去 `docs/05` 账号表占位。
5. **死亡循环是一票否决**：60s 死 3 次 → 自动下线 5 分钟。Safety 闸的优先级高于任何计划。

## 不要做的事

- **不把没跑通的代码合进 main**：没有真机验证的技能/策略/LLM 接入，先放 `docs/04` 或分支

- 不提交密钥（`.env` / `sk-...` / 皮肤站密码），仓库里只放 `.env.example`
- 不动三处代码：`client.py` 的 `trust_env`、`CODE_CLASS`、`Executor` 的单飞锁
- 不 `push --force`、不 rebase 别人分支、不删别人的判断条目
- 不绕过 `Executor` 直接调 `client` 的动作方法（会丢日志、丢错误分类、破坏互斥）

## 收尾三件事

1. 更新 `docs/05` 里你那一行的状态（不在做了写"空闲"）
2. 没做完的写进 `docs/04`，别留半成品
3. `git status` 干净再走——下一个人可能就在同一个目录开工
