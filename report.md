# AIAMC 2026 Harness 赛道 · 工程技术报告

> **仓库**：`qingyanzhuyu/mc-agent-2026` · **目标**：可直接导出为 `report.pdf`（五部分评审材料包之一，对应 `docs/07-评审材料.md` §二）
> **编制原则**：所有数据均指向仓库内可验证的 `results/` 原始脱敏记录、`code/tests/` 单测或 `docs/06-工作日志.md` 条目；严格区分 **`official`（官方校园网端点）**、**`local-target`（队内联调靶场）** 与 **`verified: offline`（离线故障注入验证）**，不夸大边界。

---

## 一、问题建模与核心工程判断（J1–J20 闭环结论）

围绕 NMO 官方 REST API（`api-spec-v1.3.11.md`）、`RULES v2` 草案计分规则与官方校园网实测特征，我们在开发全周期维护了 20 条带"推翻条件"的关键工程判断（[`docs/01-关键判断.md`](docs/01-关键判断.md)），并随实测证据持续修订甚至主动推翻旧假设：

| 编号 | 核心判断摘要 | 当前验证状态与证据 |
|---|---|---|
| **J1 / J8 / J11** | 动作互斥阻塞 + 2 路并发上限 + LLM 重尾延迟 ⇒ **并发只用来"想"，确定性技能推进基础链，模型只做目标级异步战略** | **已坐实（official + local-target）**：官方 AIA 实测 `p50=1.65s`（低频）/`3.83s`、`p95=5.22s`（1306 次长跑）；`--strategy llm` 仅在 8 类边界事件经 `DecisionScheduler` 异步预取 `Objective`，`local-target` 240s 冷启动仅耗 `1,746` tokens |
| **~~J2~~ → J14 / J19** | 深度复利假设随 `RULES v2` 作废 ⇒ 计分本质是**带共享前置的加权集合覆盖**，7 个 100 分极限项不进主线 | **已修订并落地**：主动废止 J2，重构为 76 项 `GoalSpec` DAG 台账（1,725 分上限）与多目标共享子图边际 ROI 摊销排序（`rank_actionable_goals()`） |
| **J3 / J10 / J16** | 60s 死 3 次下线 5 分钟（不补时）+ 虚空岛赛制 ⇒ **稳定性与虚空防坠高于一切策略优化** | **已坐实（official）**：可抢占单飞锁 + `SafetyMonitor` C16 虚空承重保护在官方校园网实现 `0/12`（0.0%）误报；官方 2h 长跑实现 **0 死亡、0 崩溃**、`emergency_escape` **6/6 (100%)** |
| **~~J4~~ → J13** | 书面配额（8M/1.5M）与群聊口径差异 ⇒ 模型名、并发、思考开关与 `LLM_TOKEN_BUDGET` 全部配置化 | **已坐实（official）**：2h 官方长跑验证不限流战术兜底会在 8h 外推烧至 166.2% 输入额度；已补齐指数退避与 `LLM_TOKEN_BUDGET` 软熔断护栏 |
| **J5 / J9 / J20** | 40 分工程评审靠可追溯纪律与模块化边界拿稳；按"单技能解锁分值与复用度"排实现优先级 | **已落地**：`AGENTS.md` §2A.6/§9 强制 `verified: offline/live` 分级准入、一文件一职责（≤360 行）与零白名单 `@register` 自动发现 |
| **J6 / J7 / J17** | API 已是高层语义技能库，但无矿透/无鞘翅且模型会编造坐标与实体 ID ⇒ **技能参数严禁坐标与实体 ID，执行前现场观测复核** | **已坐实（offline + official）**：`@register` 启动期强制拦截 `pos/x/y/z/entity_id` 参数；`context.py` 剥离全部裸坐标与 ID（943–1141 字）；几何与射线统一收口至 `skills.sightline` |
| **J12 / J15 / J18** | 54 项进度依赖岛上实际结构；结构类进度判据只看玩家位置 ⇒ **开局扇区岛体检 + 方块指纹驱动 + 沿路增量扫描 + 支线止损** | **已落地（offline + local-target 冒烟）**：`world_memo.py` 维护 16 类结构/群系指纹与原子检查点；`survey_island` 已在 `local-target` 2 步完成 2 扇区体检写入 44 条资源与 1 处地标；`MainLoop` 补齐 30s 节流沿路扫描 |

