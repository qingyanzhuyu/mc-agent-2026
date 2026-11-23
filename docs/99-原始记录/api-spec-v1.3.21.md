# Minecraft Agent API 规范 v1.3

> 给 AI agent 用的 Minecraft Bot HTTP 控制接口：每个登录对应一个独立的 bot，agent 通过 JSON 接口观察世界并执行动作。

## 基础信息

- **Base URL**：`http://<部署地址>:18900/api/v1`（端口见 `config.json` 的 `http.port`；对公网开放时请放在 HTTPS 反向代理后面）
- **协议**：HTTP + JSON
- **认证**：Bearer Token（`/auth/login` 换取；账号体系由部署方的认证适配器决定，见 `docs/ADAPTERS.md`）
- **自己本机部署开源版时**：服务器内部处理很快（`status` / `get_block` 约 1 ms，`/control` 只比 `ms` 多 1–2 ms，`/control` 带 `"wait": false` 时约 1 ms，`/posture` 约 55 ms）。如果你测到的延迟大很多，先查客户端：**用 `127.0.0.1` 而不是 `localhost`**（1.3.12 前服务只监听 IPv4，Windows 上 `localhost` 先试 IPv6，每个新连接白等约 0.25 s（Node fetch）到 2 s（Python））；**复用连接**（Python 用 `requests.Session()`，不要每次新建）；bot 本身到游戏服务器的网络延迟也会体现在动作效果上
- **并发**：每个 token 对应一个独立的 bot 实例
- **性能隔离**：每个 bot 跑在服务端自己的进程里，别的 bot 在寻路、大范围查找方块时不影响你的请求延迟（23 个 bot 同时满负荷实测：`status` p99 约 50 ms）
- **动作互斥**：同一时间只能执行一个占锁动作。`/chat` 可与动作并发。

---

## 0. 通用约定

这三条适用于所有接口，先读这一节再看具体接口。

### 0.1 响应封装

成功（HTTP 200）：

```json
{ "ok": true, "ms": 12, "result": { "...": "接口各自的返回值" } }
```

失败（HTTP 200，业务错误一律 200）：

```json
{ "ok": false, "ms": 12, "error": { "code": "path_not_found", "message": "...", "pos": [0, 64, 0] } }
```

| HTTP | 含义 |
|---|---|
| 200 | 业务结果（成功或失败都看 `ok`） |
| 400 | 请求体不是合法 JSON |
| 401 | 缺少 / 无效 / 过期的 Bearer Token |
| 404 | 路径不存在（`error.code` 为 `unknown_api`） |
| 413 | 请求体超过 1 MiB（`error.code` 为 `body_too_large`） |

`ms` 是本次调用在服务端的耗时（毫秒）。`error` 里除了 `code`、`message` 还会带上下文（如 `inventory`、`pos`、`required`），便于 agent 直接纠错。

### 0.2 坐标与距离

- 输出：**一律 `pos: [x, y, z]`**（保留 1 位小数）；距离字段一律 `dist`；朝向 `yaw` / `pitch`（弧度）。
- 输入：写 `x`、`y`、`z` 三个数字，**也可以直接传 `pos: [x, y, z]`**，可以把观察结果原样回填。

```json
{"x": 100, "y": 64, "z": -200}     // 等价
{"pos": [100, 64, -200]}
```

### 0.3 时间

- 时间戳：Unix 毫秒。
- 事件流用**自增序号 `seq`**，不是时间戳：用上一次返回的 `last_seq` 作为下一次的 `since`。

---

## 1. 认证

### 1.1 登录换取 Token

```
POST /auth/login
```

```json
{ "username": "用户名", "password": "密码" }
```

**Response 200**

```json
{
  "ok": true,
  "token": "64 位十六进制字符串",
  "expires_at": "2026-11-03T12:00:00.000Z",
  "bot_username": "Agent_1"
}
```

| HTTP | `error.code` | 说明 |
|---|---|---|
| 401 | `invalid_credentials` | 用户名或密码错误 |
| 401 | `no_profile` | 外置登录（yggdrasil）账号下没有选定角色 |
| 429 | `rate_limited` | 上游认证服务限频（`retry_after_s` 为空，等几秒重试） |
| 429 | `rate_limited` | **本服务限流**：同一 IP 每分钟最多 10 次失败，超过锁 60 秒；全局每分钟最多 60 次尝试（均可在 `http.loginRateLimit` 配置）。带 `retry_after_s` 与 `Retry-After` 响应头 |
| 503 | `routing_unavailable` | 分配表缺失或无效。新登录暂停，已有 token 保持原分配 |

限流的客户端身份默认取 socket 地址；配置 `http.trustProxyHeaders: true` 后改取 `CF-Connecting-IP` / `X-Forwarded-For`（只在确实部署于反向代理之后时打开，否则可被伪造）。登录成功会清零该 IP 的失败计数。

登录成功后 bot 会**自动连接**服务器，通常 5–10 秒内上线；请轮询 `/status` 直到 `result.ready: true`。同一角色重复登录会让旧 token 立即失效。

### 1.2 登出

```
POST /auth/logout
Authorization: Bearer <token>
```

```json
{ "ok": true }
```

---

## 2. 连接管理

### 2.1 连接

```
POST /connect
```

`result`：`{"ok": true}`；已在线时 `{"ok": true, "already_connected": true}`。bot 死亡后会自动重生，客户端可轮询 `/status` 等待就绪。

`status.ready` 由部署方配置的就绪适配器和世界加载状态共同决定。内置代理适配器按认证角色名为每个 token 固定后端，`status.server` 显示目标。切服或确认中断期间，`/status` 只返回连接信息。坐标、背包、方块和进度等数据在目标世界就绪后提供。配置说明见 `docs/ADAPTERS.md`。

已经连着但尚未就绪时，`/connect` 会请求适配器重试，实际发送受重试间隔限制。接口可能返回：

```json
{"ok": true, "already_connected": true, "ready": false}
```

适配器可能附带额外字段。切服等待期间，观察和动作可能返回 `wrong_server`；在目标服务器上等待世界加载时，可能返回 `not_ready`。内置代理适配器在后端确认失联时返回 `backend_unavailable`，恢复确认后同一个 token 可以继续使用。

### 2.2 断开

```
POST /disconnect
```

```json
{ "ok": true, "result": { "ok": true } }
```

---

## 3. 观察接口

观察接口不占用动作锁，可与动作并发调用。

### 3.1 状态

```
GET /status
```

```json
{
  "ok": true, "ms": 0,
  "result": {
    "username": "Agent_1",
    "pos": [-76.5, 102, 37.5],
    "speed": 0,
    "velocity": [0, 0, 0],
    "yaw": 1.57,
    "dimension": "overworld",
    "gamemode": "survival",
    "health": 20,
    "food": 20,
    "saturation": 5,
    "xp_level": 0,
    "xp_progress": 0.42,
    "oxygen": 20,
    "armor_points": 17,
    "held": {"name": "iron_sword", "count": 1, "slot": 36},
    "offhand": {"name": "shield", "count": 1, "slot": 45},
    "effects": [{"name": "poison", "level": 2, "duration_s": 12.4}],
    "on_fire": false,
    "is_sleeping": false,
    "in_water": false,
    "submerged": false,
    "in_lava": false,
    "in_vehicle": null,
    "sneaking": false,
    "blocking": false,
    "using_item": null,
    "time_of_day": 731,
    "is_day": true,
    "raining": false,
    "thundering": false,
    "biome": "plains",
    "on_ground": true,
    "alive": true,
    "ready": true,
    "server": "survival2",
    "readiness_state": "ready",
    "busy": null,
    "nearest_hostile": {"id": 812, "name": "zombie", "dist": 12.5},
    "last_death": {"pos": [80, 63, -180], "t": 1791881333488, "cause": "Agent_1 was slain by Zombie"}
  }
}
```

