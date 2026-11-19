"""Stage 3 Base Extensions & Wave A (7 T4 Skills + 9 Goals) offline unit tests."""
from __future__ import annotations

import math
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from mcagent.client import ApiError
from mcagent.loop.draft_scores import ADVANCEMENT_SCORES_V2
from mcagent.loop.goals import (
    GOAL_MAP,
    GOAL_TIERS,
    GOALS,
    GoalSpec,
    rank_actionable_goals,
    resolve_active_goals,
    validate_ledger,
)
from mcagent.loop.subgoals import (
    _KNOWN_STATIONS,
    advancement_criteria_remaining,
    enchantment_ready,
    evaluate_all_subgoals,
    mob_collection_ready,
    ominous_ready,
    station_ready,
)
from mcagent.runtime import EnvBlocked, Replan, State
from mcagent.skills import REGISTRY, bind_args, contract_errors
from mcagent.skills.t4_end_and_frogs import LeashFrogVariants, LootEndShipElytra
from mcagent.skills.t4_quick_exploits import (
    AllayCakeBirthday,
    CrafterChain,
    DecoratedPotSherds,
    JukeboxMeadow,
    SweepOutpostCaptain,
)
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches
from mcagent.skills.world_memo import (
    CATEGORY_BLOCKS,
    STRUCTURE_FINGERPRINTS,
    WorldResourceMemo,
    classify_block,
    memo_for,
    reset_all_world_memos,
    structure_tags_for,
)

WAVE_A_PREP_IDS: tuple[str, ...] = (
    "prep_ominous_bottle",
    "prep_two_cakes",
)
WAVE_A_SCORED_IDS: tuple[str, ...] = (
    "crafters_crafting_crafters",
    "craft_decorated_pot_using_only_sherds",
    "play_jukebox_in_meadows",
    "voluntary_exile",
    "allay_deliver_cake_to_note_block",
    "elytra",
    "leash_all_frog_variants",
)
WAVE_A_ALL_IDS: tuple[str, ...] = WAVE_A_PREP_IDS + WAVE_A_SCORED_IDS


def _flat(x0: int, x1: int, z0: int, z1: int, y: int = 63, block: str = "stone") -> dict[tuple[int, int, int], str]:
    return {(x, y, z): block for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)}


def _state_with(
    *,
    ready: bool = True,
    alive: bool = True,
    dimension: str = "overworld",
    biome: str = "plains",
    health: float = 20.0,
    food: float = 20.0,
    pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
    items: list[dict[str, Any]] | None = None,
    held: dict[str, Any] | None = None,
    offhand: dict[str, Any] | None = None,
    armor: dict[str, dict[str, Any]] | None = None,
    dirty: bool = False,
    effects: Any = None,
    extra_status: dict[str, Any] | None = None,
) -> State:
    state = State()
    st: dict[str, Any] = {
        "ready": ready,
        "alive": alive,
        "dimension": dimension,
        "biome": biome,
        "health": health,
        "food": food,
        "pos": list(pos),
        "on_ground": True,
        "sneaking": False,
        "blocking": False,
    }
    if effects is not None:
        st["effects"] = effects
    if extra_status:
        st.update(extra_status)
    state.set_status(st)
    inv: dict[str, Any] = {"items": list(items or [])}
    if held is not None:
        inv["held"] = dict(held)
    if offhand is not None:
        inv["offhand"] = dict(offhand)
    if armor is not None:
        inv["armor"] = deepcopy(armor)
    state.set_inventory(inv)
    if dirty:
        state.invalidate_inventory()
    return state


