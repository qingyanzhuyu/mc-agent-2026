"""运行时：状态跟踪 / 安全闸 / 串行执行器 / 打点记录。

核心事实（来自 spec v1.3.1）：
- 动作互斥且同步阻塞，所以执行器必须是串行的单飞锁。
- /status、/events、/find_blocks 不占动作锁，动作进行中也能读。
- 60s 内死 3 次 -> 下线 5 分钟，这是唯一的一票否决，安全闸优先于任何计划。
"""
from __future__ import annotations

import contextlib
import inspect
import json
import copy
import math
from dataclasses import dataclass, fields
from functools import wraps
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Literal, Optional

import requests

from .client import ApiError, FATAL, REPLAN, RETRY, Client


OXYGEN_FULL = 20  # API §3.1, Minecraft 1.21


class Replan(Exception):
    """前置条件不成立，当前任务作废，换一个目标。"""


EnvironmentWake = Literal["chunk_moved", "day", "night", "inventory_changed"]


class EnvBlocked(Replan):
    """已有环境观察证据，挂起当前技能，等待对应条件变化。"""

    def __init__(
        self, message: str, *, missing_predicate: str, searched_volume: str, wake_on: EnvironmentWake,
    ) -> None:
        super().__init__(message)
        if wake_on not in ("chunk_moved", "day", "night", "inventory_changed"):
            raise ValueError(f"未知环境唤醒条件: {wake_on}")
        self.missing_predicate = missing_predicate
        self.searched_volume = searched_volume
        self.wake_on = wake_on

    def evidence(self) -> dict[str, str]:
        return {
            "message": str(self),
            "missing_predicate": self.missing_predicate,
            "searched_volume": self.searched_volume,
            "wake_on": self.wake_on,
        }


class Fatal(Exception):
    """认证/接口级错误，别继续硬跑。"""


@dataclass(frozen=True)
class RuntimeConfig:
    # 临时工程值，必须用官方 C1/寻路证据标定；不代表已验证的生存阈值。
    poll_safe_s: float = 0.8
    poll_risk_s: float = 0.2
    observer_min_interval_s: float = 0.05
    status_ttl_s: float = 0.2
    stop_cooldown_s: float = 10.0
    hp_crit: float = 8.0
    hp_risk: float = 14.0
    oxygen_warn: float = 6.0
    hostile_risk_dist: float = 16.0
    hostile_flee_dist: float = 6.0
    creeper_urgent_dist: float = 5.0
    food_warn: float = 6.0
    food_crit: float = 2.0
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
        # 兼容已有 .env 中按旧网关 300 刻度配置的阈值（旧默认值为 80）。
        if OXYGEN_FULL < values["oxygen_warn"] <= 300:
            values["oxygen_warn"] /= 15
        if values["oxygen_warn"] > OXYGEN_FULL:
            raise ValueError("MC_OXYGEN_WARN 必须使用 1.21 的 20 格氧气刻度")
        if values["poll_risk_s"] > values["poll_safe_s"] or values["hp_crit"] > values["hp_risk"]:
            raise ValueError("风险轮询不能比安全轮询慢，危急血量不能高于预警血量")
        if values["food_crit"] > values["food_warn"]:
            raise ValueError("危急饱食度不能高于预警饱食度")
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
        self.active_escape_intent: Optional[str] = None
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
        self._totem_guard: Optional[dict[str, Any]] = None
        self.last_hurt_at: float = 0.0
        self.last_hurt_info: dict[str, Any] = {}

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
    def patch_action(self, result: dict, *, action: str = "") -> None:
        # 动作返回只更新已知字段，不能把整份旧 status 冒充为新鲜状态。
        self._status_generation += 1
        for key in ("pos", "health", "food"):
            # 规范 §4.3：place.pos 是方块坐标；§4.12 的 dismount.pos 才是玩家坐标。
            if key == "pos" and action == "place":
                continue
            if key in result:
                self.st = {**self.st, key: copy.deepcopy(result[key])}
                if key == "pos":
                    # 速度属于上一次 /status 的物理 tick，与新动作返回的坐标不能混用。
                    self.st.pop("speed", None)
                    self.st.pop("velocity", None)

    def refresh_status(self, client: Client, max_age_s: Optional[float] = None) -> dict:
        ttl = self.config.status_ttl_s if max_age_s is None else max_age_s
        # 锁内再次判断，合并主循环与 monitor 的并发补读。
        with self._status_fetch:
            with self._lock:
                # 非正 TTL 显式强制补读，即使粗时钟下两次读数完全相同。
                if ttl > 0 and self.st and time.monotonic() - self.status_at <= ttl:
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

    @_locked
    def set_active_escape(self, intent: Optional[str]) -> None:
        self.active_escape_intent = intent

    @_locked
    def get_active_escape(self) -> Optional[str]:
        return self.active_escape_intent

    @_locked
    def clear_recent_hurt(self) -> None:
        self.last_hurt_at = 0.0
        self.last_hurt_info = {}

    @_locked
    def get_recent_hurt(self, max_age_s: float = 4.0) -> Optional[dict[str, Any]]:
        if self.last_hurt_at <= 0.0 or not self.last_hurt_info:
            return None
        if (time.monotonic() - self.last_hurt_at) > max_age_s:
            return None
        return copy.deepcopy(self.last_hurt_info)

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
                    self.last_hurt_at = 0.0
                    self.last_hurt_info = {}
                    self.safety_intent = None
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
                elif etype == "spawn":
                    self.last_hurt_at = 0.0
                    self.last_hurt_info = {}
                    self.safety_intent = None
                elif etype == "hurt" and was_booted:
                    self.last_hurt_at = now_mono
                    self.last_hurt_info = {
                        "source": ev.get("source"),
                        "source_entity": copy.deepcopy(ev.get("source_entity")),
                        "likely_attacker": copy.deepcopy(ev.get("likely_attacker")),
                        "damage": ev.get("damage"),
                        "health": ev.get("health"),
                    }
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

    @_locked
    def last_hurt_source(self) -> Optional[str]:
        for event in reversed(self.events):
            if not isinstance(event, dict):
                continue
            if event.get("type") in ("spawn", "death"):
                break
            if event.get("type") == "hurt":
                source = event.get("source")
                return source if isinstance(source, str) and source else None
        return None

    def summary(self) -> str:
        st = self.st
        return (
            f"pos={st.get('pos')} hp={st.get('health')} food={st.get('food')} "
            f"dim={st.get('dimension')} held={(st.get('held') or {}).get('name')} "
            f"nearest_hostile={st.get('nearest_hostile')}"
        )

    @_locked
    def is_totem_trigger_armed(self) -> bool:
        """返回受控图腾触发守卫是否处于已布防、未失效且未超时状态。"""
        guard = self._totem_guard
        if not (isinstance(guard, dict) and guard.get("active") and not guard.get("consumed")):
            return False
        expires_at = guard.get("expires_at")
        if isinstance(expires_at, (int, float)) and time.monotonic() > expires_at:
            return False
        return True

    @_locked
    def consume_totem_trigger_suppression(self) -> bool:
        """锁内二次核验条件并在受控坠落窗口内支持多拍抑制（直到落地/消耗/超时/撤防）。"""
        guard = self._totem_guard
        if not (isinstance(guard, dict) and guard.get("active") and not guard.get("consumed")):
            return False
        expires_at = guard.get("expires_at")
        if isinstance(expires_at, (int, float)) and time.monotonic() > expires_at:
            guard["active"] = False
            return False
        st = self.st or {}
        inv = self.inventory or {}
        if self.in_safe_mode or st.get("in_lava") or st.get("submerged") or not st.get("alive", True):
            return False
        if st.get("on_ground") is False:
            guard["saw_airborne"] = True
        if guard.get("saw_airborne") and st.get("on_ground") is True:
            guard["consumed"] = True
            guard["active"] = False
            return False
        if not _has_offhand_totem(inv, st):
            guard["consumed"] = True
            guard["active"] = False
            return False
        if guard.get("allow_armored"):
            armor_ok, _, _, _, _ = assess_totem_armor(inv, st)
            if not armor_ok:
                return False
        elif not _is_armor_stripped(inv, st):
            return False
        guard["suppression_count"] = int(guard.get("suppression_count", 0)) + 1
        return True

    def controlled_totem_trigger(
        self,
        client: Any,
        *,
        drop_height: int = 25,
        landing_col: Any = None,
        min_drop: int = 23,
        max_duration_s: float = 3.5,
        allow_armored: bool = False,
    ):
        """受控图腾触发（Postmortal）坠落全过程安全抑制上下文管理器。"""
        return controlled_totem_trigger(
            self,
            client,
            drop_height=drop_height,
            landing_col=landing_col,
            min_drop=min_drop,
            max_duration_s=max_duration_s,
            allow_armored=allow_armored,
        )


