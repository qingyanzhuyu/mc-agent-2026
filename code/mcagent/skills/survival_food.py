"""地表食物补给技能：白天狩猎、烤肉熟化与干草块/甜浆果兜底。"""
from __future__ import annotations

import math
import time
from typing import Optional

from ..client import ApiError
from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, failure, rec
from .field import _drop_name, _standable_cell, collect_drops, place_block, scan_blocks, scan_entities
from .food_forage import forage_surface_stride
from .furnace import collect_furnace, load_furnace
from .furnace_lease import (
    LOG_TO_PLANK_FUEL, SMELTING_STATIONS, best_fuel_for, collect_leased, ensure_plank_fuel,
    lease_book_for, smelt_output_for, smelt_seconds_per_item,
)
from .items import bare
from .recipes import RECIPES, craft_recipe
from .registry import register
from .sightline import (
    DIG_EYE_REACH, ENTITY_EYE_REACH, STATION_EYE_REACH,
    can_reach_block, eye_to_block_dist, eye_to_point_dist, point3,
)
from .survival import _approach, _cell, _neighbors, _on_ground, _stand_for
from .windows import put_window_item, take_container_item, window_amount, window_section, window_view
from .workstations import reclaim_field_station
from .world_memo import memo_for

FOOD_RESTORE: dict[str, int] = {
    "cooked_beef": 8, "cooked_porkchop": 8, "cooked_mutton": 6, "cooked_chicken": 6,
    "cooked_salmon": 6, "cooked_cod": 5, "cooked_rabbit": 5, "golden_carrot": 6,
    "bread": 5, "baked_potato": 5, "pumpkin_pie": 8, "apple": 4, "golden_apple": 4,
    "enchanted_golden_apple": 4, "carrot": 3, "beef": 3, "porkchop": 3, "rabbit": 3,
    "mutton": 2, "chicken": 2, "cod": 2, "salmon": 2, "sweet_berries": 2,
    "glow_berries": 2, "melon_slice": 2, "cookie": 2, "potato": 1, "beetroot": 1,
    "dried_kelp": 1, "mushroom_stew": 6, "beetroot_soup": 6, "rabbit_stew": 10,
}
COOKED_FOODS: frozenset[str] = frozenset({
    "cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_chicken", "cooked_salmon",
    "cooked_cod", "cooked_rabbit", "bread", "baked_potato", "golden_carrot", "pumpkin_pie",
    "golden_apple", "enchanted_golden_apple", "mushroom_stew", "beetroot_soup", "rabbit_stew", "apple",
})
RAW_MEAT_TO_COOKED: dict[str, str] = {
    "beef": "cooked_beef", "porkchop": "cooked_porkchop", "mutton": "cooked_mutton",
    "chicken": "cooked_chicken", "rabbit": "cooked_rabbit", "cod": "cooked_cod", "salmon": "cooked_salmon",
}
PASSIVE_FOOD_MOBS: tuple[str, ...] = ("cow", "pig", "sheep", "chicken", "rabbit")
_WEAPONS = (
    "netherite_sword", "diamond_sword", "iron_sword", "stone_sword", "wooden_sword",
    "netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "wooden_axe",
)
_WOOLS = tuple(f"{c}_wool" for c in ("white", "orange", "magenta", "light_blue", "yellow", "lime", "pink",
    "gray", "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black"))
_HUNT_DROPS = frozenset(RAW_MEAT_TO_COOKED) | frozenset(_WOOLS) | {"leather"}
_SKIP_ERRS = frozenset({"not_found", "out_of_reach", "path_not_found", "unreachable", "timeout", "move_timeout", "stalled"})
COOK_POLL_SLEEP_S: float = 1.5
_MAX_MEMO_STATION_DIST: float = 32.0
_HUNT_ATTACK_TIMEOUT_MS: int = 20000
_HUNT_MAX_ROUNDS: int = 4
TWO_DAY_FOOD_RESTORE: int = 32
FOOD_RESERVE_LOW_WATER: int = 14
MAX_HUNT_ANIMALS_PER_RUN: int = 3


def inventory_food_restore(state) -> int:
    return sum(state.count(item) * pts for item, pts in FOOD_RESTORE.items())
