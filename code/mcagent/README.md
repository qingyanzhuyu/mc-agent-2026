# mcagent — Minecraft Agent API 的最小骨架

对应规范 **v1.3.1**（`https://mc-api.windywindie.top/api/v1`）。
目标不是"能打的 bot"，而是**把管道接对**：认证与 Token 持久化复用 / ready 状态机 / 观察 /
可抢占串行动作 / 非幂等超时判重 / 错误分类 / 安全闸 / 打点。

```
client.py     HTTP 客户端：错误封装、.token.json 落盘复用、401 单点重登、429/5xx 退避、姿态合并
runtime.py    State（单调时钟死亡去重 + 65s safe_mode）/ Safety / SafetyMonitor / Executor（可抢占单飞锁 + 非幂等判重 + 打点）
smoke.py      冒烟：登录 -> ready -> 状态 -> 背包 -> 事件
probe.py      基线：量 T_status / T_move20 / T_dig1，落盘 json；也是 --pathfinding 的入口
probe_path.py     寻路与手动兜底探测（跑的是 skills/nav.py 的原语）
probe_fixture.py  寻路夹具模板与离线校验
planner/      技能菜单与选择校验（常规路径）+ 原子动作 Decision 校验（兜底路径）+ LLM 客户端
skills/       三层动作抽象的 L3/L2：Skill 声明式契约、自注册表、nav 战术原语、离线夹具（暂无 L3 技能）
../tests/     离线故障注入、多线程抢占、技能框架单测（无需账号与外网）
```

分层与写技能的规则见仓库根 `AGENTS.md` §9。

> 所有命令在 **`code/`** 目录下通过 `uv run` 执行（根目录 `pyproject.toml` + `uv.lock` 锁定依赖）。

## 三条必须遵守的约束（写代码时别退回原样）

1. **业务错误大多 HTTP 200，同时兼顾网关/认证 HTTP 401/429/5xx**：优先看 `payload["ok"]`，但 `call()` 亦统一兜底 HTTP 401 单点刷新与 429/5xx 重试。
2. **本机注册表可能挂死代理 127.0.0.1:7897**，`requests` 默认会读它 → 必须 `trust_env=False`
   （`client.py` 已做）。要显式走代理就填 `MC_PROXY`。
3. **动作互斥且同步阻塞，但紧急避险可抢占**：一个动作没返回前不能发第二个普通动作（会 `busy`）。
   所以 `Executor` 内置条件变量单飞锁；后台 `SafetyMonitor` 发现危险时调 `Executor.emergency()` 立即发 `/stop` 并插队执行逃生动作；`/status`、`/events`、`/find_blocks` 不占锁，可以并行读。

## 快速开始

```bash
cp mcagent/.env.example mcagent/.env             # 填 MC_USER / MC_PASS（需要真机时）
uv run python -m unittest discover -s tests -v   # 跑 14 项离线故障注入单测（无需账号）
uv run python -m mcagent.client                  # 只验端点（不需要账号）
uv run python -m mcagent.smoke --dry             # 同上，输出更多
uv run python -m mcagent.smoke                   # 登录 + ready（需要账号，且要先在 docs/05 占位）
uv run python -m mcagent.probe                   # 量基线
```

## 什么算"已验证"（分级准入，见 `AGENTS.md` §2A.6）

| 模块 | 验证等级 | 状态说明 |
|---|---|---|
| `client.py`（401 单点刷新 / `.token.json` 复用 / 姿态合并 / `chat` 防 `/` 注入 / `ensure_connected` ≥30s） | `verified: offline` | 实测未授权 401（RTT ≈ 0.6s）+ 离线故障注入单测全过；**真实账号登录待验** |
| `planner/decision.py` 校验器（反射签名 + 拦截 `bool`/NaN/非法槽位） | `verified: offline` | 单测全覆盖，纯函数 |
| `runtime.py`（单调时钟死亡去重 + 65s `safe_mode` + 非幂等超时判重 + `emergency()` 抢占锁） | `verified: offline` | 14 项离线故障注入与多线程抢占单测通过；**真机未跑** |
| `smoke.py` / `probe.py` | `--dry` 已验 / `live` 待验 | 编译通过 + `--dry` 退 0，**登录真机路径等皮肤站账号** |

耦合世界物理的模块（`loop.py`、`planner/llm.py`、具体 `skills/*`）属于 `verified: live` 范畴，等真机验证跑通后再进 `main`。

## 目录约定

- `mcagent/.token.json` — 自动持久化的登录凭证（`0600` 权限，已 gitignore，切勿提交）
- `mcagent/probe/*.json` — 每次测量的原始数据，别删，后面调参要用
- `mcagent/logs/*.jsonl` — 每个动作一行（含 ms、错误码、重试次数），是复盘的原料
