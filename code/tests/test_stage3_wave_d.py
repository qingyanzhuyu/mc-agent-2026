"""Stage 3 Wave D (3 T4 Skills: Strider Boost, Overworld Strider Ride & Opportunistic Trident) offline unit tests."""
from __future__ import annotations

import math
import re
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from mcagent.client import ApiError
from mcagent.loop.draft_scores import ADVANCEMENT_SCORES_V2
from mcagent.loop.goals import (
    GOAL_MAP,
    GOALS,
    rank_actionable_goals,
    resolve_active_goals,
    validate_ledger,
)
from mcagent.runtime import EnvBlocked, Replan, State
from mcagent.skills import REGISTRY, bind_args, contract_errors
from mcagent.skills.t4_strider_and_trident import (
    RideStriderBoost,
    RideStriderOverworld,
    ThrowOpportunisticTrident,
)
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches
from mcagent.skills.world_memo import memo_for, reset_all_world_memos

WAVE_D_IDS: tuple[str, ...] = (
    "ride_strider",
    "ride_strider_in_overworld_lava",
    "throw_trident",
)

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")


def _flat(
    x0: int, x1: int, z0: int, z1: int, y: int = 63, block: str = "stone"
) -> dict[tuple[int, int, int], str]:
    return {(x, y, z): block for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)}


def _lava_track(
    start_x: int,
    y: int,
    z: int,
    length: int = 55,
    *,
    dx: int = 1,
    dz: int = 0,
    lava_block: str = "lava",
    lava_props: dict[str, Any] | None = None,
    roof_y: int | None = None,
    roof_block: str = "cobblestone",
) -> tuple[dict[tuple[int, int, int], str], dict[tuple[int, int, int], dict[str, Any]]]:
    blocks: dict[tuple[int, int, int], str] = {}
    props: dict[tuple[int, int, int], dict[str, Any]] = {}
    for i in range(length):
        cx, cz = start_x + i * dx, z + i * dz
        blocks[(cx, y, cz)] = lava_block
        if lava_props is not None:
            props[(cx, y, cz)] = deepcopy(lava_props)
        if roof_y is not None:
            blocks[(cx, roof_y, cz)] = roof_block
    return blocks, props


def _state_with(
    *,
    ready: bool = True,
    alive: bool = True,
    dimension: str = "overworld",
    pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
    in_vehicle: Any = None,
    items: list[dict[str, Any]] | None = None,
    held: dict[str, Any] | None = None,
    offhand: dict[str, Any] | None = None,
    armor: dict[str, Any] | None = None,
) -> State:
    state = State()
    st: dict[str, Any] = {
        "ready": ready,
        "alive": alive,
        "dimension": dimension,
        "health": 20.0,
        "food": 20.0,
        "pos": list(pos),
        "on_ground": True,
        "sneaking": False,
        "blocking": False,
        "jumping": False,
    }
    if in_vehicle is not None:
        st["in_vehicle"] = deepcopy(in_vehicle)
    state.set_status(st)

    inv: dict[str, Any] = {"items": deepcopy(list(items or []))}
    if held is not None:
        inv["held"] = deepcopy(held)
    if offhand is not None:
        inv["offhand"] = deepcopy(offhand)
    if armor is not None:
        inv["armor"] = deepcopy(armor)
    state.set_inventory(inv)
    return state


