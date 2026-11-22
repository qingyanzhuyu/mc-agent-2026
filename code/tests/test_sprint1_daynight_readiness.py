"""Sprint 1.4 生存就绪度门控、昼夜双轨排序、主动生存策略与持久化离线单测（verified: offline）。"""
from __future__ import annotations

import re
import time
import unittest
from typing import Any, Mapping, Optional

import mcagent.loop  # noqa: F401  触发 starter_skills 注册
import mcagent.skills  # noqa: F401  触发全量 L3 技能注册
from mcagent.loop import (
    GOAL_MAP,
    GOALS,
    CampaignPlanner,
    LoopConfig,
    MainLoop,
    RulePlanner,
    reset_world_memo,
)
from mcagent.loop.context import (
    build_decision_context,
    format_world_landmarks,
    summarize_inventory_and_status,
)
from mcagent.loop.goals import rank_actionable_goals, survival_readiness_multiplier
from mcagent.runtime import EnvBlocked, Executor, State, effective_hostile_flee_dist
from mcagent.skills import base as skill_base
from mcagent.skills import make
from mcagent.skills.voxel_cache import reset_all_voxel_caches
from mcagent.skills.world_memo import memo_for, reset_all_world_memos

_COORD_TRIPLET_RE = re.compile(r"[\[\(]\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*[\]\)]")


def _reset_globals() -> None:
    skill_base._COOLDOWN.clear()
    reset_world_memo()
    reset_all_world_memos()
    reset_all_voxel_caches()


def _make_state(
    *,
    items: Optional[Mapping[str, int]] = None,
    health: float = 20.0,
    food: int = 20,
    dimension: str = "overworld",
    pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
    is_day: Optional[bool] = None,
    time_of_day: Optional[int] = None,
    armor_points: Optional[int] = None,
    armor: Optional[Mapping[str, Any]] = None,
    nearest_hostile: Optional[Mapping[str, Any]] = None,
    in_safe_mode: bool = False,
) -> State:
    state = State()
    st: dict[str, Any] = {
        "ready": True,
        "alive": True,
        "health": health,
        "food": food,
        "dimension": dimension,
        "pos": [float(v) for v in pos],
        "on_ground": True,
        "sneaking": False,
    }
    if is_day is not None:
        st["is_day"] = is_day
    if time_of_day is not None:
        st["time_of_day"] = time_of_day
    if armor_points is not None:
        st["armor_points"] = armor_points
    if armor is not None:
        st["armor"] = dict(armor)
    if nearest_hostile is not None:
        st["nearest_hostile"] = dict(nearest_hostile)
    state.set_status(st)
    state.set_inventory(
        {
            "items": [
                {"name": name, "count": count, "slot": idx}
                for idx, (name, count) in enumerate((items or {}).items())
                if count > 0
            ]
        }
    )
    if in_safe_mode:
        state.safe_mode_until = time.monotonic() + 30.0
    return state


class _MockRespawnClient:
    """仅用于 MainLoop._recover_session_or_respawn 的最小离线客户端替身。"""

    def __init__(self, *, status_payload: dict[str, Any], inventory_items: list[dict[str, Any]]) -> None:
        self._status_payload = dict(status_payload)
        self._inventory_items = [dict(x) for x in inventory_items]

    def status(self, **_: Any) -> dict[str, Any]:
        return dict(self._status_payload)

    def wait_ready(self, timeout: float = 45.0) -> dict[str, Any]:
        return dict(self._status_payload)

    def inventory(self) -> dict[str, Any]:
        return {"items": [dict(x) for x in self._inventory_items]}

    def events(self, since: int = 0, limit: int = 200) -> dict[str, Any]:
        return {"events": [], "last_seq": since}


class _FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.t = initial

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


