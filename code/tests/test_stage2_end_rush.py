"""Stage 2 End Rush L2 bridge_segment 与 4 项末地 L3 技能离线契约测试。"""
from __future__ import annotations

import math
import unittest
from copy import deepcopy
from unittest.mock import patch

from mcagent.runtime import EnvBlocked, Replan, State
from mcagent.skills import REGISTRY, contract_errors
import mcagent.skills.end_rush as end_rush
from mcagent.skills.end_rush import (
    EnterEndGateway, EnterTheEnd, FindEndCity, LevitateFifty, bridge_segment,
)
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


def _flat(x0: int, x1: int, z0: int, z1: int, y: int = 63, block: str = "end_stone") -> dict:
    return {(x, y, z): block for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)}


class EndWorld(ScriptedWorld):
    """支持末地维度、方块属性、实体扫描、状态序列与末影珍珠/药水交互的测试世界。"""

    def __init__(
        self,
        pos=(0.5, 64.0, 0.5),
        *,
        dimension: str = "the_end",
        blocks=None,
        items=None,
        entities=None,
        block_props=None,
        hp: float = 20.0,
        on_ground: bool = True,
    ):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st.update(dimension=dimension, health=hp, on_ground=on_ground, ready=True)
        self.block_props: dict[tuple, dict] = {tuple(k): dict(v) for k, v in (block_props or {}).items()}
        self.entities: list[dict] = [deepcopy(e) for e in (entities or ())]
        self.on_interact = None
        self.on_move_to = None
        self.on_use_item = None
        self.status_seq: list[dict] = []

    def status(self, timeout: float = 10) -> dict:
        if self.status_seq:
            patch_st = self.status_seq.pop(0)
            self.st.update(patch_st)
        return super().status(timeout)

    def get_block(self, pos) -> dict:
        cell = tuple(int(v) for v in pos)
        out = {"name": self.blocks.get(cell, "air")}
        if cell in self.block_props:
            out["properties"] = deepcopy(self.block_props[cell])
        return out

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None):
        self.calls.append(("find_blocks", {"types": types, "radius": radius, "limit": limit}))
        wanted = {t.removeprefix("minecraft:") for t in (types or ())}
        px, py, pz = self.st["pos"]
        hits = []
        for (bx, by, bz), name in sorted(self.blocks.items()):
            if wanted and name not in wanted:
                continue
            d = math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5))
            if d <= radius + 0.5:
                row = {"name": name, "pos": [bx, by, bz], "dist": round(d, 2)}
                if (bx, by, bz) in self.block_props:
                    row["properties"] = deepcopy(self.block_props[(bx, by, bz)])
                hits.append(row)
        hits.sort(key=lambda b: b["dist"])
        return {"blocks": hits[:limit]}

    def get_entities(self, radius=32, types=None):
        self.calls.append(("get_entities", {"radius": radius, "types": types}))
        wanted = {t.removeprefix("minecraft:") for t in (types or ())}
        px, py, pz = self.st["pos"]
        out = [
            deepcopy(e)
            for e in self.entities
            if (not wanted or e.get("name") in wanted or e.get("type") in wanted)
            and math.dist((px, py, pz), e["pos"]) <= radius + 0.5
        ]
        return {"entities": out}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
        if self.on_move_to is not None:
            res = self.on_move_to(self, pos=pos, x=x, y=y, z=z, rng=rng)
            if res is not None:
                self.calls.append(("move_to", {"pos": pos, "rng": rng}))
                return res
        return GridWorld.move_to(self, x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None):
        self.calls.append(("interact", {"pos": pos, "entity_id": entity_id, "item": item}))
        if item:
            self._need(item)
        if self.on_interact is not None:
            return self.on_interact(self, pos=pos, entity_id=entity_id, item=item)
        return {"changed": True}

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None):
        self.calls.append(("use_item", {"item": item, "target": target}))
        self._need(item)
        if self.on_use_item is not None:
            return self.on_use_item(self, item=item, target=target)
        if item != "water_bucket":
            self.items[item] -= 1
        return {"used": item, "target": target}


