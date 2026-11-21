"""Sprint 4 真机长跑取证缺陷的最小化离线 TDD 回归单测。"""
from __future__ import annotations

import unittest

from mcagent.loop.policy import RulePlanner
from mcagent.runtime import Executor, Replan, SafetyMonitor, State
from mcagent.skills.iron import SmeltIron, reset_iron_memo
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches, voxel_cache_for
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


class Sprint4LiveSoakRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        reset_all_voxel_caches()
        reset_iron_memo()

    def test_heal_does_not_preempt_passive_hunt_when_foraging_without_food(self) -> None:
        w = GridWorld(pos=[0.5, 64.0, 0.5], items={"stone_sword": 1})
        w.st.update(health=7.3, food=14, nearest_hostile=None, dimension="overworld", ready=True, alive=True)
        ex, state = w.session()
        mon = SafetyMonitor(w, state, ex)
        ex.active_action = "attack"
        state.foraging_passive = True

        # 无食物且处于 gather_food 被动狩猎中：heal 保持 soft，不抢占捕杀无害动物
        mon._dispatch(reconcile=False)
        self.assertEqual([c for c in w.calls if c[0] == "stop"], [])

        # 非被动狩猎（正常交战）：heal 立即升级为 urgent 并触发 /stop
        state.foraging_passive = False
        mon._dispatch(reconcile=False)
        self.assertTrue(any(c[0] == "stop" for c in w.calls))

    def test_proactive_camp_setup_requires_12_planks_without_crafting_table(self) -> None:
        w = GridWorld(pos=[0.5, 64.0, 0.5], items={"stone_pickaxe": 1, "furnace": 1, "oak_log": 2, "cooked_beef": 4})
        _, state = w.session()
        state.st.update(dimension="overworld", ready=True, alive=True, health=20, food=20, is_day=True)
        rp = RulePlanner(min_verified="offline")
        # 无工作台且仅 2 原木（8 木板）：不提前派发 setup，避免因缺 12 木板连续 replan
        dec = rp.choose_proactive_survival(state)
        self.assertTrue(dec is None or dec.skill.name != "manage_camp_chest")

        # 登记已有工作台后，8 木板即可派发 setup
        memo_for(state).record_station("crafting_table", [2, 64, 0], dimension="overworld")
        dec2 = rp.choose_proactive_survival(state)
        self.assertIsNotNone(dec2)
        assert dec2 is not None
        self.assertEqual(dec2.skill.name, "manage_camp_chest")
        self.assertEqual(dec2.skill.args["mode"], "setup")

    def test_smelt_iron_blacklists_cliff_ore_on_void_risk_preemption(self) -> None:
        ore_pos = [10, 58, 0]

        class _CliffIronWorld(GridWorld):
            def find_blocks(self, types=None, radius=16, limit=20, **_):
                if types and any("iron" in t for t in types):
                    return {"blocks": [{"name": "iron_ore", "pos": list(ore_pos)}]}
                return {"blocks": []}

            def move_to(self, *args, **kwargs):
                raise Replan("动作被安全层抢占: void_risk: 脚下连续空气且正在下落：疑似坠落/深坑")

        w = _CliffIronWorld(
            blocks={(0, 63, 0): "stone", tuple(ore_pos): "iron_ore"},
            pos=[0.5, 64.0, 0.5],
            items={"stone_pickaxe": 1, "coal": 4, "furnace": 1},
        )
        ex, state = w.session()
        state.st.update(dimension="overworld", ready=True, alive=True)
        with self.assertRaisesRegex(Replan, "void_risk"):
            drive(SmeltIron(count=3), ex, state)
        self.assertTrue(
            voxel_cache_for(state).is_unreachable("smelt_iron", ore_pos, dimension="overworld", stance_cell=(0, 64, 0))
        )

    def test_skeleton_cover_zero_displacement_falls_through_to_real_flee(self) -> None:
        from mcagent.skills.safety_escape import EmergencyEscape

        blocks = {(x, 63, z): "stone" for x in range(-14, 15) for z in range(-14, 15)}
        # 在 (-1, 64, 1) 和 (-1, 65, 1) 放墙，使 (-1, 64, 0) 成为 1 格邻近掩体位
        blocks[(-1, 64, 1)] = "stone"
        blocks[(-1, 65, 1)] = "stone"

        class _ZeroDispCoverWorld(GridWorld):
            def __init__(self):
                super().__init__(blocks=blocks, pos=[0.5, 64.0, 0.5], items={})
                self.st.update(
                    dimension="overworld",
                    ready=True,
                    alive=True,
                    health=20.0,
                    nearest_hostile={"id": 77, "name": "skeleton", "dist": 5.0, "pos": [0.5, 64.0, 5.5]},
                )
                self.move_calls: list[dict] = []

            def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
                self.move_calls.append({"pos": list(pos) if pos is not None else [x, y, z], "rng": rng})
                # 首次掩体移动故意返回原地 0 位移，模拟 rng 误判导致的 1,520 次空转
                if len(self.move_calls) == 1:
                    return {"pos": list(self.st["pos"])}
                return super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)

        w = _ZeroDispCoverWorld()
        ex, state = w.session()
        steps = drive(EmergencyEscape(intent="flee"), ex, state)
        # 掩体位必须用 rng=0 且 0 位移时不得提前返回，必须继续执行真实远逃
        self.assertTrue(any(s.get("skeleton_cover") for s in steps))
        self.assertEqual(w.move_calls[0]["rng"], 0)
        self.assertGreaterEqual(len(w.move_calls), 2)


if __name__ == "__main__":
    unittest.main()
