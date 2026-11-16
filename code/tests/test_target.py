"""P6 标靶契约。射击回执、扣弹与真实进度分别由用例控制。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills.recipes import RECIPES
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.target import HitBullseye
from mcagent.skills.testing import drive


_TARGET = (32, 64, 0)
_TABLE = (0, 64, 2)
_ADV = "minecraft:adventure/bullseye"


def _find(*matches):
    return {"blocks": [{"name": name, "pos": list(pos)} for name, pos in matches]}


def _progress(done=False):
    return {"ready": True, "advancements": [{"id": _ADV, "status": "completed"}] if done else []}


def _fire(world, *, pos, weapon, **kwargs):
    world.items["arrow"] -= 1
    return {"fired": True, "aim": {"predicted_miss": 0.0}}


class TargetWorld(ScriptedWorld):
    def __init__(self, *, items=None, target=_TARGET):
        ground = {(x, 63, z): "stone" for x in range(-34, 35) for z in range(-2, 3)}
        if target is not None:
            ground[target] = "target"
        super().__init__(blocks=ground, items={"bow": 1, "arrow": 2} if items is None else items)
        self.st["dimension"] = "overworld"

    def stop(self):
        return self._reply("stop", {}, fallback=super().stop)


class TargetTests(unittest.TestCase):
    def test_uses_observed_block_face_center_and_requires_advancement(self):
        world = TargetWorld()
        world.queue("advancements", _progress(), _progress(), _progress(True))
        world.queue("find_blocks", _find(("target", _TARGET)))
        world.queue("shoot", _fire)
        ex, state = world.session()
        steps = drive(HitBullseye(), ex, state)
        actual, = [call for method, call in world.calls if method == "shoot"]
        self.assertEqual(actual["pos"], [32.01, 64.5, 0.5])
        self.assertEqual(actual["stop_on"], ("hurt", "death"))
        self.assertEqual(ex.acts, ["shoot"])
        self.assertEqual(steps[-2]["ammo_spent"], {"arrow": 1})
        self.assertGreaterEqual(steps[-2]["horizontal"], 30)
        self.assertTrue(steps[-1]["advancement_confirmed"])
        world.assert_exhausted()

    def test_crafts_target_walks_observed_corridor_and_fires_once(self):
        world = TargetWorld(items={"bow": 1, "arrow": 2, "redstone": 4, "hay_block": 1}, target=None)
        world.blocks[_TABLE] = "crafting_table"
        world.queue("advancements", _progress(), _progress(), _progress(True))
        world.queue("find_blocks", _find(), _find(("crafting_table", _TABLE)), _find())

        def craft(world, item, count, **kwargs):
            for ingredient, need in RECIPES[item].ingredients:
                world.items[ingredient] -= count * need
            world.items[item] = 1
            return {"crafted": item}

        world.queue("craft", craft)
        world.queue("shoot", _fire)
        ex, state = world.session()
        drive(HitBullseye(), ex, state)
        self.assertEqual(ex.acts[:2], ["craft", "place"])
        self.assertIn("move_to", ex.acts)
        self.assertEqual(ex.acts[-1], "shoot")
        self.assertEqual(ex.acts.count("shoot"), 1)
        self.assertEqual(world.blocks[(1, 64, 0)], "target")
        world.assert_exhausted()

    def test_crossbow_without_compatible_ammo_and_missing_weapon_replan(self):
        for items in ({"crossbow": 1, "firework_rocket": 1}, {"arrow": 1},
                      {"bow": 1, "arrow": 1, "spectral_arrow": 1}):
            with self.subTest(items=items):
                world = TargetWorld(items=items)
                world.queue("advancements", _progress())
                ex, state = world.session()
                with self.assertRaisesRegex(Replan, "弓弩和箭矢"):
                    drive(HitBullseye(), ex, state)
                self.assertEqual(ex.acts, [])

    def test_short_range_or_occlusion_sets_environment_blocked(self):
        for target, wall in (((29, 64, 0), False), (_TARGET, True)):
            with self.subTest(target=target, wall=wall):
                world = TargetWorld(target=target)
                world.blocks = {target: "target", (0, 63, 0): "stone", (1, 63, 0): "stone"}
                if wall:
                    world.blocks[(8, 65, 0)] = "stone"
                world.queue("advancements", _progress())
                world.queue("find_blocks", _find(("target", target)))
                ex, state = world.session()
                with self.assertRaises(EnvBlocked) as caught:
                    drive(HitBullseye(), ex, state)
                self.assertEqual(caught.exception.missing_predicate, "safe_bullseye_lane")
                self.assertIn("半径 40 格", caught.exception.searched_volume)
                self.assertEqual(caught.exception.wake_on, "chunk_moved")
                self.assertEqual(ex.acts, [])

    @patch("mcagent.skills.encounter.time.sleep")
    def test_fired_success_and_predicted_hit_cannot_replace_achievement(self, sleep):
        world = TargetWorld()
        world.queue("advancements", *[_progress()] * 5)
        world.queue("find_blocks", _find(("target", _TARGET)))
        world.queue("shoot", _fire)
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "进度.*未确认"):
            drive(HitBullseye(), ex, state)
        self.assertEqual(ex.acts, ["shoot"])
        sleep.assert_called()
        world.assert_exhausted()

    def test_multiple_arrow_debit_stops_before_claiming_progress(self):
        world = TargetWorld()
        world.queue("advancements", _progress(), _progress())
        world.queue("find_blocks", _find(("target", _TARGET)))

        def double_debit(world, **kwargs):
            world.items["arrow"] -= 2
            return {"fired": True}

        world.queue("shoot", double_debit)
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "单箭物资不一致"):
            drive(HitBullseye(), ex, state)
        self.assertEqual(ex.acts, ["shoot"])
        world.assert_exhausted()

    def test_already_recorded_goal_never_shoots_again(self):
        world = TargetWorld(items={})
        world.queue("advancements", _progress(True))
        ex, state = world.session()
        self.assertEqual(drive(HitBullseye(), ex, state), [])
        self.assertEqual(ex.acts, [])
        world.assert_exhausted()

    def test_abort_after_shoot_stops_observed_lingering_draw(self):
        world = TargetWorld()
        world.queue("advancements", _progress(), _progress())
        world.queue("find_blocks", _find(("target", _TARGET)))

        def left_drawing(world, **kwargs):
            world.st["using_item"] = {"name": "bow", "elapsed_ms": 1}
            return {"fired": True}

        def stop(world):
            world.st["using_item"] = None
            return {}

        world.queue("shoot", left_drawing)
        world.queue("stop", stop)
        ex, state = world.session()
        skill = HitBullseye()
        self.assertFalse(skill.step(ex, state))
        skill.abort()
        self.assertEqual(ex.acts, ["shoot"])
        self.assertIsNone(world.st["using_item"])
        self.assertEqual([method for method, _ in world.calls if method == "stop"], ["stop"])
        world.assert_exhausted()

    def test_material_plan_counts_arrow_and_nine_wheat_only_on_craft_path(self):
        world = TargetWorld(items={"crossbow": 1, "arrow": 2, "wheat": 9, "redstone": 4})
        _, state = world.session()
        self.assertEqual(set(HitBullseye.material_plan({}, state)), {
            ("crossbow", 1, "borrow"), ("arrow", 1, "consume"),
            ("wheat", 9, "consume"), ("redstone", 4, "consume"),
        })

    @patch("mcagent.skills.target._ENABLE_TRAPDOOR_DROP", True)
    def test_falling_arrow_flag_has_no_side_effects(self):
        world = TargetWorld()
        world.queue("advancements", _progress())
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "落箭路线待实测"):
            drive(HitBullseye(), ex, state)
        self.assertEqual(ex.acts, [])
