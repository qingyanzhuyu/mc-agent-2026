# `mcagent.loop` 与 `tools/test-island` 全流程操作记录与验收报告

- **日期**：2026-11-14（UTC+8）/ 2026-11-13（EST）
- **开发分支**：`feat/loop-soak-agy`（基线提交 `main@95993f9`，保留人工闸门未合入 `main`）
- **验证分级**：框架与异常故障注入 `verified: offline`（120 项单测全绿）；主循环、HTTP 录制回放、LLM 遥测及首批技能经过本地 `1.21.11` 靶场 5 轮 HTTP API 闭环实测（统一标注 `source="local-target"`，不写入官方预算表）

---

## 一、六项评审建议落实对照表

| 编号 | 评审修订要求 | 落地模块与实现方式 | 验收结果 |
|---|---|---|---|
| **1** | **测试循环即比赛主循环**，Supervisor 内置，Auditor/Recorder 以 `--soak` 观察者挂载；重生后阶段推导放在规划层 | `engine.py`（`MainLoop`）统一承载登录就绪、单步推进、死亡/断线/冷却恢复；`auditor.py`（`SoakObserver`）通过钩子挂载；`policy.py`（`RulePlanner`）从刷新后的真实 `State` 背包实时推导阶段（天然兼容 `keepInventory=true/false`） | 通过（离线双模式死亡注入 + 真机多轮实测） |
| **2** | **收敛范围与开关**：先写首批 L3 技能 + 主循环 + 最小高信噪比 Recorder；只留 `campaign` 和 `focus`；默认规则队列，`--llm` 统计延迟与非法输出 | `starter_skills.py` 实现 5 个自注册 L3 技能（`eat_food`、`scout_island`、`gather_wood`、`craft_gear`、`mine_stone`）；`policy.py` 仅保留 `campaign`/`focus`，`--llm` 记录耗时分位与校验错误并零等待回退规则队列 | 通过（真机验证 `campaign`、`focus`、`--llm` 三条链路） |
| **3** | **测试环境贴近虚空岛且物理隔离**：RCON 虚空岛裁切、`keepInventory` 双模式切换、搭场景与快照重置放 `tools/`，`code/mcagent/` 零导入 | 新增仓库根 `tools/test-island/rcon_ctl.py`（纯标准库 RCON 客户端、分块 `<=32768` 虚空护城河裁切、`keepInventory` 切换、`empty_day`/`night_combat`/`low_hp_hunger`/`stone_tier` 场景与玩家重置）；AST 静态扫描单测锁死物理隔离 | 通过（AST 扫描 0 违规 + RCON 指令体积单测） |
| **4** | **预期核对收敛防噪声 + 主目标曲线**：技能自声明 `verify_done`，动作级零额外 RTT，分清 `bug`/`strategy`/`env_variance`，重生不调 `/posture`，记录进度曲线 | `SoakObserver` 优先调用 `skill.verify_done(state, before_counts)`；单次 `Replan` 仅记入环境波动，连续震荡（默认 `>=3`）升级 `REPLAN_OSCILLATION`；死前潜行重生不发 `/posture` 且不误报 `POSTURE_LEAK`；落盘 `progress_curve.jsonl` 支持 `--baseline` 对比 | 通过（真机首跑零误报并精准抓出真实策略震荡单 `INC-001`） |
| **5** | **HTTP 层录制回放替代自动生成 GridWorld 夹具**：脱敏录制请求/响应，附 `git commit` + 运行配置 + 快照号，支持离线确定性重放 | `tape.py` 实现 `TapeRecorder` / `TapeClient`（剥离密码、Bearer Token、API Key，区分 `main`/`monitor` 线程）与 `ReplayClient` / `--replay <INC.json>` 断网重放 | 通过（真机 177 条 HTTP 磁带零敏感字段，`--replay` 离线 `matched_original_outcome=true`） |
| **6** | **保留人工合入闸门**：自动测试与分支提交全自动，不直接合入 `main` | 全程在 `feat/loop-soak-agy` 分支开发、回归与提交，零改动冻结底座（`client.py`、`runtime.py`、`planner/*`、`skills/*`），等待人工审阅后再合入 `main` | 通过 |

