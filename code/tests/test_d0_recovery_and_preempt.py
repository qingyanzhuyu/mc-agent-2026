"""D0 执行器恢复与安全抢占单测：验证非幂等动作防重、超时 /stop、抢占优先级与大厅恢复。"""
from __future__ import annotations

import json
import threading
import time
import unittest
from typing import Any, Callable
from unittest import mock

import requests

from mcagent.client import CODE_CLASS, REPLAN, Client, Config
from mcagent.runtime import Executor, Replan, SafetyMonitor, State


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload) if payload is not None else ""

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


class NonIdempotentTimeoutTests(unittest.TestCase):
    def test_craft_succeeds_but_response_lost_does_not_craft_twice(self) -> None:
        """模拟“合成成功但 HTTP 响应丢失”：核对背包已产出目标物后直接恢复返回，绝不二次消耗材料。"""
        cobble = 6
        sticks = 4
        pickaxes = 0
        craft_calls = 0

        def current_inv() -> dict:
            items = []
            if cobble > 0:
                items.append({"name": "cobblestone", "count": cobble, "slot": 0})
            if sticks > 0:
                items.append({"name": "stick", "count": sticks, "slot": 1})
            if pickaxes > 0:
                items.append({"name": "stone_pickaxe", "count": pickaxes, "slot": 2})
            return {"items": items, "armor": {}, "offhand": None, "empty_slots": 33}

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal cobble, sticks, pickaxes, craft_calls
            if url.endswith("/inventory"):
                return FakeResponse(200, {"ok": True, "result": current_inv()})
            if url.endswith("/status"):
                return FakeResponse(200, {"ok": True, "result": {"ready": True, "alive": True, "busy": None}})
            if url.endswith("/craft"):
                craft_calls += 1
                cobble -= 3
                sticks -= 2
                pickaxes += 1
                if craft_calls == 1:
                    raise requests.ReadTimeout("simulated tunnel read timeout after server crafted")
                return FakeResponse(200, {"ok": True, "result": {"crafted": "stone_pickaxe", "gained": 1}})
            raise AssertionError(url)

        cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
        c = Client(cfg, log=lambda *_: None)
        c.token = "tok"
        c.session = FakeSession(handler)
        s = State()
        ex = Executor(c, s, log=lambda *_: None)

        res = ex.craft("stone_pickaxe", 1)
        self.assertTrue(res.get("recovered"))
        self.assertEqual(craft_calls, 1)
        self.assertEqual(pickaxes, 1)
        self.assertEqual(cobble, 3)

    def test_use_item_consumes_eye_on_lost_response_stops_retry(self) -> None:
        """模拟投掷末影之眼后响应丢失：背包中末影之眼减少，执行器停止自动重发以保住剩余珍珠。"""
        eyes = 2
        use_calls = 0

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal eyes, use_calls
            if url.endswith("/inventory"):
                return FakeResponse(200, {
                    "ok": True,
                    "result": {"items": [{"name": "ender_eye", "count": eyes, "slot": 0}], "empty_slots": 35},
                })
            if url.endswith("/status"):
                return FakeResponse(200, {"ok": True, "result": {"ready": True, "alive": True, "busy": None}})
            if url.endswith("/stop"):
                return FakeResponse(200, {"ok": True, "result": {"stopped": True}})
            if url.endswith("/use_item"):
                use_calls += 1
                eyes -= 1
                raise requests.ReadTimeout("lost bearing_deg response")
            raise AssertionError(url)

        cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
        c = Client(cfg, log=lambda *_: None)
        c.token = "tok"
        c.session = FakeSession(handler)
        s = State()
        ex = Executor(c, s, log=lambda *_: None)

        with self.assertRaises(Replan):
            ex.use_item("ender_eye")
        self.assertEqual(use_calls, 1)
        self.assertEqual(eyes, 1)

    def test_move_timeout_triggers_stop_fallback(self) -> None:
        stops = 0
        moves = 0

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal stops, moves
            if url.endswith("/move_to"):
                moves += 1
                if moves == 1:
                    return FakeResponse(200, {"ok": False, "error": {"code": "timeout", "message": "path slow"}})
                return FakeResponse(200, {"ok": True, "result": {"pos": [10, 64, 10]}})
            if url.endswith("/stop"):
                stops += 1
                return FakeResponse(200, {"ok": True, "result": {"stopped": True}})
            raise AssertionError(url)

        cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
        c = Client(cfg, log=lambda *_: None)
        c.token = "tok"
        c.session = FakeSession(handler)
        s = State()
        ex = Executor(c, s, log=lambda *_: None)

        res = ex.act("move_to", lambda: c.move_to(10, 10), retries=1, backoff=0.01)
        self.assertEqual(res["pos"], [10, 64, 10])
        self.assertEqual(stops, 1)


