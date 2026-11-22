"""Stage 3 Wave B (5 T4 Skills + 12 Goals: 7 T4_PREP + 5 T4) offline unit tests."""
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
from mcagent.loop.reserve import ReservationBook
from mcagent.runtime import EnvBlocked, Replan, State
from mcagent.skills import REGISTRY, bind_args, contract_errors
from mcagent.skills.t4_arbalistic_and_height import ArbalisticTrenchShot, TradeWorldHeight
from mcagent.skills.t4_nether_endgame import (
    ARMOR_SLOTS,
    ActivateBeacon,
    CraftNetheriteArmorSet,
    SummonAndTrapWither,
)
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches
from mcagent.skills.world_memo import memo_for, reset_all_world_memos

WAVE_B_PREP_IDS: tuple[str, ...] = (
    "prep_high_altitude_portal",
    "prep_piercing_iv_crossbow",
    "prep_five_unique_mobs_pack",
    "prep_looting_sword",
    "prep_wither_3_skulls",
    "prep_nether_star",
    "prep_netherite_upgrade_set",
)
WAVE_B_SCORED_IDS: tuple[str, ...] = (
    "trade_at_world_height",
    "arbalistic",
    "netherite_armor",
    "summon_wither",
    "create_beacon",
)
WAVE_B_ALL_IDS: tuple[str, ...] = WAVE_B_PREP_IDS + WAVE_B_SCORED_IDS

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")
_TEMPLATE = "netherite_upgrade_smithing_template"


def _flat(x0: int, x1: int, z0: int, z1: int, y: int = 63, block: str = "stone") -> dict[tuple[int, int, int], str]:
    return {(x, y, z): block for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)}


def _state_with(
    *,
    ready: bool = True,
    alive: bool = True,
    dimension: str = "overworld",
    biome: str = "plains",
    health: float = 20.0,
    food: float = 20.0,
    pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
    items: list[dict[str, Any]] | None = None,
    held: dict[str, Any] | None = None,
    offhand: dict[str, Any] | None = None,
    armor: dict[str, dict[str, Any]] | None = None,
    dirty: bool = False,
    effects: Any = None,
) -> State:
    state = State()
    st: dict[str, Any] = {
        "ready": ready,
        "alive": alive,
        "dimension": dimension,
        "biome": biome,
        "health": health,
        "food": food,
        "pos": list(pos),
        "on_ground": True,
        "sneaking": False,
        "blocking": False,
        "jumping": False,
    }
    if effects is not None:
        st["effects"] = effects
    state.set_status(st)
    inv: dict[str, Any] = {"items": list(items or [])}
    if held is not None:
        inv["held"] = dict(held)
    if offhand is not None:
        inv["offhand"] = dict(offhand)
    if armor is not None:
        inv["armor"] = deepcopy(armor)
    state.set_inventory(inv)
    if dirty:
        state.invalidate_inventory()
    return state


class WaveBWorld(GridWorld):
    """Offline deterministic world double supporting all Stage 3 Wave B client methods."""

    def __init__(
        self,
        pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
        *,
        dimension: str = "overworld",
        biome: str = "plains",
        blocks: dict[tuple[int, int, int], str] | None = None,
        items: dict[str, int] | None = None,
        item_meta: dict[str, dict[str, Any]] | None = None,
        held: dict[str, Any] | None = None,
        entities: list[dict[str, Any]] | None = None,
        hp: float = 20.0,
        food: float = 20.0,
        adv_snapshot: dict[str, Any] | None = None,
    ):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st.update(
            dimension=dimension,
            biome=biome,
            health=hp,
            food=food,
            blocking=False,
            jumping=False,
        )
        self.item_meta: dict[str, dict[str, Any]] = deepcopy(item_meta or {})
        self.held: dict[str, Any] | None = deepcopy(held)
        self.entities: list[dict[str, Any]] = [deepcopy(e) for e in (entities or ())]
        self.adv_snapshot: dict[str, Any] = (
            deepcopy(adv_snapshot) if adv_snapshot is not None else {"advancements": []}
        )

        self.on_move_to: Callable[..., dict | None] | None = None
        self.on_dig: Callable[..., dict | None] | None = None
        self.on_place: Callable[..., dict | None] | None = None
        self.on_craft: Callable[..., dict | None] | None = None
        self.on_interact: Callable[..., dict | None] | None = None
        self.on_use_item: Callable[..., dict | None] | None = None
        self.on_attack: Callable[..., dict | None] | None = None
        self.on_shoot: Callable[..., dict | None] | None = None
        self.on_look: Callable[..., dict | None] | None = None
        self.on_control: Callable[..., dict | None] | None = None
        self.on_window_view: Callable[..., dict] | None = None
        self.on_window_act: Callable[..., dict] | None = None
        self.on_advancements: Callable[..., dict] | None = None

    def inventory(self, timeout: float = 10) -> dict:
        rows = []
        for idx, (name, cnt) in enumerate(self.items.items()):
            if cnt > 0:
                row: dict[str, Any] = {"name": name, "count": cnt, "slot": idx}
                if name in self.item_meta:
                    row.update(deepcopy(self.item_meta[name]))
                rows.append(row)
        inv: dict[str, Any] = {"items": rows}
        if self.held is not None and self.items.get(self.held["name"], 0) > 0:
            held_row = dict(self.held)
            held_row["count"] = self.items[self.held["name"]]
            if self.held["name"] in self.item_meta:
                held_row.update(deepcopy(self.item_meta[self.held["name"]]))
            inv["held"] = held_row
        return inv

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
                hits.append({"name": bname, "pos": [bx, by, bz], "dist": round(d, 2)})
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
        res = super().equip(item, slot=slot, stop_on=stop_on)
        if slot == "hand":
            self.held = {"name": item, "count": self.items.get(item, 1)}
            if item in self.item_meta:
                self.held.update(deepcopy(self.item_meta[item]))
        return res

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        if self.on_move_to is not None:
            res = self.on_move_to(self, pos=pos, x=x, y=y, z=z, entity_id=entity_id, rng=rng)
            if res is not None:
                self.calls.append(("move_to", {"x": x, "y": y, "z": z, "entity_id": entity_id, "pos": pos, "rng": rng}))
                return res
        if pos is None and entity_id is not None:
            ent = next((e for e in self.entities if e.get("id") == entity_id), None)
            if ent is None:
                raise ApiError("entity_not_found", f"实体 {entity_id} 不存在")
            pos = [float(v) for v in ent["pos"]]
        return super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)

    def dig(self, pos, collect=True, *, stop_on=None) -> dict:
        if self.on_dig is not None:
            res = self.on_dig(self, pos=list(pos), collect=collect)
            if res is not None:
                self.calls.append(("dig", {"pos": list(pos), "collect": collect}))
                return res
        return super().dig(pos, collect=collect, stop_on=stop_on)

    def place(self, item, pos=None, *, stop_on=None) -> dict:
        if self.on_place is not None:
            res = self.on_place(self, item=item, pos=list(pos) if pos is not None else None)
            if res is not None:
                self.calls.append(("place", {"item": item, "pos": list(pos) if pos is not None else None}))
                return res
        return super().place(item, pos=pos, stop_on=stop_on)

    def craft(self, item, count=1, *, stop_on=None) -> dict:
        self.calls.append(("craft", {"item": item, "count": count}))
        if self.on_craft is not None:
            res = self.on_craft(self, item=item, count=count)
            if res is not None:
                return res
        if item in (_TEMPLATE, "netherite_ingot", "beacon"):
            px, py, pz = self.st["pos"]
            if not any(
                b == "crafting_table" and math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5)) <= 16.0
                for (bx, by, bz), b in self.blocks.items()
            ):
                raise ApiError("need_crafting_table", f"合成 {item} 需要附近有工作台")
        if item == _TEMPLATE:
            if self.items.get(_TEMPLATE, 0) < 1 or self.items.get("diamond", 0) < 7 * count or self.items.get("netherrack", 0) < count:
                raise ApiError("missing_item", "缺少复制锻造模板材料")
            self.items["diamond"] -= 7 * count
            self.items["netherrack"] -= count
            self.items[_TEMPLATE] += count
            return {"crafted": item, "count": count}
        if item == "netherite_ingot":
            if self.items.get("netherite_scrap", 0) < 4 * count or self.items.get("gold_ingot", 0) < 4 * count:
                raise ApiError("missing_item", "缺少下界合金锭材料")
            self.items["netherite_scrap"] -= 4 * count
            self.items["gold_ingot"] -= 4 * count
            self.items["netherite_ingot"] = self.items.get("netherite_ingot", 0) + count
            return {"crafted": item, "count": count}
        if item == "beacon":
            if self.items.get("nether_star", 0) < count or self.items.get("obsidian", 0) < 3 * count or self.items.get("glass", 0) < 5 * count:
                raise ApiError("missing_item", "缺少信标合成材料")
            self.items["nether_star"] -= count
            self.items["obsidian"] -= 3 * count
            self.items["glass"] -= 5 * count
            self.items["beacon"] = self.items.get("beacon", 0) + count
            return {"crafted": item, "count": count}
        self.items[item] = self.items.get(item, 0) + count
        return {"crafted": item, "count": count}

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None) -> dict:
        self.calls.append(("interact", {"pos": list(pos) if pos is not None else None, "entity_id": entity_id, "item": item}))
        if item:
            self._need(item)
        if self.on_interact is not None:
            res = self.on_interact(self, pos=pos, entity_id=entity_id, item=item)
            if res is not None:
                return res
        return {"changed": True}

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None) -> dict:
        self.calls.append(("use_item", {"item": item, "target": deepcopy(target)}))
        self._need(item)
        if self.on_use_item is not None:
            res = self.on_use_item(self, item=item, target=target)
            if res is not None:
                return res
        self.items[item] -= 1
        return {"used": item, "target": target}

    def attack(self, entity_id, timeout_ms=20000, *, max_hits=None, interval_ms=None, stop_on=None) -> dict:
        self.calls.append(("attack", {"entity_id": entity_id, "max_hits": max_hits}))
        if self.on_attack is not None:
            res = self.on_attack(self, entity_id=entity_id, max_hits=max_hits)
            if res is not None:
                return res
        return {"entity_id": entity_id, "hits": max_hits or 1, "killed": True}

    def shoot(self, entity_id=None, pos=None, weapon="bow", charge_ms=1100, *, stop_on=None) -> dict:
        self.calls.append(("shoot", {"entity_id": entity_id, "pos": list(pos) if pos is not None else None, "weapon": weapon}))
        if self.on_shoot is not None:
            res = self.on_shoot(self, entity_id=entity_id, pos=pos, weapon=weapon)
            if res is not None:
                return res
        return {"hit": True, "entity_id": entity_id, "weapon": weapon}

    def look(self, yaw=None, pitch=None, *, pos=None, entity_id=None) -> dict:
        self.calls.append(("look", {"yaw": yaw, "pitch": pitch, "pos": list(pos) if pos is not None else None, "entity_id": entity_id}))
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
        return {"ok": True, **keys}

    def window_view(self, target, *, stop_on=None) -> dict:
        self.calls.append(("window_view", {"target": deepcopy(target)}))
        if self.on_window_view is not None:
            return self.on_window_view(self, target=target)
        return {"window": "generic_9x3", "sections": {"container": []}}

    def window_act(self, target, ops, *, stop_on=None) -> dict:
        self.calls.append(("window_act", {"target": deepcopy(target), "ops": deepcopy(ops)}))
        tpos = tuple(target.get("pos", ())) if isinstance(target, dict) and target.get("pos") else None
        if tpos is not None and self.blocks.get(tpos) == "hopper":
            for op in ops:
                if op.get("op") == "take" and op.get("from") != "container":
                    raise ApiError("missing_param", "hopper take requires from='container'")
        if self.on_window_act is not None:
            return self.on_window_act(self, target=target, ops=ops)
        return {"ok": True, "ops": deepcopy(ops)}

    def advancements(self, status="all", include_recipes=False) -> dict:
        self.calls.append(("advancements", {"status": status, "include_recipes": include_recipes}))
        if self.on_advancements is not None:
            return self.on_advancements(self, status=status, include_recipes=include_recipes)
        return deepcopy(self.adv_snapshot)