class WaveAWorld(GridWorld):
    """Offline deterministic world double supporting all Stage 3 Wave A client methods."""

    def __init__(
        self,
        pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
        *,
        dimension: str = "overworld",
        biome: str = "plains",
        blocks: dict[tuple[int, int, int], str] | None = None,
        items: dict[str, int] | None = None,
        entities: list[dict[str, Any]] | None = None,
        hp: float = 20.0,
        food: float = 20.0,
        adv_snapshot: dict[str, Any] | None = None,
    ):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st.update(
            dimension=dimension,
            biome=biome,
            health=hp,
            food=food,
            blocking=False,
            jumping=False,
        )
        self.entities: list[dict[str, Any]] = [deepcopy(e) for e in (entities or ())]
        self.held: dict[str, Any] | None = None
        self.adv_snapshot: dict[str, Any] = deepcopy(adv_snapshot) if adv_snapshot is not None else {"advancements": []}

        self.on_move_to: Callable[..., dict | None] | None = None
        self.on_dig: Callable[..., dict | None] | None = None
        self.on_craft: Callable[..., dict | None] | None = None
        self.on_interact: Callable[..., dict | None] | None = None
        self.on_use_item: Callable[..., dict | None] | None = None
        self.on_attack: Callable[..., dict | None] | None = None
        self.on_shoot: Callable[..., dict | None] | None = None
        self.on_look: Callable[..., dict | None] | None = None
        self.on_control: Callable[..., dict | None] | None = None
        self.on_window_view: Callable[..., dict] | None = None
        self.on_window_act: Callable[..., dict] | None = None
        self.on_drop: Callable[..., dict | None] | None = None
        self.on_advancements: Callable[..., dict] | None = None

    def inventory(self, timeout: float = 10) -> dict:
        inv = super().inventory(timeout)
        if self.held is not None and self.items.get(self.held["name"], 0) > 0:
            inv["held"] = dict(self.held)
        return inv

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None) -> dict:
        self.calls.append(("find_blocks", {"types": types, "radius": radius, "limit": limit}))
        if isinstance(types, str):
            wanted = {types.removeprefix("minecraft:")}
        else:
            wanted = {str(t).removeprefix("minecraft:") for t in (types or ())}
        px, py, pz = self.st["pos"]
        hits = []
        for (bx, by, bz), name in sorted(self.blocks.items()):
            bname = name.removeprefix("minecraft:")
            if wanted and bname not in wanted:
                continue
            d = math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5))
            if d <= radius + 0.5:
                hits.append({"name": bname, "pos": [bx, by, bz], "dist": round(d, 2)})
        hits.sort(key=lambda b: b["dist"])
        return {"blocks": hits[:limit]}

    def get_entities(self, radius=32, types=None) -> dict:
        self.calls.append(("get_entities", {"radius": radius, "types": types}))
        if isinstance(types, str):
            wanted = {types.removeprefix("minecraft:")}
        else:
            wanted = {str(t).removeprefix("minecraft:") for t in (types or ())}
        px, py, pz = self.st["pos"]
        out = []
        for ent in self.entities:
            ename = str(ent.get("name", "")).removeprefix("minecraft:")
            etype = str(ent.get("type", "")).removeprefix("minecraft:")
            if wanted and ename not in wanted and etype not in wanted:
                continue
            d = math.dist((px, py, pz), ent["pos"])
            if d <= radius + 0.5:
                row = deepcopy(ent)
                row.setdefault("dist", round(d, 2))
                out.append(row)
        return {"entities": out}

    def equip(self, item, slot="hand", *, stop_on=None) -> dict:
        res = super().equip(item, slot=slot, stop_on=stop_on)
        if slot == "hand":
            self.held = {"name": item, "count": self.items.get(item, 1)}
        return res

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        if self.on_move_to is not None:
            res = self.on_move_to(self, pos=pos, x=x, y=y, z=z, entity_id=entity_id, rng=rng)
            if res is not None:
                self.calls.append(("move_to", {"x": x, "y": y, "z": z, "entity_id": entity_id, "pos": pos, "rng": rng}))
                return res
        if pos is None and entity_id is not None:
            ent = next((e for e in self.entities if e.get("id") == entity_id), None)
            if ent is None:
                raise ApiError("entity_not_found", f"实体 {entity_id} 不存在")
            pos = [float(v) for v in ent["pos"]]
        return super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)

    def dig(self, pos, collect=True, *, stop_on=None) -> dict:
        if self.on_dig is not None:
            res = self.on_dig(self, pos=list(pos), collect=collect)
            if res is not None:
                self.calls.append(("dig", {"pos": list(pos), "collect": collect}))
                return res
        return super().dig(pos, collect=collect, stop_on=stop_on)

    def craft(self, item, count=1, *, stop_on=None) -> dict:
        self.calls.append(("craft", {"item": item, "count": count}))
        if self.on_craft is not None:
            res = self.on_craft(self, item=item, count=count)
            if res is not None:
                return res
        if item == "lead":
            need = 5 * count
            if self.items.get("string", 0) < need:
                raise ApiError("missing_item", "缺少合成拴绳所需的线")
            self.items["string"] -= need
        elif item == "jukebox":
            if self.items.get("diamond", 0) < count:
                raise ApiError("missing_item", "缺少钻石")
            self.items["diamond"] -= count
        elif item == "note_block":
            if self.items.get("redstone", 0) < count:
                raise ApiError("missing_item", "缺少红石")
            self.items["redstone"] -= count
        self.items[item] = self.items.get(item, 0) + count
        return {"crafted": item, "count": count}

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None) -> dict:
        self.calls.append(("interact", {"pos": list(pos) if pos is not None else None, "entity_id": entity_id, "item": item}))
        if item:
            self._need(item)
        if self.on_interact is not None:
            res = self.on_interact(self, pos=pos, entity_id=entity_id, item=item)
            if res is not None:
                return res
        return {"changed": True}

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None) -> dict:
        self.calls.append(("use_item", {"item": item, "target": deepcopy(target)}))
        self._need(item)
        if self.on_use_item is not None:
            res = self.on_use_item(self, item=item, target=target)
            if res is not None:
                return res
        self.items[item] -= 1
        return {"used": item, "target": target}

    def attack(self, entity_id, timeout_ms=20000, *, max_hits=None, interval_ms=None, stop_on=None) -> dict:
        self.calls.append(("attack", {"entity_id": entity_id, "max_hits": max_hits}))
        if self.on_attack is not None:
            res = self.on_attack(self, entity_id=entity_id, max_hits=max_hits)
            if res is not None:
                return res
        return {"entity_id": entity_id, "hits": max_hits or 1, "killed": True}

    def shoot(self, entity_id=None, pos=None, weapon="bow", charge_ms=1100, *, stop_on=None) -> dict:
        self.calls.append(("shoot", {"entity_id": entity_id, "pos": pos, "weapon": weapon}))
        if self.on_shoot is not None:
            res = self.on_shoot(self, entity_id=entity_id, pos=pos, weapon=weapon)
            if res is not None:
                return res
        return {"hit": True, "entity_id": entity_id, "weapon": weapon}

    def look(self, yaw=None, pitch=None, *, pos=None, entity_id=None) -> dict:
        self.calls.append(("look", {"yaw": yaw, "pitch": pitch, "pos": pos, "entity_id": entity_id}))
        if self.on_look is not None:
            res = self.on_look(self, yaw=yaw, pitch=pitch, pos=pos, entity_id=entity_id)
            if res is not None:
                return res
        return {"ok": True}

    def control(self, **keys) -> dict:
        self.calls.append(("control", dict(keys)))
        if self.on_control is not None:
            res = self.on_control(self, **keys)
            if res is not None:
                return res
        return {"ok": True, **keys}

    def window_view(self, target, *, stop_on=None) -> dict:
        self.calls.append(("window_view", {"target": deepcopy(target)}))
        if self.on_window_view is not None:
            return self.on_window_view(self, target=target)
        return {"options": []}

    def window_act(self, target, ops, *, stop_on=None) -> dict:
        self.calls.append(("window_act", {"target": deepcopy(target), "ops": deepcopy(ops)}))
        if self.on_window_act is not None:
            return self.on_window_act(self, target=target, ops=ops)
        for op in ops:
            if op.get("op") == "put":
                item = op["item"]
                cnt = int(op.get("count", 1))
                if self.items.get(item, 0) < cnt:
                    raise ApiError("missing_item", f"缺少 {item}x{cnt}")
                self.items[item] -= cnt
        return {"ok": True, "ops": deepcopy(ops)}

    def drop(self, item, count=1, *, stop_on=None) -> dict:
        self.calls.append(("drop", {"item": item, "count": count}))
        if self.items.get(item, 0) < count:
            raise ApiError("missing_item", f"缺少 {item}x{count}")
        if self.on_drop is not None:
            res = self.on_drop(self, item=item, count=count)
            if res is not None:
                return res
        self.items[item] -= count
        return {"dropped": item, "count": count}

    def advancements(self, status="all", include_recipes=False) -> dict:
        self.calls.append(("advancements", {"status": status, "include_recipes": include_recipes}))
        if self.on_advancements is not None:
            return self.on_advancements(self, status=status, include_recipes=include_recipes)
        return deepcopy(self.adv_snapshot)


