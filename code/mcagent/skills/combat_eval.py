"""双方战力评估与躲避可行性判断（纯只读决策 + 有界自卫反杀，严守单步单动作与 <400 行约束）。"""
from __future__ import annotations

import math
import time
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from ..client import ApiError
from ..runtime import Replan
from .base import rec
from .encounter import wait_attack_cooldown, weapon_cooldown_s
from .items import AIR, bare
from .voxel_cache import voxel_cache_for

if TYPE_CHECKING:
    from ..runtime import Executor, State

PRIMARY_WEAPONS: tuple[tuple[str, float], ...] = (
    ("netherite_sword", 8.0), ("diamond_sword", 7.0), ("iron_sword", 6.0), ("stone_sword", 5.0),
    ("netherite_axe", 7.2), ("diamond_axe", 6.5), ("iron_axe", 5.8), ("stone_axe", 5.2),
    ("wooden_sword", 4.0), ("golden_sword", 4.0), ("wooden_axe", 4.2),
)
EMERGENCY_TOOLS: tuple[tuple[str, float], ...] = (
    ("netherite_pickaxe", 4.5), ("diamond_pickaxe", 4.0), ("iron_pickaxe", 3.5), ("stone_pickaxe", 3.0),
)
NEVER_MELEE_MOBS: frozenset[str] = frozenset({
    "creeper", "warden", "ravager", "wither", "ender_dragon", "evoker", "vindicator", "piglin_brute",
    "witch", "blaze", "ghast", "enderman", "elder_guardian", "guardian", "shulker", "phantom",
    "vex", "hoglin", "zoglin", "breeze", "creaking",
})
RANGED_MOBS: frozenset[str] = frozenset({"skeleton", "stray", "bogged", "pillager", "parched"})
FAST_PURSUIT_MOBS: frozenset[str] = frozenset({"spider", "cave_spider", *RANGED_MOBS})
_MOB_BASE_THREAT: dict[str, tuple[float, float]] = {
    "zombie": (20.0, 3.2), "husk": (20.0, 3.4), "drowned": (20.0, 3.4), "zombie_villager": (20.0, 3.2),
    "spider": (16.0, 3.0), "cave_spider": (12.0, 4.2), "slime": (16.0, 3.0),
    "silverfish": (8.0, 2.0), "endermite": (8.0, 2.2),
    "skeleton": (20.0, 4.2), "stray": (20.0, 4.4), "bogged": (16.0, 4.4),
    "pillager": (24.0, 4.8), "parched": (20.0, 4.2),
}
_ARMOR_POINTS: dict[str, float] = {
    "leather": 0.06, "golden": 0.09, "chainmail": 0.11, "iron": 0.15, "diamond": 0.22, "netherite": 0.25,
}
_HAZARD_BLOCKS: frozenset[str] = frozenset({
    "lava", "flowing_lava", "fire", "soul_fire", "magma_block",
    "water", "flowing_water", "sweet_berry_bush", "cactus",
})


@dataclass(frozen=True)
class CombatPowerAssessment:
    self_power: float
    enemy_power: float
    win_ratio: float
    best_weapon: Optional[str]
    fallback_weapon: Optional[str]
    has_shield: bool
    enemy_count: int
    force_flee: bool
    reason: str


@dataclass(frozen=True)
class EvasionAssessment:
    can_evade: bool
    evasion_score: float
    open_dirs: int
    can_sprint: bool
    reason: str


@dataclass(frozen=True)
class TacticalDecision:
    action: str  # "fight" | "flee"
    weapon: Optional[str]
    target: Optional[dict[str, Any]]
    power: CombatPowerAssessment
    evasion: EvasionAssessment
    reason: str


def _num(val: Any, default: float) -> float:
    return float(val) if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val) else default


def _state_snapshot(state: "State") -> dict[str, Any]:
    snap = state.snapshot() if hasattr(state, "snapshot") else getattr(state, "st", {})
    return dict(snap) if isinstance(snap, Mapping) else {}


def _armor_multiplier(st: Mapping[str, Any]) -> float:
    bonus = 0.0
    armor = st.get("armor")
    if isinstance(armor, Mapping):
        for piece in armor.values():
            name = bare(piece.get("name") if isinstance(piece, Mapping) else str(piece or ""))
            for tier, val in _ARMOR_POINTS.items():
                if name.startswith(f"{tier}_"):
                    bonus += val
                    break
    elif isinstance(armor, (int, float)) and not isinstance(armor, bool):
        bonus = min(0.8, max(0.0, float(armor) * 0.035))
    return 1.0 + min(1.0, bonus)


