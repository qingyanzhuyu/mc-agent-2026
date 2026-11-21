"""Sprint 2 离线 TDD 回归单测：共享主矿井/堵水/分层鱼骨支巷 + HuntMob/BreedAnimal/SleepInBed 解锁。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcagent.loop.goals import GOAL_MAP
from mcagent.loop.policy import RulePlanner
from mcagent.runtime import EnvBlocked, Executor, State
from mcagent.skills import REGISTRY, make
from mcagent.skills.combat import HuntMob
from mcagent.skills.combat_scene import KILL_MOB
from mcagent.skills.furnace_lease import lease_book_for
from mcagent.skills.iron import SmeltIron, has_ready_iron_lease, reset_iron_memo
from mcagent.skills.mine_shaft import (
    DIAMOND_LAYER_Y,
    IRON_LAYER_Y,
    branch_mine_level,
    plug_or_detour_water,
    resolve_shaft_entry,
)
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.survival_camp import SleepInBed
from mcagent.skills.survival_farm import BreedAnimal
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.world_memo import WorldResourceMemo, memo_for, reset_all_world_memos


class _ScanGridWorld(GridWorld):
    def find_blocks(self, types=None, radius=16, limit=32, **_):
        tset = set(types or ())
        px, py, pz = self.st["pos"]
        out = []
        for (bx, by, bz), name in self.blocks.items():
            if tset and name not in tset:
                continue
            if abs(bx - px) <= radius and abs(by - py) <= radius and abs(bz - pz) <= radius:
                # 仅返回至少有一面接触空气的暴露矿
                if any(
                    self.blocks.get((bx + dx, by + dy, bz + dz), "air") == "air"
                    for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
                ):
                    out.append({"name": name, "pos": [bx, by, bz]})
        return {"blocks": out[:limit]}


def _session(w: GridWorld) -> tuple[Executor, State]:
    ex, state = w.session()
    state.st.setdefault("dimension", "overworld")
    state.st.setdefault("ready", True)
    state.st.setdefault("alive", True)
    return ex, state


def _adv(goal: str | None = None) -> dict:
    return {"ready": True, "advancements": [{"id": goal, "status": "completed"}] if goal else []}


class Sprint2MineShaftMemoTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        reset_iron_memo()

    def test_mine_shaft_is_l2_primitive_not_registered(self) -> None:
        for name, cls in REGISTRY.items():
            self.assertNotEqual(cls.__module__, "mcagent.skills.mine_shaft", f"L2 原语不应注册: {name}")

    def test_world_memo_primary_mine_shaft_checkpoint_and_zero_coords(self) -> None:
        memo = WorldResourceMemo()
        memo.set_camp([10, 64, 20], dimension="overworld", mine_entry=[11, 63, 21])
        saved = memo.set_mine_shaft([11, 63, 21], dimension="overworld", heading=(0, 1))
        self.assertEqual(saved["entry"], [11, 63, 21])
        self.assertEqual(saved["lowest_y"], 63)
        self.assertEqual(saved["heading"], [0, 1])

        memo.update_mine_shaft(dimension="overworld", lowest_y=16, lowest_pos=[11, 16, 68], heading=(1, 0))
        # 不允许把 lowest_y 往浅处回退
        memo.update_mine_shaft(dimension="overworld", lowest_y=40, lowest_pos=[11, 40, 44])
        shaft = memo.primary_mine_shaft
        self.assertIsNotNone(shaft)
        assert shaft is not None
        self.assertEqual(shaft["lowest_y"], 16)
        self.assertEqual(shaft["lowest_pos"], [11, 16, 68])
        self.assertEqual(memo.next_mine_branch(16), 0)
        self.assertEqual(memo.next_mine_branch(16), 1)
        self.assertEqual(memo.next_mine_branch(-58), 0)

        ctx = memo.summary_for_context(dimension="overworld")
        self.assertTrue(ctx["has_mine_shaft"])
        self.assertEqual(ctx["mine_shaft_lowest_y"], 16)
        serialized = json.dumps(ctx, ensure_ascii=False)
        self.assertNotIn("68", serialized)
        self.assertNotIn("21", serialized)

        with tempfile.TemporaryDirectory() as tmp:
            cp = Path(tmp) / "memo.json"
            memo.save_checkpoint(cp)
            reloaded = WorldResourceMemo()
            self.assertTrue(reloaded.load_checkpoint(cp))
            self.assertEqual(reloaded.primary_mine_shaft["lowest_y"], 16)
            self.assertEqual(reloaded.primary_mine_shaft["branch_index"], {"16": 2, "-58": 1})

            # 旧版检查点（无 primary_mine_shafts）仍向后兼容
            raw = json.loads(cp.read_text(encoding="utf-8"))
            raw.pop("primary_mine_shafts", None)
            legacy_cp = Path(tmp) / "legacy.json"
            legacy_cp.write_text(json.dumps(raw), encoding="utf-8")
            legacy_memo = WorldResourceMemo()
            self.assertTrue(legacy_memo.load_checkpoint(legacy_cp))
            self.assertIsNone(legacy_memo.primary_mine_shaft)

    def test_resolve_shaft_entry_prefers_camp_mine_entry(self) -> None:
        w = GridWorld(pos=[10.5, 64.0, 10.5], items={"stone_pickaxe": 1})
        ex, state = _session(w)
        memo_for(state).set_camp([12, 64, 12], dimension="overworld", mine_entry=[12, 63, 13])
        shaft = resolve_shaft_entry(ex, state, dimension="overworld")
        self.assertEqual(shaft["entry"], [12, 63, 13])
        self.assertEqual(memo_for(state).primary_mine_shaft["entry"], [12, 63, 13])


class Sprint2WaterPlugAndBranchMiningTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        reset_iron_memo()

    def test_plug_water_with_single_cobblestone_recovers_descent(self) -> None:
        blocks: dict[tuple[int, int, int], str] = {}
        for x in range(-2, 5):
            for y in range(27, 35):
                for z in range(-2, 3):
                    blocks[(x, y, z)] = "stone"
        blocks[(0, 30, 0)] = "air"
        blocks[(0, 31, 0)] = "air"
        # 前方下探列 [1, 29..31, 0] 中有一格水
        blocks[(1, 30, 0)] = "water"
        w = GridWorld(blocks=blocks, pos=[0.5, 30.0, 0.5], items={"stone_pickaxe": 1, "cobblestone": 8})
        ex, state = _session(w)

        res: dict[str, object] = {}

        def _flow():
            ok, heading = yield from plug_or_detour_water(ex, state, (0, 30, 0), (1, 0))
            res["ok"] = ok
            res["heading"] = heading

        steps = drive(_flow(), ex, state)
        self.assertTrue(res["ok"])
        self.assertEqual(res["heading"], (1, 0))
        self.assertEqual(w.blocks[(1, 30, 0)], "cobblestone")
        self.assertTrue(any(s.get("op") == "place" and s.get("reason") == "plug_water" for s in steps))

    def test_detour_water_90_deg_when_no_placeable_blocks(self) -> None:
        blocks: dict[tuple[int, int, int], str] = {}
        for x in range(-2, 5):
            for y in range(27, 35):
                for z in range(-4, 5):
                    blocks[(x, y, z)] = "stone"
        blocks[(0, 30, 0)] = "air"
        blocks[(0, 31, 0)] = "air"
        # 前方列全是水，无圆石可堵，应 90° 横移绕开
        for dy in (-1, 0, 1, 2):
            blocks[(1, 29 + dy, 0)] = "water"
        w = GridWorld(blocks=blocks, pos=[0.5, 30.0, 0.5], items={"stone_pickaxe": 1})
        ex, state = _session(w)

        res: dict[str, object] = {}

        def _flow():
            ok, heading = yield from plug_or_detour_water(ex, state, (0, 30, 0), (1, 0))
            res["ok"] = ok
            res["heading"] = heading

        drive(_flow(), ex, state)
        self.assertTrue(res["ok"])
        # 应横向偏移出 z=0
        self.assertNotEqual(int(w.st["pos"][2]), 0)

    def test_branch_mine_level_y16_and_y_minus58_exposes_target_and_bonus_ores(self) -> None:
        for level_y, ore_name, bonus_name, pick in (
            (IRON_LAYER_Y, "iron_ore", "coal_ore", "stone_pickaxe"),
            (DIAMOND_LAYER_Y, "deepslate_diamond_ore", "deepslate_lapis_ore", "iron_pickaxe"),
        ):
            with self.subTest(level_y=level_y, ore=ore_name):
                blocks: dict[tuple[int, int, int], str] = {}
                for x in range(-2, 4):
                    for y in range(level_y - 2, level_y + 4):
                        for z in range(-2, 10):
                            blocks[(x, y, z)] = "stone" if level_y > 0 else "deepslate"
                blocks[(0, level_y, 0)] = "air"
                blocks[(0, level_y + 1, 0)] = "air"
                # 沿 +z 分支放一颗伴生矿在侧壁，再往前 3 格放目标矿
                blocks[(1, level_y + 1, 1)] = bonus_name
                blocks[(0, level_y, 3)] = ore_name
                w = _ScanGridWorld(blocks=blocks, pos=[0.5, float(level_y), 0.5], items={pick: 1})
                ex, state = _session(w)
                memo_for(state).set_mine_shaft([0, level_y, 0], dimension="overworld", heading=(1, 0))

                found_box: dict[str, list] = {}

                def _flow():
                    hits = yield from branch_mine_level(
                        ex,
                        state,
                        level_y=level_y,
                        target_ores=(ore_name,),
                        bonus_ores=(bonus_name,),
                        max_length=8,
                        scan_stride=3,
                        scan_radius=16,
                    )
                    found_box["hits"] = hits

                drive(_flow(), ex, state)
                self.assertIn([0, level_y, 3], found_box["hits"])
                self.assertEqual(memo_for(state).primary_mine_shaft["branch_index"][str(level_y)], 1)

    def test_ready_iron_lease_unblocks_smelt_iron_and_policy_dispatches_pickup(self) -> None:
        w = GridWorld(pos=[0.5, 64.0, 0.5], items={})
        _, state = _session(w)
        # 无镐无粗铁时默认 blocked
        self.assertTrue(SmeltIron.blocked(state))

        book = lease_book_for(state)
        book.register_lease(
            [2, 64, 0],
            station_kind="furnace",
            input_item="raw_iron",
            expected_output="iron_ingot",
            count=12,
            fuel_item="coal",
            dimension="overworld",
            started_at=0.0,
            ready_at=0.0,
        )
        self.assertTrue(has_ready_iron_lease(state))
        self.assertEqual(SmeltIron.blocked(state), "")
        rp = RulePlanner(min_verified="offline")
        dec = rp.choose_proactive_survival(state)
        self.assertIsNotNone(dec)
        assert dec is not None
        self.assertEqual(dec.skill.name, "smelt_iron")
        self.assertTrue(dec.skill.args.get("async_lease"))


class _OpenFieldCombatWorld(ScriptedWorld):
    """无掩体开阔平地战斗夹具，复现 5h 长跑中 HuntMob 的 59 次 env_blocked。"""

    def __init__(self, items=None, *, entities=(), armor=None, held=None, offhand=None, health=20):
        floor = {(x, 63, z): "stone" for x in range(-6, 12) for z in range(-6, 6)}
        super().__init__(blocks=floor, items=items)
        self.st.update(dimension="overworld", is_day=False, health=health, armor=armor or {})
        self.entities = list(entities)
        self.held = held
        self.offhand = offhand
        self.seen: set[str] = set()

    def inventory(self, timeout=10):
        snap = super().inventory(timeout)
        if self.held:
            snap["held"] = {"name": self.held, "count": 1}
        if self.offhand:
            snap["offhand"] = {"name": self.offhand, "count": 1, "slot": 45}
        return snap

    def get_entities(self, radius=32, types=None):
        rows = [dict(r) for r in self.entities if not types or r["type"] in types or r["name"] in types]
        return self._reply("get_entities", dict(radius=radius, types=types), fallback=lambda **_: {"entities": rows})

    def advancements(self, status="completed", include_recipes=False):
        return self._reply(
            "advancements",
            dict(status=status, include_recipes=include_recipes),
            fallback=lambda **_: {"ready": True, "advancements": [{"id": x, "status": "completed"} for x in sorted(self.seen)]},
        )


class Sprint2HuntMobOpenFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sleep = patch("mcagent.skills.combat_scene.time.sleep", return_value=None)
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def test_open_field_isolated_spider_with_sword_and_shield_kites_successfully(self) -> None:
        spider = {"id": 41, "name": "spider", "type": "hostile", "pos": [4.5, 64.0, 0.5], "health": 16}
        world = _OpenFieldCombatWorld({"iron_sword": 1, "shield": 1}, entities=[spider], held="iron_sword", offhand="shield")

        def attack(w, entity_id, **_):
            w.entities = [e for e in w.entities if e["id"] != entity_id]
            w.items["string"] = w.items.get("string", 0) + 2
            w.seen.add(KILL_MOB)
            return {"killed": True, "hits": 2}

        world.queue("posture", {"blocking": True}, {"blocking": False})
        world.queue("attack", attack)
        ex, state = world.session()
        records = drive(HuntMob(target="spider", drop="string", count=2), ex, state)
        self.assertEqual(state.count("string"), 2)
        kill_rec = next(r for r in records if r["op"] == "combat_kill_receipt")
        self.assertEqual(kill_rec["tactic"], "kite")

    def test_open_field_isolated_zombie_with_iron_armor_and_blocks_uses_pillar_up(self) -> None:
        zombie = {"id": 42, "name": "zombie", "type": "hostile", "pos": [2.5, 64.0, 0.5], "health": 20}
        world = _OpenFieldCombatWorld(
            {"iron_sword": 1, "cobblestone": 8},
            entities=[zombie],
            held="iron_sword",
            armor={"torso": {"name": "iron_chestplate"}},
        )

        def place_pillar(w, item, pos, **_):
            w.items[item] -= 1
            w.blocks[tuple(pos)] = item
            w.st["pos"] = [pos[0] + 0.5, float(pos[1] + 1), pos[2] + 0.5]
            return {"placed": item, "pos": pos}

        def control_hit(w, **_):
            w.entities = []
            w.seen.add(KILL_MOB)
            return {"ok": True}

        world.queue("posture", {"jumping": True}, {"jumping": False})
        world.queue("place", place_pillar, place_pillar, place_pillar)
        world.queue("look", {"ok": True})
        world.queue("control", control_hit)
        ex, state = world.session()
        records = drive(HuntMob(target="zombie"), ex, state)
        self.assertEqual(ex.acts.count("place"), 3)
        kill_rec = next(r for r in records if r["op"] == "combat_kill_receipt")
        self.assertEqual(kill_rec["tactic"], "bunker_hit")

    def test_open_field_without_shield_or_armor_remains_blocked(self) -> None:
        spider = {"id": 43, "name": "spider", "type": "hostile", "pos": [4.5, 64.0, 0.5], "health": 16}
        world = _OpenFieldCombatWorld({"stone_sword": 1}, entities=[spider], held="stone_sword")
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(HuntMob(target="spider"), ex, state)
        self.assertEqual(ex.acts, [])

    def test_open_field_with_second_hostile_within_isolation_radius_remains_blocked(self) -> None:
        spider = {"id": 44, "name": "spider", "type": "hostile", "pos": [4.5, 64.0, 0.5], "health": 16}
        second = {"id": 45, "name": "zombie", "type": "hostile", "pos": [7.5, 64.0, 0.5], "health": 20}
        world = _OpenFieldCombatWorld(
            {"iron_sword": 1, "shield": 1},
            entities=[spider, second],
            held="iron_sword",
            offhand="shield",
        )
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(HuntMob(target="spider"), ex, state)
        self.assertEqual(ex.acts, [])


class Sprint2BreedAndSleepTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()

    def test_breed_animal_scans_r32_pairs_before_digging_any_grass(self) -> None:
        floor = {(x, 63, z): "stone" for x in range(-3, 5) for z in range(-3, 4)}
        world = ScriptedWorld(blocks=floor, items={"wooden_hoe": 1})
        world.st.update(dimension="overworld", is_day=True)
        world.queue("advancements", _adv())
        world.queue("get_entities", {"entities": []})
        ex, state = world.session()
        with self.assertRaises(EnvBlocked) as ctx:
            drive(BreedAnimal(), ex, state)
        self.assertIn("32 格", ctx.exception.searched_volume)
        # 绝不先盲挖草丛
        self.assertEqual([op for op, _ in world.calls if op in ("find_blocks", "dig")], [])
        get_ent_req = next(req for op, req in world.calls if op == "get_entities")
        self.assertEqual(get_ent_req["radius"], 32)

    def test_breed_animal_expands_grass_scan_to_r32_limit48_after_finding_chickens(self) -> None:
        floor = {(x, 63, z): "stone" for x in range(-3, 5) for z in range(-3, 4)}
        floor[(2, 64, 0)] = "short_grass"
        world = ScriptedWorld(blocks=floor, items={"wooden_hoe": 1})
        world.st.update(dimension="overworld", is_day=True)
        c1 = {"id": 11, "pos": [1.5, 64.0, 0.5], "type": "animal", "name": "chicken", "health": 4}
        c2 = {"id": 12, "pos": [2.5, 64.0, 0.5], "type": "animal", "name": "chicken", "health": 4}
        world.queue("advancements", _adv(), _adv("minecraft:husbandry/breed_an_animal"))
        world.queue("get_entities", *([{"entities": [c1, c2]}] * 5))
        world.queue("find_blocks", {"blocks": [{"name": "short_grass", "pos": [2, 64, 0]}]})

        def dig_grass(w, *, pos, **_):
            w.blocks[tuple(pos)] = "air"
            w.items["wheat_seeds"] = w.items.get("wheat_seeds", 0) + 2
            return {"broken": "short_grass"}

        def feed(w, *, entity_id, item, **_):
            w.items[item] -= 1
            return {"entity": {"id": entity_id, "name": "chicken"}, "used": {item: 1}}

        world.queue("dig", dig_grass)
        world.queue("interact", feed, feed)
        ex, state = world.session()
        drive(BreedAnimal(), ex, state)
        fb_req = next(req for op, req in world.calls if op == "find_blocks")
        self.assertEqual((fb_req["radius"], fb_req["limit"]), (32, 48))
        self.assertEqual(ex.acts, ["dig", "interact", "interact"])

    def test_sleep_in_bed_blocked_guards_hopeless_runs_and_allows_camp_bed(self) -> None:
        w = GridWorld(pos=[0.5, 64.0, 0.5], items={})
        _, state = _session(w)
        # 既无床、又无 3 同色/可染羊毛、又无剪刀、又无已知床 -> 拦截
        self.assertTrue(SleepInBed.blocked(state))
        self.assertIn("羊毛", SleepInBed.blocked(state))

        # 记忆中登记营地室内床位 -> 放行
        memo_for(state).set_camp([0, 64, 0], dimension="overworld", bed=[1, 64, 0])
        self.assertEqual(SleepInBed.blocked(state), "")

        # 清空记忆，仅给 3 同色羊毛 -> 放行
        reset_all_world_memos()
        w2 = GridWorld(pos=[0.5, 64.0, 0.5], items={"white_wool": 3})
        _, st2 = _session(w2)
        self.assertEqual(SleepInBed.blocked(st2), "")

        # 确认 goals.py sleep_in_bed 已配置木料补料回退
        sleep_spec = GOAL_MAP["sleep_in_bed"]
        self.assertTrue(sleep_spec.gather_fallbacks)
        self.assertTrue(sleep_spec.gather_when_blocked)

    def test_sleep_in_bed_auto_crafts_planks_from_log_and_links_camp_bed(self) -> None:
        floor = {(x, 63, z): "stone" for x in range(-3, 5) for z in range(-3, 4)}
        floor[(-2, 64, 0)] = "crafting_table"
        world = ScriptedWorld(blocks=floor, items={"white_wool": 3, "oak_log": 1})
        world.st.update(dimension="overworld", is_day=False, is_sleeping=False)
        world.queue("advancements", _adv(), _adv("minecraft:adventure/sleep_in_bed"))
        world.queue("find_blocks", {"blocks": []}, {"blocks": [{"pos": [-2, 64, 0], "name": "crafting_table"}]})

        def craft_planks(w, *, item, **_):
            self.assertEqual(item, "oak_planks")
            w.items["oak_log"] -= 1
            w.items["oak_planks"] = w.items.get("oak_planks", 0) + 4
            return {"crafted": item, "gained": 4}

        def craft_bed(w, *, item, **_):
            self.assertEqual(item, "white_bed")
            w.items["white_wool"] -= 3
            w.items["oak_planks"] -= 3
            w.items["white_bed"] = 1
            return {"crafted": item, "gained": 1}

        def place_bed(w, *, item, pos, **_):
            x, y, z = pos
            w.items[item] -= 1
            w.blocks[(x, y, z)] = w.blocks[(x + 1, y, z)] = item
            return {"placed": item, "pos": pos}

        def sleep_fn(w, **_):
            w.st["is_sleeping"] = True
            return {"slept": True}

        world.queue("craft", craft_planks, craft_bed)
        world.queue("place", place_bed)
        world.queue("interact", sleep_fn)
        ex, state = world.session()
        memo_for(state).set_camp([0, 64, 0], dimension="overworld")
        drive(make("sleep_in_bed", {}), ex, state)
        self.assertEqual(ex.acts, ["craft", "craft", "place", "interact"])
        camp = memo_for(state).get_camp(dimension="overworld")
        self.assertIsNotNone(camp)
        assert camp is not None
        self.assertIsNotNone(camp["bed"])


if __name__ == "__main__":
    unittest.main()
