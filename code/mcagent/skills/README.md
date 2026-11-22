# skills —— 执行层

**职责**：一个目标 → 一串 `Executor` 动作。**不写 HTTP，不写决策**。
规则的权威版本在仓库根 `AGENTS.md` §9；这里只说怎么上手。

```
base.py       Skill 基类 / Param 参数声明 / rec() / failure()
registry.py   @register、REGISTRY、make()、契约检查、自动发现
items.py      只读选择器：最好的镐、垫块、空气判断
sightline.py  眼高（1.62/1.27）、方块中心眼距与 3D 体素视线遮挡检查（只读）
nav.py        L2 战术原语：breach_1x2 / sneak_bridge / step_up_1x2
testing.py    离线夹具：GridWorld 替身 + drive()
<领域>.py      L3 意图技能
```

## 两层

| | L3 意图技能 | L2 战术原语 |
|---|---|---|
| 形式 | `Skill` 子类 + `@register` | 普通生成器函数 |
| 谁调 | 模型、规则队列 | 只有 L3 和 `probe_path.py` |
| 参数 | 物品名、数量这类标量 | 当场观察到的方块坐标 |
| 模型能选到吗 | 能 | 不能 |

## 唯一的架构约束

> 每发一个阻塞动作就 `yield` 一次。

Safety 闸只能在动作之间抢占。反例：

```python
# 错：一步连做 20 个动作，血量掉到 2 没人知道
def run(self, ex, state):
    for b in blocks:
        ex.move_to(pos=b); ex.dig(b)
    yield
```

正确写法——代码照样是从上往下读的一条线，只是每个动作后停一下：

```python
def run(self, ex, state):
    st = state.refresh_status(ex.client, max_age_s=0)
    for b in blocks:                                  # 观察随便调，不用 yield
        if not sightline.can_reach_block(ex, st, b, reach=sightline.DIG_EYE_REACH):
            yield rec("move_to", ex.move_to(pos=b, rng=3), pos=b)
            st = state.refresh_status(ex.client, max_age_s=0)
        yield rec("dig", ex.dig(b), pos=b)
```

基类的 `step()` 每次只推进到下一个 `yield`，所以对主循环来说它仍然是"一步一动作"的状态机。
要用 L2 原语就 `yield from nav.breach_1x2(ex, state, [...])`。

## 眼高、方块中心与视线遮挡（强制复用 `sightline`）

根据 `docs/99-原始记录/api-spec-v1.3.11.md` §3.1 / §3.3 / §4.2 / §8 1.3.10 ①：
- `/status` 返回的 `pos` 是**玩家脚底坐标**（站立眼高 `+1.62`，潜行 `status.sneaking == True` 时 `+1.27`）；
- `/find_blocks` 与 `/get_block` 的 `pos` 是**整数格角点** `[bx, by, bz]`，几何中心为 `[bx + 0.5, by + 0.5, bz + 0.5]`。

编写任何技能或原语判断挖掘、工作站交互、瞄准距离与可达性时，**必须复用 `skills.sightline`**
（`eye_pos` / `eye_to_block_dist` / `can_see_block` / `can_reach_block` 及 `DIG_EYE_REACH`、`API_DIG_EYE_REACH`、`STATION_EYE_REACH`），
**禁止**手写 `math.dist(state.pos, block_pos)` 脚底角点距离或跳过 3D 体素视线遮挡检查。

## 加一个技能

完整模板和四步流程见 `AGENTS.md` §9.4。要点：

- 类属性声明 `name` / `description` / `params` / `goal`，加 `@register`——**不用改任何名单**，菜单和校验自动跟上。
- `blocked(state)` 返回空串表示可以进菜单，否则返回一句原因（模型会看到）。
- 参数只许 `str/int/float/bool`，不许叫 `pos/x/y/z/entity_id`——注册时会直接报错。
- 要收尾的状态（潜行、举盾）写在 `finally` 里，且从进入该状态起就在 `try` 里面。

## 离线测试

```python
from mcagent.skills import nav
from mcagent.skills.testing import GridWorld, drive

world = GridWorld(blocks={(1, 64, 0): "stone"}, items={"wooden_pickaxe": 1})
ex, state = world.session()
steps = drive(nav.breach_1x2(ex, state, [[1, 64, 0]]), ex)   # 一步发两个动作会断言失败
assert [s["op"] for s in steps] == ["equip", "dig"]
```