# ---------------------------------------------------------------- 受控图腾触发守卫
_TOTEM_UNSAFE_LANDING_BLOCKS: frozenset[str] = frozenset({
    "lava", "flowing_lava", "water", "flowing_water", "bubble_column",
    "fire", "soul_fire", "powder_snow", "magma_block", "cobweb",
    "slime_block", "hay_block", "sweet_berry_bush", "honey_block",
})
_TOTEM_PASSABLE_BLOCKS: frozenset[str] = frozenset({
    "short_grass", "tall_grass", "fern", "large_fern",
    "short_dry_grass", "tall_dry_grass", "dead_bush",
    "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
    "oxeye_daisy", "cornflower", "lily_of_the_valley",
})
_TOTEM_AIR_BLOCKS: frozenset[str] = frozenset({"air", "cave_air", "void_air"})
_TOTEM_ARMOR_SLOTS: tuple[str, ...] = ("head", "chest", "legs", "feet")


def _is_unsafe_totem_landing_block(bname: str) -> bool:
    return bname in _TOTEM_UNSAFE_LANDING_BLOCKS or bname == "bed" or bname.endswith("_bed")


def _is_totem_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    name = str(item.get("name", "")).removeprefix("minecraft:")
    if name != "totem_of_undying":
        return False
    raw_count = item.get("count", 1)
    if raw_count is None or isinstance(raw_count, bool):
        return False
    try:
        return int(raw_count) >= 1
    except (TypeError, ValueError):
        return False


def _has_offhand_totem(inv: Any, st: Any) -> bool:
    inv_off = inv.get("offhand") if isinstance(inv, dict) else None
    st_off = st.get("offhand") if isinstance(st, dict) else None
    if isinstance(inv_off, dict) and inv_off and not _is_totem_item(inv_off):
        return False
    if isinstance(st_off, dict) and st_off and not _is_totem_item(st_off):
        return False
    return _is_totem_item(inv_off) or _is_totem_item(st_off)


def _is_armor_stripped(inv: Any, st: Any) -> bool:
    armor = inv.get("armor") if isinstance(inv, dict) else None
    if isinstance(armor, dict):
        for slot in (*_TOTEM_ARMOR_SLOTS, "torso"):
            piece = armor.get(slot)
            if isinstance(piece, dict):
                if piece and (piece.get("name") or int(piece.get("count", 0) or 0) > 0):
                    return False
            elif piece:
                return False
    elif armor:
        return False
    raw_ap = st.get("armor_points", 0) if isinstance(st, dict) else 0
    if isinstance(raw_ap, bool):
        return False
    try:
        return float(raw_ap or 0) == 0.0
    except (TypeError, ValueError):
        return False