class EndRushTestBase(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()
        reset_all_voxel_caches()
        self._sleep_patch = patch.object(end_rush, "_POLL_SLEEP_S", 0.0)
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)


class EndRushContractTests(EndRushTestBase):
    def test_all_four_skills_pass_contract_and_register_offline(self):
        expected = {
            "enter_the_end": EnterTheEnd,
            "enter_end_gateway": EnterEndGateway,
            "find_end_city": FindEndCity,
            "levitate_fifty": LevitateFifty,
        }
        for name, cls in expected.items():
            self.assertEqual(contract_errors(cls), [], f"{name} 契约检查失败")
            self.assertEqual(cls.verified, "offline")
            self.assertIn(name, REGISTRY)
            self.assertIs(REGISTRY[name], cls)
            self.assertTrue(cls.blocked(State()), f"{name} 应对未就绪空 State 返回 blocked 原因")
        self.assertNotIn("bridge_segment", REGISTRY)


class BridgeSegmentTests(EndRushTestBase):
    def test_normal_bridge_over_void_and_sneak_lifecycle(self):
        world = EndWorld(pos=(0.5, 64.0, 0.5), blocks={(0, 63, 0): "end_stone"}, items={"cobblestone": 6})
        ex, state = world.session()
        cells = [[1, 63, 0], [2, 63, 0], [3, 63, 0]]
        steps = drive(bridge_segment(ex, state, cells), ex, state)
        self.assertTrue(any(s.get("op") == "place" for s in steps))
        for c in cells:
            self.assertEqual(world.blocks.get(tuple(c)), "cobblestone")
        postures = [req for op, req in world.calls if op == "posture"]
        self.assertTrue(postures[0]["sneak"])
        self.assertFalse(postures[-1]["sneak"])
        self.assertFalse(world.st["sneaking"])

    def test_skips_existing_solid_blocks_and_respects_max_blocks(self):
        blocks = {(0, 63, 0): "end_stone", (1, 63, 0): "end_stone"}
        world = EndWorld(pos=(0.5, 64.0, 0.5), blocks=blocks, items={"cobblestone": 10})
        ex, state = world.session()
        cells = [[1, 63, 0], [2, 63, 0], [3, 63, 0], [4, 63, 0]]
        drive(bridge_segment(ex, state, cells, max_blocks=2), ex, state)
        placed = [req["pos"] for op, req in world.calls if op == "place"]
        self.assertEqual(placed, [[2, 63, 0], [3, 63, 0]])
        self.assertNotIn((4, 63, 0), world.blocks)

    def test_rejects_missing_blocks_stop_loss(self):
        world = EndWorld(pos=(0.5, 64.0, 0.5), blocks={(0, 63, 0): "end_stone"}, items={"cobblestone": 2})
        ex, state = world.session()
        with self.assertRaises(Replan) as ctx:
            drive(bridge_segment(ex, state, [[1, 63, 0], [2, 63, 0], [3, 63, 0]]), ex, state)
        self.assertIn("不足", str(ctx.exception))
        self.assertEqual([op for op, _ in world.calls if op == "place"], [])

    def test_rejects_unsafe_void_starting_footing(self):
        world = EndWorld(pos=(0.5, 64.0, 0.5), blocks={}, items={"cobblestone": 16})
        ex, state = world.session()
        with self.assertRaises(Replan):
            drive(bridge_segment(ex, state, [[1, 63, 0]]), ex, state)
        # on_ground=False 也必须拒绝
        world2 = EndWorld(pos=(0.5, 64.0, 0.5), blocks={(0, 63, 0): "end_stone"}, items={"cobblestone": 16}, on_ground=False)
        ex2, state2 = world2.session()
        with self.assertRaises(Replan):
            drive(bridge_segment(ex2, state2, [[1, 63, 0]]), ex2, state2)

    def test_abort_releases_sneak(self):
        world = EndWorld(pos=(0.5, 64.0, 0.5), blocks={(0, 63, 0): "end_stone"}, items={"cobblestone": 6})
        ex, state = world.session()
        gen = bridge_segment(ex, state, [[1, 63, 0], [2, 63, 0]])
        first = next(gen)
        self.assertEqual(first["op"], "posture")
        self.assertTrue(world.st["sneaking"])
        gen.close()
        self.assertFalse(world.st["sneaking"])


