"""群系与结构感知底座重构离线回归单测（verified: offline）。

覆盖：
1. WorldResourceMemo.record_biome(..., pos=...) 锚点落账、landmark_version 递增、变体匹配与虚空岛穷尽判定；
2. MainLoop 零成本群系采样 + 沿途扫描实体->规范地标映射 + landmark_opportunity 唤醒；
3. format_world_landmarks 渲染已探群系与全岛探尽状态（严守 J17 零坐标与字符上限）；
4. rank_actionable_goals 顺路就近加权（1.35x）；
5. RulePlanner.choose_proactive_survival 周期性群系外扩勘探调度与耗尽停探；
6. L2 goto_known_landmark_or_biome 接入 T2/T3 群系技能（远离锚点时先导航再复扫完成）。
"""
from __future__ import annotations

from dataclasses import replace
import math
import tempfile
from pathlib import Path
import unittest

from mcagent.loop import CampaignPlanner, GoalSpec, LoopConfig, MainLoop, reset_world_memo
from mcagent.loop.context import MAX_CONTEXT_CHARS, build_decision_context, format_world_landmarks
from mcagent.loop.goals import GOAL_MAP, GOALS, goal_landmarks_satisfied, rank_actionable_goals
from mcagent.loop.policy import RulePlanner
from mcagent.runtime import State
from mcagent.skills import REGISTRY, Skill, base as skill_base, registry as skill_registry
from mcagent.skills.base import rec
from mcagent.skills.survey import SurveyIsland
from mcagent.skills.t2_overworld import BiomeQuickAction
from mcagent.skills.t3_biomes import CatchTadpole, SpyglassObserve
from mcagent.skills.testing import CountingExecutor, GridWorld, drive
from mcagent.skills.world_memo import (
    ENTITY_LANDMARK_TAGS,
    SPIRAL_RING_RADII,
    WorldResourceMemo,
    entity_landmark_tags,
    memo_for,
    reset_all_world_memos,
)

skill_registry.load_all()


class _FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


class _NoopSkill(Skill):
    name = "biome_infra_noop"
    description = "单步空操作技能"
    params = {}
    goal = "test:biome_infra_noop"
    verified = "offline"

    def run(self, ex, state):
        yield rec("noop")


class _RoadsideBiomeWorld:
    def __init__(self, *, biome: str = "meadow", entities=(), blocks=(), pos=(12.5, 64.0, -8.5)) -> None:
        self.biome = biome
        self.entities = list(entities)
        self.blocks = list(blocks)
        self.pos = list(pos)

    def wait_ready(self, timeout=120):
        return self.status()

    def status(self):
        return {
            "ready": True,
            "alive": True,
            "health": 20,
            "food": 20,
            "is_day": True,
            "dimension": "overworld",
            "biome": self.biome,
            "pos": list(self.pos),
        }

    def inventory(self):
        return {"items": []}

    def events(self, since=0, limit=100):
        return {"events": [], "next_id": since}

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None):
        return {"blocks": list(self.blocks)}

    def get_entities(self, radius=32, types=None):
        return {"entities": list(self.entities)}


class _RemoteAnchorGridWorld(GridWorld):
    """支持远程群系锚点实体扫描、移动与交互的离线世界替身。"""

    def __init__(self, *, pos=(0.5, 64.0, 0.5), blocks=None, items=None, entities=()) -> None:
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st.update(ready=True, alive=True, health=20, food=20, is_day=True, dimension="overworld")
        self.entities = list(entities)
        self.move_calls: list[list[float]] = []

    def get_entities(self, radius=32, types=None):
        px, py, pz = self.st["pos"]
        allowed = {t.removeprefix("minecraft:") for t in types} if types else None
        out = []
        for ent in self.entities:
            ename = str(ent.get("name", "")).removeprefix("minecraft:")
            if allowed is not None and ename not in allowed:
                continue
            if math.dist(ent["pos"], (px, py, pz)) <= radius:
                out.append(dict(ent))
        return {"entities": out}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
        res = super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)
        self.move_calls.append(list(self.st["pos"]))
        return res

    def interact(self, pos=None, entity_id=None, item=None, face=None, sneak=False, *, stop_on=None):
        if item == "brush":
            self.items["armadillo_scute"] = self.items.get("armadillo_scute", 0) + 1
            return {"ok": True, "changed": True}
        if item == "water_bucket":
            self.items["water_bucket"] = max(0, self.items.get("water_bucket", 0) - 1)
            self.items["tadpole_bucket"] = self.items.get("tadpole_bucket", 0) + 1
            return {"ok": True, "changed": True}
        return {"ok": True, "changed": True}

    def look(self, yaw=None, pitch=None, pos=None, entity_id=None):
        return {"ok": True}

    def use_item(self, item, target=None, ms=None, *, stop_on=None, **_):
        return {"ok": True, "used": item}


