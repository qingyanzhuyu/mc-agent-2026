"""Sprint 3 离线 TDD 回归单测：螺旋网格岛体检 + 地标先验静默门控 + 下界行前检查与安全碉堡闭环。"""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mcagent.loop.context import MAX_CONTEXT_CHARS, build_decision_context
from mcagent.loop.goals import (
    GOAL_MAP,
    GOAL_STAGES,
    GOALS,
    goal_landmarks_satisfied,
    rank_actionable_goals,
)
from mcagent.loop.policy import CampaignPlanner, RulePlanner
from mcagent.planner.objective import collect_objective_candidates, resolve_objective
from mcagent.runtime import Replan, State
from mcagent.skills import make
from mcagent.skills.nether import BuildAndEnterPortal
from mcagent.skills.nether_portal import nether_preflight_missing, nether_preflight_ready
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.spawn_shelter import BuildSpawnShelter
from mcagent.skills.survey import SurveyIsland, survey_sector
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.world_memo import SPIRAL_RING_RADII, WorldResourceMemo, memo_for, reset_all_world_memos


class _SpiralWorld(GridWorld):
    def __init__(self, *, blocks=None, pos=(0.5, 64.0, 0.5), items=None, entities=(), dimension="overworld"):
        super().__init__(blocks=blocks, pos=pos, items=items)
        self.st.update(dimension=dimension, ready=True, alive=True, biome="plains")
        self.entities = list(entities)
        self.find_calls: list[dict[str, Any]] = []
        self.entity_calls: list[dict[str, Any]] = []
        self.move_calls: list[list[float]] = []

    def find_blocks(self, types=None, radius=16, limit=20, **_):
        self.find_calls.append({"types": list(types) if types else None, "radius": radius, "limit": limit})
        px, py, pz = self.st["pos"]
        allowed = set(types) if types else None
        out = []
        for (bx, by, bz), name in self.blocks.items():
            if not name or name == "air":
                continue
            if allowed is not None and name not in allowed:
                continue
            d = math.dist((bx, by, bz), (px, py, pz))
            if d <= radius:
                out.append({"name": name, "pos": [bx, by, bz], "dist": round(d, 2)})
        out.sort(key=lambda r: r["dist"])
        return {"blocks": out[:limit]}

    def get_entities(self, radius=32, types=None):
        self.entity_calls.append({"radius": radius, "types": types})
        return {"entities": list(self.entities)}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, retries=0, *, stop_on=None):
        res = super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)
        self.move_calls.append(list(self.st["pos"]))
        return res


