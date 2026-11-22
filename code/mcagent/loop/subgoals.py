"""Pure subgoal predicates. Missing evidence leaves a condition unconfirmed."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..runtime import State
from ..skills.items import bare
from ..skills.world_memo import BED_BLOCKS, CATEGORY_BLOCKS, memo_for

_TOOL_TIER = {"wooden": 1, "golden": 1, "stone": 2, "copper": 2,
              "iron": 3, "diamond": 4, "netherite": 5}
_KNOWN_STATIONS: tuple[str, ...] = (
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
_FURNACE_BLOCKS: tuple[str, ...] = ("furnace", "blast_furnace", "smoker")
_ANVIL_BLOCKS: tuple[str, ...] = ("anvil", "chipped_anvil", "damaged_anvil")
_WEAPON_EXACT: frozenset[str] = frozenset({"bow", "crossbow", "trident", "mace"})
_AQUATIC_MOB_BUCKETS: tuple[str, ...] = (
    "cod_bucket",
    "salmon_bucket",
    "tropical_fish_bucket",
    "pufferfish_bucket",
    "tadpole_bucket",
    "axolotl_bucket",
)
_LAND_COLLECTION_ANIMALS: frozenset[str] = frozenset(
    {"chicken", "rabbit", "pig", "sheep", "cow", "frog", "cat", "fox"}
)
_OMEN_EFFECTS: frozenset[str] = frozenset({"bad_omen", "trial_omen", "raid_omen"})
_EDIBLE_FOODS: frozenset[str] = frozenset({
    "cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_chicken", "cooked_cod", "cooked_salmon",
    "cooked_rabbit", "bread", "baked_potato", "golden_carrot", "golden_apple", "enchanted_golden_apple",
    "apple", "carrot", "melon_slice", "sweet_berries", "glow_berries", "beef", "porkchop", "mutton",
    "chicken", "cod", "salmon", "rabbit", "potato", "beetroot", "pumpkin_pie", "cookie",
    "rabbit_stew", "mushroom_stew", "beetroot_soup", "dried_kelp", "honey_bottle",
})
_ARMOR_POINTS_BY_MATERIAL: dict[str, tuple[int, int, int, int]] = {
    "leather": (1, 3, 2, 1),
    "golden": (2, 5, 3, 1),
    "chainmail": (2, 5, 4, 1),
    "copper": (2, 4, 3, 1),
    "iron": (2, 6, 5, 2),
    "diamond": (3, 8, 6, 3),
    "netherite": (3, 8, 6, 3),
}
_ARMOR_SLOT_INDEX: dict[str, int] = {
    "helmet": 0,
    "chestplate": 1,
    "leggings": 2,
    "boots": 3,
}
_HAZARD_FLAGS: tuple[str, ...] = ("in_lava", "in_water", "submerged", "on_fire", "in_vehicle")
_RESOURCE_ALIASES: dict[str, str] = {
    "wood": "wood",
    "iron": "iron_ore",
    "iron_ore": "iron_ore",
    "diamond": "diamond_ore",
    "diamond_ore": "diamond_ore",
    "coal": "coal_ore",
    "coal_ore": "coal_ore",
    "lava": "lava",
    "water": "water",
    "obsidian": "obsidian",
}


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _iter_inventory_stacks(inventory: Mapping[str, Any], *, include_armor: bool = True) -> Iterable[Mapping[str, Any]]:
    items = inventory.get("items")
    if isinstance(items, Iterable) and not isinstance(items, (str, bytes, Mapping)):
        for stack in items:
            if isinstance(stack, Mapping) and _is_num(stack.get("count", 1)) and stack.get("count", 1) >= 1:
                yield stack
    for slot_key in ("held", "offhand"):
        stack = inventory.get(slot_key)
        if isinstance(stack, Mapping) and bool(stack) and _is_num(stack.get("count", 1)) and stack.get("count", 1) >= 1:
            yield stack
    if include_armor:
        armor = inventory.get("armor")
        if isinstance(armor, Mapping):
            for stack in armor.values():
                if isinstance(stack, Mapping) and bool(stack) and _is_num(stack.get("count", 1)) and stack.get("count", 1) >= 1:
                    yield stack


def _armor_points_for_item(name: str) -> int:
    bname = bare(name).strip().lower()
    if bname == "turtle_helmet":
        return 2
    mat, _, slot = bname.partition("_")
    idx = _ARMOR_SLOT_INDEX.get(slot)
    pts = _ARMOR_POINTS_BY_MATERIAL.get(mat)
    if idx is not None and pts is not None:
        return pts[idx]
    return 0


def _in_world(status: Mapping, dimension: str) -> bool:
    return bool(dimension and status.get("ready") is True and status.get("alive") is not False
                and bare(status.get("dimension")) == bare(dimension))


def _live_status_and_dim(state: State, dimension: str | None = None) -> tuple[Mapping, str] | None:
    status = state.snapshot()
    if status.get("alive") is False or status.get("ready") is False:
        return None
    cur_dim = bare(status.get("dimension")) or "overworld"
    if dimension is not None and bare(dimension) != cur_dim:
        return None
    return status, cur_dim


def _station_kind_ready(state: State, memo: Any, kind: str, dim: str) -> bool:
    if not state.inventory_dirty:
        if kind == "bed":
            if state.count("bed") >= 1 or any(state.count(b) >= 1 for b in BED_BLOCKS):
                return True
        elif kind == "furnace":
            if any(state.count(f) >= 1 for f in _FURNACE_BLOCKS):
                return True
        elif kind == "anvil":
            if any(state.count(a) >= 1 for a in _ANVIL_BLOCKS):
                return True
        elif state.count(kind) >= 1:
            return True
    if hasattr(memo, "has_station"):
        if kind == "anvil":
            if any(memo.has_station(a, dimension=dim) for a in _ANVIL_BLOCKS):
                return True
        elif memo.has_station(kind, dimension=dim):
            return True
    if hasattr(memo, "query"):
        for entry in memo.query("station", dimension=dim):
            block = bare(getattr(entry, "block", "")).lower()
            if kind == "bed":
                if block == "bed" or block in BED_BLOCKS or block.endswith("_bed"):
                    return True
            elif kind == "furnace":
                if block in _FURNACE_BLOCKS:
                    return True
            elif kind == "anvil":
                if block in _ANVIL_BLOCKS:
                    return True
            elif block == kind:
                return True
    return False


def station_ready(
    state: State,
    observations: Iterable[Mapping] | str | None = None,
    station_type: str | None = None,
    dimension: str | None = None,
    *,
    station: str | None = None,
    memo: Any = None,
) -> bool:
    """Confirm a crafting/smelting/enchanting/sleep station via observations or live inventory+memo."""
    if observations is not None and not isinstance(observations, str):
        target_type = station_type if station_type is not None else (station or "")
        dim = dimension or ""
        return _in_world(state.snapshot(), dim) and any(
            bare(obs.get("name")) == bare(target_type)
            and bare(obs.get("dimension")) == bare(dim)
            and obs.get("reachable") is True
            for obs in observations
        )

    res = _live_status_and_dim(state, dimension)
    if res is None:
        return False
    _, cur_dim = res
    target = bare(observations) if isinstance(observations, str) else bare(station or station_type or "")
    m = memo if memo is not None else memo_for(state)
    if not target:
        return any(_station_kind_ready(state, m, k, cur_dim) for k in _KNOWN_STATIONS)
    return _station_kind_ready(state, m, target, cur_dim)


def item_available(state: State, item: str, quantity: int = 1, *, allocatable: int | None = None) -> bool:
    if type(quantity) is not int or quantity < 1 or state.inventory_dirty:
        return False
    return (state.count(bare(item)) >= quantity
            and (allocatable is None or allocatable >= quantity))


def tool_ready(state: State, purpose: str, tier: str, min_durability: int = 0) -> bool:
    """Match a tool family (e.g. pickaxe), its material tier and available durability."""
    if tier not in _TOOL_TIER or type(min_durability) is not int or min_durability < 0 or state.inventory_dirty:
        return False
    inventory = state.inventory
    armor = inventory.get("armor") or {}
    stacks = (*(inventory.get("items") or ()), inventory.get("held"), inventory.get("offhand"), *armor.values())
    for stack in stacks:
        if not isinstance(stack, dict) or stack.get("count", 1) < 1:
            continue
        material, _, family = bare(stack.get("name")).partition("_")
        if family != purpose or _TOOL_TIER.get(material, 0) < _TOOL_TIER[tier]:
            continue
        durability = stack.get("durability")
        if durability is None:
            if min_durability == 0:
                return True
            continue
        if (isinstance(durability, (list, tuple)) and len(durability) == 2
                and type(durability[0]) is int and durability[0] >= max(1, min_durability)):
            return True
    return False


def _stack_has_enchantment(stack: Mapping[str, Any], target_ench: str, min_level: int) -> bool:
    enchants = stack.get("enchantments")
    if isinstance(enchants, Mapping):
        for k, v in enchants.items():
            if bare(k).strip().lower() == target_ench and isinstance(v, int) and not isinstance(v, bool) and v >= min_level:
                return True
        return False
    if isinstance(enchants, Iterable) and not isinstance(enchants, (str, bytes)):
        for entry in enchants:
            if not isinstance(entry, Mapping):
                continue
            ename = bare(entry.get("name") or entry.get("id")).strip().lower()
            lvl = entry.get("level") if entry.get("level") is not None else entry.get("lvl")
            if ename == target_ench and isinstance(lvl, int) and not isinstance(lvl, bool) and lvl >= min_level:
                return True
    return False


def enchantment_ready(state: State, item_tag: str, enchant_id: str, min_level: int = 1) -> bool:
    """Confirm an inventory/equipped item matching item_tag carries enchant_id >= min_level."""
    if state.inventory_dirty or type(min_level) is not int or min_level < 1:
        return False
    tag = bare(item_tag).strip().lower()
    target_ench = bare(enchant_id).strip().lower()
    if not tag or not target_ench:
        return False

    for stack in _iter_inventory_stacks(state.inventory or {}, include_armor=True):
        name = bare(stack.get("name")).strip().lower()
        if not name or (name != tag and not name.endswith(f"_{tag}")):
            continue
        if _stack_has_enchantment(stack, target_ench, min_level):
            return True
    return False


def mob_collection_ready(
    state: State,
    min_unique: int = 5,
    *,
    entities: Iterable[Mapping] | None = None,
) -> bool:
    """Confirm at least min_unique unique aquatic mob buckets and nearby passive animals."""
    if state.inventory_dirty or type(min_unique) is not int or min_unique < 1:
        return False
    unique_species: set[str] = {
        bucket for bucket in _AQUATIC_MOB_BUCKETS if state.count(bucket) >= 1
    }
    if entities is not None:
        for ent in entities:
            if not isinstance(ent, Mapping):
                continue
            name = bare(ent.get("name")).strip().lower()
            if name not in _LAND_COLLECTION_ANIMALS:
                continue
            dist = ent.get("dist")
            if dist is not None and not (_is_num(dist) and dist <= 16):
                continue
            unique_species.add(name)
    return len(unique_species) >= min_unique


def _has_active_omen_effect(effects: Any) -> bool:
    if isinstance(effects, Mapping):
        return any(
            v is not None and v is not False and bare(k).strip().lower() in _OMEN_EFFECTS
            for k, v in effects.items()
        )
    if isinstance(effects, Iterable) and not isinstance(effects, (str, bytes)):
        for eff in effects:
            if isinstance(eff, str):
                if bare(eff).strip().lower() in _OMEN_EFFECTS:
                    return True
            elif isinstance(eff, Mapping):
                ename = bare(eff.get("name") or eff.get("id") or eff.get("effect")).strip().lower()
                if ename in _OMEN_EFFECTS:
                    return True
    return False


def ominous_ready(
    state: State,
    mode: str = "trial",
    *,
    min_bottles: int = 1,
    min_health: float = 14.0,
) -> bool:
    """Confirm readiness for an ominous trial chamber or village raid."""
    if mode not in ("trial", "raid") or type(min_bottles) is not int or min_bottles < 1:
        return False
    if not _is_num(min_health) or min_health <= 0:
        return False

    res = _live_status_and_dim(state)
    if res is None:
        return False
    status, _ = res
    if state.in_safe_mode or state.status_at < state.safe_mode_until or state.inventory_dirty:
        return False

    health = status.get("health")
    if not (_is_num(health) and health >= min_health):
        return False

    if not (state.count("ominous_bottle") >= min_bottles or _has_active_omen_effect(status.get("effects"))):
        return False

    inv = state.inventory or {}
    has_weapon = False
    has_food_stock = False
    for stack in _iter_inventory_stacks(inv, include_armor=False):
        name = bare(stack.get("name")).strip().lower()
        if not name:
            continue
        if name.endswith(("_sword", "_axe")) or name in _WEAPON_EXACT:
            has_weapon = True
        if name in _EDIBLE_FOODS:
            has_food_stock = True
    if not has_weapon:
        return False

    status_food = status.get("food")
    if not (has_food_stock or (_is_num(status_food) and status_food >= 14)):
        return False

    if mode == "raid":
        if state.count("milk_bucket") >= 1:
            return True
        armor_pieces = 0
        computed_points = 0
        armor = inv.get("armor")
        if isinstance(armor, Mapping):
            for piece in armor.values():
                if isinstance(piece, Mapping) and bool(piece) and _is_num(piece.get("count", 1)) and piece.get("count", 1) >= 1:
                    pname = bare(piece.get("name")).strip().lower()
                    if pname:
                        armor_pieces += 1
                        computed_points += _armor_points_for_item(pname)
        raw_ap = status.get("armor_points")
        if not _is_num(raw_ap):
            raw_ap = status.get("armor")
        armor_points = float(raw_ap) if _is_num(raw_ap) else float(computed_points)
        if armor_points < 8 and armor_pieces < 2:
            return False

    return True


def _find_advancement_entry(source: Any, target_bare: str) -> tuple[bool, Any]:
    if isinstance(source, Mapping):
        if "completed" in source or "in_progress" in source:
            completed = source.get("completed")
            if isinstance(completed, Mapping):
                for k, v in completed.items():
                    if bare(k).strip() == target_bare and v is not None and v is not False:
                        return True, True
            elif isinstance(completed, Iterable) and not isinstance(completed, (str, bytes)):
                for item in completed:
                    if isinstance(item, Mapping):
                        rid = item.get("id") or item.get("name") or item.get("advancement") or item.get("key")
                        if bare(rid).strip() == target_bare:
                            return True, True
                    elif bare(item).strip() == target_bare:
                        return True, True

            in_prog = source.get("in_progress")
            if isinstance(in_prog, Mapping):
                for k, v in in_prog.items():
                    if bare(k).strip() == target_bare:
                        return True, v
            elif isinstance(in_prog, Iterable) and not isinstance(in_prog, (str, bytes)):
                for item in in_prog:
                    if isinstance(item, Mapping):
                        rid = item.get("id") or item.get("name") or item.get("advancement") or item.get("key")
                        if bare(rid).strip() == target_bare:
                            return True, item
                    elif bare(item).strip() == target_bare:
                        return True, {}

        if "advancements" in source:
            inner = source["advancements"]
            if isinstance(inner, Mapping):
                for k, v in inner.items():
                    if bare(k).strip() == target_bare:
                        return True, v
            elif isinstance(inner, Iterable) and not isinstance(inner, (str, bytes)):
                for item in inner:
                    if isinstance(item, Mapping):
                        rid = item.get("id") or item.get("name") or item.get("advancement") or item.get("key")
                        if bare(rid).strip() == target_bare:
                            return True, item
                    elif bare(item).strip() == target_bare:
                        return True, True

        for k, v in source.items():
            if k in ("advancements", "completed", "in_progress"):
                continue
            if bare(k).strip() == target_bare:
                return True, v
    elif isinstance(source, Iterable) and not isinstance(source, (str, bytes)):
        for item in source:
            if isinstance(item, Mapping):
                rid = item.get("id") or item.get("name") or item.get("advancement") or item.get("key")
                if bare(rid).strip() == target_bare:
                    return True, item
            elif bare(item).strip() == target_bare:
                return True, True
    return False, None


def _norm_criterion(value: Any) -> str:
    return bare(value.strip() if isinstance(value, str) else value).strip()


def _extract_missing_criteria(entry: Any, expected: tuple[str, ...] | None) -> tuple[str, ...]:
    norm_expected = (
        tuple(sorted({c for x in expected if (c := _norm_criterion(x))}))
        if expected is not None
        else None
    )
    if entry is True:
        return ()
    if isinstance(entry, str) and entry.strip().lower() in ("completed", "done"):
        return ()
    if isinstance(entry, Mapping):
        if entry.get("status") == "completed" or entry.get("done") is True or entry.get("completed") is True:
            return ()
        rem = entry.get("remaining") if entry.get("remaining") is not None else entry.get("criteria_missing")
        if rem is not None and isinstance(rem, Iterable) and not isinstance(rem, (str, bytes, Mapping)):
            return tuple(sorted({c for item in rem if (c := _norm_criterion(item))}))
        crit = entry.get("criteria")
        if isinstance(crit, Mapping):
            if norm_expected is not None:
                done_set = {c for k, v in crit.items() if v and (c := _norm_criterion(k))}
                return tuple(sorted(c for c in norm_expected if c not in done_set))
            return tuple(sorted({c for k, v in crit.items() if not v and (c := _norm_criterion(k))}))
        done_raw = entry.get("completed_criteria") if entry.get("completed_criteria") is not None else entry.get("criteria_done")
        if done_raw is not None and isinstance(done_raw, Iterable) and not isinstance(done_raw, (str, bytes, Mapping)):
            done_set = {c for item in done_raw if (c := _norm_criterion(item))}
            if norm_expected is not None:
                return tuple(sorted(c for c in norm_expected if c not in done_set))
            return ()
    return norm_expected if norm_expected is not None else ()


def advancement_criteria_remaining(
    state: State,
    adv_id: str,
    *,
    expected_criteria: Iterable[str] | None = None,
    advancements: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return sorted remaining criteria names for adv_id from advancement snapshots."""
    target_bare = bare(adv_id).strip()
    if not target_bare:
        return ()
    expected = (
        tuple(sorted({c for item in expected_criteria if (c := _norm_criterion(item))}))
        if expected_criteria is not None
        else None
    )
    source = advancements
    if source is None:
        source = getattr(state, "advancements", None)
    if source is None:
        source = state.snapshot().get("advancements")
    if source is None:
        return expected if expected is not None else ()

    found, entry = _find_advancement_entry(source, target_bare)
    if not found:
        return expected if expected is not None else ()
    return _extract_missing_criteria(entry, expected)