class _WaveATestBase(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        reset_all_voxel_caches()


# ============================================================================
# 1. Base Extensions (GOAL_TIERS, world_memo, subgoals)
# ============================================================================


class Stage3BaseExtensionsTests(_WaveATestBase):
    def test_goal_tiers_exact_order(self) -> None:
        self.assertEqual(GOAL_TIERS, ("T0_PREP", "T1", "T2", "T3", "T4_PREP", "T4"))

    def test_world_memo_five_new_fingerprints_and_structure_tags(self) -> None:
        expected_fingerprints = {
            "meadow": "cornflower",
            "swamp": "lily_pad",
            "mangrove_swamp": "mangrove_roots",
            "cold_biome": "packed_ice",
            "trial_vault_ominous": "ominous_trial_spawner",
        }
        for tag, sample_block in expected_fingerprints.items():
            with self.subTest(fingerprint=tag):
                self.assertIn(tag, STRUCTURE_FINGERPRINTS)
                self.assertIn(sample_block, STRUCTURE_FINGERPRINTS[tag])
                matched = structure_tags_for([f"minecraft:{sample_block}"])
                self.assertIn(tag, matched)
                self.assertIn(sample_block, matched[tag])

    def test_world_memo_five_new_station_blocks_and_classify_block(self) -> None:
        new_stations = ("smithing_table", "jukebox", "note_block", "crafter", "beacon")
        for blk in new_stations:
            with self.subTest(station=blk):
                self.assertIn(blk, CATEGORY_BLOCKS["station"])
                self.assertEqual(classify_block(blk), "station")
                self.assertEqual(classify_block(f"minecraft:{blk}"), "station")

    def test_subgoals_known_stations_all_ten_inventory_and_memo(self) -> None:
        expected_stations = (
            "crafting_table",
            "furnace",
            "enchanting_table",
            "bed",
            "anvil",
            "smithing_table",
            "jukebox",
            "note_block",
            "crafter",
            "beacon",
        )
        self.assertEqual(_KNOWN_STATIONS, expected_stations)

        for idx, kind in enumerate(expected_stations):
            with self.subTest(via="inventory", station=kind):
                item_name = "red_bed" if kind == "bed" else kind
                st = _state_with(items=[{"name": item_name, "count": 1}])
                self.assertTrue(station_ready(st, kind))
                self.assertTrue(station_ready(st))

            with self.subTest(via="memo", station=kind):
                st = _state_with(items=[])
                memo = WorldResourceMemo()
                block_name = "white_bed" if kind == "bed" else kind
                memo.record_seen("station", block_name, (idx, 64, 0), dimension="overworld", status="reachable")
                self.assertTrue(station_ready(st, kind, memo=memo))
                self.assertTrue(station_ready(st, memo=memo))

    def test_enchantment_ready_positive_negative_dirty_and_edges(self) -> None:
        st_map = _state_with(
            items=[{"name": "minecraft:crossbow", "count": 1, "enchantments": {"minecraft:piercing": 4}}],
            held={"name": "diamond_sword", "count": 1, "enchantments": [{"name": "minecraft:looting", "level": 3}]},
            armor={"boots": {"name": "leather_boots", "count": 1, "enchantments": [{"id": "feather_falling", "lvl": 2}]}},
        )
        self.assertTrue(enchantment_ready(st_map, "crossbow", "piercing", 4))
        self.assertTrue(enchantment_ready(st_map, "minecraft:crossbow", "minecraft:piercing", 3))
        self.assertTrue(enchantment_ready(st_map, "sword", "looting", 3))
        self.assertTrue(enchantment_ready(st_map, "boots", "feather_falling", 2))

        self.assertFalse(enchantment_ready(st_map, "crossbow", "piercing", 5))
        self.assertFalse(enchantment_ready(st_map, "pickaxe", "piercing", 1))
        self.assertFalse(enchantment_ready(st_map, "crossbow", "quick_charge", 1))
        self.assertFalse(enchantment_ready(st_map, "", "piercing", 1))
        self.assertFalse(enchantment_ready(st_map, "crossbow", "", 1))
        self.assertFalse(enchantment_ready(st_map, "crossbow", "piercing", 0))
        self.assertFalse(enchantment_ready(st_map, "crossbow", "piercing", True))  # type: ignore[arg-type]

        st_bool_lvl = _state_with(
            items=[{"name": "crossbow", "count": 1, "enchantments": {"piercing": True}}],
        )
        self.assertFalse(enchantment_ready(st_bool_lvl, "crossbow", "piercing", 1))

        st_zero_count = _state_with(
            items=[{"name": "crossbow", "count": 0, "enchantments": {"piercing": 4}}],
        )
        self.assertFalse(enchantment_ready(st_zero_count, "crossbow", "piercing", 1))

        st_dirty = _state_with(
            items=[{"name": "crossbow", "count": 1, "enchantments": {"piercing": 4}}],
            dirty=True,
        )
        self.assertFalse(enchantment_ready(st_dirty, "crossbow", "piercing", 1))

    def test_mob_collection_ready_positive_negative_dirty_and_edges(self) -> None:
        st_buckets = _state_with(
            items=[
                {"name": "cod_bucket", "count": 2},
                {"name": "salmon_bucket", "count": 1},
                {"name": "tadpole_bucket", "count": 1},
            ],
        )
        self.assertFalse(mob_collection_ready(st_buckets, 5))
        self.assertTrue(mob_collection_ready(st_buckets, 3))

        nearby = [
            {"name": "minecraft:frog", "dist": 5.0},
            {"name": "cow", "dist": 16.0},
            {"name": "cow", "dist": 4.0},      # duplicate species
            {"name": "sheep", "dist": 16.1},   # out of 16-block radius
            {"name": "zombie", "dist": 3.0},   # hostile, excluded
        ]
        self.assertTrue(mob_collection_ready(st_buckets, 5, entities=nearby))
        self.assertFalse(mob_collection_ready(st_buckets, 6, entities=nearby))

        # Entity without dist field still counts if passive animal
        self.assertTrue(mob_collection_ready(st_buckets, 4, entities=[{"name": "pig"}]))
        self.assertFalse(mob_collection_ready(st_buckets, 0))
        self.assertFalse(mob_collection_ready(st_buckets, True))  # type: ignore[arg-type]

        st_dirty = _state_with(
            items=[
                {"name": "cod_bucket", "count": 1},
                {"name": "salmon_bucket", "count": 1},
                {"name": "tropical_fish_bucket", "count": 1},
                {"name": "pufferfish_bucket", "count": 1},
                {"name": "axolotl_bucket", "count": 1},
            ],
            dirty=True,
        )
        self.assertFalse(mob_collection_ready(st_dirty, 5))

    def test_ominous_ready_trial_and_raid_positive_negative_dirty_and_edges(self) -> None:
        base_items = [
            {"name": "ominous_bottle", "count": 1},
            {"name": "iron_sword", "count": 1},
            {"name": "cooked_beef", "count": 4},
        ]
        st_trial = _state_with(health=16.0, food=10.0, items=base_items)
        self.assertTrue(ominous_ready(st_trial, "trial"))
        self.assertFalse(ominous_ready(st_trial, "raid"))  # no milk and armor < 8 / < 2 pieces

        # Active omen effects (mapping, string list, dict list) bypass bottle requirement
        for eff in (
            {"minecraft:bad_omen": 1},
            ["trial_omen"],
            [{"id": "minecraft:raid_omen", "amplifier": 0}],
        ):
            with self.subTest(effects=eff):
                st_eff = _state_with(
                    health=15.0,
                    food=16.0,
                    items=[{"name": "diamond_axe", "count": 1}],
                    effects=eff,
                )
                self.assertTrue(ominous_ready(st_eff, "trial"))

        # Raid mode satisfied by milk_bucket OR >=2 armor pieces OR >=8 armor points
        st_raid_milk = _state_with(items=[*base_items, {"name": "milk_bucket", "count": 1}])
        self.assertTrue(ominous_ready(st_raid_milk, "raid"))

        st_raid_two_pieces = _state_with(
            items=base_items,
            armor={"helmet": {"name": "leather_helmet", "count": 1}, "boots": {"name": "leather_boots", "count": 1}},
        )
        self.assertTrue(ominous_ready(st_raid_two_pieces, "raid"))

        st_raid_chest_pts = _state_with(
            items=base_items,
            armor={"chestplate": {"name": "diamond_chestplate", "count": 1}},
        )
        self.assertTrue(ominous_ready(st_raid_chest_pts, "raid"))

        # Negative / edge cases
        self.assertFalse(ominous_ready(st_trial, "invalid_mode"))
        self.assertFalse(ominous_ready(st_trial, "trial", min_bottles=0))
        self.assertFalse(ominous_ready(st_trial, "trial", min_health=0))
        self.assertFalse(ominous_ready(_state_with(health=13.9, items=base_items), "trial"))
        self.assertFalse(ominous_ready(_state_with(ready=False, items=base_items), "trial"))
        self.assertFalse(ominous_ready(_state_with(alive=False, items=base_items), "trial"))
        self.assertFalse(ominous_ready(_state_with(items=[{"name": "ominous_bottle", "count": 1}, {"name": "cooked_beef", "count": 4}]), "trial"))
        self.assertFalse(ominous_ready(_state_with(food=13.0, items=[{"name": "ominous_bottle", "count": 1}, {"name": "iron_sword", "count": 1}]), "trial"))
        self.assertFalse(ominous_ready(_state_with(items=base_items, dirty=True), "trial"))

        st_safe = _state_with(items=base_items)
        st_safe.safe_mode_until = st_safe.status_at + 60.0
        self.assertFalse(ominous_ready(st_safe, "trial"))

    def test_advancement_criteria_remaining_all_formats_and_edges(self) -> None:
        adv_id = "minecraft:husbandry/leash_all_frog_variants"
        expected = ("temperate", "warm", "cold")
        st = _state_with()

        # Missing snapshot -> returns sorted expected
        self.assertEqual(advancement_criteria_remaining(st, adv_id, expected_criteria=expected), ("cold", "temperate", "warm"))
        self.assertEqual(advancement_criteria_remaining(st, adv_id), ())
        self.assertEqual(advancement_criteria_remaining(st, "", expected_criteria=expected), ())

        # completed list -> empty
        self.assertEqual(
            advancement_criteria_remaining(
                st, adv_id, expected_criteria=expected, advancements={"completed": [adv_id]},
            ),
            (),
        )

        # in_progress dict with criteria mapping
        snap_crit = {
            "in_progress": {
                "husbandry/leash_all_frog_variants": {"criteria": {"temperate": True, "warm": False, "cold": False}},
            }
        }
        self.assertEqual(
            advancement_criteria_remaining(st, adv_id, expected_criteria=expected, advancements=snap_crit),
            ("cold", "warm"),
        )

        # advancements list with remaining field
        snap_rem = {
            "advancements": [{"id": adv_id, "status": "in_progress", "remaining": ["cold"]}]
        }
        self.assertEqual(
            advancement_criteria_remaining(st, adv_id, expected_criteria=expected, advancements=snap_rem),
            ("cold",),
        )

        # completed_criteria field
        snap_done_list = {
            "advancements": [{"id": adv_id, "completed_criteria": ["cold", "temperate"]}]
        }
        self.assertEqual(
            advancement_criteria_remaining(st, adv_id, expected_criteria=expected, advancements=snap_done_list),
            ("warm",),
        )

        # Reads from state.st["advancements"] when advancements arg omitted
        st_embedded = _state_with(extra_status={"advancements": {adv_id: {"done": True}}})
        self.assertEqual(advancement_criteria_remaining(st_embedded, adv_id, expected_criteria=expected), ())

    def test_evaluate_all_subgoals_integration_all_ten_groups(self) -> None:
        st = _state_with(
            health=18.0,
            food=18.0,
            items=[
                {"name": "iron_ingot", "count": 4},
                {"name": "diamond_pickaxe", "count": 1},
                {"name": "crossbow", "count": 1, "enchantments": {"piercing": 4}},
                {"name": "diamond_sword", "count": 1, "enchantments": {"looting": 3}},
                {"name": "ominous_bottle", "count": 1},
                {"name": "milk_bucket", "count": 1},
                {"name": "cooked_beef", "count": 5},
                {"name": "cod_bucket", "count": 1},
                {"name": "salmon_bucket", "count": 1},
                {"name": "tadpole_bucket", "count": 1},
                {"name": "jukebox", "count": 1},
                {"name": "crafter", "count": 1},
            ],
        )
        memo = memo_for(st)
        memo.record_landmark("meadow", (10, 64, 10), dimension="overworld")
        adv = {"advancements": [{"id": "minecraft:husbandry/leash_all_frog_variants", "completed_criteria": ["temperate"]}]}
        ents = [{"name": "cow", "dist": 5.0}, {"name": "frog", "dist": 6.0}]

        res = evaluate_all_subgoals(st, memo=memo, advancements=adv, entities=ents)
        self.assertEqual(len(res), 10)
        self.assertEqual(tuple(res["station_ready"].keys()), _KNOWN_STATIONS)
        self.assertTrue(res["station_ready"]["jukebox"])
        self.assertTrue(res["station_ready"]["crafter"])
        self.assertFalse(res["station_ready"]["beacon"])
        self.assertTrue(res["enchantment_ready"]["piercing_iv"])
        self.assertTrue(res["enchantment_ready"]["looting_iii"])
        self.assertTrue(res["mob_collection_ready"])
        self.assertTrue(res["ominous_ready"]["trial"])
        self.assertTrue(res["ominous_ready"]["raid"])
        self.assertEqual(res["advancement_criteria_remaining"]["frog_variants"], ["cold", "warm"])
        self.assertTrue(res["landmark_reachable"]["meadow"])


# ============================================================================
# 2. Wave A 7 Skills Contract + drive() Tests
# ============================================================================


class WaveASkillsContractTests(_WaveATestBase):
    def test_all_seven_wave_a_skills_registered_offline_contract_and_empty_blocked(self) -> None:
        expected_skills = {
            "crafter_chain": CrafterChain,
            "decorated_pot_sherds": DecoratedPotSherds,
            "jukebox_meadow": JukeboxMeadow,
            "sweep_outpost_captain": SweepOutpostCaptain,
            "allay_cake_birthday": AllayCakeBirthday,
            "loot_end_ship_elytra": LootEndShipElytra,
            "leash_frog_variants": LeashFrogVariants,
        }
        empty_ready = _state_with(items=[])
        for name, cls in expected_skills.items():
            with self.subTest(skill=name):
                self.assertEqual(contract_errors(cls), [])
                self.assertEqual(cls.verified, "offline")
                self.assertIn(name, REGISTRY)
                self.assertIs(REGISTRY[name], cls)
                self.assertTrue(cls.blocked(State()), f"{name} must block on unready State()")
                self.assertTrue(cls.blocked(empty_ready), f"{name} must block on empty inventory")


class CrafterChainTests(_WaveATestBase):
    def test_blocked_missing_materials_and_missing_power(self) -> None:
        full_items = [
            {"name": "crafter", "count": 1},
            {"name": "hopper", "count": 1},
            {"name": "iron_ingot", "count": 5},
            {"name": "crafting_table", "count": 1},
            {"name": "redstone", "count": 2},
            {"name": "dropper", "count": 1},
            {"name": "lever", "count": 1},
        ]
        self.assertEqual(CrafterChain.blocked(_state_with(items=full_items)), "")

        # Missing one iron_ingot (4 < 5)
        short_iron = [({**r, "count": 4} if r["name"] == "iron_ingot" else r) for r in full_items]
        self.assertTrue(CrafterChain.blocked(_state_with(items=short_iron)))

        # Missing redstone power source
        no_power = [r for r in full_items if r["name"] != "lever"]
        self.assertTrue(CrafterChain.blocked(_state_with(items=no_power)))

    def test_happy_path_strict_nine_slot_order_dig_hopper_and_lever_power(self) -> None:
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-4, 4, -4, 4, y=63),
            items={
                "crafter": 1,
                "hopper": 1,
                "iron_ingot": 5,
                "crafting_table": 1,
                "redstone": 2,
                "dropper": 1,
                "lever": 1,
            },
        )
        ex, state = world.session()
        skill = CrafterChain()
        steps = drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")

        place_calls = [req for op, req in world.calls if op == "place"]
        self.assertEqual(place_calls[0]["item"], "crafter")
        self.assertEqual(place_calls[1]["item"], "hopper")
        cpos = tuple(place_calls[0]["pos"])
        hpos = tuple(place_calls[1]["pos"])
        self.assertEqual(hpos, (cpos[0], cpos[1] + 1, cpos[2]))

        # Strict 9-slot 0->8 order loaded into hopper
        wact_calls = [req for op, req in world.calls if op == "window_act"]
        loaded = [(req["ops"][0]["item"], req["ops"][0]["count"]) for req in wact_calls]
        self.assertEqual(
            loaded,
            [
                ("iron_ingot", 4),
                ("crafting_table", 1),
                ("iron_ingot", 1),
                ("redstone", 1),
                ("dropper", 1),
                ("redstone", 1),
            ],
        )
        self.assertTrue(all(req["target"] == {"pos": list(hpos)} for req in wact_calls))
        self.assertEqual(sum(cnt for _, cnt in loaded), 9)

        # Hopper dug before powering
        dig_calls = [req for op, req in world.calls if op == "dig"]
        self.assertIn({"pos": list(hpos)}, dig_calls)
        self.assertNotIn(hpos, world.blocks)

        # Standing <= 8.5 blocks from crafter
        px, py, pz = world.st["pos"]
        self.assertLessEqual(math.dist((px, py + 1.62, pz), (cpos[0] + 0.5, cpos[1] + 0.5, cpos[2] + 0.5)), 8.5)

        # Powered via power_block_once (lever placed adjacent + interacted)
        self.assertEqual(place_calls[2]["item"], "lever")
        lever_pos = place_calls[2]["pos"]
        self.assertEqual(sum(abs(a - b) for a, b in zip(lever_pos, cpos)), 1)
        interact_calls = [req for op, req in world.calls if op == "interact"]
        self.assertTrue(any(req["pos"] == lever_pos for req in interact_calls))
        self.assertTrue(any(s and s.get("op") == "interact" for s in steps))

    def test_happy_path_redstone_torch_power_and_reapproach_when_far(self) -> None:
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-12, 12, -12, 12, y=63),
            items={
                "crafter": 1,
                "hopper": 1,
                "iron_ingot": 5,
                "crafting_table": 1,
                "redstone": 2,
                "dropper": 1,
                "redstone_torch": 1,
            },
        )

        # Simulate knockback/drift after digging hopper so player is > 8.5 blocks away and must re-approach
        def _on_dig(w: WaveAWorld, pos, collect):
            dug = w.blocks.pop(tuple(pos), "air")
            if dug != "air" and collect:
                w.items[dug] = w.items.get(dug, 0) + 1
            w.st["pos"] = [10.5, 64.0, 10.5]
            return {"dug": dug}

        world.on_dig = _on_dig
        ex, state = world.session()
        skill = CrafterChain()
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")

        place_calls = [req for op, req in world.calls if op == "place"]
        cpos = tuple(place_calls[0]["pos"])
        self.assertEqual(place_calls[-1]["item"], "redstone_torch")
        px, py, pz = world.st["pos"]
        self.assertLessEqual(math.dist((px, py + 1.62, pz), (cpos[0] + 0.5, cpos[1] + 0.5, cpos[2] + 0.5)), 8.5)

    def test_env_blocked_when_no_two_high_column(self) -> None:
        # Solid ceiling at y=65 prevents height=2 placement anywhere nearby
        blocks = _flat(-4, 4, -4, 4, y=63)
        for x in range(-4, 5):
            for z in range(-4, 5):
                blocks[(x, 65, z)] = "stone"
                blocks[(x, 64, z)] = "stone"
        blocks.pop((0, 64, 0))
        blocks.pop((0, 65, 0))
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={
                "crafter": 1,
                "hopper": 1,
                "iron_ingot": 5,
                "crafting_table": 1,
                "redstone": 2,
                "dropper": 1,
                "lever": 1,
            },
        )
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(CrafterChain(), ex, state)


