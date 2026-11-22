from __future__ import annotations

import unittest

from mcagent.loop.reserve import ReservationBook
from mcagent.loop.subgoals import (
    active_portal, item_available, landmark_reachable, safe_encounter, station_ready, tool_ready,
)
from mcagent.runtime import State


def stock(state: State, **counts: int) -> None:
    state.set_inventory({"items": [{"name": name, "count": count} for name, count in counts.items()]})


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.state.set_status({"ready": True, "alive": True, "dimension": "overworld"})
        self.book = ReservationBook()

    def test_six_iron_cannot_be_reserved_for_pick_and_boots(self):
        stock(self.state, iron_ingot=6)
        self.assertTrue(self.book.reserve(self.state, "pick", "iron_ingot", 3))
        self.assertFalse(self.book.reserve(self.state, "boots", "iron_ingot", 4))
        self.assertEqual(self.book.available(self.state, "iron_ingot", "pick"), 6)
        self.assertEqual(self.book.available(self.state, "iron_ingot", "boots"), 3)
        stock(self.state, iron_ingot=7)
        self.assertTrue(self.book.reserve(self.state, "boots", "iron_ingot", 4))
        self.assertEqual(self.book.available(self.state, "iron_ingot"), 0)

    def test_two_diamond_consumers_need_seven(self):
        stock(self.state, diamond=6)
        self.assertTrue(self.book.reserve(self.state, "boots", "diamond", 4))
        self.assertFalse(self.book.reserve(self.state, "pick", "diamond", 3))
        stock(self.state, diamond=7)
        self.assertTrue(self.book.reserve(self.state, "pick", "diamond", 3))

    def test_bucket_ownership_survives_inventory_variant_change(self):
        stock(self.state, bucket=1)
        self.assertTrue(self.book.reserve(self.state, "lava", "bucket", 1, mode="borrow"))
        stock(self.state, lava_bucket=1)
        self.assertFalse(self.book.reserve(self.state, "water", "lava_bucket", 1, mode="borrow"))
        stock(self.state, bucket=1)
        self.book.release("lava")
        self.assertTrue(self.book.reserve(self.state, "water", "bucket", 1, mode="borrow"))

    def test_seed_stock_is_separate_from_safety_floor(self):
        self.book = ReservationBook({"wheat_seeds": 1})
        stock(self.state, wheat_seeds=3)
        self.assertTrue(self.book.reserve(self.state, "plant", "wheat_seeds", 1))
        self.assertFalse(self.book.reserve(self.state, "breed", "wheat_seeds", 2))
        stock(self.state, wheat_seeds=4)
        self.assertTrue(self.book.reserve(self.state, "breed", "wheat_seeds", 2))
        self.assertEqual(self.book.available(self.state, "wheat_seeds"), 0)

    def test_seed_stock_without_floor_supports_one_plant_and_two_feeds(self):
        stock(self.state, wheat_seeds=3)
        self.assertTrue(self.book.reserve(self.state, "plant", "wheat_seeds", 1))
        self.assertTrue(self.book.reserve(self.state, "breed", "wheat_seeds", 2))
        self.assertEqual(self.book.available(self.state, "wheat_seeds"), 0)

    def test_inflight_material_is_not_subtracted_twice_from_bag(self):
        stock(self.state, raw_iron=3)
        self.assertTrue(self.book.reserve(self.state, "smelt", "raw_iron", 2))
        stock(self.state, raw_iron=1)
        self.assertTrue(self.book.move_to_container(self.state, "smelt", "raw_iron", 2))
        self.assertEqual(self.book.entries[0].mode, "in_transit")
        self.assertEqual(self.book.available(self.state, "raw_iron"), 1)

    def test_death_marks_all_old_reservations_stale_until_reset(self):
        stock(self.state, iron_ingot=4)
        self.assertTrue(self.book.reserve(self.state, "boots", "iron_ingot", 4))
        self.state.set_status({"ready": False, "alive": False, "dimension": "overworld"})
        stock(self.state, iron_ingot=4)
        self.assertEqual(self.book.available(self.state, "iron_ingot"), 0)
        self.assertTrue(self.book.stale)
        self.assertEqual(len(self.book.entries), 1)
        self.state.set_status({"ready": True, "alive": True, "dimension": "overworld"})
        self.book.reset(self.state)
        self.assertFalse(self.book.stale)
        self.assertEqual(self.book.available(self.state, "iron_ingot"), 4)

    def test_dimension_change_or_dirty_inventory_closes_allocation(self):
        stock(self.state, bucket=1)
        self.assertTrue(self.book.reserve(self.state, "water", "bucket", 1, mode="borrow"))
        self.state.invalidate_inventory()
        self.assertEqual(self.book.available(self.state, "bucket", "water"), 0)
        stock(self.state, bucket=1)
        self.state.set_status({"ready": True, "alive": True, "dimension": "nether"})
        self.assertFalse(self.book.reserve(self.state, "lava", "bucket", 1, mode="borrow"))
        self.assertTrue(self.book.stale)


