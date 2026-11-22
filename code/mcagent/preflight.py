"""赛前切换自检：一条命令检查「开发配置」有没有被切成「比赛配置」。

    python -m mcagent.preflight                 # 只做静态检查（不联网、不登录）
    python -m mcagent.preflight --live          # 再打一次 /status 看延迟与鉴权
    python -m mcagent.preflight --apply         # 把 .env.match 覆盖到 .env，再检查
    python -m mcagent.preflight --env-file X    # 检查指定文件（试运行用）

退出码：0 = 可以开赛；1 = 有 BLOCK 项没过。
设计原则：**只检查、不改代码**；除 `--apply` 外不写任何文件。
"""
from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Sequence
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV = os.path.join(HERE, ".env")
DEFAULT_MATCH = os.path.join(HERE, ".env.match")

OFFICIAL_HOSTS = ("apiin.nmo.net.cn", "mc-api.windywindie.top")
CAMPUS_DIRECT_HOSTS = ("apiin.nmo.net.cn",)
LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0")
#: 群里发过的公共压测 key 前缀，正式 key 不应以它开头
TEST_KEY_PREFIX = "sk-mRbe"
#: 开发期常用的占位值
PLACEHOLDER_USERS = {"qyzy_dev", "dev_bot_1", "test", "Test_Agent", ""}
PLACEHOLDER_PASSES = {"localdev", ""}

BLOCK, WARN = "BLOCK", "WARN"

_IGNORED_CMD_TOOLS = frozenset({"grep", "egrep", "fgrep", "rg", "pgrep", "pkill", "tail", "less", "cat"})
_SHELL_WRAPPERS = frozenset({"sh", "bash", "zsh", "dash"})
_PREFLIGHT_RE = re.compile(r"(?:^|[\s/\\])(?:mcagent\.preflight|preflight\.py)(?:\s|$)")
_TEST_RUNNER_RE = re.compile(r"(?:^|[\s/\\])(?:unittest|pytest|py\.test)(?:\.py)?(?:\s|$)")
_SOAK_CTL_RE = re.compile(r"(?:^|[\s/\\])soak_ctl(?:\.py)?(?:\s|$)")
_MCAGENT_MOD_RE = re.compile(r"(?:^|\s)(?:-m\s+)?mcagent(?:\.[a-zA-Z0-9_]+)+(?:\s|$)")
_MCAGENT_BARE_MOD_RE = re.compile(r"(?:^|\s)-m\s+mcagent(?:\s|$)")
_MCAGENT_PATH_RE = re.compile(r"(?:^|[\s/\\])mcagent[/\\](?!\.venv\b|logs\b)[a-zA-Z0-9_./\\-]+(?:\s|$)")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    level: str = BLOCK


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _host(base: str) -> str:
    return base.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()


def _proxy_targets_port(proxy: str, port: int = 7897) -> bool:
    """判断代理配置是否指向指定端口（默认 7897），异常格式安全返回 False。"""
    raw = (proxy or "").strip()
    if not raw:
        return False
    if raw.isdigit():
        return int(raw) == port
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlsplit(candidate)
        return parsed.port == port
    except ValueError:
        return False


def _list_processes() -> list[tuple[int, str]]:
    """列出当前运行进程 (pid, cmdline)。Linux 优先读 /proc，不可用时回退 ps。"""
    if os.path.isdir("/proc"):
        try:
            entries = os.listdir("/proc")
        except OSError:
            entries = []
        if entries:
            procs: list[tuple[int, str]] = []
            for entry in entries:
                if not entry.isdigit():
                    continue
                pid = int(entry)
                try:
                    with open(f"/proc/{entry}/cmdline", "rb") as f:
                        raw = f.read()
                except OSError:
                    continue
                if not raw:
                    continue
                cmd = " ".join(
                    part.decode("utf-8", errors="replace")
                    for part in raw.split(b"\x00")
                    if part
                ).strip()
                if cmd:
                    procs.append((pid, cmd))
            return procs

    try:
        res = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    out: list[tuple[int, str]] = []
    for line in res.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            out.append((int(parts[0]), parts[1].strip()))
    return out


