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

1. **靶场 ≠ 比赛预算。** 本地网关 RTT ≈ 1.5 ms，官方端点 500–700 ms，差约 400 倍。
   靶场能验证"做不做得成"，**不能**验证"来不来得及"。靶场数据一律标 `local-target`。
2. **合规。** 网关是自建 MC 协议客户端（§2B.2 禁止项），它靠 `auth:"offline"` 连不上正式比赛服。
   比赛当天 `MC_API_BASE` 必须切回官方，**没有任何例外**。
3. **`MC_USER` 每人不同。** 同名会按规范 §1.1 踢掉旧 token——那是正确行为，不是 bug。

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
# 1) 同步
cd mc-agent-2026 && git pull

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

1. `docs/06-工作日志.md` 追加一条：改了什么 / 为什么 / **测到什么数**（附 probe json 路径）/ 遗留
2. 数字写进 `docs/03` 时**必须标 `local-target`**，不得进时间预算表
3. 关掉网关，别把 bot 挂在共享世界线上

## 本机专属参数

npm 缓存路径、`uv` 是否可用、已验证过的网关启动命令——这些因机器而异，
写在本地 skill `mc-agent-live-experiment` 里，不进仓库。**两者冲突时以本文件为准。**
