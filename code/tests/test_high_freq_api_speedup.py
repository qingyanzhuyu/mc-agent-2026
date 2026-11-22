"""高频无限速 API 提速改造离线单测（限速器快路径/连接池/并发查块/高频安全层/沿途扫描/window_timeout）。

运行方式（在 code/ 目录下）：
    uv run python -m unittest tests/test_high_freq_api_speedup.py -v
"""
from __future__ import annotations

import inspect
import os
import threading
import time
import unittest
from unittest import mock

import mcagent.client as client_mod
from mcagent.client import (
    CODE_CLASS,
    REPLAN,
    RETRY,
    ApiError,
    Client,
    Config,
    RequestLimiter,
)
from mcagent.loop.engine import LoopConfig, MainLoop
from mcagent.loop.goals import GoalSpec
from mcagent.loop.policy import CampaignPlanner
from mcagent.runtime import Executor, RuntimeConfig, SafetyMonitor, State
from mcagent.skills.base import Skill
from mcagent.skills.diamond_site import _safe_excavation
from mcagent.skills.mining import safe_mine_column
from mcagent.skills.survey import _pick_sector_waypoint
from mcagent.skills.testing import GridWorld
from mcagent.skills.voxel_cache import (
    VoxelCache,
    client_get_blocks_batch,
    has_batch_get_blocks,
    reset_all_voxel_caches,
)
from mcagent.skills.world_memo import memo_for, reset_all_world_memos
from tests.test_d0_core import FakeResponse, FakeSession


def _make_client(handler, *, max_rps: float = 100_000.0, block_cache_ttl_s: float = 0.3) -> Client:
    cfg = Config(
        "https://mock.local/api/v1",
        "u",
        "p",
        token_file=None,
        max_rps=max_rps,
        block_cache_ttl_s=block_cache_ttl_s,
    )
    c = Client(cfg, log=lambda *_: None)
    c.token = "tok"
    c.session = FakeSession(handler)
    return c