class EnterTheEndTests(EndRushTestBase):
    def test_blocked_empty_bag_wrong_dim_and_memo_override(self):
        w_empty = EndWorld(dimension="overworld", blocks=_flat(-2, 2, -2, 2, block="stone"), items={})
        _, st_empty = w_empty.session()
        self.assertTrue(EnterTheEnd.blocked(st_empty))
        memo_for(st_empty).record_landmark("end_portal", (2, 64, 0), dimension="overworld")
        self.assertEqual(EnterTheEnd.blocked(st_empty), "")

        w_nether = EndWorld(dimension="the_nether", items={"ender_eye": 4})
        _, st_nether = w_nether.session()
        self.assertTrue(EnterTheEnd.blocked(st_nether))

    def test_already_in_the_end_fast_path(self):
        world = EndWorld(dimension="the_end", blocks=_flat(-2, 2, -2, 2))
        ex, state = world.session()
        skill = EnterTheEnd()
        steps = drive(skill, ex, state)
        self.assertEqual(steps, [])
        self.assertEqual(ex.acts, [])
        self.assertEqual(skill.verify_done(state), "")

    def test_fills_frames_enters_portal_and_verifies_end_landing(self):
        blocks = _flat(-3, 5, -3, 3, y=63, block="stone_bricks")
        blocks[(2, 64, 0)] = "end_portal_frame"
        blocks[(3, 64, 0)] = "end_portal_frame"
        props = {(2, 64, 0): {"eye": "false"}, (3, 64, 0): {"eye": "true"}}
        world = EndWorld(pos=(0.5, 64.0, 0.5), dimension="overworld", blocks=blocks, items={"ender_eye": 2}, block_props=props)

        def on_interact(w, *, pos, item, **kwargs):
            cell = tuple(pos)
            self.assertEqual(item, "ender_eye")
            w.items["ender_eye"] -= 1
            w.block_props[cell] = {"eye": "true"}
            w.blocks[(2, 64, 1)] = "end_portal"
            return {"changed": True}

        def on_move_to(w, *, pos, rng, **kwargs):
            if pos and tuple(map(math.floor, pos)) == (2, 64, 1):
                w.st.update(dimension="minecraft:the_end", pos=[100.5, 49.0, 0.5], ready=True, on_ground=True)
                w.blocks = {(100, 48, 0): "obsidian", (101, 48, 0): "obsidian"}
                return {"dimension_changed": {"from": "overworld", "to": "the_end"}, "pos": list(w.st["pos"])}
            return None

        world.on_interact = on_interact
        world.on_move_to = on_move_to
        ex, state = world.session()
        skill = EnterTheEnd(radius=16)
        steps = drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("ender_eye"), 1)
        self.assertTrue(any(s.get("op") == "interact" for s in steps))
        self.assertTrue(any(s.get("op") == "stop" for s in steps))
        memo = memo_for(state)
        self.assertTrue(memo.has_landmark("end_portal_frame", dimension="overworld"))
        self.assertTrue(memo.has_landmark("end_portal", dimension="overworld"))
        self.assertTrue(memo.has_landmark("end_spawn", dimension="the_end"))

    def test_missing_eyes_stop_loss(self):
        blocks = _flat(-2, 4, -2, 2, y=63, block="stone_bricks")
        blocks[(2, 64, 0)] = "end_portal_frame"
        blocks[(3, 64, 0)] = "end_portal_frame"
        props = {(2, 64, 0): {"eye": "false"}, (3, 64, 0): {"eye": "false"}}
        world = EndWorld(pos=(0.5, 64.0, 0.5), dimension="overworld", blocks=blocks, items={"ender_eye": 1}, block_props=props)

        def on_interact(w, *, pos, item, **kwargs):
            w.items["ender_eye"] -= 1
            w.block_props[tuple(pos)] = {"eye": "true"}
            return {"changed": True}

        world.on_interact = on_interact
        ex, state = world.session()
        skill = EnterTheEnd(radius=16)
        with self.assertRaises(Replan) as ctx:
            drive(skill, ex, state)
        self.assertIn("末影之眼不足", str(ctx.exception))
        self.assertTrue(skill.verify_done(state))


