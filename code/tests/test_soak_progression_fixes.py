"""Offline regression tests for soak-deep-20261121 progression-blocker fixes (seg-24..seg-31)."""
from __future__ import annotations

import unittest

from mcagent.loop.goals import (
    GOAL_MAP,
    GOALS,
    eval_item_expr,
    rank_actionable_goals,
    survival_readiness_multiplier,
)
from mcagent.runtime import Executor, RuntimeConfig, State
from mcagent.skills.camp_plan import compute_deposit_plan, compute_withdraw_plan
from mcagent.skills.furnace_lease import reset_all_furnace_leases
from mcagent.skills.iron import SmeltIron
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


def _make_state(
    *,
    items: dict[str, int],
    pos: list[float] | None = None,
    dimension: str = "overworld",
    health: float = 20.0,
    is_day: bool = True,
    armor: dict | None = None,
) -> State:
    st = State(RuntimeConfig())
    st.set_status(
        {
            "pos": list(pos or [-63.5, 69.0, 27.5]),
            "dimension": dimension,
            "ready": True,
            "alive": True,
            "health": health,
            "food": 20.0,
            "is_day": is_day,
            "time_of_day": 3000 if is_day else 15000,
        }
    )
    inv_items = [{"name": k, "count": v, "slot": i} for i, (k, v) in enumerate(items.items()) if v > 0]
    st.set_inventory(
        {
            "items": inv_items,
            "armor": dict(armor or {}),
            "empty_slots": max(0, 36 - len(inv_items)),
        }
    )
    return st


class _DropRollbackIronWorld(GridWorld):
    """Simulates iron_ore mined with collect=True leaving raw_iron drop 2 blocks away."""

    def __init__(self):
        floor = {(x, 30, z): "stone" for x in range(-4, 6) for z in range(-4, 6)}
        floor[(2, 31, 0)] = "iron_ore"
        super().__init__(
            blocks=floor,
            items={"stone_pickaxe": 1, "furnace": 1, "coal": 4},
            pos=[0.5, 31.0, 0.5],
        )
        self.drop_entities = [
            {"id": 9001, "type": "item", "name": "item", "item": "raw_iron", "pos": [2.5, 31.0, 0.5]},
        ]
        self.drop_spawned = False

    def dig(self, pos, collect=True, **kwargs):
        key = tuple(int(v) for v in pos)
        if self.blocks.get(key) == "iron_ore":
            self.blocks.pop(key, None)
            self.drop_spawned = True
            return {"ok": True, "dug": list(key)}
        return {"ok": True}

    def get_entities(self, radius=16, types=None, **kwargs):
        if self.drop_spawned and self.drop_entities:
            return {"ready": True, "entities": list(self.drop_entities)}
        return {"ready": True, "entities": []}

    def move_to(self, *args, **kwargs):
        res = super().move_to(*args, **kwargs)
        if self.drop_spawned and self.drop_entities:
            p = self.st["pos"]
            dx = abs(p[0] - 2.5)
            dz = abs(p[2] - 0.5)
            if dx <= 2.0 and dz <= 2.0:
                self.drop_entities.clear()
                self.items["raw_iron"] = self.items.get("raw_iron", 0) + 1
        return res


