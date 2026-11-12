# mcagent — Minecraft Agent API 的最小骨架

对应规范 **v1.3.1**（`https://mc-api.windywindie.top/api/v1`）。目标不是"能打的 bot"，
而是**先把管道接对**：认证 / ready 状态机 / 观察 / 串行动作 / 错误分类 / 安全闸 / 打点。

```
client.py    HTTP 客户端：错误封装、token 自动重登、错误码分类、按动作给超时
runtime.py   State（状态+事件增量）/ Safety（安全闸）/ Executor（单飞锁+重试+打点）
smoke.py     D0 冒烟：登录 -> ready -> 状态 -> 背包 -> 事件
probe.py     D1 基线：量 T_status / T_move20 / T_dig1，落盘 json
loop.py      D2+ 管道演示：木->工作台->木镐（不是策略）
```

## 三条必须遵守的约束（写代码时别退回原样）

1. **业务错误一律 HTTP 200**，只看 `payload["ok"]`，别看状态码。
2. **本机注册表有个死代理 127.0.0.1:7897**，`requests` 会读它 → 必须 `trust_env=False`
   （`client.py` 已做）。要显式走代理就填 `MC_PROXY`。
3. **动作互斥且同步阻塞**：一个动作没返回前不能发第二个（会 `busy`）。
   所以 `Executor` 里有单飞锁；`/status`、`/events`、`/find_blocks` 不占锁，可以并行读。

## 快速开始

> 所有命令在 **`code/`** 目录下执行（包名是 `mcagent`）。

```bash
cp .env.example .env      # 填 MC_USER / MC_PASS
python -m mcagent.client          # 只验端点
python -m mcagent.smoke           # 登录 + ready
python -m mcagent.probe           # 量基线
python -m mcagent.loop --max-steps 12
```

## 待验证的两件事（`probe` 里没覆盖）

- **`craft` 的 `count` 语义**：是"合成几次"还是"合成几个"？
  第一次跑 `loop.py` 时看 `craft oak_planks, count=16` 到底给了多少木板。
- **`/events` 的 `seq` 跨重连是否连续**：断线重连后 `since` 还能不能接上。
  测法：记下 `last_seq`，`/disconnect` + `/connect`，再用同一个 `since` 拉一次。

## 目录约定

- `probe/*.json` — 每次测量的原始数据，别删，后面调参要用
- `logs/*.jsonl` — 每个动作一行（含 ms、错误码、重试次数），是 D6 分析的原料
