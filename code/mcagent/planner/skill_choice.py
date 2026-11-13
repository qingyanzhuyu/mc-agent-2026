"""技能选择：给模型一张当前可启动技能的菜单，把它的回答校验成一个技能实例。

    {"skill": "mine_ore", "args": {"target": "iron_ore", "count": 3}, "why": "..."}

菜单和校验都只读 `skills.REGISTRY` 里各技能自己的声明，所以**新增一个技能文件就自动
出现在菜单里、自动被校验**，这里没有第二份名单要维护（与 `decision.py` 反射 `Client`
是同一个思路）。

模型在这条路径上不接触坐标和实体 id：技能参数注册时就被限制为标量（J17）。
本文件只出决策，不调 Executor。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from ..skills import REGISTRY, Skill, bind_args
from ..skills.base import VERIFIED_LEVELS

if TYPE_CHECKING:
    from ..runtime import State
    from .llm import LLMClient

# 措辞未经真机抽测（verified: offline 只覆盖菜单/校验/重问机制），上真机后按实测调整并在 docs/06 留记录
SKILL_SYSTEM_PROMPT = """你是 Minecraft 生存 Agent 的目标选择器。每次只输出一个 JSON 对象，不要输出任何其他文字、不要用 Markdown 代码块。

格式必须严格是：
{"skill": "...", "args": {}, "why": "..."}

要求（错一个就会被拒绝）：
- skill：只能从「可启动技能」清单里选，不能自创，也不能选「暂不可启动」里的
- args：对象，键和取值范围必须符合清单里该技能的签名；有默认值的参数可以不写
- why：一句话原因
- 你不需要也不能提供坐标或实体编号，找目标是技能自己的事
- 生存优先：血量低、饥饿、天黑、附近有敌对生物时先选处理危险的技能"""


def _usable(registry: dict, min_verified: str) -> dict[str, type[Skill]]:
    floor = VERIFIED_LEVELS.index(min_verified)
    return {n: c for n, c in sorted(registry.items()) if VERIFIED_LEVELS.index(c.verified) >= floor}


def menu(state: "State", registry: Optional[dict] = None, min_verified: str = "live") -> str:
    """渲染技能菜单。默认只收真机验证过的技能；联调时可传 `min_verified="offline"`。"""
    ready, held = [], []
    for name, cls in _usable(REGISTRY if registry is None else registry, min_verified).items():
        reason = cls.blocked(state)
        if reason:
            held.append(f"- {name}：{reason}")
        else:
            sig = ", ".join(p.describe(n) for n, p in cls.params.items())
            ready.append(f"- {name}({sig}) — {cls.description}")
    lines = ["可启动技能（只能从这里选）：", *(ready or ["- （无）"])]
    if held:
        lines += ["暂不可启动（原因）：", *held]
    return "\n".join(lines)


def _resolve(obj: Any, state: Optional["State"], registry: Optional[dict],
             min_verified: str) -> tuple[Optional[Skill], list[str]]:
    if not isinstance(obj, dict):
        return None, ["输出必须是 JSON object"]
    errs: list[str] = []
    if "why" in obj and not isinstance(obj["why"], str):
        errs.append("why 必须是字符串")
    usable = _usable(REGISTRY if registry is None else registry, min_verified)
    name = obj.get("skill")
    cls = usable.get(name) if isinstance(name, str) else None
    if cls is None:
        return None, errs + [f"skill 必须是以下之一: {', '.join(usable) or '（无）'}；收到 {name!r}"]
    bound, arg_errs = bind_args(cls, obj.get("args"))
    errs.extend(arg_errs)
    if errs:
        return None, errs
    if state is not None:
        reason = cls.blocked(state)
        if reason:
            return None, [f"{name} 现在不可启动：{reason}"]
    skill = cls(**bound)
    if skill.is_cooling():
        return None, [f"{skill.goal} 刚失败过，还在冷却，换一个目标"]
    if state is not None and not skill.can_start(state):
        return None, [f"{name} 在参数 {bound} 下不可启动，换参数或换技能"]
    return skill, []


def validate_choice(obj: Any, state: Optional["State"] = None, registry: Optional[dict] = None,
                    min_verified: str = "live") -> tuple[bool, list[str]]:
    """校验模型的技能选择。返回 (是否合法, 错误列表)；错误信息可直接回喂给模型。"""
    skill, errs = _resolve(obj, state, registry, min_verified)
    return skill is not None, errs


def from_choice(obj: Any, state: Optional["State"] = None, registry: Optional[dict] = None,
                min_verified: str = "live") -> Skill:
    """校验通过后返回绑定好参数的技能实例；不通过抛 ValueError。"""
    skill, errs = _resolve(obj, state, registry, min_verified)
    if skill is None:
        raise ValueError("技能选择不合法: " + " | ".join(errs))
    return skill


def choose(llm: "LLMClient", state_summary: str, state: "State", *, recent_failures: str = "",
           registry: Optional[dict] = None, min_verified: str = "live", attempts: int = 2,
           thinking: bool = False, total_timeout: Optional[float] = None,
           ) -> tuple[Optional[Skill], list[str], int]:
    """让模型从菜单里选一个技能。返回 (技能实例或 None, 最后一次的错误列表, 实际调用次数)。

    `state_summary` 与 `recent_failures` 由主循环准备（一屏以内）；菜单在这里现算。
    返回 None 时由调用方走规则兜底，不重试、不等待。
    """
    user = f"当前状态：\n{state_summary}\n\n{menu(state, registry, min_verified)}\n"
    if recent_failures:
        user += f"\n最近失败：\n{recent_failures}\n"
    obj, errors, used = llm.ask_json(
        SKILL_SYSTEM_PROMPT, user,
        lambda o: validate_choice(o, state, registry, min_verified),
        attempts=attempts, thinking=thinking, total_timeout=total_timeout)
    if obj is None:
        return None, errors, used
    return from_choice(obj, state, registry, min_verified), [], used