class BiomeReconInfraTests(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        reset_all_world_memos()

    def tearDown(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        reset_all_world_memos()

    def test_record_biome_anchor_version_variant_matching_and_checkpoint(self) -> None:
        memo = WorldResourceMemo(clock=lambda: 100.0)
        self.assertEqual(memo.landmark_version, 0)

        # 首次记录群系带坐标：递增 landmark_version 并存储群系锚点（block_cell 按 math.floor 取整）
        self.assertTrue(memo.record_biome("minecraft:windswept_savanna", pos=[120.4, 70.0, -45.2]))
        self.assertEqual(memo.landmark_version, 1)
        self.assertEqual(memo.biomes(), {"windswept_savanna"})
        self.assertEqual(memo.landmarks("windswept_savanna"), [(120, 70, -46)])

        # 重复记录同群系：不重复递增 landmark_version；32 格以外可补充新锚点
        self.assertFalse(memo.record_biome("windswept_savanna", pos=[125.0, 70.0, -40.0]))
        self.assertEqual(memo.landmark_version, 1)
        self.assertEqual(len(memo.landmarks("windswept_savanna")), 1)
        self.assertFalse(memo.record_biome("windswept_savanna", pos=[180.0, 70.0, -45.0]))
        self.assertEqual(memo.landmark_version, 1)
        self.assertEqual(len(memo.landmarks("windswept_savanna")), 2)

        # biome_or_landmark_positions 支持 OR 表达与子变种前缀/后缀匹配
        matched = memo.biome_or_landmark_positions("savanna|badlands", dimension="overworld")
        self.assertEqual(set(matched), {(120, 70, -46), (180, 70, -45)})

        # 检查点持久化完整保留群系与锚点
        with tempfile.TemporaryDirectory() as tmp:
            cp = Path(tmp) / "memo.json"
            memo.save_checkpoint(cp)
            reloaded = WorldResourceMemo(clock=lambda: 100.0)
            self.assertTrue(reloaded.load_checkpoint(cp))
            self.assertEqual(reloaded.biomes(), {"windswept_savanna"})
            self.assertEqual(set(reloaded.biome_or_landmark_positions("savanna|badlands")), {(120, 70, -46), (180, 70, -45)})

    def test_island_survey_exhaustion_blocks_survey_island(self) -> None:
        state = State()
        state.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "dimension": "overworld", "pos": [0.5, 64.0, 0.5]})
        state.set_inventory({"items": [{"name": "stone_pickaxe", "count": 1, "slot": 0}]})
        memo = memo_for(state)
        memo.set_spawn([0, 64, 0], dimension="overworld")

        self.assertFalse(memo.is_island_survey_exhausted(dimension="overworld"))
        self.assertTrue(SurveyIsland(dimension="overworld", sectors=2, radius=48).can_start(state))

        # 6 个不同方位扇区均触达虚空边界 + 完成 4 扇区 -> 判定全岛已探尽
        for idx in range(4):
            memo.mark_sector_done(idx, dimension="overworld")
        for angle_deg in (0, 50, 100, 150, 210, 270):
            rad = math.radians(angle_deg)
            memo.record_void_edge([200.0 * math.cos(rad), 64.0, 200.0 * math.sin(rad)], dimension="overworld")

        self.assertTrue(memo.is_island_survey_exhausted(dimension="overworld"))
        self.assertFalse(SurveyIsland(dimension="overworld", sectors=2, radius=48).can_start(state))
        ctx = memo.summary_for_context(dimension="overworld")
        self.assertTrue(ctx.get("island_survey_exhausted"))

        # 或者螺旋环全部跑完同样判尽
        memo2 = WorldResourceMemo()
        for _ in range(len(SPIRAL_RING_RADII) * 8):
            memo2.advance_spiral_cursor(dimension="overworld", sectors_per_ring=8)
        self.assertTrue(memo2.is_island_survey_exhausted(dimension="overworld"))

    def test_roadside_scan_records_biome_and_maps_entities_to_canonical_landmarks(self) -> None:
        self.assertEqual(entity_landmark_tags("armadillo"), ("savanna",))
        self.assertEqual(entity_landmark_tags("parrot"), ("jungle",))
        self.assertEqual(entity_landmark_tags("tadpole"), ("swamp",))
        self.assertIn("villager", ENTITY_LANDMARK_TAGS)

        clock = _FakeClock(1000.0)
        world = _RoadsideBiomeWorld(
            biome="minecraft:meadow",
            entities=[
                {"id": 1, "name": "armadillo", "pos": [20.5, 64.0, 5.5]},
                {"id": 2, "name": "parrot", "pos": [25.5, 70.0, -4.5]},
                {"id": 3, "name": "villager", "pos": [18.5, 64.0, 8.5]},
            ],
        )
        state = State()
        state.set_status(world.status())
        state.set_inventory(world.inventory())
        planner = CampaignPlanner(
            mode="campaign",
            strategy="rule",
            registry={_NoopSkill.name: _NoopSkill},
            ledger=(GoalSpec(id="g1", title="测试", tier="T1", score=5, skill=_NoopSkill.name, stage="graduated"),),
            clock=clock,
        )
        cfg = LoopConfig(enable_monitor=False, enable_roadside_scan=True, roadside_scan_interval_s=10.0)
        loop = MainLoop(world, state, CountingExecutor(world, state), planner, config=cfg, clock=clock, log=lambda *_: None)

        clock.advance(15.0)
        found = loop._maybe_roadside_scan()
        memo = memo_for(state)
        self.assertIn("meadow", found)
        self.assertIn("savanna", found)
        self.assertIn("jungle", found)
        self.assertIn("village", found)
        self.assertIn("meadow", memo.biomes())
        self.assertTrue(memo.has_landmark("meadow"))
        self.assertTrue(goal_landmarks_satisfied(GOAL_MAP["play_jukebox_in_meadows"], memo))
        self.assertTrue(goal_landmarks_satisfied(GOAL_MAP["brush_armadillo"], memo))
        self.assertTrue(goal_landmarks_satisfied(GOAL_MAP["spyglass_at_parrot"], memo))
        self.assertTrue(goal_landmarks_satisfied(GOAL_MAP["trade"], memo))

    def test_context_renders_visited_biomes_and_exhaustion_without_coords(self) -> None:
        line = format_world_landmarks({
            "stations_ready": ["crafting_table"],
            "landmarks_discovered": ["village"],
            "visited_biomes": ["plains", "meadow", "swamp"],
            "island_survey_exhausted": True,
            "pending_landmark_goals": 3,
        })
        self.assertIn("已探群系=plains,meadow,swamp", line)
        self.assertIn("全岛已探尽", line)
        self.assertLessEqual(len(line), 200)

        state = State()
        state.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "dimension": "overworld", "pos": [333.5, 64.0, -444.5]})
        state.set_inventory({"items": [{"name": "iron_pickaxe", "count": 1, "slot": 0}]})
        memo_for(state).record_biome("meadow", pos=[333, 64, -444], dimension="overworld")
        rendered = build_decision_context(state, ledger=GOALS, min_verified="offline").render()
        self.assertIn("已探群系=meadow", rendered)
        self.assertNotIn("333", rendered)
        self.assertNotIn("-444", rendered)
        self.assertLessEqual(len(rendered), MAX_CONTEXT_CHARS)

    def test_locality_bonus_prioritizes_nearby_biome_goal(self) -> None:
        state = State()
        state.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "dimension": "overworld", "biome": "jungle", "pos": [10.5, 64.0, 10.5]})
        state.set_inventory({"items": [{"name": "iron_sword", "count": 1, "slot": 0}, {"name": "cooked_beef", "count": 10, "slot": 1}]})
        memo = memo_for(state)
        memo.record_biome("savanna", pos=[500, 64, 500], dimension="overworld")
        memo.record_biome("jungle", pos=[10, 64, 10], dimension="overworld")

        g_far = GoalSpec(
            id="far_goal", title="远处草原", tier="T2", score=15, skill="biome_quick_action",
            est_cost_s=100.0, stage="graduated", requires_landmarks=("savanna",),
        )
        g_near = GoalSpec(
            id="near_goal", title="脚下丛林", tier="T2", score=15, skill="spyglass_observe",
            est_cost_s=100.0, stage="graduated", requires_landmarks=("jungle",),
        )
        ranked = rank_actionable_goals(state, (g_far, g_near))
        self.assertEqual([s.step_goal.id for s in ranked], ["near_goal", "far_goal"])

    def test_periodic_recon_triggers_when_armed_and_stops_when_exhausted(self) -> None:
        clock = _FakeClock(1000.0)
        state = State()
        state.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "is_day": True, "dimension": "overworld", "pos": [0.5, 64.0, 0.5]})
        state.set_inventory({
            "items": [
                {"name": "iron_sword", "count": 1, "slot": 0},
                {"name": "shield", "count": 1, "slot": 1},
                {"name": "cooked_beef", "count": 10, "slot": 2},
                {"name": "stone_pickaxe", "count": 1, "slot": 3},
            ],
            "armor": {"chest": {"name": "iron_chestplate", "count": 1}},
        })
        # 先建营地与避难所避免触发建营/避难所快路径
        memo = memo_for(state)
        memo.set_camp([0, 64, 0], dimension="overworld", chest=[1, 64, 0], shelter_tier=1)
        memo.set_spawn([0, 64, 0], dimension="overworld")

        gated_ledger = (
            replace(GOAL_MAP["brush_armadillo"], stage="graduated"),
        )
        rp = RulePlanner(
            REGISTRY,
            ledger=gated_ledger,
            include_stages=("graduated",),
            min_verified="offline",
            recon_interval_s=120.0,
            recon_initial_delay_s=60.0,
            clock=clock,
        )

        # 未满 initial_delay 不触发
        clock.advance(30.0)
        self.assertIsNone(rp.choose_proactive_survival(state))

        # 满 initial_delay 且存在待勘探地标目标 -> 触发 survey_island
        clock.advance(35.0)
        dec = rp.choose_proactive_survival(state)
        self.assertIsNotNone(dec)
        assert dec is not None
        self.assertEqual(dec.skill.name, "survey_island")
        self.assertIn("周期性群系", dec.reason)

        # 冷却期内不重复触发
        clock.advance(50.0)
        self.assertIsNone(rp.choose_proactive_survival(state))

        # 全岛探尽后即使超过冷却也不再触发
        clock.advance(100.0)
        for _ in range(len(SPIRAL_RING_RADII) * 8):
            memo.advance_spiral_cursor(dimension="overworld", sectors_per_ring=8)
        self.assertIsNone(rp.choose_proactive_survival(state))

    def test_t2_and_t3_skills_navigate_to_known_biome_anchor_before_failing(self) -> None:
        # 1) T2 BiomeQuickAction(brush_armadillo): 玩家在 (0, 64, 0)，犰狳在远处 savanna 锚点 (100, 64, 0)
        blocks = {(x, 63, z): "grass_block" for x in range(-4, 108) for z in range(-4, 5)}
        w1 = _RemoteAnchorGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"brush": 1},
            entities=[{"id": 11, "name": "armadillo", "type": "animal", "pos": [101.5, 64.0, 0.5]}],
        )
        ex1, st1 = w1.session()
        memo_for(st1).record_biome("windswept_savanna", pos=[100, 64, 0], dimension="overworld")
        drive(BiomeQuickAction(action="brush_armadillo", radius=24), ex1, st1)
        self.assertGreaterEqual(st1.count("armadillo_scute"), 1)
        self.assertTrue(any(p[0] >= 90.0 for p in w1.move_calls))

        # 2) T3 CatchTadpole: 玩家在 (0, 64, 0)，蝌蚪在远处 swamp 锚点 (90, 64, 0)
        w2 = _RemoteAnchorGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"water_bucket": 1},
            entities=[{"id": 22, "name": "tadpole", "type": "animal", "pos": [91.5, 64.0, 0.5]}],
        )
        ex2, st2 = w2.session()
        memo_for(st2).record_biome("swamp", pos=[90, 64, 0], dimension="overworld")
        drive(CatchTadpole(radius=24), ex2, st2)
        self.assertGreaterEqual(st2.count("tadpole_bucket"), 1)
        self.assertTrue(any(p[0] >= 80.0 for p in w2.move_calls))

        # 3) T3 SpyglassObserve(parrot): 玩家在 (0, 64, 0)，鹦鹉在远处 jungle 锚点 (85, 64, 0)
        w3 = _RemoteAnchorGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"spyglass": 1},
            entities=[{"id": 33, "name": "parrot", "type": "animal", "pos": [86.5, 64.0, 0.5]}],
        )
        ex3, st3 = w3.session()
        memo_for(st3).record_biome("bamboo_jungle", pos=[85, 64, 0], dimension="overworld")
        drive(SpyglassObserve(target="parrot", radius=24), ex3, st3)
        self.assertTrue(any(p[0] >= 75.0 for p in w3.move_calls))


if __name__ == "__main__":
    unittest.main()
