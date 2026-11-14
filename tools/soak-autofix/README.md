# `tools/soak-autofix` — 双工位持续挂机压测与 AI 自动修单热接力框架

本工具将 **工位 A（Runner 持续挂机压测）** 与 **工位 B（Fixer AI 隔离修单 + 四道机器门禁）** 编排在同一套双 `git worktree` 中，所有通过验证的修复统一汇入**同一个独立待审分支**（默认 `feat/soak-autofix`，严禁为 `main`），跑完后由人查看 `REVIEW_REPORT.md` 一次性验收合并。

> **物理隔离声明**：本目录位于 `tools/soak-autofix/`，`code/mcagent/` 对本目录零导入、零依赖。

## 架构与数据流

```
                 refs/heads/feat/soak-autofix (单一待审分支，不碰 main)
                        ▲                              │
         Gate 4: 验证通过才 commit              轮次边界 checkout --detach
                        │                              ▼
┌───────────────────────────────────────┐    ┌───────────────────────────────────────┐
│ 工位 B：worktrees/fixer (检出分支)     │    │ 工位 A：worktrees/runner (游离 HEAD)   │
│ Bot: <用户名>_fixer (--mode focus)    │    │ Bot: <用户名>_soak (--mode campaign)   │
│                                       │    │                                       │
│ 1. claim 认领最高优 INC-*.json         │◀───┼─ 产出异常包 + progress_curve.jsonl     │
│ 2. AI 修复（Codex/Claude/AGY 子代理）  │    │  若新 commit 诱发新 P0 bug → 自动 revert│
│ 3. 四道机器闸门独立核验：               │    └───────────────────────────────────────┘
│    - Gate 1: 文件白名单 + 密钥正则扫描 │       (支持 --goal 聚焦单目标晋级 + 实测遥测回写)
│      (GOAL_MISS 自动收窄至目标专属文件) │
│    - Gate 2: compileall + 120+ 单测   │
│    - Gate 3: 真机 focus/goal 金丝雀    │
│    - Gate 4: 提交至待审分支 + 防退化回滚│
└───────────────────────────────────────┘
```

## 单目标晋级纪律与遥测隔离（配合 `mcagent.loop.goals`）

- **单目标聚焦与止损（`--goal <id> --max-goal-attempts 4`）**：在 `init`、`runner-loop` 或 `run` 中指定 `--goal <goal_id>`，Runner 自动透传 `--goal` 仅激活该目标及其传递前置链；若目标未达成并产出 `GOAL_MISS:<goal_id>` 异常单，最多允许自动修复 `--max-goal-attempts`（默认 `4`）轮，超限自动升级 `escalated_human` 并在持续挂机模式下写入 `STOP` 止损退出，防止单目标死磕耗尽时间与额度。
- **`GOAL_MISS` 动态收窄白名单（Gate 1）**：针对 `GOAL_MISS` 异常单，Gate 1 在冻结文件保护之上进一步将允许修改范围收窄至该目标声明的 `editable_files` 及 `code/tests/test_*.py`（越界触碰 `policy.py` 或其他无关技能直接打回）；Gate 3 金丝雀验收自动使用 `--mode campaign --goal <goal_id>` 冷启动跑通前置；Gate 4 若检测到新提交导致原有已达成前置全部丢失且最高分倒退，自动触发 `git revert`（`prereq_score_regression`）。
- **环境隔离实测成本落盘（`goal_telemetry.<source>.json`）**：每轮非回滚挂机结束后按运行环境 `source` 隔离聚合成就达成率（`hit_rate`）、中位耗时（`p50_elapsed_s`）、中位 Token（`p50_tokens`）与死亡次数，落盘至 `<session-dir>/goal_telemetry.local-target.json`（遵循 `AGENTS.md §4`，绝不混同本地靶场与官方赛服数据），并在后续轮次通过 `--goal-telemetry` 回喂给规划器更新性价比排序。

## 两种使用方式

### 方式零：网页控制台（实时改轮长、看每一轮、一键启停）

**队友上手（每人在自己电脑上开，页面只认本机）：**

