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
import socket
import sys
import time
from dataclasses import dataclass
from typing import Callable

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV = os.path.join(HERE, ".env")
DEFAULT_MATCH = os.path.join(HERE, ".env.match")

OFFICIAL_HOSTS = ("apiin.nmo.net.cn", "mc-api.windywindie.top")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0")
#: 群里发过的公共压测 key 前缀，正式 key 不应以它开头
TEST_KEY_PREFIX = "sk-mRbe"
#: 开发期常用的占位值
PLACEHOLDER_USERS = {"qyzy_dev", "dev_bot_1", "test", "Test_Agent", ""}
PLACEHOLDER_PASSES = {"localdev", ""}

BLOCK, WARN = "BLOCK", "WARN"


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


def check_static(env: dict[str, str], gateway_open: bool | None = None,
                 port_probe: Callable[[str, int], bool] | None = None) -> list[Check]:
    """静态检查。`gateway_open` 可注入，便于离线单测。"""
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