class DecoratedPotSherdsTests(_WaveATestBase):
    def test_blocked_empty_or_no_pickaxe_when_short_of_sherds(self) -> None:
        self.assertTrue(DecoratedPotSherds.blocked(_state_with(items=[{"name": "angler_pottery_sherd", "count": 3}])))
        self.assertEqual(DecoratedPotSherds.blocked(_state_with(items=[{"name": "angler_pottery_sherd", "count": 4}])), "")
        self.assertEqual(DecoratedPotSherds.blocked(_state_with(items=[{"name": "iron_pickaxe", "count": 1}])), "")

    def test_happy_path_mine_pots_duplicate_sherds_and_craft_grid(self) -> None:
        blocks = _flat(-4, 4, -4, 4, y=63)
        blocks[(2, 64, 0)] = "decorated_pot"
        blocks[(2, 64, 1)] = "decorated_pot"
        blocks[(-2, 64, 0)] = "crafting_table"
        world = WaveAWorld(pos=(0.5, 64.0, 0.5), blocks=blocks, items={"iron_pickaxe": 1})

        # Each Trial Chamber decorated_pot drops 2 identical sherds -> duplicates allowed!
        def _on_dig(w: WaveAWorld, pos, collect):
            dug = w.blocks.pop(tuple(pos), "air")
            if dug == "decorated_pot" and collect:
                w.items["flow_pottery_sherd"] = w.items.get("flow_pottery_sherd", 0) + 2
            return {"dug": dug}

        def _on_wact(w: WaveAWorld, target, ops):
            w.items["flow_pottery_sherd"] -= 4
            w.items["decorated_pot"] = w.items.get("decorated_pot", 0) + 1
            return {"ok": True, "crafted": "decorated_pot"}

        world.on_dig = _on_dig
        world.on_window_act = _on_wact
        ex, state = world.session()
        skill = DecoratedPotSherds()
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("decorated_pot"), 1)

        wact_calls = [req for op, req in world.calls if op == "window_act"]
        self.assertEqual(len(wact_calls), 1)
        op0 = wact_calls[0]["ops"][0]
        self.assertEqual(op0["op"], "craft_grid")
        self.assertEqual(
            op0["grid"],
            [
                None, "flow_pottery_sherd", None,
                "flow_pottery_sherd", None, "flow_pottery_sherd",
                None, "flow_pottery_sherd", None,
            ],
        )

    def test_fallback_to_ex_craft_on_window_act_unsupported(self) -> None:
        blocks = _flat(-4, 4, -4, 4, y=63)
        blocks[(2, 64, 0)] = "crafting_table"
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"arms_up_pottery_sherd": 2, "burn_pottery_sherd": 2},
        )

        def _fail_wact(w: WaveAWorld, target, ops):
            raise ApiError("unsupported", "craft_grid not supported")

        world.on_window_act = _fail_wact
        ex, state = world.session()
        skill = DecoratedPotSherds()
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")

        craft_calls = [req for op, req in world.calls if op == "craft"]
        self.assertEqual(craft_calls, [{"item": "decorated_pot", "count": 1}])

    def test_env_blocked_when_no_decorated_pots_nearby(self) -> None:
        world = WaveAWorld(pos=(0.5, 64.0, 0.5), blocks=_flat(-4, 4, -4, 4, y=63), items={"iron_pickaxe": 1})
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(DecoratedPotSherds(), ex, state)