| 字段 | 说明 |
|---|---|
| `ready` | bot 存活且可执行动作。死亡重生或切换维度会重新等待世界加载 |
| `busy` | 正在执行的动作名，空闲为 `null` |
| `speed` | 当前水平速度（格/秒，3 位小数），按 bot **上一个物理 tick 的实际位移**算。参考：步行约 4.3、疾跑约 5.6、站着 0；为 `null` 的情况同 `velocity` |
| `velocity` | 上一个物理 tick 的实际位移 `[vx, vy, vz]` × 20（格/秒），Y 正值向上；下落、被击退、被水流冲时都会体现。刚传送 / 上下载具后的第一个 tick、骑乘中、还没有物理数据时为 `null` |
| `xp_progress` | 当前等级内的经验进度 0–1（配合 `xp_level` 判断离 30 级还差多少） |
| `armor_points` | 当前护甲值（0–20），按护甲属性或装备换算 |
| `effects` | 药水效果列表；`level` 从 1 起（原版 amplifier + 1） |
| `on_fire` | 是否着火（**不是**药水效果，来自实体 flags） |
| `in_water` | 身体是否浸在水里（含水草/海带这类水方块） |
| `submerged` | 头部是否在水下（配合 `oxygen` 判断溺水） |
| `oxygen` | 剩余氧气，**1.21 满值是 20**（不是 300）；`-1` 表示已在扣血 |
| `in_lava` | 是否站在岩浆里 |
| `in_vehicle` | `{"id","name"}` 或 `null` |
| `sneaking` / `jumping` / `blocking` | 当前姿态，与 `/posture` 对应（`jumping` 为持续按住跳跃，水里即踩水浮面） |
| `held_controls` | `/control` `wait:false` 正在按住的键：`{"keys": ["forward"], "release_in_ms": 120}`；没有时为 `null` |
| `server` | 代理就绪适配器为会话选定的目标后端名（`/server` 后面那个名字）。切服期间这个字段仍显示目标；各后端分别保存世界、背包和进度 |
| `readiness_state` | 内置代理适配器的状态。可能为 `connecting`、`switching`、`loading_world`、`backend_unavailable`、`dead` 或 `ready` |
| `using_item` | 正在使用物品时返回 `{"name","elapsed_ms"}` |
| `nearest_hostile` | 24 格内最近的敌对生物，没有则省略 |
| `last_death` | 最近一次死亡的位置、时间、死亡信息 |

### 3.2 背包

```
GET /inventory
```

```json
{
  "ok": true,
  "result": {
    "items": [{"name": "oak_log", "count": 64, "slot": 0}],
    "armor": {"head": null, "chest": {"name": "iron_chestplate", "count": 1, "slot": 6, "durability": [240, 240]}, "legs": null, "feet": null},
    "offhand": null,
    "held": {"name": "oak_log", "count": 64, "slot": 0},
    "empty_slots": 30
  }
}
```

物品附加信息（`/inventory` 与窗口接口一致）：附魔 `enchantments: [{name, level}]`、自定义名称 `custom_name`、药水 `potion`、旗帜图案 `banner_patterns`。

### 3.3 查找方块

