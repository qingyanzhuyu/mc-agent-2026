from __future__ import annotations

import unittest

from mcagent.client import ApiError
from mcagent.runtime import Replan
from mcagent.skills.base import failure
from mcagent.skills.furnace import collect_furnace, load_furnace
from mcagent.skills.mining import MineStepUnsafe, mine_step, prepare_mining_pick
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import drive
from mcagent.skills.windows import (
    apply_window_ops, is_storage_window, put_window_item, select_window_option, take_container_item,
)

TARGET = {"pos": [1, 64, 0]}
FIRST = {"op": "put", "item": "raw_iron", "count": 1, "to": "input"}
SECOND = {"op": "put", "item": "coal", "count": 1, "to": "fuel"}


def window(name="furnace", **sections):
    return {"window": name, "sections": sections}


class WindowScriptsTests(unittest.TestCase):
    def test_partial_result_reobserves_and_sends_only_unexecuted_suffix(self):
        world = ScriptedWorld(items={"raw_iron": 1, "coal": 1})

        def partial(world, **request):
            world.items["raw_iron"] = 0
            raise ApiError("missing_item", "fuel slot temporarily unavailable", {"done": 1, "failed_op": 1})

        def finish(world, **request):
            self.assertEqual(request["ops"], [SECOND])
            world.items["coal"] = 0
            return {"done": 1}

        world.queue("window_act", partial, finish)
        world.queue("window_view", window(input=[{"name": "raw_iron", "count": 1}], fuel=[]))
        ex, state = world.session()
        def confirm(pending, view, state):
            return (pending == (SECOND,) and bool(view["sections"]["input"])
                    and view["sections"]["fuel"] == [] and state.count("raw_iron") == 0
                    and state.count("coal") == 1)
        drive(apply_window_ops(ex, state, TARGET, [FIRST, SECOND], verify_pending=confirm), ex)
        self.assertEqual(ex.acts, ["window/act", "window/view", "window/act"])
        world.assert_exhausted()

    def test_conflicting_receipt_or_unsafe_effect_never_replays(self):
        for extra, confirm in (
            ({"done": 1, "failed_op": 0}, True),
            ({"done": 1, "failed_op": 1}, False),
            ({"done": 0, "failed_op": 0}, True),
        ):
            with self.subTest(extra=extra, confirm=confirm):
                world = ScriptedWorld()
                world.queue("window_act", ApiError("missing_item", "partial", extra))
                world.queue("window_view", window())
                ex, state = world.session()
                flow = apply_window_ops(ex, state, TARGET, [FIRST, SECOND],
                                        verify_pending=lambda *_: confirm)
                with self.assertRaisesRegex(Replan, "partial"):
                    drive(flow, ex)
                self.assertEqual(ex.acts, ["window/act", "window/view"])
                world.assert_exhausted()

    def test_furnace_missing_fuel_refuses_to_partially_load_inputs(self):
        world = ScriptedWorld(items={"raw_iron": 2})
        world.queue("window_view", window())
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "coal"):
            drive(load_furnace(ex, state, TARGET, raw_item="raw_iron", raw_count=2,
                               fuel_item="coal", fuel_count=1), ex)
        self.assertEqual(ex.acts, ["window/view"])

    def test_furnace_fuel_slot_return_needs_inventory_confirmation(self):
        world = ScriptedWorld()
        world.queue("window_view", window(fuel=[{"name": "bucket", "count": 1}]), window(fuel=[]))

        def take_bucket(world, **request):
            self.assertEqual(request["ops"], [{"op": "take", "from": "fuel", "item": "bucket", "count": 1}])
            world.items["bucket"] = 1
            return {}

        world.queue("window_act", take_bucket)
        ex, state = world.session()
        drive(collect_furnace(ex, state, TARGET, item="bucket", section="fuel", require_empty=True), ex)
        self.assertEqual(state.count("bucket"), 1)
        self.assertEqual(ex.acts, ["window/view", "window/act", "window/view"])
        world.assert_exhausted()

    def test_furnace_replacement_checks_returned_fuel_before_loading(self):
        world = ScriptedWorld(items={"raw_iron": 1, "coal": 1})
        world.queue("window_view", window(fuel=[{"name": "bucket", "count": 1}]))
        world.queue("window_act", {})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "未确认收到 1 个 bucket"):
            drive(load_furnace(ex, state, TARGET, raw_item="raw_iron", raw_count=1,
                               fuel_item="coal", fuel_count=1, replace_fuel=True), ex)
        self.assertEqual(ex.acts, ["window/view", "window/act"])

    def test_put_requires_window_evidence_even_after_inventory_debit(self):
        world = ScriptedWorld(items={"lapis_lazuli": 1})
        world.queue("window_view", window("enchanting_table", lapis=[]),
                    window("enchanting_table", lapis=[{"name": "lapis_lazuli", "count": 1, "slot": 7}]))

        def wrong_slot(world, **request):
            world.items["lapis_lazuli"] = 0
            return {}

        world.queue("window_act", wrong_slot)
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "未确认"):
            drive(put_window_item(ex, state, TARGET, "lapis_lazuli", 1, "lapis", slot=2), ex)
        self.assertEqual(ex.acts, ["window/view", "window/act", "window/view"])

    def test_select_requires_caller_postcondition(self):
        world = ScriptedWorld()
        world.queue("window_view", window("enchanting_table"), window("enchanting_table"))
        world.queue("window_act", {})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "select 0"):
            drive(select_window_option(ex, state, TARGET, 0, verify=lambda *_: False), ex)
        world.assert_exhausted()

    def test_scripted_entity_reads_are_explicit_and_exhaustion_is_an_error(self):
        world = ScriptedWorld()
        world.queue("get_entities", {"entities": [{"id": 7}]}, {"entities": []})
        self.assertEqual(world.get_entities()["entities"], [{"id": 7}])
        self.assertEqual(world.get_entities()["entities"], [])
        with self.assertRaisesRegex(AssertionError, "未安排"):
            world.get_entities()
        world.assert_exhausted()

    def test_done_step_receipt_shape_resumes_unexecuted_suffix(self):
        world = ScriptedWorld(items={"raw_iron": 1, "coal": 1})

        def partial(world, **request):
            world.items["raw_iron"] = 0
            raise ApiError("section_full", "fuel occupied", {
                "done": [{"op": "put", "put": "raw_iron", "count": 1, "to": "input"}],
                "failed_op": 1,
            })

        def finish(world, **request):
            self.assertEqual(request["ops"], [SECOND])
            world.items["coal"] = 0
            return {"done": [{"op": "put", "put": "coal", "count": 1, "to": "fuel"}]}

        world.queue("window_act", partial, finish)
        world.queue("window_view", window(input=[{"name": "raw_iron", "count": 1}], fuel=[]))
        ex, state = world.session()
        drive(apply_window_ops(
            ex, state, TARGET, [FIRST, SECOND],
            verify_pending=lambda pending, view, st: pending == (SECOND,) and st.count("raw_iron") == 0,
        ), ex)
        self.assertEqual(ex.acts, ["window/act", "window/view", "window/act"])
        world.assert_exhausted()

    def test_take_container_partial_and_zero_fit_inventory_full(self):
        world = ScriptedWorld(items={"diamond": 62})
        before = {"window": "generic_9x3", "target": "chest",
                  "sections": {"container": [{"name": "diamond", "count": 5}]}}
        after = {"window": "generic_9x3", "target": "chest",
                 "sections": {"container": [{"name": "diamond", "count": 3}]}}
        self.assertTrue(is_storage_window(before, "chest"))
        self.assertFalse(is_storage_window(before, "barrel"))
        # 兼容本地联调网关与方块名 window 别名（如 window='chest' / 'barrel' / 'red_shulker_box'）
        self.assertTrue(is_storage_window({"window": "chest", "target": "chest", "sections": {"container": []}}))
        self.assertTrue(is_storage_window({"window": "barrel", "target": "barrel", "sections": {"container": []}}))
        self.assertTrue(is_storage_window({"window": "red_shulker_box", "target": "red_shulker_box", "sections": {"container": []}}))
        self.assertFalse(is_storage_window({"window": "furnace", "target": "furnace", "sections": {"input": []}}))

        def partial_take(world, **request):
            world.items["diamond"] = 64
            return {"done": [{"op": "take", "took": {"diamond": 2}, "from": "container", "inventory_full": True}]}

        world.queue("window_view", before, after)
        world.queue("window_act", partial_take)
        ex, state = world.session()
        got = {}

        def run_partial():
            got.update((yield from take_container_item(ex, state, TARGET, "container", "diamond", 5)))

        drive(run_partial(), ex)
        self.assertEqual(got["count"], 2)
        self.assertTrue(got["inventory_full"])
        self.assertEqual(state.count("diamond"), 64)
        world.assert_exhausted()

        world2 = ScriptedWorld()
        world2.queue("window_view", before)
        world2.queue("window_act", ApiError("inventory_full", "no space", {"failed_op": 0, "done": [], "window": before}))
        world2.queue("window_view", before)
        ex2, state2 = world2.session()
        with self.assertRaisesRegex(Replan, "inventory_full") as caught:
            drive(take_container_item(ex2, state2, TARGET, "container", "diamond", 5), ex2)
        self.assertEqual(failure(caught.exception)[0], "inventory_full")
        world2.assert_exhausted()

    def test_collect_furnace_output_requires_whole_stack_while_fuel_allows_partial(self):
        world = ScriptedWorld()
        out_view = window(output=[{"name": "iron_ingot", "count": 3}])
        world.queue("window_view", out_view, out_view)
        world.queue("window_act", ApiError("inventory_full", "output requires full stack", {
            "failed_op": 0, "done": [], "window": out_view,
        }))
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "inventory_full") as caught:
            drive(collect_furnace(ex, state, TARGET, item="iron_ingot"), ex)
        self.assertEqual(failure(caught.exception)[0], "inventory_full")
        world.assert_exhausted()

        world2 = ScriptedWorld()
        fuel_before = window(fuel=[{"name": "coal", "count": 4}])
        fuel_after = window(fuel=[{"name": "coal", "count": 2}])

        def take_fuel(world, **request):
            world.items["coal"] = 2
            return {"done": [{"op": "take", "took": {"coal": 2}, "from": "fuel", "inventory_full": True}]}

        world2.queue("window_view", fuel_before, fuel_after)
        world2.queue("window_act", take_fuel)
        ex2, state2 = world2.session()
        got = {}

        def run_fuel():
            got.update((yield from collect_furnace(ex2, state2, TARGET, item="coal", section="fuel")))

        drive(run_fuel(), ex2)
        self.assertEqual((got["count"], got["inventory_full"]), (2, True))
        world2.assert_exhausted()


