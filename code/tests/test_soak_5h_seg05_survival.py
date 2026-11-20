"""5h soak seg-05 生存逃生故障回归离线单测（verified: offline）。

覆盖 seg-05 10 分钟 8 死的三大直接根因：
1. `_same_escape_kind("flee", "void_risk")` 必须返回 True：正在执行 `EmergencyEscape(flee)`
   逃避僵尸/蜘蛛时，下坡触发 `void_risk` 不得 `/stop` 自我抢占把角色定在原地挨打（岩浆/溺水仍抢占）。
2. 普通非虚空地形（`guard=False`）正常走下 3 格零摔伤山坡（如 `y=73 -> 70`，脚下 `y=69` 为实心石头）
   不应误报 `void_risk`；真正 4+ 格深坑仍须立即触发 `void_risk`。
3. `escape_hazard_step("flee")` 在 `move_to`（寻路禁疾跑）结束后若敌怪仍紧贴在 `<5.5` 格内追咬，
   必须立刻补一次 `control(forward=True, sprint=True, jump=True)` 疾跑跳甩脱。
"""
from __future__ import annotations

import dataclasses
import unittest

from mcagent.runtime import Executor, RuntimeConfig, SafetyMonitor, State, _same_escape_kind
from mcagent.skills.escape import escape_hazard_step
from tests.test_escape_water import EscapeWorld, _drive_result


class _BlockProbeClient:
    def __init__(self, blocks: dict[tuple[int, int, int], str], st: dict) -> None:
        self.blocks = dict(blocks)
        self.st = dict(st)
        self.stops = 0

    def status(self, **_):
        return dict(self.st)

    def events(self, since=0, limit=200):
        return {"events": [], "last_seq": since}

    def inventory(self):
        return {"items": []}

    def get_block(self, pos):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        return {"pos": list(key), "name": self.blocks.get(key, "air")}

    def stop(self):
        self.stops += 1
        return {"stopped": True, "released": True}


class Soak5hSeg05SurvivalTests(unittest.TestCase):
    def test_active_flee_not_preempted_by_void_risk_but_still_preempted_by_lava(self) -> None:
        self.assertTrue(_same_escape_kind("flee", "void_risk"))
        self.assertTrue(_same_escape_kind("void_risk", "flee"))
        self.assertFalse(_same_escape_kind("flee", "escape_lava"))
        self.assertFalse(_same_escape_kind("flee", "surface"))

        # 深坑（y=60..72 全空气）下落中：active_escape="flee" 时不自我 /stop 冻结逃生
        client = _BlockProbeClient(
            blocks={},
            st={"pos": [-66.5, 72.2, -25.6], "velocity": [0.0, -6.0, 0.0], "on_ground": False, "health": 20, "food": 20, "alive": True},
        )
        state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
        ex = Executor(client, state, log=lambda *_: None)
        mon = SafetyMonitor(client, state, ex)

        state.set_active_escape("flee")
        mon.tick()
        self.assertEqual(client.stops, 0)

        # 一旦遇岩浆仍必须立即抢占
        client.st["in_lava"] = True
        state.status_at = float("-inf")
        mon.tick()
        self.assertEqual(client.stops, 1)
        state.set_active_escape(None)

    def test_normal_3block_hill_drop_does_not_false_trigger_void_risk_while_deep_pit_does(self) -> None:
        # 复现 seg-01/05：y=72.2 下落，脚下 y=69 有实心石头（3 格零摔伤山坡，under=72，dy=3 处 y=69 为实心）
        hill_client = _BlockProbeClient(
            blocks={(-67, 69, -26): "stone"},
            st={"pos": [-66.5, 72.2, -25.6], "velocity": [0.0, -6.0, 0.0], "on_ground": False, "health": 20, "food": 20, "alive": True},
        )
        st_hill = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
        ex_hill = Executor(hill_client, st_hill, log=lambda *_: None)
        mon_hill = SafetyMonitor(hill_client, st_hill, ex_hill)
        mon_hill.tick()
        self.assertEqual(hill_client.stops, 0)
        self.assertFalse(st_hill.get_support().get("risk"))

        # 真正 4+ 格深坑（y=68 及以上全空气，实心底在 y=67）仍必须立即触发 void_risk 急停
        pit_client = _BlockProbeClient(
            blocks={(-67, 67, -26): "stone"},
            st={"pos": [-66.5, 72.2, -25.6], "velocity": [0.0, -6.0, 0.0], "on_ground": False, "health": 20, "food": 20, "alive": True},
        )
        st_pit = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
        ex_pit = Executor(pit_client, st_pit, log=lambda *_: None)
        mon_pit = SafetyMonitor(pit_client, st_pit, ex_pit)
        mon_pit.tick()
        self.assertEqual(pit_client.stops, 1)
        self.assertTrue(st_pit.get_support().get("risk"))

    def test_flee_adds_sprint_burst_when_spider_still_close_after_move_to(self) -> None:
        """move_to 寻路禁疾跑被高速蜘蛛紧追（<5.5 格）时，必须补一次疾跑跳拉脱。"""
        world = EscapeWorld(
            pos=(0.5, 64.0, 0.5),
            nearest_hostile={"name": "spider", "pos": [0.5, 64.0, 3.5], "dist": 3.0},
        )
        # 模拟 move_to 走完但蜘蛛一路追上，落点距蜘蛛仍仅 3.2 格
        world.on_move = lambda dest: world.st.update({
            "nearest_hostile": {"name": "spider", "pos": [dest[0], dest[1], dest[2] + 3.2], "dist": 3.2},
        })
        world.on_control = lambda _: world.st.update({
            "pos": [0.5, 64.0, -25.5],
            "nearest_hostile": {"name": "spider", "pos": [0.5, 64.0, -14.3], "dist": 11.2},
        })
        ex, state = world.session()

        ok, _ = _drive_result(escape_hazard_step(ex, state, "flee"), ex)
        self.assertTrue(ok)
        self.assertEqual(ex.acts, ["move_to", "control"])
        self.assertTrue(world.control_requests[0]["sprint"])
        self.assertTrue(world.control_requests[0]["jump"])


if __name__ == "__main__":
    unittest.main()
