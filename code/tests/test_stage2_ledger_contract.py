"""Stage 2 (Wave 1 + Wave 2 + Wave 3, T2/T3) Goal Ledger contract tests."""
from __future__ import annotations

import unittest
from pathlib import Path

from mcagent.loop.draft_scores import ADVANCEMENT_SCORES_V2
from mcagent.loop.goals import GOAL_MAP, GOALS, resolve_active_goals, validate_ledger
from mcagent.skills import REGISTRY, bind_args

BATCH_1_IDS: tuple[str, ...] = (
    "follow_ender_eye",
    "find_fortress",
    "find_bastion",
    "minecraft_trials_edition",
    "explore_nether",
)
BATCH_2A_IDS: tuple[str, ...] = (
    "trade",
    "trim_with_any_armor_pattern",
    "obtain_netherite_hoe",
    "brew_potion",
    "cure_zombie_villager",
)
BATCH_2B_IDS: tuple[str, ...] = (
    "obtain_ancient_debris",
    "obtain_blaze_rod",
    "loot_bastion",
    "charge_respawn_anchor",
    "obtain_crying_obsidian",
    "distract_piglin",
    "get_wither_skull",
)
BATCH_2C_IDS: tuple[str, ...] = (
    "summon_iron_golem",
    "sniper_duel",
    "make_sign_glow",
    "axolotl_in_a_bucket",
    "tactical_fishing",
    "tame_an_animal",
    "safely_harvest_honey",
    "wax_on",
    "wax_off",
    "avoid_vibration",
    "brush_armadillo",
    "walk_on_powder_snow_with_leather_boots",
    "place_dried_ghast_in_water",
    "whos_the_pillager_now",
)
BATCH_2D_IDS: tuple[str, ...] = (
    "enter_the_end",
    "enter_end_gateway",
    "find_end_city",
    "levitate",
)
BATCH_2E_IDS: tuple[str, ...] = (
    "under_lock_and_key",
    "who_needs_rockets",
    "lighten_up",
    "read_power_of_chiseled_bookshelf",
    "heart_transplanter",
)
BATCH_2F_IDS: tuple[str, ...] = (
    "kill_mob_near_sculk_catalyst",
    "kill_axolotl_target",
    "remove_wolf_armor",
    "repair_wolf_armor",
    "allay_deliver_item_to_player",
    "ride_a_boat_with_a_goat",
    "silk_touch_nest",
    "tadpole_in_a_bucket",
    "spyglass_at_ghast",
    "spyglass_at_parrot",
)
STAGE2_EXPECTED_IDS: tuple[str, ...] = (
    BATCH_1_IDS
    + BATCH_2A_IDS
    + BATCH_2B_IDS
    + BATCH_2C_IDS
    + BATCH_2D_IDS
    + BATCH_2E_IDS
    + BATCH_2F_IDS
    + ("salvage_sherd", "obtain_sniffer_egg", "plant_any_sniffer_seed")
)


STAGE12_TIERS: frozenset[str] = frozenset({"T0_PREP", "T1", "T2", "T3"})


