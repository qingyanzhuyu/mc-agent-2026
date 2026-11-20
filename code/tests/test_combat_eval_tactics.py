"""双方战力与躲避可行性战术评估器 + EmergencyEscape 智能攻防离线单测（verified: offline）。

覆盖：
1. 双方战力模型 `evaluate_combat_power`：武器伤害、护甲/盾牌折算、血量门限与多怪群殴惩罚；
2. 躲避可行性模型 `evaluate_evasion_feasibility`：饱食度禁疾跑、高速怪（蜘蛛/幼年僵尸）、
   四面墙/矿道死角围堵；
3. 综合决策 `decide_combat_or_flee`：苦力怕/残血/安全模式必逃、持剑对落单僵尸主攻、
   群殴且可逃则避、无路可逃且有自卫工具则被迫反击；
4. `escape_hazard_step("flee")` 端到端集成：持剑遇落单僵尸自动拔剑反杀；交战中途掉血或刷出苦力怕
   立即中止攻击转入逃生。
"""
from __future__ import annotations

import time
import unittest

from mcagent.skills.combat_eval import (
    decide_combat_or_flee,
    evaluate_combat_power,
    evaluate_evasion_feasibility,
)
from mcagent.skills.escape import escape_hazard_step
from mcagent.skills.safety_escape import EmergencyEscape
from mcagent.skills.testing import GridWorld


def _drive_result(gen, ex):
    steps = []
    while True:
        ex.step_acts = 0
        try:
            rec = next(gen)
        except StopIteration as stop:
            return stop.value, steps
        assert ex.step_acts <= 1, f"每步最多 1 个阻塞动作，实际 {ex.step_acts}"
        steps.append(rec)


def _mob(name: str, *, eid: int = 10, pos=(2.5, 64.0, 0.5), health: float = 20.0, dist: float = 2.0) -> dict:
    return {
        "id": eid,
        "name": name,
        "type": "hostile",
        "pos": list(pos),
        "health": health,
        "dist": dist,
    }


class _CombatTacticsWorld(GridWorld):
    def __init__(self, *, pos=(0.5, 64.0, 0.5), items=None, blocks=None, entities=None,
                 health: float = 20.0, food: int = 20, held=None, armor=None,
                 kill_on_hit: int = 2, mid_fight_hook=None):
        ground = {(x, 63, z): "stone" for x in range(-12, 13) for z in range(-12, 13)}
        if blocks:
            ground.update(blocks)
        super().__init__(pos=pos, blocks=ground, items=items or {})
        self.st["health"] = health
        self.st["food"] = food
        self.st["on_ground"] = True
        if held:
            self.st["held"] = {"name": held}
        if armor:
            self.st["armor"] = armor
        self.entities = list(entities or [])
        if self.entities:
            m0 = self.entities[0]
            self.st["nearest_hostile"] = {"id": m0["id"], "name": m0["name"], "dist": m0.get("dist", 2.0)}
        self.kill_on_hit = kill_on_hit
        self.mid_fight_hook = mid_fight_hook
        self.attack_calls: list[dict] = []

    def get_entities(self, radius=16, types=None):
        self.calls.append(("get_entities", {"radius": radius}))
        return {"entities": [dict(e) for e in self.entities]}

    def equip(self, item, slot="hand", *, stop_on=None):
        res = super().equip(item, slot=slot, stop_on=stop_on)
        if slot == "hand":
            self.st["held"] = {"name": item}
        return res

    def attack(self, entity_id, timeout_ms=5000, *, max_hits=1, interval_ms=600, stop_on=None):
        self.calls.append(("attack", {"entity_id": entity_id, "max_hits": max_hits}))
        self.attack_calls.append({"entity_id": entity_id, "max_hits": max_hits})
        if self.mid_fight_hook is not None:
            self.mid_fight_hook(self, len(self.attack_calls))
        if len(self.attack_calls) >= self.kill_on_hit:
            self.entities = [e for e in self.entities if e["id"] != entity_id]
            if self.entities:
                m0 = self.entities[0]
                self.st["nearest_hostile"] = {"id": m0["id"], "name": m0["name"], "dist": m0.get("dist", 2.0)}
            else:
                self.st["nearest_hostile"] = None
            return {"hits": max_hits, "killed": True}
        return {"hits": max_hits, "killed": False}

    def control(self, **kwargs):
        self.calls.append(("control", kwargs))
        self.st["pos"] = [self.st["pos"][0] - 5.0, self.st["pos"][1], self.st["pos"][2]]
        if self.st.get("nearest_hostile"):
            self.st["nearest_hostile"] = dict(self.st["nearest_hostile"], dist=9.0)
        return {"moved": 5.0}

    def move_to(self, pos, rng=1, **kwargs):
        res = super().move_to(pos=pos, rng=rng)
        if self.st.get("nearest_hostile"):
            self.st["nearest_hostile"] = None
        return res


