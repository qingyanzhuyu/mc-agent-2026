"""T2/T3 下界核心技能与 L2 原语：远古残骸、烈焰人、猪灵堡垒、重生锚、猪灵诱饵/交易、凋灵骷髅头。"""
from __future__ import annotations

import math
from collections.abc import Generator, Sequence
from ..runtime import EnvBlocked, Executor, Replan, State
from .base import Param, Skill, failure, rec
from .encounter import combat_melee, shoot_projectile, verify_shot_lane
from .field import _cell, _drop_name, _observed_block, _standable_cell, collect_drops, equip_item, move_local, place_block, scan_blocks, scan_entities
from .interaction import use_item_on
from .items import AIR, bare
from .landmark_nav import goto_known_landmark_or_biome
from .mining import MineStepUnsafe, mine_step, reach_ore, sort_ore_candidates
from .recipes import _table_position
from .registry import register
from .sightline import DIG_EYE_REACH, STATION_EYE_REACH, can_reach_block, eye_to_block_dist
from .voxel_cache import voxel_cache_for
from .windows import is_storage_window, take_container_item, window_amount, window_section, window_view
from .world_memo import memo_for

_GOLD_ARMOR = (("golden_helmet", "head"), ("golden_chestplate", "torso"), ("golden_leggings", "legs"), ("golden_boots", "feet"))
_GOLD_NAMES = frozenset(n for n, _ in _GOLD_ARMOR)
_DIAMOND_PICKS = frozenset({"diamond_pickaxe", "netherite_pickaxe"})
_SWORDS = ("netherite_sword", "diamond_sword", "iron_sword", "stone_sword")
_LAVA = frozenset({"lava", "flowing_lava"})
_NETHER_SOLID = frozenset({"netherrack", "basalt", "smooth_basalt", "blackstone", "polished_blackstone", "soul_soil", "nether_bricks", "nether_quartz_ore", "nether_gold_ore", "ancient_debris", "crying_obsidian", "obsidian", "cobblestone", "stone"})
_BASTION_BLOCKS = frozenset({"blackstone", "polished_blackstone_bricks", "gilded_blackstone", "gold_block", "cracked_polished_blackstone_bricks", "chiseled_polished_blackstone"})
BASTION_LOOT: tuple[str, ...] = ("netherite_upgrade_smithing_template", "ancient_debris", "netherite_scrap", "netherite_ingot", "crying_obsidian", "gold_ingot", "obsidian", "arrow")
_LOOT_RANK = {n: i for i, n in enumerate((*BASTION_LOOT, "enchanted_golden_apple", "golden_apple", "diamond", "emerald", "iron_ingot", "ender_pearl", "blaze_rod", "gold_nugget"))}
_CARDINALS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_OFFSETS_6 = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))

def _in_nether(st: dict | None) -> bool:
    return bare((st or {}).get("dimension")) == "the_nether"

def _nether_gate(state: State, *, min_hp: float | None = None) -> str:
    if not state.ready:
        return "世界尚未就绪（ready=false）"
    if not _in_nether(state.st):
        return "当前不在下界（the_nether）"
    return f"生命值过低（{state.hp:.1f} < {min_hp:.0f}）" if (min_hp is not None and state.hp < min_hp) else ""

def _fresh_bag(ex: Executor, state: State) -> State:
    state.invalidate_inventory()
    state.refresh_inventory(ex.client)
    return state

def _nether_status(ex: Executor, state: State, msg: str, *, min_hp: float | None = None) -> dict:
    st = state.refresh_status(ex.client, max_age_s=0)
    if not _in_nether(st):
        raise Replan(msg)
    if min_hp is not None and float(st.get("health", 20)) < min_hp:
        raise Replan(f"血量过低（{st.get('health')} < {min_hp:.0f}），止损撤离")
    return st

def _ready(scan: dict) -> dict:
    if scan.get("ready") is False:
        raise Replan("世界加载尚未就绪（ready=false）")
    return scan
def _scan_blocks_ready(ex: Executor, state: State, types, *, radius: int, limit: int, skill_name: str) -> list[dict]:
    return _ready(scan_blocks(ex, state, types, radius=radius, limit=limit, skill_name=skill_name)).get("blocks") or []
def _scan_named(ex: Executor, state: State, kind: str, radius: int) -> tuple[dict, list[dict]]:
    scan = _ready(scan_entities(ex, state, kind, radius=radius))
    return scan, [e for e in (scan.get("entities") or ()) if bare(e.get("name")) == kind]

