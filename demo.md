# 11/22 线下交流日与阶段模拟展示脚本（`demo.md`）

> 严格对照 2026-11-15 版公开教学包 §4 七步展示闭环（**目标 → 架构图 → 调用序列 → 现场运行 → 实际结果 → 失败处理 → 下一步**）与 [`plan/赛前切换清单.md`](plan/赛前切换清单.md) §A 编写。
> 原则：**展示与正式计分严格区分；`official` 与 `local-target` 数据明确标注；任何时候不在投屏终端打印 `.env`、密码、Token 或 API Key。**

---

## Step 1 · 目标（1 分钟）

- **赛道与定位**：AIAMC 2026 **Harness 赛道**——面向 8 小时不补时、动作互斥阻塞、无矿透/无鞘翅、虚空岛死亡循环（60s 死 3 次下线 5 分钟）与 2 路 LLM 并发限制的高可靠 Minecraft Agent Harness。
- **当前阶段成果（截至 `main@d9e3f5c`，已合入 `feat/arch-converge-v2-agy` 268 commits）**：
  - **109 项 `GoalSpec` 目标台账**（覆盖 **94 / 99** 项计分进度，理论上限 **2,925 分 [86.4%]**；阶段分布 `6 graduated / 1 candidate (smelt_iron) / 102 draft`，严格遵守 WIP=1；**52 项**地标先验静默门控 `requires_landmarks`；明确放弃 **5 项**高脆度/极低性价比目标，合计 **460 分**：`all_effects`、`all_potions`、`create_full_beacon`、`uneasy_alliance`、`two_birds_one_arrow`）；
  - **85 个注册 L3 技能**（**4** 项已在官方服 2h 实测零缺陷晋级 `verified="live"`：`gather_wood`、`craft_gear`、`mine_stone`、`emergency_escape`；**81** 项已完成 `verified="offline"` 故障注入单测，含阶段二、阶段三 Wave A–D 18 项及新增 `brush_archaeology`、`hatch_and_raise_sniffer`、`collect_froglights`）；
  - **1,789 项 Python `unittest` + 16 项 Node 网关契约测试（`tools/local-api-bridge/test/`）全绿（0 失败 0 跳过）**；已完成 **`official`** 官方校园网端点基线标定、三项真机故障演练、C16 误报 0/12 与 2 小时连跑取证，以及 **`local-target`** Stage 3 冒烟、Sprint 1–4 生存收敛与 `soak8h_deep`（`seg-01..seg-31`）深度压测。

---

## Step 2 · 架构图（1.5 分钟）

核心设计：**三层向下调用（L3 意图技能 → L2 战术原语 → L1 单飞执行器）+ 四层异步调度（目标级异步战略与确定性技能推进解耦）**。

```text
  [战略层] StrategyOrchestrator (--strategy llm, 仅 8 类边界事件触发, 2 路并发闸门)
       │  输出脱敏 Objective (上下文 1,085–1,283 字, 零裸坐标/零实体 ID)
       ▼
  [台账层] Goal Ledger (109 项 DAG / 94/99 计分 / 2,925 分上限 [86.4%] + 52 项 requires_landmarks 先验静音 + 弃 5 项 460 分)
       │  展开最近未满足技能 (铁器/基础链确定性推进, 不等 LLM)
       ▼
  [L3 意图技能] 85 个 @register 技能 (4 live + 81 offline / 仅标量参数 / 1 阻塞动作 = 1 yield)
       │  yield from
       ▼
  [L2 空间与战术原语] sightline (3D DDA) / voxel_cache / world_memo (21结构+4群系+32×32网格+虚空停探) / landmark_nav / mine_shaft / spawn_shelter / camp_base / camp_plan / reach / escape / nav / furnace_lease
       │  ex.<动作>()
       ▼
  [L1 执行与安全层] Executor (可抢占单飞锁 + 非幂等快照判重 + 100,000 RPS 无频控零排队快路径 + 32 路连接池 + 8 线程 get_blocks_batch + busy:dig 双端自愈)
       ▲  emergency() 随时抢占 /stop
  [后台看护] SafetyMonitor (风险态 20 Hz 0.05s / 安全态 0.15s, 苦力怕 <7.5m / 远程 14m 预警 + C16 虚空承重保护 + 65s safe_mode)
```