def landmark_reachable(
    state: State,
    observations: Iterable[Mapping] | str | None = None,
    landmark_type: str | None = None,
    dimension: str | None = None,
    *,
    kind: str | None = None,
    memo: Any = None,
) -> bool:
    """Confirm a reachable landmark or resource zone via observations or live WorldResourceMemo."""
    if observations is not None and not isinstance(observations, str):
        target_type = landmark_type if landmark_type is not None else (kind or "")
        dim = dimension or ""
        return _in_world(state.snapshot(), dim) and any(
            bare(obs.get("type")) == bare(target_type)
            and bare(obs.get("dimension")) == bare(dim)
            and obs.get("reachable") is True and obs.get("return_safe") is True
            for obs in observations
        )

    res = _live_status_and_dim(state, dimension)
    if res is None:
        return False
    _, cur_dim = res
    target = bare(observations) if isinstance(observations, str) else bare(kind or landmark_type or "")
    m = memo if memo is not None else memo_for(state)
    if not target:
        if hasattr(m, "has_landmark") and m.has_landmark(None, dimension=cur_dim):
            return True
        if hasattr(m, "query"):
            if m.query("structure", dimension=cur_dim):
                return True
            if any(m.query(cat, dimension=cur_dim) for cat in ("wood", "iron_ore", "diamond_ore")):
                return True
        return False

    if hasattr(m, "has_landmark") and m.has_landmark(target, dimension=cur_dim):
        return True
    if hasattr(m, "query"):
        if any(getattr(entry, "tag", None) == target for entry in m.query("structure", dimension=cur_dim)):
            return True
        cat = _RESOURCE_ALIASES.get(target) or (target if target in CATEGORY_BLOCKS else None)
        if cat is not None and m.query(cat, dimension=cur_dim):
            return True
    return False