```
POST /find_blocks
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `types` | 无 | 方块名数组；省略时返回附近方块**种类摘要**（远距离找特定方块请务必传入 `types`） |
| `radius` | 16 | 指定 `types` 时最大 128（对齐 8 区块视距），摘要模式最大 64；超出上限自动截断，`< 1` 或非数字返回 `bad_args` |
| `limit` | 20 | 最多返回多少条 |
| `properties` | 无 | 属性筛选，如 `{"lit": true}`、`{"age": 7}` |
| `mature` | 无 | 只对作物有效，`true` 只要成熟 |

**指定类型**

```json
{"types": ["oak_log"], "radius": 32, "limit": 5}
```

```json
{"ok": true, "result": {"total": 3, "blocks": [
  {"name": "oak_log", "pos": [105, 68, -195], "dist": 8.1, "properties": {"axis": "y"}}
]}}
```

**摘要模式**

```json
{"ok": true, "result": {"radius": 32, "scanned": 412, "blocks": {
  "stone": {"count": 120, "nearest": [98, 60, -198], "dist": 5.2}
}}}
```

约束：只返回**暴露方块**（至少一面邻接空气或透明方块），不提供矿透能力。范围是以 bot 为球心、半径 `radius` 的球；指定类型时最多统计最近的 10000 个（底层按区块段调色板跳过无关区域，半径 128 搜稀疏目标通常仅数毫秒），摘要模式最多统计最近的 4000 个（`scanned`）。扫描期间同一个 bot 的其它请求照常响应。

### 3.4 查询单个方块

```
POST /get_block  {"pos": [100, 64, -200]}
```

```json
{"ok": true, "result": {
  "name": "wheat", "pos": [100, 64, -200],
  "properties": {"age": 5}, "growth": [5, 7], "mature": false,
  "diggable": true, "hardness": 0, "harvest_tool_ok": true
}}
```

`not_loaded` 表示该区块还没加载，等一会儿重试；坐标缺失/不是数字（`{}`、`{"pos":[8,99]}`、`{"x":"abc"}`）返回 `bad_args`。

### 3.5 查找实体

```
POST /get_entities  {"radius": 32, "types": ["hostile", "animal"]}
```

```json
{"ok": true, "result": {"entities": [
  {"id": 12345, "type": "hostile", "name": "zombie", "pos": [95, 64, -195], "dist": 12.5, "health": 20},
  {"id": 12346, "type": "animal", "name": "cow", "pos": [110, 65, -190], "dist": 15.2, "health": 10}
]}}
```

- `types` 可写类别（`hostile` / `animal` / `player` / `item` / `other`）或具体名字（`zombie`、`cow`）。
- 掉落物额外带 `item`：`{"name":"item","item":"oak_logx1"}`。
- 最多返回 40 个，按距离排序。

### 3.6 事件流

```
GET  /events?since=<seq>&limit=50
POST /events  {"since": 41, "limit": 50}
```

两种写法等价：查询串参数会与 JSON body 合并（同名时 body 优先）。

```json
{"ok": true, "result": {"since": 41, "last_seq": 41, "total": 1, "events": [
  {"seq": 40, "t": 1791881333488, "type": "hurt", "health": 15.5, "damage": 4.5,
   "likely_attacker": {"id": 12345, "name": "zombie", "dist": 12.5}}
]}}
```

- `since`：只返回 `seq > since` 的事件，必须是非负数字；
- `limit`：最多返回多少条，1–500（默认 50）；返回的是**最早的** N 条，`has_more=true` 表示还有积压，继续轮询即可；
- `last_seq`：本批最后一条的序号（没有新事件时等于当前最新序号），下次直接当 `since`，不会漏事件；`total`：过滤后的事件总数。

事件类型：

| type | 字段 |
|---|---|
| `chat` | `text` |
| `hurt` | `health`、`damage`、`source`（原版伤害类型，如 `fall`、`mob_attack`、`drown`、`in_fire`）、`source_entity`（造成伤害的实体，若有）、`likely_attacker`（16 格内最近的敌对生物） |
| `death` | `pos`、`t`、`dropped_items_at` |
| `spawn` | `pos` |
| `pickup` | `item`、`count` |
| `effect_start` / `effect_end` | `name`、`level`、`duration_s` |
| `sleep` / `wake` | 无 |
| `vehicle` | `action`（`mount`/`dismount`）、`name` |
| `kicked` / `disconnected` | `reason` |
| `attack_hit` | `hit`、`t_ms`、`entity_id`、`dist`、`target_pos`、`bot_hp`、可选 `occluded`（仅 `/attack` 传 `feedback: true` 时推送） |
| `server_confirmed` | `via`（以及适配器给出的 `server` 等） |

### 3.7 进度

```
POST /advancements  {"status": "completed", "include_recipes": false}
```

返回 `result.{ready, updated_at, packets_received, summary, advancements[]}`；每项含 `id`、`title`、`frame`、`status`、`completed_at`、`progress`。只包含服务器已下发的可见进度树。

---

## 4. 动作接口

所有动作会自动寻路到可操作距离。通用参数 `stop_on`（默认 `["hurt","death"]`），`attack`/`shoot` 默认只中断于 `death`。

被打断时：动作已有部分结果则照常 `ok:true` 返回，`result.interrupted_by` 给出原因；否则返回 `error.code = "interrupted"`（死亡为 `dead`），附带 `cause`、`health`、`pos`、`nearest_hostile`，以及被打断前的原始错误码 `inner_code` 和它的附加字段（例如钓鱼的 `casts`）。

### 4.1 移动

```
POST /move_to  {"x": 100, "z": -200, "range": 1}
POST /move_to  {"x": 100, "y": 64, "z": -200, "range": 0}
POST /move_to  {"entity_id": 12345, "range": 2}
POST /move_to  {"x": 200, "z": -300, "allow_partial": true}
```

- `range`：坐标移动默认 `1`（靠近目标周围 1 格即停，兼容直接传实心方块坐标的场景）；传 `0`（或 `< 1`）时进入精确目标格并用真实物理按键微调到目标中心（传整数坐标时对准方块中心 `+0.5`，传小数坐标时对准指定位置，误差 ≤ 0.15 格）。
- `entity_id`：未传 `range` 时，掉落物（`item`）默认 `0.5`（走到可直接拾取），其它实体默认 `2`；显式传 `range` 时按传入值生效（不再强制 ≥ 2）。
- `allow_partial`：默认 `false`（到不了终点直接返回 `path_not_found`）；传 `true` 时若终点不可达但 A* 找到了更接近终点的可达点，会先走到最近可达点并返回 `partial: true`。
- 省略 `y` 时只要求到达水平坐标。`result`：`{"pos": [100.2, 64.0, -199.8], "dist": 0.36}`（部分靠近时附 `partial: true`，跨维度时附 `dimension_changed: {"from","to"}`）。失败时 `error` 附带 `pos`、`dist_left`，必要时给出 `target_unloaded: true` 或 `target_solid: true`。

**骑乘态自动切掌舵**：当 `status.in_vehicle` 非空时，同一个 `move_to` 不再走路，而是驱动载具（船 / 炽足兽 / 猪 / 马等）驶向目标，`y` 忽略：

```json
{"ok": true, "result": {
  "drove": true,
  "vehicle": {"id": 1234, "name": "oak_boat"},
  "pos": [116.4, 63.0, -200.1],
  "dist": 1.4, "ms": 3255
}}
```

- 载具速度上限按种类：船 8、炽足兽 4、猪 5、马 9 格/秒，接近目标时线性减速；
- 目标在水里/岩浆里都行（船在水、炽足兽在岩浆），撞到障碍不会自动绕行，会一直朝目标顶，超时报 `drive_timeout`；
- 骑炽足兽要先上鞍（`interact {entity_id, item:"saddle"}`），并用 `equip` 手持 `warped_fungus_on_a_stick`，否则服务端不认方向输入；
- 中途掉下来返回 `dismounted`。

### 4.2 挖掘

```
POST /dig  {"pos": [100, 64, -200], "collect": true}
```

```json
{"ok": true, "result": {"dug": "stone", "dig_ms": 1150, "tool": "stone_pickaxe", "gained": {"cobblestone": 1}, "sneaked": false}}
```

`gained` 是"物品名 → 数量"的对象。没有合适工具时会附 `warning`。

目标不在眼睛 5 格内时会先走过去（包括斜下方、要下到槽里才够得着的方块）；走到了还够不着返回 `out_of_reach`（附 `pos`）。

只有收到**服务端**发来的这一格方块变化才算挖掉（`dig_ms` 含等待确认的时间）。服务端没确认时返回 `dig_unconfirmed`（方块还在，本地缓存也会恢复成原方块），附 `pos`、`eye_dist`（眼睛到方块中心的距离）、`on_ground`、`ping_ms` 方便排查——最常见的原因是服务端认为 bot 离得太远（两边位置不同步）；服务端明确把方块改回原样时返回 `protected_region`。

### 4.3 放置方块

```
POST /place  {"item": "cobblestone", "pos": [100, 65, -200]}
```

不给坐标时自动在附近选落点。`place` 会在放置瞬间**自动潜行**，因此可以直接在箱子、工作台、熔炉、床上放方块；`result.sneaked: true` 表示这次做了保护。

```json
{"ok": true, "result": {"placed": "cobblestone", "pos": [100, 65, -200], "sneaked": true}}
```

若参照方块弹出了界面（说明这个位置应该走窗口接口），会自动关窗并返回 `use_window_api`。

**睡莲、蛙卵**只能放在水面上：`pos` 传水面上方那格（下面必须是水源），按原版视线判定对着水使用，返回 `sneaked: false`；下面不是水源返回 `no_support`。

### 4.4 交互（方块或实体）

```
POST /interact  {"pos": [100, 64, -200]}                 // 方块：门、拉杆、按钮、床
POST /interact  {"pos": [100, 64, -200], "item": "wheat_seeds"}   // 手持物品右键方块
POST /interact  {"entity_id": 812, "item": "wheat"}      // 实体：喂食、驯服、剪毛、挤奶、拴绳
POST /interact  {"entity_id": 42}                        // 实体：上船 / 上马 / 上矿车
```

**方块**返回变化前后的状态：

```json
{"ok": true, "result": {
  "block": "farmland", "before": {"moisture": 0},
  "after": {"name": "farmland", "properties": {"moisture": 0}},
  "above": {"before": "air", "after": "wheat", "properties": {"age": 0}},
  "changed": true, "used": {"wheat_seeds": 1}
}}
```

**床**：夜里睡着返回 `{"slept": true}`；已经睡着再点一次是起床，返回 `{"woke": true}`（返回时服务端已确认下床；2 s 内没下床返回 `cannot_sleep`）。
白天（或入睡失败）**仍会发一次原生右键包**，返回 `{"slept": false, "interacted": true, "daytime": true}`——这样白天也能把重生点绑到床上，而不是本地 `cannot_sleep` 一个包都不发。

**桶**（`bucket`、`water_bucket`、`lava_bucket`）、**玻璃瓶**、**睡莲**、**蛙卵**对着液体用时按原版视线判定：bot 先瞄准再使用。

| 操作 | `pos` 传什么 |
|---|---|
| 空桶装水 / 装岩浆 | **液体源方块本身**；传旁边的固体方块返回 `cannot_interact` |
| 倒出 | **承载方块**：液体出现在它朝向 bot 的那一面（bot 站得比它高时就是顶面）。也可以直接传**目标空气格**，此时倒在它下面那块的顶面上 |
| 玻璃瓶装水 | **水源方块本身**（也可以传水面上方那格）；水不变，`gained: {"potion": 1}`，背包里这瓶的 `potion` 为 `"water"` |
| 放睡莲 / 蛙卵 | **水源方块本身**（也可以传水面上方那格）；出现在水面上方，`target` 即该格。也可以用 `/place` |
| 炼药锅 | 桶倒进 / 舀出、玻璃瓶取水都是普通右键方块：`pos` 传**炼药锅本身**，返回普通的方块变化（`before` / `after`） |

```
POST /interact  {"pos": [100, 63, -200], "item": "bucket"}         // 装起 (100,63,-200) 的水
POST /interact  {"pos": [104, 63, -200], "item": "water_bucket"}   // 倒在 (104,63,-200) 上面，水出现在 (104,64,-200)
POST /interact  {"pos": [100, 63, -200], "item": "glass_bottle"}   // 从 (100,63,-200) 的水装一瓶水
POST /interact  {"pos": [102, 64, -200], "item": "water_bucket"}   // (102,64,-200) 是炼药锅：倒满它
```

返回 `{"block", "target", "before", "after", "changed", "used", "gained"}`：`target` 是液体实际消失 / 出现的那格。

**实体**返回：

```json
{"ok": true, "result": {
  "entity": {"id": 812, "name": "cow"},
  "in_vehicle": null,
  "used": {"wheat": 1},
  "gained": {"milk_bucket": 1}
}}
```

- `in_vehicle` 非空表示已经骑上去了；`vehicle_changed: true` 表示这次调用改变了载具状态。
- 目标开着界面（村民、流浪商人）时自动关窗并返回 `use_window_api`，请改用 `window/view` / `window/act` 的 `entity_id`。
- 走过来时目标消失返回 `no_such_entity`；服务器拒绝交互返回 `cannot_interact`。

### 4.5 使用物品（空放）

```
POST /use_item  {"item": "ender_pearl"}
POST /use_item  {"item": "spyglass", "target": {"entity_id": 900}, "ms": 800}
POST /use_item  {"item": "fishing_rod", "target": {"pos": [10, 62, 12]}, "timeout": 30000}
POST /use_item  {"item": "oak_boat", "target": {"pos": [14, 64, 10]}}
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `item` | 必填 | 背包里的物品名 |
| `target` | 无 | `{"entity_id"}` 或 `{"pos"}`；先转头对准（望远镜、喷溅药水、末影之眼、钓鱼需要）；放船 / 矿车时这个坐标**就是摆放位置** |
| `ms` | 1000 | 保持时长（盾牌、望远镜），上限 10000 |
| `timeout` | 30000 | 等待时长（吃东西、钓鱼） |
| `source` | `hand` | `off-hand` 可指定副手 |