class MineStepTests(unittest.TestCase):
    def test_only_allowed_pick_is_selected_even_if_more_powerful_pick_is_owned(self):
        world = ScriptedWorld(items={"diamond_pickaxe": 1, "iron_pickaxe": 1})
        ex, state = world.session()
        drive(prepare_mining_pick(ex, state, allowed=frozenset({"iron_pickaxe"})), ex)
        self.assertEqual(world.calls, [("equip", {"item": "iron_pickaxe", "slot": "hand"})])

    def test_observed_column_leaves_floor_and_reaches_landing(self):
        world = ScriptedWorld(items={"iron_pickaxe": 1}, blocks={(1, y, 0): "stone" for y in range(62, 66)})
        ex, state = world.session()
        drive(mine_step(ex, state, (1, 63, 0)), ex)
        self.assertEqual(state.pos, [1.5, 63.0, 0.5])
        self.assertEqual(world.blocks, {(1, 62, 0): "stone"})
        self.assertEqual([op for op, _ in world.calls if op != "equip"], ["dig", "dig", "dig", "move_to"])

    def test_dangerous_observed_roof_fails_before_any_action(self):
        world = ScriptedWorld(blocks={(1, 62, 0): "stone", (1, 66, 0): "gravel"})
        ex, state = world.session()
        with self.assertRaises(MineStepUnsafe):
            drive(mine_step(ex, state, (1, 63, 0)), ex)
        self.assertEqual(ex.acts, [])

    def test_mine_step_dig_unconfirmed_raises_cut_unsafe_unless_moved(self):
        world = ScriptedWorld(items={"iron_pickaxe": 1}, blocks={(1, y, 0): "stone" for y in range(62, 66)})
        world.queue("equip", {"equipped": "iron_pickaxe"})
        world.queue("dig", ApiError("dig_unconfirmed", "unconfirmed", {"pos": [1, 65, 0], "eye_dist": 4.8}))
        ex, state = world.session()
        with self.assertRaises(MineStepUnsafe) as caught:
            drive(mine_step(ex, state, (1, 63, 0)), ex)
        self.assertEqual(caught.exception.stage, "cut")
        world.assert_exhausted()

        moved_world = ScriptedWorld(items={"iron_pickaxe": 1}, blocks={(1, y, 0): "stone" for y in range(62, 66)})
        moved_world.queue("equip", {"equipped": "iron_pickaxe"})

        def walk_and_fail(w, **kwargs):
            w.st["pos"] = [1.5, 65.0, 0.5]
            raise ApiError("dig_unconfirmed", "unconfirmed after walk", {"pos": [1, 65, 0]})

        moved_world.queue("dig", walk_and_fail)
        ex2, state2 = moved_world.session()
        with self.assertRaises(Replan) as caught2:
            drive(mine_step(ex2, state2, (1, 63, 0)), ex2)
        self.assertNotIsInstance(caught2.exception, MineStepUnsafe)
        moved_world.assert_exhausted()
