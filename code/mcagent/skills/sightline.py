"""玩家眼高、方块中心/碰撞箱眼距与 3D 体素视线遮挡（LOS）公共基础模块。

规范依据（`docs/99-原始记录/api-spec-v1.3.11.md`）：
- `/status` 返回的 `pos` 是玩家脚底坐标 `bot.entity.position`，站立眼高 `+1.62`、潜行眼高 `+1.27`。
- `/find_blocks` 与 `/get_block` 的 `pos` 是整数格角点 `[bx, by, bz]`，几何中心为 `[bx+0.5, by+0.5, bz+0.5]`。
- §4.2 / §8 1.3.10 ①：能否原地挖掘/开窗由**眼睛到方块中心（或暴露面）的距离与视线无遮挡**决定，
  直接拿脚底算整数角点会产生最高约 1.62 格高差误判与 ±0.87 格正负轴方向不对称偏差。

所有 L2 原语与 L3 技能在判断挖掘、工作站交互、瞄准与近场可达时一律复用本模块，不发任何阻塞动作。
"""
from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from itertools import product
from typing import TYPE_CHECKING, Any

from ..client import ApiError
from ..runtime import Replan
from .items import AIR, bare

if TYPE_CHECKING:
    from ..runtime import Executor

#: 原版 1.21 玩家站立眼高（相对 `/status` 脚底 `pos[1]`）
EYE_HEIGHT_STANDING: float = 1.62
#: 原版 1.21 玩家潜行眼高（`status.sneaking == True`）
EYE_HEIGHT_SNEAKING: float = 1.27

#: 稳定原地挖掘眼距上限（较服务端 §4.2 的 5.0 格内缩 0.5 格防双端位置轻微不同步报 dig_unconfirmed）
DIG_EYE_REACH: float = 4.5
#: 规范 §4.2 服务端判定“已在眼睛 5 格内、不先自动寻路”的硬阈值
API_DIG_EYE_REACH: float = 5.0
#: 工作台、熔炉、附魔台、容器、磁石等方块窗口稳定原地打开的眼睛到中心距离上限
STATION_EYE_REACH: float = 3.5
#: 近场实体右键交互（喂食、剪毛等）眼睛距离上限
ENTITY_EYE_REACH: float = 4.0

_FACE_NORMALS: tuple[tuple[int, int, int], ...] = (
    (-1, 0, 0), (1, 0, 0),
    (0, -1, 0), (0, 1, 0),
    (0, 0, -1), (0, 0, 1),
)
_FACE_INSET: float = 0.42

Cell = tuple[int, int, int]
Point3 = tuple[float, float, float]


def _is_finite_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def point3(value: Any, *, label: str = "三维坐标") -> Point3:
    """校验并返回 `(x, y, z)` 浮点元组；拒绝布尔值、缺失值与 NaN/Inf。"""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 3:
        if all(_is_finite_num(axis) for axis in value):
            return (float(value[0]), float(value[1]), float(value[2]))
    raise Replan(f"缺少有效的{label}")


def feet_pos(source: Any) -> Point3:
    """从 `/status` 快照字典或坐标序列提取玩家脚底坐标 `(x, y, z)`。"""
    raw = source.get("pos") if isinstance(source, Mapping) else source
    return point3(raw, label="玩家脚底坐标")


def eye_height(source: Any = None, *, sneaking: bool | None = None) -> float:
    """返回当前姿态对应的眼高（潜行 `1.27`，其余 `1.62`）。"""
    if sneaking is not None:
        if not isinstance(sneaking, bool):
            raise Replan("sneaking 参数必须是布尔值")
        return EYE_HEIGHT_SNEAKING if sneaking else EYE_HEIGHT_STANDING
    if isinstance(source, bool):
        return EYE_HEIGHT_SNEAKING if source else EYE_HEIGHT_STANDING
    if isinstance(source, Mapping) and source.get("sneaking") is True:
        return EYE_HEIGHT_SNEAKING
    return EYE_HEIGHT_STANDING


def eye_pos(source: Any, *, sneaking: bool | None = None) -> Point3:
    """计算玩家眼睛世界坐标 `(x, y + eye_height, z)`。"""
    x, y, z = feet_pos(source)
    return (x, y + eye_height(source, sneaking=sneaking), z)


def block_cell(pos: Any) -> Cell:
    """将三维坐标规整为方块整数格 `(bx, by, bz)`。"""
    x, y, z = point3(pos, label="方块坐标")
    return (math.floor(x), math.floor(y), math.floor(z))


def block_center(pos: Any) -> Point3:
    """返回方块几何中心 `(bx + 0.5, by + 0.5, bz + 0.5)`，消除角点正负轴不对称偏差。"""
    bx, by, bz = block_cell(pos)
    return (bx + 0.5, by + 0.5, bz + 0.5)


