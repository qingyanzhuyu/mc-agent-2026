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
import math
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
        if "pos" in args and args["pos"] is not None:
            errs.extend(_check_pos(args["pos"]))
        errs.extend(_cross_rules(action, args))

    # 软约束：不合法就报错，但不阻止执行（方便人手工构造 Decision）
    if "why" in obj and not isinstance(obj["why"], str):
        errs.append("why 必须是字符串")
    ab = obj.get("abort_if", [])
    if not isinstance(ab, list):
        errs.append("abort_if 必须是数组")

    return (len(errs) == 0), errs


_VALID_EQUIP_SLOTS = frozenset(
    {"hand", "off-hand", "offhand", "head", "torso", "chest", "legs", "feet"}
)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _cross_rules(action: str, args: dict) -> list[str]:
    """跨参数与值类型约束：防止 None/字符串/错误槽位透传到服务端触发 bad_args。"""
    errs: list[str] = []
    if action == "move_to":
        has_xz = "x" in args and "z" in args and args["x"] is not None and args["z"] is not None
        has_pos = "pos" in args and args["pos"] is not None
        has_ent = "entity_id" in args and args["entity_id"] is not None
        if not (has_xz or has_pos or has_ent):
            errs.append("move_to 需要有效的 (x, z)、pos 或 entity_id")
        if has_xz and not (_is_num(args["x"]) and _is_num(args["z"])):
            errs.append("move_to 的 x, z 必须是有限数字")
        if args.get("y") is not None and not _is_num(args["y"]):
            errs.append("move_to 的 y 必须是有限数字")
        if has_ent and not _is_int(args["entity_id"]):
            errs.append("move_to 的 entity_id 必须是整数")
    elif action == "shoot":
        has_ent = "entity_id" in args and args["entity_id"] is not None
        has_pos = "pos" in args and args["pos"] is not None
        if not (has_ent or has_pos):
            errs.append("shoot 需要有效的 entity_id 或 pos")
        if has_ent and not _is_int(args["entity_id"]):
            errs.append("shoot 的 entity_id 必须是整数")
    elif action == "interact":
        has_pos = "pos" in args and args["pos"] is not None
        has_ent = "entity_id" in args and args["entity_id"] is not None
        if not (has_pos or has_ent):
            errs.append("interact 需要有效的 pos 或 entity_id")
        if has_ent and not _is_int(args["entity_id"]):
            errs.append("interact 的 entity_id 必须是整数")
    elif action == "dig":
        if args.get("pos") is None:
            errs.append("dig 的 pos 不能为空")
    elif action == "attack":
        if not _is_int(args.get("entity_id")):
            errs.append("attack 的 entity_id 必须是整数")
    elif action in ("craft", "drop", "place", "use_item", "equip"):
        item = args.get("item")
        if not isinstance(item, str) or not item.strip():
            errs.append(f"{action} 的 item 必须是非空字符串")
        if action in ("craft", "drop") and "count" in args:
            if not _is_int(args["count"]) or args["count"] < 1:
                errs.append(f"{action} 的 count 必须是 >=1 的整数")
        if action == "equip" and "slot" in args and args["slot"] not in _VALID_EQUIP_SLOTS:
            errs.append(f"equip 的 slot 不合法: {args['slot']!r}")
    elif action == "chat":
        msg = args.get("message")
        if not isinstance(msg, str) or not msg.strip():
            errs.append("chat 的 message 必须是非空字符串")
    elif action == "posture":
        for k in ("sneak", "shield"):
            if k in args and args[k] is not None and not isinstance(args[k], bool):
                errs.append(f"posture 的 {k} 必须是布尔值")
    elif action in ("window_view", "window_act"):
        if not isinstance(args.get("target"), dict) or not args["target"]:
            errs.append(f"{action} 的 target 必须是非空对象")
        if action == "window_act":
            ops = args.get("ops")
            if not isinstance(ops, list) or not ops:
                errs.append("window_act 的 ops 必须是非空数组")
    return errs


def _check_pos(pos: Any) -> list[str]:
    if not isinstance(pos, (list, tuple)) or len(pos) != 3:
        return [f"pos 必须是 [x, y, z]；收到 {pos!r}"]
    if not all(_is_num(v) for v in pos):
        return [f"pos 的三个分量必须是有限数字；收到 {pos!r}"]
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
