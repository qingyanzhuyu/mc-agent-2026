"""Stage 3 Wave C (3 T4 Skills + 4 Goals + Controlled Totem Trigger Hook) offline unit tests."""
from __future__ import annotations

import math
import re
import time
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, patch

from mcagent.client import ApiError
from mcagent.loop.draft_scores import ADVANCEMENT_SCORES_V2
from mcagent.loop.goals import (
    GOAL_MAP,
    GOALS,
    rank_actionable_goals,
    resolve_active_goals,
    validate_ledger,
)
from mcagent.runtime import (
    EnvBlocked,
    Executor,
    Replan,
    SafetyMonitor,
    State,
    controlled_totem_trigger,
    validate_controlled_totem_conditions,
)
from mcagent.skills import REGISTRY, bind_args, contract_errors
import mcagent.skills.t4_ominous_combat as t4_ominous_combat
from mcagent.skills.t4_ominous_combat import (
    TriggerTotemSafely,
    UnlockOminousVault,
    WinMicroVillageRaid,
    _select_lowest_ominous_bottle,
)
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches
from mcagent.skills.world_memo import memo_for, reset_all_world_memos

WAVE_C_PREP_IDS: tuple[str, ...] = ("prep_micro_village_bunker",)
WAVE_C_SCORED_IDS: tuple[str, ...] = (
    "revaulting",
    "hero_of_the_village",
    "totem_of_undying",
)
WAVE_C_ALL_IDS: tuple[str, ...] = WAVE_C_PREP_IDS + WAVE_C_SCORED_IDS

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")


def _flat(
    x0: int, x1: int, z0: int, z1: int, y: int = 63, block: str = "stone"
) -> dict[tuple[int, int, int], str]:
    return {(x, y, z): block for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)}


def _state_with(
    *,
    ready: bool = True,
    alive: bool = True,
    dimension: str = "overworld",
    difficulty: str = "normal",
    health: float = 20.0,
    food: float = 20.0,
    pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
    on_ground: bool = True,
    armor_points: float = 0.0,
    in_lava: bool = False,
    in_water: bool = False,
    submerged: bool = False,
    on_fire: bool = False,
    oxygen: float = 20.0,
    elevated_column: bool = False,
    items: list[dict[str, Any]] | None = None,
    held: dict[str, Any] | None = None,
    offhand: dict[str, Any] | None = None,
    st_offhand: dict[str, Any] | None = None,
    armor: dict[str, Any] | None = None,
    dirty: bool = False,
    effects: Any = None,
    safe_mode_s: float = 0.0,
) -> State:
    state = State()
    st: dict[str, Any] = {
        "ready": ready,
        "alive": alive,
        "dimension": dimension,
        "difficulty": difficulty,
        "health": health,
        "food": food,
        "pos": list(pos),
        "on_ground": on_ground,
        "armor_points": armor_points,
        "in_lava": in_lava,
        "in_water": in_water,
        "submerged": submerged,
        "on_fire": on_fire,
        "oxygen": oxygen,
        "sneaking": False,
        "blocking": False,
        "jumping": False,
    }
    if elevated_column:
        st["elevated_column"] = True
    if effects is not None:
        st["effects"] = deepcopy(effects)
    if st_offhand is not None:
        st["offhand"] = deepcopy(st_offhand)
    state.set_status(st)

    inv: dict[str, Any] = {"items": deepcopy(list(items or []))}
    if held is not None:
        inv["held"] = deepcopy(held)
    if offhand is not None:
        inv["offhand"] = deepcopy(offhand)
    if armor is not None:
        inv["armor"] = deepcopy(armor)
    state.set_inventory(inv)
    if dirty:
        state.invalidate_inventory()
    if safe_mode_s > 0.0:
        state.safe_mode_until = time.monotonic() + safe_mode_s
    return state


class _BlockProbeClient:
    """Minimal read-only client double for `validate_controlled_totem_conditions`."""

    def __init__(self, blocks: dict[tuple[int, int, int], str] | None = None):
        self.blocks: dict[tuple[int, int, int], str] = {
            tuple(k): v for k, v in (blocks or {}).items()
        }
        self.calls: list[tuple[list[int], bool]] = []

    def get_block(self, pos: list[int], use_cache: bool = True) -> dict[str, Any]:
        self.calls.append((list(pos), use_cache))
        return {"name": self.blocks.get(tuple(pos), "air")}

    def stop(self) -> dict[str, Any]:
        return {"stopped": True, "released": True}


