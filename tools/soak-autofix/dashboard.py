#!/usr/bin/env python3
"""双工位挂机本地控制台（只监听 127.0.0.1，零第三方依赖）。

用途：
1. 一键启动 / 优雅停止 / 立即中断 / 重启 `soak_ctl.py run` 双工位守护；
2. 挂机中实时改下一轮的时长、步数、聚焦目标、玩家模型（直接写会话 state.json，Runner 每轮开跑前读取）；
3. 实时看当前轮（步数、用时、正在跑的技能、LLM 延迟、最近事件）与历史轮次、异常单、目标实测成本、守护日志；
4. 把开发分支热合入挂机分支（后台调用 `soak_ctl.inject_and_set_goal`，带离线门禁）。

本文件只做编排与展示，不碰比赛运行时代码；所有挂机语义（门禁、回滚、修单）仍由 `soak_ctl.py` 负责。

    python3 tools/soak-autofix/dashboard.py                    # 默认会话 code/mcagent/logs/soak-cont，端口 8790
    python3 tools/soak-autofix/dashboard.py --port 8791 --session-dir code/mcagent/logs/soak-night
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse, urlsplit

HERE = Path(__file__).resolve().parent
SOAK_CTL_PATH = HERE / "soak_ctl.py"
HTML_PATH = HERE / "dashboard.html"
RUN_LOG_NAME = "dashboard_run.log"
LAST_START_NAME = "dashboard_last_start.json"
SOAK_SUBCOMMANDS = ("run", "runner-loop", "fixer-loop")
EP_ID_RE = re.compile(r"^ep-\d{4}$")
MAX_TIMELINE_BYTES = 4 * 1024 * 1024


def _load_soak_ctl():
    spec = importlib.util.spec_from_file_location("soak_ctl_for_dashboard", SOAK_CTL_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


soak_ctl = _load_soak_ctl()


# ============================================================================
# 只读观察：会话状态、守护进程、当前轮、台账
# ============================================================================

def read_state_nolock(session_dir: Path) -> dict[str, Any]:
    """state.json 由 soak_ctl 原子替换写入，展示用读取不加锁（soak_ctl.read_state 会回写文件）。"""
    p = session_dir / "state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _boot_time() -> float:
    try:
        for ln in Path("/proc/stat").read_text().splitlines():
            if ln.startswith("btime "):
                return float(ln.split()[1])
    except OSError:
        pass
    return 0.0


def _proc_start_epoch(pid: int, boot: float) -> Optional[float]:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rindex(")") + 2:].split()
        ticks = int(fields[19])  # 第 22 列 starttime（去掉 pid 与 comm 后的下标 19）
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def find_soak_processes(session_dir: Path, proc_root: Path = Path("/proc")) -> list[dict[str, Any]]:
    """扫描 /proc 找属于该会话目录的 soak_ctl.py run / runner-loop / fixer-loop 进程（含终端里手动启动的）。"""
    target = session_dir.resolve()
    boot = _boot_time()
    found: list[dict[str, Any]] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return found
    for ent in entries:
        if not ent.name.isdigit():
            continue
        try:
            argv = [a for a in (ent / "cmdline").read_bytes().split(b"\0") if a]
            args = [a.decode("utf-8", "replace") for a in argv]
        except OSError:
            continue
        if not any(a.endswith("soak_ctl.py") for a in args):
            continue
        role = next((a for a in args if a in SOAK_SUBCOMMANDS), None)
        if role is None or "--session-dir" not in args:
            continue
        idx = args.index("--session-dir")
        if idx + 1 >= len(args):
            continue
        sd = Path(args[idx + 1])
        if not sd.is_absolute():
            try:
                sd = Path(os.readlink(ent / "cwd")) / sd
            except OSError:
                continue
        if sd.resolve() != target:
            continue
        pid = int(ent.name)
        found.append({
            "pid": pid,
            "role": role,
            "started_at": _proc_start_epoch(pid, boot),
            "cmd": " ".join(args[1:])[:400],
        })
    found.sort(key=lambda p: SOAK_SUBCOMMANDS.index(p["role"]))
    return found


def _read_jsonl_tail(path: Path, max_bytes: int = MAX_TIMELINE_BYTES) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for ln in data.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _first_jsonl(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.loads(fh.readline() or "{}")
    except (OSError, json.JSONDecodeError):
        return {}


def _event_text(ev: dict[str, Any]) -> str:
    kind = ev.get("event")
    if kind == "skill_start":
        return f"开始技能 {ev.get('skill')} {json.dumps(ev.get('args') or {}, ensure_ascii=False)}（{ev.get('source', '')}）"
    if kind == "skill_end":
        out = ev.get("outcome")
        if out == "env_blocked":
            env_info = ev.get("environment") or {}
            msg = env_info.get("message") or ev.get("error_msg") or ""
            wake = env_info.get("wake_on") or ""
            return f"环境挂起 {ev.get('skill')}" + (f"：{msg[:140]}" if msg else "") + (f"（恢复：{wake}）" if wake else "")
        if out == "budget_stopped":
            return f"预算中止 {ev.get('skill')}"
        msg = ev.get("error_msg") or ev.get("postcondition_error") or ""
        return f"技能结束 {ev.get('skill')} → {out}" + (f"：{msg[:160]}" if msg else "")
    if kind == "llm_call":
        state = "成功" if ev.get("ok") else "失败"
        tok = int(ev.get("total_tokens") or 0)
        tok_s = f" [{tok}tok]" if tok > 0 else ""
        return f"LLM {state} {float(ev.get('ms') or 0) / 1000:.1f}s → {ev.get('skill') or '-'}{tok_s}"
    if kind == "objective_decision":
        src = ev.get("source") or "rule"
        trig = ev.get("trigger") or ""
        adopted = "采纳" if ev.get("adopted") else "沿用"
        return f"战略目标 {ev.get('goal_id') or '-'}（{src}/{trig}/{adopted}）"
    if kind == "death_recovered":
        cause = ev.get("cause") or "原因未知"
        ms = ev.get("respawn_ms")
        ms_s = f"，{float(ms):.0f}ms" if ms is not None else ""
        return f"死亡重生 #{ev.get('deaths_total') or '?'}（{cause}{ms_s}）"
    if kind == "goal_reached":
        return f"达成目标 {ev.get('goal_id')}"
    if kind == "step":
        rec = ev.get("record") or {}
        err = rec.get("error_code") or ""
        return f"动作 {ev.get('op') or rec.get('op')}" + (f"（{err}）" if err else "") + f" hp={ev.get('hp')} 饱食={ev.get('food')}"
    if kind == "run_end":
        return f"本轮结束：{ev.get('stop_reason')}"
    if kind == "run_start":
        return "本轮开始"
    return str(kind)


def _event_kind(ev: dict[str, Any]) -> str:
    ev_type = str(ev.get("event") or "")
    if ev_type == "skill_end":
        out = ev.get("outcome")
        if out == "completed":
            return "skill_end"
        if out == "env_blocked":
            return "env_blocked"
        if out == "budget_stopped":
            return "budget_stopped"
        return "skill_fail"
    return ev_type


def summarize_live_episode(ep_dir: Path, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """解析一轮的 timeline.jsonl / progress_curve.jsonl，给出实时进度。目录不存在返回 None。"""
    tl_path = ep_dir / "timeline.jsonl"
    if not tl_path.exists():
        return None
    now = time.time() if now is None else now
    events = _read_jsonl_tail(tl_path)
    # 守护被中断后重启会复用同一轮次号并往同一个 timeline 追加：只看最后一次 run_start 之后的事件
    starts = [i for i, e in enumerate(events) if e.get("event") == "run_start"]
    if starts:
        head = events[starts[-1]]
        events = events[starts[-1]:]
    else:
        head = _first_jsonl(tl_path)
    meta = head.get("meta") or {}
    # Ctrl+C 中断时 loop 可能连写两条 run_end（keyboard_interrupt 后又一条 stopped），以第一条为准
    run_end = next((e for e in events if e.get("event") == "run_end"), None)
    steps = [e for e in events if e.get("event") == "step"]
    llm = [e for e in events if e.get("event") == "llm_call"]
    ends = [e for e in events if e.get("event") == "skill_end"]
    skill_starts = [e for e in events if e.get("event") == "skill_start"]
    obj_decisions = [e for e in events if e.get("event") == "objective_decision"]
    current_skill = None
    for e in events:
        if e.get("event") == "skill_start":
            current_skill = e.get("skill")
        elif e.get("event") == "skill_end" and e.get("skill") == current_skill:
            current_skill = None
    if run_end is not None:
        current_skill = None
    llm_ms = sorted(float(e.get("ms") or 0) for e in llm)
    curve = _read_jsonl_tail(ep_dir / "progress_curve.jsonl", max_bytes=256 * 1024)
    last_curve = curve[-1] if curve else {}
    last_step = steps[-1] if steps else {}
    t0 = head.get("t")
    if run_end is not None:
        elapsed = float(run_end.get("elapsed_s") or 0.0)
    elif isinstance(t0, (int, float)):
        elapsed = max(0.0, now - float(t0))
    else:
        elapsed = float(events[-1].get("elapsed_s") or 0.0) if events else 0.0
    prompt_tokens = int(llm[-1].get("cumulative_prompt_tokens") or 0) if llm else 0
    completion_tokens = int(llm[-1].get("cumulative_completion_tokens") or 0) if llm else 0
    tokens = int(llm[-1].get("cumulative_total_tokens") or 0) if llm else 0
    if run_end is not None:
        prompt_tokens = int(run_end.get("prompt_tokens") or prompt_tokens)
        completion_tokens = int(run_end.get("completion_tokens") or completion_tokens)
        tokens = int(run_end.get("total_tokens") or tokens)
    elif obj_decisions:
        last_obj = obj_decisions[-1]
        prompt_tokens = max(prompt_tokens, int(last_obj.get("cumulative_prompt_tokens") or 0))
        completion_tokens = max(completion_tokens, int(last_obj.get("cumulative_completion_tokens") or 0))
        tokens = max(tokens, int(last_obj.get("cumulative_total_tokens") or 0))

    decision_sources: dict[str, int] = {}
    if run_end is not None and isinstance(run_end.get("decision_sources"), dict):
        decision_sources = {str(k): int(v or 0) for k, v in run_end["decision_sources"].items()}
    else:
        for e in skill_starts:
            src = str(e.get("source") or "rule")
            decision_sources[src] = decision_sources.get(src, 0) + 1

    current_objective = None
    for od in reversed(obj_decisions):
        if od.get("adopted") or current_objective is None:
            current_objective = od.get("goal_id")
            if od.get("adopted"):
                break

    return {
        "ep_id": ep_dir.name,
        "finished": run_end is not None,
        "stop_reason": (run_end or {}).get("stop_reason"),
        "source": head.get("source") or (run_end or {}).get("source") or "",
        "bot_user": meta.get("bot_user"),
        "model": meta.get("llm_model") if meta.get("use_llm") else "规则",
        "strategy": meta.get("strategy") or "",
        "min_verified": meta.get("min_verified") or "",
        "base": meta.get("base") or "",
        "target_goal": meta.get("target_goal") or "",
        "commit": ((head.get("git") or {}).get("commit") or "")[:7],
        "max_seconds": meta.get("max_seconds"),
        "max_steps": meta.get("max_steps"),
        "elapsed_s": round(elapsed, 1),
        "steps": len(steps),
        "pos": last_step.get("pos") or head.get("pos"),
        "current_skill": current_skill,
        "current_objective": current_objective,
        "skills_completed": sum(1 for e in ends if e.get("outcome") == "completed"),
        "skills_env_blocked": sum(1 for e in ends if e.get("outcome") == "env_blocked"),
        "skills_budget_stopped": sum(1 for e in ends if e.get("outcome") == "budget_stopped"),
        "skills_failed": sum(
            1 for e in ends if e.get("outcome") not in ("completed", "env_blocked", "budget_stopped", None)
        ),
        "llm_calls": len(llm),
        "llm_fail": sum(1 for e in llm if not e.get("ok")),
        "llm_p50_ms": round(llm_ms[len(llm_ms) // 2]) if llm_ms else None,
        "llm_last_ms": round(float(llm[-1].get("ms") or 0)) if llm else None,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens": tokens,
        "decision_sources": decision_sources,
        "hp": last_step.get("hp"),
        "food": last_step.get("food"),
        "deaths": last_curve.get("deaths", 0),
        "server_advancements": last_curve.get("server_advancements", 0),
        "goals_reached": last_curve.get("goals_reached") or list(((run_end or {}).get("goals_reached") or {}).keys()),
        "environment_blocked_goals": dict(((run_end or {}).get("environment_blocked_goals") or {})),
        "server_score": last_curve.get("server_score_v2", 0),
        "inferred_score": last_curve.get("inferred_score_v2", 0),
        "recent": [
            {
                "t": e.get("elapsed_s"),
                "kind": _event_kind(e),
                "text": _event_text(e),
            }
            for e in events[-18:]
        ][::-1],
    }


_GOAL_FIELD_RES = {
    "id": re.compile(r'\bid="([^"]+)"'),
    "title": re.compile(r'\btitle="([^"]*)"'),
    "tier": re.compile(r'\btier="([^"]+)"'),
    "score": re.compile(r"\bscore=(\d+)"),
    "skill": re.compile(r'\bskill="([^"]+)"'),
    "stage": re.compile(r'\bstage="([^"]+)"'),
    "budget_s": re.compile(r"\bbudget_s=(\d+(?:\.\d+)?)"),
}
_PREREQ_RE = re.compile(r"\bprereq_goals=\(([^)]*)\)", re.S)
_STOP_LOSS_RE = re.compile(r"\bstop_loss=True\b")
_STR_LIT_RE = re.compile(r'"([^"]+)"')


def parse_ledger(text: str) -> list[dict[str, Any]]:
    """从 goals.py 源码静态抽取台账行（不导入 mcagent，避免依赖 uv 环境）。"""
    body = text.split("GOALS: tuple[GoalSpec, ...] = (", 1)[-1]
    goals: list[dict[str, Any]] = []
    for chunk in body.split("GoalSpec(")[1:]:
        row: dict[str, Any] = {}
        for key, rx in _GOAL_FIELD_RES.items():
            m = rx.search(chunk)
            if m:
                if key == "score":
                    row[key] = int(m.group(1))
                elif key == "budget_s":
                    row[key] = float(m.group(1))
                else:
                    row[key] = m.group(1)
        pm = _PREREQ_RE.search(chunk)
        row["prereq_goals"] = _STR_LIT_RE.findall(pm.group(1)) if pm else []
        row["stop_loss"] = bool(_STOP_LOSS_RE.search(chunk))
        if row.get("id"):
            row.setdefault("stage", "draft")
            goals.append(row)
    return goals


def _is_register_decorator(dec: ast.expr) -> bool:
    return (
        (isinstance(dec, ast.Name) and dec.id == "register")
        or (isinstance(dec, ast.Attribute) and dec.attr == "register")
    )


def _extract_class_str_attr(node: ast.ClassDef, attr: str) -> Optional[str]:
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            t = stmt.targets[0]
            if isinstance(t, ast.Name) and t.id == attr and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                return stmt.value.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == attr:
            if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                return stmt.value.value
    return None


def parse_skills_from_sources(file_contents: dict[str, str]) -> dict[str, Any]:
    """用 AST 静态抽取各模块中 @register 技能的 name 与 verified 等级（不执行模块代码）。"""
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for rel_path, src in sorted(file_contents.items()):
        if not src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(_is_register_decorator(d) for d in node.decorator_list):
                continue
            name = _extract_class_str_attr(node, "name")
            if not name or name in seen:
                continue
            seen.add(name)
            verified = _extract_class_str_attr(node, "verified") or "offline"
            items.append({
                "name": name,
                "verified": verified,
                "file": rel_path,
            })
    items.sort(key=lambda x: x["name"])
    live_n = sum(1 for x in items if x["verified"] == "live")
    return {
        "total": len(items),
        "live": live_n,
        "offline": len(items) - live_n,
        "items": items,
    }


def _read_env_snapshot(repo_root: Path) -> dict[str, str]:
    """读取 code/mcagent/.env 与进程环境，供展示非密钥配置与自检（严禁向前端透出密钥值）。"""
    vals: dict[str, str] = {}
    env_path = repo_root / "code" / "mcagent" / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    for k in (
        "MC_API_BASE", "MC_LOCAL_API_BASE", "MC_OFFICIAL_API_BASE",
        "MC_USER", "MC_PASS", "MC_OXYGEN_WARN",
        "LLM_PROFILE", "LLM_BASE", "LLM_KEY", "LLM_MODEL", "LLM_MAX_CONCURRENCY", "LLM_TOKEN_BUDGET",
        "LLM_CUSTOM_BASE", "LLM_CUSTOM_KEY", "LLM_CUSTOM_MODEL",
        "MCAGENT_TRAIN_ON_OFFICIAL",
    ):
        if os.environ.get(k):
            vals[k] = os.environ[k]
    return vals


def _probe_local_bridge(local_base: str) -> dict[str, Any]:
    """非阻塞检查本地联调网关 /healthz，仅用于控制台自检按钮。"""
    parts = urlsplit(local_base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return {"reachable": False, "url": local_base, "error": "本地网关地址无效"}
    health_url = f"{parts.scheme}://{parts.netloc}/healthz"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(health_url, timeout=0.6) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return {
            "reachable": True,
            "url": health_url,
            "mc_target": data.get("mc_target"),
            "mc_version": data.get("mc_version"),
            "active_bots": data.get("active_bots") or [],
        }
    except Exception as exc:
        return {"reachable": False, "url": health_url, "error": str(exc)}


def _fixer_group(key: str) -> str:
    if key.startswith("codex:"):
        return "Codex · " + soak_ctl.CODEX_FIXER_MODELS[key.split(":")[1]][0]
    if key.startswith("agy:"):
        for model, (title, _cli, _effs) in soak_ctl.AGY_FIXER_MODELS.items():
            if key.startswith(f"agy:{model}-"):
                return "agy · " + title
    return "简写 / 其他"


# ============================================================================
# 控制台核心：所有可变操作都在这里，HTTP 层只做路由与校验
# ============================================================================

class Console:
    def __init__(self, repo_root: Path, session_dir: Path):
        self.repo_root = repo_root
        self.session_dir = session_dir
        self._initial_session_dir = session_dir.resolve()
        self._lock = threading.Lock()
        self._child: Optional[subprocess.Popen] = None
        self._job: dict[str, Any] = {}
        self._ledger_cache: tuple[str, list[dict[str, Any]]] = ("", [])
        self._skills_cache: tuple[str, dict[str, Any]] = ("", {"total": 0, "live": 0, "offline": 0, "items": []})
        self._budget_feature_ct: Optional[float] = self._git_feature_time("EPISODE_SECONDS_BOUNDS")
        self._fixer_hot_ct: Optional[float] = self._git_feature_time("def resolve_fixer_template")

    # ---- git 辅助 ----
    def _git(self, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=str(self.repo_root), capture_output=True, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _git_feature_time(self, marker: str) -> Optional[float]:
        """某个热切换能力进入 soak_ctl.py 的提交时间；早于它启动的守护进程不认这个能力。"""
        out = self._git("log", "-1", "--format=%ct", "-S", marker, "--", "tools/soak-autofix/soak_ctl.py")
        return float(out) if out.isdigit() else None

    def _list_branches(self) -> list[str]:
        out = self._git("for-each-ref", "--format=%(refname:short)", "refs/heads")
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def _main_ahead_count(self, branch: str) -> Optional[int]:
        if not branch or branch == "main":
            return 0 if branch == "main" else None
        if not self._git("rev-parse", "--verify", "refs/heads/main") or not self._git("rev-parse", "--verify", branch):
            return None
        out = self._git("rev-list", "--count", f"{branch}..main")
        return int(out) if out.isdigit() else None

    def ledger(self, branch: str) -> list[dict[str, Any]]:
        tip = self._git("rev-parse", branch) if branch else ""
        key = tip or "worktree"
        if self._ledger_cache[0] == key:
            return self._ledger_cache[1]
        text = self._git("show", f"{tip}:code/mcagent/loop/goals.py") if tip else ""
        if not text:
            p = self.repo_root / "code" / "mcagent" / "loop" / "goals.py"
            text = p.read_text(encoding="utf-8") if p.exists() else ""
        goals = parse_ledger(text)
        self._ledger_cache = (key, goals)
        return goals

    def skills_summary(self, branch: str) -> dict[str, Any]:
        tip = self._git("rev-parse", branch) if branch else ""
        key = tip or "worktree"
        if self._skills_cache[0] == key:
            return self._skills_cache[1]
        sources: dict[str, str] = {}
        rel_paths: list[str] = ["code/mcagent/loop/starter_skills.py"]
        if tip:
            tree = self._git("ls-tree", "-r", "--name-only", tip, "code/mcagent/skills")
            for ln in tree.splitlines():
                ln = ln.strip()
                if ln.endswith(".py"):
                    rel_paths.append(ln)
            for rel in rel_paths:
                txt = self._git("show", f"{tip}:{rel}")
                if txt:
                    sources[rel] = txt
        else:
            skills_dir = self.repo_root / "code" / "mcagent" / "skills"
            if skills_dir.is_dir():
                for p in sorted(skills_dir.rglob("*.py")):
                    rel_paths.append(str(p.relative_to(self.repo_root)))
            for rel in rel_paths:
                p = self.repo_root / rel
                if p.is_file():
                    try:
                        sources[rel] = p.read_text(encoding="utf-8")
                    except OSError:
                        pass
        summary = parse_skills_from_sources(sources)
        self._skills_cache = (key, summary)
        return summary

    def _discover_sessions(self) -> list[dict[str, Any]]:
        logs_root = self.repo_root / "code" / "mcagent" / "logs"
        candidates: list[Path] = []
        seen: set[Path] = set()
        if logs_root.is_dir():
            for child in sorted(logs_root.iterdir()):
                if child.is_dir() and (child / "state.json").is_file():
                    rp = child.resolve()
                    candidates.append(rp)
                    seen.add(rp)
        cur = self.session_dir.resolve()
        if cur not in seen:
            candidates.insert(0, cur)
        out: list[dict[str, Any]] = []
        for d in candidates:
            st = read_state_nolock(d)
            out.append({
                "id": d.name,
                "path": str(d),
                "target_branch": st.get("target_branch") or "",
                "episodes": len(st.get("episodes") or []),
                "active": d == cur,
            })
        return out

    def switch_session(self, target_path: str) -> dict[str, Any]:
        raw = (target_path or "").strip()
        if not raw:
            raise ValueError("会话路径不能为空")
        cand = Path(raw).resolve()
        logs_root = (self.repo_root / "code" / "mcagent" / "logs").resolve()
        allowed = cand == self._initial_session_dir or (
            logs_root.is_dir() and cand.parent == logs_root and cand.is_dir()
        )
        if not allowed:
            raise ValueError("只能切换到 code/mcagent/logs 下的已知会话目录或初始会话")
        with self._lock:
            if self._job.get("status") == "running":
                raise ValueError("后台任务进行中，暂不能切换会话")
            self.session_dir = cand
        return self.snapshot()

    # ---- 读 ----
    def _reap_child(self) -> None:
        if self._child is not None and self._child.poll() is not None:
            self._child = None

    def last_start_params(self) -> dict[str, Any]:
        defaults = {
            "branch": "feat/soak-autofix",
            "episodes": 0,
            "episode_seconds": 240.0,
            "episode_steps": 45,
            "goal": "",
            "focus_mode": "together",
            "max_goal_attempts": 4,
            "llm": True,
            "player_model": soak_ctl.DEFAULT_PLAYER_MODEL,
            "fixer_preset": "codex",
            "skip_live_canary": False,
            "bot_user": soak_ctl.DEFAULT_BOT_USER,
            "rotate_user": False,
            "server": "official",
            "official_llm": "custom",
            "official_api_base": "",
            "min_verified": "",
            "account_reserved": False,
        }
        p = self.session_dir / LAST_START_NAME
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
            defaults.update({k: v for k, v in saved.items() if k in defaults})
        except (OSError, json.JSONDecodeError):
            pass
        defaults["account_reserved"] = False  # 占位确认每次启动都要重新勾，不记住
        return defaults

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._reap_child()
            job = dict(self._job)
            managed_pid = self._child.pid if self._child is not None else None
        st = read_state_nolock(self.session_dir)
        procs = find_soak_processes(self.session_dir)
        roles = {p["role"] for p in procs}
        stop_flag = (self.session_dir / "STOP").exists()
        if not procs:
            daemon = "stopped"
        elif stop_flag:
            daemon = "stopping"
        else:
            daemon = "running"

        def _started_after(role: str, ct: Optional[float]) -> Optional[bool]:
            pr = next((p for p in procs if p["role"] == role), None)
            if pr and pr.get("started_at") and ct:
                return pr["started_at"] >= ct
            return None

        hot_budget_ok = _started_after("runner-loop", self._budget_feature_ct)
        hot_fixer_ok = _started_after("fixer-loop", self._fixer_hot_ct)

        episodes = list(st.get("episodes") or [])
        live = None
        next_dir = self.session_dir / "episodes" / f"ep-{len(episodes) + 1:04d}"
        if next_dir.exists():
            live = summarize_live_episode(next_dir)
            if live is not None:
                live["running"] = bool("runner-loop" in roles and not live["finished"])

        branch = st.get("target_branch") or self.last_start_params()["branch"]
        telemetry_by_source: dict[str, dict[str, Any]] = {}
        telemetry_rows: list[dict[str, Any]] = []
        for tf in sorted(self.session_dir.glob("goal_telemetry.*.json")):
            try:
                tdata = json.loads(tf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            src = str(tdata.get("source") or tf.stem.removeprefix("goal_telemetry."))
            goals_obj = tdata.get("goals") or {}
            if isinstance(goals_obj, dict):
                telemetry_by_source[src] = goals_obj
                for gid, gstat in goals_obj.items():
                    if isinstance(gstat, dict):
                        telemetry_rows.append({"goal_id": str(gid), "source": src, **gstat})
        preferred_src = "official" if st.get("server_target") == "official" else "local-target"
        telemetry = (
            telemetry_by_source.get(preferred_src)
            or telemetry_by_source.get("local-target")
            or (next(iter(telemetry_by_source.values())) if telemetry_by_source else {})
        )

        env_cfg = _read_env_snapshot(self.repo_root)
        local_base = soak_ctl.local_mc_base(self.repo_root)
        official_base = ""
        official_base_error = ""
        try:
            official_base = soak_ctl.official_mc_base(self.repo_root)
        except ValueError as exc:
            official_base_error = str(exc)

        def _inc_summary(r: dict[str, Any]) -> dict[str, Any]:
            gh = r.get("gate_history") or []
            last_g = gh[-1] if gh else {}
            row = {k: r.get(k) for k in (
                "incident_id", "fingerprint", "code", "severity", "category", "skill", "goal_id",
                "status", "attempts", "last_seen_ep", "fix_commit", "last_fixer_preset", "revert_reason",
            )}
            row["editable_files"] = list(r.get("editable_files") or [])
            row["gate_count"] = len(gh)
            row["last_gate_ok"] = last_g.get("ok") if gh else None
            row["last_failed_gate"] = last_g.get("failed_gate") if gh else None
            return row

        tot_in = sum(int(e.get("prompt_tokens", 0) or 0) for e in episodes)
        tot_out = sum(int(e.get("completion_tokens", 0) or 0) for e in episodes)
        tot_elapsed = sum(float(e.get("elapsed_s", 0.0) or 0.0) for e in episodes)

        return {
            "now": time.time(),
            "session_dir": str(self.session_dir),
            "session_exists": bool(st),
            "sessions": self._discover_sessions(),
            "branches": self._list_branches(),
            "main_ahead_commits": self._main_ahead_count(branch),
            "daemon": daemon,
            "processes": procs,
            "managed_pid": managed_pid,
            "stop_flag": stop_flag,
            "hot_budget_supported": hot_budget_ok,
            "hot_fixer_supported": hot_fixer_ok,
            "state": {
                "target_branch": st.get("target_branch"),
                "base_commit": (st.get("base_commit") or "")[:7],
                "current_tip": (st.get("current_tip") or "")[:7],
                "target_goal": st.get("target_goal", ""),
                "player_model": st.get("player_model", ""),
                "fixer_preset": st.get("fixer_preset", ""),
                "focus_mode": st.get("focus_mode", "together"),
                "server_target": st.get("server_target", "local"),
                "server_base": st.get("server_base", ""),
                "official_llm": st.get("official_llm", "custom"),
                "min_verified": st.get("min_verified", ""),
                "max_goal_attempts": st.get("max_goal_attempts", 4),
                "episode_seconds": st.get("episode_seconds"),
                "episode_steps": st.get("episode_steps"),
                "commits": len(st.get("commits") or []),
                "total_prompt_tokens": tot_in,
                "total_completion_tokens": tot_out,
                "total_elapsed_s": round(tot_elapsed, 1),
            },
            "live": live,
            "episodes": [
                {k: e.get(k) for k in (
                    "ep_id", "at", "commit", "bot_user", "source", "elapsed_s", "stop_reason",
                    "skills_completed", "skills_env_blocked", "skills_budget_stopped",
                    "bugs_count", "p0_bugs", "server_score", "inferred_score", "deaths", "target_goal",
                    "target_goal_reached", "target_goal_elapsed_s", "goals_reached", "prompt_tokens",
                    "target_goals_reached", "environment_blocked_goals", "decision_sources",
                    "completion_tokens", "rolled_back", "revert_reason",
                )}
                for e in reversed(episodes[-60:])
            ],
            "incidents": [_inc_summary(r) for r in (st.get("incidents") or {}).values()],
            "telemetry": telemetry,
            "telemetry_rows": telemetry_rows,
            "ledger": self.ledger(branch),
            "skills_summary": self.skills_summary(branch),
            "start_defaults": self.last_start_params(),
            "fixer_presets": list(soak_ctl.FIXER_PRESETS),
            "fixer_catalog": [
                {"key": k, "label": soak_ctl.FIXER_PRESET_LABELS.get(k, k), "group": _fixer_group(k)}
                for k in soak_ctl.FIXER_PRESETS
            ],
            "player_models": [{"name": k, "note": v} for k, v in soak_ctl.PLAYER_MODEL_CHOICES.items()],
            "servers": [{"key": k, "label": v} for k, v in soak_ctl.SERVER_TARGETS.items()],
            "official_llm_sources": [{"key": k, "label": v} for k, v in soak_ctl.OFFICIAL_LLM_SOURCES.items()],
            "official_endpoints": [
                {
                    "key": "",
                    "label": f"沿用 .env（当前：{(urlsplit(official_base).hostname if official_base else '未配置/无效')}）",
                },
                {"key": soak_ctl.CAMPUS_MC_BASE, "label": "校园网直连 apiin.nmo.net.cn（校内 ≈5ms）"},
                {"key": soak_ctl.OFFICIAL_MC_BASE, "label": "公网隧道 mc-api.windywindie.top（任意网络 ≈1305ms）"},
            ],
            "official_base": official_base,
            "official_base_error": official_base_error,
            "local_base": local_base,
            "min_verified_choices": [
                {"key": k, "label": v} for k, v in soak_ctl.MIN_VERIFIED_CHOICES.items()
            ],
            "focus_modes": [{"key": k, "label": v} for k, v in soak_ctl.FOCUS_MODES.items()],
            "quota": {
                "input_tokens": soak_ctl.QUOTA_INPUT_TOKENS,
                "output_tokens": soak_ctl.QUOTA_OUTPUT_TOKENS,
                "match_duration_s": soak_ctl.MATCH_DURATION_S,
            },
            "env_summary": {
                "mc_api_host": urlsplit(env_cfg.get("MC_API_BASE") or "").hostname or "",
                "local_base": local_base,
                "official_base": official_base,
                "official_base_error": official_base_error,
                "llm_profile": (env_cfg.get("LLM_PROFILE") or "official").strip().lower(),
                "llm_model": env_cfg.get("LLM_MODEL") or "default",
                "llm_max_concurrency": env_cfg.get("LLM_MAX_CONCURRENCY") or "",
                "llm_token_budget": env_cfg.get("LLM_TOKEN_BUDGET") or "0",
                "mc_oxygen_warn": env_cfg.get("MC_OXYGEN_WARN") or "",
                "train_on_official_flag": env_cfg.get("MCAGENT_TRAIN_ON_OFFICIAL", "").strip() not in ("", "0"),
                "has_mc_account": bool(env_cfg.get("MC_USER") and env_cfg.get("MC_PASS")),
                "has_llm_key": bool(env_cfg.get("LLM_KEY")),
                "has_custom_llm": bool(env_cfg.get("LLM_CUSTOM_BASE") and env_cfg.get("LLM_CUSTOM_KEY")),
            },
            # 比赛服前置检查（占位确认另算），页面据此提前提示；只给「缺什么」，不含任何值
            "official_checks": {
                "custom": soak_ctl.check_server_target(self.repo_root, "official", use_llm=True,
                                                       account_reserved=True, official_llm="custom"),
                "official": soak_ctl.check_server_target(self.repo_root, "official", use_llm=True,
                                                         account_reserved=True, official_llm="official"),
                "rules_only": soak_ctl.check_server_target(self.repo_root, "official", use_llm=False, account_reserved=True),
            },
            "bounds": {
                "seconds": list(soak_ctl.EPISODE_SECONDS_BOUNDS),
                "steps": list(soak_ctl.EPISODE_STEPS_BOUNDS),
            },
            "job": job,
        }

    def episode_detail(self, ep_id: str) -> dict[str, Any]:
        if not EP_ID_RE.match(ep_id or ""):
            raise ValueError("非法轮次 id")
        ep_dir = self.session_dir / "episodes" / ep_id
        if not ep_dir.is_dir():
            raise ValueError(f"轮次 {ep_id} 不存在")
        summary = ep_dir / "summary.md"
        inc_dir = ep_dir / "incidents"
        return {
            "ep_id": ep_id,
            "summary_md": summary.read_text(encoding="utf-8") if summary.exists() else "（本轮还没有 summary.md，可能仍在运行）",
            "incidents": sorted(p.name for p in inc_dir.glob("*.json")) if inc_dir.is_dir() else [],
            "live": summarize_live_episode(ep_dir),
        }

    def incident_detail(self, fp: str) -> dict[str, Any]:
        fp = (fp or "").strip()
        if not fp:
            raise ValueError("缺少异常指纹")
        st = read_state_nolock(self.session_dir)
        rec = (st.get("incidents") or {}).get(fp)
        if not isinstance(rec, dict):
            raise ValueError(f"未找到异常单 {fp}")
        sess_root = self.session_dir.resolve()
        inc_json: dict[str, Any] = {}
        inc_path_raw = rec.get("incident_path")
        if inc_path_raw:
            try:
                p = Path(str(inc_path_raw)).resolve()
                if sess_root in p.parents and p.is_file():
                    inc_json = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        prompt_preview = ""
        prompt_raw = rec.get("prompt_file")
        if prompt_raw:
            try:
                pp = Path(str(prompt_raw)).resolve()
                if sess_root in pp.parents and pp.is_file():
                    prompt_preview = pp.read_text(encoding="utf-8", errors="replace")[:8000]
            except OSError:
                pass
        return {
            "record": {
                k: rec.get(k) for k in (
                    "incident_id", "fingerprint", "code", "severity", "category", "skill", "goal_id",
                    "editable_files", "status", "attempts", "first_seen_ep", "last_seen_ep",
                    "fix_commit", "last_failed_fix_commit", "last_fixer_preset", "revert_reason",
                    "goal_achieved_ep", "gate_history",
                )
            },
            "title": inc_json.get("title") or "",
            "detail": inc_json.get("detail") or inc_json.get("last_detail") or {},
            "skill_context": {
                k: (inc_json.get("skill_context") or {}).get(k)
                for k in ("skill", "args", "outcome")
            } if inc_json.get("skill_context") else {},
            "http_tape_count": len(inc_json.get("http_tape") or []),
            "prompt_preview": prompt_preview,
        }

    def probe_env(self) -> dict[str, Any]:
        local_base = soak_ctl.local_mc_base(self.repo_root)
        bridge = _probe_local_bridge(local_base)
        env = _read_env_snapshot(self.repo_root)
        base = env.get("MC_API_BASE", "")
        host = (urlsplit(base).hostname or "").lower() if base else ""
        profile = (env.get("LLM_PROFILE") or "official").strip().lower()
        try:
            conc = int(env.get("LLM_MAX_CONCURRENCY") or "")
        except ValueError:
            conc = -1
        checks = [
            {"name": "MC_API_BASE 非本机", "ok": bool(base) and host not in ("127.0.0.1", "localhost", "0.0.0.0"),
             "detail": host or "(空)"},
            {"name": "MC_API_BASE 为官方入口", "ok": host in soak_ctl.OFFICIAL_MC_HOSTS,
             "detail": host or "(空)"},
            {"name": "正式账号已填", "ok": bool(env.get("MC_USER") and env.get("MC_PASS")),
             "detail": "已设置" if (env.get("MC_USER") and env.get("MC_PASS")) else "未设置"},
            {"name": "AIA 官方 LLM_KEY 已填", "ok": bool(env.get("LLM_KEY")) and not env.get("LLM_KEY", "").startswith("sk-mRbe"),
             "detail": "已设置" if env.get("LLM_KEY") else "未设置"},
            {"name": "LLM_PROFILE == official", "ok": profile == "official", "detail": profile},
            {"name": "LLM_MODEL == default", "ok": (env.get("LLM_MODEL") or "default") == "default",
             "detail": env.get("LLM_MODEL") or "default"},
            {"name": "LLM_MAX_CONCURRENCY <= 2", "ok": 0 < conc <= 2,
             "detail": env.get("LLM_MAX_CONCURRENCY") or "(未设)"},
            {"name": "未开 MCAGENT_TRAIN_ON_OFFICIAL", "ok": env.get("MCAGENT_TRAIN_ON_OFFICIAL", "").strip() in ("", "0"),
             "detail": env.get("MCAGENT_TRAIN_ON_OFFICIAL") or "(未设)"},
            {"name": "本地联调网关已停", "ok": not bridge["reachable"],
             "detail": "监听中" if bridge["reachable"] else "未运行"},
        ]
        return {"bridge": bridge, "preflight_checks": checks}

    def log_tail(self, lines: int = 200) -> str:
        p = self.session_dir / RUN_LOG_NAME
        if not p.exists():
            return ""
        try:
            data = p.read_bytes()[-256 * 1024:].decode("utf-8", "replace")
        except OSError:
            return ""
        return "\n".join(data.splitlines()[-max(1, min(lines, 2000)):])

    # ---- 写：实时参数（下一轮起生效） ----
    def apply_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not (self.session_dir / "state.json").exists():
            raise ValueError("会话还没初始化：先一键启动一次双工位")
        changes: dict[str, Any] = {}
        clear = bool(payload.get("clear_budget"))
        sec = payload.get("episode_seconds")
        steps = payload.get("episode_steps")
        sec = None if sec in (None, "") else float(sec)
        steps = None if steps in (None, "") else int(steps)
        if clear and (sec is not None or steps is not None):
            raise ValueError("清除覆盖与设置新值不能同时进行")
        err = soak_ctl.validate_episode_budget(sec, steps)
        if err:
            raise ValueError(err)
        if "target_goal" in payload:
            st_now = read_state_nolock(self.session_dir)
            changes["target_goal"] = self._validate_goals(
                payload.get("target_goal"), st_now.get("target_branch") or ""
            )
        if payload.get("focus_mode") not in (None, ""):
            mode = str(payload["focus_mode"])
            if mode not in soak_ctl.FOCUS_MODES:
                raise ValueError(f"未知聚焦模式 {mode!r}")
            changes["focus_mode"] = mode
        if "player_model" in payload and payload["player_model"] is not None:
            changes["player_model"] = str(payload["player_model"]).strip()
        if payload.get("fixer_preset") not in (None, ""):
            fx = str(payload["fixer_preset"]).strip()
            if fx not in soak_ctl.FIXER_PRESETS:
                raise ValueError(f"未知修单模型 {fx!r}")
            changes["fixer_preset"] = fx
        if payload.get("max_goal_attempts") not in (None, ""):
            n = int(payload["max_goal_attempts"])
            if not 1 <= n <= 20:
                raise ValueError("最大尝试轮数需在 1–20")
            changes["max_goal_attempts"] = n
        clear_min_ver = False
        if "min_verified" in payload and payload["min_verified"] is not None:
            mv = str(payload["min_verified"]).strip()
            if mv not in soak_ctl.MIN_VERIFIED_CHOICES:
                raise ValueError(f"未知验证门槛 {mv!r}")
            if mv:
                changes["min_verified"] = mv
            else:
                clear_min_ver = True
        with soak_ctl.locked_state(self.session_dir) as st:
            if clear:
                st.pop("episode_seconds", None)
                st.pop("episode_steps", None)
            if clear_min_ver:
                st.pop("min_verified", None)
            if sec is not None:
                st["episode_seconds"] = sec
            if steps is not None:
                st["episode_steps"] = steps
            st.update(changes)
            result = {
                "episode_seconds": st.get("episode_seconds"),
                "episode_steps": st.get("episode_steps"),
                "target_goal": st.get("target_goal", ""),
                "player_model": st.get("player_model", ""),
                "fixer_preset": st.get("fixer_preset", ""),
                "focus_mode": st.get("focus_mode", "together"),
                "min_verified": st.get("min_verified", ""),
                "max_goal_attempts": st.get("max_goal_attempts", 4),
            }
        try:
            soak_ctl.write_review_report(self.session_dir)
        except Exception:  # 报告只是展示，失败不影响设置生效
            pass
        return result

    def _validate_goals(self, goals: Any, branch: str) -> str:
        """聚焦目标：接受列表或逗号串，逐个核对台账，返回规整后的逗号串（空串 = 不聚焦）。"""
        ids = soak_ctl.split_goal_ids(goals)
        known = {g["id"] for g in self.ledger(branch)}
        unknown = [gid for gid in ids if known and gid not in known]
        if unknown:
            raise ValueError(f"台账里没有目标 {', '.join(unknown)}")
        return ",".join(ids)

    # ---- 写：启停 ----
    def _validate_start(self, params: dict[str, Any]) -> dict[str, Any]:
        p = self.last_start_params()
        p.update({k: v for k, v in params.items() if k in p})
        p["branch"] = soak_ctl.validate_target_branch(str(p["branch"]).strip())
        p["episodes"] = int(p["episodes"])
        if p["episodes"] < 0:
            raise ValueError("轮数不能为负（0 表示一直跑）")
        p["episode_seconds"] = float(p["episode_seconds"])
        p["episode_steps"] = int(p["episode_steps"])
        err = soak_ctl.validate_episode_budget(p["episode_seconds"], p["episode_steps"])
        if err:
            raise ValueError(err)
        p["max_goal_attempts"] = int(p["max_goal_attempts"])
        if not 1 <= p["max_goal_attempts"] <= 20:
            raise ValueError("最大尝试轮数需在 1–20")
        if p["fixer_preset"] not in soak_ctl.FIXER_PRESETS:
            raise ValueError(f"未知修单预设 {p['fixer_preset']!r}")
        p["goal"] = self._validate_goals(p["goal"], p["branch"])
        p["focus_mode"] = str(p["focus_mode"] or "together")
        if p["focus_mode"] not in soak_ctl.FOCUS_MODES:
            raise ValueError(f"未知聚焦模式 {p['focus_mode']!r}")
        p["player_model"] = str(p["player_model"] or "").strip()
        p["llm"] = bool(p["llm"])
        p["skip_live_canary"] = bool(p["skip_live_canary"])
        p["bot_user"] = soak_ctl.validate_bot_user(str(p["bot_user"]))
        p["rotate_user"] = bool(p["rotate_user"])
        p["server"] = str(p["server"] or "official")
        p["official_llm"] = str(p["official_llm"] or "custom")
        p["official_api_base"] = str(p.get("official_api_base") or "").strip()
        if p["official_api_base"]:
            parts = urlsplit(p["official_api_base"])
            if parts.scheme != "https" or parts.hostname not in soak_ctl.OFFICIAL_MC_HOSTS:
                raise ValueError(
                    f"official_api_base={p['official_api_base']!r} 不是官方入口"
                )
        p["min_verified"] = str(p.get("min_verified") or "").strip()
        if p["min_verified"] not in soak_ctl.MIN_VERIFIED_CHOICES:
            raise ValueError(f"未知验证门槛 {p['min_verified']!r}")
        p["account_reserved"] = bool(p["account_reserved"])
        errs = soak_ctl.check_server_target(
            self.repo_root, p["server"], use_llm=p["llm"], account_reserved=p["account_reserved"],
            official_llm=p["official_llm"],
        )
        if p["official_api_base"]:
            errs = [e for e in errs if not e.startswith("MC_OFFICIAL_API_BASE=")]
        if errs:
            raise ValueError("；".join(errs))
        return p

    def build_run_cmd(self, p: dict[str, Any]) -> list[str]:
        cmd = [
            sys.executable, str(SOAK_CTL_PATH), "run",
            "--session-dir", str(self.session_dir),
            "--branch", p["branch"],
            "--episodes", str(p["episodes"]),
            "--episode-seconds", str(p["episode_seconds"]),
            "--episode-steps", str(p["episode_steps"]),
            "--max-goal-attempts", str(p["max_goal_attempts"]),
            "--player-model", p["player_model"],
            "--fixer-preset", p["fixer_preset"],
            "--bot-user", p["bot_user"],
            "--llm" if p["llm"] else "--no-llm",
        ]
        if p["rotate_user"]:
            cmd.append("--rotate-user")
        cmd.extend(["--server", p["server"], "--official-llm", p["official_llm"]])
        if p["server"] == "official" and p["account_reserved"]:
            cmd.append("--account-reserved")
        if p["goal"]:
            cmd.extend(["--goal", p["goal"]])
        cmd.extend(["--focus-mode", p["focus_mode"]])
        if p.get("min_verified") in ("offline", "live"):
            cmd.extend(["--min-verified", p["min_verified"]])
        if p["skip_live_canary"]:
            cmd.append("--skip-live-canary")
        return cmd

    def start(self, params: dict[str, Any]) -> dict[str, Any]:
        p = self._validate_start(params)
        with self._lock:
            self._reap_child()
            procs = find_soak_processes(self.session_dir)
            if procs:
                raise ValueError(f"该会话已有守护在跑（pid {[x['pid'] for x in procs]}），先停止再启动")
            return self._spawn_locked(p)

    def _spawn_locked(self, p: dict[str, Any]) -> dict[str, Any]:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        saved = dict(p, account_reserved=False)
        (self.session_dir / LAST_START_NAME).write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        cmd = self.build_run_cmd(p)
        log = (self.session_dir / RUN_LOG_NAME).open("ab")
        log.write(f"\n===== [dashboard] {time.strftime('%Y-%m-%d %H:%M:%S')} 启动: {' '.join(cmd)}\n".encode("utf-8"))
        log.flush()
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        if p.get("official_api_base"):
            env["MC_OFFICIAL_API_BASE"] = p["official_api_base"]
        self._child = subprocess.Popen(
            cmd, cwd=str(self.repo_root), stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env, start_new_session=True,
        )
        log.close()
        return {"pid": self._child.pid, "cmd": cmd}

    def request_stop(self) -> dict[str, Any]:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / "STOP").write_text(soak_ctl._utc_now(), encoding="utf-8")
        return {"stop_flag": True}

    def interrupt(self) -> dict[str, Any]:
        """等同在终端按 Ctrl+C：run 进程会写 STOP 并把 SIGINT 转给两个工位。"""
        procs = find_soak_processes(self.session_dir)
        if not procs:
            raise ValueError("没有在跑的守护")
        run_procs = [p for p in procs if p["role"] == "run"]
        targets = run_procs or procs
        sent = []
        for pr in targets:
            try:
                os.kill(pr["pid"], signal.SIGINT)
                sent.append(pr["pid"])
            except OSError:
                pass
        return {"signaled": sent}

    # ---- 后台任务：重启、热合入 ----
    def _run_job(self, kind: str, fn) -> dict[str, Any]:
        with self._lock:
            if self._job.get("status") == "running":
                raise ValueError(f"已有后台任务在跑：{self._job.get('kind')}")
            self._job = {"kind": kind, "status": "running", "started_at": time.time(), "detail": ""}

        def _target():
            try:
                res = fn()
                status = "ok" if not (isinstance(res, dict) and res.get("ok") is False) else "failed"
                detail = res
            except Exception as exc:  # 后台任务的异常要显示给页面
                status, detail = "failed", f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._job.update(status=status, detail=detail, finished_at=time.time())

        threading.Thread(target=_target, daemon=True).start()
        return dict(self._job)

    def restart(self, params: dict[str, Any], wait_timeout_s: float = 3600.0) -> dict[str, Any]:
        p = self._validate_start(params)

        def _job():
            self.request_stop()
            deadline = time.time() + wait_timeout_s
            while find_soak_processes(self.session_dir):
                if time.time() > deadline:
                    raise TimeoutError("等待旧守护退出超时（当前轮或修单仍在进行）")
                with self._lock:
                    self._reap_child()
                time.sleep(2.0)
            with self._lock:
                self._reap_child()
                return self._spawn_locked(p)

        return self._run_job("restart", _job)

    def inject(self, merge_ref: str, goal: Any) -> dict[str, Any]:
        ref = (merge_ref or "").strip()
        if not ref or not re.match(r"^[A-Za-z0-9._/\-]+$", ref):
            raise ValueError("请填写合法的分支名或 commit")
        if not (self.session_dir / "state.json").exists():
            raise ValueError("会话还没初始化：先一键启动一次双工位")
        st_now = read_state_nolock(self.session_dir)
        tgt = None if goal is None else self._validate_goals(goal, st_now.get("target_branch") or "")
        return self._run_job(
            f"inject {ref}",
            lambda: soak_ctl.inject_and_set_goal(self.session_dir, merge_ref=ref, target_goal=tgt),
        )


# ============================================================================
# HTTP 层
# ============================================================================

def make_handler(console: Console, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "SoakDashboard/1.0"

        def log_message(self, fmt, *args):  # 轮询很频繁，不刷终端
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "host not allowed"})
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            try:
                if url.path == "/":
                    return self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
                if url.path == "/api/snapshot":
                    return self._json(200, console.snapshot())
                if url.path == "/api/episode":
                    return self._json(200, console.episode_detail((qs.get("id") or [""])[0]))
                if url.path == "/api/incident":
                    return self._json(200, console.incident_detail((qs.get("fp") or [""])[0]))
                if url.path == "/api/ledger":
                    br = (qs.get("branch") or [""])[0].strip()
                    if br and not re.match(r"^[A-Za-z0-9._/\-]+$", br):
                        raise ValueError("非法分支名")
                    return self._json(200, {"branch": br, "ledger": console.ledger(br),
                                            "skills_summary": console.skills_summary(br)})
                if url.path == "/api/probe_env":
                    return self._json(200, console.probe_env())
                if url.path == "/api/log":
                    n = int((qs.get("lines") or ["200"])[0])
                    return self._json(200, {"log": console.log_tail(n)})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._json(403, {"error": "host not allowed"})
            origin = self.headers.get("Origin")
            if origin and origin not in allowed_origins:
                return self._json(403, {"error": "origin not allowed"})
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json(415, {"error": "需要 application/json"})
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("请求体必须是 JSON 对象")
                path = urlparse(self.path).path
                if path == "/api/settings":
                    return self._json(200, {"ok": True, "result": console.apply_settings(payload)})
                if path == "/api/session":
                    return self._json(200, {"ok": True, "snapshot": console.switch_session(str(payload.get("path") or ""))})
                if path == "/api/start":
                    return self._json(200, {"ok": True, "result": console.start(payload)})
                if path == "/api/stop":
                    return self._json(200, {"ok": True, "result": console.request_stop()})
                if path == "/api/interrupt":
                    return self._json(200, {"ok": True, "result": console.interrupt()})
                if path == "/api/restart":
                    return self._json(200, {"ok": True, "result": console.restart(payload)})
                if path == "/api/inject":
                    return self._json(200, {"ok": True, "result": console.inject(
                        str(payload.get("merge_ref") or ""), payload.get("goal"))})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            return self._json(404, {"ok": False, "error": "not found"})

    return Handler


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="双工位挂机本地控制台（只监听 127.0.0.1）")
    ap.add_argument("--session-dir", default="code/mcagent/logs/soak-cont",
                    help="会话目录；相对路径按仓库根解析")
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args(argv)

    repo_root = soak_ctl.find_repo_root(HERE)
    session_dir = Path(args.session_dir)
    if not session_dir.is_absolute():
        session_dir = repo_root / session_dir
    console = Console(repo_root, session_dir.resolve())
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(console, args.port))
    print(f"[dashboard] 会话 {console.session_dir}")
    print(f"[dashboard] 打开 http://127.0.0.1:{args.port}/  （Ctrl+C 退出控制台；不会停掉已启动的双工位）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
