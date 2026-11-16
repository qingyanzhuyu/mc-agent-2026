"""P5 tests with explicit inventories, block snapshots and dimension-change receipts."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.runtime import EnvBlocked, Replan, State
from mcagent.skills import REGISTRY
from mcagent.skills.nether import BuildAndEnterPortal, MineObsidian
from mcagent.skills.nether_portal import Frame
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import GridWorld, drive

_PICK = "diamond_pickaxe"
_FRAME = Frame((-1, 64, 1), "x")
_ACTIVE = Frame((-1, 63, 1), "x")


class NetherWorld(ScriptedWorld):
    def __init__(self, *, items=None, extra=None, observed=()):
        ground = {(x, 63, z): "stone" for x in range(-4, 9) for z in range(-4, 9)}
        ground.update(extra or {})
        super().__init__(blocks=ground, items=items or {})
        self.st["dimension"] = "overworld"
        self.observed = tuple(observed)
        self.held = None
        self.unloaded = set()
        self.properties = {}

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None):
        self.calls.append(("find_blocks", {"types": types, "radius": radius}))
        return {"blocks": [{"name": self.blocks[pos], "pos": list(pos)} for pos in self.observed
                           if self.blocks.get(pos) in types][:limit]}

    def get_block(self, pos):
        pos = tuple(pos)
        if "nether" in self.st["dimension"]:
            self.calls.append(("destination_block", {"pos": pos}))
        if pos in self.unloaded:
            raise ApiError("not_loaded", "fixture unloaded")
        result = super().get_block(pos)
        return {**result, "properties": self.properties.get(pos, {})}

    def inventory(self, timeout=10):
        inv = super().inventory(timeout)
        for slot in inv["items"]:
            if slot["name"] in (_PICK, "netherite_pickaxe"):
                slot["durability"] = [15, 100]
        inv["held"] = next((it for it in inv["items"] if it["name"] == self.held), None)
        return inv


def _equip(world, *, item, slot, **kwargs):
    world.held = item
    return {"held": world.inventory()["held"]}


def _dig(world, *, pos, collect, **kwargs):
    return GridWorld.dig(world, pos, collect=collect, **kwargs)


def _place(world, *, item, pos, **kwargs):
    x, y, z = pos
    adjacent = ((x + 1, y, z), (x - 1, y, z), (x, y + 1, z), (x, y - 1, z),
                (x, y, z + 1), (x, y, z - 1))
    if not any(world.blocks.get(p) in {"stone", "obsidian", "cobblestone"} for p in adjacent):
        raise AssertionError(f"fixture 未安排 {pos} 的放置贴面")
    return GridWorld.place(world, item, pos, **kwargs)


def _activate(frame):
    def respond(world, *, pos, item, **kwargs):
        if item != "flint_and_steel" or tuple(pos) not in frame.ring[:2]:
            raise AssertionError("fixture 点火目标不在已观察的底框")
        if not all(world.blocks.get(p) == "obsidian" for p in frame.ring):
            raise AssertionError("fixture 点火前框不完整")
        for cell in frame.inside:
            world.blocks[cell] = "nether_portal"
        return {"changed": True}
    return respond


def _arrive(*, narrow=False, on_fire=False):
    def respond(world, *, pos, **kwargs):
        world.st.update(dimension="minecraft:the_nether", pos=[80.5, 50, -30.5],
                        on_fire=on_fire, sneaking=False)
        world.blocks = {(80, 49, -31): "obsidian", (80, 50, -31): "nether_portal",
                        (80, 51, -31): "nether_portal"}
        if not narrow:
            world.blocks[81, 49, -31] = "netherrack"
        return {"dimension_changed": {"from": "overworld", "to": "the_nether"}, "pos": world.st["pos"]}
    return respond


def _active_world():
    world = NetherWorld(extra={
        **{pos: "obsidian" for pos in _ACTIVE.ring},
        **{pos: "nether_portal" for pos in _ACTIVE.inside},
    }, observed=_ACTIVE.inside)
    world.properties.update({pos: {"axis": "x"} for pos in _ACTIVE.inside})
    return world


class NetherContractTests(unittest.TestCase):
    def test_registers_only_offline_l3_and_blocks_unknown_world(self):
        self.assertIs(REGISTRY["mine_obsidian"], MineObsidian)
        self.assertIs(REGISTRY["build_and_enter_portal"], BuildAndEnterPortal)
        self.assertNotIn("build_pattern", REGISTRY)
        self.assertEqual({MineObsidian.verified, BuildAndEnterPortal.verified}, {"offline"})
        self.assertTrue(MineObsidian.blocked(State()))
        self.assertTrue(BuildAndEnterPortal.blocked(State()))

    def test_materials_are_observed_route_candidates(self):
        world = NetherWorld(items={_PICK: 1, "water_bucket": 1, "obsidian": 4,
                                   "cobblestone": 1, "flint_and_steel": 1})
        _, state = world.session()
        self.assertEqual(MineObsidian.material_plan({"count": 4}, state), ())
        self.assertEqual(MineObsidian.material_plan({"count": 5}, state),
                         ((_PICK, 1, "borrow"), ("water_bucket", 1, "borrow")))
        self.assertEqual(BuildAndEnterPortal.material_plan({}, state),
                         (("obsidian", 4, "consume"), ("cobblestone", 1, "borrow"),
                          ("flint_and_steel", 1, "borrow")))
        self.assertEqual(BuildAndEnterPortal.material_plan({"prepare_flint": True}, state), ())


class ObsidianTests(unittest.TestCase):
    def run_skill(self, world, *, count=1):
        ex, state = world.session()
        return drive(MineObsidian(count=count), ex, state), ex, state

    def test_holds_enough_skips_observations_and_actions(self):
        world = NetherWorld(items={"obsidian": 2})
        steps, ex, _ = self.run_skill(world, count=2)
        self.assertEqual(steps, [])
        self.assertEqual(ex.acts, [])
        self.assertEqual(world.calls, [])

    def test_takes_only_shortfall_from_observed_chest_without_pick(self):
        box = (1, 64, 1)
        world = NetherWorld(items={"obsidian": 1}, extra={box: "chest"}, observed=(box,))

        def take(world, *, target, ops, **kwargs):
            self.assertEqual(target, {"pos": list(box)})
            self.assertEqual(ops, [{"op": "take", "from": "container", "item": "obsidian", "count": 1}])
            world.items["obsidian"] += 1
            return {"done": 1}

        world.queue("window_view",
                    {"window": "chest", "sections": {"container": [{"name": "obsidian", "count": 1}]}},
                    {"window": "chest", "sections": {"container": []}})
        world.queue("window_act", take)
        steps, _, state = self.run_skill(world, count=2)
        self.assertEqual([step["op"] for step in steps], ["window_view", "window_act", "window_view"])
        self.assertEqual(state.count("obsidian"), 2)
        world.assert_exhausted()

    def test_false_container_success_does_not_confirm_loot(self):
        box = (1, 64, 1)
        world = NetherWorld(extra={box: "chest"}, observed=(box,))
        world.queue("window_view",
                    {"window": "chest", "sections": {"container": [{"name": "obsidian", "count": 1}]}})
        world.queue("window_act", {"done": 1})
        with self.assertRaisesRegex(Replan, "背包未确认收到"):
            self.run_skill(world)
        world.assert_exhausted()

    def test_dry_diamond_pick_harvest_checks_inventory_and_changed_block(self):
        ore = (2, 64, 0)
        world = NetherWorld(items={_PICK: 1}, extra={ore: "obsidian"}, observed=(ore,))
        world.queue("equip", _equip)
        world.queue("dig", _dig)
        steps, _, state = self.run_skill(world)
        self.assertEqual([step["op"] for step in steps], ["equip", "dig"])
        self.assertEqual(state.count("obsidian"), 1)
        self.assertEqual(world.get_block(ore)["name"], "air")
        world.assert_exhausted()

    def test_ore_present_without_required_pick_replans(self):
        ore = (2, 64, 0)
        world = NetherWorld(extra={ore: "obsidian"}, observed=(ore,))
        with self.assertRaisesRegex(Replan, "钻石镐"):
            self.run_skill(world)
        self.assertNotIn("dig", [op for op, _ in world.calls])

    def test_no_ordinary_ore_is_bounded_environment_failure(self):
        ore = (2, 64, 0)
        world = NetherWorld(items={_PICK: 1}, extra={ore: "crying_obsidian"}, observed=(ore,))
        with self.assertRaises(EnvBlocked) as caught:
            self.run_skill(world)
        self.assertEqual(caught.exception.missing_predicate, "safe_obsidian_source")
        self.assertEqual(caught.exception.wake_on, "chunk_moved")
        self.assertIn("16 格", caught.exception.searched_volume)

    def test_lava_below_or_unloaded_neighbor_prevents_dig(self):
        ore = (2, 64, 0)
        for other in ((2, 63, 0), (3, 64, 0)):
            with self.subTest(other=other):
                world = NetherWorld(items={_PICK: 1}, extra={ore: "obsidian"}, observed=(ore,))
                if other[1] == 63:
                    world.blocks[other] = "lava"
                    world.blocks[2, 62, 0] = "lava"
                else:
                    world.unloaded.add(other)
                with self.assertRaises(EnvBlocked):
                    self.run_skill(world)
                self.assertNotIn("dig", [op for op, _ in world.calls])

    def test_fluid_cooling_needs_bucket_and_block_evidence(self):
        ore = (3, 64, 1)

        def pour(cools):
            def respond(world, *, pos, item, **kwargs):
                self.assertEqual(item, "water_bucket")
                self.assertEqual(pos, [2, 63, 1])
                world.items["water_bucket"] -= 1
                world.items["bucket"] = 1
                world.blocks[2, 64, 1] = "water"
                if cools:
                    world.blocks[3, 63, 1] = "obsidian"
                return {"changed": True}
            return respond

        for cools in (False, True):
            with self.subTest(cools=cools):
                world = NetherWorld(items={_PICK: 1, "water_bucket": 1},
                                    extra={ore: "obsidian", (3, 63, 1): "lava"}, observed=(ore,))
                world.queue("interact", pour(cools))
                if cools:
                    world.queue("equip", _equip)
                    world.queue("dig", _dig)
                    steps, _, state = self.run_skill(world)
                    self.assertEqual([step["op"] for step in steps], ["interact", "equip", "dig"])
                    self.assertEqual(state.count("obsidian"), 1)
                else:
                    with self.assertRaises(EnvBlocked):
                        self.run_skill(world)
                    self.assertNotIn("dig", [op for op, _ in world.calls])
                world.assert_exhausted()

    def test_false_dig_success_without_inventory_or_block_change(self):
        ore = (2, 64, 0)
        world = NetherWorld(items={_PICK: 1}, extra={ore: "obsidian"}, observed=(ore,))
        world.queue("equip", _equip)
        world.queue("dig", {"dug": "obsidian", "gained": {"obsidian": 1}})
        with self.assertRaisesRegex(Replan, "未同时确认"):
            self.run_skill(world)
        world.assert_exhausted()

    def test_abort_after_equip_does_not_dig(self):
        ore = (2, 64, 0)
        world = NetherWorld(items={_PICK: 1}, extra={ore: "obsidian"}, observed=(ore,))
        world.queue("equip", _equip)
        ex, state = world.session()
        skill = MineObsidian()
        self.assertFalse(skill.step(ex, state))
        self.assertEqual(skill.last["op"], "equip")
        skill.abort()
        self.assertEqual(ex.acts, ["equip"])
        self.assertEqual(world.get_block(ore)["name"], "obsidian")
        world.assert_exhausted()


class PortalTests(unittest.TestCase):
    def run_skill(self, world, **kwargs):
        ex, state = world.session()
        return drive(BuildAndEnterPortal(**kwargs), ex, state), ex, state

    def test_active_gate_no_materials_and_stops_before_landing_reads(self):
        world = _active_world()
        world.queue("move_to", _arrive())
        world.queue("advancements", {"advancements": [
            {"id": "minecraft:story/enter_the_nether", "status": "completed"}]})
        steps, _, _ = self.run_skill(world)
        self.assertEqual([step["op"] for step in steps], ["move_to", "stop", "portal_landing"])
        ops = [op for op, _ in world.calls]
        self.assertLess(ops.index("stop"), ops.index("destination_block"))
        self.assertTrue(steps[-1]["advancement_confirmed"])
        self.assertEqual(steps[-1]["result"]["pos"], [80.5, 50, -30.5])
        self.assertEqual(ops.count("move_to"), 1)
        world.assert_exhausted()

    def test_repair_only_missing_obsidian_and_reuse_observed_frame(self):
        missing = {_FRAME.at(0, 2), _FRAME.at(3, 2)}
        repair = {p: "obsidian" for p in _FRAME.ring if p not in missing}
        world = NetherWorld(items={"obsidian": 2, "flint_and_steel": 1}, extra=repair, observed=repair)
        world.queue("place", _place, _place)
        world.queue("interact", _activate(_FRAME))
        world.queue("move_to", _arrive())
        world.queue("advancements", {"advancements": []})
        steps, _, state = self.run_skill(world)
        self.assertEqual([step["op"] for step in steps], ["place", "place", "interact",
                                                          "move_to", "stop", "portal_landing"])
        self.assertEqual({tuple(step["pos"]) for step in steps if step["op"] == "place"}, missing)
        self.assertEqual(state.count("obsidian"), 0)
        self.assertFalse(steps[-1]["advancement_confirmed"])
        world.assert_exhausted()

    def test_new_gate_uses_ten_obsidian_and_reclaims_roof_support(self):
        world = NetherWorld(items={"obsidian": 10, "flint_and_steel": 1, "cobblestone": 1})
        world.queue("place", *([_place] * 13))
        world.queue("dig", _dig, _dig, _dig)
        world.queue("interact", _activate(_FRAME))
        world.queue("move_to", _arrive())
        world.queue("advancements", {"advancements": []})
        steps, _, state = self.run_skill(world)
        placed = [tuple(step["pos"]) for step in steps if step["op"] == "place" and step["item"] == "obsidian"]
        self.assertEqual(set(placed), set(_FRAME.ring))
        self.assertEqual(len(placed), 10)
        self.assertEqual([s["op"] for s in steps[-4:]], ["interact", "move_to", "stop", "portal_landing"])
        self.assertEqual([s["op"] for s in steps].count("dig"), 3)
        self.assertEqual(state.count("obsidian"), 0)
        self.assertEqual(state.count("cobblestone"), 1)
        world.assert_exhausted()

    def test_new_gate_needs_actual_ten_obsidian(self):
        world = NetherWorld(items={"obsidian": 9, "flint_and_steel": 1, "cobblestone": 1})
        with self.assertRaisesRegex(Replan, "缺普通黑曜石"):
            self.run_skill(world)
        self.assertNotIn("place", [op for op, _ in world.calls])

    def test_no_safe_ground_for_build_is_environment_failure(self):
        world = NetherWorld(items={"obsidian": 10, "flint_and_steel": 1, "cobblestone": 1})
        world.blocks = {(0, 63, 0): "stone"}
        with self.assertRaises(EnvBlocked) as caught:
            self.run_skill(world)
        self.assertEqual(caught.exception.missing_predicate, "safe_portal_site")
        self.assertEqual(caught.exception.wake_on, "chunk_moved")
        self.assertIn("16 格", caught.exception.searched_volume)

    def test_false_ignition_stops_before_entry(self):
        frame = {pos: "obsidian" for pos in _FRAME.ring}
        world = NetherWorld(items={"flint_and_steel": 1}, extra=frame, observed=frame)
        world.queue("interact", {"changed": True})
        with self.assertRaisesRegex(Replan, "未产生"):
            self.run_skill(world)
        self.assertNotIn("move_to", [op for op, _ in world.calls])
        world.assert_exhausted()

    def test_abort_after_portal_step_stops_and_never_trusts_receipt(self):
        world = _active_world()
        world.queue("move_to", _arrive())
        ex, state = world.session()
        skill = BuildAndEnterPortal()
        self.assertFalse(skill.step(ex, state))
        self.assertEqual(skill.last["op"], "move_to")
        skill.abort()
        self.assertEqual([op for op, _ in world.calls].count("stop"), 1)
        self.assertNotIn("destination_block", [op for op, _ in world.calls])
        world.assert_exhausted()

    def test_portal_still_in_overworld_polls_at_most_twelve_times(self):
        world = _active_world()
        with patch("mcagent.skills.nether_portal.time.sleep") as wait:
            with self.assertRaises(EnvBlocked) as caught:
                self.run_skill(world)
        self.assertEqual(caught.exception.missing_predicate, "nether_transition")
        self.assertEqual(caught.exception.wake_on, "chunk_moved")
        self.assertIn("12 次", caught.exception.searched_volume)
        self.assertEqual(wait.call_count, 11)
        self.assertEqual([op for op, _ in world.calls].count("stop"), 1)

    def test_no_further_motion_after_narrow_landing_or_burning(self):
        for case in ({"narrow": True}, {"on_fire": True}):
            with self.subTest(case=case):
                world = _active_world()
                world.queue("move_to", _arrive(**case))
                with self.assertRaises(Replan):
                    self.run_skill(world)
                ops = [op for op, _ in world.calls]
                self.assertEqual(ops.count("move_to"), 1)
                self.assertEqual(ops.count("stop"), 1)
                world.assert_exhausted()

    def test_currently_in_nether_at_safe_landing_uses_no_action(self):
        world = NetherWorld()
        _arrive()(world, pos=None)
        steps, ex, _ = self.run_skill(world)
        self.assertEqual(steps, [])
        self.assertEqual(ex.acts, [])

    def test_flint_preparation_reads_gravel_change_and_pickup(self):
        pos = (2, 64, 1)
        world = NetherWorld(extra={pos: "gravel"}, observed=(pos,))

        def digs_flint(world, *, pos, **kwargs):
            del world.blocks[tuple(pos)]
            world.items["flint"] = 1
            return {"dug": "gravel"}

        world.queue("dig", digs_flint)
        steps, _, state = self.run_skill(world, prepare_flint=True)
        self.assertEqual([step["purpose"] for step in steps], ["gather_flint"])
        self.assertEqual(state.count("flint"), 1)
        world.assert_exhausted()

    def test_flint_missing_no_gravel_is_environment_failure(self):
        world = NetherWorld()
        with self.assertRaises(EnvBlocked) as caught:
            self.run_skill(world, prepare_flint=True)
        self.assertEqual(caught.exception.missing_predicate, "accessible_flint")
        self.assertEqual(caught.exception.wake_on, "chunk_moved")
        self.assertIn("5 处", caught.exception.searched_volume)
