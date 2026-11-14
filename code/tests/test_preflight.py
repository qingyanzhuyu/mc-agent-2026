"""preflight 的离线单测：不联网、不读真实 .env。

锁住三件事：
1. 开发态配置（本机网关 + localdev + 压测 key + 并发 48）必须被拦下；
2. 比赛态配置必须全过；
3. 网关仍在监听时必须 BLOCK。
"""
from __future__ import annotations

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


def run(env, gateway=False):
    return pf.check_static(env, gateway_open=gateway)


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
