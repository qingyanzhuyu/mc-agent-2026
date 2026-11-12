# mcagent — Minecraft Agent API 的最小骨架

对应规范 **v1.3.1**（`https://mc-api.windywindie.top/api/v1`）。
目标不是"能打的 bot"，而是**把管道接对**：认证 / ready 状态机 / 观察 / 串行动作 /
错误分类 / 安全闸 / 打点。

```
client.py     HTTP 客户端：错误封装、token 自动重登、错误码分类、按动作给超时
runtime.py    State（状态+事件增量）/ Safety（安全闸）/ Executor（单飞锁+重试+打点）
smoke.py      冒烟：登录 -> ready -> 状态 -> 背包 -> 事件
probe.py      基线：量 T_status / T_move20 / T_dig1，落盘 json
planner/      决策契约 + 校验器（没有策略实现）
skills/       Skill 基类 + 空注册表（没有具体技能）
```

> 所有命令在 **`code/`** 目录下执行（包名是 `mcagent`）。

## 三条必须遵守的约束（写代码时别退回原样）

1. **业务错误一律 HTTP 200**，只看 `payload["ok"]`，别看状态码。
2. **本机注册表有个死代理 127.0.0.1:7897**，`requests` 会读它 → 必须 `trust_env=False`
   （`client.py` 已做）。要显式走代理就填 `MC_PROXY`。
3. **动作互斥且同步阻塞**：一个动作没返回前不能发第二个（会 `busy`）。
   所以 `Executor` 里有单飞锁；`/status`、`/events`、`/find_blocks` 不占锁，可以并行读。

## 快速开始

```bash
cp .env.example .env      # 填 MC_USER / MC_PASS
python -m mcagent.client          # 只验端点（不需要账号）
python -m mcagent.smoke --dry     # 同上，输出更多
python -m mcagent.smoke           # 登录 + ready（需要账号，且要先在 docs/05 占位）
python -m mcagent.probe           # 量基线
```

## 什么算"已验证"

| 模块 | 状态 |
|---|---|
| `client.py` 端点/错误封装/`probe()` | 已验证（未授权 401，RTT ≈ 0.6s） |
| `planner/decision.py` 校验器 | 已验证（9 个用例，纯函数） |
| `runtime.py` | 编译通过，**未在真机跑过** |
| `smoke.py` / `probe.py` | 编译通过 + `--dry` 通过，**登录路径未验证** |

**没跑通的东西不进 main**（`AGENTS.md` §2 第 6 条）。这也是为什么现在没有 `loop.py`、
没有 `planner/llm.py`、没有具体 skill——它们都得等真机。

## 目录约定

- `probe/*.json` — 每次测量的原始数据，别删，后面调参要用
- `logs/*.jsonl` — 每个动作一行（含 ms、错误码、重试次数），是复盘的原料
