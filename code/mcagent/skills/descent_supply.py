"""下矿备货闸门（L2，不注册到 REGISTRY）。

## 为什么需要

实测事故：bot 在 y=41 地下作业，背包只有 63 圆石 + 3 木棍 + 1 木板，
既没有工作台也没有原木。镐子一断就再也造不出新镐（3x3 配方要工作台，
工作台要 4 木板，木板要原木），于是只能：

  · 徒手挖 stone/deepslate：7.5~12.5 秒/块，且**零掉落**；或
  · 被困在地下出不来。

根因不是"用完没收工作台"（回收机制工作正常），而是**下矿前没有备货**：
地表那 4 个工作台相距 50~64 格，地下根本够不到。

## 策略

进入地下作业类技能前，要求携带足够的"自给自足"物资：

  · 原木 >= DESCENT_LOG_MIN（用户要求大于 10 根）—— 有木头就能就地
    木板 → 工作台 → 木镐/石镐 全链路自救；
  · 至少一把镐；若只有一把且耐久过低，则要求先补备用镐。

不满足时 `blocked()` 返回具体缺口，主循环会先去 `gather_wood` 补货，
而不是让 bot 空手下去送。
"""
from __future__ import annotations

import os
from functools import wraps
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..runtime import State

LOG_BLOCKS = (
    "oak_log", "birch_log", "spruce_log", "jungle_log", "acacia_log",
    "dark_oak_log", "cherry_log", "mangrove_log", "pale_oak_log",
)
PLANK_BLOCKS = tuple(name.replace("_log", "_planks") for name in LOG_BLOCKS)
PICKAXES = (
    "netherite_pickaxe", "diamond_pickaxe", "iron_pickaxe",
    "stone_pickaxe", "wooden_pickaxe", "golden_pickaxe",
)

# 需要备货才允许进入的地下作业技能
GATED_SKILLS = ("mine_diamond", "smelt_iron", "mine_obsidian")

# 阈值从环境变量读，**默认 0 = 闸门关闭**。
# 为什么默认关：现有离线单测（167 个）里有大量"空背包时 blocked() 必须为空串"的断言，
# 硬闸门会直接推翻这些约定（实测 13 失败 + 39 报错）。所以做成可配置：
# 真机/比赛在 .env 里写 MC_DESCENT_LOG_MIN=10 开启，单测环境不设即为关闭、保持全绿。
#
# 注意：这里不 import 时就读环境变量，而是留 None 表示"运行时再解析"。
# 实测事故（2026-11-24 真机 300 步）：`loop/__main__.py` 先 `import registry`
# （触发本模块 import）再 `Config.load()`（才把 `.env` 灌进 os.environ），
# 所以 import 时读到的永远是 0 —— 闸门静默失效，bot 带着 2 根原木就下去挖铁了。
DESCENT_LOG_MIN: Optional[int] = None
DESCENT_PICK_MIN_DURABILITY: Optional[int] = None


def _env_int(name: str, default: int) -> int:
    """运行时读取整数环境变量；空值/非数字回落默认值。"""
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return int(default)


def log_min() -> int:
    """当前生效的原木阈值（None 时每次按 `MC_DESCENT_LOG_MIN` 现读，规避 import 顺序）。"""
    if DESCENT_LOG_MIN is not None:
        return int(DESCENT_LOG_MIN)
    return _env_int("MC_DESCENT_LOG_MIN", 0)


def pick_min_durability() -> int:
    """当前生效的镐子耐久阈值（None 时按 `MC_DESCENT_PICK_MIN_DURABILITY` 现读）。"""
    if DESCENT_PICK_MIN_DURABILITY is not None:
        return int(DESCENT_PICK_MIN_DURABILITY)
    return _env_int("MC_DESCENT_PICK_MIN_DURABILITY", 30)


def _count_any(state: "State", names) -> int:
    total = 0
    for name in names:
        try:
            total += int(state.count(name) or 0)
        except Exception:
            pass
    return total


def _best_pick_durability(state: "State"):
    """返回手上最好的镐的剩余耐久；无法判定时返回 None。"""
    try:
        inv = state.inventory or {}
    except Exception:
        return None
    known = None      # 已知的最大剩余耐久
    saw_unknown = False
    stacks = list(inv.get("items") or ())
    stacks.extend([inv.get("held"), inv.get("offhand")])
    for stack in stacks:
        if not isinstance(stack, dict):
            continue
        name = stack.get("name") or ""
        if not isinstance(name, str) or not name.endswith("_pickaxe"):
            continue
        durability = stack.get("durability")
        if isinstance(durability, (list, tuple)) and len(durability) == 2 and isinstance(durability[0], int):
            known = durability[0] if known is None else max(known, durability[0])
        else:
            saw_unknown = True
    # 只有"完全没有已知耐久"时才当作无限；不能用一个无耐久信息的堆栈
    # 盖掉手上那把已知快断的镐（否则会把"耐久 12"误判成"无限"）
    if known is not None:
        return known
    return 10 ** 6 if saw_unknown else None


def descent_supply_gap(state: "State") -> str:
    """返回下矿备货缺口；空串表示备货充足、可以出发。"""
    min_logs = log_min()
    if min_logs <= 0:
        return ""          # 闸门关闭（默认）：保持既有 blocked() 契约不变
    logs = _count_any(state, LOG_BLOCKS)
    if logs < min_logs:
        return (
            f"下矿备货不足：原木 {logs}/{min_logs} 根 —— 镐子断了就无法就地补镐"
            "（原木→木板→工作台→镐），只能徒手挖或困在地下；先去 gather_wood 补货"
        )
    picks = _count_any(state, PICKAXES)
    if picks < 1:
        return "下矿备货不足：没有镐子"
    if picks < 2:
        durability = _best_pick_durability(state)
        threshold = pick_min_durability()
        if durability is not None and durability <= threshold:
            return f"下矿备货不足：唯一镐子耐久仅 {durability}，且无备用镐"
    return ""


def install_descent_gates() -> list[str]:
    """把闸门包装到 GATED_SKILLS 的 `blocked()` 上（注册完成后调用一次）。"""
    from .registry import REGISTRY

    wrapped = []
    for name in GATED_SKILLS:
        cls = REGISTRY.get(name)
        if not isinstance(cls, type) or getattr(cls, "_descent_gated", False):
            continue
        original = cls.blocked.__func__

        @wraps(original)
        def gated_blocked(klass, state, _original=original):
            reason = _original(klass, state)
            if reason:
                return reason
            return descent_supply_gap(state)

        cls.blocked = classmethod(gated_blocked)
        cls._descent_gated = True
        wrapped.append(name)
    return wrapped