`GridWorld` 只验证控制流：没有重力、没有挖掘时间、没有寻路失败。过了这里是 `verified: offline`，
离 `verified: live` 还差一次真机。

## 失败处理

- 前置条件不成立（找不到目标、缺材料）→ 抛 `runtime.Replan`，文案写清楚缺什么——它会作为"最近失败原因"回喂给模型
- 认证/接口级错误 → `Executor` 会抛 `runtime.Fatal`，整个停
- 同一目标反复失败 → 基类 `mark_failed()` / `is_cooling()` 按 `goal` 冷却 30 秒
- 被循环切走 → 循环调 `abort()`，`finally` 收尾；下次新建实例从头观察

## T1 公共接口

本节所列脚本为 `verified: offline`。L3 用 `yield from` 调用含动作的脚本，接收其生成器返回值。
观察和纯计算直接调用。坐标、实体及窗口目标取自当场官方 API 观察。状态变化、中断或失败后重新观察。
表中 `R` 为 `Replan`，`E` 为含 `missing_predicate`、`searched_volume`、`wake_on` 的 `EnvBlocked`。
只读端点还可能抛 `ApiError`，执行器保留底层 `Fatal` 和抢占语义。
端点章节出自 [比赛 API v1.3.11](../../../docs/99-原始记录/api-spec-v1.3.11.md)。

