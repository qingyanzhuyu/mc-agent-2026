"""Regression tests for proactive food reserve upgrade (48m scan, surface foraging stride, pre-mine food gate)."""
from __future__ import annotations

import math
import unittest

from mcagent.loop.policy import CampaignPlanner, PlanDecision, RulePlanner
from mcagent.runtime import EnvBlocked, RuntimeConfig, State
from mcagent.skills import make
from mcagent.skills.survival_food import inventory_food_restore
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


if __name__ == "__main__":
    unittest.main()