1. 环境：Linux 或 Windows 的 **WSL2**（`soak_ctl.py` 用 `fcntl` 加锁、控制台扫 `/proc` 识别守护，原生 Windows / macOS 跑不了）；装好 `uv`、Node.js ≥ 18，`code/mcagent/.env` 配好 LLM key；修单用 `codex` / `agy` / `claude` 中的哪个，就要先装好并登录那个 CLI。
2. 拉最新 `main` 后先起本地网关（另开一个终端，保持运行）：
   ```bash
   cd tools/local-api-bridge && npm ci && npm start     # 看到 listening on http://127.0.0.1:8787/api/v1 即可
   ```
3. 在仓库根目录起控制台：
   ```bash
   python3 tools/soak-autofix/dashboard.py
   ```
4. 浏览器打开 **http://127.0.0.1:8790/** → 左侧「启动参数」确认一下 → 点右上角 **一键启动**。
   - 「进服角色名」默认是 `<你的系统用户名前 8 位>_soak`，**每轮同一个名字进服**，轮次号只记在 `ep-NNNN` 里。共享测试世界里同名会互踢，所以每人保持各自的名字，别改成同一个。
   - 同名意味着上一轮的背包、位置、已拿到的进度会带进下一轮；要每轮空包从头跑（测聚焦目标的真实成功率时），勾「每轮换新角色」，角色名变成 `<名字>001`、`002`…
   - 修单工位的金丝雀验修角色同样按本机区分，默认 `<系统用户名前 8 位>_fixer`。
5. 想在别的机器上看：`ssh -L 8790:127.0.0.1:8790 <挂机那台机器>`，再在自己浏览器开 http://127.0.0.1:8790/ 。

```bash
python3 tools/soak-autofix/dashboard.py --port 8791 --session-dir code/mcagent/logs/soak-night   # 换端口 / 换会话目录
```

- 只监听 `127.0.0.1`，零第三方依赖；校验 `Host` / `Origin` / `Content-Type`，别的网页无法跨站调用。
- **一键启动 / 重启**：按页面「启动参数」拼 `soak_ctl.py run`，以独立进程组后台运行，输出写到会话目录 `dashboard_run.log`；关掉控制台或网页不会停掉守护。同一会话已有守护（包括终端里手动起的）时拒绝重复启动。
- **优雅停止** = 写 `STOP`；**立即中断** = 给 `run` 进程发 SIGINT（等同 Ctrl+C，当前轮不入账）；**重启并应用新代码** = 写 `STOP` → 等两个工位都退出 → 用启动参数重新起。
- **实时参数**：秒数、步数、聚焦目标、工位 A 玩家模型、工位 B 修单模型、单目标最大尝试直接写会话 `state.json`；Runner 下一轮开跑时读取，Fixer 每接一单前读取（正在修的那一单不受影响）。守护若是在「热改轮长」功能之前启动的，页面会提示改轮长不生效、需要重启。
- **当前轮**：解析正在跑的 `timeline.jsonl` / `progress_curve.jsonl`，2 秒刷新用时、步数、正在跑的技能、LLM 延迟、Token、血量和最近事件；**轮次历史**点一行看该轮 `summary.md`。
- **热合入开发分支**：后台调用 `inject_and_set_goal`（同样跑编译 + 全量单测 + `--dry` 门禁），页面显示任务进度与结果。

命令行等价写法：`soak_ctl.py run ... --bot-user <名字>`（默认同上），加 `--rotate-user` 每轮换新角色。

**统一配置：工位 A 进哪个服务器**（一份 `code/mcagent/.env` 同时装两套端点，每次启动时选，挂机中不切）：

