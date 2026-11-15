"""GridWorld + 显式窗口响应脚本；不模拟熔炉计时/物理，协议见 API §4.9。"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills import REGISTRY
from mcagent.skills.iron import SmeltIron
from mcagent.skills.testing import GridWorld, drive


def view(input_count=0, output=0, fuel=None):
    return {"window": "furnace", "sections": {
        "input": [{"name": "raw_iron", "count": input_count}] if input_count else [],
        "fuel": [{"name": fuel, "count": 1}] if fuel else [],
        "output": [{"name": "iron_ingot", "count": output}] if output else [],
    }, "info": {"burning": bool(input_count), "progress": 0.5, "fuel_left": 0.5}}


class IronWorld(GridWorld):
    """只返回测试明确安排的暴露矿与窗口快照，不扩展共享 GridWorld。"""

    def __init__(self, items=None, blocks=None, views=None):
        ground = {(x, 63, z): "stone" for x in range(-2, 10) for z in range(-2, 3)}
        ground.update(blocks or {})
        super().__init__(blocks=ground, items=items if items is not None else
                         {"stone_pickaxe": 1, "furnace": 1, "coal": 1})
        self.views = list(views or [view(), view(input_count=3), view(output=3), view()])
        self.last_view = view()
        self.no_drop = False

    def find_blocks(self, types=None, radius=16, limit=20):
        self.calls.append(("find_blocks", {"types": types, "radius": radius}))
        return {"blocks": [{"pos": list(p), "name": n} for p, n in self.blocks.items()
                           if n in types and sum((a-b)**2 for a, b in zip(p, self.st["pos"])) <= radius**2][:limit]}

    def dig(self, pos, collect=True, *, stop_on=None):
        result = super().dig(pos, collect=collect, stop_on=stop_on)
        name = result["dug"]
        # 此用例显式安排的掉落；不声称是通用矿石/附魔规则。
        if name in ("iron_ore", "deepslate_iron_ore", "coal_ore", "deepslate_coal_ore"):
            self.items[name] -= 1
            drop = "raw_iron" if "iron" in name else "coal"
            if not self.no_drop:
                self.items[drop] = self.items.get(drop, 0) + 1
        return result

    def window_view(self, target, *, stop_on=None):
        self.calls.append(("window_view", {"target": target}))
        if self.views:
            self.last_view = self.views.pop(0)
        return copy.deepcopy(self.last_view)

    def window_act(self, target, ops, *, stop_on=None):
        self.calls.append(("window_act", {"target": target, "ops": ops}))
        for op in ops:
            if op["op"] == "put":
                assert self.items.get(op["item"], 0) >= op["count"]
                self.items[op["item"]] -= op["count"]
            elif op["from"] == "output":
                self.items["iron_ingot"] = self.items.get("iron_ingot", 0) + op["count"]
        return {}


class ProspectingWorld(IronWorld):
    """显式安排揭开表层后的暴露矿；不模拟矿物生成、重力或寻路。"""

    def __init__(self, *args, exposed_after=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.exposed_after = exposed_after or {}

    def find_blocks(self, *args, **kwargs):
        result = super().find_blocks(*args, **kwargs)
        result["blocks"] = [b for b in result["blocks"]
                            if self.exposed_after.get(tuple(b["pos"])) not in self.blocks]
        return result


class SmeltIronTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("mcagent.skills.iron.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def run_skill(self, world, count=3):
        ex, state = world.session()
        return drive(SmeltIron(count=count), ex, state), ex, state

    def test_registered_and_normal_mine_place_smelt_take(self):
        self.assertIs(REGISTRY["smelt_iron"], SmeltIron)
        world = IronWorld(blocks={(2, 64, z): "iron_ore" for z in (-1, 0, 1)})
        steps, ex, state = self.run_skill(world)
        self.assertEqual([r["op"] for r in steps], [
            "equip", "dig", "equip", "dig", "equip", "dig", "place", "window_view",
            "window_act", "window_act", "window_view", "window_view", "window_act", "window_view"])
        ops = [c[1]["ops"][0] for c in world.calls if c[0] == "window_act"]
        self.assertEqual(ops, [
            {"op": "put", "item": "raw_iron", "count": 3, "to": "input"},
            {"op": "put", "item": "coal", "count": 1, "to": "fuel"},
            {"op": "take", "from": "output", "item": "iron_ingot", "count": 3}])
        self.assertEqual(state.count("iron_ingot"), 3)
        self.assertEqual(SmeltIron().verify_done(state, {}), "")
        self.assertIn("未增加", SmeltIron().verify_done(state, {"iron_ingot": 3}))
        self.assertEqual(self.sleep.call_count, 1)

    def test_no_exposed_ore_suspends_until_new_chunk(self):
        world = IronWorld()
        with self.assertRaisesRegex(EnvBlocked, "半径 32/64.*需换区域探索") as caught:
            self.run_skill(world)
        self.assertEqual(caught.exception.missing_predicate, "exposed_iron")
        self.assertEqual(caught.exception.wake_on, "chunk_moved")
        self.assertIn("阶梯 0/48 级", caught.exception.searched_volume)
        self.assertNotIn("环境缺铁矿", str(caught.exception))
        self.assertEqual([args["radius"] for op, args in world.calls if op == "find_blocks"],
                         [32, 64])
        self.assertFalse(any(op in ("dig", "move_to", "place") for op, _ in world.calls))

    def test_expands_exposed_ore_search_before_digging_stairs(self):
        world = IronWorld(blocks={(40, 64, 0): "iron_ore", (41, 64, 0): "furnace",
                                  (40, 63, 0): "stone", (41, 63, 0): "stone"},
                          views=[view(), view(output=1), view()])
        _, _, state = self.run_skill(world)
        scans = [args["radius"] for op, args in world.calls if op == "find_blocks"]
        self.assertEqual(scans[:2], [32, 64])
        self.assertEqual([args["pos"] for op, args in world.calls if op == "dig"], [[40, 64, 0]])
        self.assertEqual(state.count("iron_ingot"), 1)

    def test_empty_scan_prospects_then_smelts_newly_exposed_iron(self):
        # INC-003 的空扫描前缀；以下可安全揭露的矿是合成回归夹具，不是原磁带证据。
        world = ProspectingWorld(blocks={(1, 62, 0): "stone", (2, 63, 0): "iron_ore",
                                         (2, 62, 0): "stone", (2, 64, 0): "stone"},
                                 exposed_after={(2, 63, 0): (1, 63, 0)},
                                 views=[view(), view(output=1), view()])
        steps, _, state = self.run_skill(world)
        digs = [args["pos"] for op, args in world.calls if op == "dig"]
        self.assertEqual(digs, [[1, 63, 0], [2, 63, 0]])
        self.assertEqual(world.blocks[(1, 62, 0)], "stone")
        self.assertEqual(state.count("iron_ingot"), 1)
        self.assertEqual(SmeltIron().verify_done(state, {}), "")
        self.assertTrue(any(step["op"] == "window_act" for step in steps))

    def test_prospecting_has_total_descent_budget_and_keeps_support(self):
        start_y = 16
        blocks = {(x, y, 0): "stone" for x in range(1, 8)
                  for y in range(start_y - 1 - x, start_y)}
        world = IronWorld(blocks=blocks)
        world.st["pos"][1] = float(start_y)
        with self.assertRaisesRegex(Replan, "安全阶梯探矿仍未发现"):
            self.run_skill(world)
        moves = [args["pos"] for op, args in world.calls if op == "move_to"]
        self.assertEqual(len(moves), 6)
        self.assertLessEqual(sum(op == "dig" for op, _ in world.calls), 18)
        for x in range(1, 8):
            self.assertEqual(world.blocks[(x, start_y - 1 - x, 0)], "stone")
        self.assertEqual(world.blocks[(7, start_y - 7, 0)], "stone")
        self.assertEqual(world.items.get("raw_iron", 0), 0)

    def test_surface_prospecting_reaches_shallow_iron_band_before_exhaustion(self):
        start_y = 26
        blocks = {(x, y, 0): "stone" for x in range(1, 12)
                  for y in range(start_y - 1 - x, start_y)}
        world = IronWorld(blocks=blocks)
        world.st["pos"][1] = float(start_y)
        with self.assertRaisesRegex(Replan, "Y=26 到 16.*10/10"):
            self.run_skill(world)
        moves = [args["pos"] for op, args in world.calls if op == "move_to"]
        self.assertEqual([int(pos[1]) for pos in moves], list(range(25, 15, -1)))
        self.assertEqual(world.blocks[(10, 15, 0)], "stone")
        self.assertEqual(world.blocks[(11, 16, 0)], "stone")

    def test_prospecting_rejects_unsafe_floor_clearance_and_roof(self):
        hazards = [(62, name) for name in ("air", "water", "lava", "sand", "gravel", "unknown")]
        hazards += [(64, name) for name in ("water", "lava", "sand", "gravel", "furnace", "unknown")]
        hazards += [(66, name) for name in ("water", "lava", "sand", "red_sand", "gravel", "unknown")]
        for y, name in hazards:
            with self.subTest(y=y, name=name):
                world = IronWorld(blocks={(1, 62, 0): "stone", (1, y, 0): name})
                with self.assertRaises(Replan):
                    self.run_skill(world)
                self.assertFalse(any(op in ("dig", "move_to") for op, _ in world.calls))

    def test_prospecting_rechecks_support_position_and_tool_after_each_yield(self):
        for change in ("support", "position", "tool"):
            with self.subTest(change=change):
                world = IronWorld(blocks={(1, y, 0): "stone" for y in range(62, 66)})
                ex, state = world.session()
                skill = SmeltIron()
                while not skill.step(ex, state):
                    if skill.last["op"] == "dig":
                        break
                self.assertEqual(skill.last["pos"], [1, 65, 0])
                if change == "support":
                    world.blocks.pop((1, 62, 0))
                elif change == "position":
                    world.st["pos"] = [1.5, 65, 0.5]
                else:
                    world.items.pop("stone_pickaxe")
                with self.assertRaisesRegex(Replan, "保留支撑|探矿工具"):
                    drive(skill, ex, state)
                self.assertEqual(world.blocks[(1, 64, 0)], "stone")
                self.assertEqual(sum(op == "dig" for op, _ in world.calls), 1)
                self.assertFalse(any(op == "move_to" for op, _ in world.calls))

    def test_prospecting_requires_observed_arrival_before_further_descent(self):
        world = IronWorld(blocks={(1, 62, 0): "stone", (2, 61, 0): "stone"})
        move = world.move_to
        def stays_put(*args, **kwargs):
            old = list(world.st["pos"])
            result = move(*args, **kwargs)
            world.st["pos"] = old
            return result
        world.move_to = stays_put
        with self.assertRaisesRegex(Replan, "未确认到达落脚点"):
            self.run_skill(world)
        self.assertEqual(sum(op == "move_to" for op, _ in world.calls), 1)
        self.assertEqual(world.blocks[(2, 63, 0)], "stone")

    def test_abort_during_prospecting_stops_all_further_actions(self):
        for stop_at in range(1, 8):
            with self.subTest(stop_at=stop_at):
                world = IronWorld(blocks={(x, y, 0): "stone" for x in range(1, 4)
                                          for y in range(63 - x, 64)})
                ex, state = world.session()
                skill = SmeltIron()
                for _ in range(stop_at):
                    self.assertFalse(skill.step(ex, state))
                before = list(ex.acts)
                skill.abort()
                self.assertIsNone(skill._flow)
                self.assertEqual(ex.acts, before)
                self.assertFalse(world.st["sneaking"])

    def test_missing_pickaxe_and_gold_cannot_mine(self):
        for pick in ("wooden_pickaxe", "golden_pickaxe"):
            with self.subTest(pick=pick):
                world = IronWorld(items={pick: 1, "coal": 1, "furnace": 1})
                ex, state = world.session()
                self.assertIn("石镐", SmeltIron.blocked(state))
                with self.assertRaisesRegex(Replan, "石镐"):
                    drive(SmeltIron(), ex, state)

    def test_raw_iron_skips_mining_and_no_pick_needed(self):
        world = IronWorld(items={"raw_iron": 1, "oak_planks": 1, "furnace": 1},
                          views=[view(), view(output=1), view()])
        _, _, state = self.run_skill(world)
        self.assertEqual(state.count("iron_ingot"), 1)
        self.assertFalse(any(c[0] == "dig" for c in world.calls))

    def test_partial_ore_supply_finishes_available(self):
        world = IronWorld(blocks={(2, 64, 0): "deepslate_iron_ore"},
                          views=[view(), view(output=1), view()])
        _, _, state = self.run_skill(world)
        self.assertEqual(state.count("iron_ingot"), 1)

    def test_fuel_shortfall_reports_quantity_before_place(self):
        world = IronWorld(items={"raw_iron": 3, "oak_planks": 1, "furnace": 1})
        with self.assertRaisesRegex(Replan, "燃料不足.*缺 2"):
            self.run_skill(world)
        self.assertFalse(any(c[0] == "place" for c in world.calls))

    def test_mine_nearby_coal_before_wood(self):
        world = IronWorld(items={"raw_iron": 3, "stone_pickaxe": 1, "furnace": 1, "oak_planks": 2},
                          blocks={(2, 64, 0): "coal_ore"})
        self.run_skill(world)
        self.assertEqual(world.items["oak_planks"], 2)
        self.assertTrue(any(c[0] == "dig" for c in world.calls))

    def test_mixed_wood_is_separate_batches(self):
        world = IronWorld(items={"raw_iron": 2, "oak_planks": 1, "birch_log": 1, "furnace": 1},
                          views=[view(), view(output=1), view(), view(output=1), view()])
        _, _, state = self.run_skill(world)
        fuels = [c[1]["ops"][0]["item"] for c in world.calls if c[0] == "window_act"
                 and c[1]["ops"][0].get("to") == "fuel"]
        self.assertEqual(fuels, ["oak_planks", "birch_log"])
        self.assertEqual(state.count("iron_ingot"), 2)

    def test_reuses_furnace_and_collects_all_output(self):
        world = IronWorld(items={"raw_iron": 3, "coal": 1}, blocks={(2, 64, 0): "furnace"},
                          views=[view(output=2), view(output=5), view()])
        _, _, state = self.run_skill(world)
        self.assertEqual(state.count("iron_ingot"), 5)
        self.assertFalse(any(c[0] == "place" for c in world.calls))

    def test_no_furnace_or_safe_ground(self):
        world = IronWorld(items={"raw_iron": 3, "coal": 1})
        with self.assertRaisesRegex(Replan, "背包缺熔炉"):
            self.run_skill(world)
        world.items["furnace"] = 1
        world.blocks.clear()
        with self.assertRaisesRegex(Replan, "环境缺安全承重落点"):
            self.run_skill(world)
        self.assertFalse(any(c[0] == "dig" for c in world.calls))

    def test_mining_no_drop_replans(self):
        world = IronWorld(blocks={(2, 64, 0): "iron_ore"})
        world.no_drop = True
        with self.assertRaisesRegex(Replan, "raw_iron 未确认增加"):
            self.run_skill(world)

    def test_does_not_dig_support_after_move(self):
        world = IronWorld(blocks={(8, 63, 0): "iron_ore"})
        move = world.move_to
        def move_above(*args, **kwargs):
            result = move(*args, **kwargs)
            world.st["pos"][1] += 1
            return result
        world.move_to = move_above
        with self.assertRaisesRegex(Replan, "脚下"):
            self.run_skill(world)
        self.assertFalse(any(c[0] == "dig" for c in world.calls))

    def test_poll_budget_replans_without_real_sleep(self):
        world = IronWorld(items={"raw_iron": 3, "coal": 1, "furnace": 1},
                          views=[view(), view(input_count=3)])
        with self.assertRaisesRegex(Replan, "轮询上限"):
            self.run_skill(world)
        self.assertEqual(sum(c[0] == "window_view" for c in world.calls), 27)
        self.assertEqual(self.sleep.call_count, 25)

    def test_busy_furnace_replans_before_mutation(self):
        world = IronWorld(items={"raw_iron": 3, "coal": 1}, blocks={(2, 64, 0): "furnace"},
                          views=[view(input_count=1)])
        with self.assertRaisesRegex(Replan, "input 仍有原料"):
            self.run_skill(world)
        self.assertFalse(any(c[0] == "window_act" for c in world.calls))

    def test_deadline_replans(self):
        world = IronWorld(items={"raw_iron": 3, "coal": 1, "furnace": 1},
                          views=[view(), view(input_count=3)])
        # 仅替换技能本地 clock 对象，不影响 Executor 的计时。
        with patch("mcagent.skills.iron.time") as clock:
            clock.monotonic.side_effect = [0, 51]
            with self.assertRaisesRegex(Replan, "等待 50s"):
                self.run_skill(world)

    def test_take_requires_inventory_evidence(self):
        world = IronWorld(items={"raw_iron": 3, "coal": 1, "furnace": 1})
        act = world.window_act
        def no_pickup(target, ops, **kwargs):
            result = act(target, ops, **kwargs)
            if ops[0].get("from") == "output":
                world.items["iron_ingot"] = 0
            return result
        world.window_act = no_pickup
        with self.assertRaisesRegex(Replan, "take output 后背包铁锭未确认"):
            self.run_skill(world)

    def test_abort_at_each_yield_has_no_posture_or_window_leak(self):
        # §4.9 的窗口响应已自动关窗；abort 不再发额外阻塞动作。
        for stop_at in range(1, 9):
            with self.subTest(stop_at=stop_at):
                world = IronWorld(items={"raw_iron": 3, "coal": 1, "furnace": 1})
                ex, state = world.session()
                skill = SmeltIron()
                for _ in range(stop_at):
                    if skill.step(ex, state):
                        break
                before = len(ex.acts)
                skill.abort()
                self.assertIsNone(skill._flow)
                self.assertEqual(len(ex.acts), before)
                self.assertFalse(world.st["sneaking"])
                self.assertFalse(world.st.get("blocking", False))
                self.assertFalse(any(c[0] == "posture" for c in world.calls))


if __name__ == "__main__":
    unittest.main()
