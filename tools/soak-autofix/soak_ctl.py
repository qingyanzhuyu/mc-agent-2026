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
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

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

FIXER_PRESETS: dict[str, str] = {
    "codex": "codex exec -C {fixer_wt} -s workspace-write - < {prompt_file}",
    "claude": 'cd {fixer_wt} && claude -p --allowedTools "Read,Edit,Write,Grep,Glob,Bash" < {prompt_file}',
}

SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
CATEGORY_RANK = {"bug": 0, "strategy": 1}


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


def is_allowed_edit_path(rel_path: str) -> tuple[bool, str]:
    norm = rel_path.strip().lstrip("./").replace("\\", "/")
    if not norm:
        return False, "空路径"
    if norm in FORBIDDEN_EDIT_PATHS:
        return False, f"命中冻结保护文件 {norm}"
    base = Path(norm).name
    if base.startswith(".env") or base.startswith(".token") or "__pycache__" in norm:
        return False, f"禁止提交敏感配置或缓存文件 {norm}"
    if any(fnmatch.fnmatch(norm, pat) for pat in ALLOWED_EDIT_PATTERNS):
        return True, ""
    return False, f"不在自动修复允许白名单内: {norm}"


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


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _uv_env(repo_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    primary_venv = repo_root / "code" / ".venv"
    if primary_venv.exists():
        env["UV_PROJECT_ENVIRONMENT"] = str(primary_venv.resolve())
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

    _wire_worktree_env(repo_root, fixer_wt)
    _wire_worktree_env(repo_root, runner_wt)

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
                "episodes": [],
                "incidents": {},
                "commits": [],
            })
        else:
            st["current_tip"] = tip_sha
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
            except Exception:
                pass

    skills_ok = 0
    elapsed_s = 0.0
    tl_path = ep_dir / "timeline.jsonl"
    if tl_path.exists():
        for line in tl_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                if ev.get("event") == "skill_end" and ev.get("outcome") == "completed":
                    skills_ok += 1
                if ev.get("event") == "run_end":
                    elapsed_s = float(ev.get("elapsed_s", elapsed_s))
            except Exception:
                pass

    return {
        "bugs_count": bugs_count,
        "p0_bugs": p0_bugs,
        "server_score": server_score,
        "inferred_score": inferred_score,
        "skills_completed": skills_ok,
        "elapsed_s": elapsed_s,
        "parsed_incidents": parsed_incidents,
    }


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

    with locked_state(session_dir) as st:
        repo_root = Path(st["repo_root"])
        fixer_wt = Path(st["fixer_wt"])
        branch = st["target_branch"]

        # 检查是否存在尚未经过整轮 campaign 验证的新自动修复提交
        prev_good = next(
            (e for e in reversed(st["episodes"]) if e.get("p0_bugs", 0) == 0),
            st["episodes"][-1] if st["episodes"] else None,
        )
        unverified_commit = next(
            (c for c in reversed(st["commits"]) if c["sha"] == commit_sha and c.get("status") == "pending_canary_ep"),
            None,
        )

        if unverified_commit is not None:
            prev_p0 = int(prev_good.get("p0_bugs", 0)) if prev_good else 0
            if summary["p0_bugs"] > prev_p0:
                # 发现自动修复引入了新 P0 bug：当场在待审分支自动 revert 回退（仅当 fixer 工位干净时执行）
                dirty = _git(fixer_wt, "status", "--porcelain", check=False).stdout.strip()
                if not dirty and _branch_tip(repo_root, branch) == commit_sha:
                    rev = _git(fixer_wt, "revert", "--no-edit", commit_sha, check=False)
                    if rev.returncode == 0:
                        rolled_back_sha = commit_sha
                        unverified_commit["status"] = "reverted"
                        unverified_commit["reverted_by_ep"] = ep_id
                        fp = unverified_commit.get("fingerprint", "")
                        if fp in st["incidents"]:
                            st["incidents"][fp]["status"] = "rolled_back"
                        st["current_tip"] = _branch_tip(repo_root, branch)
                if rolled_back_sha is None:
                    unverified_commit["status"] = "flagged_regression"
            else:
                unverified_commit["status"] = "active"
                unverified_commit["validated_by_ep"] = ep_id

        # 吸收本轮新发现的 bug / strategy 异常单
        for inc_path, inc_obj in parsed_incidents:
            cat = str(inc_obj.get("category", ""))
            if cat not in ("bug", "strategy"):
                continue
            fp = str(inc_obj.get("fingerprint") or f"{inc_obj.get('code')}:{inc_obj.get('skill')}")
            rec = st["incidents"].get(fp)
            if rec is None:
                st["incidents"][fp] = {
                    "fingerprint": fp,
                    "incident_id": inc_obj.get("incident_id", inc_path.stem),
                    "code": inc_obj.get("code", ""),
                    "severity": inc_obj.get("severity", "P2"),
                    "category": cat,
                    "skill": inc_obj.get("skill", ""),
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
                if commit_sha not in rec.get("seen_commits", []):
                    rec.setdefault("seen_commits", []).append(commit_sha)
                # 若之前标为 fixed 但在修复提交之后的轮次再次复发，说明未根治，若未超阈值则重新打开
                if rec["status"] == "fixed" and rec.get("fix_commit") and rec["fix_commit"] != commit_sha:
                    # 确认当前 commit_sha 包含 fix_commit 祖先却仍复发
                    anc = _git(repo_root, "merge-base", "--is-ancestor", rec["fix_commit"], commit_sha, check=False)
                    if anc.returncode == 0:
                        if int(rec.get("attempts", 0)) < int(st.get("max_attempts", 2)):
                            rec["status"] = "pending"
                        else:
                            rec["status"] = "escalated_human"

        ep_meta = {
            "ep_id": ep_id,
            "at": _utc_now(),
            "commit": commit_sha,
            "bot_user": bot_user,
            "run_dir": str(ep_dir.resolve()),
            "rolled_back": rolled_back_sha,
            **summary,
        }
        st["episodes"].append(ep_meta)
        st["current_tip"] = _branch_tip(repo_root, branch)

    write_review_report(session_dir)
    return ep_meta


def _render_fixer_prompt(inc_rec: dict[str, Any], inc_json: dict[str, Any], fixer_wt: Path) -> str:
    tape_preview = json.dumps((inc_json.get("http_tape") or [])[-10:], ensure_ascii=False, indent=2)
    inc_path = inc_rec["incident_path"]
    return f"""# 自动修单任务：{inc_rec['incident_id']} (`{inc_rec['code']}`)

你正在独立的修复工作树 `{fixer_wt}` 中修复挂机压测捕获的真实异常单。

## 1. 异常摘要
- **指纹 (`fingerprint`)**：`{inc_rec['fingerprint']}`
- **级别 / 类别**：`{inc_rec['severity']}` / `{inc_rec['category']}`
- **触发技能**：`{inc_rec['skill']}` · 参数：`{json.dumps(inc_json.get('skill_args', {}), ensure_ascii=False)}`
- **结果 / 报错码**：`{inc_json.get('outcome')}` / `{inc_json.get('error_code')}`
- **错误信息**：`{inc_json.get('error_msg')}`
- **后置断言信息**：`{inc_json.get('postcondition_error')}`
- **异常包完整路径**：`{inc_path}`

## 2. 最近 HTTP 交互磁带切片（末尾 10 条）
```json
{tape_preview}
```

## 3. 修复步骤与硬性红线（机器门禁将逐项核查，违规直接丢弃改动）
1. **复现与定位**：可在 `{fixer_wt}/code` 下运行 `uv run python -m mcagent.loop --replay {shlex.quote(inc_path)}` 查看确定性回放，并阅读相关技能与规划代码。
2. **文件白名单（严禁越界）**：
   - **只允许修改/新增**：`code/mcagent/loop/*.py`、`code/mcagent/skills/*.py`（不含冻结文件）、`code/tests/test_*.py`。
   - **严禁触碰**：`code/mcagent/client.py`、`code/mcagent/runtime.py`、`code/mcagent/skills/{{base,registry,testing}}.py`、`code/mcagent/planner/decision.py`、`AGENTS.md`、`.env*`。
3. **补充离线回归单测**：在 `code/tests/` 中针对该根因补充至少 1 条确定性单测，确保 `cd {fixer_wt}/code && uv run python -m unittest discover -s tests` 全部通过。
4. **不要自己执行 `git commit`**：保留工作区修改即可，外层 `soak_ctl.py verify-and-promote` 会独立运行四道门禁并统一提交到待审分支。
"""


def claim_next_incident(session_dir: Path) -> Optional[dict[str, Any]]:
    with locked_state(session_dir) as st:
        max_attempts = int(st.get("max_attempts", 2))
        fixer_wt = Path(st["fixer_wt"])
        candidates = []
        for fp, rec in st.get("incidents", {}).items():
            if rec.get("status") != "pending":
                continue
            if int(rec.get("attempts", 0)) >= max_attempts:
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
    fixer_bot_user: str = "agy_soak_fixer",
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
    uv_env = _uv_env(repo_root)

    def _fail(gate: str, detail: str) -> dict[str, Any]:
        _clean_fixer_worktree(fixer_wt)
        with locked_state(session_dir) as st:
            rec = st["incidents"][fingerprint]
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            max_att = int(st.get("max_attempts", 2))
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

    for rel in changed_files:
        ok, why = is_allowed_edit_path(rel)
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

    # ---------------- Gate 3: 独立测试号真机 focus 金丝雀小灶验收 ----------------
    skill_name = str(inc_rec.get("skill") or "").strip()
    if live_canary and skill_name:
        canary_dir = attempt_dir / "live_canary"
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
        proc = subprocess.run(cmd, cwd=str(code_dir), env=uv_env, capture_output=True, text=True, timeout=int(canary_max_seconds + 45))
        (attempt_dir / "gate3_live_canary.log").write_text(
            (proc.stdout or "") + "\n" + (proc.stderr or ""), encoding="utf-8"
        )
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-600:]
            return _fail("gate3_live_canary_exit", tail)
        canary_summary = _summarize_episode_dir(canary_dir)
        if canary_summary["bugs_count"] > 0:
            return _fail("gate3_live_canary_bugs", f"focus 金丝雀仍触发 {canary_summary['bugs_count']} 个 bug 异常单")

    # ---------------- Gate 4: 提交到单一待审分支并通知 Runner 下一轮热切换 ----------------
    _git(fixer_wt, "add", *changed_files)
    commit_msg = (
        f"fix(soak-autofix): {skill_name or 'loop'} resolve {inc_rec.get('incident_id')} "
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
            "live_canary": bool(live_canary and skill_name),
        })
        st["commits"].append({
            "sha": new_sha,
            "at": _utc_now(),
            "fingerprint": fingerprint,
            "incident_id": inc_rec.get("incident_id"),
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
    use_llm: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    idx = 0
    while episodes <= 0 or idx < episodes:
        if (session_dir / "STOP").exists():
            print("[runner] 检测到 STOP 标志文件，安全退出挂机循环。")
            break
        idx += 1
        st = read_state(session_dir)
        repo_root = Path(st["repo_root"])
        runner_wt = Path(st["runner_wt"])
        branch = st["target_branch"]
        ep_num = len(st["episodes"]) + 1
        ep_id = f"ep-{ep_num:04d}"
        ep_dir = session_dir / "episodes" / ep_id

        # 热接力：轮次起点原子切换到待审分支最新已验证 commit
        tip_sha = _branch_tip(repo_root, branch)
        _git(runner_wt, "checkout", "--detach", tip_sha)

        bot_user = f"{user_prefix}{ep_num:03d}" if rotate_user else user_prefix
        prev_good = next(
            (e for e in reversed(st["episodes"]) if e.get("p0_bugs", 0) == 0),
            st["episodes"][-1] if st["episodes"] else None,
        )

        cmd = [
            "uv", "run", "python", "-m", "mcagent.loop",
            "--mode", "campaign",
            "--soak",
            "--local-target", "--live",
            "--user", bot_user,
            "--world-snapshot", f"{ep_id}-{tip_sha[:7]}",
            "--run-dir", str(ep_dir),
            "--max-steps", str(episode_steps),
            "--max-seconds", str(episode_seconds),
            "--disconnect-on-exit",
        ]
        if use_llm:
            cmd.append("--llm")
        if prev_good and Path(prev_good["run_dir"]).exists():
            cmd.extend(["--baseline", prev_good["run_dir"]])

        print(f"[runner] 启动 {ep_id} @ {tip_sha[:7]} (bot={bot_user}, max={episode_seconds}s)...")
        subprocess.run(cmd, cwd=str(runner_wt / "code"), env=_uv_env(repo_root), check=False)
        meta = ingest_episode_and_check_rollback(session_dir, ep_id, ep_dir, tip_sha, bot_user)
        results.append(meta)
        print(
            f"[runner] 完成 {ep_id}: skills_ok={meta['skills_completed']} bugs={meta['bugs_count']} "
            f"score={meta['server_score']}/{meta['inferred_score']} rolled_back={meta['rolled_back']}"
        )
    return results


def run_fixer_once(
    session_dir: Path,
    *,
    fixer_cmd_template: str,
    fixer_timeout_s: int = 300,
    live_canary: bool = True,
    fixer_bot_user: str = "agy_soak_fixer",
) -> Optional[dict[str, Any]]:
    claimed = claim_next_incident(session_dir)
    if claimed is None:
        return None
    st = read_state(session_dir)
    repo_root = Path(st["repo_root"])
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
    print(f"[fixer] 认领 {claimed['incident_id']} ({claimed['fingerprint']}) -> 执行修复器...")
    try:
        proc = subprocess.run(
            ["bash", "-lc", cmd_str],
            cwd=str(fixer_wt),
            env=_uv_env(repo_root),
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


def write_review_report(session_dir: Path) -> Path:
    st = read_state(session_dir)
    branch = st["target_branch"]
    base_sha = st["base_commit"][:7]
    tip_sha = st["current_tip"][:7]

    lines = [
        f"# 双工位挂机自愈待审报告（分支 `{branch}`）",
        "",
        f"- **会话目录**：`{session_dir}`",
        f"- **基线提交 → 当前分支尖端**：`{base_sha}` → `{tip_sha}`（目标待审分支：`{branch}`，**未自动合并 `main`**）",
        f"- **已完成挂机轮次**：`{len(st['episodes'])}` 轮 · **自动修复提交数**：`{len(st['commits'])}` 个",
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
    ]
    if not st["commits"]:
        lines.append("- 暂无自动修复提交。")
    else:
        lines.append("| 提交 SHA | 状态 | 修复异常单 | 技能 | 修改文件 |")
        lines.append("|---|---|---|---|---|")
        for c in st["commits"]:
            files_s = ", ".join(f"`{x}`" for x in c.get("files", []))
            lines.append(
                f"| `{c['sha'][:7]}` | `{c.get('status')}` | `{c.get('incident_id')}` | `{c.get('skill')}` | {files_s} |"
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
        lines.append("| 轮次 | 提交 | 测试角色 | 完成技能数 | Bug 单数 | 服务端分 / 代理分 | 回滚 |")
        lines.append("|---|---|---|---:|---:|---|---|")
        for e in st["episodes"]:
            rb = f"`{e['rolled_back'][:7]}`" if e.get("rolled_back") else "-"
            lines.append(
                f"| `{e['ep_id']}` | `{e['commit'][:7]}` | `{e['bot_user']}` | "
                f"{e['skills_completed']} | {e['bugs_count']} | "
                f"{e['server_score']} / {e['inferred_score']} | {rb} |"
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

    p_run_ep = sub.add_parser("runner-loop", help="启动工位 A（挂机压测循环，轮间自动热切换新 commit）")
    p_run_ep.add_argument("--session-dir", required=True)
    p_run_ep.add_argument("--episodes", type=int, default=1, help="0 表示持续循环直到出现 STOP 文件")
    p_run_ep.add_argument("--episode-seconds", type=float, default=180.0)
    p_run_ep.add_argument("--episode-steps", type=int, default=40)
    p_run_ep.add_argument("--user-prefix", default="agy_soak_r")
    p_run_ep.add_argument("--fixed-user", action="store_true", help="不按轮次轮换角色名")
    p_run_ep.add_argument("--llm", action="store_true")

    p_claim = sub.add_parser("claim", help="工位 B 原子认领最高优先级异常单并生成 prompt.md")
    p_claim.add_argument("--session-dir", required=True)

    p_ver = sub.add_parser("verify-and-promote", help="对 fixer 工位改动执行四道机器闸门并提交至待审分支")
    p_ver.add_argument("--session-dir", required=True)
    p_ver.add_argument("--fingerprint", required=True)
    p_ver.add_argument("--skip-live-canary", action="store_true")
    p_ver.add_argument("--fixer-user", default="agy_soak_fixer")

    p_fix = sub.add_parser("fixer-loop", help="启动工位 B（轮询待修队列并调用 CLI Agent 自动修单）")
    p_fix.add_argument("--session-dir", required=True)
    p_fix.add_argument("--fixer-preset", choices=tuple(FIXER_PRESETS), default=None)
    p_fix.add_argument("--fixer-cmd", default="", help="自定义修复命令模板，支持 {fixer_wt} {prompt_file} {incident_file}")
    p_fix.add_argument("--poll-s", type=float, default=5.0)
    p_fix.add_argument("--once", action="store_true", help="只处理一单即退出")
    p_fix.add_argument("--skip-live-canary", action="store_true")
    p_fix.add_argument("--fixer-user", default="agy_soak_fixer")
    p_fix.add_argument("--fixer-timeout", type=int, default=300)

    p_dual = sub.add_parser("run", help="一键并行启动 Runner + Fixer 双工位守护")
    p_dual.add_argument("--session-dir", required=True)
    p_dual.add_argument("--branch", default="feat/soak-autofix")
    p_dual.add_argument("--episodes", type=int, default=3)
    p_dual.add_argument("--episode-seconds", type=float, default=180.0)
    p_dual.add_argument("--episode-steps", type=int, default=40)
    p_dual.add_argument("--fixer-preset", choices=tuple(FIXER_PRESETS), default="codex")
    p_dual.add_argument("--fixer-cmd", default="")
    p_dual.add_argument("--skip-live-canary", action="store_true")

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
        )
        print(json.dumps({
            "ok": True,
            "target_branch": st["target_branch"],
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
            print("错误: fixer-loop 需要传 --fixer-preset {codex,claude} 或 --fixer-cmd", file=sys.stderr)
            return 2
        while not (session_dir / "STOP").exists():
            res = run_fixer_once(
                session_dir,
                fixer_cmd_template=tmpl,
                fixer_timeout_s=args.fixer_timeout,
                live_canary=not args.skip_live_canary,
                fixer_bot_user=args.fixer_user,
            )
            if args.once:
                return 0 if (res is None or res.get("ok")) else 1
            if res is None:
                time.sleep(max(0.5, args.poll_s))
        return 0

    if args.cmd == "run":
        repo_root = find_repo_root()
        init_session(repo_root, session_dir, target_branch=args.branch)
        tmpl = args.fixer_cmd or FIXER_PRESETS[args.fixer_preset]
        script = str(Path(__file__).resolve())
        runner_cmd = [
            sys.executable, script, "runner-loop",
            "--session-dir", str(session_dir),
            "--episodes", str(args.episodes),
            "--episode-seconds", str(args.episode_seconds),
            "--episode-steps", str(args.episode_steps),
        ]
        fixer_cmd = [
            sys.executable, script, "fixer-loop",
            "--session-dir", str(session_dir),
            "--fixer-cmd", tmpl,
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

    if args.cmd == "status":
        st = read_state(session_dir)
        report = write_review_report(session_dir)
        print(json.dumps({
            "target_branch": st["target_branch"],
            "base_commit": st["base_commit"][:7],
            "current_tip": st["current_tip"][:7],
            "episodes": len(st["episodes"]),
            "commits": len(st["commits"]),
            "incidents_by_status": {
                k: sum(1 for r in st["incidents"].values() if r["status"] == k)
                for k in ("pending", "claimed", "fixed", "rolled_back", "escalated_human")
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