class JukeboxMeadowTests(_WaveATestBase):
    def test_blocked_missing_disc_or_jukebox_materials(self) -> None:
        self.assertTrue(JukeboxMeadow.blocked(_state_with(items=[{"name": "jukebox", "count": 1}])))
        self.assertTrue(JukeboxMeadow.blocked(_state_with(items=[{"name": "music_disc_cat", "count": 1}, {"name": "diamond", "count": 1}])))
        self.assertEqual(
            JukeboxMeadow.blocked(_state_with(items=[{"name": "music_disc_cat", "count": 1}, {"name": "jukebox", "count": 1}])),
            "",
        )
        self.assertEqual(
            JukeboxMeadow.blocked(_state_with(items=[
                {"name": "music_disc_13", "count": 1},
                {"name": "diamond", "count": 1},
                {"name": "oak_planks", "count": 8},
            ])),
            "",
        )

    def test_happy_path_in_meadow_crafts_places_and_plays_disc(self) -> None:
        blocks = _flat(-4, 4, -4, 4, y=63)
        blocks[(-2, 64, 0)] = "crafting_table"
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            biome="meadow",
            blocks=blocks,
            items={"diamond": 1, "oak_planks": 8, "music_disc_precipice": 1},
        )
        ex, state = world.session()
        skill = JukeboxMeadow()
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")

        self.assertTrue(any(op == "craft" and req["item"] == "jukebox" for op, req in world.calls))
        placed = [req for op, req in world.calls if op == "place" and req["item"] == "jukebox"]
        self.assertEqual(len(placed), 1)
        jpos = placed[0]["pos"]
        self.assertTrue(any(op == "interact" and req["pos"] == jpos and req["item"] == "music_disc_precipice" for op, req in world.calls))

    def test_env_blocked_outside_meadow_without_landmark(self) -> None:
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            biome="plains",
            blocks=_flat(-4, 4, -4, 4, y=63),
            items={"jukebox": 1, "music_disc_cat": 1},
        )
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(JukeboxMeadow(), ex, state)

    def test_landmark_navigation_into_meadow_happy_path(self) -> None:
        blocks = _flat(-4, 24, -4, 4, y=63)
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            biome="plains",
            blocks=blocks,
            items={"jukebox": 1, "music_disc_blocks": 1},
        )

        def _on_move(w: WaveAWorld, pos, **kwargs):
            if pos is not None:
                w.st["pos"] = [float(v) for v in pos]
                if w.st["pos"][0] >= 18.0:
                    w.st["biome"] = "meadow"
                return {"pos": list(w.st["pos"])}
            return None

        world.on_move_to = _on_move
        ex, state = world.session()
        memo_for(state).record_landmark("meadow", (20, 64, 0), dimension="overworld")

        skill = JukeboxMeadow()
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(world.st["biome"], "meadow")
        self.assertTrue(any(op == "interact" and req["item"] == "music_disc_blocks" for op, req in world.calls))


