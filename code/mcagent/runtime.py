"""运行时：状态跟踪 / 安全闸 / 串行执行器 / 打点记录。

核心事实（来自 spec v1.3.1）：
- 动作互斥且同步阻塞，所以执行器必须是串行的单飞锁。
- /status、/events、/find_blocks 不占动作锁，动作进行中也能读。
- 60s 内死 3 次 -> 下线 5 分钟，这是唯一的一票否决，安全闸优先于任何计划。
"""
from __future__ import annotations

import json
import copy
import math
from dataclasses import dataclass, fields
from functools import wraps
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from .client import ApiError, FATAL, REPLAN, RETRY, Client


class Replan(Exception):
    """前置条件不成立，当前任务作废，换一个目标。"""


class Fatal(Exception):
    """认证/接口级错误，别继续硬跑。"""


@dataclass(frozen=True)
class RuntimeConfig:
    # 临时工程值，必须用官方 C1/寻路证据标定；不代表已验证的生存阈值。
    poll_safe_s: float = 4.0
    poll_risk_s: float = 1.0
    status_ttl_s: float = 1.0
    stop_cooldown_s: float = 10.0
    hp_crit: float = 8.0
    hp_risk: float = 14.0
    oxygen_warn: float = 80.0
    hostile_risk_dist: float = 16.0
    hostile_flee_dist: float = 6.0
    night_start: float = 13000.0
    night_end: float = 23000.0
    support_ttl_s: float = 5.0
    void_fall_delta: float = 0.5
    safe_cooldown_s: float = 65.0
    model_timeout_s: float = 8.0
    model_min_interval_s: float = 20.0
    model_review_s: float = 240.0
    model_failure_count: float = 2.0

    @classmethod
    def load(cls):
        values = {f.name: float(os.environ.get("MC_" + f.name.upper(), f.default)) for f in fields(cls)}
        if any(not math.isfinite(v) or v <= 0 for v in values.values()):
            raise ValueError("MC 运行时阈值必须为有限正数")
        if values["poll_risk_s"] > values["poll_safe_s"] or values["hp_crit"] > values["hp_risk"]:
            raise ValueError("风险轮询不能比安全轮询慢，危急血量不能高于预警血量")
        if values["safe_cooldown_s"] < 65:
            raise ValueError("死亡循环冷却不能低于 65 秒")
        return cls(**values)


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