---

## 二、系统架构与关键技术创新

### 2.1 三层动作抽象与四层异步调度架构

系统严守「调用只能向下、一文件一职责、每发一个阻塞动作立刻 `yield`」硬约束（[`AGENTS.md`](AGENTS.md) §9 与 [`docs/02-接口与架构.md`](docs/02-接口与架构.md)）：

```mermaid
flowchart TD
    S["战略层 StrategyOrchestrator<br/>--strategy llm · 8 类边界事件 · 2 路信号量"] -->|Objective 脱敏上下文 ≤2000 字| G["目标台账与规划层 loop/goals.py<br/>76 项 DAG · 共享子图边际 ROI · 物资预留 · 止损"]
    G -->|展开确定性最近前置技能| L3["L3 意图技能层 skills/*.py<br/>61 个 @register 技能 · 仅标量参数 · 1 动作 = 1 yield"]
    L3 -->|yield from| L2["L2 空间与战术原语<br/>sightline · voxel_cache · world_memo · reach · nav · escape · furnace_lease"]
    L2 -->|ex.动作()| L1["L1 执行器 runtime.Executor<br/>可抢占单飞锁 · 非幂等快照判重 · 16 RPS 三级限速"]
    M["后台看护 SafetyMonitor<br/>连续异步 status/events · C16 虚空保护 · 65s safe_mode"] -->|emergency() 抢占 /stop| L1
    L1 -->|REST HTTP 仅认 ok 字段| API["NMO 官方 REST API (v1.3.11)"]
    E["MainLoop 技能完成边界<br/>30s 低频沿路增量扫描"] -->|写入新地标 landmark_version++| S
```

### 2.2 五项核心技术创新点

1. **执行-思考重叠与目标级异步战略（破解 2 路并发与重尾延迟）**：
   - `planner/llm.py` 内置 2 路 `BoundedSemaphore` 并发闸门与代理/直连双通道熔断；`loop/strategy.py` + `strategy_async.py` 仅在 `initial`/`goal_achieved`/`env_blocked`/`budget_exceeded`/`max_fails_reached`/`respawn`/`day_night_changed`/`landmark_opportunity` 等 8 类边界事件上由 `DecisionScheduler` 异步预取下一 `Objective`，当拍立即由确定性规则推进，**零阻塞等待模型**。
2. **零裸坐标契约与统一体素视线层（根治模型坐标幻觉与几何偏移）**：
   - `skills/registry.py` 在 `@register` 期强制禁止非标量参数与 `pos/x/y/z/entity_id` 字段；`loop/context.py` 输出严格脱敏的战略上下文（典型 **943–1,141 字**）；`skills/sightline.py` 统一封装站立眼高 `+1.62` / 潜行 `+1.27`、方块中心 `+0.5` 与 3D DDA 视线遮挡，消除手写脚底角点距离导致的最高 1.62 格高度误差与方向不对称。
3. **非幂等快照判重 + 可抢占单飞锁 + `v1.3.11` 精选恢复**：
   - 对 `craft`/`use_item`/`window_act` 等非幂等写动作在发包前采样背包指纹，网络或服务端 `timeout` 时先 `/stop` 再比对快照，避免重复扣料；`Condition` + 抢占代次保证 `SafetyMonitor` 遇岩浆/虚空/死亡循环立即抢占当前阻塞动作；401 单飞双检锁（`login_calls=1`）杜绝多线程并发互踢 Token。
