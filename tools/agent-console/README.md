# AIAMC Agent Console (`tools/agent-console`)

AIAMC Harness 赛道的零第三方依赖可视化监控与双模式启动控制台（基于 Python 标准库 `http.server.ThreadingHTTPServer`，前端位于 `tools/agent-console/web/`）。

## 1. 一键启动方式（所有前端与启动器均在同一目录）

本目录 `tools/agent-console/` 自包含后端、前端（`web/`）与全平台一键启动器，**点开即自动拉起服务并打开浏览器**（若已在运行则直接复用现有服务打开页面）：

- **Linux 桌面双击**：直接双击 `tools/agent-console/AIAMC-Console.desktop`（或在终端执行 `./tools/agent-console/start_console.sh`）
- **Windows / WSL2 双击**：直接双击 `tools/agent-console/start_console.bat`
- **macOS 双击**：直接双击 `tools/agent-console/start_console.command`
- **停止后台控制台**：
  ```bash
  ./tools/agent-console/start_console.sh --stop
  ```
- **手动前台启动并自动开浏览器**：
  ```bash
  python3 tools/agent-console/server.py --open --port 8795
  ```

启动后自动打开 `http://127.0.0.1:8795/`。

> **说明**：控制台仅绑定本机回环地址（`127.0.0.1` / `localhost` / `::1`），不支持对外网网卡监听。

---

## 2. 双模式隔离设计（Match Mode vs Local Test Mode）

后端 `build_launch_spec()` 对两种运行模式实施强校验与环境隔离，防止开发配置误打官方服或比赛模式漏配合规参数：

| 维度 | 比赛模式（`mode="match"`） | 日常自搭服测试（`mode="local"`） |
| --- | --- | --- |
| **端点白名单 / 黑名单** | 仅允许官方端点：`https://apiin.nmo.net.cn/api/v1`（校园网直连）或 `https://mc-api.windywindie.top/api/v1`（公网备用）；拒绝本地网关 | 仅允许本地或局域网联调网关（默认 `http://127.0.0.1:8787/api/v1`）；严禁填写官方端点 |
| **启动标识** | 强制附带 `--live` | 强制附带 `--local-target --user <bot_user>`（隔离测试角色） |
| **账号与占位门禁** | 读取 `code/mcagent/.env.match` 正式账号；拒绝占位账号/密码（如 `qyzy_dev` / `localdev`）；启动前必须勾选确认已在 `docs/05` 账号表占位（`account_reserved=True`） | 使用表单指定的测试角色名（`^[A-Za-z0-9_.-]{1,32}$`，默认 `dev_console_bot`） |
| **LLM 合规锁定** | 强制 `LLM_PROFILE=official`、`LLM_MODEL=default`、`LLM_MAX_CONCURRENCY` 钳制在 `1..2`；开启 LLM 时拒绝 `sk-mRbe` 公共压测 key | 支持 `llm_profile="custom"`（读取 `.env` 中的 `LLM_CUSTOM_*`）、`"official"` 或 `"none"`（自动切 `--strategy rule`） |
| **网络代理与训练开关** | 校园网直连（`apiin.nmo.net.cn`）强制清空 `MC_PROXY=""`；强制 `MCAGENT_TRAIN_ON_OFFICIAL=""` | 强制 `MC_PROXY=""` 与 `MCAGENT_TRAIN_ON_OFFICIAL=""` |
| **运行形态** | 支持 `campaign` / `focus`、目标过滤、时间/步数预算与 `world_memo.json` 断点检查点 | 支持 `campaign` / `focus`（可传入单技能与 JSON 参数 `--skill-args`）、本地网关 `/healthz` 探活与快捷压测预设 |

比赛模式下还提供 **Preflight 门禁面板**（`GET /api/preflight`、`POST /api/preflight/apply-match`），可触发静态检查、联网测 RTT 以及将 `.env.match` 应用到 `.env`。

---

## 3. 安全与零密钥保证

1. **回环监听与 CSRF 防护**：
   - `create_server()` 拒绝非回环主机地址（如 `0.0.0.0`）。
   - `ConsoleHandler._check_local_origin()` 校验请求 `Host` 与 `Origin` 头，拒绝非本机来源请求（返回 `403`）。
2. **环境变量严格脱敏（Zero-Secret Summary）**：
   - `read_env_summary()` 明确过滤 `MC_PASS`、`LLM_KEY`、`LLM_CUSTOM_KEY`，接口返回中**绝不出现明文密码或 API Key**，仅在 `secrets` 字段返回布尔状态标志（如 `has_mc_pass`、`mc_pass_is_localdev`、`has_llm_key`、`llm_key_is_test_prefix`、`has_llm_custom_key`）。
3. **命令展示脱敏**：
   - `build_launch_spec()` 仅将凭据注入子进程 `env_overrides`，前端展示与控制台日志使用的 `cmd_display` 与 `meta` 不包含任何密钥或密码。
4. **路径边界收敛**：
   - 静态资源读取限制在 `tools/agent-console/web/` 内；运行记录查询 `resolve_run_id()` 限制在 `code/mcagent/logs/` 与 `results/` 内，防止目录穿越。

---

## 4. 主办方交付清单页签（Organizer Handover Tab）

点击顶部「📦 交付清单」或底部「📦 主办方交付清单」页签，前端调用 `GET /api/handover`（`build_handover_audit()`）实时展示与核查：

- **技能与目标台账统计**：统计 `mcagent.skills.REGISTRY` 总数及 `live` / `offline` 验证分布，以及 `mcagent.loop.goals.GOALS` 总数、计分项数量、满分分值与阶段分布。
- **运行包与评审材料检查**：核对 `pyproject.toml`、`uv.lock`、`code/mcagent/`、`code/mcagent/.env.match.example`、`code/tests/`、`docs/99-原始记录/NMO_API_LICENSE.txt` 及 `README.md`、`report.pdf`、`demo.md`、`results/` 是否就绪。
- **敏感/禁用项隔离核查**：提示本地禁带目录与凭据文件（`.env`、`.env.match`、`.token.json`、`tools/local-api-bridge/`、`tools/soak-autofix/`、`code/mcagent/logs/`），并通过 `git ls-files` 校验 `tracked_secrets` 为空（确保零密钥进入 Git 跟踪）。
- **标准复现与比赛命令**：直接提供 `uv sync --frozen`、Preflight 自检、全量/保守比赛启动命令以及无账号离线验证命令。

---

## 5. 离线测试

控制台逻辑与回环 HTTP 接口的离线单测位于 `code/tests/test_agent_console.py`（零联网、零真实登录）：

```bash
cd code && uv run python -m unittest tests/test_agent_console.py -v
```