| 选项 | 连哪里 | 角色名 | 模型 | 启动前要求 |
|---|---|---|---|---|
| `--server official --official-llm custom`（**默认**，日常训练） | 比赛服官方端点 `mc-api.windywindie.top` | 只能用 `.env` 里的队伍正式账号 `MC_USER`/`MC_PASS`，不能改名、不能轮换 | 自备 key（`LLM_CUSTOM_*`，如并行云 DeepSeek-V4.1-Flash），玩家模型可热切换 | `.env` 有账号与 `LLM_CUSTOM_BASE/KEY`；先在 `docs/05` 账号表占位，再加 `--account-reserved` |
| `--server official --official-llm official`（比赛彩排） | 同上 | 同上 | AIA 官方代理（`LLM_KEY`），与比赛当天完全一致 | `.env` 有 `LLM_KEY`；同样要占位 |
| `--server local`（备选） | `MC_LOCAL_API_BASE`（默认 `127.0.0.1:8787` 本机网关 → 队内测试服） | `--bot-user` 随意起 | 自备 key | 网关已启动 |

- 比赛服 + 自备 key 是**赛前训练专用**：脚本给子进程设 `MCAGENT_TRAIN_ON_OFFICIAL=1`，`mcagent.loop` 只在 `--soak` 且有这个开关时放行；比赛当天 `preflight` 对 `LLM_PROFILE=custom` 与这个开关都会 BLOCK，必须切回官方代理（AGENTS.md §2B.3，公告：比赛统一用 AIA 代理上的 DeepSeek V4.1 Flash）。
- 比赛服的轮次数据标 `official`，不进 `goal_telemetry.local-target.json`；工位 B 的金丝雀验修始终走 `MC_LOCAL_API_BASE`，不占比赛账号。比赛账号同一时刻只能一个 bot 在线（§3），挂机期间别人不能用这个账号。
- 命令行里不写 `--server` 就是比赛服；只想在自建测试服跑，显式加 `--server local`。

**工位 B 修单模型**（`--fixer-preset`，`run` 启动时指定，挂机中用页面或 `set-goal --fixer-preset` 热切换）：

| 写法 | 含义 |
|---|---|
| `codex:gpt-6.1-sol:{low,medium,high,xhigh,max}` | `codex exec -m gpt-6.1-sol -c model_reasoning_effort=...` |
| `codex:gpt-6-astra:{low,medium,high,xhigh,max}` | `codex exec -m gpt-6-astra ...` |
| `agy:gemini-4-hydrogen-{low,medium,high,xhigh}` | `agy --model gemini-4-hydrogen-<档> --effort <档>` |
| `agy:gemini-4-carbon-{low,medium,high}` | `agy --model gemini-4-carbon-<档> --effort <档>`（carbon 没有 xhigh） |
| `codex` / `agy` / `claude` | 旧简写：6.1-sol xhigh / hydrogen high / `claude -p` |

模型 id 与档位来自 2026-11-14 本机 `agy models` 与 `~/.codex/models_cache.json`；codex 的 `ultra` 档会自动派子代理，无人值守修单不收。

```bash
python3 tools/soak-autofix/soak_ctl.py set-goal --session-dir code/mcagent/logs/soak-cont --fixer-preset codex:gpt-6-astra:high
```

### 方式一：一键双进程全自动挂机（外接本机 `codex` / `agy` / `claude` CLI）

内置模型分工（默认）：
- **工位 A（进游戏玩的玩家 `--player-model`）**：默认 `DeepSeek-V4.1-Flash`（自动注入子进程 `LLM_CUSTOM_MODEL`，无需改 `.env`；也可传 `--player-model DeepSeek-V4-Pro-0813` 或 `Qwen3.6-Flash`）
- **工位 B（改代码的修单入口 `--fixer-preset`）**：
  - `codex`（默认）：`gpt-6.1-sol` + `model_reasoning_effort="xhigh"`
  - `agy`：Antigravity CLI（`gemini-4-hydrogen-high` + `--effort high`）
  - `claude`：Claude Code CLI

确保本地联调网关 `tools/local-api-bridge` 已启动后执行：

