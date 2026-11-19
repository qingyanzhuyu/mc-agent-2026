# 运行诊断与进度汇总（`official`）

- **Git 版本**：`090a04a` (branch `feat/arch-converge-v2-agy`, dirty=`True`)
- **世界快照编号**：`unspecified` · **运行时长**：`6303.9s`
- **死亡/重生**：`0` 次
- **服务端确认进度**：`4` 项（v2 分值 `10`）
- **背包里程碑代理**：`2` 项 `['story/mine_stone', 'story/upgrade_tools']`（代理分值 `10`）
- **台账目标达成**：`5` 项（`prep_wood` (`69.4s`)、`prep_wooden_pickaxe` (`74.8s`)、`mine_stone` (`118.9s`)、`upgrade_tools` (`123.9s`)、`prep_stone_sword` (`124.7s`)）
- **LLM 决策遥测**：调用 `1306` 次，成功率 `862/1306` (`66.0%`)，p50=`3831ms`，p95=`5224ms`，max=`7815ms`，回退规则次数=`444`
- **决策来源占比**：选技能 共 `1406` 次（`llm` `858` 次 (`61.0%`) · `llm_fallback_rule` `0` 次 (`0.0%`) · `rule` `548` 次 (`39.0%`)）；动作步 共 `873` 步（`llm` `214` 步 (`24.5%`) · `llm_fallback_rule` `0` 步 (`0.0%`) · `rule` `659` 步 (`75.5%`)）
- **战略目标决策**：共 `8772` 次（`llm` `4` 次 (`0.0%`) · `llm_fallback_rule` `3` 次 (`0.0%`) · `rule` `8765` 次 (`99.9%`)）

## Token 花费
- **累计用量**：输入 `2,911,145` · 输出 `71,172` · 合计 `2,982,317` tokens（`llm_call` `1306` 次）
- **初赛公告额度占比**（输入 ÷ 800 万 / 输出 ÷ 150 万）：输入 `36.389%` · 输出 `4.745%`
- **8 小时满场推算**（按当前 `6303.9s` 外推 28,800s）：预计输入 `13,299,858`（占 800 万额度的 `166.2%`）· 预计输出 `325,156`（占 150 万额度的 `21.7%`）
- **预算上限（`LLM_TOKEN_BUDGET`）**：`0`（仅记账、不熔断）

## 一、代码与安全缺陷榜（`category = bug`，优先修）
- **[P2] `INC-001` `MOVE_RANGE_SHORTFALL`**（×3）：move_to 返回成功但水平距目标仍差 7.1 格 → `INC-001-P2-move_range_shortfall-scout_island.json`

## 二、策略瓶颈与震荡榜（`category = strategy`）
- **[P1] `INC-006` `STUCK_NO_PROGRESS`**（×39）：连续 12 步位移 <0.8 格且背包无变化 → `INC-006-P1-stuck_no_progress-scout_island.json`
- **[P2] `INC-002` `REPLAN_OSCILLATION`**（×495）：目标 explore:island 在 180s 内连续失败 3 次 (replan) → `INC-002-P2-replan_oscillation-scout_island.json`
- **[P2] `INC-003` `REPLAN_OSCILLATION`**（×119）：目标 goal:sleep_in_bed 在 180s 内连续失败 3 次 (replan) → `INC-003-P2-replan_oscillation-sleep_in_bed.json`
- **[P2] `INC-005` `REPLAN_OSCILLATION`**（×92）：目标 craft:furnace 在 180s 内连续失败 3 次 (replan) → `INC-005-P2-replan_oscillation-craft_gear.json`
- **[P2] `INC-007` `REPLAN_OSCILLATION`**（×2）：目标 acq:diamond 在 180s 内连续失败 3 次 (replan) → `INC-007-P2-replan_oscillation-mine_diamond.json`
- **[P2] `INC-008` `REPLAN_OSCILLATION`**（×1）：目标 explore:island 在 180s 内连续失败 10 次 (network) → `INC-008-P2-replan_oscillation-scout_island.json`
- **[P2] `INC-009` `SKILL_SPAM_LOOP`**（×1）：技能 scout_island 重复空转 531 次 ([{"radius": 36}, {"radius": 32}, {"radius": 64}]) → `INC-009-P2-skill_spam_loop-scout_island.json`
- **[P2] `INC-010` `SKILL_SPAM_LOOP`**（×1）：技能 mine_stone 重复空转 12 次 ([{"count": 8}, {"count": 16}, {"count": 32}]) → `INC-010-P2-skill_spam_loop-mine_stone.json`
- **[P3] `INC-004` `LLM_INVALID_OR_TIMEOUT`**（×444）：LLM 未产出合法技能选择 (4492ms): explore:island 刚失败过，还在冷却，换一个目标 → `INC-004-P3-llm_invalid_or_timeout-session.json`