def _iter_piece_enchants(piece: dict[str, Any]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    sources: list[Any] = [piece]
    for key in ("components", "nbt", "metadata", "meta"):
        sub = piece.get(key)
        if isinstance(sub, dict):
            sources.append(sub)
    for src in sources:
        ench = src.get("enchantments")
        if isinstance(ench, dict):
            for k, v in ench.items():
                if isinstance(v, int) and not isinstance(v, bool):
                    out.append((str(k).removeprefix("minecraft:").strip().lower(), v))
        elif isinstance(ench, (list, tuple)):
            for row in ench:
                if not isinstance(row, dict):
                    continue
                ename = str(row.get("name") or row.get("id") or "").removeprefix("minecraft:").strip().lower()
                lvl = row.get("level") if row.get("level") is not None else row.get("lvl", 1)
                if ename and isinstance(lvl, int) and not isinstance(lvl, bool):
                    out.append((ename, lvl))
    return out


def assess_totem_armor(inv: Any, st: Any) -> tuple[bool, str, int, int, bool]:
    """评估当前护甲附魔与减伤情况：返回 (可否执行, 拒绝原因, 所需坠落高度, min_drop, 是否带甲)。"""
    armor = inv.get("armor") if isinstance(inv, dict) else None
    occupied = 0
    prot_epf = 0
    seen_slots: set[str] = set()
    if isinstance(armor, dict):
        for slot in (*_TOTEM_ARMOR_SLOTS, "torso"):
            norm_slot = "chest" if slot == "torso" else slot
            if norm_slot in seen_slots:
                continue
            piece = armor.get(slot)
            if not isinstance(piece, dict) or not piece:
                continue
            if not (piece.get("name") or int(piece.get("count", 0) or 0) > 0):
                continue
            seen_slots.add(norm_slot)
            occupied += 1
            for ename, lvl in _iter_piece_enchants(piece):
                if ename == "feather_falling" and lvl >= 1:
                    return (
                        False,
                        "totem_feather_falling_armor_cannot_unequip: 穿戴摔落保护靴子且无脱甲接口，禁止受控坠落",
                        0,
                        0,
                        False,
                    )
                if ename == "protection" and lvl >= 1:
                    prot_epf += lvl

    raw_ap = st.get("armor_points", 0) if isinstance(st, dict) else 0
    if isinstance(raw_ap, bool):
        return False, "护甲值 armor_points 格式无效，禁止受控坠落", 0, 0, False
    try:
        armor_points = float(raw_ap or 0)
    except (TypeError, ValueError):
        return False, "护甲值 armor_points 解析失败，禁止受控坠落", 0, 0, False

    if occupied == 0 and armor_points == 0.0:
        return True, "ok", 25, 23, False

    if prot_epf >= 12:
        return (
            False,
            f"totem_high_protection_armor_cannot_unequip: 护甲保护总等级过高(prot_epf={prot_epf})且无脱甲接口，禁止受控坠落",
            0,
            0,
            False,
        )

    reduction = min(0.80, 0.04 * prot_epf)
    needed_height = math.ceil(3.0 + 24.0 / (1.0 - reduction))
    needed_height = max(needed_height, 28) if prot_epf == 0 else max(needed_height + 2, 30)
    return True, "ok_armored", needed_height, needed_height - 2, True


def validate_controlled_totem_conditions(
    state: State,
    client: Any,
    *,
    drop_height: int = 25,
    landing_col: Any = None,
    min_drop: int = 23,
    allow_armored: bool = False,
) -> tuple[bool, str, Optional[dict[str, Any]]]:
    """严格校验受控图腾触发（Postmortal）的全部安全前置条件与落点列连续净空。"""
    if isinstance(drop_height, bool) or not isinstance(drop_height, int) or drop_height < 1:
        return False, "drop_height 必须为正整数", None
    if isinstance(min_drop, bool) or not isinstance(min_drop, int) or min_drop < 1:
        return False, "min_drop 必须为正整数", None

    with state._lock:
        st = state.snapshot()
        inv = copy.deepcopy(state.inventory)
        inv_dirty = bool(state.inventory_dirty)
        in_safe_mode = state.in_safe_mode

    # (a) 存活 & 就绪 & 不在 safe_mode & 不在岩浆/水/溺水/着火中
    if not isinstance(st, dict) or not st:
        return False, "缺少 /status 状态快照，无法校验受控图腾触发条件", None
    if not st.get("alive", True):
        return False, "角色当前未存活，禁止受控图腾触发", None
    if not st.get("ready"):
        return False, "角色当前未就绪(ready=False)，禁止受控图腾触发", None
    if in_safe_mode:
        return False, "当前处于死亡循环避险冷却(safe_mode)，禁止受控图腾触发", None
    if st.get("in_lava"):
        return False, "角色当前处于岩浆中，禁止受控图腾触发", None
    if st.get("in_water") or st.get("submerged"):
        return False, "角色当前处于水中/溺水状态，禁止受控图腾触发", None
    if st.get("on_fire"):
        return False, "角色当前处于着火状态，禁止受控图腾触发", None

    # (b) 背包快照新鲜且副手确认持有 totem_of_undying
    if inv_dirty:
        return False, "背包快照未刷新(inventory_dirty=True)，无法确认副手图腾与护甲状态", None
    if not _has_offhand_totem(inv, st):
        return False, "副手未确认持有不死图腾(totem_of_undying)", None

    # (c) 无护甲或满足无摔落保护/低减伤且抬高落差要求
    if allow_armored:
        armor_ok, armor_reason, req_height, req_min_drop, _ = assess_totem_armor(inv, st)
        if not armor_ok:
            return False, armor_reason, None
        if drop_height < req_height or min_drop < req_min_drop:
            return False, f"穿戴护甲时坠落高度不足（需 drop_height>={req_height}, min_drop>={req_min_drop}）", None
    elif not _is_armor_stripped(inv, st):
        return False, "角色仍穿戴护甲或 armor_points 不为 0，必须先脱甲或按护甲减伤抬高落差再触发图腾", None

    # (d) 落点列向下连续至少 min_drop 格空气/非致命阻挡后落到安全实心方块
    pos = st.get("pos")
    if (
        not isinstance(pos, (list, tuple))
        or len(pos) < 3
        or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in pos[:3]
        )
    ):
        return False, "缺少有效玩家坐标 pos，无法校验安全落点", None

    get_block = getattr(client, "get_block", None)
    if not callable(get_block):
        return False, "缺少 client.get_block，无法校验安全落点", None

    fx, fy, fz = math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2])
    if landing_col is not None:
        if (
            not isinstance(landing_col, (list, tuple))
            or len(landing_col) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in landing_col)
        ):
            return False, "landing_col 必须为有效的 (x, z) 坐标", None
        lx, lz = math.floor(landing_col[0]), math.floor(landing_col[1])
    else:
        lx, lz = fx, fz

    supports_use_cache = _accepts_use_cache(get_block)

    def _read_bname(probe: list[int]) -> str:
        blk = get_block(probe, use_cache=False) if supports_use_cache else get_block(probe)
        raw = blk.get("name") if isinstance(blk, dict) else None
        return str(raw).removeprefix("minecraft:") if isinstance(raw, str) else ""

    if (lx, lz) != (fx, fz):
        for head_dy in (0, 1):
            try:
                head_name = _read_bname([lx, fy + head_dy, lz])
            except ApiError as e:
                return False, f"查询落点列起步空间失败 ({e.code}): {e.message}", None
            if head_name and head_name not in _TOTEM_AIR_BLOCKS and head_name not in _TOTEM_PASSABLE_BLOCKS:
                return False, f"落点列 ({lx}, {lz}) 起步空间受阻 ({head_name})", None

    max_probe = max(drop_height, min_drop) + 2
    for dy in range(1, max_probe + 1):
        probe_pos = [lx, fy - dy, lz]
        try:
            bname = _read_bname(probe_pos)
        except ApiError as e:
            return False, f"查询落点列方块失败 ({e.code}): {e.message}", None
        if not bname or bname in _TOTEM_AIR_BLOCKS or bname in _TOTEM_PASSABLE_BLOCKS:
            continue
        if _is_unsafe_totem_landing_block(bname):
            return False, f"落点列 ({lx}, {lz}) 存在岩浆/水/缓冲/床等危险方块 ({bname})", None
        drop_blocks = dy - 1
        if drop_blocks < min_drop:
            return False, f"落点列 ({lx}, {lz}) 坠落高度不足（仅 {drop_blocks} 格 < {min_drop} 格即遇 {bname}）", None
        return True, "ok", {
            "landing_pos": probe_pos,
            "landing_block": bname,
            "drop_blocks": drop_blocks,
            "landing_col": [lx, lz],
        }

    return False, f"落点列 ({lx}, {lz}) 下方 {max(drop_height, min_drop)} 格内无实心安全落点（疑似虚空）", None


