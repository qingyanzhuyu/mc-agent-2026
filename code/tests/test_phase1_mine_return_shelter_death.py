"""阶段一回归单测：地下矿道就地工作台/沿主矿井返程、森林丘陵避难所选址与轻量整平、--stop-on-death。"""
from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mcagent.client import ApiError
from mcagent.loop.__main__ import build_parser
from mcagent.loop.engine import LoopConfig, MainLoop
from mcagent.loop.policy import PlanDecision
from mcagent.loop.starter_skills import CraftGear, GatherWood, reset_world_memo
from mcagent.runtime import Executor, Replan, State
from mcagent.skills.base import Skill
from mcagent.skills.iron_gear import CraftIronGear
from mcagent.skills.items import MINE_TRAILS, bare
from mcagent.skills.mine_return import is_underground_in_mine
from mcagent.skills.shelter_site import evaluate_shelter_site
from mcagent.skills.spawn_shelter import BuildSpawnShelter, _site_is_valid
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


class _MineWorld(GridWorld):
    """支持地下矿道、地表树/营地工作台、就地合成与路径约束的离线夹具。"""

    def __init__(self, *, pos=(0.5, 34.0, 0.5), blocks=None, items=None, fail_surface_direct_from_y=50.0, block_shaft_nav=False):
        super().__init__(pos=list(pos), blocks=blocks or {}, items=items or {})
        self.st.update(dimension="overworld", ready=True, alive=True, on_ground=True, health=20.0, food=20.0)
        self.fail_surface_direct_from_y = fail_surface_direct_from_y
        self.block_shaft_nav = block_shaft_nav
        self.move_targets: list[list[float]] = []

    def find_blocks(self, types=None, radius=32, limit=32, **kwargs):
        wanted = {bare(t) for t in (types or [])}
        px, py, pz = self.st["pos"]
        hits = []
        for (bx, by, bz), name in self.blocks.items():
            bname = bare(name)
            if wanted and bname not in wanted:
                continue
            d = math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5))
            if d <= float(radius):
                hits.append({"name": bname, "pos": [bx, by, bz], "distance": round(d, 2)})
        hits.sort(key=lambda h: h["distance"])
        return {"blocks": hits[: int(limit)]}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
        dest = list(pos) if pos is not None else [x, y, z]
        self.move_targets.append(list(dest))
        cur = list(self.st["pos"])
        if self.block_shaft_nav and float(dest[1]) > float(cur[1]) + 2.0:
            raise ApiError("path_not_found", "shaft blocked")
        # 模拟地下直接寻路地表树/箱子失败，但沿主矿井分段上升（单步 dy<=14）成功
        if (
            self.fail_surface_direct_from_y is not None
            and float(cur[1]) < self.fail_surface_direct_from_y
            and float(dest[1]) >= self.fail_surface_direct_from_y
            and (float(dest[1]) - float(cur[1])) > 16.0
        ):
            raise ApiError("path_not_found", "cannot path directly from deep mine to surface")
        self.st["pos"] = [float(dest[0]), float(dest[1]), float(dest[2])]
        self.calls.append(("move_to", {"pos": dest, "rng": rng}))
        return {"pos": list(self.st["pos"])}

    def craft(self, item, count=1, *, stop_on=None):
        self.calls.append(("craft", {"item": item, "count": count}))
        if item.endswith("_planks"):
            log = item.removesuffix("_planks") + "_log"
            self.items[log] -= 1
            self.items[item] = self.items.get(item, 0) + 4
            return {"crafted": item}
        if item == "crafting_table":
            for k in list(self.items):
                if k.endswith("_planks") and self.items[k] >= 4:
                    self.items[k] -= 4
                    break
            self.items["crafting_table"] = self.items.get("crafting_table", 0) + 1
            return {"crafted": "crafting_table"}
        if item == "stick":
            for k in list(self.items):
                if k.endswith("_planks") and self.items[k] >= 2:
                    self.items[k] -= 2
                    break
            self.items["stick"] = self.items.get("stick", 0) + 4
            return {"crafted": "stick"}
        if item == "iron_pickaxe":
            self.items["iron_ingot"] -= 3
            self.items["stick"] -= 2
            self.items["iron_pickaxe"] = self.items.get("iron_pickaxe", 0) + 1
            return {"crafted": "iron_pickaxe"}
        if item == "stone_pickaxe":
            self.items["cobblestone"] -= 3
            self.items["stick"] -= 2
            self.items["stone_pickaxe"] = self.items.get("stone_pickaxe", 0) + 1
            return {"crafted": "stone_pickaxe"}
        return super().craft(item, count, stop_on=stop_on)

    def place(self, item, pos=None, *, stop_on=None):
        if pos is None:
            px, py, pz = map(math.floor, self.st["pos"])
            pos = [px + 1, py, pz]
        res = super().place(item, pos, stop_on=stop_on)
        return {**res, "pos": list(pos)}


