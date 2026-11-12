"""运行时：状态跟踪 / 安全闸 / 串行执行器 / 打点记录。

核心事实（来自 spec v1.3.1）：
- 动作互斥且同步阻塞，所以执行器必须是串行的单飞锁。
- /status、/events、/find_blocks 不占动作锁，动作进行中也能读。
- 60s 内死 3 次 -> 下线 5 分钟，这是唯一的一票否决，安全闸优先于任何计划。
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from typing import Any, Callable, Optional

from .client import ApiError, FATAL, REPLAN, RETRY, Client


class Replan(Exception):
    """前置条件不成立，当前任务作废，换一个目标。"""


class Fatal(Exception):
    """认证/接口级错误，别继续硬跑。"""


# ---------------------------------------------------------------- 状态
class State:
    #: 触发接近死亡循环（60s 内 ≥2 死）后的强制避险冷却时长（秒，留 5s 缓冲防窗口边界抖动）
    SAFE_COOLDOWN_S: float = 65.0

    def __init__(self):
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
    def set_status(self, st: dict) -> None:
        was_booted = self._status_booted
        prev_alive = self.st.get("alive", True)
        self.st = st
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

    def set_inventory(self, inv: dict) -> None:
        self.inventory = inv

    def drain(self, client: Client, limit: int = 200) -> list[dict]:
        """按 seq 增量拉事件，一直拉到没有积压，并同步消费死亡与死亡循环事件。"""
        was_booted = self._events_booted
        out: list[dict] = []
        for _ in range(20):  # 防止死循环
            r = client.events(since=self.last_seq, limit=limit)
            evs = r.get("events") or []
            self.last_seq = r.get("last_seq", self.last_seq)
            out.extend(evs)
            self.events.extend(evs)
            if not r.get("has_more"):
                break
        self._events_booted = True

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
                if etype == "death":
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
    """只产出"必须立刻做"的判断，不产出策略。返回 None 表示安全。"""

    HP_CRIT = 8.0
    HP_WARN = 12.0
    OXY_WARN = 80

    def check(self, s: State) -> Optional[dict]:
        st = s.st
        if not st:
            return {"kind": "no_status"}
        if not st.get("alive", True):
            return {"kind": "wait_respawn", "why": "已死亡，等重生"}
        recent_deaths = s.deaths_in(60)
        if recent_deaths >= 2:
            s.safe_mode_until = max(s.safe_mode_until, time.monotonic() + s.SAFE_COOLDOWN_S)
        if s.in_safe_mode:
            return {
                "kind": "safe_mode",
                "remaining_s": round(s.safe_mode_remaining, 1),
                "why": f"60s 内已死 {recent_deaths} 次，强制避险冷却剩 {s.safe_mode_remaining:.0f}s",
            }
        if st.get("in_lava"):
            return {"kind": "escape_lava", "why": "在岩浆里"}
        if st.get("submerged") and int(st.get("oxygen", 300)) < self.OXY_WARN:
            return {"kind": "surface", "why": f"氧气 {st.get('oxygen')}"}
        if s.hp <= self.HP_CRIT:
            return {"kind": "heal", "why": f"血量 {s.hp}"}
        h = st.get("nearest_hostile")
        if h and s.hp <= self.HP_WARN and float(h.get("dist", 99)) < 6:
            return {"kind": "flee", "why": f"{h.get('name')} 距离 {h.get('dist')}，血量 {s.hp}"}
        if st.get("on_fire") and not st.get("in_water"):
            return {"kind": "douse", "why": "着火"}
        return None


# ---------------------------------------------------------------- 执行器
class Executor:
    """串行执行动作：单飞锁 + 错误分类 + 超时兜底 /stop + 打点。"""

    def __init__(self, client: Client, state: State, log_path: Optional[str] = None, log=print):
        self.client = client
        self.state = state
        self.log = log
        self.log_path = log_path
        self.n_actions = 0
        self.n_errors = 0
        self._busy = False

    def _record(self, row: dict) -> None:
        if not self.log_path:
            return
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def act(self, name: str, fn: Callable[[], dict], retries: int = 2, backoff: float = 0.6) -> dict:
        if self._busy:
            raise RuntimeError(f"动作互斥：上一个动作还没结束就想发 {name}")
        self._busy = True
        t0 = time.time()
        attempt = 0
        try:
            while True:
                attempt += 1
                try:
                    result = fn()
                    self.n_actions += 1
                    self._record({
                        "t": round(t0, 3), "name": name, "ms": round((time.time() - t0) * 1000),
                        "ok": True, "attempt": attempt, "result": _shrink(result),
                    })
                    if isinstance(result, dict) and result.get("pos"):
                        self.state.st["pos"] = result["pos"]
                    return result
                except ApiError as e:
                    self.n_errors += 1
                    self._record({
                        "t": round(t0, 3), "name": name, "ms": round((time.time() - t0) * 1000),
                        "ok": False, "attempt": attempt, "code": e.code, "msg": e.message,
                        "kind": e.kind, "extra": _shrink(e.extra),
                    })
                    if e.kind == FATAL:
                        raise Fatal(str(e)) from e
                    if e.kind == RETRY and attempt <= retries:
                        wait = backoff * (2 ** (attempt - 1))
                        if e.code == "dead":
                            wait = min(15.0, wait * 3)
                        self.log(f"  [{name}] {e} -> {wait:.1f}s 后重试 ({attempt}/{retries})")
                        time.sleep(wait)
                        continue
                    raise Replan(str(e)) from e
        finally:
            self._busy = False

    # ---- 常用包装（顺便统一超时/参数） --------------------------------
    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1) -> dict:
        return self.act("move_to", lambda: self.client.move_to(x, z, y=y, entity_id=entity_id, rng=rng))

    def dig(self, pos) -> dict:
        return self.act("dig", lambda: self.client.dig(pos))

    def place(self, item, pos=None) -> dict:
        return self.act("place", lambda: self.client.place(item, pos))

    def interact(self, pos=None, entity_id=None, item=None) -> dict:
        return self.act("interact", lambda: self.client.interact(pos, entity_id, item))

    def craft(self, item, count=1) -> dict:
        return self.act("craft", lambda: self.client.craft(item, count))

    def attack(self, entity_id, timeout_ms=20000) -> dict:
        return self.act("attack", lambda: self.client.attack(entity_id, timeout_ms))

    def equip(self, item, slot="hand") -> dict:
        return self.act("equip", lambda: self.client.equip(item, slot))

    def stop(self) -> None:
        try:
            self.client.stop()
        except Exception as e:
            self.log(f"  [stop] 失败: {e}")


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