def _is_ignored_process_cmd(cmd: str) -> bool:
    """过滤 preflight 自身、测试运行器及查进程/看日志命令，避免误报。"""
    stripped = (cmd or "").strip()
    if not stripped:
        return True
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    if not tokens:
        return True

    exe = os.path.basename(tokens[0]).lower()
    if exe in _IGNORED_CMD_TOOLS:
        return True
    if exe in _SHELL_WRAPPERS:
        for i, tok in enumerate(tokens[1:-1], start=1):
            if tok == "-c":
                sub_tokens = tokens[i + 1].strip().split()
                if sub_tokens and os.path.basename(sub_tokens[0]).lower() in _IGNORED_CMD_TOOLS:
                    return True
                break

    if _PREFLIGHT_RE.search(stripped):
        return True
    if _TEST_RUNNER_RE.search(stripped):
        return True
    return False


def _is_residual_target_cmd(cmd: str) -> bool:
    return bool(
        _SOAK_CTL_RE.search(cmd)
        or _MCAGENT_MOD_RE.search(cmd)
        or _MCAGENT_BARE_MOD_RE.search(cmd)
        or _MCAGENT_PATH_RE.search(cmd)
    )


def _find_residual_processes(
    processes: Sequence[tuple[int, str]],
    self_pid: int | None = None,
    self_ppid: int | None = None,
) -> list[tuple[int, str]]:
    cur_pid = os.getpid() if self_pid is None else self_pid
    cur_ppid = os.getppid() if self_ppid is None else self_ppid
    residuals: list[tuple[int, str]] = []
    for pid, cmd in processes:
        if pid <= 0 or pid in (cur_pid, cur_ppid):
            continue
        if _is_ignored_process_cmd(cmd):
            continue
        if _is_residual_target_cmd(cmd):
            residuals.append((pid, cmd))
    return residuals


def _summarize_cmd(cmd: str, max_len: int = 72) -> str:
    compact_tokens: list[str] = []
    for idx, tok in enumerate(cmd.split()):
        if idx <= 2 and not tok.startswith("-") and ("/" in tok or "\\" in tok):
            base = tok.replace("\\", "/").rsplit("/", 1)[-1]
            compact_tokens.append(base or tok)
        else:
            compact_tokens.append(tok)
    compact = " ".join(compact_tokens)
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3] + "..."


