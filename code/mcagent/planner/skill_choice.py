"""技能选择：给模型一张当前可启动技能的菜单，把它的回答校验成一个技能实例。

    {"skill": "mine_ore", "args": {"target": "iron_ore", "count": 3}, "why": "..."}

菜单和校验都只读 `skills.REGISTRY` 里各技能自己的声明，所以**新增一个技能文件就自动
出现在菜单里、自动被校验**，这里没有第二份名单要维护（与 `decision.py` 反射 `Client`
是同一个思路）。

模型在这条路径上不接触坐标和实体 id：技能参数注册时就被限制为标量（J17）。
本文件只出决策，不调 Executor。
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Optional

from ..skills import REGISTRY, Skill, bind_args
from ..skills.base import VERIFIED_LEVELS, _COOLDOWN

if TYPE_CHECKING:
    from ..runtime import State
    from .llm import LLMClient

# 经 local-target 真模型（Qwen3.6-Flash）20 步压测抽测调优（见 docs/06）
SKILL_SYSTEM_PROMPT = """你是 Minecraft 生存 Agent 的目标选择器。每次只输出一个单行 JSON 对象，不要输出任何其他文字、不要用 Markdown 代码块。

格式必须严格是：
{"skill": "...", "args": {}, "why": "..."}

要求（错一个就会被拒绝）：
- skill：只能从「可启动技能」清单里选，不能自创，也不能选「暂不可启动」里的
- args：对象，键和取值范围必须符合清单里该技能的签名；有默认值的参数可以不写
- why：一句话简短原因（20 字以内）
- 你不需要也不能提供坐标或实体编号，找目标是技能自己的事
- 生存优先：血量低、饥饿、天黑、附近有敌对生物时先选处理危险的技能
- 避开刚失败的目标：参考「最近失败」，不要原样重复刚失败的同一技能与参数；若某目标因缺原料、无裸露方块或路径不通失败，优先换前置采集/探索技能或换参数
- 科技推进顺序：空手先采木 -> 合成木镐 -> 已有木镐后选 mine_stone 挖圆石（勿重复合成已拥有的同级工具）-> 圆石够后再升级石镐/石剑/熔炉；选合成类技能前先核对背包原料是否够配方"""


def _usable(registry: dict, min_verified: str) -> dict[str, type[Skill]]:
    floor = VERIFIED_LEVELS.index(min_verified)
    return {n: c for n, c in sorted(registry.items()) if VERIFIED_LEVELS.index(c.verified) >= floor}


def _fixed_goal_cooling(cls: type[Skill]) -> str:
    """固定 goal（不含 `{参数}` 模板）若在失败冷却期，任何参数都无法通过 `_resolve`，须直接在菜单标明。"""
    goal_tpl = getattr(cls, "goal", "")
    if not goal_tpl or "{" in goal_tpl:
        return ""
    rem = _COOLDOWN.get(goal_tpl, 0.0) - time.time()
    if rem > 0:
        return f"目标 {goal_tpl} 刚失败冷却中（剩 {rem:.0f}s）"
    return ""


def has_ready_skill(state: "State", registry: Optional[dict] = None, min_verified: str = "live") -> bool:
    """当前是否存在至少一个未受阻且未整类冷却的可选技能（避免空菜单白烧 LLM token）。"""
    for cls in _usable(REGISTRY if registry is None else registry, min_verified).values():
        if not cls.blocked(state) and not _fixed_goal_cooling(cls):
            return True
    return False


def menu(state: "State", registry: Optional[dict] = None, min_verified: str = "live") -> str:
    """渲染技能菜单。默认只收真机验证过的技能；联调时可传 `min_verified="offline"`。"""
    ready, held = [], []
    for name, cls in _usable(REGISTRY if registry is None else registry, min_verified).items():
        reason = cls.blocked(state) or _fixed_goal_cooling(cls)
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
           usage_out: Optional[dict[str, int]] = None,
           ) -> tuple[Optional[Skill], list[str], int]:
    """让模型从菜单里选一个技能。返回 (技能实例或 None, 最后一次的错误列表, 实际调用次数)。

    `state_summary` 与 `recent_failures` 由主循环准备（一屏以内）；菜单在这里现算。
    返回 None 时由调用方走规则兜底，不重试、不等待。
    若传入 `usage_out`，会把本次决策（含多次重问）消耗的 token 累加写入其中。
    """
    user = f"当前状态：\n{state_summary}\n\n{menu(state, registry, min_verified)}\n"
    if recent_failures:
        user += f"\n最近失败：\n{recent_failures}\n"
    obj, errors, used = llm.ask_json(
        SKILL_SYSTEM_PROMPT, user,
        lambda o: validate_choice(o, state, registry, min_verified),
        attempts=attempts, thinking=thinking, total_timeout=total_timeout,
        usage_out=usage_out)
    if obj is None:
        return None, errors, used
    return from_choice(obj, state, registry, min_verified), [], used