4. **多目标共享子图 DAG 边际 ROI + 异步熔炉租约 + 沿路增量扫描**：
   - `rank_actionable_goals()` 采用两遍扫描统计每个公共前置节点（如 `smelt_iron`/`mine_diamond`/`enter_the_nether`）被多少活跃高分下游共享，按共享度摊销前置成本；`skills/furnace_lease.py` 支持 `blast_furnace`/`smoker`（2× 速率）、`lava_bucket` 空桶回收与 `async_lease=True` 非阻塞交还；`MainLoop._maybe_roadside_scan()` 在技能完成边界按 `30s` 节流做短半径结构/机会实体扫描，写入 `WorldResourceMemo` 触发 `landmark_opportunity`。
5. **无朝向参数 `/place` 的多侧重试闭环与虚空安全搭桥**：
   - 针对 NMO API `/place` 无朝向参数的限制，`t2_trials_oriented.py` 实现 `place_oriented()` 多侧站位放置 + `get_block` 复核 `facing`/`axis` + 错向挖回换侧重试，打通比较器读雕纹书架与嘎枝之心轴向夹心；`end_rush.py` 实现 `bridge_segment()` 虚空承重保护搭桥与 6 级优先级摔落救援。

---

## 三、真实工程迭代案例（抽取自 `docs/06-工作日志.md` 标「评审价值：有」）

以下按时间线呈现从 D0 至今最能体现"取证 → 归因 → 架构收敛 → TDD 回归锁死"的 6 个关键工程闭环：

| 阶段 | 观察到的真实问题 | 根因定位与架构改造 | 量化验证结果 |
|---|---|---|---|
| **① D0 协议安全（11/13 `ec8eabb`）** | 401 重登穿透并发互踢；轮询 `last_death.t` 重复计数误触发 65s 冷却；`posture(shield=True)` 清零潜行 | 双检锁 `_refresh_token()` + `0600` `.token.json` 复用；按 `last_death.t` + `seq` 联合去重；全量合并姿态字段 | 离线故障注入 8 线程并发 401 仅登录 1 次；后续 11/19 **官方真机 3 线程演练 `login_calls=1`（428.7ms）复证** |
| **② 几何统一与 v1.3.11 对齐（11/15–11/16）** | 10+ 技能手写脚底到方块角点距离存在 ±0.87 格偏移；附魔跨窗丢失、下界 `ready=false` 抢读、钓鱼 `timeout` 三连重发 | 收口全仓至 `skills.sightline` 并写入 `AGENTS.md` §9.2；按 v1.3.11 逐节补齐可选路由 `unsupported_api` 降级与 `/stop released` 校验 | 全仓无裸脚底角点测距；`test_sightline.py`、`test_api_v1311_client.py` 与 `test_runtime_v1311_recovery.py` 全绿 |
| **③ 53 轮冷启动归因与架构收敛（11/19 `01:55`）** | 旧自动修单分支叠加 230+ 补丁使 `starter_skills.py` 膨胀至 3,014 行，`smelt_iron` 达成率仅 `11.3%`（6/53），冷却空睡占 `59%` 时长，单轮 `/get_block` 达 `2,883` 次 | 果断止损旧膨胀并加 `--observe-only` 护栏；落地 `voxel_cache`、`reach.py`、原子 `world_memo` 检查点、同格冷却三级升级（`local_retry→relocate→goal_abandoned`）与 `--strategy llm` | `local-target` 240s 冷启动实测（`de7_v2_test02`）：0 死亡、`/dig` 33/33、`/craft` 7/7、`prep_wood` 提速 **2–3×**、`/get_block` 降 **70%+**（551 次）、Token 外推由 313% 压至 **2.5%** |
| **④ P8 物理与同步取证修复（11/19 `04:16`）** | 靶场 HTTP 磁带暴露 `GoalNear` 抖动、`set_slot` 1-tick 延迟、滞空幽灵块回滚与悬空树冠 `stalled` | 逐条回放磁带定位 MC 1.21.11 物理同步缺陷，先红后绿补齐回归单测并关停本地桥疾跑/跑酷（`4aedb5d`） | `test_p8_log_fixes.py` + `bot_session.test.js`（11/11）全绿 |
| **⑤ 官方 2h 长跑取证与 Token 黑洞根治（11/19 `f416d71`→`c03c26e`）** | 2h 官方连跑前 `124.7s` 拿下 5 项起步目标且 0 死亡，但后 103 分钟陷入 4 环死锁（地下未记轨 + 未复用 7 格外已知工作台 + 地下无净空 502 次 `Replan` + 战术无技能每 5s 回落旧 LLM 菜单），烧掉 **2.91M 输入 Token**（8h 外推 **166.2%**） | 离线回放 `INC-005` 精准复现；`CraftGear` 复用 `WorldResourceMemo` 已知台、`MineStone` 写 `MINE_TRAILS`、`ScoutIsland` 地下安全上挖 1×2 阶梯、`strategy.py` 战术兜底 LLM 加 `20s→240s` 指数退避 + `LLM_TOKEN_BUDGET=7500000` 护栏 | 新增 `test_official_soak_fixes.py` 8 条回归单测全绿；仅将官方实机 0 缺陷的 4 项技能升格 `verified="live"` |
| **⑥ 阶段二三波模块化扩展（11/19 `3454a9f`）** | 台账原仅覆盖 T1 26 项（290 分），缺少结构勘探、工作站、下界核心、末地冲刺、试炼密室、群系 T3 与提速替代路线 | 在隔离 worktree 用多路严格文件互斥并行子代理分三波落地 9 个新技能文件（单文件均 ≤360 行，仅极少数已有大文件按职责增量扩展）+ 共享子图 DAG ROI + 30s 节流沿路扫描 + 5 条提速替代路线 | 台账扩至 **76 项 / 1,725 分上限**、**61 个注册技能**；全仓 **1,275/1,275** 单测全绿，`survey_island` 等已过 `local-target` 冒烟 |

