---
name: live-experiment
description: Use when you need to pull the latest mc-agent-2026 repo and run a live in-game experiment (local API bridge + bot + smoke/probe). Covers the sync routine, the three pre-flight checks, bot naming, the npm mirror trap, and why local-target numbers must never enter the competition time budget.
---

# 拉最新代码 + 跑一次真机实验

## 何时使用

- 要验证某个改动在**真实 Minecraft 世界**里成不成立（`verified: live`）
- 要拿基线数据（`T_status` / `T_move20` / `T_dig1`）
- 队友刚推了新东西，你要在自己的环境里复现一次

## 三条铁律（先读，再动手）

规则本体在仓库根目录 `AGENTS.md`，这里只说和真机实验的关系：

1. **靶场 ≠ 比赛预算**（`AGENTS.md` §4）。本地网关 RTT ≈ 1.5 ms，官方端点 500–700 ms，差约 400 倍。
   靶场能验证"做不做得成"，**不能**验证"来不来得及"。
2. **比赛当天切回官方端点**（`AGENTS.md` §2B.2）。网关靠 `auth:"offline"` 连的是队内测试服，连不上正式比赛服。
3. **`MC_USER` 每人不同**（`AGENTS.md` §3）。

## 前置三查（每查 5 秒，跳过任一查都会浪费半小时）

```bash
# ① 仓库是不是最新
cd mc-agent-2026 && git log --oneline -1 && git status --short

# ② 测试服通不通（当前地址见 tools/local-api-bridge/.env.example）
python -c "import socket;s=socket.socket();s.settimeout(5);s.connect(('frp-net.com',64463));print('MC 通')"

# ③ 网关在不在（不在就先起，见第 2 步）
curl -s http://127.0.0.1:8787/healthz
```

## 五步流程

```bash
# 1) 同步（可选；规则见 AGENTS.md §6：先在自己分支 commit，再同步）
cd mc-agent-2026
git status --short        # 有改动 -> 先在自己的分支上 commit（commit 不是推送，不动远端）
git fetch --dry-run 2>&1  # 先干跑，看远端到底有没有新东西
git pull                  # 只有确实有新提交时才拉；没有就别做无效 pull

# 2) 起网关（第一次要装依赖）
cd tools/local-api-bridge
test -f .env || cp .env.example .env     # 默认已指向 frp-net.com:64463
npm ci --registry=https://registry.npmmirror.com --replace-registry-host=always
npm start                                 # 另开窗口；看到 8787 即可
curl -s http://127.0.0.1:8787/healthz     # 应含 mc_target

# 3) 配 Python 侧（每人只做一次）
cd ../../code
# > code/mcagent/.env
#   MC_API_BASE=http://127.0.0.1:8787/api/v1
#   MC_USER=<你的英文bot名>
#   MC_PASS=localdev
#   MC_PROXY=

# 4) 进服
python -m mcagent.smoke        # 看到 ready=True + 坐标 就算通

# 5) 量基线
python -m mcagent.probe        # 落盘 probe/probe-*.json
```

> 装了 `uv` 就用 `uv run python -m ...`；没装就直接 `python -m ...`，两者等价。

## 第 1 步为什么不是"无脑 git pull"

规则（先 commit 再同步、三条禁用命令）在 `AGENTS.md` §6，这里只解释原因。

**pull 不会覆盖你的改动，它只会拒绝。** 默认的 `git pull` 是 fail-closed 的：

| 你的本地状态 | 远端同一文件也改了 | 实际行为 |
|---|---|---|
| 未提交改动 | 是 | **拒绝**：`Your local changes would be overwritten`，一个字节都不动 |
| 未提交改动 | 否 | 正常合并，你的改动原样保留 |
| 未跟踪文件 | 远端新增同名 | **拒绝**：`untracked working tree files would be overwritten` |
| 已提交未推送 | 是 | merge/rebase，可能冲突——不会丢，但容易乱 |

会真正丢数据的只有 `reset --hard`、`pull --force`、`checkout -- .`——这三条**任何时候都不要用**。

所以顺序是：**先 commit，再 pull**。commit 是本地操作，不推送、不影响远端；
反过来做（先 pull 再 commit）会卡在“工作区脏”上，而 agent 往往把这个报错误读成“需要强制覆盖”。

另外 `code/mcagent/.env`、`.token.json`、`probe/`、`logs/`、`tools/local-api-bridge/.env`
都在 `.gitignore` 里，**pull 永远不碰它们**，实验配置和 token 缓存是安全的。

> 在 Windows 上如果 `git pull` 报 `SEC_E_NO_CREDENTIALS`（schannel 坏），
> 改用 GitHub Desktop 点 **Pull origin**，不要为了绕过它去 `--force`。


## 故障对照表

| 症状 | 原因 | 处理 |
|---|---|---|
| `npm ci` 卡住不动 | `registry.npmjs.org` 国内直连超时 | 加 `--registry=https://registry.npmmirror.com --replace-registry-host=always` |
| `operation was rejected by your operating system` | npm 缓存目录不在可写位置 | 再加 `--cache <工作区>/.npm-cache` |
| `login` 成功但 `ready` 一直 false | 网关连不上 MC 服，或卡在大厅 | 看网关窗口日志；`curl /healthz` 确认 `mc_target` |
| `invalid_token` | 别人用了同一个 `MC_USER`，或 token 过期 | 换自己的名字；client 会自动重登 |
| `ProxyError` | `MC_PROXY` 没留空，走了 7897 | 本机网关必须直连，清空该行 |
| `/healthz` 连不上 | 网关没起 / 端口被占 | `BRIDGE_PORT` 换端口，同步改 `MC_API_BASE` |

## 收尾（每次都做）

按 `AGENTS.md` §3（下线、关网关、账号表改回空闲）和 §8（日志、`docs/05`、`docs/04`、`git status`）做完，另外：

1. 日志里写清**测到什么数**，并附 probe json 路径
2. 数字写进 `docs/03` 时标 `local-target`（`AGENTS.md` §4）

## 本机专属参数

npm 缓存路径、`uv` 是否可用、已验证过的网关启动命令——这些因机器而异，
写在本地 skill `mc-agent-live-experiment` 里，不进仓库。**两者冲突时以本文件为准。**
