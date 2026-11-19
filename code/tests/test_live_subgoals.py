"""Live subgoal predicates and enriched DecisionContext offline unit tests (verified: offline)."""
from __future__ import annotations

import json
import re
import time
import unittest

from mcagent.loop import CampaignPlanner
from mcagent.loop.context import (
    DecisionContext,
    ObjectiveRecord,
    build_decision_context,
    format_world_landmarks,
)
from mcagent.loop.goals import GOALS
from mcagent.loop.reserve import ReservationBook
from mcagent.loop.subgoals import (
    active_portal,
    evaluate_all_subgoals,
    item_available,
    landmark_reachable,
    safe_encounter,
    station_ready,
    tool_ready,
)
from mcagent.planner.objective import Objective
from mcagent.runtime import EnvBlocked, State
from mcagent.skills import base as skill_base
from mcagent.skills import make
from mcagent.skills.world_memo import WorldResourceMemo, memo_for, reset_all_world_memos


def _make_state(
    *,
    ready: bool = True,
    alive: bool = True,
    dimension: str = "overworld",
    health: float = 20.0,
    food: float = 20.0,
    is_day: bool = True,
    extra_status: dict | None = None,
    inventory: dict | None = None,
) -> State:
    st = State()
    status = {
        "ready": ready,
        "alive": alive,
        "dimension": dimension,
        "health": health,
        "food": food,
        "is_day": is_day,
    }
    if extra_status:
        status.update(extra_status)
    st.set_status(status)
    if inventory is not None:
        st.set_inventory(inventory)
    return st


def _stock(state: State, **counts: int) -> None:
    state.set_inventory({"items": [{"name": name, "count": count} for name, count in counts.items()]})


