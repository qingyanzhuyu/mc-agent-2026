"""技能注册表：`@register` 即接入，菜单与校验器都只读这里。

"注册了" == "模型能选到"。所以只有 L3 意图技能用 `@register`；L2 战术原语（`nav.py`）
是普通生成器函数，永远不进这张表。
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from typing import Any, Optional

from .base import FORBIDDEN_PARAMS, PARAM_TYPES, VERIFIED_LEVELS, Param, Skill

#: name -> Skill 子类
REGISTRY: dict[str, type[Skill]] = {}

_NAME = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
#: 这些模块不是技能文件，自动发现时跳过
_NOT_SKILLS = frozenset({"base", "registry", "testing"})
#: 菜单要一屏放得下，说明别写成段落
MAX_DESCRIPTION = 60


def contract_errors(cls: Any) -> list[str]:
    """检查一个类是否满足技能声明契约。返回错误列表（空 = 合格）。"""
    if not (isinstance(cls, type) and issubclass(cls, Skill)):
        return ["必须继承 skills.base.Skill"]
    errs: list[str] = []
    if not isinstance(cls.name, str) or not _NAME.match(cls.name) or cls.name == "skill":
        errs.append(f"name 必须是 3–32 位小写蛇形命名；收到 {cls.name!r}")
    desc = cls.description
    if not isinstance(desc, str) or not desc.strip() or len(desc) > MAX_DESCRIPTION:
        errs.append(f"description 必须是 1–{MAX_DESCRIPTION} 字的一句话")
    if cls.verified not in VERIFIED_LEVELS:
        errs.append(f"verified 必须是 {VERIFIED_LEVELS} 之一")
    params = cls.params
    if not isinstance(params, dict):
        return errs + ["params 必须是 dict[str, Param]"]
    for pname, p in params.items():
        if not isinstance(p, Param):
            errs.append(f"参数 {pname} 必须用 Param(...) 声明")
            continue
        if pname in FORBIDDEN_PARAMS:
            errs.append(f"参数 {pname} 不允许：坐标/实体 id 必须由技能自己观察得到，不能让模型填（J17）")
        if p.type not in PARAM_TYPES:
            errs.append(f"参数 {pname} 的类型只能是 str/int/float/bool")
            continue
        # 默认值允许是 None（"不指定，由技能自己挑"）；其余默认值和 choices 都得过自己的类型/范围检查
        has_default = not p.required and p.default is not None
        for v in (*p.choices, p.default) if has_default else p.choices:
            errs.extend(f"声明自相矛盾: {e}" for e in Param(p.type, min=p.min, max=p.max).check(pname, v))
        if has_default and p.choices and p.default not in p.choices:
            errs.append(f"参数 {pname} 的默认值不在 choices 里")
    try:
        cls.goal.format(**{n: "x" for n in params})
    except (KeyError, IndexError, ValueError):
        errs.append(f"goal 模板 {cls.goal!r} 用了没声明的参数名")
    if cls.step is Skill.step and not inspect.isgeneratorfunction(cls.run):
        errs.append("run() 必须是生成器（每个阻塞动作后 yield），或者自己覆盖 step()")
    return errs


def register(cls: type[Skill]) -> type[Skill]:
    """类装饰器。声明不合格或重名直接抛错——启动时炸，好过赛中模型选到半成品。"""
    errs = contract_errors(cls)
    if errs:
        raise TypeError(f"技能 {getattr(cls, '__name__', cls)} 不符合契约: " + " | ".join(errs))
    old = REGISTRY.get(cls.name)
    if old is not None and (old.__module__, old.__qualname__) != (cls.__module__, cls.__qualname__):
        raise ValueError(f"技能名 {cls.name!r} 重复: {old.__module__} 与 {cls.__module__}")
    REGISTRY[cls.name] = cls
    return cls


def get(name: str) -> type[Skill] | None:
    return REGISTRY.get(name)


def all_names() -> list[str]:
    return sorted(REGISTRY)


def make(name: Any, args: Optional[dict] = None, registry: Optional[dict] = None) -> Skill:
    """按名字和参数造一个技能实例；名字不存在或参数不合法抛 ValueError。"""
    reg = REGISTRY if registry is None else registry
    cls = reg.get(name) if isinstance(name, str) else None
    if cls is None:
        raise ValueError(f"没有名为 {name!r} 的技能；可选: {', '.join(sorted(reg)) or '（空）'}")
    return cls(**(args or {}))


def load_all() -> list[str]:
    """导入包内所有技能文件，触发各自的 `@register`。新增文件不需要改任何名单。"""
    package = importlib.import_module(__package__)
    loaded = []
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_") or info.name in _NOT_SKILLS:
            continue
        importlib.import_module(f"{__package__}.{info.name}")
        loaded.append(info.name)
    return loaded