class EnterEndGatewayTests(EndRushTestBase):
    def test_blocked_outside_end_or_without_pearl(self):
        _, st_ow = EndWorld(dimension="overworld", items={"ender_pearl": 2}).session()
        self.assertTrue(EnterEndGateway.blocked(st_ow))
        _, st_no_pearl = EndWorld(dimension="the_end", items={}).session()
        self.assertTrue(EnterEndGateway.blocked(st_no_pearl))

    def test_throws_pearl_and_verifies_outer_island_landing(self):
        blocks = _flat(-2, 12, -2, 2, y=63, block="end_stone")
        blocks[(10, 65, 0)] = "end_gateway"
        world = EndWorld(pos=(0.5, 64.0, 0.5), dimension="the_end", blocks=blocks, items={"ender_pearl": 2})

        def on_use_item(w, *, item, target):
            self.assertEqual(item, "ender_pearl")
            self.assertEqual(target, {"pos": [10.5, 65.5, 0.5]})
            w.items["ender_pearl"] -= 1
            w.st.update(pos=[1024.5, 64.0, 12.5], on_ground=True, ready=True)
            w.blocks[(1024, 63, 12)] = "end_stone"
            return {"used": "ender_pearl"}

        world.on_use_item = on_use_item
        ex, state = world.session()
        skill = EnterEndGateway(radius=32)
        steps = drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertTrue(any(s.get("op") == "use_item" and s.get("item") == "ender_pearl" for s in steps))
        self.assertTrue(memo_for(state).has_landmark("end_gateway", dimension="the_end"))
        self.assertTrue(memo_for(state).has_landmark("end_outer_island", dimension="the_end"))

    def test_rejects_no_displacement_and_void_landing(self):
        # 情况 1：珍珠未产生外岛位移
        blocks = _flat(-2, 6, -2, 2, y=63, block="end_stone")
        blocks[(4, 65, 0)] = "end_gateway"
        w1 = EndWorld(pos=(0.5, 64.0, 0.5), dimension="the_end", blocks=blocks, items={"ender_pearl": 2})
        ex1, st1 = w1.session()
        sk1 = EnterEndGateway(radius=16)
        with self.assertRaises(Replan) as ctx1:
            drive(sk1, ex1, st1)
        self.assertIn("外岛位移", str(ctx1.exception))
        self.assertTrue(sk1.verify_done(st1))

        # 情况 2：传送到外岛但脚下虚空
        w2 = EndWorld(pos=(0.5, 64.0, 0.5), dimension="the_end", blocks=dict(blocks), items={"ender_pearl": 2})

        def on_void_tp(w, *, item, target):
            w.items["ender_pearl"] -= 1
            w.st.update(pos=[900.5, 64.0, 0.5], on_ground=True, ready=True)
            return {"used": "ender_pearl"}

        w2.on_use_item = on_void_tp
        ex2, st2 = w2.session()
        sk2 = EnterEndGateway(radius=16)
        with self.assertRaises(Replan) as ctx2:
            drive(sk2, ex2, st2)
        self.assertIn("虚空", str(ctx2.exception))
        self.assertTrue(sk2.verify_done(st2))


