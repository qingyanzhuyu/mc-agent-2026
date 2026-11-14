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
│    - Gate 1: 文件白名单 + 密钥正则扫描 │
│    - Gate 2: compileall + 120+ 单测   │
│    - Gate 3: 30s 真机 focus 金丝雀     │
│    - Gate 4: 提交至待审分支            │
└───────────────────────────────────────┘
```

## 两种使用方式

### 方式一：一键双进程全自动挂机（外接本机 `codex` 或 `claude` CLI）

确保本地联调网关 `tools/local-api-bridge` 已启动后执行：

```bash
# 启动 5 轮连续挂机（默认已启用 --llm 让模型操纵角色；若跑纯规则基线可传 --no-llm）+ Codex 自动修单
python3 tools/soak-autofix/soak_ctl.py run \
  --session-dir code/mcagent/logs/soak-night-01 \
  --branch feat/soak-autofix \
  --episodes 5 \
  --episode-seconds 180 \
  --llm \
  --fixer-preset codex

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
