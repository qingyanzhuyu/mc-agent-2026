# Local NMO Agent API v1.3.1 Bridge（队内本地/局域网联调网关）

将 NMO 官方 [`Minecraft Agent API v1.3.1`](../../docs/99-原始记录/api-spec-v1.3.1.md)（HTTP JSON 契约）1:1 映射到本地或局域网自建的 **Minecraft Java `1.21.11` 原版测试服（`online-mode=false`）**。

用于在官方测试服暂不可用或单皮肤站账号被队友占用时，支撑多人并行完成 `AGENTS.md` §2A.6 要求的 **`verified: live` 真机物理闭环验证**（对应 `docs/04` `B9`）。

> **赛事合规与隔离声明（`AGENTS.md` §2B.2）**
> - 本目录属于**纯赛前开发期测试靶场（Mock Server）**，比赛运行时代码 `code/mcagent/` 对本目录**零依赖、零导入**。
> - `code/mcagent` 始终只发标准 REST HTTP 请求；切回正式比赛环境时，只需将 `code/mcagent/.env` 中的 `MC_API_BASE` 改回 `https://mc-api.windywindie.top/api/v1`，**无需改动任何一行 Python 代码**。
> - 本网关底层固定 `auth: "offline"`，既**严禁**也**无法**连接开启外置皮肤站鉴权的正式比赛服。

---

## 1. 团队联调快速上手（默认直连队内共享测试服 `frp-net.com:64463`）

网关内部按 `MC_USER` 维护独立的 `BotSession`：
- **不同 `MC_USER`**（例如 `alice_bot`、`bob_bot`）可在**同一个测试世界**里同时在线独立运行，互不抢锁、互不踢 Token；
- **相同 `MC_USER`** 重复登录会严格复现规范 §1.1 的“踢掉旧 Token（返回 HTTP 401 `invalid_token`）”行为，方便验证单点重登容错。

### 方式一（推荐，异地/全队通用）：队友各自本机一键启动网关

网关默认已指向队内穿透测试服 `frp-net.com:64463`（Minecraft `1.21.11`），**无需改任何网关配置**，装有 **Node.js ≥ 18** 即可直接跑：

```bash
cd mc-agent-2026/tools/local-api-bridge
npm ci
npm start
# 看到 listening on http://127.0.0.1:8787/api/v1 -> MC frp-net.com:64463 即成功
```

然后在 `mc-agent-2026/code/mcagent/.env` 填写（**注意每人取不同的 `MC_USER` 避免同角色互踢**）：

```ini
MC_API_BASE=http://127.0.0.1:8787/api/v1
MC_USER=你的专属bot英文名   # 2~16 位字母数字下划线，每人取不同名字即可同服并发！
MC_PASS=localdev            # 本地模式任意非空字符串即可
MC_PROXY=                   # 必须留空（直连本机网关）
```

> 若你想改连自己本机另开的 `1.21.11` 服，只需 `cp .env.example .env` 并把 `MC_HOST=127.0.0.1`、`MC_PORT=25565` 填进去即可。

### 方式二（同局域网可选）：一人起网关，队友免装 Node.js 直连

若大家在同一局域网，也可只由一台机器启动 `BRIDGE_HOST=0.0.0.0 npm start`，其余队友直接在 `mc-agent-2026/code/mcagent/.env` 将 `MC_API_BASE` 设为 `http://<网关机器局域网IP>:8787/api/v1` 并填各自不同的 `MC_USER`。

---

## 2. 原版 `1.21.11` 测试服推荐配置（`server.properties`）

自建原版服务端（Vanilla `1.21.11` / Paper `1.21.11`）建议检查以下三项：

```properties
online-mode=false
enforce-secure-profile=false
# 若想测试出生点保护回滚（protected_region），保留默认 16；若想出生即挖，设为 0：
spawn-protection=16
```

---

## 3. 验证与联调命令

### 3.1 网关自身离线契约单测（无需启动 MC 服）

```bash
cd mc-agent-2026/tools/local-api-bridge
npm test
```

### 3.2 用 `mcagent` 跑冒烟与基线探测

保持网关与 `1.21.11` 测试服运行后，打开新终端：

```bash
cd mc-agent-2026/code

# 1) 未授权干跑探测（验证 401 unauthorized）
uv run python -m mcagent.smoke --dry

# 2) 登录并等待 ready=True，打印坐标/血量/背包
uv run python -m mcagent.smoke

# 3) 跑全套 [1]~[7] 接口探测并落盘 probe/*.json
uv run python -m mcagent.probe
```

### 3.3 健康检查接口

浏览器或 `curl` 访问网关状态（查看当前指向的 MC 服与在线的队友 Bot 列表）：

```bash
curl -s http://127.0.0.1:8787/healthz
```

---

## 4. 协议对齐范围与已知近似说明

| 模块 | 对齐情况（对照 `api-spec-v1.3.1.md`） |
|---|---|
| 响应封装与状态码（§0.1） | 业务成败统一 HTTP 200 `{ok, ms, result\|error}`；非法 JSON 返 400 `bad_json`；未授权/过期返 401 `unauthorized`/`invalid_token`；未知路由返 404 `unknown_api` |
| 认证与会话互斥（§1 / §2 / §7） | 同名 `username` 重登立即踢废旧 Token；断线重连 ≥30s 节流；60s 内死亡 3 次触发 5 分钟 `death_loop_cooldown` |
| 观察接口（§3.1–§3.7） | `/status`、`/inventory`、`/find_blocks`（严格执行**仅暴露方块**过滤，无矿透）、`/get_block`、`/get_entities`、`/events`（`seq` 自增、GET+POST 参数合并）、`/advancements`（解析原版进度包） |
| 动作互斥与中断（§0 / §4 / §4.15） | 单会话同步阻塞互斥锁（并发动作返 `busy`）；支持 `stop_on`（`hurt`/`death`）自动中断并附 `nearest_hostile`；`/stop` 即时终止寻路与挖掘；服务端回滚识别为 `protected_region` |
| 已知与官方实现的细微差异 | 1. 载具骑乘态下的 `move_to` 逐帧掌舵（§4.1 炽足兽/船速度曲线）当前仍走通用寻路近似；<br>2. `/use_item` 的钓鱼咬钩与末影之眼 `bearing_deg` 为简化近似；<br>3. `/craft` 的 `count` 按“最少期望产出数量”折算配方次数（官方精确语义仍挂 `docs/04` `C2` 待官方服核对） |