class SweepOutpostCaptainTests(_WaveATestBase):
    def test_blocked_empty_no_weapon_and_low_hp(self) -> None:
        self.assertTrue(SweepOutpostCaptain.blocked(_state_with(items=[{"name": "bread", "count": 3}])))
        self.assertTrue(SweepOutpostCaptain.blocked(_state_with(health=7.5, items=[{"name": "iron_sword", "count": 1}])))
        self.assertEqual(SweepOutpostCaptain.blocked(_state_with(health=8.0, items=[{"name": "iron_sword", "count": 1}])), "")

    def test_happy_path_shield_raise_sweep_collect_ominous_bottle_and_finally_cleanup(self) -> None:
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-6, 6, -6, 6, y=63),
            items={"iron_sword": 1, "shield": 1},
            entities=[{"id": 77, "name": "pillager", "type": "hostile", "pos": [2.5, 64.0, 0.5]}],
        )

        def _on_attack(w: WaveAWorld, entity_id, max_hits):
            w.entities = [e for e in w.entities if e["id"] != entity_id]
            w.items["ominous_bottle"] = w.items.get("ominous_bottle", 0) + 1
            return {"entity_id": entity_id, "killed": True}

        world.on_attack = _on_attack
        ex, state = world.session()
        skill = SweepOutpostCaptain()
        drive(skill, ex, state)

        self.assertEqual(skill.verify_done(state), "")
        self.assertGreaterEqual(state.count("ominous_bottle"), 1)
        postures = [req for op, req in world.calls if op == "posture"]
        self.assertTrue(any(p.get("shield") is True for p in postures))
        self.assertFalse(postures[-1].get("shield"))
        self.assertFalse(world.st["blocking"])
        self.assertFalse(state.st.get("blocking", False))

    def test_abort_cleans_up_raised_shield_in_finally(self) -> None:
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-8, 8, -8, 8, y=63),
            items={"iron_sword": 1, "shield": 1},
            entities=[{"id": 88, "name": "pillager", "type": "hostile", "pos": [6.5, 64.0, 0.5]}],
        )
        ex, state = world.session()
        skill = SweepOutpostCaptain()

        # Step 1: equip iron_sword; Step 2: shield raised + move_to pillager
        self.assertFalse(skill.step(ex, state))
        self.assertFalse(skill.step(ex, state))
        self.assertTrue(world.st["blocking"])

        skill.abort()
        self.assertFalse(world.st["blocking"])
        self.assertFalse(state.st.get("blocking", False))
        postures = [req for op, req in world.calls if op == "posture"]
        self.assertFalse(postures[-1].get("shield"))

    def test_env_blocked_when_no_pillagers_or_outpost_landmark(self) -> None:
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-4, 4, -4, 4, y=63),
            items={"iron_sword": 1},
        )
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(SweepOutpostCaptain(), ex, state)


class AllayCakeBirthdayTests(_WaveATestBase):
    def test_blocked_empty_missing_cake_or_note_block_materials(self) -> None:
        self.assertTrue(AllayCakeBirthday.blocked(_state_with(items=[{"name": "note_block", "count": 1}])))
        self.assertTrue(AllayCakeBirthday.blocked(_state_with(items=[{"name": "cake", "count": 2}])))
        self.assertEqual(
            AllayCakeBirthday.blocked(_state_with(items=[{"name": "cake", "count": 2}, {"name": "note_block", "count": 1}])),
            "",
        )

    def test_prep_only_villager_trade_path(self) -> None:
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-4, 4, -4, 4, y=63),
            items={"emerald": 6, "redstone": 1, "oak_planks": 8},
            entities=[{"id": 12, "name": "villager", "type": "villager", "pos": [2.0, 64.0, 0.5]}],
        )
        world.on_window_view = lambda w, target: {
            "options": [
                {"index": 0, "give": {"name": "emerald", "count": 1}, "get": {"name": "bread", "count": 6}},
                {"index": 1, "give": {"name": "emerald", "count": 1}, "get": {"name": "cake", "count": 1}, "uses": 0, "max_uses": 12},
            ]
        }

        def _on_wact(w: WaveAWorld, target, ops):
            times = int(ops[0].get("times", 1))
            w.items["emerald"] -= times
            w.items["cake"] = w.items.get("cake", 0) + times
            return {"ok": True}

        world.on_window_act = _on_wact
        ex, state = world.session()
        skill = AllayCakeBirthday(prep_only=True)
        drive(skill, ex, state)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("cake"), 2)
        self.assertEqual(state.count("note_block"), 1)
        self.assertFalse(any(op == "drop" for op, _ in world.calls))

    def test_prep_only_craft_cakes_and_note_block_path(self) -> None:
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-4, 4, -4, 4, y=63),
            items={
                "wheat": 6,
                "sugar": 4,
                "egg": 2,
                "milk_bucket": 6,
                "redstone": 1,
                "oak_planks": 8,
            },
        )
        ex, state = world.session()
        skill = AllayCakeBirthday(prep_only=True)
        drive(skill, ex, state)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("cake"), 2)
        self.assertEqual(state.count("note_block"), 1)
        craft_items = [(req["item"], req["count"]) for op, req in world.calls if op == "craft"]
        self.assertEqual(craft_items, [("cake", 2), ("note_block", 1)])

    def test_full_delivery_path_place_give_ring_drop_and_step_back(self) -> None:
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-8, 8, -8, 8, y=63),
            items={"cake": 2, "note_block": 1},
            entities=[{"id": 55, "name": "allay", "type": "passive", "pos": [2.0, 64.0, 0.5]}],
        )
        ex, state = world.session()
        skill = AllayCakeBirthday(prep_only=False)
        drive(skill, ex, state)

        self.assertEqual(skill.verify_done(state), "")
        placed = [req for op, req in world.calls if op == "place" and req["item"] == "note_block"]
        self.assertEqual(len(placed), 1)
        nb_pos = placed[0]["pos"]

        # Sequence: place note_block -> give 1st cake to allay -> ring note_block -> drop 2nd cake -> step back >= 4 blocks
        action_ops = [op for op, _ in world.calls if op in ("place", "interact", "drop", "move_to")]
        self.assertEqual(action_ops, ["place", "interact", "interact", "drop", "move_to"])
        interacts = [req for op, req in world.calls if op == "interact"]
        self.assertEqual(interacts[0]["entity_id"], 55)
        self.assertEqual(interacts[0]["item"], "cake")
        self.assertEqual(interacts[1]["pos"], nb_pos)
        self.assertIsNone(interacts[1]["item"])

        drops = [req for op, req in world.calls if op == "drop"]
        self.assertEqual(drops, [{"item": "cake", "count": 1}])

        px, _, pz = world.st["pos"]
        self.assertGreaterEqual(math.hypot(px - (nb_pos[0] + 0.5), pz - (nb_pos[2] + 0.5)), 4.0)

    def test_env_blocked_when_no_allay_nearby(self) -> None:
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-4, 4, -4, 4, y=63),
            items={"cake": 2, "note_block": 1},
        )
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(AllayCakeBirthday(prep_only=False), ex, state)


