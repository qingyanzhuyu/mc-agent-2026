"""LLM 端点客户端（AIA 统一代理）。

实测结论（2026-11-13，写进代码别"优化"掉）：

1. **只有 `model="default"` 能调。** 后端实际是 `qwen3.6-35b-a3b`（响应 `model` 字段会如实回报），
   但字面写 `qwen3.6-35b-a3b` 或 `deepseek-v4.1-flash` 都是 403 `This token has no access to model`。
2. **关思考**：`chat_template_kwargs={"enable_thinking": False}` → 356 ms（走代理）。
   顶层 `enable_thinking=False` 无效。
3. **开思考**：默认即可，3.2 s；但**长请求走代理会被掐断**（ProxyError）。
   所以两条路径用不同的连接方式：快路径走代理，慢路径直连。
4. **模型 JSON 约束不强**：实测 3 个状态里 2 个输出不合法（`goal` 给了数字、`attack` 用了不存在的
   `entity` 参数）。所以决策必须过 `validate()`，失败要带错误回去重问。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Optional

import requests

from .decision import ALLOWED_ACTIONS, Decision, from_dict, validate

DEFAULT_BASE = "https://api.nju-aia.com/v1"
DEFAULT_MODEL = "default"
DEFAULT_FAST_PROXY = "http://127.0.0.1:7897"

#: poster(url, headers, data, timeout, proxy) -> (status_code, body_text)
Poster = Callable[[str, dict, str, float, Optional[str]], "tuple[int, str]"]


class LLMError(Exception):
    pass


SYSTEM_PROMPT = """你是 Minecraft 生存 Agent 的决策器。每次只输出一个 JSON 对象，不要输出任何其他文字、不要用 Markdown 代码块。

字段必须严格照抄下面这六个名字，一个都不能改、不能少：
{"goal": "...", "action": "...", "args": {}, "why": "...", "expect": {}, "abort_if": []}

类型要求（错一个就会被拒绝）：
- goal：字符串，不能是数字
- action：必须是给定清单里的名字，不能自创
- args：对象，键必须是该动作真实存在的参数名（例如 entity_id，不是 entity）
- why：字符串
- expect：对象或 null
- abort_if：数组

行为要求：
- 一次只做一个动作；动作会阻塞到结果发生为止。
- 坐标必须来自你看到的状态，不要凭记忆编造。
- 生存优先：血量低、敌对生物贴身、在岩浆或虚空中时先处理危险。"""


def extract_json(text: str) -> Optional[dict]:
    """从回复里抠出第一个 JSON 对象（容忍 ```json 包裹、前后废话）。"""
    if not text:
        return None
    for block in re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) + [text]:
        start = block.find("{")
        while start != -1:
            depth = 0
            for i in range(start, len(block)):
                ch = block[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(block[start:i + 1])
                            if isinstance(obj, dict):
                                return obj
                        except json.JSONDecodeError:
                            pass
                        break
            start = block.find("{", start + 1)
    return None


class LLMClient:
    def __init__(
        self,
        base: Optional[str] = None,
        key: Optional[str] = None,
        model: Optional[str] = None,
        fast_proxy: Optional[str] = None,
        timeout: float = 90,
        poster: Optional[Poster] = None,
    ):
        self.base = (base or os.environ.get("LLM_BASE") or DEFAULT_BASE).rstrip("/")
        self.key = key or os.environ.get("LLM_KEY") or ""
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        # 快路径的代理；慢路径强制直连（长请求走代理会被掐）
        env_proxy = os.environ.get("LLM_PROXY_FAST")
        self.fast_proxy = fast_proxy if fast_proxy is not None else (
            env_proxy if env_proxy is not None else DEFAULT_FAST_PROXY)
        self.timeout = timeout
        self._poster = poster or self._requests_poster
        self._sessions: dict[Optional[str], requests.Session] = {}

    # ---- 传输层（可注入，便于离线单测） ------------------------------
    def _session(self, proxy: Optional[str]) -> requests.Session:
        if proxy not in self._sessions:
            s = requests.Session()
            s.trust_env = False  # 注册表里的死代理会污染一切
            if proxy:
                s.proxies = {"http": proxy, "https": proxy}
            self._sessions[proxy] = s
        return self._sessions[proxy]

    def _requests_poster(self, url: str, headers: dict, data: str, timeout: float,
                         proxy: Optional[str]) -> tuple[int, str]:
        try:
            r = self._session(proxy).post(url, headers=headers, data=data, timeout=timeout)
        except requests.RequestException as e:
            raise LLMError(f"net(proxy={proxy}): {type(e).__name__}: {e}") from e
        return r.status_code, r.text

    def proxy_for(self, thinking: bool) -> Optional[str]:
        """快路径走代理，慢路径直连——这是实测出来的，不是随手写的。"""
        return None if thinking else self.fast_proxy

    # ---- 单次调用 ----------------------------------------------------
    def chat(
        self,
        messages: list[dict],
        thinking: bool = False,
        max_tokens: int = 900,
        timeout: Optional[float] = None,
    ) -> str:
        if not self.key:
            raise LLMError("没有 LLM_KEY（见 .env.example）")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if not thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}

        url = f"{self.base}/chat/completions"
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        request_timeout = timeout
        if request_timeout is None:
            request_timeout = self.timeout if thinking else float(os.environ.get("MC_MODEL_TIMEOUT_S", "8"))
        status, text = self._poster(url, headers, json.dumps(body), request_timeout, self.proxy_for(thinking))
        if status != 200:
            raise LLMError(f"HTTP {status}: {text[:200]}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"回复不是 JSON: {text[:160]}") from e
        if payload.get("error"):
            raise LLMError(str(payload["error"])[:300])

        choice = (payload.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        if content:
            return content

        # 空回复：多半是思考把 token 吃光了
        usage = payload.get("usage") or {}
        raise LLMError(
            f"空 content（finish={choice.get('finish_reason')}, "
            f"completion={usage.get('completion_tokens')}）")

    # ---- 决策：带校验回喂的重问循环 ----------------------------------
    def decide(
        self,
        state_summary: str,
        allowed: Optional[list[str]] = None,
        extra: str = "",
        attempts: int = 2,
        thinking: bool = False,
    ) -> tuple[Optional[Decision], list[str], int]:
        """返回 (决策或 None, 最后一次的错误列表, 实际调用次数)。

        失败处理：把 `validate()` 的错误原文回喂给模型重问一次；仍不合法就返回 None，
        由上层走规则兜底——**绝不让非法决策进入执行器**。
        """
        allowed = list(allowed or ALLOWED_ACTIONS)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        errors: list[str] = []
        for attempt in range(1, attempts + 1):
            user = f"当前状态：\n{state_summary}\n\n可用动作：\n{', '.join(allowed)}\n"
            if extra:
                user += f"\n补充：\n{extra}\n"
            if errors:
                user += ("\n上次输出被校验拒绝，错误如下，请修正后重新输出完整 JSON：\n- "
                         + "\n- ".join(errors[:8]) + "\n")
            user += "\n输出下一步的 JSON。"
            messages.append({"role": "user", "content": user})

            try:
                text = self.chat(messages, thinking=thinking)
            except LLMError as e:
                errors = [f"调用失败: {e}"]
                time.sleep(min(2.0, 0.5 * attempt))
                continue

            obj = extract_json(text)
            if obj is None:
                errors = ["回复里找不到合法 JSON 对象"]
                messages.append({"role": "assistant", "content": text[:800]})
                continue

            ok, errs = validate(obj)
            if ok:
                return from_dict(obj), [], attempt
            errors = errs
            messages.append({"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)})

        return None, errors, attempts
