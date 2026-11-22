"""离线的战斗夹具。世界变化由用例的回执明确安排。"""
import unittest
from copy import deepcopy
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.runtime import EnvBlocked, Replan, State
from mcagent.skills import make
from mcagent.skills.combat import DeflectSkeletonArrow, FireCrossbow, HuntMob, ShootMob, _kill_criteria
from mcagent.skills.combat_scene import DEFLECT, KILL_MOB, OL_BETSY, SHOOT_ARROW
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import drive


def mob(name="skeleton", *, identifier=91, pos=(5.5, 64, 0.5), category="hostile"):
    return {"name": name, "id": identifier, "pos": list(pos), "dist": 5,
            "type": category, "health": 16}


class CombatWorld(ScriptedWorld):
    def __init__(self, items=None, *, entities=(), held=None, offhand=None):
        blocks = {(x, 63, z): "stone" for x in range(-4, 16) for z in range(-4, 5)}
        blocks.update({(1, 65, z): "stone" for z in (2, 3)})
        super().__init__(blocks=blocks, items=items)
        self.st.update(dimension="overworld", is_day=False)
        self.entities = list(entities)
        self.seen = set()
        self.held = held
        self.offhand = offhand
        self.visible_blocks = []

    def inventory(self, timeout=10):
        snapshot = super().inventory(timeout)
        if self.held:
            snapshot["held"] = {"name": self.held, "count": 1}
        if self.offhand:
            snapshot["offhand"] = {"name": self.offhand, "count": 1, "slot": 45}
            for stack in snapshot["items"]:
                if stack["name"] == self.offhand:
                    stack["slot"] = 45
        return snapshot

    def get_entities(self, radius=32, types=None):
        def observe(radius, types):
            return {"entities": deepcopy([row for row in self.entities
                                          if not types or row["type"] in types or row["name"] in types])}
        return self._reply("get_entities", dict(radius=radius, types=types), fallback=observe)

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None):
        return {"blocks": [{"pos": list(pos), "name": self.blocks[pos]}
                           for pos in self.visible_blocks if not types or self.blocks[pos] in types][:limit]}

    def advancements(self, status="completed", include_recipes=False):
        return self._reply("advancements", dict(status=status, include_recipes=include_recipes),
                           fallback=lambda **_: _progress(*sorted(self.seen)))

    def stops(self):
        return [op for op, _ in self.calls if op == "stop"]

    def stop(self):
        self.st.update(busy=None, using_item=None)
        return super().stop()

    def equip_expected(self):
        def equip(world, item, slot, **_):
            if slot == "off-hand":
                world.offhand = item
            else:
                world.held = item
            return {"equipped": {"name": item}}
        self.queue("equip", equip)

    def shot(self, ammo="arrow", *, progress=(), spent=1, using=False):
        def shoot(world, **request):
            if spent:
                world.items[ammo] -= spent
            world.seen.update(progress)
            if using:
                world.st["using_item"] = {"name": request["weapon"]}
            return {"fired": True, "used": {ammo: spent}, "aim": {"predicted_miss": 0.0}}
        self.queue("shoot", shoot)

    def attack_result(self, *, progress=(), loot=None, killed=True, busy=False):
        def attack(world, entity_id, **_):
            if killed:
                world.entities = [row for row in world.entities if row["id"] != entity_id]
            world.seen.update(progress)
            for name, count in (loot or {}).items():
                world.items[name] = world.items.get(name, 0) + count
            if busy:
                world.st["busy"] = "attack"
            return {"killed": True, "hits": 1}
        self.queue("attack", attack)

    def shield_pulse(self, *, progress=(), damaged=False, disappear=False, using=False):
        def use(world, **_):
            world.st["blocking"] = True
            if using:
                world.st["using_item"] = {"name": "shield"}
            if damaged:
                world.st["health"] -= 1
            if disappear:
                world.entities = []
            world.seen.update(progress)
            return {"used": "shield", "blocked_ms": 1100}
        self.queue("use_item", use)
        if "posture" not in self._replies:
            def lower(world, shield=None, **_):
                world.st["blocking"] = bool(shield)
                return {"blocking": world.st["blocking"]}
            self.queue("posture", lower)

    def workbench(self):
        table = (0, 64, -2)
        self.blocks[table] = "crafting_table"
        self.visible_blocks.append(table)

    def craft_batch(self, product, ingredients, output=1):
        def craft(world, item, count, **_):
            if (item, count) != (product, 1):
                raise AssertionError(f"未预期的配方请求 {item=} {count=}")
            for name, quantity in ingredients.items():
                world.items[name] -= quantity
            world.items[product] = world.items.get(product, 0) + output
            return {"crafted": item, "gained": output}
        self.queue("craft", craft)