---

## 二、本轮完整操作流水记录

### 阶段 1：分支创建与子系统实现

1. 从干净的 `main`（`95993f9`）切出独立特性分支：`git checkout -b feat/loop-soak-agy`。
2. 在不改动任何现有冻结文件的前提下，新建子目录 `code/mcagent/loop/`（7 个模块 + 文档）与 `tools/test-island/`（RCON 场景工具 + 文档）：
   - `code/mcagent/loop/__init__.py`：包导出与触发 `starter_skills` 自注册；
   - `code/mcagent/loop/tape.py`：HTTP 请求/响应脱敏录制器 `TapeClient` 与离线回放客户端 `ReplayClient`；
   - `code/mcagent/loop/starter_skills.py`：5 个符合 `AGENTS.md` §9 契约的 L3 技能（每步单阻塞动作、无坐标/实体 ID 参数、自带 `verify_done` 后置断言、动态记忆保护区与已放置工作台）；
   - `code/mcagent/loop/policy.py`：`RulePlanner` 状态派生规则队列与 `CampaignPlanner`（`campaign` / `focus` + 可选 `--llm` 遥测与兜底）；
   - `code/mcagent/loop/auditor.py`：`SoakObserver` 低噪声异常归档（`bug` / `strategy` / `env_variance`）、`progress_curve.jsonl` 计分曲线与 `--baseline` 对比报告生成；
   - `code/mcagent/loop/engine.py`：`MainLoop` 正式赛与挂机合一主循环（含死亡重生、断线 `>=30s` 重连、`death_loop_cooldown` 冷却自愈并顺延超时窗口）；
   - `code/mcagent/loop/__main__.py`：CLI 入口（支持 `--dry`、`--local-target`、`--live`、`--soak`、`--mode`、`--llm`、`--replay`、`--baseline`、`--user`、`--disconnect-on-exit` 等）；
   - `tools/test-island/rcon_ctl.py`：纯标准库 RCON 客户端、虚空岛裁切、`keepInventory` 切换与场景重置工具。

### 阶段 2：离线单测与子代理并行边界审计

1. 编写集成单测 `code/tests/test_loop_soak.py`（涵盖 5 技能一步一动作契约、`keepInventory=true/false` 死亡恢复及不调 `/posture`、三分类降噪去重、HTTP 磁带脱敏离线回放、`--llm` 非法输出回退、`mcagent` 与 `tools` AST 物理隔离）。
2. 调度子代理（Edge-Case Test & Audit Engineer）并行编写 `code/tests/test_loop_edge_cases.py`（28 项故障注入单测：60s 双死 `safe_mode` 驻留、`not_connected` 与 `death_loop_cooldown` P0 立档、死因分类、5 步微抖动卡死检测、零 RTT 动作级告警、`ReplayClient` 耗尽与路径不匹配、CLI 安全护栏、RCON 单块 `<=32768` 体积上限），并审计发现 `engine.py` 在真实睡眠 `302s` 冷却后需顺延 `deadline` 的边界隐患，已同步修补。

### 阶段 3：本地联调网关（`1.21.11`）5 轮真实 HTTP API 实测与迭代修复

> **环境与安全隔离说明**：
> - 所有实测均附加 `--local-target`，强制校验目标网关为本机回环地址 `http://127.0.0.1:8787/api/v1`（`tools/local-api-bridge`，后端连接队内测试服 `frp-net.com:64463`，Minecraft `1.21.11`），**未触碰官方比赛服**。
> - 控制台输出的 `[ready] 已进比赛服` 来自仓库冻结底座 `code/mcagent/client.py:489` 在 `status()["ready"] == True` 时的固定提示字符串（用于区分大厅 `ready=false` 与子服 `ready=true`）。
> - 全程通过标准 HTTP REST API（`/auth/login`、`/bot/connect`、`/status`、`/inventory`、`/events`、`/advancements`、`/find_blocks`、`/get_block`、`/move_to`、`/dig`、`/craft`、`/place`、`/equip`、`/bot/disconnect`）驱动独立离线测试角色 `agy_soak_t1` / `agy_soak_t2` / `agy_soak_t3`（内存态 Token），**未抢占 `de7_bot`，未修改 `code/mcagent/.token.json`**，每轮结束均自动 `--disconnect-on-exit` 并经 `curl http://127.0.0.1:8787/healthz` 确认为 `ready: false`。