---

## Step 3 · 调用序列（1 分钟）

以「空包起步 → 采集原木 → 工作台 → 木镐 → 挖圆石 → 石镐（`upgrade_tools`）」短任务为例，单步严格遵守 **1 阻塞动作 = 1 `yield`** 与 **动作间安全抢占窗口**：

1. `preflight` 静态门禁（12 项检查）→ `POST /auth/login`（单飞锁 + `0600` `.token.json` 跨重启复用）→ `GET /status` 等 `ready=true`。
2. `MainLoop` 调 `CampaignPlanner.select()`：空包按共享子图 DAG ROI 选中根目标 `shiny_gear` → 展开最近前置 `prep_wood` → 派发 `gather_wood(count=4)`。
3. `gather_wood` 通过 `find_blocks(oak_log)` 观察 → `skills.sightline` 复核眼距与 3D 视线 → `ex.move_to`（yield）→ `ex.dig`（yield，每步间 `SafetyMonitor` 检血量/虚空/敌对）。
4. 达成 `prep_wood` 后自动衔接 `prep_wooden_pickaxe`（`craft_gear(wooden_pickaxe)`）→ `mine_stone`（记 `MINE_TRAILS` 地下回地表轨迹）→ `upgrade_tools`（复用 `WorldResourceMemo` 已知工作台合石镐），达成 `target_goal` 即止。
5. 每 `5s` 或位移 `≥8` 格在技能完成边界执行沿路增量扫（`_maybe_roadside_scan`）与零成本群系被动采样，发现新结构/群系指纹/流浪商人/僵尸村民写入 `WorldResourceMemo`（21 类结构指纹 + 4 类群系方块指纹 + 32×32 访问网格 + 虚空岛穷尽停探）触发 `landmark_opportunity`，解除对应 `requires_landmarks` 先验静音。

---

## Step 4 · 现场运行（一键短任务命令，3 分钟）

> **会前检查**：投屏前确认终端未导出 `MC_PASS` / `LLM_KEY`；若连官方模拟服，先停本机 `8787` 网关（`curl --max-time 2 http://127.0.0.1:8787/healthz` 必须失败）。

### 方案 A · 官方校园网 / 模拟服一键短任务（`official`）

```bash
cd code

# 1. 赛前 12 项门禁自检（核验官方端点、2 并发、无 8787 网关、无残留进程）
uv run python -m mcagent.preflight --apply --live

# 2. 连通性与 RTT 冒烟（确认 ready=True，校园网约 5–11ms）
uv run python -m mcagent.smoke

# 3. 一键短任务演示：空包自举 -> 木 -> 木镐 -> 挖石 -> 石镐，达成即停并自动下线
uv run python -m mcagent.loop \
  --live --llm --soak \
  --goal upgrade_tools \
  --min-verified offline \
  --max-seconds 240 \
  --disconnect-on-exit
```

### 方案 B · 队内本地靶场一键短任务（显式标注 `local-target`）

> 仅在本地/局域网测试服（`127.0.0.1:8787`）联调展示时使用，**须向评委明确说明为 `local-target` 靶场环境**。

```bash
cd code

# 1. 干跑核验 85 技能与 109 项台账
uv run python -m mcagent.loop --dry --strategy llm --min-verified offline

# 2. 一键短任务演示（使用独立角色名，退出自动 /disconnect）
uv run python -m mcagent.loop \
  --local-target --user demo_stage3 \
  --goal upgrade_tools \
  --min-verified offline \
  --max-seconds 180 \
  --soak --disconnect-on-exit
```

### 方案 C · 断网 / 现场测试服不可达兜底演示（100% 离线，零网络零账号）

```bash
cd code

# 1. 现场跑全量离线单测与故障注入套件（1,789 项 Python unittest，0 失败 0 跳过；另附 Node 网关契约 16 项）
uv run python -m unittest discover -s tests

# 2. 展示 85 技能契约（4 live + 81 offline）+ 109 项台账 DAG + 脱敏战略上下文
uv run python -m mcagent.loop --dry --strategy llm --min-verified offline

# 3. 确定性离线重放 2026-11-19 官方 2h 长跑真实异常包（INC-005，2/2 磁带命中）
uv run python -m mcagent.loop --replay ../results/replay-sample/INC-005-P2-replan_oscillation-craft_gear.json
```

