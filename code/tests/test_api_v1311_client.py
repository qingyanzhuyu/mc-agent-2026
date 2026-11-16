"""v1.3.11 客户端封装离线单测：新错误码分类、/control /look 请求体、可选接口能力探测。

运行方式（在 code/ 目录下）：
    python -m unittest tests.test_api_v1311_client -v
"""
from __future__ import annotations

import unittest
from typing import Any

import requests

from mcagent.client import CODE_CLASS, FATAL, REPLAN, ApiError, Client, Config
from mcagent.planner.decision import validate as validate_decision
from mcagent.runtime import Executor, Fatal, Replan, State
from tests.test_d0_core import FakeResponse, FakeSession


def _client(handler) -> Client:
    c = Client(Config("https://mock.local/api/v1", "u", "p", token_file=None), log=lambda *_: None)
    c.token = "tok"
    c.session = FakeSession(handler)
    return c


class CodeClassTests(unittest.TestCase):
    def test_v1311_codes_replan_not_default_by_accident(self) -> None:
        for code in ("stalled", "inventory_full", "dig_unconfirmed", "no_window", "section_full",
                     "no_output", "option_unavailable", "cannot_sleep", "unsupported_api"):
            self.assertEqual(CODE_CLASS.get(code), REPLAN, code)

    def test_required_route_unknown_api_stays_fatal(self) -> None:
        self.assertEqual(CODE_CLASS["unknown_api"], FATAL)
        self.assertEqual(CODE_CLASS["bad_args"], REPLAN)


class ControlLookBodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, Any]] = []

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            self.calls.append((url.rsplit("/", 1)[-1], body))
            return FakeResponse(200, {"ok": True, "result": {"ms": 210, "moved": 0.8, "pos": [1.5, 64, 2.5]}})

        self.client = _client(handler)

    def test_control_drops_unset_keys_and_adds_stop_on(self) -> None:
        self.client.control(forward=True, jump=True, yaw=1.57, ms=200)
        route, body = self.calls[-1]
        self.assertEqual(route, "control")
        self.assertEqual(body, {"forward": True, "jump": True, "yaw": 1.57, "ms": 200, "stop_on": ["hurt", "death"]})

    def test_control_hit_only_passes_empty_stop_on(self) -> None:
        self.client.control(hit=3, hit_interval_ms=450, stop_on=[])
        self.assertEqual(self.calls[-1][1], {"hit": 3, "hit_interval_ms": 450, "stop_on": []})

    def test_look_accepts_angles_pos_or_entity(self) -> None:
        self.client.look(yaw=0.5, pitch=0)
        self.client.look(pos=(448, 64, 220))
        self.client.look(entity_id=812)
        self.assertEqual([b for _, b in self.calls], [
            {"yaw": 0.5, "pitch": 0}, {"x": 448, "y": 64, "z": 220}, {"entity_id": 812},
        ])
        with self.assertRaises(ValueError):
            self.client.look()

    def test_attack_limited_hits(self) -> None:
        self.client.attack(812, max_hits=1, interval_ms=300)
        self.assertEqual(self.calls[-1][1]["max_hits"], 1)
        self.assertEqual(self.calls[-1][1]["interval_ms"], 300)
        self.client.attack(812)
        self.assertNotIn("max_hits", self.calls[-1][1])