按物品类别分派，**等到效果真正结束**才返回：

| 物品 | 行为 | `result` |
|---|---|---|
| 食物、药水、牛奶桶、蜂蜜瓶 | 吃完 / 喝完。饱食度满时普通食物返回 `not_hungry`；金苹果、附魔金苹果、紫颂果照原版随时能吃；药水、牛奶桶、蜂蜜瓶不检查饱食度 | `used`、`food_before`、`food`、`health_before`、`health`、`effects` |
| 盾牌 | 举盾保持 `ms` | `used`、`blocked_ms` |
| 末影珍珠 | 投掷 | `used`、`thrown`、`teleported`、`pos` |
| 末影之眼 | 投掷并读出方位 | `used`、`thrown`、`landed_at`、`eye_vector`、`bearing_deg` |
| 雪球、鸡蛋、风弹、喷溅 / 滞留药水、烟花、经验瓶 | 投掷 | `used`、`thrown` |
| 钓鱼竿 | 抛竿 → 等浮标入水 → 等咬钩（相对最近 1 秒最高点下沉 > 0.25 格）→ 收竿；误判咬钩或浮标 6 秒内没落进水会在 `timeout` 内自动重抛 | `used`、`caught`、`wait_ms`、`casts`、`false_bites` |
| 望远镜 | 对准目标观察 `ms` | `used`、`observed_ms`、`target?` |
| 山羊角 | 吹响 | `used` |
| 船 / 矿车 | 放在 `target` 指的那格（不传则放在面前的地上） | `used`、`entity`、`pos` |

`bearing_deg` 用 MC 的 yaw 约定：0 = 正南（+Z），90 = 正西（−X）。

钓鱼在 `timeout` 内一条都没钓到时返回 `timeout`（附 `casts`、`false_bites`、`bobber_y`、`base_y`、`in_water`）；第一竿连浮标都没出现返回 `cannot_use`。被 `stop_on` 打断后不会再抛下一竿。

**限制**：打火石、桶、种子、骨粉、锄头、玻璃瓶、睡莲、蛙卵必须对着方块用，会返回 `need_block_target`，请改调 `/interact` 并带上 `item`。其它没有空放行为的物品返回 `cannot_use`。

### 4.6 攻击

```
POST /attack  {"entity_id": 812}
POST /attack  {"entity_id": 812, "max_hits": 1}
POST /attack  {"entity_id": 812, "max_hits": 1, "chase": false, "feedback": true}
POST /attack  {"entity_id": 812, "max_hits": 5, "interval_ms": 300}
```

- 挥击间隔默认按手持物品的原版攻速计算：空手和非工具物品 250ms、剑 625ms、镐 833ms、锹 1000ms、斧 1000–1250ms、锄 250–1000ms（按材质）、三叉戟 909ms、重锤 1667ms（空手拆船/拆矿车需要 250ms，固定 650ms 会被船的每 tick 自愈抵消）；
- `max_hits`：打够这么多下立刻返回，用于"砍一下就走"（风筝苦力怕、单次敲击）；
- `interval_ms`：手动覆盖挥击间隔（50–5000）；
- `chase`（默认 `true`）：设为 `false` 时**原地站桩面向目标挥击**，不启动 `GoalFollow` 寻路追击，击杀后也**不会自动移动捡掉落物**（适合 1×2 窄矿道、门洞贴脸防身）；未传 `timeout` 时默认守点窗口 `1500ms`（可按需传入 `timeout` 范围 `50`–`60000`ms 等待怪物走进射程）；若校验目标 `0.8` 高度与 `0.5` 高度两条射线均被实心方块遮挡，会**立即返回 `blocked_by_wall`**（不继续等待 `timeout` 窗口），窗口结束仍够不到时返回 `out_of_reach`；
- `feedback`（默认 `false`）：设为 `true` 时在返回体中附带逐次挥击回执 `hit_log`（含 `hit`、`t_ms`、`entity_id`、`dist`、`target_pos`、`bot_hp`，遮挡时附带 `occluded: true`）和 `stop_reason`，同时推送 `attack_hit` 事件供 `/events` 实时观测；注意 `hit_log` / `attack_hit` 记录的是**客户端发起挥击回执**而非服务端伤害确认（`chase: true, feedback: true` 且目标被遮挡时，仍会按原版逻辑发起挥击并记录带 `occluded: true` 的回执）；默认 `false` 时保持与旧版完全一致的返回格式和事件流。

`/attack` 会自动寻路贴近、自动瞄准、自动换上剑/斧。需要自己控制方向、站位和节奏（跳劈暴击、隔缝挥砍、朝固定方向打）时，改用 `POST /control {"hit": ...}`，见 §4.16。


```
POST /attack  {"entity_id": 12345, "timeout": 20000}
```

```json
{"ok": true, "result": {"killed": true, "hits": 7, "ms": 5200, "inventory": {"rotten_flesh": 2}}}
```

跟随攻击直到目标死亡或超时；自动换近战武器、自动捡掉落。

### 4.7 远程攻击

```
POST /shoot  {"entity_id": 12345, "weapon": "bow", "charge_ms": 1100}
POST /shoot  {"pos": [120.5, 68.5, -31.5], "weapon": "crossbow"}
```

```json
{"ok": true, "result": {
  "fired": true, "weapon": "bow", "target": [120.5, 68.2, -31.5], "dist": 42.3,
  "charge_ms": 1100,
  "aim": {"flight_ticks": 17, "elevation_degrees": 8.75, "predicted_miss": 0.31},
  "projectiles": [{"id": 9001, "name": "arrow", "pos": [101, 66, -199]}],
  "used": {"arrow": 1}
}}
```

按原版重力和阻力计算弹道，对移动实体按水平速度做提前量（1.3.5 起才真正生效）；不穿墙、不追踪。弓需要箭，弩可用箭或烟花，三叉戟会被投出。

### 4.8 合成

```
POST /craft  {"item": "crafting_table", "count": 1}
```

```json
{"ok": true, "result": {"crafted": "crafting_table", "gained": 1, "used_table": false, "inventory": {"oak_planks": 0}}}
```

失败：`missing_ingredients`（附 `example_recipe_needs`、`inventory`）、`need_crafting_table`、`no_recipe`；工作台 5 秒内没打开返回 `no_window`（附 `table` 坐标、`pos`、`eye_dist`）；背包满、产物放不进去返回 `inventory_full`（附已做出的 `gained`、`empty_slots`、`inventory`；材料留在背包里，腾出一格再调即可）。

### 4.9 界面（窗口）

所有带界面的方块和生物只用两个接口：`POST /window/view`（看一眼就关）和 `POST /window/act`（打开 → 执行一串操作 → 关闭）。

完整请求体（`target` 三选一，`ops` 仅 `act` 需要）：

