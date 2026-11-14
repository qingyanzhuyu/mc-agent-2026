"""INC 8a936dfcdc: a supported stair must be able to turn at an unsafe floor.

These sparse blocks exercise observed support decisions, not gravity/pathfinding.
The original tape confirms the abandoned stair's next floor was air; it does not
record other directions, so the safe side branch is an explicit regression case.
INC 5c7e937cc1 adds the observed negative-coordinate beach landing and water
floor; any side support added below is synthetic, not evidence of a live route.
"""
import unittest

from mcagent.loop import reset_world_memo
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import drive
from tests.test_loop_soak import RichGridWorld


class RecordedBeachLandingWorld(RichGridWorld):
    """Only replay the recorded move result/status pair; do not simulate gravity."""

    def __init__(self, **kwargs):
        super().__init__(pos=(-9.5, 64, 53.5), **kwargs)
        self.st["held"] = {"name": "wooden_pickaxe"}
        self.probes = []

    def get_block(self, pos):
        self.probes.append(list(pos))
        return super().get_block(pos)

    def move_to(self, *args, **kwargs):
        result = super().move_to(*args, **kwargs)
        if kwargs.get("pos") == [-8.5, 63, 53.5]:
            # ep-0031 seq310 reports the transient height; seq312 settles at y=63.
            self.st["pos"] = [-8.4, 63, 53.5]
            return {"pos": [-8.4, 63.9, 53.5]}
        return result


class MiningStairRecoveryTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_stair_turns_to_supported_branch_instead_of_abandoning_mining(self):
        for unsafe in ("air", "water", "lava"):
            with self.subTest(next_floor=unsafe):
                world = RichGridWorld(blocks={
                    (1, 63, 0): "dirt", (1, 62, 0): "dirt",
                    (2, 61, 0): unsafe,
                    (1, 62, 1): "dirt", (1, 61, 1): "dirt",
                    (1, 61, 2): "stone",
                }, items={"wooden_pickaxe": 1})
                world.st["held"] = {"name": "wooden_pickaxe"}
                ex, state = world.session()
                steps = drive(make("mine_stone", {"count": 1}), ex, state)
                self.assertEqual(state.count("cobblestone"), 1)
                self.assertEqual([s["op"] for s in steps],
                                 ["dig", "move_to", "dig", "move_to", "dig"])
                moves = [c[1]["pos"] for c in world.calls if c[0] == "move_to"]
                self.assertEqual(moves, [[1.5, 63, 0.5], [1.5, 62, 1.5]])
                self.assertEqual(world.blocks[(1, 62, 0)], "dirt")
                self.assertFalse(any(c[0] == "dig" and c[1]["pos"] == [2, 62, 0]
                                     for c in world.calls))

    def test_ep0031_water_floor_turns_from_observed_beach_landing(self):
        world = RecordedBeachLandingWorld(blocks={
            # Recorded prefix: one sand stair, deep exposed stone, then water.
            (-9, 63, 53): "sand", (-9, 62, 53): "sand",
            (-8, 61, 53): "water", (-6, 60, 51): "stone",
            # Synthetic extension: the tape did not observe this side branch.
            (-9, 62, 54): "sand", (-9, 61, 54): "sand",
            (-9, 61, 55): "stone",
        }, items={"wooden_pickaxe": 1, "sand": 5})
        ex, state = world.session()
        steps = drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps],
                         ["dig", "move_to", "dig", "move_to", "dig"])
        self.assertEqual(steps[1]["result"]["pos"], [-8.4, 63.9, 53.5])
        moves = [args["pos"] for op, args in world.calls if op == "move_to"]
        self.assertEqual(moves, [[-8.5, 63, 53.5], [-8.5, 62, 54.5]])
        self.assertIn([-8, 61, 53], world.probes)
        self.assertEqual(world.blocks[(-8, 61, 53)], "water")
        self.assertEqual(world.blocks[(-9, 62, 53)], "sand")
        self.assertEqual(world.blocks[(-6, 60, 51)], "stone")
        self.assertEqual(state.count("cobblestone"), 1)

    def test_ep0031_observed_prefix_alone_does_not_imply_safe_escape(self):
        world = RecordedBeachLandingWorld(blocks={
            (-9, 63, 53): "sand", (-9, 62, 53): "sand",
            (-8, 61, 53): "water", (-6, 60, 51): "stone",
        }, items={"wooden_pickaxe": 1, "sand": 5})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "附近未找到可安全挖掘"):
            drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, ["dig", "move_to"])
        self.assertEqual(state.pos, [-8.4, 63, 53.5])
        self.assertIn([-8, 61, 53], world.probes)
        self.assertEqual(world.blocks[(-9, 62, 53)], "sand")
        self.assertEqual(state.count("cobblestone"), 0)

    def test_stair_without_supported_branch_still_replans(self):
        world = RichGridWorld(blocks={
            (1, 63, 0): "dirt", (1, 62, 0): "dirt",
            (0, 62, 0): "dirt", (0, 61, 0): "dirt",
        }, items={"wooden_pickaxe": 1})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "附近未找到可安全挖掘"):
            drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, ["equip", "dig", "move_to"])
        self.assertEqual(world.blocks[(1, 62, 0)], "dirt")
        self.assertEqual(world.blocks[(0, 62, 0)], "dirt")

    def test_turns_do_not_reset_six_step_descent_limit(self):
        columns = [(1, 0), (1, 1), (2, 1), (2, 2), (3, 2), (3, 3), (4, 3)]
        blocks = {}
        for step, (x, z) in enumerate(columns, start=1):
            blocks[(x, 64 - step, z)] = "dirt"
            blocks[(x, 63 - step, z)] = "dirt"
        world = RichGridWorld(blocks=blocks, items={"wooden_pickaxe": 1})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "附近未找到可安全挖掘"):
            drive(make("mine_stone", {"count": 1}), ex, state)
        moves = [c[1]["pos"] for c in world.calls if c[0] == "move_to"]
        self.assertEqual(len(moves), 6)
        self.assertEqual(moves[-1], [3.5, 58, 3.5])
        self.assertEqual(world.blocks[(4, 57, 3)], "dirt")


if __name__ == "__main__":
    unittest.main()
