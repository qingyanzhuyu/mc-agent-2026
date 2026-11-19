# mcagent — Minecraft Agent API 的最小骨架

协议参考为[上游规范 v1.3.11](../../docs/99-原始记录/api-spec-v1.3.11.md)。比赛服官方双端点：校园网直连 `https://apiin.nmo.net.cn/api/v1`（2026-11-19 无鉴权探活长连接 RTT `4.0–4.3 ms`）与公网备用 `https://mc-api.windywindie.top/api/v1`（2026-11-19 实测 `816–973 ms`），实际部署版本与正式账号标定见[待确认 B18/C1](../../docs/04-待确认.md)。
当前开发分支为 **`feat/arch-converge-v2-agy`（尚未合入 `main`）**，整体验证口径为 **`verified: offline`**（含队内靶场 `local-target` 冷启动复测；官方端点尚待正式账号全量标定）。
目标不是"能打的 bot"，而是**把管道接对**：认证与 Token 持久化复用 / ready 状态机 / 观察 /
可抢占串行动作 / 非幂等超时判重 / 错误分类 / 安全闸 / 赛前门禁 / 打点。

```
client.py         HTTP 客户端：错误封装、.token.json 复用、401 单点重登、429/5xx 退避、16 RPS 三级限速、get_block 缓存、姿态合并、可选接口降级探测
runtime.py        State（单调时钟死亡去重 + 65s safe_mode）/ Safety / 连续异步 SafetyMonitor / Executor（可抢占单飞锁 + 非幂等判重 + 有界 move_to 超时禁重试 + v1.3.11 精选恢复 + 打点）
preflight.py      赛前切换自检：官方端点 / 账号密码 / LLM 配置与 2 路并发闸门 / 8787 网关 / 残留 soak_ctl·mcagent 进程 / 校园网 apiin.nmo.net.cn 误配 7897 代理拦截
smoke.py          冒烟：登录 -> ready -> 状态 -> 背包 -> 事件
probe.py          基线：量 T_status / T_move20 / T_dig1，落盘 json；也是 --pathfinding / --api-capabilities 的入口
probe_path.py     寻路与手动兜底探测（跑的是 skills/nav.py 的原语）
probe_fixture.py  寻路夹具模板与离线校验
planner/          战略目标 objective.py + 异步预取 scheduler.py + 技能菜单 skill_choice.py + 原子动作 decision.py + llm.py（2 路 BoundedSemaphore 与双线路降级）+ llm_fault.py
skills/           L2 空间与脱困原语（voxel_cache / reach / escape / nav / sightline / T1 公共脚本）、world_memo.py（原子 JSON 检查点）与 24 个注册 L3 技能（23 个起步/T1 模块化技能 + emergency_escape）
loop/             比赛与取证主循环：engine.py（同格冷却自旋三级升级）、goals.py（26 项 GoalSpec 台账）、subgoals.py（6 项活谓词）、context.py（脱敏摘要）、strategy.py & strategy_async.py（--strategy llm）、policy.py、auditor.py、tape.py
../tests/         离线故障注入、多线程抢占、L2/L3 与主循环回归单测：1,103 项（无需账号与外网）
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
uv run python -m unittest discover -s tests -v                             # 跑离线单测：1,103 项（无需账号）
uv run python -m mcagent.preflight                                         # 赛前静态自检（含残留进程/7897 代理/并发闸门/占位检查，不联网）
uv run python -m mcagent.client                                            # 只验端点（不需要账号）
uv run python -m mcagent.smoke --dry                                       # 同上，输出更多
uv run python -m mcagent.loop --dry --strategy llm --min-verified offline  # 主循环离线契约干跑（24 技能 / 26 台账项 / 目标级异步战略）
uv run python -m mcagent.smoke                                             # 登录 + ready（需要账号，且要先在 docs/05 占位）
uv run python -m mcagent.probe                                             # 量基线（官方端点需先占号）
```