配合展示 [`results/official-soak-20261119/summary.md`](results/official-soak-20261119/summary.md) 与 [`results/probe/fault-drills-official-20261119.json`](results/probe/fault-drills-official-20261119.json) 讲解官方真机数据。

---

## Step 5 · 实际结果（1.5 分钟）

展示四组已归档在 [`results/`](results/README.md) 中的真实数据（严守 `official` 与 `local-target` 分列）：

1. **官方校园网基线（`official`，`results/probe/pathfinding-official-20261119.json`、`probe-official-20261119.json` & `aia-llm-official-20261119.json`）**：
   - `T_status` 稳态 `7.34ms`（`pathfinding-official-20261119.json`）、首轮加载 `74.4ms`（`probe-official-20261119.json`）；`T_move20` `3648ms`（5.5 格/秒）、`T_dig1` `2146ms`、动作周期纯开销 `202.3ms`（`probe-official-20261119.json`）；
   - 2026-11-19 历史阶梯测试 `MC_MAX_RPS` `4/8/12/16` 全阶梯 **0 次 429**（11/21 确认赛事无频控后已升级 `100,000 RPS` 无频控零排队快路径，保留 `/stop` 优先与 429 退避）；C16 `--void-guard` 虚空保护 **`0/12`（0.0%）误报**；
   - 官方 AIA 模型（`default` → `qwen3.6-35b-a3b`）低频快路径 `p50=1652.7ms`（4/4 成功）。
2. **官方 2 小时连跑（`official`，`results/official-soak-20261119/summary.md`）**：
   - 有效运行 `6,303.9s`，**0 死亡、0 崩溃**，行动 `873` 步（错误率 `5.04%`，即 `44/873` 次失败：`/move_to` 失败 `43` 次 + `/dig` 失败 `1` 次，原始日志均记为 `path_not_found`），`emergency_escape` 真机 **`6/6 (100%)`**；前 `124.7s` 拿下 5 项起步目标（服务端确认 4 项进度 / 10 分）；`c03c26e` 四环死锁修复与 `100k RPS` 改造待新一轮官方对比复验。
3. **阶段二技能本地靶场冒烟（`local-target`，`<local_test_bot>`）**：
   - 空包门禁精准拦截 `survey_island`/`trade_villager`/`smelt_iron`；获木后 `survey_island` 2 步 `18.9s` 完成 2 扇区体检并写入 `44` 条暴露资源与 `1` 处地标；`discover_structure(stronghold)` 4 步有界推进无崩溃。
4. **Stage 3 Wave A–D 与 `local-target` 3/3 真进度解锁冒烟（`local-target`，`results/probe/t4-p{1..4}-*.json` & `stage3-local-smoke-20261119.json`）**：
   - Step 0 探针标定全通：`T4-P1`（`/attack(max_hits=1)` 与 `/control(hit=true)` 均一次弹出并拾取满耐久鞘翅）、`T4-P2`（确认自搭 Vanilla `1.21.11` `hard` 刷怪正常并修复 `SurveyIsland`/`ScoutIsland` 卡点）、`T4-P3`（私域下界 `Y=127` 基岩顶水平 T 形召唤凋灵零漂移稳态窒息 `-1.0 HP/s`）、`T4-P4`（B13 炽足兽 `/use_item` 触发耐久 `100->99` 条件解锁）；
   - 实时冒烟：18 项 T4 技能空包 `blocked` **18/18 拦截**；Vanilla `1.21.11` 实跑 **`crafter_chain`**（26 步漏斗排空）、**`ride_strider_boost`**（5 步耐久 `-1`）、**`jukebox_meadow`**（2 步草甸放唱片）**3/3 解锁真实游戏进度**（严格标为 `local-target`，不混充官方预算）。
