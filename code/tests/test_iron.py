"""GridWorld + 显式窗口响应脚本；不模拟熔炉计时/物理，协议见 API §4.9。"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.loop.starter_skills import MINE_TRAILS, ScoutIsland, reset_world_memo
from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills import REGISTRY
from mcagent.skills.iron import SmeltIron, reset_iron_memo
from mcagent.skills.iron_gear import CraftIronGear
from mcagent.skills.iron_lava import FillLavaBucket
from mcagent.skills.scripted_world import ScriptedWorld
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
        if types is None:
            return {"radius": radius, "blocks": {}}
        return {"blocks": [{"pos": list(p), "name": n} for p, n in self.blocks.items()
                           if n in types and sum((a-b)**2 for a, b in zip(p, self.st["pos"])) <= radius**2][:limit]}

    def craft(self, item, count=1, *, stop_on=None):
        self.calls.append(("craft", {"item": item, "count": count}))
        if item == "raw_iron":
            self._need("raw_iron_block")
            self.items["raw_iron_block"] -= count
            self.items["raw_iron"] = self.items.get("raw_iron", 0) + 9 * count
        elif item.endswith("_planks"):
            log = item.removesuffix("_planks") + "_log"
            self._need(log)
            self.items[log] -= count
            self.items[item] = self.items.get(item, 0) + 4 * count
        else:
            raise ApiError("missing_item", f"不支持夹具配方 {item}")
        return {"crafted": item, "count": count}

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

    def test_unreachable_deep_ore_does_not_preempt_bounded_prospecting(self):
        deep_ore = [2, 35, 1]
        world = ProspectingWorld(
            blocks={tuple(deep_ore): "iron_ore", (1, 62, 0): "stone",
                    (1, 64, 0): "dirt", (2, 64, 0): "iron_ore"},
            exposed_after={(2, 64, 0): (1, 64, 0)},
            views=[view(), view(output=1), view()],
        )
        move = world.move_to
        moves = []

        def reject_deep_path(*args, **kwargs):
            moves.append(kwargs["pos"])
            if kwargs["pos"] == deep_ore:
                raise ApiError("path_not_found", "Stuck")
            return move(*args, **kwargs)

        world.move_to = reject_deep_path
        steps, _, state = self.run_skill(world, count=1)
        self.assertEqual(moves.count(deep_ore), 1)
        self.assertEqual(steps[0]["op"], "ore_route_skip")
        self.assertEqual(world.blocks[tuple(deep_ore)], "iron_ore")
        self.assertEqual(state.count("iron_ingot"), 1)

    def test_exhausted_direct_routes_switch_to_local_stair_scan(self):
        deep_ores = [(x, 35, 1) for x in range(2, 6)]
        world = ProspectingWorld(
            blocks={**dict.fromkeys(deep_ores, "iron_ore"), (1, 62, 0): "stone",
                    (1, 64, 0): "dirt", (2, 64, 0): "iron_ore"},
            exposed_after={(2, 64, 0): (1, 64, 0)},
            views=[view(), view(output=1), view()],
        )
        move = world.move_to
        rejected = []

        def reject_deep_paths(*args, **kwargs):
            target = tuple(kwargs["pos"])
            if target in deep_ores:
                rejected.append(target)
                raise ApiError("path_not_found", "No path")
            return move(*args, **kwargs)

        world.move_to = reject_deep_paths
        _, _, state = self.run_skill(world, count=1)
        self.assertEqual(rejected, deep_ores[:3])
        self.assertEqual(state.count("iron_ingot"), 1)

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

    def test_mines_with_copper_and_uses_pale_oak_fuel(self):
        world = IronWorld(
            items={"copper_pickaxe": 1, "furnace": 1, "pale_oak_planks": 1},
            blocks={(1, 63, 0): "tuff", (2, 64, 0): "iron_ore"},
            views=[view(), view(output=1), view()],
        )
        _, _, state = self.run_skill(world, count=1)
        self.assertEqual(state.count("iron_ingot"), 1)
        fuel = [a["ops"][0]["item"] for op, a in world.calls
                if op == "window_act" and a["ops"][0].get("to") == "fuel"]
        self.assertEqual(fuel, ["pale_oak_planks"])

    def test_smelt_iron_skips_dig_unconfirmed_ore_candidate(self):
        world = IronWorld(
            items={"stone_pickaxe": 1, "furnace": 1, "coal": 1},
            blocks={(2, 64, 0): "iron_ore", (3, 64, 0): "iron_ore"},
            views=[view(), view(output=1), view()],
        )
        orig_dig = world.dig
        failed_once = {"done": False}

        def flaky_dig(pos, collect=True, *, stop_on=None):
            if list(pos) == [2, 64, 0] and not failed_once["done"]:
                failed_once["done"] = True
                world.calls.append(("dig", {"pos": list(pos)}))
                raise ApiError("dig_unconfirmed", "unconfirmed", {"pos": [2, 64, 0], "eye_dist": 4.8})
            return orig_dig(pos, collect=collect, stop_on=stop_on)

        world.dig = flaky_dig
        steps, _, state = self.run_skill(world, count=1)
        self.assertTrue(any(s["op"] == "ore_route_skip" and s["error_code"] == "dig_unconfirmed" for s in steps))
        self.assertEqual(state.count("iron_ingot"), 1)
        self.assertEqual(world.blocks[(2, 64, 0)], "iron_ore")

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
        self.assertEqual(sum(c[0] == "window_view" for c in world.calls), 72)
        self.assertEqual(self.sleep.call_count, 70)

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

    def test_unreachable_ores_cached_across_retries_and_scout_climbs_mine_trail(self):
        reset_iron_memo()
        reset_world_memo()
        self.addCleanup(reset_iron_memo)
        self.addCleanup(reset_world_memo)
        deep_ores = [(x, 35, 1) for x in range(2, 5)]
        stair_blocks = {(x, y, 0): "stone" for x in range(1, 5) for y in range(63 - x, 64)}
        surface_far = {(20, 63, 0): "grass_block"}
        world = IronWorld(blocks={**dict.fromkeys(deep_ores, "iron_ore"), **stair_blocks, **surface_far})
        move = world.move_to
        rejected = []

        def reject_deep(*args, **kwargs):
            target = tuple(kwargs["pos"])
            if target in deep_ores:
                rejected.append(target)
                raise ApiError("path_not_found", "No path")
            return move(*args, **kwargs)

        world.move_to = reject_deep
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(SmeltIron(count=1), ex, state)
        self.assertEqual(rejected, deep_ores)
        self.assertEqual(int(state.pos[1]), 62)
        self.assertEqual(len(MINE_TRAILS), 1)

        # 同一区块再次进入 SmeltIron：不再对已知死洞的 3 个铁矿重复发 move_to。
        rejected.clear()
        with self.assertRaises(EnvBlocked):
            drive(SmeltIron(count=1), ex, state)
        self.assertEqual(rejected, [])

        # ScoutIsland 在井底 Y=60 沿记录阶梯先退回井口 Y=64，再外迁至新区块 x=20.5。
        drive(ScoutIsland(radius=36), ex, state)
        self.assertGreaterEqual(state.pos[0], 20.0)
        self.assertEqual(int(state.pos[1]), 64)

    def test_prospect_turns_around_mid_stair_water_and_places_furnace_on_upper_step(self):
        # 复现 ep-0003：向 +x 下探一级到 (1, 63, 0) 后，继续挖 (2, 64, 0) 时暴露上方地下水 (2, 65, 0)；
        # 技能应停止进入 (2, 62, 0)，改为向 +z 侧向转向 (1, 62, 1) 挖出铁矿，并转身把炉放在上一级台阶 (0, 63, 0)。
        blocks = {
            (0, 62, 0): "stone",
            (1, 62, 0): "stone", (1, 63, 0): "stone", (1, 64, 0): "stone",
            (2, 61, 0): "stone", (2, 62, 0): "stone", (2, 63, 0): "stone", (2, 64, 0): "granite",
            (1, 61, 1): "stone", (1, 62, 1): "stone", (1, 63, 1): "stone", (1, 64, 1): "stone",
            (1, 62, 2): "iron_ore", (1, 61, 2): "stone",
            # 井底四周同高度格皆为实心岩壁，迫使 _furnace 使用上一级台阶 py=y+1 放置熔炉
            (2, 62, 1): "stone", (0, 62, 1): "stone", (1, 62, 3): "stone",
        }
        world = ProspectingWorld(
            blocks=blocks,
            exposed_after={(1, 62, 2): (1, 62, 1)},
            views=[view(), view(output=1), view()],
        )
        orig_dig = world.dig

        def flood_on_granite(pos, collect=True, *, stop_on=None):
            res = orig_dig(pos, collect=collect, stop_on=stop_on)
            if list(pos) == [2, 64, 0]:
                world.blocks[(2, 65, 0)] = "water"
            return res

        world.dig = flood_on_granite
        _, _, state = self.run_skill(world, count=1)
        self.assertEqual(state.count("iron_ingot"), 1)
        # 未挖穿渗水列下层 (2, 63, 0)，也未踩进 (2, 62, 0)；站在 y=63 转身把熔炉放在上一级台阶 [0, 64, 0]
        self.assertEqual(world.blocks[(2, 63, 0)], "stone")
        placed = [args["pos"] for op, args in world.calls if op == "place"]
        self.assertEqual(placed, [[0, 64, 0]])

    def test_converts_log_to_planks_and_uses_retired_wooden_pickaxe_fuel(self):
        with self.subTest("log_to_4_planks"):
            world = IronWorld(items={"raw_iron": 3, "pale_oak_log": 1, "furnace": 1},
                              views=[view(), view(output=3), view()])
            _, _, state = self.run_skill(world, count=3)
            self.assertEqual(state.count("iron_ingot"), 3)
            self.assertEqual(state.count("pale_oak_planks"), 2)

        with self.subTest("post_furnace_cold_start_inventory"):
            world = IronWorld(
                items={"raw_iron": 3, "stone_pickaxe": 1, "wooden_pickaxe": 1,
                       "oak_planks": 1, "stick": 3, "furnace": 1},
                views=[view(), view(output=1), view(), view(output=1), view(), view(output=1), view()],
            )
            _, _, state = self.run_skill(world, count=3)
            self.assertEqual(state.count("iron_ingot"), 3)
            self.assertEqual(state.count("stone_pickaxe"), 1)
            self.assertEqual(state.count("stick"), 1)

    def test_unpacks_raw_iron_block_and_recovers_interrupted_placed_furnace(self):
        with self.subTest("raw_iron_block"):
            world = IronWorld(
                items={"stone_pickaxe": 1, "coal": 1, "furnace": 1},
                blocks={(2, 64, 0): "raw_iron_block"},
                views=[view(), view(output=3), view()],
            )
            _, _, state = self.run_skill(world, count=3)
            self.assertEqual(state.count("iron_ingot"), 3)
            self.assertEqual(state.count("raw_iron"), 6)

        with self.subTest("recover_placed_furnace_output"):
            world = IronWorld(
                items={"stone_pickaxe": 1},
                blocks={(2, 64, 0): "furnace"},
                views=[view(output=3)],
            )
            _, _, state = self.run_skill(world, count=3)
            self.assertEqual(state.count("iron_ingot"), 3)
            self.assertFalse(any(op == "dig" for op, _ in world.calls))

    def test_fake_mining_arrival_skips_route_across_retries_until_dimension_changes(self):
        ore = [6, 64, 0]
        world = IronWorld(blocks={tuple(ore): "iron_ore"})
        world.st["dimension"] = "overworld"
        attempts = []

        def stays_put(*args, **kwargs):
            pos = kwargs["pos"]
            attempts.append(pos)
            return {"pos": pos}

        world.move_to = stays_put
        ex, state = world.session()
        for dimension, times in (("overworld", 1), ("overworld", 1), ("the_nether", 2)):
            world.st["dimension"] = dimension
            with self.assertRaises(EnvBlocked):
                drive(SmeltIron(count=1), ex, state)
            self.assertEqual(attempts, [ore] * times)
        self.assertNotIn("dig", ex.acts)

    def test_material_plan_counts_transformed_log_once(self):
        world = IronWorld(items={"pale_oak_log": 1, "raw_iron": 3, "furnace": 1})
        _, state = world.session()
        expected = (("raw_iron", 3, "consume"), ("pale_oak_log", 1, "consume"))
        self.assertEqual(SmeltIron.material_plan({"count": 3}, state), expected)


class CraftIronGearTests(unittest.TestCase):
    def test_registered_and_copper_tool_does_not_replace_explicit_iron_pickaxe(self):
        self.assertIs(REGISTRY["craft_iron_gear"], CraftIronGear)
        world = ScriptedWorld(blocks={(1, 64, 0): "crafting_table"},
                              items={"iron_ingot": 3, "stick": 2, "copper_pickaxe": 1})
        world.queue("find_blocks", {"blocks": [{"pos": [1, 64, 0], "name": "crafting_table"}]})

        def craft(fixture, item, count, **_):
            self.assertEqual((item, count), ("iron_pickaxe", 1))
            fixture.items.update(iron_ingot=0, stick=0, iron_pickaxe=1)
            return {"crafted": item}

        world.queue("craft", craft)
        ex, state = world.session()
        skill = CraftIronGear(item="iron_pickaxe")
        self.assertEqual([step["op"] for step in drive(skill, ex, state)], ["craft"])
        self.assertEqual(state.count("iron_pickaxe"), 1)
        self.assertEqual(skill.verify_done(state, {}), "")
        world.assert_exhausted()

    def test_missing_ingredients_replan_without_action(self):
        world = ScriptedWorld(items={"iron_ingot": 2, "stick": 2})
        ex, state = world.session()
        skill = CraftIronGear(item="iron_pickaxe")
        self.assertFalse(skill.can_start(state))
        self.assertTrue(CraftIronGear(item="shears").can_start(state))
        with self.assertRaisesRegex(Replan, "缺材料"):
            drive(skill, ex, state)
        self.assertEqual(ex.acts, [])

    def test_missing_table_has_bounded_environment_evidence(self):
        world = ScriptedWorld(items={"iron_ingot": 4})
        world.queue("find_blocks", {"blocks": []})
        ex, state = world.session()
        with self.assertRaises(EnvBlocked) as caught:
            drive(CraftIronGear(item="iron_boots"), ex, state)
        self.assertEqual(caught.exception.missing_predicate, "station_ready:crafting_table")
        self.assertIn("16", caught.exception.searched_volume)
        self.assertEqual(caught.exception.wake_on, "chunk_moved")
        self.assertEqual(ex.acts, [])
        world.assert_exhausted()

    def test_false_craft_receipt_without_inventory_evidence_fails(self):
        world = ScriptedWorld(items={"iron_ingot": 2})
        world.queue("craft", {"crafted": "shears"})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "背包仅增加 0"):
            drive(CraftIronGear(item="shears"), ex, state)
        self.assertEqual(ex.acts, ["craft"])
        world.assert_exhausted()

    def test_existing_product_does_not_spend_materials(self):
        world = ScriptedWorld(items={"iron_boots": 1, "iron_ingot": 4})
        ex, state = world.session()
        self.assertEqual(drive(CraftIronGear(item="iron_boots"), ex, state), [])
        self.assertEqual(CraftIronGear.material_plan({"item": "iron_boots"}, state), ())

    def test_material_plan_resolves_tag_and_shares_exact_iron_amounts(self):
        _, state = ScriptedWorld(items={"iron_ingot": 6, "stick": 2,
                                       "oak_planks": 4, "birch_planks": 2}).session()
        self.assertEqual(CraftIronGear.material_plan({"item": "iron_pickaxe"}, state),
                         (("iron_ingot", 3, "consume"), ("stick", 2, "consume")))
        self.assertEqual(CraftIronGear.material_plan({"item": "iron_boots"}, state),
                         (("iron_ingot", 4, "consume"),))
        self.assertEqual(CraftIronGear.material_plan({"item": "shield"}, state),
                         (("oak_planks", 4, "consume"), ("birch_planks", 2, "consume"),
                          ("iron_ingot", 1, "consume")))

    def test_abort_after_craft_has_no_additional_action(self):
        world = ScriptedWorld(items={"iron_ingot": 2})
        world.queue("craft", {"crafted": "shears"})
        ex, state = world.session()
        skill = CraftIronGear(item="shears")
        self.assertFalse(skill.step(ex, state))
        skill.abort()
        self.assertIsNone(skill._flow)
        self.assertEqual(ex.acts, ["craft"])
        world.assert_exhausted()


class LavaFixture(ScriptedWorld):
    def __init__(self, *, items=None, source=(2, 64, 0), level=0, support=True):
        terrain = {(x, 63, 0): "stone" for x in range(-2 if support else 0, 3)}
        super().__init__(blocks=terrain, items={"bucket": 1} if items is None else items)
        if source:
            self.blocks[source] = "lava"
        self.source = source
        self.level = level
        self.st["dimension"] = "overworld"
        self.queue("find_blocks", {"blocks": [{"name": "lava", "pos": list(source)}] if source else []})

    def get_block(self, pos):
        data = super().get_block(pos)
        if data.get("name") == "lava":
            data["properties"] = {"level": self.level}
        return data


class FillLavaBucketTests(unittest.TestCase):
    def test_registered_fills_observed_source_and_reads_separate_advancement(self):
        self.assertIs(REGISTRY["fill_lava_bucket"], FillLavaBucket)
        world = LavaFixture()

        def collect(fixture, **request):
            self.assertEqual(request["pos"], [2, 64, 0])
            self.assertEqual(request["item"], "bucket")
            fixture.items.update(bucket=0, lava_bucket=1)
            fixture.blocks.pop(fixture.source)
            return {"changed": True}

        world.queue("interact", collect)
        world.queue("advancements", {"ready": True, "advancements": [
            {"id": "minecraft:story/lava_bucket", "status": "completed"},
        ]})
        ex, state = world.session()
        skill = FillLavaBucket()
        result = drive(skill, ex, state)
        self.assertEqual(ex.acts, ["interact"])
        self.assertEqual([step["op"] for step in result], ["interact", "lava_bucket_evidence"])
        self.assertTrue(result[-1]["advancement_confirmed"])
        self.assertEqual(skill.verify_done(state, {}), "")
        world.assert_exhausted()

    def test_no_bucket_replans_and_owned_product_skips_search(self):
        for items in ({}, {"lava_bucket": 1}):
            with self.subTest(items=items):
                world = ScriptedWorld(items=items)
                ex, state = world.session()
                if items:
                    self.assertEqual(drive(FillLavaBucket(), ex, state), [])
                    self.assertEqual(FillLavaBucket.material_plan({}, state), ())
                else:
                    with self.assertRaisesRegex(Replan, "空桶"):
                        drive(FillLavaBucket(), ex, state)
                self.assertEqual(ex.acts, [])

    def test_no_source_flow_unknown_and_no_retreat_are_environment_blocks(self):
        for fixture, predicate in (
            (LavaFixture(source=None), "lava_source"),
            (LavaFixture(level=4), "lava_source"),
            (LavaFixture(level=None), "lava_source"),
            (LavaFixture(support=False), "safe_lava_access"),
        ):
            with self.subTest(level=fixture.level, predicate=predicate):
                ex, state = fixture.session()
                with self.assertRaises(EnvBlocked) as caught:
                    drive(FillLavaBucket(), ex, state)
                self.assertEqual(caught.exception.missing_predicate, predicate)
                self.assertIn("半径 16 格内最多 12", caught.exception.searched_volume)
                self.assertEqual(caught.exception.wake_on, "chunk_moved")
                self.assertEqual(ex.acts, [])
                fixture.assert_exhausted()

    def test_source_is_refreshed_after_approach_and_abort_sends_no_interaction(self):
        for abort in (True, False):
            with self.subTest(abort=abort):
                world = LavaFixture(source=(4, 64, 0))
                ex, state = world.session()
                skill = FillLavaBucket()
                self.assertFalse(skill.step(ex, state))
                self.assertEqual(ex.acts, ["move_to"])
                if abort:
                    skill.abort()
                    self.assertIsNone(skill._flow)
                else:
                    world.level = 1
                    with self.assertRaises(EnvBlocked) as caught:
                        drive(skill, ex, state)
                    self.assertEqual(caught.exception.missing_predicate, "lava_source")
                self.assertEqual(ex.acts, ["move_to"])
                world.assert_exhausted()

    def test_receipt_without_new_lava_bucket_fails(self):
        world = LavaFixture()
        world.queue("interact", {"changed": True})
        ex, state = world.session()
        self.assertEqual(FillLavaBucket.material_plan({}, state), (("bucket", 1, "borrow"),))
        with self.assertRaisesRegex(Replan, "背包未确认空桶转为岩浆桶"):
            drive(FillLavaBucket(), ex, state)
        self.assertEqual(ex.acts, ["interact"])
        world.assert_exhausted()

    def test_filled_bucket_can_be_verified_before_advancement_arrives(self):
        world = LavaFixture(level="0")

        def collect(fixture, **_):
            fixture.items.update(bucket=0, lava_bucket=1)
            return {}

        world.queue("interact", collect)
        world.queue("advancements", {"ready": True, "advancements": []})
        ex, state = world.session()
        steps = drive(FillLavaBucket(), ex, state)
        self.assertTrue(steps[-1]["inventory_confirmed"])
        self.assertFalse(steps[-1]["advancement_confirmed"])
        world.assert_exhausted()

    def test_lava_cauldron_overhang_raycast_and_cannot_interact(self):
        overhang = LavaFixture(source=(2, 63, 0))
        overhang.blocks[(2, 64, 0)] = "stone"
        ex, state = overhang.session()
        with self.assertRaises(EnvBlocked) as blocked:
            drive(FillLavaBucket(), ex, state)
        self.assertEqual(blocked.exception.missing_predicate, "safe_lava_access")
        overhang.assert_exhausted()

        cauldron_world = ScriptedWorld(
            blocks={(x, 63, 0): "stone" for x in range(-2, 3)} | {(2, 64, 0): "lava_cauldron"},
            items={"bucket": 1},
        )
        cauldron_world.st["dimension"] = "overworld"
        cauldron_world.queue("find_blocks", {"blocks": [{"name": "lava_cauldron", "pos": [2, 64, 0]}]})

        def take_cauldron(w, **req):
            self.assertEqual(req["pos"], [2, 64, 0])
            w.blocks[(2, 64, 0)] = "cauldron"
            w.items.update(bucket=0, lava_bucket=1)
            return {"block": "lava_cauldron", "changed": True, "gained": {"lava_bucket": 1}}

        cauldron_world.queue("interact", take_cauldron)
        cauldron_world.queue("advancements", {"ready": True, "advancements": []})
        ex2, state2 = cauldron_world.session()
        drive(FillLavaBucket(), ex2, state2)
        self.assertEqual(state2.count("lava_bucket"), 1)
        cauldron_world.assert_exhausted()

        reject_world = LavaFixture()
        reject_world.queue("interact", ApiError("cannot_interact", "hit solid block"))
        ex3, state3 = reject_world.session()
        with self.assertRaisesRegex(Replan, "cannot_interact"):
            drive(FillLavaBucket(), ex3, state3)
        reject_world.assert_exhausted()


if __name__ == "__main__":
    unittest.main()
