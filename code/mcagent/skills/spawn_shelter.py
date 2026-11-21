"""出生点保护区外缘 Tier 1 圆石防爆避难所与下界门 3×3 圆石碉堡 L3 技能（docs/15）。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import TYPE_CHECKING, Any, Optional

from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, failure, rec
from .camp_plan import is_protected_cell, record_protected_cell, state_dim, state_snap
from .field import _observed_block, place_block, scan_blocks
from .items import AIR, PASSABLE_COVER, bare, is_safe_solid_floor
from .observations import refresh_inventory
from .registry import register
from .sightline import DIG_EYE_REACH, STATION_EYE_REACH, block_cell, can_reach_block, eye_to_block_dist
from .voxel_cache import client_get_blocks_batch, has_batch_get_blocks
from .world_memo import BED_BLOCKS, memo_for

if TYPE_CHECKING:
    from ..runtime import Executor, State

_BLAST_BLOCKS: tuple[str, ...] = ("cobblestone", "cobbled_deepslate", "stone_bricks", "blackstone")
_DOORS: tuple[str, ...] = ("oak_door", "spruce_door", "birch_door", "jungle_door", "acacia_door",
                           "dark_oak_door", "mangrove_door", "cherry_door", "bamboo_door", "pale_oak_door")
_PASSABLE = AIR | PASSABLE_COVER
_BED_SET: frozenset[str] = frozenset(BED_BLOCKS)
_RING_RADII: tuple[int, ...] = (10, 14, 18, 22, 26, 32)
_RING_DIRS: tuple[tuple[float, float], ...] = ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0),
                                              (0.707, 0.707), (-0.707, 0.707), (0.707, -0.707), (-0.707, -0.707))
_MIN_SHELTER_COBBLE, _MIN_BUNKER_COBBLE = 36, 20


def _blast_block_count(state: "State") -> int:
    return sum(state.count(b) for b in _BLAST_BLOCKS)


def _pick_blast_block(state: "State") -> Optional[str]:
    return next((b for b in _BLAST_BLOCKS if state.count(b) >= 1), None)


def _bounded_move_to(ex: "Executor", pos: Sequence[float], *, rng: int = 2, timeout: float = 30.0) -> dict:
    dest = list(pos)
    try:
        return ex.move_to(pos=dest, rng=rng, timeout=timeout, retries=0)
    except TypeError:
        return ex.move_to(pos=dest, rng=rng)


def _read_blocks(ex: "Executor", cells: Sequence[tuple[int, int, int]]) -> dict[tuple[int, int, int], dict]:
    if has_batch_get_blocks(getattr(ex, "client", None)):
        return client_get_blocks_batch(ex.client, cells)
    return {c: _observed_block(ex, c) for c in cells}


def _site_is_valid(ex: "Executor", cx: int, cy: int, cz: int, *, radius: int = 2) -> bool:
    if is_protected_cell((cx, cy, cz)):
        return False
    floor_cells = [(cx + dx, cy - 1, cz + dz) for dx in range(-radius, radius + 1) for dz in range(-radius, radius + 1)]
    head_cells = [(cx + dx, cy + dy, cz + dz) for dx in (-1, 0, 1) for dz in (-1, 0, 1) for dy in (0, 1)]
    snap = _read_blocks(ex, [*floor_cells, *head_cells])
    for fc in floor_cells:
        if is_protected_cell(fc) or not is_safe_solid_floor(snap.get(fc, {})):
            return False
    blocked = 0
    for hc in head_cells:
        bname = bare((snap.get(hc) or {}).get("name"))
        if bname in ("water", "lava", "flowing_water", "flowing_lava", "bedrock"):
            return False
        if bname and bname not in _PASSABLE:
            blocked += 1
    return blocked <= 6


def _select_shelter_center(ex: "Executor", state: "State", dim: str) -> tuple[int, int, int]:
    st = state.refresh_status(ex.client, max_age_s=0)
    cur = block_cell(st.get("pos"))
    memo = memo_for(state)
    camp = memo.get_camp(dimension=dim)
    spawn = memo.get_spawn(dimension=dim)
    candidates: list[tuple[int, int, int]] = []
    if isinstance(camp, Mapping) and camp.get("pos"):
        candidates.append(block_cell(camp["pos"]))
    candidates.append(cur)
    anchor = block_cell(spawn) if spawn is not None else cur
    candidates.extend((anchor[0] + round(r * ux), anchor[1], anchor[2] + round(r * uz)) for r in _RING_RADII for ux, uz in _RING_DIRS)
    seen: set[tuple[int, int, int]] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if _site_is_valid(ex, cand[0], cand[1], cand[2], radius=2):
            return cand
    raise EnvBlocked("出生点保护区外缘 r=10..32 环带未找到平坦干燥 5×5 避难所地基",
                     missing_predicate="shelter_flat_site", searched_volume="出生点环带 r=10..32 5×5 地基", wake_on="chunk_moved")


def _approach_cell(ex: "Executor", state: "State", cell: tuple[int, int, int], *, reach: float = STATION_EYE_REACH):
    st = state.refresh_status(ex.client, max_age_s=0)
    if can_reach_block(ex, st, cell, reach=reach) or eye_to_block_dist(st, cell) <= reach:
        return True
    try:
        yield rec("move_to", _bounded_move_to(ex, cell, rng=2, timeout=30.0), pos=list(cell))
    except Replan as exc:
        yield rec("move_to", pos=list(cell), error_code=failure(exc)[0])
        return False
    return True


def _clear_if_needed(ex: "Executor", state: "State", cell: tuple[int, int, int], *, skip_names: frozenset[str] = frozenset()):
    bname = bare(_observed_block(ex, cell).get("name"))
    if not bname or bname in AIR or bname in skip_names:
        return True
    if bname in ("bedrock", "obsidian", "nether_portal", "water", "lava", "flowing_water", "flowing_lava"):
        return False
    if not (yield from _approach_cell(ex, state, cell, reach=DIG_EYE_REACH)):
        return False
    try:
        yield rec("dig", ex.dig(list(cell), collect=True), pos=list(cell))
        return True
    except Replan as exc:
        code = failure(exc)[0]
        if code == "protected_region":
            raise
        yield rec("dig", pos=list(cell), error_code=code)
        return False


def _place_structural(ex: "Executor", state: "State", cell: tuple[int, int, int], item: str, *, memo: Any, dim: str) -> Any:
    st = state.refresh_status(ex.client, max_age_s=0)
    px, py, pz = block_cell(st.get("pos"))
    if cell[0] == px and cell[2] == pz and abs(cell[1] - py) <= 1:
        return False
    cur_name = bare(_observed_block(ex, cell).get("name"))
    if cur_name == bare(item) or cur_name in _BLAST_BLOCKS:
        return True
    if cur_name and cur_name not in AIR:
        return False
    if state.count(item) < 1:
        return False
    if not (yield from _approach_cell(ex, state, cell, reach=STATION_EYE_REACH)):
        return False
    try:
        yield from place_block(ex, state, item, cell)
        return True
    except Replan as exc:
        code = failure(exc)[0]
        if code == "protected_region":
            record_protected_cell(cell, memo, dim)
            raise
        yield rec("place", item=item, pos=list(cell), error_code=code)
        return False


def shelter_blueprint(cx: int, cy: int, cz: int) -> dict[str, Any]:
    """生成 docs/15 §三 的 5×5×4 圆石防爆避难所几何坐标蓝图（保证箱顶 y+1 为 AIR）。"""
    door_cells = ((cx, cy, cz + 2), (cx, cy + 1, cz + 2))
    wall_cells = [(cx + dx, cy + dy, cz + dz) for dy in (0, 1) for dx in range(-2, 3) for dz in range(-2, 3)
                  if max(abs(dx), abs(dz)) == 2 and (cx + dx, cy + dy, cz + dz) not in door_cells]
    roof_cells = [(cx + dx, cy + 2, cz + dz) for dx in range(-2, 3) for dz in range(-2, 3)]
    # 箱子坐标独立非相邻（kit 与 vault 隔 1 格防合大箱，bulk 在对角），上方 cy+1 严格保持 AIR
    chests = {"kit": (cx - 1, cy, cz - 1), "vault": (cx + 1, cy, cz - 1), "bulk": (cx + 1, cy, cz + 1)}
    return {
        "center": (cx, cy, cz), "wall_cells": wall_cells, "roof_cells": roof_cells, "door_cells": door_cells,
        "chests": chests, "chest_headroom": [(x, y + 1, z) for x, y, z in chests.values()],
        "furnace": (cx - 1, cy, cz), "crafting_table": (cx - 1, cy, cz + 1),
        "torch": (cx, cy + 1, cz - 1), "bed": (cx + 1, cy, cz), "mine_entry": (cx, cy - 1, cz + 1),
    }


def _place_shelter_facilities(ex: "Executor", state: "State", bp: Mapping[str, Any], dim: str, memo: Any) -> Any:
    placed_stations: dict[str, list[int]] = {}
    placed_chests: dict[str, list[int]] = {}
    camp = memo.get_camp(dimension=dim) or {}
    existing_chests = dict(camp.get("chests") or {})
    if camp.get("chest") and "kit" not in existing_chests:
        existing_chests["kit"] = list(camp["chest"])

    # 1. 清理箱顶净空（cy+1 必须为 AIR 防无法开箱）
    for head_cell in bp["chest_headroom"]:
        yield from _clear_if_needed(ex, state, head_cell)

    # 2. 按需放置独立单箱（kit -> vault -> bulk）
    for role in ("kit", "vault", "bulk"):
        if role in existing_chests:
            placed_chests[role] = list(existing_chests[role])
            continue
        refresh_inventory(ex, state)
        if state.count("chest") < 1:
            break
        pos = bp["chests"][role]
        yield from _clear_if_needed(ex, state, pos, skip_names=frozenset({"chest"}))
        if (yield from _place_structural(ex, state, pos, "chest", memo=memo, dim=dim)):
            placed_chests[role] = list(pos)
            memo.record_station("chest", pos, dimension=dim)

    # 3. 室内工作台 / 熔炉 / 火把 / 床
    for kind in ("crafting_table", "furnace"):
        if camp.get(kind):
            placed_stations[kind] = list(camp[kind])
            continue
        refresh_inventory(ex, state)
        if state.count(kind) >= 1:
            pos = bp[kind]
            yield from _clear_if_needed(ex, state, pos, skip_names=frozenset({kind}))
            if (yield from _place_structural(ex, state, pos, kind, memo=memo, dim=dim)):
                placed_stations[kind] = list(pos)
                memo.record_station(kind, pos, dimension=dim)

    refresh_inventory(ex, state)
    if state.count("torch") >= 1:
        tpos = bp["torch"]
        if bare(_observed_block(ex, tpos).get("name")) in AIR:
            yield from _place_structural(ex, state, tpos, "torch", memo=memo, dim=dim)

    if not camp.get("bed"):
        bed_item = next((b for b in BED_BLOCKS if state.count(b) >= 1), None)
        if bed_item is not None:
            bpos = bp["bed"]
            yield from _clear_if_needed(ex, state, bpos, skip_names=_BED_SET)
            if (yield from _place_structural(ex, state, bpos, bed_item, memo=memo, dim=dim)):
                placed_stations["bed"] = list(bpos)
                memo.record_station("bed", bpos, dimension=dim)

    # 4. 门口装门或 2 格圆石临时封口
    refresh_inventory(ex, state)
    door_item = next((d for d in _DOORS if state.count(d) >= 1), None)
    if door_item is not None:
        yield from _place_structural(ex, state, bp["door_cells"][0], door_item, memo=memo, dim=dim)
    else:
        for dcell in bp["door_cells"]:
            if (blk := _pick_blast_block(state)) is not None:
                yield from _place_structural(ex, state, dcell, blk, memo=memo, dim=dim)

    return placed_chests, placed_stations


def build_nether_bunker_flow(ex: "Executor", state: "State", memo: Any, *, skill_name: str = "build_spawn_shelter"):
    """在下界门或当前位置周围建造 3×3 内腔圆石碉堡，并按需合成/放置应急补给箱。"""
    st = state.refresh_status(ex.client, max_age_s=0)
    cx, cy, cz = block_cell(st.get("pos"))
    scan = scan_blocks(ex, state, ("nether_portal",), radius=12, limit=4, skill_name=skill_name)
    if scan.get("ready") is True and (blks := scan.get("blocks") or ()):
        pcell = block_cell(blks[0]["pos"])
        cx, cy, cz = pcell[0], min(pcell[1], cy) if abs(pcell[1] - cy) <= 2 else pcell[1], pcell[2]
    placed = 0
    # 3×3 围墙 + 封顶，严禁覆盖 nether_portal / obsidian，留一侧出口
    door = {(cx, cy, cz + 2), (cx, cy + 1, cz + 2)}
    cells = [
        (cx + dx, cy + dy, cz + dz) for dy in (0, 1) for dx in range(-2, 3) for dz in range(-2, 3)
        if max(abs(dx), abs(dz)) == 2 and (cx + dx, cy + dy, cz + dz) not in door
    ] + [(cx + dx, cy + 2, cz + dz) for dx in range(-1, 2) for dz in range(-1, 2)]
    for cell in cells:
        if (blk := _pick_blast_block(state)) is None:
            break
        if bare(_observed_block(ex, cell).get("name")) in ("nether_portal", "obsidian"):
            continue
        if (yield from _place_structural(ex, state, cell, blk, memo=memo, dim="the_nether")):
            placed += 1
    chest_pos: Optional[tuple[int, int, int]] = None
    refresh_inventory(ex, state)
    if state.count("chest") < 1:
        planks = sum(int(r.get("count", 0) or 0) for r in ((state.inventory or {}).get("items") or ())
                     if bare(r.get("name")).endswith("_planks"))
        if planks >= 8 and (
            any(math.dist(c, (cx, cy, cz)) <= 6.0 for c in memo.stations("crafting_table", dimension="the_nether"))
            or bool(scan_blocks(ex, state, ("crafting_table",), radius=6, limit=1, skill_name=skill_name).get("blocks"))
        ):
            try:
                yield rec("craft", ex.craft("chest", 1), item="chest", count=1)
            except Replan:
                pass
            refresh_inventory(ex, state)
    if state.count("chest") >= 1:
        for cand in ((cx - 1, cy, cz - 1), (cx + 1, cy, cz - 1), (cx - 1, cy, cz + 1), (cx + 1, cy, cz + 1)):
            if bare(_observed_block(ex, cand).get("name")) in AIR and bare(_observed_block(ex, (cand[0], cand[1] + 1, cand[2])).get("name")) in AIR:
                if (yield from _place_structural(ex, state, cand, "chest", memo=memo, dim="the_nether")):
                    chest_pos = cand
                    memo.record_landmark("nether_portal_depot", cand, dimension="the_nether", meta={"block": "chest"})
                    break
    memo.set_camp([cx, cy, cz], dimension="the_nether", chest=list(chest_pos) if chest_pos else None,
                  shelter_tier=1, chest_role="kit" if chest_pos else None)
    return {"mode": "nether_bunker", "center": [cx, cy, cz], "placed": placed, "chest": list(chest_pos) if chest_pos else None}


@register
class BuildSpawnShelter(Skill):
    name = "build_spawn_shelter"
    description = "在出生点保护区外缘建造5x5圆石防爆避难所或下界门3x3圆石碉堡"
    params = {"mode": Param(str, "避难所建造模式", default="tier1_shelter", choices=("tier1_shelter", "nether_bunker"))}
    goal = "camp:shelter:{mode}"
    verified = "offline"
    cooldown_s = 20.0

    @classmethod
    def blocked(cls, state: "State") -> str:
        st = state_snap(state)
        if st.get("ready") is False:
            return "世界加载尚未就绪"
        if st.get("alive") is False:
            return "角色未存活，无法建造避难所"
        dim = state_dim(state)
        if dim not in ("overworld", "the_nether"):
            return f"维度 {dim} 不支持避难所建造"
        min_req = _MIN_BUNKER_COBBLE if dim == "the_nether" else _MIN_SHELTER_COBBLE
        if _blast_block_count(state) < min_req:
            return f"防爆建材不足（需至少 {min_req} 块圆石/深板岩/黑石）"
        return ""

    def can_start(self, state: "State") -> bool:
        if self.blocked(state):
            return False
        mode, dim = self.args["mode"], state_dim(state)
        if mode == "nether_bunker":
            return dim == "the_nether" and _blast_block_count(state) >= _MIN_BUNKER_COBBLE
        if dim != "overworld":
            return False
        pos = state_snap(state).get("pos")
        if isinstance(pos, Sequence) and len(pos) >= 2:
            try:
                if float(pos[1]) < 55.0:
                    return False
            except (TypeError, ValueError):
                pass
        camp = memo_for(state).get_camp(dimension="overworld")
        return not (isinstance(camp, Mapping) and int(camp.get("shelter_tier", 0)) >= 1)

    def run(self, ex: "Executor", state: "State"):
        self._build_result: Optional[dict[str, Any]] = None
        st = state.refresh_status(ex.client, max_age_s=0)
        if st.get("ready") is False or st.get("alive") is False:
            raise Replan("世界未就绪或角色未存活，停止建造避难所")
        dim, memo = state_dim(state), memo_for(state)
        if self.args["mode"] == "nether_bunker":
            if dim != "the_nether":
                raise Replan("nether_bunker 模式仅在下界执行")
            self._build_result = yield from build_nether_bunker_flow(ex, state, memo, skill_name=self.name)
            return
        if dim != "overworld":
            raise Replan("tier1_shelter 模式仅在主世界执行")

        center = None
        for _attempt in range(3):
            cand_center = _select_shelter_center(ex, state, dim)
            bp = shelter_blueprint(*cand_center)
            # 试放第一块外墙，若触发 protected_region 则记录保护区并外移重试
            probe_cell = bp["wall_cells"][0]
            blk = _pick_blast_block(state)
            if blk is None:
                raise Replan("防爆建材已耗尽")
            try:
                yield from _place_structural(ex, state, probe_cell, blk, memo=memo, dim=dim)
                center = cand_center
                break
            except Replan as exc:
                if failure(exc)[0] != "protected_region":
                    raise
                record_protected_cell(cand_center, memo, dim)
                yield rec("protected_region_skip", pos=list(cand_center))
                continue
        if center is None:
            raise EnvBlocked("出生点周边多次试放均命中保护区，暂无法锁定避难所坐标", missing_predicate="shelter_flat_site", searched_volume="出生点外缘环带", wake_on="chunk_moved")

        bp = shelter_blueprint(*center)
        placed = 1
        # 先清室内净空（保证中心与箱顶均为 AIR），再砌墙封顶
        skip_keep = frozenset({"chest", "furnace", "crafting_table", "torch"}) | _BED_SET
        for dx, dz, dy in ((dx, dz, dy) for dx in (-1, 0, 1) for dz in (-1, 0, 1) for dy in (0, 1)):
            yield from _clear_if_needed(ex, state, (center[0] + dx, center[1] + dy, center[2] + dz), skip_names=skip_keep)
        for cell in (*bp["wall_cells"][1:], *bp["roof_cells"]):
            if (blk := _pick_blast_block(state)) is None:
                break
            if (yield from _place_structural(ex, state, cell, blk, memo=memo, dim=dim)):
                placed += 1
        placed_chests, placed_stations = yield from _place_shelter_facilities(ex, state, bp, dim, memo)
        tier = 2 if len(placed_chests) >= 3 else 1
        memo.set_camp(list(center), dimension=dim, chests=placed_chests or None,
                      shelter_tier=tier, mine_entry=list(bp["mine_entry"]), **placed_stations)
        self._build_result = {"mode": "tier1_shelter", "center": list(center), "placed": placed,
                              "shelter_tier": tier, "chests": placed_chests, "mine_entry": list(bp["mine_entry"])}

    def verify_done(self, state: "State", before_counts: Optional[dict[str, int]] = None) -> str:
        dim = "the_nether" if self.args["mode"] == "nether_bunker" else "overworld"
        camp = memo_for(state).get_camp(dimension=dim)
        res = getattr(self, "_build_result", None)
        if not isinstance(res, Mapping) or res.get("mode") != self.args["mode"]:
            return "尚未完成避难所建造流程"
        if not isinstance(camp, Mapping) or int(camp.get("shelter_tier", 0)) < 1:
            return "世界记忆未确认避难所等级升级至 Tier 1+"
        return ""
