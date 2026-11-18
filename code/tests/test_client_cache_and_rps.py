"""Task 1 离线单测：16 RPS 上限、三级优先级限速器、后台观察上下文与 get_block 短时缓存。

运行方式（在 code/ 目录下）：
    uv run python -m unittest tests/test_client_cache_and_rps.py -v
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import mcagent.client as client_mod
from mcagent.client import (
    CODE_CLASS,
    FATAL,
    REPLAN,
    ApiError,
    Client,
    Config,
    RequestLimiter,
)
from tests.test_d0_core import FakeResponse, FakeSession


def _make_client(handler, *, max_rps: float = 16.0, block_cache_ttl_s: float = 1.5) -> Client:
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


class ConfigCapAndEnvTests(unittest.TestCase):
    def test_max_rps_cap_constant_and_defaults(self) -> None:
        self.assertEqual(client_mod.MAX_RPS_CAP, 16.0)
        self.assertIsInstance(client_mod.MAX_RPS_CAP, float)
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
        self.assertEqual(cfg.max_rps, 16.0)
        self.assertEqual(cfg.block_cache_ttl_s, 1.5)

    def test_max_rps_bounds_and_error_message(self) -> None:
        for ok_rate in (0.1, 4, 15.5, 16, 16.0):
            cfg = Config("https://mock.local/api/v1", "u", "p", max_rps=ok_rate)
            self.assertEqual(cfg.max_rps, float(ok_rate))

        for bad_rate in (0, -1, 16.01, 17, float("nan"), float("inf"), float("-inf"), True, False):
            with self.subTest(bad_rate=bad_rate):
                with self.assertRaises(ValueError) as ctx:
                    Config("https://mock.local/api/v1", "u", "p", max_rps=bad_rate)
                self.assertEqual(str(ctx.exception), "MC_MAX_RPS 必须在 (0, 16]，16 是客户端硬上限")

        for bad_env in ("0", "17", "nan", "abc"):
            with self.subTest(bad_env=bad_env):
                with mock.patch.dict(os.environ, {"MC_MAX_RPS": bad_env}, clear=True):
                    with self.assertRaises(ValueError):
                        Config("https://mock.local/api/v1", "u", "p", token_file=None)

    def test_block_cache_ttl_validation_and_env_load(self) -> None:
        for ok_ttl in (0, 0.0, 0.25, 1.5, 10):
            cfg = Config("https://mock.local/api/v1", "u", "p", block_cache_ttl_s=ok_ttl)
            self.assertEqual(cfg.block_cache_ttl_s, float(ok_ttl))

        for bad_ttl in (-0.01, -5, float("nan"), float("inf"), float("-inf"), True, False):
            with self.subTest(bad_ttl=bad_ttl):
                with self.assertRaises(ValueError):
                    Config("https://mock.local/api/v1", "u", "p", block_cache_ttl_s=bad_ttl)

        for bad_env in ("-1", "nan", "inf", "not-a-number"):
            with self.subTest(bad_env=bad_env):
                with mock.patch.dict(os.environ, {"MC_BLOCK_CACHE_TTL_S": bad_env}, clear=True):
                    with self.assertRaises(ValueError):
                        Config("https://mock.local/api/v1", "u", "p", token_file=None)

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "MC_API_BASE=http://127.0.0.1:8787/api/v1\n"
                "MC_USER=bot1\n"
                "MC_PASS=pass1\n"
                "MC_MAX_RPS=16\n"
                "MC_BLOCK_CACHE_TTL_S=2.25\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                cfg = Config.load(str(env_path))
        self.assertEqual(cfg.max_rps, 16.0)
        self.assertEqual(cfg.block_cache_ttl_s, 2.25)

    def test_env_example_documents_16_rps_cache_and_observer_interval(self) -> None:
        env_example = Path(client_mod.__file__).resolve().with_name(".env.example")
        text = env_example.read_text(encoding="utf-8")
        self.assertIn("MC_MAX_RPS=16", text)
        self.assertIn("MC_BLOCK_CACHE_TTL_S=1.5", text)
        self.assertIn("MC_OBSERVER_MIN_INTERVAL_S=0.05", text)


class RequestLimiterPriorityTests(unittest.TestCase):
    def test_backward_compatible_acquire_signatures(self) -> None:
        limiter = RequestLimiter(100)
        limiter.acquire()
        limiter.acquire(True)
        limiter.acquire(False)
        limiter.acquire(urgent=True)
        limiter.acquire(background=True)
        limiter.acquire(urgent=True, background=True)

    def test_three_tier_priority_urgent_before_normal_before_background(self) -> None:
        limiter = RequestLimiter(8)  # 125ms spacing
        limiter.acquire()  # 占掉当前瞬时槽，后续线程全部进入等待

        order: list[str] = []
        order_lock = threading.Lock()
        errors: list[Exception] = []

        def run_tier(label: str, *, urgent: bool = False, background: bool = False) -> None:
            try:
                limiter.acquire(urgent=urgent, background=background)
                with order_lock:
                    order.append(label)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        bg_threads = [
            threading.Thread(target=run_tier, args=(f"bg{i}",), kwargs={"background": True})
            for i in range(2)
        ]
        for t in bg_threads:
            t.start()
        # 确保两个后台线程已经进锁等待
        time.sleep(0.02)

        normal_threads = [threading.Thread(target=run_tier, args=(f"norm{i}",)) for i in range(2)]
        for t in normal_threads:
            t.start()
        deadline = time.monotonic() + 1.0
        while limiter._normal < 2 and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertEqual(limiter._normal, 2)

        urgent_thread = threading.Thread(target=run_tier, args=("stop",), kwargs={"urgent": True})
        urgent_thread.start()
        deadline = time.monotonic() + 1.0
        while limiter._urgent < 1 and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertEqual(limiter._urgent, 1)

        for t in (*bg_threads, *normal_threads, urgent_thread):
            t.join(timeout=3.0)
            self.assertFalse(t.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(order[0], "stop")
        self.assertEqual(set(order[1:3]), {"norm0", "norm1"})
        self.assertEqual(set(order[3:5]), {"bg0", "bg1"})

    def test_urgent_overrides_background_flag_if_both_true(self) -> None:
        limiter = RequestLimiter(10)  # 100ms spacing
        limiter.acquire()
        order: list[str] = []

        def w(name: str, **kw: Any) -> None:
            limiter.acquire(**kw)
            order.append(name)

        t_norm = threading.Thread(target=w, args=("norm",))
        t_norm.start()
        deadline = time.monotonic() + 1.0
        while limiter._normal < 1 and time.monotonic() < deadline:
            time.sleep(0.002)

        t_urg_bg = threading.Thread(target=w, args=("urg",), kwargs={"urgent": True, "background": True})
        t_urg_bg.start()
        deadline = time.monotonic() + 1.0
        while limiter._urgent < 1 and time.monotonic() < deadline:
            time.sleep(0.002)

        t_norm.join(timeout=2.0)
        t_urg_bg.join(timeout=2.0)
        self.assertEqual(order, ["urg", "norm"])

    def test_exception_during_wait_cleans_counters_and_wakes_background(self) -> None:
        limiter = RequestLimiter(20)
        limiter.acquire()

        bg_done = threading.Event()
        norm_err = threading.Event()
        real_wait = limiter._cond.wait
        calls = 0

        def flaky_wait(timeout=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("injected wait failure")
            return real_wait(timeout)

        def bg_worker() -> None:
            limiter.acquire(background=True)
            bg_done.set()

        def failing_normal() -> None:
            try:
                with mock.patch.object(limiter._cond, "wait", side_effect=flaky_wait):
                    limiter.acquire()
            except RuntimeError:
                norm_err.set()

        t_norm = threading.Thread(target=failing_normal)
        t_norm.start()
        self.assertTrue(norm_err.wait(timeout=1.0))
        self.assertEqual(limiter._normal, 0)

        t_bg = threading.Thread(target=bg_worker)
        t_bg.start()
        self.assertTrue(bg_done.wait(timeout=1.0))
        t_norm.join(timeout=1.0)
        t_bg.join(timeout=1.0)


class ClientBackgroundContextTests(unittest.TestCase):
    def test_background_observation_context_nesting_isolation_and_stop_priority(self) -> None:
        recorded: list[tuple[str, bool, bool]] = []

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            return FakeResponse(200, {"ok": True, "result": {"ready": True, "stopped": True}})

        c1 = _make_client(handler)
        c2 = _make_client(handler)

        orig_acquire = c1._limiter.acquire

        def spy_acquire(urgent: bool = False, *, background: bool = False) -> None:
            recorded.append(("c1", urgent, background))
            return orig_acquire(urgent=urgent, background=background)

        c1._limiter.acquire = spy_acquire  # type: ignore[method-assign]
        orig_c2_acquire = c2._limiter.acquire

        def spy_c2_acquire(urgent: bool = False, *, background: bool = False) -> None:
            recorded.append(("c2", urgent, background))
            return orig_c2_acquire(urgent=urgent, background=background)

        c2._limiter.acquire = spy_c2_acquire  # type: ignore[method-assign]

        c1.status()
        with c1.background_observation():
            c1.status()
            c2.status()  # 不同 Client 实例互不影响
            with c1.background_observation():
                c1.events()
                c1.stop()  # /stop 即使在后台观察上下文中也绝不能降级为 background
            c1.status()  # 退出内层后仍处于外层后台上下文

        with self.assertRaises(RuntimeError):
            with c1.background_observation():
                raise RuntimeError("boom")
        c1.status()  # 异常退出后必须恢复为普通优先级

        self.assertEqual(
            recorded,
            [
                ("c1", False, False),
                ("c1", False, True),
                ("c2", False, False),
                ("c1", False, True),
                ("c1", True, False),
                ("c1", False, True),
                ("c1", False, False),
            ],
        )

    def test_background_observation_is_thread_local(self) -> None:
        recorded: list[tuple[str, bool, bool]] = []
        lock = threading.Lock()
        bg_entered = threading.Event()
        main_done = threading.Event()

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            return FakeResponse(200, {"ok": True, "result": {"ready": True}})

        c = _make_client(handler, max_rps=16.0)
        orig_acquire = c._limiter.acquire

        def spy_acquire(urgent: bool = False, *, background: bool = False) -> None:
            with lock:
                recorded.append((threading.current_thread().name, urgent, background))
            return orig_acquire(urgent=urgent, background=background)

        c._limiter.acquire = spy_acquire  # type: ignore[method-assign]

        def bg_loop() -> None:
            with c.background_observation():
                bg_entered.set()
                main_done.wait(timeout=2.0)
                c.status()

        t = threading.Thread(target=bg_loop, name="bg-observer")
        t.start()
        self.assertTrue(bg_entered.wait(timeout=1.0))

        threading.current_thread().name = "main-loop"
        c.status()
        main_done.set()
        t.join(timeout=2.0)

        by_thread = {name: (u, b) for name, u, b in recorded}
        self.assertEqual(by_thread["main-loop"], (False, False))
        self.assertEqual(by_thread["bg-observer"], (False, True))


class GetBlockCacheTests(unittest.TestCase):
    def test_caches_by_floored_coords_returns_deepcopy_and_respects_use_cache_false(self) -> None:
        calls: list[list[Any]] = []
        current_name = "stone"

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            self.assertTrue(url.endswith("/get_block"))
            calls.append(body["pos"])
            return FakeResponse(200, {"ok": True, "result": {"name": current_name, "pos": list(body["pos"]), "meta": {"tag": 1}}})

        c = _make_client(handler, block_cache_ttl_s=2.0)

        r1 = c.get_block([-0.5, 64.9, -1.2])
        self.assertEqual(r1["name"], "stone")
        self.assertEqual(len(calls), 1)

        # 调用方修改返回字典不应污染缓存
        r1["name"] = "mutated"
        r1["meta"]["tag"] = 999

        # 同格不同浮点坐标命中同一缓存键 (-1, 64, -2)
        r2 = c.get_block((-1, 64, -2))
        self.assertEqual(r2["name"], "stone")
        self.assertEqual(r2["meta"]["tag"], 1)
        self.assertEqual(len(calls), 1)
        r2["meta"]["tag"] = 777

        r3 = c.get_block([-1.0, 64.0, -2.0])
        self.assertEqual(r3["meta"]["tag"], 1)
        self.assertEqual(len(calls), 1)

        # use_cache=False 绕过读缓存且不写入缓存
        current_name = "dirt"
        r4 = c.get_block([-1, 64, -2], use_cache=False)
        self.assertEqual(r4["name"], "dirt")
        self.assertEqual(len(calls), 2)

        # 新坐标先用 use_cache=False 查，再用 use_cache=True 查仍需打服务端
        r5 = c.get_block([5, 64, 5], use_cache=False)
        self.assertEqual(r5["name"], "dirt")
        self.assertEqual(len(calls), 3)
        r6 = c.get_block([5, 64, 5], use_cache=True)
        self.assertEqual(r6["name"], "dirt")
        self.assertEqual(len(calls), 4)

    def test_ttl_expiry_zero_ttl_missing_name_and_error_not_cached(self) -> None:
        calls = 0
        mode = "stone"

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal calls
            calls += 1
            if mode == "empty":
                return FakeResponse(200, {"ok": True, "result": {"pos": body["pos"]}})
            if mode == "err":
                return FakeResponse(200, {"ok": False, "error": {"code": "not_loaded", "message": "chunk unloaded"}})
            return FakeResponse(200, {"ok": True, "result": {"name": mode}})

        # TTL 到期后重新请求
        c = _make_client(handler, block_cache_ttl_s=0.04)
        self.assertEqual(c.get_block([1, 64, 1])["name"], "stone")
        self.assertEqual(c.get_block([1, 64, 1])["name"], "stone")
        self.assertEqual(calls, 1)
        mode = "air"
        time.sleep(0.06)
        self.assertEqual(c.get_block([1, 64, 1])["name"], "air")
        self.assertEqual(calls, 2)

        # block_cache_ttl_s=0 完全关闭缓存
        c_off = _make_client(handler, block_cache_ttl_s=0.0)
        calls = 0
        c_off.get_block([2, 64, 2])
        c_off.get_block([2, 64, 2])
        self.assertEqual(calls, 2)

        # 缺 name 或报错不入缓存
        c2 = _make_client(handler, block_cache_ttl_s=2.0)
        calls = 0
        mode = "empty"
        c2.get_block([3, 64, 3])
        c2.get_block([3, 64, 3])
        self.assertEqual(calls, 2)

        mode = "err"
        with self.assertRaises(ApiError):
            c2.get_block([4, 64, 4])
        mode = "oak_log"
        self.assertEqual(c2.get_block([4, 64, 4])["name"], "oak_log")
        self.assertEqual(calls, 4)

    def test_invalidation_on_world_mutating_and_session_methods(self) -> None:
        block_calls = 0
        block_name = "stone"

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal block_calls
            if url.endswith("/get_block"):
                block_calls += 1
                return FakeResponse(200, {"ok": True, "result": {"name": block_name}})
            if url.endswith("/auth/login"):
                return FakeResponse(200, {
                    "ok": True,
                    "token": "tok-new",
                    "expires_at": "2099-01-01T00:00:00.000Z",
                    "bot_username": "BotA",
                })
            return FakeResponse(200, {"ok": True, "result": {"ok": True}})

        c = _make_client(handler, block_cache_ttl_s=5.0)
        triggers = [
            ("invalidate_block_cache", lambda p: c.invalidate_block_cache()),
            ("dig", lambda p: c.dig(p)),
            ("place", lambda p: c.place("cobblestone", p)),
            ("interact", lambda p: c.interact(p)),
            ("use_item", lambda p: c.use_item("water_bucket")),
            ("connect", lambda p: c.connect()),
            ("disconnect", lambda p: c.disconnect()),
            ("login", lambda p: c.login()),
        ]
        for idx, (label, fn) in enumerate(triggers, start=1):
            with self.subTest(trigger=label):
                pos = [10 + idx, 64, 10]
                other = [100 + idx, 64, 100]
                block_name = f"mat_{idx}_a"
                self.assertEqual(c.get_block(pos)["name"], f"mat_{idx}_a")
                self.assertEqual(c.get_block(pos)["name"], f"mat_{idx}_a")
                self.assertEqual(c.get_block(other)["name"], f"mat_{idx}_a")
                calls_before = block_calls
                block_name = f"mat_{idx}_b"
                fn(pos)
                self.assertEqual(c.get_block(pos)["name"], f"mat_{idx}_b")
                self.assertEqual(c.get_block(other)["name"], f"mat_{idx}_b")
                self.assertEqual(block_calls, calls_before + 2)

    def test_non_3d_or_non_finite_pos_skips_cache_without_crashing(self) -> None:
        calls = 0

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal calls
            calls += 1
            return FakeResponse(200, {"ok": True, "result": {"name": "stone"}})

        c = _make_client(handler, block_cache_ttl_s=5.0)
        for bad_pos in ([1, 64], [True, 64, 0], [float("nan"), 64, 0], ["1", "64", "0"]):
            self.assertEqual(c.get_block(bad_pos)["name"], "stone")
        self.assertEqual(calls, 4)
        self.assertEqual(c._block_cache, {})

    def test_dig_failure_still_invalidates_and_inflight_get_block_does_not_poison_cache(self) -> None:
        in_get_block = threading.Event()
        allow_get_block_return = threading.Event()
        get_block_calls = 0

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal get_block_calls
            if url.endswith("/get_block"):
                get_block_calls += 1
                if get_block_calls == 1:
                    in_get_block.set()
                    allow_get_block_return.wait(timeout=2.0)
                    return FakeResponse(200, {"ok": True, "result": {"name": "stone"}})
                return FakeResponse(200, {"ok": True, "result": {"name": "air"}})
            if url.endswith("/dig"):
                return FakeResponse(200, {"ok": False, "error": {"code": "timeout", "message": "dig timeout"}})
            raise AssertionError(url)

        c = _make_client(handler, block_cache_ttl_s=5.0)
        pos = [7, 64, 7]
        first_result: list[dict] = []

        t = threading.Thread(target=lambda: first_result.append(c.get_block(pos)))
        t.start()
        self.assertTrue(in_get_block.wait(timeout=1.0))

        # 在途 get_block 尚未返回期间发生 dig（甚至 dig 抛出 timeout 错误）
        with self.assertRaises(ApiError):
            c.dig(pos)

        allow_get_block_return.set()
        t.join(timeout=2.0)
        self.assertEqual(first_result[0]["name"], "stone")

        # 旧在途 get_block 绝不能把 "stone" 回填进缓存毒化后续查询
        latest = c.get_block(pos)
        self.assertEqual(latest["name"], "air")
        self.assertEqual(get_block_calls, 2)

    def test_preserves_trust_env_false_and_code_class_invariants(self) -> None:
        cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
        c = Client(cfg, log=lambda *_: None)
        self.assertFalse(c.session.trust_env)
        self.assertEqual(CODE_CLASS["bad_args"], REPLAN)
        self.assertEqual(CODE_CLASS["unknown_api"], FATAL)


if __name__ == "__main__":
    unittest.main()
