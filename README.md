# 南大 AI Agent for Minecraft 开发大赛 · Harness 赛道备赛仓库

> 状态：**已合入 `main@d9e3f5c`（`feat/arch-converge-v2-agy` 268 commits 已并入主线；85 个注册 L3 技能：`4 live` + `81 offline`；109 项 `GoalSpec` 台账：94/99 计分项 / 2,925 分上限 [86.4%]；另附 `local-target` Sprint 1–4 与 `soak8h_deep` `seg-01..seg-31` 压测）** · 更新 2026-11-22
> 初赛：**11/22（日）10:00–18:00，8 小时，每人一座虚空岛（v2 草案）** · 决赛：12/6
> 出线：初赛取 8 队 · 报名截止：**11/18**

这个仓库放三样东西：**我们认定的事实**、**我们的判断（可被推翻）**、**还没搞清的问题**。
所有结论都标了来源。改任何一条，请附来源。

---

## 三十秒版本

- 赛道：自建 harness 控制一个 Minecraft bot 自主生存 8 小时，按原版 advancement 计分（60）+ 工程评审（40）。
- 模型：公告写"统一 DeepSeek V4.1 Flash"，群里已改口为 **qwen3.6-35b-a3b 无限额度**，d4.1f 额度有限，正式用哪个**还未定**（代码层统一配置为 `LLM_MODEL=default` 并通过环境变量切换，见 J13；2026-11-19 官方 AIA 实测返回 `qwen3.6-35b-a3b`）。
- 接口：官方双端点——校园网直连入口 `https://apiin.nmo.net.cn/api/v1`（2026-11-19 官方校园网实测 `T_status` 稳态 `7.34 ms`、`T_move20=3648 ms`、`T_dig1=2146 ms`）与公网备用入口 `https://mc-api.windywindie.top/api/v1`（2026-11-19 无鉴权探活 `816–973 ms`，返 `401 unauthorized`）；以上游 [v1.3.11 规范](docs/99-原始记录/api-spec-v1.3.11.md)为协议参考，已关闭 [B18/C1](docs/04-待确认.md) 官方标定项。

## 我们目前最重要的五条判断

| # | 判断 | 为什么重要 | 依据 |
|---|---|---|---|
| J1 | **动作互斥且同步阻塞**，所以并发只能用来"想"，不能用来"做" | 决定了整个架构：不是多 Agent 并行，而是"动作期间在另一个线程里算下一步 + 后台监控紧急抢占" | [接口 §0/§4](docs/02-接口与架构.md) |
| J14 / J19 | **分值只由难度决定（深度已取消）；明确放弃 5 项高脆度/极低性价比目标（合计 460 分）** | 先把约 290 分的底仓做稳，再按岛的实际情况叠加，长链必须带止损 | [性价比分层](docs/11-任务性价比分层.md) |
| J3 | **死亡循环（60s 死 3 次 → 下线 5 分钟）是唯一的一票否决** | 8 小时一次机会，触发两次就丢 10 分钟 + 全部掉落 | [规范 §7](docs/02-接口与架构.md) |
| J11 | **基础生存靠确定性技能推进，LLM 负责选目标与异常诊断** | 规避 LLM 30–118s 尾延迟与单步幻觉，模型抖动时仍能继续保活发育 | [关键判断 J8/J11](docs/01-关键判断.md) |
| J5 | **40 分工程评审是最稳的分数**，靠可复现证据与纪律拿分 | 很多队"跑起来了但讲不清"；日志/故障注入单测/结构清晰度是我们主动可控的证据 | [计划 §4](plan/11-12到11-22执行计划.md) |

完整清单见 [docs/01-关键判断.md](docs/01-关键判断.md)（J1–J20，其中 J2 已作废；每条都写了"什么情况会推翻它"）。

## 目录

