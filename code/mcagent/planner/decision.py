"""决策层：把"下一步做什么"变成一个可校验的 JSON。

契约（别改，别人依赖）：
    Decision(goal, action, args, why, expect, abort_if)
    validate(obj) -> (ok, errors)
    bind(client, decision) -> Callable[[], dict]   # 交给 Executor 执行

`action` 只能是 `client.Client` 上真实存在的动作方法，`args` 的键必须是该方法的参数名。
校验器直接用 `inspect.signature` 反射客户端，所以**客户端加了新方法，这里自动跟上**，
不用维护两份名单——这是防止模型"发明接口"的唯一可靠办法。
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from ..client import Client

#: 允许模型调用的动作方法名（观察类不在此列：观察由循环自己控制节奏）
ALLOWED_ACTIONS: tuple[str, ...] = (
    "move_to", "dig", "place", "interact", "use_item", "attack", "shoot",
    "craft", "window_view", "window_act", "equip", "posture", "dismount",
    "drop", "chat", "stop",
)

_SIGS: dict[str, inspect.Signature] = {
    n: inspect.signature(getattr(Client, n)) for n in ALLOWED_ACTIONS
}


@dataclass
class Decision:
    goal: str                      # 目标 id，冒号分段，如 "acq:oak_log" / "tool:stone_pickaxe"
    action: str                    # ALLOWED_ACTIONS 之一
    args: dict = field(default_factory=dict)
    why: str = ""                  # 一句话原因，进日志，人工复盘就看这个
    expect: dict | None = None     # 预期结果，如 {"item": "cobblestone", "count": 3}
    abort_if: list[str] = field(default_factory=list)  # 中止条件 token，如 ["hp<10"]

    def __str__(self) -> str:
        return f"{self.action}({self.args}) <- {self.why}"


def validate(obj: Any) -> tuple[bool, list[str]]:
    """校验一个候选决策。返回 (是否合法, 错误列表)。错误信息要能直接回喂给模型。"""
    errs: list[str] = []
    if not isinstance(obj, dict):
        return False, ["决策必须是 JSON object"]

    goal = obj.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        errs.append("goal 必须是非空字符串")

    action = obj.get("action")
    if action not in ALLOWED_ACTIONS:
        errs.append(f"action 必须是以下之一: {', '.join(ALLOWED_ACTIONS)}；收到 {action!r}")

    args = obj.get("args", {})
    if not isinstance(args, dict):
        errs.append("args 必须是 JSON object")
    elif action in ALLOWED_ACTIONS:
        # 反射签名：参数名必须真实存在，且必填项不能缺
        sig = _SIGS[action]
        valid_names = set(sig.parameters) - {"self", "timeout"}
        bad = set(args) - valid_names
        if bad:
            errs.append(f"{action} 不认识这些参数: {sorted(bad)}；可用: {sorted(valid_names)}")
        missing = [
            n for n, p in sig.parameters.items()
            if n not in ("self", "timeout") and p.default is inspect.Parameter.empty and n not in args
        ]
        if missing:
            errs.append(f"{action} 缺少必填参数: {missing}")
        if "pos" in args:
            errs.extend(_check_pos(args["pos"]))
        errs.extend(_cross_rules(action, args))

    # 软约束：不合法就报错，但不阻止执行（方便人手工构造 Decision）
    if "why" in obj and not isinstance(obj["why"], str):
        errs.append("why 必须是字符串")
    ab = obj.get("abort_if", [])
    if not isinstance(ab, list):
        errs.append("abort_if 必须是数组")

    return (len(errs) == 0), errs


def _cross_rules(action: str, args: dict) -> list[str]:
    """跨参数的语义约束：签名全是默认值的方法（move_to/shoot/interact）靠签名查不出来。"""
    if action == "move_to":
        if not (("x" in args and "z" in args) or "entity_id" in args):
            return ["move_to 需要 (x, z) 或 entity_id"]
    elif action == "shoot":
        if "entity_id" not in args and "pos" not in args:
            return ["shoot 需要 entity_id 或 pos"]
    elif action == "interact":
        if "pos" not in args and "entity_id" not in args:
            return ["interact 需要 pos 或 entity_id"]
    return []


def _check_pos(pos: Any) -> list[str]:
    if not isinstance(pos, (list, tuple)) or len(pos) != 3:
        return [f"pos 必须是 [x, y, z]；收到 {pos!r}"]
    if not all(isinstance(v, (int, float)) for v in pos):
        return [f"pos 的三个分量必须是数字；收到 {pos!r}"]
    return []


def from_dict(obj: Any) -> Decision:
    """校验通过后转成 Decision；不通过抛 ValueError（错误列表拼在消息里）。"""
    ok, errs = validate(obj)
    if not ok:
        raise ValueError("决策不合法: " + " | ".join(errs))
    return Decision(
        goal=obj["goal"],
        action=obj["action"],
        args=dict(obj.get("args") or {}),
        why=str(obj.get("why", "")),
        expect=obj.get("expect"),
        abort_if=list(obj.get("abort_if") or []),
    )


def bind(client: Client, d: Decision) -> Callable[[], dict]:
    """把 Decision 绑成一个零参可调用对象，交给 Executor.act 执行。

    注意：必须走 Executor，不要直接调 client——否则没有日志、没有错误分类，
    而且绕开了单飞锁（动作互斥是接口的硬约束）。
    """
    fn = getattr(client, d.action)
    return lambda: fn(**d.args)
