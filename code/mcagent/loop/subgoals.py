"""Pure subgoal predicates. Missing evidence leaves a condition unconfirmed."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..runtime import State
from ..skills.items import bare
from ..skills.world_memo import BED_BLOCKS, CATEGORY_BLOCKS, memo_for

_TOOL_TIER = {"wooden": 1, "golden": 1, "stone": 2, "copper": 2,
              "iron": 3, "diamond": 4, "netherite": 5}
_KNOWN_STATIONS: tuple[str, ...] = ("crafting_table", "furnace", "enchanting_table", "bed")
_FURNACE_BLOCKS: tuple[str, ...] = ("furnace", "blast_furnace", "smoker")
_WEAPON_EXACT: frozenset[str] = frozenset({"bow", "crossbow", "trident"})
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
        elif state.count(kind) >= 1:
            return True
    if hasattr(memo, "has_station") and memo.has_station(kind, dimension=dim):
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
) -> dict[str, Any]:
    """Evaluate all 6 shared subgoal predicates deterministically against live state and memo."""
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
            for kind in ("crafting_table", "furnace", "bed", "enchanting_table")
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
    }


__all__ = [
    "active_portal",
    "evaluate_all_subgoals",
    "item_available",
    "landmark_reachable",
    "safe_encounter",
    "station_ready",
    "tool_ready",
]

