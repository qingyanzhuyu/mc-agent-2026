"""Explicit observations for P3. No simulated advancement is inferred from an action."""
import unittest

from mcagent.client import ApiError
from mcagent.runtime import EnvBlocked, Replan
from mcagent.skills import make
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.survival_camp import SleepInBed
from mcagent.skills.survival_farm import BreedAnimal, PlantSeed
from mcagent.skills.survival_fish import CatchFish
from mcagent.skills.testing import drive


PLANT = "minecraft:husbandry/plant_seed"
BREED = "minecraft:husbandry/breed_an_animal"
SLEEP = "minecraft:adventure/sleep_in_bed"
FISH = "minecraft:husbandry/fishy_business"
FIELD = (2, 63, 0)
BED = (2, 64, 0)
WATER = (2, 63, 0)


def progress(goal=None):
    return {"ready": True, "advancements": [{"id": goal, "status": "completed"}] if goal else []}


def blocks(*rows):
    return {"blocks": [{"name": name, "pos": pos} for pos, name in rows]}


def entities(*rows):
    return {"entities": list(rows)}


def chicken(id_, pos, **extra):
    return {"id": id_, "pos": pos, "type": "animal", "name": "chicken", "health": 4, **extra}


FIRST = chicken(11, (1.5, 64, 0.5))
SECOND = chicken(12, (2.5, 64, 0.5))


class SurvivalWorld(ScriptedWorld):
    def __init__(self, items=None, extra=None, **status):
        floor = {(x, 63, z): "stone" for x in range(-3, 5) for z in range(-3, 4)}
        super().__init__(blocks={**floor, **(extra or {})}, items=items)
        self.st.update({"dimension": "overworld", "is_day": False, "is_sleeping": False, **status})


def plant(world, *, pos, item, **_):
    x, y, z = pos
    world.items[item] -= 1
    world.blocks[(x, y + 1, z)] = "wheat"
    return {"changed": True, "used": {item: 1}}


def till(world, *, pos, **_):
    world.blocks[tuple(pos)] = "farmland"
    return {"block": "farmland", "changed": True}


def feed(world, *, entity_id, item, **_):
    world.items[item] -= 1
    return {"entity": {"id": entity_id, "name": "chicken"}, "used": {item: 1}}


def place_bed(world, *, item, pos, **_):
    x, y, z = pos
    world.items[item] -= 1
    world.blocks[(x, y, z)] = world.blocks[(x + 1, y, z)] = item
    return {"placed": item, "pos": pos}


def sleep(world, **_):
    world.st["is_sleeping"] = True
    return {"slept": True}


def catch(world, **_):
    world.items["salmon"] = world.items.get("salmon", 0) + 1
    return {"caught": {"name": "minecraft:salmon"}, "wait_ms": 30000}


