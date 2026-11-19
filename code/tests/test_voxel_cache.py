"""Offline unit tests for mcagent.skills.voxel_cache."""
from __future__ import annotations

import unittest

from mcagent.runtime import State
from mcagent.skills.voxel_cache import (
    VoxelCache,
    reset_all_voxel_caches,
    voxel_cache_for,
)


class _FakeClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class _FakeClient:
    def __init__(self, blocks: dict[tuple[int, int, int], dict] | None = None) -> None:
        self._block_cache_gen = 0
        self.blocks: dict[tuple[int, int, int], dict] = dict(blocks or {})
        self.calls: list[list[int]] = []

    def get_block(self, pos) -> dict:
        cell = [int(pos[0]), int(pos[1]), int(pos[2])]
        self.calls.append(cell)
        blk = self.blocks.get(tuple(cell))
        if blk is None:
            return {"name": "air", "pos": cell}
        return dict(blk)


class VoxelCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_voxel_caches()
        self.addCleanup(reset_all_voxel_caches)

    def test_ingest_find_blocks_list_and_summary_formats(self) -> None:
        clock = _FakeClock()
        cache = VoxelCache(clock=clock)
        client = _FakeClient({(1, 64, 2): {"name": "stone", "pos": [1, 64, 2]}})

        list_count = cache.ingest_find_blocks(
            {"blocks": [{"name": "minecraft:oak_log", "pos": [1, 64, 2]}]},
        )
        self.assertEqual(list_count, 1)

        blk = cache.get_block(client, [1, 64, 2])
        self.assertEqual(blk["name"], "oak_log")
        self.assertEqual(blk["pos"], [1, 64, 2])
        self.assertEqual(client.calls, [])

        # Mutating returned dict must not corrupt cached entry
        blk["name"] = "dirt"
        blk2 = cache.get_block(client, [1, 64, 2])
        self.assertEqual(blk2["name"], "oak_log")

        # Summary dict format
        summary_count = cache.ingest_find_blocks(
            {"blocks": {"minecraft:iron_ore": {"nearest": [5, 30, -4], "dist": 3.0}}},
        )
        self.assertEqual(summary_count, 1)

        ore = cache.get_block(client, [5, 30, -4])
        self.assertEqual(ore["name"], "iron_ore")
        self.assertEqual(ore["pos"], [5, 30, -4])
        self.assertEqual(ore["dist"], 3.0)
        self.assertEqual(client.calls, [])

        # Empty / invalid inputs return 0
        self.assertEqual(cache.ingest_find_blocks({}), 0)
        self.assertEqual(cache.ingest_find_blocks({"blocks": []}), 0)
        self.assertEqual(cache.ingest_find_blocks({"blocks": [{"name": "oak_log", "pos": [1, 2, 3]}]}, ttl_s=0), 0)

    def test_get_block_counters_force_ttl_and_dimension_isolation(self) -> None:
        clock = _FakeClock(1000.0)
        cache = VoxelCache(default_ttl_s=10.0, clock=clock)
        client = _FakeClient(
            {
                (2, 65, 3): {"name": "minecraft:cobblestone", "pos": [2, 65, 3]},
                (9, 65, 9): {"name": "minecraft:netherrack", "pos": [9, 65, 9]},
            }
        )

        # First fetch -> miss
        res1 = cache.get_block(client, [2, 65, 3], dimension="overworld", ttl_s=5.0)
        self.assertEqual(res1["name"], "minecraft:cobblestone")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(cache.stats(), {"hits": 0, "misses": 1, "ingested": 0, "blacklisted": 0})

        # Second fetch -> hit
        res2 = cache.get_block(client, [2, 65, 3], dimension="overworld", ttl_s=5.0)
        self.assertEqual(res2["name"], "minecraft:cobblestone")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(cache.stats(), {"hits": 1, "misses": 1, "ingested": 0, "blacklisted": 0})

        # Dimension isolation: same coords in the_nether must miss
        client.blocks[(2, 65, 3)] = {"name": "minecraft:basalt", "pos": [2, 65, 3]}
        res_nether = cache.get_block(client, [2, 65, 3], dimension="the_nether", ttl_s=5.0)
        self.assertEqual(res_nether["name"], "minecraft:basalt")
        self.assertEqual(len(client.calls), 2)
        # Overworld entry remains untouched
        res_ow_again = cache.get_block(client, [2, 65, 3], dimension="overworld", ttl_s=5.0)
        self.assertEqual(res_ow_again["name"], "minecraft:cobblestone")
        self.assertEqual(len(client.calls), 2)

        # force=True bypasses cache and updates cached value
        client.blocks[(2, 65, 3)] = {"name": "minecraft:stone", "pos": [2, 65, 3]}
        res_forced = cache.get_block(client, [2, 65, 3], dimension="overworld", ttl_s=5.0, force=True)
        self.assertEqual(res_forced["name"], "minecraft:stone")
        self.assertEqual(len(client.calls), 3)
        res_after_force = cache.get_block(client, [2, 65, 3], dimension="overworld", ttl_s=5.0)
        self.assertEqual(res_after_force["name"], "minecraft:stone")
        self.assertEqual(len(client.calls), 3)

        # TTL expiration triggers re-fetch (now == expires_at is expired)
        clock.advance(5.0)
        client.blocks[(2, 65, 3)] = {"name": "minecraft:granite", "pos": [2, 65, 3]}
        res_expired = cache.get_block(client, [2, 65, 3], dimension="overworld", ttl_s=5.0)
        self.assertEqual(res_expired["name"], "minecraft:granite")
        self.assertEqual(len(client.calls), 4)

    def test_block_cache_gen_auto_invalidation_and_post_bump_ingest(self) -> None:
        clock = _FakeClock()
        cache = VoxelCache(clock=clock)
        client = _FakeClient(
            {
                (0, 64, 0): {"name": "oak_log", "pos": [0, 64, 0]},
                (1, 64, 0): {"name": "oak_leaves", "pos": [1, 64, 0]},
            }
        )

        # Populate via get_block at gen=0
        self.assertEqual(cache.get_block(client, [0, 64, 0])["name"], "oak_log")
        self.assertEqual(cache.get_block(client, [1, 64, 0])["name"], "oak_leaves")
        self.assertEqual(len(client.calls), 2)

        # Bump generation and update world block
        client._block_cache_gen += 1
        client.blocks[(0, 64, 0)] = {"name": "air", "pos": [0, 64, 0]}

        # Fresh ingest after gen bump (without client arg) must survive gen sync,
        # while stale pre-bump entry at [0, 64, 0] is dropped.
        ingested = cache.ingest_find_blocks(
            {"blocks": [{"name": "minecraft:birch_log", "pos": [5, 64, 5]}]},
        )
        self.assertEqual(ingested, 1)

        # Querying [0, 64, 0] triggers gen sync -> stale entry dropped, re-fetched
        self.assertEqual(cache.get_block(client, [0, 64, 0])["name"], "air")
        self.assertEqual(len(client.calls), 3)

        # Fresh post-bump ingested entry at [5, 64, 5] hits cache without RPC
        self.assertEqual(cache.get_block(client, [5, 64, 5])["name"], "birch_log")
        self.assertEqual(len(client.calls), 3)

        # Pre-bump entry at [1, 64, 0] was also dropped during gen sync
        client.blocks[(1, 64, 0)] = {"name": "stick_block", "pos": [1, 64, 0]}
        self.assertEqual(cache.get_block(client, [1, 64, 0])["name"], "stick_block")
        self.assertEqual(len(client.calls), 4)

        # Also verify ingest_find_blocks(..., client=client) after another gen bump
        client._block_cache_gen += 1
        cache.ingest_find_blocks(
            {"blocks": [{"name": "spruce_log", "pos": [7, 64, 7]}]},
            client=client,
        )
        self.assertEqual(cache.get_block(client, [7, 64, 7])["name"], "spruce_log")
        self.assertEqual(len(client.calls), 4)
        # Entry from gen 1 ([0, 64, 0]) was invalidated by gen 2
        self.assertEqual(cache.get_block(client, [0, 64, 0])["name"], "air")
        self.assertEqual(len(client.calls), 5)

    def test_invalidate_and_invalidate_radius(self) -> None:
        clock = _FakeClock()
        cache = VoxelCache(clock=clock)
        client = _FakeClient()

        # Seed a 3x3x3 cube around (10, 64, 10) plus a distant block and another dimension
        blocks = []
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                for dz in range(-1, 2):
                    blocks.append({"name": "stone", "pos": [10 + dx, 64 + dy, 10 + dz]})
        blocks.append({"name": "diamond_ore", "pos": [12, 64, 10]})
        cache.ingest_find_blocks({"blocks": blocks}, dimension="overworld")
        cache.ingest_find_blocks({"blocks": [{"name": "netherrack", "pos": [10, 64, 10]}]}, dimension="the_nether")

        # Single-cell invalidate only removes [10, 64, 10] in overworld
        cache.invalidate([10, 64, 10], dimension="overworld")
        self.assertEqual(cache.get_block(client, [10, 64, 10], dimension="overworld")["name"], "air")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(cache.get_block(client, [11, 64, 10], dimension="overworld")["name"], "stone")
        self.assertEqual(cache.get_block(client, [10, 64, 10], dimension="the_nether")["name"], "netherrack")
        self.assertEqual(len(client.calls), 1)

        # Re-ingest center then invalidate_radius(radius=1)
        cache.ingest_find_blocks({"blocks": [{"name": "stone", "pos": [10, 64, 10]}]}, dimension="overworld")
        cache.invalidate_radius([10, 64, 10], radius=1, dimension="overworld")

        # Distant entry at [12, 64, 10] (Chebyshev dist 2) and nether entry stay cached
        self.assertEqual(cache.get_block(client, [12, 64, 10], dimension="overworld")["name"], "diamond_ore")
        self.assertEqual(cache.get_block(client, [10, 64, 10], dimension="the_nether")["name"], "netherrack")
        self.assertEqual(len(client.calls), 1)

        # All 27 cells in the 3x3x3 region were invalidated -> each triggers client.get_block
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                for dz in range(-1, 2):
                    res = cache.get_block(client, [10 + dx, 64 + dy, 10 + dz], dimension="overworld")
                    self.assertEqual(res["name"], "air")
        self.assertEqual(len(client.calls), 1 + 27)

    def test_unreachable_blacklist_and_clear_if_moved(self) -> None:
        clock = _FakeClock(500.0)
        cache = VoxelCache(clock=clock)

        # Stance-scoped entry for chop_tree in overworld
        cache.mark_unreachable(
            "chop_tree",
            [10, 65, 10],
            dimension="overworld",
            stance_cell=[8, 64, 10],
            ttl_s=30.0,
        )
        # Unconditional entry (stance_cell=None) for mine_ore
        cache.mark_unreachable(
            "mine_ore",
            [20, 40, 20],
            dimension="overworld",
            stance_cell=None,
            ttl_s=30.0,
        )

        # Matching skill/dim/stance -> True
        self.assertTrue(cache.is_unreachable("chop_tree", [10, 65, 10], dimension="overworld", stance_cell=[8, 64, 10]))
        # Querying without stance_cell still matches
        self.assertTrue(cache.is_unreachable("chop_tree", [10, 65, 10], dimension="overworld"))
        # Different stance_cell -> False
        self.assertFalse(cache.is_unreachable("chop_tree", [10, 65, 10], dimension="overworld", stance_cell=[9, 64, 10]))
        # Skill isolation -> False
        self.assertFalse(cache.is_unreachable("mine_ore", [10, 65, 10], dimension="overworld", stance_cell=[8, 64, 10]))
        # Dimension isolation -> False
        self.assertFalse(cache.is_unreachable("chop_tree", [10, 65, 10], dimension="the_nether", stance_cell=[8, 64, 10]))

        # Unconditional entry matches regardless of stance_cell
        self.assertTrue(cache.is_unreachable("mine_ore", [20, 40, 20], dimension="overworld", stance_cell=[0, 64, 0]))

        # Move < 6.0 blocks from stance center (8.5, 64.0, 10.5) -> returns False and keeps entry
        self.assertFalse(cache.clear_stance_blacklist_if_moved([14.4, 64.0, 10.5], min_dist=6.0))
        self.assertTrue(cache.is_unreachable("chop_tree", [10, 65, 10], dimension="overworld", stance_cell=[8, 64, 10]))

        # Move >= 6.0 blocks -> returns True, clears stance-scoped entry, preserves unconditional entry
        self.assertTrue(cache.clear_stance_blacklist_if_moved([14.5, 64.0, 10.5], min_dist=6.0))
        self.assertFalse(cache.is_unreachable("chop_tree", [10, 65, 10], dimension="overworld", stance_cell=[8, 64, 10]))
        self.assertTrue(cache.is_unreachable("mine_ore", [20, 40, 20], dimension="overworld", stance_cell=[0, 64, 0]))

        # TTL expiration clears unconditional entry as well
        clock.advance(30.0)
        self.assertFalse(cache.is_unreachable("mine_ore", [20, 40, 20], dimension="overworld"))
        self.assertEqual(cache.stats()["blacklisted"], 0)

    def test_stats_exact_counts(self) -> None:
        clock = _FakeClock(10.0)
        cache = VoxelCache(clock=clock)
        client = _FakeClient({(3, 64, 3): {"name": "stone", "pos": [3, 64, 3]}})

        cache.ingest_find_blocks(
            {
                "blocks": [
                    {"name": "oak_log", "pos": [1, 64, 1]},
                    {"name": "oak_log", "pos": [2, 64, 2]},
                ]
            }
        )
        cache.get_block(client, [1, 64, 1])  # hit 1
        cache.get_block(client, [2, 64, 2])  # hit 2
        cache.get_block(client, [3, 64, 3])  # miss 1 (cached)
        cache.get_block(client, [3, 64, 3])  # hit 3

        cache.mark_unreachable("chop_tree", [1, 64, 1], ttl_s=5.0)
        cache.mark_unreachable("mine_ore", [3, 64, 3], ttl_s=20.0)

        self.assertEqual(
            cache.stats(),
            {"hits": 3, "misses": 1, "ingested": 2, "blacklisted": 2},
        )

        clock.advance(5.0)
        self.assertEqual(
            cache.stats(),
            {"hits": 3, "misses": 1, "ingested": 2, "blacklisted": 1},
        )

    def test_voxel_cache_for_state_and_reset(self) -> None:
        s1 = State()
        s2 = State()

        c1_a = voxel_cache_for(s1)
        c1_b = voxel_cache_for(s1)
        c2 = voxel_cache_for(s2)

        self.assertIsInstance(c1_a, VoxelCache)
        self.assertIs(c1_a, c1_b)
        self.assertIsNot(c1_a, c2)

        reset_all_voxel_caches()
        c1_after = voxel_cache_for(s1)
        c2_after = voxel_cache_for(s2)
        self.assertIsNot(c1_after, c1_a)
        self.assertIsNot(c2_after, c2)
        self.assertIsNot(c1_after, c2_after)


if __name__ == "__main__":
    unittest.main()
