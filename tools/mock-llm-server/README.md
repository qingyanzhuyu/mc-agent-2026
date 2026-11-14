# `tools/mock-llm-server` — 离线测试专用假冒 OpenAI 兼容接口

> ⚠️ **仅限赛前本地离线测试（`AGENTS.md` §2B.3）**：
> 本工具只绑定 `127.0.0.1` 回环地址，用于在**不消耗任何真实大模型 API 额度**的前提下验证 `--llm` 选技能闭环、Token 用量采集、`LLM_TOKEN_BUDGET` 熔断、`summary.md` 与 `REVIEW_REPORT.md` 价值核算。**严禁在正式比赛期间启动或指向本服务**（`preflight` 亦会拦截非官方配置）。

## 启动与联调方法

```bash
# 终端 1：启动本地假冒模型服务（默认监听 127.0.0.1:18080）
python3 tools/mock-llm-server/server.py --port 18080

# 终端 2：临时将 LLM_BASE 指向本地服务、清空快路径代理，跑一轮离线验证
cd code
LLM_BASE=http://127.0.0.1:18080/v1 LLM_KEY=mock-local LLM_PROXY_FAST= \
  uv run python -m mcagent.loop --local-target --soak --llm --user agy_mock1 --max-steps 8 --disconnect-on-exit
```
