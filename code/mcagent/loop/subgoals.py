"""Pure subgoal predicates. Missing evidence leaves a condition unconfirmed."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..runtime import State
from ..skills.items import bare

_TOOL_TIER = {"wooden": 1, "golden": 1, "stone": 2, "copper": 2,
              "iron": 3, "diamond": 4, "netherite": 5}


def _in_world(status: Mapping, dimension: str) -> bool:
    return bool(dimension and status.get("ready") is True and status.get("alive") is not False
                and bare(status.get("dimension")) == bare(dimension))


def station_ready(
    state: State, observations: Iterable[Mapping], station_type: str, dimension: str,
) -> bool:
    return _in_world(state.snapshot(), dimension) and any(
        bare(obs.get("name")) == bare(station_type)
        and bare(obs.get("dimension")) == bare(dimension)
        and obs.get("reachable") is True
        for obs in observations
    )


def item_available(state: State, item: str, quantity: int, *, allocatable: int | None = None) -> bool:
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
    state: State, observations: Iterable[Mapping], landmark_type: str, dimension: str,
) -> bool:
    return _in_world(state.snapshot(), dimension) and any(
        bare(obs.get("type")) == bare(landmark_type)
        and bare(obs.get("dimension")) == bare(dimension)
        and obs.get("reachable") is True and obs.get("return_safe") is True
        for obs in observations
    )


def safe_encounter(
    state: State, entity: Mapping, entity_type: str, *, dimension: str,
    retreat_safe: bool, min_health: float, min_distance: float = 0, max_distance: float = 24,
    ammo: tuple[str, int] | None = None,
) -> bool:
    status = state.snapshot()
    if (not _in_world(status, dimension) or retreat_safe is not True or state.status_at < state.safe_mode_until
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


def active_portal(
    state: State, portal: Mapping, source_dimension: str, destination: str,
) -> bool:
    """Confirm an observed active block in the current world and its route type."""
    source, dest = bare(source_dimension), bare(destination)
    kind = bare(portal.get("name"))
    if not _in_world(state.snapshot(), source) or bare(portal.get("dimension")) != source:
        return False
    if portal.get("destination") and bare(portal["destination"]) != dest:
        return False
    if kind == "nether_portal":
        return (source, dest) in (("overworld", "nether"), ("nether", "overworld"))
    return kind == "end_portal" and (source, dest) in (("overworld", "end"), ("end", "overworld"))
