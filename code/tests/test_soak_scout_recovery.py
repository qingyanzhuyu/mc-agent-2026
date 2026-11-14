"""local-target INC-002: scout changes candidates after an observed navigation failure."""
import unittest

from mcagent.client import ApiError
from mcagent.loop import reset_world_memo
from mcagent.loop.starter_skills import VISITED_ANCHORS
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import drive
from tests.test_loop_soak import RichGridWorld


class ScoutFailureWorld(RichGridWorld):
    def __init__(self, *, blocks=None, failures=None):
        super().__init__(blocks=blocks or {(18, 63, 0): "grass_block", (10, 63, 0): "grass_block"})
        self.failures = failures or {}
        self.block_reads = []

    def get_block(self, pos):
        self.block_reads.append(tuple(pos))
        return super().get_block(pos)

    def move_to(self, *args, **kwargs):
        dest = tuple(kwargs["pos"])
        if dest in self.failures:
            self.calls.append(("move_to", {"pos": list(dest)}))
            raise ApiError(self.failures[dest], "injected scout navigation failure")
        return super().move_to(*args, **kwargs)


class ScoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_failed_farthest_candidate_yields_then_moves_to_alternative(self):
        world = ScoutFailureWorld(failures={(18.5, 64, 0.5): "path_not_found"})
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "move_to"])
        self.assertEqual(steps[0]["error_code"], "path_not_found")
        self.assertEqual([s["pos"] for s in steps], [[18.5, 64, 0.5], [10.5, 64, 0.5]])
        self.assertEqual(VISITED_ANCHORS, [(10.5, 0.5)])

    def test_side_exposed_and_submerged_blocks_are_not_standing_positions(self):
        world = ScoutFailureWorld(blocks={
            (18, 63, 0): "grass_block", (18, 64, 0): "oak_log",
            (14, 63, 0): "sand", (14, 64, 0): "water",
            (10, 63, 0): "grass_block",
        })
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertEqual([s["pos"] for s in steps], [[10.5, 64, 0.5]])
        self.assertEqual(ex.acts, ["move_to"])

    def test_in_water_prefers_nearest_dry_land_over_farthest_destination(self):
        world = ScoutFailureWorld(blocks={(18, 63, 0): "grass_block", (6, 63, 0): "sand"})
        world.st["in_water"] = True
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertEqual(steps[0]["pos"], [6.5, 64, 0.5])

    def test_all_failed_candidates_preserve_error_and_limit_action_attempts(self):
        blocks = {(x, 63, 0): "grass_block" for x in (6, 10, 14, 18, 22)}
        failures = {(x + 0.5, 64, 0.5): "path_not_found" for x, _, _ in blocks}
        world = ScoutFailureWorld(blocks=blocks, failures=failures)
        ex, state = world.session()
        skill = make("scout_island")
        with self.assertRaisesRegex(Replan, "path_not_found"):
            drive(skill, ex, state)
        self.assertEqual(ex.acts, ["move_to"] * 3)
        self.assertEqual(skill.last["error_code"], "path_not_found")
        self.assertEqual(VISITED_ANCHORS, [])

    def test_repeated_failed_scouts_do_not_create_fictitious_visit_history(self):
        # ep-0004: repeated failures from below the surface used to mark each
        # unreachable destination visited, changing the next invocation's ranking.
        blocks = {(x, 63, 0): "grass_block" for x in (6, 10, 14, 18, 22)}
        failures = {(x + 0.5, 64, 0.5): "path_not_found" for x, _, _ in blocks}
        world = ScoutFailureWorld(blocks=blocks, failures=failures)
        world.st["pos"][1] = 60.0
        ex, state = world.session()
        real_visits = [(0.5, 0.5)]
        VISITED_ANCHORS.extend(real_visits)
        destinations = []
        for _ in range(3):
            before = len(world.calls)
            with self.assertRaisesRegex(Replan, "path_not_found"):
                drive(make("scout_island"), ex, state)
            destinations.append([args["pos"] for op, args in world.calls[before:] if op == "move_to"])
            self.assertEqual(VISITED_ANCHORS, real_visits)
        self.assertEqual([len(attempts) for attempts in destinations], [3, 3, 3])
        self.assertEqual(destinations[0], destinations[1])
        self.assertEqual(destinations[1], destinations[2])
        self.assertEqual(ex.acts, ["move_to"] * 9)

    def test_fallback_success_records_observed_arrival_instead_of_requested_target(self):
        class NearArrivalWorld(ScoutFailureWorld):
            def move_to(self, *args, **kwargs):
                result = super().move_to(*args, **kwargs)
                # A successful range=2 move can stop short of the requested point.
                self.st["pos"][0] -= 1.0
                return {**result, "pos": list(self.st["pos"])}

        world = NearArrivalWorld(failures={(18.5, 64, 0.5): "path_not_found"})
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertEqual([step["op"] for step in steps], ["move_to", "move_to"])
        self.assertEqual(steps[0]["error_code"], "path_not_found")
        self.assertEqual(steps[1]["pos"], [10.5, 64, 0.5])
        self.assertEqual(state.pos, [9.5, 64.0, 0.5])
        self.assertEqual(VISITED_ANCHORS, [(9.5, 0.5)])

    def test_failed_region_is_not_retried_at_neighboring_block(self):
        world = ScoutFailureWorld(
            blocks={(18, 63, 0): "grass_block", (17, 63, 0): "grass_block", (10, 63, 0): "grass_block"},
            failures={(18.5, 64, 0.5): "unreachable"},
        )
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertEqual([s["pos"] for s in steps], [[18.5, 64, 0.5], [10.5, 64, 0.5]])

    def test_abort_after_failed_move_starts_no_fallback_and_marks_no_visit(self):
        world = ScoutFailureWorld(failures={(18.5, 64, 0.5): "move_timeout"})
        ex, state = world.session()
        skill = make("scout_island")
        self.assertFalse(skill.step(ex, state))
        self.assertEqual(skill.last["error_code"], "move_timeout")
        skill.abort()
        self.assertEqual(ex.acts, ["move_to"])
        self.assertEqual(VISITED_ANCHORS, [])

    def test_safety_interruption_is_propagated_without_fallback(self):
        world = ScoutFailureWorld(failures={(18.5, 64, 0.5): "interrupted"})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "interrupted"):
            drive(make("scout_island"), ex, state)
        self.assertEqual(ex.acts, ["move_to"])
        self.assertEqual(VISITED_ANCHORS, [])

    def test_unloaded_candidate_is_skipped_without_navigation(self):
        class UnloadedWorld(ScoutFailureWorld):
            def get_block(self, pos):
                if pos[0] == 18:
                    raise ApiError("not_loaded", "injected unloaded standing space")
                return super().get_block(pos)

        world = UnloadedWorld()
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertEqual([s["pos"] for s in steps], [[10.5, 64, 0.5]])


if __name__ == "__main__":
    unittest.main()
