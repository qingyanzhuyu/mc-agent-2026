"""下矿备货闸门（skills/descent_supply.py）的离线单测。

覆盖：闸门关闭时零影响；开启后按原木/镐子/耐久/多树种木材正确判定。
对应 AGENTS.md §5 DoD：改底层逻辑须补对应单测。
"""
from __future__ import annotations

import unittest

from mcagent.skills import descent_supply
from mcagent.skills.descent_supply import descent_supply_gap


class _FakeState:
    """只实现闸门用到的两个接口：count() 与 inventory。"""

    def __init__(self, items=None, stacks=None):
        self._items = dict(items or {})
        self.inventory = {"items": list(stacks or []), "held": None}

    def count(self, name):
        return self._items.get(name, 0)


def _pick(name, durability):
    stack = {"name": name, "count": 1}
    if durability is not None:
        stack["durability"] = [durability, 131]
    return stack


class DescentSupplyGapTest(unittest.TestCase):
    def setUp(self):
        self._orig_min = descent_supply.DESCENT_LOG_MIN
        self._orig_dur = descent_supply.DESCENT_PICK_MIN_DURABILITY

    def tearDown(self):
        descent_supply.DESCENT_LOG_MIN = self._orig_min
        descent_supply.DESCENT_PICK_MIN_DURABILITY = self._orig_dur

    def _enable(self, logs=10, dur=30):
        descent_supply.DESCENT_LOG_MIN = logs
        descent_supply.DESCENT_PICK_MIN_DURABILITY = dur

    # ---- 关闭状态 ----
    def test_disabled_gate_never_blocks(self):
        descent_supply.DESCENT_LOG_MIN = 0
        state = _FakeState({"cobblestone": 1})
        self.assertEqual(descent_supply_gap(state), "")

    def test_disabled_gate_does_not_touch_state(self):
        """关闭时不应读取 state，避免 mock 状态抛异常。"""
        descent_supply.DESCENT_LOG_MIN = 0

        class _Exploding:
            def count(self, name):
                raise AssertionError("闸门关闭时不应调用 state.count")

            @property
            def inventory(self):
                raise AssertionError("闸门关闭时不应读取 state.inventory")

        self.assertEqual(descent_supply_gap(_Exploding()), "")

    # ---- 开启后：原木 ----
    def test_blocks_when_logs_insufficient(self):
        self._enable()
        state = _FakeState({"oak_log": 9, "stone_pickaxe": 2})
        self.assertIn("原木 9/10", descent_supply_gap(state))

    def test_allows_at_exact_threshold(self):
        self._enable()
        state = _FakeState({"oak_log": 10, "stone_pickaxe": 2})
        self.assertEqual(descent_supply_gap(state), "")

    def test_counts_mixed_wood_species(self):
        self._enable()
        state = _FakeState({"oak_log": 6, "birch_log": 5, "stone_pickaxe": 2})
        self.assertEqual(descent_supply_gap(state), "")

    # ---- 开启后：镐子 ----
    def test_blocks_without_any_pickaxe(self):
        self._enable()
        state = _FakeState({"oak_log": 12, "cobblestone": 64})
        self.assertIn("没有镐子", descent_supply_gap(state))

    def test_blocks_when_only_pickaxe_low_durability(self):
        self._enable()
        state = _FakeState(
            {"oak_log": 12, "stone_pickaxe": 1},
            [_pick("stone_pickaxe", 12)],
        )
        self.assertIn("耐久仅 12", descent_supply_gap(state))

    def test_allows_when_spare_pickaxe_healthy(self):
        self._enable()
        state = _FakeState(
            {"oak_log": 12, "stone_pickaxe": 2},
            [_pick("stone_pickaxe", 12), _pick("stone_pickaxe", 100)],
        )
        self.assertEqual(descent_supply_gap(state), "")


class DescentSupplyEnvTest(unittest.TestCase):
    """闸门阈值必须在**调用时**读环境变量，不能 import 时读死（真机事故回归）。"""

    def setUp(self):
        import os
        from unittest import mock

        self._os = os
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._orig_min = descent_supply.DESCENT_LOG_MIN
        self._orig_dur = descent_supply.DESCENT_PICK_MIN_DURABILITY
        descent_supply.DESCENT_LOG_MIN = None      # 回到"运行时解析"模式
        descent_supply.DESCENT_PICK_MIN_DURABILITY = None
        for name in ("MC_DESCENT_LOG_MIN", "MC_DESCENT_PICK_MIN_DURABILITY"):
            os.environ.pop(name, None)

    def tearDown(self):
        descent_supply.DESCENT_LOG_MIN = self._orig_min
        descent_supply.DESCENT_PICK_MIN_DURABILITY = self._orig_dur

    def test_env_read_at_call_time_not_import_time(self):
        """模拟主循环顺序：模块已 import（当时无 env），之后 .env 才灌进 os.environ。"""
        state = _FakeState({"birch_log": 2, "stone_pickaxe": 1})
        self.assertEqual(descent_supply_gap(state), "")     # 未设 env：闸门关

        self._os.environ["MC_DESCENT_LOG_MIN"] = "10"        # 相当于 Config.load() 之后
        self.assertIn("原木 2/10", descent_supply_gap(state))

    def test_env_pick_durability_threshold(self):
        self._os.environ["MC_DESCENT_LOG_MIN"] = "10"
        self._os.environ["MC_DESCENT_PICK_MIN_DURABILITY"] = "50"
        state = _FakeState({"oak_log": 12, "stone_pickaxe": 1}, [_pick("stone_pickaxe", 12)])
        self.assertIn("耐久仅 12", descent_supply_gap(state))

    def test_invalid_env_falls_back_to_disabled(self):
        self._os.environ["MC_DESCENT_LOG_MIN"] = "abc"
        self.assertEqual(descent_supply_gap(_FakeState({"birch_log": 0})), "")

    def test_explicit_module_override_still_wins(self):
        self._os.environ["MC_DESCENT_LOG_MIN"] = "10"
        descent_supply.DESCENT_LOG_MIN = 0
        self.assertEqual(descent_supply_gap(_FakeState({"birch_log": 0})), "")


if __name__ == "__main__":
    unittest.main()
