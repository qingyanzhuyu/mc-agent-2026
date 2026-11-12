"""D0 离线核心单测：不依赖真实皮肤站账号，验证认证恢复、死亡去重、姿态保护与决策校验。

运行方式（在 code/ 目录下）：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from typing import Any, Callable

from mcagent.client import ApiError, Client, Config
from mcagent.planner import validate
from mcagent.runtime import Safety, State


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, headers: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text else (json.dumps(payload) if payload is not None else "")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, handler: Callable[[str, str, dict, Any], FakeResponse]):
        self.handler = handler
        self.trust_env = True
        self.proxies: dict = {}

    def get(self, url: str, headers: dict | None = None, params: Any = None, timeout: float = 30) -> FakeResponse:
        return self.handler("GET", url, headers or {}, params)

    def post(self, url: str, headers: dict | None = None, data: str = "{}", timeout: float = 30) -> FakeResponse:
        body = json.loads(data) if data else {}
        return self.handler("POST", url, headers or {}, body)


class AuthRecoveryTests(unittest.TestCase):
    def test_http_401_and_ok_false_invalid_token_recovery(self) -> None:
        """覆盖两处历史缺陷：① HTTP 401 未捕获；② ok=false 重登成功后穿透抛出旧错误。"""
        for use_http_401 in (True, False):
            with self.subTest(use_http_401=use_http_401):
                login_calls = 0
                status_calls = 0

                def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
                    nonlocal login_calls, status_calls
                    if url.endswith("/auth/login"):
                        login_calls += 1
                        return FakeResponse(200, {
                            "ok": True,
                            "token": f"tok-{login_calls}",
                            "expires_at": "2099-01-01T00:00:00.000Z",
                            "bot_username": "BotA",
                        })
                    if url.endswith("/status"):
                        status_calls += 1
                        auth = headers.get("Authorization", "")
                        if auth != "Bearer tok-2":
                            if use_http_401:
                                return FakeResponse(401, {"ok": False, "error": {"code": "invalid_token"}})
                            return FakeResponse(200, {"ok": False, "error": {"code": "invalid_token", "message": "expired"}})
                        return FakeResponse(200, {"ok": True, "result": {"ready": True, "health": 20}})
                    raise AssertionError(f"unexpected url: {url}")

                cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
                c = Client(cfg, log=lambda *_: None)
                c.session = FakeSession(handler)
                c.token = "tok-stale"
                login_calls = 1  # 模拟此前已登录过一次，后续重登产出 tok-2

                res = c.status()
                self.assertTrue(res["ready"])
                self.assertEqual(c.token, "tok-2")
                self.assertEqual(login_calls, 2)
                self.assertEqual(status_calls, 2)

    def test_concurrent_401_refreshes_only_once(self) -> None:
        """并发请求同时撞上失效 token 时，只允许触发一次 /auth/login，防重复登录互踢。"""
        lock = threading.Lock()
        login_calls = 0
        barrier = threading.Barrier(6)

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal login_calls
            if url.endswith("/auth/login"):
                with lock:
                    login_calls += 1
                    tok = f"fresh-{login_calls}"
                time.sleep(0.02)
                return FakeResponse(200, {
                    "ok": True,
                    "token": tok,
                    "expires_at": "2099-01-01T00:00:00.000Z",
                    "bot_username": "BotA",
                })
            if url.endswith("/status"):
                auth = headers.get("Authorization", "")
                if auth == "Bearer expired-tok":
                    try:
                        barrier.wait(timeout=1.0)
                    except threading.BrokenBarrierError:
                        pass
                    return FakeResponse(401, {"ok": False, "error": {"code": "invalid_token"}})
                if auth == "Bearer fresh-1":
                    return FakeResponse(200, {"ok": True, "result": {"ready": True}})
                return FakeResponse(401, {"ok": False, "error": {"code": "invalid_token"}})
            raise AssertionError(url)

        cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
        c = Client(cfg, log=lambda *_: None)
        c.session = FakeSession(handler)
        c.token = "expired-tok"

        results: list[dict] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                results.append(c.status())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        self.assertEqual(login_calls, 1)

    def test_token_persists_across_client_restart(self) -> None:
        """进程重启后应直接复用落盘 token，不再触发 /auth/login 踢掉在线连接。"""
        with tempfile.TemporaryDirectory() as tmp:
            tf = os.path.join(tmp, ".token.json")
            login_calls = 0

            def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
                nonlocal login_calls
                if url.endswith("/auth/login"):
                    login_calls += 1
                    return FakeResponse(200, {
                        "ok": True,
                        "token": "persisted-token",
                        "expires_at": "2099-01-01T00:00:00.000Z",
                        "bot_username": "BotPersist",
                    })
                if url.endswith("/status"):
                    self.assertEqual(headers.get("Authorization"), "Bearer persisted-token")
                    return FakeResponse(200, {"ok": True, "result": {"ready": True}})
                raise AssertionError(url)

            cfg1 = Config("https://mock.local/api/v1", "u", "p", token_file=tf)
            c1 = Client(cfg1, log=lambda *_: None)
            c1.session = FakeSession(handler)
            c1.status()
            self.assertEqual(login_calls, 1)

            # 模拟新进程重新实例化 Client
            cfg2 = Config("https://mock.local/api/v1", "u", "p", token_file=tf)
            c2 = Client(cfg2, log=lambda *_: None)
            c2.session = FakeSession(handler)
            self.assertEqual(c2.token, "persisted-token")
            c2.status()
            self.assertEqual(login_calls, 1)


class ClientSemanticsTests(unittest.TestCase):
    def test_posture_preserves_other_flag_and_empty_resets(self) -> None:
        sent_bodies: list[dict] = []

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            sent_bodies.append(body)
            return FakeResponse(200, {
                "ok": True,
                "result": {
                    "sneaking": bool(body.get("sneak", False)),
                    "blocking": bool(body.get("shield", False)),
                },
            })

        cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
        c = Client(cfg, log=lambda *_: None)
        c.token = "tok"
        c.session = FakeSession(handler)

        c.posture(sneak=True)
        self.assertEqual(sent_bodies[-1], {"sneak": True, "shield": False})
        # 搭桥中途举盾，绝不能把 sneak 漏掉导致服务端复位为 False
        c.posture(shield=True)
        self.assertEqual(sent_bodies[-1], {"sneak": True, "shield": True})
        # 无参调用保持全量复位 `{}` 语义
        c.posture()
        self.assertEqual(sent_bodies[-1], {})

    def test_chat_sanitizes_leading_slash_and_equip_normalizes_slot(self) -> None:
        calls: list[tuple[str, dict]] = []

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            calls.append((url, body))
            return FakeResponse(200, {"ok": True, "result": {"sent": True}})

        cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
        c = Client(cfg, log=lambda *_: None)
        c.token = "tok"
        c.session = FakeSession(handler)

        c.chat("   /tp 0 64 0")
        self.assertFalse(calls[-1][1]["message"].lstrip().startswith("/"))
        c.equip("iron_chestplate", slot="chest")
        self.assertEqual(calls[-1][1]["slot"], "torso")


class DeathDeduplicationAndStateTests(unittest.TestCase):
    def test_repeated_last_death_and_event_counted_once(self) -> None:
        """同一次死亡经过连续 status 轮询 + events 拉取，只能计为 1 次死亡。"""
        s = State()
        death_t_ms = int(time.time() * 1000) - 2000
        st = {"alive": True, "health": 20, "last_death": {"pos": [0, 64, 0], "t": death_t_ms}}

        s.set_status(st)
        s.set_status(st)
        s.set_status(st)
        self.assertEqual(s.deaths_in(60), 1)
        self.assertIsNone(Safety().check(s))

        # 同一死亡事件再从 /events 到达（时间戳差 500ms 内），仍不重复累加
        class DummyClient:
            def events(self, since: int = 0, limit: int = 100) -> dict:
                return {
                    "events": [{"seq": 10, "t": death_t_ms + 300, "type": "death", "pos": [0, 64, 0]}],
                    "last_seq": 10,
                    "has_more": False,
                }

        s.drain(DummyClient())  # type: ignore[arg-type]
        self.assertEqual(s.deaths_in(60), 1)

    def test_drain_updates_deaths_and_latches_60s_safe_mode(self) -> None:
        """drain() 独立收到两次死亡事件时，更新计数并锁定 >=60s 的 safe_mode 冷却。"""
        s = State()
        s.set_status({"alive": True, "health": 20})

        batch = [
            {"seq": 1, "t": 1_000_000, "type": "death", "pos": [0, 64, 0]},
            {"seq": 2, "t": 1_015_000, "type": "death", "pos": [2, 64, 0]},
        ]

        class DummyClient:
            def events(self, since: int = 0, limit: int = 100) -> dict:
                return {"events": batch if since == 0 else [], "last_seq": 2, "has_more": False}

        s.drain(DummyClient())  # type: ignore[arg-type]
        self.assertEqual(s.deaths_in(60), 2)
        chk = Safety().check(s)
        self.assertIsNotNone(chk)
        assert chk is not None
        self.assertEqual(chk["kind"], "safe_mode")
        self.assertGreaterEqual(s.safe_mode_remaining, 60.0)

    def test_inventory_count_includes_armor_and_offhand_without_double_count(self) -> None:
        s = State()
        s.set_inventory({
            "items": [{"name": "oak_log", "count": 4, "slot": 36}],
            "held": {"name": "oak_log", "count": 4, "slot": 36},
            "armor": {
                "head": None,
                "chest": {"name": "iron_chestplate", "count": 1, "slot": 6},
                "legs": None,
                "feet": None,
            },
            "offhand": {"name": "shield", "count": 1, "slot": 45},
        })
        self.assertEqual(s.count("oak_log"), 4)
        self.assertEqual(s.count("iron_chestplate"), 1)
        self.assertEqual(s.count("shield"), 1)


class DecisionValidatorTests(unittest.TestCase):
    def test_validator_rejects_bad_types_and_accepts_valid(self) -> None:
        ok, _ = validate({"goal": "acq:log", "action": "dig", "args": {"pos": [10, 64, -5]}})
        self.assertTrue(ok)

        ok, _ = validate({"goal": "nav", "action": "move_to", "args": {"pos": [10, 64, -5]}})
        self.assertTrue(ok)

        # None / bool 伪装成数字 / 非法槽位 均须拦截
        for bad in (
            {"goal": "nav", "action": "move_to", "args": {"x": None, "z": None}},
            {"goal": "dig", "action": "dig", "args": {"pos": [True, 64, 0]}},
            {"goal": "craft", "action": "craft", "args": {"item": "stick", "count": 0}},
            {"goal": "eq", "action": "equip", "args": {"item": "iron_sword", "slot": "back"}},
        ):
            ok, errs = validate(bad)
            self.assertFalse(ok, f"expected invalid for {bad}")
            self.assertTrue(errs)


if __name__ == "__main__":
    unittest.main()
