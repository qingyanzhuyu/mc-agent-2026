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

from ..client import DEFAULT_BASE, Client, Config
from ..planner.llm import OFFICIAL_PROFILE, resolve_profile
from ..preflight import OFFICIAL_HOSTS
from ..runtime import Executor, State
from ..skills import REGISTRY, contract_errors
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Minecraft Agent 统一比赛主循环与持续异常取证运行器")
    p.add_argument("--mode", choices=("campaign", "focus"), default="campaign", help="campaign=比赛跑法, focus=单技能定向验收")
    p.add_argument("--skill", default=None, help="focus 模式下要运行的技能名")
    p.add_argument("--skill-args", default="{}", help="focus 模式下的 JSON 参数对象")
    p.add_argument("--goal", default=None, help="聚焦推进并验收目标台账中的指定 goal_id")
    p.add_argument("--no-stop-on-goal", action="store_true", help="与 --goal 同用时达成目标后不提前结束，继续跑完预算")
    p.add_argument("--goal-telemetry", type=Path, default=None, help="可选实测成本 JSON 路径（用于动态调整目标排序）")
    p.add_argument("--llm", action="store_true", help="开启 LLM 菜单选择（默认关闭，走纯规则队列；失败自动回退规则）")
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


def _run_dry(cfg: Config) -> int:
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
    active_cnt = sum(1 for g in GOALS if g.stage != "draft")
    print(f"[dry] 目标台账核验通过: 共 {len(GOALS)} 项（已激活 {active_cnt} 项）")
    st = State()
    st.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "pos": [0.5, 64.0, 0.5]})
    st.set_inventory({"items": []})
    d = RulePlanner().choose(st)
    print(f"[dry] 空包规则首拍决策 -> {d.skill} ({d.reason})")
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

    if args.goal is not None and args.goal not in GOAL_MAP:
        print(f"参数错误: 未知 --goal {args.goal!r}（可选: {', '.join(GOAL_MAP)}）", file=sys.stderr)
        return 2

    cfg = Config.load()
    if args.user:
        cfg.username = args.user
        cfg.password = cfg.password or "localdev"
        cfg.token_file = None  # 临时测试 bot 不污染默认 .token.json

    if args.dry:
        return _run_dry(cfg)

    official = cfg.base == DEFAULT_BASE
    if official and not args.live:
        print("拒绝登录：当前指向官方端点，请先在 docs/05 占位并显式加 --live（见 AGENTS.md §3）", file=sys.stderr)
        return 2
    if not official and not args.local_target:
        print("拒绝运行：当前指向非官方端点，请显式加 --local-target 标注测试数据来源（见 AGENTS.md §4）", file=sys.stderr)
        return 2
    if not cfg.has_credentials:
        print("缺少 MC_USER / MC_PASS（见 mcagent/.env.example 或传 --user）", file=sys.stderr)
        return 2

    llm_meta: dict = {}
    if args.llm:
        try:
            prof = resolve_profile()
        except ValueError as e:
            print(f"参数错误: {e}", file=sys.stderr)
            return 2
        # 校园网入口也是官方端点，但不等于 DEFAULT_BASE，所以按域名再判一次
        mc_official = official or any(h in cfg.base for h in OFFICIAL_HOSTS)
        if mc_official and prof["profile"] != OFFICIAL_PROFILE:
            print(f"拒绝运行：MC 指向官方端点时模型只能用 LLM_PROFILE=official（当前 {prof['profile']}，见 AGENTS.md §2B.3）",
                  file=sys.stderr)
            return 2
        llm_meta = {"llm_profile": prof["profile"], "llm_model": prof["model"]}
        print(f"[loop] LLM 配置: {prof['profile']} (model={prof['model'] or '(未设置)'})")

    source_tag = "official" if (official and not args.local_target) else "local-target"
    min_ver = args.min_verified or ("live" if source_tag == "official" else "offline")
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
                "skill": args.skill,
                "target_goal": args.goal,
                "stop_on_goal": stop_on_goal,
                "use_llm": args.llm,
                **llm_meta,
                "min_verified": min_ver,
                "max_steps": args.max_steps,
                "max_seconds": args.max_seconds,
                "base": cfg.base,
                "bot_user": cfg.username,
            },
        )

    planner = CampaignPlanner(
        mode=args.mode,
        focus_skill=args.skill,
        focus_args=focus_args,
        use_llm=args.llm,
        min_verified=min_ver,
        on_llm_telemetry=observer.on_llm_call if observer else None,
        target_goal=args.goal,
        telemetry=telemetry,
    )
    loop_cfg = LoopConfig(
        mode=args.mode,
        max_steps=args.max_steps,
        max_seconds=args.max_seconds,
        max_deaths=args.max_deaths,
        disconnect_on_exit=args.disconnect_on_exit,
        target_goal=args.goal,
        stop_on_goal=stop_on_goal,
    )
    loop = MainLoop(client, state, executor, planner, config=loop_cfg, observer=observer)
    result = loop.run()
    print(f"\n[loop] 结束: {result}")
    if observer is not None:
        print(f"[loop] 诊断汇总已写入: {observer.summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