class WaveCWorld(GridWorld):
    """Offline deterministic world double supporting all Stage 3 Wave C skills."""

    def __init__(
        self,
        pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
        *,
        dimension: str = "overworld",
        difficulty: str = "normal",
        blocks: dict[tuple[int, int, int], str] | None = None,
        block_props: dict[tuple[int, int, int], dict[str, Any]] | None = None,
        block_extra: dict[tuple[int, int, int], dict[str, Any]] | None = None,
        items: dict[str, int] | None = None,
        item_stacks: list[dict[str, Any]] | None = None,
        held: dict[str, Any] | None = None,
        offhand: dict[str, Any] | None = None,
        armor: dict[str, dict[str, Any]] | None = None,
        entities: list[dict[str, Any]] | None = None,
        hp: float = 20.0,
        food: float = 20.0,
        effects: Any = None,
        elevated_column: bool = False,
    ):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st.update(
            dimension=dimension,
            difficulty=difficulty,
            health=hp,
            food=food,
            blocking=False,
            jumping=False,
            armor_points=0,
            in_lava=False,
            in_water=False,
            submerged=False,
            on_fire=False,
            oxygen=20,
        )
        if elevated_column:
            self.st["elevated_column"] = True
        if effects is not None:
            self.st["effects"] = deepcopy(effects)
        self.block_props: dict[tuple[int, int, int], dict[str, Any]] = {
            tuple(k): deepcopy(v) for k, v in (block_props or {}).items()
        }
        self.block_extra: dict[tuple[int, int, int], dict[str, Any]] = {
            tuple(k): deepcopy(v) for k, v in (block_extra or {}).items()
        }
        self.item_stacks: list[dict[str, Any]] | None = (
            deepcopy(item_stacks) if item_stacks is not None else None
        )
        self.held: dict[str, Any] | None = deepcopy(held)
        self.offhand: dict[str, Any] | None = deepcopy(offhand)
        self.armor: dict[str, dict[str, Any]] = deepcopy(armor or {})
        self.entities: list[dict[str, Any]] = [deepcopy(e) for e in (entities or ())]
        self.completed_advs: list[str] = []
        self._sync_armor_points()

        self.on_move_to: Callable[..., dict | None] | None = None
        self.on_place: Callable[..., dict | None] | None = None
        self.on_interact: Callable[..., dict | None] | None = None
        self.on_use_item: Callable[..., dict | None] | None = None
        self.on_attack: Callable[..., dict | None] | None = None
        self.on_shoot: Callable[..., dict | None] | None = None
        self.on_look: Callable[..., dict | None] | None = None
        self.on_control: Callable[..., dict | None] | None = None
        self._last_look_entity_id: int | None = None
        self._last_look_pos: list[float] | None = None
        self.on_equip: Callable[..., dict | None] | None = None
        self.on_window_act: Callable[..., dict | None] | None = None

    def _sync_armor_points(self) -> None:
        self.st["armor_points"] = 4 * sum(
            1
            for piece in self.armor.values()
            if isinstance(piece, dict) and piece.get("name") and (piece.get("count", 1) or 0) >= 1
        )

    def _total_item_count(self, item: str) -> int:
        if self.item_stacks is not None:
            return sum(
                int(s.get("count", 0))
                for s in self.item_stacks
                if s.get("name") == item and int(s.get("count", 0)) > 0
            )
        return self.items.get(item, 0)

    def _consume_item(self, item: str, count: int = 1) -> None:
        if self.item_stacks is not None:
            need = count
            for s in self.item_stacks:
                if s.get("name") == item and int(s.get("count", 0)) > 0:
                    take = min(need, int(s["count"]))
                    s["count"] -= take
                    need -= take
                    if need <= 0:
                        break
            if need > 0:
                raise ApiError("missing_item", f"背包没有足够的 {item}")
            self.item_stacks = [s for s in self.item_stacks if int(s.get("count", 0)) > 0]
            return
        self._need(item)
        self.items[item] -= count

    def _add_item(self, item: str, count: int = 1) -> None:
        if self.item_stacks is not None:
            next_slot = max((int(s.get("slot", 0)) for s in self.item_stacks), default=-1) + 1
            self.item_stacks.append({"name": item, "count": count, "slot": next_slot})
            return
        self.items[item] = self.items.get(item, 0) + count

    def status(self, timeout: float = 10) -> dict:
        self._sync_armor_points()
        st = deepcopy(self.st)
        if self.offhand is not None:
            st["offhand"] = deepcopy(self.offhand)
        else:
            st.pop("offhand", None)
        return st

    def inventory(self, timeout: float = 10) -> dict:
        if self.item_stacks is not None:
            rows = [deepcopy(s) for s in self.item_stacks if int(s.get("count", 0)) > 0]
        else:
            rows = [
                {"name": n, "count": c, "slot": i}
                for i, (n, c) in enumerate(self.items.items())
                if c > 0
            ]
        inv: dict[str, Any] = {"items": rows, "armor": deepcopy(self.armor)}
        if self.held is not None:
            inv["held"] = deepcopy(self.held)
        if self.offhand is not None:
            inv["offhand"] = deepcopy(self.offhand)
        return inv

    def get_block(self, pos, use_cache: bool = True) -> dict:
        key = tuple(pos)
        blk: dict[str, Any] = {"name": self.blocks.get(key, "air")}
        if key in self.block_props:
            blk["properties"] = deepcopy(self.block_props[key])
        if key in self.block_extra:
            blk.update(deepcopy(self.block_extra[key]))
        return blk

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None) -> dict:
        self.calls.append(("find_blocks", {"types": types, "radius": radius, "limit": limit}))
        if isinstance(types, str):
            wanted = {types.removeprefix("minecraft:")}
        else:
            wanted = {str(t).removeprefix("minecraft:") for t in (types or ())}
        px, py, pz = self.st["pos"]
        hits = []
        for (bx, by, bz), name in sorted(self.blocks.items()):
            bname = name.removeprefix("minecraft:")
            if wanted and bname not in wanted:
                continue
            d = math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5))
            if d <= radius + 0.5:
                entry: dict[str, Any] = {"name": bname, "pos": [bx, by, bz], "dist": round(d, 2)}
                if (bx, by, bz) in self.block_props:
                    entry["properties"] = deepcopy(self.block_props[(bx, by, bz)])
                if (bx, by, bz) in self.block_extra:
                    entry.update(deepcopy(self.block_extra[(bx, by, bz)]))
                hits.append(entry)
        hits.sort(key=lambda b: b["dist"])
        return {"blocks": hits[:limit]}

    def get_entities(self, radius=32, types=None) -> dict:
        self.calls.append(("get_entities", {"radius": radius, "types": types}))
        if isinstance(types, str):
            wanted = {types.removeprefix("minecraft:")}
        else:
            wanted = {str(t).removeprefix("minecraft:") for t in (types or ())}
        px, py, pz = self.st["pos"]
        out = []
        for ent in self.entities:
            ename = str(ent.get("name", "")).removeprefix("minecraft:")
            etype = str(ent.get("type", "")).removeprefix("minecraft:")
            if wanted and ename not in wanted and etype not in wanted:
                continue
            d = math.dist((px, py, pz), ent["pos"])
            if d <= radius + 0.5:
                row = deepcopy(ent)
                row.setdefault("dist", round(d, 2))
                out.append(row)
        return {"entities": out}

    def equip(self, item, slot="hand", *, stop_on=None) -> dict:
        self.calls.append(("equip", {"item": item, "slot": slot}))
        if self.on_equip is not None:
            res = self.on_equip(self, item=item, slot=slot)
            if res is not None:
                return res
        if not item or slot not in ("hand", "off-hand", "offhand", "head", "torso", "chest", "legs", "feet"):
            raise ApiError("bad_args", "Invalid item or slot")
        if slot == "off-hand":
            if self.offhand and self.offhand.get("name") == item:
                return {"equipped": item, "slot": slot}
            self._consume_item(item, 1)
            self.offhand = {"name": item, "count": 1, "slot": 45}
            return {"equipped": item, "slot": slot}
        if self._total_item_count(item) < 1 and not (self.held and self.held.get("name") == item):
            raise ApiError("missing_item", f"背包没有 {item}")
        self.held = {"name": item, "count": max(1, self._total_item_count(item))}
        return {"equipped": item, "slot": slot}

    def place(self, item, pos=None, *, stop_on=None) -> dict:
        if self.on_place is not None:
            res = self.on_place(self, item=item, pos=list(pos) if pos is not None else None)
            if res is not None:
                self.calls.append(("place", {"item": item, "pos": list(pos) if pos is not None else None}))
                return res
        if self.item_stacks is not None:
            self.calls.append(("place", {"item": item, "pos": list(pos)}))
            self._consume_item(item, 1)
            x, y, z = self.st["pos"]
            if list(pos) in ([math.floor(x), math.floor(y) + dy, math.floor(z)] for dy in (0, 1)):
                raise ApiError("blocked_by_self", "目标格与自身碰撞箱重叠")
            self.blocks[tuple(pos)] = item
            return {"placed": item, "pos": list(pos)}
        return super().place(item, pos=pos, stop_on=stop_on)

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        if self.on_move_to is not None:
            res = self.on_move_to(self, pos=pos, x=x, y=y, z=z, entity_id=entity_id, rng=rng)
            if res is not None:
                self.calls.append(("move_to", {"x": x, "y": y, "z": z, "entity_id": entity_id, "pos": pos, "rng": rng}))
                return res
        return super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None) -> dict:
        self.calls.append(
            ("interact", {"pos": list(pos) if pos is not None else None, "entity_id": entity_id, "item": item})
        )
        if item and self._total_item_count(item) < 1:
            raise ApiError("missing_item", f"背包没有 {item}")
        if self.on_interact is not None:
            res = self.on_interact(self, pos=pos, entity_id=entity_id, item=item)
            if res is not None:
                return res
        return {"changed": True}

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None) -> dict:
        self.calls.append(("use_item", {"item": item, "target": deepcopy(target)}))
        if self.on_use_item is not None:
            res = self.on_use_item(self, item=item, target=target)
            if res is not None:
                return res
        self._consume_item(item, 1)
        return {"used": item, "target": target}

    def attack(self, entity_id, timeout_ms=20000, *, max_hits=None, interval_ms=None, stop_on=None) -> dict:
        self.calls.append(("attack", {"entity_id": entity_id, "max_hits": max_hits}))
        if self.on_attack is not None:
            res = self.on_attack(self, entity_id=entity_id, max_hits=max_hits)
            if res is not None:
                return res
        self.entities = [e for e in self.entities if e.get("id") != entity_id]
        return {"entity_id": entity_id, "hits": max_hits or 1, "killed": True}

    def shoot(self, entity_id=None, pos=None, weapon="bow", charge_ms=1100, *, stop_on=None) -> dict:
        self.calls.append(
            ("shoot", {"entity_id": entity_id, "pos": list(pos) if pos is not None else None, "weapon": weapon})
        )
        if self.on_shoot is not None:
            res = self.on_shoot(self, entity_id=entity_id, pos=pos, weapon=weapon)
            if res is not None:
                return res
        self.entities = [e for e in self.entities if e.get("id") != entity_id]
        return {"hit": True, "entity_id": entity_id, "weapon": weapon, "killed": True}

    def look(self, yaw=None, pitch=None, *, pos=None, entity_id=None) -> dict:
        self.calls.append(
            ("look", {"yaw": yaw, "pitch": pitch, "pos": list(pos) if pos is not None else None, "entity_id": entity_id})
        )
        self._last_look_entity_id = entity_id
        self._last_look_pos = list(pos) if pos is not None else None
        if self.on_look is not None:
            res = self.on_look(self, yaw=yaw, pitch=pitch, pos=pos, entity_id=entity_id)
            if res is not None:
                return res
        return {"ok": True}

    def control(self, **keys) -> dict:
        self.calls.append(("control", dict(keys)))
        if self.on_control is not None:
            res = self.on_control(self, **keys)
            if res is not None:
                return res
        if keys.get("hit"):
            target = None
            if self._last_look_entity_id is not None:
                target = next((e for e in self.entities if e.get("id") == self._last_look_entity_id), None)
            elif self._last_look_pos is not None:
                hostiles = [e for e in self.entities if e.get("type") == "hostile"]
                if hostiles:
                    target = min(hostiles, key=lambda e: math.dist(e["pos"], self._last_look_pos))
            if target is not None:
                eid = target["id"]
                if self.on_attack is not None:
                    self.on_attack(self, entity_id=eid, max_hits=1)
                else:
                    self.entities = [e for e in self.entities if e.get("id") != eid]
                return {"ok": True, "hits": [{"t_ms": 0, "entity": {"id": eid, "name": target.get("name")}}]}
        return {"ok": True, **keys}

    def window_act(self, target, ops, *, stop_on=None) -> dict:
        self.calls.append(("window_act", {"target": deepcopy(target), "ops": deepcopy(ops)}))
        if self.on_window_act is not None:
            res = self.on_window_act(self, target=target, ops=ops)
            if res is not None:
                return res
        if isinstance(target, dict) and target.get("kind") == "inventory":
            raise ApiError("bad_args", "inventory window is unsupported in API v1.3.11")
        for op in ops:
            if op.get("op") not in ("put", "take", "select", "text"):
                raise ApiError("bad_args", f"unsupported window op: {op.get('op')}")
        return {"ok": True, "ops": deepcopy(ops)}

    def advancements(self, status="completed", include_recipes=False) -> dict:
        return {"completed": list(self.completed_advs), "in_progress": {}}


