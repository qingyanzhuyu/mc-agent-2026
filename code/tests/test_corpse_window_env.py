"""捡尸窗口（距离/时效）环境变量覆盖的离线回归测试。

覆盖 `WorldResourceMemo.should_attempt_corpse_recovery`：
- 默认仍是 80 格 / 240s，行为与改动前完全一致；
- `MC_CORPSE_MAX_DIST` / `MC_CORPSE_MAX_AGE_S` 可放宽窗口；
- 非法/空/非正数值一律回落默认；
- 显式传参优先于环境变量。
"""
import os
import unittest
from unittest import mock

from mcagent.skills.world_memo import (
    WorldResourceMemo,
    corpse_recovery_limits,
)

DEATH_POS = (0.0, 64.0, 0.0)
LOOT = {"iron_pickaxe": 1}


def _memo(now: float = 1000.0) -> WorldResourceMemo:
    m = WorldResourceMemo()
    m.record_death(DEATH_POS, cause="zombie", lost_items=LOOT, dimension="overworld", now=now)
    return m


class CorpseWindowDefaultsTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("MC_CORPSE_MAX_DIST", "MC_CORPSE_MAX_AGE_S"):
            os.environ.pop(name, None)

    def test_default_limits_are_80_and_240(self) -> None:
        self.assertEqual(corpse_recovery_limits(), (80.0, 240.0))

    def test_nearby_recent_death_is_eligible(self) -> None:
        m = _memo()
        ok, reason = m.should_attempt_corpse_recovery((20.0, 64.0, 0.0), dimension="overworld", now=1010.0)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "eligible")

    def test_default_distance_cutoff_still_80(self) -> None:
        m = _memo()
        ok, reason = m.should_attempt_corpse_recovery((100.0, 64.0, 0.0), dimension="overworld", now=1010.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "too_far")

    def test_default_age_cutoff_still_240(self) -> None:
        m = _memo()
        ok, reason = m.should_attempt_corpse_recovery((20.0, 64.0, 0.0), dimension="overworld", now=1000.0 + 500.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "expired")


class CorpseWindowEnvOverrideTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("MC_CORPSE_MAX_DIST", "MC_CORPSE_MAX_AGE_S"):
            os.environ.pop(name, None)

    def test_env_widens_distance(self) -> None:
        os.environ["MC_CORPSE_MAX_DIST"] = "256"
        self.assertEqual(corpse_recovery_limits(), (256.0, 240.0))
        m = _memo()
        ok, reason = m.should_attempt_corpse_recovery((100.0, 64.0, 0.0), dimension="overworld", now=1010.0)
        self.assertTrue(ok, reason)
        # 放宽后仍受上限约束
        ok2, reason2 = m.should_attempt_corpse_recovery((300.0, 64.0, 0.0), dimension="overworld", now=1010.0)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "too_far")

    def test_env_widens_age(self) -> None:
        os.environ["MC_CORPSE_MAX_AGE_S"] = "900"
        m = _memo()
        ok, reason = m.should_attempt_corpse_recovery((20.0, 64.0, 0.0), dimension="overworld", now=1000.0 + 500.0)
        self.assertTrue(ok, reason)
        ok2, reason2 = m.should_attempt_corpse_recovery((20.0, 64.0, 0.0), dimension="overworld", now=1000.0 + 1000.0)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "expired")

    def test_invalid_env_values_fall_back_to_defaults(self) -> None:
        for bad in ("", "  ", "abc", "0", "-5", "nan", "inf"):
            with self.subTest(bad=bad):
                os.environ["MC_CORPSE_MAX_DIST"] = bad
                os.environ["MC_CORPSE_MAX_AGE_S"] = bad
                self.assertEqual(corpse_recovery_limits(), (80.0, 240.0))

    def test_explicit_args_beat_env(self) -> None:
        os.environ["MC_CORPSE_MAX_DIST"] = "256"
        os.environ["MC_CORPSE_MAX_AGE_S"] = "900"
        m = _memo()
        ok, reason = m.should_attempt_corpse_recovery(
            (100.0, 64.0, 0.0), dimension="overworld", now=1010.0, max_dist=10.0, max_age_s=1.0
        )
        self.assertFalse(ok)
        self.assertIn(reason, ("too_far", "expired"))

    def test_unrecoverable_cause_and_no_valuables_unaffected(self) -> None:
        os.environ["MC_CORPSE_MAX_DIST"] = "512"
        os.environ["MC_CORPSE_MAX_AGE_S"] = "3600"
        lava = WorldResourceMemo()
        lava.record_death(DEATH_POS, cause="lava", lost_items=LOOT, dimension="overworld", now=1000.0)
        ok, reason = lava.should_attempt_corpse_recovery((1.0, 64.0, 0.0), dimension="overworld", now=1001.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "unrecoverable_cause")

        cheap = WorldResourceMemo()
        cheap.record_death(DEATH_POS, cause="zombie", lost_items={"dirt": 3}, dimension="overworld", now=1000.0)
        ok2, reason2 = cheap.should_attempt_corpse_recovery((1.0, 64.0, 0.0), dimension="overworld", now=1001.0)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "no_valuable_items")

    def test_missing_record_still_reports_no_death_record(self) -> None:
        os.environ["MC_CORPSE_MAX_DIST"] = "512"
        self.assertEqual(
            WorldResourceMemo().should_attempt_corpse_recovery((0.0, 64.0, 0.0))[1],
            "no_death_record",
        )


class _FakeState:
    def __init__(self, pos):
        self._pos = pos

    def snapshot(self):
        return {"pos": self._pos}


class CorpseMoveTimeoutTest(unittest.TestCase):
    """远距离捡尸的单次 move_to 超时必须随距离放宽，否则 256 格永远走不到。"""

    def _timeout(self, cur, death):
        from mcagent.skills.camp_base import _corpse_move_timeout

        return _corpse_move_timeout(_FakeState(cur), death)

    def test_nearby_keeps_base_budget(self):
        self.assertEqual(self._timeout((0.5, 64.0, 0.5), (10.0, 64.0, 0.0)), 30.0)

    def test_scales_with_distance(self):
        self.assertAlmostEqual(self._timeout((0.0, 64.0, 0.0), (200.0, 64.0, 0.0)), 100.0, places=3)

    def test_capped(self):
        self.assertEqual(self._timeout((0.5, 64.0, 0.5), (5000.0, 64.0, 0.0)), 180.0)

    def test_missing_pos_falls_back_to_base(self):
        self.assertEqual(self._timeout(None, (200.0, 64.0, 0.0)), 30.0)


if __name__ == "__main__":
    unittest.main()
