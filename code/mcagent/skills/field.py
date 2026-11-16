"""有界现场观察与物资操作。坐标和实体记录由调用者当场观察。"""
from __future__ import annotations

import math
import time
from itertools import islice

from ..runtime import EnvBlocked, Replan
from .base import rec
from .items import AIR, PLACEABLE_BLOCKS, bare
from .observations import observe_block as _observed_block


_UNSAFE_SUPPORT = AIR | {"water", "lava", "bubble_column", "fire", "soul_fire",
                         "powder_snow", "magma_block", "cactus", "campfire", "soul_campfire"}
_KNOWN_SUPPORT = set(PLACEABLE_BLOCKS) | {
    "grass_block", "coarse_dirt", "rooted_dirt", "podzol", "mycelium", "bedrock", "obsidian",
    "sand", "red_sand", "gravel", "clay", "mud", "stone_bricks", "deepslate", "end_stone", "blackstone", "basalt", "soul_soil",
    "bricks", "glass", "nether_bricks", "crimson_planks", "warped_planks",
}
_SLOTS = {"hand", "off-hand", "head", "torso", "legs", "feet"}


def _bounded(value, lo, hi, label):
    if type(value) is not int or not lo <= value <= hi:
        raise Replan(f"{label} 必须是 {lo}–{hi} 的整数")


def _position(value):
    if (isinstance(value, (list, tuple)) and len(value) == 3
            and all(type(v) in (int, float) and math.isfinite(v) for v in value)):
        return tuple(value)
    raise Replan("缺少当前观测到的有效三维坐标")


def _cell(value):
    return tuple(map(math.floor, _position(value)))


def _block_matches(block, names, properties=None, mature=None):
    return (bare(block.get("name")) in names
            and all((block.get("properties") or {}).get(key) == value
                    for key, value in (properties or {}).items())
            and (mature is None or block.get("mature") is mature))


def _scan_result(snapshot, observed_at, radius, key, hits):
    return {
        "status": "found" if hits else "unknown",
        key: hits,
        "origin": snapshot.get("pos"),
        "dimension": snapshot.get("dimension"),
        "radius": radius,
        "observed_at": observed_at,
    }


def scan_blocks(ex, state, types=None, *, radius=16, limit=12, properties=None, mature=None):
    """§3.3–3.4：复读最多 limit 个暴露候选。未知不能当作地形不存在。"""
    _bounded(radius, 1, 64, "方块搜索半径")
    _bounded(limit, 1, 64, "方块搜索条数")
    names = {bare(name) for name in ([types] if isinstance(types, str) else types or ())}
    snapshot = state.refresh_status(ex.client, max_age_s=0)
    observed_at = time.monotonic()
    found = ex.client.find_blocks(types=sorted(names) or None, radius=radius,
                                  limit=limit, properties=properties, mature=mature)
    blocks = found.get("blocks") or []
    if isinstance(blocks, dict):
        summaries = sorted(blocks.items(), key=lambda entry: entry[1].get("dist", math.inf))
        blocks = ({"name": name, "pos": summary.get("nearest"), "dist": summary.get("dist")}
                  for name, summary in summaries)
    hits = []
    for candidate in islice(blocks, limit):
        if not candidate.get("pos") or not (name := bare(candidate.get("name"))):
            continue
        pos = _cell(candidate["pos"])
        observed = _observed_block(ex, pos)
        if _block_matches(observed, names or {name}, properties, mature):
            hits.append({**candidate, **observed, "pos": list(pos), "name": bare(observed["name"])})
    return _scan_result(snapshot, observed_at, radius, "blocks", hits)


def scan_entities(ex, state, types=None, *, radius=32, limit=40):
    """§3.5：实体快照批次。空回执只表示本次结果未知。"""
    _bounded(radius, 1, 64, "实体搜索半径")
    _bounded(limit, 1, 40, "实体搜索条数")
    names = [bare(types)] if isinstance(types, str) else [bare(name) for name in types or ()]
    snapshot = state.refresh_status(ex.client, max_age_s=0)
    observed_at = time.monotonic()
    entities = ex.client.get_entities(radius=radius, types=names or None).get("entities") or []
    hits = [
        entity for entity in entities[:limit]
        if type(entity.get("id")) is int and entity.get("pos")
        and (not names or bare(entity.get("name")) in names or entity.get("type") in names)
    ]
    return _scan_result(snapshot, observed_at, radius, "entities", hits)


def _safe_support(ex, pos):
    name = bare(_observed_block(ex, pos).get("name"))
    return name in _KNOWN_SUPPORT or name.endswith("_ore")