```
AGENTS.md            多 agent 协作硬规则（文件所有权 / 协作禁令与赛事红线 / 分级验证 / DoD）—— 开工前必读
skill/SKILL.md       仓库使用手册（定位 / 任务路由 / 五个坑 / DoD）—— 同上，先读这个
skill/live-experiment/SKILL.md  真机实验流程（同步 → 起网关 → smoke → probe）
pyproject.toml       Python 依赖声明（配合 uv.lock 锁定可复现测试环境）
report.md            40 分工程评审技术报告正文（整合 J1–J20、架构、官方基线与迭代案例）
report.pdf           由 report.md 导出的可提交工程技术报告 PDF
demo.md              11/22 线下交流日与阶段模拟展示七步脚本（含断网兜底回放命令）
results/             脱敏官方实测证据、2h 长跑曲线、离线 --replay 异常包与阶段二 local-target 冒烟记录
docs/
  00-赛事事实.md      客观条款（公告 + 细则 + 群聊补充），带来源
  01-关键判断.md      J1–J20，每条可被证伪
  02-接口与架构.md    API 能力地图 + 三层架构 + 四条工程红线
  03-策略推论.md      旧计分公式下的推论（前三节已作废）+ §六 官方校园网基线与长跑曲线（靶场数据 ≠ 比赛预算）
  04-待确认.md        checkbox 清单，认领制
  05-分工与契约.md    角色表 / 账号表（已脱敏占位）/ 模块冻结面
  06-工作日志.md      每次改动追加一条（含为什么、验证到哪一步）
  07-评审材料.md      40 分评审怎么攒：评分项 → 证据 → 现状（含五部分材料包映射）
  08-计分建模与推进顺序.md  v2 草案计分表：分池、分档、不计分项
  09-进度树与判据表.md  125 项进度树 + 官方判据 + capability 归并（脚本生成）
  10-推进批次.md      99 个计分项聚成 22 批（按共享前置）+ 岛体检结构指纹表
  11-任务性价比分层.md  99 项分四层：底仓 / 有就做 / 长链带止损 / 放弃
  13-得分目标脚本与子目标规划.md  99 项拆成子目标与公共脚本 + 新旧技能对照 + 岛体检 + 成本与时间线
  14-T4精选扩展与子目标规划.md    Stage 3 1.21.11×API 交叉审计、Wave A–D 18 项 T4 精选扩展与 10 项共享 T4_PREP 子图
  99-原始记录/        v1/v2/v3 原始分析 + 官方接口规范原文
plan/
  11-12到11-22执行计划.md
  赛前切换清单.md    T-7 / T-1 / T-0 与 11/22 线下交流日切换清单
code/
  mcagent/
    client.py        HTTP 客户端（401 单点刷新 + .token.json 复用 + 100,000 RPS 无频控零排队快路径/429 retry_after_s 退避//stop 优先 + trust_env=False + 32 路连接池 + 8 线程并发只读 get_blocks_batch + busy:dig 连续 3 次未释放断线重连，冻结面）
    runtime.py       State / Safety / 连续异步 SafetyMonitor（风险态 20 Hz 0.05s / 安全态 0.15s，地下无盾苦力怕 <7.5m 与残血远程 14m 预警）/ Executor（可抢占锁 + 非幂等判重 + 有界 move_to 超时禁重试 + v1.3.11 精选恢复，冻结面）
    preflight.py     赛前切换自检（12 项静态门禁：官方端点 / 凭据 / 2 路并发闸门 / 8787 网关 / 残留 soak_ctl·mcagent 进程 / apiin 误配 7897 代理拦截）
    smoke.py probe.py 冒烟与基线测量（含 --api-capabilities / --pathfinding）
    planner/         决策层：objective.py（目标级战略校验）、skill_choice.py、decision.py、llm.py（2 路 BoundedSemaphore 并发闸门）、scheduler.py
    skills/          执行层：L2 原语（voxel_cache / reach / escape / nav / landmark_nav / mine_shaft / spawn_shelter / camp_base / camp_plan / sightline 3D DDA / furnace_lease 等）、world_memo.py（21 类结构指纹 + 4 类群系方块指纹 + 32×32 访问网格 + 虚空岛穷尽停探 + 原子 JSON 检查点）、85 个注册 L3 技能（4 项 verified="live" + 81 项 verified="offline"，含 emergency_escape、阶段三 18 项 T4 技能及新增 brush_archaeology / hatch_and_raise_sniffer / collect_froglights）
    loop/            主循环与策略：engine.py（同格冷却自旋三级升级 + 5s 或位移 ≥8 格沿路增量扫描与零成本群系被动采样）、goals.py（109 项 GoalSpec 台账：覆盖 94/99 计分项 / 2,925 分上限 [86.4%]，6 graduated / 1 candidate (smelt_iron) / 102 draft，52 项地标先验静默门控 requires_landmarks，明确放弃 5 项合计 460 分）、subgoals.py（10 项活谓词 + 10 类工作站）、strategy.py & strategy_async.py（--strategy llm）、context.py、auditor.py、tape.py
  tests/             离线故障注入、架构契约与集成回归单测：1,789 项 Python unittest（0 失败 0 跳过，verified: offline）
tools/
  local-api-bridge/  赛前本地/局域网 v1.3.1 契约联调网关（连接 1.21.11 原版测试服，含 16 项 Node 网关契约单测，支持可取消 dig 根治 busy:dig 永久锁与 bot.consume() 超时映射 timeout，与比赛代码零耦合）
```

