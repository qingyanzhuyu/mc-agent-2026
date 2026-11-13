"""planner —— 决策层（只放契约，不放未验证的实现）。

契约：
    Decision(goal, action, args, why, expect, abort_if)
    validate(obj) -> (ok, errors)
    from_dict(obj) -> Decision
    bind(client, decision) -> Callable[[], dict]

`action` 只能是 `client.Client` 上真实存在的动作方法，`args` 的键必须是该方法的参数名。
校验器用 `inspect.signature` 反射客户端，**客户端加方法后自动跟上**，不需要维护两份名单。

两条决策路径：
    常规   skill_choice.menu/validate_choice/choose —— 模型从技能菜单里选一个，不接触坐标
    兜底   decision.Decision/validate —— 没有技能覆盖的场景才让模型出单个原子动作

本包现在有：两个校验器（纯函数）+ LLM 客户端（带离线单测与真机抽测）。
策略选择仍待写——见 `AGENTS.md` §2A.6 分级准入。
"""
from .decision import ALLOWED_ACTIONS, Decision, bind, from_dict, validate
from .llm import LLMClient, LLMError, extract_json
from .skill_choice import choose, from_choice, menu, validate_choice

__all__ = ["ALLOWED_ACTIONS", "Decision", "bind", "from_dict", "validate",
           "LLMClient", "LLMError", "extract_json",
           "menu", "validate_choice", "from_choice", "choose"]