def _has_shield(state: "State", st: Mapping[str, Any]) -> bool:
    if hasattr(state, "count") and state.count("shield") >= 1:
        return True
    for src in (st.get("offhand"), (getattr(state, "inventory", None) or {}).get("offhand")):
        if isinstance(src, Mapping) and bare(src.get("name")) == "shield":
            return True
    return False


def _best_owned(state: "State", st: Mapping[str, Any], table: Sequence[tuple[str, float]]) -> tuple[Optional[str], float]:
    held_name = bare((st.get("held") or {}).get("name")) if isinstance(st.get("held"), Mapping) else ""
    for item, dps in table:
        if (hasattr(state, "count") and state.count(item) >= 1) or held_name == item:
            return item, dps
    return None, 0.0


def evaluate_combat_power(state: "State", hostiles: Sequence[Mapping[str, Any]]) -> CombatPowerAssessment:
    """估算双方战力比（>=1.25 显著占优，<0.75 明显劣势）。"""
    st = _state_snapshot(state)
    hp = _num(st.get("health"), 20.0)
    weapon, weapon_dps = _best_owned(state, st, PRIMARY_WEAPONS)
    fb_weapon, fb_dps = _best_owned(state, st, EMERGENCY_TOOLS)
    shield = _has_shield(state, st)
    effective_dps = weapon_dps if weapon is not None else (fb_dps if fb_weapon is not None else 1.0)
    self_power = round(max(0.0, hp) * _armor_multiplier(st) * (1.25 if shield else 1.0) * effective_dps, 2)

    if not hostiles:
        return CombatPowerAssessment(self_power, 0.0, 99.0, weapon, fb_weapon, shield, 0, False, "周边无敌对生物")

    force_flee, force_reason, raw_enemy = False, "", 0.0
    for mob in hostiles:
        name = bare(mob.get("name")).strip().lower()
        dist = _num(mob.get("dist"), 4.0)
        if name in NEVER_MELEE_MOBS:
            force_flee, force_reason = True, f"高危敌怪 {name} 禁止近战硬刚"
        elif name in RANGED_MOBS and (not shield or len(hostiles) > 1 or dist > 6.5):
            force_flee, force_reason = True, f"远程敌怪 {name}（无盾/远距/多怪）优先规避"
        default_hp, base_dps = _MOB_BASE_THREAT.get(name, (20.0, 4.0))
        raw_enemy += max(4.0, _num(mob.get("health"), default_hp)) * base_dps

    enemy_power = round(raw_enemy * (1.0 + 0.65 * max(0, len(hostiles) - 1)), 2)
    win_ratio = round(self_power / max(1.0, enemy_power), 3)
    reason = (
        force_reason if force_flee else
        f"无盾面对 {len(hostiles)} 只敌怪群殴（战力比 {win_ratio:.2f}）" if len(hostiles) >= 2 and not shield else
        f"我方战力 {self_power:.1f} vs 敌方 {enemy_power:.1f}（比值 {win_ratio:.2f}）"
    )
    return CombatPowerAssessment(self_power, enemy_power, win_ratio, weapon, fb_weapon, shield, len(hostiles), force_flee, reason)


def _cell_passable(cache: Any, client: Any, cell: tuple[int, int, int], dim: str) -> bool:
    x, y, z = cell
    foot = bare(cache.get_block(client, [x, y, z], dimension=dim).get("name"))
    head = bare(cache.get_block(client, [x, y + 1, z], dimension=dim).get("name"))
    floor = bare(cache.get_block(client, [x, y - 1, z], dimension=dim).get("name"))
    return foot in AIR and head in AIR and bool(floor) and floor not in AIR and floor not in _HAZARD_BLOCKS