class CombatEvalTacticsTests(unittest.TestCase):
    def test_combat_power_comparison_single_vs_pack_and_unarmed(self) -> None:
        w_armed = _CombatTacticsWorld(items={"stone_sword": 1}, health=20.0)
        _, st_armed = w_armed.session()
        p_single = evaluate_combat_power(st_armed, [_mob("zombie")])
        self.assertGreaterEqual(p_single.self_power, p_single.enemy_power * 1.25)
        self.assertEqual(p_single.best_weapon, "stone_sword")

        p_pack = evaluate_combat_power(st_armed, [_mob("zombie", eid=1), _mob("zombie", eid=2), _mob("zombie", eid=3)])
        self.assertLess(p_pack.self_power, p_pack.enemy_power)

        w_unarmed = _CombatTacticsWorld(items={}, health=20.0)
        _, st_unarmed = w_unarmed.session()
        p_unarmed = evaluate_combat_power(st_unarmed, [_mob("zombie")])
        self.assertIsNone(p_unarmed.best_weapon)
        self.assertLess(p_unarmed.self_power, p_unarmed.enemy_power)

    def test_evasion_feasibility_open_vs_walled_and_starving(self) -> None:
        # 1) 开阔平地 + 饱食度充足 -> 易于躲避
        w_open = _CombatTacticsWorld(food=18, entities=[_mob("zombie", pos=(2.5, 64.0, 0.5))])
        ex_open, st_open = w_open.session()
        ev_open = evaluate_evasion_feasibility(ex_open, st_open, w_open.entities)
        self.assertTrue(ev_open.can_evade)

        # 2) 四面 2 格高石墙围堵（矿洞死角）-> 躲不掉
        walls = {}
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            for dy in (0, 1):
                walls[(dx, 64 + dy, dz)] = "stone"
        w_boxed = _CombatTacticsWorld(food=18, blocks=walls, entities=[_mob("zombie", pos=(1.5, 64.0, 0.5), dist=1.0)])
        ex_boxed, st_boxed = w_boxed.session()
        ev_boxed = evaluate_evasion_feasibility(ex_boxed, st_boxed, w_boxed.entities)
        self.assertFalse(ev_boxed.can_evade)

        # 3) 饱食度 <= 6 无法疾跑 + 蜘蛛高速追击 -> 躲不掉
        w_starve = _CombatTacticsWorld(food=4, entities=[_mob("spider", pos=(2.0, 64.0, 0.5), dist=1.8)])
        ex_starve, st_starve = w_starve.session()
        ev_starve = evaluate_evasion_feasibility(ex_starve, st_starve, w_starve.entities)
        self.assertFalse(ev_starve.can_evade)

    def test_decision_rules_creeper_low_hp_safe_mode_vs_lone_zombie(self) -> None:
        # 苦力怕永远逃跑（哪怕钻石剑满血）
        w_creeper = _CombatTacticsWorld(items={"diamond_sword": 1, "shield": 1}, health=20.0, entities=[_mob("creeper")])
        ex_c, st_c = w_creeper.session()
        self.assertEqual(decide_combat_or_flee(ex_c, st_c).action, "flee")

        # 残血 (hp=8) 永远逃跑
        w_low = _CombatTacticsWorld(items={"iron_sword": 1}, health=8.0, entities=[_mob("zombie")])
        ex_l, st_l = w_low.session()
        self.assertEqual(decide_combat_or_flee(ex_l, st_l).action, "flee")

        # 安全模式 (in_safe_mode) 永远逃跑
        w_safe = _CombatTacticsWorld(items={"iron_sword": 1}, health=20.0, entities=[_mob("zombie")])
        ex_s, st_s = w_safe.session()
        st_s.safe_mode_until = time.monotonic() + 60.0
        self.assertEqual(decide_combat_or_flee(ex_s, st_s).action, "flee")

        # 持石剑满血遇落单僵尸 -> 主动反杀
        w_fight = _CombatTacticsWorld(items={"stone_sword": 1}, health=18.0, entities=[_mob("zombie")])
        ex_f, st_f = w_fight.session()
        dec_f = decide_combat_or_flee(ex_f, st_f)
        self.assertEqual(dec_f.action, "fight")
        self.assertEqual(dec_f.weapon, "stone_sword")

        # 矿洞四面围堵无法躲避 + 有石镐 + 满血对单只僵尸 -> 被迫自卫反击
        walls = {(dx, 64 + dy, dz): "stone" for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)) for dy in (0, 1)}
        w_corner = _CombatTacticsWorld(items={"stone_pickaxe": 1}, health=18.0, blocks=walls,
                                       entities=[_mob("zombie", pos=(1.4, 64.0, 0.5), dist=1.2)])
        ex_cn, st_cn = w_corner.session()
        dec_cn = decide_combat_or_flee(ex_cn, st_cn)
        self.assertEqual(dec_cn.action, "fight")
        self.assertEqual(dec_cn.weapon, "stone_pickaxe")

    def test_escape_hazard_step_fights_and_kills_lone_zombie_when_armed(self) -> None:
        w = _CombatTacticsWorld(
            items={"stone_sword": 1},
            health=20.0,
            entities=[_mob("zombie", eid=42, pos=(2.0, 64.0, 0.5), dist=1.8)],
            kill_on_hit=2,
        )
        ex, state = w.session()
        ok, steps = _drive_result(escape_hazard_step(ex, state, "flee"), ex)
        self.assertTrue(ok)
        self.assertGreaterEqual(len(w.attack_calls), 2)
        self.assertEqual(w.attack_calls[0]["entity_id"], 42)
        self.assertEqual(w.st.get("held"), {"name": "stone_sword"})
        self.assertEqual(EmergencyEscape(intent="flee").verify_done(state), "")

    def test_escape_hazard_step_bails_out_to_flee_if_hp_drops_mid_fight(self) -> None:
        def hurt_player(world, hit_no):
            if hit_no == 1:
                world.st["health"] = 7.0  # 首刀后掉至残血，必须立刻转逃生

        w = _CombatTacticsWorld(
            items={"stone_sword": 1},
            health=18.0,
            entities=[_mob("zombie", eid=42, pos=(2.0, 64.0, 0.5), dist=1.8)],
            kill_on_hit=5,
            mid_fight_hook=hurt_player,
        )
        ex, state = w.session()
        ok, steps = _drive_result(escape_hazard_step(ex, state, "flee"), ex)
        self.assertTrue(ok)
        # 打了 1 次后残血立刻中止战斗，并执行了逃生（control 或 move_to）
        self.assertEqual(len(w.attack_calls), 1)
        ops = [s.get("op") for s in steps]
        self.assertTrue("control" in ops or "move_to" in ops, f"残血后应转入逃生，实际步骤: {ops}")


if __name__ == "__main__":
    unittest.main()