## 什么算"已验证"（分级准入，见 `AGENTS.md` §2A.6）

| 模块 | 验证等级 | 状态说明 |
|---|---|---|
| `client.py`（401 单点刷新 / `.token.json` 复用 / 16 RPS 三级限速 / `get_block` 代际缓存 / 姿态合并 / `chat` 防 `/` 注入 / 可选接口 `unsupported_api` 探测降级 / `ensure_connected` ≥30s） | `verified: offline` | 2026-11-19 官方双端点无鉴权探活（`apiin.nmo.net.cn` `4.0–4.3 ms`、`mc-api.windywindie.top` `816–973 ms` 均返 401）+ 离线故障注入单测全过；**官方正式账号登录待验** |
| `runtime.py`（单调时钟死亡去重 + 65s `safe_mode` + 非幂等超时判重 + `emergency()` 抢占锁 + 连续异步 `SafetyMonitor` + 有界 `move_to` 超时禁重试 + `v1.3.11` 精选恢复） | `verified: offline` | 离线故障注入/多线程抢占单测通过 + `local-target` 实机 `void_risk` 抢占复测；**官方真机待验** |
| `preflight.py`（官方端点 / 凭据 / `LLM_MAX_CONCURRENCY <= 2` / 8787 网关 / 残留 `soak_ctl`·`mcagent` 进程 / `apiin.nmo.net.cn` 误配 `7897` 代理拦截） | `verified: offline` | 离线单测全覆盖 |
| `skills/`（L2 `voxel_cache` / `reach` / `escape` / `nav`（含 `v1.3.11` 精选恢复） / `sightline` + `world_memo` 原子 JSON 检查点 + 24 个注册 L3 技能含 `emergency_escape`） | `verified: offline` | 离线契约与回归单测全过 + `local-target` 2 轮 240s 冷启动清障/脱困复测；技能仍保持 `verified="offline"`，**待官方服抽测后升 `"live"`** |
| `loop/`（`goals.py` 26 项 `GoalSpec` 台账 + `subgoals.py` 6 项活谓词 + `context.py` 脱敏摘要 + `engine.py` 同格冷却自旋三级升级 + `strategy.py`/`strategy_async.py` `--strategy llm` + `starter_skills.py` 含 `v1.3.11` 精选恢复） | `verified: offline` | 离线单测与 `--dry` 全过；`local-target` 第 2 轮 `de7_v2_test02` 0 死亡、`/dig` 33/33、`/craft` 7/7、达成 5 项台账目标拿 10 分、累计 1,746 tokens、`/get_block` 551 次（`local-target` 不入正式预算，**官方真机待验**） |
| `planner/`（`objective.py`、`scheduler.py`、`skill_choice.py`、`decision.py`、`llm.py` 2 路 `BoundedSemaphore` 与双线路降级、`llm_fault.py`） | `verified: offline` | 离线单测全覆盖（含方法反射、强类型校验与 `Objective` 菜单校验）；`decide()` 早期真机 3/3 抽测；**赛服官方模型下 `OBJECTIVE_SYSTEM_PROMPT` 待标定** |
| `smoke.py` / `probe.py` | `--dry` 已验 / `live` 待验 | 编译通过 + `--dry` 退 0；**官方账号登录、`probe` 三组基线与 `--api-capabilities --live` 待占号标定** |

当前分支 `feat/arch-converge-v2-agy` 尚未合入 `main`；待官方端点账号登录、基线测量与真机技能抽测跑通后，再按 `AGENTS.md` §2A.6 升格 `"live"` 并合入 `main`。

## 目录约定

- `mcagent/.token.json` — 自动持久化的登录凭证（`0600` 权限，已 gitignore，切勿提交）
- `mcagent/probe/*.json` — 每次测量的原始数据，别删，后面调参要用
- `mcagent/logs/*.jsonl` — 每个动作一行（含 ms、错误码、重试次数），是复盘的原料
