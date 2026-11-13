"""loop —— 比赛与持续挂机共用的主循环、预期收敛核对器与 HTTP 录制回放子系统。

模块结构（遵守 AGENTS.md §9.3 一文件一职责）：
    engine.py          MainLoop / LoopConfig：登录就绪、选技能、单步执行、死亡/断线自愈恢复
    policy.py          RulePlanner / CampaignPlanner：按真实背包实时派生阶段 + 可选 LLM 选择
    starter_skills.py  首批基础生存 L3 技能（探索、砍树、工作台/工具合成、挖圆石、进食）
    auditor.py         SoakObserver：技能后置判据核对、bug/策略/环境波动分流、进度分曲线、去重异常包
    tape.py            TapeClient / ReplayClient / replay_incident：HTTP 层脱敏录制与离线确定性回放
"""
from . import starter_skills as _starter_skills  # noqa: F401  导入即向 skills.REGISTRY 注册首批技能
from .auditor import ADVANCEMENT_SCORES_V2, SoakObserver, git_provenance
from .engine import LoopConfig, MainLoop, RunResult
from .policy import CampaignPlanner, PlanDecision, RulePlanner, build_state_summary
from .starter_skills import reset_world_memo
from .tape import ReplayClient, ReplayExhausted, ReplayMismatch, TapeClient, TapeRecorder, replay_incident

__all__ = [
    "ADVANCEMENT_SCORES_V2",
    "CampaignPlanner",
    "LoopConfig",
    "MainLoop",
    "PlanDecision",
    "ReplayClient",
    "ReplayExhausted",
    "ReplayMismatch",
    "RulePlanner",
    "RunResult",
    "SoakObserver",
    "TapeClient",
    "TapeRecorder",
    "build_state_summary",
    "git_provenance",
    "replay_incident",
    "reset_world_memo",
]