5. **困难服 Sprint 1–4 收敛与 `soak8h_deep` 深度压测（`local-target`，`keepInventory=false`）**：
   - **Sprint 1–4 收敛**：落地 `spawn_shelter.py` / `camp_base.py` / `camp_plan.py` 出生点防爆屋与三区仓库、`mine_shaft.py` 共享持久化主矿井及 `52` 项 `requires_landmarks` 先验静音门控后，30m 死亡从 **`9 → 1`（约降 89%）**、未探明地标空唤醒从 **`28 → 0`**、20m LLM 仅耗 **`28,991` tokens**（外推 8h 占官方输入额度 `8.5%`）；
   - **`soak8h_deep` 连续 31 段（`seg-01..seg-31`）**：闭环验证 `busy:dig` 双端锁自愈（桥接可取消 `dig` + 客户端连续 3 次未释放自动断线重连）、`bot.consume()` 超时映射 `timeout`、水坑/隐形边界/干地 1×1 石穴三类几何陷阱脱困，及 `c68cbc6` 破 30 分五项进度根因修复。

---

## Step 6 · 失败处理与真实复盘（1.5 分钟）

展示**三类真机故障恢复证据**：

1. **三项官方真机故障演练（`results/probe/fault-drills-official-20261119.json`，3/3 全通过）**：
   - **断线重连**：断线后 `/status` 返回 `not_connected`，`63.11s` 守约重连，`/events` `seq` 跨重连连续不丢事件；
   - **65s `safe_mode` 保护**：进入安全模式后持续避险 `65.2s` 自动解除并恢复 `gather_wood`，同步采集真实 `death_loop` 事件结构；
   - **3 线程并发 401 单飞刷新**：双检锁确保 3 线程同时 401 仅触发 **`login_calls=1`**（`428.7ms`），杜绝并发互踢 Token。
2. **官方 2h 长跑暴露的 4 环死锁与 Token 烧爆闭环修复（`c03c26e`）**：
   - **现场问题**：2h 长跑后段因地下采石未记录回地表轨迹 + `CraftGear` 未复用 7 格外已知工作台 + `ScoutIsland` 地下无净空 502 次 `Replan` + 战术无技能每 5s 回落旧 LLM 菜单，导致输入 Token 烧至 `2.91M`（8h 外推 `166.2%` 超额）。
   - **离线回放复现**：用 `INC-005`（`results/replay-sample/`）离线确定性复现 `craft_gear(furnace)` 缺木失败现场（2/2 磁带命中）。
   - **根因修复**：`CraftGear` 复用 `WorldResourceMemo` 工作台 + `MINE_TRAILS` 地下回地表 + `ScoutIsland` 安全上挖 1×2 阶梯 + 战术兜底 LLM 指数退避（`20s→240s`）+ `LLM_TOKEN_BUDGET=7500000` 软熔断（与 `100k RPS` 改造待新一轮官方对比复验）。
3. **`soak8h_deep` 连续压测暴露的永久锁与几何陷阱闭环修复（`local-target`，`seg-01..seg-31` + `c68cbc6`）**：
   - 双端根治 `busy:dig` 永久动作锁死锁（联调桥接可取消 `dig` + 客户端连续 3 次未释放自动断线重连），`bot.consume()` 超时统一映射为 `timeout`；
   - 攻克水坑、隐形边界与干地 1×1 石穴三类几何陷阱脱困，并在 `c68cbc6` 完成破 30 分五项进度根因修复。

---

## Step 7 · 下一步（0.5 分钟）

1. **官方对比复验与真机逐项晋级**：`feat/arch-converge-v2-agy`（268 commits）已合入 `main@d9e3f5c`；下一步在官方模拟服复验 `c03c26e` 四环死锁修复与 `100,000 RPS` / 20 Hz 安全看护升级效果，并遵循 `AGENTS.md` §2A.6 与 WIP=1 纪律按 ROI 顺序逐项验通 **81 项** `verified="offline"` 技能与 **102 项** `draft` 目标（`smelt_iron` → `mine_diamond`/`shiny_gear` → `enter_the_nether` → 堡垒/要塞/试炼密室/末地冲刺与 T4 精选链），每项留日志后晋级 `live` / `graduated`。
2. **赛前 T-0 收口**：切 `.env.match`、停 `8787` 网关、固定 `LLM_MAX_CONCURRENCY=2`、`LLM_TOKEN_BUDGET=7500000`，以 `preflight --apply --live` 全绿开赛。