class WaveDWorld(GridWorld):
    """Offline deterministic world double supporting all Stage 3 Wave D skills."""

    def __init__(
        self,
        pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
        *,
        dimension: str = "overworld",
        in_vehicle: Any = None,
        blocks: dict[tuple[int, int, int], str] | None = None,
        block_props: dict[tuple[int, int, int], dict[str, Any]] | None = None,
        items: dict[str, int] | None = None,
        item_stacks: list[dict[str, Any]] | None = None,
        held: dict[str, Any] | None = None,
        offhand: dict[str, Any] | None = None,
        armor: dict[str, dict[str, Any]] | None = None,
        entities: list[dict[str, Any]] | None = None,
        completed_advs: set[str] | None = None,
    ):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st.update(
            dimension=dimension,
            blocking=False,
            jumping=False,
        )
        if in_vehicle is not None:
            self.st["in_vehicle"] = deepcopy(in_vehicle)
        self.block_props: dict[tuple[int, int, int], dict[str, Any]] = {
            tuple(k): deepcopy(v) for k, v in (block_props or {}).items()
        }
        self.item_stacks: list[dict[str, Any]] | None = (
            deepcopy(item_stacks) if item_stacks is not None else None
        )
        self.held: dict[str, Any] | None = deepcopy(held)
        self.offhand: dict[str, Any] | None = deepcopy(offhand)
        self.armor: dict[str, dict[str, Any]] = deepcopy(armor or {})
        self.entities: list[dict[str, Any]] = [deepcopy(e) for e in (entities or ())]
        self.completed_advs: set[str] = set(completed_advs or ())

        self.on_move_to: Callable[..., dict | None] | None = None
        self.on_interact: Callable[..., dict | None] | None = None
        self.on_use_item: Callable[..., dict | None] | None = None
        self.on_place: Callable[..., dict | None] | None = None
        self.on_dismount: Callable[..., dict | None] | None = None
        self.on_shoot: Callable[..., dict | None] | None = None
        self.on_look: Callable[..., dict | None] | None = None
        self.on_equip: Callable[..., dict | None] | None = None

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
            if self.held and self.held.get("name") == item:
                if self._total_item_count(item) <= 0:
                    self.held = None
            return
        self._need(item)
        self.items[item] -= count
        if self.held and self.held.get("name") == item and self.items[item] <= 0:
            self.held = None

    def _add_item(self, item: str, count: int = 1, **extra: Any) -> None:
        if self.item_stacks is not None:
            next_slot = max((int(s.get("slot", 0)) for s in self.item_stacks), default=-1) + 1
            self.item_stacks.append({"name": item, "count": count, "slot": next_slot, **extra})
            return
        self.items[item] = self.items.get(item, 0) + count

    def damage_item(self, item: str, delta: int = 1) -> None:
        """Drop `[cur, max]` durability on both `item_stacks` and `held` for `item`."""
        updated_stack: dict[str, Any] | None = None
        if self.item_stacks is not None:
            for s in self.item_stacks:
                if s.get("name") == item and isinstance(s.get("durability"), list):
                    s["durability"][0] = max(0, int(s["durability"][0]) - delta)
                    updated_stack = s
                    break
        if self.held and self.held.get("name") == item:
            if updated_stack is not None:
                self.held = deepcopy(updated_stack)
            elif isinstance(self.held.get("durability"), list):
                self.held["durability"][0] = max(0, int(self.held["durability"][0]) - delta)

    def status(self, timeout: float = 10) -> dict:
        return deepcopy(self.st)

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

    def advancements(self) -> dict:
        return {"completed": sorted(self.completed_advs)}

    def get_block(self, pos, use_cache: bool = True) -> dict:
        self.calls.append(("get_block", {"pos": list(pos)}))
        key = tuple(pos)
        blk: dict[str, Any] = {"name": self.blocks.get(key, "air")}
        if key in self.block_props:
            blk["properties"] = deepcopy(self.block_props[key])
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
        if self._total_item_count(item) < 1 and not (self.held and self.held.get("name") == item):
            raise ApiError("missing_item", f"背包没有 {item}")
        if self.item_stacks is not None:
            match = next((s for s in self.item_stacks if s.get("name") == item and int(s.get("count", 0)) > 0), None)
            chosen = deepcopy(match) if match is not None else {"name": item, "count": 1}
        else:
            chosen = {"name": item, "count": max(1, self._total_item_count(item))}
        if slot == "off-hand":
            self.offhand = chosen
        else:
            self.held = chosen
        return {"equipped": item, "slot": slot}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        self.calls.append(("move_to", {"x": x, "y": y, "z": z, "entity_id": entity_id, "pos": list(pos) if pos is not None else None, "rng": rng}))
        if self.on_move_to is not None:
            res = self.on_move_to(self, pos=pos, x=x, y=y, z=z, entity_id=entity_id, rng=rng)
            if res is not None:
                return res
        dest = [float(v) for v in pos] if pos is not None else [
            float(x), float(self.st["pos"][1] if y is None else y), float(z),
        ]
        self.st["pos"] = dest
        return {"pos": list(dest)}

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
        return {"used": item, "target": target}

    def place(self, item, pos=None, *, stop_on=None) -> dict:
        self.calls.append(("place", {"item": item, "pos": list(pos) if pos is not None else None}))
        if self.on_place is not None:
            res = self.on_place(self, item=item, pos=list(pos) if pos is not None else None)
            if res is not None:
                return res
        self._consume_item(item, 1)
        x, y, z = self.st["pos"]
        if list(pos) in ([math.floor(x), math.floor(y) + dy, math.floor(z)] for dy in (0, 1)):
            raise ApiError("blocked_by_self", "目标格与自身碰撞箱重叠")
        self.blocks[tuple(pos)] = item
        return {"placed": item, "pos": list(pos)}

    def dismount(self, *, stop_on=None) -> dict:
        self.calls.append(("dismount", {}))
        if self.on_dismount is not None:
            res = self.on_dismount(self)
            if res is not None:
                return res
        self.st.pop("in_vehicle", None)
        return {"dismounted": True}

    def shoot(self, entity_id=None, pos=None, weapon="bow", charge_ms=1100, *, stop_on=None) -> dict:
        self.calls.append(
            ("shoot", {"entity_id": entity_id, "pos": list(pos) if pos is not None else None, "weapon": weapon, "charge_ms": charge_ms})
        )
        if self.on_shoot is not None:
            res = self.on_shoot(self, entity_id=entity_id, pos=pos, weapon=weapon, charge_ms=charge_ms)
            if res is not None:
                return res
        self.entities = [e for e in self.entities if e.get("id") != entity_id]
        return {"hit": True, "entity_id": entity_id, "weapon": weapon, "killed": True}

    def look(self, yaw=None, pitch=None, *, pos=None, entity_id=None) -> dict:
        self.calls.append(
            ("look", {"yaw": yaw, "pitch": pitch, "pos": list(pos) if pos is not None else None, "entity_id": entity_id})
        )
        if self.on_look is not None:
            res = self.on_look(self, yaw=yaw, pitch=pitch, pos=pos, entity_id=entity_id)
            if res is not None:
                return res
        return {"ok": True}


