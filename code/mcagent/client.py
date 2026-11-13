"""Minecraft Agent API 客户端（对应规范 v1.3.1）。

设计要点（都是血的教训，改的时候别退回原样）：
1. 业务错误一律 HTTP 200，必须看 payload["ok"]，不能看状态码。
2. 本机注册表里有死代理 127.0.0.1:7897，requests 会读它 -> 必须 trust_env=False。
3. token 会过期，401 invalid_token 要能自动重登；重登会踢掉旧连接。
4. 动作是同步阻塞的（move_to 走完才返回），所以 HTTP timeout 要按动作给。
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

DEFAULT_BASE = "https://mc-api.windywindie.top/api/v1"

# ---------------------------------------------------------------- 错误码分类
RETRY = "retry"    # 等一会重试同一动作
REPLAN = "replan"  # 前置条件不成立，换计划
FATAL = "fatal"    # 别继续了

CODE_CLASS: dict[str, str] = {
    # 瞬时/会话可恢复：由客户端或执行器按协议退避恢复后重试
    "busy": RETRY,
    "timeout": RETRY,
    "not_loaded": RETRY,
    "rate_limited": RETRY,
    "not_ready": RETRY,
    "not_connected": RETRY,
    "wrong_server": RETRY,
    "death_loop_cooldown": RETRY,
    "network": RETRY,
    "bad_json": RETRY,
    # 目标/条件不成立：重规划（注意 bad_args 是单次参数不合法，绝不能当 FATAL 杀掉整场进程）
    "bad_args": REPLAN,
    "path_not_found": REPLAN,
    "interrupted": REPLAN,
    "no_such_entity": REPLAN,
    "out_of_reach": REPLAN,
    "out_of_range": REPLAN,
    "missing_item": REPLAN,
    "missing_ammo": REPLAN,
    "missing_ingredients": REPLAN,
    "need_crafting_table": REPLAN,
    "need_furnace": REPLAN,
    "no_recipe": REPLAN,
    "no_support": REPLAN,
    "occupied": REPLAN,
    "place_failed": REPLAN,
    "blocked_by_self": REPLAN,
    "nothing_to_dig": REPLAN,
    "undiggable": REPLAN,
    "use_window_api": REPLAN,
    "cannot_interact": REPLAN,
    "cannot_use": REPLAN,
    "need_block_target": REPLAN,
    "not_hungry": REPLAN,
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

# 每个动作的 HTTP 超时（秒）：API 是阻塞式，超时 < 动作时长会误判失败
ACTION_TIMEOUT = {
    "move_to": 180,
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
class Config:
    def __init__(
        self,
        base: str,
        username: str,
        password: str,
        proxy: Optional[str] = None,
        token_file: Optional[str] = None,
        max_rps: Optional[float] = None,
    ):
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.proxy = proxy
        self.token_file = token_file
        self.max_rps = float(os.environ.get("MC_MAX_RPS", "4")) if max_rps is None else max_rps
        if not math.isfinite(self.max_rps) or not 0 < self.max_rps <= 4:
            raise ValueError("MC_MAX_RPS 必须在 (0, 4]，4 是客户端硬上限")

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
    """所有请求共用的无突发限速；等待者不持锁睡眠，/stop 优先取下一槽。"""

    def __init__(self, rate: float):
        self.spacing = 1.0 / rate
        self._next = 0.0
        self._cond = threading.Condition()
        self._urgent = 0

    def acquire(self, urgent: bool = False) -> None:
        with self._cond:
            self._urgent += int(urgent)
            try:
                while True:
                    delay = self._next - time.monotonic()
                    if delay <= 0 and (urgent or not self._urgent):
                        self._next = time.monotonic() + self.spacing
                        return
                    self._cond.wait(max(0.001, delay) if delay > 0 else self.spacing)
            finally:
                self._urgent -= int(urgent)
                self._cond.notify_all()


class Client:
    def __init__(self, cfg: Config, log=print):
        self.cfg = cfg
        self._limiter = RequestLimiter(cfg.max_rps)
        self.log = log
        self.session = requests.Session()
        # 关键：不读环境/注册表里的代理
        self.session.trust_env = False
        if cfg.proxy:
            self.session.proxies = {"http": cfg.proxy, "https": cfg.proxy}
        self.token: Optional[str] = None
        self.expires_at: Optional[float] = None
        self.bot_username: Optional[str] = None
        self._auth_lock = threading.RLock()
        self._posture: dict[str, bool] = {"sneak": False, "shield": False}
        self._last_disconnect_mono: Optional[float] = None
        self.auto_wait_ready_on_relogin: bool = True
        self._load_cached_token()

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
        url = f"{self.cfg.base}/{path.lstrip('/')}"
        headers = {"Content-Type": "application/json"}
        use_token = self.token if token is None else token
        if auth and use_token:
            headers["Authorization"] = f"Bearer {use_token}"
        self._limiter.acquire(urgent=path.lstrip("/") == "stop")
        try:
            if method == "GET":
                r = self.session.get(url, headers=headers, params=body, timeout=timeout)
            else:
                r = self.session.post(url, headers=headers, data=json.dumps(body or {}), timeout=timeout)
        except requests.RequestException as e:
            raise ApiError("network", f"{type(e).__name__}: {e}") from e

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
        return p in ACTION_TIMEOUT or p in ("equip", "dismount", "drop", "chat", "posture")

    def call(self, path: str, body: Any = None, method: str = "POST", timeout: float = 30) -> dict:
        """发一次调用，返回 result；失败抛 ApiError。401 / invalid_token 会单飞自动重登一次。"""
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
        self._posture = {"sneak": False, "shield": False}
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
        res = self.call("status", method="GET", timeout=timeout)
        if isinstance(res, dict):
            if not res.get("alive", True):
                self._posture = {"sneak": False, "shield": False}
            else:
                if "sneaking" in res:
                    self._posture["sneak"] = bool(res.get("sneaking"))
                if "blocking" in res:
                    self._posture["shield"] = bool(res.get("blocking"))
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

    def get_block(self, pos) -> dict:
        return self.call("get_block", {"pos": list(pos)}, timeout=10)

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
        return self.action("dig", {"pos": list(pos), "collect": collect}, stop_on=stop_on)

    def place(self, item, pos=None, *, stop_on=None) -> dict:
        body: dict = {"item": item}
        if pos is not None:
            body["pos"] = list(pos)
        return self.action("place", body, stop_on=stop_on)

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None) -> dict:
        body: dict = {}
        if pos is not None:
            body["pos"] = list(pos)
        if entity_id is not None:
            body["entity_id"] = entity_id
        if item:
            body["item"] = item
        return self.action("interact", body, stop_on=stop_on)

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
        return self.action("use_item", body, stop_on=stop_on)

    def attack(self, entity_id, timeout_ms=20000, *, stop_on=None) -> dict:
        return self.action("attack", {"entity_id": entity_id, "timeout": timeout_ms}, stop_on=stop_on)

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

    def posture(self, sneak=None, shield=None) -> dict:
        # 规范 §4.11：/posture 是全量覆盖，未写字段在服务端会复位为 false。
        # 无参调用保持 `{}` 全量复位语义；单参调用则合并当前已知姿态，防潜行搭桥时误松键坠落。
        if sneak is None and shield is None:
            body: dict = {}
            target_sneak, target_shield = False, False
        else:
            target_sneak = self._posture["sneak"] if sneak is None else bool(sneak)
            target_shield = self._posture["shield"] if shield is None else bool(shield)
            body = {"sneak": target_sneak, "shield": target_shield}
        res = self.call("posture", body, timeout=10)
        if isinstance(res, dict):
            self._posture["sneak"] = bool(res.get("sneaking", target_sneak))
            self._posture["shield"] = bool(res.get("blocking", target_shield))
        else:
            self._posture = {"sneak": target_sneak, "shield": target_shield}
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
        return self.call("connect", {}, timeout=timeout)

    def disconnect(self) -> dict:
        res = self.call("disconnect", {}, timeout=20)
        self._posture = {"sneak": False, "shield": False}
        self._last_disconnect_mono = time.monotonic()
        return res


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
