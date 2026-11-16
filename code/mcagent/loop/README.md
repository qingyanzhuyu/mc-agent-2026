# `mcagent.loop` — 比赛主循环与持续异常取证子系统

本目录将**正式赛 8 小时主循环**与**赛前持续挂机测试 / 异常收敛取证 / HTTP 离线确定性回放**合二为一，零侵入复用上层冻结模块（`client.py`、`runtime.py`、`planner/`、`skills/`）。

## 核心设计要点

1. **一套主循环同时跑比赛与压测**（`engine.py`）：
   - `wait_ready -> 刷新状态 -> 规划选技能 -> 单步执行 -> 死亡/断线/冷却自愈 -> 继续`。
   - 死亡重生后不冗余发送 `POST /posture`（规范 §4.11 服务端自动复位姿态）。
2. **战略/战术两段式规划与实时状态派生**（`policy.py`、`strategy.py`、`strategy_async.py`、`context.py`）：
   - 重生后处于哪一阶段完全由规划层按刷新后的真实背包与环境实时推导，天然兼容 `keepInventory=true` 与 `keepInventory=false`。
   - **战略层（`--strategy {legacy,rule,llm}`，默认 `rule`）**：`policy.py` 仅保留 `RulePlanner`、`PlanDecision` 与薄分派 `CampaignPlanner.select()`，战略编排与异步预取分别拆至 `strategy.py` 与 `strategy_async.py`。`rule` 与 `llm` 统一输出 `Objective(goal_id, budget_s, why, source, started_at)`：`rule` 取台账性价比排序第一名；`llm` 仅在目标达成、环境挂起、预算耗尽、连败达上限、重生、昼夜切换、600s 例行复核、战术层无技能 8 类边界事件上通过 `DecisionScheduler` 异步请求模型，当拍立即由规则 `Objective` 继续推进，`select()` 永不阻塞等待模型；`legacy` 直接调用拆分前的 `RulePlanner.choose(state)`（带 `--llm` 时走 `_select_legacy_skill_llm`），供靶场基线对照与比赛期间一键回退。
   - **战术层（`RulePlanner(target_goal=...)`）**：将当前 `Objective` 收敛到前置闭包展开下一技能；生存补给（`eat_food`）与 `safe_mode` 始终优先于任何目标。
   - 既有 `--llm`（技能级同步菜单选择）在 `--strategy rule --llm` 与 `--strategy legacy --llm` 下保持原样兼容挂机控制台；`--strategy llm` 下技能级模型选择仅在战术层给不出技能时异步兜底。
   - 仅保留两种模式：`campaign`（比赛跑法）与 `focus`（单技能定向验收）。
3. **低噪声预期核对与进度曲线**（`auditor.py`）：
   - 优先核对每个技能自己声明的 `verify_done(state, before_counts)`，动作级仅用已有返回值做零额外 RTT 检查；姿态泄漏仅在**非死亡/非断线**结束时核对。
   - 异常严格分流为 `bug`（代码/契约/安全缺陷）、`strategy`（卡死/连续震荡/战斗折损）与已知环境波动（单次 `Replan` 只计数不入榜）。
   - 记录 `progress_curve.jsonl`（服务端进度分 + 背包里程碑代理分随时间曲线），支持 `--baseline` 跨版本回归对比。
4. **HTTP 层脱敏录制与确定性回放**（`tape.py`）：
   - 录制 `Client.call()` 往返（剥离一切密钥与 Token，按 `main`/`monitor` 线程打标）。
   - 任何异常包 `INC-*.json` 均可直接离线断网重放技能当时的完整决策流。

## 常用命令（在 `code/` 目录执行）

```bash
# 1) 离线契约与连通性干跑（不登录、不占号；支持 --strategy {legacy,rule,llm}）
uv run python -m mcagent.loop --dry
uv run python -m mcagent.loop --dry --strategy llm
uv run python -m mcagent.loop --dry --strategy legacy

# 2) 在队内本地联调网关持续运行（使用独立测试 bot 名，不踢队友、不碰 .token.json）
uv run python -m mcagent.loop --local-target --soak --user agy_soak1 --world-snapshot snap-v1 --max-steps 30 --disconnect-on-exit

# 3) 单技能定向回归验收
uv run python -m mcagent.loop --local-target --soak --user agy_soak1 --mode focus --skill gather_wood --skill-args '{"count":2}' --max-steps 10 --disconnect-on-exit

# 4) 离线断网重放任意一份异常包
uv run python -m mcagent.loop --replay mcagent/logs/soak-YYYYmmdd-HHMMSS/incidents/INC-001-....json
```

> 自建测试服虚空岛裁切、`keepInventory` 切换、RCON 快速搭场景与快照恢复工具物理隔离在仓库根 `tools/test-island/` 下，`code/mcagent/` 对其零导入、零依赖。
> 本子系统的全流程构建操作、离线 120 项单测与本地 `1.21.11` 靶场多轮 HTTP API 闭环验收记录见 [`ACCEPTANCE_REPORT.md`](./ACCEPTANCE_REPORT.md) 与 `docs/06-工作日志.md`。
