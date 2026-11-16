"""starter_skills 接入共享世界资源台账 skills.world_memo 离线回归单测（verified: offline）。"""
from __future__ import annotations

import unittest

from mcagent.client import ApiError
from mcagent.loop import reset_world_memo
from mcagent.loop.starter_skills import (
    KNOWN_TABLES,
    PROTECTED_ZONES,
    VISITED_ANCHORS,
    _mark_protected,
    has_known_table,
)
from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import drive
from mcagent.skills.world_memo import memo_for
from tests.test_loop_soak import RichGridWorld


class MemoSkillWorld(RichGridWorld):
    """支持按方块类型屏蔽实时扫描、注入寻路失败与追踪 get_block 读取的离线夹具。"""

    def __init__(
        self,
        *,
        pos=(0.5, 64.0, 0.5),
        blocks=None,
        items=None,
        move_failures=None,
        hide_typed_find_blocks: bool = False,
    ):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.move_failures = {tuple(k): v for k, v in (move_failures or {}).items()}
        self.hide_typed_find_blocks = hide_typed_find_blocks
        self.find_calls: list[dict] = []
        self.block_reads: list[tuple[int, int, int]] = []

    def get_block(self, pos):
        self.block_reads.append((int(pos[0]), int(pos[1]), int(pos[2])))
        return super().get_block(pos)

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None) -> dict:
        self.find_calls.append({"types": types, "radius": radius, "limit": limit})
        if self.hide_typed_find_blocks and types and any("log" in t for t in types):
            self.calls.append(("find_blocks", {"types": types, "radius": radius}))
            return {"total": 0, "blocks": []}
        return super().find_blocks(types=types, radius=radius, limit=limit, properties=properties, mature=mature)

    def move_to(self, *args, **kwargs):
        pos = kwargs.get("pos")
        key = tuple(pos) if pos is not None else None
        if key in self.move_failures:
            self.calls.append(("move_to", {"pos": list(pos)}))
            raise ApiError(self.move_failures[key], "injected memo skill navigation failure")
        return super().move_to(*args, **kwargs)


class WorldMemoSkillsIntegrationTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_gather_wood_expands_to_radius_64_when_no_logs_within_32(self):
        world = MemoSkillWorld(blocks={(35, 64, 0): "spruce_log"})
        ex, state = world.session()

        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "dig"])
        self.assertEqual(steps[0]["pos"], [35, 64, 0])
        self.assertEqual(steps[1]["pos"], [35, 64, 0])
        self.assertEqual(state.count("spruce_log"), 1)

        radii = [c["radius"] for c in world.find_calls if c["types"]]
        self.assertEqual(radii, [32, 64])
        # 原木被砍下并核验消失后不得在台账留下残影
        self.assertTrue(memo_for(state).is_blocked((35, 64, 0), category="wood"))
        self.assertEqual(memo_for(state).query("wood"), [])

    def test_scout_island_ingests_summary_and_steers_and_gather_wood_reuses_memo(self):
        world = MemoSkillWorld(
            blocks={
                (40, 64, 0): "spruce_log",
                (45, 63, 0): "lava",
                (42, 64, 0): "hay_block",
                (-24, 63, 0): "grass_block",
                (20, 63, 0): "grass_block",
            }
        )
        ex, state = world.session()

        scout_steps = drive(make("scout_island", {"radius": 48}), ex, state)
        self.assertEqual(scout_steps[0]["fingerprints"], {"village": ["hay_block"]})
        # 背包无原木且已知云杉距当前 >28 格：同惩罚组优先朝 40 格外已知树推进
        self.assertEqual(scout_steps[0]["pos"], [20.5, 64, 0.5])

        memo = memo_for(state)
        self.assertEqual(memo.query("wood")[0].pos, (40, 64, 0))
        self.assertEqual(memo.query("lava")[0].pos, (45, 63, 0))
        self.assertEqual(memo.query("structure")[0].tag, "village")

        # 模拟回退或实时按类型扫描返回空：GatherWood 仍可直接复用台账走过去砍下原木
        world.st["pos"] = [0.5, 64.0, 0.5]
        state.set_status(world.status())
        world.hide_typed_find_blocks = True
        ex.acts.clear()

        wood_steps = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in wood_steps], ["move_to", "dig"])
        self.assertEqual(wood_steps[0]["pos"], [40, 64, 0])
        self.assertEqual(state.count("spruce_log"), 1)
        self.assertTrue(memo.is_blocked((40, 64, 0), category="wood"))

    def test_gather_wood_reusing_memo_marks_depleted_on_missing_tree_and_switches(self):
        # 1) >36 格远端缓存原木已消失：走过去后 get_block 复核发现空气 -> mark_depleted，不浪费 dig，换下一棵
        world = MemoSkillWorld(
            blocks={(45, 64, 0): "oak_log"},
            hide_typed_find_blocks=True,
        )
        ex, state = world.session()
        memo = memo_for(state)
        memo.record_seen("wood", "oak_log", [40, 64, 0], dist=40.0)
        memo.record_seen("wood", "oak_log", [45, 64, 0], dist=45.0)

        steps = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "move_to", "dig"])
        self.assertEqual([s["pos"] for s in steps], [[40, 64, 0], [45, 64, 0], [45, 64, 0]])
        self.assertEqual([c for c in world.calls if c[0] == "dig"], [("dig", {"pos": [45, 64, 0]})])
        self.assertIn((40, 64, 0), world.block_reads)
        self.assertTrue(memo.is_blocked((40, 64, 0), category="wood"))
        self.assertEqual(state.count("oak_log"), 1)

        # 2) <=36 格缓存原木已消失：原位复核即可直接 mark_depleted，连 move_to 也省掉
        reset_world_memo()
        _COOLDOWN.clear()
        world2 = MemoSkillWorld(blocks={(18, 64, 0): "birch_log"}, hide_typed_find_blocks=True)
        ex2, state2 = world2.session()
        memo2 = memo_for(state2)
        memo2.record_seen("wood", "birch_log", [10, 64, 0], dist=10.0)
        memo2.record_seen("wood", "birch_log", [18, 64, 0], dist=18.0)

        steps2 = drive(make("gather_wood", {"count": 1}), ex2, state2)
        self.assertEqual([s["op"] for s in steps2], ["move_to", "dig"])
        self.assertEqual(steps2[0]["pos"], [18, 64, 0])
        self.assertTrue(memo2.is_blocked((10, 64, 0), category="wood"))
        self.assertEqual(state2.count("birch_log"), 1)

    def test_gather_wood_marks_unreachable_and_does_not_retry_on_reentry(self):
        world = MemoSkillWorld(
            blocks={(6, 64, 0): "oak_log", (14, 64, 0): "oak_log"},
            move_failures={(6, 64, 0): "path_not_found"},
        )
        ex, state = world.session()

        steps1 = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps1], ["move_to", "move_to", "dig"])
        self.assertEqual(steps1[0]["error_code"], "path_not_found")
        self.assertEqual(steps1[1]["pos"], [14, 64, 0])
        self.assertTrue(memo_for(state).is_blocked((6, 64, 0), category="wood"))

        # 重置回原点且只留不可达旧树 + 更远的新树：第二次重入 GatherWood 绝不重复撞 [6, 64, 0]
        world.st["pos"] = [0.5, 64.0, 0.5]
        state.set_status(world.status())
        world.blocks[(18, 64, 0)] = "oak_log"
        ex.acts.clear()

        steps2 = drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual([s["op"] for s in steps2], ["move_to", "dig"])
        self.assertEqual(steps2[0]["pos"], [18, 64, 0])
        self.assertEqual(state.count("oak_log"), 2)

        # 若此时只剩被拉黑的 [6, 64, 0]：再次重入零动作直接抛出 EnvBlocked
        world.st["pos"] = [0.5, 64.0, 0.5]
        state.set_status(world.status())
        ex.acts.clear()
        with self.assertRaises(EnvBlocked) as caught:
            drive(make("gather_wood", {"count": 1}), ex, state)
        self.assertEqual(ex.acts, [])
        self.assertEqual(caught.exception.missing_predicate, "exposed_log")
        self.assertEqual(caught.exception.wake_on, "chunk_moved")

    def test_gather_wood_raises_env_blocked_when_radius_64_and_memo_empty(self):
        world = MemoSkillWorld(blocks={(10, 63, 0): "grass_block"})
        ex, state = world.session()

        with self.assertRaises(EnvBlocked) as caught:
            drive(make("gather_wood", {"count": 2}), ex, state)

        err = caught.exception
        self.assertIsInstance(err, Replan)
        self.assertIn("没有可砍的裸露原木", str(err))
        self.assertEqual(err.missing_predicate, "exposed_log")
        self.assertEqual(err.wake_on, "chunk_moved")
        self.assertIn("64", err.searched_volume)
        self.assertEqual([c["radius"] for c in world.find_calls if c["types"]], [32, 64])
        self.assertEqual(ex.acts, [])

    def test_scout_island_breaks_10_to_13_block_triangle_ping_pong(self):
        # 构造 10-13 格三角锚点：A(0,0)、B(11,0)、C(6,10)，以及更近但远离 A/B 的外部未访点 D(15,16)
        world = MemoSkillWorld(
            pos=(6.5, 64.0, 10.5),
            blocks={
                (0, 63, 0): "grass_block",
                (11, 63, 0): "grass_block",
                (6, 63, 10): "grass_block",
                (15, 63, 16): "grass_block",
            },
        )
        ex, state = world.session()
        VISITED_ANCHORS.extend([(0.5, 0.5), (11.5, 0.5), (6.5, 10.5)])

        steps = drive(make("scout_island", {"radius": 24}), ex, state)
        # 即使 D 距 C 仅 10.1 格（比 A=12.1 格、B=11.4 格更近），三角内 A/B 均受 <14 格叠加惩罚，必选外部点 D
        self.assertEqual(steps[0]["pos"], [15.5, 64, 16.5])

    def test_scout_island_consecutive_steps_escape_triangle_to_outer_ground(self):
        world = MemoSkillWorld(
            pos=(6.5, 64.0, 10.5),
            blocks={
                (0, 63, 0): "grass_block",
                (11, 63, 0): "grass_block",
                (6, 63, 10): "grass_block",
                (16, 63, 24): "grass_block",
                (30, 63, 16): "grass_block",
                (26, 63, -4): "grass_block",
            },
        )
        ex, state = world.session()
        triangle = [(0.5, 0.5), (11.5, 0.5), (6.5, 10.5)]
        VISITED_ANCHORS.extend(triangle)

        visited = []
        for _ in range(3):
            steps = drive(make("scout_island", {"radius": 28}), ex, state)
            visited.append((steps[0]["pos"][0], steps[0]["pos"][2]))
        # 连续 3 跳全部落在外围未访问陆地，绝不回到 10-13 格三角区内乒乓
        self.assertEqual(len(set(visited)), 3)
        for pt in visited:
            self.assertNotIn(pt, triangle)

    def test_scout_island_does_not_steer_when_close_wood_or_inventory_logs_present(self):
        # 1) 近处 6 格已有白桦树时，不因远处树干扰原有探索排序
        world_near = MemoSkillWorld(
            blocks={
                (6, 64, 0): "birch_log",
                (40, 64, 0): "spruce_log",
                (12, 63, 0): "grass_block",
                (-18, 63, 0): "grass_block",
            }
        )
        ex_near, state_near = world_near.session()
        steps_near = drive(make("scout_island", {"radius": 48}), ex_near, state_near)
        self.assertEqual(steps_near[0]["pos"], [-17.5, 64, 0.5])

        # 2) 背包已有原木时，即使只有 40 格远树，也不强制朝远树转向
        reset_world_memo()
        _COOLDOWN.clear()
        world_bag = MemoSkillWorld(
            blocks={
                (40, 64, 0): "spruce_log",
                (20, 63, 0): "grass_block",
                (-24, 63, 0): "grass_block",
            },
            items={"oak_log": 1},
        )
        ex_bag, state_bag = world_bag.session()
        steps_bag = drive(make("scout_island", {"radius": 48}), ex_bag, state_bag)
        self.assertEqual(steps_bag[0]["pos"], [-23.5, 64, 0.5])

    def test_craft_gear_reuses_memo_table_within_48_and_handles_depleted_or_unreachable(self):
        # 1) radius=16 外但 <=48 格的已知工作台：直接走过去复用，无需消耗木板重造工作台
        world = MemoSkillWorld(
            blocks={(30, 64, 0): "crafting_table"},
            items={"stick": 2, "cobblestone": 3},
        )
        ex, state = world.session()
        memo_for(state).record_seen("station", "crafting_table", [30, 64, 0], dist=30.0)

        steps = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "craft"])
        self.assertEqual(steps[0]["pos"], [30, 64, 0])
        self.assertEqual(state.count("stone_pickaxe"), 1)
        self.assertIn((30.0, 64.0, 0.0), KNOWN_TABLES)
        self.assertEqual(memo_for(state).query("station")[0].status, "reachable")

        # 2) 台账工作台已被打掉：自动标记 depleted 后原地造新工作台完成合成
        reset_world_memo()
        _COOLDOWN.clear()
        world2 = MemoSkillWorld(
            blocks={},
            items={"oak_planks": 4, "stick": 2, "cobblestone": 3},
        )
        ex2, state2 = world2.session()
        memo2 = memo_for(state2)
        memo2.record_seen("station", "crafting_table", [30, 64, 0], dist=30.0)

        steps2 = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex2, state2)
        self.assertEqual([s["op"] for s in steps2], ["craft", "place", "craft"])
        self.assertTrue(memo2.is_blocked((30, 64, 0), category="station"))
        self.assertEqual(state2.count("stone_pickaxe"), 1)
        self.assertTrue(has_known_table(state2.pos))

        # 3) 寻路失败的工作台标记 unreachable 后，下一次重入不再重复尝试撞该工作台
        reset_world_memo()
        _COOLDOWN.clear()
        world3 = MemoSkillWorld(
            blocks={(6, 66, 0): "crafting_table"},
            items={"oak_planks": 4, "stick": 2, "cobblestone": 3},
            move_failures={(6, 66, 0): "path_not_found"},
        )
        ex3, state3 = world3.session()
        steps3 = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex3, state3)
        self.assertEqual([s["op"] for s in steps3], ["move_to", "craft", "place", "craft"])
        self.assertTrue(memo_for(state3).is_blocked((6, 66, 0), category="station"))

        world3.items.update({"stick": 2, "cobblestone": 3, "oak_planks": 4})
        state3.invalidate_inventory()
        world3.blocks.pop((1, 64, 0), None)
        ex3.acts.clear()
        steps3_retry = drive(make("craft_gear", {"item": "stone_pickaxe"}), ex3, state3)
        self.assertEqual([s["op"] for s in steps3_retry], ["craft", "place", "craft"])
        self.assertNotIn("move_to", ex3.acts)

    def test_protected_region_in_gather_wood_and_mine_stone_syncs_to_memo(self):
        # MineStone 遇到 protected_region 同步记入 memo 与 PROTECTED_ZONES
        world_stone = MemoSkillWorld(
            blocks={(2, 64, 0): "stone", (14, 63, 0): "grass_block"},
            items={"wooden_pickaxe": 1},
        )
        world_stone.st["held"] = {"name": "wooden_pickaxe"}
        world_stone.protected_blocks.add((2, 64, 0))
        ex_stone, state_stone = world_stone.session()

        with self.assertRaisesRegex(Replan, "保护区"):
            drive(make("mine_stone", {"count": 1}), ex_stone, state_stone)
        self.assertTrue(memo_for(state_stone).is_blocked((2, 64, 0)))
        self.assertTrue(PROTECTED_ZONES)

        # GatherWood 遇到 protected_region 同步记入 memo("wood") 并保留在 PROTECTED_ZONES
        reset_world_memo()
        _COOLDOWN.clear()
        world_wood = MemoSkillWorld(blocks={(2, 64, 0): "oak_log"})
        world_wood.protected_blocks.add((2, 64, 0))
        ex_wood, state_wood = world_wood.session()

        with self.assertRaisesRegex(Replan, "保护区"):
            drive(make("gather_wood", {"count": 1}), ex_wood, state_wood)
        self.assertTrue(memo_for(state_wood).is_blocked((2, 64, 0), category="wood"))
        self.assertEqual(memo_for(state_wood).query("wood"), [])

    def test_dimension_isolation_and_reset_world_memo_contract(self):
        world = MemoSkillWorld(
            blocks={
                (40, 64, 0): "spruce_log",
                (20, 63, 0): "grass_block",
                (-24, 63, 0): "grass_block",
            },
            hide_typed_find_blocks=True,
        )
        ex, state = world.session()
        memo = memo_for(state)
        memo.record_seen("wood", "spruce_log", [40, 64, 0], dimension="overworld", dist=40.0)

        # 切换到下界后：overworld 的原木记录不会诱偏 ScoutIsland，也不会被 GatherWood 复用
        world.st["dimension"] = "the_nether"
        state.set_status(world.status())

        scout_steps = drive(make("scout_island", {"radius": 32}), ex, state)
        self.assertEqual(scout_steps[0]["pos"], [-23.5, 64, 0.5])

        with self.assertRaises(EnvBlocked):
            drive(make("gather_wood", {"count": 1}), ex, state)

        # reset_world_memo() 必须同时清空旧全局变量与所有共享台账
        _mark_protected([2, 64, 2])
        KNOWN_TABLES.append((3.0, 64.0, 0.0))
        self.assertGreater(len(memo), 0)
        reset_world_memo()
        self.assertEqual(PROTECTED_ZONES, [])
        self.assertEqual(VISITED_ANCHORS, [])
        self.assertEqual(KNOWN_TABLES, [])
        self.assertEqual(len(memo), 0)


if __name__ == "__main__":
    unittest.main()
