# Minecraft Agent 挑战赛初赛规则（草案 v2）

> 草案，会根据实际情况改动喵。

## 一、活动目标

参赛者设计一个能通过赛事 API（见 [API.md](API.md)）自主游玩 Minecraft 的智能 Agent。在规定时间内，Agent 自行观察环境、制定计划并执行操作，尽可能多地完成计分 advancement，获得更高的总分。

## 二、比赛时间

每场比赛时长 **8 小时**，从裁判系统宣布比赛开始时起算。

比赛结束后新产生的操作和 advancement 一律不计入成绩。因参赛程序、网络连接或模型服务造成的中断计入比赛时间，不另行补时；若赛事服务器发生统一故障，由主办方决定暂停、补时或重赛。

## 三、比赛世界

1. **游戏版本**：Minecraft Java Edition 1.21.11。难度、gamerule 等服务器参数赛前公布。
2. **虚空岛**：每位参赛者分配一座**完全相同**的岛。岛是从同一个正常生成的世界中截取的一块圆形区域，四周是虚空；主世界和下界各有一座，两者之间可以照常用下界传送门往返。所有岛的地形、结构、箱子战利品和初始生物都相同，岛与岛之间相距很远，互不可见。
3. **岛内可用**：岛上的要塞可以用末影之眼找到（末影之眼会飞向本岛的要塞）。藏宝图、探险家地图和海豚引路依赖世界生成器，在虚空世界中不可用。
4. **活动范围**：参赛者只能在自己的岛及其上空、下方活动，不得前往其他参赛者的岛。
5. **末地共用**：末地只有一个，所有参赛者共用。比赛开始前主办方已击败末影龙，主岛上的末地折跃门已开启，龙蛋已被移走。比赛中**禁止复活末影龙**，也不得以任何方式妨碍其他参赛者（攻击、堵门、破坏或拿走他人的物品等）。末地城和末地船的战利品是共用的，先到先得。
6. **岛屿半径、世界种子和岛的位置**：待定，赛前公布。

## 四、初始状态

比赛开始前，主办方统一重置每位参赛 Agent 的出生位置（各自岛上的同一相对位置）、背包、末影箱、经验、状态效果和 advancement 进度。

比赛过程中，Agent 必须通过主办方公开的赛事 API 自主完成观察、决策和操作。除处理赛事平台故障外，不得由人工代替 Agent 操作，也不得使用服务器命令、管理员权限、未公开接口或直接读取世界存档等方式获取额外信息或资源。

## 五、计分方法

初赛满分 100 分，由"游戏表现分"和"工程评审分"两部分组成：

```text
初赛总成绩 = 游戏表现分（60 分）+ 工程评审分（40 分）
```

### 1. 游戏表现分（60 分）

只统计下方计分表中标为计分的原版 advancement。配方解锁、服务器自定义 advancement、标为"不计分"的条目，以及在比赛时间之外完成的 advancement 都不计分。同一个 advancement 只计一次。

每项的分值**只由难度等级决定**：

| 难度 | 说明 | 分值 |
|---:|---|---:|
| 1 | 基础动作，游戏早期自然完成 | 5 |
| 2 | 简单制作、交互或短距离探索 | 15 |
| 3 | 需要一定资源积累、结构探索或战斗准备 | 30 |
| 4 | 需要稀有资源、复杂战斗、长链条准备或精确操作 | 60 |
| 5 | 需要大规模收集、复合条件或极高执行难度 | 100 |

各分类的根节点（Minecraft、Nether、The End、Adventure、Husbandry）为 0 分。

```text
游戏原始分 = 8 小时内完成的所有计分 advancement 的分值之和
游戏表现分 = 参赛者游戏原始分 ÷ 本届最高游戏原始分 × 60
```

游戏原始分最高的有效参赛者得 60 分，其他人按比例折算。若所有有效参赛者的游戏原始分都为 0，则所有人的游戏表现分都记 0 分。

