"""Regression tests for proactive food reserve upgrade (48m scan, surface foraging stride, pre-mine food gate)."""
from __future__ import annotations

import math
import unittest

from mcagent.loop.context import summarize_inventory_and_status
from mcagent.loop.policy import CampaignPlanner, PlanDecision, RulePlanner, build_state_summary
from mcagent.planner.objective import OBJECTIVE_SYSTEM_PROMPT
from mcagent.planner.skill_choice import SKILL_SYSTEM_PROMPT, validate_choice
from mcagent.runtime import EnvBlocked, RuntimeConfig, State
from mcagent.skills import make
from mcagent.skills.furnace_lease import lease_book_for
from mcagent.skills.survival_food import (
    TWO_DAY_FOOD_RESTORE,
    cooked_food_restore,
    effective_food_restore,
    inventory_food_restore,
)
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


class _ForageFoodWorld(GridWorld):
    def __init__(self, *, pos=(0.5, 64.0, 0.5), items=None, blocks=None, entities=None, is_day=True):
        super().__init__(pos=list(pos), blocks=blocks or {}, items=items or {})
        self.st.update(dimension="overworld", ready=True, alive=True, on_ground=True, health=20.0, food=20.0, is_day=is_day)
        self.entities = [dict(e) for e in (entities or [])]
        self.entity_radii: list[int] = []
        self.moves: list[list[float]] = []

    def get_entities(self, radius=32, types=None):
        self.entity_radii.append(int(radius))
        px, py, pz = self.st["pos"]
        hits = []
        for e in self.entities:
            if e.get("alive") is False:
                continue
            d = math.dist((px, py, pz), e["pos"])
            if d <= float(radius):
                hits.append(dict(e))
        return {"ready": True, "entities": hits}

    def find_blocks(self, types=None, radius=32, limit=32, **kwargs):
        wanted = set(types or [])
        px, py, pz = self.st["pos"]
        hits = []
        for (bx, by, bz), name in self.blocks.items():
            if wanted and name not in wanted:
                continue
            d = math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5))
            if d <= float(radius):
                hits.append({"name": name, "pos": [bx, by, bz], "distance": round(d, 2)})
        hits.sort(key=lambda h: h["distance"])
        return {"ready": True, "blocks": hits[: int(limit)]}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
        dest = list(pos) if pos is not None else [x, y, z]
        self.moves.append(dest)
        self.st["pos"] = [float(dest[0]), float(dest[1]), float(dest[2])]
        return {"pos": list(self.st["pos"])}

    def attack(self, entity_id, timeout_ms=None, max_hits=6, **kwargs):
        for e in self.entities:
            if e["id"] == entity_id:
                e["alive"] = False
                self.items["beef"] = self.items.get("beef", 0) + 3
                return {"hits": 2, "killed": True}
        return {"hits": 0}


