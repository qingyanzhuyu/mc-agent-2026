"""命令行入口：运行比赛主循环 / 持续挂机压测（`--soak`）/ 离线回放异常包（`--replay`）。

用法示例（在 `code/` 目录下）：
    uv run python -m mcagent.loop --dry
    uv run python -m mcagent.loop --local-target --soak --user agy_soak1 --max-steps 25 --disconnect-on-exit
    uv run python -m mcagent.loop --local-target --soak --mode focus --skill gather_wood --skill-args '{"count":2}'
    uv run python -m mcagent.loop --replay mcagent/logs/soak-.../incidents/INC-001-....json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..client import ApiError, Client, Config
from ..planner.llm import OFFICIAL_PROFILE, resolve_profile
from ..planner.skill_choice import meets_verification
from ..preflight import OFFICIAL_HOSTS
from ..runtime import Executor, Fatal, State
from ..skills import REGISTRY, contract_errors
from .goals import GOAL_STAGES, normalize_goal_ids
from . import (
    GOAL_MAP,
    GOALS,
    CampaignPlanner,
    LoopConfig,
    MainLoop,
    RulePlanner,
    SoakObserver,
    TapeClient,
    TapeRecorder,
    load_goal_telemetry,
    replay_incident,
    validate_ledger,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PKG_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOG_ROOT = PKG_DIR / "logs"


class _ExplicitStrategyAction(argparse.Action):
    """记录用户是否显式传入了 `--strategy`，以支持 `--llm` 未指定策略时默认收敛为 `llm`。"""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, "_strategy_explicit", True)


def resolve_cli_strategy(args: argparse.Namespace) -> str:
    """当 `--llm` 显式开启且未指定 `--strategy` 时，默认走目标级异步战略 `llm`。"""
    if getattr(args, "llm", False) and not getattr(args, "_strategy_explicit", False):
        return "llm"
    return str(getattr(args, "strategy", "rule"))


def resolve_cli_include_stages(args: argparse.Namespace) -> tuple[str, ...] | None:
    """解析 `--include-draft` 与 `--goal-stages`；未传时返回 None 以保持既有默认阶段门禁。"""
    raw_stages = getattr(args, "goal_stages", None)
    include_draft = bool(getattr(args, "include_draft", False))
    if raw_stages is None:
        return GOAL_STAGES if include_draft else None

    tokens: list[str] = []
    for part in str(raw_stages).split(","):
        st = part.strip()
        if st and st not in tokens:
            tokens.append(st)
    invalid = [st for st in tokens if st not in GOAL_STAGES]
    if not tokens or invalid:
        raise ValueError(
            f"非法 --goal-stages {', '.join(invalid) or raw_stages!r}（仅允许: {', '.join(GOAL_STAGES)}）"
        )
    if include_draft and "draft" not in tokens:
        tokens.insert(0, "draft")
    return tuple(tokens)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Minecraft Agent 统一比赛主循环与持续异常取证运行器")
    p.add_argument("--mode", choices=("campaign", "focus"), default="campaign", help="campaign=比赛跑法, focus=单技能定向验收")
    p.add_argument("--skill", default=None, help="focus 模式下要运行的技能名")
    p.add_argument("--skill-args", default="{}", help="focus 模式下的 JSON 参数对象")
    p.add_argument("--goal", default=None,
                   help="聚焦推进并验收目标台账中的 goal_id；多个用逗号分隔表示同一轮一起追（如 smelt_iron,kill_a_mob）")
    p.add_argument("--include-draft", action="store_true",
                   help="解锁全部 draft 阶段目标进入规划器与战略菜单（等价全阶段 GOAL_STAGES；仍严格受 --min-verified 约束）")
    p.add_argument("--goal-stages", default=None,
                   help="逗号分隔的目标阶段白名单（可选: draft,candidate,graduated,live；未传时沿用默认门禁）")
    p.add_argument("--no-stop-on-goal", action="store_true", help="与 --goal 同用时达成目标后不提前结束，继续跑完预算")
    p.add_argument("--goal-telemetry", type=Path, default=None, help="可选实测成本 JSON 路径（用于动态调整目标排序）")
    p.add_argument("--strategy", choices=("legacy", "rule", "llm"), default="rule", action=_ExplicitStrategyAction,
                   help="战略层目标选择实现：默认 rule；若带 --llm 且未显式指定则自动收敛为 llm，显式 --strategy rule --llm 保留兼容")
    p.set_defaults(_strategy_explicit=False)
    p.add_argument("--strategy-review-s", type=float, default=None,
                   help="战略层例行复核目标的间隔秒数（默认 600s，可由 MC_STRATEGY_REVIEW_S 覆盖）")
    p.add_argument("--require-iron", action="store_true", default=None,
                   help="铁器（smelt_iron）达成前不调用战略层模型（默认关闭）")
    p.add_argument("--llm", action="store_true", help="开启 LLM 战略目标选择（未显式指定 --strategy 时默认走 llm；显式 --strategy rule --llm 保留旧菜单）")
    p.add_argument("--checkpoint", default=None, help="可选世界资源记忆检查点 JSON 路径（默认不落盘）")
    p.add_argument("--failure-cooldown-cap", type=float, default=8.0,
                   help="失败冷却上限秒数（默认 8.0s 防止长睡锁死；0 表示不额外封顶）")
    p.add_argument("--soak", action="store_true", help="挂载 SoakObserver（落盘 timeline/http_tape/progress_curve/异常包）")
    p.add_argument("--run-dir", type=Path, default=None, help="指定运行日志目录（默认 mcagent/logs/soak-YYYYmmdd-HHMMSS）")
    p.add_argument("--baseline", type=Path, default=None, help="对比上一版运行目录的 progress_curve.jsonl")
    p.add_argument("--world-snapshot", default="unspecified", help="当前测试世界快照编号（写入异常包用于复现追踪）")
    p.add_argument("--sample-verify", type=float, default=0.0, help="低频 get_block 抽样复核比例 0..1（默认 0 零额外 RTT）")
    p.add_argument("--stuck-steps", type=int, default=12, help="连续多少步无位移且背包不变判定为 STUCK_NO_PROGRESS")
    p.add_argument("--min-verified", choices=("offline", "live"), default=None, help="允许选用的最低技能验证等级")
    p.add_argument("--max-steps", type=int, default=0, help="最大动作步数（0=不限）")
    p.add_argument("--max-seconds", type=float, default=0.0, help="最大运行秒数（0=不限，正式赛可设 28800）")
    p.add_argument("--max-deaths", type=int, default=0, help="最大自愈重生次数（0=不限）")
    p.add_argument("--user", default=None, help="联调网关覆盖 MC_USER（自动隔离内存 token，不覆盖 .token.json）")
    p.add_argument("--local-target", action="store_true", help="确认连接的是队内本地/局域网联调网关")
    p.add_argument("--live", action="store_true", help="确认已在 docs/05 账号表占位并授权登录官方端点")
    p.add_argument("--disconnect-on-exit", action="store_true", help="退出时主动调 POST /disconnect 下线释放测试世界")
    p.add_argument("--dry", action="store_true", help="离线契约与端点连通性干跑（不登录、不占号）")
    p.add_argument("--replay", type=Path, default=None, help="离线确定性重放指定 INC-*.json 异常包")
    return p


def _run_dry(
    cfg: Config,
    *,
    min_verified: str = "offline",
    strategy: str = "rule",
    include_stages: tuple[str, ...] | None = None,
) -> int:
    print(f"[dry] 已注册技能 ({len(REGISTRY)}): {', '.join(sorted(REGISTRY))}")
    for name, cls in sorted(REGISTRY.items()):
        errs = contract_errors(cls)
        if errs:
            print(f"[dry] 契约不合格 {name}: {errs}")
            return 1
    ledger_errs = validate_ledger(GOALS)
    if ledger_errs:
        print(f"[dry] 目标台账契约不合格: {ledger_errs}")
        return 1
    planner = RulePlanner(min_verified=min_verified, include_stages=include_stages)
    active_cnt = len(planner.active_goals)
    print(f"[dry] 目标台账核验通过: 共 {len(GOALS)} 项（已激活 {active_cnt} 项）")
    st = State()
    st.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "pos": [0.5, 64.0, 0.5]})
    st.set_inventory({"items": []})
    d = planner.choose(st)
    print(f"[dry] 空包规则首拍决策 -> {d.skill} ({d.reason})")
    cp = CampaignPlanner(
        mode="campaign",
        strategy=strategy,
        min_verified=min_verified,
        include_stages=include_stages,
    )
    if strategy != "llm" and cp.select(st, []).skill is None:
        print(f"[dry] strategy={strategy} 空包首拍未产出技能")
        return 1
    ctx = cp.build_context(st)
    rendered = ctx.render()
    if len(rendered) > 2000:
        print(f"[dry] 战略决策上下文超长: {len(rendered)} > 2000")
        return 1
    rule_obj = cp.rule_objective(st)
    print(
        f"[dry] 战略层核验通过 (strategy={strategy}): 上下文 {len(rendered)} 字, "
        f"可选目标 {len(ctx.eligible_goals)} 项, 规则首选={rule_obj.goal_id if rule_obj else None}"
    )
    c = Client(cfg)
    code = c.probe()
    print(f"[dry] 端点无凭据探测 ({cfg.base}) -> {code}")
    return 0 if code in ("unauthorized", "invalid_token", "ok") else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    if args.replay is not None:
        report = replay_incident(args.replay)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    try:
        focus_args = json.loads(args.skill_args)
        if not isinstance(focus_args, dict):
            raise ValueError("--skill-args 必须是 JSON 对象")
    except Exception as e:
        print(f"参数错误: {e}", file=sys.stderr)
        return 2

    if args.goal is not None:
        goal_ids = normalize_goal_ids(args.goal)
        unknown = [gid for gid in goal_ids if gid not in GOAL_MAP]
        if not goal_ids or unknown:
            print(f"参数错误: 未知 --goal {', '.join(unknown) or args.goal!r}（可选: {', '.join(GOAL_MAP)}）",
                  file=sys.stderr)
            return 2
        args.goal = ",".join(goal_ids)

    try:
        include_stages = resolve_cli_include_stages(args)
    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        return 2

    cfg = Config.load()
    if args.user:
        cfg.username = args.user
        cfg.password = cfg.password or "localdev"
        cfg.token_file = None  # 临时测试 bot 不污染默认 .token.json

    effective_strategy = resolve_cli_strategy(args)

    if args.dry:
        return _run_dry(
            cfg,
            min_verified=args.min_verified or "offline",
            strategy=effective_strategy,
            include_stages=include_stages,
        )

    official = urlsplit(cfg.base).hostname in OFFICIAL_HOSTS
    if official and not args.live:
        print("拒绝登录：当前指向官方端点，请先在 docs/05 占位并显式加 --live（见 AGENTS.md §3）", file=sys.stderr)
        return 2
    if not official and not args.local_target:
        print("拒绝运行：当前指向非官方端点，请显式加 --local-target 标注测试数据来源（见 AGENTS.md §4）", file=sys.stderr)
        return 2
    if not cfg.has_credentials:
        print("缺少 MC_USER / MC_PASS（见 mcagent/.env.example 或传 --user）", file=sys.stderr)
        return 2

    use_llm = args.llm and effective_strategy != "legacy"
    llm_meta: dict = {}
    if use_llm or effective_strategy == "llm":
        try:
            prof = resolve_profile()
        except ValueError as e:
            print(f"参数错误: {e}", file=sys.stderr)
            return 2
        # 赛前训练放行：比赛服 + 自备模型，只在挂机（--soak）且显式设 MCAGENT_TRAIN_ON_OFFICIAL=1 时允许；
        # 比赛当天 preflight 会 BLOCK 这个开关和 LLM_PROFILE=custom，强制切回官方代理（AGENTS.md §2B.3）
        train_override = (
            official and prof["profile"] != OFFICIAL_PROFILE and args.soak
            and os.environ.get("MCAGENT_TRAIN_ON_OFFICIAL", "").strip() == "1"
        )
        if official and prof["profile"] != OFFICIAL_PROFILE and not train_override:
            print(f"拒绝运行：MC 指向官方端点时模型只能用 LLM_PROFILE=official（当前 {prof['profile']}，见 AGENTS.md §2B.3）；"
                  "赛前挂机训练可设 MCAGENT_TRAIN_ON_OFFICIAL=1 并加 --soak",
                  file=sys.stderr)
            return 2
        if train_override:
            print(f"[loop] ⚠ 比赛服训练模式：使用自备模型 {prof['model']}（LLM_PROFILE={prof['profile']}）。"
                  "仅限赛前训练，比赛当天必须切回 official（preflight 会拦）", file=sys.stderr)
        llm_meta = {"llm_profile": prof["profile"], "llm_model": prof["model"]}
        if train_override:
            llm_meta["train_on_official"] = True
        print(f"[loop] LLM 配置: {prof['profile']} (model={prof['model'] or '(未设置)'})")

    source_tag = "official" if official else "local-target"
    min_ver = args.min_verified or ("live" if source_tag == "official" else "offline")
    if (official or args.live) and (args.include_draft or (include_stages is not None and "draft" in include_stages)):
        print(
            f"[loop] ⚠ 风险提示：官方端点已开启 --include-draft / draft 目标阶段（stages={list(include_stages or ())}），"
            f"仍严格受 --min-verified={min_ver} 技能验证门禁约束，未验证技能不会被放行。",
            file=sys.stderr,
        )
    if args.mode == "focus":
        cls = REGISTRY.get(args.skill)
        if cls is None:
            print(f"参数错误: focus 需要已注册的 --skill，收到 {args.skill!r}", file=sys.stderr)
            return 2
        if not meets_verification(cls, min_ver):
            print(f"参数错误: --skill {args.skill} 需要 verified={min_ver}，当前为 {cls.verified}", file=sys.stderr)
            return 2
    enable_obs = args.soak or args.run_dir is not None
    stop_on_goal = bool(args.goal and not args.no_stop_on_goal)
    telemetry = load_goal_telemetry(args.goal_telemetry)

    run_dir = args.run_dir or (DEFAULT_LOG_ROOT / time.strftime("soak-%Y%m%d-%H%M%S"))
    tape = TapeRecorder(tape_path=str(run_dir / "http_tape.jsonl")) if enable_obs else None
    client: Client = TapeClient(cfg, tape) if tape is not None else Client(cfg)

    state = State()
    ex_log_path = str(run_dir / "actions.jsonl") if enable_obs else os.path.join(str(DEFAULT_LOG_ROOT), "actions.jsonl")
    executor = Executor(client, state, log_path=ex_log_path)

    observer = None
    if enable_obs:
        observer = SoakObserver(
            run_dir,
            tape=tape,
            world_snapshot_id=args.world_snapshot,
            source=source_tag,
            sample_verify_rate=args.sample_verify,
            stuck_steps=args.stuck_steps,
            baseline_dir=args.baseline,
            target_goal=args.goal,
            run_meta={
                "mode": args.mode,
                "strategy": effective_strategy,
                "skill": args.skill,
                "target_goal": args.goal,
                "stop_on_goal": stop_on_goal,
                "use_llm": use_llm,
                **llm_meta,
                "min_verified": min_ver,
                "include_draft": bool(args.include_draft),
                "include_stages": list(include_stages) if include_stages is not None else None,
                "max_steps": args.max_steps,
                "max_seconds": args.max_seconds,
                "checkpoint_path": args.checkpoint,
                "base": cfg.base,
                "bot_user": cfg.username,
            },
        )

    planner = CampaignPlanner(
        mode=args.mode,
        strategy=effective_strategy,
        focus_skill=args.skill,
        focus_args=focus_args,
        use_llm=use_llm,
        min_verified=min_ver,
        on_llm_telemetry=observer.on_llm_call if observer else None,
        on_objective_telemetry=observer.on_objective_decision if observer else None,
        target_goal=args.goal,
        include_stages=include_stages,
        telemetry=telemetry,
        review_interval_s=args.strategy_review_s,
        require_iron=True if args.require_iron else None,
        max_seconds=args.max_seconds,
    )
    cooldown_cap = float(args.failure_cooldown_cap) if (args.failure_cooldown_cap and args.failure_cooldown_cap > 0) else None
    loop_cfg = LoopConfig(
        mode=args.mode,
        max_steps=args.max_steps,
        max_seconds=args.max_seconds,
        max_deaths=args.max_deaths,
        disconnect_on_exit=args.disconnect_on_exit,
        target_goal=args.goal,
        stop_on_goal=stop_on_goal,
        checkpoint_path=args.checkpoint,
        failure_cooldown_cap_s=cooldown_cap,
    )
    loop = MainLoop(client, state, executor, planner, config=loop_cfg, observer=observer)
    try:
        result = loop.run()
    except (Fatal, ApiError) as e:
        # 主循环只让致命错误（凭据、接口路径）穿透；给出可读原因和非零退出码，不打 traceback
        print(f"\n[loop] 致命错误，停机: {e}", file=sys.stderr)
        if observer is not None:
            print(f"[loop] 诊断汇总已写入: {observer.summary_path}", file=sys.stderr)
        return 1
    print(f"\n[loop] 结束: {result}")
    if observer is not None:
        print(f"[loop] 诊断汇总已写入: {observer.summary_path}")
    return 3 if result.stop_reason == "transient_outage" else 0


if __name__ == "__main__":
    sys.exit(main())