完成状态和完成时间以赛事服务器的记录为准。客户端显示、Agent 自行报告或本地日志都不能代替裁判记录。

**不计分的条目**分三类，具体见计分表：

- 赛事 API 目前缺少必要动作的（反弹飞行物、长矛冲锋、跳跃和落地攻击等）；
- 虚空岛赛制下做不到或会越出自己活动范围的（需要大量群系、林地府邸，或在下界长途旅行）；
- 依赖共用末影龙的，以及依赖随机雷暴天气的。

**"岛上有……时计分"的条目**是否计分，取决于最终选定的岛上有没有对应的地形或结构。最终计分表会把每一项确定为"计分"或"不计分"。

### 2. 工程评审分（40 分）

组委会组织不少于 3 名评委，对参赛者提交的 harness 和 Agent 工程进行评审，每位评委独立给出 0–40 分。评审主要考察：

- harness 与 Agent 整体设计的完善性；
- 方案、策略或技术实现的新颖性；
- 工程结构、代码组织和实现逻辑的清晰度；
- 系统的稳定性、可复现性、可维护性及其他综合工程表现。

汇总时去掉一个最高分和一个最低分，其余评委分数取算术平均，作为工程评审分。最高分或最低分有多个相同时，也只各去掉一个。工程评审分保留两位小数。

### 3. 初赛总成绩

```text
初赛总成绩 = 游戏表现分 + 工程评审分
```

满分 100 分，保留两位小数，作为初赛排名的主要依据。

## 六、并列判定

初赛总成绩相同时，依次比较：

1. 游戏表现分较高者优先；
2. 仍相同时，完成难度 5 的计分 advancement 数量多者优先；
3. 仍相同时，依次比较难度 4、3、2、1 的完成数量；
4. 仍相同时，较早达到最终游戏原始分者优先；
5. 仍无法区分时，并列排名。

## 七、违规处理

下列行为视为违规，主办方可视情节扣除相关 advancement 的分数或取消成绩：

- 离开自己的岛前往其他参赛者的岛；
- 复活末影龙，或在末地妨碍其他参赛者；
- 人工代替 Agent 操作、使用服务器命令或管理员权限、调用未公开接口、读取世界存档；
- 利用游戏或服务器漏洞获得 advancement。

## 八、API 能力说明

赛事 API 不保证能完成每一个原版 advancement。参赛者应根据公开接口的实际能力，自行选择任务、制定策略。

## 九、成绩与申诉

比赛结束后，主办方公布每位参赛者的游戏原始分、游戏表现分、工程评审分、初赛总成绩，以及已完成的 advancement 和对应完成时间。对成绩有异议的，应在成绩公布后的规定期限内提出申诉；主办方以服务器记录、赛事运行日志和评审记录作为最终裁定依据。

主办方保留对未尽事宜进行解释和补充的权利。任何补充规则都会在生效前向所有参赛者统一公布。

决赛的比赛形式、计分方式和具体规则另行通知。

## 附：计分表（草案）

Minecraft Java 1.21.11 共有 125 个非配方原版 advancement，下表列出除 5 个根节点外的 120 项。分值一栏中的"—"表示不计分。

### story

