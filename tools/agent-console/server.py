#!/usr/bin/env python3
"""AIAMC Harness 可视化监控与双模式控制台后端（零第三方依赖，仅监听 127.0.0.1）。

支持两种严格隔离的运行模式：
  - 比赛模式（match）：锁定官方 NMO 端点（校园网直连 / 公网备用）+ 官方 AIA 代理（LLM_PROFILE=official,
    LLM_MODEL=default, LLM_MAX_CONCURRENCY<=2）+ 12 项 preflight 门禁 + 正式角色占位确认。
  - 日常自搭服测试模式（local）：锁定本地/局域网联调网关（如 127.0.0.1:8787），支持自定义隔离测试角色、
    自备 LLM（custom）或纯规则基线、聚焦单目标/单技能与快捷压测预设，严禁误连官方端点。

用法（仓库根目录）：
    python3 tools/agent-console/server.py
    python3 tools/agent-console/server.py --port 8795
"""
from __future__ import annotations

import argparse
import collections
import datetime
import importlib.util
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse, urlsplit

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
CODE_DIR = REPO_ROOT / "code"
MCAGENT_DIR = CODE_DIR / "mcagent"
LOGS_ROOT = MCAGENT_DIR / "logs"
RESULTS_ROOT = REPO_ROOT / "results"
WEB_DIR = HERE / "web"


def _ensure_venv_python() -> None:
    """若用系统 python3 直接启动且缺少 requests，则自动切到仓库 .venv 解释器。"""
    if os.environ.get("_AIAMC_CONSOLE_REEXEC") == "1":
        return
    if importlib.util.find_spec("requests") is not None:
        return
    for cand in (REPO_ROOT / ".venv" / "bin" / "python", REPO_ROOT / ".venv" / "bin" / "python3"):
        if cand.is_file():
            os.environ["_AIAMC_CONSOLE_REEXEC"] = "1"
            os.execv(str(cand), [str(cand), *sys.argv])


_ensure_venv_python()

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from mcagent import preflight as mc_preflight  # noqa: E402
from mcagent.loop.goals import GOAL_MAP, GOALS  # noqa: E402
from mcagent.skills import REGISTRY  # noqa: E402

OFFICIAL_CAMPUS_API = "https://apiin.nmo.net.cn/api/v1"
OFFICIAL_PUBLIC_API = "https://mc-api.windywindie.top/api/v1"
DEFAULT_LOCAL_API = "http://127.0.0.1:8787/api/v1"
DEFAULT_PORT = 8795

QUOTA_INPUT_TOKENS = 8_000_000
QUOTA_OUTPUT_TOKENS = 1_500_000
MATCH_DURATION_S = 8 * 3600

MAX_TIMELINE_BYTES = 8 * 1024 * 1024
MAX_CONSOLE_LINES = 600
MAX_TRAJECTORY_POINTS = 1400
MAX_EVENTS_RETURN = 260

_SECRET_KEYS = frozenset({
    "MC_PASS",
    "LLM_KEY",
    "LLM_CUSTOM_KEY",
})

_SAFE_ENV_KEYS = (
    "MC_API_BASE",
    "MC_LOCAL_API_BASE",
    "MC_OFFICIAL_API_BASE",
    "MC_USER",
    "MC_PROXY",
    "LLM_BASE",
    "LLM_MODEL",
    "LLM_PROFILE",
    "LLM_MAX_CONCURRENCY",
    "LLM_PROXY_FAST",
    "LLM_TOKEN_BUDGET",
    "LLM_CUSTOM_BASE",
    "LLM_CUSTOM_MODEL",
    "LLM_CUSTOM_PROXY",
    "MCAGENT_TRAIN_ON_OFFICIAL",
)

_SAFE_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_STEP_GOAL_RE = re.compile(r"推进目标\s+([a-z0-9_]+)")
_OBJ_GOAL_RE = re.compile(r"\[目标:([a-z0-9_]+)\]")


# ============================================================================
# 环境脱敏读取与安全辅助
# ============================================================================

def _git_info() -> dict[str, Any]:
    info: dict[str, Any] = {"commit": "unknown", "branch": "unknown", "dirty": None}
    try:
        info["commit"] = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO_ROOT), stderr=subprocess.DEVNULL, text=True
        ).strip()
        info["branch"] = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(REPO_ROOT), stderr=subprocess.DEVNULL, text=True
        ).strip()
        st = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=str(REPO_ROOT), stderr=subprocess.DEVNULL, text=True
        )
        info["dirty"] = bool(st.strip())
    except Exception:
        pass
    return info


def read_env_summary(env_path: Path) -> dict[str, Any]:
    """读取 .env 文件的脱敏摘要：严禁返回任何密码或 API Key 明文。"""
    exists = env_path.is_file()
    raw = mc_preflight.load_env(str(env_path)) if exists else {}
    safe_values = {k: raw.get(k, "") for k in _SAFE_ENV_KEYS}
    llm_key = raw.get("LLM_KEY", "")
    mc_pass = raw.get("MC_PASS", "")
    custom_key = raw.get("LLM_CUSTOM_KEY", "")
    return {
        "exists": exists,
        "path": str(env_path.relative_to(REPO_ROOT)),
        "values": safe_values,
        "secrets": {
            "has_mc_pass": bool(mc_pass) and mc_pass not in mc_preflight.PLACEHOLDER_PASSES,
            "mc_pass_is_localdev": mc_pass == "localdev",
            "has_llm_key": bool(llm_key),
            "llm_key_is_test_prefix": llm_key.startswith(mc_preflight.TEST_KEY_PREFIX) if llm_key else False,
            "has_llm_custom_key": bool(custom_key),
        },
    }


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _resolve_python_runner() -> list[str]:
    """优先直启仓库 .venv/bin/python，避免 uv 包装层吞掉 SIGINT 或强杀后遗留子进程。"""
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    if venv_py.is_file() and os.access(venv_py, os.X_OK):
        return [str(venv_py)]
    if shutil.which("uv"):
        return ["uv", "run", "python"]
    return [sys.executable]


# ============================================================================
# 双模式启动参数与环境构造（严格隔离 Match vs Local）
# ============================================================================