class _WaveCTestBase(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        reset_all_voxel_caches()


# ============================================================================
# 1. Controlled Totem Trigger Hook (runtime.py)
# ============================================================================


class ControlledTotemTriggerTests(_WaveCTestBase):
    def _ready_totem_state(self, **overrides: Any) -> State:
        defaults: dict[str, Any] = dict(
            pos=(0.5, 89.0, 0.5),
            offhand={"name": "totem_of_undying", "count": 1, "slot": 45},
            armor={},
            armor_points=0.0,
            items=[{"name": "cobblestone", "count": 4, "slot": 0}],
        )
        defaults.update(overrides)
        return _state_with(**defaults)

    def _stone_floor_client(self, floor_y: int = 63, block: str = "stone") -> _BlockProbeClient:
        return _BlockProbeClient({(0, floor_y, 0): block})

    def test_rejects_missing_wrong_or_zero_count_offhand_totem(self) -> None:
        client = self._stone_floor_client(floor_y=64)
        cases = (
            ("missing_offhand", None),
            ("empty_offhand_dict", {}),
            ("wrong_offhand_item", {"name": "shield", "count": 1, "slot": 45}),
            ("zero_count_totem", {"name": "totem_of_undying", "count": 0, "slot": 45}),
        )
        for label, offhand in cases:
            with self.subTest(case=label):
                state = self._ready_totem_state(offhand=offhand)
                ok, reason, meta = validate_controlled_totem_conditions(state, client, drop_height=25)
                self.assertFalse(ok)
                self.assertTrue(reason)
                self.assertIsNone(meta)
                with self.assertRaises(Replan):
                    with controlled_totem_trigger(state, client, drop_height=25):
                        pass
                self.assertFalse(state.is_totem_trigger_armed())

    def test_rejects_any_occupied_armor_slot_or_nonzero_armor_points(self) -> None:
        client = self._stone_floor_client(floor_y=64)
        for slot, item_name in (
            ("head", "iron_helmet"),
            ("chest", "iron_chestplate"),
            ("legs", "iron_leggings"),
            ("feet", "iron_boots"),
            ("torso", "diamond_chestplate"),
        ):
            with self.subTest(slot=slot):
                state = self._ready_totem_state(armor={slot: {"name": item_name, "count": 1}})
                ok, _, _ = validate_controlled_totem_conditions(state, client, drop_height=25)
                self.assertFalse(ok)
                with self.assertRaises(Replan):
                    with controlled_totem_trigger(state, client, drop_height=25):
                        pass
                self.assertFalse(state.is_totem_trigger_armed())

        with self.subTest(case="armor_points_gt_0"):
            state = self._ready_totem_state(armor={}, armor_points=2.0)
            ok, _, _ = validate_controlled_totem_conditions(state, client, drop_height=25)
            self.assertFalse(ok)
            with self.assertRaises(Replan):
                with controlled_totem_trigger(state, client, drop_height=25):
                    pass
            self.assertFalse(state.is_totem_trigger_armed())

    def test_rejects_dirty_inventory(self) -> None:
        client = self._stone_floor_client(floor_y=64)
        state = self._ready_totem_state(dirty=True)
        ok, reason, _ = validate_controlled_totem_conditions(state, client, drop_height=25)
        self.assertFalse(ok)
        self.assertIn("inventory_dirty", reason)
        with self.assertRaises(Replan):
            with controlled_totem_trigger(state, client, drop_height=25):
                pass
        self.assertFalse(state.is_totem_trigger_armed())

    def test_rejects_void_and_unsafe_landing_blocks(self) -> None:
        state = self._ready_totem_state(pos=(0.5, 89.0, 0.5))
        with self.subTest(landing="void_all_air"):
            void_client = _BlockProbeClient({})
            ok, reason, _ = validate_controlled_totem_conditions(state, void_client, drop_height=25)
            self.assertFalse(ok)
            self.assertIn("虚空", reason)
            with self.assertRaises(Replan):
                with controlled_totem_trigger(state, void_client, drop_height=25):
                    pass
            self.assertFalse(state.is_totem_trigger_armed())

        for bad_block in (
            "lava",
            "water",
            "slime_block",
            "hay_block",
            "flowing_lava",
            "flowing_water",
            "honey_block",
            "red_bed",
            "white_bed",
        ):
            with self.subTest(landing=bad_block):
                bad_client = self._stone_floor_client(floor_y=64, block=bad_block)
                ok, reason, _ = validate_controlled_totem_conditions(state, bad_client, drop_height=25)
                self.assertFalse(ok)
                self.assertIn(bad_block, reason)
                with self.assertRaises(Replan):
                    with controlled_totem_trigger(state, bad_client, drop_height=25):
                        pass
                self.assertFalse(state.is_totem_trigger_armed())

    def test_rejects_pillar_top_drop_zero_and_validates_explicit_landing_col(self) -> None:
        # Standing on pillar top (0, 88, -1) with feet at (0.5, 89.0, -0.5):
        # Default (fx, fz) hits pillar top at dy=1 (drop_blocks=0 < min_drop=23) -> MUST reject!
        state = self._ready_totem_state(pos=(0.5, 89.0, -0.5))
        pillar_and_floor = {(0, 88, -1): "cobblestone", (1, 63, -1): "stone"}
        client_ok = _BlockProbeClient(pillar_and_floor)
        ok_default, reason_default, meta_default = validate_controlled_totem_conditions(state, client_ok, drop_height=25)
        self.assertFalse(ok_default)
        self.assertIsNone(meta_default)
        self.assertIn("0", reason_default)

        # Explicit landing_col=(1, -1) onto solid stone at y=63 (drop_blocks=25 >= 23) -> passes!
        ok_col, _, meta_col = validate_controlled_totem_conditions(
            state, client_ok, drop_height=25, landing_col=(1, -1), min_drop=23
        )
        self.assertTrue(ok_col)
        self.assertEqual(meta_col["landing_block"], "stone")
        self.assertEqual(meta_col["landing_pos"], [1, 63, -1])
        self.assertEqual(meta_col["drop_blocks"], 25)
        self.assertEqual(meta_col["landing_col"], [1, -1])

        # Explicit landing_col over void, lava, honey_block, red_bed, or shallow ledge (< 23) -> MUST reject!
        bad_cols = (
            ("void", {(0, 88, -1): "cobblestone"}),
            ("lava", {(0, 88, -1): "cobblestone", (1, 63, -1): "lava"}),
            ("honey_block", {(0, 88, -1): "cobblestone", (1, 63, -1): "honey_block"}),
            ("red_bed", {(0, 88, -1): "cobblestone", (1, 63, -1): "red_bed"}),
            ("shallow_ledge_10", {(0, 88, -1): "cobblestone", (1, 78, -1): "stone"}),
            ("head_blocked", {(0, 88, -1): "cobblestone", (1, 89, -1): "stone", (1, 63, -1): "stone"}),
        )
        for label, blocks in bad_cols:
            with self.subTest(case=label):
                bad_client = _BlockProbeClient(blocks)
                ok, reason, meta = validate_controlled_totem_conditions(
                    state, bad_client, drop_height=25, landing_col=(1, -1), min_drop=23
                )
                self.assertFalse(ok)
                self.assertTrue(reason)
                self.assertIsNone(meta)

    def test_rejects_unhealthy_unready_or_hazardous_states(self) -> None:
        client = self._stone_floor_client(floor_y=64)
        bad_states = (
            ("in_safe_mode", dict(safe_mode_s=65.0)),
            ("in_lava", dict(in_lava=True)),
            ("submerged", dict(submerged=True)),
            ("in_water", dict(in_water=True)),
            ("on_fire", dict(on_fire=True)),
            ("dead", dict(alive=False)),
            ("not_ready", dict(ready=False)),
        )
        for label, kw in bad_states:
            with self.subTest(case=label):
                state = self._ready_totem_state(**kw)
                ok, _, _ = validate_controlled_totem_conditions(state, client, drop_height=25)
                self.assertFalse(ok)
                with self.assertRaises(Replan):
                    with controlled_totem_trigger(state, client, drop_height=25):
                        pass
                self.assertFalse(state.is_totem_trigger_armed())

    def test_multi_tick_fall_window_suppresses_until_landing_totem_consumed_or_expiry(self) -> None:
        for hazard_mode in ("void_risk", "heal"):
            with self.subTest(hazard=hazard_mode):
                client = self._stone_floor_client(floor_y=64, block="stone")
                state = self._ready_totem_state(
                    pos=(0.5, 89.0, 0.5),
                    on_ground=False,
                    health=4.0 if hazard_mode == "heal" else 20.0,
                )
                if hazard_mode == "void_risk":
                    state.set_support(
                        {
                            "pos": [0, 88, 0],
                            "status_pos": [0.5, 89.0, 0.5],
                            "at": time.monotonic(),
                            "risk": "脚下连续空气且正在下落",
                        }
                    )
                ex = Executor(client, state, log=lambda *_: None)
                ex.emergency = MagicMock(name="emergency")
                if hazard_mode == "heal":
                    ex.active_action = "attack"  # elevates soft heal -> urgent
                monitor = SafetyMonitor(client, state, ex)

                with controlled_totem_trigger(state, client, drop_height=25, max_duration_s=3.5) as meta:
                    self.assertTrue(state.is_totem_trigger_armed())
                    self.assertEqual(meta["landing_block"], "stone")
                    self.assertEqual(meta["landing_pos"], [0, 64, 0])

                    # Multiple airborne ticks during fall: guard remains armed and suppresses void_risk/heal
                    for _ in range(4):
                        monitor._dispatch(reconcile=True)
                        self.assertEqual(ex.emergency.call_count, 0)
                        self.assertTrue(state.is_totem_trigger_armed())

                    # Landing after airborne (or totem consumption) disarms and restores emergency dispatch
                    if hazard_mode == "void_risk":
                        state.st["on_ground"] = True
                    else:
                        state.inventory["offhand"] = None
                        state.st["offhand"] = None
                    monitor._dispatch(reconcile=True)
                    self.assertFalse(state.is_totem_trigger_armed())
                    self.assertEqual(ex.emergency.call_count, 1)

                self.assertFalse(state.is_totem_trigger_armed())
                self.assertIsNone(state._totem_guard)

        # Window expiration (max_duration_s) also disarms and triggers emergency
        client = self._stone_floor_client(floor_y=64, block="stone")
        state = self._ready_totem_state(pos=(0.5, 89.0, 0.5), on_ground=False)
        state.set_support(
            {
                "pos": [0, 88, 0],
                "status_pos": [0.5, 89.0, 0.5],
                "at": time.monotonic(),
                "risk": "脚下连续空气且正在下落",
            }
        )
        ex = Executor(client, state, log=lambda *_: None)
        ex.emergency = MagicMock(name="emergency")
        monitor = SafetyMonitor(client, state, ex)
        with controlled_totem_trigger(state, client, drop_height=25, max_duration_s=0.05):
            monitor._dispatch(reconcile=True)
            self.assertEqual(ex.emergency.call_count, 0)
            time.sleep(0.08)
            self.assertFalse(state.is_totem_trigger_armed())
            monitor._dispatch(reconcile=True)
            self.assertEqual(ex.emergency.call_count, 1)
        self.assertIsNone(state._totem_guard)

    def test_never_suppresses_escape_lava_surface_or_safe_mode_while_armed(self) -> None:
        for hazard_kind in ("escape_lava", "surface", "safe_mode"):
            with self.subTest(hazard=hazard_kind):
                client = self._stone_floor_client(floor_y=64, block="stone")
                state = self._ready_totem_state(pos=(0.5, 89.0, 0.5))
                ex = Executor(client, state, log=lambda *_: None)
                ex.emergency = MagicMock(name="emergency")
                monitor = SafetyMonitor(client, state, ex)

                with state.controlled_totem_trigger(client, drop_height=25):
                    self.assertTrue(state.is_totem_trigger_armed())
                    if hazard_kind == "escape_lava":
                        state.st["in_lava"] = True
                    elif hazard_kind == "surface":
                        state.st["submerged"] = True
                        state.st["oxygen"] = 2.0
                    elif hazard_kind == "safe_mode":
                        state.safe_mode_until = time.monotonic() + 65.0

                    monitor._dispatch(reconcile=True)
                    self.assertEqual(ex.emergency.call_count, 1)
                    reason_arg = ex.emergency.call_args[0][0]
                    self.assertIn(hazard_kind, reason_arg)

                self.assertFalse(state.is_totem_trigger_armed())

    def test_exit_and_exception_unconditionally_clear_guard_and_restore_dispatch(self) -> None:
        client = self._stone_floor_client(floor_y=64, block="stone")
        state = self._ready_totem_state(pos=(0.5, 89.0, 0.5))
        ex = Executor(client, state, log=lambda *_: None)
        ex.emergency = MagicMock(name="emergency")
        monitor = SafetyMonitor(client, state, ex)

        # Normal exit clears guard and restores normal void_risk emergency dispatch
        with controlled_totem_trigger(state, client, drop_height=25):
            self.assertTrue(state.is_totem_trigger_armed())
        self.assertFalse(state.is_totem_trigger_armed())
        self.assertIsNone(state._totem_guard)

        state.set_support(
            {
                "pos": [0, 88, 0],
                "status_pos": [0.5, 89.0, 0.5],
                "at": time.monotonic(),
                "risk": "脚下连续空气且正在下落",
            }
        )
        monitor._dispatch(reconcile=True)
        self.assertEqual(ex.emergency.call_count, 1)

        # Exception inside `with` block also unconditionally clears guard
        ex.emergency.reset_mock()
        monitor._active.clear()
        monitor._triggered.clear()
        with self.assertRaises(RuntimeError):
            with controlled_totem_trigger(state, client, drop_height=25):
                self.assertTrue(state.is_totem_trigger_armed())
                raise RuntimeError("boom inside controlled drop")
        self.assertFalse(state.is_totem_trigger_armed())
        self.assertIsNone(state._totem_guard)

        monitor._dispatch(reconcile=True)
        self.assertEqual(ex.emergency.call_count, 1)


# ============================================================================
# 2. unlock_ominous_vault (adventure/revaulting)
# ============================================================================


class UnlockOminousVaultTests(_WaveCTestBase):
    def test_blocked_on_empty_inventory_dead_or_unready(self) -> None:
        for label, st in (
            ("empty_inv", _state_with(items=[])),
            ("dead", _state_with(alive=False, items=[{"name": "ominous_trial_key", "count": 1}])),
            ("not_ready", _state_with(ready=False, items=[{"name": "ominous_trial_key", "count": 1}])),
            ("no_key_no_bottle", _state_with(items=[{"name": "iron_sword", "count": 1}])),
        ):
            with self.subTest(case=label):
                msg = UnlockOminousVault.blocked(st)
                self.assertTrue(msg and _ZH_RE.search(msg))

    def test_happy_path_scouts_first_prefers_weak_spawner_lintel_drinks_clears_loots_unlocks(self) -> None:
        blocks = _flat(-6, 8, -6, 8, y=63, block="stone")
        # Normal vault (must be ignored), ominous vault at (3, 64, 0)
        blocks[(2, 64, 2)] = "vault"
        blocks[(3, 64, 0)] = "vault"
        # Closer breeze spawner along -X (-2, 64, 0) vs farther weak zombie spawner along +Z (0, 64, 5)
        blocks[(-2, 64, 0)] = "trial_spawner"
        blocks[(0, 64, 5)] = "trial_spawner"
        block_props = {
            (2, 64, 2): {"ominous": False, "vault_state": "active"},
            (3, 64, 0): {"ominous": True, "vault_state": "active"},
            (-2, 64, 0): {"mob": "breeze"},
            (0, 64, 5): {"mob": "zombie"},
        }
        world = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            block_props=block_props,
            items={
                "iron_sword": 1,
                "shield": 1,
                "ominous_bottle": 1,
                "cobblestone": 8,
                "cooked_beef": 4,
            },
        )
        wave_spawned = False

        def _on_use_item(w: WaveCWorld, item: str, target=None):
            nonlocal wave_spawned
            if item == "ominous_bottle":
                w._consume_item("ominous_bottle", 1)
                w.st["effects"] = ["trial_omen"]
                w.entities.append(
                    {"id": 501, "name": "zombie", "type": "hostile", "pos": [0.5, 64.0, 3.5], "health": 20}
                )
                wave_spawned = True
                return {"used": "ominous_bottle"}
            return None

        def _on_attack(w: WaveCWorld, entity_id: int, max_hits=None):
            w.entities = [e for e in w.entities if e["id"] != entity_id]
            w.entities.append({"id": 502, "name": "ominous_trial_key", "type": "item", "pos": [0.5, 64.0, 3.5]})
            return {"entity_id": entity_id, "hits": 3, "killed": True}

        def _on_move_to(w: WaveCWorld, pos=None, **_kw):
            if pos is not None and math.dist(pos, [0.5, 64.0, 3.5]) < 1.2:
                if any(e["id"] == 502 for e in w.entities):
                    w.entities = [e for e in w.entities if e["id"] != 502]
                    w._add_item("ominous_trial_key", 1)
                w.st["pos"] = [float(v) for v in pos]
                return {"pos": [float(v) for v in pos]}
            return None

        def _on_interact(w: WaveCWorld, pos=None, entity_id=None, item=None):
            if pos == [3, 64, 0] and item == "ominous_trial_key":
                w._consume_item("ominous_trial_key", 1)
                w.block_props[(3, 64, 0)]["vault_state"] = "ejecting"
                return {"changed": True}
            return None

        world.on_use_item = _on_use_item
        world.on_attack = _on_attack
        world.on_move_to = _on_move_to
        world.on_interact = _on_interact

        ex, state = world.session()
        skill = UnlockOminousVault()
        drive(skill, ex, state, max_steps=60)

        self.assertTrue(wave_spawned)
        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("ominous_trial_key"), 0)
        self.assertEqual(world.block_props[(3, 64, 0)]["vault_state"], "ejecting")

        # Scouted vault + trial_spawner BEFORE drinking ominous_bottle
        ops = [op for op, _ in world.calls]
        first_find_idx = ops.index("find_blocks")
        drink_idx = next(i for i, (op, req) in enumerate(world.calls) if op == "use_item" and req["item"] == "ominous_bottle")
        self.assertLess(first_find_idx, drink_idx)

        # Recorded trial_vault_ominous landmark in world_memo
        memo = memo_for(state)
        self.assertTrue(memo.has_landmark("trial_vault_ominous", dimension="overworld"))
        self.assertIn((3, 64, 0), memo.landmarks("trial_vault_ominous", dimension="overworld"))

        # Preferred weak zombie spawner at (0, 64, 5) over closer breeze spawner at (-2, 64, 0):
        # 1-block foot-strike lintel placed at (fx + dx, fy + 1, fz + dz) = (0, 65, 1)
        placed_positions = [tuple(req["pos"]) for op, req in world.calls if op == "place"]
        self.assertIn((0, 65, 1), placed_positions)
        self.assertNotIn((-1, 65, 0), placed_positions)

        # Unlocked ominous vault via interact(item="ominous_trial_key")
        interacts = [req for op, req in world.calls if op == "interact"]
        self.assertTrue(any(r["pos"] == [3, 64, 0] and r["item"] == "ominous_trial_key" for r in interacts))

        # Shield posture raised and cleanly reset
        postures = [req for op, req in world.calls if op == "posture"]
        self.assertTrue(any(p.get("shield") is True for p in postures))
        self.assertIs(postures[-1].get("shield"), False)
        self.assertFalse(state.st.get("blocking"))
        self.assertFalse(world.st.get("blocking"))

    def test_env_blocked_when_no_ominous_vault_nearby(self) -> None:
        for label, blocks, props in (
            ("no_vault", _flat(-4, 4, -4, 4, y=63), {}),
            (
                "only_normal_vault",
                {**_flat(-4, 4, -4, 4, y=63), (2, 64, 0): "vault"},
                {(2, 64, 0): {"ominous": False, "vault_state": "active"}},
            ),
        ):
            with self.subTest(case=label):
                world = WaveCWorld(
                    pos=(0.5, 64.0, 0.5),
                    blocks=blocks,
                    block_props=props,
                    items={"iron_sword": 1, "ominous_bottle": 1},
                )
                ex, state = world.session()
                with self.assertRaises(EnvBlocked) as ctx:
                    drive(UnlockOminousVault(), ex, state)
                self.assertEqual(ctx.exception.missing_predicate, "trial_vault_ominous")
                # Must NOT waste ominous_bottle before scouting an ominous vault
                self.assertFalse(any(op == "use_item" for op, _ in world.calls))

    def test_shield_posture_cleared_on_abort_or_exception_mid_wave(self) -> None:
        def _make_world() -> WaveCWorld:
            blocks = _flat(-4, 6, -4, 6, y=63)
            blocks[(3, 64, 0)] = "vault"
            blocks[(4, 64, 0)] = "trial_spawner"
            return WaveCWorld(
                pos=(0.5, 64.0, 0.5),
                blocks=blocks,
                block_props={(3, 64, 0): {"ominous": True}, (4, 64, 0): {"mob": "zombie"}},
                items={"iron_sword": 1, "shield": 1, "ominous_bottle": 1, "cobblestone": 4},
                entities=[{"id": 601, "name": "zombie", "type": "hostile", "pos": [2.5, 64.0, 0.5], "health": 20}],
            )

        # Subcase A: abort() mid-wave
        world_abort = _make_world()
        ex1, st1 = world_abort.session()
        skill1 = UnlockOminousVault()
        for _ in range(10):
            done = skill1.step(ex1, st1)
            self.assertFalse(done)
            if world_abort.st.get("blocking") is True:
                break
        self.assertTrue(world_abort.st.get("blocking"), "expected shield up before abort()")
        skill1.abort()
        self.assertFalse(world_abort.st.get("blocking"))
        self.assertFalse(st1.st.get("blocking"))
        self.assertIs([req for op, req in world_abort.calls if op == "posture"][-1].get("shield"), False)

        # Subcase B: exception mid-wave (attack fails with Replan)
        world_err = _make_world()

        def _boom_attack(w: WaveCWorld, entity_id: int, max_hits=None):
            raise ApiError("interrupted", "hurt mid wave")

        world_err.on_attack = _boom_attack
        ex2, st2 = world_err.session()
        with self.assertRaises(Replan):
            drive(UnlockOminousVault(), ex2, st2)
        self.assertFalse(world_err.st.get("blocking"))
        self.assertFalse(st2.st.get("blocking"))
        postures = [req for op, req in world_err.calls if op == "posture"]
        self.assertTrue(any(p.get("shield") is True for p in postures))
        self.assertIs(postures[-1].get("shield"), False)

    def test_landmark_nav_to_trial_chambers_and_low_hp_retreat_with_posture_cleanup(self) -> None:
        # Subcase A: ominous vault outside radius 48 -> navigates via trial_chambers landmark and unlocks
        blocks = {**_flat(-4, 4, -4, 4, y=63), **_flat(86, 94, -4, 4, y=63), (91, 64, 0): "vault"}
        w_nav = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            block_props={(91, 64, 0): {"ominous": True, "vault_state": "active"}},
            items={"ominous_trial_key": 1},
        )
        w_nav.on_interact = lambda w, pos=None, entity_id=None, item=None: (
            w._consume_item("ominous_trial_key", 1),
            w.block_props[(91, 64, 0)].__setitem__("vault_state", "ejecting"),
            {"changed": True},
        )[2]
        ex1, st1 = w_nav.session()
        memo_for(st1).record_landmark("trial_chambers", (90, 64, 0), dimension="overworld")
        sk1 = UnlockOminousVault()
        drive(sk1, ex1, st1, max_steps=40)
        self.assertEqual(sk1.verify_done(st1), "")
        self.assertTrue(any(op == "move_to" and req["pos"] and req["pos"][0] >= 88.0 for op, req in w_nav.calls))

        # Subcase B: low HP before drinking ominous_bottle -> retreats safely without wasting bottle
        w_pre = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={**_flat(-5, 5, -5, 5, y=63), (3, 64, 0): "vault", (4, 64, 0): "trial_spawner"},
            block_props={(3, 64, 0): {"ominous": True}, (4, 64, 0): {"mob": "zombie"}},
            items={"iron_sword": 1, "ominous_bottle": 1},
            hp=7.0,
        )
        ex2, st2 = w_pre.session()
        with self.assertRaises(Replan):
            drive(UnlockOminousVault(), ex2, st2)
        self.assertFalse(any(op == "use_item" for op, _ in w_pre.calls))
        self.assertLess(w_pre.st["pos"][0], 0.5)

        # Subcase C: low HP mid-wave -> retreats away from spawner and clears shield posture
        w_mid = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={**_flat(-5, 5, -5, 5, y=63), (3, 64, 0): "vault", (4, 64, 0): "trial_spawner"},
            block_props={(3, 64, 0): {"ominous": True}, (4, 64, 0): {"mob": "zombie"}},
            items={"iron_sword": 1, "shield": 1},
            effects=["trial_omen"],
            hp=7.0,
            entities=[{"id": 650, "name": "zombie", "type": "hostile", "pos": [2.5, 64.0, 0.5], "health": 20}],
        )
        ex3, st3 = w_mid.session()
        with self.assertRaises(Replan):
            drive(UnlockOminousVault(), ex3, st3)
        self.assertLess(w_mid.st["pos"][0], 0.5)
        self.assertFalse(w_mid.st.get("blocking"))
        self.assertFalse(st3.st.get("blocking"))
        postures = [req for op, req in w_mid.calls if op == "posture"]
        self.assertTrue(any(p.get("shield") is True for p in postures))
        self.assertIs(postures[-1].get("shield"), False)