```json
POST /window/view  {"target": {"pos": [x, y, z]}}
POST /window/view  {"target": {"entity_id": 812}}
POST /window/view  {"target": {"block": "furnace"}}
POST /window/act   {"target": {"pos": [x, y, z]}, "ops": [{"op": "take", "from": "output", "count": 1}]}
```

也接受扁平写法 `{"pos": [x,y,z]}`、`{"x": 8, "y": 99, "z": 20}`、`{"block": "furnace"}`、`{"entity_id": 812}`——等价于把它们塞进 `target`。

```json
{"ok": true, "result": {
  "window": "furnace", "target": "furnace",
  "sections": {"input": [{"name": "raw_iron", "count": 3}], "fuel": [], "output": [{"name": "iron_ingot", "count": 1}]},
  "info": {"burning": true, "progress": 0.52, "fuel_left": 0.94}
}}
```

- `sections`：按名字划分的区域，不需要知道槽位编号。玩家背包不在这里，用 `/inventory` 查看。
- `info`：只读状态，例如熔炉进度、铁砧花费。
- `options`：可以 `select` 的选项，例如附魔台的三个附魔、村民的交易。
- 界面不会跨调用保持打开。想"先看再做"就先 `view` 再 `act`；附魔台、切石机等的选项要放好物品后才出现，所以 `put` 和 `select` 要写在**同一次** `act` 里。

#### `act` 的操作（4 种）

| op | 参数 | 作用 |
|---|---|---|
| `put` | `item`, `count?`, `to`, `slot?`, `potion?`, `enchanted?`, `custom_name?` | 从背包放进区域 `to`；`count` 省略则放全部。`slot` 指定区域内第几格（工作台按行 0–8）；`potion` 按药水种类挑；`enchanted` / `custom_name` 在同名多件时指定用哪件，不指定时优先放无附魔、未改名、耐久最好的那件 |
| `take` | `from`, `item?`, `count?` | 从区域取回背包；对成品区（`output`）取就是领取成品，铁砧会扣经验。只取背包放得下的量：放下了一部分时成功并带 `inventory_full: true`，一个都放不下时报 `inventory_full`，东西留在原处（成品区只能整组取，放不下整组就不取） |
| `select` | `option`, `times?` | 选 `options` 里的第几项（从 0 开始）；村民交易可用 `times` 连续做多次，次数用完或付不起时提前结束 |
| `text` | `value` | 文本输入（铁砧改名，最长 50 字符） |

```json
POST /window/act
{
  "target": {"block": "furnace"},
  "ops": [
    {"op": "take", "from": "output"},
    {"op": "put", "item": "raw_iron", "count": 8, "to": "input"},
    {"op": "put", "item": "coal", "count": 1, "to": "fuel"}
  ]
}
```

**Response 200**
```json
{
  "ok": true,
  "result": {
    "done": [
      {"op": "take", "took": {"iron_ingot": 3}, "from": "output"},
      {"op": "put", "put": "raw_iron", "count": 8, "to": "input"},
      {"op": "put", "put": "coal", "count": 1, "to": "fuel"}
    ],
    "window": { "...": "执行完后的界面描述" }
  }
}
```

**某一步失败时**：停止执行，界面照常关闭，返回已完成的步骤、失败的是第几步、失败时的界面：
```json
{
  "ok": false,
  "error": {
    "code": "section_full",
    "message": "input is occupied by 1 cobblestone; take it out first",
    "failed_op": 0,
    "done": [],
    "window": { "...": "失败时的界面描述" }
  }
}
```

`act` 返回 `{"done": [...每一步的结果], "window": {...执行完后的界面}}`。某一步失败时停止执行，界面照常关闭，返回 `error.failed_op`（第几步，从 0 开始）、`error.done`（已完成的步骤）、`error.window`（失败时的界面）。

#### 各界面的区域与选项

| 界面 | `window` | sections | options / info |
|---|---|---|---|
| 箱子、木桶、陷阱箱、末影箱、漏斗、投掷器、发射器、潜影盒 | `generic_9x3` 等 | `container` | — |
| 熔炉、高炉、烟熏炉 | `furnace` 等 | `input` `fuel` `output` | info：`burning`、`progress`（0–1）、`fuel_left`（0–1） |
| 工作台 | `crafting` | `grid`（`slot` 0–8，从左上按行）`output` | 合成建议直接用 `/craft` |
| 附魔台 | `enchantment` | `item` `lapis` | options：`index`、`level_required`、`lapis_cost`、`hint`（`{enchantment, level}`，提示的附魔）、`available` |
| 铁砧 | `anvil` | `left` `right` `output` | info：`repair_cost`（等级）；用 `text` 改名 |
| 砂轮 | `grindstone` | `top` `bottom` `output` | — |
| 锻造台 | `smithing` | `template` `base` `addition` `output` | — |
| 切石机 | `stonecutter` | `input` `output` | options：`index`、`get`（`{name, count}`）、`available`，放入原料后才有；info：`selected` |
| 织布机 | `loom` | `banner` `dye` `pattern` `output` | options：`index`、`pattern`（放了旗帜和染料后才有；放了图案物品时只有一项 `from_pattern_item`） |
| 酿造台 | `brewing_stand` | `bottles`（3 格）`ingredient` `fuel` | info：`brew_time_left`（秒）、`fuel_left`；`put` 药水时可用 `potion` 指定种类 |
| 信标 | `beacon` | `payment` | info：`beacon_level`、`primary`、`secondary`；options：`index`、`primary`、`secondary?`、`min_level`、`available`（需先放好付款） |
| 村民、流浪商人 | `merchant` | `payment1` `payment2` `output` | options：`index`、`give`、`get`、`uses`、`max_uses`、`available`；info：`villager_level`、`villager_xp` |

**示例**

```json
// 附魔：放剑和 3 个青金石，选第 3 个选项，取回剑
{"target": {"block": "enchanting_table"}, "ops": [
  {"op": "put", "item": "iron_sword", "to": "item"},
  {"op": "put", "item": "lapis_lazuli", "count": 3, "to": "lapis"},
  {"op": "select", "option": 2},
  {"op": "take", "from": "item"}
]}

// 村民交易：第 0 号交易做 4 次（服务器自动从背包付款）
{"target": {"entity_id": 812}, "ops": [{"op": "select", "option": 0, "times": 4}]}

// 铁砧改名
{"target": {"block": "anvil"}, "ops": [
  {"op": "put", "item": "iron_sword", "to": "left"},
  {"op": "text", "value": "Agent Blade"},
  {"op": "take", "from": "output"}
]}
```

**界面相关错误码**

| code | 含义 |
|---|---|
| `no_window` | 目标不会打开界面（例如对草方块调用） |
| `unsupported_window` | 界面类型暂不支持 |
| `bad_section` | 区域名不存在，或往成品区放东西；message 会列出可用区域 |
| `section_full` | 放不进去：区域被其他物品占着（message 会说是什么），或该槽位不接受这种物品 |
| `no_output` | 成品区是空的 |
| `bad_option` | 选项编号越界 |
| `option_unavailable` | 选项当前不可用：等级/青金石/付款不够、交易次数已用完；或服务器拒绝交出成品（经验不足） |
| `window_closed` | 执行中服务器关闭了界面 |
| `not_found` | `{"block": ...}` 在 32 格内找不到 |


### 4.10 装备

```
POST /equip  {"item": "diamond_sword", "slot": "hand"}
```

`slot`：`hand`、`off-hand`、`head`、`torso`、`legs`、`feet`。返回 `{"held": {...}}`（副手/护甲时返回 `{"equipped": {...}}`）。

### 4.11 姿态（潜行 / 举盾 / 持续跳跃）

```
POST /posture  {"sneak": true}
POST /posture  {"sneak": true, "shield": true}
POST /posture  {"jump": true}
POST /posture  {}
```