def _safe_retreat(ex: Executor, st: dict) -> list[float] | None:
    cx, cy, cz = _cell(st.get("pos") or [0, 0, 0])
    return next(([cx + dx + 0.5, float(cy), cz + dz + 0.5] for dx, dz in _CARDINALS if _standable_cell(ex, (cx + dx, cy, cz + dz))), None)

def _collect_drop_if_seen(ex: Executor, state: State, item: str, *, radius: int) -> Generator[dict, None, None]:
    scan = _ready(scan_entities(ex, state, "item", radius=radius))
    if any(_drop_name(e) == item and _standable_cell(ex, e["pos"]) for e in (scan.get("entities") or ())):
        yield from collect_drops(ex, state, item, count=1, radius=radius)

def _move_to_cell(ex: Executor, state: State, pos: Sequence[float], *, rng: int = 2) -> Generator[dict, None, dict]:
    dest = [float(pos[0]), float(pos[1]), float(pos[2])]
    yield rec("move_to", ex.move_to(pos=dest, rng=rng), pos=dest)
    return state.refresh_status(ex.client, max_age_s=0)

def _find_place_cell(ex: Executor, st: dict) -> list[int] | None:
    cx, cy, cz = _cell(st.get("pos") or [0, 0, 0])
    for dy in (0, 1):
        for dx, dz in _CARDINALS:
            cand = (cx + dx, cy + dy, cz + dz)
            below = bare(_observed_block(ex, (cand[0], cand[1] - 1, cand[2])).get("name"))
            if bare(_observed_block(ex, cand).get("name")) in AIR and below and below not in (AIR | _LAVA | {"fire", "soul_fire"}):
                return list(cand)
    return None

def _hunt_mob_for_drop(
    ex: Executor, state: State, *, mob: str, drop: str, target_count: int, radius: int,
    max_kills: int, allow_ranged: bool, missing_msg: str, fail_msg: str,
    landmark: str | Sequence[str] | None = None,
) -> Generator[dict, None, None]:
    _fresh_bag(ex, state)
    navigated = False
    for _ in range(max_kills):
        if state.count(drop) >= target_count:
            return
        st = _nether_status(ex, state, f"已离开下界，停止猎杀 {mob}", min_hp=10.0)
        _, targets = _scan_named(ex, state, mob, radius)
        if not targets:
            yield from _collect_drop_if_seen(ex, state, drop, radius=min(16, radius))
            if state.count(drop) >= target_count:
                return
            if landmark and not navigated and (yield from goto_known_landmark_or_biome(ex, state, landmark)):
                navigated = True
                st = _nether_status(ex, state, f"已离开下界，停止猎杀 {mob}", min_hp=10.0)
                _, targets = _scan_named(ex, state, mob, radius)
            if not targets:
                raise EnvBlocked(missing_msg, missing_predicate=mob, searched_volume=f"半径 {radius} 格", wake_on="chunk_moved")
        target = min(targets, key=lambda e: math.dist(st["pos"], e["pos"]))
        retreat = _safe_retreat(ex, st)
        arrows = any(state.count(a) >= 1 for a in ("arrow", "spectral_arrow", "tipped_arrow"))
        weapon = ("bow" if (state.count("bow") >= 1 and arrows) else ("crossbow" if (state.count("crossbow") >= 1 and (arrows or state.count("firework_rocket") >= 1)) else None)) if allow_ranged else None
        aim = (target["pos"][0], target["pos"][1] + 0.5, target["pos"][2])
        if weapon and retreat is not None and math.dist(st["pos"], target["pos"]) <= 32 and verify_shot_lane(ex, state, aim, retreat_pos=retreat, max_distance=32)["status"] == "clear":
            yield from shoot_projectile(ex, state, entity=target, weapon=weapon, retreat_pos=retreat)
        else:
            if math.dist(st["pos"], target["pos"]) > 4.5:
                st = yield from _move_to_cell(ex, state, target["pos"], rng=2)
                retreat = _safe_retreat(ex, st)
            if (sword := next((s for s in _SWORDS if state.count(s) >= 1), None)) is not None:
                yield from equip_item(ex, state, sword, "hand")
            yield from combat_melee(ex, state, target, retreat_pos=retreat, radius=min(16, radius))
        if _fresh_bag(ex, state).count(drop) < target_count:
            yield from _collect_drop_if_seen(ex, state, drop, radius=8)
    if state.count(drop) < target_count:
        raise Replan(fail_msg)