def _progress(*ids):
    return {"ready": True, "advancements": [{"id": key, "status": "completed"} for key in ids]}


class CombatTestCase(unittest.TestCase):
    def setUp(self):
        clock = patch("mcagent.skills.combat_scene.time.sleep", return_value=None)
        clock.start()
        self.addCleanup(clock.stop)

    def run_skill(self, world, skill):
        ex, state = world.session()
        records = drive(skill, ex, state)
        world.assert_exhausted()
        return ex, state, records

    def assert_env(self, world, skill, missing, wake="chunk_moved"):
        ex, state = world.session()
        with self.assertRaises(EnvBlocked) as caught:
            drive(skill, ex, state)
        self.assertEqual(caught.exception.missing_predicate, missing)
        self.assertIn("20 格", caught.exception.searched_volume)
        self.assertEqual(caught.exception.wake_on, wake)
        self.assertEqual(ex.acts, [])

    def assert_replan(self, world, skill, error, actions=()):
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, error):
            drive(skill, ex, state)
        self.assertEqual(ex.acts, list(actions))
        world.assert_exhausted()


def progress_records(records):
    return [row["result"] for row in records if row["op"] == "combat_progress"]


class HuntMobTests(CombatTestCase):
    def test_criteria_loaded_and_registry(self):
        self.assertEqual(len(_kill_criteria()), 41)
        self.assertIn("parched", _kill_criteria())
        self.assertNotIn("cow", _kill_criteria())
        self.assertEqual(make("hunt_mob").verified, "live")

    def test_normal_melee_equip_then_reward(self):
        world = CombatWorld({"stone_sword": 1}, entities=[mob("spider")])
        world.equip_expected()
        world.attack_result(progress=(KILL_MOB,), loot={"string": 2})
        ex, state, records = self.run_skill(world, HuntMob())
        self.assertEqual(ex.acts, ["equip", "attack"])
        self.assertEqual(state.count("string"), 2)
        self.assertEqual(progress_records(records), [{KILL_MOB: True}])
        attack = next(args for op, args in world.calls if op == "attack")
        self.assertEqual((attack["entity_id"], attack["stop_on"]), (91, ("death",)))

    def test_missing_sword_and_invalid_species(self):
        self.assert_replan(CombatWorld(), HuntMob(), "近战剑")
        self.assert_replan(CombatWorld({"stone_sword": 1}), HuntMob(target="cow"), "不属于")

    def test_no_default_creeper_and_daytime_parched_eligible(self):
        world = CombatWorld({"stone_sword": 1}, entities=[mob("creeper")], held="stone_sword")
        world.st["is_day"] = True
        self.assert_env(world, HuntMob(), "safe_hostile:any", "night")
        world.entities = [mob("parched")]
        world.attack_result(progress=(KILL_MOB,))
        ex, _, _ = self.run_skill(world, HuntMob(target="parched"))
        self.assertEqual(ex.acts, ["attack"])

    def test_kill_receipt_with_live_mob_fails_and_retreats(self):
        world = CombatWorld({"stone_sword": 1}, entities=[mob()], held="stone_sword")
        world.attack_result(killed=False)
        self.assert_replan(world, HuntMob(), "未确认击杀", ("attack", "move_to"))

    def test_completed_kill_shortcuts_and_repeatable_loot_uses_stock_threshold(self):
        world = CombatWorld({"stone_sword": 1, "string": 2}, entities=[mob("spider")], held="stone_sword")
        world.seen.add(KILL_MOB)
        self.assertFalse(self.run_skill(world, HuntMob())[0].acts)
        world.attack_result(loot={"string": 2})
        args = dict(target="spider", drop="string", count=4)
        ex, state, records = self.run_skill(world, HuntMob(**args))
        self.assertEqual(ex.acts, ["attack"])
        self.assertEqual(state.count("string"), 4)
        self.assertFalse(progress_records(records))
        self.assertFalse(self.run_skill(world, HuntMob(**args))[0].acts)

    def test_pickup_is_measured_separately(self):
        world = CombatWorld({"stone_sword": 1}, entities=[mob("spider")], held="stone_sword")

        def kill(world, **_):
            world.entities = [{"id": 12, "name": "item", "type": "item",
                               "pos": [0.5, 64, -1.5], "item": "stringx2"}]
            return {"killed": True}

        def pickup(world, **request):
            world.st["pos"] = list(request["pos"])
            world.items["string"] = 2
            return {"pos": world.st["pos"]}

        world.queue("attack", kill)
        world.queue("move_to", pickup)
        ex, _, records = self.run_skill(world, HuntMob(target="spider", drop="string", count=2))
        self.assertEqual(ex.acts, ["attack", "move_to"])
        self.assertEqual([row["result"] for row in records if row["op"] == "combat_drops"], [{}, {"string": 2}])

    def test_abort_stops_observed_leftover_pursuit(self):
        world = CombatWorld({"stone_sword": 1}, entities=[mob()], held="stone_sword")
        world.attack_result(busy=True)
        ex, state = world.session()
        skill = HuntMob()
        self.assertFalse(skill.step(ex, state))
        skill.abort()
        self.assertIsNone(world.st["busy"])
        self.assertEqual(ex.acts, ["attack"])

    def test_attack_no_such_entity_stalled_and_interrupted_by_stop(self):
        for err, expected in (
            (ApiError("no_such_entity", "entity 91 not visible"), "no_such_entity"),
            (ApiError("stalled", "stuck on boat"), "stalled"),
        ):
            with self.subTest(code=err.code):
                world = CombatWorld({"stone_sword": 1}, entities=[mob()], held="stone_sword")
                world.queue("attack", err)
                self.assert_replan(world, HuntMob(), expected, ("attack", "move_to"))

        stop_world = CombatWorld({"stone_sword": 1}, entities=[mob()], held="stone_sword")
        stop_world.queue("attack", {"killed": False, "hits": 2, "interrupted_by": "stop"})
        self.assert_replan(stop_world, HuntMob(), "interrupted_by=stop", ("attack",))