| Advancement | 英文名 | 难度 | 分值 | 计分条件 |
|---|---|---:|---:|---|
| `minecraft:story/cure_zombie_villager` | Zombie Doctor | 4 | 60 | 岛上有下界要塞时计分（不出意外，应该是要保证有要塞的） |
| `minecraft:story/enter_the_end` | The End? | 4 | 60 | 岛上有下界要塞时计分 |
| `minecraft:story/follow_ender_eye` | Eye Spy | 4 | 60 | 岛上有下界要塞时计分 |
| `minecraft:story/shiny_gear` | Cover Me with Diamonds | 4 | 60 | 计分 |
| `minecraft:story/enchant_item` | Enchanter | 3 | 30 | 计分 |
| `minecraft:story/enter_the_nether` | We Need to Go Deeper | 3 | 30 | 计分 |
| `minecraft:story/deflect_arrow` | Not Today, Thank You | 2 | 15 | 计分 |
| `minecraft:story/form_obsidian` | Ice Bucket Challenge | 2 | 15 | 计分 |
| `minecraft:story/lava_bucket` | Hot Stuff | 2 | 15 | 计分 |
| `minecraft:story/mine_diamond` | Diamonds! | 2 | 15 | 计分 |
| `minecraft:story/iron_tools` | Isn't It Iron Pick | 1 | 5 | 计分 |
| `minecraft:story/mine_stone` | Stone Age | 1 | 5 | 计分 |
| `minecraft:story/obtain_armor` | Suit Up | 1 | 5 | 计分 |
| `minecraft:story/smelt_iron` | Acquire Hardware | 1 | 5 | 计分 |
| `minecraft:story/upgrade_tools` | Getting an Upgrade | 1 | 5 | 计分 |

### nether

| Advancement | 英文名 | 难度 | 分值 | 计分条件 |
|---|---|---:|---:|---|
| `minecraft:nether/all_effects` | How Did We Get Here? | 5 | 100 | 岛上有下界要塞时计分 |
| `minecraft:nether/all_potions` | A Furious Cocktail | 5 | 100 | 岛上有下界要塞时计分 |
| `minecraft:nether/create_full_beacon` | Beaconator | 5 | 100 | 岛上有下界要塞时计分 |
| `minecraft:nether/netherite_armor` | Cover Me in Debris | 5 | 100 | 岛上有堡垒遗迹时计分 |
| `minecraft:nether/summon_wither` | Withering Heights | 5 | 100 | 岛上有下界要塞时计分 |
| `minecraft:nether/uneasy_alliance` | Uneasy Alliance | 5 | 100 | 计分 |
| `minecraft:nether/create_beacon` | Bring Home the Beacon | 4 | 60 | 岛上有下界要塞时计分 |
| `minecraft:nether/fast_travel` | Subspace Bubble | 4 | — | 如果岛不够大就不计分 |
| `minecraft:nether/get_wither_skull` | Spooky Scary Skeleton | 4 | 60 | 岛上有下界要塞时计分 |
| `minecraft:nether/obtain_ancient_debris` | Hidden in the Depths | 4 | 60 | 计分 |
| `minecraft:nether/ride_strider_in_overworld_lava` | Feels Like Home | 4 | 60 | 岛上有主世界 50 格以上的岩浆湖时计分 |
| `minecraft:nether/charge_respawn_anchor` | Not Quite "Nine" Lives | 3 | 30 | 计分 |
| `minecraft:nether/explore_nether` | Hot Tourist Destinations | 3 | 30 | 岛上有下界五种群系都在时计分 |
| `minecraft:nether/find_bastion` | Those Were the Days | 3 | 30 | 岛上有堡垒遗迹时计分 |
| `minecraft:nether/find_fortress` | A Terrible Fortress | 3 | 30 | 岛上有下界要塞时计分 |
| `minecraft:nether/loot_bastion` | War Pigs | 3 | 30 | 岛上有堡垒遗迹时计分 |
| `minecraft:nether/obtain_blaze_rod` | Into Fire | 3 | 30 | 岛上有下界要塞时计分 |
| `minecraft:nether/return_to_sender` | Return to Sender | 3 | — | 按目前API无法完成 |
| `minecraft:nether/ride_strider` | This Boat Has Legs | 3 | 30 | 计分 |
| `minecraft:nether/brew_potion` | Local Brewery | 2 | 15 | 岛上有下界要塞时计分 |
| `minecraft:nether/obtain_crying_obsidian` | Who is Cutting Onions? | 2 | 15 | 计分 |
| `minecraft:nether/distract_piglin` | Oh Shiny | 1 | 5 | 计分 |

### end

