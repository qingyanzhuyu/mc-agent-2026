"""T1 工作站、下界、生存与战斗标靶模块收口至 skills.sightline 的离线契约单测（verified: offline）。"""
from __future__ import annotations

import unittest

import mcagent.skills as skills
from mcagent.runtime import EnvBlocked
from mcagent.skills import sightline
from mcagent.skills.combat_scene import _cover, _other_in_shot
from mcagent.skills.enchant import _REACH, _face_reachable, _nearby_blocks
from mcagent.skills.nether_mining import _at_work_site, _line_clear, _work_route
from mcagent.skills.nether_site import _near
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.sightline import STATION_EYE_REACH, block_center
from mcagent.skills.survival import _can_reach, _center, _stand_for
from mcagent.skills.survival_camp import _existing_bed
from mcagent.skills.survival_farm import _gather_wheat, _planting_site
from mcagent.skills.survival_fish import _cast_clear
from mcagent.skills.target import _range_ok
from mcagent.skills.testing import GridWorld, drive


class T1SightlineStationsTests(unittest.TestCase):
    def test_enchant_face_reachable_and_nearby_blocks_respect_sneaking_and_diagonal_pinch(self):
        self.assertEqual(_REACH, STATION_EYE_REACH)

        high_table = (1, 68, 1)  # 站立眼距 ~3.21 <= 3.5，潜行眼距 ~3.53 > 3.5
        world = ScriptedWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(0, 63, 0): "stone", high_table: "enchanting_table"},
        )
        world.st.update(dimension="overworld", sneaking=False)
        ex, state = world.session()
        self.assertTrue(_face_reachable(ex, state.st, high_table))

        world.st["sneaking"] = True
        state.set_status(world.status())
        self.assertFalse(_face_reachable(ex, state.st, high_table))

        world.queue("find_blocks", {"blocks": [{"name": "enchanting_table", "pos": list(high_table)}]})
        self.assertEqual(_nearby_blocks(ex, state, ("enchanting_table",)), [])
        world.assert_exhausted()

        # 对角双石夹缝遮挡不得漏判可达
        pinch_world = GridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "stone",
                (1, 64, 0): "stone",
                (1, 65, 0): "stone",
                (0, 64, 1): "stone",
                (0, 65, 1): "stone",
                (1, 64, 1): "enchanting_table",
            },
        )
        pinch_ex, pinch_state = pinch_world.session()
        self.assertFalse(_face_reachable(pinch_ex, pinch_state.st, (1, 64, 1)))

    def test_nether_near_and_line_clear_respect_sneaking_water_and_solid_occlusion(self):
        standing = {"pos": [0.5, 64.0, 0.5], "sneaking": False}
        sneaking = {"pos": [0.5, 64.0, 0.5], "sneaking": True}

        # (0, 69, 0) 中心 (0.5, 69.5, 0.5)：站立眼距 3.88 <= 4.0，潜行眼距 4.23 > 4.0
        self.assertTrue(_near(standing, (0, 69, 0), radius=4.0))
        self.assertTrue(_near((0.5, 64.0, 0.5), (0, 69, 0), radius=4.0))
        self.assertFalse(_near(sneaking, (0, 69, 0), radius=4.0))

        # (2, 68, 1) 中心 (2.5, 68.5, 1.5)：站立眼距 ~3.65 <= 3.75，潜行眼距 ~3.93 > 3.75
        ledge_ore = (2, 68, 1)
        nether_world = GridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(0, 63, 0): "stone", ledge_ore: "obsidian"},
        )
        nether_world.st.update(dimension="overworld", sneaking=False)
        ex, state = nether_world.session()
        self.assertEqual(_work_route(ex, state.st, ledge_ore), [])
        self.assertTrue(_at_work_site(ex, state, "overworld", ledge_ore))

        nether_world.st["sneaking"] = True
        state.set_status(nether_world.status())
        self.assertIsNone(_work_route(ex, state.st, ledge_ore))
        self.assertFalse(_at_work_site(ex, state, "overworld", ledge_ore))

        # wet=True 允许穿过水体但拦截实心岩块与对角双石缝
        wet_world = GridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={(1, 64, 0): "water", (1, 65, 0): "water", (2, 64, 0): "obsidian"},
        )
        wet_ex, _ = wet_world.session()
        self.assertFalse(_line_clear(wet_ex, sneaking, (2, 64, 0), wet=False))
        self.assertTrue(_line_clear(wet_ex, sneaking, (2, 64, 0), wet=True))

        wet_world.blocks[(1, 64, 0)] = "stone"
        wet_world.blocks[(1, 65, 0)] = "stone"
        self.assertFalse(_line_clear(wet_ex, sneaking, (2, 64, 0), wet=True))

        pinch_world = GridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (1, 64, 0): "stone",
                (1, 65, 0): "stone",
                (0, 64, 1): "stone",
                (0, 65, 1): "stone",
                (1, 64, 1): "obsidian",
            },
        )
        pinch_ex, _ = pinch_world.session()
        self.assertFalse(_line_clear(pinch_ex, standing, (1, 64, 1), wet=True))

    def test_survival_distinguishes_feet_center_from_block_center_and_rejects_walled_blocks(self):
        self.assertEqual(_center((2, 64, 0)), (2.5, 64, 0.5))
        self.assertEqual(block_center((2, 64, 0)), (2.5, 64.5, 0.5))

        standing = {"ready": True, "alive": True, "on_ground": True, "pos": [0.5, 64.0, 0.5], "sneaking": False}
        sneaking = {**standing, "sneaking": True}
        overhead = block_center((0, 69, 0))  # 站立眼距 3.88 <= 4.0，潜行眼距 4.23 > 4.0
        self.assertTrue(_can_reach(standing, overhead, 4.0))
        self.assertFalse(_can_reach(sneaking, overhead, 4.0))

        floor = {(x, 63, z): "stone" for x in range(-2, 6) for z in range(-2, 4)}
        wall = {(1, 64, 0): "stone", (1, 65, 0): "stone"}

        # 1) 隔着 2 格实心石墙的耕地不会被选为原地直达站位 (0.5, 64.0, 0.5)，而是绕到侧翼可见站位
        farm_world = ScriptedWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={**floor, **wall, (2, 63, 0): "farmland"},
            items={"wheat_seeds": 1},
        )
        farm_world.st.update(dimension="overworld")
        farm_world.queue("find_blocks", {"blocks": [{"name": "farmland", "pos": [2, 63, 0]}]})
        farm_ex, farm_state = farm_world.session()
        self.assertNotEqual(
            _stand_for(farm_ex, farm_state.st, block_center((2, 63, 0)), block_pos=(2, 63, 0)),
            (0.5, 64.0, 0.5),
        )
        _, pos, stand, _ = _planting_site(farm_ex, farm_state, "wheat_seeds")
        self.assertEqual(pos, (2, 63, 0))
        self.assertNotEqual(stand, (0.5, 64.0, 0.5))
        farm_world.assert_exhausted()

        # 2) 四周被 2 格实心石墙封死的草丛不得隔墙直挖
        sealed_grass = {(2, 64, 0): "short_grass"}
        for gx, gz in ((1, 0), (3, 0), (2, 1), (2, -1)):
            sealed_grass[(gx, 64, gz)] = "stone"
            sealed_grass[(gx, 65, gz)] = "stone"
        grass_world = ScriptedWorld(pos=(0.5, 64.0, 0.5), blocks={**floor, **sealed_grass})
        grass_world.st.update(dimension="overworld")
        grass_world.queue("find_blocks", {"blocks": [{"name": "short_grass", "pos": [2, 64, 0]}]})
        grass_ex, grass_state = grass_world.session()
        with self.assertRaises(EnvBlocked):
            drive(_gather_wheat(grass_ex, grass_state, 1), grass_ex)
        self.assertEqual(grass_ex.acts, [])
        grass_world.assert_exhausted()

        # 3) 四周被 2 格实心石墙封死的床不会被当成原地可达床位
        sealed_bed = {(2, 64, 0): "white_bed", (3, 64, 0): "white_bed"}
        for bx, bz in ((1, 0), (4, 0), (2, 1), (2, -1), (3, 1), (3, -1)):
            sealed_bed[(bx, 64, bz)] = "stone"
            sealed_bed[(bx, 65, bz)] = "stone"
        bed_world = ScriptedWorld(pos=(0.5, 64.0, 0.5), blocks={**floor, **sealed_bed})
        bed_world.st.update(dimension="overworld")
        bed_world.queue("find_blocks", {"blocks": [{"name": "white_bed", "pos": [2, 64, 0]}]})
        bed_ex, bed_state = bed_world.session()
        self.assertIsNone(_existing_bed(bed_ex, bed_state))
        bed_world.assert_exhausted()

        # 4) 钓鱼抛竿视线支持潜行眼高：顶梁 (1, 65, 0) 阻挡站立视线但允许潜行从下方抛向 (2, 63, 0) 水面
        fish_world = GridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={**floor, (2, 63, 0): "water", (1, 65, 0): "stone"},
        )
        fish_ex, _ = fish_world.session()
        self.assertFalse(_cast_clear(fish_ex, standing, (2, 63, 0)))
        self.assertTrue(_cast_clear(fish_ex, sneaking, (2, 63, 0)))

    def test_combat_scene_and_target_unify_standing_and_sneaking_eye_pos(self):
        standing = {"pos": [0.5, 64.0, 0.5], "sneaking": False}
        sneaking = {"pos": [0.5, 64.0, 0.5], "sneaking": True}

        # (0.5, 65.62, 0.5) -> (1.5, 66.39, 0.5) 先穿过 (0, 66, 0)，而旧硬编码 +1.60 与潜行 +1.27 均走 (1, 65, 0)
        cover_world = GridWorld(pos=(0.5, 64.0, 0.5), blocks={(0, 66, 0): "stone"})
        cover_ex, _ = cover_world.session()
        self.assertTrue(_cover(cover_ex, (0.5, 64.0, 0.5), (1.5, 66.39, 0.5)))
        self.assertTrue(_cover(cover_ex, standing, (1.5, 66.39, 0.5)))
        self.assertFalse(_cover(cover_ex, sneaking, (1.5, 66.39, 0.5)))

        bystanders = [
            {"id": 91, "type": "hostile", "name": "skeleton", "pos": [10.5, 64.77, 0.5]},
            {"id": 3, "type": "player", "name": "ally", "pos": [5.5, 66.30, 0.5]},
        ]
        aim = (10.5, 65.27, 0.5)
        self.assertTrue(_other_in_shot(bystanders, 91, standing, aim, 0))
        self.assertFalse(_other_in_shot(bystanders, 91, sneaking, aim, 0))

        high_bullseye = (31.5, 87.40, 0.5)  # 站立眼距 ~37.89 <= 38，潜行眼距 ~38.09 > 38
        self.assertTrue(_range_ok(standing, high_bullseye))
        self.assertFalse(_range_ok(sneaking, high_bullseye))

    def test_skills_package_exports_sightline(self):
        self.assertIn("sightline", skills.__all__)
        self.assertIs(skills.sightline, sightline)
        self.assertIn("sightline.py", skills.__doc__ or "")


if __name__ == "__main__":
    unittest.main()
