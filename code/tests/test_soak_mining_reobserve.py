"""INC 5c7e937cc1: re-observe mining after collection changes the stance.

The ep39 status/dig prefix and ep41 starting stance/nearby stone are recorded.
Safe continuations are explicit fixture inputs: neither incident tape records
enough blocks to establish that a safe route existed in the original world.
"""
import unittest

from mcagent.client import ApiError
from mcagent.loop import reset_world_memo
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import drive
from tests.test_loop_soak import RichGridWorld


class MiningReobserveTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def beach_world(self, extension=None):
        class CollectedWorld(RichGridWorld):
            def move_to(self, *args, **kwargs):
                result = super().move_to(*args, **kwargs)
                if kwargs.get("pos") == [-6.5, 65, 50.5]:
                    self.st["pos"] = [-6.4, 65, 50.5]
                    return {"pos": [-6.4, 65.9, 50.5]}
                return result

            def dig(self, pos, **kwargs):
                result = super().dig(pos, **kwargs)
                if pos == [-6, 66, 50]:
                    # ep39 seq305 followed by seq309; floor(-5.5) is -6.
                    self.st["pos"] = [-5.5, 66, 50.5]
                return result

        blocks = {(-7, 65, 50): "sand", (-7, 64, 50): "sand",
                  (-6, 66, 50): "sand", (-6, 65, 50): "sand",
                  (-6, 63, 50): "sandstone"}
        blocks.update(extension or {})
        world = CollectedWorld(pos=(-7.5, 66, 50.5), blocks=blocks,
                               items={"wooden_pickaxe": 1})
        world.st["held"] = {"name": "wooden_pickaxe"}
        return world

    def test_ep39_collection_rebuilds_from_new_stance_and_preserves_support(self):
        world = self.beach_world({
            (-5, 65, 50): "dirt", (-5, 64, 50): "dirt",
            (-4, 64, 50): "dirt", (-4, 63, 50): "dirt",
            (-4, 63, 51): "stone",
        })
        ex, state = world.session()
        drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual(state.count("cobblestone"), 1)
        self.assertEqual(world.blocks[(-6, 65, 50)], "sand")
        moves = [args["pos"] for op, args in world.calls if op == "move_to"]
        self.assertIn([-4.5, 65, 50.5], moves)
        self.assertNotIn([-5.5, 64, 50.5], moves)

    def test_ep39_without_observed_support_still_stops_without_digging_floor(self):
        world = self.beach_world()
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "附近未找到可安全挖掘"):
            drive(make("mine_stone", {"count": 1}), ex, state, max_steps=12)
        self.assertEqual(ex.acts, ["dig", "move_to", "dig"])
        self.assertEqual(world.blocks[(-6, 65, 50)], "sand")
        self.assertEqual(state.pos, [-5.5, 66, 50.5])

    def test_repeated_collection_rebases_share_the_six_column_limit(self):
        class MovingCollectionWorld(RichGridWorld):
            def dig(self, pos, **kwargs):
                result = super().dig(pos, **kwargs)
                self.st["pos"] = [pos[0] + 0.5, pos[1] + 1, pos[2] + 0.5]
                return result

        blocks = {}
        for x in range(1, 9):
            top = 63 + 2 * x
            blocks[(x, top, 0)] = "dirt"
            blocks[(x, top - 1, 0)] = "dirt"
            blocks[(x, top - 3, 0)] = "dirt"
        world = MovingCollectionWorld(blocks=blocks, items={"wooden_pickaxe": 1})
        world.st["held"] = {"name": "wooden_pickaxe"}
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "附近未找到可安全挖掘"):
            drive(make("mine_stone", {"count": 1}), ex, state, max_steps=12)
        self.assertEqual(ex.acts, ["dig"] * 6)
        self.assertEqual(world.blocks[(7, 77, 0)], "dirt")
        self.assertEqual(world.blocks[(6, 74, 0)], "dirt")

    def test_ep41_shallow_stone_requires_observed_safe_adjacent_stance(self):
        class ObservedWorld(RichGridWorld):
            def get_block(self, pos):
                self.calls.append(("get_block", {"pos": list(pos)}))
                return super().get_block(pos)

        # Only the stone and initial stance are from ep41. The neighboring
        # support is a synthetic continuation that must be observed before use.
        world = ObservedWorld(pos=(-2.3, 65, 30.5), blocks={
            (-4, 63, 33): "stone", (-3, 63, 33): "sandstone",
        }, items={"wooden_pickaxe": 1})
        world.st["held"] = {"name": "wooden_pickaxe"}
        ex, state = world.session()
        drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, ["move_to", "dig"])
        self.assertEqual(state.count("cobblestone"), 1)
        move_index = next(i for i, (op, _) in enumerate(world.calls) if op == "move_to")
        observed = [args["pos"] for op, args in world.calls[:move_index] if op == "get_block"]
        for pos in ([-3, 63, 33], [-3, 64, 33], [-3, 65, 33]):
            self.assertIn(pos, observed)
        self.assertEqual(world.blocks[(-3, 63, 33)], "sandstone")

    def test_shallow_stone_approach_rejects_void_fluid_and_falling_roof(self):
        for floor, roof in (("air", "air"), ("water", "air"),
                            ("lava", "air"), ("sandstone", "sand")):
            with self.subTest(floor=floor, roof=roof):
                world = RichGridWorld(pos=(-2.3, 65, 30.5), blocks={
                    (-4, 63, 33): "stone", (-3, 63, 33): floor,
                    (-3, 66, 33): roof,
                }, items={"wooden_pickaxe": 1})
                world.st["held"] = {"name": "wooden_pickaxe"}
                ex, state = world.session()
                with self.assertRaisesRegex(Replan, "附近未找到可安全挖掘"):
                    drive(make("mine_stone", {"count": 1}), ex, state)
                self.assertEqual(ex.acts, [])
                self.assertEqual(world.blocks[(-4, 63, 33)], "stone")

    def test_hurt_during_rebuilt_stair_propagates_without_followup_actions(self):
        world = self.beach_world({(-5, 65, 50): "dirt", (-5, 64, 50): "dirt"})
        original_dig = world.dig

        def hurt(pos, **kwargs):
            if pos == [-5, 65, 50]:
                world.st["health"] = 19
                world.calls.append(("dig", {"pos": list(pos)}))
                raise ApiError("interrupted", "hurt", {"interrupted_by": "hurt"})
            return original_dig(pos, **kwargs)

        world.dig = hurt
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "interrupted"):
            drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, ["dig", "move_to", "dig", "dig"])
        self.assertEqual(world.blocks[(-6, 65, 50)], "sand")
        self.assertEqual(world.blocks[(-5, 65, 50)], "dirt")

    def test_failed_approaches_yield_and_stop_after_three_moves(self):
        class UnreachableWorld(RichGridWorld):
            def move_to(self, *args, **kwargs):
                self.calls.append(("move_to", {"pos": kwargs["pos"]}))
                raise ApiError("path_not_found", "no approach")

        world = UnreachableWorld(pos=(0.5, 65, 0.5), blocks={
            (3, 63, 0): "stone", (2, 63, 0): "sandstone",
            (4, 63, 0): "sandstone", (3, 63, 1): "sandstone",
            (3, 63, -1): "sandstone",
        }, items={"wooden_pickaxe": 1})
        world.st["held"] = {"name": "wooden_pickaxe"}
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "附近未找到可安全挖掘"):
            drive(make("mine_stone", {"count": 1}), ex, state, max_steps=8)
        self.assertEqual(ex.acts, ["move_to"] * 3)
        self.assertEqual(world.blocks[(3, 63, 0)], "stone")

    def test_approach_arriving_above_target_does_not_remove_own_support(self):
        class ImpreciseWorld(RichGridWorld):
            def move_to(self, *args, **kwargs):
                self.calls.append(("move_to", {"pos": kwargs["pos"]}))
                self.st["pos"] = [3.5, 64, 0.5]
                return {"pos": list(self.st["pos"])}

        world = ImpreciseWorld(pos=(0.5, 65, 0.5), blocks={
            (3, 63, 0): "stone", (2, 63, 0): "sandstone",
        }, items={"wooden_pickaxe": 1})
        world.st["held"] = {"name": "wooden_pickaxe"}
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "附近未找到可安全挖掘"):
            drive(make("mine_stone", {"count": 1}), ex, state, max_steps=8)
        self.assertEqual(ex.acts, ["move_to"])
        self.assertEqual(world.blocks[(3, 63, 0)], "stone")

    def test_surface_move_rechecks_stance_before_digging_selected_target(self):
        class AboveTargetWorld(RichGridWorld):
            def move_to(self, *args, **kwargs):
                self.calls.append(("move_to", {"pos": kwargs["pos"]}))
                self.st["pos"] = [6.5, 65, 0.5]
                return {"pos": list(self.st["pos"])}

        world = AboveTargetWorld(blocks={(6, 64, 0): "stone"},
                                 items={"wooden_pickaxe": 1})
        world.st["held"] = {"name": "wooden_pickaxe"}
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "附近未找到可安全挖掘"):
            drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, ["move_to"])
        self.assertEqual(world.blocks[(6, 64, 0)], "stone")

    def test_hurt_during_rebuilt_stair_move_is_not_treated_as_navigation_failure(self):
        world = self.beach_world({(-5, 65, 50): "dirt", (-5, 64, 50): "dirt"})
        original_move = world.move_to

        def hurt(*args, **kwargs):
            if kwargs.get("pos") == [-4.5, 65, 50.5]:
                world.st["health"] = 19
                world.calls.append(("move_to", {"pos": kwargs["pos"]}))
                raise ApiError("interrupted", "hurt", {"interrupted_by": "hurt"})
            return original_move(*args, **kwargs)

        world.move_to = hurt
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "interrupted"):
            drive(make("mine_stone", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, ["dig", "move_to", "dig", "dig", "move_to"])
        self.assertEqual(world.blocks[(-6, 65, 50)], "sand")


if __name__ == "__main__":
    unittest.main()