class TestSurvivalReadinessGates(unittest.TestCase):
    def setUp(self) -> None:
        _reset_globals()

    def tearDown(self) -> None:
        _reset_globals()

    def test_early_readiness_gate_discounts_risky_roots_until_sword_and_food(self) -> None:
        from mcagent.loop.goals import GoalSpec

        risky_ids = ("kill_a_mob", "shoot_arrow", "ol_betsy", "find_fortress", "adventuring_time")
        risky_specs = {
            gid: GOAL_MAP.get(gid)
            or GoalSpec(id=gid, title=gid, tier="T2", score=15, skill="scout_island")
            for gid in risky_ids
        }
        bare_state = _make_state(items={"stone_pickaxe": 1}, is_day=True)
        for gid, g in risky_specs.items():
            self.assertAlmostEqual(
                survival_readiness_multiplier(g, g, bare_state, ()),
                0.35,
                msg=f"{gid} should be discounted 0.35 before sword+food",
            )

        # Even when stone_sword is held (making kill_a_mob directly actionable), lack of food keeps 0.35 discount
        sword_no_food = _make_state(items={"stone_pickaxe": 1, "stone_sword": 1}, is_day=True)
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["kill_a_mob"], GOAL_MAP["kill_a_mob"], sword_no_food, ()),
            0.35,
        )
        ranked_no_food = rank_actionable_goals(
            sword_no_food,
            (GOAL_MAP["prep_wood"], GOAL_MAP["prep_wooden_pickaxe"], GOAL_MAP["mine_stone"], GOAL_MAP["upgrade_tools"], GOAL_MAP["prep_stone_sword"], GOAL_MAP["kill_a_mob"]),
            is_day=True,
        )
        idx_by_root = {step.root_goal.id: idx for idx, step in enumerate(ranked_no_food)}
        self.assertLess(idx_by_root["mine_stone"], idx_by_root["kill_a_mob"])

        wood_pick_sword = _make_state(items={"wooden_pickaxe": 1, "cobblestone": 3, "stone_sword": 1}, is_day=True)
        ranked_wood = rank_actionable_goals(
            wood_pick_sword,
            (GOAL_MAP["upgrade_tools"], GOAL_MAP["kill_a_mob"]),
            is_day=True,
        )
        idx_wood = {step.root_goal.id: idx for idx, step in enumerate(ranked_wood)}
        self.assertLess(idx_wood["upgrade_tools"], idx_wood["kill_a_mob"])

        # Holding stone_sword + cooked_beef >= 6 unlocks 1.0
        ready_beef = _make_state(items={"stone_pickaxe": 1, "stone_sword": 1, "cooked_beef": 6}, is_day=True)
        for gid, g in risky_specs.items():
            self.assertAlmostEqual(survival_readiness_multiplier(g, g, ready_beef, ()), 1.0)

        # Holding stone_sword + bread x4 (restore 20 >= 18) also unlocks 1.0
        ready_bread = _make_state(items={"stone_pickaxe": 1, "stone_sword": 1, "bread": 4}, is_day=True)
        for gid, g in risky_specs.items():
            self.assertAlmostEqual(survival_readiness_multiplier(g, g, ready_bread, ()), 1.0)

    def test_explicit_target_goal_bypasses_readiness_and_daynight_multipliers(self) -> None:
        # Empty bag at night: kill_a_mob normally suffers early (0.35) + night combat (0.15)
        night_bare = _make_state(items={"stone_sword": 1}, is_day=False)
        ranked_default = rank_actionable_goals(night_bare, GOALS, is_day=False)
        self.assertNotEqual(ranked_default[0].root_goal.id, "kill_a_mob")

        ranked_targeted = rank_actionable_goals(night_bare, GOALS, target_goal="kill_a_mob", is_day=False)
        self.assertEqual(ranked_targeted[0].root_goal.id, "kill_a_mob")

        # Comparing two explicit targets at night: kill_a_mob (roi=5/40=0.125) vs smelt_iron (roi=5/60=0.0833)
        # Without target_goal, smelt_iron gets 1.6x and kill_a_mob gets 0.15x; with target_goal both bypass mult (=1.0).
        dual_state = _make_state(
            items={"stone_pickaxe": 1, "stone_sword": 1, "cooked_beef": 6, "furnace": 1},
            is_day=False,
        )
        completed = {"minecraft:story/mine_stone", "minecraft:story/upgrade_tools"}
        sub_ledger = (GOAL_MAP["smelt_iron"], GOAL_MAP["kill_a_mob"])
        ranked_untargeted = rank_actionable_goals(dual_state, sub_ledger, completed_advs=completed, is_day=False)
        self.assertEqual([s.root_goal.id for s in ranked_untargeted], ["smelt_iron", "kill_a_mob"])

        ranked_dual_target = rank_actionable_goals(
            dual_state,
            sub_ledger,
            completed_advs=completed,
            target_goal=("kill_a_mob", "smelt_iron"),
            is_day=False,
        )
        self.assertEqual([s.root_goal.id for s in ranked_dual_target], ["kill_a_mob", "smelt_iron"])

    def test_post_iron_gate_inactive_in_stone_age_or_empty_bag(self) -> None:
        empty_state = _make_state(items={}, is_day=True)
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["prep_wood"], GOAL_MAP["shiny_gear"], empty_state, ()),
            1.0,
        )
        ranked_empty = rank_actionable_goals(empty_state, GOALS, is_day=True)
        self.assertEqual(ranked_empty[0].root_goal.id, "shiny_gear")
        self.assertEqual(ranked_empty[0].step_goal.id, "prep_wood")

        stone_state = _make_state(items={"stone_pickaxe": 1, "cobblestone": 8}, is_day=True)
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["prep_furnace"], GOAL_MAP["shiny_gear"], stone_state, ()),
            1.0,
        )
        ranked_stone = rank_actionable_goals(stone_state, GOALS, is_day=True)
        self.assertEqual(ranked_stone[0].root_goal.id, "shiny_gear")
        self.assertIn(ranked_stone[0].step_goal.id, ("prep_stone_sword", "prep_furnace"))

    def test_post_iron_foundation_gate_progression_and_armor_shield_unlock(self) -> None:
        # 1) smelt_iron achieved, no iron_pickaxe -> iron_tools (3.5x) outranks naked risky roots (0.2x)
        post_smelt = _make_state(
            items={"stone_pickaxe": 1, "stone_sword": 1, "cooked_beef": 6, "furnace": 1, "stick": 4, "iron_ingot": 4},
            is_day=True,
        )
        completed = {"minecraft:story/mine_stone", "minecraft:story/upgrade_tools", "minecraft:story/smelt_iron"}
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["iron_tools"], GOAL_MAP["iron_tools"], post_smelt, completed),
            3.5,
        )
        for naked_gid in ("shiny_gear", "mine_diamond", "enter_the_nether", "lava_bucket"):
            self.assertAlmostEqual(
                survival_readiness_multiplier(GOAL_MAP[naked_gid], GOAL_MAP[naked_gid], post_smelt, completed),
                0.2,
            )
        ranked_no_pick = rank_actionable_goals(post_smelt, GOALS, completed_advs=completed, is_day=True)
        self.assertEqual(ranked_no_pick[0].root_goal.id, "iron_tools")
        idx_no_pick = {s.root_goal.id: i for i, s in enumerate(ranked_no_pick)}
        self.assertLess(idx_no_pick["iron_tools"], idx_no_pick["lava_bucket"])

        # 2) iron_pickaxe=1, iron_ingot=4, unarmored & no shield -> obtain_armor (3.5x) & deflect_arrow (2.8x if hp>=14)
        post_pick = _make_state(
            items={
                "iron_pickaxe": 1,
                "iron_ingot": 4,
                "stone_sword": 1,
                "cooked_beef": 6,
                "furnace": 1,
                "stick": 4,
            },
            health=20.0,
            is_day=True,
        )
        completed_pick = completed | {"minecraft:story/iron_tools"}
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["obtain_armor"], GOAL_MAP["obtain_armor"], post_pick, completed_pick),
            3.5,
        )
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["deflect_arrow"], GOAL_MAP["deflect_arrow"], post_pick, completed_pick),
            2.8,
        )
        post_iron_ledger = tuple(
            GOAL_MAP[gid]
            for gid in (
                "smelt_iron",
                "iron_tools",
                "obtain_armor",
                "deflect_arrow",
                "mine_diamond",
                "shiny_gear",
                "prep_diamond_pickaxe",
                "form_obsidian",
                "enter_the_nether",
                "lava_bucket",
            )
        )
        ranked_naked = rank_actionable_goals(post_pick, post_iron_ledger, completed_advs=completed_pick, is_day=True)
        idx_naked = {s.root_goal.id: i for i, s in enumerate(ranked_naked)}
        diamond_slot_root = next(
            s.root_goal.id for s in ranked_naked if s.step_goal.id == "mine_diamond"
        )
        for priority_gid in ("obtain_armor", "deflect_arrow"):
            self.assertLess(idx_naked[priority_gid], idx_naked[diamond_slot_root])
            self.assertLess(idx_naked[priority_gid], idx_naked["lava_bucket"])

        # Verify deflect_arrow loses 2.8x boost when hp < 14
        low_hp = _make_state(
            items={
                "iron_pickaxe": 1,
                "iron_ingot": 4,
                "stone_sword": 1,
                "cooked_beef": 6,
                "furnace": 1,
            },
            health=13.0,
            is_day=True,
        )
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["deflect_arrow"], GOAL_MAP["deflect_arrow"], low_hp, completed_pick),
            1.0,
        )

        # 3) Equipped armor + shield -> shiny_gear / mine_diamond reclaim top rank
        for armor_kwargs in (
            {"armor_points": 2},
            {"items_extra": {"iron_boots": 1}},
        ):
            extra = armor_kwargs.get("items_extra", {})
            geared = _make_state(
                items={
                    "iron_pickaxe": 1,
                    "iron_ingot": 4,
                    "stone_sword": 1,
                    "cooked_beef": 6,
                    "furnace": 1,
                    "shield": 1,
                    **extra,
                },
                armor_points=armor_kwargs.get("armor_points"),
                health=20.0,
                is_day=True,
            )
            ranked_geared = rank_actionable_goals(geared, GOALS, completed_advs=completed_pick, is_day=True)
            self.assertIn(ranked_geared[0].root_goal.id, ("shiny_gear", "mine_diamond"))

    def test_raw_step_roi_fields_unmodified_by_multiplier(self) -> None:
        state_early = _make_state(items={"stone_pickaxe": 1, "stone_sword": 1}, is_day=True)
        state_ready = _make_state(items={"stone_pickaxe": 1, "stone_sword": 1, "cooked_beef": 6}, is_day=True)
        # Ensure multiplier genuinely differs for kill_a_mob
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["kill_a_mob"], GOAL_MAP["kill_a_mob"], state_early, ()),
            0.35,
        )
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["kill_a_mob"], GOAL_MAP["kill_a_mob"], state_ready, ()),
            1.0,
        )
        step_early = next(
            s for s in rank_actionable_goals(state_early, (GOAL_MAP["kill_a_mob"],), is_day=True)
            if s.root_goal.id == "kill_a_mob"
        )
        step_ready = next(
            s for s in rank_actionable_goals(state_ready, (GOAL_MAP["kill_a_mob"],), is_day=True)
            if s.root_goal.id == "kill_a_mob"
        )
        self.assertEqual(step_early.path_score, step_ready.path_score)
        self.assertEqual(step_early.path_cost, step_ready.path_cost)
        self.assertEqual(step_early.roi, step_ready.roi)
        self.assertEqual(step_early.marginal_roi, step_ready.marginal_roi)