def check_static(
    env: dict[str, str],
    gateway_open: bool | None = None,
    port_probe: Callable[[str, int], bool] | None = None,
    running_processes: Sequence[tuple[int, str]] | None = None,
    process_lister: Callable[[], list[tuple[int, str]]] | None = None,
) -> list[Check]:
    """静态检查。`gateway_open`、`port_probe` 与进程列表可注入，便于离线单测。"""
    out: list[Check] = []
    base = env.get("MC_API_BASE", "")
    host = _host(base) if base else ""

    out.append(Check(
        "端点不是本机", bool(base) and host not in LOCAL_HOSTS,
        f"MC_API_BASE={base or '(空)'}" + ("  ← 指向本地网关，禁止用于比赛" if host in LOCAL_HOSTS else "")))

    if host in LOCAL_HOSTS:
        out.append(Check("端点是官方入口", False, f"host={host}  ← 本地地址，已由上一条拦截", level=WARN))
    else:
        out.append(Check(
            "端点是官方入口", host in OFFICIAL_HOSTS,
            f"host={host or '(空)'}" + ("" if host in OFFICIAL_HOSTS else "  ← 不认识的域名，确认后再用"),
            level=WARN))

    mc_proxy = (env.get("MC_PROXY", "") or "").strip()
    if host not in CAMPUS_DIRECT_HOSTS:
        out.append(Check(
            "校园网直连未挂 7897 代理", True,
            f"host={host or '(空)'}（非校园网直连入口，不拦截 MC_PROXY）",
            level=BLOCK))
    elif not mc_proxy:
        out.append(Check(
            "校园网直连未挂 7897 代理", True,
            "MC_PROXY=(空)（校园网直连）",
            level=BLOCK))
    elif _proxy_targets_port(mc_proxy, 7897):
        out.append(Check(
            "校园网直连未挂 7897 代理", False,
            f"MC_PROXY={mc_proxy}  ← 校园网入口 {host} 直连实测仅 ~4ms，挂本地 7897 代理会导致不可达或高延迟，需清空 MC_PROXY",
            level=BLOCK))
    else:
        out.append(Check(
            "校园网直连未挂 7897 代理", False,
            f"MC_PROXY={mc_proxy}  ← 校园网直连建议清空 MC_PROXY，避免代理绕行",
            level=WARN))

    user = env.get("MC_USER", "")
    out.append(Check(
        "账号是正式角色名", user not in PLACEHOLDER_USERS,
        f"MC_USER={user or '(空)'}" + ("  ← 还是开发占位值" if user in PLACEHOLDER_USERS else "")))

    pwd = env.get("MC_PASS", "")
    out.append(Check(
        "密码不是 localdev", pwd not in PLACEHOLDER_PASSES,
        "MC_PASS=" + ("(空)" if not pwd else ("localdev ← 本地网关专用" if pwd in PLACEHOLDER_PASSES else "已设置"))))

    key = env.get("LLM_KEY", "")
    out.append(Check(
        "LLM key 不是公共压测 key", bool(key) and not key.startswith(TEST_KEY_PREFIX),
        ("LLM_KEY 未设置" if not key else
         ("以 sk-mRbe 开头 ← 这是群发的压测 key" if key.startswith(TEST_KEY_PREFIX) else "已设置"))))

    model = env.get("LLM_MODEL", "default")
    out.append(Check(
        "LLM_MODEL 是 default", model == "default",
        f"LLM_MODEL={model or '(空)'}" + ("" if model == "default" else "  ← 写模型名会被 403")))

    # custom 是赛前自备 key 的测试配置；比赛只许官方 AIA 代理（AGENTS §2B.3）
    profile = (env.get("LLM_PROFILE", "") or "official").strip().lower()
    out.append(Check(
        "LLM_PROFILE 是 official", profile == "official",
        f"LLM_PROFILE={env.get('LLM_PROFILE') or '(未设置=official)'}" +
        ("" if profile == "official" else "  ← 自备模型只能赛前测试，比赛必须走官方代理")))

    # 赛前在比赛服上用自备模型训练的开关（mcagent.loop --soak 才认），比赛当天必须关掉
    train_flag = (env.get("MCAGENT_TRAIN_ON_OFFICIAL", "") or "").strip()
    out.append(Check(
        "没开「比赛服训练」开关", train_flag in ("", "0"),
        f"MCAGENT_TRAIN_ON_OFFICIAL={train_flag or '(未设置)'}" +
        ("" if train_flag in ("", "0") else "  ← 只许赛前训练用，比赛必须删掉这一行")))

    conc_raw = env.get("LLM_MAX_CONCURRENCY", "")
    try:
        conc = int(conc_raw)
    except (TypeError, ValueError):
        conc = -1
    out.append(Check(
        "并发 ≤ 2（比赛期）", 0 < conc <= 2,
        f"LLM_MAX_CONCURRENCY={conc_raw or '(未设置)'}" +
        ("" if 0 < conc <= 2 else "  ← 比赛只给 2 路，没设置等于没有闸门")))

    # 网关是否已停：本地端口探测，不联网
    if gateway_open is None:
        gateway_open = _port_open("127.0.0.1", 8787, port_probe)
    out.append(Check(
        "本地联调网关已停", not gateway_open,
        "127.0.0.1:8787 " + ("仍在监听 ← 先停掉再开赛" if gateway_open else "已关闭")))

    # 残留后台进程检查：避免 soak_ctl / mcagent 占号或吃掉 2 路 LLM 并发
    if running_processes is None:
        procs = process_lister() if process_lister is not None else _list_processes()
    else:
        procs = list(running_processes)
    residuals = _find_residual_processes(procs)
    if residuals:
        pids_str = " ".join(str(pid) for pid, _ in residuals)
        preview = "; ".join(f"pid={pid} ({_summarize_cmd(cmd)})" for pid, cmd in residuals[:3])
        if len(residuals) > 3:
            preview += f" 等共 {len(residuals)} 个进程"
        out.append(Check(
            "无残留 soak / mcagent 进程", False,
            f"发现残留进程：{preview}  ← 先清理再开赛：kill {pids_str}（或 pkill -f 'soak_ctl|mcagent\\.loop'）",
            level=BLOCK))
    else:
        out.append(Check(
            "无残留 soak / mcagent 进程", True,
            "未发现残留 soak_ctl / mcagent 后台进程",
            level=BLOCK))

    return out


