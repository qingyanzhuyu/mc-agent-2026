"""skills —— 执行层：L3 意图技能 + L2 战术原语。完整规则见仓库根 `AGENTS.md` §9。

    base.py       Skill 基类 / Param 参数声明 / rec() / failure()
    registry.py   @register、REGISTRY、make()、自动发现
    items.py      选择器：最好的镐、垫块、空气判断（只读）
    nav.py        L2 原语：breach_1x2 / sneak_bridge / step_up_1x2（probe 直接导入验收）
    testing.py    离线夹具：GridWorld 替身 + drive()（强制"一步一动作"）
    <领域>.py      L3 技能，一个文件一个领域，`@register` 即接入

硬约束（架构性的，不是风格问题）：

    每发一个阻塞动作就 yield 一次。

原因：Safety 闸只能在**动作之间**抢占。一步里连做 20 个动作，中间血量掉到 2 也不会有人
知道——而 60 秒死 3 次就会下线 5 分钟。

写新 skill：

    from .base import Param, Skill, rec
    from .registry import register

    @register
    class GetStone(Skill):
        name = "get_stone"
        description = "挖圆石并捡起"
        params = {"count": Param(int, "要几块", default=3, min=1, max=64)}
        goal = "acq:cobblestone"

        @classmethod
        def blocked(cls, state) -> str:
            return "" if best_pickaxe(state) else "背包没有镐"

        def run(self, ex, state):
            ...                                   # 观察随便调，不用 yield
            yield rec("dig", ex.dig(pos), pos=pos)  # 每个阻塞动作后 yield

规则：
- 观察（`ex.client.find_blocks/get_block`、`state.refresh_*`）不占动作锁，可以随便调。
- 动作只走 `ex.<动作>()`；前置条件不成立抛 `runtime.Replan`，循环换下一个技能。
- 参数只许标量，不许坐标/实体 id（注册时强制）。
- **没在真机跑通的技能不要合进 main**（`AGENTS.md` §2A.6）；跑通后把 `verified` 改成 "live"。
"""
from .base import Param, Skill, bind_args, failure, rec
from .registry import REGISTRY, all_names, contract_errors, get, load_all, make, register

# 导入包内所有技能文件，触发各自的 @register。坏文件在这里就炸——启动时发现好过赛中才发现。
load_all()

__all__ = ["Skill", "Param", "REGISTRY", "register", "get", "all_names", "make",
           "contract_errors", "bind_args", "load_all", "rec", "failure"]
