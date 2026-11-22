# mcagent — Minecraft Agent API 的最小骨架

协议参考为[上游规范 v1.3.11](../../docs/99-原始记录/api-spec-v1.3.11.md)。比赛服官方双端点：校园网直连 `https://apiin.nmo.net.cn/api/v1`（2026-11-19 官方校园网实测 `T_status` 稳态 `7.34 ms`、`T_move20=3648 ms`、`T_dig1=2146 ms`）与公网备用 `https://mc-api.windywindie.top/api/v1`（2026-11-19 无鉴权探活 `816–973 ms`）。
当前开发分支为 **`feat/arch-converge-v2-agy`（待最终合入 `main`）**，已完成官方校园网 `smoke`、`probe`、三项故障演练 3/3 PASS 与 2 小时官方长跑取证，4 项核心技能已升格 **`verified="live"`**；其余 75 项技能（含阶段二 T2/T3 与阶段三 T4 技能）保持 **`verified="offline"`**（附 `local-target` 冒烟），待后续真机验收后再合入 `main`。
目标不是"能打的 bot"，而是**把管道接对**：认证与 Token 持久化复用 / ready 状态机 / 观察 /
可抢占串行动作 / 非幂等超时判重 / 错误分类 / 安全闸 / 赛前门禁 / 打点。

```
client.py         HTTP 客户端：错误封装、.token.json 复用、401 单点重登、429/5xx 退避、16 RPS 三级限速、get_block 缓存、姿态合并、可选接口降级探测
runtime.py        State（单调时钟死亡去重 + 65s safe_mode + 受控图腾单次白名单守卫）/ Safety / 连续异步 SafetyMonitor / Executor（可抢占单飞锁 + 非幂等判重 + 有界 move_to 超时禁重试 + v1.3.11 精选恢复 + 打点）
preflight.py      赛前切换自检（12 项静态门禁）：官方端点 / 账号密码 / LLM 配置与 2 路并发闸门 / 8787 网关 / 残留 soak_ctl·mcagent 进程 / 校园网 apiin.nmo.net.cn 误配 7897 代理拦截
smoke.py          冒烟：登录 -> ready -> 状态 -> 背包 -> 事件
probe.py          基线：量 T_status / T_move20 / T_dig1，落盘 json；也是 --pathfinding / --api-capabilities 的入口
probe_path.py     寻路与手动兜底探测（跑的是 skills/nav.py 的原语）
probe_fixture.py  寻路夹具模板与离线校验
planner/          战略目标 objective.py + 异步预取 scheduler.py + 技能菜单 skill_choice.py + 原子动作 decision.py + llm.py（2 路 BoundedSemaphore 与双线路降级）+ llm_fault.py
skills/           L2 空间与脱困原语（voxel_cache / reach / escape / nav / sightline / furnace_lease 等）、world_memo.py（21 类指纹 + 原子 JSON 检查点）与 79 个注册 L3 技能（4 项 verified="live" + 75 项 verified="offline"，含 emergency_escape 及阶段三 18 项 T4 技能）
loop/             比赛与取证主循环：engine.py（同格冷却自旋三级升级 + 30s 沿路扫描）、goals.py（104 项 GoalSpec 台账：89 计分项 / 2,730 分上限 [80.6%]，6 graduated / 1 candidate / 97 draft）、subgoals.py（10 项活谓词 + 10 类工作站）、context.py（脱敏摘要）、strategy.py & strategy_async.py（--strategy llm）、policy.py、auditor.py、tape.py
../tests/         离线故障注入、多线程抢占、L2/L3 与主循环回归单测：1,421 项（无需账号与外网；另有 ../../tools/local-api-bridge/test/ 11 项 Node 网关契约单测）
```

分层与写技能的规则见仓库根 `AGENTS.md` §9。

> 所有命令在 **`code/`** 目录下通过 `uv run` 执行（根目录 `pyproject.toml` + `uv.lock` 锁定依赖）。

## 三条必须遵守的约束（写代码时别退回原样）

1. **业务错误大多 HTTP 200，同时兼顾网关/认证 HTTP 401/429/5xx**：优先看 `payload["ok"]`，但 `call()` 亦统一兜底 HTTP 401 单点刷新与 429/5xx 重试。
2. **本机注册表可能挂死代理 127.0.0.1:7897**，`requests` 默认会读它 → 必须 `trust_env=False`（`client.py` 已做）；且**校园网入口 `apiin.nmo.net.cn` 严禁挂 `7897` 代理**（`preflight.py` 已拦截）。要显式走代理仅限公网/必要链路并填 `MC_PROXY`。
3. **动作互斥且同步阻塞，但紧急避险可抢占**：一个动作没返回前不能发第二个普通动作（会 `busy`）。
   所以 `Executor` 内置条件变量单飞锁；后台连续异步 `SafetyMonitor` 发现危险时调 `Executor.emergency()` 立即发 `/stop` 并插队执行逃生动作；`/status`、`/events`、`/find_blocks` 不占锁，可以并行读。

## 快速开始