- **全量设置**：每次调用表达"我现在要的姿态"，没写的字段就是 `false`；`{}` 表示全部复位。
- **不占动作锁**：可以在 `move_to` 执行中途调用。约 55 ms 返回（等一个游戏 tick 让潜行 / 跳跃发出去；举盾是立即发的）。
- `sneak`：搭桥（`posture{sneak:true}` → `place` → `move_to` 循环）、经过 sculk sensor、原地潜行。
- `shield`：副手自动装备盾牌（背包里没有则 `missing_item`），举盾会取消疾跑。
- `jump`：**持续按住跳跃**。在水里等于自动踩水浮面（`posture{jump:true}` 之后可以一直停在水面，不会沉底溺亡）；陆地上会持续起跳，配合 `place` 可以原地垫高。
- 动作内部会清理按键状态（`stop`、被打断、切维度），服务端会在动作结束后按姿态重放，所以调用之间姿态保持。
- 死亡、断线、重生后姿态自动复位。

```json
{"ok": true, "result": {"sneaking": true, "jumping": false, "blocking": false, "offhand": null}}
```

### 4.12 载具

上船 / 上马 / 上矿车：`/interact {"entity_id": ...}`（`result.in_vehicle` 非空即成功）。

```
POST /dismount
```

```json
{"ok": true, "result": {"dismounted": true, "vehicle": {"id": 77, "name": "oak_boat"}, "pos": [12.5, 64, 10.5]}}
```

不在载具上返回 `not_in_vehicle`。矿车沿铁轨自动行驶，不需要驾驶；骑乘状态的转向/掌舵走 `POST /move_to`（见 4.1，会自动识别载具）。

### 4.13 丢弃

```
POST /drop  {"item": "dirt", "count": 32}
```

`result`：`{"inventory": {...}}`。

### 4.14 聊天

```
POST /chat  {"message": "Hello world!"}
```

`/chat` 不占用动作锁，可在移动、挖掘等动作执行期间调用，也不会松开 `/control` 保持的按键。角色需存活且已在目标服就绪。

限制：最长 100 字符、1 条/秒、禁止 `/` 开头的命令。`result`：`{"sent": true}`。

三种违规分别返回：

| 情况 | `error.code` | 说明 |
|---|---|---|
| 以 `/` 开头（含前置空格） | `bad_args` | 命令不通过聊天发送，避免越权执行 `/tp`、`/give` 等服务端指令 |
| 超过 100 字符 | `bad_args` | 附 `got` 实际长度 |
| 1 秒内第二条 | `rate_limited` | 附 `retry in XXXms` |

### 4.15 中断

```
POST /stop
```

```json
{"ok": true, "result": {"stopped": true, "action": "move_to", "released": true, "wait_ms": 21}}
```

清空寻路、停止挖掘、松开按键、放下物品，然后按当前姿态恢复（潜行仍然保持）。

**返回时动作锁已经释放**：`/stop` 会等正在执行的动作真正退出（通常几十毫秒，最多 5 秒）再返回，紧接着发的下一个动作不会撞 `busy`。`action` 是被停掉的动作名（没有动作在跑时为 `null`）；`released: false` 只在等满 5 秒仍未退出时出现。

被停掉的那个动作：有部分结果的照常 `ok: true` 并带 `interrupted_by: "stop"`（例如 `/control`）；否则返回 `interrupted`，`cause: "stop"`，`inner_code` 是停下时的原始错误（例如寻路被取消的 `path_not_found`）。被打断的原因由 `cause` 区分：`stop`（主动停止）、`hurt`、`death` 等。

---

## 5. 错误码

| code | 说明 |
|---|---|
| `invalid_credentials` / `no_profile` / `rate_limited` | 认证相关（`rate_limited` 也用于 `/chat` 限频） |
| `unauthorized` / `invalid_token` | Token 缺失或失效 |
| `not_connected` | Bot 未连接；动作执行中途被 `/disconnect`、踢出或断线也返回它 |
| `wrong_server` | 还没就绪（不在目标服务器上） |
| `backend_unavailable` | 暂时无法确认 bot 在目标服务器，等待后端确认恢复 |
| `not_ready` | 数据（如进度）还没下发；或登录 / 重生 / 切维度后世界还在加载（稍等重试） |
| `busy` | 有动作正在执行 |
| `dead` | 已死亡，等待重生 |
| `interrupted` | 动作被打断（附 `cause`：`stop` / `hurt` / …，以及 `nearest_hostile`、`inner_code`） |
| `path_not_found` / `timeout` | 寻路失败 / 超时 |
| `out_of_reach` / `out_of_range` | 够不到 / 没有可用弹道 |
| `no_such_entity` | 实体不在 bot 的实体列表里（消息写作 `entity N not visible`，但**不检查视线**，墙后的目标照样会寻路过去）：已死亡（死亡动画约 1 s 后移除）/ 消失、超出服务端实体追踪范围（取决于服务器的 `entity-tracking-range`，Spigot/Paper 默认生物与玩家都是 48 格）、或 id 来自上次登录 / 重生 / 切维度之前（这些情况 id 全部作废）。请用 `get_entities` 重新取 id，不要长期缓存 |
| `missing_item` / `missing_ammo` / `missing_ingredients` | 缺物品 / 缺弹药 / 缺材料 |
| `inventory_full` | `craft` 的产物 / `window/act` `take` 的物品放不进背包（没有空格，也没有能叠加的同种堆）；物品不会被扔到地上 |
| `need_crafting_table` / `need_furnace` / `no_recipe` | 合成相关 |
| `no_support` / `occupied` / `place_failed` / `blocked_by_self` | 放置相关 |
| `protected_region` | 服务端把改动回滚了 |
| `nothing_to_dig` / `undiggable` | 挖掘相关 |
| `dig_unconfirmed` | 挖完了但服务端没确认破坏，方块还在（见 4.2） |
| `use_window_api` | 该目标有界面，应用 `window/*` |
| `no_window` | 目标没有打开界面（`window/*`；`craft` 打不开工作台） |
| `cannot_interact` / `cannot_use` / `need_block_target` | 交互或使用物品失败 |
| `not_hungry` | 饱食度已满 |
| `not_in_vehicle` / `dismount_failed` | 载具相关 |
| `not_loaded` | 区块未加载 |
| `stalled` | 寻路 8 秒没有位移（卡在实体碰撞箱/未加载区块），换 `/control` 自救 |
| `bad_args` / `bad_json` / `unknown_api` | 请求不合法 |

### 4.16 底层键位控制（绕过寻路）

```
POST /control  {"forward": true, "jump": true, "yaw": 1.57, "ms": 400}
POST /control  {"hit": true, "yaw": 1.57, "pitch": -0.2}
POST /control  {"jump": true, "hit": 1, "hit_interval_ms": 450, "ms": 500}
POST /control  {"forward": true, "sprint": true, "yaw": 0.8, "ms": 200, "wait": false}
POST /control  {"wait": false}
```

不经过 mineflayer-pathfinder 的原生按键控制：后端 `setControlState` 按住指定键 `ms` 毫秒后自动松开，返回位移。用于 A* 处理不了的中间态：

- 下船后站在船体碰撞箱顶上（`on_ground:false`、`in_water:false`，寻路会永久死锁）——按 200ms 方向键即可脱离；
- 水里上浮 / 贴岸、助跑跳沟、原地 `jump` 配合 `place` 垫高；
- 微调站位（对齐箱子、漏斗、农田）；
- **朝指定方向挥砍**（`hit`）：不寻路、不自动瞄准、不换武器，按当前视线点左键——用于跳劈暴击、打船 / 矿车 / 末影水晶、隔着方块缝挥砍、自己控制节奏的走位战斗。

| 参数 | 说明 |
|---|---|
| `forward` / `back` / `left` / `right` | 方向键，布尔 |
| `jump` / `sneak` / `sprint` | 跳跃 / 潜行 / 疾跑，布尔 |
| `yaw` / `pitch` | 可选，弧度（mineflayer 约定，0 朝 -Z），先转向再走 / 再挥 |
| `hit` | 可选，`true` = 挥 1 下，整数 1–20 = 挥 N 下；第一下在开始时立即挥出 |
| `hit_interval_ms` | 两下之间的间隔，50–5000；默认按手持物品的原版攻击冷却（空手及非工具物品 250 / 剑 625 / 镐 833 / 锹 1000 / 斧 1000–1250、锄 250–1000 按材质 / 三叉戟 909 / 重锤 1667）。比冷却短也会照发，但原版伤害会按冷却进度打折 |
| `ms` | 按住时长，50–3000；默认 300，只给 `hit` 时默认刚好够挥完（`(hit-1)×间隔+50`）。给了 `ms` 但不够挥完会返回 `bad_args`（附 `required_ms`） |
| `wait` | 默认 `true`：按满 `ms` 再返回。`false`：**按下就返回**（服务端约 1 ms），键在后台按住 `ms` 后自动松开——见下面"实时操控" |