# ============================================================================
# 3. win_micro_village_raid (adventure/hero_of_the_village + prep_micro_village_bunker)
# ============================================================================


class WinMicroVillageRaidTests(_WaveCTestBase):
    def test_blocked_on_empty_inventory_dead_or_unready(self) -> None:
        for label, st in (
            ("empty_inv", _state_with(items=[])),
            ("dead", _state_with(alive=False, items=[{"name": "iron_sword", "count": 1}, {"name": "ominous_bottle", "count": 1}])),
            ("not_ready", _state_with(ready=False, items=[{"name": "iron_sword", "count": 1}, {"name": "ominous_bottle", "count": 1}])),
            ("insufficient_materials", _state_with(items=[{"name": "cobblestone", "count": 5}])),
        ):
            with self.subTest(case=label):
                msg = WinMicroVillageRaid.blocked(st)
                self.assertTrue(msg and _ZH_RE.search(msg))

    def test_prep_only_builds_bunker_bed_bell_sniper_tower_without_drinking_bottle(self) -> None:
        world = WaveCWorld(
            pos=(1.5, 64.0, 2.5),
            blocks=_flat(-6, 8, -6, 8, y=63, block="stone"),
            items={
                "cobblestone": 24,
                "red_bed": 1,
                "bell": 1,
                "ominous_bottle": 1,
                "iron_sword": 1,
            },
            entities=[{"id": 701, "name": "villager", "type": "other", "pos": [3.5, 64.0, 0.5]}],
        )
        ex, state = world.session()
        skill = WinMicroVillageRaid(prep_only=True)
        drive(skill, ex, state, max_steps=80)

        self.assertEqual(skill.verify_done(state), "")
        # Did NOT drink ominous_bottle in prep_only mode
        self.assertFalse(any(op == "use_item" and req["item"] == "ominous_bottle" for op, req in world.calls))

        # Enclosure blocks placed around villager (3, 64, 0)
        cobble_places = [tuple(req["pos"]) for op, req in world.calls if op == "place" and req["item"] == "cobblestone"]
        enclosure_ring = {(4, 64, 0), (2, 64, 0), (3, 64, 1), (3, 64, -1)}
        self.assertTrue(enclosure_ring.intersection(cobble_places))

        # Bed & bell placed and recorded as stations; cramped 1-high alcove skipped for bed
        self.assertTrue(any(op == "place" and req["item"] == "red_bed" for op, req in world.calls))
        self.assertTrue(any(op == "place" and req["item"] == "bell" for op, req in world.calls))
        memo = memo_for(state)
        self.assertTrue(memo.has_station("bell", dimension="overworld"))
        self.assertTrue(memo.has_station("red_bed", dimension="overworld"))

        # 4-block sniper tower climbed (player Y reached >= 68.0)
        self.assertGreaterEqual(world.st["pos"][1], 68.0)

        # Micro village bunker landmark recorded
        self.assertTrue(memo.has_landmark("micro_village_bunker", dimension="overworld"))
        self.assertIn((3, 64, 0), memo.landmarks("micro_village_bunker", dimension="overworld"))

    def test_bed_placement_requires_two_block_clearance(self) -> None:
        blocks = _flat(-4, 6, -4, 6, y=63, block="stone")
        # Roof at y=65 over almost entire area except open spot at (5, 64, 0)-(6, 64, 0)
        for x in range(-1, 5):
            for z in range(-4, 5):
                blocks[(x, 65, z)] = "stone"
        world = WaveCWorld(
            pos=(5.5, 64.0, 2.5),
            blocks=blocks,
            items={"cobblestone": 20, "red_bed": 1},
            entities=[{"id": 702, "name": "villager", "type": "other", "pos": [3.5, 64.0, 0.5]}],
        )
        ex, state = world.session()
        drive(WinMicroVillageRaid(prep_only=True), ex, state, max_steps=80)
        bed_places = [tuple(req["pos"]) for op, req in world.calls if op == "place" and req["item"] == "red_bed"]
        self.assertEqual(len(bed_places), 1)
        bx, by, bz = bed_places[0]
        self.assertEqual(blocks.get((bx, by + 1, bz), "air"), "air")

    def test_env_blocked_when_villager_and_village_landmark_missing(self) -> None:
        world = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-4, 4, -4, 4, y=63),
            items={"cobblestone": 20, "red_bed": 1, "bell": 1},
            entities=[],
        )
        ex, state = world.session()
        with self.assertRaises(EnvBlocked) as ctx:
            drive(WinMicroVillageRaid(prep_only=True), ex, state)
        self.assertEqual(ctx.exception.missing_predicate, "villager")

    def test_full_raid_multi_wave_selects_level1_bottle_prioritizes_evoker_and_wins_hero(self) -> None:
        blocks = _flat(-6, 8, -6, 8, y=63)
        blocks[(2, 64, 0)] = "bell"
        # Level 5 bottle listed BEFORE Level 1 bottle to guarantee amplifier sorting (not list order)
        item_stacks = [
            {"name": "ominous_bottle", "count": 1, "slot": 0, "components": {"ominous_amplifier": 4}},
            {"name": "ominous_bottle", "count": 1, "slot": 1, "components": {"ominous_amplifier": 0}},
            {"name": "iron_sword", "count": 1, "slot": 2},
            {"name": "shield", "count": 1, "slot": 3},
        ]
        world = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            item_stacks=item_stacks,
            entities=[],  # Raiders initially hidden until bell is rung!
        )
        bell_rung = False
        wave_num = 0
        polls_between_waves = 0

        def _on_use_item(w: WaveCWorld, item: str, target=None):
            if item == "ominous_bottle":
                w.item_stacks = [
                    s for s in (w.item_stacks or [])
                    if not (s.get("name") == "ominous_bottle" and (s.get("components") or {}).get("ominous_amplifier") == 0)
                ]
                w.st["effects"] = ["raid_omen"]
                return {"used": "ominous_bottle"}
            return None

        def _on_interact(w: WaveCWorld, pos=None, entity_id=None, item=None):
            nonlocal bell_rung, wave_num
            if pos == [2, 64, 0] and not bell_rung:
                bell_rung = True
                wave_num = 1
                w.st["effects"] = []  # raid_omen consumed when Wave 1 starts
                w.entities.append({"id": 800, "name": "pillager", "type": "hostile", "pos": [2.0, 64.0, 0.5]})
                return {"rung": True}
            return {"changed": True}

        orig_get_entities = world.get_entities

        def _get_entities_multi_wave(radius=32, types=None):
            nonlocal wave_num, polls_between_waves
            res = orig_get_entities(radius=radius, types=types)
            if bell_rung and wave_num == 1 and not res["entities"] and types and "pillager" in types:
                polls_between_waves += 1
                if polls_between_waves >= 2:
                    wave_num = 2
                    # Wave 2 reveals closer pillager (801) AND farther evoker (802) -> evoker targeted first!
                    world.entities.extend(
                        [
                            {"id": 801, "name": "pillager", "type": "hostile", "pos": [2.0, 64.0, 0.5]},
                            {"id": 802, "name": "evoker", "type": "hostile", "pos": [4.0, 64.0, 0.5]},
                        ]
                    )
                    res = orig_get_entities(radius=radius, types=types)
            return res

        world.get_entities = _get_entities_multi_wave  # type: ignore[method-assign]

        def _on_attack(w: WaveCWorld, entity_id: int, max_hits=None):
            w.entities = [e for e in w.entities if e["id"] != entity_id]
            if entity_id == 802:
                w.entities.append({"id": 899, "name": "totem_of_undying", "type": "item", "pos": [4.0, 64.0, 0.5]})
            if wave_num == 2 and not any(e.get("type") == "hostile" for e in w.entities):
                w.st["effects"] = ["hero_of_the_village"]
            return {"entity_id": entity_id, "hits": 3, "killed": True}

        def _on_move_to(w: WaveCWorld, pos=None, **_kw):
            if pos is not None and math.dist(pos, [4.0, 64.0, 0.5]) < 1.5:
                if any(e["id"] == 899 for e in w.entities):
                    w.entities = [e for e in w.entities if e["id"] != 899]
                    w._add_item("totem_of_undying", 1)
                w.st["pos"] = [float(v) for v in pos]
                return {"pos": [float(v) for v in pos]}
            return None

        world.on_use_item = _on_use_item
        world.on_interact = _on_interact
        world.on_attack = _on_attack
        world.on_move_to = _on_move_to

        ex, state = world.session()
        memo_for(state).record_landmark("micro_village_bunker", (3, 64, 0), dimension="overworld")

        chosen = _select_lowest_ominous_bottle(state)
        self.assertIsNotNone(chosen)
        self.assertEqual((chosen.get("components") or {}).get("ominous_amplifier"), 0)

        skill = WinMicroVillageRaid(prep_only=False)
        with patch.object(t4_ominous_combat, "_RAID_WAVE_POLL_S", 0.0):
            steps = drive(skill, ex, state, max_steps=60)

        self.assertEqual(skill.verify_done(state), "")
        self.assertTrue(bell_rung)
        self.assertEqual(wave_num, 2)
        self.assertTrue(skill._saw_evoker)
        self.assertTrue(skill._looted_totem)
        self.assertGreaterEqual(skill._bot_killed_raiders, 3)
        self.assertEqual(state.count("totem_of_undying"), 1)

        drink_steps = [s for s in steps if s and s.get("op") == "use_item" and s.get("item") == "ominous_bottle"]
        self.assertEqual(len(drink_steps), 1)
        self.assertEqual(drink_steps[0].get("bottle_rank"), 0)
        self.assertTrue(drink_steps[0].get("api_selects_by_name"))

        # Wave 1 pillager (800), then Wave 2 evoker (802) prioritized over closer pillager (801)
        attacked_ids = [req["entity_id"] for op, req in world.calls if op == "attack"]
        self.assertEqual(attacked_ids, [800, 802, 801])

        postures = [req for op, req in world.calls if op == "posture"]
        self.assertTrue(any(p.get("shield") is True for p in postures))
        self.assertIs(postures[-1].get("shield"), False)
        self.assertFalse(state.st.get("blocking"))
        self.assertFalse(world.st.get("blocking"))

    def test_wave1_clear_without_hero_does_not_false_win_and_easy_3wave_marks_landmark(self) -> None:
        blocks = _flat(-4, 4, -4, 4, y=63)
        # Case A: 1 wave cleared, NO hero_of_the_village, NO raid_omen -> MUST NOT false-win on Wave 1!
        world_no_hero = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"iron_sword": 1, "shield": 1},
            effects=[],  # active raid has no raid_omen
            entities=[{"id": 810, "name": "pillager", "type": "hostile", "pos": [2.0, 64.0, 0.5]}],
        )
        # Note: blocked() requires raid_omen/bad_omen or bottle, so give bad_omen at start then clear it on attack
        world_no_hero.st["effects"] = ["raid_omen"]
        world_no_hero.on_attack = lambda w, entity_id, max_hits=None: (
            w.entities.__setitem__(slice(None), [e for e in w.entities if e["id"] != entity_id]),
            w.st.__setitem__("effects", []),
            {"entity_id": entity_id, "hits": 1, "killed": True},
        )[2]
        ex1, st1 = world_no_hero.session()
        memo_for(st1).record_landmark("micro_village_bunker", (3, 64, 0), dimension="overworld")
        skill1 = WinMicroVillageRaid(prep_only=False)
        with patch.object(t4_ominous_combat, "_RAID_WAVE_POLL_S", 0.0):
            with self.assertRaises(Replan) as ctx1:
                drive(skill1, ex1, st1, max_steps=60)
        self.assertIn("hero_of_the_village", str(ctx1.exception))
        self.assertFalse(skill1._won)
        self.assertTrue(skill1.verify_done(st1))

        # Case B: 3 waves cleared with 0 evokers & 0 totems -> wins and records raid_no_evoker_easy landmark
        world_easy = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            difficulty="",  # Simulate bridge omitting difficulty from /status
            blocks=blocks,
            items={"iron_sword": 1, "cobblestone": 30},
            effects=["raid_omen"],
            entities=[{"id": 901, "name": "pillager", "type": "hostile", "pos": [2.0, 64.0, 0.5]}],
        )
        wave = 1
        gap_polls = 0
        orig_get_entities = world_easy.get_entities

        def _easy_get_entities(radius=32, types=None):
            nonlocal wave, gap_polls
            res = orig_get_entities(radius=radius, types=types)
            if types and "pillager" in types and not res["entities"] and wave < 3:
                gap_polls += 1
                if gap_polls >= 2:
                    gap_polls = 0
                    wave += 1
                    world_easy.entities.append({"id": 900 + wave, "name": "pillager", "type": "hostile", "pos": [2.0, 64.0, 0.5]})
                    res = orig_get_entities(radius=radius, types=types)
            return res

        world_easy.get_entities = _easy_get_entities  # type: ignore[method-assign]

        def _easy_attack(w: WaveCWorld, entity_id: int, max_hits=None):
            w.entities = [e for e in w.entities if e["id"] != entity_id]
            w.st["effects"] = ["hero_of_the_village"] if wave == 3 else []
            return {"entity_id": entity_id, "hits": 1, "killed": True}

        world_easy.on_attack = _easy_attack
        ex2, st2 = world_easy.session()
        memo_for(st2).record_landmark("micro_village_bunker", (3, 64, 0), dimension="overworld")
        skill2 = WinMicroVillageRaid(prep_only=False)
        with patch.object(t4_ominous_combat, "_RAID_WAVE_POLL_S", 0.0):
            drive(skill2, ex2, st2, max_steps=60)
        self.assertEqual(skill2.verify_done(st2), "")
        self.assertTrue(memo_for(st2).has_landmark("raid_no_evoker_easy", dimension="overworld"))
        # Subsequent TriggerTotemSafely recognizes Easy fallback via landmark and raises EnvBlocked!
        with self.assertRaises(EnvBlocked) as ctx2:
            drive(TriggerTotemSafely(), ex2, st2)
        self.assertEqual(ctx2.exception.missing_predicate, "totem_of_undying_or_normal_plus_difficulty")

    def test_zero_manual_bot_kills_raises_replan_and_fails_verify_done(self) -> None:
        # Case A: all raiders already killed by environment (0 visible even after ringing bell)
        blocks = _flat(-4, 4, -4, 4, y=63)
        blocks[(2, 64, 0)] = "bell"
        world_env_kills = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"iron_sword": 1, "shield": 1},
            effects=["raid_omen"],
            entities=[],
        )
        ex1, st1 = world_env_kills.session()
        memo_for(st1).record_landmark("micro_village_bunker", (3, 64, 0), dimension="overworld")
        skill1 = WinMicroVillageRaid(prep_only=False)
        with patch.object(t4_ominous_combat, "_RAID_WAVE_POLL_S", 0.0):
            with self.assertRaises(Replan) as ctx1:
                drive(skill1, ex1, st1)
        self.assertIn("亲手补刀", str(ctx1.exception))
        self.assertEqual(skill1._bot_killed_raiders, 0)
        self.assertTrue(skill1.verify_done(st1))

        # Case B: Bot attacks but never scores a kill (killed=False, entity remains alive)
        world_no_kill = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"iron_sword": 1, "shield": 1},
            effects=["raid_omen"],
            entities=[{"id": 850, "name": "pillager", "type": "hostile", "pos": [2.0, 64.0, 0.5]}],
        )
        world_no_kill.on_attack = lambda w, entity_id, max_hits=None: {
            "entity_id": entity_id,
            "hits": 1,
            "killed": False,
        }
        ex2, st2 = world_no_kill.session()
        memo_for(st2).record_landmark("micro_village_bunker", (3, 64, 0), dimension="overworld")
        skill2 = WinMicroVillageRaid(prep_only=False)
        with patch.object(t4_ominous_combat, "_RAID_WAVE_POLL_S", 0.0):
            with self.assertRaises(Replan) as ctx2:
                drive(skill2, ex2, st2, max_steps=60)
        self.assertIn("亲手补刀", str(ctx2.exception))
        self.assertEqual(skill2._bot_killed_raiders, 0)
        self.assertTrue(skill2.verify_done(st2))

    def test_shield_posture_reset_on_abort_or_failure_mid_raid(self) -> None:
        blocks = _flat(-4, 4, -4, 4, y=63)

        # Subcase A: abort() mid-raid
        world_abort = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"iron_sword": 1, "shield": 1},
            effects=["raid_omen"],
            entities=[{"id": 860, "name": "pillager", "type": "hostile", "pos": [2.0, 64.0, 0.5]}],
        )
        world_abort.on_attack = lambda w, entity_id, max_hits=None: {"entity_id": entity_id, "hits": 1, "killed": False}
        ex1, st1 = world_abort.session()
        memo_for(st1).record_landmark("micro_village_bunker", (3, 64, 0), dimension="overworld")
        skill1 = WinMicroVillageRaid(prep_only=False)
        for _ in range(5):
            done = skill1.step(ex1, st1)
            self.assertFalse(done)
            if world_abort.st.get("blocking") is True:
                break
        self.assertTrue(world_abort.st.get("blocking"))
        skill1.abort()
        self.assertFalse(world_abort.st.get("blocking"))
        self.assertFalse(st1.st.get("blocking"))
        self.assertIs([req for op, req in world_abort.calls if op == "posture"][-1].get("shield"), False)

        # Subcase B: low HP abort mid-raid raises Replan and resets shield
        world_lowhp = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"iron_sword": 1, "shield": 1},
            effects=["raid_omen"],
            hp=4.0,
            entities=[{"id": 861, "name": "pillager", "type": "hostile", "pos": [2.0, 64.0, 0.5]}],
        )
        ex2, st2 = world_lowhp.session()
        memo_for(st2).record_landmark("micro_village_bunker", (3, 64, 0), dimension="overworld")
        with self.assertRaises(Replan):
            drive(WinMicroVillageRaid(prep_only=False), ex2, st2)
        self.assertFalse(world_lowhp.st.get("blocking"))
        self.assertFalse(st2.st.get("blocking"))
        postures = [req for op, req in world_lowhp.calls if op == "posture"]
        self.assertTrue(any(p.get("shield") is True for p in postures))
        self.assertIs(postures[-1].get("shield"), False)
        # Retreated away from pillager at x=2.0 before raising Replan
        self.assertLess(world_lowhp.st["pos"][0], 0.5)

    def test_raid_landmark_nav_to_village_and_bunker_and_low_hp_bottle_guard(self) -> None:
        # Subcase A: prep_only navigates to village landmark when villager is outside radius 32
        blocks = {**_flat(-4, 4, -4, 4, y=63), **_flat(84, 96, -4, 4, y=63)}
        w_prep = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"cobblestone": 24, "red_bed": 1, "bell": 1},
            entities=[{"id": 880, "name": "villager", "type": "other", "pos": [91.5, 64.0, 0.5]}],
        )
        ex1, st1 = w_prep.session()
        memo_for(st1).record_landmark("village", (90, 64, 0), dimension="overworld")
        sk1 = WinMicroVillageRaid(prep_only=True)
        drive(sk1, ex1, st1, max_steps=80)
        self.assertEqual(sk1.verify_done(st1), "")
        self.assertTrue(any(op == "move_to" and req["pos"] and req["pos"][0] >= 88.0 for op, req in w_prep.calls))

        # Subcase B: full raid returns to remote micro_village_bunker before drinking ominous_bottle,
        # and refuses to drink when HP < 10.0
        w_ret = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"iron_sword": 1, "ominous_bottle": 1},
            hp=7.5,
        )
        ex2, st2 = w_ret.session()
        memo_for(st2).record_landmark("micro_village_bunker", (90, 64, 0), dimension="overworld")
        with self.assertRaises(Replan):
            drive(WinMicroVillageRaid(prep_only=False), ex2, st2)
        self.assertTrue(any(op == "move_to" and req["pos"] and req["pos"][0] >= 88.0 for op, req in w_ret.calls))
        self.assertFalse(any(op == "use_item" for op, _ in w_ret.calls))