| Advancement | 英文名 | 难度 | 分值 | 计分条件 |
|---|---|---:|---:|---|
| `minecraft:end/elytra` | Sky's the Limit | 4 | 60 | 计分 |
| `minecraft:end/find_end_city` | The City at the End of the Game | 4 | 60 | 计分 |
| `minecraft:end/kill_dragon` | Free the End | 4 | — | 不计分，按照规则没有末影龙的存在 |
| `minecraft:end/levitate` | Great View From Up Here | 4 | 60 | 计分 |
| `minecraft:end/respawn_dragon` | The End... Again... | 4 | — | 不计分 |
| `minecraft:end/dragon_breath` | You Need a Mint | 3 | — | 不计分 |
| `minecraft:end/dragon_egg` | The Next Generation | 3 | — | 不计分 |
| `minecraft:end/enter_end_gateway` | Remote Getaway | 3 | 30 | 计分 |

### adventure

| Advancement | 英文名 | 难度 | 分值 | 计分条件 |
|---|---|---:|---:|---|
| `minecraft:adventure/adventuring_time` | Adventuring Time | 5 | — | 不计分：54 个群系，岛上不够 |
| `minecraft:adventure/arbalistic` | Arbalistic | 5 | 100 | 计分 |
| `minecraft:adventure/kill_all_mobs` | Monsters Hunted | 5 | — | 岛上做不到 |
| `minecraft:adventure/trim_with_all_exclusive_armor_patterns` | Smithing with Style | 5 | — | 不计分：要林地府邸、远古城市、海底神殿、末地城等 |
| `minecraft:adventure/very_very_frightening` | Very Very Frightening | 5 | — | 不计分：依赖雷暴，天气随机 |
| `minecraft:adventure/blowback` | Blowback | 4 | — | 不计分：API 缺反弹动作 |
| `minecraft:adventure/fall_from_world_height` | Caves & Cliffs | 4 | — | 不计分：缺跳跃控制；虚空岛底下没有地面 |
| `minecraft:adventure/hero_of_the_village` | Hero of the Village | 4 | 60 | 岛上有村庄（交易、铁傀儡、袭击）时计分 |
| `minecraft:adventure/lightning_rod_with_villager_no_fire` | Surge Protector | 4 | — | 不计分：依赖雷暴，天气随机 |
| `minecraft:adventure/overoverkill` | Over-Overkill | 4 | — | 不计分：缺落地攻击；重锤核心来自不祥宝库 |
| `minecraft:adventure/revaulting` | Revaulting | 4 | 60 | 岛上有试炼密室时计分 |
| `minecraft:adventure/spear_many_mobs` | Mob Kabob | 4 | — | 不计分：API 缺长矛冲锋 |
| `minecraft:adventure/spyglass_at_dragon` | Is It a Plane? | 4 | — | 不计分：龙赛前已被杀 |
| `minecraft:adventure/throw_trident` | A Throwaway Joke | 4 | 60 | 计分 |
| `minecraft:adventure/totem_of_undying` | Postmortal | 4 | 60 | 岛上有村庄（交易、铁傀儡、袭击）时计分 |
| `minecraft:adventure/trade_at_world_height` | Star Trader | 4 | 60 | 岛上有村庄（交易、铁傀儡、袭击）时计分 |
| `minecraft:adventure/two_birds_one_arrow` | Two Birds, One Arrow | 4 | 60 | 计分 |
| `minecraft:adventure/craft_decorated_pot_using_only_sherds` | Careful Restoration | 3 | 30 | 岛上有可疑沙砾/沙子（沙漠井、沙漠神殿、海底废墟、古迹废墟）时计分 |
| `minecraft:adventure/heart_transplanter` | Heart Transplanter | 3 | 30 | 岛上有苍白之园（嘎枝之心）时计分 |
| `minecraft:adventure/kill_mob_near_sculk_catalyst` | It Spreads | 3 | 30 | 岛上有深暗之域（幽匿感测体/催发体）时计分 |
| `minecraft:adventure/play_jukebox_in_meadows` | Sound of Music | 3 | 30 | 岛上有草甸时计分 |
| `minecraft:adventure/sniper_duel` | Sniper Duel | 3 | 30 | 计分 |
| `minecraft:adventure/summon_iron_golem` | Hired Help | 3 | 30 | 计分 |
| `minecraft:adventure/trim_with_any_armor_pattern` | Crafting a New Look | 3 | 30 | 计分 |
| `minecraft:adventure/under_lock_and_key` | Under Lock and Key | 3 | 30 | 岛上有试炼密室时计分 |
| `minecraft:adventure/voluntary_exile` | Voluntary Exile | 3 | 30 | 计分 |
| `minecraft:adventure/who_needs_rockets` | Who Needs Rockets? | 3 | 30 | 岛上有试炼密室时计分 |
| `minecraft:adventure/avoid_vibration` | Sneak 100 | 2 | 15 | 岛上有深暗之域（幽匿感测体/催发体）时计分 |
| `minecraft:adventure/brush_armadillo` | Isn't It Scute? | 2 | 15 | 岛上有热带草原/恶地（犰狳）时计分 |
| `minecraft:adventure/bullseye` | Bullseye | 2 | 15 | 计分 |
| `minecraft:adventure/crafters_crafting_crafters` | Crafters Crafting Crafters | 2 | 15 | 计分 |
| `minecraft:adventure/honey_block_slide` | Sticky Situation | 2 | — | 不计分：缺跳跃控制，待复测 |
| `minecraft:adventure/lighten_up` | Lighten Up | 2 | 15 | 岛上有试炼密室时计分 |
| `minecraft:adventure/minecraft_trials_edition` | Minecraft: Trial(s) Edition | 2 | 15 | 岛上有试炼密室时计分 |
| `minecraft:adventure/ol_betsy` | Ol' Betsy | 2 | 15 | 计分 |
| `minecraft:adventure/read_power_of_chiseled_bookshelf` | The Power of Books | 2 | 15 | 计分 |
| `minecraft:adventure/salvage_sherd` | Respecting the Remnants | 2 | 15 | 岛上有可疑沙砾/沙子（沙漠井、沙漠神殿、海底废墟、古迹废墟）时计分 |
| `minecraft:adventure/spyglass_at_ghast` | Is It a Balloon? | 2 | 15 | 计分 |
| `minecraft:adventure/spyglass_at_parrot` | Is It a Bird? | 2 | 15 | 岛上有丛林（鹦鹉）时计分 |
| `minecraft:adventure/trade` | What a Deal! | 2 | 15 | 岛上有村庄（交易、铁傀儡、袭击）时计分 |
| `minecraft:adventure/use_lodestone` | Country Lode, Take Me Home | 2 | 15 | 计分 |
| `minecraft:adventure/walk_on_powder_snow_with_leather_boots` | Light as a Rabbit | 2 | 15 | 岛上有细雪（雪坡/雪林）时计分 |
| `minecraft:adventure/whos_the_pillager_now` | Who's the Pillager Now? | 2 | 15 | 计分 |
| `minecraft:adventure/kill_a_mob` | Monster Hunter | 1 | 5 | 计分 |
| `minecraft:adventure/shoot_arrow` | Take Aim | 1 | 5 | 计分 |
| `minecraft:adventure/sleep_in_bed` | Sweet Dreams | 1 | 5 | 计分 |