def drop_item(ex: Executor, state: State, item: str, count: int = 1) -> Generator[dict, None, dict]:
    """丢弃指定数量物品，独立刷新背包确认数量减少。"""
    if type(count) is not int or count < 1:
        raise Replan("丢弃数量必须是正整数")
    item = bare(item)
    if (before := _fresh_bag(ex, state).count(item)) < count:
        raise Replan(f"背包缺少 {count} 个 {item}（当前 {before}）")
    receipt = ex.drop(item, count)
    yield rec("drop", receipt, item=item, count=count)
    if before - (after := _fresh_bag(ex, state).count(item)) < count:
        raise Replan(f"丢弃 {item} 后背包未确认减少 {count} 个")
    return {"receipt": receipt, "item": item, "dropped": before - after, "before": before, "after": after}

def ensure_gold_armor(ex: Executor, state: State) -> Generator[dict, None, bool]:
    """确认已穿戴任意金护甲；未穿戴但包内有时立即装备一件。"""
    armor = state.refresh_inventory(ex.client).get("armor") or {}
    if isinstance(armor, dict) and any(bare((armor.get(s) or {}).get("name")) in _GOLD_NAMES for s in ("head", "torso", "chest", "legs", "feet")):
        return True
    for piece, slot in _GOLD_ARMOR:
        if state.count(piece) >= 1:
            yield from equip_item(ex, state, piece, slot)
            return True
    return False

def loot_container_items(ex: Executor, state: State, pos: Sequence[float], *, wanted: tuple[str, ...] = ()) -> Generator[dict, None, list[str]]:
    """安全抵近容器并按优先级逐项取出战利品（遇满包优雅停止），返回实取物品名列表。"""
    cell, st = _cell(pos), state.refresh_status(ex.client, max_age_s=0)
    if eye_to_block_dist(st, cell) > STATION_EYE_REACH or not can_reach_block(ex, st, cell, reach=STATION_EYE_REACH):
        st = yield from _move_to_cell(ex, state, cell, rng=2)
        if eye_to_block_dist(st, cell) > STATION_EYE_REACH + 0.5:
            raise Replan(f"接近容器 {list(cell)} 后仍超出交互距离")
    target = {"pos": list(cell)}
    view = yield from window_view(ex, target)
    if not is_storage_window(view):
        raise Replan(f"位置 {list(cell)} 不是可取物存储容器")
    avail: dict[str, int] = {}
    for e in window_section(view, "container"):
        if (name := bare(e.get("name"))) and (cnt := int(e.get("count", 0) or 0)) > 0:
            avail[name] = avail.get(name, 0) + cnt
    order = [w for w in dict.fromkeys(bare(x) for x in wanted) if avail.get(w, 0) > 0] if wanted else sorted(avail, key=lambda n: (_LOOT_RANK.get(n, 999), -avail[n], n))
    taken: list[str] = []
    for item_name in order:
        if (amt := window_amount(view, "container", item_name)) <= 0:
            continue
        try:
            res = yield from take_container_item(ex, state, target, "container", item_name, amt, view=view)
        except Replan as exc:
            code, _ = failure(exc)
            if code == "inventory_full" or "inventory_full" in str(exc):
                yield rec("window_act", target=target, item=item_name, error_code="inventory_full")
                break
            raise
        taken.append(item_name)
        view = res["view"]
        if res.get("inventory_full"):
            break
    return taken

