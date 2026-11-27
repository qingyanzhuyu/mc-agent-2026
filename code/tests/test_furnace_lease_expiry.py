"""熔炉租约过期语义（FurnaceLeaseBook 惰性清理）的离线单测。

审计发现的原问题：租约只有 `started_at/ready_at`，没有任何 TTL。玩家在冶炼途中死亡、
断线、换维度或炉子被炸，租约就永久留在账本里 —— `can_reclaim_station` 永远报
"存在活跃熔炉租约暂不回收"（野外熔炉再也收不回），`ready_leases()` 又永远返回它，
每拍都去领一条早已失效的租约并失败。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from mcagent.skills.furnace_lease import FurnaceLeaseBook, _lease_grace_s

POS = (10, 64, -20)


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _book(t: float = 1000.0):
    clock = _Clock(t)
    book = FurnaceLeaseBook(clock=clock)
    lease = book.register_lease(
        POS, input_item="raw_iron", expected_output="iron_ingot", count=6,
        station_kind="furnace", started_at=t, ready_at=t + 60.0, consumer="smelt_iron",
    )
    return book, clock, lease


class FurnaceLeaseExpiryTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("MC_LEASE_GRACE_S", None)

    def test_grace_default_is_300(self):
        self.assertEqual(_lease_grace_s(), 300.0)

    def test_invalid_grace_falls_back(self):
        for bad in ("abc", "-5", "nan", " "):
            os.environ["MC_LEASE_GRACE_S"] = bad
            self.assertEqual(_lease_grace_s(), 300.0)

    def test_lease_survives_within_grace(self):
        book, clock, _ = _book()
        clock.t = 1000.0 + 60.0 + 100.0      # 就绪后 100s，仍在宽限期内
        self.assertIsNotNone(book.get_lease(POS))
        self.assertEqual(len(book.active_leases()), 1)

    def test_stale_lease_disappears_after_grace(self):
        book, clock, _ = _book()
        clock.t = 1000.0 + 60.0 + 301.0      # 超过 300s 宽限
        self.assertIsNone(book.get_lease(POS))
        self.assertEqual(book.active_leases(), [])
        self.assertEqual(book.ready_leases(), [])

    def test_expiry_frees_the_station_for_reclaim(self):
        book, clock, _ = _book()
        self.assertTrue(book.active_leases())
        clock.t = 1000.0 + 60.0 + 500.0
        self.assertFalse(book.active_leases(), "过期租约必须放手，否则熔炉永远收不回")

    def test_grace_zero_disables_expiry(self):
        os.environ["MC_LEASE_GRACE_S"] = "0"
        book, clock, _ = _book()
        clock.t = 1000.0 + 10_000.0
        self.assertIsNotNone(book.get_lease(POS))


if __name__ == "__main__":
    unittest.main()
