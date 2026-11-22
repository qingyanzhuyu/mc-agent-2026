"""Stage 3 验收修复回归测试：Code-B1 台账死锁、M9 指纹清洁与 subgoals 修复。"""
from __future__ import annotations

import unittest

from mcagent.loop import subgoals
from mcagent.loop.goals import (
    GOAL_MAP,
    GOALS,
    GOAL_STAGES,
    GoalSpec,
    is_goal_achieved,
    is_prereq_satisfied,
    rank_actionable_goals,
    resolve_active_goals,
    validate_ledger,
)
from mcagent.loop.starter_skills import reset_world_memo
from mcagent.runtime import State
from mcagent.skills.world_memo import STRUCTURE_FINGERPRINTS, memo_for, structure_tags_for


def _ready_state(
    items: list[dict] | None = None,
    *,
    dimension: str = "overworld",
    health: float = 20.0,
    food: float = 20.0,
    armor: dict | None = None,
) -> State:
    st = State()
    st.set_status(
        {
            "ready": True,
            "alive": True,
            "health": health,
            "food": food,
            "dimension": dimension,
            "pos": [0.5, 64.0, 0.5],
        }
    )
    st.set_inventory(
        {
            "items": list(items or []),
            "held": {},
            "offhand": {},
            "armor": dict(armor or {}),
        }
    )
    return st


class T4PrepDonePredicatesAndUnlockTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_world_memo()
        self.addCleanup(reset_world_memo)

    def test_five_t4_prep_goals_false_then_true_via_done_predicates(self) -> None:
        empty = _ready_state()
        for gid in (
            "prep_high_altitude_portal",
            "prep_piercing_iv_crossbow",
            "prep_five_unique_mobs_pack",
            "prep_looting_sword",
            "prep_micro_village_bunker",
        ):
            goal = GOAL_MAP[gid]
            self.assertFalse(is_goal_achieved(goal, empty), gid)
            self.assertFalse(is_prereq_satisfied(goal, empty), gid)

        # 1. prep_high_altitude_portal -> landmark_reachable:high_altitude_portal
        st_portal = _ready_state()
        memo_for(st_portal).record_landmark("high_altitude_portal", (12, 319, 8), dimension="overworld")
        self.assertTrue(is_goal_achieved(GOAL_MAP["prep_high_altitude_portal"], st_portal))
        self.assertTrue(is_prereq_satisfied(GOAL_MAP["prep_high_altitude_portal"], st_portal))

        # 2. prep_piercing_iv_crossbow -> enchantment_ready:crossbow:piercing>=4
        st_xbow_low = _ready_state(
            [{"name": "minecraft:crossbow", "count": 1, "enchantments": {"minecraft:piercing": 3}}]
        )
        self.assertFalse(is_prereq_satisfied(GOAL_MAP["prep_piercing_iv_crossbow"], st_xbow_low))
        st_xbow_ok = _ready_state(
            [{"name": "minecraft:crossbow", "count": 1, "enchantments": {"minecraft:piercing": 4}}]
        )
        self.assertTrue(is_goal_achieved(GOAL_MAP["prep_piercing_iv_crossbow"], st_xbow_ok))
        self.assertTrue(is_prereq_satisfied(GOAL_MAP["prep_piercing_iv_crossbow"], st_xbow_ok))

        # 3. prep_five_unique_mobs_pack -> mob_collection_ready>=5
        st_mobs_four = _ready_state(
            [
                {"name": "cod_bucket", "count": 1},
                {"name": "salmon_bucket", "count": 1},
                {"name": "tropical_fish_bucket", "count": 1},
                {"name": "pufferfish_bucket", "count": 1},
            ]
        )
        self.assertFalse(is_prereq_satisfied(GOAL_MAP["prep_five_unique_mobs_pack"], st_mobs_four))
        st_mobs_five = _ready_state(
            [
                {"name": "cod_bucket", "count": 1},
                {"name": "salmon_bucket", "count": 1},
                {"name": "tropical_fish_bucket", "count": 1},
                {"name": "pufferfish_bucket", "count": 1},
                {"name": "tadpole_bucket", "count": 1},
            ]
        )
        self.assertTrue(is_goal_achieved(GOAL_MAP["prep_five_unique_mobs_pack"], st_mobs_five))
        self.assertTrue(is_prereq_satisfied(GOAL_MAP["prep_five_unique_mobs_pack"], st_mobs_five))

        # 4. prep_looting_sword -> enchantment_ready:sword:looting>=3
        st_sword_low = _ready_state(
            [{"name": "diamond_sword", "count": 1, "enchantments": {"looting": 2}}]
        )
        self.assertFalse(is_prereq_satisfied(GOAL_MAP["prep_looting_sword"], st_sword_low))
        st_sword_ok = _ready_state(
            [{"name": "minecraft:diamond_sword", "count": 1, "enchantments": {"minecraft:looting": 3}}]
        )
        self.assertTrue(is_goal_achieved(GOAL_MAP["prep_looting_sword"], st_sword_ok))
        self.assertTrue(is_prereq_satisfied(GOAL_MAP["prep_looting_sword"], st_sword_ok))

        # 5. prep_micro_village_bunker -> landmark_reachable:micro_village_bunker
        st_bunker = _ready_state()
        custom_memo = memo_for(State())
        custom_memo.record_landmark("micro_village_bunker", (30, 64, -10), dimension="overworld")
        self.assertFalse(is_prereq_satisfied(GOAL_MAP["prep_micro_village_bunker"], st_bunker))
        self.assertTrue(
            is_goal_achieved(GOAL_MAP["prep_micro_village_bunker"], st_bunker, memo=custom_memo)
        )
        self.assertTrue(
            is_prereq_satisfied(GOAL_MAP["prep_micro_village_bunker"], st_bunker, memo=custom_memo)
        )

    def test_rank_actionable_goals_unlocks_all_six_downstream_goals(self) -> None:
        upstream_advs = {
            "minecraft:adventure/trade",
            "minecraft:story/enter_the_nether",
            "minecraft:story/enchant_item",
            "minecraft:adventure/ol_betsy",
            "minecraft:husbandry/tactical_fishing",
            "minecraft:husbandry/tadpole_in_a_bucket",
            "minecraft:nether/find_fortress",
            "minecraft:nether/get_wither_skull",
        }
        all_goals = resolve_active_goals(GOALS, include_stages=GOAL_STAGES)

        # 未满足 5 个 T4_PREP 时，下游高分目标全部卡在各自 T4_PREP 前置步，无法直接推进根目标
        blocked_state = _ready_state([{"name": "ominous_bottle", "count": 1}])
        for target_id, expected_prep in (
            ("trade_at_world_height", "prep_high_altitude_portal"),
            ("arbalistic", "prep_piercing_iv_crossbow"),
            ("summon_wither", "prep_looting_sword"),
            ("create_beacon", "prep_looting_sword"),
            ("hero_of_the_village", "prep_micro_village_bunker"),
            ("totem_of_undying", "prep_micro_village_bunker"),
        ):
            steps = rank_actionable_goals(
                blocked_state,
                all_goals,
                completed_advs=upstream_advs,
                target_goal=target_id,
            )
            self.assertTrue(steps, target_id)
            self.assertNotEqual(steps[0].step_goal.id, target_id, f"{target_id} 应先卡在前置")
            self.assertEqual(steps[0].step_goal.id, expected_prep, target_id)

        # 满足 5 个 T4_PREP（以及各自链路上游物品/前置成就）后，6 个总计 440 分目标全部直接可执行
        unlocked_state = _ready_state(
            [
                {"name": "crossbow", "count": 1, "enchantments": {"piercing": 4}},
                {"name": "diamond_sword", "count": 1, "enchantments": {"looting": 3}},
                {"name": "cod_bucket", "count": 1},
                {"name": "salmon_bucket", "count": 1},
                {"name": "tropical_fish_bucket", "count": 1},
                {"name": "pufferfish_bucket", "count": 1},
                {"name": "tadpole_bucket", "count": 1},
                {"name": "wither_skeleton_skull", "count": 3},
                {"name": "nether_star", "count": 1},
                {"name": "ominous_bottle", "count": 1},
            ]
        )
        memo = memo_for(unlocked_state)
        memo.record_landmark("high_altitude_portal", (0, 319, 0), dimension="overworld")
        memo.record_landmark("micro_village_bunker", (24, 64, 24), dimension="overworld")

        beacon_advs = upstream_advs | {"minecraft:nether/summon_wither"}
        totem_advs = upstream_advs | {"minecraft:adventure/hero_of_the_village"}

        expected_targets = {
            "trade_at_world_height": (60, upstream_advs),
            "arbalistic": (100, upstream_advs),
            "summon_wither": (100, upstream_advs),
            "create_beacon": (60, beacon_advs),
            "hero_of_the_village": (60, upstream_advs),
            "totem_of_undying": (60, totem_advs),
        }
        self.assertEqual(sum(score for score, _ in expected_targets.values()), 440)

        for target_id, (expected_score, advs) in expected_targets.items():
            steps = rank_actionable_goals(
                unlocked_state,
                all_goals,
                completed_advs=advs,
                target_goal=target_id,
            )
            self.assertTrue(steps, target_id)
            self.assertEqual(steps[0].step_goal.id, target_id)
            self.assertEqual(steps[0].root_goal.id, target_id)
            self.assertEqual(steps[0].path_score, expected_score)

            focused_goals = resolve_active_goals(GOALS, include_stages=GOAL_STAGES, target_goal=target_id)
            focused_steps = rank_actionable_goals(
                unlocked_state,
                focused_goals,
                completed_advs=advs,
                target_goal=target_id,
            )
            self.assertTrue(focused_steps, target_id)
            self.assertEqual(focused_steps[0].step_goal.id, target_id)
            self.assertEqual(focused_steps[0].root_goal.id, target_id)
            self.assertEqual(len(focused_steps[0].chain), 1, target_id)