def evaluate_evasion_feasibility(ex: "Executor", state: "State", hostiles: Sequence[Mapping[str, Any]]) -> EvasionAssessment:
    """评估当前环境与生理状态下能否成功甩脱敌对生物。"""
    st = _state_snapshot(state)
    can_sprint = _num(st.get("food"), 20.0) > 6.0
    pos = st.get("pos")
    dim = str(st.get("dimension") or "overworld")
    cache = voxel_cache_for(state)
    client = getattr(ex, "client", None)

    open_dirs = 0
    if isinstance(pos, (list, tuple)) and len(pos) >= 3 and client is not None and hasattr(client, "get_block"):
        fx, fy, fz = math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if any(_cell_passable(cache, client, (fx + dx, fy + dy, fz + dz), dim) for dy in (0, 1, -1)):
                open_dirs += 1
    else:
        open_dirs = 2

    has_fast = any(bare(m.get("name")).strip().lower() in FAST_PURSUIT_MOBS for m in hostiles)
    in_water = bool(st.get("in_water") or st.get("submerged"))
    score = round(max(0.0, min(1.0, (0.55 if open_dirs >= 2 else (0.30 if open_dirs == 1 else 0.0))
                               + (0.35 if can_sprint else 0.0) + (0.15 if not has_fast else 0.0)
                               - (0.35 if in_water else 0.0))), 2)

    if open_dirs == 0:
        return EvasionAssessment(False, score, open_dirs, can_sprint, "四周无干地退路（矿洞死角或墙体围堵）")
    if not can_sprint and has_fast:
        return EvasionAssessment(False, score, open_dirs, can_sprint, "饱食度<=6无法疾跑且遭遇高速/远程怪难甩脱")
    if in_water and has_fast:
        return EvasionAssessment(False, score, open_dirs, can_sprint, "水中移速受限且遭遇高速怪难甩脱")
    return EvasionAssessment(True, score, open_dirs, can_sprint, f"存在 {open_dirs} 条干地退路且可脱离（评分 {score:.2f}）")


def observe_nearby_hostiles(ex: "Executor", state: "State", *, radius: float = 10.0) -> list[dict[str, Any]]:
    """只读采集半径内敌对生物，缺失实体接口时退化使用 `/status.nearest_hostile`。"""
    st = _state_snapshot(state)
    pos = st.get("pos")
    origin = (float(pos[0]), float(pos[1]), float(pos[2])) if isinstance(pos, (list, tuple)) and len(pos) >= 3 else (0.0, 0.0, 0.0)
    out: list[dict[str, Any]] = []
    client = getattr(ex, "client", None)
    if client is not None and hasattr(client, "get_entities"):
        try:
            resp = client.get_entities(radius=int(math.ceil(radius)))
        except Exception:
            resp = None
        if isinstance(resp, Mapping):
            for ent in resp.get("entities") or ():
                if not isinstance(ent, Mapping) or ent.get("type") != "hostile":
                    continue
                health = ent.get("health")
                if isinstance(health, (int, float)) and not isinstance(health, bool) and health <= 0:
                    continue
                epos = ent.get("pos")
                dist = (math.dist(origin, (float(epos[0]), float(epos[1]), float(epos[2])))
                        if isinstance(epos, (list, tuple)) and len(epos) >= 3 else _num(ent.get("dist"), 99.0))
                if dist <= radius:
                    out.append({**dict(ent), "dist": round(dist, 2)})
    if not out:
        nh = st.get("nearest_hostile")
        if isinstance(nh, Mapping) and nh.get("name") and _num(nh.get("dist"), 99.0) <= radius:
            out.append(dict(nh))
    out.sort(key=lambda e: _num(e.get("dist"), 99.0))
    return out


def _offhand_name(state: "State", st: Mapping[str, Any]) -> str:
    for src in (st.get("offhand"), (getattr(state, "inventory", None) or {}).get("offhand")):
        if isinstance(src, Mapping) and src.get("name"):
            return bare(src.get("name"))
    return ""


def _set_shield_posture(ex: "Executor", state: "State", active: bool) -> bool:
    """§4.11 不占动作锁的盾牌举盾/收盾；死亡或旧网关不支持时静默降级。"""
    posture = getattr(ex, "posture", None)
    if (not active and _state_snapshot(state).get("alive") is False) or not callable(posture):
        return False
    try:
        res = posture(shield=active)
        return bool(res.get("blocking", active)) if isinstance(res, Mapping) else active
    except (ApiError, Replan, AttributeError):
        return False