def eye_to_block_dist(source: Any, block_pos: Any, *, sneaking: bool | None = None) -> float:
    """零网络请求：计算玩家眼睛到目标方块几何中心的欧氏距离（对齐规范 §4.2 `eye_dist`）。"""
    return math.dist(eye_pos(source, sneaking=sneaking), block_center(block_pos))


def eye_to_aabb_dist(source: Any, block_pos: Any, *, sneaking: bool | None = None) -> float:
    """零网络请求：计算玩家眼睛到整格方块 `[x, x+1]×[y, y+1]×[z, z+1]` 碰撞箱最近点的距离。"""
    ex, ey, ez = eye_pos(source, sneaking=sneaking)
    bx, by, bz = block_cell(block_pos)
    cx = min(max(ex, float(bx)), float(bx + 1))
    cy = min(max(ey, float(by)), float(by + 1))
    cz = min(max(ez, float(bz)), float(bz + 1))
    return math.dist((ex, ey, ez), (cx, cy, cz))


def eye_to_point_dist(source: Any, target_point: Any, *, sneaking: bool | None = None) -> float:
    """零网络请求：计算玩家眼睛到任意目标点（实体、标靶面心、抛竿点等）的欧氏距离。"""
    return math.dist(eye_pos(source, sneaking=sneaking), point3(target_point))


def voxel_line_cells(start: Any, end: Any) -> list[Cell]:
    """3D Amanatides-Woo 体素步进：枚举线段经过的全部体素格（含首尾端点）。"""
    s = point3(start, label="射线起点")
    e = point3(end, label="射线终点")
    cell = list(block_cell(s))
    target = block_cell(e)
    delta = [b - a for a, b in zip(s, e)]
    step = [(d > 0) - (d < 0) for d in delta]
    stride = [abs(1.0 / d) if d else math.inf for d in delta]
    crossing = [
        (cell[i] + (step[i] > 0) - s[i]) / delta[i] if cell[i] != target[i] else math.inf
        for i in range(3)
    ]
    out: list[Cell] = [tuple(cell)]
    while tuple(cell) != target:
        first = min(crossing)
        for i in range(3):
            if math.isclose(crossing[i], first, abs_tol=1e-10, rel_tol=0.0):
                cell[i] += step[i]
                crossing[i] = crossing[i] + stride[i] if cell[i] != target[i] else math.inf
        out.append(tuple(cell))
    return out


def _client_rtt_s(client: Any) -> float:
    raw = getattr(client, "smoothed_rtt_s", 0.0)
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) else 0.0


def _is_cell_passable(
    ex: "Executor",
    cell: Cell,
    passable_names: frozenset[str],
    reject_waterlogged: bool,
    cache: dict[Cell, dict],
) -> bool:
    if cell not in cache:
        client = getattr(ex, "client", None)
        rtt = _client_rtt_s(client)
        st_obj = getattr(ex, "state", None)
        if rtt > 0.15 and st_obj is not None:
            from .voxel_cache import voxel_cache_for
            dim = str(((getattr(st_obj, "st", None) or {}).get("dimension")) or "overworld")
            try:
                cache[cell] = voxel_cache_for(st_obj).get_block(client, list(cell), dimension=dim)
            except ApiError as exc:
                if exc.code in ("not_loaded", "not_ready"):
                    cache[cell] = {}
                else:
                    raise
        else:
            try:
                cache[cell] = ex.client.get_block(list(cell))
            except ApiError as exc:
                if exc.code in ("not_loaded", "not_ready"):
                    cache[cell] = {}
                else:
                    raise
    block = cache[cell]
    if not isinstance(block, dict):
        return False
    if reject_waterlogged and (block.get("properties") or {}).get("waterlogged") in (True, "true"):
        return False
    name = bare(block.get("name"))
    return bool(name) and name in passable_names


