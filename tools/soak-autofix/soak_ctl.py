#!/usr/bin/env python3
"""双工位「持续挂机压测 + AI 自动修单热接力」编排器（物理隔离于 tools/soak-autofix/）。

核心设计：
1. 单一待审分支收敛：所有通过四道机器闸门的自动修复，统一提交到同一个独立待审分支（默认 `feat/soak-autofix`，严禁为 `main`），
   挂机结束后生成 `REVIEW_REPORT.md`，由人工一次性审阅再决定是否合并进 `main`。
2. 双 Git Worktree 物理隔离（零锁冲突）：
   - `fixer` 工位检出 `<branch>` 分支，专供 AI 修改代码与跑门禁；
   - `runner` 工位以 `--detach` 游离态指向 `refs/heads/<branch>`，每轮起跑前原子切到分支最新验证过的提交，
     彻底避免“同一目录热改 Python 文件导致半截语法崩溃 / `@register` 重复注册 / `dirty` 溯源失真”。
3. 秒级环境复用：自动软链主工作区 `code/mcagent/.env` 并注入 `UV_PROJECT_ENVIRONMENT=<repo>/code/.venv`，免拷贝 `.venv`。
4. 双离线 Bot 并行不互踢（配合 `local-api-bridge`）：
   - Runner 按轮使用 `soak_r_0001`...（默认每轮新冷启动空包角色，或配合 RCON 重置固定角色）；
   - Fixer 门禁使用独立 `soak_fixer` 角色跑 30 秒 `--mode focus` 真机小灶验修，绝不踢掉挂机中的 Runner 与开发者的 `de7_bot`。
5. 不信任 AI 自报结论的四道硬闸门（由本脚本独立执行）：
   - Gate 1：修改路径白名单（仅限 `loop/*.py`、非冻结 `skills/*.py`、`tests/test_*.py`）+ 密钥泄露正则扫描；
   - Gate 2：离线语法编译 + 全量 `unittest`（120+ 项）+ `mcagent.loop --dry`；
   - Gate 3：可选真机 `focus` 金丝雀小灶验收（要求退出码 0 且 0 新增 `bug` 异常单）；
   - Gate 4：提交到 `<branch>`，并在 Runner 下一轮 `campaign` 实跑时做 `--baseline` 对比；若诱发新 `P0 bug` 自动 `git revert` 回滚。
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import fnmatch
import getpass
import json
import math
import os
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

PROTECTED_BRANCHES = frozenset({"main", "master", "HEAD"})

# Gate 1 白名单与冻结保护面（严守 AGENTS.md §2A.3 / §5 / docs/05 §3）
ALLOWED_EDIT_PATTERNS: tuple[str, ...] = (
    "code/mcagent/loop/*.py",
    "code/mcagent/skills/*.py",
    "code/tests/test_*.py",
)
FORBIDDEN_EDIT_PATHS: frozenset[str] = frozenset({
    "code/mcagent/client.py",
    "code/mcagent/runtime.py",
    "code/mcagent/skills/base.py",
    "code/mcagent/skills/registry.py",
    "code/mcagent/skills/testing.py",
    "code/mcagent/planner/decision.py",
    "AGENTS.md",
})

SECRET_SCAN_RE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._\-]{16,}|MC_PASS\s*=\s*\S+|LLM_API_KEY\s*=\s*\S+)"
)

# 工位 B 修单模型目录（2026-11-14 本机实测：`agy models` 与 ~/.codex/models_cache.json）
# - codex：gpt-6.1-sol / gpt-6-astra 都支持 low/medium/high/xhigh/max（另有 ultra 会自动派子代理，无人值守修单不收）
# - agy：hydrogen 有 low/medium/high/xhigh 四档，carbon 有 low/medium/high 三档；档位写在模型名里，--effort 同步传
_CODEX_FIXER_TMPL = (
    "codex exec -C {{fixer_wt}} -s workspace-write -m {model} "
    "-c 'model_reasoning_effort=\"{effort}\"' - < {{prompt_file}}"
)
_AGY_FIXER_TMPL = (
    "cd {{fixer_wt}} && agy --model {model}-{effort} --effort {effort} --mode accept-edits --sandbox "
    "--dangerously-skip-permissions --disable-slash-commands -p=\"$(cat {{prompt_file}})\""
)
CODEX_FIXER_MODELS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "gpt-6.1-sol": ("GPT-6.1-Sol", "codex", ("low", "medium", "high", "xhigh", "max")),
    "gpt-6-astra": ("GPT-6-Astra", "codex", ("low", "medium", "high", "xhigh", "max")),
}
AGY_FIXER_MODELS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "gemini-4-hydrogen": ("Gemini 4 Hydrogen", "agy", ("low", "medium", "high", "xhigh")),
    "gemini-4-carbon": ("Gemini 4 Carbon", "agy", ("low", "medium", "high")),
}


def _build_fixer_catalog() -> tuple[dict[str, str], dict[str, str]]:
    presets: dict[str, str] = {}
    labels: dict[str, str] = {}
    for model, (title, cli, efforts) in CODEX_FIXER_MODELS.items():
        for eff in efforts:
            key = f"codex:{model}:{eff}"
            presets[key] = _CODEX_FIXER_TMPL.format(model=model, effort=eff)
            labels[key] = f"Codex · {title} · {eff}"
    for model, (title, cli, efforts) in AGY_FIXER_MODELS.items():
        for eff in efforts:
            key = f"agy:{model}-{eff}"
            presets[key] = _AGY_FIXER_TMPL.format(model=model, effort=eff)
            labels[key] = f"agy · {title} · {eff}"
    # 兼容旧名字：codex = 6.1-sol xhigh，agy = hydrogen high
    presets["codex"] = presets["codex:gpt-6.1-sol:xhigh"]
    labels["codex"] = "codex（= Codex · GPT-6.1-Sol · xhigh）"
    presets["agy"] = presets["agy:gemini-4-hydrogen-high"]
    labels["agy"] = "agy（= agy · Gemini 4 Hydrogen · high）"
    presets["claude"] = 'cd {fixer_wt} && claude -p --allowedTools "Read,Edit,Write,Grep,Glob,Bash" < {prompt_file}'
    labels["claude"] = "Claude Code（claude -p，本机默认模型）"
    return presets, labels


FIXER_PRESETS, FIXER_PRESET_LABELS = _build_fixer_catalog()


def resolve_fixer_template(session_dir: Path, default_name: str, default_tmpl: str) -> tuple[str, str]:
    """每接一单前取工位 B 的修单命令：state.json 里有合法的热切换 fixer_preset 就用它，否则用启动参数。"""
    try:
        st = json.loads((session_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        st = {}
    name = str(st.get("fixer_preset") or "").strip()
    if name and name in FIXER_PRESETS:
        return name, FIXER_PRESETS[name]
    return default_name, default_tmpl

DEFAULT_PLAYER_MODEL = "DeepSeek-V4.1-Flash"

# 工位 A 进哪个服务器（每次启动时选，挂机中不切）：
# - local：本机联调网关 tools/local-api-bridge（.env 的 MC_API_BASE，默认 127.0.0.1:8787 → 队内测试服），
#   角色名随意（--bot-user），玩家模型可用 custom；数据标 local-target。
# - official：比赛服官方端点。只能用 .env 里的队伍正式账号（MC_USER/MC_PASS，不能改名、不能轮换），
#   模型只能走 LLM_PROFILE=official（AGENTS.md §2B.3），账号是全队独占资源，启动前须在 docs/05 账号表占位（§3）。
# 工位 B 的金丝雀验修始终在自建测试服跑，不占比赛账号。
OFFICIAL_MC_BASE = "https://mc-api.windywindie.top/api/v1"
SERVER_TARGETS: dict[str, str] = {
    "local": "自建测试服（本机联调网关 → 队内测试服）",
    "official": "比赛服（官方端点 mc-api.windywindie.top，用队伍正式账号）",
}
# 前端下拉用的已知玩家模型（并行云 custom 通道实测过；也可以手填别的名字）
PLAYER_MODEL_CHOICES: dict[str, str] = {
    "DeepSeek-V4.1-Flash": "默认；关思考后单拍约 1.5–2.5s",
    "Qwen3.6-Flash": "Qwen 快速档",
    "DeepSeek-V4-Pro-0813": "完整菜单下常超 12s 主循环超时，慎用",
}

# 工位 A 进服角色名：默认每轮同名（服务器上只有一个挂机角色，轮次号只体现在记录里）；
# 同名意味着上一轮的背包、位置、进度会带进下一轮，要每轮空包冷启动就加 --rotate-user（<名字>001、002…）
# 默认名按本机系统用户名派生（<用户名前 8 位>_soak），避免队友在同一个共享测试世界里同名互踢
#（网关对同名重复登录会踢掉旧 token）
BOT_USER_RE = re.compile(r"^[A-Za-z0-9_]{3,13}$")  # 轮换时末尾补 3 位轮次号，总长不超 MC 的 16 字符


def _local_user_stem() -> str:
    try:
        raw = getpass.getuser()
    except Exception:
        raw = ""
    return re.sub(r"[^A-Za-z0-9_]", "", raw or "")[:8]


def default_bot_user() -> str:
    stem = _local_user_stem()
    return f"{stem}_soak" if stem else "agy_soak_bot"


def default_fixer_user() -> str:
    """工位 B 金丝雀验修角色：同样按本机用户名区分，避免两名队友同时修单时在共享测试世界互踢。"""
    stem = _local_user_stem()
    return f"{stem}_fixer" if stem else "agy_soak_fixer"


DEFAULT_BOT_USER = default_bot_user()
DEFAULT_FIXER_USER = default_fixer_user()


def _read_dotenv_keys(repo_root: Path) -> dict[str, str]:
    """只为判断「填没填」读取 code/mcagent/.env；进程环境变量优先（与 mcagent.client.Config.load 一致）。"""
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
    for k in ("MC_USER", "MC_PASS", "LLM_KEY"):
        if os.environ.get(k):
            vals[k] = os.environ[k]
    return vals


def check_server_target(repo_root: Path, server: str, *, use_llm: bool, account_reserved: bool) -> list[str]:
    """启动前核对目标服务器的前置条件，返回中文问题清单（空表示可以启动）。不输出任何密钥值。"""
    if server not in SERVER_TARGETS:
        return [f"未知服务器 {server!r}（可选：{', '.join(SERVER_TARGETS)}）"]
    if server == "local":
        return []
    errs: list[str] = []
    env = _read_dotenv_keys(repo_root)
    if not (env.get("MC_USER") and env.get("MC_PASS")):
        errs.append("code/mcagent/.env 缺队伍正式账号 MC_USER / MC_PASS")
    if use_llm and not env.get("LLM_KEY"):
        errs.append("比赛服只能用官方模型（LLM_PROFILE=official），但 .env 没填 LLM_KEY；填上 key，或关掉 LLM 用纯规则跑")
    if not account_reserved:
        errs.append("比赛账号是全队独占资源：先在 docs/05 账号表占位，再确认「已占位」（AGENTS.md §3）")
    return errs


def validate_bot_user(name: str) -> str:
    norm = (name or "").strip()
    if not BOT_USER_RE.match(norm):
        raise ValueError(f"角色名 {name!r} 非法：只能用字母、数字、下划线，长度 3–13")
    return norm

SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
CATEGORY_RANK = {"bug": 0, "strategy": 1}

QUOTA_INPUT_TOKENS = 8_000_000
QUOTA_OUTPUT_TOKENS = 1_500_000
MATCH_DURATION_S = 8 * 3600

# set-goal 热改每轮时长/步数的合法区间（越界直接拒绝，不写入 state.json）
EPISODE_SECONDS_BOUNDS: tuple[float, float] = (60.0, 3600.0)
EPISODE_STEPS_BOUNDS: tuple[int, int] = (10, 600)


def validate_episode_budget(seconds: Optional[float], steps: Optional[int]) -> str:
    """校验热覆盖的每轮时长/步数；返回空串表示合法，否则返回中文原因。None 表示不改该项。"""
    errs: list[str] = []
    if seconds is not None:
        lo, hi = EPISODE_SECONDS_BOUNDS
        try:
            s_val = float(seconds)
        except (TypeError, ValueError):
            s_val = float("nan")
        if not (math.isfinite(s_val) and lo <= s_val <= hi):
            errs.append(f"episode_seconds={seconds!r} 超出允许区间 [{lo:g}, {hi:g}]")
    if steps is not None:
        lo_n, hi_n = EPISODE_STEPS_BOUNDS
        if isinstance(steps, bool) or not isinstance(steps, int) or not (lo_n <= steps <= hi_n):
            errs.append(f"episode_steps={steps!r} 超出允许区间 [{lo_n}, {hi_n}]")
    return "；".join(errs)


def effective_episode_budget(
    st: dict[str, Any], default_seconds: float, default_steps: int
) -> tuple[float, int]:
    """取本轮生效的时长/步数：state.json 里有合法热覆盖就用覆盖值，否则沿用启动参数。"""
    o_sec = st.get("episode_seconds")
    o_steps = st.get("episode_steps")
    if validate_episode_budget(o_sec, o_steps):
        print(
            f"[runner] state.json 中的热覆盖轮长非法（episode_seconds={o_sec!r}, episode_steps={o_steps!r}），"
            "本轮沿用启动参数。"
        )
        return float(default_seconds), int(default_steps)
    return (
        float(o_sec) if o_sec is not None else float(default_seconds),
        int(o_steps) if o_steps is not None else int(default_steps),
    )


def _nonneg_float(val: Any) -> float:
    try:
        x = float(val)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) and x > 0.0 else 0.0


def format_episode_value(
    prompt_tokens: int,
    completion_tokens: int,
    elapsed_s: float,
    *,
    price_in_per_m: Optional[float] = None,
    price_out_per_m: Optional[float] = None,
) -> str:
    """按初赛公告额度占比（含 8h 外推）与可选单价（默认 0 不显示金额）格式化一轮 token 价值。"""
    p_tok = max(0, int(prompt_tokens))
    c_tok = max(0, int(completion_tokens))
    if p_tok == 0 and c_tok == 0:
        return "-"

    in_pct = p_tok / QUOTA_INPUT_TOKENS * 100.0
    out_pct = c_tok / QUOTA_OUTPUT_TOKENS * 100.0
    parts = [f"额度 入{in_pct:.2f}%/出{out_pct:.2f}%"]
    if elapsed_s > 0.0:
        scale_8h = MATCH_DURATION_S / elapsed_s
        proj_in_pct = (p_tok * scale_8h) / QUOTA_INPUT_TOKENS * 100.0
        proj_out_pct = (c_tok * scale_8h) / QUOTA_OUTPUT_TOKENS * 100.0
        parts[0] += f"（8h≈入{proj_in_pct:.1f}%/出{proj_out_pct:.1f}%）"

    p_in = _nonneg_float(os.environ.get("LLM_PRICE_IN_PER_M", "0")) if price_in_per_m is None else max(0.0, price_in_per_m)
    p_out = _nonneg_float(os.environ.get("LLM_PRICE_OUT_PER_M", "0")) if price_out_per_m is None else max(0.0, price_out_per_m)
    if p_in > 0.0 or p_out > 0.0:
        cost = (p_tok / 1_000_000.0) * p_in + (c_tok / 1_000_000.0) * p_out
        cost_str = f"¥{cost:.4f}"
        if elapsed_s > 0.0:
            cost_str += f"（8h≈¥{cost * (MATCH_DURATION_S / elapsed_s):.2f}）"
        parts.append(cost_str)

    return " · ".join(parts)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_repo_root(start: Optional[Path] = None) -> Path:
    cur = (start or Path(__file__).resolve()).resolve()
    if cur.is_file():
        cur = cur.parent
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(cur),
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip()).resolve()


def validate_target_branch(branch: str) -> str:
    b = (branch or "").strip()
    if not b:
        raise ValueError("目标分支名不能为空")
    if b in PROTECTED_BRANCHES or b.startswith("refs/heads/main"):
        raise ValueError(f"安全护栏拒绝：目标分支不能是受保护主干 {b!r}，请使用独立待审分支（如 feat/soak-autofix）")
    if not re.match(r"^[A-Za-z0-9._/\-]+$", b):
        raise ValueError(f"非法分支名: {b!r}")
    return b


def is_allowed_edit_path(
    rel_path: str,
    *,
    narrow_files: Optional[Sequence[str]] = None,
) -> tuple[bool, str]:
    norm = rel_path.strip().lstrip("./").replace("\\", "/")
    if not norm:
        return False, "空路径"
    if norm in FORBIDDEN_EDIT_PATHS:
        return False, f"命中冻结保护文件 {norm}"
    base = Path(norm).name
    if base.startswith(".env") or base.startswith(".token") or "__pycache__" in norm:
        return False, f"禁止提交敏感配置或缓存文件 {norm}"
    if not any(fnmatch.fnmatch(norm, pat) for pat in ALLOWED_EDIT_PATTERNS):
        return False, f"不在自动修复允许白名单内: {norm}"
    if narrow_files:
        allowed_narrow = {
            p.strip().lstrip("./").replace("\\", "/")
            for p in narrow_files
            if p and p.strip()
        }
        if allowed_narrow and not (
            norm in allowed_narrow or fnmatch.fnmatch(norm, "code/tests/test_*.py")
        ):
            return (
                False,
                f"超出目标单收窄白名单（仅限 {sorted(allowed_narrow)} 及 code/tests/test_*.py）: {norm}",
            )
    return True, ""


def _max_attempts_for(st: dict[str, Any], inc_rec: dict[str, Any]) -> int:
    if inc_rec.get("code") == "GOAL_MISS":
        return int(st.get("max_goal_attempts", 4))
    return int(st.get("max_attempts", 2))


def scan_diff_for_secrets(diff_text: str) -> list[str]:
    hits: list[str] = []
    for line in diff_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        m = SECRET_SCAN_RE.search(line)
        if m:
            hits.append(m.group(0)[:12] + "***")
    return hits


@contextlib.contextmanager
def locked_state(session_dir: Path) -> Iterator[dict[str, Any]]:
    session_dir.mkdir(parents=True, exist_ok=True)
    lock_path = session_dir / ".state.lock"
    state_path = session_dir / "state.json"
    with lock_path.open("a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            if state_path.exists():
                data = json.loads(state_path.read_text(encoding="utf-8"))
            else:
                data = {}
            yield data
            tmp = state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(state_path)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def read_state(session_dir: Path) -> dict[str, Any]:
    state_path = session_dir / "state.json"
    if not state_path.exists():
        raise FileNotFoundError(f"未找到会话状态文件: {state_path}（请先运行 init）")
    with locked_state(session_dir) as st:
        return json.loads(json.dumps(st))


@contextlib.contextmanager
def locked_fixer_wt(session_dir: Path) -> Iterator[None]:
    """序列化对 fixer worktree 的独占访问，确保运行期 set-goal 热合入新目标时绝不与 fixer-loop 撞车。"""
    session_dir.mkdir(parents=True, exist_ok=True)
    lock_path = session_dir / ".fixer_wt.lock"
    with lock_path.open("a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _uv_env(wt_dir: Path, *, player_model: str = "") -> dict[str, str]:
    """为指定 worktree 构造独立运行环境，显式锁定 PYTHONPATH 防 editable .pth 跨目录串包。"""
    env = dict(os.environ)
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    env.setdefault("UV_LINK_MODE", "copy")
    env["PYTHONPATH"] = str((wt_dir / "code").resolve())
    norm_model = (player_model or "").strip()
    if norm_model:
        env["LLM_CUSTOM_MODEL"] = norm_model
    local_bin = str((Path.home() / ".local" / "bin").resolve())
    cur_path = env.get("PATH", "")
    if local_bin not in cur_path.split(os.pathsep):
        env["PATH"] = f"{local_bin}{os.pathsep}{cur_path}" if cur_path else local_bin
    return env


def _wire_worktree_env(repo_root: Path, wt_dir: Path) -> None:
    src_env = repo_root / "code" / "mcagent" / ".env"
    dst_env = wt_dir / "code" / "mcagent" / ".env"
    if src_env.exists() and not dst_env.exists() and not dst_env.is_symlink():
        dst_env.parent.mkdir(parents=True, exist_ok=True)
        dst_env.symlink_to(src_env.resolve())


def _clean_fixer_worktree(fixer_wt: Path) -> None:
    """仅在隔离的 fixer worktree 内丢弃未通过门禁的脏改动（严守 §6 不用 git reset --hard / checkout -- .）。"""
    _git(fixer_wt, "restore", "--staged", "--worktree", ".", check=False)
    _git(fixer_wt, "clean", "-fd", check=False)


def _branch_tip(repo_root: Path, branch: str) -> str:
    return _git(repo_root, "rev-parse", f"refs/heads/{branch}").stdout.strip()


def init_session(
    repo_root: Path,
    session_dir: Path,
    *,
    target_branch: str,
    base_ref: str = "HEAD",
    max_attempts: int = 2,
    target_goal: str = "",
    max_goal_attempts: int = 4,
    player_model: str = DEFAULT_PLAYER_MODEL,
) -> dict[str, Any]:
    branch = validate_target_branch(target_branch)
    session_dir = session_dir.resolve()
    wt_root = session_dir / "worktrees"
    fixer_wt = wt_root / "fixer"
    runner_wt = wt_root / "runner"
    (session_dir / "episodes").mkdir(parents=True, exist_ok=True)
    (session_dir / "fixer_runs").mkdir(parents=True, exist_ok=True)

    base_sha = _git(repo_root, "rev-parse", base_ref).stdout.strip()
    exists = _git(repo_root, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0
    if not exists:
        _git(repo_root, "branch", branch, base_sha)

    tip_sha = _branch_tip(repo_root, branch)
    if not fixer_wt.exists():
        wt_root.mkdir(parents=True, exist_ok=True)
        _git(repo_root, "worktree", "add", str(fixer_wt), branch)
    if not runner_wt.exists():
        wt_root.mkdir(parents=True, exist_ok=True)
        _git(repo_root, "worktree", "add", "--detach", str(runner_wt), tip_sha)

    # 若主干（base_ref）已有新提交未进入已有待审分支，自动在 fixer_wt 同步跟进并清理上次 Ctrl+C 遗留脏状态
    with locked_fixer_wt(session_dir):
        _clean_fixer_worktree(fixer_wt)
        _git(fixer_wt, "checkout", branch)
        if tip_sha != base_sha and _git(repo_root, "merge-base", "--is-ancestor", base_sha, tip_sha, check=False).returncode != 0:
            mproc = _git(fixer_wt, "merge", "--no-edit", base_sha, check=False)
            if mproc.returncode != 0:
                _git(fixer_wt, "merge", "--abort", check=False)
                _clean_fixer_worktree(fixer_wt)
                print(
                    f"[init] 警告: 待审分支 {branch} 自动同步 {base_sha[:7]} 存在冲突，保留原尖端 {tip_sha[:7]}",
                    file=sys.stderr,
                )
            else:
                tip_sha = _branch_tip(repo_root, branch)
        _git(runner_wt, "checkout", "--detach", tip_sha)

    _wire_worktree_env(repo_root, fixer_wt)
    _wire_worktree_env(repo_root, runner_wt)

    norm_goal = (target_goal or "").strip()
    norm_player_model = (player_model if player_model is not None else DEFAULT_PLAYER_MODEL).strip()
    with locked_state(session_dir) as st:
        if not st:
            st.update({
                "session_id": session_dir.name,
                "created_at": _utc_now(),
                "repo_root": str(repo_root),
                "target_branch": branch,
                "base_commit": base_sha,
                "current_tip": tip_sha,
                "runner_wt": str(runner_wt),
                "fixer_wt": str(fixer_wt),
                "max_attempts": int(max_attempts),
                "target_goal": norm_goal,
                "max_goal_attempts": int(max_goal_attempts),
                "player_model": norm_player_model,
                "episodes": [],
                "incidents": {},
                "commits": [],
            })
        else:
            st["current_tip"] = tip_sha
            # 回收上次进程 Ctrl+C 中断留下的孤儿 claimed 状态
            for rec in st.get("incidents", {}).values():
                if rec.get("status") == "claimed":
                    rec["status"] = "pending"
            if norm_goal:
                st["target_goal"] = norm_goal
            else:
                st.setdefault("target_goal", "")
            if "max_goal_attempts" not in st or int(max_goal_attempts) != 4:
                st["max_goal_attempts"] = int(max_goal_attempts)
            if "player_model" not in st or norm_player_model != DEFAULT_PLAYER_MODEL:
                st["player_model"] = norm_player_model
    write_review_report(session_dir)
    return read_state(session_dir)


def _summarize_episode_dir(ep_dir: Path) -> dict[str, Any]:
    inc_dir = ep_dir / "incidents"
    inc_files = sorted(inc_dir.glob("INC-*.json")) if inc_dir.exists() else []
    bugs_count = 0
    p0_bugs = 0
    parsed_incidents: list[tuple[Path, dict[str, Any]]] = []
    for f in inc_files:
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        parsed_incidents.append((f, obj))
        if obj.get("category") == "bug":
            bugs_count += 1
            if obj.get("severity") == "P0":
                p0_bugs += 1

    server_score = 0
    inferred_score = 0
    curve_prompt = 0
    curve_comp = 0
    curve_total = 0
    elapsed_s = 0.0
    curve_path = ep_dir / "progress_curve.jsonl"
    if curve_path.exists():
        for line in curve_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pt = json.loads(line)
                server_score = max(server_score, int(pt.get("server_score_v2", 0)))
                inferred_score = max(inferred_score, int(pt.get("inferred_score_v2", 0)))
                curve_prompt = max(curve_prompt, int(pt.get("prompt_tokens", 0) or 0))
                curve_comp = max(curve_comp, int(pt.get("completion_tokens", 0) or 0))
                curve_total = max(curve_total, int(pt.get("total_tokens", 0) or 0))
                elapsed_s = max(elapsed_s, float(pt.get("elapsed_s", 0.0) or 0.0))
            except Exception:
                pass

    skills_ok = 0
    tl_sum_prompt = 0
    tl_sum_comp = 0
    tl_cum_prompt = 0
    tl_cum_comp = 0
    tl_run_end_total = 0
    source = "local-target"
    deaths_recovered_count = 0
    run_end_deaths: Optional[int] = None
    stop_reason = ""
    target_goal = ""
    target_goal_reached = False
    target_goal_elapsed_s: Optional[float] = None
    goals_reached: dict[str, float] = {}
    tl_path = ep_dir / "timeline.jsonl"
    if tl_path.exists():
        for line in tl_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                if "elapsed_s" in ev:
                    elapsed_s = max(elapsed_s, float(ev.get("elapsed_s", 0.0) or 0.0))
                ev_type = ev.get("event")
                if ev_type == "run_start":
                    if ev.get("source"):
                        source = str(ev["source"])
                    if ev.get("target_goal"):
                        target_goal = str(ev["target_goal"])
                elif ev_type == "death_recovered":
                    deaths_recovered_count += 1
                elif ev_type == "goal_reached" and ev.get("goal_id"):
                    goals_reached.setdefault(str(ev["goal_id"]), float(ev.get("elapsed_s", 0.0) or 0.0))
                elif ev_type == "skill_end" and ev.get("outcome") == "completed":
                    skills_ok += 1
                elif ev_type == "llm_call":
                    tl_sum_prompt += int(ev.get("prompt_tokens", 0) or 0)
                    tl_sum_comp += int(ev.get("completion_tokens", 0) or 0)
                    tl_cum_prompt = max(tl_cum_prompt, int(ev.get("cumulative_prompt_tokens", 0) or 0))
                    tl_cum_comp = max(tl_cum_comp, int(ev.get("cumulative_completion_tokens", 0) or 0))
                elif ev_type == "run_end":
                    elapsed_s = max(elapsed_s, float(ev.get("elapsed_s", elapsed_s) or 0.0))
                    tl_cum_prompt = max(tl_cum_prompt, int(ev.get("prompt_tokens", 0) or 0))
                    tl_cum_comp = max(tl_cum_comp, int(ev.get("completion_tokens", 0) or 0))
                    tl_run_end_total = max(tl_run_end_total, int(ev.get("total_tokens", 0) or 0))
                    if ev.get("source"):
                        source = str(ev["source"])
                    if ev.get("deaths") is not None:
                        run_end_deaths = int(ev.get("deaths") or 0)
                    if ev.get("stop_reason"):
                        stop_reason = str(ev["stop_reason"])
                    if ev.get("target_goal"):
                        target_goal = str(ev["target_goal"])
                    if "target_goal_reached" in ev:
                        target_goal_reached = bool(ev.get("target_goal_reached"))
                    if ev.get("target_goal_elapsed_s") is not None:
                        target_goal_elapsed_s = float(ev["target_goal_elapsed_s"])
                    if isinstance(ev.get("goals_reached"), dict):
                        for k, v in ev["goals_reached"].items():
                            try:
                                goals_reached[str(k)] = float(v)
                            except (TypeError, ValueError):
                                pass
            except Exception:
                pass

    if target_goal and target_goal in goals_reached:
        target_goal_reached = True
        if target_goal_elapsed_s is None:
            target_goal_elapsed_s = goals_reached[target_goal]

    deaths = run_end_deaths if run_end_deaths is not None else deaths_recovered_count
    prompt_tokens = max(curve_prompt, tl_cum_prompt, tl_sum_prompt)
    completion_tokens = max(curve_comp, tl_cum_comp, tl_sum_comp)
    total_tokens = max(prompt_tokens + completion_tokens, curve_total, tl_run_end_total)

    return {
        "bugs_count": bugs_count,
        "p0_bugs": p0_bugs,
        "server_score": server_score,
        "inferred_score": inferred_score,
        "skills_completed": skills_ok,
        "elapsed_s": elapsed_s,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "source": source,
        "deaths": deaths,
        "stop_reason": stop_reason,
        "target_goal": target_goal,
        "target_goal_reached": target_goal_reached,
        "target_goal_elapsed_s": target_goal_elapsed_s,
        "goals_reached": goals_reached,
        "parsed_incidents": parsed_incidents,
    }


def update_session_goal_telemetry(session_dir: Path, episodes: list[dict[str, Any]]) -> dict[str, Path]:
    """按运行环境 source 物理隔离聚合非回滚轮次的目标实测成本（严守 AGENTS.md §4）。"""
    by_source: dict[str, list[dict[str, Any]]] = {}
    for ep in episodes:
        if ep.get("rolled_back"):
            continue
        src = str(ep.get("source") or "local-target").strip() or "local-target"
        by_source.setdefault(src, []).append(ep)

    written: dict[str, Path] = {}
    for src, source_eps in by_source.items():
        goal_ids: list[str] = []
        seen_gids: set[str] = set()
        for ep in source_eps:
            for gid in (ep.get("goals_reached") or {}):
                gid_s = str(gid).strip()
                if gid_s and gid_s not in seen_gids:
                    seen_gids.add(gid_s)
                    goal_ids.append(gid_s)
            tg = str(ep.get("target_goal") or "").strip()
            if tg and tg not in seen_gids:
                seen_gids.add(tg)
                goal_ids.append(tg)

        goals_stats: dict[str, dict[str, Any]] = {}
        for gid in goal_ids:
            ep_pool = [
                ep for ep in source_eps
                if not ep.get("target_goal")
                or ep.get("target_goal") == gid
                or gid in (ep.get("goals_reached") or {})
            ]
            if not ep_pool:
                ep_pool = list(source_eps)
            hit_times = sorted(
                float(ep["goals_reached"][gid])
                for ep in source_eps
                if gid in (ep.get("goals_reached") or {})
            )
            hit_tokens = sorted(
                int(ep.get("total_tokens", 0) or 0)
                for ep in source_eps
                if gid in (ep.get("goals_reached") or {})
            )
            hits = len(hit_times)
            n_eps = max(1, len(ep_pool))
            hit_rate = round(hits / n_eps, 3)
            p50_elapsed_s = round(statistics.median(hit_times), 1) if hit_times else None
            p50_tokens = int(statistics.median(hit_tokens)) if hit_tokens else 0
            total_deaths = sum(int(ep.get("deaths", 0) or 0) for ep in ep_pool)
            goals_stats[gid] = {
                "episodes": len(ep_pool),
                "hits": hits,
                "hit_rate": hit_rate,
                "p50_elapsed_s": p50_elapsed_s,
                "p50_tokens": p50_tokens,
                "total_deaths": total_deaths,
            }

        safe_src = re.sub(r"[^A-Za-z0-9._\-]", "_", src)
        out_path = session_dir / f"goal_telemetry.{safe_src}.json"
        payload = {
            "source": src,
            "updated_at": _utc_now(),
            "note": (
                f"AGENTS.md §4 环境与测试隔离落盘：本文件仅聚合 source={src!r} 的挂机实测成本，"
                "本地靶场数据严禁混同为官方赛服结论。"
            ),
            "episodes_total": len(source_eps),
            "goals": goals_stats,
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written[src] = out_path
    return written


def ingest_episode_and_check_rollback(
    session_dir: Path,
    ep_id: str,
    ep_dir: Path,
    commit_sha: str,
    bot_user: str,
) -> dict[str, Any]:
    summary = _summarize_episode_dir(ep_dir)
    parsed_incidents = summary.pop("parsed_incidents")
    rolled_back_sha: Optional[str] = None
    revert_reason: Optional[str] = None

    with locked_state(session_dir) as st:
        repo_root = Path(st["repo_root"])
        fixer_wt = Path(st["fixer_wt"])
        branch = st["target_branch"]

        # 检查是否存在尚未经过整轮 campaign 验证的新自动修复提交
        prev_good = next(
            (e for e in reversed(st["episodes"]) if e.get("p0_bugs", 0) == 0 and not e.get("rolled_back")),
            st["episodes"][-1] if st["episodes"] else None,
        )
        unverified_commit = next(
            (c for c in reversed(st["commits"]) if c["sha"] == commit_sha and c.get("status") == "pending_canary_ep"),
            None,
        )

        if unverified_commit is not None:
            prev_p0 = int(prev_good.get("p0_bugs", 0)) if prev_good else 0
            commit_code = str(
                unverified_commit.get("code")
                or st["incidents"].get(unverified_commit.get("fingerprint", ""), {}).get("code", "")
            )
            if summary["p0_bugs"] > prev_p0:
                revert_reason = "p0_bug_regression"
            elif prev_good and (prev_good.get("goals_reached") or {}):
                prev_best_score = max(
                    int(prev_good.get("server_score", 0) or 0),
                    int(prev_good.get("inferred_score", 0) or 0),
                )
                if prev_best_score > 0 and commit_code == "GOAL_MISS":
                    tgt_set = {summary["target_goal"]} if summary.get("target_goal") else set()
                    prev_prereqs = set((prev_good.get("goals_reached") or {}).keys()) - tgt_set
                    cur_reached = set((summary.get("goals_reached") or {}).keys())
                    cur_best_score = max(
                        int(summary.get("server_score", 0) or 0),
                        int(summary.get("inferred_score", 0) or 0),
                    )
                    if prev_prereqs and not (prev_prereqs & cur_reached) and cur_best_score < prev_best_score:
                        revert_reason = "prereq_score_regression"

            if revert_reason is not None:
                # 发现自动修复引入了新 P0 bug 或破坏了前置目标得分：当场在待审分支自动 revert 回退（仅当 fixer 工位干净时执行）
                dirty = _git(fixer_wt, "status", "--porcelain", check=False).stdout.strip()
                if not dirty and _branch_tip(repo_root, branch) == commit_sha:
                    rev = _git(fixer_wt, "revert", "--no-edit", commit_sha, check=False)
                    if rev.returncode == 0:
                        rolled_back_sha = commit_sha
                        unverified_commit["status"] = "reverted"
                        unverified_commit["reverted_by_ep"] = ep_id
                        unverified_commit["revert_reason"] = revert_reason
                        fp = unverified_commit.get("fingerprint", "")
                        if fp in st["incidents"]:
                            st["incidents"][fp]["status"] = "rolled_back"
                            st["incidents"][fp]["revert_reason"] = revert_reason
                        st["current_tip"] = _branch_tip(repo_root, branch)
                if rolled_back_sha is None:
                    unverified_commit["status"] = "flagged_regression"
                    unverified_commit["revert_reason"] = revert_reason
            else:
                unverified_commit["status"] = "active"
                unverified_commit["validated_by_ep"] = ep_id

        # 吸收本轮新发现的 bug / strategy 异常单
        for inc_path, inc_obj in parsed_incidents:
            cat = str(inc_obj.get("category", ""))
            if cat not in ("bug", "strategy"):
                continue
            skill_val = str(inc_obj.get("skill") or (inc_obj.get("skill_context") or {}).get("skill") or "")
            inc_id_val = str(inc_obj.get("incident_id") or inc_obj.get("id") or inc_path.stem)
            goal_id_val = str(inc_obj.get("goal_id") or (inc_obj.get("detail") or {}).get("goal_id") or "")
            editable_files_val = [
                str(x)
                for x in (inc_obj.get("editable_files") or (inc_obj.get("detail") or {}).get("editable_files") or [])
                if x
            ]
            fp = str(inc_obj.get("fingerprint") or f"{inc_obj.get('code')}:{skill_val}")
            rec = st["incidents"].get(fp)
            if rec is None:
                st["incidents"][fp] = {
                    "fingerprint": fp,
                    "incident_id": inc_id_val,
                    "code": inc_obj.get("code", ""),
                    "severity": inc_obj.get("severity", "P2"),
                    "category": cat,
                    "skill": skill_val,
                    "goal_id": goal_id_val,
                    "editable_files": editable_files_val,
                    "status": "pending",
                    "attempts": 0,
                    "first_seen_ep": ep_id,
                    "last_seen_ep": ep_id,
                    "seen_commits": [commit_sha],
                    "incident_path": str(inc_path.resolve()),
                    "fix_commit": None,
                    "gate_history": [],
                }
            else:
                rec["last_seen_ep"] = ep_id
                rec["incident_path"] = str(inc_path.resolve())
                if skill_val and not rec.get("skill"):
                    rec["skill"] = skill_val
                if goal_id_val:
                    rec["goal_id"] = goal_id_val
                if editable_files_val:
                    rec["editable_files"] = editable_files_val
                if commit_sha not in rec.get("seen_commits", []):
                    rec.setdefault("seen_commits", []).append(commit_sha)
                # 若之前标为 fixed 但在包含 fix_commit 的轮次（含 commit_sha == fix_commit 当轮）仍复发，说明未根治
                if rec["status"] == "fixed" and rec.get("fix_commit"):
                    anc = _git(repo_root, "merge-base", "--is-ancestor", rec["fix_commit"], commit_sha, check=False)
                    if anc.returncode == 0:
                        rec["last_failed_fix_commit"] = rec["fix_commit"]
                        rec["fix_commit"] = None
                        if int(rec.get("attempts", 0)) < _max_attempts_for(st, rec):
                            rec["status"] = "pending"
                        else:
                            rec["status"] = "escalated_human"
                elif rec["status"] == "resolved_by_run":
                    if int(rec.get("attempts", 0)) < _max_attempts_for(st, rec):
                        rec["status"] = "pending"

        # 若本轮成功达成聚焦目标，同步标记对应 GOAL_MISS 已由实跑达成，避免 Fixer 空转认领
        gid = str(summary.get("target_goal") or "").strip()
        if gid and summary.get("target_goal_reached"):
            miss_rec = st["incidents"].get(f"GOAL_MISS:{gid}")
            if miss_rec is not None and miss_rec.get("status") in ("pending", "fixed"):
                miss_rec["goal_achieved_ep"] = ep_id
                if miss_rec.get("status") == "pending":
                    miss_rec["status"] = "resolved_by_run"

        ep_meta = {
            "ep_id": ep_id,
            "at": _utc_now(),
            "commit": commit_sha,
            "bot_user": bot_user,
            "run_dir": str(ep_dir.resolve()),
            "rolled_back": rolled_back_sha,
            "revert_reason": revert_reason if rolled_back_sha else None,
            **summary,
        }
        st["episodes"].append(ep_meta)
        st["current_tip"] = _branch_tip(repo_root, branch)
        episodes_snap = list(st["episodes"])

    update_session_goal_telemetry(session_dir, episodes_snap)
    write_review_report(session_dir)
    return ep_meta


def _render_fixer_prompt(inc_rec: dict[str, Any], inc_json: dict[str, Any], fixer_wt: Path) -> str:
    tape_preview = json.dumps((inc_json.get("http_tape") or [])[-10:], ensure_ascii=False, indent=2)
    inc_path = inc_rec["incident_path"]
    skill_ctx = inc_json.get("skill_context") or {}
    detail = inc_json.get("detail") or {}
    skill_args = inc_json.get("skill_args") if inc_json.get("skill_args") is not None else skill_ctx.get("args", {})
    outcome = inc_json.get("outcome") if inc_json.get("outcome") is not None else skill_ctx.get("outcome")
    error_code = inc_json.get("error_code") if inc_json.get("error_code") is not None else skill_ctx.get("error_code")
    error_msg = (
        inc_json.get("error_msg")
        if inc_json.get("error_msg") is not None
        else (skill_ctx.get("error_msg") or detail.get("summary"))
    )

    is_goal_miss = inc_rec.get("code") == "GOAL_MISS"
    goal_id = str(inc_rec.get("goal_id") or inc_json.get("goal_id") or detail.get("goal_id") or "")
    narrow_files = [
        str(p)
        for p in (inc_rec.get("editable_files") or inc_json.get("editable_files") or detail.get("editable_files") or [])
        if p
    ]

    goal_section = ""
    if is_goal_miss:
        goal_section = f"""