---

## 四、实测数据汇总（`official` 与 `local-target` 严格分列）

### 4.1 三端点性能与稳定性基线对照

| 指标 | 本地靶场（`local-target`，`127.0.0.1:8787`） | **官方校园网（`official`，`apiin.nmo.net.cn`）** | 官方公网（`mc-api.windywindie.top`） | 原始证据 |
|---|---:|---:|---:|---|
| 无鉴权 `/status` RTT | ~1.5 ms | **5–11 ms** | 816–973 ms | `docs/03` §6.1 |
| `T_status`（鉴权 p50） | 1.5 ms | **7.34 ms**（稳态）/ **74.4 ms**（首轮加载） | 500–700 ms | `results/probe/probe-official-20261119.json` |
| `find_blocks`（r=16/32/64）p50 | — | **14.8 / 25.4 / 55.9 ms** | — | `results/probe/probe-official-20261119.json` |
| `T_move20` / `T_dig1` | 2175 ms / 1736 ms | **3648 ms**（5.5 格/秒）/ **2146 ms** | 待测 | `results/probe/probe-official-20261119.json` |
| 单动作周期纯开销 | 7.3 ms | **202.3 ms** | ≈1200–1500 ms | `results/probe/probe-official-20261119.json` |
| `MC_MAX_RPS` 压测（4/8/12/16） | 16 RPS 0 限流 | **全阶梯 0 次 429**（12 RPS 实得 10.19 RPS，p50 96.1ms） | 受 RTT 节流 | `results/probe/official-ext-calibration-20261119.json` |
| C16 虚空承重保护误报率 | 0% | **`0 / 12`（0.0%）**，tick p50 `223.2 ms` | 待测 | `results/probe/c16-void-guard-official-20261119.json` |
| 官方 AIA LLM（`qwen3.6-35b-a3b`） | — | 快路径 `p50=1652.7 ms`（4/4）；长跑 1306 次 `p50=3831 ms` / `p95=5224 ms` | 同上 | `results/probe/aia-llm-official-20261119.json` |

