"""INC 8a936dfcdc: a supported stair must be able to turn at an unsafe floor.

These sparse blocks exercise observed support decisions, not gravity/pathfinding.
The original tape confirms the abandoned stair's next floor was air; it does not
record other directions, so the safe side branch is an explicit regression case.
"""
import unittest

from mcagent.loop import reset_world_memo
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import drive
from tests.test_loop_soak import RichGridWorld


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