class DeflectTests(CombatTestCase):
    def test_daytime_parched_equips_offhand_and_releases(self):
        world = CombatWorld({"shield": 1}, entities=[mob("parched")])
        world.st["is_day"] = True
        world.equip_expected()
        world.shield_pulse(progress=(DEFLECT,))
        ex, _, records = self.run_skill(world, DeflectSkeletonArrow())
        self.assertEqual(ex.acts, ["equip", "use_item"])
        request = next(args for op, args in world.calls if op == "use_item")
        self.assertEqual((request["target"], request["source"], request["stop_on"]),
                         ({"entity_id": 91}, "off-hand", ("hurt", "death")))
        self.assertFalse(world.st.get("blocking"))
        self.assertEqual(progress_records(records), [{DEFLECT: True}])

    def test_missing_shield_and_no_skeleton(self):
        self.assert_replan(CombatWorld(), DeflectSkeletonArrow(), "战斗备料还缺")
        world = CombatWorld({"shield": 1}, entities=[mob("zombie")])
        world.st["is_day"] = True
        self.assert_env(world, DeflectSkeletonArrow(), "safe_skeleton_arrow_lane", "night")

    def test_success_receipt_cannot_prove_block_and_wait_is_bounded(self):
        world = CombatWorld({"shield": 1}, entities=[mob()], offhand="shield")
        for _ in range(3):
            world.shield_pulse()
        self.assert_replan(world, DeflectSkeletonArrow(), "3 次上限", ("use_item", "use_item", "use_item", "move_to"))

    def test_damage_or_loss_releases_and_withdraws(self):
        for change in ("damaged", "disappear"):
            with self.subTest(change=change):
                world = CombatWorld({"shield": 1}, entities=[mob()], offhand="shield")
                world.shield_pulse(**{change: True})
                self.assert_replan(world, DeflectSkeletonArrow(), "受伤|离开视野", ("use_item", "move_to"))
                self.assertFalse(world.st["blocking"])

    def test_abort_releases_shield_and_never_restarts_pulse(self):
        world = CombatWorld({"shield": 1}, entities=[mob()], offhand="shield")
        world.shield_pulse(using=True)
        ex, state = world.session()
        skill = DeflectSkeletonArrow()
        self.assertFalse(skill.step(ex, state))
        self.assertTrue(world.st["blocking"])
        skill.abort()
        self.assertFalse(world.st["blocking"])
        self.assertIsNone(world.st["using_item"])
        self.assertEqual(ex.acts, ["use_item"])

    def test_completed_progress_needs_no_shield(self):
        world = CombatWorld()
        world.seen.add(DEFLECT)
        self.assertFalse(self.run_skill(world, DeflectSkeletonArrow())[0].acts)

    def test_deflect_succeeds_after_knockback_push_and_halts_on_interrupted_by_stop(self):
        world = CombatWorld({"shield": 1}, entities=[mob()], offhand="shield")

        def pushed_block(w, **_):
            w.st["blocking"] = True
            w.st["pos"] = [-0.5, 64.0, 0.5]
            w.seen.add(DEFLECT)
            return {"used": "shield", "blocked_ms": 1100}

        world.queue("use_item", pushed_block)
        ex, _, records = self.run_skill(world, DeflectSkeletonArrow())
        self.assertEqual(ex.acts, ["use_item"])
        self.assertEqual(progress_records(records), [{DEFLECT: True}])
        self.assertFalse(world.st["blocking"])

        stop_world = CombatWorld({"shield": 1}, entities=[mob()], offhand="shield")
        stop_world.queue("use_item", {"used": "shield", "blocked_ms": 220, "interrupted_by": "stop"})
        self.assert_replan(stop_world, DeflectSkeletonArrow(), "interrupted_by=stop", ("use_item",))


