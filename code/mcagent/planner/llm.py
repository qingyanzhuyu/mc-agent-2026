"""LLM 端点客户端（AIA 统一代理）。

**状态：未验证**（2026-11-12 实测过端点行为，但没在比赛环境跑完整决策）。

实测到、必须写进代码的三件事（别"优化"掉）：
1. 模型 id 写 `default`；群里给的字面 id `qwen3.6-35b-a3b` 在当前 key 上是 403。
2. 这是思考模型，`reasoning_content` 与 `content` 分开返回，**思考 token 计入 max_tokens**。
   给太小会拿到 `finish_reason=length` + `content=""` —— 静默失败，必须当失败处理。
3. 关思考的正确写法是 `chat_template_kwargs={"enable_thinking": False}`；
   顶层 `enable_thinking=False` **无效**（照样思考几百字）。
4. 网络：api.nju-aia.com 走代理 219ms / 直连 1520ms —— 这个端点**建议走代理**，
   和 MC API 相反（那个直连更快）。所以代理是 per-client 配置，不是全局开关。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

import requests

DEFAULT_BASE = "https://api.nju-aia.com/v1"
DEFAULT_MODEL = "default"


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(
        self,
        base: str | None = None,
        key: str | None = None,
        model: str | None = None,
        proxy: str | None = None,
        timeout: float = 90,
    ):
        self.base = (base or os.environ.get("LLM_BASE") or DEFAULT_BASE).rstrip("/")
        self.key = key or os.environ.get("LLM_KEY") or ""
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        # 注意默认值和 MC 客户端相反：这个端点走代理更快
        self.proxy = proxy if proxy is not None else (os.environ.get("LLM_PROXY") or None)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False  # 注册表里的死代理会污染一切
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}

    def chat(
        self,
        messages: list[dict],
        thinking: bool = False,
        max_tokens: int = 1500,
        retry_on_empty: bool = True,
    ) -> str:
        """返回 assistant 的 content 文本。空 content 视为失败并重试一次（思考吃光 token 的坑）。"""
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

        last_err = ""
        for attempt in (1, 2):
            t0 = time.time()
            try:
                r = self.session.post(
                    f"{self.base}/chat/completions",
                    headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                    data=json.dumps(body),
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                last_err = f"network: {type(e).__name__}: {e}"
                time.sleep(1.0)
                continue

            if r.status_code != 200:
                raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")

            payload = r.json()
            if payload.get("error"):
                raise LLMError(str(payload["error"])[:300])

            choice = (payload.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = (msg.get("content") or "").strip()
            usage = payload.get("usage") or {}
            dt = time.time() - t0

            if content:
                return content

            # 空回复：多半是思考把 token 吃光了
            last_err = (
                f"空 content（finish={choice.get('finish_reason')}，"
                f"completion={usage.get('completion_tokens')}，{dt:.1f}s）"
            )
            if not retry_on_empty or attempt == 2:
                break
            body["max_tokens"] = max(max_tokens * 3, 3000)  # 给思考留够
            thinking = True  # 第二次允许思考，但预算放大
            body.pop("chat_template_kwargs", None)

        raise LLMError(f"模型没有返回可用内容: {last_err}")


def extract_json(text: str) -> dict | None:
    """从模型回复里抠出第一个 JSON object（容忍 ```json 包裹和前后废话）。"""
    if not text:
        return None
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = fenced + [text]
    for c in candidates:
        start = c.find("{")
        while start != -1:
            depth = 0
            for i in range(start, len(c)):
                if c[i] == "{":
                    depth += 1
                elif c[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(c[start:i + 1])
                            if isinstance(obj, dict):
                                return obj
                        except json.JSONDecodeError:
                            pass
                        break
            start = c.find("{", start + 1)
    return None


SYSTEM_PROMPT = """你是 Minecraft 生存 Agent 的决策器。每一步只输出一个 JSON 对象，不要输出任何其他文字。

{
  "goal": "目标 id，如 acq:oak_log",
  "action": "必须是给定的动作名之一",
  "args": {},
  "why": "一句话原因",
  "expect": {"item": "oak_log", "count": 1},
  "abort_if": ["hp<10"]
}

硬性约束：
- 一次只做一个动作。动作会阻塞到你返回的结果发生为止。
- 不要发明动作名或参数名，只能从给你的清单里选。
- 坐标必须来自你看到的状态，不要凭记忆编。
- 生存优先：血量低、有敌对生物贴身、在岩浆里时，先处理危险。
"""


def decide(client: LLMClient, state_summary: str, allowed: list[str], extra: str = "", max_tokens: int = 800) -> dict | None:
    """一次决策调用。返回原始 dict（未校验），交给 planner.decision.validate 处理。"""
    user = f"""当前状态：
{state_summary}

可用动作：
{", ".join(allowed)}

{extra}

输出下一步的 JSON。"""
    text = client.chat(
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
        thinking=False,
        max_tokens=max_tokens,
    )
    return extract_json(text)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = LLMClient()
    print(f"base={c.base} model={c.model} proxy={c.proxy or '直连'}")
    s = "pos=[0,64,0] hp=20 food=20 背包空 附近有橡木"
    d = decide(c, s, ["move_to", "dig", "craft"])
    print("决策:", d)