#### 实测第 1 轮（子代理首跑全模式烟测，`bot=agy_soak_t1`）
1. **干跑探测**：`uv run python -m mcagent.loop --dry` → 退出码 `0`，自注册 5 技能齐全，首拍规则决策 `<gather_wood:acq:wood>`，无凭据探测返回 `unauthorized`。
2. **`campaign` 首跑**：`--mode campaign --soak --local-target --live --user agy_soak_t1 --max-steps 18 --max-seconds 90 --disconnect-on-exit`
   - 出生点 `[-43.5, 63, 89.5]`（`forest`），砍下 2 块 `birch_log` 并合出 8 块 `birch_planks`。
   - **挂机审计器自动立档真实问题 `INC-001-P2-replan_oscillation-scout_island.json`**：
     - 根因 A：白桦树高树冠原木（高于脚底 `>4` 格且底干已砍空）使地面 `GoalNear(rng=3)` 报 `path_not_found`，中断第 3 块原木采集；
     - 根因 B：旧版 `RulePlanner` 在未放置过工作台时尝试 `s_reuse` 合木镐，把 2 原木转成 8 木板后发现缺工作台（共需 9 木板）触发 `Replan`；
     - 根因 C：`scout_island` 调 `find_blocks(limit=40)` 在森林草地被脚边 `<5` 格草方块占满，过滤 `dist >= 6` 后候选为空，连续触发震荡被 `SoakObserver` 准确捕获立档。
   - **脱敏与回放核验**：`http_tape.jsonl` 录得 177 条真实 HTTP 往返，全文正则扫描 `password`/`Bearer`/`token` 泄露数为 `0`；执行 `uv run python -m mcagent.loop --replay .../INC-001-...json` 断网重放，退出码 `0`，`matched_original_outcome: true`。
3. **`focus` 模式 + 基线对比**：`--mode focus --skill scout_island --skill-args '{"radius":24}' --baseline <campaign_dir> ...` → 仅派发 `scout_island` 并在 `summary.md` 输出基线对比。
4. **`--llm` 模型规划模式**：`--mode campaign --llm --soak --local-target --live --user agy_soak_t1 --max-steps 4 --max-seconds 50 --disconnect-on-exit`
   - 真实调用 `.env` 配置的 `Qwen3.6-Flash`，耗时 `1118.6ms` 合法选出 `gather_wood({"count": 8})` 并通过契约校验执行，`timeline.jsonl` 完整记录 `llm_call` 延迟遥测。

#### 实测第 2 轮（修补 `INC-001` 三处根因后复验，`bot=agy_soak_t2`）
- **针对性修复**：
  1. `scout_island` 将 `find_blocks` 的 `limit` 提升至 `250`（覆盖近处草块之外的远端安全陆地）；
  2. `gather_wood` 跳过头顶悬空高树冠原木（水平 `<3` 格且 `dy > 4` 格）、优先同树种防杂木木板碎片化、对单点不可达树拉黑换树；
  3. `starter_skills.py` 记录已放置/已观测的 `KNOWN_TABLES`，`RulePlanner` 在周边无已知工作台时要求凑满 9 木板才合木镐。
