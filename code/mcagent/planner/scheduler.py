"""事件驱动的模型预取槽。只思考，不持有 Client/Executor，不运行游戏动作。

主循环（后续实现）在动作发出前 notify，在动作返回时 poll；poll 永不等待模型。
铁器前交给确定性技能；超时返回调用方的固定顺序 fallback，迟到结果丢弃。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ..runtime import RuntimeConfig


class DecisionScheduler:
    EVENTS = frozenset({"skill_done", "skill_failed", "phase_changed", "uncovered", "review"})

    def __init__(self, ask: Callable[[str], Any], config: RuntimeConfig | None = None):
        self.ask = ask
        self.config = config or RuntimeConfig.load()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._pending: dict | None = None
        self._last_request = float("-inf")
        self._last_review = time.monotonic()
        self._failed_skill: str | None = None
        self._failures = 0
        self._generation = 0

    def invalidate(self) -> None:
        """目标/阶段/安全意图变更时丢弃旧计划；不会产生第二个失控工作线程。"""
        with self._lock:
            self._generation += 1
            self._pending = None

    def notify(self, event: str, summary: str, *, skill: str = "", iron_complete: bool = False) -> bool:
        if event not in self.EVENTS:
            raise ValueError(f"未知决策事件 {event}")
        with self._lock:
            if event == "phase_changed":
                self._generation += 1
                self._pending = None
            if not iron_complete:
                return False
            if event == "skill_failed":
                self._failures = self._failures + 1 if skill == self._failed_skill else 1
                self._failed_skill = skill
                if self._failures < self.config.model_failure_count:
                    return False
            elif event == "skill_done":
                self._failed_skill, self._failures = None, 0
            now = time.monotonic()
            if event == "review" and now - self._last_review < self.config.model_review_s:
                return False
            if (self._pending is not None or (self._worker and self._worker.is_alive())
                    or now - self._last_request < self.config.model_min_interval_s):
                return False
            self._last_request = self._last_review = now
            request = {"generation": self._generation, "deadline": now + self.config.model_timeout_s,
                       "done": False, "value": None, "finished": float("inf")}
            self._pending = request

            def run():
                try:
                    value = self.ask(summary)
                except Exception:
                    value = None
                with self._lock:
                    request.update(done=True, value=value, finished=time.monotonic())

            self._worker = threading.Thread(target=run, name="mcagent-planner", daemon=True)
            self._worker.start()
            return True

    def poll(self, fallback: Callable[[], Any]) -> Any:
        """未就绪返回 None（继续当前技能）；到期限/失败就给规则目标，不阻塞。"""
        with self._lock:
            request = self._pending
            if request is None:
                return None
            now = time.monotonic()
            if not request["done"] and now < request["deadline"]:
                return None
            self._pending = None
            valid = (request["generation"] == self._generation and request["done"]
                     and request["finished"] <= request["deadline"] and request["value"] is not None)
            value = request["value"] if valid else None
        return value if valid else fallback()