class ProgressionFixesTests(unittest.TestCase):
    def tearDown(self):
        reset_all_world_memos()
        reset_all_furnace_leases()

    def test_blocking_iron_goals_use_synchronous_smelt_not_async_lease(self):
        """seg-30/31: early iron goals must smelt synchronously instead of abandoning cave furnaces."""
        self.assertFalse(GOAL_MAP["smelt_iron"].args_dict.get("async_lease", False))
        for gid in ("iron_tools", "obtain_armor", "lava_bucket", "axolotl_in_a_bucket", "wax_on"):
            for _, sk_name, sk_args in GOAL_MAP[gid].gather_fallbacks:
                if sk_name == "smelt_iron":
                    self.assertFalse(
                        dict(sk_args).get("async_lease", False),
                        f"{gid} fallback smelt_iron should not abandon iron via async_lease=True",
                    )

    def test_eval_item_expr_recognizes_memo_crafting_table_for_prep_furnace(self):
        """seg-31: @gear_ready:furnace>=1 uses world_memo crafting_table within 24m (no false gather_wood)."""
        st = _make_state(items={"cobblestone": 16, "stone_pickaxe": 1}, pos=[-63.5, 69.0, 27.5])
        ok_before, _, _, _ = eval_item_expr(st, "@gear_ready:furnace>=1")
        self.assertFalse(ok_before)

        memo_for(st).record_station("crafting_table", [-64, 69, 27], dimension="overworld")
        ok_after, _, _, _ = eval_item_expr(st, "@gear_ready:furnace>=1")
        self.assertTrue(ok_after)

    def test_camp_withdraw_unlocks_stored_raw_iron_without_redeposit_pingpong(self):
        """seg-31: 11 raw_iron in camp chest must be withdrawable and not immediately re-deposited."""
        st = _make_state(items={"stone_pickaxe": 1, "stone_sword": 1})
        stored = {"lapis_lazuli": 18, "raw_iron": 11}
        wd = dict(compute_withdraw_plan(st, stored))
        self.assertEqual(wd.get("raw_iron"), 6)

        st_after = _make_state(items={"stone_pickaxe": 1, "stone_sword": 1, "raw_iron": 6})
        dep = dict(compute_deposit_plan(st_after))
        self.assertNotIn("raw_iron", dep)

    def test_unarmored_overworld_prioritizes_armor_over_nether_strider_and_allay(self):
        """seg-24..31: after respawn (story/iron_tools done, no armor), obtain_armor beats ride_strider & allay."""
        st = _make_state(
            items={"stone_pickaxe": 1, "stone_sword": 1, "cooked_beef": 6, "furnace": 1, "raw_iron": 6},
            is_day=True,
        )
        memo = memo_for(st)
        memo.record_landmark("pillager_outpost", [100, 68, 100], dimension="overworld")
        completed = {
            "minecraft:story/mine_stone",
            "minecraft:story/upgrade_tools",
            "minecraft:story/smelt_iron",
            "minecraft:story/iron_tools",
            "minecraft:adventure/kill_a_mob",
            "minecraft:husbandry/plant_seed",
        }
        m_armor = survival_readiness_multiplier(
            GOAL_MAP["obtain_armor"], GOAL_MAP["obtain_armor"], st, completed, memo=memo, is_day=True,
        )
        m_strider = survival_readiness_multiplier(
            GOAL_MAP["smelt_iron"], GOAL_MAP["ride_strider_in_overworld_lava"], st, completed, memo=memo, is_day=True,
        )
        m_allay = survival_readiness_multiplier(
            GOAL_MAP["allay_deliver_item_to_player"],
            GOAL_MAP["allay_deliver_item_to_player"],
            st,
            completed,
            memo=memo,
            is_day=True,
        )
        self.assertAlmostEqual(m_armor, 3.5)
        self.assertAlmostEqual(m_strider, 0.2)
        self.assertAlmostEqual(m_allay, 0.25)

        ranked = rank_actionable_goals(st, GOALS, completed_advs=completed, memo=memo, is_day=True)
        root_ids = [step.root_goal.id for step in ranked]
        self.assertIn("obtain_armor", root_ids)
        if "ride_strider_in_overworld_lava" in root_ids:
            self.assertLess(root_ids.index("obtain_armor"), root_ids.index("ride_strider_in_overworld_lava"))
        if "allay_deliver_item_to_player" in root_ids:
            self.assertLess(root_ids.index("obtain_armor"), root_ids.index("allay_deliver_item_to_player"))

    def test_smelt_iron_mine_collects_dropped_raw_iron_before_drop_uncollected(self):
        """seg-30: SmeltIron._mine must try collect_drops when dig(collect=True) leaves raw_iron nearby."""
        world = _DropRollbackIronWorld()
        ex, state = world.session()
        skill = SmeltIron(count=1)
        flow = skill._mine(ex, state, [2, 31, 0], ("iron_ore",), "raw_iron")
        for _ in flow:
            pass
        self.assertEqual(state.count("raw_iron"), 1)


if __name__ == "__main__":
    unittest.main()
