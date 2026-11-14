"""并发闸门的离线单测：不联网、不调真模型。

背景：比赛期每组只有 2 路并发（官方 2026-11-14 口径，可能降至 1 路）。
`LLM_MAX_CONCURRENCY` 必须真的被执行，而不只是写在 .env 里被 preflight 检查。
"""
from __future__ import annotations

import json
import threading
import time
import unittest

from mcagent.planner import llm as L


def ok_body() -> str:
    return json.dumps({"choices": [{"message": {"content": "ok"}}],
                       "usage": {"completion_tokens": 1}})


class Base(unittest.TestCase):
    def setUp(self):
        L._reset_semaphores()
        self._env_backup = {}
        for k in ("LLM_MAX_CONCURRENCY",):
            self._env_backup[k] = __import__("os").environ.pop(k, None)

    def tearDown(self):
        import os
        for k, v in self._env_backup.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        L._reset_semaphores()

    def client(self, poster, **kw):
        return L.LLMClient(key="test-key", fast_proxy=None, poster=poster, log=lambda *_: None, **kw)


class CapacityTests(Base):
    def test_default_is_two(self):
        c = self.client(lambda *a: (200, ok_body()))
        self.assertEqual(c.max_concurrency, 2)

    def test_env_overrides(self):
        import os
        os.environ["LLM_MAX_CONCURRENCY"] = "48"
        c = self.client(lambda *a: (200, ok_body()))
        self.assertEqual(c.max_concurrency, 48)

    def test_invalid_env_falls_back_to_default(self):
        import os
        os.environ["LLM_MAX_CONCURRENCY"] = "abc"
        c = self.client(lambda *a: (200, ok_body()))
        self.assertEqual(c.max_concurrency, 2)

    def test_zero_or_negative_clamped_to_one(self):
        import os
        os.environ["LLM_MAX_CONCURRENCY"] = "0"
        c = self.client(lambda *a: (200, ok_body()))
        self.assertEqual(c.max_concurrency, 1)


class GateTests(Base):
    def _blocking_poster(self):
        state = {"in_flight": 0, "peak": 0}
        lock = threading.Lock()
        gate = threading.Event()

        def poster(url, headers, data, timeout, proxy):
            with lock:
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
            gate.wait(3)
            with lock:
                state["in_flight"] -= 1
            return 200, ok_body()

        return poster, state, gate, lock

    def test_in_flight_never_exceeds_capacity(self):
        poster, state, gate, lock = self._blocking_poster()
        c = self.client(poster)
        errors = []

        def worker():
            try:
                c.chat([{"role": "user", "content": "hi"}], timeout=5)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.3)                     # 让前 2 个占到槽、后 2 个排队
        with lock:
            self.assertLessEqual(state["peak"], 2, "同时在飞的请求数超过了并发上限")
            self.assertEqual(state["in_flight"], 2)
        gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual(errors, [])
        self.assertLessEqual(state["peak"], 2)

    def test_slot_released_after_error(self):
        """失败一次不能把槽漏掉：容量 1 下泄漏会让后续调用直接卡死。"""
        import os
        os.environ["LLM_MAX_CONCURRENCY"] = "1"

        def always_fail(url, headers, data, timeout, proxy):
            raise L.LLMError("boom", kind="net")

        bad = self.client(always_fail)
        with self.assertRaises(Exception):
            bad.chat([{"role": "user", "content": "a"}], timeout=1)

        good = self.client(lambda *a: (200, ok_body()))
        # 槽是模块级、按容量共享的：上一个 client 漏掉槽，这里会等到超时
        self.assertEqual(good.chat([{"role": "user", "content": "b"}], timeout=2), "ok")

    def test_waiting_for_slot_times_out_with_kind(self):
        import os
        os.environ["LLM_MAX_CONCURRENCY"] = "1"
        gate = threading.Event()

        def slow(url, headers, data, timeout, proxy):
            gate.wait(3)
            return 200, ok_body()

        c = self.client(slow)
        t = threading.Thread(target=lambda: c.chat([{"role": "user", "content": "x"}], timeout=3))
        t.start()
        time.sleep(0.2)
        with self.assertRaises(L.LLMError) as cm:
            c.chat([{"role": "user", "content": "y"}], timeout=0.2)
        self.assertEqual(getattr(cm.exception, "kind", None), "no_slot")
        gate.set()
        t.join(5)


if __name__ == "__main__":
    unittest.main()
