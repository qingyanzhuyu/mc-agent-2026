"""planner.llm 的离线单测：不联网、不调真模型。

重点验证三件事：
1. 非法决策会被 `validate()` 拦住，并把错误**回喂**给模型重问；
2. 重问仍失败时返回 None（交给上层规则兜底），绝不把非法决策放行；
3. 快/慢两条路径的代理选择不同（快走代理、慢直连）。
"""
from __future__ import annotations

import json
import unittest

from mcagent.planner import decision as dec
from mcagent.planner.llm import LLMClient, LLMError, extract_json


class Recorder:
    """假的传输层：按顺序返回预置回复，并记录每次请求。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, headers, data, timeout, proxy):
        self.calls.append({"url": url, "body": json.loads(data), "proxy": proxy,
                           "headers": dict(headers)})
        if not self.replies:
            raise AssertionError("回复用完了，说明重试次数超出预期")
        return self.replies.pop(0)


def ok_reply(content: str) -> tuple[int, str]:
    return 200, json.dumps({
        "model": "qwen3.6-35b-a3b",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }, ensure_ascii=False)


def empty_reply() -> tuple[int, str]:
    return 200, json.dumps({
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
        "usage": {"completion_tokens": 900},
    })


GOOD = json.dumps({"goal": "acq:log", "action": "dig", "args": {"pos": [1, 64, 2]},
                   "why": "附近有原木", "expect": {"item": "oak_log"}, "abort_if": []})
BAD_PARAM = json.dumps({"goal": "kill_zombie", "action": "attack", "args": {"entity": "zombie"},
                        "why": "打僵尸"})


class DecideTests(unittest.TestCase):
    def client(self, replies):
        rec = Recorder(replies)
        c = LLMClient(key="test-key", fast_proxy="http://127.0.0.1:7897", poster=rec, timeout=5)
        return c, rec

    def test_retries_after_validation_error_and_feedback_reaches_model(self):
        c, rec = self.client([ok_reply(BAD_PARAM), ok_reply(GOOD)])
        d, errs, attempts = c.decide("pos=[0,64,0] hp=20")
        self.assertIsNotNone(d)
        self.assertEqual(d.action, "dig")
        self.assertEqual(attempts, 2)
        self.assertEqual(errs, [])
        # 第二次请求里必须带上第一次的错误说明
        second = json.dumps(rec.calls[1]["body"], ensure_ascii=False)
        self.assertIn("entity_id", second)

    def test_gives_up_and_returns_none_when_still_invalid(self):
        c, rec = self.client([ok_reply(BAD_PARAM), ok_reply(BAD_PARAM)])
        d, errs, attempts = c.decide("pos=[0,64,0]")
        self.assertIsNone(d)
        self.assertEqual(attempts, 2)
        self.assertTrue(errs)
        self.assertTrue(any("entity_id" in e for e in errs))

    def test_empty_content_is_retried_not_accepted(self):
        import mcagent.planner.llm as llm
        old = llm.time.sleep
        llm.time.sleep = lambda *_: None
        try:
            c, rec = self.client([empty_reply(), ok_reply(GOOD)])
            d, errs, attempts = c.decide("pos=[0,64,0]")
        finally:
            llm.time.sleep = old
        self.assertIsNotNone(d)
        self.assertEqual(attempts, 2)

    def test_http_error_becomes_llm_error(self):
        c, _ = self.client([(403, '{"error":{"message":"no access"}}')])
        with self.assertRaises(LLMError):
            c.chat([{"role": "user", "content": "hi"}])

    def test_unparseable_reply_is_reported(self):
        c, _ = self.client([ok_reply("我想想……先砍树吧"), ok_reply(GOOD)])
        d, errs, attempts = c.decide("pos=[0,64,0]")
        self.assertIsNotNone(d)
        self.assertEqual(attempts, 2)


class RoutingTests(unittest.TestCase):
    def test_fast_path_uses_proxy_slow_path_goes_direct(self):
        rec = Recorder([ok_reply(GOOD), ok_reply(GOOD)])
        c = LLMClient(key="k", fast_proxy="http://127.0.0.1:7897", poster=rec, timeout=5)
        c.decide("s", thinking=False)
        c.decide("s", thinking=True)
        self.assertEqual(rec.calls[0]["proxy"], "http://127.0.0.1:7897")
        self.assertIsNone(rec.calls[1]["proxy"])

    def test_thinking_off_sends_chat_template_kwargs(self):
        rec = Recorder([ok_reply(GOOD)])
        c = LLMClient(key="k", fast_proxy=None, poster=rec, timeout=5)
        c.chat([{"role": "user", "content": "hi"}], thinking=False)
        self.assertEqual(rec.calls[0]["body"]["chat_template_kwargs"], {"enable_thinking": False})

    def test_thinking_on_omits_the_kwargs(self):
        rec = Recorder([ok_reply(GOOD)])
        c = LLMClient(key="k", fast_proxy=None, poster=rec, timeout=5)
        c.chat([{"role": "user", "content": "hi"}], thinking=True)
        self.assertNotIn("chat_template_kwargs", rec.calls[0]["body"])


class ExtractJsonTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(extract_json('{"a":1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(extract_json('```json\n{"a": {"b": 2}}\n```'), {"a": {"b": 2}})

    def test_with_prose_around(self):
        self.assertEqual(extract_json('好的，决策如下：{"action":"dig"} 完成'), {"action": "dig"})

    def test_none_when_absent(self):
        self.assertIsNone(extract_json("没有 JSON"))


class ValidatorGuardTests(unittest.TestCase):
    """锁住校验器的关键行为，防止有人把它改松。"""

    def test_rejects_invented_param(self):
        ok, errs = dec.validate(json.loads(BAD_PARAM))
        self.assertFalse(ok)
        self.assertTrue(any("entity_id" in e for e in errs))

    def test_rejects_numeric_goal(self):
        ok, errs = dec.validate({"goal": 1, "action": "dig", "args": {"pos": [1, 2, 3]}})
        self.assertFalse(ok)

    def test_accepts_valid(self):
        ok, errs = dec.validate(json.loads(GOOD))
        self.assertTrue(ok, errs)


if __name__ == "__main__":
    unittest.main()
