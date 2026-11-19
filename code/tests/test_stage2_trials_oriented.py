"""Stage 2 Wave 2 试炼密室与朝向放置离线单元测试。"""
from __future__ import annotations

import math
import unittest
from copy import deepcopy

from mcagent.client import ApiError
from mcagent.runtime import EnvBlocked, Replan, State
from mcagent.skills.registry import REGISTRY, contract_errors
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.t2_trials_oriented import (
    ReadChiseledBookshelf,
    ScrapeLitCopperBulb,
    TransplantCreakingHeart,
    UnlockTrialVault,
    WindChargeBounce,
    clear_trial_wave,
    place_oriented,
    power_block_once,
)
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches

_SKILLS = (
    UnlockTrialVault,
    WindChargeBounce,
    ScrapeLitCopperBulb,
    ReadChiseledBookshelf,
    TransplantCreakingHeart,
)
_OX_PREV = {
    "oxidized_copper_bulb": "weathered_copper_bulb",
    "weathered_copper_bulb": "exposed_copper_bulb",
    "exposed_copper_bulb": "copper_bulb",
}


def _flat_floor(x0=-5, x1=5, z0=-5, z1=5, y=63, block="stone") -> dict[tuple[int, int, int], str]:
    return {(x, y, z): block for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)}


def _run_gen(gen, ex, state=None):
    """驱动带 return 值的 L2 生成器，强制一步最多一个阻塞动作。"""
    result = None
    for i in range(200):
        before = len(ex.acts)
        try:
            next(gen)
        except StopIteration as stop:
            fired = ex.acts[before:]
            if len(fired) > 1:
                raise AssertionError(f"第 {i + 1} 步发了 {len(fired)} 个阻塞动作 {fired}")
            result = stop.value
            return result
        fired = ex.acts[before:]
        if len(fired) > 1:
            raise AssertionError(f"第 {i + 1} 步发了 {len(fired)} 个阻塞动作 {fired}")
    raise AssertionError("生成器超过 200 步未结束")


