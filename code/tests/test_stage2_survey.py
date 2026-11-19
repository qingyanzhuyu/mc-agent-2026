"""Stage 2 survey & discovery skills + WorldResourceMemo biome/sector offline tests (verified: offline)."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import tempfile
import unittest
from typing import Any, Callable, Optional

from mcagent.runtime import EnvBlocked, Replan, State
from mcagent.skills import REGISTRY, bind_args, contract_errors, make
from mcagent.skills.base import FORBIDDEN_PARAMS, PARAM_TYPES, _COOLDOWN
from mcagent.skills.items import bare
from mcagent.skills.survey import (
    NETHER_BIOMES,
    DiscoverStructure,
    SurveyIsland,
    resolve_eye_bearings,
    survey_sector,
)
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches, voxel_cache_for
from mcagent.skills.world_memo import (
    STRUCTURE_FINGERPRINTS,
    WorldResourceMemo,
    memo_for,
    reset_all_world_memos,
    structure_tags_for,
)

_COORD_TRIPLET_RE = re.compile(r"-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+")
_BRACKETED_COORD_RE = re.compile(r"[\[(]\s*-?\d+[^\]\)\n]*-?\d+[^\]\)\n]*-?\d+\s*[\])]")

_NEW_STRUCTURE_TAGS: dict[str, tuple[str, ...]] = {
    "nether_fortress": ("nether_bricks", "nether_brick_fence", "nether_brick_stairs", "nether_wart"),
    "bastion_remnant": (
        "polished_blackstone_bricks",
        "gilded_blackstone",
        "cracked_polished_blackstone_bricks",
        "chiseled_polished_blackstone",
    ),
    "end_city": ("purpur_block", "purpur_pillar", "end_stone_bricks"),
    "lush_caves": ("moss_block", "clay", "azalea", "glow_berries", "cave_vines"),
    "amethyst_geode": ("amethyst_block", "budding_amethyst", "calcite"),
    "pale_garden": ("pale_oak_log", "pale_moss_block", "creaking_heart"),
    "pillager_outpost": ("dark_oak_log", "white_banner"),
    "dried_ghast": ("dried_ghast", "bone_block"),
}


class _SurveyGridWorld(GridWorld):
    """Offline GridWorld supporting find_blocks, dimension, biome, and dynamic biome-by-pos."""

    def __init__(
        self,
        *,
        pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
        blocks: Optional[dict[tuple[int, int, int], str]] = None,
        items: Optional[dict[str, int]] = None,
        dimension: str = "overworld",
        biome: str = "plains",
        biome_fn: Optional[Callable[[list[float]], str]] = None,
    ) -> None:
        super().__init__(pos=pos, blocks=blocks, items=items)
        self._biome_fn = biome_fn
        self.st["dimension"] = dimension
        self.st["biome"] = biome_fn(list(self.st["pos"])) if biome_fn is not None else biome
        self.find_calls: list[dict[str, Any]] = []
        self.move_calls: list[list[float]] = []

    def _sync_biome(self) -> None:
        if self._biome_fn is not None:
            self.st["biome"] = self._biome_fn(list(self.st["pos"]))

    def status(self, timeout: float = 10) -> dict:
        self._sync_biome()
        return super().status(timeout=timeout)

    def get_block(self, pos) -> dict:
        cell = (math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2]))
        return {"name": self.blocks.get(cell, "air")}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        res = super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)
        self.move_calls.append(list(self.st["pos"]))
        self._sync_biome()
        return res

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None) -> dict:
        req_types = list(types) if types is not None else None
        self.find_calls.append({"types": req_types, "radius": radius, "limit": limit})
        self.calls.append(("find_blocks", {"types": req_types, "radius": radius, "limit": limit}))
        cx, cy, cz = self.st["pos"]

        if not req_types:
            summary: dict[str, dict[str, Any]] = {}
            for (bx, by, bz), raw_name in sorted(self.blocks.items()):
                name = bare(raw_name).strip().lower()
                if not name or name == "air":
                    continue
                d = math.dist((bx, by, bz), (cx, cy, cz))
                if d > radius:
                    continue
                entry = summary.get(name)
                if entry is None:
                    summary[name] = {"count": 1, "nearest": [bx, by, bz], "dist": round(d, 2)}
                else:
                    entry["count"] += 1
                    if d < entry["dist"]:
                        entry["nearest"] = [bx, by, bz]
                        entry["dist"] = round(d, 2)
            return {"radius": radius, "scanned": len(self.blocks), "blocks": summary}

        allowed = {bare(t).strip().lower() for t in req_types}
        out: list[dict[str, Any]] = []
        for (bx, by, bz), raw_name in sorted(self.blocks.items()):
            name = bare(raw_name).strip().lower()
            if name not in allowed:
                continue
            d = math.dist((bx, by, bz), (cx, cy, cz))
            if d <= radius:
                out.append({"name": name, "pos": [bx, by, bz], "dist": round(d, 2)})
        out.sort(key=lambda b: b["dist"])
        return {"total": len(out), "blocks": out[:limit]}


def _flat_ground(
    surface: str = "grass_block",
    y: int = 63,
    extent: int = 36,
    hazards: Optional[dict[tuple[int, int, int], str]] = None,
) -> dict[tuple[int, int, int], str]:
    """Build a solid dry floor over [-extent, extent]^2 with optional hazard/structure overrides."""
    blocks: dict[tuple[int, int, int], str] = {
        (x, y, z): surface
        for x in range(-extent, extent + 1)
        for z in range(-extent, extent + 1)
    }
    if hazards:
        blocks.update(hazards)
    return blocks


class SurveySkillContractTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_all_world_memos()
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def test_registration_contract_and_param_validation(self) -> None:
        self.assertIs(REGISTRY.get("survey_island"), SurveyIsland)
        self.assertIs(REGISTRY.get("discover_structure"), DiscoverStructure)
        self.assertEqual(contract_errors(SurveyIsland), [])
        self.assertEqual(contract_errors(DiscoverStructure), [])
        self.assertEqual(SurveyIsland.verified, "offline")
        self.assertEqual(DiscoverStructure.verified, "offline")
        self.assertEqual(SurveyIsland.goal, "survey:{dimension}")
        self.assertEqual(DiscoverStructure.goal, "discover:{structure}")

        for cls in (SurveyIsland, DiscoverStructure):
            for pname, p in cls.params.items():
                self.assertIn(p.type, PARAM_TYPES)
                self.assertNotIn(pname, FORBIDDEN_PARAMS)

        self.assertEqual(set(SurveyIsland.params), {"dimension", "sectors", "radius"})
        self.assertEqual(set(DiscoverStructure.params), {"structure", "radius"})

        valid_survey = make("survey_island", {"dimension": "overworld", "sectors": 4, "radius": 32})
        self.assertEqual(valid_survey.goal, "survey:overworld")
        valid_discover = make("discover_structure", {"structure": "stronghold", "radius": 48})
        self.assertEqual(valid_discover.goal, "discover:stronghold")

        invalid_survey_cases = [
            {"dimension": ""},
            {"dimension": 123},
            {"dimension": "invalid_dimension"},
            {"sectors": 0},
            {"sectors": -2},
            {"sectors": True},
            {"radius": 0},
            {"radius": -10},
            {"radius": False},
            {"unknown_param": 1},
        ]
        for bad_args in invalid_survey_cases:
            with self.subTest(skill="survey_island", bad_args=bad_args):
                with self.assertRaises(ValueError):
                    make("survey_island", bad_args)

        invalid_discover_cases = [
            {"structure": ""},
            {"structure": 42},
            {"structure": "nonexistent_structure"},
            {"radius": 0},
            {"radius": -5},
            {"radius": True},
            {"unknown_param": "x"},
        ]
        for bad_args in invalid_discover_cases:
            with self.subTest(skill="discover_structure", bad_args=bad_args):
                with self.assertRaises(ValueError):
                    make("discover_structure", bad_args)

        _, errs = bind_args(DiscoverStructure, {"structure": ""})
        self.assertTrue(errs)


class EyeBearingMathTests(unittest.TestCase):
    def test_valid_intersections(self) -> None:
        pt = resolve_eye_bearings((0.0, 0.0, -45.0), (100.0, 0.0, 0.0))
        self.assertIsNotNone(pt)
        assert pt is not None
        self.assertAlmostEqual(pt[0], 100.0, places=5)
        self.assertAlmostEqual(pt[1], 100.0, places=5)

        # Non-axis-aligned intersection at (320.0, -240.0)
        tx, tz = 320.0, -240.0
        o1x, o1z = 15.0, 40.0
        o2x, o2z = -180.0, 90.0
        yaw1 = math.degrees(math.atan2(-(tx - o1x), tz - o1z))
        yaw2 = math.degrees(math.atan2(-(tx - o2x), tz - o2z))
        pt2 = resolve_eye_bearings((o1x, o1z, yaw1), (o2x, o2z, yaw2))
        self.assertIsNotNone(pt2)
        assert pt2 is not None
        self.assertAlmostEqual(pt2[0], tx, places=4)
        self.assertAlmostEqual(pt2[1], tz, places=4)

    def test_parallel_and_antiparallel_return_none(self) -> None:
        self.assertIsNone(resolve_eye_bearings((0.0, 0.0, 0.0), (50.0, 0.0, 0.0)))
        self.assertIsNone(resolve_eye_bearings((0.0, 0.0, 37.5), (12.0, -34.0, 37.5)))
        self.assertIsNone(resolve_eye_bearings((0.0, 0.0, 0.0), (50.0, 0.0, 180.0)))
        self.assertIsNone(resolve_eye_bearings((0.0, 0.0, -45.0), (100.0, 100.0, 135.0)))

    def test_intersection_behind_ray_returns_none(self) -> None:
        # Lines cross at (100, 100), which is behind ray 1 (yaw=135 -> (-1, -1))
        self.assertIsNone(resolve_eye_bearings((0.0, 0.0, 135.0), (100.0, 0.0, 0.0)))
        # Lines cross at (100, 100), which is behind ray 2 (yaw=180 -> (0, -1))
        self.assertIsNone(resolve_eye_bearings((0.0, 0.0, -45.0), (100.0, 0.0, 180.0)))
        # Behind both rays
        self.assertIsNone(resolve_eye_bearings((0.0, 0.0, 135.0), (100.0, 0.0, 180.0)))

    def test_non_finite_and_malformed_inputs_return_none(self) -> None:
        bad_obs = [
            (float("nan"), 0.0, 0.0),
            (0.0, float("inf"), 0.0),
            (0.0, 0.0, float("-inf")),
            (0.0, 0.0),
            (True, 0.0, 0.0),
            "bad",
            None,
        ]
        good = (10.0, 0.0, 45.0)
        for bad in bad_obs:
            with self.subTest(bad=bad):
                self.assertIsNone(resolve_eye_bearings(bad, good))  # type: ignore[arg-type]
                self.assertIsNone(resolve_eye_bearings(good, bad))  # type: ignore[arg-type]


class WorldMemoSurveyTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        self.addCleanup(reset_all_world_memos)
        self.memo = WorldResourceMemo(clock=lambda: 100.0)

    def test_new_structure_fingerprints_and_ingest(self) -> None:
        for tag, blocks in _NEW_STRUCTURE_TAGS.items():
            with self.subTest(tag=tag):
                self.assertEqual(STRUCTURE_FINGERPRINTS[tag], blocks)
                tags = structure_tags_for([f"minecraft:{blocks[0]}"])
                self.assertEqual(tags.get(tag), [blocks[0]])

                memo = WorldResourceMemo(clock=lambda: 100.0)
                n = memo.ingest_find_blocks(
                    {"blocks": [{"name": f"minecraft:{blocks[-1]}", "pos": [20, 64, 20], "dist": 20.0}]},
                    dimension="the_nether" if tag in ("nether_fortress", "bastion_remnant", "dried_ghast") else "overworld",
                )
                self.assertGreaterEqual(n, 1)
                dim = "the_nether" if tag in ("nether_fortress", "bastion_remnant", "dried_ghast") else "overworld"
                self.assertTrue(memo.has_landmark(tag, dimension=dim))
                entries = memo.query("structure", dimension=dim)
                self.assertTrue(any(e.tag == tag for e in entries))

    def test_record_biome_and_sectors_isolation(self) -> None:
        self.assertTrue(self.memo.record_biome("plains"))
        self.assertFalse(self.memo.record_biome("minecraft:plains"))
        self.assertFalse(self.memo.record_biome("  PLAINS  "))
        self.assertFalse(self.memo.record_biome(""))
        self.assertFalse(self.memo.record_biome("   "))
        self.assertFalse(self.memo.record_biome("minecraft:"))
        self.assertEqual(self.memo.biomes(), {"plains"})

        self.assertTrue(self.memo.record_biome("minecraft:crimson_forest", dimension="the_nether"))
        self.assertEqual(self.memo.biomes(dimension="overworld"), {"plains"})
        self.assertEqual(self.memo.biomes(dimension="minecraft:the_nether"), {"crimson_forest"})

        returned_biomes = self.memo.biomes()
        returned_biomes.add("mutated_copy")
        self.assertEqual(self.memo.biomes(), {"plains"})

        self.memo.mark_sector_done(0)
        self.memo.mark_sector_done(3)
        self.memo.mark_sector_done(1, dimension="the_nether")
        self.assertEqual(self.memo.done_sectors(dimension="overworld"), {0, 3})
        self.assertEqual(self.memo.done_sectors(dimension="the_nether"), {1})

        returned_sectors = self.memo.done_sectors()
        returned_sectors.add(99)
        self.assertEqual(self.memo.done_sectors(), {0, 3})

        for bad_idx in (-1, True, False):
            with self.subTest(bad_idx=bad_idx):
                with self.assertRaises(ValueError):
                    self.memo.mark_sector_done(bad_idx)  # type: ignore[arg-type]

    def test_checkpoint_roundtrip_legacy_and_corruption_safety(self) -> None:
        self.memo.record_biome("forest", dimension="overworld")
        self.memo.record_biome("meadow", dimension="overworld")
        self.memo.record_biome("soul_sand_valley", dimension="the_nether")
        self.memo.mark_sector_done(0, dimension="overworld")
        self.memo.mark_sector_done(2, dimension="overworld")
        self.memo.mark_sector_done(1, dimension="the_nether")
        self.memo.record_landmark("amethyst_geode", [120, 40, -240], meta={"block": "calcite"})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ckpt = tmp_dir / "survey_memo.json"
            self.memo.save_checkpoint(ckpt)

            restored = WorldResourceMemo(clock=lambda: 100.0)
            self.assertTrue(restored.load_checkpoint(ckpt))
            self.assertEqual(restored.biomes(dimension="overworld"), {"forest", "meadow"})
            self.assertEqual(restored.biomes(dimension="the_nether"), {"soul_sand_valley"})
            self.assertEqual(restored.done_sectors(dimension="overworld"), {0, 2})
            self.assertEqual(restored.done_sectors(dimension="the_nether"), {1})

            # Legacy schema_version=1 checkpoint without visited_biomes / survey_sectors_done
            legacy_payload = {
                "schema_version": 1,
                "landmark_version": 1,
                "stations": {},
                "landmarks": {"overworld": {"village": [{"pos": [50, 64, 50], "meta": {}}]}},
                "portals": {},
                "resources": [],
            }
            legacy_ckpt = tmp_dir / "legacy_v1.json"
            legacy_ckpt.write_text(json.dumps(legacy_payload), encoding="utf-8")
            legacy_loaded = WorldResourceMemo(clock=lambda: 100.0)
            self.assertTrue(legacy_loaded.load_checkpoint(legacy_ckpt))
            self.assertEqual(legacy_loaded.biomes(), set())
            self.assertEqual(legacy_loaded.done_sectors(), set())
            self.assertEqual(legacy_loaded.landmarks("village"), [(50, 64, 50)])

            # Malformed visited_biomes / survey_sectors_done must fail without corrupting memo
            base = json.loads(ckpt.read_text(encoding="utf-8"))
            malformed_cases = [
                {**base, "visited_biomes": ["plains"]},
                {**base, "visited_biomes": {"overworld": "plains"}},
                {**base, "visited_biomes": {"overworld": [""]}},
                {**base, "visited_biomes": {"overworld": [123]}},
                {**base, "survey_sectors_done": [0, 1]},
                {**base, "survey_sectors_done": {"overworld": 0}},
                {**base, "survey_sectors_done": {"overworld": [-1]}},
                {**base, "survey_sectors_done": {"overworld": [True]}},
                {**base, "survey_sectors_done": {"overworld": ["0"]}},
            ]
            for idx, bad_data in enumerate(malformed_cases):
                bad_path = tmp_dir / f"bad_{idx}.json"
                bad_path.write_text(json.dumps(bad_data), encoding="utf-8")
                with self.subTest(case=idx):
                    self.assertFalse(restored.load_checkpoint(bad_path))

            self.assertEqual(restored.biomes(dimension="overworld"), {"forest", "meadow"})
            self.assertEqual(restored.done_sectors(dimension="overworld"), {0, 2})
            self.assertEqual(restored.landmarks("amethyst_geode"), [(120, 40, -240)])

    def test_summary_for_context_includes_biomes_and_sectors_sanitized(self) -> None:
        empty_ctx = self.memo.summary_for_context(dimension="overworld")
        self.assertNotIn("visited_biomes", empty_ctx)
        self.assertNotIn("survey_sectors_done", empty_ctx)

        self.memo.record_biome("plains", dimension="overworld")
        self.memo.record_biome("birch_forest", dimension="overworld")
        self.memo.mark_sector_done(0, dimension="overworld")
        self.memo.mark_sector_done(2, dimension="overworld")
        self.memo.record_landmark("lush_caves", [1234, 48, -5678], meta={"block": "moss_block"})
        self.memo.record_biome("crimson_forest", dimension="the_nether")

        ctx = self.memo.summary_for_context(dimension="overworld")
        self.assertEqual(ctx["visited_biomes"], ["birch_forest", "plains"])
        self.assertEqual(ctx["survey_sectors_done"], [0, 2])
        self.assertIn("lush_caves", ctx["landmarks_discovered"])

        serialized = json.dumps(ctx, ensure_ascii=False)
        self.assertIsNone(_COORD_TRIPLET_RE.search(serialized))
        self.assertIsNone(_BRACKETED_COORD_RE.search(serialized))
        for raw_coord in ("1234", "48", "-5678"):
            self.assertNotIn(raw_coord, serialized)

        nether_ctx = self.memo.summary_for_context(dimension="the_nether")
        self.assertEqual(nether_ctx["visited_biomes"], ["crimson_forest"])
        self.assertNotIn("survey_sectors_done", nether_ctx)


class SurveyIslandSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_all_world_memos()
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def _build_overworld_survey_world(self) -> _SurveyGridWorld:
        hazards = {
            (16, 63, 0): "lava",
            (16, 64, 0): "lava",
            (10, 63, 16): "water",
            (-10, 63, 0): "lava",
            (0, 63, -10): "water",
            (14, 63, 14): "amethyst_block",
            (15, 63, 14): "calcite",
            (-14, 63, 14): "moss_block",
            (-15, 63, 14): "azalea",
            (-14, 63, -14): "hay_block",
            (-15, 63, -14): "bell",
        }
        blocks = _flat_ground("grass_block", y=63, extent=36, hazards=hazards)

        def biome_at(pos: list[float]) -> str:
            if pos[0] >= 8.0:
                return "forest"
            if pos[0] <= -4.0:
                return "meadow"
            return "plains"

        return _SurveyGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            dimension="overworld",
            biome_fn=biome_at,
        )

    def test_survey_sector_primitive_records_sector_biome_and_voxel_cache(self) -> None:
        world = self._build_overworld_survey_world()
        ex, state = world.session()

        steps = drive(survey_sector(ex, state, sector_idx=0, total_sectors=4, radius=32), ex, state)
        self.assertTrue(steps)
        memo = memo_for(state)
        self.assertIn(0, memo.done_sectors(dimension="overworld"))
        self.assertTrue(memo.biomes(dimension="overworld"))
        self.assertGreater(voxel_cache_for(state).stats()["ingested"], 0)

    def test_survey_island_sweeps_all_sectors_avoids_hazards_and_records_landmarks(self) -> None:
        world = self._build_overworld_survey_world()
        world.items["stone_pickaxe"] = 1
        ex, state = world.session()

        skill = make("survey_island", {"dimension": "overworld", "sectors": 4, "radius": 32})
        self.assertTrue(skill.can_start(state))
        self.assertNotEqual(skill.verify_done(state), "")

        steps = drive(skill, ex, state)
        self.assertTrue(steps)
        self.assertIn("move_to", ex.acts)
        self.assertTrue(world.move_calls)

        hazard_cells = {(16, 63, 0), (16, 64, 0), (10, 63, 16), (-10, 63, 0), (0, 63, -10)}
        for dest in world.move_calls:
            cell = (math.floor(dest[0]), math.floor(dest[1]), math.floor(dest[2]))
            foot_cell = (cell[0], cell[1] - 1, cell[2])
            self.assertNotIn(cell, hazard_cells)
            self.assertNotIn(foot_cell, hazard_cells)

        memo = memo_for(state)
        self.assertEqual(memo.done_sectors(dimension="overworld"), {0, 1, 2, 3})
        self.assertTrue({"plains", "forest", "meadow"}.issubset(memo.biomes(dimension="overworld")))
        self.assertTrue(memo.has_landmark("amethyst_geode", dimension="overworld"))
        self.assertTrue(memo.has_landmark("lush_caves", dimension="overworld"))
        self.assertTrue(memo.has_landmark("village", dimension="overworld"))
        self.assertGreater(voxel_cache_for(state).stats()["ingested"], 0)
        self.assertEqual(skill.verify_done(state), "")

    def test_partial_resume_runs_only_remaining_sectors(self) -> None:
        full_world = self._build_overworld_survey_world()
        full_ex, full_state = full_world.session()
        drive(SurveyIsland(dimension="overworld", sectors=4, radius=32), full_ex, full_state)
        full_move_count = len(full_world.move_calls)
        self.assertGreaterEqual(full_move_count, 4)

        world = self._build_overworld_survey_world()
        ex, state = world.session()
        memo = memo_for(state)
        memo.mark_sector_done(0, dimension="overworld")
        memo.mark_sector_done(1, dimension="overworld")

        skill = SurveyIsland(dimension="overworld", sectors=4, radius=32)
        drive(skill, ex, state)
        self.assertEqual(memo.done_sectors(dimension="overworld"), {0, 1, 2, 3})
        self.assertGreaterEqual(len(world.move_calls), 1)
        self.assertLess(len(world.move_calls), full_move_count)
        self.assertEqual(skill.verify_done(state), "")

    def test_dimension_mismatch_blocks_can_start_and_raises_replan(self) -> None:
        nether_world = _SurveyGridWorld(
            blocks=_flat_ground("netherrack"),
            dimension="the_nether",
            biome="nether_wastes",
        )
        ex, state = nether_world.session()
        skill = SurveyIsland(dimension="overworld", sectors=4, radius=32)
        self.assertFalse(skill.can_start(state))
        with self.assertRaises(Replan):
            drive(skill, ex, state)
        self.assertEqual(ex.acts, [])


class DiscoverStructureSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_all_world_memos()
        reset_all_voxel_caches()
        _COOLDOWN.clear()

    def test_discover_stronghold_in_overworld(self) -> None:
        extra = {
            (20, 63, 0): "stone_bricks",
            (21, 63, 0): "mossy_stone_bricks",
            (22, 63, 0): "end_portal_frame",
        }
        world = _SurveyGridWorld(
            blocks=_flat_ground("grass_block", hazards=extra),
            dimension="overworld",
            biome="plains",
            items={"stone_pickaxe": 1},
        )
        ex, state = world.session()
        skill = make("discover_structure", {"structure": "stronghold", "radius": 48})
        self.assertTrue(skill.can_start(state))

        drive(skill, ex, state)
        self.assertTrue(world.move_calls)
        self.assertTrue(memo_for(state).has_landmark("stronghold", dimension="overworld"))
        self.assertLessEqual(math.hypot(world.st["pos"][0] - 20.5, world.st["pos"][2] - 0.5), 10.0)
        self.assertEqual(skill.verify_done(state), "")

    def test_discover_trial_chambers_in_overworld(self) -> None:
        extra = {
            (0, 63, 20): "trial_spawner",
            (1, 63, 20): "vault",
        }
        world = _SurveyGridWorld(
            blocks=_flat_ground("grass_block", hazards=extra),
            dimension="overworld",
            biome="plains",
            items={"stone_pickaxe": 1},
        )
        ex, state = world.session()
        skill = DiscoverStructure(structure="trial_chambers", radius=48)
        self.assertTrue(skill.can_start(state))

        drive(skill, ex, state)
        self.assertTrue(world.move_calls)
        self.assertTrue(memo_for(state).has_landmark("trial_chambers", dimension="overworld"))
        self.assertEqual(skill.verify_done(state), "")

    def test_discover_nether_fortress_in_nether(self) -> None:
        extra = {
            (18, 63, 0): "nether_bricks",
            (19, 63, 0): "nether_wart",
        }
        world = _SurveyGridWorld(
            blocks=_flat_ground("netherrack", hazards=extra),
            dimension="the_nether",
            biome="nether_wastes",
        )
        ex, state = world.session()
        skill = DiscoverStructure(structure="nether_fortress", radius=48)
        self.assertTrue(skill.can_start(state))

        drive(skill, ex, state)
        self.assertTrue(world.move_calls)
        self.assertTrue(memo_for(state).has_landmark("nether_fortress", dimension="the_nether"))
        self.assertEqual(skill.verify_done(state), "")

    def test_discover_bastion_remnant_in_nether(self) -> None:
        extra = {
            (-18, 63, 0): "polished_blackstone_bricks",
            (-19, 63, 0): "gilded_blackstone",
        }
        world = _SurveyGridWorld(
            blocks=_flat_ground("netherrack", hazards=extra),
            dimension="the_nether",
            biome="nether_wastes",
        )
        ex, state = world.session()
        skill = DiscoverStructure(structure="bastion_remnant", radius=48)
        self.assertTrue(skill.can_start(state))

        drive(skill, ex, state)
        self.assertTrue(world.move_calls)
        self.assertTrue(memo_for(state).has_landmark("bastion_remnant", dimension="the_nether"))
        self.assertEqual(skill.verify_done(state), "")

    def test_discover_nether_biomes_finds_new_biome(self) -> None:
        def nether_biome_at(pos: list[float]) -> str:
            return "crimson_forest" if math.hypot(pos[0], pos[2]) >= 8.0 else "nether_wastes"

        world = _SurveyGridWorld(
            blocks=_flat_ground("netherrack"),
            dimension="the_nether",
            biome_fn=nether_biome_at,
        )
        ex, state = world.session()
        memo = memo_for(state)
        memo.record_biome("nether_wastes", dimension="the_nether")

        skill = DiscoverStructure(structure="nether_biomes", radius=32)
        self.assertTrue(skill.can_start(state))
        drive(skill, ex, state)

        self.assertTrue(world.move_calls)
        self.assertIn("crimson_forest", memo.biomes(dimension="the_nether"))
        self.assertEqual(skill.verify_done(state), "")

    def test_discover_nether_biomes_already_all_five_completes_without_moving(self) -> None:
        self.assertEqual(
            NETHER_BIOMES,
            frozenset({"nether_wastes", "soul_sand_valley", "crimson_forest", "warped_forest", "basalt_deltas"}),
        )
        world = _SurveyGridWorld(
            blocks=_flat_ground("netherrack"),
            dimension="the_nether",
            biome="nether_wastes",
        )
        ex, state = world.session()
        memo = memo_for(state)
        for biome in NETHER_BIOMES:
            memo.record_biome(biome, dimension="the_nether")

        skill = DiscoverStructure(structure="nether_biomes", radius=32)
        self.assertTrue(skill.can_start(state))
        drive(skill, ex, state)

        self.assertEqual(world.move_calls, [])
        self.assertEqual(ex.acts, [])
        self.assertEqual(skill.verify_done(state), "")

    def test_discover_nether_biomes_no_new_biome_raises_env_blocked(self) -> None:
        world = _SurveyGridWorld(
            blocks=_flat_ground("netherrack"),
            dimension="the_nether",
            biome="nether_wastes",
        )
        ex, state = world.session()
        memo_for(state).record_biome("nether_wastes", dimension="the_nether")

        skill = DiscoverStructure(structure="nether_biomes", radius=32)
        with self.assertRaises(EnvBlocked) as caught:
            drive(skill, ex, state)
        self.assertEqual(caught.exception.wake_on, "chunk_moved")

    def test_dimension_guards_for_nether_and_overworld_structures(self) -> None:
        overworld = _SurveyGridWorld(blocks=_flat_ground("grass_block"), dimension="overworld")
        for struct in ("nether_fortress", "bastion_remnant", "nether_biomes"):
            with self.subTest(dimension="overworld", structure=struct):
                ex, state = overworld.session()
                skill = DiscoverStructure(structure=struct, radius=32)
                self.assertFalse(skill.can_start(state))
                with self.assertRaises(Replan):
                    drive(skill, ex, state)
                self.assertEqual(ex.acts, [])

        nether = _SurveyGridWorld(blocks=_flat_ground("netherrack"), dimension="the_nether")
        for struct in ("stronghold", "trial_chambers"):
            with self.subTest(dimension="the_nether", structure=struct):
                ex, state = nether.session()
                skill = DiscoverStructure(structure=struct, radius=32)
                self.assertFalse(skill.can_start(state))
                with self.assertRaises(Replan):
                    drive(skill, ex, state)
                self.assertEqual(ex.acts, [])

    def test_missing_structure_in_all_sectors_raises_env_blocked(self) -> None:
        overworld = _SurveyGridWorld(blocks=_flat_ground("grass_block"), dimension="overworld")
        ex_ow, st_ow = overworld.session()
        with self.assertRaises(EnvBlocked) as caught_ow:
            drive(DiscoverStructure(structure="stronghold", radius=32), ex_ow, st_ow)
        self.assertEqual(caught_ow.exception.wake_on, "chunk_moved")

        nether = _SurveyGridWorld(blocks=_flat_ground("netherrack"), dimension="the_nether", biome="nether_wastes")
        ex_ne, st_ne = nether.session()
        with self.assertRaises(EnvBlocked) as caught_ne:
            drive(DiscoverStructure(structure="nether_fortress", radius=32), ex_ne, st_ne)
        self.assertEqual(caught_ne.exception.wake_on, "chunk_moved")


if __name__ == "__main__":
    unittest.main()
