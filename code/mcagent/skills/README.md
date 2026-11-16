# skills —— 执行层

**职责**：一个目标 → 一串 `Executor` 动作。**不写 HTTP，不写决策**。
规则的权威版本在仓库根 `AGENTS.md` §9；这里只说怎么上手。

```
base.py       Skill 基类 / Param 参数声明 / rec() / failure()
registry.py   @register、REGISTRY、make()、契约检查、自动发现
items.py      只读选择器：最好的镐、垫块、空气判断
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
    for b in blocks:                                  # 观察随便调，不用 yield
        yield rec("move_to", ex.move_to(pos=b, rng=3), pos=b)
        yield rec("dig", ex.dig(b), pos=b)
```

基类的 `step()` 每次只推进到下一个 `yield`，所以对主循环来说它仍然是"一步一动作"的状态机。
要用 L2 原语就 `yield from nav.breach_1x2(ex, state, [...])`。

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
端点章节出自 [比赛 API v1.3.1](../../../docs/99-原始记录/api-spec-v1.3.1.md)。

| 模块及签名 | 前置、返回与后验 | 失败 | 端点、规范与待实测 |
|---|---|---|---|
| `recipes.craft_recipe(ex, state, item, *, goal_qty=1)` | `goal_qty` 是最终持有量下限，只能按关键字传入。核对该配方原料和所需工作台，最多调用 64 次合成。持有足额直接返回。每批核对成品增量。返回 `{item,before,after,gained,consumed}`，`consumed` 记录背包实物净减。调用方负责检查与结算预留 | R 缺料、未知配方、预算用尽或后验不足。E 找不到可达工作台 | `/inventory /status /find_blocks /get_block /move_to /craft` §3.1–3.4、§4.1、§4.8。待测 `POST /craft` 的 `count=1`、选配方、混用标签原料和工作台实际触达距离 |
| `field.scan_blocks(ex, state, types=None, *, radius=16, limit=12, properties=None, mature=None)` | 有界搜索，并逐个复核方块。返回 `{status,blocks,origin,dimension,radius,observed_at}`，`status` 为 `found/unknown`，观察时刻为本机单调时钟 | R 无效参数。空回执、过滤后为空或候选未加载时为 `unknown` | `/status /find_blocks /get_block` §3.1、§3.3–3.4。待测性质键值、负搜索覆盖和暴露面范围 |
| `field.scan_entities(ex, state, types=None, *, radius=32, limit=40)` | 返回 `{status,entities,origin,dimension,radius,observed_at}`；只返回有整型 ID 和坐标的观察记录 | R 无效参数。空回执为 `unknown` | `/status /get_entities` §3.1、§3.5。待复核 ready 后可见实体与字段 |
| `field.move_local(ex, state, pos, *, rng=1, max_distance=16, require_move=False)` | 只走近处已观测落脚格。检查整格支撑和两格净空，移动后重读同维度落地状态与距离，返回状态快照。足够近且已稳时可跳过，`require_move=True` 强制发出尝试 | R 无落脚证据、超限、偏离目标、掉线/切维度后无法核对 | `/status /get_block /move_to` §3.1、§3.4、§4.1。待测单格、台阶、贴边与实际到点精度 |
| `field.place_block(ex, state, item, pos, *, expected_block=None, properties=None)` | 要求观测到的空格、物品且不与自身占格重叠；返回目标方块复读快照。目标类型与属性已满足时不发动作 | R 目标格/物品无证据、碰撞或后验不同 | `/status /inventory /get_block /place` §3.1–3.2、§3.4、§4.3。待测侧放和方向性方块影响 |
| `field.equip_item(ex, state, item, slot="hand", *, min_durability=None)` | 按指定槽位检查持有与剩余耐久。已经装备且满足条件时直接返回该快照 | R 槽位、库存、实际装备或耐久不满足 | `/inventory /equip` §3.2、§4.10。待测手持/副手/护甲各端点读数对齐 |
| `field.mine_block_batch(ex, state, positions, *, block_types, drop, count=1, tool=None, max_blocks=16)` | `count` 为本批新增数量。从近处已观察候选逐格确认类型、工具条件和脚下安全。返回背包实收增量，并复读确认被挖方块变化 | R 缺工具、挖掘告警或后验不足。E 有界候选耗尽仍不足 | `/status /inventory /get_block /equip /dig` §3.1–3.2、§3.4、§4.2、§4.10。待测 4 格动作高差与实际拾取 |
| `field.collect_drops(ex, state, item, count=1, *, radius=8, max_targets=4)` | 就近走向有支撑的掉落实体，每次强制重读背包。返回本批已入包增量 | R 尝试后无增量或不足。E 未观察到可拾取实体 | `/status /get_entities /get_block /move_to /inventory` §3.1–3.2、§3.4–3.5、§4.1。待测拾取半径、实体中的 `item` 格式与在途拾取 |
| `interaction.use_item_on(ex, state, target, item=None, *, verify=None)` | `target` 是坐标、实体 ID 或对应单键字典。可选空手。返回 `{receipt,before,after,confirmed}`，快照有库存、状态和目标格，可在 `verify(evidence)` 里验业务后置条件 | R 目标、物品或指定后验不成立 | `/status /inventory /get_block /interact` §3.1–3.2、§3.4、§4.4。待测桶、耕作、喂食、床和点门的专项结果 |
| `interaction.use_item(ex, state, item, target=None, *, ms=None, timeout_ms=None, source=None, verify=None)` | 空放或瞄准使用；返回同上证据。盾牌调用由 `finally` 结束举盾。可选验证器控制 `confirmed`，省略时保留待确认结果 | R 缺物品、持时/部位非法或指定后验不足 | `/status /inventory /use_item /posture` §3.1–3.2、§4.5、§4.11。待测钓竿实际超时和 `caught`、盾牌朝向和拦截 |
| `interaction.hold_posture(ex, state, *, sneak=None, shield=None, duration_ms=1000, verify=None)` | 维持指定姿态 1–10000 ms。设置、观察、分拍等待，`finally` 只清除本次指定字段；返回回执与前后姿态观察。只有验证器确认的效果使 `confirmed=True` | R 姿态、受伤、维度、等待结果或清理后验不符合 | `/posture /status /inventory` §3.1–3.2、§4.11。待测挡箭取证和动作后的姿态回放 |
| `windows.window_view(ex, target)` | 要求现地窗口目标。读取一次、交还一拍，返回包含 `sections` 的视图 | R 视图缺结构 | `/window/view` §4.9。待测工作台外窗口的实际槽名与物品属性 |
| `windows.apply_window_ops(ex, state, target, ops, *, verify_pending=None)` | 返回最后一批回执。只有 `Executor` 上送可判定的 `done/failed_op`、重读窗口与背包且 `verify_pending(pending, view, state)` 为真，才补未执行后缀。调用方验最终状态 | R 证据矛盾、前缀不明、后缀前置不足或执行器失败 | `/window/act /window/view /inventory` §4.9、§3.2。字段形状待测；带部分完成的可重试错误可能被现有执行器先重发，暂用单操作投取 |
| `windows.put_window_item(ex, state, target, item, count, to, *, slot=None, verify=None)` | 见证扣包及窗口目标分区增量，指定 `slot` 时还需该槽位证明。可传 `verify(before_view, after_view, state)` 验窗。返回 `{receipt,view}` | R 缺料或库存、槽位后验不足 | `/window/view /window/act /inventory` §4.9、§3.2。待测槽编号与窗口属性 |
| `windows.select_window_option(ex, state, target, option, *, times=1, verify)` | 一次 `select`，重读窗口和背包。调用方必须提供最终后验 `verify(before_view, after_view, state)`。返回 `{receipt,view}` | R 次数非法、选项拒绝或后验不足 | `/window/view /window/act /inventory` §4.9、§3.2。待测不同窗口的选项索引和附魔的槽位/等级变化 |
| `windows.take_container_item(ex, state, target, section, item, count, *, view=None)` | 从已观察到的分区取指定物品，核对入包后重读窗口。返回 `{receipt,view,count}` | R 数量、视图或实际入包不足 | `/window/view /window/act /inventory` §4.9、§3.2。待测窗口重开与取出延迟 |
| `windows.window_section(view, section)` 与 `window_amount(view, section, item, *, slot=None)` | 纯读取，分别返回槽位列表及匹配物品数量。指定槽位只计有对应整数 `slot` 的条目 | 视图由调用者预先校验 | 无。按 §4.9 快照结构读取 |
| `observations.observe_block(ex, pos)` | 按现场坐标读取方块快照，未加载返回 `{}` | 上送其余 `ApiError` | `/get_block` §3.4 |
| `observations.refresh_inventory(ex, state)` | 强制刷新背包并返回快照，供材料和事后对账 | 上送 `ApiError` | `/inventory` §3.2 |
| `observations.completed_advancements(ex, *, require_ready=True)` | 返回可见、明确完成的进度 ID 集合，返回值去掉 `minecraft:` 前缀。默认要求 `ready=True`，未就绪返回空 | 上送 `not_ready` 以外的 `ApiError` | `/advancements` §3.7。进度传播延迟与 `ready` 时序待实测 |
| `furnace.inspect_furnace(ex, target)` | 读取一次普通炉窗口，返回含 `sections` 的快照 | R 非普通炉或视图字段不可用 | `/window/view` §4.9。待测燃烧状态、进度与燃料字段 |
| `furnace.load_furnace(ex, state, target, *, raw_item=None, raw_count=0, fuel_item, fuel_count, view=None, replace_fuel=False, output_item=None)` | 原料与燃料足额，投新批时输入槽为空，输出不能被指定产物以外的物品占用。可复用已核实窗口；换燃料先验取回，随后每笔投放验扣包。返回动作回执列表，续用炉面前再次读取 | R 缺料、旧槽位冲突、换料/投放后验不足 | `/window/view /window/act /inventory` §4.9、§3.2。待测燃烧导致的槽位即时变更与烟熏炉/高炉 |
| `furnace.collect_furnace(ex, state, target, *, item, count=None, section="output", view=None, inspect_after=True, require_empty=False)` | 单个普通炉已观测的 `output/fuel` 槽有物料；默认取该物品目前全部。返回 `{receipt,view,count}`。按实收判定，`require_empty` 时再核对槽为空 | R 槽位或入包不合 | `/window/view /window/act /inventory` §4.9、§3.2。待测空桶退回与产物入包 |
| `mining.mine_step(ex, state, pos, *, allowed_picks=STONE_OR_BETTER_PICKS, solid=SOLID_SUPPORT, after_dig=None)` | 当前站位前后左右之一且低一格，有可见顶板、支撑。自上往下单格开凿，确认同维度站位和落脚点。返回 `(dest,None)`；若 `after_dig()` 生成器返回发现证据，则暂停下移并返回 `(None,evidence)` | R 不安全或装备/落点失败。列变化时为 `MineStepUnsafe`，含 `stage=cut/move` | `/status /inventory /get_block /equip /dig /move_to` §3.1–3.2、§3.4、§4.1–4.2、§4.10。待测深层水沙、实际路径与回程 |
| `mining.safe_mine_column(ex, pos, *, solid=SOLID_SUPPORT)`、`under_feet(cur, pos)`、`prepare_mining_pick(ex, state, *, allowed=STONE_OR_BETTER_PICKS)` | 依次返回列体检布尔值、目标是否在脚下的布尔值、装备允许列表中库存最好的镐。默认允许岩种沿用铁线，重要矿点应先观察，调用方可用更严格工具和岩种集合 | R 缺允许的镐或未看到手持 | `/get_block /inventory /equip` §3.2、§3.4、§4.10。待测当场可挖与工具回执 |
| `encounter.verify_shot_lane(ex, state, pos, *, retreat_pos, min_horizontal=0, max_distance=32)` | 有明确现场瞄点、同高短退路及脚下支撑。返回 `{status,reason,distance,horizontal_dist,origin,dimension,target}`，`status` 为 `clear/blocked/unknown`，直线是保守观察门槛 | R 坐标、距离参数非法 | `/status /get_block` §3.1、§3.4。服务端弹道见 §4.7，直线门槛、移动实体与高差待测 |
| `encounter.combat_melee(ex, state, entity, *, retreat_pos, timeout_ms=8000, radius=8, advancement=None, advancement_polls=3)` | 重观测同一可见实体，确认站位和退路。一次交战，回执与之后实体/库存与服务器进度分开。返回 `{receipt,target_before,target_after,inventory_before,inventory_after,confirmed}`，已确认进度时直接返回 `{receipt:None,already_completed:True,confirmed:True}` | E 目标重观测失败。R 退路、击杀或指定进度不足 | `/status /get_entities /get_block /inventory /attack /advancements` §3.1–3.2、§3.4–3.5、§3.7、§4.6。待测追击范围、掉落和进度延迟 |
| `encounter.shoot_projectile(ex, state, *, entity=None, pos=None, weapon="bow", charge_ms=1100, retreat_pos, min_horizontal=0, max_distance=32, advancement=None, advancement_polls=3)` | 实体/瞄点二选一，持有武器和可确认弹药，射界和退路已确认。返回 `{receipt,lane,ammo_spent,confirmed}`。已有服务器进度时跳过；指定进度才确认该效果 | E 找不到当场目标。R 前置、发射回执或指定进度不足 | `/status /get_entities /get_block /inventory /shoot /advancements` §3.1–3.2、§3.4–3.5、§3.7、§4.7。待测弩装填回执、实际扣弹、命中和进度延迟 |

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
