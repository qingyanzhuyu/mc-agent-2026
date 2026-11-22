"""事件驱动的模型预取槽。只思考，不持有 Client/Executor，不运行游戏动作。

主循环与 CampaignPlanner 在触发换目标时 notify，在技能边界 poll / poll_detailed；
查询结果永不阻塞等待模型。超时或世代失效的结果自动丢弃，当拍由规则 Objective 先行推进。
"""
from __future__ import annotations

import math
import os
import threading
import time
from typing import Any, Callable, Optional

from ..runtime import RuntimeConfig


def _env_require_iron(default: bool = True) -> bool:
    raw = os.environ.get("MC_SCHEDULER_REQUIRE_IRON")
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


class DecisionScheduler:
    EVENTS = frozenset({"skill_done", "skill_failed", "phase_changed", "uncovered", "review", "objective"})

    def __init__(
        self,
        ask: Callable[[str], Any],
        config: RuntimeConfig | None = None,
        *,
        require_iron: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ask = ask
        self.config = config or RuntimeConfig.load()
        self.require_iron: bool = _env_require_iron(True) if require_iron is None else bool(require_iron)
        self._clock = clock
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._pending: dict[str, Any] | None = None
        self._last_request = float("-inf")
        self._last_review = self._clock()
        self._failed_skill: str | None = None
        self._failures = 0
        self._generation = 0

    def has_pending(self) -> bool:
        """当前是否有尚未取走或正在后台计算的请求。"""
        with self._lock:
            return self._pending is not None

    def invalidate(self) -> Optional[dict[str, Any]]:
        """目标/阶段/安全意图变更时丢弃旧计划；不会产生第二个失控工作线程。"""
        with self._lock:
            self._generation += 1
            old = self._pending
            self._pending = None
            return old

    def notify(
        self,
        event: str,
        summary: str,
        *,
        skill: str = "",
        iron_complete: bool = False,
        meta: Optional[dict[str, Any]] = None,
        ask_override: Optional[Callable[[str], Any]] = None,
    ) -> bool:
        if event not in self.EVENTS:
            raise ValueError(f"未知决策事件 {event}")
        with self._lock:
            if event == "phase_changed":
                self._generation += 1
                self._pending = None
            if self.require_iron and not iron_complete:
                return False
            if event == "skill_failed":
                self._failures = self._failures + 1 if skill == self._failed_skill else 1
                self._failed_skill = skill
                if self._failures < self.config.model_failure_count:
                    return False
            elif event == "skill_done":
                self._failed_skill, self._failures = None, 0
            now = self._clock()
            if event == "review" and now - self._last_review < self.config.model_review_s:
                return False
            if self._worker and self._worker.is_alive():
                return False
            if self._pending is not None and not self._pending["done"]:
                return False
            routine = event in ("skill_done", "review")
            if routine and (
                self._pending is not None
                or now - self._last_request < self.config.model_min_interval_s
            ):
                return False
            self._last_request = now
            if event == "review":
                self._last_review = now
            fn = ask_override or self.ask
            request: dict[str, Any] = {
                "generation": self._generation,
                "started": now,
                "deadline": now + self.config.model_timeout_s,
                "done": False,
                "value": None,
                "error": None,
                "finished": float("inf"),
                "meta": dict(meta or {}),
            }
            self._pending = request

            def run() -> None:
                err_text: Optional[str] = None
                try:
                    value = fn(summary)
                except Exception as exc:
                    value = None
                    err_text = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    request.update(done=True, value=value, error=err_text, finished=self._clock())

            self._worker = threading.Thread(target=run, name="mcagent-planner", daemon=True)
            self._worker.start()
            return True

    def poll_detailed(self) -> Optional[dict[str, Any]]:
        """非阻塞读取预取槽状态：返回 None（无挂起）或带 status/value/error/meta/elapsed_ms 的字典。"""
        with self._lock:
            request = self._pending
            if request is None:
                return None
            now = self._clock()
            if not request["done"] and now < request["deadline"]:
                return {
                    "status": "running",
                    "value": None,
                    "raw_value": None,
                    "error": None,
                    "meta": dict(request.get("meta") or {}),
                    "elapsed_ms": round(max(0.0, now - request["started"]) * 1000.0, 1),
                }
            self._pending = None
            if request["generation"] != self._generation:
                status = "stale_generation"
            elif not request["done"] or request["finished"] > request["deadline"]:
                status = "timeout"
            elif request["error"] is not None:
                status = "error"
            elif request["value"] is None:
                status = "empty"
            else:
                status = "ready"
            end_t = request["finished"] if math.isfinite(request["finished"]) else now
            elapsed_ms = round(max(0.0, end_t - request["started"]) * 1000.0, 1)
            return {
                "status": status,
                "value": request["value"] if status == "ready" else None,
                "raw_value": request["value"],
                "error": request["error"],
                "meta": dict(request.get("meta") or {}),
                "elapsed_ms": elapsed_ms,
            }

    def poll(self, fallback: Callable[[], Any]) -> Any:
        """未就绪返回 None（继续当前技能）；到期限/失败就给规则目标，不阻塞。"""
        res = self.poll_detailed()
        if res is None or res["status"] == "running":
            return None
        if res["status"] == "ready":
            return res["value"]
        return fallback()