class TestDayNightDualTrackRanking(unittest.TestCase):
    def setUp(self) -> None:
        _reset_globals()

    def tearDown(self) -> None:
        _reset_globals()

    def test_neutral_when_daytime_absent_or_in_nether(self) -> None:
        no_time = _make_state(items={"stone_sword": 1, "cooked_beef": 6, "white_bed": 1})
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["sleep_in_bed"], GOAL_MAP["sleep_in_bed"], no_time, ()),
            1.0,
        )
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["plant_seed"], GOAL_MAP["plant_seed"], no_time, ()),
            1.0,
        )
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["mine_stone"], GOAL_MAP["mine_stone"], no_time, ()),
            1.0,
        )

        nether_night = _make_state(
            items={"stone_sword": 1, "cooked_beef": 6, "white_bed": 1},
            dimension="the_nether",
            is_day=False,
        )
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["sleep_in_bed"], GOAL_MAP["sleep_in_bed"], nether_night, ()),
            1.0,
        )
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["plant_seed"], GOAL_MAP["plant_seed"], nether_night, ()),
            1.0,
        )
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["smelt_iron"], GOAL_MAP["smelt_iron"], nether_night, ()),
            1.0,
        )

    def test_night_bed_sleep_priority_ranks_first_against_full_ledger(self) -> None:
        variants = (
            {"white_bed": 1},
            {"white_wool": 3},
            {},
        )
        for idx, extra in enumerate(variants):
            _reset_globals()
            st = _make_state(
                items={
                    "oak_planks": 4,
                    "stone_pickaxe": 1,
                    "stone_sword": 1,
                    "cooked_beef": 6,
                    "iron_pickaxe": 1,
                    "iron_boots": 1,
                    "shield": 1,
                    **extra,
                },
                armor_points=2,
                is_day=False,
            )
            if idx == 2:
                memo_for(st).record_station("bed", [2, 64, 2], dimension="overworld")

            mult = survival_readiness_multiplier(
                GOAL_MAP["sleep_in_bed"], GOAL_MAP["sleep_in_bed"], st, (), is_day=False
            )
            self.assertAlmostEqual(mult, 6.0)
            ranked = rank_actionable_goals(st, GOALS, is_day=False)
            self.assertEqual(ranked[0].root_goal.id, "sleep_in_bed")
            self.assertEqual(ranked[0].step_goal.id, "sleep_in_bed")

    def test_night_no_bed_unarmored_prefers_underground_and_suppresses_surface_combat(self) -> None:
        st = _make_state(
            items={"stone_pickaxe": 1, "stone_sword": 1, "cooked_beef": 6, "furnace": 1, "oak_planks": 4},
            is_day=False,
        )
        for ug in ("smelt_iron", "mine_stone", "mine_diamond"):
            self.assertAlmostEqual(
                survival_readiness_multiplier(GOAL_MAP[ug], GOAL_MAP[ug], st, (), is_day=False),
                1.6,
            )
        for surf in ("prep_wood", "breed_an_animal", "plant_seed"):
            self.assertAlmostEqual(
                survival_readiness_multiplier(GOAL_MAP[surf], GOAL_MAP[surf], st, (), is_day=False),
                0.15,
            )
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["kill_a_mob"], GOAL_MAP["kill_a_mob"], st, (), is_day=False),
            0.15,
        )

        sub_ledger = (
            GOAL_MAP["prep_wood"],
            GOAL_MAP["plant_seed"],
            GOAL_MAP["breed_an_animal"],
            GOAL_MAP["kill_a_mob"],
            GOAL_MAP["mine_stone"],
            GOAL_MAP["smelt_iron"],
        )
        ranked = rank_actionable_goals(
            st,
            sub_ledger,
            completed_advs={"minecraft:story/upgrade_tools"},
            is_day=False,
        )
        top_ids = [s.step_goal.id for s in ranked[:2]]
        self.assertEqual(set(top_ids), {"mine_stone", "smelt_iron"})

    def test_night_armored_surface_softens_and_combat_depends_on_hp(self) -> None:
        armored_hp16 = _make_state(
            items={"stone_sword": 1, "cooked_beef": 6, "iron_boots": 1, "shield": 1},
            health=16.0,
            is_day=False,
        )
        for surf in ("prep_wood", "breed_an_animal", "plant_seed"):
            self.assertAlmostEqual(
                survival_readiness_multiplier(GOAL_MAP[surf], GOAL_MAP[surf], armored_hp16, (), is_day=False),
                0.5,
            )
        for combat in ("kill_a_mob", "deflect_arrow"):
            self.assertAlmostEqual(
                survival_readiness_multiplier(GOAL_MAP[combat], GOAL_MAP[combat], armored_hp16, (), is_day=False),
                1.0,
            )

        armored_hp14 = _make_state(
            items={"stone_sword": 1, "cooked_beef": 6, "iron_boots": 1, "shield": 1},
            health=14.0,
            is_day=False,
        )
        for combat in ("kill_a_mob", "deflect_arrow"):
            self.assertAlmostEqual(
                survival_readiness_multiplier(GOAL_MAP[combat], GOAL_MAP[combat], armored_hp14, (), is_day=False),
                0.15,
            )

    def test_daytime_discounts_sleep_and_keeps_surface_normal(self) -> None:
        day_st = _make_state(
            items={"stone_sword": 1, "cooked_beef": 6, "white_bed": 1},
            is_day=True,
        )
        self.assertAlmostEqual(
            survival_readiness_multiplier(GOAL_MAP["sleep_in_bed"], GOAL_MAP["sleep_in_bed"], day_st, (), is_day=True),
            0.1,
        )
        for surf in ("prep_wood", "breed_an_animal", "plant_seed"):
            self.assertAlmostEqual(
                survival_readiness_multiplier(GOAL_MAP[surf], GOAL_MAP[surf], day_st, (), is_day=True),
                1.0,
            )


