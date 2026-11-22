"""Regression tests for Run-21: craft updateSlot timeout handling and naked surface cave-descent guard."""
from __future__ import annotations

import unittest

from mcagent.client import ApiError
from mcagent.loop.starter_skills import ScoutIsland
from mcagent.runtime import Replan
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.world_memo import reset_all_world_memos


class _CraftTimeoutWorld(GridWorld):
    def craft(self, item, count=1, *, stop_on=None):
        raise ApiError("bad_args", "Error: Event updateSlot:0 did not fire within timeout of 20000ms")


class _CaveSlopeScoutWorld(GridWorld):
    def __init__(self):
        blocks = {
            # Deep cave candidate at Y=50 (dist=20, would win by -dist if unfiltered)
            (20, 50, 0): "stone",
            (20, 51, 0): "air",
            (20, 52, 0): "air",
            # Safe surface candidate at Y=60 (dist=10)
            (0, 60, 10): "grass_block",
            (0, 61, 10): "air",
            (0, 62, 10): "air",
        }
        super().__init__(pos=[0.5, 60.0, 0.5], blocks=blocks, items={"oak_log": 4})
        self.st.update(dimension="overworld", ready=True, alive=True, on_ground=True, health=20.0, food=20.0)
        self.moved_to: list[list[float]] = []

    def find_blocks(self, types=None, radius=32, limit=64, **kwargs):
        wanted = set(types or [])
        hits = []
        for (bx, by, bz), name in self.blocks.items():
            if wanted and name not in wanted:
                continue
            hits.append({"name": name, "pos": [bx, by, bz], "distance": 10.0})
        return {"ready": True, "blocks": hits[: int(limit)]}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None):
        dest = list(pos) if pos is not None else [x, y, z]
        self.moved_to.append(dest)
        self.st["pos"] = [float(dest[0]), float(dest[1]), float(dest[2])]
        return {"pos": list(self.st["pos"])}


class TestRun21CraftAbortAndCaveGuard(unittest.TestCase):
    def setUp(self):
        reset_all_world_memos()

    def tearDown(self):
        reset_all_world_memos()

    def test_runtime_craft_update_slot_timeout_does_not_surface_as_bad_args(self):
        w = _CraftTimeoutWorld(pos=[0.5, 64.0, 0.5], items={"oak_log": 4})
        ex, _ = w.session()
        with self.assertRaises(Replan) as ctx:
            ex.craft("oak_planks", 4)
        # Must NOT leak raw bad_args cause (which triggers P0 CONTRACT_OR_FATAL_ERROR)
        cause = ctx.exception.__cause__
        self.assertNotEqual(getattr(cause, "code", None), "bad_args")

    def test_scout_island_skips_subsurface_cave_drop_when_naked_on_surface(self):
        w = _CaveSlopeScoutWorld()
        ex, st = w.session()
        drive(ScoutIsland(radius=28), ex, st)
        self.assertTrue(w.moved_to, "expected scout_island to move to safe surface")
        self.assertTrue(all(p[1] >= 54.0 for p in w.moved_to), f"naked surface bot descended into cave: {w.moved_to}")


if __name__ == "__main__":
    unittest.main()
