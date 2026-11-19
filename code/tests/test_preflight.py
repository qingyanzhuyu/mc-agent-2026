"""preflight 的离线单测：不联网、不读真实 .env。

锁住三件事：
1. 开发态配置（本机网关 + localdev + 压测 key + 并发 48）必须被拦下；
2. 比赛态配置必须全过；
3. 网关仍在监听时必须 BLOCK。
"""
from __future__ import annotations

import os
import unittest

from mcagent import preflight as pf

DEV = {
    "MC_API_BASE": "http://127.0.0.1:8787/api/v1",
    "MC_USER": "qyzy_dev",
    "MC_PASS": "localdev",
    "LLM_KEY": "sk-mRbe07wnXwL975GpfflElIbOPH69Q4YMRO5BERpSAGxTBddG",
    "LLM_MODEL": "default",
    "LLM_MAX_CONCURRENCY": "48",
}

MATCH = {
    "MC_API_BASE": "https://apiin.nmo.net.cn/api/v1",
    "MC_USER": "nmo_real_bot",
    "MC_PASS": "realpass",
    "LLM_KEY": "sk-team-xxxx",
    "LLM_MODEL": "default",
    "LLM_MAX_CONCURRENCY": "2",
}


def run(env, gateway=False, running_processes=()):
    return pf.check_static(env, gateway_open=gateway, running_processes=running_processes)


def failed_names(checks):
    return {c.name for c in checks if not c.ok}


class DevConfigTests(unittest.TestCase):
    def test_dev_config_is_blocked_everywhere(self):
        bad = failed_names(run(DEV))
        self.assertIn("端点不是本机", bad)
        self.assertIn("账号是正式角色名", bad)
        self.assertIn("密码不是 localdev", bad)
        self.assertIn("LLM key 不是公共压测 key", bad)
        self.assertIn("并发 ≤ 2（比赛期）", bad)

    def test_match_config_passes(self):
        checks = run(MATCH)
        bad = failed_names(checks)
        self.assertEqual(bad, set(), f"比赛配置不应有失败项，实际: {bad}")
        self.assertTrue(all(c.ok for c in checks))


class GuardTests(unittest.TestCase):
    def test_gateway_still_listening_blocks(self):
        bad = failed_names(run(MATCH, gateway=True))
        self.assertIn("本地联调网关已停", bad)

    def test_missing_concurrency_fails(self):
        env = dict(MATCH)
        env.pop("LLM_MAX_CONCURRENCY")
        self.assertIn("并发 ≤ 2（比赛期）", failed_names(run(env)))

    def test_concurrency_three_fails(self):
        env = dict(MATCH, LLM_MAX_CONCURRENCY="3")
        self.assertIn("并发 ≤ 2（比赛期）", failed_names(run(env)))

    def test_wrong_model_id_fails(self):
        env = dict(MATCH, LLM_MODEL="qwen3.6-35b-a3b")
        self.assertIn("LLM_MODEL 是 default", failed_names(run(env)))

    def test_custom_llm_profile_blocks(self):
        env = dict(MATCH, LLM_PROFILE="custom")
        c = next(x for x in run(env) if x.name == "LLM_PROFILE 是 official")
        self.assertFalse(c.ok)
        self.assertEqual(c.level, pf.BLOCK)

    def test_train_on_official_flag_blocks_match(self):
        env = dict(MATCH, MCAGENT_TRAIN_ON_OFFICIAL="1")
        self.assertIn("没开「比赛服训练」开关", failed_names(run(env)))
        self.assertNotIn("没开「比赛服训练」开关", failed_names(run(dict(MATCH, MCAGENT_TRAIN_ON_OFFICIAL="0"))))

    def test_explicit_official_profile_passes(self):
        env = dict(MATCH, LLM_PROFILE="Official")
        self.assertNotIn("LLM_PROFILE 是 official", failed_names(run(env)))

    def test_unknown_host_is_warn_not_block(self):
        env = dict(MATCH, MC_API_BASE="https://example.com/api/v1")
        checks = run(env)
        c = next(x for x in checks if x.name == "端点是官方入口")
        self.assertFalse(c.ok)
        self.assertEqual(c.level, pf.WARN)

    def test_local_host_is_block(self):
        env = dict(MATCH, MC_API_BASE="http://localhost:8787/api/v1")
        c = next(x for x in run(env) if x.name == "端点不是本机")
        self.assertFalse(c.ok)
        self.assertEqual(c.level, pf.BLOCK)