def _look_at_mob(ex: "Executor", target: Mapping[str, Any]) -> None:
    look = getattr(ex, "look", None)
    if not callable(look):
        return
    try:
        if target.get("id") is not None:
            look(entity_id=target["id"])
        elif isinstance(target.get("pos"), (list, tuple)) and len(target["pos"]) >= 3:
            epos = target["pos"]
            look(pos=[float(epos[0]), float(epos[1]) + 0.9, float(epos[2])])
    except (ApiError, Replan, AttributeError):
        pass


def recent_hurt_attacker_id(state: "State") -> Optional[int]:
    """从最近事件流提取最新一次受击的攻击者实体 ID（遇到 spawn/death 即止）。"""
    events = getattr(state, "events", None)
    if not events:
        return None
    for idx, ev in enumerate(reversed(events)):
        if idx >= 20 or not isinstance(ev, Mapping):
            break
        etype = ev.get("type")
        if etype in ("spawn", "death"):
            break
        if etype == "hurt":
            for key in ("source_entity", "likely_attacker"):
                src = ev.get(key)
                if isinstance(src, Mapping) and isinstance(src.get("id"), int) and not isinstance(src.get("id"), bool):
                    return int(src["id"])
            return None
    return None


def shield_guard_yaw(
    player_pos: Any,
    hostiles: Sequence[Mapping[str, Any]],
    *,
    preferred_attacker_id: Optional[int] = None,
) -> Optional[float]:
    """计算面向敌群的最优举盾 yaw（多怪取威胁加权角平分线，前后夹击时锁定最高危目标/偷袭者）。"""
    if not isinstance(player_pos, (list, tuple)) or len(player_pos) < 3:
        return None
    px, pz = float(player_pos[0]), float(player_pos[2])
    wx, wz = 0.0, 0.0
    best_weight, best_vec = 0.0, None

    for mob in hostiles:
        if not isinstance(mob, Mapping):
            continue
        epos = mob.get("pos")
        if not isinstance(epos, (list, tuple)) or len(epos) < 3:
            continue
        dx, dz = float(epos[0]) - px, float(epos[2]) - pz
        d = math.hypot(dx, dz)
        if d < 1e-6:
            continue
        name = bare(mob.get("name")).strip().lower()
        if name == "creeper" and d <= 7.0:
            w = 5.0 / max(1.0, d)
        elif name in RANGED_MOBS and d <= 16.5:
            w = 3.5 / max(1.5, d)
        elif d <= 8.5:
            w = 2.0 / max(1.0, d)
        else:
            continue
        if preferred_attacker_id is not None and mob.get("id") == preferred_attacker_id:
            w *= 2.5
        ux, uz = dx / d, dz / d
        wx += ux * w
        wz += uz * w
        if w > best_weight:
            best_weight, best_vec = w, (ux, uz)

    if best_vec is None:
        return None
    # 前后 180° 夹击导致向量抵消时，放弃指向空侧方，改锁最高威胁单目标确保 100% 格挡主威胁
    if math.hypot(wx, wz) < 0.25 * best_weight:
        wx, wz = best_vec
    return math.atan2(-wx, -wz)


def _look_for_shield(
    ex: "Executor",
    player_pos: Any,
    hostiles: Sequence[Mapping[str, Any]],
    fallback_target: Mapping[str, Any],
    *,
    preferred_attacker_id: Optional[int] = None,
) -> None:
    if len(hostiles) > 1 or preferred_attacker_id is not None:
        yaw = shield_guard_yaw(player_pos, hostiles, preferred_attacker_id=preferred_attacker_id)
        if yaw is not None and callable(getattr(ex, "look", None)):
            try:
                ex.look(yaw=yaw, pitch=0.0)
                return
            except (ApiError, Replan, AttributeError):
                pass
    _look_at_mob(ex, fallback_target)