# ============================================================================
# 4. trigger_totem_safely (adventure/totem_of_undying)
# ============================================================================


class TriggerTotemSafelyTests(_WaveCTestBase):
    def test_blocked_on_empty_inventory_dead_unready_and_normal_no_totem(self) -> None:
        for label, st in (
            ("empty_normal", _state_with(difficulty="normal", items=[])),
            ("empty_easy", _state_with(difficulty="easy", items=[])),
            ("dead", _state_with(alive=False, items=[{"name": "totem_of_undying", "count": 1}, {"name": "cobblestone", "count": 30}])),
            ("not_ready", _state_with(ready=False, items=[{"name": "totem_of_undying", "count": 1}, {"name": "cobblestone", "count": 30}])),
            ("normal_no_totem", _state_with(difficulty="normal", items=[{"name": "iron_sword", "count": 1}, {"name": "cobblestone", "count": 30}])),
            ("hard_no_totem", _state_with(difficulty="hard", items=[{"name": "iron_sword", "count": 1}, {"name": "cobblestone", "count": 30}])),
            ("totem_but_under_25_blocks", _state_with(difficulty="normal", items=[{"name": "totem_of_undying", "count": 1}, {"name": "cobblestone", "count": 10}])),
        ):
            with self.subTest(case=label):
                msg = TriggerTotemSafely.blocked(st)
                self.assertTrue(msg and _ZH_RE.search(msg))

    def test_easy_and_peaceful_difficulty_guard_raises_env_blocked_without_mansion_search(self) -> None:
        for diff in ("easy", "peaceful"):
            with self.subTest(difficulty=diff):
                world = WaveCWorld(
                    pos=(0.5, 64.0, 0.5),
                    difficulty=diff,
                    blocks=_flat(-4, 4, -4, 4, y=63),
                    items={"iron_sword": 1, "cobblestone": 32},
                )
                ex, state = world.session()
                # Non-empty inventory + easy/peaceful + no totem -> blocked() allows entering run()
                self.assertEqual(TriggerTotemSafely.blocked(state), "")
                with self.assertRaises(EnvBlocked) as ctx:
                    drive(TriggerTotemSafely(), ex, state)
                self.assertEqual(ctx.exception.missing_predicate, "totem_of_undying_or_normal_plus_difficulty")
                self.assertEqual(ctx.exception.wake_on, "inventory_changed")
                # Must NOT search for Woodland Mansions (zero find_blocks / move_to calls)
                self.assertEqual([c for c in world.calls if c[0] in ("find_blocks", "move_to")], [])

    def test_rejects_lava_water_honey_bed_and_void_landing_with_replan(self) -> None:
        # Ground-start unsafe landings (including post-pillar actual exit column (1, -1))
        for bad_block in ("lava", "water", "honey_block", "red_bed", None):
            with self.subTest(mode="ground", landing=bad_block or "void"):
                blocks = _flat(-2, 2, -2, 2, y=63, block="stone")
                if bad_block is None:
                    blocks.pop((1, 63, -1), None)
                else:
                    blocks[(1, 63, -1)] = bad_block
                world = WaveCWorld(
                    pos=(0.5, 64.0, 0.5),
                    blocks=blocks,
                    items={"totem_of_undying": 1, "cobblestone": 30},
                )
                ex, state = world.session()
                with self.assertRaises(Replan):
                    drive(TriggerTotemSafely(), ex, state)
                self.assertFalse(any(op == "place" for op, _ in world.calls))
                self.assertFalse(state.is_totem_trigger_armed())

        # Elevated-start unsafe landings (player at (0.5, 89.0, 0.5) on pillar top (0, 88, 0))
        for bad_block in ("lava", "water", "honey_block", "red_bed", None):
            with self.subTest(mode="elevated", landing=bad_block or "void"):
                blocks: dict[tuple[int, int, int], str] = {(0, 88, 0): "cobblestone"}
                if bad_block is not None:
                    blocks[(1, 63, 0)] = bad_block
                world = WaveCWorld(
                    pos=(0.5, 89.0, 0.5),
                    elevated_column=True,
                    blocks=blocks,
                    items={"totem_of_undying": 1},
                )
                ex, state = world.session()
                with self.assertRaises(Replan):
                    drive(TriggerTotemSafely(), ex, state)
                self.assertFalse(any(op == "control" for op, _ in world.calls))
                self.assertFalse(state.is_totem_trigger_armed())

    def test_feather_falling_and_high_protection_armor_reject_without_fake_unequip(self) -> None:
        # Feather Falling boots -> Replan("totem_feather_falling_armor_cannot_unequip...")
        world_ff = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            difficulty="normal",
            blocks=_flat(-3, 3, -3, 3, y=63, block="stone"),
            items={"totem_of_undying": 1, "cobblestone": 32},
            armor={"feet": {"name": "diamond_boots", "count": 1, "enchantments": [{"name": "feather_falling", "level": 4}]}},
        )
        ex1, st1 = world_ff.session()
        with self.assertRaises(Replan) as ctx_ff:
            drive(TriggerTotemSafely(), ex1, st1)
        self.assertIn("totem_feather_falling_armor_cannot_unequip", str(ctx_ff.exception))
        self.assertFalse(any(op == "window_act" for op, _ in world_ff.calls))
        self.assertFalse(any(op == "equip" and req.get("item") == "" for op, req in world_ff.calls))

        # Full Protection IV armor -> Replan("totem_high_protection_armor_cannot_unequip...")
        prot4 = {"enchantments": [{"name": "protection", "level": 4}]}
        world_prot = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            difficulty="normal",
            blocks=_flat(-3, 3, -3, 3, y=63, block="stone"),
            items={"totem_of_undying": 1, "cobblestone": 40},
            armor={
                "head": {"name": "diamond_helmet", "count": 1, **prot4},
                "chest": {"name": "diamond_chestplate", "count": 1, **prot4},
                "legs": {"name": "diamond_leggings", "count": 1, **prot4},
                "feet": {"name": "diamond_boots", "count": 1, **prot4},
            },
        )
        ex2, st2 = world_prot.session()
        with self.assertRaises(Replan) as ctx_prot:
            drive(TriggerTotemSafely(), ex2, st2)
        self.assertIn("totem_high_protection_armor_cannot_unequip", str(ctx_prot.exception))
        self.assertFalse(any(op == "window_act" for op, _ in world_prot.calls))
        self.assertFalse(any(op == "equip" and req.get("item") == "" for op, req in world_prot.calls))

    def test_ground_start_unarmored_pillars_25_uses_actual_top_col_and_multi_poll_fall(self) -> None:
        world = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            difficulty="normal",
            blocks=_flat(-3, 3, -3, 3, y=63, block="stone"),
            items={"totem_of_undying": 1, "cobblestone": 32},
        )
        ex, state = world.session()
        armed_snapshots: list[bool] = []
        poll_phase = 0

        def _on_control(w: WaveCWorld, **keys):
            nonlocal poll_phase
            armed_snapshots.append(state.is_totem_trigger_armed())
            # Stepping off pillar top (0, 89, -1) into mid-air over (1, *, -1)
            w.st["pos"] = [1.5, 82.0, -0.5]
            w.st["on_ground"] = False
            poll_phase = 1
            return {"ok": True, **keys}

        orig_status = world.status

        def _falling_status(timeout: float = 10) -> dict:
            nonlocal poll_phase
            if poll_phase == 1:
                armed_snapshots.append(state.is_totem_trigger_armed())
                world.st["pos"] = [1.5, 72.0, -0.5]
                world.st["on_ground"] = False
                poll_phase = 2
            elif poll_phase == 2:
                armed_snapshots.append(state.is_totem_trigger_armed())
                # Impact ground at (1.5, 64.0, -0.5) and pop offhand totem
                world.st["pos"] = [1.5, 64.0, -0.5]
                world.st["on_ground"] = True
                world.offhand = None
                poll_phase = 3
            return orig_status(timeout=timeout)

        world.on_control = _on_control
        world.status = _falling_status  # type: ignore[method-assign]
        skill = TriggerTotemSafely()
        with patch.object(t4_ominous_combat, "_TOTEM_FALL_POLL_S", 0.0):
            drive(skill, ex, state, max_steps=100)

        self.assertEqual(skill.verify_done(state), "")
        # Guard stayed armed across control() AND all mid-air polling ticks
        self.assertGreaterEqual(len(armed_snapshots), 3)
        self.assertTrue(all(armed_snapshots))
        self.assertFalse(state.is_totem_trigger_armed())
        self.assertIsNone(state._totem_guard)
        self.assertEqual(state.count("totem_of_undying"), 0)

        # Never calls fake unequip APIs
        self.assertFalse(any(op == "window_act" for op, _ in world.calls))
        self.assertFalse(any(op == "equip" and req.get("item") == "" for op, req in world.calls))
        # Equipped totem to off-hand
        self.assertTrue(
            any(op == "equip" and req["item"] == "totem_of_undying" and req["slot"] == "off-hand" for op, req in world.calls)
        )
        # Pillared 25 blocks; actual top cell is (0, 89, -1) and look targets (1.5, 89.0, -0.5)
        places = [req for op, req in world.calls if op == "place"]
        self.assertEqual(len(places), 25)
        self.assertIn((0, 89, -1), memo_for(state).landmarks("totem_pillar", dimension="overworld"))
        looks = [req for op, req in world.calls if op == "look"]
        self.assertEqual(looks[-1]["pos"], [1.5, 89.0, -0.5])

    def test_ordinary_unenchanted_armor_raises_pillar_height_without_unequip_api(self) -> None:
        world = WaveCWorld(
            pos=(0.5, 64.0, 0.5),
            difficulty="normal",
            blocks=_flat(-3, 3, -3, 3, y=63, block="stone"),
            items={"totem_of_undying": 1, "cobblestone": 32},
            armor={
                "head": {"name": "iron_helmet", "count": 1},
                "chest": {"name": "iron_chestplate", "count": 1},
            },
        )
        ex, state = world.session()

        def _on_control(w: WaveCWorld, **keys):
            w.st["pos"] = [1.5, 64.0, 0.5]
            w.st["on_ground"] = True
            w.offhand = None
            return {"ok": True, **keys}

        world.on_control = _on_control
        skill = TriggerTotemSafely()
        drive(skill, ex, state, max_steps=120)

        self.assertEqual(skill.verify_done(state), "")
        self.assertFalse(any(op == "window_act" for op, _ in world.calls))
        self.assertFalse(any(op == "equip" and req.get("item") == "" for op, req in world.calls))
        # Raised pillar height to 28 blocks for ordinary unenchanted armor
        places = [req for op, req in world.calls if op == "place"]
        self.assertEqual(len(places), 28)
        self.assertFalse(state.is_totem_trigger_armed())
        self.assertIsNone(state._totem_guard)

    def test_elevated_start_happy_path_effect_confirmation(self) -> None:
        blocks = _flat(-2, 2, -2, 2, y=63, block="stone")
        blocks[(0, 88, 0)] = "cobblestone"
        world = WaveCWorld(
            pos=(0.5, 89.0, 0.5),
            difficulty="normal",
            elevated_column=True,
            blocks=blocks,
            items={"totem_of_undying": 1},
        )
        ex, state = world.session()
        armed_during_fall: list[bool] = []

        def _on_control(w: WaveCWorld, **keys):
            armed_during_fall.append(state.is_totem_trigger_armed())
            w.st["pos"] = [1.5, 64.0, 0.5]
            # Confirm regeneration + absorption effect branch independently
            w.st["effects"] = {"regeneration": 1, "absorption": 1}
            return {"ok": True, **keys}

        world.on_control = _on_control

        skill = TriggerTotemSafely()
        drive(skill, ex, state, max_steps=40)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(armed_during_fall, [True])
        self.assertFalse(state.is_totem_trigger_armed())
        self.assertIsNone(state._totem_guard)
        self.assertEqual([c for c in world.calls if c[0] == "place"], [])

    def test_disarms_totem_guard_on_control_error_or_unconfirmed_trigger(self) -> None:
        blocks = _flat(-2, 2, -2, 2, y=63, block="stone")
        blocks[(0, 88, 0)] = "cobblestone"

        # Case A: control raises ApiError -> guard MUST be disarmed
        world_err = WaveCWorld(
            pos=(0.5, 89.0, 0.5),
            elevated_column=True,
            blocks=blocks,
            items={"totem_of_undying": 1},
        )
        world_err.on_control = lambda w, **keys: (_ for _ in ()).throw(ApiError("unsupported_api", "no control"))
        ex1, st1 = world_err.session()
        skill1 = TriggerTotemSafely()
        with self.assertRaises(Replan):
            drive(skill1, ex1, st1, max_steps=20)
        self.assertFalse(st1.is_totem_trigger_armed())
        self.assertIsNone(st1._totem_guard)
        self.assertTrue(skill1.verify_done(st1))

        # Case B: control succeeds without consuming totem or granting effects -> Replan + disarmed
        world_noop = WaveCWorld(
            pos=(0.5, 89.0, 0.5),
            elevated_column=True,
            blocks=blocks,
            items={"totem_of_undying": 1},
        )
        ex2, st2 = world_noop.session()
        skill2 = TriggerTotemSafely()
        with self.assertRaises(Replan):
            drive(skill2, ex2, st2, max_steps=20)
        self.assertFalse(st2.is_totem_trigger_armed())
        self.assertIsNone(st2._totem_guard)
        self.assertTrue(skill2.verify_done(st2))