class CapabilityProbeTests(unittest.TestCase):
    def _old_server(self, *, envelope: bool = False):
        hits: list[str] = []

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            route = url.rsplit("/", 1)[-1]
            hits.append(route)
            if route in ("control", "look"):
                if envelope:
                    return FakeResponse(200, {"ok": False, "error": {"code": "unknown_api", "message": "no route"}})
                return FakeResponse(404, {"ok": False, "error": {"code": "unknown_api"}})
            return FakeResponse(200, {"ok": True, "result": {"released": True}})

        return _client(handler), hits

    def test_unknown_api_on_optional_route_becomes_replan_and_is_cached(self) -> None:
        for envelope in (False, True):
            with self.subTest(envelope=envelope):
                c, hits = self._old_server(envelope=envelope)
                self.assertIsNone(c.supports("control"))
                with self.assertRaises(ApiError) as first:
                    c.control(forward=True)
                self.assertEqual(first.exception.code, "unsupported_api")
                self.assertEqual(first.exception.kind, REPLAN)
                self.assertIs(c.supports("control"), False)
                with self.assertRaises(ApiError):
                    c.control(forward=True)
                self.assertEqual(hits.count("control"), 1, "已知不支持后不应再发请求")

    def test_executor_control_on_old_server_replans_not_fatal(self) -> None:
        c, hits = self._old_server()
        ex = Executor(c, State(), log=lambda *_: None)
        with self.assertRaises(Replan) as ctx:
            ex.control(forward=True, ms=200)
        self.assertNotIsInstance(ctx.exception, Fatal)
        self.assertEqual(hits.count("control"), 1)
        # 不支持不该清掉执行器状态：后续普通动作照常可发
        self.assertIsNone(ex.active_action)

    def test_executor_look_on_old_server_raises_replan_kind(self) -> None:
        c, _ = self._old_server()
        ex = Executor(c, State(), log=lambda *_: None)
        with self.assertRaises(ApiError) as ctx:
            ex.look(yaw=0, pitch=0)
        self.assertEqual(ctx.exception.kind, REPLAN)

    def test_required_route_unknown_api_still_fatal_in_executor(self) -> None:
        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            return FakeResponse(404, {"ok": False, "error": {"code": "unknown_api"}})

        ex = Executor(_client(handler), State(), log=lambda *_: None)
        with self.assertRaises(Fatal):
            ex.dig((1, 64, 1))

    def test_business_error_marks_route_supported_network_does_not(self) -> None:
        sent: list[int] = []

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            sent.append(1)
            if len(sent) == 1:
                raise requests.ConnectionError("reset")
            return FakeResponse(200, {"ok": False, "error": {"code": "bad_args", "required_ms": 950}})

        c = _client(handler)
        with self.assertRaises(ApiError):
            c.control(hit=3, ms=100)
        self.assertIsNone(c.supports("control"))
        with self.assertRaises(ApiError) as ctx:
            c.control(hit=3, ms=100)
        self.assertEqual(ctx.exception.code, "bad_args")
        self.assertIs(c.supports("control"), True)


class ControlRetryTests(unittest.TestCase):
    def test_control_timeout_stops_and_does_not_resend(self) -> None:
        hits: list[str] = []

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            route = url.rsplit("/", 1)[-1]
            hits.append(route)
            if route == "control":
                return FakeResponse(200, {"ok": False, "error": {"code": "timeout"}})
            return FakeResponse(200, {"ok": True, "result": {"released": True}})

        ex = Executor(_client(handler), State(), log=lambda *_: None)
        with self.assertRaises(Replan):
            ex.control(forward=True, ms=300)
        self.assertEqual(hits.count("control"), 1)
        self.assertIn("stop", hits, "按键动作超时后须 /stop 松开按键")

    def test_limited_attack_not_retried_on_busy(self) -> None:
        hits: list[str] = []

        def handler(method: str, url: str, headers: dict, body: Any) -> FakeResponse:
            hits.append(url.rsplit("/", 1)[-1])
            return FakeResponse(200, {"ok": False, "error": {"code": "busy"}})

        ex = Executor(_client(handler), State(), log=lambda *_: None)
        with self.assertRaises(Replan):
            ex.attack(812, max_hits=1)
        self.assertEqual(hits.count("attack"), 1)


class DecisionAttackCrossRuleTests(unittest.TestCase):
    def test_attack_max_hits_and_interval_bounds(self) -> None:
        ok, errs = validate_decision({
            "goal": "combat:creeper",
            "action": "attack",
            "args": {"entity_id": 812, "max_hits": 1, "interval_ms": 300},
        })
        self.assertTrue(ok, errs)

        for bad_args in (
            {"entity_id": 812, "max_hits": 0},
            {"entity_id": 812, "max_hits": True},
            {"entity_id": 812, "interval_ms": 49},
            {"entity_id": 812, "interval_ms": 5001},
            {"entity_id": 812, "interval_ms": "300"},
        ):
            with self.subTest(bad_args=bad_args):
                ok, errs = validate_decision({"goal": "combat:creeper", "action": "attack", "args": bad_args})
                self.assertFalse(ok)
                self.assertTrue(errs)


if __name__ == "__main__":
    unittest.main()
