"""聚合长跑日志生成与 `goals.load_goal_telemetry()` 兼容的 `goal_cost_table.json`（零坐标、离线可复现）。"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import statistics
import tempfile
from typing import Any, Iterable, Sequence

from .goals import GOAL_MAP

_STEP_GOAL_RE = re.compile(r"推进目标\s+([a-z0-9_]+)")
_OBJ_GOAL_RE = re.compile(r"\[目标:([a-z0-9_]+)\]")
_BLOCKED_GOAL_RE = re.compile(r"目标\s+([a-z0-9_]+)\s+当前")


def _extract_goal_id(reason: str) -> str | None:
    if not reason:
        return None
    for pat in (_STEP_GOAL_RE, _BLOCKED_GOAL_RE, _OBJ_GOAL_RE):
        m = pat.search(reason)
        if m and m.group(1) in GOAL_MAP:
            return m.group(1)
    return None


def _iter_run_dirs(roots: Iterable[str | Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for raw in roots:
        p = Path(raw)
        if not p.exists():
            continue
        if (p / "timeline.jsonl").is_file():
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                found.append(p)
            continue
        for child in sorted(p.iterdir()):
            if child.is_dir() and (child / "timeline.jsonl").is_file():
                rp = child.resolve()
                if rp not in seen:
                    seen.add(rp)
                    found.append(child)
    return found


def aggregate_goal_telemetry(
    run_dirs: Sequence[str | Path],
    *,
    source: str = "local-target",
) -> dict[str, Any]:
    """从一组长跑目录（含 `timeline.jsonl` / `progress_curve.jsonl`）聚合每项目标的实测成本表。"""
    resolved_dirs = _iter_run_dirs(run_dirs)
    per_goal_durations: dict[str, list[float]] = defaultdict(list)
    per_goal_episodes: dict[str, int] = defaultdict(int)
    per_goal_hits: dict[str, int] = defaultdict(int)
    per_goal_ok: dict[str, int] = defaultdict(int)
    per_goal_fail: dict[str, int] = defaultdict(int)
    per_goal_env: dict[str, int] = defaultdict(int)
    run_labels: list[str] = []

    for rdir in resolved_dirs:
        run_labels.append(rdir.name)
        reached_in_run: set[str] = set()
        curve_path = rdir / "progress_curve.jsonl"
        if curve_path.is_file():
            for line in curve_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                for gid in row.get("goals_reached") or ():
                    if isinstance(gid, str) and gid in GOAL_MAP:
                        reached_in_run.add(gid)

        attempted_in_run: set[str] = set()
        run_goal_elapsed: dict[str, float] = defaultdict(float)
        active_goal: str | None = None
        active_start_s: float | None = None

        tl_path = rdir / "timeline.jsonl"
        for line in tl_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            etype = ev.get("event")
            elapsed = ev.get("elapsed_s")
            t_s = float(elapsed) if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) else None
            if etype == "skill_start":
                gid = _extract_goal_id(str(ev.get("reason") or ""))
                active_goal = gid
                active_start_s = t_s
                if gid is not None:
                    attempted_in_run.add(gid)
            elif etype == "skill_end":
                if active_goal is not None:
                    dt = max(0.1, (t_s - active_start_s)) if (t_s is not None and active_start_s is not None) else 0.0
                    if dt > 0.0:
                        run_goal_elapsed[active_goal] += dt
                    outcome = str(ev.get("outcome") or "").lower()
                    if outcome == "completed":
                        per_goal_ok[active_goal] += 1
                    elif outcome == "env_blocked":
                        per_goal_env[active_goal] += 1
                    else:
                        per_goal_fail[active_goal] += 1
                active_goal = None
                active_start_s = None
            elif etype == "objective_decision":
                gid = ev.get("goal_id")
                if isinstance(gid, str) and gid in GOAL_MAP and ev.get("adopted") is True:
                    attempted_in_run.add(gid)

        for gid in attempted_in_run | reached_in_run:
            per_goal_episodes[gid] += 1
            if gid in reached_in_run:
                per_goal_hits[gid] += 1
            if run_goal_elapsed.get(gid, 0.0) > 0.0:
                per_goal_durations[gid].append(round(run_goal_elapsed[gid], 1))

    goals_out: dict[str, dict[str, Any]] = {}
    for gid in sorted(per_goal_episodes):
        eps = per_goal_episodes[gid]
        hits = per_goal_hits[gid]
        durs = per_goal_durations.get(gid, [])
        p50 = round(float(statistics.median(durs)), 1) if durs else float(GOAL_MAP[gid].est_cost_s)
        goals_out[gid] = {
            "episodes": eps,
            "hits": hits,
            "hit_rate": round(hits / max(1, eps), 4),
            "p50_elapsed_s": p50,
            "skill_ok": per_goal_ok[gid],
            "skill_fail": per_goal_fail[gid],
            "skill_env_blocked": per_goal_env[gid],
        }

    return {
        "schema_version": 1,
        "source": source,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs_included": run_labels,
        "note": "由自搭困难服(local-target)长跑实测日志聚合生成，仅用于本地/相对成本重排参考，不作为官方校园网绝对时间预算（AGENTS.md §4）",
        "goals": goals_out,
    }


def write_goal_cost_table(
    run_dirs: Sequence[str | Path],
    output_path: str | Path,
    *,
    source: str = "local-target",
) -> dict[str, Any]:
    """原子写出 `goal_cost_table.json`（严格零坐标、UTF-8 编码）。"""
    payload = aggregate_goal_telemetry(run_dirs, source=source)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_path = fh.name
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从长跑日志聚合生成 goal_cost_table.json 遥测文件")
    parser.add_argument("--runs", nargs="+", required=True, help="一个或多个长跑日志目录（支持含 seg-* 子目录）")
    parser.add_argument("--output", required=True, help="输出 goal_cost_table.json 路径")
    parser.add_argument("--source", default="local-target", help="遥测来源标识（默认 local-target）")
    args = parser.parse_args(argv)
    table = write_goal_cost_table(args.runs, args.output, source=args.source)
    print(f"[telemetry] 已聚合 {len(table['runs_included'])} 段日志 -> {len(table['goals'])} 项实测目标成本 ({args.output})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
