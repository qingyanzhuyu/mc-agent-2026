"""离线单测：无 health 字段实体识别、攻速满冷却间隔、盾牌主动格挡/收盾、16 格远程侦察、抗骷髅 2 格挡箭墙与早期造盾快路径。"""
from __future__ import annotations

import dataclasses
import math
import unittest
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.loop import RulePlanner, reset_world_memo
from mcagent.runtime import RuntimeConfig, State
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.combat import HuntMob
from mcagent.skills.combat_eval import counterattack_hostile, decide_combat_or_flee
from mcagent.skills.combat_scene import KILL_MOB, crowd_melee_hostiles
from mcagent.skills.encounter import pillar_up, weapon_cooldown_s
from mcagent.skills.escape import escape_hazard_step
from mcagent.skills.iron_gear import CraftIronGear
from mcagent.skills.safety_escape import EmergencyEscape
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches


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


def _real_server_mob(name: str, *, eid: int = 51, pos=(5.5, 64.0, 0.5), dist: float | None = None) -> dict:
    """模拟真实官方服/本地网关 /get_entities：不含 health 字段。"""
    return {
        "id": eid,
        "type": "hostile",
        "name": name,
        "pos": list(pos),
        "dist": round(math.dist([0.5, 64.0, 0.5], pos), 2) if dist is None else dist,
    }


class _ShieldCombatWorld(GridWorld):
    def __init__(self, *, pos=(0.5, 64.0, 0.5), items=None, blocks=None, entities=None,
                 health: float = 20.0, food: int = 20, held=None, offhand=None, kill_on_attack: int = 2):
        ground = {(x, 63, z): "stone" for x in range(-12, 13) for z in range(-12, 13)}
        if blocks:
            ground.update(blocks)
        super().__init__(pos=pos, blocks=ground, items=items or {})
        self.st.update(dimension="overworld", is_day=False, health=health, food=food, on_ground=True)
        if held:
            self.st["held"] = {"name": held}
        if offhand:
            self.st["offhand"] = {"name": offhand, "count": 1, "slot": 45}
        self.entities = list(entities or [])
        if self.entities:
            m0 = self.entities[0]
            self.st["nearest_hostile"] = {"id": m0["id"], "name": m0["name"], "dist": m0.get("dist", 2.0)}
        self.kill_on_attack = kill_on_attack
        self.attack_reqs: list[dict] = []
        self.posture_reqs: list[dict] = []
        self.equip_reqs: list[dict] = []
        self.place_reqs: list[dict] = []
        self.control_reqs: list[dict] = []

    def inventory(self, timeout=10):
        inv = super().inventory(timeout)
        if self.st.get("held"):
            inv["held"] = dict(self.st["held"])
        if self.st.get("offhand"):
            inv["offhand"] = dict(self.st["offhand"])
        return inv

    def get_entities(self, radius=16, types=None):
        self.calls.append(("get_entities", {"radius": radius, "types": types}))
        out = []
        px, py, pz = self.st["pos"]
        for ent in self.entities:
            if types and ent.get("type") not in types and ent.get("name") not in types:
                continue
            d = math.dist([px, py, pz], ent["pos"])
            if d <= radius:
                out.append({**dict(ent), "dist": round(d, 2)})
        return {"entities": out}

    def equip(self, item, slot="hand", *, stop_on=None):
        self.equip_reqs.append({"item": item, "slot": slot})
        res = super().equip(item, slot=slot, stop_on=stop_on)
        if slot == "hand":
            self.st["held"] = {"name": item}
        elif slot == "off-hand":
            self.st["offhand"] = {"name": item, "count": 1, "slot": 45}
        return res

    def posture(self, sneak=None, shield=None, *, jump=None):
        self.posture_reqs.append({"sneak": sneak, "shield": shield, "jump": jump})
        return super().posture(sneak=sneak, shield=shield, jump=jump)

    def look(self, yaw=None, pitch=None, *, pos=None, entity_id=None):
        self.calls.append(("look", {"yaw": yaw, "pitch": pitch, "pos": pos, "entity_id": entity_id}))
        return {"yaw": 0.0, "pitch": 0.0}

    def attack(self, entity_id, timeout_ms=20000, *, max_hits=None, interval_ms=None, stop_on=None):
        req = {
            "entity_id": entity_id,
            "max_hits": max_hits,
            "interval_ms": interval_ms,
            "stop_on": stop_on,
            "blocking_when_swung": bool(self.st.get("blocking")),
        }
        self.calls.append(("attack", req))
        self.attack_reqs.append(req)
        if len(self.attack_reqs) >= self.kill_on_attack:
            self.entities = [e for e in self.entities if e["id"] != entity_id]
            self.st["nearest_hostile"] = (
                {"id": self.entities[0]["id"], "name": self.entities[0]["name"], "dist": self.entities[0].get("dist", 5.0)}
                if self.entities else None
            )
            return {"hits": max_hits or 2, "killed": True}
        return {"hits": max_hits or 2, "killed": False}

    def place(self, item, pos=None, *, stop_on=None):
        self.place_reqs.append({"item": item, "pos": list(pos)})
        return super().place(item, pos=pos, stop_on=stop_on)

    def control(self, **kwargs):
        self.control_reqs.append(dict(kwargs))
        self.calls.append(("control", dict(kwargs)))
        self.st["pos"] = [self.st["pos"][0] - 2.0, self.st["pos"][1], self.st["pos"][2]]
        return {"moved": 2.0, "pos": list(self.st["pos"])}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, **kwargs):
        res = super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos)
        self.st["nearest_hostile"] = None
        return res


class CombatShieldAndCooldownTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def test_missing_health_field_works_in_hunt_mob_and_crowd_detection(self):
        # 真实服务端 /get_entities 不带 health 字段，HuntMob 与群怪检测必须正常识别
        z1 = _real_server_mob("zombie", eid=701, pos=(5.5, 64.0, 0.5))
        z2 = _real_server_mob("zombie", eid=702, pos=(2.0, 64.0, 0.5))
        dead_z = {**_real_server_mob("zombie", eid=703, pos=(1.5, 64.0, 0.5)), "health": 0}
        crowd = crowd_melee_hostiles([z1, z2, dead_z], [0.5, 64.0, 0.5], radius=6.0)
        self.assertEqual([e["id"] for e in crowd], [702, 701])

        blocks = {(x, 63, z): "stone" for x in range(-4, 10) for z in range(-4, 5)}
        blocks.update({(1, 65, 2): "stone", (1, 65, 3): "stone"})
        world = ScriptedWorld(blocks=blocks, items={"stone_sword": 1})
        world.st.update(dimension="overworld", is_day=False, health=20, food=20, on_ground=True)
        live = [dict(z1)]
        world.get_entities = lambda radius=32, types=None: {"entities": list(live)}
        world.advancements = lambda status="completed", include_recipes=False: {
            "ready": True,
            "advancements": [{"id": KILL_MOB, "status": "completed"}] if not live else [],
        }

        held_box = {"name": None}
        base_inv = world.inventory

        def inv_with_held(timeout=10):
            snap = base_inv(timeout)
            if held_box["name"]:
                snap["held"] = {"name": held_box["name"], "count": 1}
            return snap

        world.inventory = inv_with_held

        def equip_sword(w, item, slot, **_):
            held_box["name"] = item
            w.st["held"] = {"name": item}
            return {"equipped": {"name": item}}

        def kill_with_minor_hurt(w, entity_id, stop_on=None, **_):
            self.assertEqual(stop_on, ("death",))
            w.st["health"] = 17  # 近战吃 1 下普攻不该误判失败
            live.clear()
            return {"killed": True, "hits": 4}

        world.queue("equip", equip_sword)
        world.queue("attack", kill_with_minor_hurt)
        ex, state = world.session()
        with patch("mcagent.skills.combat_scene.time.sleep", return_value=None):
            drive(HuntMob(), ex, state)
        self.assertEqual(ex.acts, ["equip", "attack"])
        world.assert_exhausted()

    def test_counterattack_omits_hardcoded_600ms_and_uses_shield_with_cooldown(self):
        # 持石剑 + 背包有盾（尚未装备副手）遇落单僵尸：
        # 1) 自动副手装盾；2) /attack 不传 interval_ms=600；
        # 3) 两轮之间举盾格挡并补足冷却，挥砍前收盾，结束/异常必收盾。
        zombie = _real_server_mob("zombie", eid=88, pos=(2.0, 64.0, 0.5), dist=1.5)
        w = _ShieldCombatWorld(items={"stone_sword": 1, "shield": 1}, health=18.0, entities=[zombie], kill_on_attack=2)
        ex, state = w.session()

        cooldown_waits: list[float] = []
        with patch("mcagent.skills.combat_eval.wait_attack_cooldown", side_effect=lambda _t, s: cooldown_waits.append(s)):
            ok, _steps = _drive_result(escape_hazard_step(ex, state, "flee"), ex)

        self.assertTrue(ok)
        self.assertEqual(w.equip_reqs, [{"item": "stone_sword", "slot": "hand"}, {"item": "shield", "slot": "off-hand"}])
        self.assertEqual(len(w.attack_reqs), 2)
        for req in w.attack_reqs:
            self.assertIsNone(req["interval_ms"], "禁止硬编码 interval_ms=600，必须走武器满蓄原版冷却")
            self.assertFalse(req["blocking_when_swung"], "出刀瞬间必须处于收盾状态")
        self.assertEqual([p["shield"] for p in w.posture_reqs], [True, False])
        self.assertFalse(w.st.get("blocking"))
        self.assertEqual(cooldown_waits, [weapon_cooldown_s("stone_sword")])

    def test_counterattack_abort_always_lowers_shield(self):
        zombie = _real_server_mob("zombie", eid=89, pos=(2.0, 64.0, 0.5), dist=1.5)
        w = _ShieldCombatWorld(
            items={"stone_sword": 1, "shield": 1}, health=18.0, held="stone_sword", offhand="shield",
            entities=[zombie], kill_on_attack=5,
        )
        ex, state = w.session()
        dec = decide_combat_or_flee(ex, state)
        self.assertEqual(dec.action, "fight")

        gen = counterattack_hostile(ex, state, dec)
        step1 = next(gen)
        self.assertEqual(step1["op"], "attack")
        # 推进到下一轮前（或直接中止）时，若已举盾则 close() 必收盾
        with patch("mcagent.skills.combat_eval.wait_attack_cooldown", return_value=None):
            step2 = next(gen)
        self.assertEqual(step2["op"], "attack")
        gen.close()
        self.assertFalse(w.st.get("blocking"))

    def test_16m_outer_skeleton_blocks_unshielded_fight_but_shield_allows_close_skeleton(self):
        # 1) 13 格外有骷髅 + 2 格有僵尸，无盾禁止原地对砍
        w_no_shield = _ShieldCombatWorld(
            items={"stone_sword": 1},
            health=20.0,
            entities=[
                _real_server_mob("zombie", eid=1, pos=(2.5, 64.0, 0.5), dist=2.0),
                _real_server_mob("skeleton", eid=2, pos=(13.5, 64.0, 0.5), dist=13.0),
            ],
        )
        ex1, st1 = w_no_shield.session()
        dec1 = decide_combat_or_flee(ex1, st1)
        self.assertEqual(dec1.action, "flee")
        self.assertIn("16 格", dec1.reason)

        # 2) 有盾 + 铁剑面对 5.8 格落单骷髅：允许持盾反杀
        w_shield = _ShieldCombatWorld(
            items={"iron_sword": 1, "shield": 1},
            health=18.0,
            entities=[_real_server_mob("skeleton", eid=3, pos=(6.3, 64.0, 0.5), dist=5.8)],
        )
        ex2, st2 = w_shield.session()
        dec2 = decide_combat_or_flee(ex2, st2)
        self.assertEqual(dec2.action, "fight")

    def test_flee_from_skeleton_uses_shield_or_places_2_block_barricade(self):
        # Case A: 持盾遇远程骷髅逃离（无天然掩体）-> 先举盾面向骷髅后撤，再收盾拉脱
        skel = _real_server_mob("skeleton", eid=301, pos=(8.5, 64.0, 0.5), dist=8.0)
        w_sh = _ShieldCombatWorld(items={"shield": 1}, health=12.0, entities=[skel])
        ex_sh, st_sh = w_sh.session()
        ok_sh, _ = _drive_result(escape_hazard_step(ex_sh, st_sh, "flee"), ex_sh)
        self.assertTrue(ok_sh)
        self.assertEqual([p["shield"] for p in w_sh.posture_reqs], [True, False])
        self.assertTrue(w_sh.control_reqs and w_sh.control_reqs[0].get("back"))
        self.assertFalse(w_sh.st.get("blocking"))

        # Case B: 无盾、无天然掩体遇骷髅且带圆石 -> 原地朝骷髅方向立 2 格高挡箭墙并满足 verify_done
        w_bar = _ShieldCombatWorld(items={"cobblestone": 6}, health=12.0, entities=[skel])
        ex_bar, st_bar = w_bar.session()
        esc = EmergencyEscape(intent="flee")
        ok_bar, steps_bar = _drive_result(esc.run(ex_bar, st_bar), ex_bar)
        self.assertTrue(ok_bar)
        self.assertEqual([p["pos"] for p in w_bar.place_reqs], [[1, 64, 0], [1, 65, 0]])
        self.assertTrue(all(s.get("skeleton_cover") for s in steps_bar if s.get("op") == "place"))
        self.assertEqual(esc.verify_done(st_bar), "")

    def test_policy_crafts_shield_early_with_1_iron_when_underground_or_night(self):
        reg = {"craft_iron_gear": CraftIronGear}
        planner = RulePlanner(registry=reg, min_verified="offline")

        def make_st(*, iron: int, raw_iron: int = 0, is_day: bool = True, y: float = 64.0, hp: float = 20.0):
            s = State(dataclasses.replace(RuntimeConfig(), status_ttl_s=0.0))
            s.set_status({
                "ready": True, "alive": True, "health": hp, "food": 20, "oxygen": 20,
                "pos": [0.5, y, 0.5], "on_ground": True, "dimension": "overworld", "is_day": is_day,
            })
            items = [
                {"name": "iron_ingot", "count": iron, "slot": 0},
                {"name": "oak_planks", "count": 8, "slot": 1},
            ]
            if raw_iron > 0:
                items.append({"name": "raw_iron", "count": raw_iron, "slot": 2})
            s.set_inventory({"items": items})
            return s

        # 地下仅 1 块铁锭（不够做铁镐）+ 8 木板 -> 立即补盾防身
        dec_ug = planner.choose_proactive_survival(make_st(iron=1, y=40.0))
        self.assertIsNotNone(dec_ug)
        self.assertEqual(dec_ug.skill.name, "craft_iron_gear")
        self.assertEqual(dec_ug.skill.args["item"], "shield")

        # 若已有 3 块铁锭且无铁镐 -> 不抢铁镐配额
        dec_pick = planner.choose_proactive_survival(make_st(iron=3, y=40.0))
        self.assertIsNone(dec_pick)

    def test_pillar_up_retries_once_on_transient_blocked_by_self(self):
        blocks = {(x, 63, z): "stone" for x in range(-3, 4) for z in range(-3, 4)}
        w = ScriptedWorld(pos=(0.5, 64.0, 0.5), blocks=blocks, items={"cobblestone": 5})
        w.st.update(dimension="overworld", health=20, food=20, on_ground=True)

        def place_ok(world, item, pos, **_):
            world.items[item] -= 1
            world.blocks[tuple(pos)] = item
            world.st["pos"] = [0.5, float(pos[1] + 1), 0.5]
            return {"placed": item, "pos": list(pos)}

        w.queue("place", ApiError("blocked_by_self", "still descending"), place_ok, place_ok)
        ex, state = w.session()
        with patch("mcagent.skills.encounter.time.sleep", return_value=None):
            ok, steps = _drive_result(pillar_up(ex, state, height=2), ex)
        self.assertTrue(ok)
        self.assertEqual(w.st["pos"][1], 66.0)
        self.assertEqual([s.get("error_code") for s in steps], ["blocked_by_self", None, None])
        self.assertFalse(w.st.get("jumping"))
        w.assert_exhausted()


if __name__ == "__main__":
    unittest.main()