## 三、各技能执行统计

| 技能 | 启动 | 完成 | Replan | 预算中止 | Bug |
|---|---:|---:|---:|---:|---:|
| `build_and_enter_portal` | 19 | 0 | 3 | 0 | 0 |
| `craft_gear` | 142 | 4 | 138 | 0 | 0 |
| `emergency_escape` | 6 | 6 | 0 | 0 | 0 |
| `gather_wood` | 13 | 1 | 2 | 0 | 0 |
| `hunt_mob` | 534 | 0 | 4 | 0 | 0 |
| `mine_diamond` | 18 | 0 | 18 | 0 | 0 |
| `mine_stone` | 12 | 11 | 1 | 0 | 0 |
| `scout_island` | 531 | 29 | 502 | 0 | 0 |
| `sleep_in_bed` | 131 | 0 | 131 | 0 | 0 |

## 四、已知环境波动计数（仅统计，不入缺陷榜）
- `hunt_mob:env_blocked`: 530 次
- `scout_island:replan`: 501 次
- `craft_gear:replan`: 138 次
- `sleep_in_bed:replan`: 131 次
- `mine_diamond:replan`: 18 次
- `build_and_enter_portal:env_blocked`: 16 次
- `gather_wood:env_blocked`: 10 次
- `hunt_mob:replan`: 4 次
- `build_and_enter_portal:replan`: 3 次
- `gather_wood:path_not_found`: 2 次
- `mine_stone:replan`: 1 次
- `scout_island:network`: 1 次

## 五、接口耗时与高频行为剖析

### 接口耗时 Top（按总耗时）

| 接口 | 次数 | 成功 | 失败 | 总耗时(ms) | 均耗(ms) | 最大(ms) | 占运行时长 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `/events` | 8951 | 8950 | 1 | 1745521.7 | 195.0 | 9596.4 | 27.7% |
| `/get_block` | 21424 | 21423 | 1 | 1347366.4 | 62.9 | 9485.3 | 21.4% |
| `/status` | 14694 | 14691 | 3 | 845654.0 | 57.6 | 10007.7 | 13.4% |
| `/dig` | 212 | 211 | 1 | 383523.6 | 1809.1 | 11675.1 | 6.1% |
| `/find_blocks` | 1605 | 1605 | 0 | 114774.8 | 71.5 | 268.6 | 1.8% |
| `/move_to` | 622 | 579 | 43 | 91842.8 | 147.7 | 15907.3 | 1.5% |
| `/inventory` | 1135 | 1135 | 0 | 63761.1 | 56.2 | 1442.3 | 1.0% |
| `/get_entities` | 530 | 530 | 0 | 33235.5 | 62.7 | 153.6 | 0.5% |

### 高频同参数技能调用（`started >= 3`）
- `scout_island` `{"radius": 36}`：启动 `527` 次，完成 `27`，Replan `500`，环境阻塞 `0`，累计耗时 `1545.60s`
- `hunt_mob` `{"count": 1, "drop": "none", "target": "spider"}`：启动 `323` 次，完成 `0`，Replan `0`，环境阻塞 `323`，累计耗时 `134.70s`
- `sleep_in_bed` `{}`：启动 `131` 次，完成 `0`，Replan `131`，环境阻塞 `0`，累计耗时 `57.40s`
- `craft_gear` `{"item": "furnace"}`：启动 `94` 次，完成 `0`，Replan `94`，环境阻塞 `0`，累计耗时 `14.40s`
- `hunt_mob` `{"count": 1, "drop": "string", "target": "spider"}`：启动 `78` 次，完成 `0`，Replan `0`，环境阻塞 `78`，累计耗时 `27.60s`
- `hunt_mob` `{"count": 1, "drop": "none", "target": "creeper"}`：启动 `58` 次，完成 `0`，Replan `0`，环境阻塞 `58`，累计耗时 `25.80s`
- `hunt_mob` `{"count": 1, "drop": "none", "target": "zombie"}`：启动 `45` 次，完成 `0`，Replan `0`，环境阻塞 `45`，累计耗时 `19.60s`
- `craft_gear` `{"item": "stone_pickaxe"}`：启动 `45` 次，完成 `1`，Replan `44`，环境阻塞 `0`，累计耗时 `11.90s`