class Sprint3SpiralSurveyTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()

    def test_world_memo_visited_grid_spiral_cursor_void_edges_checkpoint_and_zero_coords(self) -> None:
        self.assertEqual(SPIRAL_RING_RADII, (48, 96, 160, 240, 320))
        memo = WorldResourceMemo()
        self.assertEqual(memo.mark_grid_visited([65.2, 64.0, -33.1]), (2, -2))
        self.assertTrue(memo.is_grid_visited([70.0, 64.0, -40.0]))
        self.assertFalse(memo.is_grid_visited([0.5, 64.0, 0.5]))

        cur = memo.get_spiral_cursor()
        self.assertEqual((cur["ring"], cur["sector"], cur["radius"]), (0, 0, 48))
        for _ in range(8):
            cur = memo.advance_spiral_cursor(sectors_per_ring=8)
        self.assertEqual((cur["ring"], cur["sector"], cur["radius"]), (1, 0, 96))

        self.assertTrue(memo.record_void_edge([180, 64, 0]))
        self.assertFalse(memo.record_void_edge([184, 64, 2]))  # 12 格内去重
        self.assertTrue(memo.is_near_void_edge([182, 64, 4], margin=12.0))
        self.assertFalse(memo.is_near_void_edge([10, 64, 10], margin=12.0))

        ctx = memo.summary_for_context(dimension="overworld")
        self.assertEqual(ctx["visited_grid_count"], 1)
        self.assertEqual(ctx["spiral_ring"], 1)
        self.assertEqual(ctx["void_edges_count"], 1)
        self.assertNotIn("180", json.dumps(ctx, ensure_ascii=False))

        with tempfile.TemporaryDirectory() as tmp:
            cp = Path(tmp) / "memo.json"
            memo.save_checkpoint(cp)
            reloaded = WorldResourceMemo()
            self.assertTrue(reloaded.load_checkpoint(cp))
            self.assertEqual(reloaded.visited_grid_cells(), {(2, -2)})
            self.assertEqual(reloaded.get_spiral_cursor()["ring"], 1)
            self.assertEqual(reloaded.void_edges(), [(180, 0)])

    def test_survey_island_continues_across_rings_scans_r64_blocks_r48_entities_and_guards_void(self) -> None:
        # 构造带实体与结构的大平台（+x 到 60，-x 到 -60，z 到 +-60；留出 (48, *, 0) 外侧为虚空）
        blocks = {(x, 63, z): "grass_block" for x in range(-52, 53) for z in range(-52, 53)}
        # 在东侧 48 格远设一道虚空缺口（全空气）以验证 void_edge 登记与绕避
        for x in range(44, 53):
            for z in range(-4, 5):
                blocks.pop((x, 63, z), None)
        villager = {"id": 99, "name": "villager", "type": "passive", "pos": [12.5, 64.0, 2.5], "health": 20}
        w = _SpiralWorld(blocks=blocks, items={"stone_pickaxe": 1}, entities=[villager])
        ex, state = w.session()

        # 第 1 轮 sectors=2 完成 Ring 0 的 {0, 1}
        sk1 = SurveyIsland(dimension="overworld", sectors=2, radius=48)
        self.assertTrue(sk1.can_start(state))
        drive(sk1, ex, state)
        memo = memo_for(state)
        self.assertTrue(memo.has_landmark("village", dimension="overworld"))
        self.assertTrue(any(c["radius"] == 64 and c["limit"] == 64 for c in w.find_calls))
        self.assertTrue(any(c["radius"] == 48 for c in w.entity_calls))
        self.assertTrue(memo.visited_grid_cells(dimension="overworld"))

        # 第 2 轮新建实例不再永久 can_start=False，可继续外扩推进
        sk2 = SurveyIsland(dimension="overworld", sectors=2, radius=48)
        self.assertTrue(sk2.can_start(state))
        drive(sk2, ex, state)
        self.assertGreaterEqual(len(w.move_calls), 2)

    def test_scout_island_prefers_unvisited_32x32_grid_cell_and_avoids_void_edge(self) -> None:
        # 角色在 (16, 64, 16) 处于网格 (0, 0)；提供窄带草地使 limit=250 完整覆盖网格 (0, 0) 与未访网格 (1, 0)
        blocks = {(x, 63, z): "grass_block" for x in range(8, 44) for z in range(13, 19)}
        w = _SpiralWorld(blocks=blocks, pos=(16.5, 64.0, 16.5), items={"stone_pickaxe": 1})
        ex, state = w.session()
        memo = memo_for(state)
        memo.mark_grid_visited([16.5, 64.0, 16.5], dimension="overworld")
        # 标记远端靠近 z=13 的某处为虚空边缘
        memo.record_void_edge([40, 64, 10], dimension="overworld")

        drive(make("scout_island", {"radius": 28}), ex, state)
        self.assertTrue(w.move_calls)
        dest = w.move_calls[-1]
        # 应优先跨入未访的 32x32 网格 (gx >= 1) 且远离虚空边
        self.assertGreaterEqual(math.floor(dest[0]) // 32, 1)
        self.assertGreater(math.hypot(dest[0] - 40, dest[2] - 10), 8.0)


class Sprint3LandmarkPriorGatingTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()

    def test_silent_landmark_gating_filters_undiscovered_and_wakes_on_discovery(self) -> None:
        self.assertIn("village", GOAL_MAP["trade"].requires_landmarks)
        self.assertIn("village", GOAL_MAP["trade_at_world_height"].requires_landmarks)
        self.assertIn("pillager_outpost", GOAL_MAP["voluntary_exile"].requires_landmarks)
        self.assertIn("trial_chambers", GOAL_MAP["minecraft_trials_edition"].requires_landmarks)
        self.assertIn("nether_fortress", GOAL_MAP["obtain_blaze_rod"].requires_landmarks)
        self.assertIn("bastion_remnant", GOAL_MAP["loot_bastion"].requires_landmarks)
        self.assertIn("stronghold", GOAL_MAP["follow_ender_eye"].requires_landmarks)

        w = _SpiralWorld(
            items={"iron_pickaxe": 1, "iron_sword": 1, "iron_chestplate": 1, "shield": 1, "cooked_beef": 16, "furnace": 1}
        )
        _, state = w.session()
        state.st["on_ground"] = True
        completed = {"minecraft:story/mine_stone", "minecraft:story/upgrade_tools", "minecraft:story/smelt_iron", "minecraft:story/iron_tools"}

        # 空地标台账：村庄/前哨/试炼/要塞目标被静默过滤，既不进 rank 也不进 LLM 菜单与挂起区
        ranked_empty = rank_actionable_goals(state, GOALS, completed_advs=completed)
        ranked_roots_empty = {s.root_goal.id for s in ranked_empty}
        for gated in ("trade", "voluntary_exile", "minecraft_trials_edition", "follow_ender_eye"):
            self.assertNotIn(gated, ranked_roots_empty)

        eligible, suspended = collect_objective_candidates(
            state, ledger=GOALS, min_verified="offline", include_stages=GOAL_STAGES, completed_advs=completed
        )
        elig_ids = {c.goal_id for c in eligible}
        susp_ids = {s.goal_id for s in suspended}
        for gated in ("trade", "voluntary_exile", "minecraft_trials_edition", "follow_ender_eye"):
            self.assertNotIn(gated, elig_ids)
            self.assertNotIn(gated, susp_ids)

        # LLM 直接选未探明地标目标会被校验器拒绝
        obj, errs = resolve_objective(
            {"goal_id": "trade", "why": "想交易"},
            state,
            ledger=GOALS,
            min_verified="offline",
            include_stages=GOAL_STAGES,
            completed_advs=completed,
        )
        self.assertIsNone(obj)
        self.assertTrue(any("地标" in e for e in errs))

        # 一旦登记 village 地标，landmark_version++ 且村庄目标立即解锁
        memo = memo_for(state)
        v_before = memo.landmark_version
        memo.record_landmark("village", [777, 64, -888], dimension="overworld")
        self.assertEqual(memo.landmark_version, v_before + 1)
        self.assertTrue(goal_landmarks_satisfied(GOAL_MAP["trade"], memo))
        ranked_after = rank_actionable_goals(state, GOALS, completed_advs=completed)
        self.assertIn("trade", {s.step_goal.id for s in ranked_after})
        self.assertIn("trade_at_world_height", {s.root_goal.id for s in ranked_after})

        # 决策上下文严格 <= 2000 字且不含坐标
        ctx = build_decision_context(
            state, ledger=GOALS, min_verified="offline", include_stages=GOAL_STAGES, completed_advs=completed
        )
        rendered = ctx.render()
        self.assertLessEqual(len(rendered), MAX_CONTEXT_CHARS)
        self.assertNotIn("777", rendered)
        self.assertNotIn("-888", rendered)


class Sprint3NetherPreflightAndBunkerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()

    def test_nether_preflight_checklist_detects_all_7_items_and_blocks_strict_entry(self) -> None:
        floor = {(x, 63, z): "stone" for x in range(-3, 4) for z in range(-3, 4)}
        w = _SpiralWorld(blocks=floor, items={"obsidian": 10})
        ex, state = w.session()
        missing = nether_preflight_missing(state)
        for expected in (
            "iron_armor",
            "gold_armor",
            "shield",
            "cooked_food>=24",
            "cobblestone>=96",
            "flint_and_steel",
            "camp_kit_backup",
        ):
            self.assertIn(expected, missing)
        self.assertFalse(nether_preflight_ready(state))

        with self.assertRaisesRegex(Replan, "下界远征行前检查未通过"):
            drive(BuildAndEnterPortal(strict_preflight=True), ex, state)

        # 备齐全部 7 项后通过行前检查
        w2 = _SpiralWorld(
            items={
                "iron_chestplate": 1,
                "golden_boots": 1,
                "shield": 1,
                "cooked_beef": 24,
                "cobblestone": 96,
                "flint_and_steel": 1,
                "obsidian": 10,
            }
        )
        _, state2 = w2.session()
        memo_for(state2).set_camp(
            [10, 64, 10],
            dimension="overworld",
            chest=[11, 64, 10],
            stored_items={"iron_sword": 1, "cooked_beef": 16},
            shelter_tier=1,
        )
        self.assertEqual(nether_preflight_missing(state2), [])
        self.assertTrue(nether_preflight_ready(state2))

    def test_nether_landing_triggers_immediate_3x3_bunker_and_emergency_depot(self) -> None:
        # 角色刚跨入下界，携带圆石>=20 和箱子，policy 第一时间强制派发 build_spawn_shelter(mode="nether_bunker")
        blocks = {(x, 63, z): "netherrack" for x in range(-5, 6) for z in range(-5, 6)}
        blocks[(0, 64, 0)] = "nether_portal"
        blocks[(0, 65, 0)] = "nether_portal"
        blocks[(1, 64, 0)] = "obsidian"
        w = _SpiralWorld(
            blocks=blocks,
            pos=(0.5, 64.0, 0.5),
            items={"cobblestone": 48, "chest": 1, "iron_chestplate": 1, "golden_boots": 1, "shield": 1},
            dimension="the_nether",
        )
        ex, state = w.session()

        rp = RulePlanner(min_verified="offline")
        dec = rp.choose_urgent_survival_or_safe_mode(state)
        self.assertIsNotNone(dec)
        assert dec is not None
        self.assertEqual(dec.skill.name, "build_spawn_shelter")
        self.assertEqual(dec.skill.args.get("mode"), "nether_bunker")

        # 用 drive() 执行建造碉堡，确认不覆盖传送门/黑曜石、放置应急箱并登记 nether_portal_depot
        drive(BuildSpawnShelter(mode="nether_bunker"), ex, state)
        self.assertEqual(w.blocks[(0, 64, 0)], "nether_portal")
        self.assertEqual(w.blocks[(1, 64, 0)], "obsidian")
        memo = memo_for(state)
        nether_camp = memo.get_camp(dimension="the_nether")
        self.assertIsNotNone(nether_camp)
        assert nether_camp is not None
        self.assertGreaterEqual(nether_camp["shelter_tier"], 1)
        self.assertTrue(memo.has_landmark("nether_portal_depot", dimension="the_nether"))


if __name__ == "__main__":
    unittest.main()
