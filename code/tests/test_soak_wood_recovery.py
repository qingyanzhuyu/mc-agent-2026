"""ep87 8d24eb84bd: try low trunks before exhausting the budget on canopies.

The first test uses the recorded stance and candidate positions. Its reachable
low trunk is an explicit fault injection: the incident does not establish that
a safe route to that trunk actually existed in the recorded world.
"""
import unittest

from mcagent.client import ApiError
from mcagent.loop import reset_world_memo
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import drive
from tests.test_loop_soak import RichGridWorld


class WoodFailureWorld(RichGridWorld):
    def __init__(self, *, blocks, pos=(0.5, 64, 0.5), items=None, failures=None):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.failures = failures or {}

    def move_to(self, *args, **kwargs):
        target = tuple(kwargs["pos"])
        if target in self.failures:
            self.calls.append(("move_to", {"pos": list(target)}))
            raise ApiError(self.failures[target], "injected wood navigation failure")
        return super().move_to(*args, **kwargs)


class WoodRecoveryTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_ep87_low_trunk_is_tried_before_high_canopies_exhaust_budget(self):
        high = [(-42, 75, 139), (-40, 80, 143), (-40, 81, 142),
                (-41, 81, 141), (-41, 81, 144), (-41, 82, 140),
                (-50, 74, 134), (-49, 78, 149)]
        blocked_low, reachable_low = (-20, 72, 134), (-17, 70, 130)
        blocks = {p: "oak_log" for p in high + [blocked_low, reachable_low]}
        failures = {p: "path_not_found" for p in high + [blocked_low]}
        world = WoodFailureWorld(pos=(-35.7, 69, 140.5), blocks=blocks,
                                 failures=failures, items={"birch_log": 1})
        ex, state = world.session()
        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "move_to", "dig"])
        self.assertEqual(steps[0]["error_code"], "path_not_found")
        self.assertEqual([s["pos"] for s in steps[:2]], [list(blocked_low), list(reachable_low)])
        self.assertEqual(state.count("oak_log"), 1)
        self.assertTrue(all(p in world.blocks for p in high))

    def test_height_priority_precedes_species_without_excluding_canopies(self):
        world = WoodFailureWorld(blocks={(8, 70, 0): "birch_log", (15, 65, 0): "oak_log"},
                                 items={"birch_log": 1})
        ex, state = world.session()
        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual(steps[0]["pos"], [15, 65, 0])
        self.assertEqual(state.count("oak_log"), 1)
        # Height is a preference; a tree on a hill remains a valid fallback.
        world = WoodFailureWorld(blocks={(8, 70, 0): "birch_log"})
        ex, state = world.session()
        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual(steps[0]["pos"], [8, 70, 0])

    def test_failed_move_yields_before_fallback_and_abort_starts_no_new_action(self):
        world = WoodFailureWorld(blocks={(6, 64, 0): "oak_log", (12, 64, 0): "oak_log"},
                                 failures={(6, 64, 0): "move_timeout"})
        ex, state = world.session()
        skill = make("gather_wood", {"count": 1})
        self.assertFalse(skill.step(ex, state))
        self.assertEqual(ex.acts, ["move_to"])
        self.assertEqual(skill.last["error_code"], "move_timeout")
        skill.abort()
        self.assertEqual(ex.acts, ["move_to"])
        self.assertEqual(state.count("oak_log"), 0)

    def test_failed_dig_yields_before_trying_another_log(self):
        class DigFailureWorld(RichGridWorld):
            def dig(self, pos, **kwargs):
                if pos == [2, 64, 0]:
                    self.calls.append(("dig", {"pos": list(pos)}))
                    raise ApiError("unreachable", "injected dig failure")
                return super().dig(pos, **kwargs)

        world = DigFailureWorld(blocks={(2, 64, 0): "oak_log", (3, 64, 0): "oak_log"})
        ex, state = world.session()
        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["dig", "dig"])
        self.assertEqual(steps[0]["error_code"], "unreachable")
        self.assertEqual(state.count("oak_log"), 1)

    def test_all_failed_candidates_keep_error_and_existing_attempt_budget(self):
        blocks = {(x, 64, 0): "oak_log" for x in range(6, 16)}
        world = WoodFailureWorld(blocks=blocks,
                                 failures={p: "path_not_found" for p in blocks})
        ex, state = world.session()
        skill = make("gather_wood", {"count": 1})
        with self.assertRaisesRegex(Replan, "path_not_found"):
            drive(skill, ex, state)
        self.assertEqual(ex.acts, ["move_to"] * 7)
        self.assertEqual(skill.last["error_code"], "path_not_found")
        self.assertEqual(state.count("oak_log"), 0)

    def test_safety_interruption_does_not_start_fallback(self):
        world = WoodFailureWorld(blocks={(6, 64, 0): "oak_log", (12, 64, 0): "oak_log"},
                                 failures={(6, 64, 0): "interrupted"})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "interrupted"):
            drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, ["move_to"])


if __name__ == "__main__":
    unittest.main()
