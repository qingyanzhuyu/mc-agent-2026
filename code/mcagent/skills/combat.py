"""T1 的 Minecraft 击杀、挡箭及射击技能，均等待服务端进度证据。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ..client import ApiError
from ..runtime import EnvBlocked, Replan
from .base import Param, Skill, rec
from .combat_materials import (
    ROCKET, MaterialPlan, blocked_loadout, carried_sword, prepare_loadout, ranged_plan, shield_plan,
)
from .combat_scene import (
    DEFLECT, KILL_MOB, OL_BETSY, SHOOT_ARROW, SKELETONS, check_after_action, completed, confirm_progress,
    gate_reason, interrupted, prepared_scene, refresh_encounter, select_encounter, stop_activity,
)
from .encounter import combat_melee, shoot_projectile
from .field import collect_drops, equip_item, scan_blocks
from .items import bare
from .observations import refresh_inventory as _fresh_inventory
from .registry import register

_CRITERIA = Path(__file__).resolve().parents[3] / "docs" / "99-原始记录" / "criteria-1.21.11.json"
_DROPS = ("string", "bone", "arrow", "tipped_arrow", "spectral_arrow")
_BACKSTOPS = ("stone", "cobblestone", "deepslate", "stone_bricks", "obsidian")


@lru_cache(maxsize=1)
def _kill_criteria():
    """从 Java 1.21.11 kill_a_mob 的实体谓词派生，不在运行时猜名单。"""
    with _CRITERIA.open(encoding="utf-8") as source:
        advancement = json.load(source)["items"]["adventure/kill_a_mob"]
    return frozenset(
        bare(entry["predicate"]["type"])
        for name in advancement["requirements"][0]
        for entry in advancement["criteria"][name]["conditions"]["entity"]
        if entry["condition"] == "minecraft:entity_properties" and entry["entity"] == "this"
    )


def _allowed_vanilla_hostiles():
    try:
        return _kill_criteria()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Replan("无法加载 1.21.11 击杀判据，需复核仓库中的规则数据") from exc


def _hostile(entity):
    health = entity.get("health")
    return entity.get("type") == "hostile" and type(health) in (float, int) and health > 0


def _shield_source(entity):
    return _hostile(entity) and bare(entity.get("name")) in SKELETONS


@register
class HuntMob(Skill):
    name = "hunt_mob"
    description = "击杀合法敌对生物，或指定种类采集战斗掉落"
    goal = "acq:combat:{target}:{drop}"
    verified = "offline"
    params = {
        "target": Param(str, "原版敌对生物名，any 为任意", default="any"),
        "drop": Param(str, "需要的掉落物类型，none 为只击杀", default="none", choices=("none", *_DROPS)),
        "count": Param(int, "掉落物最终持有量下限", default=1, min=1, max=16),
    }

    @classmethod
    def blocked(cls, state):
        return gate_reason(state, state.snapshot()) or ("" if carried_sword(state) else "背包没有近战剑")

    @classmethod
    def material_plan(cls, args: dict, state) -> MaterialPlan:
        drop = args.get("drop", "none")
        if drop != "none" and state.count(drop) >= args.get("count", 1):
            return ()
        sword = carried_sword(state)
        return ((sword, 1, "borrow"),) if sword is not None else ()

    def run(self, ex, state):
        drop, quantity = self.args["drop"], self.args["count"]
        initial = completed(ex) if drop == "none" else set()
        if bare(KILL_MOB) in initial:
            return
        _fresh_inventory(ex, state)
        if drop != "none" and state.count(drop) >= quantity:
            return
        sword = carried_sword(state)
        if sword is None:
            raise Replan("击杀前需要背包有一把近战剑")
        allowed = _allowed_vanilla_hostiles()
        requested = bare(self.args["target"])
        if requested != "any" and requested not in allowed:
            raise Replan(f"{requested} 不属于 1.21.11 击杀判据")
        candidates = allowed - {"creeper"} if requested == "any" else {requested}

        def eligible(entity):
            return _hostile(entity) and bare(entity.get("name")) in candidates

        scene = select_encounter(ex, state, eligible, description="有掩体退路的目标敌对生物",
                                 missing=f"safe_hostile:{requested}", max_distance=6,
                                 night_source=requested != "parched")
        try:
            yield from equip_item(ex, state, sword)
            scene = refresh_encounter(ex, state, scene)
            before = {name: state.count(name) for name in _DROPS}
            try:
                evidence = yield from combat_melee(ex, state, scene.target, retreat_pos=scene.retreat,
                                                   timeout_ms=3000, radius=6)
            finally:
                stop_activity(ex, state, busy="attack")
            check_after_action(ex, state, scene, stay=False)
            yield rec("combat_kill_receipt", None, killed=evidence["receipt"].get("killed"),
                      entity=bare(scene.target["name"]))
            gained = {name: state.count(name) - count for name, count in before.items()
                      if state.count(name) > count}
            yield rec("combat_drops", gained, source="attack_inventory_delta")
            if drop == "none":
                yield from confirm_progress(ex, (KILL_MOB,), KILL_MOB, initial=initial,
                                            check=lambda: check_after_action(ex, state, scene, stay=False))
            elif state.count(drop) < quantity:
                gained = yield from collect_drops(ex, state, drop, count=quantity - state.count(drop),
                                                  radius=4, max_targets=1)
                yield rec("combat_drops", {drop: gained}, source="local_pickup_inventory_delta")
        except (ApiError, Replan) as exc:
            yield from interrupted(ex, state, scene, exc)
            raise


def _shield_pulse(ex, target):
    """§4.5 定向举盾入口。实际转向、格挡和 stop_on 时序待实测。"""
    return ex.use_item("shield", target={"entity_id": target["id"]}, source="off-hand",
                       ms=1100, stop_on=("hurt", "death"))


def _lower_shield(ex, state, *, suppress_error):
    try:
        if ex.posture(shield=False).get("blocking") is not False:
            raise Replan("收盾后未确认 blocking=false")
    except (ApiError, Replan):
        if not suppress_error:
            raise
    finally:
        state.invalidate_inventory()
        stop_activity(ex, state, items=("shield",))


@register
class DeflectSkeletonArrow(Skill):
    name = "deflect_skeleton_arrow"
    description = "用副手盾牌格挡骷髅弹射物"
    goal = "adv:deflect_arrow"
    verified = "offline"

    @classmethod
    def blocked(cls, state):
        return gate_reason(state, state.snapshot()) or blocked_loadout(state, shield_plan(state))

    @classmethod
    def material_plan(cls, args: dict, state) -> MaterialPlan:
        return shield_plan(state).materials

    def run(self, ex, state):
        if bare(DEFLECT) in completed(ex):
            return
        _fresh_inventory(ex, state)
        plan = shield_plan(state)
        if reason := blocked_loadout(state, plan):
            raise Replan(reason)
        def choose():
            return select_encounter(ex, state, _shield_source, description="有掩体退路的骷髅或沙漠骷髅",
                                    missing="safe_skeleton_arrow_lane", max_distance=12, aimed=True,
                                    night_source=True)

        scene = choose()
        attempted = False
        successful = False
        try:
            yield from prepare_loadout(ex, state, plan)
            yield from equip_item(ex, state, "shield", slot="off-hand")
            scene = prepared_scene(ex, state, scene, choose, aimed=True)
            try:
                for _ in range(3):
                    attempted = True
                    receipt = _shield_pulse(ex, scene.target)
                    yield rec("use_item", receipt, item="shield", target=scene.target["id"])
                    check_after_action(ex, state, scene)
                    _fresh_inventory(ex, state)
                    confirmed = bare(DEFLECT) in completed(ex)
                    yield rec("combat_progress", {DEFLECT: confirmed})
                    if confirmed:
                        successful = True
                        break
                    scene = refresh_encounter(ex, state, scene, aimed=True)
                    if state.count("shield") == 0:
                        raise Replan("本次举盾后盾牌已不可用")
            finally:
                if attempted:
                    _lower_shield(ex, state, suppress_error=not successful)
            if not successful:
                raise Replan("举盾等待达到 3 次上限，未确认弹射物格挡进度")
        except (ApiError, Replan) as exc:
            yield from interrupted(ex, state, scene, exc)
            raise


def _backstop_candidates(ex, state):
    observed = scan_blocks(ex, state, _BACKSTOPS, radius=14, limit=6)
    return tuple((x + 0.5, y + 0.5, z + 0.5) for x, y, z in
                 (block["pos"] for block in observed["blocks"]))


def _shot_encounter(ex, state, *, crossbow_only, rockets):
    if not rockets:
        allowed = _allowed_vanilla_hostiles()
        try:
            return select_encounter(ex, state, lambda entity: _hostile(entity) and bare(entity["name"]) in allowed,
                                    description="有安全射界和掩体退路的目标",
                                    missing="safe_combat_shot", max_distance=14, aimed=True)
        except EnvBlocked:
            if not crossbow_only:
                raise
    return select_encounter(
        ex, state, lambda entity: False, description="可安全承接弩弹的实心挡墙",
        missing="safe_crossbow_backstop", max_distance=14, aimed=True, backstops=_backstop_candidates(ex, state),
        min_distance=8 if rockets else 2, splash_radius=5 if rockets else 0,
    )


def _fire(ex, state, *, crossbow_only):
    primary = OL_BETSY if crossbow_only else SHOOT_ARROW
    initial = completed(ex)
    if bare(primary) in initial:
        return
    _fresh_inventory(ex, state)
    plan = ranged_plan(state, crossbow_only=crossbow_only)
    if reason := blocked_loadout(state, plan):
        raise Replan(reason)
    def choose():
        return _shot_encounter(ex, state, crossbow_only=crossbow_only, rockets=plan.ammo == ROCKET)

    scene = choose()
    try:
        yield from prepare_loadout(ex, state, plan)
        scene = prepared_scene(ex, state, scene, choose, aimed=True)
        _fresh_inventory(ex, state)
        current_plan = ranged_plan(state, crossbow_only=crossbow_only)
        if (current_plan is None or current_plan.crafts or current_plan.weapon != plan.weapon
                or current_plan.ammo != plan.ammo):
            raise Replan("射击前兼容弹药或武器与已声明物资不一致")
        try:
            target = {"entity": scene.target} if scene.target is not None else {"pos": scene.aim}
            evidence = yield from shoot_projectile(ex, state, **target, weapon=plan.weapon,
                                                   retreat_pos=scene.retreat, max_distance=scene.max_distance)
        finally:
            stop_activity(ex, state, items=("bow", "crossbow"))
        check_after_action(ex, state, scene)
        yield rec("combat_shot", evidence["receipt"], ammo_spent=evidence["ammo_spent"], weapon=plan.weapon)
        spent = {item: count for item, count in evidence["ammo_spent"].items() if count > 0}
        if spent != {plan.ammo: 1}:
            raise Replan("射击后的实际弹药扣减与本次预留不一致")
        wanted = (SHOOT_ARROW, OL_BETSY) if plan.weapon == "crossbow" else (SHOOT_ARROW,)
        await_both = len(wanted) == 2 and scene.target is not None and plan.ammo != ROCKET
        yield from confirm_progress(ex, wanted, primary, initial=initial, await_all=await_both,
                                    check=lambda: check_after_action(ex, state, scene))
    except (ApiError, Replan) as exc:
        yield from interrupted(ex, state, scene, exc)
        raise


class _RangedSkill(Skill):
    crossbow_only = False

    @classmethod
    def blocked(cls, state):
        return gate_reason(state, state.snapshot()) or blocked_loadout(
            state, ranged_plan(state, crossbow_only=cls.crossbow_only))

    @classmethod
    def material_plan(cls, args: dict, state) -> MaterialPlan:
        plan = ranged_plan(state, crossbow_only=cls.crossbow_only)
        return plan.materials if plan is not None else ()

    def run(self, ex, state):
        yield from _fire(ex, state, crossbow_only=self.crossbow_only)


@register
class ShootMob(_RangedSkill):
    name = "shoot_mob"
    description = "用弓或弩发射真箭并验证对实体造成伤害"
    goal = "adv:shoot_arrow"
    verified = "offline"


@register
class FireCrossbow(_RangedSkill):
    name = "fire_crossbow"
    description = "用弩发射箭或烟花并核对服务器进度"
    goal = "adv:ol_betsy"
    verified = "offline"
    crossbow_only = True
