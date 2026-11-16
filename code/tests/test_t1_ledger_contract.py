"""T1 goal, material, and dispatch integration checks using offline fixtures."""
from __future__ import annotations

import unittest
from pathlib import Path

from mcagent.loop.draft_scores import ADVANCEMENT_SCORES_V2
from mcagent.loop.engine import LoopConfig, MainLoop
from mcagent.loop.goals import GOAL_MAP, GOALS, is_goal_achieved, is_prereq_satisfied, resolve_active_goals
from mcagent.loop.policy import CampaignPlanner
from mcagent.runtime import State
from mcagent.skills import REGISTRY, bind_args, make
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import drive
from test_enchant import EnchantWorld
from test_loop_soak import RichGridWorld
from test_nether import NetherWorld


def stock(state, **items):
    state.set_inventory({"items": [{"name": name, "count": count} for name, count in items.items()]})


def make_goal(goal_id):
    spec = GOAL_MAP[goal_id]
    return make(spec.skill, spec.args_dict)


def plan(skill, state):
    return type(skill).material_plan(skill.args, state)


class LedgerContractTests(unittest.TestCase):
    def test_every_main_and_fallback_skill_registered_and_bindable_including_drafts(self):
        root = Path(__file__).resolve().parents[2]
        for goal in GOALS:
            bindings = [(goal.skill, goal.skill_args)] + [(name, args) for _, name, args in goal.gather_fallbacks]
            for name, args in bindings:
                with self.subTest(goal=goal.id, skill=name, args=args):
                    self.assertIn(name, REGISTRY)
                    self.assertEqual(bind_args(REGISTRY[name], dict(args))[1], [])
            for path in goal.editable_files:
                self.assertTrue((root / path).is_file(), (goal.id, path))

    def test_every_t1_goal_resolves_exact_prerequisite_closure_regardless_of_stage(self):
        def closure(goal_id):
            return {goal_id}.union(*(closure(child) for child in GOAL_MAP[goal_id].prereq_goals))

        for goal in (row for row in GOALS if row.tier == "T1"):
            with self.subTest(goal=goal.id):
                resolved = resolve_active_goals(GOALS, target_goal=goal.id)
                self.assertEqual({row.id for row in resolved}, closure(goal.id))
                self.assertEqual(len(resolved), len(closure(goal.id)))

    def test_t1_scoring_and_candidate_unchanged(self):
        t1 = [goal for goal in GOALS if goal.tier == "T1" and goal.score > 0]
        self.assertEqual((len(t1), sum(goal.score for goal in t1)), (21, 290))
        self.assertEqual(len({goal.done_advancements[0] for goal in t1}), 21)
        for goal in t1:
            self.assertEqual(ADVANCEMENT_SCORES_V2[goal.done_advancements[0]], goal.score, goal.id)
        self.assertEqual([g.id for g in GOALS if g.stage == "candidate"], ["smelt_iron"])
        self.assertEqual(len([g for g in GOALS if g.stage in {"graduated", "live"}]), 6)
        self.assertEqual(len(GOALS), 26)

    def test_iron_score_is_exact_but_higher_tier_can_unlock_downstream(self):
        state = State()
        stock(state, diamond_pickaxe=1)
        goal = GOAL_MAP["iron_tools"]
        self.assertFalse(is_goal_achieved(goal, state))
        self.assertTrue(is_prereq_satisfied(goal, state))

    def test_iron_chain_converts_wood_instead_of_repeating_wood_gather(self):
        world = RichGridWorld(items={"iron_ingot": 3, "oak_log": 1})
        ex, state = world.session()
        planner = CampaignPlanner(target_goal="iron_tools")
        action = planner.select(state, []).skill
        self.assertEqual((action.name, action.args["item"]), ("craft_gear", "stick"))
        self.assertEqual([step["op"] for step in drive(action, ex, state)], ["craft", "craft"])
        self.assertEqual(state.count("stick"), 4)
        next_skill = planner.select(state, []).skill
        self.assertEqual((next_skill.name, next_skill.args["item"]), ("craft_iron_gear", "iron_pickaxe"))


class ReservationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.planner = CampaignPlanner()
        self.book = self.planner.reservations
        self.state = State()
        self.state.set_status({"ready": True, "alive": True, "dimension": "overworld"})

    def test_iron_tools_and_armor_compete_on_same_book_without_partial_claim(self):
        pick, boots = (make_goal(gid) for gid in ("iron_tools", "obtain_armor"))
        stock(self.state, iron_ingot=6, stick=2)
        self.assertIs(self.planner.rules.reservations,
                      self.planner._strategy.make_tactical_planner("iron_tools").reservations)
        self.assertTrue(self.book.reserve_plan(self.state, "pick", plan(pick, self.state)))
        before = self.book.entries
        self.assertFalse(self.book.reserve_plan(self.state, "boots", plan(boots, self.state)))
        self.assertEqual(self.book.entries, before)
        stock(self.state, iron_ingot=7, stick=2)
        self.assertTrue(self.book.reserve_plan(self.state, "boots", plan(boots, self.state)))
        self.assertEqual(self.book.available(self.state, "iron_ingot"), 0)

    def test_one_bucket_moves_from_lava_to_water_use_only_after_settlement(self):
        lava = make_goal("lava_bucket")
        water = make_goal("form_obsidian")
        world = NetherWorld(items={"bucket": 1, "diamond_pickaxe": 1})
        _, state = world.session()
        self.assertEqual(plan(lava, state), (("bucket", 1, "borrow"),))
        self.assertTrue(self.book.reserve_plan(state, "lava", plan(lava, state)))
        world.items = {"lava_bucket": 1, "diamond_pickaxe": 1}
        state.set_inventory(world.inventory())
        self.assertEqual(self.book.available(state, "lava_bucket"), 0)
        world.items = {"bucket": 1, "diamond_pickaxe": 1}
        state.set_inventory(world.inventory())
        water_plan = plan(water, state)
        self.assertIn(("bucket", 1, "borrow"), water_plan)
        self.assertFalse(self.book.reserve_plan(state, "water", water_plan))
        self.assertFalse(any(row.consumer == "water" for row in self.book.entries))
        self.book.release("lava")
        self.assertTrue(self.book.reserve_plan(state, "water", water_plan))
        world.items = {"water_bucket": 1, "diamond_pickaxe": 1}
        state.set_inventory(world.inventory())
        self.assertEqual(self.book.available(state, "water_bucket"), 0)

    def test_boots_pickaxe_and_enchanting_table_need_nine_diamonds(self):
        boots, pick, enchant = (make_goal(gid) for gid in ("shiny_gear", "prep_diamond_pickaxe", "enchant_item"))
        other = {"iron_pickaxe": 1, "lapis_lazuli": 1, "obsidian": 4, "book": 1, "stick": 2}
        world = EnchantWorld(items={**other, "diamond": 8}, at_table=False)
        _, state = world.session()
        self.assertTrue(self.book.reserve_plan(state, "boots", plan(boots, state)))
        self.assertTrue(self.book.reserve_plan(state, "pick", plan(pick, state)))
        self.assertIn(("diamond", 2, "consume"), plan(enchant, state))
        self.assertFalse(self.book.reserve_plan(state, "enchant", plan(enchant, state)))
        world.items["diamond"] = 9
        state.set_inventory(world.inventory())
        self.assertTrue(self.book.reserve_plan(state, "enchant", plan(enchant, state)))
        self.assertEqual(self.book.available(state, "diamond"), 0)

    def test_seed_claims_cover_one_plant_and_two_chicken_feedings(self):
        plant, breed = (make_goal(gid) for gid in ("plant_seed", "breed_an_animal"))
        stock(self.state, wheat_seeds=2)
        self.assertTrue(self.book.reserve_plan(self.state, "plant", plan(plant, self.state)))
        self.assertFalse(self.book.reserve_plan(self.state, "breed", plan(breed, self.state)))
        stock(self.state, wheat_seeds=3)
        self.assertTrue(self.book.reserve_plan(self.state, "breed", plan(breed, self.state)))
        self.assertEqual(self.book.available(self.state, "wheat_seeds"), 0)

    def test_real_focus_dispatch_observes_contention_then_releases_when_finished(self):
        planner = CampaignPlanner(mode="focus", focus_skill="craft_iron_gear", focus_args={"item": "iron_boots"})
        stock(self.state, iron_ingot=6)
        self.assertTrue(planner.reservations.reserve(self.state, "pick", "iron_ingot", 3))
        self.assertIsNone(planner.select(self.state, []).skill)
        planner.reservations.release("pick")
        self.assertEqual(planner.select(self.state, []).skill.name, "craft_iron_gear")
        self.assertEqual(planner.reservations.available(self.state, "iron_ingot"), 2)
        planner.record_skill_outcome("craft_iron_gear", ok=False)
        self.assertEqual(planner.reservations.available(self.state, "iron_ingot"), 6)

    def test_dimension_change_discards_old_reservations_only_after_refresh(self):
        stock(self.state, iron_ingot=4)
        self.assertTrue(self.book.reserve(self.state, "prior", "iron_ingot", 4))
        self.state.set_status({"ready": True, "alive": True, "dimension": "nether"})
        self.state.invalidate_inventory()
        self.planner.select(self.state, [])
        self.assertTrue(self.book.stale)
        self.assertEqual(len(self.book.entries), 1)
        stock(self.state, iron_ingot=4)
        self.planner.select(self.state, [])
        self.assertFalse(self.book.stale)
        self.assertEqual(self.book.entries, ())

    def test_main_loop_budget_interrupt_releases_active_claim(self):
        class World(ScriptedWorld):
            def wait_ready(self, timeout=120):
                return self.status()

        planner = CampaignPlanner(mode="focus", focus_skill="craft_iron_gear", focus_args={"item": "flint_and_steel"})
        world = World(items={"iron_ingot": 1, "flint": 1})

        def craft(world, *, item, count, **_):
            self.assertEqual({row.item for row in planner.reservations.entries}, {"iron_ingot", "flint"})
            self.assertEqual((item, count), ("flint_and_steel", 1))
            world.items = {item: 1}
            return {"item": item}

        world.queue("craft", craft)
        ex, state = world.session()
        loop = MainLoop(world, state, ex, planner, log=lambda *_: None,
                        config=LoopConfig(mode="focus", max_steps=1, enable_monitor=False))
        self.assertEqual(loop.run().stop_reason, "max_steps_reached")
        self.assertEqual(planner.reservations.entries, ())