def _standable_cell(ex, pos):
    """当前可见整格站位；局部移动和撤离的前置证据。"""
    x, y, z = _cell(pos)
    return (_safe_support(ex, (x, y - 1, z))
            and all(bare(_observed_block(ex, (x, y + dy, z)).get("name")) in AIR for dy in (0, 1)))


def _grounded(ex, status):
    if (status.get("on_ground") is not True or not status.get("alive", True)
            or status.get("in_lava") or status.get("in_water")
            or status.get("in_vehicle") or status.get("on_fire")):
        return False
    x, y, z = _position(status.get("pos"))
    support_y = math.floor(y - 0.01)
    # 与 SafetyMonitor 一致，在一位小数的 /status 坐标下复核碰撞箱覆盖范围。
    columns = dict.fromkeys(((math.floor(x), math.floor(z)), *(
        (math.floor(x + dx), math.floor(z + dz))
        for dx in (-0.35, 0.35) for dz in (-0.35, 0.35)
    )))
    return any(_safe_support(ex, (cx, support_y, cz)) for cx, cz in columns)


def move_local(ex, state, pos, *, rng=1, max_distance=16, require_move=False):
    """§3.1、§3.4、§4.1：到已观察的站位，确认同维度、距离与落脚支撑。"""
    _bounded(rng, 1, 4, "局部移动到达范围")
    _bounded(max_distance, 1, 64, "局部移动最远范围")
    pos = _position(pos)
    before = state.refresh_status(ex.client, max_age_s=0)
    distance = math.dist(_position(before.get("pos")), pos)
    if distance > max_distance:
        raise Replan(f"目标超出局部移动范围 {max_distance} 格")
    if not require_move and distance <= rng + 0.09 and _grounded(ex, before):
        return before
    if not _standable_cell(ex, pos):
        raise Replan(f"位置 {pos} 的承重和两格净空尚未确认")
    receipt = ex.move_to(pos=list(pos), rng=rng)
    yield rec("move_to", receipt, pos=list(pos))
    after = state.refresh_status(ex.client, max_age_s=0)
    if receipt.get("dimension_changed") or after.get("dimension") != before.get("dimension"):
        raise Replan("局部移动发生维度变化，需复核落地安全")
    if math.dist(_position(after.get("pos")), pos) > rng + 0.09 or not _grounded(ex, after):
        raise Replan(f"移动后未确认安全到达 {pos} 附近")
    return after


def place_block(ex, state, item, pos, *, expected_block=None, properties=None):
    """§3.4、§4.3：从已观察的空格放置，完成以世界方块为准。"""
    item = bare(item)
    pos = _cell(pos)
    names = {bare(expected_block or item)}
    before = _observed_block(ex, pos)
    if _block_matches(before, names, properties):
        return before
    if bare(before.get("name")) not in AIR:
        raise Replan(f"放置前 {pos} 空位未确认")
    state.refresh_inventory(ex.client)
    if state.count(item) < 1:
        raise Replan(f"缺少要放置的 {item}")
    cur = state.refresh_status(ex.client, max_age_s=0).get("pos")
    x, y, z = _cell(cur)
    if abs(pos[1] - y) <= 1 and pos[0] == x and pos[2] == z:
        raise Replan(f"放置点 {pos} 与自身站位重叠")
    yield rec("place", ex.place(item, pos=list(pos)), item=item, pos=list(pos))
    after = _observed_block(ex, pos)
    if not _block_matches(after, names, properties):
        raise Replan(f"放置后 {pos} 未确认 {expected_block or item} 及要求属性")
    return after


def _slot_item(inventory, slot):
    if slot == "hand":
        return inventory.get("held") or {}
    if slot == "off-hand":
        return inventory.get("offhand") or {}
    armor = inventory.get("armor") or {}
    return (armor.get(slot) or (armor.get("chest") if slot == "torso" else None)) or {}


def _equip_matches(entry, item, min_durability):
    if bare(entry.get("name")) != item:
        return False
    durability = entry.get("durability")
    if min_durability is None:
        return not durability or durability[0] > 0
    return bool(durability and durability[0] >= min_durability)


def equip_item(ex, state, item, slot="hand", *, min_durability=None):
    """§3.2、§4.10：从背包检查实际槽位，最小耐久未知时不得通过该前置。"""
    slot = {"chest": "torso", "offhand": "off-hand"}.get(slot, slot)
    if slot not in _SLOTS:
        raise Replan(f"没有该装备槽 {slot}")
    if min_durability is not None:
        _bounded(min_durability, 1, 10000, "最低耐久")
    item = bare(item)
    entry = _slot_item(state.refresh_inventory(ex.client), slot)
    if _equip_matches(entry, item, min_durability):
        return entry
    if state.count(item) < 1:
        raise Replan(f"背包缺少装备 {item}")
    yield rec("equip", ex.equip(item, slot), item=item, slot=slot)
    entry = _slot_item(state.refresh_inventory(ex.client), slot)
    if not _equip_matches(entry, item, min_durability):
        raise Replan(f"装备后 {slot} 未确认 {item} 或所需耐久")
    return entry