方向键和 `hit` 至少给一个。每一下按原版左键判定：从眼睛沿视线找 **3 格**内最近的实体判定箱（被方块挡住的不算；掉落物、经验球、箭等原版打不到的实体会跳过；船、矿车、末影水晶都能打到），命中就攻击，否则只挥手——**不会挖方块**，挖掘请用 `/dig`。`hits[].entity` 只说明攻击包发给了谁，伤害由服务端按原版规则结算：同一目标受击后有 0.5 秒无敌帧（间隔 < 500ms 时中间那几下通常不掉血），冷却没转满伤害打折，下落中命中才是暴击。

```json
{"ok": true, "result": {"ms": 52, "pos": [443.8, 63.4, 218.5], "moved": 0,
  "on_ground": true, "in_water": false, "yaw": 1.57, "pitch": -0.2,
  "hits": [{"t_ms": 0, "entity": {"id": 812, "name": "zombie", "dist": 2.41}}],
  "hit_interval_ms": 625, "held": "iron_sword"}}
```

没命中时那一下是 `{"t_ms": 0, "entity": null, "block": {"name": "stone", "pos": [445, 63, 218]}}`（视线 3 格内没有方块则没有 `block`）。

动作锁语义与其它动作一致（会占用动作锁，有别的动作在跑时返回 `busy`，要先 `/stop`；默认 `stop_on` 含 `hurt`——近身对打时建议传 `"stop_on": []`）；结束时会重放当前 `/posture`。

**实时操控（`"wait": false`）**：按下指定键、转好视角、挥出第一下就立刻返回，不等 `ms`。适合 agent 按自己的节奏（例如每 50–100 ms 一次）连续发指令，像玩家按住键一样操控：

- 键在后台按住 `ms` 毫秒后自动松开；`hit` 大于 1 时后面几下按间隔在后台挥出。返回 `{"held": ["forward", "sprint"], "release_in_ms": 200, "pos": …, "on_ground": …, "yaw": …, "pitch": …}`（有 `hit` 时另带第一下的 `hits` 和 `scheduled_hits`）。
- **新的 `/control` 立刻接管**：先松开上一次留下的键（以及没挥完的几下），再按新的。想一直走就在松开前续上，例如每 100 ms 发一次 `ms: 200`。
- `{"wait": false}` 什么键都不给 = 立刻全部松开（返回 `released`）。`/stop`、死亡、开始任何其它动作（`move_to`、`dig`……）也会先松开。
- 只在设置按键的那一刻占用动作锁，返回后锁就释放了：按住期间可以调 `/look`、`/posture`、观察接口，也可以开始别的动作（会先松开按键）。
- `/status.held_controls` 显示当前按住的键和剩余毫秒（没有时为 `null`）。
- 键的效果从下一个物理 tick（≤ 50 ms）开始；客户端到 API 的网络往返（校内约 0.1 s，公网 0.5–1 s）仍然要算上，所以离 API 越近越"实时"。

### 4.17 主动转向

```
POST /look  {"yaw": 1.57, "pitch": 0}
POST /look  {"x": 448, "y": 64, "z": 220}
POST /look  {"entity_id": 812}
```

设置视角（弧度）或朝向一个坐标/实体自动计算。不占动作锁，常用于观察、定向放置（漏斗/活塞朝向）、配合 `/control` 走位。

```json
{"ok": true, "result": {"yaw": 1.57, "pitch": -0.02}}
```

---

## 6. 示例

### Python

```python
import time

import requests

BASE = "http://127.0.0.1:18900/api/v1"

r = requests.post(f"{BASE}/auth/login", json={"username": "user", "password": "pass"}).json()
token = r["token"]
H = {"Authorization": f"Bearer {token}"}

def call(path, body=None, method="POST"):
    """统一取 result / error，避免每个接口各写一遍。"""
    if method == "GET":
        payload = requests.get(f"{BASE}/{path}", headers=H).json()
    else:
        payload = requests.post(f"{BASE}/{path}", headers=H, json=body or {}).json()
    if not payload["ok"]:
        raise RuntimeError(payload["error"])
    return payload["result"]

while not call("status", method="GET")["ready"]:      # 等 bot 上线
    time.sleep(1)

s = call("status", method="GET")
print(s["pos"], s["health"], s["armor_points"], s["effects"])

log = call("find_blocks", {"types": ["oak_log"], "radius": 32, "limit": 3})["blocks"][0]
print(call("dig", {"pos": log["pos"]}))                 # 观察结果可以直接回填
```

### curl

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:18900/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"user","password":"pass"}' | jq -r .token)

curl -s http://127.0.0.1:18900/api/v1/status -H "Authorization: Bearer $TOKEN"