class LootEndShipElytraTests(_WaveATestBase):
    def test_blocked_wrong_dimension_empty_and_no_boarding_mats(self) -> None:
        self.assertTrue(LootEndShipElytra.blocked(_state_with(dimension="overworld", items=[{"name": "ender_pearl", "count": 2}])))
        self.assertTrue(LootEndShipElytra.blocked(_state_with(dimension="the_end", items=[{"name": "cobblestone", "count": 3}])))
        self.assertEqual(LootEndShipElytra.blocked(_state_with(dimension="the_end", items=[{"name": "ender_pearl", "count": 1}])), "")
        self.assertEqual(LootEndShipElytra.blocked(_state_with(dimension="the_end", items=[{"name": "cobblestone", "count": 4}])), "")

    def test_happy_path_pearl_board_shulker_shield_and_primary_attack_max_hits_1(self) -> None:
        blocks = {**_flat(-2, 2, -2, 2, y=63, block="end_stone"), **_flat(8, 12, -2, 2, y=67, block="purpur_block")}
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_end",
            biome="end_highlands",
            blocks=blocks,
            items={"ender_pearl": 2, "shield": 1},
            entities=[
                {"id": 301, "name": "shulker", "type": "hostile", "pos": [8.5, 68.0, 1.5]},
                {"id": 302, "name": "item_frame", "type": "entity", "pos": [10.5, 68.0, 0.5]},
            ],
        )

        def _on_use_item(w: WaveAWorld, item, target):
            w.items[item] -= 1
            if item == "ender_pearl":
                w.st["pos"] = [9.5, 68.0, 0.5]
            return {"used": item}

        def _on_attack(w: WaveAWorld, entity_id, max_hits):
            if entity_id == 302 and max_hits == 1:
                w.items["elytra"] = w.items.get("elytra", 0) + 1
            return {"entity_id": entity_id, "hits": 1}

        world.on_use_item = _on_use_item
        world.on_attack = _on_attack
        ex, state = world.session()
        skill = LootEndShipElytra()
        drive(skill, ex, state)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("elytra"), 1)
        self.assertTrue(any(op == "use_item" and req["item"] == "ender_pearl" for op, req in world.calls))
        self.assertTrue(any(op == "attack" and req["entity_id"] == 302 and req["max_hits"] == 1 for op, req in world.calls))
        postures = [req for op, req in world.calls if op == "posture"]
        self.assertTrue(any(p.get("shield") is True for p in postures))
        self.assertFalse(world.st["blocking"])
        self.assertFalse(world.st["sneaking"])

    def test_sneak_bridge_fallback_boarding_on_unreachable_move(self) -> None:
        # 3-block air gap at x=2,3,4 between island (x<=1) and ship deck (x>=5)
        blocks = {
            **_flat(-1, 1, -1, 1, y=63, block="end_stone"),
            **_flat(5, 8, -1, 1, y=63, block="purpur_block"),
        }
        world = WaveAWorld(
            pos=(1.5, 64.0, 0.5),
            dimension="the_end",
            biome="end_highlands",
            blocks=blocks,
            items={"cobblestone": 8},
            entities=[{"id": 401, "name": "item_frame", "type": "entity", "pos": [6.5, 64.0, 0.5]}],
        )
        move_attempts = {"n": 0}

        def _on_move(w: WaveAWorld, pos, **kwargs):
            move_attempts["n"] += 1
            if move_attempts["n"] == 1:
                raise ApiError("unreachable", "虚空断层无法直接寻路")
            return None

        def _on_attack(w: WaveAWorld, entity_id, max_hits):
            w.items["elytra"] = w.items.get("elytra", 0) + 1
            return {"entity_id": entity_id, "hits": 1}

        world.on_move_to = _on_move
        world.on_attack = _on_attack
        ex, state = world.session()
        skill = LootEndShipElytra()
        drive(skill, ex, state)

        self.assertEqual(skill.verify_done(state), "")
        for gx in (2, 3, 4):
            self.assertEqual(world.blocks.get((gx, 63, 0)), "cobblestone")
        postures = [req for op, req in world.calls if op == "posture"]
        self.assertTrue(any(p.get("sneak") is True for p in postures))
        self.assertFalse(world.st["sneaking"])

    def test_t4_p1_fallback_look_and_control_hit_then_pickup_elytra(self) -> None:
        blocks = _flat(-2, 4, -2, 2, y=63, block="purpur_block")
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_end",
            biome="end_highlands",
            blocks=blocks,
            items={"cobblestone": 6},
            entities=[{"id": 501, "name": "item_frame", "type": "entity", "pos": [2.5, 64.0, 0.5]}],
        )
        popped = {"done": False}

        def _fail_attack(w: WaveAWorld, entity_id, max_hits):
            raise ApiError("unsupported_api", "/attack does not pop item_frame")

        def _on_control(w: WaveAWorld, **keys):
            if keys.get("hit"):
                popped["done"] = True
            return {"ok": True}

        def _on_move(w: WaveAWorld, pos, **kwargs):
            if pos is not None:
                w.st["pos"] = [float(v) for v in pos]
                if popped["done"]:
                    w.items["elytra"] = w.items.get("elytra", 0) + 1
                return {"pos": list(w.st["pos"])}
            return None

        world.on_attack = _fail_attack
        world.on_control = _on_control
        world.on_move_to = _on_move
        ex, state = world.session()
        skill = LootEndShipElytra()
        drive(skill, ex, state)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("elytra"), 1)
        self.assertTrue(any(op == "look" and req["entity_id"] == 501 for op, req in world.calls))
        self.assertTrue(any(op == "control" and req.get("hit") is True for op, req in world.calls))

    def test_abort_cleans_up_shulker_shield_in_finally(self) -> None:
        blocks = _flat(-2, 10, -2, 2, y=63, block="purpur_block")
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_end",
            biome="end_highlands",
            blocks=blocks,
            items={"cobblestone": 8, "shield": 1},
            entities=[
                {"id": 601, "name": "shulker", "type": "hostile", "pos": [5.5, 64.0, 0.5]},
                {"id": 602, "name": "item_frame", "type": "entity", "pos": [8.5, 64.0, 0.5]},
            ],
        )
        ex, state = world.session()
        skill = LootEndShipElytra()

        # First step raises shield against shulker and yields initial move_to
        self.assertFalse(skill.step(ex, state))
        self.assertTrue(world.st["blocking"])

        skill.abort()
        self.assertFalse(world.st["blocking"])
        self.assertFalse(world.st["sneaking"])

    def test_env_blocked_when_no_ship_or_frame(self) -> None:
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_end",
            biome="end_highlands",
            blocks=_flat(-3, 3, -3, 3, y=63, block="end_stone"),
            items={"ender_pearl": 2},
        )
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(LootEndShipElytra(), ex, state)


class LeashFrogVariantsTests(_WaveATestBase):
    def test_blocked_empty_or_insufficient_string_and_no_lead(self) -> None:
        self.assertTrue(LeashFrogVariants.blocked(_state_with(items=[{"name": "string", "count": 4}])))
        self.assertEqual(LeashFrogVariants.blocked(_state_with(items=[{"name": "string", "count": 5}])), "")
        self.assertEqual(LeashFrogVariants.blocked(_state_with(items=[{"name": "lead", "count": 1}])), "")

    def test_happy_path_craft_lead_from_five_string_and_sequential_leash_in_end_cold_biome(self) -> None:
        adv_id = "minecraft:husbandry/leash_all_frog_variants"
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_end",
            biome="end_highlands",
            blocks=_flat(-4, 4, -4, 4, y=63, block="end_stone"),
            items={"string": 5},
            entities=[{"id": 901, "name": "frog", "type": "animal", "pos": [2.0, 64.0, 0.5]}],
            adv_snapshot={
                "advancements": [
                    {"id": adv_id, "status": "in_progress", "completed_criteria": ["temperate", "warm"]},
                ]
            },
        )

        def _on_interact(w: WaveAWorld, pos, entity_id, item):
            if entity_id == 901 and item == "lead":
                w.adv_snapshot = {
                    "advancements": [
                        {"id": adv_id, "status": "completed", "completed_criteria": ["temperate", "warm", "cold"]},
                    ]
                }
            return {"leashed": True}

        world.on_interact = _on_interact
        ex, state = world.session()
        skill = LeashFrogVariants()
        drive(skill, ex, state)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(world.items.get("string"), 0)
        self.assertEqual(state.count("lead"), 1)
        self.assertTrue(any(op == "craft" and req == {"item": "lead", "count": 1} for op, req in world.calls))
        self.assertTrue(any(op == "interact" and req["entity_id"] == 901 and req["item"] == "lead" for op, req in world.calls))
        self.assertEqual(
            advancement_criteria_remaining(
                state,
                adv_id,
                expected_criteria=("temperate", "warm", "cold"),
                advancements=world.adv_snapshot,
            ),
            (),
        )

    def test_release_tadpole_bucket_in_matching_biome_when_no_frog(self) -> None:
        adv_id = "minecraft:husbandry/leash_all_frog_variants"
        blocks = _flat(-4, 4, -4, 4, y=63, block="end_stone")
        blocks[(2, 63, 0)] = "water"
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="the_end",
            biome="the_end",
            blocks=blocks,
            items={"lead": 1, "tadpole_bucket": 1},
            adv_snapshot={
                "advancements": [
                    {"id": adv_id, "status": "in_progress", "completed_criteria": ["temperate"]},
                ]
            },
        )

        def _on_interact(w: WaveAWorld, pos, entity_id, item):
            if item == "tadpole_bucket":
                w.items["tadpole_bucket"] -= 1
                w.items["water_bucket"] = w.items.get("water_bucket", 0) + 1
            return {"released": True}

        world.on_interact = _on_interact
        ex, state = world.session()
        skill = LeashFrogVariants()
        drive(skill, ex, state)

        interacts = [req for op, req in world.calls if op == "interact"]
        self.assertEqual(len(interacts), 1)
        self.assertEqual(interacts[0]["item"], "tadpole_bucket")
        self.assertEqual(interacts[0]["pos"], [2, 63, 0])
        self.assertEqual(state.count("tadpole_bucket"), 0)
        # Releasing tadpole does not falsely mark frog leashing complete
        self.assertTrue(skill.verify_done(state))

    def test_env_blocked_when_remaining_biome_and_entity_missing(self) -> None:
        adv_id = "minecraft:husbandry/leash_all_frog_variants"
        world = WaveAWorld(
            pos=(0.5, 64.0, 0.5),
            dimension="overworld",
            biome="plains",  # temperate, already completed
            blocks=_flat(-4, 4, -4, 4, y=63),
            items={"lead": 1},
            adv_snapshot={
                "advancements": [
                    {"id": adv_id, "status": "in_progress", "completed_criteria": ["temperate"]},
                ]
            },
        )
        ex, state = world.session()
        with self.assertRaises(EnvBlocked):
            drive(LeashFrogVariants(), ex, state)


