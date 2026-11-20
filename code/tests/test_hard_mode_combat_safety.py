"""困难模式战斗战术、1vN 防围殴、近身苦力怕抢占与进食门禁离线单测（verified: offline）。

所有技能与 L2 原语测试均经 `skills.testing.drive()` 驱动，严格校验"一步至多一个阻塞动作"、
姿态（举盾/跳跃）收尾清理与零伪造回执。
"""
from __future__ import annotations

import dataclasses
import math
import time
import unittest
from copy import deepcopy
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.loop import RulePlanner, reset_world_memo
from mcagent.loop.starter_skills import EatFood
from mcagent.runtime import EnvBlocked, Executor, Replan, RuntimeConfig, Safety, SafetyMonitor, State
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.combat import HuntMob
from mcagent.skills.combat_scene import KILL_MOB
from mcagent.skills.encounter import combat_melee, dump_water_crowd_control, pillar_up
from mcagent.skills.escape import escape_hazard_step
from mcagent.skills.safety_escape import EmergencyEscape
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import CountingExecutor, GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches


def _mob(name="zombie", *, identifier=91, pos=(5.5, 64.0, 0.5), health=20, category="hostile"):
    return {
        "name": name,
        "id": identifier,
        "pos": list(pos),
        "dist": round(math.dist([0.5, 64.0, 0.5], pos), 2),
        "type": category,
        "health": health,
    }


def _progress(*ids):
    return {"ready": True, "advancements": [{"id": key, "status": "completed"} for key in ids]}


class HardCombatWorld(ScriptedWorld):
    """支持 /attack(max_hits)、/control(hit/back)、/look、/posture、/place 与 /interact 的离线世界。"""

    def __init__(self, items=None, *, entities=(), pos=(0.5, 64.0, 0.5), blocks=None, held=None, offhand=None):
        default_blocks = {(x, 63, z): "stone" for x in range(-6, 16) for z in range(-6, 6)}
        default_blocks.update({(1, 65, z): "stone" for z in (2, 3)})
        if blocks is not None:
            default_blocks.update(blocks)
        super().__init__(pos=pos, blocks=default_blocks, items=items)
        self.st.update(dimension="overworld", is_day=False, health=20, food=20, on_ground=True)
        self.entities = list(entities)
        self.seen: set[str] = set()
        self.held = held
        self.offhand = offhand

    def status(self, timeout=10):
        base = super().status(timeout)
        if self.offhand:
            base["offhand"] = {"name": self.offhand, "count": 1, "slot": 45}
        if self.held:
            base["held"] = {"name": self.held, "count": 1}
        return base

    def inventory(self, timeout=10):
        snap = super().inventory(timeout)
        if self.held:
            snap["held"] = {"name": self.held, "count": 1}
        if self.offhand:
            snap["offhand"] = {"name": self.offhand, "count": 1, "slot": 45}
            for stack in snap["items"]:
                if stack["name"] == self.offhand:
                    stack["slot"] = 45
        return snap

    def get_entities(self, radius=32, types=None):
        def observe(radius, types):
            return {
                "entities": deepcopy([
                    row for row in self.entities
                    if not types or row["type"] in types or row["name"] in types
                ])
            }
        return self._reply("get_entities", dict(radius=radius, types=types), fallback=observe)

    def advancements(self, status="completed", include_recipes=False):
        return self._reply(
            "advancements",
            dict(status=status, include_recipes=include_recipes),
            fallback=lambda **_: _progress(*sorted(self.seen)),
        )

    def stop(self):
        self.st.update(busy=None, using_item=None)
        return super().stop()

    def equip_expected(self):
        def equip(world, item, slot, **_):
            if slot == "off-hand":
                world.offhand = item
            else:
                world.held = item
            return {"equipped": {"name": item}, "slot": slot}
        self.queue("equip", equip)

    def look(self, yaw=None, pitch=None, *, pos=None, entity_id=None):
        req = {k: v for k, v in dict(yaw=yaw, pitch=pitch, pos=pos, entity_id=entity_id).items() if v is not None}
        return self._reply("look", req, fallback=lambda **_: {"yaw": 0.0, "pitch": 0.0})


def _drive_ret(flow, ex: CountingExecutor, state: State | None = None):
    box = {}

    def _wrap():
        box["ret"] = yield from flow

    steps = drive(_wrap(), ex, state)
    return box.get("ret"), steps


