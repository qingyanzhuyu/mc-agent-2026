# Minecraft Agent API 规范 v1.3

> 竞赛用 Bot 控制接口。v1.3 与 v1.2 **不兼容**（内测期不做兼容层）：统一响应封装、统一坐标命名、`/consume` 并入 `/use_item`，并新增 `/posture`、`/dismount`。

## 基础信息

- **Base URL**：`https://mc-api.windywindie.top/api/v1`（Cloudflare Tunnel 公网入口，HTTPS）
- **认证**：Bearer Token（从皮肤站账号换取）
- **动作互斥**：同一时间只能执行一个动作，重复调用返回 `busy`

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
{ "username": "皮肤站用户名", "password": "皮肤站密码" }
```

**Response 200**

```json
{
  "ok": true,
  "token": "64 位十六进制字符串",
  "expires_at": "2026-11-03T12:00:00.000Z",
  "bot_username": "Test_Agent"
}
```

**如果您还没有皮肤站账号，请在 https://skin.nmo.net.cn/ 上注册**

| HTTP | `error.code` | 说明 |
|---|---|---|
| 401 | `invalid_credentials` | 用户名或密码错误 |
| 401 | `no_profile` | 皮肤站账号下没有选定角色 |
| 429 | `rate_limited` | 皮肤站限频（`retry_after_s` 为空，等几秒重试） |
| 429 | `rate_limited` | **本服务限流**：同一 IP 每分钟最多 10 次失败，超过锁 60 秒；全局每分钟最多 60 次尝试。带 `retry_after_s` 与 `Retry-After` 响应头 |

限流的客户端身份取 `CF-Connecting-IP`（Cloudflare 隧道注入），没有则取 `X-Forwarded-For` / socket 地址；登录成功会清零该 IP 的失败计数。

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

`result`：`{"ok": true}`；已在线时 `{"ok": true, "already_connected": true}`。断线重连需间隔 ≥ 30 秒，死亡循环冷却期间返回 `death_loop_cooldown`（附 `cooldown_remaining_seconds`）。

API 自己会维护"是否在比赛服"（`status.ready`）：登录后自动 `/server survivaltest`，被后端踢回大厅（proxy lobby）时会在 5–15 秒内自动重发并恢复。`ready` 由**比赛服侧**确认——API 会核对 bot 是否真的出现在比赛服玩家列表里，而不是只看 `/server` 的回话：所以即使代理回 `already connected`、或后端把 advancement 数据分几次下发，只要 bot 确实在比赛服，`ready` 也会置 `true`。如果连续几次重发都没有真正切过去，API 会在 30 秒左右把整条连接重登一次（`events` 里会出现 `reconnect`），不会一直停在 `ready=false`。如果已经连着但 `ready` 仍为 `false`，`/connect` 会**立刻再发一次** `/server survivaltest` 并返回：

```json
{"ok": true, "already_connected": true, "resent_server_switch": true, "ready": false}
```

等待几秒后重新 `GET /status`，`ready` 变 `true` 就可以正常调用动作接口。`ready=false` 时动作接口返回 `wrong_server`，这是有意为之——此时 bot 在大厅，挖方块/攻击都会打空。

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
    "username": "Test_Agent",
    "pos": [-76.5, 102, 37.5],
    "yaw": 1.57,
    "dimension": "overworld",
    "gamemode": "survival",
    "health": 20,
    "food": 20,
    "saturation": 5,
    "xp_level": 0,
    "xp_progress": 0.42,
    "oxygen": 300,
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
    "busy": null,
    "nearest_hostile": {"id": 812, "name": "zombie", "dist": 12.5},
    "last_death": {"pos": [80, 63, -180], "t": 1791881333488, "cause": "Test_Agent was slain by Zombie"}
  }
}
```