class _WaveDTestBase(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        reset_all_voxel_caches()


# ============================================================================
# 1. ride_strider_boost (nether/ride_strider, +30 pts)
# ============================================================================


class RideStriderBoostTests(_WaveDTestBase):
    def test_blocked_rejects_invalid_states_and_allows_landmark_or_riding(self) -> None:
        blocked_cases = (
            ("empty_inv", _state_with(dimension="the_nether", items=[])),
            ("not_ready", _state_with(ready=False, dimension="the_nether", items=[
                {"name": "warped_fungus_on_a_stick", "count": 1}, {"name": "saddle", "count": 1},
            ])),
            ("dead", _state_with(alive=False, dimension="the_nether", items=[
                {"name": "warped_fungus_on_a_stick", "count": 1}, {"name": "saddle", "count": 1},
            ])),
            ("missing_rod", _state_with(dimension="the_nether", items=[{"name": "saddle", "count": 1}])),
            ("missing_saddle_no_landmark_not_riding", _state_with(
                dimension="the_nether", items=[{"name": "warped_fungus_on_a_stick", "count": 1}],
            )),
        )
        for label, st in blocked_cases:
            with self.subTest(blocked_case=label):
                msg = RideStriderBoost.blocked(st)
                self.assertTrue(msg and _ZH_RE.search(msg), f"expected Chinese block msg for {label}")

        # Dimension-scoped landmark: overworld landmark must NOT unblock the_nether
        st_wrong_dim = _state_with(
            dimension="the_nether", items=[{"name": "warped_fungus_on_a_stick", "count": 1}]
        )
        memo_for(st_wrong_dim).record_landmark("saddled_strider", (10, 32, 10), dimension="overworld")
        self.assertTrue(RideStriderBoost.blocked(st_wrong_dim))

        # Allowed #1: saddled_strider landmark exists in current dimension
        st_landmark = _state_with(
            dimension="the_nether", items=[{"name": "warped_fungus_on_a_stick", "count": 1}]
        )
        memo_for(st_landmark).record_landmark("saddled_strider", (10, 32, 10), dimension="the_nether")
        self.assertEqual(RideStriderBoost.blocked(st_landmark), "")

        # Allowed #2: already riding strider (dict or string) with rod
        for veh in ({"name": "strider", "id": 77}, "minecraft:strider"):
            with self.subTest(in_vehicle=veh):
                st_riding = _state_with(
                    dimension="the_nether",
                    in_vehicle=veh,
                    items=[{"name": "warped_fungus_on_a_stick", "count": 1}],
                )
                self.assertEqual(RideStriderBoost.blocked(st_riding), "")

    def test_missing_strider_raises_env_blocked(self) -> None:
        world = WaveDWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_nether",
            blocks=_flat(-2, 2, -2, 2, y=63, block="netherrack"),
            item_stacks=[
                {"name": "warped_fungus_on_a_stick", "count": 1, "slot": 0, "durability": [100, 100]},
                {"name": "saddle", "count": 1, "slot": 1},
            ],
            entities=[],
        )
        ex, state = world.session()
        skill = RideStriderBoost()
        with self.assertRaises(EnvBlocked) as cm:
            drive(skill, ex, state, max_steps=20)
        self.assertEqual(cm.exception.missing_predicate, "visible_entity:strider")
        self.assertTrue(skill.verify_done(state))

    def _build_shore_strider_world(self) -> WaveDWorld:
        # Solid shore x in [-2..4], lava lake x in [5..12] at y=63
        blocks = _flat(-2, 4, -3, 3, y=63, block="netherrack")
        for x in range(5, 13):
            for z in range(-3, 4):
                blocks[(x, 63, z)] = "lava"
        return WaveDWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_nether",
            blocks=blocks,
            item_stacks=[
                {"name": "warped_fungus_on_a_stick", "count": 1, "slot": 0, "durability": [100, 100]},
                {"name": "saddle", "count": 1, "slot": 1},
            ],
            entities=[{"id": 101, "name": "strider", "type": "strider", "pos": [6.5, 64.0, 0.5], "saddled": False}],
        )

    def test_happy_path_local_b13_use_item_pass(self) -> None:
        world = self._build_shore_strider_world()
        move_destinations: list[list[float]] = []

        def _on_move_to(w: WaveDWorld, pos=None, **_kw):
            dest = [float(v) for v in pos]
            move_destinations.append(dest)
            # Floor beneath destination must be solid netherrack, NEVER lava
            floor = w.blocks.get((math.floor(dest[0]), math.floor(dest[1]) - 1, math.floor(dest[2])), "air")
            self.assertNotEqual(floor, "lava", f"bot walked into lava at {dest}")
            w.st["pos"] = dest
            # Strider lured closer to shore edge once player approaches
            w.entities[0]["pos"] = [5.5, 64.0, 0.5]
            return {"pos": list(dest)}

        def _on_interact(w: WaveDWorld, pos=None, entity_id=None, item=None):
            if entity_id == 101 and item == "saddle":
                w._consume_item("saddle", 1)
                w.entities[0]["saddled"] = True
                return {"used": {"saddle": 1}}
            if entity_id == 101 and item is None:
                w.st["in_vehicle"] = {"name": "strider", "id": 101}
                w.st["pos"] = list(w.entities[0]["pos"])
                return {"mounted": True}
            return None

        def _on_use_item(w: WaveDWorld, item: str, target=None):
            if item == "warped_fungus_on_a_stick":
                w.damage_item("warped_fungus_on_a_stick", 1)
                return {"used": item}
            return None

        world.on_move_to = _on_move_to
        world.on_interact = _on_interact
        world.on_use_item = _on_use_item

        ex, state = world.session()
        skill = RideStriderBoost()
        drive(skill, ex, state, max_steps=40)

        self.assertEqual(skill.verify_done(state), "")
        self.assertTrue(move_destinations, "expected shore approach move_to")
        self.assertEqual(state.count("saddle"), 0)
        self.assertIsNone(world.st.get("in_vehicle"))
        self.assertTrue(any(op == "dismount" for op, _ in world.calls))
        self.assertTrue(any(op == "use_item" and req["item"] == "warped_fungus_on_a_stick" for op, req in world.calls))
        self.assertEqual(world.held["durability"], [99, 100])
        self.assertTrue(memo_for(state).has_landmark("saddled_strider", dimension="the_nether"))

    def test_happy_path_official_cannot_use_falls_back_to_move_to_pass(self) -> None:
        world = self._build_shore_strider_world()

        def _on_move_to(w: WaveDWorld, pos=None, **_kw):
            dest = [float(v) for v in pos]
            w.st["pos"] = dest
            if w.st.get("in_vehicle"):
                # Official §4.5 steering fallback triggers boost and drops rod durability
                w.damage_item("warped_fungus_on_a_stick", 1)
            else:
                w.entities[0]["pos"] = [5.5, 64.0, 0.5]
            return {"pos": list(dest)}

        def _on_interact(w: WaveDWorld, pos=None, entity_id=None, item=None):
            if entity_id == 101 and item == "saddle":
                w._consume_item("saddle", 1)
                w.entities[0]["saddled"] = True
                return {"used": {"saddle": 1}}
            if entity_id == 101 and item is None:
                w.st["in_vehicle"] = {"name": "strider", "id": 101}
                w.st["pos"] = list(w.entities[0]["pos"])
                return {"mounted": True}
            return None

        def _on_use_item(w: WaveDWorld, item: str, target=None):
            raise ApiError("cannot_use", "official §4.5 rejects warped_fungus_on_a_stick")

        world.on_move_to = _on_move_to
        world.on_interact = _on_interact
        world.on_use_item = _on_use_item

        ex, state = world.session()
        skill = RideStriderBoost()
        steps = drive(skill, ex, state, max_steps=40)

        self.assertEqual(skill.verify_done(state), "")
        self.assertIsNone(world.st.get("in_vehicle"))
        self.assertTrue(any(op == "dismount" for op, _ in world.calls))
        self.assertEqual(world.held["durability"], [98, 100])
        # Verify failure rec for use_item(cannot_use) AND fallback move_to rec
        self.assertTrue(
            any(isinstance(s, dict) and s.get("op") == "use_item" and s.get("error_code") == "cannot_use" for s in steps)
        )
        self.assertTrue(
            any(isinstance(s, dict) and s.get("op") == "move_to" and s.get("b13_fallback") is True for s in steps)
        )
        self.assertTrue(memo_for(state).has_landmark("saddled_strider", dimension="the_nether"))

    def test_honest_b13_unsupported_failure_raises_replan_without_blocking_finally_dismount(self) -> None:
        for fallback_mode in ("noop_move_to", "failing_move_to"):
            with self.subTest(fallback_mode=fallback_mode):
                reset_all_world_memos()
                world = WaveDWorld(
                    pos=(1.5, 64.0, 0.5),
                    dimension="the_nether",
                    blocks=_flat(-2, 4, -2, 2, y=63, block="netherrack"),
                    item_stacks=[
                        {"name": "warped_fungus_on_a_stick", "count": 1, "slot": 0, "durability": [100, 100]},
                        {"name": "saddle", "count": 1, "slot": 1},
                    ],
                    entities=[{"id": 102, "name": "strider", "type": "strider", "pos": [2.5, 64.0, 0.5], "saddled": False}],
                )

                def _on_interact(w: WaveDWorld, pos=None, entity_id=None, item=None):
                    if item == "saddle":
                        w._consume_item("saddle", 1)
                        w.entities[0]["saddled"] = True
                        return {"used": {"saddle": 1}}
                    w.st["in_vehicle"] = {"name": "strider", "id": 102}
                    return {"mounted": True}

                def _on_use_item(w: WaveDWorld, item: str, target=None):
                    raise ApiError("cannot_use", "endpoint does not support rod use_item")

                def _on_move_to(w: WaveDWorld, pos=None, **_kw):
                    if fallback_mode == "failing_move_to":
                        raise ApiError("move_failed", "steering blocked")
                    # Moves without reducing durability or confirming boost
                    w.st["pos"] = [float(v) for v in pos]
                    return {"pos": list(w.st["pos"])}

                world.on_interact = _on_interact
                world.on_use_item = _on_use_item
                world.on_move_to = _on_move_to

                ex, state = world.session()
                skill = RideStriderBoost()
                with self.assertRaises(Replan) as cm:
                    drive(skill, ex, state, max_steps=40)
                self.assertIn("api_b13_unsupported", str(cm.exception))
                self.assertTrue(skill.verify_done(state))
                # Blocking dismount MUST NOT be invoked un-yielded in finally
                self.assertFalse(any(op == "dismount" for op, _ in world.calls))
                self.assertIsNotNone(world.st.get("in_vehicle"))


