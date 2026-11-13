# AGENTS.md — 在这个仓库工作的 agent 必读

这个仓库有**多个 DSH 实例和人在同时改**。下面的不是建议，是准入条件。
违反其中任意一条，你的改动会被整体回退。

---

## 0. 开工前必读（按顺序，别跳）

1. `skill/SKILL.md` — **这份仓库的使用手册**（定位 / 任务路由 / 五个坑 / DoD）
   - 要跑真机实验时，再加读 `skill/live-experiment/SKILL.md`（同步 → 起网关 → smoke → probe 全流程）
2. `README.md` — 这个是干什么的
3. `docs/01-关键判断.md` — 当前共识，**别重复论证已经定下的判断**
4. `docs/04-待确认.md` — **别问已经有人认领的问题**
5. `docs/05-分工与契约.md` — 你能改哪些文件、模块接口是什么
6. 要动 `skills/` `planner/` `probe*.py` 时，再读本文件 **§9 Harness 框架规则**（三层动作抽象）

---

## 1. 只改你负责的文件

| 路径 | 谁能改 |
|---|---|
| `docs/00-赛事事实.md` | 任何人，**但每改一条必须附来源** |
| `docs/01-关键判断.md` | 任何人，新判断编号顺延，**必须写推翻条件** |
| `docs/02` `docs/03` | 接口负责人 / 策略负责人（见 `docs/05`） |
| `docs/04-待确认.md` | 任何人（认领机制） |
| `docs/05-分工与契约.md` | 只有人类改，agent 只在"当前状态"列登记 |
| `docs/06-工作日志.md` | 任何人，**只追加不改历史** |
| `docs/07-评审材料.md` | 工程评审负责人汇总，其他人只提建议 |
| `code/mcagent/client.py` `runtime.py` | 接口负责人 |
| `code/mcagent/planner/` `skills/` | 策略负责人（见 §2A.6 分级验证要求、§9 框架规则） |
| `code/mcagent/skills/nav.py` `items.py` | 策略负责人；**改了动作顺序或常量必须重跑 `probe --pathfinding --manual-only`**（§9.2 第 6 条） |
| `code/tests/` | 接口负责人 / 策略负责人（各自模块的离线单测，必须无网可跑） |
| `pyproject.toml` `uv.lock` | 接口负责人（改依赖必须同步更新 `uv.lock`） |
| `code/mcagent/probe.py` `probe_path.py` `probe_fixture.py` `smoke.py` | 任何人（**加功能可以，改行为要先说**） |
| `tools/local-api-bridge/` | 接口负责人 / 任何人（赛前本地/局域网联调 Mock 网关，须与 `code/mcagent/` 保持零耦合） |
| `plan/` | 任何人，只追加不重写 |

未列出的文件默认**只读**。要改先到 `docs/05` 认领一行。

---

## 2. 协作禁令与赛事合规红线

### 2A. 仓库协作六条禁令

1. **不提交任何密钥与会话凭证**：`.env`、`.token.json`、`sk-...`、皮肤站密码、bearer token、群里的压测 key。只放 `.env.example`。
2. **不编造接口行为**：接口语义只能引用 `docs/99-原始记录/api-spec-v1.3.1.md` 并写清 § 号。猜的写进 `docs/04`，不许写进 `docs/00`。
3. **不破坏这三项底层不变量**（确需调整须附规范 § 号与回归单测，并经人类批准）：
   - `client.py` 的 `trust_env=False` 与分路代理机制（本机环境可能挂死代理 `127.0.0.1:7897`，且 MC API 直连与 LLM 代理最优路径相反）
   - `client.py` 的 `CODE_CLASS` 容错边界（新增/细化可，不得删除已有错误码，也不得把单次坏参 `bad_args` 改回会杀死 8h 进程的 `FATAL`）
   - `Executor` 的可抢占单飞锁（动作互斥是规范 §0 硬约束，且紧急逃生 `emergency()` 必须优先于普通排队动作）
   - 调用方同样**不得绕过 `Executor` 直接调 `client` 的动作方法**——会丢日志、丢非幂等判重、破坏抢占互斥