# ---------------------------------------------------------------- 状态
class State:
    #: 触发接近死亡循环（60s 内 ≥2 死）后的强制避险冷却时长（秒，留 5s 缓冲防窗口边界抖动）
    SAFE_COOLDOWN_S: float = 65.0

    def __init__(self, config: Optional[RuntimeConfig] = None):
        self.config = config or RuntimeConfig.load()
        self.SAFE_COOLDOWN_S = self.config.safe_cooldown_s
        self._lock = threading.RLock()
        self._status_fetch = threading.Lock()
        self._inventory_fetch = threading.Lock()
        self._event_fetch = threading.Lock()
        self.status_at = float("-inf")
        self.inventory_at = float("-inf")
        self.events_at = float("-inf")
        self.inventory_dirty = True
        self._inventory_generation = 0
        self._status_generation = 0
        self.safety_intent: Optional[dict] = None
        self.risk_context = {"near_water": False, "near_void": False, "void_guard": False}
        self.support: dict = {}
        self.st: dict = {}
        self.last_seq: int = 0
        self.events: deque = deque(maxlen=400)
        self.deaths: deque = deque(maxlen=20)  # 本机 monotonic 秒时间戳，免疫客户端/服务端时钟差
        self.inventory: dict = {}
        self.safe_mode_until: float = 0.0
        self._status_booted: bool = False
        self._events_booted: bool = False
        self._seen_death_t_ms: set[int] = set()
        self._seen_death_seqs: set[int] = set()
        self._unlinked_alive_death_mono: Optional[float] = None

    @_locked
    def _record_death(
        self,
        mono_ts: Optional[float] = None,
        server_t_ms: Optional[int] = None,
        seq: Optional[int] = None,
    ) -> bool:
        """去重记录一次真实死亡（融合 status.alive 翻转、status.last_death.t 与 events.death）。"""
        if seq is not None and seq in self._seen_death_seqs:
            return False
        if server_t_ms is not None:
            for seen_t in self._seen_death_t_ms:
                if abs(seen_t - server_t_ms) <= 2000:
                    if seq is not None:
                        self._seen_death_seqs.add(seq)
                    self._seen_death_t_ms.add(server_t_ms)
                    return False
            self._seen_death_t_ms.add(server_t_ms)
        if seq is not None:
            self._seen_death_seqs.add(seq)

        now_mono = time.monotonic()
        ts = now_mono if mono_ts is None else mono_ts

        # 若前一拍刚因 alive: True->False 先记了一次未绑定 server_t 的死亡，10s 内合并为同一次
        if (
            server_t_ms is not None
            and self._unlinked_alive_death_mono is not None
            and abs(ts - self._unlinked_alive_death_mono) <= 10.0
        ):
            self._unlinked_alive_death_mono = None
            return False

        self.deaths.append(ts)
        if self.deaths_in(60) >= 2:
            self.safe_mode_until = max(self.safe_mode_until, now_mono + self.SAFE_COOLDOWN_S)
        return True

    # ---- 更新
    @_locked
    def set_status(self, st: dict) -> None:
        was_booted = self._status_booted
        prev_alive = self.st.get("alive", True)
        self.st = copy.deepcopy(st)
        self.status_at = time.monotonic()
        self._status_booted = True

        now_mono = time.monotonic()
        ld = st.get("last_death") or {}
        raw_t = ld.get("t")
        ld_t_ms: Optional[int] = int(raw_t) if isinstance(raw_t, (int, float)) and raw_t > 0 else None

        alive_now = st.get("alive", True)
        if not alive_now and prev_alive:
            if ld_t_ms is not None and ld_t_ms not in self._seen_death_t_ms:
                self._record_death(mono_ts=now_mono, server_t_ms=ld_t_ms)
            else:
                # 死亡刚发生但服务端尚未刷新 last_death.t：先记一笔待合并的本地死亡
                if self._record_death(mono_ts=now_mono):
                    self._unlinked_alive_death_mono = now_mono
            return

        if ld_t_ms is not None and ld_t_ms not in self._seen_death_t_ms:
            if was_booted:
                # 运行期 last_death.t 变成新值 = 刚刚发生一次新死亡（完全不依赖本机/服务端时钟对齐）
                self._record_death(mono_ts=now_mono, server_t_ms=ld_t_ms)
            else:
                # 首次拉状态：标记该历史 last_death.t 已见，防后续轮询重复追加；
                # 若死亡刚在重启前不久发生（按墙钟 0~300s 内），按相对秒数折算进窗口以衔接崩溃恢复
                age_s = time.time() - (ld_t_ms / 1000.0)
                if -5.0 <= age_s <= 300.0:
                    self._record_death(mono_ts=now_mono - max(0.0, age_s), server_t_ms=ld_t_ms)
                else:
                    self._seen_death_t_ms.add(ld_t_ms)

    @_locked
    def set_inventory(self, inv: dict) -> None:
        self.inventory = copy.deepcopy(inv)
        self.inventory_at = time.monotonic()
        self.inventory_dirty = False

    @_locked
    def invalidate_inventory(self) -> None:
        self.inventory_dirty = True
        self._inventory_generation += 1

    @_locked
    def patch_action(self, result: dict) -> None:
        # 动作返回只更新已知字段，不能把整份旧 status 冒充为新鲜状态。
        self._status_generation += 1
        for key in ("pos", "health", "food"):
            if key in result:
                self.st = {**self.st, key: copy.deepcopy(result[key])}

    def refresh_status(self, client: Client, max_age_s: Optional[float] = None) -> dict:
        ttl = self.config.status_ttl_s if max_age_s is None else max_age_s
        # 锁内再次判断，合并主循环与 monitor 的并发补读。
        with self._status_fetch:
            with self._lock:
                if self.st and time.monotonic() - self.status_at <= ttl:
                    return copy.deepcopy(self.st)
                generation = self._status_generation
            result = client.status()
            with self._lock:
                if generation == self._status_generation:
                    self.set_status(result)
                # 读取途中有动作完成：丢弃可能过时的快照，下一次补读。
                return copy.deepcopy(self.st)

    def refresh_inventory(self, client: Client) -> dict:
        with self._inventory_fetch:
            with self._lock:
                if not self.inventory_dirty:
                    return copy.deepcopy(self.inventory)
                generation = self._inventory_generation
            result = client.inventory()
            with self._lock:
                self.set_inventory(result)
                self.inventory_dirty = generation != self._inventory_generation
                return copy.deepcopy(self.inventory)

    @_locked
    def set_risk_context(self, **flags: bool) -> None:
        if set(flags) - set(self.risk_context):
            raise ValueError("未知风险上下文")
        self.risk_context.update(flags)

    @_locked
    def get_risk_context(self) -> dict[str, bool]:
        return dict(self.risk_context)

    @_locked
    def get_support(self) -> dict:
        return copy.deepcopy(self.support)

    @_locked
    def set_support(self, evidence: dict) -> None:
        self.support = copy.deepcopy(evidence)

    @_locked
    def snapshot(self) -> dict:
        return copy.deepcopy(self.st)

    @_locked
    def set_safety_intent(self, hazard: Optional[dict]) -> None:
        self.safety_intent = copy.deepcopy(hazard)

    def drain(self, client: Client, limit: int = 200) -> list[dict]:
        """首次启动先追平历史积压（防数小时前旧 death 误计为实时死亡），运行期每拍读一页防挤占急停额度。"""
        with self._event_fetch:
            return self._drain(client, limit)

    def _drain(self, client: Client, limit: int) -> list[dict]:
        with self._lock:
            booting = not self._events_booted
            last_seq = self.last_seq
        if not booting:
            result = client.events(since=last_seq, limit=limit)
            return self._consume_events(result)

        combined: list[dict] = []
        still_more = False
        for _ in range(20):
            r = client.events(since=last_seq, limit=limit)
            evs = r.get("events") or []
            last_seq = r.get("last_seq", last_seq)
            combined.extend(evs)
            still_more = bool(r.get("has_more"))
            if not still_more or not evs:
                break
        return self._consume_events(
            {"events": combined, "last_seq": last_seq, "has_more": still_more}
        )

    @_locked
    def _consume_events(self, result: dict) -> list[dict]:
        was_booted = self._events_booted
        out = result.get("events") or []
        self.last_seq = result.get("last_seq", self.last_seq)
        self.events.extend(out)
        if not result.get("has_more"):
            self._events_booted = True
        self.events_at = time.monotonic()

        if out:
            now_mono = time.monotonic()
            # 首批事件用同批最大服务端时间戳做相对年龄参照，彻底消除客户端时钟偏差
            ref_t_ms = max(
                (int(e["t"]) for e in out if isinstance(e.get("t"), (int, float))),
                default=None,
            )
            for ev in out:
                etype = ev.get("type")
                seq = ev.get("seq") if isinstance(ev.get("seq"), int) else None
                raw_t = ev.get("t")
                ev_t_ms = int(raw_t) if isinstance(raw_t, (int, float)) else None
                if etype == "pickup":
                    self.invalidate_inventory()
                if etype == "death":
                    self.invalidate_inventory()
                    if was_booted:
                        self._record_death(mono_ts=now_mono, server_t_ms=ev_t_ms, seq=seq)
                    elif ev_t_ms is not None and ref_t_ms is not None:
                        age_s = max(0.0, (ref_t_ms - ev_t_ms) / 1000.0)
                        if age_s <= 300.0:
                            self._record_death(mono_ts=now_mono - age_s, server_t_ms=ev_t_ms, seq=seq)
                        else:
                            if seq is not None:
                                self._seen_death_seqs.add(seq)
                            self._seen_death_t_ms.add(ev_t_ms)
                    else:
                        self._record_death(mono_ts=now_mono, server_t_ms=ev_t_ms, seq=seq)
                elif etype == "death_loop":
                    self.safe_mode_until = max(self.safe_mode_until, now_mono + self.SAFE_COOLDOWN_S)
        return out

    # ---- 便捷读
    @property
    def pos(self) -> list[float]:
        return self.st.get("pos") or [0, 0, 0]

    @property
    def hp(self) -> float:
        return float(self.st.get("health", 20))

    @property
    def ready(self) -> bool:
        return bool(self.st.get("ready"))

    @property
    def in_safe_mode(self) -> bool:
        return time.monotonic() < self.safe_mode_until

    @property
    def safe_mode_remaining(self) -> float:
        return max(0.0, self.safe_mode_until - time.monotonic())

    @_locked
    def count(self, item: str) -> int:
        inv = self.inventory or {}
        total = 0
        seen_slots: set[int] = set()
        for it in (inv.get("items") or []):
            if not isinstance(it, dict):
                continue
            slot = it.get("slot")
            if isinstance(slot, int):
                seen_slots.add(slot)
            if it.get("name") == item:
                total += int(it.get("count", 0))
        # 穿在身上的护甲与副手盾牌不一定出现在 items 列表，按 slot 去重合并统计
        armor = inv.get("armor") or {}
        extra_slots = list(armor.values()) if isinstance(armor, dict) else []
        extra_slots.append(inv.get("offhand"))
        for eq in extra_slots:
            if not isinstance(eq, dict) or eq.get("name") != item:
                continue
            slot = eq.get("slot")
            if isinstance(slot, int) and slot in seen_slots:
                continue
            if isinstance(slot, int):
                seen_slots.add(slot)
            total += int(eq.get("count", 1))
        return total

    @_locked
    def deaths_in(self, window_s: float = 60) -> int:
        now = time.monotonic()
        return sum(1 for t in self.deaths if 0 <= now - t <= window_s)

    def summary(self) -> str:
        st = self.st
        return (
            f"pos={st.get('pos')} hp={st.get('health')} food={st.get('food')} "
            f"dim={st.get('dimension')} held={(st.get('held') or {}).get('name')} "
            f"nearest_hostile={st.get('nearest_hostile')}"
        )