# ============================================================================
# 2. ride_strider_overworld (nether/ride_strider_in_overworld_lava, +60 pts)
# ============================================================================


class RideStriderOverworldTests(_WaveDTestBase):
    def test_blocked_rejects_invalid_states_and_nether_transfer_without_lure(self) -> None:
        blocked_cases = (
            ("empty_inv", _state_with(dimension="overworld", items=[])),
            ("not_ready", _state_with(ready=False, dimension="overworld", items=[
                {"name": "warped_fungus_on_a_stick", "count": 1}, {"name": "saddle", "count": 1},
            ])),
            ("dead", _state_with(alive=False, dimension="overworld", items=[
                {"name": "warped_fungus_on_a_stick", "count": 1}, {"name": "saddle", "count": 1},
            ])),
            ("the_end", _state_with(dimension="the_end", items=[
                {"name": "warped_fungus_on_a_stick", "count": 1}, {"name": "saddle", "count": 1},
            ])),
            ("missing_rod", _state_with(dimension="overworld", items=[{"name": "saddle", "count": 1}])),
            ("missing_saddle", _state_with(dimension="overworld", items=[
                {"name": "warped_fungus_on_a_stick", "count": 1},
            ])),
            ("nether_without_lead_or_fungus_or_rod", _state_with(
                dimension="the_nether", items=[{"name": "saddle", "count": 1}, {"name": "cobblestone", "count": 8}],
            )),
        )
        for label, st in blocked_cases:
            with self.subTest(blocked_case=label):
                msg = RideStriderOverworld.blocked(st)
                self.assertTrue(msg and _ZH_RE.search(msg), f"expected block msg for {label}")

        # Allowed in Overworld with rod + saddle, and in Nether with rod + saddle + lead
        self.assertEqual(
            RideStriderOverworld.blocked(
                _state_with(dimension="overworld", items=[
                    {"name": "warped_fungus_on_a_stick", "count": 1}, {"name": "saddle", "count": 1},
                ])
            ),
            "",
        )
        self.assertEqual(
            RideStriderOverworld.blocked(
                _state_with(dimension="the_nether", items=[
                    {"name": "warped_fungus_on_a_stick", "count": 1},
                    {"name": "saddle", "count": 1},
                    {"name": "lead", "count": 1},
                ])
            ),
            "",
        )

    def _build_overworld_track_world(
        self,
        *,
        lava_y: int,
        shore_y: int,
        roof_y: int | None,
        roof_block: str = "cobblestone",
        lava_block: str = "lava",
        lava_props: dict[str, Any] | None = None,
        extra_items: list[dict[str, Any]] | None = None,
    ) -> WaveDWorld:
        blocks = _flat(-2, 0, -2, 2, y=shore_y, block="stone")
        tblocks, tprops = _lava_track(
            1, lava_y, 0, length=55, dx=1, dz=0,
            lava_block=lava_block, lava_props=lava_props,
            roof_y=roof_y, roof_block=roof_block,
        )
        blocks.update(tblocks)
        blocks.update(_flat(56, 57, -1, 1, y=shore_y, block="stone"))
        stacks = [
            {"name": "warped_fungus_on_a_stick", "count": 1, "slot": 0, "durability": [100, 100]},
            {"name": "saddle", "count": 1, "slot": 1},
        ]
        if extra_items:
            stacks.extend(extra_items)
        feet_y = float(shore_y + 1)
        return WaveDWorld(
            pos=(0.5, feet_y, 0.5),
            dimension="overworld",
            blocks=blocks,
            block_props=tprops,
            item_stacks=stacks,
            entities=[{"id": 201, "name": "strider", "type": "strider", "pos": [1.5, feet_y, 0.5], "saddled": False}],
        )

    def _wire_mount_and_boost(self, world: WaveDWorld) -> None:
        def _on_interact(w: WaveDWorld, pos=None, entity_id=None, item=None):
            if entity_id is not None and item == "saddle":
                w._consume_item("saddle", 1)
                for e in w.entities:
                    if e["id"] == entity_id:
                        e["saddled"] = True
                return {"used": {"saddle": 1}}
            if entity_id is not None and item is None:
                w.st["in_vehicle"] = {"name": "strider", "id": entity_id}
                ent = next(e for e in w.entities if e["id"] == entity_id)
                w.st["pos"] = list(ent["pos"])
                return {"mounted": True}
            if pos is not None and item == "lava_bucket":
                w._consume_item("lava_bucket", 1)
                w.blocks[tuple(pos)] = "lava"
                w.block_props.pop(tuple(pos), None)
                return {"used": {"lava_bucket": 1}}
            return None

        def _on_use_item(w: WaveDWorld, item: str, target=None):
            if item == "warped_fungus_on_a_stick":
                w.damage_item("warped_fungus_on_a_stick", 1)
                return {"used": item}
            return None

        world.on_interact = _on_interact
        world.on_use_item = _on_use_item

    def test_straight_line_50_block_ride_natural_deep_lava_sea_and_artificial_trench(self) -> None:
        scenarios = (
            ("natural_deep_lava_sea_y_minus_54", -54, -54, -46, "deepslate"),
            ("artificial_trench_y_64", 64, 63, 67, "cobblestone"),
        )
        for label, lava_y, shore_y, roof_y, roof_block in scenarios:
            with self.subTest(track=label):
                reset_all_world_memos()
                world = self._build_overworld_track_world(
                    lava_y=lava_y, shore_y=shore_y, roof_y=roof_y, roof_block=roof_block,
                )
                self._wire_mount_and_boost(world)
                ex, state = world.session()
                skill = RideStriderOverworld()
                drive(skill, ex, state, max_steps=60)

                self.assertEqual(skill.verify_done(state), "")
                self.assertGreaterEqual(skill._net_horiz_dist, 50.0)
                self.assertIsNone(world.st.get("in_vehicle"))
                self.assertTrue(any(op == "dismount" for op, _ in world.calls))
                get_blocks = [c for c in world.calls if c[0] == "get_block"]
                self.assertLess(len(get_blocks), 25, f"unexpected get_block HTTP storm: {len(get_blocks)}")
                self.assertTrue(any(op == "find_blocks" for op, _ in world.calls))

    def test_rejects_turn_back_and_short_displacement_without_lava_dismount(self) -> None:
        cases = (
            ("turn_back_to_net_20", [16.5, 31.5, 26.5, 21.5], 20.0),
            ("stall_at_net_49", [16.5, 31.5, 46.5, 50.5], 49.0),
        )
        for label, xs, expected_net in cases:
            with self.subTest(case=label):
                reset_all_world_memos()
                world = self._build_overworld_track_world(lava_y=64, shore_y=63, roof_y=67)
                self._wire_mount_and_boost(world)
                idx = {"n": 0}

                def _on_move_to(w: WaveDWorld, pos=None, **_kw):
                    k = min(idx["n"], len(xs) - 1)
                    idx["n"] += 1
                    w.st["pos"] = [xs[k], w.st["pos"][1], w.st["pos"][2]]
                    return {"pos": list(w.st["pos"])}

                world.on_move_to = _on_move_to
                ex, state = world.session()
                skill = RideStriderOverworld()
                with self.assertRaises(Replan):
                    drive(skill, ex, state, max_steps=60)
                self.assertAlmostEqual(skill._net_horiz_dist, expected_net, places=3)
                self.assertTrue(skill.verify_done(state))
                self.assertFalse(any(op == "dismount" for op, _ in world.calls))
                self.assertIsNotNone(world.st.get("in_vehicle"))

    def test_rejects_flowing_lava_and_open_sky_track_without_rainproof_roof(self) -> None:
        # Case A: flowing_lava mid-track
        world_flow = self._build_overworld_track_world(lava_y=64, shore_y=63, roof_y=67)
        world_flow.blocks[(10, 64, 0)] = "flowing_lava"

        # Case B: lava with level=1 mid-track
        world_lvl = self._build_overworld_track_world(lava_y=64, shore_y=63, roof_y=67)
        world_lvl.block_props[(10, 64, 0)] = {"level": 1}

        # Case C: open-sky lava track without roof and no blocks
        world_sky = self._build_overworld_track_world(lava_y=64, shore_y=63, roof_y=None)

        # Case D: open-sky roof gap out of reach even when cobblestone is in bag
        world_far_gap = self._build_overworld_track_world(
            lava_y=64, shore_y=63, roof_y=67,
            extra_items=[{"name": "cobblestone", "count": 16, "slot": 2}],
        )
        world_far_gap.blocks.pop((20, 67, 0), None)

        for label, world in (
            ("flowing_lava", world_flow),
            ("lava_level_1", world_lvl),
            ("open_sky_no_blocks", world_sky),
            ("unreachable_roof_gap", world_far_gap),
        ):
            with self.subTest(case=label):
                reset_all_world_memos()
                ex, state = world.session()
                skill = RideStriderOverworld()
                with self.assertRaises(EnvBlocked) as cm:
                    drive(skill, ex, state, max_steps=40)
                self.assertEqual(cm.exception.missing_predicate, "overworld_lava_straight_track")
                self.assertTrue(skill.verify_done(state))

    def test_in_reach_lava_fill_and_roof_repair_completes_ride(self) -> None:
        world = self._build_overworld_track_world(
            lava_y=64,
            shore_y=63,
            roof_y=67,
            extra_items=[
                {"name": "lava_bucket", "count": 1, "slot": 2},
                {"name": "cobblestone", "count": 4, "slot": 3},
            ],
        )
        # Remove 1 in-reach lava cell and 1 in-reach roof cell at x=2 (within reach of (0.5, 64.0, 0.5))
        world.blocks[(2, 64, 0)] = "flowing_lava"
        world.blocks.pop((2, 67, 0), None)
        self._wire_mount_and_boost(world)

        ex, state = world.session()
        skill = RideStriderOverworld()
        drive(skill, ex, state, max_steps=60)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(world.blocks[(2, 64, 0)], "lava")
        self.assertEqual(world.blocks[(2, 67, 0)], "cobblestone")
        self.assertTrue(any(op == "interact" and req["pos"] == [2, 64, 0] and req["item"] == "lava_bucket" for op, req in world.calls))
        self.assertTrue(any(op == "place" and req["pos"] == [2, 67, 0] and req["item"] == "cobblestone" for op, req in world.calls))
        self.assertGreaterEqual(skill._net_horiz_dist, 50.0)

    def test_nether_to_overworld_portal_transfer_phase_a_and_missing_portal(self) -> None:
        # Subtest 1: missing nether_portal raises EnvBlocked(missing_predicate="nether_portal")
        world_no_portal = WaveDWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_nether",
            blocks=_flat(-2, 4, -2, 2, y=63, block="netherrack"),
            item_stacks=[
                {"name": "warped_fungus_on_a_stick", "count": 1, "slot": 0, "durability": [100, 100]},
                {"name": "saddle", "count": 1, "slot": 1},
                {"name": "lead", "count": 1, "slot": 2},
            ],
            entities=[{"id": 301, "name": "strider", "type": "strider", "pos": [2.0, 64.0, 0.5], "saddled": False}],
        )
        ex0, st0 = world_no_portal.session()
        skill0 = RideStriderOverworld()
        with self.assertRaises(EnvBlocked) as cm:
            drive(skill0, ex0, st0, max_steps=20)
        self.assertEqual(cm.exception.missing_predicate, "nether_portal")

        # Subtest 2 & 3: Phase A transfer with lead OR warped_fungus -> Phase B straight-line Overworld ride
        for transfer_item in ("lead", "warped_fungus"):
            with self.subTest(transfer_item=transfer_item):
                reset_all_world_memos()
                nether_blocks = _flat(-2, 5, -2, 2, y=63, block="netherrack")
                nether_blocks[(4, 64, 0)] = "nether_portal"
                ow_blocks = _flat(-2, 0, -2, 2, y=63, block="stone")
                tblocks, _ = _lava_track(1, 64, 0, length=55, dx=1, dz=0, roof_y=67, roof_block="cobblestone")
                ow_blocks.update(tblocks)
                ow_blocks.update(_flat(56, 57, -1, 1, y=63, block="stone"))

                world = WaveDWorld(
                    pos=(0.5, 64.0, 0.5),
                    dimension="the_nether",
                    blocks=nether_blocks,
                    item_stacks=[
                        {"name": "warped_fungus_on_a_stick", "count": 1, "slot": 0, "durability": [100, 100]},
                        {"name": "saddle", "count": 1, "slot": 1},
                        {"name": transfer_item, "count": 1, "slot": 2},
                    ],
                    entities=[{"id": 302, "name": "strider", "type": "strider", "pos": [1.5, 64.0, 0.5], "saddled": False}],
                )
                self._wire_mount_and_boost(world)
                base_interact = world.on_interact

                def _on_interact_portal(w: WaveDWorld, pos=None, entity_id=None, item=None):
                    if entity_id == 302 and item == "lead":
                        w._consume_item("lead", 1)
                        return {"leashed": True}
                    return base_interact(w, pos=pos, entity_id=entity_id, item=item)

                def _on_move_to_portal(w: WaveDWorld, pos=None, **_kw):
                    dest = [float(v) for v in pos]
                    if w.st["dimension"] == "the_nether" and math.dist(dest, [4.5, 64.0, 0.5]) < 1.0:
                        # Portal transition to Overworld with strider beside the covered lava trench
                        w.st["dimension"] = "overworld"
                        w.st["pos"] = [0.5, 64.0, 0.5]
                        w.blocks = deepcopy(ow_blocks)
                        w.entities = [{"id": 302, "name": "strider", "type": "strider", "pos": [1.5, 64.0, 0.5], "saddled": False}]
                        return {"pos": [0.5, 64.0, 0.5], "dimension": "overworld"}
                    w.st["pos"] = dest
                    return {"pos": list(dest)}

                world.on_interact = _on_interact_portal
                world.on_move_to = _on_move_to_portal

                ex, state = world.session()
                skill = RideStriderOverworld()
                drive(skill, ex, state, max_steps=60)

                self.assertEqual(skill.verify_done(state), "")
                self.assertEqual(world.st["dimension"], "overworld")
                self.assertGreaterEqual(skill._net_horiz_dist, 50.0)
                if transfer_item == "lead":
                    self.assertTrue(any(op == "interact" and req["item"] == "lead" for op, req in world.calls))
                else:
                    self.assertTrue(any(op == "equip" and req["item"] == "warped_fungus" for op, req in world.calls))

        # Subtest 4: Strider too far (>3.5 blocks) from portal blocks crossing alone
        reset_all_world_memos()
        nether_blocks = _flat(-4, 5, -2, 2, y=63, block="netherrack")
        nether_blocks[(4, 64, 0)] = "nether_portal"
        w_far = WaveDWorld(
            pos=(3.5, 64.0, 0.5),
            dimension="the_nether",
            blocks=nether_blocks,
            item_stacks=[
                {"name": "warped_fungus_on_a_stick", "count": 1, "slot": 0, "durability": [100, 100]},
                {"name": "saddle", "count": 1, "slot": 1},
                {"name": "lead", "count": 1, "slot": 2},
            ],
            entities=[{"id": 303, "name": "strider", "type": "strider", "pos": [-3.5, 64.0, 0.5], "saddled": False}],
        )
        ex_f, st_f = w_far.session()
        with self.assertRaises(Replan):
            drive(RideStriderOverworld(), ex_f, st_f, max_steps=20)
        self.assertEqual(w_far.st["dimension"], "the_nether")