class LiveSubgoalPredicatesTests(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_all_world_memos()
        self.addCleanup(reset_all_world_memos)
        self.addCleanup(skill_base._COOLDOWN.clear)
        self.state = _make_state(inventory={"items": []})
        self.memo = memo_for(self.state)

    # ---- 1. item_available ----

    def test_item_available_fresh_insufficient_allocatable_and_dirty(self) -> None:
        _stock(self.state, iron_ingot=5)
        self.assertTrue(item_available(self.state, "iron_ingot", 3))
        self.assertTrue(item_available(self.state, "minecraft:iron_ingot", 5))
        self.assertFalse(item_available(self.state, "iron_ingot", 6))

        # allocatable constraint
        self.assertTrue(item_available(self.state, "iron_ingot", 3, allocatable=3))
        self.assertTrue(item_available(self.state, "iron_ingot", 3, allocatable=5))
        self.assertFalse(item_available(self.state, "iron_ingot", 3, allocatable=2))

        # invalid quantity guards
        self.assertFalse(item_available(self.state, "iron_ingot", 0))
        self.assertFalse(item_available(self.state, "iron_ingot", -1))
        self.assertFalse(item_available(self.state, "iron_ingot", True))  # type: ignore[arg-type]

        # dirty inventory -> False
        self.state.invalidate_inventory()
        self.assertFalse(item_available(self.state, "iron_ingot", 1))

    # ---- 2. tool_ready ----

    def test_tool_ready_tier_hierarchy_durability_and_dirty(self) -> None:
        # Tier hierarchy: wooden < stone == copper < iron < diamond
        _stock(self.state, wooden_pickaxe=1)
        self.assertTrue(tool_ready(self.state, "pickaxe", "wooden"))
        self.assertFalse(tool_ready(self.state, "pickaxe", "stone"))

        _stock(self.state, stone_pickaxe=1)
        self.assertTrue(tool_ready(self.state, "pickaxe", "wooden"))
        self.assertTrue(tool_ready(self.state, "pickaxe", "stone"))
        self.assertTrue(tool_ready(self.state, "pickaxe", "copper"))
        self.assertFalse(tool_ready(self.state, "pickaxe", "iron"))

        _stock(self.state, copper_pickaxe=1)
        self.assertTrue(tool_ready(self.state, "pickaxe", "stone"))
        self.assertTrue(tool_ready(self.state, "pickaxe", "copper"))
        self.assertFalse(tool_ready(self.state, "pickaxe", "iron"))

        _stock(self.state, iron_pickaxe=1)
        self.assertTrue(tool_ready(self.state, "pickaxe", "iron"))
        self.assertFalse(tool_ready(self.state, "pickaxe", "diamond"))

        _stock(self.state, diamond_pickaxe=1)
        self.assertTrue(tool_ready(self.state, "pickaxe", "iron"))
        self.assertTrue(tool_ready(self.state, "pickaxe", "diamond"))

        # Durability checks: missing durability only passes min_durability == 0
        self.state.set_inventory({
            "items": [{"name": "iron_pickaxe", "count": 1}],
        })
        self.assertTrue(tool_ready(self.state, "pickaxe", "iron", 0))
        self.assertFalse(tool_ready(self.state, "pickaxe", "iron", 1))

        # Explicit durability tuple/list in held/offhand
        self.state.set_inventory({
            "items": [],
            "held": {"name": "iron_pickaxe", "count": 1, "durability": [0, 250]},
        })
        self.assertFalse(tool_ready(self.state, "pickaxe", "iron", 0))

        self.state.set_inventory({
            "items": [],
            "offhand": {"name": "iron_pickaxe", "count": 1, "durability": [15, 250]},
        })
        self.assertTrue(tool_ready(self.state, "pickaxe", "iron", 15))
        self.assertFalse(tool_ready(self.state, "pickaxe", "iron", 16))

        # Dirty inventory -> False
        self.state.invalidate_inventory()
        self.assertFalse(tool_ready(self.state, "pickaxe", "iron", 1))

    # ---- 3. station_ready ----

    def test_station_ready_inventory_and_memo_variants_and_signatures(self) -> None:
        # Initially no stations
        self.assertFalse(station_ready(self.state))
        self.assertFalse(station_ready(self.state, "crafting_table"))

        # Inventory detection (crafting_table, blast_furnace/smoker -> furnace, red_bed -> bed, enchanting_table)
        _stock(self.state, crafting_table=1)
        self.assertTrue(station_ready(self.state, "crafting_table"))
        self.assertTrue(station_ready(self.state))
        self.assertFalse(station_ready(self.state, "furnace"))

        _stock(self.state, blast_furnace=1)
        self.assertTrue(station_ready(self.state, "furnace"))

        _stock(self.state, smoker=1)
        self.assertTrue(station_ready(self.state, "furnace"))

        _stock(self.state, red_bed=1)
        self.assertTrue(station_ready(self.state, station="bed", memo=self.memo))

        _stock(self.state, enchanting_table=1)
        self.assertTrue(station_ready(self.state, "enchanting_table"))

        # Empty inventory -> rely on WorldResourceMemo.record_station
        _stock(self.state)
        self.assertFalse(station_ready(self.state))

        self.memo.record_station("furnace", [10, 64, 12], dimension="overworld")
        self.assertTrue(station_ready(self.state, "furnace"))
        self.assertTrue(station_ready(self.state))

        # ingest_find_blocks populates stations and bed variants
        self.memo.ingest_find_blocks(
            {
                "blocks": [
                    {"name": "minecraft:crafting_table", "pos": [15, 64, 15], "dist": 5.0},
                    {"name": "blue_bed", "pos": [20, 64, 15], "dist": 7.0},
                    {"name": "enchanting_table", "pos": [25, 64, 15], "dist": 9.0},
                ]
            },
            dimension="overworld",
        )
        self.assertTrue(station_ready(self.state, "crafting_table"))
        self.assertTrue(station_ready(self.state, station="bed", memo=self.memo))
        self.assertTrue(station_ready(self.state, "enchanting_table"))

    def test_station_ready_dimension_isolation_alive_ready_and_t1_observations(self) -> None:
        self.memo.record_station("crafting_table", [5, 64, 5], dimension="overworld")
        self.assertTrue(station_ready(self.state, "crafting_table", dimension="overworld"))
        self.assertFalse(station_ready(self.state, "crafting_table", dimension="the_nether"))

        # Moving player to the_nether isolates overworld stations
        nether_state = _make_state(dimension="the_nether", inventory={"items": []})
        nether_memo = WorldResourceMemo()
        nether_memo.record_station("crafting_table", [5, 64, 5], dimension="overworld")
        self.assertFalse(station_ready(nether_state, "crafting_table", memo=nether_memo))

        nether_memo.record_station("smoker", [8, 70, 8], dimension="the_nether")
        self.assertTrue(station_ready(nether_state, "furnace", memo=nether_memo))

        # alive=False / ready=False -> False
        dead_state = _make_state(alive=False, inventory={"items": [{"name": "crafting_table", "count": 1}]})
        self.assertFalse(station_ready(dead_state, "crafting_table"))
        unready_state = _make_state(ready=False, inventory={"items": [{"name": "crafting_table", "count": 1}]})
        self.assertFalse(station_ready(unready_state, "crafting_table"))

        # T1 observation-based mode remains intact
        obs = [{"name": "furnace", "dimension": "overworld", "reachable": True}]
        self.assertTrue(station_ready(self.state, obs, "furnace", "overworld"))
        self.assertFalse(station_ready(self.state, obs, "furnace", "nether"))
        self.assertFalse(station_ready(self.state, [{"name": "furnace", "dimension": "overworld", "reachable": False}], "furnace", "overworld"))

    # ---- 4. landmark_reachable ----

    def test_landmark_reachable_landmarks_resources_blocked_and_t1_observations(self) -> None:
        self.assertFalse(landmark_reachable(self.state))
        self.assertFalse(landmark_reachable(self.state, "village"))

        # record_landmark
        self.memo.record_landmark("village", [120, 68, -40], dimension="overworld")
        self.memo.record_landmark("stronghold", [900, 32, 400], dimension="overworld")
        self.assertTrue(landmark_reachable(self.state))
        self.assertTrue(landmark_reachable(self.state, "village"))
        self.assertTrue(landmark_reachable(self.state, kind="stronghold", memo=self.memo))
        self.assertFalse(landmark_reachable(self.state, "ruined_portal"))

        # ingest_find_blocks detects structure fingerprints (bee_nest, ruined_portal) & resources
        self.memo.ingest_find_blocks(
            {
                "blocks": [
                    {"name": "bee_nest", "pos": [40, 66, 12], "dist": 12.0},
                    {"name": "crying_obsidian", "pos": [60, 64, 20], "dist": 25.0},
                    {"name": "oak_log", "pos": [18, 64, 4], "dist": 6.0},
                    {"name": "iron_ore", "pos": [22, 45, 9], "dist": 18.0},
                ]
            },
            dimension="overworld",
        )
        self.assertTrue(landmark_reachable(self.state, "bee_nest"))
        self.assertTrue(landmark_reachable(self.state, "ruined_portal"))
        self.assertTrue(landmark_reachable(self.state, "wood"))
        self.assertTrue(landmark_reachable(self.state, kind="iron"))
        self.assertFalse(landmark_reachable(self.state, kind="diamond"))

        # record_seen + ignoring protected/depleted resource entries
        self.memo.record_seen("diamond_ore", "diamond_ore", [30, 12, -18], dimension="overworld")
        self.assertTrue(landmark_reachable(self.state, kind="diamond"))
        self.memo.mark_protected([30, 12, -18], dimension="overworld", category="diamond_ore")
        self.assertFalse(landmark_reachable(self.state, kind="diamond"))

        self.memo.record_seen("diamond_ore", "deepslate_diamond_ore", [34, -20, -18], dimension="overworld")
        self.assertTrue(landmark_reachable(self.state, kind="diamond"))
        self.memo.mark_depleted([34, -20, -18], dimension="overworld", category="diamond_ore")
        self.assertFalse(landmark_reachable(self.state, kind="diamond"))

        # Dimension isolation and ready/alive checks
        self.assertFalse(landmark_reachable(self.state, "village", dimension="the_nether"))
        dead_state = _make_state(alive=False, inventory={"items": []})
        self.assertFalse(landmark_reachable(dead_state, "village", memo=self.memo))

        # T1 observation-based mode remains intact
        obs = [{"type": "village", "dimension": "overworld", "reachable": True, "return_safe": True}]
        self.assertTrue(landmark_reachable(self.state, obs, "village", "overworld"))
        self.assertFalse(landmark_reachable(self.state, [{**obs[0], "return_safe": False}], "village", "overworld"))

    # ---- 5. safe_encounter ----

    def test_safe_encounter_live_survival_equipment_hazards_and_hostiles(self) -> None:
        # Unarmed -> False
        _stock(self.state)
        self.assertFalse(safe_encounter(self.state))

        # Weapon variants (sword, axe, bow)
        for weapon in ("stone_sword", "iron_axe", "bow"):
            _stock(self.state, **{weapon: 1})
            self.assertTrue(safe_encounter(self.state), msg=f"{weapon} should satisfy weapon check")

        # Low HP and low food
        low_hp = _make_state(health=11.5, food=20, inventory={"items": [{"name": "iron_sword", "count": 1}]})
        self.assertFalse(safe_encounter(low_hp, min_health=12.0))
        self.assertTrue(safe_encounter(low_hp, min_health=11.0))

        low_food = _make_state(health=20, food=9, inventory={"items": [{"name": "iron_sword", "count": 1}]})
        self.assertFalse(safe_encounter(low_food))

        # safe_mode -> False (without time.sleep)
        safe_mode_state = _make_state(inventory={"items": [{"name": "iron_sword", "count": 1}]})
        safe_mode_state.safe_mode_until = time.monotonic() + 30.0
        self.assertTrue(safe_mode_state.in_safe_mode)
        self.assertFalse(safe_encounter(safe_mode_state))

        # Environmental hazards -> False
        for flag in ("in_lava", "in_water", "on_fire"):
            haz_state = _make_state(
                extra_status={flag: True},
                inventory={"items": [{"name": "iron_sword", "count": 1}]},
            )
            self.assertFalse(safe_encounter(haz_state), msg=f"hazard {flag} must make safe_encounter False")

        # Point-blank creeper (< 4.5) without armor -> False; with >= 2 armor pieces -> True
        creeper_no_armor = _make_state(
            health=20,
            food=20,
            extra_status={"nearest_hostile": {"id": 42, "name": "creeper", "dist": 3.5}},
            inventory={"items": [{"name": "iron_sword", "count": 1}]},
        )
        self.assertFalse(safe_encounter(creeper_no_armor))

        creeper_one_armor = _make_state(
            health=20,
            food=20,
            extra_status={"nearest_hostile": {"id": 42, "name": "creeper", "dist": 3.5}},
            inventory={
                "items": [{"name": "iron_sword", "count": 1}],
                "armor": {"chest": {"name": "iron_chestplate", "count": 1}},
            },
        )
        self.assertFalse(safe_encounter(creeper_one_armor))

        creeper_two_armor = _make_state(
            health=20,
            food=20,
            extra_status={"nearest_hostile": {"id": 42, "name": "creeper", "dist": 3.5}},
            inventory={
                "items": [{"name": "iron_sword", "count": 1}],
                "armor": {
                    "chest": {"name": "iron_chestplate", "count": 1},
                    "legs": {"name": "iron_leggings", "count": 1},
                },
            },
        )
        self.assertTrue(safe_encounter(creeper_two_armor))

        # Low HP (< 14) with close hostile (< 3.0) and 0 armor -> False
        close_zombie_low_hp = _make_state(
            health=13.0,
            food=20,
            extra_status={"nearest_hostile": {"id": 43, "name": "zombie", "dist": 2.5}},
            inventory={"items": [{"name": "iron_sword", "count": 1}]},
        )
        self.assertFalse(safe_encounter(close_zombie_low_hp))

        # With >= 1 armor piece at same HP/dist -> True
        close_zombie_armored = _make_state(
            health=13.0,
            food=20,
            extra_status={"nearest_hostile": {"id": 43, "name": "zombie", "dist": 2.5}},
            inventory={
                "items": [{"name": "iron_sword", "count": 1}],
                "armor": {"head": {"name": "iron_helmet", "count": 1}},
            },
        )
        self.assertTrue(safe_encounter(close_zombie_armored))

        # Optional ammo requirement
        _stock(self.state, bow=1)
        self.assertFalse(safe_encounter(self.state, ammo=("arrow", 4)))
        _stock(self.state, bow=1, arrow=4)
        self.assertTrue(safe_encounter(self.state, ammo=("arrow", 4)))

        # T1 observation-based entity mode remains intact
        skeleton = {"id": 7, "name": "skeleton", "type": "hostile", "dist": 8.0, "health": 20}
        self.assertTrue(
            safe_encounter(
                self.state,
                skeleton,
                "skeleton",
                dimension="overworld",
                retreat_safe=True,
                ammo=("arrow", 1),
            )
        )
        self.assertFalse(
            safe_encounter(
                self.state,
                skeleton,
                "skeleton",
                dimension="overworld",
                retreat_safe=False,
                ammo=("arrow", 1),
            )
        )

    # ---- 6. active_portal ----

    def test_active_portal_nether_memo_inventory_and_t1_observations(self) -> None:
        # Empty overworld -> False
        self.assertFalse(active_portal(self.state))

        # Already in the_nether / nether -> True
        self.assertTrue(active_portal(_make_state(dimension="the_nether", inventory={"items": []})))
        self.assertTrue(active_portal(_make_state(dimension="nether", inventory={"items": []})))

        # Recorded active portal in memo for current dimension -> True
        self.memo.record_active_portal([100, 64, -50], dimension="overworld")
        self.assertTrue(active_portal(self.state, memo=self.memo))

        # Dimension isolation for memo portal
        other_memo = WorldResourceMemo()
        other_memo.record_active_portal([100, 64, -50], dimension="the_end")
        self.assertFalse(active_portal(self.state, memo=other_memo))

        # Inventory obsidian >= 10 and flint_and_steel >= 1 -> True
        _stock(self.state, obsidian=10, flint_and_steel=1)
        self.assertTrue(active_portal(self.state, memo=other_memo))

        # Only 9 obsidian -> False
        _stock(self.state, obsidian=9, flint_and_steel=1)
        self.assertFalse(active_portal(self.state, memo=other_memo))

        # Missing flint_and_steel -> False (unlit obsidian frame cannot ignite)
        _stock(self.state, obsidian=14)
        self.assertFalse(active_portal(self.state, memo=other_memo))

        # T1 observation-based mode remains intact
        portal_obs = {"name": "nether_portal", "dimension": "overworld"}
        self.assertTrue(active_portal(self.state, portal_obs, "overworld", "nether"))
        self.assertFalse(active_portal(self.state, {"name": "obsidian", "dimension": "overworld"}, "overworld", "nether"))

    # ---- 7. evaluate_all_subgoals + ReservationBook ----

    def test_evaluate_all_subgoals_keys_no_stub_and_reservation_book(self) -> None:
        self.state.set_inventory({
            "items": [
                {"name": "iron_ingot", "count": 5},
                {"name": "cobblestone", "count": 4},
                {"name": "stone_pickaxe", "count": 1, "durability": [100, 131]},
                {"name": "iron_sword", "count": 1},
            ]
        })
        self.memo.record_station("crafting_table", [2, 64, 2], dimension="overworld")
        self.memo.record_landmark("village", [80, 68, -20], dimension="overworld")
        self.memo.record_landmark("stronghold", [320, 30, -240], dimension="overworld")
        self.memo.record_seen("iron_ore", "iron_ore", [14, 48, 6], dimension="overworld", status="reachable")

        book = ReservationBook()
        res_unreserved = evaluate_all_subgoals(self.state, memo=self.memo, reservations=book)

        self.assertEqual(
            set(res_unreserved.keys()),
            {"item_available", "tool_ready", "station_ready", "landmark_reachable", "safe_encounter", "active_portal"},
        )
        self.assertNotIn("待现场观察", json.dumps(res_unreserved, ensure_ascii=False))
        self.assertTrue(res_unreserved["item_available"]["iron_ingot"])
        self.assertTrue(res_unreserved["tool_ready"]["stone"])
        self.assertFalse(res_unreserved["tool_ready"]["iron"])
        self.assertTrue(res_unreserved["station_ready"]["crafting_table"])
        self.assertTrue(res_unreserved["landmark_reachable"]["village"])
        self.assertTrue(res_unreserved["landmark_reachable"]["stronghold"])
        self.assertTrue(res_unreserved["landmark_reachable"]["iron"])
        self.assertTrue(res_unreserved["safe_encounter"])
        self.assertFalse(res_unreserved["active_portal"])

        # Reserve 3 iron_ingot -> allocatable becomes 2 < 3 -> item_available["iron_ingot"] turns False
        self.assertTrue(book.reserve(self.state, "iron_pick", "iron_ingot", 3))
        res_reserved = evaluate_all_subgoals(self.state, memo=self.memo, reservations=book)
        self.assertFalse(res_reserved["item_available"]["iron_ingot"])
        self.assertNotIn("待现场观察", json.dumps(res_reserved, ensure_ascii=False))


class LiveDecisionContextTests(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_all_world_memos()
        self.addCleanup(reset_all_world_memos)
        self.addCleanup(skill_base._COOLDOWN.clear)

    def test_build_decision_context_populates_live_subgoals_and_landmarks(self) -> None:
        state = _make_state(
            inventory={
                "items": [
                    {"name": "iron_ingot", "count": 4, "slot": 0},
                    {"name": "stone_pickaxe", "count": 1, "slot": 1},
                    {"name": "iron_sword", "count": 1, "slot": 2},
                ]
            }
        )
        memo = memo_for(state)
        memo.record_station("crafting_table", [10, 64, 10])
        memo.record_station("furnace", [12, 64, 10])
        memo.record_landmark("village", [120, 68, -60])
        memo.record_active_portal([24, 64, -18])

        ctx = build_decision_context(state)
        self.assertIsNotNone(ctx.subgoal_predicates)
        assert ctx.subgoal_predicates is not None
        self.assertEqual(
            set(ctx.subgoal_predicates.keys()),
            {"item_available", "tool_ready", "station_ready", "landmark_reachable", "safe_encounter", "active_portal"},
        )
        self.assertTrue(ctx.subgoal_predicates["item_available"]["iron_ingot"])
        self.assertTrue(ctx.subgoal_predicates["station_ready"]["crafting_table"])
        self.assertTrue(ctx.subgoal_predicates["station_ready"]["furnace"])
        self.assertTrue(ctx.subgoal_predicates["landmark_reachable"]["village"])
        self.assertTrue(ctx.subgoal_predicates["active_portal"])
        self.assertTrue(ctx.subgoal_summary)
        self.assertIsNotNone(ctx.world_landmarks)
        self.assertNotIn("待现场观察", ctx.subgoal_summary)

        rendered = ctx.render()
        self.assertIn("工作台=已就绪", rendered)
        self.assertIn("熔炉=已就绪", rendered)
        self.assertIn("村庄=已发现", rendered)
        self.assertIn("传送门=已激活", rendered)
        self.assertNotIn("待现场观察", rendered)

    def test_campaign_planner_replaces_pending_stubs_and_never_emits_placeholder(self) -> None:
        state = _make_state(
            inventory={
                "items": [
                    {"name": "iron_ingot", "count": 3, "slot": 0},
                    {"name": "iron_sword", "count": 1, "slot": 1},
                ]
            }
        )
        memo = memo_for(state)
        memo.record_station("crafting_table", [4, 64, 4])
        memo.record_landmark("ruined_portal", [45, 65, -30])

        planner = CampaignPlanner(mode="campaign", strategy="rule", min_verified="offline")
        ctx = planner.build_context(state)
        rendered = ctx.render()
        self.assertNotIn("待现场观察", rendered)
        self.assertNotIn("待现场观察", ctx.subgoal_summary)
        assert ctx.subgoal_predicates is not None
        self.assertNotIn("待现场观察", json.dumps(ctx.subgoal_predicates, ensure_ascii=False))
        self.assertIsInstance(ctx.subgoal_predicates["station_ready"], dict)
        self.assertTrue(ctx.subgoal_predicates["station_ready"]["crafting_table"])
        self.assertTrue(ctx.subgoal_predicates["landmark_reachable"]["ruined_portal"])
        self.assertTrue(ctx.subgoal_predicates["safe_encounter"])
        self.assertFalse(ctx.subgoal_predicates["active_portal"])

        # Also verify legacy "待现场观察" placeholders supplied to build_decision_context are upgraded
        legacy_ctx = build_decision_context(
            state,
            subgoal_predicates={
                "station_ready": "待现场观察",
                "landmark_reachable": "待现场观察",
                "safe_encounter": "待现场观察",
                "active_portal": "待现场观察",
            },
        )
        self.assertNotIn("待现场观察", legacy_ctx.render())
        assert legacy_ctx.subgoal_predicates is not None
        self.assertTrue(legacy_ctx.subgoal_predicates["station_ready"]["crafting_table"])
        self.assertTrue(legacy_ctx.subgoal_predicates["landmark_reachable"]["ruined_portal"])
        self.assertTrue(legacy_ctx.subgoal_predicates["safe_encounter"])
        self.assertFalse(legacy_ctx.subgoal_predicates["active_portal"])

    def test_heavy_memo_and_goals_stay_within_2000_char_budget(self) -> None:
        state = _make_state(
            health=18,
            food=17,
            is_day=False,
            inventory={
                "items": [
                    {"name": f"item_{i}", "count": i + 1, "slot": i}
                    for i in range(18)
                ]
                + [
                    {"name": "diamond_pickaxe", "count": 1, "slot": 18, "durability": [900, 1561]},
                    {"name": "iron_sword", "count": 1, "slot": 19},
                    {"name": "cooked_beef", "count": 12, "slot": 20},
                ]
            },
        )
        memo = memo_for(state)
        for kind, pos in (
            ("crafting_table", [10, 64, 10]),
            ("furnace", [12, 64, 10]),
            ("blast_furnace", [14, 64, 10]),
            ("smoker", [16, 64, 10]),
            ("red_bed", [18, 64, 10]),
            ("enchanting_table", [20, 64, 10]),
            ("anvil", [22, 64, 10]),
        ):
            memo.record_station(kind, pos)
        for idx, lk in enumerate(("village", "ruined_portal", "stronghold", "bee_nest", "trial_chambers", "deep_dark")):
            memo.record_landmark(lk, [100 + idx * 50, 64, -100 - idx * 50])
        memo.record_active_portal([30, 64, -30])
        for idx in range(6):
            memo.record_seen("wood", "oak_log", [40 + idx * 2, 64, 40], status="reachable")
            memo.record_seen("iron_ore", "iron_ore", [50 + idx * 2, 42, 40], status="reachable")
            memo.record_seen("diamond_ore", "diamond_ore", [60 + idx * 2, 11, 40], status="reachable")
            memo.record_seen("obsidian", "obsidian", [70 + idx * 2, 15, 40], status="reachable")

        planner = CampaignPlanner(mode="campaign", strategy="llm", min_verified="offline", max_seconds=28800.0)
        planner.record_environment_block(
            make("smelt_iron", {"count": 3}),
            EnvBlocked("半径 64 内无裸露铁矿", missing_predicate="exposed_iron", searched_volume="64", wake_on="chunk_moved"),
            state,
        )
        planner.record_environment_block(
            make("mine_diamond", {"count": 3}),
            EnvBlocked("缺少深层钻石矿", missing_predicate="exposed_diamond", searched_volume="64", wake_on="chunk_moved"),
            state,
        )
        recent = [
            ObjectiveRecord(f"goal_{i}", "env_blocked", "env_blocked", 45.0, f"detail_{i}")
            for i in range(5)
        ]
        cur_obj = Objective(
            goal_id="smelt_iron",
            budget_s=180.0,
            why="补充铁锭制作工具与盔甲以备下界探索",
            source="llm",
            started_at=100.0,
        )
        ctx = build_decision_context(
            state,
            ledger=GOALS,
            environment_blocks=planner.rules._environment_blocks,
            current_objective=cur_obj,
            current_objective_elapsed_s=55.0,
            recent_objectives=recent,
            elapsed_match_s=1200.0,
            max_match_seconds=28800.0,
            reservation_book=planner.reservations,
        )
        rendered = ctx.render()
        self.assertLessEqual(len(rendered), 2000)

    def test_j17_anti_cheat_sanitization_blocks_raw_coords_and_entity_ids(self) -> None:
        state = _make_state(
            health=19,
            food=18,
            extra_status={
                "pos": [2468.5, 81.0, -3579.25],
                "nearest_hostile": {
                    "id": 98765,
                    "entity_id": 98765,
                    "name": "zombie",
                    "pos": [2471, 81, -3575],
                    "dist": 5.5,
                },
            },
            inventory={"items": [{"name": "iron_sword", "count": 1}]},
        )
        memo = memo_for(state)
        memo.record_station("crafting_table", [1234, 67, -8910])
        memo.record_station("furnace", [1234, 67, -8910])
        memo.record_landmark("village", [-4321, 52, 9876])
        memo.record_active_portal([777, 70, -888])
        memo.record_seen("iron_ore", "iron_ore", [1234, 67, -8910], status="reachable")

        ctx = build_decision_context(
            state,
            elapsed_match_s=100.0,
            max_match_seconds=10000.0,
            current_objective=Objective("prep_wood", 180.0, "常规采集", "rule", 0.0),
            current_objective_elapsed_s=12.0,
        )
        rendered = ctx.render()
        landmarks_line = format_world_landmarks(ctx.world_landmarks)
        surfaces = (rendered, ctx.subgoal_summary, landmarks_line)

        forbidden_tokens = (
            "[1234, 67, -8910]",
            "[-4321, 52, 9876]",
            "[777, 70, -888]",
            "[2468.5, 81.0, -3579.25]",
            "[2471, 81, -3575]",
            "1234", "67", "-8910",
            "-4321", "52", "9876",
            "777", "70", "-888",
            "2468", "3579", "2471", "3575",
            "98765",
        )
        for text in surfaces:
            for token in forbidden_tokens:
                self.assertNotIn(token, text, msg=f"Leaked J17 token {token!r} in {text!r}")
            self.assertIsNone(re.search(r"\[\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+", text))

        # Verify expected sanitized Chinese labels still appear
        self.assertIn("工作台=已就绪", landmarks_line)
        self.assertIn("熔炉=已就绪", landmarks_line)
        self.assertIn("村庄=已发现", landmarks_line)
        self.assertIn("传送门=已激活", landmarks_line)

    def test_empty_world_memo_emits_no_landmarks_line(self) -> None:
        state = _make_state(inventory={"items": []})
        ctx = build_decision_context(state)
        self.assertEqual(format_world_landmarks(ctx.world_landmarks), "")
        self.assertNotIn("已知设施与地标：", ctx.render())

    def test_backward_compatibility_without_new_fields_and_custom_subgoal_provider(self) -> None:
        state = _make_state(inventory={"items": [{"name": "iron_ingot", "count": 6, "slot": 0}]})
        # Direct DecisionContext construction omitting world_landmarks and subgoal_summary
        ctx_minimal = DecisionContext(
            remaining_match_s=1000.0,
            elapsed_match_s=0.0,
            day_phase="白天",
            current_objective=None,
            current_objective_elapsed_s=0.0,
            inventory_summary="状态：维度=overworld",
            eligible_goals=(),
            suspended_goals=(),
        )
        self.assertIsNone(ctx_minimal.world_landmarks)
        self.assertEqual(ctx_minimal.subgoal_summary, "")
        rendered_min = ctx_minimal.render()
        self.assertNotIn("已知设施与地标：", rendered_min)
        self.assertNotIn("子目标谓词：", rendered_min)

        # CampaignPlanner with custom subgoal_provider
        planner = CampaignPlanner(
            mode="campaign",
            strategy="llm",
            min_verified="offline",
            subgoal_provider=lambda _st: {"custom_flag": True, "station_ready": "待现场观察"},
        )
        ctx_custom = planner.build_context(state)
        rendered_custom = ctx_custom.render()
        self.assertIn("custom_flag=True", rendered_custom)
        self.assertNotIn("待现场观察", rendered_custom)
        self.assertIsInstance(ctx_custom.subgoal_predicates["station_ready"], dict)


if __name__ == "__main__":
    unittest.main()
