"""紧急安全脱险技能：响应 Safety 意图执行上浮、灭火/脱岩浆或脱离敌对生物。"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any, Callable, Generator, Optional

from .base import Param, Skill, rec
from .registry import register


def _is_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val)


def _valid_pos(pos: Any) -> Optional[tuple[float, float, float]]:
    if not isinstance(pos, (list, tuple)) or len(pos) != 3:
        return None
    if not all(_is_number(axis) for axis in pos):
        return None
    return (float(pos[0]), float(pos[1]), float(pos[2]))


def _state_dict(state: Any) -> dict[str, Any]:
    if hasattr(state, "snapshot"):
        snap = state.snapshot()
        return dict(snap) if isinstance(snap, Mapping) else {}
    st = getattr(state, "st", None)
    return dict(st) if isinstance(st, Mapping) else {}


def _resolve_escape_hazard_step() -> Optional[Callable[..., Generator[dict, None, bool]]]:
    """懒加载 L2 脱险原语，避免未就绪或循环导入时阻塞模块加载。"""
    try:
        from .escape import escape_hazard_step
    except ImportError:
        return None
    return escape_hazard_step


def _fallback_escape_step(ex: Any, state: Any, intent: str) -> Generator[dict, None, bool]:
    """在 L2 escape 原语不可用时执行的单步安全兜底动作。"""
    st = _state_dict(state)
    pos = _valid_pos(st.get("pos"))

    if intent == "douse":
        if hasattr(state, "count") and state.count("water_bucket") >= 1 and hasattr(ex, "use_item"):
            yield rec("use_item", ex.use_item("water_bucket"), intent="douse")
            return True
        if pos is not None and hasattr(ex, "move_to"):
            px, py, pz = pos
            yield rec("move_to", ex.move_to(pos=[px + 2.0, py, pz], rng=1), intent="douse")
            return True
        yield rec("escape_noop", {"intent": "douse", "fallback": True})
        return False

    if intent == "surface":
        if pos is not None and hasattr(ex, "move_to"):
            px, py, pz = pos
            yield rec("move_to", ex.move_to(pos=[px, py + 2.0, pz], rng=1), intent="surface")
            return True
        yield rec("escape_noop", {"intent": "surface", "fallback": True})
        return False

    if intent == "flee":
        if pos is not None and hasattr(ex, "move_to"):
            px, py, pz = pos
            hostile = st.get("nearest_hostile")
            h_name = (
                str(hostile.get("name", "")).removeprefix("minecraft:").strip().lower()
                if isinstance(hostile, Mapping)
                else ""
            )
            h_dist = hostile.get("dist") if isinstance(hostile, Mapping) else None
            is_creeper = h_name == "creeper"
            stride = 10.0 if is_creeper else 8.0
            hpos = _valid_pos(hostile.get("pos")) if isinstance(hostile, Mapping) else None
            if hpos is not None:
                dx = px - hpos[0]
                dz = pz - hpos[2]
                dist = math.hypot(dx, dz)
                if dist > 1e-6:
                    tx = px + (dx / dist) * stride
                    tz = pz + (dz / dist) * stride
                else:
                    tx, tz = px + stride, pz
            else:
                tx, tz = px + stride, pz
            shield_up = False
            if (
                is_creeper
                and _is_number(h_dist)
                and float(h_dist) <= 3.5
                and hasattr(state, "count")
                and state.count("shield") >= 1
                and hasattr(ex, "posture")
            ):
                try:
                    ex.posture(shield=True)
                    shield_up = True
                except Exception:
                    shield_up = False
            try:
                try:
                    mv = ex.move_to(pos=[tx, py, tz], rng=2, stop_on=("death",))
                except TypeError:
                    mv = ex.move_to(pos=[tx, py, tz], rng=2)
                yield rec("move_to", mv, intent="flee")
            finally:
                if shield_up and _state_dict(state).get("alive") is not False:
                    try:
                        ex.posture(shield=False)
                    except Exception:
                        pass
            return True
        yield rec("escape_noop", {"intent": "flee", "fallback": True})
        return False

    yield rec("escape_noop", {"intent": intent, "fallback": True})
    return False


@register
class EmergencyEscape(Skill):
    name = "emergency_escape"
    description = "响应安全意图执行上浮上岸、灭火或脱离敌对生物"
    params = {
        "intent": Param(str, "紧急安全意图", default="surface", choices=("surface", "flee", "douse")),
    }
    goal = "safety:{intent}"
    cooldown_s = 1.0
    verified = "live"

    _start_pos: Optional[tuple[float, float, float]] = None
    _sealed_corridor: bool = False
    _cover_taken: bool = False

    @staticmethod
    def _has_active_hazard(state: Any) -> bool:
        intent_raw = getattr(state, "safety_intent", None)
        if isinstance(intent_raw, str) and intent_raw.strip():
            return True
        if isinstance(intent_raw, Mapping):
            kind = intent_raw.get("kind") or intent_raw.get("intent")
            if isinstance(kind, str) and kind.strip():
                expires_at = intent_raw.get("expires_at")
                if not (_is_number(expires_at) and time.monotonic() >= float(expires_at)):
                    return True
        st = _state_dict(state)
        if st.get("in_water") or st.get("submerged") or st.get("in_lava") or st.get("on_fire"):
            return True
        for key in ("oxygen", "air", "breath"):
            val = st.get(key)
            if _is_number(val) and float(val) < 20.0:
                return True
        hostile = st.get("nearest_hostile")
        if isinstance(hostile, Mapping):
            dist = hostile.get("dist")
            if _is_number(dist) and float(dist) <= 12.0:
                return True
        return False

    @classmethod
    def blocked(cls, state) -> str:
        return "" if cls._has_active_hazard(state) else "当前无紧急安全风险"

    def can_start(self, state) -> bool:
        return self._has_active_hazard(state)

    def run(self, ex, state):
        self._start_pos = _valid_pos(_state_dict(state).get("pos"))
        self._sealed_corridor = False
        self._cover_taken = False
        intent = self.args["intent"]
        setter = getattr(state, "set_active_escape", None)
        if callable(setter):
            setter(intent)
        try:
            escape_step = _resolve_escape_hazard_step()
            if escape_step is not None:
                gen = escape_step(ex, state, intent)
                sealed_places = 0
                cover_ok = False
                ok = False
                try:
                    while True:
                        try:
                            step = next(gen)
                        except StopIteration as stop:
                            ok = stop.value
                            break
                        if isinstance(step, Mapping) and not step.get("error_code"):
                            if step.get("tunnel_seal"):
                                sealed_places += 1
                            if step.get("skeleton_cover"):
                                cover_ok = True
                        yield step
                finally:
                    close = getattr(gen, "close", None)
                    if callable(close):
                        close()
                self._sealed_corridor = bool(ok and sealed_places >= 2)
                self._cover_taken = bool(ok and cover_ok)
                return ok
            return (yield from _fallback_escape_step(ex, state, intent))
        finally:
            if callable(setter):
                setter(None)

    def verify_done(self, state, before_counts=None) -> str:
        st = _state_dict(state)
        intent = self.args["intent"]

        if intent == "surface":
            in_water = bool(st.get("in_water"))
            submerged = bool(st.get("submerged"))
            oxygen = None
            for key in ("oxygen", "air", "breath"):
                if key in st and st[key] is not None:
                    oxygen = st[key]
                    break
            if (not in_water and not submerged) or (not submerged and _is_number(oxygen) and oxygen >= 15):
                return ""
            return "上浮未完成：仍处于水下或氧气未恢复"

        if intent == "douse":
            if not st.get("on_fire") and not st.get("in_lava"):
                return ""
            return "灭火/脱岩浆未完成：仍处于着火或岩浆中"

        if intent == "flee":
            if self._sealed_corridor or self._cover_taken:
                return ""
            hostile = st.get("nearest_hostile")
            if not isinstance(hostile, Mapping) or not hostile:
                return ""
            h_name = str(hostile.get("name", "")).removeprefix("minecraft:").strip().lower()
            safe_dist = 7.0 if h_name == "creeper" else 6.0
            dist = hostile.get("dist")
            if _is_number(dist) and dist >= safe_dist:
                return ""
            cur_pos = _valid_pos(st.get("pos"))
            if self._start_pos is not None and cur_pos is not None:
                if math.dist(self._start_pos, cur_pos) >= 4.0:
                    return ""
            return "脱离未完成：敌对生物仍处于近战威胁距离"

        return ""