4. **不 `push --force`、不 rebase 别人的分支、不改别人的提交**。分支与同步的完整规则见 §6。
5. **不在没有人类确认的情况下用正式比赛账号登录**（见 §3，会踢人）。
6. **代码分级准入（禁止未验证逻辑混入 `main`）**：
   - **`verified: offline`（协议封装 / 状态去重 / 错误恢复 / 校验器 / 纯算法）**：必须配套 `code/tests/` 离线故障注入或边界单测且全绿，可在不占用真机账号的情况下合入 `main`。
   - **`verified: live`（具体游戏技能 `skills/`、距离/时间阈值、提示词与主循环）**：强依赖 Minecraft 服务端真实物理与延迟，没上过真机跑通的一律先放分支或登记 `docs/04`，真机验证留存日志后再合入 `main`。
   - 严禁把未注明验证等级的半成品合入 `main`——一个看起来能跑但没人验证过的模块，会让下一个 agent 基于错误前提工作。

### 2B. 赛事官方四条合规红线（触发即判罚/取消资格，来源：`[公告][细则][规范]`）

1. **严禁人工代打与当场硬编码**：赛中允许修改提示词、调整框架参数、重启程序，但**禁止**人工直接操控角色执行游戏行为，禁止人工探图后向代码硬编码当场世界坐标。
2. **严禁绕过官方接口或越权感知**：只能通过 NMO 官方 REST API 感知与操作；禁止直连 MC 协议、逆向种子/读取存档、利用矿透等视野漏洞；`/chat` 禁止发送 `/` 开头的服务端指令（`Client.chat()` 已做防误触转义，勿删）。
   - 赛前联调网关 `tools/local-api-bridge/` 本身就是自建 MC 协议客户端，只能连队内测试服。**比赛当天 `MC_API_BASE` 必须切回官方端点，没有任何例外。**
3. **严禁接入非官方外部大模型**：Harness 赛道必须通过官方 AIA 统一代理 API 调用模型，不得改用外部大模型服务或绕开赛事额度/并发约束。
4. **严禁破坏基础设施或持续阻挠其他队伍**：允许正常资源竞争与自愿协作，但禁止利用游戏/服务器漏洞、禁止高频恶意请求攻击接口、禁止**持续性阻止其他 Agent 正常比赛**（具体 PvP 与破坏边界以赛前 NMO 公布为准）。

---

## 3. 登录是独占资源（最容易出事故的一条）

> 规范 §1.1：「同一角色重复登录会让旧 token 立即失效。」

两个 agent 用同一个皮肤站账号 = 互相踢下线。而且断线重连要 ≥30 秒，触发死亡循环还要冷却 5 分钟——
**你随手一次登录，可能让队友的 8 小时练习白废一段。**

所以：

1. 登录前先去 `docs/05-分工与契约.md` 的**账号表**把那一行改成 `使用中：<你的 id> · <HH:MM>`
2. 用完立刻改回 `空闲`，并让 bot 下线、关掉自己起的联调网关，别把 bot 挂在共享测试世界里
3. 看到别人写着"使用中"，**就等**，不要试着重登"看看能不能挤上去"
4. 尽量用 `--dry` 和 `unittest`（不需要账号）先验证你的代码路径
5. 同机重启默认自动复用 `code/mcagent/.token.json` 并单点刷新，调试时不要无故删除 token 文件或循环调 `login()`
6. 在联调网关上，**每人的 `MC_USER` 必须不同**；同名会按规范 §1.1 踢掉对方的 token，那是正确行为，不是 bug

---

## 4. 事实 / 判断 / 猜测，三个地方别混

| 你手上的东西 | 放哪里 | 硬要求 |
|---|---|---|
| 官方条款、接口语义 | `docs/00-赛事事实.md` | 附来源（链接 / § 号 / 截图） |
| 有依据的推论 | `docs/01-关键判断.md` | 必带"推翻条件" |
| 实测数字 | `docs/03` 或 `plan/` | 附命令 + 原始输出路径（`code/mcagent/probe/*.json`）；本地联调网关测得的一律标 `local-target`，**不得进时间预算表**（RTT 与官方端点差约 400 倍，见 `docs/03` 第五节） |
| 拿不准的 | `docs/04-待确认.md` | 写清"为什么重要"+"临时假设" |
| 能证明工程能力的决策过程 | `docs/06`（标 `评审价值：有`）+ 汇总到 `docs/07` | 让外行也读得懂 |

**不许**把"我猜的"写成陈述句。这个仓库里唯一比"没写"更糟的是"写错了但看起来很确定"。