| 字段 | 说明 |
|---|---|
| `ready` | **可以调用动作接口了**：已确认在比赛服且存活 |
| `busy` | 正在执行的动作名，空闲为 `null` |
| `xp_progress` | 当前等级内的经验进度 0–1（配合 `xp_level` 判断离 30 级还差多少） |
| `armor_points` | 当前护甲值（0–20），按护甲属性或装备换算 |
| `effects` | 药水效果列表；`level` 从 1 起（原版 amplifier + 1） |
| `on_fire` | 是否着火（**不是**药水效果，来自实体 flags） |
| `in_water` | 身体是否浸在水里（含水草/海带这类水方块） |
| `submerged` | 头部是否在水下（配合 `oxygen` 判断溺水） |
| `in_lava` | 是否站在岩浆里 |
| `in_vehicle` | `{"id","name"}` 或 `null` |
| `sneaking` / `blocking` | 当前姿态，与 `/posture` 对应 |
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
| `types` | 无 | 方块名数组；省略时返回附近方块**种类摘要** |
| `radius` | 16 | 1–64，超出范围返回 `bad_args` |
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

约束：只返回**暴露方块**（至少一面邻接空气或透明方块），不提供矿透能力。

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
| `hurt` | `health`、`damage`、`likely_attacker`（16 格内最近的敌对生物） |
| `death` | `pos`、`t`、`dropped_items_at` |
| `spawn` | `pos` |
| `pickup` | `item`、`count` |
| `effect_start` / `effect_end` | `name`、`level`、`duration_s` |
| `sleep` / `wake` | 无 |
| `vehicle` | `action`（`mount`/`dismount`）、`name` |
| `kicked` / `disconnected` | `reason` |
| `death_loop` | `deaths`、`cooldown_until` |
| `server_confirmed` | `server`、`via` |

### 3.7 进度

```
POST /advancements  {"status": "completed", "include_recipes": false}
```

返回 `result.{ready, updated_at, packets_received, summary, advancements[]}`；每项含 `id`、`title`、`frame`、`status`、`completed_at`、`progress`。只包含服务器已下发的可见进度树。

---

## 4. 动作接口

所有动作会自动寻路到可操作距离。通用参数 `stop_on`（默认 `["hurt","death"]`），`attack`/`shoot` 默认只中断于 `death`。

失败时返回 `error.code`，可能附带 `interrupted_by`、`health`、`pos`、`nearest_hostile`。

### 4.1 移动

```
POST /move_to  {"x": 100, "z": -200, "range": 1}
POST /move_to  {"entity_id": 12345, "range": 2}
```

省略 `y` 时只要求到达水平坐标。`result`：`{"pos": [100.2, 64.0, -199.8]}`；跨维度时附 `dimension_changed: {"from","to"}`。

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

### 4.3 放置方块

```
POST /place  {"item": "cobblestone", "pos": [100, 65, -200]}
```

不给坐标时自动在附近选落点。`place` 会在放置瞬间**自动潜行**，因此可以直接在箱子、工作台、熔炉、床上放方块；`result.sneaked: true` 表示这次做了保护。

```json
{"ok": true, "result": {"placed": "cobblestone", "pos": [100, 65, -200], "sneaked": true}}
```

若参照方块弹出了界面（说明这个位置应该走窗口接口），会自动关窗并返回 `use_window_api`。

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

**床**：睡着返回 `{"slept": true}`；已经睡着再点一次是起床，返回 `{"woke": true}`。

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
| `target` | 无 | `{"entity_id"}` 或 `{"pos"}`；先转头对准（望远镜、喷溅药水、末影之眼、钓鱼需要） |
| `ms` | 1000 | 保持时长（盾牌、望远镜），上限 10000 |
| `timeout` | 30000 | 等待时长（吃东西、钓鱼） |
| `source` | `hand` | `off-hand` 可指定副手 |

按物品类别分派，**等到效果真正结束**才返回：

