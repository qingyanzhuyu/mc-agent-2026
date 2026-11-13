"""寻路 probe 的夹具：case 名单、空模板、离线校验。不联网、不登录。

夹具里的坐标只能来自赛前已获准测试场的 API 观察；这是**赛前测试工具**，
不能用于赛中人工探图后硬编码当场坐标（AGENTS §2B）。
"""
from __future__ import annotations

import math
import os

#: probe 原始证据目录（已 gitignore）
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe")

# 先测无损的 move20 / dig1（采集 C1），再测裸 move_to 障碍能力、手动组合兜底动作序列，最后测会摔伤的 pearl
PATH_CASES = (
    "move20", "dig1", "wall", "gap", "pillar", "climb_y", "climb_xz", "water",
    "manual_breach", "manual_bridge", "manual_step_up", "pearl",
)
MANUAL_CASES = ("climb_xz", "manual_breach", "manual_bridge", "manual_step_up")
#: 走 skills/nav.py 原语的三项手动兜底序列
SEQUENCE_CASES = ("manual_breach", "manual_bridge", "manual_step_up")


def path_template() -> dict:
    """坐标来自赛前测试场 API 观察，模板刻意不预置世界坐标。"""
    return {
        "purpose": "precompetition-pathfinding-only",
        "notes": "只用于赛前已获准的测试场；每个 case 的 start/target 必须来自当前 API 观察。",
        "cases": {name: {
            "start": None, "target": None, "witness_blocks": [],
            "description": {
                "wall": "石墙两侧，witness_blocks 列出挡路的 stone；记录裸 move_to 是否绕行或挖开",
                "gap": "有安全底板的坑两侧，witness_blocks 列出待填的空气格；测裸 move_to 能否跨坑/垫块",
                "pillar": "同 x/z 高处目标，witness_blocks 列出待搭柱空气格；测裸 move_to 能否原地搭柱",
                "climb_y": "现成阶梯/坡道顶部，传 pos=[x,y,z] 带 y 爬高；与 pillar 分开测",
                "climb_xz": "现成阶梯/高低差目标，仅传 (x,z) 省略 y，对比带 y 寻路行为差异",
                "water": "浅水池内起点到池外安全岸边；witness_blocks 含水格",
                "manual_breach": "手动装备镐 + 按序 dig witness_blocks 开 1x2 通道 + move_to 穿墙兜底",
                "manual_bridge": "手动 posture(sneak=True) + 按序向悬空侧面 place witness_blocks 搭桥 + move_to 过坑",
                "manual_step_up": "先测自身脚底碰撞箱 place 是否被拒，再按 witness_blocks 执行 1x2 交替垫块上高处",
                "pearl": "可见的宽大安全落地区，目标为瞄准点；包内有 ender_pearl",
                "move20": "平坦已观察通路，水平距离严格 20 格",
                "dig1": "可达的单块 stone，脚下及承重块除外；记录工具",
            }[name],
        } for name in PATH_CASES},
    }


def validate_fixture(fixture: dict, selected: list[str]) -> None:
    if fixture.get("purpose") != "precompetition-pathfinding-only":
        raise ValueError("fixture 必须标记 purpose=precompetition-pathfinding-only")
    def pos_ok(pos):
        return (isinstance(pos, list) and len(pos) == 3
                and all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in pos))
    for name in selected:
        case = fixture.get("cases", {}).get(name, {})
        if not all(pos_ok(case.get(k)) for k in ("start", "target")):
            raise ValueError(f"{name}: 先用官方 API 观察并填入 start/target [x,y,z]")
        rng = case.get("range")
        if rng is not None and (not isinstance(rng, (int, float)) or isinstance(rng, bool) or not math.isfinite(rng) or rng <= 0):
            raise ValueError(f"{name}: range 必须为正有限数")
        witnesses = case.get("witness_blocks")
        if not isinstance(witnesses, list) or not all(pos_ok(v) for v in witnesses):
            raise ValueError(f"{name}: witness_blocks 必须为坐标数组")
        if name in ("wall", "gap", "pillar", "water", "manual_breach", "manual_bridge", "manual_step_up") and not witnesses:
            raise ValueError(f"{name}: 必须提供要对比的 witness_blocks")
        start, target = case["start"], case["target"]
        if name == "move20" and not math.isclose(math.hypot(target[0]-start[0], target[2]-start[2]), 20, abs_tol=0.01):
            raise ValueError("move20 起点到目标的水平距离必须为 20 格")
        if name in ("pillar", "climb_y", "manual_step_up") and target[1] <= start[1]:
            raise ValueError(f"{name}: 目标必须高于起点")
        if name == "pillar" and start[::2] != target[::2]:
            raise ValueError("pillar 必须同 x/z，避免把爬坡当成搭柱")
