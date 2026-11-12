## 这个 PR 做了什么

<!-- 一句话。文档说清改了哪条，代码说清改了什么行为 -->

## 类型

- [ ] 文档：新增/修正**事实**（附来源）
- [ ] 文档：新增/修正**判断**（附推翻条件）
- [ ] 代码：新功能
- [ ] 代码：修 bug
- [ ] 认领/回答 `docs/04-待确认.md` 里的条目

## 自检（DoD，见 AGENTS.md §5）

- [ ] 在 `docs/06-工作日志.md` 追加了一条记录
- [ ] 文档改动都附了来源；新判断写了推翻条件
- [ ] `cd code && uv run python -m compileall -q mcagent tests` 通过
- [ ] `cd code && uv run python -m unittest discover -s tests` 全绿
- [ ] `cd code && uv run python -m mcagent.smoke --dry` 返回 0
- [ ] 提交信息格式正确，代码与文档尽量分提交以便独立回滚
- [ ] 没有多出来的 `.env` / `.token.json` / `sk-...` / `logs/` / `__pycache__`

## 有没有碰冻结面

- [ ] 没有
- [ ] 有，动了 `client.Client` / `runtime` 的已有签名或返回结构 —— 理由：

<!-- 动了冻结面必须写理由，并由人类批准后才能合 -->

## 验证等级（见 AGENTS.md §2A.6）

- [ ] `verified: offline`（纯协议/状态机/校验逻辑，已附 `code/tests/` 离线单测并跑通，`smoke --dry` 通过）
- [ ] `verified: live`（上过真实 bot，占位在 `docs/05` 账号表里登记的账号是：____）

## 影响面

<!-- 谁会因为这个改动需要改自己的代码？会不会影响别人的分支？ -->
