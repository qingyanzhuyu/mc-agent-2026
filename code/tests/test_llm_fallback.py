"""LLM 调用降级的离线单测（verified: offline）：代理坏了改直连、代理线路熔断、端点整体熔断。

全部用注入的假传输层和假时钟，不发任何网络请求、不睡眠。阈值的真机标定不在这里。
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import requests

from mcagent.planner import skill_choice
from mcagent.planner.llm import LLMClient, LLMError
from mcagent.skills import Param, Skill, rec
from mcagent.skills.testing import GridWorld

PROXY = "http://127.0.0.1:7897"
MSG = [{"role": "user", "content": "hi"}]


def ok(content: str = '{"a": 1}') -> tuple[int, str]:
    return 200, json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})


def empty() -> tuple[int, str]:
    return 200, json.dumps({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Wire:
    """假传输层：分别给"经代理"和"直连"设定行为，记录每次请求走了哪条线路、给了多少超时。"""

    def __init__(self, clock: Clock, proxy=None, direct=None, cost_s: float = 0.0):
        self.clock, self.proxy, self.direct, self.cost_s = clock, proxy, direct, cost_s
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url, headers, data, timeout, proxy):
        self.calls.append(("proxy" if proxy else "direct", round(timeout, 2)))
        self.clock.t += self.cost_s
        behaviour = self.proxy if proxy else self.direct
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour if behaviour is not None else ok()

    def routes(self) -> list[str]:
        return [r for r, _ in self.calls]


def refused() -> LLMError:
    return LLMError("net: ConnectionRefusedError", kind="net")


def timed_out() -> LLMError:
    return LLMError("timeout", kind="timeout")


class FallbackBase(unittest.TestCase):
    def make(self, **wire):
        self.clock = Clock()
        self.wire = Wire(self.clock, **wire)
        self.logs: list[str] = []
        self.llm = LLMClient(key="k", fast_proxy=PROXY, poster=self.wire, timeout=8, clock=self.clock,
                             breaker_fails=3, breaker_cooldown_s=60, log=self.logs.append)
        return self.llm


class RouteFallbackTests(FallbackBase):
    def test_dead_proxy_falls_back_to_direct_within_the_same_call(self):
        llm = self.make(proxy=refused())
        self.assertEqual(llm.chat(MSG), '{"a": 1}')
        self.assertEqual(self.wire.routes(), ["proxy", "direct"])
        self.assertEqual((llm.last_route, llm.stats["fallback_direct"], llm.stats["direct_ok"]), ("direct", 1, 1))

    def test_gateway_errors_fall_back_but_real_endpoint_answers_do_not(self):
        for status in (502, 503, 504):
            llm = self.make(proxy=(status, "bad gateway"))
            llm.chat(MSG)
            self.assertEqual(self.wire.routes(), ["proxy", "direct"], status)
        for behaviour in ((403, "no access to model"), (500, "oops"), empty(), timed_out()):
            llm = self.make(proxy=behaviour)
            with self.assertRaises(LLMError):
                llm.chat(MSG)
            self.assertEqual(self.wire.routes(), ["proxy"], behaviour)

    def test_fallback_only_spends_what_is_left_of_the_call_budget(self):
        llm = self.make(proxy=refused(), cost_s=3.0)
        llm.chat(MSG, timeout=8)
        self.assertEqual(self.wire.calls, [("proxy", 8.0), ("direct", 5.0)])

        llm = self.make(proxy=refused(), cost_s=7.8)   # 代理拖到只剩 0.2s：不值得再发
        with self.assertRaises(LLMError) as ctx:
            llm.chat(MSG, timeout=8)
        self.assertEqual((ctx.exception.kind, self.wire.routes()), ("net", ["proxy"]))

    def test_thinking_path_and_disabled_fallback_stay_on_one_route(self):
        llm = self.make(direct=refused())
        with self.assertRaises(LLMError):
            llm.chat(MSG, thinking=True)
        self.assertEqual(self.wire.routes(), ["direct"])

        self.clock = Clock()
        self.wire = Wire(self.clock, proxy=refused())
        llm = LLMClient(key="k", fast_proxy=PROXY, poster=self.wire, fallback_direct=False, clock=self.clock, log=None)
        with self.assertRaises(LLMError):
            llm.chat(MSG)
        self.assertEqual(self.wire.routes(), ["proxy"])

    def test_no_proxy_configured_means_plain_direct(self):
        self.clock = Clock()
        self.wire = Wire(self.clock)
        llm = LLMClient(key="k", fast_proxy="", poster=self.wire, clock=self.clock, log=None)
        llm.chat(MSG)
        self.assertEqual((self.wire.routes(), llm.stats["fallback_direct"]), (["direct"], 0))


class ProxyBreakerTests(FallbackBase):
    def test_proxy_is_skipped_after_repeated_failures_then_probed_again(self):
        llm = self.make(proxy=refused())
        for _ in range(3):
            llm.chat(MSG)
        self.assertEqual(self.wire.routes(), ["proxy", "direct"] * 3)
        self.assertEqual(llm.stats["proxy_opened"], 1)
        self.assertTrue(any("改走直连" in line for line in self.logs))

        self.wire.calls.clear()
        llm.chat(MSG)
        llm.chat(MSG)
        self.assertEqual(self.wire.routes(), ["direct", "direct"])   # 熔断期内不再碰代理
        self.assertEqual(llm.health()["proxy_open_s"], 60.0)

        self.clock.t += 60                                          # 冷却结束：试探一次
        self.wire.calls.clear()
        llm.chat(MSG)
        self.assertEqual(self.wire.routes(), ["proxy", "direct"])
        self.assertEqual(llm.stats["proxy_opened"], 2)              # 试探失败立刻重新打开，不用再攒 3 次
        self.wire.calls.clear()
        llm.chat(MSG)
        self.assertEqual(self.wire.routes(), ["direct"])

    def test_recovered_proxy_closes_the_breaker(self):
        llm = self.make(proxy=refused())
        for _ in range(3):
            llm.chat(MSG)
        self.clock.t += 60
        self.wire.proxy = None                                      # 代理恢复
        self.wire.calls.clear()
        llm.chat(MSG)
        llm.chat(MSG)
        self.assertEqual((self.wire.routes(), llm.last_route), (["proxy", "proxy"], "proxy"))
        self.wire.proxy = refused()                                 # 之后再坏，要重新攒够 3 次才熔断
        llm.chat(MSG)
        self.assertEqual(llm.stats["proxy_opened"], 1)

    def test_slow_failing_proxy_never_locks_out_the_direct_route(self):
        # 代理每次都拖光时间预算才失败：直连在同一次调用里来不及试。
        # 这不能算端点故障，否则两个熔断器同开同关，直连永远轮不到。
        llm = self.make(proxy=refused(), cost_s=7.9)
        for _ in range(3):
            with self.assertRaises(LLMError):
                llm.chat(MSG, timeout=8)
        self.assertEqual((llm.stats["proxy_opened"], llm.stats["endpoint_opened"]), (1, 0))
        self.wire.cost_s = 0.3
        self.wire.calls.clear()
        self.assertEqual(llm.chat(MSG, timeout=8), '{"a": 1}')
        self.assertEqual(self.wire.routes(), ["direct"])


class EndpointBreakerTests(FallbackBase):
    def test_endpoint_opens_after_all_routes_keep_failing_and_stops_sending(self):
        llm = self.make(proxy=refused(), direct=refused())
        for _ in range(3):
            with self.assertRaises(LLMError):
                llm.chat(MSG)
        self.assertEqual(llm.stats["endpoint_opened"], 1)
        sent = len(self.wire.calls)
        with self.assertRaises(LLMError) as ctx:
            llm.chat(MSG)
        self.assertEqual((ctx.exception.kind, len(self.wire.calls)), ("circuit_open", sent))
        self.assertEqual(llm.stats["short_circuited"], 1)

        self.clock.t += 60                                          # 冷却后放行一次试探
        self.wire.proxy = self.wire.direct = None
        self.assertEqual(llm.chat(MSG), '{"a": 1}')
        self.assertEqual(llm.health()["endpoint_open_s"], 0.0)

    def test_failed_probe_reopens_immediately(self):
        llm = self.make(proxy=timed_out())
        for _ in range(3):
            with self.assertRaises(LLMError):
                llm.chat(MSG)
        self.clock.t += 60
        with self.assertRaises(LLMError) as ctx:
            llm.chat(MSG)
        self.assertEqual((ctx.exception.kind, llm.stats["endpoint_opened"]), ("timeout", 2))

    def test_model_behaviour_and_starved_deadlines_do_not_trip_it(self):
        llm = self.make(proxy=empty())
        for _ in range(5):
            with self.assertRaises(LLMError):
                llm.chat(MSG)
        llm = self.make(proxy=timed_out())
        for _ in range(5):
            with self.assertRaises(LLMError):
                llm.chat(MSG, timeout=0.3)                          # 调用方只给 0.3s：超时不怪端点
        self.assertEqual(llm.stats["endpoint_opened"], 0)
        llm = self.make(proxy=(403, "quota exhausted"))             # 额度/权限类错误同样别再打
        for _ in range(3):
            with self.assertRaises(LLMError):
                llm.chat(MSG)
        self.assertEqual(llm.stats["endpoint_opened"], 1)

    def test_success_resets_the_failure_streak(self):
        llm = self.make(proxy=timed_out())
        for _ in range(2):
            with self.assertRaises(LLMError):
                llm.chat(MSG)
        self.wire.proxy = None
        llm.chat(MSG)
        self.wire.proxy = timed_out()
        for _ in range(2):
            with self.assertRaises(LLMError):
                llm.chat(MSG)
        self.assertEqual(llm.stats["endpoint_opened"], 0)


class AskJsonDegradeTests(FallbackBase):
    def check(self, obj):
        return True, []

    def test_open_circuit_returns_at_once_without_retry_or_sleep(self):
        llm = self.make(proxy=timed_out())
        with patch("mcagent.planner.llm.time.sleep") as sleep:
            for _ in range(2):
                llm.ask_json("sys", "user", self.check, attempts=2)
            self.assertEqual(llm.stats["endpoint_opened"], 1)       # 第 3 次失败时打开
            sleep.reset_mock()
            sent = len(self.wire.calls)
            obj, errs, used = llm.ask_json("sys", "user", self.check, attempts=2)
        self.assertEqual((obj, used, len(self.wire.calls)), (None, 0, sent))
        self.assertIn("熔断", errs[0])
        sleep.assert_not_called()

    def test_no_sleep_after_the_last_failed_attempt(self):
        llm = self.make(proxy=(500, "oops"))
        with patch("mcagent.planner.llm.time.sleep") as sleep:
            obj, errs, used = llm.ask_json("sys", "user", self.check, attempts=2)
        self.assertEqual((obj, used, sleep.call_count), (None, 2, 1))  # 只在两次尝试之间等一次

    def test_dead_proxy_is_invisible_to_the_caller(self):
        llm = self.make(proxy=refused())
        obj, errs, used = llm.ask_json("sys", "user", self.check)
        self.assertEqual((obj, errs, used), ({"a": 1}, [], 1))

    def test_skill_choice_gets_none_immediately_while_circuit_is_open(self):
        class Wait(Skill):
            name = "wait_here"
            description = "原地等"
            params = {"seconds": Param(int, default=1, min=1, max=5)}
            verified = "live"

            def run(self, ex, state):
                yield rec("noop")

        llm = self.make(proxy=refused(), direct=refused())
        _, state = GridWorld().session()
        with patch("mcagent.planner.llm.time.sleep"):
            for _ in range(2):
                skill_choice.choose(llm, "hp=20", state, registry={"wait_here": Wait})
            sent = len(self.wire.calls)
            skill, errs, used = skill_choice.choose(llm, "hp=20", state, registry={"wait_here": Wait})
        self.assertEqual((skill, used, len(self.wire.calls)), (None, 0, sent))


class TransportClassificationTests(unittest.TestCase):
    def kind_for(self, exc: Exception) -> str:
        llm = LLMClient(key="k", fast_proxy="", log=None)
        with patch.object(requests.Session, "post", side_effect=exc):
            with self.assertRaises(LLMError) as ctx:
                llm._requests_poster("http://x", {}, "{}", 1.0, None)
        return ctx.exception.kind

    def test_requests_exceptions_map_to_kinds(self):
        self.assertEqual(self.kind_for(requests.exceptions.ProxyError("cut")), "net")
        self.assertEqual(self.kind_for(requests.exceptions.ConnectionError("refused")), "net")
        self.assertEqual(self.kind_for(requests.exceptions.ReadTimeout("slow")), "timeout")
        self.assertEqual(self.kind_for(requests.exceptions.ConnectTimeout("slow")), "timeout")

    def test_thresholds_come_from_environment(self):
        with patch.dict(os.environ, {"LLM_BREAKER_FAILS": "1", "LLM_BREAKER_COOLDOWN_S": "5",
                                     "LLM_FALLBACK_DIRECT": "0"}):
            clock = Clock()
            wire = Wire(clock, proxy=refused())
            llm = LLMClient(key="k", fast_proxy=PROXY, poster=wire, clock=clock, log=None)
        with self.assertRaises(LLMError):
            llm.chat(MSG)
        self.assertEqual(wire.routes(), ["proxy"])                  # 关掉了降级直连
        with self.assertRaises(LLMError) as ctx:
            llm.chat(MSG)                                           # 1 次失败即熔断
        self.assertEqual((ctx.exception.kind, llm.health()["endpoint_open_s"]), ("circuit_open", 5.0))


if __name__ == "__main__":
    unittest.main()