class Stage2LedgerContractTests(unittest.TestCase):
    def test_stage2_counts_and_batch_point_totals(self):
        self.assertEqual(len(GOALS), len(GOAL_MAP))
        self.assertGreaterEqual(len(GOALS), 76)

        stage12 = [g for g in GOALS if g.tier in STAGE12_TIERS]
        t0_t1 = [g for g in stage12 if g.tier in {"T0_PREP", "T1"}]
        t0_t1_scored = [g for g in t0_t1 if g.score > 0]
        stage2 = [g for g in stage12 if g.tier in {"T2", "T3"}]
        self.assertEqual(len(stage12), 79)
        self.assertEqual(len(t0_t1), 26)
        self.assertEqual((len(t0_t1_scored), sum(g.score for g in t0_t1_scored)), (21, 290))
        self.assertEqual(len(stage2), 53)
        self.assertEqual(sum(g.score for g in stage2), 1510)
        self.assertEqual(sum(g.score for g in stage12), 1800)
        self.assertEqual(tuple(g.id for g in stage2), STAGE2_EXPECTED_IDS)

        expected_batches = (
            ("batch_1", BATCH_1_IDS, 5, 165),
            ("batch_2a", BATCH_2A_IDS, 5, 180),
            ("batch_2b", BATCH_2B_IDS, 7, 230),
            ("batch_2c", BATCH_2C_IDS, 14, 260),
            ("batch_2d", BATCH_2D_IDS, 4, 210),
            ("batch_2e", BATCH_2E_IDS, 5, 120),
            ("batch_2f", BATCH_2F_IDS, 10, 270),
        )
        for name, ids, count, pts in expected_batches:
            with self.subTest(batch=name):
                rows = [GOAL_MAP[gid] for gid in ids]
                self.assertEqual(len(rows), count)
                self.assertEqual(sum(row.score for row in rows), pts)

    def test_stage2_goals_preserve_draft_and_wip1_discipline(self):
        stage2 = [g for g in GOALS if g.tier in {"T2", "T3"}]
        self.assertEqual(len(stage2), 53)
        for goal in stage2:
            with self.subTest(goal=goal.id):
                self.assertEqual(goal.stage, "graduated")
                self.assertTrue(goal.stop_loss)
                self.assertGreaterEqual(goal.budget_s, 180.0)
                self.assertLessEqual(goal.budget_s, 540.0)
                self.assertIn(goal.max_fails_per_ep, (2, 3))

        self.assertEqual([g.id for g in GOALS if g.stage == "candidate"], [])
        self.assertEqual([g.id for g in GOALS if g.stage == "draft"], [])
        self.assertEqual(len([g for g in GOALS if g.stage in {"graduated", "live"}]), len(GOALS))

    def test_stage2_advancements_and_scores_match_draft_scores_v2(self):
        stage12_scored = [g for g in GOALS if g.tier in STAGE12_TIERS and g.score > 0]
        self.assertEqual(len(stage12_scored), 74)
        self.assertEqual(sum(g.score for g in stage12_scored), 1800)

        scored = [g for g in GOALS if g.score > 0]
        for goal in scored:
            with self.subTest(goal=goal.id):
                self.assertEqual(len(goal.done_advancements), 1)
                adv = goal.done_advancements[0]
                self.assertIn(adv, ADVANCEMENT_SCORES_V2)
                self.assertEqual(goal.score, ADVANCEMENT_SCORES_V2[adv])

        adv_ids = [g.done_advancements[0] for g in scored]
        self.assertEqual(len(set(adv_ids)), len(adv_ids))

    def test_stage2_dag_validation_and_transitive_prereq_closure(self):
        self.assertEqual(validate_ledger(GOALS), [])

        def closure(goal_id: str) -> set[str]:
            return {goal_id}.union(*(closure(pid) for pid in GOAL_MAP[goal_id].prereq_goals))

        for goal in GOALS:
            with self.subTest(goal=goal.id):
                for pid in goal.prereq_goals:
                    self.assertIn(pid, GOAL_MAP)
                resolved = resolve_active_goals(GOALS, target_goal=goal.id)
                expected = closure(goal.id)
                self.assertEqual({row.id for row in resolved}, expected)
                self.assertEqual(len(resolved), len(expected))

    def test_every_goal_skill_args_and_editable_files_bind_cleanly_in_registry(self):
        root = Path(__file__).resolve().parents[2]
        for goal in (row for row in GOALS if row.tier in STAGE12_TIERS):
            bindings = [(goal.skill, goal.skill_args)] + [
                (name, args) for _, name, args in goal.gather_fallbacks
            ]
            for name, args in bindings:
                with self.subTest(goal=goal.id, skill=name, args=args):
                    self.assertIn(name, REGISTRY)
                    self.assertEqual(bind_args(REGISTRY[name], dict(args))[1], [])
            for path in goal.editable_files:
                with self.subTest(goal=goal.id, editable_file=path):
                    self.assertTrue((root / path).is_file(), (goal.id, path))


if __name__ == "__main__":
    unittest.main()