@contextlib.contextmanager
def controlled_totem_trigger(
    state: State,
    client: Any,
    *,
    drop_height: int = 25,
    landing_col: Any = None,
    min_drop: int = 23,
    max_duration_s: float = 3.5,
    allow_armored: bool = False,
):
    """受控图腾触发守卫：严格核验前置条件后在持续窗口内布防，退出必恢复。"""
    if isinstance(max_duration_s, bool) or not isinstance(max_duration_s, (int, float)) or not math.isfinite(max_duration_s) or max_duration_s <= 0:
        raise Replan("受控图腾触发守卫拒绝放行: max_duration_s 必须为正有限数")
    ok, reason, meta = validate_controlled_totem_conditions(
        state,
        client,
        drop_height=drop_height,
        landing_col=landing_col,
        min_drop=min_drop,
        allow_armored=allow_armored,
    )
    if not ok:
        raise Replan(f"受控图腾触发守卫拒绝放行: {reason}")
    now = time.monotonic()
    guard: dict[str, Any] = {
        "active": True,
        "consumed": False,
        "allow_armored": bool(allow_armored),
        "saw_airborne": False,
        "suppression_count": 0,
        "max_duration_s": float(max_duration_s),
        **(meta or {}),
        "armed_at": now,
        "expires_at": now + float(max_duration_s),
    }
    with state._lock:
        state._totem_guard = guard
    try:
        yield meta
    finally:
        with state._lock:
            state._totem_guard = None


# ---------------------------------------------------------------- 安全闸
_EDIBLE_FOODS: frozenset[str] = frozenset({
    "cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_chicken", "cooked_cod",
    "cooked_salmon", "cooked_rabbit", "bread", "baked_potato", "golden_carrot",
    "golden_apple", "enchanted_golden_apple", "pumpkin_pie", "apple", "carrot",
    "melon_slice", "sweet_berries", "glow_berries", "beef", "porkchop", "mutton",
    "chicken", "rabbit", "potato", "beetroot", "dried_kelp", "cookie",
    "mushroom_stew", "beetroot_soup", "rabbit_stew",
})


def _has_edible_food(s: State) -> bool:
    return any(s.count(food) >= 1 for food in _EDIBLE_FOODS)


_ARMOR_SLOTS: frozenset[str] = frozenset({
    "head", "chest", "torso", "legs", "feet",
    "helmet", "chestplate", "leggings", "boots",
})


def _is_equipped_armor_piece(piece: Any) -> bool:
    """判断单个槽位条目是否为有效已穿戴护甲件。"""
    if isinstance(piece, str):
        name = piece.removeprefix("minecraft:").strip().lower()
        return bool(name) and name not in ("air", "none")
    if isinstance(piece, dict):
        name = str(piece.get("name") or "").removeprefix("minecraft:").strip().lower()
        if not name or name in ("air", "none"):
            return False
        raw_count = piece.get("count", 1)
        if raw_count is None:
            return True
        if isinstance(raw_count, bool) or not isinstance(raw_count, (int, float)) or not math.isfinite(raw_count):
            return False
        return float(raw_count) > 0
    return False


def _has_armor_in_container(container: Any, *, filter_slots: bool = False, allow_numeric: bool = False) -> bool:
    if allow_numeric and isinstance(container, (int, float)) and not isinstance(container, bool):
        return math.isfinite(container) and float(container) > 0
    if isinstance(container, dict):
        for slot, piece in container.items():
            if filter_slots and str(slot).strip().lower() not in _ARMOR_SLOTS:
                continue
            if _is_equipped_armor_piece(piece):
                return True
        return False
    if isinstance(container, (list, tuple)) and not filter_slots:
        return any(_is_equipped_armor_piece(piece) for piece in container)
    return False