class HighFreqLimiterAndPoolTests(unittest.TestCase):
    def test_100k_rps_fast_path_zero_queue_and_stop_priority(self) -> None:
        limiter = RequestLimiter(100_000.0)
        self.assertTrue(limiter._fast_path)

        t0 = time.monotonic()
        for _ in range(200):
            limiter.acquire()
            limiter.acquire(background=True)
            limiter.acquire(urgent=True)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.2, f"100k RPS fast path should not sleep; took {elapsed:.3f}s")

        # 保留 /stop 优先语义：若 _urgent > 0，普通请求必须让路直到急停退出
        limiter._urgent = 1
        normal_done = threading.Event()
        urgent_done = threading.Event()

        def run_normal() -> None:
            limiter.acquire(urgent=False)
            normal_done.set()

        def run_urgent() -> None:
            limiter.acquire(urgent=True)
            urgent_done.set()

        tn = threading.Thread(target=run_normal)
        tn.start()
        time.sleep(0.015)
        self.assertFalse(normal_done.is_set())

        tu = threading.Thread(target=run_urgent)
        tu.start()
        self.assertTrue(urgent_done.wait(0.2))
        self.assertFalse(normal_done.is_set())

        with limiter._cond:
            limiter._urgent = 0
            limiter._cond.notify_all()
        self.assertTrue(normal_done.wait(0.5))
        tn.join(0.5)
        tu.join(0.5)

    def test_client_mounts_keepalive_pool_and_preserves_trust_env_false(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
        self.assertEqual(cfg.max_rps, 100_000.0)
        self.assertEqual(cfg.block_cache_ttl_s, 0.3)

        c = Client(cfg, log=lambda *_: None)
        self.assertFalse(c.session.trust_env)
        for scheme in ("http://", "https://"):
            adapter = c.session.get_adapter(f"{scheme}mock.local/api/v1/status")
            self.assertEqual(adapter._pool_connections, 32)
            self.assertEqual(adapter._pool_maxsize, 32)

    def test_429_rate_limited_backoff_preserved_under_100k_rps(self) -> None:
        self.assertEqual(CODE_CLASS["rate_limited"], RETRY)
        attempts = {"n": 0}

        def handler(method, url, headers, body):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return FakeResponse(
                    429,
                    {"ok": False, "error": {"code": "rate_limited", "message": "slow down", "retry_after_s": 0.03}},
                )
            return FakeResponse(200, {"ok": True, "result": {"pos": [1, 64, 0]}})

        c = _make_client(handler, max_rps=100_000.0)
        st = State(RuntimeConfig())
        ex = Executor(c, st, log=lambda *_: None)
        slept: list[float] = []
        with mock.patch("mcagent.runtime.time.sleep", side_effect=slept.append):
            res = ex.move_to(x=1, z=0)
        self.assertEqual(res, {"pos": [1, 64, 0]})
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(slept, [0.03])


class GetBlocksBatchAndFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_voxel_caches()

    def tearDown(self) -> None:
        reset_all_voxel_caches()

    def test_client_get_blocks_batch_concurrent_cache_isolation_and_no_action_lock(self) -> None:
        calls: list[tuple[int, int, int]] = []
        calls_lock = threading.Lock()
        active = {"now": 0, "peak": 0}

        def handler(method, url, headers, body):
            pos = tuple(body["pos"])
            with calls_lock:
                calls.append(pos)
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            try:
                time.sleep(0.025)
                if pos == (2, 64, 0):
                    return FakeResponse(
                        200, {"ok": False, "error": {"code": "not_loaded", "message": "unloaded"}}
                    )
                return FakeResponse(200, {"ok": True, "result": {"name": "stone", "pos": list(pos)}})
            finally:
                with calls_lock:
                    active["now"] -= 1

        c = _make_client(handler, max_rps=100_000.0, block_cache_ttl_s=2.0)
        st = State(RuntimeConfig())
        ex = Executor(c, st, log=lambda *_: None)
        # 模拟动作锁正被占用：只读 get_blocks_batch 绝不能碰 Executor 锁
        ex._busy = True

        positions = [(i, 64, 0) for i in range(16)]
        t0 = time.monotonic()
        batch = c.get_blocks_batch(positions, max_workers=8, use_cache=True)
        dt = time.monotonic() - t0

        self.assertGreaterEqual(active["peak"], 2, "get_blocks_batch should execute concurrently")
        self.assertLess(dt, 0.25, f"16x25ms concurrent batch should finish well under serial 400ms; got {dt:.3f}s")
        self.assertEqual(batch[(0, 64, 0)]["name"], "stone")
        self.assertEqual(batch[(2, 64, 0)], {}, "isolated cell failure must return empty dict without crashing batch")
        self.assertEqual(len(batch), 16)

        # 第二次命中缓存（仅重试之前失败未入缓存的 (2,64,0)）
        before_calls = len(calls)
        batch2 = c.get_blocks_batch(positions, max_workers=8, use_cache=True)
        self.assertEqual(len(calls) - before_calls, 1)
        self.assertEqual(batch2[(1, 64, 0)]["name"], "stone")

    def test_mock_and_gridworld_graceful_degradation(self) -> None:
        gw = GridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (1, 62, 0): "stone",
                (1, 63, 0): "stone",
                (1, 64, 0): "stone",
                (1, 65, 0): "air",
                (1, 66, 0): "stone",
            },
        )
        self.assertFalse(has_batch_get_blocks(gw))
        self.assertFalse(has_batch_get_blocks(mock.MagicMock()))

        vc = VoxelCache()
        res = vc.get_blocks_batch(gw, [(1, 62, 0), (1, 65, 0), (1, 66, 0)], dimension="overworld")
        self.assertEqual(res[(1, 62, 0)]["name"], "stone")
        self.assertEqual(res[(1, 65, 0)]["name"], "air")
        self.assertEqual(res[(1, 66, 0)]["name"], "stone")

        # 接入了 batch 探针的技能原语在无 get_blocks_batch 的 GridWorld 上仍完全兼容
        ex, state = gw.session()
        self.assertTrue(safe_mine_column(ex, [1, 63, 0]))
        wp = _pick_sector_waypoint(ex, state, (0.5, 64.0, 0.5), "overworld", 0, 4, 8)
        self.assertIsNone(wp)

        # 有 get_blocks_batch 时走批量快路径并保持判定一致
        batch_calls: list[int] = []

        def batch_fn(positions, *, max_workers=8, use_cache=True):
            batch_calls.append(len(positions))
            return {tuple(p): gw.get_block(p) for p in positions}

        gw.get_blocks_batch = batch_fn
        self.assertTrue(has_batch_get_blocks(gw))
        self.assertTrue(safe_mine_column(ex, [1, 63, 0]))
        self.assertGreaterEqual(len(batch_calls), 1)

        # diamond_site._safe_excavation 同时支持 batch 路径
        gw.blocks[(0, 63, 0)] = "stone"
        gw.blocks[(0, 64, 0)] = "air"
        gw.blocks[(0, 65, 0)] = "air"
        gw.blocks[(2, 64, 0)] = "diamond_ore"
        for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            gw.blocks.setdefault((2 + dx, 64 + dy, dz), "stone")
        self.assertTrue(_safe_excavation(ex, (0, 64, 0), ((2, 64, 0),), ()))

        # client_get_blocks_batch 对异常单格隔离
        class FlakyLegacyClient:
            def get_block(self, pos):
                if tuple(pos) == (0, 0, 0):
                    raise ApiError("not_loaded", "missing")
                return {"name": "dirt", "pos": list(pos)}

        fallback = client_get_blocks_batch(FlakyLegacyClient(), [(0, 0, 0), (1, 0, 0)])
        self.assertEqual(fallback[(0, 0, 0)], {})
        self.assertEqual(fallback[(1, 0, 0)]["name"], "dirt")


class HighFreqRuntimeAndRoadsideTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_all_world_memos()

    def test_runtime_and_executor_high_freq_defaults(self) -> None:
        cfg = RuntimeConfig()
        self.assertEqual(cfg.observer_min_interval_s, 0.05)
        self.assertEqual(cfg.poll_risk_s, 0.2)
        self.assertEqual(cfg.poll_safe_s, 0.8)
        self.assertEqual(cfg.status_ttl_s, 0.2)

        sig = inspect.signature(Executor._check_ambiguous_effect)
        self.assertEqual(sig.parameters["busy_interval_s"].default, 0.1)

        lcfg = LoopConfig()
        self.assertEqual(lcfg.idle_sleep_s, 0.2)
        self.assertEqual(lcfg.respawn_poll_s, 0.1)
        self.assertEqual(lcfg.roadside_scan_interval_s, 5.0)
        self.assertEqual(lcfg.roadside_scan_radius, 48)
        self.assertEqual(lcfg.roadside_scan_limit, 64)
        self.assertEqual(lcfg.roadside_scan_move_dist, 8.0)

        st = State(cfg)
        mon = SafetyMonitor(mock.Mock(), st, mock.Mock(), continuous=True)
        st.set_status({
            "ready": True, "alive": True, "health": 20, "pos": [0, 64, 0],
            "dimension": "overworld", "time_of_day": 6000, "on_ground": True,
        })
        self.assertAlmostEqual(mon.next_interval(), 0.15, places=6)
        st.set_status({
            "ready": True, "alive": True, "health": 20, "pos": [0, 64, 0],
            "dimension": "overworld", "time_of_day": 6000, "on_ground": False,
        })
        self.assertAlmostEqual(mon.next_interval(), 0.05, places=6)

    def test_roadside_scan_defaults_and_displacement_trigger(self) -> None:
        class _Clock:
            def __init__(self, t: float = 100.0):
                self.t = t
            def __call__(self) -> float:
                return self.t

        class _World:
            def __init__(self):
                self.find_calls: list[dict] = []
                self.ent_calls: list[dict] = []
            def find_blocks(self, types=None, radius=16, limit=20):
                self.find_calls.append({"types": types, "radius": radius, "limit": limit})
                return {"blocks": [{"name": "hay_block", "pos": [20, 64, 0]}]}
            def get_entities(self, radius=32, types=None):
                self.ent_calls.append({"radius": radius, "types": types})
                return {"entities": [{"id": 1, "name": "wandering_trader", "pos": [18, 64, 0]}]}

        class _DummySkill(Skill):
            name = "dummy_hf"
            description = "dummy"
            params = {}
            goal = "dummy:hf"
            verified = "offline"
            def run(self, ex, state):
                if False:
                    yield

        clock = _Clock(500.0)
        world = _World()
        state = State(RuntimeConfig())
        state.set_status({"ready": True, "alive": True, "pos": [0.5, 64.0, 0.5], "dimension": "overworld"})
        state.set_inventory({"items": []})
        planner = CampaignPlanner(
            mode="campaign",
            strategy="rule",
            registry={_DummySkill.name: _DummySkill},
            ledger=(GoalSpec(id="g1", title="g1", tier="T1", score=5, skill=_DummySkill.name, est_cost_s=10.0, stage="graduated"),),
            min_verified="offline",
            clock=clock,
        )
        loop = MainLoop(
            world, state, Executor(world, state, log=lambda *_: None), planner,
            config=LoopConfig(enable_monitor=False),
            clock=clock, log=lambda *_: None,
        )

        # 未满 5s 且位移 < 8 格：不触发
        clock.t += 2.0
        state.st["pos"] = [5.5, 64.0, 0.5]
        self.assertEqual(loop._maybe_roadside_scan(), [])
        self.assertEqual(len(world.find_calls), 0)

        # 仅过 2s 但位移 >= 8 格：立即触发，且默认 radius=48, limit=64
        state.st["pos"] = [9.0, 64.0, 0.5]
        found = loop._maybe_roadside_scan()
        self.assertEqual(set(found), {"village", "wandering_trader"})
        self.assertEqual(len(world.find_calls), 1)
        self.assertEqual(world.find_calls[0]["radius"], 48)
        self.assertEqual(world.find_calls[0]["limit"], 64)
        self.assertEqual(world.ent_calls[0]["radius"], 48)
        self.assertTrue(memo_for(state).has_landmark("village"))

        # 位移归零后未满 5s 不重复触发；满 5s 后正常时间触发
        clock.t += 4.9
        self.assertEqual(loop._maybe_roadside_scan(), [])
        self.assertEqual(len(world.find_calls), 1)
        clock.t += 0.2
        loop._maybe_roadside_scan()
        self.assertEqual(len(world.find_calls), 2)


class WindowTimeoutRetryTests(unittest.TestCase):
    def test_window_timeout_classified_retry_and_retried_by_executor(self) -> None:
        self.assertEqual(CODE_CLASS["window_timeout"], RETRY)
        self.assertEqual(CODE_CLASS["bad_args"], REPLAN)
        self.assertTrue(ApiError("window_timeout", "Event windowOpen did not fire").retryable)

        calls = {"n": 0}

        class _WindowClient:
            def window_view(self, target, *, stop_on=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ApiError("window_timeout", "Event windowOpen did not fire within timeout of 20000ms")
                return {"window": "furnace", "sections": {"output": [{"name": "iron_ingot", "count": 2}]}}

            def invalidate_block_cache(self):
                pass

        state = State(RuntimeConfig())
        ex = Executor(_WindowClient(), state, log=lambda *_: None)
        with mock.patch("mcagent.runtime.time.sleep"):
            res = ex.window_view({"pos": [1, 64, 0]})
        self.assertEqual(calls["n"], 2)
        self.assertEqual(res["window"], "furnace")


if __name__ == "__main__":
    unittest.main()