# ---------------------------------------------------------------- 安全闸
class Safety:
    """纯判断：urgent 抢占，soft 留给执行层在动作边界切换技能。"""

    def __init__(self, config: Optional[RuntimeConfig] = None):
        self.config = config or RuntimeConfig.load()

    def hazards(self, s: State) -> list[dict]:
        st = s.snapshot()
        if not st:
            return [{"kind": "no_status", "severity": "soft"}]
        if not st.get("alive", True):
            return [{"kind": "wait_respawn", "severity": "soft"}]
        out = []

        def add(kind: str, why: str, severity: str = "urgent", **extra: Any) -> None:
            out.append({"kind": kind, "why": why, "severity": severity, **extra})

        # 致命物理险情（岩浆/缺氧/失重坠落）必须排在 safe_mode 之前，避免 65s 避险锁存遮蔽当前逃生意图
        if st.get("in_lava"):
            add("escape_lava", "在岩浆里")
        if st.get("submerged") and float(st.get("oxygen", 300)) < self.config.oxygen_warn:
            add("surface", f"氧气 {st.get('oxygen')}")
        support = s.get_support()
        if (
            support.get("risk")
            and time.monotonic() - support.get("at", 0) <= self.config.support_ttl_s
            and support.get("status_pos") == st.get("pos")
        ):
            add("void_risk", support["risk"])
        if s.in_safe_mode:
            add(
                "safe_mode",
                f"接近死亡循环，锁存避险剩 {s.safe_mode_remaining:.0f}s",
                remaining_s=round(s.safe_mode_remaining, 1),
            )
        hp = float(st.get("health", 20))
        if hp <= self.config.hp_crit:
            add("heal", f"血量 {hp}", "soft")
        hostile = st.get("nearest_hostile") or {}
        if hostile and float(hostile.get("dist", 99)) < self.config.hostile_flee_dist:
            add(
                "flee",
                f"{hostile.get('name')} 距离 {hostile.get('dist')}",
                "soft",
                hostile=hostile.get("name"),
                dist=hostile.get("dist"),
            )
        if st.get("on_fire") and not st.get("in_water"):
            add("douse", "着火", "soft")
        return out

    def check(self, s: State) -> Optional[dict]:
        hazards = self.hazards(s)
        return hazards[0] if hazards else None


