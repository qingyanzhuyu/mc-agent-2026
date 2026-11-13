"""Skill 基类与参数声明。所有具体技能继承它，遵守"一次 step 一个动作"。

写技能只需要三样东西：类属性声明（name / description / params / goal）、可选的
`blocked()` 前置检查、一个生成器 `run()`。`run()` 里每发一个阻塞动作就 `yield` 一次，
基类的 `step()` 负责把它变成"一步一动作"的状态机。完整规则见仓库根 `AGENTS.md` §9。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, Optional

from ..client import ApiError

if TYPE_CHECKING:  # 只用于类型提示，避免循环导入
    from ..runtime import Executor, State

#: 必填参数的哨兵（不能用 None：None 可能是合法默认值）
REQUIRED: Any = object()
#: 技能参数只许标量。坐标 / 列表 / 对象一律不收——那是模型最爱编的东西（J17）
PARAM_TYPES: tuple[type, ...] = (str, int, float, bool)
#: 这些名字的参数注册时直接拒绝：世界坐标和实体 id 必须由技能自己观察得到
FORBIDDEN_PARAMS = frozenset({"pos", "x", "y", "z", "entity_id", "coords", "target_pos"})
#: 验证等级从低到高。`live` = 真机跑通并在 docs/06 留了日志路径
VERIFIED_LEVELS: tuple[str, ...] = ("offline", "live")


@dataclass(frozen=True)
class Param:
    """一个技能参数的声明。菜单渲染与模型输出校验都从这里读，别处不再写第二份。"""

    type: type
    doc: str = ""
    default: Any = REQUIRED
    choices: tuple = ()
    min: Optional[float] = None
    max: Optional[float] = None

    @property
    def required(self) -> bool:
        return self.default is REQUIRED

    def check(self, name: str, value: Any) -> list[str]:
        """返回错误列表；措辞要能原样回喂给模型。"""
        t = self.type
        if t is bool:
            ok = isinstance(value, bool)
        elif t is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif t is float:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        else:
            ok = isinstance(value, str) and bool(value.strip())
        if not ok:
            return [f"{name} 必须是 {t.__name__}；收到 {value!r}"]
        if self.choices and value not in self.choices:
            return [f"{name} 必须是以下之一: {', '.join(map(str, self.choices))}；收到 {value!r}"]
        if self.min is not None and value < self.min:
            return [f"{name} 不能小于 {self.min}；收到 {value!r}"]
        if self.max is not None and value > self.max:
            return [f"{name} 不能大于 {self.max}；收到 {value!r}"]
        return []

    def describe(self, name: str) -> str:
        """菜单里的一小段签名，如 `count: int 1..16 = 1`。"""
        if self.choices:
            kind = "|".join(map(str, self.choices))
        elif self.min is not None or self.max is not None:
            lo = "" if self.min is None else f"{self.min:g}"
            hi = "" if self.max is None else f"{self.max:g}"
            kind = f"{self.type.__name__} {lo}..{hi}"
        else:
            kind = self.type.__name__
        return f"{name}: {kind}" + ("" if self.required else f" = {self.default!r}")


def bind_args(cls: "type[Skill]", args: Any) -> tuple[dict, list[str]]:
    """按 `cls.params` 校验并补默认值。返回 (绑定后的参数, 错误列表)。"""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {}, ["args 必须是 JSON object"]
    errs: list[str] = []
    unknown = sorted(set(args) - set(cls.params))
    if unknown:
        errs.append(f"{cls.name} 不认识这些参数: {unknown}；可用: {sorted(cls.params)}")
    bound: dict = {}
    for name, p in cls.params.items():
        if name in args:
            errs.extend(p.check(name, args[name]))
            bound[name] = args[name]
        elif p.required:
            errs.append(f"{cls.name} 缺少必填参数: {name}")
        else:
            bound[name] = p.default
    return bound, errs


def rec(op: str, result: Any = None, **fields: Any) -> dict:
    """一步的记录：`yield rec("dig", ex.dig(pos), pos=pos)`。probe 报告和失败摘要都读它。"""
    return {"op": op, **fields, "result": result}


def failure(e: BaseException) -> tuple[str, dict]:
    """从 Replan/ApiError 链上取回服务端错误码与附加信息，用来生成"最近失败原因"。"""
    if isinstance(e, ApiError):
        return e.code, dict(e.extra)
    cause = e.__cause__
    if isinstance(cause, ApiError):
        return cause.code, dict(cause.extra)
    return type(e).__name__.lower(), {}


class Skill:
    #: 唯一标识，用于注册、日志和模型输出的 `skill` 字段
    name: str = "skill"
    #: 目标 id，形如 "acq:oak_log"；可用参数名做模板，如 "acq:{target}"
    goal: str = "goal:unnamed"
    #: 同一个目标失败后，多久内不再重试（秒）
    cooldown_s: float = 30.0
    #: 给模型看的一句话说明（进菜单，越短越好）
    description: str = ""
    #: 参数声明：名字 -> Param。只许标量，不许坐标/实体 id
    params: dict[str, Param] = {}
    #: 验证等级；真机跑通后由作者改成 "live"，赛中菜单默认只收 "live"
    verified: str = "offline"

    def __init__(self, **args: Any):
        bound, errs = bind_args(type(self), args)
        if errs:
            raise ValueError(f"{self.name} 参数不合法: " + " | ".join(errs))
        self.args: dict = bound
        self.goal = type(self).goal.format(**bound)
        #: 最近一步的记录（`run()` 里 yield 出来的东西）
        self.last: Optional[dict] = None
        self._flow: Optional[Iterator] = None

    # ---- 能不能开始
    @classmethod
    def blocked(cls, state: "State") -> str:
        """与参数无关的前置检查。返回空串 = 可以进菜单；否则返回一句原因（会给模型看）。"""
        return ""

    def can_start(self, state: "State") -> bool:
        """绑定参数后的最终检查。默认只看 `blocked()`；需要看参数时覆盖它。"""
        return not self.blocked(state)

    # ---- 怎么做
    def run(self, ex: "Executor", state: "State") -> Iterator[dict]:
        """生成器：**每发一个阻塞动作后 yield 一次**；正常 return = 目标完成。

        前置条件不成立抛 `runtime.Replan`。需要收尾的（如松开潜行）写在 `finally` 里，
        被抢占时 `abort()` 会触发它。
        """
        raise NotImplementedError

    def step(self, ex: "Executor", state: "State") -> bool:
        """推进一小步。**最多执行一个动作**。返回 True 表示目标完成。"""
        flow = getattr(self, "_flow", None)
        if flow is None:
            flow = self._flow = self.run(ex, state)
        try:
            self.last = next(flow)
        except StopIteration:
            self._flow = None
            return True
        except BaseException:
            self._flow = None
            raise
        return False

    def abort(self) -> None:
        """循环切走这个技能时调用：结束流程并执行 `run()` 里的 `finally` 收尾。

        被切走的技能不续跑——下次要做就新建实例、从观察重来，因为世界已经变了。
        """
        flow, self._flow = getattr(self, "_flow", None), None
        if flow is not None:
            flow.close()

    # ---- 失败冷却（循环用它避免反复撞同一堵墙）
    def _fail_key(self) -> str:
        return self.goal

    def is_cooling(self) -> bool:
        until = _COOLDOWN.get(self._fail_key(), 0.0)
        return time.time() < until

    def mark_failed(self) -> None:
        _COOLDOWN[self._fail_key()] = time.time() + self.cooldown_s

    def __str__(self) -> str:
        return f"<{self.name}:{self.goal}>"


_COOLDOWN: dict[str, float] = {}