---

## 5. 完成定义（DoD）

一个改动要算完成，下面几条必须全过：

- [ ] **在 `docs/06-工作日志.md` 追加了一条**（改了什么 / 为什么 / 验证到什么程度）
- [ ] 文档改动：附了来源；新判断写了推翻条件
- [ ] 代码改动：`cd code && uv run python -m compileall -q mcagent tests` 通过
- [ ] 代码改动：`cd code && uv run python -m unittest discover -s tests` 全绿（改底层逻辑须补对应单测）
- [ ] 代码改动：`cd code && uv run python -m mcagent.smoke --dry` 返回码 0
- [ ] 新增/改动技能或原语：配了用 `skills.testing.drive()` 驱动的离线单测（§9.4）
- [ ] 提交信息符合格式（见下），代码与文档尽量分提交以便独立 `git revert`
- [ ] `git status` 里没有多出来的密钥/`.token.json`/日志/`__pycache__`

跑真实的 bot 测试（`probe` / 真机技能验收）属于 `verified: live` 阶段，在占到账号时执行，不阻塞 `verified: offline` 的单测自检。

---

## 6. 分支、同步与提交

1. **先在自己的分支上提交，不直接在 `main` 上提交。** 开工先开分支（命名见下）；§5 的 DoD 全过后再合入 `main`。`verified: live` 的改动真机跑通前只留在分支（§2A.6）。
2. **先 commit，再同步。** 要拉远端时：先 `git status --short`，有改动就先在自己的分支上 commit（commit 是本地操作，不动远端）；再 `git fetch --dry-run` 看远端到底有没有新提交，确实有才 `git pull`。
   - 反过来（先 pull 再 commit）会卡在 `Your local changes would be overwritten` 上——这个报错是 git 在拒绝、一个字节都没动，**不是"需要强制覆盖"**。
3. **任何时候都不用** `git reset --hard`、`git pull --force`、`git checkout -- .`：只有这三条会真正丢掉本地改动。遇到同步报错就停下来报告，不要为了绕过它加 `--force`。
4. `.env`、`.token.json`、`probe/`、`logs/`、`tools/local-api-bridge/.env` 都在 `.gitignore` 里，pull 永远不碰它们，不必为了"保护配置"先删或先藏。

```
分支： <类型>/<模块>-<你的 id>      例：feat/planner-a1、docs/strategy-a1、fix/safety-a1
提交： <范围>: <做了什么>
       docs: 补充计分表深度值 [来源: 11/15 会议]
       code: probe 增加 craft count 验证
       fix: safety 在 death_loop 冷却期死循环
```

一次提交只做一件事。文档和代码混在一个提交里，别人没法只回退一半。

提交信息和 `docs/06` 不是一回事：提交信息说"改了什么"，日志说"**为什么改、验证到哪一步、还剩什么**"。两个都要。

---

## 7. 冲突时谁说了算

1. **事实冲突** → 以来源更新的为准；来源同级 → 以主办方原话为准；书面公告与群聊口头冲突 → 两边并列写清来源与状态，挂 `docs/04` 等正式口径，**不许自己删对方的条目**。
2. **判断冲突** → 两条都留着，各标 `@作者 日期`，等人类裁决。
3. **代码冲突** → 先合的人赢，后合的人负责 rebase + 重跑自检。

---

## 8. 会话结束时做四件事

1. 在 `docs/06-工作日志.md` 追加一条（**包括没做成的尝试**——"试过什么不行"比"做成了什么"更有用）
2. 把 `docs/05` 里你那一行的"当前状态"更新掉（不在做了就写"空闲"）
3. 没做完的事写进 `docs/04`，别留半成品在工作区
4. `git status` 干净后再离开——**下一个人可能就在同一个目录里开工**

---

## 9. Harness 框架规则：三层动作抽象

写 `skills/`、`planner/`、`probe*.py` 之前读这一节。它回答三个问题：**一段新代码该放哪一层、
谁能调它、怎样才算接好了**。背后的判断是 J8（模型尾延迟 30s+）、J11（确定性技能推进）、
J16（虚空防坠）、J17（模型会编坐标和实体 id），这里不重复论证。

### 9.1 三层，调用只能向下