# ============================================================================
# 3. Wave A Ledger & Shared-Subgraph ROI Contract
# ============================================================================


class WaveALedgerAndROIContractTests(_WaveATestBase):
    def test_wave_a_goals_counts_scores_draft_stop_loss_and_wip1(self) -> None:
        self.assertEqual(len(GOALS), 85)
        self.assertEqual(len(GOAL_MAP), 85)

        scored_all = [g for g in GOALS if g.score > 0]
        self.assertEqual(len(scored_all), 78)
        self.assertEqual(sum(g.score for g in scored_all), 1980)

        t4_prep = [g for g in GOALS if g.tier == "T4_PREP"]
        t4_scored = [g for g in GOALS if g.tier == "T4"]
        self.assertEqual(tuple(g.id for g in t4_prep), WAVE_A_PREP_IDS)
        self.assertEqual(tuple(g.id for g in t4_scored), WAVE_A_SCORED_IDS)
        self.assertEqual(sum(g.score for g in t4_prep), 0)
        self.assertEqual(sum(g.score for g in t4_scored), 255)

        for gid in WAVE_A_ALL_IDS:
            with self.subTest(goal=gid):
                goal = GOAL_MAP[gid]
                self.assertEqual(goal.stage, "draft")
                self.assertTrue(goal.stop_loss)
                self.assertGreater(goal.est_cost_s, 0.0)

        # WIP=1 discipline preserved
        self.assertEqual([g.id for g in GOALS if g.stage == "candidate"], ["smelt_iron"])

    def test_wave_a_goals_validate_bind_offline_skills_and_advancement_scores(self) -> None:
        self.assertEqual(validate_ledger(GOALS), [])
        repo_root = Path(__file__).resolve().parents[2]

        for gid in WAVE_A_ALL_IDS:
            goal = GOAL_MAP[gid]
            with self.subTest(goal=gid):
                self.assertIn(goal.skill, REGISTRY)
                skill_cls = REGISTRY[goal.skill]
                self.assertEqual(skill_cls.verified, "offline")
                self.assertEqual(contract_errors(skill_cls), [])
                _, arg_errs = bind_args(skill_cls, goal.args_dict)
                self.assertEqual(arg_errs, [])
                for rel_path in goal.editable_files:
                    self.assertTrue((repo_root / rel_path).is_file(), f"missing editable file: {rel_path}")
                if goal.score > 0:
                    self.assertEqual(len(goal.done_advancements), 1)
                    adv = goal.done_advancements[0]
                    self.assertIn(adv, ADVANCEMENT_SCORES_V2)
                    self.assertEqual(goal.score, ADVANCEMENT_SCORES_V2[adv])

    def test_rank_actionable_goals_resolves_wave_a_chains_and_shared_t4_prep_amortization(self) -> None:
        # 1) Real Wave A transitive resolution & ranking when earlier milestones are completed
        active = resolve_active_goals(
            GOALS,
            target_goal=(
                "crafters_crafting_crafters",
                "craft_decorated_pot_using_only_sherds",
                "play_jukebox_in_meadows",
                "voluntary_exile",
                "allay_deliver_cake_to_note_block",
                "elytra",
                "leash_all_frog_variants",
            ),
        )
        completed = {
            "minecraft:story/mine_stone",
            "minecraft:story/upgrade_tools",
            "minecraft:story/smelt_iron",
            "minecraft:story/mine_diamond",
            "minecraft:adventure/trade",
            "minecraft:husbandry/allay_deliver_item_to_player",
            "minecraft:end/find_end_city",
        }
        st = _state_with(
            items=[
                {"name": "diamond_pickaxe", "count": 1},
                {"name": "furnace", "count": 1},
                {"name": "iron_ingot", "count": 8},
            ],
        )
        ranked = rank_actionable_goals(st, active, completed_advs=completed)
        by_step = {step.step_goal.id: step for step in ranked}

        # Direct Wave A roots ready immediately
        self.assertIn("elytra", by_step)
        self.assertIn("voluntary_exile", by_step)
        self.assertIn("crafters_crafting_crafters", by_step)
        # T4_PREP step drives allay_deliver_cake_to_note_block
        self.assertIn("prep_two_cakes", by_step)
        self.assertEqual(by_step["prep_two_cakes"].root_goal.id, "allay_deliver_cake_to_note_block")
        # Shared prereq minecraft_trials_edition is amortized across 2 Wave A goals
        self.assertIn("minecraft_trials_edition", by_step)
        self.assertGreaterEqual(by_step["minecraft_trials_edition"].shared_dependents, 2)
        self.assertGreater(by_step["minecraft_trials_edition"].marginal_roi, by_step["minecraft_trials_edition"].roi)

        # 2) Shared T4_PREP amortization (two scored T4 goals sharing prep_ominous_bottle)
        shared_prep = GOAL_MAP["prep_ominous_bottle"]
        t4_branch_a = GoalSpec(
            id="t4_ominous_branch_a",
            title="不祥支线 A",
            tier="T4",
            score=60,
            skill="sweep_outpost_captain",
            prereq_goals=("prep_ominous_bottle",),
            est_cost_s=200.0,
            stop_loss=True,
            stage="draft",
        )
        t4_branch_b = GoalSpec(
            id="t4_ominous_branch_b",
            title="不祥支线 B",
            tier="T4",
            score=60,
            skill="sweep_outpost_captain",
            prereq_goals=("prep_ominous_bottle",),
            est_cost_s=220.0,
            stop_loss=True,
            stage="draft",
        )
        solo_ranked = rank_actionable_goals(st, (shared_prep, t4_branch_a), completed_advs=completed)
        shared_ranked = rank_actionable_goals(st, (shared_prep, t4_branch_a, t4_branch_b), completed_advs=completed)
        self.assertEqual(solo_ranked[0].step_goal.id, "prep_ominous_bottle")
        self.assertEqual(shared_ranked[0].step_goal.id, "prep_ominous_bottle")
        self.assertEqual(solo_ranked[0].shared_dependents, 1)
        self.assertEqual(shared_ranked[0].shared_dependents, 2)
        self.assertEqual(solo_ranked[0].marginal_roi, solo_ranked[0].roi)
        self.assertGreater(shared_ranked[0].marginal_roi, shared_ranked[0].roi)
        self.assertGreater(shared_ranked[0].marginal_roi, solo_ranked[0].marginal_roi)


if __name__ == "__main__":
    unittest.main()