def safe_encounter(
    state: State,
    entity: Mapping | None = None,
    entity_type: str | None = None,
    *,
    dimension: str | None = None,
    retreat_safe: bool | None = None,
    min_health: float = 12.0,
    min_distance: float = 0,
    max_distance: float = 24,
    ammo: tuple[str, int] | None = None,
    memo: Any = None,
) -> bool:
    """Confirm combat readiness against an observed entity or live survival/equipment state."""
    del memo  # Accepted for uniform evaluator signature; live combat readiness uses state/inventory.
    if entity is not None:
        status = state.snapshot()
        if (not dimension or entity_type is None
                or not _in_world(status, dimension) or retreat_safe is not True
                or state.status_at < state.safe_mode_until
                or status.get("on_ground") is False or status.get("in_vehicle")
                or bare(entity.get("dimension", dimension)) != bare(dimension)):
            return False
        health, distance = status.get("health"), entity.get("dist")
        entity_health = entity.get("health")
        return (type(entity.get("id")) is int
                and bare(entity_type) in (bare(entity.get("name")), entity.get("type"))
                and entity.get("type") in ("hostile", "animal")
                and isinstance(entity_health, (int, float)) and entity_health > 0
                and isinstance(health, (int, float)) and health >= min_health
                and not any(status.get(hazard) for hazard in ("in_lava", "in_water", "submerged", "on_fire"))
                and isinstance(distance, (int, float)) and min_distance <= distance <= max_distance
                and (ammo is None or item_available(state, *ammo)))

    res = _live_status_and_dim(state, dimension)
    if res is None or retreat_safe is False:
        return False
    status, _ = res
    if state.in_safe_mode or state.status_at < state.safe_mode_until:
        return False
    if any(status.get(hazard) for hazard in _HAZARD_FLAGS):
        return False
    hp = status.get("health")
    food = status.get("food", 20)
    if not (_is_num(hp) and hp >= min_health and _is_num(food) and food >= 10):
        return False

    has_weapon = False
    armor_pieces = 0
    if not state.inventory_dirty:
        inv = state.inventory
        stacks = (*(inv.get("items") or ()), inv.get("held"), inv.get("offhand"))
        for stack in stacks:
            if not isinstance(stack, Mapping) or stack.get("count", 1) < 1:
                continue
            wname = bare(stack.get("name"))
            if wname.endswith(("_sword", "_axe")) or wname in _WEAPON_EXACT:
                has_weapon = True
                break
        armor = inv.get("armor") or {}
        if isinstance(armor, Mapping):
            armor_pieces = sum(
                1 for piece in armor.values()
                if isinstance(piece, Mapping) and bool(piece) and piece.get("count", 1) >= 1
            )
    if not has_weapon:
        return False

    nearest_hostile = status.get("nearest_hostile")
    if isinstance(nearest_hostile, Mapping):
        hname = bare(nearest_hostile.get("name"))
        hdist = nearest_hostile.get("dist")
        if _is_num(hdist):
            if hname == "creeper" and hdist < 4.5 and armor_pieces < 2:
                return False
            if hdist < 3.0 and hp < 14.0 and armor_pieces == 0:
                return False

    if ammo is not None and not item_available(state, *ammo):
        return False
    return True