```
模型 / 规则队列 ──选一个技能──▶ L3 意图技能 ──yield from──▶ L2 战术原语 ──ex.动作()──▶ L1 Executor ──▶ HTTP
  planner/skill_choice.py        skills/<领域>.py              skills/nav.py              runtime.py
```

| 层 | 放哪 | 谁能调 | 接口长什么样 | 例子 |
|---|---|---|---|---|
| **L3 意图技能** | `skills/<领域>.py`，类上加 `@register` | 模型、规则队列 | 只收语义参数（物品名、数量），**不收坐标** | `mine_ore(target, count)`（待写） |
| **L2 战术原语** | `skills/nav.py`，普通生成器函数 | 只有 L3 和 probe | 收当场观察到的方块坐标列表 | `breach_1x2` `sneak_bridge` `step_up_1x2` |
| **L1 原子执行** | `runtime.Executor` | 只有 L2 / L3 | 一次一个阻塞动作 | `ex.move_to` `ex.dig` `ex.place` |

**"用了 `@register`" 就等于"模型能选到"。** 所以 L2 永远不注册；想让模型用一个原语，就写一个 L3 技能包住它。

### 9.2 七条硬规则（括号里是谁在替你检查）

1. **每发一个阻塞动作就 `yield` 一次**。Safety 只能在动作之间抢占；一步里连发两个动作，中间掉血没人知道。
   （`skills.testing.drive()`：一步内发两个动作直接断言失败。`posture` 和所有观察不占动作锁，不算。）
2. **模型不碰坐标和实体 id**。技能参数只许 `str/int/float/bool`，名字不许是 `pos/x/y/z/entity_id`。
   坐标一律由技能自己用 `find_blocks` / `get_block` / `get_entities` 当场观察得到。（`@register` 时强制，违反则启动报错。）
3. **注册即接入，不维护名单**。技能菜单和模型输出校验都从 `skills.REGISTRY` 里各技能自己的声明读。
   新增技能 = 新增一个文件；不要在任何地方手写技能白名单。（`skills/__init__.py` 启动时自动导入包内所有文件。）
4. **调用只能向下，动作只走 `ex.<动作>()`**。`skills/` 里不出现 HTTP、不直接调 `client` 的动作方法（观察方法可以：
   `ex.client.get_block(...)`、`state.refresh_status(ex.client)`）；`planner/` 里不出现 `Executor`。（人工评审；§2A.3。）
5. **被切走的技能不续跑**。循环换技能时调 `skill.abort()`，下次要做就新建实例从观察重来——世界已经变了。
   需要收尾的状态（潜行、举盾）写在 `run()` 的 `finally` 里，而且**从进入该状态的那一行起就要在 `try` 里面**。
6. **原语先过 probe，再给技能用；两边导入同一份代码**。`probe_path.py` 直接 `from .skills import nav`。
   往 `nav.py` 加新原语时同步在 probe 里加一个 case；改了已有原语的动作顺序或常量（如 `PLACE_REACH`），
   必须重跑 `uv run python -m mcagent.probe --pathfinding --manual-only ...` 并把证据路径写进 `docs/06`。
7. **`verified` 如实标**。技能类默认 `verified = "offline"`；真机跑通、`docs/06` 留了日志路径之后才改成 `"live"`。
   赛中菜单默认只收 `"live"`（联调时传 `min_verified="offline"`）。这是 §2A.6 在代码里的落点，不是它的替代品：
   没上过真机的技能仍然不进 `main`。

### 9.3 一文件一职责

目标是每个文件 150–300 行、只有一个改它的理由——编码 agent 读写省上下文，几个人并行改也不撞。

```
code/mcagent/
  skills/base.py        Skill 基类、Param 声明、rec()、failure()
  skills/registry.py    @register、REGISTRY、make()、契约检查、自动发现
  skills/items.py       只读选择器：最好的镐、垫块、空气判断
  skills/nav.py         L2 原语（真机验过的三段手动兜底序列）
  skills/testing.py     离线夹具：GridWorld 替身 + drive()
  skills/<领域>.py       L3 技能：wood / stone_iron / survival / explore ……（待写，一个领域一个文件）
  planner/skill_choice.py  技能菜单、选择校验、choose()      ← 常规决策路径
  planner/decision.py      原子动作 Decision 校验            ← 只给没有技能覆盖的场景兜底
  planner/llm.py           LLM 客户端 + 通用重问循环 ask_json()
  probe.py              基线测量（T_status / T_move20 / T_dig1）+ 命令行入口
  probe_path.py         寻路与手动兜底探测（跑的是 nav.py 的原语）
  probe_fixture.py      寻路夹具模板与离线校验
```