class SubgoalPredicatesTests(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.state.set_status({"ready": True, "alive": True, "dimension": "overworld", "health": 20})

    def test_station_and_landmark_require_current_dimension_and_route_evidence(self):
        station = {"name": "furnace", "dimension": "overworld", "reachable": True}
        landmark = {"type": "village", "dimension": "overworld", "reachable": True, "return_safe": True}
        self.assertTrue(station_ready(self.state, [station], "furnace", "overworld"))
        self.assertTrue(landmark_reachable(self.state, [landmark], "village", "overworld"))
        self.assertFalse(station_ready(self.state, [station], "furnace", "nether"))
        self.assertFalse(landmark_reachable(self.state, [{**landmark, "return_safe": False}], "village", "overworld"))

    def test_item_and_tool_require_fresh_inventory_and_real_durability(self):
        self.state.set_inventory({"items": [{"name": "copper_pickaxe", "count": 1, "durability": [4, 190]}]})
        self.assertTrue(tool_ready(self.state, "pickaxe", "stone", 4))
        self.assertFalse(tool_ready(self.state, "pickaxe", "iron", 1))
        self.assertFalse(tool_ready(self.state, "pickaxe", "copper", 5))
        self.assertTrue(item_available(self.state, "copper_pickaxe", 1, allocatable=1))
        self.assertFalse(item_available(self.state, "copper_pickaxe", 1, allocatable=0))
        self.state.invalidate_inventory()
        self.assertFalse(tool_ready(self.state, "pickaxe", "stone"))
        self.assertFalse(item_available(self.state, "copper_pickaxe", 1))

    def test_encounter_needs_reachable_retreat_and_explicit_ammunition(self):
        skeleton = {"id": 1, "name": "skeleton", "type": "hostile", "dist": 8, "health": 20}
        args = {"dimension": "overworld", "retreat_safe": True, "min_health": 12, "ammo": ("arrow", 1)}
        stock(self.state, arrow=1)
        self.assertTrue(safe_encounter(self.state, skeleton, "skeleton", **args))
        self.assertFalse(safe_encounter(self.state, {**skeleton, "health": None}, "skeleton", **args))
        self.assertFalse(safe_encounter(self.state, skeleton, "skeleton", **{**args, "retreat_safe": False}))
        stock(self.state)
        self.assertFalse(safe_encounter(self.state, skeleton, "skeleton", **args))

    def test_portal_requires_active_block_not_obsidian_frame(self):
        portal = {"name": "nether_portal", "dimension": "overworld"}
        self.assertTrue(active_portal(self.state, portal, "overworld", "nether"))
        self.assertFalse(active_portal(self.state, portal, "overworld", "end"))
        self.assertFalse(active_portal(self.state, {**portal, "name": "obsidian"}, "overworld", "nether"))
