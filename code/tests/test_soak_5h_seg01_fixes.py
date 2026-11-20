"""5h soak seg-01 故障回归离线单测（verified: offline）。

覆盖 seg-01 实测确认的三类根因：
1. `smelt_iron` 进度已达成后石镐耗尽（或死后清包），下游目标（如 `obtain_armor` / `iron_tools`）
   的补料技能 `smelt_iron` 被 `blocked()` 阻塞时，策略层应沿该技能对应目标的 `prereq_goals`
   自动恢复未满足的 `must_hold_for_children` 工具/工作站前置（如 `upgrade_tools` 补石镐）。
2. `SmeltIron._prospect()` 在起挖或单步移动后若角色尚处于未落地/位置回弹过渡态，
   不应因 `mine_step` 抛出「阶梯落点须位于现场站位相邻方向且低一格」直接中止整条矿道，
   而应先收敛站位并按最新站位重新观测阶梯落点。
3. `SmeltIron._mine()` 在 `move_to` / `clear_line_of_sight` 可能导致服务端自动切换手持物品
   （如 `wooden_hoe`）后，开挖目标铁矿前必须无条件复核并重新装备合规镐子，避免 `INC-005`。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcagent.loop import RulePlanner, reset_world_memo
from mcagent.loop.goals import GOAL_STAGES
from mcagent.runtime import Replan, State
from mcagent.skills import base as skill_base
from mcagent.skills.iron import SmeltIron, reset_iron_memo
from mcagent.skills.mining import MineStepUnsafe, mine_step
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches
from mcagent.skills.world_memo import reset_all_world_memos
from tests.test_iron_reach_integration import _ReachIronWorld, _furnace_view


class _TransientStanceProspectWorld(_ReachIronWorld):
    """复现 seg-01 t=112s/180s：首拍 status 为未落地瞬态位置，经 safe_mine_column 探测后回弹至稳态格。"""

    def __init__(self) -> None:
        # 地下实心岩层 y=16..23，留出 (-54, 20..21, 18) 稳态站位空气
        blocks: dict[tuple[int, int, int], str] = {}
        for x in range(-57, -50):
            for z in range(15, 22):
                for y in range(16, 24):
                    blocks[(x, y, z)] = "stone"
        for y in (20, 21):
            blocks.pop((-54, y, 18), None)
        # 挖开稳态邻格 (-53, 19, 18) 阶梯后，在 (-52, 19, 18) 暴露铁矿
        blocks[(-52, 19, 18)] = "iron_ore"
        super().__init__(
            pos=(-53.5, 19.9, 17.5),
            items={"stone_pickaxe": 1, "furnace": 1, "coal": 2},
            blocks=blocks,
            views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
            track_held=True,
        )
        self.st["held"] = {"name": "stone_pickaxe"}
        self.st["on_ground"] = False
        self.st["velocity"] = [0.0, -1.568, -4.1]
        self._block_reads = 0
        self._snapped = False
        self._ore_exposed = False

    def get_block(self, pos):
        self._block_reads += 1
        # 模拟 safe_mine_column 探测期间服务端将角色回弹落地到 (-54, 20, 18)
        if not self._snapped and self._block_reads >= 5:
            self._snapped = True
            self.st["pos"] = [-53.5, 20.0, 18.3]
            self.st["on_ground"] = True
            self.st["velocity"] = [0.0, 0.0, 0.0]
        return super().get_block(pos)

    def dig(self, pos, collect=True, *, stop_on=None):
        res = super().dig(pos, collect=collect, stop_on=stop_on)
        if tuple(pos) == (-53, 19, 18):
            self._ore_exposed = True
        return res

    def find_blocks(self, types=None, radius=16, limit=20):
        if types and "iron_ore" in types and not self._ore_exposed:
            self.calls.append(("find_blocks", {"types": types, "radius": radius}))
            return {"blocks": []}
        return super().find_blocks(types=types, radius=radius, limit=limit)


class _ToolSwapOnMoveWorld(_ReachIronWorld):
    """复现 seg-01 t=1664.7s (INC-005)：先清视线装备石镐后，move_to 自动切换手持为 wooden_hoe。"""

    def __init__(self) -> None:
        # 3 格挡块超过 max_clear=2，先触发 clear_line_of_sight(equipped_pick=True)，
        # 随后仍需 move_to 靠近，move_to 把手持改回 wooden_hoe；挖铁矿前必须重新装备石镐。
        blocks = {
            (1, 65, 0): "dirt",
            (2, 65, 0): "dirt",
            (3, 65, 0): "dirt",
            (4, 65, 0): "iron_ore",
        }
        super().__init__(
            pos=(0.5, 64.0, 0.5),
            items={"stone_pickaxe": 1, "wooden_hoe": 1, "furnace": 1, "coal": 1},
            blocks=blocks,
            views=[_furnace_view(), _furnace_view(output=1), _furnace_view()],
            track_held=True,
        )
        self.st["held"] = {"name": "wooden_hoe"}
        self.dug_ore_with: list[str] = []

    def move_to(self, *args, **kwargs):
        res = super().move_to(*args, **kwargs)
        self.st["pos"] = [3.5, 64.0, 0.5]
        # 模拟 Mineflayer pathfinder 途中自动切换到 wooden_hoe
        self.st["held"] = {"name": "wooden_hoe"}
        return res

    def dig(self, pos, collect=True, *, stop_on=None):
        target = tuple(pos)
        name = self.blocks.get(target, "air")
        held_name = (self.st.get("held") or {}).get("name", "")
        if name == "iron_ore":
            self.dug_ore_with.append(held_name)
            if held_name != "stone_pickaxe":
                self.calls.append(("dig", {"pos": list(pos), "collect": collect}))
                self.blocks.pop(target, None)
                return {
                    "dug": "iron_ore",
                    "warning": f"Tool {held_name} may not harvest drops from iron_ore",
                }
        return super().dig(pos, collect=collect, stop_on=stop_on)


class Soak5hSeg01FixesTests(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        reset_iron_memo()
        reset_all_voxel_caches()
        reset_all_world_memos()
        self.addCleanup(skill_base._COOLDOWN.clear)
        self.addCleanup(reset_world_memo)
        self.addCleanup(reset_iron_memo)
        self.addCleanup(reset_all_voxel_caches)
        self.addCleanup(reset_all_world_memos)
        sleep_patch = patch("mcagent.skills.iron.time.sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def _state_with_items(self, items: dict[str, int]) -> State:
        world = GridWorld(pos=(-51.4, 59.0, -17.5), items=items)
        _, state = world.session()
        return state

    def test_blocked_fallback_recovers_missing_stone_pickaxe_after_smelt_iron_done(self) -> None:
        """seg-01 t=2360.9s：smelt_iron 已解锁后石镐耗尽，应立刻重造石镐而非 no_tactical_skill 空转。"""
        state = self._state_with_items({
            "stick": 3,
            "cobblestone": 120,
            "oak_planks": 1,
            "furnace": 1,
            "stone_sword": 1,
            "wooden_pickaxe": 1,
            "oak_log": 2,
        })
        planner = RulePlanner(
            target_goal="iron_tools,obtain_armor,lava_bucket",
            min_verified="offline",
            include_stages=GOAL_STAGES,
        )
        planner.completed_advancements.update([
            "minecraft:story/mine_stone",
            "minecraft:story/upgrade_tools",
            "minecraft:story/smelt_iron",
        ])
        decision = planner.choose_for_objective(state)
        self.assertIsNotNone(decision.skill, f"不应陷入 no_tactical_skill: {decision.reason}")
        self.assertEqual(decision.skill.name, "craft_gear")
        self.assertEqual(decision.skill.args.get("item"), "stone_pickaxe")

    def test_blocked_fallback_recovers_full_tool_chain_from_empty_bag_after_death(self) -> None:
        """smelt_iron 已解锁但死后空包时，应自动回溯到 gather_wood 重建木镐->石镐->熔炉链。"""
        state = self._state_with_items({})
        planner = RulePlanner(
            target_goal="obtain_armor",
            min_verified="offline",
            include_stages=GOAL_STAGES,
        )
        planner.completed_advancements.update([
            "minecraft:story/mine_stone",
            "minecraft:story/upgrade_tools",
            "minecraft:story/smelt_iron",
        ])
        decision = planner.choose_for_objective(state)
        self.assertIsNotNone(decision.skill, f"空包后应自动回溯前置工具链，不应 idle: {decision.reason}")
        self.assertEqual(decision.skill.name, "gather_wood")

    def test_mine_step_stance_mismatch_raises_mine_step_unsafe(self) -> None:
        """mine_step 站位失配应抛出 MineStepUnsafe(stage='stance')（仍兼容 Replan）。"""
        world = GridWorld(
            pos=(-53.5, 20.0, 18.3),
            items={"stone_pickaxe": 1},
        )
        ex, state = world.session()
        with self.assertRaises(MineStepUnsafe) as ctx:
            drive(mine_step(ex, state, [-54, 18, 16]), ex, state)
        self.assertIsInstance(ctx.exception, Replan)
        self.assertEqual(ctx.exception.stage, "stance")
        self.assertIn("阶梯落点须位于现场站位相邻方向且低一格", str(ctx.exception))

    def test_smelt_iron_prospect_recovers_from_transient_stance_drift(self) -> None:
        """seg-01 t=112s/180s：探矿期间瞬态站位回弹时，应重新观测稳态落点而非直接中止。"""
        world = _TransientStanceProspectWorld()
        ex, state = world.session()
        with patch("mcagent.skills.reach.time.sleep"):
            drive(SmeltIron(count=1), ex, state)
        self.assertGreaterEqual(state.count("iron_ingot"), 1)

    def test_smelt_iron_reequips_pickaxe_after_move_swaps_held_item(self) -> None:
        """seg-01 t=1664.7s (INC-005)：move_to 切换手持为 wooden_hoe 后，挖铁矿前必须重新装备石镐。"""
        world = _ToolSwapOnMoveWorld()
        ex, state = world.session()
        drive(SmeltIron(count=1), ex, state)
        self.assertEqual(world.dug_ore_with, ["stone_pickaxe"])
        self.assertGreaterEqual(state.count("iron_ingot"), 1)


if __name__ == "__main__":
    unittest.main()
