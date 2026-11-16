"""Read-only API evidence shared by the local skill domains."""
from __future__ import annotations

from ..client import ApiError
from .items import bare


def observe_block(ex, pos) -> dict:
    """§3.4 block snapshot. An unloaded block is unknown."""
    try:
        return ex.client.get_block(pos)
    except ApiError as exc:
        if exc.code == "not_loaded":
            return {}
        raise


def refresh_inventory(ex, state) -> dict:
    """§3.2 fresh bag snapshot for a skill's material or outcome check."""
    state.invalidate_inventory()
    return state.refresh_inventory(ex.client)


def completed_advancements(ex, *, require_ready: bool = True) -> set[str]:
    """§3.7 visible completed IDs without their namespace prefix."""
    try:
        observed = ex.client.advancements(status="completed", include_recipes=False)
    except ApiError as exc:
        if exc.code == "not_ready":
            return set()
        raise
    if require_ready and observed.get("ready") is not True:
        return set()
    return {bare(row["id"]) for row in observed.get("advancements") or ()
            if row.get("id") and row.get("status") == "completed"}