## 怎么参与

0. **先读 [AGENTS.md](AGENTS.md)**——多个 DSH 实例同时改这个仓库，文件所有权、官方合规红线、登录占位、分级验证与 DoD 都在里面。违反会被整体回退。
1. **认领问题**：打开 [docs/04-待确认.md](docs/04-待确认.md)，把 `[ ]` 改成 `[x] @你的名字`，然后去问 / 去试。
2. **改事实**：`docs/00` 是我们的共识底座，改动必须附来源（链接、截图、会议纪要）。
3. **提判断**：新判断写进 `docs/01`，编号顺延，**必须写"什么情况会推翻它"**——写不出就别写。
4. **改代码**：先读 [code/mcagent/README.md](code/mcagent/README.md) 的三条约束，跑通 `cd code && uv run python -m unittest discover -s tests && uv run python -m mcagent.smoke --dry && uv run python -m mcagent.preflight`。
5. **别提交密钥**：`.env`、`.token.json`、`sk-...`、皮肤站密码，一律不进仓库。

## 当前进度

- [x] 抓全公告 + 初赛细则 + 计分/判据表整理（`docs/00`、`docs/08`–`docs/13`）
- [x] 读完接口规范 v1.3.1–v1.3.11，探通官方双端点无鉴权（2026-11-19 实测：校园网 `apiin.nmo.net.cn` 长连接 RTT `4.0–4.3 ms`，公网 `mc-api.windywindie.top` `816–973 ms`，均返 `401 unauthorized`）
- [x] 底层容错与运行时加固（401 单点重登与 Token 持久化、`100,000 RPS` 无频控零排队快路径 + `/stop` 急停优先 + HTTP 429 `retry_after_s` 退避 + `trust_env=False`、`32` 路连接池、`8` 线程并发只读 `get_blocks_batch`、风险态 `20 Hz (0.05s)` / 安全态 `0.15s` 连续异步 `SafetyMonitor`、双端根治 `busy:dig` 永久动作锁死锁、`bot.consume()` 超时映射为 `timeout`、死亡去重与 65s 避险锁存、非幂等超时判重、有界 `move_to` 超时禁重试、`emergency()` 紧急抢占、移植 `v1.3.11` 精选恢复 4 项）
- [x] `feat/arch-converge-v2-agy`（268 commits）已合入 `main@d9e3f5c`（**85 个注册 L3 技能**：`4` 项 `verified="live"` + `81` 项 `verified="offline"`，新增 `brush_archaeology`、`hatch_and_raise_sniffer`、`collect_froglights`；**109 项 `GoalSpec` 台账**：覆盖 `94 / 99` 计分项 / **2,925 分**上限 [86.4%]，`6 graduated / 1 candidate (smelt_iron) / 102 draft`，**52 项** `requires_landmarks` 地标先验静默门控，明确放弃 5 项共 460 分；`world_memo.py` 21 类结构指纹 + 4 类群系方块指纹 + 32×32 访问网格 + 虚空岛穷尽停探、5s/位移 ≥8 格沿路扫描 + 零成本群系被动采样、`landmark_nav.py` / `mine_shaft.py` / `spawn_shelter.py` / `camp_base.py` / `camp_plan.py` / `furnace_lease.py` / `sightline.py`）+ **1,789 项 Python `unittest` + 16 项 Node 网关契约测试全绿（0 失败 0 跳过）**
- [x] 本地靶场 `local-target` 冷启动、阶段二/三冒烟、Sprint 1–4 与 `soak8h_deep` 压测（Sprint 1–4 将 30m 死亡从 `9 → 1` [约降 89%]、未探明地标空唤醒 `28 → 0`、20m LLM 耗 `28,991` tokens [外推 8h 占官方输入额度 8.5%]；随后 `soak8h_deep` 连续 31 段 `seg-01..seg-31` 闭环双端锁自愈、水坑/隐形边界/干地 1×1 石穴三类几何陷阱脱困及 `c68cbc6` 破 30 分五项进度根因修复；阶段三 `T4-P1..P4` 标定及 18 项空包 `blocked` 拦截 + `crafter_chain`/`ride_strider_boost`/`jukebox_meadow` 3/3 解锁真实进度；**均不进入正式比赛时间预算表**）
- [x] **官方正式皮肤站账号登录 + `ready=True`**（校园网 `apiin.nmo.net.cn` `preflight --apply --live` 15/15 全绿、`smoke` `0.5s` 登录进服就绪，Token TTL 24h）
- [x] **官方校园网基线 `probe` + 能力标定 + AIA 实测**（`T_status` 稳态 `7.34 ms`、`T_move20=3648 ms`、`T_dig1=2146 ms`、11/19 历史阶梯 `MC_MAX_RPS` 4/8/12/16 零限流、C16 虚空保护误报 `0/12`、官方 AIA `qwen3.6-35b-a3b` 快路径 `4/4` `p50=1652.7 ms`，已关闭 `docs/04` B18/C1 等 10 项待确认项，数据归档 `results/probe/`）
- [x] **官方真机故障演练 3/3 PASS**（断线重连 `63.11s`、`safe_mode` `65.2s` 冷却自愈、3 线程并发 401 单飞刷新 `login_calls=1`，归档 `results/probe/fault-drills-official-20261119.json`）
- [x] **官方 2 小时长跑取证 + 根因修复 + 评审五部分材料冻结**（`official-soak-20261119` 有效 `6303.9s` **0 死亡 0 崩溃**、`emergency_escape` 6/6，前 `124.7s` 拿下 5 项起步目标；4 项零缺陷技能升格 `verified="live"`；暴露的 4 环死锁与 Token 烧爆已在 `c03c26e` 闭环修复；`INC-005` 2/2 离线确定性 `--replay`；`report.md`、`report.pdf`、`demo.md`、`results/` 已脱敏汇编就绪）
- [ ] **阶段二/三及新增技能真机晋级 + 官方复验 + 赛前收尾**（阶段二/三及未实测技能共 **81 项仍保持 `verified="offline"`**、台账 **102 项**仍为 `draft`，`c03c26e` 四环死锁修复与 `100k RPS` 改造待新一轮官方对比复验；另待 11/22 演示备用录屏、跟进官方提交入口 `docs/04` A3 与 `docs/05` 角色表人类签字）

## 已知的两个坑

1. **本机注册表挂了死代理 `127.0.0.1:7897`**，`requests` 默认会读它 → 报 `ProxyError`，看着像断网。代码里必须保持 `trust_env=False`。
2. **校园网直连端点与 LLM 端点的网络路径不同**：校园网 MC 入口 `apiin.nmo.net.cn` 直连长连接 RTT 实测 `4.0–4.3 ms`（公网备用 `mc-api.windywindie.top` 约 `816–973 ms`），**严禁给 `apiin.nmo.net.cn` 误配 `7897` 代理**（`code/mcagent/preflight.py` 已内置拦截）；而 `api.nju-aia.com` 走代理与直连表现不同（见 `planner/llm.py` 双线路降级）。别一刀切开/关 VPN。
