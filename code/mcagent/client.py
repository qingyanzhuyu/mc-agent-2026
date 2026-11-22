"""Minecraft Agent API 客户端（对应规范 v1.3.1）。

设计要点（都是血的教训，改的时候别退回原样）：
1. 业务错误一律 HTTP 200，必须看 payload["ok"]，不能看状态码。
2. 本机注册表里有死代理 127.0.0.1:7897，requests 会读它 -> 必须 trust_env=False。
3. token 会过期，401 invalid_token 要能自动重登；重登会踢掉旧连接。
4. 动作是同步阻塞的（move_to 走完才返回），所以 HTTP timeout 要按动作给。
"""
from __future__ import annotations

import contextlib
import copy
import json
import math
import numbers
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter

DEFAULT_BASE = "https://mc-api.windywindie.top/api/v1"
MAX_RPS_CAP: float = 100_000.0
_FAST_PATH_MIN_RPS: float = 10_000.0
_FAST_PATH_MAX_SPACING: float = 0.0001

# §3.1 / §4.11: 姿态请求与响应使用不同的字段名。
_POSTURE_FIELDS = {"sneak": "sneaking", "shield": "blocking", "jump": "jumping"}
_POSTURE_RESET = dict.fromkeys(_POSTURE_FIELDS, False)

# ---------------------------------------------------------------- 错误码分类
RETRY = "retry"    # 等一会重试同一动作
REPLAN = "replan"  # 前置条件不成立，换计划
FATAL = "fatal"    # 别继续了

CODE_CLASS: dict[str, str] = {
    # 瞬时/会话可恢复：由客户端或执行器按协议退避恢复后重试
    "busy": RETRY,
    "timeout": RETRY,
    "window_timeout": RETRY,
    "not_loaded": RETRY,
    "rate_limited": RETRY,
    "not_ready": RETRY,
    "not_connected": RETRY,
    "wrong_server": RETRY,
    # v1.3.16 多后端代理瞬时不可用：保留原 token 重试恢复
    "backend_unavailable": RETRY,
    "routing_unavailable": RETRY,
    "death_loop_cooldown": RETRY,
    "network": RETRY,
    "bad_json": RETRY,
    # v1.3.15 请求体超过 1 MiB（HTTP 413）：原样重发仍会失败
    "body_too_large": REPLAN,
    # 目标/条件不成立：重规划（注意 bad_args 是单次参数不合法，绝不能当 FATAL 杀掉整场进程）
    "bad_args": REPLAN,
    "path_not_found": REPLAN,
    # v1.3.11 §5：寻路 8 秒没位移。原样重发 move_to 还会卡住，要换 /control 脱困，所以归重规划
    "stalled": REPLAN,
    "interrupted": REPLAN,
    "no_such_entity": REPLAN,
    "out_of_reach": REPLAN,
    "out_of_range": REPLAN,
    "missing_item": REPLAN,
    "missing_ammo": REPLAN,
    "missing_ingredients": REPLAN,
    # v1.3.11 §5：背包满。物品留在原处，腾出空位前重发只会再失败
    "inventory_full": REPLAN,
    "need_crafting_table": REPLAN,
    "need_furnace": REPLAN,
    "no_recipe": REPLAN,
    "no_support": REPLAN,
    "occupied": REPLAN,
    "place_failed": REPLAN,
    "blocked_by_self": REPLAN,
    "nothing_to_dig": REPLAN,
    "undiggable": REPLAN,
    # v1.3.11 §4.2：服务端没确认破坏，方块还在，须重新观察站位后再挖
    "dig_unconfirmed": REPLAN,
    "use_window_api": REPLAN,
    "no_window": REPLAN,
    "section_full": REPLAN,
    "no_output": REPLAN,
    "option_unavailable": REPLAN,
    "cannot_interact": REPLAN,
    "cannot_use": REPLAN,
    "need_block_target": REPLAN,
    "not_hungry": REPLAN,
    "cannot_sleep": REPLAN,
    # 客户端自产：可选接口（OPTIONAL_ROUTES）在当前部署上不存在。只停这一招，不终止整场
    "unsupported_api": REPLAN,
    "not_in_vehicle": REPLAN,
    "dismount_failed": REPLAN,
    "protected_region": REPLAN,
    "drive_timeout": REPLAN,
    "dismounted": REPLAN,
    "dead": RETRY,  # 等重生，executor 里用长退避
    # 致命：凭据或接口路径本身错误，继续重试只会触发限流封禁
    "unauthorized": FATAL,
    "invalid_token": FATAL,  # client 内部会先尝试单飞重登
    "invalid_credentials": FATAL,
    "no_profile": FATAL,
    "unknown_api": FATAL,
}

# v1.3.3 以后才有的可选接口：旧部署返回 unknown_api 时只记为不支持，不按 FATAL 终止（见 Client.call）
OPTIONAL_ROUTES: frozenset[str] = frozenset({"control", "look"})