def active_portal(
    state: State,
    portal: Mapping | None = None,
    source_dimension: str | None = None,
    destination: str | None = None,
    *,
    memo: Any = None,
) -> bool:
    """Confirm an observed active block in the current world or live portal readiness."""
    if portal is not None:
        if not source_dimension or not destination:
            return False
        source, dest = bare(source_dimension), bare(destination)
        kind = bare(portal.get("name"))
        if not _in_world(state.snapshot(), source) or bare(portal.get("dimension")) != source:
            return False
        if portal.get("destination") and bare(portal["destination"]) != dest:
            return False
        if kind == "nether_portal":
            return (source, dest) in (("overworld", "nether"), ("nether", "overworld"))
        return kind == "end_portal" and (source, dest) in (("overworld", "end"), ("end", "overworld"))

    res = _live_status_and_dim(state)
    if res is None:
        return False
    _, dim = res
    if dim in ("the_nether", "nether"):
        return True
    m = memo if memo is not None else memo_for(state)
    if hasattr(m, "has_active_portal") and m.has_active_portal(dimension=dim):
        return True
    if not state.inventory_dirty and state.count("obsidian") >= 10 and state.count("flint_and_steel") >= 1:
        return True
    return False


def evaluate_all_subgoals(
    state: State,
    *,
    memo: Any = None,
    reservations: Any = None,
    advancements: Mapping[str, Any] | None = None,
    entities: Iterable[Mapping] | None = None,
) -> dict[str, Any]:
    """Evaluate all 10 shared subgoal predicate groups deterministically against live state and memo."""
    m = memo if memo is not None else memo_for(state)
    dim = bare(state.snapshot().get("dimension")) or "overworld"

    def alloc(item: str) -> int | None:
        return reservations.available(state, item) if reservations is not None else None

    discovered_landmarks = (
        m.summary_for_context(dimension=dim).get("landmarks_discovered", ())
        if hasattr(m, "summary_for_context")
        else ()
    )
    return {
        "item_available": {
            "iron_ingot": item_available(state, "iron_ingot", 3, allocatable=alloc("iron_ingot")),
            "diamond": item_available(state, "diamond", 4, allocatable=alloc("diamond")),
            "obsidian": item_available(state, "obsidian", 10, allocatable=alloc("obsidian")),
            "wheat_seeds": item_available(state, "wheat_seeds", 3, allocatable=alloc("wheat_seeds")),
        },
        "tool_ready": {
            "stone": tool_ready(state, "pickaxe", "stone", 1),
            "iron": tool_ready(state, "pickaxe", "iron", 1),
            "diamond": tool_ready(state, "pickaxe", "diamond", 1),
        },
        "station_ready": {
            kind: station_ready(state, kind, memo=m)
            for kind in _KNOWN_STATIONS
        },
        "landmark_reachable": {
            "any": landmark_reachable(state, memo=m),
            "wood": landmark_reachable(state, "wood", memo=m),
            "iron": landmark_reachable(state, "iron", memo=m),
            "diamond": landmark_reachable(state, "diamond", memo=m),
            "village": landmark_reachable(state, "village", memo=m),
            "ruined_portal": landmark_reachable(state, "ruined_portal", memo=m),
            **{
                lk: True
                for lk in discovered_landmarks
                if lk not in ("wood", "iron", "diamond", "village", "ruined_portal", "any")
            },
        },
        "safe_encounter": safe_encounter(state, memo=m),
        "active_portal": active_portal(state, memo=m),
        "enchantment_ready": {
            "piercing_iv": enchantment_ready(state, "crossbow", "piercing", 4),
            "looting_iii": enchantment_ready(state, "sword", "looting", 3),
        },
        "mob_collection_ready": mob_collection_ready(state, 5, entities=entities),
        "ominous_ready": {
            "trial": ominous_ready(state, "trial"),
            "raid": ominous_ready(state, "raid"),
        },
        "advancement_criteria_remaining": {
            "frog_variants": list(
                advancement_criteria_remaining(
                    state,
                    "minecraft:husbandry/leash_all_frog_variants",
                    expected_criteria=("temperate", "warm", "cold"),
                    advancements=advancements,
                )
            ),
        },
    }


__all__ = [
    "active_portal",
    "advancement_criteria_remaining",
    "enchantment_ready",
    "evaluate_all_subgoals",
    "item_available",
    "landmark_reachable",
    "mob_collection_ready",
    "ominous_ready",
    "safe_encounter",
    "station_ready",
    "tool_ready",
]