def build_launch_spec(
    payload: dict[str, Any],
    *,
    env_dev_path: Path = MCAGENT_DIR / ".env",
    env_match_path: Path = MCAGENT_DIR / ".env.match",
    run_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """根据前端提交的表单构造严格合规的 `mcagent.loop` 启动命令与子进程环境变量。

    返回 dict 包含：
      - `mode`: "match" | "local"
      - `argv`: list[str]
      - `env_overrides`: dict[str, str]（包含子进程运行所需凭据，绝不直接返给前端）
      - `cmd_display`: str（脱敏后的可读命令行）
      - `run_dir`: Path
      - `meta`: dict[str, Any]（脱敏后的运行元数据）
    """
    mode = str(payload.get("mode") or "local").strip().lower()
    if mode not in ("match", "local"):
        raise ValueError(f"不支持的模式: {mode!r}（仅允许 match / local）")

    raw_dev = mc_preflight.load_env(str(env_dev_path)) if env_dev_path.is_file() else {}
    raw_match = mc_preflight.load_env(str(env_match_path)) if env_match_path.is_file() else {}

    strategy = str(payload.get("strategy") or "llm").strip().lower()
    if strategy not in ("llm", "rule"):
        raise ValueError(f"不支持的策略: {strategy!r}（仅允许 llm / rule）")

    min_verified = str(payload.get("min_verified") or ("offline" if mode == "local" else "offline")).strip().lower()
    if min_verified not in ("offline", "live"):
        raise ValueError(f"非法的 min_verified: {min_verified!r}")

    include_draft = bool(payload.get("include_draft", True if mode == "local" else False))
    disconnect_on_exit = bool(payload.get("disconnect_on_exit", mode == "local"))
    stop_on_death = bool(payload.get("stop_on_death", False))
    no_stop_on_goal = bool(payload.get("no_stop_on_goal", False))
    use_checkpoint = bool(payload.get("use_checkpoint", True))

    try:
        max_seconds = float(payload.get("max_seconds") if payload.get("max_seconds") is not None else (28800 if mode == "match" else 240))
    except (TypeError, ValueError) as exc:
        raise ValueError("max_seconds 必须是数字") from exc
    if max_seconds < 0 or max_seconds > 86400:
        raise ValueError("max_seconds 必须在 0..86400 秒之间")

    try:
        max_steps = int(payload.get("max_steps") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_steps 必须是整数") from exc
    if max_steps < 0 or max_steps > 200000:
        raise ValueError("max_steps 必须在 0..200000 之间")

    goal_raw = str(payload.get("goal") or "").strip()
    if goal_raw:
        goal_ids = [g.strip() for g in goal_raw.split(",") if g.strip()]
        bad_goals = [g for g in goal_ids if g not in GOAL_MAP]
        if bad_goals:
            raise ValueError(f"未知目标 ID: {', '.join(bad_goals)}")
        goal_clean = ",".join(goal_ids)
    else:
        goal_clean = ""

    run_mode = str(payload.get("run_mode") or "campaign").strip().lower()
    if run_mode not in ("campaign", "focus"):
        raise ValueError("run_mode 仅支持 campaign 或 focus")
    focus_skill = str(payload.get("focus_skill") or "").strip()
    focus_args_raw = str(payload.get("focus_args") or "{}").strip() or "{}"
    if run_mode == "focus":
        if focus_skill not in REGISTRY:
            raise ValueError(f"focus 模式需要已注册的技能，收到 {focus_skill!r}")
        try:
            parsed_args = json.loads(focus_args_raw)
            if not isinstance(parsed_args, dict):
                raise ValueError
        except ValueError as exc:
            raise ValueError("focus_args 必须是合法 JSON 对象") from exc
        focus_args_clean = json.dumps(parsed_args, ensure_ascii=False, separators=(",", ":"))
    else:
        focus_args_clean = "{}"

    stamp = time.strftime("%Y%m%d-%H%M%S")
    target_run_dir = run_dir or (LOGS_ROOT / f"console-{mode}-{stamp}")

    env_overrides: dict[str, str] = {}
    argv = [*_resolve_python_runner(), "-m", "mcagent.loop", "--soak", "--run-dir", str(target_run_dir)]

    if mode == "match":
        if not bool(payload.get("account_reserved", False)):
            raise ValueError("比赛模式启动前必须勾选确认已在 docs/05 账号表占位（防止踢掉队友）")

        base_env = {**raw_dev, **raw_match}
        api_base = str(payload.get("api_base") or base_env.get("MC_API_BASE") or OFFICIAL_CAMPUS_API).strip()
        host = _host_of(api_base)
        if host not in mc_preflight.OFFICIAL_HOSTS:
            raise ValueError(
                f"比赛模式仅允许官方端点（{', '.join(mc_preflight.OFFICIAL_HOSTS)}），拒绝 {api_base!r}"
            )

        mc_user = str(base_env.get("MC_USER") or "").strip()
        mc_pass = str(base_env.get("MC_PASS") or "").strip()
        if mc_user in mc_preflight.PLACEHOLDER_USERS or mc_pass in mc_preflight.PLACEHOLDER_PASSES:
            raise ValueError("比赛模式缺少有效的正式皮肤站账号或密码（请先配置 code/mcagent/.env.match）")

        llm_key = str(base_env.get("LLM_KEY") or "").strip()
        if strategy == "llm":
            if not llm_key or llm_key.startswith(mc_preflight.TEST_KEY_PREFIX):
                raise ValueError("比赛模式开启 LLM 时必须配置队伍正式 AIA LLM_KEY（禁止使用 sk-mRbe 压测 key）")

        try:
            conc = int(payload.get("llm_max_concurrency") or base_env.get("LLM_MAX_CONCURRENCY") or 2)
        except (TypeError, ValueError):
            conc = 2
        conc = max(1, min(2, conc))

        mc_proxy = "" if host in mc_preflight.CAMPUS_DIRECT_HOSTS else str(base_env.get("MC_PROXY") or "").strip()
        token_budget = str(payload.get("llm_token_budget") or base_env.get("LLM_TOKEN_BUDGET") or "7500000").strip()

        env_overrides.update({
            "MC_API_BASE": api_base,
            "MC_USER": mc_user,
            "MC_PASS": mc_pass,
            "MC_PROXY": mc_proxy,
            "LLM_PROFILE": "official",
            "LLM_BASE": str(base_env.get("LLM_BASE") or "https://api.nju-aia.com/v1").strip(),
            "LLM_KEY": llm_key,
            "LLM_MODEL": "default",
            "LLM_MAX_CONCURRENCY": str(conc),
            "LLM_TOKEN_BUDGET": token_budget,
            "MCAGENT_TRAIN_ON_OFFICIAL": "",
        })
        if base_env.get("LLM_PROXY_FAST"):
            env_overrides["LLM_PROXY_FAST"] = str(base_env["LLM_PROXY_FAST"]).strip()

        argv.append("--live")
        if strategy == "llm":
            argv.extend(["--llm", "--strategy", "llm"])
        else:
            argv.extend(["--strategy", "rule"])

        effective_user = mc_user
        effective_llm_profile = "official" if strategy == "llm" else "none"
        effective_llm_model = "default" if strategy == "llm" else "rule-only"
    else:
        api_base = str(
            payload.get("api_base")
            or raw_dev.get("MC_LOCAL_API_BASE")
            or DEFAULT_LOCAL_API
        ).strip()
        host = _host_of(api_base)
        if not api_base or host in mc_preflight.OFFICIAL_HOSTS:
            raise ValueError("日常自搭服测试模式禁止填写官方比赛服端点，请切到「比赛模式」")

        bot_user = str(payload.get("bot_user") or "dev_console_bot").strip()
        if not _SAFE_USER_RE.match(bot_user):
            raise ValueError("测试角色名仅允许 1–32 位字母、数字、下划线、点或连字符")

        llm_profile = str(payload.get("llm_profile") or ("custom" if strategy == "llm" else "none")).strip().lower()
        if strategy == "rule":
            llm_profile = "none"
        if llm_profile not in ("custom", "official", "none"):
            raise ValueError(f"非法的 llm_profile: {llm_profile!r}")

        env_overrides.update({
            "MC_API_BASE": api_base,
            "MC_PROXY": "",
            "MCAGENT_TRAIN_ON_OFFICIAL": "",
        })

        argv.extend(["--local-target", "--user", bot_user])

        effective_llm_model = "rule-only"
        if llm_profile == "none":
            argv.extend(["--strategy", "rule"])
        elif llm_profile == "custom":
            custom_base = str(payload.get("llm_custom_base") or raw_dev.get("LLM_CUSTOM_BASE") or "").strip()
            custom_key = str(raw_dev.get("LLM_CUSTOM_KEY") or "").strip()
            custom_model = str(payload.get("llm_custom_model") or raw_dev.get("LLM_CUSTOM_MODEL") or "").strip()
            if not (custom_base and custom_key and custom_model):
                raise ValueError("自备 LLM 模式（custom）需要在 code/mcagent/.env 配置 LLM_CUSTOM_BASE / LLM_CUSTOM_KEY / LLM_CUSTOM_MODEL")
            env_overrides.update({
                "LLM_PROFILE": "custom",
                "LLM_CUSTOM_BASE": custom_base,
                "LLM_CUSTOM_KEY": custom_key,
                "LLM_CUSTOM_MODEL": custom_model,
            })
            argv.extend(["--llm", "--strategy", "llm"])
            effective_llm_model = custom_model
        else:
            llm_key = str(raw_match.get("LLM_KEY") or raw_dev.get("LLM_KEY") or "").strip()
            if not llm_key:
                raise ValueError("未配置官方 AIA LLM_KEY，无法使用 official LLM")
            env_overrides.update({
                "LLM_PROFILE": "official",
                "LLM_BASE": str(raw_match.get("LLM_BASE") or raw_dev.get("LLM_BASE") or "https://api.nju-aia.com/v1").strip(),
                "LLM_KEY": llm_key,
                "LLM_MODEL": "default",
            })
            argv.extend(["--llm", "--strategy", "llm"])
            effective_llm_model = "default"

        effective_user = bot_user
        effective_llm_profile = llm_profile

    argv.extend(["--mode", run_mode])
    if run_mode == "focus":
        argv.extend(["--skill", focus_skill, "--skill-args", focus_args_clean])

    argv.extend(["--min-verified", min_verified])
    if include_draft:
        argv.append("--include-draft")
    if goal_clean:
        argv.extend(["--goal", goal_clean])
        if no_stop_on_goal:
            argv.append("--no-stop-on-goal")
    if max_seconds > 0:
        argv.extend(["--max-seconds", str(int(max_seconds) if max_seconds.is_integer() else max_seconds)])
    if max_steps > 0:
        argv.extend(["--max-steps", str(max_steps)])
    if stop_on_death:
        argv.append("--stop-on-death")
    if disconnect_on_exit:
        argv.append("--disconnect-on-exit")
    if use_checkpoint:
        argv.extend(["--checkpoint", str(target_run_dir / "world_memo.json")])

    # 仅展示脱敏命令
    safe_prefix = f"MC_API_BASE={api_base} LLM_PROFILE={effective_llm_profile}"
    if mode == "match":
        safe_prefix += f" LLM_MODEL=default LLM_MAX_CONCURRENCY={env_overrides.get('LLM_MAX_CONCURRENCY', '2')}"
    elif effective_llm_profile == "custom":
        safe_prefix += f" LLM_CUSTOM_MODEL={effective_llm_model}"
    cmd_display = f"{safe_prefix} " + " ".join(argv)

    return {
        "mode": mode,
        "argv": argv,
        "env_overrides": env_overrides,
        "cmd_display": cmd_display,
        "run_dir": target_run_dir,
        "meta": {
            "mode": mode,
            "api_base": api_base,
            "bot_user": effective_user,
            "strategy": strategy,
            "llm_profile": effective_llm_profile,
            "llm_model": effective_llm_model,
            "min_verified": min_verified,
            "include_draft": include_draft,
            "max_seconds": max_seconds,
            "max_steps": max_steps,
            "goal": goal_clean or None,
            "run_mode": run_mode,
            "focus_skill": focus_skill or None,
        },
    }


# ============================================================================
# 子进程生命周期管理（直启 + 进程树/残留进程收割）
# ============================================================================

def _find_descendant_pids(root_pid: int, proc_root: Path = Path("/proc")) -> list[int]:
    """扫描 /proc 找出 root_pid 的全部后代 PID（子进程优先，便于自底向上收割）。"""
    if root_pid <= 0 or not proc_root.is_dir():
        return []
    children_of: dict[int, list[int]] = collections.defaultdict(list)
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    for ent in entries:
        if not ent.name.isdigit():
            continue
        try:
            stat_txt = (ent / "stat").read_text(encoding="utf-8", errors="replace")
            rparen = stat_txt.rfind(")")
            if rparen < 0:
                continue
            fields = stat_txt[rparen + 2:].split()
            if len(fields) < 2:
                continue
            pid = int(ent.name)
            ppid = int(fields[1])
            children_of[ppid].append(pid)
        except (OSError, ValueError):
            continue

    descendants: list[int] = []
    stack = list(children_of.get(root_pid, ()))
    seen: set[int] = {root_pid}
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        descendants.append(cur)
        stack.extend(children_of.get(cur, ()))
    descendants.reverse()
    return descendants


def _find_external_loop_procs(
    *,
    exclude_pids: Optional[set[int]] = None,
    proc_root: Path = Path("/proc"),
) -> list[dict[str, Any]]:
    """发现当前仓库下正在运行的 `mcagent.loop` 进程（含控制台重启后脱离句柄的残留进程）。"""
    if not proc_root.is_dir():
        return []
    skip = set(exclude_pids or ()) | {os.getpid()}
    repo_str = str(REPO_ROOT.resolve())
    found: list[dict[str, Any]] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    for ent in entries:
        if not ent.name.isdigit():
            continue
        pid = int(ent.name)
        if pid in skip:
            continue
        try:
            raw = (ent / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        args = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not args:
            continue
        cmd = " ".join(args)
        if "mcagent.loop" not in cmd:
            continue
        if any(tok in cmd for tok in ("unittest", "pytest", "preflight", "--dry", "--replay")):
            continue
        # 归属校验：cwd 或 cmdline 指向本仓库，避免误碰其他目录的无关进程
        in_repo = repo_str in cmd
        if not in_repo:
            try:
                cwd = os.readlink(ent / "cwd")
                in_repo = cwd == repo_str or cwd.startswith(repo_str + os.sep)
            except OSError:
                in_repo = False
        if not in_repo:
            continue
        run_id = None
        if "--run-dir" in args:
            idx = args.index("--run-dir")
            if idx + 1 < len(args):
                run_id = run_id_for_path(Path(args[idx + 1]))
        mode = "match" if "--live" in args else ("local" if "--local-target" in args else None)
        found.append({"pid": pid, "run_id": run_id, "mode": mode, "cmd": cmd[:300]})
    found.sort(key=lambda r: r["pid"])
    return found


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class ProcessManager:
    """管理本控制台启动（以及残留接管）的 `mcagent.loop` 实例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen[str]] = None
        self._state: str = "idle"
        self._run_id: Optional[str] = None
        self._run_dir: Optional[Path] = None
        self._mode: Optional[str] = None
        self._meta: dict[str, Any] = {}
        self._cmd_display: str = ""
        self._started_at: Optional[float] = None
        self._ended_at: Optional[float] = None
        self._returncode: Optional[int] = None
        self._console_lines: collections.deque[str] = collections.deque(maxlen=MAX_CONSOLE_LINES)

    def _is_alive_locked(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._is_alive_locked()
            if not running and self._proc is not None and self._state in ("running", "stopping"):
                self._returncode = self._proc.returncode
                self._ended_at = self._ended_at or time.time()
                self._state = "exited"
            elapsed = None
            if self._started_at is not None:
                end = time.time() if running else (self._ended_at or time.time())
                elapsed = round(max(0.0, end - self._started_at), 1)
            snap = {
                "state": self._state,
                "running": running,
                "pid": self._proc.pid if running and self._proc is not None else None,
                "run_id": self._run_id,
                "mode": self._mode,
                "meta": dict(self._meta),
                "cmd_display": self._cmd_display,
                "started_at": self._started_at,
                "ended_at": self._ended_at,
                "elapsed_s": elapsed,
                "returncode": self._returncode,
                "external": False,
            }
        if not snap["running"]:
            ext = _find_external_loop_procs()
            if ext:
                first = ext[0]
                snap.update({
                    "state": "running",
                    "running": True,
                    "pid": first["pid"],
                    "run_id": first["run_id"] or snap["run_id"],
                    "mode": first["mode"] or snap["mode"],
                    "cmd_display": first["cmd"],
                    "external": True,
                })
        return snap

    def console_tail(self, limit: int = 200) -> list[str]:
        with self._lock:
            items = list(self._console_lines)
        return items[-max(1, limit):]

    def start(self, spec: dict[str, Any]) -> dict[str, Any]:
        ext = _find_external_loop_procs()
        with self._lock:
            if self._is_alive_locked():
                raise RuntimeError(f"已有运行中的实例 (pid={self._proc.pid}, run={self._run_id})，请先停止后再启动")
            if ext:
                pids = ", ".join(str(x["pid"]) for x in ext)
                raise RuntimeError(f"检测到残留/外部运行的 mcagent.loop 进程 (pid={pids})，请先点「优雅停止」或「强杀」清理")

            run_dir: Path = spec["run_dir"]
            run_dir.mkdir(parents=True, exist_ok=True)
            log_file_path = run_dir / "console.log"

            env = os.environ.copy()
            env.update(spec["env_overrides"])
            env["PYTHONUNBUFFERED"] = "1"

            proc = subprocess.Popen(
                spec["argv"],
                cwd=str(CODE_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )

            self._proc = proc
            self._state = "running"
            self._run_dir = run_dir
            self._run_id = run_id_for_path(run_dir)
            self._mode = spec["mode"]
            self._meta = dict(spec["meta"])
            self._cmd_display = spec["cmd_display"]
            self._started_at = time.time()
            self._ended_at = None
            self._returncode = None
            self._console_lines.clear()
            self._console_lines.append(f"[console] 启动命令: {self._cmd_display}")

            reader = threading.Thread(
                target=self._pump_output,
                args=(proc, log_file_path),
                daemon=True,
                name=f"console-pump-{proc.pid}",
            )
            reader.start()

        return self.status()

    def _pump_output(self, proc: subprocess.Popen[str], log_file_path: Path) -> None:
        try:
            with log_file_path.open("a", encoding="utf-8", errors="replace") as fh:
                if proc.stdout is not None:
                    for raw_line in proc.stdout:
                        line = raw_line.rstrip("\r\n")
                        fh.write(line + "\n")
                        fh.flush()
                        with self._lock:
                            self._console_lines.append(line)
        except Exception as exc:
            with self._lock:
                self._console_lines.append(f"[console] 日志读取异常: {exc}")
        finally:
            rc = proc.wait()
            with self._lock:
                if self._proc is proc:
                    self._returncode = rc
                    self._ended_at = time.time()
                    self._state = "exited"
                    self._console_lines.append(f"[console] 进程已退出，exit_code={rc}")

    def stop(self, *, force: bool = False) -> dict[str, Any]:
        sig = signal.SIGKILL if force else signal.SIGINT
        with self._lock:
            proc = self._proc if self._is_alive_locked() else None
            if proc is not None:
                self._state = "stopping"

        target_pids: list[int] = []
        pgids: set[int] = set()
        if proc is not None:
            pgids.add(proc.pid)
            target_pids.extend(_find_descendant_pids(proc.pid))
            target_pids.append(proc.pid)

        # 同时纳入脱离句柄的外部/残留 mcagent.loop 进程及其子进程，彻底杜绝残留
        for ext in _find_external_loop_procs():
            ep = int(ext["pid"])
            for dp in _find_descendant_pids(ep):
                if dp not in target_pids:
                    target_pids.append(dp)
            if ep not in target_pids:
                target_pids.append(ep)
            try:
                pg = os.getpgid(ep)
                if pg > 0 and pg != os.getpgrp():
                    pgids.add(pg)
            except OSError:
                pass

        if not target_pids and not pgids:
            return {"ok": True, "message": "当前没有运行中的进程", "status": self.status()}

        with self._lock:
            self._console_lines.append(
                f"[console] 发送 {sig.name} -> pids={target_pids} pgids={sorted(pgids)}"
            )

        for pg in pgids:
            try:
                os.killpg(pg, sig)
            except (ProcessLookupError, PermissionError):
                pass
        for pid in target_pids:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

        # 短暂等待回收，确保强杀立即生效且状态立刻更新
        deadline = time.monotonic() + (1.5 if force else 0.8)
        while time.monotonic() < deadline:
            if proc is not None and proc.poll() is None:
                try:
                    proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            if not any(_pid_alive(pid) for pid in target_pids):
                break
            time.sleep(0.05)

        still_alive = [pid for pid in target_pids if _pid_alive(pid)]
        if proc is not None and proc.poll() is not None:
            with self._lock:
                if self._proc is proc:
                    self._returncode = proc.returncode
                    self._ended_at = self._ended_at or time.time()
                    self._state = "exited"

        msg = (
            f"已强杀进程 {target_pids}" if (force and not still_alive)
            else f"已发送 {sig.name} 到 {target_pids}" + (f"（等待退出中: {still_alive}）" if still_alive else "（已退出）")
        )
        return {"ok": True, "message": msg, "killed_pids": target_pids, "remaining_pids": still_alive, "status": self.status()}

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "running": self._is_alive_locked(),
            "pid": self._proc.pid if self._proc is not None and self._is_alive_locked() else None,
            "run_id": self._run_id,
            "mode": self._mode,
        }


# ============================================================================
# 运行目录发现与遥测聚合（实时 + 历史）
# ============================================================================

def run_id_for_path(run_dir: Path) -> str:
    resolved = run_dir.resolve()
    for prefix, root in (("logs/", LOGS_ROOT.resolve()), ("results/", RESULTS_ROOT.resolve())):
        try:
            rel = resolved.relative_to(root)
            return prefix + rel.as_posix()
        except ValueError:
            continue
    return resolved.name


def resolve_run_id(run_id: str) -> Optional[Path]:
    clean = (run_id or "").strip().lstrip("/")
    if not clean or ".." in clean.split("/"):
        return None
    if clean.startswith("logs/"):
        base = LOGS_ROOT.resolve()
        candidate = (base / clean[len("logs/"):]).resolve()
    elif clean.startswith("results/"):
        base = RESULTS_ROOT.resolve()
        candidate = (base / clean[len("results/"):]).resolve()
    else:
        return None
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    if not candidate.is_dir():
        return None
    return candidate


def _read_jsonl_tail(path: Path, max_bytes: int = MAX_TIMELINE_BYTES) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except json.JSONDecodeError:
            continue
    return out


def _read_first_and_last_jsonl(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not path.is_file():
        return {}, {}
    first: dict[str, Any] = {}
    last: dict[str, Any] = {}
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            first_raw = fh.readline().decode("utf-8", errors="replace").strip()
            if first_raw:
                try:
                    obj = json.loads(first_raw)
                    if isinstance(obj, dict):
                        first = obj
                except json.JSONDecodeError:
                    pass
            if size > 65536:
                fh.seek(size - 65536)
                fh.readline()
            else:
                fh.seek(0)
            tail = fh.read().decode("utf-8", errors="replace").splitlines()
            for line in reversed(tail):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        last = obj
                        break
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return first, last


_DISCOVER_CACHE_LOCK = threading.Lock()
_DISCOVER_CACHE: dict[tuple[Optional[str], int], tuple[float, list[dict[str, Any]]]] = {}
_RUN_SUMMARY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_RUN_DETAIL_CACHE: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
_DISCOVER_TTL_S = 4.0


def invalidate_run_caches() -> None:
    with _DISCOVER_CACHE_LOCK:
        _DISCOVER_CACHE.clear()


def discover_runs(active_run_id: Optional[str] = None, limit: int = 60) -> list[dict[str, Any]]:
    """快速发现 `code/mcagent/logs/` 与 `results/` 下最新的运行记录（带目录剪枝与 mtime 缓存，避免海量历史目录拖慢 UI）。"""
    cache_key = (active_run_id, limit)
    now = time.monotonic()
    with _DISCOVER_CACHE_LOCK:
        cached = _DISCOVER_CACHE.get(cache_key)
        if cached is not None and (now - cached[0]) < _DISCOVER_TTL_S:
            return [dict(item) for item in cached[1]]

    skip_names = {"incidents", "worktrees", "__pycache__", "probe", "replay-sample"}
    marker_names = ("timeline.jsonl", "progress_curve.jsonl", "console.log")
    # 收集 (approx_mtime, path)，按 mtime 剪枝只解析最新的一批目录，防止 2500+ ep 目录全盘 IO
    raw_candidates: list[tuple[float, Path]] = []
    seen: set[str] = set()

    def _scan(root: Path, depth: int) -> None:
        if depth < 0:
            return
        try:
            with os.scandir(root) as it:
                entries = list(it)
        except OSError:
            return

        has_data = False
        newest_marker_mtime = 0.0
        subdirs: list[tuple[float, Path]] = []
        for ent in entries:
            try:
                if ent.is_file(follow_symlinks=False):
                    if ent.name in marker_names:
                        has_data = True
                        st_m = ent.stat(follow_symlinks=False).st_mtime
                        if st_m > newest_marker_mtime:
                            newest_marker_mtime = st_m
                elif depth > 0 and ent.is_dir(follow_symlinks=False) and ent.name not in skip_names:
                    st_m = ent.stat(follow_symlinks=False).st_mtime
                    subdirs.append((st_m, Path(ent.path)))
            except OSError:
                continue

        if has_data and root != LOGS_ROOT and root != RESULTS_ROOT:
            key = str(root)
            if key not in seen:
                seen.add(key)
                raw_candidates.append((newest_marker_mtime, root))

        if subdirs:
            subdirs.sort(key=lambda x: -x[0])
            # 单父目录下有上千 ep-* 子目录时只扫最新 80 个，保证毫秒级返回
            for _, sub in subdirs[: max(limit + 20, 80)]:
                _scan(sub, depth - 1)

    if LOGS_ROOT.is_dir():
        _scan(LOGS_ROOT, 3)
    if RESULTS_ROOT.is_dir():
        _scan(RESULTS_ROOT, 2)

    # 确保当前活跃运行一定在候选集中
    if active_run_id:
        active_path = resolve_run_id(active_run_id)
        if active_path is not None and str(active_path) not in seen:
            try:
                raw_candidates.append((active_path.stat().st_mtime + 1e9, active_path))
            except OSError:
                pass

    raw_candidates.sort(key=lambda x: -x[0])
    top_candidates = raw_candidates[: max(limit + 10, 70)]

    runs: list[dict[str, Any]] = []
    for _, rdir in top_candidates:
        rid = run_id_for_path(rdir)
        tl = rdir / "timeline.jsonl"
        pc = rdir / "progress_curve.jsonl"
        sm = rdir / "summary.md"
        cl = rdir / "console.log"

        mtimes = []
        for p in (tl, pc, sm, cl):
            try:
                if p.is_file():
                    mtimes.append(p.stat().st_mtime)
            except OSError:
                pass
        try:
            mtime = max(mtimes) if mtimes else rdir.stat().st_mtime
        except OSError:
            continue

        cached_row = _RUN_SUMMARY_CACHE.get(rid)
        if cached_row is not None and cached_row[0] == mtime:
            row = dict(cached_row[1])
            row["is_active"] = rid == active_run_id
            runs.append(row)
            continue

        first_tl, last_tl = _read_first_and_last_jsonl(tl)
        _, last_pc = _read_first_and_last_jsonl(pc)

        source = str(first_tl.get("source") or ("official" if "official" in rid else "local-target"))
        meta = first_tl.get("meta") if isinstance(first_tl.get("meta"), dict) else {}
        elapsed_s = float(last_pc.get("elapsed_s") or last_tl.get("elapsed_s") or 0.0)
        server_score = int(last_pc.get("server_score_v2") or 0)
        inferred_score = int(last_pc.get("inferred_score_v2") or 0)
        deaths = max(int(last_pc.get("deaths") or 0), int(last_tl.get("deaths") or 0))
        goals_reached = last_pc.get("goals_reached") if isinstance(last_pc.get("goals_reached"), list) else []
        stop_reason = last_tl.get("stop_reason") if last_tl.get("event") == "run_end" else None
        total_tok_hint = int(last_pc.get("total_tokens") or 0)

        row = {
            "id": rid,
            "label": rid.split("/", 1)[-1],
            "group": "results" if rid.startswith("results/") else "logs",
            "source": source,
            "mtime": mtime,
            "mtime_iso": datetime.datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M:%S"),
            "elapsed_s": round(elapsed_s, 1),
            "server_score": server_score,
            "inferred_score": inferred_score,
            "deaths": deaths,
            "goals_count": len(goals_reached),
            "strategy": meta.get("strategy") or ("llm" if (meta.get("use_llm") or total_tok_hint > 0) else "rule"),
            "bot_user": meta.get("bot_user") or "",
            "stop_reason": stop_reason,
            "is_active": rid == active_run_id,
            "has_timeline": tl.is_file(),
            "has_summary": sm.is_file(),
        }
        _RUN_SUMMARY_CACHE[rid] = (mtime, dict(row))
        runs.append(row)

    runs.sort(key=lambda r: (not r["is_active"], -r["mtime"]))
    result = runs[:limit]
    with _DISCOVER_CACHE_LOCK:
        _DISCOVER_CACHE[cache_key] = (time.monotonic(), [dict(item) for item in result])
    return result


def _extract_goal_from_reason(reason: str) -> Optional[str]:
    if not reason:
        return None
    for pat in (_STEP_GOAL_RE, _OBJ_GOAL_RE):
        m = pat.search(reason)
        if m and m.group(1) in GOAL_MAP:
            return m.group(1)
    return None


def _downsample_trajectory(points: list[dict[str, Any]], max_points: int = MAX_TRAJECTORY_POINTS) -> list[dict[str, Any]]:
    if len(points) <= max_points:
        return points
    keep_idx: set[int] = {0, len(points) - 1}
    for i, pt in enumerate(points):
        if pt.get("kind") in ("death", "skill_change", "goal"):
            keep_idx.add(i)
    stride = max(1, math.ceil(len(points) / max_points))
    for i in range(0, len(points), stride):
        keep_idx.add(i)
    return [points[i] for i in sorted(keep_idx)]


def parse_run_detail(run_dir: Path, *, is_active: bool = False) -> dict[str, Any]:
    """解析单个运行目录的全部遥测：生命体征、2D 空间轨迹、得分/Token 曲线、技能分布、事件流与异常包。"""
    rid = run_id_for_path(run_dir)
    tl_rows = _read_jsonl_tail(run_dir / "timeline.jsonl")
    pc_rows = _read_jsonl_tail(run_dir / "progress_curve.jsonl", max_bytes=2 * 1024 * 1024)

    meta: dict[str, Any] = {}
    git_meta: dict[str, Any] = {}
    source = "official" if "official" in rid else "local-target"
    started_epoch: Optional[float] = None

    # Vitals & active brain
    last_pos: Optional[list[float]] = None
    hp: Optional[float] = None
    food: Optional[float] = None
    elapsed_s: float = 0.0
    steps_count = 0
    deaths_count = 0
    stop_reason: Optional[str] = None
    active_skill: Optional[str] = None
    active_skill_args: dict[str, Any] = {}
    active_skill_source: str = "rule"
    active_skill_reason: str = ""
    active_skill_start_t: Optional[float] = None
    active_objective: Optional[dict[str, Any]] = None
    last_op: Optional[str] = None
    last_op_ok: Optional[bool] = None
    last_op_ms: Optional[float] = None

    # Skill aggregation
    skill_stats: dict[str, dict[str, Any]] = {}
    cur_skill_name: Optional[str] = None
    cur_skill_t0: Optional[float] = None

    # LLM stats
    llm_calls = 0
    llm_ok = 0
    llm_ms_list: list[float] = []
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    # Trajectory & events
    raw_traj: list[dict[str, Any]] = []
    total_dist = 0.0
    prev_pos_for_dist: Optional[list[float]] = None
    events_out: list[dict[str, Any]] = []
    goals_reached_times: dict[str, float] = {}
    env_blocked_goals: dict[str, Any] = {}

    for ev in tl_rows:
        kind = str(ev.get("event") or "")
        t_val = ev.get("elapsed_s")
        t_s = (
            float(t_val)
            if isinstance(t_val, (int, float)) and not isinstance(t_val, bool) and math.isfinite(t_val)
            else elapsed_s
        )
        if t_s > elapsed_s:
            elapsed_s = t_s

        if kind == "run_start":
            source = str(ev.get("source") or source)
            if isinstance(ev.get("git"), dict):
                git_meta = ev["git"]
            if isinstance(ev.get("meta"), dict):
                meta = ev["meta"]
            if isinstance(ev.get("t"), (int, float)):
                started_epoch = float(ev["t"])
            events_out.append({
                "t": 0.0,
                "kind": "run_start",
                "category": "system",
                "tone": "info",
                "badge": "START",
                "title": f"开始运行 · {source} · {meta.get('strategy', 'rule')}",
                "detail": f"bot={meta.get('bot_user') or '-'} · model={meta.get('llm_model') or 'rule'} · min_verified={meta.get('min_verified') or '-'}",
            })
        elif kind == "objective_decision":
            gid = ev.get("goal_id")
            src = str(ev.get("source") or "rule")
            trig = str(ev.get("trigger") or "")
            adopted = bool(ev.get("adopted", True))
            why = str(ev.get("reason") or ev.get("why") or "")
            ms = float(ev.get("ms") or 0.0)
            p_tok = int(ev.get("prompt_tokens") or 0)
            c_tok = int(ev.get("completion_tokens") or 0)
            tot_tok = int(ev.get("total_tokens") or (p_tok + c_tok))
            cum_p = int(ev.get("cumulative_prompt_tokens") or 0)
            cum_c = int(ev.get("cumulative_completion_tokens") or 0)
            cum_t = int(ev.get("cumulative_total_tokens") or 0)
            errs = ev.get("errors") if isinstance(ev.get("errors"), list) else []

            if src != "rule" or ms > 0 or tot_tok > 0:
                llm_calls += 1
                if src == "llm" and not errs:
                    llm_ok += 1
                if ms > 0:
                    llm_ms_list.append(ms)
                prompt_tokens = max(prompt_tokens, cum_p) if cum_p > 0 else prompt_tokens + p_tok
                completion_tokens = max(completion_tokens, cum_c) if cum_c > 0 else completion_tokens + c_tok
                total_tokens = max(total_tokens, cum_t) if cum_t > 0 else total_tokens + tot_tok

            if adopted and gid:
                spec = GOAL_MAP.get(str(gid))
                active_objective = {
                    "goal_id": str(gid),
                    "title": spec.title if spec else str(gid),
                    "score": spec.score if spec else 0,
                    "source": src,
                    "trigger": trig,
                    "reason": why[:200],
                    "t": round(t_s, 1),
                }
            llm_tag = f" · {ms / 1000:.2f}s · +{tot_tok}tok" if (ms > 0 or tot_tok > 0) else ""
            events_out.append({
                "t": round(t_s, 1),
                "kind": "objective_decision",
                "category": "strategy",
                "tone": "accent" if adopted else "muted",
                "badge": "OBJ",
                "title": f"战略目标 → {gid or '-'}（{'采纳' if adopted else '沿用'} · {src}{llm_tag}）",
                "detail": f"{trig}{' · ' + why[:140] if why else ''}",
            })
        elif kind == "skill_start":
            sname = str(ev.get("skill") or "")
            sargs = ev.get("args") if isinstance(ev.get("args"), dict) else {}
            ssrc = str(ev.get("source") or "rule")
            sreason = str(ev.get("reason") or "")
            active_skill = sname
            active_skill_args = sargs
            active_skill_source = ssrc
            active_skill_reason = sreason
            active_skill_start_t = t_s
            cur_skill_name = sname
            cur_skill_t0 = t_s

            st_entry = skill_stats.setdefault(
                sname, {"skill": sname, "started": 0, "ok": 0, "replan": 0, "env_blocked": 0, "fail": 0, "total_s": 0.0, "steps": 0}
            )
            st_entry["started"] += 1

            inferred_gid = _extract_goal_from_reason(sreason)
            if inferred_gid and active_objective is None:
                spec = GOAL_MAP.get(inferred_gid)
                active_objective = {
                    "goal_id": inferred_gid,
                    "title": spec.title if spec else inferred_gid,
                    "score": spec.score if spec else 0,
                    "source": ssrc,
                    "trigger": "planner",
                    "reason": sreason[:160],
                    "t": round(t_s, 1),
                }

            if last_pos is not None:
                raw_traj.append({
                    "t": round(t_s, 1),
                    "x": round(last_pos[0], 2),
                    "y": round(last_pos[1], 2),
                    "z": round(last_pos[2], 2),
                    "op": "skill_start",
                    "skill": sname,
                    "kind": "skill_change",
                })
            args_str = json.dumps(sargs, ensure_ascii=False) if sargs else ""
            events_out.append({
                "t": round(t_s, 1),
                "kind": "skill_start",
                "category": "skill",
                "tone": "info",
                "badge": "SKILL",
                "title": f"开始技能 {sname} {args_str}",
                "detail": sreason[:160],
            })
        elif kind == "step":
            steps_count += 1
            rec = ev.get("record") if isinstance(ev.get("record"), dict) else {}
            op = str(ev.get("op") or rec.get("op") or "")
            sname = str(ev.get("skill") or active_skill or "")
            if sname:
                st_entry = skill_stats.setdefault(
                    sname, {"skill": sname, "started": 0, "ok": 0, "replan": 0, "env_blocked": 0, "fail": 0, "total_s": 0.0, "steps": 0}
                )
                st_entry["steps"] += 1

            pos = ev.get("pos")
            if isinstance(pos, list) and len(pos) >= 3:
                try:
                    px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
                    if all(math.isfinite(v) for v in (px, py, pz)):
                        if prev_pos_for_dist is not None:
                            d = math.dist(prev_pos_for_dist, [px, py, pz])
                            if d < 120.0:  # 过滤跨维度/传送瞬移跳变
                                total_dist += d
                        prev_pos_for_dist = [px, py, pz]
                        last_pos = [round(px, 2), round(py, 2), round(pz, 2)]
                        raw_traj.append({
                            "t": round(t_s, 1),
                            "x": round(px, 2),
                            "y": round(py, 2),
                            "z": round(pz, 2),
                            "op": op,
                            "skill": sname,
                            "kind": "step",
                        })
                except (TypeError, ValueError):
                    pass

            if isinstance(ev.get("hp"), (int, float)):
                hp = round(float(ev["hp"]), 1)
            if isinstance(ev.get("food"), (int, float)):
                food = round(float(ev["food"]), 1)

            last_op = op
            last_op_ok = rec.get("ok") if isinstance(rec.get("ok"), bool) else True
            if isinstance(rec.get("ms"), (int, float)):
                last_op_ms = round(float(rec["ms"]), 1)

            detail_bits = []
            if rec.get("item"):
                detail_bits.append(f"item={rec['item']}")
            if rec.get("pos"):
                detail_bits.append(f"target={rec['pos']}")
            if rec.get("error"):
                detail_bits.append(f"err={rec['error']}")
            if last_op_ms is not None:
                detail_bits.append(f"{last_op_ms:.0f}ms")
            events_out.append({
                "t": round(t_s, 1),
                "kind": "step",
                "category": "step",
                "tone": "ok" if last_op_ok is not False else "warn",
                "badge": op.upper()[:8] or "STEP",
                "title": f"{sname} → {op}",
                "detail": " · ".join(detail_bits)[:160],
            })
        elif kind == "skill_end":
            sname = str(ev.get("skill") or cur_skill_name or "")
            outcome = str(ev.get("outcome") or "unknown")
            dt = max(0.0, t_s - cur_skill_t0) if (cur_skill_name == sname and cur_skill_t0 is not None) else 0.0
            st_entry = skill_stats.setdefault(
                sname, {"skill": sname, "started": 0, "ok": 0, "replan": 0, "env_blocked": 0, "fail": 0, "total_s": 0.0, "steps": 0}
            )
            st_entry["total_s"] = round(st_entry["total_s"] + dt, 1)
            if outcome == "completed":
                st_entry["ok"] += 1
                tone = "ok"
            elif outcome == "env_blocked":
                st_entry["env_blocked"] += 1
                tone = "warn"
            elif outcome == "replan":
                st_entry["replan"] += 1
                tone = "warn"
            elif outcome == "budget_stopped":
                tone = "muted"
            else:
                st_entry["fail"] += 1
                tone = "bad"
            cur_skill_name = None
            cur_skill_t0 = None
            if not is_active:
                active_skill = None

            msg = str(ev.get("error_msg") or ev.get("postcondition_error") or "")
            if outcome == "env_blocked" and isinstance(ev.get("environment"), dict):
                msg = str(ev["environment"].get("message") or msg)
            events_out.append({
                "t": round(t_s, 1),
                "kind": "skill_end",
                "category": "skill",
                "tone": tone,
                "badge": outcome.upper()[:10],
                "title": f"技能结束 {sname} → {outcome} ({dt:.1f}s)",
                "detail": msg[:180],
            })
        elif kind == "llm_call":
            llm_calls += 1
            ok = bool(ev.get("ok"))
            if ok:
                llm_ok += 1
            ms = float(ev.get("ms") or 0.0)
            if ms > 0:
                llm_ms_list.append(ms)
            p_tok = int(ev.get("prompt_tokens") or 0)
            c_tok = int(ev.get("completion_tokens") or 0)
            tot_tok = int(ev.get("total_tokens") or (p_tok + c_tok))
            cum_p = int(ev.get("cumulative_prompt_tokens") or 0)
            cum_c = int(ev.get("cumulative_completion_tokens") or 0)
            cum_t = int(ev.get("cumulative_total_tokens") or 0)
            prompt_tokens = max(prompt_tokens, cum_p) if cum_p > 0 else prompt_tokens + p_tok
            completion_tokens = max(completion_tokens, cum_c) if cum_c > 0 else completion_tokens + c_tok
            total_tokens = max(total_tokens, cum_t) if cum_t > 0 else total_tokens + tot_tok
            err_text = ev.get("error") or ("; ".join(str(x) for x in ev["errors"]) if isinstance(ev.get("errors"), list) else "")
            events_out.append({
                "t": round(t_s, 1),
                "kind": "llm_call",
                "category": "llm",
                "tone": "accent" if ok else "bad",
                "badge": "LLM",
                "title": f"LLM {'成功' if ok else '失败'} · {ms / 1000:.2f}s · +{tot_tok} tok",
                "detail": f"choice={ev.get('skill') or ev.get('goal_id') or '-'} {err_text}".strip()[:160],
            })
        elif kind == "goal_reached":
            gid = str(ev.get("goal_id") or "")
            if gid:
                goals_reached_times[gid] = round(t_s, 1)
                spec = GOAL_MAP.get(gid)
                if last_pos is not None:
                    raw_traj.append({
                        "t": round(t_s, 1),
                        "x": last_pos[0],
                        "y": last_pos[1],
                        "z": last_pos[2],
                        "op": f"goal:{gid}",
                        "skill": active_skill or "",
                        "kind": "goal",
                    })
                events_out.append({
                    "t": round(t_s, 1),
                    "kind": "goal_reached",
                    "category": "strategy",
                    "tone": "ok",
                    "badge": f"+{spec.score if spec else 0}分",
                    "title": f"达成目标 {gid}（{spec.title if spec else gid}）",
                    "detail": f"累计用时 {t_s:.1f}s",
                })
        elif kind == "death_recovered":
            deaths_count = max(deaths_count + 1, int(ev.get("deaths_total") or 0))
            cause = str(ev.get("cause") or "未知死因")
            if last_pos is not None:
                raw_traj.append({
                    "t": round(t_s, 1),
                    "x": last_pos[0],
                    "y": last_pos[1],
                    "z": last_pos[2],
                    "op": f"death:{cause[:40]}",
                    "skill": active_skill or "",
                    "kind": "death",
                })
            events_out.append({
                "t": round(t_s, 1),
                "kind": "death_recovered",
                "category": "safety",
                "tone": "bad",
                "badge": "DEATH",
                "title": f"死亡重生 #{deaths_count}：{cause}",
                "detail": f"重生耗时 {ev.get('respawn_ms') or '?'}ms",
            })
        elif kind == "run_end":
            stop_reason = str(ev.get("stop_reason") or "finished")
            deaths_count = max(deaths_count, int(ev.get("deaths") or 0))
            if isinstance(ev.get("goals_reached"), dict):
                for gid, gt in ev["goals_reached"].items():
                    if isinstance(gt, (int, float)):
                        goals_reached_times[str(gid)] = round(float(gt), 1)
            if isinstance(ev.get("environment_blocked_goals"), dict):
                env_blocked_goals = ev["environment_blocked_goals"]
            active_skill = None
            events_out.append({
                "t": round(t_s, 1),
                "kind": "run_end",
                "category": "system",
                "tone": "ok" if stop_reason in ("target_goal_reached", "max_seconds_reached", "max_steps_reached") else "warn",
                "badge": "END",
                "title": f"运行结束 · {stop_reason}",
                "detail": f"总耗时 {t_s:.1f}s · 死亡 {deaths_count}",
            })

    # If timeline.jsonl exceeded MAX_TIMELINE_BYTES, recover run_start meta from the head
    if not meta and (run_dir / "timeline.jsonl").is_file():
        first_tl, _ = _read_first_and_last_jsonl(run_dir / "timeline.jsonl")
        if first_tl.get("event") == "run_start":
            source = str(first_tl.get("source") or source)
            if isinstance(first_tl.get("git"), dict):
                git_meta = first_tl["git"]
            if isinstance(first_tl.get("meta"), dict):
                meta = first_tl["meta"]
            if isinstance(first_tl.get("t"), (int, float)):
                started_epoch = float(first_tl["t"])

    # Merge progress_curve.jsonl
    curve: list[dict[str, Any]] = []
    server_score_v2 = 0
    server_advancements = 0
    inferred_score_v2 = 0
    inferred_milestones: list[str] = []
    goals_reached_list: list[str] = []

    for row in pc_rows:
        rt_raw = row.get("elapsed_s")
        rt = float(rt_raw) if isinstance(rt_raw, (int, float)) and math.isfinite(rt_raw) else 0.0
        if rt > elapsed_s:
            elapsed_s = rt
        s_score = int(row.get("server_score_v2") or 0)
        i_score = int(row.get("inferred_score_v2") or 0)
        s_adv = int(row.get("server_advancements") or 0)
        r_deaths = int(row.get("deaths") or 0)
        r_ptok = int(row.get("prompt_tokens") or 0)
        r_ctok = int(row.get("completion_tokens") or 0)
        r_ttok = int(row.get("total_tokens") or (r_ptok + r_ctok))
        r_goals = row.get("goals_reached") if isinstance(row.get("goals_reached"), list) else []

        server_score_v2 = s_score
        server_advancements = s_adv
        inferred_score_v2 = i_score
        if isinstance(row.get("inferred_milestones"), list):
            inferred_milestones = list(row["inferred_milestones"])
        goals_reached_list = [str(g) for g in r_goals]
        deaths_count = max(deaths_count, r_deaths)
        prompt_tokens = max(prompt_tokens, r_ptok)
        completion_tokens = max(completion_tokens, r_ctok)
        total_tokens = max(total_tokens, r_ttok)

        curve.append({
            "t": round(rt, 1),
            "server_score": s_score,
            "inferred_score": i_score,
            "advs": s_adv,
            "deaths": r_deaths,
            "tokens": r_ttok,
            "goals_count": len(r_goals),
        })

    # Enrich goals reached
    all_reached_ids = list(dict.fromkeys([*goals_reached_times.keys(), *goals_reached_list]))
    goals_reached_detail = []
    for gid in all_reached_ids:
        spec = GOAL_MAP.get(gid)
        goals_reached_detail.append({
            "id": gid,
            "title": spec.title if spec else gid,
            "score": spec.score if spec else 0,
            "tier": spec.tier if spec else "-",
            "stage": spec.stage if spec else "-",
            "t": goals_reached_times.get(gid),
        })
    goals_reached_detail.sort(key=lambda g: (g["t"] is None, g["t"] or 0.0, g["id"]))

    # Trajectory bounds
    traj = _downsample_trajectory(raw_traj)
    bounds: Optional[dict[str, float]] = None
    if traj:
        xs = [p["x"] for p in traj]
        ys = [p["y"] for p in traj]
        zs = [p["z"] for p in traj]
        bounds = {
            "min_x": min(xs),
            "max_x": max(xs),
            "min_y": min(ys),
            "max_y": max(ys),
            "min_z": min(zs),
            "max_z": max(zs),
        }

    # Incidents
    incidents: list[dict[str, Any]] = []
    inc_dir = run_dir / "incidents"
    if inc_dir.is_dir():
        for fp in sorted(inc_dir.glob("INC-*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            rel_replay = os.path.relpath(fp, CODE_DIR)
            incidents.append({
                "filename": fp.name,
                "code": data.get("code") or "UNKNOWN",
                "severity": data.get("severity") or "P2",
                "category": data.get("category") or "strategy",
                "title": data.get("title") or fp.name,
                "skill": data.get("skill") or "-",
                "elapsed_s": data.get("elapsed_s"),
                "count": data.get("count") or 1,
                "replay_cmd": f"cd code && uv run python -m mcagent.loop --replay {rel_replay}",
                "payload": data,
            })

    # Summary & console log & world_memo
    summary_md = ""
    sm_path = run_dir / "summary.md"
    if sm_path.is_file():
        try:
            summary_md = sm_path.read_text(encoding="utf-8", errors="replace")[:40000]
        except OSError:
            pass

    console_log_lines: list[str] = []
    cl_path = run_dir / "console.log"
    if cl_path.is_file():
        try:
            console_log_lines = cl_path.read_text(encoding="utf-8", errors="replace").splitlines()[-250:]
        except OSError:
            pass

    landmarks: dict[str, Any] = {}
    memo_path = run_dir / "world_memo.json"
    if memo_path.is_file():
        try:
            memo_raw = json.loads(memo_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(memo_raw, dict):
                landmarks = {
                    "biomes": list((memo_raw.get("biomes") or {}).keys()) if isinstance(memo_raw.get("biomes"), dict) else [],
                    "landmarks": list((memo_raw.get("landmarks") or {}).keys()) if isinstance(memo_raw.get("landmarks"), dict) else [],
                }
        except (OSError, json.JSONDecodeError):
            pass

    scale_8h = (MATCH_DURATION_S / elapsed_s) if elapsed_s > 1.0 else 0.0
    in_pct = round(prompt_tokens / QUOTA_INPUT_TOKENS * 100.0, 2)
    out_pct = round(completion_tokens / QUOTA_OUTPUT_TOKENS * 100.0, 2)
    proj_in_pct = round(prompt_tokens * scale_8h / QUOTA_INPUT_TOKENS * 100.0, 1) if scale_8h else 0.0
    proj_out_pct = round(completion_tokens * scale_8h / QUOTA_OUTPUT_TOKENS * 100.0, 1) if scale_8h else 0.0

    skill_rows = sorted(skill_stats.values(), key=lambda r: (-r["total_s"], -r["steps"]))

    return {
        "id": rid,
        "source": source,
        "is_active": is_active,
        "git": git_meta,
        "meta": meta,
        "started_at": started_epoch,
        "vitals": {
            "pos": last_pos,
            "hp": hp,
            "food": food,
            "elapsed_s": round(elapsed_s, 1),
            "max_seconds": float(meta.get("max_seconds") or 0.0),
            "steps": steps_count,
            "max_steps": int(meta.get("max_steps") or 0),
            "deaths": deaths_count,
            "distance_m": round(total_dist, 1),
            "stop_reason": stop_reason,
            "active_skill": active_skill,
            "active_skill_args": active_skill_args,
            "active_skill_source": active_skill_source,
            "active_skill_reason": active_skill_reason,
            "active_skill_elapsed_s": round(max(0.0, elapsed_s - active_skill_start_t), 1)
            if (active_skill and active_skill_start_t is not None)
            else 0.0,
            "active_objective": active_objective,
            "last_op": last_op,
            "last_op_ok": last_op_ok,
            "last_op_ms": last_op_ms,
        },
        "scores": {
            "server_score_v2": server_score_v2,
            "server_advancements": server_advancements,
            "inferred_score_v2": inferred_score_v2,
            "inferred_milestones": inferred_milestones,
            "goals_reached": goals_reached_detail,
            "env_blocked_goals": env_blocked_goals,
        },
        "tokens": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "in_quota_pct": in_pct,
            "out_quota_pct": out_pct,
            "proj_8h_in_pct": proj_in_pct,
            "proj_8h_out_pct": proj_out_pct,
            "llm_calls": llm_calls,
            "llm_ok": llm_ok,
            "llm_avg_ms": round(sum(llm_ms_list) / len(llm_ms_list), 1) if llm_ms_list else 0.0,
        },
        "trajectory": {
            "points": traj,
            "total_raw_points": len(raw_traj),
            "bounds": bounds,
        },
        "curve": curve,
        "skill_stats": skill_rows[:30],
        "events": events_out[-MAX_EVENTS_RETURN:],
        "incidents": incidents,
        "landmarks": landmarks,
        "summary_md": summary_md,
        "console_log": console_log_lines,
    }


# ============================================================================
# 主办方交付审计 + 网关/Preflight 探测
# ============================================================================

def build_handover_audit() -> dict[str, Any]:
    """实时核查仓库可交付给主办方运行与评审的文件清单、风险提醒和零密钥校验。"""
    live_skills = sorted(name for name, cls in REGISTRY.items() if cls.verified == "live")
    offline_skills = sorted(name for name, cls in REGISTRY.items() if cls.verified != "live")
    stage_counts = collections.Counter(g.stage for g in GOALS)
    scoring_goals = [g for g in GOALS if g.score > 0]

    required_runtime = [
        ("pyproject.toml", "Python 依赖声明（仅 requests>=2.31.0）"),
        ("uv.lock", "依赖精确锁文件（主办方 uv sync --frozen 一键复现）"),
        ("code/mcagent/", f"完整 Agent Harness 运行时（{len(REGISTRY)} L3 技能 / {len(GOALS)} 台账 / 容错与安全看护）"),
        ("code/mcagent/.env.match.example", "主办方/比赛环境变量配置模板（无真实密钥）"),
        ("code/tests/", "离线单测与故障注入套件（无账号可跑）"),
        ("docs/99-原始记录/NMO_API_LICENSE.txt", "NMO API MIT 许可声明"),
    ]
    required_review = [
        ("README.md", "项目总览、运行说明与无账号复现命令"),
        ("report.pdf", "40 分工程评审技术报告（PDF）"),
        ("demo.md", "阶段展示与断网兜底回放脚本"),
        ("results/", "脱敏官方校园网基线、2h 长跑曲线与 INC-005 离线回放包"),
    ]
    forbidden_items = [
        ("code/mcagent/.env", "含个人/压测密钥与账号密码，严禁打包提交"),
        ("code/mcagent/.env.match", "含正式皮肤站密码与 AIA Key，严禁打包提交"),
        ("code/mcagent/.token.json", "Bearer 会话凭证，严禁打包提交"),
        ("tools/local-api-bridge/", "自建 Mineflayer 协议客户端（赛前本地联调专用，比赛严禁使用）"),
        ("tools/soak-autofix/", "赛前压测自动修单工具，运行包不需要"),
        ("code/mcagent/logs/", "本地历史运行日志（可能含角色名/坐标/磁带），不要打进运行包"),
    ]

    tracked_secrets: list[str] = []
    try:
        tracked = subprocess.check_output(
            ["git", "ls-files"], cwd=str(REPO_ROOT), stderr=subprocess.DEVNULL, text=True
        ).splitlines()
        for rel in tracked:
            base = os.path.basename(rel)
            if base in (".env", ".env.match", ".env.dev", ".token.json"):
                tracked_secrets.append(rel)
    except Exception:
        pass

    draft_count = stage_counts.get("draft", 0)
    warnings: list[str] = []
    if len(offline_skills) > 0 or draft_count > 0:
        warnings.append(
            f"当前 {len(REGISTRY)} 个技能中有 {len(live_skills)} 个标为 verified='live'、{len(offline_skills)} 个为 offline，{len(GOALS)} 项台账中 {draft_count} 项为 draft；在官方端点直接跑若不加 --min-verified offline --include-draft，将仅执行已晋级的技能与目标。"
        )
    warnings.extend([
        "提交给主办方时只需给核心代码 + pyproject.toml + uv.lock + .env.match.example，由主办方注入 MC_API_BASE / MC_USER / MC_PASS / LLM_BASE / LLM_KEY / LLM_MODEL=default / LLM_MAX_CONCURRENCY=2。",
        "绝不能把 tools/local-api-bridge/、.env、.env.match、.token.json 混入主办方运行包。",
    ])

    return {
        "git": _git_info(),
        "skills": {
            "total": len(REGISTRY),
            "live": len(live_skills),
            "offline": len(offline_skills),
            "live_names": live_skills,
        },
        "goals": {
            "total": len(GOALS),
            "scoring_count": len(scoring_goals),
            "max_score": sum(g.score for g in scoring_goals),
            "stages": dict(stage_counts),
        },
        "runtime_package": [
            {"path": p, "desc": d, "exists": (REPO_ROOT / p).exists()} for p, d in required_runtime
        ],
        "review_package": [
            {"path": p, "desc": d, "exists": (REPO_ROOT / p).exists()} for p, d in required_review
        ],
        "forbidden": [
            {"path": p, "reason": r, "exists_locally": (REPO_ROOT / p).exists()} for p, r in forbidden_items
        ],
        "tracked_secrets": tracked_secrets,
        "warnings": warnings,
        "commands": {
            "install": "uv sync --frozen",
            "preflight": "cd code && uv run python -m mcagent.preflight --live",
            "match_full": "cd code && uv run python -m mcagent.loop --live --llm --soak --min-verified offline --include-draft --max-seconds 28800 --checkpoint mcagent/logs/world_memo.json",
            "match_conservative": "cd code && uv run python -m mcagent.loop --live --llm --soak --max-seconds 28800 --checkpoint mcagent/logs/world_memo.json",
            "offline_verify": "cd code && uv run python -m unittest discover -s tests && uv run python -m mcagent.loop --dry --strategy llm --min-verified offline",
        },
    }


def run_preflight_checks(*, source: str = "match", live: bool = False) -> dict[str, Any]:
    env_path = MCAGENT_DIR / (".env.match" if source == "match" else ".env")
    if not env_path.is_file():
        return {
            "ok": False,
            "env_file": str(env_path.relative_to(REPO_ROOT)),
            "blocks": 1,
            "warns": 0,
            "checks": [{"name": "配置文件存在", "ok": False, "level": "BLOCK", "detail": f"找不到 {env_path.name}"}],
        }
    env = mc_preflight.load_env(str(env_path))
    checks = mc_preflight.check_static(env)
    if live:
        checks.extend(mc_preflight.check_live(env))
    rows = [{"name": c.name, "ok": c.ok, "level": c.level, "detail": c.detail} for c in checks]
    blocks = sum(1 for c in checks if not c.ok and c.level == mc_preflight.BLOCK)
    warns = sum(1 for c in checks if not c.ok and c.level == mc_preflight.WARN)
    return {
        "ok": blocks == 0,
        "env_file": str(env_path.relative_to(REPO_ROOT)),
        "blocks": blocks,
        "warns": warns,
        "checks": rows,
    }


def probe_local_bridge(base_url: str) -> dict[str, Any]:
    raw = (base_url or DEFAULT_LOCAL_API).strip()
    parsed = urlsplit(raw if "://" in raw else f"http://{raw}")
    host = (parsed.hostname or "").lower()
    if host in mc_preflight.OFFICIAL_HOSTS:
        return {"ok": False, "url": raw, "error": "禁止用本地网关探针探测官方端点"}
    origin = f"{parsed.scheme or 'http'}://{parsed.netloc}"
    health_url = f"{origin}/healthz"
    t0 = time.monotonic()
    req = urllib.request.Request(health_url, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=2.5) as resp:
            ms = round((time.monotonic() - t0) * 1000, 1)
            body = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {"raw": body[:300]}
            return {"ok": resp.status == 200, "status": resp.status, "ms": ms, "url": health_url, "data": data}
    except Exception as exc:
        ms = round((time.monotonic() - t0) * 1000, 1)
        return {"ok": False, "ms": ms, "url": health_url, "error": f"{type(exc).__name__}: {exc}"}


def run_dry_check(payload: dict[str, Any]) -> dict[str, Any]:
    mode = str(payload.get("mode") or "local").strip().lower()
    strategy = str(payload.get("strategy") or "llm").strip().lower()
    min_verified = str(payload.get("min_verified") or "offline").strip().lower()
    include_draft = bool(payload.get("include_draft", True))
    api_base = str(payload.get("api_base") or (OFFICIAL_CAMPUS_API if mode == "match" else DEFAULT_LOCAL_API)).strip()

    argv = [
        *_resolve_python_runner(),
        "-m",
        "mcagent.loop",
        "--dry",
        "--strategy",
        strategy if strategy in ("llm", "rule") else "rule",
        "--min-verified",
        min_verified if min_verified in ("offline", "live") else "offline",
    ]
    if include_draft:
        argv.append("--include-draft")
    env = os.environ.copy()
    env["MC_API_BASE"] = api_base
    env["MC_PROXY"] = ""
    t0 = time.monotonic()
    res = subprocess.run(
        argv,
        cwd=str(CODE_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    return {
        "ok": res.returncode == 0,
        "returncode": res.returncode,
        "ms": round((time.monotonic() - t0) * 1000, 1),
        "stdout": res.stdout,
        "stderr": res.stderr,
    }


# ============================================================================
# HTTP 服务（仅 127.0.0.1，CSRF Origin/Host 守卫）
# ============================================================================

PROCESS_MGR = ProcessManager()


def _is_loopback_host(val: str) -> bool:
    if not val:
        return True
    host = val.split(":", 1)[0].strip().lower()
    return host in ("127.0.0.1", "localhost", "[::1]")


def build_bootstrap_payload() -> dict[str, Any]:
    proc = PROCESS_MGR.status()
    goals_catalog = [
        {
            "id": g.id,
            "title": g.title,
            "tier": g.tier,
            "score": g.score,
            "stage": g.stage,
            "skill": g.skill,
        }
        for g in GOALS
    ]
    skills_catalog = [
        {
            "name": name,
            "verified": cls.verified,
            "description": getattr(cls, "description", ""),
        }
        for name, cls in sorted(REGISTRY.items())
    ]
    return {
        "git": _git_info(),
        "endpoints": {
            "official_campus": OFFICIAL_CAMPUS_API,
            "official_public": OFFICIAL_PUBLIC_API,
            "default_local": DEFAULT_LOCAL_API,
        },
        "env_dev": read_env_summary(MCAGENT_DIR / ".env"),
        "env_match": read_env_summary(MCAGENT_DIR / ".env.match"),
        "process": proc,
        "runs": discover_runs(active_run_id=proc.get("run_id")),
        "goals_catalog": goals_catalog,
        "skills_catalog": skills_catalog,
        "handover": build_handover_audit(),
    }


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "AIAMC-AgentConsole/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_static(self, rel_name: str, content_type: str) -> None:
        fp = (WEB_DIR / rel_name).resolve()
        try:
            fp.relative_to(WEB_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not fp.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        raw = fp.read_bytes()
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _check_local_origin(self) -> bool:
        host = self.headers.get("Host", "")
        if host and not _is_loopback_host(host):
            self._send_json({"ok": False, "error": "仅允许本机回环访问"}, status=403)
            return False
        origin = self.headers.get("Origin", "")
        if origin:
            ohost = _host_of(origin)
            if ohost not in ("127.0.0.1", "localhost", "::1"):
                self._send_json({"ok": False, "error": "拒绝跨站请求"}, status=403)
                return False
        return True

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        if length > 256 * 1024:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return obj

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_local_origin():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_static("index.html", "text/html; charset=utf-8")
            return
        if path == "/styles.css":
            self._send_static("styles.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self._send_static("app.js", "application/javascript; charset=utf-8")
            return

        if path == "/healthz":
            self._send_json({"ok": True, "service": "aiamc-agent-console"})
            return

        if path == "/api/bootstrap":
            self._send_json(build_bootstrap_payload())
            return

        if path == "/api/runs":
            proc = PROCESS_MGR.status()
            self._send_json({
                "process": proc,
                "runs": discover_runs(active_run_id=proc.get("run_id")),
            })
            return

        if path == "/api/run":
            run_id = (qs.get("id") or [""])[0]
            proc = PROCESS_MGR.status()
            if not run_id:
                run_id = proc.get("run_id") or ""
            rdir = resolve_run_id(run_id)
            if rdir is None:
                self._send_json({"ok": False, "error": f"找不到运行记录: {run_id!r}"}, status=404)
                return
            is_active = bool(proc.get("running") and proc.get("run_id") == run_id)
            detail = parse_run_detail(rdir, is_active=is_active)
            if proc.get("run_id") == run_id:
                live_lines = PROCESS_MGR.console_tail(260)
                if live_lines:
                    detail["console_log"] = live_lines
            detail["process"] = proc
            self._send_json(detail)
            return

        if path == "/api/preflight":
            live = (qs.get("live") or ["0"])[0] in ("1", "true", "yes")
            src = (qs.get("source") or ["match"])[0]
            if src not in ("match", "env"):
                src = "match"
            self._send_json(run_preflight_checks(source=src, live=live))
            return

        if path == "/api/probe-bridge":
            base = (qs.get("base") or [DEFAULT_LOCAL_API])[0]
            self._send_json(probe_local_bridge(base))
            return

        if path == "/api/handover":
            self._send_json(build_handover_audit())
            return

        self._send_json({"ok": False, "error": f"unknown route: {path}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_local_origin():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            body = self._read_json_body()
        except Exception as exc:
            self._send_json({"ok": False, "error": f"请求解析失败: {exc}"}, status=400)
            return

        if path == "/api/run/start":
            try:
                spec = build_launch_spec(body)
                status = PROCESS_MGR.start(spec)
                invalidate_run_caches()
                self._send_json({"ok": True, "status": status, "run_id": status.get("run_id")})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except RuntimeError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=409)
            except Exception as exc:
                self._send_json({"ok": False, "error": f"启动失败: {exc}"}, status=500)
            return

        if path == "/api/run/stop":
            invalidate_run_caches()
            self._send_json(PROCESS_MGR.stop(force=False))
            return

        if path == "/api/run/kill":
            invalidate_run_caches()
            self._send_json(PROCESS_MGR.stop(force=True))
            return

        if path == "/api/run/dry":
            try:
                res = run_dry_check(body)
                self._send_json(res)
            except Exception as exc:
                self._send_json({"ok": False, "error": f"Dry 检查异常: {exc}"}, status=500)
            return

        if path == "/api/preflight/apply-match":
            ok, msg = mc_preflight.apply_match()
            self._send_json({"ok": ok, "message": msg, "preflight": run_preflight_checks(source="env", live=False)})
            return

        self._send_json({"ok": False, "error": f"unknown route: {path}"}, status=404)


def create_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("出于安全与合规考虑，控制台仅允许监听本机回环地址（127.0.0.1）")
    return ThreadingHTTPServer((host, port), ConsoleHandler)


def _is_console_running(host: str, port: int) -> bool:
    url = f"http://{host}:{port}/healthz"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def _open_browser_async(url: str) -> None:
    def _worker() -> None:
        time.sleep(0.35)
        try:
            import webbrowser
            webbrowser.open(url, new=2)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True, name="console-open-browser").start()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AIAMC Harness 双模式可视化监控控制台")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（仅限本机回环，默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--open", action="store_true", help="启动后自动打开系统默认浏览器")
    args = parser.parse_args(argv)

    url = f"http://{args.host}:{args.port}/"
    if _is_console_running(args.host, args.port):
        print(f"[agent-console] 已在运行 -> {url}")
        if args.open:
            import webbrowser
            webbrowser.open(url, new=2)
        return 0

    srv = create_server(args.host, args.port)
    print(f"[agent-console] 启动成功 -> {url}")
    print(f"[agent-console] 仓库目录: {REPO_ROOT} | 仅本机可访问 | 按 Ctrl+C 退出")
    if args.open:
        _open_browser_async(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[agent-console] 收到中断信号，正在关闭服务...")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