```bash
# 启动 5 轮连续挂机 + Codex (gpt-6.1-sol xhigh) 自动修单（默认预设即 codex）
python3 tools/soak-autofix/soak_ctl.py run \
  --session-dir code/mcagent/logs/soak-night-01 \
  --branch feat/soak-autofix \
  --episodes 5 \
  --episode-seconds 180 \
  --llm \
  --fixer-preset codex

# 或切换为 Antigravity CLI (gemini-4-hydrogen-high) 自动修单入口
python3 tools/soak-autofix/soak_ctl.py run \
  --session-dir code/mcagent/logs/soak-night-agy \
  --branch feat/soak-autofix \
  --episodes 5 \
  --episode-seconds 180 \
  --llm \
  --fixer-preset agy

# 单候选目标晋级聚焦模式（--episodes 0 持续不间断挂机；可在挂机期间随时用 set-goal 热注入下一个目标）
python3 tools/soak-autofix/soak_ctl.py run \
  --session-dir code/mcagent/logs/soak-cont \
  --branch feat/soak-autofix \
  --goal smelt_iron \
  --max-goal-attempts 4 \
  --episodes 0 \
  --episode-seconds 240 \
  --llm \
  --fixer-preset codex

# 挂机不中断期间：在主仓库分支（如 feat/goal-staging）写好下一个初版技能并改好 goals.py 后，一键热合入并切换聚焦目标
python3 tools/soak-autofix/soak_ctl.py set-goal \
  --session-dir code/mcagent/logs/soak-cont \
  --merge-ref feat/goal-staging \
  --goal obtain_armor

# 技能文件名以 goals.py 的 skill / editable_files 为准（铁器线在 skills/iron.py + tests/test_iron.py，
# 钻石 diamond.py、生存 survival.py、战斗 combat.py、黑曜石/下界门 nether.py），否则 Fixer 白名单改不到。

# 热改每轮时长/步数（每轮都是新角色空包从头跑前置链，越往后的目标需要越长的轮；下一轮起生效）
# 合法区间：60–3600 秒、10–600 步；越界直接拒绝。可与 --merge-ref / --goal 同时使用
python3 tools/soak-autofix/soak_ctl.py set-goal \
  --session-dir code/mcagent/logs/soak-cont \
  --episode-seconds 420 --episode-steps 80

# 清除热覆盖，回到 run 启动时的 --episode-seconds / --episode-steps
python3 tools/soak-autofix/soak_ctl.py set-goal \
  --session-dir code/mcagent/logs/soak-cont \
  --clear-episode-budget

# 或切回常规全量已毕业目标挂机（不聚焦单一目标）
python3 tools/soak-autofix/soak_ctl.py set-goal \
  --session-dir code/mcagent/logs/soak-cont \
  --clear-goal

# 优雅停止持续挂机：写入 STOP 文件（Runner 跑完当前轮后自动收尾退出）
touch code/mcagent/logs/soak-cont/STOP

# 跑完后卸载临时 worktree（分支 feat/soak-autofix 与 REVIEW_REPORT.md 完整保留）
python3 tools/soak-autofix/soak_ctl.py teardown \
  --session-dir code/mcagent/logs/soak-night-01
```

### 方式二：在 Antigravity 对话中由主代理 + 子代理驱动（无需外部 CLI 额度）

```bash
# 1. 初始化双 worktree 与待审分支
python3 tools/soak-autofix/soak_ctl.py init \
  --session-dir code/mcagent/logs/soak-agy-01 \
  --branch feat/soak-autofix

# 2. 后台持续跑挂机轮次（默认开启 --llm 走模型选技能；纯规则基线传 --no-llm）
python3 tools/soak-autofix/soak_ctl.py runner-loop \
  --session-dir code/mcagent/logs/soak-agy-01 \
  --episodes 5 --episode-seconds 180 --llm

# 3. 子代理认领一单（获取 fixer_wt 与生成的 prompt.md）→ 修改代码 → 跑四道机器门禁入分支
python3 tools/soak-autofix/soak_ctl.py claim --session-dir code/mcagent/logs/soak-agy-01
python3 tools/soak-autofix/soak_ctl.py verify-and-promote \
  --session-dir code/mcagent/logs/soak-agy-01 \
  --fingerprint "<fingerprint>"
```

## 人工验收与合入

挂机结束后打开 `<session-dir>/REVIEW_REPORT.md`，对照报告中的各轮得分/Bug 变化表执行：

```bash
git log --oneline --stat main..feat/soak-autofix
git diff main..feat/soak-autofix
# 人工确认无误后再合入 main
```