class TestSurvivalPolicyLayer(unittest.TestCase):
    def setUp(self) -> None:
        _reset_globals()
        self.planner = RulePlanner(min_verified="offline")

    def tearDown(self) -> None:
        _reset_globals()

    def test_food_reserve_gate_conditions(self) -> None:
        # 1) Daytime + food_restore < 10 + stone_sword=1 -> gather_food(target_restore=16, cook=True)
        st_sword = _make_state(items={"stone_sword": 1}, is_day=True)
        dec = self.planner.choose_survival_or_safe_mode(st_sword)
        self.assertIsNotNone(dec)
        self.assertIsNotNone(dec.skill)
        self.assertEqual(dec.skill.name, "gather_food")
        self.assertEqual(dec.skill.args.get("target_restore"), 16)
        self.assertEqual(dec.skill.args.get("cook"), True)

        # 2) Daytime + food_restore < 10 + empty bag + food=16 (and also hp=16) -> gather_food
        for st_low in (
            _make_state(items={}, food=16, health=20.0, is_day=True),
            _make_state(items={}, food=20, health=16.0, is_day=True),
        ):
            dec_low = self.planner.choose_survival_or_safe_mode(st_low)
            self.assertIsNotNone(dec_low)
            self.assertIsNotNone(dec_low.skill)
            self.assertEqual(dec_low.skill.name, "gather_food")

        # 3) Empty minute-0 bag (hp=20, food=20, no sword) -> does NOT trigger gather_food
        st_m0 = _make_state(items={}, food=20, health=20.0, is_day=True)
        self.assertIsNone(self.planner.choose_survival_or_safe_mode(st_m0))

        # 4) food_restore >= 10 (cooked_beef=2 -> 16) -> does NOT trigger
        st_fed = _make_state(items={"stone_sword": 1, "cooked_beef": 2}, is_day=True)
        self.assertIsNone(self.planner.choose_survival_or_safe_mode(st_fed))

        # 5) Night without raw meat + furnace -> does NOT trigger gather_food (unarmored bot now digs night_shelter)
        st_night = _make_state(items={"stone_sword": 1}, is_day=False)
        dec_night = self.planner.choose_survival_or_safe_mode(st_night)
        if dec_night is not None and dec_night.skill is not None:
            self.assertNotEqual(dec_night.skill.name, "gather_food")
            self.assertEqual(dec_night.skill.name, "night_shelter")

        # 6) Raw meat cooking gate at night: beef=3, coal=2, cooked=0, furnace in memo -> triggers gather_food
        st_cook = _make_state(items={"beef": 3, "coal": 2}, is_day=False)
        memo_for(st_cook).record_station("furnace", [1, 64, 1], dimension="overworld")
        dec_cook = self.planner.choose_survival_or_safe_mode(st_cook)
        self.assertIsNotNone(dec_cook)
        self.assertIsNotNone(dec_cook.skill)
        self.assertEqual(dec_cook.skill.name, "gather_food")

        # 7) Blocked when gather_food is env-blocked
        p_blk = RulePlanner(min_verified="offline")
        gf_inst = make("gather_food", {"target_restore": 16, "cook": True})
        p_blk.record_environment_block(
            gf_inst,
            EnvBlocked("无动物", missing_predicate="food_source", searched_volume="nearby", wake_on="chunk_moved"),
            st_sword,
        )
        self.assertIsNone(p_blk.choose_survival_or_safe_mode(st_sword))

        # 8) Blocked when hostile < 12 (dist=10.5 avoids unarmored flee <9.0 while violating safe_12)
        st_hostile = _make_state(
            items={"stone_sword": 1},
            is_day=True,
            nearest_hostile={"name": "zombie", "dist": 10.5},
        )
        self.assertIsNone(self.planner.choose_survival_or_safe_mode(st_hostile))

    def test_night_bed_sleep_fast_path_and_guards(self) -> None:
        # Triggers with white_bed, 3 white_wool + planks, or memo bed when night & hdist > 12
        for variant in (
            {"items": {"white_bed": 1, "cooked_beef": 4}},
            {"items": {"white_wool": 3, "oak_planks": 4, "cooked_beef": 4}},
            {"items": {"cooked_beef": 4}, "memo_bed": True},
        ):
            _reset_globals()
            st = _make_state(
                items=variant["items"],
                is_day=False,
                nearest_hostile={"name": "zombie", "dist": 15.0},
            )
            if variant.get("memo_bed"):
                memo_for(st).record_station("bed", [3, 64, 3], dimension="overworld")
            dec = RulePlanner(min_verified="offline").choose_survival_or_safe_mode(st)
            self.assertIsNotNone(dec)
            self.assertIsNotNone(dec.skill)
            self.assertEqual(dec.skill.name, "sleep_in_bed")

        # Does NOT trigger sleep_in_bed when hostile <= 12 (dist=11.0 falls back to relaxed night_shelter)
        st_near_mob = _make_state(
            items={"white_bed": 1, "cooked_beef": 4},
            is_day=False,
            nearest_hostile={"name": "zombie", "dist": 11.0},
        )
        dec_near = RulePlanner(min_verified="offline").choose_survival_or_safe_mode(st_near_mob)
        if dec_near is not None and dec_near.skill is not None:
            self.assertNotEqual(dec_near.skill.name, "sleep_in_bed")
            self.assertEqual(dec_near.skill.name, "night_shelter")

        # Does NOT trigger in daytime
        st_day = _make_state(items={"white_bed": 1, "cooked_beef": 4}, is_day=True)
        self.assertIsNone(RulePlanner(min_verified="offline").choose_survival_or_safe_mode(st_day))

        # Does NOT trigger when target_goal is set
        st_night_safe = _make_state(items={"white_bed": 1, "cooked_beef": 4}, is_day=False)
        targeted_planner = RulePlanner(min_verified="offline", target_goal="mine_stone")
        self.assertIsNone(targeted_planner.choose_survival_or_safe_mode(st_night_safe))

    def test_camp_withdraw_on_respawn_near_camp(self) -> None:
        st = _make_state(items={}, pos=(10.5, 64.0, 10.5), is_day=True)
        memo_for(st).set_camp(
            [0, 64, 0],
            chest=[1, 64, 0],
            stored_items={"iron_pickaxe": 1, "iron_sword": 1, "cooked_beef": 6},
        )
        dec = self.planner.choose_survival_or_safe_mode(st)
        self.assertIsNotNone(dec)
        self.assertIsNotNone(dec.skill)
        self.assertEqual(dec.skill.name, "manage_camp_chest")
        self.assertEqual(dec.skill.args.get("mode"), "withdraw")

    def test_controlled_corpse_recovery_and_blocks(self) -> None:
        def _setup_death(st: State) -> None:
            memo_for(st).record_death(
                [20, 64, 0],
                cause="zombie",
                lost_items={"iron_pickaxe": 1, "diamond": 2},
                dimension="overworld",
            )

        # Eligible: hp >= 14, no hostile < 10, not in safe_mode
        st_ok = _make_state(
            items={"stone_sword": 1, "cooked_beef": 4},
            health=14.0,
            pos=(0.5, 64.0, 0.5),
            is_day=True,
        )
        _setup_death(st_ok)
        dec = self.planner.choose_survival_or_safe_mode(st_ok)
        self.assertIsNotNone(dec)
        self.assertIsNotNone(dec.skill)
        self.assertEqual(dec.skill.name, "manage_camp_chest")
        self.assertEqual(dec.skill.args.get("mode"), "recover_corpse")

        # Blocked when hp < 14
        st_low_hp = _make_state(
            items={"stone_sword": 1, "cooked_beef": 4},
            health=13.0,
            pos=(0.5, 64.0, 0.5),
            is_day=True,
        )
        _setup_death(st_low_hp)
        dec_low = self.planner.choose_survival_or_safe_mode(st_low_hp)
        self.assertTrue(dec_low is None or dec_low.skill is None or dec_low.skill.name != "manage_camp_chest")

        # Blocked when in_safe_mode
        st_safe = _make_state(
            items={"stone_sword": 1, "cooked_beef": 4},
            health=20.0,
            pos=(0.5, 64.0, 0.5),
            is_day=True,
            in_safe_mode=True,
        )
        _setup_death(st_safe)
        dec_safe = self.planner.choose_survival_or_safe_mode(st_safe)
        self.assertEqual(dec_safe.source, "idle")

        # Blocked when hostile < 10 (dist=9.5 avoids <9.0 flee while failing safe_10)
        st_mob = _make_state(
            items={"stone_sword": 1, "cooked_beef": 4},
            health=20.0,
            pos=(0.5, 64.0, 0.5),
            is_day=True,
            nearest_hostile={"name": "zombie", "dist": 9.5},
        )
        _setup_death(st_mob)
        self.assertIsNone(self.planner.choose_survival_or_safe_mode(st_mob))

    def test_camp_surplus_deposit(self) -> None:
        for surplus in ({"iron_ingot": 10}, {"diamond": 4}):
            _reset_globals()
            st = _make_state(
                items={"stone_sword": 1, "cooked_beef": 4, **surplus},
                pos=(10.5, 64.0, 10.5),
                is_day=True,
            )
            memo_for(st).set_camp([0, 64, 0], chest=[1, 64, 0], stored_items={})
            dec = RulePlanner(min_verified="offline").choose_survival_or_safe_mode(st)
            self.assertIsNotNone(dec)
            self.assertIsNotNone(dec.skill)
            self.assertEqual(dec.skill.name, "manage_camp_chest")
            self.assertEqual(dec.skill.args.get("mode"), "deposit")

    def test_post_iron_shield_crafting_fast_path(self) -> None:
        st = _make_state(
            items={"iron_pickaxe": 1, "iron_ingot": 1, "oak_planks": 6, "cooked_beef": 4},
            is_day=True,
        )
        dec = self.planner.choose_survival_or_safe_mode(st)
        self.assertIsNotNone(dec)
        self.assertIsNotNone(dec.skill)
        self.assertEqual(dec.skill.name, "craft_iron_gear")
        self.assertEqual(dec.skill.args.get("item"), "shield")

    def test_dynamic_flee_distance_armor_and_hp(self) -> None:
        zombie_8m = {"name": "zombie", "dist": 8.0}

        # Unarmored at dist=8.0 -> effective_hostile_flee_dist=9.0 -> flee
        st_unarmored = _make_state(items={"cooked_beef": 4}, health=20.0, nearest_hostile=zombie_8m)
        self.assertAlmostEqual(effective_hostile_flee_dist(st_unarmored, "zombie"), 9.0)
        dec_unarmored = self.planner.choose_survival_or_safe_mode(st_unarmored)
        self.assertIsNotNone(dec_unarmored)
        self.assertIsNotNone(dec_unarmored.skill)
        self.assertEqual(dec_unarmored.skill.name, "emergency_escape")
        self.assertEqual(dec_unarmored.skill.args.get("intent"), "flee")

        # Armored full HP at dist=8.0 -> effective_hostile_flee_dist=6.0 -> does NOT flee
        st_armored_full = _make_state(
            items={"cooked_beef": 4},
            health=20.0,
            armor_points=6,
            nearest_hostile=zombie_8m,
            is_day=True,
        )
        self.assertAlmostEqual(effective_hostile_flee_dist(st_armored_full, "zombie"), 6.0)
        self.assertIsNone(self.planner.choose_survival_or_safe_mode(st_armored_full))

        # Armored low HP (hp=10 < hp_risk=14) at dist=8.0 -> effective_hostile_flee_dist=9.0 -> flee
        st_armored_low = _make_state(
            items={"cooked_beef": 4},
            health=10.0,
            armor_points=6,
            nearest_hostile=zombie_8m,
            is_day=True,
        )
        self.assertAlmostEqual(effective_hostile_flee_dist(st_armored_low, "zombie"), 9.0)
        dec_low = self.planner.choose_survival_or_safe_mode(st_armored_low)
        self.assertIsNotNone(dec_low)
        self.assertIsNotNone(dec_low.skill)
        self.assertEqual(dec_low.skill.name, "emergency_escape")
        self.assertEqual(dec_low.skill.args.get("intent"), "flee")