class _HillyShelterWorld(GridWorld):
    def __init__(self, *, pos=(0.5, 64.0, 0.5), blocks=None, items=None):
        super().__init__(pos=list(pos), blocks=blocks or {}, items=items or {})
        self.st.update(dimension="overworld", ready=True, alive=True, on_ground=True, health=20.0, food=20.0)
        self.dug_cells: list[tuple[int, int, int]] = []
        self.placed_cells: list[tuple[tuple[int, int, int], str]] = []

    def dig(self, pos, collect=True, *, stop_on=None):
        cell = (int(pos[0]), int(pos[1]), int(pos[2]))
        self.dug_cells.append(cell)
        return super().dig(pos, collect=collect, stop_on=stop_on)

    def place(self, item, pos=None, *, stop_on=None):
        cell = (int(pos[0]), int(pos[1]), int(pos[2]))
        self.placed_cells.append((cell, bare(item)))
        return super().place(item, pos, stop_on=stop_on)


class Phase1MineReturnShelterDeathTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_world_memo()
        reset_all_world_memos()
        reset_all_voxel_caches()
        MINE_TRAILS.clear()

    def tearDown(self) -> None:
        reset_world_memo()
        reset_all_world_memos()
        reset_all_voxel_caches()
        MINE_TRAILS.clear()

    def test_craft_iron_gear_underground_crafts_temp_table_in_place_when_wood_available(self) -> None:
        blocks = {(x, 33, z): "stone" for x in range(-2, 3) for z in range(-2, 3)}
        blocks[(0, 36, 0)] = "stone"
        world = _MineWorld(
            pos=(0.5, 34.0, 0.5),
            blocks=blocks,
            items={"iron_ingot": 3, "stick": 2, "oak_log": 2},
        )
        ex, state = world.session()
        memo_for(state).set_mine_shaft([0, 64, 0], dimension="overworld", lowest_y=34, lowest_pos=[0, 34, 0])
        self.assertTrue(is_underground_in_mine(ex, state))

        steps = drive(CraftIronGear(item="iron_pickaxe"), ex, state)
        self.assertEqual(world.items.get("iron_pickaxe", 0), 1)
        self.assertTrue(any(s.get("op") == "place" and s.get("item") == "crafting_table" for s in steps))
        self.assertTrue(memo_for(state).has_station("crafting_table", dimension="overworld"))
        # 就地合成放台，无需回地表
        self.assertLess(float(world.st["pos"][1]), 40.0)

    def test_craft_iron_gear_underground_returns_along_shaft_to_camp_table_when_no_wood(self) -> None:
        blocks = {
            (0, 33, 0): "stone",
            (0, 36, 0): "stone",
            (2, 64, 0): "crafting_table",
            (2, 63, 0): "grass_block",
        }
        world = _MineWorld(
            pos=(0.5, 34.0, 0.5),
            blocks=blocks,
            items={"iron_ingot": 3, "stick": 2},
        )
        ex, state = world.session()
        memo = memo_for(state)
        memo.set_mine_shaft([0, 64, 0], dimension="overworld", lowest_y=34, lowest_pos=[0, 34, 0])
        memo.set_camp([0, 64, 0], dimension="overworld", crafting_table=[2, 64, 0])

        steps = drive(CraftIronGear(item="iron_pickaxe"), ex, state)
        self.assertEqual(world.items.get("iron_pickaxe", 0), 1)
        self.assertGreaterEqual(float(world.st["pos"][1]), 62.0)
        self.assertTrue(any(s.get("reason") == "return_primary_mine_shaft" for s in steps))

    def test_gather_wood_underground_returns_via_shaft_before_chopping_surface_logs(self) -> None:
        blocks = {
            (0, 33, 0): "stone",
            (0, 36, 0): "stone",
            (4, 64, 0): "oak_log",
            (4, 65, 0): "oak_log",
            (4, 63, 0): "grass_block",
        }
        world = _MineWorld(pos=(0.5, 34.0, 0.5), blocks=blocks, items={})
        ex, state = world.session()
        memo_for(state).set_mine_shaft([0, 64, 0], dimension="overworld", lowest_y=34, lowest_pos=[0, 34, 0])

        steps = drive(GatherWood(count=1), ex, state)
        self.assertGreaterEqual(world.items.get("oak_log", 0), 1)
        self.assertFalse(any(s.get("error_code") == "path_not_found" for s in steps))
        self.assertTrue(any(s.get("reason") == "return_primary_mine_shaft" for s in steps))

    def test_gather_wood_underground_fails_fast_without_poisoning_surface_wood_memo(self) -> None:
        blocks = {
            (0, 33, 0): "stone",
            (0, 36, 0): "stone",
            (4, 64, 0): "oak_log",
            (4, 63, 0): "grass_block",
        }
        world = _MineWorld(pos=(0.5, 34.0, 0.5), blocks=blocks, items={}, block_shaft_nav=True)
        ex, state = world.session()
        memo = memo_for(state)
        memo.set_mine_shaft([0, 64, 0], dimension="overworld", lowest_y=34, lowest_pos=[0, 34, 0])

        with self.assertRaisesRegex(Replan, "地下矿道"):
            drive(GatherWood(count=1), ex, state)
        # 地表原木不得因在地下盲搜而被拉黑
        self.assertFalse(memo.is_blocked((4, 64, 0), dimension="overworld", category="wood"))

    def test_craft_gear_underground_local_table_and_shaft_return(self) -> None:
        blocks = {(x, 33, z): "stone" for x in range(-2, 3) for z in range(-2, 3)}
        blocks[(0, 36, 0)] = "stone"
        blocks[(2, 64, 0)] = "crafting_table"
        # 1) 有富余原木 -> 就地建台并合石镐
        w1 = _MineWorld(pos=(0.5, 34.0, 0.5), blocks=dict(blocks), items={"cobblestone": 3, "stick": 2, "oak_log": 1})
        ex1, st1 = w1.session()
        memo_for(st1).set_mine_shaft([0, 64, 0], dimension="overworld", lowest_y=34, lowest_pos=[0, 34, 0])
        memo_for(st1).set_camp([0, 64, 0], dimension="overworld", crafting_table=[2, 64, 0])
        drive(CraftGear(item="stone_pickaxe"), ex1, st1)
        self.assertEqual(w1.items.get("stone_pickaxe", 0), 1)
        self.assertLess(float(w1.st["pos"][1]), 40.0)

        # 2) 无木材但营地有工作台 -> 沿主矿井回营地合石镐
        reset_world_memo()
        w2 = _MineWorld(pos=(0.5, 34.0, 0.5), blocks=dict(blocks), items={"cobblestone": 3, "stick": 2})
        ex2, st2 = w2.session()
        memo_for(st2).set_mine_shaft([0, 64, 0], dimension="overworld", lowest_y=34, lowest_pos=[0, 34, 0])
        memo_for(st2).set_camp([0, 64, 0], dimension="overworld", crafting_table=[2, 64, 0])
        drive(CraftGear(item="stone_pickaxe"), ex2, st2)
        self.assertEqual(w2.items.get("stone_pickaxe", 0), 1)
        self.assertGreaterEqual(float(w2.st["pos"][1]), 62.0)

    def test_hilly_forest_shelter_tolerates_1_block_elevation_and_foliage_and_levels(self) -> None:
        # 构造森林丘陵 5×5 基座：部分格低 1 格（y=62 实心）、部分格高 1 格（y=64 泥土）、夹杂草/花/低叶与室内营地箱
        blocks: dict[tuple[int, int, int], str] = {}
        for dx in range(-4, 5):
            for dz in range(-4, 5):
                blocks[(dx, 62, dz)] = "dirt"
                blocks[(dx, 63, dz)] = "grass_block"
        # 2 个 1 格低洼格（y=63 为空气，y=62 为实心泥土 -> 需垫平）
        del blocks[(-2, 63, -2)]
        del blocks[(-2, 63, -1)]
        # 2 个 1 格凸起格（y=64 为泥土 -> 需削平）
        blocks[(2, 64, 1)] = "dirt"
        blocks[(1, 64, 0)] = "grass_block"
        # 草、花与低矮树叶
        blocks[(0, 64, 0)] = "short_grass"
        blocks[(2, 64, -2)] = "dandelion"
        blocks[(-1, 65, 1)] = "oak_leaves"
        # 室内已有营地箱（必须保留，不得挖掘！）
        blocks[(-1, 64, -1)] = "chest"

        world = _HillyShelterWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"cobblestone": 64, "dirt": 8, "oak_door": 1},
        )
        ex, state = world.session()
        memo_for(state).set_camp([0, 64, 0], dimension="overworld", chest=[-1, 64, -1])

        self.assertTrue(_site_is_valid(ex, 0, 64, 0, radius=2))
        ev = evaluate_shelter_site(ex, 0, 64, 0, radius=2)
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(set(ev["pad_cells"]), {(-2, 63, -2), (-2, 63, -1)})
        self.assertEqual(set(ev["trim_cells"]), {(2, 64, 1), (1, 64, 0)})

        drive(BuildSpawnShelter(mode="tier1_shelter"), ex, state)
        # 营地箱完好无损
        self.assertEqual(world.blocks.get((-1, 64, -1)), "chest")
        self.assertNotIn((-1, 64, -1), world.dug_cells)
        # 1 格低洼已垫平为实心
        self.assertIn(world.blocks.get((-2, 63, -2)), ("dirt", "cobblestone"))
        self.assertIn(world.blocks.get((-2, 63, -1)), ("dirt", "cobblestone"))
        # 避难所成功升级 Tier 1
        camp = memo_for(state).get_camp(dimension="overworld")
        self.assertGreaterEqual(int(camp["shelter_tier"]), 1)

    def test_shelter_site_probes_variable_surface_y_and_rejects_water_or_cliff(self) -> None:
        # 出生点 (0, 64, 0) 中心有水不可建；r=10 处 (10, 67, 0) 为高 3 格的丘陵平坦斑块
        blocks: dict[tuple[int, int, int], str] = {}
        blocks[(0, 63, 0)] = "water"
        for dx in range(-3, 4):
            for dz in range(-3, 4):
                blocks[(10 + dx, 66, dz)] = "grass_block"
        world = _HillyShelterWorld(pos=(0.5, 64.0, 0.5), blocks=blocks, items={"cobblestone": 64})
        ex, state = world.session()
        memo_for(state).set_spawn([0, 64, 0], dimension="overworld")

        # 含水点拒绝；悬崖（y=63、y=62 均空）拒绝；丘陵高地 (10, 67, 0) 自动探出并建房
        self.assertFalse(_site_is_valid(ex, 0, 64, 0, radius=2))
        self.assertFalse(_site_is_valid(ex, -10, 64, 0, radius=2))
        self.assertTrue(_site_is_valid(ex, 10, 67, 0, radius=2))

        drive(BuildSpawnShelter(mode="tier1_shelter"), ex, state)
        camp = memo_for(state).get_camp(dimension="overworld")
        self.assertEqual(camp["pos"], [10, 67, 0])
        self.assertGreaterEqual(int(camp["shelter_tier"]), 1)

    def test_cli_stop_on_death_flag_defaults_false_and_parses_true(self) -> None:
        parser = build_parser()
        self.assertFalse(parser.parse_args([]).stop_on_death)
        self.assertTrue(parser.parse_args(["--stop-on-death"]).stop_on_death)

    def test_engine_stop_on_death_exits_cleanly_records_context_and_disconnects(self) -> None:
        class _DyingSkill(Skill):
            name = "dying_skill_stub"
            description = "stub"
            goal = "test:die"
            verified = "offline"

            def run(self, ex, state):
                yield {"op": "move_to"}
                client.dead = True
                raise ApiError("dead", "player killed by skeleton")

        class _FakeClient:
            def __init__(self):
                self.disconnected = False
                self.dead = False

            def wait_ready(self, timeout=120):
                return {"ready": True, "alive": True, "health": 20.0, "food": 20.0, "pos": [1.5, 64.0, 2.5], "dimension": "overworld"}

            def status(self):
                if self.dead:
                    return {
                        "ready": True, "alive": False, "health": 0.0, "food": 16.0,
                        "pos": [1.5, 64.0, 2.5], "dimension": "overworld",
                        "last_death": {"cause": "shot by skeleton", "pos": [1.5, 64.0, 2.5]},
                    }
                return {"ready": True, "alive": True, "health": 20.0, "food": 20.0, "pos": [1.5, 64.0, 2.5], "dimension": "overworld"}

            def inventory(self):
                return {"items": [{"name": "iron_pickaxe", "count": 1, "slot": 0}], "empty_slots": 35}

            def events(self, since=0, limit=50):
                return {"events": [], "next": since}

            def disconnect(self):
                self.disconnected = True

        class _FakeObserver:
            tape = None
            target_goals = None
            def __init__(self):
                self.server_advancements, self.death_records, self.run_end_reason = set(), [], None
            def on_run_start(self, *_a, **_kw): pass
            def on_progress_tick(self, *_a, **_kw): pass
            def update_goals_reached(self, *_a, **_kw): pass
            def on_skill_start(self, *_a, **_kw): pass
            def on_step(self, *_a, **_kw): pass
            def on_skill_end(self, *_a, **_kw): pass
            def on_death_recovered(self, state, **kwargs): self.death_records.append(kwargs)
            def on_run_end(self, state, client=None, stop_reason="", **kwargs): self.run_end_reason = stop_reason

        class _FakePlanner:
            mode = "campaign"
            rules = SimpleNamespace(active_goals=(), completed_advancements=set(), ledger_map={})
            def select(self, state, recent_failures): return PlanDecision(_DyingSkill(), "rule", "test")
            def blocked_targets(self, *args, **kwargs): return {}
            def release_skill_materials(self): pass

        client = _FakeClient()
        state = State()
        with tempfile.TemporaryDirectory() as td:
            ex = Executor(client, state, log_path=str(Path(td) / "actions.jsonl"))
            obs = _FakeObserver()
            cfg = LoopConfig(enable_monitor=False, stop_on_death=True, disconnect_on_exit=True, enable_roadside_scan=False, max_steps=10)
            res = MainLoop(client, state, ex, _FakePlanner(), config=cfg, observer=obs, log=lambda *_: None).run()
        self.assertEqual(res.stop_reason, "death_stopped")
        self.assertEqual(res.deaths_recovered, 0)
        self.assertTrue(client.disconnected)
        self.assertEqual(obs.run_end_reason, "death_stopped")
        self.assertEqual(len(obs.death_records), 1)
        self.assertEqual(obs.death_records[0]["cause"], "shot by skeleton")
        self.assertEqual(obs.death_records[0]["death_pos"], [1.5, 64.0, 2.5])


if __name__ == "__main__":
    unittest.main()