- **执行命令**：`uv run python -m mcagent.loop --mode campaign --soak --local-target --live --user agy_soak_t2 --world-snapshot live-verify-v2 --baseline <v1> --max-steps 35 --max-seconds 160 --disconnect-on-exit`
- **结果与新发现**：
  - 出生 `[-37.5, 63, 83.5]`，**仅用 `29.2s` 零报错一次走完“砍 3 块白桦原木 → 合 12 木板 → 合 4 木棍 → 合工作台并自动潜行放置 → 合成木镐”全链路**，`INC-001` 彻底消失（0 bug，0 strategy 震荡）。
  - 随后 `mine_stone` 暴露出真实地形第 4 处物理细节：`find_blocks(types=["stone"], radius=24)` 扫到了脚下 `y=58..60` 地下洞穴里的暴露石头，角色站在 `y=63` 崖顶挖开 `[-41, 60, 71]` 与 `[-40, 60, 75]` 两块石头（木镐耐久 `59 -> 57`），但掉落圆石落入洞底无法拾取（`gained: {}`），其余洞穴点寻路卡满 8s 超时。

#### 实测第 3 轮（加入平层过滤与防挖穿阶梯探槽复验，`bot=agy_soak_t3`）
- **针对性修复**：`mine_stone` 限定平层暴露石高差在 `[-1.5, +2.5]` 内（若挖开后未捡到圆石则拉黑崖边邻域），地表无平层裸石时自动向非保护区、非工作台方向开凿 3 格净空的阶梯式下行探槽（每步先 `get_block` 核对底板非空气/岩浆/水防挖穿虚空）。
- **执行命令**：`uv run python -m mcagent.loop --mode campaign --soak --local-target --live --user agy_soak_t3 --world-snapshot live-verify-v3 --baseline <v2> --max-steps 35 --max-seconds 110 --disconnect-on-exit`
- **结果**：
  - `29.6s` 再次零失误完成橡木采集 + 工作台放置 + 木镐合成；
  - `scout_island` 连续 **5 次 100% 成功外迁探索**（`[-66,63,90] -> [-71,63,87] -> [-68,63,80] -> [-74,63,75] -> [-66,62,65] -> [-77,63,64]`）；
  - `mine_stone` 阶梯探槽成功避开地下盲洞，从 `y=63` 沿斜阶梯安全下行至 `y=59` 并平稳走回地面 `y=63`（全程 `hp=20`、0 死亡、0 bug、0 strategy 异常）；针对森林 4–5 格厚表土层将阶梯最大深度从 `4` 放宽至 `6` 并避开工作台方向。

---

## 三、最终离线门禁与环境清理核对

1. `uv run python -m compileall -q mcagent tests`：`0` 错误。
2. `uv run python -m unittest discover -s tests`：**`Ran 120 tests in 11.399s — OK`**（原仓库 83 项 + `test_loop_soak.py` 9 项含 `INC-001` 回归 + `test_loop_edge_cases.py` 28 项）。
3. `uv run python -m mcagent.smoke --dry` 与 `uv run python -m mcagent.loop --dry`：均退出码 `0`。
4. `curl -s http://127.0.0.1:8787/healthz`：`de7_bot`、`agy_soak_t1`、`agy_soak_t2`、`agy_soak_t3` 全部 `ready: false`。
5. 临时目录 `/tmp/agy-soak-test` 已清理，仓库工作区无任何遗留日志、临时文件或凭据。

---

## 四、后续推进与人工合入说明

1. 当前所有代码与文档均已提交在分支 `feat/loop-soak-agy`，各 L3 技能按规范保持 `verified = "offline"`，等待人工审阅确认后再合入 `main`。
2. 自建测试服若开启 RCON，可直接运行 `python3 tools/test-island/rcon_ctl.py carve-void` 与 `scenario --name night_combat` 等指令做虚空边界与 `keepInventory=true/false` 专项回归。
3. 下一步按 `docs/11-任务性价比分层.md` 往 `skills/` 扩展铁器冶炼（`smelt_iron`）、盾牌与夜间自卫、打火石/水桶等 L3 技能，无需改动 `mcagent.loop` 主框架即可自动接入挂机回归与正式赛循环。