def _local_mine_target(ex, state, pos, names):
    cur = _position(state.refresh_status(ex.client, max_age_s=0).get("pos"))
    center = tuple(v + 0.5 for v in pos)
    eye = (cur[0], cur[1] + 1.62, cur[2])
    if math.dist(eye, center) > 4 or (_cell(cur)[::2] == pos[::2] and pos[1] < cur[1]):
        return {}
    block = _observed_block(ex, pos)
    return block if _block_matches(block, names) else {}


def mine_block_batch(ex, state, positions, *, block_types, drop, count=1, tool=None, max_blocks=16):
    """§3.2、§3.4、§4.2：近处逐格复核，返回实收新增数量。"""
    _bounded(count, 1, 64, "本批采集数量")
    _bounded(max_blocks, 1, 64, "本批采集格数")
    names = {bare(name) for name in ([block_types] if isinstance(block_types, str) else block_types)}
    names -= _UNSAFE_SUPPORT
    if not names:
        raise Replan("缺少可采集的安全方块类型")
    drop = bare(drop)
    state.refresh_inventory(ex.client)
    initial = state.count(drop)
    if tool is not None and state.count(bare(tool)) < 1:
        raise Replan(f"缺少采收工具 {tool}")
    candidates = dict.fromkeys(_cell(pos) for pos in islice(positions, max_blocks))
    equipped = tool is None
    for pos in candidates:
        block = _local_mine_target(ex, state, pos, names)
        if not block:
            continue
        if not equipped:
            yield from equip_item(ex, state, tool)
            equipped = True
            block = _local_mine_target(ex, state, pos, names)
            if not block:
                continue
        if block.get("diggable") is False or block.get("harvest_tool_ok") is False:
            raise Replan(f"无法安全采收 {pos} 的 {block['name']}，请换工具或矿点")
        before = state.count(drop)
        receipt = ex.dig(list(pos), collect=True)
        yield rec("dig", receipt, pos=list(pos))
        state.refresh_inventory(ex.client)
        if receipt.get("warning") or state.count(drop) <= before:
            raise Replan(f"挖掘后背包未确认增加 {drop}，检查工具和掉落位置")
        remaining = bare(_observed_block(ex, pos).get("name"))
        if not remaining or remaining == bare(block.get("name")):
            raise Replan(f"挖掘后未确认目标格 {pos} 已发生改变")
        if state.count(drop) >= initial + count:
            return state.count(drop) - initial
    raise EnvBlocked(f"已观察的近处矿点不足以采齐 {count} 个 {drop}",
                     missing_predicate=f"reachable_blocks:{drop}",
                     searched_volume=f"近处 4 格、至多 {max_blocks} 个观测候选",
                     wake_on="chunk_moved")


def _drop_name(entity):
    item = entity.get("item")
    if not isinstance(item, str):
        return ""
    name, sep, count = item.rpartition("x")
    return bare(name if sep and count.isascii() and count.isdigit() and int(count) > 0 else item)


def collect_drops(ex, state, item, count=1, *, radius=8, max_targets=4):
    """§3.2、§3.5、§4.1：尝试局部拾取，返回实收新增数量。"""
    _bounded(count, 1, 64, "本批拾取数量")
    _bounded(radius, 1, 16, "掉落物搜索半径")
    _bounded(max_targets, 1, 8, "掉落物移动预算")
    item = bare(item)
    state.invalidate_inventory()
    state.refresh_inventory(ex.client)
    initial = state.count(item)
    attempted = set()
    for _ in range(max_targets):
        scan = scan_entities(ex, state, "item", radius=radius)
        target = next((entity for entity in scan["entities"]
                       if entity["id"] not in attempted and _drop_name(entity) == item
                       and _standable_cell(ex, entity["pos"])), None)
        if target is None:
            break
        attempted.add(target["id"])
        yield from move_local(ex, state, target["pos"], max_distance=radius, require_move=True)
        # move_to 不会使运行时背包缓存失效，必须显式刷新再核对实际拾取。
        state.invalidate_inventory()
        state.refresh_inventory(ex.client)
        gained = state.count(item) - initial
        if gained >= count:
            return gained
    if attempted:
        raise Replan(f"向 {item} 掉落物移动后未确认拾齐 {count} 个，重新观察后再选点")
    raise EnvBlocked(f"未观察到可局部拾取的 {item} 掉落物",
                     missing_predicate=f"visible_drop:{item}",
                     searched_volume=f"半径 {radius} 格、最多 40 个实体",
                     wake_on="chunk_moved")
