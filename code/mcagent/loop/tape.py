"""HTTP 层录制与离线确定性回放（替代猜测性的物理替身，见 AGENTS.md §9.4）。

录制点选在 `Client.call()`：
1. 所有观察接口与动作接口都经由它，天然包含服务端真实物理结果与错误码；
2. 不接触 `auth/login` 的密码与 Bearer Token，磁盘零密钥（AGENTS.md §2A.1）；
3. 标记调用线程（`main` vs `monitor`），离线回放技能生成器时只喂 `main` 线程序列，
   不受后台 `SafetyMonitor` 并发轮询的时序干扰。
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from ..client import ApiError, Client, Config
from ..runtime import Executor, Fatal, Replan, State
from ..skills import REGISTRY, make
from ..skills.base import failure as unwrap_failure

_SECRET_KEYS = frozenset({"password", "token", "authorization", "api_key", "key", "secret"})


def _scrub(obj: Any, depth: int = 0) -> Any:
    """脱敏字典中的敏感键，防止凭据意外落盘。"""
    if depth > 8:
        return "..."
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in _SECRET_KEYS:
                out[k] = "<redacted>"
            else:
                out[k] = _scrub(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [_scrub(x, depth + 1) for x in obj]
    return copy.deepcopy(obj)


class TapeRecorder:
    """线程安全的 HTTP 调用环形缓冲区 + 可选全量 JSONL 落盘 + 当前技能切片。"""

    def __init__(self, tape_path: Optional[str] = None, ring_size: int = 160):
        self.tape_path = tape_path
        self._lock = threading.Lock()
        self._seq = 0
        self._ring: deque[dict] = deque(maxlen=ring_size)
        self._skill_slice: list[dict] = []
        self._recording_skill = False
        if tape_path:
            parent = os.path.dirname(os.path.abspath(tape_path))
            if parent:
                os.makedirs(parent, exist_ok=True)

    def begin_skill_slice(self) -> None:
        with self._lock:
            self._skill_slice = []
            self._recording_skill = True

    def end_skill_slice(self) -> list[dict]:
        with self._lock:
            out = copy.deepcopy(self._skill_slice)
            self._recording_skill = False
            return out

    def current_skill_slice(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._skill_slice)

    def recent(self, n: int = 40) -> list[dict]:
        with self._lock:
            items = list(self._ring)
        return copy.deepcopy(items[-n:])

    def record(
        self,
        *,
        method: str,
        path: str,
        body: Any,
        ms: float,
        ok: bool,
        thread: str,
        result: Optional[dict] = None,
        error: Optional[dict] = None,
    ) -> dict:
        clean_path = path.lstrip("/")
        if clean_path.startswith("auth/"):
            return {}
        entry: dict[str, Any] = {
            "t": round(time.time(), 3),
            "ms": round(ms, 1),
            "thread": thread,
            "method": method.upper(),
            "path": clean_path,
            "body": _scrub(body),
            "ok": ok,
        }
        if ok:
            entry["result"] = _scrub(result or {})
        else:
            entry["error"] = _scrub(error or {})

        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._ring.append(entry)
            if self._recording_skill and thread == "main":
                self._skill_slice.append(copy.deepcopy(entry))
            if self.tape_path:
                with open(self.tape_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry


class TapeClient(Client):
    """透明录制包装器：不改动 `Client` 契约，自动记录每次 `call()` 请求与响应。"""

    def __init__(self, cfg: Config, recorder: TapeRecorder, log=print):
        super().__init__(cfg, log=log)
        self.recorder = recorder
        self._tls = threading.local()

    def _thread_role(self) -> str:
        tname = threading.current_thread().name
        return "monitor" if "safety-monitor" in tname else "main"

    def call(self, path: str, body: Any = None, method: str = "POST", timeout: float = 30) -> dict:
        depth = getattr(self._tls, "depth", 0)
        self._tls.depth = depth + 1
        t0 = time.monotonic()
        try:
            res = super().call(path, body=body, method=method, timeout=timeout)
            if depth == 0:
                self.recorder.record(
                    method=method,
                    path=path,
                    body=body,
                    ms=(time.monotonic() - t0) * 1000.0,
                    ok=True,
                    thread=self._thread_role(),
                    result=res,
                )
            return res
        except ApiError as e:
            if depth == 0:
                self.recorder.record(
                    method=method,
                    path=path,
                    body=body,
                    ms=(time.monotonic() - t0) * 1000.0,
                    ok=False,
                    thread=self._thread_role(),
                    error={"code": e.code, "message": e.message, "extra": e.extra, "http": e.http},
                )
            raise
        finally:
            self._tls.depth = depth


class ReplayExhausted(RuntimeError):
    """录制的 HTTP 磁带已耗尽，但技能仍在继续发起请求。"""


class ReplayMismatch(RuntimeError):
    """技能回放发出的请求路径与录制磁带不一致（说明代码分支已改变）。"""


class ReplayClient(Client):
    """离线确定性回放客户端：完全断网，按录制顺序原样喂回响应或抛 `ApiError`。"""

    def __init__(
        self,
        entries: Iterable[dict],
        *,
        thread_filter: Optional[str] = "main",
        strict_path: bool = True,
        state: Optional[State] = None,
    ):
        cfg = Config(base="http://replay.offline/api/v1", username="", password="", token_file="")
        super().__init__(cfg, log=lambda *_: None)
        self.token = "offline-replay"
        self.strict_path = strict_path
        self._bound_state = state
        filtered = [
            copy.deepcopy(e)
            for e in entries
            if not thread_filter or e.get("thread", "main") == thread_filter
        ]
        self._queue: deque[dict] = deque(filtered)
        self.total_entries = len(filtered)
        self.consumed: list[dict] = []

    @property
    def remaining(self) -> int:
        return len(self._queue)

    def _send(self, *args, **kwargs) -> dict:  # pragma: no cover - 双重保险禁绝外网
        raise RuntimeError("ReplayClient 严禁发起真实网络请求")

    def ensure_connected(self, min_interval_s: float = 0.0, timeout: float = 20) -> dict:
        return self.connect(timeout=timeout)

    def wait_ready(self, timeout: float = 5.0, interval: float = 0.0, min_reconnect_s: float = 0.0) -> dict:
        st = self.status()
        if not st.get("ready"):
            raise ApiError("not_ready", "回放状态未 ready")
        return st

    def _peek_invalidate_cache_if_needed(self) -> None:
        """离线回放耗时≈0ms，若下一条磁带记录是 status/inventory，失效本地 TTL 缓存以对齐真实调用序。"""
        if self._bound_state is None or not self._queue:
            return
        nxt = self._queue[0].get("path")
        if nxt == "status":
            self._bound_state.status_at = float("-inf")
        elif nxt == "inventory":
            self._bound_state.invalidate_inventory()

    def call(self, path: str, body: Any = None, method: str = "POST", timeout: float = 30) -> dict:
        clean = path.lstrip("/")
        if not self._queue:
            raise ReplayExhausted(f"磁带已耗尽，待执行请求: {method.upper()} /{clean}")
        entry = self._queue.popleft()
        self.consumed.append(entry)
        rec_path = str(entry.get("path", "")).lstrip("/")
        if self.strict_path and rec_path != clean:
            raise ReplayMismatch(
                f"请求序列偏离录制磁带：当前发 /{clean}，录制第 {len(self.consumed)} 条为 /{rec_path}"
            )
        if not entry.get("ok", True):
            err = entry.get("error") or {}
            self._peek_invalidate_cache_if_needed()
            raise ApiError(
                err.get("code", "unknown"),
                err.get("message", ""),
                copy.deepcopy(err.get("extra") or {}),
                http=err.get("http"),
            )
        res = copy.deepcopy(entry.get("result") or {})
        self._peek_invalidate_cache_if_needed()
        return res


def replay_incident(
    incident_or_path: Union[str, Path, dict],
    *,
    registry: Optional[dict] = None,
    max_steps: int = 200,
) -> dict:
    """加载一份异常取证包（`INC-*.json`），离线确定性重放当时的技能决策与执行过程。"""
    if isinstance(incident_or_path, (str, Path)):
        data = json.loads(Path(incident_or_path).read_text(encoding="utf-8"))
    else:
        data = copy.deepcopy(incident_or_path)

    ctx = data.get("skill_context") or {}
    skill_name = ctx.get("skill")
    skill_args = ctx.get("args") or {}
    tape = data.get("http_tape") or []
    if not skill_name:
        raise ValueError("该异常包不包含可回放的 skill_context.skill（可能是纯会话层事件）")

    state = State()
    if ctx.get("initial_status"):
        state.set_status(ctx["initial_status"])
    if ctx.get("initial_inventory"):
        state.set_inventory(ctx["initial_inventory"])

    client = ReplayClient(tape, thread_filter="main", state=state)
    client._peek_invalidate_cache_if_needed()
    ex = Executor(client, state, log=lambda *_: None)
    reg = REGISTRY if registry is None else registry
    skill = make(skill_name, skill_args, registry=reg)
    before_counts = copy.deepcopy(ctx.get("before_counts") or {})

    steps: list[dict] = []
    outcome = "completed"
    err_code = ""
    err_msg = ""
    try:
        for _ in range(max_steps):
            orig_act = ex.act
            ex.act = lambda name, fn, retries=2, backoff=0.0, **kw: orig_act(
                name, fn, retries=retries, backoff=0.0, **kw
            )
            try:
                done = skill.step(ex, state)
            finally:
                ex.act = orig_act
            if done:
                break
            if skill.last is not None:
                steps.append(skill.last)
        else:
            outcome = "max_steps_exceeded"
    except (Replan, Fatal, ApiError) as e:
        outcome = type(e).__name__.lower()
        err_code, _ = unwrap_failure(e)
        err_msg = str(e)
    finally:
        skill.abort()

    post_err = ""
    verifier = getattr(skill, "verify_done", None)
    if outcome == "completed" and callable(verifier):
        post_err = verifier(state, before_counts) or ""
        if post_err:
            outcome = "postcondition_failed"

    orig_outcome = ctx.get("outcome")
    return {
        "incident_id": data.get("id"),
        "skill": skill_name,
        "args": skill_args,
        "outcome": outcome,
        "error_code": err_code,
        "error_message": err_msg,
        "postcondition_error": post_err,
        "steps_executed": len(steps),
        "steps": steps,
        "tape_consumed": len(client.consumed),
        "tape_total": client.total_entries,
        "matched_original_outcome": (orig_outcome == outcome) if orig_outcome else None,
    }