@register
class MineAncientDebris(Skill):
    name, goal, verified = "mine_ancient_debris", "nether:ancient_debris", "offline"
    description = "在下界探测暴露远古残骸或安全阶梯下行采掘"
    params = {"count": Param(int, "目标远古残骸数", default=1, min=1, max=8), "max_steps": Param(int, "阶梯掘进步数", default=24, min=4, max=48)}
    @classmethod
    def blocked(cls, state: State) -> str:
        return _nether_gate(state) or ("" if any(state.count(p) >= 1 for p in _DIAMOND_PICKS) else "缺少钻石镐或下界合金镐")
    def run(self, ex: Executor, state: State):
        target_count, max_steps = self.args["count"], self.args["max_steps"]
        _fresh_bag(ex, state)
        for step_idx in range(max_steps):
            if state.count("ancient_debris") >= target_count:
                return
            st = _nether_status(ex, state, "已离开下界，停止采掘远古残骸")
            if (pick := next((p for p in ("netherite_pickaxe", "diamond_pickaxe") if state.count(p) >= 1), None)) is None:
                raise Replan("背包缺少钻石镐或下界合金镐")
            if blocks := _scan_blocks_ready(ex, state, "ancient_debris", radius=16, limit=12, skill_name=self.name):
                ordered = sort_ore_candidates(st.get("pos") or state.pos, blocks)
                safe = [b for b in ordered if not any(bare(_observed_block(ex, (_cell(b["pos"])[0] + dx, _cell(b["pos"])[1] + dy, _cell(b["pos"])[2] + dz)).get("name")) in _LAVA for dx, dy, dz in _OFFSETS_6)]
                pos = [int(v) for v in (safe or ordered)[0]["pos"]]
                if not can_reach_block(ex, st, pos, reach=DIG_EYE_REACH):
                    pos = yield from reach_ore(ex, state, pos, skill_name=self.name)
                yield from equip_item(ex, state, pick, "hand")
                res = ex.dig(pos, collect=True)
                voxel_cache_for(state).invalidate(pos, dimension=str(state.st.get("dimension") or "the_nether"))
                yield rec("dig", res, pos=pos)
                _fresh_bag(ex, state)
                continue
            cur = _cell(st.get("pos") or state.pos)
            if cur[1] <= 15:
                break
            for idx in range(4):
                dx, dz = _CARDINALS[(step_idx + idx) % 4]
                nx, ny, nz = cur[0] + dx, cur[1] - 1, cur[2] + dz
                if ny <= 6 or any(bare(_observed_block(ex, (nx, ny + dy, nz)).get("name")) in _LAVA for dy in range(-1, 4)):
                    continue
                try:
                    yield from mine_step(ex, state, [nx, ny, nz], allowed_picks=_DIAMOND_PICKS, solid=_NETHER_SOLID)
                    break
                except MineStepUnsafe:
                    continue
            else:
                break
        if state.count("ancient_debris") < target_count:
            raise EnvBlocked("未发现暴露的远古残骸", missing_predicate="ancient_debris", searched_volume="半径 16 格", wake_on="chunk_moved")
    def verify_done(self, state: State, before_counts=None) -> str:
        need, have = self.args["count"], state.count("ancient_debris")
        return "" if have >= need else f"远古残骸数量不足（现有 {have}，目标 {need}）"

@register
class HuntBlaze(Skill):
    name, goal, verified = "hunt_blaze", "nether:blaze_rod", "offline"
    description = "在下界猎杀烈焰人并收集烈焰棒，远程优先与血量止损"
    params = {"count": Param(int, "目标烈焰棒数", default=1, min=1, max=8), "radius": Param(int, "搜索半径", default=32, min=8, max=64)}
    @classmethod
    def blocked(cls, state: State) -> str:
        return _nether_gate(state, min_hp=10.0)
    def run(self, ex: Executor, state: State):
        cnt = self.args["count"]
        yield from _hunt_mob_for_drop(ex, state, mob="blaze", drop="blaze_rod", target_count=cnt, radius=self.args["radius"], max_kills=max(8, cnt * 6), allow_ranged=True, missing_msg="附近未发现烈焰人", fail_msg=f"烈焰棒收集未达到目标数量 {cnt}", landmark="nether_fortress")
    def verify_done(self, state: State, before_counts=None) -> str:
        need, have = self.args["count"], state.count("blaze_rod")
        return "" if have >= need else f"烈焰棒数量不足（现有 {have}，目标 {need}）"

