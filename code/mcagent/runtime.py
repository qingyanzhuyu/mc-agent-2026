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
    def __init__(self):
        self.st: dict = {}
        self.last_seq: int = 0
        self.events: deque = deque(maxlen=400)
        self.deaths: deque = deque(maxlen=10)  # 死亡时间戳，用来算死亡循环
        self.inventory: dict = {}

    # ---- 更新
    def set_status(self, st: dict) -> None:
        prev_alive = self.st.get("alive", True)
        self.st = st
        if not st.get("alive", True) and prev_alive:
            self.deaths.append(time.time())
        # last_death 里的时间戳也记一份，避免 alive 抖动漏记
        ld = st.get("last_death") or {}
        if ld.get("t"):
            ts = ld["t"] / 1000.0
            if ts > time.time() - 300:
                self.deaths.append(ts)

    def set_inventory(self, inv: dict) -> None:
        self.inventory = inv

    def drain(self, client: Client, limit: int = 200) -> list[dict]:
        """按 seq 增量拉事件，一直拉到没有积压。"""
        out: list[dict] = []
        for _ in range(20):  # 防止死循环
            r = client.events(since=self.last_seq, limit=limit)
            evs = r.get("events") or []
            self.last_seq = r.get("last_seq", self.last_seq)
            out.extend(evs)
            self.events.extend(evs)
            if not r.get("has_more"):
                break
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

    def count(self, item: str) -> int:
        items = (self.inventory or {}).get("items") or []
        return sum(i.get("count", 0) for i in items if i.get("name") == item)

    def deaths_in(self, window_s: float = 60) -> int:
        now = time.time()
        return sum(1 for t in self.deaths if now - t <= window_s)

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
        if s.deaths_in(60) >= 2:
            return {"kind": "safe_mode", "why": f"60s 内已死 {s.deaths_in(60)} 次，接近死亡循环"}
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