| 物品 | 行为 | `result` |
|---|---|---|
| 食物、药水、牛奶桶、蜂蜜瓶 | 吃完 / 喝完 | `used`、`food_before`、`food`、`health_before`、`health`、`effects` |
| 盾牌 | 举盾保持 `ms` | `used`、`blocked_ms` |
| 末影珍珠 | 投掷 | `used`、`thrown`、`teleported`、`pos` |
| 末影之眼 | 投掷并读出方位 | `used`、`thrown`、`landed_at`、`eye_vector`、`bearing_deg` |
| 雪球、鸡蛋、风弹、喷溅 / 滞留药水、烟花、经验瓶 | 投掷 | `used`、`thrown` |
| 钓鱼竿 | 抛竿 → 等咬钩 → 收竿 | `used`、`caught`、`wait_ms` |
| 望远镜 | 对准目标观察 `ms` | `used`、`observed_ms`、`target?` |
| 山羊角 | 吹响 | `used` |
| 船 / 矿车 | 放在面前的地上 | `used`、`entity`、`pos` |

`bearing_deg` 用 MC 的 yaw 约定：0 = 正南（+Z），90 = 正西（−X）。

**限制**：打火石、水桶、种子、骨粉、锄头必须对着方块用，会返回 `need_block_target`，请改调 `/interact` 并带上 `item`。其它没有空放行为的物品返回 `cannot_use`。

### 4.6 攻击

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

按原版重力和阻力计算弹道，对移动实体做提前量；不穿墙、不追踪。弓需要箭，弩可用箭或烟花，三叉戟会被投出。

### 4.8 合成

```
POST /craft  {"item": "crafting_table", "count": 1}
```

```json
{"ok": true, "result": {"crafted": "crafting_table", "gained": 1, "used_table": false, "inventory": {"oak_planks": 0}}}
```

失败：`missing_ingredients`（附 `example_recipe_needs`、`inventory`）、`need_crafting_table`、`no_recipe`。

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

`act` 支持 4 种操作：`put`（`item`、`count`、`to`、`slot`、`potion`、`enchanted`、`custom_name`）、`take`（`from`、`item`、`count`）、`select`（`option`、`times`）、`text`（`value`）。

某步失败时停止并返回 `error.failed_op`、`error.done`、`error.window`。完整界面表（箱子、熔炉、工作台、附魔台、铁砧、砂轮、锻造台、切石机、织布机、酿造台、信标、村民）与 v1.2 相同。

### 4.10 装备

```
POST /equip  {"item": "diamond_sword", "slot": "hand"}
```

`slot`：`hand`、`off-hand`、`head`、`torso`、`legs`、`feet`。返回 `{"held": {...}}`（副手/护甲时返回 `{"equipped": {...}}`）。

### 4.11 姿态（潜行 / 举盾）

```
POST /posture  {"sneak": true}
POST /posture  {"sneak": true, "shield": true}
POST /posture  {}
```

- **全量设置**：每次调用表达"我现在要的姿态"，没写的字段就是 `false`；`{}` 表示全部复位。
- **不占动作锁**：可以在 `move_to` 执行中途调用。
- `sneak`：搭桥（`posture{sneak:true}` → `place` → `move_to` 循环）、经过 sculk sensor、原地潜行。
- `shield`：副手自动装备盾牌（背包里没有则 `missing_item`），举盾会取消疾跑。
- 动作内部会清理按键状态（`stop`、被打断、切维度），服务端会在动作结束后按姿态重放，所以调用之间姿态保持。
- 死亡、断线、重生后姿态自动复位。

```json
{"ok": true, "result": {"sneaking": true, "blocking": false, "offhand": null}}
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
{"ok": true, "result": {"stopped": true}}
```

清空寻路、停止挖掘、松开按键、放下物品，然后按当前姿态恢复（潜行仍然保持）。

---

## 5. 错误码