# 响应丢失时不可盲目重发的非幂等动作（可能再次消耗材料、弹药或耐久）
NON_IDEMPOTENT_ACTIONS: frozenset[str] = frozenset({
    "craft", "window/act", "window_act", "use_item", "place", "interact", "drop", "shoot",
})
# 执行前需确保背包快照新鲜，以便断流后按产物/消耗做确定性判重的动作（避免搭桥 place 等每步多发一次 GET /inventory）
SNAPSHOT_INVENTORY_ACTIONS: frozenset[str] = frozenset({
    "craft", "window/act", "window_act", "use_item",
})
# 超时或网络中断后需主动发 /stop 清理服务端持续寻路/挖掘/追击的动作
STOP_ON_TIMEOUT_ACTIONS: frozenset[str] = frozenset({
    "move_to", "dig", "attack", "use_item",
})


def _inv_fingerprint(inv: Optional[dict]) -> Optional[dict[str, int]]:
    if not isinstance(inv, dict):
        return None
    tmp = State()
    tmp.set_inventory(inv)
    names: set[str] = set()
    for it in (inv.get("items") or []):
        if isinstance(it, dict) and it.get("name"):
            names.add(it["name"])
    armor = inv.get("armor") or {}
    if isinstance(armor, dict):
        for eq in armor.values():
            if isinstance(eq, dict) and eq.get("name"):
                names.add(eq["name"])
    off = inv.get("offhand")
    if isinstance(off, dict) and off.get("name"):
        names.add(off["name"])
    return {n: tmp.count(n) for n in names if tmp.count(n) > 0}