class SurvivalTests(unittest.TestCase):
    def run_skill(self, world, name, **kwargs):
        ex, state = world.session()
        steps = drive(make(name, kwargs), ex, state)
        world.assert_exhausted()
        return ex.acts, steps

    def assert_blocked(self, world, name, predicate, volume, wake_on="chunk_moved", **kwargs):
        with self.assertRaises(EnvBlocked) as ctx:
            self.run_skill(world, name, **kwargs)
        exc = ctx.exception
        self.assertEqual((exc.missing_predicate, exc.searched_volume, exc.wake_on), (predicate, volume, wake_on))

    def assert_aborted(self, world, name, steps, expected, **kwargs):
        ex, state = world.session()
        skill = make(name, kwargs)
        for _ in range(steps):
            self.assertFalse(skill.step(ex, state))
        skill.abort()
        skill.abort()
        self.assertEqual(ex.acts, expected)
        self.assertIsNone(skill._flow)
        world.assert_exhausted()

    def plant_world(self, base="farmland", items=None):
        world = SurvivalWorld(items if items is not None else {"wheat_seeds": 3}, extra={FIELD: base})
        world.queue("find_blocks", blocks((FIELD, base)))
        return world

    def test_plant_reuses_farmland_and_verifies_seed_debit_and_crop(self):
        world = self.plant_world()
        world.queue("advancements", progress(), progress(PLANT))
        world.queue("interact", plant)
        actions, steps = self.run_skill(world, "plant_seed")
        self.assertEqual(actions, ["interact"])
        self.assertEqual(steps[-1]["result"], {"crop": "wheat", "advancement_confirmed": True})
        self.assertEqual(world.items["wheat_seeds"], 2)

    def test_plant_tills_with_copper_hoe_then_rechecks(self):
        world = self.plant_world("dirt", {"wheat_seeds": 1, "copper_hoe": 1})
        world.queue("advancements", progress(), progress())
        world.queue("interact", till, plant)
        actions, steps = self.run_skill(world, "plant_seed")
        self.assertEqual(actions, ["interact", "interact"])
        self.assertFalse(steps[-1]["result"]["advancement_confirmed"])
        self.assertEqual([req["item"] for op, req in world.calls if op == "interact"], ["copper_hoe", "wheat_seeds"])

    def test_plant_missing_hoe_and_ingredients_replan(self):
        world = self.plant_world("dirt", {"wheat_seeds": 1})
        world.queue("advancements", progress())
        with self.assertRaisesRegex(Replan, "缺材料"):
            self.run_skill(world, "plant_seed")

    def test_plant_no_site_reports_required_predicate(self):
        world = SurvivalWorld({"wheat_seeds": 1})
        world.queue("advancements", progress())
        world.queue("find_blocks", blocks())
        self.assert_blocked(world, "plant_seed", "reachable_planting_site:farmland", "半径 12 格内最多 10 个候选")

    def test_plant_seed_skips_dig_unconfirmed_grass_candidate(self):
        g1, g2 = (1, 64, 0), (0, 64, 1)
        world = SurvivalWorld({}, extra={g1: "short_grass", g2: "short_grass", FIELD: "farmland"})
        world.queue("advancements", progress(), progress(PLANT))
        world.queue("find_blocks", blocks((g1, "short_grass"), (g2, "short_grass")), blocks((FIELD, "farmland")))

        def dig_grass(w, *, pos, **_):
            w.blocks.pop(tuple(pos))
            w.items["wheat_seeds"] = 1
            return {"dug": "short_grass", "gained": {"wheat_seeds": 1}}

        world.queue(
            "dig",
            ApiError("dig_unconfirmed", "unconfirmed", {"pos": list(g1), "eye_dist": 4.9}),
            dig_grass,
        )
        world.queue("interact", plant)
        _, steps = self.run_skill(world, "plant_seed")
        dig_steps = [s for s in steps if s["op"] == "dig"]
        self.assertEqual([s.get("error_code") for s in dig_steps], ["dig_unconfirmed", None])
        self.assertTrue(steps[-1]["result"]["advancement_confirmed"])

    def test_plant_grass_without_seed_drop_is_not_completion(self):
        grass = (2, 64, 0)
        world = SurvivalWorld(extra={grass: "short_grass"})
        world.queue("advancements", progress())
        world.queue("find_blocks", blocks((grass, "short_grass")))
        world.queue("get_entities", entities())

        def no_seed(world, *, pos, **_):
            world.blocks.pop(tuple(pos))
            return {"dug": "short_grass"}

        world.queue("dig", no_seed)
        self.assert_blocked(world, "plant_seed", "reachable_wheat_seeds", "半径 12 格内最多 8 个候选")

    def test_plant_success_receipt_without_crop_fails(self):
        world = self.plant_world()
        world.queue("advancements", progress())
        world.queue("interact", {"changed": True, "above": {"after": "wheat"}})
        with self.assertRaisesRegex(Replan, "未同时确认新作物"):
            self.run_skill(world, "plant_seed")

    def test_abort_plant_after_tilling_does_not_seed(self):
        world = self.plant_world("dirt", {"wheat_seeds": 1, "wooden_hoe": 1})
        world.queue("advancements", progress())
        world.queue("interact", till)
        self.assert_aborted(world, "plant_seed", 1, ["interact"])
        self.assertEqual(world.items["wheat_seeds"], 1)

    def breeding_world(self, *, confirmed=None, extra_polls=0, action_count=2):
        world = SurvivalWorld({"wheat_seeds": 3})
        world.queue("advancements", progress(), *([progress(confirmed)] * extra_polls))
        world.queue("get_entities", *([entities(FIRST, SECOND)] * (1 + 2 * action_count)))
        world.queue("interact", *([feed] * action_count))
        return world

    def test_breed_two_distinct_parents_and_read_advancement(self):
        world = self.breeding_world(confirmed=BREED, extra_polls=1)
        actions, steps = self.run_skill(world, "breed_animal")
        self.assertEqual(actions, ["interact", "interact"])
        self.assertEqual([req["entity_id"] for op, req in world.calls if op == "interact"], [11, 12])
        self.assertTrue(steps[-1]["result"]["advancement_confirmed"])
        self.assertEqual(world.items["wheat_seeds"], 1)

    def test_breed_accepts_new_nearby_juvenile_but_not_as_server_progress(self):
        world = self.breeding_world(extra_polls=1)
        world.queue("get_entities", entities(FIRST, SECOND, chicken(13, (2.1, 64, 0.5), baby=True)))
        _, steps = self.run_skill(world, "breed_animal")
        self.assertEqual(steps[-1]["result"], {"advancement_confirmed": False, "observed_offspring": True})

    def test_breed_missing_compatible_food_replans(self):
        world = SurvivalWorld({"wheat": 1})
        world.queue("advancements", progress())
        with self.assertRaisesRegex(Replan, "两份 wheat"):
            self.run_skill(world, "breed_animal", animal="cow")

    def test_breed_duplicate_entity_or_juvenile_is_environment_blocked(self):
        for other in (FIRST, chicken(12, (2.5, 64, 0.5), baby=True)):
            with self.subTest(other=other):
                world = SurvivalWorld({"wheat_seeds": 2})
                world.queue("advancements", progress())
                world.queue("get_entities", entities(FIRST, other))
                self.assert_blocked(world, "breed_animal", "reachable_pair:chicken", "半径 16 格内最多 40 个候选")

    def test_breed_two_successful_feeds_without_breeding_fail(self):
        world = self.breeding_world(extra_polls=3)
        world.queue("get_entities", *([entities(FIRST, SECOND)] * 3))
        with self.assertRaisesRegex(Replan, "尚未观察到繁殖"):
            self.run_skill(world, "breed_animal")

    def test_abort_breed_leaves_second_parent_unfed(self):
        world = self.breeding_world(action_count=1)
        self.assert_aborted(world, "breed_animal", 1, ["interact"])
        self.assertEqual(world.items["wheat_seeds"], 2)

    def bed_world(self, **status):
        world = SurvivalWorld(extra={BED: "white_bed", (3, 64, 0): "white_bed"}, **status)
        world.queue("find_blocks", blocks((BED, "white_bed")))
        return world

    def test_sleep_reuses_existing_bed_and_reads_sleeping_state(self):
        world = self.bed_world()
        world.queue("advancements", progress(), progress())
        world.queue("interact", sleep)
        actions, steps = self.run_skill(world, "sleep_in_bed")
        self.assertEqual(actions, ["interact"])
        self.assertTrue(steps[-1]["result"]["observed_sleeping"])

    def test_sleep_crafts_same_color_bed_then_rechecks_both_halves(self):
        table = (-2, 64, 0)
        world = SurvivalWorld({"white_wool": 3, "red_wool": 1, "oak_planks": 3}, extra={table: "crafting_table"})
        world.queue("advancements", progress(), progress(SLEEP))
        world.queue("find_blocks", blocks(), {"blocks": [{"pos": list(table)}]})

        def craft_bed(world, *, item, **_):
            self.assertEqual(item, "white_bed")
            world.items["white_wool"] -= 3
            world.items["oak_planks"] -= 3
            world.items[item] = 1
            return {"crafted": item, "gained": 1}

        world.queue("craft", craft_bed)
        world.queue("place", place_bed)
        world.queue("interact", sleep)
        actions, _ = self.run_skill(world, "sleep_in_bed")
        self.assertEqual(actions, ["craft", "place", "interact"])
        self.assertEqual(world.items["red_wool"], 1)

    def test_sleep_mixed_wool_cannot_make_a_bed(self):
        world = SurvivalWorld({"white_wool": 2, "red_wool": 1, "oak_planks": 3})
        world.queue("advancements", progress())
        world.queue("find_blocks", blocks())
        with self.assertRaisesRegex(Replan, "3 块同色羊毛"):
            self.run_skill(world, "sleep_in_bed")

    def test_sleep_daytime_waits_for_night_and_threat_replans(self):
        daytime = SurvivalWorld(is_day=True)
        daytime.queue("advancements", progress())
        self.assert_blocked(daytime, "sleep_in_bed", "sleep_at_night", "当前主世界 /status 昼夜快照", "night")
        night = SurvivalWorld(nearest_hostile={"name": "skeleton", "dist": 6})
        night.queue("advancements", progress())
        with self.assertRaisesRegex(Replan, "敌对生物"):
            self.run_skill(night, "sleep_in_bed")

    def test_sleep_receipt_without_sleep_witness_fails(self):
        world = self.bed_world()
        world.queue("advancements", progress(), progress())
        world.queue("interact", {"slept": True})
        with self.assertRaisesRegex(Replan, "未复核到睡眠"):
            self.run_skill(world, "sleep_in_bed")

    def test_abort_sleep_after_place_preserves_bed_and_does_not_interact(self):
        world = SurvivalWorld({"red_bed": 1})
        world.queue("advancements", progress())
        world.queue("find_blocks", blocks())
        world.queue("place", place_bed)
        self.assert_aborted(world, "sleep_in_bed", 1, ["place"])
        self.assertEqual(world.blocks[BED], "red_bed")

    def fish_world(self, *, extra=None):
        world = SurvivalWorld({"fishing_rod": 1, "salmon": 1}, extra={WATER: "water", **(extra or {})})
        world.queue("find_blocks", blocks((WATER, "water")))
        return world

    def test_fish_reuses_rod_and_checks_delta_and_45_second_request(self):
        world = self.fish_world()
        world.queue("advancements", progress(), progress(FISH))
        world.queue("use_item", catch)
        actions, steps = self.run_skill(world, "catch_fish")
        self.assertEqual(actions, ["use_item"])
        req = next(req for op, req in world.calls if op == "use_item")
        self.assertEqual((req["timeout_ms"], req["target"], req["stop_on"]), (45000, {"pos": list(WATER)}, ("hurt", "death")))
        self.assertEqual(steps[-1]["result"], {"caught": "salmon", "gained": 1, "advancement_confirmed": True})

    def test_fish_missing_rod_and_string_replan(self):
        world = SurvivalWorld({"stick": 3, "string": 1})
        world.queue("advancements", progress())
        with self.assertRaisesRegex(Replan, "2 根线"):
            self.run_skill(world, "catch_fish")

    def test_fish_water_without_clear_surface_is_environment_blocked(self):
        world = self.fish_world(extra={(2, 68, 0): "stone"})
        world.queue("advancements", progress())
        self.assert_blocked(world, "catch_fish", "reachable_open_water", "半径 16 格内最多 10 个候选")

    def test_fish_rejects_garbage_empty_payload_and_unconfirmed_old_fish(self):
        for caught in ("leather", "enchanted_book", None, "salmon"):
            with self.subTest(caught=caught):
                world = self.fish_world()
                world.queue("advancements", progress(), *([progress()] if caught == "salmon" else []))
                world.queue("use_item", {"caught": caught})
                with self.assertRaisesRegex(Replan, "未确认|未获"):
                    self.run_skill(world, "catch_fish")

    def test_abort_fish_clears_observed_active_rod_once(self):
        world = self.fish_world()
        world.queue("advancements", progress())

        def incomplete(world, **_):
            world.st["using_item"] = {"name": "fishing_rod", "elapsed_ms": 1000}
            return {"caught": None}

        world.queue("use_item", incomplete)
        self.assert_aborted(world, "catch_fish", 1, ["use_item"])
        self.assertEqual([op for op, _ in world.calls if op == "stop"], ["stop"])

    def test_goals_with_existing_server_progress_issue_no_action(self):
        for skill, goal in (("plant_seed", PLANT), ("breed_animal", BREED), ("sleep_in_bed", SLEEP), ("catch_fish", FISH)):
            with self.subTest(skill=skill):
                world = SurvivalWorld()
                world.queue("advancements", progress(goal))
                self.assertEqual(self.run_skill(world, skill), ([], []))

    def test_material_declarations_match_seed_split_bed_and_rod_paths(self):
        world = SurvivalWorld({"wheat_seeds": 3, "white_wool": 3, "oak_planks": 3, "fishing_rod": 1})
        _, state = world.session()
        self.assertEqual(PlantSeed.material_plan({}, state), (("wheat_seeds", 1, "consume"),))
        self.assertEqual(BreedAnimal.material_plan({}, state), (("wheat_seeds", 2, "consume"),))
        self.assertEqual(SleepInBed.material_plan({}, state), (("white_wool", 3, "consume"), ("oak_planks", 3, "consume")))
        self.assertEqual(CatchFish.material_plan({}, state), (("fishing_rod", 1, "borrow"),))
        self.assertEqual(world.calls, [])

    def test_pure_material_plan_holds_one_hoe_or_shearing_route(self):
        world = SurvivalWorld({"wheat_seeds": 1, "stick": 2, "oak_planks": 3, "red_wool": 2, "shears": 1})
        _, state = world.session()
        self.assertEqual(PlantSeed.material_plan({}, state),
                         (("wheat_seeds", 1, "consume"), ("stick", 2, "consume"), ("oak_planks", 2, "consume")))
        self.assertEqual(SleepInBed.material_plan({}, state),
                         (("shears", 1, "borrow"), ("red_wool", 2, "consume"), ("oak_planks", 3, "consume")))
        self.assertEqual(world.calls, [])
