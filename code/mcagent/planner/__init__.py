"""planner —— 把状态变成"下一个动作"。

分工（别混淆）：
    planner/   决策：看状态 + 目标 → 输出一个 Decision（JSON）
    skills/    执行：一个目标 → 一串 Executor 动作，自带中间状态
    runtime/   执行器：单飞锁、错误分类、重试、打点

一个典型循环：

    from .planner import Decision, validate, bind
    from .planner.llm import LLMClient, decide

    d_dict = decide(llm, state.summary(), list(ALLOWED_ACTIONS))
    ok, errs = validate(d_dict)
    if not ok: ...           # 把 errs 回喂给模型重问一次，第二次还不合法就退规则兜底
    d = from_dict(d_dict)
    ex.act(d.action, bind(client, d))

两条不许破的规矩：
1. 模型只输出 JSON，绝不直接调接口。校验不通过就重问，不要"猜它的意思"。
2. 执行永远走 `Executor.act`，不要绕过它直接调 client。
"""
from .decision import ALLOWED_ACTIONS, Decision, bind, from_dict, validate
from .llm import LLMClient, LLMError, decide, extract_json

__all__ = [
    "ALLOWED_ACTIONS", "Decision", "bind", "from_dict", "validate",
    "LLMClient", "LLMError", "decide", "extract_json",
]