curl -s -X POST http://127.0.0.1:18900/api/v1/posture \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"sneak":true}'
```

---

## 7. 注意事项

1. **动作互斥**：一次一个占锁动作，重复调用返回 `busy`。聊天可与动作并发。
2. **重连间隔**：断开后至少 30 秒。
3. **方块探测**：只返回暴露方块，无矿透。
4. **战斗**：不提供自动反击，被打断时返回 `nearest_hostile` 供决策。
5. **配方**：未解锁配方的合成会慢一些。
6. **已知限制**：
   - `move_to` 走寻路：卡在实体碰撞箱（例如站在船顶）或未加载区块上时，8 秒没位移就会报 `stalled` 而不是一直占着动作锁；这时用 `/control` 的 200ms 方向键或 `jump` 自救；
   - 在水里做 `move_to` 时，默认 `stop_on` 含 `hurt`，溺水扣血会打断上浮；要么先 `POST /posture {"jump": true}` 浮起来，要么显式传 `"stop_on": []`；
   - 载具掌舵只能"朝目标直线开"，不会绕障；撞上地形会一直顶住直到超时（`drive_timeout`）；
   - 不支持鞘翅飞行、逐 tick 键位控制；
   - `use_item` 钓鱼在人工小水池里可能长时间不咬钩，建议在自然水域使用（对判定有疑问时看 `caught` 是否为空）。

---

## 8. 版本历史

| 版本 | 日期 | 变更 |
|---|---|---|
| 1.3.19 | 2026-11-21 | `/chat` 可与动作并发，不占用动作锁或松开控制键 |
| 1.3.18 | 2026-11-21 | 修复被鱼竿钩中后的拉动，并按协议保留、旋转或替换传送后的速度 |
| 1.3.17 | 2026-11-21 | 关闭死亡循环保护，连续死亡后保留会话并自动重生 |
| 1.3.16 | 2026-11-20 | 按认证角色固定分配；分配表异常暂停新登录；切服和确认中断期间暂停世界接口，RCON 恢复后同一会话继续 |
| 1.3.15 | 2026-11-20 | 支持按角色分配代理后端，`/status.server` 显示目标；修复 Velocity 切服后世界就绪状态未恢复，以及断线时动作报错；限制 HTTP 请求体为 1 MiB |
| 1.3.14 | 2026-11-16 | 新增 `/control` `"wait": false`（实时操控）：按下就返回（服务端约 1 ms，以前要等满 `ms`），键在后台按住 `ms` 后松开，下一个 `/control` 立刻接管；`{"wait": false}` 空调用立即松开；`/status` 新增 `held_controls` |
| 1.3.13 | 2026-11-16 | 23 个 bot 压测后的修复：① **每个 bot 一个进程**——以前所有 bot 共用一个 Node 线程，任何一个 bot 寻路或大范围 `find_blocks` 都会拖慢所有人，23 个 bot 同时干活时 `status` p99 4.5 s、`control` p99 4 s；现在同样负载下 `status` p99 3 ms、`control`（ms=100）p99 106 ms，全部 bot 满负荷重计算时 `status` p99 约 50 ms；② 后端在线人数超过 13 时，服务器 RCON `list` 只列前 13 个名字，之后的 bot 会被误判为"不在后端"而反复 `/server`、断线重登（在线人数在 20–23 之间跳）——现在被截断时逐个确认；③ `find_blocks` 改为直接按方块状态扫描并分段让出：半径 64 从约 0.4–3.6 s 降到约 0.1 s；以前的实现按区块"八面体"遍历，会漏掉对角方向、但仍在半径内的方块，现在结果更完整（数量会比以前多）；④ 重连途中调用 `get_block` / `find_blocks` / `get_entities` / `inventory` 返回 `not_ready`，以前是 `internal`；⑤ 寻路每个 tick 最多计算 20 ms（以前 40 ms） |
| 1.3.12 | 2026-11-15 | 延迟：① 服务监听 `127.0.0.1` 或 `0.0.0.0` 时额外监听 `[::1]`——以前 Windows 客户端用 `http://localhost:...` 时每个新连接先在 IPv6 上白等（实测 Python 2 s、Node 20+ fetch 约 0.25 s）；② `/posture` 固定等待 150 ms → 55 ms（一个 tick），服务器端实测每次都已看到潜行状态 |
| 1.3.11 | 2026-11-15 | 修复背包满时物品被扔到地上：① `/window/act` `take` 从箱子 / 熔炉成品槽等取东西，以前把东西扔到地上并报 `missing_item` / `no_output` "nothing to take"（取 2 个也会把整组 5 个扔掉），现在只取放得下的量（部分成功带 `inventory_full: true`），一个都放不下时报 `inventory_full`、东西留在原处；② `/dig` 没有合适工具时会先把手上的东西收进背包，背包满时会把手上整组扔掉（实测 64 钻石），现在直接拿着它挖 |
| 1.3.10 | 2026-11-14 | 修复：① `/dig` 挖**斜下方**的方块（例如脚下往外 1 格、往下 3 格、往前 3 格）原地 1 ms 返回 `out_of_reach`——"已经够得着"的判断按脚到方块角算，实际挖掘按眼睛到方块中心算，两者不一致；现在会先走过去；② `/interact` 用**玻璃瓶**对着水装水、放**睡莲 / 蛙卵**完全无效（`changed: false`）——现在和桶一样按视线判定，`/place` 也能放睡莲；③ 1.3.8 起**桶对炼药锅**会把水倒在炼药锅上面、空桶舀不出炼药锅里的水——炼药锅恢复为普通右键方块；④ 动作执行中途被 `/disconnect` 或断线时返回 `internal "Cannot read properties of null"`，现在返回 `not_connected`；⑤ `/craft` 打不开工作台时要等 20 s 才返回 `internal "Event windowOpen did not fire"`，现在 5 s 返回 `no_window` 并附坐标与距离；⑥ 背包满时 `/craft` 空转约 30 s 后报 `missing_ingredients "could not craft X: null"`（产物取不出来却当作成功，重试 64 轮），现在约 1 s 返回 `inventory_full` |
| 1.3.9 | 2026-11-14 | 修复：手持**附魔工具**时 `/dig`（以及会顺路挖方块的寻路）返回 `internal`：`enchantments is not iterable`。1.21 物品组件下 prismarine-item 的 `item.enchants` 是对象而不是数组，mineflayer 算挖掘时间时遍历它就崩；附魔头盔则被静默忽略。现在附魔从物品组件读取，效率附魔也正确计入挖掘时间（效率 V 铁镐挖石头约 0.13 s，普通铁镐约 0.45 s） |
| 1.3.8 | 2026-11-14 | 修复：① `/stop` 不等动作退出就返回，`/control` 会继续按满 `ms`，紧接着的下一个动作撞 `busy`——现在 `/stop` 返回时锁已释放，返回值新增 `action`、`released`、`wait_ms`，被停掉的动作 `cause: "stop"`；② 死亡重生、切换维度后 `ready` 不变 `false`，约 0.25 s 内 `get_block` 返回 `not_loaded`、`get_entities` 为空——现在重新等世界到齐，期间动作接口返回 `not_ready`；③ `/interact` 用桶装水 / 倒水完全无效（返回 `changed: false`）——现在按原版视线判定，坐标约定见 4.4。文档：4.9 补全界面表；4.5 写明满饱食度时哪些物品仍可使用 |
| 1.3.7 | 2026-11-13 | `/status` 新增 `speed`（水平速度，格/秒）与 `velocity`（三轴速度，格/秒），骑乘时为 `null`（社区贡献 PR #1） |
| 1.3.6 | 2026-11-13 | 修复：① `/dig` 在服务端**没接受**破坏时也报成功（mineflayer 挖完先把本地方块改成空气，服务端拒绝时不回滚）——现在只认服务端发来的方块变化，否则返回新错误码 `dig_unconfirmed` 并恢复本地缓存；② bot **挤不动**（原版由客户端自己算实体挤压，mineflayer 没实现）——现在会被重叠的生物 / 玩家 / 船 / 矿车推开；③ `ready` 变 `true` 时周围实体还没到齐、`get_entities` 可能为空——现在 `ready` 会等区块和实体到齐（进服约 2–3 s）；④ 手持非工具物品时默认挥击间隔误算成 500ms（应为 250ms），斧 / 锄按材质区分；⑤ 刚进服时 `/status` 偶发 `internal` 错误；⑥ 进服第 4 秒的内部位置校正会把速度清零，恰好在那时挨打时击退被吞 |
| 1.3.5 | 2026-11-13 | 修复：agent 挨打**不会被击退**（1.21.9+ 的速度包改了编码，mineflayer 仍按旧格式换算，速度被缩小 8000 倍，bot 原地不动）。现在被打会像原版玩家一样被推开约 2 格；其他实体的速度也变成真值，`/shoot` 的移动目标提前量随之生效（只用水平速度） |
| 1.3.4 | 2026-11-12 | 修复：睡着时再点床**起不来**（mineflayer 的起床包在 1.21.11 协议里被当成"停止疾跑"，服务端不认；以前只在全员入睡、服务端跳夜时才会被叫醒）。现在 `woke: true` 表示服务端已确认下床，2 s 内没下床返回 `cannot_sleep`。早夜（原版 12542 起）睡觉也如实返回 `slept: true` |
| 1.3.3 | 2026-11-12 | `POST /control` 新增 `hit`（按当前视线原版左键，可连挥 N 下）与 `hit_interval_ms`；结果新增 `pitch`、`hits`、`hit_interval_ms`、`held` |
| 1.3.2 | 2026-11-12 | 钓鱼咬钩判定修正（只在浮标入水后判定，基准取最近 1 秒最高点），误判自动重抛，结果新增 `casts`、`false_bites`，没钓到改返回 `timeout`；`hurt` 事件新增 `source`、`source_entity`；`interrupted` 错误保留原始错误的附加字段与 `inner_code` |
| 1.3.1 | 2026-11-03 | `POST /move_to` 支持骑乘掌舵：`status.in_vehicle` 非空时自动切载具模式（客户端权威，逐帧 `vehicle_move`），船 8 / 炽足兽 4 / 猪 5 / 马 9 格每秒，返回 `drove`、`vehicle`、`dist` |
| 1.3 | 2026-11-02 | 统一 `{ok,ms,result}` 封装与 `pos` 命名；`/interact` 支持实体；新增 `/use_item`（吸收 `/consume`）、`/posture`、`/dismount`；`/place` 自动潜行；`/status` 补齐效果、着火、水中、载具、姿态、护甲、经验进度等字段；事件流改为 `seq` |
| 1.2 | 2026-10-19 | 弓、弩、三叉戟远程攻击与弹道计算 |
| 1.1 | 2026-10-15 | advancement 查询 |
| 1.0 | 2026-10-10 | 初版 |