def _attach_merchant(
    world: WaveBWorld,
    entity_id: int,
    options: list[dict[str, Any]],
    *,
    window_type: str = "merchant",
    reward_meta: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Attach a deterministic villager/wandering_trader window handler to `world`."""
    prev_view = world.on_window_view
    prev_act = world.on_window_act

    def _view(w: WaveBWorld, target: dict) -> dict:
        if target.get("entity_id") == entity_id:
            return {"window": window_type, "sections": {}, "options": deepcopy(options)}
        if prev_view is not None:
            return prev_view(w, target=target)
        return {"window": "generic_9x3", "sections": {"container": []}}

    def _act(w: WaveBWorld, target: dict, ops: list[dict]) -> dict:
        if target.get("entity_id") == entity_id:
            for op in ops:
                if op.get("op") == "select":
                    idx = int(op["option"])
                    opt = options[idx]
                    give = opt["give"]
                    give_list = [give] if isinstance(give, dict) else list(give)
                    for g in give_list:
                        gname = g["name"]
                        gcnt = int(g.get("count", 1))
                        if w.items.get(gname, 0) < gcnt:
                            raise ApiError("missing_item", f"交易缺少 {gname}")
                        w.items[gname] -= gcnt
                    got = opt["get"]
                    rname = got if isinstance(got, str) else got["name"]
                    rcnt = 1 if isinstance(got, str) else int(got.get("count", 1))
                    w.items[rname] = w.items.get(rname, 0) + rcnt
                    if reward_meta and rname in reward_meta:
                        w.item_meta[rname] = deepcopy(reward_meta[rname])
            return {"ok": True, "ops": deepcopy(ops)}
        if prev_act is not None:
            return prev_act(w, target=target, ops=ops)
        return {"ok": True, "ops": deepcopy(ops)}

    world.on_window_view = _view
    world.on_window_act = _act


def _attach_smithing_table(world: WaveBWorld, table_pos: tuple[int, int, int]) -> None:
    """Attach a stateful smithing table simulator supporting diamond -> netherite upgrades."""
    slots: dict[str, list[dict[str, Any]]] = {"template": [], "base": [], "addition": [], "output": []}
    prev_view = world.on_window_view
    prev_act = world.on_window_act

    def _refresh_output() -> None:
        if slots["template"] and slots["base"] and slots["addition"]:
            tname = slots["template"][0]["name"]
            bname = slots["base"][0]["name"]
            aname = slots["addition"][0]["name"]
            if tname == _TEMPLATE and aname == "netherite_ingot" and bname.startswith("diamond_"):
                slots["output"] = [{"name": bname.replace("diamond_", "netherite_", 1), "count": 1}]
                return
        slots["output"] = []

    def _view(w: WaveBWorld, target: dict) -> dict:
        if target.get("pos") == list(table_pos):
            _refresh_output()
            return {"window": "smithing_table", "sections": deepcopy(slots)}
        if prev_view is not None:
            return prev_view(w, target=target)
        return {"window": "generic_9x3", "sections": {"container": []}}

    def _act(w: WaveBWorld, target: dict, ops: list[dict]) -> dict:
        if target.get("pos") == list(table_pos):
            for op in ops:
                kind = op.get("op")
                if kind == "put":
                    item = op["item"]
                    cnt = int(op.get("count", 1))
                    to = op["to"]
                    if w.items.get(item, 0) < cnt:
                        raise ApiError("missing_item", f"锻造缺少 {item}")
                    w.items[item] -= cnt
                    slots[to].append({"name": item, "count": cnt})
                    _refresh_output()
                elif kind == "take" and op.get("from") == "output":
                    _refresh_output()
                    if not slots["output"]:
                        raise ApiError("empty_output", "锻造台无产物")
                    out = slots["output"].pop(0)
                    slots["template"].clear()
                    slots["base"].clear()
                    slots["addition"].clear()
                    w.items[out["name"]] = w.items.get(out["name"], 0) + int(out.get("count", 1))
            return {"ok": True, "ops": deepcopy(ops)}
        if prev_act is not None:
            return prev_act(w, target=target, ops=ops)
        return {"ok": True, "ops": deepcopy(ops)}

    world.on_window_view = _view
    world.on_window_act = _act


class _WaveBTestBase(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        reset_all_voxel_caches()


# ============================================================================
# 1. Wave B Skills Contract & Empty-Inventory Blocking
# ============================================================================


class WaveBSkillsContractTests(_WaveBTestBase):
    def test_five_wave_b_skills_registered_offline_contract_empty_blocked_and_line_budget(self) -> None:
        expected_skills = {
            "trade_world_height": TradeWorldHeight,
            "arbalistic_trench_shot": ArbalisticTrenchShot,
            "craft_netherite_armor_set": CraftNetheriteArmorSet,
            "summon_and_trap_wither": SummonAndTrapWither,
            "activate_beacon": ActivateBeacon,
        }
        empty_ready = _state_with(items=[])
        for name, cls in expected_skills.items():
            with self.subTest(skill=name):
                self.assertIn(name, REGISTRY)
                self.assertIs(REGISTRY[name], cls)
                self.assertEqual(cls.verified, "live")
                self.assertEqual(contract_errors(cls), [])
                unready_msg = cls.blocked(State())
                empty_msg = cls.blocked(empty_ready)
                self.assertTrue(unready_msg and _ZH_RE.search(unready_msg), f"{name} unready blocked must be Chinese")
                self.assertTrue(empty_msg and _ZH_RE.search(empty_msg), f"{name} empty blocked must be Chinese")

        skills_dir = Path(__file__).resolve().parents[1] / "mcagent" / "skills"
        for fname in ("t4_arbalistic_and_height.py", "t4_nether_endgame.py"):
            with self.subTest(file=fname):
                line_count = len((skills_dir / fname).read_text(encoding="utf-8").splitlines())
                self.assertLess(line_count, 380, f"{fname} exceeds 380 lines ({line_count})")


# ============================================================================
# 2. trade_world_height (3D Portal Anchoring + High-Altitude Trade)
# ============================================================================


class TradeWorldHeightTests(_WaveBTestBase):
    def test_high_altitude_wandering_trader_fast_path(self) -> None:
        world = WaveBWorld(
            pos=(0.5, 314.0, 0.5),
            dimension="overworld",
            blocks=_flat(-2, 2, -2, 2, y=318),
            items={"emerald": 4},
            entities=[{"id": 77, "name": "wandering_trader", "pos": [1.5, 319.0, 0.5]}],
        )
        _attach_merchant(
            world,
            77,
            [{"index": 0, "give": {"name": "emerald", "count": 1}, "get": {"name": "blue_ice", "count": 1}}],
            window_type="wandering_trader",
        )
        ex, state = world.session()
        skill = TradeWorldHeight()
        drive(skill, ex, state)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("blue_ice"), 1)
        self.assertEqual(state.count("emerald"), 3)
        self.assertGreaterEqual(world.st["pos"][1], 319.0)
        wacts = [req for op, req in world.calls if op == "window_act"]
        self.assertTrue(any(r["target"] == {"entity_id": 77} for r in wacts))

    def test_overworld_low_altitude_3d_portal_anchoring_prep_only_and_sneak_cleanup(self) -> None:
        blocks = _flat(-4, 6, -4, 4, y=311)
        # Village temporary nether portal + frame obsidian to unlink
        blocks[(4, 312, 0)] = "nether_portal"
        blocks[(4, 312, 1)] = "obsidian"
        world = WaveBWorld(
            pos=(0.5, 312.0, 0.5),
            dimension="overworld",
            blocks=blocks,
            items={
                "cobblestone": 32,
                "obsidian": 10,
                "flint_and_steel": 1,
                "diamond_pickaxe": 1,
                "emerald": 2,
            },
            entities=[{"id": 12, "name": "villager", "pos": [2.5, 312.0, 0.5]}],
        )
        ex, state = world.session()
        skill = TradeWorldHeight(prep_only=True)
        drive(skill, ex, state, max_steps=120)

        self.assertEqual(skill.verify_done(state), "")
        # Village temp portal obsidian was dug (unlinked)
        digs = [req["pos"] for op, req in world.calls if op == "dig"]
        self.assertIn([4, 312, 1], digs)
        # High portal ring (10 obsidian) placed at Y=314..318 and ignited with flint_and_steel
        obs_places = [req["pos"] for op, req in world.calls if op == "place" and req["item"] == "obsidian"]
        self.assertEqual(len(obs_places), 10)
        self.assertTrue(all(314 <= p[1] <= 318 for p in obs_places))
        ignites = [req for op, req in world.calls if op == "interact" and req["item"] == "flint_and_steel"]
        self.assertEqual(len(ignites), 1)
        self.assertEqual(ignites[0]["pos"][1], 314)
        # 3x3 platform blocks placed at Y=318 (standing surface Y=319)
        y318_blocks = [pos for pos, bname in world.blocks.items() if pos[1] == 318 and bname == "cobblestone"]
        self.assertGreaterEqual(len(y318_blocks), 8)
        # Sneak posture entered and cleanly released in try...finally
        postures = [req for op, req in world.calls if op == "posture"]
        self.assertTrue(any(p.get("sneak") is True for p in postures))
        self.assertIs(postures[-1].get("sneak"), False)
        self.assertFalse(state.st.get("sneaking"))
        self.assertFalse(world.st.get("sneaking"))
        # Landmark recorded in memo_for(state)
        memo = memo_for(state)
        self.assertTrue(memo.has_landmark("high_altitude_portal", dimension="overworld"))
        lms = memo.landmarks("high_altitude_portal", dimension="overworld")
        self.assertEqual(len(lms), 1)
        self.assertEqual(lms[0][1], 314)

    def test_sneak_cleanup_on_abort_during_portal_build(self) -> None:
        world = WaveBWorld(
            pos=(0.5, 314.0, 0.5),
            dimension="overworld",
            blocks=_flat(-2, 4, -2, 4, y=313),
            items={"cobblestone": 16, "obsidian": 10, "flint_and_steel": 1},
        )
        ex, state = world.session()
        skill = TradeWorldHeight(prep_only=True)
        for _ in range(10):
            done = skill.step(ex, state)
            self.assertFalse(done)
            if world.st.get("sneaking") is True:
                break
        self.assertTrue(world.st.get("sneaking"), "expected skill to enter sneaking before abort()")

        skill.abort()
        self.assertFalse(world.st.get("sneaking"))
        self.assertFalse(state.st.get("sneaking"))
        postures = [req for op, req in world.calls if op == "posture"]
        self.assertIs(postures[-1].get("sneak"), False)

    def test_nether_villager_pushback_through_portal(self) -> None:
        blocks = _flat(-4, 6, -4, 4, y=63, block="netherrack")
        blocks[(3, 64, 0)] = "nether_portal"
        world = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_nether",
            blocks=blocks,
            items={"emerald": 3},
            entities=[{"id": 22, "name": "villager", "pos": [1.5, 64.0, 0.5]}],
        )

        # Simulate stepping into the portal transferring player & villager to Overworld Y=319
        def _on_move_to(w: WaveBWorld, pos=None, **_kw):
            if pos and math.dist(pos, [3.5, 64.0, 0.5]) < 0.2:
                w.st["dimension"] = "overworld"
                w.st["pos"] = [0.5, 319.0, 0.5]
                w.entities = [{"id": 22, "name": "villager", "pos": [1.5, 319.0, 0.5]}]
                return {"pos": [0.5, 319.0, 0.5]}
            return None

        world.on_move_to = _on_move_to
        _attach_merchant(
            world,
            22,
            [{"index": 0, "give": {"name": "emerald", "count": 1}, "get": {"name": "bread", "count": 6}}],
            window_type="villager",
        )
        ex, state = world.session()
        skill = TradeWorldHeight()
        drive(skill, ex, state)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("bread"), 6)
        self.assertTrue(any(op == "control" and req.get("forward") is True for op, req in world.calls))

    def test_env_blocked_at_high_altitude_with_no_merchant(self) -> None:
        world = WaveBWorld(
            pos=(0.5, 319.0, 0.5),
            dimension="overworld",
            blocks=_flat(-2, 2, -2, 2, y=318),
            items={"emerald": 4},
            entities=[],
        )
        ex, state = world.session()
        with self.assertRaises(EnvBlocked) as ctx:
            drive(TradeWorldHeight(), ex, state)
        self.assertEqual(ctx.exception.missing_predicate, "high_altitude_merchant")

    def test_blocked_low_altitude_missing_placeables_and_approach_solid_guard(self) -> None:
        from mcagent.skills.t4_arbalistic_and_height import _approach_block

        st = _state_with(
            pos=(0.5, 64.0, 0.5),
            dimension="overworld",
            items=[{"name": "emerald", "count": 1}, {"name": "obsidian", "count": 10}],
        )
        self.assertTrue(TradeWorldHeight.blocked(st))

        # High-altitude void around target block (8, 314, 0): _approach_block must raise Replan instead of walking off edge
        world = WaveBWorld(pos=(0.5, 314.0, 0.5), dimension="overworld", blocks=_flat(-1, 1, -1, 1, y=313))
        ex, state = world.session()
        with self.assertRaises(Replan):
            list(_approach_block(ex, state, (8, 314, 0), reach=3.0))
        self.assertFalse(any(op == "move_to" for op, _ in world.calls))


# ============================================================================
# 3. arbalistic_trench_shot (Axial 1x1x5 Trench + Piercing IV One-Shot)
# ============================================================================


class ArbalisticTrenchShotTests(_WaveBTestBase):
    def test_explicit_rejection_of_multishot_crossbow(self) -> None:
        fish_pack = {
            "cod_bucket": 1,
            "salmon_bucket": 1,
            "tropical_fish_bucket": 1,
            "pufferfish_bucket": 1,
            "tadpole_bucket": 1,
            "crossbow": 1,
            "arrow": 2,
        }
        for label, enchants, kwargs in (
            ("multishot_only", {"multishot": 1}, {}),
            ("piercing_and_multishot_shot", {"piercing": 4, "multishot": 1}, {}),
            ("piercing_and_multishot_prep", {"piercing": 4, "multishot": 1}, {"prep_only": True, "prep_mode": "crossbow"}),
        ):
            with self.subTest(case=label):
                world = WaveBWorld(
                    pos=(0.5, 64.0, 0.5),
                    blocks=_flat(-2, 8, -2, 2, y=63),
                    items=dict(fish_pack),
                    item_meta={"crossbow": {"enchantments": enchants}},
                )
                ex, state = world.session()
                with self.assertRaises(Replan) as ctx:
                    drive(ArbalisticTrenchShot(**kwargs), ex, state)
                self.assertIn("多重射击", str(ctx.exception))

    def test_prep_crossbow_via_librarian_trade_and_anvil(self) -> None:
        blocks = _flat(-4, 6, -4, 4, y=63)
        blocks[(2, 64, 0)] = "anvil"
        world = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"crossbow": 1, "emerald": 16},
            entities=[{"id": 31, "name": "villager", "pos": [1.5, 64.0, 1.5]}],
        )
        _attach_merchant(
            world,
            31,
            [{"index": 0, "give": {"name": "emerald", "count": 10}, "get": {"name": "enchanted_book", "count": 1}}],
            window_type="villager",
            reward_meta={"enchanted_book": {"enchantments": {"piercing": 4}}},
        )
        prev_act = world.on_window_act

        def _anvil_act(w: WaveBWorld, target: dict, ops: list[dict]) -> dict:
            if target.get("pos") == [2, 64, 0]:
                w.items["enchanted_book"] = max(0, w.items.get("enchanted_book", 0) - 1)
                w.item_meta["crossbow"] = {"enchantments": {"piercing": 4}}
                return {"ok": True, "ops": deepcopy(ops)}
            assert prev_act is not None
            return prev_act(w, target=target, ops=ops)

        world.on_window_act = _anvil_act
        ex, state = world.session()
        skill = ArbalisticTrenchShot(prep_only=True, prep_mode="crossbow")
        drive(skill, ex, state)

        self.assertEqual(skill.verify_done(state), "")
        wacts = [req for op, req in world.calls if op == "window_act"]
        self.assertTrue(any(r["target"] == {"entity_id": 31} for r in wacts))
        self.assertTrue(any(r["target"] == {"pos": [2, 64, 0]} for r in wacts))

    def test_prep_mobs_water_bucket_collection_and_env_blocked_under_five(self) -> None:
        # Happy path: 3 buckets + 2 water_buckets scoop pufferfish & tadpole -> 5 ready
        world = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-4, 6, -4, 4, y=63),
            items={
                "cod_bucket": 1,
                "salmon_bucket": 1,
                "tropical_fish_bucket": 1,
                "water_bucket": 2,
            },
            entities=[
                {"id": 41, "name": "pufferfish", "pos": [2.0, 64.0, 0.5]},
                {"id": 42, "name": "tadpole", "pos": [2.5, 64.0, 0.5]},
            ],
        )

        def _scoop(w: WaveBWorld, pos=None, entity_id=None, item=None):
            if item == "water_bucket" and entity_id is not None:
                mob = next(e for e in w.entities if e["id"] == entity_id)
                w.entities = [e for e in w.entities if e["id"] != entity_id]
                w.items["water_bucket"] -= 1
                bname = f"{mob['name']}_bucket"
                w.items[bname] = w.items.get(bname, 0) + 1
                return {"caught": mob["name"]}
            return None

        world.on_interact = _scoop
        ex, state = world.session()
        skill = ArbalisticTrenchShot(prep_only=True, prep_mode="mobs")
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("pufferfish_bucket"), 1)
        self.assertEqual(state.count("tadpole_bucket"), 1)

        # EnvBlocked when < 5 unique mobs even after scooping all available
        world_short = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-4, 6, -4, 4, y=63),
            items={"cod_bucket": 1, "salmon_bucket": 1, "water_bucket": 1},
            entities=[{"id": 43, "name": "tadpole", "pos": [2.0, 64.0, 0.5]}],
        )
        world_short.on_interact = _scoop
        ex2, state2 = world_short.session()
        with self.assertRaises(EnvBlocked) as ctx:
            drive(ArbalisticTrenchShot(prep_only=True, prep_mode="mobs"), ex2, state2)
        self.assertEqual(ctx.exception.missing_predicate, "mob_collection_5")

    def test_full_axial_1x1x5_trench_tadpole_single_tap_and_piercing_iv_shot(self) -> None:
        blocks = {**_flat(-2, 10, -2, 2, y=62, block="stone"), **_flat(-2, 10, -2, 2, y=63, block="dirt")}
        world = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={
                "crossbow": 1,
                "arrow": 4,
                "cod_bucket": 1,
                "salmon_bucket": 1,
                "tropical_fish_bucket": 1,
                "pufferfish_bucket": 1,
                "tadpole_bucket": 1,
                "water_bucket": 1,
                "cobblestone": 4,
            },
            item_meta={"crossbow": {"enchantments": {"piercing": 4}}},
            held={"name": "crossbow", "count": 1},
        )
        next_eid = 100

        def _release(w: WaveBWorld, pos=None, entity_id=None, item=None):
            nonlocal next_eid
            if item and item.endswith("_bucket") and pos is not None:
                if item == "water_bucket":
                    w.blocks[tuple(pos)] = "water"
                    return {"placed": "water"}
                mob_name = item.removesuffix("_bucket")
                w.items[item] -= 1
                w.items["bucket"] = w.items.get("bucket", 0) + 1
                w.blocks[tuple(pos)] = "water"
                w.entities.append({"id": next_eid, "name": mob_name, "pos": [pos[0] + 0.5, float(pos[1]), pos[2] + 0.5]})
                next_eid += 1
                return {"released": mob_name}
            return None

        world.on_interact = _release
        ex, state = world.session()
        skill = ArbalisticTrenchShot()
        drive(skill, ex, state, max_steps=120)

        self.assertEqual(skill.verify_done(state), "")
        # Exact 1x1x5 axial trench along +X at (2..6, 63, 0) plus 1-deep firing pit (0, 63, 0)
        dug = [tuple(req["pos"]) for op, req in world.calls if op == "dig"]
        self.assertEqual(dug, [(2, 63, 0), (3, 63, 0), (4, 63, 0), (5, 63, 0), (6, 63, 0), (0, 63, 0)])

        # Released 4 fish buckets + tadpole_bucket into the trench
        released_items = [req["item"] for op, req in world.calls if op == "interact" and req["item"] != "water_bucket"]
        self.assertEqual(
            released_items,
            ["cod_bucket", "salmon_bucket", "tropical_fish_bucket", "pufferfish_bucket", "tadpole_bucket"],
        )

        # Tadpole tapped EXACTLY once with max_hits=1 and non-weapon equipped first
        attacks = [req for op, req in world.calls if op == "attack"]
        self.assertEqual(len(attacks), 1)
        self.assertEqual(attacks[0]["max_hits"], 1)
        tad_ent = next(e for e in world.entities if e["name"] == "tadpole")
        self.assertEqual(attacks[0]["entity_id"], tad_ent["id"])
        attack_idx = next(i for i, (op, _) in enumerate(world.calls) if op == "attack")
        equips_before_tap = [req["item"] for op, req in world.calls[:attack_idx] if op == "equip"]
        self.assertTrue(equips_before_tap and equips_before_tap[-1] != "crossbow")

        # Axial level firing endpoint [tx - 2 + 0.5, ty - 1, tz + 0.5] = [0.5, 63.0, 0.5], look + shoot(crossbow)
        self.assertEqual(world.st["pos"], [0.5, 63.0, 0.5])
        looks = [req for op, req in world.calls if op == "look"]
        shoots = [req for op, req in world.calls if op == "shoot"]
        self.assertEqual(len(shoots), 1)
        self.assertEqual(shoots[0]["weapon"], "crossbow")
        self.assertEqual(shoots[0]["pos"], [6.5, 63.5, 0.5])
        self.assertTrue(any(l["pos"] == [6.5, 63.5, 0.5] for l in looks))

    def test_axolotl_bucket_and_look_unsupported_api_guard(self) -> None:
        blocks = {**_flat(-2, 10, -2, 2, y=62, block="stone"), **_flat(-2, 10, -2, 2, y=63, block="dirt")}
        world = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={
                "crossbow": 1,
                "arrow": 2,
                "cod_bucket": 1,
                "salmon_bucket": 1,
                "tropical_fish_bucket": 1,
                "pufferfish_bucket": 1,
                "axolotl_bucket": 1,
                "cobblestone": 4,
            },
            item_meta={"crossbow": {"enchantments": {"piercing": 4}}},
            held={"name": "crossbow", "count": 1},
        )
        next_eid = 200

        def _release(w: WaveBWorld, pos=None, entity_id=None, item=None):
            nonlocal next_eid
            if item and item.endswith("_bucket") and pos is not None:
                mob_name = item.removesuffix("_bucket")
                w.items[item] -= 1
                w.items["bucket"] = w.items.get("bucket", 0) + 1
                w.blocks[tuple(pos)] = "water"
                w.entities.append({"id": next_eid, "name": mob_name, "pos": [pos[0] + 0.5, float(pos[1]), pos[2] + 0.5]})
                next_eid += 1
                return {"released": mob_name}
            return None

        world.on_interact = _release
        world.on_look = lambda w, **kw: (_ for _ in ()).throw(ApiError("unsupported_api", "look disabled"))
        ex, state = world.session()
        skill = ArbalisticTrenchShot()
        drive(skill, ex, state, max_steps=120)
        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(len([req for op, req in world.calls if op == "shoot"]), 1)


# ============================================================================
# 4. craft_netherite_armor_set (ReservationBook 21 Diamonds + Hopper + Smith)
# ============================================================================


class CraftNetheriteArmorSetTests(_WaveBTestBase):
    def test_material_plan_and_reservation_book_reserves_21_diamonds(self) -> None:
        state = _state_with(
            items=[
                {"name": _TEMPLATE, "count": 1},
                {"name": "diamond", "count": 24},
            ]
        )
        plan = CraftNetheriteArmorSet.material_plan({}, state)
        self.assertEqual(plan, (("diamond", 21, "consume"),))

        book = ReservationBook()
        self.assertTrue(book.reserve_plan(state, "netherite_armor", plan))
        # Only 3 diamonds remain unreserved -> another consumer cannot steal 4 diamonds
        self.assertEqual(book.available(state, "diamond", consumer="other_goal"), 3)
        self.assertFalse(book.reserve(state, "other_goal", "diamond", 4))
        self.assertTrue(book.reserve(state, "other_goal", "diamond", 3))

        # Partial upgrade (2 slots already netherite, 1 template) -> only 1 dup = 7 diamonds
        partial = _state_with(
            items=[
                {"name": _TEMPLATE, "count": 1},
                {"name": "netherite_helmet", "count": 1},
                {"name": "netherite_boots", "count": 1},
                {"name": "diamond", "count": 10},
            ]
        )
        self.assertEqual(CraftNetheriteArmorSet.material_plan({}, partial), (("diamond", 7, "consume"),))
        # Already 4 templates -> no duplication diamonds needed
        enough = _state_with(items=[{"name": _TEMPLATE, "count": 4}, {"name": "diamond", "count": 21}])
        self.assertEqual(CraftNetheriteArmorSet.material_plan({}, enough), ())

    def test_zero_aggro_bastion_hopper_extraction_and_template_duplication(self) -> None:
        chest_pos = (3, 65, 0)
        hopper_pos = (3, 64, 0)
        blocks = _flat(-2, 6, -2, 2, y=63, block="blackstone")
        blocks[chest_pos] = "chest"
        blocks[(-2, 64, 0)] = "crafting_table"
        world = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_nether",
            blocks=blocks,
            items={
                "hopper": 1,
                "diamond": 21,
                "netherrack": 4,
                "netherite_ingot": 4,
                "diamond_helmet": 1,
                "diamond_chestplate": 1,
                "diamond_leggings": 1,
                "diamond_boots": 1,
            },
        )
        hopper_contents = [{_TEMPLATE: 1}]

        def _view(w: WaveBWorld, target: dict) -> dict:
            self.assertNotEqual(target.get("pos"), list(chest_pos), "forbidden direct window_view on bastion chest")
            return {"window": "hopper", "sections": {"container": [{"name": _TEMPLATE, "count": 1}] if hopper_contents else []}}

        def _act(w: WaveBWorld, target: dict, ops: list[dict]) -> dict:
            self.assertNotEqual(target.get("pos"), list(chest_pos), "forbidden direct window_act on bastion chest")
            self.assertEqual(ops[0].get("from"), "container")
            if target.get("pos") == list(hopper_pos) and hopper_contents:
                hopper_contents.clear()
                w.items[_TEMPLATE] = w.items.get(_TEMPLATE, 0) + 1
            return {"ok": True, "ops": deepcopy(ops)}

        world.on_window_view = _view
        world.on_window_act = _act
        ex, state = world.session()
        skill = CraftNetheriteArmorSet(prep_only=True)
        drive(skill, ex, state)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(world.blocks.get(hopper_pos), "hopper")
        self.assertEqual(state.count(_TEMPLATE), 4)
        self.assertEqual(state.count("diamond"), 0)

        win_targets = [req["target"] for op, req in world.calls if op in ("window_view", "window_act")]
        self.assertTrue(win_targets)
        self.assertTrue(all(t == {"pos": list(hopper_pos)} for t in win_targets))
        self.assertFalse(any(t == {"pos": list(chest_pos)} for t in win_targets))

    def test_bastion_hopper_drains_junk_and_polls_for_template(self) -> None:
        from unittest import mock

        chest_pos = (3, 65, 0)
        blocks = _flat(-2, 6, -2, 2, y=63, block="blackstone")
        blocks[chest_pos] = "chest"
        blocks[(-2, 64, 0)] = "crafting_table"
        world = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_nether",
            blocks=blocks,
            items={
                "hopper": 1,
                "diamond": 21,
                "netherrack": 4,
                "netherite_ingot": 4,
                "diamond_helmet": 1,
                "diamond_chestplate": 1,
                "diamond_leggings": 1,
                "diamond_boots": 1,
            },
        )
        polls = [0]

        def _view(w: WaveBWorld, target: dict) -> dict:
            polls[0] += 1
            if polls[0] == 1:
                return {"window": "hopper", "sections": {"container": [{"name": "gold_nugget", "count": 2}]}}
            if polls[0] == 2:
                return {"window": "hopper", "sections": {"container": []}}
            return {"window": "hopper", "sections": {"container": [{"name": _TEMPLATE, "count": 1}]}}

        def _act(w: WaveBWorld, target: dict, ops: list[dict]) -> dict:
            self.assertEqual(ops[0].get("from"), "container")
            item = ops[0].get("item")
            w.items[item] = w.items.get(item, 0) + int(ops[0].get("count", 1))
            return {"ok": True}

        world.on_window_view = _view
        world.on_window_act = _act
        ex, state = world.session()
        with mock.patch("mcagent.skills.t4_nether_endgame.time.sleep"):
            drive(CraftNetheriteArmorSet(prep_only=True), ex, state)
        self.assertEqual(state.count(_TEMPLATE), 4)
        self.assertEqual(state.count("gold_nugget"), 2)
        self.assertGreaterEqual(polls[0], 3)

    def test_armorer_trade_and_full_smith_four_netherite_pieces_without_equipping(self) -> None:
        table_pos = (2, 64, 0)
        blocks = _flat(-4, 6, -4, 4, y=63)
        blocks[table_pos] = "smithing_table"
        world = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={
                _TEMPLATE: 4,
                "netherite_ingot": 4,
                "diamond_helmet": 1,
                "diamond_chestplate": 1,
                "diamond_leggings": 1,
                "emerald": 16,
            },
            entities=[{"id": 55, "name": "villager", "pos": [1.5, 64.0, 1.5]}],
        )
        _attach_merchant(
            world,
            55,
            [{"index": 0, "give": {"name": "emerald", "count": 8}, "get": {"name": "diamond_boots", "count": 1}}],
            window_type="villager",
        )
        _attach_smithing_table(world, table_pos)

        ex, state = world.session()
        skill = CraftNetheriteArmorSet(prep_only=False)
        drive(skill, ex, state, max_steps=160)

        self.assertEqual(skill.verify_done(state), "")
        for slot in ARMOR_SLOTS:
            self.assertEqual(state.count(f"netherite_{slot}"), 1)
        # Verify NO armor was equipped/worn (verify_done checks inventory only)
        armor_equips = [req for op, req in world.calls if op == "equip" and req.get("slot") != "hand"]
        self.assertEqual(armor_equips, [])
        self.assertFalse(state.inventory.get("armor"))


# ============================================================================
# 5. summon_and_trap_wither (Shared End Redline + Private Nether Y=127 T4-P3)
# ============================================================================


def _nether_y127_blocks() -> dict[tuple[int, int, int], str]:
    blocks = _flat(-5, 5, -5, 5, y=123, block="netherrack")
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            blocks[(dx, 127, dz)] = "bedrock"
    return blocks


class SummonAndTrapWitherTests(_WaveBTestBase):
    def test_hard_shared_end_redline_blocks_and_replan_with_zero_blocks_placed(self) -> None:
        end_items = [
            {"name": "wither_skeleton_skull", "count": 3},
            {"name": "soul_sand", "count": 4},
            {"name": "obsidian", "count": 1},
            {"name": "diamond_sword", "count": 1},
        ]
        for dim in ("the_end", "end"):
            with self.subTest(dimension=dim):
                end_state = _state_with(dimension=dim, items=end_items)
                msg = SummonAndTrapWither.blocked(end_state)
                self.assertTrue(msg)
                self.assertIn("末地", msg)

                for mode in ("summon_only", "nether_roof_trap"):
                    world = WaveBWorld(
                        pos=(0.5, 64.0, 0.5),
                        dimension=dim,
                        blocks=_flat(-3, 3, -3, 3, y=63, block="end_stone"),
                        items={r["name"]: r["count"] for r in end_items},
                    )
                    ex, state = world.session()
                    with self.assertRaises(Replan) as ctx:
                        drive(SummonAndTrapWither(mode=mode), ex, state)
                    self.assertIn("末地", str(ctx.exception))
                    self.assertEqual([c for c in world.calls if c[0] in ("place", "dig")], [])

    def test_prep_looting_trade_and_anvil(self) -> None:
        blocks = _flat(-4, 6, -4, 4, y=63)
        blocks[(2, 64, 0)] = "anvil"
        world = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="overworld",
            blocks=blocks,
            items={"diamond_sword": 1, "emerald": 12},
            entities=[{"id": 61, "name": "villager", "pos": [1.5, 64.0, 1.5]}],
        )
        _attach_merchant(
            world,
            61,
            [{"index": 0, "give": {"name": "emerald", "count": 8}, "get": {"name": "enchanted_book", "count": 1}}],
            window_type="villager",
            reward_meta={"enchanted_book": {"enchantments": {"looting": 3}}},
        )
        prev_view, prev_act = world.on_window_view, world.on_window_act

        def _view(w: WaveBWorld, target: dict) -> dict:
            if target.get("pos") == [2, 64, 0]:
                return {"window": "anvil", "sections": {"left": [], "right": [], "output": []}}
            assert prev_view is not None
            return prev_view(w, target=target)

        anvil_ops: list[list[dict]] = []

        def _act(w: WaveBWorld, target: dict, ops: list[dict]) -> dict:
            if target.get("pos") == [2, 64, 0]:
                anvil_ops.append(deepcopy(ops))
                if any(o.get("op") == "take" and o.get("from") == "output" for o in ops):
                    w.items["enchanted_book"] = max(0, w.items.get("enchanted_book", 0) - 1)
                    w.item_meta["diamond_sword"] = {"enchantments": {"looting": 3}}
                return {"ok": True, "ops": deepcopy(ops)}
            assert prev_act is not None
            return prev_act(w, target=target, ops=ops)

        world.on_window_view = _view
        world.on_window_act = _act
        ex, state = world.session()
        skill = SummonAndTrapWither(mode="prep_looting")
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(len(anvil_ops), 1)
        self.assertEqual(
            anvil_ops[0],
            [
                {"op": "put", "item": "diamond_sword", "count": 1, "to": "left"},
                {"op": "put", "item": "enchanted_book", "count": 1, "to": "right", "enchanted": True},
                {"op": "take", "from": "output"},
            ],
        )

    def test_prep_skulls_two_block_choke_and_shield_reset_on_abort(self) -> None:
        # Happy path: places 2-block choke lintel at fy+2, raises shield, farms 3 skulls, lowers shield
        world = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_nether",
            blocks=_flat(-4, 6, -4, 4, y=63, block="nether_bricks"),
            items={"diamond_sword": 1, "shield": 1, "cobblestone": 4, "soul_sand": 4},
            entities=[{"id": 81, "name": "wither_skeleton", "pos": [2.5, 64.0, 0.5]}],
        )

        def _attack(w: WaveBWorld, entity_id: int, max_hits=None):
            w.items["wither_skeleton_skull"] = w.items.get("wither_skeleton_skull", 0) + 3
            return {"entity_id": entity_id, "hits": 3, "killed": True}

        world.on_attack = _attack
        ex, state = world.session()
        skill = SummonAndTrapWither(mode="prep_skulls")
        drive(skill, ex, state)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(world.blocks.get((1, 66, 0)), "cobblestone")  # 2-block axial choke lintel at fy+2
        postures = [req for op, req in world.calls if op == "posture"]
        self.assertTrue(any(p.get("shield") is True for p in postures))
        self.assertIs(postures[-1].get("shield"), False)
        self.assertFalse(state.st.get("blocking"))
        self.assertFalse(world.st.get("blocking"))

        # Abort path: shield MUST reset in try...finally when aborted mid-hunt
        world_abort = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_nether",
            blocks=_flat(-4, 6, -4, 4, y=63, block="nether_bricks"),
            items={"diamond_sword": 1, "shield": 1, "cobblestone": 4, "soul_sand": 4},
            entities=[{"id": 82, "name": "wither_skeleton", "pos": [2.5, 64.0, 0.5]}],
        )
        ex2, state2 = world_abort.session()
        skill2 = SummonAndTrapWither(mode="prep_skulls")
        for _ in range(10):
            done = skill2.step(ex2, state2)
            self.assertFalse(done)
            if world_abort.st.get("blocking") is True:
                break
        self.assertTrue(world_abort.st.get("blocking"), "expected shield up before abort()")

        skill2.abort()
        self.assertFalse(world_abort.st.get("blocking"))
        self.assertFalse(state2.st.get("blocking"))
        self.assertIs([req for op, req in world_abort.calls if op == "posture"][-1].get("shield"), False)

    def test_summon_only_t4_p3_template_and_3rd_skull_error_confirmed_via_entities_and_advancements(self) -> None:
        for case, err_code, via_entities, via_adv in (
            ("place_failed_via_entities", "place_failed", True, False),
            ("protected_region_via_advancements", "protected_region", False, True),
        ):
            with self.subTest(case=case):
                world = WaveBWorld(
                    pos=(0.5, 124.0, 2.5),
                    dimension="the_nether",
                    blocks=_nether_y127_blocks(),
                    items={
                        "obsidian": 1,
                        "cobblestone": 8,
                        "soul_sand": 4,
                        "wither_skeleton_skull": 3,
                    },
                )
                skull_attempts: list[tuple[int, int, int]] = []

                def _on_place(w: WaveBWorld, item: str, pos: list[int] | None):
                    if item == "wither_skeleton_skull" and pos is not None:
                        skull_attempts.append(tuple(pos))
                        if len(skull_attempts) == 3:
                            w.items[item] -= 1
                            if via_entities:
                                w.entities.append({"id": 901, "name": "wither", "pos": [0.5, 125.0, -1.5]})
                            if via_adv:
                                w.adv_snapshot = {
                                    "advancements": [{"id": "minecraft:nether/summon_wither", "status": "completed"}]
                                }
                            raise ApiError(err_code, f"3rd skull consumed by wither ({err_code})")
                    return None

                world.on_place = _on_place
                ex, state = world.session()
                skill = SummonAndTrapWither(mode="summon_only")
                drive(skill, ex, state, max_steps=120)

                self.assertEqual(skill.verify_done(state), "")
                # Exact T4-P3 single obsidian at [cx, 124, cz] = [0, 124, 0]
                self.assertEqual(world.blocks.get((0, 124, 0)), "obsidian")
                # Exact horizontal T of 4 soul_sand at Y=125
                for sp in ((0, 125, 0), (0, 125, -1), (-1, 125, -1), (1, 125, -1)):
                    self.assertEqual(world.blocks.get(sp), "soul_sand")
                # Exact 3 wither_skeleton_skull positions at Y=125
                self.assertEqual(skull_attempts, [(-1, 125, -2), (0, 125, -2), (1, 125, -2)])
                # Retreated to [cx+0.5, 124.0, cz+3.5] = [0.5, 124.0, 3.5]
                self.assertEqual(world.st["pos"], [0.5, 124.0, 3.5])

        # Unconfirmed 3rd-skull place_failed (no wither entity and no advancement) -> Replan
        world_unconf = WaveBWorld(
            pos=(0.5, 124.0, 2.5),
            dimension="the_nether",
            blocks=_nether_y127_blocks(),
            items={"obsidian": 1, "cobblestone": 8, "soul_sand": 4, "wither_skeleton_skull": 3},
        )
        attempts = 0

        def _on_place_fail(w: WaveBWorld, item: str, pos: list[int] | None):
            nonlocal attempts
            if item == "wither_skeleton_skull":
                attempts += 1
                if attempts == 3:
                    raise ApiError("place_failed", "skull failed without spawning wither")
            return None

        world_unconf.on_place = _on_place_fail
        ex_u, st_u = world_unconf.session()
        with self.assertRaises(Replan):
            drive(SummonAndTrapWither(mode="summon_only"), ex_u, st_u, max_steps=120)

    def test_nether_roof_trap_full_kill_and_nether_star_pickup(self) -> None:
        from unittest import mock

        world = WaveBWorld(
            pos=(0.5, 124.0, 2.5),
            dimension="the_nether",
            blocks=_nether_y127_blocks(),
            items={
                "obsidian": 1,
                "cobblestone": 8,
                "soul_sand": 4,
                "wither_skeleton_skull": 3,
                "diamond_sword": 1,
                "shield": 1,
            },
        )
        skull_count = 0
        wither_killed = False

        def _on_place(w: WaveBWorld, item: str, pos: list[int] | None):
            nonlocal skull_count
            if item == "wither_skeleton_skull":
                skull_count += 1
                if skull_count == 3:
                    w.entities.append({"id": 902, "name": "wither", "pos": [0.5, 125.0, -1.5]})
            return None

        def _on_attack(w: WaveBWorld, entity_id: int, max_hits=None):
            nonlocal wither_killed
            w.entities = [e for e in w.entities if e["id"] != entity_id]
            wither_killed = True
            return {"entity_id": entity_id, "hits": 6, "killed": True}

        def _on_move_to(w: WaveBWorld, pos=None, **_kw):
            if wither_killed and pos and math.dist(pos, [0.5, 124.0, 0.5]) < 0.5:
                w.st["pos"] = list(pos)
                w.items["nether_star"] = w.items.get("nether_star", 0) + 1
                return {"pos": list(pos)}
            return None

        world.on_place = _on_place
        world.on_attack = _on_attack
        world.on_move_to = _on_move_to
        ex, state = world.session()
        skill = SummonAndTrapWither(mode="nether_roof_trap")
        with mock.patch("mcagent.skills.t4_nether_endgame.time.sleep") as m_sleep:
            drive(skill, ex, state, max_steps=160)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("nether_star"), 1)
        self.assertGreaterEqual(m_sleep.call_count, 1)
        self.assertTrue(any(op == "attack" and req["entity_id"] == 902 for op, req in world.calls))
        self.assertFalse(world.st.get("blocking"))
        self.assertFalse(state.st.get("blocking"))

    def test_nether_roof_trap_resumes_existing_wither_without_requiring_3_skulls(self) -> None:
        from unittest import mock

        world = WaveBWorld(
            pos=(0.5, 124.0, 2.5),
            dimension="the_nether",
            blocks=_nether_y127_blocks(),
            items={"diamond_sword": 1, "shield": 1},
            entities=[{"id": 903, "name": "wither", "pos": [0.5, 125.0, -1.5]}],
        )

        def _on_attack(w: WaveBWorld, entity_id: int, max_hits=None):
            w.entities = [e for e in w.entities if e["id"] != entity_id]
            w.items["nether_star"] = 1
            return {"entity_id": entity_id, "hits": 6, "killed": True}

        world.on_attack = _on_attack
        ex, state = world.session()
        skill = SummonAndTrapWither(mode="nether_roof_trap")
        with mock.patch("mcagent.skills.t4_nether_endgame.time.sleep"):
            drive(skill, ex, state, max_steps=120)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("nether_star"), 1)
        self.assertTrue(any(op == "attack" and req["entity_id"] == 903 for op, req in world.calls))
        self.assertFalse(any(op == "place" and req["item"] == "wither_skeleton_skull" for op, req in world.calls))


# ============================================================================
# 6. activate_beacon (Zero-GUI Beacon + Explicit Copper Block Exclusion)
# ============================================================================


class ActivateBeaconTests(_WaveBTestBase):
    def test_explicit_copper_block_exclusion(self) -> None:
        for copper in ("copper_block", "waxed_copper_block", "exposed_copper"):
            with self.subTest(copper=copper):
                st = _state_with(items=[{"name": "beacon", "count": 1}, {"name": copper, "count": 9}])
                msg = ActivateBeacon.blocked(st)
                self.assertTrue(msg)
                self.assertIn("铜块", msg)

        # 8 valid iron_block + 9 copper_block MUST still be blocked (copper never counts)
        mixed = _state_with(
            items=[
                {"name": "beacon", "count": 1},
                {"name": "iron_block", "count": 8},
                {"name": "copper_block", "count": 9},
            ]
        )
        self.assertIn("铜块", ActivateBeacon.blocked(mixed))

    def test_happy_path_craft_beacon_3x3_pyramid_zero_gui_and_within_10_blocks(self) -> None:
        blocks = _flat(-6, 6, -6, 6, y=63)
        blocks[(-2, 64, 0)] = "crafting_table"
        world = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="overworld",
            blocks=blocks,
            items={
                "nether_star": 1,
                "obsidian": 3,
                "glass": 5,
                "iron_block": 5,
                "gold_block": 2,
                "emerald_block": 1,
                "diamond_block": 1,
            },
        )
        ex, state = world.session()
        skill = ActivateBeacon()
        drive(skill, ex, state, max_steps=100)

        self.assertEqual(skill.verify_done(state), "")
        # Crafted beacon from nether_star + 3 obsidian + 5 glass
        crafts = [req for op, req in world.calls if op == "craft"]
        self.assertTrue(any(c["item"] == "beacon" for c in crafts))
        # Placed 3x3 valid mineral pyramid + beacon on top at [bx, by+1, bz]
        beacon_places = [req["pos"] for op, req in world.calls if op == "place" and req["item"] == "beacon"]
        self.assertEqual(len(beacon_places), 1)
        bx, btop_y, bz = beacon_places[0]
        by = btop_y - 1
        valid_set = {"iron_block", "gold_block", "emerald_block", "diamond_block", "netherite_block"}
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                self.assertIn(world.blocks.get((bx + dx, by, bz + dz)), valid_set)
        self.assertEqual(world.blocks.get((bx, btop_y, bz)), "beacon")
        # Standing within <= 10.0 blocks of beacon
        px, py, pz = world.st["pos"]
        eye_dist = math.dist((px, py + 1.62, pz), (bx + 0.5, btop_y + 0.5, bz + 0.5))
        self.assertLessEqual(eye_dist, 10.0)
        # ZERO window_view / window_act calls on beacon (zero-GUI contract)
        self.assertEqual([c for c in world.calls if c[0] in ("window_view", "window_act")], [])

    def test_unbreakable_bedrock_roof_rejects_activation(self) -> None:
        blocks = _flat(-6, 6, -6, 6, y=63)
        blocks.update(_flat(-6, 6, -6, 6, y=70, block="bedrock"))
        world = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="overworld",
            blocks=blocks,
            items={"beacon": 1, "iron_block": 9},
        )
        ex, state = world.session()
        with self.assertRaises(Replan) as ctx:
            drive(ActivateBeacon(), ex, state)
        self.assertIn("基岩", str(ctx.exception))
        self.assertFalse(any(op == "place" and req["item"] == "beacon" for op, req in world.calls))

    def test_sky_column_rejects_high_opaque_ceiling_within_64_blocks(self) -> None:
        blocks = _flat(-6, 6, -6, 6, y=63)
        blocks.update(_flat(-6, 6, -6, 6, y=104, block="stone"))
        world = WaveBWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="overworld",
            blocks=blocks,
            items={"beacon": 1, "iron_block": 9},
        )
        ex, state = world.session()
        with self.assertRaises(Replan):
            drive(ActivateBeacon(), ex, state)
        self.assertFalse(any(op == "place" and req["item"] == "beacon" for op, req in world.calls))


# ============================================================================
# 7. Wave B Ledger & Shared-Subgraph ROI Contract
# ============================================================================


class WaveBLedgerAndROIContractTests(_WaveBTestBase):
    def test_wave_b_ledger_counts_scores_draft_stop_loss_wip1_and_validation(self) -> None:
        self.assertEqual(validate_ledger(GOALS), [])
        self.assertEqual(len(GOAL_MAP), len(GOALS))

        # Totals monotonically >= Wave B baseline (97 goals / 83 scored / 2400 pts)
        scored_all = [g for g in GOALS if g.score > 0]
        self.assertGreaterEqual(len(GOALS), 97)
        self.assertGreaterEqual(len(scored_all), 83)
        self.assertGreaterEqual(sum(g.score for g in scored_all), 2400)

        # WIP<=1 discipline preserved
        self.assertLessEqual(len([g for g in GOALS if g.stage == "candidate"]), 1)

        # Wave B 7 T4_PREP + 5 T4 (+420 pts)
        wave_b_prep = [GOAL_MAP[gid] for gid in WAVE_B_PREP_IDS]
        wave_b_scored = [GOAL_MAP[gid] for gid in WAVE_B_SCORED_IDS]
        self.assertEqual(sum(g.score for g in wave_b_prep), 0)
        self.assertEqual(sum(g.score for g in wave_b_scored), 420)
        for g in wave_b_prep:
            self.assertEqual(g.tier, "T4_PREP")
        for g in wave_b_scored:
            self.assertEqual(g.tier, "T4")

        repo_root = Path(__file__).resolve().parents[2]
        seen_advs: set[str] = set()
        for g in scored_all:
            for adv in g.done_advancements:
                self.assertNotIn(adv, seen_advs, f"duplicate advancement in GOALS: {adv}")
                seen_advs.add(adv)

        for gid in WAVE_B_ALL_IDS:
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

    def test_rank_actionable_goals_amortizes_prep_wither_3_skulls_across_wither_and_beacon(self) -> None:
        st = _state_with(items=[])
        wither_subgraph = (
            GOAL_MAP["prep_wither_3_skulls"],
            GOAL_MAP["summon_wither"],
            GOAL_MAP["prep_nether_star"],
            GOAL_MAP["create_beacon"],
        )
        solo_subgraph = (
            GOAL_MAP["prep_wither_3_skulls"],
            GOAL_MAP["summon_wither"],
        )

        from mcagent.skills.world_memo import memo_for
        memo_for(st).record_landmark("nether_fortress", [40, 64, 40], dimension="the_nether")
        memo_for(st).record_landmark("village", [40, 64, 40], dimension="overworld")
        ranked_shared = rank_actionable_goals(st, wither_subgraph)
        ranked_solo = rank_actionable_goals(st, solo_subgraph)
        by_step_shared = {step.step_goal.id: step for step in ranked_shared}
        by_step_solo = {step.step_goal.id: step for step in ranked_solo}

        self.assertIn("prep_wither_3_skulls", by_step_shared)
        shared_step = by_step_shared["prep_wither_3_skulls"]
        solo_step = by_step_solo["prep_wither_3_skulls"]

        # prep_wither_3_skulls is shared by summon_wither (+100) and create_beacon (+60 via prep_nether_star)
        self.assertGreaterEqual(shared_step.shared_dependents, 2)
        self.assertGreater(shared_step.marginal_roi, shared_step.roi)
        self.assertEqual(solo_step.shared_dependents, 1)
        self.assertEqual(solo_step.marginal_roi, solo_step.roi)
        self.assertGreater(shared_step.marginal_roi, solo_step.marginal_roi)

        # Also verify full transitive resolution for Wave B wither + beacon chains
        active = resolve_active_goals(GOALS, target_goal=("summon_wither", "create_beacon"))
        completed = {
            "minecraft:story/mine_stone",
            "minecraft:story/upgrade_tools",
            "minecraft:story/smelt_iron",
            "minecraft:story/mine_diamond",
            "minecraft:story/form_obsidian",
            "minecraft:story/enter_the_nether",
            "minecraft:story/enchant_item",
            "minecraft:adventure/trade",
            "minecraft:nether/find_fortress",
            "minecraft:nether/get_wither_skull",
        }
        full_ranked = rank_actionable_goals(st, active, completed_advs=completed)
        by_step_full = {step.step_goal.id: step for step in full_ranked}
        self.assertIn("prep_looting_sword", by_step_full)
        self.assertGreaterEqual(by_step_full["prep_looting_sword"].shared_dependents, 2)
        self.assertGreater(by_step_full["prep_looting_sword"].marginal_roi, by_step_full["prep_looting_sword"].roi)


if __name__ == "__main__":
    unittest.main()