# ============================================================================
# 3. throw_opportunistic_trident (adventure/throw_trident, +60 pts)
# ============================================================================


class ThrowOpportunisticTridentTests(_WaveDTestBase):
    def test_strict_passive_guard_blocked_rules(self) -> None:
        # Empty / unready / dead
        self.assertTrue(ThrowOpportunisticTrident.blocked(_state_with(items=[])))
        self.assertTrue(ThrowOpportunisticTrident.blocked(_state_with(ready=False, items=[{"name": "trident", "count": 1}])))
        self.assertTrue(ThrowOpportunisticTrident.blocked(_state_with(alive=False, items=[{"name": "trident", "count": 1}])))

        # Non-empty combat gear with NO trident -> strictly blocked (never farms drowned)
        combat_no_trident = _state_with(items=[
            {"name": "diamond_sword", "count": 1},
            {"name": "bow", "count": 1},
            {"name": "arrow", "count": 32},
            {"name": "cobblestone", "count": 64},
        ])
        msg_no_tri = ThrowOpportunisticTrident.blocked(combat_no_trident)
        self.assertTrue(msg_no_tri and _ZH_RE.search(msg_no_tri))

        # Only riptide-enchanted trident (dict, list, components forms) -> strictly blocked
        riptide_stacks = (
            ("dict_ench", {"name": "trident", "count": 1, "enchantments": {"riptide": 3}}),
            ("list_ench", {"name": "trident", "count": 1, "enchantments": [{"name": "minecraft:riptide", "level": 1}]}),
            ("components_ench", {"name": "trident", "count": 1, "components": {"minecraft:enchantments": {"minecraft:riptide": 2}}}),
        )
        for label, stack in riptide_stacks:
            with self.subTest(riptide_format=label):
                msg = ThrowOpportunisticTrident.blocked(_state_with(items=[stack]))
                self.assertTrue(msg and _ZH_RE.search(msg))

        # Clean non-riptide trident -> immediately unblocked
        self.assertEqual(
            ThrowOpportunisticTrident.blocked(_state_with(items=[{"name": "trident", "count": 1}])),
            "",
        )
        self.assertEqual(
            ThrowOpportunisticTrident.blocked(
                _state_with(items=[{"name": "trident", "count": 1, "enchantments": {"loyalty": 3}}])
            ),
            "",
        )

        # BOTH a riptide trident AND a clean trident -> unblocked
        self.assertEqual(
            ThrowOpportunisticTrident.blocked(
                _state_with(items=[
                    {"name": "trident", "count": 1, "slot": 0, "enchantments": {"riptide": 3}},
                    {"name": "trident", "count": 1, "slot": 1},
                ])
            ),
            "",
        )

    def test_happy_path_prefers_armor_stand_ignores_unsafe_mobs_and_retrieves_non_loyalty_trident(self) -> None:
        world = WaveDWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-12, 12, -12, 12, y=63, block="grass_block"),
            item_stacks=[{"name": "trident", "count": 1, "slot": 0}],
            entities=[
                {"id": 401, "name": "villager", "type": "villager", "pos": [2.5, 64.0, 0.5], "health": 20.0},
                {"id": 402, "name": "iron_golem", "type": "iron_golem", "pos": [3.5, 64.0, 0.5], "health": 100.0},
                {"id": 403, "name": "drowned", "type": "drowned", "pos": [4.5, 64.0, 0.5], "health": 20.0},
                {"id": 404, "name": "wolf", "type": "wolf", "pos": [3.0, 64.0, 1.5], "health": 20.0, "tamed": True},
                {"id": 405, "name": "horse", "type": "horse", "pos": [3.0, 64.0, -1.5], "health": 20.0, "owner": "player1"},
                {"id": 406, "name": "cow", "type": "cow", "pos": [5.5, 64.0, 0.5], "health": 10.0},
                {"id": 407, "name": "armor_stand", "type": "armor_stand", "pos": [9.5, 64.0, 0.5], "health": 2.0},
            ],
        )

        def _on_shoot(w: WaveDWorld, entity_id=None, pos=None, weapon=None, charge_ms=1100):
            # Non-loyalty trident leaves inventory on throw
            w._consume_item("trident", 1)
            w.entities = [e for e in w.entities if e["id"] != entity_id]
            return {"hit": True, "entity_id": entity_id, "weapon": weapon}

        def _on_move_to(w: WaveDWorld, pos=None, **_kw):
            dest = [float(v) for v in pos]
            w.st["pos"] = dest
            if math.dist(dest, [9.5, 64.0, 0.5]) < 1.5 and w._total_item_count("trident") == 0:
                w._add_item("trident", 1)
            return {"pos": list(dest)}

        world.on_shoot = _on_shoot
        world.on_move_to = _on_move_to

        ex, state = world.session()
        skill = ThrowOpportunisticTrident()
        steps = drive(skill, ex, state, max_steps=30)

        self.assertEqual(skill.verify_done(state), "")
        shoots = [req for op, req in world.calls if op == "shoot"]
        self.assertEqual(len(shoots), 1)
        self.assertEqual(shoots[0]["entity_id"], 407)  # Preferred armor_stand over closer cow & unsafe mobs
        self.assertEqual(shoots[0]["weapon"], "trident")
        # Retrieved non-loyalty trident after count dropped
        self.assertTrue(any(isinstance(s, dict) and s.get("retrieve_trident") is True for s in steps))
        self.assertEqual(state.count("trident"), 1)

    def test_no_safe_passive_or_armor_stand_raises_env_blocked(self) -> None:
        world = WaveDWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-6, 6, -6, 6, y=63, block="grass_block"),
            item_stacks=[{"name": "trident", "count": 1, "slot": 0}],
            entities=[
                {"id": 501, "name": "villager", "type": "villager", "pos": [3.5, 64.0, 0.5], "health": 20.0},
                {"id": 502, "name": "iron_golem", "type": "iron_golem", "pos": [4.5, 64.0, 0.5], "health": 100.0},
                {"id": 503, "name": "drowned", "type": "drowned", "pos": [5.5, 64.0, 0.5], "health": 20.0},
                {"id": 504, "name": "horse", "type": "horse", "pos": [6.5, 64.0, 0.5], "health": 20.0, "tamed": True},
            ],
        )
        ex, state = world.session()
        skill = ThrowOpportunisticTrident()
        with self.assertRaises(EnvBlocked) as cm:
            drive(skill, ex, state, max_steps=20)
        self.assertEqual(cm.exception.missing_predicate, "safe_passive_or_armor_stand")
        self.assertTrue(skill.verify_done(state))

    def test_missed_throw_raises_replan_and_verify_done_fails(self) -> None:
        for receipt in ({"fired": True, "hit": False}, {"ok": True, "hit": False}):
            with self.subTest(receipt=receipt):
                world = WaveDWorld(
                    pos=(0.5, 64.0, 0.5),
                    blocks=_flat(-6, 8, -6, 6, y=63, block="grass_block"),
                    item_stacks=[{"name": "trident", "count": 1, "slot": 0}],
                    entities=[{"id": 601, "name": "cow", "type": "cow", "pos": [6.5, 64.0, 0.5], "health": 10.0}],
                )
                world.on_shoot = lambda w, entity_id=None, pos=None, weapon=None, charge_ms=1100, rcpt=receipt: dict(rcpt)
                ex, state = world.session()
                skill = ThrowOpportunisticTrident()
                with self.assertRaises(Replan):
                    drive(skill, ex, state, max_steps=20)
                self.assertTrue(skill.verify_done(state))

    def test_preemption_replan_during_trident_retrieval_is_not_swallowed(self) -> None:
        world = WaveDWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-6, 8, -6, 6, y=63, block="grass_block"),
            item_stacks=[{"name": "trident", "count": 1, "slot": 0}],
            entities=[{"id": 601, "name": "cow", "type": "cow", "pos": [5.5, 64.0, 0.5], "health": 10.0}],
        )

        def _hit_then_drop(w: WaveDWorld, entity_id=None, pos=None, weapon=None, charge_ms=1100):
            w._consume_item("trident", 1)
            w.entities.append({"id": 701, "name": "trident", "type": "item", "pos": [5.5, 64.0, 0.5]})
            return {"hit": True, "entity_id": entity_id}

        world.on_shoot = _hit_then_drop
        world.on_move_to = lambda w, pos=None, **_kw: (_ for _ in ()).throw(Replan("danger: creeper_nearby"))
        ex, state = world.session()
        skill = ThrowOpportunisticTrident()
        with self.assertRaises(Replan) as cm:
            drive(skill, ex, state, max_steps=20)
        self.assertIn("creeper_nearby", str(cm.exception))