TIMEOUT_ERR = {"ok": False, "error": {"code": "timeout", "message": "server timeout"}}
READY_STATUS = {"ok": True, "result": {"ready": True, "alive": True, "busy": None}}


class ServerTimeoutNonIdempotentTests(unittest.TestCase):
    """规范 §5：`timeout` =「寻路失败 / 超时」。非幂等动作遇服务端 timeout 时，
    副作用可能已经发生，必须与 network 一样先核对背包再决定是否重发。"""

    def setUp(self) -> None:
        # 重试退避与 busy 轮询都走 runtime 模块的 time.sleep；替换成记录器，测试不依赖真实计时
        self.sleeps: list[float] = []
        patcher = mock.patch("mcagent.runtime.time.sleep", side_effect=self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls: list[str] = []

    def _executor(self, handler: Callable[[str, str, dict, Any], FakeResponse]) -> Executor:
        def recording(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            self.calls.append(url.rsplit("/api/v1/", 1)[-1])
            return handler(method, url, headers, body)

        cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
        c = Client(cfg, log=lambda *_: None)
        c.token = "tok"
        c.session = FakeSession(recording)
        c._limiter = mock.Mock()  # 限速器按 max_rps≤4 真实等待；判重逻辑与计时无关，这里免等
        return Executor(c, State(), log=lambda *_: None)

    @staticmethod
    def _inv(counts: dict[str, int]) -> dict:
        items = [{"name": n, "count": k, "slot": i} for i, (n, k) in enumerate(counts.items()) if k > 0]
        return {"ok": True, "result": {"items": items, "armor": {}, "offhand": None,
                                       "empty_slots": 36 - len(items)}}

    def test_craft_timeout_after_server_crafted_recovers_without_resend(self) -> None:
        inv = {"oak_planks": 4}
        crafts = 0

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal crafts
            if url.endswith("/inventory"):
                return FakeResponse(200, self._inv(inv))
            if url.endswith("/status"):
                return FakeResponse(200, READY_STATUS)
            if url.endswith("/craft"):
                crafts += 1
                inv["oak_planks"] -= 4
                inv["crafting_table"] = inv.get("crafting_table", 0) + 1
                return FakeResponse(200, TIMEOUT_ERR)
            raise AssertionError(url)

        ex = self._executor(handler)
        res = ex.craft("crafting_table", 1)
        self.assertTrue(res.get("recovered"))
        self.assertEqual(crafts, 1)
        self.assertEqual(inv, {"oak_planks": 0, "crafting_table": 1})

    def test_craft_timeout_with_inventory_changed_replans_without_resend(self) -> None:
        inv = {"cobblestone": 6, "stick": 4}
        crafts = 0

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal crafts
            if url.endswith("/inventory"):
                return FakeResponse(200, self._inv(inv))
            if url.endswith("/status"):
                return FakeResponse(200, READY_STATUS)
            if url.endswith("/craft"):
                crafts += 1
                inv["cobblestone"] -= 3  # 材料已扣但产物未见：无法确认完成，也不能盲目重发
                return FakeResponse(200, TIMEOUT_ERR)
            raise AssertionError(url)

        ex = self._executor(handler)
        with self.assertRaises(Replan):
            ex.craft("stone_pickaxe", 1)
        self.assertEqual(crafts, 1)
        self.assertEqual(inv["cobblestone"], 3)

    def test_craft_timeout_with_inventory_unchanged_retries_once(self) -> None:
        inv = {"oak_planks": 4}
        crafts = 0

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal crafts
            if url.endswith("/inventory"):
                return FakeResponse(200, self._inv(inv))
            if url.endswith("/status"):
                return FakeResponse(200, READY_STATUS)
            if url.endswith("/craft"):
                crafts += 1
                if crafts == 1:
                    return FakeResponse(200, TIMEOUT_ERR)
                inv["oak_planks"] -= 4
                inv["crafting_table"] = 1
                return FakeResponse(200, {"ok": True, "result": {"crafted": "crafting_table", "gained": 1}})
            raise AssertionError(url)

        ex = self._executor(handler)
        res = ex.craft("crafting_table", 1)
        self.assertEqual(res.get("crafted"), "crafting_table")
        self.assertFalse(res.get("recovered", False))
        self.assertEqual(crafts, 2)
        self.assertEqual(inv, {"oak_planks": 0, "crafting_table": 1})
        self.assertEqual(len(self.sleeps), 1)  # 只退避一次，随后重发成功

    def test_window_act_timeout_with_inventory_changed_does_not_resend(self) -> None:
        inv = {"raw_iron": 3, "coal": 2}
        acts = 0

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal acts
            if url.endswith("/inventory"):
                return FakeResponse(200, self._inv(inv))
            if url.endswith("/status"):
                return FakeResponse(200, READY_STATUS)
            if url.endswith("/window/act"):
                acts += 1
                inv["raw_iron"] -= 3  # 已投料进熔炉，响应却报 timeout
                return FakeResponse(200, TIMEOUT_ERR)
            raise AssertionError(url)

        ex = self._executor(handler)
        with self.assertRaises(Replan):
            ex.window_act({"pos": [0, 64, 0]}, [{"op": "deposit", "item": "raw_iron", "count": 3}])
        self.assertEqual(acts, 1)
        self.assertEqual(inv["raw_iron"], 0)
        self.assertNotIn("stop", self.calls)  # window/act 不在 STOP_ON_TIMEOUT_ACTIONS，且服务端不 busy

    def test_window_act_timeout_with_inventory_unchanged_retries_once(self) -> None:
        inv = {"raw_iron": 3, "coal": 2}
        acts = 0

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal acts
            if url.endswith("/inventory"):
                return FakeResponse(200, self._inv(inv))
            if url.endswith("/status"):
                return FakeResponse(200, READY_STATUS)
            if url.endswith("/window/act"):
                acts += 1
                if acts == 1:
                    return FakeResponse(200, TIMEOUT_ERR)
                inv["raw_iron"] -= 3
                return FakeResponse(200, {"ok": True, "result": {"done": 1}})
            raise AssertionError(url)

        ex = self._executor(handler)
        res = ex.window_act({"pos": [0, 64, 0]}, [{"op": "deposit", "item": "raw_iron", "count": 3}])
        self.assertEqual(res, {"done": 1})
        self.assertEqual(acts, 2)
        self.assertEqual(inv["raw_iron"], 0)

    def test_use_item_timeout_still_stops_then_checks_inventory(self) -> None:
        """use_item 同属 STOP_ON_TIMEOUT_ACTIONS：止动不能丢，且背包已变化时不重发。"""
        inv = {"ender_eye": 2}
        uses = 0

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal uses
            if url.endswith("/inventory"):
                return FakeResponse(200, self._inv(inv))
            if url.endswith("/status"):
                return FakeResponse(200, READY_STATUS)
            if url.endswith("/stop"):
                return FakeResponse(200, {"ok": True, "result": {"stopped": True}})
            if url.endswith("/use_item"):
                uses += 1
                inv["ender_eye"] -= 1
                return FakeResponse(200, TIMEOUT_ERR)
            raise AssertionError(url)

        ex = self._executor(handler)
        with self.assertRaises(Replan):
            ex.use_item("ender_eye")
        self.assertEqual(uses, 1)
        self.assertEqual(self.calls.count("stop"), 1)
        self.assertLess(self.calls.index("stop"), len(self.calls) - 1)
        self.assertEqual(self.calls[-1], "inventory")

    def test_move_to_timeout_behaviour_unchanged(self) -> None:
        """幂等动作：timeout -> /stop -> 退避重发，不触发 status/inventory 判重。"""
        moves = 0

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal moves
            if url.endswith("/move_to"):
                moves += 1
                if moves == 1:
                    return FakeResponse(200, TIMEOUT_ERR)
                return FakeResponse(200, {"ok": True, "result": {"pos": [10, 64, 10]}})
            if url.endswith("/stop"):
                return FakeResponse(200, {"ok": True, "result": {"stopped": True}})
            raise AssertionError(url)

        ex = self._executor(handler)
        res = ex.move_to(10, 10)
        self.assertEqual(res["pos"], [10, 64, 10])
        self.assertEqual(self.calls, ["move_to", "stop", "move_to"])
        self.assertEqual(len(self.sleeps), 1)

    def test_move_to_timeout_exhausts_retries_then_replans(self) -> None:
        moves = 0

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal moves
            if url.endswith("/move_to"):
                moves += 1
                return FakeResponse(200, TIMEOUT_ERR)
            if url.endswith("/stop"):
                return FakeResponse(200, {"ok": True, "result": {"stopped": True}})
            raise AssertionError(url)

        ex = self._executor(handler)
        with self.assertRaises(Replan):
            ex.move_to(10, 10)
        self.assertEqual(moves, 3)  # 默认 retries=2：首发 + 2 次重发
        self.assertEqual(self.calls.count("stop"), 3)
        self.assertNotIn("inventory", self.calls)
        self.assertNotIn("status", self.calls)


class PreemptionAndSessionTests(unittest.TestCase):
    def test_hazard_during_blocking_move_stops_and_runs_escape_before_next_normal_action(self) -> None:
        """验收标准：移动未返回时检测到危险立即 /stop，且后续普通动作绝不抢在逃生动作前执行。"""
        timeline: list[str] = []
        tl_lock = threading.Lock()
        move_started = threading.Event()
        stop_received = threading.Event()

        def rec(tag: str) -> None:
            with tl_lock:
                timeline.append(tag)

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            if url.endswith("/status"):
                return FakeResponse(200, {
                    "ok": True,
                    "result": {"ready": True, "alive": True, "health": 10, "in_lava": True, "pos": [0, 64, 0]},
                })
            if url.endswith("/events"):
                return FakeResponse(200, {"ok": True, "result": {"events": [], "last_seq": 0, "has_more": False}})
            if url.endswith("/stop"):
                rec("stop")
                stop_received.set()
                return FakeResponse(200, {"ok": True, "result": {"stopped": True}})
            if url.endswith("/move_to"):
                if body.get("x") == 100:
                    rec("normal_move_start")
                    move_started.set()
                    # 模拟长耗时阻塞移动，直到收到 /stop 才被打断返回
                    stop_received.wait(timeout=2.0)
                    time.sleep(0.03)
                    return FakeResponse(200, {"ok": False, "error": {"code": "interrupted"}})
                if body.get("x") == -20:
                    rec("escape_move")
                    time.sleep(0.05)
                    return FakeResponse(200, {"ok": True, "result": {"pos": [-20, 65, 0]}})
            if url.endswith("/dig"):
                rec("next_normal_dig")
                return FakeResponse(200, {"ok": True, "result": {"dug": "stone"}})
            raise AssertionError(f"unexpected {url} {body}")

        cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
        c = Client(cfg, log=lambda *_: None)
        c.token = "tok"
        c.session = FakeSession(handler)
        s = State()
        ex = Executor(c, s, log=lambda *_: None)

        def main_worker() -> None:
            try:
                ex.move_to(100, 0)
            except Replan:
                # 主循环刚被 /stop 打断就立刻尝试发下一个普通动作 dig，验证它必须排在逃生动作之后
                ex.dig([1, 64, 1])

        t_main = threading.Thread(target=main_worker)
        t_main.start()
        self.assertTrue(move_started.wait(timeout=1.0))

        mon = SafetyMonitor(
            c, s, ex,
            on_hazard=lambda hz, e, st: e.move_to(-20, 0),
        )
        mon.tick()
        t_main.join(timeout=3.0)

        self.assertEqual(
            timeline,
            ["normal_move_start", "stop", "escape_move", "next_normal_dig"],
        )

    def test_bad_args_is_replan_not_fatal_and_wait_ready_resends_connect_in_lobby(self) -> None:
        self.assertEqual(CODE_CLASS["bad_args"], REPLAN)

        connect_calls = 0
        polls = 0

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            nonlocal connect_calls, polls
            if url.endswith("/status"):
                polls += 1
                # 前 4 次卡在大厅 ready=False，直到收到 /connect 重发切服后变 True
                return FakeResponse(200, {"ok": True, "result": {"ready": connect_calls >= 1, "pos": [1, 64, 1]}})
            if url.endswith("/connect"):
                connect_calls += 1
                return FakeResponse(200, {"ok": True, "result": {"already_connected": True, "resent_server_switch": True}})
            raise AssertionError(url)

        cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None)
        c = Client(cfg, log=lambda *_: None)
        c.token = "tok"
        c.session = FakeSession(handler)

        st = c.wait_ready(timeout=2.0, interval=0.02, min_reconnect_s=0)
        self.assertTrue(st["ready"])
        self.assertGreaterEqual(connect_calls, 1)


if __name__ == "__main__":
    unittest.main()
