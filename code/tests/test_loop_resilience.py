"""主循环瞬时故障自愈与官方端点判定的离线单测（verified: offline）。

覆盖：
1. 读状态遇 5xx / 网络抖动按退避重试并继续推进，不让 8 小时进程退出；
2. 连续故障超过停机上限才停，停机原因为 transient_outage，且不派 GOAL_MISS 修单；
3. 致命错误（凭据类）仍立即穿透；
4. 启动时等不到 ready、恢复过程本身失败、技能失败后的状态复读失败都不当作死亡；
5. probe_path 与挂机工具按主机名识别公网、校园网两个官方入口。

退避用注入的 sleep / clock，不依赖真实计时。
"""
from __future__ import annotations

import dataclasses
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcagent.client import ApiError, Config
from mcagent.loop import CampaignPlanner, LoopConfig, MainLoop, SoakObserver, reset_world_memo
from mcagent.runtime import RuntimeConfig, State
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import CountingExecutor

from tests.test_loop_edge_cases import EdgeCaseMockClient


def _load_soak_ctl():
    mod_path = Path(__file__).resolve().parents[2] / "tools" / "soak-autofix" / "soak_ctl.py"
    spec = importlib.util.spec_from_file_location("soak_ctl_resilience_test", mod_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class FakeClock:
    """sleep 推进时间；clock 读当前时间。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(round(s, 3))
        self.now += s

    def __call__(self) -> float:
        return self.now


def _net_err() -> ApiError:
    return ApiError("network", "HTTP 502: Bad Gateway", http=502)


class LoopHarness:
    def __init__(self, client: EdgeCaseMockClient, **cfg):
        self.client = client
        self.clock = FakeClock()
        # TTL 置 0：每拍开头的刷新必定真实调用 status()，注入的第 n 次错误位置才确定
        self.state = State(dataclasses.replace(RuntimeConfig.load(), status_ttl_s=0.0))
        self.ex = CountingExecutor(client, self.state)
        self.planner = CampaignPlanner(mode="campaign", min_verified="offline")
        base = dict(idle_sleep_s=0.0, respawn_poll_s=0.001, respawn_timeout_s=0.05, enable_monitor=False,
                    transient_retries=2, transient_backoff_s=1.0, transient_backoff_max_s=4.0,
                    outage_limit_s=30.0)
        base.update(cfg)
        self.cfg = LoopConfig(**base)
        self.logs: list[str] = []

    def loop(self, observer=None) -> MainLoop:
        return MainLoop(self.client, self.state, self.ex, self.planner, config=self.cfg, observer=observer,
                        log=lambda *a: self.logs.append(" ".join(str(x) for x in a)),
                        sleep=self.clock.sleep, clock=self.clock)


def _status_script(client: EdgeCaseMockClient, failures: dict[int, ApiError]):
    """按 status() 第 n 次调用注入错误；其余正常返回。"""
    calls = {"n": 0}
    orig = client.status

    def status(*args, **kwargs):
        calls["n"] += 1
        err = failures.get(calls["n"])
        if err is not None:
            raise err
        return orig(*args, **kwargs)

    client.status = status
    return calls


class TestTransientRefresh(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_first_tick_502_is_retried_and_loop_keeps_running(self):
        """启动后第一次刷新连遇两次 502：按 1s、2s 退避后恢复，继续推进到步数上限。"""
        client = EdgeCaseMockClient()
        _status_script(client, {2: _net_err(), 3: _net_err()})  # 第 1 次是启动时的 wait_ready
        h = LoopHarness(client, max_steps=3)
        with patch("time.sleep"):
            res = h.loop().run()
        self.assertEqual(h.clock.sleeps[:2], [1.0, 2.0])
        self.assertEqual(res.transient_faults, 0)
        self.assertEqual(res.deaths_recovered, 0)
        self.assertNotEqual(res.stop_reason, "transient_outage")
        self.assertGreater(res.steps + res.skills_completed, 0)

    def test_retries_exhausted_skips_tick_then_recovers(self):
        """单拍重试用尽：跳过这一拍、计一次瞬时故障，下一拍恢复后照常推进。"""
        client = EdgeCaseMockClient()
        _status_script(client, {2: _net_err(), 3: _net_err(), 4: _net_err()})
        h = LoopHarness(client, max_steps=3)
        with patch("time.sleep"):
            res = h.loop().run()
        self.assertEqual(res.transient_faults, 1)
        self.assertEqual(res.deaths_recovered, 0)
        self.assertNotEqual(res.stop_reason, "transient_outage")
        self.assertTrue(any("跳过本拍" in line for line in h.logs))

    def test_long_outage_stops_with_transient_outage_and_no_goal_miss(self):
        """持续读不到状态超过停机上限：以 transient_outage 停机，不当作死亡，也不派 GOAL_MISS。"""
        client = EdgeCaseMockClient()
        calls = {"n": 0}
        orig = client.status

        def always_down_after_start(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise _net_err()
            return orig(*args, **kwargs)

        client.status = always_down_after_start
        h = LoopHarness(client)
        with tempfile.TemporaryDirectory() as tmp:
            obs = SoakObserver(tmp, target_goal="mine_stone")
            with patch.object(obs, "_report_goal_miss") as goal_miss, patch("time.sleep"):
                res = h.loop(observer=obs).run()
        self.assertEqual(res.stop_reason, "transient_outage")
        self.assertGreaterEqual(res.transient_faults, 1)
        self.assertEqual(res.deaths_recovered, 0)
        goal_miss.assert_not_called()
        self.assertTrue(all(s <= 4.0 for s in h.clock.sleeps))  # 退避有上限

    def test_internal_error_on_status_is_transient_not_replan(self):
        """服务端偶发 internal（默认归 REPLAN 类）出现在读状态时，也按瞬时故障处理。"""
        client = EdgeCaseMockClient()
        _status_script(client, {2: ApiError("internal", "Cannot read properties of null")})
        h = LoopHarness(client, max_steps=2)
        with patch("time.sleep"):
            res = h.loop().run()
        self.assertEqual(res.deaths_recovered, 0)
        self.assertNotEqual(res.stop_reason, "transient_outage")

    def test_fatal_error_still_propagates(self):
        """凭据类致命错误不重试，原样抛出。"""
        client = EdgeCaseMockClient()
        _status_script(client, {2: ApiError("invalid_credentials", "bad password")})
        h = LoopHarness(client, max_steps=3)
        with patch("time.sleep"), self.assertRaises(ApiError) as cm:
            h.loop().run()
        self.assertEqual(cm.exception.code, "invalid_credentials")
        self.assertEqual(h.clock.sleeps, [])


class TestGuardedStartupAndRecovery(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_startup_wait_ready_network_failure_is_retried(self):
        client = EdgeCaseMockClient()
        orig = client.wait_ready
        fails = {"left": 2}

        def flaky_wait_ready(timeout: float = 120):
            if fails["left"] > 0:
                fails["left"] -= 1
                raise _net_err()
            return orig(timeout=timeout)

        client.wait_ready = flaky_wait_ready
        h = LoopHarness(client, max_steps=2)
        with patch("time.sleep"):
            res = h.loop().run()
        self.assertEqual(h.clock.sleeps[:2], [1.0, 2.0])
        self.assertNotEqual(res.stop_reason, "transient_outage")

    def test_startup_never_ready_stops_after_outage_limit(self):
        client = EdgeCaseMockClient()

        def down(timeout: float = 120):
            raise ApiError("not_ready", "120s 内没 ready")

        client.wait_ready = down
        h = LoopHarness(client)
        with patch("time.sleep"):
            res = h.loop().run()
        self.assertEqual(res.stop_reason, "transient_outage")
        self.assertEqual(res.steps, 0)

    def test_recovery_timeout_does_not_crash_the_process(self):
        """断线恢复本身超时（重连一直报网络错误）：不抛出，按停机上限结束。"""
        client = EdgeCaseMockClient()
        calls = {"n": 0}
        orig = client.status

        def status(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise ApiError("not_connected", "socket closed")
            return orig(*args, **kwargs)

        def ensure_connected(min_interval_s: float = 0.0, timeout: float = 20):
            raise _net_err()

        client.status = status
        client.ensure_connected = ensure_connected
        h = LoopHarness(client)
        with patch("time.sleep"):
            res = h.loop().run()
        self.assertEqual(res.stop_reason, "transient_outage")
        self.assertEqual(res.deaths_recovered, 0)
        self.assertGreaterEqual(res.transient_faults, 1)

    def test_refresh_failure_after_skill_error_is_not_counted_as_death(self):
        """技能失败后复读状态遇到网关抖动：技能照常记失败，但不按死亡/断线去恢复。"""
        client = EdgeCaseMockClient()
        client.action_errors = [ApiError("nothing_to_dig", "air")]
        orig = client.status
        state = {"fail_next": False}
        orig_dig = client.dig

        def dig(pos, collect=True, *, stop_on=None):
            if client.action_errors:
                state["fail_next"] = True
            return orig_dig(pos, collect=collect, stop_on=stop_on)

        def status(*args, **kwargs):
            if state["fail_next"]:
                state["fail_next"] = False
                raise _net_err()
            return orig(*args, **kwargs)

        client.dig = dig
        client.status = status
        h = LoopHarness(client, max_steps=6)
        with patch("time.sleep"):
            res = h.loop().run()
        self.assertGreaterEqual(res.skills_replan, 1)
        self.assertEqual(res.deaths_recovered, 0)


class TestOfficialEndpointDetection(unittest.TestCase):
    def test_probe_path_accepts_campus_endpoint_as_official(self):
        from mcagent import probe_path

        for base, official in (("https://apiin.nmo.net.cn/api/v1", True),
                               ("https://mc-api.windywindie.top/api/v1", True),
                               ("http://127.0.0.1:8787/api/v1", False)):
            with self.subTest(base=base), \
                 patch("mcagent.probe_path.Config.load", return_value=Config(base, "u", "p")), \
                 patch("mcagent.probe_path.Client", side_effect=RuntimeError("stop-before-network")):
                if official:
                    # 官方端点通过入口判定，直到真正创建客户端才停下
                    with self.assertRaises(RuntimeError):
                        probe_path.pathfinding_main(["--pathfinding", "--snapshot", "--live"])
                else:
                    with self.assertRaises(SystemExit):
                        probe_path.pathfinding_main(["--pathfinding", "--snapshot", "--live"])


class TestSoakOfficialBase(unittest.TestCase):
    def setUp(self):
        self.soak_ctl = _load_soak_ctl()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "code" / "mcagent").mkdir(parents=True)
        self.env_file = self.repo / "code" / "mcagent" / ".env"
        self.clean_env = {k: v for k, v in os.environ.items() if k != "MC_OFFICIAL_API_BASE"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_is_public_endpoint(self):
        with patch.dict(os.environ, self.clean_env, clear=True):
            self.assertEqual(self.soak_ctl.official_mc_base(self.repo), "https://mc-api.windywindie.top/api/v1")

    def test_campus_endpoint_selectable_via_env_file_or_process_env(self):
        with patch.dict(os.environ, self.clean_env, clear=True):
            self.env_file.write_text("MC_OFFICIAL_API_BASE=https://apiin.nmo.net.cn/api/v1/\n", encoding="utf-8")
            self.assertEqual(self.soak_ctl.official_mc_base(self.repo), "https://apiin.nmo.net.cn/api/v1")
        env = dict(self.clean_env, MC_OFFICIAL_API_BASE="https://apiin.nmo.net.cn/api/v1")
        with patch.dict(os.environ, env, clear=True):
            self.env_file.write_text("", encoding="utf-8")
            self.assertEqual(self.soak_ctl.official_mc_base(self.repo), "https://apiin.nmo.net.cn/api/v1")

    def test_non_official_host_is_rejected_and_reported(self):
        with patch.dict(os.environ, self.clean_env, clear=True):
            for bad in ("http://127.0.0.1:8787/api/v1", "https://evil.example.com/api/v1",
                        "http://apiin.nmo.net.cn/api/v1"):
                with self.subTest(bad=bad):
                    self.env_file.write_text(f"MC_USER=t\nMC_PASS=p\nMC_OFFICIAL_API_BASE={bad}\n", encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.soak_ctl.official_mc_base(self.repo)
                    errs = self.soak_ctl.check_server_target(self.repo, "official", use_llm=False,
                                                             account_reserved=True)
                    self.assertEqual(len(errs), 1)
                    self.assertIn("MC_OFFICIAL_API_BASE", errs[0])

    def test_host_lists_stay_in_sync_with_preflight(self):
        from mcagent.preflight import OFFICIAL_HOSTS

        self.assertEqual(set(self.soak_ctl.OFFICIAL_MC_HOSTS), set(OFFICIAL_HOSTS))


if __name__ == "__main__":
    unittest.main()