## 1B. 目标未达成诊断（GOAL_MISS）
- **目标 (`goal_id`)**：`{goal_id}` — {detail.get('goal_title', '')}（层级 `{detail.get('tier', '')}`，分值 `{detail.get('score', 0)}`）
- **卡点摘要**：{detail.get('summary', '')}
- **耗时 / 预算 / 结束原因**：`{detail.get('elapsed_s')}`s / `{detail.get('budget_s')}`s / `{detail.get('stop_reason')}`
- **瓶颈目标 / 瓶颈技能**：`{detail.get('bottleneck_goal_id', '')}` / `{detail.get('bottleneck_skill', '')}`（目标主技能是否已启动：`{detail.get('target_skill_started')}`）
- **已完成前置 (`completed_prereqs`)**：`{json.dumps(detail.get('completed_prereqs', []), ensure_ascii=False)}`
- **未满足前置 (`missing_prereqs`)**：`{json.dumps(detail.get('missing_prereqs', []), ensure_ascii=False)}`
- **物资/判据缺口 (`missing_items`)**：`{json.dumps(detail.get('missing_items', []), ensure_ascii=False)}`
- **最近一次 Replan (`last_replan`)**：`{json.dumps(detail.get('last_replan'), ensure_ascii=False)}`
- **技能执行统计 (`skill_stats`)**：`{json.dumps(detail.get('skill_stats', {}), ensure_ascii=False)}`\n"""

    if is_goal_miss and narrow_files:
        narrow_list_str = "、".join(f"`{f}`" for f in narrow_files)
        whitelist_block = f"""2. **目标单收窄文件白名单（Gate 1 动态收窄强校验，越界直接拒绝并丢弃改动）**：
   - **本单只允许修改/新增**：{narrow_list_str} 以及 `code/tests/test_*.py`。
   - **严禁越界触碰其他模块**：修改 `code/mcagent/loop/policy.py`、其他无关 `skills/*.py` 或 `loop/*.py` 将被 Gate 1 直接拒绝！
   - **严禁触碰冻结底座**：`code/mcagent/client.py`、`code/mcagent/runtime.py`、`code/mcagent/skills/{{base,registry,testing}}.py`、`code/mcagent/planner/decision.py`、`AGENTS.md`、`.env*`。"""
    else:
        whitelist_block = """2. **文件白名单（严禁越界）**：
   - **只允许修改/新增**：`code/mcagent/loop/*.py`、`code/mcagent/skills/*.py`（不含冻结文件）、`code/tests/test_*.py`。
   - **严禁触碰**：`code/mcagent/client.py`、`code/mcagent/runtime.py`、`code/mcagent/skills/{base,registry,testing}.py`、`code/mcagent/planner/decision.py`、`AGENTS.md`、`.env*`。"""

    return f"""# 自动修单任务：{inc_rec['incident_id']} (`{inc_rec['code']}`)