已知超标、暂不拆的：`runtime.py`、`client.py`（冻结面，拆了会波及所有人）、`probe_path.py` 里的
`pathfinding_main()`（一个 360 行的函数，刚做完真机验收，等官方测试场复核完再拆）。
新代码不要以它们为榜样。

### 9.4 加一个 L3 技能

```python
# skills/stone_iron.py
from ..runtime import Replan
from . import nav
from .base import Param, Skill, rec
from .items import best_pickaxe
from .registry import register

@register
class MineOre(Skill):
    name = "mine_ore"                      # 模型输出里的 skill 字段
    description = "挖指定矿石并捡起掉落"        # 进菜单，≤60 字
    params = {
        "target": Param(str, "矿石名", choices=("coal_ore", "iron_ore")),
        "count": Param(int, "要几块", default=1, min=1, max=16),
    }
    goal = "acq:{target}"                  # 失败冷却按它计，可用参数名做模板

    @classmethod
    def blocked(cls, state) -> str:        # 与参数无关的前置检查；返回的原因会给模型看
        return "" if best_pickaxe(state) else "背包没有镐"

    def run(self, ex, state):
        found = ex.client.find_blocks(types=[self.args["target"]], radius=32, limit=4)   # 观察，不用 yield
        if not found.get("blocks"):
            raise Replan(f"半径 32 内没有裸露的 {self.args['target']}")                    # 这句话会回喂给模型
        pos = found["blocks"][0]["pos"]
        yield rec("move_to", ex.move_to(pos=pos, rng=3), pos=pos)                        # 一个动作，一个 yield
        yield from nav.breach_1x2(ex, state, [pos])                                      # 组合 L2 原语
```

上面是**写法示例，不是已验证的技能**（仓库里目前没有任何 L3 技能）。步骤：

1. 选对文件（按领域），照上面的形状写；先想清楚失败时抛什么 `Replan` 文案——那是模型下一轮唯一能看到的线索。
2. 在 `code/tests/` 里用 `GridWorld` + `drive()` 写离线单测：发了哪些动作、顺序对不对、被 `abort()` 后有没有收尾。
3. 跑 §5 的自检。此时是 `verified: offline`，留在分支上。
4. 占账号上真机跑通，把日志路径写进 `docs/06`，`verified` 改成 `"live"`，再合 `main`。

`GridWorld` 没有重力、挖掘时间和寻路失败，**过了离线单测不等于技能能用**。别往替身里加猜出来的服务端行为（§2A.2）。

### 9.5 模型看到什么、输出什么

常规路径（`planner.skill_choice.choose()`）：

- **输入**：主循环给的一屏状态摘要 + 现算的技能菜单（可启动的带签名，不可启动的带原因）+ 最近失败原因。
- **输出**：`{"skill": "mine_ore", "args": {"target": "iron_ore", "count": 3}, "why": "..."}`
- **校验**：技能名在菜单里、参数符合声明、`blocked()` 为空、该目标不在冷却中、`can_start()` 为真；
  任何一条不过，错误原文回喂重问一次，仍不过返回 `None`，由调用方走规则兜底，**不等待**（J8）。

兜底路径（`LLMClient.decide()` → `Decision`）：只在没有技能覆盖的场景让模型出**一个**原子动作。
这条路径上模型会接触坐标，所以执行前必须按 J17 二次核对。能写成技能的就不要走这条路。

### 9.6 现在有什么、没有什么（2026-11-14）

| 部件 | 状态 |
|---|---|
| 技能契约、注册表、菜单、选择校验、通用重问循环 | `verified: offline`（`code/tests/test_skills_framework.py`） |
| `nav.py` 三个原语 | 动作序列在本地 1.21.11 靶场跑通过（probe 旧实现）；改成生成器后离线逐步证据与旧实现一致，**真机尚未重跑** |
| `SKILL_SYSTEM_PROMPT` 的措辞 | **未在真模型上抽测** |
| 任何 L3 技能、主循环、状态摘要、`reach_pos`（由起终点自动算出要挖/要放哪些格） | **还没有** |

