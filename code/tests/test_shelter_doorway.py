"""出生点避难所"门口"行为回归测试。

实测事故（2026-11-24 真机 150 步）：`RECIPES` 里没有门配方、背包也从没有门，旧逻辑
在 else 分支把门口两格用圆石封死 —— 5×5 避难所变成没有门的盒子，bot 被自己关在里面，
只能挖墙出去。现在改为：有门装门，没门保留敞开出入口。
"""
from __future__ import annotations

import unittest

from mcagent.skills.items import bare
from mcagent.skills.spawn_shelter import BuildSpawnShelter, shelter_blueprint
from mcagent.skills.testing import drive
from mcagent.skills.world_memo import memo_for

from tests.test_sprint15_shelter_and_fixes import _Sprint15World, _flat_ground


def _build(items):
    blocks = _flat_ground(0, 64, 0, radius=14)
    world = _Sprint15World(pos=(0.5, 64.0, 0.5), blocks=blocks, items=dict(items))
    ex, state = world.session()
    memo_for(state).set_spawn([0, 64, 0], dimension="overworld")
    skill = BuildSpawnShelter(mode="tier1_shelter")
    steps = drive(skill, ex, state)
    return world, state, skill, steps


def _blueprint_from_camp(state):
    """营地记录里 `mine_entry = (cx, cy-1, cz+1)`，据此还原蓝图中心。"""
    camp = memo_for(state).get_camp(dimension="overworld")
    mx, my, mz = camp["mine_entry"]
    return shelter_blueprint(mx, my + 1, mz - 1)


class ShelterDoorwayTest(unittest.TestCase):
    def test_without_door_doorway_stays_open(self):
        world, state, skill, steps = _build({
            "cobblestone": 64, "chest": 3, "furnace": 1, "crafting_table": 1, "torch": 2, "white_bed": 1,
        })
        self.assertEqual(skill.verify_done(state), "")
        camp = memo_for(state).get_camp(dimension="overworld")
        self.assertGreaterEqual(camp["shelter_tier"], 1)

        bp = _blueprint_from_camp(state)
        for cell in bp["door_cells"]:
            self.assertEqual(bare(world.blocks.get(tuple(cell), "air")), "air", f"门口 {cell} 被砌死了")
        self.assertTrue(
            any(s.get("op") == "doorway_open" for s in steps),
            "没有门时应显式记录保留敞开出入口",
        )

    def test_with_door_installs_it(self):
        world, state, skill, steps = _build({
            "cobblestone": 64, "chest": 3, "furnace": 1, "crafting_table": 1, "torch": 2,
            "white_bed": 1, "oak_door": 1,
        })
        self.assertEqual(skill.verify_done(state), "")
        bp = _blueprint_from_camp(state)
        self.assertEqual(bare(world.blocks.get(tuple(bp["door_cells"][0]), "air")), "oak_door")


if __name__ == "__main__":
    unittest.main()