| 模块及签名 | 前置、返回与后验 | 失败 | 端点、规范与待实测 |
|---|---|---|---|
| `sightline.eye_pos(source, *, sneaking=None)`、`block_center(pos)`、`eye_to_block_dist(source, block_pos, ...)`、`eye_to_aabb_dist(source, block_pos, ...)`、`eye_to_point_dist(source, target_point, ...)`、`voxel_line_cells(start, end)`、`ray_clear_to_point(...)`、`can_see_block(ex, source, block_pos, ...)`、`can_reach_block(ex, source, block_pos, *, reach=DIG_EYE_REACH, ...)` | 统一将 `/status.pos` 脚底坐标换算为站立/潜行眼睛坐标（`EYE_HEIGHT_STANDING=1.62`、`EYE_HEIGHT_SNEAKING=1.27`），将整数角点换算为方块中心 `(bx+0.5, by+0.5, bz+0.5)`；导出触达常量 `DIG_EYE_REACH=4.5`、`API_DIG_EYE_REACH=5.0`、`STATION_EYE_REACH=3.5`、`ENTITY_EYE_REACH=4.0`，并提供两段式零请求眼距粗筛 + 3D 体素视线遮挡复核（单次调用内缓存 `get_block`，拦截对角双石夹缝） | R 坐标缺失、布尔值或非有限数。`not_loaded` / `not_ready` 视为不可穿透；上送其余 `ApiError` | 纯计算或 `/get_block` §3.1、§3.3–3.4、§4.2、§8 1.3.10 ①。不发阻塞动作 |
| `recipes.craft_recipe(ex, state, item, *, goal_qty=1)` | `goal_qty` 是最终持有量下限，只能按关键字传入。核对该配方原料和所需工作台，最多调用 64 次合成。持有足额直接返回。每批核对成品增量。返回 `{item,before,after,gained,consumed}`，`consumed` 记录背包实物净减。调用方负责检查与结算预留 | R 缺料、未知配方、预算用尽、`inventory_full`、`no_window` 或后验不足。E 找不到可达工作台（含跳过 `stalled`） | `/inventory /status /find_blocks /get_block /move_to /craft` §3.1–3.4、§4.1、§4.8、§5。待测 `POST /craft` 的 `count=1`、选配方、混用标签原料和工作台实际触达距离 |
| `field.scan_blocks(ex, state, types=None, *, radius=16, limit=12, properties=None, mature=None)` | 有界搜索，并逐个复核方块。返回 `{status,blocks,ready,origin,dimension,radius,observed_at}`，`status` 为 `found/unknown`，`ready=False` 表示世界正在加载区块 | R 无效参数。`not_ready`/空回执/过滤后为空或候选未加载时为 `unknown` | `/status /find_blocks /get_block` §3.1、§3.3–3.4、§5。待测性质键值、负搜索覆盖和暴露面范围 |
| `field.scan_entities(ex, state, types=None, *, radius=32, limit=40)` | 返回 `{status,entities,ready,origin,dimension,radius,observed_at}`；`ready=True` 后区块和实体已到齐，只返回有整型 ID 和坐标的观察记录 | R 无效参数。`not_ready` 或空回执为 `unknown` | `/status /get_entities` §3.1、§3.5、§5。待复核实体筛选边界与年龄/羊毛专项字段 |
| `field.move_local(ex, state, pos, *, rng=1, max_distance=16, require_move=False)` | 只走近处已观测落脚格。检查整格支撑和两格净空，移动后重读同维度落地状态与距离，返回状态快照。足够近且已稳时可跳过，`require_move=True` 强制发出尝试 | R 无落脚证据、超限、偏离目标、掉线/切维度后无法核对 | `/status /get_block /move_to` §3.1、§3.4、§4.1。待测单格、台阶、贴边与实际到点精度 |
| `field.place_block(ex, state, item, pos, *, expected_block=None, properties=None)` | 要求观测到的空格、物品且不与自身占格重叠；返回目标方块复读快照。目标类型与属性已满足时不发动作 | R 目标格/物品无证据、碰撞或后验不同 | `/status /inventory /get_block /place` §3.1–3.2、§3.4、§4.3。待测侧放和方向性方块影响 |
| `field.equip_item(ex, state, item, slot="hand", *, min_durability=None)` | 按指定槽位检查持有与剩余耐久。已经装备且满足条件时直接返回该快照 | R 槽位、库存、实际装备或耐久不满足 | `/inventory /equip` §3.2、§4.10。待测手持/副手/护甲各端点读数对齐 |
| `field.mine_block_batch(ex, state, positions, *, block_types, drop, count=1, tool=None, max_blocks=16)` | `count` 为本批新增数量。从眼睛 5 格内已观察候选逐格确认类型、工具条件和脚下安全，挖掘后同步站位与背包，跳过单点 `dig_unconfirmed`/`out_of_reach`/`protected_region`。返回背包实收增量并复读方块变化 | R 缺工具、精准采集(`silk_touch`)不掉矿物、全部候选 `dig_unconfirmed`、挖掘告警或后验不足。E 有界候选耗尽仍不足 | `/status /inventory /get_block /equip /dig` §3.1–3.2、§3.4、§4.2、§4.10、§5。待测实际掉落自动拾取半径 |
| `field.collect_drops(ex, state, item, count=1, *, radius=8, max_targets=4)` | 就近走向有支撑的掉落实体，每次强制重读背包。返回本批已入包增量 | R `not_ready`、尝试后无增量或不足。E 未观察到可拾取实体 | `/status /get_entities /get_block /move_to /inventory` §3.1–3.2、§3.4–3.5、§4.1、§5。待测拾取半径、实体中的 `item` 格式与在途拾取 |
| `interaction.use_item_on(ex, state, target, item=None, *, verify=None)` | `target` 是坐标、实体 ID 或对应单键字典。可选空手。返回 `{receipt,before,after,confirmed}`，快照有库存、状态和目标格，可在 `verify(evidence)` 里验业务后置条件 | R 目标、物品或指定后验不成立 | `/status /inventory /get_block /interact` §3.1–3.2、§3.4、§4.4。待测耕作光照、喂食育龄与点门面 |
| `interaction.use_item(ex, state, item, target=None, *, ms=None, timeout_ms=None, source=None, verify=None)` | 空放或瞄准使用；返回同上证据。盾牌调用由 `finally` 结束举盾。可选验证器控制 `confirmed`，省略时保留待确认结果 | R 缺物品、持时/部位非法或指定后验不足 | `/status /inventory /use_item /posture` §3.1–3.2、§4.5、§4.11。待测天然水域咬钩率与盾牌拦截角 |
| `interaction.hold_posture(ex, state, *, sneak=None, shield=None, jump=None, duration_ms=1000, verify=None)` | 维持指定姿态（含 `jump` 持续跳跃/水中踩水）1–10000 ms。设置、观察、分拍等待，`finally` 通过全量覆盖合并语义只复位本次指定字段（死亡自动复位跳过）；只有验证器确认的效果使 `confirmed=True` | R 姿态、受伤、维度、等待结果或清理后验不符合 | `/posture /status /inventory` §3.1–3.2、§4.11。待测挡箭取证和动作后的姿态回放 |
| `windows.window_view(ex, target)` | 要求现地窗口目标。读取一次、交还一拍，返回包含 `sections` 的视图 | R 视图缺结构 | `/window/view` §4.9。界面不跨调用保持打开 |
| `windows.apply_window_ops(ex, state, target, ops, *, verify_pending=None)` | 返回最后一批回执。兼容 `done` 整数计数与步骤对象列表（`put`/`took`/`select`/`text`），核对 `failed_op`、重读窗口与背包且 `verify_pending(pending, view, state)` 为真时才补未执行后缀 | R 证据矛盾、前缀不明、后缀前置不足或执行器失败 | `/window/act /window/view /inventory` §4.9、§3.2。附魔台等需将 `put/select/take` 放在同一次 `act` |
| `windows.put_window_item(ex, state, target, item, count, to, *, slot=None, verify=None)` | 见证扣包及窗口目标分区增量，指定 `slot` 时还需该槽位证明。可传 `verify(before_view, after_view, state)` 验窗。返回 `{receipt,view}` | R 缺料或库存、槽位后验不足 | `/window/view /window/act /inventory` §4.9、§3.2。待测槽编号与窗口属性 |
| `windows.select_window_option(ex, state, target, option, *, times=1, verify)` | 一次 `select`，重读窗口和背包。调用方必须提供最终后验 `verify(before_view, after_view, state)`。返回 `{receipt,view}` | R 次数非法、选项拒绝或后验不足 | `/window/view /window/act /inventory` §4.9、§3.2 |
| `windows.take_container_item(ex, state, target, section, item, count, *, view=None)` 与 `is_storage_window(view, expected_block=None)` | 识别 `generic_9x3` 等存储容器窗口与 `target` 方块名；从分区取物时兼容部分入包 `inventory_full: true`（`output` 除外），返回 `{receipt,view,count,inventory_full}` | R 数量、视图不足，或零可放返回 `inventory_full`（物品留原处） | `/window/view /window/act /inventory` §4.9、§5、§8 1.3.11 ① |
| `windows.window_section(view, section)` 与 `window_amount(view, section, item, *, slot=None)` | 纯读取，分别返回槽位列表及匹配物品数量。指定槽位只计有对应整数 `slot` 的条目 | 视图由调用者预先校验 | 无。按 §4.9 快照结构读取 |
| `observations.observe_block(ex, pos)` | 按现场坐标读取方块快照，`not_loaded` 或 `not_ready` 返回 `{}` | 上送其余 `ApiError` | `/get_block` §3.4、§5 |
| `observations.refresh_inventory(ex, state)` | 强制刷新背包并返回快照，供材料和事后对账 | `not_ready` 转瞬态 R；上送其余 `ApiError` | `/inventory` §3.2、§5 |
| `observations.completed_advancements(ex, *, require_ready=True)` | 返回可见、明确完成的进度 ID 集合，返回值去掉 `minecraft:` 前缀。默认要求 `ready=True`，未就绪返回空 | 上送 `not_ready` 以外的 `ApiError` | `/advancements` §3.7。进度传播延迟与 `ready` 时序待实测 |
| `furnace.inspect_furnace(ex, target)` | 读取一次普通炉窗口，返回含 `sections` 的快照 | R 非普通炉或视图字段不可用 | `/window/view` §4.9。待测燃烧负载时序 |
| `furnace.load_furnace(ex, state, target, *, raw_item=None, raw_count=0, fuel_item, fuel_count, view=None, replace_fuel=False, output_item=None)` | 原料与燃料足额，投新批时输入槽为空，输出不能被指定产物以外的物品占用。可复用已核实窗口；换燃料先验取回（处理 `inventory_full`），随后每笔投放验扣包。返回动作回执列表 | R 缺料、旧槽位冲突、`inventory_full` 或换料/投放后验不足 | `/window/view /window/act /inventory` §4.9、§3.2、§5 |
| `furnace.collect_furnace(ex, state, target, *, item, count=None, section="output", view=None, inspect_after=True, require_empty=False)` | 单个普通炉已观测的 `output/fuel` 槽有物料；`output` 只能整组取（放不下报 `inventory_full`），`fuel` 不要求清空时允许部分取出。返回 `{receipt,view,count,inventory_full}` | R `inventory_full`、槽位或入包不合 | `/window/view /window/act /inventory` §4.9、§3.2、§8 1.3.11 ①。待测空桶退回时序 |
| `mining.mine_step(ex, state, pos, *, allowed_picks=STONE_OR_BETTER_PICKS, solid=SOLID_SUPPORT, after_dig=None)` | 当前站位前后左右之一且低一格，有可见顶板、支撑。自上往下单格开凿，确认同维度站位和落脚点。返回 `(dest,None)`；若 `after_dig()` 生成器返回发现证据，则暂停下移并返回 `(None,evidence)` | R 不安全、开凿位移或装备/落点失败。列变化或原地 `dig_unconfirmed` 时为 `MineStepUnsafe`，含 `stage=cut/move` | `/status /inventory /get_block /equip /dig /move_to` §3.1–3.2、§3.4、§4.1–4.2、§4.10、§5。待测深层水沙、实际路径与回程 |
| `mining.safe_mine_column(ex, pos, *, solid=SOLID_SUPPORT)`、`under_feet(cur, pos)`、`prepare_mining_pick(ex, state, *, allowed=STONE_OR_BETTER_PICKS)` | 依次返回列体检布尔值、目标是否在脚下的布尔值、装备允许列表中库存最好的镐。默认允许岩种沿用铁线，重要矿点应先观察，调用方可用更严格工具和岩种集合 | R 缺允许的镐或未看到手持 | `/get_block /inventory /equip` §3.2、§3.4、§4.10。待测当场可挖与工具回执 |
| `encounter.verify_shot_lane(ex, state, pos, *, retreat_pos, min_horizontal=0, max_distance=32, elevated_ok=False)` | 有明确现场瞄点、同高短退路（高柱位可由 `elevated_ok=True` 豁免）及脚下支撑。返回 `{status,reason,distance,horizontal_dist,origin,dimension,target}`，`status` 为 `clear/blocked/unknown`，直线是保守观察门槛 | R 坐标、距离参数非法 | `/status /get_block` §3.1、§3.4。服务端弹道与水平速度提前量见 §4.7、§8 1.3.5 |
| `encounter.combat_melee(ex, state, entity, *, retreat_pos, timeout_ms=8000, radius=8, advancement=None, advancement_polls=3, tactic="auto", max_rounds=6)` | 重观测同一可见实体，支持 `auto/kite/bunker_hit/direct`（苦力怕自动 `max_hits=1`+后退风筝、高柱/门框自动 `/look`+`/control(hit=1)` 定点挥砍、副手有盾自动间隙举盾格挡），区分 `no_such_entity`、`stalled`、`interrupted`/`interrupted_by`。返回 `{receipt,target_before,target_after,inventory_before,inventory_after,confirmed,tactic}` | E 目标重观测失败。R `no_such_entity`、`stalled`、退路、击杀或指定进度不足 | `/status /get_entities /get_block /inventory /attack /control /look /posture /advancements` §3.1–3.2、§3.4–3.5、§3.7、§4.6、§4.11、§4.15–4.17、§5 |
| `encounter.shoot_projectile(ex, state, *, entity=None, pos=None, weapon="bow", charge_ms=1100, retreat_pos, min_horizontal=0, max_distance=32, advancement=None, advancement_polls=3, elevated_ok=False)` | 实体/瞄点二选一，持有武器和可确认弹药，射界和退路已确认。返回 `{receipt,lane,ammo_spent,confirmed}`。已有服务器进度时跳过；指定进度才确认该效果 | E 找不到当场目标。R 前置、发射回执或指定进度不足 | `/status /get_entities /get_block /inventory /shoot /advancements` §3.1–3.2、§3.4–3.5、§3.7、§4.7。待测弩装填回执、实际扣弹、命中和进度延迟 |

