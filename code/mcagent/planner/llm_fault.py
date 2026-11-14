"""模型调用的故障分类与熔断器。`llm.py` 用它决定：换线路、熔断、还是原样报错。

阈值都是工程默认值（未经真机标定），由 `LLMClient` 从 `LLM_BREAKER_*` 环境变量读入。
"""
from __future__ import annotations

from typing import Callable, Optional

#: 改直连重试至少要剩这么多时间，否则不值得再发一次
MIN_FALLBACK_BUDGET_S = 0.5
#: 给的超时比这还短时，超时不算端点故障（那是调用方的时间预算见底，不是端点慢）
MIN_FAIR_TIMEOUT_S = 1.0
#: 经代理收到这些状态码，按"线路坏了"处理（网关错误，多半不是模型端点自己回的）
GATEWAY_STATUSES = frozenset({502, 503, 504})


class LLMError(Exception):
    """一次模型调用失败。`kind` 决定降级怎么走：

    net 连不上/代理错误 · timeout 超时 · http 非 200（`status` 是状态码）· bad_body 回复不是 JSON ·
    api 端点返回 error 字段 · empty 空 content · config 没配 key · circuit_open 熔断中没有发请求 ·
    no_slot 本地并发槽没等到（没有发请求）· budget_exceeded 本地 token 预算已达上限（没有发请求）
    """

    def __init__(self, message: str = "", kind: str = "error", status: Optional[int] = None):
        super().__init__(message)
        self.kind = kind
        self.status = status

    @property
    def route_fault(self) -> bool:
        """换一条线路可能就好了（只对经代理的那次请求有意义）。超时不算：重发一次也没有时间了。"""
        return self.kind == "net" or (self.kind == "http" and self.status in GATEWAY_STATUSES)

    @property
    def endpoint_fault(self) -> bool:
        """计入端点熔断的失败。空 content 是模型行为（思考吃光 token），不是端点故障；
        no_slot / budget_exceeded 是本地治理（请求根本没发出去），计入会误伤健康端点。"""
        return self.kind not in ("empty", "config", "circuit_open", "no_slot", "budget_exceeded")


class Breaker:
    """连续失败计数熔断器。打开后冷却期内 `allow()` 为假；冷却结束放行一次试探，再失败立刻重新打开。"""

    def __init__(self, limit: int, cooldown_s: float, clock: Callable[[], float]):
        self.limit = max(1, int(limit))
        self.cooldown_s = float(cooldown_s)
        self._clock = clock
        self.fails = 0
        self.opened = 0
        self.open_until = float("-inf")

    def allow(self) -> bool:
        return self._clock() >= self.open_until

    def remaining(self) -> float:
        return max(0.0, self.open_until - self._clock())

    def ok(self) -> None:
        self.fails = 0

    def fail(self) -> bool:
        """记一次失败；返回 True 表示这一次把熔断器打开了。"""
        self.fails += 1
        if self.fails < self.limit:
            return False
        self.open_until = self._clock() + self.cooldown_s
        self.fails = self.limit - 1   # 半开：冷却后的第一次失败直接重新打开
        self.opened += 1
        return True