# ---------------------------------------------------------------- 执行器
class Executor:
    """串行执行动作：可抢占单飞锁 + 错误分类 + 非幂等超时判重 + 超时兜底 /stop + 打点。"""

    def __init__(self, client: Client, state: State, log_path: Optional[str] = None, log=print):
        self.client = client
        self.state = state
        self.log = log
        self.log_path = log_path
        self.n_actions = 0
        self.n_errors = 0
        self._cond = threading.Condition()
        self._busy = False
        self.active_action: Optional[str] = None
        self._owner_tid: Optional[int] = None
        self._emergency_pending = 0
        self._emergency_tid: Optional[int] = None
        self._preempt_epoch = 0
        self._preempt_reason = ""

    def _record(self, row: dict) -> None:
        if not self.log_path:
            return
        parent = os.path.dirname(self.log_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _snapshot_before_non_idempotent(self, name: str) -> Optional[dict[str, int]]:
        if name not in NON_IDEMPOTENT_ACTIONS:
            return None
        if name in SNAPSHOT_INVENTORY_ACTIONS:
            try:
                self.state.refresh_inventory(self.client)
            except Exception:
                return None
        elif self.state.inventory_dirty or not self.state.inventory:
            return None
        return _inv_fingerprint(self.state.inventory)

    def _check_ambiguous_effect(
        self,
        name: str,
        before_fp: Optional[dict[str, int]],
        verify_done: Optional[Callable[[dict[str, int], dict[str, int]], bool]],
        busy_polls: int = 3,
        busy_interval_s: float = 0.4,
    ) -> tuple[str, Optional[dict]]:
        """网络超时/断流后核实服务端状态：先等 busy 结束，再比对背包确认副作用是否已发生。"""
        still_busy = False
        for _ in range(busy_polls):
            try:
                st = self.client.status()
                self.state.set_status(st)
                if not st.get("busy"):
                    still_busy = False
                    break
                still_busy = True
            except Exception:
                break
            time.sleep(busy_interval_s)

        if still_busy or name in STOP_ON_TIMEOUT_ACTIONS:
            self.stop()

        if name not in NON_IDEMPOTENT_ACTIONS:
            return "unchanged", None

        try:
            after_inv = self.client.inventory()
            self.state.set_inventory(after_inv)
        except Exception:
            return "unknown", None

        after_fp = _inv_fingerprint(after_inv) or {}
        base_fp = before_fp or {}
        if verify_done is not None and before_fp is not None and verify_done(base_fp, after_fp):
            return "done", {"recovered": True, "inventory": after_inv}
        if before_fp is not None and after_fp != base_fp:
            return "changed", None
        return "unchanged", None

    def _recover_session_if_needed(self, e: ApiError, backoff: float, attempt: int) -> Optional[float]:
        """处理会话态错误（掉线/切服大厅/死亡冷却/限流），返回还需额外 sleep 的秒数（None 表示已处置完毕）。"""
        if e.code == "wrong_server":
            self.log("  [session] wrong_server（在代理大厅），等待 ready...")
            self.client.wait_ready(timeout=45)
            return 0.0
        if e.code == "not_connected":
            self.log("  [session] not_connected，按 ≥30s 间隔重连并等待 ready...")
            self.client.ensure_connected()
            self.client.wait_ready(timeout=60)
            return 0.0
        if e.code == "death_loop_cooldown":
            rem = float(e.extra.get("cooldown_remaining_seconds", 60) or 60)
            wait_s = min(310.0, max(1.0, rem + 2.0))
            self.log(f"  [session] death_loop_cooldown，休眠 {wait_s:.1f}s 后重连")
            time.sleep(wait_s)
            self.client.connect(timeout=20)
            self.client.wait_ready(timeout=60)
            return 0.0
        if e.code == "rate_limited":
            ra = e.extra.get("retry_after_s")
            if isinstance(ra, (int, float)) and ra > 0:
                return float(ra)
        wait = backoff * (2 ** (attempt - 1))
        if e.code == "dead":
            wait = min(15.0, wait * 3)
        return wait

    def emergency(self, reason: str, escape_fn: Optional[Callable[["Executor"], Any]] = None) -> Any:
        """安全层最高优先级抢占：挂起后续普通动作 -> 立即 POST /stop -> 独占执行逃生动作序列。"""
        tid = threading.get_ident()
        with self._cond:
            self._emergency_pending += 1
            self._preempt_epoch += 1
            self._preempt_reason = reason
            prev_em_tid = self._emergency_tid
            self._emergency_tid = tid
        try:
            self.log(f"  [safety] 紧急抢占 ({reason}) -> /stop")
            self.stop(raise_on_error=True)
            if escape_fn is not None:
                return escape_fn(self)
            return None
        finally:
            with self._cond:
                self._emergency_tid = prev_em_tid
                self._emergency_pending -= 1
                self._cond.notify_all()

    def act(
        self,
        name: str,
        fn: Callable[[], dict],
        retries: int = 2,
        backoff: float = 0.6,
        *,
        verify_done: Optional[Callable[[dict[str, int], dict[str, int]], bool]] = None,
        priority: str = "normal",
    ) -> dict:
        tid = threading.get_ident()
        with self._cond:
            eff_priority = "emergency" if tid == self._emergency_tid else priority
            if self._busy and self._owner_tid == tid:
                raise RuntimeError(f"动作互斥：同一线程重入动作 {name}")
            if eff_priority == "emergency":
                while self._busy:
                    self._cond.wait()
            else:
                if self._emergency_pending > 0:
                    while self._emergency_pending > 0 or self._busy:
                        self._cond.wait()
                elif self._busy:
                    raise RuntimeError(f"动作互斥：上一个动作还没结束就想发 {name}")
            self._busy = True
            self.active_action = name
            self._owner_tid = tid
            start_epoch = self._preempt_epoch

        t0 = time.time()
        attempt = 0
        try:
            before_fp = self._snapshot_before_non_idempotent(name)
            while True:
                if eff_priority != "emergency" and self._preempt_epoch != start_epoch:
                    raise Replan(f"动作被安全层抢占: {self._preempt_reason}")
                attempt += 1
                try:
                    result = fn()
                    self.n_actions += 1
                    self._record({
                        "t": round(t0, 3), "name": name, "ms": round((time.time() - t0) * 1000),
                        "ok": True, "attempt": attempt, "result": _shrink(result),
                    })
                    if isinstance(result, dict) and any(k in result for k in ("pos", "health", "food")):
                        self.state.patch_action(result)
                    return result
                except ApiError as e:
                    self.n_errors += 1
                    self._record({
                        "t": round(t0, 3), "name": name, "ms": round((time.time() - t0) * 1000),
                        "ok": False, "attempt": attempt, "code": e.code, "msg": e.message,
                        "kind": e.kind, "extra": _shrink(e.extra),
                    })
                    if eff_priority != "emergency" and self._preempt_epoch != start_epoch:
                        raise Replan(f"动作被安全层抢占: {self._preempt_reason}") from e
                    if e.kind == FATAL:
                        raise Fatal(str(e)) from e

                    # 超时 / 网络断流：先止动 /stop，再核对非幂等动作是否已在服务端生效
                    if e.code == "timeout" and name in STOP_ON_TIMEOUT_ACTIONS:
                        self.stop()
                    elif e.code == "network":
                        status_tag, recovered = self._check_ambiguous_effect(name, before_fp, verify_done)
                        if status_tag == "done" and recovered is not None:
                            self.n_actions += 1
                            self._record({
                                "t": round(t0, 3), "name": name, "ms": round((time.time() - t0) * 1000),
                                "ok": True, "attempt": attempt, "recovered": True,
                            })
                            return recovered
                        if status_tag == "changed":
                            raise Replan(f"{name} 响应丢失但服务端背包已变化，停止重试防重复消耗") from e
                        if name in NON_IDEMPOTENT_ACTIONS and (status_tag == "unknown" or before_fp is None):
                            raise Replan(f"{name} 响应丢失且无法可靠判重，停止重试") from e

                    if e.kind == RETRY and attempt <= retries:
                        try:
                            wait_s = self._recover_session_if_needed(e, backoff, attempt)
                        except ApiError as rec_err:
                            if rec_err.kind == FATAL:
                                raise Fatal(str(rec_err)) from rec_err
                            raise Replan(f"会话恢复失败: {rec_err}") from rec_err
                        if wait_s and wait_s > 0:
                            self.log(f"  [{name}] {e} -> {wait_s:.1f}s 后重试 ({attempt}/{retries})")
                            time.sleep(wait_s)
                        continue
                    raise Replan(str(e)) from e
        finally:
            if name in NON_IDEMPOTENT_ACTIONS or name in ("dig", "attack", "equip"):
                self.state.invalidate_inventory()
            with self._cond:
                self.active_action = None
                self._busy = False
                self._owner_tid = None
                self._cond.notify_all()

    # ---- 常用包装（顺便统一超时/参数与非幂等判重） --------------------
    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, *, stop_on=None) -> dict:
        return self.act(
            "move_to",
            lambda: self.client.move_to(x, z, y=y, entity_id=entity_id, rng=rng, pos=pos, stop_on=stop_on),
        )

    def dig(self, pos, collect: bool = True, *, stop_on=None) -> dict:
        return self.act("dig", lambda: self.client.dig(pos, collect=collect, stop_on=stop_on))

    def place(self, item, pos=None, *, stop_on=None) -> dict:
        return self.act("place", lambda: self.client.place(item, pos, stop_on=stop_on))

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None) -> dict:
        return self.act("interact", lambda: self.client.interact(pos, entity_id, item, stop_on=stop_on))

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None) -> dict:
        return self.act(
            "use_item",
            lambda: self.client.use_item(
                item, target=target, ms=ms, timeout_ms=timeout_ms, source=source, stop_on=stop_on
            ),
        )

    def craft(self, item, count=1, *, stop_on=None) -> dict:
        return self.act(
            "craft",
            lambda: self.client.craft(item, count, stop_on=stop_on),
            verify_done=lambda before, after: after.get(item, 0) > before.get(item, 0),
        )

    def attack(self, entity_id, timeout_ms=20000, *, stop_on=None) -> dict:
        return self.act("attack", lambda: self.client.attack(entity_id, timeout_ms, stop_on=stop_on))

    def shoot(self, entity_id=None, pos=None, weapon="bow", charge_ms=1100, *, stop_on=None) -> dict:
        return self.act(
            "shoot",
            lambda: self.client.shoot(
                entity_id=entity_id, pos=pos, weapon=weapon, charge_ms=charge_ms, stop_on=stop_on
            ),
        )

    def window_view(self, target, *, stop_on=None) -> dict:
        return self.act("window/view", lambda: self.client.window_view(target, stop_on=stop_on))

    def window_act(self, target, ops, *, stop_on=None) -> dict:
        return self.act("window/act", lambda: self.client.window_act(target, ops, stop_on=stop_on))

    def equip(self, item, slot="hand", *, stop_on=None) -> dict:
        return self.act("equip", lambda: self.client.equip(item, slot, stop_on=stop_on))

    def posture(self, sneak=None, shield=None) -> dict:
        # 规范 §4.11：/posture 不占动作锁，可在动作阻塞中途调用
        t0 = time.time()
        res = self.client.posture(sneak=sneak, shield=shield)
        self._record({
            "t": round(t0, 3), "name": "posture", "ms": round((time.time() - t0) * 1000),
            "ok": True, "attempt": 1, "result": _shrink(res),
        })
        return res

    def dismount(self, *, stop_on=None) -> dict:
        return self.act("dismount", lambda: self.client.dismount(stop_on=stop_on))

    def drop(self, item, count=1, *, stop_on=None) -> dict:
        return self.act("drop", lambda: self.client.drop(item, count, stop_on=stop_on))

    def chat(self, message, *, stop_on=None) -> dict:
        return self.act("chat", lambda: self.client.chat(message, stop_on=stop_on))

    def stop(self, *, raise_on_error: bool = False) -> None:
        try:
            self.client.stop()
        except Exception as e:
            self.log(f"  [stop] 失败: {e}")
            if raise_on_error:
                raise