你正在独立的修复工作树 `{fixer_wt}` 中修复挂机压测捕获的真实异常单。

## 1. 异常摘要
- **指纹 (`fingerprint`)**：`{inc_rec['fingerprint']}`
- **级别 / 类别**：`{inc_rec['severity']}` / `{inc_rec['category']}`
- **触发技能**：`{inc_rec['skill']}` · 参数：`{json.dumps(skill_args, ensure_ascii=False)}`
- **结果 / 报错码**：`{outcome}` / `{error_code}`
- **错误信息**：`{error_msg}`
- **后置断言信息**：`{inc_json.get('postcondition_error')}`
- **异常包完整路径**：`{inc_path}`
{goal_section}
## 2. 最近 HTTP 交互磁带切片（末尾 10 条）
```json
{tape_preview}
```

## 3. 修复步骤与硬性红线（机器门禁将逐项核查，违规直接丢弃改动）
1. **复现与定位**：可在 `{fixer_wt}/code` 下运行 `uv run python -m mcagent.loop --replay {shlex.quote(inc_path)}` 查看确定性回放，并阅读相关技能与规划代码。
{whitelist_block}
3. **补充离线回归单测**：在 `code/tests/` 中针对该根因补充至少 1 条确定性单测，确保 `cd {fixer_wt}/code && uv run python -m unittest discover -s tests` 全部通过。
4. **不要自己执行 `git commit`**：保留工作区修改即可，外层 `soak_ctl.py verify-and-promote` 会独立运行四道门禁并统一提交到待审分支。
"""


def claim_next_incident(session_dir: Path) -> Optional[dict[str, Any]]:
    with locked_state(session_dir) as st:
        fixer_wt = Path(st["fixer_wt"])
        candidates = []
        for fp, rec in st.get("incidents", {}).items():
            if rec.get("status") != "pending":
                continue
            max_att = _max_attempts_for(st, rec)
            if int(rec.get("attempts", 0)) >= max_att:
                rec["status"] = "escalated_human"
                continue
            rank = (
                SEVERITY_RANK.get(str(rec.get("severity", "P2")), 9),
                CATEGORY_RANK.get(str(rec.get("category", "strategy")), 9),
                int(rec.get("attempts", 0)),
            )
            candidates.append((rank, fp, rec))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        _, fp, chosen = candidates[0]
        chosen["status"] = "claimed"
        chosen["claimed_at"] = _utc_now()

        attempt_no = sum(len(r.get("gate_history", [])) for r in st["incidents"].values()) + 1
        attempt_dir = session_dir / "fixer_runs" / f"attempt-{attempt_no:04d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        inc_path = Path(chosen["incident_path"])
        inc_json = json.loads(inc_path.read_text(encoding="utf-8")) if inc_path.exists() else {}
        prompt_file = attempt_dir / "prompt.md"
        prompt_file.write_text(_render_fixer_prompt(chosen, inc_json, fixer_wt), encoding="utf-8")
        chosen["active_attempt_dir"] = str(attempt_dir)
        chosen["prompt_file"] = str(prompt_file)
        return json.loads(json.dumps(chosen))


def verify_and_promote(
    session_dir: Path,
    fingerprint: str,
    *,
    live_canary: bool = True,
    fixer_bot_user: str = DEFAULT_FIXER_USER,
    canary_max_steps: int = 10,
    canary_max_seconds: float = 45.0,
) -> dict[str, Any]:
    """独立四道机器闸门核验：任一闸门不过则自动清理 fixer 工位脏改动并累计失败次数。"""
    st_snap = read_state(session_dir)
    repo_root = Path(st_snap["repo_root"])
    fixer_wt = Path(st_snap["fixer_wt"])
    branch = st_snap["target_branch"]
    inc_rec = st_snap["incidents"].get(fingerprint)
    if inc_rec is None:
        raise KeyError(f"未知的异常指纹: {fingerprint}")

    attempt_dir = Path(inc_rec.get("active_attempt_dir") or (session_dir / "fixer_runs" / "manual"))
    attempt_dir.mkdir(parents=True, exist_ok=True)
    uv_env = _uv_env(fixer_wt)

    def _fail(gate: str, detail: str) -> dict[str, Any]:
        _clean_fixer_worktree(fixer_wt)
        with locked_state(session_dir) as st:
            rec = st["incidents"][fingerprint]
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            max_att = _max_attempts_for(st, rec)
            rec["status"] = "escalated_human" if rec["attempts"] >= max_att else "pending"
            entry = {"at": _utc_now(), "ok": False, "failed_gate": gate, "detail": detail[:1000]}
            rec.setdefault("gate_history", []).append(entry)
        write_review_report(session_dir)
        return {"ok": False, "failed_gate": gate, "detail": detail}

    # ---------------- Gate 1: 修改文件白名单 + 密钥泄露扫描 ----------------
    porcelain = _git(fixer_wt, "status", "--porcelain").stdout.splitlines()
    changed_files: list[str] = []
    for line in porcelain:
        if len(line) < 4:
            continue
        raw = line[3:].strip()
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1].strip()
        changed_files.append(raw.strip('"'))

    if not changed_files:
        return _fail("gate1_whitelist", "fixer 工位没有任何文件改动")

    narrow_files = (
        inc_rec.get("editable_files")
        if inc_rec.get("code") == "GOAL_MISS" and inc_rec.get("editable_files")
        else None
    )
    for rel in changed_files:
        ok, why = is_allowed_edit_path(rel, narrow_files=narrow_files)
        if not ok:
            return _fail("gate1_whitelist", why)

    diff_txt = _git(fixer_wt, "diff").stdout
    for untracked in changed_files:
        p = fixer_wt / untracked
        if p.is_file():
            diff_txt += "\n+" + "\n+".join(p.read_text(encoding="utf-8", errors="ignore").splitlines())
    secret_hits = scan_diff_for_secrets(diff_txt)
    if secret_hits:
        return _fail("gate1_secrets", f"检测到疑似敏感凭据片段: {secret_hits}")

    # ---------------- Gate 2: 离线语法编译 + 全量单测 + 干跑契约 ----------------
    code_dir = fixer_wt / "code"
    for step_name, cmd in (
        ("compileall", ["uv", "run", "python", "-m", "compileall", "-q", "mcagent", "tests"]),
        ("unittest", ["uv", "run", "python", "-m", "unittest", "discover", "-s", "tests"]),
        ("loop_dry", ["uv", "run", "python", "-m", "mcagent.loop", "--dry"]),
    ):
        proc = subprocess.run(cmd, cwd=str(code_dir), env=uv_env, capture_output=True, text=True, timeout=180)
        (attempt_dir / f"gate2_{step_name}.log").write_text(
            (proc.stdout or "") + "\n" + (proc.stderr or ""), encoding="utf-8"
        )
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-600:]
            return _fail(f"gate2_{step_name}", tail)

    # ---------------- Gate 3: 独立测试号真机 focus / goal 金丝雀小灶验收 ----------------
    skill_name = str(inc_rec.get("skill") or "").strip()
    goal_id = str(inc_rec.get("goal_id") or "").strip()
    is_goal_canary = inc_rec.get("code") == "GOAL_MISS" and bool(goal_id)
    if live_canary and (is_goal_canary or skill_name):
        canary_dir = attempt_dir / "live_canary"
        if is_goal_canary:
            cmd = [
                "uv", "run", "python", "-m", "mcagent.loop",
                "--mode", "campaign",
                "--goal", goal_id,
                "--soak",
                "--local-target", "--live",
                "--user", fixer_bot_user,
                "--run-dir", str(canary_dir),
                "--max-steps", str(canary_max_steps),
                "--max-seconds", str(canary_max_seconds),
                "--disconnect-on-exit",
            ]
        else:
            cmd = [
                "uv", "run", "python", "-m", "mcagent.loop",
                "--mode", "focus",
                "--skill", skill_name,
                "--soak",
                "--local-target", "--live",
                "--user", fixer_bot_user,
                "--run-dir", str(canary_dir),
                "--max-steps", str(canary_max_steps),
                "--max-seconds", str(canary_max_seconds),
                "--disconnect-on-exit",
            ]
        canary_env = _uv_env(fixer_wt, player_model=(st_snap.get("player_model") or "").strip())
        proc = subprocess.run(cmd, cwd=str(code_dir), env=canary_env, capture_output=True, text=True, timeout=int(canary_max_seconds + 45))
        (attempt_dir / "gate3_live_canary.log").write_text(
            (proc.stdout or "") + "\n" + (proc.stderr or ""), encoding="utf-8"
        )
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-600:]
            return _fail("gate3_live_canary_exit", tail)
        canary_summary = _summarize_episode_dir(canary_dir)
        if canary_summary["bugs_count"] > 0:
            return _fail("gate3_live_canary_bugs", f"金丝雀验收仍触发 {canary_summary['bugs_count']} 个 bug 异常单")

    # ---------------- Gate 4: 提交到单一待审分支并通知 Runner 下一轮热切换 ----------------
    _git(fixer_wt, "add", *changed_files)
    commit_msg = (
        f"fix(soak-autofix): {skill_name or goal_id or 'loop'} resolve {inc_rec.get('incident_id')} "
        f"[{inc_rec.get('code')}]"
    )
    _git(fixer_wt, "commit", "-m", commit_msg)
    new_sha = _git(fixer_wt, "rev-parse", "HEAD").stdout.strip()

    with locked_state(session_dir) as st:
        rec = st["incidents"][fingerprint]
        rec["attempts"] = int(rec.get("attempts", 0)) + 1
        rec["status"] = "fixed"
        rec["fix_commit"] = new_sha
        rec.setdefault("gate_history", []).append({
            "at": _utc_now(),
            "ok": True,
            "commit": new_sha,
            "changed_files": changed_files,
            "live_canary": bool(live_canary and (is_goal_canary or skill_name)),
        })
        st["commits"].append({
            "sha": new_sha,
            "at": _utc_now(),
            "fingerprint": fingerprint,
            "incident_id": inc_rec.get("incident_id"),
            "code": inc_rec.get("code", ""),
            "goal_id": goal_id,
            "skill": skill_name,
            "files": changed_files,
            "status": "pending_canary_ep",
        })
        st["current_tip"] = _branch_tip(repo_root, branch)

    write_review_report(session_dir)
    return {"ok": True, "commit": new_sha, "changed_files": changed_files, "branch": branch}


def run_runner_loop(
    session_dir: Path,
    *,
    episodes: int = 1,
    episode_seconds: float = 180.0,
    episode_steps: int = 40,
    user_prefix: str = "agy_soak_r",
    rotate_user: bool = True,
    use_llm: bool = True,
    target_goal: str = "",
    player_model: Optional[str] = None,
    server: str = "local",
) -> list[dict[str, Any]]:
    if server not in SERVER_TARGETS:
        raise ValueError(f"未知服务器 {server!r}")
    results: list[dict[str, Any]] = []
    if (target_goal or "").strip() or player_model is not None:
        with locked_state(session_dir) as st_init:
            if st_init:
                if (target_goal or "").strip():
                    st_init["target_goal"] = target_goal.strip()
                if player_model is not None:
                    st_init["player_model"] = player_model.strip()
    idx = 0
    while episodes <= 0 or idx < episodes:
        if (session_dir / "STOP").exists():
            print("[runner] 检测到 STOP 标志文件，安全退出挂机循环。")
            break
        st = read_state(session_dir)
        active_goal = (st.get("target_goal") or "").strip()
        active_model = (st.get("player_model") or "").strip()
        ep_seconds, ep_steps = effective_episode_budget(st, episode_seconds, episode_steps)
        if episodes <= 0 and active_goal:
            goal_inc = st.get("incidents", {}).get(f"GOAL_MISS:{active_goal}", {})
            if goal_inc.get("status") == "escalated_human":
                print(
                    f"[runner] 聚焦目标 {active_goal} 已超最大自动尝试轮数并升级人工（escalated_human），自动止损退出。"
                )
                (session_dir / "STOP").write_text(_utc_now(), encoding="utf-8")
                break
        idx += 1
        repo_root = Path(st["repo_root"])
        runner_wt = Path(st["runner_wt"])
        branch = st["target_branch"]
        ep_num = len(st["episodes"]) + 1
        ep_id = f"ep-{ep_num:04d}"
        ep_dir = session_dir / "episodes" / ep_id

        # 热接力：轮次起点原子切换到待审分支最新已验证 commit
        tip_sha = _branch_tip(repo_root, branch)
        _git(runner_wt, "checkout", "--detach", tip_sha)

        if server == "official":
            bot_user = "official-account"  # 比赛服只能用 .env 里的正式账号，不传 --user
        else:
            bot_user = f"{user_prefix}{ep_num:03d}" if rotate_user else user_prefix
        prev_good = next(
            (e for e in reversed(st["episodes"]) if e.get("p0_bugs", 0) == 0 and not e.get("rolled_back")),
            st["episodes"][-1] if st["episodes"] else None,
        )

        cmd = [
            "uv", "run", "python", "-m", "mcagent.loop",
            "--mode", "campaign",
            "--soak",
            *(["--live"] if server == "official" else ["--local-target", "--live", "--user", bot_user]),
            "--world-snapshot", f"{ep_id}-{tip_sha[:7]}",
            "--run-dir", str(ep_dir),
            "--max-steps", str(ep_steps),
            "--max-seconds", str(ep_seconds),
            "--disconnect-on-exit",
        ]
        if active_goal:
            cmd.extend(["--goal", active_goal])
        telemetry_path = session_dir / "goal_telemetry.local-target.json"
        if telemetry_path.exists():
            cmd.extend(["--goal-telemetry", str(telemetry_path)])
        if use_llm:
            cmd.append("--llm")
        if prev_good and Path(prev_good["run_dir"]).exists():
            cmd.extend(["--baseline", prev_good["run_dir"]])

        print(
            f"[runner] 启动 {ep_id} @ {tip_sha[:7]} "
            f"(server={server}, bot={bot_user}, "
            f"model={('official' if server == 'official' else (active_model or 'env')) if use_llm else 'rule'}, "
            f"goal={active_goal or 'all'}, max={ep_seconds:g}s/{ep_steps} 步)..."
        )
        run_env = _uv_env(runner_wt, player_model=active_model if (use_llm and server == "local") else "")
        if server == "official":
            run_env["MC_API_BASE"] = OFFICIAL_MC_BASE
            run_env["LLM_PROFILE"] = "official"
            run_env.pop("LLM_CUSTOM_MODEL", None)
        subprocess.run(
            cmd,
            cwd=str(runner_wt / "code"),
            env=run_env,
            check=False,
        )
        meta = ingest_episode_and_check_rollback(session_dir, ep_id, ep_dir, tip_sha, bot_user)
        results.append(meta)
        print(
            f"[runner] 完成 {ep_id}: skills_ok={meta['skills_completed']} bugs={meta['bugs_count']} "
            f"score={meta['server_score']}/{meta['inferred_score']} "
            f"tokens={meta['prompt_tokens']}/{meta['completion_tokens']} rolled_back={meta['rolled_back']}"
        )
    return results


def run_fixer_once(
    session_dir: Path,
    *,
    fixer_cmd_template: str,
    fixer_timeout_s: int = 600,
    live_canary: bool = True,
    fixer_bot_user: str = DEFAULT_FIXER_USER,
    fixer_preset_name: str = "",
) -> Optional[dict[str, Any]]:
    with locked_fixer_wt(session_dir):
        claimed = claim_next_incident(session_dir)
        if claimed is None:
            return None
        if fixer_preset_name:
            with locked_state(session_dir) as st_rec:
                rec = st_rec.get("incidents", {}).get(claimed["fingerprint"])
                if rec is not None:
                    rec["last_fixer_preset"] = fixer_preset_name
        st = read_state(session_dir)
        fixer_wt = Path(st["fixer_wt"])
        branch = st["target_branch"]

        # 确保 fixer 工位处于待审分支最新干净状态
        _clean_fixer_worktree(fixer_wt)
        _git(fixer_wt, "checkout", branch)

        cmd_str = fixer_cmd_template.format(
            fixer_wt=shlex.quote(str(fixer_wt)),
            prompt_file=shlex.quote(str(claimed["prompt_file"])),
            incident_file=shlex.quote(str(claimed["incident_path"])),
            skill=shlex.quote(str(claimed.get("skill", ""))),
        )
        attempt_dir = Path(claimed["active_attempt_dir"])
        print(
            f"[fixer] 认领 {claimed['incident_id']} ({claimed['fingerprint']}) -> 执行修复器"
            f"{' ' + fixer_preset_name if fixer_preset_name else ''}..."
        )
        try:
            proc = subprocess.run(
                ["bash", "-lc", cmd_str],
                cwd=str(fixer_wt),
                env=_uv_env(fixer_wt),
                capture_output=True,
                text=True,
                timeout=fixer_timeout_s,
            )
            (attempt_dir / "fixer_agent.log").write_text(
                (proc.stdout or "") + "\n" + (proc.stderr or ""), encoding="utf-8"
            )
        except subprocess.TimeoutExpired:
            _clean_fixer_worktree(fixer_wt)
            return verify_and_promote(
                session_dir, claimed["fingerprint"], live_canary=live_canary, fixer_bot_user=fixer_bot_user
            )

        res = verify_and_promote(
            session_dir,
            claimed["fingerprint"],
            live_canary=live_canary,
            fixer_bot_user=fixer_bot_user,
        )
        print(f"[fixer] 门禁结果 ({claimed['incident_id']}): {res}")
        return res


def _rollback_branch_in_fixer_wt(fixer_wt: Path, branch: str, prev_tip: str) -> None:
    """无 git reset --hard 回退 fixer_wt 上的待审分支到 prev_tip。"""
    _clean_fixer_worktree(fixer_wt)
    _git(fixer_wt, "checkout", "--detach", prev_tip)
    _git(fixer_wt, "branch", "-f", branch, prev_tip)
    _git(fixer_wt, "checkout", branch)


def inject_and_set_goal(
    session_dir: Path,
    *,
    merge_ref: str = "",
    target_goal: Optional[str] = None,
    max_goal_attempts: Optional[int] = None,
    player_model: Optional[str] = None,
    episode_seconds: Optional[float] = None,
    episode_steps: Optional[int] = None,
    clear_episode_budget: bool = False,
    fixer_preset: Optional[str] = None,
    skip_offline_gate: bool = False,
) -> dict[str, Any]:
    """在不停机的情况下向待审分支热合入新目标/初版技能提交，并原子切换下一轮聚焦目标。

    `episode_seconds` / `episode_steps` 写进 state.json，Runner 下一轮起生效；
    `clear_episode_budget=True` 清掉热覆盖，回到 `run` 启动参数。越界时在合入前直接拒绝。
    """
    if fixer_preset is not None and fixer_preset not in FIXER_PRESETS:
        return {"ok": False, "failed_step": "invalid_fixer_preset", "detail": f"未知修单预设 {fixer_preset!r}"}
    budget_err = validate_episode_budget(episode_seconds, episode_steps)
    if budget_err:
        return {"ok": False, "failed_step": "invalid_episode_budget", "detail": budget_err}
    if clear_episode_budget and (episode_seconds is not None or episode_steps is not None):
        return {
            "ok": False,
            "failed_step": "invalid_episode_budget",
            "detail": "--clear-episode-budget 不能与 --episode-seconds / --episode-steps 同时使用",
        }
    with locked_fixer_wt(session_dir):
        st_snap = read_state(session_dir)
        claimed_fps = [
            fp for fp, rec in st_snap.get("incidents", {}).items() if rec.get("status") == "claimed"
        ]
        if claimed_fps:
            return {
                "ok": False,
                "failed_step": "fixer_busy",
                "detail": f"当前有手动认领中的异常单 {claimed_fps}，请先完成 verify-and-promote 再热合入",
            }

        repo_root = Path(st_snap["repo_root"])
        fixer_wt = Path(st_snap["fixer_wt"])
        branch = st_snap["target_branch"]
        prev_tip = _branch_tip(repo_root, branch)
        merged_sha: Optional[str] = None
        merged_files: list[str] = []

        norm_ref = (merge_ref or "").strip()
        if norm_ref:
            ver = _git(repo_root, "rev-parse", "--verify", norm_ref, check=False)
            if ver.returncode != 0:
                return {
                    "ok": False,
                    "failed_step": "invalid_ref",
                    "detail": f"无法解析 Git 引用 {norm_ref!r}: {(ver.stderr or ver.stdout).strip()}",
                }
            _clean_fixer_worktree(fixer_wt)
            _git(fixer_wt, "checkout", branch)
            mproc = _git(fixer_wt, "merge", "--no-ff", "--no-edit", norm_ref, check=False)
            if mproc.returncode != 0:
                _git(fixer_wt, "merge", "--abort", check=False)
                _clean_fixer_worktree(fixer_wt)
                return {
                    "ok": False,
                    "failed_step": "git_merge",
                    "detail": (mproc.stderr or mproc.stdout).strip(),
                }

            cur_tip = _branch_tip(repo_root, branch)
            if cur_tip != prev_tip:
                diff_txt = _git(fixer_wt, "diff", f"{prev_tip}..{cur_tip}").stdout
                secret_hits = scan_diff_for_secrets(diff_txt)
                if secret_hits:
                    _rollback_branch_in_fixer_wt(fixer_wt, branch, prev_tip)
                    return {
                        "ok": False,
                        "failed_step": "gate1_secrets",
                        "detail": f"检测到疑似敏感凭据片段: {secret_hits}",
                    }

                if not skip_offline_gate:
                    code_dir = fixer_wt / "code"
                    uv_env = _uv_env(fixer_wt)
                    for step_name, cmd in (
                        ("compileall", ["uv", "run", "python", "-m", "compileall", "-q", "mcagent", "tests"]),
                        ("unittest", ["uv", "run", "python", "-m", "unittest", "discover", "-s", "tests"]),
                        ("loop_dry", ["uv", "run", "python", "-m", "mcagent.loop", "--dry"]),
                    ):
                        proc = subprocess.run(
                            cmd, cwd=str(code_dir), env=uv_env, capture_output=True, text=True, timeout=180
                        )
                        if proc.returncode != 0:
                            _rollback_branch_in_fixer_wt(fixer_wt, branch, prev_tip)
                            tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-600:]
                            return {"ok": False, "failed_step": f"gate2_{step_name}", "detail": tail}

                merged_sha = cur_tip
                merged_files = [
                    ln.strip()
                    for ln in _git(fixer_wt, "diff", "--name-only", f"{prev_tip}..{cur_tip}").stdout.splitlines()
                    if ln.strip()
                ]

        with locked_state(session_dir) as st:
            st["current_tip"] = _branch_tip(repo_root, branch)
            if target_goal is not None:
                st["target_goal"] = target_goal.strip()
            if max_goal_attempts is not None:
                st["max_goal_attempts"] = int(max_goal_attempts)
            if player_model is not None:
                st["player_model"] = player_model.strip()
            if clear_episode_budget:
                st.pop("episode_seconds", None)
                st.pop("episode_steps", None)
            if episode_seconds is not None:
                st["episode_seconds"] = float(episode_seconds)
            if episode_steps is not None:
                st["episode_steps"] = int(episode_steps)
            if fixer_preset is not None:
                st["fixer_preset"] = fixer_preset
            if merged_sha:
                st["commits"].append({
                    "sha": merged_sha,
                    "at": _utc_now(),
                    "fingerprint": f"INJECT:{norm_ref}",
                    "incident_id": "MANUAL",
                    "code": "GOAL_INJECT",
                    "goal_id": st.get("target_goal", ""),
                    "skill": "",
                    "files": merged_files,
                    "status": "manual_inject",
                })
            (session_dir / "STOP").unlink(missing_ok=True)
            final_goal = st.get("target_goal", "")
            final_tip = st["current_tip"]
            final_budget = {
                "episode_seconds": st.get("episode_seconds"),
                "episode_steps": st.get("episode_steps"),
            }
            final_fixer = st.get("fixer_preset", "")

        write_review_report(session_dir)
        return {
            "ok": True,
            "target_branch": branch,
            "target_goal": final_goal,
            "episode_budget_override": final_budget,
            "fixer_preset": final_fixer,
            "previous_tip": prev_tip,
            "current_tip": final_tip,
            "merged_files": merged_files,
        }


def write_review_report(session_dir: Path) -> Path:
    st = read_state(session_dir)
    branch = st["target_branch"]
    base_sha = st["base_commit"][:7]
    tip_sha = st["current_tip"][:7]

    tot_in = sum(int(e.get("prompt_tokens", 0) or 0) for e in st["episodes"])
    tot_out = sum(int(e.get("completion_tokens", 0) or 0) for e in st["episodes"])
    tot_elapsed = sum(float(e.get("elapsed_s", 0.0) or 0.0) for e in st["episodes"])

    lines = [
        f"# 双工位挂机自愈待审报告（分支 `{branch}`）",
        "",
        f"- **会话目录**：`{session_dir}`",
        f"- **基线提交 → 当前分支尖端**：`{base_sha}` → `{tip_sha}`（目标待审分支：`{branch}`，**未自动合并 `main`**）",
        f"- **已完成挂机轮次**：`{len(st['episodes'])}` 轮 · **自动修复提交数**：`{len(st['commits'])}` 个",
        f"- **工位 A 服务器**：`{SERVER_TARGETS.get(st.get('server_target') or 'local', st.get('server_target'))}`",
        f"- **玩家操控模型（工位 A）**：`{'官方模型（LLM_PROFILE=official）' if st.get('server_target') == 'official' else (st.get('player_model') or '沿用 .env')}`",
        f"- **修单模型（工位 B）**：`{FIXER_PRESET_LABELS.get(st.get('fixer_preset') or '', st.get('fixer_preset') or '沿用启动参数')}`",
        f"- **累计 Token 花费**：输入 `{tot_in:,}` / 输出 `{tot_out:,}`（{format_episode_value(tot_in, tot_out, tot_elapsed)}）",
    ]
    if st.get("target_goal"):
        lines.append(
            f"- **本轮聚焦晋级目标**：`{st['target_goal']}`（最大自动尝试上限 `{st.get('max_goal_attempts', 4)}` 轮）"
        )
    o_sec, o_steps = st.get("episode_seconds"), st.get("episode_steps")
    if o_sec is not None or o_steps is not None:
        bad = validate_episode_budget(o_sec, o_steps)
        if bad:
            lines.append(f"- **每轮时长/步数热覆盖**：⚠️ 非法（{bad}），Runner 沿用启动参数")
        else:
            sec_txt = f"{float(o_sec):g}s" if o_sec is not None else "沿用启动参数"
            steps_txt = f"{int(o_steps)} 步" if o_steps is not None else "沿用启动参数"
            lines.append(f"- **每轮时长/步数热覆盖**：`{sec_txt}` / `{steps_txt}`（下一轮起生效）")

    lines.extend([
        "",
        "## 一、人工验收与合并命令（跑完后由人执行）",
        "",
        "```bash",
        f"# 1. 查看所有 AI 自动修复提交及改动差异",
        f"git log --oneline --stat {st['base_commit']}..{branch}",
        f"git diff {st['base_commit']}..{branch}",
        "",
        f"# 2. 审阅无误后合并回你的工作分支或 main",
        f"git checkout main && git merge --no-ff {branch}",
        "```",
        "",
        "## 二、自动修复提交清单",
        "",
    ])
    if not st["commits"]:
        lines.append("- 暂无自动修复提交。")
    else:
        lines.append("| 提交 SHA | 状态 | 修复异常单 | 技能 | 修改文件 |")
        lines.append("|---|---|---|---|---|")
        for c in st["commits"]:
            files_s = ", ".join(f"`{x}`" for x in c.get("files", []))
            st_s = f"`{c.get('status')}`"
            if c.get("revert_reason"):
                st_s += f" ({c['revert_reason']})"
            lines.append(
                f"| `{c['sha'][:7]}` | {st_s} | `{c.get('incident_id')}` | `{c.get('skill') or c.get('goal_id')}` | {files_s} |"
            )

    lines.extend(["", "## 三、异常单状态总览", ""])
    if not st["incidents"]:
        lines.append("- 暂未捕获 `bug` 或 `strategy` 级异常单。")
    else:
        lines.append("| 指纹 | 级别/类别 | 状态 | 尝试次数 | 修复提交 |")
        lines.append("|---|---|---|---:|---|")
        for fp, r in sorted(st["incidents"].items()):
            fix_s = f"`{r['fix_commit'][:7]}`" if r.get("fix_commit") else "-"
            lines.append(
                f"| `{fp}` | `{r['severity']}/{r['category']}` | `{r['status']}` | {r['attempts']} | {fix_s} |"
            )

    lines.extend(["", "## 四、各轮挂机指标曲线汇总", ""])
    if not st["episodes"]:
        lines.append("- 暂无已完成挂机轮次。")
    else:
        lines.append("| 轮次 | 提交 | 测试角色 | 完成技能数 | Bug 单数 | 服务端分 / 代理分 | 目标达成 | 输入/输出 token | 价值 | 回滚 |")
        lines.append("|---|---|---|---:|---:|---|---|---|---|---|")
        for e in st["episodes"]:
            if e.get("rolled_back"):
                rb = f"`{e['rolled_back'][:7]}`"
                if e.get("revert_reason"):
                    rb += f" ({e['revert_reason']})"
            else:
                rb = "-"
            if e.get("target_goal"):
                if e.get("target_goal_reached"):
                    t_el = e.get("target_goal_elapsed_s")
                    goal_s = (
                        f"✅ `{e['target_goal']}` ({float(t_el):.1f}s)"
                        if t_el is not None
                        else f"✅ `{e['target_goal']}`"
                    )
                else:
                    goal_s = f"❌ `{e['target_goal']}`"
            else:
                gr = e.get("goals_reached") or {}
                goal_s = f"{len(gr)} 项" if gr else "-"
            p_tok = int(e.get("prompt_tokens", 0) or 0)
            c_tok = int(e.get("completion_tokens", 0) or 0)
            el_s = float(e.get("elapsed_s", 0.0) or 0.0)
            tok_s = f"`{p_tok:,} / {c_tok:,}`"
            val_s = format_episode_value(p_tok, c_tok, el_s)
            lines.append(
                f"| `{e['ep_id']}` | `{e['commit'][:7]}` | `{e['bot_user']}` | "
                f"{e['skills_completed']} | {e['bugs_count']} | "
                f"{e['server_score']} / {e['inferred_score']} | {goal_s} | {tok_s} | {val_s} | {rb} |"
            )

    telemetry_rows: list[tuple[str, str, dict[str, Any]]] = []
    for tf in sorted(session_dir.glob("goal_telemetry.*.json")):
        try:
            tdata = json.loads(tf.read_text(encoding="utf-8"))
        except Exception:
            continue
        src = str(tdata.get("source") or tf.stem.removeprefix("goal_telemetry."))
        for gid, gstat in (tdata.get("goals") or {}).items():
            if isinstance(gstat, dict):
                telemetry_rows.append((str(gid), src, gstat))

    if telemetry_rows:
        lines.extend([
            "",
            "## 五、目标台账实测成本汇总",
            "",
            "| 目标 ID | 数据来源 | 达成/轮数 (达成率) | P50 用时 | 中位 Token | 累计死亡 |",
            "|---|---|---|---:|---:|---:|",
        ])
        for gid, src, gstat in telemetry_rows:
            hits = int(gstat.get("hits", 0) or 0)
            eps = int(gstat.get("episodes", 0) or 0)
            hr_pct = float(gstat.get("hit_rate", 0.0) or 0.0) * 100.0
            p50_s = gstat.get("p50_elapsed_s")
            p50_s_str = f"{float(p50_s):.1f}s" if p50_s is not None else "-"
            p50_tok = int(gstat.get("p50_tokens", 0) or 0)
            p50_tok_str = f"`{p50_tok:,}`" if p50_tok > 0 else "-"
            deaths = int(gstat.get("total_deaths", 0) or 0)
            lines.append(
                f"| `{gid}` | `{src}` | {hits}/{eps} ({hr_pct:.1f}%) | {p50_s_str} | {p50_tok_str} | {deaths} |"
            )

    report_path = session_dir / "REVIEW_REPORT.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def teardown_session(session_dir: Path) -> Path:
    st = read_state(session_dir)
    repo_root = Path(st["repo_root"])
    for key in ("runner_wt", "fixer_wt"):
        wt = Path(st[key])
        if wt.exists():
            _clean_fixer_worktree(wt)
            _git(repo_root, "worktree", "remove", "--force", str(wt), check=False)
    _git(repo_root, "worktree", "prune", check=False)
    return write_review_report(session_dir)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="soak_ctl.py", description="双工位挂机压测与 AI 自动修单热接力编排器")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="初始化双 worktree 与单一待审分支")
    p_init.add_argument("--session-dir", required=True)
    p_init.add_argument("--branch", default="feat/soak-autofix", help="自动修复汇入的待审分支（严禁 main）")
    p_init.add_argument("--base-ref", default="HEAD")
    p_init.add_argument("--max-attempts", type=int, default=2)
    p_init.add_argument("--goal", default="", help="会话级聚焦晋级目标 goal_id（空表示常规全目标挂机）")
    p_init.add_argument(
        "--max-goal-attempts",
        type=int,
        default=4,
        help="单目标 GOAL_MISS 自动修复最大尝试轮数上限（超限自动转交人工，防单目标死磕耗尽时间）",
    )
    p_init.add_argument(
        "--player-model",
        default=DEFAULT_PLAYER_MODEL,
        help=f"工位 A 玩游戏的 custom 模型名（默认 {DEFAULT_PLAYER_MODEL}；传空串则沿用 .env）",
    )

    p_run_ep = sub.add_parser("runner-loop", help="启动工位 A（挂机压测循环，轮间自动热切换新 commit）")
    p_run_ep.add_argument("--session-dir", required=True)
    p_run_ep.add_argument("--episodes", type=int, default=1, help="0 表示持续循环直到出现 STOP 文件")
    p_run_ep.add_argument("--episode-seconds", type=float, default=180.0)
    p_run_ep.add_argument("--episode-steps", type=int, default=40)
    p_run_ep.add_argument("--user-prefix", default="agy_soak_r")
    p_run_ep.add_argument("--fixed-user", action="store_true", help="不按轮次轮换角色名")
    p_run_ep.add_argument("--server", choices=tuple(SERVER_TARGETS), default="local",
                          help="工位 A 进哪个服务器：local 自建测试服 | official 比赛服")
    p_run_ep.add_argument("--goal", default="", help="覆盖会话级聚焦目标 goal_id")
    p_run_ep.add_argument("--player-model", default=None, help="覆盖会话级工位 A 玩家模型名")
    p_run_ep.add_argument(
        "--llm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="使用 LLM API 操纵角色选技能（默认开启；纯规则基线传 --no-llm）",
    )

    p_claim = sub.add_parser("claim", help="工位 B 原子认领最高优先级异常单并生成 prompt.md")
    p_claim.add_argument("--session-dir", required=True)

    p_ver = sub.add_parser("verify-and-promote", help="对 fixer 工位改动执行四道机器闸门并提交至待审分支")
    p_ver.add_argument("--session-dir", required=True)
    p_ver.add_argument("--fingerprint", required=True)
    p_ver.add_argument("--skip-live-canary", action="store_true")
    p_ver.add_argument("--fixer-user", default=DEFAULT_FIXER_USER)

    p_fix = sub.add_parser("fixer-loop", help="启动工位 B（轮询待修队列并调用 CLI Agent 自动修单）")
    p_fix.add_argument("--session-dir", required=True)
    p_fix.add_argument("--fixer-preset", choices=tuple(FIXER_PRESETS), default=None)
    p_fix.add_argument("--fixer-cmd", default="", help="自定义修复命令模板，支持 {fixer_wt} {prompt_file} {incident_file}")
    p_fix.add_argument("--poll-s", type=float, default=5.0)
    p_fix.add_argument("--once", action="store_true", help="只处理一单即退出")
    p_fix.add_argument("--skip-live-canary", action="store_true")
    p_fix.add_argument("--fixer-user", default=DEFAULT_FIXER_USER)
    p_fix.add_argument("--fixer-timeout", type=int, default=600)

    p_dual = sub.add_parser("run", help="一键并行启动 Runner + Fixer 双工位守护")
    p_dual.add_argument("--session-dir", required=True)
    p_dual.add_argument("--branch", default="feat/soak-autofix")
    p_dual.add_argument("--episodes", type=int, default=3)
    p_dual.add_argument("--episode-seconds", type=float, default=180.0)
    p_dual.add_argument("--episode-steps", type=int, default=40)
    p_dual.add_argument("--goal", default="", help="本轮聚焦晋级目标 goal_id")
    p_dual.add_argument(
        "--max-goal-attempts",
        type=int,
        default=4,
        help="单目标 GOAL_MISS 自动修复最大尝试轮数上限（超限自动转交人工，防单目标死磕耗尽时间）",
    )
    p_dual.add_argument(
        "--llm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="使用 LLM API 操纵角色选技能（默认开启，接入 run_runner_loop(use_llm=...)；纯规则传 --no-llm）",
    )
    p_dual.add_argument(
        "--player-model",
        default=DEFAULT_PLAYER_MODEL,
        help=f"工位 A 玩游戏的 custom 模型名（默认 {DEFAULT_PLAYER_MODEL}；传空串则沿用 .env）",
    )
    p_dual.add_argument(
        "--fixer-preset",
        choices=tuple(FIXER_PRESETS),
        default="codex",
        help="工位 B 修单入口预设：codex（默认 gpt-6.1-sol xhigh）| agy（gemini-4-hydrogen-high）| claude",
    )
    p_dual.add_argument(
        "--server",
        choices=tuple(SERVER_TARGETS),
        default="local",
        help="工位 A 进哪个服务器：local 自建测试服（默认）| official 比赛服（正式账号 + 官方模型，须先在 docs/05 占位）",
    )
    p_dual.add_argument(
        "--account-reserved",
        action="store_true",
        help="--server official 时必加：确认已在 docs/05 账号表占位比赛账号",
    )
    p_dual.add_argument(
        "--bot-user",
        default=DEFAULT_BOT_USER,
        help=f"工位 A 进服角色名（默认 {DEFAULT_BOT_USER}，每轮同名，轮次号只记在 ep-NNNN 记录里）",
    )
    p_dual.add_argument(
        "--rotate-user",
        action="store_true",
        help="每轮换新角色 <bot-user>001、002…（空包冷启动；不加则每轮同名、继承上一轮背包与进度）",
    )
    p_dual.add_argument("--fixer-cmd", default="")
    p_dual.add_argument("--fixer-timeout", type=int, default=600, help="单次 AI 修单命令超时秒数（默认 600s）")
    p_dual.add_argument("--skip-live-canary", action="store_true")

    p_set = sub.add_parser(
        "set-goal",
        help="不停机向待审分支热合入新目标/初版技能提交，并原子切换下一轮聚焦目标",
    )
    p_set.add_argument("--session-dir", required=True)
    p_set.add_argument(
        "--merge-ref",
        default="",
        help="可选：将指定分支或 commit（如 feat/goal-staging）经离线门禁核验后合入待审分支",
    )
    g_grp = p_set.add_mutually_exclusive_group()
    g_grp.add_argument("--goal", default=None, help="切换下一轮起跑的聚焦目标 goal_id")
    g_grp.add_argument("--clear-goal", action="store_true", help="清空单目标聚焦，切回常规全量已毕业目标挂机")
    p_set.add_argument("--max-goal-attempts", type=int, default=None, help="可选更新单目标最大自动尝试轮数")
    p_set.add_argument("--player-model", default=None, help="可选热切换下一轮起工位 A 玩家模型名")
    p_set.add_argument(
        "--fixer-preset",
        choices=tuple(FIXER_PRESETS),
        default=None,
        help="可选热切换工位 B 修单模型（下一单起生效），如 codex:gpt-6-astra:high、agy:gemini-4-carbon-medium",
    )
    p_set.add_argument(
        "--clear-episode-budget",
        action="store_true",
        help="清除每轮时长/步数热覆盖，回到 run 启动参数",
    )
    p_set.add_argument(
        "--episode-seconds",
        type=float,
        default=None,
        help=f"可选热改下一轮起每轮最长秒数（{EPISODE_SECONDS_BOUNDS[0]:g}–{EPISODE_SECONDS_BOUNDS[1]:g}）",
    )
    p_set.add_argument(
        "--episode-steps",
        type=int,
        default=None,
        help=f"可选热改下一轮起每轮最大步数（{EPISODE_STEPS_BOUNDS[0]}–{EPISODE_STEPS_BOUNDS[1]}）",
    )
    p_set.add_argument("--skip-offline-gate", action="store_true", help="仅测试夹具用：跳过合入后的离线编译与单测")

    p_st = sub.add_parser("status", help="查看当前待修队列与待审分支进度")
    p_st.add_argument("--session-dir", required=True)

    p_td = sub.add_parser("teardown", help="卸载临时双 worktree，保留待审分支与 REVIEW_REPORT.md 供人工合并")
    p_td.add_argument("--session-dir", required=True)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    session_dir = Path(args.session_dir).resolve()

    if args.cmd == "init":
        repo_root = find_repo_root()
        st = init_session(
            repo_root,
            session_dir,
            target_branch=args.branch,
            base_ref=args.base_ref,
            max_attempts=args.max_attempts,
            target_goal=args.goal,
            max_goal_attempts=args.max_goal_attempts,
            player_model=args.player_model,
        )
        print(json.dumps({
            "ok": True,
            "target_branch": st["target_branch"],
            "target_goal": st.get("target_goal", ""),
            "player_model": st.get("player_model", ""),
            "runner_wt": st["runner_wt"],
            "fixer_wt": st["fixer_wt"],
            "review_report": str(session_dir / "REVIEW_REPORT.md"),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "runner-loop":
        run_runner_loop(
            session_dir,
            episodes=args.episodes,
            episode_seconds=args.episode_seconds,
            episode_steps=args.episode_steps,
            user_prefix=args.user_prefix,
            rotate_user=not args.fixed_user,
            use_llm=args.llm,
            target_goal=args.goal,
            player_model=args.player_model,
            server=args.server,
        )
        return 0

    if args.cmd == "claim":
        claimed = claim_next_incident(session_dir)
        print(json.dumps(claimed, ensure_ascii=False, indent=2))
        return 0 if claimed else 3

    if args.cmd == "verify-and-promote":
        res = verify_and_promote(
            session_dir,
            args.fingerprint,
            live_canary=not args.skip_live_canary,
            fixer_bot_user=args.fixer_user,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.cmd == "fixer-loop":
        tmpl = args.fixer_cmd or (FIXER_PRESETS[args.fixer_preset] if args.fixer_preset else "")
        if not tmpl:
            print("错误: fixer-loop 需要传 --fixer-preset {codex,agy,claude} 或 --fixer-cmd", file=sys.stderr)
            return 2
        default_name = "custom" if args.fixer_cmd else args.fixer_preset
        while not (session_dir / "STOP").exists():
            cur_name, cur_tmpl = resolve_fixer_template(session_dir, default_name, tmpl)
            res = run_fixer_once(
                session_dir,
                fixer_cmd_template=cur_tmpl,
                fixer_timeout_s=args.fixer_timeout,
                live_canary=not args.skip_live_canary,
                fixer_bot_user=args.fixer_user,
                fixer_preset_name=cur_name,
            )
            if args.once:
                return 0 if (res is None or res.get("ok")) else 1
            if res is None:
                time.sleep(max(0.5, args.poll_s))
        return 0

    if args.cmd == "run":
        try:
            bot_user = validate_bot_user(args.bot_user)
        except ValueError as exc:
            print(f"[run] {exc}", file=sys.stderr)
            return 2
        repo_root = find_repo_root()
        server_errs = check_server_target(
            repo_root, args.server, use_llm=args.llm, account_reserved=args.account_reserved
        )
        if server_errs:
            for msg in server_errs:
                print(f"[run] 不能启动：{msg}", file=sys.stderr)
            return 2
        init_session(
            repo_root,
            session_dir,
            target_branch=args.branch,
            target_goal=args.goal,
            max_goal_attempts=args.max_goal_attempts,
            player_model=args.player_model,
        )
        (session_dir / "STOP").unlink(missing_ok=True)
        with locked_state(session_dir) as st_fx:
            st_fx["server_target"] = args.server
            if args.fixer_cmd:
                st_fx.pop("fixer_preset", None)
            else:
                st_fx["fixer_preset"] = args.fixer_preset
        tmpl = args.fixer_cmd or FIXER_PRESETS[args.fixer_preset]
        script = str(Path(__file__).resolve())
        runner_cmd = [
            sys.executable, script, "runner-loop",
            "--session-dir", str(session_dir),
            "--episodes", str(args.episodes),
            "--episode-seconds", str(args.episode_seconds),
            "--episode-steps", str(args.episode_steps),
            "--user-prefix", bot_user,
            "--server", args.server,
        ]
        if not args.rotate_user:
            runner_cmd.append("--fixed-user")
        if (args.goal or "").strip():
            runner_cmd.extend(["--goal", args.goal.strip()])
        runner_cmd.append("--llm" if args.llm else "--no-llm")
        fixer_cmd = [
            sys.executable, script, "fixer-loop",
            "--session-dir", str(session_dir),
            "--fixer-cmd", tmpl,
            "--fixer-timeout", str(args.fixer_timeout),
        ]
        if args.skip_live_canary:
            fixer_cmd.append("--skip-live-canary")

        r_proc = subprocess.Popen(runner_cmd)
        f_proc = subprocess.Popen(fixer_cmd)
        try:
            r_code = r_proc.wait()
            # Runner 跑满指定轮次后，再处理完最后一轮残余待修单即优雅收尾
            (session_dir / "STOP").write_text(_utc_now(), encoding="utf-8")
            f_proc.wait(timeout=30)
            return r_code
        except KeyboardInterrupt:
            (session_dir / "STOP").write_text(_utc_now(), encoding="utf-8")
            for proc in (r_proc, f_proc):
                if proc.poll() is None:
                    proc.send_signal(signal.SIGINT)
            return 130
        finally:
            write_review_report(session_dir)

    if args.cmd == "set-goal":
        tgt_goal: Optional[str] = "" if args.clear_goal else args.goal
        res = inject_and_set_goal(
            session_dir,
            merge_ref=args.merge_ref,
            target_goal=tgt_goal,
            max_goal_attempts=args.max_goal_attempts,
            player_model=args.player_model,
            episode_seconds=args.episode_seconds,
            episode_steps=args.episode_steps,
            clear_episode_budget=args.clear_episode_budget,
            fixer_preset=args.fixer_preset,
            skip_offline_gate=args.skip_offline_gate,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.cmd == "status":
        st = read_state(session_dir)
        report = write_review_report(session_dir)
        print(json.dumps({
            "target_branch": st["target_branch"],
            "target_goal": st.get("target_goal", ""),
            "player_model": st.get("player_model", ""),
            "fixer_preset": st.get("fixer_preset", ""),
            "server_target": st.get("server_target", "local"),
            "episode_budget_override": {
                "episode_seconds": st.get("episode_seconds"),
                "episode_steps": st.get("episode_steps"),
            },
            "base_commit": st["base_commit"][:7],
            "current_tip": st["current_tip"][:7],
            "episodes": len(st["episodes"]),
            "commits": len(st["commits"]),
            "incidents_by_status": {
                k: sum(1 for r in st["incidents"].values() if r["status"] == k)
                for k in ("pending", "claimed", "fixed", "resolved_by_run", "rolled_back", "escalated_human")
            },
            "review_report": str(report),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "teardown":
        report = teardown_session(session_dir)
        print(f"[teardown] 已卸载临时双 worktree；待审分支与报告保留于: {report}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
