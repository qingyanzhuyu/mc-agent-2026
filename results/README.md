# `results/` · 脱敏实测证据与离线回放包

> 本目录汇总用于 **11/22 阶段成果展示** 与 **40 分工程评审** 的脱敏实测证据，全部来自真实运行原始记录并已剥离账号名（替换为 `<redacted_bot>`）、密码、Token 与 API Key。
> 严守 `AGENTS.md` §4 口径：**`official`（官方校园网直连 `https://apiin.nmo.net.cn/api/v1`）与 `local-target`（队内本地联调网关 `127.0.0.1:8787`）严格分列，绝不混充官方时间预算。**

## 一、目录索引

| 路径 | 来源标签 | 采样时间 | 证明内容 |
|---|---|---|---|
| `probe/probe-official-20261119.json` | **`official`** | 2026-11-19 | 官方校园网基线：`T_status` p50 `74.4ms`（稳态 `7.34ms`）、`T_move20` `3648ms`（5.5 格/秒）、`T_dig1` `2146ms`、`find_blocks`（r=16/32/64: `14.8/25.4/55.9ms`）、动作周期 `202.3ms` |
| `probe/api-capabilities-official-20261119.json` | **`official`** | 2026-11-19 | 官方部署能力无损取证（关闭 B18）：确认 `/control` 支持（无键返 `bad_args`）、`/stop` 正常、`/status` 含 `speed`/`velocity`/`jumping` |
| `probe/aia-llm-official-20261119.json` | **`official`** | 2026-11-19 | 官方 AIA 代理（`https://api.nju-aia.com/v1`，`model=default` → `qwen3.6-35b-a3b`）：4/4 快路径 JSON 成功、`p50=1652.7ms`，兼容多种关思考参数 |
| `probe/official-ext-calibration-20261119.json` | **`official`** | 2026-11-19 | 扩展接口与限速标定：`/look` 支持、`/advancements` 参数校验、`MC_MAX_RPS` `4/8/12/16` 全阶梯 `0` 次 429（`12 RPS` 实得 `10.19 RPS`、`p50=96.1ms` 最优） |
| `probe/c16-void-guard-official-20261119.json` | **`official`** | 2026-11-19 | C16 虚空/承重保护标定（`--void-guard`）：连续移动+跳跃 `12/12` ticks 支撑命中，**`0/12`（`0.0%`）误报**，`tick p50=223.2ms` |
| `probe/fault-drills-official-20261119.json` | **`official`** | 2026-11-19 | 三项官方真机故障演练全通过：① 断线重连（`63.11s`、`seq` 连续）；② `65s` `safe_mode` 冷却自动解除并恢复任务（附真实 `death_loop` 事件结构采样）；③ 3 线程并发 401 单飞刷新（`login_calls=1`、`428.7ms`） |
| `official-soak-20261119/summary.md` | **`official`** | 2026-11-19 | 官方 2h 长跑诊断汇总（有效 `6303.9s`，**0 死亡 0 崩溃**，873 步、`emergency_escape` 6/6，前 `124.7s` 拿下 5 项起步目标，精确锁定后段 4 环死锁与 `166.2%` Token 外推根因并已于 `c03c26e` 修复） |
| `official-soak-20261119/progress_curve.jsonl` | **`official`** | 2026-11-19 | 230 条官方长跑时序采样（时间、得分、进度数、死亡、累计输入/输出 Token） |
| `replay-sample/INC-005-P2-replan_oscillation-craft_gear.json` | **`official`** | 2026-11-19 | 可 **100% 离线确定性重放** 的官方异常包样本（`craft_gear(furnace)`，2/2 磁带命中，`matched_original_outcome=true`） |

## 二、零账号一键复现与离线回放命令

在仓库 `code/` 目录下执行（无需网络、无需游戏账号）：

```bash
# 1. 全量离线单测（1,275 项）
uv run python -m unittest discover -s tests

# 2. 脱敏官方异常包确定性离线重放（零网络，2/2 磁带命中）
uv run python -m mcagent.loop --replay ../results/replay-sample/INC-005-P2-replan_oscillation-craft_gear.json

# 3. 三层技能契约 + 76 项台账 DAG + 战略上下文干跑
uv run python -m mcagent.loop --dry --strategy llm --min-verified offline
```

## 三、阶段二新增技能本地靶场冒烟记录（显式标 `local-target`）

> 以下为 2026-11-19 在队内本地联调网关（`127.0.0.1:8787` → `10.6.17.10:25565`，MC `1.21.11`，独立角色 `de7_s2_check`，结束自动 `/disconnect`）执行的阶段二技能冒烟记录；**仅验证接口联动与状态机闭环，不纳入官方时间预算**。

| 验证项 | 模式 / 参数 | 实测结果（`local-target`） |
|---|---|---|
| 空包防误选门禁 | `survey_island` / `trade_villager` / `smelt_iron(async_lease=true)` | 全部在启动前被 `blocked(state)` 精准拦截，0 无效动作 |
| 起步木料 + `survey_island` | `gather_wood(2)` → `survey_island(sectors=2, radius=32)` | `gather_wood` 3 步 `12.0s` 拿 2 原木；`survey_island` 2 步 `18.9s` 完成 2 扇区体检，写入 `WorldResourceMemo` **44 条暴露资源**、**1 处地标** |
| `trade_villager` / `furnace_lease` | `blocked()` + `best_fuel_for(1)` + `lease_book_for(state)` | 持木后 `trade_villager` 门禁放行；`best_fuel_for(1)` 命中 `('oak_log', 1)`，租约账本正常初始化 |
| `discover_structure` | `discover_structure(structure="stronghold", radius=32)`（4 步有界） | 4 步 `34.8s` 有界探索推进正常，无卡死/崩溃，退出干净 `/disconnect` |