def _port_open(host: str, port: int, probe: Callable[[str, int], bool] | None = None) -> bool:
    if probe is not None:
        return probe(host, port)
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def check_live(env: dict[str, str], getter: Callable[[str, float], tuple[int, float]] | None = None) -> list[Check]:
    """联网检查：端点延迟 + 未授权鉴权。getter(base, timeout) -> (status, ms) 可注入。"""
    base = env.get("MC_API_BASE", "").rstrip("/")
    if not base:
        return [Check("端点可达（延迟）", False, "MC_API_BASE 为空")]
    if getter is None:
        getter = _http_status

    try:
        status, ms = getter(base + "/status", 10.0)
    except Exception as e:  # noqa: BLE001
        return [Check("端点可达（延迟）", False, f"{type(e).__name__}: {e}")]

    checks = [Check("端点可达（延迟）", True, f"{ms:.0f} ms  HTTP {status}")]
    checks.append(Check(
        "未授权返回 401", status == 401,
        f"HTTP {status}" + ("" if status == 401 else "  ← 期望 401 unauthorized")))
    checks.append(Check(
        "延迟像校内（<100ms）", ms < 100,
        f"{ms:.0f} ms" + ("" if ms < 100 else "  ← 大概率走了公网端点，预算要按公网算"),
        level=WARN))
    return checks


def _http_status(url: str, timeout: float) -> tuple[int, float]:
    """先预热一次再计时——否则 TLS 握手会被算进延迟，5ms 的校内端点会显示成 390ms。"""
    import requests

    s = requests.Session()
    s.trust_env = False  # 注册表里的死代理会污染一切
    try:
        s.get(url, timeout=timeout)  # 预热：建连 + TLS 握手，不计时
    except requests.RequestException:
        pass
    t0 = time.time()
    r = s.get(url, timeout=timeout)
    return r.status_code, (time.time() - t0) * 1000


def apply_match(src: str = DEFAULT_MATCH, dst: str = DEFAULT_ENV) -> tuple[bool, str]:
    if not os.path.exists(src):
        return False, f"找不到 {os.path.basename(src)}（先按 plan/赛前切换清单.md 准备一份）"
    with open(src, encoding="utf-8") as f:
        content = f.read()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(content)
    return True, f"已用 {os.path.basename(src)} 覆盖 {os.path.basename(dst)}"


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        mark = "✅" if c.ok else ("⛔" if c.level == BLOCK else "⚠️ ")
        lines.append(f"  {mark} {c.name:24s} {c.detail}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    env_file = DEFAULT_ENV
    live = "--live" in argv
    do_apply = "--apply" in argv
    if "--env-file" in argv:
        env_file = argv[argv.index("--env-file") + 1]

    print("=" * 72)
    print("赛前切换自检 · 目标：开发配置 → 比赛配置")
    print("=" * 72)

    if do_apply:
        ok, msg = apply_match(dst=DEFAULT_ENV)
        print(("[apply] " + msg))
        if not ok:
            return 1
        env_file = DEFAULT_ENV

    env = load_env(env_file)
    print(f"配置文件：{env_file}")
    if not env:
        print("⛔ 配置文件不存在或为空")
        return 1
    print()

    checks = check_static(env)
    if live:
        checks += check_live(env)
    print(render(checks))

    blocks = [c for c in checks if not c.ok and c.level == BLOCK]
    warns = [c for c in checks if not c.ok and c.level == WARN]
    print()
    if blocks:
        print(f"⛔ {len(blocks)} 项未过，**不能开赛**：" + "、".join(c.name for c in blocks))
        return 1
    if warns:
        print(f"⚠️  全部通过，但有 {len(warns)} 项提醒：" + "、".join(c.name for c in warns))
        return 0
    print("✅ 全部通过，可以开赛。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