`recipes.RECIPES` 键为真实成品 ID，值为不可变 `Recipe(ingredients, output, requires_table)`。60 条原版直制配方（含竹子木棍替代）已逐条对照 Mojang Java 1.21.11 服务端 JAR 的 `data/minecraft/recipe` 与 `data/minecraft/tags/item`，外层 JAR SHA-1 为 `64bb6d763bed0a9f1d632ec347938594144943ed`。配方表负责原版材料和产量，接口路线选择由靶场探针复核。 [Mojang Java 1.21.11](https://www.minecraft.net/en-us/article/minecraft-java-edition-1-21-11)

### 子目标、预留和夹具

| 模块及签名 | 前置与返回 | 失败或依赖 |
|---|---|---|
| `loop.subgoals.station_ready(state, observations, station_type, dimension)` | 当前世界就绪，且观察条目类型、维度、`reachable` 匹配时返回真 | 缺证据返回假。纯计算 |
| `loop.subgoals.item_available(state, item, quantity, *, allocatable=None)` | 库存快照未标为陈旧、物品够数，并满足可选可分配量 | 缺证据返回假。纯计算 |
| `loop.subgoals.tool_ready(state, purpose, tier, min_durability=0)` | 如 `purpose="pickaxe", tier="iron"`。检查库存工具家族、档位与可选剩余耐久，并拒绝显式零耐久 | 缺证据返回假。纯计算 |
| `loop.subgoals.landmark_reachable(state, observations, landmark_type, dimension)` | 当前维度有 `type` 匹配项，且 `reachable=True, return_safe=True` | 缺证据返回假。纯计算 |
| `loop.subgoals.safe_encounter(state, entity, entity_type, *, dimension, retreat_safe, min_health, min_distance=0, max_distance=24, ammo=None)` | 用当场实体快照核对类型、距离和血量，同时检查状态、短退路、异常状态与可选弹药 `(item,count)`。安全模式以状态快照时刻判定 | 缺证据返回假。纯计算 |
| `loop.subgoals.active_portal(state, portal, source_dimension, destination)` | 本维度已观察到匹配维度方向的激活门方块才为真。落地安全需另验 | 缺证据返回假。纯计算 |
| `loop.reserve.ReservationBook(safety_floor=None)` | 单调度循环负责一本账，`entries` 为 `Reservation(consumer,item,count,mode)` 序列，模式为 `consume/borrow/in_transit` | 非法安全底线抛 `ValueError`。无端点 |
| `book.can_reserve_plan(state, consumer, requirements)` / `book.reserve_plan(state, consumer, requirements)` | 计算整份 `(item,count,mode)` 同时可用的条件，合并同种需求。`reserve_plan` 成功后替换该消费者的库存预留，保留已经登记的容器在途项。 | 库存陈旧或未足额时返回假，保持已有账项。格式无效抛 `ValueError` |
| `book.observe(state)`、`book.reset(state)` | 检查死亡/换维度，识别后整账 `stale` 并返回假；调用方复查状态、背包、窗口后用 `reset` 清旧账重建 | 数据陈旧期间不可分配。无端点 |
| `book.available(state, item, consumer=None)`、`book.reserve(state, consumer, item, count, *, mode="consume")` | 返回库存扣其他占用和底线所得件数；预留更新指定键并返回是否成功。在途已离开背包，同一桶体的变体共享占用 | 不够或陈旧返回 0/假，非法键/数量抛 `ValueError` |
| `book.move_to_container(state, consumer, item, count, *, from_mode="consume")`、`book.release(consumer, item=None, *, mode=None)` | 凭实际扣包与炉面确认将库存预留转为在途，返回是否结转；实收、取消或消耗后释放匹配预留 | 陈旧或无足额旧预留时转移返回假，非法数量抛 `ValueError` |
| `scripted_world.ScriptedWorld(*args, **kwargs)`、`world.queue(method, *responses)`、`world.assert_exhausted()` | 按方法排列快照、异常或 `callback(world, **request)`，测试显式模拟相应副作用。用 `testing.drive()` 驱动生成器并核对回执消费 | 需要安排的回执缺失或未消费时报 `AssertionError`。纯离线 |

扫描结果的 `dimension`、`observed_at` 在外层。调用方将已观察维度与可达证据并入候选，再交给子目标谓词；切维度、死亡或地标陈旧后重新观察。

`CampaignPlanner` 持有一本 `ReservationBook`。规则和战术展开先核对需求，最终派发时用技能的可选 `material_plan(args, state)` 进行原子预留。技能结束、重规划、环境挂起或主循环退出时释放当前账项，下一拍基于刷新后的背包重算。模型策略读取同一账本的摘要，现场工作站、地标、遭遇和门在缺少现场快照时保持待观察。
