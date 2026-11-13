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

降级（2026-11-14 补，机制有离线单测，阈值是工程默认值、未经真机标定）：

5. **代理坏了就直连**：快路径经本机代理连不上/被网关拒（502/503/504）时，同一次调用里用剩余时间
   改直连重试；代理连续坏 `LLM_BREAKER_FAILS` 次后熔断 `LLM_BREAKER_COOLDOWN_S` 秒，期间直接走直连。
   "直连"仍是同一个官方端点、同一把 key，只是不经本机代理——**没有也不允许有第二个模型端点**（AGENTS §2B.3）。
6. **端点坏了就别等**：所有线路都失败连续达到同一阈值后整体熔断，期间 `chat()` 立刻抛
   `LLMError(kind="circuit_open")`，`ask_json()` 不重试不睡眠直接返回 None，调用方走规则兜底（J8）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable, Optional

import requests

from .decision import ALLOWED_ACTIONS, Decision, from_dict, validate
from .llm_fault import MIN_FAIR_TIMEOUT_S, MIN_FALLBACK_BUDGET_S, Breaker, LLMError

DEFAULT_BASE = "https://api.nju-aia.com/v1"
DEFAULT_MODEL = "default"
DEFAULT_FAST_PROXY = "http://127.0.0.1:7897"

