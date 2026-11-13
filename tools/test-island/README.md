# `tools/test-island` — 自建测试服虚空岛、场景布置与快照恢复（物理隔离）

> **赛事合规隔离声明（`AGENTS.md` §2B）**
> - 本目录仅用于赛前队内自建 `1.21.11` 原版测试服的环境准备与多轮快照重置。
> - 比赛运行时代码 `code/mcagent/` **严禁且从未导入**本目录下的任何代码；本脚本完全基于 Python 标准库实现，不污染 `pyproject.toml`。

## 功能与用法

在测试服 `server.properties` 开启 `enable-rcon=true`、`rcon.port=25575`、`rcon.password=<密码>` 后：

```bash
# 1) 干跑预览将下发的 RCON 指令（无需连服务器）
python3 tools/test-island/rcon_ctl.py --dry carve-void --center 0,0 --radius 40
python3 tools/test-island/rcon_ctl.py --dry scene night_combat --player agy_soak1

# 2) 分别测试 keepInventory=true 与 keepInventory=false 两种死亡恢复衔接
RCON_PASS=yourpass python3 tools/test-island/rcon_ctl.py gamerule --keep-inventory false

# 3) 免漫长前戏一键跳入黑夜战斗 / 残血饥饿 / 石器前置场景
RCON_PASS=yourpass python3 tools/test-island/rcon_ctl.py scene night_combat --player agy_soak1 --origin 0,70,0

# 4) 每轮演练前清理掉落物/进度或调用停服快照恢复脚本，并把快照号传给 mcagent.loop
SNAP=$(python3 tools/test-island/rcon_ctl.py reset-episode --player agy_soak1 --restore-hook ./restore_world.sh)
cd code && uv run python -m mcagent.loop --local-target --soak --user agy_soak1 --world-snapshot "$SNAP"
```
