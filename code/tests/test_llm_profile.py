"""LLM 双配置（official / custom）的离线单测（verified: offline）。

锁住四件事：
1. 不设 LLM_PROFILE 时行为与改动前一致（官方端点、default、7897 快路径代理）；
2. custom 只读 LLM_CUSTOM_*，**不会**借用官方的 key，缺项在调用时报 config 错误；
3. custom 端点 400 拒收关思考字段时去掉重发一次；官方端点回 400 不做这件事；
4. MC 指向官方端点时，loop 拒绝用 custom 启动。

全部用注入的假传输层，不发网络请求、不读真实 .env。
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from mcagent.planner.llm import DEFAULT_BASE, DEFAULT_FAST_PROXY, LLMClient, LLMError, resolve_profile

MSG = [{"role": "user", "content": "hi"}]


def ok_body(content: str = '{"a": 1}') -> tuple[int, str]:
    return 200, json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})


class Recorder:
    """假传输层：记录每次请求；`reject_field=True` 时带 chat_template_kwargs 的请求回 400。"""

    def __init__(self, reject_field: bool = False, status: int = 200):
        self.reject_field = reject_field
        self.status = status
        self.calls: list[dict] = []

    def __call__(self, url, headers, data, timeout, proxy):
        body = json.loads(data)
        self.calls.append({"url": url, "auth": headers.get("Authorization"), "body": body, "proxy": proxy})
        if self.reject_field and "chat_template_kwargs" in body:
            return 400, '{"error": {"message": "Unrecognized request argument: chat_template_kwargs"}}'
        if self.status != 200:
            return self.status, '{"error": "bad request"}'
        return ok_body()


CUSTOM_ENV = {
    "LLM_PROFILE": "custom",
    "LLM_CUSTOM_BASE": "https://example-llm.test/v1/",
    "LLM_CUSTOM_KEY": "sk-own-test",
    "LLM_CUSTOM_MODEL": "some-model",
}


class ResolveProfileTests(unittest.TestCase):
    def test_default_is_official_with_old_defaults(self):
        cfg = resolve_profile(env={})
        self.assertEqual(cfg["profile"], "official")
        self.assertEqual(cfg["base"], DEFAULT_BASE)
        self.assertEqual(cfg["model"], "default")
        self.assertEqual(cfg["proxy"], DEFAULT_FAST_PROXY)
        self.assertEqual(cfg["key"], "")

    def test_official_empty_proxy_means_direct(self):
        self.assertEqual(resolve_profile(env={"LLM_PROXY_FAST": ""})["proxy"], "")

    def test_custom_reads_only_custom_vars(self):
        env = dict(CUSTOM_ENV, LLM_KEY="sk-official", LLM_BASE="https://api.nju-aia.com/v1", LLM_MODEL="default")
        cfg = resolve_profile(env=env)
        self.assertEqual(cfg["profile"], "custom")
        self.assertEqual(cfg["key"], "sk-own-test")
        self.assertEqual(cfg["model"], "some-model")
        self.assertEqual(cfg["proxy"], "")

    def test_custom_never_borrows_official_values(self):
        cfg = resolve_profile(env={"LLM_PROFILE": "custom", "LLM_KEY": "sk-official"})
        self.assertEqual((cfg["base"], cfg["key"], cfg["model"]), ("", "", ""))

    def test_profile_is_case_and_space_insensitive(self):
        self.assertEqual(resolve_profile(env={"LLM_PROFILE": " Custom "})["profile"], "custom")

    def test_typo_raises(self):
        with self.assertRaises(ValueError):
            resolve_profile(env={"LLM_PROFILE": "offical"})


class ClientProfileTests(unittest.TestCase):
    def client(self, env: dict, **kw) -> LLMClient:
        with patch.dict(os.environ, env, clear=True):
            return LLMClient(log=None, **kw)

    def test_custom_routes_to_custom_endpoint_direct(self):
        wire = Recorder()
        c = self.client(CUSTOM_ENV, poster=wire)
        self.assertEqual(c.chat(MSG, timeout=5), '{"a": 1}')
        call = wire.calls[0]
        self.assertEqual(call["url"], "https://example-llm.test/v1/chat/completions")
        self.assertEqual(call["auth"], "Bearer sk-own-test")
        self.assertEqual(call["body"]["model"], "some-model")
        self.assertIsNone(call["proxy"])
        self.assertEqual(c.health()["profile"], "custom")

    def test_custom_missing_fields_is_config_error_without_request(self):
        wire = Recorder()
        c = self.client({"LLM_PROFILE": "custom", "LLM_KEY": "sk-official"}, poster=wire)
        with self.assertRaises(LLMError) as cm:
            c.chat(MSG, timeout=5)
        self.assertEqual(cm.exception.kind, "config")
        for name in ("LLM_CUSTOM_BASE", "LLM_CUSTOM_KEY", "LLM_CUSTOM_MODEL"):
            self.assertIn(name, str(cm.exception))
        self.assertEqual(wire.calls, [])

    def test_official_missing_key_names_official_var(self):
        c = self.client({}, poster=Recorder())
        with self.assertRaises(LLMError) as cm:
            c.chat(MSG, timeout=5)
        self.assertIn("LLM_KEY", str(cm.exception))
        self.assertNotIn("CUSTOM", str(cm.exception))

    def test_explicit_args_override_profile(self):
        wire = Recorder()
        c = self.client(CUSTOM_ENV, poster=wire, base="https://other.test/v1", key="k2", model="m2")
        c.chat(MSG, timeout=5)
        self.assertEqual(wire.calls[0]["url"], "https://other.test/v1/chat/completions")
        self.assertEqual(wire.calls[0]["auth"], "Bearer k2")
        self.assertEqual(wire.calls[0]["body"]["model"], "m2")

    def test_profile_argument_overrides_env(self):
        c = self.client(CUSTOM_ENV, profile="official", key="sk-team")
        self.assertEqual(c.profile, "official")
        self.assertEqual(c.base, DEFAULT_BASE)


class ThinkingFieldTests(unittest.TestCase):
    def test_custom_400_drops_field_and_retries_once(self):
        wire = Recorder(reject_field=True)
        with patch.dict(os.environ, CUSTOM_ENV, clear=True):
            c = LLMClient(log=None, poster=wire)
        self.assertEqual(c.chat(MSG, timeout=5), '{"a": 1}')
        self.assertEqual(len(wire.calls), 2)
        self.assertIn("chat_template_kwargs", wire.calls[0]["body"])
        self.assertNotIn("chat_template_kwargs", wire.calls[1]["body"])
        self.assertTrue(c.drop_thinking_field)
        self.assertEqual(c.stats["thinking_field_dropped"], 1)

        # 之后的请求一开始就不带，不再多打一次 400
        c.chat(MSG, timeout=5)
        self.assertEqual(len(wire.calls), 3)
        self.assertNotIn("chat_template_kwargs", wire.calls[2]["body"])

    def test_custom_other_400_still_raises(self):
        wire = Recorder(status=400)
        with patch.dict(os.environ, CUSTOM_ENV, clear=True):
            c = LLMClient(log=None, poster=wire)
        with self.assertRaises(LLMError) as cm:
            c.chat(MSG, timeout=5)
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(len(wire.calls), 2)  # 去字段重发一次仍是 400，就如实报错

    def test_official_400_keeps_field_and_does_not_retry(self):
        wire = Recorder(reject_field=True)
        with patch.dict(os.environ, {"LLM_KEY": "sk-team", "LLM_PROXY_FAST": ""}, clear=True):
            c = LLMClient(log=None, poster=wire)
        with self.assertRaises(LLMError) as cm:
            c.chat(MSG, timeout=5)
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(len(wire.calls), 1)
        self.assertFalse(c.drop_thinking_field)

    def test_thinking_mode_never_sends_field(self):
        wire = Recorder(reject_field=True)
        with patch.dict(os.environ, CUSTOM_ENV, clear=True):
            c = LLMClient(log=None, poster=wire)
        c.chat(MSG, thinking=True, timeout=5)
        self.assertEqual(len(wire.calls), 1)
        self.assertFalse(c.drop_thinking_field)

    def test_custom_deepseek_uses_thinking_disabled_and_drops_on_400(self):
        ds_env = dict(CUSTOM_ENV, LLM_CUSTOM_MODEL="DeepSeek-V4.1-Flash")
        calls: list[dict] = []

        def reject_thinking_once(url, headers, data, timeout, proxy):
            body = json.loads(data)
            calls.append(body)
            if "thinking" in body:
                return 400, '{"error": {"message": "Unrecognized request argument: thinking"}}'
            return ok_body()

        wire_ok = Recorder()
        with patch.dict(os.environ, ds_env, clear=True):
            c_ok = LLMClient(log=None, poster=wire_ok)
        self.assertEqual(c_ok.chat(MSG, timeout=5), '{"a": 1}')
        self.assertEqual(wire_ok.calls[0]["body"].get("thinking"), {"type": "disabled"})
        self.assertNotIn("chat_template_kwargs", wire_ok.calls[0]["body"])

        # 若某第三方兼容网关拒收 thinking 回 400，也能自动剥离重发且后续不再带
        with patch.dict(os.environ, ds_env, clear=True):
            c_drop = LLMClient(log=None, poster=reject_thinking_once)
        self.assertEqual(c_drop.chat(MSG, timeout=5), '{"a": 1}')
        self.assertEqual(len(calls), 2)
        self.assertIn("thinking", calls[0])
        self.assertNotIn("thinking", calls[1])
        self.assertTrue(c_drop.drop_thinking_field)


class LoopGuardTests(unittest.TestCase):
    """MC 指向官方端点 + custom 模型：loop 在登录之前就拒绝（返回 2，不发任何请求）。"""

    def run_main(self, env: dict, argv: list[str]) -> tuple[int, str]:
        from mcagent.loop import __main__ as loop_main

        err = io.StringIO()
        with patch.dict(os.environ, env, clear=True), redirect_stdout(io.StringIO()), redirect_stderr(err), \
                patch.object(loop_main.Config, "load", classmethod(lambda cls, env_file=None: _cfg_from_env())):
            code = loop_main.main(argv)
        return code, err.getvalue()

    def test_official_mc_with_custom_llm_refused(self):
        env = dict(CUSTOM_ENV, MC_API_BASE="https://mc-api.windywindie.top/api/v1", MC_USER="bot", MC_PASS="p")
        code, err = self.run_main(env, ["--llm", "--live"])
        self.assertEqual(code, 2)
        self.assertIn("LLM_PROFILE=official", err)

    def test_train_on_official_needs_flag_and_soak(self):
        base = dict(CUSTOM_ENV, MC_API_BASE="https://mc-api.windywindie.top/api/v1", MC_USER="bot", MC_PASS="p")
        # 只设开关、不加 --soak：仍拒绝
        code, err = self.run_main(dict(base, MCAGENT_TRAIN_ON_OFFICIAL="1"), ["--llm", "--live"])
        self.assertEqual(code, 2)
        # 加 --soak 但没设开关：仍拒绝
        code, err = self.run_main(base, ["--llm", "--live", "--soak"])
        self.assertEqual(code, 2)
        self.assertIn("MCAGENT_TRAIN_ON_OFFICIAL", err)

        # 开关 + --soak：放行过模型闸门（在真正登录前截停，不发任何请求）
        from mcagent.loop import __main__ as loop_main

        class Stop(Exception):
            pass

        err = io.StringIO()
        with tempfile.TemporaryDirectory() as td, \
                patch.dict(os.environ, dict(base, MCAGENT_TRAIN_ON_OFFICIAL="1"), clear=True), \
                redirect_stdout(io.StringIO()), redirect_stderr(err), \
                patch.object(loop_main.Config, "load", classmethod(lambda cls, env_file=None: _cfg_from_env())), \
                patch.object(loop_main, "TapeClient", side_effect=Stop), patch.object(loop_main, "Client", side_effect=Stop):
            with self.assertRaises(Stop):
                loop_main.main(["--llm", "--live", "--soak", "--run-dir", td])
        self.assertIn("比赛服训练模式", err.getvalue())

    def test_campus_mc_with_custom_llm_refused(self):
        env = dict(CUSTOM_ENV, MC_API_BASE="https://apiin.nmo.net.cn/api/v1", MC_USER="bot", MC_PASS="p")
        code, err = self.run_main(env, ["--llm", "--local-target"])
        self.assertEqual(code, 2)
        self.assertIn("LLM_PROFILE=official", err)

    def test_bad_profile_refused(self):
        env = {"LLM_PROFILE": "offical", "MC_API_BASE": "http://127.0.0.1:8787/api/v1",
               "MC_USER": "bot", "MC_PASS": "p"}
        code, err = self.run_main(env, ["--llm", "--local-target"])
        self.assertEqual(code, 2)
        self.assertIn("LLM_PROFILE", err)


def _cfg_from_env():
    """绕过真实 .env：只按当前（已打补丁的）环境变量构造 Config。"""
    from mcagent.client import Config

    return Config(
        base=os.environ.get("MC_API_BASE", ""),
        username=os.environ.get("MC_USER", ""),
        password=os.environ.get("MC_PASS", ""),
        proxy=None,
        token_file=None,
    )


if __name__ == "__main__":
    unittest.main()