| code | 说明 |
|---|---|
| `invalid_credentials` / `no_profile` / `rate_limited` | 认证相关（`rate_limited` 也用于 `/chat` 限频） |
| `unauthorized` / `invalid_token` | Token 缺失或失效 |
| `not_connected` | Bot 未连接 |
| `wrong_server` | 不在比赛服 |
| `not_ready` | 数据（如进度）还没下发 |
| `death_loop_cooldown` | 死亡循环保护中 |
| `busy` | 有动作正在执行 |
| `dead` | 已死亡，等待重生 |
| `interrupted` | 动作被打断（附 `interrupted_by`、`nearest_hostile`） |
| `path_not_found` / `timeout` | 寻路失败 / 超时 |
| `out_of_reach` / `out_of_range` | 够不到 / 没有可用弹道 |
| `no_such_entity` | 实体不存在或已消失 |
| `missing_item` / `missing_ammo` / `missing_ingredients` | 缺物品 / 缺弹药 / 缺材料 |
| `need_crafting_table` / `need_furnace` / `no_recipe` | 合成相关 |
| `no_support` / `occupied` / `place_failed` / `blocked_by_self` | 放置相关 |
| `protected_region` | 服务端把改动回滚了 |
| `nothing_to_dig` / `undiggable` | 挖掘相关 |
| `use_window_api` | 该目标有界面，应用 `window/*` |
| `cannot_interact` / `cannot_use` / `need_block_target` | 交互或使用物品失败 |
| `not_hungry` | 饱食度已满 |
| `not_in_vehicle` / `dismount_failed` | 载具相关 |
| `not_loaded` | 区块未加载 |
| `bad_args` / `bad_json` / `unknown_api` | 请求不合法 |

---

## 6. 示例

### Python

```python
import time

import requests

BASE = "https://mc-api.windywindie.top/api/v1"

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
TOKEN=$(curl -s -X POST https://mc-api.windywindie.top/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"user","password":"pass"}' | jq -r .token)

curl -s https://mc-api.windywindie.top/api/v1/status -H "Authorization: Bearer $TOKEN"

curl -s -X POST https://mc-api.windywindie.top/api/v1/posture \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"sneak":true}'
```

---

## 7. 注意事项

1. **死亡循环保护**：60 秒内死亡 3 次，bot 自动下线 5 分钟。
2. **动作互斥**：一次一个动作，重复调用返回 `busy`。
3. **重连间隔**：断开后至少 30 秒。
4. **方块探测**：只返回暴露方块，无矿透。
5. **战斗**：不提供自动反击，被打断时返回 `nearest_hostile` 供决策。
6. **配方**：未解锁配方的合成会慢一些。
7. **已知限制**：
   - 载具掌舵只能"朝目标直线开"，不会绕障；撞上地形会一直顶住直到超时（`drive_timeout`）；
   - 不支持鞘翅飞行、逐 tick 键位控制；
   - `use_item` 钓鱼在人工小水池里可能长时间不咬钩，建议在自然水域使用（对判定有疑问时看 `caught` 是否为空）。

---

## 8. 版本历史

| 版本 | 日期 | 变更 |
|---|---|---|
| 1.3.1 | 2026-11-03 | `POST /move_to` 支持骑乘掌舵：`status.in_vehicle` 非空时自动切载具模式（客户端权威，逐帧 `vehicle_move`），船 8 / 炽足兽 4 / 猪 5 / 马 9 格每秒，返回 `drove`、`vehicle`、`dist` |
| 1.3 | 2026-11-02 | 统一 `{ok,ms,result}` 封装与 `pos` 命名；`/interact` 支持实体；新增 `/use_item`（吸收 `/consume`）、`/posture`、`/dismount`；`/place` 自动潜行；`/status` 补齐效果、着火、水中、载具、姿态、护甲、经验进度等字段；事件流改为 `seq` |
| 1.2 | 2026-10-19 | 弓、弩、三叉戟远程攻击与弹道计算 |
| 1.1 | 2026-10-15 | advancement 查询 |
| 1.0 | 2026-10-10 | 初版 |