```bash
cp mcagent/.env.example mcagent/.env                                       # 填 MC_USER / MC_PASS（需要真机时）
uv run python -m unittest discover -s tests -v                             # 跑离线单测：1,421 项（无需账号）
uv run python -m mcagent.preflight                                         # 赛前静态自检（12 项门禁：残留进程/7897 代理/并发闸门/占位检查，不联网）
uv run python -m mcagent.client                                            # 只验端点（不需要账号）
uv run python -m mcagent.smoke --dry                                       # 同上，输出更多
uv run python -m mcagent.loop --dry --strategy llm --min-verified offline  # 主循环离线契约干跑（79 技能 / 104 台账项 / 目标级异步战略）
uv run python -m mcagent.smoke                                             # 登录 + ready（需要账号，且要先在 docs/05 占位）
uv run python -m mcagent.probe                                             # 量基线（官方端点需先占号）
```

## 什么算"已验证"（分级准入，见 `AGENTS.md` §2A.6）

| 模块 | 验证等级 | 状态说明 |
|---|---|---|
| `client.py`（401 单点刷新 / `.token.json` 复用 / 16 RPS 三级限速 / `get_block` 代际缓存 / 姿态合并 / `chat` 防 `/` 注入 / 可选接口 `unsupported_api` 探测降级 / `ensure_connected` ≥30s） | `verified: live` | 2026-11-19 官方校园网实测通过：0.5s 登录（TTL 24h）、`MC_MAX_RPS` 4/8/12/16 零限流、并发 401 单飞刷新 `login_calls=1`、断线 63.11s 重连与 B18/C10 接口实测 + 离线单测全绿 |
| `runtime.py`（单调时钟死亡去重 + 65s `safe_mode` + 非幂等超时判重 + `emergency()` 抢占锁 + 连续异步 `SafetyMonitor` + 有界 `move_to` 超时禁重试 + `v1.3.11` 精选恢复） | `verified: live` | 2026-11-19 官方三项故障演练 3/3 PASS（断线重连 / `65.2s` `safe_mode` 自愈 / 并发 401）、C16 虚空保护 `0/12` 误报、2h 官方长跑 **0 死亡 0 崩溃** + 离线故障注入单测全绿 |
| `preflight.py`（12 项门禁：官方端点 / 凭据 / `LLM_MAX_CONCURRENCY <= 2` / 8787 网关 / 残留 `soak_ctl`·`mcagent` 进程 / `apiin.nmo.net.cn` 误配 `7897` 代理拦截） | `verified: live` | 2026-11-19 官方模式 `preflight --apply --live` **15/15 全绿** + 离线单测全覆盖 |
| `skills/`（L2 原语 + `world_memo` 21 类指纹与原子检查点 + `furnace_lease` + **79 个注册 L3 技能**） | **4 `live` / 75 `offline`** | 官方 2h 长跑完整跑通且 0 Bug 的 4 项技能已升格 `verified="live"`（`gather_wood`、`craft_gear`、`mine_stone`、`emergency_escape` 6/6）；其余 **75 项（含阶段二 37 项与阶段三 18 项 T4 技能）诚实保持 `verified="offline"`**（阶段二与阶段三 `crafter_chain`/`ride_strider_boost`/`jukebox_meadow` 及 18 项空包拦截已在 `local-target` 冒烟通过，待官方服真机复测后逐项升格） |
| `loop/`（`goals.py` **104 项 `GoalSpec` 台账** [89 计分 / 2,730 分上限，80.6%，`6 graduated / 1 candidate / 97 draft`] + 共享子图 DAG ROI + 被动机会根目标门控 + 30s 沿路扫描 + `subgoals.py` 10 项活谓词 + `strategy_async.py`） | 部分 `live` / 阶段二三 `offline` | 官方 2h 长跑前 `124.7s` 拿下 5 项起步目标、230 条进度曲线与 `INC-005` 100% 离线 `--replay` 落地；暴露的 4 环死锁与 Token 烧爆已于 `c03c26e` 闭环修复（待新一轮官方长跑复验）；全量 `1,421` 单测与 `--dry` 全绿 |
| `planner/`（`objective.py`、`scheduler.py`、`skill_choice.py`、`decision.py`、`llm.py` 2 路 `BoundedSemaphore` 与双线路降级、`llm_fault.py`） | `verified: live` | 2026-11-19 官方 AIA `api.nju-aia.com`（`qwen3.6-35b-a3b`）快路径 `4/4`（`p50=1652.7ms`）与长跑 1,306 次实测（`p50=3.83s`）标定通过 + 离线单测全覆盖 |
| `smoke.py` / `probe.py` | `verified: live` | 官方校园网 `smoke`（`0.5s` 登录 `ready=True`）、`probe` 三组基线、`--api-capabilities --live`、C16 标定与三项故障演练全部实测完成并归档 `../../results/probe/` |

当前分支 `feat/arch-converge-v2-agy` **尚待最终合入 `main`**（阶段二/三等 75 项技能仍为 `verified="offline"`，待后续真机验收并完成 `main` 合并）。

## 目录约定

- `mcagent/.token.json` — 自动持久化的登录凭证（`0600` 权限，已 gitignore，切勿提交）
- `mcagent/probe/*.json` — 每次测量的原始数据，别删，后面调参要用
- `mcagent/logs/*.jsonl` — 每个动作一行（含 ms、错误码、重试次数），是复盘的原料
