"""INC db5161b4a7: never descend below an observed falling roof.

ep33 seq299 cleared sandstone at (-10, 61, 68); seq313 found sand there
while the player remained one block below and repeatedly took hurt events.
The roof and block updates below are explicit fault injections, not simulated
gravity or a claim that an unobserved escape route existed in that episode.
"""
import unittest

from mcagent.loop import reset_world_memo
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import drive
from tests.test_loop_soak import RichGridWorld


class MiningCeilingTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_falling_roof_is_rejected_before_dig_and_safe_branch_is_used(self):
        for roof in ("sand", "red_sand", "gravel"):
            with self.subTest(roof=roof):
                world = RichGridWorld(blocks={
                    (1, 66, 0): roof, (1, 63, 0): "sandstone",
                    (1, 62, 0): "sandstone",
                    (0, 63, 1): "dirt", (0, 62, 1): "dirt",
                    (0, 62, 2): "stone",
                }, items={"wooden_pickaxe": 1})
                world.st["held"] = {"name": "wooden_pickaxe"}
                ex, state = world.session()
                drive(make("mine_stone", {"count": 1}), ex, state)
                self.assertEqual(state.count("cobblestone"), 1)
                self.assertEqual(world.blocks.get((1, 63, 0)), "sandstone")
                moves = [args["pos"] for op, args in world.calls if op == "move_to"]
                self.assertEqual(moves, [[0.5, 63, 1.5]])

    def test_roof_is_checked_at_each_descent_step_before_turning(self):
        world = RichGridWorld(blocks={
            (1, 63, 0): "dirt", (1, 62, 0): "dirt",
            (2, 65, 0): "sand", (2, 62, 0): "sandstone",
            (2, 61, 0): "sandstone",
            (1, 62, 1): "dirt", (1, 61, 1): "dirt",
            (1, 61, 2): "stone",
        }, items={"wooden_pickaxe": 1})
        world.st["held"] = {"name": "wooden_pickaxe"}
        ex, state = world.session()
        drive(make("mine_stone", {"count": 1}), ex, state)
        moves = [args["pos"] for op, args in world.calls if op == "move_to"]
        self.assertEqual(moves, [[1.5, 63, 0.5], [1.5, 62, 1.5]])
        self.assertEqual(world.blocks[(2, 62, 0)], "sandstone")

    def test_refilled_clearance_stops_before_moving_into_column(self):
        class RefilledWorld(RichGridWorld):
            def dig(self, pos, **kwargs):
                result = super().dig(pos, **kwargs)
                if pos == [1, 63, 0]:
                    self.blocks[(1, 64, 0)] = "sand"
                return result

        world = RefilledWorld(blocks={
            (1, 63, 0): "sandstone", (1, 62, 0): "sandstone",
        }, items={"wooden_pickaxe": 1})
        world.st["held"] = {"name": "wooden_pickaxe"}
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "净空.*变化"):
            drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, ["dig"])

    def test_ep0033_dig_rechecks_recorded_position_before_removing_support(self):
        class MovedWorld(RichGridWorld):
            def dig(self, pos, **kwargs):
                result = super().dig(pos, **kwargs)
                if pos == [-12, 64, 68]:
                    # seq240 monitor status during seq241's completed sand dig.
                    self.st["pos"] = [-12, 64.2, 68.5]
                return result

        world = MovedWorld(pos=(-12.4, 63.9, 68.5), blocks={
            (-12, 64, 68): "sand", (-12, 63, 68): "sand",
            (-12, 62, 68): "sandstone", (-12, 61, 68): "sandstone",
            # The tape did not observe y=65; this clear roof is a fixture input.
        }, items={"wooden_pickaxe": 1})
        world.st["held"] = {"name": "wooden_pickaxe"}
        ex, state = world.session()
        # 站位变更后重新观察；此夹具没有安全侧路，仍必须保留脚下并退出。
        with self.assertRaisesRegex(Replan, "附近未找到可安全挖掘"):
            drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, ["dig"])
        self.assertEqual(world.blocks[(-12, 63, 68)], "sand")


if __name__ == "__main__":
    unittest.main()
