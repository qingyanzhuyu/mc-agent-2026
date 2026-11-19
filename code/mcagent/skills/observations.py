"""Read-only API evidence shared by the local skill domains."""
from __future__ import annotations

from ..client import ApiError
from ..runtime import Replan
from .items import bare


def observe_block(ex, pos) -> dict:
    """§3.4 block snapshot. An unloaded or not-yet-ready block is unknown."""
    try:
        return ex.client.get_block(pos)
    except ApiError as exc:
        if exc.code in ("not_loaded", "not_ready"):
            return {}
        raise


def refresh_inventory(ex, state) -> dict:
    """§3.2 fresh bag snapshot for a skill's material or outcome check."""
    state.invalidate_inventory()
    try:
        return state.refresh_inventory(ex.client)
    except ApiError as exc:
        if exc.code == "not_ready":
            raise Replan("世界加载尚未就绪（not_ready），请等区块与实体到齐后重读背包") from exc
        raise


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