class TestEngineDeathPersistence(unittest.TestCase):
    def setUp(self) -> None:
        _reset_globals()

    def tearDown(self) -> None:
        _reset_globals()

    def test_recover_session_or_respawn_persists_last_death_and_lost_items(self) -> None:
        client = _MockRespawnClient(
            status_payload={
                "ready": True,
                "alive": True,
                "dimension": "overworld",
                "pos": [0.5, 64.0, 0.5],
                "health": 20,
                "food": 20,
                "last_death": {
                    "pos": [18.2, 62.0, -9.7],
                    "cause": "Bot was slain by Zombie",
                },
            },
            inventory_items=[],
        )
        state = State()
        executor = Executor(client, state)
        planner = CampaignPlanner(mode="campaign", min_verified="offline")
        cfg = LoopConfig(respawn_poll_s=0.001, enable_monitor=False)
        loop = MainLoop(client, state, executor, planner, config=cfg, log=lambda *_: None)

        ok = loop._recover_session_or_respawn(pre_death_inv={"iron_pickaxe": 1, "diamond": 2})
        self.assertTrue(ok)

        memo = memo_for(state)
        death = memo.get_last_death(dimension="overworld")
        self.assertIsNotNone(death)
        self.assertEqual(death["pos"], [18, 62, -10])
        self.assertEqual(death["cause"], "bot was slain by zombie")
        self.assertEqual(death["lost_items"], {"iron_pickaxe": 1, "diamond": 2})
        self.assertEqual(
            memo.should_attempt_corpse_recovery([0.5, 64.0, 0.5], dimension="overworld"),
            (True, "eligible"),
        )