class RangedTests(CombatTestCase):
    def test_bow_hit_reports_inventory_delta_and_progress(self):
        world = CombatWorld({"bow": 1, "arrow": 2}, entities=[mob()])
        world.shot(progress=(SHOOT_ARROW,))
        ex, state, records = self.run_skill(world, ShootMob())
        self.assertEqual(ex.acts, ["shoot"])
        self.assertEqual(state.count("arrow"), 1)
        shot = next(row for row in records if row["op"] == "combat_shot")
        self.assertEqual(shot["ammo_spent"], {"arrow": 1})
        self.assertEqual(progress_records(records), [{SHOOT_ARROW: True}])

    def test_fired_and_prediction_without_actual_hit_do_not_finish(self):
        world = CombatWorld({"bow": 1, "arrow": 1}, entities=[mob()])
        world.shot()
        self.assert_replan(world, ShootMob(), "未在轮询预算内", ("shoot", "move_to"))

    def test_crossbow_captures_independently_arriving_advancements(self):
        for skill in (ShootMob(), FireCrossbow()):
            with self.subTest(skill=skill.name):
                world = CombatWorld({"crossbow": 1, "arrow": 1}, entities=[mob()])
                world.shot()
                world.queue("advancements", _progress(), _progress(OL_BETSY), _progress(SHOOT_ARROW, OL_BETSY))
                ex, _, records = self.run_skill(world, skill)
                self.assertEqual(ex.acts, ["shoot"])
                self.assertEqual(progress_records(records), [
                    {SHOOT_ARROW: False, OL_BETSY: True}, {SHOOT_ARROW: True, OL_BETSY: True}])

    def test_missing_arrows_and_legacy_loaded_zero_ammo(self):
        for skill, weapon in ((ShootMob(), "bow"), (FireCrossbow(), "crossbow")):
            with self.subTest(skill=skill.name):
                self.assert_replan(CombatWorld({weapon: 1}, entities=[mob()]), skill, "还缺")

    def test_bystanders_and_covered_lanes_do_not_shoot(self):
        world = CombatWorld({"bow": 1, "arrow": 1}, entities=[
            mob(), mob("other_team", identifier=3, pos=(2.5, 64, 0.5), category="player")])
        self.assert_env(world, ShootMob(), "safe_combat_shot")
        world = CombatWorld({"bow": 1, "arrow": 1}, entities=[mob()])
        world.blocks[(2, 65, 0)] = "stone"
        self.assert_env(world, ShootMob(), "safe_combat_shot")

    def test_crossbow_without_target_or_backstop_yields_environment(self):
        self.assert_env(CombatWorld({"crossbow": 1, "arrow": 1}), FireCrossbow(), "safe_crossbow_backstop")

    def test_rocket_uses_solid_backstop_and_only_confirms_crossbow(self):
        world = CombatWorld({"crossbow": 1, "firework_rocket": 1})
        point = (9, 65, 0)
        world.blocks[point] = "obsidian"
        world.visible_blocks.append(point)
        world.shot("firework_rocket", progress=(OL_BETSY,))
        ex, _, records = self.run_skill(world, FireCrossbow())
        self.assertEqual(ex.acts, ["shoot"])
        request = next(args for op, args in world.calls if op == "shoot")
        self.assertEqual(request["pos"], [9.5, 65.5, 0.5])
        self.assertIsNone(request["entity_id"])
        self.assertEqual(progress_records(records), [{SHOOT_ARROW: False, OL_BETSY: True}])

    def test_fired_without_inventory_debit_rejected(self):
        world = CombatWorld({"crossbow": 1, "arrow": 1}, entities=[mob()])
        world.shot(progress=(OL_BETSY,), spent=0)
        self.assert_replan(world, FireCrossbow(), "弹药扣减", ("shoot", "move_to"))

    def test_abort_releases_latched_draw(self):
        for skill, weapon in ((ShootMob(), "bow"), (FireCrossbow(), "crossbow")):
            with self.subTest(skill=skill.name):
                world = CombatWorld({weapon: 1, "arrow": 1}, entities=[mob()])
                world.shot(using=True)
                ex, state = world.session()
                self.assertFalse(skill.step(ex, state))
                self.assertEqual(world.st["using_item"]["name"], weapon)
                skill.abort()
                self.assertIsNone(world.st["using_item"])
                self.assertEqual(ex.acts, ["shoot"])

    def test_already_confirmed_progress_does_not_fire(self):
        for skill, progress in ((ShootMob(), SHOOT_ARROW), (FireCrossbow(), OL_BETSY)):
            with self.subTest(skill=skill.name):
                world = CombatWorld()
                world.seen.add(progress)
                self.assertFalse(self.run_skill(world, skill)[0].acts)

    def test_crossbow_preparation_keeps_fishing_strings(self):
        world = CombatWorld({"stick": 4, "string": 4, "iron_ingot": 2, "oak_planks": 1, "arrow": 1}, entities=[mob()])
        world.workbench()
        world.craft_batch("tripwire_hook", {"stick": 1, "iron_ingot": 1, "oak_planks": 1}, output=2)
        world.craft_batch("crossbow", {"stick": 3, "string": 2, "iron_ingot": 1, "tripwire_hook": 1})
        world.shot(progress=(OL_BETSY, SHOOT_ARROW))
        ex, state, _ = self.run_skill(world, FireCrossbow())
        self.assertEqual(ex.acts, ["craft", "craft", "shoot"])
        self.assertEqual(state.count("tripwire_hook"), 1)
        self.assertEqual(state.count("string"), 2)

    def test_arrow_recipe_four_outputs_then_uses_one(self):
        world = CombatWorld({"bow": 1, "flint": 1, "stick": 1, "feather": 1}, entities=[mob()])
        world.workbench()
        world.craft_batch("arrow", {"flint": 1, "stick": 1, "feather": 1}, output=4)
        world.shot(progress=(SHOOT_ARROW,))
        ex, state, _ = self.run_skill(world, ShootMob())
        self.assertEqual(ex.acts, ["craft", "shoot"])
        self.assertEqual(state.count("arrow"), 3)

    def test_ambiguous_crossbow_ammo_routes_to_bow_for_arrow_hit(self):
        world = CombatWorld({"arrow": 1, "crossbow": 1, "bow": 1, "firework_rocket": 1}, entities=[mob()])
        world.shot(progress=(SHOOT_ARROW,))
        ex, state, _ = self.run_skill(world, ShootMob())
        self.assertEqual(ex.acts, ["shoot"])
        self.assertEqual(state.count("firework_rocket"), 1)
        self.assertEqual(next(args for op, args in world.calls if op == "shoot")["weapon"], "bow")

    def test_interrupted_shot_clears_latched_using_item(self):
        world = CombatWorld({"bow": 1, "arrow": 1}, entities=[mob()])

        def cancelled(world, **_):
            world.st["using_item"] = {"name": "bow"}
            raise ApiError("interrupted", "hurt")

        world.queue("shoot", cancelled)
        self.assert_replan(world, ShootMob(), "hurt", ("shoot", "move_to"))
        self.assertIsNone(world.st["using_item"])

    def test_jumping_blocks_combat_and_stop_cause_skips_retreat_move(self):
        jumping_world = CombatWorld({"bow": 1, "arrow": 1}, entities=[mob()])
        jumping_world.st["jumping"] = True
        _, jump_state = jumping_world.session()
        self.assertIn("跳跃", ShootMob.blocked(jump_state))

        world = CombatWorld({"bow": 1, "arrow": 1}, entities=[mob()])
        world.queue("shoot", ApiError(
            "interrupted", "stopped by user", {"cause": "stop", "inner_code": "path_not_found"},
        ))
        ex, state = world.session()
        skill = ShootMob()
        seen = []
        with self.assertRaisesRegex(Replan, "stopped by user"):
            while not skill.step(ex, state):
                seen.append(skill.last)
        self.assertEqual(ex.acts, ["shoot"])
        self.assertEqual(seen[-1]["op"], "combat_interrupted")
        self.assertEqual((seen[-1]["cause"], seen[-1]["inner_code"]), ("stop", "path_not_found"))
        world.assert_exhausted()