class TestEncounterHardModeTactics(unittest.TestCase):
    def setUp(self):
        self.sleep_patch = patch("mcagent.skills.combat_scene.time.sleep", return_value=None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def test_auto_tactic_kites_creeper_with_max_hits_1_and_backstep(self):
        creeper = _mob("creeper", identifier=77, pos=(3.5, 64.0, 0.5), health=20)
        world = HardCombatWorld({"iron_sword": 1}, entities=[creeper], held="iron_sword")

        def hit_1(w, entity_id, max_hits=None, **_):
            self.assertEqual(max_hits, 1)
            w.entities[0]["health"] = 10
            return {"killed": False, "hits": 1}

        def backstep(w, **req):
            self.assertTrue(req.get("back"))
            self.assertEqual(req.get("ms"), 450)
            return {"moved": 1.8, "pos": list(w.st["pos"])}

        def hit_2(w, entity_id, max_hits=None, **_):
            self.assertEqual(max_hits, 1)
            w.entities = []
            return {"killed": True, "hits": 1}

        world.queue("attack", hit_1, hit_2)
        world.queue("control", backstep)
        ex, state = world.session()

        res, steps = _drive_ret(
            combat_melee(ex, state, creeper, retreat_pos=(-1.5, 64.0, 0.5), tactic="auto"),
            ex,
        )

        self.assertEqual(res["tactic"], "kite")
        self.assertTrue(res["confirmed"])
        self.assertEqual(ex.acts, ["attack", "control", "attack"])
        self.assertEqual([s["op"] for s in steps], ["attack", "control", "attack"])
        world.assert_exhausted()

    def test_kite_with_offhand_shield_blocks_between_hits_and_clears_on_abort(self):
        creeper = _mob("creeper", identifier=78, pos=(3.5, 64.0, 0.5), health=20)
        world = HardCombatWorld({"iron_sword": 1, "shield": 1}, entities=[creeper], held="iron_sword", offhand="shield")

        def hit_1(w, **_):
            w.entities[0]["health"] = 10
            return {"killed": False, "hits": 1}

        world.queue("attack", hit_1)
        world.queue("control", {"moved": 1.5})
        ex, state = world.session()

        flow = combat_melee(ex, state, creeper, retreat_pos=(-1.5, 64.0, 0.5), tactic="auto")
        # Step 1: attack(max_hits=1)
        first = next(flow)
        self.assertEqual(first["op"], "attack")
        # Step 2: shield raises then control(back=True)
        second = next(flow)
        self.assertEqual(second["op"], "control")
        self.assertTrue(world.st.get("blocking"))
        # Abort mid-kite -> finally MUST lower shield
        flow.close()
        self.assertFalse(world.st.get("blocking"))
        posture_calls = [args for op, args in world.calls if op == "posture"]
        self.assertEqual(posture_calls, [{"sneak": None, "shield": True}, {"sneak": None, "shield": False}])

    def test_auto_tactic_bunker_hit_on_pillar_never_calls_attack(self):
        # 站在 3 格高柱顶 y=67，脚下 (0, 66, 0) 为圆石，僵尸在 y=64 正下方 1 格
        blocks = {(0, 64, 0): "cobblestone", (0, 65, 0): "cobblestone", (0, 66, 0): "cobblestone"}
        zombie = _mob("zombie", identifier=88, pos=(1.5, 64.0, 0.5), health=14)
        world = HardCombatWorld(
            {"iron_sword": 1},
            entities=[zombie],
            pos=(0.5, 67.0, 0.5),
            blocks=blocks,
            held="iron_sword",
        )

        def swing_1(w, **req):
            self.assertEqual(req.get("hit"), 1)
            w.entities[0]["health"] = 7
            return {"hits": [{"t_ms": 0, "entity": {"id": 88, "name": "zombie", "dist": 2.6}}]}

        def swing_2(w, **req):
            self.assertEqual(req.get("hit"), 1)
            w.entities = []
            return {"hits": [{"t_ms": 0, "entity": {"id": 88, "name": "zombie", "dist": 2.6}}]}

        world.queue("control", swing_1, swing_2)
        ex, state = world.session()

        res, _ = _drive_ret(combat_melee(ex, state, zombie, retreat_pos=None, tactic="auto"), ex)

        self.assertEqual(res["tactic"], "bunker_hit")
        self.assertTrue(res["receipt"]["killed"])
        self.assertEqual(ex.acts, ["control", "control"])
        self.assertNotIn("attack", ex.acts)
        world.assert_exhausted()

    def test_auto_tactic_bunker_hit_behind_doorway_and_stops_on_consecutive_misses(self):
        # 2 格门框拦截凋灵骷髅：玩家 (0.5, 64, 0.5)，头顶门梁 (1, 66, 0) 石块
        blocks = {(1, 66, 0): "stone"}
        ws = _mob("wither_skeleton", identifier=95, pos=(2.5, 64.0, 0.5), health=20)
        world = HardCombatWorld({"iron_sword": 1}, entities=[ws], blocks=blocks, held="iron_sword")
        # 模拟旧部署不支持 /look -> 自动退回本地 yaw/pitch，且连续两次未命中抛 Replan
        world.queue("look", ApiError("unsupported_api", "no look"), ApiError("unsupported_api", "no look"))
        world.queue(
            "control",
            {"hits": [{"t_ms": 0, "entity": None}]},
            {"hits": [{"t_ms": 0, "entity": None}]},
        )
        ex, state = world.session()

        with self.assertRaisesRegex(Replan, "连续未命中"):
            drive(combat_melee(ex, state, ws, retreat_pos=(-1.5, 64.0, 0.5), tactic="auto"), ex)

        ctrl_calls = [args for op, args in world.calls if op == "control"]
        self.assertEqual(len(ctrl_calls), 2)
        self.assertIn("yaw", ctrl_calls[0])
        self.assertIn("pitch", ctrl_calls[0])
        self.assertEqual(ex.acts, ["control", "control"])
        world.assert_exhausted()

    def test_direct_with_offhand_shield_uses_max_hits_2_and_raises_shield_between_bursts(self):
        skel = _mob("skeleton", identifier=51, pos=(4.5, 64.0, 0.5), health=20)
        world = HardCombatWorld(
            {"iron_sword": 1, "shield": 1},
            entities=[skel],
            held="iron_sword",
            offhand="shield",
        )

        def burst_1(w, max_hits=None, **_):
            self.assertEqual(max_hits, 2)
            w.entities[0]["health"] = 8
            return {"killed": False, "hits": 2}

        def burst_2(w, max_hits=None, **_):
            self.assertEqual(max_hits, 2)
            w.entities = []
            return {"killed": True, "hits": 2}

        world.queue("attack", burst_1, burst_2)
        ex, state = world.session()

        res, _ = _drive_ret(combat_melee(ex, state, skel, retreat_pos=(-1.5, 64.0, 0.5), tactic="auto"), ex)

        self.assertEqual(res["tactic"], "direct")
        self.assertEqual(ex.acts, ["attack", "attack"])
        self.assertFalse(world.st.get("blocking"))
        posture_shields = [args["shield"] for op, args in world.calls if op == "posture"]
        self.assertEqual(posture_shields, [True, False])
        world.assert_exhausted()

    def test_kite_control_unsupported_yields_before_move_to_fallback(self):
        creeper = _mob("creeper", identifier=79, pos=(3.5, 64.0, 0.5), health=20)
        world = HardCombatWorld({"iron_sword": 1}, entities=[creeper], held="iron_sword")

        def hit_1(w, **_):
            w.entities[0]["health"] = 10
            return {"killed": False, "hits": 1}

        def hit_2(w, **_):
            w.entities = []
            return {"killed": True, "hits": 1}

        world.queue("attack", hit_1, hit_2)
        world.queue("control", ApiError("unsupported_api", "legacy gateway"))
        ex, state = world.session()

        res, steps = _drive_ret(
            combat_melee(ex, state, creeper, retreat_pos=(-1.5, 64.0, 0.5), tactic="kite"),
            ex,
        )
        self.assertEqual(res["tactic"], "kite")
        self.assertEqual(ex.acts, ["attack", "control", "move_to", "attack"])
        self.assertEqual(steps[1]["error_code"], "unsupported_api")
        world.assert_exhausted()

    def test_pillar_up_places_3_blocks_with_jump_posture_and_clears_on_abort(self):
        world = HardCombatWorld({"cobblestone": 5}, pos=(0.5, 64.0, 0.5))

        def place_jump(w, item, pos, **_):
            w.items[item] -= 1
            w.blocks[tuple(pos)] = item
            w.st["pos"] = [0.5, float(pos[1] + 1), 0.5]
            return {"placed": item, "pos": list(pos)}

        world.queue("place", place_jump, place_jump, place_jump)
        ex, state = world.session()

        ok, steps = _drive_ret(pillar_up(ex, state, height=3), ex)
        self.assertTrue(ok)
        self.assertEqual(ex.acts, ["place", "place", "place"])
        self.assertEqual(world.st["pos"][1], 67.0)
        self.assertFalse(world.st.get("jumping"))
        posture_jumps = [args.get("jump") for op, args in world.calls if op == "posture"]
        self.assertEqual(posture_jumps, [True, False])

        # 验证中断时同样清理 jump=False
        world2 = HardCombatWorld({"cobblestone": 5}, pos=(0.5, 64.0, 0.5))
        world2.queue("place", place_jump)
        ex2, state2 = world2.session()
        flow = pillar_up(ex2, state2, height=3)
        next(flow)
        self.assertTrue(world2.st.get("jumping"))
        flow.close()
        self.assertFalse(world2.st.get("jumping"))


class TestHuntMobHardMode(unittest.TestCase):
    def setUp(self):
        self.sleep_patch = patch("mcagent.skills.combat_scene.time.sleep", return_value=None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def test_prefers_isolated_spider_over_closer_skeleton_and_skips_paired_or_pack(self):
        # 骷髅在 (7.5, 64, 0.5) 更近（7 格），落单蜘蛛在 (-8.5, 64, 0.5) 较远（9 格），两者均在 6 格警戒圈外
        skel = _mob("skeleton", identifier=10, pos=(7.5, 64.0, 0.5))
        spider = _mob("spider", identifier=20, pos=(-8.5, 64.0, 0.5))
        blocks = {(x, 63, z): "stone" for x in range(-10, 12) for z in range(-4, 5)}
        blocks.update({(-1, 65, 2): "stone", (-2, 65, 2): "stone"})
        world = HardCombatWorld(
            {"iron_sword": 1},
            entities=[skel, spider],
            blocks=blocks,
            held="iron_sword",
        )

        def kill_spider(w, entity_id, **_):
            self.assertEqual(entity_id, 20)  # 必须优先选蜘蛛而非同距/更近的骷髅
            w.entities = [e for e in w.entities if e["id"] != entity_id]
            w.seen.add(KILL_MOB)
            return {"killed": True, "hits": 2}

        world.queue("attack", kill_spider)
        ex, state = world.session()
        drive(HuntMob(), ex, state)
        self.assertEqual(ex.acts, ["attack"])
        world.assert_exhausted()

        # 两只敌怪相距 <=6 格时，拒绝主动接战（EnvBlocked）
        paired_world = HardCombatWorld(
            {"iron_sword": 1},
            entities=[
                _mob("zombie", identifier=31, pos=(5.5, 64.0, 0.5)),
                _mob("zombie", identifier=32, pos=(8.5, 64.0, 0.5)),  # 距目标 3 格 <= 6 格
            ],
            held="iron_sword",
        )
        ex2, state2 = paired_world.session()
        with self.assertRaises(EnvBlocked):
            drive(HuntMob(), ex2, state2)
        self.assertEqual(ex2.acts, [])

    def test_crowd_ambush_builds_pillar_then_bunker_hits_without_attack(self):
        z1 = _mob("zombie", identifier=101, pos=(5.5, 64.0, 0.5), health=14)
        z2 = _mob("zombie", identifier=102, pos=(2.5, 64.0, 0.5), health=20)
        world = HardCombatWorld({"iron_sword": 1, "cobblestone": 6}, entities=[z1])

        def equip_and_ambush(w, item, slot, **_):
            w.held = item
            # 装备剑瞬间第二只僵尸冲到贴脸，同时 z1 逼近到 2 格内形成 1v2
            w.entities = [
                _mob("zombie", identifier=101, pos=(1.5, 64.0, 0.5), health=14),
                z2,
            ]
            return {"equipped": {"name": item}, "slot": slot}

        def place_pillar(w, item, pos, **_):
            w.items[item] -= 1
            w.blocks[tuple(pos)] = item
            w.st["pos"] = [0.5, float(pos[1] + 1), 0.5]
            return {"placed": item, "pos": list(pos)}

        def bunker_kill(w, **req):
            self.assertEqual(req.get("hit"), 1)
            w.entities = [e for e in w.entities if e["id"] != 101]
            w.seen.add(KILL_MOB)
            return {"hits": [{"t_ms": 0, "entity": {"id": 101, "name": "zombie", "dist": 2.7}}]}

        world.queue("equip", equip_and_ambush)
        world.queue("place", place_pillar, place_pillar, place_pillar)
        world.queue("control", bunker_kill)
        ex, state = world.session()

        steps = drive(HuntMob(), ex, state)
        self.assertEqual(ex.acts, ["equip", "place", "place", "place", "control"])
        self.assertNotIn("attack", ex.acts)
        defense = [s for s in steps if s and s["op"] == "combat_crowd_defense"]
        self.assertEqual(len(defense), 1)
        self.assertEqual(defense[0]["result"]["method"], "pillar_up")
        world.assert_exhausted()

    def test_crowd_ambush_falls_back_to_water_bucket_and_shoot_when_no_blocks(self):
        z1 = _mob("zombie", identifier=201, pos=(5.5, 64.0, 0.5), health=14)
        z2 = _mob("zombie", identifier=202, pos=(3.0, 64.0, 1.0), health=20)
        world = HardCombatWorld(
            {"iron_sword": 1, "water_bucket": 1, "bow": 1, "arrow": 4},
            entities=[z1],
        )

        def equip_and_ambush(w, item, slot, **_):
            w.held = item
            w.entities = [
                _mob("zombie", identifier=201, pos=(3.5, 64.0, 0.5), health=14),
                z2,
            ]
            return {"equipped": {"name": item}, "slot": slot}

        def dump_water(w, pos, item, **_):
            self.assertEqual(item, "water_bucket")
            self.assertEqual(pos, [0, 63, 0])
            w.items["water_bucket"] -= 1
            w.items["bucket"] = 1
            return {"interacted": True}

        def bow_kill(w, **req):
            self.assertEqual(req["weapon"], "bow")
            w.items["arrow"] -= 1
            w.entities = [e for e in w.entities if e["id"] != 201]
            w.seen.add(KILL_MOB)
            return {"fired": True, "used": {"arrow": 1}}

        world.queue("equip", equip_and_ambush)
        world.queue("interact", dump_water)
        world.queue("shoot", bow_kill)
        ex, state = world.session()

        steps = drive(HuntMob(), ex, state)
        self.assertEqual(ex.acts, ["equip", "interact", "shoot"])
        defense = [s for s in steps if s and s["op"] == "combat_crowd_defense"]
        self.assertEqual(defense[0]["result"]["method"], "water_bucket")
        world.assert_exhausted()

    def test_explicit_creeper_hunt_equips_shield_and_kites(self):
        creeper = _mob("creeper", identifier=301, pos=(5.5, 64.0, 0.5), health=20)
        world = HardCombatWorld({"iron_sword": 1, "shield": 1}, entities=[creeper])
        world.equip_expected()
        world.equip_expected()

        def hit_1(w, max_hits=None, **_):
            self.assertEqual(max_hits, 1)
            w.entities[0]["health"] = 10
            return {"killed": False, "hits": 1}

        def hit_2(w, max_hits=None, **_):
            self.assertEqual(max_hits, 1)
            w.entities = []
            w.seen.add(KILL_MOB)
            return {"killed": True, "hits": 1}

        world.queue("attack", hit_1, hit_2)
        world.queue("control", {"moved": 1.6})
        ex, state = world.session()

        steps = drive(HuntMob(target="creeper"), ex, state)
        self.assertEqual(ex.acts, ["equip", "equip", "attack", "control", "attack"])
        self.assertFalse(world.st.get("blocking"))
        kill_rec = next(s for s in steps if s and s["op"] == "combat_kill_receipt")
        self.assertEqual(kill_rec["tactic"], "kite")
        world.assert_exhausted()

    def test_crowd_ambush_water_bucket_falls_back_to_kite_without_bow(self):
        z1 = _mob("zombie", identifier=401, pos=(5.5, 64.0, 0.5), health=14)
        z2 = _mob("zombie", identifier=402, pos=(3.0, 64.0, 1.0), health=20)
        world = HardCombatWorld({"iron_sword": 1, "water_bucket": 1}, entities=[z1])

        def equip_and_ambush(w, item, slot, **_):
            w.held = item
            w.entities = [
                _mob("zombie", identifier=401, pos=(3.5, 64.0, 0.5), health=14),
                z2,
            ]
            return {"equipped": {"name": item}, "slot": slot}

        def dump_water(w, pos, item, **_):
            w.items["water_bucket"] -= 1
            w.items["bucket"] = 1
            return {"interacted": True}

        def kite_kill(w, max_hits=None, **_):
            self.assertEqual(max_hits, 1)
            w.entities = [e for e in w.entities if e["id"] != 401]
            w.seen.add(KILL_MOB)
            return {"killed": True, "hits": 1}

        world.queue("equip", equip_and_ambush)
        world.queue("interact", dump_water)
        world.queue("attack", kite_kill)
        ex, state = world.session()

        steps = drive(HuntMob(), ex, state)
        self.assertEqual(ex.acts, ["equip", "interact", "attack"])
        defense = [s for s in steps if s and s["op"] == "combat_crowd_defense"]
        self.assertEqual(defense[0]["result"]["method"], "water_bucket")
        world.assert_exhausted()


class _FakeSafetyClient:
    def __init__(self):
        self.st = {
            "ready": True,
            "alive": True,
            "health": 20,
            "food": 20,
            "pos": [0.5, 64.0, 0.5],
            "on_ground": True,
        }
        self.inv = {"items": []}
        self.stops = 0

    def status(self):
        return dict(self.st)

    def inventory(self):
        return deepcopy(self.inv)

    def events(self, **kwargs):
        return {"events": [], "last_seq": kwargs.get("since", 0)}

    def stop(self):
        self.stops += 1
        return {"stopped": True, "released": True}

    def get_block(self, pos):
        return {"name": "stone"}


class TestHardModeSafetyAndEating(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def test_close_creeper_under_5m_is_urgent_even_in_safe_mode(self):
        client = _FakeSafetyClient()
        state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
        ex = Executor(client, state, log=lambda *_: None)
        mon = SafetyMonitor(client, state, ex)

        # 距离 5.5 格：普通 soft flee，不急停
        client.st["nearest_hostile"] = {"name": "creeper", "dist": 5.5}
        state.status_at = float("-inf")
        mon.tick()
        self.assertEqual(client.stops, 0)
        self.assertEqual(state.safety_intent["kind"], "flee")
        self.assertEqual(state.safety_intent["severity"], "soft")

        # 距离 4.5 格（<5 格），即使处于 safe_mode 也必须立刻 urgent /stop
        state.safe_mode_until = time.monotonic() + 60.0
        client.st["nearest_hostile"] = {"name": "creeper", "dist": 4.5}
        state.status_at = float("-inf")
        mon.tick()
        self.assertEqual(client.stops, 1)
        self.assertEqual(state.safety_intent["kind"], "flee")
        self.assertEqual(state.safety_intent["severity"], "urgent")
        self.assertTrue(state.safety_intent.get("creeper"))

    def test_low_food_risk_interval_and_starvation_preemption(self):
        client = _FakeSafetyClient()
        state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
        ex = Executor(client, state, log=lambda *_: None)
        mon = SafetyMonitor(client, state, ex)

        # food=5（<=food_warn=6）：进入高速风险轮询，发布 soft eat
        client.st["food"] = 5
        state.set_inventory({"items": [{"name": "bread", "count": 2, "slot": 0}]})
        state.status_at = float("-inf")
        mon.tick()
        self.assertEqual(mon.next_interval(), 1.0)
        self.assertEqual(client.stops, 0)
        self.assertEqual(state.safety_intent["kind"], "eat")
        self.assertEqual(state.safety_intent["severity"], "soft")

        # 战斗期间 food=5 且有食物 -> 升级 urgent 抢占
        ex.active_action = "attack"
        state.status_at = float("-inf")
        mon.tick()
        self.assertEqual(client.stops, 1)

        # 非战斗时 food=2（<=food_crit）且无食物 -> 保持 soft 避免空包死循环急停
        ex.active_action = None
        mon._triggered.clear()
        mon._active.clear()
        client.stops = 0
        state.set_inventory({"items": []})
        client.st["food"] = 2
        state.status_at = float("-inf")
        mon.tick()
        self.assertEqual(client.stops, 0)
        self.assertEqual(state.safety_intent["severity"], "soft")

        # 非战斗时 food=2 且有面包 -> 立即 urgent /stop 防困难模式饿死
        state.set_inventory({"items": [{"name": "bread", "count": 1, "slot": 0}]})
        state.status_at = float("-inf")
        mon.tick()
        self.assertEqual(client.stops, 1)
        self.assertEqual(state.safety_intent["severity"], "urgent")

    def test_policy_prioritizes_close_creeper_and_lava_above_eat_food(self):
        reg = {"emergency_escape": EmergencyEscape, "eat_food": EatFood}
        planner = RulePlanner(registry=reg, min_verified="offline")

        def make_state(**overrides):
            s = State(dataclasses.replace(RuntimeConfig(), status_ttl_s=0.0))
            st = {
                "ready": True,
                "alive": True,
                "health": 16,
                "food": 10,
                "oxygen": 20,
                "pos": [0.5, 64.0, 0.5],
                "on_ground": True,
                "in_water": False,
                "submerged": False,
                "in_lava": False,
                "on_fire": False,
                "dimension": "overworld",
            }
            st.update(overrides)
            s.set_status(st)
            s.set_inventory({"items": [{"name": "bread", "count": 3, "slot": 0}]})
            return s

        # 近身苦力怕 (<5 格) + 低饱食度：必须先逃，绝不站桩吃面包
        creeper_st = make_state(nearest_hostile={"name": "creeper", "dist": 3.5, "pos": [4.0, 64.0, 0.5]})
        dec_creeper = planner.choose(creeper_st)
        self.assertEqual(dec_creeper.skill.name, "emergency_escape")
        self.assertEqual(dec_creeper.skill.args["intent"], "flee")

        # 岩浆中 + 低饱食度：必须先脱岩浆
        lava_st = make_state(in_lava=True)
        dec_lava = planner.choose(lava_st)
        self.assertEqual(dec_lava.skill.name, "emergency_escape")
        self.assertEqual(dec_lava.skill.args["intent"], "douse")

        # 安全状态下饱食度 <=14 或 掉血且饱食<20 或 收到 eat 意图 -> 立刻吃东西
        for st_case in (
            make_state(health=20, food=14),
            make_state(health=17, food=19),
            make_state(health=20, food=18),
        ):
            if st_case.st["food"] == 18:
                st_case.set_safety_intent({"kind": "eat", "why": "low food", "severity": "soft"})
            dec_eat = planner.choose(st_case)
            self.assertEqual(dec_eat.skill.name, "eat_food")

    def test_flee_close_creeper_shields_backsteps_lowers_and_verifies_7m_clearance(self):
        world = HardCombatWorld({"shield": 1}, pos=(0.5, 64.0, 0.5))
        world.st["nearest_hostile"] = {"name": "creeper", "pos": [3.5, 64.0, 0.5], "dist": 3.0}

        def backstep(w, **req):
            self.assertTrue(req.get("back"))
            self.assertEqual(req.get("ms"), 500)
            self.assertTrue(w.st.get("blocking"))  # 后退瞬间盾牌处于举起状态
            w.st["pos"] = [-1.5, 64.0, 0.5]
            w.st["nearest_hostile"] = {"name": "creeper", "pos": [3.5, 64.0, 0.5], "dist": 5.0}
            return {"moved": 2.0}

        def sprint_away(w, **req):
            self.assertFalse(w.st.get("blocking"))  # 奔跑前已收盾
            w.st["pos"] = [-9.5, 64.0, 0.5]
            w.st["nearest_hostile"] = {"name": "creeper", "pos": [3.5, 64.0, 0.5], "dist": 13.0}
            return {"pos": list(w.st["pos"])}

        world.queue("control", backstep)
        world.queue("move_to", sprint_away)
        ex, state = world.session()

        ok, _ = _drive_ret(escape_hazard_step(ex, state, "flee"), ex)
        self.assertTrue(ok)
        self.assertEqual(ex.acts, ["control", "move_to"])
        self.assertFalse(world.st.get("blocking"))
        world.assert_exhausted()

        # EmergencyEscape.verify_done 对苦力怕要求 >= 7.0 格
        esc = EmergencyEscape(intent="flee")
        esc._start_pos = (0.5, 64.0, 0.5)
        state.set_status({
            "pos": [1.5, 64.0, 0.5],
            "nearest_hostile": {"name": "creeper", "dist": 6.2},
        })
        self.assertTrue(esc.verify_done(state))
        state.set_status({
            "pos": [1.5, 64.0, 0.5],
            "nearest_hostile": {"name": "creeper", "dist": 7.2},
        })
        self.assertEqual(esc.verify_done(state), "")

    def test_spawn_camp_and_safe_mode_live_hostile_triggers_flee_immediately(self):
        reg = {"emergency_escape": EmergencyEscape, "eat_food": EatFood}
        planner = RulePlanner(registry=reg, min_verified="offline")

        # 1) Tick-0 刚出生 monitor 尚未刷出 safety_intent，僵尸已在 0.5 格贴脸 -> 必须立刻逃离
        st_spawn = State(dataclasses.replace(RuntimeConfig(), status_ttl_s=0.0))
        st_spawn.set_status({
            "ready": True, "alive": True, "health": 20, "food": 20, "oxygen": 20,
            "pos": [0.5, 64.0, 0.5], "on_ground": True, "dimension": "overworld",
            "nearest_hostile": {"id": 1, "name": "zombie", "dist": 0.5, "pos": [1.0, 64.0, 0.5]},
        })
        st_spawn.set_inventory({"items": [{"name": "bread", "count": 2, "slot": 0}]})
        dec_spawn = planner.choose(st_spawn)
        self.assertEqual(dec_spawn.skill.name, "emergency_escape")
        self.assertEqual(dec_spawn.skill.args["intent"], "flee")

        # 2) safe_mode 期间若真有僵尸守尸 (<6 格) -> 绝不原地发呆吃第 3 死
        st_spawn.safe_mode_until = time.monotonic() + 60.0
        dec_safe_camp = planner.choose(st_spawn)
        self.assertEqual(dec_safe_camp.skill.name, "emergency_escape")
        self.assertEqual(dec_safe_camp.skill.args["intent"], "flee")

        # 3) 贴脸僵尸 (<=2.5 格) + 低饱食度 -> 先逃离再吃东西
        st_spawn.safe_mode_until = 0.0
        st_spawn.st["food"] = 10
        dec_point_blank = planner.choose(st_spawn)
        self.assertEqual(dec_point_blank.skill.name, "emergency_escape")
        self.assertEqual(dec_point_blank.skill.args["intent"], "flee")

    def test_monitor_session_error_disables_async_cache_and_throttles_logs(self):
        from mcagent.loop.engine import LoopConfig, MainLoop

        class _DisconnClient(_FakeSafetyClient):
            def status(self):
                raise ApiError("not_connected", "Bot is not connected")

        logs: list[str] = []
        client = _DisconnClient()
        state = State(dataclasses.replace(RuntimeConfig(), status_ttl_s=0.0))
        state._status_booted = True
        state._events_booted = True
        ex = Executor(client, state, log=logs.append)
        mon = SafetyMonitor(client, state, ex, continuous=True)

        # 模拟连续 3 拍 not_connected：只记 1 条日志，且立刻置位 session_error
        calls = 0

        def stop_after_three(_wait):
            nonlocal calls
            calls += 1
            if calls >= 3:
                mon._stop_ev.set()

        with patch.object(mon._stop_ev, "wait", side_effect=stop_after_three) as wait_mock:
            mon._loop()

        self.assertEqual(mon.session_error, "not_connected")
        self.assertEqual(len(logs), 1)
        self.assertGreaterEqual(wait_mock.call_args_list[0].args[0], 3.0)
        loop = MainLoop(client, state, ex, planner=RulePlanner(registry={}), config=LoopConfig())
        self.assertFalse(loop._use_async_observation(mon))

    def test_flee_uses_stop_on_death_repulsion_vector_and_point_blank_sprint_burst(self):
        # 真实服务端 /status.nearest_hostile 无 pos，且僵尸在 +X (1.5, 64, 0.5) 贴脸 1.0 格
        world = HardCombatWorld(
            pos=(0.5, 64.0, 0.5),
            entities=[_mob("zombie", identifier=99, pos=(1.5, 64.0, 0.5))],
        )
        world.st["nearest_hostile"] = {"id": 99, "name": "zombie", "dist": 1.0}

        def burst_away(w, **req):
            self.assertEqual(req.get("stop_on"), ("death",))
            self.assertTrue(req.get("forward"))
            self.assertTrue(req.get("sprint"))
            self.assertTrue(req.get("jump"))
            # 敌人在 +X，逃离方向应为 -X (yaw = +pi/2)
            self.assertAlmostEqual(req["yaw"], math.pi / 2, places=3)
            w.st["pos"] = [-5.5, 64.0, 0.5]
            w.st["nearest_hostile"] = {"id": 99, "name": "zombie", "dist": 7.0}
            return {"moved": 6.0}

        def move_farther(w, **req):
            self.assertEqual(req.get("stop_on"), ("death",))
            self.assertLess(req["pos"][0], -10.0)
            w.st["pos"] = [-16.0, 64.0, 0.5]
            w.st["nearest_hostile"] = None
            return {"pos": list(w.st["pos"])}

        world.queue("control", burst_away)
        world.queue("move_to", move_farther)
        ex, state = world.session()

        ok, _ = _drive_ret(escape_hazard_step(ex, state, "flee"), ex)
        self.assertTrue(ok)
        self.assertEqual(ex.acts, ["control", "move_to"])
        world.assert_exhausted()

    def test_mine_step_collect_only_ores_and_accepts_natural_landing(self):
        from mcagent.skills.mining import mine_step

        # 复现 INC-003：挖完最后一块石头时角色若已落入目标台阶格 (1, 63, 0)，应直接确认到达而非误报 Replan
        blocks = {(1, 62, 0): "stone", (1, 63, 0): "iron_ore", (1, 64, 0): "stone", (1, 65, 0): "stone"}
        world = HardCombatWorld({"stone_pickaxe": 1}, pos=(0.5, 64.0, 0.5), blocks=blocks, held="stone_pickaxe")
        collect_flags: list[tuple[list[int], bool]] = []

        def dig_cb(w, pos, collect=True, **_):
            collect_flags.append((list(pos), bool(collect)))
            w.blocks.pop(tuple(pos), None)
            if list(pos) == [1, 63, 0]:
                # 模拟 collect=True 捡铁矿后自然落到目标台阶格 (1, 63, 0)
                w.st["pos"] = [1.5, 63.0, 0.5]
                w.st["on_ground"] = True
            return {"dug": "stone"}

        world.queue("dig", dig_cb, dig_cb, dig_cb)
        ex, state = world.session()

        res, _ = _drive_ret(mine_step(ex, state, (1, 63, 0)), ex)
        self.assertEqual(res, ([1.5, 63.0, 0.5], None))
        # 非矿石块 collect=False，矿石块 collect=True
        self.assertEqual(collect_flags, [([1, 65, 0], False), ([1, 64, 0], False), ([1, 63, 0], True)])
        self.assertEqual(ex.acts, ["dig", "dig", "dig"])
        world.assert_exhausted()

    def test_safety_monitor_suppresses_self_preemption_for_active_flee_but_keeps_lava(self):
        # 复现 INC-010：EmergencyEscape(flee) 运行期间遇近身苦力怕不得自我 /stop 抢占，但遇岩浆仍须立即抢占
        client = _FakeSafetyClient()
        state = State(dataclasses.replace(RuntimeConfig(), stop_cooldown_s=10))
        ex = Executor(client, state, log=lambda *_: None)
        mon = SafetyMonitor(client, state, ex)

        state.set_active_escape("flee")
        client.st["nearest_hostile"] = {"name": "creeper", "dist": 3.5}
        state.status_at = float("-inf")
        mon.tick()
        self.assertEqual(client.stops, 0)  # 同类 flee 不自打断

        client.st["in_lava"] = True
        state.status_at = float("-inf")
        mon.tick()
        self.assertEqual(client.stops, 1)  # 跨类致命险情（岩浆）仍立即抢占
        state.set_active_escape(None)

    def test_flee_rotates_orthogonal_yaw_when_wall_blocks_primary_burst(self):
        # 复现 INC-009：主方向贴脸疾跑撞墙 (moved < 1.5) 且 move_to path_not_found 后，旋转 ±90° 而非重撞同一堵墙
        blocks = {(x, 63, z): "stone" for x in range(-4, 5) for z in range(-4, 5)}
        world = HardCombatWorld(pos=(0.5, 64.0, 0.5), blocks=blocks, entities=[_mob("zombie", identifier=7, pos=(1.5, 64.0, 0.5))])
        world.st["nearest_hostile"] = {"id": 7, "name": "zombie", "dist": 1.0}
        seen_yaws: list[float] = []

        def burst_hit_wall(w, **req):
            seen_yaws.append(float(req["yaw"]))
            # 只移动 0.4 格 -> 视为撞墙
            w.st["pos"] = [0.1, 64.0, 0.5]
            return {"moved": 0.4}

        def retry_ortho(w, **req):
            seen_yaws.append(float(req["yaw"]))
            w.st["pos"] = [0.1, 64.0, -5.5]
            w.st["nearest_hostile"] = None
            return {"moved": 6.0}

        world.queue("control", burst_hit_wall, retry_ortho)
        world.queue("move_to", ApiError("path_not_found", "wall"))
        ex, state = world.session()

        ok, _ = _drive_ret(escape_hazard_step(ex, state, "flee"), ex)
        self.assertTrue(ok)
        self.assertEqual(len(seen_yaws), 2)
        # 第二次逃生角度必须与第一次撞墙角度正交（差约 pi/2）
        delta = abs(math.atan2(math.sin(seen_yaws[1] - seen_yaws[0]), math.cos(seen_yaws[1] - seen_yaws[0])))
        self.assertAlmostEqual(delta, math.pi / 2, places=2)
        world.assert_exhausted()


if __name__ == "__main__":
    unittest.main()