# 每个动作的 HTTP 超时（秒）：API 是阻塞式，超时 < 动作时长会误判失败
ACTION_TIMEOUT = {
    "move_to": 180,
    "control": 10,  # 按住时长上限 3 s；只挥击时按次数另算，见 Client.control
    "attack": 90,
    "shoot": 30,
    "use_item": 90,
    "dig": 40,
    "place": 20,
    "interact": 30,
    "craft": 30,
    "window/act": 40,
    "window/view": 20,
}


class ApiError(Exception):
    def __init__(self, code: str, message: str = "", extra: Optional[dict] = None, http: Optional[int] = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.extra = extra or {}
        self.http = http

    @property
    def kind(self) -> str:
        return CODE_CLASS.get(self.code, REPLAN)

    @property
    def retryable(self) -> bool:
        return self.kind == RETRY

    def __str__(self) -> str:
        e = f"{self.code}"
        if self.message:
            e += f" ({self.message})"
        if self.extra:
            e += f" {self.extra}"
        return e


# ---------------------------------------------------------------- 配置
def _parse_finite_float(raw: Any, err_msg: str) -> float:
    if isinstance(raw, bool):
        raise ValueError(err_msg)
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(err_msg) from exc
    if not math.isfinite(val):
        raise ValueError(err_msg)
    return val


class Config:
    def __init__(
        self,
        base: str,
        username: str,
        password: str,
        proxy: Optional[str] = None,
        token_file: Optional[str] = None,
        max_rps: Optional[float] = None,
        block_cache_ttl_s: Optional[float] = None,
    ):
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.proxy = proxy
        self.token_file = token_file

        raw_rps = os.environ.get("MC_MAX_RPS", "100000") if max_rps is None else max_rps
        rps_err = "MC_MAX_RPS 必须在 (0, 100000]，100000 是客户端硬上限"
        self.max_rps = _parse_finite_float(raw_rps, rps_err)
        if not 0 < self.max_rps <= MAX_RPS_CAP:
            raise ValueError(rps_err)

        raw_ttl = os.environ.get("MC_BLOCK_CACHE_TTL_S", "0.3") if block_cache_ttl_s is None else block_cache_ttl_s
        ttl_err = "MC_BLOCK_CACHE_TTL_S 必须为有限非负数"
        self.block_cache_ttl_s = _parse_finite_float(raw_ttl, ttl_err)
        if self.block_cache_ttl_s < 0:
            raise ValueError(ttl_err)

    @classmethod
    def load(cls, env_file: str | None = None) -> "Config":
        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        if env_file is None:
            env_file = os.path.join(pkg_dir, ".env")
        if os.path.exists(env_file):
            with open(env_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        raw_tf = os.environ.get("MC_TOKEN_FILE")
        if raw_tf is None:
            token_file: Optional[str] = os.path.join(pkg_dir, ".token.json")
        else:
            token_file = raw_tf.strip() or None
        return cls(
            base=os.environ.get("MC_API_BASE", DEFAULT_BASE),
            username=os.environ.get("MC_USER", ""),
            password=os.environ.get("MC_PASS", ""),
            proxy=os.environ.get("MC_PROXY") or None,
            token_file=token_file,
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)


def _parse_iso(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------- 客户端
class RequestLimiter:
    """所有请求共用的限速器；高频(>=10000 RPS)走零睡眠快路径，低频保留 /stop > 普通 > 后台观察三级排队。"""

    def __init__(self, rate: float):
        self.rate = float(rate)
        self.spacing = 1.0 / self.rate
        self._fast_path = self.rate >= _FAST_PATH_MIN_RPS or self.spacing <= _FAST_PATH_MAX_SPACING
        self._next = 0.0
        self._cond = threading.Condition()
        self._urgent = 0
        self._normal = 0

    def acquire(self, urgent: bool = False, *, background: bool = False) -> None:
        is_urgent = bool(urgent)
        if self._fast_path:
            if is_urgent:
                return
            if self._urgent == 0:
                return
            with self._cond:
                while self._urgent > 0:
                    self._cond.wait(0.001)
            return
        is_normal = not is_urgent and not bool(background)
        with self._cond:
            if is_urgent:
                self._urgent += 1
            elif is_normal:
                self._normal += 1
            try:
                while True:
                    delay = self._next - time.monotonic()
                    if delay <= 0 and (
                        is_urgent
                        or (is_normal and self._urgent == 0)
                        or (not is_urgent and not is_normal and self._urgent == 0 and self._normal == 0)
                    ):
                        self._next = time.monotonic() + self.spacing
                        return
                    self._cond.wait(max(0.001, delay) if delay > 0 else self.spacing)
            finally:
                if is_urgent:
                    self._urgent -= 1
                elif is_normal:
                    self._normal -= 1
                self._cond.notify_all()


class Client:
    def __init__(self, cfg: Config, log=print):
        self.cfg = cfg
        self._limiter = RequestLimiter(cfg.max_rps)
        self.log = log
        self.session = requests.Session()
        # 关键：不读环境/注册表里的代理
        self.session.trust_env = False
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        if cfg.proxy:
            self.session.proxies = {"http": cfg.proxy, "https": cfg.proxy}
        self.token: Optional[str] = None
        self.expires_at: Optional[float] = None
        self.bot_username: Optional[str] = None
        self._auth_lock = threading.RLock()
        self._posture = _POSTURE_RESET.copy()
        self._route_support: dict[str, bool] = {}
        self._last_disconnect_mono: Optional[float] = None
        self.auto_wait_ready_on_relogin: bool = True
        self._bg_tls = threading.local()
        self._block_cache_lock = threading.Lock()
        self._block_cache: dict[tuple[int, int, int], tuple[float, dict]] = {}
        self._block_cache_gen: int = 0
        self._rtt_lock = threading.Lock()
        self._smoothed_rtt_s: float = 0.0
        self._load_cached_token()

    @property
    def smoothed_rtt_s(self) -> float:
        """最近只读观测请求的指数滑动平均 RTT（秒），供批量并发与缓存 TTL 自适应。"""
        with self._rtt_lock:
            return self._smoothed_rtt_s

    def _record_rtt(self, route: str, elapsed_s: float) -> None:
        if route not in ("status", "get_block", "events", "inventory", "find_blocks", "get_entities", "advancements"):
            return
        if not math.isfinite(elapsed_s) or elapsed_s < 0:
            return
        clamped = min(10.0, float(elapsed_s))
        with self._rtt_lock:
            if self._smoothed_rtt_s <= 0.0:
                self._smoothed_rtt_s = clamped
            else:
                self._smoothed_rtt_s = 0.75 * self._smoothed_rtt_s + 0.25 * clamped

    def _effective_block_cache_ttl(self) -> float:
        ttl = getattr(self.cfg, "block_cache_ttl_s", 0.0)
        if ttl <= 0:
            return 0.0
        rtt = self.smoothed_rtt_s
        if rtt > 0.20:
            return max(float(ttl), min(10.0, rtt * 6.0))
        return float(ttl)

    def _is_background_thread(self) -> bool:
        return getattr(self._bg_tls, "depth", 0) > 0

    @contextlib.contextmanager
    def background_observation(self):
        depth = getattr(self._bg_tls, "depth", 0)
        self._bg_tls.depth = depth + 1
        try:
            yield
        finally:
            self._bg_tls.depth = max(0, getattr(self._bg_tls, "depth", 1) - 1)

    # ---- Token 缓存落盘 ------------------------------------------------
    def _load_cached_token(self) -> None:
        path = self.cfg.token_file
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        if self.cfg.username and data.get("username") != self.cfg.username:
            return
        if data.get("base") and data.get("base") != self.cfg.base:
            return
        tok = data.get("token")
        exp = data.get("expires_at_ts")
        if not isinstance(tok, str) or not tok:
            return
        if isinstance(exp, (int, float)) and time.time() > float(exp) - 120:
            return
        self.token = tok
        self.expires_at = float(exp) if isinstance(exp, (int, float)) else None
        self.bot_username = data.get("bot_username")

    def _save_cached_token(self, raw_expires: Optional[str] = None) -> None:
        path = self.cfg.token_file
        if not path or not self.token:
            return
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = f"{path}.tmp.{os.getpid()}"
            payload = {
                "base": self.cfg.base,
                "username": self.cfg.username,
                "token": self.token,
                "expires_at": raw_expires,
                "expires_at_ts": self.expires_at,
                "bot_username": self.bot_username,
                "saved_at": time.time(),
            }
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            self.log(f"[auth] token 落盘失败（不影响运行）: {e}")

    # ---- 底层 ----------------------------------------------------------
    def _send(
        self,
        method: str,
        path: str,
        body: Any = None,
        timeout: float = 30,
        auth: bool = True,
        token: Optional[str] = None,
    ) -> dict:
        route = path.lstrip("/")
        url = f"{self.cfg.base}/{route}"
        headers = {"Content-Type": "application/json"}
        use_token = self.token if token is None else token
        if auth and use_token:
            headers["Authorization"] = f"Bearer {use_token}"
        is_urgent = route == "stop"
        self._limiter.acquire(
            urgent=is_urgent,
            background=self._is_background_thread() and not is_urgent,
        )
        t_req = time.monotonic()
        try:
            if method == "GET":
                r = self.session.get(url, headers=headers, params=body, timeout=timeout)
            else:
                r = self.session.post(url, headers=headers, data=json.dumps(body or {}), timeout=timeout)
        except requests.RequestException as e:
            raise ApiError("network", f"{type(e).__name__}: {e}") from e
        if r.status_code < 500:
            self._record_rtt(route, time.monotonic() - t_req)

        if r.status_code == 401:
            code = "invalid_token"
            msg = "token 无效或过期"
            extra: dict = {}
            try:
                err = r.json().get("error") or {}
                code = err.get("code", code)
                msg = err.get("message", msg)
                extra = {k: v for k, v in err.items() if k not in ("code", "message")}
            except ValueError:
                pass
            raise ApiError(code, msg, extra, http=401)
        if r.status_code == 429:
            msg = "请求过于频繁"
            extra = {}
            try:
                err = r.json().get("error") or {}
                msg = err.get("message", msg)
                extra = {k: v for k, v in err.items() if k not in ("code", "message")}
            except ValueError:
                pass
            hdr_retry = r.headers.get("Retry-After")
            if "retry_after_s" not in extra and hdr_retry:
                try:
                    extra["retry_after_s"] = float(hdr_retry)
                except ValueError:
                    pass
            raise ApiError("rate_limited", msg, extra, http=429)
        if r.status_code == 404:
            raise ApiError("unknown_api", f"路径不存在: {path}", http=404)
        if r.status_code == 400:
            raise ApiError("bad_json", "请求体不是合法 JSON", http=400)
        if r.status_code >= 500:
            raise ApiError("network", f"HTTP {r.status_code}: {r.text[:160]}", http=r.status_code)
        try:
            payload = r.json()
        except ValueError:
            raise ApiError("bad_json", r.text[:200], http=r.status_code)
        return payload

    def _is_game_action_path(self, path: str) -> bool:
        p = path.lstrip("/")
        return p in ACTION_TIMEOUT or p in ("equip", "dismount", "drop", "chat", "posture", "look")

    def supports(self, route: str) -> Optional[bool]:
        """可选接口的能力探测结果：True 已调通，False 当前部署没有该路径，None 还没试过。"""
        return self._route_support.get(route.lstrip("/"))

    def call(self, path: str, body: Any = None, method: str = "POST", timeout: float = 30) -> dict:
        """发一次调用，返回 result；失败抛 ApiError。401 / invalid_token 会单飞自动重登一次。

        可选接口（OPTIONAL_ROUTES）边用边探测：第一次返回 unknown_api 就记为不支持，
        改抛 unsupported_api（REPLAN）。之后不再发请求，直接本地抛同一个码。
        其它路径的 unknown_api 仍是 FATAL，说明客户端把必有接口写错了。
        """
        route = path.lstrip("/")
        if route not in OPTIONAL_ROUTES:
            return self._call(path, body, method, timeout)
        if self._route_support.get(route) is False:
            raise ApiError("unsupported_api", f"当前部署不支持 /{route}（此前已返回 unknown_api）", {"route": route})
        try:
            result = self._call(path, body, method, timeout)
        except ApiError as e:
            if e.code != "unknown_api":
                if e.kind == REPLAN:
                    self._route_support.setdefault(route, True)  # 业务错误（如 bad_args）说明路径存在；断网不算
                raise
            self._route_support[route] = False
            self.log(f"[capability] /{route} 返回 unknown_api，本局按不支持处理")
            raise ApiError("unsupported_api", f"当前部署不支持 /{route}", {"route": route}, http=e.http) from e
        self._route_support[route] = True
        return result

    def _call(self, path: str, body: Any = None, method: str = "POST", timeout: float = 30) -> dict:
        used_token = self._ensure_token()
        try:
            payload = self._send(method, path, body, timeout, token=used_token)
        except ApiError as e:
            if e.http == 401 or e.code in ("invalid_token", "unauthorized"):
                relogged = self._refresh_token(failed_token=used_token, reason=e.code)
                if relogged and self.auto_wait_ready_on_relogin and self._is_game_action_path(path):
                    self.wait_ready(timeout=60)
                payload = self._send(method, path, body, timeout, token=self.token)
            else:
                raise

        if payload.get("ok") is False:
            err = payload.get("error") or {}
            code = err.get("code", "unknown")
            if code in ("invalid_token", "unauthorized"):
                relogged = self._refresh_token(failed_token=used_token, reason=code)
                if relogged and self.auto_wait_ready_on_relogin and self._is_game_action_path(path):
                    self.wait_ready(timeout=60)
                payload = self._send(method, path, body, timeout, token=self.token)
                if payload.get("ok") is False:
                    err2 = payload.get("error") or {}
                    raise ApiError(
                        err2.get("code", "unknown"),
                        err2.get("message", ""),
                        {k: v for k, v in err2.items() if k not in ("code", "message")},
                    )
                return payload.get("result") or {}
            if code == "not_connected" and self._last_disconnect_mono is None:
                self._last_disconnect_mono = time.monotonic()
            raise ApiError(code, err.get("message", ""), {k: v for k, v in err.items() if k not in ("code", "message")})
        return payload.get("result") or {}

    # ---- 无凭据探测 ----------------------------------------------------
    def probe(self) -> str:
        """不带 token 打一次 /status，用来确认端点/网络可用。返回错误码。"""
        try:
            self._send("GET", "status", timeout=15, auth=False)
            return "ok"
        except ApiError as e:
            return e.code

    # ---- 认证 ----------------------------------------------------------
    def _ensure_token(self) -> str:
        with self._auth_lock:
            if not self.token:
                self._login_unlocked()
            elif self.expires_at and time.time() > self.expires_at - 120:
                self.log("[auth] token 快过期，提前重登")
                self._login_unlocked()
            return self.token or ""

    def _refresh_token(self, failed_token: Optional[str], reason: str = "invalid_token") -> bool:
        """并发安全单点刷新：若其他线程已刷新出新 token，直接复用不重复登录互踢。"""
        with self._auth_lock:
            now = time.time()
            if (
                self.token
                and self.token != failed_token
                and (not self.expires_at or now <= self.expires_at - 120)
            ):
                return False
            self.log(f"[auth] {reason}，尝试重登")
            self._login_unlocked()
            return True

    def login(self) -> None:
        with self._auth_lock:
            self._login_unlocked()

    def _login_unlocked(self) -> None:
        if not self.cfg.has_credentials:
            raise ApiError("invalid_credentials", "没配 MC_USER / MC_PASS（见 mcagent/.env.example）")
        payload = self._send(
            "POST", "auth/login",
            {"username": self.cfg.username, "password": self.cfg.password},
            timeout=20, auth=False,
        )
        if payload.get("ok") is False:
            err = payload.get("error") or {}
            raise ApiError(
                err.get("code", "invalid_credentials"),
                err.get("message", ""),
                {k: v for k, v in err.items() if k not in ("code", "message")},
            )
        self.token = payload["token"]
        self.expires_at = _parse_iso(payload.get("expires_at"))
        self.bot_username = payload.get("bot_username")
        self._posture = _POSTURE_RESET.copy()
        self.invalidate_block_cache()
        self._save_cached_token(payload.get("expires_at"))
        self.log(f"[auth] 登录成功 bot={self.bot_username} token 到期={payload.get('expires_at')}")

    # ---- 连接 ----------------------------------------------------------
    def ensure_connected(self, min_interval_s: float = 31.0, timeout: float = 20) -> dict:
        """遵守规范 §2.1 断线重连间隔 ≥30s 的硬限制后发起 /connect。"""
        if self._last_disconnect_mono is not None and min_interval_s > 0:
            elapsed = time.monotonic() - self._last_disconnect_mono
            if elapsed < min_interval_s:
                wait_s = min_interval_s - elapsed
                self.log(f"[connect] 距断线 {elapsed:.1f}s，等待 {wait_s:.1f}s 满足 ≥30s 重连间隔")
                time.sleep(wait_s)
        res = self.connect(timeout=timeout)
        self._last_disconnect_mono = None
        return res

    def wait_ready(
        self,
        timeout: float = 120,
        interval: float = 1.0,
        min_reconnect_s: float = 31.0,
    ) -> dict:
        """登录后要经过代理大厅，5-15s 才真正进比赛服。ready=true 前发动作会 wrong_server。"""
        t0 = time.monotonic()
        deadline = t0 + timeout
        last_connect_try = 0.0
        last: dict = {}
        while time.monotonic() < deadline:
            try:
                last = self.status()
            except ApiError as e:
                if e.code == "not_connected":
                    if self._last_disconnect_mono is None:
                        self._last_disconnect_mono = time.monotonic()
                    try:
                        self.ensure_connected(min_interval_s=min_reconnect_s)
                        last_connect_try = time.monotonic()
                    except ApiError as e2:
                        if e2.code == "death_loop_cooldown":
                            rem = float(e2.extra.get("cooldown_remaining_seconds", 30) or 30)
                            self.log(f"[ready] 死亡循环冷却中，剩余 {rem:.0f}s")
                            deadline = max(deadline, time.monotonic() + rem + 30.0)
                            time.sleep(min(max(interval, 1.0), rem))
                            continue
                elif e.code == "not_ready" and (time.monotonic() - last_connect_try >= 15.0):
                    try:
                        self.connect(timeout=20)
                        last_connect_try = time.monotonic()
                    except ApiError:
                        pass
                self.log(f"[ready] 等待中: {e}")
                time.sleep(interval)
                continue

            if last.get("ready"):
                self._last_disconnect_mono = None
                self.log(f"[ready] 已进比赛服 ({time.monotonic() - t0:.1f}s) pos={last.get('pos')}")
                return last

            # 规范 §2.1：已在线但 ready=false（卡在代理大厅），调一次 /connect 立刻重发 /server 切服
            now_m = time.monotonic()
            if (now_m - t0 >= min(5.0, interval * 3)) and (now_m - last_connect_try >= 15.0):
                try:
                    self.connect(timeout=20)
                    last_connect_try = time.monotonic()
                except ApiError as e2:
                    if e2.code == "death_loop_cooldown":
                        rem = float(e2.extra.get("cooldown_remaining_seconds", 30) or 30)
                        deadline = max(deadline, time.monotonic() + rem + 30.0)
            time.sleep(interval)
        raise ApiError("not_ready", f"{timeout}s 内没 ready，最后状态: {json.dumps(last, ensure_ascii=False)[:300]}")

    # ---- 观察 ----------------------------------------------------------
    def status(self, timeout: float = 10) -> dict:
        previous = self._posture
        res = self.call("status", method="GET", timeout=timeout)
        if isinstance(res, dict):
            if not res.get("alive", True):
                self._posture = _POSTURE_RESET.copy()
            elif self._posture is previous:
                # 并发轮询的旧响应不能覆盖刚成功设置的姿态。
                self._posture = self._posture_from(res, previous)
        return res

    def inventory(self, timeout: float = 10) -> dict:
        return self.call("inventory", method="GET", timeout=timeout)

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None) -> dict:
        body: dict = {"radius": radius, "limit": limit}
        if types:
            body["types"] = types
        if properties:
            body["properties"] = properties
        if mature is not None:
            body["mature"] = mature
        return self.call("find_blocks", body, timeout=20)

    def invalidate_block_cache(self) -> None:
        with self._block_cache_lock:
            self._block_cache.clear()
            self._block_cache_gen += 1

    @staticmethod
    def _block_cache_key(pos: Any) -> Optional[tuple[int, int, int]]:
        try:
            coords = list(pos)
        except TypeError:
            return None
        if len(coords) != 3:
            return None
        floored: list[int] = []
        for v in coords:
            if isinstance(v, bool) or not isinstance(v, numbers.Real):
                return None
            if not math.isfinite(float(v)):
                return None
            floored.append(math.floor(v))
        return (floored[0], floored[1], floored[2])

    def get_block(self, pos, *, use_cache: bool = True) -> dict:
        pos_list = list(pos)
        ttl = self._effective_block_cache_ttl()
        key = self._block_cache_key(pos_list) if (use_cache and ttl > 0) else None
        with self._block_cache_lock:
            gen_before = self._block_cache_gen
            if key is not None:
                entry = self._block_cache.get(key)
                if entry is not None:
                    expires_at, cached = entry
                    if time.monotonic() < expires_at:
                        return copy.deepcopy(cached)
                    self._block_cache.pop(key, None)
        res = self.call("get_block", {"pos": pos_list}, timeout=10)
        if key is not None and isinstance(res, dict) and res.get("name"):
            now = time.monotonic()
            with self._block_cache_lock:
                if self._block_cache_gen == gen_before:
                    if len(self._block_cache) > 256:
                        expired = [k for k, (exp, _) in self._block_cache.items() if now >= exp]
                        for k in expired:
                            self._block_cache.pop(k, None)
                    self._block_cache[key] = (now + ttl, copy.deepcopy(res))
        return copy.deepcopy(res) if isinstance(res, dict) else res

    def get_blocks_batch(
        self,
        positions,
        *,
        max_workers: int = 8,
        use_cache: bool = True,
    ) -> dict[tuple[int, int, int], dict]:
        """并发只读批量查块（有界线程池 + 短时缓存命中 + 单格异常隔离，不触碰 Executor 动作锁）。"""
        keys: list[tuple[int, int, int]] = []
        seen: set[tuple[int, int, int]] = set()
        for pos in positions:
            key = self._block_cache_key(pos)
            if key is not None and key not in seen:
                seen.add(key)
                keys.append(key)
        if not keys:
            return {}

        out: dict[tuple[int, int, int], dict] = {}
        missing: list[tuple[int, int, int]] = []
        ttl = self._effective_block_cache_ttl()
        if use_cache and ttl > 0:
            now = time.monotonic()
            with self._block_cache_lock:
                for key in keys:
                    entry = self._block_cache.get(key)
                    if entry is not None:
                        expires_at, cached = entry
                        if now < expires_at:
                            out[key] = copy.deepcopy(cached)
                            continue
                        self._block_cache.pop(key, None)
                    missing.append(key)
        else:
            missing = keys

        if not missing:
            return out

        is_bg = self._is_background_thread()

        def _fetch_one(cell: tuple[int, int, int]) -> tuple[tuple[int, int, int], dict]:
            try:
                if is_bg:
                    with self.background_observation():
                        res = self.get_block(list(cell), use_cache=use_cache)
                else:
                    res = self.get_block(list(cell), use_cache=use_cache)
                return cell, (res if isinstance(res, dict) else {})
            except Exception:
                return cell, {}

        req_workers = int(max_workers or 1)
        if self.smoothed_rtt_s > 0.25:
            req_workers = min(req_workers, 3)
        workers = max(1, min(req_workers, len(missing), 16))
        if workers <= 1 or len(missing) == 1:
            for cell in missing:
                k, val = _fetch_one(cell)
                out[k] = val
            return out

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for k, val in pool.map(_fetch_one, missing):
                out[k] = val
        return out

    def get_entities(self, radius=32, types=None) -> dict:
        body: dict = {"radius": radius}
        if types:
            body["types"] = types
        return self.call("get_entities", body, timeout=15)

    def events(self, since: int = 0, limit: int = 100) -> dict:
        return self.call("events", {"since": since, "limit": limit}, method="GET", timeout=15)

    def advancements(self, status="completed", include_recipes=False) -> dict:
        return self.call("advancements", {"status": status, "include_recipes": include_recipes}, timeout=20)

    # ---- 动作 ----------------------------------------------------------
    def action(self, name: str, body: Any = None, timeout: Optional[float] = None,
               *, stop_on=None) -> dict:
        # 规范 §4：显式声明服务端反射，不依赖轮询发现 hurt/death。
        body = dict(body or {})
        conditions = stop_on if stop_on is not None else body.get("stop_on")
        if conditions is None:
            conditions = ["death"] if name in ("attack", "shoot") else ["hurt", "death"]
        if not isinstance(conditions, (list, tuple)) or any(v not in ("hurt", "death") for v in conditions):
            raise ValueError("stop_on 必须是 hurt/death 数组")
        body["stop_on"] = list(conditions)
        return self.call(name, body, timeout=timeout or ACTION_TIMEOUT.get(name, 30))

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        body: dict = {"range": rng}
        if entity_id is not None:
            body["entity_id"] = entity_id
        elif pos is not None:
            p = list(pos)
            body["x"], body["y"], body["z"] = p[0], p[1], p[2]
        else:
            body["x"], body["z"] = x, z
            if y is not None:
                body["y"] = y
        return self.action("move_to", body, timeout, stop_on=stop_on)

    def dig(self, pos, collect=True, *, stop_on=None) -> dict:
        try:
            return self.action("dig", {"pos": list(pos), "collect": collect}, stop_on=stop_on)
        finally:
            self.invalidate_block_cache()

    def place(self, item, pos=None, *, stop_on=None) -> dict:
        body: dict = {"item": item}
        if pos is not None:
            body["pos"] = list(pos)
        try:
            return self.action("place", body, stop_on=stop_on)
        finally:
            self.invalidate_block_cache()

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None) -> dict:
        body: dict = {}
        if pos is not None:
            body["pos"] = list(pos)
        if entity_id is not None:
            body["entity_id"] = entity_id
        if item:
            body["item"] = item
        try:
            return self.action("interact", body, stop_on=stop_on)
        finally:
            self.invalidate_block_cache()

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None) -> dict:
        body: dict = {"item": item}
        if target is not None:
            body["target"] = target
        if ms is not None:
            body["ms"] = ms
        if timeout_ms is not None:
            body["timeout"] = timeout_ms
        if source:
            body["source"] = source
        try:
            return self.action("use_item", body, stop_on=stop_on)
        finally:
            self.invalidate_block_cache()

    def attack(self, entity_id, timeout_ms=20000, *, max_hits=None, interval_ms=None,
               chase=None, feedback=None, stop_on=None) -> dict:
        # v1.3.11 §4.6 + PR #4 (937045f)：max_hits / interval_ms / chase=false 原地挥刀 / feedback=true 回执
        body: dict = {"entity_id": entity_id}
        if timeout_ms is not None:
            body["timeout"] = timeout_ms
        if max_hits is not None:
            body["max_hits"] = max_hits
        if interval_ms is not None:
            body["interval_ms"] = interval_ms
        if chase is not None:
            body["chase"] = chase
        if feedback is not None:
            body["feedback"] = feedback
        return self.action("attack", body, stop_on=stop_on)

    def control(self, *, forward=None, back=None, left=None, right=None, jump=None, sneak=None,
                sprint=None, yaw=None, pitch=None, hit=None, hit_interval_ms=None, ms=None,
                wait=None, stop_on=None) -> dict:
        """v1.3.14 §4.16 底层按键。wait=False 时按下即返并在后台按住 ms；可选接口旧部署抛 unsupported_api。"""
        body = {k: v for k, v in {
            "forward": forward, "back": back, "left": left, "right": right,
            "jump": jump, "sneak": sneak, "sprint": sprint,
            "yaw": yaw, "pitch": pitch, "hit": hit, "hit_interval_ms": hit_interval_ms, "ms": ms,
            "wait": wait,
        }.items() if v is not None}
        timeout = ACTION_TIMEOUT["control"]
        if wait is not False and ms is None and hit is not None and hit is not True:
            # 只给 hit 时服务端按 (hit-1)×间隔 算时长，间隔缺省最长 1667 ms（重锤）
            timeout += (int(hit) - 1) * (hit_interval_ms or 1667) / 1000
        return self.action("control", body, timeout, stop_on=stop_on)

    def look(self, yaw=None, pitch=None, *, pos=None, entity_id=None) -> dict:
        """v1.3.11 §4.17 转向，不占动作锁。给 yaw/pitch（弧度）、pos 或 entity_id 之一。"""
        body: dict = {}
        if entity_id is not None:
            body["entity_id"] = entity_id
        elif pos is not None:
            body["x"], body["y"], body["z"] = list(pos)[:3]
        else:
            body = {k: v for k, v in {"yaw": yaw, "pitch": pitch}.items() if v is not None}
        if not body:
            raise ValueError("look 需要 yaw/pitch、pos 或 entity_id")
        return self.call("look", body, timeout=10)

    def shoot(self, entity_id=None, pos=None, weapon="bow", charge_ms=1100, *, stop_on=None) -> dict:
        body: dict = {"weapon": weapon, "charge_ms": charge_ms}
        if entity_id is not None:
            body["entity_id"] = entity_id
        if pos is not None:
            body["pos"] = list(pos)
        return self.action("shoot", body, stop_on=stop_on)

    def craft(self, item, count=1, *, stop_on=None) -> dict:
        return self.action("craft", {"item": item, "count": count}, stop_on=stop_on)

    def window_view(self, target, *, stop_on=None) -> dict:
        return self.action("window/view", {"target": target}, stop_on=stop_on)

    def window_act(self, target, ops, *, stop_on=None) -> dict:
        return self.action("window/act", {"target": target, "ops": ops}, stop_on=stop_on)

    def equip(self, item, slot="hand", *, stop_on=None) -> dict:
        slot_norm = {"chest": "torso", "offhand": "off-hand"}.get(slot, slot)
        return self.action("equip", {"item": item, "slot": slot_norm}, stop_on=stop_on)

    @staticmethod
    def _posture_from(response: Any, fallback: dict[str, bool]) -> dict[str, bool]:
        if not isinstance(response, dict):
            return fallback
        return {
            key: bool(response.get(field, fallback[key]))
            for key, field in _POSTURE_FIELDS.items()
        }

    def posture(self, sneak=None, shield=None, *, jump=None) -> dict:
        # §4.11：服务端全量覆盖。无参发 {} 复位，部分修改合并缓存中的姿态。
        updates = {"sneak": sneak, "shield": shield, "jump": jump}
        specified = {key: bool(value) for key, value in updates.items() if value is not None}
        target = _POSTURE_RESET.copy()
        body: dict = {}
        if specified:
            target = {**self._posture, **specified}
            body = target.copy()
            # 未使用 jump 时延续旧版网关的请求结构；服务端缺省即 false。
            if jump is None and not target["jump"]:
                del body["jump"]
        res = self.call("posture", body, timeout=10)
        self._posture = self._posture_from(res, target)
        return res

    def dismount(self, *, stop_on=None) -> dict:
        return self.action("dismount", stop_on=stop_on)

    def drop(self, item, count=1, *, stop_on=None) -> dict:
        return self.action("drop", {"item": item, "count": count}, stop_on=stop_on)

    def chat(self, message, *, stop_on=None) -> dict:
        # 规范 §4.14：禁止以 / 开头（含前置空格），否则触发 bad_args
        msg = str(message).lstrip()
        if msg.startswith("/"):
            msg = "／" + msg.lstrip("/")
        return self.action("chat", {"message": msg[:100]}, stop_on=stop_on)

    def stop(self) -> dict:
        return self.call("stop", {}, timeout=15)

    def connect(self, timeout=20) -> dict:
        try:
            return self.call("connect", {}, timeout=timeout)
        finally:
            self.invalidate_block_cache()

    def disconnect(self) -> dict:
        try:
            res = self.call("disconnect", {}, timeout=20)
            self._posture = _POSTURE_RESET.copy()
            self._last_disconnect_mono = time.monotonic()
            return res
        finally:
            self.invalidate_block_cache()


# ---------------------------------------------------------------- 自检
def _selftest() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = Config.load()
    log = print
    log(f"base = {cfg.base}")
    log(f"凭据 = {'已配置' if cfg.has_credentials else '未配置'}")
    log(f"代理 = {cfg.proxy or '直连'}")
    c = Client(cfg, log=log)
    t0 = time.time()
    code = c.probe()
    dt = (time.time() - t0) * 1000
    if code in ("unauthorized", "invalid_token"):
        log(f"探测结果 = {code} ({dt:.0f}ms)  <- 端点活着、TLS 正常、代理已绕开")
        return 0
    log(f"探测结果 = {code} ({dt:.0f}ms)  <- 不正常，看 .env 里的 MC_API_BASE / MC_PROXY")
    return 1


if __name__ == "__main__":
    sys.exit(_selftest())
