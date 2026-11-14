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
│ Bot: agy_soak_fixer (--mode focus)    │    │ Bot: agy_soak_r001... (--mode campaign)│
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