def _split_hostile_rings(
    all_hostiles: Sequence[dict[str, Any]], *, melee_radius: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    near = [e for e in all_hostiles if _num(e.get("dist"), 99.0) <= melee_radius]
    outer_ranged = [
        e for e in all_hostiles
        if _num(e.get("dist"), 99.0) > melee_radius and bare(e.get("name")).strip().lower() in RANGED_MOBS
    ]
    return near, outer_ranged


def _refresh_st(ex: "Executor", state: "State") -> dict[str, Any]:
    return state.refresh_status(ex.client, max_age_s=0) if hasattr(state, "refresh_status") else _state_snapshot(state)


def decide_combat_or_flee(ex: "Executor", state: "State", *, radius: float = 10.0) -> TacticalDecision:
    """综合双方战力与躲避可行性，决定当下反杀 (`fight`) 或躲避 (`flee`)。"""
    st = _state_snapshot(state)
    all_hostiles = observe_nearby_hostiles(ex, state, radius=max(radius, 16.0))
    hostiles, outer_ranged = _split_hostile_rings(all_hostiles, melee_radius=radius)
    outer_creepers = [
        e for e in all_hostiles
        if _num(e.get("dist"), 99.0) > radius and bare(e.get("name")).strip().lower() == "creeper"
    ]
    power = evaluate_combat_power(state, hostiles)
    evasion = evaluate_evasion_feasibility(ex, state, hostiles)
    target, hp = (hostiles[0] if hostiles else None), _num(st.get("health"), 20.0)

    if target is None or target.get("id") is None:
        return TacticalDecision("flee", None, target, power, evasion, "未获取可锁定敌怪实体 ID，执行规避")
    if st.get("in_lava") or st.get("submerged") or st.get("in_water"):
        return TacticalDecision("flee", None, target, power, evasion, "身处岩浆或水域，优先上岸脱险")
    if getattr(state, "in_safe_mode", False):
        return TacticalDecision("flee", None, target, power, evasion, "处于安全模式冷却期，禁止恋战")
    if hp < 10.0:
        return TacticalDecision("flee", None, target, power, evasion, f"血量偏低 (hp={hp:.1f}<10)，优先撤离")
    if power.force_flee:
        return TacticalDecision("flee", None, target, power, evasion, power.reason)
    if outer_ranged and not power.has_shield:
        r0 = outer_ranged[0]
        return TacticalDecision("flee", None, target, power, evasion,
                                f"16 格内存在远程敌怪 {bare(r0.get('name')) or 'ranged'}({_num(r0.get('dist'), 16.0):.1f}m) 且无盾，禁止原地对砍")
    if outer_creepers and not power.has_shield:
        c0 = outer_creepers[0]
        return TacticalDecision("flee", None, target, power, evasion,
                                f"16 格内存在苦力怕({_num(c0.get('dist'), 16.0):.1f}m) 且无盾，禁止近战恋战防偷袭引爆")

    proactive_hp_floor = 12.0 if (power.has_shield or _armor_multiplier(st) > 1.0) else 14.0
    if (power.best_weapon is not None and hp >= proactive_hp_floor and power.win_ratio >= 1.25
            and (power.enemy_count == 1 or (power.enemy_count == 2 and power.has_shield))):
        return TacticalDecision("fight", power.best_weapon, target, power, evasion,
                                f"战力占优主动反杀：{power.best_weapon} ({power.reason})")

    defense_weapon = power.best_weapon or power.fallback_weapon
    if not evasion.can_evade and defense_weapon is not None and power.win_ratio >= 0.75:
        return TacticalDecision("fight", defense_weapon, target, power, evasion,
                                f"退路受阻被迫反击（{evasion.reason}；{power.reason}）")

    return TacticalDecision("flee", None, target, power, evasion, f"选择规避：{power.reason}；{evasion.reason}")


def counterattack_hostile(
    ex: "Executor", state: "State", decision: TacticalDecision, *, max_rounds: int = 5,
) -> Generator[dict, None, bool]:
    """按战力决策执行有界反杀：自动副手装盾+冷却期举盾格挡+满蓄攻速间隔，异常必收盾。"""
    if decision.action != "fight" or not decision.weapon or not decision.target or not hasattr(ex, "attack"):
        return False
    target_id = decision.target.get("id")
    if target_id is None:
        return False

    st = _refresh_st(ex, state)
    held_name = bare((st.get("held") or {}).get("name")) if isinstance(st.get("held"), Mapping) else ""
    if held_name != decision.weapon and hasattr(ex, "equip"):
        try:
            yield rec("equip", ex.equip(decision.weapon, "hand"), item=decision.weapon, tactical="counterattack")
        except (ApiError, Replan):
            return False

    has_shield = decision.power.has_shield or _has_shield(state, st)
    if has_shield and _offhand_name(state, st) not in ("shield", "totem_of_undying") and hasattr(ex, "equip"):
        try:
            yield rec("equip", ex.equip("shield", "off-hand"), item="shield", slot="off-hand", tactical="counterattack")
        except (ApiError, Replan):
            pass

    target_name = bare(decision.target.get("name")).strip().lower()
    target_is_ranged = target_name in RANGED_MOBS
    target_is_fast_melee = target_name in ("spider", "cave_spider")
    cooldown_s = weapon_cooldown_s(decision.weapon)
    hits_per_round = 1 if (has_shield and (target_is_ranged or bare(decision.weapon).endswith("_axe"))) else 2
    shield_raised, last_hit_at = False, 0.0

    try:
        for round_idx in range(max_rounds):
            if round_idx > 0:
                wait_attack_cooldown(last_hit_at, max(cooldown_s, 0.85) if (shield_raised and target_is_ranged) else cooldown_s)
            st = _refresh_st(ex, state)
            hp_now = _num(st.get("health"), 20.0)
            hard_hp_floor = 6.0 if (round_idx >= 1 and target_is_fast_melee) else 10.0
            if st.get("alive") is False or hp_now < hard_hp_floor or st.get("in_lava") or st.get("submerged"):
                return False

            all_hostiles = observe_nearby_hostiles(ex, state, radius=16.0)
            hostiles, outer_ranged = _split_hostile_rings(all_hostiles, melee_radius=10.0)
            if not hostiles:
                return True
            if (len(hostiles) >= 3 or (outer_ranged and not has_shield)
                    or any(bare(h.get("name")).strip().lower() == "creeper" and _num(h.get("dist"), 99.0) <= 6.5 for h in hostiles)):
                return False
            # 仅当面对单只已被打残的高速蜘蛛且无远程支援时允许 6..10 HP 补刀击杀，避免转身慢跑被咬死
            if hp_now < 10.0 and not (round_idx >= 1 and target_is_fast_melee and len(hostiles) == 1 and not outer_ranged):
                return False

            cur_target = next((h for h in hostiles if h.get("id") == target_id), None)
            if cur_target is None:
                return _num(hostiles[0].get("dist"), 99.0) >= 6.0

            dist = _num(cur_target.get("dist"), 3.0)
            if dist > 3.2:
                if dist > 7.0 or not isinstance(cur_target.get("pos"), (list, tuple)) or not hasattr(ex, "move_to"):
                    return False
                if has_shield and target_is_ranged and not shield_raised:
                    _look_for_shield(ex, st.get("pos"), all_hostiles, cur_target,
                                     preferred_attacker_id=recent_hurt_attacker_id(state))
                    shield_raised = _set_shield_posture(ex, state, True)
                elif shield_raised and not target_is_ranged:
                    shield_raised = _set_shield_posture(ex, state, False)
                tpos = [float(cur_target["pos"][0]), float(cur_target["pos"][1]), float(cur_target["pos"][2])]
                try:
                    try:
                        mv = ex.move_to(pos=tpos, rng=2, timeout=4.0, retries=0, stop_on=("death",))
                    except TypeError:
                        mv = ex.move_to(pos=tpos, rng=2)
                    yield rec("move_to", mv, tactical="close_gap")
                except (ApiError, Replan):
                    return False

            if shield_raised:
                shield_raised = _set_shield_posture(ex, state, False)

            try:
                try:
                    res = ex.attack(entity_id=target_id, max_hits=hits_per_round, stop_on=("death",))
                except TypeError:
                    res = ex.attack(entity_id=target_id, max_hits=hits_per_round)
            except (ApiError, Replan) as exc:
                msg = str(exc).lower()
                return "not_found" in msg or "gone" in msg or "invalid" in msg
            last_hit_at = time.monotonic()
            yield rec("attack", res, entity_id=target_id, weapon=decision.weapon, reason=decision.reason)
            if isinstance(res, Mapping) and res.get("killed"):
                nh = _refresh_st(ex, state).get("nearest_hostile")
                return not isinstance(nh, Mapping) or _num(nh.get("dist"), 99.0) >= 6.0

            if has_shield:
                _look_for_shield(ex, st.get("pos"), all_hostiles, cur_target,
                                 preferred_attacker_id=recent_hurt_attacker_id(state))
                shield_raised = _set_shield_posture(ex, state, True)
    finally:
        if shield_raised:
            _set_shield_posture(ex, state, False)
    return False