#: poster(url, headers, data, timeout, proxy) -> (status_code, body_text)
Poster = Callable[[str, dict, str, float, Optional[str]], "tuple[int, str]"]


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
        *,
        fallback_direct: Optional[bool] = None,
        breaker_fails: Optional[int] = None,
        breaker_cooldown_s: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
        log: Optional[Callable[[str], None]] = print,
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

        # ---- 降级：代理线路熔断 + 端点整体熔断（J13：阈值走配置，不写死在调用点）
        env = os.environ.get
        self.fallback_direct = (env("LLM_FALLBACK_DIRECT", "1") != "0") if fallback_direct is None else fallback_direct
        fails = int(env("LLM_BREAKER_FAILS", "3")) if breaker_fails is None else breaker_fails
        cooldown = float(env("LLM_BREAKER_COOLDOWN_S", "60")) if breaker_cooldown_s is None else breaker_cooldown_s
        self._clock = clock
        self._log = log or (lambda _msg: None)
        self._lock = threading.Lock()   # 预取线程与主线程可能同时调用
        self._proxy_breaker = Breaker(fails, cooldown, clock)
        self._endpoint_breaker = Breaker(fails, cooldown, clock)
        #: 最近一次成功调用走的线路："proxy" / "direct" / None
        self.last_route: Optional[str] = None
        self.stats: dict[str, int] = {
            "proxy_ok": 0, "direct_ok": 0, "fallback_direct": 0,
            "proxy_opened": 0, "endpoint_opened": 0, "short_circuited": 0,
        }

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
        except requests.Timeout as e:
            raise LLMError(f"timeout(proxy={proxy}, {timeout:.1f}s): {type(e).__name__}", kind="timeout") from e
        except requests.RequestException as e:
            raise LLMError(f"net(proxy={proxy}): {type(e).__name__}: {e}", kind="net") from e
        return r.status_code, r.text

    def proxy_for(self, thinking: bool) -> Optional[str]:
        """首选线路：快路径走代理，慢路径直连——这是实测出来的，不是随手写的。"""
        return None if thinking else self.fast_proxy

    def routes_for(self, thinking: bool) -> list[Optional[str]]:
        """这一次调用按顺序尝试的线路。首选线路是代理时，后面跟一条直连作降级。"""
        first = self.proxy_for(thinking)
        if not first:
            return [None]
        if not self.fallback_direct:
            return [first]
        with self._lock:
            proxy_usable = self._proxy_breaker.allow()
        return [first, None] if proxy_usable else [None]

    def health(self) -> dict:
        """降级状态快照，给主循环/观察者打点用。"""
        with self._lock:
            return {
                **self.stats,
                "last_route": self.last_route,
                "proxy_open_s": round(self._proxy_breaker.remaining(), 1),
                "endpoint_open_s": round(self._endpoint_breaker.remaining(), 1),
            }

    def _post_once(self, url: str, headers: dict, data: str, timeout: float, proxy: Optional[str]) -> str:
        """发一次请求并取出 content；任何失败都抛带 `kind` 的 LLMError。"""
        status, text = self._poster(url, headers, data, timeout, proxy)
        if status != 200:
            raise LLMError(f"HTTP {status}: {text[:200]}", kind="http", status=status)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"回复不是 JSON: {text[:160]}", kind="bad_body") from e
        if payload.get("error"):
            raise LLMError(str(payload["error"])[:300], kind="api")

        choice = (payload.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        if content:
            return content

        # 空回复：多半是思考把 token 吃光了
        usage = payload.get("usage") or {}
        raise LLMError(
            f"空 content（finish={choice.get('finish_reason')}, "
            f"completion={usage.get('completion_tokens')}）", kind="empty")

    # ---- 单次调用 ----------------------------------------------------
    def chat(
        self,
        messages: list[dict],
        thinking: bool = False,
        max_tokens: int = 900,
        timeout: Optional[float] = None,
    ) -> str:
        if not self.key:
            raise LLMError("没有 LLM_KEY（见 .env.example）", kind="config")
        with self._lock:
            if not self._endpoint_breaker.allow():
                self.stats["short_circuited"] += 1
                raise LLMError(f"模型端点熔断中，还剩 {self._endpoint_breaker.remaining():.0f}s", kind="circuit_open")
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
            env_fast = float(os.environ.get("MC_MODEL_TIMEOUT_S", "8"))
            request_timeout = self.timeout if thinking else min(self.timeout, env_fast)
        data = json.dumps(body)

        # 两条线路共用这一次调用的时间预算：降级直连只用剩下的时间，不让调用方多等
        deadline = self._clock() + request_timeout
        routes = self.routes_for(thinking)
        error: Optional[LLMError] = None
        failed_via: Optional[str] = None
        for i, proxy in enumerate(routes):
            budget = request_timeout if i == 0 else deadline - self._clock()
            if i > 0 and budget < MIN_FALLBACK_BUDGET_S:
                break
            try:
                content = self._post_once(url, headers, data, budget, proxy)
            except LLMError as e:
                error, failed_via = e, proxy
                if proxy and e.route_fault:
                    with self._lock:
                        opened = self._proxy_breaker.fail()
                        if opened:
                            self.stats["proxy_opened"] += 1
                    if opened:
                        self._log(f"  [llm] 代理 {proxy} 连续失败，{self._proxy_breaker.cooldown_s:.0f}s 内改走直连: {e}")
                    if i + 1 < len(routes):
                        continue  # 换直连再试一次
                break
            with self._lock:
                if proxy:
                    self._proxy_breaker.ok()
                self._endpoint_breaker.ok()
                self.last_route = "proxy" if proxy else "direct"
                self.stats["proxy_ok" if proxy else "direct_ok"] += 1
                if i > 0:
                    self.stats["fallback_direct"] += 1
            return content

        assert error is not None
        starved = error.kind == "timeout" and request_timeout < MIN_FAIR_TIMEOUT_S
        # 只坏在代理这一段、且下次还能改走直连：这不是端点的错，不计入端点熔断。
        # 否则一个"慢慢失败"的代理会让两个熔断器同开同关，永远轮不到直连。
        proxy_only = bool(failed_via) and error.route_fault and self.fallback_direct
        if error.endpoint_fault and not starved and not proxy_only:
            with self._lock:
                opened = self._endpoint_breaker.fail()
                if opened:
                    self.stats["endpoint_opened"] += 1
            if opened:
                self._log(f"  [llm] 模型端点连续失败，熔断 {self._endpoint_breaker.cooldown_s:.0f}s，期间走规则兜底: {error}")
        raise error

    # ---- 带校验回喂的重问循环（原子动作决策与技能选择共用） ------------
    def ask_json(
        self,
        system: str,
        user: str,
        check: Callable[[Any], "tuple[bool, list[str]]"],
        attempts: int = 2,
        thinking: bool = False,
        total_timeout: Optional[float] = None,
    ) -> tuple[Optional[dict], list[str], int]:
        """问模型要一个 JSON 对象，用 `check` 校验；不合法就把错误原文回喂重问。

        返回 (通过校验的对象或 None, 最后一次的错误列表, 实际调用次数)。
        仍不合法就返回 None，由上层走规则兜底——**绝不让非法输出进入执行器**。
        """
        messages = [{"role": "system", "content": system}]
        errors: list[str] = []
        deadline = (time.monotonic() + total_timeout) if total_timeout is not None else None
        used_attempts = 0
        for attempt in range(1, attempts + 1):
            call_timeout: Optional[float] = None
            if deadline is not None:
                rem = deadline - time.monotonic()
                if rem <= 0.05:
                    errors = errors or ["决策总耗时到达上限"]
                    break
                call_timeout = rem
            used_attempts = attempt
            content = user
            if errors:
                content += ("\n上次输出被校验拒绝，错误如下，请修正后重新输出完整 JSON：\n- "
                            + "\n- ".join(errors[:8]) + "\n")
            content += "\n输出下一步的 JSON。"
            messages.append({"role": "user", "content": content})

            try:
                text = self.chat(messages, thinking=thinking, timeout=call_timeout)
            except LLMError as e:
                errors = [f"调用失败: {e}"]
                if e.kind == "circuit_open":
                    used_attempts = attempt - 1  # 熔断中没有发出请求：不重试、不睡眠，立刻交还给规则兜底
                    break
                if attempt == attempts:
                    break  # 后面没有重试了，不必再等
                sleep_s = min(2.0, 0.5 * attempt)
                if deadline is not None:
                    sleep_s = min(sleep_s, max(0.0, deadline - time.monotonic() - 0.05))
                if sleep_s > 0:
                    time.sleep(sleep_s)
                continue

            obj = extract_json(text)
            if obj is None:
                errors = ["回复里找不到合法 JSON 对象"]
                messages.append({"role": "assistant", "content": text[:800]})
                continue

            ok, errs = check(obj)
            if ok:
                return obj, [], attempt
            errors = errs
            messages.append({"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)})

        return None, errors, used_attempts

    def decide(
        self,
        state_summary: str,
        allowed: Optional[list[str]] = None,
        extra: str = "",
        attempts: int = 2,
        thinking: bool = False,
        total_timeout: Optional[float] = None,
    ) -> tuple[Optional[Decision], list[str], int]:
        """原子动作决策。返回 (决策或 None, 最后一次的错误列表, 实际调用次数)。

        这是 uncovered 兜底路径；常规路径是选技能，见 `skill_choice.choose()`。
        """
        allowed = list(allowed or ALLOWED_ACTIONS)
        user = f"当前状态：\n{state_summary}\n\n可用动作：\n{', '.join(allowed)}\n"
        if extra:
            user += f"\n补充：\n{extra}\n"
        obj, errors, used = self.ask_json(SYSTEM_PROMPT, user, validate, attempts=attempts,
                                          thinking=thinking, total_timeout=total_timeout)
        return (from_dict(obj) if obj is not None else None), errors, used