class FindEndCityTests(EndRushTestBase):
    def test_blocked_wrong_dim_and_empty_bag(self):
        _, st_ow = EndWorld(dimension="overworld", items={"cobblestone": 16}).session()
        self.assertTrue(FindEndCity.blocked(st_ow))
        _, st_empty = EndWorld(dimension="the_end", items={"cobblestone": 4}).session()
        self.assertTrue(FindEndCity.blocked(st_empty))

    def test_discovers_end_city_and_bridges_void_gap(self):
        blocks = {}
        blocks.update(_flat(-1, 2, -1, 1, y=63, block="end_stone"))
        # x=3,4,5 为虚空缺口；x=6..9 为末地城岛屿
        blocks.update(_flat(6, 9, -1, 1, y=63, block="end_stone_bricks"))
        blocks[(8, 64, 0)] = "purpur_block"
        world = EndWorld(pos=(0.5, 64.0, 0.5), dimension="the_end", blocks=blocks, items={"cobblestone": 16})
        ex, state = world.session()
        skill = FindEndCity(radius=32, max_hops=4)
        steps = drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertTrue(memo_for(state).has_landmark("end_city", dimension="the_end"))
        for gx in (3, 4, 5):
            self.assertEqual(world.blocks.get((gx, 63, 0)), "cobblestone")
        self.assertTrue(any(s.get("op") == "place" for s in steps))

    def test_max_hops_exhaustion_and_stop_losses(self):
        # 孤岛无末地城、仅有珍珠无法搭桥 -> 耗尽 max_hops 报 EnvBlocked
        world = EndWorld(pos=(0.5, 64.0, 0.5), dimension="the_end", blocks=_flat(-1, 1, -1, 1), items={"ender_pearl": 1})
        ex, state = world.session()
        skill = FindEndCity(radius=32, max_hops=2)
        with self.assertRaises(EnvBlocked) as ctx:
            drive(skill, ex, state)
        self.assertEqual(ctx.exception.missing_predicate, "end_city")
        self.assertTrue(skill.verify_done(state))

        # 探索中血量跌破 8 触发止损 Replan（在 session 之后注入 status_seq）
        w_hp = EndWorld(pos=(0.5, 64.0, 0.5), dimension="the_end", blocks=_flat(-1, 10, -1, 1), items={"cobblestone": 16})
        ex_hp, st_hp = w_hp.session()
        w_hp.status_seq = [{"health": 20.0}, {"health": 6.0}]
        with self.assertRaises(Replan) as ctx_hp:
            drive(FindEndCity(radius=32, max_hops=3), ex_hp, st_hp)
        self.assertIn("止损", str(ctx_hp.exception))

        # 探索中方块耗尽触发止损 Replan
        w_low = EndWorld(pos=(0.5, 64.0, 0.5), dimension="the_end", blocks=_flat(-1, 10, -1, 1), items={"cobblestone": 8})
        ex_low, st_low = w_low.session()
        w_low.items["cobblestone"] = 2
        with self.assertRaises(Replan) as ctx_low:
            drive(FindEndCity(radius=32, max_hops=3), ex_low, st_low)
        self.assertIn("缺少搭桥方块", str(ctx_low.exception))