# ============================================================================
# 4. Wave D Ledger, Contracts & Passive Opportunistic Ranking
# ============================================================================


class WaveDLedgerAndContractTests(_WaveDTestBase):
    def test_three_wave_d_skills_registered_offline_contract_scalar_params_line_cap_and_empty_guard(self) -> None:
        expected_skills = {
            "ride_strider_boost": RideStriderBoost,
            "ride_strider_overworld": RideStriderOverworld,
            "throw_opportunistic_trident": ThrowOpportunisticTrident,
        }
        forbidden_param_names = {"x", "y", "z", "pos", "position", "coord", "coords", "entity_id", "target_id"}
        allowed_scalar_types = (str, int, float, bool)
        empty_ready = _state_with(dimension="overworld", items=[])
        empty_nether = _state_with(dimension="the_nether", items=[])

        for name, cls in expected_skills.items():
            with self.subTest(skill=name):
                self.assertIn(name, REGISTRY)
                self.assertIs(REGISTRY[name], cls)
                self.assertEqual(cls.verified, "offline")
                self.assertEqual(contract_errors(cls), [])
                self.assertFalse(forbidden_param_names.intersection(cls.params.keys()))
                for pname, spec in cls.params.items():
                    self.assertIn(spec.type, allowed_scalar_types, f"non-scalar param {pname} on {name}")
                for st in (State(), empty_ready, empty_nether):
                    msg = cls.blocked(st)
                    self.assertTrue(msg and _ZH_RE.search(msg))

        skill_file = Path(__file__).resolve().parents[1] / "mcagent" / "skills" / "t4_strider_and_trident.py"
        line_count = len(skill_file.read_text(encoding="utf-8").splitlines())
        self.assertLess(line_count, 350, f"t4_strider_and_trident.py exceeds 350 lines ({line_count})")

    def test_stage3_final_ledger_totals_wip1_wave_d_metadata_and_exact_scores(self) -> None:
        self.assertEqual(validate_ledger(GOALS), [])
        self.assertEqual(len(GOAL_MAP), len(GOALS))

        # WIP=1 discipline preserved: smelt_iron is sole candidate
        self.assertEqual([g.id for g in GOALS if g.stage == "candidate"], ["smelt_iron"])

        # Exact Stage 3 final totals: 104 goals / 89 scored / 2,730 pts (89 / 99, 80.6% of 3,385)
        scored_all = [g for g in GOALS if g.score > 0]
        total_pts = sum(g.score for g in scored_all)
        self.assertEqual(len(GOALS), 104)
        self.assertEqual(len(scored_all), 89)
        self.assertEqual(total_pts, 2730)
        self.assertEqual(len(ADVANCEMENT_SCORES_V2), 99)
        self.assertEqual(sum(ADVANCEMENT_SCORES_V2.values()), 3385)
        self.assertEqual(round(total_pts / sum(ADVANCEMENT_SCORES_V2.values()) * 100, 1), 80.6)

        # Wave D goals: tier="T4", stage="draft", stop_loss=True, scores (30, 60, 60) = +150
        wave_d_goals = [GOAL_MAP[gid] for gid in WAVE_D_IDS]
        self.assertEqual(tuple(g.score for g in wave_d_goals), (30, 60, 60))
        self.assertEqual(sum(g.score for g in wave_d_goals), 150)
        self.assertEqual(tuple(g.tier for g in wave_d_goals), ("T4", "T4", "T4"))

        repo_root = Path(__file__).resolve().parents[2]
        seen_advs: set[str] = set()
        for g in scored_all:
            for adv in g.done_advancements:
                self.assertNotIn(adv, seen_advs, f"duplicate advancement in GOALS: {adv}")
                seen_advs.add(adv)

        for gid in WAVE_D_IDS:
            with self.subTest(goal=gid):
                goal = GOAL_MAP[gid]
                self.assertEqual(goal.stage, "draft")
                self.assertTrue(goal.stop_loss)
                self.assertGreater(goal.est_cost_s, 0.0)
                self.assertIn(goal.skill, REGISTRY)
                skill_cls = REGISTRY[goal.skill]
                self.assertEqual(skill_cls.verified, "offline")
                self.assertEqual(contract_errors(skill_cls), [])
                _, arg_errs = bind_args(skill_cls, goal.args_dict)
                self.assertEqual(arg_errs, [])
                for rel_path in goal.editable_files:
                    self.assertTrue((repo_root / rel_path).is_file(), f"missing editable file: {rel_path}")
                self.assertEqual(len(goal.done_advancements), 1)
                adv = goal.done_advancements[0]
                self.assertIn(adv, ADVANCEMENT_SCORES_V2)
                self.assertEqual(goal.score, ADVANCEMENT_SCORES_V2[adv])

    def test_passive_opportunistic_ranking_contract_for_throw_trident(self) -> None:
        st_no_trident = _state_with(dimension="overworld", items=[])
        ranked_without = rank_actionable_goals(st_no_trident, GOALS)
        self.assertTrue(ranked_without)
        # Without trident: omitted from passive campaign ranking; prep_wood -> shiny_gear stays rank #0
        self.assertFalse(any(s.root_goal.id == "throw_trident" for s in ranked_without))
        self.assertFalse(any(s.step_goal.id == "throw_trident" for s in ranked_without))
        self.assertEqual(ranked_without[0].step_goal.id, "prep_wood")
        self.assertEqual(ranked_without[0].root_goal.id, "shiny_gear")

        # The instant trident enters inventory: throw_trident jumps immediately to rank #0 (roi=2.0)
        st_with_trident = _state_with(
            dimension="overworld",
            items=[{"name": "trident", "count": 1, "slot": 0}],
        )
        ranked_with = rank_actionable_goals(st_with_trident, GOALS)
        self.assertTrue(ranked_with)
        self.assertEqual(ranked_with[0].step_goal.id, "throw_trident")
        self.assertEqual(ranked_with[0].root_goal.id, "throw_trident")
        self.assertEqual(ranked_with[0].roi, 2.0)
        self.assertEqual(ranked_with[0].marginal_roi, 2.0)

        # Explicit target_goal="throw_trident" surfaces throw_trident even without trident
        active_explicit = resolve_active_goals(GOALS, target_goal="throw_trident")
        ranked_explicit = rank_actionable_goals(
            st_no_trident, active_explicit, target_goal="throw_trident"
        )
        self.assertEqual(len(ranked_explicit), 1)
        self.assertEqual(ranked_explicit[0].step_goal.id, "throw_trident")
        self.assertEqual(ranked_explicit[0].root_goal.id, "throw_trident")


if __name__ == "__main__":
    unittest.main()
