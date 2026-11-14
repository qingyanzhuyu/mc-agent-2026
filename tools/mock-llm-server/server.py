#!/usr/bin/env python3
"""赛前离线测试专用：本地假冒 OpenAI 兼容模型接口（返回合成 usage，零额度消耗）。

⚠️ 合规红线（AGENTS.md §2B.3）：
- 本工具仅允许绑定本机回环地址（`127.0.0.1` / `localhost`），仅供赛前离线联调 Token 记账、
  熔断与流水线汇总逻辑；**严禁在正式比赛期间启动或将 `LLM_BASE` 指向本服务**。
- 与 `code/mcagent/` 物理隔离、零耦合。
"""
from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MENU_ITEM_RE = re.compile(r"^-\s+([a-z][a-z0-9_]*)\((.*?)\)\s+—", re.M)


def choose_mock_reply(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """从最后一条 user 消息的技能菜单中挑一个合法可启动技能，生成符合契约的 JSON。"""
    user_text = ""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            user_text = str(m.get("content") or "")
            break

    # 截取「可启动技能」段落，绝不选「暂不可启动」里的技能
    ready_section = user_text
    if "可启动技能" in user_text:
        ready_section = user_text.split("可启动技能", 1)[1]
        if "暂不可启动" in ready_section:
            ready_section = ready_section.split("暂不可启动", 1)[0]

    skills = [m.group(1) for m in MENU_ITEM_RE.finditer(ready_section)]
    if not skills:
        return {"a": 1}

    # 简单模拟开荒推进优先级，且避开刚刚报错重问里点名不可启动的项
    rejected_text = user_text.split("上次输出被校验拒绝", 1)[1] if "上次输出被校验拒绝" in user_text else ""

    if "eat_food" in skills and "饱食=" in user_text and "饱食=20" not in user_text:
        return {"skill": "eat_food", "args": {}, "why": "mock: 恢复饱食度"}

    if "craft_gear" in skills and "craft_gear" not in rejected_text:
        if "最优镐=无" in user_text and not any(f"原木={n} " in user_text for n in ("0", "1", "2")):
            return {"skill": "craft_gear", "args": {"item": "wooden_pickaxe"}, "why": "mock: 原木充足先造木镐"}
        if "最优镐=wooden_pickaxe" in user_text and not any(f"圆石={n} " in user_text for n in ("0", "1", "2")):
            return {"skill": "craft_gear", "args": {"item": "stone_pickaxe"}, "why": "mock: 升级石镐"}

    for pref, default_args in (
        ("mine_stone", {"count": 4}),
        ("gather_wood", {"count": 3}),
        ("scout_island", {"radius": 28}),
    ):
        if pref in skills and pref not in rejected_text:
            return {"skill": pref, "args": default_args, "why": f"mock: 选择 {pref}"}

    return {"skill": skills[0], "args": {}, "why": f"mock: 兜底选择 {skills[0]}"}


def build_completion_payload(body: dict[str, Any], *, prompt_base: int = 320, completion_base: int = 42) -> dict[str, Any]:
    messages = body.get("messages") or []
    char_len = sum(len(str(m.get("content") or "")) for m in messages if isinstance(m, dict))
    prompt_tokens = max(16, prompt_base + char_len // 4)
    reply_obj = choose_mock_reply(messages)
    content = json.dumps(reply_obj, ensure_ascii=False)
    completion_tokens = max(8, completion_base + len(content) // 4)
    return {
        "id": f"chatcmpl-mock-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(body.get("model") or "mock-local-test-only"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def make_server(host: str = "127.0.0.1", port: int = 18080, *, quiet: bool = False) -> ThreadingHTTPServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"安全护栏：mock-llm-server 仅允许绑定本机回环地址 {sorted(LOOPBACK_HOSTS)}；收到 {host!r}")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            if not quiet:
                super().log_message(fmt, *args)

        def _send_json(self, code: int, obj: dict[str, Any]) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") in ("/healthz", "/v1/models"):
                self._send_json(200, {"ok": True, "test_only": True, "data": [{"id": "default"}]})
                return
            self._send_json(404, {"error": f"unknown path {self.path}"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/v1/chat/completions":
                self._send_json(404, {"error": f"unknown path {self.path}"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid json body"})
                return
            self._send_json(200, build_completion_payload(body))

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="[TEST ONLY] 本地假冒 OpenAI 兼容接口（带合成 usage）")
    p.add_argument("--host", default="127.0.0.1", help="仅允许 127.0.0.1 / localhost")
    p.add_argument("--port", type=int, default=18080)
    args = p.parse_args(argv)

    srv = make_server(args.host, args.port)
    actual_host, actual_port = srv.server_address[:2]
    print(f"[TEST-ONLY mock-llm-server] 监听 http://{actual_host}:{actual_port}/v1 （严禁用于正式比赛）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
