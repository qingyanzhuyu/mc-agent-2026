"""planner —— 决策层（只放契约，不放未验证的实现）。

契约：
    Decision(goal, action, args, why, expect, abort_if)
    validate(obj) -> (ok, errors)
    from_dict(obj) -> Decision
    bind(client, decision) -> Callable[[], dict]

`action` 只能是 `client.Client` 上真实存在的动作方法，`args` 的键必须是该方法的参数名。
校验器用 `inspect.signature` 反射客户端，**客户端加方法后自动跟上**，不需要维护两份名单。

本包当前**只有校验器**（纯函数，9 个用例验过）。LLM 接入、策略选择等实现，
跑通之后再进仓库——见 `AGENTS.md` §2 第 6 条。
"""
from .decision import ALLOWED_ACTIONS, Decision, bind, from_dict, validate

__all__ = ["ALLOWED_ACTIONS", "Decision", "bind", "from_dict", "validate"]