# ---------------------------------------------------------------- 后台监控与抢占闭环
class SafetyMonitor:
    """唯一定时 status/events 读取者；持续危险一次抢占，软危险只发布意图。"""

    def __init__(self, client: Client, state: State, executor: Executor,
                 safety: Optional[Safety] = None,
                 on_hazard: Optional[Callable[[dict, Executor, State], None]] = None,
                 interval_s: Optional[float] = None):
        self.client, self.state, self.executor = client, state, executor
        self.config = state.config
        self.safety = safety or Safety(self.config)
        self.on_hazard = on_hazard
        self.interval_s = interval_s
        self._active: set[str] = set()
        self._triggered: dict[str, float] = {}
        self._last_y: Optional[float] = None
        self._stop_ev = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def next_interval(self) -> float:
        if self.interval_s is not None:
            return self.interval_s
        st, cfg = self.state.snapshot(), self.config
        rc = self.state.get_risk_context()
        hostile = st.get("nearest_hostile") or {}
        tod = float(st.get("time_of_day", 0)) % 24000
        risk = (not st or cfg.night_start <= tod <= cfg.night_end
                or "nether" in st.get("dimension", "")
                or float(st.get("health", 20)) < cfg.hp_risk
                or float(hostile.get("dist", 999)) <= cfg.hostile_risk_dist
                or st.get("in_water") or st.get("in_lava") or st.get("on_fire")
                or st.get("on_ground") is False or any(rc.values()))
        return cfg.poll_risk_s if risk else cfg.poll_safe_s

    def _check_support(self) -> None:
        st = self.state.snapshot()
        pos = st.get("pos")
        rc = self.state.get_risk_context()
        guard = rc["void_guard"] or rc["near_void"]
        if not pos or st.get("in_water") or st.get("in_vehicle") or not st.get("alive", True):
            self._last_y = None
            self.state.set_support({})
            return
        y = float(pos[1])
        prev_y = self._last_y
        falling = prev_y is not None and prev_y - y >= self.config.void_fall_delta
        ascending = prev_y is not None and y - prev_y > 0.05
        self._last_y = y
        on_ground = st.get("on_ground")
        # 潜行且仍在地面上（如搭桥探边准备放下一块）受原版潜行防坠保护，不把待放空气格误判为坠落
        if on_ground is True and st.get("sneaking") and not falling:
            self.state.set_support({
                "pos": [math.floor(pos[0]), math.floor(y - 0.01), math.floor(pos[2])],
                "status_pos": pos,
                "at": time.monotonic(),
                "sneak_edge": True,
            })
            return
        # 高风险区正常起跳上升瞬间（刚离地），不把起跳前的空气格误判为失重悬空
        if guard and on_ground is False and ascending:
            self.state.set_support({})
            return
        if not guard and on_ground is not False:
            self.state.set_support({})
            return
        # y-epsilon 正确处理整格顶面与半砖；空气只表示这一格无支撑，非“证明无限虚空”。
        under = [math.floor(pos[0]), math.floor(y - 0.01), math.floor(pos[2])]
        evidence: dict[str, Any] = {"pos": under, "status_pos": pos}
        try:
            block = self.client.get_block(under)
            evidence["block"] = block
            name = str(block.get("name", "")).removeprefix("minecraft:")
            if name in ("air", "cave_air", "void_air"):
                if guard:
                    # 玩家水平碰撞箱 0.6×0.6（半宽 0.3，叠加上 /status 一位小数四舍五入容差 0.05 → ±0.35）：
                    # 非潜行走过单格桥/窄边且服务端物理判定 on_ground=True 时，中心点可能落在相邻空气格，
                    # 需核对碰撞箱四角是否存在实心承重块，避免把正常贴边行走误判为悬空并误发 /stop。
                    has_aabb_support = False
                    if on_ground is True and not falling:
                        corners = sorted({
                            (math.floor(pos[0] + dx), under[1], math.floor(pos[2] + dz))
                            for dx in (-0.35, 0.35) for dz in (-0.35, 0.35)
                        } - {tuple(under)})
                        aabb_checked = []
                        for cx, cy, cz in corners:
                            cpos = [cx, cy, cz]
                            cb = self.client.get_block(cpos)
                            cname = str(cb.get("name", "")).removeprefix("minecraft:")
                            aabb_checked.append({"pos": cpos, "name": cname})
                            if cname and cname not in ("air", "cave_air", "void_air", "lava"):
                                has_aabb_support = True
                                evidence["aabb_support"] = {"pos": cpos, "name": cname}
                                break
                        if aabb_checked:
                            evidence["aabb_checked"] = aabb_checked
                    if not has_aabb_support:
                        evidence["risk"] = "脚下空气：疑似无支撑/坠落，尚不能证明是虚空"
                elif falling:
                    # 普通地形下落时再往下探 2 格，区分正常下 1~2 格台阶与真悬空/深坑
                    has_near_ground = False
                    below_checked = []
                    for dy in (1, 2):
                        bpos = [under[0], under[1] - dy, under[2]]
                        b = self.client.get_block(bpos)
                        bname = str(b.get("name", "")).removeprefix("minecraft:")
                        below_checked.append({"pos": bpos, "name": bname})
                        if bname and bname not in ("air", "cave_air", "void_air", "lava"):
                            has_near_ground = True
                            break
                    evidence["below_checked"] = below_checked
                    if not has_near_ground:
                        evidence["risk"] = "脚下连续空气且正在下落：疑似坠落/深坑"
            elif not name and guard:
                evidence["risk"] = "高风险路径脚下方块未知"
        except ApiError as e:
            evidence["error"] = e.code
            if guard:
                evidence["risk"] = "高风险路径支撑查询失败，不能按安全处理"
        # 在 get_block 返回后打时间戳，防官方慢 RTT 下刚返回即超过 support_ttl_s
        evidence["at"] = time.monotonic()
        self.state.set_support(evidence)

    def tick(self) -> Optional[dict]:
        self.state.refresh_status(self.client)
        # 先处理致命状态，再查支撑与事件，避免慢请求延误已观察到的岩浆/缺氧抢占。
        hazard = self._dispatch(reconcile=False)
        self._check_support()
        hazard = self._dispatch(reconcile=True) or hazard
        self.state.drain(self.client)
        after_events = self._dispatch(reconcile=False)
        return after_events or hazard

    def _dispatch(self, reconcile: bool = True) -> Optional[dict]:
        hazards = self.safety.hazards(self.state)
        for hazard in hazards:
            if hazard["kind"] == "heal" and self.executor.active_action in ("attack", "shoot"):
                hazard["severity"] = "urgent"
        self.state.set_safety_intent(hazards[0] if hazards else None)
        urgent = [h for h in hazards if h["severity"] == "urgent"]
        keys = {h["kind"] for h in urgent}
        # 持续存在的危险不重新触发；解除后也要过冷静期，防止阈值抖动。
        if reconcile:
            self._active.intersection_update(keys)
        now = time.monotonic()
        fresh = [h for h in urgent if h["kind"] not in self._active
                 and now - self._triggered.get(h["kind"], float("-inf")) >= self.config.stop_cooldown_s]
        if fresh:
            hazard = fresh[0]
            reason = "; ".join(f"{h['kind']}: {h.get('why', '')}" for h in fresh)
            def after_stop(ex):
                # 在成功 stop 后、逃生回调前锁存；逃生失败也不能重新循环急停。
                for h in fresh:
                    self._active.add(h["kind"])
                    self._triggered[h["kind"]] = now
                if self.on_hazard:
                    return self.on_hazard(hazard, ex, self.state)
            self.executor.emergency(reason, after_stop)
        return hazards[0] if hazards else None

    def _loop(self) -> None:
        while not self._stop_ev.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception as e:
                self.executor.log(f"  [monitor] 轮询异常: {e}")
            # 从轮询开始计时，慢 RTT 时不堆积补发。
            self._stop_ev.wait(max(0.01, self.next_interval() - (time.monotonic() - started)))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_ev.clear()
        self._thread = threading.Thread(target=self._loop, name="mcagent-safety-monitor", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 3.0) -> None:
        self._stop_ev.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)


def _shrink(obj: Any, depth: int = 0) -> Any:
    """日志别把整包背包写进去。"""
    if depth > 3:
        return "..."
    if isinstance(obj, dict):
        return {k: _shrink(v, depth + 1) for k, v in list(obj.items())[:12]}
    if isinstance(obj, list):
        return [_shrink(v, depth + 1) for v in obj[:6]]
    if isinstance(obj, str) and len(obj) > 120:
        return obj[:120] + "…"
    return obj
