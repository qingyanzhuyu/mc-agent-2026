"""Minecraft Agent API 客户端（对应规范 v1.3.1）。

设计要点（都是血的教训，改的时候别退回原样）：
1. 业务错误一律 HTTP 200，必须看 payload["ok"]，不能看状态码。
2. 本机注册表里有死代理 127.0.0.1:7897，requests 会读它 -> 必须 trust_env=False。
3. token 会过期，401 invalid_token 要能自动重登；重登会踢掉旧连接。
4. 动作是同步阻塞的（move_to 走完才返回），所以 HTTP timeout 要按动作给。
"""
from __future__ import annotations

import json
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
    # 瞬时：隔一下再试
    "busy": RETRY,
    "timeout": RETRY,
    "not_loaded": RETRY,
    "rate_limited": RETRY,
    "not_ready": RETRY,
    "not_connected": RETRY,
    "network": RETRY,
    "bad_json": RETRY,
    # 目标/条件不成立：重规划
    "path_not_found": REPLAN,
    "interrupted": REPLAN,
    "death_loop_cooldown": REPLAN,
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
    "wrong_server": REPLAN,
    "protected_region": REPLAN,
    "drive_timeout": REPLAN,
    "dismounted": REPLAN,
    "dead": RETRY,  # 等重生，executor 里用长退避
    # 致命
    "unauthorized": FATAL,
    "invalid_token": FATAL,  # client 内部会先尝试重登
    "invalid_credentials": FATAL,
    "no_profile": FATAL,
    "unknown_api": FATAL,
    "bad_args": FATAL,
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
    def __init__(self, base: str, username: str, password: str, proxy: Optional[str] = None):
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.proxy = proxy

    @classmethod
    def load(cls, env_file: str | None = None) -> "Config":
        if env_file is None:
            env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_file):
            for line in open(env_file, encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return cls(
            base=os.environ.get("MC_API_BASE", DEFAULT_BASE),
            username=os.environ.get("MC_USER", ""),
            password=os.environ.get("MC_PASS", ""),
            proxy=os.environ.get("MC_PROXY") or None,
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
class Client:
    def __init__(self, cfg: Config, log=print):
        self.cfg = cfg
        self.log = log
        self.session = requests.Session()
        # 关键：不读环境/注册表里的代理
        self.session.trust_env = False
        if cfg.proxy:
            self.session.proxies = {"http": cfg.proxy, "https": cfg.proxy}
        self.token: Optional[str] = None
        self.expires_at: Optional[float] = None
        self.bot_username: Optional[str] = None
        self._auth_lock = threading.Lock()

    # ---- 底层 ----------------------------------------------------------
    def _send(self, method: str, path: str, body: Any = None, timeout: float = 30, auth: bool = True) -> dict:
        url = f"{self.cfg.base}/{path.lstrip('/')}"
        headers = {"Content-Type": "application/json"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            if method == "GET":
                r = self.session.get(url, headers=headers, params=body, timeout=timeout)
            else:
                r = self.session.post(url, headers=headers, data=json.dumps(body or {}), timeout=timeout)
        except requests.RequestException as e:
            raise ApiError("network", f"{type(e).__name__}: {e}") from e

        if r.status_code == 401:
            code = "invalid_token"
            try:
                code = r.json().get("error", {}).get("code", code)
            except ValueError:
                pass
            raise ApiError(code, "token 无效或过期", http=401)
        if r.status_code == 404:
            raise ApiError("unknown_api", f"路径不存在: {path}", http=404)
        if r.status_code == 400:
            raise ApiError("bad_json", "请求体不是合法 JSON", http=400)
        try:
            payload = r.json()
        except ValueError:
            raise ApiError("bad_json", r.text[:200], http=r.status_code)
        return payload

    def call(self, path: str, body: Any = None, method: str = "POST", timeout: float = 30) -> dict:
        """发一次调用，返回 result；失败抛 ApiError。401 会自动重登一次。"""
        self._ensure_token()
        payload = self._send(method, path, body, timeout)
        if payload.get("ok") is False:
            err = payload.get("error") or {}
            code = err.get("code", "unknown")
            if code in ("invalid_token", "unauthorized"):
                self.log(f"[auth] {code}，尝试重登")
                self.login()
                payload = self._send(method, path, body, timeout)
                if payload.get("ok") is False:
                    err = payload.get("error") or {}
                    raise ApiError(err.get("code", "unknown"), err.get("message", ""), err)
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
    def _ensure_token(self) -> None:
        if not self.token:
            self.login()
            return
        if self.expires_at and time.time() > self.expires_at - 120:
            self.log("[auth] token 快过期，提前重登")
            self.login()

    def login(self) -> None:
        if not self.cfg.has_credentials:
            raise ApiError("invalid_credentials", "没配 MC_USER / MC_PASS（见 mcagent/.env.example）")
        with self._auth_lock:
            payload = self._send(
                "POST", "auth/login",
                {"username": self.cfg.username, "password": self.cfg.password},
                timeout=20, auth=False,
            )
            if payload.get("ok") is False:
                err = payload.get("error") or {}
                raise ApiError(err.get("code", "invalid_credentials"), err.get("message", ""), err)
            self.token = payload["token"]
            self.expires_at = _parse_iso(payload.get("expires_at"))
            self.bot_username = payload.get("bot_username")
            self.log(f"[auth] 登录成功 bot={self.bot_username} token 到期={payload.get('expires_at')}")

    # ---- 连接 ----------------------------------------------------------
    def wait_ready(self, timeout: float = 120, interval: float = 1.0) -> dict:
        """登录后要经过代理大厅，5-15s 才真正进比赛服。ready=true 前发动作会 wrong_server。"""
        t0 = time.time()
        tried_connect = False
        last = {}
        while time.time() - t0 < timeout:
            try:
                last = self.status()
            except ApiError as e:
                if e.code in ("not_connected", "not_ready") and not tried_connect:
                    try:
                        self.call("connect", timeout=20)
                        tried_connect = True
                    except ApiError as e2:
                        if e2.code == "death_loop_cooldown":
                            self.log(f"[ready] 死亡循环冷却中: {e2.extra}")
                            time.sleep(min(30, float(e2.extra.get("cooldown_remaining_seconds", 30) or 30)))
                self.log(f"[ready] 等待中: {e}")
                time.sleep(interval)
                continue
            if last.get("ready"):
                self.log(f"[ready] 已进比赛服 ({time.time() - t0:.1f}s) pos={last.get('pos')}")
                return last
            time.sleep(interval)
        raise ApiError("not_ready", f"{timeout}s 内没 ready，最后状态: {json.dumps(last, ensure_ascii=False)[:300]}")

    # ---- 观察 ----------------------------------------------------------
    def status(self, timeout: float = 10) -> dict:
        return self.call("status", method="GET", timeout=timeout)

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
    def action(self, name: str, body: Any = None, timeout: Optional[float] = None) -> dict:
        return self.call(name, body or {}, timeout=timeout or ACTION_TIMEOUT.get(name, 30))

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, timeout=None) -> dict:
        body: dict = {"range": rng}
        if entity_id is not None:
            body["entity_id"] = entity_id
        else:
            body["x"], body["z"] = x, z
            if y is not None:
                body["y"] = y
        return self.action("move_to", body, timeout)

    def dig(self, pos, collect=True) -> dict:
        return self.action("dig", {"pos": list(pos), "collect": collect})

    def place(self, item, pos=None) -> dict:
        body: dict = {"item": item}
        if pos:
            body["pos"] = list(pos)
        return self.action("place", body)

    def interact(self, pos=None, entity_id=None, item=None) -> dict:
        body: dict = {}
        if pos:
            body["pos"] = list(pos)
        if entity_id is not None:
            body["entity_id"] = entity_id
        if item:
            body["item"] = item
        return self.action("interact", body)

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None) -> dict:
        body: dict = {"item": item}
        if target:
            body["target"] = target
        if ms is not None:
            body["ms"] = ms
        if timeout_ms is not None:
            body["timeout"] = timeout_ms
        if source:
            body["source"] = source
        return self.action("use_item", body)

    def attack(self, entity_id, timeout_ms=20000) -> dict:
        return self.action("attack", {"entity_id": entity_id, "timeout": timeout_ms})

    def shoot(self, entity_id=None, pos=None, weapon="bow", charge_ms=1100) -> dict:
        body: dict = {"weapon": weapon, "charge_ms": charge_ms}
        if entity_id is not None:
            body["entity_id"] = entity_id
        if pos:
            body["pos"] = list(pos)
        return self.action("shoot", body)

    def craft(self, item, count=1) -> dict:
        return self.action("craft", {"item": item, "count": count})

    def window_view(self, target) -> dict:
        return self.action("window/view", {"target": target})

    def window_act(self, target, ops) -> dict:
        return self.action("window/act", {"target": target, "ops": ops})

    def equip(self, item, slot="hand") -> dict:
        return self.action("equip", {"item": item, "slot": slot})

    def posture(self, sneak=None, shield=None) -> dict:
        body = {}
        if sneak is not None:
            body["sneak"] = sneak
        if shield is not None:
            body["shield"] = shield
        return self.call("posture", body, timeout=10)

    def dismount(self) -> dict:
        return self.action("dismount")

    def drop(self, item, count=1) -> dict:
        return self.action("drop", {"item": item, "count": count})

    def chat(self, message) -> dict:
        return self.action("chat", {"message": message[:100]})

    def stop(self) -> dict:
        return self.call("stop", {}, timeout=15)

    def connect(self, timeout=20) -> dict:
        return self.call("connect", {}, timeout=timeout)

    def disconnect(self) -> dict:
        return self.call("disconnect", {}, timeout=20)


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