def cooked_food_count(state) -> int:
    return sum(state.count(item) for item in COOKED_FOODS)
def cooked_food_restore(state) -> int:
    """背包中已熟/即食食物的总饱食回复量（不含生肉）。"""
    return sum(state.count(item) * FOOD_RESTORE.get(item, 0) for item in COOKED_FOODS)
def raw_meat_count(state) -> int:
    return sum(state.count(item) for item in RAW_MEAT_TO_COOKED)
def _state_dim(state) -> str:
    st = state.snapshot() if hasattr(state, "snapshot") else (getattr(state, "st", None) or {})
    return bare((st.get("dimension") if isinstance(st, dict) else None) or "overworld") or "overworld"

def leased_food_restore(state) -> int:
    """当前维度活动熔炉租约中正在烤制或待取回的熟食总回复量。"""
    dim = _state_dim(state)
    total = 0
    for lease in lease_book_for(state).active_leases(dimension=dim):
        out = bare(getattr(lease, "expected_output", "") or "")
        if out in COOKED_FOODS or getattr(lease, "consumer", "") == "gather_food":
            pts = FOOD_RESTORE.get(out, 6)
            cnt = max(0, int(getattr(lease, "count", 0) or 0))
            total += cnt * pts
    return total

def effective_food_restore(state) -> int:
    """有效食物底仓（点）：背包即食 + 生肉按烤熟后折算 + 当前维度熔炉在烤熟食租约。"""
    total = 0
    for item, pts in FOOD_RESTORE.items():
        cnt = state.count(item)
        if cnt <= 0:
            continue
        if item in RAW_MEAT_TO_COOKED:
            total += cnt * FOOD_RESTORE.get(RAW_MEAT_TO_COOKED[item], pts)
        else:
            total += cnt * pts
    return total + leased_food_restore(state)

def has_ready_food_lease(state) -> bool:
    """检查当前维度是否存在已到时可取回的熟食熔炉租约。"""
    dim = _state_dim(state)
    return any(l.expected_output in COOKED_FOODS or l.consumer == "gather_food" for l in lease_book_for(state).ready_leases(dimension=dim))

def has_cookable_food(state) -> bool:
    """检查当前是否持有生肉且有可用燃料（含可拆板的原木），或已有待取回熟食租约。"""
    has_fuel = (
        best_fuel_for(state, 1, allow_lava_bucket=False) is not None
        or any(state.count(lg) >= 1 for lg in LOG_TO_PLANK_FUEL)
    )
    return (raw_meat_count(state) > 0 and has_fuel) or has_ready_food_lease(state)

def _is_night(st: dict) -> bool:
    tod = st.get("time_of_day")
    return st.get("is_day") is False or (isinstance(tod, (int, float)) and not isinstance(tod, bool) and 12500 <= int(tod) % 24000 < 23459)

def _refresh_inv(ex, state) -> dict:
    state.invalidate_inventory()
    return state.refresh_inventory(ex.client)


def _collect_visible_drops(ex, state, targets: frozenset[str]):
    scan = scan_entities(ex, state, "item", radius=8)
    if scan.get("ready") is False:
        return
    for name in sorted({n for e in (scan.get("entities") or ()) if (n := _drop_name(e)) in targets}):
        try:
            yield from collect_drops(ex, state, name, count=1, radius=8, max_targets=2)
        except (EnvBlocked, Replan):
            pass


def _try_act(op: str, fn, **fields):
    try:
        res = fn()
    except Replan as exc:
        if (code := failure(exc)[0]) not in _SKIP_ERRS:
            raise
        yield rec(op, **fields, error_code=code)
        return False
    yield rec(op, res, **fields)
    return True


def _move_bounded(ex, *, timeout: float = 20.0, **kwargs):
    try:
        return ex.move_to(**kwargs, timeout=timeout, retries=0)
    except TypeError:
        return ex.move_to(**kwargs)