@register
class LootBastion(Skill):
    name, goal, verified = "loot_bastion", "nether:loot_bastion", "offline"
    description = "穿金甲并规避猪灵蛮兵，搜刮猪灵堡垒箱子战利品"
    params = {"radius": Param(int, "搜箱半径", default=32, min=8, max=64)}
    @classmethod
    def blocked(cls, state: State) -> str:
        return _nether_gate(state)
    @staticmethod
    def _guard_brute(ex: Executor, state: State) -> None:
        scan, brutes = _scan_named(ex, state, "piglin_brute", 10)
        if any(math.dist(scan.get("origin") or state.pos, e["pos"]) <= 10.0 for e in brutes):
            raise Replan("10 格内发现猪灵蛮兵（piglin_brute），停止搜刮")
    def run(self, ex: Executor, state: State):
        self._looted, radius = [], self.args["radius"]
        _nether_status(ex, state, "当前不在下界，停止搜刮堡垒")
        yield from ensure_gold_armor(ex, state)
        self._guard_brute(ex, state)
        blocks = _scan_blocks_ready(ex, state, ("chest", "trapped_chest"), radius=radius, limit=16, skill_name=self.name)
        dim, memo = bare(state.st.get("dimension")) or "the_nether", memo_for(state)
        chests = [c for c in blocks if not memo.is_blocked(c["pos"], dimension=dim)]
        if not chests and (yield from goto_known_landmark_or_biome(ex, state, "bastion_remnant")):
            _nether_status(ex, state, "当前不在下界，停止搜刮堡垒")
            self._guard_brute(ex, state)
            blocks = _scan_blocks_ready(ex, state, ("chest", "trapped_chest"), radius=radius, limit=16, skill_name=self.name)
            chests = [c for c in blocks if not memo.is_blocked(c["pos"], dimension=dim)]
        if not chests:
            raise EnvBlocked("附近未发现可搜刮的堡垒箱子", missing_predicate="bastion_chest", searched_volume=f"半径 {radius} 格", wake_on="chunk_moved")
        rank = lambda e: (0 if any(bare(_observed_block(ex, (_cell(e["pos"])[0] + dx, _cell(e["pos"])[1] + dy, _cell(e["pos"])[2] + dz)).get("name")) in _BASTION_BLOCKS for dx, dy, dz in _OFFSETS_6) else 1, math.dist(state.pos, e["pos"]))
        for chest in sorted(chests, key=rank):
            self._guard_brute(ex, state)
            cpos = _cell(chest["pos"])
            self._looted.extend((yield from loot_container_items(ex, state, cpos, wanted=BASTION_LOOT)))
            memo.mark_depleted(cpos, dimension=dim)
        if not self._looted:
            raise Replan("堡垒箱子中未获取到目标战利品")
    def verify_done(self, state: State, before_counts=None) -> str:
        ok = getattr(self, "_looted", []) or (before_counts and any(state.count(i) > before_counts.get(i, 0) for i in BASTION_LOOT))
        return "" if ok else "未从堡垒箱子取得任何战利品"

@register
class ChargeRespawnAnchor(Skill):
    name, goal, verified = "charge_respawn_anchor", "nether:respawn_anchor", "offline"
    description = "仅在下界制作/放置并用萤石充能重生锚（严禁非下界操作）"
    params = {"charges": Param(int, "充能次数", default=4, min=1, max=4)}
    @classmethod
    def blocked(cls, state: State) -> str:
        return _nether_gate(state)
    def run(self, ex: Executor, state: State):
        self._charges_done, target_charges = 0, self.args["charges"]
        _nether_status(ex, state, "严禁在非下界维度操作重生锚（防爆炸）")
        _fresh_bag(ex, state)
        if anchors := _scan_blocks_ready(ex, state, "respawn_anchor", radius=16, limit=4, skill_name=self.name):
            anchor_pos = _cell(anchors[0]["pos"])
            self._charges_done = min(int((anchors[0].get("properties") or {}).get("charges", 0) or 0), target_charges)
        else:
            if state.count("respawn_anchor") < 1:
                if state.count("crying_obsidian") < 6 or state.count("glowstone") < 3:
                    raise Replan("合成重生锚缺少哭泣的黑曜石（需 6）或萤石（需 3）")
                try:
                    yield from _table_position(ex, state)
                except EnvBlocked:
                    if state.count("crafting_table") < 1 or (tpos := _find_place_cell(ex, state.refresh_status(ex.client, max_age_s=0))) is None:
                        raise
                    yield from place_block(ex, state, "crafting_table", tpos)
                before_cnt = state.count("respawn_anchor")
                yield rec("craft", ex.craft("respawn_anchor", 1), item="respawn_anchor", count=1)
                if _fresh_bag(ex, state).count("respawn_anchor") <= before_cnt:
                    raise Replan("合成重生锚后背包未确认增加")
            if (place_cell := _find_place_cell(ex, state.refresh_status(ex.client, max_age_s=0))) is None:
                raise Replan("未找到可放置重生锚的安全空位")
            yield from place_block(ex, state, "respawn_anchor", place_cell)
            anchor_pos = tuple(place_cell)
        if eye_to_block_dist(state.refresh_status(ex.client, max_age_s=0), anchor_pos) > STATION_EYE_REACH:
            yield from _move_to_cell(ex, state, anchor_pos, rng=2)
        while self._charges_done < target_charges:
            _nether_status(ex, state, "已离开下界，立即中止重生锚充能（防爆炸）")
            if _fresh_bag(ex, state).count("glowstone") < 1:
                raise Replan("背包缺少萤石（glowstone），停止充能（严禁空手右键）")
            yield from use_item_on(ex, state, anchor_pos, "glowstone")
            self._charges_done += 1
    def verify_done(self, state: State, before_counts=None) -> str:
        if not _in_nether(state.st):
            return "当前不在下界（the_nether），重生锚状态不安全"
        done, need = getattr(self, "_charges_done", 0), self.args["charges"]
        return "" if done >= need else f"重生锚充能次数不足（已完成 {done}/{need}）"

