"""INC-001 local-target: lost/switched tools must stop or recover before mining stone.

Tool changes here are explicit fault injections, not Minecraft physics simulation.
The captured incident lost its full-durability wooden pick after the first soil dig.
"""
import unittest

from mcagent.client import ApiError
from mcagent.loop import reset_world_memo
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import drive
from tests.test_loop_soak import RichGridWorld


class ToolChangeWorld(RichGridWorld):
    def __init__(self, *, blocks=None, items=None, after_equip=None, after_dig=None):
        super().__init__(
            blocks=blocks or {(2, 64, 0): "stone", (3, 64, 0): "stone"},
            items=items or {"wooden_pickaxe": 1, "oak_log": 1},
        )
        self.after_equip = after_equip
        self.after_dig = after_dig
        self.dig_tools = []
        self.held = "wooden_pickaxe"
        self.st["held"] = {"name": self.held}

    def inventory(self, timeout=10):
        inv = super().inventory(timeout=timeout)
        inv["held"] = {"name": self.held} if self.held else None
        return inv

    def equip(self, item, slot="hand", *, stop_on=None):
        super().equip(item, slot, stop_on=stop_on)
        self.held = item
        if self.after_equip:
            self.after_equip(self)
        return {"held": {"name": item}}

    def dig(self, pos, collect=True, *, stop_on=None):
        self.dig_tools.append(self.held)
        result = super().dig(pos, collect=collect, stop_on=stop_on)
        if self.after_dig:
            self.after_dig(self, result)
        return result


def lose_pick(world):
    world.items.pop("wooden_pickaxe", None)
    world.held = "oak_log"


class MiningToolsTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_tool_lost_after_soil_dig_stops_before_next_stair_action(self):
        world = ToolChangeWorld(
            blocks={(1, 63, 0): "dirt", (1, 62, 0): "dirt", (2, 62, 0): "stone"},
            after_dig=lambda w, r: lose_pick(w),
        )
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "背包已无镐"):
            drive(make("mine_stone", {"count": 2}), ex, state)
        self.assertEqual(ex.acts, ["dig"])
        self.assertEqual(world.blocks[(2, 62, 0)], "stone")

    def test_tool_lost_after_initial_equip_is_not_used_for_surface_stone(self):
        world = ToolChangeWorld(after_equip=lose_pick)
        world.st["held"] = None
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "背包已无镐"):
            drive(make("mine_stone"), ex, state)
        self.assertEqual(ex.acts, ["equip"])
        self.assertEqual(world.dig_tools, [])

    def test_last_pick_breaks_after_stone_and_stale_status_cannot_authorize_next_dig(self):
        world = ToolChangeWorld(after_dig=lambda w, r: lose_pick(w))
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "背包已无镐"):
            drive(make("mine_stone", {"count": 2}), ex, state)
        self.assertEqual(ex.acts, ["dig"])
        self.assertEqual(state.st["held"]["name"], "wooden_pickaxe")
        self.assertEqual(state.count("cobblestone"), 1)

    def test_backup_pick_is_equipped_when_first_pick_disappears(self):
        world = ToolChangeWorld(
            items={"wooden_pickaxe": 1, "stone_pickaxe": 1},
            after_dig=lambda w, r: (w.items.pop("stone_pickaxe", None), setattr(w, "held", "oak_log")),
        )
        world.held = "stone_pickaxe"
        world.st["held"] = {"name": "stone_pickaxe"}
        ex, state = world.session()
        steps = drive(make("mine_stone", {"count": 2}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["dig", "equip", "dig"])
        self.assertEqual(world.dig_tools, ["stone_pickaxe", "wooden_pickaxe"])
        self.assertEqual(state.count("cobblestone"), 2)

    def test_inventory_held_switch_overrides_stale_status(self):
        world = ToolChangeWorld(after_dig=lambda w, r: setattr(w, "held", "oak_log"))
        ex, state = world.session()
        steps = drive(make("mine_stone", {"count": 2}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["dig", "equip", "dig"])
        self.assertEqual(world.dig_tools, ["wooden_pickaxe", "wooden_pickaxe"])

    def test_equip_acknowledgment_without_confirmed_held_pick_stops(self):
        world = ToolChangeWorld(after_equip=lambda w: setattr(w, "held", "oak_log"))
        world.held = "oak_log"
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "手持未确认"):
            drive(make("mine_stone"), ex, state)
        self.assertEqual(ex.acts, ["equip"])
        self.assertEqual(world.dig_tools, [])

    def test_server_warning_is_yielded_and_stops_repeated_attempts(self):
        def warn(world, result):
            result["warning"] = "injected unsuitable harvest tool"

        world = ToolChangeWorld(after_dig=warn)
        ex, state = world.session()
        skill = make("mine_stone", {"count": 2})
        with self.assertRaisesRegex(Replan, "工具告警"):
            drive(skill, ex, state)
        self.assertEqual(ex.acts, ["dig"])
        self.assertIn("warning", skill.last["result"])

    def test_failed_surface_move_yields_before_another_candidate(self):
        class FailedMoveWorld(ToolChangeWorld):
            def move_to(self, *args, **kwargs):
                if kwargs["pos"] == [5, 64, 0]:
                    self.calls.append(("move_to", kwargs))
                    raise ApiError("path_not_found", "injected navigation failure")
                return super().move_to(*args, **kwargs)

        world = FailedMoveWorld(blocks={(5, 64, 0): "stone", (6, 64, 0): "stone"})
        ex, state = world.session()
        steps = drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, ["move_to", "move_to", "dig"])
        self.assertEqual(steps[0]["error_code"], "path_not_found")

    def test_failed_surface_dig_yields_before_another_candidate(self):
        class FailedDigWorld(ToolChangeWorld):
            def dig(self, pos, **kwargs):
                if pos == [2, 64, 0]:
                    self.calls.append(("dig", {"pos": pos}))
                    raise ApiError("unreachable", "injected dig failure")
                return super().dig(pos, **kwargs)

        world = FailedDigWorld()
        ex, state = world.session()
        steps = drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, ["dig", "dig"])
        self.assertEqual(steps[0]["error_code"], "unreachable")

    def test_failed_stair_move_yields_before_next_stair_dig(self):
        class FailedStairWorld(ToolChangeWorld):
            def move_to(self, *args, **kwargs):
                self.calls.append(("move_to", kwargs))
                raise ApiError("path_not_found", "injected stair navigation failure")

        world = FailedStairWorld(blocks={
            (1, 63, 0): "dirt", (1, 62, 0): "dirt",
            (2, 62, 0): "stone", (2, 61, 0): "stone",
        })
        ex, state = world.session()
        steps = drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, ["dig", "move_to", "dig"])
        self.assertEqual(steps[1]["error_code"], "path_not_found")


if __name__ == "__main__":
    unittest.main()