def has_equipped_armor(state: State) -> bool:
    """判断角色当前是否穿戴了任意部位护甲（背包未穿戴不计入）。"""
    st = state.snapshot() if hasattr(state, "snapshot") else (getattr(state, "st", None) or {})
    if not isinstance(st, dict):
        st = {}
    inv = getattr(state, "inventory", None) or {}
    if not isinstance(inv, dict):
        inv = {}

    armor_points = st.get("armor_points")
    if (
        isinstance(armor_points, (int, float))
        and not isinstance(armor_points, bool)
        and math.isfinite(armor_points)
        and armor_points > 0
    ):
        return True
    if _has_armor_in_container(st.get("armor"), allow_numeric=True):
        return True
    if _has_armor_in_container(st.get("equipment"), filter_slots=True):
        return True
    if _has_armor_in_container(inv.get("armor"), allow_numeric=False):
        return True
    if _has_armor_in_container(inv.get("equipment"), filter_slots=True):
        return True
    return False


def effective_hostile_flee_dist(state: State, hostile_name: str = "") -> float:
    """无甲或低血量时扩大非苦力怕敌怪预警逃跑半径（骷髅 10m、其余 9m）。"""
    cfg = getattr(state, "config", None)
    raw_base = getattr(cfg, "hostile_flee_dist", 6.0)
    base = (
        float(raw_base)
        if isinstance(raw_base, (int, float)) and not isinstance(raw_base, bool) and math.isfinite(raw_base) and raw_base > 0
        else 6.0
    )
    raw_hp_risk = getattr(cfg, "hp_risk", 14.0)
    hp_risk = (
        float(raw_hp_risk)
        if isinstance(raw_hp_risk, (int, float)) and not isinstance(raw_hp_risk, bool) and math.isfinite(raw_hp_risk) and raw_hp_risk > 0
        else 14.0
    )

    st = state.snapshot() if hasattr(state, "snapshot") else (getattr(state, "st", None) or {})
    if not isinstance(st, dict):
        st = {}
    raw_hp = st.get("health", 20)
    if isinstance(raw_hp, (int, float)) and not isinstance(raw_hp, bool) and math.isfinite(raw_hp):
        hp = float(raw_hp)
    else:
        hp = 20.0

    if (not has_equipped_armor(state)) or hp < hp_risk:
        norm = str(hostile_name or "").removeprefix("minecraft:").strip().lower()
        if norm == "skeleton":
            return max(base, 10.0)
        return max(base, 9.0)
    return base


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

        # 致命物理险情（岩浆/缺氧/失重坠落/近身苦力怕）必须排在 safe_mode 之前，避免 65s 避险锁存遮蔽当前逃生意图
        if st.get("in_lava"):
            add("escape_lava", "在岩浆里")
        oxygen = st.get("oxygen")
        if st.get("submerged") and oxygen is not None and float(oxygen) < self.config.oxygen_warn:
            add("surface", f"氧气 {oxygen}")
        support = s.get_support()
        if (
            support.get("risk")
            and time.monotonic() - support.get("at", 0) <= self.config.support_ttl_s
            and support.get("status_pos") == st.get("pos")
        ):
            add("void_risk", support["risk"])
        raw_hostile = st.get("nearest_hostile")
        hostile = raw_hostile if isinstance(raw_hostile, dict) else {}
        hostile_name = (
            str(hostile.get("name", "")).removeprefix("minecraft:").strip().lower()
            if hostile
            else ""
        )
        raw_dist = hostile.get("dist", 99.0) if hostile else 99.0
        if isinstance(raw_dist, (int, float)) and not isinstance(raw_dist, bool) and math.isfinite(raw_dist):
            hostile_dist = float(raw_dist)
        else:
            hostile_dist = 99.0
        creeper_urgent = hostile_name == "creeper" and hostile_dist < self.config.creeper_urgent_dist
        if creeper_urgent:
            add(
                "flee",
                f"近身苦力怕 距离 {hostile.get('dist')}",
                "urgent",
                hostile="creeper",
                dist=hostile.get("dist"),
                creeper=True,
            )
        close_hostile_threshold = max(6.0, float(self.config.hostile_flee_dist))
        close_non_creeper = (not creeper_urgent) and bool(hostile) and hostile_dist < close_hostile_threshold
        emitted_flee = bool(creeper_urgent)
        if s.in_safe_mode:
            suppress_safe_mode_urgent = bool(s.get_active_escape()) or close_non_creeper
            if close_non_creeper:
                add(
                    "flee",
                    f"{hostile.get('name')} 距离 {hostile.get('dist')}",
                    "soft",
                    hostile=hostile.get("name"),
                    dist=hostile.get("dist"),
                )
                emitted_flee = True
            add(
                "safe_mode",
                f"接近死亡循环，锁存避险剩 {s.safe_mode_remaining:.0f}s",
                "soft" if suppress_safe_mode_urgent else "urgent",
                remaining_s=round(s.safe_mode_remaining, 1),
            )
        hp = float(st.get("health", 20))
        if hp <= self.config.hp_crit:
            add("heal", f"血量 {hp}", "soft")
        raw_food = st.get("food")
        if isinstance(raw_food, (int, float)) and not isinstance(raw_food, bool):
            food = float(raw_food)
            if food <= self.config.food_warn:
                can_eat = _has_edible_food(s)
                sev = "urgent" if (food <= self.config.food_crit and can_eat) else "soft"
                add("eat", f"低饱食度 {food:g}", sev, food=food, has_food=can_eat)
        flee_threshold = effective_hostile_flee_dist(s, hostile_name)
        if not emitted_flee and hostile and hostile_dist < flee_threshold:
            add(
                "flee",
                f"{hostile.get('name')} 距离 {hostile.get('dist')}",
                "soft",
                hostile=hostile.get("name"),
                dist=hostile.get("dist"),
            )
            emitted_flee = True
        if not emitted_flee:
            recent_hurt = s.get_recent_hurt(max_age_s=4.0) if hasattr(s, "get_recent_hurt") else None
            if isinstance(recent_hurt, dict):
                hurt_src = str(recent_hurt.get("source") or "").strip().lower()
                src_ent = recent_hurt.get("source_entity") if isinstance(recent_hurt.get("source_entity"), dict) else {}
                att_ent = recent_hurt.get("likely_attacker") if isinstance(recent_hurt.get("likely_attacker"), dict) else {}
                src_name = str(src_ent.get("name") or att_ent.get("name") or "").removeprefix("minecraft:").strip().lower()
                is_arrow_or_ranged = hurt_src in ("arrow", "trident") or src_name in (
                    "skeleton", "stray", "bogged", "pillager", "parched", "witch", "blaze", "ghast", "drowned",
                )
                is_mob_attack = hurt_src in ("mob_attack", "mob_projectile", "indirect_magic") or bool(src_name)
                if (is_arrow_or_ranged or is_mob_attack) and (not has_equipped_armor(s) or hp < self.config.hp_risk):
                    sev = "urgent" if (hp <= self.config.hp_crit and not s.get_active_escape()) else "soft"
                    add(
                        "flee",
                        f"受击应急脱险 {src_name or hurt_src or 'hostile'}",
                        sev,
                        hostile=src_name or hostile_name or "hostile",
                        dist=hostile_dist if hostile else 16.0,
                        hurt_triggered=True,
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
# 超时或网络中断后需主动发 /stop 清理服务端持续寻路/按键/挖掘/追击的动作
STOP_ON_TIMEOUT_ACTIONS: frozenset[str] = frozenset({
    "move_to", "control", "dig", "attack", "use_item",
})
# 单次尝试耗时已达到该阈值再触发 timeout 时不再无谓原地重试，直接清理并 Replan
_NO_RETRY_TIMEOUT_ELAPSED_S = 20.0


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
        busy_interval_s: float = 0.1,
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
                attempt_started = time.monotonic()
                try:
                    result = fn()
                    self.n_actions += 1
                    self._record({
                        "t": round(t0, 3), "name": name, "ms": round((time.time() - t0) * 1000),
                        "ok": True, "attempt": attempt, "result": _shrink(result),
                    })
                    if isinstance(result, dict) and any(k in result for k in ("pos", "health", "food")):
                        self.state.patch_action(result, action=name)
                    return result
                except ApiError as e:
                    if (
                        name == "craft"
                        and e.code == "bad_args"
                        and any(tok in e.message.lower() for tok in ("updateslot", "did not fire within timeout"))
                    ):
                        e = ApiError("window_timeout", e.message, e.extra)
                    attempt_elapsed = time.monotonic() - attempt_started
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

                    # 超时 / 网络断流：先止动 /stop，再核对非幂等动作是否已在服务端生效。
                    # 规范 §5：`timeout` =「寻路失败 / 超时」，服务端可能已执行完副作用，
                    # 故非幂等动作的 timeout 与 network 走同一判重路径（只有确认背包未变才重发）；
                    # use_item 同时在 STOP_ON_TIMEOUT_ACTIONS 中，判重路径内部仍会先 /stop。
                    ambiguous = e.code == "network" or (
                        e.code == "timeout" and name in NON_IDEMPOTENT_ACTIONS
                    )
                    if not ambiguous and e.code == "timeout" and name in STOP_ON_TIMEOUT_ACTIONS:
                        self.stop()
                    elif ambiguous:
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

                    retryable = e.kind == RETRY and not (
                        e.code == "timeout" and attempt_elapsed >= _NO_RETRY_TIMEOUT_ELAPSED_S
                    ) and not (e.code == "window_timeout" and name in NON_IDEMPOTENT_ACTIONS)
                    if retryable and attempt <= retries:
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
            if name in ("dig", "place", "interact", "use_item"):
                inv_cache = getattr(self.client, "invalidate_block_cache", None)
                if callable(inv_cache):
                    inv_cache()
            with self._cond:
                self.active_action = None
                self._busy = False
                self._owner_tid = None
                self._cond.notify_all()

    # ---- 常用包装（顺便统一超时/参数与非幂等判重） --------------------
    def move_to(
        self,
        x=None,
        z=None,
        y=None,
        entity_id=None,
        rng=1,
        pos=None,
        timeout=None,
        *,
        retries=2,
        stop_on=None,
    ) -> dict:
        if retries is None:
            retries = 2

        def _call() -> dict:
            if timeout is None:
                return self.client.move_to(
                    x, z, y=y, entity_id=entity_id, rng=rng, pos=pos, stop_on=stop_on
                )
            try:
                return self.client.move_to(
                    x, z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on
                )
            except ApiError as err:
                if err.code == "network" and isinstance(err.__cause__, requests.ReadTimeout):
                    raise ApiError("timeout", err.message, err.extra) from err
                raise

        return self.act("move_to", _call, retries=retries)

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

    def attack(self, entity_id, timeout_ms=20000, *, max_hits=None, interval_ms=None, stop_on=None) -> dict:
        # 限次攻击是「砍几下就走」，断流重发会多打，故不重试
        return self.act(
            "attack",
            lambda: self.client.attack(entity_id, timeout_ms, max_hits=max_hits, interval_ms=interval_ms,
                                       stop_on=stop_on),
            retries=0 if max_hits is not None else 2,
        )

    def control(self, **keys) -> dict:
        """§4.16 底层按键。断流后走过的距离和出手次数都不可知，不重试，交上层重新观察。

        当前部署没有 /control 时抛 Replan（unsupported_api），不会 Fatal。
        """
        try:
            return self.act("control", lambda: self.client.control(**keys), retries=0)
        finally:
            if keys.get("hit"):
                self.state.invalidate_inventory()  # 挥击会掉武器耐久

    def look(self, yaw=None, pitch=None, *, pos=None, entity_id=None) -> dict:
        """§4.17 转向，不占动作锁。不支持时抛 ApiError(unsupported_api)，kind 为 REPLAN。"""
        return self._act_without_lock(
            "look", lambda: self.client.look(yaw=yaw, pitch=pitch, pos=pos, entity_id=entity_id),
        )

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

    def posture(self, sneak=None, shield=None, *, jump=None) -> dict:
        # 规范 §4.11：/posture 不占动作锁，可在动作阻塞中途调用
        jump_opts = {} if jump is None else {"jump": jump}
        return self._act_without_lock(
            "posture", lambda: self.client.posture(sneak=sneak, shield=shield, **jump_opts),
        )

    def _act_without_lock(self, name: str, call: Callable[[], dict]) -> dict:
        t0 = time.time()
        res = call()
        self._record({
            "t": round(t0, 3), "name": name, "ms": round((time.time() - t0) * 1000),
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
            res = self.client.stop()
            if isinstance(res, dict) and res.get("released") is False:
                raise ApiError("busy", "/stop 等待后动作仍未退出", res)
        except Exception as e:
            self.log(f"  [stop] 失败: {e}")
            if raise_on_error:
                raise


# ---------------------------------------------------------------- 后台监控与抢占闭环
def _accepts_use_cache(fn: Any) -> bool:
    """检查 fn 是否接受 use_cache 关键字参数。"""
    side_effect = getattr(fn, "side_effect", None)
    target = side_effect if callable(side_effect) and not isinstance(side_effect, type) else fn
    try:
        sig = inspect.signature(target)
    except (TypeError, ValueError):
        return False
    param = sig.parameters.get("use_cache")
    if param is not None and param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    ):
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


def _same_escape_kind(active_escape: Optional[str], hazard_kind: str) -> bool:
    """避免后台 SafetyMonitor 重复 /stop 打断正在执行的同类紧急脱险（如 flee 被 flee 抢占）。"""
    if not active_escape or not active_escape.strip():
        return False
    a = active_escape.strip().lower()
    h = hazard_kind.strip().lower()
    if h == "safe_mode":
        return True
    if a == h:
        return True
    douse_group = {"douse", "escape_lava"}
    surface_group = {"surface", "escape_water"}
    flee_group = {"flee", "void_risk"}
    return (
        (a in douse_group and h in douse_group)
        or (a in surface_group and h in surface_group)
        or (a in flee_group and h in flee_group)
    )


_VOID_NON_SOLID: frozenset[str] = frozenset({
    "air", "cave_air", "void_air", "lava", "flowing_lava",
})


class SafetyMonitor:
    """唯一定时 status/events 读取者；持续危险一次抢占，软危险只发布意图。"""

    def __init__(self, client: Client, state: State, executor: Executor,
                 safety: Optional[Safety] = None,
                 on_hazard: Optional[Callable[[dict, Executor, State], None]] = None,
                 interval_s: Optional[float] = None,
                 continuous: bool = False,
                 refresh_inventory: bool = False):
        self.client, self.state, self.executor = client, state, executor
        self.config = state.config
        self.safety = safety or Safety(self.config)
        self.on_hazard = on_hazard
        self.interval_s = interval_s
        self.continuous = continuous
        self.refresh_inventory = refresh_inventory
        self._get_block_key: Optional[tuple] = None
        self._get_block_supports_use_cache: Optional[bool] = None
        self._active: set[str] = set()
        self._triggered: dict[str, float] = {}
        self._last_y: Optional[float] = None
        self.session_error: Optional[str] = None
        self._last_err_msg: str = ""
        self._err_repeat: int = 0
        self._stop_ev = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _is_risk_state(self) -> bool:
        """Return whether cached telemetry or risk context indicates elevated hazard."""
        st, cfg = self.state.snapshot(), self.config
        rc = self.state.get_risk_context()
        hostile = st.get("nearest_hostile") or {}
        tod = float(st.get("time_of_day", 0)) % 24000
        raw_food = st.get("food")
        low_food = (
            isinstance(raw_food, (int, float))
            and not isinstance(raw_food, bool)
            and float(raw_food) <= cfg.food_warn
        )
        return bool(
            not st
            or cfg.night_start <= tod <= cfg.night_end
            or "nether" in st.get("dimension", "")
            or float(st.get("health", 20)) < cfg.hp_risk
            or low_food
            or float(hostile.get("dist", 999)) <= cfg.hostile_risk_dist
            or st.get("in_water")
            or st.get("submerged")
            or st.get("in_lava")
            or st.get("on_fire")
            or st.get("on_ground") is False
            or any(rc.values())
        )

    def _continuous_wait_s(self) -> float:
        """Yield tightly on risk/airborne states and back off safely on dry ground."""
        base = float(self.config.observer_min_interval_s)
        if self._is_risk_state():
            return base
        return min(float(self.config.poll_risk_s), base * 3.0)

    def next_interval(self) -> float:
        if self.interval_s is not None:
            return self.interval_s
        if self.continuous:
            return self._continuous_wait_s()
        return self.config.poll_risk_s if self._is_risk_state() else self.config.poll_safe_s

    def _get_block_uncached(self, pos: list[int]) -> dict:
        fn = self.client.get_block
        if hasattr(fn, "_mock_calls"):
            supports_use_cache = _accepts_use_cache(fn)
        else:
            key = (getattr(fn, "__func__", fn), getattr(fn, "__self__", None))
            if self._get_block_key != key or self._get_block_supports_use_cache is None:
                self._get_block_key = key
                self._get_block_supports_use_cache = _accepts_use_cache(fn)
            supports_use_cache = self._get_block_supports_use_cache
        return fn(pos, use_cache=False) if supports_use_cache else fn(pos)

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
        on_ground = st.get("on_ground")
        velocity = st.get("velocity")
        if isinstance(velocity, (list, tuple)) and len(velocity) == 3 and all(
            isinstance(component, (float, int)) and not isinstance(component, bool) and math.isfinite(component)
            for component in velocity
        ):
            # 落地所在 tick 仍可能有负的垂直位移。
            falling = on_ground is not True and velocity[1] < 0
            ascending = velocity[1] > 0
        else:
            # 旧版本与传送后的空值仍可用相邻位置样本判断。
            falling = prev_y is not None and prev_y - y >= self.config.void_fall_delta
            ascending = prev_y is not None and y - prev_y > 0.05
        self._last_y = y
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
            block = self._get_block_uncached(under)
            evidence["block"] = block
            name = str(block.get("name", "")).removeprefix("minecraft:")
            if guard:
                if name in ("air", "cave_air", "void_air"):
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
                            cb = self._get_block_uncached(cpos)
                            cname = str(cb.get("name", "")).removeprefix("minecraft:")
                            aabb_checked.append({"pos": cpos, "name": cname})
                            if cname and cname not in _VOID_NON_SOLID:
                                has_aabb_support = True
                                evidence["aabb_support"] = {"pos": cpos, "name": cname}
                                break
                        if aabb_checked:
                            evidence["aabb_checked"] = aabb_checked
                    if not has_aabb_support:
                        evidence["risk"] = "脚下空气：疑似无支撑/坠落，尚不能证明是虚空"
                elif not name:
                    evidence["risk"] = "高风险路径脚下方块未知"
            else:
                near_world_bottom = y <= -60.0
                if name == "void_air":
                    evidence["risk"] = "脚下虚空空气(void_air)：疑似坠入虚空"
                elif near_world_bottom and name in ("air", "cave_air", "") and not ascending:
                    evidence["risk"] = "接近世界底部(y<=-60)且脚下无实心支撑：疑似坠入虚空"
                elif name in ("air", "cave_air") and falling:
                    # 普通地形下落时再往下探 3 格，区分正常下 1~3 格零摔伤台阶/山坡与真悬空/深坑（>=4 格）
                    has_near_ground = False
                    below_checked = []
                    for dy in (1, 2, 3):
                        bpos = [under[0], under[1] - dy, under[2]]
                        b = self._get_block_uncached(bpos)
                        bname = str(b.get("name", "")).removeprefix("minecraft:")
                        below_checked.append({"pos": bpos, "name": bname})
                        if bname in ("lava", "flowing_lava"):
                            break
                        if bname and bname not in _VOID_NON_SOLID:
                            has_near_ground = True
                            break
                    evidence["below_checked"] = below_checked
                    if not has_near_ground:
                        evidence["risk"] = "脚下连续空气且正在下落：疑似坠落/深坑"
                elif not name and falling:
                    evidence["risk"] = "脚下方块未知且正在下落：疑似虚空边缘"
        except ApiError as e:
            evidence["error"] = e.code
            if guard:
                evidence["risk"] = "高风险路径支撑查询失败，不能按安全处理"
        # 在 get_block 返回后打时间戳，防官方慢 RTT 下刚返回即超过 support_ttl_s
        evidence["at"] = time.monotonic()
        self.state.set_support(evidence)

    def tick(self, *, force_status: Optional[bool] = None) -> Optional[dict]:
        force = self.continuous if force_status is None else force_status
        self.state.refresh_status(self.client, max_age_s=0 if force else None)
        # 先处理致命状态，再查支撑与事件，避免慢请求延误已观察到的岩浆/缺氧抢占。
        hazard = self._dispatch(reconcile=False)
        self._check_support()
        hazard = self._dispatch(reconcile=True) or hazard
        self.state.drain(self.client)
        after_events = self._dispatch(reconcile=False)
        if (self.refresh_inventory or self.continuous) and (self.state.inventory_dirty or not self.state.inventory):
            self.state.refresh_inventory(self.client)
        return after_events or hazard

    def _dispatch(self, reconcile: bool = True) -> Optional[dict]:
        hazards = self.safety.hazards(self.state)
        for hazard in hazards:
            if (
                hazard["kind"] == "heal"
                and self.executor.active_action in ("attack", "shoot")
                and not (getattr(self.state, "foraging_passive", False) and not _has_edible_food(self.state))
            ):
                hazard["severity"] = "urgent"
            elif (
                hazard["kind"] == "eat"
                and hazard.get("has_food")
                and self.executor.active_action in ("attack", "shoot")
            ):
                hazard["severity"] = "urgent"
        if hazards:
            self.state.set_safety_intent(hazards[0])
        else:
            cur_intent = getattr(self.state, "safety_intent", None)
            keep_event_intent = (
                isinstance(cur_intent, dict)
                and cur_intent.get("source") == "hurt_interrupt"
                and isinstance(cur_intent.get("expires_at"), (int, float))
                and time.monotonic() < float(cur_intent["expires_at"])
            )
            if not keep_event_intent:
                self.state.set_safety_intent(None)
        active_escape = self.state.get_active_escape()
        urgent = [
            h for h in hazards
            if h["severity"] == "urgent" and not _same_escape_kind(active_escape, h["kind"])
        ]
        keys = {h["kind"] for h in urgent}
        # 持续存在的危险不重新触发；解除后也要过冷静期，防止阈值抖动。
        if reconcile:
            self._active.intersection_update(keys)
        now = time.monotonic()
        fresh = [h for h in urgent if h["kind"] not in self._active
                 and now - self._triggered.get(h["kind"], float("-inf")) >= self.config.stop_cooldown_s]
        if fresh:
            if (
                all(h["kind"] in ("void_risk", "heal") for h in fresh)
                and self.state.is_totem_trigger_armed()
                and self.state.consume_totem_trigger_suppression()
            ):
                return hazards[0] if hazards else None
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
            session_backoff = 0.0
            try:
                bg_obs = getattr(self.client, "background_observation", None)
                ctx = bg_obs() if callable(bg_obs) else None
                if ctx is None or not (hasattr(type(ctx), "__enter__") and hasattr(type(ctx), "__exit__")):
                    ctx = contextlib.nullcontext()
                with ctx:
                    self.tick()
                self.session_error = None
                self._last_err_msg = ""
                self._err_repeat = 0
            except Exception as e:
                code = e.code if isinstance(e, ApiError) else ""
                if code in ("not_connected", "death_loop_cooldown", "wrong_server"):
                    self.session_error = code
                    session_backoff = 3.0
                msg = str(e)
                if msg == self._last_err_msg:
                    self._err_repeat += 1
                    if self._err_repeat % 10 == 0:
                        self.executor.log(f"  [monitor] 轮询异常（重复×{self._err_repeat}）: {msg}")
                else:
                    self._last_err_msg = msg
                    self._err_repeat = 1
                    self.executor.log(f"  [monitor] 轮询异常: {msg}")
            if self.continuous:
                if self.interval_s is not None:
                    wait_s = max(0.0, float(self.interval_s) - (time.monotonic() - started))
                else:
                    wait_s = max(0.0, self._continuous_wait_s())
            else:
                # 从轮询开始计时，慢 RTT 时不堆积补发。
                wait_s = max(0.01, self.next_interval() - (time.monotonic() - started))
            if session_backoff > 0.0:
                wait_s = max(wait_s, session_backoff)
            self._stop_ev.wait(wait_s)

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