class TrialsWorld(ScriptedWorld):
    """支持方块属性、朝向模拟、红石通电、铜灯刮氧化、试炼怪与风弹弹跳的离线夹具。"""

    def __init__(self, pos=(0.5, 64.0, 0.5), *, blocks=None, items=None, entities=None, block_props=None, hp=20.0):
        floor = _flat_floor()
        if blocks:
            floor.update({tuple(k): v for k, v in blocks.items()})
        super().__init__(pos=pos, blocks=floor, items=items)
        self.st.update(dimension="overworld", health=hp)
        self.entities: list[dict] = [deepcopy(e) for e in (entities or ())]
        self.block_props: dict[tuple[int, int, int], dict] = {tuple(k): dict(v) for k, v in (block_props or {}).items()}
        self.held: dict | None = None
        self.place_props_fn = None
        self.status_y_seq: list[float] = []
        self.kill_damage: float = 0.0

    def status(self, timeout=10):
        if self.status_y_seq:
            self.st["pos"][1] = float(self.status_y_seq.pop(0))
        return super().status(timeout=timeout)

    def inventory(self, timeout=10):
        inv = super().inventory(timeout=timeout)
        if self.held:
            inv["held"] = dict(self.held)
        return inv

    def get_block(self, pos):
        cell = tuple(int(v) for v in pos)
        out = {"name": self.blocks.get(cell, "air")}
        if cell in self.block_props:
            out["properties"] = deepcopy(self.block_props[cell])
        return out

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None):
        self.calls.append(("find_blocks", {"types": types, "radius": radius, "limit": limit}))
        allowed = {t.removeprefix("minecraft:") for t in (types or ())}
        px, py, pz = self.st["pos"]
        hits = []
        for (bx, by, bz), name in sorted(self.blocks.items()):
            if allowed and name not in allowed:
                continue
            d = math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5))
            if d <= radius:
                row = {"name": name, "pos": [bx, by, bz], "dist": round(d, 2)}
                if (bx, by, bz) in self.block_props:
                    row["properties"] = deepcopy(self.block_props[(bx, by, bz)])
                hits.append(row)
        hits.sort(key=lambda b: b["dist"])
        return {"blocks": hits[:limit]}

    def get_entities(self, radius=32, types=None):
        self.calls.append(("get_entities", {"radius": radius, "types": types}))
        allowed = {t.removeprefix("minecraft:") for t in (types or ())}
        px, py, pz = self.st["pos"]
        return {
            "entities": [
                deepcopy(e)
                for e in self.entities
                if (not allowed or e.get("name") in allowed or e.get("type") in allowed)
                and math.dist((px, py, pz), e["pos"]) <= radius
            ]
        }

    def equip(self, item, slot="hand", *, stop_on=None):
        res = GridWorld.equip(self, item, slot=slot, stop_on=stop_on)
        if slot == "hand":
            self.held = {"name": item, "count": self.items[item]}
        return res

    def _default_place_props(self, item: str, cell: tuple[int, int, int]) -> dict:
        px, py, pz = self.st["pos"]
        dx, dz = (cell[0] + 0.5) - px, (cell[2] + 0.5) - pz
        if item == "comparator":
            if abs(dx) >= abs(dz):
                return {"facing": "east" if dx > 0 else "west", "powered": "false"}
            return {"facing": "south" if dz > 0 else "north", "powered": "false"}
        if item in ("pale_oak_log", "creaking_heart"):
            return {"axis": "y"}
        if item == "chiseled_bookshelf":
            return {"books_stored": 0}
        return {}

    def place(self, item, pos=None, *, stop_on=None):
        res = GridWorld.place(self, item, pos=pos, stop_on=stop_on)
        cell = tuple(int(v) for v in pos)
        props = self.place_props_fn(self, item, cell) if self.place_props_fn else self._default_place_props(item, cell)
        if props:
            self.block_props[cell] = dict(props)
        return res

    def dig(self, pos, collect=True, *, stop_on=None):
        cell = tuple(int(v) for v in pos)
        self.block_props.pop(cell, None)
        return GridWorld.dig(self, pos, collect=collect, stop_on=stop_on)

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None):
        self.calls.append(("interact", {"pos": pos, "entity_id": entity_id, "item": item}))
        if item is not None:
            self._need(item)
        cell = tuple(int(v) for v in pos) if pos is not None else None
        bname = self.blocks.get(cell, "air") if cell is not None else ""
        if cell is not None and item is None and (bname == "lever" or bname.endswith("_button")):
            self.block_props.setdefault(cell, {})["powered"] = "true"
            for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                nb = (cell[0] + dx, cell[1] + dy, cell[2] + dz)
                if "copper_bulb" in self.blocks.get(nb, ""):
                    cur = self.block_props.setdefault(nb, {})
                    cur["lit"] = "false" if cur.get("lit") in (True, "true") else "true"
            return {"changed": True}
        if cell is not None and bname == "vault" and item in ("trial_key", "ominous_trial_key"):
            self.items[item] -= 1
            self.block_props.setdefault(cell, {})["vault_state"] = "unlocking"
            return {"changed": True}
        if cell is not None and item and item.endswith("_axe") and "copper_bulb" in bname:
            if bname.startswith("waxed_"):
                self.blocks[cell] = bname.removeprefix("waxed_")
                return {"changed": True}
            if bname in _OX_PREV:
                self.blocks[cell] = _OX_PREV[bname]
                return {"changed": True}
        if cell is not None and bname == "chiseled_bookshelf" and item in ("book", "enchanted_book", "writable_book", "written_book"):
            self.items[item] -= 1
            props = self.block_props.setdefault(cell, {"books_stored": 0})
            props["books_stored"] = int(props.get("books_stored", 0)) + 1
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (cell[0] + dx, cell[1], cell[2] + dz)
                if self.blocks.get(nb) == "comparator":
                    self.block_props.setdefault(nb, {})["powered"] = "true"
            return {"changed": True}
        return {"changed": False}

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None):
        self.calls.append(("use_item", {"item": item, "target": target}))
        self._need(item)
        self.items[item] -= 1
        return {"used": item, "target": target}

    def attack(self, entity_id, timeout_ms=20000, *, max_hits=None, interval_ms=None, stop_on=None):
        self.calls.append(("attack", {"entity_id": entity_id}))
        self.entities = [e for e in self.entities if e["id"] != entity_id]
        if self.kill_damage:
            self.st["health"] = max(0.0, float(self.st["health"]) - self.kill_damage)
        return {"killed": True, "entity_id": entity_id}


class TrialsContractTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_contract_registry_verified_and_empty_bag_blocked(self):
        for cls in _SKILLS:
            with self.subTest(skill=cls.name):
                self.assertEqual(contract_errors(cls), [])
                self.assertEqual(cls.verified, "offline")
                self.assertIs(REGISTRY[cls.name], cls)
                self.assertTrue(cls.blocked(State()))
                empty_ready = State()
                empty_ready.set_status({"ready": True, "alive": True, "health": 20, "pos": [0.5, 64.0, 0.5]})
                empty_ready.set_inventory({"items": []})
                self.assertTrue(cls.blocked(empty_ready))
                inst = cls()
                self.assertTrue(inst.verify_done(empty_ready))
        for l2_name in ("place_oriented", "power_block_once", "clear_trial_wave"):
            self.assertNotIn(l2_name, REGISTRY)

    def test_abort_cleans_up_active_posture(self):
        world = TrialsWorld(items={"wind_charge": 1})
        world.st["sneaking"] = True
        ex, state = world.session()
        skill = WindChargeBounce()
        skill.step(ex, state)
        skill.abort()
        self.assertFalse(world.st["sneaking"])
        self.assertTrue(any(m == "posture" for m, _ in world.calls))


class PlaceOrientedTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_retries_sides_until_facing_matches(self):
        world = TrialsWorld(pos=(0.5, 64.0, 0.5), items={"comparator": 1})
        ex, state = world.session()
        # 从西侧 (1,64,0) 放置 (2,64,0) 会得到 facing="east"，期望 "west" 时应挖掉换侧重试直到东侧 (3,64,0)
        blk = _run_gen(place_oriented(ex, state, (2, 64, 0), "comparator", want_facing="west"), ex, state)
        self.assertEqual(blk["properties"]["facing"], "west")
        self.assertGreaterEqual(ex.acts.count("place"), 2)
        self.assertGreaterEqual(ex.acts.count("dig"), 1)

    def test_retries_sides_for_want_axis_y(self):
        world = TrialsWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(3, 64, 0): "stone"},  # 东侧高台 (3,65,0) 可站立，作为垂直/高位视角
            items={"creaking_heart": 1},
        )
        attempts = {"n": 0}

        def props_fn(w, item, cell):
            attempts["n"] += 1
            return {"axis": "y"} if w.st["pos"][1] >= 65.0 or attempts["n"] >= 2 else {"axis": "x"}

        world.place_props_fn = props_fn
        ex, state = world.session()
        blk = _run_gen(place_oriented(ex, state, (2, 64, 0), "creaking_heart", want_axis="y"), ex, state)
        self.assertEqual(blk["properties"]["axis"], "y")

    def test_all_sides_fail_raises_replan_with_expected_and_observed(self):
        world = TrialsWorld(pos=(0.5, 64.0, 0.5), items={"comparator": 1})
        world.place_props_fn = lambda w, item, cell: {"facing": "north"}
        ex, state = world.session()
        with self.assertRaises(Replan) as ctx:
            _run_gen(place_oriented(ex, state, (2, 64, 0), "comparator", want_facing="south", max_sides=3), ex, state)
        self.assertIn("south", str(ctx.exception))
        self.assertIn("north", str(ctx.exception))
        self.assertEqual(ex.acts.count("place"), 3)
        self.assertEqual(ex.acts.count("dig"), 2)

    def test_no_constraint_returns_first_placement_immediately(self):
        world = TrialsWorld(pos=(0.5, 64.0, 0.5), items={"comparator": 1})
        ex, state = world.session()
        blk = _run_gen(place_oriented(ex, state, (1, 64, 0), "comparator"), ex, state)
        self.assertEqual(blk["name"], "comparator")
        self.assertIn(blk["properties"]["facing"], ("north", "south", "east", "west"))
        self.assertEqual(ex.acts.count("place"), 1)
        self.assertEqual(ex.acts.count("dig"), 0)


class PowerBlockAndWaveTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_power_block_once_places_switch_and_interacts(self):
        world = TrialsWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(2, 64, 0): "oxidized_copper_bulb"},
            block_props={(2, 64, 0): {"lit": "false"}},
            items={"stone_button": 1},
        )
        ex, state = world.session()
        res = _run_gen(power_block_once(ex, state, (2, 64, 0)), ex, state)
        self.assertEqual(res["item"], "stone_button")
        self.assertEqual(res["target"]["properties"]["lit"], "true")
        self.assertEqual(res["switch"]["properties"]["powered"], "true")
        self.assertIn("place", ex.acts)
        self.assertIn("interact", ex.acts)

    def test_power_block_once_approaches_distant_target_and_uses_wood_button(self):
        world = TrialsWorld(
            pos=(-4.5, 64.0, 0.5),
            blocks={(4, 64, 0): "oxidized_copper_bulb"},
            block_props={(4, 64, 0): {"lit": "false"}},
            items={"oak_button": 1},
        )
        ex, state = world.session()
        res = _run_gen(power_block_once(ex, state, (4, 64, 0)), ex, state)
        self.assertEqual(res["item"], "oak_button")
        self.assertIn("move_to", ex.acts)
        self.assertEqual(res["target"]["properties"]["lit"], "true")

    def test_power_block_once_missing_switch_raises_replan(self):
        world = TrialsWorld(blocks={(2, 64, 0): "oxidized_copper_bulb"}, items={})
        ex, state = world.session()
        with self.assertRaises(Replan):
            _run_gen(power_block_once(ex, state, (2, 64, 0)), ex, state)

    def test_clear_trial_wave_clears_respects_max_kills_and_hp_stop_loss(self):
        mobs = [
            {"id": 1, "type": "hostile", "name": "breeze", "pos": [2.5, 64.0, 0.5], "health": 30},
            {"id": 2, "type": "hostile", "name": "bogged", "pos": [3.5, 64.0, 0.5], "health": 16},
            {"id": 3, "type": "hostile", "name": "skeleton", "pos": [4.5, 64.0, 0.5], "health": 20},
        ]
        world = TrialsWorld(items={"iron_sword": 1}, entities=mobs)
        ex, state = world.session()
        killed = _run_gen(clear_trial_wave(ex, state, radius=16, max_kills=2, min_hp=10.0), ex, state)
        self.assertEqual(killed, 2)
        self.assertEqual(len(world.entities), 1)
        self.assertIn("equip", ex.acts)

        # 低血量止损
        low_hp_world = TrialsWorld(items={"iron_sword": 1}, entities=mobs[:2], hp=12.0)
        low_hp_world.kill_damage = 5.0  # 杀第一只后血量降到 7 < 10
        ex2, state2 = low_hp_world.session()
        with self.assertRaises(Replan):
            _run_gen(clear_trial_wave(ex2, state2, radius=16, max_kills=4, min_hp=10.0), ex2, state2)
        self.assertEqual(len(low_hp_world.entities), 1)


class UnlockTrialVaultTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_clears_wave_and_unlocks_vault(self):
        world = TrialsWorld(
            blocks={(4, 64, 0): "vault"},
            block_props={(4, 64, 0): {"vault_state": "active", "ominous": "false"}},
            items={"trial_key": 1, "iron_sword": 1},
            entities=[{"id": 10, "type": "hostile", "name": "breeze", "pos": [2.5, 64.0, 0.5], "health": 30}],
        )
        ex, state = world.session()
        skill = UnlockTrialVault(radius=16, clear_wave=True)
        steps = drive(skill, ex, state)
        self.assertTrue(any(s["op"] == "attack" for s in steps))
        self.assertTrue(any(s["op"] == "interact" and s.get("item") == "trial_key" for s in steps))
        self.assertEqual(world.items["trial_key"], 0)
        self.assertEqual(world.block_props[(4, 64, 0)]["vault_state"], "unlocking")
        self.assertEqual(skill.verify_done(state), "")

    def test_ominous_vault_uses_ominous_key_and_skips_wave_when_disabled(self):
        world = TrialsWorld(
            blocks={(2, 64, 0): "vault"},
            block_props={(2, 64, 0): {"vault_state": "active", "ominous": "true"}},
            items={"trial_key": 1, "ominous_trial_key": 1},
            entities=[{"id": 11, "type": "hostile", "name": "bogged", "pos": [3.5, 64.0, 0.5], "health": 16}],
        )
        ex, state = world.session()
        skill = UnlockTrialVault(radius=16, clear_wave=False)
        steps = drive(skill, ex, state)
        self.assertFalse(any(s["op"] == "attack" for s in steps))
        self.assertTrue(any(s["op"] == "interact" and s.get("item") == "ominous_trial_key" for s in steps))
        self.assertEqual(world.items["ominous_trial_key"], 0)
        self.assertEqual(skill.verify_done(state), "")

    def test_missing_vault_raises_env_blocked(self):
        world = TrialsWorld(items={"trial_key": 1})
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(UnlockTrialVault(radius=16), ex, state)


class WindChargeBounceTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_bounce_reaches_target_dy(self):
        world = TrialsWorld(pos=(0.5, 64.0, 0.5), items={"wind_charge": 2}, hp=20.0)
        ex, state = world.session()
        world.status_y_seq = [64.0, 67.5, 71.8]
        skill = WindChargeBounce(target_dy=7)
        steps = drive(skill, ex, state)
        self.assertEqual([s["op"] for s in steps], ["use_item"])
        self.assertEqual(steps[0]["target"], {"pos": [0.5, 63.5, 0.5]})
        self.assertEqual(skill.verify_done(state), "")

    def test_bounce_insufficient_height_raises_replan(self):
        world = TrialsWorld(pos=(0.5, 64.0, 0.5), items={"wind_charge": 1}, hp=20.0)
        ex, state = world.session()
        world.status_y_seq = [64.0, 66.0, 67.0, 66.5, 65.0, 64.0]
        with self.assertRaises(Replan):
            drive(WindChargeBounce(target_dy=7), ex, state)

    def test_low_ceiling_rejected_before_firing(self):
        world = TrialsWorld(pos=(0.5, 64.0, 0.5), blocks={(0, 68, 0): "stone"}, items={"wind_charge": 1}, hp=20.0)
        ex, state = world.session()
        with self.assertRaises(Replan):
            drive(WindChargeBounce(target_dy=7), ex, state)
        self.assertEqual(ex.acts, [])


class ScrapeLitCopperBulbTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_powers_unlit_bulb_then_scrapes(self):
        world = TrialsWorld(
            blocks={(2, 64, 0): "oxidized_copper_bulb"},
            block_props={(2, 64, 0): {"lit": "false"}},
            items={"iron_axe": 1, "lever": 1},
        )
        ex, state = world.session()
        skill = ScrapeLitCopperBulb(radius=16)
        steps = drive(skill, ex, state)
        self.assertIn("place", [s["op"] for s in steps])
        self.assertEqual(world.blocks[(2, 64, 0)], "weathered_copper_bulb")
        self.assertEqual(world.block_props[(2, 64, 0)]["lit"], "true")
        self.assertEqual(skill.verify_done(state), "")

    def test_already_lit_bulb_skips_powering(self):
        world = TrialsWorld(
            blocks={(2, 64, 0): "weathered_copper_bulb"},
            block_props={(2, 64, 0): {"lit": "true"}},
            items={"diamond_axe": 1, "lever": 1},
        )
        ex, state = world.session()
        skill = ScrapeLitCopperBulb(radius=16)
        steps = drive(skill, ex, state)
        self.assertNotIn("place", [s["op"] for s in steps])
        self.assertEqual(world.blocks[(2, 64, 0)], "exposed_copper_bulb")
        self.assertEqual(skill.verify_done(state), "")

    def test_waxed_unlit_bulb_lights_unwaxes_then_scrapes(self):
        world = TrialsWorld(
            blocks={(2, 64, 0): "waxed_oxidized_copper_bulb"},
            block_props={(2, 64, 0): {"lit": "false"}},
            items={"iron_axe": 1, "stone_button": 1},
        )
        ex, state = world.session()
        skill = ScrapeLitCopperBulb(radius=16)
        steps = drive(skill, ex, state)
        axe_clicks = [s for s in steps if s["op"] == "interact" and s.get("item") == "iron_axe"]
        self.assertEqual(len(axe_clicks), 2)
        self.assertEqual(world.blocks[(2, 64, 0)], "weathered_copper_bulb")
        self.assertEqual(world.block_props[(2, 64, 0)]["lit"], "true")
        self.assertEqual(skill.verify_done(state), "")


class ReadChiseledBookshelfTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_places_comparator_reads_facing_places_bookshelf_and_inserts_book(self):
        world = TrialsWorld(items={"comparator": 1, "chiseled_bookshelf": 1, "book": 1})
        ex, state = world.session()
        skill = ReadChiseledBookshelf(radius=16)
        drive(skill, ex, state)
        comp_pos = next(p for p, name in world.blocks.items() if name == "comparator")
        shelf_pos = next(p for p, name in world.blocks.items() if name == "chiseled_bookshelf")
        facing = world.block_props[comp_pos]["facing"]
        delta = {"north": (0, 0, -1), "south": (0, 0, 1), "east": (1, 0, 0), "west": (-1, 0, 0)}[facing]
        self.assertEqual(shelf_pos, (comp_pos[0] + delta[0], comp_pos[1] + delta[1], comp_pos[2] + delta[2]))
        self.assertEqual(world.items["book"], 0)
        self.assertEqual(world.block_props[shelf_pos]["books_stored"], 1)
        self.assertEqual(world.block_props[comp_pos]["powered"], "true")
        self.assertEqual(skill.verify_done(state), "")

    def test_moves_off_shelf_cell_when_comparator_input_points_at_player(self):
        world = TrialsWorld(pos=(0.5, 64.0, 0.5), items={"comparator": 1, "chiseled_bookshelf": 1, "writable_book": 1})
        # 强制比较器朝向指向玩家起始位置方向，验证放置书架前会自动让开不触发 blocked_by_self
        def force_toward_player(w, item, cell):
            if item == "comparator":
                dx, dz = 0 - cell[0], 0 - cell[2]
                facing = ("east" if dx > 0 else "west") if abs(dx) >= abs(dz) else ("south" if dz > 0 else "north")
                return {"facing": facing, "powered": "false"}
            return w._default_place_props(item, cell)
        world.place_props_fn = force_toward_player
        ex, state = world.session()
        skill = ReadChiseledBookshelf(radius=8)
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(world.items["writable_book"], 0)


class TransplantCreakingHeartTests(unittest.TestCase):
    def setUp(self):
        reset_all_voxel_caches()

    def test_places_vertical_pale_oak_and_creaking_heart_axis_y(self):
        world = TrialsWorld(items={"pale_oak_log": 2, "creaking_heart": 1})
        ex, state = world.session()
        skill = TransplantCreakingHeart(radius=16)
        drive(skill, ex, state)
        mid = next(p for p, name in world.blocks.items() if name == "creaking_heart")
        bot, top = (mid[0], mid[1] - 1, mid[2]), (mid[0], mid[1] + 1, mid[2])
        self.assertEqual(world.blocks[bot], "pale_oak_log")
        self.assertEqual(world.blocks[top], "pale_oak_log")
        for cell in (bot, mid, top):
            self.assertEqual(world.block_props[cell]["axis"], "y")
        self.assertEqual(skill.verify_done(state), "")

    def test_retries_when_creaking_heart_axis_initially_wrong(self):
        world = TrialsWorld(items={"pale_oak_log": 2, "creaking_heart": 1})
        heart_tries = {"n": 0}
        def props_fn(w, item, cell):
            if item == "creaking_heart":
                heart_tries["n"] += 1
                return {"axis": "x"} if heart_tries["n"] == 1 else {"axis": "y"}
            return {"axis": "y"}
        world.place_props_fn = props_fn
        ex, state = world.session()
        skill = TransplantCreakingHeart(radius=16)
        drive(skill, ex, state)
        self.assertEqual(heart_tries["n"], 2)
        self.assertEqual(skill.verify_done(state), "")

    def test_no_vertical_column_raises_env_blocked(self):
        ceiling = {(x, 65, z): "stone" for x in range(-5, 6) for z in range(-5, 6)}
        world = TrialsWorld(blocks=ceiling, items={"pale_oak_log": 2, "creaking_heart": 1})
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(TransplantCreakingHeart(radius=16), ex, state)


if __name__ == "__main__":
    unittest.main()