def ray_clear_to_point(
    ex: "Executor",
    start: Any,
    aim_point: Any,
    *,
    target_cell: Cell | None = None,
    passable: Collection[str] = AIR,
    reject_waterlogged: bool = False,
    cache: dict[Cell, dict] | None = None,
) -> bool:
    """检查从 `start` 到 `aim_point`（落于 `target_cell` 内）的直线体素路径是否无遮挡。

    当射线恰好擦过体素棱边或顶点时，保守要求相邻夹角格也全部可穿透，防止隔对角双石缝误判可见。
    """
    s = point3(start, label="射线起点")
    aim = point3(aim_point, label="瞄准点")
    end_cell = block_cell(aim) if target_cell is None else block_cell(target_cell)
    cell = block_cell(s)
    if cell == end_cell:
        return True

    passable_set = frozenset(bare(name) for name in passable)
    block_cache = cache if cache is not None else {}
    client = getattr(ex, "client", None)
    if _client_rtt_s(client) > 0.15:
        from .voxel_cache import has_batch_get_blocks, voxel_cache_for
        if has_batch_get_blocks(client):
            line_cells = [c for c in voxel_line_cells(s, aim) if c != end_cell and c not in block_cache]
            if len(line_cells) >= 2:
                st_obj = getattr(ex, "state", None)
                if st_obj is not None:
                    dim = str(((getattr(st_obj, "st", None) or {}).get("dimension")) or "overworld")
                    fetched = voxel_cache_for(st_obj).get_blocks_batch(client, line_cells, dimension=dim)
                else:
                    fetched = client.get_blocks_batch(line_cells)
                if isinstance(fetched, Mapping):
                    for k, v in fetched.items():
                        if isinstance(v, dict) and v:
                            block_cache[k] = v
    delta = tuple(b - a for a, b in zip(s, aim))
    sign = tuple(1 if d > 0 else -1 for d in delta)

    while cell != end_cell:
        if not _is_cell_passable(ex, cell, passable_set, reject_waterlogged, block_cache):
            return False
        crossings = tuple(
            ((cell[i] + (sign[i] > 0)) - s[i]) / d if cell[i] != end_cell[i] else math.inf
            for i, d in enumerate(delta)
        )
        first = min(crossings)
        if not math.isfinite(first):
            return False
        hit = tuple(math.isclose(t, first, rel_tol=0.0, abs_tol=1e-9) for t in crossings)
        stepped = tuple(v + sign[i] if hit[i] else v for i, v in enumerate(cell))
        if sum(hit) > 1:
            corners = product(*(tuple({a, b}) for a, b in zip(cell, stepped)))
            if any(
                not _is_cell_passable(ex, corner, passable_set, reject_waterlogged, block_cache)
                for corner in corners
                if corner not in (cell, stepped)
            ):
                return False
        cell = stepped
    return True


def can_see_block(
    ex: "Executor",
    source: Any,
    block_pos: Any,
    *,
    sneaking: bool | None = None,
    passable: Collection[str] = AIR,
    reject_waterlogged: bool = False,
    face_fallback: bool = True,
    cache: dict[Cell, dict] | None = None,
) -> bool:
    """检查玩家眼睛是否能无遮挡看到目标方块。

    1. 先测「眼睛 → 方块球心」主射线；
    2. 若球心被顶板/台阶边缘轻擦遮挡且 `face_fallback=True`，再测朝向玩家且邻格已暴露为可穿透的方块表面中心；
    3. 单次调用内共享 `get_block` 缓存，同一体素最多查询一次。
    """
    eye = eye_pos(source, sneaking=sneaking)
    target = block_cell(block_pos)
    center = block_center(target)
    block_cache = cache if cache is not None else {}
    passable_set = frozenset(bare(name) for name in passable)

    if ray_clear_to_point(
        ex,
        eye,
        center,
        target_cell=target,
        passable=passable_set,
        reject_waterlogged=reject_waterlogged,
        cache=block_cache,
    ):
        return True
    if not face_fallback:
        return False

    bx, by, bz = target
    for nx, ny, nz in _FACE_NORMALS:
        if nx == -1 and eye[0] >= bx:
            continue
        if nx == 1 and eye[0] <= bx + 1:
            continue
        if ny == -1 and eye[1] >= by:
            continue
        if ny == 1 and eye[1] <= by + 1:
            continue
        if nz == -1 and eye[2] >= bz:
            continue
        if nz == 1 and eye[2] <= bz + 1:
            continue
        neighbor = (bx + nx, by + ny, bz + nz)
        if not _is_cell_passable(ex, neighbor, passable_set, reject_waterlogged, block_cache):
            continue
        face_aim = (
            center[0] + _FACE_INSET * nx,
            center[1] + _FACE_INSET * ny,
            center[2] + _FACE_INSET * nz,
        )
        if ray_clear_to_point(
            ex,
            eye,
            face_aim,
            target_cell=target,
            passable=passable_set,
            reject_waterlogged=reject_waterlogged,
            cache=block_cache,
        ):
            return True
    return False


def can_reach_block(
    ex: "Executor",
    source: Any,
    block_pos: Any,
    *,
    reach: float = DIG_EYE_REACH,
    sneaking: bool | None = None,
    check_los: bool = True,
    passable: Collection[str] = AIR,
    reject_waterlogged: bool = False,
    face_fallback: bool = True,
    cache: dict[Cell, dict] | None = None,
) -> bool:
    """两段式原地触达判定：先做零请求眼距粗筛，通过后再按需做 3D 体素视线遮挡复核。"""
    if eye_to_block_dist(source, block_pos, sneaking=sneaking) > reach:
        return False
    if not check_los:
        return True
    return can_see_block(
        ex,
        source,
        block_pos,
        sneaking=sneaking,
        passable=passable,
        reject_waterlogged=reject_waterlogged,
        face_fallback=face_fallback,
        cache=cache,
    )