def _reach_block(ex, state, pos, reach: float):
    st = state.refresh_status(ex.client, max_age_s=0)
    if can_reach_block(ex, st, pos, reach=reach):
        return True
    stand = _stand_for(ex, st, reach=reach, block_pos=pos) if _on_ground(st) else None
    if stand is not None:
        try:
            yield from _approach(ex, state, st, stand, reach=reach, block_pos=pos)
            return True
        except Replan:
            pass
    if not (yield from _try_act("move_to", lambda: _move_bounded(ex, pos=list(pos), rng=2, timeout=20.0), pos=list(pos))):
        return False
    st = state.refresh_status(ex.client, max_age_s=0)
    return can_reach_block(ex, st, pos, reach=reach) or eye_to_block_dist(st, pos) <= reach + 1.0

def _find_or_place_stations(
    ex, state, memo_kind: str, allowed: tuple[str, ...], skill_name: str, *, newly_placed: Optional[set[tuple[int, int, int]]] = None,
):
    st = state.refresh_status(ex.client, max_age_s=0)
    dim, memo = bare(st.get("dimension") or "overworld"), memo_for(state)
    found: dict[tuple[int, int, int], str] = {}
    for cell in memo.stations(memo_kind, dimension=dim):
        if st.get("pos") and eye_to_block_dist(st, cell) > _MAX_MEMO_STATION_DIST:
            continue
        try:
            kind = bare(ex.client.get_block(list(cell)).get("name"))
        except ApiError as exc:
            if exc.code in ("not_loaded", "not_ready"):
                continue
            raise
        if kind in allowed:
            found[tuple(cell)] = kind
        else:
            memo.remove_station(memo_kind, cell, dimension=dim)
    if not found:
        scan = scan_blocks(ex, state, allowed, radius=16, limit=4, skill_name=skill_name)
        if scan.get("ready") is False:
            raise Replan("世界区块加载尚未就绪（not_ready），暂无法扫描工作站")
        for blk in scan.get("blocks") or ():
            if (kind := bare(blk.get("name"))) in allowed and blk.get("pos"):
                cell = _cell(blk["pos"])
                memo.record_station(kind, cell, dimension=dim)
                found[cell] = kind
    if not found and st.get("pos") and (item := next((n for n in allowed if state.count(n) > 0), None)):
        for cand in _neighbors(_cell(st["pos"])):
            if _standable_cell(ex, cand):
                try:
                    yield from place_block(ex, state, item, cand)
                except Replan as exc:
                    code, _ = failure(exc)
                    yield rec("place", item=item, pos=list(cand), error_code=code)
                    continue
                memo.record_station(item, cand, dimension=dim)
                found[cand] = item
                if newly_placed is not None:
                    newly_placed.add(tuple(cand))
                break
    st = state.refresh_status(ex.client, max_age_s=0)
    order = {name: idx for idx, name in enumerate(allowed)}
    return sorted(((k, p) for p, k in found.items()), key=lambda r: (order.get(r[0], 9), eye_to_block_dist(st, r[1])))


def _take_output(ex, state, target: dict, win: str, item: str, count: int, view: dict):
    if win == "furnace":
        return (yield from collect_furnace(ex, state, target, item=item, count=count, view=view, inspect_after=True))
    return (yield from take_container_item(ex, state, target, "output", item, count, view=view))