### 4.2 官方三项故障恢复演练（`results/probe/fault-drills-official-20261119.json`）

| 演练项目 | 关键观测指标（`official`） | 结果 |
|---|---|---|
| 1. 断线与守约重连 | 断线返 `not_connected`；`63.11s` 恢复就绪（遵守 ≥30s 间隔）；`/events` `seq` 跨重连连续；恢复后动作正常 | **PASS** |
| 2. 65s `safe_mode` 冷却自愈 | 锁定 `65.0s` 冷却，实持 `65.2s` 自动解除并恢复 `gather_wood`；同步采得官方 `death_loop` 事件结构 | **PASS** |
| 3. 3 线程并发 401 单飞刷新 | 3 线程同时遇 401 仅触发 **`login_calls=1`**（`428.7ms`），Token 落盘轮换并在 `31.98s` 完成自动重连 | **PASS** |

### 4.3 全仓自动化测试与目标台账覆盖率

| 维度 | 数量 / 状态 | 复现命令 |
|---|---|---|
| Python 离线单测（含故障注入与契约） | **`1,275 / 1,275 OK`**（0 失败 0 跳过） | `cd code && uv run python -m unittest discover -s tests` |
| 本地联调网关契约单测（Node.js） | **`11 / 11 OK`** | `node --test tools/local-api-bridge/test/*.test.js` |
| 官方脱敏异常包确定性离线回放 | **`INC-005` 2/2 磁带命中，`matched_original_outcome=true`** | `cd code && uv run python -m mcagent.loop --replay ../results/replay-sample/INC-005-P2-replan_oscillation-craft_gear.json` |
| 注册技能总数 | **61 个**（**4** `verified="live"` + **57** `verified="offline"`） | `cd code && uv run python -m mcagent.loop --dry --strategy llm --min-verified offline` |
| 目标台账覆盖 | **76 项**（71 计分项 / **1,725 分上限**：T1 290 分 + 阶段二 50 项 1,435 分） | `tests/test_stage2_ledger_contract.py`（5/5 OK） |

---

## 五、已知局限、诚实边界与后续计划

遵循 `docs/07-评审材料.md` §六的诚实边界原则，明确声明当前尚未完成的真机验证范围：

1. **分级验证边界**：当前仅有 4 项基础技能（`gather_wood`、`craft_gear`、`mine_stone`、`emergency_escape`）在官方服 2h 长跑中零缺陷跑通并标为 `verified="live"`；其余 **57 个技能（含全部 37 个阶段二新技能）均如实标为 `verified="offline"`**，台账 50 项 T2/T3 目标均标为 `stage="draft"`，待靶场与官方服按 WIP=1 逐项实测后再升格并合入 `main`。
2. **三项 API 未证实与物理启发式局限**：
   - `place_oriented()` 依赖服务端 `/place` 随玩家相对方位决定方块 `facing`/`axis` 的原版物理语义，需在官方服实测核对各工作方块朝向映射；
   - `SilkTouchBeeNest` 因 `/get_block` 不返回蜂巢内蜜蜂数量，当前采用夜间/雨天/近蜂保守策略；
   - 3 项强依赖未证实 API 语义的目标（如 `crafters_crafting_crafters`、`salvage_sherd` 等待探测项）及 25 项高风险 T4 目标（含 7 个 100 分极限项）暂不入主台账。
3. **11/22 后与决赛冲刺计划**：
   - 立即在官方模拟服复验 `c03c26e`（工作台复用 + 地下脱困 + 战术 LLM 退避）消除 4 环死锁与 Token 超额；
   - 按共享子图 ROI 逐批真机晋级 `smelt_iron` → `shiny_gear` → `enter_the_nether` → `survey_island`/要塞/试炼密室/末地冲刺，并积累官方 `goal_cost_table.json` 实测遥测驱动动态重排。