class TestProactiveFoodReserve(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()

    def tearDown(self):
        reset_all_world_memos()

    def test_gather_food_scans_48_blocks_and_hunts_distant_animal(self):
        w = _ForageFoodWorld(
            items={"stone_sword": 1},
            entities=[{"id": 101, "type": "animal", "name": "cow", "pos": [40.5, 64.0, 0.5], "health": 10, "alive": True}],
        )
        ex, st = w.session()
        skill = make("gather_food", {"target_restore": 8, "cook": False})
        drive(skill, ex, st)
        self.assertTrue(any(r >= 48 for r in w.entity_radii), f"expected radius>=48 scan, got {w.entity_radii}")
        self.assertGreaterEqual(inventory_food_restore(st), 8)

    def test_gather_food_forages_outward_to_new_clearing_when_local_empty(self):
        # Cow is 58m away (outside 48m from origin, inside 48m from clearing at x=24)
        w = _ForageFoodWorld(
            items={"stone_sword": 1},
            blocks={(24, 63, 0): "grass_block", (24, 64, 0): "air", (24, 65, 0): "air"},
            entities=[{"id": 202, "type": "animal", "name": "cow", "pos": [58.5, 64.0, 0.5], "health": 10, "alive": True}],
        )
        ex, st = w.session()
        skill = make("gather_food", {"target_restore": 8, "cook": False})
        drive(skill, ex, st)
        self.assertGreaterEqual(inventory_food_restore(st), 8)
        self.assertTrue(any(m[0] >= 20.0 for m in w.moves), f"expected outward forage stride, got {w.moves}")

    def test_policy_proactive_food_floor_14_and_single_raw_meat_cooking(self):
        rp = RulePlanner(min_verified="offline")
        # 1) restore=12 (cooked_mutton x2 = 12 < 14) with stone_sword -> triggers daytime gather_food
        st_12 = State(RuntimeConfig())
        st_12.set_status({"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "ready": True, "alive": True, "health": 20.0, "food": 20.0, "is_day": True, "nearest_hostile": None})
        st_12.set_inventory({"items": [{"name": "stone_sword", "count": 1, "slot": 0}, {"name": "cooked_mutton", "count": 2, "slot": 1}], "empty_slots": 34})
        dec = rp.choose_proactive_survival(st_12)
        self.assertIsNotNone(dec)
        self.assertEqual(dec.skill.name, "gather_food")

        # 2) Single raw beef (count=1) + coal + furnace at night -> triggers cooking
        st_raw1 = State(RuntimeConfig())
        st_raw1.set_status({"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "ready": True, "alive": True, "health": 20.0, "food": 20.0, "is_day": False, "nearest_hostile": None})
        st_raw1.set_inventory({"items": [{"name": "beef", "count": 1, "slot": 0}, {"name": "coal", "count": 2, "slot": 1}], "empty_slots": 34})
        memo_for(st_raw1).record_station("furnace", [1, 64, 0], dimension="overworld")
        dec_cook = rp.choose_proactive_survival(st_raw1)
        self.assertIsNotNone(dec_cook)
        self.assertEqual(dec_cook.skill.name, "gather_food")

    def test_campaign_pre_mine_food_gate_scouts_surface_before_caving_with_zero_food(self):
        st = State(RuntimeConfig())
        st.set_status({"pos": [0.5, 69.0, 0.5], "dimension": "overworld", "ready": True, "alive": True, "health": 20.0, "food": 20.0, "is_day": True, "nearest_hostile": None})
        st.set_inventory({
            "items": [
                {"name": "stone_sword", "count": 1, "slot": 0},
                {"name": "stone_pickaxe", "count": 1, "slot": 1},
                {"name": "furnace", "count": 1, "slot": 2},
                {"name": "coal", "count": 4, "slot": 3},
            ],
            "empty_slots": 32,
        })
        planner = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline")
        # Simulate gather_food EnvBlocked in local forest patch
        gf_inst = make("gather_food", {"target_restore": 16, "cook": True})
        planner.rules.record_environment_block(
            gf_inst,
            EnvBlocked("无动物", missing_predicate="food_source", searched_volume="nearby", wake_on="chunk_moved"),
            st,
        )
        smelt_choice = PlanDecision(make("smelt_iron", {"count": 4}), "rule", "test smelt_iron")
        gated = planner._apply_pre_mine_food_gate(st, smelt_choice)
        self.assertEqual(gated.skill.name, "scout_island")

        # After 3 bounded scouts, gate must yield and allow smelt_iron so barren worlds never deadlock
        gated2 = planner._apply_pre_mine_food_gate(st, smelt_choice)
        gated3 = planner._apply_pre_mine_food_gate(st, smelt_choice)
        gated4 = planner._apply_pre_mine_food_gate(st, smelt_choice)
        self.assertEqual(gated2.skill.name, "scout_island")
        self.assertEqual(gated3.skill.name, "scout_island")
        self.assertEqual(gated4.skill.name, "smelt_iron")

    def test_two_day_effective_food_stops_overhunting_and_respects_furnace_lease(self):
        # 5 cows nearby; each drops 3 raw beef (cooked value = 24). Two cows = 48 >= 32 -> stops after 2 cows, never 4!
        cows = [
            {"id": 300 + i, "type": "animal", "name": "cow", "pos": [2.5 + i * 1.5, 64.0, 0.5], "health": 10, "alive": True}
            for i in range(5)
        ]
        w = _ForageFoodWorld(items={"stone_sword": 1}, entities=cows)
        ex, st = w.session()
        skill = make("gather_food", {"target_restore": TWO_DAY_FOOD_RESTORE, "cook": True, "hunt": True})
        drive(skill, ex, st)
        killed = sum(1 for e in w.entities if e.get("alive") is False)
        self.assertEqual(killed, 2)
        self.assertGreaterEqual(effective_food_restore(st), TWO_DAY_FOOD_RESTORE)

        # Active furnace lease counts toward effective_food_restore and stops proactive hunting & pre-mine gate
        st_lease = State(RuntimeConfig())
        st_lease.set_status({"pos": [0.5, 68.0, 0.5], "dimension": "overworld", "ready": True, "alive": True, "health": 20.0, "food": 20.0, "is_day": True, "nearest_hostile": None})
        st_lease.set_inventory({"items": [{"name": "stone_sword", "count": 1, "slot": 0}, {"name": "stone_pickaxe", "count": 1, "slot": 1}], "empty_slots": 34})
        lease_book_for(st_lease).register_lease(
            (2, 68, 0), dimension="overworld", station_kind="furnace",
            input_item="beef", expected_output="cooked_beef", count=4, consumer="gather_food",
        )
        self.assertEqual(effective_food_restore(st_lease), 32)
        rp = RulePlanner(min_verified="offline")
        self.assertIsNone(rp.choose_proactive_survival(st_lease))
        cp = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline")
        smelt_choice = PlanDecision(make("smelt_iron", {"count": 4}), "rule", "test smelt_iron")
        self.assertEqual(cp._apply_pre_mine_food_gate(st_lease, smelt_choice).skill.name, "smelt_iron")

    def test_cook_only_and_ready_lease_dispatch_never_hunt_and_full_two_day_blocks_redisp(self):
        # 1) Policy dispatches hunt=False when cooking camp raw meat or retrieving ready food lease
        rp = RulePlanner(min_verified="offline")
        st_raw = State(RuntimeConfig())
        st_raw.set_status({"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "ready": True, "alive": True, "health": 20.0, "food": 20.0, "is_day": True, "nearest_hostile": None})
        st_raw.set_inventory({"items": [{"name": "beef", "count": 2, "slot": 0}, {"name": "coal", "count": 2, "slot": 1}, {"name": "stone_sword", "count": 1, "slot": 2}], "empty_slots": 33})
        memo_for(st_raw).record_station("furnace", [2, 64, 0], dimension="overworld")
        dec_cook = rp.choose_proactive_survival(st_raw)
        self.assertIsNotNone(dec_cook)
        self.assertEqual(dec_cook.skill.name, "gather_food")
        self.assertFalse(dec_cook.skill.args["hunt"])

        # 2) Once cooked_food_restore >= 32 (4 cooked_beef), even stray raw meat + furnace will NOT interrupt main quest
        st_full = State(RuntimeConfig())
        st_full.set_status({"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "ready": True, "alive": True, "health": 20.0, "food": 20.0, "is_day": True, "nearest_hostile": None})
        st_full.set_inventory({"items": [{"name": "cooked_beef", "count": 4, "slot": 0}, {"name": "chicken", "count": 1, "slot": 1}, {"name": "coal", "count": 2, "slot": 2}, {"name": "stone_sword", "count": 1, "slot": 3}], "empty_slots": 32})
        memo_for(st_full).record_station("furnace", [2, 64, 0], dimension="overworld")
        self.assertEqual(cooked_food_restore(st_full), 32)
        self.assertIsNone(rp.choose_proactive_survival(st_full))

        # 3) GatherFood.can_start rejects default 32-pt gather_food when already >=32 cooked, while allowing Nether 64-pt prep
        st_fed = State(RuntimeConfig())
        st_fed.set_status({"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "ready": True, "alive": True, "health": 20.0, "food": 20.0, "is_day": True, "nearest_hostile": None})
        st_fed.set_inventory({"items": [{"name": "cooked_beef", "count": 4, "slot": 0}, {"name": "stone_sword", "count": 1, "slot": 1}], "empty_slots": 34})
        self.assertFalse(make("gather_food").can_start(st_fed))
        ok_choice, errs = validate_choice({"skill": "gather_food", "args": {}, "why": "再找吃的"}, st_fed)
        self.assertFalse(ok_choice)
        self.assertTrue(errs)
        self.assertTrue(make("gather_food", {"target_restore": 64, "cook": True}).can_start(st_fed))

        # 4) hunt=False never attacks nearby animals even if inventory_food_restore < target_restore
        w_nohunt = _ForageFoodWorld(
            items={"stone_sword": 1},
            entities=[{"id": 401, "type": "animal", "name": "cow", "pos": [2.5, 64.0, 0.5], "health": 10, "alive": True}],
        )
        ex, st_nh = w_nohunt.session()
        skill_nh = make("gather_food", {"target_restore": 32, "cook": True, "hunt": False})
        drive(skill_nh, ex, st_nh)
        self.assertTrue(w_nohunt.entities[0]["alive"])

    def test_prompts_and_summaries_surface_two_day_food_threshold(self):
        st_fed = State(RuntimeConfig())
        st_fed.set_status({"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "ready": True, "alive": True, "health": 20.0, "food": 20.0, "is_day": True, "nearest_hostile": None})
        st_fed.set_inventory({"items": [{"name": "cooked_beef", "count": 4, "slot": 0}], "empty_slots": 35})
        self.assertIn("够2天-勿补粮", summarize_inventory_and_status(st_fed))
        self.assertIn("够2天-勿补粮", build_state_summary(st_fed))
        self.assertIn("32", OBJECTIVE_SYSTEM_PROMPT)
        self.assertIn("2 个游戏日", OBJECTIVE_SYSTEM_PROMPT)
        self.assertIn("32", SKILL_SYSTEM_PROMPT)
        self.assertIn("gather_food", SKILL_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