@register
class GatherFood(Skill):
    name = "gather_food"
    description = "白天补粮至两天量(32点)或烤熟生肉，足量后勿重复调用"
    params = {
        "target_restore": Param(int, "目标食物总回复量（32点≈两天）", default=TWO_DAY_FOOD_RESTORE, min=4, max=64),
        "cook": Param(bool, "是否尽量烤熟生肉", default=True),
        "hunt": Param(bool, "不足时是否外出狩猎或采食（仅烤肉/取炉时设为False）", default=True),
    }
    goal = "survival:gather_food"
    verified = "live"
    cooldown_s = 15.0

    def __init__(self, **args):
        super().__init__(**args)
        self._before_restore, self._before_effective, self._before_cooked, self._loaded_furnace = 0, 0, 0, False

    def _current_restore(self, state) -> int:
        return effective_food_restore(state) if self.args["cook"] else inventory_food_restore(state)

    def can_start(self, state) -> bool:
        if not self.args["hunt"]:
            return bool(self.args["cook"] and has_cookable_food(state))
        target = self.args["target_restore"]
        if self._current_restore(state) < target:
            return True
        if self.args["cook"] and (
            has_ready_food_lease(state)
            or (has_cookable_food(state) and cooked_food_restore(state) < target)
        ):
            return True
        return False

    @classmethod
    def blocked(cls, state) -> str:
        st = state.snapshot()
        if st.get("ready") is False:
            return "世界正在加载区块和实体（ready=false），请稍候"
        if st.get("alive") is False:
            return "角色尚未存活，等待重生"
        if bare(st.get("dimension") or "overworld") != "overworld":
            return "仅在主世界执行地表补粮"
        has_cookable = has_cookable_food(state)
        if _is_night(st) and not has_cookable:
            return "夜晚停止地表狩猎补粮"
        has_weapon = any(state.count(w) > 0 for w in _WEAPONS)
        if not has_weapon and not has_cookable and float(st.get("food", 20)) >= 18 and float(st.get("health", 20)) >= 18:
            return "空手且满状态时先备武器或待饥饿/受伤再补粮"
        return ""

    def _made_progress(self, state) -> bool:
        return (
            inventory_food_restore(state) > self._before_restore
            or effective_food_restore(state) > self._before_effective
            or cooked_food_count(state) > self._before_cooked
            or self._loaded_furnace
        )

    def _collect_food_leases(self, ex, state, *, ready_only: bool = True):
        dim = _state_dim(state)
        book = lease_book_for(state)
        leases = book.ready_leases(dimension=dim) if ready_only else book.active_leases(dimension=dim)
        collected_any = False
        for lease in [l for l in leases if l.expected_output in COOKED_FOODS or l.consumer == "gather_food"]:
            if not (yield from _reach_block(ex, state, lease.pos, STATION_EYE_REACH)):
                continue
            try:
                got = yield from collect_leased(ex, state, lease)
            except Replan:
                continue
            _refresh_inv(ex, state)
            if got > 0:
                collected_any = True
        return collected_any

    def _cook_raw_meat(self, ex, state):
        if not self.args["cook"]:
            return False
        collected_lease = yield from self._collect_food_leases(ex, state, ready_only=True)
        if raw_meat_count(state) <= 0:
            return collected_lease
        raw_item = max(RAW_MEAT_TO_COOKED, key=state.count)
        raw_qty, cooked_item = state.count(raw_item), smelt_output_for(raw_item, RAW_MEAT_TO_COOKED[raw_item])
        if (
            best_fuel_for(state, 1, allow_lava_bucket=False) is None
            and not any(state.count(lg) >= 1 for lg in LOG_TO_PLANK_FUEL)
        ):
            return collected_lease
        dim, book = _state_dim(state), lease_book_for(state)
        newly_placed_furnaces: set[tuple[int, int, int]] = set()
        for _, pos in (yield from _find_or_place_stations(ex, state, "furnace", ("smoker", "furnace"), self.name, newly_placed=newly_placed_furnaces)):
            if not (yield from _reach_block(ex, state, pos, STATION_EYE_REACH)):
                continue
            target = {"pos": list(pos)}
            view = yield from window_view(ex, target)
            win = bare(view.get("window"))
            if win not in ("furnace", "smoker") or win not in SMELTING_STATIONS:
                continue
            used = False
            if (out := window_amount(view, "output", cooked_item)) > 0:
                view = (yield from _take_output(ex, state, target, win, cooked_item, out, view))["view"]
                _refresh_inv(ex, state)
                used = True
            if any(bare(s.get("name")) != cooked_item for s in window_section(view, "output")):
                continue
            qty = min(raw_qty, state.count(raw_item))
            if not window_section(view, "input") and qty > 0:
                has_loaded_fuel = bool(window_section(view, "fuel"))
                fuel_item, fuel_count = "", 0
                if not has_loaded_fuel:
                    # 尽量先将原木合成木板再作为燃料，避免直烧原木
                    yield from ensure_plank_fuel(ex, state, qty, allow_lava_bucket=False)
                    pair = next(((q, f) for q in range(qty, 0, -1) if (f := best_fuel_for(state, q, allow_lava_bucket=False)) is not None), None)
                    if pair is None:
                        continue
                    qty, (fuel_item, fuel_count) = pair
                if win == "furnace" and not has_loaded_fuel:
                    yield from load_furnace(ex, state, target, raw_item=raw_item, raw_count=qty,
                                            fuel_item=fuel_item, fuel_count=fuel_count, view=view, output_item=cooked_item)
                elif win == "furnace":
                    yield from put_window_item(ex, state, target, raw_item, qty, "input", verify=lambda *_: True)
                else:
                    res = yield from put_window_item(ex, state, target, raw_item, qty, "input", verify=lambda *_: True)
                    if not window_section(res["view"], "fuel"):
                        yield from put_window_item(ex, state, target, fuel_item, fuel_count, "fuel", verify=lambda *_: True)
                self._loaded_furnace = used = True
                book.register_lease(pos, dimension=dim, station_kind=win, input_item=raw_item,
                                    expected_output=cooked_item, count=qty, consumer=self.name)
            elif not used:
                continue
            sec = smelt_seconds_per_item(win)
            deadline = time.monotonic() + min(22.0, sec * qty + 2.0)
            max_polls = min(10, max(4, int(sec * min(qty, 2) / max(0.5, COOK_POLL_SLEEP_S)) + 2))
            for poll in range(max_polls):
                if poll:
                    time.sleep(COOK_POLL_SLEEP_S)
                view = yield from window_view(ex, target)
                if (out_amt := window_amount(view, "output", cooked_item)) > 0:
                    view = (yield from _take_output(ex, state, target, win, cooked_item, out_amt, view))["view"]
                    _refresh_inv(ex, state)
                if not window_section(view, "input") and window_amount(view, "output", cooked_item) <= 0:
                    book.complete_lease(pos, dimension=dim)
                    break
                if time.monotonic() >= deadline:
                    break
            furnace_emptied = (
                not window_section(view, "input")
                and not window_section(view, "output")
                and not window_section(view, "fuel")
            )
            if tuple(pos) in newly_placed_furnaces and furnace_emptied:
                yield from reclaim_field_station(
                    ex, state, win, pos, dimension=dim, newly_placed=True, furnace_emptied=True,
                )
            return True
        if _is_night(state.refresh_status(ex.client, max_age_s=0)) and not self._made_progress(state):
            raise EnvBlocked("附近未找到可用熔炉或烟熏炉以烤熟生肉", missing_predicate="station_ready:furnace",
                             searched_volume="半径 16 格熔炉或烟熏炉", wake_on="chunk_moved")
        return collected_lease

    @staticmethod
    def _observe_prey(ex, eid: int, *, radius: int = 48) -> tuple[Optional[dict], bool]:
        """Re-observe target passive entity; returns `(entity_or_none, near_hostile)`."""
        try:
            entities = ex.client.get_entities(radius=radius).get("entities") or []
        except ApiError as exc:
            if exc.code == "not_ready":
                return (None, False)
            raise
        target = next((e for e in entities if e.get("id") == eid), None)
        if target is None or target.get("alive") is False:
            return (None, False)
        hp = target.get("health")
        if isinstance(hp, (int, float)) and not isinstance(hp, bool) and hp <= 0:
            return (None, False)
        if not target.get("pos"):
            return (None, False)
        hostiles = [e for e in entities if e.get("type") == "hostile" and e.get("alive") is not False and e.get("pos")]
        try:
            near_hostile = any(math.dist(point3(target["pos"]), point3(h["pos"])) <= 8.0 for h in hostiles)
        except Replan:
            near_hostile = False
        return (target, near_hostile)

    def _hunt_single_animal(self, ex, state, ent: dict):
        """Chase and attack one passive animal until confirmed dead, lost, or hostile-blocked."""
        inv = _refresh_inv(ex, state)
        if (w := next((x for x in _WEAPONS if state.count(x) > 0), None)) and bare((inv.get("held") or {}).get("name")) != w:
            yield rec("equip", ex.equip(w, "hand"), item=w, slot="hand")
            _refresh_inv(ex, state)
        eid, cur_pos = ent["id"], list(ent["pos"])
        killed, ever_hit = False, False
        for round_idx in range(_HUNT_MAX_ROUNDS):
            if round_idx > 0:
                fresh, near_hostile = self._observe_prey(ex, eid, radius=48)
                if fresh is None:
                    killed = True
                    break
                if near_hostile:
                    break
                cur_pos = list(fresh["pos"])
            st = state.refresh_status(ex.client, max_age_s=0)
            if eye_to_point_dist(st, cur_pos) > ENTITY_EYE_REACH:
                if not (yield from _try_act(
                    "move_to",
                    lambda p=cur_pos: _move_bounded(ex, entity_id=eid, pos=p, rng=2, timeout=18.0),
                    entity_id=eid, pos=cur_pos,
                )):
                    break
                st = state.refresh_status(ex.client, max_age_s=0)
                if eye_to_point_dist(st, cur_pos) > 16.0:
                    fresh_after, near_hostile_after = self._observe_prey(ex, eid, radius=48)
                    if fresh_after is None:
                        killed = ever_hit
                        break
                    if near_hostile_after:
                        break
                    cur_pos = list(fresh_after["pos"])
                    if eye_to_point_dist(st, cur_pos) > 16.0:
                        break
            state.foraging_passive = True
            atk_res, atk_err = None, None
            try:
                # 不设 max_hits 上限，遵循 §4.6 持续追击直至目标死亡或超时，并在未击杀时补追
                atk_res = ex.attack(eid, timeout_ms=_HUNT_ATTACK_TIMEOUT_MS)
            except Replan as exc:
                code = failure(exc)[0]
                if code not in _SKIP_ERRS and code != "no_such_entity":
                    raise
                atk_err = code
            finally:
                state.foraging_passive = False
            if atk_err is not None:
                yield rec("attack", entity_id=eid, error_code=atk_err)
                if atk_err == "no_such_entity" and ever_hit:
                    killed = True
                break
            yield rec("attack", atk_res, entity_id=eid)
            hits = int(atk_res.get("hits") or 0) if isinstance(atk_res, dict) else 0
            if hits > 0:
                ever_hit = True
            if isinstance(atk_res, dict) and atk_res.get("killed") is True:
                killed = True
                break
            if hits <= 0:
                break
            if round_idx == _HUNT_MAX_ROUNDS - 1:
                fresh_end, _ = self._observe_prey(ex, eid, radius=48)
                if fresh_end is None:
                    killed = True
        return killed

    def _hunt_animals(self, ex, state, target_restore: int):
        try:
            entities = ex.client.get_entities(radius=48).get("entities") or []
        except ApiError as exc:
            if exc.code == "not_ready":
                raise Replan("世界实体加载尚未就绪（not_ready），请稍后再试") from exc
            raise
        hostiles = [e for e in entities if e.get("type") == "hostile" and e.get("alive") is not False and e.get("pos")]
        prey = []
        for ent in entities:
            if (bare(ent.get("name")) not in PASSIVE_FOOD_MOBS or ent.get("alive") is False
                    or ent.get("baby") is True or ent.get("is_baby") is True
                    or type(ent.get("id")) is not int or not ent.get("pos")):
                continue
            try:
                if not any(math.dist(point3(ent["pos"]), point3(h["pos"])) <= 8.0 for h in hostiles):
                    prey.append(ent)
            except Replan:
                continue
        st, need_wool = state.refresh_status(ex.client, max_age_s=0), sum(state.count(w) for w in _WOOLS) < 3
        prey.sort(key=lambda e: (0 if need_wool and bare(e.get("name")) == "sheep" else 1, eye_to_point_dist(st, e["pos"])))
        hunted = 0
        for ent in prey:
            if hunted >= MAX_HUNT_ANIMALS_PER_RUN or self._current_restore(state) >= target_restore:
                break
            killed = yield from self._hunt_single_animal(ex, state, ent)
            if not killed:
                continue
            yield from _collect_visible_drops(ex, state, _HUNT_DROPS)
            _refresh_inv(ex, state)
            hunted += 1
        return hunted

    def _gather_hay_and_berries(self, ex, state, target_restore: int):
        scan = scan_blocks(ex, state, ("hay_block", "sweet_berry_bush"), radius=32, limit=8, skill_name=self.name)
        if scan.get("ready") is False:
            raise Replan("世界区块加载尚未就绪（not_ready），暂无法扫描干草块或浆果丛")
        st = state.refresh_status(ex.client, max_age_s=0)
        for blk in sorted(scan.get("blocks") or (), key=lambda b: eye_to_block_dist(st, b["pos"])):
            if self._current_restore(state) >= target_restore:
                break
            pos, bname = tuple(blk["pos"]), bare(blk.get("name"))
            if bname == "hay_block":
                if not (yield from _reach_block(ex, state, pos, DIG_EYE_REACH)):
                    continue
                yield rec("dig", ex.dig(list(pos), collect=True), pos=list(pos)); _refresh_inv(ex, state)
                yield from _collect_visible_drops(ex, state, frozenset({"hay_block"})); _refresh_inv(ex, state)
                if state.count("hay_block") > 0:
                    if "wheat" in RECIPES:
                        yield from craft_recipe(ex, state, "wheat", goal_qty=state.count("wheat") + 9)
                    else:
                        yield rec("craft", ex.craft("wheat", 1), item="wheat", count=1); _refresh_inv(ex, state)
                if state.count("wheat") >= 3 and self._current_restore(state) < target_restore:
                    table_ok = "bread" in RECIPES
                    placed_table_pos = None
                    if not table_ok:
                        newly_placed_tables: set[tuple[int, int, int]] = set()
                        for _, tpos in (yield from _find_or_place_stations(ex, state, "crafting_table", ("crafting_table",), self.name, newly_placed=newly_placed_tables)):
                            if (yield from _reach_block(ex, state, tpos, STATION_EYE_REACH)):
                                table_ok = True
                                if tuple(tpos) in newly_placed_tables:
                                    placed_table_pos = tuple(tpos)
                                break
                    for _ in range(6):
                        if not table_ok or state.count("wheat") < 3 or self._current_restore(state) >= target_restore:
                            break
                        if "bread" in RECIPES:
                            yield from craft_recipe(ex, state, "bread", goal_qty=state.count("bread") + 1)
                        else:
                            yield rec("craft", ex.craft("bread", 1), item="bread", count=1); _refresh_inv(ex, state)
                    if placed_table_pos is not None:
                        yield from reclaim_field_station(ex, state, "crafting_table", placed_table_pos, dimension=_state_dim(state), newly_placed=True)
            elif bname == "sweet_berry_bush" and (yield from _reach_block(ex, state, pos, STATION_EYE_REACH)):
                yield rec("interact", ex.interact(pos=list(pos)), pos=list(pos))
                yield from _collect_visible_drops(ex, state, frozenset({"sweet_berries"})); _refresh_inv(ex, state)

    def run(self, ex, state):
        state.refresh_status(ex.client, max_age_s=0); _refresh_inv(ex, state)
        self._before_restore = inventory_food_restore(state)
        self._before_effective = effective_food_restore(state)
        self._before_cooked, self._loaded_furnace = cooked_food_count(state), False
        yield from self._cook_raw_meat(ex, state)
        if not self.args["hunt"]:
            return
        st, target_restore = state.refresh_status(ex.client, max_age_s=0), self.args["target_restore"]
        if not _is_night(st) and self._current_restore(state) < target_restore:
            for attempt in range(2):
                hunted = yield from self._hunt_animals(ex, state, target_restore)
                if self.args["cook"] and raw_meat_count(state) > 0:
                    yield from self._cook_raw_meat(ex, state)
                if self._current_restore(state) < target_restore and hunted == 0:
                    yield from self._gather_hay_and_berries(ex, state, target_restore)
                if self._made_progress(state) or self._current_restore(state) >= target_restore:
                    break
                if attempt == 0 and (yield from forage_surface_stride(ex, state)):
                    continue
                raise EnvBlocked("半径 48 内未找到被动动物、干草块或甜浆果丛", missing_predicate="food_source",
                                 searched_volume="半径 48 格实体与半径 32 格方块", wake_on="chunk_moved")

    def verify_done(self, state, before_counts=None) -> str:
        return "" if (self._current_restore(state) >= self.args["target_restore"] or self._made_progress(state)) else "补粮结束后食物回复量与熟食均未增加且未装炉烤肉"