class CombatMaterialTests(CombatTestCase):
    def test_material_plan_pure_only_active_recipe_and_gear(self):
        world = CombatWorld({"iron_ingot": 1, "oak_planks": 6, "stone_sword": 1})
        _, state = world.session()
        before = list(world.calls)
        self.assertEqual(set(DeflectSkeletonArrow.material_plan({}, state)),
                         {("oak_planks", 6, "consume"), ("iron_ingot", 1, "consume")})
        self.assertEqual(HuntMob.material_plan({}, state), (("stone_sword", 1, "borrow"),))
        self.assertEqual(world.calls, before)

    def test_two_strings_require_existing_rod_or_more_supply(self):
        world = CombatWorld({"stick": 3, "string": 2, "iron_ingot": 1, "tripwire_hook": 1, "arrow": 1})
        _, state = world.session()
        expected = {("stick", 3, "consume"), ("string", 2, "consume"), ("iron_ingot", 1, "consume"),
                    ("tripwire_hook", 1, "consume"), ("arrow", 1, "consume")}
        self.assertEqual(set(FireCrossbow.material_plan({}, state)), expected)
        self.assert_replan(world, FireCrossbow(), "给钓竿留的 2 根")
        world.items["fishing_rod"] = 1
        state.set_inventory(world.inventory())
        self.assertEqual(FireCrossbow.blocked(state), "")

    def test_empty_uninitialized_menu_remains_blocked(self):
        for skill in (HuntMob, DeflectSkeletonArrow, FireCrossbow, ShootMob):
            self.assertTrue(skill.blocked(State()))

    def test_combat_scan_not_ready_replans_instead_of_env_blocked(self):
        world = CombatWorld({"stone_sword": 1}, held="stone_sword")
        world.queue("get_entities", ApiError("not_ready", "respawn chunks arriving"))
        self.assert_replan(world, HuntMob(), "not_ready", ())