class ResidualProcessTests(unittest.TestCase):
    def _proc_check(self, checks):
        return next(x for x in checks if x.name == "无残留 soak / mcagent 进程")

    def test_detects_soak_ctl_and_mcagent_processes_as_block(self):
        for pid, cmd in [
            (21001, "python3 scripts/soak_ctl.py run --session-dir /tmp/s"),
            (21002, "python -m soak_ctl --session-dir /tmp/s"),
            (21003, "/opt/venv/bin/python -m mcagent.loop --soak"),
            (21004, "python -m mcagent.soak --hours 8"),
            (21005, "python -m mcagent.probe --live"),
            (21006, "python code/mcagent/loop/__main__.py"),
        ]:
            with self.subTest(cmd=cmd):
                c = self._proc_check(run(MATCH, running_processes=[(pid, cmd)]))
                self.assertFalse(c.ok)
                self.assertEqual(c.level, pf.BLOCK)
                self.assertIn(str(pid), c.detail)
                self.assertTrue(
                    "kill " in c.detail or "pkill" in c.detail,
                    f"detail 应含清理命令提示，实际: {c.detail}",
                )

    def test_ignores_self_parent_preflight_test_runners_and_log_tools(self):
        procs = [
            (os.getpid(), "python -m mcagent.loop"),
            (os.getppid(), "python scripts/soak_ctl.py"),
            (31001, "python -m mcagent.preflight --env-file /tmp/.env"),
            (31002, "python code/mcagent/preflight.py"),
            (31003, "python -m unittest tests.test_preflight -v"),
            (31004, "pytest tests/test_preflight.py"),
            (31005, "grep -rn soak_ctl code/"),
            (31006, "rg mcagent.loop code/"),
            (31007, "pgrep -af mcagent"),
            (31008, "pkill -f soak_ctl|mcagent.loop"),
            (31009, "tail -f code/mcagent/logs/soak.log"),
            (31010, "less code/mcagent/logs/soak.log"),
            (31011, "cat code/mcagent/logs/soak.log"),
            (31012, "/home/user/mcagent/.venv/bin/python -m http.server 8000"),
        ]
        c = self._proc_check(run(MATCH, running_processes=procs))
        self.assertTrue(c.ok, f"不应误报自身/单测/看日志命令，实际 detail: {c.detail}")
        self.assertEqual(c.detail, "未发现残留 soak_ctl / mcagent 后台进程")

    def test_no_residual_processes_passes(self):
        c = self._proc_check(run(MATCH, running_processes=[(9999, "bash"), (10000, "python -m http.server")]))
        self.assertTrue(c.ok)

    def test_process_lister_injection_path(self):
        called = []

        def fake_lister():
            called.append(True)
            return [(42424, "python -m mcagent.loop")]

        checks = pf.check_static(MATCH, gateway_open=False, process_lister=fake_lister)
        c = self._proc_check(checks)
        self.assertTrue(called)
        self.assertFalse(c.ok)
        self.assertEqual(c.level, pf.BLOCK)
        self.assertIn("42424", c.detail)


class CampusProxyTests(unittest.TestCase):
    def _proxy_check(self, checks):
        return next(x for x in checks if x.name == "校园网直连未挂 7897 代理")

    def test_campus_host_with_7897_proxy_blocks(self):
        for proxy in (
            "http://127.0.0.1:7897",
            "socks5://localhost:7897",
            "127.0.0.1:7897",
            "https://127.0.0.1:7897/",
            "[::1]:7897",
            "7897",
        ):
            with self.subTest(proxy=proxy):
                env = dict(MATCH, MC_PROXY=proxy)
                c = self._proxy_check(run(env))
                self.assertFalse(c.ok)
                self.assertEqual(c.level, pf.BLOCK)
                self.assertIn("MC_PROXY", c.detail)
                self.assertIn("apiin.nmo.net.cn", c.detail)

    def test_campus_host_with_empty_proxy_passes(self):
        for proxy in ("", "   "):
            with self.subTest(proxy=repr(proxy)):
                env = dict(MATCH, MC_PROXY=proxy)
                c = self._proxy_check(run(env))
                self.assertTrue(c.ok)
                self.assertEqual(c.level, pf.BLOCK)

    def test_campus_host_with_non_7897_proxy_warns(self):
        env = dict(MATCH, MC_PROXY="http://127.0.0.1:8080")
        c = self._proxy_check(run(env))
        self.assertFalse(c.ok)
        self.assertEqual(c.level, pf.WARN)
        self.assertIn("MC_PROXY", c.detail)

    def test_public_official_host_with_7897_proxy_is_not_blocked(self):
        env = dict(MATCH, MC_API_BASE="https://mc-api.windywindie.top/api/v1", MC_PROXY="http://127.0.0.1:7897")
        checks = run(env)
        c = self._proxy_check(checks)
        self.assertTrue(c.ok)
        self.assertTrue(all(x.ok for x in checks))

    def test_proxy_targets_port_handles_invalid_formats_safely(self):
        for bad in ("", "not-a-proxy", "http://127.0.0.1:badport", "http://[::1", "http://127.0.0.1:78970"):
            with self.subTest(bad=bad):
                self.assertFalse(pf._proxy_targets_port(bad, 7897))


class LiveCheckTests(unittest.TestCase):
    def test_fast_401_passes(self):
        checks = pf.check_live(MATCH, getter=lambda url, t: (401, 5.0))
        self.assertTrue(all(c.ok for c in checks))

    def test_slow_endpoint_warns_like_public(self):
        checks = pf.check_live(MATCH, getter=lambda url, t: (401, 1305.0))
        c = next(x for x in checks if x.name == "延迟像校内（<100ms）")
        self.assertFalse(c.ok)
        self.assertEqual(c.level, pf.WARN)

    def test_non_401_blocks(self):
        checks = pf.check_live(MATCH, getter=lambda url, t: (200, 5.0))
        self.assertIn("未授权返回 401", failed_names(checks))

    def test_connection_error_reports(self):
        def boom(url, t):
            raise OSError("no route")

        checks = pf.check_live(MATCH, getter=boom)
        self.assertIn("端点可达（延迟）", failed_names(checks))


class LoadEnvTests(unittest.TestCase):
    def test_parse_skips_comments_and_quotes(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text('# c\nA=1\nB="x y"\n\nC=\'z\'\n', encoding="utf-8")
            env = pf.load_env(str(p))
        self.assertEqual(env, {"A": "1", "B": "x y", "C": "z"})


if __name__ == "__main__":
    unittest.main()