@register
class DistractPiglin(Skill):
    name, goal, verified = "distract_piglin", "nether:piglin_{mode}", "offline"
    description = "向普通猪灵投掷金锭引开注意或交易换取哭泣黑曜石"
    params = {"mode": Param(str, "猪灵交互模式", default="distract", choices=("distract", "barter_crying_obsidian")), "radius": Param(int, "找猪灵半径", default=24, min=8, max=48)}
    @classmethod
    def blocked(cls, state: State) -> str:
        return _nether_gate(state) or ("" if state.count("gold_ingot") >= 1 else "背包缺少金锭（gold_ingot）")
    def run(self, ex: Executor, state: State):
        self._dropped_gold, mode, radius = False, self.args["mode"], self.args["radius"]
        _nether_status(ex, state, "当前不在下界，停止向猪灵投金")
        if _fresh_bag(ex, state).count("gold_ingot") < 1:
            raise Replan("背包缺少金锭（gold_ingot）")
        self._crying_before = state.count("crying_obsidian")
        _, piglins = _scan_named(ex, state, "piglin", radius)
        if not piglins:
            raise EnvBlocked("附近未发现普通猪灵", missing_predicate="piglin", searched_volume=f"半径 {radius} 格", wake_on="chunk_moved")
        st = state.snapshot()
        target = min(piglins, key=lambda e: math.dist(st["pos"], e["pos"]))
        dist = math.dist(st["pos"], target["pos"])
        if dist > 8.0:
            yield from _move_to_cell(ex, state, target["pos"], rng=5)
        elif dist < 3.5:
            dx, dz = st["pos"][0] - target["pos"][0], st["pos"][2] - target["pos"][2]
            norm, (cx, cy, cz) = math.hypot(dx, dz) or 1.0, _cell(st["pos"])
            back = (cx + round(2 * dx / norm), cy, cz + round(2 * dz / norm))
            if back != (cx, cy, cz) and _standable_cell(ex, back):
                yield from move_local(ex, state, [back[0] + 0.5, float(back[1]), back[2] + 0.5], rng=1, max_distance=6)
        yield from drop_item(ex, state, "gold_ingot", 1)
        self._dropped_gold = True
        if mode == "barter_crying_obsidian":
            yield from _collect_drop_if_seen(ex, state, "crying_obsidian", radius=8)
            if _fresh_bag(ex, state).count("crying_obsidian") <= self._crying_before:
                raise Replan("投掷金锭后尚未获得哭泣的黑曜石")
    def verify_done(self, state: State, before_counts=None) -> str:
        if self.args["mode"] == "barter_crying_obsidian":
            before = getattr(self, "_crying_before", (before_counts or {}).get("crying_obsidian", 0))
            return "" if state.count("crying_obsidian") > before else "未确认获得新的哭泣黑曜石"
        return "" if getattr(self, "_dropped_gold", False) else "未确认投出金锭引开猪灵"

@register
class HuntWitherSkull(Skill):
    name, goal, verified = "hunt_wither_skull", "nether:wither_skull", "offline"
    description = "在下界击杀凋灵骷髅获取头颅，带止损和血量保护"
    params = {"radius": Param(int, "搜索半径", default=32, min=8, max=64), "max_kills": Param(int, "止损最大击杀数", default=6, min=1, max=20)}
    @classmethod
    def blocked(cls, state: State) -> str:
        return _nether_gate(state, min_hp=10.0)
    def run(self, ex: Executor, state: State):
        k = self.args["max_kills"]
        yield from _hunt_mob_for_drop(ex, state, mob="wither_skeleton", drop="wither_skeleton_skull", target_count=1, radius=self.args["radius"], max_kills=k, allow_ranged=False, missing_msg="附近未发现凋灵骷髅", fail_msg=f"已击杀 {k} 只凋灵骷髅仍未掉落头颅，止损退出", landmark="nether_fortress")
    def verify_done(self, state: State, before_counts=None) -> str:
        return "" if state.count("wither_skeleton_skull") >= 1 else "未获取到凋灵骷髅头颅（wither_skeleton_skull）"