class TestContextAndStrategyIntegration(unittest.TestCase):
    def setUp(self) -> None:
        _reset_globals()

    def tearDown(self) -> None:
        _reset_globals()

    def test_context_armor_food_and_camp_summary_without_coord_leaks(self) -> None:
        st = _make_state(
            items={"cooked_beef": 2, "shield": 1},
            armor_points=2,
            is_day=True,
        )
        inv_summary = summarize_inventory_and_status(st)
        self.assertIn("护甲=已穿戴+盾", inv_summary)
        self.assertIn("食物量=16", inv_summary)

        memo = memo_for(st)
        memo.set_camp(
            [123, 64, -456],
            chest=[124, 64, -456],
            stored_items={"iron_ingot": 5, "cooked_beef": 4},
        )
        lm_line = format_world_landmarks(memo.summary_for_context(dimension="overworld"))
        self.assertIn("营地=已建(存箱=9件)", lm_line)

        rendered = build_decision_context(st).render()
        self.assertIn("营地=已建(存箱=9件)", rendered)
        self.assertLessEqual(len(rendered), 2000)
        for leaked in ("123", "124", "-456"):
            self.assertNotIn(leaked, rendered)
        self.assertIsNone(_COORD_TRIPLET_RE.search(rendered))

        # Empty camp chest renders 营地=已建
        memo.update_camp_stored({}, dimension="overworld")
        empty_lm = format_world_landmarks(memo.summary_for_context(dimension="overworld"))
        self.assertIn("营地=已建", empty_lm)
        self.assertNotIn("存箱=", empty_lm)

    def test_strategy_day_night_changed_trigger_switches_to_sleep_or_underground(self) -> None:
        # 1) Switching is_day True -> False with white_bed triggers day_night_changed -> sleep_in_bed
        clock = _FakeClock()
        planner_bed = CampaignPlanner(
            mode="campaign",
            strategy="rule",
            min_verified="offline",
            include_stages=("graduated", "candidate", "draft"),
            clock=clock,
        )
        st_day_bed = _make_state(
            items={
                "stone_pickaxe": 1,
                "stone_sword": 1,
                "cooked_beef": 6,
                "oak_planks": 4,
                "white_bed": 1,
            },
            is_day=True,
        )
        dec1 = planner_bed.select(st_day_bed, [])
        self.assertIsNotNone(dec1.objective)
        self.assertNotEqual(dec1.objective.goal_id, "sleep_in_bed")

        clock.advance(1.0)
        st_day_bed.set_status({**st_day_bed.snapshot(), "is_day": False})
        dec2 = planner_bed.select(st_day_bed, [])
        self.assertIsNotNone(dec2.objective)
        self.assertEqual(dec2.objective.goal_id, "sleep_in_bed")
        self.assertEqual(dec2.objective_trigger, "day_night_changed")
        self.assertIsNotNone(dec2.skill)
        self.assertEqual(dec2.skill.name, "sleep_in_bed")

        # 2) Switching is_day True -> False without bed (unarmored, surface + underground goals in ledger)
        _reset_globals()
        clock2 = _FakeClock()
        surface_ug_ledger = (
            GOAL_MAP["prep_wood"],
            GOAL_MAP["plant_seed"],
            GOAL_MAP["breed_an_animal"],
            GOAL_MAP["smelt_iron"],
        )
        planner_ug = CampaignPlanner(
            mode="campaign",
            strategy="rule",
            min_verified="offline",
            include_stages=("graduated", "candidate", "draft"),
            ledger=surface_ug_ledger,
            clock=clock2,
        )
        planner_ug.rules.completed_advancements.update(
            {"minecraft:story/mine_stone", "minecraft:story/upgrade_tools"}
        )
        st_ug = _make_state(
            items={
                "stone_pickaxe": 1,
                "stone_sword": 1,
                "cooked_beef": 6,
                "oak_planks": 4,
                "furnace": 1,
                "coal": 2,
                "wheat_seeds": 2,
                "wheat": 2,
            },
            is_day=True,
        )
        dec_day = planner_ug.select(st_ug, [])
        self.assertIsNotNone(dec_day.objective)
        self.assertIn(dec_day.objective.goal_id, ("plant_seed", "breed_an_animal"))

        clock2.advance(1.0)
        st_ug.set_status({**st_ug.snapshot(), "is_day": False})
        dec_night = planner_ug.select(st_ug, [])
        self.assertIsNotNone(dec_night.objective)
        self.assertEqual(dec_night.objective_trigger, "day_night_changed")
        self.assertIn(dec_night.objective.goal_id, ("smelt_iron", "mine_stone"))

    def test_utility_survival_skills_do_not_increment_consecutive_goal_fails(self) -> None:
        clock = _FakeClock()
        planner = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline", clock=clock)
        st = _make_state(items={"stone_pickaxe": 1, "stone_sword": 1, "cooked_beef": 6}, is_day=True)
        dec = planner.select(st, [])
        self.assertIsNotNone(dec.objective)
        gid = dec.objective.goal_id

        # Set a baseline consecutive failure count and ensure gather_food / manage_camp_chest leave it untouched
        planner._strategy._consecutive_goal_fails[gid] = 2
        for skill_name in ("gather_food", "manage_camp_chest"):
            planner._strategy.on_skill_outcome(skill_name, ok=False, elapsed_s=5.0)
            self.assertEqual(planner._strategy._consecutive_goal_fails.get(gid), 2)
            planner._strategy.on_skill_outcome(skill_name, ok=True, elapsed_s=5.0)
            self.assertEqual(planner._strategy._consecutive_goal_fails.get(gid), 2)


if __name__ == "__main__":
    unittest.main()