class ValidateLedgerPrereqContractTests(unittest.TestCase):
    def test_default_ledger_passes_validation(self) -> None:
        self.assertEqual(validate_ledger(GOALS), [])

    def test_validate_ledger_rejects_prereq_without_done_criteria(self) -> None:
        deadlocked = (
            GoalSpec(
                id="prep_deadlock",
                title="空判据前置",
                tier="T4_PREP",
                score=0,
                skill="gather_wood",
                stage="draft",
            ),
            GoalSpec(
                id="downstream_goal",
                title="下游目标",
                tier="T4",
                score=60,
                skill="mine_stone",
                prereq_goals=("prep_deadlock",),
                done_items=("cobblestone>=1",),
                stage="draft",
            ),
        )
        errs = validate_ledger(deadlocked)
        self.assertTrue(any("prep_deadlock" in e and "done_predicates" in e for e in errs), errs)

    def test_validate_ledger_accepts_prereq_with_done_predicates(self) -> None:
        valid = (
            GoalSpec(
                id="prep_ok",
                title="带谓词前置",
                tier="T4_PREP",
                score=0,
                skill="gather_wood",
                done_predicates=("mob_collection_ready>=5",),
                stage="draft",
            ),
            GoalSpec(
                id="downstream_goal",
                title="下游目标",
                tier="T4",
                score=60,
                skill="mine_stone",
                prereq_goals=("prep_ok",),
                done_items=("cobblestone>=1",),
                stage="draft",
            ),
        )
        self.assertEqual(validate_ledger(valid), [])


class SubgoalsAndWorldMemoFixTests(unittest.TestCase):
    def test_mace_recognized_as_weapon_in_safe_encounter_and_ominous_ready(self) -> None:
        self.assertIn("mace", subgoals._WEAPON_EXACT)
        st = _ready_state(
            [
                {"name": "minecraft:mace", "count": 1},
                {"name": "ominous_bottle", "count": 1},
                {"name": "bread", "count": 4},
            ],
            health=18.0,
            food=18.0,
            armor={
                "chest": {"name": "iron_chestplate", "count": 1},
                "legs": {"name": "iron_leggings", "count": 1},
            },
        )
        self.assertTrue(subgoals.safe_encounter(st))
        self.assertTrue(subgoals.ominous_ready(st, "trial"))
        self.assertTrue(subgoals.ominous_ready(st, "raid"))

    def test_extract_missing_criteria_normalizes_minecraft_prefix(self) -> None:
        self.assertEqual(
            subgoals._extract_missing_criteria(
                {"remaining": ["minecraft:temperate", "cold"]},
                ("temperate", "warm", "cold"),
            ),
            ("cold", "temperate"),
        )
        self.assertEqual(
            subgoals._extract_missing_criteria(
                {"criteria": {"minecraft:temperate": True, "minecraft:warm": False, "cold": False}},
                ("temperate", "warm", "cold"),
            ),
            ("cold", "warm"),
        )
        self.assertEqual(
            subgoals._extract_missing_criteria(
                {"criteria": {"minecraft:temperate": True, "minecraft:warm": False}},
                None,
            ),
            ("warm",),
        )
        self.assertEqual(
            subgoals._extract_missing_criteria(
                {"completed_criteria": ["minecraft:temperate", "minecraft:cold"]},
                ("temperate", "warm", "cold"),
            ),
            ("warm",),
        )

    def test_world_memo_m9_fingerprints_cleaned(self) -> None:
        ominous_fp = STRUCTURE_FINGERPRINTS["trial_vault_ominous"]
        self.assertNotIn("ominous_trial_spawner", ominous_fp)
        self.assertNotIn("heavy_core", ominous_fp)
        self.assertIn("vault", ominous_fp)
        self.assertIn("trial_spawner", ominous_fp)

        meadow_fp = STRUCTURE_FINGERPRINTS["meadow"]
        for bad in ("cornflower", "azure_bluet", "oxeye_daisy"):
            self.assertNotIn(bad, meadow_fp)
        self.assertEqual(set(meadow_fp), {"allium", "emerald_ore"})
        self.assertNotIn("meadow", structure_tags_for(["cornflower", "azure_bluet", "oxeye_daisy"]))

        swamp_fp = STRUCTURE_FINGERPRINTS["swamp"]
        self.assertNotIn("slime_block", swamp_fp)
        self.assertEqual(set(swamp_fp), {"lily_pad", "blue_orchid"})


if __name__ == "__main__":
    unittest.main()
