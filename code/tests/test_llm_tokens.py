"""Token 花费采集、重问累加、失败不计数、`LLM_TOKEN_BUDGET` 与观察者汇总离线单测（verified: offline）。

完全断网可跑：用注入的假 `poster` 与本地回环 `mock-llm-server` 验证：
1. 多次调用 `usage`（prompt/completion/total）在 `LLMClient.stats` 与 `usage_out` 中准确累加；
2. `ask_json` / `skill_choice.choose` 单次决策内多次重问的 token 用量累加；
3. 失败请求（HTTP 非 200、网络/超时、非法包体、API error、空 content、熔断/预算拦截）一律不计入 token；
4. `LLM_TOKEN_BUDGET=0` 只记账不熔断，`>0` 达上限后零等待切回规则且不误触端点熔断；
5. `choose -> CampaignPlanner -> SoakObserver` 写入 `timeline.jsonl`、`progress_curve.jsonl`（累计值）与 `summary.md`
   （含决策来源 `llm` / `llm_fallback_rule` / `rule` 比例、初赛公告额度占比、8h 推算与可选单价开关）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcagent.loop import CampaignPlanner, SoakObserver, reset_world_memo
from mcagent.loop.auditor import compute_token_valuation
from mcagent.planner import llm as llm_mod
from mcagent.planner import skill_choice
from mcagent.planner.llm import LLMClient, LLMError
from mcagent.runtime import State
from mcagent.skills import base as skill_base
from mcagent.skills import make
from mcagent.skills.testing import GridWorld


def _reply_with_usage(content: str, prompt_tokens: int, completion_tokens: int,
                      *, finish_reason: str = "stop") -> tuple[int, str]:
    return 200, json.dumps({
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    })


def _load_mock_server():
    repo_root = Path(__file__).resolve().parents[2]
    mod_path = repo_root / "tools" / "mock-llm-server" / "server.py"
    spec = importlib.util.spec_from_file_location("mock_llm_server_under_test", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LLMTokenCollectionTests(unittest.TestCase):
    def setUp(self):
        llm_mod._reset_semaphores()
        skill_base._COOLDOWN.clear()
        reset_world_memo()

    def tearDown(self):
        llm_mod._reset_semaphores()

    def test_usage_accumulates_across_multiple_calls(self):
        seq = [
            _reply_with_usage('{"ok": 1}', 120, 30),
            _reply_with_usage('{"ok": 2}', 205, 45),
        ]
        idx = {"i": 0}

        def poster(url, headers, data, timeout, proxy):
            r = seq[idx["i"]]
            idx["i"] += 1
            return r

        llm = LLMClient(key="k", fast_proxy="", poster=poster, log=None)
        u1: dict[str, int] = {}
        u2: dict[str, int] = {}
        llm.chat([{"role": "user", "content": "a"}], usage_out=u1)
        llm.chat([{"role": "user", "content": "b"}], usage_out=u2)

        self.assertEqual((u1["prompt_tokens"], u1["completion_tokens"], u1["total_tokens"]), (120, 30, 150))
        self.assertEqual((u2["prompt_tokens"], u2["completion_tokens"], u2["total_tokens"]), (205, 45, 250))
        self.assertEqual(
            (u2["cumulative_prompt_tokens"], u2["cumulative_completion_tokens"], u2["cumulative_total_tokens"]),
            (325, 75, 400),
        )
        self.assertEqual(
            (llm.stats["prompt_tokens"], llm.stats["completion_tokens"], llm.stats["total_tokens"]),
            (325, 75, 400),
        )

    def test_reask_within_single_decision_accumulates_all_attempts(self):
        world = GridWorld()
        _, state = world.session()
        seq = [
            # 第 1 问返回不存在的技能 -> 触发校验失败重问
            _reply_with_usage('{"skill": "teleport_home", "args": {}, "why": "bad"}', 310, 28),
            # 第 2 问修正为合法可启动技能
            _reply_with_usage('{"skill": "gather_wood", "args": {"count": 3}, "why": "chop"}', 390, 32),
            # 第 2 次决策：连续两问都不合法 -> 返回 None，但两次成功 HTTP 响应的 token 仍须累计
            _reply_with_usage("不是JSON文本", 300, 20),
            _reply_with_usage('{"skill": "fly_away"}', 340, 25),
        ]
        idx = {"i": 0}

        def poster(url, headers, data, timeout, proxy):
            r = seq[idx["i"]]
            idx["i"] += 1
            return r

        llm = LLMClient(key="k", fast_proxy="", poster=poster, log=None)
        u_first: dict[str, int] = {}
        skill, errs, used = skill_choice.choose(
            llm, "hp=20 food=20", state, min_verified="offline", attempts=2, usage_out=u_first
        )
        self.assertIsNotNone(skill)
        self.assertEqual((skill.name, errs, used), ("gather_wood", [], 2))
        self.assertEqual((u_first["prompt_tokens"], u_first["completion_tokens"], u_first["total_tokens"]),
                         (700, 60, 760))

        u_second: dict[str, int] = {}
        skill2, errs2, used2 = skill_choice.choose(
            llm, "hp=20 food=20", state, min_verified="offline", attempts=2, usage_out=u_second
        )
        self.assertIsNone(skill2)
        self.assertTrue(errs2)
        self.assertEqual(used2, 2)
        self.assertEqual((u_second["prompt_tokens"], u_second["completion_tokens"], u_second["total_tokens"]),
                         (640, 45, 685))
        self.assertEqual((llm.stats["prompt_tokens"], llm.stats["completion_tokens"], llm.stats["total_tokens"]),
                         (1340, 105, 1445))

    def test_failed_requests_never_count_tokens(self):
        failures = [
            # HTTP 500 即使包体带 usage 也绝不计数
            (500, json.dumps({"error": "internal", "usage": {"prompt_tokens": 999, "completion_tokens": 111}})),
            (429, json.dumps({"error": "rate_limit", "usage": {"prompt_tokens": 888, "completion_tokens": 222}})),
            # 非 JSON 乱码包体
            (200, "<html>502 Bad Gateway</html>"),
            # HTTP 200 但带顶层 error 字段
            (200, json.dumps({"error": "upstream failed", "usage": {"prompt_tokens": 777, "completion_tokens": 333}})),
            # 空 content（思考耗尽 max_tokens 静默失败）
            _reply_with_usage("", 450, 900, finish_reason="length"),
            # 传输层异常
            LLMError("net down", kind="net"),
            LLMError("read timed out", kind="timeout"),
        ]
        for case in failures:
            def poster(url, headers, data, timeout, proxy, _c=case):
                if isinstance(_c, Exception):
                    raise _c
                return _c

            llm = LLMClient(key="k", fast_proxy="", poster=poster, breaker_fails=10, log=None)
            with self.assertRaises(LLMError):
                llm.chat([{"role": "user", "content": "x"}])
            self.assertEqual(
                (llm.stats["prompt_tokens"], llm.stats["completion_tokens"], llm.stats["total_tokens"]),
                (0, 0, 0),
                f"失败场景不应计入 token: {case!r}",
            )

        # ask_json 第 1 次 HTTP 500 失败、第 2 次 HTTP 200 成功：只计第 2 次的用量
        mixed = [
            (500, json.dumps({"usage": {"prompt_tokens": 999, "completion_tokens": 999}})),
            _reply_with_usage('{"ok": true}', 140, 22),
        ]
        m_idx = {"i": 0}

        def mixed_poster(*_a):
            r = mixed[m_idx["i"]]
            m_idx["i"] += 1
            return r

        llm2 = LLMClient(key="k", fast_proxy="", poster=mixed_poster, log=None)
        u_mix: dict[str, int] = {}
        with patch("mcagent.planner.llm.time.sleep"):
            obj, errs, used = llm2.ask_json("sys", "u", lambda o: (True, []), attempts=2, usage_out=u_mix)
        self.assertEqual((obj, errs, used), ({"ok": True}, [], 2))
        self.assertEqual((u_mix["prompt_tokens"], u_mix["completion_tokens"]), (140, 22))
        self.assertEqual((llm2.stats["prompt_tokens"], llm2.stats["completion_tokens"]), (140, 22))

    def test_token_budget_zero_only_accounts_and_positive_budget_cuts_off_cleanly(self):
        calls = {"n": 0}

        def poster(*_a):
            calls["n"] += 1
            return _reply_with_usage('{"skill": "gather_wood", "args": {"count": 3}, "why": "ok"}', 300, 50)

        # 1) LLM_TOKEN_BUDGET=0：只记账，不熔断
        with patch.dict(os.environ, {"LLM_TOKEN_BUDGET": "0"}):
            llm_free = LLMClient(key="k", fast_proxy="", poster=poster, log=None)
            for _ in range(4):
                llm_free.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(llm_free.stats["total_tokens"], 1400)
            self.assertEqual(llm_free.stats["budget_exceeded"], 0)

        # 2) LLM_TOKEN_BUDGET=600：前两次合计 700 >= 600，第 3 次起零请求零等待直接走规则兜底，且不打开端点熔断
        calls["n"] = 0
        with patch.dict(os.environ, {"LLM_TOKEN_BUDGET": "600"}):
            llm_cap = LLMClient(key="k", fast_proxy="", poster=poster, breaker_fails=1, log=None)
            world = GridWorld(blocks={(2, 64, 0): "oak_log"})
            _, state = world.session()
            planner = CampaignPlanner(mode="campaign", use_llm=True, llm=llm_cap, min_verified="offline")

            d1 = planner.select(state, [])
            d2 = planner.select(state, [])
            self.assertEqual((d1.source, d2.source, calls["n"], llm_cap.stats["total_tokens"]), ("llm", "llm", 2, 700))

            with patch("mcagent.planner.llm.time.sleep") as sleep_mock:
                d3 = planner.select(state, [])
            self.assertEqual(d3.source, "llm_fallback_rule")
            self.assertEqual(d3.skill.name, "gather_wood")
            self.assertEqual(calls["n"], 2)  # 达上限后未再发任何 HTTP 请求
            self.assertEqual(llm_cap.stats["budget_exceeded"], 1)
            self.assertEqual(llm_cap.stats["endpoint_opened"], 0)  # 本地预算上限不怪远端端点
            sleep_mock.assert_not_called()

    def test_pipeline_observer_timeline_curve_summary_and_price_toggle(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = GridWorld(blocks={(2, 64, 0): "oak_log"})
            ex, state = world.session()

            # 第 1 次决策成功（400/40）；第 2 次决策两问均非法退回规则（300/30 + 350/35 = 650/65）
            seq = [
                _reply_with_usage('{"skill": "gather_wood", "args": {"count": 1}, "why": "砍树"}', 400, 40),
                _reply_with_usage("胡言乱语", 300, 30),
                _reply_with_usage('{"skill": "unknown_skill"}', 350, 35),
            ]
            idx = {"i": 0}

            def poster(*_a):
                r = seq[idx["i"]]
                idx["i"] += 1
                return r

            llm = LLMClient(key="k", fast_proxy="", poster=poster, log=None)
            obs = SoakObserver(tmp, source="local-target")
            obs.started_mono -= 60.0  # 模拟已运行 60s 以检验 8h 外推
            planner = CampaignPlanner(
                mode="campaign",
                use_llm=True,
                llm=llm,
                min_verified="offline",
                on_llm_telemetry=obs.on_llm_call,
            )

            obs.on_run_start(state)

            # 决策 1：llm
            dec1 = planner.select(state, [])
            self.assertEqual(dec1.source, "llm")
            obs.on_skill_start(dec1.skill, dec1.source, dec1.reason, state)
            obs.on_step(dec1.skill, {"op": "dig"}, state)
            obs.on_skill_end(dec1.skill, "replan", state, error_code="path_not_found")

            # 决策 2：llm_fallback_rule
            skill_base._COOLDOWN.clear()
            dec2 = planner.select(state, [])
            self.assertEqual(dec2.source, "llm_fallback_rule")
            obs.on_skill_start(dec2.skill, dec2.source, dec2.reason, state)
            obs.on_step(dec2.skill, {"op": "move_to"}, state)
            obs.on_skill_end(dec2.skill, "replan", state, error_code="path_not_found")

            # 决策 3：进入 safe_mode 走纯规则 rule（饥饿状态优先进食）
            skill_base._COOLDOWN.clear()
            state.set_status({**state.st, "food": 10})
            state.set_inventory({"items": [{"name": "bread", "count": 2, "slot": 0}]})
            state.safe_mode_until = time.monotonic() + 30.0
            dec3 = planner.select(state, [])
            self.assertEqual(dec3.source, "rule")
            obs.on_skill_start(dec3.skill, dec3.source, dec3.reason, state)
            obs.on_step(dec3.skill, {"op": "use_item"}, state)
            obs.on_skill_end(dec3.skill, "completed", state)

            # 默认单价为 0：不显示金额
            with patch.dict(os.environ, {"LLM_PRICE_IN_PER_M": "0", "LLM_PRICE_OUT_PER_M": "0"}):
                obs.on_run_end(state)
                summary_no_price = (Path(tmp) / "summary.md").read_text(encoding="utf-8")

            self.assertIn("成功率 `1/2` (`50.0%`)", summary_no_price)
            self.assertIn("`llm` `1` 次 (`33.3%`)", summary_no_price)
            self.assertIn("`llm_fallback_rule` `1` 次 (`33.3%`)", summary_no_price)
            self.assertIn("`rule` `1` 次 (`33.3%`)", summary_no_price)
            self.assertIn("## Token 花费", summary_no_price)
            self.assertIn("输入 `1,050` · 输出 `105` · 合计 `1,155` tokens", summary_no_price)
            self.assertIn("8 小时满场推算", summary_no_price)
            self.assertNotIn("估算金额", summary_no_price)
            self.assertNotIn("¥", summary_no_price)

            # 配置非零单价后：显示金额与 8h 金额推算
            with patch.dict(os.environ, {"LLM_PRICE_IN_PER_M": "2.0", "LLM_PRICE_OUT_PER_M": "8.0"}):
                summary_with_price = obs.write_summary()
            self.assertIn("估算金额", summary_with_price)
            self.assertIn("¥", summary_with_price)

            val = compute_token_valuation(80_000, 15_000, 288.0, price_in_per_m=1.0, price_out_per_m=2.0)
            self.assertAlmostEqual(val["in_quota_pct"], 1.0)
            self.assertAlmostEqual(val["out_quota_pct"], 1.0)
            self.assertAlmostEqual(val["proj_8h_in_pct"], 100.0)
            self.assertAlmostEqual(val["proj_8h_out_pct"], 100.0)
            self.assertAlmostEqual(val["cost"], 0.11)
            self.assertAlmostEqual(val["proj_8h_cost"], 11.0)

            # 核查 timeline.jsonl 与 progress_curve.jsonl 累计值
            tl_rows = [json.loads(x) for x in (Path(tmp) / "timeline.jsonl").read_text(encoding="utf-8").splitlines() if x]
            llm_rows = [r for r in tl_rows if r["event"] == "llm_call"]
            self.assertEqual(len(llm_rows), 2)
            self.assertEqual((llm_rows[0]["prompt_tokens"], llm_rows[0]["cumulative_prompt_tokens"]), (400, 400))
            self.assertEqual((llm_rows[1]["prompt_tokens"], llm_rows[1]["cumulative_prompt_tokens"]), (650, 1050))

            curve_rows = [json.loads(x) for x in (Path(tmp) / "progress_curve.jsonl").read_text(encoding="utf-8").splitlines() if x]
            self.assertEqual((curve_rows[-1]["prompt_tokens"], curve_rows[-1]["completion_tokens"]), (1050, 105))

    def test_mock_llm_server_loopback_guard_and_end_to_end_usage(self):
        mock_srv = _load_mock_server()
        with self.assertRaisesRegex(ValueError, "回环地址"):
            mock_srv.make_server("0.0.0.0", 0, quiet=True)

        srv = mock_srv.make_server("127.0.0.1", 0, quiet=True)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            world = GridWorld(blocks={(2, 64, 0): "oak_log"})
            _, state = world.session()
            llm = LLMClient(base=f"http://127.0.0.1:{port}/v1", key="mock-key", fast_proxy="", log=None)
            usage: dict[str, int] = {}
            skill, errs, used = skill_choice.choose(
                llm, "位置=[0,64,0]\n物资：最优镐=无 原木=0 ", state, min_verified="offline", usage_out=usage
            )
            self.assertIsNotNone(skill)
            self.assertEqual((skill.name, errs, used), ("gather_wood", [], 1))
            self.assertGreater(usage["prompt_tokens"], 300)
            self.assertGreater(usage["completion_tokens"], 20)
            self.assertEqual(llm.stats["total_tokens"], usage["total_tokens"])
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(timeout=2)

    def test_cooling_fixed_goal_hidden_from_ready_menu_and_empty_menu_skips_llm(self):
        world = GridWorld()
        _, state = world.session()
        make("gather_wood").mark_failed()
        m = skill_choice.menu(state, min_verified="offline")
        # gather_wood 处于冷却，不应继续出现在可启动列表诱骗 LLM 盲选被拒
        ready_block = m.split("暂不可启动")[0]
        self.assertNotIn("gather_wood(", ready_block)
        self.assertIn("gather_wood：目标 acq:wood 刚失败冷却中", m)
        self.assertTrue(skill_choice.has_ready_skill(state, min_verified="offline"))  # scout_island 仍可选

        # 当 scout_island 也进入冷却时，空包状态下已无任何可启动技能 -> CampaignPlanner 直接跳过 LLM 零 token 空转
        make("scout_island").mark_failed()
        self.assertFalse(skill_choice.has_ready_skill(state, min_verified="offline"))
        calls = {"n": 0}
        llm = LLMClient(key="k", fast_proxy="", poster=lambda *_a: (calls.__setitem__("n", calls["n"] + 1), {})[1], log=None)
        planner = CampaignPlanner(mode="campaign", use_llm=True, llm=llm, min_verified="offline")
        dec = planner.select(state, [])
        self.assertEqual(dec.source, "idle")
        self.assertEqual(calls["n"], 0)
        self.assertEqual(llm.stats["total_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