class LevitateFiftyTests(EndRushTestBase):
    def test_blocked_without_rescue_or_low_hp(self):
        _, st_no_rescue = EndWorld(dimension="the_end", items={"cobblestone": 32}, hp=20.0).session()
        self.assertTrue(LevitateFifty.blocked(st_no_rescue))
        _, st_low_hp = EndWorld(dimension="the_end", items={"potion": 1}, hp=10.0).session()
        self.assertTrue(LevitateFifty.blocked(st_low_hp))

    def test_missing_shulker_raises_env_blocked(self):
        world = EndWorld(pos=(0.5, 64.0, 0.5), dimension="the_end", blocks=_flat(-2, 2, -2, 2), items={"potion": 1})
        ex, state = world.session()
        with self.assertRaises(EnvBlocked) as ctx:
            drive(LevitateFifty(radius=16), ex, state)
        self.assertEqual(ctx.exception.missing_predicate, "visible_entity:shulker")

    def test_levitates_fifty_and_drinks_slow_falling_potion(self):
        blocks = _flat(-2, 6, -2, 2, y=63, block="purpur_block")
        shulker = {"id": 42, "name": "shulker", "type": "shulker", "pos": [4.5, 64.0, 0.5]}
        world = EndWorld(pos=(0.5, 64.0, 0.5), dimension="the_end", blocks=blocks, items={"potion": 1}, entities=[shulker])
        ex, state = world.session()
        # _begin status, scan_entities status, _approach_block status -> 然后在漂浮循环中逐步上升 >= 50 格
        world.status_seq = [
            {}, {}, {},
            {"pos": [0.5, 76.0, 0.5], "on_ground": False},
            {"pos": [0.5, 96.0, 0.5], "on_ground": False},
            {"pos": [0.5, 115.0, 0.5], "on_ground": False},
        ]
        skill = LevitateFifty(radius=16)
        steps = drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertTrue(any(s.get("op") == "use_item" and s.get("item") == "potion" for s in steps))
        self.assertEqual(state.count("potion"), 0)

    def test_fallback_rescue_water_bucket_ender_pearl_and_hay_block(self):
        blocks = _flat(-2, 6, -2, 2, y=63, block="purpur_block")
        shulker = {"id": 43, "name": "shulker", "type": "shulker", "pos": [4.5, 64.0, 0.5]}
        for item_name, expected_op in (("water_bucket", "use_item"), ("ender_pearl", "use_item"), ("hay_block", "place")):
            world = EndWorld(pos=(0.5, 64.0, 0.5), dimension="the_end", blocks=dict(blocks), items={item_name: 1}, entities=[shulker])
            ex, state = world.session()
            world.status_seq = [{}, {}, {}, {"pos": [0.5, 116.0, 0.5], "on_ground": False}]
            skill = LevitateFifty(radius=16)
            steps = drive(skill, ex, state)
            self.assertEqual(skill.verify_done(state), "")
            self.assertTrue(any(s.get("op") == expected_op and s.get("item") == item_name for s in steps))

    def test_low_hp_mid_flight_triggers_rescue_and_replan(self):
        blocks = _flat(-2, 6, -2, 2, y=63, block="purpur_block")
        shulker = {"id": 44, "name": "shulker", "type": "shulker", "pos": [4.5, 64.0, 0.5]}
        world = EndWorld(pos=(0.5, 64.0, 0.5), dimension="the_end", blocks=blocks, items={"chorus_fruit": 1}, entities=[shulker])
        ex, state = world.session()
        world.status_seq = [
            {}, {}, {},
            {"pos": [0.5, 80.0, 0.5], "on_ground": False, "health": 6.0},
        ]
        skill = LevitateFifty(radius=16)
        with self.assertRaises(Replan) as ctx:
            drive(skill, ex, state)
        self.assertIn("生命值过低", str(ctx.exception))
        self.assertTrue(any(op == "use_item" and req["item"] == "chorus_fruit" for op, req in world.calls))
        self.assertTrue(skill.verify_done(state))

    def test_timeout_without_fifty_rise_raises_replan(self):
        blocks = _flat(-2, 6, -2, 2, y=63, block="purpur_block")
        shulker = {"id": 45, "name": "shulker", "type": "shulker", "pos": [4.5, 64.0, 0.5]}
        world = EndWorld(pos=(0.5, 64.0, 0.5), dimension="the_end", blocks=blocks, items={"potion": 1}, entities=[shulker])
        ex, state = world.session()
        skill = LevitateFifty(radius=16)
        with self.assertRaises(Replan) as ctx:
            drive(skill, ex, state)
        self.assertIn("超时", str(ctx.exception))
        self.assertTrue(skill.verify_done(state))


if __name__ == "__main__":
    unittest.main()