### husbandry

| Advancement | 英文名 | 难度 | 分值 | 计分条件 |
|---|---|---:|---:|---|
| `minecraft:husbandry/balanced_diet` | A Balanced Diet | 5 | — | 不计分：附魔金苹果、热带鱼等，岛上凑不齐 |
| `minecraft:husbandry/bred_all_animals` | Two by Two | 5 | — | 不计分：骆驼、熊猫、哞菇等多群系 |
| `minecraft:husbandry/complete_catalogue` | A Complete Catalogue | 5 | — | 不计分：11 种猫 |
| `minecraft:husbandry/whole_pack` | The Whole Pack | 5 | — | 不计分：9 种狼要 9 种群系 |
| `minecraft:husbandry/feed_snifflet` | Little Sniffs | 4 | 60 | 岛上有暖水海底废墟（嗅探兽蛋）时计分 |
| `minecraft:husbandry/froglights` | With Our Powers Combined! | 4 | 60 | 岛上有冷/温/暖三种群系都在（三种青蛙）时计分 |
| `minecraft:husbandry/leash_all_frog_variants` | When the Squad Hops into Town | 4 | 60 | 岛上有冷/温/暖三种群系都在（三种青蛙）时计分 |
| `minecraft:husbandry/obtain_netherite_hoe` | Serious Dedication | 4 | 60 | 岛上有堡垒遗迹时计分 |
| `minecraft:husbandry/allay_deliver_cake_to_note_block` | Birthday Song | 3 | 30 | 岛上有掠夺者前哨站（悦灵）时计分 |
| `minecraft:husbandry/allay_deliver_item_to_player` | You've Got a Friend in Me | 3 | 30 | 岛上有掠夺者前哨站（悦灵）时计分 |
| `minecraft:husbandry/axolotl_in_a_bucket` | The Cutest Predator | 3 | 30 | 岛上有繁茂洞穴（美西螈）时计分 |
| `minecraft:husbandry/kill_axolotl_target` | The Healing Power of Friendship! | 3 | 30 | 岛上有繁茂洞穴（美西螈）时计分 |
| `minecraft:husbandry/make_a_sign_glow` | Glow and Behold! | 3 | 30 | 计分 |
| `minecraft:husbandry/obtain_sniffer_egg` | Smells Interesting | 3 | 30 | 岛上有暖水海底废墟（嗅探兽蛋）时计分 |
| `minecraft:husbandry/plant_any_sniffer_seed` | Planting the Past | 3 | 30 | 岛上有暖水海底废墟（嗅探兽蛋）时计分 |
| `minecraft:husbandry/remove_wolf_armor` | Shear Brilliance | 3 | 30 | 岛上有热带草原/恶地（犰狳）时计分 |
| `minecraft:husbandry/repair_wolf_armor` | Good as New | 3 | 30 | 岛上有热带草原/恶地（犰狳）时计分 |
| `minecraft:husbandry/ride_a_boat_with_a_goat` | Whatever Floats Your Goat! | 3 | 30 | 岛上有山地（山羊）时计分 |
| `minecraft:husbandry/silk_touch_nest` | Total Beelocation | 3 | 30 | 岛上有蜂巢（花林/平原/草甸的蜂巢）时计分 |
| `minecraft:husbandry/tadpole_in_a_bucket` | Bukkit Bukkit | 3 | 30 | 岛上有沼泽/红树林沼泽（青蛙、史莱姆）时计分 |
| `minecraft:husbandry/fishy_business` | Fishy Business | 2 | 15 | 计分 |
| `minecraft:husbandry/place_dried_ghast_in_water` | Stay Hydrated! | 2 | 15 | 岛上有灵魂沙峡谷（下界化石里的失水恶魂）时计分 |
| `minecraft:husbandry/safely_harvest_honey` | Bee Our Guest | 2 | 15 | 岛上有蜂巢（花林/平原/草甸的蜂巢）时计分 |
| `minecraft:husbandry/tactical_fishing` | Tactical Fishing | 2 | 15 | 计分 |
| `minecraft:husbandry/tame_an_animal` | Best Friends Forever | 2 | 15 | 计分 |
| `minecraft:husbandry/wax_on` | Wax On | 2 | 15 | 岛上有蜂巢（花林/平原/草甸的蜂巢）时计分 |
| `minecraft:husbandry/breed_an_animal` | The Parrots and the Bats | 1 | 5 | 计分 |
| `minecraft:husbandry/plant_seed` | A Seedy Place | 1 | 5 | 计分 |
| `minecraft:husbandry/wax_off` | Wax Off | 1 | 5 | 岛上有蜂巢（花林/平原/草甸的蜂巢）时计分 |