# ============================================================================
# 5. Wave C Ledger, Contract & 3-Way Shared Subgraph Amortization
# ============================================================================


class WaveCLedgerAndContractTests(_WaveCTestBase):
    def test_three_wave_c_skills_registered_offline_contract_no_coord_entity_params_and_empty_guard(self) -> None:
        expected_skills = {
            "unlock_ominous_vault": UnlockOminousVault,
            "win_micro_village_raid": WinMicroVillageRaid,
            "trigger_totem_safely": TriggerTotemSafely,
        }
        forbidden_param_names = {"x", "y", "z", "pos", "position", "coord", "coords", "entity_id", "target_id"}
        empty_normal = _state_with(difficulty="normal", items=[])
        empty_easy = _state_with(difficulty="easy", items=[])

        for name, cls in expected_skills.items():
            with self.subTest(skill=name):
                self.assertIn(name, REGISTRY)
                self.assertIs(REGISTRY[name], cls)
                self.assertEqual(cls.verified, "live")
                self.assertEqual(contract_errors(cls), [])
                self.assertFalse(forbidden_param_names.intersection(cls.params.keys()))
                # Empty-inventory guard blocks all 3 skills in both normal and easy difficulty
                msg_unready = cls.blocked(State())
                msg_empty_normal = cls.blocked(empty_normal)
                msg_empty_easy = cls.blocked(empty_easy)
                self.assertTrue(msg_unready and _ZH_RE.search(msg_unready))
                self.assertTrue(msg_empty_normal and _ZH_RE.search(msg_empty_normal))
                self.assertTrue(msg_empty_easy and _ZH_RE.search(msg_empty_easy))

        skill_file = Path(__file__).resolve().parents[1] / "mcagent" / "skills" / "t4_ominous_combat.py"
        line_count = len(skill_file.read_text(encoding="utf-8").splitlines())
        self.assertLess(line_count, 380, f"t4_ominous_combat.py exceeds 380 lines ({line_count})")

    def test_wave_c_ledger_totals_wip1_draft_stop_loss_and_exact_scores(self) -> None:
        self.assertEqual(validate_ledger(GOALS), [])
        self.assertEqual(len(GOAL_MAP), len(GOALS))

        # Totals monotonically >= Wave C baseline (101 goals / 86 scored / 2580 pts)
        scored_all = [g for g in GOALS if g.score > 0]
        self.assertGreaterEqual(len(GOALS), 101)
        self.assertGreaterEqual(len(scored_all), 86)
        self.assertGreaterEqual(sum(g.score for g in scored_all), 2580)

        # WIP<=1 discipline preserved
        self.assertLessEqual(len([g for g in GOALS if g.stage == "candidate"]), 1)

        # Wave C goals: exact scores (0, 60, 60, 60) totaling +180
        wave_c_goals = [GOAL_MAP[gid] for gid in WAVE_C_ALL_IDS]
        self.assertEqual(tuple(g.score for g in wave_c_goals), (0, 60, 60, 60))
        self.assertEqual(sum(g.score for g in wave_c_goals), 180)
        self.assertEqual(tuple(g.tier for g in wave_c_goals), ("T4_PREP", "T4", "T4", "T4"))

        repo_root = Path(__file__).resolve().parents[2]
        seen_advs: set[str] = set()
        for g in scored_all:
            for adv in g.done_advancements:
                self.assertNotIn(adv, seen_advs, f"duplicate advancement in GOALS: {adv}")
                seen_advs.add(adv)

        for gid in WAVE_C_ALL_IDS:
            with self.subTest(goal=gid):
                goal = GOAL_MAP[gid]
                self.assertEqual(goal.stage, "graduated")
                self.assertTrue(goal.stop_loss)
                self.assertGreater(goal.est_cost_s, 0.0)
                self.assertIn(goal.skill, REGISTRY)
                skill_cls = REGISTRY[goal.skill]
                self.assertEqual(skill_cls.verified, "live")
                self.assertEqual(contract_errors(skill_cls), [])
                _, arg_errs = bind_args(skill_cls, goal.args_dict)
                self.assertEqual(arg_errs, [])
                for rel_path in goal.editable_files:
                    self.assertTrue((repo_root / rel_path).is_file(), f"missing editable file: {rel_path}")
                if goal.score > 0:
                    self.assertEqual(len(goal.done_advancements), 1)
                    adv = goal.done_advancements[0]
                    self.assertIn(adv, ADVANCEMENT_SCORES_V2)
                    self.assertEqual(goal.score, ADVANCEMENT_SCORES_V2[adv])

    def test_rank_actionable_goals_3way_shared_subgraph_amortization_for_prep_ominous_bottle(self) -> None:
        st = _state_with(items=[])
        from mcagent.skills.world_memo import memo_for
        memo = memo_for(st)
        memo.record_landmark("village", [40, 64, 40], dimension="overworld")
        memo.record_landmark("pillager_outpost", [80, 64, 80], dimension="overworld")
        memo.record_landmark("trial_chambers", [120, 40, 120], dimension="overworld")
        wave_c_subgraph = (
            GOAL_MAP["prep_ominous_bottle"],
            GOAL_MAP["revaulting"],
            GOAL_MAP["hero_of_the_village"],
            GOAL_MAP["totem_of_undying"],
        )
        solo_subgraph = (
            GOAL_MAP["prep_ominous_bottle"],
            GOAL_MAP["revaulting"],
        )

        ranked_shared = rank_actionable_goals(st, wave_c_subgraph)
        ranked_solo = rank_actionable_goals(st, solo_subgraph)
        by_step_shared = {step.step_goal.id: step for step in ranked_shared}
        by_step_solo = {step.step_goal.id: step for step in ranked_solo}

        self.assertIn("prep_ominous_bottle", by_step_shared)
        shared_step = by_step_shared["prep_ominous_bottle"]
        solo_step = by_step_solo["prep_ominous_bottle"]

        # 3-way shared across revaulting (+60), hero_of_the_village (+60), and totem_of_undying (+60)
        self.assertEqual(shared_step.shared_dependents, 3)
        self.assertGreaterEqual(shared_step.shared_dependents, 2)
        self.assertGreater(shared_step.marginal_roi, shared_step.roi)
        self.assertEqual(solo_step.shared_dependents, 1)
        self.assertEqual(solo_step.marginal_roi, solo_step.roi)
        self.assertGreater(shared_step.marginal_roi, solo_step.marginal_roi)

        # Full transitive resolution with upstream prereqs already satisfied
        active = resolve_active_goals(
            GOALS, target_goal=("revaulting", "hero_of_the_village", "totem_of_undying")
        )
        completed = {
            "minecraft:story/mine_stone",
            "minecraft:story/upgrade_tools",
            "minecraft:story/smelt_iron",
            "minecraft:adventure/minecraft_trials_edition",
            "minecraft:adventure/trade",
        }
        full_ranked = rank_actionable_goals(st, active, completed_advs=completed)
        by_step_full = {step.step_goal.id: step for step in full_ranked}
        self.assertIn("prep_ominous_bottle", by_step_full)
        self.assertGreaterEqual(by_step_full["prep_ominous_bottle"].shared_dependents, 2)
        self.assertGreater(
            by_step_full["prep_ominous_bottle"].marginal_roi,
            by_step_full["prep_ominous_bottle"].roi,
        )


if __name__ == "__main__":
    unittest.main()
